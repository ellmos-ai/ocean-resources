# SPDX-License-Identifier: MIT
from hashlib import sha256
from types import SimpleNamespace

import pytest

from ocean_resources import Hold
from ocean_resources.mlx_qwen_runtime import PAGE_BYTES, _verified_blob_sources


def test_qwen_authenticates_exact_tensor_ranges(tmp_path):
    wired = bytes(PAGE_BYTES)
    small = b"factor"
    contents = b"header" + wired + b"gap" + small + b"trailing"
    path = tmp_path / ("sha256-" + sha256(contents).hexdigest())
    path.write_bytes(contents)
    sources = (
        SimpleNamespace(name="factor", offset=9 + len(wired), nbytes=len(small)),
        SimpleNamespace(name="projection", offset=6, nbytes=len(wired)),
    )
    assert _verified_blob_sources(path, sources) == ({"projection": sha256(wired).hexdigest()}, {"factor": small})
    path.write_bytes(contents[:-1] + b"x")
    with pytest.raises(Hold, match="blob digest changed"):
        _verified_blob_sources(path, sources)
