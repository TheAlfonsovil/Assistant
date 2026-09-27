from __future__ import annotations

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

def _dashboard_analytics(context, tasks, task_nodes, events):
    model = getattr(context.service.llm, "model", "configured-provider")
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
            usage = payload.get("usage", {})
            actual_prompt_tokens += int(usage.get("prompt_eval_count", 0) or 0)
            actual_response_tokens += int(usage.get("eval_count", 0) or 0)
            if usage.get("prompt_eval_count") or usage.get("eval_count"):
                actual_usage_events += 1
            prefill_seconds += float(usage.get("prompt_eval_duration", 0) or 0) / 1_000_000_000
            generation_seconds += float(usage.get("eval_duration", 0) or 0) / 1_000_000_000
            measured_generation_tokens += int(usage.get("eval_count", 0) or 0)
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
            int(((event.payload or {}).get("usage") or {}).get("prompt_eval_count", 0) or 0)
            for event in task_task_events
            if event.event_type == "LLM_RESPONSE"
        )
        task_actual_response = sum(
            int(((event.payload or {}).get("usage") or {}).get("eval_count", 0) or 0)
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
            bucket["prompt_tokens"] += int(usage.get("prompt_eval_count", 0) or 0)
            bucket["response_tokens"] += int(usage.get("eval_count", 0) or 0)
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
                measured = int(provider_usage.get("prompt_eval_count", 0) or 0) + int(
                    provider_usage.get("eval_count", 0) or 0
                )
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
            "token_source": "ollama" if task_actual_available else "estimated_chars_divided_by_4",
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
                bucket["actual_prompt_tokens"] += int(usage.get("prompt_eval_count", 0) or 0)
                bucket["actual_response_tokens"] += int(usage.get("eval_count", 0) or 0)
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
    return {
        "model": model,
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
            "total": actual_prompt_tokens + actual_response_tokens,
            "available": bool(actual_prompt_tokens or actual_response_tokens),
            "basis": "Ollama prompt_eval_count/eval_count when provided",
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
                "ollama"
                if actual_usage_events == llm_response_events and actual_usage_events
                else "ollama_partial"
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
