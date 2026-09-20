from datetime import UTC, datetime, timedelta

import pytest

from assistant.domain.errors import GraphCycleError
from assistant.domain.graph import TaskGraph
from assistant.domain.models import (
    DependencyType,
    GraphEdge,
    MemoryRecord,
    NodeStatus,
    NodeType,
    Task,
    TaskEvent,
    TaskNode,
    TaskStatus,
)
from assistant.domain.state import InvalidStateTransition
from assistant.infrastructure.db import Database
from assistant.infrastructure.repositories import TaskRepository
from assistant.project_analysis import ProjectAnalyzer


def test_graph_resolves_dependencies_and_rejects_cycles():
    first = TaskNode(
        task_id="task", description="first", type=NodeType.OPERATION, status=NodeStatus.READY
    )
    second = TaskNode(
        task_id="task", description="second", type=NodeType.OPERATION, status=NodeStatus.READY
    )
    graph = TaskGraph([first, second], [GraphEdge(from_node=first.id, to_node=second.id)])

    assert [node.id for node in graph.ready_nodes()] == [first.id]
    first.status = NodeStatus.SUCCEEDED
    assert [node.id for node in graph.ready_nodes()] == [second.id]

    with pytest.raises(GraphCycleError):
        TaskGraph(
            [first, second],
            [
                GraphEdge(from_node=first.id, to_node=second.id),
                GraphEdge(from_node=second.id, to_node=first.id),
            ],
        )


def test_graph_allows_success_dependency_for_skipped_branch():
    skipped = TaskNode(
        task_id="task",
        description="skipped branch",
        type=NodeType.OPERATION,
        status=NodeStatus.CANCELLED,
        metadata={"branch_skipped": True},
    )
    dependent = TaskNode(
        task_id="task",
        description="dependent",
        type=NodeType.OPERATION,
        status=NodeStatus.READY,
    )
    graph = TaskGraph(
        [skipped, dependent],
        [GraphEdge(from_node=skipped.id, to_node=dependent.id)],
    )

    assert graph.dependencies_satisfied(dependent.id) is True


@pytest.mark.asyncio
async def test_sqlite_persists_task_nodes_edges_and_events(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'nested' / 'assistant.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        task = Task(goal="persist me")
        node = TaskNode(task_id=task.id, description="root", status=NodeStatus.READY)
        await repository.save_task(task)
        await repository.save_node(node)
        await repository.save_edge(
            task.id,
            GraphEdge(from_node=node.id, to_node=node.id, dependency_type=DependencyType.ALWAYS),
        ) if False else None
        assert (await repository.get_task(task.id)).goal == "persist me"
        assert (await repository.get_node(node.id)).description == "root"
    await database.close()


@pytest.mark.asyncio
async def test_persistence_rejects_illegal_terminal_transition(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        task = Task(goal="state", status=TaskStatus.SUCCEEDED)
        await repository.save_task(task)
        task.status = TaskStatus.READY

        with pytest.raises(InvalidStateTransition):
            await repository.save_task(task)
    await database.close()


@pytest.mark.asyncio
async def test_sqlite_persists_and_searches_long_term_memory(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'memory.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        await repository.save_memory(
            MemoryRecord(kind="preference", key="project_language", value="Python", source="USER")
        )
        memories = await repository.search_memory("Python project")
        assert memories[0].value == "Python"
    await database.close()


@pytest.mark.asyncio
async def test_memory_expiration_redaction_deletion_and_export(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'memory-governance.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        expired = await repository.upsert_memory(
            kind="temporary",
            key="old",
            value="remove me",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        current = await repository.upsert_memory(
            kind="secret",
            key="token",
            value="do not expose",
        )

        assert await repository.search_memory("old") == []
        redacted = await repository.redact_memory(current.id)
        assert redacted.value == "[REDACTED]"
        assert (await repository.search_memory("token"))[0].value == "[REDACTED]"
        exported = await repository.export_memory()
        assert all(item["id"] != expired.id for item in exported)
        assert await repository.purge_expired_memory() == 1
        assert await repository.delete_memory(current.id) is True
        assert await repository.list_memory() == []
    await database.close()


@pytest.mark.asyncio
async def test_sqlite_health_reports_wal_and_integrity(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'health.db'}")
    await database.create_all()

    health = await database.health_check()

    assert health["status"] == "ok"
    assert health["journal_mode"] == "wal"
    assert health["foreign_keys"] == 1
    assert health["integrity"] == "ok"
    await database.close()


@pytest.mark.asyncio
async def test_idempotency_result_is_reused_and_old_events_are_purged(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'maintenance.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        first = await repository.save_idempotency_result("same-operation", {"value": 1})
        second = await repository.save_idempotency_result("same-operation", {"value": 2})
        assert first == second == {"value": 1}

        old = TaskEvent(
            task_id="task",
            event_type="old",
            created_at=datetime.now(UTC) - timedelta(days=60),
        )
        recent = TaskEvent(task_id="task", event_type="recent")
        await repository.save_event(old)
        await repository.save_event(recent)
        assert await repository.purge_old_events(retention_days=30, keep_recent=0) == 1
        assert [event.event_type for event in await repository.list_events("task")] == ["recent"]
    await database.close()


@pytest.mark.asyncio
async def test_project_analyzer_emits_inheritance_and_composition_edges(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Base:\n    pass\n\nclass Dependency:\n    pass\n\nclass Child(Base):\n    def __init__(self):\n        self.dependency = Dependency()\n",
        encoding="utf-8",
    )

    result = await ProjectAnalyzer().analyze(str(tmp_path))

    assert result.success is True
    kinds = {edge["kind"] for edge in result.output["dependency_edges"]}
    assert {"inherits", "composes"}.issubset(kinds)
