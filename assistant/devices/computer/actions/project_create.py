from __future__ import annotations

import json
import re
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from assistant.domain.models import ErrorType, OperationResult


async def create_project(args: dict[str, Any]) -> OperationResult:
    root = args.get("root")
    name = args.get("name")
    kind = str(args.get("kind") or "workspace").strip().lower()
    description = str(args.get("description") or "").strip()
    directories = args.get("directories", [])
    files = args.get("files", [])
    if not isinstance(root, str) or not isinstance(name, str) or not name.strip():
        return OperationResult(success=False, error="project.create requires root and name", error_type=ErrorType.INVALID_ARGUMENT)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name.strip()):
        return OperationResult(success=False, error="project name contains unsupported path characters", error_type=ErrorType.INVALID_ARGUMENT)
    if not isinstance(directories, list) or any(not isinstance(item, str) or not item.strip() for item in directories):
        return OperationResult(success=False, error="directories must contain non-empty relative paths", error_type=ErrorType.INVALID_ARGUMENT)
    if not isinstance(files, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("path"), str)
        or not isinstance(item.get("content", ""), str)
        for item in files
    ):
        return OperationResult(success=False, error="files must contain objects with path and optional string content", error_type=ErrorType.INVALID_ARGUMENT)
    if any(not item["path"].strip("/\\") for item in files):
        return OperationResult(success=False, error="file paths must be non-empty relative paths", error_type=ErrorType.INVALID_ARGUMENT)
    if any(
        PurePosixPath(item["path"].strip().replace("\\", "/")).drive
        or PureWindowsPath(item["path"].strip()).drive
        for item in files
    ):
        return OperationResult(success=False, error="file paths must be relative to the project root", error_type=ErrorType.INVALID_ARGUMENT)
    if len(files) > 200 or len(directories) > 100:
        return OperationResult(success=False, error="workspace artifact limits exceeded", error_type=ErrorType.INVALID_ARGUMENT)
    projects_root = Path(root).resolve()
    project_root = (projects_root / name.strip()).resolve()
    if project_root.parent != projects_root:
        return OperationResult(success=False, error="project name must create a direct child of root", error_type=ErrorType.INVALID_ARGUMENT)
    existing = project_root.exists()
    artifacts: dict[str, str] = {}
    for item in files:
        relative = item["path"].strip().replace("\\", "/").strip("/")
        if relative and relative != ".assistant/project.json":
            artifacts[relative] = item.get("content", "")
    normalized_directories = [
        item
        for item in (entry.strip().replace("\\", "/").strip("/") for entry in directories)
        if item and item != "."
    ]
    manifest = {
        "name": name.strip(),
        "kind": kind or "workspace",
        "description": description,
        "objective": description,
        "created_by": "assistant.project.create",
        "artifacts": sorted(artifacts),
    }
    generated = {
        ".assistant/project.json": json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        **artifacts,
    }
    try:
        project_root.mkdir(parents=True, exist_ok=True)
        for relative in normalized_directories:
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
        if not existing:
            shutil.rmtree(project_root, ignore_errors=True)
        return OperationResult(success=False, error=str(error), error_type=ErrorType.TOOL_FAILURE)
    return OperationResult(
        success=True,
        output={
            "path": str(project_root),
            "name": name.strip(),
            "kind": kind or "workspace",
            "manifest": manifest,
            "files": sorted(generated),
            "directories": sorted(normalized_directories),
            "existing": existing,
            "verified": all((project_root / relative).is_file() for relative in generated),
        },
        side_effects=["project.created"],
    )