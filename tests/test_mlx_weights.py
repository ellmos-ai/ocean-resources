# SPDX-License-Identifier: MIT
from types import SimpleNamespace

import pytest

from ocean_resources import Hold, WiredWeightLease
from ocean_resources import mlx_weights


@pytest.fixture
def mlx_lease_fixture(monkeypatch):
    events = []
    references = {}
    class Runner:
        def borrow_weight(self, name):
            index = len(references) + 1
            return WiredWeightLease(name, index, 4)

        def release_weight(self, lease):
            events.append(lease.tensor_name)

    def bind(address, *_):
        proof = SimpleNamespace(released=False)
        references[address] = proof
        return object(), proof

    native = SimpleNamespace(bind=bind)
    runtime = SimpleNamespace(__version__="0.32.0", synchronize=lambda: events.append("sync"))
    modules = {"mlx.core": runtime, "_ocean_mlx_wired": native}
    monkeypatch.setattr(mlx_weights, "import_module", modules.__getitem__)
    return mlx_weights.MLXWeightBindings(Runner()), references, events, native


def test_drain_waits_for_each_native_reference(mlx_lease_fixture):
    owner, references, events, _ = mlx_lease_fixture
    owner.array("first", [1], object())
    owner.array("second", [1], object())
    references[2].released = True
    with pytest.raises(Hold, match="active or unconfirmed"):
        owner.drain()
    assert events == ["sync", "second"]
    with pytest.raises(Hold, match="draining"):
        owner.array("third", [1], object())
    references[1].released = True
    owner.drain()
    owner.drain()
    assert events == ["sync", "second", "sync", "first", "sync"]


def test_ambiguous_native_failure_retains_its_lease(mlx_lease_fixture):
    owner, _, events, native = mlx_lease_fixture
    def unavailable(*_):
        raise RuntimeError("native bind failed")
    native.bind = unavailable
    with pytest.raises(RuntimeError, match="native bind failed"):
        owner.array("first", [1], object())
    with pytest.raises(Hold, match="active or unconfirmed"):
        owner.drain()
    assert events == ["sync"]
