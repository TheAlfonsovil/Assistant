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

from ..context import ContextBuilder
from ..domain.contracts import (
    ArtifactKind,
    ArtifactRef,
    NodeContract,
    OperationHint,
)
from ..domain.graph import TaskGraph
from ..domain.models import (
    AgentDecision,
    AgentDecisionType,
    DependencyType,
    ErrorType,
    GraphEdge,
    NodeStatus,
    NodeType,
    Operation,
    OperationResult,
    Project,
    RecoveryExpansion,
    Task,
    TaskEvent,
    TaskNode,
    TaskRequest,
    TaskStatus,
)
from ..infrastructure.orm import IdempotencyRow, LeaseRow
from ..infrastructure.repositories import TaskRepository
from ..llm import (
    AssistantResponse,
    LLMProvider,
    NodeDecision,
    OrchestratorDecision,
    PlanNodeProposal,
    PlanProposal,
)
from ..observability import compact
from ..planning import plan_coverage_warnings, validate_plan_quality
from ..project_analysis import ProjectAnalyzer
from ..scheduler import NodeScheduler
from ..tools import ToolRegistry
from ..verifier import DeterministicVerifier

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

    async def _publish_operation_artifacts(
        self,
        task_id: str,
        node_id: str,
        artifacts: list[Any],
        output_specs: list[dict[str, Any]] | None = None,
    ) -> None:
        save_artifact = getattr(self.repository, "save_artifact", None)
        if not save_artifact or not isinstance(artifacts, list):
            return
        unnamed_specs = [
            item for item in (output_specs or [])
            if isinstance(item, dict) and item.get("name")
        ]
        published: list[ArtifactRef] = []
        for raw in artifacts:
            if isinstance(raw, ArtifactRef):
                artifact = raw.model_copy(update={"producer_node_id": node_id})
            elif isinstance(raw, dict):
                kind_value = raw.get("kind", ArtifactKind.FILE)
                try:
                    kind = ArtifactKind(kind_value)
                except ValueError:
                    kind = ArtifactKind.FILE
                metadata = {
                    **raw.get("metadata", {}),
                    **({"name": raw["name"]} if raw.get("name") else {}),
                }
                if not raw.get("name") and len(artifacts) == 1 and len(unnamed_specs) == 1:
                    metadata["name"] = unnamed_specs[0]["name"]
                artifact = ArtifactRef(
                    id=raw.get("id", str(uuid4())),
                    kind=kind,
                    description=raw.get("description") or raw.get("path") or "tool output",
                    producer_node_id=node_id,
                    path=raw.get("path"),
                    checksum=raw.get("checksum"),
                    version=raw.get("version", 1),
                    metadata=metadata,
                )
            else:
                continue
            await save_artifact(task_id, artifact)
            published.append(artifact)
        if published and hasattr(self.repository, "get_task"):
            task = await self.repository.get_task(task_id)
            if task is not None:
                known = {item.id for item in task.working_memory.artifact_refs}
                task.working_memory.artifact_refs.extend(
                    artifact for artifact in published if artifact.id not in known
                )
                await self.repository.save_task(task)

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
        if project:
            task.metadata.setdefault("project_path", project.path)
        if target:
            task.runtime.target = target
        if self._supports_agent_mode():
            task.metadata["execution_mode"] = "agent"
            task.runtime.workflow = "agent"
        if project and self._is_project_audit_request(task.goal):
            if task.runtime.workflow is None:
                task.runtime.workflow = "project_audit"
            task.runtime.run_tests = True
        if self.default_execution_time is not None:
            task.budget.max_execution_time = max(1.0, self.default_execution_time)
        if requires_project_selection:
            task.runtime.clarification = {
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
            payload=task.runtime.clarification,
        ))
        return task

    @staticmethod
    def _is_project_audit_request(goal: str) -> bool:
        normalized = goal.casefold()
        return any(
            phrase in normalized
            for phrase in (
                "audita el proyecto",
                "auditar el proyecto",
                "audita este proyecto",
                "auditar este proyecto",
                "audit the project",
                "audit this project",
                "revisa el proyecto",
                "revisar el proyecto",
                "review the project",
            )
        )

    def _supports_agent_mode(self) -> bool:
        agent_decide = getattr(self.llm, "agent_decide", None)
        if not callable(agent_decide):
            return False
        from ..llm import MockLLMProvider

        if isinstance(self.llm, MockLLMProvider):
            return type(self.llm) is not MockLLMProvider and "agent_decide" in type(self.llm).__dict__
        return True

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
        await self.reconcile_workspace_projects()
        return await self.repository.list_projects()

    async def reconcile_workspace_projects(self) -> int:
        """Recover initialized workspaces created before automatic registration."""
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
            is_workspace = (candidate / ".assistant" / "project.json").is_file()
            if not is_workspace:
                continue
            resolved = str(candidate.resolve())
            if resolved.casefold() in known_paths or candidate.name.casefold() in known_names:
                continue
            await self.repository.create_project(
                Project(
                    name=candidate.name,
                    path=resolved,
                    description="Workspace recovered from the configured projects root",
                    project_type="workspace",
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
        task = await self.create_task(
            TaskRequest(
                goal=goal,
                project_id=project.id,
                metadata={"project_name": project.name},
            )
        )
        task.runtime.workflow = "agent"
        task.metadata["execution_mode"] = "agent"
        task.runtime.run_tests = True if run_tests else task.runtime.run_tests
        await self.repository.save_task(task)
        return task

    async def get_task(self, task_id: str) -> Task | None:
        return await self.repository.get_task(task_id)

    async def dashboard_tasks(self, limit: int = 100, status: str | None = None) -> list[Task]:
        return await self.repository.list_dashboard_tasks(limit=limit, status=status)

    async def dashboard_task_detail(self, task_id: str) -> dict[str, object] | None:
        task = await self.repository.get_task(task_id)
        if task is None:
            return None
        return {
            "task": task,
            "nodes": await self.repository.list_nodes(task_id),
            "edges": await self.repository.list_edges(task_id),
            "events": await self.repository.list_events(task_id),
        }

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
        task.status = TaskStatus.CANCELLED
        task.failure_reason = reason
        task.finished_at = datetime.now(UTC)
        await self.repository.save_task(task)
        for node in await self.repository.list_nodes(task_id):
            if node.status in {
                NodeStatus.CREATED,
                NodeStatus.READY,
                NodeStatus.WAITING,
                NodeStatus.RUNNING,
                NodeStatus.VERIFYING,
            }:
                node.status = NodeStatus.CANCELLED
                await self.repository.save_node(node)
        await self.repository.save_event(
            TaskEvent(task_id=task_id, event_type="TASK_CANCELLED", payload={"reason": reason})
        )
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
        task.runtime.final_response = None
        task.runtime.llm_calls = 0
        task.runtime.tool_calls = 0
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
        if not node.runtime.review_required:
            raise ValueError("node does not contain a reviewable action")
        if approved:
            node.runtime.review_status = "APPROVED"
            node.input_data["approved"] = True
            node.status = NodeStatus.READY
            task.status = TaskStatus.READY
            event_type = "ACTION_APPROVED"
            reason = None
        else:
            node.runtime.review_status = "REJECTED"
            node.status = NodeStatus.BLOCKED
            node.error = "generated action rejected by user"
            task.status = TaskStatus.BLOCKED
            task.failure_reason = node.error
            task.finished_at = datetime.now(UTC)
            event_type = "ACTION_REJECTED"
            reason = node.error
        if approved:
            task.finished_at = None
        task.runtime.final_response = None
        await self.repository.save_node(node)
        await self.repository.save_task(task)
        await self._mark_recovery_expansion_failed(node, node.error)
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
            if task.runtime.clarification.get("kind") == "project_selection":
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
        clarification = task.runtime.clarification
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
            task.runtime.target = selected_target
            task.runtime.clarification = {}
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
        task.runtime.final_response = None
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
        used = int(getattr(task.runtime, key, 0))
        if used >= limit:
            task.status = TaskStatus.BLOCKED
            task.failure_reason = f"{key} budget exhausted"
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(task_id=task.id, event_type="BUDGET_EXHAUSTED", payload={"budget": key})
            )
            return False

        setattr(task.runtime, key, used + 1)
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
        await self._mark_recovery_expansion_failed(node, reason)

    async def _mark_recovery_expansion_failed(self, node: TaskNode, reason: str) -> None:
        expansion_id = node.runtime.recovery_expansion_id
        if not expansion_id:
            return
        expansion = await self.repository.get_recovery_expansion_by_id(str(expansion_id))
        if expansion is None or expansion.status in {"FAILED", "SUCCEEDED"}:
            return
        expansion.status = "FAILED"
        expansion.reason = reason
        expansion.finished_at = datetime.now(UTC)
        await self.repository.save_recovery_expansion(expansion)
        await self.repository.save_event(
            TaskEvent(
                task_id=node.task_id,
                node_id=node.id,
                event_type="RECOVERY_EXPANSION_FAILED",
                payload={"expansion_id": expansion.id, "reason": reason},
            )
        )

    async def _resume_recovery_target(self, task: Task, node: TaskNode) -> None:
        target_id = node.runtime.recovery_target_id
        if not node.runtime.recovery_finalize or not target_id:
            return
        target = await self.repository.get_node(target_id)
        if target is None or target.status is not NodeStatus.FAILED:
            return
        expansion_id = node.runtime.recovery_expansion_id
        if expansion_id:
            expansion = await self.repository.get_recovery_expansion_by_id(expansion_id)
            if expansion is not None:
                expansion.status = "SUCCEEDED"
                expansion.finished_at = datetime.now(UTC)
                await self.repository.save_recovery_expansion(expansion)
        target.status = NodeStatus.READY
        target.retry_count += 1
        target.error = None
        target.runtime.recovery_pending = False
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

    async def _expand_recovery_graph(
        self,
        task: Task,
        target: TaskNode,
        subtasks: list[str],
        attempt: int,
        branch_name: str | None = None,
        reason: str = "",
    ) -> RecoveryExpansion:
        """Create one idempotent and traceable recovery branch."""
        existing = await self.repository.get_recovery_expansion(task.id, target.id, attempt)
        if existing is not None:
            return existing
        if not subtasks:
            raise ValueError("recovery expansion requires at least one subtask")
        existing_nodes = await self.repository.list_nodes(task.id)
        if len(existing_nodes) + len(subtasks) > task.budget.max_plan_nodes:
            raise ValueError(
                "recovery expansion exceeds the task max_plan_nodes budget"
            )
        expansion = RecoveryExpansion(
            task_id=task.id,
            target_node_id=target.id,
            attempt=attempt,
            strategy="FIX",
            status="RUNNING",
            branch_name=branch_name,
            reason=reason,
        )
        await self.repository.save_recovery_expansion(expansion)
        previous_id = None
        for index, description in enumerate(subtasks):
            recovery_node = TaskNode(
                task_id=task.id,
                parent_node_id=target.id,
                type=NodeType.SUBTASK,
                description=description,
                status=NodeStatus.READY,
                metadata={"recovery_branch": branch_name},
                runtime={
                    "recovery_target_id": target.id,
                    "recovery_expansion_id": expansion.id,
                    "recovery_finalize": index == len(subtasks) - 1,
                    "recovery_attempt": attempt,
                },
            )
            await self.repository.save_node(recovery_node)
            expansion.node_ids.append(recovery_node.id)
            if previous_id:
                await self.repository.save_edge(
                    task.id,
                    GraphEdge(
                        from_node=previous_id,
                        to_node=recovery_node.id,
                        condition=f"recovery_expansion:{expansion.id}",
                    ),
                )
            previous_id = recovery_node.id
        expansion.final_node_id = previous_id
        await self.repository.save_recovery_expansion(expansion)
        target.runtime.recovery_pending = True
        target.runtime.recovery_expansion_id = expansion.id
        await self.repository.save_node(target)
        await self.repository.save_event(
            TaskEvent(
                task_id=task.id,
                node_id=target.id,
                event_type="GRAPH_EXPANDED",
                payload={
                    "expansion_id": expansion.id,
                    "strategy": expansion.strategy,
                    "attempt": attempt,
                    "created_node_ids": expansion.node_ids,
                    "final_node_id": expansion.final_node_id,
                },
            )
        )
        return expansion

    async def _attempt_recovery(
        self,
        task: Task,
        node: TaskNode,
        reason: str,
        graph: TaskGraph,
        time_remaining: float | None = None,
    ) -> bool:
        attempts = task.runtime.recovery_attempts
        if attempts >= task.budget.max_recovery_attempts:
            return False
        if not await self._consume_budget(task, "llm_calls", task.budget.max_llm_calls):
            return False
        task.runtime.recovery_attempts = attempts + 1
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
            await self._persist_llm_request(task.id, "REPLANNER", context, NodeDecision, node.id)
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
            try:
                expansion = await self._expand_recovery_graph(
                    task,
                    node,
                    decision.subtasks,
                    attempts + 1,
                    branch_name=branch_name,
                    reason=reason,
                )
            except ValueError as error:
                node.status = NodeStatus.BLOCKED
                node.error = str(error)
                task.status = TaskStatus.BLOCKED
                task.failure_reason = str(error)
                await self.repository.save_node(node)
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task.id,
                        node_id=node.id,
                        event_type="RECOVERY_EXPANSION_BLOCKED",
                        payload={"reason": str(error)},
                    )
                )
                return True
            task.status = TaskStatus.READY
            task.failure_reason = reason
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    node_id=node.id,
                    event_type="RECOVERY_FIX_BRANCH_CREATED",
                    payload={"steps": len(decision.subtasks), "expansion_id": expansion.id},
                )
            )
            return True
        if decision.action == "BLOCK" and decision.user_input_required:
            node.status = NodeStatus.WAITING
            node.error = decision.reason or "additional user information is required"
            task.status = TaskStatus.WAITING
            task.failure_reason = node.error
            task.finished_at = None
            task.runtime.clarification = {
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
                    payload=task.runtime.clarification,
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
        """Keep audit evidence deterministic while preserving useful agent stages.

        The planner may add bounded reads, graph queries, or applicable
        validation operations. Those stages are evidence-producing work and
        must not be discarded merely because the audit operation remains the
        canonical structured collector.
        """
        if task.runtime.workflow != "project_audit":
            return proposal
        audit_nodes = [
            node for node in proposal.nodes
            if (
                node.operation_hint is not None
                and node.operation_hint.tool == "project"
                and node.operation_hint.method == "audit"
            )
            or "auditar el proyecto" in node.description.casefold()
            or "audit the project" in node.description.casefold()
        ]
        if len(audit_nodes) != 1:
            fallback = TaskService._fallback_plan(task)
            if fallback is not None:
                return fallback
            return proposal
        audit = audit_nodes[0]
        project_path = task.metadata.get("project_path")
        executable_nodes = [
            node for node in proposal.nodes
            if node.operation_hint is not None
            and node.id != audit.id
            and node.type == "OPERATION"
        ]
        graph = next(
            (
                node for node in executable_nodes
                if node.operation_hint
                and node.operation_hint.tool == "codegraph"
                and node.operation_hint.method == "build"
            ),
            None,
        )
        if graph is None and isinstance(project_path, str) and project_path:
            graph = PlanNodeProposal(
                id="refresh-codegraph-before-audit",
                description="Actualizar el codegraph del proyecto antes de elaborar la auditoría",
                type="OPERATION",
                operation_hint=OperationHint(
                    tool="codegraph",
                    method="build",
                    args={"root": project_path, "max_files": 500},
                    timeout=300,
                ),
            )
            executable_nodes.insert(0, graph)
        evidence_dependencies = [node.id for node in executable_nodes]
        dependencies = list(dict.fromkeys([*evidence_dependencies, *audit.dependencies]))
        # The audit tool returns structured findings; prose acceptance phrases
        # from the planner cannot be matched reliably against that payload and
        # would cause the same idempotent audit to retry unnecessarily.
        audit = audit.model_copy(update={
            "dependencies": dependencies,
            "acceptance": {},
            "operation_hint": audit.operation_hint.model_copy(update={
                "args": {
                    **audit.operation_hint.args,
                    "run_tests": True,
                }
            }) if audit.operation_hint else None,
        })
        return proposal.model_copy(update={"nodes": [*executable_nodes, audit]})

    @staticmethod
    def _normalize_browser_intent(task: Task, proposal: PlanProposal) -> PlanProposal:
        """Prevent a direct-answer response from swallowing a simple browser action."""
        if proposal.answer is None or proposal.nodes or proposal.subtasks:
            return proposal
        fallback = TaskService._fallback_plan(task)
        if fallback is None or not fallback.nodes:
            return proposal
        if fallback.nodes[0].operation_hint is None or fallback.nodes[0].operation_hint.tool != "browser":
            return proposal
        return fallback

    async def _normalize_project_modification(
        self, task: Task, proposal: PlanProposal
    ) -> PlanProposal:
        """Keep an existing-project change executable when the planner asks a question."""
        if proposal.answer is None or proposal.nodes or proposal.subtasks or not task.project_id:
            return proposal
        goal = task.goal.casefold()
        if not re.search(
            r"\b(?:añad|agreg|implement|modific|mejor|inclu|add|improv|modify)\w*",
            goal,
        ):
            return proposal
        project = await self.repository.get_project(task.project_id)
        if project is None:
            return proposal
        return PlanProposal(
            task_id=task.id,
            coverage=["inspect the registered project before applying the requested change"],
            nodes=[
                PlanNodeProposal(
                    id="fallback-project-analyze",
                    description=f"Inspeccionar la estructura del proyecto {project.name} para preparar el cambio solicitado",
                    type="OPERATION",
                    acceptance={"fields": {"root": project.path}},
                    operation_hint=OperationHint(
                        tool="project",
                        method="analyze",
                        args={"root": project.path, "max_files": 500},
                        timeout=120,
                    ),
                )
            ],
        )

    async def _ensure_project_validation(self, task: Task, proposal: PlanProposal) -> PlanProposal:
        """Guarantee that project mutations have executable validation evidence."""
        if proposal.answer is not None or not proposal.nodes:
            return proposal
        mutation_methods = {"create", "edit"}
        mutation_nodes = []
        validation_present = False
        project_root = None
        for node in proposal.nodes:
            hint = node.operation_hint.model_dump(mode="python") if node.operation_hint else {}
            method = hint.get("method")
            if method in mutation_methods:
                mutation_nodes.append(node)
                args = hint.get("args", {})
                if isinstance(args, dict) and args.get("root"):
                    project_root = str(args["root"])
            if method == "validate":
                validation_present = True
        if not mutation_nodes or validation_present:
            return proposal
        if not project_root and task.project_id:
            project = await self.repository.get_project(task.project_id)
            project_root = project.path if project else None
        if not project_root:
            return proposal
        validation_id = "auto-project-validation"
        if any(node.id == validation_id for node in proposal.nodes):
            return proposal
        validation = PlanNodeProposal(
            id=validation_id,
            description="Build, test and validate the changed project configuration",
            type="OPERATION",
            dependencies=[node.id for node in proposal.nodes],
            acceptance={
                "fields": {
                    "root": project_root,
                    "validation_status": "PASS",
                }
            },
            operation_hint=OperationHint(
                tool="project",
                method="validate",
                args={"root": project_root},
                timeout=300,
            ),
        )
        structural_verify = next(
            (
                node
                for node in proposal.nodes
                if node.type == "VERIFY"
                and node.operation_hint is None
            ),
            None,
        )
        if structural_verify is not None and len(proposal.nodes) >= task.budget.max_plan_nodes:
            replacement = structural_verify.model_copy(
                update={
                    "description": validation.description,
                    "type": validation.type,
                    "dependencies": validation.dependencies,
                    "acceptance": validation.acceptance,
                    "operation_hint": validation.operation_hint,
                }
            )
            return proposal.model_copy(
                update={
                    "nodes": [
                        replacement if node.id == structural_verify.id else node
                        for node in proposal.nodes
                    ]
                }
            )
        return proposal.model_copy(update={"nodes": [*proposal.nodes, validation]})

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
                        operation_hint=OperationHint(
                            tool="browser",
                            method="open",
                            args={
                                "url": browser_url,
                                "origin": f"{task.id}/fallback-browser-open",
                            },
                            timeout=60,
                        ),
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
            kind = "workspace"
            if any(term in goal for term in ("libro", "novela", "book", "writing", "manuscrito")):
                kind = "book"
            elif any(term in goal for term in ("química", "quimica", "chemistry", "laboratorio", "lab")):
                kind = "chemistry"
            elif any(term in goal for term in ("twitter", "x.com", "automatización", "automation", "api")):
                kind = "automation"
            elif any(term in goal for term in ("datos", "dataset", "investigación", "research")):
                kind = "research"
            return PlanProposal(
                task_id=task.id,
                coverage=["initialize a stack-neutral workspace with a durable manifest and artifact folders"],
                nodes=[
                    PlanNodeProposal(
                        id="fallback-project-initialize",
                        description=f"Inicializar el workspace {project_name} como proyecto de tipo {kind}",
                        type="OPERATION",
                        acceptance={"fields": {"name": project_name, "kind": kind}},
                        operation_hint=OperationHint(
                            tool="project",
                            method="initialize",
                            args={
                                "name": project_name,
                                "kind": kind,
                                "description": task.goal,
                                "directories": [],
                            },
                            timeout=300,
                        ),
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
            )
        )
        tests_requested = True
        explicit_tests_requested = any(
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
                    operation_hint=OperationHint(
                        tool="codegraph",
                        method="build",
                        args={"max_files": 500},
                        timeout=300,
                    ),
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
                operation_hint=OperationHint(
                    tool="project",
                    method="audit",
                    args={
                        "max_files": 500,
                        "run_tests": tests_requested or explicit_tests_requested,
                        "objective": task.goal,
                        "profile": "general",
                        "depth": "standard",
                    },
                    timeout=300,
                ),
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
        task.runtime.final_response = AssistantResponse(
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
        operation_args = {
            key: value for key, value in operation.args.items()
            if not key.startswith("_")
        }
        payload = json.dumps(
            {
                "task_id": task_id,
                "node_id": node_id,
                "tool": operation.tool,
                "method": operation.method,
                "args": operation_args,
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
            task.runtime.final_response is not None
            and not task.runtime.final_response_pending
        ):
            return
        final_status = task.status
        task.runtime.final_status = final_status.value
        task.runtime.final_finished_at = task.finished_at
        task.status = TaskStatus.FINALIZING
        task.finished_at = None
        await self.repository.save_task(task)
        respond = getattr(self.llm, "respond", None)
        if respond is None:
            await self._save_fallback_response(task, "final response provider unavailable")
            await self._restore_final_status(task)
            return
        used_llm_calls = task.runtime.llm_calls
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
        task.runtime.llm_calls = used_llm_calls + 1
        nodes = await self.repository.list_nodes(task.id)
        task.runtime.final_response = AssistantResponse(
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
        task.runtime.final_response_pending = True
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
            task.runtime.final_response = response.model_dump(mode="json")
            task.runtime.final_response_pending = False
            if task.result_summary:
                task.runtime.final_response["summary"] = task.result_summary
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
        status = task.runtime.final_status
        finished_at = task.runtime.final_finished_at
        task.runtime.final_status = None
        task.runtime.final_finished_at = None
        if status is None:
            return
        task.status = TaskStatus(status)
        task.finished_at = (
            finished_at
            if isinstance(finished_at, datetime)
            else datetime.fromisoformat(finished_at)
            if finished_at
            else datetime.now(UTC)
        )
        await self.repository.save_task(task)

    async def _save_fallback_response(self, task: Task, reason: str) -> None:
        if task.runtime.final_response is not None and not task.runtime.final_response_pending:
            return
        task.runtime.final_response_pending = False
        nodes = await self.repository.list_nodes(task.id)
        evidence = [
            f"{node.description}: {node.status.value}"
            for node in nodes
            if node.status in {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.BLOCKED}
        ]
        summary = task.result_summary or task.failure_reason or "Task finished without a generated report"
        task.runtime.final_response = AssistantResponse(
            response_type="blocked" if task.runtime.final_status == TaskStatus.BLOCKED.value or task.status is TaskStatus.BLOCKED else "report",
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
        if operation.tool == "project" and operation.method == "initialize":
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
                description=str(output.get("manifest", {}).get("description") or f"Project initialized by task {task.id}"),
                project_type=str(output.get("kind") or "workspace"),
            )
            if existing is None:
                project = await self.repository.create_project(project)
            task.project_id = project.id
            task.runtime.created_project = {
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
        if operation.tool == "project" and operation.method == "edit":
            # Source edits invalidate the persisted structural index. Keeping a
            # stale graph would make the next planner select obsolete files.
            project.codegraph = None
            project.codegraph_updated_at = None
            event_type = "PROJECT_CODEGRAPH_INVALIDATED"
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

    async def _persist_llm_request(
        self,
        task_id: str,
        role: str,
        context: dict[str, Any],
        schema: type,
        node_id: str | None = None,
    ) -> None:
        prepare_request = getattr(self.llm, "prepare_request", None)
        request = {}
        if prepare_request is not None:
            request = prepare_request(role, context, schema)
        await self.repository.save_event(
            TaskEvent(
                task_id=task_id,
                node_id=node_id,
                event_type="LLM_REQUEST",
                payload={"role": role, "request": request},
            )
        )

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
                    payload={
                        "result": result,
                        "operator": node.runtime.branch_config.get("operator", "truthy"),
                    },
                )
            )
            branch_key = "skip_on_true" if result else "skip_on_false"
            for target_id in node.runtime.branch_config.get(branch_key, []):
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
        config = node.runtime.branch_config
        if isinstance(config.get("value"), bool):
            left = config["value"]
        else:
            source = graph.nodes.get(config.get("source_node_id"))
            container = source.output_data if source else node.input_data
            left = self._read_path(container, config.get("field"))
        operator = config.get("operator", "truthy")
        right = config.get("right")
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
                current.runtime.branch_skipped = True
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

    async def _execute_agent_turn(
        self, task: Task, time_remaining: float | None = None
    ) -> bool:
        if task.runtime.agent_turns >= task.budget.max_plan_nodes:
            return await self._finish_task(
                task, TaskStatus.BLOCKED, "agent step budget exhausted", "AGENT_BUDGET_EXHAUSTED"
            ) is not None
        if task.runtime.llm_calls >= task.budget.max_llm_calls:
            return await self._finish_task(
                task, TaskStatus.BLOCKED, "LLM call budget exhausted", "TASK_BUDGET_EXHAUSTED"
            ) is not None
        events = await self.repository.list_events(task.id)
        last_observation = next(
            (
                event.payload
                for event in reversed(events)
                if event.event_type == "AGENT_OBSERVATION"
            ),
            None,
        )
        context = await self.context_builder.for_agent_decision(task, last_observation)
        await self._persist_llm_request(task.id, "AGENT", context, AgentDecision)
        task.runtime.llm_calls += 1
        task.runtime.agent_turns += 1
        decision = await self._call_llm(
            self.llm.agent_decide(context), time_remaining
        )
        task.apply_worker_decision(decision)
        await self.repository.save_event(
            TaskEvent(
                task_id=task.id,
                event_type="AGENT_DECISION",
                payload=decision.model_dump(mode="json"),
            )
        )
        if decision.decision_type is AgentDecisionType.COMPLETE:
            completion_evidence = [
                {
                    "type": event.event_type,
                    "node_id": event.node_id,
                    "payload": compact(event.payload, 2400),
                }
                for event in events[-12:]
                if event.event_type in {
                    "TOOL_RESULT",
                    "AGENT_OBSERVATION",
                    "NODE_COMPLETED",
                    "NODE_FAILED",
                }
            ]
            task.metadata["worker_completion"] = {
                "reason": decision.reason,
                "metadata": decision.metadata,
                "evidence": completion_evidence,
            }
            task.metadata["orchestration_stage"] = "REVIEW"
            task.result_summary = decision.reason or "Worker completed its execution."
            task.status = TaskStatus.READY
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    event_type="WORKER_COMPLETED",
                    payload=task.metadata["worker_completion"],
                )
            )
            await self.repository.save_task(task)
            return True

        if decision.decision_type in {AgentDecisionType.FAIL, AgentDecisionType.WAIT, AgentDecisionType.ASK_USER}:
            task.status = (
                TaskStatus.FAILED
                if decision.decision_type is AgentDecisionType.FAIL
                else TaskStatus.WAITING
            )
            task.failure_reason = decision.reason or "agent requires external input"
            task.finished_at = None if task.status is TaskStatus.WAITING else datetime.now(UTC)
            if decision.decision_type is AgentDecisionType.ASK_USER:
                task.runtime.clarification = {"kind": "agent_input", "prompt": decision.reason}
            await self.repository.save_task(task)
            return False
        if decision.decision_type is AgentDecisionType.DELEGATE:
            return await self._delegate_agent_work(task, decision, time_remaining)
        if decision.decision_type is not AgentDecisionType.EXECUTE or decision.operation is None:
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    event_type="AGENT_DECISION_REJECTED",
                    payload={"reason": "only EXECUTE decisions are executable"},
                )
            )
            return True

        operation = decision.operation
        if self.tools.definition(operation.tool) is None:
            task.status = TaskStatus.BLOCKED
            task.failure_reason = f"unknown tool: {operation.tool}"
            task.finished_at = datetime.now(UTC)
            await self.repository.save_task(task)
            return False
        if not await self._consume_budget(task, "tool_calls", task.budget.max_tool_calls):
            return False
        project = await self.repository.get_project(task.project_id) if task.project_id else None
        if operation.tool in {"project", "codegraph"} and project:
            operation.args["root"] = project.path
        node = TaskNode(
            task_id=task.id,
            type=NodeType.OPERATION,
            description=decision.reason or f"{operation.tool}.{operation.method}",
            status=NodeStatus.RUNNING,
            runtime={"operation_hint": operation.model_dump(mode="json")},
        )
        await self.repository.save_node(node)
        await self.repository.save_event(
            TaskEvent(
                task_id=task.id,
                node_id=node.id,
                event_type="TOOL_CALLED",
                payload={"tool": operation.tool, "method": operation.method},
            )
        )
        task.status = TaskStatus.RUNNING
        try:
            operation_error = self.tools.validate_operation(operation)
            if operation_error:
                raise ValueError(operation_error)
            output = await self.tools.execute(operation)
            observation = output.model_dump(mode="json") if hasattr(output, "model_dump") else output
            node.output_data = observation if isinstance(observation, dict) else {"value": observation}
            node.status = NodeStatus.SUCCEEDED
            node.finished_at = datetime.now(UTC)
            await self.repository.save_node(node)
            if isinstance(observation, dict):
                await self._persist_project_operation_result(task, operation, observation)
            await self.repository.save_event(
                TaskEvent(task_id=task.id, node_id=node.id, event_type="TOOL_RESULT", payload=node.output_data)
            )
            await self.repository.save_event(
                TaskEvent(task_id=task.id, node_id=node.id, event_type="AGENT_OBSERVATION", payload=node.output_data)
            )
        except Exception as error:
            node.status = NodeStatus.FAILED
            node.error = str(error)
            node.finished_at = datetime.now(UTC)
            await self.repository.save_node(node)
            await self.repository.save_event(
                TaskEvent(task_id=task.id, node_id=node.id, event_type="AGENT_OBSERVATION", payload={"success": False, "error": str(error)})
            )
        task.status = TaskStatus.READY
        await self.repository.save_task(task)
        return True

    async def _delegate_agent_work(
        self,
        task: Task,
        decision: AgentDecision,
        time_remaining: float | None = None,
    ) -> bool:
        depth = int(task.metadata.get("agent_depth", 0))
        if depth >= 3:
            return await self._finish_task(
                task,
                TaskStatus.BLOCKED,
                "agent delegation depth exhausted",
                "AGENT_DELEGATION_DEPTH_EXHAUSTED",
            ) is not None
        if len(decision.subtasks) > task.budget.max_plan_nodes - task.runtime.agent_turns:
            return await self._finish_task(
                task,
                TaskStatus.BLOCKED,
                "agent delegation exceeds the remaining step budget",
                "AGENT_DELEGATION_BUDGET_EXHAUSTED",
            ) is not None

        children: list[Task] = []
        child_budget = task.budget.model_copy(
            update=(
                {
                    "max_llm_calls": decision.budget.max_llm_calls,
                    "max_tool_calls": decision.budget.max_tool_calls,
                    "max_execution_time": decision.budget.max_execution_time,
                    "max_plan_nodes": decision.budget.max_steps,
                }
                if decision.budget is not None
                else {}
            )
        )
        if time_remaining is not None:
            child_budget.max_execution_time = min(
                child_budget.max_execution_time, max(0.0, time_remaining)
            )
        for description in decision.subtasks:
            child = Task(
                parent_task_id=task.id,
                root_task_id=task.root_task_id,
                source="AGENT",
                project_id=task.project_id,
                goal=description,
                description=description,
                priority=task.priority,
                status=TaskStatus.READY,
                budget=child_budget.model_copy(deep=True),
                metadata={
                    "execution_mode": "agent",
                    "orchestration_stage": "WORK",
                    "worker": task.metadata.get("worker", "GENERAL_WORKER"),
                    "template": task.metadata.get("template", "general"),
                    "extra_context": {"delegated_from": task.id},
                    "agent_depth": depth + 1,
                },
            )
            await self.repository.save_task(child)
            root = TaskNode(
                task_id=child.id,
                type=NodeType.TASK,
                description=description,
                status=NodeStatus.READY,
                priority=child.priority,
            )
            await self.repository.save_node(root)
            await self.repository.save_event(
                TaskEvent(
                    task_id=child.id,
                    node_id=root.id,
                    event_type="TASK_DELEGATED",
                    payload={"parent_task_id": task.id, "goal": description},
                )
            )
            children.append(child)

        results = []
        for child in children:
            result = await self.run_task(child.id, wait_for_retry=False)
            child_events = await self.repository.list_events(child.id)
            results.append(
                {
                    "task_id": child.id,
                    "status": result.status.value if result else TaskStatus.FAILED.value,
                    "summary": result.result_summary if result else None,
                    "failure_reason": result.failure_reason if result else "child task disappeared",
                    "evidence": [
                        {
                            "type": event.event_type,
                            "payload": compact(event.payload, 2400),
                        }
                        for event in child_events
                        if event.event_type in {"TOOL_RESULT", "AGENT_OBSERVATION", "WORKER_COMPLETED"}
                    ][-8:],
                }
            )
        await self.repository.save_event(
            TaskEvent(
                task_id=task.id,
                event_type="AGENT_DELEGATION_RESULT",
                payload={"reason": decision.reason, "children": results},
            )
        )
        await self.repository.save_event(
            TaskEvent(
                task_id=task.id,
                event_type="AGENT_OBSERVATION",
                payload={
                    "success": all(item["status"] == TaskStatus.SUCCEEDED.value for item in results),
                    "delegated_tasks": results,
                },
            )
        )
        task.status = TaskStatus.READY
        await self.repository.save_task(task)
        return True

    async def _execute_orchestrator_turn(
        self, task: Task, time_remaining: float | None = None
    ) -> bool:
        if task.runtime.llm_calls >= task.budget.max_llm_calls:
            return await self._finish_task(
                task, TaskStatus.BLOCKED, "LLM call budget exhausted", "TASK_BUDGET_EXHAUSTED"
            ) is not None
        if task.metadata.get("orchestration_stage", "ROUTE") == "ROUTE":
            if not await self._prepare_audit_codegraph(task, time_remaining):
                return False
        context = await self.context_builder.for_orchestrator(task)
        await self._persist_llm_request(
            task.id, "ORCHESTRATOR", context, OrchestratorDecision
        )
        task.runtime.llm_calls += 1
        task.runtime.agent_turns += 1
        decision = await self._call_llm(
            self.llm.orchestrate(context), time_remaining
        )
        if self.context_builder._planner_intent(task.goal) == "audit":
            decision = decision.model_copy(
                update={
                    "intent": "audit",
                    "worker": "AUDIT_WORKER",
                    "template": "audit",
                }
            )
        await self.repository.save_event(
            TaskEvent(
                task_id=task.id,
                event_type="ORCHESTRATOR_DECISION",
                payload=decision.model_dump(mode="json"),
            )
        )
        if context.get("orchestration_stage") == "REVIEW":
            # Older providers only returned routing fields. Once a worker has
            # completed, that shape is an implicit finalize decision.
            if decision.stage in {"FINALIZE", "ROUTE"}:
                task.metadata["orchestration_stage"] = "FINALIZED"
                task.status = TaskStatus.SUCCEEDED
                task.finished_at = datetime.now(UTC)
                await self.repository.save_task(task)
                return False
            if decision.stage == "BLOCK":
                task.status = TaskStatus.BLOCKED
                task.failure_reason = decision.reason or "orchestrator blocked finalization"
                task.finished_at = datetime.now(UTC)
                await self.repository.save_task(task)
                return False
            if decision.needs_input:
                task.status = TaskStatus.WAITING
                task.failure_reason = decision.clarification or "orchestrator requires clarification"
                task.runtime.clarification = {
                    "kind": "orchestrator_review",
                    "prompt": task.failure_reason,
                }
                await self.repository.save_task(task)
                return False
            task.metadata["orchestration_stage"] = "WORK"
            if decision.worker:
                task.metadata["worker"] = decision.worker
            if decision.template:
                task.metadata["template"] = decision.template
            if decision.extra_context:
                task.metadata["extra_context"] = decision.extra_context
            if decision.acceptance_criteria:
                task.metadata["acceptance_criteria"] = decision.acceptance_criteria
            task.status = TaskStatus.READY
            await self.repository.save_task(task)
            return True
        if decision.stage == "BLOCK":
            task.status = TaskStatus.BLOCKED
            task.failure_reason = decision.reason or "orchestrator blocked routing"
            task.finished_at = datetime.now(UTC)
            await self.repository.save_task(task)
            return False
        if decision.needs_input:
            task.status = TaskStatus.WAITING
            task.failure_reason = decision.clarification or "orchestrator requires clarification"
            task.runtime.clarification = {
                "kind": "orchestrator_routing",
                "prompt": task.failure_reason,
            }
            await self.repository.save_task(task)
            return False
        if decision.target_type and decision.target_id:
            target = {"type": decision.target_type, "id": decision.target_id}
            if decision.target_type == "project":
                project = await self.repository.resolve_project(decision.target_id, None)
                if project is None:
                    task.status = TaskStatus.BLOCKED
                    task.failure_reason = f"orchestrator selected unknown project: {decision.target_id}"
                    task.finished_at = datetime.now(UTC)
                    await self.repository.save_task(task)
                    return False
                task.project_id = project.id
                task.metadata["project_path"] = project.path
            elif decision.target_type == "device":
                self._validate_device_target(decision.target_id)
            task.runtime.target = target
        task.metadata["worker"] = decision.worker
        task.metadata["template"] = decision.template
        task.metadata["orchestrator_intent"] = decision.intent
        task.metadata["extra_context"] = decision.extra_context
        task.metadata["acceptance_criteria"] = decision.acceptance_criteria
        task.metadata["orchestration_stage"] = "WORK"
        task.runtime.workflow = "agent"
        task.status = TaskStatus.READY
        await self.repository.save_task(task)
        return True

    async def _prepare_audit_codegraph(
        self, task: Task, time_remaining: float | None = None
    ) -> bool:
        if self.context_builder._planner_intent(task.goal) != "audit" or not task.project_id:
            return True
        if not await self._consume_budget(task, "codegraph_queries", task.budget.max_codegraph_queries):
            return False
        try:
            project = await self.repository.get_project(task.project_id)
            if project is None:
                return True
            if time_remaining is not None and time_remaining <= 0:
                raise TimeoutError("audit codegraph preflight exceeded the task time budget")
            started = monotonic()
            if time_remaining is None:
                refreshed = await self.refresh_project_codegraph(project.id)
            else:
                refreshed = await asyncio.wait_for(
                    self.refresh_project_codegraph(project.id), timeout=time_remaining
                )
            if refreshed is None:
                raise ValueError("project disappeared during audit codegraph preflight")
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    event_type="ORCHESTRATOR_CODEGRAPH_READY",
                    payload={
                        "project_id": project.id,
                        "version": refreshed.codegraph_version,
                        "file_count": refreshed.codegraph.get("file_count") if refreshed.codegraph else 0,
                        "duration_seconds": monotonic() - started,
                    },
                )
            )
            return True
        except Exception as error:
            task.metadata["codegraph_error"] = str(error)
            await self.repository.save_task(task)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task.id,
                    event_type="ORCHESTRATOR_CODEGRAPH_FAILED",
                    payload={"error": str(error), "project_id": task.project_id},
                )
            )
            return True

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
        if (
            task.runtime.workflow == "agent"
            or task.metadata.get("execution_mode") == "agent"
        ) and self._supports_agent_mode():
            events = await self.repository.list_events(task.id)
            if task.metadata.get("orchestration_stage", "ROUTE") in {"ROUTE", "REVIEW"}:
                return await self._execute_orchestrator_turn(task, time_remaining)
            return await self._execute_agent_turn(task, time_remaining)
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
                    await self._persist_llm_request(
                        task_id, "PLANNER", planner_context, PlanProposal
                    )
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
                    await self._persist_llm_request(
                        task_id, "PLANNER", retry_context, PlanProposal
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
                proposal = await self._normalize_project_modification(task, proposal)
                proposal = await self._ensure_project_validation(task, proposal)
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
                original_proposal = proposal
                proposal = self._normalize_project_audit_plan(task, proposal)
                if (
                    task.runtime.workflow == "project_audit"
                    and original_proposal.answer is not None
                    and proposal.answer is None
                    and proposal.nodes
                ):
                    await self.repository.save_event(
                        TaskEvent(
                            task_id=task_id,
                            event_type="PLAN_REPAIRED",
                            payload={
                                "reason": "direct_answer_is_invalid_for_project_audit",
                                "strategy": "deterministic_project_audit",
                                "nodes": [item.id for item in proposal.nodes],
                            },
                        )
                    )
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
                            "logical_id": proposed.id,
                            **{
                                key: value
                                for key, value in proposed.metadata.items()
                                if key not in {
                                    "logical_id",
                                    "acceptance",
                                    "inputs",
                                    "outputs",
                                    "acceptance_criteria",
                                    "allowed_tools",
                                    "retry_policy",
                                    "idempotency_policy",
                                    "failure_policy",
                                    "deadline",
                                    "next_retry_at",
                                    "review_required",
                                    "review_status",
                                    "recovery_pending",
                                    "recovery_expansion_id",
                                    "recovery_target_id",
                                    "recovery_finalize",
                                    "recovery_attempt",
                                    "branch_skipped",
                                }
                            },
                        },
                        contract=NodeContract(
                            logical_id=proposed.id,
                            acceptance=proposed.acceptance,
                            inputs=proposed.inputs,
                            outputs=proposed.outputs,
                            acceptance_criteria=[
                                item
                                if hasattr(item, "model_dump")
                                else {
                                    "id": f"criterion-{index}",
                                    "description": item,
                                    "required": True,
                                }
                                for index, item in enumerate(
                                    proposed.acceptance_criteria, start=1
                                )
                            ],
                            allowed_tools=proposed.allowed_tools,
                            retry_policy=proposed.retry_policy,
                            idempotency_policy=proposed.idempotency_policy,
                            failure_policy=proposed.failure_policy,
                            deadline=proposed.deadline,
                        ),
                        max_retries=min(
                            task.budget.max_retries,
                            proposed.retry_policy.max_retries,
                        ),
                    )
                    for proposed in proposal.nodes
                ]
                for proposed, planned in zip(proposal.nodes, planned_models):
                    if proposed.operation_hint is not None:
                        planned.runtime.operation_hint = proposed.operation_hint.model_dump(
                            mode="json", exclude_none=True
                        )
                    planned.runtime.branch_config.update(
                        {
                            "operator": proposed.branch_config.operator,
                            "value": proposed.branch_config.value,
                            "source_node_id": proposed.branch_config.source_node_id,
                        }
                    )
                    for key in ("skip_on_false", "skip_on_true"):
                        targets = getattr(proposed.branch_config, key)
                        if targets:
                            planned.runtime.branch_config[key] = [
                                node_ids.get(target, target)
                                for target in targets
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
        selected = self.scheduler.next_ready_node(task.status, graph, task.deadline)
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
                deadline = candidate.contract.deadline
                if not deadline:
                    continue
                try:
                    deadline_at = deadline
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
            await self.repository.save_event(
                TaskEvent(
                    task_id=task_id,
                    node_id=node.id,
                    event_type="NODE_LEASE_UNAVAILABLE",
                    payload={
                        "reason": "another worker still owns the node lease",
                        "retry": True,
                    },
                )
            )
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
            missing_inputs = await self.context_builder.missing_required_inputs(
                task, node, graph
            )
            if missing_inputs:
                reason = "required inputs are missing"
                node.status = NodeStatus.BLOCKED
                node.error = reason
                node.metadata["missing_inputs"] = missing_inputs
                task.status = TaskStatus.BLOCKED
                task.failure_reason = reason
                task.finished_at = datetime.now(UTC)
                await self.repository.save_node(node)
                await self.repository.save_task(task)
                await self.repository.save_event(
                    TaskEvent(
                        task_id=task_id,
                        node_id=node.id,
                        event_type="INPUTS_MISSING",
                        payload={"inputs": missing_inputs, "reason": reason},
                    )
                )
                return True
            operation_hint = node.runtime.operation_hint
            if operation_hint:
                try:
                    decision = NodeDecision(
                        action="OPERATION",
                        operation=Operation.model_validate(operation_hint),
                    )
                    await self.repository.save_event(
                        TaskEvent(
                            task_id=task_id,
                            node_id=node.id,
                            event_type="NODE_RESOLVER_SKIPPED",
                            payload={
                                "reason": "planner_operation_hint_is_authoritative",
                                "tool": decision.operation.tool,
                                "method": decision.operation.method,
                            },
                        )
                    )
                except (ValidationError, ValueError, TypeError) as error:
                    reason = f"invalid planner operation hint: {error}"
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
                            event_type="NODE_BLOCKED",
                            payload={"reason": reason},
                        )
                    )
                    return True
            else:
                try:
                    if not await self._consume_budget(task, "llm_calls", task.budget.max_llm_calls):
                        await self._block_node_for_budget(task, node, "llm_calls")
                        return True
                    await self._persist_llm_request(
                        task_id, "NODE_RESOLVER", context, NodeDecision, node.id
                    )
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
                node.runtime.review_required = True
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
                await self._mark_recovery_expansion_failed(node, node.error)
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
            if operation_hint:
                operation = Operation.model_validate(operation_hint)
            if operation.tool == "project" and operation.method == "audit":
                # project.audit returns the audit report itself. Its structured
                # payload is the acceptance evidence; prose contains checks
                # from older planner prompts are not verifiable.
                node.contract.acceptance = {}
                await self.repository.save_node(node)
            tool_definition = self.tools.definition(operation.tool)
            if tool_definition is not None:
                await self.renew_lease(
                    node.id, seconds=max(300, int(operation.timeout) + 60)
                )
            acceptance = node.contract.acceptance
            if acceptance:
                operation.metadata.setdefault("expected", acceptance)
            project = await self.repository.get_project(task.project_id) if task.project_id else None
            if operation.tool == "project" and operation.method == "create":
                operation.args.setdefault("root", self.projects_root)
            elif operation.tool == "project" and operation.method == "initialize":
                operation.args["root"] = self.projects_root
            elif operation.tool == "project" and operation.method in {"analyze", "read", "audit", "edit", "build", "system"}:
                operation.args["root"] = project.path if project else self.context_builder.workspace_root
            elif operation.tool == "project" and operation.method == "validate":
                operation.args["root"] = project.path if project else operation.args.get("root", self.context_builder.workspace_root)
            elif operation.tool == "codegraph" and operation.method in {"analyze", "audit", "build", "system", "query"}:
                operation.args["root"] = project.path if project else self.context_builder.workspace_root
                if operation.method == "query" and project:
                    refreshed = await self.refresh_project_codegraph(project.id)
                    project = refreshed or project
                    if project.codegraph:
                        operation.args["_persisted_graph"] = project.codegraph
                        operation.args["_graph_fresh"] = True
            budget_key = None
            budget_limit = None
            if operation.tool == "codegraph" and operation.method == "query":
                budget_key, budget_limit = "codegraph_queries", task.budget.max_codegraph_queries
            elif operation.tool == "project" and operation.method == "read":
                budget_key, budget_limit = "project_reads", task.budget.max_project_reads
            if budget_key and not await self._consume_budget(task, budget_key, budget_limit):
                await self._block_node_for_budget(task, node, budget_key)
                return True
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
                if operation.tool == "project" and operation.method == "read" and operation_result.output:
                    source_bytes = int(operation_result.output.get("source_bytes", 0))
                    task.runtime.source_bytes += source_bytes
                    if task.runtime.source_bytes > task.budget.max_source_bytes:
                        await self._block_node_for_budget(task, node, "source_bytes")
                        return True
                if cancellation.is_set():
                    node.status = NodeStatus.CANCELLED
                    node.output_data = operation_result.model_dump(mode="json")
                    await self._publish_operation_artifacts(
                        task.id,
                        node.id,
                        operation_result.artifacts,
                        [item.model_dump(mode="json") for item in node.contract.outputs],
                    )
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
                await self._publish_operation_artifacts(
                    task.id,
                    node.id,
                    result.get("artifacts", []),
                    [item.model_dump(mode="json") for item in node.contract.outputs],
                )
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
            acceptance = node.contract.acceptance
            if acceptance:
                verification_result.metadata["expected"] = acceptance
            acceptance_criteria = [
                item.model_dump(mode="json")
                for item in node.contract.acceptance_criteria
            ]
            if acceptance_criteria:
                verification_result.metadata["acceptance_criteria"] = acceptance_criteria
            missing_outputs = await self.context_builder.missing_required_outputs(task, node)
            if missing_outputs:
                verification_result.metadata["missing_required_outputs"] = missing_outputs
            if tool_definition is not None:
                evidence = tool_definition.evidence.get(operation.method, {})
                if evidence:
                    verification_result = DeterministicVerifier.with_tool_evidence(
                        verification_result, evidence
                    )
                    expected = verification_result.metadata.get("expected", {})
                    if isinstance(expected, dict) and evidence.get("success_fields"):
                        # Natural-language acceptance is guidance for the
                        # planner, not a literal substring contract.
                        expected.pop("contains", None)
                        expected.pop("output_contains", None)
                        verification_result.metadata["expected"] = expected
            verification = self.verifier.verify(verification_result)
            await self.repository.save_event(
                TaskEvent(
                    task_id=task_id,
                    node_id=node.id,
                    event_type="NODE_VERIFIED",
                    payload={
                        "decision": verification.decision.value,
                        "reason": verification.reason,
                        "criteria_results": [
                            item.model_dump(mode="json")
                            for item in verification.criteria_results
                        ],
                        "diagnostics": [
                            item.model_dump(mode="json")
                            for item in verification.diagnostics
                        ],
                        "missing_evidence": verification.missing_evidence,
                    },
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
            elif (
                verification.decision.value == "RETRY"
                and self.scheduler.retry_allowed(node, verification_result)
                and node.retry_count < node.max_retries
            ):
                node.retry_count += 1
                node.status = NodeStatus.READY
                node.error = result.get("error")
                delay = self.scheduler.retry_delay(node)
                node.runtime.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
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
                reason = result.get("error") or (
                    "retry policy does not allow this failure"
                    if not self.scheduler.retry_allowed(node, verification_result)
                    else "retry limit exhausted"
                )
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
                    await self._persist_llm_request(
                        task.id, "REPLANNER", replanner_context, NodeDecision, node.id
                    )
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
            attempt = node.runtime.recovery_attempt + 1
            try:
                await self._expand_recovery_graph(
                    task,
                    node,
                    decision.subtasks,
                    attempt,
                    branch_name=f"assistant/recovery/{task.id[:8]}-{attempt}",
                    reason=decision.reason or "",
                )
            except ValueError as error:
                node.status = NodeStatus.BLOCKED
                node.error = str(error)
                task.status = TaskStatus.BLOCKED
                task.failure_reason = str(error)
            else:
                task.status = TaskStatus.READY
        elif decision.action == "BLOCK" and decision.user_input_required:
            node.status = NodeStatus.WAITING
            node.error = decision.reason or "additional user information is required"
            task.status = TaskStatus.WAITING
            task.failure_reason = node.error
            task.finished_at = None
            task.runtime.clarification = {
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
                    payload=task.runtime.clarification,
                )
            )
        else:
            node.status = NodeStatus.BLOCKED
            node.error = decision.reason or "replanning returned no executable strategy"
            task.status = TaskStatus.BLOCKED
            task.failure_reason = node.error
            task.finished_at = datetime.now(UTC)
        if node.status is NodeStatus.BLOCKED:
            await self._mark_recovery_expansion_failed(node, node.error or "recovery node blocked")
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
                    node.runtime.next_retry_at
                    for node in await self.repository.list_nodes(task_id)
                    if node.status is NodeStatus.READY and node.runtime.next_retry_at
                ]
                if retry_times:
                    retry_at = min(retry_times)
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
