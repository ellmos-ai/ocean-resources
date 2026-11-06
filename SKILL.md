---
name: ocean-resources
version: "0.2.0"
type: standalone-module
standalone: true
visibility: private
provenance: "Extrahiert aus Ocean / BACH Ressourcenarchitektur (#2441)"
description: "Offline-first RAM distributor and model runtime scheduler for cooperative local LLM inference and compute job arbitration."
---

# ocean-resources Skill

Use this skill when developing, testing, or integrating with `ocean-resources`: managing host RAM distribution, allocating model residence torch, or arbitrating multi-process local LLM inference.

## Primary Responsibilities

1. **Physical Memory Management (`RAMDistributor`)**:
   - Register consumer workloads via `ConsumerCapability`.
   - Evaluate admission requests via `DistributorRequest` against real host memory budgets.
   - Adjust host tendency (`"ollama"` vs `"compute"`).
   - Apply `WatchdogHysteresisPolicy` to prevent premature model eviction during cold starts.

2. **Inference Turn Arbitration (`inference_queue_policy`)**:
   - Supply a host-owned snapshot of live `QueueTicket` objects with validated target keys; the host maps proximity classes to foreground/background.
   - Select one candidate with `select_next_ticket`; persist `next_foreground_streak` in the host. With both priorities live and no legacy foreground hold, background is selected after three foreground selections. A legacy hold selects nothing; the function neither enqueues nor dispatches work.

## Code Patterns

```python
from ocean_resources import (
    ConsumerCapability,
    DistributorRequest,
    RAMDistributor,
    UserProximity,
    QueueTicket,
    select_next_ticket,
)

# Initialize RAM distributor
dist = RAMDistributor(total_ram_bytes=32 * 1024**3, tendency="ollama")

# Evaluate ticket selection
tickets = [
    QueueTicket(ticket_id="t-1", priority="foreground", sequence=1, target_key="qwen3.8"),
    QueueTicket(ticket_id="t-2", priority="background", sequence=2, target_key="qwen3.8"),
]
foreground_streak = 0  # Host-owned state, carried between selections.
selection = select_next_ticket(
    tickets, foreground_streak=foreground_streak, expected_target_key="qwen3.8"
)
foreground_streak = selection.next_foreground_streak
if selection.ticket is not None:
    # The host must admit/dispatch the selected candidate and update its queue.
    tickets = [ticket for ticket in tickets if ticket.ticket_id != selection.ticket.ticket_id]
next_selection = select_next_ticket(
    tickets, foreground_streak=foreground_streak, expected_target_key="qwen3.8"
)
```
