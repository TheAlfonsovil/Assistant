"""Recurring work as a tool: what turns a worker into a scheduled one.

A schedule is a **holder task** that carries ``metadata["schedule"]`` and stays
``WAITING``; the runtime clones it when it is due. Nothing else has to change:
the clone is an ordinary task, so budgets, leases, retries, artifacts and the
ledger all apply exactly as before.

Intervals are bounded (one minute to thirty days) so a schedule cannot become a
self-inflicted request loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from assistant.domain.models import (
    ErrorType,
    OperationResult,
    Task,
)
from assistant.tools import Tool, ToolDefinition

MIN_EVERY_SECONDS = 60
MAX_EVERY_SECONDS = 30 * 24 * 3600


class ScheduleTool(Tool):
    definition = ToolDefinition(
        name="schedule",
        description=(
            "Recurring work. 'every' creates a task that runs again every N "
            "seconds, 'list' shows the schedules, 'cancel' stops one. Use it for "
            "monitoring, periodic audits or any repeated check."
        ),
        methods=["every", "list", "cancel"],
        argument_schema={
            "goal": {"type": "string"},
            "every_seconds": {"type": "integer"},
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
            "list": {},
            "cancel": {"task_id": {"type": "string", "required": True}},
        },
        permissions=["schedule.read", "schedule.write"],
        # Creating a schedule twice would duplicate recurring work.
        idempotent=False,
    )

    def __init__(self, service):
        self.service = service

    @staticmethod
    def _serialize(task: Task) -> dict[str, Any]:
        schedule = task.metadata.get("schedule") or {}
        return {
            "task_id": task.id,
            "title": task.title,
            "goal": task.goal,
            "status": task.status.value,
            "enabled": bool(schedule.get("enabled", True)),
            "every_seconds": schedule.get("every_seconds"),
            "next_run_at": schedule.get("next_run_at"),
            "last_fired_at": schedule.get("last_fired_at"),
            "runs": int(schedule.get("runs") or 0),
            "project_id": task.project_id,
        }

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        try:
            if method == "every":
                return await self._create(args)
            if method == "list":
                return await self._list()
            if method == "cancel":
                return await self._cancel(args)
            return OperationResult(
                success=False,
                error=f"Unsupported schedule method: {method}",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            return OperationResult(
                success=False,
                error=f"schedule.{method} failed: {error}",
                error_type=ErrorType.TOOL_FAILURE,
            )

    async def _create(self, args: dict[str, Any]) -> OperationResult:
        goal = str(args["goal"]).strip()
        if not goal:
            return OperationResult(
                success=False,
                error="goal must not be empty",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        try:
            every = int(args["every_seconds"])
        except (TypeError, ValueError):
            return OperationResult(
                success=False,
                error="every_seconds must be an integer",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        if not MIN_EVERY_SECONDS <= every <= MAX_EVERY_SECONDS:
            return OperationResult(
                success=False,
                error=(
                    f"every_seconds must be between {MIN_EVERY_SECONDS} and "
                    f"{MAX_EVERY_SECONDS}"
                ),
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        parent = None
        parent_id = args.get("_task_id")
        if parent_id:
            parent = await self.service.get_task(str(parent_id))
        created = await self.service.create_schedule(
            goal,
            every,
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
                "schedule": self._serialize(created),
                "note": (
                    "Holder created in WAITING; the runtime clones it as a child "
                    "task on every due window."
                ),
            },
            side_effects=["schedule.created"],
        )

    async def _list(self) -> OperationResult:
        schedules = [
            self._serialize(task)
            for task in await self.service.repository.list_tasks()
            if isinstance(task.metadata.get("schedule"), dict)
        ]
        schedules.sort(key=lambda item: str(item.get("next_run_at") or ""))
        return OperationResult(success=True, output={"count": len(schedules), "schedules": schedules})

    async def _cancel(self, args: dict[str, Any]) -> OperationResult:
        task_id = str(args["task_id"]).strip()
        task = await self.service.cancel_schedule(task_id)
        if task is None:
            return OperationResult(
                success=False,
                error=f"no schedule found for task {task_id}",
                error_type=ErrorType.NOT_FOUND,
            )
        return OperationResult(
            success=True,
            output={"schedule": self._serialize(task)},
            side_effects=["schedule.cancelled"],
        )
