"""Chrome DevTools Protocol client, small on purpose.

The browser branch already discovers Chromium tabs over HTTP (``/json/list``).
Talking to a tab needs the same protocol over WebSocket, and that is all this is:
connect to one target, send commands, read the matching reply, close. No
framework, no browser automation dependency, and it always runs against the
*browser the user already has open* rather than a private instance.

``websockets`` ships with ``uvicorn[standard]``; when it is missing, callers fall
back to the HTTP observation path, which needs no dependency at all.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

try:  # modern API (websockets >= 13)
    from websockets.asyncio.client import connect as _connect
except ImportError:  # pragma: no cover - older releases
    try:
        from websockets.client import connect as _connect  # type: ignore[no-redef]
    except ImportError:
        _connect = None  # type: ignore[assignment]

DEFAULT_TIMEOUT = 10.0
DEFAULT_PORTS = (9222, 9223, 9224)


class CdpError(RuntimeError):
    """A protocol-level failure: bad target, timeout or a JS exception."""


def available() -> bool:
    """True when a DevTools WebSocket can be opened at all."""
    return _connect is not None


async def list_targets(port: int, timeout: float = 1.0) -> list[dict[str, Any]]:
    """Page targets of one Chromium instance, or an empty list."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"http://127.0.0.1:{port}/json/list")
            response.raise_for_status()
            targets = response.json()
    except (httpx.HTTPError, OSError, ValueError):
        return []
    if not isinstance(targets, list):
        return []
    return [item for item in targets if isinstance(item, dict)]


async def find_page(
    ports: tuple[int, ...] = DEFAULT_PORTS,
    *,
    url_contains: str | None = None,
) -> tuple[int, dict[str, Any]] | None:
    """First debuggable page target, preferring the one whose url matches."""
    found: list[tuple[int, dict[str, Any]]] = []
    for port in ports:
        for target in await list_targets(port):
            if target.get("type") != "page" or not target.get("webSocketDebuggerUrl"):
                continue
            found.append((port, target))
    if not found:
        return None
    if url_contains:
        wanted = url_contains.casefold()
        for port, target in found:
            if wanted in str(target.get("url", "")).casefold():
                return port, target
    return found[0]


class CdpSession:
    """One WebSocket connection to one tab."""

    def __init__(self, web_socket_url: str, timeout: float = DEFAULT_TIMEOUT):
        self.url = web_socket_url
        self.timeout = timeout
        self._socket = None
        self._counter = 0

    async def __aenter__(self) -> CdpSession:
        if _connect is None:
            raise CdpError(
                "the 'websockets' package is required for live DOM control; "
                "install it or use the http observation path"
            )
        try:
            self._socket = await asyncio.wait_for(_connect(self.url), timeout=self.timeout)
        except (OSError, asyncio.TimeoutError, ValueError) as error:
            raise CdpError(f"cannot open the devtools socket: {error}") from error
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._socket is not None:
            try:
                await self._socket.close()
            except (OSError, asyncio.TimeoutError, RuntimeError):  # pragma: no cover
                pass
            self._socket = None

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one command and return its result payload."""
        if self._socket is None:
            raise CdpError("the devtools socket is not open")
        self._counter += 1
        message_id = self._counter
        payload = {"id": message_id, "method": method}
        if params:
            payload["params"] = params
        try:
            await self._socket.send(json.dumps(payload))
            while True:
                raw = await asyncio.wait_for(self._socket.recv(), timeout=self.timeout)
                data = json.loads(raw)
                if data.get("id") != message_id:
                    continue  # an event, not the reply we are waiting for
                if "error" in data:
                    raise CdpError(f"{method} failed: {data['error']}")
                return data.get("result") or {}
        except asyncio.TimeoutError as error:
            raise CdpError(f"{method} timed out after {self.timeout}s") from error
        except (OSError, ValueError) as error:
            raise CdpError(f"{method} failed: {error}") from error

    async def evaluate(self, expression: str, *, await_promise: bool = True) -> Any:
        """Evaluate an expression in the page and return its value."""
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = detail.get("text") or (detail.get("exception") or {}).get("description")
            raise CdpError(f"javascript error: {text or 'unknown'}")
        return (result.get("result") or {}).get("value")
