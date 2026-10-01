"""Semantic queries and the debugger must be bounded, honest and testable.

Framing and normalisation are pure functions and are tested directly. The live
tests run against pyright and debugpy when they are installed and are skipped
with a clear reason when they are not, so the suite never depends on a toolchain
it has not checked for.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from assistant.devices.computer import dap, lsp
from assistant.devices.computer.debugger import DebugTool
from assistant.devices.computer.semantics import (
    SemanticTool,
    normalize_workspace_edit,
)

PROGRAM = (
    "def double(value):\n"
    "    result = value * 2\n"
    "    return result\n"
    "\n"
    "\n"
    "total = 0\n"
    "for index in range(3):\n"
    "    total = double(index + 1)\n"
    "\n"
    'print("total", total)\n'
)

TYPED = (
    "def add(left: int, right: int) -> int:\n"
    "    return left + right\n"
    "\n"
    "\n"
    "text: str = add(1, 2)\n"
)


def _reader(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_debug_messages_round_trip_through_content_length_framing():
    message = {"seq": 3, "type": "request", "command": "stackTrace", "arguments": {"threadId": 1}}

    decoded = await dap.read_message(_reader(dap.encode_message(message)))

    assert decoded == message


@pytest.mark.asyncio
async def test_debug_framing_rejects_a_message_without_a_length_header():
    with pytest.raises(ValueError):
        await dap.read_message(_reader(b"Seq: 1\r\n\r\n{}"))


def test_debug_availability_is_reported_not_assumed(monkeypatch):
    monkeypatch.setattr(dap, "debugpy_path", lambda: None)
    status, reason = dap.availability()

    assert status == "missing-debugpy"
    assert "debugpy" in reason


@pytest.mark.asyncio
async def test_the_debug_tool_reports_availability_without_starting_anything(tmp_path):
    tool = DebugTool(workspace_root=str(tmp_path))

    result = await tool.execute("probe", {}, 5)

    assert result.success is True
    assert set(result.output) >= {"available", "status", "interpreter", "methods"}


@pytest.mark.asyncio
async def test_the_debug_tool_validates_its_inputs(tmp_path):
    tool = DebugTool(workspace_root=str(tmp_path))
    (tmp_path / "ok.py").write_text("value = 1\n", encoding="utf-8")

    unknown = await tool.execute("step", {}, 5)
    no_program = await tool.execute("trace", {}, 5)
    missing = await tool.execute("trace", {"program": "absent.py", "breakpoints": [1]}, 5)
    no_breakpoints = await tool.execute("trace", {"program": "ok.py"}, 5)
    bad_root = await tool.execute(
        "trace", {"root": str(tmp_path / "nowhere"), "program": "ok.py", "breakpoints": [1]}, 5
    )

    assert unknown.success is False
    assert no_program.success is False
    assert "not found" in missing.error
    assert "at least one breakpoint" in no_breakpoints.error
    assert "not a directory" in bad_root.error


def test_breakpoints_accept_lines_and_objects(tmp_path):
    program = tmp_path / "p.py"

    parsed = DebugTool._breakpoints([4, {"line": 9, "condition": "index > 5"}], program)

    assert parsed[0] == {"file": str(program), "line": 4}
    assert parsed[1]["line"] == 9
    assert parsed[1]["condition"] == "index > 5"


def test_workspace_edits_are_normalised_from_both_shapes():
    changes, unsupported = normalize_workspace_edit(
        {
            "changes": {"file:///a.py": [{"range": {}, "newText": "x"}]},
            "documentChanges": [
                {"textDocument": {"uri": "file:///b.py"}, "edits": [{"range": {}, "newText": "y"}]},
                {"kind": "create", "uri": "file:///c.py"},
            ],
        }
    )

    assert sorted(changes) == ["file:///a.py", "file:///b.py"]
    assert len(changes["file:///b.py"]) == 1
    # A file operation cannot be applied as a text edit: it is reported.
    assert unsupported == ["create"]


def test_normalising_a_missing_edit_yields_nothing():
    assert normalize_workspace_edit(None) == ({}, [])


def _semantic_reason() -> str:
    status, reason = lsp.langserver_availability("")
    return "" if status == "ready" else reason


def _debug_reason() -> str:
    status, reason = dap.availability()
    return "" if status == "ready" else reason


@pytest.mark.asyncio
@pytest.mark.skipif(
    _semantic_reason() != "", reason=f"no language server on this host: {_semantic_reason()}"
)
async def test_type_checking_and_renaming_against_the_real_analyzer(tmp_path):
    (tmp_path / "pyrightconfig.json").write_text('{"include": ["."]}', encoding="utf-8")
    (tmp_path / "mod.py").write_text(TYPED, encoding="utf-8")
    tool = SemanticTool(workspace_root=str(tmp_path))
    try:
        diagnostics = await tool.execute("diagnostics", {"path": "mod.py"}, 240)
        plan = await tool.execute(
            "rename", {"path": "mod.py", "symbol": "add", "new_name": "sum_two"}, 120
        )
        applied = await tool.execute(
            "rename", {"path": "mod.py", "symbol": "add", "new_name": "sum_two", "apply": True}, 120
        )
    finally:
        await lsp.shutdown_language_server()

    assert diagnostics.success is True, diagnostics.error
    assert diagnostics.output["errors"] >= 1
    finding = diagnostics.output["diagnostics"][0]
    assert finding["severity"] == "error"
    assert finding["line"] == 5
    assert "str" in finding["message"]

    # The plan is reported before anything is written.
    assert plan.success is True, plan.error
    assert plan.output["applied"] is False
    assert plan.output["edits"] >= 2

    assert applied.success is True, applied.error
    assert applied.output["applied"] is True
    text = (tmp_path / "mod.py").read_text(encoding="utf-8")
    assert "def sum_two(" in text
    assert "sum_two(1, 2)" in text
    assert "def add(" not in text


@pytest.mark.asyncio
@pytest.mark.skipif(
    _debug_reason() != "", reason=f"no debug adapter on this host: {_debug_reason()}"
)
async def test_a_debug_trace_reads_real_values_per_iteration(tmp_path):
    (tmp_path / "program.py").write_text(PROGRAM, encoding="utf-8")
    tool = DebugTool(workspace_root=str(tmp_path))

    result = await tool.execute(
        "trace", {"program": "program.py", "breakpoints": [3], "max_stops": 3}, 180
    )

    assert result.success is True, result.error
    stops = result.output["stops"]
    assert len(stops) >= 2, result.output
    first, second = stops[0], stops[1]
    assert first["function"] == "double"
    assert first["file"] == "program.py"
    assert first["locals"]["value"] == "1"
    # The second iteration proves the values are read per stop, not once.
    assert second["locals"]["value"] == "2"
    assert second["locals"]["total"] == "2"
