"""HTTP tool, tool pacing and the metrics series."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from assistant.api.serializers import _metrics_series
from assistant.devices.computer.http_client import HttpTool, summarize_response
from assistant.domain.models import ErrorType, Operation, TaskEvent
from assistant.policy import RateLimiter
from assistant.tools import ToolRegistry


def _response(status: int = 200, **kwargs) -> httpx.Response:
    request = httpx.Request("GET", "https://example.com/api")
    return httpx.Response(status, request=request, **kwargs)


def test_response_summary_is_bounded_and_parses_json():
    response = _response(json={"ok": True})
    response.headers["content-type"] = "application/json"

    summary = summarize_response(response, limit=10)

    assert summary["status_code"] == 200
    assert summary["ok"] is True
    assert summary["json"] == {"ok": True}
    assert "truncated" in summary


@pytest.mark.asyncio
async def test_http_tool_blocks_private_destinations():
    tool = HttpTool()

    result = await tool.execute("get", {"url": "http://127.0.0.1:8000/api/v1/health"}, 5)
    other = await tool.execute("get", {"url": "http://10.0.0.5/internal"}, 5)

    assert result.success is False
    assert result.error_type is ErrorType.AUTH
    assert "blocked destination" in result.error
    assert other.success is False


@pytest.mark.asyncio
async def test_http_tool_rejects_unsupported_verb_and_conflicting_body():
    tool = HttpTool()

    verb = await tool.execute("fetch", {"url": "https://example.com"}, 5)
    both = await tool.execute(
        "post",
        {"url": "https://example.com", "body": "a", "json": {"a": 1}},
        5,
    )

    assert verb.success is False and verb.error_type is ErrorType.INVALID_ARGUMENT
    assert both.success is False and "not both" in both.error


@pytest.mark.asyncio
async def test_http_tool_reports_a_status_code_without_crashing(monkeypatch):
    """A 404 is an observation the caller can act on, not a tool crash."""

    async def fake_request(self, verb, url, **kwargs):  # noqa: ANN001
        return _response(404, json={"detail": "missing"})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request, raising=True)
    tool = HttpTool()

    result = await tool.execute("get", {"url": "https://example.com/api"}, 5)

    assert result.success is False
    assert result.output["status_code"] == 404
    assert result.retryable is False
    assert result.error == "HTTP 404"


def test_http_declares_safe_methods_as_repeatable():
    definition = HttpTool.definition

    assert definition.idempotent is False
    assert set(definition.idempotent_methods) == {"get", "head", "options"}
    assert {"post", "delete"} <= set(definition.methods)


def test_operation_is_repeatable_consults_the_method_list():
    from assistant.application import TaskService

    assert TaskService._operation_is_repeatable(HttpTool.definition, "get") is True
    # Case-insensitive: the model may send the verb in any case.
    assert TaskService._operation_is_repeatable(HttpTool.definition, "GET") is True
    assert TaskService._operation_is_repeatable(HttpTool.definition, "post") is False
    assert TaskService._operation_is_repeatable(HttpTool.definition, "delete") is False
    assert TaskService._operation_is_repeatable(HttpTool.definition, "patch") is False


def test_rate_limiter_paces_one_tool_without_touching_others():
    limiter = RateLimiter({"http": 60})
    operation = Operation(tool="http", method="get", args={"url": "https://x"})

    assert limiter.check(operation) is None
    blocked = limiter.check(operation)
    assert blocked is not None and "rate limit" in blocked
    assert limiter.check(Operation(tool="filesystem", method="list", args={"path": "."})) is None
    assert limiter.snapshot()["blocked_now"]["http"] > 0


def test_rate_limiter_ignores_invalid_intervals():
    limiter = RateLimiter({"http": "not-a-number", "web": 0, "shell": 2})

    assert limiter.active is True
    assert "http" not in limiter.min_intervals and "web" not in limiter.min_intervals
    assert limiter.min_intervals["shell"] == 2.0


@pytest.mark.asyncio
async def test_registry_refuses_a_call_that_is_too_soon():
    registry = ToolRegistry([], rate_limit=RateLimiter({"echo": 60}))

    from tests.test_tool_policy import _EchoTool

    registry.register(_EchoTool())
    first = await registry.execute(Operation(tool="echo", method="run", args={"value": "a"}))
    second = await registry.execute(Operation(tool="echo", method="run", args={"value": "b"}))

    assert first.success is True
    assert second.success is False
    assert second.retryable is True
    assert second.error_type is ErrorType.TRANSIENT


def test_metrics_series_buckets_events_by_hour():
    now = datetime(2026, 9, 30, 12, 30, tzinfo=UTC)
    events = [
        TaskEvent(
            task_id="t1",
            event_type="LLM_RESPONSE",
            created_at=now - timedelta(minutes=40),
            payload={
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_cache_hit_tokens": 30,
                    "completion_tokens_details": {"reasoning_tokens": 5},
                    "provider_latency_ms": 1000,
                }
            },
        ),
        TaskEvent(
            task_id="t1",
            event_type="TOOL_RESULT",
            created_at=now - timedelta(minutes=35),
            payload={"success": False},
        ),
        TaskEvent(
            task_id="t1",
            event_type="TASK_CREATED",
            created_at=now - timedelta(hours=3),
            payload={},
        ),
    ]

    series = _metrics_series(events, hours=4, now=now)

    assert len(series) == 4
    assert series[0]["tasks_created"] == 1  # oldest bucket
    # 11:50 events land in the 11:00 bucket; the current bucket is still empty.
    active = series[-2]
    assert active["llm_calls"] == 1
    assert active["prompt_tokens"] == 100
    assert active["cached_tokens"] == 30
    assert active["reasoning_tokens"] == 5
    assert active["tool_calls"] == 1
    assert active["tool_failures"] == 1
    assert active["average_latency_ms"] == 1000.0
    assert active["hour"].endswith("11:00:00+00:00")
    assert series[-1]["llm_calls"] == 0


def test_metrics_series_ignores_events_outside_the_window():
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    events = [
        TaskEvent(
            task_id="t1",
            event_type="TASK_COMPLETED",
            created_at=now - timedelta(days=5),
            payload={},
        )
    ]

    series = _metrics_series(events, hours=2, now=now)

    assert len(series) == 2
    assert all(bucket["tasks_finished"] == 0 for bucket in series)


def test_series_endpoint_is_mounted():
    from assistant.api.application import API_PREFIX, app

    assert f"{API_PREFIX}/metrics/series" in app.openapi()["paths"]


def _unused(_: json.JSONDecodeError | None = None) -> None:
    pass
