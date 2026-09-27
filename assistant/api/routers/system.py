from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request

from ..deps import get_context, get_runtime

router = APIRouter(tags=["system"])


@router.get("/health")
async def health(request: Request) -> dict:
    context = get_context(request)
    startup = context.startup
    runtime = get_runtime(request)
    database_health = await context.database.health_check()
    status = startup.status
    if database_health.get("status") != "ok" or runtime.last_error:
        status = "degraded"
    if (
        runtime.last_completed_at is not None
        and (datetime.now(UTC) - runtime.last_completed_at).total_seconds()
        > max(60.0, context.settings.maintenance_interval * 3)
    ):
        status = "degraded"
    return {
        "status": status,
        "llm_ready": startup.llm_ready,
        "first_initialization": startup.first_initialization,
        "loaded_memories": len(startup.loaded_memories),
        "unfinished_tasks": startup.unfinished_tasks,
        "runtime": {
            "active_tasks": runtime.last_active_count,
            "last_started_at": runtime.last_started_at,
            "last_completed_at": runtime.last_completed_at,
            "last_error": runtime.last_error,
            "metrics": runtime.metrics_snapshot(),
            "idle": runtime.idle_snapshot(),
        },
        "database": database_health,
    }
