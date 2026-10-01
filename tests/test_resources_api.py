"""The resources read model: what this machine has and what it can show.

The hardware inventory is best-effort by design, so these tests assert the
contract (shape, safe serving, refusal) rather than exact capacities, which
would differ on every host.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from assistant.api.application import API_PREFIX, app
from assistant.config import Settings
from assistant.devices.computer.inventory import physical_memory, storage
from assistant.runtime import TaskRuntime
from assistant.startup.bootstrap import create_context


@pytest.fixture
async def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'resources.db'}",
        deepseek_api_key="",
        workspace_root=str(tmp_path),
        projects_root=str(tmp_path / "projects"),
        idle_enabled=False,
    )
    context = await create_context(use_mock=True, settings=settings)
    app.state.context = context
    app.state.runtime = TaskRuntime(context.service.repository, lambda task_id: None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://assistant.test") as http:
        yield http, context
    await context.close()


def test_memory_and_storage_never_raise_on_an_unknown_host():
    memory = physical_memory()
    disks = storage()
    workspace_disks = storage("C:/projects/Assistant" if Path("C:/").exists() else None)

    assert memory is None or {
        "total_bytes",
        "available_bytes",
        "load_percent",
    } <= set(memory)
    if memory:
        assert memory["total_bytes"] > 0
        assert 0 <= memory["load_percent"] <= 100
    for entry in [*disks, *workspace_disks]:
        assert entry["total_bytes"] > 0
        assert entry["free_bytes"] >= 0
        assert 0 <= entry["used_percent"] <= 100


@pytest.mark.asyncio
async def test_resources_reports_hardware_and_capabilities(client):
    http, _context = client

    response = await http.get(f"{API_PREFIX}/resources")

    assert response.status_code == 200, response.text
    payload = response.json()
    hardware = payload["hardware"]
    assert hardware["host"]["hostname"]
    assert hardware["cpu"]["logical_cores"] >= 1
    assert hardware["capabilities"]["screen_capture"] is (Path("C:/").exists())
    # Input control is opt-in, so the view must say so instead of looking broken.
    assert hardware["capabilities"]["input_control"] is False
    assert "ASSISTANT_ENABLE_INPUT_CONTROL" in hardware["capabilities"]["input_control_hint"]
    display = hardware["display"]
    assert display["monitor_count"] == len(display["monitors"])
    for monitor in display["monitors"]:
        assert {"index", "left", "top", "width", "height", "primary"} <= set(monitor)


@pytest.mark.asyncio
@pytest.mark.skipif(not Path("C:/").exists(), reason="screen capture is Windows only")
async def test_a_capture_can_be_taken_and_viewed(client):
    http, _context = client

    captured = await http.post(f"{API_PREFIX}/resources/capture", params={"monitor": 0})

    assert captured.status_code == 200, captured.text
    payload = captured.json()
    assert payload["image_width"] > 0 and payload["image_height"] > 0
    assert 0 < payload["scale"] <= 1
    assert payload["coordinate_hint"]
    filename = payload["filename"]

    try:
        served = await http.get(f"{API_PREFIX}/resources/screenshot/{filename}")
        assert served.status_code == 200
        assert served.headers["content-type"] == "image/png"
        assert served.content[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        (Path("data") / "screenshots" / filename).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_only_real_capture_filenames_are_served(client):
    http, _context = client

    for name in (
        "..%2F..%2Fassistant.db",
        "not-a-capture.png",
        f"{'a' * 32}.png",
        f"{'a' * 31}.png",
    ):
        response = await http.get(f"{API_PREFIX}/resources/screenshot/{name}")
        assert response.status_code in {404, 422}, (name, response.status_code)


@pytest.mark.asyncio
async def test_an_impossible_monitor_index_is_refused(client):
    http, _context = client

    response = await http.post(f"{API_PREFIX}/resources/capture", params={"monitor": 99})

    assert response.status_code == 409
    assert "does not exist" in response.json()["detail"]
