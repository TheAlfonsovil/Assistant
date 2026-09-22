from __future__ import annotations

import asyncio
import json
import math
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from .domain.models import ErrorType, Operation, OperationResult


class ToolDefinition(BaseModel):
    name: str
    description: str
    methods: list[str]
    argument_schema: dict[str, Any] = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)
    timeout: float = 60.0
    idempotent: bool = True
    evidence: dict[str, Any] = Field(default_factory=dict)


class Tool(ABC):
    definition: ToolDefinition

    @abstractmethod
    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        raise NotImplementedError


class MockTool(Tool):
    def __init__(self, mode: str):
        self.mode = mode
        self.calls = 0
        self.definition = ToolDefinition(
            name=f"mock.{mode}", description="Testing tool", methods=["run"]
        )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        self.calls += 1
        if self.mode in {"success", "dynamic_result"} or (
            self.mode == "fail_once" and self.calls > 1
        ):
            return OperationResult(success=True, output=args.get("output", {"calls": self.calls}))
        if self.mode == "fail_once" and self.calls == 1:
            return OperationResult(
                success=False, error="first failure", error_type=ErrorType.TRANSIENT, retryable=True
            )
        if self.mode == "timeout":
            await asyncio.sleep(timeout + 1)
        return OperationResult(
            success=False,
            error="mock failure",
            error_type=ErrorType.TOOL_FAILURE,
            retryable=self.mode != "always_fail",
        )


class NotificationTool(Tool):
    """Persists local notifications without pretending to deliver them externally."""

    definition = ToolDefinition(
        name="notify",
        description="Persist a user notification for a local adapter to deliver",
        methods=["send"],
        argument_schema={
            "channel": {"type": "string"},
            "message": {"type": "string", "required": True},
            "metadata": {"type": "object"},
        },
        permissions=["notification.write"],
    )

    def __init__(self, path: str | Path = "data/notifications.jsonl"):
        self.path = Path(path)

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        channel = str(args.get("channel", "local"))
        message = str(args.get("message", "")).strip()
        if not message:
            return OperationResult(
                success=False,
                error="notification message is required",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        record = {
            "channel": channel,
            "message": message,
            "metadata": args.get("metadata", {}),
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return OperationResult(success=True, output=record, side_effects=["notification.persisted"])


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        from .devices.computer.actions import register_actions

        self._tools = {
            tool.definition.name: tool
            for tool in (tools if tools is not None else [])
        }
        if tools is None:
            register_actions(self)
            self.register(NotificationTool())

    def register(self, tool: Tool) -> None:
        self._tools[tool.definition.name] = tool

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def definition(self, name: str) -> ToolDefinition | None:
        tool = self._tools.get(name)
        return tool.definition if tool else None

    def validate_operation(self, operation: Operation) -> str | None:
        if not math.isfinite(operation.timeout) or operation.timeout <= 0:
            return "operation timeout must be a finite positive number"
        definition = self.definition(operation.tool)
        if definition is None:
            return f"Unknown tool: {operation.tool}"
        if operation.method not in definition.methods:
            return f"Unknown method: {operation.tool}.{operation.method}"
        return self._arguments_match_schema(definition, operation.args)

    @staticmethod
    def _arguments_match_schema(definition: ToolDefinition, args: dict[str, Any]) -> str | None:
        for name, schema in definition.argument_schema.items():
            expected = schema.get("type") if isinstance(schema, dict) else schema
            required = isinstance(schema, dict) and schema.get("required", False)
            if name not in args:
                if required:
                    return f"argument '{name}' is required"
                continue
            value = args[name]
            valid = (
                (expected == "string" and isinstance(value, str))
                or (expected == "integer" and isinstance(value, int) and not isinstance(value, bool))
                or (expected == "number" and isinstance(value, (int, float)) and not isinstance(value, bool))
                or (expected == "boolean" and isinstance(value, bool))
                or (expected == "object" and isinstance(value, dict))
                or (expected == "array" and isinstance(value, list))
                or expected in (None, "")
            )
            if not valid:
                return f"argument '{name}' must be of type {expected}"
        return None

    async def execute(self, operation: Operation) -> OperationResult:
        tool = self._tools.get(operation.tool)
        if tool is None:
            return OperationResult(
                success=False,
                error=f"Unknown tool: {operation.tool}",
                error_type=ErrorType.NOT_FOUND,
            )
        if operation.method not in tool.definition.methods:
            return OperationResult(
                success=False,
                error=f"Unknown method: {operation.method}",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        schema_error = self._arguments_match_schema(tool.definition, operation.args)
        if schema_error:
            return OperationResult(
                success=False,
                error=schema_error,
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        started = perf_counter()
        try:
            result = await asyncio.wait_for(
                tool.execute(operation.method, operation.args, operation.timeout),
                timeout=operation.timeout,
            )
        except TimeoutError:
            result = OperationResult(
                success=False,
                error=f"tool timed out after {operation.timeout}s",
                error_type=ErrorType.TIMEOUT,
                retryable=True,
            )
        finished_at = datetime.now(UTC)
        return result.model_copy(
            update={
                "finished_at": finished_at,
                "duration": max(result.duration, perf_counter() - started),
            }
        )


if TYPE_CHECKING:
    from .devices.computer.actions import FilesystemTool, GitTool, ProjectTool, ShellTool


def __getattr__(name: str):
    """Resolve legacy action exports without importing the action package eagerly."""
    if name in {"FilesystemTool", "GitTool", "ProjectTool", "ShellTool"}:
        from .devices.computer.actions import FilesystemTool, GitTool, ProjectTool, ShellTool

        return {
            "FilesystemTool": FilesystemTool,
            "GitTool": GitTool,
            "ProjectTool": ProjectTool,
            "ShellTool": ShellTool,
        }[name]
    raise AttributeError(name)

__all__ = [
    "FilesystemTool",
    "GitTool",
    "MockTool",
    "NotificationTool",
    "ProjectTool",
    "ShellTool",
    "Tool",
    "ToolDefinition",
    "ToolRegistry",
]
