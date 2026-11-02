"""Pure FIFO and bounded foreground-fairness selection for host adapters.

This module selects one ticket from a snapshot supplied by the host. It does
not own a queue, coordinate processes, manage model resources, cancel running
calls, or dispatch inference. The host must supply only live tickets already
validated for the active model targets and handle all admission effects.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

QueuePriority = Literal["foreground", "background"]
SelectionReason = Literal[
    "background_fairness",
    "background_only",
    "empty",
    "foreground_only",
    "foreground_priority",
    "legacy_foreground_pending",
]

MAX_CONSECUTIVE_FOREGROUND = 3


class QueuePolicyError(ValueError):
    """Raised when a host supplies an invalid ticket snapshot or state."""


@dataclass(frozen=True, slots=True)
class QueueTicket:
    """One live candidate in a host-owned inference queue snapshot.

    ``sequence`` is a host-assigned, globally unique FIFO ordinal. ``target_key``
    is the opaque key of the already validated model binding; the selector
    checks equality but does not resolve or authorize a model target.
    """

    ticket_id: str
    priority: QueuePriority
    sequence: int
    target_key: str


@dataclass(frozen=True, slots=True)
class QueueSelection:
    """The selected candidate and the fairness state for the next decision."""

    ticket: QueueTicket | None
    next_foreground_streak: int
    reason: SelectionReason


def select_next_ticket(
    tickets: Iterable[QueueTicket],
    *,
    foreground_streak: int,
    expected_target_key: str | None = None,
    trusted_target_keys: Iterable[str] | None = None,
    legacy_foreground_pending: bool = False,
) -> QueueSelection:
    """Select one eligible ticket without mutating the supplied snapshot."""

    if type(foreground_streak) is not int or foreground_streak < 0:
        raise QueuePolicyError("foreground streak must be a non-negative integer")
    if type(legacy_foreground_pending) is not bool:
        raise QueuePolicyError("legacy foreground state must be a boolean")
    if (expected_target_key is None) == (trusted_target_keys is None):
        raise QueuePolicyError(
            "provide exactly one of expected_target_key or trusted_target_keys"
        )

    if expected_target_key is not None:
        if (
            type(expected_target_key) is not str
            or not expected_target_key
            or expected_target_key != expected_target_key.strip()
        ):
            raise QueuePolicyError("expected model target key is invalid")
        trusted_targets = frozenset((expected_target_key,))
    else:
        if isinstance(trusted_target_keys, (str, bytes)):
            raise QueuePolicyError("trusted model targets must be an iterable of keys")
        try:
            target_values = tuple(trusted_target_keys or ())
        except TypeError as exc:
            raise QueuePolicyError("trusted model targets must be iterable") from exc
        if not target_values:
            raise QueuePolicyError("at least one trusted model target is required")
        if any(
            type(target) is not str
            or not target
            or target != target.strip()
            for target in target_values
        ):
            raise QueuePolicyError("trusted model target key is invalid")
        if len(set(target_values)) != len(target_values):
            raise QueuePolicyError("trusted model target keys must be unique")
        trusted_targets = frozenset(target_values)

    try:
        candidates = tuple(tickets)
    except TypeError as exc:
        raise QueuePolicyError("tickets must be iterable") from exc

    seen_ids: set[str] = set()
    seen_sequences: set[int] = set()
    foreground: list[QueueTicket] = []
    background: list[QueueTicket] = []

    for ticket in candidates:
        if type(ticket) is not QueueTicket:
            raise QueuePolicyError("ticket has an unsupported shape")
        if (
            type(ticket.ticket_id) is not str
            or not ticket.ticket_id
            or ticket.ticket_id != ticket.ticket_id.strip()
        ):
            raise QueuePolicyError("ticket identifier is invalid")
        if ticket.ticket_id in seen_ids:
            raise QueuePolicyError("ticket identifiers must be unique")
        seen_ids.add(ticket.ticket_id)

        if type(ticket.priority) is not str or ticket.priority not in (
            "foreground",
            "background",
        ):
            raise QueuePolicyError("ticket priority is invalid")
        if type(ticket.sequence) is not int or ticket.sequence < 0:
            raise QueuePolicyError("ticket sequence must be a non-negative integer")
        if ticket.sequence in seen_sequences:
            raise QueuePolicyError("ticket sequences must be unique")
        seen_sequences.add(ticket.sequence)

        if (
            type(ticket.target_key) is not str
            or not ticket.target_key
            or ticket.target_key not in trusted_targets
        ):
            raise QueuePolicyError("ticket target is not trusted by the host binding")

        if ticket.priority == "foreground":
            foreground.append(ticket)
        else:
            background.append(ticket)

    foreground.sort(key=lambda ticket: ticket.sequence)
    background.sort(key=lambda ticket: ticket.sequence)

    if legacy_foreground_pending:
        return QueueSelection(None, foreground_streak, "legacy_foreground_pending")

    if not background:
        if not foreground:
            return QueueSelection(None, 0, "empty")
        return QueueSelection(foreground[0], 0, "foreground_only")

    if not foreground:
        return QueueSelection(background[0], 0, "background_only")

    if foreground_streak >= MAX_CONSECUTIVE_FOREGROUND:
        return QueueSelection(background[0], 0, "background_fairness")

    return QueueSelection(
        foreground[0],
        foreground_streak + 1,
        "foreground_priority",
    )
