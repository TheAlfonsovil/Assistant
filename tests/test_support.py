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


@pytest.mark.asyncio
async def test_idle_cycle_uses_normal_task_creation_path():
    goals = []
    cycle = IdleCycle(lambda goal: _record(goals, goal))
    created = await cycle.run_once()
    assert created == goals[0]
    assert goals[0].startswith("Assistant maintenance")


@pytest.mark.asyncio
async def test_idle_cycle_skips_creation_when_work_exists_and_can_stop():
    goals = []
    cycle = IdleCycle(lambda goal: _record(goals, goal), interval=0)
    assert await cycle.run_once(has_work=True) is None
    assert goals == []
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


async def _record(goals, goal):
    goals.append(goal)
    return goal
