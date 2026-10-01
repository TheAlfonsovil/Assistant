"""The failure path has to be able to close a task, whatever went wrong.

This is the regression suite for a real incident: one orchestrator response was
rejected because its ``intent`` label was 121 characters long, and that single
rejection cost far more than a bad answer. The validation error counted as a
provider failure, three of them opened the circuit breaker (blocking every other
task), and the task itself could not be closed — ``QUEUED -> FAILED`` was not an
allowed transition — so the runtime re-dispatched it forever, one crash per
iteration, and the user saw a log storm and cancelled it by hand.
"""

from __future__ import annotations

import json

import pytest

from assistant.application import TaskService
from assistant.domain.models import Project, TaskRequest, TaskStatus
from assistant.domain.state import TERMINAL_TASK_STATUSES, validate_task_transition
from assistant.infrastructure.db import Database
from assistant.llm import INTENT_LABEL_CHARS, MockLLMProvider, OrchestratorDecision
from assistant.tools import ToolRegistry


def test_every_live_task_state_can_be_failed():
    """A step that raises must be able to record it, from any state it can be in.

    The exception path is the one place that cannot afford to discover a missing
    edge, because the missing edge *is* the failure being recorded.
    """
    for status in TaskStatus:
        if status in TERMINAL_TASK_STATUSES:
            continue
        validate_task_transition(status, TaskStatus.FAILED)


def test_a_long_intent_is_clipped_instead_of_rejected():
    """The label describes the routing; it does not select code."""
    decision = OrchestratorDecision(intent="x" * 500, reason="long goal")

    assert len(decision.intent) == INTENT_LABEL_CHARS
    assert decision.reason == "long goal"


def test_a_long_intent_from_the_model_is_accepted():
    """The same, through the validator the runtime applies to the raw response.

    ``_ask`` calls exactly this, so this is the call that raised in production.
    """
    from assistant.llm import OrchestratorDecision as Decision

    goal = "Create a new project called test_zone with a Vue front end and a Java 25 backend"
    decision = Decision.model_validate_json(
        json.dumps(
            {
                "stage": "ROUTE",
                "intent": goal,
                "worker": "CODE_WORKER",
                "template": "implementation",
            }
        )
    )

    assert len(decision.intent) <= INTENT_LABEL_CHARS
    assert len(goal) > 60


class _ExplodingProvider(MockLLMProvider):
    """Fails on the first turn, which is where the incident happened.

    ``agent_decide`` is defined on the subclass because that is what makes the
    service treat this stub as agent mode (see ``_supports_agent_mode``), which is
    the only path that calls the orchestrator.
    """

    def __init__(self):
        super().__init__()
        self.hook = None

    async def orchestrate(self, context):
        if self.hook is not None:
            await self.hook()
        raise RuntimeError("orchestrator response could not be validated")

    async def agent_decide(self, context):
        raise AssertionError("the orchestrator must fail before the agent runs")


@pytest.mark.asyncio
async def test_a_task_that_fails_on_its_first_turn_is_closed_as_failed(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'failure.db'}")
    await database.create_all()
    async with database.sessions() as session:
        provider = _ExplodingProvider()
        service = TaskService(
            session, provider, ToolRegistry(), workspace_root=str(tmp_path)
        )
        # A destination is what sends the task through the orchestrator, which is
        # where the rejected response happened.
        project = await service.create_project(
            Project(name="test_zone", path=str(tmp_path))
        )
        task = await service.create_task(
            TaskRequest(goal="crea un proyecto nuevo", project_id=project.id)
        )

        finished = await service.run_task(task.id)

        assert finished.status is TaskStatus.FAILED
        # The persisted row agrees, so the runtime stops re-dispatching it.
        stored = await service.get_task(task.id)
        assert stored.status is TaskStatus.FAILED
        events = await service.repository.list_events(task.id)
        assert "TASK_FAILED" in [event.event_type for event in events]


@pytest.mark.asyncio
async def test_a_task_cancelled_while_the_worker_runs_is_not_failed_again(tmp_path):
    """The user's decision outranks a late crash, and the crash stays recorded."""
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cancelled-race.db'}")
    await database.create_all()
    async with database.sessions() as session:
        provider = _ExplodingProvider()
        service = TaskService(
            session, provider, ToolRegistry(), workspace_root=str(tmp_path)
        )
        project = await service.create_project(
            Project(name="long_job", path=str(tmp_path))
        )
        task = await service.create_task(
            TaskRequest(goal="haz un trabajo largo", project_id=project.id)
        )
        # The cancel arrives between turns, exactly as the API delivers it.
        provider.hook = lambda: service.cancel_task(task.id)

        finished = await service.run_task(task.id)

        assert finished.status is TaskStatus.CANCELLED
        stored = await service.get_task(task.id)
        assert stored.status is TaskStatus.CANCELLED
        events = await service.repository.list_events(task.id)
        types = [event.event_type for event in events]
        assert "TASK_ERROR_IGNORED" in types
        assert "TASK_FAILED" not in types
