"""Recurring work as a tool: what turns a worker into a scheduled one.

A schedule is a **holder task** that carries ``metadata["schedule"]`` and stays
``WAITING``; the runtime clones it when it is due. Nothing else has to change:
the clone is an ordinary task, so budgets, leases, retries, artifacts and the
ledger all apply exactly as before.

Intervals are bounded (one minute to thirty days) so a schedule cannot become a
self-inflicted request loop. A schedule is either a fixed interval or a
wall-clock time of day in a named timezone; bounds and timezone handling live in
``assistant.recurrence`` so the tool, the API and the scheduler cannot drift.
"""

from __future__ import annotations

from typing import Any

from assistant.domain.models import ErrorType, OperationResult
from assistant.recurrence import (
    MAX_EVERY_SECONDS,
    MIN_EVERY_SECONDS,
    ScheduleError,
)
from assistant.tools import Tool, ToolDefinition

__all__ = ["MAX_EVERY_SECONDS", "MIN_EVERY_SECONDS", "ScheduleTool"]


class ScheduleTool(Tool):
    definition = ToolDefinition(
        name="schedule",
        description=(
            "Recurring work. 'every' repeats after N seconds, 'daily' runs at a "
            "wall-clock time in a named timezone, 'list' shows the schedules and "
            "'cancel' stops one. Use it for monitoring, periodic audits or any "
            "repeated check instead of staying in a loop."
        ),
        methods=["every", "daily", "list", "cancel"],
        argument_schema={
            "goal": {"type": "string"},
            "every_seconds": {"type": "integer"},
            "at_hour": {"type": "integer"},
            "at_minute": {"type": "integer"},
            "timezone": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "task_id": {"type": "string"},
        },
        method_argument_schema={
            "every": {
                "goal": {"type": "string", "required": True},
                "every_seconds": {"type": "integer", "required": True},
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
            "daily": {
                "goal": {"type": "string", "required": True},
                "at_hour": {"type": "integer", "required": True},
                "at_minute": {"type": "integer"},
                "timezone": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
            "list": {},
            "cancel": {"task_id": {"type": "string", "required": True}},
        },
        permissions=["schedule.read", "schedule.write"],
        # Creating a schedule twice would duplicate recurring work.
        idempotent=False,
    )

    def __init__(self, service):
        self.service = service

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        try:
            if method == "every":
                return await self._create(args, daily=False)
            if method == "daily":
                return await self._create(args, daily=True)
            if method == "list":
                return await self._list()
            if method == "cancel":
                return await self._cancel(args)
            return OperationResult(
                success=False,
                error=f"Unsupported schedule method: {method}",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        except ScheduleError as error:
            return OperationResult(
                success=False, error=str(error), error_type=ErrorType.INVALID_ARGUMENT
            )
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            return OperationResult(
                success=False,
                error=f"schedule.{method} failed: {error}",
                error_type=ErrorType.TOOL_FAILURE,
            )

    async def _create(self, args: dict[str, Any], *, daily: bool) -> OperationResult:
        goal = str(args.get("goal") or "").strip()
        if not goal:
            return OperationResult(
                success=False,
                error="goal must not be empty",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        parent = None
        parent_id = args.get("_task_id")
        if parent_id:
            parent = await self.service.get_task(str(parent_id))
        created = await self.service.create_schedule(
            goal,
            every_seconds=None if daily else args.get("every_seconds"),
            at_hour=args.get("at_hour") if daily else None,
            at_minute=args.get("at_minute") if daily else None,
            timezone_name=args.get("timezone"),
            title=str(args.get("title") or "").strip(),
            description=str(args.get("description") or "").strip(),
            project_id=parent.project_id if parent else None,
            parent_task_id=parent.id if parent else None,
            # A recurring review of the same evidence must keep it.
            attachments=parent.attachments if parent else None,
        )
        return OperationResult(
            success=True,
            output={
                "schedule": self.service.schedule_view(created),
                "note": (
                    "Holder created in WAITING; the runtime clones it as a child "
                    "task on every due window."
                ),
            },
            side_effects=["schedule.created"],
        )

    async def _list(self) -> OperationResult:
        schedules = await self.service.list_schedules()
        return OperationResult(
            success=True, output={"count": len(schedules), "schedules": schedules}
        )

    async def _cancel(self, args: dict[str, Any]) -> OperationResult:
        task_id = str(args.get("task_id") or "").strip()
        task = await self.service.cancel_schedule(task_id)
        if task is None:
            return OperationResult(
                success=False,
                error=f"no schedule found for task {task_id}",
                error_type=ErrorType.NOT_FOUND,
            )
        return OperationResult(
            success=True,
            output={"schedule": self.service.schedule_view(task)},
            side_effects=["schedule.cancelled"],
        )
