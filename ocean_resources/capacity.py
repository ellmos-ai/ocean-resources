"""Native capacity port with explicit reserves; a reading is never a grant.

CPU headroom is measured from two per-CPU cumulative counter snapshots. Core
count and load average alone never establish available CPU. psutil is optional
and imported only by the explicit native observation path.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from copy import deepcopy
import hashlib
import math
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
from threading import RLock
import time

from .contracts import CapacityReadback, Hold, identity, instant, positive, sha256_hex
from .host import sample_host

DARWIN_VM_STAT_SOURCE = "apple.platform.vm_stat.v1"
DARWIN_VM_STAT_PATH = Path("/usr/bin/vm_stat")
DARWIN_VM_STAT_REQUIREMENT = '=anchor apple and identifier "com.apple.vm_stat"'
_VM_STAT_HEADER_RE = re.compile(r"^Mach Virtual Memory Statistics: \(page size of ([0-9]+) bytes\)\Z")
_VM_STAT_LINE_RE = re.compile(r"^([^:]+):\s*([0-9]+)\.\Z")


def _power_of_two_page_size(value: object) -> bool:
    return positive(value) and value >= 4096 and (value & (value - 1)) == 0


@dataclass(frozen=True)
class DarwinVmStatSample:
    """Single signed Apple platform /usr/bin/vm_stat observation.

    Apple's vm_stat prints ``Pages free`` with ``speculative_count`` already
    subtracted, whereas Mach/psutil ``available`` equals
    ``page_size * (free_count + inactive_count)`` where ``free_count`` includes
    ``speculative_count``. Summing free + speculative + inactive from one
    platform-binary sample reproduces the exact same physical available metric
    without XNU's non-platform host_statistics64 cache.
    """
    sequence: int
    page_size: int
    pages_free: int
    pages_speculative: int
    pages_inactive: int
    pages_wired: int
    sample_started_ns: int
    sample_completed_ns: int
    vm_stat_sha256: str
    signature_verified: bool
    os_release: str
    source: str = DARWIN_VM_STAT_SOURCE

    def __post_init__(self):
        if self.source != DARWIN_VM_STAT_SOURCE:
            raise ValueError("unrecognized platform capacity sample source")
        if self.signature_verified is not True:
            raise ValueError("platform vm_stat Apple signature must be verified")
        sha256_hex(self.vm_stat_sha256)
        if not positive(self.sequence) or not _power_of_two_page_size(self.page_size):
            raise ValueError("positive sequence and power-of-two page size required")
        for count in (self.pages_free, self.pages_speculative, self.pages_inactive, self.pages_wired):
            if type(count) is not int or count < 0:
                raise ValueError("vm_stat page counters must be non-negative integers")
        if (not positive(self.sample_started_ns) or not positive(self.sample_completed_ns)
                or self.sample_started_ns > self.sample_completed_ns):
            raise ValueError("monotonic nanosecond sample bracket required")
        if not isinstance(self.os_release, str) or not self.os_release.strip() or len(self.os_release) > 128:
            raise ValueError("non-empty OS release binding required")

    @property
    def available_bytes(self) -> int:
        return self.page_size * (self.pages_free + self.pages_speculative + self.pages_inactive)

    @property
    def wired_bytes(self) -> int:
        return self.page_size * self.pages_wired


def parse_darwin_vm_stat_output(stdout: str, *, expected_page_size: int) -> dict[str, int]:
    """Strictly parse one single-shot /usr/bin/vm_stat output."""
    if not _power_of_two_page_size(expected_page_size):
        raise Hold("platform_sample_page_size_invalid")
    if not isinstance(stdout, str) or not stdout or len(stdout) > 65536:
        raise Hold("platform_sample_schema")
    lines = stdout.splitlines()
    header = _VM_STAT_HEADER_RE.fullmatch(lines[0]) if lines else None
    if header is None or int(header.group(1)) != expected_page_size:
        raise Hold("platform_sample_header")
    values: dict[str, int] = {}
    for line in lines[1:]:
        match = _VM_STAT_LINE_RE.fullmatch(line)
        if match is None:
            raise Hold("platform_sample_schema")
        key = match.group(1)
        if key in values:
            raise Hold("platform_sample_schema")
        values[key] = int(match.group(2))
    required = ("Pages free", "Pages speculative", "Pages inactive", "Pages wired down")
    if any(key not in values for key in required):
        raise Hold("platform_available_fields_missing")
    return values


def _vm_stat_identity(path: Path, *, require_root_owner: bool) -> tuple[tuple[int, int, int, int], str]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise Hold("platform_sample_binary_unavailable") from exc
    if (path.is_symlink() or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_size <= 0 or before.st_size > 8 * 1024 * 1024
            or (require_root_owner and before.st_uid != 0)):
        raise Hold("platform_sample_binary_identity")
    try:
        raw = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise Hold("platform_sample_binary_unavailable") from exc
    ident_before = (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
    ident_after = (after.st_dev, after.st_ino, after.st_mtime_ns, len(raw))
    if ident_before != ident_after:
        raise Hold("platform_sample_binary_changed")
    return ident_before, hashlib.sha256(raw).hexdigest()


def verify_darwin_vm_stat_binary(
    *,
    expected_sha256: str | None = None,
    path: Path = DARWIN_VM_STAT_PATH,
    require_root_owner: bool = True,
    runner=subprocess.run,
) -> tuple[tuple[int, int, int, int], str]:
    """Verify binary identity, optional pinned SHA-256 and strict Apple codesign."""
    if expected_sha256 is not None:
        sha256_hex(expected_sha256)
    ident, digest = _vm_stat_identity(path, require_root_owner=require_root_owner)
    if expected_sha256 is not None and digest != expected_sha256:
        raise Hold("vm_stat_binary_changed")
    try:
        proc = runner(
            ["/usr/bin/codesign", "--verify", "--strict", "-R", DARWIN_VM_STAT_REQUIREMENT, str(path)],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as exc:
        raise Hold("vm_stat_apple_signature_unconfirmed") from exc
    if getattr(proc, "returncode", None) != 0:
        raise Hold("vm_stat_apple_signature_unconfirmed")
    return ident, digest


def sample_darwin_vm_stat(
    *,
    sequence: int,
    expected_sha256: str | None = None,
    timeout: float = 3.0,
    path: Path = DARWIN_VM_STAT_PATH,
    require_root_owner: bool = True,
    runner=subprocess.run,
    clock_ns=time.monotonic_ns,
    page_size: int | None = None,
    os_release: str | None = None,
) -> DarwinVmStatSample:
    """Capture one signed Apple platform /usr/bin/vm_stat sample with ns bracketing."""
    if not positive(sequence):
        raise Hold("platform_sample_sequence_invalid")
    if not instant(timeout) or not 0 < timeout <= 10:
        raise Hold("platform_sample_timeout_invalid")
    if page_size is None:
        if platform.system() != "Darwin":
            raise Hold("darwin_platform_sample_unavailable")
        page_size = os.sysconf("SC_PAGE_SIZE")
    if not _power_of_two_page_size(page_size):
        raise Hold("platform_sample_page_size_invalid")
    release = platform.release() if os_release is None else os_release
    if not isinstance(release, str) or not release.strip():
        raise Hold("platform_sample_os_release_unavailable")
    ident_before, digest_before = verify_darwin_vm_stat_binary(
        expected_sha256=expected_sha256,
        path=path,
        require_root_owner=require_root_owner,
        runner=runner,
    )
    started_ns = clock_ns()
    try:
        proc = runner([str(path)], capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        raise Hold("platform_sample_failed") from exc
    completed_ns = clock_ns()
    if getattr(proc, "returncode", None) != 0 or getattr(proc, "stderr", ""):
        raise Hold("platform_sample_failed")
    ident_after, digest_after = _vm_stat_identity(path, require_root_owner=require_root_owner)
    if ident_after != ident_before or digest_after != digest_before:
        raise Hold("sample_binary_changed")
    values = parse_darwin_vm_stat_output(proc.stdout, expected_page_size=page_size)
    try:
        return DarwinVmStatSample(
            sequence=sequence,
            page_size=page_size,
            pages_free=values["Pages free"],
            pages_speculative=values["Pages speculative"],
            pages_inactive=values["Pages inactive"],
            pages_wired=values["Pages wired down"],
            sample_started_ns=started_ns,
            sample_completed_ns=completed_ns,
            vm_stat_sha256=digest_after,
            signature_verified=True,
            os_release=release,
        )
    except ValueError as exc:
        raise Hold("platform_sample_invalid") from exc


@dataclass(frozen=True)
class CapacityPolicy:
    reserve_ram_bytes: int
    reserve_cpu_units: int
    reserve_disk_bytes: int
    directory: Path
    sample_seconds: float = 0.25
    max_age_seconds: float = 2.0

    def __post_init__(self):
        if not all(positive(v) for v in (
            self.reserve_ram_bytes, self.reserve_cpu_units, self.reserve_disk_bytes,
        )):
            raise ValueError("explicit positive RAM, CPU and disk reserves required")
        if not instant(self.sample_seconds) or not 0.1 <= self.sample_seconds <= 1:
            raise ValueError("CPU sampling interval must be 0.1..1 seconds")
        if not instant(self.max_age_seconds) or not self.sample_seconds < self.max_age_seconds <= 5:
            raise ValueError("freshness must exceed sampling interval and be at most 5 seconds")
        p = self.directory
        if not isinstance(p, Path) or not p.is_absolute() or not p.is_dir() or p.resolve() != p.absolute():
            raise ValueError("explicit existing unlinked absolute filesystem directory required")


def idle_cpu_units(before: tuple[dict, ...], after: tuple[dict, ...], eligible: tuple[int, ...]) -> float:
    """Conservative observed idle shares, including I/O wait as busy.

Linux guest counters duplicate user/nice time and are excluded from totals.
Other optional counters count as busy; this can understate capacity (e.g.
overlapping Windows interrupt counters), but cannot turn them into idle time.
Every individual counter must be finite, nonnegative and nondecreasing.
"""
    if not isinstance(before, tuple) or not isinstance(after, tuple) or not before or len(before) != len(after):
        raise ValueError("CPU topology changed or is unknown")
    if (
        not isinstance(eligible, tuple) or not eligible or len(set(eligible)) != len(eligible)
        or any(type(i) is not int or not 0 <= i < len(before) for i in eligible)
    ):
        raise ValueError("unknown CPU affinity")
    fractions = []
    for old, new in zip(before, after):
        if (
            not isinstance(old, dict) or not isinstance(new, dict) or set(old) != set(new)
            or not {"user", "system", "idle"} <= set(old) or len(old) > 64
            or any(not isinstance(k, str) for k in old)
        ):
            raise ValueError("CPU counter schema changed")
        if any(not instant(old[k]) or not instant(new[k]) or new[k] < old[k] for k in old):
            raise ValueError("invalid or regressing CPU counters")
        delta = {k: new[k] - old[k] for k in old}
        total = sum(value for key, value in delta.items() if key not in {"guest", "guest_nice"})
        if not math.isfinite(total) or total <= 0 or not 0 <= delta["idle"] <= total:
            raise ValueError("CPU counters made no valid progress")
        fractions.append(delta["idle"] / total)
    return sum(fractions[i] for i in eligible)


def _psutil():
    try:
        import psutil
    except ImportError as exc:
        raise Hold("optional native resource dependency unavailable") from exc
    if not (5, 9, 6) <= psutil.version_info < (8,):
        raise Hold("unsupported native resource dependency version")
    return psutil


def _cpu_snapshot(backend) -> tuple[dict, ...]:
    return tuple(dict(row._asdict()) for row in backend.cpu_times(percpu=True))


def _eligible_cpus(backend, system: str, count: int) -> tuple[int, ...]:
    if system == "Darwin":
        return tuple(range(count))  # macOS exposes no psutil CPU-affinity API.
    if system == "Windows" and count > 64:
        raise Hold("Windows processor-group budget is unverified")
    result = tuple(sorted(backend.Process().cpu_affinity()))
    if not result or len(set(result)) != len(result) or any(type(i) is not int or not 0 <= i < count for i in result):
        raise Hold("CPU affinity is unavailable or inconsistent")
    return result


def _windows_in_job() -> bool:
    """Ask whether any Windows Job constrains this process, including parents.

    Querying only the innermost Job's limits cannot prove the effective budget.
    Unknown membership must not silently become unrestricted host capacity.
    """
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetCurrentProcess.argtypes = []
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    api.IsProcessInJob.restype = wintypes.BOOL
    inside = wintypes.BOOL()
    if not api.IsProcessInJob(api.GetCurrentProcess(), None, ctypes.byref(inside)):
        raise Hold("windows-job-membership-unavailable")
    return bool(inside.value)


class NativeCapacitySensor:
    """Process-bound trusted sensor port; default profile/claim gates stay separate.

    Windows Jobs and Linux cgroups remain closed until their effective RAM/CPU
    constraints are proved. No native queue/service/model is discovered.
    """

    def __init__(
        self,
        policy: CapacityPolicy,
        host_id: str | None = None,
        *,
        vm_stat_reader=None,
        use_platform_vm_stat: bool = False,
        expected_vm_stat_sha256: str | None = None,
    ):
        self.policy = policy
        self.host_id = identity(platform.node() if host_id is None else host_id)
        self._pid = os.getpid()
        self._sequence = 0
        self._mutex = RLock()
        self._vm_stat_reader = vm_stat_reader
        self._use_platform_vm_stat = bool(use_platform_vm_stat)
        self._expected_vm_stat_sha256 = expected_vm_stat_sha256
        self.last_observation: dict | None = None

    def observe(self) -> dict:
        # A child may inherit a mutex held by a vanished parent thread. Reject
        # the foreign PID before touching that mutex or any native backend.
        if os.getpid() != self._pid:
            raw = time.monotonic()
            reading = CapacityReadback(self.host_id, self._sequence + 1,
                                       raw if instant(raw) else 0.0, None, None, None, False)
            return self._report(reading, ["foreign-host-or-process"], None)
        with self._mutex:
            self._sequence += 1
            started_raw = time.monotonic()
            started = started_raw if instant(started_raw) else 0.0
            reading = CapacityReadback(self.host_id, self._sequence, started, None, None, None, False)
            errors = []
            observed_idle = None
            observations = []
            try:
                if not instant(started_raw):
                    raise Hold("native-clock-unavailable")
                if os.getpid() != self._pid or platform.node() != self.host_id:
                    raise Hold("foreign-host-or-process")
                backend = _psutil()
                first = sample_host(self.policy.directory)
                observations.append(first)
                if first.host_id != self.host_id or first.platform not in {"Windows", "Darwin", "Linux"}:
                    raise Hold("unsupported-or-foreign-host")
                if first.platform == "Windows" and _windows_in_job():
                    raise Hold("effective-windows-job-budget-unverified")
                before = _cpu_snapshot(backend)
                eligible = _eligible_cpus(backend, first.platform, len(before))
                cpu_started = time.monotonic()
                time.sleep(self.policy.sample_seconds)
                after = _cpu_snapshot(backend)
                cpu_finished = time.monotonic()
                if not instant(cpu_started) or not instant(cpu_finished):
                    raise Hold("native-clock-unavailable")
                if cpu_finished - cpu_started < self.policy.sample_seconds * 0.9:
                    raise Hold("CPU-sampling-window-too-short")
                if _eligible_cpus(backend, first.platform, len(after)) != eligible:
                    raise Hold("CPU-affinity-changed")
                observed_idle = idle_cpu_units(before, after, eligible)
                last = sample_host(self.policy.directory)
                observations.append(last)
                if first.platform == "Windows" and _windows_in_job():
                    raise Hold("effective-windows-job-budget-unverified")
                if (
                    last.host_id != self.host_id or last.platform != first.platform
                    or first.total_ram_bytes != last.total_ram_bytes
                    or first.logical_cpus != len(before) or last.logical_cpus != len(after)
                ):
                    raise Hold("host-topology-changed")
                if any(o.available_ram_bytes is None or o.disk_free_bytes is None for o in observations):
                    raise Hold("memory-or-disk-unavailable")
                ram = max(0, min(o.available_ram_bytes for o in observations) - self.policy.reserve_ram_bytes)
                cpu = max(0, math.floor(observed_idle - self.policy.reserve_cpu_units))
                pressure = all(o.pressure == "normal" for o in observations)
                disk = min(o.disk_free_bytes for o in observations) >= self.policy.reserve_disk_bytes
                # The first snapshot is the oldest input. Never refresh its age
                # simply because a later probe or output serialization completed.
                fresh = 0 <= time.monotonic() - started <= self.policy.max_age_seconds
                if not fresh:
                    errors.append("stale-observation-window")
                if first.platform == "Linux":
                    errors.append("effective-cgroup-budget-unverified")
                if not pressure:
                    errors.append("memory-pressure-not-normal")
                if not disk:
                    errors.append("disk-reserve-unavailable")
                if not ram or not cpu:
                    errors.append("insufficient-observed-headroom")
                reading = CapacityReadback(self.host_id, self._sequence, started, ram, cpu, pressure,
                                           fresh and disk and first.platform != "Linux")
            except Exception as exc:
                # No private paths, process command lines or backend exceptions.
                errors.append(str(exc) if isinstance(exc, Hold) else "native-capacity-probe-failed")
            return self._report(reading, errors, observed_idle, observations)

    def _report(self, reading, errors, observed_idle, observations=()):
        result = {
            "schema": "ocean-native-capacity.v1", "mode": "read-only-capacity-observation",
            "start_authorized": False, "reading": asdict(reading),
            "observed_idle_cpu_units": observed_idle, "errors": errors,
            "state": "CANDIDATE" if not errors else "HOLD",
            "system_reserve": {"ram_bytes": self.policy.reserve_ram_bytes,
                               "cpu_units": self.policy.reserve_cpu_units,
                               "disk_bytes": self.policy.reserve_disk_bytes},
            "profile_admission": "separate-default-closed", "models_started": 0, "queue_writes": 0,
        }
        if (len(observations) == 2 and all(o.platform == "Darwin" for o in observations)
                and all(type(o.available_ram_bytes) is int for o in observations)
                and all(o.pressure in {"normal", "elevated", "critical"} for o in observations)):
            result["model_load_capacity"] = {
                "physical_available_ram_bytes": min(o.available_ram_bytes for o in observations),
                "reserve_ram_bytes": self.policy.reserve_ram_bytes,
                "pressure_level": max({"normal": 1, "elevated": 2, "critical": 4}[o.pressure] for o in observations),
            }
        self.last_observation = deepcopy(result)
        return result

    def model_load_readback(self):
        if self._use_platform_vm_stat:
            return self.platform_model_load_readback(
                expected_vm_stat_sha256=self._expected_vm_stat_sha256,
            )
        from .model_loading import ModelLoadCapacity
        result = self.observe()
        if "model_load_capacity" not in result:
            raise Hold("native model load capacity is unavailable")
        return ModelLoadCapacity(CapacityReadback(**result["reading"]), **result["model_load_capacity"])

    def platform_model_load_readback(self, *, expected_vm_stat_sha256: str | None = None):
        """Return a ModelLoadCapacity backed by one signed Apple /usr/bin/vm_stat sample."""
        from .model_loading import ModelLoadCapacity

        if os.getpid() != self._pid:
            raise Hold("foreign-host-or-process")
        with self._mutex:
            result = self.observe()
            if "model_load_capacity" not in result:
                raise Hold("native model load capacity is unavailable")
            reading = CapacityReadback(**result["reading"])
            if self._vm_stat_reader is not None:
                vm_stat = self._vm_stat_reader(sequence=reading.sequence)
            else:
                vm_stat = sample_darwin_vm_stat(
                    sequence=reading.sequence,
                    expected_sha256=expected_vm_stat_sha256,
                )
            if not isinstance(vm_stat, DarwinVmStatSample) or vm_stat.sequence != reading.sequence:
                raise Hold("platform capacity sample sequence or schema invalid")
            finished = time.monotonic()
            if not instant(finished) or not 0 <= finished - reading.observed_at <= self.policy.max_age_seconds:
                raise Hold("stale-observation-window")
            physical_available = vm_stat.available_bytes
            if physical_available < self.policy.reserve_ram_bytes:
                raise Hold("insufficient-observed-headroom")
            updated_reading = replace(
                reading,
                available_ram_bytes=physical_available - self.policy.reserve_ram_bytes,
            )
            return ModelLoadCapacity(
                updated_reading,
                physical_available,
                self.policy.reserve_ram_bytes,
                result["model_load_capacity"]["pressure_level"],
                platform_sample=vm_stat,
            )

    def __call__(self) -> CapacityReadback:
        return CapacityReadback(**self.observe()["reading"])
