"""Tests for the hardware-/RAM-coupled default model tuning catalog."""
from __future__ import annotations

import pytest

from ocean_resources import (
    CATALOG_SCHEMA,
    export_clutch_local_gears,
    load_model_tuning_catalog,
    resolve_model_tuning,
)


def test_catalog_loads_and_encodes_verified_mac_studio_32gb_boundary():
    cat = load_model_tuning_catalog()
    assert cat["schema"] == CATALOG_SCHEMA
    assert len(cat["models"]) >= 6

    # 32 GiB Apple M1 Max empirical optimum: 16384 ctx + 40 tool rounds (24k OOMs under load)
    t32 = resolve_model_tuning("inkling-2-mini:latest", host_ram_gib=32.0)
    assert t32.supported_on_host is True
    assert t32.num_ctx == 16384
    assert t32.num_ctx_safe == 16384
    assert t32.num_ctx_max == 16384
    assert t32.num_ctx_source == "catalog_default"
    assert t32.recommended_max_tool_rounds == 40
    assert t32.handoff_percent == 75
    assert t32.confidence_tier == "measured_live"

    # 16 GiB host must mark 26.4B inkling-2-mini as unsupported
    t16 = resolve_model_tuning("inkling-2-mini", host_ram_gib=16.0)
    assert t16.supported_on_host is False
    assert t16.matched_tier_min_ram_gib == 0

    # 64 GiB host unlocks 32k safe / 64k max
    t64 = resolve_model_tuning("inkling-2-mini:latest", host_ram_gib=64.0)
    assert t64.supported_on_host is True
    assert t64.num_ctx == 32768
    assert t64.num_ctx_max == 65536
    assert t64.recommended_max_tool_rounds == 50


def test_unified_num_ctx_precedence_chain():
    # 1. Slot override beats model_config, catalog, and env
    r1 = resolve_model_tuning(
        "inkling-2-mini:latest",
        host_ram_gib=32.0,
        slot_override={"num_ctx": 12288, "max_tool_rounds": 35},
        model_config={"num_ctx": 8192},
        env_num_ctx="24576",
    )
    assert r1.num_ctx == 12288
    assert r1.num_ctx_source == "slot_override"
    assert r1.confidence_tier == "manual_override"
    assert r1.recommended_max_tool_rounds == 35

    # 2. Model config beats catalog and env
    r2 = resolve_model_tuning(
        "inkling-2-mini:latest",
        host_ram_gib=32.0,
        model_config={"num_ctx": 8192},
        env_num_ctx="24576",
    )
    assert r2.num_ctx == 8192
    assert r2.num_ctx_source == "model_config"
    assert r2.confidence_tier == "manual_override"

    # 3. Catalog default beats env_fallback for known models
    r3 = resolve_model_tuning(
        "gemma4:12b",
        host_ram_gib=16.0,
        env_num_ctx="4096",
    )
    assert r3.num_ctx == 16384
    assert r3.num_ctx_source == "catalog_default"
    assert r3.confidence_tier == "curated"

    # 4. Unknown model uses env_fallback, then safe_baseline
    r4 = resolve_model_tuning("custom-local:latest", host_ram_gib=16.0, env_num_ctx="12288")
    assert r4.num_ctx == 12288
    assert r4.num_ctx_source == "env_fallback"
    assert r4.confidence_tier == "fallback"

    r5 = resolve_model_tuning("custom-local:latest", host_ram_gib=16.0)
    assert r5.num_ctx == 4096
    assert r5.num_ctx_source == "safe_baseline"


def test_invalid_num_ctx_rejected():
    with pytest.raises(ValueError):
        resolve_model_tuning("qwen3:8b", host_ram_gib=16.0, slot_override={"num_ctx": True})
    with pytest.raises(ValueError):
        resolve_model_tuning("qwen3:8b", host_ram_gib=16.0, model_config={"num_ctx": 256})


def test_clutch_local_gears_export_filters_by_host_ram():
    gears_16 = export_clutch_local_gears(host_ram_gib=16.0)
    assert "ollama-qwen3-8b" in gears_16
    assert "ollama-gemma4-12b" in gears_16
    assert "ollama-inkling-2-mini" not in gears_16

    gears_32 = export_clutch_local_gears(host_ram_gib=32.0)
    assert "ollama-inkling-2-mini" in gears_32
    ink = gears_32["ollama-inkling-2-mini"]
    assert ink["num_ctx_safe"] == 16384
    assert ink["recommended_max_tool_rounds"] == 40
    assert ink["confidence_tier"] == "measured_live"
    assert ink["source_schema"] == CATALOG_SCHEMA
