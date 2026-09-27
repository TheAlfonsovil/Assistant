"""Domain models for the programmatic video generator.

The split mirrors the renderer's own contract: a character is a reusable visual
identity, a scene is renderable data, and a render is one attempt at turning a
scene into a file.

Validation note: the authoritative ``SeriesSpec`` validator is
``series/src/spec.js`` (29 tests guard it). Reimplementing those rules in Python
would be a second source of truth that drifts. So the checks here are a cheap
structural gate that rejects obviously malformed input at the API boundary; real
validation still happens at render time in JavaScript, and the issues it
reports are stored on the scene.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from .models import utcnow


class SeriesCharacter(BaseModel):
    """A reusable visual identity referenced by scenes."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    palette: dict[str, str] = Field(default_factory=dict)
    style_notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("description", "style_notes", mode="before")
    @classmethod
    def _text_or_empty(cls, value: Any) -> str:
        return "" if value is None else str(value)

    @field_validator("palette", mode="before")
    @classmethod
    def _palette_or_empty(cls, value: Any) -> dict[str, str]:
        if not value:
            return {}
        if not isinstance(value, dict):
            # ValueError, not TypeError: Pydantic converts it into a
            # ValidationError with the field path. TypeError would escape raw.
            raise ValueError("palette must be an object of color values")  # noqa: TRY004
        return {str(key): str(item) for key, item in value.items()}


class SeriesScene(BaseModel):
    """One renderable scene: data, never code."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1, max_length=255)
    character_id: str | None = None
    duration: float = Field(default=0.0, ge=0)
    spec: dict[str, Any] = Field(default_factory=dict)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("spec", mode="before")
    @classmethod
    def _spec_object(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            # ValueError, not TypeError: Pydantic converts it into a
            # ValidationError with the field path. TypeError would escape raw.
            raise ValueError("spec must be an object describing the scene")  # noqa: TRY004
        return value

    @field_validator("issues", mode="before")
    @classmethod
    def _issue_list(cls, value: Any) -> list[Any]:
        return list(value) if value else []


class SeriesRender(BaseModel):
    """One attempt at rendering a scene into a video file."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    scene_id: str
    status: str = "PENDING"
    format: str = "mp4"
    video_path: str | None = None
    duration: float = 0.0
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class SeriesCharacterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    palette: dict[str, str] = Field(default_factory=dict)
    style_notes: str = ""


class SeriesSceneRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    character_id: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "SeriesCharacter",
    "SeriesCharacterRequest",
    "SeriesRender",
    "SeriesScene",
    "SeriesSceneRequest",
]

