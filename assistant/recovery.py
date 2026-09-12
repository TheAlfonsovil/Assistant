from datetime import UTC, datetime

from sqlalchemy import delete, select

from .domain.models import NodeStatus
from .infrastructure.orm import LeaseRow, NodeRow


class RecoveryManager:
    def __init__(self, session):
        self.session = session

    async def recover(self) -> int:
        now = datetime.now(UTC)
        await self.session.execute(delete(LeaseRow).where(LeaseRow.expires_at <= now))
        result = await self.session.execute(
            select(NodeRow).where(NodeRow.status == NodeStatus.RUNNING.value)
        )
        recovered = 0
        for row in result.scalars():
            row.status = NodeStatus.READY.value
            row.error = "recovered after process restart"
            recovered += 1
        if recovered:
            await self.session.commit()
        return recovered
