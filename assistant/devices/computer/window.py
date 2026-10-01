"""Windows UI Automation: observe any desktop window as a tree of controls.

The browser path has ``browser.snapshot``: a page becomes text with a reference
per element, so the worker acts on names instead of guessing pixels. Native
windows had nothing equivalent — only a screenshot and coordinates — which is why
desktop work was the blind spot.

This closes it with the Windows accessibility tree (UI Automation), which every
real application exposes: name, control type, enabled state and bounding
rectangle for each control. Two decisions keep it honest and general:

* **Observation only.** Nothing here clicks or types: the tree plus the existing
  ``input.*`` tools (opt-in) cover acting, and the model decides what to do with
  the structure. No step is hardcoded per application.
* **Bounded and pruned.** Anonymous empty panes are noise, so a control is kept
  when it has a name, is actionable, or has children; the rest is skipped. A
  ``ref`` is only valid inside the answer that produced it, and coordinates come
  from the rectangle.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic
from typing import Any

from assistant.domain.models import ErrorType, OperationResult
from assistant.tools import Tool, ToolDefinition

PACKAGE = "uiautomation"
DEFAULT_MAX_CONTROLS = 80
MAX_MAX_CONTROLS = 400
DEFAULT_MAX_DEPTH = 4
MAX_MAX_DEPTH = 10
MAX_TEXT_CHARS = 80
# Unnamed containers are only interesting when their type says something about
# the layout (a toolbar, a tab strip, a document). Everything else without a
# name is a pane that would only add depth to the answer.
STRUCTURAL_TYPES = {
    "WindowControl",
    "TitleBarControl",
    "MenuBarControl",
    "ToolBarControl",
    "StatusBarControl",
    "TabControl",
    "TabItemControl",
    "ListControl",
    "TreeControl",
    "TableControl",
    "DocumentControl",
    "EditControl",
    "ComboBoxControl",
}
# Patterns that mean "a person can act on this".
_ACTION_PATTERNS = (
    "GetInvokePattern",
    "GetValuePattern",
    "GetTogglePattern",
    "GetSelectionItemPattern",
    "GetExpandCollapsePattern",
    "GetScrollPattern",
    "GetTextPattern",
)


class UiaUnavailable(RuntimeError):
    """Raised when UI Automation cannot be used on this host."""


def _uia():
    """Import the UI Automation wrapper lazily, with an honest failure."""
    try:
        import uiautomation as automation
    except ImportError as error:  # pragma: no cover - depends on the host
        raise UiaUnavailable(
            f"{PACKAGE} is not installed: pip install '{PACKAGE}>=2.0'"
        ) from error
    return automation


def _in_thread(callable_, /, *args, **kwargs):
    """Run a UIA call with COM initialised **in the thread that uses it**.

    Windows requires ``CoInitialize`` per thread, so calling the API from a
    worker thread without it fails with "CoInitialize has not been called". The
    wrapper ships the initialiser for exactly this case.
    """
    automation = _uia()
    initializer = getattr(automation, "UIAutomationInitializerInThread", None)
    if initializer is None:  # pragma: no cover - older releases
        return callable_(*args, **kwargs)
    try:
        context = initializer(debug=False)
    except TypeError:  # pragma: no cover - signature drift
        context = initializer()
    with context:
        return callable_(*args, **kwargs)


def availability() -> tuple[str, str]:
    """Return ``(status, reason)`` without touching any window."""
    try:
        module = _uia()
    except UiaUnavailable as error:
        return "missing-uiautomation", str(error)
    version = getattr(module, "VERSION", None) or getattr(module, "__version__", "")
    return "ready", f"{PACKAGE} {version}".strip()


def _rect(control: Any) -> dict[str, int] | None:
    try:
        box = control.BoundingRectangle
    except Exception:  # noqa: BLE001 - a stale element can throw anything
        return None
    if box is None:
        return None
    try:
        left, top, right, bottom = int(box.left), int(box.top), int(box.right), int(box.bottom)
    except (AttributeError, TypeError, ValueError):
        return None
    return {
        "left": left,
        "top": top,
        "width": max(0, right - left),
        "height": max(0, bottom - top),
    }


def _actionable(control: Any) -> bool:
    for name in _ACTION_PATTERNS:
        try:
            if getattr(control, name)() is not None:
                return True
        except Exception:  # noqa: BLE001 - unsupported pattern or stale element
            continue
    return False


def _describe(control: Any) -> dict[str, Any]:
    try:
        name = str(control.Name or "")[:MAX_TEXT_CHARS]
    except Exception:  # noqa: BLE001
        name = ""
    try:
        kind = str(control.ControlTypeName or "")
    except Exception:  # noqa: BLE001
        kind = ""
    try:
        enabled = bool(control.IsEnabled)
    except Exception:  # noqa: BLE001
        enabled = None
    return {"name": name, "type": kind, "enabled": enabled, "rect": _rect(control)}


def list_windows(limit: int = 40) -> list[dict[str, Any]]:
    """Top-level windows, newest listing every time they are asked for."""
    automation = _uia()
    root = automation.GetRootControl()
    windows: list[dict[str, Any]] = []
    for index, window in enumerate(root.GetChildren()):
        if len(windows) >= limit:
            break
        entry = _describe(window)
        entry["index"] = index
        try:
            entry["pid"] = int(window.ProcessId)
        except Exception:  # noqa: BLE001
            entry["pid"] = None
        entry["class"] = str(getattr(window, "ClassName", "") or "")
        windows.append(entry)
    return windows


def snapshot_window(
    *,
    title: str = "",
    pid: int | None = None,
    index: int | None = None,
    max_controls: int = DEFAULT_MAX_CONTROLS,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> dict[str, Any]:
    """Choose a window and walk its controls, breadth first and bounded."""
    automation = _uia()
    root = automation.GetRootControl()
    target = None
    if title:
        needle = title.casefold()
        for window in root.GetChildren():
            try:
                if needle in str(window.Name or "").casefold():
                    target = window
                    break
            except Exception:  # noqa: BLE001
                continue
        if target is None:
            raise ValueError(f"no window title contains {title!r}")
    elif pid is not None:
        for window in root.GetChildren():
            try:
                if int(window.ProcessId) == int(pid):
                    target = window
                    break
            except Exception:  # noqa: BLE001
                continue
        if target is None:
            raise ValueError(f"no top-level window belongs to pid {pid}")
    elif index is not None:
        children = root.GetChildren()
        if not 0 <= index < len(children):
            raise ValueError(f"window index {index} is out of range (0-{len(children) - 1})")
        target = children[index]
    else:
        target = automation.GetForegroundControl()

    head = _describe(target)
    controls: list[dict[str, Any]] = []
    truncated = False
    queue: list[tuple[Any, int]] = [(target, 0)]
    while queue:
        control, depth = queue.pop(0)
        if len(controls) >= max_controls:
            truncated = True
            break
        try:
            children = control.GetChildren()
        except Exception:  # noqa: BLE001
            children = []
        # One round of property reads per control: each one is a COM call, and
        # asking twice doubles the cost of a walk.
        info = _describe(control)
        named = bool(info["name"])
        structural = info["type"] in STRUCTURAL_TYPES
        keep = depth == 0 or named or structural or _actionable(control)
        if keep:
            info["ref"] = f"w{len(controls) + 1}"
            info["depth"] = depth
            info["children"] = len(children)
            controls.append(info)
        if depth < max_depth:
            queue.extend((child, depth + 1) for child in children)
    return {
        "window": head,
        "controls": controls,
        "count": len(controls),
        "truncated": truncated,
        "max_depth": max_depth,
    }


class WindowTool(Tool):
    definition = ToolDefinition(
        name="window",
        description=(
            "Observe desktop windows through Windows UI Automation: which windows "
            "exist and, for one of them, its controls with name, type, state and "
            "screen rectangle. Read-only: use input.* (opt-in) to act on the "
            "coordinates it reports."
        ),
        methods=["probe", "list", "snapshot"],
        argument_schema={
            "title": {"type": "string", "description": "Window whose title contains this text."},
            "pid": {"type": "integer", "description": "Window owned by this process id."},
            "index": {"type": "integer", "description": "Index from window.list."},
            "max_controls": {"type": "integer", "description": f"1-{MAX_MAX_CONTROLS} controls (default {DEFAULT_MAX_CONTROLS})."},
            "max_depth": {"type": "integer", "description": f"1-{MAX_MAX_DEPTH} levels (default {DEFAULT_MAX_DEPTH})."},
            "limit": {"type": "integer", "description": "Maximum windows listed (default 40)."},
        },
        method_argument_schema={
            "probe": {},
            "list": {"limit": {"type": "integer"}},
            "snapshot": {
                "title": {"type": "string"},
                "pid": {"type": "integer"},
                "index": {"type": "integer"},
                "max_controls": {"type": "integer"},
                "max_depth": {"type": "integer"},
            },
        },
        permissions=["screen.read"],
        idempotent=True,
    )

    def __init__(self, workspace_root: str = ".") -> None:
        # Kept for symmetry with the other computer tools; UIA needs no root.
        self.workspace_root = Path(workspace_root).expanduser().resolve()

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        if method not in self.definition.methods:
            return OperationResult(
                success=False,
                error=f"window does not implement {method}",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        if method == "probe":
            status, detail = availability()
            return OperationResult(
                success=True,
                output={
                    "available": status == "ready",
                    "status": status,
                    "backend": "Windows UI Automation",
                    "reason": None if status == "ready" else detail,
                    "methods": ["list", "snapshot"],
                },
            )
        started = monotonic()
        try:
            if method == "list":
                limit = min(max(int(args.get("limit", 40)), 1), 200)
                # COM calls block, so they never run on the event loop.
                windows = await asyncio.wait_for(
                    asyncio.to_thread(_in_thread, list_windows, limit),
                    timeout=max(5.0, float(timeout)),
                )
                return OperationResult(
                    success=True,
                    output={
                        "windows": windows,
                        "count": len(windows),
                        "duration_seconds": round(monotonic() - started, 2),
                    },
                )
            max_controls = min(
                max(int(args.get("max_controls", DEFAULT_MAX_CONTROLS)), 1), MAX_MAX_CONTROLS
            )
            max_depth = min(max(int(args.get("max_depth", DEFAULT_MAX_DEPTH)), 1), MAX_MAX_DEPTH)
            pid = args.get("pid")
            index = args.get("index")
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    _in_thread,
                    snapshot_window,
                    title=str(args.get("title") or ""),
                    pid=int(pid) if isinstance(pid, int) else None,
                    index=int(index) if isinstance(index, int) else None,
                    max_controls=max_controls,
                    max_depth=max_depth,
                ),
                timeout=max(5.0, float(timeout)),
            )
        except UiaUnavailable as error:
            return OperationResult(
                success=False,
                error=f"UI Automation unavailable: {error}",
                error_type=ErrorType.TOOL_FAILURE,
                retryable=False,
            )
        except (ValueError, TimeoutError, OSError) as error:
            return OperationResult(
                success=False, error=f"window query failed: {error}", error_type=ErrorType.UNKNOWN
            )
        result["duration_seconds"] = round(monotonic() - started, 2)
        result["note"] = (
            "refs are valid only inside this answer; the rectangle is absolute "
            "screen pixels, so input.* can act on its centre"
        )
        return OperationResult(success=True, output=result)


__all__ = [
    "DEFAULT_MAX_CONTROLS",
    "UiaUnavailable",
    "WindowTool",
    "availability",
    "list_windows",
    "snapshot_window",
]
