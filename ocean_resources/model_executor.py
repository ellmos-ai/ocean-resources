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
    def __init__(self, arbiter, spec: ModelSpec, port, state_directory: Path):
        directory = Path(state_directory)
        if (not isinstance(spec, ModelSpec) or not directory.is_absolute() or not directory.is_dir()
                or directory.resolve() != directory or any(p.lower().startswith("onedrive") for p in directory.parts)):
            raise ValueError("explicit model spec and unlinked local state directory required")
        self.arbiter, self.spec, self.port, self.directory = arbiter, spec, port, directory
        self._pid = os.getpid()
        self._execution = None
        self._absence_verified = False

    def adapter(self):
        return ComputeExecutorAdapter(self.start, self.checkpoint, self.stop, self.readback,
                                      self.spec.fingerprint(), "model-api")

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
        self.port.unload(self.spec, lambda: self._owned(grant, execution_id, phase="checkpointed"))

    def readback(self, grant, execution_id):
        self._owned(grant, execution_id)
        resident = self.port.resident_bytes(self.spec)  # Unknown must raise, never return zero.
        if type(resident) is not int or resident < 0:
            raise Hold("unknown model residency")
        self._absence_verified = resident == 0
        return ExitReadback(grant, execution_id, self._absence_verified, resident, self.arbiter.now())
