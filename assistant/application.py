from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

from httpx import HTTPError
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from .context import ContextBuilder
from .domain.graph import TaskGraph
from .domain.models import (
    GraphEdge,
    NodeStatus,
    NodeType,
    OperationResult,
    Project,
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
from .scheduler import NodeScheduler
from .tools import ToolRegistry
from .verifier import DeterministicVerifier

logger = logging.getLogger(__name__)


class TaskService:
    def __init__(
        self,
        session,
        llm: LLMProvider,
        tools: ToolRegistry,
        verifier=None,
        event_sink=None,
        workspace_root: str = ".",
    ):
        self.repository = TaskRepository(session, event_sink=event_sink)
        self.session = session
        self.llm = llm
        self.tools = tools
        self.verifier = verifier or DeterministicVerifier()
        self.scheduler = NodeScheduler()
        self.context_builder = ContextBuilder(self.repository, tools, workspace_root=workspace_root)
        self.owner = f"{socket.gethostname()}:{id(self)}"

    async def create_task(self, request: TaskRequest) -> Task:
        project = await self.repository.resolve_project(request.project_id, request.project_name)
        task_data = request.model_dump(exclude={"project_name"})
        task_data["project_id"] = project.id if project else None
        task = Task.model_validate(task_data)
        if project:
            project.last_used_at = datetime.now(UTC)
            await self.repository.update_project(project)
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

    async def create_project(self, project: Project) -> Project:
        project.path = str(Path(project.path).expanduser().resolve())
        if not Path(project.path).is_dir():
            raise ValueError(f"Project directory does not exist: {project.path}")
        return await self.repository.create_project(project)

    async def get_project(self, project_id: str) -> Project | None:
        return await self.repository.get_project(project_id)

    async def list_projects(self) -> list[Project]:
        return await self.repository.list_projects()

    async def update_project(self, project: Project) -> Project:
        existing = await self.repository.get_project(project.id)
        if existing is None:
            raise KeyError(f"Project not found: {project.id}")
        project.path = str(Path(project.path).expanduser().resolve())
        if not Path(project.path).is_dir():
            raise ValueError(f"Project directory does not exist: {project.path}")
        project.created_at = existing.created_at
        project.last_used_at = existing.last_used_at
        project.last_audited_at = existing.last_audited_at
        return await self.repository.update_project(project)

    async def delete_project(self, project_id: str) -> bool:
        return await self.repository.delete_project(project_id)

    async def create_project_audit_task(self, project_id: str) -> Task | None:
        project = await self.repository.get_project(project_id)
        if project is None or not project.enabled:
            return None
        return await self.create_task(
            TaskRequest(
                goal=project.audit_prompt,
                project_id=project.id,
                metadata={"workflow": "project_audit", "project_name": project.name},
            )
        )

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
        await self._ensure_final_response(task)
        return task

    async def delete_task(self, task_id: str) -> bool:
        task = await self.repository.get_task(task_id)
        if task is None:
            return False
        if task.status in {TaskStatus.PLANNING, TaskStatus.RUNNING}:
            raise ValueError("cancel the running task before deleting it")
        deleted = await self.repository.delete_task(task_id)
        if deleted and self.repository.event_sink:
            await self.repository.event_sink(
                TaskEvent(task_id=task_id, event_type="TASK_DELETED")
            )
        return deleted

    async def redefine_task(
        self, task_id: str, goal: str, description: str = "", metadata: dict | None = None
    ) -> Task | None:
        task = await self.repository.get_task(task_id)
        if task is None:
            return None
        if task.status in {TaskStatus.PLANNING, TaskStatus.RUNNING}:
            raise ValueError("cancel the running task before redefining it")
        nodes = await self.repository.list_nodes(task_id)
        root = next((node for node in nodes if node.type is NodeType.TASK), None)
        if root is None:
            raise ValueError("task root node is missing")
        await self.repository.reset_task_graph(task_id, root.id)
        root.description = goal
        root.status = NodeStatus.READY
        root.output_data = {}
        root.input_data = {}
        root.retry_count = 0
        root.error = None
        root.started_at = None
        root.finished_at = None
        task.goal = goal
        task.description = description
        task.status = TaskStatus.QUEUED
        task.failure_reason = None
        task.result_summary = None
        task.started_at = None
        task.finished_at = None
        task.metadata.pop("final_response", None)
        task.metadata.pop("llm_calls", None)
        task.metadata.pop("tool_calls", None)
        if metadata:
            task.metadata.update(metadata)
        await self.repository.save_task(task)
        await self.repository.save_node(root)
        await self.repository.save_event(
            TaskEvent(
                task_id=task_id,
                node_id=root.id,
                event_type="TASK_REDEFINED",
                payload={"goal": goal},
            )
        )
        return task

    async def resume_task(self, task_id: str) -> Task | None:
        task = await self.repository.get_task(task_id)
        if task and task.status is TaskStatus.BLOCKED:
            raise ValueError("blocked tasks require a solution or redefinition")
        if task and task.status is TaskStatus.WAITING:
            task.status = TaskStatus.READY
            task.failure_reason = None
            await self.repository.save_task(task)
            for node in await self.repository.list_nodes(task_id):
                if node.status is NodeStatus.WAITING:
                    node.status = NodeStatus.READY
                    await self.repository.save_node(node)
        return task

    async def submit_task_input(
        self, task_id: str, input_data: dict[str, object], node_id: str | None = None
    ) -> Task | None:
        task = await self.repository.get_task(task_id)
        if task is None:
            return None
        if task.status not in {TaskStatus.WAITING, TaskStatus.BLOCKED}:
            raise ValueError("task is not waiting for user input or blocked")
        candidate_status = (
            NodeStatus.WAITING if task.status is TaskStatus.WAITING else NodeStatus.BLOCKED
        )
        waiting_nodes = [
            node
            for node in await self.repository.list_nodes(task_id)
            if node.status is candidate_status
        ]
        if node_id:
            waiting_nodes = [node for node in waiting_nodes if node.id == node_id]
        if len(waiting_nodes) != 1:
            raise ValueError("exactly one waiting node must be selected")
        node = waiting_nodes[0]
        previous_error = node.error
        node.input_data.update(input_data)
        node.status = NodeStatus.READY
        node.error = None
        task.status = TaskStatus.READY
        task.failure_reason = None
        task.finished_at = None
        task.metadata.pop("final_response", None)
        await self.repository.save_node(node)
        await self.repository.save_task(task)
        await self.repository.save_event(
            TaskEvent(
                task_id=task_id,
                node_id=node.id,
                event_type=(
                    "USER_INPUT_RECEIVED"
                    if candidate_status is NodeStatus.WAITING
                    else "USER_SOLUTION_RECEIVED"
                ),
                payload={"keys": sorted(input_data), "previous_error": previous_error},
            )
        )
        return task

    async def acquire_lease(self, node_id: str, seconds: int = 300) -> bool:
        current = await self.session.get(LeaseRow, node_id)
        now = datetime.now(UTC)
        if current and current.expires_at > now and current.owner != self.owner:
            return False
        try:
            if current:
                current.owner = self.owner
                current.expires_at = now + timedelta(seconds=seconds)
            else:
                self.session.add(
                    LeaseRow(
                        node_id=node_id,
                        owner=self.owner,
                        expires_at=now + timedelta(seconds=seconds),
                    )
                )
            await self.session.commit()
            return True
        except IntegrityError:
            await self.session.rollback()
            return False

    async def _has_active_running_node(self, graph: TaskGraph) -> bool:
        running_ids = [
            node.id for node in graph.nodes.values() if node.status is NodeStatus.RUNNING
        ]
        if not running_ids:
            return False
        result = await self.session.execute(
            select(LeaseRow.node_id).where(
                LeaseRow.node_id.in_(running_ids),
                LeaseRow.expires_at > datetime.now(UTC),
            )
        )
        return result.first() is not None

    async def release_lease(self, node_id: str) -> None:
        await self.session.execute(
            delete(LeaseRow).where(LeaseRow.node_id == node_id, LeaseRow.owner == self.owner)
        )
        await self.session.commit()

    async def _consume_budget(self, task: Task, key: str, limit: int) -> bool:
        used = int(task.metadata.get(key, 0))
        if used >= limit:
            task.status = TaskStatus.BLOCKED
            task.failure_reason = f"{key} budget exhausted"
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(task_id=task.id, event_type="BUDGET_EXHAUSTED", payload={"budget": key})
            )
            return False

        task.metadata[key] = used + 1
        await self.repository.save_task(task)
        return True

    async def _block_node_for_budget(self, task: Task, node: TaskNode | None, key: str) -> None:
        if node is None:
            return
        node.status = NodeStatus.BLOCKED
        node.error = f"{key} budget exhausted"
        await self.repository.save_node(node)

    @staticmethod
    def _operation_key(task_id: str, node_id: str, operation) -> str:
        payload = json.dumps(
            {
                "task_id": task_id,
                "node_id": node_id,
                "tool": operation.tool,
                "method": operation.method,
                "args": operation.args,
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    async def _ensure_final_response(self, task: Task) -> None:
        if task.status not in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        } or task.metadata.get("final_response") is not None:
            return
        respond = getattr(self.llm, "respond", None)
        if respond is None:
            return
        try:
            events = await self.repository.list_events(task.id)
            response_context = {
                "phase": "FINAL_RESPONSE",
                "user_prompt": task.goal,
                "task": task.model_dump(mode="json"),
                "assistant_state": {"status": task.status.value},
                "events": [
                    {"event": event.event_type, "payload": event.payload}
                    for event in events
                    if event.event_type
                    in {"TOOL_RESULT", "NODE_COMPLETED", "TASK_FAILED", "ACTION_PROPOSED"}
                ],
                "long_term_memory": [],
            }
            response = await asyncio.wait_for(
                respond(response_context),
                timeout=min(60.0, max(1.0, task.budget.max_execution_time)),
            )
            task.metadata["final_response"] = response.model_dump(mode="json")
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(task_id=task.id, event_type="FINAL_RESPONSE_READY")
            )
        except Exception as error:
            logger.exception("Final response generation failed for %s", task.id)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    event_type="FINAL_RESPONSE_FAILED",
                    payload={"error": str(error)},
                )
            )

    async def _finish_task(
        self,
        task: Task,
        status: TaskStatus,
        reason: str,
        event_type: str,
        payload: dict | None = None,
    ) -> Task:
        task.status = status
        task.failure_reason = reason if status is not TaskStatus.SUCCEEDED else task.failure_reason
        task.finished_at = datetime.now(UTC)
        await self.repository.save_task(task)
        await self.repository.save_event(
            TaskEvent(task_id=task.id, event_type=event_type, payload=payload or {"reason": reason})
        )
        await self._ensure_final_response(task)
        return task

    @staticmethod
    async def _call_llm(awaitable, timeout: float | None):
        if timeout is None:
            return await awaitable
        if timeout <= 0:
            raise TimeoutError("task execution time budget exhausted")
        return await asyncio.wait_for(awaitable, timeout=timeout)

    async def _handle_structural_node(
        self, task: Task, node: TaskNode, graph: TaskGraph
    ) -> bool:
        if node.type is NodeType.WAIT:
            if node.input_data:
                node.status = NodeStatus.SUCCEEDED
                node.output_data = {"input": node.input_data}
                await self.repository.save_node(node)
                await self.repository.save_event(
                    TaskEvent(task_id=task.id, node_id=node.id, event_type="NODE_COMPLETED")
                )
            else:
                node.status = NodeStatus.WAITING
                task.status = TaskStatus.WAITING
                await self.repository.save_node(node)
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(task_id=task.id, node_id=node.id, event_type="NODE_WAITING")
                )
            return True
        if node.type is NodeType.VERIFY:
            dependencies = [
                graph.nodes[edge.from_node]
                for edge in graph.edges
                if edge.to_node == node.id
            ]
            if not dependencies or not all(
                dependency.status is NodeStatus.SUCCEEDED for dependency in dependencies
            ):
                node.status = NodeStatus.BLOCKED
                node.error = "verify node has no successful dependencies"
                task.status = TaskStatus.BLOCKED
                task.failure_reason = node.error
                await self.repository.save_node(node)
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task.id,
                        node_id=node.id,
                        event_type="NODE_BLOCKED",
                        payload={"reason": node.error},
                    )
                )
            else:
                node.status = NodeStatus.SUCCEEDED
                node.output_data = {
                    "verified_dependencies": [dependency.id for dependency in dependencies]
                }
                await self.repository.save_node(node)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task.id,
                        node_id=node.id,
                        event_type="NODE_VERIFIED",
                        payload={"decision": "SUCCESS", "kind": "structural"},
                    )
                )
            return True
        if node.type in {NodeType.CONDITION, NodeType.NOTIFY}:
            node.status = NodeStatus.BLOCKED
            node.error = f"{node.type.value.lower()} nodes are not supported by the local runtime"
            task.status = TaskStatus.BLOCKED
            task.failure_reason = node.error
            await self.repository.save_node(node)
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    node_id=node.id,
                    event_type="NODE_BLOCKED",
                    payload={"reason": node.error},
                )
            )
            return True
        return False

    async def execute_once(self, task_id: str, time_remaining: float | None = None) -> bool:
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
                if not await self._consume_budget(task, "llm_calls", task.budget.max_llm_calls):
                    return True
                planner_context = await self.context_builder.for_planner(task)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        event_type="LLM_REQUEST",
                        payload={"role": "PLANNER", "context": compact(planner_context)},
                    )
                )
                proposal = await self._call_llm(self.llm.plan(planner_context), time_remaining)
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
                if proposal.answer is not None and not proposal.nodes:
                    task.status = TaskStatus.SUCCEEDED
                    task.result_summary = proposal.answer
                    task.finished_at = datetime.now(UTC)
                    if root_node:
                        root_node.status = NodeStatus.SUCCEEDED
                        root_node.output_data = {"answer": proposal.answer}
                        await self.repository.save_node(root_node)
                    await self.repository.save_task(task)
                    await self.repository.save_event(
                        TaskEvent(
                            task_id=task_id,
                            event_type="TASK_COMPLETED",
                            payload={"kind": "direct_answer", "answer": proposal.answer},
                        )
                    )
                    return False
                if not proposal.nodes:
                    raise ValueError("planner returned an empty plan")
                if len({proposed.id for proposed in proposal.nodes}) != len(proposal.nodes):
                    raise ValueError("planner returned duplicate node ids")
                node_ids = {proposed.id: str(uuid4()) for proposed in proposal.nodes}
                planned_models = [
                    TaskNode(
                        id=node_ids[proposed.id],
                        task_id=task.id,
                        type=NodeType(proposed.type),
                        description=proposed.description,
                        status=NodeStatus.READY,
                        priority=proposed.priority,
                        max_retries=task.budget.max_retries,
                    )
                    for proposed in proposal.nodes
                ]
                planned_edges = []
                for proposed in proposal.nodes:
                    for dependency in proposed.dependencies:
                        if dependency not in node_ids:
                            raise ValueError(f"Unknown planner dependency: {dependency}")
                        planned_edges.append(
                            GraphEdge(
                                from_node=node_ids[dependency],
                                to_node=node_ids[proposed.id],
                            )
                        )
                TaskGraph(planned_models, planned_edges)
                for node in planned_models:
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
                for edge in planned_edges:
                    await self.repository.save_edge(task.id, edge)
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
        selected = self.scheduler.next_ready_node(task.status, graph)
        ready = graph.ready_nodes()
        await self.repository.save_event(
            TaskEvent(
                task_id=task_id,
                event_type="SCHEDULER_SELECTED",
                payload={
                    "ready_nodes": [item.id for item in ready],
                    "selected": selected.id if selected else None,
                },
            )
        )
        if selected is None and ready:
            now = datetime.now(UTC)
            expired = []
            for candidate in ready:
                deadline = candidate.metadata.get("deadline")
                if not deadline:
                    continue
                try:
                    deadline_at = datetime.fromisoformat(deadline)
                    if deadline_at.tzinfo is None:
                        deadline_at = deadline_at.replace(tzinfo=UTC)
                except (TypeError, ValueError):
                    continue
                if deadline_at <= now:
                    candidate.status = NodeStatus.BLOCKED
                    candidate.error = "node deadline exceeded"
                    candidate.finished_at = now
                    expired.append(candidate)
                    await self.repository.save_node(candidate)
                    await self.repository.save_event(
                        TaskEvent(
                            task_id=task_id,
                            node_id=candidate.id,
                            event_type="NODE_DEADLINE_EXCEEDED",
                        )
                    )
            if expired:
                return True
        if not ready:
            if await self._has_active_running_node(graph):
                task.status = TaskStatus.RUNNING
                task.finished_at = None
                await self.repository.save_task(task)
                return False
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
            if any(node.status is NodeStatus.WAITING for node in graph.nodes.values()):
                task.status = TaskStatus.WAITING
                task.failure_reason = "task is waiting for user input"
            elif any(node.status is NodeStatus.BLOCKED for node in graph.nodes.values()):
                task.status = TaskStatus.BLOCKED
                task.failure_reason = "task has blocked nodes"
            else:
                task.status = TaskStatus.BLOCKED
                task.failure_reason = "task graph made no progress"
            task.finished_at = datetime.now(UTC)
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task_id,
                    event_type="TASK_NO_PROGRESS",
                    payload={"status": task.status, "reason": task.failure_reason},
                )
            )
            return False
        node = selected
        if node is None:
            return False
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
            if await self._handle_structural_node(task, node, graph):
                return True
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
                if not await self._consume_budget(task, "llm_calls", task.budget.max_llm_calls):
                    await self._block_node_for_budget(task, node, "llm_calls")
                    return True
                decision = await self._call_llm(self.llm.decide(context), time_remaining)
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
            if decision.action == "CREATE_ACTION":
                proposal = decision.action_proposal
                if proposal is None or not proposal.code.strip():
                    node.status = NodeStatus.BLOCKED
                    node.error = "LLM requested CREATE_ACTION without reviewable code"
                    task.status = TaskStatus.BLOCKED
                    task.failure_reason = node.error
                    await self.repository.save_node(node)
                    await self.repository.save_task(task)
                    return True
                safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "-", proposal.name).strip("-") or "generated-action"
                extension = {"python": ".py", "powershell": ".ps1", "javascript": ".js"}.get(
                    proposal.language.lower(), ".txt"
                )
                artifact_path = Path("data") / "generated_actions" / f"{safe_name}{extension}"
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_text(proposal.code, encoding="utf-8")
                node.status = NodeStatus.WAITING
                node.output_data = {
                    "action_proposal": proposal.model_dump(mode="json"),
                    "artifact": str(artifact_path),
                    "review_required": True,
                }
                task.status = TaskStatus.WAITING
                await self.repository.save_node(node)
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="ACTION_PROPOSED",
                        payload={"artifact": str(artifact_path), "name": proposal.name},
                    )
                )
                return True
            if decision.operation is None:
                node.status = NodeStatus.BLOCKED
                node.error = "LLM returned no operation"
                await self.repository.save_node(node)
                return True
            operation = decision.operation
            project = await self.repository.get_project(task.project_id) if task.project_id else None
            if project and operation.tool == "project" and operation.method == "analyze":
                operation.args["root"] = project.path
            operation_key = operation.idempotency_key or self._operation_key(
                task.id, node.id, operation
            )
            existing = await self.session.get(IdempotencyRow, operation_key)
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
                if not await self._consume_budget(task, "tool_calls", task.budget.max_tool_calls):
                    await self._block_node_for_budget(task, node, "tool_calls")
                    return True
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
                            idempotency_key=operation_key, result_json=result
                        )
                    )
                    await self.session.commit()
            node.status = NodeStatus.VERIFYING
            await self.repository.save_node(node)
            await self.repository.save_event(
                TaskEvent(task_id=task_id, node_id=node.id, event_type="NODE_VERIFYING")
            )
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
                delay = min(300, 2 ** node.retry_count)
                node.metadata["next_retry_at"] = (
                    datetime.now(UTC) + timedelta(seconds=delay)
                ).isoformat()
                await self.repository.save_node(node)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="RETRY_SCHEDULED",
                        payload={"attempt": node.retry_count, "delay_seconds": delay},
                    )
                )
            elif verification.decision.value == "WAIT_USER":
                node.status = NodeStatus.WAITING
                node.error = result.get("error") or "user input required"
                task.status = TaskStatus.WAITING
                task.failure_reason = node.error
                await self.repository.save_node(node)
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(task_id=task_id, node_id=node.id, event_type="WAITING_FOR_USER", payload={"reason": node.error})
                )
            elif verification.decision.value == "REPLAN":
                failure = OperationResult.model_validate(result)
                replanner_context = await self.context_builder.for_replanner(
                    task, node, graph, failure
                )
                if not await self._consume_budget(
                    task, "llm_calls", task.budget.max_llm_calls
                ):
                    await self._block_node_for_budget(task, node, "llm_calls")
                    return True
                replanned = await self._call_llm(
                    self.llm.replan(replanner_context), time_remaining
                )
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="REPLAN_REQUESTED",
                        payload={"action": replanned.action, "reason": replanned.reason},
                    )
                )
                if replanned.action == "SUBTASKS" and replanned.subtasks:
                    for description in replanned.subtasks:
                        child = TaskNode(
                            task_id=task.id,
                            type=NodeType.SUBTASK,
                            description=description,
                            status=NodeStatus.READY,
                        )
                        await self.repository.save_node(child)
                        await self.repository.save_edge(
                            task.id, GraphEdge(from_node=node.id, to_node=child.id)
                        )
                    node.status = NodeStatus.SUCCEEDED
                    task.status = TaskStatus.READY
                elif replanned.action == "COMPLETE":
                    node.status = NodeStatus.SUCCEEDED
                    task.status = TaskStatus.READY
                else:
                    node.status = NodeStatus.BLOCKED
                    node.error = replanned.reason or "replanning failed"
                    task.status = TaskStatus.BLOCKED
                    task.failure_reason = node.error
                await self.repository.save_node(node)
                await self.repository.save_task(task)
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

    async def run_task(
        self, task_id: str, max_steps: int = 100, wait_for_retry: bool = True
    ) -> Task | None:
        started = monotonic()
        steps = 0
        while steps < max_steps:
            current = await self.repository.get_task(task_id)
            deadline = current.deadline if current else None
            if deadline and deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            if current and deadline and datetime.now(UTC) >= deadline:
                current.status = TaskStatus.BLOCKED
                current.failure_reason = "task deadline exceeded"
                current.finished_at = datetime.now(UTC)
                await self.repository.save_task(current)
                await self.repository.save_event(
                    TaskEvent(task_id=task_id, event_type="TASK_DEADLINE_EXCEEDED")
                )
                await self._ensure_final_response(current)
                return current
            if current and monotonic() - started >= current.budget.max_execution_time:
                return await self._finish_task(
                    current,
                    TaskStatus.BLOCKED,
                    "execution time budget exhausted",
                    "TASK_BUDGET_EXHAUSTED",
                )
            try:
                remaining = current.budget.max_execution_time - (monotonic() - started) if current else None
                progressed = await self.execute_once(task_id, max(0.0, remaining) if remaining is not None else None)
            except TimeoutError as error:
                task = await self.repository.get_task(task_id)
                if task:
                    return await self._finish_task(
                        task,
                        TaskStatus.BLOCKED,
                        str(error) or "execution time budget exhausted",
                        "TASK_BUDGET_EXHAUSTED",
                    )
                return task
            except Exception as error:
                logger.exception("Task execution failed for %s", task_id)
                task = await self.repository.get_task(task_id)
                if task:
                    return await self._finish_task(
                        task,
                        TaskStatus.FAILED,
                        f"execution failed: {error}",
                        "TASK_FAILED",
                        {"error": str(error)},
                    )
                return task
            steps += 1
            task = await self.repository.get_task(task_id)
            if not progressed and wait_for_retry and task is not None:
                retry_times = [
                    node.metadata.get("next_retry_at")
                    for node in await self.repository.list_nodes(task_id)
                    if node.status is NodeStatus.READY and node.metadata.get("next_retry_at")
                ]
                if retry_times:
                    retry_at = datetime.fromisoformat(min(retry_times))
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    delay = max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
                    if delay:
                        await asyncio.sleep(delay)
                        continue
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
                if task is not None:
                    await self._ensure_final_response(task)
                return task
        task = await self.repository.get_task(task_id)
        if task:
            task.status = TaskStatus.BLOCKED
            task.failure_reason = "execution budget exhausted"
            task.finished_at = datetime.now(UTC)
            await self.repository.save_task(task)
            await self._ensure_final_response(task)
        return task
