from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ReadinessStatus(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class StartupReport(BaseModel):
    status: ReadinessStatus
    assistant_version: str
    database_ready: bool = False
    llm_ready: bool = False
    llm_model: str | None = None
    recovered_nodes: int = 0
    unfinished_tasks: int = 0
    checks: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    @property
    def ready(self) -> bool:
        return self.status in {ReadinessStatus.READY, ReadinessStatus.DEGRADED}
