# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""
ocean_resources.ram_distributor -- RAM-Distributor und Modell-Runtime-Scheduler
================================================================================

Setzt das präzisierte Konzept vom 24.10.2026 (#1949, #2027, #2028, #2152, #2334,
#2419, #2441) um:
1. Anschlusskriterium mit Fähigkeitsmeldung (`ConsumerCapability`)
2. Prioritätsklassen nach Nutzernähe mit Vererbung (`UserProximity`)
3. Tray-Schalter als Tendenz (`TendencyMode`: `compute`, `balanced`, `ollama`)
4. Deterministische Fallbacks für Verlierer (`FallbackDecision`)
5. Hysterese-Schutz gegen Watchdog-Fehlalarme beim 27B-Kaltstart (#2419, Rückbau
   der `free_emergency_pages=0`-Zwischenlösung)
6. Abkühlzulassung vor jedem Lauf eines lokalen Großverbrauchers (`RunCooldownGate`):
   Swap-Schreibrate und Speicherdruck, Wärmewarnung, Mindestpause
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum
import math
from typing import Any, Optional

from .contracts import Hold, identity, instant, positive
from .host import StressSample, sample_stress  # noqa: F401 -- consumers load only this module


class UserProximity(IntEnum):
    """Prioritätsklassen nach Nutzernähe (kleinerer Wert = näher am Nutzer)."""

    DIRECT_INTERACTIVE = 1      # Direkter Nutzerkontakt (GUI-Chat, Telegram, Konsole)
    DELEGATED_FOREGROUND = 2    # Vom Nutzerauftrag losgeschickter Subagent / Kettenschritt
    SCHEDULED_ROUTINE = 3       # Termin-/Routine-Job, verzahnter Review-Lauf
    AUTONOMOUS_BACKGROUND = 4   # Autonomer Dauerläufer (z. B. Buddha Always-On)


VALID_TENDENCIES = ("compute", "balanced", "ollama")
VALID_FALLBACK_ACTIONS = ("cloud_router", "smaller_local_model", "defer_with_checkpoint", "reject_hold")
VALID_PRESSURE_STATES = ("normal", "warning", "elevated", "critical", "unknown")


@dataclass(frozen=True)
class ConsumerCapability:
    """Anschlusskriterium & Fähigkeitsmeldung eines RAM-/Modell-Verbrauchers."""

    consumer_id: str
    kind: str  # "model" | "compute" | "media"
    weights_ram_bytes: int
    kv_reserve_bytes: int = 0
    overhead_bytes: int = 0
    can_yield_at_turn_boundary: bool = True
    can_checkpoint: bool = True
    fallback_chain: tuple[str, ...] = ("cloud_router", "defer_with_checkpoint")

    def __post_init__(self) -> None:
        identity(self.consumer_id)
        if self.kind not in {"model", "compute", "media"}:
            raise ValueError(f"Unbekannte Verbraucherart: {self.kind!r}")
        if not positive(self.weights_ram_bytes):
            raise ValueError("weights_ram_bytes muss positiv sein")
        if (
            type(self.kv_reserve_bytes) is not int
            or type(self.overhead_bytes) is not int
            or self.kv_reserve_bytes < 0
            or self.overhead_bytes < 0
        ):
            raise ValueError("Reserve-Bytes müssen nichtnegative Ganzzahlen sein")
        if type(self.can_yield_at_turn_boundary) is not bool or type(self.can_checkpoint) is not bool:
            raise ValueError("can_yield_at_turn_boundary und can_checkpoint müssen bool sein")
        if not isinstance(self.fallback_chain, tuple):
            raise ValueError("fallback_chain muss ein Tupel gültiger Fallback-Aktionen sein")
        for fb in self.fallback_chain:
            if fb not in VALID_FALLBACK_ACTIONS:
                raise ValueError(f"Unbekannte Fallback-Aktion: {fb!r}")

    @property
    def total_required_bytes(self) -> int:
        return self.weights_ram_bytes + self.kv_reserve_bytes + self.overhead_bytes


@dataclass(frozen=True)
class DistributorRequest:
    """Anfrage an den gemeinsamen RAM-Distributor inkl. vererbter Nutzernähe."""

    request_id: str
    consumer_id: str
    proximity: UserProximity = UserProximity.AUTONOMOUS_BACKGROUND
    inherited_from_proximity: Optional[UserProximity] = None
    parent_request_id: Optional[str] = None
    submitted_at: float = 0.0

    def __post_init__(self) -> None:
        identity(self.request_id)
        identity(self.consumer_id)
        if type(self.proximity) is bool or not isinstance(self.proximity, UserProximity):
            raise ValueError("proximity muss ein gültiger UserProximity-Wert sein")
        if self.inherited_from_proximity is not None and (
            type(self.inherited_from_proximity) is bool
            or not isinstance(self.inherited_from_proximity, UserProximity)
        ):
            raise ValueError("inherited_from_proximity muss UserProximity oder None sein")
        if self.parent_request_id is not None:
            identity(self.parent_request_id)
        if not instant(self.submitted_at):
            raise ValueError("submitted_at muss endlich und >= 0 sein")

    @property
    def effective_proximity(self) -> UserProximity:
        """Kind-Aufträge erben die höhere Nutzernähe ihres Auslösers (max. Stufe 2)."""
        if self.inherited_from_proximity is None:
            return self.proximity
        inherited = (
            UserProximity.DELEGATED_FOREGROUND
            if self.inherited_from_proximity == UserProximity.DIRECT_INTERACTIVE
            else self.inherited_from_proximity
        )
        return UserProximity(min(int(self.proximity), int(inherited)))


@dataclass(frozen=True)
class WatchdogHysteresisPolicy:
    """Ersatz für die statische `free_emergency_pages=0`-Zwischenlösung (#2419).

    Verhindert, dass `memwatchdog` ein 27B-Modell während des Ladens oder einer
    kurzen KV-Spitze fälschlich entlädt, hält aber die echte Notbremse für
    Compute-PULL-Schranken (#2409) aktiv.
    """

    system_reserve_bytes: int = 5 * 1024**3          # 5 GiB Basisreserve
    emergency_floor_bytes: int = 512 * 1024**2       # 512 MiB harte Notgrenze
    cold_start_grace_seconds: float = 90.0           # Schutzfenster beim Modell-Laden
    sustained_pressure_seconds: float = 15.0         # Druckstufe muss anhalten

    def __post_init__(self) -> None:
        if type(self.system_reserve_bytes) is not int or self.system_reserve_bytes < 0:
            raise ValueError("system_reserve_bytes muss eine nichtnegative Ganzzahl sein")
        if type(self.emergency_floor_bytes) is not int or self.emergency_floor_bytes < 0:
            raise ValueError("emergency_floor_bytes muss eine nichtnegative Ganzzahl sein")
        if not instant(self.cold_start_grace_seconds) or not instant(self.sustained_pressure_seconds):
            raise ValueError("Hysterese-Zeitdauern müssen endlich und >= 0 sein")

    def should_evict_model(
        self,
        *,
        available_ram_bytes: int,
        pressure: str,
        seconds_since_model_load: float,
        sustained_critical_seconds: float,
    ) -> tuple[bool, str]:
        if type(available_ram_bytes) is not int or available_ram_bytes < 0:
            raise ValueError("available_ram_bytes muss eine nichtnegative Ganzzahl sein")
        if pressure not in VALID_PRESSURE_STATES:
            raise ValueError(f"Unbekannte Druckstufe: {pressure!r}")
        if not instant(seconds_since_model_load) or not instant(sustained_critical_seconds):
            raise ValueError("Zeitdauern müssen endlich und >= 0 sein")
        if available_ram_bytes < self.emergency_floor_bytes and pressure == "critical":
            return True, "emergency_floor_breached_critical"
        if seconds_since_model_load < self.cold_start_grace_seconds and pressure != "critical":
            return False, "within_cold_start_grace_window"
        if pressure == "critical" and sustained_critical_seconds >= self.sustained_pressure_seconds:
            return True, "sustained_critical_memory_pressure"
        return False, "within_safe_operating_envelope"


@dataclass(frozen=True)
class RunCooldownPolicy:
    """Abkühlzulassung vor jedem Lauf eines lokalen Großverbrauchers.

    Hauptkriterium ist der Swap, weil er die nicht tauschbare SSD des Mac Studio
    verschleißt (2,9 TB Schreibmenge in 45 h). Die Druckstufe allein erkennt das
    nicht: Am 08.11. 00:43 meldete sie 1 (normal) bei ~198 MiB/min Swapouts.
    """

    min_pause_seconds: float = 60.0  # Zwangspause nach jedem Lauf
    # Fester Schwellenwert (~46 GB/Tag bei Dauerlast, 1/6 der 08.11.-Messung);
    # bei Bedarf anhand realer Cooldown-Verläufe auf Apple Silicon nachkalibrieren.
    max_swapout_mib_per_minute: float = 32.0
    max_pressure_level: int = 1  # kern.memorystatus_vm_pressure_level: 2 = warn, 4 = critical
    unload_after_seconds: float = 300.0  # so lange Abkühlung -> Modell einmal entladen

    def __post_init__(self) -> None:
        if not instant(self.min_pause_seconds):
            raise ValueError("min_pause_seconds muss endlich und >= 0 sein")
        if not instant(self.max_swapout_mib_per_minute) or self.max_swapout_mib_per_minute <= 0:
            raise ValueError("max_swapout_mib_per_minute muss endlich und > 0 sein")
        if type(self.max_pressure_level) is not int or self.max_pressure_level not in {1, 2, 4}:
            raise ValueError("max_pressure_level muss 1, 2 oder 4 sein")
        if not instant(self.unload_after_seconds) or self.unload_after_seconds <= 0:
            raise ValueError("unload_after_seconds muss endlich und > 0 sein")


@dataclass(frozen=True)
class CooldownDecision:
    admitted: bool
    reason: str  # "admitted" | "cooldown:swap" | "cooldown:thermal" | "cooldown:min"
    swapout_mib_per_minute: Optional[float] = None

    def __post_init__(self) -> None:
        if type(self.admitted) is not bool:
            raise ValueError("admitted muss bool sein")
        if self.reason not in {"admitted", "cooldown:swap", "cooldown:thermal", "cooldown:min"}:
            raise ValueError(f"Unbekannter Cooldown-Grund: {self.reason!r}")
        if self.swapout_mib_per_minute is not None and not instant(self.swapout_mib_per_minute):
            raise ValueError("swapout_mib_per_minute muss endlich und >= 0 oder None sein")


def _validate_stress_sample(sample: StressSample, label: str) -> None:
    if not isinstance(sample, StressSample):
        raise TypeError(f"{label} muss StressSample oder None sein")
    if not instant(sample.observed_at):
        raise ValueError("endliche nichtnegative Messzeitpunkte erforderlich")
    if sample.swapouts_bytes is not None and (
        type(sample.swapouts_bytes) is not int or sample.swapouts_bytes < 0
    ):
        raise ValueError("swapouts_bytes muss eine nichtnegative Ganzzahl sein")
    if sample.pressure_level is not None and (
        type(sample.pressure_level) is not int or sample.pressure_level not in {1, 2, 4}
    ):
        raise ValueError("pressure_level muss 1, 2, 4 oder None sein")
    if sample.thermal_warning is not None and type(sample.thermal_warning) is not bool:
        raise ValueError("thermal_warning muss bool oder None sein")


def swapout_mib_per_minute(previous: Optional[StressSample], current: Optional[StressSample]) -> Optional[float]:
    """Swap-Schreibrate zwischen zwei Messungen; None ohne Zähler, bei Reset oder Fenster < 1 s."""
    if previous is not None:
        _validate_stress_sample(previous, "previous")
    if current is not None:
        _validate_stress_sample(current, "current")
    if previous is None or current is None or None in (previous.swapouts_bytes, current.swapouts_bytes):
        return None
    seconds = current.observed_at - previous.observed_at
    delta = current.swapouts_bytes - previous.swapouts_bytes
    if seconds < 1 or delta < 0:
        return None
    rate = delta / 1024**2 / seconds * 60
    if not math.isfinite(rate):
        raise ValueError("nicht-endliche Swap-Rate berechnet")
    return rate


@dataclass
class RunCooldownGate:
    """Eine Pause: Basismessung am Laufende, danach eine Zulassungsfrage je Abfrage.

    Fehlt eine Messung, entfällt nur ihr Kriterium; die Mindestpause bleibt. So wird
    unbekannter Swap nie zum Dauerstopp.
    """

    baseline: Optional[StressSample] = None
    policy: RunCooldownPolicy = field(default_factory=RunCooldownPolicy)
    _last_swapout_rate: Optional[float] = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.baseline is not None:
            _validate_stress_sample(self.baseline, "baseline")
        if not isinstance(self.policy, RunCooldownPolicy):
            raise TypeError("policy muss RunCooldownPolicy sein")

    def check(self, sample: Optional[StressSample], *, seconds_since_run_end: float) -> CooldownDecision:
        if sample is not None:
            _validate_stress_sample(sample, "sample")
        if self.baseline is not None:
            _validate_stress_sample(self.baseline, "baseline")
        if not instant(seconds_since_run_end):
            raise ValueError("seconds_since_run_end muss endlich und >= 0 sein")
        if not isinstance(self.policy, RunCooldownPolicy):
            raise TypeError("policy muss RunCooldownPolicy sein")
        if self.baseline is not None and sample is not None and sample.observed_at < self.baseline.observed_at:
            raise ValueError("sample.observed_at darf nicht vor baseline.observed_at liegen")
        rate = swapout_mib_per_minute(self.baseline, sample)
        if sample is None or sample.swapouts_bytes is None:
            self._last_swapout_rate = None
        elif self.baseline is None or self.baseline.swapouts_bytes is None:
            self.baseline = sample
            self._last_swapout_rate = None
        elif sample.swapouts_bytes < self.baseline.swapouts_bytes:
            # Zähler-Reset (z. B. Reboot): neue Basislinie setzen, alte Rate verwerfen
            self.baseline = sample
            self._last_swapout_rate = None
        elif (sample.observed_at - self.baseline.observed_at) >= 1:
            self.baseline = sample  # Ein ruhiges >=1s-Messfenster lässt zu; bei Flapping Hysterese ergänzen
            self._last_swapout_rate = rate
        else:
            # Sub-Sekunden-Abfrage (< 1 s): Basislinie festhalten und letzte >=1s-Rate weiterführen
            rate = self._last_swapout_rate
        policy = self.policy
        level = sample.pressure_level if sample is not None else None
        if ((rate is not None and rate > policy.max_swapout_mib_per_minute)
                or (level is not None and level > policy.max_pressure_level)):
            return CooldownDecision(False, "cooldown:swap", rate)
        if sample is not None and sample.thermal_warning is True:
            return CooldownDecision(False, "cooldown:thermal", rate)
        if seconds_since_run_end < policy.min_pause_seconds:
            return CooldownDecision(False, "cooldown:min", rate)
        return CooldownDecision(True, "admitted", rate)


@dataclass(frozen=True)
class FallbackDecision:
    request_id: str
    consumer_id: str
    admitted: bool
    action: str  # "admitted_local" | one of VALID_FALLBACK_ACTIONS
    reason: str
    effective_rank: tuple[int, int, float]

    def __post_init__(self) -> None:
        identity(self.request_id)
        identity(self.consumer_id)
        if type(self.admitted) is not bool:
            raise ValueError("admitted muss bool sein")
        if self.action not in ("admitted_local", *VALID_FALLBACK_ACTIONS):
            raise ValueError(f"Unbekannte Entscheidungsaktion: {self.action!r}")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("reason muss ein nicht-leerer String sein")
        if (
            not isinstance(self.effective_rank, tuple)
            or len(self.effective_rank) != 3
            or type(self.effective_rank[0]) is not int
            or type(self.effective_rank[1]) is not int
            or not instant(self.effective_rank[2])
        ):
            raise ValueError("effective_rank muss ein gültiges (prox, bias, submitted_at)-Tupel sein")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RAMDistributor:
    """Einziger Host-Scheduler für RAM-Fackel, Modell-Residenz und Compute-Jobs."""

    total_ram_bytes: int
    tendency: str = "balanced"
    watchdog_policy: WatchdogHysteresisPolicy = field(default_factory=WatchdogHysteresisPolicy)
    _consumers: dict[str, ConsumerCapability] = field(default_factory=dict)
    _resident_consumer_id: Optional[str] = None
    _resident_loaded_at: float = 0.0

    def __post_init__(self) -> None:
        if not positive(self.total_ram_bytes):
            raise ValueError("total_ram_bytes muss positiv sein")
        if self.tendency not in VALID_TENDENCIES:
            raise ValueError(f"Ungültige Tendenz: {self.tendency!r}")
        if not isinstance(self.watchdog_policy, WatchdogHysteresisPolicy):
            raise TypeError("watchdog_policy muss WatchdogHysteresisPolicy sein")

    def set_tendency(self, tendency: str) -> None:
        if tendency not in VALID_TENDENCIES:
            raise ValueError(f"Ungültige Tendenz: {tendency!r}; erlaubt: {VALID_TENDENCIES}")
        self.tendency = tendency

    def register_consumer(self, capability: ConsumerCapability) -> None:
        if not isinstance(capability, ConsumerCapability):
            raise TypeError("capability muss ConsumerCapability sein")
        self._consumers[capability.consumer_id] = capability

    def _tendency_bias(self, kind: str, proximity: UserProximity) -> int:
        # Direkter Nutzerbedarf (Stufe 1) wird nie durch die Tendenz benachteiligt
        if proximity == UserProximity.DIRECT_INTERACTIVE:
            return 0
        if self.tendency == "compute" and kind == "compute":
            return -1
        if self.tendency == "ollama" and kind == "model":
            return -1
        return 0

    def rank_request(self, req: DistributorRequest) -> tuple[int, int, float]:
        if not isinstance(req, DistributorRequest):
            raise TypeError("req muss DistributorRequest sein")
        cap = self._consumers.get(req.consumer_id)
        if cap is None:
            raise Hold(f"Unbekannter Verbraucher: {req.consumer_id}")
        prox = req.effective_proximity
        bias = self._tendency_bias(cap.kind, prox)
        return (int(prox), bias, req.submitted_at)

    def evaluate(
        self,
        requests: list[DistributorRequest],
        *,
        available_ram_bytes: int,
        pressure: str = "normal",
        now: float = 0.0,
    ) -> list[FallbackDecision]:
        if type(available_ram_bytes) is not int or available_ram_bytes < 0:
            raise ValueError("available_ram_bytes muss eine nichtnegative Ganzzahl sein")
        if pressure not in VALID_PRESSURE_STATES:
            raise ValueError(f"Unbekannte Druckstufe: {pressure!r}")
        if not instant(now):
            raise ValueError("now muss endlich und >= 0 sein")
        if isinstance(requests, (str, bytes)):
            raise TypeError("requests darf kein String- oder Bytes-Objekt sein")
        try:
            items = tuple(requests)
        except TypeError as exc:
            raise TypeError("requests muss über DistributorRequest-Objekte iterierbar sein") from exc
        if not items:
            return []
        for req in items:
            if not isinstance(req, DistributorRequest):
                raise TypeError("requests darf nur DistributorRequest-Objekte enthalten")
        ordered = sorted(items, key=self.rank_request)
        decisions: list[FallbackDecision] = []
        remaining_ram = max(0, available_ram_bytes - self.watchdog_policy.system_reserve_bytes)
        winner_granted = False
        pressure_blocks = pressure in {"critical", "unknown"}

        for req in ordered:
            cap = self._consumers[req.consumer_id]
            rank = self.rank_request(req)
            # Wenn das Modell bereits resident ist, zählt nur die KV-Reserve
            needed = (
                cap.kv_reserve_bytes
                if self._resident_consumer_id == req.consumer_id
                else cap.total_required_bytes
            )
            if not winner_granted and not pressure_blocks and remaining_ram >= needed:
                winner_granted = True
                remaining_ram -= needed
                if cap.kind == "model" and self._resident_consumer_id != req.consumer_id:
                    self._resident_consumer_id = req.consumer_id
                    self._resident_loaded_at = now
                decisions.append(
                    FallbackDecision(
                        request_id=req.request_id,
                        consumer_id=req.consumer_id,
                        admitted=True,
                        action="admitted_local",
                        reason="sufficient_ram_after_reserve",
                        effective_rank=rank,
                    )
                )
            else:
                fb_action = cap.fallback_chain[0] if cap.fallback_chain else "reject_hold"
                if pressure == "critical":
                    reason = "critical_memory_pressure"
                elif pressure == "unknown":
                    reason = "unknown_memory_pressure"
                else:
                    reason = "preempted_or_insufficient_ram"
                decisions.append(
                    FallbackDecision(
                        request_id=req.request_id,
                        consumer_id=req.consumer_id,
                        admitted=False,
                        action=fb_action,
                        reason=reason,
                        effective_rank=rank,
                    )
                )
        return decisions
