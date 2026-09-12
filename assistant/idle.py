import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic


class IdleCycle:
    def __init__(
        self,
        create_task: Callable[[str], Awaitable[object]] | None = None,
        interval: float = 30.0,
        on_idle: Callable[[], Awaitable[object]] | None = None,
    ):
        self.create_task = create_task
        self.interval = interval
        self.on_idle = on_idle
        self.stop_requested = False
        self._last_idle_at: float | None = None
        self._idle_running = False

    async def run_once(self, has_work: bool = False) -> object | None:
        now = monotonic()
        if has_work or self.stop_requested or self._idle_running:
            return None
        if self._last_idle_at is not None and now - self._last_idle_at < self.interval:
            return None
        self._last_idle_at = now
        self._idle_running = True
        try:
            if self.on_idle:
                return await self.on_idle()
            if self.create_task:
                return await self.create_task("Assistant maintenance: check persisted task health")
            return None
        finally:
            self._idle_running = False

    async def run_forever(self) -> None:
        while not self.stop_requested:
            await self.run_once()
            await asyncio.sleep(self.interval)

    def stop(self) -> None:
        self.stop_requested = True
