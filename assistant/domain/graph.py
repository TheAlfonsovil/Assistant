from collections import defaultdict

from .errors import GraphCycleError, GraphValidationError
from .models import DependencyType, GraphEdge, NodeStatus, TaskNode


class TaskGraph:
    def __init__(self, nodes: list[TaskNode] | None = None, edges: list[GraphEdge] | None = None):
        self.nodes = {node.id: node for node in nodes or []}
        self.edges = list(edges or [])
        self._validate()

    def _validate(self) -> None:
        for edge in self.edges:
            if edge.from_node not in self.nodes or edge.to_node not in self.nodes:
                raise GraphValidationError("Graph edge references an unknown node")
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        outgoing: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges:
            outgoing[edge.from_node].append(edge.to_node)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise GraphCycleError("Task graph contains a cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for child in outgoing[node_id]:
                visit(child)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in self.nodes:
            visit(node_id)

    def add_node(self, node: TaskNode) -> None:
        if node.id in self.nodes:
            raise GraphValidationError(f"Node already exists: {node.id}")
        self.nodes[node.id] = node
        try:
            self._validate()
        except Exception:
            del self.nodes[node.id]
            raise

    def add_edge(self, edge: GraphEdge) -> None:
        if edge.from_node not in self.nodes or edge.to_node not in self.nodes:
            raise GraphValidationError("Both edge endpoints must exist")
        self.edges.append(edge)
        try:
            self._validate()
        except Exception:
            self.edges.pop()
            raise

    def dependencies_satisfied(self, node_id: str) -> bool:
        for edge in self.edges:
            if edge.to_node != node_id:
                continue
            dependency = self.nodes[edge.from_node]
            if (
                edge.dependency_type is DependencyType.SUCCESS
                and dependency.status is not NodeStatus.SUCCEEDED
                and not (
                    dependency.status is NodeStatus.CANCELLED
                    and dependency.metadata.get("branch_skipped") is True
                )
            ):
                return False
            if (
                edge.dependency_type is DependencyType.FAILURE
                and dependency.status is not NodeStatus.FAILED
            ):
                return False
            if edge.dependency_type is DependencyType.ALWAYS and dependency.status in {
                NodeStatus.CREATED,
                NodeStatus.READY,
                NodeStatus.RUNNING,
                NodeStatus.WAITING,
                NodeStatus.VERIFYING,
            }:
                return False
        return True

    def ready_nodes(self) -> list[TaskNode]:
        return sorted(
            (
                node
                for node in self.nodes.values()
                if node.status is NodeStatus.READY and self.dependencies_satisfied(node.id)
            ),
            key=lambda node: (-node.priority, node.created_at),
        )

    def expand(
        self, parent_id: str, children: list[TaskNode], edges: list[GraphEdge] | None = None
    ) -> None:
        for child in children:
            child.parent_node_id = parent_id
            self.add_node(child)
        for edge in edges or [
            GraphEdge(from_node=parent_id, to_node=child.id) for child in children
        ]:
            self.add_edge(edge)
