"""Task-scoped capabilities: durable memory and the artifact ledger.

Also pins the prompt-visibility contract: a registered capability that is not
listed in the context builder never reaches the model, which is exactly how a
tool silently stops existing.
"""

from __future__ import annotations

import json

import httpx
import pytest

from assistant.application import TaskService
from assistant.capabilities.artifacts import ArtifactTool
from assistant.capabilities.memory import MemoryTool
from assistant.context import VISIBLE_TOOL_NAMES, ContextBuilder
from assistant.devices.registry import build_tool_registry
from assistant.domain.contracts import ContractScope
from assistant.domain.models import ErrorType, Operation, Project, Task, TaskRequest
from assistant.infrastructure.db import Database
from assistant.infrastructure.repositories import TaskRepository
from assistant.llm import MockLLMProvider
from assistant.tools import ToolRegistry


@pytest.mark.asyncio
async def test_memory_write_then_search_round_trips(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        tool = MemoryTool(repository)

        written = await tool.execute(
            "write",
            {"_task_id": "none", "key": "pytest-baseline", "value": "238 tests", "kind": "lesson"},
            timeout=5,
        )
        assert written.success is True, written.error
        assert written.output["stored"]["kind"] == "lesson"
        assert written.output["stored"]["source"] == "AGENT"
        assert written.output["stored"]["scope"] == "global"

        found = await tool.execute("search", {"query": "baseline", "limit": 5}, timeout=5)
        assert found.success is True
        assert [item["key"] for item in found.output["memories"]] == ["pytest-baseline"]


@pytest.mark.asyncio
async def test_memory_rejects_blank_key_and_unknown_kind(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'memory-invalid.db'}")
    await database.create_all()
    async with database.sessions() as session:
        tool = MemoryTool(TaskRepository(session))

        blank = await tool.execute("write", {"key": "   ", "value": "x"}, timeout=5)
        kind = await tool.execute(
            "write", {"key": "k", "value": "x", "kind": "nonsense"}, timeout=5
        )

        assert blank.success is False and blank.error_type is ErrorType.INVALID_ARGUMENT
        assert kind.success is False and "kind must be one of" in kind.error


@pytest.mark.asyncio
async def test_memory_write_is_scoped_to_the_project_by_default(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'memory-scope.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        project = await repository.create_project(
            Project(name="scoped", path=str(tmp_path))
        )
        task = Task(goal="aprende", project_id=project.id)
        await repository.save_task(task)
        tool = MemoryTool(repository)

        result = await tool.execute(
            "write", {"_task_id": task.id, "key": "build-cmd", "value": "pytest -q"}, timeout=5
        )

        assert result.output["stored"]["scope"] == ContractScope.PROJECT.value
        assert result.output["stored"]["value"] == "pytest -q"


@pytest.mark.asyncio
async def test_memory_write_without_task_context_stays_global(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'memory-global.db'}")
    await database.create_all()
    async with database.sessions() as session:
        tool = MemoryTool(TaskRepository(session))

        result = await tool.execute("write", {"key": "port", "value": "8080"}, timeout=5)

        assert result.output["stored"]["scope"] == "global"


@pytest.mark.asyncio
async def test_artifact_tool_lists_and_reads_text_and_refuses_binaries(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'artifacts.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session, MockLLMProvider(), ToolRegistry(), workspace_root=str(tmp_path)
        )
        # A text file and a PNG attached: one readable, one not.
        from tests.test_attachments import PNG_BYTES, _upload

        task = await service.create_task(
            TaskRequest(
                goal="inspecciona artefactos", attachments=[_upload(PNG_BYTES)]
            )
        )
        written_file = tmp_path / "report.md"
        written_file.write_text("# Informe\ncontenido\n", encoding="utf-8")
        from assistant.domain.contracts import ArtifactKind, ArtifactRef

        await service.repository.save_artifact(
            task.id,
            ArtifactRef(
                id="text-1",
                kind=ArtifactKind.FILE,
                description="informe",
                producer_node_id="node-1",
                path=str(written_file),
            ),
        )
        tool = ArtifactTool(service.repository)

        listed = await tool.execute("list", {"_task_id": task.id}, timeout=5)
        assert listed.success is True
        assert listed.output["count"] == 2

        text = await tool.execute(
            "read", {"_task_id": task.id, "artifact_id": "text-1"}, timeout=5
        )
        assert text.success is True
        assert "contenido" in text.output["content"]
        assert text.output["truncated"] is False

        image = await tool.execute(
            "read", {"_task_id": task.id, "artifact_id": task.attachments[0]["id"]}, timeout=5
        )
        assert image.success is True
        assert "content" not in image.output
        assert "Binary artifact" in image.output["note"]

        missing = await tool.execute(
            "read", {"_task_id": task.id, "artifact_id": "nope"}, timeout=5
        )
        assert missing.success is False and missing.error_type is ErrorType.NOT_FOUND


@pytest.mark.asyncio
async def test_artifact_tool_requires_a_task_context(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'artifacts-nocontext.db'}")
    await database.create_all()
    async with database.sessions() as session:
        result = await ArtifactTool(TaskRepository(session)).execute("list", {}, timeout=5)

        assert result.success is False
        assert result.error_type is ErrorType.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_service_injects_the_task_id_and_never_the_model(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'inject.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session, MockLLMProvider(), ToolRegistry(), workspace_root=str(tmp_path)
        )
        task = Task(goal="memoriza")
        await service.repository.save_task(task)

        operation = Operation(tool="memory", method="search", args={"query": "x"})
        await service._resolve_operation_root(operation, None, task)
        # A model-supplied value is overwritten, never trusted.
        forged = Operation(
            tool="memory", method="search", args={"query": "x", "_task_id": "attacker"}
        )
        await service._resolve_operation_root(forged, None, task)
        # Recurring work inherits the project and evidence of its creator, so it
        # needs the same injection as memory and artifacts.
        recurring = Operation(
            tool="schedule", method="every", args={"goal": "x", "every_seconds": 900}
        )
        await service._resolve_operation_root(recurring, None, task)

        assert operation.args["_task_id"] == task.id
        assert forged.args["_task_id"] == task.id
        assert recurring.args["_task_id"] == task.id


def test_new_capabilities_are_visible_to_the_model(tmp_path):
    """A registered capability that the context builder drops does not exist."""
    registry = build_tool_registry(repository=_StubRepository())
    tool_names = {definition.name for definition in registry.definitions()}
    assert {"screen", "memory", "artifact", "types"} <= tool_names

    builder = ContextBuilder(
        repository=_StubRepository(),
        tools=build_tool_registry(repository=_StubRepository()),
        vision_enabled=False,
    )
    for intent in ("audit", "create", "edit", "browser", "release", "deploy", "general"):
        groups = builder._available_actions(intent=intent)
        visible = {
            tool["name"]
            for group in groups
            for tool in group["tools"]
            if isinstance(tool, dict)
        }
        assert {"memory", "artifact", "screen", "types"} <= visible, intent


def test_every_real_capability_is_visible_in_every_intent():
    """Not a sample of names: all of them, so a new tool cannot hide.

    The failure this catches is the quiet one. Adding a tool and forgetting this
    list does not raise anything: the tool is registered, dispatchable, and
    invisible to the model, which then reports it cannot do what it can do.
    """
    registry = build_tool_registry(repository=_StubRepository())
    registered = {
        definition.name
        for definition in registry.definitions()
        # Placeholder branches only answer "adapter not connected".
        if not definition.name.startswith("device.")
    }
    builder = ContextBuilder(repository=_StubRepository(), tools=registry, vision_enabled=False)

    for intent in ("audit", "create", "edit", "browser", "release", "deploy", "general"):
        groups = builder._available_actions(intent=intent)
        visible = {
            tool["name"]
            for group in groups
            for tool in group["tools"]
            if isinstance(tool, dict)
        }
        assert registered <= visible, (intent, sorted(registered - visible))


def test_the_visibility_list_names_only_real_capabilities():
    """A stale name is a promise the catalog cannot keep."""
    registry = build_tool_registry(repository=_StubRepository())
    known = {definition.name for definition in registry.definitions()}
    # ``schedule`` is registered by the composition root, which owns the service,
    # and ``input`` only exists when the deployment opts into mouse/keyboard
    # control. Both must be listed anyway: opting in has to be enough to make a
    # capability visible, and the builder never sees whether input is on.
    known |= {"schedule", "input"}

    assert VISIBLE_TOOL_NAMES - known == set()
    assert not [name for name in VISIBLE_TOOL_NAMES if name.startswith("device.")]


def test_release_and_deploy_intents_lead_with_the_tools_that_do_the_work():
    builder = ContextBuilder(
        repository=_StubRepository(),
        tools=build_tool_registry(repository=_StubRepository()),
        vision_enabled=False,
    )

    def primary(intent: str) -> set[str]:
        groups = builder._available_actions(intent=intent)
        return {tool["name"] for tool in groups[0]["tools"]}

    assert "git" in primary("release")
    assert "deployment" in primary("deploy")
    assert builder._planner_intent("haz commit de lo que hemos hecho") == "release"
    assert builder._planner_intent("despliega la aplicacion") == "deploy"


def test_task_scoped_capabilities_need_a_repository():
    names = {definition.name for definition in build_tool_registry().definitions()}

    assert "memory" not in names and "artifact" not in names


class _StubRepository:
    """Only needs to answer the tool-definition lookup used above."""

    async def get_project(self, project_id):
        return None

    async def list_projects(self, enabled_only=False):
        return []

    async def list_events(self, task_id):
        return []

    async def list_artifacts(self, task_id):
        return []

    async def search_memory(self, query, limit=8, scope=None, scope_id=None):
        return []

    async def list_memory(self, limit=20, scope=None, scope_id=None):
        return []


def test_api_prefix_and_router_surface_is_mounted():
    """The OpenAPI schema is the canonical view of the mounted prefixed paths."""
    from assistant.api.application import API_PREFIX, app

    schema_paths = set(app.openapi()["paths"])
    for expected in (
        f"{API_PREFIX}/health",
        f"{API_PREFIX}/metrics",
        f"{API_PREFIX}/overview",
        f"{API_PREFIX}/tasks",
        f"{API_PREFIX}/resources",
        f"{API_PREFIX}/runtime/idle",
        f"{API_PREFIX}/runtime/offpeak",
    ):
        assert expected in schema_paths, expected

    methods = app.openapi()["paths"][f"{API_PREFIX}/runtime/offpeak"]
    assert {"get", "put"} <= set(methods)


def test_frontend_client_exposes_the_new_runtime_calls():
    from pathlib import Path

    client = Path("frontend/src/api/client.js").read_text(encoding="utf-8")
    assert "/runtime/offpeak" in client
    store = Path("frontend/src/stores/system.js").read_text(encoding="utf-8")
    assert "offpeak" in store


def _unused(_: httpx.AsyncClient) -> None:  # keeps the httpx import meaningful
    json.dumps({})
