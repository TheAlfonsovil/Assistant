"""The runtime's half of the GUI loop: observe again after an action.

These tests drive ``TaskService._observe_surface`` directly with stub tools, so
they pin the contract — digest, changed, repeats, events — without a browser, a
screen or an LLM.
"""

from __future__ import annotations

from typing import Any

import pytest

from assistant.application import TaskService
from assistant.domain.models import (
    NodeStatus,
    NodeType,
    Operation,
    OperationResult,
    Task,
    TaskEvent,
    TaskNode,
)
from assistant.domain.models import ErrorType as _ErrorType  # noqa: F401  (documented import)
from assistant.infrastructure.db import Database
from assistant.llm import MockLLMProvider
from assistant.tools import Tool, ToolDefinition, ToolRegistry


class StubScreen(Tool):
    """A screen whose digest is whatever the test says it is."""

    definition = ToolDefinition(
        name="screen",
        description="stub",
        methods=["capture"],
        observable_methods=[],
    )

    def __init__(self, checksums: list[str]):
        self.checksums = checksums
        self.calls = 0

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        self.calls += 1
        checksum = self.checksums[min(self.calls - 1, len(self.checksums) - 1)]
        return OperationResult(
            success=True,
            output={
                "image": "data/screenshots/abc.png",
                "image_width": 1280,
                "image_height": 720,
                "scale": 0.66,
                "bytes": 1234,
                "screen": {"left": 0, "top": 0},
                "coordinate_hint": "screen_x = 0 + image_x / 0.66",
            },
            artifacts=[
                {
                    "id": f"art-{self.calls}",
                    "kind": "image",
                    "path": "data/screenshots/abc.png",
                    "checksum": checksum,
                    # The real tool publishes these: without them a model that
                    # sees the image cannot map pixels back to screen points.
                    "metadata": {
                        "kind": "screenshot",
                        "content_type": "image/png",
                        "origin_x": 0,
                        "origin_y": 0,
                        "scale": 0.66,
                        "image_width": 1280,
                        "image_height": 720,
                    },
                }
            ],
        )


class StubInput(Tool):
    """The acting tool: it declares which methods must be observed afterwards."""

    definition = ToolDefinition(
        name="input",
        description="stub",
        methods=["click", "move"],
        observable_methods=["click"],
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        return OperationResult(success=True, output={"acted": method})


class StubBrowser(Tool):
    definition = ToolDefinition(
        name="browser",
        description="stub",
        methods=["snapshot", "click"],
        observable_methods=["click"],
    )

    def __init__(self, digests: list[str], elements: int = 3):
        self.digests = digests
        self.elements = elements
        self.calls = 0

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        if method != "snapshot":
            return OperationResult(success=True, output={"clicked": args.get("ref")})
        self.calls += 1
        digest = self.digests[min(self.calls - 1, len(self.digests) - 1)]
        return OperationResult(
            success=True,
            output={
                "source": "cdp",
                "url": "http://localhost:8000/dashboard/tasks",
                "title": "Tareas",
                "text": "x" * 4000,
                "elements": [
                    {"ref": f"e{i}", "role": "button", "name": f"Botón {i}"}
                    for i in range(self.elements)
                ],
                "elements_total": self.elements,
                "coordinates": True,
                "digest": digest,
            },
        )


async def _service(tmp_path, tools: list[Tool]):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'surface.db'}")
    await database.create_all()
    session = database.sessions()
    service = TaskService(
        session, MockLLMProvider(), ToolRegistry(tools), workspace_root=str(tmp_path)
    )
    return service, database, session


async def _task_and_node(service: TaskService) -> tuple[Task, TaskNode]:
    task = Task(goal="pulsa el botón")
    await service.repository.save_task(task)
    node = TaskNode(
        task_id=task.id,
        type=NodeType.OPERATION,
        description="click",
        status=NodeStatus.READY,
    )
    await service.repository.save_node(node)
    return task, node


@pytest.mark.asyncio
async def test_the_capture_of_an_action_reaches_a_model_that_can_see(tmp_path):
    """The loop is only useful if the model receives the image it just caused.

    With vision enabled the fresh capture must arrive as an image part, not as a
    path: otherwise the worker is describing what it cannot see.
    """
    from assistant.attachments import image_parts

    screen = StubScreen(["s1", "s2"])
    service, database, session = await _service(tmp_path, [screen, StubInput()])
    try:
        task, node = await _task_and_node(service)
        await service._observe_surface(task, node, Operation(tool="input", method="click"))

        context = await service.context_builder.for_agent_decision(task)

        assert context["screenshots"], "the capture must travel with the next turn"
        reference = context["screenshots"][-1]
        assert reference["path"] == "data/screenshots/abc.png"
        assert reference["origin_x"] == 0 and reference["scale"] == 0.66
    finally:
        await session.close()
        await database.close()

    # The provider decides from the flag, and the file has to exist to be read.
    shot = tmp_path / "data" / "screenshots" / "abc.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    parts = image_parts(
        [reference], max_bytes=1_000_000, detail="original", base_dir=tmp_path
    )

    assert parts and parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"].startswith("data:")
    assert parts[0]["image_url"]["detail"] == "original"


@pytest.mark.asyncio
async def test_a_non_observable_action_is_not_observed(tmp_path):
    browser = StubBrowser(["d1"])
    service, database, session = await _service(tmp_path, [browser])
    try:
        task, node = await _task_and_node(service)

        # ``snapshot`` only reads; there is nothing to observe afterwards.
        surface = await service._observe_surface(
            task, node, Operation(tool="browser", method="snapshot")
        )

        assert surface is None
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_an_unknown_tool_is_not_observed(tmp_path):
    service, database, session = await _service(tmp_path, [StubScreen(["a"]), StubInput()])
    try:
        task, node = await _task_and_node(service)

        surface = await service._observe_surface(
            task, node, Operation(tool="teleport", method="click")
        )

        assert surface is None
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_the_first_observation_has_nothing_to_compare_with(tmp_path):
    screen = StubScreen(["a"])
    service, database, session = await _service(tmp_path, [screen, StubInput()])
    try:
        task, node = await _task_and_node(service)

        surface = await service._observe_surface(
            task, node, Operation(tool="input", method="click")
        )

        assert surface is not None
        assert surface["kind"] == "screen"
        assert surface["changed"] is None
        assert surface["attempts_without_change"] == 0
        assert "hint" not in surface
        assert surface["digest"] == "a"
        # The capture is published as evidence, not just described.
        artifacts = await service.repository.list_artifacts(task.id)
        assert [item.checksum for item in artifacts] == ["a"]
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_an_action_that_changed_nothing_is_reported_and_counted(tmp_path):
    screen = StubScreen(["same", "same"])
    service, database, session = await _service(tmp_path, [screen, StubInput()])
    try:
        task, node = await _task_and_node(service)
        operation = Operation(tool="input", method="click")

        await service._observe_surface(task, node, operation)
        surface = await service._observe_surface(task, node, operation)

        assert surface is not None
        assert surface["changed"] is False
        assert surface["attempts_without_change"] == 1
        assert "did not change" in surface["hint"]
        stored = await service.get_task(task.id)
        assert stored.metadata["surface"]["repeats"] == 1
        events = [event.event_type for event in await service.repository.list_events(task.id)]
        assert events.count("SURFACE_OBSERVED") == 2
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_a_real_change_clears_the_no_progress_counter(tmp_path):
    screen = StubScreen(["first", "first", "second"])
    service, database, session = await _service(tmp_path, [screen, StubInput()])
    try:
        task, node = await _task_and_node(service)
        operation = Operation(tool="input", method="click")

        for _ in range(3):
            surface = await service._observe_surface(task, node, operation)

        assert surface["changed"] is True
        assert surface["attempts_without_change"] == 0
        assert "hint" not in surface
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_a_page_observation_is_trimmed_for_the_next_prompt(tmp_path):
    browser = StubBrowser(["d1", "d2"], elements=40)
    service, database, session = await _service(tmp_path, [browser])
    try:
        task, node = await _task_and_node(service)

        await service._observe_surface(
            task, node, Operation(tool="browser", method="click")
        )
        surface = await service._observe_surface(
            task, node, Operation(tool="browser", method="click")
        )

        assert surface["kind"] == "page"
        assert surface["changed"] is True
        assert surface["url"].endswith("/dashboard/tasks")
        # The excerpt and the element list are bounded: a huge page must not
        # flood the next turn.
        assert len(surface["text_excerpt"]) == 1400
        assert len(surface["elements"]) == 24
        assert surface["elements_total"] == 40
        assert surface["coordinates"] is True
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_a_failing_observation_does_not_break_the_turn(tmp_path):
    class Broken(StubScreen):
        async def execute(self, method, args, timeout):  # noqa: ANN001
            raise RuntimeError("no display")

    service, database, session = await _service(tmp_path, [Broken(["a"]), StubInput()])
    try:
        task, node = await _task_and_node(service)

        surface = await service._observe_surface(
            task, node, Operation(tool="input", method="click")
        )

        assert surface is None
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_observable_declarations_are_what_the_runtime_reads(tmp_path):
    service, database, session = await _service(tmp_path, [StubBrowser(["d1"])])
    try:
        browser = service.tools.definition("browser")

        assert browser.observes_after("click") is True
        assert browser.observes_after("snapshot") is False
        assert browser.observes_after("inspect") is False
    finally:
        await session.close()
        await database.close()
