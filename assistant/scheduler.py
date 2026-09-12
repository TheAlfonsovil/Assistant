from datetime import UTC, datetime

from .domain.graph import TaskGraph
from .domain.models import NodeStatus, TaskNode, TaskStatus


class NodeScheduler:
    """Selects ready graph nodes; lease acquisition remains owned by TaskService."""

    def next_ready_node(self, task_status: TaskStatus, graph: TaskGraph) -> TaskNode | None:
        if task_status in {
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.SUCCEEDED,
            TaskStatus.WAITING,
            TaskStatus.BLOCKED,
        }:
            return None
        now = datetime.now(UTC)
        for node in graph.ready_nodes():
            if node.status is not NodeStatus.READY:
                continue
            if self._timestamp_is_at_or_before(node.metadata.get("deadline"), now):
                continue
            if self._timestamp_is_after(node.metadata.get("next_retry_at"), now):
                continue
            return node
        return None

    @staticmethod
    def _timestamp_is_after(value, now: datetime) -> bool:
        if not value:
            return False
        try:
            timestamp = datetime.fromisoformat(value)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            return timestamp > now
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _timestamp_is_at_or_before(value, now: datetime) -> bool:
        if not value:
            return False
        try:
            timestamp = datetime.fromisoformat(value)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            return timestamp <= now
        except (TypeError, ValueError):
            return False
