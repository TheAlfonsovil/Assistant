"""Code graph action for architecture and dependency exploration."""

from __future__ import annotations

import re
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
            "query": {"type": "string", "description": "Space-separated terms matched independently against files, modules, and symbols."},
            "kind": {"type": "string", "description": "Optional node kind filter: module or symbol."},
            "limit": {"type": "integer", "description": "Maximum matching nodes to return (1-500)."},
            "detail": {"type": "string", "enum": ["summary", "full"]},
        },
        method_argument_schema={
            "build": {
                "root": {"type": "string", "required": True},
                "max_files": {"type": "integer"},
            },
            "system": {
                "root": {"type": "string", "required": True},
                "max_files": {"type": "integer"},
                "detail": {"type": "string", "enum": ["summary", "full"]},
            },
            "query": {
                "root": {"type": "string", "required": True},
                "max_files": {"type": "integer"},
                "query": {"type": "string"},
                "kind": {"type": "string", "enum": ["module", "symbol"]},
                "limit": {"type": "integer"},
                "detail": {"type": "string", "enum": ["summary", "full"]},
            },
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
            result = await SystemGraphAnalyzer().analyze(
                args["root"], int(args.get("max_files", 300))
            )
            if not result.success or args.get("detail", "summary") == "full":
                return result
            graph = result.output.get("graph", {})
            nodes = graph.get("nodes", [])
            edges = graph.get("edges", [])
            modules = [node for node in nodes if node.get("kind") == "module"]
            symbols = [node for node in nodes if node.get("kind") == "symbol"]
            edge_kinds: dict[str, int] = {}
            for edge in edges:
                kind = str(edge.get("kind", "unknown"))
                edge_kinds[kind] = edge_kinds.get(kind, 0) + 1
            result.output = {
                "root": result.output.get("root"),
                "file_count": len(result.output.get("files_analyzed", [])),
                "files_sample": result.output.get("files_analyzed", [])[:40],
                "graph": {
                    "nodes": [*modules[:40], *symbols[:40]],
                    "edges": edges[:60],
                    "truncated": graph.get("truncated", False),
                },
                "graph_summary": {
                    "module_count": len(modules),
                    "symbol_count": len(symbols),
                    "edge_count": len(edges),
                    "edge_kinds": edge_kinds,
                    "truncated": graph.get("truncated", False),
                    "module_sample": modules[:40],
                    "symbol_sample": symbols[:40],
                    "edge_sample": edges[:60],
                },
                "next": {
                    "query": "Use codegraph.query with a distinctive term for more detail.",
                    "detail": "full is available only when the complete graph is necessary.",
                },
            }
            return result
        persisted = args.get("_persisted_graph")
        if (
            method == "query"
            and args.get("_graph_fresh") is True
            and isinstance(persisted, dict)
            and persisted.get("graph")
        ):
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
            terms = [term for term in re.findall(r"[a-z0-9_]+", query) if len(term) > 1]
            candidates = []
            for node in graph.get("nodes", []):
                if kind and str(node.get("kind", "")).casefold() != kind:
                    continue
                haystack = " ".join(
                    str(node.get(field, "")).casefold()
                    for field in ("id", "name", "file")
                )
                matched = [term for term in terms if term in haystack]
                if terms and not matched:
                    continue
                candidates.append((len(matched), node))
            candidates.sort(key=lambda item: (-item[0], str(item[1].get("id", ""))))
            nodes = [node for _, node in candidates[:limit]]
            node_ids = {node.get("id") for node in nodes}
            edges = [
                edge for edge in graph.get("edges", [])
                if edge.get("from") in node_ids or edge.get("to") in node_ids
            ][: limit * 3]
            result.output = {
                "root": result.output.get("root"),
                "query": query,
                "query_terms": terms,
                "kind": kind or None,
                "nodes": nodes,
                "edges": edges,
                "truncated": len(nodes) == limit,
            }
            return result
        return result


__all__ = ["CodeGraphTool"]
