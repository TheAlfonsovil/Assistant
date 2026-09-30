"""Recurring work: a holder task that the runtime clones when due."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from assistant.application import TaskService
from assistant.capabilities.scheduling import MAX_EVERY_SECONDS, MIN_EVERY_SECONDS, ScheduleTool
from assistant.context import ContextBuilder
from assistant.devices.registry import build_tool_registry
from assistant.domain.models import ErrorType, TaskRequest, TaskStatus
from assistant.infrastructure.db import Database
from assistant.llm import MockLLMProvider
from assistant.tools import ToolRegistry


@pytest.fixture
async def make_service(tmp_path):
    """Build services and always close their session and engine afterwards."""
    opened: list[tuple[Database, object]] = []

    async def _make(name: str) -> TaskService:
        database = Database(f"sqlite+aiosqlite:///{tmp_path / name}")
        await database.create_all()
        session = database.sessions()
        opened.append((database, session))
        return TaskService(
            session, MockLLMProvider(), ToolRegistry(), workspace_root=str(tmp_path)
        )

    yield _make

    for database, session in opened:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_every_creates_a_waiting_holder(make_service):
    service = await make_service("schedule-create.db")
    tool = ScheduleTool(service)

    result = await tool.execute(
        "every",
        {"goal": "comprueba el estado del servicio", "every_seconds": 900, "title": "Chequeo"},
        timeout=5,
    )

    assert result.success is True, result.error
    schedule = result.output["schedule"]
    assert schedule["every_seconds"] == 900
    assert schedule["status"] == TaskStatus.WAITING.value
    holder = await service.get_task(schedule["task_id"])
    assert holder is not None
    assert holder.status is TaskStatus.WAITING
    assert holder.metadata["schedule"]["enabled"] is True
    events = [event.event_type for event in await service.repository.list_events(holder.id)]
    assert "SCHEDULE_CREATED" in events


@pytest.mark.asyncio
async def test_every_rejects_out_of_range_intervals(make_service):
    service = await make_service("schedule-invalid.db")
    tool = ScheduleTool(service)

    too_fast = await tool.execute("every", {"goal": "x", "every_seconds": 5}, timeout=5)
    too_slow = await tool.execute(
        "every", {"goal": "x", "every_seconds": MAX_EVERY_SECONDS + 1}, timeout=5
    )

    assert too_fast.success is False and too_fast.error_type is ErrorType.INVALID_ARGUMENT
    assert f"between {MIN_EVERY_SECONDS}" in too_fast.error
    assert too_slow.success is False


@pytest.mark.asyncio
async def test_due_schedule_is_cloned_and_holder_advances(make_service):
    service = await make_service("schedule-fire.db")
    tool = ScheduleTool(service)
    created = await tool.execute(
        "every", {"goal": "revisa la cola", "every_seconds": 600}, timeout=5
    )
    holder_id = created.output["schedule"]["task_id"]
    holder = await service.get_task(holder_id)

    # Force it due two windows in the past: only ONE run must appear.
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    holder.metadata["schedule"]["next_run_at"] = (now - timedelta(minutes=25)).isoformat()
    await service.repository.save_task(holder)

    fired = await service.reconcile_schedules(now=now)

    assert fired == 1
    refreshed = await service.get_task(holder_id)
    assert refreshed is not None
    schedule = refreshed.metadata["schedule"]
    assert schedule["runs"] == 1
    assert datetime.fromisoformat(schedule["next_run_at"]) > now

    clones = [
        task
        for task in await service.repository.list_tasks()
        if task.source == "SCHEDULE" and task.id != holder_id
    ]
    assert len(clones) == 1
    assert clones[0].status is TaskStatus.QUEUED
    assert clones[0].goal == "revisa la cola"
    assert clones[0].parent_task_id == holder_id
    # The clone must route and review from scratch.
    assert "schedule" not in clones[0].metadata
    assert "orchestration_stage" not in clones[0].metadata

    events = await service.repository.list_events(holder_id)
    assert any(event.event_type == "SCHEDULE_FIRED" for event in events)


@pytest.mark.asyncio
async def test_a_busy_queue_does_not_starve_a_due_schedule(make_service):
    service = await make_service("schedule-busy.db")
    created = await ScheduleTool(service).execute(
        "every", {"goal": "vigila", "every_seconds": 300}, timeout=5
    )
    holder_id = created.output["schedule"]["task_id"]
    holder = await service.get_task(holder_id)
    holder.metadata["schedule"]["next_run_at"] = (
        datetime.now(UTC) - timedelta(minutes=1)
    ).isoformat()
    await service.repository.save_task(holder)

    # has_work=True short-circuits idle supervision; recurrence must still run.
    await service.reconcile_idle(has_work=True)

    refreshed = await service.get_task(holder_id)
    assert refreshed.metadata["schedule"]["runs"] == 1


@pytest.mark.asyncio
async def test_future_schedule_does_not_fire(make_service):
    service = await make_service("schedule-future.db")
    created = await ScheduleTool(service).execute(
        "every", {"goal": "nada aún", "every_seconds": 3600}, timeout=5
    )

    fired = await service.reconcile_schedules()

    assert fired == 0
    tasks = await service.repository.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].id == created.output["schedule"]["task_id"]


@pytest.mark.asyncio
async def test_cancelled_schedule_stops_firing_and_is_listed(make_service):
    service = await make_service("schedule-cancel.db")
    tool = ScheduleTool(service)
    created = await tool.execute("every", {"goal": "vigila", "every_seconds": 300}, timeout=5)
    holder_id = created.output["schedule"]["task_id"]

    listed = await tool.execute("list", {}, timeout=5)
    assert listed.output["count"] == 1

    cancelled = await tool.execute("cancel", {"task_id": holder_id}, timeout=5)
    assert cancelled.success is True
    assert cancelled.output["schedule"]["enabled"] is False

    holder = await service.get_task(holder_id)
    assert holder.status is TaskStatus.CANCELLED
    holder.metadata["schedule"]["next_run_at"] = (
        datetime.now(UTC) - timedelta(hours=1)
    ).isoformat()
    await service.repository.save_task(holder)

    assert await service.reconcile_schedules() == 0


@pytest.mark.asyncio
async def test_cancel_reports_an_unknown_schedule(make_service):
    service = await make_service("schedule-unknown.db")

    result = await ScheduleTool(service).execute("cancel", {"task_id": "nope"}, timeout=5)

    assert result.success is False
    assert result.error_type is ErrorType.NOT_FOUND


@pytest.mark.asyncio
async def test_recurring_work_inherits_the_project_and_attachments(make_service, tmp_path):
    service = await make_service("schedule-scope.db")
    from assistant.domain.contracts import ArtifactKind, ArtifactRef
    from assistant.domain.models import Project

    project_path = tmp_path / "proj"
    project_path.mkdir()
    project = await service.create_project(Project(name="p", path=str(project_path)))
    parent = await service.create_task(
        TaskRequest(goal="vigila el proyecto", project_id=project.id)
    )
    parent.metadata["attachments"] = [{"id": "a1", "path": str(tmp_path / "x.png")}]
    await service.repository.save_task(parent)
    await service.repository.save_artifact(
        parent.id,
        ArtifactRef(
            id="a1",
            kind=ArtifactKind.IMAGE,
            description="x",
            producer_node_id="n1",
            path=str(tmp_path / "x.png"),
        ),
    )

    result = await ScheduleTool(service).execute(
        "every",
        {"_task_id": parent.id, "goal": "vigila otra vez", "every_seconds": 1800},
        timeout=5,
    )

    holder = await service.get_task(result.output["schedule"]["task_id"])
    assert holder.project_id == project.id
    assert holder.metadata["attachments"] == parent.metadata["attachments"]


def test_schedule_capability_is_visible_to_the_model():
    class _Repository:
        async def get_project(self, project_id):
            return None

        async def list_projects(self, enabled_only=False):
            return []

        async def list_events(self, task_id):
            return []

        async def list_artifacts(self, task_id):
            return []

        async def search_memory(self, query, limit=8, scope=None, scope_id=None):
            return []

        async def list_memory(self, limit=20, scope=None, scope_id=None):
            return []

    # Recurrence needs the service, so `create_context` (the only production
    # entry point) registers it after building the service — same as here.
    tools = build_tool_registry(repository=_Repository())
    tools.register(ScheduleTool(None))
    builder = ContextBuilder(
        repository=_Repository(),
        tools=tools,
        vision_enabled=False,
    )

    for intent in ("audit", "browser", "general"):
        visible = {
            tool["name"]
            for group in builder._available_actions(intent=intent)
            for tool in group["tools"]
            if isinstance(tool, dict)
        }
        assert {"http", "screen", "schedule"} <= visible, intent
