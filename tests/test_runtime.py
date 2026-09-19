import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from assistant.application import TaskService
from assistant.config import Settings
from assistant.context import ContextBuilder
from assistant.prompts.v1.template import render
from assistant.devices.computer.browser import BrowserTool
from assistant.devices.computer.web import WebTool
from assistant.devices.registry import DEVICE_BRANCHES, build_tool_registry
from assistant.domain.graph import TaskGraph
from assistant.domain.models import (
    DependencyType,
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
    VerificationDecision,
)
from assistant.infrastructure.db import Database
from assistant.infrastructure.orm import LeaseRow
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
    VerificationResult,
)
from assistant.project_analysis import ProjectAnalyzer
from assistant.prompts.v1.template import render
from assistant.planning import plan_coverage_warnings
from assistant.recovery import RecoveryManager
from assistant.idle import IdleCycle
from assistant.runtime import TaskRuntime
from assistant.startup.manager import StartupManager
from assistant.tools import MockTool, NotificationTool, Tool, ToolDefinition, ToolRegistry


def test_device_registry_exposes_four_branches_and_computer_actions():
    assert [branch.name for branch in DEVICE_BRANCHES] == ["computer", "mobile", "home", "robot"]
    assert DEVICE_BRANCHES[0].platform == "windows"
    assert DEVICE_BRANCHES[0].transport == "local"
    assert DEVICE_BRANCHES[1].platform == "android"
    assert DEVICE_BRANCHES[1].transport == "adb"
    definitions = {definition.name for definition in build_tool_registry().definitions()}
    assert {"filesystem", "shell", "git", "deployment", "project", "codegraph", "system", "web", "browser"} <= definitions
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
async def test_tool_registry_rejects_arguments_with_wrong_declared_type():
    result = await build_tool_registry().execute(
        Operation(tool="project", method="analyze", args={"root": 123})
    )

    assert result.success is False
    assert result.error_type is ErrorType.INVALID_ARGUMENT
    assert "root" in result.error


@pytest.mark.asyncio
async def test_notification_tool_is_registered_and_persists_node_result(tmp_path):
    class NotifyProvider(MockLLMProvider):
        async def plan(self, context):
            return PlanProposal(
                nodes=[PlanNodeProposal(id="notify", description="send notification", type="NOTIFY")]
            )

        async def decide(self, context):
            return NodeDecision(
                action="OPERATION",
                operation=Operation(
                    tool="notify",
                    method="send",
                    args={"channel": "local", "message": "task complete"},
                ),
            )

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'notify-node.db'}")
    await database.create_all()
    async with database.sessions() as session:
        path = tmp_path / "notifications.jsonl"
        registry = ToolRegistry([NotificationTool(path)])
        service = TaskService(session, NotifyProvider(), registry)
        task = await service.create_task(TaskRequest(goal="notify me"))

        result = await service.run_task(task.id)

        assert result.status is TaskStatus.SUCCEEDED
        node = (await service.repository.list_nodes(task.id))[-1]
        assert node.output_data["output"]["message"] == "task complete"
        assert '"message": "task complete"' in path.read_text(encoding="utf-8")
    await database.close()


@pytest.mark.asyncio
async def test_decision_node_evaluates_without_calling_resolver(tmp_path):
    class DecisionProvider(MockLLMProvider):
        async def plan(self, context):
            return PlanProposal(
                nodes=[
                    PlanNodeProposal(
                        id="decision",
                        description="evaluate decision",
                        type="DECISION",
                        metadata={"value": True, "operator": "truthy", "skip_on_true": ["skip"]},
                    ),
                    PlanNodeProposal(id="skip", description="skipped branch"),
                ]
            )

        async def decide(self, context):
            raise AssertionError("structural decision must not call resolver")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'decision-node.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, DecisionProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="evaluate decision"))

        result = await service.run_task(task.id)
        nodes = await service.repository.list_nodes(task.id)

        assert result.status is TaskStatus.SUCCEEDED
        assert next(node for node in nodes if node.description == "skipped branch").status is NodeStatus.CANCELLED
        assert any(event.event_type == "DECISION_EVALUATED" for event in await service.repository.list_events(task.id))
    await database.close()


@pytest.mark.asyncio
async def test_invalid_operation_is_rejected_before_tool_execution(tmp_path):
    class InvalidOperationProvider(MockLLMProvider):
        async def decide(self, context):
            return NodeDecision(
                action="OPERATION",
                operation=Operation(tool="missing", method="run"),
            )

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'invalid-operation.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, InvalidOperationProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="invalid operation"))

        result = await service.run_task(task.id)

        assert result.status is TaskStatus.BLOCKED
        assert any(event.event_type == "OPERATION_REJECTED" for event in await service.repository.list_events(task.id))
    await database.close()


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
async def test_codegraph_builds_system_relationships_and_prompt_context(tmp_path):
    (tmp_path / "a.py").write_text("def helper():\n    return 1\ndef run():\n    return helper()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import a\n", encoding="utf-8")

    result = await build_tool_registry().execute(
        Operation(tool="codegraph", method="system", args={"root": str(tmp_path)})
    )

    assert result.success is True
    assert any(node["kind"] == "module" for node in result.output["graph"]["nodes"])
    assert any(edge["kind"] == "contains" for edge in result.output["graph"]["edges"])
    assert any(edge["kind"] == "calls" for edge in result.output["graph"]["edges"])

    context = ContextBuilder(object(), ToolRegistry([]), workspace_root=str(tmp_path))
    planner_context = await context.for_planner(
        Task(goal="understand the system", metadata={"include_system_graph": True})
    )
    assert "system_graph" not in planner_context


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


def test_prompt_template_replaces_completed_artifacts_marker():
    rendered = render(
        "{{completed_artifacts}}",
        {"completed_artifacts": [{"node_id": "node-1", "artifacts": ["out.txt"]}]},
        {"type": "object"},
    )

    assert "{{completed_artifacts}}" not in rendered
    assert "out.txt" in rendered


def test_plan_reports_missing_goal_coverage():
    proposal = PlanProposal(
        nodes=[PlanNodeProposal(id="step", description="inspect configuration")]
    )

    warnings = plan_coverage_warnings(proposal, "deploy database migration")

    assert any("goal terms" in warning for warning in warnings)


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
        node = (await service.repository.list_nodes(task.id))[-1]
        assert (tmp_path / "data" / "generated_actions" / "inspect.py").exists()
        assert node.metadata["review_required"] is True
        approved = await service.approve_action(task.id, node.id, True)
        assert approved.status is TaskStatus.READY
        assert any(
            event.event_type == "ACTION_APPROVED"
            for event in await service.repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_notification_tool_persists_local_delivery(tmp_path):
    path = tmp_path / "notifications.jsonl"
    result = await NotificationTool(path).execute(
        "send", {"channel": "desktop", "message": "ready"}, 1
    )
    assert result.success is True
    assert '"message": "ready"' in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_condition_node_skips_false_branch(tmp_path):
    class ConditionalProvider(MockLLMProvider):
        async def plan(self, context):
            return PlanProposal(
                nodes=[
                    PlanNodeProposal(
                        id="gate",
                        description="check gate",
                        type="CONDITION",
                        metadata={"value": False, "skip_on_false": ["skip"]},
                    ),
                    PlanNodeProposal(
                        id="skip",
                        description="skipped branch",
                        dependencies=["gate"],
                    ),
                    PlanNodeProposal(
                        id="continue",
                        description="continue branch",
                        dependencies=["gate"],
                    ),
                ]
            )

        async def decide(self, context):
            return NodeDecision(action="COMPLETE")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'condition.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, ConditionalProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="branch safely"))
        result = await service.run_task(task.id)
        nodes = await service.repository.list_nodes(task.id)

        assert result.status is TaskStatus.SUCCEEDED
        assert next(node for node in nodes if node.description == "skipped branch").status is NodeStatus.CANCELLED
        assert next(node for node in nodes if node.description == "continue branch").status is NodeStatus.SUCCEEDED
        assert any(
            event.event_type == "CONDITION_EVALUATED"
            for event in await service.repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_condition_skip_preserves_valid_converging_branch(tmp_path):
    class ConvergingProvider(MockLLMProvider):
        async def plan(self, context):
            return PlanProposal(
                nodes=[
                    PlanNodeProposal(
                        id="gate",
                        description="evaluate gate",
                        type="CONDITION",
                        metadata={"value": True, "skip_on_true": ["optional"]},
                    ),
                    PlanNodeProposal(
                        id="optional", description="optional branch", dependencies=["gate"]
                    ),
                    PlanNodeProposal(
                        id="other", description="other branch", dependencies=["gate"]
                    ),
                    PlanNodeProposal(
                        id="merge",
                        description="merge results",
                        dependencies=["optional", "other"],
                    ),
                ]
            )

        async def decide(self, context):
            return NodeDecision(action="COMPLETE")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'converging.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, ConvergingProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="graph"))

        result = await service.run_task(task.id)
        nodes = {node.description: node for node in await service.repository.list_nodes(task.id)}

        assert result.status is TaskStatus.SUCCEEDED
        assert nodes["optional branch"].status is NodeStatus.CANCELLED
        assert nodes["other branch"].status is NodeStatus.SUCCEEDED
        assert nodes["merge results"].status is NodeStatus.SUCCEEDED
    await database.close()


@pytest.mark.asyncio
async def test_structural_verify_respects_failure_dependency(tmp_path):
    class FailureVerifyProvider(MockLLMProvider):
        async def plan(self, context):
            return PlanProposal(
                nodes=[
                    PlanNodeProposal(id="work", description="work"),
                    PlanNodeProposal(
                        id="verify",
                        description="verify failure path",
                        type="VERIFY",
                        dependencies=["work"],
                        dependency_types={"work": DependencyType.FAILURE},
                    ),
                ]
            )

        async def decide(self, context):
            if context["node"]["description"] == "work":
                return NodeDecision(action="OPERATION", operation=self.operation)
            raise AssertionError("structural VERIFY must not call resolver")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'verify-failure.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session,
            FailureVerifyProvider(Operation(tool="mock.always_fail", method="run")),
            ToolRegistry([MockTool("always_fail")]),
        )
        task = await service.create_task(TaskRequest(goal="verify"))
        task.budget.max_recovery_attempts = 0
        await service.repository.save_task(task)

        result = await service.run_task(task.id)
        nodes = {node.description: node for node in await service.repository.list_nodes(task.id)}

        assert result.status is TaskStatus.FAILED
        assert nodes["verify failure path"].status is NodeStatus.SUCCEEDED
    await database.close()


@pytest.mark.asyncio
async def test_step_limit_blocks_pending_nodes_explicitly(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'step-limit.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="step limit"))

        result = await service.run_task(task.id, max_steps=1)
        nodes = await service.repository.list_nodes(task.id)

        assert result.status is TaskStatus.BLOCKED
        assert all(node.status is not NodeStatus.READY for node in nodes)
        assert any(
            event.event_type == "TASK_STEP_LIMIT_EXCEEDED"
            for event in await service.repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_complex_graph_combines_condition_and_mixed_dependencies(tmp_path):
    class MixedGraphProvider(MockLLMProvider):
        async def plan(self, context):
            return PlanProposal(
                nodes=[
                    PlanNodeProposal(
                        id="gate",
                        description="evaluate gate",
                        type="CONDITION",
                        metadata={"value": True, "skip_on_true": ["optional"]},
                    ),
                    PlanNodeProposal(
                        id="work", description="run work", dependencies=["gate"]
                    ),
                    PlanNodeProposal(
                        id="optional",
                        description="optional work",
                        type="SUBTASK",
                        dependencies=["gate"],
                    ),
                    PlanNodeProposal(
                        id="cleanup",
                        description="always cleanup",
                        type="SUBTASK",
                        dependencies=["work"],
                        dependency_types={"work": DependencyType.ALWAYS},
                    ),
                    PlanNodeProposal(
                        id="failure-path",
                        description="handle failure",
                        type="SUBTASK",
                        dependencies=["work"],
                        dependency_types={"work": DependencyType.FAILURE},
                    ),
                ]
            )

        async def decide(self, context):
            if context["node"]["description"] == "run work":
                return NodeDecision(action="OPERATION", operation=self.operation)
            return NodeDecision(action="COMPLETE")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'complex-graph.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session,
            MixedGraphProvider(Operation(tool="mock.always_fail", method="run")),
            ToolRegistry([MockTool("always_fail")]),
        )
        task = await service.create_task(TaskRequest(goal="run mixed graph"))
        task.budget.max_recovery_attempts = 0
        await service.repository.save_task(task)

        result = await service.run_task(task.id)
        nodes = {node.description: node for node in await service.repository.list_nodes(task.id)}

        assert result.status is TaskStatus.FAILED
        assert nodes["optional work"].status is NodeStatus.CANCELLED
        assert nodes["always cleanup"].status is NodeStatus.SUCCEEDED
        assert nodes["handle failure"].status is NodeStatus.SUCCEEDED
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


@pytest.mark.asyncio
async def test_startup_registers_assistant_and_computer_projects_idempotently(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'builtin-projects.db'}")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'builtin-projects.db'}",
        persist_system_facts=False,
        persist_user_profile=False,
    )

    await StartupManager(database, settings, MockLLMProvider()).initialize()
    await StartupManager(database, settings, MockLLMProvider()).initialize()

    async with database.sessions() as session:
        projects = await TaskRepository(session).list_projects()
        assert {project.name for project in projects} == {"Assistant"}
        assert sum(project.is_default for project in projects) == 1
    await database.close()


@pytest.mark.asyncio
async def test_reset_state_deletes_runtime_state_but_keeps_projects(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'reset-memory.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        await repository.upsert_memory(kind="test", key="one", value={"value": 1})
        await repository.upsert_memory(kind="test", key="two", value={"value": 2})
        project = Project(name="keep", path=str(tmp_path))
        await repository.create_project(project)

        deleted = await repository.reset_state()
        assert deleted["memories"] == 2
        assert await repository.list_memory() == []
        assert len(await repository.list_projects()) == 1
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
    assert payload["options"] == {"temperature": 0.1, "num_ctx": 32768}
    assert payload["think"] is False
    assert "Valid example:" in prompt
    await provider.close()


@pytest.mark.asyncio
async def test_ollama_provider_uses_explicit_reasoning_and_context_budget():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"response": '{"task_id": null, "nodes": []}'})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaLLMProvider(
        "http://ollama.test",
        "test-model",
        client=client,
        num_ctx=8192,
        thinking=True,
        reasoning_effort="medium",
        context_reserve_tokens=1024,
        max_prompt_chars=200_000,
    )
    await provider.plan({"task": {"goal": "run tests"}})
    payload = json.loads(requests[0].content)

    assert payload["think"] == "medium"
    assert provider.effective_prompt_chars == (8192 - 1024) * 4
    await provider.close()


@pytest.mark.asyncio
async def test_ollama_provider_applies_reasoning_policy_per_phase():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"response": '{"decision": "SUCCESS", "reason": "ok"}'})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaLLMProvider(
        "http://ollama.test",
        "test-model",
        client=client,
        thinking=True,
        reasoning_policy="PLANNER:medium,NODE_RESOLVER:low,VERIFIER:off",
    )
    await provider.verify({"result": {"success": True}})

    assert requests[0]["think"] is False
    assert provider.reasoning_policy["PLANNER"] == "medium"
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
async def test_ollama_provider_exposes_prefill_and_generation_timing():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "response": '{"task_id": null, "nodes": []}',
                "prompt_eval_count": 1200,
                "eval_count": 240,
                "prompt_eval_duration": 2_000_000_000,
                "eval_duration": 3_000_000_000,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaLLMProvider("http://ollama.test", "test-model", client=client)
    await provider.plan({"task": {"goal": "timing"}})

    assert provider.last_usage["prompt_eval_duration"] == 2_000_000_000
    assert provider.last_usage["eval_duration"] == 3_000_000_000
    assert provider.last_usage["prompt_eval_count"] == 1200
    await provider.close()


@pytest.mark.asyncio
async def test_ollama_circuit_breaker_opens_after_repeated_failures():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaLLMProvider(
        "http://ollama.test",
        "test-model",
        client=client,
        failure_threshold=2,
        recovery_timeout=60,
    )

    with pytest.raises(httpx.HTTPStatusError):
        await provider.plan({"task": {"goal": "first"}})
    with pytest.raises(httpx.HTTPStatusError):
        await provider.plan({"task": {"goal": "second"}})
    assert provider.circuit_state == "OPEN"
    with pytest.raises(RuntimeError, match="circuit breaker is open"):
        await provider.plan({"task": {"goal": "blocked"}})
    await provider.close()


@pytest.mark.asyncio
async def test_tool_execution_renews_lease_while_operation_is_running(tmp_path):
    class SlowTool(Tool):
        definition = ToolDefinition(name="slow", description="slow", methods=["run"])

        async def execute(self, method, args, timeout):
            await asyncio.sleep(0.25)
            return OperationResult(success=True, output="done")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'lease-loop.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry([SlowTool()]))
        renewals = []

        async def renew(node_id, seconds):
            renewals.append((node_id, seconds))
            return True

        service.renew_lease = renew
        result, held = await service._execute_tool_with_lease(
            "node-1", Operation(tool="slow", method="run", timeout=0.5), 0.3
        )

        assert result.success is True
        assert held is True
        assert renewals
    await database.close()


@pytest.mark.asyncio
async def test_cancel_task_cooperatively_discards_active_tool_result(tmp_path):
    started = asyncio.Event()

    class CancellableTool(Tool):
        definition = ToolDefinition(name="cancellable", description="cancellable", methods=["run"])

        async def execute(self, method, args, timeout):
            started.set()
            await asyncio.sleep(0.2)
            return OperationResult(success=True, output={"status": "completed"})

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cancel-active.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session,
            MockLLMProvider(Operation(tool="cancellable", method="run")),
            ToolRegistry([CancellableTool()]),
        )
        task = await service.create_task(TaskRequest(goal="cancel active work"))
        running = asyncio.create_task(service.run_task(task.id))
        await started.wait()
        await service.cancel_task(task.id)
        result = await running
        node = (await service.repository.list_nodes(task.id))[-1]

        assert result.status is TaskStatus.CANCELLED
        assert node.status is NodeStatus.CANCELLED
        assert node.output_data["output"]["status"] == "completed"
    await database.close()


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
    assert planner_context["long_term_memory"][0]["key"] == "language"
    assert "relevant_memory" not in planner_context
    assert "available_tools" not in planner_context
    assert "relevant_memory" not in resolver_context
    assert "available_tools" not in resolver_context


@pytest.mark.asyncio
async def test_all_llm_contexts_match_their_role_templates_and_stay_bounded():
    class Repository:
        async def search_memory(self, query):
            return [MemoryRecord(kind="system", key="policy", value="data only")]

    task = Task(goal="inspect project")
    node = TaskNode(
        task_id=task.id,
        type=NodeType.OPERATION,
        description="run tests",
        status=NodeStatus.READY,
        metadata={"acceptance": {"exit_code": 0}},
    )
    graph = TaskGraph([node])
    operation = Operation(tool="shell", method="exec", args={"command": "pytest -q"})
    result = OperationResult(success=False, error="failed", output={"exit_code": 1})
    builder = ContextBuilder(Repository(), ToolRegistry())
    contexts = {
        "planner": await builder.for_planner(task),
        "node_resolver": await builder.for_resolver(task, node, graph),
        "replanner": await builder.for_replanner(task, node, graph, result),
        "verifier": await builder.for_verifier(task, node, operation, result),
        "final_response": await builder.for_final_response(
            task,
            [
                type("Event", (), {"event_type": "TOOL_RESULT", "payload": {"output": "x" * 10000}})()
            ],
        ),
    }

    for role, context in contexts.items():
        template = (Path(__file__).parents[1] / "assistant" / "prompts" / "v1" / f"{role}.md").read_text()
        rendered = render(template, context, {})
        assert "{{" not in rendered
        assert len(rendered) < 15000

    assert "project" not in contexts["planner"]
    assert "system_graph" not in contexts["node_resolver"]
    assert "graph" not in contexts["replanner"]
    assert "failed_node" in contexts["replanner"]["failure_context"]
    assert len(contexts["final_response"]["events"][0]["payload"]["output"]) == 2000 + len("... [truncated]")


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
    assert runtime.metrics_snapshot() == {
        "passes": 1,
        "tasks_dispatched": 0,
        "task_errors": 0,
        "idle_passes": 1,
        "idle_skipped": 0,
        "not_ready_passes": 0,
        "runtime_errors": 0,
    }


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
    assert runtime.metrics_snapshot()["not_ready_passes"] == 1


@pytest.mark.asyncio
async def test_runtime_can_disable_idle_without_stopping_task_dispatch():
    task = Task(goal="work", status=TaskStatus.QUEUED)
    called = []
    idle_calls = []

    async def on_idle():
        idle_calls.append(True)

    class Repository:
        async def list_tasks(self):
            return [task]

    runtime = TaskRuntime(
        Repository(),
        lambda task_id: _record_task(called, task_id),
        idle_cycle=IdleCycle(on_idle=on_idle),
    )
    runtime.set_idle_enabled(False)

    assert await runtime.run_once() == 1
    assert called == [task.id]
    assert idle_calls == []
    assert runtime.metrics_snapshot()["idle_skipped"] == 1


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
async def test_final_response_consumes_llm_budget_without_changing_terminal_status(tmp_path):
    class CountingProvider(MockLLMProvider):
        def __init__(self):
            super().__init__(Operation(tool="mock.success", method="run"))
            self.responses = 0

        async def respond(self, context):
            self.responses += 1
            return await super().respond(context)

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'final-response-budget.db'}")
    await database.create_all()
    async with database.sessions() as session:
        provider = CountingProvider()
        service = TaskService(session, provider, ToolRegistry([MockTool("success")]))
        task = await service.create_task(TaskRequest(goal="count final response"))
        task.budget.max_llm_calls = 3
        await service.repository.save_task(task)

        result = await service.run_task(task.id)

        assert result.status is TaskStatus.SUCCEEDED
        assert result.metadata["llm_calls"] == 3
        assert provider.responses == 1
        assert result.metadata["final_response"]
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
async def test_lease_can_be_renewed_by_its_owner(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'renew-lease.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="renew lease"))
        node = (await service.repository.list_nodes(task.id))[0]
        assert await service.acquire_lease(node.id, seconds=1) is True
        before = (await session.get(LeaseRow, node.id)).expires_at
        assert await service.renew_lease(node.id, seconds=120) is True
        after = (await session.get(LeaseRow, node.id)).expires_at
        assert after > before
    await database.close()


@pytest.mark.asyncio
async def test_active_lease_cannot_be_taken_by_another_worker(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'lease-contention.db'}")
    await database.create_all()
    async with database.sessions() as first_session, database.sessions() as second_session:
        first = TaskService(first_session, MockLLMProvider(), ToolRegistry())
        second = TaskService(second_session, MockLLMProvider(), ToolRegistry())
        task = await first.create_task(TaskRequest(goal="lease contention"))
        node = (await first.repository.list_nodes(task.id))[0]

        assert await first.acquire_lease(node.id, seconds=120) is True
        assert await second.acquire_lease(node.id, seconds=120) is False
    await database.close()


@pytest.mark.asyncio
async def test_deployment_requires_explicit_project_command(tmp_path):
    registry = build_tool_registry()
    invalid = await registry.execute(
        Operation(tool="deployment", method="deploy", args={"cwd": str(tmp_path)})
    )
    assert invalid.success is False
    assert invalid.error_type is ErrorType.INVALID_ARGUMENT


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

        refreshed = await service.refresh_project_codegraph(project.id)
        persisted_project = await service.get_project(project.id)
        assert refreshed.codegraph_version == 1
        assert persisted_project.codegraph["root"] == str(project_path.resolve())

        assert task.project_id == project.id
        result = await service.run_task(task.id)
        node = (await service.repository.list_nodes(task.id))[-1]
        assert result.status is TaskStatus.SUCCEEDED
        assert node.output_data["output"]["root"] == str(project_path.resolve())
    await database.close()


@pytest.mark.asyncio
async def test_ambiguous_project_selection_waits_for_explicit_input(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'ambiguous-project.db'}")
    await database.create_all()
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        first = await service.create_project(Project(name="first", path=str(first_path)))
        await service.create_project(Project(name="second", path=str(second_path)))

        task = await service.create_task(TaskRequest(goal="audit a project"))

        assert task.status is TaskStatus.WAITING
        with pytest.raises(ValueError, match="project selection"):
            await service.resume_task(task.id)

        resumed = await service.submit_task_input(
            task.id, {"project_id": first.id}
        )
        assert resumed.status is TaskStatus.QUEUED
        assert resumed.project_id == first.id
        assert "clarification" not in resumed.metadata

        result = await service.run_task(task.id)
        assert result.status is TaskStatus.SUCCEEDED
        assert any(
            node.description == "execute configured mock operation"
            for node in await service.repository.list_nodes(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_planner_acceptance_evidence_is_persisted_and_checked(tmp_path):
    class AcceptanceProvider(MockLLMProvider):
        async def plan(self, context):
            return PlanProposal(
                nodes=[
                    PlanNodeProposal(
                        id="checked",
                        description="checked operation",
                        acceptance={"contains": ["ok"]},
                    )
                ]
            )

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'acceptance.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session,
            AcceptanceProvider(Operation(tool="mock.success", method="run", args={"output": "ok"})),
            ToolRegistry([MockTool("success")]),
        )
        task = await service.create_task(TaskRequest(goal="check evidence"))

        result = await service.run_task(task.id)
        nodes = await service.repository.list_nodes(task.id)

        assert result.status is TaskStatus.SUCCEEDED
        checked = next(node for node in nodes if node.description == "checked operation")
        assert checked.metadata["acceptance"] == {"contains": ["ok"]}
        assert "checked operation" in result.result_summary
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
async def test_recovery_requeues_parent_task_and_running_node(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'recovery-running.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        task = Task(goal="recover running", status=TaskStatus.RUNNING)
        node = TaskNode(
            task_id=task.id,
            type=NodeType.OPERATION,
            description="interrupted operation",
            status=NodeStatus.RUNNING,
        )
        await repository.save_task(task)
        await repository.save_node(node)

        recovered = await RecoveryManager(session).recover()

        assert recovered == 1
        assert (await repository.get_task(task.id)).status is TaskStatus.READY
        assert (await repository.get_node(node.id)).status is NodeStatus.READY
        assert (await repository.get_node(node.id)).error == "recovered after process restart"
        assert any(
            event.event_type == "NODE_RECOVERED"
            for event in await repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_recovery_requeues_interrupted_verification(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'recovery-verifying.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        task = Task(goal="recover verification", status=TaskStatus.VERIFYING)
        node = TaskNode(
            task_id=task.id,
            type=NodeType.OPERATION,
            description="interrupted verification",
            status=NodeStatus.VERIFYING,
        )
        await repository.save_task(task)
        await repository.save_node(node)

        recovered = await RecoveryManager(session).recover()

        assert recovered == 1
        assert (await repository.get_task(task.id)).status is TaskStatus.READY
        assert (await repository.get_node(node.id)).status is NodeStatus.READY
    await database.close()


@pytest.mark.asyncio
async def test_direct_answer_persists_exact_user_facing_response(tmp_path):
    class DirectAnswerProvider(MockLLMProvider):
        async def plan(self, context):
            return PlanProposal(answer="4")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'direct-response.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, DirectAnswerProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="cuanto es 2 + 2"))

        result = await service.run_task(task.id)

        assert result.status is TaskStatus.SUCCEEDED
        assert result.result_summary == "4"
        assert result.metadata["final_response"]["summary"] == "4"
    await database.close()


@pytest.mark.asyncio
async def test_non_retryable_operation_failure_is_terminal_failed(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'failed-operation.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session,
            MockLLMProvider(Operation(tool="mock.always_fail", method="run")),
            ToolRegistry([MockTool("always_fail")]),
        )
        task = await service.create_task(TaskRequest(goal="run failing operation"))

        result = await service.run_task(task.id)

        assert result.status is TaskStatus.FAILED
        assert (await service.repository.list_nodes(task.id))[-1].status is NodeStatus.FAILED
    await database.close()


@pytest.mark.asyncio
async def test_large_plan_is_decomposed_before_persisting_all_nodes(tmp_path):
    class LargePlanProvider(MockLLMProvider):
        async def plan(self, context):
            return PlanProposal(
                nodes=[
                    PlanNodeProposal(id=f"step-{index}", description=f"step {index}")
                    for index in range(101)
                ],
                subtasks=["inspect the first half", "inspect the second half"],
            )

        async def decide(self, context):
            return NodeDecision(action="COMPLETE", reason="subtask complete")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'large-plan.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, LargePlanProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="large plan"))

        result = await service.run_task(task.id)
        nodes = await service.repository.list_nodes(task.id)

        assert result.status is TaskStatus.SUCCEEDED
        assert {node.description for node in nodes} >= {
            "inspect the first half",
            "inspect the second half",
        }
        assert len(nodes) == 3
        assert any(
            event.event_type == "PLAN_DECOMPOSED"
            for event in await service.repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_planner_preserves_typed_dependency_edges(tmp_path):
    class TypedPlanProvider(MockLLMProvider):
        async def plan(self, context):
            return PlanProposal(
                nodes=[
                    PlanNodeProposal(id="first", description="first"),
                    PlanNodeProposal(
                        id="cleanup",
                        description="cleanup",
                        dependencies=["first"],
                        dependency_types={"first": DependencyType.ALWAYS},
                    ),
                ]
            )

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'typed-edges.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, TypedPlanProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="typed edges"))

        await service.execute_once(task.id)
        edges = await service.repository.list_edges(task.id)

        assert len(edges) == 1
        assert edges[0].dependency_type is DependencyType.ALWAYS
    await database.close()


@pytest.mark.asyncio
async def test_failure_and_always_dependencies_run_before_terminal_failure(tmp_path):
    class CleanupProvider(MockLLMProvider):
        async def plan(self, context):
            return PlanProposal(
                nodes=[
                    PlanNodeProposal(id="work", description="work", type="OPERATION"),
                    PlanNodeProposal(
                        id="cleanup",
                        description="cleanup",
                        type="OPERATION",
                        dependencies=["work"],
                        dependency_types={"work": DependencyType.ALWAYS},
                    ),
                ]
            )

        async def decide(self, context):
            if context["node"]["description"] == "cleanup":
                return NodeDecision(action="COMPLETE")
            return NodeDecision(action="OPERATION", operation=self.operation)

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cleanup.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session,
            CleanupProvider(Operation(tool="mock.always_fail", method="run")),
            ToolRegistry([MockTool("always_fail")]),
        )
        task = await service.create_task(TaskRequest(goal="run cleanup after failure"))

        result = await service.run_task(task.id)
        nodes = await service.repository.list_nodes(task.id)

        assert result.status is TaskStatus.FAILED
        assert next(node for node in nodes if node.description == "work").status is NodeStatus.FAILED
        assert next(node for node in nodes if node.description == "cleanup").status is NodeStatus.SUCCEEDED
    await database.close()


@pytest.mark.asyncio
async def test_failed_node_can_run_fix_branch_then_resume_original_node(tmp_path):
    class FixProvider(MockLLMProvider):
        async def plan(self, context):
            return PlanProposal(
                nodes=[PlanNodeProposal(id="work", description="work", type="OPERATION")]
            )

        async def decide(self, context):
            if context["node"]["description"] == "apply correction":
                return NodeDecision(action="COMPLETE")
            return NodeDecision(action="OPERATION", operation=self.operation)

        async def replan(self, context):
            return NodeDecision(
                action="FIX",
                subtasks=["apply correction"],
                reason="the failed operation needs a repair before retry",
            )

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'fix-recovery.db'}")
    await database.create_all()
    async with database.sessions() as session:
        provider = FixProvider(Operation(tool="mock.fail_once", method="run"))
        service = TaskService(session, provider, ToolRegistry([MockTool("fail_once")]))
        task = await service.create_task(TaskRequest(goal="repair and retry"))
        task.budget.max_retries = 0
        await service.repository.save_task(task)

        result = await service.run_task(task.id)
        nodes = await service.repository.list_nodes(task.id)
        events = await service.repository.list_events(task.id)

        assert result.status is TaskStatus.SUCCEEDED
        assert {node.description for node in nodes} >= {"work", "apply correction"}
        assert next(node for node in nodes if node.description == "work").status is NodeStatus.SUCCEEDED
        assert any(event.event_type == "RECOVERY_FIX_BRANCH_CREATED" for event in events)
        assert any(event.event_type == "RECOVERY_TARGET_REQUEUED" for event in events)
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
async def test_non_idempotent_retryable_failure_is_not_replayed_without_key(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'non-idempotent-retry.db'}")
    await database.create_all()
    async with database.sessions() as session:
        tool = MockTool("fail_once")
        tool.definition.idempotent = False
        service = TaskService(
            session,
            MockLLMProvider(Operation(tool="mock.fail_once", method="run")),
            ToolRegistry([tool]),
        )
        task = await service.create_task(TaskRequest(goal="do not duplicate effect"))

        result = await service.run_task(task.id)

        assert result.status is TaskStatus.FAILED
        assert tool.calls == 1
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
async def test_replan_failure_blocks_with_explicit_event(tmp_path):
    class ReplanVerifier:
        def verify(self, result):
            from assistant.domain.models import VerificationDecision
            from assistant.llm import VerificationResult
            return VerificationResult(decision=VerificationDecision.REPLAN, reason="replan needed")

    class FailingReplanProvider(MockLLMProvider):
        async def replan(self, context):
            raise ValueError("replanner unavailable")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'replan-failure.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session,
            FailingReplanProvider(Operation(tool="mock.always_fail", method="run")),
            ToolRegistry([MockTool("always_fail")]),
            verifier=ReplanVerifier(),
        )
        task = await service.create_task(TaskRequest(goal="failed replan"))

        result = await service.run_task(task.id)

        assert result.status is TaskStatus.BLOCKED
        assert any(
            event.event_type == "REPLAN_FAILED"
            for event in await service.repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_recovery_budget_exhaustion_leaves_failed_node_terminal(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'recovery-budget.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session,
            MockLLMProvider(Operation(tool="mock.always_fail", method="run")),
            ToolRegistry([MockTool("always_fail")]),
        )
        task = await service.create_task(TaskRequest(goal="exhaust recovery"))
        task.budget.max_recovery_attempts = 0
        await service.repository.save_task(task)

        result = await service.run_task(task.id)
        events = await service.repository.list_events(task.id)

        assert result.status is TaskStatus.FAILED
        assert not any(event.event_type == "RECOVERY_ANALYZED" for event in events)
        assert (await service.repository.list_nodes(task.id))[-1].status is NodeStatus.FAILED
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
async def test_resume_requires_exactly_one_waiting_node(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'multiple-waits.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="multiple waits"))
        first = (await service.repository.list_nodes(task.id))[0]
        second = TaskNode(
            task_id=task.id,
            type=NodeType.WAIT,
            description="second wait",
            status=NodeStatus.WAITING,
        )
        first.status = NodeStatus.WAITING
        task.status = TaskStatus.WAITING
        await service.repository.save_node(first)
        await service.repository.save_node(second)
        await service.repository.save_task(task)

        with pytest.raises(ValueError, match="exactly one waiting node"):
            await service.resume_task(task.id)
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
        assert result.failure_reason == "LLM returned no operation"
        assert any(
            event.event_type == "NODE_BLOCKED"
            for event in await service.repository.list_events(task.id)
        )
    await database.close()


@pytest.mark.asyncio
async def test_verifier_block_is_persisted_as_blocked_task(tmp_path):
    class BlockingVerifier:
        def verify(self, result):
            return VerificationResult(
                decision=VerificationDecision.BLOCK,
                reason="manual review is required",
            )

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'verifier-block.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session,
            MockLLMProvider(Operation(tool="mock.success", method="run")),
            ToolRegistry([MockTool("success")]),
            verifier=BlockingVerifier(),
        )
        task = await service.create_task(TaskRequest(goal="block after verification"))

        result = await service.run_task(task.id)
        node = (await service.repository.list_nodes(task.id))[-1]
        events = await service.repository.list_events(task.id)

        assert result.status is TaskStatus.BLOCKED
        assert result.failure_reason == "manual review is required"
        assert node.status is NodeStatus.BLOCKED
        assert any(event.event_type == "NODE_BLOCKED" for event in events)
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
async def test_notify_node_is_no_longer_treated_as_unsupported_structural_node(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'notify-node-compat.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(session, MockLLMProvider(), ToolRegistry())
        task = await service.create_task(TaskRequest(goal="notify from node"))
        root = (await service.repository.list_nodes(task.id))[0]
        root.status = NodeStatus.SUCCEEDED
        node = TaskNode(task_id=task.id, type=NodeType.NOTIFY, description="notify", status=NodeStatus.READY)
        await service.repository.save_node(root)
        await service.repository.save_node(node)
        task.status = TaskStatus.READY
        await service.repository.save_task(task)

        result = await service.run_task(task.id)

        assert result.status is TaskStatus.SUCCEEDED
        assert (await service.repository.get_node(node.id)).status is NodeStatus.SUCCEEDED
    await database.close()
