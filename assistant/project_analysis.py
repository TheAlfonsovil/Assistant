from __future__ import annotations

import ast
import asyncio
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain.models import ErrorType, OperationResult
from .observability import compact


class ProjectAnalyzer:
    """Builds a bounded structural inventory and dependency graph for a local project."""

    async def analyze(self, root: str, max_files: int = 500) -> OperationResult:
        started_at = datetime.now(UTC)
        started_files = Path(root).resolve()
        try:
            files = await asyncio.to_thread(self._collect_files, started_files, max_files)
            languages = Counter(path.suffix.lower() or "[no extension]" for path in files)
            symbols: list[dict[str, Any]] = []
            edges: list[dict[str, str]] = []
            for path in files:
                if path.suffix.lower() == ".py":
                    self._analyze_python(path, started_files, symbols, edges)
            output = {
                "root": str(started_files),
                "files_analyzed": [str(path.relative_to(started_files)) for path in files],
                "file_count": len(files),
                "languages": dict(languages),
                "symbols": symbols[:2000],
                "dependency_edges": edges[:4000],
                "truncated": len(files) >= max_files,
            }
            finished_at = datetime.now(UTC)
            return OperationResult(
                success=True,
                output=compact(output),
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
        ignored = {".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__", ".gradle"}
        files = [path for path in root.rglob("*") if path.is_file() and not ignored.intersection(path.parts)]
        return sorted(files)[:max_files]

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
