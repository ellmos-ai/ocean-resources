# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""Unit tests for the standalone ocean-resources module (#2441)."""

from ocean_resources import (
    ConsumerCapability,
    DistributorRequest,
    RAMDistributor,
    UserProximity,
    WatchdogHysteresisPolicy,
)


def test_standalone_ocean_resources_distributor() -> None:
    dist = RAMDistributor(total_ram_bytes=32 * 1024**3, tendency="ollama")
    dist.register_consumer(
        ConsumerCapability(
            consumer_id="qwen3.8-27b-mlx",
            kind="model",
            weights_ram_bytes=17 * 1024**3,
            kv_reserve_bytes=3 * 1024**3,
        )
    )
    req = DistributorRequest(
        request_id="req-1",
        consumer_id="qwen3.8-27b-mlx",
        proximity=UserProximity.DIRECT_INTERACTIVE,
        submitted_at=1.0,
    )
    res = dist.evaluate([req], available_ram_bytes=28 * 1024**3, pressure="normal", now=2.0)
    assert len(res) == 1
    assert res[0].admitted is True
    assert res[0].action == "admitted_local"

    # Resident reuse only needs KV reserve
    req2 = DistributorRequest(
        request_id="req-2",
        consumer_id="qwen3.8-27b-mlx",
        proximity=UserProximity.AUTONOMOUS_BACKGROUND,
        submitted_at=3.0,
    )
    res2 = dist.evaluate([req2], available_ram_bytes=9 * 1024**3, pressure="normal", now=4.0)
    assert res2[0].admitted is True


def test_watchdog_hysteresis_policy() -> None:
    policy = WatchdogHysteresisPolicy()

    # 1. During cold start grace window, non-critical pressure does not evict
    evict, reason = policy.should_evict_model(
        available_ram_bytes=4 * 1024**3,
        pressure="warning",
        seconds_since_model_load=30.0,
        sustained_critical_seconds=0.0,
    )
    assert evict is False
    assert reason == "within_cold_start_grace_window"

    # 2. Hard emergency floor breach under critical pressure evicts immediately
    evict, reason = policy.should_evict_model(
        available_ram_bytes=256 * 1024**2,
        pressure="critical",
        seconds_since_model_load=10.0,
        sustained_critical_seconds=1.0,
    )
    assert evict is True
    assert reason == "emergency_floor_breached_critical"

    # 3. Sustained critical memory pressure evicts after threshold
    evict, reason = policy.should_evict_model(
        available_ram_bytes=2 * 1024**3,
        pressure="critical",
        seconds_since_model_load=120.0,
        sustained_critical_seconds=16.0,
    )
    assert evict is True
    assert reason == "sustained_critical_memory_pressure"

    # 4. Normal operations stay within safe operating envelope
    evict, reason = policy.should_evict_model(
        available_ram_bytes=10 * 1024**3,
        pressure="normal",
        seconds_since_model_load=120.0,
        sustained_critical_seconds=0.0,
    )
    assert evict is False
    assert reason == "within_safe_operating_envelope"
