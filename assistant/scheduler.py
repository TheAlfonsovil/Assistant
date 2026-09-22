from datetime import UTC, datetime

from .domain.graph import TaskGraph
from .domain.models import NodeStatus, TaskNode, TaskStatus


class NodeScheduler:
    """Selects ready graph nodes; lease acquisition remains owned by TaskService."""

    def next_ready_node(
        self,
        task_status: TaskStatus,
        graph: TaskGraph,
        task_deadline: datetime | None = None,
    ) -> TaskNode | None:
        if task_status in {
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.SUCCEEDED,
            TaskStatus.WAITING,
            TaskStatus.BLOCKED,
            TaskStatus.FINALIZING,
        }:
            return None
        now = datetime.now(UTC)
        for node in graph.ready_nodes():
            if node.status is not NodeStatus.READY:
                continue
            node_deadline = self._parse_timestamp(node.metadata.get("deadline"))
            deadlines = [deadline for deadline in (task_deadline, node_deadline) if deadline]
            if deadlines and min(deadlines) <= now:
                continue
            if self._timestamp_is_after(node.metadata.get("next_retry_at"), now):
                continue
            return node
        return None

    @staticmethod
    def _parse_timestamp(value) -> datetime | None:
        if isinstance(value, datetime):
            timestamp = value
        elif value:
            try:
                timestamp = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                return None
        else:
            return None
        return timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp

    @staticmethod
    def _timestamp_is_after(value, now: datetime) -> bool:
        timestamp = NodeScheduler._parse_timestamp(value)
        return timestamp is not None and timestamp > now

    @staticmethod
    def _timestamp_is_at_or_before(value, now: datetime) -> bool:
        timestamp = NodeScheduler._parse_timestamp(value)
        return timestamp is not None and timestamp <= now
