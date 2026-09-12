import pytest

from assistant.domain.models import ErrorType, OperationResult, VerificationDecision
from assistant.idle import IdleCycle
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


async def _record(goals, goal):
    goals.append(goal)
    return goal
