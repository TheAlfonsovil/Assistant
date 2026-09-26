from __future__ import annotations

from typing import Any

from assistant.domain.models import ErrorType, OperationResult
from assistant.project_analysis import ProjectAnalyzer
from assistant.tools import Tool, ToolDefinition

from .project_create import create_project
from .project_edit import edit_project
from .project_read import read_project
from .project_validate import validate_project


class ProjectTool(Tool):
    """Project tool contract and dispatcher; handlers are isolated by operation."""

    definition = ToolDefinition(
        name="project",
        description="Inspect, read, create, validate, or edit a local project.",
        methods=["analyze", "read", "validate", "create", "edit"],
        evidence={
            "analyze": {"success_fields": ["root", "files_analyzed", "file_count", "languages"]},
            "read": {"success_fields": ["root", "files"]},
            "edit": {"success_fields": ["feature", "files", "deleted", "validation"]},
            "validate": {"success_fields": ["root", "commands", "validation_status"]},
            "create": {"success_fields": ["path", "name", "kind", "manifest", "files"]},
        },
        argument_schema={
            "root": {"type": "string"}, "max_files": {"type": "integer"},
            "files": {
                "type": "array",
                "description": "Relative paths or {path,start_line,end_line} ranges inside the project root.",
            },
            "max_chars": {"type": "integer", "description": "Maximum total characters returned by project.read."},
            "timeout": {"type": "number"}, "name": {"type": "string"},
            "kind": {"type": "string", "description": "Optional descriptive category; does not select a stack."},
            "description": {"type": "string", "description": "Project objective stored in its manifest."},
            "directories": {"type": "array", "description": "Relative directories to create."},
            "feature": {"type": "string", "description": "Feature being changed by project.edit."},
            "changes": {"type": "array", "description": "File changes with relative path and content."},
            "edit_operations": {"type": "array", "description": "File changes with write, append, prepend, or json_merge mode."},
            "deletions": {"type": "array", "description": "Relative files to delete."},
            "commands": {"type": "array", "description": "Explicit commands only; validation never infers a stack."},
        },
        method_argument_schema={
            "analyze": {"root": {"type": "string", "required": True}, "max_files": {"type": "integer"}},
            "read": {"root": {"type": "string", "required": True}, "files": {"type": "array", "required": True}, "max_chars": {"type": "integer"}},
            "validate": {"root": {"type": "string", "required": True}, "commands": {"type": "array", "required": True}, "timeout": {"type": "number"}},
            "create": {"root": {"type": "string", "required": True}, "name": {"type": "string", "required": True}, "kind": {"type": "string"}, "description": {"type": "string"}, "directories": {"type": "array"}, "files": {"type": "array"}},
            "edit": {"root": {"type": "string", "required": True}, "feature": {"type": "string", "required": True}, "changes": {"type": "array"}, "edit_operations": {"type": "array"}, "deletions": {"type": "array"}, "commands": {"type": "array"}, "timeout": {"type": "number"}},
        },
        permissions=["filesystem.read", "filesystem.write", "project.analysis", "project.edit"],
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        if method not in self.definition.methods:
            return OperationResult(success=False, error="project requires a supported method", error_type=ErrorType.INVALID_ARGUMENT)
        if method == "create":
            return await create_project(args)
        if method == "read":
            return await read_project(args)
        if method == "edit":
            return await edit_project(args, timeout)
        if method == "validate":
            return await validate_project(args, timeout)
        root = args.get("root")
        if not isinstance(root, str):
            return OperationResult(success=False, error="project requires a root directory", error_type=ErrorType.INVALID_ARGUMENT)
        return await ProjectAnalyzer().analyze(root, int(args.get("max_files", 500)))