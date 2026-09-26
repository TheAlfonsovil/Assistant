from __future__ import annotations

import os
import shlex
import subprocess
from typing import Any

from assistant.domain.models import OperationResult
from assistant.tools import ToolDefinition
from .shell import ShellTool

class GitTool(ShellTool):
    definition = ToolDefinition(
        name="git",
        description="Read and modify the local git repository",
        methods=["status", "diff", "log", "branch", "branch_create", "checkout", "add", "commit", "merge"],
        argument_schema={
            "cwd": {"type": "string"},
            "target": {"type": "string"},
            "message": {"type": "string"},
        },
        permissions=["git"],
        idempotent=False,
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        commands = {
            "status": "git status --short",
            "diff": "git diff",
            "log": "git log --oneline -20",
            "branch": "git branch",
            "branch_create": "git switch -c",
            "checkout": "git checkout",
            "add": "git add",
            "commit": "git commit",
            "merge": "git merge --no-edit",
        }
        if method not in commands:
            return OperationResult(
                success=False,
                error=f"Unsupported git method: {method}",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        command = commands[method]
        if method in {"branch_create", "checkout", "add", "commit", "merge"}:
            target = args.get("target") or args.get("message")
            if not isinstance(target, str):
                return OperationResult(
                    success=False,
                    error=f"git.{method} requires target/message",
                    error_type=ErrorType.INVALID_ARGUMENT,
                )
            quoted_target = (
                subprocess.list2cmdline([target]) if os.name == "nt" else shlex.quote(target)
            )
            if method == "commit":
                command += f" -m {quoted_target}"
            elif method in {"checkout", "add"}:
                command += f" -- {quoted_target}"
            else:
                command += f" {quoted_target}"
        return await super().execute("exec", {"command": command, "cwd": args.get("cwd")}, timeout)
