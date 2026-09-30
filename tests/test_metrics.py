"""Metrics contract: the aggregates a 24/7 run depends on."""

from __future__ import annotations

import json
from types import SimpleNamespace

from assistant.api.serializers import _dashboard_analytics
from assistant.domain.models import Task, TaskEvent, TaskStatus

USAGE_EVENT = {
    "role": "AGENT",
    "usage": {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
        "prompt_cache_hit_tokens": 40,
        "prompt_cache_miss_tokens": 60,
        "completion_tokens_details": {"reasoning_tokens": 4},
    },
}


def _context(pricing: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        service=SimpleNamespace(llm=SimpleNamespace(model="deepseek-flash")),
        settings=SimpleNamespace(
            deepseek_model_label="DeepSeek V4.1 Flash", model_pricing=pricing
        ),
    )


def _analytics(pricing: str = ""):
    task = Task(
        goal="audita el proyecto",
        status=TaskStatus.SUCCEEDED,
        metadata={"worker": "AUDIT_WORKER"},
    )
    task.started_at = task.created_at
    events = [
        TaskEvent(
            task_id=task.id,
            event_type="TOOL_RESULT",
            payload={"tool": "shell", "success": False},
        ),
        TaskEvent(
            task_id=task.id,
            event_type="TOOL_RESULT",
            payload={"tool": "audit", "success": True},
        ),
        TaskEvent(
            task_id=task.id,
            event_type="NODE_BLOCKED",
            payload={"reason": "required inputs are missing"},
        ),
        TaskEvent(task_id=task.id, event_type="LLM_RESPONSE", payload=dict(USAGE_EVENT)),
    ]
    return _dashboard_analytics(_context(pricing), [task], {task.id: []}, events)


def test_analytics_report_model_label_and_reliability():
    analytics = _analytics()

    assert analytics["model"] == "deepseek-flash"
    assert analytics["model_label"] == "DeepSeek V4.1 Flash"
    assert analytics["reliability"] == {
        "tool_calls": 2,
        "tool_failures": 1,
        "tool_success_rate": 50.0,
        "retries": 0,
        "blockers": {"required inputs are missing": 1},
    }


def test_analytics_report_queue_health_and_worker_outcomes():
    analytics = _analytics()

    assert analytics["queue"] == {
        "queued": 0,
        "running": 0,
        "waiting": 0,
        "blocked": 0,
        "average_queue_seconds": 0.0,
        "max_queue_seconds": 0.0,
    }
    assert analytics["worker_outcomes"]["AUDIT_WORKER"] == {
        "total": 1,
        "succeeded": 1,
        "failed": 0,
        "blocked": 0,
        "cancelled": 0,
    }


def test_analytics_report_cache_and_reasoning_tokens():
    analytics = _analytics()
    actual = analytics["actual_tokens"]

    assert actual["prompt"] == 100
    assert actual["response"] == 10
    assert actual["total"] == 110
    assert actual["cached"] == 40
    assert actual["cache_miss"] == 60
    assert actual["reasoning"] == 4
    assert actual["cache_hit_rate"] == 40.0


def test_cost_is_reported_only_when_pricing_is_configured():
    unconfigured = _analytics()
    assert unconfigured["cost"]["configured"] is False
    assert "ASSISTANT_MODEL_PRICING" in unconfigured["cost"]["reason"]

    pricing = json.dumps(
        {"deepseek-flash": {"input": 1.0, "cached_input": 0.5, "output": 2.0}}
    )
    configured = _analytics(pricing)

    assert configured["cost"]["configured"] is True
    # 60 uncached input * 1.0 + 40 cached * 0.5 + 10 output * 2.0 = 100 per million
    assert configured["cost"]["total_usd"] == 0.0001
    assert configured["cost"]["rates_per_million"]["cached_input"] == 0.5


def test_cost_reports_an_unknown_model_instead_of_guessing():
    pricing = json.dumps({"some-other-model": {"input": 1.0}})

    cost = _analytics(pricing)["cost"]

    assert cost["configured"] is False
    assert "deepseek-flash" in cost["reason"]


def test_latency_percentiles_are_present_and_zero_without_samples():
    latency = _analytics()["latency"]

    for key in (
        "llm_p50_seconds",
        "llm_p95_seconds",
        "tool_p50_seconds",
        "tool_p95_seconds",
        "task_p50_seconds",
        "task_p95_seconds",
    ):
        assert key in latency
