"""Minimal DAP client: run a program under a debugger and read real state.

A debugger answers the question no amount of re-running answers: *what was the
value at that moment*. Print statements inside a hot path to find out is slow,
mutates the code under test, and is exactly what an agent should not have to do.

The client speaks Debug Adapter Protocol over stdio (the same Content-Length
framed JSON-RPC as the language server) to ``debugpy``, because that adapter
ships with Python and needs no editor. Three protocol details are load-bearing
and were verified against the real adapter:

* the ``launch`` response arrives **after** ``configurationDone``, so the order
  is launch → wait for ``initialized`` → setBreakpoints → configurationDone;
* the programme's own output arrives as ``output`` events, not on the response;
* the debuggee keeps running while events arrive, so the reader must keep
  draining the pipe or the adapter blocks.

Sessions are per call and are torn down at the end: a debugger process tied to a
finished event loop cannot be awaited afterwards, and caching it would only move
that failure to a later call.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ADAPTER_MODULE = "debugpy.adapter"
DEFAULT_TIMEOUT = 120.0
MAX_OUTPUT_CHARS = 4000


class DebugUnavailable(RuntimeError):
    """Raised when no debug adapter can be started on this host."""


def debugpy_path() -> str | None:
    """Path to the interpreter that has debugpy installed, or ``None``."""
    executable = shutil.which(sys.executable) or sys.executable
    try:
        import debugpy  # noqa: F401  (import is the probe)
    except ImportError:
        return None
    return executable


def availability() -> tuple[str, str]:
    """Return ``(status, reason)`` without starting anything."""
    executable = debugpy_path()
    if executable is None:
        return (
            "missing-debugpy",
            "debugpy is not installed in this interpreter: pip install 'debugpy>=1.8'",
        )
    return "ready", executable


def encode_message(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n%s" % (len(body), body)


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one Content-Length framed DAP message."""
    length = 0
    while True:
        line = await reader.readline()
        if not line:
            raise EOFError("debug adapter closed the stream")
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode("ascii", "replace").partition(":")
        if name.strip().lower() == "content-length":
            try:
                length = int(value.strip())
            except ValueError as error:
                raise ValueError(f"invalid Content-Length header: {value!r}") from error
    if length <= 0:
        raise ValueError("message without a Content-Length header")
    return json.loads((await reader.readexactly(length)).decode("utf-8"))


class DebugSession:
    """One debugpy adapter process and the program it runs."""

    def __init__(
        self,
        program: Path,
        *,
        python: str | None = None,
        cwd: str | Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.program = Path(program).expanduser().resolve()
        self.python = python or sys.executable
        self.cwd = Path(cwd).expanduser().resolve() if cwd else self.program.parent
        self.timeout = max(5.0, float(timeout))
        self.process: asyncio.subprocess.Process | None = None
        self.sequence = 0
        self.responses: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.output: list[str] = []
        self.stopped: list[dict[str, Any]] = []
        self._reader: asyncio.Task[None] | None = None
        self._started = False
        self._exit_code: int | None = None
        self._terminated = False

    async def start(self) -> None:
        status, reason = availability()
        if status != "ready":
            raise DebugUnavailable(reason)
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.python,
                "-m",
                ADAPTER_MODULE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as error:
            raise DebugUnavailable(f"cannot start the debug adapter: {error}") from error
        self._reader = asyncio.create_task(self._pump())
        await self._send(
            "initialize",
            {
                "clientID": "assistant",
                "adapterID": "debugpy",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "pathFormat": "path",
                # No terminal to host the debuggee: output comes as events.
                "supportsRunInTerminalRequest": False,
            },
        )
        await self._await_response("initialize")

    async def _pump(self) -> None:
        """Drain adapter messages: the pipe must never stall."""
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                message = await read_message(process.stdout)
                kind = message.get("type")
                if kind == "response":
                    self.responses[str(message.get("command"))] = message
                elif kind == "event":
                    name = str(message.get("event"))
                    body = message.get("body") or {}
                    self.events.append({"event": name, "body": body})
                    if name == "output":
                        if body.get("category") in {None, "stdout", "stderr", "console"}:
                            self.output.append(str(body.get("output", "")))
                    elif name == "stopped":
                        self.stopped.append(body)
                    elif name == "exited":
                        self._exit_code = body.get("exitCode")
                    elif name == "terminated":
                        self._terminated = True
        except (EOFError, ValueError, asyncio.IncompleteReadError):
            return
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise

    async def _send(self, command: str, arguments: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise DebugUnavailable("debug adapter is not running")
        self.sequence += 1
        payload = {
            "seq": self.sequence,
            "type": "request",
            "command": command,
            "arguments": arguments,
        }
        process.stdin.write(encode_message(payload))
        try:
            await process.stdin.drain()
        except (ConnectionResetError, BrokenPipeError) as error:
            raise DebugUnavailable(f"debug adapter closed its input: {error}") from error

    async def _await_response(self, command: str, timeout: float | None = None) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + (timeout or self.timeout)
        while asyncio.get_running_loop().time() < deadline:
            response = self.responses.pop(command, None)
            if response is not None:
                if not response.get("success", False):
                    message = (response.get("message") or "").strip()
                    raise ValueError(f"{command} failed: {message or 'unknown error'}")
                return response
            if self.process is None or self.process.returncode is not None:
                raise DebugUnavailable(f"debug adapter exited during {command}")
            await asyncio.sleep(0.05)
        raise TimeoutError(f"no response to {command} within the timeout")

    async def _await_event(self, name: str, timeout: float | None = None) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + (timeout or self.timeout)
        while asyncio.get_running_loop().time() < deadline:
            for index, item in enumerate(self.events):
                if item["event"] == name:
                    return self.events.pop(index)["body"]
            if name != "terminated" and self._terminated:
                raise DebugUnavailable(f"debuggee terminated before {name}")
            await asyncio.sleep(0.05)
        raise TimeoutError(f"no {name} event within the timeout")

    async def launch(self, breakpoints: list[dict[str, Any]] | None = None) -> None:
        await self._send(
            "launch",
            {
                "program": str(self.program),
                "cwd": str(self.cwd),
                "python": [self.python],
                "console": "internalConsole",
                "justMyCode": False,
                "stopOnEntry": False,
            },
        )
        await self._await_event("initialized")
        if breakpoints:
            await self.set_breakpoints(breakpoints)
        await self._send("configurationDone", {})
        # The launch response only arrives now; skipping it would leave the
        # response map misaligned for the next request.
        await self._await_response("launch")
        self._started = True

    async def set_breakpoints(self, breakpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_file: dict[str, list[dict[str, Any]]] = {}
        for item in breakpoints:
            path = str(item.get("file") or self.program)
            by_file.setdefault(path, []).append(
                {"line": int(item["line"]), "condition": item.get("condition")}
            )
        verified: list[dict[str, Any]] = []
        for path, entries in by_file.items():
            for entry in entries:
                if entry["condition"] is None:
                    entry.pop("condition")
            await self._send(
                "setBreakpoints",
                {"source": {"path": str(Path(path).expanduser().resolve())}, "breakpoints": entries},
            )
            response = await self._await_response("setBreakpoints")
            verified.extend(response.get("body", {}).get("breakpoints", []))
        return verified

    async def wait_for_stop(self, timeout: float | None = None) -> dict[str, Any]:
        return await self._await_event("stopped", timeout)

    async def stack(self, thread_id: int, levels: int = 5) -> list[dict[str, Any]]:
        await self._send("stackTrace", {"threadId": thread_id, "levels": levels})
        response = await self._await_response("stackTrace")
        return response.get("body", {}).get("stackFrames", [])

    async def variables(self, frame_id: int, limit: int = 20) -> list[dict[str, Any]]:
        await self._send("scopes", {"frameId": frame_id})
        scopes = await self._await_response("scopes")
        references = [
            scope.get("variablesReference")
            for scope in scopes.get("body", {}).get("scopes", [])
            if scope.get("variablesReference")
        ]
        collected: list[dict[str, Any]] = []
        for reference in references:
            await self._send("variables", {"variablesReference": reference})
            response = await self._await_response("variables")
            collected.extend(response.get("body", {}).get("variables", []))
            if len(collected) >= limit:
                break
        return collected[:limit]

    async def resume(self, thread_id: int) -> bool:
        await self._send("continue", {"threadId": thread_id, "singleThread": False})
        response = await self._await_response("continue")
        return bool(response.get("body", {}).get("allThreadsContinued", True))

    @property
    def finished(self) -> bool:
        return self._terminated or (self.process is not None and self.process.returncode is not None)

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    def output_text(self, limit: int = MAX_OUTPUT_CHARS) -> str:
        return "".join(self.output)[-limit:]

    async def close(self) -> None:
        process = self.process
        self.process = None
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if process is None:
            return
        try:
            if process.returncode is None:
                if self._started:
                    try:
                        await asyncio.wait_for(
                            self._send("disconnect", {"terminateDebuggee": True}), timeout=5
                        )
                    except (DebugUnavailable, TimeoutError):
                        pass
                process.terminate()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except (TimeoutError, ProcessLookupError, RuntimeError):
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                pass


__all__ = [
    "ADAPTER_MODULE",
    "DebugSession",
    "DebugUnavailable",
    "availability",
    "debugpy_path",
    "encode_message",
    "read_message",
]
