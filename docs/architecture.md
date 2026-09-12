# Assistant Core Architecture

Assistant Core is a persistent local task engine. The database is the source of truth; the LLM proposes structured actions but never executes operating-system calls.

## Mental model

There are three layers with different responsibilities:

1. `startup` loads the application, checks readiness and recovers persisted work.
2. `devices` exposes what the assistant can do on each device branch.
3. `application` resolves projects and executes tasks through the stable `Tool` contract.

Projects are domain resources, not devices. The project registry and task
association belong to the domain/application and persistence layers; the
computer branch only exposes the adapter (`project.analyze`) that operates on
the already-resolved path.

The task engine does not contain Windows, mobile, home or robot details. A device branch owns its adapters and actions; the registry only composes them.

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
|   |   |-- actions.py        filesystem, shell, git and project actions
|   |   `-- registry.py       computer facade
|   |-- mobile/               mock branch
|   |-- home/                 mock branch
|   `-- robot/                mock branch
|-- tools.py                 Tool contract, dispatch and normalized results
|-- capabilities/            user-facing abilities built from tools
|-- application.py            task lifecycle and graph execution
|-- runtime.py                persistent scheduler loop
|-- domain/                   models and graph rules
|-- infrastructure/           SQLite and repositories
|-- llm.py                    provider contract and Ollama/mock providers
`-- api.py / cli.py           entry points using startup.bootstrap
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

1. Implement a `Tool` in `assistant/devices/computer/actions.py` (or split that module into an `actions/` package when it grows).
2. Give it a unique `ToolDefinition` name, methods, argument schema and permissions.
3. Add an instance to `register_actions`.
4. Add a focused test for success and invalid arguments.

The planner will receive the definition automatically. Do not modify `application.py` for a normal device action.

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
- `assistant.project_analysis`: bounded project inventory with Python symbols and import edges.

SQLite stores tasks, nodes, edges, events, leases and idempotency results. Important transitions are events for audit and later UI/debugging work.

## Project workflow

Each project stores a stable name, absolute path, description, project type and
default audit prompt. A task may reference `project_id`. Resolution is
deterministic: explicit id, explicit name, one default, or the sole enabled
project. Multiple projects without an explicit selection remain unresolved and
should be clarified by the user. The default code-project workflow creates a
normal task for each iteration:

```text
audit -> plan -> inspect/implement/test -> verify -> evidence report
```

This keeps repeated reviews independent and durable without making the project
itself an endlessly running task.

## Memory injection

Memory is context, not control flow. The planner receives a bounded set of
relevant records, plus explicit profile/system facts. Each record includes its
kind, key, value, provenance and confidence and is labelled as data-only. A
memory value never supplies a project path or overrides the registered project
context; project identity is resolved structurally before prompting the LLM.
