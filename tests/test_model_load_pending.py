# SPDX-License-Identifier: MIT
"""No load call or process signal: native observations are explicit fixtures."""
import hashlib
import json
import platform
from dataclasses import asdict, replace

import pytest

from ocean_resources import (
    BACHFacade,
    CapacityReadback,
    ControllerClient,
    Grant,
    Hold,
    HostController,
    HostLocalArbiter,
    HostWriterAuthority,
    ResourceSocket,
    SlotRequest,
)
from ocean_resources.model_executor import ModelAPIExecutor, ModelSpec
from ocean_resources.model_loading import (
    LoadObservation,
    ModelLoadCapacity,
    ProcessIdentity,
    ResidentCredit,
)
from ocean_resources.profiles import (
    ProfileSource,
    ReviewedProfileGate,
    native_architecture,
)

GIB = 1024**3
POLICY = "e" * 64


@pytest.fixture
def pending_host(tmp_path):
    now, sequence, free = [100.0], [0], [17 * GIB]
    host = platform.node()
    spec = ModelSpec("fixture:27b", "a" * 64, 16384)
    evidence = {"schema": "ocean-model-measurement.v2", "scope": "ollama-model-api",
        "method": "ollama-ps-native-rss-and-cpu-v2", "host_id": host, "platform": platform.system(),
        "architecture": native_architecture(), "workload_sha256": spec.fingerprint(),
        "model_digest": spec.digest, "model_spec": asdict(spec), "peak_resident_bytes": 19 * GIB,
        "peak_process_rss_bytes": 19 * GIB, "process_identity_verified": True,
        "measured_processes": [{"pid": 4321, "created_at": 10.0, "boot_time": 1.0}],
        "peak_cpu_units": .6, "cpu_concurrency_units": 1, "sample_count": 3,
        "sample_interval_seconds": 1, "started_at": 90, "finished_at": 94,
        "unload_ack_verified": True, "model_absence_verified": True, "server_version": "0.33.3",
        "unmeasured_external_residency": False}
    ep, pp = tmp_path / "evidence.json", tmp_path / "profile.json"
    ep.write_text(json.dumps(evidence))
    profile = {"schema": "ocean-reviewed-workload-profile.v1", "kind": "model-api", "profile_id": "p",
        "socket_id": "model", "host_id": host, "platform": platform.system(),
        "architecture": native_architecture(), "workload_sha256": spec.fingerprint(),
        "ram_bytes": 20 * GIB, "cpu_units": 1, "ram_margin_bytes": GIB, "approved_by": "fixture-reviewer",
        "approved_at": 95, "expires_at": 1000, "evidence_sha256": hashlib.sha256(ep.read_bytes()).hexdigest()}
    pp.write_text(json.dumps(profile))
    gate = ReviewedProfileGate((ProfileSource(pp.resolve(), hashlib.sha256(pp.read_bytes()).hexdigest(), ep.resolve()),),
        max_age_seconds=1000, wall_clock=lambda: 100)
    class Sensor:
        pressure = 1
        def __call__(self):
            sequence[0] += 1
            return CapacityReadback(host, sequence[0], now[0], free[0] - 2 * GIB, 4, self.pressure == 1, True)
        def model_load_readback(self):
            return ModelLoadCapacity(self(), free[0], 2 * GIB, self.pressure)
    sensor = Sensor()
    class Port:
        supports_load_pending = True
        resident = 0
        rss = 19 * GIB
        pressure = 1
        swap = 0
        unknown = False
        def __init__(self):
            self.calls = []
        credit = True
        context = 16384
        identity = ProcessIdentity(4321, 10.0, 1.0)
        def verify_available(self, actual):
            assert actual == spec
        def load_observation(self, actual, policy, *, loaded, expected_process=None, capacity_reader=None, accept=None):
            assert actual == spec and policy == POLICY
            if self.unknown:
                raise Hold("fixture unknown native effect")
            cap = capacity_reader()
            credit = (ResidentCredit(19 * GIB, spec.fingerprint(), self.identity, cap.reading.sequence, now[0])
                      if loaded and self.credit else None)
            result = LoadObservation(spec.fingerprint(), POLICY, self.identity, now[0], free[0],
                self.pressure, self.swap, self.resident, self.rss if loaded else GIB // 10,
                self.context if self.resident else None, GIB + GIB // 2, cap, credit)
            if accept is not None:
                accept(result)
            return result
        def unload(self, actual, owned):
            owned()
            if self.unknown:
                raise Hold("fixture unknown unload")
            self.calls.append("guarded-unload")
            self.resident = 0
        def abort_load(self, actual, expected, owned, *, accept=None):
            assert expected == asdict(self.identity)
            self.unload(actual, owned)
            if accept is not None:
                accept()
        def resident_bytes(self, actual):
            if self.unknown:
                raise Hold("fixture unknown residency")
            return self.resident
    with HostWriterAuthority(tmp_path.resolve(), host) as writer:
        arbiter = HostLocalArbiter(writer, (ResourceSocket("model", host, 20 * GIB, 1, "p", spec.fingerprint()),),
            sensor, lambda: now[0], profile_gate=gate, heartbeat_ttl=30)
        port = Port()
        executor = ModelAPIExecutor(arbiter, spec, port, tmp_path.resolve(), load_policy_sha256=POLICY)
        client = ControllerClient("bach", frozenset({"model"}), allow_interactive=True,
            require_leases=True, allow_model_load=True)
        operator = ControllerClient("operator", frozenset({"model"}), operator=True)
        ctl = HostController(BACHFacade(arbiter, {"model": executor.adapter()}), (client, operator))
        yield ctl, client, operator, arbiter, port, free, now, writer, sensor


def start(ctx, *, pending=True):
    ctl, client, operator, arbiter, *_ = ctx
    request = SlotRequest("load", "model", "bach-model", arbiter.authority.host_id, "load", interactive=True)
    method = ctl.submit_model_load if pending else ctl.submit_leased
    lease = method(client.client_id, client.token, request, 900)
    status = ctl.tick(operator.client_id, operator.token)
    return lease, status


def test_full_profile_does_not_fit_but_explicit_load_is_exclusive(pending_host):
    ctl, client, _operator, _arbiter, port, free, _now, writer, _ = pending_host
    _lease, status = start(pending_host)
    assert status["state"] == "LOAD_PENDING"
    grant = Grant(**status["torch"])
    assert writer.unresolved()["load_state"]["phase"] == "PREPARED"
    assert writer.unresolved()["load_state"]["profile_ram_bytes"] == 20 * GIB
    with pytest.raises(Hold):
        ctl.begin_turn(client.client_id, client.token, grant, "ordinary")
    permit = ctl.begin_model_load(client.client_id, client.token, grant, "attempt")
    assert writer.unresolved()["load_state"]["phase"] == "MAY_LOAD"
    assert permit["policy_sha256"] == POLICY
    assert port.calls == [] and port.resident == 0
    port.resident, free[0] = 19 * GIB, 4 * GIB
    result = ctl.complete_model_load(client.client_id, client.token, grant, "attempt", True)
    assert result["state"] == "RUNNING"
    assert writer.unresolved()["load_state"]["phase"] == "VERIFIED"
    ctl.begin_turn(client.client_id, client.token, grant, "ordinary")
    ctl.end_turn(client.client_id, client.token, grant, "ordinary")


def test_plain_request_cannot_spend_cold_start_threshold(pending_host):
    _lease, status = start(pending_host, pending=False)
    assert status["state"] == "HOLD" and status["torch"] is None


@pytest.mark.parametrize("failure", ["reserve", "rss", "identity", "pressure", "swap", "unknown", "expired", "unattributed", "context", "api_peak"])
def test_no_running_or_release_on_incomplete_load_proof(pending_host, failure):
    ctl, client, operator, arbiter, port, free, now, writer, sensor = pending_host
    _lease, status = start(pending_host)
    grant = Grant(**status["torch"])
    ctl.begin_model_load(client.client_id, client.token, grant, "attempt")
    port.resident, free[0] = 19 * GIB, 4 * GIB
    if failure == "reserve":
        free[0] = 3 * GIB
    if failure == "rss":
        port.rss = 21 * GIB
    if failure == "identity":
        port.identity = replace(port.identity, created_at=11.0)
    if failure == "pressure":
        sensor.pressure = port.pressure = 2
    if failure == "swap":
        port.swap = 9 * GIB
    if failure == "unknown":
        port.unknown = True
    if failure == "expired":
        now[0] = 1100
    if failure == "unattributed":
        port.credit = False
    if failure == "context":
        port.context = 32768
    if failure == "api_peak":
        port.resident = 23 * GIB
    with pytest.raises(Hold):
        ctl.complete_model_load(client.client_id, client.token, grant, "attempt", True)
    assert arbiter.status()["torch"] == asdict(grant)
    assert writer.unresolved()["load_state"]["phase"] == "RECOVERY_HOLD"
    assert port.calls == []
    assert ctl.tick(operator.client_id, operator.token)["state"] == "RECOVERY_HOLD"


def test_expiry_never_reclaims_an_unconfirmed_possible_post(pending_host):
    ctl, client, operator, _arbiter, port, _free, now, writer, _ = pending_host
    _lease, status = start(pending_host)
    grant = Grant(**status["torch"])
    ctl.begin_model_load(client.client_id, client.token, grant, "attempt")
    now[0] += 901
    assert ctl.tick(operator.client_id, operator.token)["state"] == "RECOVERY_HOLD"
    assert port.calls == [] and writer.unresolved() is not None


def test_terminal_abort_fences_delayed_begin_without_a_native_post(pending_host):
    ctl, client, _operator, _arbiter, port, *_ = pending_host
    _lease, status = start(pending_host)
    grant = Grant(**status["torch"])
    ctl.complete_model_load(client.client_id, client.token, grant, "late", False)
    with pytest.raises(Hold):
        ctl.begin_model_load(client.client_id, client.token, grant, "late")
    assert port.calls == []


def test_missing_explicit_client_capability_cannot_request_load(pending_host):
    ctl, client, *_ = pending_host
    ctl._clients[client.client_id] = replace(client, allow_model_load=False)
    with pytest.raises(Hold):
        start(pending_host)


def test_two_live_load_requests_for_the_same_socket_do_not_race(pending_host):
    ctl, client, _operator, arbiter, *_ = pending_host
    start(pending_host)
    second = SlotRequest("other", "model", "bach-model", arbiter.authority.host_id, "other", interactive=True)
    with pytest.raises(Hold, match="another live model load"):
        ctl.submit_model_load(client.client_id, client.token, second, 900)
    assert {r["request_id"] for r in arbiter.status()["waiting"]} == set()


def test_native_provider_without_accounting_contract_cannot_reach_possible_post(pending_host):
    _ctl, _client, _operator, _arbiter, port, _free, _now, writer, _sensor = pending_host
    port.supports_load_pending = False
    _lease, status = start(pending_host)
    assert status["state"] == "HOLD" and status["torch"] is None
    assert writer.unresolved() is None and port.calls == []


def test_prepare_and_completion_exact_reserve_boundaries(pending_host):
    ctl, client, _operator, _arbiter, port, free, _now, _writer, _sensor = pending_host
    free[0] = 6 * GIB
    _lease, status = start(pending_host)
    assert status["state"] == "LOAD_PENDING"
    grant = Grant(**status["torch"])
    ctl.begin_model_load(client.client_id, client.token, grant, "attempt")
    port.resident = 19 * GIB
    free[0] = 2 * GIB + GIB + GIB // 2
    assert ctl.complete_model_load(client.client_id, client.token, grant, "attempt", True)["state"] == "RUNNING"


def test_one_byte_below_minimum_cannot_obtain_even_pending_grant(pending_host):
    _ctl, _client, _operator, _arbiter, port, free, _now, writer, _sensor = pending_host
    free[0] = 6 * GIB - 1
    _lease, state = start(pending_host)
    assert state["state"] == "HOLD" and state["torch"] is None
    assert writer.unresolved() is None and port.calls == []


def test_elevated_pressure_is_model_load_only_then_requires_normal_proof(pending_host):
    ctl, client, operator, _arbiter, port, free, _now, _writer, sensor = pending_host
    port.pressure = sensor.pressure = 2
    _lease, state = start(pending_host)
    assert state["state"] == "LOAD_PENDING"
    grant = Grant(**state["torch"])
    ctl.begin_model_load(client.client_id, client.token, grant, "attempt")
    port.resident, free[0] = 19 * GIB, 4 * GIB
    with pytest.raises(Hold):
        ctl.complete_model_load(client.client_id, client.token, grant, "attempt", True)
    assert ctl.tick(operator.client_id, operator.token)["state"] == "RECOVERY_HOLD"


def test_native_proof_ack_loss_retries_do_not_load_or_promote_twice(pending_host):
    ctl, client, _operator, _arbiter, port, free, _now, writer, _sensor = pending_host
    _lease, state = start(pending_host)
    grant = Grant(**state["torch"])
    permit = ctl.begin_model_load(client.client_id, client.token, grant, "attempt")
    # A retry after MAY_LOAD must return the exact intent even if now resident.
    port.resident, free[0] = 19 * GIB, 4 * GIB
    assert ctl.begin_model_load(client.client_id, client.token, grant, "attempt") == permit
    result = ctl.complete_model_load(client.client_id, client.token, grant, "attempt", True)
    saved = writer.unresolved()
    assert ctl.complete_model_load(client.client_id, client.token, grant, "attempt", True) == result
    assert writer.unresolved() == saved and port.calls == []


def test_successful_abort_uses_strict_port_then_native_release(pending_host, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    ctl, client, operator, arbiter, port, _free, _now, writer, _sensor = pending_host
    lease, state = start(pending_host)
    grant = Grant(**state["torch"])
    ctl.begin_model_load(client.client_id, client.token, grant, "attempt")
    port.resident = 19 * GIB
    assert ctl.complete_model_load(client.client_id, client.token, grant, "attempt", False)["state"] == "RETURN_REQUESTED"
    assert port.calls == ["guarded-unload"] and writer.unresolved() is not None
    released, acknowledge = Event(), Event()
    yield_active = ctl.facade.yield_active

    def pause_after_release(grant, *, requeue):
        checkpoint = yield_active(grant, requeue=requeue)
        released.set()
        assert acknowledge.wait(5)
        return checkpoint

    monkeypatch.setattr(ctl.facade, "yield_active", pause_after_release)
    with ThreadPoolExecutor(max_workers=1) as pool:
        tick = pool.submit(ctl.tick, operator.client_id, operator.token)
        try:
            assert released.wait(5)
            handoff = ctl.request_status(client.client_id, client.token, lease.request_id)
            assert handoff["state"] == "RETURN_REQUESTED"
            assert handoff["grant"] == asdict(grant) and handoff["model_load"] is None
        finally:
            acknowledge.set()
        assert tick.result(timeout=5)["state"] == "IDLE"

    finished = ctl.request_status(client.client_id, client.token, lease.request_id)
    assert finished["state"] == "FINISHED" and finished["grant"] is None
    assert arbiter.status()["torch"] is None and writer.unresolved() is None


def test_ambiguous_abort_retains_durable_possible_load(pending_host):
    ctl, client, operator, arbiter, port, _free, _now, writer, _sensor = pending_host
    _lease, state = start(pending_host)
    grant = Grant(**state["torch"])
    ctl.begin_model_load(client.client_id, client.token, grant, "attempt")
    port.unknown = True
    with pytest.raises(Hold):
        ctl.complete_model_load(client.client_id, client.token, grant, "attempt", False)
    assert ctl.tick(operator.client_id, operator.token)["state"] == "RECOVERY_HOLD"
    assert writer.unresolved() is not None and arbiter.status()["torch"] == asdict(grant)


@pytest.fixture
def pending_http(pending_host):
    from threading import Thread

    from ocean_resources.client import HostClient
    from ocean_resources.service import HostService
    ctl, client, operator, arbiter, *_ = pending_host
    service = HostService(ctl, operator, tick_seconds=.05)
    thread = Thread(target=service.serve_forever)
    thread.start()
    http = HostClient(service.port, arbiter.authority.host_id, arbiter.authority.authority_id,
                      client.client_id, client.token)
    try:
        yield pending_host, http
    finally:
        service.close()
        thread.join(5)
        assert not thread.is_alive()


def test_http_materialization_has_one_native_worker_and_independent_readbacks(pending_http, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import time

    ctx, client = pending_http
    _ctl, _native, _operator, arbiter, port, free, *_ = ctx
    entered, resume = Event(), Event()
    permits = []

    def materialize(actual, permit, **authority):
        assert permit["spec"]["digest"] == actual.digest
        assert authority["client_id"] == client.client_id
        assert authority["authority_id"] == client.authority_id
        assert authority["host_id"] == client.host_id
        permits.append(permit)
        entered.set()
        assert resume.wait(5)
        port.resident, free[0] = 19 * GIB, 4 * GIB

    monkeypatch.setattr(port, "materialize", materialize, raising=False)
    request = SlotRequest("native-materialize", "model", "bach-model", client.host_id, "idempotent", interactive=True)
    lease = client.submit_model_load(request, 60)
    deadline = time.monotonic() + 5
    while True:
        state = client.request_status(lease.request_id)
        if state["state"] == "LOAD_PENDING":
            break
        assert time.monotonic() < deadline
        time.sleep(.01)
    grant = Grant(**state["grant"])
    client.begin_model_load(grant, "materialize")
    short_client = replace(client, timeout_seconds=.25)
    with ThreadPoolExecutor(max_workers=1) as pool:
        original = pool.submit(short_client.materialize_model_load, grant, "materialize")
        try:
            assert entered.wait(5)
            with pytest.raises(Hold):
                original.result(timeout=5)
            assert client.materialize_model_load(grant, "materialize")["state"] == "LOADING"
            assert client.request_status(lease.request_id)["state"] == "LOAD_PENDING"
            with pytest.raises(Hold):
                client.complete_model_load(grant, "materialize", True)
        finally:
            resume.set()
    deadline = time.monotonic() + 5
    while client.materialize_model_load(grant, "materialize")["state"] != "READY":
        assert time.monotonic() < deadline
        time.sleep(.01)
    assert len(permits) == 1
    assert client.complete_model_load(grant, "materialize", True)["state"] == "RUNNING"
    assert client.finish(grant)["state"] == "IDLE"
    assert arbiter.status()["torch"] is None


def test_native_generation_reconciles_timeout_and_fences_ended_turn(pending_http, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from ocean_resources.model_client import model_turn
    from ocean_resources.model_load_client import model_load_turn

    ctx, client = pending_http
    _ctl, _native, _operator, arbiter, port, free, *_ = ctx
    entered, resume = Event(), Event()
    requests = []

    def generate(actual, prompt, maximum, ready, *, stop_tokens):
        ready()
        requests.append((prompt, maximum, stop_tokens))
        entered.set()
        assert resume.wait(6)
        ready()
        return (11, 17)

    monkeypatch.setattr(port, "generate", generate, raising=False)

    async def load():
        async with model_load_turn(client, "model", "bach-model", "foreground") as ticket:
            port.resident, free[0] = 19 * GIB, 4 * GIB
            ticket["successful"] = True
        async with model_turn(client, "model", "bach-model", "foreground") as ticket:
            return Grant(**ticket["grant"]), ticket["turn_id"]

    grant, ended_turn = asyncio.run(load())
    with pytest.raises(Hold):
        client.generate_model(grant, ended_turn, [1], 2)

    short = replace(client, timeout_seconds=.25)
    client.begin_turn(grant, "first")
    with ThreadPoolExecutor(max_workers=1) as pool:
        original = pool.submit(short.generate_model, grant, "first", [1], 2, stop_tokens=[3])
        try:
            assert entered.wait(5)
            with pytest.raises(Hold):
                original.result(timeout=5)
            assert client.generate_model(grant, "first", [1], 2, stop_tokens=[3])["state"] == "RUNNING"
            with pytest.raises(Hold):
                client.generate_model(grant, "first", [2], 2, stop_tokens=[3])
        finally:
            resume.set()
    import time

    deadline = time.monotonic() + 5
    while True:
        state = client.generate_model(grant, "first", [1], 2, stop_tokens=[3])
        if state["state"] == "COMPLETE":
            break
        assert time.monotonic() < deadline
        time.sleep(.01)
    assert state["tokens"] == [11, 17] and requests == [((1,), 2, (3,))]
    client.end_turn(grant, "first")

    entered.clear()
    resume.clear()
    client.begin_turn(grant, "cancelled")
    with ThreadPoolExecutor(max_workers=1) as pool:
        cancelled = pool.submit(client.generate_model, grant, "cancelled", [1], 2)
        try:
            assert entered.wait(5)
            client.end_turn(grant, "cancelled")
            with pytest.raises(Hold):
                client.finish(grant)
            with pytest.raises(Hold):
                client.generate_model(grant, "cancelled", [1], 2)
        finally:
            resume.set()
        with pytest.raises(Hold):
            cancelled.result(timeout=5)
    assert client.finish(grant)["state"] == "IDLE"
    assert arbiter.status()["torch"] is None


def test_real_http_staged_load_then_ordinary_turn_reuses_one_identity(pending_http):
    import asyncio

    from ocean_resources.model_client import model_turn
    from ocean_resources.model_load_client import model_load_turn
    ctx, client = pending_http
    ctl, _native, _operator, arbiter, port, free, *_ = ctx
    async def scenario():
        async with model_load_turn(client, "model", "bach-model", "foreground") as ticket:
            assert (await asyncio.to_thread(client.status))["state"] == "LOAD_PENDING"
            port.resident, free[0] = 19 * GIB, 4 * GIB
            ticket["successful"] = True  # This fixture owns the completed synthetic dispatch.
        assert (await asyncio.to_thread(client.status))["state"] == "RUNNING"
        async with model_turn(client, "model", "bach-model", "foreground"):
            assert len(ctl._owners) == 1
        await asyncio.to_thread(client.finish, ctl._active)
    asyncio.run(scenario())
    assert arbiter.status()["torch"] is None


@pytest.mark.parametrize("phase", ["submit", "begin"])
def test_cancelled_transport_thread_is_joined_before_owned_cleanup(pending_http, phase):
    import asyncio
    from threading import Event

    from ocean_resources.model_load_client import model_load_turn
    ctx, client = pending_http
    _ctl, _native, _operator, arbiter, port, *_ = ctx
    entered, release = Event(), Event()
    class Delayed:
        def __getattr__(self, name): return getattr(client, name)
        request_id = None
        def submit_model_load(self, request, ttl):
            self.request_id = request.request_id
            receipt = client.submit_model_load(request, ttl)
            if phase == "submit":
                entered.set()
                assert release.wait(5)
            return receipt
        def begin_model_load(self, grant, turn_id):
            receipt = client.begin_model_load(grant, turn_id)
            if phase == "begin":
                entered.set()
                assert release.wait(5)
            return receipt
    delayed = Delayed()
    async def scenario():
        async def worker():
            async with model_load_turn(delayed, "model", "bach-model", "foreground"):
                pytest.fail("cancelled load reached a model POST")
        task = asyncio.create_task(worker())
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(.05)
        task.cancel()
        assert not task.done()  # Retained transport cannot outlive its cleanup.
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            state = await asyncio.to_thread(client.request_status, delayed.request_id)
            if state["grant"] is None:
                break
            await asyncio.sleep(.02)
        assert state["grant"] is None and state["state"] in {"FINISHED", "CANCELLED"}
    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert arbiter.status()["torch"] is None and port.resident == 0


@pytest.mark.parametrize("phase", ["submit", "begin", "complete"])
def test_lost_response_is_reconciled_by_owned_request_and_attempt(pending_http, phase):
    import asyncio

    from ocean_resources.model_load_client import model_load_turn
    ctx, client = pending_http
    ctl, _native, _operator, arbiter, port, free, *_ = ctx
    class Lost:
        def __getattr__(self, name): return getattr(client, name)
        lost = False
        def invoke(self, name, *args):
            result = getattr(client, name)(*args)
            if name == phase + "_model_load" and not self.lost:
                self.lost = True
                raise Hold("fixture response lost after native acknowledgement")
            return result
        def submit_model_load(self, *args): return self.invoke("submit_model_load", *args)
        def begin_model_load(self, *args): return self.invoke("begin_model_load", *args)
        def complete_model_load(self, *args): return self.invoke("complete_model_load", *args)
    lost = Lost()
    async def run():
        async with model_load_turn(lost, "model", "bach-model", "foreground") as ticket:
            assert phase == "complete"
            port.resident, free[0] = 19 * GIB, 4 * GIB
            ticket["successful"] = True
        assert phase == "complete"
        await asyncio.to_thread(client.finish, ctl._active)
    if phase == "complete":
        asyncio.run(run())
    else:
        with pytest.raises(Hold):
            asyncio.run(run())
        import time
        for _ in range(100):
            if arbiter.status()["torch"] is None:
                break
            time.sleep(.02)
    assert lost.lost and arbiter.status()["torch"] is None


def test_failed_pre_post_probe_does_not_orphan_a_turn(pending_host):
    ctl, client, operator, arbiter, port, *_ = pending_host
    receipt, state = start(pending_host)
    grant = Grant(**state["torch"])
    port.unknown = True
    with pytest.raises(Hold):
        ctl.begin_model_load(client.client_id, client.token, grant, "no-post")
    assert ctl._turns == set() and arbiter.load_state(grant)["phase"] == "PREPARED"
    port.unknown = False
    ctl.cancel(client.client_id, client.token, receipt)
    assert ctl.tick(operator.client_id, operator.token)["state"] == "IDLE"
    assert arbiter.status()["torch"] is None and port.calls == []


def test_malformed_native_measurement_is_a_hold_without_a_failed_poll(pending_host):
    ctl, _client, _operator, _arbiter, port, *_ = pending_host
    original = port.load_observation
    def malformed(*args, **kwargs):
        return replace(original(*args, **kwargs), policy_sha256="invalid")
    port.load_observation = malformed
    _lease, state = start(pending_host)
    assert state["state"] == "HOLD" and state["torch"] is None
    assert ctl._transition is None and port.calls == []
