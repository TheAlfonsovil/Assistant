import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from assistant.context import ContextBuilder
from assistant.domain.contracts import (
    AcceptanceCriterion,
    ArtifactKind,
    ArtifactRef,
    CriterionStatus,
    InputRef,
    NodeContract,
    NodeRuntimeState,
    OutputSpec,
    RetryPolicy,
    SoftConstraint,
    TaskContract,
    TaskDeliverable,
)
from assistant.domain.errors import GraphCycleError
from assistant.domain.graph import TaskGraph
from assistant.domain.models import (
    DependencyType,
    ErrorType,
    GraphEdge,
    MemoryRecord,
    NodeStatus,
    NodeType,
    OperationResult,
    RecoveryExpansion,
    Task,
    TaskEvent,
    TaskNode,
    TaskPriority,
    TaskRequest,
    TaskStatus,
)
from assistant.domain.state import InvalidStateTransition
from assistant.infrastructure.db import Database
from assistant.infrastructure.repositories import TaskRepository
from assistant.llm import PlanNodeProposal
from assistant.project_analysis import ProjectAnalyzer
from assistant.scheduler import NodeScheduler
from assistant.tools import ToolRegistry


def test_graph_resolves_dependencies_and_rejects_cycles():
    first = TaskNode(
        task_id="task", description="first", type=NodeType.OPERATION, status=NodeStatus.READY
    )
    second = TaskNode(
        task_id="task", description="second", type=NodeType.OPERATION, status=NodeStatus.READY
    )
    graph = TaskGraph([first, second], [GraphEdge(from_node=first.id, to_node=second.id)])

    assert [node.id for node in graph.ready_nodes()] == [first.id]
    first.status = NodeStatus.SUCCEEDED
    assert [node.id for node in graph.ready_nodes()] == [second.id]

    with pytest.raises(GraphCycleError):
        TaskGraph(
            [first, second],
            [
                GraphEdge(from_node=first.id, to_node=second.id),
                GraphEdge(from_node=second.id, to_node=first.id),
            ],
        )


def test_task_priority_labels_default_to_medium_and_normalize_to_scheduler_values():
    assert TaskRequest(goal="default").priority == 1
    assert TaskRequest(goal="explicit default", priority=None).priority == 1
    assert TaskRequest(goal="urgent", priority=TaskPriority.INMEDIATE).priority == 3
    assert TaskRequest(goal="important", priority="High").priority == 2
    assert TaskRequest(goal="later", priority="Low").priority == 0


def test_graph_allows_success_dependency_for_skipped_branch():
    skipped = TaskNode(
        task_id="task",
        description="skipped branch",
        type=NodeType.OPERATION,
        status=NodeStatus.CANCELLED,
        runtime=NodeRuntimeState(branch_skipped=True),
    )
    dependent = TaskNode(
        task_id="task",
        description="dependent",
        type=NodeType.OPERATION,
        status=NodeStatus.READY,
    )
    graph = TaskGraph(
        [skipped, dependent],
        [GraphEdge(from_node=skipped.id, to_node=dependent.id)],
    )

    assert graph.dependencies_satisfied(dependent.id) is True


def test_graph_always_dependency_waits_for_terminal_status():
    upstream = TaskNode(task_id="task", description="upstream", status=NodeStatus.READY)
    cleanup = TaskNode(task_id="task", description="cleanup", status=NodeStatus.READY)
    graph = TaskGraph(
        [upstream, cleanup],
        [
            GraphEdge(
                from_node=upstream.id,
                to_node=cleanup.id,
                dependency_type=DependencyType.ALWAYS,
            )
        ],
    )

    assert graph.dependencies_satisfied(cleanup.id) is False
    upstream.status = NodeStatus.BLOCKED
    assert graph.dependencies_satisfied(cleanup.id) is True


def test_task_contract_models_deliverables_and_evidence():
    contract = TaskContract(
        objective="Create a local calculator",
        deliverables=[
            TaskDeliverable(kind=ArtifactKind.FILE, description="calculator source")
        ],
        acceptance_criteria=[
            AcceptanceCriterion(id="AC-1", description="The calculator runs")
        ],
    )
    artifact = ArtifactRef(
        kind=ArtifactKind.TEST_RESULT,
        description="smoke test",
        producer_node_id="node-1",
    )

    assert contract.acceptance_criteria[0].id == "AC-1"
    assert artifact.kind is ArtifactKind.TEST_RESULT
    assert CriterionStatus.UNKNOWN.value == "UNKNOWN"


def test_plan_node_contract_declares_inputs_outputs_and_policies():
    node = PlanNodeProposal(
        id="implement",
        description="Implement the requested change",
        inputs=[InputRef(kind="requirement", ref="artifact:requirement-id")],
        outputs=[
            OutputSpec(kind=ArtifactKind.FILE, name="source", description="Changed source")
        ],
        acceptance_criteria=["AC-1"],
        allowed_tools=["project"],
        retry_policy=RetryPolicy(max_attempts=2, retry_on=["validation_failure"]),
    )

    assert node.inputs[0].ref == "artifact:requirement-id"
    assert node.outputs[0].kind is ArtifactKind.FILE
    assert node.retry_policy.max_attempts == 2


def test_retry_policy_controls_node_attempts_and_backoff():
    node = TaskNode(
        task_id="task",
        description="retryable work",
        retry_count=1,
        contract=NodeContract(
            retry_policy=RetryPolicy(
                max_attempts=5,
                backoff_seconds=1.5,
                max_backoff_seconds=4,
            )
        ),
    )

    assert RetryPolicy(max_attempts=5).max_retries == 4
    assert NodeScheduler.retry_delay(node) == 3.0


def test_retry_policy_can_limit_failure_types():
    node = TaskNode(
        task_id="task",
        description="retryable work",
        contract=NodeContract(retry_policy=RetryPolicy(retry_on=["TIMEOUT"])),
    )

    assert NodeScheduler.retry_allowed(
        node,
        OperationResult(success=False, error_type=ErrorType.TIMEOUT, retryable=True),
    )
    assert not NodeScheduler.retry_allowed(
        node,
        OperationResult(success=False, error_type=ErrorType.TOOL_FAILURE, retryable=True),
    )


@pytest.mark.asyncio
async def test_sqlite_persists_task_nodes_edges_and_events(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'nested' / 'assistant.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        task = Task(goal="persist me")
        node = TaskNode(task_id=task.id, description="root", status=NodeStatus.READY)
        await repository.save_task(task)
        await repository.save_node(node)
        await repository.save_edge(
            task.id,
            GraphEdge(from_node=node.id, to_node=node.id, dependency_type=DependencyType.ALWAYS),
        ) if False else None
        assert (await repository.get_task(task.id)).goal == "persist me"
        assert (await repository.get_node(node.id)).description == "root"
    await database.close()


@pytest.mark.asyncio
async def test_database_rejects_incompatible_schema_version(tmp_path):
    path = tmp_path / "old-schema.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_version (
            version INTEGER NOT NULL,
            applied_at DATETIME NOT NULL
        );
        INSERT INTO schema_version(version, applied_at)
        VALUES (4, CURRENT_TIMESTAMP);
        """
    )
    connection.commit()
    connection.close()

    database = Database(f"sqlite:///{path}")
    with pytest.raises(RuntimeError, match="found schema 4"):
        await database.create_all()
    await database.close()


@pytest.mark.asyncio
async def test_database_rejects_legacy_task_columns(tmp_path):
    path = tmp_path / "legacy-schema.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_version (
            version INTEGER NOT NULL,
            applied_at DATETIME NOT NULL
        );
        CREATE TABLE tasks (
            id VARCHAR(36) PRIMARY KEY,
            metadata_json JSON
        );
        """
    )
    connection.commit()
    connection.close()

    database = Database(f"sqlite:///{path}")
    with pytest.raises(RuntimeError, match="legacy"):
        await database.create_all()
    await database.close()


@pytest.mark.asyncio
async def test_database_migrates_schema_one_to_schema_five(tmp_path):
    path = tmp_path / "schema-one.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_version (
            version INTEGER NOT NULL,
            applied_at DATETIME NOT NULL
        );
        INSERT INTO schema_version(version, applied_at)
        VALUES (1, CURRENT_TIMESTAMP);
        CREATE TABLE tasks (
            id VARCHAR(36) PRIMARY KEY,
            parent_task_id VARCHAR(36),
            root_task_id VARCHAR(36) NOT NULL,
            source VARCHAR(32) NOT NULL,
            goal TEXT NOT NULL,
            description TEXT NOT NULL,
            status VARCHAR(32) NOT NULL,
            priority INTEGER NOT NULL,
            created_at DATETIME NOT NULL,
            started_at DATETIME,
            finished_at DATETIME,
            deadline DATETIME,
            retry_count INTEGER NOT NULL,
            max_retries INTEGER NOT NULL,
            metadata_json JSON NOT NULL,
            result_summary TEXT,
            failure_reason TEXT,
            budget_json JSON NOT NULL,
            project_id VARCHAR(36)
        );
        CREATE TABLE task_nodes (
            id VARCHAR(36) PRIMARY KEY,
            task_id VARCHAR(36) NOT NULL,
            parent_node_id VARCHAR(36),
            type VARCHAR(32) NOT NULL,
            description TEXT NOT NULL,
            status VARCHAR(32) NOT NULL,
            priority INTEGER NOT NULL,
            input_data JSON NOT NULL,
            output_data JSON NOT NULL,
            retry_count INTEGER NOT NULL,
            max_retries INTEGER NOT NULL,
            created_at DATETIME NOT NULL,
            started_at DATETIME,
            finished_at DATETIME,
            error TEXT,
            metadata_json JSON NOT NULL
        );
        INSERT INTO tasks VALUES
        ('task-1', NULL, 'task-1', 'USER', 'keep me', '', 'QUEUED', 1,
         CURRENT_TIMESTAMP, NULL, NULL, NULL, 0, 3, '{"legacy": true}', NULL, NULL, '{}', NULL);
        """
    )
    connection.commit()
    connection.close()

    database = Database(f"sqlite:///{path}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        task = await repository.get_task("task-1")
        assert task is not None
        assert task.metadata == {"legacy": True}
    await database.close()

    connection = sqlite3.connect(path)
    assert connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 5
    assert "metadata_json" not in {
        column[1] for column in connection.execute("PRAGMA table_info(tasks)")
    }
    connection.close()


@pytest.mark.asyncio
async def test_sqlite_round_trips_task_contract_and_working_memory(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'contracts.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        task = Task(
            goal="persist contract",
            metadata={"legacy_flag": True},
            contract=TaskContract(objective="Build a calculator"),
        )
        task.working_memory.artifact_refs.append(
            ArtifactRef(
                kind=ArtifactKind.FILE,
                description="source",
                producer_node_id="node-1",
                path="calculator.py",
            )
        )
        await repository.save_task(task)

        loaded = await repository.get_task(task.id)

        assert loaded.contract.objective == "Build a calculator"
        assert loaded.working_memory.artifact_refs[0].path == "calculator.py"
        assert loaded.metadata == {"legacy_flag": True}
    await database.close()


@pytest.mark.asyncio
async def test_sqlite_persists_idempotent_recovery_expansion(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'recovery-expansion.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        expansion = RecoveryExpansion(
            task_id="task-1",
            target_node_id="node-1",
            attempt=1,
            strategy="FIX",
            status="RUNNING",
            node_ids=["fix-1", "fix-2"],
            final_node_id="fix-2",
        )
        await repository.save_recovery_expansion(expansion)
        loaded = await repository.get_recovery_expansion("task-1", "node-1", 1)
        assert loaded is not None
        assert loaded.node_ids == ["fix-1", "fix-2"]
        assert await repository.get_recovery_expansion("task-1", "node-1", 2) is None
    await database.close()


def test_soft_constraint_is_advisory_by_default():
    constraint = SoftConstraint(description="Prefer a short implementation")
    assert constraint.strength == "soft"
    assert constraint.on_violation == "warn"


@pytest.mark.asyncio
async def test_sqlite_persists_artifact_ledger(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'artifacts.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        artifact = ArtifactRef(
            kind=ArtifactKind.REPORT,
            description="audit report",
            producer_node_id="node-1",
            path="artifacts/report.md",
            checksum="abc123",
        )

        await repository.save_artifact("task-1", artifact)
        loaded = await repository.list_artifacts("task-1")

        assert loaded == [artifact]
        assert (await repository.list_artifacts("task-1", node_id="other")) == []
        with pytest.raises(ValueError, match="immutable"):
            await repository.save_artifact(
                "task-1",
                artifact.model_copy(update={"description": "changed"}),
            )
    await database.close()


@pytest.mark.asyncio
async def test_resolver_exposes_declared_artifact_inputs(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'resolved-inputs.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        source = TaskNode(
            id="source-node",
            task_id="task-1",
            description="produce source",
            status=NodeStatus.SUCCEEDED,
            contract=NodeContract(logical_id="produce"),
        )
        consumer = TaskNode(
            id="consumer-node",
            task_id="task-1",
            description="consume source",
            status=NodeStatus.READY,
            contract=NodeContract(
                inputs=[
                    {"kind": "report", "ref": "node:produce:source", "required": True}
                ]
            ),
        )
        artifact = ArtifactRef(
            kind=ArtifactKind.REPORT,
            description="source report",
            producer_node_id=source.id,
            metadata={"name": "source"},
        )
        await repository.save_artifact("task-1", artifact)
        context = ContextBuilder(repository, ToolRegistry([]))

        result = await context.for_resolver(
            Task(id="task-1", goal="consume report"),
            consumer,
            TaskGraph([source, consumer], []),
        )

        assert result["resolved_inputs"][0]["missing"] is False
        assert result["resolved_inputs"][0]["artifacts"][0]["id"] == artifact.id
    await database.close()


@pytest.mark.asyncio
async def test_persistence_rejects_illegal_terminal_transition(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        task = Task(goal="state", status=TaskStatus.SUCCEEDED)
        await repository.save_task(task)
        task.status = TaskStatus.READY

        with pytest.raises(InvalidStateTransition):
            await repository.save_task(task)
    await database.close()


@pytest.mark.asyncio
async def test_sqlite_persists_and_searches_long_term_memory(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'memory.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        await repository.save_memory(
            MemoryRecord(kind="preference", key="project_language", value="Python", source="USER")
        )
        memories = await repository.search_memory("Python project")
        assert memories[0].value == "Python"
    await database.close()


@pytest.mark.asyncio
async def test_memory_expiration_redaction_deletion_and_export(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'memory-governance.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        expired = await repository.upsert_memory(
            kind="temporary",
            key="old",
            value="remove me",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        current = await repository.upsert_memory(
            kind="secret",
            key="token",
            value="do not expose",
        )

        assert await repository.search_memory("old") == []
        redacted = await repository.redact_memory(current.id)
        assert redacted.value == "[REDACTED]"
        assert (await repository.search_memory("token"))[0].value == "[REDACTED]"
        exported = await repository.export_memory()
        assert all(item["id"] != expired.id for item in exported)
        assert await repository.purge_expired_memory() == 1
        assert await repository.delete_memory(current.id) is True
        assert await repository.list_memory() == []
    await database.close()


@pytest.mark.asyncio
async def test_sqlite_health_reports_wal_and_integrity(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'health.db'}")
    await database.create_all()

    health = await database.health_check()

    assert health["status"] == "ok"
    assert health["journal_mode"] == "wal"
    assert health["foreign_keys"] == 1
    assert health["integrity"] == "ok"
    await database.close()


@pytest.mark.asyncio
async def test_idempotency_result_is_reused_and_old_events_are_purged(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'maintenance.db'}")
    await database.create_all()
    async with database.sessions() as session:
        repository = TaskRepository(session)
        first = await repository.save_idempotency_result("same-operation", {"value": 1})
        second = await repository.save_idempotency_result("same-operation", {"value": 2})
        assert first == second == {"value": 1}

        old = TaskEvent(
            task_id="task",
            event_type="old",
            created_at=datetime.now(UTC) - timedelta(days=60),
        )
        recent = TaskEvent(task_id="task", event_type="recent")
        await repository.save_event(old)
        await repository.save_event(recent)
        assert await repository.purge_old_events(retention_days=30, keep_recent=0) == 1
        assert [event.event_type for event in await repository.list_events("task")] == ["recent"]
    await database.close()


@pytest.mark.asyncio
async def test_project_analyzer_emits_inheritance_and_composition_edges(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Base:\n    pass\n\nclass Dependency:\n    pass\n\nclass Child(Base):\n    def __init__(self):\n        self.dependency = Dependency()\n",
        encoding="utf-8",
    )

    result = await ProjectAnalyzer().analyze(str(tmp_path))

    assert result.success is True
    kinds = {edge["kind"] for edge in result.output["dependency_edges"]}
    assert {"inherits", "composes"}.issubset(kinds)
