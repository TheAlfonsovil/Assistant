# Assistant Core Architecture

Assistant Core is a local, persistent task engine. The database is the source of truth; the LLM proposes structured actions but never executes operating-system calls.

## Components

- `assistant.domain`: Pydantic domain models, graph rules, statuses and error types.
- `assistant.infrastructure`: SQLAlchemy 2 async persistence over SQLite. Repositories isolate SQL from the domain.
- `assistant.llm`: provider protocol, deterministic mock provider and Ollama adapter.
- `assistant.tools`: tool definitions, registry, policy boundary and normalized results.
- `assistant.project_analysis`: bounded project inventory with Python symbols and import edges.
- `assistant.runtime`: persistent background loop that resumes queued work from SQLite.
- SQLite `memories`: long-term facts/preferences available to the Planner as relevant context.
- `assistant.application`: task creation, graph execution, leases, idempotency, retries and cancellation.
- `assistant.api`: minimal FastAPI HTTP interface.
- `assistant.cli`: local smoke-test CLI.

## Execution

A request creates a queued task and a ready root node. The runtime leases a ready node, builds a role-specific context, asks the provider for a validated proposal, checks that the referenced capability exists, executes it through the registry, persists an `OperationResult`, and advances the graph. A future provider or tool only implements its own interface.

The `project.analyze` capability is intentionally separate from the execution graph. It returns an inventory of files, languages, Python symbols and import edges. The Planner can use that result to choose relevant files and later capabilities can add Java/Kotlin, TypeScript or application-specific analyzers without changing the Task Engine.

`assistant run` is the resident runtime. It loads unfinished tasks, processes active nodes and sleeps when there is no work. Idle maintenance must be scheduled explicitly; the loop does not create an unbounded stream of synthetic tasks.

To add a capability, implement `Tool`, provide a Pydantic `ToolDefinition`, register it in `ToolRegistry`, and keep its effects inside `OperationResult`. The graph engine, persistence and LLM provider remain unchanged.

SQLite stores tasks, nodes, edges, events, leases and idempotency results. All important transitions are represented as events for audit and later UI/debugging work.
