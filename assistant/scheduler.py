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
        for node in graph.ready_nodes():
            if node.status is NodeStatus.READY and (
                not node.metadata.get("deadline")
                or datetime.now(UTC).isoformat() <= node.metadata["deadline"]
            ):
                return node
        return None
