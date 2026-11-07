"""Read-only host observations. A snapshot is never permission to start work."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import ctypes
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time

from .contracts import identity, instant, positive


@dataclass(frozen=True)
class HostObservation:
    host_id: str
    platform: str
    observed_at: float  # UTC Unix seconds; comparable across hosts, unlike monotonic uptime.
    total_ram_bytes: int | None
    available_ram_bytes: int | None
    logical_cpus: int | None
    load_average_1m: float | None
    pressure: str
    disk_free_bytes: int | None
    measurement_source: str
    errors: tuple[str, ...] = ()

    def __post_init__(self):
        identity(self.host_id)
        if self.platform not in {"Windows", "Darwin", "Linux", "unknown"}:
            raise ValueError("explicit known platform or unknown required")
        if not instant(self.observed_at):
            raise ValueError("finite UTC observation time required")
        if self.total_ram_bytes is not None and not positive(self.total_ram_bytes):
            raise ValueError("invalid total physical memory")
        for value in (self.available_ram_bytes, self.disk_free_bytes):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("memory and disk samples must be nonnegative integers or unknown")
        if self.available_ram_bytes is not None and (
            self.total_ram_bytes is None or self.available_ram_bytes > self.total_ram_bytes
        ):
            raise ValueError("available memory cannot exceed known physical memory")
        if self.logical_cpus is not None and not positive(self.logical_cpus):
            raise ValueError("invalid logical CPU count")
        if self.load_average_1m is not None and (
            type(self.load_average_1m) not in {int, float}
            or not math.isfinite(self.load_average_1m)
            or self.load_average_1m < 0
        ):
            raise ValueError("finite nonnegative CPU load or unknown required")
        if self.pressure not in {"normal", "elevated", "critical", "unknown"}:
            raise ValueError("unknown pressure state")
        if not isinstance(self.measurement_source, str) or not 1 <= len(self.measurement_source) <= 256:
            raise ValueError("bounded measurement source required")
        if not isinstance(self.errors, tuple) or any(
            not isinstance(error, str) or not 1 <= len(error) <= 128 for error in self.errors
        ):
            raise ValueError("bounded explicit probe errors required")

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "mode": "read-only-observation",
            "start_authorized": False,
            "cpu_load_is_admission_capacity": False,
        }


def _command(argv: list[str]) -> str:
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode:
        raise ValueError("native probe failed")
    return result.stdout


def parse_macos_vm(text: str) -> int:
    """Conservative readily reclaimable estimate; never counts compressed pages."""
    match = re.search(r"page size of (\d+) bytes", text)
    if not match:
        raise ValueError("unknown vm_stat page size")
    page_size = int(match[1])
    if page_size not in {4096, 16384, 65536}:
        raise ValueError("unsupported page size")
    pages = []
    for label in ("Pages free", "Pages inactive", "Pages speculative"):
        item = re.search(r"^" + label + r":\s+(\d+)\.?\s*$", text, re.MULTILINE)
        if not item:
            raise ValueError("incomplete vm_stat")
        pages.append(int(item[1]))
    return sum(pages) * page_size


@dataclass(frozen=True)
class StressSample:
    """Read-only stress signals for run admission (PB-26); None means not measured."""

    observed_at: float  # time.monotonic() of this host; only used for rates between two samples
    swapouts_bytes: int | None = None  # cumulative vm_stat "Swapouts" x page size (SSD writes)
    pressure_level: int | None = None  # kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical
    thermal_warning: bool | None = None  # pmset -g therm


def parse_swapouts(text: str) -> int:
    page = re.search(r"page size of (\d+) bytes", text)
    item = re.search(r"^Swapouts:\s+(\d+)\.?\s*$", text, re.MULTILINE)
    if not page or not item or int(page[1]) not in {4096, 16384, 65536}:
        raise ValueError("vm_stat without verified Swapouts")
    return int(item[1]) * int(page[1])


def parse_thermal_warning(text: str) -> bool:
    """pmset -g therm: a warning level above 0 or a CPU limit below 100 is a warning."""
    levels = re.findall(r"warning level\s*(?:=|:|set to)\s*(\d+)", text, re.IGNORECASE)
    limits = re.findall(r"CPU_(?:Speed|Scheduler)_Limit\s*=\s*(\d+)", text)
    if not levels and not limits and "No thermal warning level has been recorded" not in text:
        raise ValueError("unrecognised pmset output")
    return any(int(v) > 0 for v in levels) or any(int(v) < 100 for v in limits)


def _pressure_level() -> int:
    level = int(_command(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip())
    if level not in {1, 2, 4}:
        raise ValueError("unknown pressure level")
    return level


def sample_stress() -> StressSample:
    """vm_stat, sysctl and pmset only (no sudo, no writes); each failed probe stays None."""
    now = time.monotonic()
    if platform.system() != "Darwin":
        return StressSample(now)
    values = []
    for probe in (
        lambda: parse_swapouts(_command(["/usr/bin/vm_stat"])),
        _pressure_level,
        lambda: parse_thermal_warning(_command(["/usr/bin/pmset", "-g", "therm"])),
    ):
        try:
            values.append(probe())
        except (OSError, ValueError, subprocess.SubprocessError):
            values.append(None)
    return StressSample(now, *values)


def _windows_memory() -> tuple[int, int]:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("load", ctypes.c_ulong),
            *[
                (name, ctypes.c_ulonglong)
                for name in (
                    "total",
                    "available",
                    "total_page",
                    "available_page",
                    "total_virtual",
                    "available_virtual",
                    "extended",
                )
            ],
        ]

    value = MemoryStatus()
    value.length = ctypes.sizeof(value)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
        raise OSError("GlobalMemoryStatusEx failed")
    return int(value.total), int(value.available)


def sample_host(directory: Path | None = None) -> HostObservation:
    """No writes, networking, model API, queue discovery or service manipulation."""
    system = platform.system()
    total = available = disk = load = None
    pressure = "unknown"
    errors = []
    source = "unsupported"
    try:
        if system == "Windows":
            total, available = _windows_memory()
            if total <= 0:
                raise ValueError("invalid physical memory size")
            source = "GlobalMemoryStatusEx"
            # Ratio is a conservative observation, not a native pressure signal.
            pressure = "normal" if available / total >= 0.20 else "elevated"
        elif system == "Darwin":
            total = int(_command(["/usr/sbin/sysctl", "-n", "hw.memsize"]).strip())
            available = parse_macos_vm(_command(["/usr/bin/vm_stat"]))
            level = int(_command(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip())
            pressure = {1: "normal", 2: "elevated", 4: "critical"}.get(level, "unknown")
            source = "vm_stat free+inactive+speculative; sysctl pressure"
        elif system == "Linux":
            values = {}
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                match = re.fullmatch(r"(MemTotal|MemAvailable):\s+(\d+) kB", line)
                if match:
                    values[match[1]] = int(match[2]) * 1024
            total, available = values["MemTotal"], values["MemAvailable"]
            if total <= 0:
                raise ValueError("invalid physical memory size")
            pressure = "normal" if available / total >= 0.20 else "elevated"
            source = "/proc/meminfo; available ratio (not PSI)"
        else:
            errors.append("unsupported-platform")
        if total is not None and not (0 <= available <= total and total > 0):
            raise ValueError("inconsistent memory probe")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        errors.append("memory-probe-unavailable")
        total = available = None
        pressure = "unknown"
    try:
        disk = shutil.disk_usage(directory if directory is not None else Path.home()).free
    except OSError:
        errors.append("disk-probe-unavailable")
    if hasattr(os, "getloadavg"):
        try:
            load = os.getloadavg()[0]
        except OSError:
            errors.append("load-probe-unavailable")
    return HostObservation(
        platform.node(),
        system,
        time.time(),
        total,
        available,
        os.cpu_count(),
        load,
        pressure,
        disk,
        source,
        tuple(errors),
    )
