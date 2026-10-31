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
