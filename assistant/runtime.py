from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .domain.models import TaskStatus


class TaskRuntime:
    """Keeps the persistent task manager alive and processes unfinished tasks."""

    def __init__(self, repository, execute_task: Callable[[str], Awaitable[object]], interval: float = 5.0):
        self.repository = repository
        self.execute_task = execute_task
        self.interval = interval
        self.stop_requested = False

    async def run_once(self) -> int:
        tasks = await self.repository.list_tasks()
        active = [
            task for task in tasks
            if task.status in {TaskStatus.QUEUED, TaskStatus.READY, TaskStatus.RUNNING}
        ]
        for task in sorted(active, key=lambda item: (-item.priority, item.created_at)):
            await self.execute_task(task.id)
        return len(active)

    async def run_forever(self) -> None:
        while not self.stop_requested:
            await self.run_once()
            await asyncio.sleep(self.interval)

    def stop(self) -> None:
        self.stop_requested = True
