from __future__ import annotations

from datetime import UTC, datetime

from httpx import HTTPError
from sqlalchemy.exc import SQLAlchemyError

from assistant import __version__
from assistant.domain.models import TaskStatus
from assistant.infrastructure.repositories import TaskRepository
from assistant.recovery import RecoveryManager
from assistant.startup.models import ReadinessStatus, StartupReport


class StartupManager:
    """Initializes dependencies, checks readiness and recovers persisted work."""

    def __init__(self, database, settings, llm):
        self.database = database
        self.settings = settings
        self.llm = llm

    async def initialize(self) -> StartupReport:
        report = StartupReport(
            status=ReadinessStatus.FAILED,
            assistant_version=__version__,
            llm_model=self.settings.ollama_model,
        )
        try:
            await self.database.create_all()
            report.database_ready = True
            report.checks.append("SQLite schema ready")
        except (OSError, SQLAlchemyError) as error:
            report.errors.append(f"database: {error}")
            report.finished_at = datetime.now(UTC)
            return report

        try:
            report.llm_ready = await self.llm.check_ready()
            report.checks.append("Ollama and configured model ready" if report.llm_ready else "Ollama not ready")
        except (HTTPError, OSError, ValueError) as error:
            report.errors.append(f"llm: {error}")

        async with self.database.sessions() as session:
            report.recovered_nodes = await RecoveryManager(session).recover()
            repository = TaskRepository(session)
            unfinished = await repository.list_tasks()
            report.unfinished_tasks = sum(
                task.status in {
                    TaskStatus.QUEUED,
                    TaskStatus.PLANNING,
                    TaskStatus.READY,
                    TaskStatus.RUNNING,
                    TaskStatus.WAITING,
                    TaskStatus.BLOCKED,
                }
                for task in unfinished
            )

        report.status = ReadinessStatus.READY if report.llm_ready else ReadinessStatus.DEGRADED
        report.finished_at = datetime.now(UTC)
        return report
