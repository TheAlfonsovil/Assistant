import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from assistant.application import TaskService
from assistant.config import Settings
from assistant.context import ContextBuilder
from assistant.devices.computer.browser import BrowserTool
from assistant.devices.computer.web import WebTool
from assistant.devices.registry import DEVICE_BRANCHES, build_tool_registry
from assistant.domain.graph import TaskGraph
from assistant.domain.models import (
    ErrorType,
    GraphEdge,
    MemoryRecord,
    NodeStatus,
    NodeType,
    Operation,
    OperationResult,
    Project,
    Task,
    TaskNode,
    TaskRequest,
    TaskStatus,
)
from assistant.infrastructure.db import Database
from assistant.infrastructure.repositories import TaskRepository
from assistant.llm import (
    ActionProposal,
    AssistantResponse,
    FinalReport,
    MockLLMProvider,
    NodeDecision,
    OllamaLLMProvider,
    PlanNodeProposal,
    PlanProposal,
)
from assistant.project_analysis import ProjectAnalyzer
from assistant.prompts.v1.template import render
from assistant.recovery import RecoveryManager
from assistant.runtime import TaskRuntime
from assistant.startup.manager import StartupManager
from assistant.tools import MockTool, Tool, ToolDefinition, ToolRegistry


def test_device_registry_exposes_four_branches_and_computer_actions():
    assert [branch.name for branch in DEVICE_BRANCHES] == ["computer", "mobile", "home", "robot"]
    definitions = {definition.name for definition in build_tool_registry().definitions()}
    assert {"filesystem", "shell", "git", "project", "codegraph", "system", "web", "browser"} <= definitions
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


@pytest.mark.asyncio
async def test_browser_open_records_origin_and_url(tmp_path, monkeypatch):
    monkeypatch.setattr("webbrowser.open", lambda url, new: True)
    tool = BrowserTool(tmp_path / "browser-log.jsonl")

    opened = await tool.execute(
        "open",
        {"url": "https://www.youtube.com/watch?v=abc", "origin": "task-123/node-1"},
        5,
    )
    log = await tool.execute("log", {"limit": 10}, 5)

    assert opened.success is True
    assert opened.output["verified"] is False
    assert log.success is True
    assert log.output["entries"][0]["url"] == "https://www.youtube.com/watch?v=abc"
    assert log.output["entries"][0]["origin"] == "task-123/node-1"


@pytest.mark.asyncio
async def test_browser_close_requires_observed_identity(tmp_path):
    tool = BrowserTool(tmp_path / "browser-log.jsonl")

    result = await tool.execute("close_tab", {"tab_id": "unknown"}, 5)

    assert result.success is False
    assert "inspected Chromium" in result.error


@pytest.mark.asyncio
async def test_browser_close_site_refuses_unobservable_tabs(tmp_path, monkeypatch):
    tool = BrowserTool(tmp_path / "browser-log.jsonl")

    async def unobservable(timeout):
        return OperationResult(
            success=True,
            output={"browsers": [], "observable": False, "limitation": "tabs unavailable"},
        )

    monkeypatch.setattr(tool, "_inspect", unobservable)
    result = await tool.execute("close_site", {"site": "youtube.com"}, 5)

    assert result.success is False
    assert result.error == "tabs unavailable"


def test_prompt_template_replaces_context_markers():
    rendered = render(
        "{{system_role}} {{user_prompt}} {{task}} {{output_schema}}",
        {"user_prompt": "run tests", "task": {"id": "task-1"}},
        {"type": "object"},
    )

    assert "{{" not in rendered
    assert "run tests" in rendered
    assert "task-1" in rendered


def test_final_response_template_receives_execution_evidence():
    template = (Path(__file__).parents[1] / "assistant" / "prompts" / "v1" / "final_response.md").read_text(
        encoding="utf-8"
    )
    rendered = render(
        template,
        {"user_prompt": "audit", "events": [{"event": "TOOL_RESULT"}]},
        {"type": "object"},
    )

    assert "TOOL_RESULT" in rendered
    assert "{{execution_evidence}}" not in rendered


@pytest.mark.asyncio
async def test_mock_provider_creates_human_report():
    report = await MockLLMProvider().summarize({"events": [{"event": "TOOL_RESULT"}]})

    assert isinstance(report, FinalReport)
    assert isinstance(report, AssistantResponse)
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
async def test_ollama_provider_traces_prompt_and_validated_response():
    traces = []

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": '{"task_id": null, "nodes": []}'})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaLLMProvider(
        "http://ollama.test", "test-model", client=client, trace_sink=traces.append
    )
    await provider.plan({"user_prompt": "audit", "task": {"id": "task-1"}})
    await provider.close()

    assert [trace["phase"] for trace in traces] == [
        "LLM_REQUEST_BUILT",
        "LLM_RESPONSE_PARSED",
    ]
    assert traces[0]["prompt_chars"] > 0
    assert traces[1]["validated"]["nodes"] == []


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


@pytest.mark.asyncio
async def test_runtime_does_not_consume_tasks_when_llm_is_not_ready():
    class Repository:
        async def list_tasks(self):
            return [Task(goal="wait", status=TaskStatus.QUEUED)]

    called = []
    runtime = TaskRuntime(
        Repository(),
        lambda task_id: _record_task(called, task_id),
        is_ready=lambda: False,
    )

    assert await runtime.run_once() == 0
    assert called == []
    assert runtime.last_active_count == 0


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
async def test_running_task_with_active_lease_is_not_reconciled_as_blocked(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'active-lease.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="already running"))
        node = (await service.repository.list_nodes(task.id))[0]
        node.status = NodeStatus.RUNNING
        task.status = TaskStatus.RUNNING
        await service.repository.save_node(node)
        await service.repository.save_task(task)
        assert await service.acquire_lease(node.id) is True

        result = await service.run_task(task.id, max_steps=1)

        assert result.status is TaskStatus.RUNNING
        assert result.failure_reason is None
    await database.close()


@pytest.mark.asyncio
async def test_llm_timeout_blocks_task_and_generates_final_response(tmp_path):
    class HangingProvider(MockLLMProvider):
        async def plan(self, context):
            await asyncio.sleep(1)
            return await super().plan(context)

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'llm-timeout.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, HangingProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="timeout"))
        task.budget.max_execution_time = 0.01
        await service.repository.save_task(task)

        result = await service.run_task(task.id)
        persisted = await service.get_task(task.id)

        assert result.status is TaskStatus.BLOCKED
        assert persisted.metadata["final_response"]["summary"]
        assert any(
            event.event_type == "TASK_BUDGET_EXHAUSTED"
            for event in await service.repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_invalid_planner_graph_is_rejected_before_persistence(tmp_path):
    class CyclicPlanner(MockLLMProvider):
        async def plan(self, context):
            return PlanProposal(
                nodes=[
                    PlanNodeProposal(id="first", description="first", dependencies=["second"]),
                    PlanNodeProposal(id="second", description="second", dependencies=["first"]),
                ]
            )

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'invalid-plan.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, CyclicPlanner(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="invalid plan"))

        result = await service.run_task(task.id)

        assert result.status is TaskStatus.FAILED
        assert len(await service.repository.list_nodes(task.id)) == 1
        assert await service.repository.list_edges(task.id) == []
    await database.close()


@pytest.mark.asyncio
async def test_single_project_is_selected_and_analysis_uses_registered_path(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "main.py").write_text("print('ok')", encoding="utf-8")
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'project.db'}")
    await database.create_all()
    async with database.sessions() as session:
        provider = MockLLMProvider(
            Operation(tool="project", method="analyze", args={"root": str(tmp_path / "wrong")})
        )
        service = TaskService(session, provider, ToolRegistry())
        project = await service.create_project(Project(name="main", path=str(project_path)))
        task = await service.create_task(TaskRequest(goal="audit the project"))

        assert task.project_id == project.id
        result = await service.run_task(task.id)
        node = (await service.repository.list_nodes(task.id))[-1]
        assert result.status is TaskStatus.SUCCEEDED
        assert node.output_data["output"]["root"] == str(project_path.resolve())
    await database.close()


@pytest.mark.asyncio
async def test_recovery_requeues_planning_tasks(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        task = Task(goal="recover", status=TaskStatus.PLANNING)
        await repository.save_task(task)
        recovered = await RecoveryManager(session).recover()
        assert recovered == 1
        assert (await repository.get_task(task.id)).status is TaskStatus.QUEUED
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
async def test_replan_creates_changed_subtasks_and_completes(tmp_path):
    class ReplanVerifier:
        def verify(self, result):
            from assistant.domain.models import VerificationDecision
            from assistant.llm import VerificationResult
            return VerificationResult(decision=VerificationDecision.REPLAN, reason="strategy changed")

    class ReplanProvider(MockLLMProvider):
        async def decide(self, context):
            self.decisions += 1
            if self.decisions > 1:
                return NodeDecision(action="COMPLETE", reason="subtask completed")
            return NodeDecision(action="OPERATION", operation=self.operation)

        async def replan(self, context):
            return NodeDecision(action="SUBTASKS", subtasks=["inspect failure", "apply correction"])

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'replan.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session,
            ReplanProvider(Operation(tool="mock.always_fail", method="run")),
            ToolRegistry([MockTool("always_fail")]),
            verifier=ReplanVerifier(),
        )
        task = await service.create_task(TaskRequest(goal="recover from failure"))
        result = await service.run_task(task.id)
        nodes = await service.repository.list_nodes(task.id)
        assert result.status is TaskStatus.SUCCEEDED
        assert {node.description for node in nodes} >= {"inspect failure", "apply correction"}
        assert any(event.event_type == "REPLAN_REQUESTED" for event in await service.repository.list_events(task.id))
    await database.close()


@pytest.mark.asyncio
async def test_wait_user_can_be_resumed(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'wait.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session,
            MockLLMProvider(Operation(tool="mock.always_fail", method="run")),
            ToolRegistry([MockTool("always_fail")]),
        )
        task = await service.create_task(TaskRequest(goal="wait for user"))
        node = (await service.repository.list_nodes(task.id))[0]
        node.status = NodeStatus.WAITING
        await service.repository.save_node(node)
        task.status = TaskStatus.WAITING
        await service.repository.save_task(task)
        resumed = await service.resume_task(task.id)
        assert resumed.status is TaskStatus.READY
        assert (await service.repository.get_node(node.id)).status is NodeStatus.READY
    await database.close()


@pytest.mark.asyncio
async def test_user_required_result_enters_waiting_and_resume_requeues_node(tmp_path):
    class ApprovalTool(Tool):
        definition = ToolDefinition(name="approval", description="Needs approval", methods=["run"])

        async def execute(self, method, args, timeout):
            return OperationResult(
                success=False,
                error="approval required",
                error_type=ErrorType.USER_REQUIRED,
            )

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'approval.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session,
            MockLLMProvider(Operation(tool="approval", method="run")),
            ToolRegistry([ApprovalTool()]),
        )
        task = await service.create_task(TaskRequest(goal="approve operation"))
        waiting = await service.run_task(task.id)
        assert waiting.status is TaskStatus.WAITING
        assert (await service.repository.list_nodes(task.id))[-1].status is NodeStatus.WAITING
        assert any(
            event.event_type == "WAITING_FOR_USER"
            for event in await service.repository.list_events(task.id)
        )
        assert (await service.resume_task(task.id)).status is TaskStatus.READY
    await database.close()


@pytest.mark.asyncio
async def test_waiting_task_accepts_input_and_requeues_selected_node(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'input.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="provide a value"))
        node = (await service.repository.list_nodes(task.id))[0]
        node.status = NodeStatus.WAITING
        task.status = TaskStatus.WAITING
        await service.repository.save_node(node)
        await service.repository.save_task(task)

        resumed = await service.submit_task_input(task.id, {"value": "approved"}, node.id)

        assert resumed.status is TaskStatus.READY
        updated = await service.repository.get_node(node.id)
        assert updated.status is NodeStatus.READY
        assert updated.input_data == {"value": "approved"}
        assert any(
            event.event_type == "USER_INPUT_RECEIVED"
            for event in await service.repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_tool_budget_exhaustion_is_persisted(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'budget.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session,
            MockLLMProvider(),
            ToolRegistry([MockTool("success")]),
        )
        task = await service.create_task(TaskRequest(goal="budget"))
        task.budget.max_tool_calls = 0
        await service.repository.save_task(task)
        result = await service.run_task(task.id)
        assert result.status is TaskStatus.BLOCKED
        assert "tool_calls budget exhausted" in result.failure_reason
        assert any(
            event.event_type == "BUDGET_EXHAUSTED"
            for event in await service.repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_unexpected_planner_failure_is_persisted_as_failed(tmp_path):
    class ExplodingProvider(MockLLMProvider):
        async def plan(self, context):
            raise RuntimeError("planner unavailable")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'planner-error.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, ExplodingProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="planner error"))
        result = await service.run_task(task.id)
        assert result.status is TaskStatus.FAILED
        assert "planner unavailable" in result.failure_reason
        assert any(
            event.event_type == "TASK_FAILED"
            for event in await service.repository.list_events(task.id)
        )
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


@pytest.mark.asyncio
async def test_direct_answer_completes_without_execution_node(tmp_path):
    class DirectAnswerProvider(MockLLMProvider):
        async def plan(self, context):
            return PlanProposal(answer="4")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'direct.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session,
            DirectAnswerProvider(),
            ToolRegistry([MockTool("success")]),
        )
        task = await service.create_task(TaskRequest(goal="cuanto es dos mas dos"))
        result = await service.run_task(task.id)

        assert result.status is TaskStatus.SUCCEEDED
        assert result.result_summary == "4"
        assert len(await service.repository.list_nodes(task.id)) == 1
        assert not any(
            event.event_type == "TOOL_CALLED"
            for event in await service.repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_blocked_node_reconciles_task_state(tmp_path):
    class InvalidDecisionProvider(MockLLMProvider):
        async def decide(self, context):
            return NodeDecision(action="OPERATION")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'blocked.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, InvalidDecisionProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="invalid operation"))
        result = await service.run_task(task.id)

        assert result.status is TaskStatus.BLOCKED
        assert result.failure_reason == "task has blocked nodes"
        assert any(
            event.event_type == "TASK_NO_PROGRESS"
            for event in await service.repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_blocked_task_accepts_user_solution_and_retries_node(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'blocked-solution.db'}")
    await database.create_all()
    async with database.sessions() as session:
        tool = MockTool("success")
        service = TaskService(
            session,
            MockLLMProvider(Operation(tool="mock.success", method="run")),
            ToolRegistry([tool]),
        )
        task = await service.create_task(TaskRequest(goal="recover blocked task"))
        root = (await service.repository.list_nodes(task.id))[0]
        root.status = NodeStatus.SUCCEEDED
        blocked = TaskNode(
            task_id=task.id,
            type=NodeType.OPERATION,
            description="blocked operation",
            status=NodeStatus.BLOCKED,
            error="missing input",
        )
        await service.repository.save_node(root)
        await service.repository.save_node(blocked)
        task.status = TaskStatus.BLOCKED
        task.failure_reason = "task has blocked nodes"
        await service.repository.save_task(task)

        recovered = await service.submit_task_input(task.id, {"solution": "use mock"}, blocked.id)
        assert recovered.status is TaskStatus.READY
        assert (await service.repository.get_node(blocked.id)).status is NodeStatus.READY

        result = await service.run_task(task.id)
        assert result.status is TaskStatus.SUCCEEDED
        assert any(
            event.event_type == "USER_SOLUTION_RECEIVED"
            for event in await service.repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_blocked_task_requires_solution_before_resume(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'blocked-resume.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="blocked"))
        task.status = TaskStatus.BLOCKED
        await service.repository.save_task(task)

        with pytest.raises(ValueError, match="require a solution"):
            await service.resume_task(task.id)
    await database.close()


@pytest.mark.asyncio
async def test_redefine_task_resets_graph_and_keeps_identity(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'redefine.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="old goal"))
        root = (await service.repository.list_nodes(task.id))[0]
        child = TaskNode(task_id=task.id, type=NodeType.OPERATION, description="old step")
        await service.repository.save_node(child)
        await service.repository.save_edge(task.id, GraphEdge(from_node=root.id, to_node=child.id))
        task.status = TaskStatus.BLOCKED
        task.metadata = {"llm_calls": 4, "final_response": {"summary": "old"}}
        await service.repository.save_task(task)

        redefined = await service.redefine_task(
            task.id, "new goal", description="new description", metadata={"source": "user"}
        )
        nodes = await service.repository.list_nodes(task.id)

        assert redefined.id == task.id
        assert redefined.status is TaskStatus.QUEUED
        assert redefined.goal == "new goal"
        assert redefined.metadata == {"source": "user"}
        assert len(nodes) == 1
        assert nodes[0].id == root.id
        assert nodes[0].status is NodeStatus.READY
        assert await service.repository.list_edges(task.id) == []
    await database.close()


@pytest.mark.asyncio
async def test_delete_task_removes_persisted_graph_and_events(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'delete-task.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="delete me"))

        assert await service.delete_task(task.id) is True
        assert await service.get_task(task.id) is None
        assert await service.repository.list_nodes(task.id) == []
        assert await service.repository.list_edges(task.id) == []
        assert await service.repository.list_events(task.id) == []
    await database.close()


@pytest.mark.asyncio
async def test_expired_task_deadline_blocks_before_execution(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'deadline.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(
            TaskRequest(goal="expired", deadline=datetime.now(UTC) - timedelta(seconds=1))
        )
        result = await service.run_task(task.id)

        assert result.status is TaskStatus.BLOCKED
        assert result.failure_reason == "task deadline exceeded"
        assert any(
            event.event_type == "TASK_DEADLINE_EXCEEDED"
            for event in await service.repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_expired_node_deadline_is_terminal(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'node-deadline.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="node deadline"))
        root = (await service.repository.list_nodes(task.id))[0]
        root.status = NodeStatus.SUCCEEDED
        await service.repository.save_node(root)
        node = TaskNode(
            task_id=task.id,
            type=NodeType.OPERATION,
            description="expired operation",
            status=NodeStatus.READY,
        )
        node.metadata["deadline"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        await service.repository.save_node(node)
        task.status = TaskStatus.READY
        await service.repository.save_task(task)

        result = await service.run_task(task.id)

        assert result.status is TaskStatus.BLOCKED
        assert (await service.repository.get_node(node.id)).status is NodeStatus.BLOCKED
        assert any(
            event.event_type == "NODE_DEADLINE_EXCEEDED"
            for event in await service.repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_terminal_task_persists_one_final_response(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'response.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="return a response"))

        result = await service.run_task(task.id)
        persisted = await service.get_task(task.id)

        assert result.status is TaskStatus.SUCCEEDED
        assert persisted.metadata["final_response"]["summary"]
        assert any(
            event.event_type == "FINAL_RESPONSE_READY"
            for event in await service.repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_structural_wait_node_completes_after_input(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'structural-wait.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="wait structurally"))
        root = (await service.repository.list_nodes(task.id))[0]
        root.status = NodeStatus.SUCCEEDED
        await service.repository.save_node(root)
        wait_node = TaskNode(task_id=task.id, type=NodeType.WAIT, description="provide input", status=NodeStatus.READY)
        await service.repository.save_node(wait_node)
        await service.repository.save_edge(task.id, GraphEdge(from_node=root.id, to_node=wait_node.id))
        task.status = TaskStatus.READY
        await service.repository.save_task(task)

        waiting = await service.run_task(task.id)
        assert waiting.status is TaskStatus.WAITING

        await service.submit_task_input(task.id, {"answer": "ok"}, wait_node.id)
        completed = await service.run_task(task.id)

        assert completed.status is TaskStatus.SUCCEEDED
        assert (await service.repository.get_node(wait_node.id)).output_data == {
            "input": {"answer": "ok"}
        }
    await database.close()


@pytest.mark.asyncio
async def test_structural_verify_node_checks_successful_dependencies(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'structural-verify.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="verify structurally"))
        root = (await service.repository.list_nodes(task.id))[0]
        root.status = NodeStatus.SUCCEEDED
        dependency = TaskNode(
            task_id=task.id,
            type=NodeType.OPERATION,
            description="completed operation",
            status=NodeStatus.SUCCEEDED,
        )
        verify = TaskNode(task_id=task.id, type=NodeType.VERIFY, description="verify", status=NodeStatus.READY)
        await service.repository.save_node(root)
        await service.repository.save_node(dependency)
        await service.repository.save_node(verify)
        await service.repository.save_edge(task.id, GraphEdge(from_node=dependency.id, to_node=verify.id))
        task.status = TaskStatus.READY
        await service.repository.save_task(task)

        result = await service.run_task(task.id)

        assert result.status is TaskStatus.SUCCEEDED
        assert (await service.repository.get_node(verify.id)).output_data == {
            "verified_dependencies": [dependency.id]
        }
    await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("node_type", [NodeType.CONDITION, NodeType.NOTIFY])
async def test_unsupported_structural_nodes_block_explicitly(tmp_path, node_type):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'unsupported-{node_type.value}.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="unsupported structural node"))
        root = (await service.repository.list_nodes(task.id))[0]
        root.status = NodeStatus.SUCCEEDED
        node = TaskNode(task_id=task.id, type=node_type, description="unsupported", status=NodeStatus.READY)
        await service.repository.save_node(root)
        await service.repository.save_node(node)
        task.status = TaskStatus.READY
        await service.repository.save_task(task)

        result = await service.run_task(task.id)

        assert result.status is TaskStatus.BLOCKED
        assert "not supported" in (await service.repository.get_node(node.id)).error
    await database.close()
