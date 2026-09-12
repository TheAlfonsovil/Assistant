from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field

from .domain.models import Operation, VerificationDecision
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
    priority: int = 0


class PlanProposal(BaseModel):
    task_id: str | None = None
    nodes: list[PlanNodeProposal] = Field(default_factory=list)
    answer: str | None = None


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
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
        trace_sink: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = client or httpx.AsyncClient(timeout=timeout)
        self.trace_sink = trace_sink

    async def check_ready(self) -> bool:
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.get(f"{self.base_url}/api/tags")
            response.raise_for_status()
            models = response.json().get("models", [])
            return any(item.get("name") == self.model for item in models)

    async def _ask(self, role: str, context: dict[str, Any], schema: type[BaseModel]) -> BaseModel:
        prompt_path = Path(__file__).parent / "prompts" / "v1" / f"{role.lower()}.md"
        instructions = prompt_path.read_text(encoding="utf-8")
        rendered_instructions = render(instructions, context, schema.model_json_schema())
        prompt = {
            "role": role,
            "instructions": rendered_instructions,
        }
        started = time.perf_counter()
        self._trace(
            {
                "phase": "LLM_REQUEST_BUILT",
                "role": role,
                "prompt_chars": len(rendered_instructions),
                "context_chars": len(json.dumps(context, default=str)),
                "sections": [line for line in rendered_instructions.splitlines() if line and line.isupper()],
                "prompt_preview": rendered_instructions[:4000],
            }
        )
        response = await self.client.post(
            f"{self.base_url}/api/generate",
            json={
                "model": self.model,
                "prompt": json.dumps(prompt, default=str),
                "stream": False,
                "format": schema.model_json_schema(),
            },
        )
        response.raise_for_status()
        payload = response.json()
        raw = payload.get("response", payload)
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            validated = schema.model_validate(parsed)
        except (TypeError, ValueError) as error:
            self._trace(
                {
                    "phase": "LLM_RESPONSE_INVALID",
                    "role": role,
                    "elapsed_seconds": round(time.perf_counter() - started, 2),
                    "raw_chars": len(raw) if isinstance(raw, str) else len(json.dumps(raw, default=str)),
                    "raw_preview": raw[:4000] if isinstance(raw, str) else raw,
                    "error": str(error),
                }
            )
            raise
        self._trace(
            {
                "phase": "LLM_RESPONSE_PARSED",
                "role": role,
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                "raw_chars": len(raw) if isinstance(raw, str) else len(json.dumps(raw, default=str)),
                "raw_preview": raw[:4000] if isinstance(raw, str) else raw,
                "validated": validated.model_dump(mode="json"),
            }
        )
        return validated

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
