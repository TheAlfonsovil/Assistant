"""Small, visible prompt-template renderer used by every LLM role."""

from __future__ import annotations

import json
from typing import Any

MARKERS = (
    "user_prompt",
    "long_term_memory",
    "assistant_state",
    "task",
    "project",
    "codegraph",
    "execution_target",
    "node",
    "dependencies",
    "completed_artifacts",
    "available_actions",
    "constraints",
    "planner_feedback",
    "failure_context",
    "output_schema",
    "working_memory",
    "last_observation",
    "evidence",
    "audit_protocol",
    "known_targets",
    "available_workers",
    "worker",
    "template",
    "orchestration_stage",
    "worker_completion",
    "execution_evidence",
    "extra_context",
    "acceptance_criteria",
)


def render(template: str, context: dict[str, Any], output_schema: dict[str, Any]) -> str:
    values = {
        "user_prompt": context.get("user_prompt", ""),
        "long_term_memory": context.get("long_term_memory", []),
        "assistant_state": context.get("assistant_state", {}),
        "task": context.get("task", {}),
        "project": context.get("project", None),
        "codegraph": context.get("codegraph", None),
        "execution_target": context.get("execution_target", None),
        "node": context.get("node", {}),
        "dependencies": context.get("dependency_results", []),
        "completed_artifacts": context.get("completed_artifacts", []),
        "available_actions": context.get("available_actions", []),
        "constraints": context.get("constraints", {}),
        "planner_feedback": context.get("planner_feedback", ""),
        "failure_context": context.get("failure_context", {}),
        "execution_evidence": context.get(
            "execution_evidence",
            context.get("events", context.get("dependency_results", [])),
        ),
        "output_schema": output_schema,
        "working_memory": context.get("working_memory", {}),
        "last_observation": context.get("last_observation", None),
        "evidence": context.get("evidence", []),
        "audit_protocol": context.get("audit_protocol", None),
        "known_targets": context.get("known_targets", []),
        "available_workers": context.get("available_workers", []),
        "worker": context.get("worker"),
        "template": context.get("template"),
        "orchestration_stage": context.get("orchestration_stage"),
        "worker_completion": context.get("worker_completion"),
        "extra_context": context.get("extra_context"),
        "acceptance_criteria": context.get("acceptance_criteria"),
    }
    rendered = template
    for marker in MARKERS:
        rendered = rendered.replace(f"{{{{{marker}}}}}", _format(values[marker]))
    return rendered


def _format(value: Any) -> str:
    if isinstance(value, str):
        return value
    # Context objects are already schema-bounded. Compact serialization keeps
    # prompts readable while avoiding indentation tokens that add no meaning.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
