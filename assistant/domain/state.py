from __future__ import annotations

from .models import NodeStatus, TaskStatus


class InvalidStateTransition(ValueError):
    """Raised when persisted state skips an allowed lifecycle transition."""


_TASK_TRANSITIONS = {
    TaskStatus.CREATED: {TaskStatus.QUEUED, TaskStatus.WAITING},
    TaskStatus.QUEUED: {TaskStatus.PLANNING, TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.PLANNING: {TaskStatus.QUEUED, TaskStatus.READY, TaskStatus.FINALIZING, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.READY: {TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.FINALIZING, TaskStatus.SUCCEEDED, TaskStatus.CANCELLED, TaskStatus.QUEUED},
    TaskStatus.RUNNING: {TaskStatus.READY, TaskStatus.WAITING, TaskStatus.VERIFYING, TaskStatus.FINALIZING, TaskStatus.SUCCEEDED, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.VERIFYING: {TaskStatus.READY, TaskStatus.WAITING, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.FINALIZING, TaskStatus.SUCCEEDED, TaskStatus.CANCELLED},
    TaskStatus.WAITING: {TaskStatus.READY, TaskStatus.QUEUED, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.QUEUED, TaskStatus.FINALIZING, TaskStatus.CANCELLED},
    TaskStatus.FAILED: {TaskStatus.READY, TaskStatus.QUEUED, TaskStatus.FINALIZING, TaskStatus.CANCELLED},
    TaskStatus.FINALIZING: {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.SUCCEEDED: {TaskStatus.FINALIZING},
    TaskStatus.CANCELLED: {TaskStatus.FINALIZING},
}

_NODE_TRANSITIONS = {
    NodeStatus.CREATED: {NodeStatus.READY, NodeStatus.WAITING, NodeStatus.CANCELLED},
    NodeStatus.READY: {NodeStatus.RUNNING, NodeStatus.WAITING, NodeStatus.BLOCKED, NodeStatus.FAILED, NodeStatus.SUCCEEDED, NodeStatus.CANCELLED},
    NodeStatus.RUNNING: {NodeStatus.READY, NodeStatus.WAITING, NodeStatus.VERIFYING, NodeStatus.BLOCKED, NodeStatus.FAILED, NodeStatus.SUCCEEDED, NodeStatus.CANCELLED},
    NodeStatus.VERIFYING: {NodeStatus.READY, NodeStatus.WAITING, NodeStatus.BLOCKED, NodeStatus.FAILED, NodeStatus.SUCCEEDED, NodeStatus.CANCELLED},
    NodeStatus.WAITING: {NodeStatus.READY, NodeStatus.BLOCKED, NodeStatus.CANCELLED, NodeStatus.SUCCEEDED},
    NodeStatus.BLOCKED: {NodeStatus.READY, NodeStatus.CANCELLED},
    NodeStatus.FAILED: {NodeStatus.READY, NodeStatus.CANCELLED},
    NodeStatus.SUCCEEDED: set(),
    NodeStatus.CANCELLED: set(),
}


def validate_task_transition(previous: TaskStatus, current: TaskStatus) -> None:
    if previous != current and current not in _TASK_TRANSITIONS[previous]:
        raise InvalidStateTransition(f"task cannot transition from {previous} to {current}")


def validate_node_transition(previous: NodeStatus, current: NodeStatus) -> None:
    if previous != current and current not in _NODE_TRANSITIONS[previous]:
        raise InvalidStateTransition(f"node cannot transition from {previous} to {current}")
