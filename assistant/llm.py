from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field

from .domain.models import Operation, VerificationDecision


class NodeDecision(BaseModel):
    action: str
    operation: Operation | None = None
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


class VerificationResult(BaseModel):
    decision: VerificationDecision
    reason: str = ""


class LLMProvider(Protocol):
    async def decide(self, context: dict[str, Any]) -> NodeDecision: ...
    async def plan(self, context: dict[str, Any]) -> PlanProposal: ...
    async def replan(self, context: dict[str, Any]) -> NodeDecision: ...
    async def verify(self, context: dict[str, Any]) -> VerificationResult: ...


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
        return PlanProposal()

    async def replan(self, context: dict[str, Any]) -> NodeDecision:
        return NodeDecision(action="COMPLETE", reason="mock replan completed")

    async def verify(self, context: dict[str, Any]) -> VerificationResult:
        result = context.get("result", {})
        return VerificationResult(
            decision=VerificationDecision.SUCCESS
            if result.get("success")
            else VerificationDecision.RETRY
        )


class OllamaLLMProvider:
    def __init__(self, base_url: str, model: str, timeout: float | None = None, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = client or httpx.AsyncClient(timeout=timeout)

    async def check_ready(self) -> bool:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{self.base_url}/api/tags")
            response.raise_for_status()
            models = response.json().get("models", [])
            return any(item.get("name") == self.model for item in models)

    async def _ask(self, role: str, context: dict[str, Any], schema: type[BaseModel]) -> BaseModel:
        prompt_path = Path(__file__).parent / "prompts" / "v1" / f"{role.lower()}.md"
        instructions = prompt_path.read_text(encoding="utf-8")
        prompt = {
            "role": role,
            "instructions": instructions,
            "context": context,
        }
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
        return schema.model_validate(json.loads(raw) if isinstance(raw, str) else raw)

    async def decide(self, context: dict[str, Any]) -> NodeDecision:
        return await self._ask("NODE_RESOLVER", context, NodeDecision)

    async def plan(self, context: dict[str, Any]) -> PlanProposal:
        return await self._ask("PLANNER", context, PlanProposal)

    async def replan(self, context: dict[str, Any]) -> NodeDecision:
        return await self._ask("REPLANNER", context, NodeDecision)

    async def verify(self, context: dict[str, Any]) -> VerificationResult:
        return await self._ask("VERIFIER", context, VerificationResult)

    async def close(self) -> None:
        await self.client.aclose()
