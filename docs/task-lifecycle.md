# Task Lifecycle

`REQUEST -> CLASSIFY -> DIRECT_RESPONSE / PLAN -> VALIDATE_GRAPH -> GRAPH -> READY -> RESOLVE -> STATE_CHECK -> OPERATION -> TOOL -> RESULT -> VERIFYING -> SUCCESS / RETRY / REPLAN / WAIT_USER / FAILED / BLOCK -> RECONCILE -> FINAL_RESPONSE -> COMPLETE`

A task starts in `QUEUED` with a persisted root node. The API starts a background runtime, while the CLI can run a task directly or start the same runtime explicitly. Simple factual or computational requests can finish during planning with a direct answer and no child operation nodes. Multi-step work receives a Pydantic-validated graph proposal.

The scheduler considers nodes, not only tasks. Dependencies must be satisfied and an active lease must not exist. A task with no ready nodes is not reconciled as blocked while a running node still owns a valid lease. Lease acquisition treats a database collision as contention, not as task failure. For browser, media, or computer-control tasks, the resolver first requests available read-only computer state when it is missing. Public web search can discover a URL and `browser.open` can ask the local default browser to open it; opening a page is not the same as verifying a click or playback.

Transient and timeout results can return a node to `READY` while incrementing its retry count and persisting an exponential backoff timestamp. A retry decision is not a failure yet. When retries are exhausted, or an operation returns a non-retryable failure, the node becomes `FAILED` and the task becomes `FAILED`. `BLOCKED` is reserved for work that needs an explicit human solution, an unsupported capability, or an invalid/no-progress graph. Waiting nodes remain persisted across process restarts; user input is submitted through `POST /tasks/{id}/input` and requeues the selected node with that input. Cancellation marks pending nodes cancelled and preserves completed output.

When no node is ready, the engine reconciles the graph: all terminal nodes complete the task, a running node with an active lease keeps the task running, a waiting node moves the task to `WAITING`, a blocked node moves it to `BLOCKED`, and any other no-progress graph is blocked explicitly with a `TASK_NO_PROGRESS` event. Expired node deadlines emit `NODE_DEADLINE_EXCEEDED` and become blocked. Terminal tasks receive one persisted `FINAL_RESPONSE`, shared by CLI and API, including timeout and unexpected-exception paths. Deadlines and execution budgets are terminal transitions, not silent runtime exits.

Structural `WAIT` nodes pause without calling the LLM and resume with their persisted input. Structural `VERIFY` nodes complete when their dependencies succeeded. Operation results move both the node and task to `VERIFYING` before deterministic verification; a successful verification returns the task to `READY` for reconciliation. Recovery requeues both `RUNNING` and interrupted `VERIFYING` nodes and requeues their parent task. Planner graphs are validated for duplicate IDs, unknown dependencies, typed dependencies and cycles before any planned node is persisted. Plans above `TaskBudget.max_plan_nodes` are decomposed into smaller `SUBTASK` nodes instead of being persisted as one oversized graph. `CONDITION` and `NOTIFY` are currently rejected explicitly because the local runtime has no condition language or notification adapter; they must not be silently interpreted as arbitrary operations.

A `BLOCKED` task requires an explicit human choice: submit a solution to a blocked node through `POST /tasks/{id}/input`, redefine the goal through `POST /tasks/{id}/redefine`, cancel it, or delete it. `resume` is intentionally limited to `WAITING`; it never guesses how to repair a blocked graph. Task-level intervention can change task data and graph state, but cannot mutate the assistant runtime or its internal contracts.

## State meanings

- `QUEUED`: task exists and still needs planning.
- `READY`: planning or a previous node transition left executable work.
- `RUNNING`: a node owns a lease and is executing or resolving.
- `VERIFYING`: the current operation result is being checked; it is observable at task and node level.
- `WAITING`: execution is paused for user input or approval.
- `FAILED`: execution reached a terminal action or planning failure; it is not a retry in progress.
- `BLOCKED`: execution cannot continue without a human solution, supported capability, or graph repair.
- `SUCCEEDED`: all required nodes completed and the user-facing result is persisted.
- `CANCELLED`: the user stopped the task; completed output is retained.

`CREATED` is an input/default model state and is not a normal persisted runtime phase. `DECISION`, `CONDITION`, and `NOTIFY` remain extension points; only `CONDITION` and `NOTIFY` are explicitly blocked today, while `DECISION` is resolved through the node resolver until a deterministic decision language exists.
