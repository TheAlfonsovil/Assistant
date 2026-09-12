from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def async_database_url(url: str) -> str:
    if url.startswith("sqlite:///"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


def ensure_sqlite_directory(url: str) -> None:
    if not url.startswith("sqlite:///"):
        return
    database_path = url.removeprefix("sqlite:///")
    if database_path == ":memory:":
        return
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)


class Database:
    def __init__(self, url: str):
        ensure_sqlite_directory(url)
        self.engine = create_async_engine(async_database_url(url), future=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(self._migrate_existing_schema)

    @staticmethod
    def _migrate_existing_schema(connection) -> None:
        inspector = inspect(connection)
        if "tasks" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("tasks")}
            if "project_id" not in columns:
                connection.execute(text("ALTER TABLE tasks ADD COLUMN project_id VARCHAR(36)"))
        if "projects" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("projects")}
            if "codegraph" not in columns:
                connection.execute(text("ALTER TABLE projects ADD COLUMN codegraph JSON"))
            if "codegraph_updated_at" not in columns:
                connection.execute(
                    text("ALTER TABLE projects ADD COLUMN codegraph_updated_at DATETIME")
                )
            if "codegraph_version" not in columns:
                connection.execute(
                    text("ALTER TABLE projects ADD COLUMN codegraph_version INTEGER DEFAULT 0")
                )
        if "memories" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("memories")}
            if "expires_at" not in columns:
                connection.execute(text("ALTER TABLE memories ADD COLUMN expires_at DATETIME"))

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()
