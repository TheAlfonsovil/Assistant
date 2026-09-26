from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from assistant.domain.models import ErrorType, OperationResult
from assistant.tools import Tool, ToolDefinition

class ShellTool(Tool):
    definition = ToolDefinition(
        name="shell",
        description="Run an approved local command",
        methods=["exec"],
        argument_schema={
            "command": {"type": "string", "required": True},
            "cwd": {"type": "string"},
            "timeout": {"type": "number"},
        },
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
            await process.wait()
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
