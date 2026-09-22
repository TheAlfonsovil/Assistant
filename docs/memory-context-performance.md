# Memory, Context and Recovery Design

This document records the audit and implementation plan for the next runtime
phase. It is a design contract, not an assertion that every item is already
implemented.

The first recovery/contract slice is now implemented: typed soft constraints,
the `recovery_expansions` table, idempotent recovery branch creation,
`GRAPH_EXPANDED` events, recovery success/failure lifecycle updates and the
`max_plan_nodes` guard. Constraint evaluation currently consumes explicit
`OperationResult.metadata.constraint_violations`; natural-language constraint
inference remains deliberately outside the deterministic verifier.

Node contracts and runtime state are separated into typed JSON documents.
Context builders can select only the fields required by a phase instead of
serializing a complete metadata dictionary. Task and node control metadata is
not a supported persistence format; incompatible databases are rejected at
startup.

## Audit conclusions

### Weak constraints

The current planner has hard graph validation, acceptance evidence and tool
contracts, but it does not distinguish a hard requirement from a preference.
That makes generic requests unnecessarily brittle: a preference such as
"prefer tests before editing" should guide planning without blocking a valid
plan when the project has no tests.

Weak constraints should be explicit and typed:

- `id`: stable criterion identifier;
- `description`: human-readable preference;
- `strength`: `hard` or `soft`;
- `weight`: advisory ordering value, not a correctness score;
- `scope`: task, node or recovery branch;
- `on_violation`: `warn`, `replan` or `block`;
- `evidence`: optional evidence reference.

The planner may trade off soft constraints. The verifier must never turn a
soft-constraint warning into a failed operation. Hard constraints continue to
be enforced deterministically. A soft constraint must never authorize an
unsafe tool or override a required input, output, permission, deadline or
idempotency rule.

### Recovery and graph expansion

The current runtime already supports `FIX` recovery nodes, bounded recovery
attempts and recovery metadata. The missing contract is provenance for the
expanded graph. A recovery branch must be a first-class subgraph:

1. The original node remains `FAILED` with its result and diagnostics.
2. A recovery root is created with `parent_node_id` pointing to the failed
   node and `metadata.recovery_target_id`.
3. Recovery nodes are connected in order with normal graph edges.
4. The final recovery node receives a `RECOVERY_FINALIZE` marker.
5. Only successful finalization can requeue the original node.
6. Recovery nodes cannot silently consume the original node's outputs.
   They consume published artifacts and explicit failure evidence.
7. Every expansion emits `GRAPH_EXPANDED`, including origin, branch id,
   strategy, parent node, and created node ids.
8. Replanning is idempotent by `(task_id, failed_node_id, recovery_attempt)`.
9. A failed recovery branch remains visible and is not deleted when the target
   is retried.

This gives a stable audit trail and prevents a recovery branch from becoming a
second untracked task. It also preserves the original graph as the causal
record while allowing the executable graph to grow.

### Memory and context

The current system has the correct ingredients — SQLite, task-local
`WorkingMemory`, long-term `MemoryRecord`, the Artifact Ledger and bounded
contexts — but the boundaries are not yet explicit enough. `output_data`,
events and long-term memory can still be confused with one another.

Use four memory scopes:

| Scope | Lifetime | Contents | Sent to |
|---|---|---|---|
| Stable prefix | process/model session | role instructions, response schema, invariant policies | every request of that role |
| Task memory | task and descendants | contract, decisions, assumptions, open questions, artifact refs | planner, resolver, replanner, final response |
| Node working set | one node attempt | resolved inputs, immediate dependency summaries, last error, acceptance evidence | resolver and verifier |
| Durable memory | across tasks | user/system facts and explicitly promoted lessons | planner and selected roles only |

Events remain audit history, not prompt memory. Full `output_data` remains a
compatibility snapshot, not an input contract. Artifacts are referenced by
immutable IDs and loaded only when a declared input or verifier criterion
requires them.

### Prefill and KV-cache reality

Ollama's current integration uses independent non-streaming
`POST /api/generate` calls. The application does not receive a stable KV-cache
handle and cannot explicitly attach a previous KV state to the next request.
Therefore:

- physical KV reuse across requests is not currently guaranteed;
- increasing `num_ctx` does not reduce prefill;
- sending the same text repeatedly still costs prompt evaluation;
- a prompt-prefix cache can only be implemented if the selected backend exposes
  prefix/KV reuse or a resident session API.

The safe optimization path is:

1. Keep a byte-identical stable prefix per role.
2. Put changing task/node data after that prefix.
3. Use compact JSON with deterministic key ordering.
4. Cache rendered stable sections and schemas in process memory.
5. Reuse the task context snapshot across sequential node resolutions.
6. Prefetch the next node's context while the current tool executes, but never
   prefetch an LLM call or make a decision before the current node commits.
7. Release node working sets after verification; retain only references and
   bounded diagnostics.
8. Measure `prompt_eval_duration`, prompt tokens, cache hits and context
   rebuild time before changing limits.

The implementation should expose a `ContextCache` and a `PromptPrefixCache`
with explicit invalidation. They cache serialized context fragments, not
unbounded model tensors. A future native backend may implement a third cache
layer for actual KV handles behind the same interface.

### Windows project boundaries

Project actions must remain platform-aware. If a registered project path is on
Windows, planning may use Windows project tools and Windows commands only:

- PowerShell or `cmd.exe` through the registered shell adapter;
- Windows-native project discovery and build commands;
- Windows path semantics and process identity.

The planner must not substitute Linux commands, WSL paths or container tools
unless the user explicitly requests a cross-environment target and a matching
tool is registered. This is a hard constraint, not a soft preference.

### `data/` and one-database policy

The canonical state database is `data/assistant.db`. The current directory also
contains WAL/SHM files, generated audit output, browser JSONL logs, process
stdout/stderr logs and an unrelated `halo.db`. They must not be merged blindly.

The target policy is:

- keep `assistant.db`, `assistant.db-wal` and `assistant.db-shm` as SQLite
  runtime files;
- migrate notifications, browser interactions and process metadata into
  tables in `assistant.db`;
- keep process stdout/stderr as external rotating files, referenced by an
  artifact row, because binary/text logs can grow independently;
- keep generated reports as Artifact Ledger entries with a path and checksum;
- quarantine or explicitly migrate `halo.db` only after identifying its owner
  and schema; never delete it automatically.

## Implementation phases

### Phase A — contracts

- Add `ConstraintSpec` with `hard`/`soft` strength and violation policy.
- Add `RecoveryBranch`/`GraphExpansion` metadata models.
- Add cache key/version models and context provenance.
- Add explicit memory promotion and eviction reasons.

### Phase B — deterministic recovery expansion

- Replace free-form recovery metadata with a persisted recovery branch record.
- Make graph expansion idempotent.
- Add graph edges and events for every created recovery node.
- Add finalization gating and tests for failed, retried and nested recovery.

### Phase C — memory boundaries

- Build task snapshots from contract, Working Memory and Ledger references.
- Reduce resolver context to immediate dependency summaries plus declared
  inputs.
- Add durable-memory promotion rules and TTL/size limits.
- Do not add new legacy compatibility fields to task or node persistence.

### Phase D — prefill optimization

- Introduce stable role prefixes and deterministic serialization.
- Cache schemas, templates and task snapshots.
- Add sequential next-node context prefetch.
- Add invalidation on graph expansion, user input, task redefine and recovery.
- Record cache hit/miss and prefill metrics.

### Phase E — data consolidation

- Add SQLite tables for notification records, browser interactions and process
  metadata.
- Backfill only files with an identified schema and checksum.
- Keep log files as artifacts until retention and migration are verified.
- Add maintenance commands to compact WAL and purge expired derived data.

### Phase F — verification

- Run tests for soft constraints, graph expansion, cache invalidation and
  Windows tool selection.
- Compare prompt token and prefill metrics before/after.
- Run the complete suite, Ruff and compile checks.
- Validate restart, recovery and task redefine against a copied local DB.

## Non-goals

- No production authentication or distributed worker coordination.
- No claim of physical KV reuse through the current Ollama API.
- No concurrent task execution; the queue remains sequential.
- No automatic deletion of `halo.db` or user-generated files.
- No cross-platform command emulation for Windows projects.
