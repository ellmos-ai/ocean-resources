# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""Run admission after each run of a local large consumer.

Measured on the Mac Studio (08.11.2026 00:43): pressure level 1 (normal) while 8.9 of
10.2 GB swap were used and 12,656 pages of 16 KiB were swapped out in 60 s (~198 MiB/min).
The swap-out rate is the SSD wear signal, the pressure level alone misses it.
"""

import pytest

from ocean_resources import (
    CooldownDecision,
    RunCooldownGate,
    RunCooldownPolicy,
    StressSample,
    sample_stress,
)
from ocean_resources.host import parse_swapouts, parse_thermal_warning
from ocean_resources.ram_distributor import swapout_mib_per_minute

MIB = 1024**2
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                    4521.
Swapins:                                    78130396.
Swapouts:                                  114757066.
"""
PMSET_QUIET = """Note: No thermal warning level has been recorded
Note: No performance warning level has been recorded
Note: No CPU power status has been recorded
"""


def _sample(at, swapped_mib=0.0, *, pressure=1, thermal=False):
    return StressSample(at, int(swapped_mib * MIB), pressure, thermal)


def _gate(baseline=None):
    return RunCooldownGate(baseline=baseline if baseline is not None else _sample(0.0))


def test_swap_above_threshold_is_no_admission():
    gate = _gate()
    decision = gate.check(_sample(60.0, 198), seconds_since_run_end=120)  # the measured 198 MiB/min
    assert (decision.admitted, decision.reason) == (False, "cooldown:swap")
    assert round(decision.swapout_mib_per_minute) == 198


def test_memory_pressure_above_normal_is_a_swap_cooldown():
    decision = _gate().check(_sample(60.0, 0, pressure=2), seconds_since_run_end=120)
    assert (decision.admitted, decision.reason) == (False, "cooldown:swap")


def test_swap_below_threshold_after_the_minimum_pause_is_admitted():
    gate = _gate()
    assert gate.check(_sample(30.0, 1), seconds_since_run_end=30).reason == "cooldown:min"
    decision = gate.check(_sample(60.0, 2), seconds_since_run_end=60)  # 2 MiB/min since the last check
    assert (decision.admitted, decision.reason) == (True, "admitted")


def test_rate_follows_the_last_check_not_the_whole_pause():
    gate = _gate()
    assert gate.check(_sample(60.0, 300), seconds_since_run_end=60).reason == "cooldown:swap"
    assert gate.check(_sample(90.0, 301), seconds_since_run_end=90).admitted  # quiet since then


def test_thermal_warning_is_a_thermal_cooldown():
    decision = _gate().check(_sample(60.0, 0, thermal=True), seconds_since_run_end=120)
    assert (decision.admitted, decision.reason) == (False, "cooldown:thermal")


def test_failed_measurement_keeps_only_the_minimum_pause_never_a_permanent_stop():
    unknown = StressSample(0.0)  # every probe failed
    gate = RunCooldownGate(baseline=None)
    assert gate.check(None, seconds_since_run_end=10).reason == "cooldown:min"
    assert gate.check(unknown, seconds_since_run_end=59).reason == "cooldown:min"
    decision = gate.check(unknown, seconds_since_run_end=60)
    assert (decision.admitted, decision.swapout_mib_per_minute) == (True, None)


def test_counter_reset_or_tiny_window_measures_no_rate():
    gate = _gate(_sample(0.0, 500))
    assert gate.check(_sample(60.0, 10), seconds_since_run_end=60).swapout_mib_per_minute is None  # reboot
    gate = _gate(_sample(0.0))
    assert gate.check(_sample(0.2, 100), seconds_since_run_end=60).swapout_mib_per_minute is None


def test_policy_defaults_are_the_calibration_knob():
    policy = RunCooldownPolicy()
    assert (policy.min_pause_seconds, policy.max_swapout_mib_per_minute,
            policy.max_pressure_level, policy.unload_after_seconds) == (60.0, 32.0, 1, 300.0)
    relaxed = RunCooldownGate(baseline=_sample(0.0), policy=RunCooldownPolicy(max_swapout_mib_per_minute=250))
    assert relaxed.check(_sample(60.0, 198), seconds_since_run_end=60).admitted


def test_parsers_read_the_measured_mac_output():
    assert parse_swapouts(VM_STAT) == 114757066 * 16384
    assert parse_thermal_warning(PMSET_QUIET) is False
    assert parse_thermal_warning("Thermal Warning Level = 5\n") is True
    assert parse_thermal_warning("CPU Power notify\n\tCPU_Scheduler_Limit \t= 100\n\tCPU_Speed_Limit \t= 70\n") is True
    assert parse_thermal_warning("CPU Power notify\n\tCPU_Speed_Limit \t= 100\n") is False
    for broken in ("", "unexpected"):
        try:
            parse_thermal_warning(broken)
        except ValueError:
            continue
        raise AssertionError("unrecognised pmset output must not count as measured")


def test_sample_stress_never_raises_on_any_host():
    sample = sample_stress()
    assert isinstance(sample, StressSample) and sample.observed_at >= 0


@pytest.mark.parametrize(
    "bad_seconds",
    [float("nan"), float("inf"), float("-inf"), -0.01, True, False, "60"],
)
def test_cooldown_gate_rejects_nonfinite_negative_or_bool_pause_without_poisoning_baseline(bad_seconds):
    initial_baseline = _sample(0.0, 10)
    gate = _gate(initial_baseline)
    next_sample = _sample(60.0, 12)
    with pytest.raises(ValueError):
        gate.check(next_sample, seconds_since_run_end=bad_seconds)  # type: ignore[arg-type]
    # Baseline must remain untainted after rejected check
    assert gate.baseline == initial_baseline
    decision = gate.check(_sample(60.0, 250), seconds_since_run_end=120)
    assert (decision.admitted, decision.reason) == (False, "cooldown:swap")


def test_stress_sample_and_policy_fail_closed_on_invalid_types_and_nan():
    for bad_at in (float("nan"), float("inf"), float("-inf"), -1.0, True, False):
        with pytest.raises(ValueError):
            StressSample(bad_at)  # type: ignore[arg-type]

    for bad_swap in (-1, True, False, 1.5, float("nan")):
        with pytest.raises(ValueError):
            StressSample(1.0, swapouts_bytes=bad_swap)  # type: ignore[arg-type]

    for bad_level in (0, 3, 5, -1, True, False, "1"):
        with pytest.raises(ValueError):
            StressSample(1.0, pressure_level=bad_level)  # type: ignore[arg-type]

    for bad_thermal in ("false", "true", 0, 1):
        with pytest.raises(ValueError):
            StressSample(1.0, thermal_warning=bad_thermal)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        RunCooldownPolicy(min_pause_seconds=float("nan"))
    with pytest.raises(ValueError):
        RunCooldownPolicy(min_pause_seconds=-1.0)
    with pytest.raises(ValueError):
        RunCooldownPolicy(max_swapout_mib_per_minute=0.0)
    with pytest.raises(ValueError):
        RunCooldownPolicy(max_swapout_mib_per_minute=float("nan"))
    with pytest.raises(ValueError):
        RunCooldownPolicy(max_pressure_level=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RunCooldownPolicy(max_pressure_level=3)
    with pytest.raises(ValueError):
        RunCooldownPolicy(unload_after_seconds=0.0)
    with pytest.raises(ValueError):
        CooldownDecision(admitted="yes", reason="admitted")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CooldownDecision(admitted=True, reason="unknown")
    with pytest.raises(ValueError):
        CooldownDecision(admitted=True, reason="admitted", swapout_mib_per_minute=float("nan"))


def test_swapout_rate_defends_against_mutated_nan_sample():
    valid = _sample(0.0, 10)
    corrupted = _sample(60.0, 20)
    object.__setattr__(corrupted, "observed_at", float("nan"))
    with pytest.raises(ValueError):
        swapout_mib_per_minute(valid, corrupted)
    # Even when baseline is None, a mutated sample must fail closed before poisoning baseline
    gate = RunCooldownGate(baseline=None)
    with pytest.raises(ValueError):
        gate.check(corrupted, seconds_since_run_end=120)
    assert gate.baseline is None


@pytest.mark.parametrize("interval", [0.5, 0.6])
def test_subsecond_polling_accumulates_window_and_blocks_active_swap(interval):
    gate = _gate(_sample(0.0, 0.0))
    # Simulate sub-second polling (0.5 s and non-integer-divisor 0.6 s) under heavy 600 MiB/min swap
    swapped_mib = 0.0
    steps = round(60.0 / interval)
    decision = None
    for step in range(1, steps + 1):
        now = round(step * interval, 6)
        swapped_mib += 10.0 * interval  # 600 MiB/min
        decision = gate.check(_sample(now, swapped_mib), seconds_since_run_end=now)
        if now >= 1.0:
            assert (decision.admitted, decision.reason) == (False, "cooldown:swap")
        else:
            assert decision.admitted is False
    assert decision is not None
    assert (decision.admitted, decision.reason) == (False, "cooldown:swap")
    assert decision.swapout_mib_per_minute is not None and decision.swapout_mib_per_minute > 500
    # Once swap quiets for a full >= 1 s window after 60 s, admission recovers cleanly
    quiet = gate.check(_sample(62.0, swapped_mib), seconds_since_run_end=62.0)
    assert (quiet.admitted, quiet.reason) == (True, "admitted")


def test_cooldown_gate_rejects_backward_sample_timestamp_without_rewinding_baseline():
    baseline = _sample(100.0, 50)
    gate = _gate(baseline)
    with pytest.raises(ValueError):
        gate.check(_sample(50.0, 60), seconds_since_run_end=120)
    assert gate.baseline == baseline
