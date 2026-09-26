from __future__ import annotations

import ast
import asyncio
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain.models import ErrorType, OperationResult
from .project_audit.analyzer import ProjectAuditMixin


class ProjectAnalyzer(ProjectAuditMixin):
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
                if path.suffix.lower() in {
                    ".py", ".java", ".kt", ".kts", ".js", ".jsx", ".ts", ".tsx",
                    ".cs", ".go", ".rs", ".swift", ".rb", ".php",
                }:
                    modules.append(
                        {
                            "module": str(path.relative_to(started_files)).replace("\\", "/"),
                            "file": str(path.relative_to(started_files)).replace("\\", "/"),
                        }
                    )
                if path.suffix.lower() == ".py":
                    self._analyze_python(path, started_files, symbols, edges)
                elif path.suffix.lower() in {
                    ".java", ".kt", ".kts", ".js", ".jsx", ".ts", ".tsx",
                    ".cs", ".go", ".rs", ".swift", ".rb", ".php",
                }:
                    self._analyze_source_symbols(path, started_files, symbols, edges)
            key_files = self._key_files(started_files, files)
            output = {
                "root": str(started_files),
                "files_analyzed": [str(path.relative_to(started_files)) for path in files],
                "key_files": [str(path.relative_to(started_files)) for path in key_files],
                "file_count": len(files),
                "languages": dict(languages),
                "project_kind": self._classify_project(started_files, files),
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
        files = [
            path
            for path in root.rglob("*")
            if path.is_file()
            and not ignored.intersection(path.parts)
            and not any(part.endswith(".egg-info") for part in path.parts)
            and path.name not in {".env", ".env.local", ".env.production"}
            and path.suffix.lower() not in {".pyc", ".pyo", ".db", ".log"}
            and not path.name.lower().endswith((".db-wal", ".db-shm"))
        ]
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

    @staticmethod
    def _analyze_source_symbols(
        path: Path,
        root: Path,
        symbols: list[dict[str, Any]],
        edges: list[dict[str, str]],
    ) -> None:
        """Extract a small, language-neutral symbol index for non-Python projects."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        source = str(path.relative_to(root)).replace("\\", "/")
        patterns = (
            (r"\b(?:export\s+)?(?:abstract\s+)?(?:class|interface|enum|struct|trait)\s+([A-Za-z_]\w*)", "type"),
            (r"\b(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)", "function"),
            (r"\b(?:pub\s+)?fn\s+([A-Za-z_]\w*)", "function"),
            (r"\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(", "function"),
            (r"\bdef\s+([A-Za-z_]\w*)\s*\(", "function"),
        )
        seen: set[tuple[int, str]] = set()
        for pattern, kind in patterns:
            for match in re.finditer(pattern, text):
                line = text.count("\n", 0, match.start()) + 1
                name = match.group(1)
                key = (line, name)
                if key in seen:
                    continue
                seen.add(key)
                symbols.append({"kind": kind, "name": name, "file": source, "line": line})
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ", "using ", "package ", "require(")):
                edges.append({
                    "from": source,
                    "to": stripped[:300],
                    "kind": "imports",
                    "line": str(line_number),
                })


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
