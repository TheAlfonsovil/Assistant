from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol

import httpx
from pydantic import BaseModel, Field

from .attachments import image_parts
from .domain.contracts import (
    AcceptanceCriterion,
    BranchConfig,
    CriterionResult,
    Diagnostic,
    FailurePolicy,
    IdempotencyPolicy,
    InputRef,
    OperationHint,
    OutputSpec,
    RetryPolicy,
)
from .domain.models import AgentDecision, DependencyType, Operation, VerificationDecision
from .prompts.template import render

# Usage and the rendered request are per-call results, not provider state.
# Context variables isolate them per asyncio task, so concurrent tasks can no
# longer read each other's token counts or prompts. Each ``asyncio.Task`` gets
# its own copy of the current context, which is exactly the isolation needed.
_LAST_USAGE: ContextVar[dict[str, Any] | None] = ContextVar("llm_last_usage", default=None)
_LAST_REQUEST: ContextVar[dict[str, Any] | None] = ContextVar("llm_last_request", default=None)


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
    user_input_required: bool = False


class OrchestratorDecision(BaseModel):
    """Routing decision made before any worker is allowed to execute."""

    stage: str = Field(default="ROUTE", pattern="^(ROUTE|FINALIZE|CONTINUE|BLOCK)$")
    intent: str = Field(default="general", min_length=1, max_length=120)
    target_type: str | None = Field(default=None, pattern="^(project|device|resource)$")
    target_id: str | None = None
    worker: str = Field(default="GENERAL_WORKER", min_length=1, max_length=120)
    template: str = Field(default="general", min_length=1, max_length=120)
    extra_context: dict[str, Any] = Field(default_factory=dict)
    acceptance_criteria: list[str] = Field(default_factory=list)
    reason: str = Field(default="", max_length=4000)
    needs_input: bool = False
    clarification: str | None = Field(default=None, max_length=2000)


class PlanNodeProposal(BaseModel):
    id: str
    description: str
    type: str = "OPERATION"
    dependencies: list[str] = Field(default_factory=list)
    dependency_types: dict[str, DependencyType] = Field(default_factory=dict)
    priority: int = 1
    acceptance: dict[str, Any] = Field(default_factory=dict)
    inputs: list[InputRef] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    acceptance_criteria: list[str | AcceptanceCriterion] = Field(default_factory=list)
    deadline: datetime | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    idempotency_policy: IdempotencyPolicy = Field(default_factory=IdempotencyPolicy)
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)
    metadata: dict[str, Any] = Field(default_factory=dict)
    operation_hint: OperationHint | None = None
    branch_config: BranchConfig = Field(default_factory=BranchConfig)



class PlanProposal(BaseModel):
    task_id: str | None = None
    nodes: list[PlanNodeProposal] = Field(default_factory=list)
    answer: str | None = None
    subtasks: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    decision: VerificationDecision
    reason: str = ""
    criteria_results: list[CriterionResult] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)


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


def parse_agent_decision(raw: str | dict[str, Any]) -> AgentDecision:
    """Parse one structured agent decision without coupling it to TaskService."""

    if isinstance(raw, str):
        return AgentDecision.model_validate_json(raw)
    return AgentDecision.model_validate(raw)


def _compact_schema(value: Any) -> Any:
    """Remove prose-only JSON Schema metadata before embedding it in prompts."""
    if isinstance(value, dict):
        return {
            key: _compact_schema(item)
            for key, item in value.items()
            if key not in {"title", "description", "default", "examples"}
        }
    if isinstance(value, list):
        return [_compact_schema(item) for item in value]
    return value


def _planner_schema() -> dict[str, Any]:
    """Describe planner output without embedding resolver-only contracts."""
    return {
        "type": "object",
        "properties": {
            "task_id": {"type": ["string", "null"]},
            "answer": {"type": ["string", "null"]},
            "coverage": {"type": "array", "items": {"type": "string"}},
            "subtasks": {"type": "array", "items": {"type": "string"}},
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "type": {"type": "string"},
                        "dependencies": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "acceptance_criteria": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "allowed_tools": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["id", "description"],
                },
            },
        },
        "required": ["nodes", "answer"],
    }


# Compatibility name for callers of the first reporting implementation.
FinalReport = AssistantResponse


class LLMProvider(Protocol):
    async def orchestrate(self, context: dict[str, Any]) -> OrchestratorDecision: ...
    async def agent_decide(self, context: dict[str, Any]) -> AgentDecision: ...
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

    async def orchestrate(self, context: dict[str, Any]) -> OrchestratorDecision:
        if context.get("orchestration_stage") == "REVIEW":
            return OrchestratorDecision(
                stage="FINALIZE",
                intent=context.get("intent") or "general",
                worker=context.get("worker", "GENERAL_WORKER"),
                template=context.get("template", "general"),
                reason="The worker completed and its evidence is ready for final response.",
            )
        target = context.get("execution_target") or {}
        project = context.get("project") or {}
        if target:
            return OrchestratorDecision(
                intent="general",
                target_type=target.get("type"),
                target_id=target.get("id"),
                worker="GENERAL_WORKER",
                template="general",
                reason="Use the target supplied with the task.",
            )
        if project:
            return OrchestratorDecision(
                intent="general",
                target_type="project",
                target_id=project.get("id"),
                worker="GENERAL_WORKER",
                template="general",
                reason="Use the project attached to the task.",
            )
        return OrchestratorDecision(
            intent="general",
            worker="GENERAL_WORKER",
            template="general",
            reason="No explicit target was required for this operation.",
        )

    async def agent_decide(self, context: dict[str, Any]) -> AgentDecision:
        self.decisions += 1
        if context.get("last_observation") is None:
            return AgentDecision(
                decision_type="EXECUTE",
                reason="Inspect the target before deciding the next evidence step.",
                operation=self.operation,
            )
        return AgentDecision(
            decision_type="COMPLETE",
            reason="The configured operation completed and no further step is required.",
        )

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


class _PromptLLMProvider:
    DEFAULT_REASONING_POLICY: ClassVar[dict[str, str]] = {
        "ORCHESTRATOR": "high",
        "AGENT": "high",
        "PLANNER": "high",
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
        thinking: bool = False,
        reasoning_effort: str = "low",
        reasoning_policy: str = "",
        trace_sink: Callable[[dict[str, Any]], None] | None = None,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        max_prompt_chars: int = 200_000,
        max_response_chars: int = 1_000_000,
        supports_vision: bool = False,
        max_image_bytes: int = 5_000_000,
        stream_responses: bool = False,
        max_vision_images: int = 1,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = client or httpx.AsyncClient(timeout=timeout)
        self.temperature = temperature
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort if reasoning_effort in {"low", "high", "max"} else "low"
        self.reasoning_policy = self._parse_reasoning_policy(reasoning_policy)
        self.trace_sink = trace_sink
        self.request_timeout = timeout if timeout is not None else 300.0
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout = max(0.1, recovery_timeout)
        self.max_prompt_chars = max(1, max_prompt_chars)
        self.effective_prompt_chars = self.max_prompt_chars
        self.max_response_chars = max(1, max_response_chars)
        self.supports_vision = bool(supports_vision)
        self.max_image_bytes = max(1, int(max_image_bytes))
        # Streaming unlocks time-to-first-token and avoids buffering a large
        # response, at the cost of an SSE-capable endpoint.
        self.stream_responses = bool(stream_responses)
        # Embedding images costs tokens on every turn, so the newest one wins.
        self.max_vision_images = max(1, int(max_vision_images))
        self._consecutive_failures = 0
        self._circuit_opened_at: float | None = None
        self.last_usage = {}
        self.last_request = {}

    @property
    def last_usage(self) -> dict[str, Any]:
        """Token usage of the most recent call **in the current asyncio task**."""
        return _LAST_USAGE.get() or {}

    @last_usage.setter
    def last_usage(self, value: dict[str, Any]) -> None:
        _LAST_USAGE.set(dict(value or {}))

    @property
    def last_request(self) -> dict[str, Any]:
        """Rendered request of the most recent call **in the current asyncio task**."""
        return _LAST_REQUEST.get() or {}

    @last_request.setter
    def last_request(self, value: dict[str, Any]) -> None:
        _LAST_REQUEST.set(dict(value or {}))

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
            if separator and role.strip() and effort.strip() in {"off", "none", "low", "high", "max"}:
                policy[role.strip().upper()] = effort.strip()
        return policy

    def _thinking_for_role(self, role: str) -> str | bool:
        if not self.thinking:
            return False
        effort = self.reasoning_policy.get(role, self.reasoning_effort)
        return False if effort in {"off", "none"} else effort

    def prepare_request(
        self, role: str, context: dict[str, Any], schema: type[BaseModel]
    ) -> dict[str, Any]:
        """Build and retain the exact request before network I/O starts."""
        prompt_name = role.lower()
        if role == "AGENT":
            prompt_name = {
                "audit": "audit_worker",
                "browser": "browser_worker",
                "implementation": "code_worker",
                "edit": "code_worker",
                "tests": "test_worker",
                "test": "test_worker",
                "codegraph": "codegraph_worker",
                "research": "research_worker",
                "general": "general_worker",
            }.get(str(context.get("template", "general")), "general_worker")
        prompt_path = Path(__file__).parent / "prompts" / f"{prompt_name}.md"
        instructions = prompt_path.read_text(encoding="utf-8")
        if role == "AGENT":
            instructions = (
                "Worker decision policy: use EXECUTE for one safe tool operation, DELEGATE "
                "for bounded independent child tasks, COMPLETE only with evidence, and "
                "WAIT, ASK_USER, or FAIL when progress cannot continue safely. "
                "Each EXECUTE operation must use registered fields tool and method separately "
                "(example: tool=codegraph, method=query), never a dotted tool name. "
                "For GUI work: screen.capture first, then act with input.* using absolute "
                "screen coordinates (screen_x = origin_x + image_x / scale from the capture "
                "artifact), and capture again to verify the result before claiming success.\n\n"
                + instructions
            )
        output_schema = (
            _planner_schema()
            if role == "PLANNER"
            else _compact_schema(schema.model_json_schema())
        )
        rendered_instructions = render(instructions, context, output_schema)
        # Every role and worker template must see attachments and fresh screen
        # captures, so the blocks are prepended here instead of relying on
        # thirteen prompt files each remembering a marker. Bytes are never
        # inlined: the model gets paths, sizes, coordinates and whether it can
        # actually view them.
        preamble: list[str] = []
        attachments = context.get("attachments")
        if isinstance(attachments, dict) and attachments.get("items"):
            preamble.append(
                "ATTACHMENTS\n" + json.dumps(attachments, ensure_ascii=False, default=str)
            )
        screenshots = context.get("screenshots")
        if screenshots:
            preamble.append(
                "SCREENSHOTS\n"
                + json.dumps(screenshots, ensure_ascii=False, default=str)
                + "\nConvert image pixels to absolute screen coordinates with "
                "screen_x = origin_x + image_x / scale (and the same for y)."
            )
        if preamble:
            rendered_instructions = "\n\n".join(preamble) + "\n\n" + rendered_instructions
        if len(rendered_instructions) > self.effective_prompt_chars:
            raise ValueError(
                f"LLM prompt exceeds effective context budget of {self.effective_prompt_chars} characters"
            )
        request = {
            "role": role,
            "prompt": {
                "role": role,
                "instructions": rendered_instructions,
            },
            "rendered_instructions": rendered_instructions,
            "prompt_chars": len(rendered_instructions),
            "prompt_preview": rendered_instructions[:4000],
        }
        self.last_request = request
        self._trace(
            {
                "phase": "LLM_REQUEST_BUILT",
                "role": role,
                "prompt_chars": len(rendered_instructions),
                "context_chars": len(json.dumps(context, default=str)),
                "thinking": self._thinking_for_role(role),
                "sections": [
                    line for line in rendered_instructions.splitlines() if line and line.isupper()
                ],
                "prompt_preview": rendered_instructions[:4000],
            }
        )
        return request

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_opened_at = None

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._circuit_opened_at = time.monotonic()

    def _trace(self, payload: dict[str, Any]) -> None:
        if self.trace_sink:
            self.trace_sink(payload)

    async def decide(self, context: dict[str, Any]) -> NodeDecision:
        return await self._ask("NODE_RESOLVER", context, NodeDecision)

    async def agent_decide(self, context: dict[str, Any]) -> AgentDecision:
        return await self._ask("AGENT", context, AgentDecision)

    async def orchestrate(self, context: dict[str, Any]) -> OrchestratorDecision:
        return await self._ask("ORCHESTRATOR", context, OrchestratorDecision)

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


class DeepSeekLLMProvider(_PromptLLMProvider):
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 600.0,
        max_tokens: int = 16384,
        max_prompt_chars: int = 240_000,
        max_response_chars: int = 250_000,
        thinking: bool = True,
        reasoning_policy: str = "",
        temperature: float = 0.1,
        trace_sink: Callable[[dict[str, Any]], None] | None = None,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        supports_vision: bool = False,
        max_image_bytes: int = 5_000_000,
        stream_responses: bool = False,
        max_vision_images: int = 1,
    ):
        super().__init__(
            base_url,
            model,
            timeout,
            client=client,
            temperature=temperature,
            thinking=thinking,
            reasoning_policy=reasoning_policy,
            max_prompt_chars=max_prompt_chars,
            max_response_chars=max_response_chars,
            trace_sink=trace_sink,
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            supports_vision=supports_vision,
            max_image_bytes=max_image_bytes,
            stream_responses=stream_responses,
            max_vision_images=max_vision_images,
        )
        self.api_key = api_key
        self.max_tokens = max_tokens

    async def check_ready(self) -> bool:
        """Report whether the configured model is usable, without ever raising.

        ``/models`` is the cheap check, but a compatible gateway may not expose
        it, may answer with a non-JSON body, or may not advertise every model it
        serves. When that list is unavailable or does not mention the configured
        model, fall back to a one-token completion probe so a working deployment
        is never reported as unready (which would silently stall the runtime).
        """
        if not self.api_key:
            return False
        advertised = await self._advertised_models()
        if advertised is not None and self.model in advertised:
            return True
        return await self._probe_completion()

    async def _advertised_models(self) -> set[str] | None:
        """Return model ids from ``/models``; ``None`` when it is unusable."""
        try:
            response = await asyncio.wait_for(
                self.client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                ),
                timeout=min(self.request_timeout, 5.0),
            )
        except (httpx.HTTPError, OSError, TimeoutError, ValueError):
            return None
        if response.status_code >= 400:
            return None
        try:
            data = response.json().get("data") or []
        except ValueError:
            return None
        return {
            str(item.get("id"))
            for item in data
            if isinstance(item, dict) and item.get("id")
        }

    async def _probe_completion(self) -> bool:
        """Confirm the model answers a minimal request.

        Deliberately bypasses ``chat`` so a probe never perturbs the circuit
        breaker, the failure counters or ``last_usage``.
        """
        try:
            response = await asyncio.wait_for(
                self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                        "stream": False,
                    },
                ),
                timeout=min(self.request_timeout, 20.0),
            )
        except (httpx.HTTPError, OSError, TimeoutError, ValueError):
            return False
        return 200 <= response.status_code < 300

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        reasoning_effort: str = "high",
        response_format: dict[str, str] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        stop: str | list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        logprobs: bool | None = None,
        top_logprobs: int | None = None,
        user_id: str | None = None,
        stream: bool = False,
        _defer_success: bool = False,
    ) -> dict[str, Any]:
        self._ensure_circuit_available()
        if not self.api_key:
            raise RuntimeError("DeepSeek API key is not configured")
        if not messages or any(message.get("role") not in {"system", "user", "assistant", "tool"} for message in messages):
            raise ValueError("Expected chat messages with system, user, assistant or tool roles")
        if any(
            message["role"] != "user" and isinstance(message.get("content"), list)
            and any(part.get("type") in {"image_url", "file"} for part in message["content"])
            for message in messages
        ):
            raise ValueError("Images are supported only in user messages")
        if reasoning_effort not in {"none", "low", "high", "max"}:
            raise ValueError("Invalid DeepSeek reasoning effort")
        if not 1 <= (self.max_tokens if max_tokens is None else max_tokens) <= 393216:
            raise ValueError("max_tokens must be between 1 and 393216")
        if response_format is not None and response_format.get("type") not in {"text", "json_object"}:
            raise ValueError("DeepSeek supports text and json_object response formats")
        if top_logprobs is not None and (logprobs is not True or not 0 <= top_logprobs <= 20):
            raise ValueError("top_logprobs requires logprobs=true and a value between 0 and 20")
        if isinstance(stop, list) and len(stop) > 16:
            raise ValueError("DeepSeek supports at most 16 stop sequences")
        if reasoning_effort != "none" and tool_choice not in (None, "auto", "none"):
            raise ValueError("Required or named tool_choice requires non-thinking mode")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "response_format": response_format or {"type": "text"},
            "thinking": {"type": "disabled" if reasoning_effort == "none" else "enabled"},
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": stream,
        }
        if reasoning_effort != "none":
            body["reasoning_effort"] = reasoning_effort
        elif temperature is not None:
            body["temperature"] = temperature
        for name, value in (
            ("top_p", top_p), ("stop", stop), ("tools", tools), ("tool_choice", tool_choice),
            ("logprobs", logprobs), ("top_logprobs", top_logprobs), ("user_id", user_id),
        ):
            if value is not None:
                body[name] = value
        if stream:
            body["stream_options"] = {"include_usage": True}
        started = time.perf_counter()
        self.last_usage = {}
        try:
            if stream:
                payload = await self._stream_chat(body)
            else:
                response = await asyncio.wait_for(
                    self.client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=body,
                    ),
                    timeout=self.request_timeout,
                )
                response.raise_for_status()
                payload = response.json()
            usage = dict(payload.get("usage") or {})
            # Timing travels with the usage sample. The ledger already reads
            # provider timing from ``usage`` (prompt_eval_duration/eval_duration),
            # so this adds real latency and TTFT without a new event field, and
            # it survives event retention through the task's persisted totals.
            usage["provider_latency_ms"] = round((time.perf_counter() - started) * 1000)
            if isinstance(payload.get("ttft_ms"), int):
                usage["ttft_ms"] = payload["ttft_ms"]
            self.last_usage = usage
            if not _defer_success:
                self._record_success()
            return payload
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, IndexError, TypeError):
            self._record_failure()
            raise

    async def _stream_chat(self, body: dict[str, Any]) -> dict[str, Any]:
        content: list[str] = []
        reasoning: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason = None
        usage = None
        content_chars = 0
        started = time.perf_counter()
        first_token_at: float | None = None
        async with asyncio.timeout(self.request_timeout):
            async with self.client.stream(
                "POST", f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"}, json=body,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        fragment = delta.get("content") or ""
                        if first_token_at is None and (
                            fragment or delta.get("reasoning_content")
                        ):
                            first_token_at = time.perf_counter()
                        content_chars += len(fragment)
                        if content_chars > self.max_response_chars:
                            raise ValueError("LLM response exceeds the response limit")
                        content.append(fragment)
                        reasoning.append(delta.get("reasoning_content") or "")
                        for call in delta.get("tool_calls") or []:
                            index = call["index"]
                            target = tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            target["id"] += call.get("id") or ""
                            function = call.get("function") or {}
                            target["function"]["name"] += function.get("name") or ""
                            target["function"]["arguments"] += function.get("arguments") or ""
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if tool_calls:
            message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
        result: dict[str, Any] = {
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": usage,
        }
        if first_token_at is not None:
            result["ttft_ms"] = int((first_token_at - started) * 1000)
        return result

    def _user_content(self, request: dict[str, Any], context: dict[str, Any]) -> Any:
        """Return the user message content, with image parts when supported.

        Images are sent only when the configured model can read them; otherwise
        the rendered prompt keeps the references and the model is told
        explicitly not to pretend it saw them. Attachments and fresh screen
        captures share the budget so a GUI loop cannot flood the prompt.
        """
        rendered = request["rendered_instructions"]
        if not self.supports_vision:
            return rendered
        references: list[dict[str, Any]] = []
        for key in ("attachments", "screenshots"):
            container = context.get(key)
            items = container.get("items") if isinstance(container, dict) else container
            if isinstance(items, list):
                references.extend(item for item in items if isinstance(item, dict))
        if not references:
            return rendered
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for item in references:
            path = str(item.get("path") or "")
            if not path or path in seen:
                continue
            seen.add(path)
            unique.append(item)
        parts = image_parts(
            unique[-self.max_vision_images :], max_bytes=self.max_image_bytes
        )
        if not parts:
            return rendered
        return [{"type": "text", "text": rendered}, *parts]

    async def _ask(self, role: str, context: dict[str, Any], schema: type[BaseModel]) -> BaseModel:
        self._ensure_circuit_available()
        request = self.prepare_request(role, context, schema)
        effort = self._thinking_for_role(role)
        started = time.perf_counter()
        payload = await self.chat(
            [
                {"role": "system", "content": "Return only a valid JSON object matching the requested output schema."},
                {"role": "user", "content": self._user_content(request, context)},
            ],
            reasoning_effort=effort or "none",
            response_format={"type": "json_object"},
            temperature=self.temperature,
            stream=self.stream_responses,
            _defer_success=True,
        )
        try:
            choice = payload["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError(f"LLM stopped with {choice.get('finish_reason')}")
            raw = choice["message"]["content"]
            if not isinstance(raw, str) or not raw or len(raw) > self.max_response_chars:
                raise ValueError("LLM response is empty or exceeds the response limit")
            validated = schema.model_validate_json(raw)
        except (ValueError, KeyError, IndexError, TypeError) as error:
            self._record_failure()
            self._trace(
                {
                    "phase": "LLM_RESPONSE_INVALID", "role": role,
                    "elapsed_seconds": round(time.perf_counter() - started, 2),
                    "usage": self.last_usage, "error": str(error),
                }
            )
            raise
        self._record_success()
        self._trace({
            "phase": "LLM_RESPONSE_PARSED",
            "role": role,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "raw_chars": len(raw),
            "usage": self.last_usage,
            "raw_preview": raw[:4000],
            "validated": validated.model_dump(mode="json"),
        })
        return validated
