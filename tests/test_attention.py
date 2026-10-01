"""A waiting task must be visible to the person, not just parked.

The runtime deliberately keeps dispatching other tasks while one waits for a
human (verified in ``runtime.py``: WAITING is not in the active set). What the
landing view has to add is the part a queue cannot do on its own: say *which*
task is waiting and *what* it is asking.
"""

from __future__ import annotations

import pytest

from assistant.api.serializers import needs_attention
from assistant.domain.models import (
    NodeStatus,
    NodeType,
    Task,
    TaskNode,
    TaskStatus,
)
from assistant.infrastructure.db import Database
from assistant.infrastructure.repositories import TaskRepository


async def _repository(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'attention.db'}")
    await database.create_all()
    session = database.sessions()
    return TaskRepository(session), database, session


async def _waiting_task(repository, *, metadata=None, question="which project?") -> Task:
    task = Task(goal="audita el repositorio", status=TaskStatus.WAITING, metadata=metadata or {})
    await repository.save_task(task)
    node = TaskNode(
        task_id=task.id,
        type=NodeType.OPERATION,
        description="choose a project",
        status=NodeStatus.WAITING,
        error=question,
    )
    await repository.save_node(node)
    return task


@pytest.mark.asyncio
async def test_a_waiting_task_is_reported_with_its_question(tmp_path):
    repository, database, session = await _repository(tmp_path)
    try:
        task = await _waiting_task(repository)

        items = await needs_attention(repository)

        assert len(items) == 1
        item = items[0]
        assert item["task_id"] == task.id
        assert item["question"] == "which project?"
        assert item["kind"] == "question"
        assert item["answer_with"] == f"POST /api/v1/tasks/{task.id}/input"
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_a_schedule_holder_waits_by_design_and_is_not_a_question(tmp_path):
    """Recurrence is expressed as a WAITING holder: it must not nag forever."""
    repository, database, session = await _repository(tmp_path)
    try:
        holder = await _waiting_task(
            repository, metadata={"schedule": {"kind": "daily", "enabled": True}}
        )
        await _waiting_task(repository, question="a real question")

        items = await needs_attention(repository)

        assert [item["task_id"] for item in items] != [holder.id]
        assert len(items) == 1
        assert items[0]["question"] == "a real question"
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_a_project_selection_offers_its_options(tmp_path):
    repository, database, session = await _repository(tmp_path)
    try:
        task = await _waiting_task(repository)
        task.runtime.clarification = {
            "kind": "project_selection",
            "options": [{"id": "a", "name": "Assistant"}, {"id": "b", "name": "test_zone"}],
        }
        await repository.save_task(task)

        items = await needs_attention(repository)

        assert items[0]["kind"] == "project_selection"
        assert items[0]["options"] == ["Assistant", "test_zone"]
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_running_tasks_are_not_reported_as_needing_attention(tmp_path):
    repository, database, session = await _repository(tmp_path)
    try:
        task = Task(goal="corre", status=TaskStatus.RUNNING)
        await repository.save_task(task)

        assert await needs_attention(repository) == []
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_a_repository_without_the_status_query_degrades_to_empty():
    class Partial:
        pass

    assert await needs_attention(Partial()) == []
