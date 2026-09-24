import json

import pytest
from pydantic import ValidationError

from assistant.context import ContextBuilder
from assistant.domain.contracts import ContractScope, DecisionRecord, MemoryFact
from assistant.domain.models import (
    AgentBudget,
    AgentDecision,
    AgentDecisionType,
    AgentSubtask,
    Operation,
    RetryDirective,
    WorkerEvidence,
    WorkerResult,
)
from assistant.llm import parse_agent_decision


def test_agent_decision_accepts_llm_action_alias_and_typed_operation():
    decision = parse_agent_decision(
        {
            "action": "EXECUTE",
            "reason": "Inspect the project root",
            "operation": {
                "tool": "filesystem",
                "method": "exists",
                "args": {"path": "."},
            },
        }
    )

    assert decision.decision_type is AgentDecisionType.EXECUTE
    assert decision.type is AgentDecisionType.EXECUTE
    assert isinstance(decision.operation, Operation)


def test_operation_accepts_dotted_tool_shorthand():
    operation = Operation.model_validate(
        {"tool": "codegraph.query", "method": "query", "args": {"query": "assistant"}}
    )
    assert operation.tool == "codegraph"
    assert operation.method == "query"

    inferred = Operation.model_validate({"tool": "project.read", "args": {"files": ["README.md"]}})
    assert inferred.tool == "project"
    assert inferred.method == "read"


def test_agent_decision_normalizes_dotted_execute_tool():
    decision = parse_agent_decision(
        {
            "decision_type": "EXECUTE",
            "reason": "Start with a codegraph query",
            "operation": {
                "tool": "codegraph.query",
                "method": "query",
                "args": {"root": "C:\\projects\\Assistant", "query": "assistant", "kind": "module"},
            },
        }
    )
    assert decision.operation is not None
    assert decision.operation.tool == "codegraph"
    assert decision.operation.method == "query"


def test_agent_decision_rejects_execute_without_operation():
    with pytest.raises(ValidationError, match="require an operation"):
        AgentDecision(type="EXECUTE")


def test_agent_decision_rejects_empty_delegation_and_blank_subtasks():
    with pytest.raises(ValidationError, match="at least one subtask"):
        AgentDecision(type=AgentDecisionType.DELEGATE)

    with pytest.raises(ValidationError, match="blank values"):
        AgentDecision(type="DELEGATE", subtasks=[""])


def test_agent_budget_validates_non_negative_limits_and_is_optional():
    decision = AgentDecision(
        type="WAIT",
        budget=AgentBudget(max_steps=3, max_llm_calls=0, max_execution_time=1.5),
    )

    assert decision.budget is not None
    assert decision.budget.max_steps == 3
    with pytest.raises(ValidationError):
        AgentBudget(max_tool_calls=-1)


def test_agent_context_bound_is_global_and_preserves_core_fields():
    context = {
        "phase": "AGENT",
        "user_prompt": "audit",
        "task": {"id": "task-1", "goal": "audit"},
        "evidence": [{"payload": "x" * 100_000}],
        "last_observation": {"stdout": "y" * 100_000},
        "available_actions": [{"group": "filesystem", "tools": ["read"] * 100}],
        "project": {"codegraph": {"graph": {"nodes": ["z"] * 100_000}}},
    }

    bounded = ContextBuilder._bound_agent_context(context, limit=4_000)

    assert len(json.dumps(bounded, default=str)) <= 4_000
    assert bounded["phase"] == "AGENT"
    assert bounded["task"]["id"] == "task-1"


def test_worker_contracts_round_trip_through_json_and_validate_payloads():
    result = WorkerResult(
        success=False,
        output={"changed_files": ["assistant/domain/models.py"]},
        error="transient tool failure",
        evidence=[
            WorkerEvidence(
                kind="test",
                source="pytest",
                value={"passed": 3},
                path="tests/test_agent_decisions.py",
                line_start=1,
                line_end=10,
            )
        ],
        retry_directive=RetryDirective(
            action="retry",
            attempt=0,
            max_attempts=2,
            delay_seconds=0.25,
        ),
        subtasks=[AgentSubtask(id="verify", description="Run focused tests")],
    )

    restored = WorkerResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert restored.evidence[0].content == {"passed": 3}
    assert restored.retry is not None
    assert restored.retry.should_retry is True


def test_worker_contracts_reject_invalid_retry_and_subtask_dependencies():
    with pytest.raises(ValidationError, match="below max_attempts"):
        RetryDirective(action="retry", attempt=2, max_attempts=2)

    with pytest.raises(ValidationError, match="cannot depend on itself"):
        AgentSubtask(id="loop", description="invalid", dependencies=["loop"])

    with pytest.raises(ValidationError, match="line_end"):
        WorkerEvidence(line_start=10, line_end=2)

    with pytest.raises(ValidationError, match="successful worker results"):
        WorkerResult(success=True, error="unexpected")


def test_worker_decision_applies_only_bounded_typed_working_memory_updates():
    from assistant.domain.models import Task

    task = Task(goal="keep context")
    decision = AgentDecision(
        type="WAIT",
        working_memory_updates={
            "facts": [
                MemoryFact(
                    key="selected_stack",
                    value="Python",
                    scope=ContractScope.TASK,
                )
            ],
            "decisions": [DecisionRecord(decision="run focused tests")],
        },
    )

    task.apply_worker_decision(decision)

    assert task.working_memory.facts[0].key == "selected_stack"
    assert task.working_memory.decisions[0].decision == "run focused tests"
