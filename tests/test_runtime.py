import json

import httpx
import pytest

from assistant.application import TaskService
from assistant.config import Settings
from assistant.context import ContextBuilder
from assistant.devices.computer.web import WebTool
from assistant.devices.registry import DEVICE_BRANCHES, build_tool_registry
from assistant.domain.graph import TaskGraph
from assistant.domain.models import (
    MemoryRecord,
    NodeStatus,
    NodeType,
    Operation,
    Task,
    TaskNode,
    TaskRequest,
    TaskStatus,
)
from assistant.infrastructure.db import Database
from assistant.llm import (
    ActionProposal,
    FinalReport,
    MockLLMProvider,
    OllamaLLMProvider,
    PlanProposal,
)
from assistant.project_analysis import ProjectAnalyzer
from assistant.prompts.v1.template import render
from assistant.runtime import TaskRuntime
from assistant.startup.manager import StartupManager
from assistant.tools import MockTool, ToolRegistry


def test_device_registry_exposes_four_branches_and_computer_actions():
    assert [branch.name for branch in DEVICE_BRANCHES] == ["computer", "mobile", "home", "robot"]
    definitions = {definition.name for definition in build_tool_registry().definitions()}
    assert {"filesystem", "shell", "git", "project", "codegraph", "system", "web"} <= definitions
    assert {"device.mobile", "device.home", "device.robot"} <= definitions


@pytest.mark.asyncio
async def test_computer_filesystem_info_and_search(tmp_path):
    nested = tmp_path / "src" / "main.py"
    nested.parent.mkdir()
    nested.write_text("print('ok')", encoding="utf-8")
    registry = build_tool_registry()

    info = await registry.execute(Operation(tool="filesystem", method="info", args={"path": str(nested)}))
    search = await registry.execute(
        Operation(tool="filesystem", method="search", args={"path": str(tmp_path), "pattern": "*.py"})
    )
    system = await registry.execute(Operation(tool="system", method="info"))

    assert info.success is True
    assert info.output["type"] == "file"
    assert search.success is True
    assert search.output[0]["path"].endswith("main.py")
    assert system.success is True
    assert "os" in system.output


@pytest.mark.asyncio
async def test_codegraph_builds_module_nodes_and_import_edges(tmp_path):
    (tmp_path / "main.py").write_text("import helper\ndef run():\n    return helper.value\n", encoding="utf-8")
    (tmp_path / "helper.py").write_text("value = 1\n", encoding="utf-8")

    result = await build_tool_registry().execute(
        Operation(tool="codegraph", method="build", args={"root": str(tmp_path)})
    )

    assert result.success is True
    assert {node["kind"] for node in result.output["graph"]["nodes"]} == {"module", "symbol"}
    assert any(edge["to"] == "helper" for edge in result.output["graph"]["edges"])


@pytest.mark.asyncio
async def test_web_blocks_local_destinations():
    tool = WebTool()
    result = await tool.execute("fetch", {"url": "http://127.0.0.1:11434/api/tags"}, 5)

    assert result.success is False
    assert "Private and local" in result.error


def test_prompt_template_replaces_context_markers():
    rendered = render(
        "{{system_role}} {{user_prompt}} {{task}} {{output_schema}}",
        {"user_prompt": "run tests", "task": {"id": "task-1"}},
        {"type": "object"},
    )

    assert "{{" not in rendered
    assert "run tests" in rendered
    assert "task-1" in rendered


@pytest.mark.asyncio
async def test_mock_provider_creates_human_report():
    report = await MockLLMProvider().summarize({"events": [{"event": "TOOL_RESULT"}]})

    assert isinstance(report, FinalReport)
    assert report.summary
    assert "events=1" in report.evidence


@pytest.mark.asyncio
async def test_create_action_proposal_waits_for_review(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'action.db'}")
    await database.create_all()
    async with database.sessions() as session:
        proposal = ActionProposal(name="inspect", description="Inspect a file", code="print('ok')")
        service = TaskService(
            session,
            MockLLMProvider(),
            ToolRegistry([MockTool("success")]),
        )
        task = await service.create_task(TaskRequest(goal="create a helper"))
        service.llm.decide = lambda context: _create_action_decision(proposal)
        result = await service.run_task(task.id)

        assert result.status is TaskStatus.WAITING
        assert (tmp_path / "data" / "generated_actions" / "inspect.py").exists()
    await database.close()


async def _create_action_decision(proposal):
    from assistant.llm import NodeDecision

    return NodeDecision(action="CREATE_ACTION", action_proposal=proposal)


@pytest.mark.asyncio
async def test_startup_loads_memory_and_syncs_system_facts_once(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'startup.db'}")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'startup.db'}",
        persist_user_profile=False,
    )
    provider = MockLLMProvider()

    first = await StartupManager(database, settings, provider).initialize()
    second = await StartupManager(database, settings, provider).initialize()

    assert first.first_initialization is True
    assert second.first_initialization is False
    assert "python_version" in second.system_facts
    assert len(second.loaded_memories) == len(second.system_facts)
    await database.close()


@pytest.mark.asyncio
async def test_startup_persists_user_profile_from_environment(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'profile.db'}")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'profile.db'}",
        user_name="Alfonso Villar Garcia",
        user_birth_date="1997-01-08",
        user_profession="Programador",
        user_degrees="Grado en Matematicas, Grado en Ingenieria Informatica",
        user_expertise="Experto",
    )

    report = await StartupManager(database, settings, MockLLMProvider()).initialize()

    assert report.user_profile is not None
    assert report.user_profile.name == "Alfonso Villar Garcia"
    assert report.user_profile.degrees == [
        "Grado en Matematicas",
        "Grado en Ingenieria Informatica",
    ]
    profile_memory = next(item for item in report.loaded_memories if item.kind == "user_profile")
    assert profile_memory.key == "primary"
    assert profile_memory.value["name"] == "Alfonso Villar Garcia"
    await database.close()


def test_ollama_planner_contract_accepts_graph_response():
    proposal = PlanProposal.model_validate(
        {
            "task_id": "task-1",
            "nodes": [
                {
                    "id": "step-1",
                    "description": "run tests",
                    "type": "OPERATION",
                    "dependencies": [],
                }
            ],
        }
    )
    assert proposal.nodes[0].dependencies == []


@pytest.mark.asyncio
async def test_ollama_provider_sends_versioned_prompt_and_json_schema():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"response": '{"task_id": null, "nodes": []}'},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaLLMProvider("http://ollama.test", "test-model", client=client)
    await provider.plan({"task": {"goal": "run tests"}})
    payload = json.loads(requests[0].content)
    prompt = payload["prompt"]
    assert payload["format"]["type"] == "object"
    assert "Valid example:" in prompt
    await provider.close()


@pytest.mark.asyncio
async def test_context_builder_separates_planner_and_resolver_context():
    task = Task(goal="inspect project")
    node = TaskNode(task_id=task.id, type=NodeType.OPERATION, description="run tests", status=NodeStatus.READY)
    class Repository:
        async def search_memory(self, query):
            return [MemoryRecord(kind="preference", key="language", value="Python")]

    builder = ContextBuilder(Repository(), ToolRegistry())
    planner_context = await builder.for_planner(task)
    resolver_context = await builder.for_resolver(task, node, TaskGraph([node]))

    assert planner_context["phase"] == "PLANNER"
    assert resolver_context["phase"] == "NODE_RESOLVER"
    assert "constraints" in planner_context
    assert resolver_context["node"]["description"] == "run tests"
    assert "node" not in planner_context
    assert planner_context["relevant_memory"][0]["key"] == "language"


@pytest.mark.asyncio
async def test_project_analyzer_reports_files_symbols_and_import_edges(tmp_path):
    (tmp_path / "main.py").write_text("import helper\ndef run():\n    return helper.value\n", encoding="utf-8")
    (tmp_path / "helper.py").write_text("value = 1\n", encoding="utf-8")
    result = await ProjectAnalyzer().analyze(str(tmp_path))
    assert result.success is True
    assert result.output["file_count"] == 2
    assert any(edge["to"] == "helper" for edge in result.output["dependency_edges"])


@pytest.mark.asyncio
async def test_runtime_processes_only_active_tasks_once():
    class Repository:
        async def list_tasks(self):
            return []

    called = []
    runtime = TaskRuntime(Repository(), lambda task_id: _record_task(called, task_id))
    assert await runtime.run_once() == 0
    assert called == []


async def _record_task(called, task_id):
    called.append(task_id)


@pytest.mark.asyncio
async def test_runtime_completes_persisted_task(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}")
    await database.create_all()
    async with database.sessions() as session:
        tools = ToolRegistry([MockTool("success")])
        provider = MockLLMProvider(
            Operation(tool="mock.success", method="run", args={"output": "ok"})
        )
        service = TaskService(session, provider, tools)
        task = await service.create_task(TaskRequest(goal="run success"))
        result = await service.run_task(task.id)
        assert result is not None
        assert result.status is TaskStatus.SUCCEEDED
        nodes = await service.repository.list_nodes(task.id)
        assert nodes[0].status is NodeStatus.SUCCEEDED
        assert await service.repository.list_events(task.id)
    await database.close()


@pytest.mark.asyncio
async def test_runtime_retries_transient_tool_failure(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}")
    await database.create_all()
    async with database.sessions() as session:
        tool = MockTool("fail_once")
        service = TaskService(
            session,
            MockLLMProvider(Operation(tool="mock.fail_once", method="run")),
            ToolRegistry([tool]),
        )
        task = await service.create_task(TaskRequest(goal="retry"))
        result = await service.run_task(task.id)
        assert result.status is TaskStatus.SUCCEEDED
        assert tool.calls == 2
    await database.close()


@pytest.mark.asyncio
async def test_cancel_preserves_task_and_cancels_pending_node(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cancel.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="cancel"))
        cancelled = await service.cancel_task(task.id)
        assert cancelled.status is TaskStatus.CANCELLED
        assert (await service.repository.list_nodes(task.id))[0].status is NodeStatus.CANCELLED
    await database.close()
