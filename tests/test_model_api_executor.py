import hashlib
import json
import platform
from dataclasses import asdict

import pytest

from ocean_resources import (
    BACHFacade,
    CapacityReadback,
    Hold,
    HostLocalArbiter,
    HostWriterAuthority,
    ResourceSocket,
    SlotRequest,
)
from ocean_resources.model_executor import ModelAPIExecutor, ModelSpec
from ocean_resources.profiles import (
    ProfileSource,
    ReviewedProfileGate,
    native_architecture,
)


class Port:
    def __init__(self):
        self.resident = 0
        self.unloads = []
        self.unknown = False
    def verify_available(self, spec):
        if self.unknown:
            raise Hold("unknown model API")
    def unload(self, spec, check):
        check()
        if self.unknown:
            raise Hold("unload not confirmed")
        self.unloads.append(spec.model)
        self.resident = 0
    def resident_bytes(self, spec):
        if self.unknown:
            raise Hold("unknown model residency")
        return self.resident


@pytest.fixture
def model_host(tmp_path):
    now = [100.0]
    seq = [0]
    def sensor():
        seq[0] += 1
        return CapacityReadback("host", seq[0], now[0], 100, 4, True, True)
    spec = ModelSpec("example:1", "a" * 64, 4096)
    with HostWriterAuthority(tmp_path.resolve(), "host") as writer:
        arbiter = HostLocalArbiter(writer, (ResourceSocket("model", "host", 20, 1, "fixture", spec.fingerprint()),),
                                   sensor, lambda: now[0], fixture_profiles=frozenset({"fixture"}))
        port = Port()
        executor = ModelAPIExecutor(arbiter, spec, port, tmp_path.resolve())
        facade = BACHFacade(arbiter, {"model": executor.adapter()})
        arbiter.submit(SlotRequest("job", "model", "slot", "host", "key", interactive=True))
        yield facade, arbiter, executor, port, now, writer


def test_model_admission_does_not_load_or_bypass_cold_start(model_host):
    facade, _arbiter, _executor, port, _now, writer = model_host
    grant = facade.start_next()
    assert port.resident == 0 and port.unloads == []
    evidence = writer.unresolved()["execution_evidence"]
    assert evidence["schema"] == "ocean-model-execution.v1"
    assert evidence["grant"] == asdict(grant)
    assert evidence["phase"] == "admitted"


def test_unknown_unload_retains_grant_and_checkpoint_then_retries(model_host):
    facade, arbiter, _executor, port, _now, writer = model_host
    grant = facade.start_next()
    port.resident = 20
    port.unknown = True
    with pytest.raises(Hold):
        facade.yield_active(grant, requeue=False)
    assert arbiter.status()["torch"] == asdict(grant)
    assert writer.unresolved()["execution_evidence"]["phase"] == "checkpointed"
    port.unknown = False
    facade.yield_active(grant, requeue=False)
    assert arbiter.status()["torch"] is None and writer.unresolved() is None


def test_wrong_model_workload_cannot_start(model_host):
    _facade, arbiter, executor, port, _now, _writer = model_host
    bad = ModelAPIExecutor(arbiter, ModelSpec("other:1", "b" * 64, 4096), port, executor.directory)
    with pytest.raises(Hold):
        BACHFacade(arbiter, {"model": bad.adapter()}).start_next()
    assert port.unloads == []


def test_missing_durable_model_identity_blocks_unload(model_host, monkeypatch):
    facade, arbiter, executor, port, _now, writer = model_host
    grant = facade.start_next()
    execution = arbiter.status()["execution_identity"]
    checkpoint = executor.checkpoint(grant, execution)
    monkeypatch.setattr(writer, "unresolved", lambda: None)
    with pytest.raises(Hold):
        executor.stop(grant, execution, checkpoint)
    assert port.unloads == []


def test_pinned_model_profile_requires_native_specific_evidence(tmp_path):
    spec = ModelSpec("example:1", "a" * 64, 4096)
    evidence = {"schema": "ocean-model-measurement.v1", "scope": "ollama-model-api",
        "method": "ollama-ps-and-native-cpu-v1", "host_id": platform.node(), "platform": platform.system(),
        "architecture": native_architecture(), "workload_sha256": spec.fingerprint(), "model_digest": spec.digest,
        "model_spec": asdict(spec), "peak_resident_bytes": 20, "peak_cpu_units": .6, "cpu_concurrency_units": 1,
        "sample_count": 3, "sample_interval_seconds": 1,
        "started_at": 90, "finished_at": 94, "unload_ack_verified": True,
        "model_absence_verified": True, "server_version": "0.33.3", "unmeasured_external_residency": False}
    profile = {"schema": "ocean-reviewed-workload-profile.v1", "kind": "model-api", "profile_id": "p",
        "socket_id": "model", "host_id": platform.node(), "platform": platform.system(),
        "architecture": native_architecture(), "workload_sha256": spec.fingerprint(), "ram_bytes": 30,
        "cpu_units": 1, "ram_margin_bytes": 10, "approved_by": "reviewer", "approved_at": 95, "expires_at": 200}
    ep, pp = tmp_path.resolve() / "evidence.json", tmp_path.resolve() / "profile.json"
    def pin():
        ep.write_text(json.dumps(evidence))
        profile["evidence_sha256"] = hashlib.sha256(ep.read_bytes()).hexdigest()
        pp.write_text(json.dumps(profile))
        return ProfileSource(pp, hashlib.sha256(pp.read_bytes()).hexdigest(), ep)
    gate = ReviewedProfileGate((pin(),), max_age_seconds=20, wall_clock=lambda: 100)
    socket = ResourceSocket("model", platform.node(), 30, 1, "p", spec.fingerprint())
    assert gate.require(socket)["kind"] == "model-api"
    evidence["model_absence_verified"] = False
    with pytest.raises(ValueError):
        ReviewedProfileGate((pin(),), max_age_seconds=20, wall_clock=lambda: 100)
    with pytest.raises(Hold):
        gate.require(socket)
    evidence["model_absence_verified"] = True
    evidence["peak_cpu_units"] = 2.1
    with pytest.raises(ValueError):
        ReviewedProfileGate((pin(),), max_age_seconds=20, wall_clock=lambda: 100)
