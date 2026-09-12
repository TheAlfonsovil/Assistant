from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from pydantic import BaseModel, Field

from .domain.models import ErrorType, Operation, OperationResult
from .project_analysis import ProjectAnalyzer


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


class FilesystemTool(Tool):
    definition = ToolDefinition(
        name="filesystem",
        description="Local filesystem operations",
        methods=["read", "write", "list", "exists"],
        permissions=["filesystem"],
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        started = datetime.now(UTC)
        try:
            path = Path(args["path"]).resolve()
            if method == "exists":
                output: Any = path.exists()
            elif method == "read":
                output = await asyncio.to_thread(path.read_text, encoding="utf-8")
            elif method == "write":
                content = args["content"]
                path.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(path.write_text, content, encoding="utf-8")
                output = {"path": str(path), "bytes": len(content.encode("utf-8"))}
            elif method == "list":
                output = [entry.name for entry in path.iterdir()]
            else:
                raise ValueError(f"Unsupported filesystem method: {method}")
            return OperationResult(
                success=True,
                output=output,
                started_at=started,
                side_effects=[method] if method == "write" else [],
            )
        except FileNotFoundError as error:
            return OperationResult(
                success=False, error=str(error), error_type=ErrorType.NOT_FOUND, started_at=started
            )
        except (KeyError, ValueError, OSError) as error:
            return OperationResult(
                success=False,
                error=str(error),
                error_type=ErrorType.INVALID_ARGUMENT,
                started_at=started,
            )


class ShellTool(Tool):
    definition = ToolDefinition(
        name="shell",
        description="Run an approved local command",
        methods=["exec"],
        permissions=["shell"],
        idempotent=False,
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        started = datetime.now(UTC)
        if method != "exec" or not isinstance(args.get("command"), str):
            return OperationResult(
                success=False,
                error="shell.exec requires command",
                error_type=ErrorType.INVALID_ARGUMENT,
                started_at=started,
            )
        try:
            process = await asyncio.create_subprocess_shell(
                args["command"],
                cwd=args.get("cwd"),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=args.get("timeout", timeout)
            )
            output = {
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
                "exit_code": process.returncode,
            }
            return OperationResult(
                success=process.returncode == 0,
                output=output,
                error=None if process.returncode == 0 else output["stderr"],
                error_type=None if process.returncode == 0 else ErrorType.TOOL_FAILURE,
                retryable=process.returncode != 0,
                started_at=started,
                side_effects=["process"],
            )
        except TimeoutError:
            process.kill()
            return OperationResult(
                success=False,
                error="command timed out",
                error_type=ErrorType.TIMEOUT,
                retryable=True,
                started_at=started,
            )
        except OSError as error:
            return OperationResult(
                success=False,
                error=str(error),
                error_type=ErrorType.TOOL_FAILURE,
                retryable=True,
                started_at=started,
            )


class GitTool(ShellTool):
    definition = ToolDefinition(
        name="git",
        description="Read and modify the local git repository",
        methods=["status", "diff", "log", "branch", "checkout", "add", "commit"],
        permissions=["git"],
        idempotent=False,
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        commands = {
            "status": "git status --short",
            "diff": "git diff",
            "log": "git log --oneline -20",
            "branch": "git branch",
            "checkout": "git checkout",
            "add": "git add",
            "commit": "git commit",
        }
        if method not in commands:
            return OperationResult(
                success=False,
                error=f"Unsupported git method: {method}",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        command = commands[method]
        if method in {"checkout", "add", "commit"}:
            target = args.get("target") or args.get("message")
            if not isinstance(target, str):
                return OperationResult(
                    success=False,
                    error=f"git.{method} requires target/message",
                    error_type=ErrorType.INVALID_ARGUMENT,
                )
            command += f" {target if method != 'commit' else '-m ' + target}"
        return await super().execute("exec", {"command": command, "cwd": args.get("cwd")}, timeout)


class ProjectTool(Tool):
    definition = ToolDefinition(
        name="project",
        description="Inspect project structure, symbols and dependency relationships",
        methods=["analyze"],
        argument_schema={"root": {"type": "string"}, "max_files": {"type": "integer"}},
        permissions=["filesystem.read", "project.analysis"],
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        if method != "analyze" or not isinstance(args.get("root"), str):
            return OperationResult(
                success=False,
                error="project.analyze requires a root directory",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        return await ProjectAnalyzer().analyze(args["root"], int(args.get("max_files", 500)))


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
        self._tools = {
            tool.definition.name: tool
            for tool in (tools or [FilesystemTool(), ShellTool(), GitTool(), ProjectTool()])
        }

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
        result = await tool.execute(operation.method, operation.args, operation.timeout)
        finished_at = datetime.now(UTC)
        return result.model_copy(
            update={
                "finished_at": finished_at,
                "duration": max(result.duration, perf_counter() - started),
            }
        )
