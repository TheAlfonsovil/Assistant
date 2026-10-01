"""Budgets are configuration, not constants.

Every ceiling the ledger enforces (LLM calls, tool calls, reads, index queries,
bytes, plan nodes, recovery attempts) is applied to each created task, and the
engine defaults stay the floor a bare ``TaskBudget`` carries. A task that dies
because a useful budget was exhausted is a configuration bug, so the wiring is
tested instead of assumed.
"""

import pytest

from assistant.domain.models import Project, TaskBudget, TaskRequest
from assistant.infrastructure.db import Database
from assistant.llm import MockLLMProvider
from assistant.tools import ToolRegistry

from assistant.application.service import TaskService

BUDGETS = {
    "max_llm_calls": 60,
    "max_tool_calls": 150,
    "max_codegraph_queries": 200,
    "max_project_reads": 200,
    "max_source_bytes": 60_000_000,
    "max_plan_nodes": 200,
    "max_retries": 3,
    "max_recovery_attempts": 3,
}


async def _service(tmp_path, **kwargs):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'budgets.db'}")
    await database.create_all()
    session = database.sessions()
    service = TaskService(session, MockLLMProvider(), ToolRegistry(), **kwargs)
    return service, database, session


@pytest.mark.asyncio
async def test_configured_budgets_are_applied_to_every_new_task(tmp_path):
    service, database, session = await _service(tmp_path, task_budgets=dict(BUDGETS))
    try:
        task = await service.create_task(TaskRequest(goal="audita el repositorio"))

        for key, value in BUDGETS.items():
            assert getattr(task.budget, key) == value, key
        persisted = await service.repository.get_task(task.id)
        assert persisted.budget.max_llm_calls == BUDGETS["max_llm_calls"]
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_an_unknown_budget_key_is_ignored_instead_of_crashing(tmp_path):
    service, database, session = await _service(
        tmp_path, task_budgets={"max_llm_calls": 42, "max_unlimited_everything": 1}
    )
    try:
        task = await service.create_task(TaskRequest(goal="cualquier cosa"))

        assert task.budget.max_llm_calls == 42
        assert not hasattr(task.budget, "max_unlimited_everything")
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_the_engine_defaults_stay_available_when_nothing_is_configured(tmp_path):
    service, database, session = await _service(tmp_path)
    try:
        task = await service.create_task(TaskRequest(goal="cualquier cosa"))
        engine = TaskBudget()

        assert task.budget.max_llm_calls == engine.max_llm_calls
        assert task.budget.max_tool_calls == engine.max_tool_calls
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_the_plan_budget_also_bounds_agent_turns(tmp_path):
    """Agent mode reuses ``max_plan_nodes`` as its step ceiling: one switch."""
    service, database, session = await _service(
        tmp_path, task_budgets={**BUDGETS, "max_plan_nodes": 7}
    )
    try:
        await service.create_project(Project(name="here", path=str(tmp_path)))
        task = await service.create_task(TaskRequest(goal="haz algo util"))

        assert task.budget.max_plan_nodes == 7
        assert task.runtime.agent_turns == 0
    finally:
        await session.close()
        await database.close()
