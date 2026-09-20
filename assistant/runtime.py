from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from .domain.models import TaskStatus
from .idle import IdleCycle

logger = logging.getLogger(__name__)


class TaskRuntime:
    """Keeps the persistent task manager alive and processes unfinished tasks."""

    def __init__(
        self,
        repository,
        execute_task: Callable[[str], Awaitable[object]],
        interval: float = 5.0,
        idle_cycle: IdleCycle | None = None,
        max_backoff: float = 60.0,
        is_ready: Callable[[], bool | Awaitable[bool]] | None = None,
    ):
        self.repository = repository
        self.execute_task = execute_task
        self.interval = interval
        self.max_backoff = max_backoff
        self.idle_cycle = idle_cycle or IdleCycle()
        self.is_ready = is_ready or (lambda: True)
        self.stop_requested = False
        self.last_started_at: datetime | None = None
        self.last_completed_at: datetime | None = None
        self.last_error: str | None = None
        self.last_ready: bool | None = None
        self.last_active_count = 0
        self.last_maintenance_at: datetime | None = None
        self.maintenance_runs = 0
        self.maintenance_errors = 0
        self.metrics = {
            "passes": 0,
            "tasks_dispatched": 0,
            "task_errors": 0,
            "idle_passes": 0,
            "idle_skipped": 0,
            "not_ready_passes": 0,
            "runtime_errors": 0,
        }
        self.worker_id = f"{socket.gethostname()}:{uuid4()}"
        self.started_at = datetime.now(UTC)

    def metrics_snapshot(self) -> dict[str, int]:
        return dict(self.metrics)

    def readiness_snapshot(self) -> bool | None:
        return self.last_ready

    def idle_snapshot(self) -> dict[str, object]:
        return {
            "enabled": self.idle_cycle.enabled,
            "interval_seconds": self.idle_cycle.interval,
            "supervision_interval_seconds": self.idle_cycle.supervision_interval,
            "last_supervision_result": self.idle_cycle.last_supervision_result,
            "reentrant_skips": self.idle_cycle.reentrant_skips,
            "last_maintenance_at": self.last_maintenance_at,
            "maintenance_runs": self.maintenance_runs,
            "maintenance_errors": self.maintenance_errors,
        }

    def set_idle_enabled(self, enabled: bool) -> None:
        self.idle_cycle.set_enabled(enabled)

    async def _run_idle(self, has_work: bool) -> None:
        if not self.idle_cycle.enabled:
            self.metrics["idle_skipped"] += 1
            return
        try:
            await self.idle_cycle.run_once(has_work=has_work)
            self.maintenance_runs += 1
            self.last_maintenance_at = datetime.now(UTC)
        except Exception:
            self.maintenance_errors += 1
            raise
        self.metrics["idle_passes"] += 1

    async def _persist_heartbeat(self) -> None:
        save = getattr(self.repository, "save_worker_heartbeat", None)
        if save is None:
            return
        await save(
            self.worker_id,
            self.started_at,
            datetime.now(UTC),
            self.last_started_at,
            self.last_completed_at,
            self.last_error,
            self.last_active_count,
        )

    async def run_once(self) -> int:
        self.metrics["passes"] += 1
        self.last_started_at = datetime.now(UTC)
        await self._persist_heartbeat()
        readiness = self.is_ready()
        if asyncio.iscoroutine(readiness) or isinstance(readiness, Awaitable):
            readiness = await cast(Awaitable[bool], readiness)
        self.last_ready = bool(readiness)
        if not self.last_ready:
            self.metrics["not_ready_passes"] += 1
            await self._run_idle(has_work=False)
            self.last_active_count = 0
            self.last_completed_at = datetime.now(UTC)
            await self._persist_heartbeat()
            return 0
        tasks = await self.repository.list_tasks()
        active = [
            task for task in tasks
            if task.status in {
                TaskStatus.QUEUED,
                TaskStatus.PLANNING,
                TaskStatus.READY,
                TaskStatus.RUNNING,
            }
        ]
        for task in sorted(active, key=lambda item: (-item.priority, item.created_at)):
            try:
                self.metrics["tasks_dispatched"] += 1
                await self.execute_task(task.id)
            except Exception:
                self.metrics["task_errors"] += 1
                logger.exception("Task worker iteration failed for %s", task.id)
                continue
        await self._run_idle(has_work=bool(active))
        self.last_active_count = len(active)
        self.last_completed_at = datetime.now(UTC)
        await self._persist_heartbeat()
        return len(active)

    async def run_forever(self) -> None:
        backoff = self.interval
        while not self.stop_requested:
            try:
                await self.run_once()
                self.last_error = None
                backoff = self.interval
            except Exception as error:
                self.metrics["runtime_errors"] += 1
                self.last_error = str(error)
                logger.exception("Task runtime pass failed")
                await self._persist_heartbeat()
                backoff = min(self.max_backoff, max(self.interval, backoff * 2))
            await asyncio.sleep(backoff)

    def stop(self) -> None:
        self.stop_requested = True
