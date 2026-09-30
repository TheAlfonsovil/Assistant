# Capabilities

A capability is a user-facing ability composed from one or more tools/actions. Add new abilities here and register them through a device branch. Low-level adapters remain behind the Tool Registry.

## Registered capabilities

| Capability | Methods | What it adds | Where it is registered |
| --- | --- | --- | --- |
| `memory` | `search`, `write` | Long-term memory the worker can query and extend. Scoped to the project by default; leaves the task open **after** a repository exists | `devices/registry.py` (needs `repository`) |
| `artifact` | `list`, `read` | Reads back evidence produced by earlier nodes or tasks, so a later step does not have to regenerate it | `devices/registry.py` (needs `repository`) |
| `screen` | `list`, `capture` | Monitor enumeration and PNG capture via `ctypes`/GDI. Publishes the image as an artifact with `origin_x`/`origin_y`/`scale` plus an explicit `coordinate_hint`, so a model can map a 0-1000 coordinate space onto real pixels | `devices/computer/actions/registry.py` |
| `input` | `move`, `click`, `type`, `key`, `scroll` | Real mouse/keyboard control. **Opt-in** (`ASSISTANT_ENABLE_INPUT_CONTROL`) because it can type into any window | `devices/computer/actions/registry.py` |
| `http` | one method per verb (`get`/`head`/`options`/`post`/`put`/`patch`/`delete`) | HTTP calls with per-method idempotency: read verbs are retry-safe, the rest are not, so the ledger never replays an unsafe request | `devices/computer/actions/registry.py` |
| `schedule` | `every`, `list`, `cancel` | Recurring work: creates a holder task that the runtime clones when due | `startup/bootstrap.py`, **after** the service is built |

## Recurring work

A schedule is a **holder task** carrying `metadata["schedule"]`:

```json
{"every_seconds": 900, "next_run_at": "...", "enabled": true, "runs": 0}
```

The holder is written directly as `WAITING` (never dispatched) and
`reconcile_schedules()` clones it once per due window. The clone goes through
`create_task`, so project resolution, agent-mode wiring, budget defaults,
leases, idempotency and the ledger behave exactly as for any other task.
Missed windows collapse into a single run instead of a backlog, and
`SCHEDULE_FIRED` / `SCHEDULE_CANCELLED` events keep the history auditable.
Intervals are bounded to 60 s – 30 days by `service.SCHEDULE_MIN_SECONDS`.

Two consequences worth knowing:

- Recurrence is reconciled **before** the `has_work` short circuit in
  `reconcile_idle`, so a busy queue cannot starve a due schedule.
- A clone inherits the holder's `project_id` and attachments, but not its
  orchestration stage: it routes and is verified from scratch.

## Adding a capability

1. Implement `Tool` (see `assistant/tools.py`) and register it in the device
   branch, in `devices/registry.py`, or in `startup/bootstrap.py` when it needs
   the `TaskService` itself.
2. Add the tool name to the allowlist in `ContextBuilder._available_actions`
   (`assistant/context.py`). It is an allowlist, not a filter: a tool that is
   registered but not listed is invisible to the model.
3. Cover it with a test that asserts the tool reaches the prompt, not only that
   it executes.
