from __future__ import annotations

from typing import Any

from assistant.domain.models import ErrorType, OperationResult
from assistant.project_analysis import ProjectAnalyzer
from assistant.tools import Tool, ToolDefinition


class AuditTool(Tool):
    definition = ToolDefinition(
        name="audit",
        description=(
            "Collect a structured project audit when requested. "
            "Detected tests run by default; set run_tests=false to skip them."
        ),
        methods=["run"],
        evidence={"run": {"success_fields": ["root", "audit_report"]}},
        argument_schema={
            "root": {"type": "string", "required": True},
            "max_files": {"type": "integer"},
            "timeout": {"type": "number"},
            "run_tests": {"type": "boolean", "default": True},
            "profile": {"type": "string", "default": "general"},
            "objective": {"type": "string"},
            "scope": {"type": "array"},
            "depth": {
                "type": "string",
                "enum": ["shallow", "standard", "deep"],
                "default": "standard",
            },
            "accepted_constraints": {"type": "array"},
            "include": {"type": "array"},
            "exclude": {"type": "array"},
            "scoring": {"type": "boolean", "default": True},
        },
        method_argument_schema={
            "run": {
                "root": {"type": "string", "required": True},
                "max_files": {"type": "integer"},
                "timeout": {"type": "number"},
                "run_tests": {"type": "boolean"},
                "profile": {"type": "string"},
                "objective": {"type": "string"},
                "scope": {"type": "array"},
                "depth": {
                    "type": "string",
                    "enum": ["shallow", "standard", "deep"],
                },
                "accepted_constraints": {"type": "array"},
                "include": {"type": "array"},
                "exclude": {"type": "array"},
                "scoring": {"type": "boolean"},
            }
        },
        permissions=["project.analysis"],
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        if method != "run":
            return OperationResult(
                success=False,
                error="audit supports only the run method",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        root = args.get("root")
        if not isinstance(root, str):
            return OperationResult(
                success=False,
                error="audit.run requires a project root",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        run_tests = args.get("run_tests", True)
        scoring = args.get("scoring", True)
        if not isinstance(run_tests, bool) or not isinstance(scoring, bool):
            return OperationResult(
                success=False,
                error="run_tests and scoring must be booleans",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        for field in ("scope", "accepted_constraints", "include", "exclude"):
            value = args.get(field)
            if value is not None and (
                not isinstance(value, list)
                or any(not isinstance(item, str) for item in value)
            ):
                return OperationResult(
                    success=False,
                    error=f"audit.run {field} must be an array of strings",
                    error_type=ErrorType.INVALID_ARGUMENT,
                )
        return await ProjectAnalyzer().audit(
            root,
            int(args.get("max_files", 500)),
            float(args.get("timeout", timeout)),
            run_tests=run_tests,
            profile=str(args.get("profile", "general")),
            objective=str(args.get("objective", "Assess the target and report actionable findings.")),
            scope=args.get("scope"),
            depth=str(args.get("depth", "standard")),
            accepted_constraints=args.get("accepted_constraints"),
            include=args.get("include"),
            exclude=args.get("exclude"),
            scoring=scoring,
        )