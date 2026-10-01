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
| `schedule` | `every`, `daily`, `list`, `cancel` | Recurring work: creates a holder task that the runtime clones when due | `startup/bootstrap.py`, **after** the service is built |
| `types` | `probe`, `hover`, `definition`, `references`, `rename`, `diagnostics`, `symbols` | Semantic answers from a language server: the real type of a symbol, where it is defined, every reference, a complete rename (opt-in apply) and real type errors. `symbols` returns a file's outline instead of the file, so a 213 KB module becomes ~4 KB. `probe` reports whether the server exists, and every failure says why instead of guessing | `devices/computer/actions/registry.py` |
| `debug` | `probe`, `trace` | Runtime truth from a debug adapter: runs a program with breakpoints and reports each stop with its frame and local values, instead of adding print statements. `probe` reports `missing-debugpy` when the optional dependency is absent | `devices/computer/actions/registry.py` |
| `window` | `probe`, `list`, `snapshot` | Desktop windows as a control tree (UI Automation): which windows exist and, for one of them, its controls with name, type, enabled state and screen rectangle. Read-only; acting stays with the opt-in `input.*`, and refs are valid only inside the answer that produced them | `devices/computer/actions/registry.py` |

## Recurring work

A schedule is a **holder task** carrying `metadata["schedule"]`:

```json
{
  "kind": "daily",
  "every_seconds": null,
  "at_hour": 7,
  "at_minute": 30,
  "timezone": "Europe/Madrid",
  "next_run_at": "2026-10-01T05:30:00+00:00",
  "enabled": true,
  "runs": 0
}
```

Two kinds, decided by `assistant/recurrence.py`: an **interval**
(`every_seconds`, 60 s to 30 days) or a **daily** wall-clock time
(`at_hour`/`at_minute`) in a named timezone. Records written before the daily
kind existed carry no `kind` and are read as intervals, so an upgrade does not
lose existing schedules. Bounds, timezone resolution and the next-run math live
in that module and nowhere else, so the tool, the HTTP API and the scheduler
cannot drift.

The holder is written directly as `WAITING` (never dispatched) and
`reconcile_schedules()` clones it once per due window. The clone goes through
`create_task`, so project resolution, agent-mode wiring, budget defaults,
leases, idempotency and the ledger behave exactly as for any other task.
Missed windows collapse into a single run instead of a backlog (a daily
schedule does not make up skipped days), and `SCHEDULE_CREATED` /
`SCHEDULE_FIRED` / `SCHEDULE_CANCELLED` events keep the history auditable.

Lifecycle, and why the three verbs differ:

- **Pause** flips `enabled` to false and leaves the holder in `WAITING`: it can
  be resumed, and resuming recomputes `next_run_at` from now so it never fires
  the instant it is re-armed.
- **Cancel** is terminal. The holder becomes `CANCELLED`, which the domain state
  machine forbids reviving, so `resume`, `pause` and `update` all refuse it
  rather than pretending otherwise. Create a new schedule instead.
- **Update** replaces the recurrence and keeps `runs` and the event history.

Two consequences worth knowing:

- Recurrence is reconciled **before** the `has_work` short circuit in
  `reconcile_idle`, so a busy queue cannot starve a due schedule.
- A clone inherits the holder's `project_id` and attachments, but not its
  orchestration stage: it routes and is verified from scratch.
- A holder whose schedule record cannot be interpreted is skipped, never
  guessed at: firing work from a spec nobody understands is worse than not
  firing.

Without the `tzdata` package (Windows has no system timezone database) only
`UTC` and fixed offsets resolve; the HTTP API reports that as
`timezone_database: false` instead of silently treating a zone as UTC.

## The observation loop (page and screen)

A worker that clicks must look again afterwards, or it only knows what it
*intended*. The loop is therefore part of the runtime, not a convention:

1. `ToolDefinition.observable_methods` lists the methods whose effect is only
   knowable by looking (`browser.click`, `browser.type`, `browser.navigate`,
   `browser.open`, and `input.click/type/key/scroll`).
2. After one of them, `TaskService._observe_surface` observes the surface again:
   `browser.snapshot` for a page, `screen.capture` for the desktop.
3. `assistant/devices/computer/page.py` reduces the surface to a stable shape —
   url, title, visible text, actionable elements with one reference each — and a
   `digest`. Identical digest means nothing changed.
4. The result travels as the next turn's `LAST OBSERVATION`, an
   `AGENT_OBSERVATION` field (`surface`) and a `SURFACE_OBSERVED` event. Captures
   are published as artifacts, so the evidence outlives the turn.
5. `changed: false` is a *failure signal*, and the count of fruitless attempts
   (`attempts_without_change`) comes with a hint telling the worker to stop
   repeating the action. That is what keeps a GUI loop from spinning forever.

Two sources feed the observation, and the reason is cost and honesty rather than
elegance: a Chromium tab with `--remote-debugging-port` gives the live DOM **and
element boxes**, which is what lets `browser.click` act on a reference instead of
a guessed coordinate; when no debug port answers, a plain HTTP fetch plus a
stdlib HTML parse still produces text, elements and a digest, so text-only work
never needs a browser. `screen.capture` plus `input.*` remains the fallback for
windows that cannot be debugged, and that path is genuinely pixel-based: without
vision the model cannot see the image, so it works from the reported geometry.

Predicates (`browser.wait_for`, and `page.matches`) are deliberately small and
deterministic — `text_contains`, `title_contains`, `url_contains`,
`element_present`, `digest_changed` — because verification that needs an LLM is
not verification.

## Recursos: what this machine has

`GET /resources` reports the peripherals an operator expects to see: monitors
(count, geometry, which one is primary), the virtual desktop, RAM, per-disk free
space, logical cores and the capability flags (`screen_capture`,
`input_control`, and the hint that explains how to enable the second one).
`POST /resources/capture` runs the same `screen.capture` a worker would, so the
dashboard shows exactly the evidence a task would produce, and
`GET /resources/screenshot/{name}` serves that PNG and nothing else.

## Adding a capability

1. Implement `Tool` (see `assistant/tools.py`) and register it in the device
   branch, in `devices/registry.py`, or in `startup/bootstrap.py` when it needs
   the `TaskService` itself.
2. Add the tool name to the allowlist in `ContextBuilder._available_actions`
   (`assistant/context.py`). It is an allowlist, not a filter: a tool that is
   registered but not listed is invisible to the model.
3. Cover it with a test that asserts the tool reaches the prompt, not only that
   it executes.
