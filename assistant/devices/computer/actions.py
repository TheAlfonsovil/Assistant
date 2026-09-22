"""Actions available on the real computer branch.

Add a computer action here, then expose it from ``register_actions``. The task
engine only sees the stable Tool contract and does not know about OS details.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
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
        description=(
            "Inspect, read, audit, create, or edit a local project. Read/edit are "
            "bounded to the registered project root."
        ),
        methods=["analyze", "read", "audit", "create", "scaffold", "modify", "edit"],
        evidence={
            "analyze": {"success_fields": ["root", "files_analyzed", "file_count", "languages"]},
            "read": {"success_fields": ["root", "files"]},
            "edit": {"success_fields": ["feature", "files", "deleted", "validation"]},
            "modify": {"success_fields": ["feature", "files", "validation"]},
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
            "name": {"type": "string"},
            "template": {
                "type": "string",
                "description": "Optional project template identifier for create.",
            },
            "frontend": {"type": "object", "description": "Frontend stack, for example {framework: 'vue'}."},
            "backend": {"type": "object", "description": "Backend stack, for example {language: 'java', framework: 'spring-boot'}."},
            "containerize": {"type": "boolean", "default": True},
            "device": {"type": "string", "description": "Target device, for example computer."},
            "os": {"type": "string", "description": "Target operating system, for example windows-11."},
            "feature": {"type": "string", "description": "Feature to add or modify."},
            "changes": {"type": "array", "description": "Files to write, each with path and content."},
            "deletions": {"type": "array", "description": "Relative files to delete, bounded by the project root."},
            "commands": {"type": "array", "description": "Optional validation commands to run after modification."},
        },
        permissions=["filesystem.read", "project.analysis"],
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
        return await analyzer.analyze(args["root"], int(args.get("max_files", 500)))

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
        if frontend_framework not in {"vue", "vue3"} or backend_framework not in {"spring-boot", "springboot"}:
            return OperationResult(
                success=False,
                error="project.scaffold currently supports frontend.framework=vue and backend.framework=spring-boot",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        if str(backend.get("language", "")).casefold() != "java" or str(backend.get("java_version", "25")) != "25":
            return OperationResult(
                success=False,
                error="project.scaffold currently requires backend.language=java and backend.java_version=25",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        containerize = args.get("containerize", True)
        if not isinstance(containerize, bool):
            return OperationResult(success=False, error="containerize must be a boolean", error_type=ErrorType.INVALID_ARGUMENT)
        files = self._vue_spring_files(name.strip(), frontend, backend, args)
        if not containerize:
            files = {
                path: content
                for path, content in files.items()
                if path not in {"docker-compose.yml", "backend/Dockerfile", "frontend/Dockerfile", "run.ps1"}
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

    async def _modify(self, args: dict[str, Any], timeout: float) -> OperationResult:
        root = args.get("root")
        feature = args.get("feature")
        changes = args.get("changes")
        deletions = args.get("deletions", [])
        if not isinstance(root, str) or not isinstance(feature, str) or not feature.strip():
            return OperationResult(success=False, error="project.edit requires root and feature", error_type=ErrorType.INVALID_ARGUMENT)
        if not isinstance(changes, list) or not changes:
            return OperationResult(success=False, error="project.edit requires a non-empty changes array", error_type=ErrorType.INVALID_ARGUMENT)
        if not isinstance(deletions, list) or any(not isinstance(item, str) or not item.strip() for item in deletions):
            return OperationResult(success=False, error="deletions must contain relative file paths", error_type=ErrorType.INVALID_ARGUMENT)
        project_root = Path(root).resolve()
        if not project_root.is_dir():
            return OperationResult(success=False, error=f"project directory does not exist: {project_root}", error_type=ErrorType.NOT_FOUND)
        written = []
        deleted = []
        try:
            for change in changes:
                if not isinstance(change, dict) or not isinstance(change.get("path"), str) or not isinstance(change.get("content"), str):
                    raise TypeError("each change requires string path and content")
                path = (project_root / change["path"]).resolve()
                if project_root not in path.parents:
                    raise ValueError(f"change path escapes project: {change['path']}")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(change["content"], encoding="utf-8")
                written.append(str(path.relative_to(project_root)))
            for relative in deletions:
                path = (project_root / relative).resolve()
                if project_root not in path.parents:
                    raise ValueError(f"deletion path escapes project: {relative}")
                if path.exists():
                    if not path.is_file():
                        raise ValueError(f"deletion target is not a file: {relative}")
                    path.unlink()
                    deleted.append(str(path.relative_to(project_root)))
        except (OSError, ValueError) as error:
            return OperationResult(success=False, error=str(error), error_type=ErrorType.INVALID_ARGUMENT)
        validation = []
        for command in args.get("commands", []):
            if not isinstance(command, str) or not command.strip():
                return OperationResult(success=False, error="commands must contain non-empty strings", error_type=ErrorType.INVALID_ARGUMENT)
            process = await asyncio.create_subprocess_shell(
                command, cwd=project_root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            validation.append({"command": command, "exit_code": process.returncode, "stdout": stdout.decode(errors="replace")[-4000:], "stderr": stderr.decode(errors="replace")[-4000:]})
            if process.returncode != 0:
                return OperationResult(success=False, output={"feature": feature, "files": written, "validation": validation}, error=f"validation command failed: {command}", error_type=ErrorType.TOOL_FAILURE)
        return OperationResult(success=True, output={"feature": feature, "files": written, "deleted": deleted, "validation": validation}, side_effects=["project.modified"])

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
            "docker-compose.yml": "services:\n  backend:\n    build: ./backend\n    ports: ['8080:8080']\n  frontend:\n    build: ./frontend\n    ports: ['5173:5173']\n    depends_on: [backend]\n",
            "backend/Dockerfile": "FROM maven:3.9-eclipse-temurin-25 AS build\nWORKDIR /app\nCOPY pom.xml .\nRUN mvn -q -DskipTests dependency:go-offline\nCOPY src src\nRUN mvn -q package -DskipTests\nFROM eclipse-temurin:25-jre\nCOPY --from=build /app/target/*.jar /app/app.jar\nENTRYPOINT [\"java\", \"-jar\", \"/app/app.jar\"]\n",
            "frontend/Dockerfile": "FROM node:22-alpine\nWORKDIR /app\nCOPY package*.json ./\nRUN npm install\nCOPY . .\nCMD [\"npm\", \"run\", \"dev\", \"--\", \"--host\", \"0.0.0.0\"]\n",
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
