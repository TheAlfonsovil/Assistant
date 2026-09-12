# Task Lifecycle

`REQUEST -> TASK -> PLAN -> GRAPH -> READY -> OPERATION -> TOOL -> RESULT -> VERIFY -> RETRY / REPLAN / BLOCK -> COMPLETE`

A task starts in `QUEUED` with a persisted root node. The scheduler considers nodes, not only tasks. Dependencies must be satisfied and an active lease must not exist. A provider returns a Pydantic-validated proposal. The registry validates tool and method names before executing.

Transient and timeout results can return a node to `READY` while incrementing its retry count. Exhausted or non-retryable failures block the task. Waiting nodes remain persisted across process restarts and can be resumed through the API. Cancellation marks pending nodes cancelled and preserves completed output.
