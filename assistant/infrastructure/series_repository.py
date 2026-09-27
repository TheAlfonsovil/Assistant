"""Persistence for series characters, scenes and renders.

Kept separate from TaskRepository so the video generator's tables stay isolated
from the task engine, and so a reset of task state does not take authored
content with it.
"""

from __future__ import annotations

from sqlalchemy import select

from ..domain.models import utcnow
from ..domain.series import SeriesCharacter, SeriesRender, SeriesScene
from ..infrastructure.orm import (
    SeriesCharacterRow,
    SeriesRenderRow,
    SeriesSceneRow,
)


class SeriesRepository:
    def __init__(self, session):
        self.session = session

    # --- characters ---------------------------------------------------------
    @staticmethod
    def _character(row: SeriesCharacterRow) -> SeriesCharacter:
        return SeriesCharacter(
            id=row.id,
            name=row.name,
            description=row.description,
            palette=row.palette or {},
            style_notes=row.style_notes,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def list_characters(self) -> list[SeriesCharacter]:
        result = await self.session.execute(
            select(SeriesCharacterRow).order_by(SeriesCharacterRow.created_at.desc())
        )
        return [self._character(row) for row in result.scalars()]

    async def get_character(self, character_id: str) -> SeriesCharacter | None:
        row = await self.session.get(SeriesCharacterRow, character_id)
        return self._character(row) if row else None

    async def save_character(self, character: SeriesCharacter) -> SeriesCharacter:
        row = await self.session.get(SeriesCharacterRow, character.id)
        if row is None:
            self.session.add(
                SeriesCharacterRow(
                    id=character.id,
                    name=character.name,
                    description=character.description,
                    palette=character.palette,
                    style_notes=character.style_notes,
                    created_at=character.created_at,
                    updated_at=character.updated_at,
                )
            )
        else:
            row.name = character.name
            row.description = character.description
            row.palette = character.palette
            row.style_notes = character.style_notes
            row.updated_at = character.updated_at
        await self.session.commit()
        return character

    async def delete_character(self, character_id: str) -> bool:
        """Delete a character, detaching scenes instead of cascading.

        Losing authored scenes because a character was removed would be a
        destructive surprise; the scenes simply stop referencing it.
        """
        row = await self.session.get(SeriesCharacterRow, character_id)
        if row is None:
            return False
        for scene in await self.scenes_for_character(character_id):
            scene.character_id = None
            scene.updated_at = utcnow()
            await self.save_scene(scene)
        await self.session.delete(row)
        await self.session.commit()
        return True

    # --- scenes -------------------------------------------------------------
    @staticmethod
    def _scene(row: SeriesSceneRow) -> SeriesScene:
        return SeriesScene(
            id=row.id,
            name=row.name,
            character_id=row.character_id,
            duration=row.duration,
            spec=row.spec_json or {},
            issues=row.issues or [],
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def list_scenes(self) -> list[SeriesScene]:
        result = await self.session.execute(
            select(SeriesSceneRow).order_by(SeriesSceneRow.created_at.desc())
        )
        return [self._scene(row) for row in result.scalars()]

    async def get_scene(self, scene_id: str) -> SeriesScene | None:
        row = await self.session.get(SeriesSceneRow, scene_id)
        return self._scene(row) if row else None

    async def scenes_for_character(self, character_id: str) -> list[SeriesScene]:
        result = await self.session.execute(
            select(SeriesSceneRow).where(SeriesSceneRow.character_id == character_id)
        )
        return [self._scene(row) for row in result.scalars()]

    async def save_scene(self, scene: SeriesScene) -> SeriesScene:
        row = await self.session.get(SeriesSceneRow, scene.id)
        if row is None:
            self.session.add(
                SeriesSceneRow(
                    id=scene.id,
                    name=scene.name,
                    character_id=scene.character_id,
                    duration=scene.duration,
                    spec_json=scene.spec,
                    issues=scene.issues,
                    created_at=scene.created_at,
                    updated_at=scene.updated_at,
                )
            )
        else:
            row.name = scene.name
            row.character_id = scene.character_id
            row.duration = scene.duration
            row.spec_json = scene.spec
            row.issues = scene.issues
            row.updated_at = scene.updated_at
        await self.session.commit()
        return scene

    async def delete_scene(self, scene_id: str) -> bool:
        row = await self.session.get(SeriesSceneRow, scene_id)
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.commit()
        return True


    # --- renders ------------------------------------------------------------
    @staticmethod
    def _render(row: SeriesRenderRow) -> SeriesRender:
        return SeriesRender(
            id=row.id,
            scene_id=row.scene_id,
            status=row.status,
            format=row.format,
            video_path=row.video_path,
            duration=row.duration,
            error=row.error,
            created_at=row.created_at,
            finished_at=row.finished_at,
        )

    async def list_renders(self, limit: int = 50) -> list[SeriesRender]:
        result = await self.session.execute(
            select(SeriesRenderRow)
            .order_by(SeriesRenderRow.created_at.desc())
            .limit(limit)
        )
        return [self._render(row) for row in result.scalars()]

    async def get_render(self, render_id: str) -> SeriesRender | None:
        row = await self.session.get(SeriesRenderRow, render_id)
        return self._render(row) if row else None

    async def save_render(self, render: SeriesRender) -> SeriesRender:
        row = await self.session.get(SeriesRenderRow, render.id)
        if row is None:
            self.session.add(
                SeriesRenderRow(
                    id=render.id,
                    scene_id=render.scene_id,
                    status=render.status,
                    format=render.format,
                    video_path=render.video_path,
                    duration=render.duration,
                    error=render.error,
                    created_at=render.created_at,
                    finished_at=render.finished_at,
                )
            )
        else:
            row.scene_id = render.scene_id
            row.status = render.status
            row.format = render.format
            row.video_path = render.video_path
            row.duration = render.duration
            row.error = render.error
            row.finished_at = render.finished_at
        await self.session.commit()
        return render


__all__ = ["SeriesRepository"]
