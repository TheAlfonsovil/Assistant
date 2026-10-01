"""The HTTP surface for recurring work.

The scheduler itself is covered elsewhere; these walk the real ASGI app, which
is where a missing router, a wrong status code or an unhandled validation error
actually breaks a deployment.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from assistant.api.application import API_PREFIX, app
from assistant.config import Settings
from assistant.runtime import TaskRuntime
from assistant.startup.bootstrap import create_context


@pytest.fixture
async def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'schedules.db'}",
        deepseek_api_key="",
        workspace_root=str(tmp_path),
        projects_root=str(tmp_path / "projects"),
        idle_enabled=False,
        # Pinned so the assertions do not depend on the developer's .env.
        schedule_timezone="UTC",
    )
    context = await create_context(use_mock=True, settings=settings)
    app.state.context = context
    app.state.runtime = TaskRuntime(context.service.repository, lambda task_id: None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://assistant.test") as http:
        yield http, context
    await context.close()


@pytest.mark.asyncio
async def test_the_listing_explains_the_scheduling_environment(client):
    http, _context = client

    response = await http.get(f"{API_PREFIX}/schedules")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["count"] == 0
    assert payload["default_timezone"] == "UTC"
    assert "UTC" in payload["common_timezones"]
    assert isinstance(payload["timezone_database"], bool)


@pytest.mark.asyncio
async def test_an_interval_schedule_round_trips(client):
    http, _context = client

    created = await http.post(
        f"{API_PREFIX}/schedules",
        json={"goal": "comprueba el servicio", "every_seconds": 900, "title": "Chequeo"},
    )

    assert created.status_code == 200, created.text
    schedule = created.json()
    assert schedule["kind"] == "interval"
    assert schedule["text"] == "every 15 min"
    assert schedule["enabled"] is True
    assert schedule["status"] == "WAITING"
    assert schedule["next_run_at"]

    listing = (await http.get(f"{API_PREFIX}/schedules")).json()
    assert listing["count"] == 1
    assert listing["enabled"] == 1
    assert listing["schedules"][0]["task_id"] == schedule["task_id"]


@pytest.mark.asyncio
async def test_a_holder_is_an_ordinary_task(client):
    http, _context = client
    created = (await http.post(f"{API_PREFIX}/schedules", json={"goal": "vigila", "every_seconds": 600})).json()

    task = await http.get(f"{API_PREFIX}/tasks/{created['task_id']}")

    assert task.status_code == 200, task.text
    body = task.json()["task"]
    assert body["status"] == "WAITING"
    assert body["source"] == "SCHEDULE"
    assert body["metadata"]["schedule"]["every_seconds"] == 600


@pytest.mark.asyncio
async def test_a_daily_schedule_reports_its_local_time(client):
    http, _context = client

    created = await http.post(
        f"{API_PREFIX}/schedules",
        json={"goal": "parte diario", "at_hour": 7, "at_minute": 30, "timezone": "UTC+02:00"},
    )

    assert created.status_code == 200, created.text
    schedule = created.json()
    assert schedule["kind"] == "daily"
    assert schedule["text"] == "every day at 07:30 (UTC+02:00)"
    assert schedule["next_run_local"] and schedule["next_run_local"].endswith("07:30 UTC+02:00")


@pytest.mark.asyncio
async def test_an_unusable_specification_is_rejected_as_unprocessable(client):
    http, _context = client

    no_kind = await http.post(f"{API_PREFIX}/schedules", json={"goal": "x"})
    too_fast = await http.post(
        f"{API_PREFIX}/schedules", json={"goal": "x", "every_seconds": 5}
    )
    impossible_hour = await http.post(
        f"{API_PREFIX}/schedules", json={"goal": "x", "at_hour": 25}
    )
    unknown_zone = await http.post(
        f"{API_PREFIX}/schedules",
        json={"goal": "x", "at_hour": 8, "timezone": "Mars/Olympus"},
    )

    for response in (no_kind, too_fast, impossible_hour, unknown_zone):
        assert response.status_code == 422, response.text
        assert response.json()["detail"]
    assert (await http.get(f"{API_PREFIX}/schedules")).json()["count"] == 0


@pytest.mark.asyncio
async def test_the_recurrence_can_be_changed_without_losing_the_holder(client):
    http, _context = client
    created = (await http.post(f"{API_PREFIX}/schedules", json={"goal": "vigila", "every_seconds": 600})).json()

    updated = await http.put(
        f"{API_PREFIX}/schedules/{created['task_id']}", json={"every_seconds": 86400}
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["text"] == "every 1 d"
    assert updated.json()["task_id"] == created["task_id"]

    bad = await http.put(f"{API_PREFIX}/schedules/{created['task_id']}", json={"at_hour": 99})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_pause_resume_and_cancel_are_distinct(client):
    http, _context = client
    created = (await http.post(f"{API_PREFIX}/schedules", json={"goal": "vigila", "every_seconds": 600})).json()
    path = f"{API_PREFIX}/schedules/{created['task_id']}"

    paused = await http.post(f"{path}/pause")
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    assert paused.json()["cancelled"] is False
    assert paused.json()["status"] == "WAITING"

    resumed = await http.post(f"{path}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["enabled"] is True
    assert resumed.json()["paused"] is False

    cancelled = await http.post(f"{path}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["cancelled"] is True
    assert cancelled.json()["status"] == "CANCELLED"

    refused = await http.post(f"{path}/resume")
    assert refused.status_code == 409

    # A cancelled schedule is final: neither resuming nor editing may revive it.
    edited = await http.put(path, json={"every_seconds": 900})
    assert edited.status_code == 409
    assert (await http.post(f"{path}/pause")).status_code == 409


@pytest.mark.asyncio
async def test_unknown_schedules_report_not_found(client):
    http, _context = client

    for path, verb in (
        ("/cancel", "post"),
        ("/pause", "post"),
        ("/resume", "post"),
    ):
        response = await getattr(http, verb)(f"{API_PREFIX}/schedules/missing{path}")
        assert response.status_code == 404, (path, response.text)

    updated = await http.put(f"{API_PREFIX}/schedules/missing", json={"every_seconds": 600})
    assert updated.status_code == 404
