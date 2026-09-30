"""Serialized write path for the SQLite-backed session factory.

Concurrent task execution must not turn a short write burst into
``database is locked``. SQLite already runs in WAL mode with a 30s busy
timeout, but serializing commits in-process keeps writes deterministic and
avoids busy-retry storms. It costs nothing while ``max_concurrent_tasks`` is
1, which is the default.

Reads are never serialized: only the commit itself takes the lock, so the LLM
call and tool execution of other tasks keep running in parallel.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

# Keyed by event-loop identity: a lock created for one loop cannot be awaited
# from another. Loop ids are reused after collection, so this stays small.
_locks: dict[int, asyncio.Lock] = {}


def write_lock() -> asyncio.Lock:
    """Return the process-wide write lock for the running event loop."""
    loop = asyncio.get_running_loop()
    lock = _locks.get(id(loop))
    if lock is None:
        lock = asyncio.Lock()
        _locks[id(loop)] = lock
    return lock


class SerializedWriteSession(AsyncSession):
    """An ``AsyncSession`` whose ``commit()`` is serialized across the process."""

    async def commit(self) -> None:
        async with write_lock():
            await super().commit()
