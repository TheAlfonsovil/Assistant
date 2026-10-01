"""Desktop windows are observed, not scripted.

``window.snapshot`` must be a description of what is on screen, bounded and
without per-application logic: name, type, state and rectangle. These tests pin
the pruning rule (anonymous panes are noise), the honest availability reporting
and the bounding, with a fake control object so they do not need a desktop.
"""

from __future__ import annotations

import pytest

from assistant.devices.computer import window as window_module
from assistant.devices.computer.window import (
    DEFAULT_MAX_CONTROLS,
    STRUCTURAL_TYPES,
    WindowTool,
    _describe,
    _rect,
)


class FakeBox:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class FakeControl:
    """The subset of the UIA API the tool touches."""

    def __init__(self, name="", type_name="PaneControl", children=None, box=None, enabled=True):
        self.Name = name
        self.ControlTypeName = type_name
        self.IsEnabled = enabled
        self.BoundingRectangle = box
        self.ProcessId = 4242
        self.ClassName = "FakeClass"
        self._children = list(children or [])

    def GetChildren(self):
        return list(self._children)


def test_a_rectangle_is_reported_as_position_and_size():
    control = FakeControl(box=FakeBox(-1920, 10, -1800, 60))

    assert _rect(control) == {"left": -1920, "top": 10, "width": 120, "height": 50}


def test_a_missing_rectangle_is_not_a_crash():
    assert _rect(FakeControl(box=None)) is None
    assert _rect(FakeControl(box="not a box")) is None


def test_a_control_is_described_without_inventing_fields():
    described = _describe(FakeControl(name="Cerrar", type_name="ButtonControl"))

    assert described["name"] == "Cerrar"
    assert described["type"] == "ButtonControl"
    assert described["enabled"] is True


def test_a_very_long_name_is_truncated():
    described = _describe(FakeControl(name="x" * 500))

    assert len(described["name"]) == 80


def test_the_structural_types_are_windows_semantics_not_an_application_list():
    # The rule has to stay general: no application name, no window title.
    joined = " ".join(STRUCTURAL_TYPES).casefold()
    for forbidden in ("vscode", "explorer", "chrome", "notepad"):
        assert forbidden not in joined
    assert "WindowControl" in STRUCTURAL_TYPES


def test_availability_explains_itself_when_the_package_is_absent(monkeypatch):
    def missing():
        raise window_module.UiaUnavailable("uiautomation is not installed: pip install 'uiautomation>=2.0'")

    monkeypatch.setattr(window_module, "_uia", missing)

    status, reason = window_module.availability()

    assert status == "missing-uiautomation"
    assert "uiautomation" in reason


@pytest.mark.asyncio
async def test_the_probe_never_touches_a_window(monkeypatch):
    monkeypatch.setattr(window_module, "availability", lambda: ("ready", "uiautomation 2.0.29"))
    tool = WindowTool()

    result = await tool.execute("probe", {}, 5)

    assert result.success is True
    assert result.output["available"] is True
    assert result.output["methods"] == ["list", "snapshot"]


@pytest.mark.asyncio
async def test_an_unavailable_backend_is_a_clear_failure_not_an_empty_answer(monkeypatch):
    def missing(*_args, **_kwargs):
        raise window_module.UiaUnavailable("uiautomation is not installed")

    monkeypatch.setattr(window_module, "list_windows", missing)
    tool = WindowTool()

    result = await tool.execute("list", {}, 5)

    assert result.success is False
    assert "unavailable" in result.error
    assert result.retryable is False


@pytest.mark.asyncio
async def test_the_tool_validates_its_own_inputs():
    tool = WindowTool()

    unknown = await tool.execute("click", {}, 5)

    assert unknown.success is False
    assert "does not implement" in unknown.error


def _live_reason() -> str:
    status, reason = window_module.availability()
    return "" if status == "ready" else reason


@pytest.mark.skipif(
    _live_reason() != "", reason=f"no UI Automation on this host: {_live_reason()}"
)
def test_the_real_desktop_is_observed_as_a_tree(tmp_path):
    windows = window_module.list_windows(limit=20)

    assert windows, "a desktop always has at least one top-level window"
    top = windows[0]
    assert {"index", "name", "type", "pid", "rect"} <= set(top)
    assert top["rect"]["width"] >= 0

    foreground = window_module.snapshot_window(max_controls=DEFAULT_MAX_CONTROLS, max_depth=3)
    controls = foreground["controls"]

    assert controls, "the foreground window must expose at least its own control"
    assert controls[0]["depth"] == 0
    # Refs are per-answer, and every control carries the data needed to act.
    assert controls[0]["ref"] == "w1"
    assert all("rect" in control for control in controls)
    assert len(controls) <= DEFAULT_MAX_CONTROLS
    named = [control for control in controls if control["name"]]
    assert named, "a window tree with no names at all would be useless to a model"
