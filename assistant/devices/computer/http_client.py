"""HTTP client for calling APIs and webhooks.

Shares the exact SSRF rule of the ``web`` tool: only public http(s)
destinations, no blind redirects, bounded response size. It exists because a
worker that can only *read* the web cannot exercise an API it just wrote, and
hand-rolling curl through ``shell`` would bypass every guard.

One method per verb, so idempotency is explicit: GET/HEAD always observe fresh
state, while POST/PUT/PATCH/DELETE are protected by the agent's replay guard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from assistant.domain.models import ErrorType, OperationResult
from assistant.tools import Tool, ToolDefinition
from assistant.devices.computer.web import WebTool

SAFE_METHODS = ["get", "head", "options"]
MUTATING_METHODS = ["post", "put", "patch", "delete"]
VERBS = SAFE_METHODS + MUTATING_METHODS
MAX_RESPONSE_CHARS = 100_000
REQUEST_TIMEOUT_CAP = 60.0

_ARGUMENTS = {
    "url": {"type": "string", "required": True},
    "headers": {"type": "object"},
    "body": {"type": "string"},
    "json": {"type": "object"},
    "timeout": {"type": "number"},
}


def summarize_response(response: httpx.Response, limit: int = MAX_RESPONSE_CHARS) -> dict[str, Any]:
    """Return a bounded, JSON-friendly view of an HTTP response."""
    text = response.text
    content_type = response.headers.get("content-type", "")
    payload: Any = None
    if "json" in content_type.casefold():
        try:
            payload = response.json()
        except ValueError:
            payload = None
    # ``response.elapsed`` raises RuntimeError until the body has been read or
    # the response closed, which is not guaranteed for every verb.
    try:
        elapsed = response.elapsed
    except RuntimeError:  # pragma: no cover - defensive, depends on httpx state
        elapsed = None
    return {
        "url": str(response.url),
        "status_code": response.status_code,
        "content_type": content_type,
        "elapsed_ms": round(elapsed.total_seconds() * 1000, 1) if elapsed else None,
        "ok": response.is_success,
        "chars": len(text),
        "truncated": len(text) > limit,
        "content": text[:limit],
        **({"json": payload} if payload is not None else {}),
    }


class HttpTool(Tool):
    definition = ToolDefinition(
        name="http",
        description=(
            "Call an HTTP API or webhook and inspect the response. Only public "
            "http(s) destinations are allowed."
        ),
        methods=VERBS,
        argument_schema=dict(_ARGUMENTS),
        method_argument_schema={verb: dict(_ARGUMENTS) for verb in VERBS},
        permissions=["http.request"],
        idempotent=False,
        idempotent_methods=list(SAFE_METHODS),
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        started = datetime.now(UTC)
        verb = (method or "").strip().lower()
        if verb not in VERBS:
            return OperationResult(
                success=False,
                error=f"Unsupported http method: {method}",
                error_type=ErrorType.INVALID_ARGUMENT,
                started_at=started,
            )
        url = str(args["url"]).strip()
        headers = args.get("headers")
        headers = (
            {str(key): str(value) for key, value in headers.items()}
            if isinstance(headers, dict)
            else {}
        )
        payload_json = args.get("json") if isinstance(args.get("json"), dict) else None
        body = args.get("body") if isinstance(args.get("body"), str) else None
        if payload_json is not None and body is not None:
            return OperationResult(
                success=False,
                error="provide either json or body, not both",
                error_type=ErrorType.INVALID_ARGUMENT,
                started_at=started,
            )
        try:
            await WebTool.validate_public_url(url)
        except (OSError, ValueError) as error:
            return OperationResult(
                success=False,
                error=f"blocked destination: {error}",
                error_type=ErrorType.AUTH,
                started_at=started,
            )
        request_timeout = min(
            max(float(args.get("timeout") or timeout or 30.0), 1.0), REQUEST_TIMEOUT_CAP
        )
        try:
            async with httpx.AsyncClient(
                timeout=request_timeout,
                follow_redirects=False,
                headers={"User-Agent": "Assistant-Core/0.1", **headers},
            ) as client:
                response = await client.request(
                    verb.upper(),
                    url,
                    content=body.encode("utf-8") if body is not None else None,
                    json=payload_json,
                )
        except (httpx.HTTPError, OSError, ValueError) as error:
            return OperationResult(
                success=False,
                error=f"http.{verb} failed: {error}",
                error_type=ErrorType.TRANSIENT,
                retryable=True,
                started_at=started,
            )
        summary = summarize_response(response)
        return OperationResult(
            # A 4xx/5xx is an observation, not a tool crash: the caller wants the
            # status and body to decide what to do next.
            success=response.is_success,
            output=summary,
            error=None if response.is_success else f"HTTP {response.status_code}",
            error_type=None if response.is_success else ErrorType.TOOL_FAILURE,
            retryable=response.status_code >= 500,
            started_at=started,
            side_effects=[] if verb in SAFE_METHODS else [f"http.{verb}"],
        )
