# SPDX-License-Identifier: MIT
"""Optional in-process Ocean model port for the wired Qwen text runtime."""
from dataclasses import asdict
import os
from threading import Lock
import time

from .capacity import DarwinVmStatSample
from .contracts import Grant, Hold, positive, sha256_hex
from .mlx_qwen import QwenTextPlan
from .mlx_qwen_runtime import QwenTextRuntime
from .model_executor import ModelSpec
from .model_loading import (
    DarwinWiredWeightRunner,
    GIB,
    LoadObservation,
    ModelLoadCapacity,
    ProcessIdentity,
    WiredScope,
    build_wired_resident_credit,
)


class InProcessQwenPort:
    """Own native observations and cleanup in the same process as the MLX runtime."""

    supports_load_pending = True

    def __init__(self, spec: ModelSpec, plan: QwenTextPlan, *, policy_sha256: str, headroom_bytes: int):
        if (not isinstance(spec, ModelSpec) or not isinstance(plan, QwenTextPlan)
                or spec.digest != plan.model_digest or not positive(plan.wired_bytes)
                or not positive(headroom_bytes) or headroom_bytes < GIB + GIB // 2):
            raise ValueError("Qwen port requires the exact plan and reviewed headroom")
        sha256_hex(policy_sha256)
        import psutil

        self._spec, self._plan = spec, plan
        self._policy = policy_sha256
        self._headroom = headroom_bytes
        self._psutil = psutil
        self._pid = os.getpid()
        self._lock = Lock()
        self._runner = self._runtime = self._scope = self._epoch = None
        self._loaded = self._accepted = False

    def verify_available(self, spec):
        if spec != self._spec or os.getpid() != self._pid:
            raise Hold("foreign in-process Qwen model or process")

    def _identity(self):
        process = self._psutil.Process(self._pid)
        return ProcessIdentity(process.pid, process.create_time(), self._psutil.boot_time()), process

    def _expect_process(self, expected):
        identity, _ = self._identity()
        if expected is not None and asdict(identity) != expected:
            raise Hold("in-process Qwen model ownership changed")
        return identity

    def _resident(self):
        if self._runner is None or self._runner.retired:
            return 0
        mapped = sum(region.mapped_bytes for region in self._runner.regions.values())
        return mapped + self._plan.subpage_bytes if self._loaded else mapped

    def resident_bytes(self, spec):
        self.verify_available(spec)
        with self._lock:
            return self._resident()

    def _capacity(self, read_capacity):
        started = time.monotonic_ns()
        cap = read_capacity()
        now = time.monotonic()
        sample = cap.platform_sample if isinstance(cap, ModelLoadCapacity) else None
        if (not isinstance(sample, DarwinVmStatSample) or sample.signature_verified is not True
                or sample.sample_started_ns < started
                or sample.sequence != cap.reading.sequence
                or not cap.usable(cap.reading.host_id, now, now - started / 1_000_000_000, -1, loading=True)):
            raise Hold("in-process Qwen needs a signed capacity readback")
        return cap

    def materialize(self, spec, permit, *, authority_id, host_id, client_id, capacity_reader):
        """Load only under the caller's current, durable MAY_LOAD permit."""
        from .model_dispatch import require_dispatch_permission

        self.verify_available(spec)

        def check_owned():
            require_dispatch_permission(permit, authority_id=authority_id, host_id=host_id, client_id=client_id)

        with self._lock:
            check_owned()
            if (permit["spec"] != asdict(spec) or permit["policy_sha256"] != self._policy
                    or permit["workload_sha256"] != spec.fingerprint()):
                raise Hold("in-process Qwen load scope is not owned")
            identity = self._expect_process(permit["baseline"]["process"])
            grant = Grant(**permit["grant"])
            scope = WiredScope(f"darwin-{int(identity.boot_time)}", identity, identity, spec.fingerprint(),
                               spec.digest, spec.context_tokens, authority_id, grant.generation, permit["turn_id"])
            if self._runner is not None and not self._runner.retired:
                raise Hold("previous in-process Qwen load remains unresolved")
            check_owned()
            weights = self._plan.wired_bytes + self._plan.subpage_bytes
            if weights > permit["profile_ram_bytes"]:
                raise Hold("Qwen weights exceed the live load permit")
            cap = self._capacity(capacity_reader)
            minimum = weights + cap.reserve_ram_bytes + self._headroom
            if (cap.reading.host_id != host_id or cap.pressure_level != 1
                    or cap.physical_available_ram_bytes < minimum):
                raise Hold("insufficient signed headroom for the in-process Qwen model")
            check_owned()
            self._accepted = self._loaded = False
            self._epoch = self._runtime = None
            self._scope = scope
            self._runner = DarwinWiredWeightRunner(max_total_bytes=self._plan.wired_bytes)
            self._runtime = QwenTextRuntime(self._plan, self._runner, context_tokens=spec.context_tokens)
            self._runtime.load(check_owned)
            self._loaded = True

    def _observe(self, spec, cap, identity, *, loaded, credit=None):
        after, process = self._identity()
        if after != identity:
            raise Hold("in-process Qwen identity changed during proof")
        return LoadObservation(
            spec.fingerprint(), self._policy, identity, time.monotonic(),
            cap.physical_available_ram_bytes, cap.pressure_level, self._psutil.swap_memory().used,
            self._resident(), process.memory_info().rss, spec.context_tokens if loaded else None,
            self._headroom, cap, credit,
        )

    def load_observation(self, spec, policy_sha256, *, loaded, expected_process=None, capacity_reader, accept=None):
        self.verify_available(spec)
        with self._lock:
            if policy_sha256 != self._policy or loaded or self._resident():
                raise Hold("in-process Qwen absence or policy is unproved")
            if self._runner is not None and not self._runner.retired:
                raise Hold("in-process Qwen setup remains unresolved")
            identity = self._expect_process(expected_process)
            obs = self._observe(spec, self._capacity(capacity_reader), identity, loaded=False)
            if accept is not None:
                accept(obs)
            return obs

    def load_wired_observation(self, spec, policy_sha256, *, expected_process, capacity_reader, accept, register_epoch):
        self.verify_available(spec)
        with self._lock:
            identity = self._expect_process(expected_process)
            if (policy_sha256 != self._policy or not self._loaded or self._accepted
                    or self._scope.listener != identity):
                raise Hold("in-process Qwen load is unavailable or has changed")
            before = self._runner.export_epoch(self._scope)
            self._epoch = before
            register_epoch(before)
            cap = self._capacity(capacity_reader)
            after = self._runner.export_epoch(self._scope)
            credit = build_wired_resident_credit(
                scope=self._scope, before_sample_epoch=before, after_sample_epoch=after,
                pre_commit_epoch=self._runner.export_epoch(self._scope),
                platform_sample=cap.platform_sample, observed_at=time.monotonic(),
            )
            obs = self._observe(spec, cap, identity, loaded=True, credit=credit)
            accept(obs)
            self._accepted = True
            return obs

    def generate(self, spec, prompt, max_new_tokens, check_owned, *, stop_tokens=()):
        self.verify_available(spec)
        with self._lock:
            if not self._accepted:
                raise Hold("in-process Qwen proof has not been accepted")
            return self._runtime.generate(prompt, max_new_tokens, check_owned, stop_tokens=stop_tokens)

    def _dispose(self, retirement=None):
        self._accepted = False
        if self._runner is None or self._runner.retired:
            return
        if self._runtime is not None:
            self._runtime.drain()
        audit = (self._runner.emergency_teardown() if retirement is None
                 else self._runner.retire_and_unwire(retirement))
        if not audit["cleanup_verified"] or not self._runner.retired:
            raise Hold("in-process Qwen cleanup is unconfirmed")
        self._runtime = None
        self._loaded = False

    def unload(self, spec, check_owned):
        self.verify_available(spec)
        with self._lock:
            check_owned()
            if self._epoch is not None:
                raise Hold("registered Qwen epoch requires durable retirement")
            self._dispose()

    def abort_load(self, spec, expected_process, check_owned, *, accept):
        self.verify_available(spec)
        with self._lock:
            self._expect_process(expected_process)
            check_owned()
            if self._epoch is not None:
                raise Hold("registered Qwen epoch requires durable abort")
            self._dispose()
            accept()

    def unload_wired(self, spec, retire_epoch):
        self.verify_available(spec)
        with self._lock:
            if self._epoch is None or self._expect_process(None) != self._epoch.scope.runner:
                raise Hold("no exact in-process Qwen epoch to retire")
            self._accepted = False
            self._dispose(retire_epoch(self._epoch))

    def abort_wired_load(self, spec, expected_process, retire_epoch):
        self.verify_available(spec)
        self._expect_process(expected_process)
        self.unload_wired(spec, retire_epoch)
