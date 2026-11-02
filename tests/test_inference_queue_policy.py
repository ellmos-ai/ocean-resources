"""Deterministic tests for the pure host-side inference queue policy."""

from __future__ import annotations

from typing import Literal

import pytest

from ocean_resources import QueueTicket as PublicQueueTicket
from ocean_resources import select_next_ticket as public_select_next_ticket
from ocean_resources.inference_queue_policy import (
    MAX_CONSECUTIVE_FOREGROUND,
    QueuePolicyError,
    QueueTicket,
    select_next_ticket,
)


TARGET = "validated-target-digest"


def ticket(
    ticket_id: str,
    priority: Literal["foreground", "background"],
    sequence: int,
    target_key: str = TARGET,
) -> QueueTicket:
    return QueueTicket(ticket_id, priority, sequence, target_key)


def test_policy_is_available_from_package_public_api():
    assert PublicQueueTicket is QueueTicket
    assert public_select_next_ticket is select_next_ticket


def test_foreground_is_fifo_and_background_gets_a_turn_after_three():
    waiting = [
        ticket("f-3", "foreground", 30),
        ticket("b-1", "background", 10),
        ticket("f-1", "foreground", 11),
        ticket("b-2", "background", 12),
        ticket("f-2", "foreground", 20),
        ticket("f-4", "foreground", 40),
    ]
    streak = 0

    for expected_id in ("f-1", "f-2", "f-3", "b-1", "f-4", "b-2"):
        decision = select_next_ticket(
            waiting,
            foreground_streak=streak,
            expected_target_key=TARGET,
        )
        assert decision.ticket is not None
        assert decision.ticket.ticket_id == expected_id
        waiting.remove(decision.ticket)
        streak = decision.next_foreground_streak

    assert waiting == []
    assert MAX_CONSECUTIVE_FOREGROUND == 3


def test_background_only_uses_fifo_and_resets_streak():
    decision = select_next_ticket(
        [ticket("b-2", "background", 20), ticket("b-1", "background", 10)],
        foreground_streak=2,
        expected_target_key=TARGET,
    )

    assert decision.ticket == ticket("b-1", "background", 10)
    assert decision.next_foreground_streak == 0
    assert decision.reason == "background_only"


def test_no_background_resets_streak_before_later_background_arrives():
    foreground = select_next_ticket(
        [ticket("f-1", "foreground", 1)],
        foreground_streak=MAX_CONSECUTIVE_FOREGROUND,
        expected_target_key=TARGET,
    )
    assert foreground.ticket == ticket("f-1", "foreground", 1)
    assert foreground.next_foreground_streak == 0
    assert foreground.reason == "foreground_only"

    next_decision = select_next_ticket(
        [ticket("f-2", "foreground", 2), ticket("b-1", "background", 3)],
        foreground_streak=foreground.next_foreground_streak,
        expected_target_key=TARGET,
    )
    assert next_decision.ticket == ticket("f-2", "foreground", 2)
    assert next_decision.next_foreground_streak == 1


def test_empty_snapshot_resets_streak():
    decision = select_next_ticket(
        [], foreground_streak=2, expected_target_key=TARGET
    )

    assert decision.ticket is None
    assert decision.next_foreground_streak == 0
    assert decision.reason == "empty"


def test_legacy_foreground_marker_blocks_new_tickets_without_counting():
    decision = select_next_ticket(
        [ticket("f-1", "foreground", 1), ticket("b-1", "background", 2)],
        foreground_streak=2,
        expected_target_key=TARGET,
        legacy_foreground_pending=True,
    )

    assert decision.ticket is None
    assert decision.next_foreground_streak == 2
    assert decision.reason == "legacy_foreground_pending"


def test_selection_does_not_mutate_ticket_snapshot():
    waiting = (
        ticket("b-1", "background", 2),
        ticket("f-1", "foreground", 1),
    )
    before = waiting

    decision = select_next_ticket(
        waiting, foreground_streak=0, expected_target_key=TARGET
    )

    assert decision.ticket == waiting[1]
    assert waiting == before


def test_mismatched_model_target_is_rejected():
    with pytest.raises(QueuePolicyError, match="host binding"):
        select_next_ticket(
            [ticket("f-1", "foreground", 1, "another-target")],
            foreground_streak=0,
            expected_target_key=TARGET,
        )
