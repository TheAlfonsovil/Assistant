from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from httpx import HTTPError
from sqlalchemy.exc import SQLAlchemyError

from assistant import __version__, host
from assistant.domain.models import Project, TaskStatus, UserProfile
from assistant.infrastructure.repositories import TaskRepository
from assistant.recovery import RecoveryManager
from assistant.startup.models import ReadinessStatus, StartupReport


async def seed_durable_memory(repository, settings) -> dict:
    """Persist the host identity and user profile into long-term memory.

    Both are derived from configuration, not learned from use, so they are
    re-seeded idempotently: an existing row is updated in place, never
    duplicated. This is also called after a runtime reset, because a reset
    clears learned state but must not leave the assistant without a record of
    which machine it is running on. Without that record, workers emitting a
    launch script have no reliable source for the host interpreter and fall
    back to a POSIX script on Windows.
    """
    checks: list[str] = []
    system_facts: dict[str, str] = {}
    if getattr(settings, "collect_system_facts", True):
        system_facts = host.system_facts()
        if getattr(settings, "persist_system_facts", True):
            for key, value in system_facts.items():
                await repository.upsert_memory(
                    kind="system", key=key, value=value, source="SYSTEM", confidence=1.0
                )
            checks.append("Long-term memory loaded and system facts synchronized")
        else:
            checks.append("System facts collected but not persisted")
    else:
        checks.append("Long-term memory loaded; system facts disabled")

    profile = StartupManager._user_profile_for(settings)
    if profile and getattr(settings, "persist_user_profile", True):
        await repository.upsert_memory(
            kind="user_profile",
            key="primary",
            value=profile.model_dump(mode="json"),
            source="USER_ENV",
            confidence=1.0,
        )
        checks.append("User profile loaded from environment")
    return {"system_facts": system_facts, "checks": checks}


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
            await self._ensure_builtin_projects(repository)
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
                    TaskStatus.FINALIZING,
                }
                for task in unfinished
            )

            seeded = await seed_durable_memory(repository, self.settings)
            report.system_facts = seeded["system_facts"]
            report.loaded_memories = await repository.list_memory()
            report.checks.extend(seeded["checks"])

        report.status = ReadinessStatus.READY if report.llm_ready else ReadinessStatus.DEGRADED
        report.finished_at = datetime.now(UTC)
        return report

    async def _ensure_builtin_projects(self, repository: TaskRepository) -> None:
        assistant_path = Path(__file__).resolve().parents[2]
        definition = Project(
            name="Assistant",
            path=str(assistant_path),
            description="Repositorio y código fuente del Assistant.",
            project_type="code",
            audit_prompt="Audita el repositorio Assistant y reporta hallazgos con evidencia.",
            enabled=True,
        )
        projects = await repository.list_projects()
        existing_paths = {str(Path(project.path).expanduser().resolve()): project for project in projects}
        if str(assistant_path.resolve()) not in existing_paths:
            await repository.create_project(definition)
        for project in projects:
            if (
                project.name == "Ordenador"
                and project.project_type == "computer"
                and project.description == "Entorno local del usuario y sus recursos de ordenador."
                and Path(project.path).resolve() == Path.home().resolve()
            ):
                await repository.delete_project(project.id)
        projects = await repository.list_projects()
        if not any(project.is_default and project.enabled for project in projects):
            assistant = next(
                (project for project in projects if Path(project.path).resolve() == assistant_path.resolve()),
                None,
            )
            if assistant is not None:
                assistant.is_default = True
                await repository.update_project(assistant)

    @staticmethod
    def _default_shell() -> tuple[str, str, str]:
        """Backwards-compatible alias for the shared host shell detection."""
        return host.default_shell()

    @classmethod
    def _system_facts(cls) -> dict[str, str]:
        """Backwards-compatible alias for the shared host fact collection."""
        return host.system_facts()

    @staticmethod
    def _user_profile_for(settings) -> UserProfile | None:
        name = getattr(settings, "user_name", None)
        if not name:
            return None
        split_values = lambda value: [item.strip() for item in value.split(",") if item.strip()]
        return UserProfile(
            name=name,
            birth_date=getattr(settings, "user_birth_date", None),
            profession=getattr(settings, "user_profession", None),
            degrees=split_values(getattr(settings, "user_degrees", "")),
            expertise=split_values(getattr(settings, "user_expertise", "")),
        )

    def _user_profile(self) -> UserProfile | None:
        return self._user_profile_for(self.settings)
