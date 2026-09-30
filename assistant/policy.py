"""Tool invocation policy: what the assistant may do unattended.

Every rule is opt-in through settings and evaluated **before** a tool runs.
The evaluation is purely deterministic: no LLM output can widen a policy, which
is what makes 24/7 operation defensible. With no rules configured the policy
allows everything, so existing deployments keep their behaviour.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .domain.models import Operation

# Tools whose free-form command argument is policed by the allowlist.
_COMMAND_TOOLS = {"shell", "deployment"}


class RateLimiter:
    """Minimum interval between two calls to the same tool, per process.

    A worker that loops on a paid endpoint can burn money faster than the LLM
    can be blamed for it. Refusing a too-early call is a *retryable* outcome, so
    it rides the existing node-retry backoff instead of inventing a new one.
    """

    def __init__(self, min_intervals: dict[str, float] | None = None):
        self.min_intervals: dict[str, float] = {}
        for tool, seconds in (min_intervals or {}).items():
            try:
                interval = float(seconds)
            except (TypeError, ValueError):
                continue
            if interval > 0:
                self.min_intervals[str(tool).strip().casefold()] = interval
        self._last_call: dict[str, float] = {}
        self.last_blocked: dict[str, float] = {}

    @property
    def active(self) -> bool:
        return bool(self.min_intervals)

    def check(self, operation: Operation) -> str | None:
        """Return an error when the call is too soon, else reserve the slot."""
        tool = (operation.tool or "").strip().casefold()
        interval = self.min_intervals.get(tool)
        if not interval:
            return None
        now = time.monotonic()
        previous = self._last_call.get(tool)
        if previous is not None:
            elapsed = now - previous
            if elapsed < interval:
                self.last_blocked[tool] = round(interval - elapsed, 3)
                return (
                    f"rate limit: {tool} may run at most once every {interval:g}s "
                    f"({interval - elapsed:.1f}s remaining)"
                )
        self._last_call[tool] = now
        self.last_blocked.pop(tool, None)
        return None

    def snapshot(self) -> dict:
        return {
            "min_intervals": dict(self.min_intervals),
            "blocked_now": dict(self.last_blocked),
        }


class ToolPolicy:
    """Allow/deny rules applied to one operation."""

    def __init__(
        self,
        *,
        denied_tools: list[str] | None = None,
        denied_methods: list[str] | None = None,
        shell_allowlist: list[str] | None = None,
        allowed_roots: list[str] | None = None,
    ):
        self.denied_tools = {
            item.strip().lower() for item in (denied_tools or []) if item.strip()
        }
        self.denied_methods = {
            item.strip().lower() for item in (denied_methods or []) if item.strip()
        }
        self.command_patterns = [
            re.compile(pattern, re.IGNORECASE)
            for pattern in (shell_allowlist or [])
            if pattern.strip()
        ]
        self.allowed_roots: list[Path] = []
        for root in allowed_roots or []:
            if not root.strip():
                continue
            try:
                self.allowed_roots.append(Path(root).expanduser().resolve())
            except OSError:
                continue

    @property
    def active(self) -> bool:
        """True when at least one rule is configured."""
        return bool(
            self.denied_tools
            or self.denied_methods
            or self.command_patterns
            or self.allowed_roots
        )

    def evaluate(self, operation: Operation) -> str | None:
        """Return a human-readable violation, or ``None`` when allowed."""
        tool = (operation.tool or "").strip().lower()
        qualified = f"{tool}.{(operation.method or '').strip().lower()}"
        if tool in self.denied_tools or qualified in self.denied_tools:
            return f"tool '{operation.tool}' is denied by policy"
        if qualified in self.denied_methods:
            return f"operation '{operation.tool}.{operation.method}' is denied by policy"
        if self.command_patterns and tool in _COMMAND_TOOLS:
            command = str(operation.args.get("command") or "")
            if not any(pattern.search(command) for pattern in self.command_patterns):
                return (
                    f"command for {operation.tool}.{operation.method} is not in the "
                    "configured allowlist"
                )
        if self.allowed_roots and tool == "filesystem":
            raw = operation.args.get("path")
            if isinstance(raw, str) and raw.strip():
                try:
                    target = Path(raw).expanduser().resolve()
                except OSError:
                    return f"cannot resolve path '{raw}'"
                if not any(
                    target == root or root in target.parents for root in self.allowed_roots
                ):
                    return f"path is outside the allowed roots: {raw}"
        return None
