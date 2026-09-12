from __future__ import annotations

import asyncio
import logging
import socket
from uuid import uuid4
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

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
        is_ready: Callable[[], bool] | None = None,
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
        self.last_active_count = 0
        self.worker_id = f"{socket.gethostname()}:{uuid4()}"
        self.started_at = datetime.now(UTC)

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
        self.last_started_at = datetime.now(UTC)
        await self._persist_heartbeat()
        if not self.is_ready():
            await self.idle_cycle.run_once(has_work=False)
            self.last_active_count = 0
            self.last_completed_at = datetime.now(UTC)
            await self._persist_heartbeat()
            return 0
        tasks = await self.repository.list_tasks()
        active = [
            task for task in tasks
            if task.status in {TaskStatus.QUEUED, TaskStatus.READY, TaskStatus.RUNNING}
        ]
        for task in sorted(active, key=lambda item: (-item.priority, item.created_at)):
            try:
                await self.execute_task(task.id)
            except Exception:
                logger.exception("Task worker iteration failed for %s", task.id)
                continue
        if not active:
            await self.idle_cycle.run_once(has_work=False)
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
                self.last_error = str(error)
                logger.exception("Task runtime pass failed")
                await self._persist_heartbeat()
                backoff = min(self.max_backoff, max(self.interval, backoff * 2))
            await asyncio.sleep(backoff)

    def stop(self) -> None:
        self.stop_requested = True
