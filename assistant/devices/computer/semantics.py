"""Semantic code queries: definitions, references and real types, from the LSP.

The codegraph answers "what exists and what imports what" by reading text. This
tool answers the questions text cannot: what a name really is, where it is
defined, and who depends on it. The answers come from pyright's language server,
so they are the same answers an editor would show, not a guess.

Two rules shape the output:

* It is bounded. A model asking for references to a common name gets the count,
  the first ``max_results`` sites and the reason for the cut, never a dump.
* It never invents availability. If node or pyright is missing, the answer is
  ``available: false`` plus what to install; nothing is fabricated.
"""

from __future__ import annotations

import re
from pathlib import Path
from time import monotonic
from typing import Any

from assistant.domain.models import ErrorType, OperationResult
from assistant.tools import ToolDefinition, Tool

from . import lsp

DEFAULT_MAX_RESULTS = 20
MAX_MAX_RESULTS = 100
MAX_TYPE_CHARS = 600
MAX_SYMBOL_CHARS = 120
MAX_MESSAGE_CHARS = 240
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SEVERITY_LABELS = {1: "error", 2: "warning", 3: "information", 4: "hint"}

# LSP SymbolKind numbers, only for the kinds a reader cares about.
SYMBOL_KINDS = {
    1: "file",
    2: "module",
    3: "namespace",
    4: "package",
    5: "class",
    6: "method",
    7: "property",
    8: "field",
    9: "constructor",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    22: "enum_member",
    23: "struct",
    24: "event",
    25: "operator",
    26: "type_parameter",
}
MAX_NAME_CHARS = 80


def relative(root: Path, path: str) -> str:
    """Workspace-relative path when possible: absolute paths waste tokens."""
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return str(path)


def locate_symbol(text: str, symbol: str) -> tuple[int, int, int]:
    """Find ``symbol`` in ``text``.

    Returns ``(line, character, matches)`` with 0-based line/character, which is
    what LSP wants, plus how many word occurrences exist so the caller can say
    when the answer may not be the one intended.
    """
    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    matches = list(pattern.finditer(text))
    if not matches:
        raise ValueError(f"symbol {symbol!r} was not found in the file")
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    first = matches[0]
    line_index = 0
    for index, offset in enumerate(offsets[1:], start=1):
        if first.start() >= offset:
            line_index = index
        else:
            break
    line_start = offsets[line_index]
    return line_index, first.start() - line_start, len(matches)


def _result_line(root: Path, item: dict[str, Any]) -> dict[str, Any] | None:
    uri = item.get("uri") or item.get("targetUri")
    if not uri:
        return None
    span = item.get("range") or item.get("targetSelectionRange") or item.get("targetRange") or {}
    start = span.get("start") or {}
    end = span.get("end") or {}
    path = lsp.uri_to_path(uri)
    return {
        "file": relative(root, path),
        "line": int(start.get("line", 0)) + 1,
        "column": int(start.get("character", 0)) + 1,
        "end_line": int(end.get("line", 0)) + 1,
    }


def normalize_workspace_edit(result: Any) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Turn either WorkspaceEdit shape into ``{uri: [edits]}``.

    The specification allows ``changes`` (a map of URI to edits) and
    ``documentChanges`` (an ordered list that may also carry file creation,
    rename or delete operations). pyright answers a rename with the second form,
    and a file operation cannot be applied as a text edit, so it is reported
    instead of silently ignored.
    """
    if not isinstance(result, dict):
        return {}, []
    changes: dict[str, list[dict[str, Any]]] = {}
    for uri, edits in (result.get("changes") or {}).items():
        if isinstance(edits, list):
            changes.setdefault(uri, []).extend(
                item for item in edits if isinstance(item, dict)
            )
    unsupported: list[str] = []
    for entry in result.get("documentChanges") or []:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "")
        if kind and kind != "edit":
            unsupported.append(kind)
            continue
        uri = ((entry.get("textDocument") or {}).get("uri")) or ""
        edits = entry.get("edits")
        if uri and isinstance(edits, list):
            changes.setdefault(uri, []).extend(
                item for item in edits if isinstance(item, dict)
            )
    return changes, unsupported


def symbol_line(item: dict[str, Any]) -> int | None:
    """First line of a symbol, in either shape the protocol allows."""
    span = item.get("range") or item.get("selectionRange")
    if not isinstance(span, dict):
        span = (item.get("location") or {}).get("range") if isinstance(item.get("location"), dict) else None
    if not isinstance(span, dict):
        return None
    start = span.get("start") or {}
    return int(start.get("line", 0)) + 1


def flatten_symbols(
    items: Any,
    *,
    max_results: int,
    depth: int = 0,
    collected: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Walk a document-symbol tree into a bounded outline.

    Truncation keeps the **shallowest** symbols: for a large module the useful
    index is its classes and functions, not the first sixty constants in
    document order. The order within a depth is document order, so the answer
    stays readable.

    Returns the outline and how many symbols exist in total, so a truncated
    answer can say what it left out instead of looking complete.
    """
    collected = collected if collected is not None else []
    total = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        total += 1
        collected.append(
            {
                "name": str(item.get("name") or "")[:MAX_NAME_CHARS],
                "kind": SYMBOL_KINDS.get(int(item.get("kind") or 0), f"kind {item.get('kind')}"),
                "line": symbol_line(item),
                "depth": depth,
            }
        )
        _, child_total = flatten_symbols(
            item.get("children"), max_results=max_results, depth=depth + 1, collected=collected
        )
        total += child_total
    if depth == 0:
        collected.sort(key=lambda entry: entry["depth"])
        return collected[:max_results], total
    return collected, total


def hover_text(result: Any) -> str:
    """Flatten an LSP hover payload into one bounded plain-text string."""
    contents = (result or {}).get("contents") if isinstance(result, dict) else result
    if isinstance(contents, dict):
        contents = contents.get("value")
    if isinstance(contents, list):
        parts = []
        for item in contents:
            parts.append(item.get("value") if isinstance(item, dict) else str(item))
        contents = "\n".join(part for part in parts if part)
    text = str(contents or "").strip()
    if len(text) > MAX_TYPE_CHARS:
        text = text[:MAX_TYPE_CHARS] + "… (truncated)"
    return text


class SemanticTool(Tool):
    definition = ToolDefinition(
        name="types",
        description=(
            "Ask a language server what a name really is: real type, definition "
            "site, every reference, a semantic rename, real type errors, and the "
            "outline of a file (cheaper than reading it). Use it before renaming, "
            "refactoring or reasoning about an API instead of guessing from text."
        ),
        methods=[
            "probe",
            "hover",
            "definition",
            "references",
            "rename",
            "diagnostics",
            "symbols",
        ],
        argument_schema={
            "root": {"type": "string", "description": "Project root; defaults to the workspace root."},
            "path": {"type": "string", "description": "File containing the symbol."},
            "symbol": {"type": "string", "description": "Identifier to resolve inside that file."},
            "line": {"type": "integer", "description": "1-based line, when no symbol is given."},
            "character": {"type": "integer", "description": "1-based column, when no symbol is given."},
            "max_results": {"type": "integer", "description": f"1-{MAX_MAX_RESULTS} results (default {DEFAULT_MAX_RESULTS})."},
            "new_name": {"type": "string", "description": "New identifier for types.rename."},
            "apply": {"type": "boolean", "description": "types.rename: false returns the edit plan, true writes it."},
        },
        method_argument_schema={
            "probe": {"root": {"type": "string"}},
            "hover": {
                "root": {"type": "string"},
                "path": {"type": "string", "required": True},
                "symbol": {"type": "string"},
                "line": {"type": "integer"},
                "character": {"type": "integer"},
            },
            "definition": {
                "root": {"type": "string"},
                "path": {"type": "string", "required": True},
                "symbol": {"type": "string"},
                "line": {"type": "integer"},
                "character": {"type": "integer"},
            },
            "references": {
                "root": {"type": "string"},
                "path": {"type": "string", "required": True},
                "symbol": {"type": "string"},
                "line": {"type": "integer"},
                "character": {"type": "integer"},
                "max_results": {"type": "integer"},
            },
            "rename": {
                "root": {"type": "string"},
                "path": {"type": "string", "required": True},
                "symbol": {"type": "string"},
                "line": {"type": "integer"},
                "character": {"type": "integer"},
                "new_name": {"type": "string", "required": True},
                "apply": {"type": "boolean"},
            },
            "diagnostics": {
                "root": {"type": "string"},
                "path": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "symbols": {
                "root": {"type": "string"},
                "path": {"type": "string", "required": True},
                "max_results": {"type": "integer"},
            },
        },
        permissions=["filesystem.read", "project.analysis"],
        idempotent=True,
    )

    def __init__(self, workspace_root: str = ".", langserver_path: str = "") -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.langserver_path = str(langserver_path or "")

    def _project_root(self, args: dict[str, Any]) -> Path:
        """Server root for this call: an explicit one, else the workspace.

        Tasks work on registered projects that may live outside the workspace,
        and a language server rooted in the wrong tree resolves imports badly.
        """
        raw = str(args.get("root") or "").strip()
        if not raw:
            return self.workspace_root
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        candidate = candidate.resolve()
        if not candidate.is_dir():
            raise ValueError(f"project root is not a directory: {candidate}")
        return candidate

    def _resolve_target(self, args: dict[str, Any], root: Path) -> tuple[Path, int, int, int]:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("types requires a path")
        path = Path(raw_path)
        if not path.is_absolute():
            path = root / path
        path = path.expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"file not found: {path}")
        symbol = str(args.get("symbol") or "").strip()
        if symbol:
            if not _IDENTIFIER.match(symbol) or len(symbol) > MAX_SYMBOL_CHARS:
                raise ValueError("symbol must be a single identifier")
            text = path.read_text(encoding="utf-8", errors="replace")
            line, character, matches = locate_symbol(text, symbol)
            return path, line, character, matches
        line = int(args.get("line") or 0)
        character = int(args.get("character") or 1)
        if line <= 0:
            raise ValueError("types requires either a symbol or a 1-based line")
        return path, line - 1, max(0, character - 1), 1

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        if method not in self.definition.methods:
            return OperationResult(
                success=False,
                error=f"types does not implement {method}",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        if method == "probe":
            return self._probe()
        if method == "diagnostics":
            return await self._diagnostics(args, timeout)
        if method == "symbols":
            return await self._symbols(args, timeout)
        try:
            root = self._project_root(args)
            path, line, character, matches = self._resolve_target(args, root)
        except (ValueError, OSError) as error:
            return OperationResult(
                success=False, error=str(error), error_type=ErrorType.INVALID_ARGUMENT
            )
        try:
            server = await lsp.shared_server(
                root,
                explicit=self.langserver_path,
                timeout=max(5.0, float(timeout)),
            )
            uri = await server.open_document(path)
            position = {"line": line, "character": character}
            if method == "hover":
                result = await server.request(
                    "textDocument/hover",
                    {"textDocument": {"uri": uri}, "position": position},
                )
                text = hover_text(result)
                return OperationResult(
                    success=True,
                    output={
                        "file": relative(root, str(path)),
                        "line": line + 1,
                        "symbol_matches": matches,
                        "type": text or "the server returned no type for that position",
                        "analyzer": lsp.analyzer_version(lsp.find_langserver(self.langserver_path)),
                    },
                )
            if method == "definition":
                result = await server.request(
                    "textDocument/definition",
                    {"textDocument": {"uri": uri}, "position": position},
                )
                items = result if isinstance(result, list) else [result] if result else []
                locations = [
                    line_info
                    for line_info in (_result_line(root, item) for item in items)
                    if line_info
                ]
                return OperationResult(
                    success=True,
                    output={
                        "symbol": args.get("symbol"),
                        "definitions": locations[:DEFAULT_MAX_RESULTS],
                        "count": len(locations),
                    },
                )
            if method == "rename":
                return await self._rename(root, uri, position, args)
            max_results = min(
                max(int(args.get("max_results", DEFAULT_MAX_RESULTS)), 1), MAX_MAX_RESULTS
            )
            result = await server.request(
                "textDocument/references",
                {
                    "textDocument": {"uri": uri},
                    "position": position,
                    "context": {"includeDeclaration": True},
                },
            )
            items = result if isinstance(result, list) else []
            locations = [
                line_info
                for line_info in (_result_line(root, item) for item in items)
                if line_info
            ]
            output: dict[str, Any] = {
                "symbol": args.get("symbol"),
                "count": len(locations),
                "references": locations[:max_results],
            }
            if len(locations) > max_results:
                output["truncated"] = True
                output["note"] = (
                    f"{len(locations)} references exist; showing {max_results}. "
                    "Narrow the symbol or raise max_results."
                )
            return OperationResult(success=True, output=output)
        except lsp.LanguageServerUnavailable as error:
            return OperationResult(
                success=False,
                error=f"language server unavailable: {error}",
                error_type=ErrorType.TOOL_FAILURE,
                retryable=False,
            )
        except (TimeoutError, EOFError, ValueError, OSError) as error:
            return OperationResult(
                success=False,
                error=f"semantic query failed: {error}",
                error_type=ErrorType.UNKNOWN,
            )

    def _probe(self) -> OperationResult:
        status, detail = lsp.langserver_availability(self.langserver_path)
        node = lsp.node_executable()
        entry = lsp.find_langserver(self.langserver_path)
        cli = lsp.find_pyright_cli()
        return OperationResult(
            success=True,
            output={
                "available": status == "ready",
                "status": status,
                "node": node,
                "analyzer": lsp.analyzer_version(entry),
                "langserver": str(entry) if entry else None,
                "type_checker": str(cli) if cli else None,
                "reason": None if status == "ready" else detail,
                "workspace_root": str(self.workspace_root),
                "methods": ["hover", "definition", "references", "rename", "diagnostics"],
            },
        )

    async def _symbols(self, args: dict[str, Any], timeout: float) -> OperationResult:
        """The outline of a file: what is in it, without reading it.

        An outline is cheap and bounded (a few hundred tokens even for a large
        file), while reading the file to find out what it contains is not. It is
        the first rung of the retrieval ladder: outline, then symbol, then the
        lines that matter.
        """
        try:
            root = self._project_root(args)
            raw_path = str(args.get("path") or "").strip()
            if not raw_path:
                raise ValueError("types.symbols requires a path")
            path = Path(raw_path)
            if not path.is_absolute():
                path = root / path
            path = path.expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"file not found: {path}")
        except (ValueError, OSError) as error:
            return OperationResult(
                success=False, error=str(error), error_type=ErrorType.INVALID_ARGUMENT
            )
        max_results = min(
            max(int(args.get("max_results", 60)), 1), MAX_MAX_RESULTS * 3
        )
        try:
            server = await lsp.shared_server(
                root, explicit=self.langserver_path, timeout=max(5.0, float(timeout))
            )
            uri = await server.open_document(path)
            result = await server.request(
                "textDocument/documentSymbol", {"textDocument": {"uri": uri}}
            )
        except lsp.LanguageServerUnavailable as error:
            return OperationResult(
                success=False,
                error=f"language server unavailable: {error}",
                error_type=ErrorType.TOOL_FAILURE,
                retryable=False,
            )
        except (TimeoutError, EOFError, ValueError, OSError) as error:
            return OperationResult(
                success=False,
                error=f"semantic query failed: {error}",
                error_type=ErrorType.UNKNOWN,
            )
        outline, total = flatten_symbols(result, max_results=max_results)
        output: dict[str, Any] = {
            "file": relative(root, str(path)),
            "symbols": outline,
            "count": total,
        }
        if total > len(outline):
            output["truncated"] = True
            output["note"] = (
                f"{total} symbols exist; showing {len(outline)}. Use hover or "
                "definition for the ones you need."
            )
        return OperationResult(success=True, output=output)

    async def _diagnostics(self, args: dict[str, Any], timeout: float) -> OperationResult:
        """Type errors as evidence, from the same engine the editor uses.

        The batch checker is a separate process, so its report is the whole
        answer. It is its own method because a full type check costs seconds, and
        the caller decides when that is worth paying.
        """
        try:
            root = self._project_root(args)
        except (ValueError, OSError) as error:
            return OperationResult(
                success=False, error=str(error), error_type=ErrorType.INVALID_ARGUMENT
            )
        node = lsp.node_executable()
        cli = lsp.find_pyright_cli()
        if node is None or cli is None:
            return OperationResult(
                success=False,
                error=(
                    "type checker unavailable: "
                    + ("node is not installed" if node is None else f"set {lsp.CLI_ENV} to pyright's index.js")
                ),
                error_type=ErrorType.TOOL_FAILURE,
                retryable=False,
            )
        raw_path = str(args.get("path") or "").strip()
        paths: list[str] = []
        if raw_path:
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = root / candidate
            paths.append(str(candidate))
        started = monotonic()
        try:
            report = await lsp.type_check(
                node, cli, root, paths=paths, timeout=max(30.0, float(timeout))
            )
        except (lsp.LanguageServerUnavailable, ValueError, OSError) as error:
            return OperationResult(
                success=False,
                error=f"type check failed: {error}",
                error_type=ErrorType.TOOL_FAILURE,
            )
        max_results = min(
            max(int(args.get("max_results", DEFAULT_MAX_RESULTS)), 1), MAX_MAX_RESULTS
        )
        raw_items = report.get("generalDiagnostics") or []
        diagnostics = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            start = ((item.get("range") or {}).get("start") or {})
            message = str(item.get("message") or "").splitlines()[0]
            # The batch report labels severity in words; LSP uses 1-4.
            raw_severity = item.get("severity")
            if isinstance(raw_severity, str):
                severity = raw_severity.strip().casefold() or "info"
            else:
                severity = SEVERITY_LABELS.get(int(raw_severity or 0), "info")
            diagnostics.append(
                {
                    "severity": severity,
                    "file": relative(root, str(item.get("file") or "")),
                    "line": int(start.get("line", 0)) + 1,
                    "column": int(start.get("character", 0)) + 1,
                    "message": message[:MAX_MESSAGE_CHARS],
                }
            )
        summary = report.get("summary") or {}
        output: dict[str, Any] = {
            "scope": relative(root, paths[0]) if paths else "project",
            "files_analyzed": summary.get("filesAnalyzed"),
            "errors": summary.get("errorCount", 0),
            "warnings": summary.get("warningCount", 0),
            "diagnostics": diagnostics[:max_results],
            "duration_seconds": round(monotonic() - started, 2),
        }
        if len(diagnostics) > max_results:
            output["truncated"] = True
            output["note"] = f"{len(diagnostics)} findings; showing {max_results}."
        return OperationResult(success=True, output=output)

    async def _rename(
        self,
        root: Path,
        uri: str,
        position: dict[str, int],
        args: dict[str, Any],
    ) -> OperationResult:
        """Semantic rename: every reference the language server knows about.

        A text search cannot prove a rename is complete; this can, because the
        server resolves the symbol. Applying is opt-in and refused when the edit
        set was truncated, since half a rename is worse than none.
        """
        new_name = str(args.get("new_name") or "").strip()
        if not _IDENTIFIER.match(new_name) or len(new_name) > MAX_SYMBOL_CHARS:
            return OperationResult(
                success=False,
                error="new_name must be a single identifier",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        server = await lsp.shared_server(
            root, explicit=self.langserver_path, timeout=float(args.get("_timeout") or 60.0)
        )
        result = await server.request(
            "textDocument/rename",
            {"textDocument": {"uri": uri}, "position": position, "newName": new_name},
        )
        changes, unsupported = normalize_workspace_edit(result)
        if unsupported:
            return OperationResult(
                success=False,
                error=(
                    "the rename needs operations this tool does not apply "
                    f"({', '.join(sorted(set(unsupported)))}); it was not applied"
                ),
                error_type=ErrorType.TOOL_FAILURE,
                retryable=False,
            )
        if not changes:
            return OperationResult(
                success=False,
                error="the language server produced no rename plan for that symbol",
                error_type=ErrorType.TOOL_FAILURE,
            )
        max_results = min(
            max(int(args.get("max_results", DEFAULT_MAX_RESULTS)), 1), MAX_MAX_RESULTS
        )
        files: list[dict[str, Any]] = []
        total_edits = 0
        for file_uri, edits in changes.items():
            entries = [item for item in (edits or []) if isinstance(item, dict)]
            total_edits += len(entries)
            files.append(
                {
                    "file": relative(root, lsp.uri_to_path(file_uri)),
                    "edits": len(entries),
                    "lines": [
                        int(((item.get("range") or {}).get("start") or {}).get("line", 0)) + 1
                        for item in entries[:max_results]
                    ],
                }
            )
        output: dict[str, Any] = {
            "symbol": args.get("symbol"),
            "new_name": new_name,
            "files": files,
            "edits": total_edits,
            "applied": False,
        }
        apply_changes = bool(args.get("apply"))
        if total_edits > MAX_MAX_RESULTS or len(files) > MAX_MAX_RESULTS:
            output["truncated"] = True
            output["note"] = (
                "the edit set is larger than the bounded report; not applying it. "
                "Narrow the symbol or raise max_results."
            )
        elif apply_changes:
            written: list[str] = []
            try:
                for file_uri, edits in changes.items():
                    target = Path(lsp.uri_to_path(file_uri))
                    text = target.read_text(encoding="utf-8")
                    for item in sorted(
                        (entry for entry in (edits or []) if isinstance(entry, dict)),
                        key=lambda entry: (
                            -((entry.get("range") or {}).get("start") or {}).get("line", 0),
                            -((entry.get("range") or {}).get("start") or {}).get("character", 0),
                        ),
                    ):
                        span = item.get("range") or {}
                        start, end = span.get("start") or {}, span.get("end") or {}
                        lines = text.splitlines(keepends=True)
                        prefix = "".join(lines[: int(start.get("line", 0))])
                        start_offset = len(prefix) + int(start.get("character", 0))
                        end_prefix = "".join(lines[: int(end.get("line", 0))])
                        end_offset = len(end_prefix) + int(end.get("character", 0))
                        text = (
                            text[:start_offset]
                            + str(item.get("newText") or "")
                            + text[end_offset:]
                        )
                    target.write_text(text, encoding="utf-8")
                    written.append(relative(root, str(target)))
                output["applied"] = True
                output["written"] = written
            except (OSError, ValueError) as error:
                output["apply_error"] = str(error)
        return OperationResult(success=True, output=output)


__all__ = ["SemanticTool", "hover_text", "locate_symbol", "relative"]
