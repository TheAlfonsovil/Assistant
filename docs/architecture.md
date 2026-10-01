# Assistant Core Architecture

Assistant Core is a persistent local task engine. The database is the source of truth; the LLM proposes structured actions but never executes operating-system calls.

## Mental model

There are three layers with different responsibilities:

1. `startup` loads the application, checks readiness and recovers persisted work.
2. `devices` exposes what the assistant can do on each platform-specific device branch.
3. `application` resolves code projects and executes tasks through the stable `Tool` contract.

Projects are domain resources, not devices. The project registry and task
association belong to the domain/application and persistence layers; the
computer branch only exposes the adapter (`project.analyze`) that operates on
the already-resolved path.

The task engine does not contain Windows, Android, home or robot details. A device branch owns its platform adapters, transport and actions; the registry only composes them.

Node execution contracts and mutable runtime state are persisted separately.
This keeps retries, review and recovery from being confused with the immutable
node contract. Task and node control metadata columns are removed; only evidence metadata on
operations and artifacts remains intentionally open-ended. Databases older
than schema 5 are rejected at startup and must be recreated or exported.
The recorded schema version must also match schema 5 exactly; the runtime does
not silently reinterpret or upgrade an older database.

## Prompt caching (why block order is a contract)

The provider caches a request *prefix*: a hit is only counted for the leading
tokens that are byte-identical to a previous request of the same role. Block
order is therefore a cost decision, and it is enforced mechanically:

- `scripts/reorder_prompts.py` puts stable blocks first (role instructions, tool
  catalog, output schema, fixed limits, target descriptors) and volatile blocks
  last (task, memory, evidence, observations, remaining budget). It refuses to
  write a file that would lose a marker or a hand-written line, and it closes
every prompt with a short anchor naming only the blocks that prompt has. The
renderer is idempotent: the anchor is re-emitted from the blocks, never appended
on top of a previous copy, so running the tool twice cannot duplicate it.
- `constraints` is split into `limits` (fixed maxima → cached) and `remaining`
  (counters that move every turn → tail).
- `scripts/measure_prompt_cache.py` reports the identical prefix between two real
  prompts, which is the number that predicts the cache hit rate.
- The agent-side size ceiling follows the same rule. `agent_context_chars`
  (default 200000, against a 400000 hard prompt cap) is the evidence budget for
  one turn: a context that fits is passed through untouched, and when it does not
  fit the degradation order is fixed — volatile evidence and observations shrink
  first, then memory/project/index material, and the tool catalog is reduced in
  argument detail only. It is never replaced by a fabricated catalog, and if it
  must be truncated that is stated in the prompt. Hiding a capability is more
  expensive than a longer prompt: the worker will not use what it cannot see.

Measured effect on the agent turn: the identical prefix between two consecutive
turns went from 13% of the prompt to 97.6%, so the tool catalog and the output
schema — the two largest blocks — now bill at the cached rate on every turn.

## The observation loop

Perception is part of the engine, not a tool convention. `ToolDefinition
.observable_methods` marks the methods whose effect is only knowable by looking;
the runtime observes again after them, attaches the result to the next turn and
records a `SURFACE_OBSERVED` event with a digest. `page.py` builds that
observation (live DOM over CDP when a debug port answers, HTML parse otherwise),
`cdp.py` is the protocol client, and `inventory.py` reports the peripherals. See
`assistant/capabilities/README.md` for the full contract.

## Semantic queries and the debugger (the two things text cannot answer)

The codegraph is a heuristic index: it parses imports and symbols out of the
source. It cannot say what a name *is*, and it cannot prove that a rename
reached every call site. A language server can, so `types` asks one:

- `lsp.py` is a small LSP client: `node` runs pyright's language server over
  stdio with Content-Length framed JSON-RPC, one shared server per workspace
  root, serialised with a lock, terminated when the application context closes.
  Servers are cached per event loop, because a process belongs to the loop that
  started it and reusing it from another one raises.
- `semantics.py` is the tool. `hover` returns the real signature and docstring,
  `definition` the site (across modules), `references` every use with a count and
  an honest `truncated` flag, `rename` the language server's complete edit set,
  and `diagnostics` the batch type checker's report as evidence. The symbol is
  located by **name** inside the file, so a caller does not need the exact
  column, and the number of name matches is reported so an ambiguous answer is
  visible.
- A rename is the operation text search cannot justify: the server resolves the
  symbol, so the plan is complete by construction. Applying is opt-in
  (`apply: true`) and refusen when the report was truncated — half a rename is
  worse than none. Both workspace-edit shapes are accepted (`changes` and
  `documentChanges`), and a file operation inside the plan is reported instead of
  silently ignored.
- Availability is a reported fact, not an assumption: `types.probe` returns
  `available`, the resolved `node`/`langserver`/`type_checker` paths, the
  analyzer version, and a reason when it cannot run (`missing-node`,
  `missing-server`). The batch checker is invoked as `node index.js` directly:
  measured 1.1 s against 18.5 s through `npx`, which is the difference between a
  usable verification step and a stall.

The debugger answers the third question: what the value **was**. `dap.py` speaks
Debug Adapter Protocol to `debugpy` (same framing, same discipline: bounded
output, explicit unavailability, per-call teardown) and `debugger.py` turns it
into `debug.trace`: run a program with breakpoints, and for each stop report the
frame, the file and line, and the local values. That replaces the print-statement
loop for "why is this wrong on the second iteration", which is exactly the kind
of evidence a deterministic verifier cannot produce on its own.

## Desktop windows (the observation loop for native applications)

The observation loop originally covered pages through CDP and everything else
through a screenshot plus coordinates. Native windows had no structure at all
until `window.py` added the Windows accessibility tree: `window.list` reports the
top-level windows with title, class, process and rectangle, and
`window.snapshot` walks one window's controls with name, type, enabled state and
screen rectangle. The model gets a tree of names, which is what makes a desktop
application reachable without a single per-application rule.

Three rules keep it honest and bounded: it is **observation only** (acting stays
with the opt-in `input.*`, which consumes the reported rectangle), the tree is
**pruned** (a control is kept when it has a name, is actionable, or its type is
structural — anonymous panes are noise), and `ref` values are valid **only inside
the answer that produced them**, so nothing pretends to be a stable handle. COM
must be initialised in the thread that uses it, and the API blocks, so every call
runs off the event loop and reports its own duration.

## Budgets (why they are configuration)

The ledger enforces one budget per dimension and records `BUDGET_EXHAUSTED` when
one runs out: LLM calls, tool calls, structural-index queries, project reads,
source bytes, plan nodes (which also bound agent turns), retries and recovery
attempts, plus wall-clock time. The `TaskBudget` model keeps conservative engine
defaults for a bare task, and the deployment sets the real ceiling for every task
it creates (`task_budgets` in `bootstrap.py`, from the `ASSISTANT_TASK_MAX_*`
settings). A ceiling that is too low does not fail loudly: it stops useful work
halfway, so it belongs in configuration rather than in a constant.

## Folder map

```text
assistant/
|-- startup/                 load, readiness report and recovery
|   |-- bootstrap.py          one composition root for CLI and API
|   |-- manager.py            database + LLM checks and state recovery
|   `-- models.py             StartupReport and readiness states
|-- devices/                 the four visible device branches
|   |-- base.py               DeviceBranch contract
|   |-- registry.py           branch composition and mock adapters
|   |-- computer/
|   |       |-- actions/          filesystem, shell, git and project actions
|   |       |   |-- __init__.py   stable computer-action exports
|   |       |   `-- core.py       current action implementations
|   |   `-- registry.py       computer facade
|   |-- mobile/               mock branch
|   |-- home/                 mock branch
|   `-- robot/                mock branch
|-- tools.py                 Tool contract, dispatch and normalized results
|-- recurrence.py            when a schedule runs next (intervals and wall-clock)
|-- capabilities/            user-facing abilities built from tools
|-- application/              task lifecycle and graph execution
|   |-- __init__.py            stable TaskService export
|   `-- service.py             task lifecycle, leases and graph execution
|-- runtime.py                persistent scheduler loop
|-- domain/                   models and graph rules
|-- infrastructure/           SQLite and repositories
|-- llm.py                    provider contract and DeepSeek/mock providers
`-- api/                      FastAPI entry points using startup.bootstrap
|   |-- __init__.py            stable app export
|   `-- application.py         HTTP routes and lifecycle
`-- cli.py                    command-line entry point
```

## Startup sequence

Both `assistant run` and FastAPI follow this sequence:

```text
load settings
    -> create database and LLM provider
    -> create SQLite schema
    -> check LLM and configured model
    -> remove expired leases and recover RUNNING nodes
    -> count unfinished tasks
    -> build device/tool registry
    -> expose TaskService
```

A missing LLM produces `DEGRADED`, not a fake `READY`: the process can inspect persisted state, but LLM-dependent work may fail. `StartupReport` is available to the CLI and `/health` endpoint. The runtime then processes queued, ready and running tasks. When there is no active work it performs an idle health pass without creating synthetic maintenance tasks, exposes its last pass and active-task count through `/health`, and applies bounded backoff if a complete runtime pass fails.

The optional `ASSISTANT_USER_*` settings create one structured `user_profile` memory. It is explicitly supplied configuration, not an inference: name, birth date, profession, degrees and expertise are stored as JSON and updated by key. The planner always includes this profile alongside memories relevant to the task. Do not place secrets or broad personal data in `.env`; use an explicit memory action for anything else.

## Add an action to the computer

1. Implement a `Tool` in `assistant/devices/computer/actions/core.py` (or split
   the package into focused modules such as `filesystem.py`, `processes.py`,
   `project.py` and `git.py` as each area grows).
2. Give it a unique `ToolDefinition` name, methods, argument schema and permissions.
3. Add an instance to `register_actions`.
4. Add a focused test for success and invalid arguments.

The planner will receive the definition automatically. Do not modify
`application/service.py` for a normal device action.

The final LLM phase is `FINAL_RESPONSE`, not a mandatory report. It chooses an appropriate response type (`answer`, `report`, `plan`, `clarification`, `blocked` or `action_proposal`) from the original request and execution evidence.

## Task routing and agent lifecycle

Creating a task records two independent inputs: the user goal and an optional
execution target (`device`, `project` or another resource). Creation does not
ask a worker to act. On the first execution turn, the `ORCHESTRATOR` receives a
small routing envelope containing the goal, explicit target, known targets,
long-term memory and available workers. It returns one routing decision:
intent, resolved target, worker and template, or a clarification request.

The routing decision is persisted as `ORCHESTRATOR_DECISION`. Only after that
event does the selected worker receive its bounded context and call tools one
operation at a time. Tool results are persisted as observations, and each
subsequent turn returns to the worker/agent until it completes, waits, asks the
user or fails. The task ledger remains the source of truth, so interruption and
recovery resume from the last persisted decision or observation.

Audit tasks have a deterministic structural preflight before the first
orchestrator request. The runtime rebuilds the selected project's bounded
codegraph from its resolved path, persists the refreshed version and exposes it
through the explicit `{{codegraph}}` prompt marker. The marker contains the
project root, project kind, bounded relative file tree, key files, languages,
modules, symbols and dependency edges. Full source and full graph payloads are
not embedded in prompts; workers query the current graph for focused details.
Planner-path codegraph queries refresh the project graph before querying, while
direct codegraph queries never trust a persisted graph without a same-turn
freshness proof.

That refresh is conditional, because an index that is rebuilt on every phase
costs seconds and moves the prompt prefix for nothing. A stat-only fingerprint of
the project tree (path, size, mtime, ignoring the same directories the analysis
ignores) is stored with the graph: unchanged tree, unchanged caps and a graph
younger than `codegraph_refresh_seconds` means the stored graph is reused and
`codegraph_version` does not move. Caps (`codegraph_max_files`,
`codegraph_max_symbols`, `codegraph_max_edges`) are part of the identity, so
raising one forces a rebuild. A failed refresh is recorded
(`LLM_CODEGRAPH_REFRESH_FAILED`, `blocking: false`) and the phase continues:
`codegraph.query` analyses on demand, so a stale index degrades orientation
instead of killing the task. The manual endpoint rebuilds by default.

Partiality travels with the graph. `truncated`, `edges_truncated`,
`symbols_truncated` and the real totals are stored, `codegraph.query` reports
`index_partial`, the prompt summary carries a `partial` block, and the summary
keeps a `hot_files` list (files with most symbols) so a model can pick its first
read without a round-trip. Presenting a capped index as complete would be worse
than having none: the model reads "no edge" as "no dependency".

For project work, the orchestrator receives only codegraph metadata and version
information. The worker queries the graph or reads files when evidence is needed;
the complete graph is never copied into every prompt.

## Add a new device branch

1. Add a package under `assistant/devices/<name>/`.
2. Add a `DeviceBranch` entry in `DEVICE_BRANCHES` with `ACTIVE` or `MOCK` status.
3. Implement `register_actions` in that branch and call it from `DeviceRegistry.register`.
4. Keep connection and platform-specific details inside that package.
5. Add a registry test and keep the task engine unchanged.

Mobile, home and robot intentionally expose mock status tools today. They establish the extension points without pretending that hardware adapters exist.

## Core components

- `assistant.domain`: Pydantic domain models, graph rules, statuses and error types.
- `assistant.infrastructure`: SQLAlchemy 2 async persistence over SQLite.
- `assistant.tools`: stable tool definitions, registry, policy boundary and normalized results.
- `assistant.capabilities`: user-facing abilities composed from one or more tools.
- `assistant.application`: task creation, graph execution, leases, idempotency, retries and cancellation.
- `assistant.runtime`: persistent background loop that resumes queued work from SQLite.
- `assistant.project_analysis`: bounded project inventory, key-file evidence,
  multi-language structural symbols, test discovery and validation evidence.

SQLite stores tasks, nodes, edges, events, leases and idempotency results. Important transitions are events for audit and later UI/debugging work.

The next runtime boundary is documented in
[memory-context-performance.md](memory-context-performance.md). It defines the
typed distinction between hard and soft constraints, recovery graph expansion,
task/node/durable memory scopes, Ledger-first context construction, Windows
project-tool selection and the limits of KV-cache reuse through the current
Ollama integration. It is an implementation plan; it does not claim that
physical KV handles are currently reusable.

## Project workflow

Each project stores a stable name, absolute path, description, project type and
default audit prompt. A task may reference `project_id`. Resolution is
deterministic: explicit id, explicit name, one default, or the sole enabled
project. Multiple projects without an explicit selection remain unresolved and
should be clarified by the user. Audit tasks use an incremental agent loop over
the existing `TaskService` runtime: each bounded turn selects one operation,
persists its decision and observation, and rebuilds context for the next turn.
Task nodes and events remain the durable ledger, so a run can be resumed without
a second scheduler. Terminal decisions are explicit (`COMPLETE`, `WAIT`,
`ASK_USER` or `FAIL`), and unknown tools or exhausted budgets are blocked.
The agent can use
first result as an orientation point and add targeted, read-only exploration:
codegraph queries, bounded project reads, `filesystem.search_text`, or a
stack-specific validation command. Each next operation must answer an explicit
unknown and must remain bounded. A typical audit may therefore look like:

```text
orientation -> targeted search/read/query -> validation -> synthesis
```

`filesystem.search_text` supports one or several words, bounded matches,
case-sensitivity, `all`/`any` matching and small context windows. It never
reads supported secret environment files. The audit phase is read-only with
respect to project files and runs a detected test command by default.
`project.audit` remains the deterministic evidence collector; it does not
replace targeted exploration and it does not authorize the LLM to invent
findings. The final response may render the persisted evidence as an
`audit.md`-style report, but the report is a synthesis, not the source of
truth. Implementation, deployment and browser work are separate phases and
must not be inferred from an audit request.

## Memory injection

Memory is context, not control flow. The planner receives a bounded set of
relevant records, plus explicit profile/system facts. Each record includes its
kind, key, value, provenance and confidence and is labelled as data-only. A
memory value never supplies a project path or overrides the registered project
context; project identity is resolved structurally before prompting the LLM.

## Relationship graphs

`codegraph.build` analyzes a user project and returns bounded module, symbol and
import relationships. `codegraph.system` analyzes the Assistant source itself
and returns module, symbol, containment, import and resolvable call edges.

The system graph is not injected into every prompt by default. A task can opt in
with `metadata.include_system_graph=true`; the planner and node resolver then
receive a bounded `system_graph` context. `metadata.system_graph_max_files`
controls the source scope so graph context remains useful without overwhelming
the LLM. The graph is context data only and does not override task, project or
tool contracts.

## Failure recovery

Terminal node failures are analyzed by the replanner within a bounded recovery
budget. It may retry the failed node, create a persisted fix branch, restart the
task graph, or block the task. Fix branches retain the original failure and use
the Git capabilities exposed by the project when available; a branch is not
merged or redeployed unless the corresponding tool is registered and the plan
explicitly requests it. This keeps recovery honest for projects that have no
deployment adapter.

The local `deployment` tool provides the stable methods `build`, `test`,
`deploy`, `verify`, and `rollback`. Each call requires a command declared by
the project in the operation arguments, so the Assistant can support scripts,
containers, services, or another local target without assuming one platform.
Deployment recovery should follow `build -> test -> deploy -> verify`; when
verification fails, `rollback` is available as an explicit recovery step.
