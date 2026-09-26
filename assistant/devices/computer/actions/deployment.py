from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from assistant.domain.models import ErrorType, OperationResult
from assistant.tools import ToolDefinition
from .shell import ShellTool

class DeploymentTool(ShellTool):
    definition = ToolDefinition(
        name="deployment",
        description="Run project-declared build, test, deploy, verify, and rollback commands",
        methods=["build", "test", "deploy", "verify", "rollback"],
        argument_schema={
            "command": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout": {"type": "number"},
        },
        permissions=["deployment"],
        idempotent=False,
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        started = datetime.now(UTC)
        command = args.get("command")
        if method not in self.definition.methods or not isinstance(command, str) or not command.strip():
            return OperationResult(
                success=False,
                error=f"deployment.{method} requires a project-declared command",
                error_type=ErrorType.INVALID_ARGUMENT,
                started_at=started,
            )
        result = await super().execute(
            "exec",
            {"command": command, "cwd": args.get("cwd"), "timeout": args.get("timeout", timeout)},
            timeout,
        )
        result.side_effects = [method] if method in {"deploy", "rollback"} else []
        return result
