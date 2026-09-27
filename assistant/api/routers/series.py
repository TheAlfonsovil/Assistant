"""Series API: characters, scenes and render results.

The video file is served from a path recorded on the render, and only if it
resolves inside the configured series root: a render row is data, and data must
not be able to make the API read an arbitrary file.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ...config import get_settings
from ...domain.models import utcnow
from ...domain.series import (
    SeriesCharacter,
    SeriesCharacterRequest,
    SeriesRender,
    SeriesScene,
    SeriesSceneRequest,
)
from ..deps import get_context

router = APIRouter(prefix="/series", tags=["series"])


def _resolve_video(settings, relative: str) -> Path:
    root = Path(settings.series_root).resolve()
    candidate = Path(relative)
    if not candidate.is_absolute():
        candidate = (root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    # Refuse anything that escapes the series root, including via symlinks.
    if root != candidate and root not in candidate.parents:
        raise HTTPException(403, "render path escapes the series root")
    if not candidate.is_file():
        raise HTTPException(404, "video file not found")
    return candidate


@router.get("/status")
async def series_status() -> dict:
    """Whether the generator is enabled and where its output lives."""
    settings = get_settings()
    root = Path(settings.series_root)
    return {
        "enabled": settings.series_enabled,
        "root": str(root),
        "root_exists": root.is_dir(),
        "output_exists": (root / "output").is_dir(),
        "format": settings.series_format,
        "width": settings.series_width,
        "height": settings.series_height,
        "fps": settings.series_fps,
        "quality": settings.series_quality,
    }


@router.get("/characters")
async def list_characters(request: Request) -> list[dict]:
    service = get_context(request).service.series
    return [item.model_dump(mode="json") for item in await service.list_characters()]


@router.post("/characters", status_code=201)
async def create_character(request: Request, payload: SeriesCharacterRequest) -> dict:
    service = get_context(request).service.series
    character: SeriesCharacter = await service.create_character(payload)
    return character.model_dump(mode="json")


@router.get("/characters/{character_id}")
async def get_character(request: Request, character_id: str) -> dict:
    service = get_context(request).service.series
    character = await service.get_character(character_id)
    if character is None:
        raise HTTPException(404, "Character not found")
    return character.model_dump(mode="json")


@router.delete("/characters/{character_id}")
async def delete_character(request: Request, character_id: str) -> dict:
    service = get_context(request).service.series
    if not await service.delete_character(character_id):
        raise HTTPException(404, "Character not found")
    return {"deleted": True, "id": character_id}


@router.get("/scenes")
async def list_scenes(request: Request) -> list[dict]:
    service = get_context(request).service.series
    return [item.model_dump(mode="json") for item in await service.list_scenes()]


@router.post("/scenes", status_code=201)
async def create_scene(request: Request, payload: SeriesSceneRequest) -> dict:
    service = get_context(request).service.series
    try:
        scene: SeriesScene = await service.create_scene(payload)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return scene.model_dump(mode="json")


@router.get("/scenes/{scene_id}")
async def get_scene(request: Request, scene_id: str) -> dict:
    service = get_context(request).service.series
    scene = await service.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "Scene not found")
    return scene.model_dump(mode="json")


@router.put("/scenes/{scene_id}")
async def update_scene(request: Request, scene_id: str, payload: SeriesSceneRequest) -> dict:
    service = get_context(request).service.series
    try:
        scene = await service.update_scene(scene_id, payload)
    except KeyError:
        raise HTTPException(404, "Scene not found") from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return scene.model_dump(mode="json")


@router.delete("/scenes/{scene_id}")
async def delete_scene(request: Request, scene_id: str) -> dict:
    service = get_context(request).service.series
    if not await service.delete_scene(scene_id):
        raise HTTPException(404, "Scene not found")
    return {"deleted": True, "id": scene_id}


@router.get("/renders")
async def list_renders(request: Request, limit: int = 50) -> list[dict]:
    service = get_context(request).service.series
    return [item.model_dump(mode="json") for item in await service.list_renders(limit=limit)]


@router.get("/renders/{render_id}")
async def get_render(request: Request, render_id: str) -> dict:
    service = get_context(request).service.series
    render: SeriesRender | None = await service.get_render(render_id)
    if render is None:
        raise HTTPException(404, "Render not found")
    return render.model_dump(mode="json")


@router.get("/renders/{render_id}/video")
async def stream_render(request: Request, render_id: str):
    service = get_context(request).service.series
    render = await service.get_render(render_id)
    if render is None:
        raise HTTPException(404, "Render not found")
    if not render.video_path:
        raise HTTPException(404, "Render has no video file")
    path = _resolve_video(get_settings(), render.video_path)
    return FileResponse(path, media_type=f"video/{render.format or 'mp4'}")


class RenderRecordRequest(BaseModel):
    """Register the outcome of a render performed outside the API.

    The renderer is a Node/Chromium/FFmpeg pipeline; this endpoint only records
    what it produced so the dashboard can list results.
    """

    scene_id: str = Field(min_length=1)
    status: str = Field(default="COMPLETED", max_length=32)
    video_path: str | None = None
    duration: float = Field(default=0.0, ge=0)
    error: str | None = None
    format: str = Field(default="mp4", max_length=16)


@router.post("/renders", status_code=201)
async def record_render(request: Request, payload: RenderRecordRequest) -> dict:
    service = get_context(request).service.series
    if not await service.get_scene(payload.scene_id):
        raise HTTPException(422, "scene not found")
    render = SeriesRender(
        scene_id=payload.scene_id,
        status=payload.status,
        format=payload.format,
        video_path=payload.video_path,
        duration=payload.duration,
        error=payload.error,
        finished_at=utcnow() if payload.status.upper() in {"COMPLETED", "FAILED"} else None,
    )
    saved = await service.record_render(render)
    return saved.model_dump(mode="json")
