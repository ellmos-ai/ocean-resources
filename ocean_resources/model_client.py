"""Async model turns over the authenticated host API, before the inference lock."""
import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict

from .contracts import Grant, Hold, RequestLease, SlotRequest


@asynccontextmanager
async def model_turn(client, socket_id, slot_id, priority, *, wait_seconds=300, ttl_seconds=1800,
                     check_ready=None):
    if priority not in {"foreground", "background"} or not 1 <= wait_seconds <= 3600:
        raise ValueError("explicit bounded model turn policy required")
    lease = grant = None
    submitted = False
    turn_attempted = turn_started = False
    turn_id = uuid.uuid4().hex
    renewal = None
    deadline = time.monotonic() + wait_seconds
    try:
        if check_ready is not None:
            check_ready()
        state = await asyncio.to_thread(client.status)
        active = state.get("torch")
        # All BACH roles share the configured model client. Reuse its resident
        # socket without unloading/reloading merely because the channel changed.
        if isinstance(active, dict) and active.get("socket_id") == socket_id:
            try:
                own = await asyncio.to_thread(client.request_status, active["request_id"])
                if own["state"] == "RUNNING":
                    lease = RequestLease(**own["lease"])
            except Hold:
                pass  # A different configured client owns this reservation.
        if lease is None:
            request_id = "model-" + uuid.uuid4().hex
            request = SlotRequest(request_id, socket_id, slot_id, client.host_id, request_id,
                                  priority=1 if priority == "foreground" else 4, interactive=True)
            lease = await asyncio.to_thread(client.submit_leased, request, ttl_seconds)
            submitted = True
        while True:
            if check_ready is not None:
                check_ready()
            observation = await asyncio.to_thread(client.request_status, lease.request_id)
            lease = RequestLease(**observation["lease"])
            if observation["state"] == "RUNNING":
                grant = Grant(**observation["grant"])
                break
            if observation["state"] != "QUEUED" or time.monotonic() >= deadline:
                raise Hold("model resource request is unavailable or its wait deadline expired")
            await asyncio.sleep(.1)

        async def renew_once():
            for attempt in range(2):
                current = await asyncio.to_thread(client.request_status, lease.request_id)
                if current["state"] != "RUNNING":
                    raise Hold("model resource return requested")
                try:
                    return await asyncio.to_thread(client.renew, RequestLease(**current["lease"]),
                                                   uuid.uuid4().hex, ttl_seconds)
                except Hold:
                    if attempt:
                        raise

        lease = await renew_once()
        if check_ready is not None:
            check_ready()
        turn_attempted = True
        await asyncio.to_thread(client.begin_turn, grant, turn_id)
        turn_started = True

        async def heartbeat():
            while True:
                await asyncio.sleep(min(30, ttl_seconds / 3))
                await renew_once()

        renewal = asyncio.create_task(heartbeat())
        # Caller still owns all normal reserve/cold-start/task/budget checks.
        yield {"grant": asdict(grant), "lease": asdict(lease), "renewal": renewal}
    finally:
        if renewal is not None:
            renewal.cancel()
            try:
                await renewal
            except (asyncio.CancelledError, Hold):
                pass
        if turn_attempted:
            # Also settle a possibly applied begin_turn after a lost response.
            try:
                await asyncio.shield(asyncio.to_thread(client.end_turn, grant, turn_id))
            except Hold:
                if turn_started:
                    raise
                # Preserve the original failed/uncertain begin response. The
                # controller retains unresolved turns; no client claims release.
        elif lease is not None and submitted:
            current = await asyncio.to_thread(client.request_status, lease.request_id)
            await asyncio.shield(asyncio.to_thread(client.cancel, RequestLease(**current["lease"])))
