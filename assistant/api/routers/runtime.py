from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from ...config import get_settings
from ...domain.models import IdleConfigurationRequest
from ...startup.manager import seed_durable_memory
from ..deps import get_context, get_runtime

router = APIRouter(prefix="/runtime", tags=["runtime"])


async def perform_reset(request: Request) -> dict:
    runtime = get_runtime(request)
    worker = request.app.state.worker
    runtime.stop()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass
    context = get_context(request)
    deleted = await context.service.repository.reset_state()
    # A reset clears learned state, not the assistant's own identity. Without
    # re-seeding, the host facts and user profile stay deleted until the next
    # restart, and workers lose the only reliable record of the host
    # interpreter, which is how a Windows host ends up with a POSIX script.
    reseeded = await seed_durable_memory(context.service.repository, get_settings())
    runtime.stop_requested = False
    request.app.state.worker = asyncio.create_task(
        runtime.run_forever(), name="assistant-task-runtime"
    )
    return {
        "reset": True,
        "deleted": deleted,
        "total": sum(deleted.values()),
        "reseeded": reseeded["system_facts"] and reseeded["checks"],
    }


@router.post("/reset")
async def reset_runtime(request: Request) -> dict:
    return await perform_reset(request)


@router.get("/idle")
async def get_idle(request: Request) -> dict:
    return get_runtime(request).idle_snapshot()


@router.put("/idle")
async def configure_idle(request: Request, configuration: IdleConfigurationRequest) -> dict:
    runtime = get_runtime(request)
    runtime.set_idle_enabled(configuration.enabled)
    return runtime.idle_snapshot()


@router.get("/offpeak")
async def get_offpeak(request: Request) -> dict:
    return get_runtime(request).offpeak_snapshot()


@router.put("/offpeak")
async def configure_offpeak(
    request: Request, configuration: IdleConfigurationRequest
) -> dict:
    """Toggle "ahorro de consumo": hold the queue during DeepSeek peak hours."""
    runtime = get_runtime(request)
    runtime.set_offpeak_enabled(configuration.enabled)
    return runtime.offpeak_snapshot()
