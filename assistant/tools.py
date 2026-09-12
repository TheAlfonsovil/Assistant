from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

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


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        from .devices.computer.actions import register_actions

        self._tools = {
            tool.definition.name: tool
            for tool in (tools if tools is not None else [])
        }
        if tools is None:
            register_actions(self)

    def register(self, tool: Tool) -> None:
        self._tools[tool.definition.name] = tool

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

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


# Compatibility exports for callers that used the original flat module.
from .devices.computer.actions import FilesystemTool, GitTool, ProjectTool, ShellTool

__all__ = [
    "FilesystemTool",
    "GitTool",
    "MockTool",
    "ProjectTool",
    "ShellTool",
    "Tool",
    "ToolDefinition",
    "ToolRegistry",
]
