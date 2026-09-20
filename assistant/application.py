from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from httpx import HTTPError
from pydantic import ValidationError
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from .context import ContextBuilder
from .domain.graph import TaskGraph
from .domain.models import (
    DependencyType,
    ErrorType,
    GraphEdge,
    NodeStatus,
    NodeType,
    Operation,
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
from .llm import AssistantResponse, LLMProvider, PlanNodeProposal, PlanProposal
from .observability import compact
from .planning import plan_coverage_warnings, validate_plan_quality
from .project_analysis import ProjectAnalyzer
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
        projects_root: str = r"C:\Assistant",
        default_execution_time: float | None = None,
        final_response_timeout: float = 36000.0,
        max_steps: int = 1000,
        event_retention_days: int = 30,
        event_retention_keep_recent: int = 1000,
    ):
        self.repository = TaskRepository(session, event_sink=event_sink)
        self.session = session
        self.llm = llm
        self.tools = tools
        self.verifier = verifier or DeterministicVerifier()
        self.scheduler = NodeScheduler()
        self.context_builder = ContextBuilder(
            self.repository,
            tools,
            workspace_root=workspace_root,
            projects_root=projects_root,
        )
        self.projects_root = projects_root
        self.default_execution_time = default_execution_time
        self.final_response_timeout = max(1.0, final_response_timeout)
        self.max_steps = max(1, max_steps)
        self.event_retention_days = max(1, event_retention_days)
        self.event_retention_keep_recent = max(0, event_retention_keep_recent)
        self.owner = f"{socket.gethostname()}:{id(self)}"
        self._cancellation_events: dict[str, asyncio.Event] = {}

    async def create_task(self, request: TaskRequest) -> Task:
        enabled_projects = await self.repository.list_projects(enabled_only=True)
        target = self._requested_target(request)
        project = (
            await self.repository.resolve_project(request.project_id, request.project_name)
            if target is None or target["type"] == "project"
            else None
        )
        if target and target["type"] == "project":
            project = await self.repository.resolve_project(target["id"], None)
            if project is None:
                raise ValueError("requested project was not found or is not enabled")
        if target and target["type"] == "device":
            self._validate_device_target(target["id"])
        if (request.project_id or request.project_name) and project is None:
            raise ValueError("requested project was not found or is not enabled")
        defaults = [item for item in enabled_projects if item.is_default]
        requires_project_selection = (
            target is None
            and not request.project_id
            and not request.project_name
            and len(defaults) != 1
            and len(enabled_projects) > 1
        )
        task_data = request.model_dump(exclude={"project_name", "target_type", "target_id"})
        task_data["project_id"] = project.id if project else None
        task = Task.model_validate(task_data)
        if target:
            task.metadata["target"] = target
        if self.default_execution_time is not None:
            task.budget.max_execution_time = max(1.0, self.default_execution_time)
        if requires_project_selection:
            task.metadata["clarification"] = {
                "kind": "project_selection",
                "options": [
                    {"type": "project", "id": item.id, "name": item.name}
                    for item in enabled_projects
                ] + [{"type": "device", "id": "computer", "name": "Ordenador"}],
            }
        if project:
            project.last_used_at = datetime.now(UTC)
            await self.repository.update_project(project)
        task.status = TaskStatus.WAITING if requires_project_selection else TaskStatus.QUEUED
        root = TaskNode(
            task_id=task.id,
            type=NodeType.TASK,
            description=task.goal,
            status=NodeStatus.WAITING if requires_project_selection else NodeStatus.READY,
            priority=task.priority,
        )
        await self.repository.save_task(task)
        await self.repository.save_node(root)
        await self.repository.save_event(
            TaskEvent(
                task_id=task.id,
                node_id=root.id,
                event_type="TASK_CREATED",
                payload={"task_id": task.id, "goal": task.goal, "status": task.status},
            )
        )
        await self.repository.save_event(TaskEvent(
            task_id=task.id,
            node_id=root.id,
            event_type="NODE_WAITING" if requires_project_selection else "NODE_READY",
            payload=task.metadata.get("clarification", {}),
        ))
        return task

    @staticmethod
    def _requested_target(request: TaskRequest) -> dict[str, str] | None:
        if request.target_type is None and request.target_id is None:
            return None
        target_type = request.target_type or "device"
        target_id = request.target_id or ("computer" if target_type == "device" else "")
        if target_type not in {"project", "device"} or not target_id:
            raise ValueError("target_type must be project or device and target_id is required")
        return {"type": target_type, "id": target_id}

    @staticmethod
    def _validate_device_target(device_id: str) -> None:
        if device_id not in {"computer", "mobile", "home", "robot"}:
            raise ValueError("requested device was not found")

    async def create_project(self, project: Project) -> Project:
        project.path = str(Path(project.path).expanduser().resolve())
        if not Path(project.path).is_dir():
            raise ValueError(f"Project directory does not exist: {project.path}")
        return await self.repository.create_project(project)

    async def get_project(self, project_id: str) -> Project | None:
        return await self.repository.get_project(project_id)

    async def list_projects(self) -> list[Project]:
        await self.reconcile_scaffold_projects()
        return await self.repository.list_projects()

    async def reconcile_scaffold_projects(self) -> int:
        """Recover scaffold projects created before automatic registration existed."""
        root = Path(self.projects_root).expanduser()
        if not root.is_dir():
            return 0
        projects = await self.repository.list_projects()
        known_paths = {str(Path(item.path).resolve()).casefold() for item in projects}
        known_names = {item.name.casefold() for item in projects}
        recovered = 0
        for candidate in root.iterdir():
            if not candidate.is_dir():
                continue
            if not all(
                (candidate / marker).is_file()
                for marker in ("docker-compose.yml", "frontend/package.json", "backend/pom.xml")
            ):
                continue
            resolved = str(candidate.resolve())
            if resolved.casefold() in known_paths or candidate.name.casefold() in known_names:
                continue
            await self.repository.create_project(
                Project(
                    name=candidate.name,
                    path=resolved,
                    description="Scaffold project recovered from the configured projects root",
                    project_type="code",
                )
            )
            known_paths.add(resolved.casefold())
            known_names.add(candidate.name.casefold())
            recovered += 1
        return recovered

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
        project.codegraph = existing.codegraph
        project.codegraph_updated_at = existing.codegraph_updated_at
        project.codegraph_version = existing.codegraph_version
        return await self.repository.update_project(project)

    async def refresh_project_codegraph(self, project_id: str, max_files: int = 500) -> Project | None:
        project = await self.repository.get_project(project_id)
        if project is None:
            return None
        result = await ProjectAnalyzer().analyze(project.path, max_files)
        if not result.success:
            raise ValueError(result.error or "project graph analysis failed")
        project.codegraph = result.output
        project.codegraph_version += 1
        project.codegraph_updated_at = datetime.now(UTC)
        return await self.repository.update_project(project)

    async def delete_project(self, project_id: str) -> bool:
        project = await self.repository.get_project(project_id)
        if project is None:
            return False
        project_path = Path(project.path).resolve()
        projects_root = Path(self.projects_root).expanduser().resolve()
        if project_path == projects_root or project_path.parent != projects_root:
            raise ValueError("project deletion is limited to a direct child of projects_root")
        if project_path.exists():
            if not project_path.is_dir():
                raise ValueError("project path is not a directory")
            await asyncio.to_thread(shutil.rmtree, project_path)
        return await self.repository.delete_project(project_id)

    async def create_project_audit_task(
        self, project_id: str, run_tests: bool = False
    ) -> Task | None:
        project = await self.repository.get_project(project_id)
        if project is None or not project.enabled:
            return None
        goal = project.audit_prompt
        if run_tests:
            if "test" not in goal.casefold():
                goal = f"{goal.rstrip('.')} and execute the detected tests."
        else:
            goal = (
                f"{goal.rstrip('.')} Report detected tests without executing them."
            )
        return await self.create_task(
            TaskRequest(
                goal=goal,
                project_id=project.id,
                metadata={
                    "workflow": "project_audit",
                    "project_name": project.name,
                    "run_tests": run_tests,
                },
            )
        )

    async def get_task(self, task_id: str) -> Task | None:
        return await self.repository.get_task(task_id)

    async def graph(self, task_id: str) -> TaskGraph:
        return TaskGraph(
            await self.repository.list_nodes(task_id), await self.repository.list_edges(task_id)
        )

    async def reconcile_idle(self) -> int:
        repaired = 0
        purge = getattr(self.repository, "purge_expired_memory", None)
        if purge is not None:
            await purge()
        purge_events = getattr(self.repository, "purge_old_events", None)
        if purge_events is not None:
            await purge_events(
                retention_days=self.event_retention_days,
                keep_recent=self.event_retention_keep_recent,
            )
        for task in await self.repository.list_tasks():
            if task.status not in {TaskStatus.READY, TaskStatus.RUNNING}:
                continue
            graph = await self.graph(task.id)
            if task.status is TaskStatus.RUNNING and not await self._has_active_running_node(graph):
                task.status = TaskStatus.READY
                task.finished_at = None
                task.failure_reason = "requeued by idle reconciliation"
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(task_id=task.id, event_type="TASK_RECONCILED")
                )
                repaired += 1
            if not graph.ready_nodes() and not await self._has_active_running_node(graph):
                await self.execute_once(task.id)
                repaired += 1
        return repaired

    async def cancel_task(self, task_id: str, reason: str = "cancelled by user") -> Task | None:
        cancellation = self._cancellation_events.get(task_id)
        if cancellation is not None:
            cancellation.set()
        task = await self.repository.get_task(task_id)
        if task is None:
            return None
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return task
        if cancellation is not None:
            task.status = TaskStatus.CANCELLED
            task.failure_reason = reason
            task.finished_at = datetime.now(UTC)
            return task
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
        if cancellation is None:
            await self._ensure_final_response(task)
        return task

    async def _persist_cancellation(self, task: Task, reason: str = "cancelled by user") -> None:
        task.status = TaskStatus.CANCELLED
        task.failure_reason = reason
        task.finished_at = datetime.now(UTC)
        await self.repository.save_task(task)
        for node in await self.repository.list_nodes(task.id):
            if node.status in {NodeStatus.CREATED, NodeStatus.READY, NodeStatus.WAITING}:
                node.status = NodeStatus.CANCELLED
                await self.repository.save_node(node)
        await self.repository.save_event(
            TaskEvent(task_id=task.id, event_type="TASK_CANCELLED", payload={"reason": reason})
        )

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
        cancellation = self._cancellation_events.pop(task_id, None)
        if cancellation is not None:
            cancellation.clear()
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

    async def replan_task(self, task_id: str) -> Task | None:
        """Run the bounded recovery planner again for a failed or blocked task."""
        task = await self.repository.get_task(task_id)
        if task is None:
            return None
        if task.status not in {TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.WAITING}:
            raise ValueError("only blocked, failed, or waiting tasks can be replanned")
        nodes = await self.repository.list_nodes(task_id)
        candidate = next(
            (
                item
                for item in reversed(nodes)
                if item.status in {NodeStatus.BLOCKED, NodeStatus.FAILED, NodeStatus.WAITING}
            ),
            None,
        )
        if candidate is None:
            raise ValueError("no recoverable node was found")
        graph = await self.graph(task_id)
        recovered = await self._attempt_recovery(task, candidate, candidate.error or task.failure_reason or "user requested replanning", graph)
        if not recovered:
            raise ValueError("replanning budget exhausted or recovery analysis failed")
        return await self.repository.get_task(task_id)

    async def approve_action(self, task_id: str, node_id: str, approved: bool) -> Task | None:
        task = await self.repository.get_task(task_id)
        node = await self.repository.get_node(node_id) if task else None
        if task is None or node is None or node.task_id != task_id:
            return None
        if task.status is not TaskStatus.WAITING or node.status is not NodeStatus.WAITING:
            raise ValueError("task node is not waiting for action review")
        if not node.metadata.get("review_required"):
            raise ValueError("node does not contain a reviewable action")
        if approved:
            node.metadata["review_status"] = "APPROVED"
            node.input_data["approved"] = True
            node.status = NodeStatus.READY
            task.status = TaskStatus.READY
            event_type = "ACTION_APPROVED"
            reason = None
        else:
            node.metadata["review_status"] = "REJECTED"
            node.status = NodeStatus.BLOCKED
            node.error = "generated action rejected by user"
            task.status = TaskStatus.BLOCKED
            task.failure_reason = node.error
            task.finished_at = datetime.now(UTC)
            event_type = "ACTION_REJECTED"
            reason = node.error
        if approved:
            task.finished_at = None
        task.metadata.pop("final_response", None)
        await self.repository.save_node(node)
        await self.repository.save_task(task)
        await self.repository.save_event(
            TaskEvent(
                task_id=task_id,
                node_id=node_id,
                event_type=event_type,
                payload={"approved": approved, "reason": reason},
            )
        )
        return task

    async def resume_task(self, task_id: str) -> Task | None:
        task = await self.repository.get_task(task_id)
        if task and task.status is TaskStatus.BLOCKED:
            raise ValueError("blocked tasks require a solution or redefinition")
        if task and task.status is TaskStatus.WAITING:
            if task.metadata.get("clarification", {}).get("kind") == "project_selection":
                raise ValueError("project selection is required before resuming")
            waiting_nodes = [
                node
                for node in await self.repository.list_nodes(task_id)
                if node.status is NodeStatus.WAITING
            ]
            if len(waiting_nodes) != 1:
                raise ValueError("exactly one waiting node must be selected for resume")
            task.status = TaskStatus.READY
            task.failure_reason = None
            await self.repository.save_task(task)
            waiting_nodes[0].status = NodeStatus.READY
            await self.repository.save_node(waiting_nodes[0])
        return task

    async def submit_task_input(
        self, task_id: str, input_data: dict[str, object], node_id: str | None = None
    ) -> Task | None:
        task = await self.repository.get_task(task_id)
        if task is None:
            return None
        if task.status not in {TaskStatus.WAITING, TaskStatus.BLOCKED}:
            raise ValueError("task is not waiting for user input or blocked")
        clarification = task.metadata.get("clarification", {})
        selecting_project = clarification.get("kind") == "project_selection"
        selected_target = None
        if selecting_project:
            target_type = input_data.get("target_type")
            target_id = input_data.get("target_id")
            if target_type == "device":
                if not isinstance(target_id, str):
                    raise ValueError("input must select a device target")
                self._validate_device_target(target_id)
                task.project_id = None
                selected_target = {"type": "device", "id": target_id}
            else:
                selected = await self.repository.resolve_project(
                    input_data.get("project_id") or target_id, input_data.get("project_name")
                )
                if selected is None:
                    raise ValueError("input must select one enabled project by project_id or project_name")
                task.project_id = selected.id
                selected.last_used_at = datetime.now(UTC)
                await self.repository.update_project(selected)
                selected_target = {"type": "project", "id": selected.id}
            task.metadata["target"] = selected_target
            task.metadata.pop("clarification", None)
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
        task.status = TaskStatus.QUEUED if selecting_project else TaskStatus.READY
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
        now = datetime.now(UTC)
        try:
            result = await self.session.execute(
                update(LeaseRow)
                .where(
                    LeaseRow.node_id == node_id,
                    or_(LeaseRow.owner == self.owner, LeaseRow.expires_at <= now),
                )
                .values(owner=self.owner, expires_at=now + timedelta(seconds=seconds))
            )
            if result.rowcount:
                await self.session.commit()
                return True
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
            result = await self.session.execute(
                update(LeaseRow)
                .where(
                    LeaseRow.node_id == node_id,
                    or_(LeaseRow.owner == self.owner, LeaseRow.expires_at <= now),
                )
                .values(owner=self.owner, expires_at=now + timedelta(seconds=seconds))
            )
            await self.session.commit()
            return bool(result.rowcount)

    async def renew_lease(self, node_id: str, seconds: int = 300) -> bool:
        now = datetime.now(UTC)
        result = await self.session.execute(
            select(LeaseRow).where(
                LeaseRow.node_id == node_id,
                LeaseRow.owner == self.owner,
            )
        )
        lease = result.scalar_one_or_none()
        lease_expires = lease.expires_at if lease else None
        if lease_expires and lease_expires.tzinfo is None:
            lease_expires = lease_expires.replace(tzinfo=UTC)
        if lease is None or lease_expires <= now:
            return False
        lease.expires_at = now + timedelta(seconds=seconds)
        await self.session.commit()
        return True

    async def _execute_tool_with_lease(
        self, node_id: str, operation, lease_seconds: int
    ) -> tuple[OperationResult, bool]:
        stop_renewal = asyncio.Event()
        lease_lost = False
        interval = max(0.1, min(lease_seconds / 3, 30.0))

        async def renew_until_done() -> None:
            nonlocal lease_lost
            while True:
                try:
                    await asyncio.wait_for(stop_renewal.wait(), timeout=interval)
                    return
                except TimeoutError:
                    if not await self.renew_lease(node_id, lease_seconds):
                        lease_lost = True
                        return

        renewal_task = asyncio.create_task(renew_until_done())
        try:
            result = await self.tools.execute(operation)
        finally:
            stop_renewal.set()
            await renewal_task
        return result, not lease_lost

    def _cancellation_event(self, task_id: str) -> asyncio.Event:
        return self._cancellation_events.setdefault(task_id, asyncio.Event())

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

    async def _fail_node(
        self, task: Task, node: TaskNode, reason: str, event_type: str = "NODE_FAILED"
    ) -> None:
        node.status = NodeStatus.FAILED
        node.error = reason
        task.status = TaskStatus.READY
        task.failure_reason = reason
        task.finished_at = None
        await self.repository.save_node(node)
        await self.repository.save_task(task)
        await self.repository.save_event(
            TaskEvent(task_id=task.id, node_id=node.id, event_type=event_type, payload={"error": reason})
        )

    async def _resume_recovery_target(self, task: Task, node: TaskNode) -> None:
        target_id = node.metadata.get("recovery_target_id")
        if not node.metadata.get("recovery_finalize") or not target_id:
            return
        target = await self.repository.get_node(target_id)
        if target is None or target.status is not NodeStatus.FAILED:
            return
        target.status = NodeStatus.READY
        target.retry_count += 1
        target.error = None
        target.metadata.pop("recovery_pending", None)
        await self.repository.save_node(target)
        task.status = TaskStatus.READY
        task.failure_reason = None
        await self.repository.save_task(task)
        await self.repository.save_event(
            TaskEvent(
                task_id=task.id,
                node_id=target.id,
                event_type="RECOVERY_TARGET_REQUEUED",
                payload={"recovery_node": node.id, "attempt": target.retry_count},
            )
        )

    async def _attempt_recovery(
        self,
        task: Task,
        node: TaskNode,
        reason: str,
        graph: TaskGraph,
        time_remaining: float | None = None,
    ) -> bool:
        attempts = int(task.metadata.get("recovery_attempts", 0))
        if attempts >= task.budget.max_recovery_attempts:
            return False
        if not await self._consume_budget(task, "llm_calls", task.budget.max_llm_calls):
            return False
        task.metadata["recovery_attempts"] = attempts + 1
        await self.repository.save_task(task)
        failure = OperationResult(success=False, error=reason, error_type=ErrorType.UNKNOWN)
        context = await self.context_builder.for_replanner(task, node, graph, failure)
        branch_name = f"assistant/recovery/{task.id[:8]}-{attempts + 1}"
        recovery_policy = context["failure_context"]["recovery_policy"]
        recovery_policy["failed_node_id"] = node.id
        recovery_policy["branch_name"] = branch_name
        recovery_policy["deployment"] = "Use only a registered deployment tool; otherwise finish BLOCKED."
        recovery_policy["task_restart_warning"] = (
            "Restart only when preserving the current graph would be unsafe."
        )
        try:
            decision = await self._call_llm(self.llm.replan(context), time_remaining)
        except (
            HTTPError,
            ValidationError,
            KeyError,
            TypeError,
            TimeoutError,
            RuntimeError,
            ValueError,
        ) as error:
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    node_id=node.id,
                    event_type="RECOVERY_ANALYSIS_FAILED",
                    payload={"error": str(error)},
                )
            )
            return False
        await self.repository.save_event(
            TaskEvent(
                task_id=task.id,
                node_id=node.id,
                event_type="RECOVERY_ANALYZED",
                payload={
                    "action": decision.action,
                    "reason": decision.reason,
                    "subtasks": decision.subtasks,
                    "user_input_required": decision.user_input_required,
                },
            )
        )
        if decision.action == "RETRY_NODE":
            node.status = NodeStatus.READY
            node.retry_count += 1
            node.error = reason
            task.status = TaskStatus.READY
            task.failure_reason = None
            await self.repository.save_node(node)
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(task_id=task.id, node_id=node.id, event_type="RECOVERY_NODE_RETRY")
            )
            return True
        if decision.action == "RESTART_TASK":
            root = next(
                (item for item in graph.nodes.values() if item.type is NodeType.TASK), None
            )
            if root is None:
                return False
            await self.repository.reset_task_graph(task.id, root.id)
            root.status = NodeStatus.READY
            root.error = None
            task.status = TaskStatus.QUEUED
            task.failure_reason = None
            task.finished_at = None
            await self.repository.save_node(root)
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(task_id=task.id, event_type="TASK_RECOVERY_RESTARTED")
            )
            return True
        if decision.action == "FIX" and decision.subtasks:
            previous_id = None
            for index, description in enumerate(decision.subtasks):
                recovery_node = TaskNode(
                    task_id=task.id,
                    type=NodeType.SUBTASK,
                    description=description,
                    status=NodeStatus.READY,
                    metadata={
                        "recovery_target_id": node.id,
                        "recovery_finalize": index == len(decision.subtasks) - 1,
                        "recovery_attempt": attempts + 1,
                        "recovery_branch": branch_name,
                    },
                )
                await self.repository.save_node(recovery_node)
                if previous_id:
                    await self.repository.save_edge(
                        task.id,
                        GraphEdge(from_node=previous_id, to_node=recovery_node.id),
                    )
                previous_id = recovery_node.id
            node.metadata["recovery_pending"] = True
            task.status = TaskStatus.READY
            task.failure_reason = reason
            await self.repository.save_node(node)
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    node_id=node.id,
                    event_type="RECOVERY_FIX_BRANCH_CREATED",
                    payload={"steps": len(decision.subtasks)},
                )
            )
            return True
        if decision.action == "BLOCK" and decision.user_input_required:
            node.status = NodeStatus.WAITING
            node.error = decision.reason or "additional user information is required"
            task.status = TaskStatus.WAITING
            task.failure_reason = node.error
            task.finished_at = None
            task.metadata["clarification"] = {
                "kind": "recovery_input",
                "reason": node.error,
                "node_id": node.id,
                "prompt": decision.reason or "Provide the missing information to continue.",
            }
            await self.repository.save_node(node)
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    node_id=node.id,
                    event_type="USER_INPUT_REQUIRED",
                    payload=task.metadata["clarification"],
                )
            )
            return True
        return False

    async def _create_subtask_nodes(self, task: Task, parent: TaskNode, descriptions: list[str]) -> None:
        for description in descriptions:
            child = TaskNode(
                task_id=task.id,
                parent_node_id=parent.id,
                type=NodeType.SUBTASK,
                description=description,
                status=NodeStatus.READY,
                max_retries=task.budget.max_retries,
            )
            await self.repository.save_node(child)
            await self.repository.save_edge(
                task.id, GraphEdge(from_node=parent.id, to_node=child.id)
            )

    @staticmethod
    def _normalize_coverage_plan(task: Task, proposal: PlanProposal) -> PlanProposal:
        """Turn coverage emitted by a weak planner into executable work.

        Coverage is normally supplementary metadata.  When a model emits only
        coverage, retaining those requested work items is safer than asking the
        same model to repeat an unconstrained empty graph.
        """
        descriptions = [item.strip() for item in proposal.coverage if item.strip()]
        if not descriptions:
            return proposal
        nodes = [
            PlanNodeProposal(
                id=f"coverage-{index}",
                description=description,
                type="OPERATION",
            )
            for index, description in enumerate(descriptions, start=1)
        ]
        return proposal.model_copy(update={"task_id": proposal.task_id or task.id, "nodes": nodes})

    @staticmethod
    def _normalize_project_audit_plan(task: Task, proposal: PlanProposal) -> PlanProposal:
        """Keep the dedicated audit workflow focused on its single audit operation."""
        if task.metadata.get("workflow") != "project_audit":
            return proposal
        audit_nodes = [
            node for node in proposal.nodes
            if node.metadata.get("action") == "project.audit"
            or (
                node.metadata.get("operation_hint", {}).get("tool") == "project"
                and node.metadata.get("operation_hint", {}).get("method") == "audit"
            )
            or "auditar el proyecto" in node.description.casefold()
            or "audit the project" in node.description.casefold()
        ]
        if len(audit_nodes) != 1:
            return proposal
        audit = audit_nodes[0]
        audit = audit.model_copy(update={"dependencies": []})
        return proposal.model_copy(update={"nodes": [audit]})

    @staticmethod
    def _normalize_browser_intent(task: Task, proposal: PlanProposal) -> PlanProposal:
        """Prevent a direct-answer response from swallowing a simple browser action."""
        if proposal.answer is None or proposal.nodes or proposal.subtasks:
            return proposal
        fallback = TaskService._fallback_plan(task)
        if fallback is None or not fallback.nodes:
            return proposal
        if fallback.nodes[0].metadata.get("operation_hint", {}).get("tool") != "browser":
            return proposal
        return fallback

    @staticmethod
    def _fallback_plan(task: Task) -> PlanProposal | None:
        goal = task.goal.casefold()
        wants_graph = "codegraph" in goal or "grafo" in goal
        browser_url = None
        if re.search(r"\b(?:abre|abrir|open)\s+youtube\b", goal):
            browser_url = "https://www.youtube.com"
        else:
            supplied_url = re.search(r"https?://[^\s]+", task.goal, flags=re.IGNORECASE)
            if supplied_url and re.search(r"\b(?:abre|abrir|open)\b", goal):
                browser_url = supplied_url.group(0).rstrip(".,;)")
        if browser_url:
            return PlanProposal(
                task_id=task.id,
                coverage=["open the requested public page in the default browser"],
                nodes=[
                    PlanNodeProposal(
                        id="fallback-browser-open",
                        description=f"Abrir {browser_url} en el navegador predeterminado",
                        type="OPERATION",
                        metadata={
                            "operation_hint": {
                                "tool": "browser",
                                "method": "open",
                                "args": {
                                    "url": browser_url,
                                    "origin": f"{task.id}/fallback-browser-open",
                                },
                                "timeout": 60,
                            }
                        },
                    )
                ],
            )
        creation_match = re.search(
            r"\b(?:llamado|llamada|named|name[d]?)\s+['\"]?([a-zA-Z0-9_-]+)",
            task.goal,
            flags=re.IGNORECASE,
        )
        creation_requested = any(
            term in goal
            for term in (
                "crear un nuevo proyecto",
                "crea un nuevo proyecto",
                "create a new project",
                "new project",
            )
        )
        if creation_requested:
            if not creation_match:
                return PlanProposal(
                    task_id=task.id,
                    coverage=["identify the requested project before creating files"],
                    nodes=[
                        PlanNodeProposal(
                            id="fallback-project-creation-input",
                            description="Solicitar el nombre exacto del proyecto antes de crearlo",
                            type="WAIT",
                        )
                    ],
                )
            project_name = creation_match.group(1)
            scaffold_requested = any(
                term in goal for term in ("vue", "spring boot", "springboot", "docker", "frontend", "backend")
            )
            if scaffold_requested:
                return PlanProposal(
                    task_id=task.id,
                    coverage=["scaffold the requested application stack and validate its generated structure"],
                    nodes=[
                        PlanNodeProposal(
                            id="fallback-project-scaffold",
                            description=f"Crear el scaffold funcional de {project_name} con frontend, backend y contenedores",
                            type="OPERATION",
                            acceptance={"fields": {"name": project_name, "scaffolded": True}},
                            metadata={
                                "operation_hint": {
                                    "tool": "project",
                                    "method": "scaffold",
                                    "args": {
                                        "name": project_name,
                                        "frontend": {"framework": "vue"},
                                        "backend": {"language": "java", "java_version": "25", "framework": "spring-boot"},
                                        "containerize": True,
                                        "device": "computer",
                                        "os": "windows-11",
                                    },
                                    "timeout": 300,
                                }
                            },
                        )
                    ],
                )
            return PlanProposal(
                task_id=task.id,
                coverage=["create the requested project directory"],
                nodes=[
                    PlanNodeProposal(
                        id="fallback-project-create",
                        description=f"Crear el proyecto {project_name} en la raíz configurada de proyectos",
                        type="OPERATION",
                        acceptance={"fields": {"name": project_name}},
                        metadata={
                            "operation_hint": {
                                "tool": "project",
                                "method": "create",
                                "args": {"name": project_name, "template": "empty"},
                                "timeout": 300,
                            }
                        },
                    )
                ],
            )
        audit_requested = any(
            term in goal
            for term in (
                "audit",
                "audita",
                "auditar",
                "review",
                "revisa",
                "revisar",
                "inspect",
                "inspecciona",
                "analiza",
                "analyze",
                "sonar",
            )
        )
        tests_requested = any(
            term in goal
            for term in (
                "run tests",
                "run the tests",
                "execute tests",
                "execute the tests",
                "ejecuta los tests",
                "ejecutar los tests",
                "corre los tests",
                "correr los tests",
                "testea",
            )
        )
        if not audit_requested and not wants_graph:
            return None
        nodes = []
        if wants_graph:
            nodes.append(
                PlanNodeProposal(
                    id="fallback-codegraph-build",
                    description="Actualizar el codegraph del proyecto y conservar sus relaciones estructurales",
                    type="OPERATION",
                    metadata={
                        "operation_hint": {
                            "tool": "codegraph",
                            "method": "build",
                            "args": {"max_files": 500},
                            "timeout": 300,
                        }
                    },
                )
            )
        nodes.append(
            PlanNodeProposal(
                id="fallback-project-inspection",
                description=(
                    "Auditar el proyecto: estructura, configuración, dependencias, tests detectados y limitaciones"
                ),
                type="OPERATION",
                dependencies=["fallback-codegraph-build"] if wants_graph else [],
                metadata={
                    "operation_hint": {
                        "tool": "project",
                        "method": "audit",
                        "args": {"max_files": 500, "run_tests": tests_requested},
                        "timeout": 300,
                    }
                },
            )
        )
        return PlanProposal(
            task_id=task.id,
            coverage=["audit the project and collect executable evidence"],
            nodes=nodes,
        )

    async def _complete_direct_answer(self, task: Task, root_node: TaskNode, answer: str) -> None:
        task.status = TaskStatus.SUCCEEDED
        task.result_summary = answer
        task.finished_at = datetime.now(UTC)
        root_node.status = NodeStatus.SUCCEEDED
        root_node.output_data = {"answer": answer}
        await self.repository.save_node(root_node)
        task.metadata["final_response"] = AssistantResponse(
            response_type="answer",
            title="Respuesta",
            summary=answer,
            evidence=["Respuesta directa del planner; no se requirieron acciones externas."],
            confidence="high",
        ).model_dump(mode="json")
        await self.repository.save_task(task)
        await self.repository.save_event(
            TaskEvent(
                task_id=task.id,
                event_type="TASK_COMPLETED",
                payload={"kind": "direct_answer", "answer": answer},
            )
        )

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
        } or (
            task.metadata.get("final_response") is not None
            and not task.metadata.get("final_response_pending")
        ):
            return
        final_status = task.status
        task.metadata["_final_status"] = final_status.value
        task.metadata["_final_finished_at"] = task.finished_at.isoformat() if task.finished_at else None
        task.status = TaskStatus.FINALIZING
        task.finished_at = None
        await self.repository.save_task(task)
        respond = getattr(self.llm, "respond", None)
        if respond is None:
            await self._save_fallback_response(task, "final response provider unavailable")
            await self._restore_final_status(task)
            return
        used_llm_calls = int(task.metadata.get("llm_calls", 0))
        if used_llm_calls >= task.budget.max_llm_calls:
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    event_type="LLM_SKIPPED",
                    payload={"role": "FINAL_RESPONSE", "reason": "llm_calls budget exhausted"},
                )
            )
            await self._save_fallback_response(task, "final response budget exhausted")
            await self._restore_final_status(task)
            return
        task.metadata["llm_calls"] = used_llm_calls + 1
        nodes = await self.repository.list_nodes(task.id)
        task.metadata["final_response"] = AssistantResponse(
            response_type="report",
            title="Resultado de la tarea",
            summary=task.result_summary or task.failure_reason or "La tarea terminó; el informe LLM está en curso.",
            evidence=[
                f"{node.description}: {node.status.value}"
                for node in nodes
                if node.status in {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.BLOCKED}
            ][:20],
            limitations=["El informe final del LLM todavía está generándose."],
            confidence="low",
        ).model_dump(mode="json")
        task.metadata["final_response_pending"] = True
        await self.repository.save_task(task)
        await self.repository.save_event(
            TaskEvent(
                task_id=task.id,
                event_type="FINAL_RESPONSE_STARTED",
                payload={"status": "pending"},
            )
        )
        try:
            events = await self.repository.list_events(task.id)
            response_context = await self.context_builder.for_final_response(
                task,
                [
                    event
                    for event in events
                    if event.event_type
                    in {
                        "TOOL_RESULT",
                        "NODE_COMPLETED",
                        "NODE_FAILED",
                        "TASK_FAILED",
                        "ACTION_PROPOSED",
                        "RECOVERY_ANALYZED",
                        "RECOVERY_FIX_BRANCH_CREATED",
                        "RECOVERY_TARGET_REQUEUED",
                        "TASK_RECOVERY_RESTARTED",
                        "RETRY_SCHEDULED",
                        "NODE_VERIFIED",
                        "NODE_BLOCKED",
                        "OPERATION_REJECTED",
                        "LLM_ERROR",
                        "PLAN_REPAIRED",
                        "BUDGET_EXHAUSTED",
                    }
                ],
            )
            response = await asyncio.wait_for(
                respond(response_context),
                timeout=min(
                    self.final_response_timeout,
                    max(1.0, task.budget.max_execution_time),
                ),
            )
            task.metadata["final_response"] = response.model_dump(mode="json")
            task.metadata.pop("final_response_pending", None)
            if task.result_summary:
                task.metadata["final_response"]["summary"] = task.result_summary
            else:
                task.result_summary = response.summary
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    event_type="LLM_REQUEST",
                    payload={
                        "role": "FINAL_RESPONSE",
                        "request": getattr(self.llm, "last_request", {}),
                    },
                )
            )
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    event_type="LLM_RESPONSE",
                    payload={
                        "role": "FINAL_RESPONSE",
                        "response": response.model_dump(mode="json"),
                        "request": getattr(self.llm, "last_request", {}),
                        "response_chars": len(json.dumps(response.model_dump(mode="json"), default=str)),
                        "usage": getattr(self.llm, "last_usage", {}),
                    },
                )
            )
            await self.repository.save_event(
                TaskEvent(task_id=task.id, event_type="FINAL_RESPONSE_READY")
            )
            await self._restore_final_status(task)
        except Exception as error:
            logger.exception("Final response generation failed for %s", task.id)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    event_type="FINAL_RESPONSE_FAILED",
                    payload={
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "request": getattr(self.llm, "last_request", {}),
                        "usage": getattr(self.llm, "last_usage", {}),
                    },
                )
            )
            await self._save_fallback_response(task, "final response generation failed")
            await self._restore_final_status(task)

    async def _restore_final_status(self, task: Task) -> None:
        status = task.metadata.pop("_final_status", None)
        finished_at = task.metadata.pop("_final_finished_at", None)
        if status is None:
            return
        task.status = TaskStatus(status)
        task.finished_at = datetime.fromisoformat(finished_at) if finished_at else datetime.now(UTC)
        await self.repository.save_task(task)

    async def _save_fallback_response(self, task: Task, reason: str) -> None:
        if task.metadata.get("final_response") is not None and not task.metadata.get("final_response_pending"):
            return
        task.metadata.pop("final_response_pending", None)
        nodes = await self.repository.list_nodes(task.id)
        evidence = [
            f"{node.description}: {node.status.value}"
            for node in nodes
            if node.status in {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.BLOCKED}
        ]
        summary = task.result_summary or task.failure_reason or "Task finished without a generated report"
        task.metadata["final_response"] = AssistantResponse(
            response_type="blocked" if task.metadata.get("_final_status") == TaskStatus.BLOCKED.value or task.status is TaskStatus.BLOCKED else "report",
            title="Task result",
            summary=summary,
            evidence=evidence[:20],
            limitations=[reason],
            confidence="low",
        ).model_dump(mode="json")
        await self.repository.save_task(task)
        await self.repository.save_event(
            TaskEvent(
                task_id=task.id,
                event_type="FINAL_RESPONSE_FALLBACK",
                payload={"reason": reason},
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
        # Persist the terminal intent before final-report generation. If the
        # provider is unavailable, the task must not remain indefinitely in
        # PLANNING/RUNNING while the fallback report is assembled.
        await self.repository.save_task(task)
        await self._ensure_final_response(task)
        await self.repository.save_event(
            TaskEvent(task_id=task.id, event_type=event_type, payload=payload or {"reason": reason})
        )
        return task

    async def _persist_project_operation_result(self, task: Task, operation, result: dict[str, Any]) -> None:
        if not result.get("success"):
            return
        if not isinstance(result.get("output"), dict):
            return
        output = result["output"]
        if operation.tool == "project" and operation.method == "scaffold":
            path = output.get("path")
            name = output.get("name")
            if not isinstance(path, str) or not isinstance(name, str) or not Path(path).is_dir():
                return
            existing = next(
                (
                    item
                    for item in await self.repository.list_projects()
                    if item.path.casefold() == str(Path(path).resolve()).casefold()
                    or item.name.casefold() == name.casefold()
                ),
                None,
            )
            project = existing or Project(
                name=name,
                path=path,
                description=f"Scaffold generated by task {task.id}",
                project_type="code",
            )
            if existing is None:
                project = await self.repository.create_project(project)
            task.project_id = project.id
            task.metadata["created_project"] = {
                "project_id": project.id,
                "name": project.name,
                "path": project.path,
            }
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    event_type="PROJECT_REGISTERED",
                    payload={
                        "project_id": project.id,
                        "name": project.name,
                        "path": project.path,
                    },
                )
            )
            return
        project = await self.repository.get_project(task.project_id) if task.project_id else None
        if project is None:
            return
        event_type = None
        if operation.tool == "codegraph" and operation.method == "build" and output.get("graph"):
            project.codegraph = output
            project.codegraph_version += 1
            project.codegraph_updated_at = datetime.now(UTC)
            event_type = "PROJECT_CODEGRAPH_UPDATED"
        if operation.tool == "project" and operation.method == "audit" and output.get("audit") is not None:
            project.last_audited_at = datetime.now(UTC)
            event_type = "PROJECT_AUDITED"
        if event_type:
            await self.repository.update_project(project)
            await self.repository.save_event(
                TaskEvent(task_id=task.id, event_type=event_type, payload={"project_id": project.id})
            )

    async def _deterministic_result_summary(self, task: Task) -> str:
        nodes = await self.repository.list_nodes(task.id)
        completed = [node.description for node in nodes if node.status is NodeStatus.SUCCEEDED]
        failed = [node.description for node in nodes if node.status is NodeStatus.FAILED]
        if failed:
            return f"Task finished with failures: {', '.join(failed[:3])}"
        if not completed:
            return "Task completed without executable node evidence"
        suffix = "" if len(completed) <= 3 else f" (+{len(completed) - 3} more)"
        return f"Completed {len(completed)} node(s): {', '.join(completed[:3])}{suffix}"

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
        if node.type in {NodeType.CONDITION, NodeType.DECISION}:
            try:
                result = self._evaluate_condition(node, graph)
            except (TypeError, ValueError) as error:
                node.status = NodeStatus.BLOCKED
                node.error = f"invalid condition: {error}"
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
            node.output_data = {"decision" if node.type is NodeType.DECISION else "condition": result}
            node.status = NodeStatus.SUCCEEDED
            await self.repository.save_node(node)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    node_id=node.id,
                    event_type="DECISION_EVALUATED" if node.type is NodeType.DECISION else "CONDITION_EVALUATED",
                    payload={"result": result, "operator": node.metadata.get("operator", "truthy")},
                )
            )
            branch_key = "skip_on_true" if result else "skip_on_false"
            for target_id in node.metadata.get(branch_key, []):
                await self._cancel_branch(task, graph, target_id, node.id)
            task.status = TaskStatus.READY
            await self.repository.save_task(task)
            return True
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
            if not dependencies or not graph.dependencies_satisfied(node.id):
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
        return False

    @staticmethod
    def _read_path(value, path: str | None):
        if not path:
            return value
        current = value
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        return current

    def _evaluate_condition(self, node: TaskNode, graph: TaskGraph) -> bool:
        metadata = node.metadata
        if isinstance(metadata.get("value"), bool):
            left = metadata["value"]
        else:
            source = graph.nodes.get(metadata.get("source_node_id"))
            container = source.output_data if source else node.input_data
            left = self._read_path(container, metadata.get("field"))
        operator = metadata.get("operator", "truthy")
        right = metadata.get("right")
        if operator == "truthy":
            return bool(left)
        if operator == "falsy":
            return not bool(left)
        if operator == "equals":
            return left == right
        if operator == "not_equals":
            return left != right
        if operator == "contains":
            return right in left if left is not None else False
        if operator == "greater_than":
            return left > right
        if operator == "less_than":
            return left < right
        raise ValueError(f"unsupported condition operator: {operator}")

    async def _cancel_branch(
        self, task: Task, graph: TaskGraph, target_id: str, branch_source_id: str
    ) -> None:
        pending = [target_id]
        visited = set()
        while pending:
            current_id = pending.pop()
            if current_id in visited or current_id not in graph.nodes:
                continue
            visited.add(current_id)
            current = graph.nodes[current_id]
            incoming = [
                edge.from_node
                for edge in graph.edges
                if edge.to_node == current_id
                and edge.from_node != branch_source_id
                and edge.from_node not in visited
                and graph.nodes[edge.from_node].status is not NodeStatus.CANCELLED
            ]
            if incoming:
                continue
            if current.status in {NodeStatus.CREATED, NodeStatus.READY, NodeStatus.WAITING}:
                current.status = NodeStatus.CANCELLED
                current.error = "branch skipped by condition"
                current.metadata["branch_skipped"] = True
                await self.repository.save_node(current)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task.id,
                        node_id=current.id,
                        event_type="NODE_SKIPPED",
                        payload={"reason": current.error},
                    )
                )
            pending.extend(
                edge.to_node for edge in graph.edges if edge.from_node == current_id
            )

    async def execute_once(self, task_id: str, time_remaining: float | None = None) -> bool:
        task = await self.repository.get_task(task_id)
        if task is None or task.status in {
            TaskStatus.CANCELLED,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
        }:
            return False
        cancellation = self._cancellation_event(task_id)
        if cancellation.is_set():
            return False
        if task.status in {TaskStatus.QUEUED, TaskStatus.PLANNING}:
            was_already_planning = task.status is TaskStatus.PLANNING
            task.status = TaskStatus.PLANNING
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task_id,
                    event_type="TASK_PLANNING_RETRY" if was_already_planning else "TASK_PLANNED",
                    payload={"status": task.status, "reason": "recovered planning state"}
                    if was_already_planning
                    else {"status": task.status},
                )
            )
            try:
                if not await self._consume_budget(task, "llm_calls", task.budget.max_llm_calls):
                    return True
                planner_context = await self.context_builder.for_planner(task)
                try:
                    proposal = await self._call_llm(self.llm.plan(planner_context), time_remaining)
                except TimeoutError as error:
                    task.status = TaskStatus.BLOCKED
                    task.failure_reason = str(error) or "execution time budget exhausted"
                    task.finished_at = datetime.now(UTC)
                    await self.repository.save_task(task)
                    await self.repository.save_event(
                        TaskEvent(
                            task_id=task_id,
                            event_type="TASK_BUDGET_EXHAUSTED",
                            payload={"reason": task.failure_reason, "phase": "planning"},
                        )
                    )
                    return True
                if cancellation.is_set():
                    await self._persist_cancellation(task)
                    return False
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        event_type="LLM_REQUEST",
                        payload={
                            "role": "PLANNER",
                            "request": getattr(self.llm, "last_request", {}),
                        },
                    )
                )
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        event_type="LLM_RESPONSE",
                        payload={
                            "role": "PLANNER",
                            "response": proposal.model_dump(mode="json"),
                            "request": getattr(self.llm, "last_request", {}),
                            "nodes": [
                                {
                                    "id": item.id,
                                    "description": item.description,
                                    "dependencies": item.dependencies,
                                    "acceptance": item.acceptance,
                                }
                                for item in proposal.nodes
                            ],
                            "response_chars": len(json.dumps(proposal.model_dump(mode="json"), default=str)),
                            "usage": getattr(self.llm, "last_usage", {}),
                        },
                    )
                )
                if (
                    proposal.answer is None
                    and not proposal.nodes
                    and not proposal.subtasks
                    and await self._consume_budget(task, "llm_calls", task.budget.max_llm_calls)
                ):
                    retry_context = dict(planner_context)
                    retry_context["planner_feedback"] = (
                        "INVALID PLAN: coverage is descriptive only. Return executable nodes "
                        "or subtasks, or provide a direct answer in answer."
                    )
                    await self.repository.save_event(
                        TaskEvent(
                            task_id=task_id,
                            event_type="PLANNER_RETRY_REQUESTED",
                            payload={"reason": "empty executable plan"},
                        )
                    )
                    proposal = await self._call_llm(
                        self.llm.plan(retry_context), time_remaining
                    )
                    await self.repository.save_event(
                        TaskEvent(
                            task_id=task_id,
                            event_type="LLM_RESPONSE",
                            payload={
                                "role": "PLANNER_RETRY",
                                "response": proposal.model_dump(mode="json"),
                                "request": getattr(self.llm, "last_request", {}),
                                "response_chars": len(json.dumps(proposal.model_dump(mode="json"), default=str)),
                                "usage": getattr(self.llm, "last_usage", {}),
                            },
                        )
                    )
                proposal = self._normalize_browser_intent(task, proposal)
                if proposal.answer is None and not proposal.nodes and not proposal.subtasks:
                    normalized = self._normalize_coverage_plan(task, proposal)
                    if normalized.nodes:
                        proposal = normalized
                        await self.repository.save_event(
                            TaskEvent(
                                task_id=task_id,
                                event_type="PLAN_NORMALIZED",
                                payload={
                                    "reason": "coverage-only planner response",
                                    "nodes": [item.id for item in proposal.nodes],
                                },
                            )
                        )
                proposal = self._normalize_project_audit_plan(task, proposal)
                try:
                    validate_plan_quality(proposal, task.budget.max_plan_nodes)
                except ValueError as error:
                    if (
                        proposal.answer is None
                        and not proposal.nodes
                        and not proposal.subtasks
                    ):
                        proposal = self._fallback_plan(task)
                        if proposal is None:
                            raise ValueError(
                                "planner returned an empty plan for a task that cannot be "
                                "safely recovered deterministically"
                            )
                        await self.repository.save_event(
                            TaskEvent(
                                task_id=task_id,
                                event_type="PLAN_REPAIRED",
                                payload={
                                    "reason": str(error),
                                    "strategy": "deterministic_project_inspection",
                                    "nodes": [item.id for item in proposal.nodes],
                                },
                            )
                        )
                    else:
                        raise
                coverage_warnings = plan_coverage_warnings(proposal, task.goal)
                for warning in coverage_warnings:
                    await self.repository.save_event(
                        TaskEvent(
                            task_id=task_id,
                            event_type="PLAN_COVERAGE_WARNING",
                            payload={"warning": warning},
                        )
                    )
                planned_nodes = await self.repository.list_nodes(task.id)
                root_node = next((item for item in planned_nodes if item.type is NodeType.TASK), None)
                if proposal.answer is not None and not proposal.nodes:
                    if root_node is not None:
                        await self._complete_direct_answer(task, root_node, proposal.answer)
                    return False
                if not proposal.nodes:
                    if proposal.subtasks and root_node is not None:
                        await self._create_subtask_nodes(task, root_node, proposal.subtasks)
                        root_node.status = NodeStatus.SUCCEEDED
                        await self.repository.save_node(root_node)
                        task.status = TaskStatus.READY
                        await self.repository.save_task(task)
                        await self.repository.save_event(
                            TaskEvent(
                                task_id=task_id,
                                node_id=root_node.id,
                                event_type="PLAN_DECOMPOSED",
                                payload={"original_nodes": 0, "subtasks": len(proposal.subtasks)},
                            )
                        )
                        return True
                    raise ValueError("planner returned an empty plan")
                if len(proposal.nodes) > task.budget.max_plan_nodes:
                    if not proposal.subtasks:
                        raise ValueError(
                            f"planner returned {len(proposal.nodes)} nodes; "
                            f"decomposition into at most {task.budget.max_plan_nodes} subtasks is required"
                        )
                    if root_node is None:
                        raise ValueError("task root node is missing")
                    await self._create_subtask_nodes(task, root_node, proposal.subtasks)
                    root_node.status = NodeStatus.SUCCEEDED
                    await self.repository.save_node(root_node)
                    task.status = TaskStatus.READY
                    await self.repository.save_task(task)
                    await self.repository.save_event(
                        TaskEvent(
                            task_id=task_id,
                            node_id=root_node.id,
                            event_type="PLAN_DECOMPOSED",
                            payload={
                                "original_nodes": len(proposal.nodes),
                                "subtasks": len(proposal.subtasks),
                            },
                        )
                    )
                    return True
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
                        metadata={
                            **proposed.metadata,
                            **({"acceptance": proposed.acceptance} if proposed.acceptance else {}),
                        },
                        max_retries=task.budget.max_retries,
                    )
                    for proposed in proposal.nodes
                ]
                for proposed, planned in zip(proposal.nodes, planned_models):
                    for key in ("skip_on_false", "skip_on_true"):
                        if key in proposed.metadata:
                            planned.metadata[key] = [
                                node_ids.get(target, target)
                                for target in proposed.metadata[key]
                            ]
                planned_edges = []
                for proposed in proposal.nodes:
                    for dependency in proposed.dependencies:
                        if dependency not in node_ids:
                            raise ValueError(f"Unknown planner dependency: {dependency}")
                        dependency_type = proposed.dependency_types.get(
                            dependency, DependencyType.SUCCESS
                        )
                        planned_edges.append(
                            GraphEdge(
                                from_node=node_ids[dependency],
                                to_node=node_ids[proposed.id],
                                dependency_type=dependency_type,
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
            except (HTTPError, ValidationError, ValueError, KeyError, TypeError, TimeoutError, RuntimeError) as error:
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        event_type="LLM_ERROR",
                        payload={
                            "role": "PLANNER",
                            "error_type": type(error).__name__,
                            "error": str(error),
                            "request": getattr(self.llm, "last_request", {}),
                            "usage": getattr(self.llm, "last_usage", {}),
                        },
                    )
                )
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
                task.result_summary = await self._deterministic_result_summary(task)
                task.finished_at = datetime.now(UTC)
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(task_id=task_id, event_type="TASK_COMPLETED")
                )
                return False
            if any(node.status is NodeStatus.FAILED for node in graph.nodes.values()):
                task.status = TaskStatus.FAILED
                task.failure_reason = "task has failed nodes"
                task.finished_at = datetime.now(UTC)
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        event_type="TASK_FAILED",
                        payload={"reason": task.failure_reason},
                    )
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
        if cancellation.is_set():
            return False
        lease_seconds = max(300, int((time_remaining or 300) + 60))
        if not await self.acquire_lease(node.id, seconds=lease_seconds):
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
            try:
                if not await self._consume_budget(task, "llm_calls", task.budget.max_llm_calls):
                    await self._block_node_for_budget(task, node, "llm_calls")
                    return True
                decision = await self._call_llm(self.llm.decide(context), time_remaining)
            except (HTTPError, ValidationError, ValueError, KeyError, TypeError, TimeoutError, RuntimeError) as error:
                reason = f"node resolution failed: {error}"
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="LLM_ERROR",
                        payload={
                            "role": "NODE_RESOLVER",
                            "error_type": type(error).__name__,
                            "error": str(error),
                            "request": getattr(self.llm, "last_request", {}),
                            "usage": getattr(self.llm, "last_usage", {}),
                        },
                    )
                )
                await self._fail_node(task, node, reason)
                await self._attempt_recovery(task, node, reason, graph, time_remaining)
                return True
            if cancellation.is_set():
                node.status = NodeStatus.CANCELLED
                await self.repository.save_node(node)
                await self._persist_cancellation(task)
                return True
            await self.repository.save_event(
                TaskEvent(
                    task_id=task_id,
                    node_id=node.id,
                    event_type="LLM_REQUEST",
                    payload={
                        "role": "NODE_RESOLVER",
                        "request": getattr(self.llm, "last_request", {}),
                    },
                )
            )
            await self.repository.save_event(
                TaskEvent(
                    task_id=task_id,
                    node_id=node.id,
                    event_type="LLM_RESPONSE",
                    payload={
                        "role": "NODE_RESOLVER",
                        "response": decision.model_dump(mode="json"),
                        "request": getattr(self.llm, "last_request", {}),
                        "action": decision.action,
                        "tool": decision.operation.tool if decision.operation else None,
                        "method": decision.operation.method if decision.operation else None,
                        "args": compact(decision.operation.args) if decision.operation else None,
                        "reason": decision.reason,
                            "response_chars": len(json.dumps(decision.model_dump(mode="json"), default=str)),
                            "usage": getattr(self.llm, "last_usage", {}),
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
                await self._resume_recovery_target(task, node)
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
                node.metadata["review_required"] = True
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
                task.status = TaskStatus.BLOCKED
                task.failure_reason = node.error
                task.finished_at = datetime.now(UTC)
                await self.repository.save_node(node)
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="NODE_BLOCKED",
                        payload={"reason": node.error},
                    )
                )
                return True
            operation = decision.operation
            operation_hint = node.metadata.get("operation_hint")
            if isinstance(operation_hint, dict):
                operation = Operation.model_validate(operation_hint)
            tool_definition = self.tools.definition(operation.tool)
            if tool_definition is not None:
                await self.renew_lease(
                    node.id, seconds=max(300, int(operation.timeout) + 60)
                )
            acceptance = node.metadata.get("acceptance")
            if acceptance:
                operation.metadata.setdefault("expected", acceptance)
            project = await self.repository.get_project(task.project_id) if task.project_id else None
            if operation.tool == "project" and operation.method == "create":
                operation.args.setdefault("root", self.projects_root)
            elif operation.tool == "project" and operation.method == "scaffold":
                # scaffold owns the final child path: it creates <root>/<name>.
                # Never let an LLM-provided root duplicate the project name.
                operation.args["root"] = self.projects_root
            elif operation.tool in {"project", "codegraph"} and operation.method in {"analyze", "audit", "build", "system"}:
                operation.args["root"] = project.path if project else self.context_builder.workspace_root
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
                operation_error = self.tools.validate_operation(operation)
                if operation_error:
                    node.status = NodeStatus.BLOCKED
                    node.error = operation_error
                    task.status = TaskStatus.BLOCKED
                    task.failure_reason = operation_error
                    task.finished_at = datetime.now(UTC)
                    await self.repository.save_node(node)
                    await self.repository.save_task(task)
                    await self.repository.save_event(
                        TaskEvent(
                            task_id=task_id,
                            node_id=node.id,
                            event_type="OPERATION_REJECTED",
                            payload={"error": operation_error},
                        )
                    )
                    return True
                operation_result, lease_held = await self._execute_tool_with_lease(
                    node.id, operation, max(300, int(operation.timeout) + 60)
                )
                if not lease_held:
                    await self._fail_node(task, node, "node lease lost during tool execution")
                    return True
                if cancellation.is_set():
                    node.status = NodeStatus.CANCELLED
                    node.output_data = operation_result.model_dump(mode="json")
                    task.status = TaskStatus.CANCELLED
                    task.failure_reason = "cancelled by user"
                    task.finished_at = datetime.now(UTC)
                    await self.repository.save_node(node)
                    await self.repository.save_task(task)
                    await self._ensure_final_response(task)
                    return True
                result = operation_result.model_dump(mode="json")
                if (
                    result.get("retryable")
                    and tool_definition is not None
                    and not tool_definition.idempotent
                    and not operation.idempotency_key
                ):
                    result["retryable"] = False
                    result.setdefault("metadata", {})["retry_blocked"] = (
                        "non-idempotent operation requires an explicit idempotency key"
                    )
                await self._persist_project_operation_result(task, operation, result)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="TOOL_RESULT",
                        payload={
                            "success": result.get("success"),
                            "tool": operation.tool,
                            "method": operation.method,
                            "error": result.get("error"),
                            "error_type": result.get("error_type"),
                            "output": compact(result.get("output")),
                            "duration": result.get("duration"),
                            "side_effects": result.get("side_effects", []),
                        },
                    )
                )
                if result.get("success") or not result.get("retryable"):
                    await self.repository.save_idempotency_result(operation_key, result)
            node.status = NodeStatus.VERIFYING
            task.status = TaskStatus.VERIFYING
            if not await self.renew_lease(node.id, lease_seconds):
                await self._fail_node(task, node, "node lease expired before verification")
                return True
            await self.repository.save_node(node)
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(task_id=task_id, node_id=node.id, event_type="NODE_VERIFYING")
            )
            verification_result = OperationResult.model_validate(result)
            acceptance = node.metadata.get("acceptance")
            if acceptance:
                verification_result.metadata["expected"] = acceptance
            verification = self.verifier.verify(verification_result)
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
                task.status = TaskStatus.READY
                node.output_data = result
                await self.repository.save_node(node)
                await self.repository.save_task(task)
                await self._resume_recovery_target(task, node)
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
                task.status = TaskStatus.READY
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="RETRY_SCHEDULED",
                        payload={"attempt": node.retry_count, "delay_seconds": delay},
                    )
                )
            elif verification.decision.value == "RETRY":
                reason = result.get("error") or "retry limit exhausted"
                await self._fail_node(task, node, reason)
                await self._attempt_recovery(task, node, reason, graph, time_remaining)
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
            elif verification.decision.value == "BLOCK":
                node.status = NodeStatus.BLOCKED
                node.error = verification.reason or result.get("error") or "operation blocked by verifier"
                node.output_data = result
                task.status = TaskStatus.BLOCKED
                task.failure_reason = node.error
                task.finished_at = datetime.now(UTC)
                await self.repository.save_node(node)
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="NODE_BLOCKED",
                        payload={"reason": node.error, "source": "verifier"},
                    )
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
                try:
                    replanned = await self._call_llm(
                        self.llm.replan(replanner_context), time_remaining
                    )
                except (HTTPError, ValidationError, ValueError, KeyError, TypeError, TimeoutError) as error:
                    reason = f"replanning failed: {error}"
                    node.status = NodeStatus.BLOCKED
                    node.error = reason
                    task.status = TaskStatus.BLOCKED
                    task.failure_reason = reason
                    task.finished_at = datetime.now(UTC)
                    await self.repository.save_node(node)
                    await self.repository.save_task(task)
                    await self.repository.save_event(
                        TaskEvent(
                            task_id=task_id,
                            node_id=node.id,
                            event_type="REPLAN_FAILED",
                            payload={"error": str(error)},
                        )
                    )
                    return True
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="REPLAN_REQUESTED",
                        payload={"action": replanned.action, "reason": replanned.reason},
                    )
                )
                await self._apply_replan_decision(task, node, replanned)
            else:
                reason = result.get("error") or "operation failed"
                await self._fail_node(task, node, reason)
                await self._attempt_recovery(task, node, reason, graph, time_remaining)
            return True
        finally:
            await self.release_lease(node.id)

    async def _apply_replan_decision(
        self, task: Task, node: TaskNode, decision
    ) -> bool:
        if decision.action == "SUBTASKS" and decision.subtasks:
            for description in decision.subtasks:
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
        elif decision.action == "COMPLETE":
            node.status = NodeStatus.SUCCEEDED
            task.status = TaskStatus.READY
        elif decision.action == "RETRY_NODE":
            node.status = NodeStatus.READY
            node.retry_count += 1
            node.error = decision.reason
            task.status = TaskStatus.READY
            task.failure_reason = None
        elif decision.action == "RESTART_TASK":
            graph = await self.graph(task.id)
            root = next((item for item in graph.nodes.values() if item.type is NodeType.TASK), None)
            if root is None:
                return False
            await self.repository.reset_task_graph(task.id, root.id)
            root.status = NodeStatus.READY
            root.error = None
            task.status = TaskStatus.QUEUED
            task.failure_reason = None
            task.finished_at = None
            await self.repository.save_node(root)
        elif decision.action == "FIX" and decision.subtasks:
            previous_id = None
            for index, description in enumerate(decision.subtasks):
                recovery_node = TaskNode(
                    task_id=task.id,
                    type=NodeType.SUBTASK,
                    description=description,
                    status=NodeStatus.READY,
                    metadata={
                        "recovery_target_id": node.id,
                        "recovery_finalize": index == len(decision.subtasks) - 1,
                    },
                )
                await self.repository.save_node(recovery_node)
                if previous_id:
                    await self.repository.save_edge(
                        task.id, GraphEdge(from_node=previous_id, to_node=recovery_node.id)
                    )
                previous_id = recovery_node.id
            node.metadata["recovery_pending"] = True
            task.status = TaskStatus.READY
        elif decision.action == "BLOCK" and decision.user_input_required:
            node.status = NodeStatus.WAITING
            node.error = decision.reason or "additional user information is required"
            task.status = TaskStatus.WAITING
            task.failure_reason = node.error
            task.finished_at = None
            task.metadata["clarification"] = {
                "kind": "recovery_input",
                "reason": node.error,
                "node_id": node.id,
                "prompt": decision.reason or "Provide the missing information to continue.",
            }
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    node_id=node.id,
                    event_type="USER_INPUT_REQUIRED",
                    payload=task.metadata["clarification"],
                )
            )
        else:
            node.status = NodeStatus.BLOCKED
            node.error = decision.reason or "replanning returned no executable strategy"
            task.status = TaskStatus.BLOCKED
            task.failure_reason = node.error
            task.finished_at = datetime.now(UTC)
        await self.repository.save_node(node)
        await self.repository.save_task(task)
        return True

    async def run_task(
        self, task_id: str, max_steps: int | None = None, wait_for_retry: bool = True
    ) -> Task | None:
        started = monotonic()
        steps = 0
        step_limit = max(1, max_steps if max_steps is not None else self.max_steps)
        while steps < step_limit:
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
                    if remaining is not None:
                        delay = min(delay, max(0.0, remaining))
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
            ) and task is not None:
                await self._ensure_final_response(task)
                self._cancellation_events.pop(task.id, None)
                return task
        task = await self.repository.get_task(task_id)
        if task:
            graph = await self.graph(task_id)
            if await self._has_active_running_node(graph):
                task.status = TaskStatus.RUNNING
                task.failure_reason = None
                task.finished_at = None
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task.id,
                        event_type="TASK_STEP_LIMIT_DEFERRED",
                        payload={"max_steps": max_steps},
                    )
                )
                return task
            for node in graph.nodes.values():
                if node.status in {NodeStatus.CREATED, NodeStatus.READY, NodeStatus.WAITING}:
                    node.status = NodeStatus.BLOCKED
                    node.error = "execution step limit reached"
                    node.finished_at = datetime.now(UTC)
                    await self.repository.save_node(node)
            task.status = TaskStatus.BLOCKED
            task.failure_reason = "maximum execution steps reached"
            task.finished_at = datetime.now(UTC)
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    event_type="TASK_STEP_LIMIT_EXCEEDED",
                    payload={"max_steps": max_steps},
                )
            )
            await self._ensure_final_response(task)
            self._cancellation_events.pop(task.id, None)
        return task
