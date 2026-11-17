"""Private configuration -> authority -> HTTP client -> model turns -> return."""
import asyncio
import hashlib
import json
import platform
import time
from dataclasses import asdict
from threading import Thread

import pytest

from ocean_resources import CapacityReadback, Hold
from ocean_resources.configuration import ConfiguredHost, private_bytes, read_client
from ocean_resources.model_client import model_turn
from ocean_resources.model_executor import ModelSpec
from ocean_resources.profiles import native_architecture


def private(path, value):
    path.write_text(json.dumps(value) if not isinstance(value, str) else value)
    path.chmod(0o600)
    return path


@pytest.fixture
def configured(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    root.chmod(0o700)
    now = time.time()
    spec = ModelSpec("fixture:1", "a" * 64, 4096)
    host_id = platform.node()
    evidence = {"schema": "ocean-model-measurement.v1", "scope": "ollama-model-api",
        "method": "ollama-ps-and-native-cpu-v1", "host_id": host_id, "platform": platform.system(),
        "architecture": native_architecture(), "workload_sha256": spec.fingerprint(), "model_digest": spec.digest,
        "model_spec": asdict(spec), "peak_resident_bytes": 20, "peak_cpu_units": .6, "cpu_concurrency_units": 1,
        "sample_count": 3, "sample_interval_seconds": 1,
        "started_at": now-10, "finished_at": now-6, "unload_ack_verified": True,
        "model_absence_verified": True, "server_version": "0.33.3", "unmeasured_external_residency": False}
    ep = private(root / "evidence.json", evidence)
    profile = {"schema": "ocean-reviewed-workload-profile.v1", "kind": "model-api", "profile_id": "p",
        "socket_id": "model", "host_id": host_id, "platform": platform.system(),
        "architecture": native_architecture(), "workload_sha256": spec.fingerprint(), "ram_bytes": 30,
        "cpu_units": 1, "ram_margin_bytes": 10, "approved_by": "fixture-reviewer", "approved_at": now-5,
        "expires_at": now+3600, "evidence_sha256": hashlib.sha256(ep.read_bytes()).hexdigest()}
    pp = private(root / "profile.json", profile)
    tokens = [private(root / (name + ".token"), char * 32) for name, char in (("operator", "o"), ("bach", "b"))]
    clients = [{"client_id": name, "sockets": ["model"], "token_file": str(tokens[i]),
                "operator": i == 0, "allow_interactive": True, "priority_floor": 1, "require_leases": True,
                "endpoint_file": str(root / (name + ".json"))} for i, name in enumerate(("operator", "bach"))]
    config = {"schema": "ocean-host-config.v1", "host_id": host_id, "state_directory": str(root), "port": 0,
        "capacity": {"reserve_ram_bytes": 1, "reserve_cpu_units": 1, "reserve_disk_bytes": 1, "directory": str(root)},
        "profiles": [{"profile_path": str(pp), "profile_sha256": hashlib.sha256(pp.read_bytes()).hexdigest(), "evidence_path": str(ep)}],
        "profile_max_age_seconds": 3600,
        "sockets": [{"socket_id": "model", "host_id": host_id, "ram_bytes": 30, "cpu_units": 1,
                     "profile_id": "p", "workload_sha256": spec.fingerprint()}],
        "clients": clients, "executors": [{"socket_id": "model", "kind": "model-api", "model": asdict(spec)}],
        "focus": {"idle_seconds": .5, "interactive_slice_seconds": 120, "background_min_seconds": 10}}
    cp = private(root / "host.json", config)
    sequence = [0]
    def sensor(*args):
        def read():
            sequence[0] += 1
            return CapacityReadback(host_id, sequence[0], time.monotonic(), 100, 4, True, True)
        return read
    monkeypatch.setattr("ocean_resources.configuration.NativeCapacitySensor", sensor)
    class Port:
        def __init__(self):
            self.calls = []
        def verify_available(self, actual):
            assert actual == spec
        def unload(self, actual, owned):
            owned()
            self.calls.append("unload")
        def resident_bytes(self, actual):
            return 0
    port = Port()
    obj = ConfiguredHost(cp, hashlib.sha256(cp.read_bytes()).hexdigest(), model_port_factory=lambda: port)
    thread = Thread(target=obj.serve_forever)
    thread.start()
    try:
        yield obj, root, port, cp
    finally:
        obj.service.close()
        thread.join(5)
        if obj.arbiter.status()["torch"] is None:
            obj.close()
        else:
            # Failed tests retain the durable journal; this fixture owns no model/process.
            obj.writer.__exit__(None, None, None)


def test_product_client_turn_has_own_liveness_and_returns_after_idle(configured):
    _obj, root, port, _ = configured
    client, models = read_client(root / "bach.json")
    assert models[0]["model"] == "fixture:1"
    async def scenario():
        async with model_turn(client, "model", "chat", "foreground", ttl_seconds=3):
            state = await asyncio.to_thread(client.status)
            assert state["in_flight_turns"] == 1 and state["torch"]["socket_id"] == "model"
            await asyncio.sleep(.25)
            assert port.calls == []  # A live turn survives idle and no client-side unload occurs.
        for _ in range(100):
            if (await asyncio.to_thread(client.status))["torch"] is None:
                break
            await asyncio.sleep(.02)
        assert (await asyncio.to_thread(client.status))["torch"] is None
    asyncio.run(scenario())
    assert port.calls == ["unload"]


def test_client_credentials_remain_private_and_writer_cannot_duplicate(configured):
    _obj, root, port, cp = configured
    assert "b" * 32 not in (root / "bach.json").read_text()
    with pytest.raises(Hold):
        ConfiguredHost(cp, hashlib.sha256(cp.read_bytes()).hexdigest(), model_port_factory=lambda: port)
    (root / "bach.json").chmod(0o644)
    with pytest.raises(Hold):
        private_bytes(root / "bach.json")


def test_client_configuration_is_bound_to_host_writer_session(configured):
    _obj, root, _port, _cp = configured
    data = json.loads((root / "bach.json").read_text())
    data["authority_id"] = "old-writer"
    private(root / "old.json", data)
    client, _ = read_client(root / "old.json")
    with pytest.raises(Hold):
        client.status()
