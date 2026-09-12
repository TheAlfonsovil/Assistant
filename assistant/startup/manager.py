from __future__ import annotations

import platform
import sys
from datetime import UTC, datetime

from httpx import HTTPError
from sqlalchemy.exc import SQLAlchemyError

from assistant import __version__
from assistant.domain.models import TaskStatus, UserProfile
from assistant.infrastructure.repositories import TaskRepository
from assistant.recovery import RecoveryManager
from assistant.startup.models import ReadinessStatus, StartupReport


class StartupManager:
    """Loads dependencies, durable context and recoverable work."""

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
            existing_memory = await repository.list_memory()
            report.first_initialization = not existing_memory
            report.loaded_memories = existing_memory
            report.user_profile = self._user_profile()
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

            if getattr(self.settings, "collect_system_facts", True):
                report.system_facts = self._system_facts()
                if getattr(self.settings, "persist_system_facts", True):
                    for key, value in report.system_facts.items():
                        await repository.upsert_memory(
                            kind="system", key=key, value=value, source="SYSTEM", confidence=1.0
                        )
                    report.loaded_memories = await repository.list_memory()
                    report.checks.append("Long-term memory loaded and system facts synchronized")
            else:
                report.checks.append("Long-term memory loaded; system facts disabled")

            if report.user_profile and getattr(self.settings, "persist_user_profile", True):
                await repository.upsert_memory(
                    kind="user_profile",
                    key="primary",
                    value=report.user_profile.model_dump(mode="json"),
                    source="USER_ENV",
                    confidence=1.0,
                )
                report.loaded_memories = await repository.list_memory()
                report.checks.append("User profile loaded from environment")

        report.status = ReadinessStatus.READY if report.llm_ready else ReadinessStatus.DEGRADED
        report.finished_at = datetime.now(UTC)
        return report

    @staticmethod
    def _system_facts() -> dict[str, str]:
        """Return stable technical facts; never inspect personal files or secrets."""
        return {
            "os": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
            "assistant_runtime": sys.implementation.name,
        }

    def _user_profile(self) -> UserProfile | None:
        name = getattr(self.settings, "user_name", None)
        if not name:
            return None
        split_values = lambda value: [item.strip() for item in value.split(",") if item.strip()]
        return UserProfile(
            name=name,
            birth_date=getattr(self.settings, "user_birth_date", None),
            profession=getattr(self.settings, "user_profession", None),
            degrees=split_values(getattr(self.settings, "user_degrees", "")),
            expertise=split_values(getattr(self.settings, "user_expertise", "")),
        )
