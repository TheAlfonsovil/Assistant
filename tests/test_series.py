"""Series backend: characters, scenes and render results.

Covers the three dashboard options and the boundaries that matter: the video
endpoint must not serve files outside the series root, and deleting a character
must not destroy authored scenes.
"""

import pytest

from assistant.application.series_service import (
    SUPPORTED_ELEMENT_TYPES,
    SeriesService,
    check_scene_structure,
)
from assistant.domain.series import (
    SeriesCharacterRequest,
    SeriesRender,
    SeriesSceneRequest,
)
from assistant.infrastructure.db import Database
from assistant.infrastructure.series_repository import SeriesRepository


def scene_spec(**overrides):
    spec = {
        "duration": 2.0,
        "background": "#05070d",
        "elements": [{"type": "text", "text": "HELLO", "start": 0, "duration": 2, "y": 860}],
    }
    spec.update(overrides)
    return spec


def errors(issues):
    return [i["path"] for i in issues if i["level"] == "error"]


def warnings(issues):
    return [i["path"] for i in issues if i["level"] == "warning"]


# --- structural gate --------------------------------------------------------
def test_valid_scene_has_no_issues():
    assert check_scene_structure(scene_spec()) == []


def test_scene_needs_a_positive_duration():
    assert "spec.duration" in errors(check_scene_structure({"duration": 0, "elements": [{}]}))
    assert "spec.duration" in errors(check_scene_structure({"duration": -1, "elements": [{}]}))
    assert "spec.duration" in errors(check_scene_structure({"elements": [{}]}))


def test_scene_needs_at_least_one_element():
    assert "spec.elements" in errors(check_scene_structure({"duration": 1, "elements": []}))


def test_unsupported_element_type_is_named_with_the_supported_set():
    issues = check_scene_structure(scene_spec(elements=[{"type": "hologram"}]))
    assert "spec.elements[0].type" in errors(issues)
    message = next(i["message"] for i in issues if i["path"] == "spec.elements[0].type")
    for kind in SUPPORTED_ELEMENT_TYPES:
        assert kind in message, f"the error should list supported type {kind}"


def test_text_element_requires_text():
    spec = scene_spec(elements=[{"type": "text"}])
    assert "spec.elements[0].text" in errors(check_scene_structure(spec))


def test_unseeded_particles_warn_but_do_not_block():
    issues = check_scene_structure(scene_spec(elements=[{"type": "particles", "count": 10}]))
    assert "spec.elements[0].seed" in warnings(issues)
    assert errors(issues) == [], "unseeded particles still render, just not reproducibly"


def test_off_frame_coordinate_warns_but_does_not_block():
    issues = check_scene_structure(scene_spec(elements=[{"type": "shape", "x": 9000}]))
    assert "spec.elements[0].x" in warnings(issues)
    assert errors(issues) == []


def test_non_numeric_coordinate_is_an_error():
    spec = scene_spec(elements=[{"type": "shape", "x": "left"}])
    assert "spec.elements[0].x" in errors(check_scene_structure(spec))


def test_booleans_are_not_accepted_as_coordinates():
    # bool subclasses int in Python; True must not silently pass as x=1.
    spec = scene_spec(elements=[{"type": "shape", "x": True}])
    assert "spec.elements[0].x" in errors(check_scene_structure(spec))


def test_camera_zoom_and_keyframes_are_checked():
    assert "spec.camera.zoom" in errors(check_scene_structure(scene_spec(camera={"zoom": 0})))
    assert "spec.camera.zoom" in errors(check_scene_structure(scene_spec(camera={"zoom": -1})))
    spec = scene_spec(camera={"keyframes": "slow"})
    assert "spec.camera.keyframes" in errors(check_scene_structure(spec))
    assert check_scene_structure(scene_spec(camera={"zoom": 1.2, "keyframes": []})) == []


# --- persistence ------------------------------------------------------------
@pytest.mark.asyncio
async def test_character_scene_and_render_round_trip(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'series.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = SeriesService(SeriesRepository(session))

        character = await service.create_character(
            SeriesCharacterRequest(
                name="Ada",
                description="protagonista",
                palette={"primary": "#38bdf8", "accent": "#f59e0b"},
                style_notes="flat shapes, cool palette",
            )
        )
        assert [c.id for c in await service.list_characters()] == [character.id]
        stored = await service.get_character(character.id)
        assert stored.palette["primary"] == "#38bdf8"

        scene = await service.create_scene(
            SeriesSceneRequest(name="intro", character_id=character.id, spec=scene_spec())
        )
        assert scene.duration == 2.0, "duration is derived from the spec"
        assert scene.issues == []
        assert (await service.get_scene(scene.id)).spec["background"] == "#05070d"

        render = await service.record_render(
            SeriesRender(scene_id=scene.id, status="COMPLETED", video_path="output/video.mp4")
        )
        assert [r.id for r in await service.list_renders()] == [render.id]
        assert (await service.get_render(render.id)).video_path == "output/video.mp4"
    await database.close()


@pytest.mark.asyncio
async def test_scene_rejects_an_unknown_character(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'series-char.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = SeriesService(SeriesRepository(session))
        with pytest.raises(ValueError, match="character not found"):
            await service.create_scene(
                SeriesSceneRequest(name="x", character_id="does-not-exist", spec=scene_spec())
            )
    await database.close()


@pytest.mark.asyncio
async def test_scene_issues_are_stored_not_raised(tmp_path):
    """A structurally bad scene is saved with its issues, not rejected.

    The worker needs to see why a spec failed and repair that path, so the API
    records the verdict instead of refusing the draft.
    """
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'series-issues.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = SeriesService(SeriesRepository(session))
        scene = await service.create_scene(
            SeriesSceneRequest(
                name="broken", spec={"duration": 1, "elements": [{"type": "hologram"}]}
            )
        )
        assert "spec.elements[0].type" in errors(scene.issues)
        assert (await service.get_scene(scene.id)).issues == scene.issues
    await database.close()


@pytest.mark.asyncio
async def test_updating_a_scene_revalidates_it(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'series-update.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = SeriesService(SeriesRepository(session))
        scene = await service.create_scene(SeriesSceneRequest(name="intro", spec=scene_spec()))
        assert scene.issues == []

        updated = await service.update_scene(
            scene.id,
            SeriesSceneRequest(
                name="intro v2", spec={"duration": 1, "elements": [{"type": "nope"}]}
            ),
        )
        assert updated.name == "intro v2"
        assert errors(updated.issues) == ["spec.elements[0].type"], "issues are recomputed"

        with pytest.raises(KeyError):
            await service.update_scene("nope", SeriesSceneRequest(name="x", spec=scene_spec()))
    await database.close()


@pytest.mark.asyncio
async def test_deleting_a_character_detaches_its_scenes(tmp_path):
    """Scenes are authored work; removing a character must not destroy them."""
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'series-detach.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = SeriesService(SeriesRepository(session))
        character = await service.create_character(SeriesCharacterRequest(name="Ada"))
        scene = await service.create_scene(
            SeriesSceneRequest(name="intro", character_id=character.id, spec=scene_spec())
        )

        assert await service.delete_character(character.id) is True
        assert await service.list_characters() == []
        kept = await service.get_scene(scene.id)
        assert kept is not None, "the scene must survive"
        assert kept.character_id is None, "it just stops referencing the character"
    await database.close()


@pytest.mark.asyncio
async def test_deleting_a_missing_record_reports_false(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'series-missing.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = SeriesService(SeriesRepository(session))
        assert await service.delete_character("nope") is False
        assert await service.delete_scene("nope") is False
        assert await service.get_render("nope") is None
    await database.close()


# --- video path boundary ----------------------------------------------------
def test_video_path_cannot_escape_the_series_root():
    """A render row is data; it must not make the API read an arbitrary file."""
    from fastapi import HTTPException

    from assistant.api.routers.series import _resolve_video
    from assistant.config import Settings

    settings = Settings(series_root="C:/projects/Assistant/series")
    with pytest.raises(HTTPException) as escaped:
        _resolve_video(settings, "../../../Windows/System32/config/SAM")
    assert escaped.value.status_code == 403

    with pytest.raises(HTTPException) as absolute:
        _resolve_video(settings, r"C:\Windows\System32\drivers\etc\hosts")
    assert absolute.value.status_code == 403


