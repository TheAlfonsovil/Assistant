from __future__ import annotations

import ast
import asyncio
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain.models import ErrorType, OperationResult


class ProjectAnalyzer:
    """Builds a bounded structural inventory and dependency graph for a local project."""

    async def analyze(self, root: str, max_files: int = 500) -> OperationResult:
        started_at = datetime.now(UTC)
        started_files = Path(root).resolve()
        try:
            files = await asyncio.to_thread(self._collect_files, started_files, max_files)
            truncated = len(files) > max_files
            files = files[:max_files]
            languages = Counter(path.suffix.lower() or "[no extension]" for path in files)
            symbols: list[dict[str, Any]] = []
            edges: list[dict[str, str]] = []
            modules: list[dict[str, str]] = []
            for path in files:
                if path.suffix.lower() == ".py":
                    modules.append(
                        {
                            "module": path.with_suffix("").relative_to(started_files).as_posix().replace("/", "."),
                            "file": str(path.relative_to(started_files)),
                        }
                    )
                    self._analyze_python(path, started_files, symbols, edges)
            output = {
                "root": str(started_files),
                "files_analyzed": [str(path.relative_to(started_files)) for path in files],
                "file_count": len(files),
                "languages": dict(languages),
                "modules": modules[:2000],
                "symbols": symbols[:2000],
                "dependency_edges": edges[:4000],
                "truncated": truncated,
            }
            finished_at = datetime.now(UTC)
            return OperationResult(
                success=True,
                output=output,
                started_at=started_at,
                finished_at=finished_at,
                duration=(finished_at - started_at).total_seconds(),
            )
        except (OSError, ValueError) as error:
            return OperationResult(
                success=False,
                error=str(error),
                error_type=ErrorType.NOT_FOUND if isinstance(error, FileNotFoundError) else ErrorType.INVALID_ARGUMENT,
                retryable=False,
                started_at=started_at,
            )

    async def audit(
        self,
        root: str,
        max_files: int = 500,
        timeout: float = 60.0,
        run_tests: bool = False,
    ) -> OperationResult:
        """Collect safe project evidence and optionally run detected tests."""
        structural = await self.analyze(root, max_files)
        if not structural.success:
            return structural
        project_root = Path(root).resolve()
        files = [Path(project_root / relative) for relative in structural.output["files_analyzed"]]
        manifests = {
            "pyproject.toml",
            "requirements.txt",
            "requirements-dev.txt",
            "package.json",
            "package-lock.json",
            "poetry.lock",
            "Pipfile",
            "Pipfile.lock",
            ".gitignore",
            ".env.example",
        }
        configuration: dict[str, str] = {}
        redaction = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|private[_-]?key)")
        for path in files:
            if path.name not in manifests or path.stat().st_size > 128 * 1024:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            lines = []
            for line in text.splitlines():
                key = line.split("=", 1)[0].strip() if "=" in line else ""
                lines.append(f"{key}=<redacted>" if key and redaction.search(key) else line)
            configuration[path.relative_to(project_root).as_posix()] = "\n".join(lines)

        test_files = [
            path.relative_to(project_root).as_posix()
            for path in files
            if path.name.startswith("test_")
            or path.name.endswith("_test.py")
            or path.parts[-2:-1] == ("tests",)
        ]
        sensitive_files = [
            path.relative_to(project_root).as_posix()
            for path in files
            if path.name in {".env", ".env.local", ".env.production"}
        ]
        test_command = self._test_command(project_root, configuration, test_files)
        if run_tests and test_command:
            test_result = await self._run_test_command(test_command, project_root, timeout)
        elif run_tests:
            test_result = {
                "available": False,
                "reason": "No supported test command or test files detected",
            }
        else:
            test_result = {
                "available": bool(test_command),
                "command": test_command,
                "executed": False,
                "reason": "Tests were detected but not run; set run_tests=true to execute them.",
            }
        structural.output.update({
            "audit": {
                "configuration": configuration,
                "dependency_manifests": [name for name in configuration if Path(name).name not in {".gitignore", ".env.example"}],
                "test_files": test_files[:500],
                "test_command": test_command,
                "run_tests": run_tests,
                "test_result": test_result,
                "sensitive_files": sensitive_files,
                "sensitive_file_contents_read": False,
                "limitations": [
                    "Secret-bearing environment files are detected but never read.",
                    "Sonar analysis is not run automatically; availability must be checked separately.",
                ],
            }
        })
        return structural

    @staticmethod
    def _test_command(root: Path, configuration: dict[str, str], test_files: list[str]) -> str | None:
        if "pyproject.toml" in configuration and test_files:
            return "python -m pytest -q"
        if "requirements.txt" in configuration and test_files:
            return "python -m pytest -q"
        if "package.json" in configuration:
            try:
                scripts = json.loads(configuration["package.json"]).get("scripts", {})
            except json.JSONDecodeError:
                scripts = {}
            if "test" in scripts:
                return "npm test -- --if-present"
        return None

    @staticmethod
    async def _run_test_command(command: str, cwd: Path, timeout: float) -> dict[str, object]:
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=max(1.0, timeout))
            return {
                "available": True,
                "command": command,
                "exit_code": process.returncode,
                "stdout": stdout.decode(errors="replace")[-12000:],
                "stderr": stderr.decode(errors="replace")[-12000:],
            }
        except TimeoutError:
            process.kill()
            return {"available": True, "command": command, "timed_out": True}
        except OSError as error:
            return {"available": True, "command": command, "error": str(error)}
    @staticmethod
    def _collect_files(root: Path, max_files: int) -> list[Path]:
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(f"Project directory does not exist: {root}")
        ignored = {
            ".git",
            ".venv",
            "venv",
            "node_modules",
            "build",
            "dist",
            "__pycache__",
            ".gradle",
            ".pytest_cache",
            ".ruff_cache",
        }
        files = [path for path in root.rglob("*") if path.is_file() and not ignored.intersection(path.parts)]
        return sorted(files)[: max_files + 1]

    @staticmethod
    def _analyze_python(path: Path, root: Path, symbols: list[dict[str, Any]], edges: list[dict[str, str]]) -> None:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            return
        source = str(path.relative_to(root))
        module = path.with_suffix("").relative_to(root).as_posix().replace("/", ".")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.append({"kind": type(node).__name__, "name": node.name, "file": source, "line": node.lineno})
                if isinstance(node, ast.ClassDef):
                    class_id = f"{source}:{node.lineno}:{node.name}"
                    for base in node.bases:
                        edges.append({"from": class_id, "to": ast.unparse(base), "kind": "inherits"})
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call):
                            edges.append({"from": class_id, "to": ast.unparse(child.func), "kind": "composes"})
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    edges.append({"from": module, "to": alias.name, "kind": "imports"})
            elif isinstance(node, ast.ImportFrom):
                edges.append({"from": module, "to": node.module or "", "kind": "imports"})


class SystemGraphAnalyzer:
    """Builds a bounded relationship graph for the Assistant source itself."""

    async def analyze(self, root: str, max_files: int = 300) -> OperationResult:
        started_at = datetime.now(UTC)
        try:
            project_root = Path(root).resolve()
            files = await asyncio.to_thread(ProjectAnalyzer._collect_files, project_root, max_files)
            files = [path for path in files[:max_files] if path.suffix.lower() == ".py"]
            nodes: list[dict[str, Any]] = []
            edges: list[dict[str, str]] = []
            symbols_by_name: dict[str, str] = {}
            parsed: list[tuple[Path, ast.AST, str]] = []

            for path in files:
                relative = path.relative_to(project_root).as_posix()
                module_id = f"module:{relative}"
                nodes.append({"id": module_id, "kind": "module", "name": relative})
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                except (OSError, SyntaxError, UnicodeDecodeError):
                    continue
                parsed.append((path, tree, module_id))
                for item in ast.walk(tree):
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        symbol_id = f"symbol:{relative}:{item.lineno}:{item.name}"
                        nodes.append({
                            "id": symbol_id,
                            "kind": "symbol",
                            "name": item.name,
                            "file": relative,
                            "line": item.lineno,
                        })
                        edges.append({"from": module_id, "to": symbol_id, "kind": "contains"})
                        symbols_by_name.setdefault(item.name, symbol_id)

            for path, tree, module_id in parsed:
                for item in ast.walk(tree):
                    if isinstance(item, ast.Import):
                        for alias in item.names:
                            edges.append({"from": module_id, "to": f"import:{alias.name}", "kind": "imports"})
                    elif isinstance(item, ast.ImportFrom):
                        imported = item.module or "."
                        edges.append({"from": module_id, "to": f"import:{imported}", "kind": "imports"})
                    elif isinstance(item, ast.Call):
                        called = item.func.id if isinstance(item.func, ast.Name) else None
                        target = symbols_by_name.get(called) if called else None
                        if target:
                            edges.append({"from": module_id, "to": target, "kind": "calls"})

            output = {
                "root": str(project_root),
                "files_analyzed": [path.relative_to(project_root).as_posix() for path in files],
                "graph": {
                    "nodes": nodes[:4000],
                    "edges": edges[:8000],
                    "truncated": len(nodes) > 4000 or len(edges) > 8000,
                },
            }
            finished_at = datetime.now(UTC)
            return OperationResult(
                success=True,
                output=output,
                started_at=started_at,
                finished_at=finished_at,
                duration=(finished_at - started_at).total_seconds(),
            )
        except (OSError, ValueError) as error:
            return OperationResult(
                success=False,
                error=str(error),
                error_type=ErrorType.NOT_FOUND if isinstance(error, FileNotFoundError) else ErrorType.INVALID_ARGUMENT,
                retryable=False,
                started_at=started_at,
            )
