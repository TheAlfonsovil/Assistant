"""Screen, input and off-peak savings: the unattended-operation surface.

The input tool is deliberately never exercised with synthetic events here: a
test suite must not move a developer's real mouse or type into their windows.
Only its pure translation helpers are asserted.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from assistant.devices.computer.input import (
    IS_WINDOWS as INPUT_WINDOWS,
    NAMED_KEYS,
    parse_key_combination,
    unicode_units,
)
from assistant.devices.computer.screen import (
    IS_WINDOWS as SCREEN_WINDOWS,
    ScreenTool,
    encode_png,
    list_monitors,
)
from assistant.devices.registry import build_tool_registry
from assistant.offpeak import OffPeakPolicy, parse_holidays
from assistant.runtime import TaskRuntime
from assistant.tools import ToolRegistry

# 2026-09-30 is a Wednesday, so weekday logic is exercised directly.
WEDNESDAY_OFFPEAK = datetime(2026, 9, 30, 0, 30, tzinfo=UTC)
WEDNESDAY_PEAK = datetime(2026, 9, 30, 2, 0, tzinfo=UTC)
WEDNESDAY_SECOND_PEAK = datetime(2026, 9, 30, 7, 30, tzinfo=UTC)
WEDNESDAY_MIDDAY = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SATURDAY_PEAK_HOUR = datetime(2026, 10, 3, 2, 0, tzinfo=UTC)


def test_png_encoder_writes_a_valid_signature_and_header():
    data = encode_png(2, 1, bytes([0, 0, 255, 0, 255, 0, 0, 0]))  # BGRA red, green

    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert data[12:16] == b"IHDR"
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    assert (width, height) == (2, 1)
    assert data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.mark.asyncio
async def test_screen_list_reports_monitors_or_a_clear_reason():
    result = await ScreenTool().execute("list", {}, 5)

    assert result.success is True
    assert result.output["monitor_count"] == len(result.output["monitors"])
    if result.output["available"]:
        first = result.output["monitors"][0]
        assert first["width"] > 0 and first["height"] > 0
        assert "left" in first and "top" in first
    else:
        assert result.output["reason"]


@pytest.mark.asyncio
async def test_screen_capture_publishes_an_image_artifact_with_coordinates(tmp_path):
    if not SCREEN_WINDOWS or not list_monitors():
        pytest.skip("no interactive desktop to capture")

    target = tmp_path / "shot.png"
    result = await ScreenTool().execute(
        "capture", {"monitor": 0, "path": str(target), "max_width": 320}, 30
    )

    assert result.success is True, result.error
    assert target.is_file() and target.read_bytes().startswith(b"\x89PNG")
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact["kind"] == "file"
    assert artifact["metadata"]["scale"] <= 1
    assert result.output["image_width"] <= 320
    assert "screen_x =" in result.output["coordinate_hint"]
    # The metadata is what makes a click possible: origin + scale.
    assert artifact["metadata"]["origin_x"] == result.output["screen"]["left"]


@pytest.mark.asyncio
async def test_screen_capture_rejects_an_unknown_monitor():
    result = await ScreenTool().execute("capture", {"monitor": 99}, 5)

    assert result.success is False
    assert "does not exist" in result.error


def test_key_combinations_put_modifiers_first():
    codes = parse_key_combination("ctrl+shift+s")

    assert codes == [0x11, 0x10, ord("S")]
    assert parse_key_combination("enter") == [NAMED_KEYS["enter"]]
    with pytest.raises(ValueError, match="unsupported key"):
        parse_key_combination("banana")


def test_unicode_units_handle_astral_characters_and_accents():
    assert unicode_units("a") == [ord("a")]
    assert unicode_units("á")[0] == ord("á")
    assert len(unicode_units("😀")) == 2  # surrogate pair


def test_input_tool_is_registered_only_when_enabled():
    default = build_tool_registry()
    enabled = build_tool_registry(enable_input=True)

    assert "screen" in [item.name for item in default.definitions()]
    assert "input" not in [item.name for item in default.definitions()]
    assert "input" in [item.name for item in enabled.definitions()]
    assert INPUT_WINDOWS is not None  # platform probe is importable everywhere


def test_input_methods_declare_required_arguments():
    definition = ToolRegistry().definition("shell")
    assert definition is not None

    from assistant.devices.computer.input import InputTool

    input_definition = InputTool.definition
    assert input_definition.idempotent is False
    for method in ("move", "click", "type", "key", "scroll"):
        assert method in input_definition.methods
    assert input_definition.arguments_for("click")["x"]["required"] is True
    assert input_definition.arguments_for("type")["text"]["required"] is True


def test_offpeak_peak_windows_and_weekends():
    policy = OffPeakPolicy()

    assert policy.in_peak(WEDNESDAY_PEAK) is True
    assert policy.in_peak(WEDNESDAY_SECOND_PEAK) is True
    assert policy.in_peak(WEDNESDAY_OFFPEAK) is False
    assert policy.in_peak(WEDNESDAY_MIDDAY) is False
    # Weekends are off-peak in full, even inside a peak hour.
    assert policy.in_peak(SATURDAY_PEAK_HOUR) is False


def test_offpeak_holidays_are_configuration_and_invalid_entries_are_reported():
    holidays, invalid = parse_holidays("2026-10-01, not-a-date, 2026-10-02")

    assert len(holidays) == 2
    assert invalid == ["not-a-date"]

    policy = OffPeakPolicy(enabled=True, holidays="2026-09-30")
    assert policy.in_peak(WEDNESDAY_PEAK) is False  # holiday: off-peak all day
    assert policy.snapshot(WEDNESDAY_PEAK)["holidays_configured"] == 1


def test_offpeak_pauses_only_when_enabled():
    disabled = OffPeakPolicy(enabled=False)
    enabled = OffPeakPolicy(enabled=True)

    assert disabled.is_paused(WEDNESDAY_PEAK) is False
    assert enabled.is_paused(WEDNESDAY_PEAK) is True
    assert enabled.is_paused(WEDNESDAY_MIDDAY) is False


def test_offpeak_next_change_is_the_end_of_the_current_window():
    policy = OffPeakPolicy()

    snapshot = policy.snapshot(WEDNESDAY_PEAK)
    assert snapshot["state"] == "RUNNING"  # savings mode is off by default
    assert snapshot["in_peak"] is True
    assert snapshot["next_change_at"].startswith("2026-09-30T04:00")
    assert snapshot["peak_windows_utc"] == ["01:00-04:00", "06:00-10:00"]


@pytest.mark.asyncio
async def test_runtime_holds_the_queue_during_peak_hours():
    class Repository:
        async def list_tasks(self):
            from assistant.domain.models import Task, TaskStatus

            return [Task(goal="paid work", status=TaskStatus.QUEUED)]

    dispatched: list[str] = []
    policy = OffPeakPolicy(enabled=True)
    policy.in_peak = lambda moment=None: True  # type: ignore[assignment]

    runtime = TaskRuntime(
        Repository(), lambda task_id: _record(dispatched, task_id), offpeak=policy
    )

    assert await runtime.run_once() == 1
    assert dispatched == []
    assert runtime.metrics_snapshot()["offpeak_passes"] == 1
    assert runtime.offpeak_snapshot()["enabled"] is True


@pytest.mark.asyncio
async def test_runtime_dispatches_normally_outside_peak_hours():
    from assistant.domain.models import Task, TaskStatus

    task = Task(goal="work", status=TaskStatus.QUEUED)

    class Repository:
        async def list_tasks(self):
            return [task]

    dispatched: list[str] = []
    policy = OffPeakPolicy(enabled=True)
    policy.in_peak = lambda moment=None: False  # type: ignore[assignment]

    runtime = TaskRuntime(
        Repository(), lambda task_id: _record(dispatched, task_id), offpeak=policy
    )

    assert await runtime.run_once() == 1
    assert dispatched == [task.id]
    assert runtime.metrics_snapshot()["offpeak_passes"] == 0


def test_runtime_offpeak_toggle_works_without_a_configured_policy():
    runtime = TaskRuntime(object(), lambda task_id: None)

    assert runtime.offpeak_snapshot() == {
        "enabled": False,
        "state": "RUNNING",
        "configured": False,
    }
    runtime.set_offpeak_enabled(True)

    snapshot = runtime.offpeak_snapshot()
    assert snapshot["configured"] is True
    assert snapshot["enabled"] is True


async def _record(dispatched: list[str], task_id: str) -> None:
    dispatched.append(task_id)
