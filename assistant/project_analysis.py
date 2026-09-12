from __future__ import annotations

import ast
import asyncio
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
