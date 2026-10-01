"""Recurring work over HTTP.

The holder tasks are ordinary tasks, so the same objects are visible under
``/tasks``; this router exists so a client can manage recurrence without having
to know that a schedule is stored in task metadata.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ...domain.models import ScheduleRequest, ScheduleUpdateRequest
from ...recurrence import COMMON_TIMEZONES, ScheduleError, timezone_database_available
from ..deps import get_context, get_runtime

router = APIRouter(prefix="/schedules", tags=["schedules"])


def _overview(request: Request, schedules: list[dict]) -> dict:
    # The settings of the running context, so the answer always describes the
    # configuration actually in use (and not a second read of the environment).
    settings = get_context(request).settings
    return {
        "schedules": schedules,
        "count": len(schedules),
        "enabled": len([item for item in schedules if item["enabled"]]),
        "default_timezone": settings.schedule_timezone,
        "common_timezones": COMMON_TIMEZONES,
        # Windows has no system timezone database: the dashboard warns instead
        # of failing a daily schedule with a confusing error.
        "timezone_database": timezone_database_available(),
    }


@router.get("")
async def list_schedules(request: Request) -> dict:
    schedules = await get_context(request).service.list_schedules()
    return _overview(request, schedules)


@router.post("")
async def create_schedule(request: Request, payload: ScheduleRequest) -> dict:
    service = get_context(request).service
    try:
        task = await service.create_schedule(
            payload.goal,
            every_seconds=payload.every_seconds,
            at_hour=payload.at_hour,
            at_minute=payload.at_minute,
            timezone_name=payload.timezone,
            title=payload.title,
            description=payload.description,
            project_id=payload.project_id,
        )
    except ScheduleError as error:
        raise HTTPException(422, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    get_runtime(request).wake()
    return service.schedule_view(task) or {}


@router.put("/{task_id}")
async def update_schedule(
    request: Request, task_id: str, payload: ScheduleUpdateRequest
) -> dict:
    service = get_context(request).service
    try:
        task = await service.update_schedule(
            task_id,
            every_seconds=payload.every_seconds,
            at_hour=payload.at_hour,
            at_minute=payload.at_minute,
            timezone_name=payload.timezone,
        )
    except ScheduleError as error:
        raise HTTPException(422, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if task is None:
        raise HTTPException(404, "Schedule not found")
    get_runtime(request).wake()
    return service.schedule_view(task) or {}


@router.post("/{task_id}/cancel")
async def cancel_schedule(request: Request, task_id: str) -> dict:
    service = get_context(request).service
    task = await service.cancel_schedule(task_id)
    if task is None:
        raise HTTPException(404, "Schedule not found")
    return service.schedule_view(task) or {}


@router.post("/{task_id}/pause")
async def pause_schedule(request: Request, task_id: str) -> dict:
    service = get_context(request).service
    try:
        task = await service.pause_schedule(task_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if task is None:
        raise HTTPException(404, "Schedule not found")
    return service.schedule_view(task) or {}


@router.post("/{task_id}/resume")
async def resume_schedule(request: Request, task_id: str) -> dict:
    service = get_context(request).service
    try:
        task = await service.resume_schedule(task_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except ScheduleError as error:
        raise HTTPException(422, str(error)) from error
    if task is None:
        raise HTTPException(404, "Schedule not found")
    get_runtime(request).wake()
    return service.schedule_view(task) or {}
