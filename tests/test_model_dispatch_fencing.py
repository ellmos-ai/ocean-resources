# SPDX-License-Identifier: MIT
"""Real native writer/inode fencing; all model observations remain fixtures."""
import os
from copy import deepcopy
from pathlib import Path

import pytest
import test_model_load_pending as pending_fixture

from ocean_resources import Grant, Hold, HostWriterAuthority, SlotRequest
from ocean_resources.model_dispatch import require_dispatch_permission

GIB = pending_fixture.GIB
start = pending_fixture.start
pending_host = pending_fixture.pending_host
posix_reader = pytest.mark.skipif(os.name == "nt", reason="staged native reader requires POSIX flock")


def pending_permit(ctx):
    ctl, client, _operator, _arbiter, _port, _free, _now, _writer, _sensor = ctx
    lease, status = start(ctx)
    grant = Grant(**status["torch"])
    permit = ctl.begin_model_load(client.client_id, client.token, grant, "attempt")
    return lease, grant, permit


def check(ctx, permit):
    _ctl, client, _operator, arbiter, _port, _free, now, writer, _sensor = ctx
    return require_dispatch_permission(permit, authority_id=writer.authority_id,
        host_id=arbiter.authority.host_id, client_id=client.client_id,
        clock=lambda: now[0], wall_clock=lambda: 100)


@posix_reader
def test_live_owned_writer_can_dispatch_and_renew_without_stale_lease_credit(pending_host):
    ctl, client, _operator, _arbiter, _port, _free, _now, _writer, _sensor = pending_host
    lease, _grant, permit = pending_permit(pending_host)
    check(pending_host, permit)
    ctl.renew(client.client_id, client.token, lease, "renew-once", 900)
    # The original attempt remains bound to the same owner; a later native
    # lease generation is read from the journal, never inferred by the caller.
    check(pending_host, permit)


@posix_reader
def test_current_host_heartbeat_is_durable_for_pending_dispatch(pending_host):
    ctl, _client, operator, _arbiter, _port, _free, now, _writer, _sensor = pending_host
    _lease, _grant, permit = pending_permit(pending_host)
    now[0] += 25
    assert ctl.tick(operator.client_id, operator.token)["state"] == "LOAD_PENDING"
    now[0] += 5
    check(pending_host, permit)


@pytest.mark.parametrize("change", ["abort", "release", "cancel", "lease", "heartbeat", "profile", "inode", "torn", "unlocked", "writer_birth", "writer_boot"])
@posix_reader
def test_cached_permit_cannot_survive_lost_native_proof(pending_host, change):
    ctl, client, operator, _arbiter, _port, _free, now, writer, _sensor = pending_host
    lease, grant, permit = pending_permit(pending_host)
    check(pending_host, permit)
    if change in {"abort", "release"}:
        ctl.complete_model_load(client.client_id, client.token, grant, "attempt", False)
        if change == "release":
            assert ctl.tick(operator.client_id, operator.token)["torch"] is None
    elif change == "cancel":
        ctl.cancel(client.client_id, client.token, lease)
    elif change == "lease":
        ctl.renew(client.client_id, client.token, lease, "short-lease", 1)
        now[0] += 1
    elif change == "heartbeat":
        now[0] += 30
    elif change == "profile":
        Path(permit["profile_readback"]["profile_path"]).write_text("{}")
    elif change == "inode":
        path = Path(permit["writer_readback"]["path"])
        content = path.read_bytes()
        path.unlink()
        path.write_bytes(content)
        path.chmod(0o600)
    elif change == "torn":
        Path(permit["writer_readback"]["path"]).write_bytes(b'0{"payload":')
    elif change == "unlocked":
        writer.__exit__()
    else:
        key = "created_at" if change == "writer_birth" else "boot_time"
        permit["writer_readback"]["writer"][key] += 1
    with pytest.raises(Hold):
        check(pending_host, permit)


@posix_reader
def test_new_writer_same_process_and_inode_never_revives_old_journal(pending_host):
    _ctl, _client, _operator, _arbiter, _port, _free, _now, writer, _sensor = pending_host
    _lease, _grant, permit = pending_permit(pending_host)
    before = deepcopy(writer.unresolved())
    writer.__exit__()
    with HostWriterAuthority(writer.directory, writer.host_id) as replacement:
        assert replacement.unresolved() == before
        assert replacement.authority_id != permit["writer_readback"]["authority_id"]
        with pytest.raises(Hold):
            check(pending_host, permit)


@pytest.mark.parametrize("change", ["cancel", "writer_reopen"])
@posix_reader
def test_profile_validation_delay_cannot_outlive_final_writer_read(pending_host, monkeypatch, change):
    import ocean_resources.model_dispatch as reader
    ctl, client, _operator, _arbiter, _port, _free, _now, writer, _sensor = pending_host
    lease, _grant, permit = pending_permit(pending_host)
    original = reader.ReviewedProfileGate.require_model_load
    replacements = []
    def cancel_after_profile(gate, socket):
        result = original(gate, socket)
        if change == "cancel":
            ctl.cancel(client.client_id, client.token, lease)
        else:
            writer.__exit__()
            replacements.append(HostWriterAuthority(writer.directory, writer.host_id).__enter__())
        return result
    monkeypatch.setattr(reader.ReviewedProfileGate, "require_model_load", cancel_after_profile)
    try:
        with pytest.raises(Hold):
            check(pending_host, permit)
    finally:
        for replacement in replacements:
            replacement.__exit__()


def test_ordinary_end_turn_cannot_tombstone_staged_attempt(pending_host):
    ctl, client, _operator, _arbiter, _port, _free, _now, _writer, _sensor = pending_host
    _lease, grant, _permit = pending_permit(pending_host)
    with pytest.raises(Hold, match="staged load"):
        ctl.end_turn(client.client_id, client.token, grant, "attempt")
    result = ctl.complete_model_load(client.client_id, client.token, grant, "attempt", False)
    assert result["model_load"]["phase"] == "ABORTED"


def test_staged_request_keeps_protocol_when_selected_during_start(pending_host, monkeypatch):
    ctl, client, operator, arbiter, _port, free, _now, _writer, _sensor = pending_host
    free[0] = 24 * GIB
    ordinary = SlotRequest("ordinary", "model", "bach-model", arbiter.authority.host_id, "ordinary", priority=4, interactive=True)
    staged = SlotRequest("staged", "model", "bach-model", arbiter.authority.host_id, "staged", priority=1, interactive=True)
    ctl.submit_leased(client.client_id, client.token, ordinary, 900)
    original = ctl.facade.start_next
    def competing_arrival(**kwargs):
        ctl.submit_model_load(client.client_id, client.token, staged, 900)
        return original(**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(ctl.facade, "start_next", competing_arrival)
        selection = ctl.tick(operator.client_id, operator.token)
    state = ctl.request_status(client.client_id, client.token, staged.request_id)
    assert state["state"] in {"QUEUED", "LOAD_PENDING"}
    if state["state"] == "QUEUED" and selection["torch"] is None:
        assert ctl.tick(operator.client_id, operator.token)["state"] == "LOAD_PENDING"
