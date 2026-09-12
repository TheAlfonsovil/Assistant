from __future__ import annotations

from typing import Any

from .domain.graph import TaskGraph
from .domain.models import Operation, OperationResult, Task, TaskNode


class ContextBuilder:
    """Builds role-specific, bounded contexts for each LLM phase."""

    def __init__(self, repository, tools):
        self.repository = repository
        self.tools = tools

    async def for_planner(self, task: Task) -> dict[str, Any]:
        memories = await self.repository.search_memory(task.goal)
        return {
            "phase": "PLANNER",
            "task": {
                "id": task.id,
                "goal": task.goal,
                "description": task.description,
                "source": task.source,
                "priority": task.priority,
                "deadline": task.deadline,
                "metadata": task.metadata,
            },
            "constraints": {
                "max_retries": task.budget.max_retries,
                "max_execution_time": task.budget.max_execution_time,
                "max_tool_calls": task.budget.max_tool_calls,
            },
            "available_tools": [definition.model_dump() for definition in self.tools.definitions()],
            "relevant_memory": [memory.model_dump(mode="json") for memory in memories],
        }

    async def for_resolver(self, task: Task, node: TaskNode, graph: TaskGraph) -> dict[str, Any]:
        dependency_results = []
        for edge in graph.edges:
            if edge.to_node == node.id:
                dependency = graph.nodes[edge.from_node]
                dependency_results.append(
                    {
                        "node_id": dependency.id,
                        "description": dependency.description,
                        "status": dependency.status,
                        "output": dependency.output_data,
                        "error": dependency.error,
                    }
                )
        return {
            "phase": "NODE_RESOLVER",
            "task": {"id": task.id, "goal": task.goal, "status": task.status},
            "node": {
                "id": node.id,
                "type": node.type,
                "description": node.description,
                "input": node.input_data,
                "retry_count": node.retry_count,
                "max_retries": node.max_retries,
                "previous_error": node.error,
            },
            "dependency_results": dependency_results,
            "available_tools": [definition.model_dump() for definition in self.tools.definitions()],
            "constraints": {"deadline": task.deadline, "cancelled": task.status.value == "CANCELLED"},
        }

    async def for_verifier(
        self, task: Task, node: TaskNode, operation: Operation, result: OperationResult
    ) -> dict[str, Any]:
        return {
            "phase": "VERIFIER",
            "task_goal": task.goal,
            "node": {"id": node.id, "description": node.description},
            "operation": operation.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "success_evidence": {
                "exit_code": result.output.get("exit_code")
                if isinstance(result.output, dict)
                else None,
                "output": result.output,
                "artifacts": result.artifacts,
            },
        }

    async def for_replanner(
        self, task: Task, node: TaskNode, graph: TaskGraph, failure: OperationResult
    ) -> dict[str, Any]:
        return {
            "phase": "REPLANNER",
            "task": {"id": task.id, "goal": task.goal, "status": task.status},
            "failed_node": {"id": node.id, "description": node.description, "error": node.error},
            "failure": failure.model_dump(mode="json"),
            "graph": {
                "nodes": [
                    {"id": item.id, "description": item.description, "status": item.status}
                    for item in graph.nodes.values()
                ],
                "edges": [edge.model_dump() for edge in graph.edges],
            },
            "instruction": "Choose a changed strategy; do not repeat the failed operation unchanged.",
        }
