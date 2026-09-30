from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
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
        readiness_ttl: float = 0.0,
        max_concurrent: int = 1,
        offpeak=None,
    ):
        self.repository = repository
        self.execute_task = execute_task
        self.interval = interval
        self.max_backoff = max_backoff
        self.idle_cycle = idle_cycle or IdleCycle()
        self.is_ready = is_ready or (lambda: True)
        # 1 keeps the original strictly sequential behaviour. Raising it lets
        # independent tasks advance in parallel; node leases and the serialized
        # write path keep that safe.
        self.max_concurrent = max(1, int(max_concurrent))
        # A 24/7 loop must not issue a readiness request on every pass. The
        # previous value (including ``None`` before the first check) is reused
        # until the TTL expires; 0 keeps the original per-pass behaviour.
        self.readiness_ttl = max(0.0, readiness_ttl)
        self._readiness_checked_at: float | None = None
        # "Ahorro de consumo": stop starting paid work inside peak windows.
        self.offpeak = offpeak
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
            "offpeak_passes": 0,
        }
        self.worker_id = f"{socket.gethostname()}:{uuid4()}"
        self.started_at = datetime.now(UTC)
        self._wake_event = asyncio.Event()

    def metrics_snapshot(self) -> dict[str, int]:
        return dict(self.metrics)

    def readiness_snapshot(self) -> bool | None:
        return self.last_ready

    def set_offpeak_enabled(self, enabled: bool) -> None:
        if self.offpeak is None:
            from .offpeak import OffPeakPolicy

            self.offpeak = OffPeakPolicy(enabled=enabled)
            return
        self.offpeak.set_enabled(enabled)

    def offpeak_snapshot(self) -> dict[str, object]:
        """Savings-mode state, or a disabled default when not configured."""
        if self.offpeak is None:
            return {"enabled": False, "state": "RUNNING", "configured": False}
        snapshot = dict(self.offpeak.snapshot())
        snapshot["configured"] = True
        return snapshot

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

    async def _release_scoped_session(self) -> None:
        """Drop the session this asyncio task created, if the repository has one.

        Sessions are scoped per asyncio task, so a task spawned by the
        concurrent dispatcher must release its own or the connection survives
        until garbage collection.
        """
        remove = getattr(getattr(self.repository, "session", None), "remove", None)
        if remove is None:
            return
        try:
            await remove()
        except Exception:  # pragma: no cover - cleanup must never break a pass
            logger.debug("Releasing the scoped session failed", exc_info=True)

    async def _dispatch(self, task_id: str) -> None:
        """Advance one task by one step, counting failures instead of aborting."""
        try:
            self.metrics["tasks_dispatched"] += 1
            await self.execute_task(task_id)
        except Exception:
            self.metrics["task_errors"] += 1
            logger.exception("Task worker iteration failed for %s", task_id)

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

    def _readiness_is_stale(self) -> bool:
        if self.readiness_ttl <= 0 or self._readiness_checked_at is None:
            return True
        return (monotonic() - self._readiness_checked_at) >= self.readiness_ttl

    async def _resolve_readiness(self) -> bool | None:
        """Return the cached readiness value until its TTL expires."""
        if not self._readiness_is_stale():
            return self.last_ready
        readiness = self.is_ready()
        if asyncio.iscoroutine(readiness) or isinstance(readiness, Awaitable):
            readiness = await cast(Awaitable[bool], readiness)
        self._readiness_checked_at = monotonic()
        return bool(readiness)

    async def run_once(self) -> int:
        self.metrics["passes"] += 1
        self.last_started_at = datetime.now(UTC)
        await self._persist_heartbeat()
        readiness = await self._resolve_readiness()
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
                TaskStatus.VERIFYING,
                TaskStatus.FINALIZING,
            }
        ]
        ordered = sorted(active, key=lambda item: (-item.priority, item.created_at))
        if self.offpeak is not None and self.offpeak.is_paused():
            # Peak pricing: hold the queue instead of spending on every step.
            # Nothing is cancelled or failed; work resumes at the next window.
            self.metrics["offpeak_passes"] += 1
            await self._run_idle(has_work=bool(ordered))
            self.last_active_count = len(ordered)
            self.last_completed_at = datetime.now(UTC)
            await self._persist_heartbeat()
            return len(ordered)
        if self.max_concurrent <= 1:
            for task in ordered:
                await self._dispatch(task.id)
        else:
            # Independent tasks advance in parallel; the semaphore bounds how
            # many run at once. ``_dispatch`` swallows per-task failures, so one
            # bad task cannot cancel the whole pass.
            semaphore = asyncio.Semaphore(self.max_concurrent)

            async def guarded(task_id: str) -> None:
                try:
                    async with semaphore:
                        await self._dispatch(task_id)
                finally:
                    await self._release_scoped_session()

            await asyncio.gather(*(guarded(task.id) for task in ordered))
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
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=backoff)
                self._wake_event.clear()
            except TimeoutError:
                pass

    def stop(self) -> None:
        self.stop_requested = True
        self._wake_event.set()

    def wake(self) -> None:
        self._wake_event.set()
