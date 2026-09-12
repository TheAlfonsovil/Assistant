from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.domain.models import GraphEdge, MemoryRecord, Task, TaskEvent, TaskNode

from .orm import EdgeRow, EventRow, MemoryRow, NodeRow, TaskRow


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
        }
        if row is None:
            self.session.add(MemoryRow(**values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        await self.session.commit()

    async def search_memory(self, query: str, limit: int = 10) -> list[MemoryRecord]:
        result = await self.session.execute(select(MemoryRow).order_by(MemoryRow.updated_at.desc()))
        terms = {term.lower() for term in query.split() if len(term) > 2}
        matches = []
        for row in result.scalars():
            haystack = f"{row.kind} {row.key} {row.value_json}".lower()
            if not terms or any(term in haystack for term in terms):
                row.usage_count += 1
                matches.append(MemoryRecord(
                    id=row.id, kind=row.kind, key=row.key, value=row.value_json,
                    source=row.source, confidence=row.confidence, created_at=row.created_at,
                    updated_at=row.updated_at, usage_count=row.usage_count,
                ))
                if len(matches) == limit:
                    break
        await self.session.commit()
        return matches

    async def list_memory(self, limit: int = 50) -> list[MemoryRecord]:
        result = await self.session.execute(
            select(MemoryRow).order_by(MemoryRow.updated_at.desc()).limit(limit)
        )
        return [
            MemoryRecord(
                id=row.id,
                kind=row.kind,
                key=row.key,
                value=row.value_json,
                source=row.source,
                confidence=row.confidence,
                created_at=row.created_at,
                updated_at=row.updated_at,
                usage_count=row.usage_count,
            )
            for row in result.scalars()
        ]

    async def upsert_memory(
        self, *, kind: str, key: str, value, source: str = "SYSTEM", confidence: float = 1.0
    ) -> MemoryRecord:
        result = await self.session.execute(
            select(MemoryRow).where(MemoryRow.kind == kind, MemoryRow.key == key)
        )
        row = result.scalars().first()
        now = datetime.now(UTC)
        if row is None:
            memory = MemoryRecord(
                kind=kind, key=key, value=value, source=source, confidence=confidence,
                created_at=now, updated_at=now,
            )
            await self.save_memory(memory)
            return memory
        row.value_json = value
        row.source = source
        row.confidence = confidence
        row.updated_at = now
        await self.session.commit()
        return MemoryRecord(
            id=row.id, kind=row.kind, key=row.key, value=row.value_json,
            source=row.source, confidence=row.confidence, created_at=row.created_at,
            updated_at=row.updated_at, usage_count=row.usage_count,
        )

    async def save_node(self, node: TaskNode) -> None:
        row = await self.session.get(NodeRow, node.id)
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
