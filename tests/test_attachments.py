"""Attachment pipeline: validation, storage, ledger and LLM propagation."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from assistant.application import TaskService
from assistant.attachments import AttachmentError, persist_uploads
from assistant.context import ContextBuilder
from assistant.domain.models import AgentDecision, Project, Task, TaskRequest
from assistant.infrastructure.db import Database
from assistant.llm import DeepSeekLLMProvider, MockLLMProvider
from assistant.tools import ToolRegistry

# A PNG magic header is all the sniffer needs to accept the payload.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
OTHER_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x11" * 64


def _upload(data: bytes, name: str = "screenshot.png") -> dict[str, str]:
    return {
        "filename": name,
        "content_type": "image/png",
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def test_upload_is_rejected_when_the_content_is_not_an_image(tmp_path):
    with pytest.raises(AttachmentError, match="unsupported image format"):
        persist_uploads(
            tmp_path,
            "task-1",
            [{"filename": "notes.txt", "data_base64": base64.b64encode(b"hello").decode()}],
            max_bytes=5_000_000,
            max_count=4,
        )


def test_upload_is_rejected_when_it_exceeds_the_byte_limit(tmp_path):
    with pytest.raises(AttachmentError, match="byte limit"):
        persist_uploads(
            tmp_path, "task-1", [_upload(PNG_BYTES)], max_bytes=8, max_count=4
        )


def test_upload_is_rejected_when_there_are_too_many_attachments(tmp_path):
    with pytest.raises(AttachmentError, match="at most"):
        persist_uploads(
            tmp_path,
            "task-1",
            [_upload(PNG_BYTES), _upload(OTHER_PNG_BYTES)],
            max_bytes=5_000_000,
            max_count=1,
        )


def test_identical_uploads_are_stored_once_per_task(tmp_path):
    references = persist_uploads(
        tmp_path, "task-1", [_upload(PNG_BYTES), _upload(PNG_BYTES)], max_bytes=5_000_000, max_count=4
    )

    assert len(references) == 2
    assert references[0] == references[1]

    other_task = persist_uploads(
        tmp_path, "task-2", [_upload(PNG_BYTES)], max_bytes=5_000_000, max_count=4
    )
    # Same bytes under a different task must not reuse the ledger identity.
    assert other_task[0]["id"] != references[0]["id"]


def test_invalid_base64_is_rejected_with_a_clear_reason(tmp_path):
    with pytest.raises(AttachmentError, match="invalid base64"):
        persist_uploads(
            tmp_path,
            "task-1",
            [{"filename": "broken.png", "data_base64": "not-base64!!"}],
            max_bytes=5_000_000,
            max_count=4,
        )


@pytest.mark.asyncio
async def test_created_task_persists_attachments_in_metadata_and_ledger(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'attach.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session,
            MockLLMProvider(),
            ToolRegistry(),
            workspace_root=str(tmp_path),
        )
        task = await service.create_task(
            TaskRequest(
                title="Analiza la captura",
                goal="describe el error de la captura",
                description="La imagen viene del log de producción.",
                attachments=[_upload(PNG_BYTES)],
            )
        )

        stored = task.metadata["attachments"]
        assert len(stored) == 1
        assert stored[0]["content_type"] == "image/png"
        assert stored[0]["size"] == len(PNG_BYTES)
        assert task.attachments == stored
        assert task.title == "Analiza la captura"
        assert task.instruction.startswith("describe el error")
        assert "log de producción" in task.instruction

        artifacts = await service.repository.list_artifacts(task.id)
        assert [artifact.kind.value for artifact in artifacts] == ["image"]
        assert artifacts[0].producer_node_id is not None

        events = await service.repository.list_events(task.id)
        added = [event for event in events if event.event_type == "ATTACHMENT_ADDED"]
        assert added and added[0].payload["attachments"][0]["filename"] == "screenshot.png"

        # Reloading from the database must keep the attachments.
        reloaded = await service.repository.get_task(task.id)
        assert reloaded is not None
        assert len(reloaded.attachments) == 1


@pytest.mark.asyncio
async def test_attachment_validation_failure_does_not_create_a_task(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'attach-fail.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session, MockLLMProvider(), ToolRegistry(), workspace_root=str(tmp_path)
        )

        with pytest.raises(AttachmentError):
            await service.create_task(
                TaskRequest(goal="adjunta algo roto", attachments=[_upload(b"not an image")])
            )

        assert await service.repository.list_tasks() == []


@pytest.mark.asyncio
async def test_context_states_when_the_model_cannot_view_images(tmp_path):
    task = Task(
        goal="describe la captura",
        metadata={"attachments": [{"id": "a", "filename": "x.png", "content_type": "image/png", "size": 12, "path": "x.png"}]},
    )
    builder = ContextBuilder(object(), ToolRegistry(), vision_enabled=False)

    context = await builder.for_planner(task)

    assert context["attachments"]["viewable_by_model"] is False
    assert context["attachments"]["items"][0]["filename"] == "x.png"
    assert "cannot read image bytes" in context["attachments"]["note"]
    assert context["user_prompt"] == task.instruction


def test_attachment_context_is_empty_without_attachments():
    builder = ContextBuilder(object(), ToolRegistry(), vision_enabled=True)
    assert builder._attachments(Task(goal="sin adjuntos")) == {}


@pytest.mark.asyncio
async def test_vision_provider_sends_multimodal_parts_only_when_enabled(tmp_path):
    image = tmp_path / "shot.png"
    image.write_bytes(PNG_BYTES)
    context = {
        "task": {"id": "t1", "goal": "analiza"},
        "attachments": {
            "items": [
                {
                    "id": "a",
                    "filename": "shot.png",
                    "content_type": "image/png",
                    "size": len(PNG_BYTES),
                    "path": str(image),
                }
            ],
            "viewable_by_model": True,
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": '{"task_id": null, "nodes": []}'},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    seen: list[dict] = []

    async def capturing(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return await handler(request)

    vision_client = httpx.AsyncClient(transport=httpx.MockTransport(capturing))
    vision = DeepSeekLLMProvider(
        "https://api.deepseek.com",
        "deepseek-flash",
        "test-key",
        client=vision_client,
        supports_vision=True,
    )
    text_client = httpx.AsyncClient(transport=httpx.MockTransport(capturing))
    text_only = DeepSeekLLMProvider(
        "https://api.deepseek.com",
        "deepseek-flash",
        "test-key",
        client=text_client,
        supports_vision=False,
    )
    try:
        await vision.plan(dict(context))
        await text_only.plan(dict(context))
    finally:
        await vision.close()
        await text_only.close()

    vision_content = seen[0]["messages"][1]["content"]
    assert isinstance(vision_content, list)
    assert vision_content[0]["type"] == "text"
    assert vision_content[1]["type"] == "image_url"
    assert vision_content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    text_content = seen[1]["messages"][1]["content"]
    assert isinstance(text_content, str)
    assert image.name in text_content


@pytest.mark.asyncio
async def test_delegated_child_inherits_attachments(tmp_path):
    attachments = [
        {
            "id": "a1",
            "filename": "shot.png",
            "content_type": "image/png",
            "size": 12,
            "path": str(tmp_path / "shot.png"),
        }
    ]

    class DelegatingAgent(MockLLMProvider):
        def __init__(self):
            super().__init__()
            self.delegated = False

        async def agent_decide(self, context):
            if context.get("extra_context", {}).get("delegated_from"):
                return AgentDecision(decision_type="COMPLETE", reason="child done")
            if not self.delegated:
                self.delegated = True
                return AgentDecision(
                    decision_type="DELEGATE",
                    reason="split the image review",
                    subtasks=["revisa la captura adjunta"],
                )
            return AgentDecision(decision_type="COMPLETE", reason="parent done")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'delegate.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session, DelegatingAgent(), ToolRegistry(), workspace_root=str(tmp_path)
        )
        task = await service.create_task(
            TaskRequest(
                goal="revisa la captura",
            )
        )
        task.metadata["attachments"] = attachments
        await service.repository.save_task(task)

        await service.run_task(task.id)
        children = [
            item
            for item in await service.repository.list_tasks()
            if item.parent_task_id == task.id
        ]

        assert children, "the delegation must create a child task"
        assert children[0].metadata["attachments"] == attachments


@pytest.mark.asyncio
async def test_attachments_survive_a_project_scoped_task(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'attach-project.db'}")
    await database.create_all()
    async with database.sessions() as session:
        service = TaskService(
            session, MockLLMProvider(), ToolRegistry(), workspace_root=str(tmp_path)
        )
        project_path = tmp_path / "p"
        project_path.mkdir()
        project = await service.create_project(Project(name="p", path=str(project_path)))
        task = await service.create_task(
            TaskRequest(
                title="Captura del proyecto",
                goal="revisa la captura del proyecto",
                project_id=project.id,
                attachments=[_upload(OTHER_PNG_BYTES)],
            )
        )

        assert task.project_id == project.id
        assert len(task.attachments) == 1
        assert (await service.repository.list_artifacts(task.id))[0].kind.value == "image"
