"""Provider-measured latency/TTFT and screen captures reaching the model."""

from __future__ import annotations

import json

import httpx
import pytest

from assistant.application import TaskService
from assistant.context import ContextBuilder
from assistant.domain.contracts import ArtifactKind, ArtifactRef
from assistant.domain.models import Task
from assistant.llm import DeepSeekLLMProvider
from assistant.tools import ToolRegistry

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _provider(client: httpx.AsyncClient, **kwargs) -> DeepSeekLLMProvider:
    return DeepSeekLLMProvider(
        "https://api.deepseek.com", "deepseek-flash", "test-key", client=client, **kwargs
    )


@pytest.mark.asyncio
async def test_non_streaming_call_records_measured_latency():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            },
        )

    provider = _provider(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        await provider.chat([{"role": "user", "content": "hi"}], reasoning_effort="none")
        usage = provider.last_usage
    finally:
        await provider.close()

    assert isinstance(usage["provider_latency_ms"], int)
    assert usage["provider_latency_ms"] >= 0
    # TTFT is only knowable while streaming.
    assert "ttft_ms" not in usage
    assert usage["prompt_tokens"] == 5


@pytest.mark.asyncio
async def test_streaming_call_records_time_to_first_token():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"reasoning_content":"hmm"},"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{"content":"{\\"ok\\":true}"},"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":9,"completion_tokens":4,"total_tokens":13}}\n\n'
                "data: [DONE]"
            ),
        )

    provider = _provider(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        payload = await provider.chat(
            [{"role": "user", "content": "hi"}], reasoning_effort="none", stream=True
        )
        usage = provider.last_usage
    finally:
        await provider.close()

    assert payload["choices"][0]["message"]["content"] == '{"ok":true}'
    assert isinstance(usage["ttft_ms"], int)
    assert usage["ttft_ms"] >= 0
    assert isinstance(usage["provider_latency_ms"], int)
    assert usage["completion_tokens"] == 4


def test_usage_totals_accumulate_latency_and_ttft_samples():
    task = Task(goal="measure")

    TaskService._record_llm_usage(
        task, {"prompt_tokens": 10, "provider_latency_ms": 1200, "ttft_ms": 300}
    )
    TaskService._record_llm_usage(
        task, {"prompt_tokens": 10, "provider_latency_ms": 800}
    )
    totals = task.runtime.llm_usage

    assert totals["calls"] == 2
    assert totals["latency_ms"] == 2000
    assert totals["latency_samples"] == 2
    assert totals["ttft_ms"] == 300
    assert totals["ttft_samples"] == 1


def test_analytics_report_provider_latency_and_ttft():
    from types import SimpleNamespace

    from assistant.api.serializers import _dashboard_analytics
    from assistant.domain.models import TaskEvent, TaskStatus

    task = Task(goal="run", status=TaskStatus.SUCCEEDED)
    events = [
        TaskEvent(
            task_id=task.id,
            event_type="LLM_RESPONSE",
            payload={
                "role": "AGENT",
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "provider_latency_ms": 1000, "ttft_ms": 200},
            },
        ),
        TaskEvent(
            task_id=task.id,
            event_type="LLM_RESPONSE",
            payload={
                "role": "AGENT",
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "provider_latency_ms": 2000},
            },
        ),
    ]
    context = SimpleNamespace(
        service=SimpleNamespace(llm=SimpleNamespace(model="deepseek-flash")),
        settings=SimpleNamespace(deepseek_model_label="DeepSeek V4.1 Flash", model_pricing=""),
    )

    analytics = _dashboard_analytics(context, [task], {task.id: []}, events)
    latency = analytics["provider_latency"]

    assert latency["samples"] == 2
    assert latency["average_ms"] == 1500.0
    assert latency["ttft_samples"] == 1
    assert latency["average_ttft_ms"] == 200.0


@pytest.mark.asyncio
async def test_smallest_capture_is_offered_to_a_vision_model(tmp_path):
    image = tmp_path / "screen.png"
    image.write_bytes(PNG_BYTES)

    class Repository:
        async def list_artifacts(self, task_id):
            return [
                ArtifactRef(
                    id="capture-1",
                    kind=ArtifactKind.IMAGE,
                    description="screen.capture monitor-0",
                    producer_node_id="node-1",
                    path=str(image),
                    metadata={"origin_x": 1920, "origin_y": 0, "scale": 0.5},
                )
            ]

    task = Task(goal="haz clic en el botón")
    builder = ContextBuilder(Repository(), ToolRegistry(), vision_enabled=True)

    screenshots = await builder._screenshots(task)

    assert screenshots[0]["path"] == str(image)
    assert screenshots[0]["origin_x"] == 1920

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": '{"task_id": null, "nodes": []}'},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    seen: list[dict] = []

    async def capturing(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return await handler(request)

    provider = _provider(
        httpx.AsyncClient(transport=httpx.MockTransport(capturing)),
        supports_vision=True,
        max_vision_images=1,
    )
    try:
        await provider.plan(
            {"task": {"id": task.id, "goal": task.goal}, "screenshots": screenshots}
        )
    finally:
        await provider.close()

    content = seen[0]["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_screenshots_are_not_embedded_without_vision(tmp_path):
    image = tmp_path / "screen.png"
    image.write_bytes(PNG_BYTES)
    screenshots = [{"id": "c1", "path": str(image), "description": "capture"}]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": '{"task_id": null, "nodes": []}'},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    seen: list[dict] = []

    async def capturing(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return await handler(request)

    provider = _provider(
        httpx.AsyncClient(transport=httpx.MockTransport(capturing)), supports_vision=False
    )
    try:
        await provider.plan(
            {"task": {"id": "t", "goal": "mira"}, "screenshots": screenshots}
        )
    finally:
        await provider.close()

    content = seen[0]["messages"][1]["content"]
    assert isinstance(content, str)
    # The path is still visible so a text model can read the file itself.
    assert image.name in content
