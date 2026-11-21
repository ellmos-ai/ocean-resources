# SPDX-License-Identifier: MIT
"""Model-only staged admission. No threshold here authorizes a compute grant.

The native port is trusted host code, never a client-supplied measurement. A
residency credit must describe attributable, private, non-reclaimable host RAM
in the same capacity observation. API size, total RSS and GPU/Metal allocation
alone do not implement that contract and must leave ``credit`` absent.
"""
from copy import deepcopy
import ctypes as C
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import struct
import sys
import threading
import time
import uuid

from .capacity import DARWIN_VM_STAT_SOURCE, DarwinVmStatSample
from .contracts import CapacityReadback, Grant, Hold, instant, positive, sha256_hex

GIB = 1024**3
MIN_AVAILABLE = 6 * GIB
HARD_FLOOR = 2 * GIB
MAX_SECONDS = 300
MAX_PRESSURE = 2
MAX_SWAP_GROWTH = 8 * GIB
LEGACY_CREDIT_SOURCE = "private-nonreclaimable-model-pages.v1"
WIRED_CREDIT_SOURCE = "darwin-wired-metal-nocopy-vmstat.v2"
_SAFETENSORS_MAX_HEADER_BYTES = 64 * 1024 * 1024


def _bounded_token(value: object, name: str, limit: int = 128) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be a non-empty bounded string")
    return value


def _power_of_two_page_size(value: object) -> bool:
    return positive(value) and value >= 4096 and (value & (value - 1)) == 0


def _json_roundtrip(value: object) -> object:
    """Normalize dataclass dicts (e.g. tuples -> lists) to exact durable JSON form."""
    return json.loads(json.dumps(value, allow_nan=False))


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    created_at: float
    boot_time: float

    def __post_init__(self):
        if (not positive(self.pid) or not instant(self.created_at) or not instant(self.boot_time)
                or not 0 < self.boot_time <= self.created_at):
            raise ValueError("complete native process identity required")


@dataclass(frozen=True)
class WiredScope:
    """Binds a physical wired-weight epoch to listener, runner, model and grant."""
    boot_id: str
    listener: ProcessIdentity
    runner: ProcessIdentity
    workload_sha256: str
    model_digest: str
    context_tokens: int
    owner_id: str
    owner_fence: int
    turn_id: str

    def __post_init__(self):
        _bounded_token(self.boot_id, "boot_id")
        if not isinstance(self.listener, ProcessIdentity) or not isinstance(self.runner, ProcessIdentity):
            raise ValueError("listener and runner process identities required")
        sha256_hex(self.workload_sha256)
        sha256_hex(self.model_digest)
        if not positive(self.context_tokens) or not positive(self.owner_fence):
            raise ValueError("positive context_tokens and owner_fence required")
        _bounded_token(self.owner_id, "owner_id")
        _bounded_token(self.turn_id, "turn_id")


@dataclass(frozen=True)
class WiredBacking:
    """One physical anonymous wired weight backing (or a view aliasing its allocation_id)."""
    allocation_id: str
    whole_weight_pages: int
    page_size: int
    weights_sha256: str
    kind: str = "owned_anonymous"
    no_copy_weight: bool = True
    wire_succeeded: bool = True
    no_fork_inherit: bool = True

    def __post_init__(self):
        _bounded_token(self.allocation_id, "allocation_id")
        if type(self.whole_weight_pages) is not int or not _power_of_two_page_size(self.page_size):
            raise ValueError("integer whole_weight_pages and power-of-two page_size required")
        sha256_hex(self.weights_sha256)


@dataclass(frozen=True)
class WiredEpoch:
    """Immutable snapshot of a cooperative runner's active mlock + Metal bytesNoCopy generation."""
    scope: WiredScope
    generation: int
    wire_completed_ns: int
    backings: tuple[WiredBacking, ...]

    def __post_init__(self):
        if not isinstance(self.scope, WiredScope):
            raise ValueError("WiredScope required")
        if not positive(self.generation) or not positive(self.wire_completed_ns):
            raise ValueError("positive generation and wire_completed_ns required")
        if not isinstance(self.backings, tuple):
            raise ValueError("backings must be an immutable tuple")

    def verified_wired_bytes(self, expected_page_size: int) -> int:
        if not _power_of_two_page_size(expected_page_size) or not self.backings:
            raise Hold("ineligible model backing")
        unique: dict[str, WiredBacking] = {}
        for backing in self.backings:
            if (not isinstance(backing, WiredBacking)
                    or backing.kind != "owned_anonymous"
                    or backing.no_copy_weight is not True
                    or backing.wire_succeeded is not True
                    or backing.no_fork_inherit is not True
                    or not positive(backing.whole_weight_pages)
                    or backing.page_size != expected_page_size):
                raise Hold("ineligible model backing")
            existing = unique.setdefault(backing.allocation_id, backing)
            if existing != backing:
                raise Hold("inconsistent physical allocation identity")
        credit = sum(item.whole_weight_pages for item in unique.values()) * expected_page_size
        if not positive(credit):
            raise Hold("ineligible model backing")
        return credit


@dataclass(frozen=True)
class WiredResidentCreditProof:
    """Three-point wire epoch proof bracketing the platform capacity sample and durable commit."""
    scope: WiredScope
    before_sample_epoch: WiredEpoch
    after_sample_epoch: WiredEpoch
    pre_commit_epoch: WiredEpoch

    def verify_bound_epoch(self, expected_scope: WiredScope) -> WiredEpoch:
        if (not isinstance(expected_scope, WiredScope)
                or not isinstance(self.scope, WiredScope)
                or self.scope != expected_scope
                or not isinstance(self.before_sample_epoch, WiredEpoch)
                or self.before_sample_epoch != self.after_sample_epoch
                or self.before_sample_epoch != self.pre_commit_epoch
                or self.before_sample_epoch.scope != expected_scope):
            raise Hold("unbound, changed or lost wire epoch")
        return self.before_sample_epoch


@dataclass(frozen=True)
class DurableRetirement:
    """Durable host authority acknowledgement required before releasing wired pages."""
    phase: str
    scope: WiredScope
    generation: int


def permit_unwire(epoch: WiredEpoch, readback: DurableRetirement | None) -> None:
    """Fail closed unless durable readback proves exact RETIRED state for scope & generation."""
    if (not isinstance(epoch, WiredEpoch)
            or not isinstance(readback, DurableRetirement)
            or readback != DurableRetirement("RETIRED", epoch.scope, epoch.generation)):
        raise Hold("retirement has no exact durable readback")


def wired_epoch_commitment(epoch: WiredEpoch | dict) -> dict:
    """Commit to the full epoch without journaling every backing."""
    raw = asdict(epoch) if isinstance(epoch, WiredEpoch) else epoch
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return {
        "scope": raw["scope"],
        "generation": raw["generation"],
        "backing_count": len(raw["backings"]),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def verified_wired_epoch(load_state):
    """Return the journaled identity from a completed wired admission."""
    if load_state is None or load_state["phase"] != "VERIFIED":
        return None
    credit = load_state["completion"]["observation"]["credit"]
    if credit is None or credit["source"] != WIRED_CREDIT_SOURCE:
        return None
    proof = credit["wired_proof"]
    if proof.get("schema") == "ocean-wired-epoch-commitment.v1":
        return proof["epoch"]
    return wired_epoch_commitment(proof["pre_commit_epoch"])


@dataclass(frozen=True)
class ModelLoadCapacity:
    reading: CapacityReadback
    physical_available_ram_bytes: int
    reserve_ram_bytes: int
    pressure_level: int
    platform_sample: DarwinVmStatSample | None = None

    def usable(self, host, now, max_age, after, *, loading):
        r = self.reading
        base_ok = (isinstance(r, CapacityReadback)
            and type(self.pressure_level) is int
            and self.pressure_level in ({1, 2} if loading else {1})
            and positive(self.reserve_ram_bytes) and self.reserve_ram_bytes >= HARD_FLOOR
            and type(self.physical_available_ram_bytes) is int
            and self.physical_available_ram_bytes >= self.reserve_ram_bytes
            and r.pressure_ok is (self.pressure_level == 1)
            and r.available_ram_bytes == self.physical_available_ram_bytes - self.reserve_ram_bytes
            and replace(r, pressure_ok=True).usable(host, now, max_age, after)
            and (loading or r.pressure_ok is True))
        if not base_ok:
            return False
        if self.platform_sample is not None:
            ps = self.platform_sample
            return (isinstance(ps, DarwinVmStatSample)
                and ps.source == DARWIN_VM_STAT_SOURCE
                and ps.signature_verified is True
                and ps.sequence == r.sequence
                and ps.available_bytes == self.physical_available_ram_bytes)
        return True


@dataclass(frozen=True)
class ResidentCredit:
    """An independently proved lower bound, not estimated model allocation."""
    bytes: int
    workload_sha256: str
    process: ProcessIdentity
    capacity_sequence: int
    observed_at: float
    source: str = LEGACY_CREDIT_SOURCE
    wired_proof: WiredResidentCreditProof | None = None


def build_wired_resident_credit(
    *,
    scope: WiredScope,
    before_sample_epoch: WiredEpoch,
    after_sample_epoch: WiredEpoch,
    pre_commit_epoch: WiredEpoch,
    platform_sample: DarwinVmStatSample,
    observed_at: float,
) -> ResidentCredit:
    """Validate and construct a v2 physical ResidentCredit bound to one platform vm_stat sample."""
    if not isinstance(platform_sample, DarwinVmStatSample):
        raise Hold("unproved capacity sample within wire epoch")
    if not instant(observed_at):
        raise Hold("invalid credit observation timestamp")
    proof = WiredResidentCreditProof(scope, before_sample_epoch, after_sample_epoch, pre_commit_epoch)
    epoch = proof.verify_bound_epoch(scope)
    if (platform_sample.source != DARWIN_VM_STAT_SOURCE
            or platform_sample.signature_verified is not True
            or not epoch.wire_completed_ns <= platform_sample.sample_started_ns <= platform_sample.sample_completed_ns):
        raise Hold("unproved capacity sample within wire epoch")
    credit_bytes = epoch.verified_wired_bytes(platform_sample.page_size)
    return ResidentCredit(
        bytes=credit_bytes,
        workload_sha256=scope.workload_sha256,
        process=scope.listener,
        capacity_sequence=platform_sample.sequence,
        observed_at=observed_at,
        source=WIRED_CREDIT_SOURCE,
        wired_proof=proof,
    )


@dataclass(frozen=True)
class LoadObservation:
    workload_sha256: str
    policy_sha256: str
    process: ProcessIdentity
    observed_at: float
    available_ram_bytes: int
    pressure_level: int
    swap_bytes: int
    resident_bytes: int
    process_rss_bytes: int
    context_tokens: int | None
    normal_headroom_bytes: int
    capacity: ModelLoadCapacity | None = None
    credit: ResidentCredit | None = None

    def validate(self, spec, policy_sha256, now, max_age, *, loaded):
        sha256_hex(self.workload_sha256)
        sha256_hex(self.policy_sha256)
        if (self.workload_sha256 != spec.fingerprint() or self.policy_sha256 != policy_sha256
                or not isinstance(self.process, ProcessIdentity)
                or not instant(self.observed_at) or not 0 <= now - self.observed_at <= max_age
                or type(self.available_ram_bytes) is not int or self.available_ram_bytes < HARD_FLOOR
                or type(self.pressure_level) is not int or self.pressure_level not in {1, 2}
                or type(self.swap_bytes) is not int or self.swap_bytes < 0
                or not positive(self.process_rss_bytes) or not positive(self.normal_headroom_bytes)
                or self.normal_headroom_bytes < GIB + GIB // 2
                or type(self.resident_bytes) is not int or self.resident_bytes < 0
                or not isinstance(self.capacity, ModelLoadCapacity)):
            raise Hold("missing, stale or mismatched native model load observation")
        if loaded:
            if self.resident_bytes <= 0 or self.context_tokens != spec.context_tokens or self.pressure_level != 1:
                raise Hold("loaded model identity, context or normal pressure is unproved")
        elif self.resident_bytes != 0 or self.context_tokens is not None or self.available_ram_bytes < MIN_AVAILABLE:
            raise Hold("cold start requires exact absence and its original minimum")

    def credited_bytes(self, now, max_age):
        if self.credit is None:
            return 0
        c = self.credit
        if (not isinstance(c, ResidentCredit)
                or c.source not in {LEGACY_CREDIT_SOURCE, WIRED_CREDIT_SOURCE}
                or not positive(c.bytes) or c.bytes > min(self.resident_bytes, self.process_rss_bytes)
                or not positive(c.capacity_sequence)
                or c.workload_sha256 != self.workload_sha256 or c.process != self.process
                or self.capacity is None or c.capacity_sequence != self.capacity.reading.sequence
                or not instant(c.observed_at) or not 0 <= now - c.observed_at <= max_age
                or c.observed_at < self.capacity.reading.observed_at):
            raise Hold("model residency cannot be credited to this host capacity observation")
        if c.source == LEGACY_CREDIT_SOURCE:
            if c.wired_proof is not None:
                raise Hold("legacy resident credit must not attach a v2 wired proof")
            return c.bytes
        if not isinstance(c.wired_proof, WiredResidentCreditProof):
            raise Hold("unbound, changed or lost wire epoch")
        ps = self.capacity.platform_sample
        if (not isinstance(ps, DarwinVmStatSample)
                or ps.source != DARWIN_VM_STAT_SOURCE
                or ps.signature_verified is not True
                or ps.sequence != self.capacity.reading.sequence
                or self.available_ram_bytes != ps.available_bytes
                or self.capacity.physical_available_ram_bytes != ps.available_bytes):
            raise Hold("unproved capacity sample within wire epoch")
        scope = c.wired_proof.scope
        if (scope.workload_sha256 != self.workload_sha256
                or scope.listener != self.process
                or scope.context_tokens != self.context_tokens):
            raise Hold("unbound, changed or lost wire epoch")
        epoch = c.wired_proof.verify_bound_epoch(scope)
        if not epoch.wire_completed_ns <= ps.sample_started_ns <= ps.sample_completed_ns:
            raise Hold("unproved capacity sample within wire epoch")
        verified = epoch.verified_wired_bytes(ps.page_size)
        if verified != c.bytes:
            raise Hold("model residency cannot be credited to this host capacity observation")
        return c.bytes


class ModelLoadLifecycle:
    """Private arbiter component; callers already hold its process-fenced mutex."""

    def _load_profile(self, socket):
        if self._profile_gate is None:
            raise Hold("model load requires a pinned native v2 profile; fixture bypass is forbidden")
        try:
            return self._profile_gate.require_model_load(socket)
        except Hold:
            raise
        except Exception as exc:
            raise Hold("staged model profile changed or became unavailable") from exc

    def _load_observation(self, socket, spec, policy, observation, *, loaded, after=0):
        if not isinstance(observation, LoadObservation):
            raise Hold("trusted native load observation required")
        try:
            observation.validate(spec, policy, self._now(), self._max_age, loaded=loaded)
        except (TypeError, ValueError) as exc:
            raise Hold("native model load observation is malformed") from exc
        cap = observation.capacity
        if (not cap.usable(self.authority.host_id, self._now(), self._max_age,
                          max(after, self._last_metric), loading=not loaded)
                or cap.reading.available_cpu_units < socket.cpu_units
                or not loaded and cap.physical_available_ram_bytes < MIN_AVAILABLE):
            raise Hold("model load lacks fresh native CPU, disk, RAM reserve or pressure proof")
        self._last_metric = cap.reading.sequence
        return cap

    def _acquire_model_load(self, request_id, spec, policy_sha256, observation):
        self._require()
        sha256_hex(policy_sha256)
        if self._active is not None or self._select() != request_id:
            raise Hold("model load selection changed or another torch remains")
        request = self._requests[request_id]
        socket = self._sockets[request.socket_id]
        profile = self._load_profile(socket)
        if not request.interactive or socket.workload_sha256 != spec.fingerprint():
            raise Hold("model load requires its exact interactive model workload")
        cap = self._load_observation(socket, spec, policy_sha256, observation, loaded=False)
        self._generation += 1
        grant = Grant(self._authority_id, self._generation, request_id, request.socket_id, request.slot_id)
        state = {"schema": "ocean-model-load.v1", "phase": "PREPARED", "turn_id": None,
            "policy_sha256": policy_sha256, "profile_sha256": profile["profile_sha256"],
            "profile_readback": profile["readback"], "socket": asdict(socket),
            "profile_ram_bytes": socket.ram_bytes, "profile_cpu_units": socket.cpu_units,
            "workload_sha256": spec.fingerprint(), "spec": asdict(spec),
            "prepared_at": self._now(), "deadline": self._now() + MAX_SECONDS,
            "baseline": _json_roundtrip(asdict(observation)), "completion": None, "reason": None}
        self._install_grant(grant, request, cap.reading, load_state=state)
        return grant

    def _begin_model_load(self, grant, spec, turn_id, observation):
        active = self._current(grant)
        state = active.load_state
        if not state or state["phase"] not in {"PREPARED", "MAY_LOAD"}:
            raise Hold("model load is not pending")
        if state["phase"] == "MAY_LOAD":
            if state["turn_id"] != turn_id:
                raise Hold("another model load attempt owns this grant")
            return deepcopy(state)
        socket = self._sockets[grant.socket_id]
        if (self._load_profile(socket)["profile_sha256"] != state["profile_sha256"]
                or active.return_requested or self._now() > active.heartbeat_until
                or self._now() >= state["deadline"]):
            raise Hold("model load permission expired or changed")
        cap = self._load_observation(socket, spec, state["policy_sha256"], observation,
                                     loaded=False, after=active.metric_sequence)
        if asdict(observation.process) != state["baseline"]["process"]:
            raise Hold("model server process changed before load")
        previous = deepcopy(state)
        state.update(phase="MAY_LOAD", turn_id=turn_id, baseline=_json_roundtrip(asdict(observation)),
                     execution_id=active.execution_identity)
        active.metric_sequence = cap.reading.sequence
        try:
            self.authority.record(asdict(active))  # Durable before any client may POST.
        except BaseException:
            active.load_state = previous
            raise
        return deepcopy(state)

    def _complete_model_load(self, grant, spec, turn_id, observation):
        active = self._current(grant)
        state = active.load_state
        if not state or state["phase"] != "MAY_LOAD" or state["turn_id"] != turn_id:
            raise Hold("no matching possible-load intent")
        socket = self._sockets[grant.socket_id]
        if (active.return_requested or self._now() >= state["deadline"]
                or self._now() > active.heartbeat_until
                or self._load_profile(socket)["profile_sha256"] != state["profile_sha256"]):
            raise Hold("model load permission expired or changed")
        cap = self._load_observation(socket, spec, state["policy_sha256"], observation,
                                     loaded=True, after=active.metric_sequence)
        if (asdict(observation.process) != state["baseline"]["process"]
                or observation.swap_bytes - state["baseline"]["swap_bytes"] > MAX_SWAP_GROWTH
                or max(observation.resident_bytes, observation.process_rss_bytes) > socket.ram_bytes):
            raise Hold("model process, swap or full measured peak envelope changed")
        credit = observation.credited_bytes(self._now(), self._max_age)
        if observation.credit is not None and observation.credit.source == WIRED_CREDIT_SOURCE:
            proof = observation.credit.wired_proof
            if not isinstance(proof, WiredResidentCreditProof):
                raise Hold("wired resident credit scope does not match active load grant")
            scope = proof.scope
            if (scope.owner_id != self._authority_id
                    or scope.owner_fence != grant.generation
                    or scope.turn_id != turn_id
                    or scope.model_digest != spec.digest
                    or scope.context_tokens != spec.context_tokens
                    or scope.workload_sha256 != spec.fingerprint()):
                raise Hold("wired resident credit scope does not match active load grant")
        remaining = max(socket.ram_bytes - credit, observation.normal_headroom_bytes)
        if (cap.reading.available_ram_bytes < remaining
                or observation.available_ram_bytes < cap.reserve_ram_bytes + observation.normal_headroom_bytes):
            raise Hold("loaded model lacks normal reserve and provable remaining peak capacity")
        registered = state.get("registered_wired_epoch")
        if observation.credit is not None and observation.credit.source == WIRED_CREDIT_SOURCE:
            epoch = observation.credit.wired_proof.pre_commit_epoch
            commitment = wired_epoch_commitment(epoch)
            if registered is not None and registered != commitment:
                raise Hold("verified wired epoch differs from registered backing")
            recorded = asdict(replace(observation, credit=replace(observation.credit, wired_proof=None)))
            recorded["credit"]["wired_proof"] = {
                "schema": "ocean-wired-epoch-commitment.v1",
                "epoch": commitment,
            }
        else:
            if registered is not None:
                raise Hold("registered wired epoch requires a wired residency proof")
            recorded = asdict(observation)
        # A native read can block; repeat immutable approval and expiry checks.
        if self._load_profile(socket)["profile_sha256"] != state["profile_sha256"]:
            raise Hold("reviewed model profile changed during completion")
        observation.validate(spec, state["policy_sha256"], self._now(), self._max_age, loaded=True)
        if (active.return_requested or self._now() >= state["deadline"]
                or self._now() > active.heartbeat_until
                or not cap.usable(self.authority.host_id, self._now(), self._max_age,
                                  active.metric_sequence, loading=False)):
            raise Hold("native load proof expired before durable commit")
        previous = deepcopy(state)
        state.update(phase="VERIFIED", completion={"observation": _json_roundtrip(recorded),
            "credited_bytes": credit, "remaining_ram_bytes": remaining}, reason=None)
        active.metric_sequence = cap.reading.sequence
        try:
            self.authority.record(asdict(active))
        except BaseException:
            active.load_state = previous
            raise

    def _hold_model_load(self, grant, reason):
        active = self._current(grant)
        if active.load_state is None:
            raise Hold("not a staged model load")
        active.load_state.update(phase="RECOVERY_HOLD", reason=reason)
        active.return_requested = True
        self.authority.record(asdict(active))

    def _load_state(self, grant):
        active = self._current(grant)
        saved = self.authority.unresolved()
        if saved is None or saved.get("load_state") != active.load_state:
            raise Hold("staged load intent is not durably present")
        return deepcopy(active.load_state)


@dataclass(frozen=True)
class SafetensorsTensorInfo:
    """Validated tensor metadata parsed from a .safetensors header."""
    name: str
    dtype: str
    shape: tuple[int, ...]
    data_start: int
    data_end: int

    @property
    def nbytes(self) -> int:
        return self.data_end - self.data_start


def parse_safetensors_header(path: Path) -> tuple[int, dict[str, SafetensorsTensorInfo]]:
    """Parse and strictly validate a .safetensors binary header without mapping it."""
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise Hold("safetensors_file_unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size < 10:
        raise Hold("safetensors_file_invalid")
    with path.open("rb") as stream:
        raw_len = stream.read(8)
        if len(raw_len) != 8:
            raise Hold("safetensors_header_truncated")
        (header_len,) = struct.unpack("<Q", raw_len)
        if not 2 <= header_len <= _SAFETENSORS_MAX_HEADER_BYTES or 8 + header_len > info.st_size:
            raise Hold("safetensors_header_size_invalid")
        raw_header = stream.read(header_len)
        if len(raw_header) != header_len:
            raise Hold("safetensors_header_truncated")

    def _no_dups(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValueError("duplicate safetensors header key")
            out[k] = v
        return out

    try:
        header = json.loads(raw_header.decode("utf-8"), object_pairs_hook=_no_dups)
    except (UnicodeDecodeError, ValueError) as exc:
        raise Hold("safetensors_header_json_invalid") from exc
    if not isinstance(header, dict):
        raise Hold("safetensors_header_json_invalid")

    data_offset = 8 + header_len
    payload_size = info.st_size - data_offset
    tensors: dict[str, SafetensorsTensorInfo] = {}
    spans: list[tuple[int, int, str]] = []
    for name, meta in header.items():
        if name == "__metadata__":
            continue
        if not isinstance(name, str) or not name or not isinstance(meta, dict):
            raise Hold("safetensors_tensor_entry_invalid")
        dtype = meta.get("dtype")
        shape = meta.get("shape")
        offsets = meta.get("data_offsets")
        if (not isinstance(dtype, str) or not dtype
                or not isinstance(shape, list) or any(type(d) is not int or d < 0 for d in shape)
                or not isinstance(offsets, list) or len(offsets) != 2
                or any(type(v) is not int for v in offsets)):
            raise Hold("safetensors_tensor_entry_invalid")
        start, end = offsets
        if not 0 <= start <= end <= payload_size:
            raise Hold("safetensors_tensor_offsets_out_of_bounds")
        spans.append((start, end, name))
        tensors[name] = SafetensorsTensorInfo(
            name=name, dtype=dtype, shape=tuple(shape), data_start=start, data_end=end
        )
    spans.sort()
    for prev, curr in zip(spans, spans[1:]):
        if curr[0] < prev[1]:
            raise Hold("safetensors_tensor_offsets_overlap")
    if not tensors:
        raise Hold("safetensors_contains_no_tensors")
    return data_offset, tensors


class _MetalSize(C.Structure):
    _fields_ = [("width", C.c_ulong), ("height", C.c_ulong), ("depth", C.c_ulong)]


class _ObjcBlockDescriptor(C.Structure):
    _fields_ = [("reserved", C.c_ulong), ("size", C.c_ulong), ("signature", C.c_char_p)]


class _ObjcBlockLiteral(C.Structure):
    _fields_ = [
        ("isa", C.c_void_p),
        ("flags", C.c_int),
        ("reserved", C.c_int),
        ("invoke", C.c_void_p),
        ("descriptor", C.POINTER(_ObjcBlockDescriptor)),
    ]


_METAL_RUNTIME_LOCK = threading.Lock()
_METAL_RUNTIME = None
_METAL_DEALLOC_LOCK = threading.Lock()
_METAL_DEALLOC_EVENTS: dict[tuple[int, int], list[int]] = {}
_METAL_DEALLOC_CB_TYPE = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p, C.c_ulong)


@_METAL_DEALLOC_CB_TYPE
def _metal_dealloc_trampoline(_block, pointer, length):
    key = (int(pointer or 0), int(length))
    ts = time.monotonic_ns()
    with _METAL_DEALLOC_LOCK:
        _METAL_DEALLOC_EVENTS.setdefault(key, []).append(ts)


class _DarwinMetalRuntime:
    """Process-lifetime Objective-C/Metal/libc runtime bridge for arm64 LP64 macOS."""

    _SHADER = (
        "#include <metal_stdlib>\n"
        "using namespace metal;\n"
        "kernel void wired_verify_u32(\n"
        "    device const uint* w [[buffer(0)]],\n"
        "    device uint* out [[buffer(1)]],\n"
        "    device const ulong* meta [[buffer(2)]],\n"
        "    uint tid [[thread_position_in_grid]]) {\n"
        "    ulong count = meta[0];\n"
        "    ulong words_per_page = meta[1];\n"
        "    ulong pages = meta[2];\n"
        "    uint s = 0u;\n"
        "    for (ulong i = ulong(tid); i < count; i += 256ul) {\n"
        "        s += w[i];\n"
        "    }\n"
        "    uint pm = 0u;\n"
        "    for (ulong p = ulong(tid); p < pages; p += 256ul) {\n"
        "        uint first = w[p * words_per_page];\n"
        "        uint last = w[(p + 1ul) * words_per_page - 1ul];\n"
        "        pm ^= ((first ^ last) + (p * 2654435761u));\n"
        "    }\n"
        "    out[tid * 2u] = s;\n"
        "    out[tid * 2u + 1u] = pm;\n"
        "}\n"
    )

    def __init__(self):
        if (sys.platform != "darwin" or platform.machine() != "arm64"
                or sys.byteorder != "little"
                or C.sizeof(C.c_void_p) != 8 or C.sizeof(C.c_ulong) != 8):
            raise Hold("darwin_arm64_metal_runtime_required")
        if C.sizeof(_ObjcBlockLiteral) != 32 or C.sizeof(_MetalSize) != 24:
            raise Hold("native_struct_layout_invalid")
        self.objc = C.CDLL("/usr/lib/libobjc.A.dylib")
        self.libc = C.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        self.foundation = C.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
        self.metal = C.CDLL("/System/Library/Frameworks/Metal.framework/Metal")

        self.objc.sel_registerName.argtypes = [C.c_char_p]
        self.objc.sel_registerName.restype = C.c_void_p
        self.objc.objc_getClass.argtypes = [C.c_char_p]
        self.objc.objc_getClass.restype = C.c_void_p
        self.objc.objc_autoreleasePoolPush.argtypes = []
        self.objc.objc_autoreleasePoolPush.restype = C.c_void_p
        self.objc.objc_autoreleasePoolPop.argtypes = [C.c_void_p]
        self.objc.objc_autoreleasePoolPop.restype = None
        self.metal.MTLCreateSystemDefaultDevice.argtypes = []
        self.metal.MTLCreateSystemDefaultDevice.restype = C.c_void_p

        self.libc.mmap.argtypes = [C.c_void_p, C.c_size_t, C.c_int, C.c_int, C.c_int, C.c_longlong]
        self.libc.mmap.restype = C.c_void_p
        for name in ("mlock", "munlock", "munmap"):
            fn = getattr(self.libc, name)
            fn.argtypes = [C.c_void_p, C.c_size_t]
            fn.restype = C.c_int
        self.libc.minherit.argtypes = [C.c_void_p, C.c_size_t, C.c_int]
        self.libc.minherit.restype = C.c_int

        self.descriptor = _ObjcBlockDescriptor(0, C.sizeof(_ObjcBlockLiteral), b"v@?^vQ")
        global_class = C.addressof((C.c_byte * 1).in_dll(self.libc, "_NSConcreteGlobalBlock"))
        self.block_literal = _ObjcBlockLiteral(
            global_class,
            (1 << 28) | (1 << 30),
            0,
            C.cast(_metal_dealloc_trampoline, C.c_void_p),
            C.pointer(self.descriptor),
        )
        self.page_size = os.sysconf("SC_PAGE_SIZE")
        if not _power_of_two_page_size(self.page_size):
            raise Hold("darwin_page_size_invalid")

    def send(self, restype, receiver, selector_name: str, types=(), values=()):
        if not receiver:
            raise Hold("nil_objc_receiver:" + selector_name)
        selector = self.objc.sel_registerName(selector_name.encode("ascii"))
        fn = C.CFUNCTYPE(restype, C.c_void_p, C.c_void_p, *types)(("objc_msgSend", self.objc))
        return fn(receiver, selector, *values)

    def nsstring(self, text: str):
        return self.send(
            C.c_void_p,
            self.objc.objc_getClass(b"NSString"),
            "stringWithUTF8String:",
            (C.c_char_p,),
            (text.encode("utf-8"),),
        )


def _get_metal_runtime() -> _DarwinMetalRuntime:
    global _METAL_RUNTIME
    with _METAL_RUNTIME_LOCK:
        if _METAL_RUNTIME is None:
            _METAL_RUNTIME = _DarwinMetalRuntime()
        return _METAL_RUNTIME


@dataclass
class WiredWeightRegion:
    """One anonymous private mlock'd + Metal bytesNoCopy weight buffer."""
    tensor_name: str
    allocation_id: str
    address: int
    raw_bytes: int
    mapped_bytes: int
    whole_weight_pages: int
    page_size: int
    weights_sha256: str
    mtl_buffer: int
    gpu_verified: bool = False


@dataclass(frozen=True, eq=False)
class WiredWeightLease:
    """Identity-scoped access to a runner-owned, immutable weight mapping."""

    tensor_name: str
    address: int
    raw_bytes: int


def _serialized_wired_runner(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if self._pid != os.getpid():
            raise Hold("foreign_wired_runner_process")
        with self._wire_lock:
            return method(self, *args, **kwargs)
    return wrapped


@contextmanager
def _metal_autorelease_pool(runtime):
    pool = runtime.objc.objc_autoreleasePoolPush()
    try:
        yield
    finally:
        if pool:
            runtime.objc.objc_autoreleasePoolPop(pool)


def _with_metal_autorelease_pool(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with _metal_autorelease_pool(self._rt):
            return method(self, *args, **kwargs)
    return wrapped


class DarwinWiredWeightRunner:
    """Cooperative Apple Silicon weight runner backing ResidentCredit v2.

    Loads .safetensors model weights into owned, private, non-fork-inheritable,
    mlock-wired anonymous pages, binds them zero-copy to Metal shared buffers
    with explicit deallocator callbacks, verifies GPU read participation across
    every weight page, exports immutable WiredEpoch snapshots, and refuses to
    unwire until permit_unwire confirms a durable RETIRED readback.
    """

    def __init__(self, *, max_total_bytes: int = 512 * 1024 * 1024):
        if not positive(max_total_bytes):
            raise ValueError("positive max_total_bytes required")
        self._pid = os.getpid()
        self._wire_lock = threading.Lock()
        self._weight_leases: set[WiredWeightLease] = set()
        self._rt = _get_metal_runtime()
        with _metal_autorelease_pool(self._rt):
            self._max_total_bytes = max_total_bytes
            self._total_mapped_bytes = 0
            self._device = self._rt.metal.MTLCreateSystemDefaultDevice()
            if not self._device or not self._rt.send(C.c_bool, self._device, "hasUnifiedMemory"):
                raise Hold("unified_metal_device_required")
            self._max_buffer_len = self._rt.send(C.c_ulong, self._device, "maxBufferLength")
            self._refs: list[int] = []
            self._pipeline: int | None = None
            self._queue: int | None = None
            self._regions: dict[str, WiredWeightRegion] = {}
            self._views: dict[str, str] = {}
            self._generation = 0
            self._wire_completed_ns: int | None = None
            self._bound_scope: WiredScope | None = None
            self._active_epoch: WiredEpoch | None = None
            self._retired = False
            self._gpu_commit_uncertain = False
            self._teardown_started = False
            self._metal_deallocated_ns: dict[str, int] = {}
            self._munlocked_regions: set[str] = set()
            self._unmapped_regions: set[str] = set()
            self._munlock_started_ns: int | None = None
            self._wire_ended_ns: int | None = None

    def _ensure_compute_pipeline(self):
        if self._pipeline is not None and self._queue is not None:
            return
        err = C.c_void_p()
        library = self._rt.send(
            C.c_void_p, self._device, "newLibraryWithSource:options:error:",
            (C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p)),
            (self._rt.nsstring(self._rt._SHADER), None, C.byref(err)),
        )
        if not library or err.value:
            raise Hold("metal_shader_compilation_failed")
        self._refs.append(library)
        fn = self._rt.send(
            C.c_void_p, library, "newFunctionWithName:",
            (C.c_void_p,), (self._rt.nsstring("wired_verify_u32"),),
        )
        if not fn:
            raise Hold("metal_shader_function_missing")
        self._refs.append(fn)
        pipeline = self._rt.send(
            C.c_void_p, self._device, "newComputePipelineStateWithFunction:error:",
            (C.c_void_p, C.POINTER(C.c_void_p)), (fn, C.byref(err)),
        )
        if not pipeline or err.value:
            raise Hold("metal_pipeline_creation_failed")
        self._refs.append(pipeline)
        max_threads = self._rt.send(C.c_ulong, pipeline, "maxTotalThreadsPerThreadgroup")
        if max_threads < 256:
            raise Hold("metal_threadgroup_capacity_insufficient")
        queue = self._rt.send(C.c_void_p, self._device, "newCommandQueue")
        if not queue:
            raise Hold("metal_command_queue_creation_failed")
        self._refs.append(queue)
        self._pipeline = pipeline
        self._queue = queue

    @property
    def page_size(self) -> int:
        return self._rt.page_size

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def retired(self) -> bool:
        return self._retired

    @property
    def regions(self) -> dict[str, WiredWeightRegion]:
        return dict(self._regions)

    @_serialized_wired_runner
    def borrow_weight(self, tensor_name: str) -> WiredWeightLease:
        """Hold a backing until the inference runtime releases its last reference.

        The borrower must synchronize GPU work and dispose of every native
        reference before returning the lease. Losing the lease holds teardown.
        """
        if self._retired or self._teardown_started or self._gpu_commit_uncertain:
            raise Hold("wired_runner_unavailable_for_borrow")
        region = self._regions.get(tensor_name) if isinstance(tensor_name, str) else None
        if region is None:
            raise Hold("unknown_wired_weight")
        lease = WiredWeightLease(region.tensor_name, region.address, region.raw_bytes)
        self._weight_leases.add(lease)
        return lease

    @_serialized_wired_runner
    def release_weight(self, lease: WiredWeightLease) -> None:
        """Acknowledge that the borrower no longer retains the backing."""
        if not isinstance(lease, WiredWeightLease) or lease not in self._weight_leases:
            raise Hold("unowned_or_released_wired_weight_lease")
        self._weight_leases.remove(lease)

    @_serialized_wired_runner
    @_with_metal_autorelease_pool
    def load_safetensors(
        self,
        path: Path,
        *,
        tensor_names: tuple[str, ...] | None = None,
        skip_subpage_tensors: bool = False,
    ) -> list[WiredWeightRegion]:
        """Load selected .safetensors tensors into mlock'd anonymous Metal bytesNoCopy buffers."""
        if self._retired:
            raise Hold("runner_already_retired")
        if self._teardown_started:
            raise Hold("runner_teardown_in_progress")
        if self._active_epoch is not None:
            raise Hold("cannot_mutate_weights_after_epoch_exported")
        path = Path(path)
        data_offset, catalog = parse_safetensors_header(path)
        selected = tuple(catalog) if tensor_names is None else tuple(tensor_names)
        if not selected:
            raise Hold("no_safetensors_tensors_selected")
        page = self._rt.page_size
        loaded: list[WiredWeightRegion] = []
        with path.open("rb") as stream:
            for name in selected:
                if name not in catalog:
                    raise Hold("safetensors_tensor_not_found:" + name)
                if name in self._regions:
                    raise Hold("duplicate_wired_tensor_name:" + name)
                info = catalog[name]
                nbytes = info.nbytes
                whole_pages = nbytes // page
                if whole_pages <= 0:
                    if skip_subpage_tensors:
                        continue
                    raise Hold("tensor_smaller_than_one_whole_page:" + name)
                mapped_len = ((nbytes + page - 1) // page) * page
                if (mapped_len > self._max_buffer_len
                        or self._total_mapped_bytes + mapped_len > self._max_total_bytes):
                    raise Hold("wired_weight_budget_exceeded")
                addr = self._rt.libc.mmap(None, mapped_len, 3, 2 | 0x1000, -1, 0)
                if addr in (None, C.c_void_p(-1).value) or addr % page != 0:
                    raise Hold("anonymous_mapping_failed")
                locked = False
                buf_handle = None
                try:
                    # VM_INHERIT_NONE = 2 prevents fork children inheriting wired model pages.
                    if self._rt.libc.minherit(addr, mapped_len, 2) != 0:
                        raise Hold("mapping_inheritance_unconfirmed")
                    stream.seek(data_offset + info.data_start)
                    target = memoryview((C.c_ubyte * nbytes).from_address(addr)).cast("B")
                    offset = 0
                    while offset < nbytes:
                        received = stream.readinto(target[offset:offset + 8 * 1024 * 1024])
                        if not received:
                            raise Hold("safetensors_tensor_read_truncated")
                        offset += received
                    if mapped_len > nbytes:
                        C.memset(addr + nbytes, 0, mapped_len - nbytes)
                    digest = hashlib.sha256(target).hexdigest()
                    if self._rt.libc.mlock(addr, mapped_len) != 0:
                        raise Hold("mlock_failed_errno:" + str(C.get_errno()))
                    locked = True
                    with _METAL_DEALLOC_LOCK:
                        _METAL_DEALLOC_EVENTS.pop((int(addr), int(mapped_len)), None)
                    buf_handle = self._rt.send(
                        C.c_void_p, self._device,
                        "newBufferWithBytesNoCopy:length:options:deallocator:",
                        (C.c_void_p, C.c_ulong, C.c_ulong, C.c_void_p),
                        (addr, mapped_len, 0, C.addressof(self._rt.block_literal)),
                    )
                    if not buf_handle:
                        raise Hold("no_copy_buffer_unconfirmed")
                    if (self._rt.send(C.c_void_p, buf_handle, "contents") != addr
                            or self._rt.send(C.c_ulong, buf_handle, "length") != mapped_len
                            or self._rt.send(C.c_ulong, buf_handle, "storageMode") != 0):
                        self._rt.send(None, buf_handle, "release")
                        raise Hold("no_copy_identity_or_mode")
                except BaseException:
                    if buf_handle is None:
                        if locked:
                            self._rt.libc.munlock(addr, mapped_len)
                        self._rt.libc.munmap(addr, mapped_len)
                    raise
                self._refs.append(buf_handle)
                self._total_mapped_bytes += mapped_len
                alloc_id = f"wired-{uuid.uuid4().hex[:16]}-{addr:x}"
                region = WiredWeightRegion(
                    tensor_name=name,
                    allocation_id=alloc_id,
                    address=int(addr),
                    raw_bytes=nbytes,
                    mapped_bytes=mapped_len,
                    whole_weight_pages=whole_pages,
                    page_size=page,
                    weights_sha256=digest,
                    mtl_buffer=int(buf_handle),
                    gpu_verified=False,
                )
                self._regions[name] = region
                loaded.append(region)
        if not loaded:
            raise Hold("no_whole_page_tensors_loaded")
        self._generation += 1
        self._wire_completed_ns = time.monotonic_ns()
        return loaded

    @_serialized_wired_runner
    def register_tensor_view(self, view_name: str, base_tensor_name: str) -> None:
        """Register an aliased view sharing the exact physical allocation_id of base_tensor_name."""
        if self._retired or self._active_epoch is not None:
            raise Hold("cannot_register_view_after_epoch_exported")
        if self._teardown_started:
            raise Hold("runner_teardown_in_progress")
        if base_tensor_name not in self._regions:
            raise Hold("base_tensor_not_loaded")
        if view_name in self._regions or view_name in self._views:
            raise Hold("duplicate_view_name")
        self._views[view_name] = base_tensor_name

    @_serialized_wired_runner
    @_with_metal_autorelease_pool
    def verify_gpu_weight_read(self, tensor_name: str, *, timeout: float = 15.0) -> dict:
        """Execute a Metal compute pass reading every 32-bit word and page boundary of a wired tensor."""
        if self._retired:
            raise Hold("runner_already_retired")
        if self._teardown_started:
            raise Hold("runner_teardown_in_progress")
        if tensor_name not in self._regions:
            raise Hold("wired_tensor_not_found")
        region = self._regions[tensor_name]
        if region.raw_bytes < 4 or region.raw_bytes % 4 != 0:
            raise Hold("tensor_bytes_must_be_u32_aligned_for_gpu_verification")
        self._ensure_compute_pipeline()
        word_count = region.raw_bytes // 4
        words_per_page = region.page_size // 4
        whole_pages = region.whole_weight_pages

        # Compute exact CPU reference from the wired anonymous buffer.
        # The mapping stays wired throughout verification. Read it in place so
        # large tensors do not require two more full-size host allocations.
        raw_view = memoryview((C.c_ubyte * region.raw_bytes).from_address(region.address)).cast("B")
        words = raw_view.cast("I")
        expected_sum = sum(words) & 0xFFFFFFFF
        expected_page_mix = 0
        for p in range(whole_pages):
            first = words[p * words_per_page]
            last = words[(p + 1) * words_per_page - 1]
            expected_page_mix ^= ((first ^ last) + (p * 2654435761)) & 0xFFFFFFFF

        out_buf = self._rt.send(C.c_void_p, self._device, "newBufferWithLength:options:",
                                (C.c_ulong, C.c_ulong), (256 * 2 * 4, 0))
        meta_buf = self._rt.send(C.c_void_p, self._device, "newBufferWithLength:options:",
                                 (C.c_ulong, C.c_ulong), (24, 0))
        if not out_buf or not meta_buf:
            if out_buf:
                self._rt.send(None, out_buf, "release")
            if meta_buf:
                self._rt.send(None, meta_buf, "release")
            raise Hold("metal_scratch_buffer_allocation_failed")
        meta_arr = (C.c_uint64 * 3).from_address(self._rt.send(C.c_void_p, meta_buf, "contents"))
        meta_arr[0] = word_count
        meta_arr[1] = words_per_page
        meta_arr[2] = whole_pages
        try:
            cmd = self._rt.send(C.c_void_p, self._queue, "commandBuffer")
            enc = self._rt.send(C.c_void_p, cmd, "computeCommandEncoder")
            if not cmd or not enc:
                raise Hold("metal_command_or_encoder_missing")
            self._rt.send(None, enc, "setComputePipelineState:", (C.c_void_p,), (self._pipeline,))
            self._rt.send(None, enc, "setBuffer:offset:atIndex:", (C.c_void_p, C.c_ulong, C.c_ulong),
                          (region.mtl_buffer, 0, 0))
            self._rt.send(None, enc, "setBuffer:offset:atIndex:", (C.c_void_p, C.c_ulong, C.c_ulong),
                          (out_buf, 0, 1))
            self._rt.send(None, enc, "setBuffer:offset:atIndex:", (C.c_void_p, C.c_ulong, C.c_ulong),
                          (meta_buf, 0, 2))
            self._rt.send(None, enc, "dispatchThreadgroups:threadsPerThreadgroup:",
                          (_MetalSize, _MetalSize), (_MetalSize(1, 1, 1), _MetalSize(256, 1, 1)))
            self._rt.send(None, enc, "endEncoding")
            commit_ns = time.monotonic_ns()
            self._gpu_commit_uncertain = True
            self._rt.send(None, cmd, "commit")
            deadline = time.monotonic() + timeout
            status = self._rt.send(C.c_ulong, cmd, "status")
            while status < 4 and time.monotonic() < deadline:
                time.sleep(0.005)
                status = self._rt.send(C.c_ulong, cmd, "status")
            if status not in (4, 5):
                raise Hold("gpu_terminal_unconfirmed")
            self._rt.send(None, cmd, "waitUntilCompleted")
            self._gpu_commit_uncertain = False
            terminal_ns = time.monotonic_ns()
            if status != 4:
                raise Hold("gpu_command_failed")
            out_arr = (C.c_uint32 * (256 * 2)).from_address(self._rt.send(C.c_void_p, out_buf, "contents"))
            gpu_sum = sum(out_arr[i * 2] for i in range(256)) & 0xFFFFFFFF
            gpu_page_mix = 0
            for i in range(256):
                gpu_page_mix ^= int(out_arr[i * 2 + 1])
            after_sha = hashlib.sha256(raw_view).hexdigest()
            if gpu_sum != expected_sum or gpu_page_mix != expected_page_mix or after_sha != region.weights_sha256:
                raise Hold("gpu_weight_verification_mismatch")
            region.gpu_verified = True
            return {
                "tensor_name": tensor_name,
                "allocation_id": region.allocation_id,
                "word_count": word_count,
                "whole_weight_pages": whole_pages,
                "gpu_status": int(status),
                "gpu_sum_u32": gpu_sum,
                "gpu_page_mix_u32": gpu_page_mix,
                "weights_sha256": after_sha,
                "gpu_commit_ns": commit_ns,
                "gpu_terminal_ns": terminal_ns,
            }
        finally:
            if not self._gpu_commit_uncertain:
                self._rt.send(None, meta_buf, "release")
                self._rt.send(None, out_buf, "release")

    @_serialized_wired_runner
    def export_epoch(self, scope: WiredScope) -> WiredEpoch:
        """Export the immutable WiredEpoch bound to scope once all tensors are GPU-verified."""
        if self._retired:
            raise Hold("runner_already_retired")
        if self._teardown_started:
            raise Hold("runner_teardown_in_progress")
        if not isinstance(scope, WiredScope):
            raise Hold("wired_scope_required")
        if not self._regions or self._wire_completed_ns is None or self._generation <= 0:
            raise Hold("no_wired_weights_loaded")
        if not all(region.gpu_verified for region in self._regions.values()):
            raise Hold("all_wired_tensors_must_pass_gpu_verification")
        if self._bound_scope is not None and self._bound_scope != scope:
            raise Hold("wired_runner_scope_mismatch")
        self._bound_scope = scope
        backings: list[WiredBacking] = []
        for region in self._regions.values():
            backings.append(WiredBacking(
                allocation_id=region.allocation_id,
                whole_weight_pages=region.whole_weight_pages,
                page_size=region.page_size,
                weights_sha256=region.weights_sha256,
            ))
        for _view_name, base_name in self._views.items():
            base = self._regions[base_name]
            backings.append(WiredBacking(
                allocation_id=base.allocation_id,
                whole_weight_pages=base.whole_weight_pages,
                page_size=base.page_size,
                weights_sha256=base.weights_sha256,
            ))
        epoch = WiredEpoch(
            scope=scope,
            generation=self._generation,
            wire_completed_ns=self._wire_completed_ns,
            backings=tuple(backings),
        )
        self._active_epoch = epoch
        return epoch

    def _release_native_resources(self) -> dict:
        if self._gpu_commit_uncertain:
            raise Hold("gpu_terminal_unconfirmed_no_manual_unmap")
        self._teardown_started = True
        if self._weight_leases:
            raise Hold("wired_weight_leases_still_active")
        with _metal_autorelease_pool(self._rt):
            while self._refs:
                self._rt.send(None, self._refs[-1], "release")
                self._refs.pop()
            self._pipeline = None
            self._queue = None
            if self._device:
                self._rt.send(None, self._device, "release")
                self._device = None

        for name, region in self._regions.items():
            if name in self._metal_deallocated_ns:
                continue
            with _METAL_DEALLOC_LOCK:
                key = (region.address, region.mapped_bytes)
                events = _METAL_DEALLOC_EVENTS.get(key, [])
                if len(events) == 1:
                    _METAL_DEALLOC_EVENTS.pop(key, None)
            if len(events) != 1:
                raise Hold("metal_deallocator_unconfirmed:" + name)
            self._metal_deallocated_ns[name] = events[0]

        if self._munlock_started_ns is None:
            self._munlock_started_ns = time.monotonic_ns()
        for name, region in self._regions.items():
            if name in self._munlocked_regions or name in self._unmapped_regions:
                continue
            if self._rt.libc.munlock(region.address, region.mapped_bytes) != 0:
                raise Hold("munlock_failed:" + name)
            self._munlocked_regions.add(name)
        if self._wire_ended_ns is None:
            self._wire_ended_ns = time.monotonic_ns()
        for name, region in self._regions.items():
            if name in self._unmapped_regions:
                continue
            if self._rt.libc.munmap(region.address, region.mapped_bytes) != 0:
                raise Hold("munmap_failed:" + name)
            self._unmapped_regions.add(name)

        self._retired = True
        self._total_mapped_bytes = 0
        dealloc_times = list(self._metal_deallocated_ns.values())
        return {
            "regions_released": len(self._regions),
            "wire_completed_ns": self._wire_completed_ns,
            "metal_deallocated_min_ns": min(dealloc_times) if dealloc_times else None,
            "metal_deallocated_max_ns": max(dealloc_times) if dealloc_times else None,
            "munlock_started_ns": self._munlock_started_ns,
            "wire_ended_ns": self._wire_ended_ns,
            "cleanup_verified": True,
        }

    @_serialized_wired_runner
    def retire_and_unwire(self, readback: DurableRetirement | None) -> dict:
        """Unwire and free Metal/VM mappings ONLY after durable RETIRED readback."""
        if self._retired:
            raise Hold("runner_already_retired")
        if self._active_epoch is None:
            raise Hold("no_active_wired_epoch_to_retire")
        permit_unwire(self._active_epoch, readback)
        audit = self._release_native_resources()
        if (audit["metal_deallocated_min_ns"] is None
                or not (self._wire_completed_ns < audit["metal_deallocated_min_ns"]
                        <= audit["metal_deallocated_max_ns"]
                        < audit["munlock_started_ns"]
                        <= audit["wire_ended_ns"])):
            raise Hold("wire_epoch_cleanup_order_unconfirmed")
        return audit

    @_serialized_wired_runner
    def emergency_teardown(self) -> dict:
        """Clean up aborted pre-credit setup; never bypass a published epoch."""
        if self._retired:
            return {"regions_released": 0, "cleanup_verified": True, "already_retired": True}
        if self._active_epoch is not None:
            raise Hold("emergency_teardown_after_epoch_export_requires_durable_retirement")
        audit = self._release_native_resources()
        audit["emergency_teardown"] = True
        return audit
