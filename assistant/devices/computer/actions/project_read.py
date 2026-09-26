from __future__ import annotations

from pathlib import Path
from typing import Any

from assistant.domain.models import ErrorType, OperationResult


async def read_project(args: dict[str, Any]) -> OperationResult:
    root = args.get("root")
    files = args.get("files")
    if not isinstance(root, str) or not isinstance(files, list) or not files:
        return OperationResult(success=False, error="project.read requires root and a non-empty files array", error_type=ErrorType.INVALID_ARGUMENT)
    if len(files) > 20 or any(
        not isinstance(item, str | dict)
        or (isinstance(item, str) and not item.strip())
        or (isinstance(item, dict) and not isinstance(item.get("path"), str))
        for item in files
    ):
        return OperationResult(success=False, error="project.read accepts at most 20 non-empty relative file paths or ranges", error_type=ErrorType.INVALID_ARGUMENT)
    project_root = Path(root).resolve()
    if not project_root.is_dir():
        return OperationResult(success=False, error=f"project directory does not exist: {project_root}", error_type=ErrorType.NOT_FOUND)
    contents: dict[str, str] = {}
    metadata: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    total_bytes = 0
    max_chars = min(max(int(args.get("max_chars", 120_000)), 1), 500_000)
    for item in files:
        relative = item if isinstance(item, str) else item["path"]
        path = (project_root / relative).resolve()
        try:
            if project_root not in path.parents or not path.is_file():
                raise FileNotFoundError(relative)
            if path.stat().st_size > 200_000:
                raise ValueError(f"file is too large to read: {relative}")
            source_text = path.read_text(encoding="utf-8")
            text = source_text
            start_line = 1
            end_line = None
            if isinstance(item, dict):
                start = item.get("start_line", 1)
                end = item.get("end_line")
                if not isinstance(start, int) or start < 1 or (
                    end is not None and (not isinstance(end, int) or end < start)
                ):
                    raise ValueError(f"invalid line range for: {relative}")
                start_line = start
                end_line = end
                lines = text.splitlines(keepends=True)
                text = "".join(lines[start - 1:end])
            source_text = text
            source_lines = source_text.splitlines(keepends=True)
            remaining = max_chars - sum(len(value) for value in contents.values())
            if remaining <= 0:
                errors.append({"path": str(relative), "error": "read character budget exhausted"})
                continue
            truncated = len(text) > remaining
            truncated_at_line = None
            if truncated:
                consumed = 0
                selected_lines = []
                for offset, line in enumerate(source_lines):
                    if consumed + len(line) > remaining:
                        truncated_at_line = start_line + offset
                        break
                    selected_lines.append(line)
                    consumed += len(line)
                text = "".join(selected_lines)
                errors.append({"path": str(relative), "error": f"content truncated at line {truncated_at_line} by max_chars"})
            total_bytes += len(text.encode("utf-8"))
            key = str(path.relative_to(project_root))
            contents[key] = text
            metadata[key] = {
                "source_chars": len(source_text),
                "returned_chars": len(text),
                "source_lines": len(source_lines),
                "returned_lines": len(text.splitlines()),
                "requested_start_line": start_line,
                "requested_end_line": end_line,
                "truncated": truncated,
                "truncated_at_line": truncated_at_line,
            }
        except FileNotFoundError:
            errors.append({"path": str(relative), "error": "file not found"})
        except (OSError, ValueError) as error:
            errors.append({"path": str(relative), "error": str(error)})
    if not contents:
        return OperationResult(success=False, error="project.read could not read any requested file", error_type=ErrorType.NOT_FOUND, metadata={"file_errors": errors})
    return OperationResult(
        success=True,
        output={
            "root": str(project_root),
            "files": contents,
            "file_metadata": metadata,
            "source_bytes": total_bytes,
            "file_errors": errors,
        },
    )