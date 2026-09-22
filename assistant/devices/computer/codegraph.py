"""Code graph action for architecture and dependency exploration."""

from __future__ import annotations

from typing import Any

from assistant.domain.models import ErrorType, OperationResult
from assistant.project_analysis import ProjectAnalyzer, SystemGraphAnalyzer
from assistant.tools import Tool, ToolDefinition


class CodeGraphTool(Tool):
    definition = ToolDefinition(
        name="codegraph",
        description="Build or query bounded project and Assistant relationship graphs",
        methods=["build", "system", "query"],
        argument_schema={
            "root": {"type": "string"},
            "max_files": {"type": "integer"},
            "query": {"type": "string", "description": "Optional text to match files, modules, or symbols."},
            "kind": {"type": "string", "description": "Optional node kind filter: module or symbol."},
            "limit": {"type": "integer", "description": "Maximum matching nodes to return (1-500)."},
        },
        permissions=["filesystem.read", "project.analysis"],
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        if method not in {"build", "system", "query"} or not isinstance(args.get("root"), str):
            return OperationResult(
                success=False,
                error="codegraph requires a root directory and a supported method",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        if method == "system":
            return await SystemGraphAnalyzer().analyze(
                args["root"], int(args.get("max_files", 300))
            )
        persisted = args.get("_persisted_graph")
        if method == "query" and isinstance(persisted, dict) and persisted.get("graph"):
            result = OperationResult(success=True, output=persisted)
        else:
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
        if method == "query":
            graph = output["graph"]
            query = str(args.get("query") or "").casefold()
            kind = str(args.get("kind") or "").casefold()
            limit = min(max(int(args.get("limit", 100)), 1), 500)
            nodes = [
                node for node in graph.get("nodes", [])
                if (not kind or str(node.get("kind", "")).casefold() == kind)
                and (
                    not query
                    or query in str(node.get("id", "")).casefold()
                    or query in str(node.get("name", "")).casefold()
                    or query in str(node.get("file", "")).casefold()
                )
            ][:limit]
            node_ids = {node.get("id") for node in nodes}
            edges = [
                edge for edge in graph.get("edges", [])
                if edge.get("from") in node_ids or edge.get("to") in node_ids
            ][: limit * 3]
            result.output = {
                "root": result.output.get("root"),
                "query": query,
                "kind": kind or None,
                "nodes": nodes,
                "edges": edges,
                "truncated": len(nodes) == limit,
            }
            return result
        return result


__all__ = ["CodeGraphTool"]
