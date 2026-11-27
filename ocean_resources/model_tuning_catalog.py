"""Hardware- and RAM-coupled default model tuning catalog (`ocean.model-tuning-catalog.v1`).

Provides zero-config safe defaults for local models based on measured host RAM,
following the unified resolution precedence:
    slot_override > model_config > catalog_default > env_fallback > safe_baseline.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Any

CATALOG_SCHEMA = "ocean.model-tuning-catalog.v1"
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent / "model-tuning-catalog.v1.json"
MIN_NUM_CTX = 512
MAX_NUM_CTX = 262_144
GIB = 1024**3


@dataclass(frozen=True)
class ResolvedModelTuning:
    """Effective tuning parameters resolved for one local model on a given host."""

    model_id: str
    catalog_model_id: str | None
    host_ram_gib: float
    matched_tier_min_ram_gib: int | None
    supported_on_host: bool
    num_ctx: int
    num_ctx_source: str
    num_ctx_safe: int
    num_ctx_max: int
    kv_cache_type: str
    flash_attention: bool
    think_level: str
    recommended_max_tool_rounds: int
    handoff_percent: int
    kv_headroom_bytes: int
    overhead_bytes: int
    confidence_tier: str
    tool_calling_verified: bool
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "ocean.resolved-model-tuning.v1",
            "model_id": self.model_id,
            "catalog_model_id": self.catalog_model_id,
            "host_ram_gib": round(self.host_ram_gib, 2),
            "matched_tier_min_ram_gib": self.matched_tier_min_ram_gib,
            "supported_on_host": self.supported_on_host,
            "num_ctx": self.num_ctx,
            "num_ctx_source": self.num_ctx_source,
            "num_ctx_safe": self.num_ctx_safe,
            "num_ctx_max": self.num_ctx_max,
            "kv_cache_type": self.kv_cache_type,
            "flash_attention": self.flash_attention,
            "think_level": self.think_level,
            "recommended_max_tool_rounds": self.recommended_max_tool_rounds,
            "handoff_percent": self.handoff_percent,
            "kv_headroom_bytes": self.kv_headroom_bytes,
            "overhead_bytes": self.overhead_bytes,
            "confidence_tier": self.confidence_tier,
            "tool_calling_verified": self.tool_calling_verified,
            "notes": self.notes,
        }


def _validate_num_ctx(raw: Any, *, field_name: str = "num_ctx") -> int:
    if isinstance(raw, bool):
        raise ValueError(f"{field_name} must be an integer, not bool")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if not MIN_NUM_CTX <= value <= MAX_NUM_CTX:
        raise ValueError(f"{field_name} must be between {MIN_NUM_CTX} and {MAX_NUM_CTX}")
    return value


def detect_host_ram_gib() -> float:
    """Detect total physical host RAM in GiB using stdlib/OS primitives only."""
    if os.name == "nt":
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return float(status.ullTotalPhys) / GIB

    if hasattr(os, "sysconf"):
        try:
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            phys_pages = int(os.sysconf("SC_PHYS_PAGES"))
            if page_size > 0 and phys_pages > 0:
                return float(page_size * phys_pages) / GIB
        except (OSError, ValueError):
            pass

    try:
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=2).strip()
        total = int(out)
        if total > 0:
            return float(total) / GIB
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    return 16.0


def load_model_tuning_catalog(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate the curated hardware-/RAM-coupled model tuning catalog."""
    target = Path(path).resolve() if path is not None else DEFAULT_CATALOG_PATH
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != CATALOG_SCHEMA:
        raise ValueError(f"Invalid model tuning catalog schema in {target}")
    models = raw.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError(f"Model tuning catalog must define at least one model: {target}")

    seen_ids: set[str] = set()
    for entry in models:
        if not isinstance(entry, dict):
            raise ValueError("Catalog model entry must be an object")
        mid = str(entry.get("model_id") or "").strip()
        if not mid or mid in seen_ids:
            raise ValueError(f"Duplicate or empty model_id in catalog: {mid!r}")
        seen_ids.add(mid)
        if float(entry.get("base_vram_gb", 0)) <= 0 or float(entry.get("kv_mb_per_1k_ctx", 0)) <= 0:
            raise ValueError(f"Model {mid} must have positive base_vram_gb and kv_mb_per_1k_ctx")
        tiers = entry.get("ram_tiers")
        if not isinstance(tiers, list) or not tiers:
            raise ValueError(f"Model {mid} must define non-empty ram_tiers")
        for tier in tiers:
            safe = _validate_num_ctx(tier.get("num_ctx_safe"), field_name=f"{mid}.num_ctx_safe")
            maximum = _validate_num_ctx(tier.get("num_ctx_max"), field_name=f"{mid}.num_ctx_max")
            if safe > maximum:
                raise ValueError(f"Model {mid} tier has num_ctx_safe ({safe}) > num_ctx_max ({maximum})")
    return raw


def _normalise_model_name(name: str) -> str:
    return name.strip().lower()


def _find_catalog_entry(catalog: dict[str, Any], model_id: str) -> dict[str, Any] | None:
    target = _normalise_model_name(model_id)
    if not target:
        return None
    bare = target.split(":", 1)[0]
    with_latest = target if ":" in target else f"{target}:latest"

    for entry in catalog.get("models") or []:
        candidates = {_normalise_model_name(entry["model_id"])}
        for alias in entry.get("aliases") or []:
            candidates.add(_normalise_model_name(str(alias)))
        if target in candidates or with_latest in candidates or bare in candidates:
            return entry
    return None


def _select_ram_tier(tiers: list[dict[str, Any]], host_ram_gib: float) -> dict[str, Any]:
    ordered = sorted(tiers, key=lambda item: float(item.get("min_ram_gib", 0)))
    chosen = ordered[0]
    for tier in ordered:
        if host_ram_gib + 0.5 >= float(tier.get("min_ram_gib", 0)):
            chosen = tier
    return chosen


def resolve_model_tuning(
    model_id: str,
    *,
    host_ram_gib: float | None = None,
    slot_override: dict[str, Any] | None = None,
    model_config: dict[str, Any] | None = None,
    env_num_ctx: int | str | None = None,
    catalog: dict[str, Any] | None = None,
    catalog_path: str | Path | None = None,
) -> ResolvedModelTuning:
    """Resolve effective local model tuning via unified precedence.

    Precedence for ``num_ctx``:
        1. ``slot_override["num_ctx"]``
        2. ``model_config["num_ctx"]``
        3. Hardware-/RAM-coupled ``catalog_default``
        4. ``env_num_ctx`` (e.g. ``OLLAMA_NUM_CTX``)
        5. Safe baseline (4096)
    """
    ram_gib = float(host_ram_gib) if host_ram_gib is not None else detect_host_ram_gib()
    cat = catalog if catalog is not None else load_model_tuning_catalog(catalog_path)
    fallback = cat.get("default_fallback") or {}
    entry = _find_catalog_entry(cat, model_id)

    if entry is not None:
        tier = _select_ram_tier(entry["ram_tiers"], ram_gib)
        catalog_model_id = str(entry["model_id"])
        matched_tier = int(tier["min_ram_gib"])
        supported = bool(tier.get("supported", True))
        num_ctx_safe = _validate_num_ctx(tier["num_ctx_safe"])
        num_ctx_max = _validate_num_ctx(tier["num_ctx_max"])
        kv_cache_type = str(tier.get("kv_cache_type", "q8_0"))
        flash_attention = bool(tier.get("flash_attention", True))
        think_level = str(tier.get("think_level", "auto"))
        max_rounds = int(tier.get("recommended_max_tool_rounds", 25))
        handoff_pct = int(tier.get("handoff_percent", 75))
        kv_headroom_bytes = int(round(float(tier.get("kv_headroom_gb", 1.5)) * GIB))
        overhead_bytes = int(round(float(tier.get("overhead_gb", 1.5)) * GIB))
        base_confidence = str(entry.get("confidence_tier", "curated"))
        tool_verified = bool(entry.get("tool_calling_verified", False))
        notes = str(tier.get("notes", ""))
    else:
        catalog_model_id = None
        matched_tier = None
        supported = True
        num_ctx_safe = _validate_num_ctx(fallback.get("num_ctx_safe", 4096))
        num_ctx_max = _validate_num_ctx(fallback.get("num_ctx_max", 8192))
        kv_cache_type = str(fallback.get("kv_cache_type", "q8_0"))
        flash_attention = bool(fallback.get("flash_attention", True))
        think_level = str(fallback.get("think_level", "auto"))
        max_rounds = int(fallback.get("recommended_max_tool_rounds", 25))
        handoff_pct = int(fallback.get("handoff_percent", 75))
        kv_headroom_bytes = int(round(float(fallback.get("kv_headroom_gb", 1.0)) * GIB))
        overhead_bytes = int(round(float(fallback.get("overhead_gb", 1.0)) * GIB))
        base_confidence = "fallback"
        tool_verified = False
        notes = "Model not present in curated catalog; using safe baseline."

    # Unified precedence chain for num_ctx
    confidence = base_confidence
    if isinstance(slot_override, dict) and slot_override.get("num_ctx") is not None:
        effective_ctx = _validate_num_ctx(slot_override["num_ctx"], field_name="slot_override.num_ctx")
        source = "slot_override"
        confidence = "manual_override"
    elif isinstance(model_config, dict) and model_config.get("num_ctx") is not None:
        effective_ctx = _validate_num_ctx(model_config["num_ctx"], field_name="model_config.num_ctx")
        source = "model_config"
        confidence = "manual_override"
    elif entry is not None:
        effective_ctx = num_ctx_safe
        source = "catalog_default"
    elif env_num_ctx is not None and str(env_num_ctx).strip():
        effective_ctx = _validate_num_ctx(env_num_ctx, field_name="OLLAMA_NUM_CTX")
        source = "env_fallback"
    else:
        effective_ctx = num_ctx_safe
        source = "safe_baseline"

    if isinstance(slot_override, dict) and slot_override.get("max_tool_rounds") is not None:
        max_rounds = int(slot_override["max_tool_rounds"])
    elif isinstance(model_config, dict) and model_config.get("max_tool_rounds") is not None:
        max_rounds = int(model_config["max_tool_rounds"])

    return ResolvedModelTuning(
        model_id=model_id,
        catalog_model_id=catalog_model_id,
        host_ram_gib=ram_gib,
        matched_tier_min_ram_gib=matched_tier,
        supported_on_host=supported,
        num_ctx=effective_ctx,
        num_ctx_source=source,
        num_ctx_safe=num_ctx_safe,
        num_ctx_max=num_ctx_max,
        kv_cache_type=kv_cache_type,
        flash_attention=flash_attention,
        think_level=think_level,
        recommended_max_tool_rounds=max_rounds,
        handoff_percent=handoff_pct,
        kv_headroom_bytes=kv_headroom_bytes,
        overhead_bytes=overhead_bytes,
        confidence_tier=confidence,
        tool_calling_verified=tool_verified,
        notes=notes,
    )


def export_clutch_local_gears(
    *,
    host_ram_gib: float | None = None,
    endpoint: str = "http://localhost:11434",
    include_unsupported: bool = False,
    catalog: dict[str, Any] | None = None,
    catalog_path: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Export local Ollama gears compatible with `clutch/config/getriebe.json`."""
    ram_gib = float(host_ram_gib) if host_ram_gib is not None else detect_host_ram_gib()
    cat = catalog if catalog is not None else load_model_tuning_catalog(catalog_path)
    gears: dict[str, dict[str, Any]] = {}

    for entry in cat.get("models") or []:
        mid = str(entry["model_id"])
        resolved = resolve_model_tuning(mid, host_ram_gib=ram_gib, catalog=cat)
        if not include_unsupported and not resolved.supported_on_host:
            continue
        slug = mid.split(":", 1)[0].replace(".", "-").replace("/", "-")
        tag = mid.split(":", 1)[1] if ":" in mid else "latest"
        gear_key = f"ollama-{slug}" if tag == "latest" else f"ollama-{slug}-{tag.replace('.', '-')}"
        strengths = ["kostenlos", "lokal", "datenschutz"]
        if resolved.tool_calling_verified:
            strengths.append("tool_calling")
        if "mlx" in (entry.get("backends") or []):
            strengths.append("apple_silicon_mlx")
        gears[gear_key] = {
            "provider": "ollama",
            "model_id": mid,
            "gang": int(entry.get("clutch_gang", 2)),
            "leistung": str(entry.get("clutch_leistung", "mittel")),
            "kosten_input_1k": 0.0,
            "kosten_output_1k": 0.0,
            "staerken": strengths,
            "schwaechen": [] if resolved.supported_on_host else ["ram_zu_knapp"],
            "max_context": resolved.num_ctx_safe,
            "num_ctx_safe": resolved.num_ctx_safe,
            "num_ctx_max": resolved.num_ctx_max,
            "recommended_max_tool_rounds": resolved.recommended_max_tool_rounds,
            "handoff_percent": resolved.handoff_percent,
            "min_ram_gib": resolved.matched_tier_min_ram_gib,
            "supported_on_host": resolved.supported_on_host,
            "confidence_tier": resolved.confidence_tier,
            "source_schema": CATALOG_SCHEMA,
            "endpoint": endpoint,
        }
    return gears
