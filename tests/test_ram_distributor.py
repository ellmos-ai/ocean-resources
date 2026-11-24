# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""Unit tests for the standalone ocean-resources module (#2441)."""

import ast
from pathlib import Path
import sys
import types

import pytest

from ocean_resources import (
    ConsumerCapability,
    DistributorRequest,
    FallbackDecision,
    Hold,
    RAMDistributor,
    UserProximity,
    WatchdogHysteresisPolicy,
)
import ocean_resources.model_dispatch as model_dispatch


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


def test_watchdog_and_distributor_fail_closed_on_nan_bool_and_unknown_pressure() -> None:
    policy = WatchdogHysteresisPolicy()
    for bad_duration in (float("nan"), float("inf"), float("-inf"), -1.0, True, False):
        with pytest.raises(ValueError):
            policy.should_evict_model(
                available_ram_bytes=2 * 1024**3,
                pressure="critical",
                seconds_since_model_load=120.0,
                sustained_critical_seconds=bad_duration,  # type: ignore[arg-type]
            )
    for bad_ram in (-1, True, False, 1.5, float("nan")):
        with pytest.raises(ValueError):
            policy.should_evict_model(
                available_ram_bytes=bad_ram,  # type: ignore[arg-type]
                pressure="critical",
                seconds_since_model_load=120.0,
                sustained_critical_seconds=20.0,
            )
    with pytest.raises(ValueError):
        policy.should_evict_model(
            available_ram_bytes=2 * 1024**3,
            pressure="corrupted",
            seconds_since_model_load=120.0,
            sustained_critical_seconds=20.0,
        )

    with pytest.raises(ValueError):
        ConsumerCapability(
            consumer_id="m1",
            kind="model",
            weights_ram_bytes=1024,
            kv_reserve_bytes=True,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        ConsumerCapability(
            consumer_id="m1",
            kind="model",
            weights_ram_bytes=1024,
            can_checkpoint="false",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        ConsumerCapability(
            consumer_id="m1",
            kind="model",
            weights_ram_bytes=1024,
            fallback_chain="cloud_router",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        DistributorRequest(
            request_id="r1",
            consumer_id="m1",
            proximity=True,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        DistributorRequest(
            request_id="r1",
            consumer_id="m1",
            parent_request_id="../escape",
        )

    dist = RAMDistributor(total_ram_bytes=32 * 1024**3, tendency="balanced")
    dist.register_consumer(
        ConsumerCapability(
            consumer_id="c1",
            kind="compute",
            weights_ram_bytes=4 * 1024**3,
        )
    )
    req = DistributorRequest(request_id="req-u", consumer_id="c1")
    decisions = dist.evaluate([req], available_ram_bytes=28 * 1024**3, pressure="unknown", now=1.0)
    assert decisions[0].admitted is False
    assert decisions[0].reason == "unknown_memory_pressure"

    for bad_now in (float("nan"), float("inf"), -1.0, True):
        with pytest.raises(ValueError):
            dist.evaluate([req], available_ram_bytes=28 * 1024**3, pressure="normal", now=bad_now)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        dist.evaluate([req], available_ram_bytes=True, pressure="normal", now=1.0)  # type: ignore[arg-type]

    # Single-pass generator/iterator must not be exhausted by validation
    gen_decisions = dist.evaluate(
        (item for item in (req,)),  # type: ignore[arg-type]
        available_ram_bytes=28 * 1024**3,
        pressure="normal",
        now=2.0,
    )
    assert len(gen_decisions) == 1 and gen_decisions[0].admitted is True

    # Falsy non-iterables and string/bytes must raise TypeError instead of silently returning []
    for bad_requests in (None, False, 0, "", "req", b"", [object()]):
        with pytest.raises(TypeError):
            dist.evaluate(bad_requests, available_ram_bytes=28 * 1024**3, pressure="normal", now=2.0)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        FallbackDecision(
            request_id="r1",
            consumer_id="c1",
            admitted="yes",  # type: ignore[arg-type]
            action="admitted_local",
            reason="ok",
            effective_rank=(1, 0, 0.0),
        )


def _iter_module_scope_nodes(nodes: list[ast.stmt]):
    for stmt in nodes:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield stmt
        for child_field in ("body", "orelse", "finalbody", "handlers", "cases"):
            children = getattr(stmt, child_field, None)
            if isinstance(children, list):
                for item in children:
                    if isinstance(item, ast.stmt):
                        yield from _iter_module_scope_nodes([item])
                    elif hasattr(item, "body") and isinstance(item.body, list):
                        yield from _iter_module_scope_nodes(item.body)


def test_zero_mandatory_dependency_psutil_lazy_fencing(monkeypatch: pytest.MonkeyPatch) -> None:
    allowed_roots = set(sys.stdlib_module_names) | {"ocean_resources"}
    pkg_dir = Path(model_dispatch.__file__).resolve().parent
    for py_file in pkg_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in _iter_module_scope_nodes(tree.body):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root in allowed_roots, f"Non-stdlib module-scope import {root!r} in {py_file.name}"
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                root = node.module.split(".")[0]
                assert root in allowed_roots, f"Non-stdlib module-scope from-import {root!r} in {py_file.name}"

    def missing_psutil():
        raise Hold("optional native resource dependency unavailable")

    monkeypatch.setattr(model_dispatch, "_psutil", missing_psutil)
    with pytest.raises(Hold, match="optional native resource dependency unavailable"):
        model_dispatch.native_writer_identity(12345)

    class DeadError(Exception):
        pass

    fake_ps = types.SimpleNamespace(
        Error=DeadError,
        STATUS_ZOMBIE="zombie",
        Process=lambda _pid: (_ for _ in ()).throw(DeadError("gone")),
    )
    monkeypatch.setattr(model_dispatch, "_psutil", lambda: fake_ps)
    with pytest.raises(Hold, match="native model writer is not alive"):
        model_dispatch.native_writer_identity(12345)
