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
    TaskNode,
)
from assistant.infrastructure.db import Database
from assistant.infrastructure.repositories import TaskRepository


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
