import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.domain.models import (
    GraphEdge,
    MemoryRecord,
    NodeStatus,
    Project,
    Task,
    TaskEvent,
    TaskNode,
    TaskStatus,
)
from assistant.domain.state import validate_node_transition, validate_task_transition

from .orm import (
    EdgeRow,
    EventRow,
    IdempotencyRow,
    LeaseRow,
    MemoryRow,
    NodeRow,
    ProjectRow,
    TaskRow,
    WorkerHeartbeatRow,
)


def task_to_row(task: Task) -> TaskRow:
    return TaskRow(
        **task.model_dump(mode="python", exclude={"metadata", "budget"}),
        metadata_json=task.metadata,
        budget_json=task.budget.model_dump(),
    )


def row_to_task(row: TaskRow) -> Task:
    return Task.model_validate(
        {
            **{
                key: getattr(row, key)
                for key in (
                    "id",
                    "parent_task_id",
                    "root_task_id",
                    "source",
                    "project_id",
                    "goal",
                    "description",
                    "priority",
                    "created_at",
                    "started_at",
                    "finished_at",
                    "deadline",
                    "retry_count",
                    "max_retries",
                    "result_summary",
                    "failure_reason",
                )
            },
            "status": row.status,
            "metadata": row.metadata_json or {},
            "budget": row.budget_json or {},
        }
    )


def node_to_row(node: TaskNode) -> NodeRow:
    return NodeRow(
        **node.model_dump(mode="python", exclude={"metadata"}), metadata_json=node.metadata
    )


def row_to_node(row: NodeRow) -> TaskNode:
    return TaskNode.model_validate(
        {
            **{
                key: getattr(row, key)
                for key in (
                    "id",
                    "task_id",
                    "parent_node_id",
                    "description",
                    "priority",
                    "input_data",
                    "output_data",
                    "retry_count",
                    "max_retries",
                    "created_at",
                    "started_at",
                    "finished_at",
                    "error",
                )
            },
            "type": row.type,
            "status": row.status,
            "metadata": row.metadata_json or {},
        }
    )


class TaskRepository:
    def __init__(self, session: AsyncSession, event_sink: Callable[[TaskEvent], Awaitable[None]] | None = None):
        self.session = session
        self.event_sink = event_sink

    async def save_task(self, task: Task) -> None:
        row = await self.session.get(TaskRow, task.id)
        if row is not None:
            validate_task_transition(TaskStatus(row.status), task.status)
        values = task_to_row(task).__dict__
        values.pop("_sa_instance_state", None)
        if row is None:
            self.session.add(TaskRow(**values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        await self.session.commit()

    async def get_task(self, task_id: str) -> Task | None:
        row = await self.session.get(TaskRow, task_id)
        return row_to_task(row) if row else None

    async def list_tasks(self) -> list[Task]:
        result = await self.session.execute(select(TaskRow).order_by(TaskRow.created_at.desc()))
        return [row_to_task(row) for row in result.scalars()]

    async def save_worker_heartbeat(
        self,
        worker_id: str,
        started_at: datetime,
        heartbeat_at: datetime,
        last_started_at: datetime | None,
        last_completed_at: datetime | None,
        last_error: str | None,
        active_count: int,
    ) -> None:
        row = await self.session.get(WorkerHeartbeatRow, worker_id)
        if row is None:
            row = WorkerHeartbeatRow(
                worker_id=worker_id,
                started_at=started_at,
                heartbeat_at=heartbeat_at,
                last_started_at=last_started_at,
                last_completed_at=last_completed_at,
                last_error=last_error,
                active_count=active_count,
                pass_count=1,
            )
            self.session.add(row)
        else:
            row.heartbeat_at = heartbeat_at
            row.last_started_at = last_started_at
            row.last_completed_at = last_completed_at
            row.last_error = last_error
            row.active_count = active_count
            row.pass_count += 1
        await self.session.commit()

    async def create_project(self, project: Project) -> Project:
        row = ProjectRow(**project.model_dump(mode="python"))
        self.session.add(row)
        await self.session.commit()
        return project

    async def get_project(self, project_id: str) -> Project | None:
        row = await self.session.get(ProjectRow, project_id)
        return self._row_to_project(row) if row else None

    async def list_projects(self, enabled_only: bool = False) -> list[Project]:
        query = select(ProjectRow).order_by(ProjectRow.is_default.desc(), ProjectRow.updated_at.desc())
        if enabled_only:
            query = query.where(ProjectRow.enabled.is_(True))
        result = await self.session.execute(query)
        return [self._row_to_project(row) for row in result.scalars()]

    async def update_project(self, project: Project) -> Project:
        row = await self.session.get(ProjectRow, project.id)
        if row is None:
            raise KeyError(f"Project not found: {project.id}")
        project.updated_at = datetime.now(UTC)
        for key, value in project.model_dump(mode="python").items():
            setattr(row, key, value)
        await self.session.commit()
        return project

    async def delete_project(self, project_id: str) -> bool:
        result = await self.session.execute(delete(ProjectRow).where(ProjectRow.id == project_id))
        await self.session.commit()
        return result.rowcount > 0

    async def resolve_project(self, project_id: str | None = None, project_name: str | None = None) -> Project | None:
        if project_id:
            return await self.get_project(project_id)
        projects = await self.list_projects(enabled_only=True)
        if project_name:
            matches = [item for item in projects if item.name.casefold() == project_name.casefold()]
            return matches[0] if len(matches) == 1 else None
        defaults = [item for item in projects if item.is_default]
        if len(defaults) == 1:
            return defaults[0]
        return projects[0] if len(projects) == 1 else None

    @staticmethod
    def _row_to_project(row: ProjectRow) -> Project:
        return Project.model_validate({key: getattr(row, key) for key in (
            "id", "name", "path", "description", "project_type", "audit_prompt", "enabled", "is_default",
            "created_at", "updated_at", "last_used_at", "last_audited_at", "codegraph",
            "codegraph_updated_at", "codegraph_version",
        )})

    async def save_memory(self, memory: MemoryRecord) -> None:
        row = await self.session.get(MemoryRow, memory.id)
        values = {
            "id": memory.id,
            "kind": memory.kind,
            "key": memory.key,
            "value_json": memory.value,
            "source": memory.source,
            "confidence": memory.confidence,
            "created_at": memory.created_at,
            "updated_at": memory.updated_at,
            "usage_count": memory.usage_count,
            "expires_at": memory.expires_at,
        }
        if row is None:
            self.session.add(MemoryRow(**values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        await self.session.commit()

    @staticmethod
    def _memory_expired(row: MemoryRow, now: datetime | None = None) -> bool:
        expires_at = row.expires_at
        if expires_at is None:
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= (now or datetime.now(UTC))

    @staticmethod
    def _memory_record(row: MemoryRow) -> MemoryRecord:
        return MemoryRecord(
            id=row.id,
            kind=row.kind,
            key=row.key,
            value=row.value_json,
            source=row.source,
            confidence=row.confidence,
            created_at=row.created_at,
            updated_at=row.updated_at,
            usage_count=row.usage_count,
            expires_at=row.expires_at,
        )

    async def search_memory(self, query: str, limit: int = 10) -> list[MemoryRecord]:
        result = await self.session.execute(select(MemoryRow).order_by(MemoryRow.updated_at.desc()))
        terms = {term.lower() for term in query.split() if len(term) > 2}
        matches = []
        for row in result.scalars():
            if self._memory_expired(row):
                continue
            haystack = f"{row.kind} {row.key} {row.value_json}".lower()
            if not terms or any(term in haystack for term in terms):
                row.usage_count += 1
                matches.append(self._memory_record(row))
                if len(matches) == limit:
                    break
        await self.session.commit()
        return matches

    async def list_memory(self, limit: int = 50) -> list[MemoryRecord]:
        result = await self.session.execute(
            select(MemoryRow).order_by(MemoryRow.updated_at.desc()).limit(limit)
        )
        return [self._memory_record(row) for row in result.scalars() if not self._memory_expired(row)]

    async def upsert_memory(
        self,
        *,
        kind: str,
        key: str,
        value,
        source: str = "SYSTEM",
        confidence: float = 1.0,
        expires_at: datetime | None = None,
    ) -> MemoryRecord:
        result = await self.session.execute(
            select(MemoryRow).where(MemoryRow.kind == kind, MemoryRow.key == key)
        )
        row = result.scalars().first()
        now = datetime.now(UTC)
        if row is None:
            memory = MemoryRecord(
                kind=kind, key=key, value=value, source=source, confidence=confidence,
                created_at=now, updated_at=now, expires_at=expires_at,
            )
            await self.save_memory(memory)
            return memory
        row.value_json = value
        row.source = source
        row.confidence = confidence
        row.updated_at = now
        row.expires_at = expires_at
        await self.session.commit()
        return self._memory_record(row)

    async def delete_memory(self, memory_id: str) -> bool:
        result = await self.session.execute(delete(MemoryRow).where(MemoryRow.id == memory_id))
        await self.session.commit()
        return bool(result.rowcount)

    async def purge_old_events(self, retention_days: int = 30, keep_recent: int = 1000) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=max(1, retention_days))
        recent_ids = select(EventRow.id).order_by(EventRow.created_at.desc()).limit(max(0, keep_recent))
        result = await self.session.execute(
            delete(EventRow).where(
                EventRow.created_at < cutoff,
                EventRow.id.not_in(recent_ids),
            )
        )
        await self.session.commit()
        return int(result.rowcount or 0)

    async def save_idempotency_result(
        self, idempotency_key: str, result_json: dict
    ) -> dict:
        existing = await self.session.get(IdempotencyRow, idempotency_key)
        if existing is not None:
            return existing.result_json
        self.session.add(
            IdempotencyRow(idempotency_key=idempotency_key, result_json=result_json)
        )
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self.session.get(IdempotencyRow, idempotency_key)
            if existing is None:
                raise
            return existing.result_json
        return result_json

    async def reset_state(self) -> dict[str, int]:
        tables = (
            (LeaseRow, "leases"),
            (EventRow, "events"),
            (EdgeRow, "edges"),
            (NodeRow, "nodes"),
            (IdempotencyRow, "operation_results"),
            (TaskRow, "tasks"),
            (MemoryRow, "memories"),
            (WorkerHeartbeatRow, "worker_heartbeat"),
        )
        deleted = {}
        for model, name in tables:
            result = await self.session.execute(delete(model))
            deleted[name] = int(result.rowcount or 0)
        await self.session.commit()
        return deleted

    async def reset_memory(self) -> int:
        """Backward-compatible memory-only reset for internal callers."""
        result = await self.session.execute(delete(MemoryRow))
        await self.session.commit()
        return int(result.rowcount or 0)

    async def redact_memory(self, memory_id: str) -> MemoryRecord | None:
        row = await self.session.get(MemoryRow, memory_id)
        if row is None:
            return None
        row.value_json = "[REDACTED]"
        row.source = "REDACTED"
        row.confidence = 0.0
        row.updated_at = datetime.now(UTC)
        await self.session.commit()
        return self._memory_record(row)

    async def purge_expired_memory(self) -> int:
        now = datetime.now(UTC)
        result = await self.session.execute(select(MemoryRow))
        expired_ids = [row.id for row in result.scalars() if self._memory_expired(row, now)]
        if expired_ids:
            await self.session.execute(delete(MemoryRow).where(MemoryRow.id.in_(expired_ids)))
            await self.session.commit()
        return len(expired_ids)

    async def export_memory(self, include_expired: bool = False) -> list[dict]:
        result = await self.session.execute(select(MemoryRow).order_by(MemoryRow.updated_at.desc()))
        return [
            json.loads(self._memory_record(row).model_dump_json())
            for row in result.scalars()
            if include_expired or not self._memory_expired(row)
        ]

    async def save_node(self, node: TaskNode) -> None:
        row = await self.session.get(NodeRow, node.id)
        if row is not None:
            validate_node_transition(NodeStatus(row.status), node.status)
        values = node_to_row(node).__dict__
        values.pop("_sa_instance_state", None)
        if row is None:
            self.session.add(NodeRow(**values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        await self.session.commit()

    async def get_node(self, node_id: str) -> TaskNode | None:
        row = await self.session.get(NodeRow, node_id)
        return row_to_node(row) if row else None

    async def list_nodes(self, task_id: str) -> list[TaskNode]:
        result = await self.session.execute(select(NodeRow).where(NodeRow.task_id == task_id))
        return [row_to_node(row) for row in result.scalars()]

    async def save_edge(self, task_id: str, edge: GraphEdge) -> None:
        self.session.add(EdgeRow(task_id=task_id, **edge.model_dump()))
        await self.session.commit()

    async def list_edges(self, task_id: str) -> list[GraphEdge]:
        result = await self.session.execute(select(EdgeRow).where(EdgeRow.task_id == task_id))
        return [
            GraphEdge(
                from_node=row.from_node,
                to_node=row.to_node,
                dependency_type=row.dependency_type,
                condition=row.condition,
            )
            for row in result.scalars()
        ]

    async def save_event(self, event: TaskEvent) -> None:
        self.session.add(EventRow(**event.model_dump()))
        await self.session.commit()
        if self.event_sink:
            await self.event_sink(event)

    async def list_events(self, task_id: str) -> list[TaskEvent]:
        result = await self.session.execute(
            select(EventRow).where(EventRow.task_id == task_id).order_by(EventRow.created_at)
        )
        return [TaskEvent.model_validate(row.__dict__) for row in result.scalars()]

    async def clear_edges(self, task_id: str) -> None:
        await self.session.execute(delete(EdgeRow).where(EdgeRow.task_id == task_id))
        await self.session.commit()

    async def reset_task_graph(self, task_id: str, root_node_id: str) -> None:
        node_ids = await self.session.execute(
            select(NodeRow.id).where(NodeRow.task_id == task_id, NodeRow.id != root_node_id)
        )
        child_ids = list(node_ids.scalars())
        if child_ids:
            await self.session.execute(delete(LeaseRow).where(LeaseRow.node_id.in_(child_ids)))
        await self.session.execute(delete(EdgeRow).where(EdgeRow.task_id == task_id))
        await self.session.execute(
            delete(NodeRow).where(NodeRow.task_id == task_id, NodeRow.id != root_node_id)
        )
        await self.session.commit()

    async def delete_task(self, task_id: str) -> bool:
        node_ids = await self.session.execute(
            select(NodeRow.id).where(NodeRow.task_id == task_id)
        )
        ids = list(node_ids.scalars())
        if ids:
            await self.session.execute(delete(LeaseRow).where(LeaseRow.node_id.in_(ids)))
        await self.session.execute(delete(EventRow).where(EventRow.task_id == task_id))
        await self.session.execute(delete(EdgeRow).where(EdgeRow.task_id == task_id))
        await self.session.execute(delete(NodeRow).where(NodeRow.task_id == task_id))
        result = await self.session.execute(delete(TaskRow).where(TaskRow.id == task_id))
        await self.session.commit()
        return result.rowcount > 0
