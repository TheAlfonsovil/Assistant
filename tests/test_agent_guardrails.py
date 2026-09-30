"""Agent-mode guardrails: artifacts, deterministic verification, idempotency.

The incremental agent path used to trust every tool result and re-execute
side-effecting operations after a replay. These tests pin the three guardrails
that close that gap.
"""

from __future__ import annotations

import pytest

from assistant.application import TaskService
from assistant.devices.computer.actions.filesystem import FilesystemTool
from assistant.domain.models import (
    AgentDecision,
    ErrorType,
    Operation,
    OperationResult,
    Project,
    TaskRequest,
    TaskStatus,
)
from assistant.infrastructure.db import Database
from assistant.llm import MockLLMProvider
from assistant.tools import Tool, ToolDefinition, ToolRegistry
from assistant.verifier import DeterministicVerifier


@pytest.mark.asyncio
async def test_filesystem_write_publishes_a_stable_artifact(tmp_path):
    tool = FilesystemTool()
    target = tmp_path / "out.txt"

    first = await tool.execute("write", {"path": str(target), "content": "hola"}, 10)
    second = await tool.execute("write", {"path": str(target), "content": "hola"}, 10)

    assert first.success is True
    assert len(first.artifacts) == 1
    assert first.artifacts[0]["kind"] == "file"
    assert first.artifacts[0]["path"] == str(target)
    assert first.artifacts[0]["checksum"] == second.artifacts[0]["checksum"]
    # A replay must not create a second ledger row for the same path.
    assert first.artifacts[0]["id"] == second.artifacts[0]["id"]

    read = await tool.execute("read", {"path": str(target)}, 10)
    assert read.artifacts == []


@pytest.mark.asyncio
async def test_agent_write_is_published_to_the_artifact_ledger(tmp_path):
    class WritingAgent(MockLLMProvider):
        def __init__(self, target: str):
            super().__init__()
            self.target = target

        async def agent_decide(self, context):
            if context["last_observation"] is None:
                return AgentDecision(
                    decision_type="EXECUTE",
                    reason="write the requested report",
                    operation=Operation(
                        tool="filesystem",
                        method="write",
                        args={"path": self.target, "content": "# Report\n"},
                    ),
                )
            return AgentDecision(decision_type="COMPLETE", reason="report written")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'agent-artifacts.db'}")
    await database.create_all()
    async with database.sessions() as session:
        target = tmp_path / "artifacts" / "report.md"
        service = TaskService(
            session, WritingAgent(str(target)), ToolRegistry(), workspace_root=str(tmp_path)
        )
        task = await service.create_task(TaskRequest(goal="escribe el informe"))

        result = await service.run_task(task.id)
        events = await service.repository.list_events(task.id)
        artifacts = await service.repository.list_artifacts(task.id)

        assert result.status is TaskStatus.SUCCEEDED
        assert [artifact.path for artifact in artifacts] == [str(target)]
        verified = [event for event in events if event.event_type == "NODE_VERIFIED"]
        assert verified and verified[0].payload["source"] == "agent"
        assert verified[0].payload["decision"] == "SUCCESS"


@pytest.mark.asyncio
async def test_agent_observation_carries_the_verifier_verdict(tmp_path):
    """A hollow success must reach the worker as a RETRY verdict."""

    class HollowTool(Tool):
        definition = ToolDefinition(
            name="hollow",
            description="Succeeds without producing the declared evidence",
            methods=["run"],
            argument_schema={"path": {"type": "string", "required": True}},
        )

        async def execute(self, method, args, timeout):
            return OperationResult(
                success=True,
                output={"note": "no report here"},
                metadata={"expected": {"fields": {"report": "present"}}},
            )

    class HollowAgent(MockLLMProvider):
        async def agent_decide(self, context):
            if context["last_observation"] is None:
                return AgentDecision(
                    decision_type="EXECUTE",
                    reason="produce the report",
                    operation=Operation(tool="hollow", method="run", args={"path": "x"}),
                )
            return AgentDecision(decision_type="COMPLETE", reason="assumed done")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'agent-verify.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session, HollowAgent(), ToolRegistry([HollowTool()]), workspace_root=str(tmp_path)
        )
        task = await service.create_task(TaskRequest(goal="genera el informe"))

        await service.run_task(task.id)
        events = await service.repository.list_events(task.id)

        observation = next(
            event for event in events if event.event_type == "AGENT_OBSERVATION"
        )
        assert observation.payload["verification"]["decision"] == "RETRY"
        assert "acceptance evidence" in observation.payload["verification"]["reason"]
        verified = [event for event in events if event.event_type == "NODE_VERIFIED"]
        assert verified[0].payload["decision"] == "RETRY"


@pytest.mark.asyncio
async def test_agent_does_not_repeat_a_side_effecting_operation(tmp_path):
    class CountingTool(Tool):
        definition = ToolDefinition(
            name="counter",
            description="Side-effecting operation that must run at most once",
            methods=["run"],
            argument_schema={"path": {"type": "string", "required": True}},
            idempotent=False,
        )

        def __init__(self):
            self.executions = 0

        async def execute(self, method, args, timeout):
            self.executions += 1
            return OperationResult(success=True, output={"executions": self.executions})

    tool = CountingTool()

    class RepeatingAgent(MockLLMProvider):
        def __init__(self):
            super().__init__()
            self.turns = 0

        async def agent_decide(self, context):
            self.turns += 1
            if self.turns <= 2:
                return AgentDecision(
                    decision_type="EXECUTE",
                    reason="perform the side effect",
                    operation=Operation(tool="counter", method="run", args={"path": "marker"}),
                )
            return AgentDecision(decision_type="COMPLETE", reason="done")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'agent-idempotency.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session, RepeatingAgent(), ToolRegistry([tool]), workspace_root=str(tmp_path)
        )
        task = await service.create_task(TaskRequest(goal="ejecuta el efecto una vez"))

        await service.run_task(task.id)
        events = await service.repository.list_events(task.id)

        assert tool.executions == 1
        replays = [event for event in events if event.event_type == "OPERATION_IDEMPOTENT_REPLAY"]
        assert len(replays) == 1
        assert replays[0].payload["tool"] == "counter"
        assert [event.event_type for event in events].count("TOOL_CALLED") == 2


@pytest.mark.asyncio
async def test_tool_evidence_only_attaches_fields_the_tool_really_returned():
    """Declared success fields are evidence, never an invented expectation."""

    verifier = DeterministicVerifier()
    present = OperationResult(
        success=True, output={"root": "/tmp/x", "audit_report": {"findings": []}}
    )
    attached = DeterministicVerifier.with_tool_evidence(
        present, {"success_fields": ["root", "audit_report"]}
    )

    assert attached.metadata["expected"]["fields"] == {
        "root": "/tmp/x",
        "audit_report": {"findings": []},
    }
    assert verifier.verify(attached).decision.value == "SUCCESS"

    # A field the tool did not return must not become a phantom requirement.
    hollow = OperationResult(success=True, output={"root": "/tmp/x"})
    missing = DeterministicVerifier.with_tool_evidence(
        hollow, {"success_fields": ["audit_report"]}
    )

    assert missing.metadata.get("expected", {}).get("fields", {}) == {}
    assert verifier.verify(missing).decision.value == "SUCCESS"

    # A declared expectation that the output does not satisfy is a retry.
    unmet = OperationResult(
        success=True,
        output={"root": "/tmp/x"},
        metadata={"expected": {"fields": {"audit_report": {"findings": []}}}},
    )
    decision = verifier.verify(unmet).decision.value
    assert decision == "RETRY"


@pytest.mark.asyncio
async def test_agent_blocks_unknown_tools_instead_of_failing_silently(tmp_path):
    class UnknownToolAgent(MockLLMProvider):
        async def agent_decide(self, context):
            return AgentDecision(
                decision_type="EXECUTE",
                reason="call something that does not exist",
                operation=Operation(tool="nope", method="run", args={}),
            )

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'unknown-tool.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session, UnknownToolAgent(), ToolRegistry(), workspace_root=str(tmp_path)
        )
        task = await service.create_task(TaskRequest(goal="llama a una herramienta inexistente"))

        result = await service.run_task(task.id)

        assert result.status is TaskStatus.BLOCKED
        assert "unknown tool" in (result.failure_reason or "")


@pytest.mark.asyncio
async def test_agent_project_work_still_resolves_the_project_root(tmp_path):
    """Regression: L2 changes must not disturb project-targeted agent turns."""

    class ProjectAgent(MockLLMProvider):
        async def agent_decide(self, context):
            if context["last_observation"] is None:
                return AgentDecision(
                    decision_type="EXECUTE",
                    reason="read the project readme",
                    operation=Operation(tool="project", method="read", args={"files": ["README.md"]}),
                )
            return AgentDecision(decision_type="COMPLETE", reason="read")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'agent-project.db'}")
    await database.create_all()
    async with database.sessions() as session:
        project_path = tmp_path / "proj"
        project_path.mkdir()
        (project_path / "README.md").write_text("# Project\n", encoding="utf-8")
        service = TaskService(session, ProjectAgent(), ToolRegistry(), workspace_root=str(tmp_path))
        project = await service.create_project(Project(name="proj", path=str(project_path)))
        task = await service.create_task(
            TaskRequest(goal="lee el readme del proyecto", project_id=project.id)
        )

        result = await service.run_task(task.id)
        events = await service.repository.list_events(task.id)

        assert result.status is TaskStatus.SUCCEEDED
        tools = [event.payload for event in events if event.event_type == "TOOL_RESULT"]
        assert tools and tools[0]["success"] is True
