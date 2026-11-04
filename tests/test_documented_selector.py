"""Execute the documented pure example and probe host-owned fairness boundaries."""
from pathlib import Path

from ocean_resources import QueueTicket, select_next_ticket


def test_skill_example_executes_with_validated_target_and_carried_state():
    skill = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
    code = skill.split("```python\n", 1)[1].split("```", 1)[0]
    namespace = {}
    exec(compile(code, "SKILL.md example", "exec"), namespace)  # noqa: S102 -- execute the reviewed repository example, not external input
    assert namespace["selection"].ticket.ticket_id == "t-1"
    assert namespace["foreground_streak"] == 1
    assert namespace["next_selection"].ticket.ticket_id == "t-2"


def test_persisted_fairness_selects_background_but_legacy_hold_admits_nothing():
    tickets = [QueueTicket("fg-" + str(i), "foreground", i, "fixture-model") for i in range(4)]
    tickets.append(QueueTicket("bg", "background", 4, "fixture-model"))
    streak = 0
    for expected in ("fg-0", "fg-1", "fg-2"):
        selection = select_next_ticket(tickets, foreground_streak=streak,
                                       expected_target_key="fixture-model")
        assert selection.ticket.ticket_id == expected
        streak = selection.next_foreground_streak
        tickets = [ticket for ticket in tickets if ticket != selection.ticket]
    original = tuple(tickets)
    held = select_next_ticket(tickets, foreground_streak=streak,
                              expected_target_key="fixture-model", legacy_foreground_pending=True)
    assert held.ticket is None
    assert held.reason == "legacy_foreground_pending"
    assert held.next_foreground_streak == 3
    assert tuple(tickets) == original
    resumed = select_next_ticket(tickets, foreground_streak=held.next_foreground_streak,
                                 expected_target_key="fixture-model", legacy_foreground_pending=False)
    assert resumed.ticket.ticket_id == "bg"
    assert resumed.reason == "background_fairness"
    assert resumed.next_foreground_streak == 0
    assert tuple(tickets) == original
