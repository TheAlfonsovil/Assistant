from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from assistant.domain.models import ErrorType, OperationResult


async def edit_project(args: dict[str, Any], timeout: float) -> OperationResult:
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
    if not isinstance(commands, list) or any(not isinstance(command, str) or not command.strip() for command in commands):
        return OperationResult(success=False, error="commands must contain non-empty strings", error_type=ErrorType.INVALID_ARGUMENT)
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
        _restore_files(originals)
        return OperationResult(success=False, error=str(error), error_type=ErrorType.INVALID_ARGUMENT)
    validation = []
    for command in commands:
        process = await asyncio.create_subprocess_shell(command, cwd=project_root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            _restore_files(originals)
            return OperationResult(success=False, output={"feature": feature, "files": written, "validation": validation}, error=f"validation command timed out: {command}", error_type=ErrorType.TIMEOUT, retryable=False)
        validation.append({"command": command, "exit_code": process.returncode, "stdout": stdout.decode(errors="replace")[-4000:], "stderr": stderr.decode(errors="replace")[-4000:]})
        if process.returncode != 0:
            _restore_files(originals)
            return OperationResult(success=False, output={"feature": feature, "files": written, "validation": validation}, error=f"validation command failed: {command}", error_type=ErrorType.TOOL_FAILURE)
    return OperationResult(success=True, output={"feature": feature, "files": written, "deleted": deleted, "validation": validation}, side_effects=["project.modified"])


def _restore_files(originals: dict[Path, bytes | None]) -> None:
    for path, content in originals.items():
        if content is None:
            if path.is_file():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)