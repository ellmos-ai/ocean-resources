# SPDX-License-Identifier: MIT
import struct
from types import SimpleNamespace

import pytest

from ocean_resources import Hold
from ocean_resources.mlx_qwen import (
    MANIFEST_NAME, QWEN_MANIFEST_SHA256, QWEN_MODEL, QwenWeightSource,
    _parameter_sources, build_qwen_text_plan,
)


def test_qwen_layout_rejects_unreviewed_identity_and_modified_manifest(tmp_path):
    with pytest.raises(Hold, match="identity is not reviewed"):
        build_qwen_text_plan(tmp_path, model=QWEN_MODEL, model_digest="0" * 64)
    manifest = tmp_path / MANIFEST_NAME
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b"{}")
    with pytest.raises(Hold, match="manifest differs"):
        build_qwen_text_plan(tmp_path, model=QWEN_MODEL, model_digest=QWEN_MANIFEST_SHA256)


@pytest.mark.parametrize("factor_value", [0.25, float("nan"), -1.0])
def test_qwen_nvfp4_plan_keeps_the_packed_weight_and_both_scales(tmp_path, factor_value):
    blob = tmp_path / "fixture"
    blob.write_bytes(struct.pack("<f", factor_value))
    name = "projection.weight"
    packed = QwenWeightSource(name, blob, 0, 8, "U32", (1, 2), (1, 2))
    scales = QwenWeightSource(name + ".scale", blob, 0, 1, "U8", (1, 1), (1, 1))
    factor = QwenWeightSource(name + ".global_scale", blob, 0, 4, "F32", (), ())
    params = [(name, SimpleNamespace(shape=(1, 16)))]
    entries = {item.name: item for item in (packed, scales, factor)}
    if factor_value == 0.25:
        assert _parameter_sources(params, entries) == {name: (packed, scales, factor)}
    else:
        with pytest.raises(Hold, match="invalid Qwen global scale"):
            _parameter_sources(params, entries)
