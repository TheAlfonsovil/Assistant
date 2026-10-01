from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ...devices.computer.inventory import hardware_report
from ...domain.models import Operation
from ..deps import get_context, get_runtime
from ..serializers import (
    _dashboard_analytics,
    _dashboard_devices,
    _event_json,
    _metrics_series,
    needs_attention,
)

router = APIRouter(tags=["dashboard"])

# Screenshots land in this folder, next to the process working directory.
SCREENSHOT_DIR = Path("data") / "screenshots"
SCREENSHOT_NAME = re.compile(r"^[0-9a-f]{32}\.png$")


@router.get("/overview")
async def overview(request: Request) -> dict:
    """Small, bounded read model for the dashboard landing view."""
    context = get_context(request)
    repository = context.service.repository
    runtime = get_runtime(request)
    startup = context.startup
    return {
        "health": {
            "status": startup.status,
            "llm_ready": runtime.readiness_snapshot()
            if runtime.readiness_snapshot() is not None
            else startup.llm_ready,
            "database_ready": startup.database_ready,
            "unfinished_tasks": startup.unfinished_tasks,
        },
        "task_counts": await repository.dashboard_task_counts(),
        # A task that waits for a person keeps its place without blocking the
        # queue, so the landing view has to say which ones are waiting.
        "needs_attention": await needs_attention(repository),
        "projects": [
            {"id": project.id, "name": project.name, "enabled": project.enabled}
            for project in await repository.list_projects()
        ],
        "runtime": {
            "active_tasks": runtime.last_active_count,
            "metrics": runtime.metrics_snapshot(),
            "idle": runtime.idle_snapshot(),
            "offpeak": runtime.offpeak_snapshot(),
        },
    }


@router.get("/metrics/series")
async def metrics_series(request: Request, hours: int = 24) -> dict:
    """Hourly rollup for the dashboard trend view.

    Derived from the retained events, so the available span is bounded by
    ``ASSISTANT_EVENT_RETENTION_DAYS`` rather than by an unbounded history.
    """
    context = get_context(request)
    span = max(1, min(hours, 168))
    events = await context.service.repository.list_recent_events(
        limit=min(5000, span * 120)
    )
    return {
        "hours": span,
        "buckets": _metrics_series(events, span),
        "basis": "hourly rollup of retained events",
    }


@router.get("/observability")
async def observability(request: Request, limit: int = 200) -> dict:
    context = get_context(request)
    runtime = get_runtime(request)
    events = await context.service.repository.list_recent_events(limit=limit)
    return {
        "events": [_event_json(event) for event in events],
        "runtime": {
            "metrics": runtime.metrics_snapshot(),
            "readiness": runtime.readiness_snapshot(),
            "last_error": runtime.last_error,
            "idle": runtime.idle_snapshot(),
            "offpeak": runtime.offpeak_snapshot(),
        },
    }


@router.get("/observability/events/{event_id}")
async def observability_event(request: Request, event_id: str) -> dict:
    """Full detail for one event, including the rendered LLM prompt."""
    event = await get_context(request).service.repository.get_event(event_id)
    if event is None:
        raise HTTPException(404, "Event not found")
    return _event_json(event, include_prompt=True)


@router.get("/resources")
async def resources(request: Request) -> dict:
    """Everything this computer can do, including its physical peripherals."""
    context = get_context(request)
    settings = context.settings
    system_result = await context.service.tools.execute(Operation(tool="system", method="info"))
    return {
        "devices": _dashboard_devices(context),
        "tools": [
            definition.model_dump(mode="json")
            for definition in context.service.tools.definitions()
        ],
        "system": system_result.output if system_result.success else {"error": system_result.error},
        "hardware": hardware_report(
            settings.workspace_root, input_control=settings.enable_input_control
        ),
        # Perception is a capability, so it is reported like one: whether the
        # configured model can actually read image bytes, and how a capture
        # becomes a vision part. Claiming sight the model does not have is worse
        # than admitting the limit.
        "vision": {
            "model_reads_images": settings.deepseek_supports_vision,
            "model": settings.deepseek_model,
            "images_per_request": settings.vision_max_images if settings.deepseek_supports_vision else 0,
            "detail": settings.vision_detail or "provider default",
            "max_image_bytes": settings.attachment_max_bytes,
            "note": (
                "Attachments and fresh screen captures are sent as image parts."
                if settings.deepseek_supports_vision
                else "The configured model is treated as text-only: captures are "
                "geometry only (origin, scale) and the model is told not to describe "
                "what it cannot see. Enable ASSISTANT_DEEPSEEK_SUPPORTS_VISION only for "
                "a model that accepts images (deepseek-flash does; deepseek-v4-pro does not)."
            ),
        },
    }


@router.post("/resources/capture")
async def capture_screen(request: Request, monitor: int | None = None, max_width: int = 1600) -> dict:
    """Take a fresh screenshot so the dashboard can show what the machine sees.

    This runs the same ``screen.capture`` operation a worker would use, so what
    an operator sees here is exactly the evidence a task would have produced.
    """
    context = get_context(request)
    result = await context.service.tools.execute(
        Operation(
            tool="screen",
            method="capture",
            args={"monitor": monitor, "max_width": max(320, min(max_width, 3840))},
        )
    )
    if not result.success or not isinstance(result.output, dict):
        raise HTTPException(409, result.error or "screen capture is unavailable")
    image = Path(str(result.output.get("image", "")))
    return {
        **result.output,
        "filename": image.name,
    }


@router.get("/resources/screenshot/{filename}")
async def screen_screenshot(filename: str) -> FileResponse:
    """Serve a capture produced by ``screen.capture`` and nothing else."""
    if not SCREENSHOT_NAME.match(filename):
        raise HTTPException(404, "Not found")
    target = (SCREENSHOT_DIR / filename).resolve()
    root = SCREENSHOT_DIR.resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(target, media_type="image/png")


@router.get("/metrics")
async def metrics(request: Request, limit: int = 500) -> dict:
    """Aggregated LLM/runtime analytics computed from recent events."""
    context = get_context(request)
    repository = context.service.repository
    tasks = await repository.list_tasks()
    task_nodes = {task.id: await repository.list_nodes(task.id) for task in tasks}
    events = await repository.list_recent_events(limit=limit)
    return _dashboard_analytics(context, tasks, task_nodes, events)
