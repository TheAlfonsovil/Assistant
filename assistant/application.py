from __future__ import annotations

import logging
import socket
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from httpx import HTTPError
from pydantic import ValidationError
from sqlalchemy import delete

from .context import ContextBuilder
from .domain.graph import TaskGraph
from .domain.models import (
    GraphEdge,
    NodeStatus,
    NodeType,
    OperationResult,
    Task,
    TaskEvent,
    TaskNode,
    TaskRequest,
    TaskStatus,
)
from .infrastructure.orm import IdempotencyRow, LeaseRow
from .infrastructure.repositories import TaskRepository
from .llm import LLMProvider
from .observability import compact
from .tools import ToolRegistry
from .verifier import DeterministicVerifier

logger = logging.getLogger(__name__)


class TaskService:
    def __init__(self, session, llm: LLMProvider, tools: ToolRegistry, verifier=None, event_sink=None):
        self.repository = TaskRepository(session, event_sink=event_sink)
        self.session = session
        self.llm = llm
        self.tools = tools
        self.verifier = verifier or DeterministicVerifier()
        self.context_builder = ContextBuilder(self.repository, tools)
        self.owner = f"{socket.gethostname()}:{id(self)}"

    async def create_task(self, request: TaskRequest) -> Task:
        task = Task.model_validate(request.model_dump())
        task.status = TaskStatus.QUEUED
        root = TaskNode(
            task_id=task.id,
            type=NodeType.TASK,
            description=task.goal,
            status=NodeStatus.READY,
            priority=task.priority,
        )
        await self.repository.save_task(task)
        await self.repository.save_node(root)
        await self.repository.save_event(
            TaskEvent(
                task_id=task.id,
                node_id=root.id,
                event_type="TASK_CREATED",
                    payload={"task_id": task.id, "goal": task.goal},
            )
        )
        await self.repository.save_event(
            TaskEvent(task_id=task.id, node_id=root.id, event_type="NODE_READY")
        )
        return task

    async def get_task(self, task_id: str) -> Task | None:
        return await self.repository.get_task(task_id)

    async def graph(self, task_id: str) -> TaskGraph:
        return TaskGraph(
            await self.repository.list_nodes(task_id), await self.repository.list_edges(task_id)
        )

    async def cancel_task(self, task_id: str, reason: str = "cancelled by user") -> Task | None:
        task = await self.repository.get_task(task_id)
        if task is None:
            return None
        task.status = TaskStatus.CANCELLED
        task.failure_reason = reason
        task.finished_at = datetime.now(UTC)
        await self.repository.save_task(task)
        for node in await self.repository.list_nodes(task_id):
            if node.status in {NodeStatus.CREATED, NodeStatus.READY, NodeStatus.WAITING}:
                node.status = NodeStatus.CANCELLED
                await self.repository.save_node(node)
        await self.repository.save_event(
            TaskEvent(task_id=task_id, event_type="TASK_CANCELLED", payload={"reason": reason})
        )
        return task

    async def resume_task(self, task_id: str) -> Task | None:
        task = await self.repository.get_task(task_id)
        if task and task.status in {TaskStatus.WAITING, TaskStatus.BLOCKED}:
            task.status = TaskStatus.READY
            task.failure_reason = None
            await self.repository.save_task(task)
            for node in await self.repository.list_nodes(task_id):
                if node.status is NodeStatus.WAITING:
                    node.status = NodeStatus.READY
                    await self.repository.save_node(node)
        return task

    async def acquire_lease(self, node_id: str, seconds: int = 300) -> bool:
        current = await self.session.get(LeaseRow, node_id)
        now = datetime.now(UTC)
        if current and current.expires_at > now and current.owner != self.owner:
            return False
        if current:
            current.owner = self.owner
            current.expires_at = now + timedelta(seconds=seconds)
        else:
            self.session.add(
                LeaseRow(
                    node_id=node_id, owner=self.owner, expires_at=now + timedelta(seconds=seconds)
                )
            )
        await self.session.commit()
        return True

    async def release_lease(self, node_id: str) -> None:
        await self.session.execute(
            delete(LeaseRow).where(LeaseRow.node_id == node_id, LeaseRow.owner == self.owner)
        )
        await self.session.commit()

    async def execute_once(self, task_id: str) -> bool:
        task = await self.repository.get_task(task_id)
        if task is None or task.status in {
            TaskStatus.CANCELLED,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
        }:
            return False
        if task.status is TaskStatus.QUEUED:
            task.status = TaskStatus.PLANNING
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(task_id=task_id, event_type="TASK_PLANNED", payload={"status": task.status})
            )
            try:
                planner_context = await self.context_builder.for_planner(task)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        event_type="LLM_REQUEST",
                        payload={"role": "PLANNER", "context": compact(planner_context)},
                    )
                )
                proposal = await self.llm.plan(planner_context)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        event_type="LLM_RESPONSE",
                        payload={
                            "role": "PLANNER",
                            "nodes": [
                                {
                                    "id": item.id,
                                    "description": item.description,
                                    "dependencies": item.dependencies,
                                }
                                for item in proposal.nodes
                            ],
                        },
                    )
                )
                planned_nodes = await self.repository.list_nodes(task.id)
                root_node = next((item for item in planned_nodes if item.type is NodeType.TASK), None)
                node_ids = {proposed.id: str(uuid4()) for proposed in proposal.nodes}
                for proposed in proposal.nodes:
                    node = TaskNode(
                        id=node_ids[proposed.id],
                        task_id=task.id,
                        type=NodeType(proposed.type),
                        description=proposed.description,
                        status=NodeStatus.READY,
                        priority=proposed.priority,
                    )
                    await self.repository.save_node(node)
                    await self.repository.save_event(
                        TaskEvent(
                            task_id=task_id,
                            node_id=node.id,
                            event_type="NODE_CREATED",
                            payload={
                                "description": node.description,
                                "type": node.type,
                                "status": node.status,
                            },
                        )
                    )
                    await self.repository.save_event(
                        TaskEvent(task_id=task_id, node_id=node.id, event_type="NODE_READY")
                    )
                    for dependency in proposed.dependencies:
                        if dependency not in node_ids:
                            raise ValueError(f"Unknown planner dependency: {dependency}")
                        await self.repository.save_edge(task.id, GraphEdge(from_node=node_ids[dependency], to_node=node.id))
                if proposal.nodes and root_node:
                    root_node.status = NodeStatus.SUCCEEDED
                    await self.repository.save_node(root_node)
                task.status = TaskStatus.READY
                await self.repository.save_task(task)
            except (HTTPError, ValidationError, ValueError, KeyError, TypeError) as error:
                task.status = TaskStatus.FAILED
                task.failure_reason = f"planning failed: {error}"
                await self.repository.save_task(task)
                await self.repository.save_event(TaskEvent(task_id=task_id, event_type="TASK_FAILED", payload={"phase": "planning", "error": str(error)}))
                return True
            return True
        graph = await self.graph(task_id)
        ready = graph.ready_nodes()
        await self.repository.save_event(
            TaskEvent(
                task_id=task_id,
                event_type="SCHEDULER_SELECTED",
                payload={"ready_nodes": [item.id for item in ready], "selected": ready[0].id if ready else None},
            )
        )
        if not ready:
            if all(
                node.status in {NodeStatus.SUCCEEDED, NodeStatus.CANCELLED}
                for node in graph.nodes.values()
            ):
                task.status = TaskStatus.SUCCEEDED
                task.result_summary = "All graph nodes completed"
                task.finished_at = datetime.now(UTC)
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(task_id=task_id, event_type="TASK_COMPLETED")
                )
                return False
            return False
        node = ready[0]
        if not await self.acquire_lease(node.id):
            return False
        try:
            task.status = TaskStatus.RUNNING
            task.started_at = task.started_at or datetime.now(UTC)
            node.status = NodeStatus.RUNNING
            await self.repository.save_task(task)
            await self.repository.save_node(node)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task_id,
                    node_id=node.id,
                    event_type="NODE_STARTED",
                    payload={"description": node.description, "status": node.status},
                )
            )
            context = await self.context_builder.for_resolver(task, node, graph)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task_id,
                    node_id=node.id,
                    event_type="LLM_REQUEST",
                    payload={
                        "role": "NODE_RESOLVER",
                        "description": node.description,
                        "context": compact(context),
                    },
                )
            )
            try:
                decision = await self.llm.decide(context)
            except (HTTPError, ValidationError, ValueError, KeyError, TypeError) as error:
                node.status = NodeStatus.FAILED
                node.error = f"node resolution failed: {error}"
                task.status = TaskStatus.FAILED
                task.failure_reason = node.error
                await self.repository.save_node(node)
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="TASK_FAILED",
                        payload={"phase": "node_resolution", "error": str(error)},
                    )
                )
                return True
            await self.repository.save_event(
                TaskEvent(
                    task_id=task_id,
                    node_id=node.id,
                    event_type="LLM_RESPONSE",
                    payload={
                        "role": "NODE_RESOLVER",
                        "action": decision.action,
                        "tool": decision.operation.tool if decision.operation else None,
                        "method": decision.operation.method if decision.operation else None,
                        "args": compact(decision.operation.args) if decision.operation else None,
                        "reason": decision.reason,
                    },
                )
            )
            await self.repository.save_event(
                TaskEvent(
                    task_id=task_id,
                    node_id=node.id,
                    event_type="LLM_CALLED",
                    payload={"action": decision.action},
                )
            )
            if decision.action == "WAIT":
                node.status = NodeStatus.WAITING
                task.status = TaskStatus.WAITING
                await self.repository.save_node(node)
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(task_id=task_id, node_id=node.id, event_type="NODE_WAITING", payload={"reason": decision.reason})
                )
                return True
            if decision.action == "BLOCK":
                node.status = NodeStatus.BLOCKED
                task.status = TaskStatus.BLOCKED
                node.error = decision.reason
                await self.repository.save_node(node)
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(task_id=task_id, node_id=node.id, event_type="NODE_BLOCKED", payload={"reason": decision.reason})
                )
                return True
            if decision.action == "SUBTASKS":
                children = [
                    TaskNode(
                        task_id=task.id,
                        type=NodeType.SUBTASK,
                        description=description,
                        status=NodeStatus.READY,
                    )
                    for description in decision.subtasks
                ]
                for child in children:
                    await self.repository.save_node(child)
                    await self.repository.save_edge(
                        task.id, GraphEdge(from_node=node.id, to_node=child.id)
                    )
                node.status = NodeStatus.SUCCEEDED
                await self.repository.save_node(node)
                return True
            if decision.action == "COMPLETE":
                node.status = NodeStatus.SUCCEEDED
                await self.repository.save_node(node)
                return True
            if decision.operation is None:
                node.status = NodeStatus.BLOCKED
                node.error = "LLM returned no operation"
                await self.repository.save_node(node)
                return True
            operation = decision.operation
            existing = await self.session.get(IdempotencyRow, operation.idempotency_key)
            if existing:
                result = existing.result_json
            else:
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="TOOL_CALLED",
                        payload={"tool": operation.tool, "method": operation.method},
                    )
                )
                result = (await self.tools.execute(operation)).model_dump(mode="json")
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="TOOL_RESULT",
                        payload={
                            "success": result.get("success"),
                            "error": result.get("error"),
                            "error_type": result.get("error_type"),
                            "output": compact(result.get("output")),
                            "side_effects": result.get("side_effects", []),
                        },
                    )
                )
                if result.get("success") or not result.get("retryable"):
                    self.session.add(
                        IdempotencyRow(
                            idempotency_key=operation.idempotency_key, result_json=result
                        )
                    )
                    await self.session.commit()
            verification = self.verifier.verify(OperationResult.model_validate(result))
            await self.repository.save_event(
                TaskEvent(
                    task_id=task_id,
                    node_id=node.id,
                    event_type="NODE_VERIFIED",
                    payload={"decision": verification.decision.value, "reason": verification.reason},
                )
            )
            if verification.decision.value == "SUCCESS":
                node.status = NodeStatus.SUCCEEDED
                node.output_data = result
                await self.repository.save_node(node)
                await self.repository.save_event(
                    TaskEvent(task_id=task_id, node_id=node.id, event_type="NODE_COMPLETED", payload={"status": node.status})
                )
            elif verification.decision.value == "RETRY" and node.retry_count < node.max_retries:
                node.retry_count += 1
                node.status = NodeStatus.READY
                node.error = result.get("error")
                await self.repository.save_node(node)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="RETRY_SCHEDULED",
                        payload={"attempt": node.retry_count},
                    )
                )
            else:
                node.status = NodeStatus.BLOCKED
                node.error = result.get("error")
                task.status = TaskStatus.BLOCKED
                task.failure_reason = node.error
                await self.repository.save_node(node)
                await self.repository.save_task(task)
            return True
        finally:
            await self.release_lease(node.id)

    async def run_task(self, task_id: str, max_steps: int = 100) -> Task | None:
        for _ in range(max_steps):
            progressed = await self.execute_once(task_id)
            task = await self.repository.get_task(task_id)
            if (
                not progressed
                or task is None
                or task.status
                in {
                    TaskStatus.SUCCEEDED,
                    TaskStatus.FAILED,
                    TaskStatus.BLOCKED,
                    TaskStatus.WAITING,
                    TaskStatus.CANCELLED,
                }
            ):
                return task
        task = await self.repository.get_task(task_id)
        if task:
            task.status = TaskStatus.BLOCKED
            task.failure_reason = "execution budget exhausted"
            await self.repository.save_task(task)
        return task
