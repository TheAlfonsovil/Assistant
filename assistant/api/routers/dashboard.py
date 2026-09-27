from __future__ import annotations

from fastapi import APIRouter, Request

from ...domain.models import Operation
from ..deps import get_context, get_runtime
from ..serializers import (
    _dashboard_analytics,
    _dashboard_devices,
    _event_json,
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
        },
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
        },
    }


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
