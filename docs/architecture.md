# Assistant Core Architecture

Assistant Core is a persistent local task engine. The database is the source of truth; the LLM proposes structured actions but never executes operating-system calls.

## Mental model

There are three layers with different responsibilities:

1. `startup` loads the application, checks readiness and recovers persisted work.
2. `devices` exposes what the assistant can do on each device branch.
3. `application` executes tasks through the stable `Tool` contract.

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

A missing LLM produces `DEGRADED`, not a fake `READY`: the process can inspect persisted state, but LLM-dependent work may fail. `StartupReport` is available to the CLI and `/health` endpoint. The runtime then processes queued, ready and running tasks; it does not create synthetic maintenance tasks.

The optional `ASSISTANT_USER_*` settings create one structured `user_profile` memory. It is explicitly supplied configuration, not an inference: name, birth date, profession, degrees and expertise are stored as JSON and updated by key. The planner always includes this profile alongside memories relevant to the task. Do not place secrets or broad personal data in `.env`; use an explicit memory action for anything else.

## Add an action to the computer

1. Implement a `Tool` in `assistant/devices/computer/actions.py` (or split that module into an `actions/` package when it grows).
2. Give it a unique `ToolDefinition` name, methods, argument schema and permissions.
3. Add an instance to `register_actions`.
4. Add a focused test for success and invalid arguments.

The planner will receive the definition automatically. Do not modify `application.py` for a normal device action.

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
