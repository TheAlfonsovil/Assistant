from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field

from .domain.models import DependencyType, Operation, VerificationDecision
from .prompts.v1.template import render


class ActionProposal(BaseModel):
    name: str
    description: str
    language: str = "python"
    code: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    safety: str = "review_required"


class NodeDecision(BaseModel):
    action: str
    operation: Operation | None = None
    action_proposal: ActionProposal | None = None
    subtasks: list[str] = Field(default_factory=list)
    reason: str | None = None


class PlanNodeProposal(BaseModel):
    id: str
    description: str
    type: str = "OPERATION"
    dependencies: list[str] = Field(default_factory=list)
    dependency_types: dict[str, DependencyType] = Field(default_factory=dict)
    priority: int = 0
    acceptance: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlanProposal(BaseModel):
    task_id: str | None = None
    nodes: list[PlanNodeProposal] = Field(default_factory=list)
    answer: str | None = None
    subtasks: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    decision: VerificationDecision
    reason: str = ""


class AssistantResponse(BaseModel):
    response_type: str = "answer"
    title: str
    summary: str
    sections: dict[str, list[str]] = Field(default_factory=dict)
    next_actions: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    confidence: str = "medium"


# Compatibility name for callers of the first reporting implementation.
FinalReport = AssistantResponse


class LLMProvider(Protocol):
    async def decide(self, context: dict[str, Any]) -> NodeDecision: ...
    async def plan(self, context: dict[str, Any]) -> PlanProposal: ...
    async def replan(self, context: dict[str, Any]) -> NodeDecision: ...
    async def verify(self, context: dict[str, Any]) -> VerificationResult: ...
    async def respond(self, context: dict[str, Any]) -> AssistantResponse: ...


class MockLLMProvider:
    def __init__(self, operation: Operation | None = None):
        self.operation = operation or Operation(
            tool="filesystem", method="exists", args={"path": "."}
        )
        self.decisions = 0

    async def check_ready(self) -> bool:
        return True

    async def decide(self, context: dict[str, Any]) -> NodeDecision:
        self.decisions += 1
        return NodeDecision(action="OPERATION", operation=self.operation)

    async def plan(self, context: dict[str, Any]) -> PlanProposal:
        return PlanProposal(
            nodes=[
                PlanNodeProposal(
                    id="mock-operation",
                    description="execute configured mock operation",
                    type="OPERATION",
                )
            ]
        )

    async def replan(self, context: dict[str, Any]) -> NodeDecision:
        return NodeDecision(action="COMPLETE", reason="mock replan completed")

    async def verify(self, context: dict[str, Any]) -> VerificationResult:
        result = context.get("result", {})
        return VerificationResult(
            decision=VerificationDecision.SUCCESS
            if result.get("success")
            else VerificationDecision.RETRY
        )

    async def respond(self, context: dict[str, Any]) -> AssistantResponse:
        return AssistantResponse(
            response_type="report" if context.get("requested_format") == "report" else "answer",
            title="Assistant response",
            summary="Task completed with the configured mock provider.",
            evidence=[f"events={len(context.get('events', []))}"],
            confidence="high",
        )

    async def summarize(self, context: dict[str, Any]) -> AssistantResponse:
        return await self.respond(context)


class OllamaLLMProvider:
    DEFAULT_REASONING_POLICY = {
        "PLANNER": "medium",
        "NODE_RESOLVER": "low",
        "REPLANNER": "high",
        "VERIFIER": "off",
        "FINAL_RESPONSE": "low",
    }

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
        temperature: float = 0.1,
        num_ctx: int = 32768,
        thinking: bool = False,
        reasoning_effort: str = "low",
        reasoning_policy: str = "",
        context_reserve_tokens: int = 4096,
        trace_sink: Callable[[dict[str, Any]], None] | None = None,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        max_prompt_chars: int = 200_000,
        max_response_chars: int = 1_000_000,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = client or httpx.AsyncClient(timeout=timeout)
        self.temperature = temperature
        self.num_ctx = max(1024, num_ctx)
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort if reasoning_effort in {"low", "medium", "high", "xhigh"} else "low"
        self.reasoning_policy = self._parse_reasoning_policy(reasoning_policy)
        self.context_reserve_tokens = max(256, min(context_reserve_tokens, self.num_ctx - 256))
        self.trace_sink = trace_sink
        self.request_timeout = timeout if timeout is not None else 300.0
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout = max(0.1, recovery_timeout)
        self.max_prompt_chars = max(1, max_prompt_chars)
        self.effective_prompt_chars = min(
            self.max_prompt_chars,
            (self.num_ctx - self.context_reserve_tokens) * 4,
        )
        self.max_response_chars = max(1, max_response_chars)
        self._consecutive_failures = 0
        self._circuit_opened_at: float | None = None
        self.last_usage: dict[str, int] = {}
        self.last_request: dict[str, Any] = {}

    @property
    def circuit_state(self) -> str:
        if self._circuit_opened_at is None:
            return "CLOSED"
        if time.monotonic() - self._circuit_opened_at >= self.recovery_timeout:
            return "HALF_OPEN"
        return "OPEN"

    def _ensure_circuit_available(self) -> None:
        if self.circuit_state == "OPEN":
            raise RuntimeError("LLM circuit breaker is open")

    @classmethod
    def _parse_reasoning_policy(cls, value: str) -> dict[str, str]:
        policy = cls.DEFAULT_REASONING_POLICY.copy()
        for item in value.split(","):
            role, separator, effort = item.partition(":")
            if separator and role.strip() and effort.strip() in {"off", "low", "medium", "high", "xhigh"}:
                policy[role.strip().upper()] = effort.strip()
        return policy

    def _thinking_for_role(self, role: str) -> str | bool:
        if not self.thinking:
            return False
        effort = self.reasoning_policy.get(role, self.reasoning_effort)
        return False if effort == "off" else effort

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_opened_at = None

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._circuit_opened_at = time.monotonic()

    async def check_ready(self) -> bool:
        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            response = await asyncio.wait_for(
                client.get(f"{self.base_url}/api/tags"), timeout=self.request_timeout
            )
            response.raise_for_status()
            models = response.json().get("models", [])
            return any(item.get("name") == self.model for item in models)

    async def _ask(self, role: str, context: dict[str, Any], schema: type[BaseModel]) -> BaseModel:
        self._ensure_circuit_available()
        prompt_path = Path(__file__).parent / "prompts" / "v1" / f"{role.lower()}.md"
        instructions = prompt_path.read_text(encoding="utf-8")
        rendered_instructions = render(instructions, context, schema.model_json_schema())
        if len(rendered_instructions) > self.effective_prompt_chars:
            raise ValueError(
                f"LLM prompt exceeds effective context budget of {self.effective_prompt_chars} characters"
            )
        prompt = {
            "role": role,
            "instructions": rendered_instructions,
        }
        request_schema = self._request_schema(role, schema)
        self.last_request = {
            "role": role,
            "prompt": prompt,
            "rendered_instructions": rendered_instructions,
            "prompt_chars": len(rendered_instructions),
        }
        started = time.perf_counter()
        self._trace(
            {
                "phase": "LLM_REQUEST_BUILT",
                "role": role,
                "prompt_chars": len(rendered_instructions),
                "context_chars": len(json.dumps(context, default=str)),
                "thinking": self._thinking_for_role(role),
                "sections": [line for line in rendered_instructions.splitlines() if line and line.isupper()],
                "prompt_preview": rendered_instructions[:4000],
            }
        )
        try:
            response = await asyncio.wait_for(
                self.client.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": json.dumps(prompt, default=str),
                        "stream": False,
                        "format": request_schema,
                        "options": {
                            "temperature": self.temperature,
                            "num_ctx": self.num_ctx,
                        },
                        "think": self._thinking_for_role(role),
                    },
                ),
                timeout=self.request_timeout,
            )
            response.raise_for_status()
            payload = response.json()
            raw = payload.get("response", payload)
            self.last_usage = {
                key: int(payload[key])
                for key in (
                    "prompt_eval_count",
                    "eval_count",
                    "prompt_eval_duration",
                    "eval_duration",
                    "load_duration",
                    "total_duration",
                )
                if isinstance(payload.get(key), (int, float))
            }
            raw_chars = len(raw) if isinstance(raw, str) else len(json.dumps(raw, default=str))
            if raw_chars > self.max_response_chars:
                raise ValueError(
                    f"LLM response exceeds limit of {self.max_response_chars} characters"
                )
        except (httpx.HTTPError, TimeoutError, ValueError):
            self._record_failure()
            raise
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            validated = schema.model_validate(parsed)
        except (TypeError, ValueError) as error:
            self._record_failure()
            self._trace(
                {
                    "phase": "LLM_RESPONSE_INVALID",
                    "role": role,
                    "elapsed_seconds": round(time.perf_counter() - started, 2),
                    "raw_chars": len(raw) if isinstance(raw, str) else len(json.dumps(raw, default=str)),
                    "usage": self.last_usage,
                    "raw_preview": raw[:4000] if isinstance(raw, str) else raw,
                    "error": str(error),
                }
            )
            raise
        self._record_success()
        self._trace(
            {
                "phase": "LLM_RESPONSE_PARSED",
                "role": role,
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                "raw_chars": len(raw) if isinstance(raw, str) else len(json.dumps(raw, default=str)),
                "usage": self.last_usage,
                "raw_preview": raw[:4000] if isinstance(raw, str) else raw,
                "validated": validated.model_dump(mode="json"),
            }
        )
        return validated

    @staticmethod
    def _request_schema(role: str, schema: type[BaseModel]) -> dict[str, Any]:
        """Make planner alternatives mutually exclusive for constrained decoding.

        The base Pydantic schema gives every list a default empty value.  That is
        useful for deserialization, but it also tells the model that an empty
        executable plan is valid.  The planner must choose a direct answer or
        at least one executable node/subtask.
        """
        result = copy.deepcopy(schema.model_json_schema())
        if role != "PLANNER":
            return result

        result["anyOf"] = [
            {
                "required": ["answer"],
                "properties": {"answer": {"type": "string", "minLength": 1}},
            },
            {"required": ["nodes"], "properties": {"nodes": {"minItems": 1}}},
            {"required": ["subtasks"], "properties": {"subtasks": {"minItems": 1}}},
        ]
        return result

    def _trace(self, payload: dict[str, Any]) -> None:
        if self.trace_sink:
            self.trace_sink(payload)

    async def decide(self, context: dict[str, Any]) -> NodeDecision:
        return await self._ask("NODE_RESOLVER", context, NodeDecision)

    async def plan(self, context: dict[str, Any]) -> PlanProposal:
        return await self._ask("PLANNER", context, PlanProposal)

    async def replan(self, context: dict[str, Any]) -> NodeDecision:
        return await self._ask("REPLANNER", context, NodeDecision)

    async def verify(self, context: dict[str, Any]) -> VerificationResult:
        return await self._ask("VERIFIER", context, VerificationResult)

    async def respond(self, context: dict[str, Any]) -> AssistantResponse:
        return await self._ask("FINAL_RESPONSE", context, AssistantResponse)

    async def summarize(self, context: dict[str, Any]) -> AssistantResponse:
        return await self.respond(context)

    async def close(self) -> None:
        await self.client.aclose()
