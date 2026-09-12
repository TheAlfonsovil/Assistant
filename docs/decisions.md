# Architecture Decisions

## SQLite first

SQLite keeps V1 local and durable. SQLAlchemy repositories keep the domain independent from the database so PostgreSQL can be added later.

## Provider boundary

`LLMProvider` exposes planning, resolution, replanning and verification without exposing Ollama payloads to the task engine.

## Proposal before execution

LLM output is validated with Pydantic, then resolved through `ToolRegistry`. No provider can call the operating system directly.

## Node scheduling

The scheduler operates on graph nodes because dependencies, retries and leases belong to nodes. A graph validator rejects cycles.

## Event history

State rows are optimized for current state; event rows preserve important changes and allow future audit/debugging views.
