"""Observable browser control for the local computer.

The tool is built around one loop, because that is the only way browser work can
be honest: **snapshot → act → snapshot again**. ``snapshot`` returns the page as
structured text with a reference per actionable element, the ``click``/``type``
methods act on those references through the DevTools protocol, and every action
reports the digest before and after so ``changed`` is a fact rather than an
assumption. A page that did not change after an action is a failed action, not
a success story.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from assistant.domain.models import ErrorType, OperationResult
from assistant.tools import Tool, ToolDefinition

from . import cdp
from .page import (
    CLICK_POINT_SCRIPT,
    FOCUS_SCRIPT,
    PageError,
    find,
    matches,
    observe,
    summarise,
)

WAIT_INTERVAL_SECONDS = 0.4
MAX_WAIT_SECONDS = 30.0


class BrowserTool(Tool):
    definition = ToolDefinition(
        name="browser",
        description=(
            "Inspect and control the local browser: snapshot the page as text, act on "
            "an element reference, and verify that the action changed the page"
        ),
        methods=[
            "open",
            "snapshot",
            "click",
            "type",
            "navigate",
            "wait_for",
            "inspect",
            "log",
            "close_tab",
            "close_site",
            "close_browser",
        ],
        argument_schema={
            "url": {"type": "string"},
            "browser_id": {"type": "string"},
            "tab_id": {"type": "string"},
            "site": {"type": "string"},
            "origin": {"type": "string"},
            "limit": {"type": "integer"},
            "ref": {"type": "string"},
            "text": {"type": "string"},
            "submit": {"type": "boolean"},
            "port": {"type": "integer"},
            "timeout_seconds": {"type": "number"},
            "predicate": {"type": "object"},
            "expect_change": {"type": "boolean"},
        },
        method_argument_schema={
            "snapshot": {"url": {"type": "string"}, "port": {"type": "integer"}},
            "click": {
                "ref": {"type": "string", "required": True},
                "port": {"type": "integer"},
                "expect_change": {"type": "boolean"},
            },
            "type": {
                "ref": {"type": "string", "required": True},
                "text": {"type": "string", "required": True},
                "submit": {"type": "boolean"},
                "port": {"type": "integer"},
            },
            "navigate": {"url": {"type": "string", "required": True}},
            "wait_for": {
                "predicate": {"type": "object"},
                "timeout_seconds": {"type": "number"},
            },
        },
        permissions=["browser.read", "browser.open", "browser.close", "browser.act"],
        idempotent=False,
        # These change what the user can see, so the runtime re-observes the page
        # right after them and hands the result to the next turn.
        observable_methods=["open", "click", "type", "navigate"],
    )

    def __init__(self, log_path: str | Path = "data/browser-interactions.jsonl"):
        self.log_path = Path(log_path)
        self._known_browsers: dict[str, dict[str, Any]] = {}

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        origin = str(args.get("origin") or "assistant")
        try:
            if method == "open":
                result = await self._open(args)
            elif method == "snapshot":
                result = await self._snapshot(args, timeout)
            elif method == "click":
                result = await self._click(args, timeout)
            elif method == "type":
                result = await self._type(args, timeout)
            elif method == "navigate":
                result = await self._navigate(args, timeout)
            elif method == "wait_for":
                result = await self._wait_for(args, timeout)
            elif method == "inspect":
                result = await self._inspect(timeout)
            elif method == "log":
                result = await self._read_log(args)
            elif method in {"close_tab", "close_site", "close_browser"}:
                result = await self._close(method, args, timeout)
            else:
                result = self._invalid(f"Unsupported browser method: {method}")
        except (OSError, ValueError, httpx.HTTPError, PageError, cdp.CdpError) as error:
            result = OperationResult(
                success=False, error=str(error), error_type=ErrorType.TOOL_FAILURE
            )
        await self._record(
            action=method,
            origin=origin,
            url=args.get("url"),
            browser_id=args.get("browser_id"),
            tab_id=args.get("tab_id"),
            target=args.get("site") or args.get("browser_id") or args.get("tab_id"),
            result=result,
        )
        return result

    async def _open(self, args: dict[str, Any]) -> OperationResult:
        url = args.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return self._invalid("browser.open requires an http(s) URL")
        opened = await asyncio.to_thread(webbrowser.open, url, 2)
        return OperationResult(
            success=bool(opened),
            output={"url": url, "opened": bool(opened), "verified": False, "next_step": "browser.inspect"},
            error=None if opened else "default browser did not accept the URL",
            error_type=None if opened else ErrorType.TOOL_FAILURE,
            retryable=not bool(opened),
            side_effects=["browser_window"],
        )

    # -- the observation loop -------------------------------------------------
    async def _live_session(self, args: dict[str, Any], timeout: float) -> cdp.CdpSession:
        """Open a session against the tab this operation should act on."""
        port = args.get("port")
        found = await cdp.find_page(
            (int(port),) if port else cdp.DEFAULT_PORTS,
            url_contains=str(args.get("url") or "") or None,
        )
        if found is None:
            raise PageError(
                "no debuggable tab is listening; start Chromium or Edge with "
                "--remote-debugging-port=9222, or use screen.capture plus input.* "
                "for a window that cannot be debugged"
            )
        _port, target = found
        return cdp.CdpSession(
            str(target["webSocketDebuggerUrl"]), timeout=min(max(timeout, 2.0), 20.0)
        )

    @staticmethod
    def _observation_output(observation: dict[str, Any]) -> dict[str, Any]:
        return {
            **observation,
            "summary": summarise(observation),
            "next_step": "act on a ref, then snapshot again if the digest did not change",
        }

    async def _snapshot(self, args: dict[str, Any], timeout: float) -> OperationResult:
        observation = await observe(
            url=args.get("url"),
            port=args.get("port"),
            timeout=min(max(timeout, 2.0), 20.0),
        )
        return OperationResult(
            success=True,
            output=self._observation_output(observation),
            started_at=datetime.now(UTC),
        )

    async def _click(self, args: dict[str, Any], timeout: float) -> OperationResult:
        ref = str(args.get("ref") or "").strip()
        if not ref:
            return self._invalid("browser.click requires ref from the last snapshot")
        expect_change = bool(args.get("expect_change", True))
        before = await observe(port=args.get("port"), timeout=min(max(timeout, 2.0), 20.0))
        element = find(before, ref)
        async with await self._live_session(args, timeout) as session:
            point = await session.evaluate(CLICK_POINT_SCRIPT % ref)
            if not isinstance(point, dict):
                return OperationResult(
                    success=False,
                    error=f"element {ref} is not in the live page; take a new snapshot",
                    error_type=ErrorType.NOT_FOUND,
                    output=self._observation_output(before),
                )
            for event_type in ("mousePressed", "mouseReleased"):
                await session.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event_type,
                        "x": point["x"],
                        "y": point["y"],
                        "button": "left",
                        "clickCount": 1,
                    },
                )
            await asyncio.sleep(WAIT_INTERVAL_SECONDS)
        after = await observe(port=args.get("port"), timeout=min(max(timeout, 2.0), 20.0))
        changed = before.get("digest") != after.get("digest")
        output = {
            "clicked": ref,
            "element": element,
            "before_digest": before.get("digest"),
            "after_digest": after.get("digest"),
            "changed": changed,
            "observation": self._observation_output(after),
        }
        if expect_change and not changed:
            # An action that changed nothing is a failed action. Saying so is the
            # whole point of observing before and after.
            return OperationResult(
                success=False,
                output=output,
                error=(
                    f"clicking {ref} did not change the page (digest {after.get('digest')}); "
                    "the element may be inert, covered, or the page needs JavaScript enabled"
                ),
                error_type=ErrorType.TOOL_FAILURE,
                retryable=False,
                side_effects=["browser_action"],
            )
        return OperationResult(
            success=True, output=output, side_effects=["browser_action"]
        )

    async def _type(self, args: dict[str, Any], timeout: float) -> OperationResult:
        ref = str(args.get("ref") or "").strip()
        text = args.get("text")
        if not ref or not isinstance(text, str):
            return self._invalid("browser.type requires ref and text")
        submit = bool(args.get("submit", False))
        before = await observe(port=args.get("port"), timeout=min(max(timeout, 2.0), 20.0))
        async with await self._live_session(args, timeout) as session:
            focused = await session.evaluate(FOCUS_SCRIPT % ref)
            if not focused:
                return OperationResult(
                    success=False,
                    error=f"element {ref} is not in the live page; take a new snapshot",
                    error_type=ErrorType.NOT_FOUND,
                    output=self._observation_output(before),
                )
            await session.send("Input.insertText", {"text": text})
            if submit:
                for event_type in ("rawKeyDown", "keyUp"):
                    await session.send(
                        "Input.dispatchKeyEvent",
                        {
                            "type": event_type,
                            "key": "Enter",
                            "code": "Enter",
                            "windowsVirtualKeyCode": 13,
                            "nativeVirtualKeyCode": 13,
                        },
                    )
            await asyncio.sleep(WAIT_INTERVAL_SECONDS)
        after = await observe(port=args.get("port"), timeout=min(max(timeout, 2.0), 20.0))
        return OperationResult(
            success=True,
            output={
                "typed_into": ref,
                "chars": len(text),
                "submitted": submit,
                "before_digest": before.get("digest"),
                "after_digest": after.get("digest"),
                "changed": before.get("digest") != after.get("digest"),
                "observation": self._observation_output(after),
            },
            side_effects=["browser_action"],
        )

    async def _navigate(self, args: dict[str, Any], timeout: float) -> OperationResult:
        url = args.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return self._invalid("browser.navigate requires an http(s) URL")
        async with await self._live_session(args, timeout) as session:
            await session.send("Page.enable")
            await session.send("Page.navigate", {"url": url})
            deadline = asyncio.get_running_loop().time() + min(max(timeout, 2.0), 20.0)
            while asyncio.get_running_loop().time() < deadline:
                state = await session.evaluate("document.readyState")
                if state == "complete":
                    break
                await asyncio.sleep(WAIT_INTERVAL_SECONDS)
        observation = await observe(port=args.get("port"), timeout=min(max(timeout, 2.0), 20.0))
        return OperationResult(
            success=observation.get("url") is not None,
            output={"navigated_to": url, "observation": self._observation_output(observation)},
            side_effects=["browser_action"],
        )

    async def _wait_for(self, args: dict[str, Any], timeout: float) -> OperationResult:
        predicate = args.get("predicate") or {}
        if not isinstance(predicate, dict) or not predicate:
            return self._invalid(
                "browser.wait_for requires a predicate such as {\"text_contains\": \"Saved\"}"
            )
        budget = min(max(float(args.get("timeout_seconds") or 10.0), 0.5), MAX_WAIT_SECONDS)
        deadline = asyncio.get_running_loop().time() + budget
        observation = await observe(port=args.get("port"), timeout=min(max(timeout, 2.0), 20.0))
        while not matches(observation, predicate):
            if asyncio.get_running_loop().time() >= deadline:
                return OperationResult(
                    success=False,
                    error=f"the page did not satisfy {predicate} within {budget}s",
                    error_type=ErrorType.TOOL_FAILURE,
                    output=self._observation_output(observation),
                )
            await asyncio.sleep(WAIT_INTERVAL_SECONDS)
            observation = await observe(
                port=args.get("port"), timeout=min(max(timeout, 2.0), 20.0)
            )
        return OperationResult(
            success=True,
            output={"satisfied": predicate, "observation": self._observation_output(observation)},
        )

    async def _inspect(self, timeout: float) -> OperationResult:
        browsers: list[dict[str, Any]] = []
        for port in (9222, 9223, 9224):
            try:
                async with httpx.AsyncClient(timeout=min(max(timeout, 1.0), 5.0)) as client:
                    response = await client.get(f"http://127.0.0.1:{port}/json/list")
                    response.raise_for_status()
                    tabs = response.json()
                browser_id = f"chromium:{port}"
                normalized = [self._normalize_tab(browser_id, port, tab) for tab in tabs]
                self._known_browsers[browser_id] = {"port": port, "kind": "chromium"}
                browsers.append({
                    "browser_id": browser_id,
                    "kind": "chromium",
                    "port": port,
                    "tabs": normalized,
                    "tab_count": len(normalized),
                    "observable": True,
                })
            except (httpx.HTTPError, OSError, ValueError):
                continue
        if browsers:
            return OperationResult(success=True, output={"browsers": browsers, "observable": True})
        return await self._inspect_windows()

    @staticmethod
    def _normalize_tab(browser_id: str, port: int, tab: dict[str, Any]) -> dict[str, Any]:
        tab_id = str(tab.get("id") or tab.get("webSocketDebuggerUrl") or "unknown")
        url = tab.get("url", "")
        return {
            "tab_id": tab_id,
            "browser_id": browser_id,
            "title": tab.get("title", ""),
            "url": url,
            "origin": urlparse(url).netloc or None,
            "type": tab.get("type", "page"),
            "debug_port": port,
        }

    async def _inspect_windows(self) -> OperationResult:
        if os.name != "nt":
            return OperationResult(
                success=False,
                error="Browser inspection requires Windows or Chromium DevTools",
                error_type=ErrorType.NOT_FOUND,
            )
        command = (
            "Get-Process chrome,msedge,firefox -ErrorAction SilentlyContinue | "
            "Where-Object {$_.MainWindowHandle -ne 0} | "
            "Select-Object Id,ProcessName,MainWindowHandle,MainWindowTitle | "
            "ConvertTo-Json -Compress"
        )
        completed = await asyncio.to_thread(
            subprocess.run,
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )
        raw = completed.stdout.strip()
        processes = json.loads(raw) if raw else []
        if isinstance(processes, dict):
            processes = [processes]
        browsers = []
        for process in processes:
            browser_id = f"window:{process['Id']}"
            browsers.append({
                "browser_id": browser_id,
                "kind": process.get("ProcessName"),
                "process_id": process.get("Id"),
                "window_handle": process.get("MainWindowHandle"),
                "title": process.get("MainWindowTitle", ""),
                "tabs": [],
                "tab_count": None,
                "observable": False,
                "limitation": "Tab URLs require a DevTools debugging port",
            })
        return OperationResult(success=True, output={
            "browsers": browsers,
            "observable": False,
            "limitation": "Start Chromium with --remote-debugging-port to inspect tabs and URLs",
        })

    async def _close(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        browser_id = args.get("browser_id")
        if method == "close_tab":
            tab_id = args.get("tab_id")
            browser = self._known_browsers.get(str(browser_id))
            if not browser or not tab_id or browser.get("kind") != "chromium":
                return self._invalid("close_tab requires an inspected Chromium browser_id and tab_id")
            async with httpx.AsyncClient(timeout=min(max(timeout, 1.0), 5.0)) as client:
                response = await client.get(f"http://127.0.0.1:{browser['port']}/json/close/{tab_id}")
                response.raise_for_status()
            return OperationResult(success=True, output={"browser_id": browser_id, "tab_id": tab_id, "closed": True})
        if method == "close_site":
            site = str(args.get("site") or "").lower().strip()
            if not site:
                return self._invalid("close_site requires site")
            inspected = await self._inspect(timeout)
            if not inspected.success:
                return inspected
            if not inspected.output.get("observable", False):
                return OperationResult(
                    success=False,
                    error=inspected.output.get("limitation", "Browser tabs are not observable"),
                    error_type=ErrorType.NOT_FOUND,
                )
            closed = []
            for browser in inspected.output["browsers"]:
                for tab in browser.get("tabs", []):
                    if site in str(tab.get("origin") or tab.get("url") or "").lower():
                        result = await self._close("close_tab", {
                            "browser_id": browser["browser_id"], "tab_id": tab["tab_id"]
                        }, timeout)
                        if result.success:
                            closed.append(tab["tab_id"])
            return OperationResult(success=True, output={"site": site, "closed_tabs": closed})
        process_id = self._process_id(browser_id)
        if process_id is None:
            return self._invalid("close_browser requires browser_id window:<pid>")
        completed = await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/PID", str(process_id), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return OperationResult(
                success=False,
                error=completed.stderr.strip() or "browser process could not be closed",
                error_type=ErrorType.TOOL_FAILURE,
            )
        return OperationResult(success=True, output={"browser_id": browser_id, "process_id": process_id, "closed": True})

    @staticmethod
    def _process_id(browser_id: Any) -> int | None:
        value = str(browser_id or "")
        if not value.startswith("window:"):
            return None
        try:
            return int(value.removeprefix("window:"))
        except ValueError:
            return None

    async def _read_log(self, args: dict[str, Any]) -> OperationResult:
        if not self.log_path.exists():
            return OperationResult(success=True, output={"entries": []})
        lines = await asyncio.to_thread(self.log_path.read_text, encoding="utf-8")
        entries = [json.loads(line) for line in lines.splitlines() if line.strip()]
        limit = min(max(int(args.get("limit", 100)), 1), 1000)
        return OperationResult(success=True, output={"entries": entries[-limit:]})

    async def _record(self, *, action: str, origin: str, url: Any, browser_id: Any, tab_id: Any, target: Any, result: OperationResult) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "action": action,
            "origin": origin,
            "url": url,
            "browser_id": browser_id,
            "tab_id": tab_id,
            "target": target,
            "success": result.success,
            "error": result.error,
            "result": result.output if isinstance(result.output, (str, int, float, bool, type(None))) else None,
        }
        await asyncio.to_thread(self._append_log, entry)

    def _append_log(self, entry: dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=True) + "\n")

    @staticmethod
    def _invalid(message: str) -> OperationResult:
        return OperationResult(success=False, error=message, error_type=ErrorType.INVALID_ARGUMENT)


__all__ = ["BrowserTool"]