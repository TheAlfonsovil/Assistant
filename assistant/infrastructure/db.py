from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool


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
    CURRENT_SCHEMA_VERSION = 5

    def __init__(self, url: str):
        ensure_sqlite_directory(url)
        async_url = async_database_url(url)
        connect_args = {"timeout": 30} if async_url.startswith("sqlite+") else {}
        engine_options = {"future": True, "connect_args": connect_args}
        if async_url.startswith("sqlite+"):
            # SQLite connections are cheap and request/task scoped sessions must
            # never pin a shared QueuePool connection across a long LLM call.
            engine_options["poolclass"] = NullPool
        self.engine = create_async_engine(async_url, **engine_options)
        if async_url.startswith("sqlite+"):
            event.listen(self.engine.sync_engine, "connect", self._configure_sqlite)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    async def create_all(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_version "
                    "(version INTEGER NOT NULL, applied_at DATETIME NOT NULL)"
                )
            )
            await connection.run_sync(self._validate_schema)
            current = await connection.scalar(text("SELECT MAX(version) FROM schema_version"))
            if current is not None and current != self.CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    "Incompatible database schema. This version requires schema "
                    f"{self.CURRENT_SCHEMA_VERSION}, found schema {current}; "
                    "recreate or export the database before starting the assistant."
                )
            if current is None:
                await connection.execute(
                    text(
                        "INSERT INTO schema_version(version, applied_at) "
                        "VALUES (:version, CURRENT_TIMESTAMP)"
                    ),
                    {"version": self.CURRENT_SCHEMA_VERSION},
                )

    @staticmethod
    def _validate_schema(connection) -> None:
        inspector = inspect(connection)
        required_columns = {
            "tasks": {
                "contract_json",
                "working_memory_json",
                "runtime_json",
                "extensions_json",
            },
            "task_nodes": {"contract_json", "runtime_json", "extensions_json"},
            "projects": {"codegraph", "codegraph_updated_at", "codegraph_version"},
            "memories": {"expires_at"},
        }
        incompatible = {
            table: sorted(columns - {column["name"] for column in inspector.get_columns(table)})
            for table, columns in required_columns.items()
            if table in inspector.get_table_names()
            and columns - {column["name"] for column in inspector.get_columns(table)}
        }
        legacy = {
            table
            for table in ("tasks", "task_nodes")
            if table in inspector.get_table_names()
            and "metadata_json" in {
                column["name"] for column in inspector.get_columns(table)
            }
        }
        if incompatible or legacy:
            raise RuntimeError(
                "Incompatible database schema. This version requires schema "
                f"{Database.CURRENT_SCHEMA_VERSION}; recreate or export the database "
                f"before starting the assistant (missing={incompatible}, legacy={sorted(legacy)})."
            )

    async def health_check(self) -> dict[str, object]:
        async with self.sessions() as session:
            await session.execute(text("SELECT 1"))
            result: dict[str, object] = {"status": "ok", "database": "connected"}
            if self.engine.url.drivername.startswith("sqlite"):
                result["journal_mode"] = (
                    await session.scalar(text("PRAGMA journal_mode"))
                )
                result["foreign_keys"] = (
                    await session.scalar(text("PRAGMA foreign_keys"))
                )
                result["integrity"] = (
                    await session.scalar(text("PRAGMA quick_check"))
                )
            return result

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()
