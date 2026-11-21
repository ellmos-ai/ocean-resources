# SPDX-License-Identifier: MIT
"""Production #4218 ResidentCredit v2, Apple /usr/bin/vm_stat and wired Metal runner tests."""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import sys
import time
from types import SimpleNamespace

import pytest

from ocean_resources import (
    BACHFacade,
    CapacityReadback,
    ControllerClient,
    DarwinVmStatSample,
    DarwinWiredWeightRunner,
    DurableRetirement,
    Grant,
    Hold,
    HostController,
    HostLocalArbiter,
    HostWriterAuthority,
    ResourceSocket,
    SlotRequest,
    WIRED_CREDIT_SOURCE,
    WiredBacking,
    WiredEpoch,
    WiredResidentCreditProof,
    WiredScope,
    build_wired_resident_credit,
    parse_darwin_vm_stat_output,
    parse_safetensors_header,
    permit_unwire,
    sample_darwin_vm_stat,
)
from ocean_resources.capacity import CapacityPolicy, NativeCapacitySensor
from ocean_resources.host import HostObservation
from ocean_resources.model_executor import ModelAPIExecutor, ModelSpec
from ocean_resources.model_loading import (
    LoadObservation,
    ModelLoadCapacity,
    ProcessIdentity,
    ResidentCredit,
)
from ocean_resources.profiles import ProfileSource, ReviewedProfileGate, native_architecture

GIB = 1024**3
PAGE = 16_384
POLICY = "e" * 64
VM_STAT_SHA = "b" * 64
WEIGHTS_SHA = "c" * 64


def _make_sample(
    sequence: int,
    *,
    started_ns: int = 2_000,
    completed_ns: int = 3_000,
    free_pages: int = 100_000,
    speculative_pages: int = 10_000,
    inactive_pages: int = 200_000,
    wired_pages: int = 120_000,
    page_size: int = PAGE,
) -> DarwinVmStatSample:
    return DarwinVmStatSample(
        sequence=sequence,
        page_size=page_size,
        pages_free=free_pages,
        pages_speculative=speculative_pages,
        pages_inactive=inactive_pages,
        pages_wired=wired_pages,
        sample_started_ns=started_ns,
        sample_completed_ns=completed_ns,
        vm_stat_sha256=VM_STAT_SHA,
        signature_verified=True,
        os_release="27.2.0",
    )


def _write_safetensors(path: Path, tensors: dict[str, tuple[str, tuple[int, ...], bytes]]) -> Path:
    header: dict[str, object] = {"__metadata__": {"format": "pt", "fixture": "4218"}}
    offset = 0
    chunks = []
    for name, (dtype, shape, payload) in tensors.items():
        end = offset + len(payload)
        header[name] = {"dtype": dtype, "shape": list(shape), "data_offsets": [offset, end]}
        chunks.append(payload)
        offset = end
    raw_header = json.dumps(header, separators=(",", ":")).encode("utf-8")
    path.write_bytes(struct.pack("<Q", len(raw_header)) + raw_header + b"".join(chunks))
    return path


@pytest.fixture
def wired_host(tmp_path):
    now, sequence = [100.0], [0]
    host = platform.node()
    spec = ModelSpec("fixture:27b-wired", "a" * 64, 16384)
    profile_ram = 14 * GIB
    reserve = 2 * GIB
    headroom = 2 * GIB
    # Available = 6 GiB initially for cold start, then 4.5 GiB post-load (insufficient without credit,
    # sufficient once 12 GiB wired credit brings remaining peak demand down to max(14-12, 2) = 2 GiB
    # and available - reserve = 4.5 - 2 = 2.5 GiB >= 2 GiB).
    available_pages = [(6 * GIB) // PAGE]
    listener = ProcessIdentity(4321, 10.0, 1.0)
    runner_proc = ProcessIdentity(4322, 11.0, 1.0)

    evidence = {
        "schema": "ocean-model-measurement.v2",
        "scope": "ollama-model-api",
        "method": "ollama-ps-native-rss-and-cpu-v2",
        "host_id": host,
        "platform": platform.system(),
        "architecture": native_architecture(),
        "workload_sha256": spec.fingerprint(),
        "model_digest": spec.digest,
        "model_spec": asdict(spec),
        "peak_resident_bytes": profile_ram - 1 * GIB,
        "peak_process_rss_bytes": profile_ram - 1 * GIB,
        "process_identity_verified": True,
        "measured_processes": [{"pid": 4321, "created_at": 10.0, "boot_time": 1.0}],
        "peak_cpu_units": 0.6,
        "cpu_concurrency_units": 1,
        "sample_count": 3,
        "sample_interval_seconds": 1,
        "started_at": now[0] - 10,
        "finished_at": now[0] - 6,
        "unload_ack_verified": True,
        "model_absence_verified": True,
        "server_version": "0.33.3",
        "unmeasured_external_residency": False,
    }
    ep = tmp_path / "evidence.json"
    ep.write_text(json.dumps(evidence), encoding="utf-8")
    profile = {
        "schema": "ocean-reviewed-workload-profile.v1",
        "kind": "model-api",
        "profile_id": "wired-27b-v2",
        "socket_id": "model",
        "host_id": host,
        "platform": platform.system(),
        "architecture": native_architecture(),
        "workload_sha256": spec.fingerprint(),
        "ram_bytes": profile_ram,
        "cpu_units": 1,
        "ram_margin_bytes": 1 * GIB,
        "approved_by": "fixture-reviewer",
        "approved_at": now[0] - 5,
        "expires_at": now[0] + 3600,
        "evidence_sha256": hashlib.sha256(ep.read_bytes()).hexdigest(),
    }
    pp = tmp_path / "profile.json"
    pp.write_text(json.dumps(profile), encoding="utf-8")
    gate = ReviewedProfileGate(
        (ProfileSource(pp.resolve(), hashlib.sha256(pp.read_bytes()).hexdigest(), ep.resolve()),),
        max_age_seconds=3600,
        wall_clock=lambda: now[0],
    )
    socket = ResourceSocket("model", host, profile_ram, 1, "wired-27b-v2", spec.fingerprint())

    state = {
        "resident": 0,
        "rss": 12 * GIB + 512 * 1024 * 1024,
        "credit_builder": None,
        "cap_kwargs": {},
    }

    class WiredSensor:
        def __call__(self):
            return self.model_load_readback().reading

        def model_load_readback(self):
            kw = state["cap_kwargs"]
            sequence[0] += 1
            total_pages = available_pages[0]
            speculative_pages = kw.get("speculative_pages", 100)
            free_pages = max(0, total_pages - speculative_pages - 1000)
            inactive_pages = total_pages - free_pages - speculative_pages
            ps = _make_sample(
                sequence[0],
                started_ns=kw.get("started_ns", 2_000),
                completed_ns=kw.get("completed_ns", 3_000),
                free_pages=free_pages,
                speculative_pages=speculative_pages,
                inactive_pages=inactive_pages,
            )
            phys = ps.available_bytes
            reading = CapacityReadback(host, sequence[0], now[0], max(0, phys - reserve), 4, True, True)
            with_ps = kw.get("with_platform_sample", True)
            return ModelLoadCapacity(reading, phys, reserve, 1, platform_sample=ps if with_ps else None)

    sensor = WiredSensor()

    class WiredPort:
        supports_load_pending = True

        def verify_available(self, actual):
            assert actual == spec

        def unload(self, actual, owned):
            owned()
            state["resident"] = 0

        def resident_bytes(self, actual):
            return state["resident"]

        def load_observation(self, actual, policy_sha256, *, loaded, expected_process=None, capacity_reader=None, accept=None):
            assert actual == spec and policy_sha256 == POLICY
            cap = capacity_reader()
            credit = None
            if loaded and state["credit_builder"] is not None:
                credit = state["credit_builder"](cap)
            obs = LoadObservation(
                spec.fingerprint(),
                policy_sha256,
                listener,
                now[0],
                cap.physical_available_ram_bytes,
                1,
                0,
                state["resident"],
                state["rss"],
                spec.context_tokens if loaded else None,
                headroom,
                cap,
                credit,
            )
            if accept is not None:
                accept(obs)
            return obs

        def abort_load(self, actual, expected_process, owned, *, accept=None):
            owned()
            state["resident"] = 0
            if accept is not None:
                accept()

    with HostWriterAuthority(tmp_path.resolve(), host) as writer:
        arbiter = HostLocalArbiter(
            writer,
            (socket,),
            sensor,
            lambda: now[0],
            profile_gate=gate,
            heartbeat_ttl=30,
        )
        authority_id = writer.authority_id
        port = WiredPort()
        executor = ModelAPIExecutor(arbiter, spec, port, tmp_path.resolve(), load_policy_sha256=POLICY)
        client = ControllerClient("bach", frozenset({"model"}), allow_interactive=True,
                                  require_leases=True, allow_model_load=True)
        operator = ControllerClient("operator", frozenset({"model"}), operator=True)
        ctl = HostController(BACHFacade(arbiter, {"model": executor.adapter()}), (client, operator))

        def make_scope(grant: Grant, turn_id: str = "turn-1", **overrides) -> WiredScope:
            base = dict(
                boot_id="boot-1",
                listener=listener,
                runner=runner_proc,
                workload_sha256=spec.fingerprint(),
                model_digest=spec.digest,
                context_tokens=spec.context_tokens,
                owner_id=authority_id,
                owner_fence=grant.generation,
                turn_id=turn_id,
            )
            base.update(overrides)
            return WiredScope(**base)

        def make_backing(pages=(12 * GIB) // PAGE, **overrides) -> WiredBacking:
            base = dict(
                allocation_id="alloc-1",
                whole_weight_pages=pages,
                page_size=PAGE,
                weights_sha256=WEIGHTS_SHA,
            )
            base.update(overrides)
            return WiredBacking(**base)

        yield SimpleNamespace(
            ctl=ctl,
            client=client,
            operator=operator,
            arbiter=arbiter,
            writer=writer,
            spec=spec,
            host=host,
            now=now,
            available_pages=available_pages,
            state=state,
            make_scope=make_scope,
            make_backing=make_backing,
            listener=listener,
            runner_proc=runner_proc,
            profile_ram=profile_ram,
            reserve=reserve,
            headroom=headroom,
        )


def _start_and_begin(h, turn_id="turn-1"):
    req = SlotRequest("req-1", "model", "bach-model", h.host, "idem-1", interactive=True)
    h.ctl.submit_model_load(h.client.client_id, h.client.token, req, 900)
    status = h.ctl.tick(h.operator.client_id, h.operator.token)
    assert status["state"] == "LOAD_PENDING"
    grant = Grant(**status["torch"])
    h.ctl.begin_model_load(h.client.client_id, h.client.token, grant, turn_id)
    return grant


# --- 1. Production ResidentCredit v2 contract tests (full arbiter lifecycle) ---

def test_bound_wired_epoch_credits_unique_pages_and_promotes_to_running(wired_host):
    grant = _start_and_begin(wired_host)
    wired_host.state["resident"] = 12 * GIB
    wired_host.available_pages[0] = (4 * GIB + 512 * 1024 * 1024) // PAGE
    scope = wired_host.make_scope(grant, "turn-1")
    backing = wired_host.make_backing()
    # Two aliased views of the exact same physical allocation count once.
    epoch = WiredEpoch(scope, 1, 1_500, (backing, backing))
    wired_host.state["credit_builder"] = lambda cap: build_wired_resident_credit(
        scope=scope,
        before_sample_epoch=epoch,
        after_sample_epoch=epoch,
        pre_commit_epoch=epoch,
        platform_sample=cap.platform_sample,
        observed_at=wired_host.now[0],
    )
    status = wired_host.ctl.complete_model_load(
        wired_host.client.client_id, wired_host.client.token, grant, "turn-1", True
    )
    assert status["state"] == "RUNNING"
    saved = wired_host.writer.unresolved()
    assert saved["load_state"]["phase"] == "VERIFIED"
    assert saved["load_state"]["completion"]["credited_bytes"] == 12 * GIB


def _expect_complete_hold(wired_host, grant, expected_inner_substring: str, turn_id: str = "turn-1"):
    with pytest.raises(Hold, match="independent native reconciliation") as exc_info:
        wired_host.ctl.complete_model_load(
            wired_host.client.client_id, wired_host.client.token, grant, turn_id, True
        )
    cause = exc_info.value.__cause__
    assert isinstance(cause, Hold) and expected_inner_substring in str(cause), (
        f"Expected inner Hold containing {expected_inner_substring!r}, got {cause!r}"
    )
    assert wired_host.ctl.tick(wired_host.operator.client_id, wired_host.operator.token)["state"] == "RECOVERY_HOLD"


def test_reported_allocation_without_wired_credit_cannot_complete(wired_host):
    grant = _start_and_begin(wired_host)
    wired_host.state["resident"] = 12 * GIB
    wired_host.available_pages[0] = (4 * GIB + 512 * 1024 * 1024) // PAGE
    wired_host.state["credit_builder"] = None
    _expect_complete_hold(wired_host, grant, "normal reserve and provable remaining peak capacity")


def test_stale_or_non_platform_capacity_sample_fails_even_with_new_sequence(wired_host):
    grant = _start_and_begin(wired_host)
    wired_host.state["resident"] = 12 * GIB
    wired_host.available_pages[0] = (4 * GIB + 512 * 1024 * 1024) // PAGE
    scope = wired_host.make_scope(grant, "turn-1")
    backing = wired_host.make_backing()
    # Case A: sample started BEFORE wire completed (wire_completed_ns=2500 > started_ns=2000).
    late_epoch = WiredEpoch(scope, 1, 2_500, (backing,))
    wired_host.state["credit_builder"] = lambda cap: ResidentCredit(
        bytes=12 * GIB,
        workload_sha256=scope.workload_sha256,
        process=scope.listener,
        capacity_sequence=cap.reading.sequence,
        observed_at=wired_host.now[0],
        source=WIRED_CREDIT_SOURCE,
        wired_proof=WiredResidentCreditProof(scope, late_epoch, late_epoch, late_epoch),
    )
    _expect_complete_hold(wired_host, grant, "unproved capacity sample")


def test_missing_platform_sample_on_capacity_rejects_v2_credit(wired_host):
    grant = _start_and_begin(wired_host)
    wired_host.state["resident"] = 12 * GIB
    wired_host.available_pages[0] = (4 * GIB + 512 * 1024 * 1024) // PAGE
    wired_host.state["cap_kwargs"] = {"with_platform_sample": False}
    scope = wired_host.make_scope(grant, "turn-1")
    epoch = WiredEpoch(scope, 1, 1_500, (wired_host.make_backing(),))
    wired_host.state["credit_builder"] = lambda cap: ResidentCredit(
        bytes=12 * GIB,
        workload_sha256=scope.workload_sha256,
        process=scope.listener,
        capacity_sequence=cap.reading.sequence,
        observed_at=wired_host.now[0],
        source=WIRED_CREDIT_SOURCE,
        wired_proof=WiredResidentCreditProof(scope, epoch, epoch, epoch),
    )
    _expect_complete_hold(wired_host, grant, "unproved capacity sample")


def test_wrong_capacity_sequence_fails(wired_host):
    grant = _start_and_begin(wired_host)
    wired_host.state["resident"] = 12 * GIB
    wired_host.available_pages[0] = (4 * GIB + 512 * 1024 * 1024) // PAGE
    scope = wired_host.make_scope(grant, "turn-1")
    epoch = WiredEpoch(scope, 1, 1_500, (wired_host.make_backing(),))
    wired_host.state["credit_builder"] = lambda cap: replace(
        build_wired_resident_credit(
            scope=scope,
            before_sample_epoch=epoch,
            after_sample_epoch=epoch,
            pre_commit_epoch=epoch,
            platform_sample=cap.platform_sample,
            observed_at=wired_host.now[0],
        ),
        capacity_sequence=cap.reading.sequence - 1,
    )
    _expect_complete_hold(wired_host, grant, "cannot be credited")


@pytest.mark.parametrize(
    "change",
    [
        {"owner_fence": 999},
        {"owner_id": "foreign-authority"},
        {"turn_id": "turn-other"},
        {"model_digest": "f" * 64},
        {"context_tokens": 8192},
        {"listener": ProcessIdentity(9999, 10.0, 1.0)},
    ],
)
def test_mismatched_scope_fields_fail(wired_host, change):
    grant = _start_and_begin(wired_host)
    wired_host.state["resident"] = 12 * GIB
    wired_host.available_pages[0] = (4 * GIB + 512 * 1024 * 1024) // PAGE
    scope = wired_host.make_scope(grant, **change)
    epoch = WiredEpoch(scope, 1, 1_500, (wired_host.make_backing(),))
    wired_host.state["credit_builder"] = lambda cap: build_wired_resident_credit(
        scope=scope,
        before_sample_epoch=epoch,
        after_sample_epoch=epoch,
        pre_commit_epoch=epoch,
        platform_sample=cap.platform_sample,
        observed_at=wired_host.now[0],
    )
    with pytest.raises(Hold):
        wired_host.ctl.complete_model_load(
            wired_host.client.client_id, wired_host.client.token, grant, "turn-1", True
        )
    assert wired_host.ctl.tick(wired_host.operator.client_id, wired_host.operator.token)["state"] == "RECOVERY_HOLD"


@pytest.mark.parametrize("tamper", ["after_changed", "precommit_changed", "precommit_empty"])
def test_changed_or_lost_epoch_before_commit_fails(wired_host, tamper):
    grant = _start_and_begin(wired_host)
    wired_host.state["resident"] = 12 * GIB
    wired_host.available_pages[0] = (4 * GIB + 512 * 1024 * 1024) // PAGE
    scope = wired_host.make_scope(grant, "turn-1")
    e1 = WiredEpoch(scope, 1, 1_500, (wired_host.make_backing(),))
    e2 = WiredEpoch(scope, 2, 1_500, (wired_host.make_backing(),))
    e_empty = WiredEpoch(scope, 1, 1_500, ())
    proof = {
        "after_changed": WiredResidentCreditProof(scope, e1, e2, e1),
        "precommit_changed": WiredResidentCreditProof(scope, e1, e1, e2),
        "precommit_empty": WiredResidentCreditProof(scope, e1, e1, e_empty),
    }[tamper]
    wired_host.state["credit_builder"] = lambda cap: ResidentCredit(
        bytes=12 * GIB,
        workload_sha256=scope.workload_sha256,
        process=scope.listener,
        capacity_sequence=cap.reading.sequence,
        observed_at=wired_host.now[0],
        source=WIRED_CREDIT_SOURCE,
        wired_proof=proof,
    )
    _expect_complete_hold(wired_host, grant, "wire epoch")


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "file_backed"},
        {"kind": "purgeable"},
        {"no_copy_weight": False},
        {"wire_succeeded": False},
        {"no_fork_inherit": False},
        {"whole_weight_pages": 0},
    ],
)
def test_ineligible_backing_cannot_be_credited(wired_host, change):
    grant = _start_and_begin(wired_host)
    scope = wired_host.make_scope(grant, "turn-1")
    backing = wired_host.make_backing(**change)
    epoch = WiredEpoch(scope, 1, 1_500, (backing,))
    with pytest.raises(Hold, match="ineligible"):
        epoch.verified_wired_bytes(PAGE)


def test_conflicting_alias_claim_raises_hold(wired_host):
    grant = _start_and_begin(wired_host)
    scope = wired_host.make_scope(grant, "turn-1")
    b1 = wired_host.make_backing(whole_weight_pages=100)
    b2 = wired_host.make_backing(whole_weight_pages=101)
    epoch = WiredEpoch(scope, 1, 1_500, (b1, b2))
    with pytest.raises(Hold, match="inconsistent physical allocation identity"):
        epoch.verified_wired_bytes(PAGE)


def test_proved_wire_cannot_substitute_for_available_reserve_plus_headroom(wired_host):
    grant = _start_and_begin(wired_host)
    wired_host.state["resident"] = 12 * GIB
    # Reserve (2 GiB) + headroom (2 GiB) = 4 GiB; set available = 4 GiB - 1 page -> MUST HOLD!
    wired_host.available_pages[0] = (4 * GIB) // PAGE - 1
    scope = wired_host.make_scope(grant, "turn-1")
    epoch = WiredEpoch(scope, 1, 1_500, (wired_host.make_backing(),))
    wired_host.state["credit_builder"] = lambda cap: build_wired_resident_credit(
        scope=scope,
        before_sample_epoch=epoch,
        after_sample_epoch=epoch,
        pre_commit_epoch=epoch,
        platform_sample=cap.platform_sample,
        observed_at=wired_host.now[0],
    )
    _expect_complete_hold(wired_host, grant, "normal reserve and provable remaining peak capacity")


def test_permit_unwire_requires_exact_durable_retired_readback(wired_host):
    grant = _start_and_begin(wired_host)
    scope = wired_host.make_scope(grant, "turn-1")
    epoch = WiredEpoch(scope, 7, 1_500, (wired_host.make_backing(),))
    permit_unwire(epoch, DurableRetirement("RETIRED", scope, 7))
    for bad in (
        None,
        DurableRetirement("RECOVERY_HOLD", scope, 7),
        DurableRetirement("VERIFIED", scope, 7),
        DurableRetirement("RETIRED", scope, 6),
        DurableRetirement("RETIRED", replace(scope, owner_fence=99), 7),
        DurableRetirement("RETIRED", replace(scope, runner=ProcessIdentity(9999, 11.0, 1.0)), 7),
    ):
        with pytest.raises(Hold, match="durable readback"):
            permit_unwire(epoch, bad)


# --- 2. /usr/bin/vm_stat parser & sensor contract tests ---

def test_parse_darwin_vm_stat_output_exact_projection_and_rejections():
    sample_stdout = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               200399.\n"
        "Pages active:                             520000.\n"
        "Pages inactive:                           434759.\n"
        "Pages speculative:                         37129.\n"
        "Pages wired down:                         141631.\n"
    )
    parsed = parse_darwin_vm_stat_output(sample_stdout, expected_page_size=16384)
    sample = DarwinVmStatSample(
        sequence=1,
        page_size=16384,
        pages_free=parsed["Pages free"],
        pages_speculative=parsed["Pages speculative"],
        pages_inactive=parsed["Pages inactive"],
        pages_wired=parsed["Pages wired down"],
        sample_started_ns=100,
        sample_completed_ns=200,
        vm_stat_sha256=VM_STAT_SHA,
        signature_verified=True,
        os_release="27.2.0",
    )
    # Exact projection verified in S440: (200399 + 37129 + 434759) * 16384 = 11014750208
    assert sample.available_bytes == 11_014_750_208
    # Removing speculative pages changes the exact metric by speculative * page_size.
    without_spec = replace(sample, pages_speculative=0)
    assert sample.available_bytes - without_spec.available_bytes == 37129 * 16384

    # Wrong page size, duplicate keys, missing speculative, or invalid line fail closed.
    with pytest.raises(Hold, match="platform_sample_header"):
        parse_darwin_vm_stat_output(sample_stdout, expected_page_size=4096)
    with pytest.raises(Hold, match="platform_sample_schema"):
        parse_darwin_vm_stat_output(sample_stdout + "Pages free: 1.\n", expected_page_size=16384)
    missing_spec = "\n".join(line for line in sample_stdout.splitlines() if "speculative" not in line)
    with pytest.raises(Hold, match="platform_available_fields_missing"):
        parse_darwin_vm_stat_output(missing_spec, expected_page_size=16384)


def test_sample_darwin_vm_stat_verifies_signature_and_binary_identity(tmp_path):
    fake_bin = tmp_path / "vm_stat"
    fake_bin.write_bytes(b"fake-signed-vm-stat")
    sha = hashlib.sha256(fake_bin.read_bytes()).hexdigest()
    stdout = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free: 100.\n"
        "Pages speculative: 20.\n"
        "Pages inactive: 300.\n"
        "Pages wired down: 50.\n"
    )
    ticks = iter([1000, 2000])

    def runner(cmd, **kw):
        if cmd[0] == "/usr/bin/codesign":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    sample = sample_darwin_vm_stat(
        sequence=5,
        expected_sha256=sha,
        path=fake_bin,
        require_root_owner=False,
        runner=runner,
        clock_ns=lambda: next(ticks),
        page_size=16384,
        os_release="27.2.0",
    )
    assert sample.available_bytes == (100 + 20 + 300) * 16384
    assert sample.vm_stat_sha256 == sha and sample.signature_verified is True

    # Signature failure holds.
    with pytest.raises(Hold, match="vm_stat_apple_signature_unconfirmed"):
        sample_darwin_vm_stat(
            sequence=6,
            expected_sha256=sha,
            path=fake_bin,
            require_root_owner=False,
            runner=lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="invalid"),
            clock_ns=lambda: 3000,
            page_size=16384,
            os_release="27.2.0",
        )

    # Binary replacement during execution holds.
    def mutate_during_run(cmd, **kw):
        if cmd[0] == "/usr/bin/codesign":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        fake_bin.write_bytes(b"tampered-vm-stat-binary")
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    with pytest.raises(Hold, match="sample_binary_changed"):
        sample_darwin_vm_stat(
            sequence=7,
            expected_sha256=sha,
            path=fake_bin,
            require_root_owner=False,
            runner=mutate_during_run,
            clock_ns=lambda: 4000,
            page_size=16384,
            os_release="27.2.0",
        )


def test_native_capacity_sensor_platform_model_load_readback(tmp_path, monkeypatch):
    host = platform.node()
    policy = CapacityPolicy(2 * GIB, 1, 1024, tmp_path.resolve(), sample_seconds=0.1, max_age_seconds=2.0)
    fake_obs = HostObservation(
        host_id=host,
        platform="Darwin",
        observed_at=1_700_000_000.0,
        total_ram_bytes=32 * GIB,
        available_ram_bytes=10 * GIB,
        logical_cpus=4,
        load_average_1m=0.5,
        pressure="normal",
        disk_free_bytes=100 * GIB,
        measurement_source="fixture-darwin-vmstat",
    )
    monkeypatch.setattr("ocean_resources.capacity.sample_host", lambda _dir: fake_obs)
    snap_idx = [0]

    class FakeCpu:
        def __init__(self, idle):
            self._d = {"user": 1.0, "system": 1.0, "idle": idle}

        def _asdict(self):
            return dict(self._d)

    class FakePsutil:
        version_info = (7, 2, 2)

        def cpu_times(self, percpu=True):
            snap_idx[0] += 1
            idle = 10.0 if snap_idx[0] == 1 else 10.9
            return [FakeCpu(idle) for _ in range(4)]

    monkeypatch.setattr("ocean_resources.capacity._psutil", lambda: FakePsutil())
    sensor = NativeCapacitySensor(
        policy,
        host_id=host,
        vm_stat_reader=lambda *, sequence: _make_sample(
            sequence,
            free_pages=100_000,
            speculative_pages=20_000,
            inactive_pages=400_000,
        ),
    )
    mlc = sensor.platform_model_load_readback()
    expected_avail = (100_000 + 20_000 + 400_000) * PAGE
    assert mlc.platform_sample is not None
    assert mlc.physical_available_ram_bytes == expected_avail
    assert mlc.reading.available_ram_bytes == expected_avail - 2 * GIB
    assert mlc.usable(host, time.monotonic(), 2.0, 0, loading=False) is True


# --- 3. .safetensors header validation tests ---

def test_parse_safetensors_header_valid_and_rejections(tmp_path):
    st_path = _write_safetensors(
        tmp_path / "model.safetensors",
        {
            "layers.0.weight": ("F32", (1024, 4), b"\x01\x02\x03\x04" * 4096),
            "layers.0.bias": ("F32", (16,), b"\x00" * 64),
        },
    )
    data_offset, tensors = parse_safetensors_header(st_path)
    assert data_offset > 8
    assert tensors["layers.0.weight"].nbytes == 16_384
    assert tensors["layers.0.bias"].nbytes == 64

    # Overlapping offsets must fail closed.
    bad_header = json.dumps({
        "a": {"dtype": "F32", "shape": [4], "data_offsets": [0, 16]},
        "b": {"dtype": "F32", "shape": [4], "data_offsets": [8, 24]},
    }).encode("utf-8")
    bad = tmp_path / "overlap.safetensors"
    bad.write_bytes(struct.pack("<Q", len(bad_header)) + bad_header + b"\x00" * 32)
    with pytest.raises(Hold, match="overlap"):
        parse_safetensors_header(bad)

    # Out-of-bounds offset must fail closed.
    oob_header = json.dumps({
        "a": {"dtype": "F32", "shape": [4], "data_offsets": [0, 1024]},
    }).encode("utf-8")
    oob = tmp_path / "oob.safetensors"
    oob.write_bytes(struct.pack("<Q", len(oob_header)) + oob_header + b"\x00" * 16)
    with pytest.raises(Hold, match="out_of_bounds"):
        parse_safetensors_header(oob)


# --- 4. Live Apple Silicon macOS end-to-end Metal + .safetensors + /usr/bin/vm_stat + Arbiter test ---

@pytest.mark.skipif(
    sys.platform != "darwin" or platform.machine() != "arm64",
    reason="Requires Apple Silicon macOS Metal runtime and signed /usr/bin/vm_stat",
)
def test_darwin_live_safetensors_wired_metal_vmstat_and_arbiter_lifecycle(tmp_path):
    """End-to-end physical #4218 test on live Apple Silicon hardware.

    Loads a multi-tensor >4 MiB .safetensors file into mlock'd anonymous pages,
    binds Metal bytesNoCopy buffers, executes a Metal compute kernel over every
    32-bit word and page boundary, takes a live signed /usr/bin/vm_stat sample
    inside the active WiredEpoch, promotes a staged HostController load with
    exact deduplicated credit (excluding partial-page tail bytes), verifies
    RECOVERY_HOLD refuses unwire, and confirms Metal deallocator + munlock +
    munmap ordering on RETIRED readback.
    """
    import psutil

    if psutil.virtual_memory().available < 4 * GIB:
        pytest.skip("Insufficient live available RAM headroom for wired Metal test")

    page = os.sysconf("SC_PAGE_SIZE")
    assert page == 16_384

    # 1024 exact pages (16 MiB) + 4 whole pages plus 512-byte partial tail + 1024-byte subpage bias.
    w1_bytes = (struct.pack("<I", 0x11223344) + struct.pack("<I", 0x55667788)) * ((1024 * page) // 8)
    w2_bytes = struct.pack("<I", 0xAABBCCDD) * ((4 * page + 512) // 4)
    bias_bytes = b"\x00" * 1024
    st_path = _write_safetensors(
        tmp_path / "fixture-weights.safetensors",
        {
            "block0.mlp.weight": ("F32", (1024, page // 4), w1_bytes),
            "block0.proj.weight": ("F32", (4 * page // 4 + 128,), w2_bytes),
            "block0.norm.bias": ("F32", (256,), bias_bytes),
        },
    )

    runner = DarwinWiredWeightRunner(max_total_bytes=32 * 1024 * 1024)
    try:
        loaded = runner.load_safetensors(st_path, skip_subpage_tensors=True)
        assert [r.tensor_name for r in loaded] == ["block0.mlp.weight", "block0.proj.weight"]
        assert loaded[0].whole_weight_pages == 1024
        assert loaded[1].whole_weight_pages == 4  # 512-byte partial tail excluded!
        runner.register_tensor_view("block0.mlp.weight_view", "block0.mlp.weight")

        # Exporting before GPU verification must fail closed.
        proc = psutil.Process()
        live_proc = ProcessIdentity(proc.pid, proc.create_time(), psutil.boot_time())
        spec = ModelSpec("fixture:live-metal-safetensors", hashlib.sha256(st_path.read_bytes()).hexdigest(), 4096)
        pre_scope = WiredScope(
            boot_id=f"boot-{int(psutil.boot_time())}",
            listener=live_proc,
            runner=live_proc,
            workload_sha256=spec.fingerprint(),
            model_digest=spec.digest,
            context_tokens=spec.context_tokens,
            owner_id="pre-check",
            owner_fence=1,
            turn_id="turn-live",
        )
        with pytest.raises(Hold, match="gpu_verification"):
            runner.export_epoch(pre_scope)

        # Verify Metal GPU compute reads every word and page boundary of both tensors.
        gpu1 = runner.verify_gpu_weight_read("block0.mlp.weight")
        gpu2 = runner.verify_gpu_weight_read("block0.proj.weight")
        assert gpu1["gpu_status"] == 4 and gpu1["whole_weight_pages"] == 1024
        assert gpu2["gpu_status"] == 4 and gpu2["whole_weight_pages"] == 4

        # Wire up a real HostController + HostLocalArbiter with signed /usr/bin/vm_stat.
        host = platform.node()
        now = time.monotonic()
        vm_before = sample_darwin_vm_stat(sequence=1)
        assert vm_before.signature_verified is True and vm_before.page_size == page
        expected_credit = (1024 + 4) * page  # 16,842,752 bytes (view deduplicated, tail excluded)

        reserve = 2 * GIB
        headroom = 2 * GIB
        if vm_before.available_bytes < 6 * GIB + 256 * 1024 * 1024:
            pytest.skip("Insufficient live available RAM for cold-start floor + 4 GiB profile")
        tight_profile = 4 * GIB
        evidence = {
            "schema": "ocean-model-measurement.v2",
            "scope": "ollama-model-api",
            "method": "ollama-ps-native-rss-and-cpu-v2",
            "host_id": host,
            "platform": "Darwin",
            "architecture": native_architecture(),
            "workload_sha256": spec.fingerprint(),
            "model_digest": spec.digest,
            "model_spec": asdict(spec),
            "peak_resident_bytes": tight_profile - 1 * GIB,
            "peak_process_rss_bytes": tight_profile - 1 * GIB,
            "process_identity_verified": True,
            "measured_processes": [asdict(live_proc)],
            "peak_cpu_units": 0.5,
            "cpu_concurrency_units": 1,
            "sample_count": 3,
            "sample_interval_seconds": 1,
            "started_at": now - 10,
            "finished_at": now - 5,
            "unload_ack_verified": True,
            "model_absence_verified": True,
            "server_version": "0.33.3",
            "unmeasured_external_residency": False,
        }
        ep = tmp_path / "live-ev.json"
        ep.write_text(json.dumps(evidence), encoding="utf-8")
        profile = {
            "schema": "ocean-reviewed-workload-profile.v1",
            "kind": "model-api",
            "profile_id": "live-wired-v2",
            "socket_id": "model",
            "host_id": host,
            "platform": "Darwin",
            "architecture": native_architecture(),
            "workload_sha256": spec.fingerprint(),
            "ram_bytes": tight_profile,
            "cpu_units": 1,
            "ram_margin_bytes": 1 * GIB,
            "approved_by": "live-fixture",
            "approved_at": now - 4,
            "expires_at": now + 3600,
            "evidence_sha256": hashlib.sha256(ep.read_bytes()).hexdigest(),
        }
        pp = tmp_path / "live-prof.json"
        pp.write_text(json.dumps(profile), encoding="utf-8")
        gate = ReviewedProfileGate(
            (ProfileSource(pp.resolve(), hashlib.sha256(pp.read_bytes()).hexdigest(), ep.resolve()),),
            max_age_seconds=3600,
            wall_clock=lambda: now,
        )
        socket = ResourceSocket("model", host, tight_profile, 1, "live-wired-v2", spec.fingerprint())
        seq = [10]

        class LivePlatformSensor:
            def __call__(self):
                return self.model_load_readback().reading

            def model_load_readback(self):
                seq[0] += 1
                ps = sample_darwin_vm_stat(sequence=seq[0])
                ts = time.monotonic()
                reading = CapacityReadback(host, seq[0], ts, ps.available_bytes - reserve, 4, True, True)
                return ModelLoadCapacity(reading, ps.available_bytes, reserve, 1, platform_sample=ps)

        sensor = LivePlatformSensor()
        live_scope_box: dict[str, object] = {}

        class LiveWiredPort:
            supports_load_pending = True

            def verify_available(self, actual):
                assert actual == spec

            def unload(self, actual, owned):
                owned()

            def resident_bytes(self, actual):
                return expected_credit

            def load_observation(self, actual, policy_sha256, *, loaded, expected_process=None, capacity_reader=None, accept=None):
                if not loaded:
                    cap = capacity_reader()
                    obs = LoadObservation(
                        spec.fingerprint(), policy_sha256, live_proc, time.monotonic(),
                        cap.physical_available_ram_bytes, 1, 0, 0, max(expected_credit, proc.memory_info().rss),
                        None, headroom, cap, None,
                    )
                    if accept is not None:
                        accept(obs)
                    return obs
                scope = live_scope_box["scope"]
                before_epoch = runner.export_epoch(scope)
                cap = capacity_reader()
                after_epoch = runner.export_epoch(scope)
                pre_commit_epoch = runner.export_epoch(scope)
                credit = build_wired_resident_credit(
                    scope=scope,
                    before_sample_epoch=before_epoch,
                    after_sample_epoch=after_epoch,
                    pre_commit_epoch=pre_commit_epoch,
                    platform_sample=cap.platform_sample,
                    observed_at=time.monotonic(),
                )
                rss = max(expected_credit, proc.memory_info().rss)
                obs = LoadObservation(
                    spec.fingerprint(), policy_sha256, live_proc, time.monotonic(),
                    cap.physical_available_ram_bytes, 1, 0, expected_credit, rss,
                    spec.context_tokens, headroom, cap, credit,
                )
                if accept is not None:
                    accept(obs)
                return obs

            def abort_load(self, actual, expected_process, owned, *, accept=None):
                owned()
                if accept is not None:
                    accept()

        state_dir = tmp_path / "live-state"
        state_dir.mkdir()
        with HostWriterAuthority(state_dir.resolve(), host) as writer:
            arbiter = HostLocalArbiter(
                writer,
                (socket,),
                sensor,
                time.monotonic,
                profile_gate=gate,
                heartbeat_ttl=30,
            )
            executor = ModelAPIExecutor(arbiter, spec, LiveWiredPort(), state_dir.resolve(), load_policy_sha256=POLICY)
            client = ControllerClient("bach", frozenset({"model"}), allow_interactive=True,
                                      require_leases=True, allow_model_load=True)
            operator = ControllerClient("operator", frozenset({"model"}), operator=True)
            ctl = HostController(BACHFacade(arbiter, {"model": executor.adapter()}), (client, operator))
            req = SlotRequest("live-req", "model", "bach-model", host, "live-idem", interactive=True)
            ctl.submit_model_load(client.client_id, client.token, req, 900)
            st = ctl.tick(operator.client_id, operator.token)
            assert st["state"] == "LOAD_PENDING"
            grant = Grant(**st["torch"])
            ctl.begin_model_load(client.client_id, client.token, grant, "turn-live")
            live_scope = WiredScope(
                boot_id=f"boot-{int(psutil.boot_time())}",
                listener=live_proc,
                runner=live_proc,
                workload_sha256=spec.fingerprint(),
                model_digest=spec.digest,
                context_tokens=spec.context_tokens,
                owner_id=writer.authority_id,
                owner_fence=grant.generation,
                turn_id="turn-live",
            )
            live_scope_box["scope"] = live_scope
            completed = ctl.complete_model_load(client.client_id, client.token, grant, "turn-live", True)
            assert completed["state"] == "RUNNING"
            saved = writer.unresolved()
            assert saved["load_state"]["completion"]["credited_bytes"] == expected_credit
            assert saved["load_state"]["completion"]["remaining_ram_bytes"] == tight_profile - expected_credit

            # RECOVERY_HOLD must block unwiring!
            with pytest.raises(Hold, match="durable readback"):
                runner.retire_and_unwire(DurableRetirement("RECOVERY_HOLD", live_scope, runner.generation))
            assert runner.retired is False

            # Only durable RETIRED readback unlocks Metal deallocation -> munlock -> munmap.
            audit = runner.retire_and_unwire(DurableRetirement("RETIRED", live_scope, runner.generation))
            assert audit["cleanup_verified"] is True
            assert audit["regions_released"] == 2
            assert runner.retired is True
    finally:
        if not runner.retired:
            runner.emergency_teardown()
