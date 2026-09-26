from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from assistant.domain.models import ErrorType, OperationResult
from assistant.tools import Tool, ToolDefinition

class FilesystemTool(Tool):
    definition = ToolDefinition(
        name="filesystem",
        description="Local filesystem operations",
        methods=["read", "write", "create", "delete", "list", "exists", "info", "search", "search_text"],
        argument_schema={
            "path": {"type": "string", "required": True},
            "content": {"type": "string"},
            "pattern": {"type": "string"},
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "case_sensitive": {"type": "boolean"},
            "mode": {"type": "string", "enum": ["all", "any"]},
            "context_lines": {"type": "integer"},
        },
        method_argument_schema={
            "read": {"path": {"type": "string", "required": True}},
            "write": {
                "path": {"type": "string", "required": True},
                "content": {"type": "string", "required": True},
            },
            "create": {"path": {"type": "string", "required": True}, "content": {"type": "string"}},
            "delete": {"path": {"type": "string", "required": True}},
            "list": {"path": {"type": "string", "required": True}},
            "exists": {"path": {"type": "string", "required": True}},
            "info": {"path": {"type": "string", "required": True}},
            "search": {
                "path": {"type": "string", "required": True},
                "pattern": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "search_text": {
                "path": {"type": "string", "required": True},
                "query": {"type": "string", "required": True},
                "pattern": {"type": "string"},
                "limit": {"type": "integer"},
                "case_sensitive": {"type": "boolean"},
                "mode": {"type": "string", "enum": ["all", "any"]},
                "context_lines": {"type": "integer"},
            },
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
            elif method == "search_text":
                if not path.is_dir():
                    raise ValueError("filesystem.search_text requires a directory")
                query = args.get("query")
                if not isinstance(query, str) or not query.strip():
                    raise ValueError("filesystem.search_text requires a non-empty query")
                terms = query.casefold().split()
                case_sensitive = bool(args.get("case_sensitive", False))
                mode = args.get("mode", "all")
                if mode not in {"all", "any"}:
                    raise ValueError("filesystem.search_text mode must be all or any")
                limit = min(max(int(args.get("limit", 100)), 1), 500)
                context_lines = min(max(int(args.get("context_lines", 1)), 0), 3)
                pattern = args.get("pattern", "*")
                ignored_parts = {
                    ".git", ".venv", "venv", "node_modules", "dist", "build",
                    "__pycache__", ".pytest_cache", ".ruff_cache",
                }
                sensitive_names = {".env", ".env.local", ".env.production"}
                matches: list[dict[str, Any]] = []
                for candidate in path.rglob(pattern):
                    if len(matches) >= limit:
                        break
                    if not candidate.is_file():
                        continue
                    if sensitive_names.intersection(part.casefold() for part in candidate.parts):
                        continue
                    if ignored_parts.intersection(part.casefold() for part in candidate.parts):
                        continue
                    try:
                        lines = candidate.read_text(encoding="utf-8").splitlines()
                    except (OSError, UnicodeDecodeError):
                        continue
                    for line_number, line in enumerate(lines, start=1):
                        haystack = line if case_sensitive else line.casefold()
                        needles = terms if not case_sensitive else query.split()
                        found = all(term in haystack for term in needles) if mode == "all" else any(
                            term in haystack for term in needles
                        )
                        if not found:
                            continue
                        start = max(1, line_number - context_lines)
                        end = min(len(lines), line_number + context_lines)
                        matches.append({
                            "path": str(candidate),
                            "line": line_number,
                            "text": line[:1000],
                            "context": [
                                {"line": index, "text": lines[index - 1][:1000]}
                                for index in range(start, end + 1)
                            ],
                        })
                        if len(matches) >= limit:
                            break
                output = {
                    "query": query,
                    "mode": mode,
                    "case_sensitive": case_sensitive,
                    "matches": matches,
                    "truncated": len(matches) >= limit,
                }
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
