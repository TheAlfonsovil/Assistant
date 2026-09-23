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
|-- capabilities/            user-facing abilities built from tools
|-- application/              task lifecycle and graph execution
|   |-- __init__.py            stable TaskService export
|   `-- service.py             task lifecycle, leases and graph execution
|-- runtime.py                persistent scheduler loop
|-- domain/                   models and graph rules
|-- infrastructure/           SQLite and repositories
|-- llm.py                    provider contract and Ollama/mock providers
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
should be clarified by the user. The default project workflow starts with a bounded evidence operation, but audit
tasks are not required to follow a fixed linear recipe. The planner can use the
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
