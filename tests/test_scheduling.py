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


@pytest.mark.asyncio
async def test_daily_creates_a_holder_for_a_wall_clock_time(make_service):
    service = await make_service("schedule-daily.db")

    result = await ScheduleTool(service).execute(
        "daily",
        {
            "goal": "revisa las alertas",
            "at_hour": 8,
            "at_minute": 30,
            "timezone": "UTC+02:00",
        },
        timeout=5,
    )

    assert result.success is True, result.error
    schedule = result.output["schedule"]
    assert schedule["kind"] == "daily"
    assert schedule["every_seconds"] is None
    assert (schedule["at_hour"], schedule["at_minute"]) == (8, 30)
    assert schedule["timezone"] == "UTC+02:00"
    assert schedule["text"] == "every day at 08:30 (UTC+02:00)"
    assert datetime.fromisoformat(schedule["next_run_at"]).minute == 30


@pytest.mark.asyncio
async def test_a_daily_schedule_fires_once_and_advances_a_day(make_service):
    service = await make_service("schedule-daily-fire.db")
    created = await ScheduleTool(service).execute(
        "daily", {"goal": "parte diario", "at_hour": 7}, timeout=5
    )
    holder_id = created.output["schedule"]["task_id"]
    holder = await service.get_task(holder_id)
    now = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
    # The 07:00 window of today has passed; it becomes due immediately.
    holder.metadata["schedule"]["next_run_at"] = datetime(2026, 9, 30, 7, 0, tzinfo=UTC).isoformat()
    await service.repository.save_task(holder)

    fired = await service.reconcile_schedules(now=now)

    assert fired == 1
    refreshed = await service.get_task(holder_id)
    schedule = refreshed.metadata["schedule"]
    assert schedule["runs"] == 1
    # A daily schedule never queues the days it missed.
    assert schedule["next_run_at"] == datetime(2026, 10, 1, 7, 0, tzinfo=UTC).isoformat()
    assert schedule["kind"] == "daily"


@pytest.mark.asyncio
async def test_an_unreadable_schedule_is_skipped_instead_of_guessed(make_service):
    service = await make_service("schedule-broken.db")
    created = await ScheduleTool(service).execute(
        "every", {"goal": "vigila", "every_seconds": 300}, timeout=5
    )
    holder = await service.get_task(created.output["schedule"]["task_id"])
    # The record survives a downgrade: an interval without bounds is not a
    # schedule this version can honour, so it must not fire.
    holder.metadata["schedule"] = {"every_seconds": 0, "enabled": True, "runs": 0}
    await service.repository.save_task(holder)

    assert await service.reconcile_schedules() == 0


@pytest.mark.asyncio
async def test_update_replaces_the_recurrence_and_keeps_the_history(make_service):
    service = await make_service("schedule-update.db")
    created = await ScheduleTool(service).execute(
        "every", {"goal": "vigila", "every_seconds": 600}, timeout=5
    )
    holder_id = created.output["schedule"]["task_id"]
    holder = await service.get_task(holder_id)
    holder.metadata["schedule"]["runs"] = 4
    await service.repository.save_task(holder)

    updated = await service.update_schedule(holder_id, at_hour=6, timezone_name="UTC+01:00")

    assert updated is not None
    schedule = updated.metadata["schedule"]
    assert schedule["kind"] == "daily"
    assert schedule["every_seconds"] is None
    assert schedule["timezone"] == "UTC+01:00"
    # Changing the recurrence cannot erase what already ran.
    assert schedule["runs"] == 4
    events = [event.event_type for event in await service.repository.list_events(holder_id)]
    assert "SCHEDULE_UPDATED" in events


@pytest.mark.asyncio
async def test_pause_and_resume_keep_the_holder_alive(make_service):
    service = await make_service("schedule-resume.db")
    created = await ScheduleTool(service).execute(
        "every", {"goal": "vigila", "every_seconds": 3600}, timeout=5
    )
    holder_id = created.output["schedule"]["task_id"]

    paused = await service.pause_schedule(holder_id)
    assert paused is not None
    assert paused.metadata["schedule"]["enabled"] is False
    # Pausing is not cancelling: the holder stays alive and stays WAITING.
    assert paused.status is TaskStatus.WAITING
    assert service.schedule_view(paused)["paused"] is True

    resumed = await service.resume_schedule(holder_id)

    assert resumed is not None
    assert resumed.status is TaskStatus.WAITING
    schedule = resumed.metadata["schedule"]
    assert schedule["enabled"] is True
    assert "paused_at" not in schedule
    # Resuming never fires the instant it is resumed.
    assert datetime.fromisoformat(schedule["next_run_at"]) > datetime.now(UTC)
    events = [event.event_type for event in await service.repository.list_events(holder_id)]
    assert {"SCHEDULE_PAUSED", "SCHEDULE_RESUMED"} <= set(events)


@pytest.mark.asyncio
async def test_a_cancelled_schedule_is_final(make_service):
    service = await make_service("schedule-final.db")
    created = await ScheduleTool(service).execute(
        "every", {"goal": "vigila", "every_seconds": 600}, timeout=5
    )
    holder_id = created.output["schedule"]["task_id"]
    await service.cancel_schedule(holder_id)

    with pytest.raises(ValueError, match="final"):
        await service.resume_schedule(holder_id)
    with pytest.raises(ValueError, match="finished"):
        await service.update_schedule(holder_id, every_seconds=900)
    with pytest.raises(ValueError, match="already"):
        await service.pause_schedule(holder_id)

    cancelled = await service.get_task(holder_id)
    assert cancelled.status is TaskStatus.CANCELLED
    view = service.schedule_view(cancelled)
    assert (view["cancelled"], view["paused"]) == (True, False)


@pytest.mark.asyncio
async def test_list_schedules_puts_the_enabled_and_nearest_first(make_service):
    service = await make_service("schedule-list.db")
    tool = ScheduleTool(service)
    soon = await tool.execute("every", {"goal": "cada minuto", "every_seconds": 60}, timeout=5)
    later = await tool.execute("every", {"goal": "cada día", "every_seconds": 86400}, timeout=5)
    await service.cancel_schedule(soon.output["schedule"]["task_id"])

    listed = await tool.execute("list", {}, timeout=5)

    schedules = listed.output["schedules"]
    assert listed.output["count"] == 2
    assert schedules[0]["task_id"] == later.output["schedule"]["task_id"]
    assert schedules[-1]["enabled"] is False


@pytest.mark.asyncio
async def test_a_holder_written_before_the_daily_kind_still_fires(make_service):
    service = await make_service("schedule-legacy.db")
    created = await ScheduleTool(service).execute(
        "every", {"goal": "vigila", "every_seconds": 300}, timeout=5
    )
    holder_id = created.output["schedule"]["task_id"]
    holder = await service.get_task(holder_id)
    # Exactly what the previous version persisted: no "kind" key at all.
    holder.metadata["schedule"].pop("kind")
    holder.metadata["schedule"]["next_run_at"] = (
        datetime.now(UTC) - timedelta(minutes=5)
    ).isoformat()
    await service.repository.save_task(holder)

    assert await service.reconcile_schedules() == 1
    assert (await service.get_task(holder_id)).metadata["schedule"]["runs"] == 1


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
