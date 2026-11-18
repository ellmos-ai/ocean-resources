# SPDX-License-Identifier: MIT
"""Model-only staged admission. No threshold here authorizes a compute grant.

The native port is trusted host code, never a client-supplied measurement. A
residency credit must describe attributable, private, non-reclaimable host RAM
in the same capacity observation. API size, total RSS and GPU/Metal allocation
alone do not implement that contract and must leave ``credit`` absent.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, replace

from .contracts import CapacityReadback, Grant, Hold, instant, positive, sha256_hex

GIB = 1024**3
MIN_AVAILABLE = 6 * GIB
HARD_FLOOR = 2 * GIB
MAX_SECONDS = 300
MAX_PRESSURE = 2
MAX_SWAP_GROWTH = 8 * GIB


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
class ModelLoadCapacity:
    reading: CapacityReadback
    physical_available_ram_bytes: int
    reserve_ram_bytes: int
    pressure_level: int

    def usable(self, host, now, max_age, after, *, loading):
        r = self.reading
        return (isinstance(r, CapacityReadback)
            and type(self.pressure_level) is int
            and self.pressure_level in ({1, 2} if loading else {1})
            and positive(self.reserve_ram_bytes) and self.reserve_ram_bytes >= HARD_FLOOR
            and type(self.physical_available_ram_bytes) is int
            and self.physical_available_ram_bytes >= self.reserve_ram_bytes
            and r.pressure_ok is (self.pressure_level == 1)
            and r.available_ram_bytes == self.physical_available_ram_bytes - self.reserve_ram_bytes
            and replace(r, pressure_ok=True).usable(host, now, max_age, after)
            and (loading or r.pressure_ok is True))


@dataclass(frozen=True)
class ResidentCredit:
    """An independently proved lower bound, not estimated model allocation."""
    bytes: int
    workload_sha256: str
    process: ProcessIdentity
    capacity_sequence: int
    observed_at: float
    source: str = "private-nonreclaimable-model-pages.v1"


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
        if (not isinstance(c, ResidentCredit) or c.source != "private-nonreclaimable-model-pages.v1"
                or not positive(c.bytes) or c.bytes > min(self.resident_bytes, self.process_rss_bytes)
                or not positive(c.capacity_sequence)
                or c.workload_sha256 != self.workload_sha256 or c.process != self.process
                or self.capacity is None or c.capacity_sequence != self.capacity.reading.sequence
                or not instant(c.observed_at) or not 0 <= now - c.observed_at <= max_age
                or c.observed_at < self.capacity.reading.observed_at):
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
            "profile_ram_bytes": socket.ram_bytes, "profile_cpu_units": socket.cpu_units,
            "workload_sha256": spec.fingerprint(), "spec": asdict(spec),
            "prepared_at": self._now(), "deadline": self._now() + MAX_SECONDS,
            "baseline": asdict(observation), "completion": None, "reason": None}
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
        state.update(phase="MAY_LOAD", turn_id=turn_id, baseline=asdict(observation))
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
        remaining = max(socket.ram_bytes - credit, observation.normal_headroom_bytes)
        if (cap.reading.available_ram_bytes < remaining
                or observation.available_ram_bytes < cap.reserve_ram_bytes + observation.normal_headroom_bytes):
            raise Hold("loaded model lacks normal reserve and provable remaining peak capacity")
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
        state.update(phase="VERIFIED", completion={"observation": asdict(observation),
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
