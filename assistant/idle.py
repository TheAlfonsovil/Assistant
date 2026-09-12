import asyncio
from collections.abc import Awaitable, Callable


class IdleCycle:
    def __init__(self, create_task: Callable[[str], Awaitable[object]], interval: float = 30.0):
        self.create_task = create_task
        self.interval = interval

    async def run_once(self, has_work: bool = False) -> object | None:
        if has_work:
            return None
        return await self.create_task("Assistant maintenance: check persisted task health")

    async def run_forever(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.interval)
