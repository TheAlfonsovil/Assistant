import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic


class IdleCycle:
    def __init__(
        self,
        create_task: Callable[[str], Awaitable[object]] | None = None,
        interval: float = 30.0,
        on_idle: Callable[[], Awaitable[object]] | None = None,
        supervise: Callable[[bool], Awaitable[object]] | None = None,
        supervision_interval: float | None = None,
        enabled: bool = True,
    ):
        self.create_task = create_task
        self.interval = interval
        self.on_idle = on_idle
        self.supervise = supervise
        self.supervision_interval = supervision_interval if supervision_interval is not None else interval
        self.enabled = enabled
        self.stop_requested = False
        self._last_idle_at: float | None = None
        self._last_supervision_at: float | None = None
        self._idle_running = False
        self.last_supervision_result: object | None = None
        self.reentrant_skips = 0

    async def run_once(self, has_work: bool = False) -> object | None:
        now = monotonic()
        if self.stop_requested or not self.enabled:
            return None
        if self._idle_running:
            self.reentrant_skips += 1
            return None
        self._idle_running = True
        try:
            result = None
            if (
                self.supervise
                and (
                    self._last_supervision_at is None
                    or now - self._last_supervision_at >= self.supervision_interval
                )
            ):
                self._last_supervision_at = now
                result = await self.supervise(has_work)
                self.last_supervision_result = result
            if has_work:
                return result
        finally:
            self._idle_running = False
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

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
