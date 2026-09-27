from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from ...domain.models import IdleConfigurationRequest
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
    deleted = await get_context(request).service.repository.reset_state()
    runtime.stop_requested = False
    request.app.state.worker = asyncio.create_task(
        runtime.run_forever(), name="assistant-task-runtime"
    )
    return {"reset": True, "deleted": deleted, "total": sum(deleted.values())}


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
