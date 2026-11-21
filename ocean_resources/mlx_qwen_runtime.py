# SPDX-License-Identifier: MIT
"""Load and execute the planned Qwen text model with retained MLX weight leases."""
from collections import defaultdict
from hashlib import sha256
from importlib.metadata import version
import json
import os
from threading import Lock

from .contracts import Hold, positive
from .mlx_qwen import MLX_LM_VERSION, PAGE_BYTES, QwenTextPlan
from .mlx_weights import MLXWeightBindings


def _hash_range(stream, length, blob):
    digest = sha256()
    while length:
        chunk = stream.read(min(length, 8 * 1024 * 1024))
        if not chunk:
            raise Hold("incomplete Qwen weight blob")
        blob.update(chunk)
        digest.update(chunk)
        length -= len(chunk)
    return digest.hexdigest()


def _verified_blob_sources(path, sources):
    """Authenticate the exact bytes used to obtain each tensor range digest."""
    wired, small = {}, {}
    blob = sha256()
    with path.open("rb") as stream:
        for source in sorted(sources, key=lambda item: item.offset):
            gap = source.offset - stream.tell()
            if gap < 0:
                raise Hold("overlapping Qwen weight ranges")
            _hash_range(stream, gap, blob)
            if source.nbytes >= PAGE_BYTES:
                wired[source.name] = _hash_range(stream, source.nbytes, blob)
            else:
                data = stream.read(source.nbytes)
                if len(data) != source.nbytes:
                    raise Hold("incomplete Qwen subpage tensor")
                blob.update(data)
                small[source.name] = data
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            blob.update(chunk)
    if path.name != "sha256-" + blob.hexdigest():
        raise Hold("Qwen weight blob digest changed")
    return wired, small


class QwenTextRuntime:
    """One pinned in-process model. Native grant, capacity and retirement are caller-owned."""

    def __init__(self, plan: QwenTextPlan, runner, *, context_tokens: int):
        if not isinstance(plan, QwenTextPlan) or not positive(context_tokens):
            raise ValueError("reviewed Qwen text plan and context limit are required")
        if runner.page_size != PAGE_BYTES:
            raise Hold("Qwen load plan page size differs from the native runner")
        if version("mlx-lm") != MLX_LM_VERSION:
            raise Hold("Qwen execution requires its pinned MLX-LM")
        import mlx.core as mx
        import mlx.nn as nn
        from mlx.utils import tree_unflatten
        from mlx_lm.models.qwen3_5 import Model, ModelArgs
        import numpy as np

        config = json.loads(plan.config_json)
        text = config["text_config"]
        if context_tokens > text["max_position_embeddings"]:
            raise Hold("Qwen context exceeds the checkpoint configuration")
        self._pid = os.getpid()
        self._plan = plan
        self._runner = runner
        self._context_tokens = context_tokens
        self._vocab_size = text["vocab_size"]
        self._mx, self._np, self._nn = mx, np, nn
        self._unflatten = tree_unflatten
        self._model = Model(ModelArgs.from_dict(config))
        self._bindings = MLXWeightBindings(runner)
        self._started = self._ready = self._draining = False
        self._lock = Lock()

    def _require_process(self):
        if self._pid != os.getpid():
            raise Hold("foreign Qwen text runtime process")

    def _subpage_array(self, source, data, dtype):
        storage_dtype = {"BF16": "<u2", "F32": "<f4", "U32": "<u4", "U8": "u1"}[source.dtype]
        array = self._mx.array(self._np.frombuffer(data, dtype=storage_dtype).copy())
        return array.view(dtype).reshape(source.raw_shape)

    def _load_arrays(self, check_owned):
        mx = self._mx
        dtypes = {"BF16": mx.bfloat16, "F32": mx.float32, "U32": mx.uint32, "U8": mx.uint8}
        files = defaultdict(list)
        for parts in self._plan.parameters.values():
            for source in parts:
                files[source.path].append(source)
        values = {}
        for path, sources in files.items():
            check_owned()
            wired, small = _verified_blob_sources(path, sources)
            check_owned()
            if wired:
                regions = self._runner.load_safetensors(path, tensor_names=tuple(wired))
                if {region.tensor_name: region.weights_sha256 for region in regions} != wired:
                    raise Hold("Qwen wired bytes differ from the authenticated blob")
            for source in sources:
                dtype = dtypes[source.dtype]
                if source.name in wired:
                    check_owned()
                    self._runner.verify_gpu_weight_read(source.name)
                    value = self._bindings.array(source.name, source.raw_shape, dtype)
                else:
                    value = self._subpage_array(source, small[source.name], dtype)
                if source.transform == "conv_channels_last":
                    value = value.moveaxis(2, 1)
                elif source.transform == "rmsnorm_add_one":
                    value = value + mx.array(1, dtype=value.dtype)
                values[source.name] = value
        return values

    def load(self, check_owned):
        """Load one native epoch under a caller-provided live grant check."""
        self._require_process()
        with self._lock:
            if self._started or self._draining:
                raise Hold("Qwen weight loading already started or is draining")
            check_owned()
            self._started = True
            values = self._load_arrays(check_owned)
            mx, nn = self._mx, self._nn

            class NVFP4Linear(nn.Module):
                def __init__(self, weight, scales, factor):
                    super().__init__()
                    self.weight, self.scales, self.factor = weight, scales, factor

                def __call__(self, x):
                    output = mx.quantized_matmul(x, self.weight, self.scales,
                                                 group_size=16, bits=4, mode="nvfp4")
                    return (output * self.factor).astype(output.dtype)

            dense, projections = [], []
            for name, sources in self._plan.parameters.items():
                arrays = [values[part.name] for part in sources]
                if len(arrays) == 1:
                    dense.append((name, arrays[0]))
                else:
                    projections.append((name.removesuffix(".weight"), NVFP4Linear(*arrays)))
            self._model.update(self._unflatten(dense))
            if projections:
                self._model.update_modules(self._unflatten(projections))
            self._model.eval()
            check_owned()
            self._ready = True

    def generate(self, prompt, max_new_tokens, check_owned, *, stop_tokens=()):
        """Execute one bounded greedy request with its own cache."""
        self._require_process()
        with self._lock:
            if not self._ready or self._draining:
                raise Hold("Qwen text runtime is not available for inference")
            if (not isinstance(prompt, (list, tuple)) or not prompt or not positive(max_new_tokens)
                    or len(prompt) + max_new_tokens > self._context_tokens
                    or any(type(t) is not int or not 0 <= t < self._vocab_size for t in prompt)):
                raise ValueError("prompt and generation exceed the reviewed Qwen context")
            mx = self._mx
            tokens = mx.array([prompt], dtype=mx.int32)
            cache = self._model.make_cache()
            emitted = []
            for _ in range(max_new_tokens):
                check_owned()
                with mx.stream(mx.gpu):
                    logits = self._model(tokens, cache=cache)
                    token = int(mx.argmax(logits[:, -1, :], axis=-1).item())
                check_owned()
                if token in stop_tokens:
                    break
                emitted.append(token)
                tokens = mx.array([[token]], dtype=mx.int32)
            return tuple(emitted)

    def drain(self):
        """Drop model references and return native leases before port-owned retirement."""
        self._require_process()
        with self._lock:
            self._draining = True
            self._model = None
            self._bindings.drain()
