# SPDX-License-Identifier: MIT
"""Preflight the pinned Qwen text checkpoint before wiring any weights."""
from dataclasses import dataclass, replace
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
from math import isfinite
from pathlib import Path
import struct
from types import MappingProxyType
from typing import Mapping

from .contracts import Hold, sha256_hex
from .mlx_weights import MLX_VERSION
from .model_loading import parse_safetensors_header

QWEN_MODEL = "qwen3.8:27b-mlx"
QWEN_MANIFEST_SHA256 = "5642e97495e1a088883805981563dcdc4a040c2f53388b7a41d1f24d3622cf7e"
MLX_LM_VERSION = "0.31.3"
MANIFEST_NAME = "manifests/registry.ollama.ai/library/qwen3.8/27b-mlx"
PAGE_BYTES = 16 * 1024


@dataclass(frozen=True)
class QwenWeightSource:
    name: str
    path: Path
    offset: int
    nbytes: int
    dtype: str
    raw_shape: tuple[int, ...]
    shape: tuple[int, ...]
    transform: str | None = None

    @property
    def ndim(self):
        return len(self.shape)

    def moveaxis(self, source, target):
        if (source, target) != (2, 1) or len(self.shape) != 3 or self.transform is not None:
            raise Hold("unsupported Qwen convolution transform")
        channels, width, kernel = self.shape
        return replace(self, shape=(channels, kernel, width), transform="conv_channels_last")

    def __add__(self, value):
        if value != 1 or self.ndim != 1 or self.transform is not None:
            raise Hold("unsupported Qwen normalization transform")
        return replace(self, transform="rmsnorm_add_one")

    def global_scale(self):
        if (self.dtype, self.shape, self.nbytes) != ("F32", (), 4):
            raise Hold("invalid Qwen global scale layout")
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            raw = stream.read(4)
        if len(raw) != 4:
            raise Hold("incomplete Qwen global scale")
        scalar, = struct.unpack("<f", raw)
        if not isfinite(scalar) or scalar <= 0:
            raise Hold("invalid Qwen global scale value")
        return scalar


@dataclass(frozen=True)
class QwenTextPlan:
    model_digest: str
    config_json: bytes
    parameters: Mapping[str, tuple[QwenWeightSource, ...]]
    block_types: tuple[str, ...]
    wired_bytes: int
    subpage_bytes: int


def _blob(root, entry):
    scheme, sep, digest = entry["digest"].partition(":")
    if scheme != "sha256" or not sep:
        raise Hold("invalid Qwen blob digest")
    sha256_hex(digest)
    path = root / "blobs" / ("sha256-" + digest)
    if path.is_symlink() or not path.is_file() or path.stat().st_size != entry["size"]:
        raise Hold("Qwen blob differs from its manifest size")
    return path, digest


def _read_catalog(root, model_digest):
    path = root / MANIFEST_NAME
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise Hold("Qwen manifest is missing or exceeds its size bound")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != model_digest:
        raise Hold("Qwen manifest differs from the reviewed model digest")
    layers = json.loads(raw)["layers"]
    config_path, config_digest = _blob(root, next(layer for layer in layers if layer.get("name") == "config.json"))
    config_bytes = config_path.read_bytes()
    if sha256(config_bytes).hexdigest() != config_digest:
        raise Hold("Qwen configuration differs from its blob digest")
    config = json.loads(config_bytes)
    if config["model_type"] != "qwen3_5" or config["quantization_config"]["quant_method"] != "modelopt":
        raise Hold("unsupported Qwen checkpoint architecture or quantization")
    entries = {}
    for layer in layers:
        if not layer["mediaType"].endswith(".tensor"):
            continue
        path, _ = _blob(root, layer)
        base, tensors = parse_safetensors_header(path)
        for name, info in tensors.items():
            if name in entries:
                raise Hold("duplicate tensor in Qwen checkpoint: " + name)
            entries[name] = QwenWeightSource(name, path, base + info.data_start, info.nbytes,
                                             info.dtype, info.shape, info.shape)
    return config, config_bytes, entries


def _parameter_sources(expected, tensors):
    sources = {}
    for name, parameter in expected:
        weight = tensors.pop(name, None)
        if weight is None:
            raise Hold("missing Qwen parameter: " + name)
        dims = parameter.shape
        if weight.dtype == "U32":
            if len(dims) != 2 or dims[1] % 16 or weight.shape != (dims[0], dims[1] // 8):
                raise Hold("invalid packed Qwen dimensions: " + name)
            scales = tensors.pop(name + ".scale", None)
            factor = tensors.pop(name + ".global_scale", None)
            if scales is None or (scales.dtype, scales.shape) != ("U8", (dims[0], dims[1] // 16)) or factor is None:
                raise Hold("incomplete Qwen NVFP4 parameter: " + name)
            factor.global_scale()
            sources[name] = (weight, scales, factor)
        else:
            if (weight.dtype, weight.shape) != ("BF16", dims):
                raise Hold("invalid dense Qwen dimensions or dtype: " + name)
            sources[name] = (weight,)
    if tensors:
        raise Hold("unexpected parameters remain in the Qwen text checkpoint")
    return sources


def build_qwen_text_plan(root: Path, *, model: str, model_digest: str) -> QwenTextPlan:
    """Resolve every text parameter into a checked weight source and memory budget."""
    if (model, model_digest) != (QWEN_MODEL, QWEN_MANIFEST_SHA256):
        raise Hold("Qwen model identity is not reviewed")
    config, config_json, entries = _read_catalog(Path(root), model_digest)
    try:
        if version("mlx") != MLX_VERSION or version("mlx-lm") != MLX_LM_VERSION:
            raise Hold("wired Qwen text layout requires its pinned MLX and MLX-LM")
        from mlx.utils import tree_flatten
        from mlx_lm.models.qwen3_5 import Model, ModelArgs
    except (ImportError, PackageNotFoundError) as exc:
        raise Hold("optional Qwen text runtime is unavailable") from exc
    text = Model(ModelArgs.from_dict(config))
    block_types = tuple("linear_attention" if layer.is_linear else "full_attention" for layer in text.layers)
    if block_types != tuple(config["text_config"]["layer_types"]):
        raise Hold("Qwen attention layer order differs from its configuration")
    sanitized = text.sanitize(entries.copy())
    omitted = entries.keys() - {tensor.name for tensor in sanitized.values()}
    if any(not name.startswith(("model.visual.", "mtp.")) for name in omitted):
        raise Hold("Qwen text layout omitted a required tensor")
    sources = _parameter_sources(tree_flatten(text.parameters()), sanitized)
    sizes = [source.nbytes for parts in sources.values() for source in parts]
    wired = sum((n + PAGE_BYTES - 1) // PAGE_BYTES * PAGE_BYTES for n in sizes if n >= PAGE_BYTES)
    subpage = sum(n for n in sizes if n < PAGE_BYTES)
    return QwenTextPlan(model_digest, config_json, MappingProxyType(sources), block_types, wired, subpage)
