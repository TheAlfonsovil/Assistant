from datetime import UTC, datetime

from sqlalchemy import delete, select

from .domain.models import NodeStatus, TaskStatus
from .infrastructure.orm import LeaseRow, NodeRow, TaskRow


class RecoveryManager:
    def __init__(self, session):
        self.session = session

    async def recover(self) -> int:
        now = datetime.now(UTC)
        expired_leases = await self.session.execute(
            delete(LeaseRow).where(LeaseRow.expires_at <= now)
        )
        result = await self.session.execute(
            select(NodeRow).where(
                NodeRow.status.in_([NodeStatus.RUNNING.value, NodeStatus.VERIFYING.value])
            )
        )
        recovered = 0
        recovered_task_ids = set()
        for row in result.scalars():
            row.status = NodeStatus.READY.value
            row.error = "recovered after process restart"
            row.finished_at = None
            recovered_task_ids.add(row.task_id)
            recovered += 1
        if recovered_task_ids:
            tasks = await self.session.execute(
                select(TaskRow).where(TaskRow.id.in_(recovered_task_ids))
            )
            for row in tasks.scalars():
                if row.status in {TaskStatus.RUNNING.value, TaskStatus.VERIFYING.value}:
                    row.status = TaskStatus.READY.value
                    row.finished_at = None
                    row.failure_reason = "node recovered after process restart"
        planning = await self.session.execute(
            select(TaskRow).where(TaskRow.status == TaskStatus.PLANNING.value)
        )
        for row in planning.scalars():
            row.status = TaskStatus.QUEUED.value
            row.failure_reason = "planning recovered after process restart"
            recovered += 1
        if recovered or expired_leases.rowcount:
            await self.session.commit()
        return recovered
