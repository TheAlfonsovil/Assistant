"""Actions available on the real computer branch.

Add a computer action here, then expose it from ``register_actions``. The task
engine only sees the stable Tool contract and does not know about OS details.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
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
        methods=["read", "write", "create", "delete", "list", "exists", "info", "search"],
        argument_schema={
            "path": {"type": "string", "required": True},
            "content": {"type": "string"},
            "pattern": {"type": "string"},
            "limit": {"type": "integer"},
        },
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
            elif method == "create":
                content = args.get("content", "")
                path.parent.mkdir(parents=True, exist_ok=True)
                def create_file() -> None:
                    with path.open("x", encoding="utf-8") as stream:
                        stream.write(content)
                await asyncio.to_thread(create_file)
                output = {"path": str(path), "created": True, "bytes": len(content.encode("utf-8"))}
            elif method == "delete":
                if path.is_dir():
                    await asyncio.to_thread(shutil.rmtree, path)
                else:
                    await asyncio.to_thread(path.unlink)
                output = {"path": str(path), "deleted": True}
            elif method == "list":
                output = [entry.name for entry in path.iterdir()]
            else:
                raise ValueError(f"Unsupported filesystem method: {method}")
            return OperationResult(
                success=True,
                output=output,
                started_at=started,
                side_effects=[method] if method in {"write", "create", "delete"} else [],
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


class ProcessTool(Tool):
    """Manage explicitly started long-lived local processes by exact PID."""

    definition = ToolDefinition(
        name="process",
        description="Start, inspect, read logs from, and stop local project processes",
        methods=["start", "status", "log", "stop"],
        argument_schema={
            "command": {"type": "string"},
            "cwd": {"type": "string"},
            "label": {"type": "string"},
            "process_id": {"type": "integer"},
            "tail": {"type": "integer"},
        },
        permissions=["process"],
        idempotent=False,
    )

    def __init__(self, log_dir: str | Path = "data/processes"):
        self.log_dir = Path(log_dir)
        self._processes: dict[int, tuple[asyncio.subprocess.Process, Any, Any, str]] = {}

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        started = datetime.now(UTC)
        if method == "start":
            return await self._start(args, started)
        process_id = args.get("process_id")
        if method == "status" and process_id is None:
            return OperationResult(success=True, output=await self._list_status())
        if not isinstance(process_id, int):
            return OperationResult(
                success=False,
                error=f"process.{method} requires integer process_id",
                error_type=ErrorType.INVALID_ARGUMENT,
                started_at=started,
            )
        record = self._processes.get(process_id)
        if record is None:
            return OperationResult(
                success=False,
                error=f"process not managed by this worker: {process_id}",
                error_type=ErrorType.NOT_FOUND,
                started_at=started,
            )
        process, stdout, stderr, label = record
        if method == "status":
            return OperationResult(success=True, output=self._status(process, label), started_at=started)
        if method == "log":
            tail = min(max(int(args.get("tail", 4000)), 1), 20000)
            return OperationResult(
                success=True,
                output={"process_id": process_id, "stdout": self._tail(stdout, tail), "stderr": self._tail(stderr, tail)},
                started_at=started,
            )
        if method == "stop":
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=min(timeout, 10.0))
                except TimeoutError:
                    process.kill()
                    await process.wait()
            stdout.close()
            stderr.close()
            self._processes.pop(process_id, None)
            return OperationResult(
                success=True,
                output={"process_id": process_id, "stopped": True},
                side_effects=["process.stopped"],
                started_at=started,
            )
        return OperationResult(
            success=False,
            error=f"Unsupported process method: {method}",
            error_type=ErrorType.INVALID_ARGUMENT,
            started_at=started,
        )

    async def _start(self, args: dict[str, Any], started: datetime) -> OperationResult:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return OperationResult(
                success=False,
                error="process.start requires command",
                error_type=ErrorType.INVALID_ARGUMENT,
                started_at=started,
            )
        self.log_dir.mkdir(parents=True, exist_ok=True)
        label = str(args.get("label") or command[:80])
        process_id_hint = len(self._processes) + 1
        stdout_path = self.log_dir / f"{process_id_hint}-stdout.log"
        stderr_path = self.log_dir / f"{process_id_hint}-stderr.log"
        stdout = stdout_path.open("ab")
        stderr = stderr_path.open("ab")
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=args.get("cwd"),
                stdout=stdout,
                stderr=stderr,
                start_new_session=os.name != "nt",
            )
        except (OSError, ValueError) as error:
            stdout.close()
            stderr.close()
            return OperationResult(
                success=False,
                error=str(error),
                error_type=ErrorType.TOOL_FAILURE,
                started_at=started,
            )
        self._processes[process.pid] = (process, stdout, stderr, label)
        return OperationResult(
            success=True,
            output={"process_id": process.pid, "label": label, "stdout": str(stdout_path), "stderr": str(stderr_path)},
            side_effects=["process.started"],
            started_at=started,
        )

    async def _list_status(self) -> list[dict[str, Any]]:
        return [self._status(process, label) for process, _, _, label in self._processes.values()]

    @staticmethod
    def _status(process: asyncio.subprocess.Process, label: str) -> dict[str, Any]:
        return {"process_id": process.pid, "label": label, "running": process.returncode is None, "exit_code": process.returncode}

    @staticmethod
    def _tail(stream, limit: int) -> str:
        stream.flush()
        try:
            with open(stream.name, "rb") as current:
                return current.read()[-limit:].decode(errors="replace")
        except OSError as error:
            return f"log unavailable: {error}"


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


class ProjectTool(Tool):
    definition = ToolDefinition(
        name="project",
        description=(
            "Inspect, read, audit, create, or edit a local project. Read/edit are "
            "bounded to the registered project root."
        ),
        methods=["analyze", "read", "audit", "validate", "create", "initialize", "scaffold", "modify", "edit"],
        evidence={
            "analyze": {"success_fields": ["root", "files_analyzed", "file_count", "languages"]},
            "read": {"success_fields": ["root", "files"]},
            "edit": {"success_fields": ["feature", "files", "deleted", "validation"]},
            "modify": {"success_fields": ["feature", "files", "validation"]},
            "validate": {"success_fields": ["root", "checks", "validation_status"]},
            "initialize": {"success_fields": ["path", "name", "kind", "manifest", "files"]},
        },
        argument_schema={
            "root": {"type": "string"},
            "max_files": {"type": "integer"},
            "files": {"type": "array", "description": "Relative files to read, bounded by the project root."},
            "timeout": {"type": "number"},
            "run_tests": {
                "type": "boolean",
                "description": "Only for audit: execute a detected test command when true.",
                "default": False,
            },
            "checks": {
                "type": "array",
                "description": "Optional safe validation checks: build, test, docker.",
            },
            "name": {"type": "string"},
            "template": {
                "type": "string",
                "description": "Optional project template identifier for create.",
            },
            "kind": {
                "type": "string",
                "description": "Workspace kind, for example book, chemistry, research, automation, data, or app.",
            },
            "description": {"type": "string"},
            "directories": {"type": "array"},
            "frontend": {"type": "object", "description": "Frontend stack, for example {framework: 'vue'}."},
            "backend": {"type": "object", "description": "Backend stack, for example {language: 'java', framework: 'spring-boot'}."},
            "containerize": {"type": "boolean", "default": True},
            "device": {"type": "string", "description": "Target device, for example computer."},
            "os": {"type": "string", "description": "Target operating system, for example windows-11."},
            "feature": {"type": "string", "description": "Feature to add or modify."},
            "changes": {"type": "array", "description": "Files to write, each with path and content."},
            "edit_operations": {
                "type": "array",
                "description": "Optional artifact operations with mode write, append, prepend, or json_merge.",
            },
            "deletions": {"type": "array", "description": "Relative files to delete, bounded by the project root."},
            "commands": {"type": "array", "description": "Optional validation commands to run after modification."},
        },
        permissions=["filesystem.read", "filesystem.write", "project.analysis", "project.modify"],
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        if method not in self.definition.methods:
            return OperationResult(
                success=False,
                error="project requires a supported method",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        if method == "create":
            root = args.get("root")
            name = args.get("name")
            if not isinstance(root, str) or not isinstance(name, str) or not name.strip():
                return OperationResult(success=False, error="project.create requires root and name", error_type=ErrorType.INVALID_ARGUMENT)
            project_root = Path(root).resolve()
            project_path = (project_root / name.strip()).resolve()
            if project_path.parent != project_root:
                return OperationResult(success=False, error="project name must create a direct child of root", error_type=ErrorType.INVALID_ARGUMENT)
            try:
                project_path.mkdir(parents=True, exist_ok=False)
                return OperationResult(success=True, output={"path": str(project_path), "name": name.strip(), "template": args.get("template", "empty")}, side_effects=["project.created"])
            except FileExistsError:
                return OperationResult(success=False, error=f"project already exists: {project_path}", error_type=ErrorType.CONFLICT)
            except OSError as error:
                return OperationResult(success=False, error=str(error), error_type=ErrorType.TOOL_FAILURE)
        if method == "initialize":
            return await self._initialize(args)
        if method == "scaffold":
            return await self._scaffold(args)
        if method in {"modify", "edit"}:
            return await self._modify(args, timeout)
        if not isinstance(args.get("root"), str):
            return OperationResult(success=False, error="project requires a root directory", error_type=ErrorType.INVALID_ARGUMENT)
        analyzer = ProjectAnalyzer()
        if method == "read":
            return await self._read(args)
        if method == "audit":
            run_tests = args.get("run_tests", False)
            if not isinstance(run_tests, bool):
                return OperationResult(
                    success=False,
                    error="project.audit run_tests must be a boolean",
                    error_type=ErrorType.INVALID_ARGUMENT,
                )
            return await analyzer.audit(
                args["root"],
                int(args.get("max_files", 500)),
                float(args.get("timeout", timeout)),
                run_tests=run_tests,
            )
        if method == "validate":
            return await self._validate(args, timeout)
        return await analyzer.analyze(args["root"], int(args.get("max_files", 500)))

    async def _initialize(self, args: dict[str, Any]) -> OperationResult:
        """Create a bounded, stack-neutral workspace from a declarative artifact set."""
        root = args.get("root")
        name = args.get("name")
        kind = str(args.get("kind") or "workspace").strip().lower()
        description = str(args.get("description") or "").strip()
        directories = args.get("directories", [])
        files = args.get("files", [])
        if not isinstance(root, str) or not isinstance(name, str) or not name.strip():
            return OperationResult(
                success=False,
                error="project.initialize requires root and name",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name.strip()):
            return OperationResult(
                success=False,
                error="project name contains unsupported path characters",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        if not isinstance(directories, list) or any(
            not isinstance(item, str) or not item.strip() for item in directories
        ):
            return OperationResult(
                success=False,
                error="directories must contain non-empty relative paths",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        if not isinstance(files, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("content", ""), str)
            for item in files
        ):
            return OperationResult(
                success=False,
                error="files must contain objects with path and optional string content",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        if len(files) > 200 or len(directories) > 100:
            return OperationResult(
                success=False,
                error="workspace artifact limits exceeded",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        project_root = (Path(root).resolve() / name.strip()).resolve()
        if project_root.parent != Path(root).resolve():
            return OperationResult(
                success=False,
                error="project name must create a direct child of root",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        if project_root.exists():
            return OperationResult(
                success=False,
                error=f"project already exists: {project_root}",
                error_type=ErrorType.CONFLICT,
            )
        manifest = {
            "name": name.strip(),
            "kind": kind or "workspace",
            "description": description,
            "created_by": "assistant.project.initialize",
            "artifacts": [item["path"] for item in files],
        }
        generated = {
            "README.md": (
                f"# {name.strip()}\n\n"
                f"Kind: {kind or 'workspace'}\n\n"
                f"{description}\n"
            ),
            ".assistant/project.json": json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        }
        generated.update({item["path"]: item.get("content", "") for item in files})
        try:
            project_root.mkdir(parents=True)
            for relative in directories:
                directory = (project_root / relative).resolve()
                if project_root not in directory.parents and directory != project_root:
                    raise ValueError(f"directory path escapes project: {relative}")
                directory.mkdir(parents=True, exist_ok=True)
            for relative, content in generated.items():
                path = (project_root / relative).resolve()
                if project_root not in path.parents:
                    raise ValueError(f"file path escapes project: {relative}")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        except (OSError, ValueError) as error:
            if project_root.exists():
                shutil.rmtree(project_root, ignore_errors=True)
            return OperationResult(
                success=False,
                error=str(error),
                error_type=ErrorType.TOOL_FAILURE,
            )
        return OperationResult(
            success=True,
            output={
                "path": str(project_root),
                "name": name.strip(),
                "kind": kind or "workspace",
                "manifest": manifest,
                "files": sorted(generated),
                "directories": sorted(directories),
            },
            side_effects=["project.initialized"],
        )

    async def _read(self, args: dict[str, Any]) -> OperationResult:
        root = args.get("root")
        files = args.get("files")
        if not isinstance(root, str) or not isinstance(files, list) or not files:
            return OperationResult(
                success=False,
                error="project.read requires root and a non-empty files array",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        if len(files) > 20 or any(not isinstance(item, str) or not item.strip() for item in files):
            return OperationResult(
                success=False,
                error="project.read accepts at most 20 non-empty relative file paths",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        project_root = Path(root).resolve()
        if not project_root.is_dir():
            return OperationResult(success=False, error=f"project directory does not exist: {project_root}", error_type=ErrorType.NOT_FOUND)
        contents: dict[str, str] = {}
        try:
            for relative in files:
                path = (project_root / relative).resolve()
                if project_root not in path.parents or not path.is_file():
                    raise FileNotFoundError(relative)
                if path.stat().st_size > 200_000:
                    raise ValueError(f"file is too large to read: {relative}")
                contents[str(path.relative_to(project_root))] = path.read_text(encoding="utf-8")
        except (OSError, ValueError) as error:
            return OperationResult(
                success=False,
                error=str(error),
                error_type=ErrorType.NOT_FOUND if isinstance(error, FileNotFoundError) else ErrorType.INVALID_ARGUMENT,
            )
        return OperationResult(success=True, output={"root": str(project_root), "files": contents})

    async def _scaffold(self, args: dict[str, Any]) -> OperationResult:
        root = args.get("root")
        name = args.get("name")
        frontend = args.get("frontend") or {}
        backend = args.get("backend") or {}
        if not isinstance(root, str) or not isinstance(name, str) or not name.strip():
            return OperationResult(success=False, error="project.scaffold requires root and name", error_type=ErrorType.INVALID_ARGUMENT)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name.strip()):
            return OperationResult(success=False, error="project name contains unsupported path characters", error_type=ErrorType.INVALID_ARGUMENT)
        if not isinstance(frontend, dict) or not isinstance(backend, dict):
            return OperationResult(success=False, error="frontend and backend must be objects", error_type=ErrorType.INVALID_ARGUMENT)
        project_root = (Path(root).resolve() / name.strip()).resolve()
        if project_root.parent != Path(root).resolve():
            return OperationResult(success=False, error="project name must create a direct child of root", error_type=ErrorType.INVALID_ARGUMENT)
        if project_root.exists():
            return OperationResult(success=False, error=f"project already exists: {project_root}", error_type=ErrorType.CONFLICT)
        frontend_framework = str(frontend.get("framework", "")).casefold()
        backend_framework = str(backend.get("framework", "")).casefold()
        supported_frontends = {"vue", "vue3", "static", "html", "vanilla", "none"}
        supported_backends = {"spring-boot", "springboot", "fastapi", "flask", "python"}
        if frontend_framework not in supported_frontends or backend_framework not in supported_backends:
            return OperationResult(
                success=False,
                error=(
                    "unsupported scaffold stack; supported frontend frameworks are "
                    "vue, static/html, or none and backend frameworks are "
                    "spring-boot, fastapi, or flask"
                ),
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        backend_language = str(backend.get("language", "")).casefold()
        if backend_language == "java" and str(backend.get("java_version", "25")) != "25":
            return OperationResult(
                success=False,
                error="project.scaffold currently requires backend.language=java and backend.java_version=25",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        if backend_language not in {"java", "python"}:
            return OperationResult(
                success=False,
                error="project.scaffold supports backend.language=java or python",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        containerize = args.get("containerize", True)
        if not isinstance(containerize, bool):
            return OperationResult(success=False, error="containerize must be a boolean", error_type=ErrorType.INVALID_ARGUMENT)
        if backend_language == "python":
            files = self._python_static_files(name.strip(), frontend, backend, args)
        else:
            files = self._vue_spring_files(name.strip(), frontend, backend, args)
        if not containerize:
            files = {
                path: content
                for path, content in files.items()
                if path not in {
                    "docker-compose.yml", "backend/Dockerfile", "frontend/Dockerfile",
                    "frontend/nginx.conf", "Dockerfile", "run.ps1",
                }
            }
        try:
            for relative, content in files.items():
                path = (project_root / relative).resolve()
                if project_root not in path.parents:
                    raise ValueError(f"scaffold path escapes project: {relative}")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        except (OSError, ValueError) as error:
            if project_root.exists():
                shutil.rmtree(project_root, ignore_errors=True)
            return OperationResult(success=False, error=str(error), error_type=ErrorType.TOOL_FAILURE)
        return OperationResult(
            success=True,
            output={
                "path": str(project_root),
                "name": name.strip(),
                "frontend": frontend,
                "backend": backend,
                "device": args.get("device", "computer"),
                "os": args.get("os", "windows-11"),
                "files": sorted(files),
                "containerized": bool(args.get("containerize", True)),
                "scaffolded": True,
            },
            side_effects=["project.scaffolded"],
        )

    async def _validate(self, args: dict[str, Any], timeout: float) -> OperationResult:
        """Run safe, stack-aware checks after a project mutation or scaffold."""
        root = Path(args["root"]).resolve()
        if not root.is_dir():
            return OperationResult(
                success=False,
                error=f"project directory does not exist: {root}",
                error_type=ErrorType.NOT_FOUND,
            )
        checks = args.get("checks")
        if checks is not None and (
            not isinstance(checks, list)
            or any(item not in {"build", "test", "docker"} for item in checks)
        ):
            return OperationResult(
                success=False,
                error="checks must contain only build, test, or docker",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        requested = set(checks or {"build", "test", "docker"})
        extra_commands = args.get("commands", [])
        if extra_commands is not None and (
            not isinstance(extra_commands, list)
            or any(not isinstance(item, str) or not item.strip() for item in extra_commands)
        ):
            return OperationResult(
                success=False,
                error="commands must contain non-empty strings",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        commands: list[tuple[str, str]] = []
        pom_files = list(root.rglob("pom.xml"))
        if pom_files and "test" in requested:
            pom = pom_files[0].relative_to(root).as_posix()
            commands.append(("maven_test", f"mvn -q -f {pom} test"))
        package_files = list(root.rglob("package.json"))
        if package_files and "build" in requested:
            package = package_files[0].parent.relative_to(root).as_posix()
            prefix = f" --prefix {package}" if package != "." else ""
            commands.append(("frontend_build", f"npm{prefix} run build --if-present"))
        if package_files and "test" in requested:
            package = package_files[0].parent.relative_to(root).as_posix()
            prefix = f" --prefix {package}" if package != "." else ""
            commands.append(("frontend_test", f"npm{prefix} test -- --if-present"))
        python_files = list(root.rglob("*.py"))
        if python_files and "build" in requested:
            commands.append(("python_compile", "python -m compileall -q ."))
        if python_files and "test" in requested and any(
            item.name.startswith("test_") or item.name.endswith("_test.py")
            for item in python_files
        ):
            commands.append(("python_test", "python -m pytest -q"))
        if (
            "docker" in requested
            and ((root / "docker-compose.yml").exists() or (root / "compose.yml").exists())
        ):
            commands.append(("docker_compose_config", "docker compose config"))
        commands.extend((f"custom_{index}", command) for index, command in enumerate(extra_commands or [], start=1))
        results = []
        for name, command in commands:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=max(1.0, timeout)
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                results.append({"name": name, "command": command, "status": "TIMEOUT"})
                continue
            results.append(
                {
                    "name": name,
                    "command": command,
                    "status": "PASS" if process.returncode == 0 else "FAIL",
                    "exit_code": process.returncode,
                    "stdout": stdout.decode(errors="replace")[-4000:],
                    "stderr": stderr.decode(errors="replace")[-4000:],
                }
            )
        failures = [item for item in results if item["status"] != "PASS"]
        return OperationResult(
            success=not failures,
            output={
                "root": str(root),
                "checks": results,
                "validation_status": "PASS" if not failures else "FAIL",
                "skipped": sorted(requested - {name.split("_")[0] for name, _ in commands}),
            },
            error=(
                "project validation failed: "
                + ", ".join(item["name"] for item in failures)
                if failures
                else None
            ),
            error_type=ErrorType.TOOL_FAILURE if failures else None,
            retryable=False,
        )

    async def _modify(self, args: dict[str, Any], timeout: float) -> OperationResult:
        root = args.get("root")
        feature = args.get("feature")
        changes = args.get("changes", [])
        edit_operations = args.get("edit_operations", [])
        deletions = args.get("deletions", [])
        if not isinstance(root, str) or not isinstance(feature, str) or not feature.strip():
            return OperationResult(success=False, error="project.edit requires root and feature", error_type=ErrorType.INVALID_ARGUMENT)
        if not isinstance(changes, list) or not isinstance(edit_operations, list):
            return OperationResult(success=False, error="project.edit requires at least one change operation", error_type=ErrorType.INVALID_ARGUMENT)
        changes = [*changes, *edit_operations]
        if not changes:
            return OperationResult(success=False, error="project.edit requires at least one change operation", error_type=ErrorType.INVALID_ARGUMENT)
        if not isinstance(deletions, list) or any(not isinstance(item, str) or not item.strip() for item in deletions):
            return OperationResult(success=False, error="deletions must contain relative file paths", error_type=ErrorType.INVALID_ARGUMENT)
        commands = args.get("commands", [])
        if not isinstance(commands, list) or any(
            not isinstance(command, str) or not command.strip() for command in commands
        ):
            return OperationResult(
                success=False,
                error="commands must contain non-empty strings",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        project_root = Path(root).resolve()
        if not project_root.is_dir():
            return OperationResult(success=False, error=f"project directory does not exist: {project_root}", error_type=ErrorType.NOT_FOUND)
        written = []
        deleted = []
        originals: dict[Path, bytes | None] = {}
        touched: set[Path] = set()
        try:
            for change in changes:
                if not isinstance(change, dict) or not isinstance(change.get("path"), str) or not isinstance(change.get("content"), str):
                    raise TypeError("each change requires string path and content")
                path = (project_root / change["path"]).resolve()
                if project_root not in path.parents:
                    raise ValueError(f"change path escapes project: {change['path']}")
                if path not in touched:
                    originals[path] = path.read_bytes() if path.is_file() else None
                    touched.add(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                mode = change.get("mode", "write")
                if mode not in {"write", "append", "prepend", "json_merge"}:
                    raise ValueError(f"unsupported edit mode: {mode}")
                if mode == "write":
                    content = change["content"]
                elif mode == "append":
                    content = (path.read_text(encoding="utf-8") if path.exists() else "") + change["content"]
                elif mode == "prepend":
                    content = change["content"] + (path.read_text(encoding="utf-8") if path.exists() else "")
                else:
                    base = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                    patch = json.loads(change["content"])
                    if not isinstance(base, dict) or not isinstance(patch, dict):
                        raise ValueError("json_merge requires JSON objects")
                    base.update(patch)
                    content = json.dumps(base, indent=2, ensure_ascii=False) + "\n"
                path.write_text(content, encoding="utf-8")
                written.append(str(path.relative_to(project_root)))
            for relative in deletions:
                path = (project_root / relative).resolve()
                if project_root not in path.parents:
                    raise ValueError(f"deletion path escapes project: {relative}")
                if path not in touched:
                    originals[path] = path.read_bytes() if path.is_file() else None
                    touched.add(path)
                if path.exists():
                    if not path.is_file():
                        raise ValueError(f"deletion target is not a file: {relative}")
                    path.unlink()
                    deleted.append(str(path.relative_to(project_root)))
        except (OSError, TypeError, ValueError) as error:
            self._restore_files(originals)
            return OperationResult(success=False, error=str(error), error_type=ErrorType.INVALID_ARGUMENT)
        validation = []
        for command in commands:
            process = await asyncio.create_subprocess_shell(
                command, cwd=project_root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError:
                process.kill()
                await process.wait()
                self._restore_files(originals)
                return OperationResult(
                    success=False,
                    output={"feature": feature, "files": written, "validation": validation},
                    error=f"validation command timed out: {command}",
                    error_type=ErrorType.TIMEOUT,
                    retryable=False,
                )
            validation.append({"command": command, "exit_code": process.returncode, "stdout": stdout.decode(errors="replace")[-4000:], "stderr": stderr.decode(errors="replace")[-4000:]})
            if process.returncode != 0:
                self._restore_files(originals)
                return OperationResult(success=False, output={"feature": feature, "files": written, "validation": validation}, error=f"validation command failed: {command}", error_type=ErrorType.TOOL_FAILURE)
        return OperationResult(success=True, output={"feature": feature, "files": written, "deleted": deleted, "validation": validation}, side_effects=["project.modified"])

    @staticmethod
    def _restore_files(originals: dict[Path, bytes | None]) -> None:
        for path, content in originals.items():
            if content is None:
                if path.is_file():
                    path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    @staticmethod
    def _vue_spring_files(name: str, frontend: dict[str, Any], backend: dict[str, Any], args: dict[str, Any]) -> dict[str, str]:
        java_package = re.sub(r"[^a-z0-9]+", ".", name.casefold()).strip(".") or "app"
        package_path = java_package.replace(".", "/")
        artifact_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "app"
        return {
            "README.md": f"# {name}\n\nVue frontend and Java 25 Spring Boot backend.\n\nTarget device: {args.get('device', 'computer')}.\nTarget OS: {args.get('os', 'windows-11')}.\n\nRun with `docker compose up --build` or the platform script.\n",
            "frontend/package.json": json.dumps({"name": f"{name}-frontend", "private": True, "type": "module", "scripts": {"dev": "vite", "build": "vite build", "test": "node -e \"console.log('No frontend tests configured')\""}, "dependencies": {"vue": "^3.5.0"}, "devDependencies": {"@vitejs/plugin-vue": "^5.2.0", "vite": "^6.0.0"}}, indent=2) + "\n",
            "frontend/index.html": "<div id=\"app\"></div><script type=\"module\" src=\"/src/main.js\"></script>\n",
            "frontend/src/main.js": "import { createApp } from 'vue';\nimport './style.css';\n\ncreateApp({ template: '<main><h1>Application ready</h1><p>Vue frontend connected to the project scaffold.</p></main>' }).mount('#app');\n",
            "frontend/src/style.css": "body { font-family: sans-serif; margin: 2rem; }\n",
            "frontend/vite.config.js": "import { defineConfig } from 'vite';\nimport vue from '@vitejs/plugin-vue';\nexport default defineConfig({ plugins: [vue()], server: { host: '0.0.0.0', port: 5173 } });\n",
            "backend/pom.xml": f"""<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId><version>4.0.0</version><relativePath/></parent><groupId>com.example</groupId><artifactId>{artifact_id}</artifactId><version>0.0.1-SNAPSHOT</version><properties><java.version>25</java.version></properties><dependencies><dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency><dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-test</artifactId><scope>test</scope></dependency></dependencies><build><plugins><plugin><groupId>org.springframework.boot</groupId><artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build></project>\n""",
            f"backend/src/main/java/{package_path}/Application.java": f"package {java_package};\n\nimport org.springframework.boot.SpringApplication;\nimport org.springframework.boot.autoconfigure.SpringBootApplication;\n\n@SpringBootApplication\npublic class Application {{ public static void main(String[] args) {{ SpringApplication.run(Application.class, args); }} }}\n",
            "backend/src/main/resources/application.properties": "server.port=8080\n",
            f"backend/src/test/java/{package_path}/ApplicationTest.java": f"package {java_package};\n\nimport org.junit.jupiter.api.Test;\nimport static org.junit.jupiter.api.Assertions.assertTrue;\n\nclass ApplicationTest {{ @Test void scaffoldIsHealthy() {{ assertTrue(true); }} }}\n",
            "docker-compose.yml": "services:\n  backend:\n    build: ./backend\n    expose: ['8080']\n  frontend:\n    build: ./frontend\n    ports: ['8080:80']\n    depends_on: [backend]\n",
            "backend/Dockerfile": "FROM maven:3.9-eclipse-temurin-25 AS build\nWORKDIR /app\nCOPY pom.xml .\nRUN mvn -q -DskipTests dependency:go-offline\nCOPY src src\nRUN mvn -q package -DskipTests\nFROM eclipse-temurin:25-jre\nCOPY --from=build /app/target/*.jar /app/app.jar\nENTRYPOINT [\"java\", \"-jar\", \"/app/app.jar\"]\n",
            "frontend/Dockerfile": "FROM node:22-alpine AS build\nWORKDIR /app\nCOPY package*.json ./\nRUN npm install\nCOPY . .\nRUN npm run build\nFROM nginx:alpine\nCOPY --from=build /app/dist /usr/share/nginx/html\nCOPY nginx.conf /etc/nginx/conf.d/default.conf\n",
            "frontend/nginx.conf": "server { listen 80; root /usr/share/nginx/html; index index.html; location /api/ { proxy_pass http://backend:8080; } location / { try_files $uri $uri/ /index.html; } }\n",
            "run.ps1": "docker compose up --build\n",
        }

    @staticmethod
    def _python_static_files(name: str, frontend: dict[str, Any], backend: dict[str, Any], args: dict[str, Any]) -> dict[str, str]:
        return {
            "README.md": f"# {name}\n\nFastAPI backend with a static frontend.\n\nRun locally with `python -m uvicorn main:app --reload` or `docker compose up --build`.\n",
            "requirements.txt": "fastapi\nuvicorn[standard]\nyfinance\n",
            "main.py": (
                "from pathlib import Path\n"
                "from fastapi import FastAPI\n"
                "from fastapi.staticfiles import StaticFiles\n"
                "import yfinance as yf\n\n"
                "app = FastAPI(title='S&P 500 data')\n"
                "BASE = Path(__file__).parent\n\n"
                "@app.get('/api/sp500')\n"
                "def sp500():\n"
                "    frame = yf.download('^GSPC', period='1mo', interval='1d', auto_adjust=False, progress=False)\n"
                "    if frame.empty:\n"
                "        return []\n"
                "    frame = frame.reset_index()\n"
                "    return frame.to_dict(orient='records')\n\n"
                "app.mount('/', StaticFiles(directory=BASE / 'static', html=True), name='static')\n"
            ),
            "static/index.html": (
                "<!doctype html><html lang='en'><head><meta charset='UTF-8'>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                "<title>S&P 500</title></head><body><main><h1>S&amp;P 500</h1>"
                "<p id='status'>Loading…</p><pre id='data'></pre></main>"
                "<script type='module' src='/app.js'></script></body></html>"
            ),
            "static/app.js": (
                "const status = document.querySelector('#status');\n"
                "const data = document.querySelector('#data');\n"
                "fetch('/api/sp500').then(response => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })\n"
                ".then(rows => { status.textContent = `${rows.length} daily records`; data.textContent = JSON.stringify(rows, null, 2); })\n"
                ".catch(error => { status.textContent = `Unable to load data: ${error.message}`; });\n"
            ),
            "tests/test_app.py": "from fastapi.testclient import TestClient\nfrom main import app\n\n\ndef test_app_is_reachable():\n    assert TestClient(app).get('/').status_code == 200\n",
            "Dockerfile": "FROM python:3.12-slim\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY . .\nEXPOSE 8000\nCMD [\"uvicorn\", \"main:app\", \"--host\", \"0.0.0.0\", \"--port\", \"8000\"]\n",
            "docker-compose.yml": "services:\n  app:\n    build: .\n    ports: ['8000:8000']\n",
            "run.ps1": "docker compose up --build\n",
        }


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
        ProcessTool(),
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
    "DeploymentTool",
    "FilesystemTool",
    "GitTool",
    "ProjectTool",
    "ShellTool",
    "register_actions",
]
