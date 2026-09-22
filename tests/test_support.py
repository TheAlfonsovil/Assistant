import pytest

from assistant.domain.models import (
    ErrorType,
    OperationResult,
    Task,
    TaskStatus,
    VerificationDecision,
)
from assistant.idle import IdleCycle
from assistant.runtime import TaskRuntime
from assistant.verifier import DeterministicVerifier


def test_deterministic_verifier_classifies_results():
    verifier = DeterministicVerifier()
    assert verifier.verify(OperationResult(success=True)).decision is VerificationDecision.SUCCESS
    assert (
        verifier.verify(
            OperationResult(success=False, error_type=ErrorType.TIMEOUT, retryable=True)
        ).decision
        is VerificationDecision.RETRY
    )


def test_deterministic_verifier_retries_when_required_output_is_missing():
    verifier = DeterministicVerifier()
    result = OperationResult(
        success=True,
        metadata={
            "missing_required_outputs": [
                {"name": "source", "kind": "file", "reason": "required output was not published"}
            ]
        },
    )

    verification = verifier.verify(result)

    assert verification.decision is VerificationDecision.RETRY
    assert verification.missing_evidence == ["output:source"]
    assert verification.diagnostics[0].code == "OUTPUT_REQUIRED_MISSING"


def test_deterministic_verifier_requires_declared_evidence():
    verifier = DeterministicVerifier()
    result = OperationResult(
        success=True,
        output={"text": "tests failed"},
        metadata={"expected": {"contains": ["tests passed"]}},
    )
    assert verifier.verify(result).decision is VerificationDecision.RETRY


def test_deterministic_verifier_supports_typed_field_evidence():
    verifier = DeterministicVerifier()
    result = OperationResult(
        success=True,
        output={"status": "healthy", "payload": {"count": 3}},
        metadata={
            "expected": {
                "fields": {"status": "healthy", "payload.count": 3},
                "exists": ["payload.count"],
                "not_exists": ["error"],
            }
        },
    )

    assert verifier.verify(result).decision is VerificationDecision.SUCCESS


def test_deterministic_verifier_reports_structured_acceptance_criteria():
    result = OperationResult(
        success=True,
        metadata={
            "acceptance_criteria": [
                {"id": "tests-pass", "description": "Tests pass", "required": True}
            ]
        },
    )

    verification = DeterministicVerifier().verify(result)

    assert verification.criteria_results[0].criterion_id == "tests-pass"
    assert verification.criteria_results[0].status.value == "PASS"


def test_deterministic_verifier_records_soft_constraint_warning_without_blocking():
    result = OperationResult(
        success=True,
        metadata={
            "constraint_violations": [
                {
                    "id": "prefer-tests",
                    "description": "Tests were not available",
                    "strength": "soft",
                }
            ]
        },
    )

    verification = DeterministicVerifier().verify(result)

    assert verification.decision is VerificationDecision.SUCCESS
    assert verification.diagnostics[0].code == "SOFT_CONSTRAINT_VIOLATED"
    assert verification.diagnostics[0].severity == "warning"


def test_deterministic_verifier_blocks_hard_constraint_violation():
    result = OperationResult(
        success=True,
        metadata={
            "constraint_violations": [
                {
                    "id": "no-network",
                    "description": "Network access was forbidden",
                    "strength": "hard",
                }
            ]
        },
    )

    verification = DeterministicVerifier().verify(result)

    assert verification.decision is VerificationDecision.BLOCK
    assert verification.diagnostics[0].code == "HARD_CONSTRAINT_VIOLATED"


@pytest.mark.asyncio
async def test_idle_cycle_uses_normal_task_creation_path():
    goals = []
    cycle = IdleCycle(lambda goal: _record(goals, goal))
    created = await cycle.run_once()
    assert created == goals[0]
    assert goals[0].startswith("Assistant maintenance")


@pytest.mark.asyncio
async def test_idle_cycle_applies_cooldown_between_maintenance_tasks():
    goals = []
    cycle = IdleCycle(lambda goal: _record(goals, goal), interval=60)

    await cycle.run_once()
    assert await cycle.run_once() is None
    assert len(goals) == 1


@pytest.mark.asyncio
async def test_idle_cycle_skips_creation_when_work_exists_and_can_stop():
    goals = []
    cycle = IdleCycle(lambda goal: _record(goals, goal), interval=0)
    assert await cycle.run_once(has_work=True) is None
    assert goals == []


@pytest.mark.asyncio
async def test_idle_cycle_supervises_active_work_without_creating_maintenance():
    supervised = []
    goals = []

    async def supervise(has_work):
        supervised.append(has_work)
        return 1

    cycle = IdleCycle(
        lambda goal: _record(goals, goal),
        interval=60,
        supervise=supervise,
        supervision_interval=0,
    )

    assert await cycle.run_once(has_work=True) == 1
    assert supervised == [True]
    assert goals == []
    assert cycle.last_supervision_result == 1
    cycle.stop()
    await cycle.run_forever()
    assert goals == []


@pytest.mark.asyncio
async def test_runtime_continues_after_one_task_exception():
    tasks = [Task(goal="first", status=TaskStatus.READY), Task(goal="second", status=TaskStatus.READY)]
    called = []

    async def execute(task_id):
        called.append(task_id)
        if len(called) == 1:
            raise RuntimeError("unexpected task failure")

    class Repository:
        async def list_tasks(self):
            return tasks

    assert await TaskRuntime(Repository(), execute).run_once() == 2
    assert called == [tasks[0].id, tasks[1].id]


@pytest.mark.asyncio
async def test_runtime_retries_tasks_left_in_planning():
    task = Task(goal="resume planner", status=TaskStatus.PLANNING)
    called = []

    async def execute(task_id):
        called.append(task_id)

    class Repository:
        async def list_tasks(self):
            return [task]

    assert await TaskRuntime(Repository(), execute).run_once() == 1
    assert called == [task.id]


async def _record(goals, goal):
    goals.append(goal)
    return goal
