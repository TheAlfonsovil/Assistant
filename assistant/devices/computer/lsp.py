"""Minimal LSP client: semantic code questions answered by a language server.

The codegraph is a heuristic index (imports and symbols parsed from the text).
A language server answers what the text cannot: the real type of an expression,
where a name is defined, and every place that references it. That is the
difference between "this file mentions `session`" and "`session` is an
``AsyncSession`` and 34 call sites depend on it".

The client is deliberately small and self-reporting:

* It speaks LSP over stdio (Content-Length framed JSON-RPC), nothing else.
* It never pretends to be available: ``find_langserver`` returns ``None`` and the
  caller reports the reason instead of returning a fabricated answer.
* One shared server per workspace, serialised with a lock, so a worker asking
  three questions does not pay the startup cost three times.

Pyright is used because Pylance (the editor's own analyzer) is a VS Code
extension, not a library: pyright is the same engine packaged as a CLI, it runs
on Node, and it needs no Python dependency installed into the interpreter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

CONTENT_LENGTH = b"Content-Length: "
HEADER_END = b"\r\n\r\n"
# The npm package ships the server under this name; there is no separate
# ``pyright-langserver`` package to install.
LANGSERVER_ENTRY = "langserver.index.js"
LANGSERVER_ENV = "ASSISTANT_PYRIGHT_LANGSERVER"
# The same package ships a batch checker next to the language server. Calling it
# with ``node`` directly is ~17x faster than going through ``npx`` (1.1 s vs
# 18.5 s measured on this host), which is the difference between a usable
# verification step and a stall.
CLI_ENTRY = "index.js"
CLI_ENV = "ASSISTANT_PYRIGHT_CLI"


class LanguageServerUnavailable(RuntimeError):
    """Raised when no language server can be started on this host."""


def npx_cache_roots() -> list[Path]:
    """Directories where ``npx`` unpacks packages, newest first."""
    roots: list[Path] = []
    for variable in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(variable)
        if base:
            roots.append(Path(base) / "npm-cache" / "_npx")
    home = Path.home()
    roots.extend([home / ".npm" / "_npx", home / "AppData" / "Local" / "npm-cache" / "_npx"])
    return [root for root in roots if root.is_dir()]


def find_langserver(explicit: str = "") -> Path | None:
    """Locate pyright's language server, or ``None`` with no guessing.

    Order: explicit setting, ``ASSISTANT_PYRIGHT_LANGSERVER`` environment
    variable, the ``npx`` cache (where ``npx pyright`` unpacks it), then the
    npm global prefix.
    """
    candidates: list[Path] = []
    for value in (explicit, os.environ.get(LANGSERVER_ENV, "")):
        if value and value.strip():
            candidates.append(Path(value.strip()))
    for root in npx_cache_roots():
        try:
            candidates.extend(
                sorted(root.glob(f"*/node_modules/pyright/{LANGSERVER_ENTRY}"))
            )
        except OSError:
            continue
    npm = shutil.which("npm")
    if npm:
        for base in (Path(npm).parent, Path(npm).parent.parent):
            candidates.append(base / "node_modules" / "pyright" / LANGSERVER_ENTRY)
    appdata = os.environ.get("APPDATA")
    if appdata:
        # Where ``npm install -g`` lands on Windows.
        candidates.append(
            Path(appdata) / "npm" / "node_modules" / "pyright" / LANGSERVER_ENTRY
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def node_executable() -> str | None:
    """Path to ``node``, or ``None`` when the runtime is not installed."""
    found = shutil.which("node")
    if found:
        return found
    for candidate in (
        Path(r"C:\Program Files\nodejs\node.exe"),
        Path(r"C:\Program Files (x86)\nodejs\node.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def find_pyright_cli(explicit: str = "") -> Path | None:
    """Locate pyright's batch checker (``index.js``), or ``None``."""
    for value in (explicit, os.environ.get(CLI_ENV, "")):
        if value and value.strip():
            candidate = Path(value.strip())
            if candidate.is_file():
                return candidate
    entry = find_langserver()
    if entry is not None:
        candidate = entry.parent / CLI_ENTRY
        if candidate.is_file():
            return candidate
    return None


async def type_check(
    node: str,
    cli: str | Path,
    workspace_root: str | Path,
    *,
    paths: list[str] | None = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Run the batch type checker and return its JSON report.

    A non-zero exit code is expected when the code has errors, so the report is
    the answer and the exit status is not. Output that is not JSON is an error
    worth raising: it means the checker did not run at all.
    """
    arguments = [node, str(cli), "--outputjson"]
    if paths:
        arguments.extend(str(path) for path in paths)
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=str(workspace_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        raise LanguageServerUnavailable(
            f"type check did not finish within {timeout:.0f}s"
        ) from None
    text = stdout.decode("utf-8", "replace").strip()
    if not text.startswith("{"):
        detail = stderr.decode("utf-8", "replace").strip()[:300]
        raise ValueError(f"type checker produced no JSON report: {detail or 'empty output'}")
    return json.loads(text)


def analyzer_version(entry: Path | None) -> str | None:
    """Version of the installed analyzer package, read from its manifest.

    The server does not always advertise ``serverInfo``, so the version comes
    from the package next to the entry point instead of being guessed.
    """
    if entry is None:
        return None
    manifest = Path(entry).parent / "package.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = payload.get("version")
    return f"pyright {version}" if version else None


def path_to_uri(path: str | Path) -> str:
    return Path(path).expanduser().resolve().as_uri()


def uri_to_path(uri: str) -> str:
    """Turn a ``file://`` URI into a local path, keeping Windows drive letters."""
    parsed = urlparse(uri)
    if parsed.scheme not in {"file", ""}:
        return unquote(uri)
    raw = unquote(parsed.path or uri.removeprefix("file://"))
    # Windows URIs look like /c:/projects/... and need the leading slash removed.
    if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return str(Path(raw.replace("/", os.sep)))


def encode_message(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n%s" % (len(body), body)


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one Content-Length framed JSON-RPC message."""
    length = 0
    while True:
        line = await reader.readline()
        if not line:
            raise EOFError("language server closed the stream")
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


class LanguageServer:
    """One pyright language-server process speaking LSP over stdio."""

    def __init__(
        self,
        workspace_root: str | Path,
        entry: Path,
        *,
        node: str = "node",
        request_timeout: float = 60.0,
        start_timeout: float = 60.0,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.entry = Path(entry)
        self.node = node
        self.request_timeout = max(1.0, float(request_timeout))
        self.start_timeout = max(1.0, float(start_timeout))
        self.process: asyncio.subprocess.Process | None = None
        self.capabilities: dict[str, Any] = {}
        self.server_info: dict[str, Any] = {}
        self.opened: set[str] = set()
        # The process belongs to the loop that started it: awaiting its exit
        # from anywhere else is an error, so the owner is recorded.
        self.loop: asyncio.AbstractEventLoop | None = None
        self._pending_id = 0
        self._lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def current_loop(self) -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def orphaned(self) -> bool:
        """True when this process belongs to a loop that is not running now."""
        running = self.current_loop()
        if self.loop is None or running is None:
            return False
        return self.loop is not running

    def discard(self) -> None:
        """Stop the process without awaiting anything (for another loop)."""
        process = self.process
        self.process = None
        self.opened.clear()
        if process is None:
            return
        for action in (process.terminate, process.kill):
            try:
                if process.returncode is None:
                    action()
            except (ProcessLookupError, OSError, RuntimeError):
                continue

    async def start(self) -> None:
        if self.alive and not self.orphaned():
            return
        if self.orphaned():
            self.discard()
        if not self.entry.is_file():
            raise LanguageServerUnavailable(f"language server entry not found: {self.entry}")
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.node,
                str(self.entry),
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(self.workspace_root),
            )
        except OSError as error:
            raise LanguageServerUnavailable(f"cannot start node: {error}") from error
        self.loop = self.current_loop()
        try:
            response = await asyncio.wait_for(
                self._exchange(
                    "initialize",
                    {
                        "processId": os.getpid() if sys.platform != "win32" else None,
                        "clientInfo": {"name": "assistant", "version": "1"},
                        "rootUri": path_to_uri(self.workspace_root),
                        "workspaceFolders": [
                            {"uri": path_to_uri(self.workspace_root), "name": self.workspace_root.name}
                        ],
                        "capabilities": {
                            "textDocument": {
                                "hover": {"contentFormat": ["plaintext"]},
                                "definition": {},
                                "references": {},
                                "publishDiagnostics": {},
                            },
                            "workspace": {"workspaceFolders": True},
                        },
                    },
                ),
                timeout=self.start_timeout,
            )
        except (TimeoutError, EOFError, ValueError, OSError) as error:
            await self.close()
            raise LanguageServerUnavailable(f"language server handshake failed: {error}") from error
        result = response.get("result") or {}
        self.capabilities = result.get("capabilities") or {}
        self.server_info = result.get("serverInfo") or {}
        await self.notify("initialized", {})

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise LanguageServerUnavailable("language server is not running")
        process.stdin.write(encode_message({"jsonrpc": "2.0", "method": method, "params": params}))
        try:
            await process.stdin.drain()
        except (ConnectionResetError, BrokenPipeError) as error:
            raise LanguageServerUnavailable(f"language server closed its input: {error}") from error

    async def request(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        async with self._lock:
            if not self.alive:
                raise LanguageServerUnavailable("language server is not running")
            response = await asyncio.wait_for(
                self._exchange(method, params), timeout=timeout or self.request_timeout
            )
        if "error" in response:
            detail = response["error"].get("message") if isinstance(response["error"], dict) else None
            raise ValueError(f"{method} failed: {detail or response['error']}")
        return response.get("result")

    async def _exchange(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdin is None or process.stdout is None:
            raise LanguageServerUnavailable("language server is not running")
        self._pending_id += 1
        request_id = self._pending_id
        process.stdin.write(
            encode_message(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
        )
        await process.stdin.drain()
        while True:
            message = await read_message(process.stdout)
            if message.get("id") == request_id:
                return message
            await self._handle_server_message(message)

    async def _handle_server_message(self, message: dict[str, Any]) -> None:
        """Answer or ignore server-initiated traffic so the pipe never stalls."""
        method = message.get("method")
        if method and message.get("id") is not None:
            # A server request we do not implement still needs a reply.
            process = self.process
            if process is not None and process.stdin is not None:
                process.stdin.write(
                    encode_message(
                        {"jsonrpc": "2.0", "id": message["id"], "result": None}
                    )
                )
                try:
                    await process.stdin.drain()
                except (ConnectionResetError, BrokenPipeError):
                    return
            return
        if method in {"window/logMessage", "window/showMessage"}:
            logger.debug("language server: %s", (message.get("params") or {}).get("message"))

    async def open_document(self, path: Path) -> str:
        uri = path_to_uri(path)
        if uri in self.opened:
            return uri
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ValueError(f"cannot read {path}: {error}") from error
        await self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "python" if path.suffix == ".py" else "plaintext",
                    "version": 1,
                    "text": text,
                }
            },
        )
        self.opened.add(uri)
        return uri

    async def close(self) -> None:
        if self.orphaned():
            # Its loop is gone: awaiting the exit would raise, so it is killed
            # synchronously and the exception-prone wait is skipped.
            self.discard()
            return
        process = self.process
        self.process = None
        self.opened.clear()
        if process is None:
            return
        try:
            if process.returncode is None:
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


_shared: dict[tuple[int, str], LanguageServer] = {}
_locks: dict[int, asyncio.Lock] = {}


def _loop_key() -> tuple[int, asyncio.AbstractEventLoop | None]:
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        return 0, None
    return id(running), running


def _loop_lock(loop_id: int) -> asyncio.Lock:
    lock = _locks.get(loop_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[loop_id] = lock
    return lock


def langserver_availability(explicit: str = "") -> tuple[str, str]:
    """Return ``(status, reason)`` without starting anything."""

    node = node_executable()
    if not node:
        return "missing-node", "node is not installed, so no language server can run"
    entry = find_langserver(explicit)
    if entry is None:
        return (
            "missing-server",
            "pyright is not in the npx cache; run 'npx --yes pyright --version' once, "
            f"or set {LANGSERVER_ENV} to the path of {LANGSERVER_ENTRY}",
        )
    return "ready", str(entry)


async def shared_server(
    workspace_root: str | Path,
    *,
    explicit: str = "",
    timeout: float = 60.0,
) -> LanguageServer:
    """Start (or reuse) the language server for one workspace root.

    Servers are cached per (loop, root): a process belongs to the loop that
    started it, so a cache entry from a finished loop is discarded instead of
    being reused and awaited from the wrong place.
    """
    status, reason = langserver_availability(explicit)
    if status != "ready":
        raise LanguageServerUnavailable(reason)
    node = node_executable()
    entry = find_langserver(explicit)
    if node is None or entry is None:  # pragma: no cover - guarded above
        raise LanguageServerUnavailable(reason)
    loop_id, running = _loop_key()
    key = (loop_id, str(Path(workspace_root).expanduser().resolve()))
    async with _loop_lock(loop_id):
        server = _shared.get(key)
        if server is not None and (not server.alive or server.orphaned()):
            _shared.pop(key, None)
            server.discard()
            server = None
        if server is None:
            server = LanguageServer(
                key[1], entry, node=node, request_timeout=timeout, start_timeout=timeout
            )
            await server.start()
            _shared[key] = server
        return server


async def shutdown_language_server() -> None:
    """Stop every shared server. Called when the application context closes."""
    loop_id, _running = _loop_key()
    async with _loop_lock(loop_id):
        mine = [key for key in _shared if key[0] == loop_id]
        servers = [_shared.pop(key) for key in mine]
    for server in servers:
        await server.close()
    # Entries from dead loops cannot be awaited: kill them and forget them.
    for key in [key for key in _shared if key[0] != loop_id]:
        _shared.pop(key).discard()


__all__ = [
    "CLI_ENV",
    "LANGSERVER_ENV",
    "LanguageServer",
    "LanguageServerUnavailable",
    "analyzer_version",
    "encode_message",
    "find_langserver",
    "find_pyright_cli",
    "langserver_availability",
    "node_executable",
    "path_to_uri",
    "read_message",
    "shared_server",
    "shutdown_language_server",
    "type_check",
    "uri_to_path",
]
