# SPDX-License-Identifier: MIT
"""Retained finite transports for one explicitly authorized staged model load."""
import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict

from .contracts import Grant, Hold, RequestLease, SlotRequest
from .model_client import _cancel_submitted_request


@asynccontextmanager
async def model_load_turn(client, socket_id, slot_id, priority, *, check_ready=None,
                          wait_seconds=300, ttl_seconds=900):
    if priority not in {"foreground", "background"} or not 1 <= wait_seconds <= 3600:
        raise ValueError("bounded explicit model load policy required")
    request_id, turn_id = "load-" + uuid.uuid4().hex, uuid.uuid4().hex
    request = SlotRequest(request_id, socket_id, slot_id, client.host_id, request_id,
                          priority=1 if priority == "foreground" else 4, interactive=True)
    submit_call = begin_call = renewal = None
    lease = grant = None
    begun = failed = False
    ticket = {"successful": False, "permit": None, "renewal": None}
    deadline = time.monotonic() + wait_seconds
    try:
        if check_ready:
            check_ready()
        submit_call = asyncio.create_task(asyncio.to_thread(client.submit_model_load, request, ttl_seconds))
        lease = await asyncio.shield(submit_call)
        while True:
            if check_ready:
                check_ready()
            state = await asyncio.to_thread(client.request_status, request_id)
            lease = RequestLease(**state["lease"])
            if state["state"] == "LOAD_PENDING":
                grant = Grant(**state["grant"])
                if grant.authority_id != client.authority_id or grant.request_id != request_id:
                    raise Hold("unrelated model load grant")
                break
            if state["state"] != "QUEUED" or time.monotonic() >= deadline:
                raise Hold("model load admission remains unavailable")
            await asyncio.sleep(.1)

        async def renew():
            for attempt in range(2):
                state = await asyncio.to_thread(client.request_status, request_id)
                if state["state"] != "LOAD_PENDING":
                    raise Hold("model load no longer pending")
                try:
                    return await asyncio.to_thread(client.renew, RequestLease(**state["lease"]),
                                                   uuid.uuid4().hex, ttl_seconds)
                except Hold:
                    if attempt:
                        raise
        lease = await renew()
        begin_call = asyncio.create_task(asyncio.to_thread(client.begin_model_load, grant, turn_id))
        permit = await asyncio.shield(begin_call)
        if (not isinstance(permit, dict) or permit.get("grant") != asdict(grant)
                or permit.get("turn_id") != turn_id or permit.get("phase") != "MAY_LOAD"):
            raise Hold("model load permission is not bound to this attempt")
        begun = True
        ticket["permit"] = permit

        async def heartbeat():
            while True:
                await asyncio.sleep(min(30, ttl_seconds / 3))
                await renew()
        renewal = asyncio.create_task(heartbeat())
        ticket["renewal"] = renewal
        yield ticket
    except BaseException:
        failed = True
        raise
    finally:
        async def settle():
            nonlocal lease
            if submit_call is not None:
                try:
                    lease = await asyncio.shield(submit_call)
                except Hold:
                    pass
            if renewal is not None:
                renewal.cancel()
                try:
                    await renewal
                except (asyncio.CancelledError, Hold):
                    pass
            if begin_call is None:
                if submit_call is not None:
                    await _cancel_submitted_request(client, request_id)
                return
            try:
                await asyncio.shield(begin_call)
            except Hold:
                pass  # Settle the exact attempt even when its ACK was lost.
            success = begun and not failed and ticket["successful"] is True
            until = time.monotonic() + 30
            while True:
                try:
                    result = await asyncio.to_thread(client.complete_model_load, grant, turn_id, success)
                    if result["state"] != ("RUNNING" if success else "RETURN_REQUESTED"):
                        raise Hold("model load completion is not confirmed")
                    ticket["completion"] = result
                    return
                except (Hold, KeyError, TypeError) as exc:
                    try:
                        current = await asyncio.to_thread(client.request_status, request_id)
                    except Hold:
                        current = None
                    if (not success and current is not None and current["grant"] is None
                            and current["state"] in {"FINISHED", "EXPIRED", "CANCELLED"}):
                        return
                    if current is not None and current["state"] == "RECOVERY_HOLD" or time.monotonic() >= until:
                        raise Hold("model load completion requires native recovery") from exc
                    await asyncio.sleep(.1)
        cleanup = asyncio.create_task(settle())
        cancelled = False
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                if cleanup.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
