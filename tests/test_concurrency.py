"""Concurrency contract: isolated provider state and parallel task dispatch.

These tests exist because parallel execution is only safe when two properties
hold: per-call LLM state must not be shared between tasks, and the session
factory must route commits through a single serialized write path.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from assistant.domain.models import Task, TaskStatus
from assistant.infrastructure.db import Database
from assistant.infrastructure.session import SerializedWriteSession, write_lock
from assistant.llm import DeepSeekLLMProvider
from assistant.runtime import TaskRuntime


@pytest.mark.asyncio
async def test_llm_usage_is_isolated_between_concurrent_tasks():
    """Two tasks calling the same provider must not read each other's usage."""

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        index = payload["messages"][-1]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 10 if index == "first" else 99,
                    "completion_tokens": 1,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DeepSeekLLMProvider(
        "https://api.deepseek.com", "deepseek-flash", "test-key", client=client
    )

    async def call(content: str) -> int:
        await provider.chat(
            [{"role": "user", "content": content}], reasoning_effort="none"
        )
        return provider.last_usage["prompt_tokens"]

    try:
        assert await asyncio.gather(call("first"), call("second")) == [10, 99]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_runtime_dispatches_independent_tasks_concurrently():
    """With max_concurrent=2, two tasks must be in flight at the same time."""

    tasks = [
        Task(goal="a", status=TaskStatus.QUEUED),
        Task(goal="b", status=TaskStatus.QUEUED),
    ]

    class Repository:
        async def list_tasks(self):
            return tasks

    started: list[str] = []
    release = asyncio.Event()

    async def execute(task_id: str) -> None:
        started.append(task_id)
        if len(started) >= 2:
            release.set()
        # Sequential dispatch would time out here instead of hanging the suite.
        await asyncio.wait_for(release.wait(), timeout=2)

    runtime = TaskRuntime(Repository(), execute, max_concurrent=2)

    assert await runtime.run_once() == 2
    assert sorted(started) == sorted(task.id for task in tasks)
    assert runtime.metrics_snapshot()["task_errors"] == 0


def test_runtime_defaults_to_sequential_dispatch():
    runtime = TaskRuntime(object(), lambda task_id: None)
    assert runtime.max_concurrent == 1


@pytest.mark.asyncio
async def test_write_lock_is_stable_per_event_loop():
    assert write_lock() is write_lock()


@pytest.mark.asyncio
async def test_database_factory_uses_the_serialized_write_session():
    database = Database("sqlite:///:memory:")
    try:
        session = database.sessions()
        try:
            assert isinstance(session, SerializedWriteSession)
        finally:
            await session.close()
    finally:
        await database.close()
