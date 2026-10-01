"""Runtime inspection: run a program under a debugger and read real values.

The codegraph says where things are, the language server says what they are, and
this says **what the value actually was when it went wrong**. Together they are
the three questions an agent cannot answer by reading text alone.

What it does: runs a program with breakpoints, and for each stop reports the
frame and its locals, then continues. What it does not do: stay open for
interactive stepping — an agent that cannot see the screen would only be guessing
where to step next, and a per-call session cannot be parked between turns.
"""

from __future__ import annotations

import sys
from pathlib import Path
from time import monotonic
from typing import Any

from assistant.domain.models import ErrorType, OperationResult
from assistant.tools import Tool, ToolDefinition

from . import dap

DEFAULT_MAX_STOPS = 5
MAX_MAX_STOPS = 20
DEFAULT_LOCALS = 12
MAX_LOCALS = 50
MAX_VALUE_CHARS = 200


def _value_text(variable: dict[str, Any]) -> str:
    text = str(variable.get("value") or "")
    if len(text) > MAX_VALUE_CHARS:
        text = text[:MAX_VALUE_CHARS] + "…"
    return text


# debugpy lists these as variables to group the real ones. They carry no value,
# so they are noise in evidence.
_SCOPE_MARKERS = ("special variables", "function variables", "class variables")


def _is_scope_marker(variable: dict[str, Any]) -> bool:
    return str(variable.get("name") or "").strip().casefold() in _SCOPE_MARKERS


class DebugTool(Tool):
    definition = ToolDefinition(
        name="debug",
        description=(
            "Run a Python program under a debugger and read real runtime state: "
            "each breakpoint stop with its frame and local values, plus the "
            "program's own output. Use it instead of adding print statements."
        ),
        methods=["probe", "trace"],
        argument_schema={
            "root": {"type": "string", "description": "Project root; defaults to the workspace root."},
            "program": {"type": "string", "description": "Script to run, relative to the root."},
            "breakpoints": {
                "type": "array",
                "description": "Breakpoints: line numbers, or {file, line, condition} objects.",
            },
            "args": {"type": "array", "description": "Command-line arguments for the program."},
            "max_stops": {"type": "integer", "description": f"1-{MAX_MAX_STOPS} stops (default {DEFAULT_MAX_STOPS})."},
            "locals_limit": {"type": "integer", "description": f"1-{MAX_LOCALS} variables per stop (default {DEFAULT_LOCALS})."},
        },
        method_argument_schema={
            "probe": {},
            "trace": {
                "root": {"type": "string"},
                "program": {"type": "string", "required": True},
                "breakpoints": {"type": "array"},
                "args": {"type": "array"},
                "max_stops": {"type": "integer"},
                "locals_limit": {"type": "integer"},
            },
        },
        permissions=["filesystem.read", "shell.execute"],
        idempotent=False,
    )

    def __init__(self, workspace_root: str = ".") -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()

    def _root(self, args: dict[str, Any]) -> Path:
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

    @staticmethod
    def _breakpoints(raw: Any, program: Path) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for item in raw or []:
            if isinstance(item, int):
                entries.append({"file": str(program), "line": item})
            elif isinstance(item, dict) and item.get("line"):
                entries.append(
                    {
                        "file": str(item.get("file") or program),
                        "line": int(item["line"]),
                        "condition": item.get("condition"),
                    }
                )
        if not entries:
            raise ValueError("debug.trace requires at least one breakpoint")
        return entries

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        if method not in self.definition.methods:
            return OperationResult(
                success=False,
                error=f"debug does not implement {method}",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        if method == "probe":
            return self._probe()
        try:
            root = self._root(args)
            raw_program = str(args.get("program") or "").strip()
            if not raw_program:
                raise ValueError("debug.trace requires a program")
            program = Path(raw_program)
            if not program.is_absolute():
                program = root / program
            program = program.resolve()
            if not program.is_file():
                raise ValueError(f"program not found: {program}")
            breakpoints = self._breakpoints(args.get("breakpoints"), program)
        except (ValueError, OSError) as error:
            return OperationResult(
                success=False, error=str(error), error_type=ErrorType.INVALID_ARGUMENT
            )
        max_stops = min(max(int(args.get("max_stops", DEFAULT_MAX_STOPS)), 1), MAX_MAX_STOPS)
        locals_limit = min(max(int(args.get("locals_limit", DEFAULT_LOCALS)), 1), MAX_LOCALS)
        program_args = [str(item) for item in (args.get("args") or [])]
        started = monotonic()
        # The adapter runs the program with the same interpreter that has
        # debugpy, and arguments travel through debugpy's own ``args`` field.
        session = dap.DebugSession(program, cwd=root, timeout=max(30.0, float(timeout)))
        stops: list[dict[str, Any]] = []
        note: str | None = None
        try:
            await session.start()
            await session.launch(breakpoints)
            if program_args:
                session.output.append(f"[debug] program args: {' '.join(program_args)}\n")
            while len(stops) < max_stops:
                try:
                    stop = await session.wait_for_stop(timeout=max(15.0, float(timeout)))
                except TimeoutError:
                    note = "the program did not reach another breakpoint in time"
                    break
                thread_id = int(stop.get("threadId") or 1)
                frames = await session.stack(thread_id, levels=3)
                if not frames:
                    note = "the debuggee stopped without a readable frame"
                    break
                frame = frames[0]
                variables = await session.variables(int(frame["id"]), limit=locals_limit)
                stops.append(
                    {
                        "reason": stop.get("reason"),
                        "function": frame.get("name"),
                        "file": self._relative(root, frame.get("source", {}).get("path")),
                        "line": frame.get("line"),
                        "locals": {
                            item["name"]: _value_text(item)
                            for item in variables
                            if not _is_scope_marker(item)
                        },
                    }
                )
                await session.resume(thread_id)
            if not stops and note is None:
                note = "the program finished without stopping at any breakpoint"
        except dap.DebugUnavailable as error:
            return OperationResult(
                success=False,
                error=f"debugger unavailable: {error}",
                error_type=ErrorType.TOOL_FAILURE,
                retryable=False,
            )
        except (TimeoutError, ValueError, EOFError, OSError) as error:
            note = f"debug session stopped early: {error}"
        finally:
            await session.close()
        output: dict[str, Any] = {
            "program": self._relative(root, str(program)),
            "exit_code": session.exit_code,
            "stops": stops,
            "output_tail": session.output_text(),
            "duration_seconds": round(monotonic() - started, 2),
        }
        if note:
            output["note"] = note
        if len(stops) >= max_stops and not session.finished:
            output["truncated"] = True
            output["note"] = note or f"stopped after {max_stops} breakpoint hits"
        return OperationResult(success=True, output=output)

    def _probe(self) -> OperationResult:
        status, detail = dap.availability()
        return OperationResult(
            success=True,
            output={
                "available": status == "ready",
                "status": status,
                "interpreter": dap.debugpy_path(),
                "python": sys.version.split()[0],
                "reason": None if status == "ready" else detail,
                "workspace_root": str(self.workspace_root),
                "methods": ["trace"],
            },
        )

    def _relative(self, root: Path, path: Any) -> str | None:
        if not path:
            return None
        try:
            return Path(str(path)).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            return str(path)


__all__ = ["DEFAULT_MAX_STOPS", "DebugTool"]
