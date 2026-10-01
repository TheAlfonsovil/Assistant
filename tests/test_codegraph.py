"""Structural index behaviour: reuse, honest truncation and non-fatal failures.

The codegraph exists to save tokens: it is orientation material that a worker
queries instead of reading every file. That only pays off if it is cheap to keep
current, if a capped index never claims to be complete, and if a broken index
degrades orientation instead of killing the task.
"""

import pytest

from assistant.context import ContextBuilder
from assistant.domain.models import Operation, Project, TaskRequest, TaskStatus
from assistant.infrastructure.db import Database
from assistant.llm import MockLLMProvider
from assistant.tools import ToolRegistry

from assistant.application.service import TaskService


def _project_files(root, count=6):
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (root / f"module_{index}.py").write_text(
            "from helper import thing\n\n\n"
            f"def feature_{index}():\n"
            f"    return thing({index})\n",
            encoding="utf-8",
        )
    (root / "helper.py").write_text("def thing(value):\n    return value\n", encoding="utf-8")


async def _service(tmp_path, **kwargs):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'codegraph.db'}")
    await database.create_all()
    session = database.sessions()
    service = TaskService(session, MockLLMProvider(), ToolRegistry(), **kwargs)
    return service, database, session


@pytest.mark.asyncio
async def test_an_unchanged_project_reuses_its_stored_graph(tmp_path):
    project_path = tmp_path / "tree"
    _project_files(project_path)
    service, database, session = await _service(tmp_path)
    try:
        project = await service.create_project(Project(name="tree", path=str(project_path)))

        first = await service.refresh_project_codegraph(project.id)
        second = await service.refresh_project_codegraph(project.id)

        assert first.codegraph_version == 1
        # Same tree, same caps, inside the TTL: no re-analysis and no version
        # bump, so prompts that carry the version stay stable.
        assert second.codegraph_version == 1

        (project_path / "module_0.py").write_text("def feature_0():\n    return 1\n", encoding="utf-8")
        third = await service.refresh_project_codegraph(project.id)

        assert third.codegraph_version == 2
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_force_rebuild_ignores_the_reuse_window(tmp_path):
    project_path = tmp_path / "tree"
    _project_files(project_path)
    service, database, session = await _service(tmp_path)
    try:
        project = await service.create_project(Project(name="tree", path=str(project_path)))

        assert (await service.refresh_project_codegraph(project.id)).codegraph_version == 1
        forced = await service.refresh_project_codegraph(project.id, force=True)

        assert forced.codegraph_version == 2
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_a_disabled_reuse_window_always_reanalyses(tmp_path):
    project_path = tmp_path / "tree"
    _project_files(project_path)
    service, database, session = await _service(tmp_path, codegraph_refresh_seconds=0)
    try:
        project = await service.create_project(Project(name="tree", path=str(project_path)))

        assert (await service.refresh_project_codegraph(project.id)).codegraph_version == 1
        assert (await service.refresh_project_codegraph(project.id)).codegraph_version == 2
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_a_capped_index_is_declared_partial_and_not_presented_as_complete(tmp_path):
    project_path = tmp_path / "tree"
    _project_files(project_path, count=12)
    service, database, session = await _service(tmp_path)
    try:
        project = await service.create_project(Project(name="tree", path=str(project_path)))
        # The constructor floors the caps (a cap of 3 edges is not a real
        # configuration), so the test sets the effective limit directly.
        service.codegraph_max_edges = 3
        project = await service.refresh_project_codegraph(project.id)
        graph = project.codegraph["graph"]

        assert graph["edges_total"] > 3
        assert graph["edges_truncated"] is True

        summary = ContextBuilder._codegraph_summary(project.codegraph)

        assert summary["truncated"] is True
        assert summary["partial"]["edges"] is True
        assert "capped" in summary["partial"]["note"]
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_a_query_says_when_it_answered_from_a_partial_index(tmp_path):
    project_path = tmp_path / "tree"
    _project_files(project_path, count=12)
    service, database, session = await _service(tmp_path)
    try:
        project = await service.create_project(Project(name="tree", path=str(project_path)))
        service.codegraph_max_edges = 3
        project = await service.refresh_project_codegraph(project.id)

        result = await service.tools.execute(
            Operation(
                tool="codegraph",
                method="query",
                args={
                    "root": str(project_path),
                    "query": "feature_1",
                    "limit": 5,
                    "_persisted_graph": project.codegraph,
                    "_graph_fresh": True,
                },
                timeout=30,
            )
        )

        assert result.success is True
        assert result.output["index_partial"] is True
        assert result.output["note"]
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_a_failed_refresh_warns_instead_of_blocking_the_task(tmp_path):
    project_path = tmp_path / "tree"
    _project_files(project_path)
    service, database, session = await _service(tmp_path)
    try:
        project = await service.create_project(Project(name="tree", path=str(project_path)))
        task = await service.create_task(TaskRequest(goal="audit the tree"))
        assert task.project_id == project.id

        async def broken(project_id, *args, **kwargs):
            raise RuntimeError("index unavailable")

        service.refresh_project_codegraph = broken

        proceeded = await service._refresh_project_codegraph_for_llm(task, "agent")

        assert proceeded is True
        persisted = await service.repository.get_task(task.id)
        assert persisted.status is not TaskStatus.BLOCKED
        assert persisted.metadata["codegraph_error"] == "index unavailable"
        events = await service.repository.list_events(task.id)
        failures = [
            event for event in events if event.event_type == "LLM_CODEGRAPH_REFRESH_FAILED"
        ]
        assert failures and failures[0].payload["blocking"] is False
    finally:
        await session.close()
        await database.close()


@pytest.mark.asyncio
async def test_the_prompt_summary_stays_bounded_and_points_at_the_dense_files(tmp_path):
    project_path = tmp_path / "tree"
    _project_files(project_path, count=40)
    service, database, session = await _service(tmp_path)
    try:
        project = await service.create_project(Project(name="tree", path=str(project_path)))
        project = await service.refresh_project_codegraph(project.id)

        summary = ContextBuilder._codegraph_summary(project.codegraph)
        rendered = len(str(summary))

        assert summary["file_count"] >= 41
        # Orientation material: must stay small enough to ride in every prompt.
        assert rendered <= 4000
        assert summary["hot_files"]
        assert all(item["symbols"] > 0 for item in summary["hot_files"])
        assert len(summary["entry_modules"]) <= 12
    finally:
        await session.close()
        await database.close()


def test_the_planner_summary_is_not_duplicated_in_the_context():
    builder = ContextBuilder.__new__(ContextBuilder)
    summary = builder._codegraph_summary(
        {
            "root": "/tmp/tree",
            "file_count": 2,
            "key_files": ["a.py"],
            "graph": {
                "nodes": [
                    {"id": "a.py", "kind": "module", "file": "a.py"},
                    {"id": "a.py:1:f", "kind": "symbol", "file": "a.py", "name": "f"},
                ],
                "edges": [],
            },
        }
    )

    assert summary["module_count"] == 1
    assert summary["symbol_count"] == 1
    assert summary["hot_files"] == [{"file": "a.py", "symbols": 1}]
    assert summary["truncated"] is False
    assert summary["partial"] is None
