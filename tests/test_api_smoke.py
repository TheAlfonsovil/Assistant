"""End-to-end smoke of the HTTP surface with an injected mock context.

Unit tests cover the pieces; this walks the real ASGI app — routers, request
models, serializers and response shaping — which is where a missing import or a
renamed field actually breaks a deployment.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from httpx import ASGITransport, AsyncClient

from assistant.api.application import API_PREFIX, app
from assistant.config import Settings
from assistant.runtime import TaskRuntime
from assistant.startup.bootstrap import create_context
from tests.test_attachments import PNG_BYTES, _upload


@pytest.fixture
async def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'smoke.db'}",
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


@pytest.mark.asyncio
async def test_read_only_endpoints_respond(client):
    http, _context = client

    for path in (
        "/health",
        "/overview",
        "/metrics",
        "/observability",
        "/resources",
        "/projects",
        "/tasks",
        "/memory/summary",
        "/runtime/idle",
        "/runtime/offpeak",
    ):
        response = await http.get(f"{API_PREFIX}{path}")
        assert response.status_code == 200, (path, response.text)

    metrics = (await http.get(f"{API_PREFIX}/metrics")).json()
    assert metrics["model_label"] == "DeepSeek V4.1 Flash"
    assert {"queue", "reliability", "worker_outcomes", "provider_latency", "cost"} <= set(metrics)
    assert metrics["cost"]["configured"] is False

    health = (await http.get(f"{API_PREFIX}/health")).json()
    assert health["status"] in {"READY", "DEGRADED"}


@pytest.mark.asyncio
async def test_resources_report_perception_honestly(client):
    """The panel must say whether the configured model can read images."""
    http, _context = client

    payload = (await http.get(f"{API_PREFIX}/resources")).json()

    vision = payload["vision"]
    assert isinstance(vision["model_reads_images"], bool)
    assert vision["model"] == "deepseek-flash"
    assert "detail" in vision
    if vision["model_reads_images"]:
        assert vision["images_per_request"] >= 1
    else:
        assert vision["images_per_request"] == 0
        assert "text-only" in vision["note"]
    assert {"hardware", "devices", "tools"} <= set(payload)


@pytest.mark.asyncio
async def test_creating_a_task_returns_the_stored_fields_and_runs_to_terminal(client):
    http, context = client

    created = await http.post(
        f"{API_PREFIX}/tasks",
        json={
            "title": "Smoke task",
            "goal": "resume el estado del asistente",
            "description": "detalle de la tarea",
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["title"] == "Smoke task"
    assert body["description"] == "detalle de la tarea"
    assert body["status"] in {"QUEUED", "WAITING"}

    result = await context.service.run_task(body["id"])

    assert result is not None
    assert result.status.value in {"SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"}
    assert result.runtime.final_response is not None
    # The instruction carries both halves of the request.
    assert "detalle de la tarea" in result.instruction


@pytest.mark.asyncio
async def test_the_project_payload_lists_the_index_shape_not_the_index(client):
    http, context = client
    root = pathlib.Path(context.settings.projects_root) / "sample"
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("def entry():\n    return 1\n", encoding="utf-8")

    created = await http.post(
        f"{API_PREFIX}/projects",
        json={"name": "sample", "path": str(root), "project_type": "code"},
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]
    refreshed = await http.post(f"{API_PREFIX}/projects/{project_id}/codegraph/refresh")
    assert refreshed.status_code == 200, refreshed.text

    listing = (await http.get(f"{API_PREFIX}/projects")).json()
    payload = next(item for item in listing if item["id"] == project_id)

    assert payload["codegraph"]["file_count"] >= 1
    assert payload["codegraph"]["module_count"] >= 1
    # The stored graph itself never rides along in a list response.
    assert "symbols" not in payload["codegraph"]
    assert "graph" not in payload["codegraph"]
    assert "dependency_edges" not in payload["codegraph"]
    assert len(json.dumps(listing)) < 4_000


@pytest.mark.asyncio
async def test_invalid_attachment_is_a_422_and_creates_nothing(client):
    http, context = client

    response = await http.post(
        f"{API_PREFIX}/tasks",
        json={"title": "Adjunto roto", "attachments": [_upload(b"not an image")]},
    )

    assert response.status_code == 422
    assert "unsupported image format" in response.text
    assert await context.service.repository.list_tasks() == []


@pytest.mark.asyncio
async def test_valid_attachment_is_stored_and_exposed(client):
    http, _context = client

    response = await http.post(
        f"{API_PREFIX}/tasks",
        json={"title": "Con captura", "attachments": [_upload(PNG_BYTES)]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["metadata"]["attachments"]) == 1
    assert body["metadata"]["attachments"][0]["content_type"] == "image/png"


@pytest.mark.asyncio
async def test_offpeak_toggle_round_trips_through_the_api(client):
    http, _context = client

    before = (await http.get(f"{API_PREFIX}/runtime/offpeak")).json()
    assert before["enabled"] is False

    enabled = await http.put(f"{API_PREFIX}/runtime/offpeak", json={"enabled": True})
    assert enabled.status_code == 200
    payload = enabled.json()
    assert payload["enabled"] is True
    assert payload["peak_windows_utc"] == ["01:00-04:00", "06:00-10:00"]

    overview = (await http.get(f"{API_PREFIX}/overview")).json()
    assert overview["runtime"]["offpeak"]["enabled"] is True

    disabled = await http.put(f"{API_PREFIX}/runtime/offpeak", json={"enabled": False})
    assert disabled.json()["enabled"] is False


@pytest.mark.asyncio
async def test_chat_endpoint_accepts_text_and_attachments(client):
    http, _context = client

    response = await http.post(
        f"{API_PREFIX}/chat",
        json={"message": "¿cuántas tareas hay?", "attachments": []},
    )

    assert response.status_code == 200, response.text
    assert response.json()["source"] == "DASHBOARD_CHAT"


@pytest.mark.asyncio
async def test_unknown_task_detail_is_a_404(client):
    http, _context = client

    response = await http.get(f"{API_PREFIX}/tasks/does-not-exist")

    assert response.status_code in {404, 409}
