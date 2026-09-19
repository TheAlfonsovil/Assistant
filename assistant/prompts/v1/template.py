"""Small, visible prompt-template renderer used by every LLM role."""

from __future__ import annotations

import json
from typing import Any

MARKERS = (
    "system_role",
    "user_prompt",
    "long_term_memory",
    "assistant_state",
    "task",
    "node",
    "dependencies",
    "completed_artifacts",
    "available_actions",
    "constraints",
    "failure_context",
    "execution_evidence",
    "output_schema",
)


def render(template: str, context: dict[str, Any], output_schema: dict[str, Any]) -> str:
    values = {
        "system_role": (
            "You are a local, persistent Assistant worker. Reason privately when useful, "
            "but never expose chain-of-thought; return only the requested JSON output."
        ),
        "user_prompt": context.get("user_prompt", ""),
        "long_term_memory": context.get("long_term_memory", []),
        "assistant_state": context.get("assistant_state", {}),
        "task": context.get("task", {}),
        "node": context.get("node", {}),
        "dependencies": context.get("dependency_results", []),
        "completed_artifacts": context.get("completed_artifacts", []),
        "available_actions": context.get("available_actions", []),
        "constraints": context.get("constraints", {}),
        "failure_context": context.get("failure_context", {}),
        "execution_evidence": context.get("events", context.get("dependency_results", [])),
        "output_schema": output_schema,
    }
    rendered = template
    for marker in MARKERS:
        rendered = rendered.replace(f"{{{{{marker}}}}}", _format(values[marker]))
    return rendered


def _format(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
