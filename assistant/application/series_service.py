"""Service layer for the programmatic video generator.

Holds the three operations the dashboard exposes: create a character, create a
scene, and read back render results.

Deliberate boundary: this module does NOT reimplement the ``SeriesSpec``
validator. The renderer (``series/src/spec.js``) owns those rules and 29 tests
guard them; a second implementation in Python would drift and quietly disagree
with the thing that actually renders. So :func:`check_scene_structure` is a
cheap gate that rejects input the renderer could never use, and the real
verdict is recorded later on the render itself.
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.models import utcnow
from ..domain.series import (
    SeriesCharacter,
    SeriesCharacterRequest,
    SeriesRender,
    SeriesScene,
    SeriesSceneRequest,
)

# Element types the renderer actually implements. Kept in sync with
# ``SUPPORTED_ELEMENT_TYPES`` in series/src/spec.js.
SUPPORTED_ELEMENT_TYPES = {
    "text",
    "shape",
    "shape3d",
    "image",
    "particles",
    "progress",
    "line",
    "circle",
}

# The renderer always composes inside this design space.
DESIGN_WIDTH = 1920
DESIGN_HEIGHT = 1080


def check_scene_structure(spec: dict[str, Any]) -> list[dict[str, str]]:
    """Structural gate for a scene body.

    Returns a list of issues shaped like the JavaScript validator's, so the UI
    renders both the same way. An empty list means "not obviously wrong", not
    "valid": final validation belongs to the renderer.
    """
    issues: list[dict[str, str]] = []

    def add(level: str, path: str, message: str) -> None:
        issues.append({"level": level, "path": path, "message": message})

    if not isinstance(spec, dict):
        # ValueError here is a plain helper return, not a Pydantic validator.
        return [{"level": "error", "path": "spec", "message": "spec must be an object"}]

    if not isinstance(spec.get("duration"), (int, float)) or spec.get("duration", 0) <= 0:
        add("error", "spec.duration", "duration must be a positive number")

    elements = spec.get("elements")
    if not isinstance(elements, list) or not elements:
        add("error", "spec.elements", "a scene needs at least one element")
        return issues

    for index, element in enumerate(elements):
        at = f"spec.elements[{index}]"
        if not isinstance(element, dict):
            add("error", at, "element must be an object")
            continue
        kind = element.get("type")
        if kind not in SUPPORTED_ELEMENT_TYPES:
            add(
                "error",
                f"{at}.type",
                f"unsupported type {json.dumps(kind)}; supported: "
                f"{', '.join(sorted(SUPPORTED_ELEMENT_TYPES))}",
            )
            continue
        if kind == "text" and not element.get("text"):
            add("error", f"{at}.text", "text elements need a text value")
        if kind == "particles" and not isinstance(element.get("seed"), (int, float)):
            # A warning, not an error: it still renders, just not reproducibly.
            add("warning", f"{at}.seed", "particles without a seed are not reproducible")
        for axis, limit in (("x", DESIGN_WIDTH), ("y", DESIGN_HEIGHT)):
            value = element.get(axis)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                add("error", f"{at}.{axis}", f"expected a number, got {json.dumps(value)}")
            elif value < -limit * 1.5 or value > limit * 1.5:
                add(
                    "warning",
                    f"{at}.{axis}",
                    f"{value} is far outside 0..{limit}; it may be off-frame",
                )

    camera = spec.get("camera")
    if isinstance(camera, dict):
        zoom = camera.get("zoom")
        if isinstance(zoom, (int, float)) and not isinstance(zoom, bool) and zoom <= 0:
            add("error", "spec.camera.zoom", "zoom must be greater than zero")
        keyframes = camera.get("keyframes")
        if keyframes is not None and not isinstance(keyframes, list):
            add("error", "spec.camera.keyframes", "keyframes must be an array")

    return issues


class SeriesService:
    def __init__(self, repository):
        self.repository = repository

    # --- characters ---------------------------------------------------------
    async def list_characters(self) -> list[SeriesCharacter]:
        return await self.repository.list_characters()

    async def create_character(self, request: SeriesCharacterRequest) -> SeriesCharacter:
        return await self.repository.save_character(SeriesCharacter(**request.model_dump()))

    async def get_character(self, character_id: str) -> SeriesCharacter | None:
        return await self.repository.get_character(character_id)

    async def delete_character(self, character_id: str) -> bool:
        return await self.repository.delete_character(character_id)

    # --- scenes -------------------------------------------------------------
    async def list_scenes(self) -> list[SeriesScene]:
        return await self.repository.list_scenes()

    async def get_scene(self, scene_id: str) -> SeriesScene | None:
        return await self.repository.get_scene(scene_id)

    async def create_scene(self, request: SeriesSceneRequest) -> SeriesScene:
        if request.character_id and not await self.repository.get_character(request.character_id):
            raise ValueError("character not found")
        scene = SeriesScene(
            name=request.name,
            character_id=request.character_id,
            duration=float(request.spec.get("duration") or 0.0),
            spec=request.spec,
            issues=check_scene_structure(request.spec),
        )
        return await self.repository.save_scene(scene)

    async def update_scene(self, scene_id: str, request: SeriesSceneRequest) -> SeriesScene:
        existing = await self.repository.get_scene(scene_id)
        if existing is None:
            raise KeyError("scene not found")
        if request.character_id and not await self.repository.get_character(request.character_id):
            raise ValueError("character not found")
        existing.name = request.name
        existing.character_id = request.character_id
        existing.duration = float(request.spec.get("duration") or 0.0)
        existing.spec = request.spec
        existing.issues = check_scene_structure(request.spec)
        existing.updated_at = utcnow()
        return await self.repository.save_scene(existing)

    async def delete_scene(self, scene_id: str) -> bool:
        return await self.repository.delete_scene(scene_id)

    # --- renders ------------------------------------------------------------
    async def list_renders(self, limit: int = 50) -> list[SeriesRender]:
        return await self.repository.list_renders(limit=limit)

    async def get_render(self, render_id: str) -> SeriesRender | None:
        return await self.repository.get_render(render_id)

    async def record_render(self, render: SeriesRender) -> SeriesRender:
        return await self.repository.save_render(render)


__all__ = ["SUPPORTED_ELEMENT_TYPES", "SeriesService", "check_scene_structure"]
