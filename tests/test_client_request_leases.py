"""Client liveness is independent of the controller's own polling heartbeat."""
import pytest

from ocean_resources import (
    BACHFacade,
    CapacityReadback,
    Checkpoint,
    ComputeExecutorAdapter,
    ControllerClient,
    ExitReadback,
    Hold,
    HostController,
    HostLocalArbiter,
    HostWriterAuthority,
    ResourceSocket,
    SlotRequest,
)


@pytest.fixture
def host(tmp_path):
    now = [100.0]
    sequence = [0]
    stopped = []
    def sensor():
        sequence[0] += 1
        return CapacityReadback("host", sequence[0], now[0], 100, 4, True, True)
    with HostWriterAuthority(tmp_path.resolve(), "host") as authority:
        arbiter = HostLocalArbiter(authority, (ResourceSocket("cpu", "host", 10, 1, "fixture"),),
                                   sensor, lambda: now[0], fixture_profiles=frozenset({"fixture"}))
        executor = ComputeExecutorAdapter(
            lambda *args: True,
            lambda grant, eid: Checkpoint.create(grant, {"progress": 7}),
            lambda grant, eid, cp: stopped.append(grant.request_id),
            lambda grant, eid: ExitReadback(grant, eid, True, 0, now[0]),
        )
        alice = ControllerClient("alice", frozenset({"cpu"}), allow_interactive=True, require_leases=True)
        bob = ControllerClient("bob", frozenset({"cpu"}), allow_interactive=True, require_leases=True)
        operator = ControllerClient("operator", frozenset({"cpu"}), operator=True)
        ctl = HostController(BACHFacade(arbiter, {"cpu": executor}), (alice, bob, operator))
        yield ctl, alice, bob, operator, now, stopped


def submit(ctl, client, request_id="job", ttl=5, interactive=False):
    request = SlotRequest(request_id, "cpu", "slot", "host", request_id, interactive=interactive)
    return ctl.submit_leased(client.client_id, client.token, request, ttl)


def tick(ctl, operator):
    return ctl.tick(operator.client_id, operator.token)


def test_controller_heartbeat_does_not_renew_client_lease(host):
    ctl, alice, _bob, operator, now, stopped = host
    receipt = submit(ctl, alice)
    assert receipt.expires_at == 105
    assert tick(ctl, operator)["state"] == "RUNNING"
    for t in (101, 102, 104):
        now[0] = t
        assert tick(ctl, operator)["state"] == "RUNNING"
    now[0] = 105
    assert tick(ctl, operator)["state"] == "IDLE"
    assert stopped == ["job"]
    assert ctl.request_status(alice.client_id, alice.token, "job")["state"] == "EXPIRED"


def test_expiry_blocks_new_turns_and_waits_for_existing_turn(host):
    ctl, alice, _bob, operator, now, stopped = host
    submit(ctl, alice, interactive=True)
    tick(ctl, operator)
    grant = ctl._active
    ctl.begin_turn(alice.client_id, alice.token, grant, "turn1")
    now[0] = 106
    assert tick(ctl, operator)["state"] == "RETURN_REQUESTED"
    assert stopped == []
    with pytest.raises(Hold):
        ctl.begin_turn(alice.client_id, alice.token, grant, "turn2")
    ctl.end_turn(alice.client_id, alice.token, grant, "turn1")
    assert tick(ctl, operator)["state"] == "IDLE"
    assert stopped == ["job"]


def test_waiting_expiry_never_starts_a_process(host):
    ctl, alice, _bob, operator, now, stopped = host
    submit(ctl, alice)
    now[0] = 106
    assert tick(ctl, operator)["state"] == "IDLE"
    assert stopped == []


def test_renew_is_owned_fenced_idempotent_and_cannot_revive(host):
    ctl, alice, bob, _operator, now, _stopped = host
    receipt = submit(ctl, alice)
    with pytest.raises(Hold):
        ctl.renew(alice.client_id, alice.token, receipt, "bad", float("nan"))
    with pytest.raises(Hold):
        ctl.renew(bob.client_id, bob.token, receipt, "wrong-owner", 10)
    now[0] = 102
    renewed = ctl.renew(alice.client_id, alice.token, receipt, "renew1", 10)
    assert renewed.generation == 2 and renewed.expires_at == 112
    now[0] = 103
    assert ctl.renew(alice.client_id, alice.token, receipt, "renew1", 10) == renewed
    with pytest.raises(Hold):
        ctl.renew(alice.client_id, alice.token, receipt, "late-old-answer", 10)
    with pytest.raises(Hold):
        ctl.renew(alice.client_id, alice.token, receipt, "renew1", 11)
    now[0] = 112
    with pytest.raises(Hold):
        ctl.renew(alice.client_id, alice.token, renewed, "expired", 10)


def test_submit_retry_does_not_extend_lease_or_revive_cancelled_request(host):
    ctl, alice, _bob, operator, now, stopped = host
    receipt = submit(ctl, alice)
    now[0] = 102
    assert submit(ctl, alice) == receipt
    assert ctl.cancel(alice.client_id, alice.token, receipt)["state"] == "CANCELLED"
    assert ctl.cancel(alice.client_id, alice.token, receipt)["state"] == "CANCELLED"
    with pytest.raises(Hold):
        submit(ctl, alice)
    assert tick(ctl, operator)["state"] == "IDLE"
    assert stopped == []


def test_live_cancel_never_releases_without_checkpoint_and_exit(host):
    ctl, alice, _bob, operator, _now, stopped = host
    receipt = submit(ctl, alice, interactive=True)
    tick(ctl, operator)
    grant = ctl._active
    ctl.begin_turn(alice.client_id, alice.token, grant, "turn")
    assert ctl.cancel(alice.client_id, alice.token, receipt)["state"] == "RETURN_REQUESTED"
    assert tick(ctl, operator)["state"] == "RETURN_REQUESTED"
    assert stopped == []
    ctl.end_turn(alice.client_id, alice.token, grant, "turn")
    assert tick(ctl, operator)["state"] == "IDLE"
    assert ctl.request_status(alice.client_id, alice.token, "job")["state"] == "CANCELLED"


def test_leased_client_cannot_use_unbounded_legacy_submit(host):
    ctl, alice, bob, _operator, _now, _stopped = host
    with pytest.raises(Hold):
        ctl.submit(alice.client_id, alice.token, SlotRequest("j", "cpu", "s", "host", "j"))
    with pytest.raises(Hold):
        ctl.request_status(bob.client_id, bob.token, "job")


def test_expiry_blocks_new_turn_before_the_next_controller_poll(host):
    ctl, alice, _bob, operator, now, stopped = host
    submit(ctl, alice, interactive=True)
    tick(ctl, operator)
    now[0] = 106
    with pytest.raises(Hold):
        ctl.begin_turn(alice.client_id, alice.token, ctl._active, "after-client-expiry")
    assert stopped == [] and ctl._active is not None
