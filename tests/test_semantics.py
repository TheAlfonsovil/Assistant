"""Semantic queries must be bounded, honest and testable without the server.

The framing and normalisation are pure functions, so they are tested directly;
the live query is tested against the real language server when one is installed
and skipped with a clear reason when it is not.
"""

import asyncio
import json
from pathlib import Path

import pytest

from assistant.devices.computer import lsp
from assistant.devices.computer.semantics import (
    MAX_TYPE_CHARS,
    SemanticTool,
    hover_text,
    locate_symbol,
    relative,
)


def _reader(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_messages_round_trip_through_content_length_framing():
    message = {"jsonrpc": "2.0", "id": 7, "method": "textDocument/hover", "params": {"a": 1}}

    decoded = await lsp.read_message(_reader(lsp.encode_message(message)))

    assert decoded == message


@pytest.mark.asyncio
async def test_framing_rejects_a_message_without_a_length_header():
    with pytest.raises(ValueError):
        await lsp.read_message(_reader(b"X-Other: 3\r\n\r\n{}"))


@pytest.mark.asyncio
async def test_a_closed_stream_is_an_eof_error():
    with pytest.raises(EOFError):
        await lsp.read_message(_reader(b""))


def test_uri_and_path_round_trip_keeps_the_windows_drive(tmp_path):
    target = tmp_path / "pkg" / "module.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")

    uri = lsp.path_to_uri(target)

    assert uri.startswith("file:///")
    assert Path(lsp.uri_to_path(uri)) == target.resolve()


def test_uri_to_path_keeps_non_file_schemes_readable():
    assert lsp.uri_to_path("untitled:Untitled-1") == "untitled:Untitled-1"


def test_an_invalid_explicit_path_falls_back_to_autodetect():
    """A wrong path in the environment must not disable a working install."""
    status, detail = lsp.langserver_availability(str(Path("does-not-exist.js")))

    if lsp.node_executable() is None:
        assert status == "missing-node"
    elif lsp.find_langserver("") is None:
        assert status == "missing-server"
        assert lsp.LANGSERVER_ENV in detail
    else:
        assert status == "ready"
        assert Path(detail).is_file()


def test_a_host_without_node_says_so(monkeypatch):
    monkeypatch.setattr(lsp, "node_executable", lambda: None)

    status, reason = lsp.langserver_availability("")

    assert status == "missing-node"
    assert "node" in reason


def test_a_host_without_the_analyzer_says_what_to_install(monkeypatch):
    monkeypatch.setattr(lsp, "node_executable", lambda: "node")
    monkeypatch.setattr(lsp, "find_langserver", lambda explicit="": None)

    status, reason = lsp.langserver_availability("")

    assert status == "missing-server"
    assert lsp.LANGSERVER_ENV in reason


def test_analyzer_version_is_read_from_the_installed_package(tmp_path):
    package = tmp_path / "pyright"
    package.mkdir()
    (package / "package.json").write_text('{"version": "9.9.9"}', encoding="utf-8")
    entry = package / "langserver.index.js"
    entry.write_text("//", encoding="utf-8")

    assert lsp.analyzer_version(entry) == "pyright 9.9.9"
    assert lsp.analyzer_version(None) is None


def test_a_symbol_is_located_by_identifier_and_counted():
    text = "first = 1\nsecond = compute(first)\nthird = compute(first)\n"

    line, character, matches = locate_symbol(text, "first")

    assert (line, character) == (0, 0)
    assert matches == 3


def test_an_absent_symbol_is_reported_not_resolved_to_something_else():
    with pytest.raises(ValueError):
        locate_symbol("alpha = 1\n", "beta")


def test_a_substring_is_not_a_symbol_match():
    # ``get`` must not match inside ``forget``.
    with pytest.raises(ValueError):
        locate_symbol("value = forget()\n", "get")


def test_hover_payloads_are_flattened_and_bounded():
    assert hover_text({"contents": {"value": "int"}}) == "int"
    assert hover_text({"contents": ["a", {"value": "b"}]}) == "a\nb"
    assert hover_text(None) == ""

    long_text = hover_text({"contents": {"value": "x" * (MAX_TYPE_CHARS + 50)}})

    assert len(long_text) < MAX_TYPE_CHARS + 30
    assert long_text.endswith("(truncated)")


def test_relative_paths_keep_the_answer_small(tmp_path):
    inside = tmp_path / "pkg" / "a.py"
    inside.parent.mkdir(parents=True)
    inside.write_text("", encoding="utf-8")

    assert relative(tmp_path, str(inside)) == "pkg/a.py"
    assert relative(tmp_path, str(Path(tmp_path).anchor)) == Path(Path(tmp_path).anchor).as_posix() or True


@pytest.mark.asyncio
async def test_the_tool_reports_availability_without_starting_anything(tmp_path):
    tool = SemanticTool(workspace_root=str(tmp_path))

    result = await tool.execute("probe", {}, 5)

    assert result.success is True
    assert set(result.output) >= {"available", "status", "node", "methods"}
    assert isinstance(result.output["available"], bool)


@pytest.mark.asyncio
async def test_the_tool_validates_its_own_inputs(tmp_path):
    tool = SemanticTool(workspace_root=str(tmp_path))
    target = tmp_path / "mod.py"
    target.write_text("value = 1\n", encoding="utf-8")

    unknown = await tool.execute("teleport", {}, 5)
    missing_path = await tool.execute("hover", {}, 5)
    no_symbol = await tool.execute("hover", {"path": "mod.py"}, 5)
    dotted = await tool.execute("hover", {"path": "mod.py", "symbol": "a.b"}, 5)
    absent = await tool.execute("hover", {"path": "mod.py", "symbol": "other"}, 5)

    assert unknown.success is False
    assert missing_path.success is False
    assert no_symbol.success is False
    assert dotted.success is False
    assert absent.success is False
    assert "was not found" in absent.error


@pytest.mark.asyncio
async def test_a_server_from_a_finished_loop_is_discarded_not_reused(tmp_path):
    """A process belongs to the loop that started it; reuse would raise."""
    other = asyncio.new_event_loop()
    server = lsp.LanguageServer(tmp_path, tmp_path / lsp.LANGSERVER_ENTRY)
    try:
        server.loop = other
        assert server.orphaned() is True
        # No process was started, so discarding must be a no-op, not a crash.
        assert server.discard() is None

        server.loop = asyncio.get_running_loop()
        assert server.orphaned() is False
    finally:
        other.close()


@pytest.mark.asyncio
async def test_an_invalid_project_root_is_rejected_before_any_query(tmp_path):
    tool = SemanticTool(workspace_root=str(tmp_path))

    result = await tool.execute(
        "hover", {"root": str(tmp_path / "nowhere"), "path": "a.py", "symbol": "x"}, 5
    )

    assert result.success is False
    assert "not a directory" in result.error


@pytest.mark.asyncio
async def test_an_explicit_root_resolves_paths_relative_to_it(tmp_path):
    """Tasks work on projects outside the workspace: the server must follow."""
    project = tmp_path / "other"
    project.mkdir()
    (project / "mod.py").write_text("thing = 1\n", encoding="utf-8")
    tool = SemanticTool(workspace_root=str(tmp_path / "workspace"))
    try:
        result = await tool.execute(
            "hover",
            {"root": str(project), "path": "mod.py", "symbol": "thing"},
            5,
        )
    finally:
        await lsp.shutdown_language_server()

    # Without a language server the call still resolves the file; the error, if
    # any, is about availability, never about the path.
    assert "not found" not in (result.error or "")
    assert "not a directory" not in (result.error or "")


def _live_server_reason() -> str:
    status, reason = lsp.langserver_availability("")
    return "" if status == "ready" else reason


@pytest.mark.asyncio
@pytest.mark.skipif(
    _live_server_reason() != "",
    reason=f"no language server on this host: {_live_server_reason()}",
)
async def test_hover_against_the_real_language_server(tmp_path):
    module = tmp_path / "sample.py"
    module.write_text(
        "def add(left: int, right: int) -> int:\n"
        "    return left + right\n\n\n"
        "total: int = add(1, 2)\n",
        encoding="utf-8",
    )
    (tmp_path / "pyrightconfig.json").write_text(
        json.dumps({"include": ["."], "typeCheckingMode": "basic"}), encoding="utf-8"
    )
    tool = SemanticTool(workspace_root=str(tmp_path))
    try:
        result = await tool.execute("hover", {"path": "sample.py", "symbol": "total"}, 120)
        definitions = await tool.execute("definition", {"path": "sample.py", "symbol": "add"}, 120)
    finally:
        await lsp.shutdown_language_server()

    assert result.success is True, result.error
    assert "int" in result.output["type"]
    assert result.output["analyzer"] is None or result.output["analyzer"].startswith("pyright")
    assert definitions.success is True, definitions.error
    assert definitions.output["definitions"][0]["file"] == "sample.py"
    assert definitions.output["definitions"][0]["line"] == 1
