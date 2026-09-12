"""Actions available on the real computer branch.

Add a computer action here, then expose it from ``register_actions``. The task
engine only sees the stable Tool contract and does not know about OS details.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from assistant.domain.models import ErrorType, OperationResult
from assistant.project_analysis import ProjectAnalyzer
from assistant.tools import Tool, ToolDefinition


class FilesystemTool(Tool):
    definition = ToolDefinition(
        name="filesystem",
        description="Local filesystem operations",
        methods=["read", "write", "list", "exists", "info", "search"],
        permissions=["filesystem"],
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        started = datetime.now(UTC)
        try:
            path = Path(args["path"]).resolve()
            if method == "exists":
                output: Any = path.exists()
            elif method == "info":
                stat = path.stat()
                output = {
                    "name": path.name,
                    "path": str(path),
                    "type": "directory" if path.is_dir() else "file",
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC),
                }
            elif method == "search":
                if not path.is_dir():
                    raise ValueError("filesystem.search requires a directory")
                pattern = args.get("pattern", "*")
                limit = min(max(int(args.get("limit", 100)), 1), 500)
                matches = list(path.rglob(pattern))[:limit]
                output = [
                    {
                        "name": item.name,
                        "path": str(item),
                        "type": "directory" if item.is_dir() else "file",
                    }
                    for item in matches
                ]
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
        methods=["status", "diff", "log", "branch", "branch_create", "checkout", "add", "commit", "merge"],
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


def register_actions(registry) -> None:
    """Register every real computer action in one discoverable place."""
    from .browser import BrowserTool
    from .codegraph import CodeGraphTool
    from .system import SystemInfoTool
    from .web import WebTool

    for action in (
        FilesystemTool(),
        ShellTool(),
        GitTool(),
        DeploymentTool(),
        ProjectTool(),
        CodeGraphTool(),
        SystemInfoTool(),
        WebTool(),
        BrowserTool(),
    ):
        registry.register(action)


__all__ = [
    "FilesystemTool",
    "GitTool",
    "DeploymentTool",
    "ProjectTool",
    "ShellTool",
    "register_actions",
]
