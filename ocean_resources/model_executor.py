"""Profiled model API ownership with an injected host-native guarded unload port.

Admission never preloads a model: the caller retains its normal cold-start and
resource gates. Reported API residency plus fresh host capacity is a cooperative
admission contract, not a kernel RAM reservation or a GPU measurement guarantee.
"""
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .adapters import ComputeExecutorAdapter
from .contracts import Checkpoint, ExitReadback, Hold, identity, sha256_hex
from .model_loading import verified_wired_epoch


@dataclass(frozen=True)
class ModelSpec:
    model: str
    digest: str
    context_tokens: int
    port: int = 11434

    def __post_init__(self):
        if (not isinstance(self.model, str) or not 1 <= len(self.model) <= 192
                or any(ord(c) < 33 or ord(c) > 126 for c in self.model)):
            raise ValueError("bounded exact model name required")
        sha256_hex(self.digest)
        if type(self.context_tokens) is not int or not 512 <= self.context_tokens <= 262144:
            raise ValueError("explicit model context limit required")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("explicit loopback model port required")

    def fingerprint(self):
        content = {"schema": "ocean-model-api-workload.v1", "server_version": "0.33.3", **asdict(self)}
        return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ModelAPIExecutor:
    """Owns only the exact configured model grant; host port owns native locking."""
    def __init__(self, arbiter, spec: ModelSpec, port, state_directory: Path, *, load_policy_sha256=None):
        directory = Path(state_directory)
        if (not isinstance(spec, ModelSpec) or not directory.is_absolute() or not directory.is_dir()
                or directory.resolve() != directory or any(p.lower().startswith("onedrive") for p in directory.parts)):
            raise ValueError("explicit model spec and unlinked local state directory required")
        self.arbiter, self.spec, self.port, self.directory = arbiter, spec, port, directory
        if load_policy_sha256 is not None:
            sha256_hex(load_policy_sha256)
        self.load_policy_sha256 = load_policy_sha256
        self._pid = os.getpid()
        self._execution = None
        self._absence_verified = False

    def adapter(self):
        return ComputeExecutorAdapter(self.start, self.checkpoint, self.stop, self.readback,
                                      self.spec.fingerprint(), "model-api", self if self.load_policy_sha256 else None)

    def _owned(self, grant, execution_id, *, phase=None):
        if self._pid != os.getpid() or self._execution != (grant, execution_id):
            raise Hold("foreign model execution")
        self.arbiter.request_for(grant)
        self.arbiter.require_model_evidence(grant, execution_id, self.spec.fingerprint(),
                                           self.spec.model, self.spec.digest, phase=phase)

    def start(self, grant, request, checkpoint, execution_id):
        if self._pid != os.getpid() or self._execution is not None and not self._absence_verified:
            raise Hold("previous model execution remains unresolved")
        identity(execution_id)
        self.arbiter.require_workload(grant, self.spec.fingerprint(), kind="model-api")
        self.port.verify_available(self.spec)
        self._execution = (grant, execution_id)
        self._absence_verified = False
        self.arbiter.record_model_evidence(grant, execution_id, self.spec.fingerprint(),
                                           self.spec.model, self.spec.digest, "admitted")
        # The normal host inference path performs and supervises any cold load.
        return True

    def _observe_load(self, *, loaded, expected_process=None, accept=None, register_epoch=None):
        if self.load_policy_sha256 is None:
            raise Hold("model cold loading is not configured")
        try:
            options = {"expected_process": expected_process,
                       "capacity_reader": self.arbiter.observe_model_capacity, "accept": accept}
            observe_wired = getattr(self.port, "load_wired_observation", None) if loaded else None
            if callable(observe_wired):
                return observe_wired(self.spec, self.load_policy_sha256, register_epoch=register_epoch, **options)
            return self.port.load_observation(self.spec, self.load_policy_sha256, loaded=loaded, **options)
        except Hold:
            raise
        except Exception as exc:
            raise Hold("native model load observation is unavailable") from exc

    def prepare_load(self):
        if getattr(self.port, "supports_load_pending", False) is not True:
            raise Hold("native model residency accounting contract is unavailable; no load may start")
        return self.spec, self.load_policy_sha256, self._observe_load(loaded=False)

    def begin_load(self, grant, execution_id, turn_id):
        self._owned(grant, execution_id)
        state = self.arbiter.load_state(grant)
        if state["phase"] == "MAY_LOAD" and state["turn_id"] == turn_id:
            return state  # Never require absence again after an acknowledged intent.
        observation = self._observe_load(loaded=False)
        return self.arbiter.begin_model_load(grant, self.spec, turn_id, observation)

    def complete_load(self, grant, execution_id, turn_id):
        self._owned(grant, execution_id)
        state = self.arbiter.load_state(grant)
        committed = False
        def accept(observation):
            nonlocal committed
            if committed:
                raise Hold("native load proof cannot be committed twice")
            self.arbiter.complete_model_load(grant, self.spec, turn_id, observation)
            committed = True
        def register_epoch(epoch):
            self._owned(grant, execution_id)
            self.arbiter.register_wired_epoch(grant, execution_id, turn_id, epoch)

        self._observe_load(loaded=True, expected_process=state["baseline"]["process"],
                           accept=accept, register_epoch=register_epoch)
        if not committed:
            raise Hold("native inference owner did not commit the model proof")

    def abort_load(self, grant, execution_id, turn_id):
        self._owned(grant, execution_id)
        state = self.arbiter.load_state(grant)
        committed = False
        def accept():
            nonlocal committed
            if committed:
                raise Hold("native load abort cannot be committed twice")
            self.arbiter.abort_model_load(grant, turn_id)
            committed = True
        if state["phase"] == "PREPARED":
            def accept_absence(observation):
                if observation.resident_bytes != 0:
                    raise Hold("unexpected residency before possible load")
                accept()
            self._observe_load(loaded=False, expected_process=state["baseline"]["process"],
                               accept=accept_absence)
        elif state.get("registered_wired_epoch") is not None:
            abort_wired = getattr(self.port, "abort_wired_load", None)
            if not callable(abort_wired):
                raise Hold("native port cannot abort a registered wired epoch")

            def retire_epoch(epoch):
                self._owned(grant, execution_id)
                return self.arbiter.retire_wired_epoch(grant, execution_id, epoch, abort_turn=turn_id)

            abort_wired(self.spec, state["baseline"]["process"], retire_epoch)
            resident = self.port.resident_bytes(self.spec)
            if type(resident) is not int or resident != 0:
                raise Hold("wired abort has no zero residency readback")
            accept()
        else:
            # Strict host gate + exact process + terminal ACK/absence, never the
            # legacy warmup helper. An unknown dispatch keeps this call held.
            self.port.abort_load(self.spec, state["baseline"]["process"],
                                 lambda: self._owned(grant, execution_id), accept=accept)
        if not committed:
            raise Hold("native inference owner did not commit the model abort")

    def checkpoint(self, grant, execution_id):
        self._owned(grant, execution_id)
        cp = Checkpoint.create(grant, {"schema": "ocean-model-checkpoint.v1",
            "workload_sha256": self.spec.fingerprint(), "conversation_state": "caller-owned"})
        path = self.directory / (execution_id + ".checkpoint.json")
        payload = json.dumps(asdict(cp), sort_keys=True, separators=(",", ":")).encode()
        if path.exists():
            if path.is_symlink() or path.read_bytes() != payload:
                raise Hold("model checkpoint identity changed")
        else:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        self.arbiter.record_model_evidence(grant, execution_id, self.spec.fingerprint(),
            self.spec.model, self.spec.digest, "checkpointed", str(path), hashlib.sha256(payload).hexdigest())
        return cp

    def stop(self, grant, execution_id, checkpoint):
        self._owned(grant, execution_id)
        if not isinstance(checkpoint, Checkpoint) or not checkpoint.valid_for(grant):
            raise Hold("invalid model checkpoint")
        path = self.directory / (execution_id + ".checkpoint.json")
        if path.is_symlink() or json.loads(path.read_bytes()) != asdict(checkpoint):
            raise Hold("durable model checkpoint differs")
        state = self.arbiter.load_state(grant)
        if state is not None and state["phase"] == "PREPARED":
            self.abort_load(grant, execution_id, "cancelled-before-begin")
        elif state is not None and state["phase"] == "ABORTED" and state.get("registered_wired_epoch"):
            # The strict abort already unwired; readback still rechecks absence.
            return
        elif verified_wired_epoch(state) is not None:
            unload = getattr(self.port, "unload_wired", None)
            if not callable(unload):
                raise Hold("native port cannot unload an admitted wired epoch")

            def retire_epoch(epoch):
                self._owned(grant, execution_id, phase="checkpointed")
                return self.arbiter.retire_wired_epoch(grant, execution_id, epoch)

            unload(self.spec, retire_epoch)
        else:
            self.port.unload(self.spec, lambda: self._owned(grant, execution_id, phase="checkpointed"))

    def readback(self, grant, execution_id):
        self._owned(grant, execution_id)
        resident = self.port.resident_bytes(self.spec)  # Unknown must raise, never return zero.
        if type(resident) is not int or resident < 0:
            raise Hold("unknown model residency")
        self._absence_verified = resident == 0
        return ExitReadback(grant, execution_id, self._absence_verified, resident, self.arbiter.now())
