"""Code graph action for architecture and dependency exploration."""

from __future__ import annotations

from typing import Any

from assistant.domain.models import ErrorType, OperationResult
from assistant.project_analysis import ProjectAnalyzer, SystemGraphAnalyzer
from assistant.tools import Tool, ToolDefinition


class CodeGraphTool(Tool):
    definition = ToolDefinition(
        name="codegraph",
        description="Build bounded project or Assistant system relationship graphs",
        methods=["build", "system"],
        argument_schema={
            "root": {"type": "string"},
            "max_files": {"type": "integer"},
        },
        permissions=["filesystem.read", "project.analysis"],
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        if method not in {"build", "system"} or not isinstance(args.get("root"), str):
            return OperationResult(
                success=False,
                error="codegraph requires a root directory and a supported method",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        if method == "system":
            return await SystemGraphAnalyzer().analyze(
                args["root"], int(args.get("max_files", 300))
            )
        result = await ProjectAnalyzer().analyze(args["root"], int(args.get("max_files", 500)))
        if not result.success:
            return result
        output = result.output
        output["graph"] = {
            "nodes": [
                *[
                    {"id": item["module"], "kind": "module", "file": item["file"]}
                    for item in output.get("modules", [])
                ],
                *[
                    {
                        "id": f"{item['file']}:{item['line']}:{item['name']}",
                        "kind": "symbol",
                        "name": item["name"],
                        "file": item["file"],
                        "line": item["line"],
                    }
                    for item in output.get("symbols", [])
                ],
            ],
            "edges": output.get("dependency_edges", []),
        }
        return result


__all__ = ["CodeGraphTool"]
