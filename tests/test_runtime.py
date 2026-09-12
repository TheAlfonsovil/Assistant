import json

import httpx
import pytest

from assistant.application import TaskService
from assistant.context import ContextBuilder
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
from assistant.llm import MockLLMProvider, OllamaLLMProvider, PlanProposal
from assistant.project_analysis import ProjectAnalyzer
from assistant.runtime import TaskRuntime
from assistant.tools import MockTool, ToolRegistry


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
