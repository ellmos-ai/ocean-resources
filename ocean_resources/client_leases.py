"""Bounded external client lifetimes under the existing controller mutex.

These are liveness requests, never independent evidence that RAM was released.
The controller's checkpoint/exit/readback protocol remains the only release path.
"""
from dataclasses import asdict

from .contracts import Hold, RequestLease, SlotRequest, identity, instant


class ClientLeaseLifecycle:
    """Private controller component; uses its existing authority and ownership."""

    @staticmethod
    def _lease_ttl(ttl):
        if not instant(ttl) or not 1 <= ttl <= 3600:
            raise Hold("client lease lifetime must be finite and between 1 and 3600 seconds")
        return ttl

    def submit_leased(self, client_id, token, request, ttl_seconds):
        client = self._authenticate(client_id, token)
        ttl = self._lease_ttl(ttl_seconds)
        if not isinstance(request, SlotRequest):
            raise Hold("explicit request required")
        with self._mutex:
            now = self._now()
            old = self._leases.get(request.request_id)
            if old is not None:
                if (self._owners.get(request.request_id) != client_id or old["request"] != request
                        or old["initial_ttl"] != ttl or old["terminal"]
                        or now >= old["receipt"].expires_at):
                    raise Hold("expired, foreign or conflicting leased request")
                return old["receipt"]
            if request.request_id in self._owners or len(self._leases) >= 10000:
                raise Hold("request identity exists or lease history requires explicit archival")
            receipt = RequestLease(self._authority_id, request.request_id, 1, now + ttl)
            self._audit("client-lease-submitted", client_id=client_id, lease=asdict(receipt))
            self._submit(client, request)
            self._leases[request.request_id] = {
                "request": request, "initial_ttl": ttl, "receipt": receipt,
                "terminal": None, "renewals": {},
            }
            return receipt

    def _owned_lease(self, client, receipt):
        if not isinstance(receipt, RequestLease) or receipt.authority_id != self._authority_id:
            raise Hold("foreign client lease writer")
        entry = self._leases.get(receipt.request_id)
        if entry is None or self._owners.get(receipt.request_id) != client.client_id:
            raise Hold("client does not own the leased request")
        return entry

    def renew(self, client_id, token, receipt, renewal_id, ttl_seconds):
        client = self._authenticate(client_id, token)
        identity(renewal_id)
        ttl = self._lease_ttl(ttl_seconds)
        with self._mutex:
            entry = self._owned_lease(client, receipt)
            old = entry["renewals"].get(renewal_id)
            if old is not None:
                if old[:2] != (receipt, ttl):
                    raise Hold("renewal identity conflicts with its original command")
                return old[2]
            if (receipt != entry["receipt"] or entry["terminal"]
                    or self._now() >= receipt.expires_at or len(entry["renewals"]) >= 1024):
                raise Hold("client lease is expired, stale, terminal or requires archival")
            renewed = RequestLease(self._authority_id, receipt.request_id,
                                   receipt.generation + 1, self._now() + ttl)
            self._audit("client-lease-renewed", client_id=client_id, lease=asdict(renewed))
            entry["renewals"][renewal_id] = (receipt, ttl, renewed)
            entry["receipt"] = renewed
            return renewed

    def request_status(self, client_id, token, request_id):
        client = self._authenticate(client_id, token)
        identity(request_id)
        with self._mutex:
            if self._owners.get(request_id) != client.client_id:
                raise Hold("client does not own this request")
            entry = self._leases.get(request_id)
            if entry is None:
                raise Hold("request has no client lease")
            active = self._active if self._active and self._active.request_id == request_id else None
            state = ("RETURN_REQUESTED" if self._return else "RUNNING") if active else (entry["terminal"] or "QUEUED")
            return {"schema": "ocean-client-request.v1", "state": state,
                    "lease": asdict(entry["receipt"]), "grant": asdict(active) if active else None,
                    "expires_in_seconds": max(0, entry["receipt"].expires_at - self._now())}

    def _end_lease(self, request_id, reason):
        entry = self._leases[request_id]
        if entry["terminal"]:
            return
        self._audit("client-lease-ended", request_id=request_id, reason=reason)
        if self._active is not None and self._active.request_id == request_id:
            self._return = ("client-lease-" + reason.lower(), False)
            self.arbiter.request_return(self._active)
        else:
            self.arbiter.cancel_waiting(request_id)
        entry["terminal"] = reason

    def cancel(self, client_id, token, receipt):
        client = self._authenticate(client_id, token)
        with self._mutex:
            entry = self._owned_lease(client, receipt)
            if receipt != entry["receipt"] or self._transition:
                raise Hold("stale client lease or handoff in progress")
            self._end_lease(receipt.request_id, "CANCELLED")
            return self.request_status(client_id, token, receipt.request_id)

    def _expire_leases(self, now):
        for request_id, entry in self._leases.items():
            if not entry["terminal"] and now >= entry["receipt"].expires_at:
                self._end_lease(request_id, "EXPIRED")
