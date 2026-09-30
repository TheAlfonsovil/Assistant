from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ...domain.models import Operation
from ..deps import get_context, get_runtime
from ..serializers import (
    _dashboard_analytics,
    _dashboard_devices,
    _event_json,
    _metrics_series,
)

router = APIRouter(tags=["dashboard"])


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
    context = get_context(request)
    system_result = await context.service.tools.execute(Operation(tool="system", method="info"))
    return {
        "devices": _dashboard_devices(context),
        "tools": [
            definition.model_dump(mode="json")
            for definition in context.service.tools.definitions()
        ],
        "system": system_result.output if system_result.success else {"error": system_result.error},
    }


@router.get("/metrics")
async def metrics(request: Request, limit: int = 500) -> dict:
    """Aggregated LLM/runtime analytics computed from recent events."""
    context = get_context(request)
    repository = context.service.repository
    tasks = await repository.list_tasks()
    task_nodes = {task.id: await repository.list_nodes(task.id) for task in tasks}
    events = await repository.list_recent_events(limit=limit)
    return _dashboard_analytics(context, tasks, task_nodes, events)
