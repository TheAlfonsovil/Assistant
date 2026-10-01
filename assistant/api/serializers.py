from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..devices.registry import DEVICE_BRANCHES
from ..domain.models import priority_label
from ..host import prompt_facts as host_facts
from ..llm import AssistantResponse, NodeDecision, OrchestratorDecision, PlanProposal
from ..prompts.template import render


def _event_json(event, *, include_prompt: bool = False) -> dict:
    payload = event.payload or {}
    if event.event_type == "LLM_REQUEST" and payload.get("role"):
        role = str(payload["role"])
        request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
        if not request.get("rendered_instructions"):
            context = payload.get("context")
            prompt_path = Path(__file__).parents[1] / "prompts" / f"{role.lower()}.md"
            schemas = {
                "PLANNER": PlanProposal,
                "NODE_RESOLVER": NodeDecision,
                "FINAL_RESPONSE": AssistantResponse,
                "ORCHESTRATOR": OrchestratorDecision,
            }
            schema = schemas.get(role)
            if isinstance(context, dict) and schema is not None and prompt_path.is_file():
                instructions = prompt_path.read_text(encoding="utf-8")
                rendered = render(instructions, context, schema.model_json_schema())
                request = {
                    "role": role,
                    "prompt": {
                        "role": role,
                        "instructions": rendered,
                    },
                    "rendered_instructions": rendered,
                }
                request["prompt_chars"] = len(request["rendered_instructions"])
        if request and not include_prompt:
            request = {
                key: value
                for key, value in request.items()
                if key not in {"prompt", "rendered_instructions", "instructions"}
            }
        payload = {**payload, "request": request}
        payload.pop("context", None)
    return {
        **event.model_dump(mode="json"),
        "payload": payload,
    }


def _task_json(task) -> dict:
    return {
        **task.model_dump(mode="json"),
        "priority_label": priority_label(task.priority),
        "final_response": task.runtime.final_response,
    }


async def needs_attention(repository, limit: int = 20) -> list[dict]:
    """Tasks parked because a person has to decide something.

    A waiting task does not block the queue: the runtime keeps dispatching the
    others. What was missing is being told. Schedule holders also live in
    ``WAITING`` (that is how recurrence is expressed), so they are excluded —
    a permanent timer is not a question waiting for you.
    """
    from ..domain.models import NodeStatus, TaskStatus

    try:
        tasks = await repository.list_tasks_by_status({TaskStatus.WAITING})
    except (AttributeError, TypeError):
        return []
    items: list[dict] = []
    for task in tasks:
        metadata = task.metadata or {}
        if metadata.get("schedule"):
            continue
        try:
            nodes = await repository.list_nodes(task.id)
        except (AttributeError, TypeError):
            nodes = []
        waiting = [node for node in nodes if node.status is NodeStatus.WAITING]
        clarification = task.runtime.clarification or {}
        question = (
            (waiting[0].error if waiting else None)
            or task.failure_reason
            or clarification.get("question")
            or ("choose a project" if clarification else None)
            or "the task needs your input to continue"
        )
        items.append(
            {
                "task_id": task.id,
                "title": task.title or task.goal,
                "goal": (task.goal or "")[:200],
                "question": str(question)[:400],
                "node_id": waiting[0].id if waiting else None,
                "kind": "project_selection" if clarification else "question",
                "options": [
                    item.get("name") or item.get("id")
                    for item in clarification.get("options", [])
                ][:10],
                "since": task.created_at.isoformat() if task.created_at else None,
                "answer_with": f"POST /api/v1/tasks/{task.id}/input",
            }
        )
    return items[:limit]


def project_view(project) -> dict:
    """Project payload without the stored graph payload.

    The full index is megabytes of modules, symbols and edges (1.3 MB for this
    repository, and it grows with the project). It is prompt material for the
    engine, not something a client needs to list projects or draw a card, so the
    response carries its shape (counts, version, partiality) instead of its bulk.
    """
    data = project.model_dump(mode="json")
    graph = data.pop("codegraph", None) or {}
    summary = {
        key: graph.get(key)
        for key in ("root", "file_count", "project_kind", "languages", "truncated")
        if graph.get(key) not in (None, [], {})
    }
    nodes = (graph.get("graph") or {}).get("nodes") or []
    edges = (graph.get("graph") or {}).get("edges") or graph.get("dependency_edges") or []
    if graph:
        summary["module_count"] = sum(1 for node in nodes if node.get("kind") == "module")
        summary["symbol_count"] = sum(1 for node in nodes if node.get("kind") == "symbol")
        summary["edge_count"] = len(edges)
        summary["partial"] = bool(
            graph.get("truncated")
            or (graph.get("graph") or {}).get("edges_truncated")
            or (graph.get("graph") or {}).get("symbols_truncated")
        )
    data["codegraph"] = summary or None
    return data


def _usage_tokens(usage: dict) -> tuple[int, int]:
    return (
        int(usage.get("prompt_tokens", usage.get("prompt_eval_count", 0)) or 0),
        int(usage.get("completion_tokens", usage.get("eval_count", 0)) or 0),
    )


def _percentile(values: list[float], ratio: float) -> float:
    """Nearest-rank percentile; empty input yields 0.0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return round(float(ordered[index]), 3)


def _metrics_series(events, hours: int = 24, now: datetime | None = None) -> list[dict]:
    """Hourly rollup derived from the retained event window.

    There is no history table: the series is bounded by event retention, which
    is reported as its basis so it is never mistaken for a permanent ledger.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC)
    hours = max(1, min(int(hours), 168))
    buckets: dict[str, dict] = {}
    for offset in range(hours - 1, -1, -1):
        moment = (current - timedelta(hours=offset)).replace(
            minute=0, second=0, microsecond=0
        )
        key = moment.isoformat()
        buckets[key] = {
            "hour": key,
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "latency_ms_total": 0,
            "tool_calls": 0,
            "tool_failures": 0,
            "tasks_created": 0,
            "tasks_finished": 0,
        }
    for event in events:
        created = event.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        key = created.astimezone(UTC).replace(minute=0, second=0, microsecond=0).isoformat()
        bucket = buckets.get(key)
        if bucket is None:
            continue
        payload = event.payload or {}
        if event.event_type == "LLM_RESPONSE":
            bucket["llm_calls"] += 1
            usage = payload.get("usage") or {}
            usage = usage if isinstance(usage, dict) else {}
            prompt_tokens, completion_tokens = _usage_tokens(usage)
            bucket["prompt_tokens"] += prompt_tokens
            bucket["completion_tokens"] += completion_tokens
            details = usage.get("prompt_tokens_details")
            details = details if isinstance(details, dict) else {}
            completion_details = usage.get("completion_tokens_details")
            completion_details = (
                completion_details if isinstance(completion_details, dict) else {}
            )
            bucket["cached_tokens"] += max(
                int(usage.get("prompt_cache_hit_tokens", 0) or 0),
                int(details.get("cached_tokens", 0) or 0),
            )
            bucket["reasoning_tokens"] += int(
                completion_details.get("reasoning_tokens", 0) or 0
            )
            latency_ms = usage.get("provider_latency_ms")
            if isinstance(latency_ms, (int, float)):
                bucket["latency_ms_total"] += int(latency_ms)
        elif event.event_type == "TOOL_RESULT":
            bucket["tool_calls"] += 1
            if payload.get("success") is False:
                bucket["tool_failures"] += 1
        elif event.event_type == "TASK_CREATED":
            bucket["tasks_created"] += 1
        elif event.event_type in {"TASK_COMPLETED", "TASK_FAILED"}:
            bucket["tasks_finished"] += 1
    series = []
    for bucket in buckets.values():
        calls = bucket.pop("llm_calls")
        latency_total = bucket.pop("latency_ms_total")
        bucket["llm_calls"] = calls
        bucket["average_latency_ms"] = round(latency_total / calls, 1) if calls else 0
        series.append(bucket)
    return series


def _cost_estimate(
    settings, model: str, prompt: int, cached: int, completion: int
) -> dict:
    """Estimate cost from configured pricing, or report that it is unset.

    Prices are never hardcoded: a model whose tariff is unknown must not be
    presented with an invented figure.
    """
    raw = getattr(settings, "model_pricing", "") or ""
    if not raw.strip():
        return {"configured": False, "reason": "ASSISTANT_MODEL_PRICING is not set"}
    try:
        table = json.loads(raw)
    except (TypeError, ValueError):
        return {"configured": False, "reason": "ASSISTANT_MODEL_PRICING is not valid JSON"}
    price = table.get(model) if isinstance(table, dict) else None
    if not isinstance(price, dict):
        return {"configured": False, "reason": f"no pricing entry for model '{model}'"}

    def rate(key: str, fallback: float = 0.0) -> float:
        try:
            return float(price.get(key, fallback) or 0.0)
        except (TypeError, ValueError):
            return fallback

    input_rate = rate("input")
    cached_rate = rate("cached_input", input_rate)
    output_rate = rate("output")
    billable_prompt = max(0, prompt - cached)
    input_cost = billable_prompt * input_rate / 1_000_000
    cached_cost = max(0, cached) * cached_rate / 1_000_000
    output_cost = completion * output_rate / 1_000_000
    # What the cached part would have cost at the normal input rate: the
    # difference is the saving the cache actually produced, not a projection.
    saving = max(0.0, max(0, cached) * (input_rate - cached_rate) / 1_000_000)
    return {
        "configured": True,
        "currency": getattr(settings, "cost_currency", "USD"),
        "prompt_usd": round(input_cost + cached_cost, 6),
        "completion_usd": round(output_cost, 6),
        "total_usd": round(input_cost + cached_cost + output_cost, 6),
        "cached_prompt_usd": round(cached_cost, 6),
        "cache_saving_usd": round(saving, 6),
        "total_without_cache_usd": round(input_cost + cached_cost + saving + output_cost, 6),
        "rates_per_million": {
            "input": input_rate,
            "cached_input": cached_rate,
            "output": output_rate,
        },
        "basis": "configured pricing applied to measured tokens",
    }


def _dashboard_analytics(context, tasks, task_nodes, events):
    settings = getattr(context, "settings", None)
    model = getattr(context.service.llm, "model", "configured-provider")
    model_label = getattr(settings, "deepseek_model_label", None) or model
    status_counts = {}
    tool_counts = {}
    phase_counts = {}
    estimated_prompt_tokens = 0
    estimated_response_tokens = 0
    actual_prompt_tokens = 0
    actual_response_tokens = 0
    actual_usage_events = 0
    llm_response_events = 0
    prefill_seconds = 0.0
    generation_seconds = 0.0
    measured_generation_tokens = 0
    actual_total_tokens = 0
    cached_tokens = 0
    cache_miss_tokens = 0
    reasoning_tokens = 0
    provider_latency_samples = 0
    provider_latency_ms_total = 0.0
    ttft_samples = 0
    ttft_ms_total = 0.0
    llm_latency = []
    tool_latency = []
    retries = 0
    llm_requests = {
        (event.task_id, event.node_id, (event.payload or {}).get("role", "unknown")): event.created_at
        for event in events
        if event.event_type == "LLM_REQUEST"
    }
    for event in events:
        payload = event.payload or {}
        status_counts[event.event_type] = status_counts.get(event.event_type, 0) + 1
        if event.event_type == "TOOL_RESULT":
            tool = payload.get("tool")
            if tool:
                tool_counts[tool] = tool_counts.get(tool, 0) + 1
            if isinstance(payload.get("duration"), (int, float)):
                tool_latency.append(payload["duration"])
        if event.event_type == "TOOL_CALLED":
            tool = payload.get("tool")
            if tool:
                tool_counts[tool] = tool_counts.get(tool, 0) + 1
        if event.event_type in {"LLM_REQUEST", "LLM_RESPONSE"}:
            phase = payload.get("role", "unknown")
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
        if event.event_type == "LLM_REQUEST":
            chars = payload.get("context_chars", 0)
            estimated_prompt_tokens += int(chars / 4) if isinstance(chars, (int, float)) else 0
        if event.event_type == "LLM_RESPONSE":
            llm_response_events += 1
            chars = payload.get("response_chars", 0)
            estimated_response_tokens += int(chars / 4) if isinstance(chars, (int, float)) else 0
            usage = payload.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}
            prompt_tokens, completion_tokens = _usage_tokens(usage)
            actual_prompt_tokens += prompt_tokens
            actual_response_tokens += completion_tokens
            if prompt_tokens or completion_tokens:
                actual_usage_events += 1
            prompt_details = usage.get("prompt_tokens_details")
            prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
            completion_details = usage.get("completion_tokens_details")
            completion_details = completion_details if isinstance(completion_details, dict) else {}
            actual_total_tokens += int(usage.get("total_tokens", 0) or 0) or (
                prompt_tokens + completion_tokens
            )
            # DeepSeek reports cache hits both at the top level and inside
            # ``prompt_tokens_details``; other gateways use only the latter.
            cached_tokens += max(
                int(usage.get("prompt_cache_hit_tokens", 0) or 0),
                int(prompt_details.get("cached_tokens", 0) or 0),
            )
            cache_miss_tokens += int(usage.get("prompt_cache_miss_tokens", 0) or 0)
            reasoning_tokens += int(completion_details.get("reasoning_tokens", 0) or 0)
            latency_ms = usage.get("provider_latency_ms")
            if isinstance(latency_ms, (int, float)):
                provider_latency_samples += 1
                provider_latency_ms_total += float(latency_ms)
            ttft_ms = usage.get("ttft_ms")
            if isinstance(ttft_ms, (int, float)):
                ttft_samples += 1
                ttft_ms_total += float(ttft_ms)
            prefill_seconds += float(usage.get("prompt_eval_duration", 0) or 0) / 1_000_000_000
            generation_seconds += float(usage.get("eval_duration", 0) or 0) / 1_000_000_000
            measured_generation_tokens += completion_tokens
            request_key = (event.task_id, event.node_id, payload.get("role", "unknown"))
            requested_at = llm_requests.get(request_key)
            if requested_at is not None:
                llm_latency.append(max(0, (event.created_at - requested_at).total_seconds()))
        if event.event_type in {"LLM_RESPONSE", "LLM_RESPONSE_PARSED"} and isinstance(
            payload.get("elapsed_seconds"), (int, float)
        ):
            llm_latency.append(payload["elapsed_seconds"])
        if event.event_type == "RETRY_SCHEDULED":
            retries += 1
    durations = []
    task_usage = []
    node_usage = {}
    for task in tasks:
        task_task_events = [event for event in events if event.task_id == task.id]
        task_prompt = sum(
            int((event.payload or {}).get("context_chars", 0) / 4)
            for event in task_task_events
            if event.event_type == "LLM_REQUEST"
        )
        task_response = sum(
            int((event.payload or {}).get("response_chars", 0) / 4)
            for event in task_task_events
            if event.event_type == "LLM_RESPONSE"
        )
        task_actual_prompt = sum(
            _usage_tokens((event.payload or {}).get("usage") or {})[0]
            for event in task_task_events
            if event.event_type == "LLM_RESPONSE"
        )
        task_actual_response = sum(
            _usage_tokens((event.payload or {}).get("usage") or {})[1]
            for event in task_task_events
            if event.event_type == "LLM_RESPONSE"
        )
        task_actual_available = bool(task_actual_prompt or task_actual_response)
        task_measured_total = task_actual_prompt + task_actual_response
        phase_usage = {}
        for event in task_task_events:
            if event.event_type != "LLM_RESPONSE":
                continue
            payload = event.payload or {}
            role = str(payload.get("role") or "unknown")
            usage = payload.get("usage") or {}
            bucket = phase_usage.setdefault(
                role,
                {
                    "prompt_tokens": 0,
                    "response_tokens": 0,
                    "estimated_tokens": 0,
                    "prefill_seconds": 0.0,
                    "generation_seconds": 0.0,
                    "calls": 0,
                },
            )
            prompt_tokens, completion_tokens = _usage_tokens(usage)
            bucket["prompt_tokens"] += prompt_tokens
            bucket["response_tokens"] += completion_tokens
            bucket["estimated_tokens"] += int((payload.get("response_chars", 0) or 0) / 4)
            bucket["prefill_seconds"] += float(usage.get("prompt_eval_duration", 0) or 0) / 1_000_000_000
            bucket["generation_seconds"] += float(usage.get("eval_duration", 0) or 0) / 1_000_000_000
            bucket["calls"] += 1
        task_prefill_seconds = sum(
            float(((event.payload or {}).get("usage") or {}).get("prompt_eval_duration", 0) or 0) / 1_000_000_000
            for event in task_task_events if event.event_type == "LLM_RESPONSE"
        )
        task_generation_seconds = sum(
            float(((event.payload or {}).get("usage") or {}).get("eval_duration", 0) or 0) / 1_000_000_000
            for event in task_task_events if event.event_type == "LLM_RESPONSE"
        )
        for event in task_task_events:
            if not event.node_id:
                continue
            usage = node_usage.setdefault(
                event.node_id,
                {
                    "llm_calls": 0,
                    "tool_calls": 0,
                    "tool_methods": [],
                    "retry_count": 0,
                    "estimated_tokens": 0,
                    "actual_tokens": 0,
                    "actual_tokens_available": False,
                },
            )
            payload = event.payload or {}
            if event.event_type == "LLM_REQUEST":
                usage["llm_calls"] += 1
                usage["estimated_tokens"] += int(payload.get("context_chars", 0) / 4)
            elif event.event_type == "LLM_RESPONSE":
                usage["estimated_tokens"] += int(payload.get("response_chars", 0) / 4)
                provider_usage = payload.get("usage") or {}
                measured = sum(_usage_tokens(provider_usage))
                usage["actual_tokens"] += measured
                usage["actual_tokens_available"] = bool(measured)
            elif event.event_type == "TOOL_CALLED":
                usage["tool_calls"] += 1
                tool_method = f"{payload.get('tool', 'unknown')}.{payload.get('method', 'unknown')}"
                if tool_method not in usage["tool_methods"]:
                    usage["tool_methods"].append(tool_method)
            elif event.event_type in {"RETRY_SCHEDULED", "NODE_RETRY"}:
                usage["retry_count"] += 1
        node_map = {node.id: node for node in task_nodes.get(task.id, [])}
        task_retries = task.retry_count + sum(
            node.retry_count for node in task_nodes.get(task.id, [])
        )
        for node_id, node in node_map.items():
            if node_id not in node_usage:
                node_usage[node_id] = {
                    "llm_calls": 0, "tool_calls": 0, "tool_methods": [], "retry_count": 0,
                    "estimated_tokens": 0, "actual_tokens": 0,
                    "actual_tokens_available": False,
                }
            node_usage[node_id].update({
                "task_id": task.id,
                "label": node.description,
                "type": node.type.value,
                "status": node.status.value,
                "duration_seconds": round(
                    max(0, (node.finished_at - node.started_at).total_seconds())
                    if node.started_at and node.finished_at else 0, 2
                ),
                "retry_count": max(node_usage[node_id]["retry_count"], node.retry_count),
            })
        task_usage.append({
            "id": task.id,
            "goal": task.goal,
            "status": task.status.value,
            "nodes": len(task_nodes.get(task.id, [])),
            "llm_calls": sum(1 for event in task_task_events if event.event_type == "LLM_REQUEST"),
            "tool_calls": sum(1 for event in task_task_events if event.event_type == "TOOL_CALLED"),
            "retries": task_retries,
            "retry_rate": round(task_retries / max(1, len(task_nodes.get(task.id, []))) * 100, 1),
            "estimated_tokens": task_prompt + task_response,
            "actual_tokens": task_measured_total,
            "actual_tokens_available": task_actual_available,
            "token_source": "measured" if task_actual_available else "estimated_chars_divided_by_4",
            "duration_seconds": round(
                max(0, (task.finished_at - task.started_at).total_seconds())
                if task.started_at and task.finished_at else 0,
                2,
            ),
            "prefill_seconds": round(task_prefill_seconds, 2),
            "generation_seconds": round(task_generation_seconds, 2),
            "phase_usage": phase_usage,
        })
        if task.started_at and task.finished_at:
            durations.append(max(0, (task.finished_at - task.started_at).total_seconds()))
    completed = sum(1 for task in tasks if task.status.value == "SUCCEEDED")
    terminal = sum(
        1 for task in tasks
        if task.status.value in {"SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"}
    )
    llm_exchange_count = llm_response_events
    measured_exchange_count = actual_usage_events
    estimated_request_count = sum(1 for event in events if event.event_type == "LLM_REQUEST")
    phase_metrics: dict[str, dict[str, float | int]] = {}
    tool_method_counts: dict[str, int] = {}
    transition_counts: dict[str, int] = {}
    task_type_counts: dict[str, int] = {}
    node_type_counts: dict[str, int] = {}
    for event in events:
        payload = event.payload or {}
        if event.event_type in {"LLM_REQUEST", "LLM_RESPONSE"}:
            role = str(payload.get("role") or "unknown")
            bucket = phase_metrics.setdefault(role, {
                "requests": 0, "responses": 0, "estimated_prompt_tokens": 0,
                "estimated_response_tokens": 0, "actual_prompt_tokens": 0,
                "actual_response_tokens": 0, "prefill_seconds": 0.0,
                "generation_seconds": 0.0,
            })
            if event.event_type == "LLM_REQUEST":
                bucket["requests"] += 1
                bucket["estimated_prompt_tokens"] += int((payload.get("context_chars", 0) or 0) / 4)
            else:
                bucket["responses"] += 1
                bucket["estimated_response_tokens"] += int((payload.get("response_chars", 0) or 0) / 4)
                usage = payload.get("usage") or {}
                prompt_tokens, completion_tokens = _usage_tokens(usage)
                bucket["actual_prompt_tokens"] += prompt_tokens
                bucket["actual_response_tokens"] += completion_tokens
                bucket["prefill_seconds"] += float(usage.get("prompt_eval_duration", 0) or 0) / 1_000_000_000
                bucket["generation_seconds"] += float(usage.get("eval_duration", 0) or 0) / 1_000_000_000
        if event.event_type == "TOOL_RESULT":
            key = f"{payload.get('tool', 'unknown')}.{payload.get('method', 'unknown')}"
            tool_method_counts[key] = tool_method_counts.get(key, 0) + 1
        if event.event_type.startswith(("TASK_", "NODE_")) or event.event_type in {"RETRY_SCHEDULED", "USER_INPUT_REQUIRED"}:
            transition_counts[event.event_type] = transition_counts.get(event.event_type, 0) + 1
    for task in tasks:
        task_type = str(
            task.runtime.workflow
            or task.metadata.get("orchestrator_intent")
            or task.metadata.get("execution_mode")
            or "general"
        )
        task_type_counts[task_type] = task_type_counts.get(task_type, 0) + 1
        for node in task_nodes.get(task.id, []):
            node_type = node.type.value
            node_type_counts[node_type] = node_type_counts.get(node_type, 0) + 1
    llm_per_request = {
        "requests": estimated_request_count,
        "responses": llm_exchange_count,
        "estimated_prompt_tokens": round(estimated_prompt_tokens / max(1, estimated_request_count)),
        "estimated_response_tokens": round(estimated_response_tokens / max(1, llm_exchange_count)),
        "estimated_total_tokens": round((estimated_prompt_tokens + estimated_response_tokens) / max(1, llm_exchange_count)),
        "actual_prompt_tokens": round(actual_prompt_tokens / max(1, measured_exchange_count)),
        "actual_response_tokens": round(actual_response_tokens / max(1, measured_exchange_count)),
        "actual_total_tokens": round((actual_prompt_tokens + actual_response_tokens) / max(1, measured_exchange_count)),
        "actual_available": bool(measured_exchange_count),
        "average_prefill_seconds": round(prefill_seconds / max(1, measured_exchange_count), 3),
        "average_generation_seconds": round(generation_seconds / max(1, measured_exchange_count), 3),
        "average_generation_tokens_per_second": round(
            actual_response_tokens / generation_seconds, 2
        ) if generation_seconds else 0,
    }
    for bucket in phase_metrics.values():
        responses = max(1, int(bucket["responses"]))
        measured = int(bucket["actual_prompt_tokens"] or bucket["actual_response_tokens"])
        bucket["average_prefill_seconds"] = round(float(bucket["prefill_seconds"]) / responses, 3)
        bucket["average_generation_seconds"] = round(float(bucket["generation_seconds"]) / responses, 3)
        bucket["actual_available"] = bool(measured)
    # Reliability, queue health and per-worker outcomes: the signals that tell
    # an unattended run apart from a healthy one.
    tool_calls_total = 0
    tool_calls_failed = 0
    blockers: dict[str, int] = {}
    worker_outcomes: dict[str, dict[str, int]] = {}
    for task in tasks:
        worker = str(
            task.metadata.get("worker") or task.metadata.get("template") or "UNROUTED"
        )
        bucket = worker_outcomes.setdefault(
            worker, {"total": 0, "succeeded": 0, "failed": 0, "blocked": 0, "cancelled": 0}
        )
        bucket["total"] += 1
        status = task.status.value
        if status == "SUCCEEDED":
            bucket["succeeded"] += 1
        elif status == "FAILED":
            bucket["failed"] += 1
        elif status == "BLOCKED":
            bucket["blocked"] += 1
        elif status == "CANCELLED":
            bucket["cancelled"] += 1
    for event in events:
        payload = event.payload or {}
        if event.event_type == "TOOL_RESULT":
            tool_calls_total += 1
            if payload.get("success") is False:
                tool_calls_failed += 1
        elif event.event_type in {
            "NODE_BLOCKED",
            "TASK_NO_PROGRESS",
            "BUDGET_EXHAUSTED",
            "OPERATION_REJECTED",
            "INPUTS_MISSING",
            "REPLAN_FAILED",
        }:
            reason = str(
                payload.get("reason") or payload.get("budget") or event.event_type
            )[:140]
            blockers[reason] = blockers.get(reason, 0) + 1
    queue_waits = [
        (task.started_at - task.created_at).total_seconds()
        for task in tasks
        if task.started_at and task.created_at
    ]
    queued_states = {"CREATED", "QUEUED", "PLANNING", "READY"}
    running_states = {"RUNNING", "VERIFYING", "FINALIZING"}
    queue = {
        "queued": sum(1 for task in tasks if task.status.value in queued_states),
        "running": sum(1 for task in tasks if task.status.value in running_states),
        "waiting": sum(1 for task in tasks if task.status.value == "WAITING"),
        "blocked": sum(1 for task in tasks if task.status.value == "BLOCKED"),
        "average_queue_seconds": round(sum(queue_waits) / len(queue_waits), 2)
        if queue_waits
        else 0,
        "max_queue_seconds": round(max(queue_waits), 2) if queue_waits else 0,
    }
    cost = _cost_estimate(
        settings, model, actual_prompt_tokens, cached_tokens, actual_response_tokens
    )
    return {
        "model": model,
        "model_label": model_label,
        "cost": cost,
        # A provider-side context cache is a *prefix* cache, so this number is a
        # measure of how much of each prompt is identical to the previous one.
        # It is the only lever this project controls over the price per turn.
        "cache": {
            "cached_tokens": cached_tokens,
            "miss_tokens": cache_miss_tokens,
            "hit_rate": round(
                cached_tokens / (cached_tokens + cache_miss_tokens) * 100, 1
            )
            if (cached_tokens + cache_miss_tokens)
            else 0,
            "saving_usd": cost.get("cache_saving_usd") if cost.get("configured") else None,
            "currency": cost.get("currency"),
            "note": (
                "Hits are counted on the identical prefix of a prompt, so stable "
                "sections (instructions, tool catalog, schema) are placed first in "
                "every template and volatile evidence last."
            ),
            "available": bool(cached_tokens or cache_miss_tokens),
        },
        "reliability": {
            "tool_calls": tool_calls_total,
            "tool_failures": tool_calls_failed,
            "tool_success_rate": round(
                (tool_calls_total - tool_calls_failed) / tool_calls_total * 100, 1
            )
            if tool_calls_total
            else 0,
            "retries": retries,
            "blockers": dict(sorted(blockers.items(), key=lambda item: -item[1])[:10]),
        },
        "queue": queue,
        "worker_outcomes": worker_outcomes,
        "event_counts": status_counts,
        "tool_counts": tool_counts,
        "phase_counts": phase_counts,
        "estimated_tokens": {
            "prompt": estimated_prompt_tokens,
            "response": estimated_response_tokens,
            "total": estimated_prompt_tokens + estimated_response_tokens,
            "basis": "context/response characters divided by 4",
        },
        "actual_tokens": {
            "prompt": actual_prompt_tokens,
            "response": actual_response_tokens,
            "total": actual_total_tokens or actual_prompt_tokens + actual_response_tokens,
            "cached": cached_tokens,
            "cache_miss": cache_miss_tokens,
            "reasoning": reasoning_tokens,
            "cache_hit_rate": round(
                cached_tokens / (cached_tokens + cache_miss_tokens) * 100, 1
            ) if (cached_tokens + cache_miss_tokens) else 0,
            "available": bool(actual_prompt_tokens or actual_response_tokens),
            "basis": "Provider prompt_tokens/completion_tokens/total_tokens plus cache and reasoning details when provided",
        },
        "display_tokens": {
            "prompt": actual_prompt_tokens if actual_prompt_tokens else estimated_prompt_tokens,
            "response": actual_response_tokens if actual_response_tokens else estimated_response_tokens,
            "total": (
                actual_prompt_tokens + actual_response_tokens
                if actual_prompt_tokens or actual_response_tokens
                else estimated_prompt_tokens + estimated_response_tokens
            ),
            "source": (
                "measured"
                if actual_usage_events == llm_response_events and actual_usage_events
                else "measured_partial"
                if actual_usage_events
                else "estimated_chars_divided_by_4"
            ),
            "measured_responses": actual_usage_events,
            "response_events": llm_response_events,
        },
        "latency": {
            "average_task_seconds": round(sum(durations) / len(durations), 2) if durations else 0,
            "average_llm_seconds": round(sum(llm_latency) / len(llm_latency), 2) if llm_latency else 0,
            "average_tool_seconds": round(sum(tool_latency) / len(tool_latency), 2) if tool_latency else 0,
            "prefill_seconds": round(prefill_seconds, 2),
            "generation_seconds": round(generation_seconds, 2),
            "generation_tokens_per_second": round(
                measured_generation_tokens / generation_seconds, 2
            ) if generation_seconds else 0,
            # Averages hide the tail; the tail is what breaks a 24/7 worker.
            "llm_p50_seconds": _percentile(llm_latency, 0.5),
            "llm_p95_seconds": _percentile(llm_latency, 0.95),
            "tool_p50_seconds": _percentile(tool_latency, 0.5),
            "tool_p95_seconds": _percentile(tool_latency, 0.95),
            "task_p50_seconds": _percentile(durations, 0.5),
            "task_p95_seconds": _percentile(durations, 0.95),
        },
        "provider_latency": {
            "samples": provider_latency_samples,
            "average_ms": round(provider_latency_ms_total / provider_latency_samples, 1)
            if provider_latency_samples
            else 0,
            "ttft_samples": ttft_samples,
            "average_ttft_ms": round(ttft_ms_total / ttft_samples, 1)
            if ttft_samples
            else 0,
            "basis": "measured around the provider call; TTFT requires streaming",
        },
        "llm_per_request": llm_per_request,
        "phase_metrics": phase_metrics,
        "distribution": {
            "task_types": task_type_counts,
            "node_types": node_type_counts,
            "tools": tool_counts,
            "tool_methods": tool_method_counts,
            "transitions": transition_counts,
        },
        "throughput": {
            "completed_tasks": completed,
            "terminal_tasks": terminal,
            "success_rate": round(completed / terminal * 100, 1) if terminal else 0,
            "retries": retries,
        },
        "task_durations": [
            {"id": task.id, "goal": task.goal, "seconds": round(duration, 2)}
            for task, duration in zip(
                [task for task in tasks if task.started_at and task.finished_at], durations
            )
        ][:20],
        "task_usage": task_usage[:50],
        "node_usage": node_usage,
        "metrics_rows": [
            {
                **usage,
                "task_goal": next(
                    (task.goal for task in tasks if task.id == usage.get("task_id")), "Tarea"
                ),
            }
            for usage in node_usage.values()
        ],
    }


def _dashboard_devices(context) -> list[dict[str, object]]:
    definitions = context.service.tools.definitions()
    device_tools = {"device.mobile", "device.home", "device.robot"}
    computer_capabilities = sorted(
        definition.name for definition in definitions if definition.name not in device_tools
    )
    # The ACTIVE branch is this machine, so it carries the resolved host facts.
    # They are derived at request time, not read from memory, so the device
    # stays identifiable here even after the memory has been reset.
    host = host_facts()
    devices = []
    for branch in DEVICE_BRANCHES:
        capabilities = (
            computer_capabilities
            if branch.name == "computer"
            else [f"device.{branch.name}"] if f"device.{branch.name}" in device_tools else []
        )
        entry = {
            "name": branch.name,
            "status": branch.status,
            "description": branch.description,
            "platform": branch.platform,
            "transport": branch.transport,
            "capabilities": capabilities,
        }
        if branch.name == "computer" and branch.status == "ACTIVE":
            entry["host"] = host
        devices.append(entry)
    return devices
