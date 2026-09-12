import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .domain.models import (
    Project,
    ProjectRequest,
    TaskInputRequest,
    TaskRedefinitionRequest,
    TaskRequest,
)
from .idle import IdleCycle
from .runtime import TaskRuntime
from .startup.bootstrap import AssistantContext, create_context


@asynccontextmanager
async def lifespan(app: FastAPI):
    context = await create_context()
    app.state.context = context
    runtime = TaskRuntime(
        context.service.repository,
        lambda task_id: context.service.run_task(task_id, wait_for_retry=False),
        idle_cycle=IdleCycle(
            on_idle=context.service.reconcile_idle,
            supervise=lambda has_work: context.service.reconcile_idle(),
        ),
        is_ready=lambda: context.startup.llm_ready,
    )
    worker = asyncio.create_task(runtime.run_forever(), name="assistant-task-runtime")
    app.state.runtime = runtime
    app.state.worker = worker
    try:
        yield
    finally:
        runtime.stop()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await context.close()


app = FastAPI(title="Assistant Core", version="0.1.0", lifespan=lifespan)
dashboard_root = Path(__file__).resolve().parent.parent / "dashboard"


def service(request: Request) -> AssistantContext:
    return request.app.state.context


@app.get("/health")
async def health(request: Request):
    startup = service(request).startup
    runtime = request.app.state.runtime
    return {
        "status": startup.status,
        "llm_ready": startup.llm_ready,
        "first_initialization": startup.first_initialization,
        "loaded_memories": len(startup.loaded_memories),
        "unfinished_tasks": startup.unfinished_tasks,
        "runtime": {
            "active_tasks": runtime.last_active_count,
            "last_started_at": runtime.last_started_at,
            "last_completed_at": runtime.last_completed_at,
            "last_error": runtime.last_error,
            "metrics": runtime.metrics_snapshot(),
        },
    }


@app.get("/dashboard/data")
async def dashboard_data(request: Request):
    context = service(request)
    repository = context.service.repository
    tasks = await repository.list_tasks()
    projects = await context.service.list_projects()
    memories = await repository.list_memory()
    task_nodes: dict[str, list] = {}
    task_edges: dict[str, list] = {}
    task_events: dict[str, list] = {}
    for task in tasks:
        task_nodes[task.id] = await repository.list_nodes(task.id)
        task_edges[task.id] = await repository.list_edges(task.id)
        task_events[task.id] = await repository.list_events(task.id)

    status_counts: dict[str, int] = {}
    node_status_counts: dict[str, int] = {}
    events = []
    for task in tasks:
        status_counts[task.status.value] = status_counts.get(task.status.value, 0) + 1
        for node in task_nodes[task.id]:
            node_status_counts[node.status.value] = node_status_counts.get(node.status.value, 0) + 1
        events.extend(task_events[task.id])
    events.sort(key=lambda event: event.created_at, reverse=True)
    startup = context.startup
    runtime = request.app.state.runtime
    return {
        "health": {
            "status": startup.status,
            "llm_ready": startup.llm_ready,
            "database_ready": startup.database_ready,
            "unfinished_tasks": startup.unfinished_tasks,
            "recovered_nodes": startup.recovered_nodes,
            "loaded_memories": len(startup.loaded_memories),
            "runtime": {
                "active_tasks": runtime.last_active_count,
                "last_started_at": runtime.last_started_at,
                "last_completed_at": runtime.last_completed_at,
                "last_error": runtime.last_error,
                "metrics": runtime.metrics_snapshot(),
            },
        },
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "task_nodes": {
            task_id: [node.model_dump(mode="json") for node in nodes]
            for task_id, nodes in task_nodes.items()
        },
        "task_edges": {
            task_id: [edge.model_dump(mode="json") for edge in edges]
            for task_id, edges in task_edges.items()
        },
        "events": [event.model_dump(mode="json") for event in events[:200]],
        "status_counts": status_counts,
        "node_status_counts": node_status_counts,
        "projects": [project.model_dump(mode="json") for project in projects],
        "memories": [memory.model_dump(mode="json") for memory in memories],
    }


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    return FileResponse(dashboard_root / "index.html")


app.mount("/dashboard", StaticFiles(directory=dashboard_root), name="dashboard-assets")


@app.get("/memory")
async def list_memory(request: Request):
    return [
        memory.model_dump(mode="json")
        for memory in await service(request).service.repository.list_memory()
    ]


@app.get("/memory/export")
async def export_memory(request: Request):
    return await service(request).service.repository.export_memory()


@app.post("/memory/{memory_id}/redact")
async def redact_memory(request: Request, memory_id: str):
    memory = await service(request).service.repository.redact_memory(memory_id)
    if memory is None:
        raise HTTPException(404, "Memory not found")
    return memory.model_dump(mode="json")


@app.delete("/memory/{memory_id}")
async def delete_memory(request: Request, memory_id: str):
    if not await service(request).service.repository.delete_memory(memory_id):
        raise HTTPException(404, "Memory not found")
    return {"deleted": True, "id": memory_id}


@app.post("/memory/purge-expired")
async def purge_expired_memory(request: Request):
    count = await service(request).service.repository.purge_expired_memory()
    return {"purged": count}


@app.post("/tasks")
async def create_task(request: Request, task_request: TaskRequest):
    try:
        task = await service(request).service.create_task(task_request)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"id": task.id, "status": task.status}


@app.post("/projects")
async def create_project(request: Request, project_request: ProjectRequest):
    project = await service(request).service.create_project(Project.model_validate(project_request.model_dump()))
    return project.model_dump(mode="json")


@app.get("/projects")
async def list_projects(request: Request):
    return [project.model_dump(mode="json") for project in await service(request).service.list_projects()]


@app.get("/projects/{project_id}")
async def get_project(request: Request, project_id: str):
    project = await service(request).service.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project.model_dump(mode="json")


@app.put("/projects/{project_id}")
async def update_project(request: Request, project_id: str, project_request: ProjectRequest):
    project = Project(id=project_id, **project_request.model_dump())
    try:
        return (await service(request).service.update_project(project)).model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "Project not found") from None


@app.delete("/projects/{project_id}")
async def delete_project(request: Request, project_id: str):
    if not await service(request).service.delete_project(project_id):
        raise HTTPException(404, "Project not found")
    return {"deleted": True, "id": project_id}


@app.post("/projects/{project_id}/audit")
async def audit_project(request: Request, project_id: str):
    task = await service(request).service.create_project_audit_task(project_id)
    if not task:
        raise HTTPException(404, "Project not found or disabled")
    return {"id": task.id, "status": task.status, "project_id": task.project_id}


@app.post("/projects/{project_id}/codegraph/refresh")
async def refresh_project_codegraph(request: Request, project_id: str):
    try:
        project = await service(request).service.refresh_project_codegraph(project_id)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    if not project:
        raise HTTPException(404, "Project not found")
    return project.model_dump(mode="json")


@app.get("/tasks")
async def list_tasks(request: Request):
    context = service(request)
    return [task.model_dump(mode="json") for task in await context.service.repository.list_tasks()]


@app.get("/tasks/{task_id}")
async def get_task(request: Request, task_id: str):
    task = await service(request).service.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task.model_dump(mode="json")


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(request: Request, task_id: str):
    task = await service(request).service.cancel_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task.model_dump(mode="json")


@app.post("/tasks/{task_id}/resume")
async def resume_task(request: Request, task_id: str):
    try:
        task = await service(request).service.resume_task(task_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if not task:
        raise HTTPException(404, "Task not found")
    return task.model_dump(mode="json")


@app.post("/tasks/{task_id}/redefine")
async def redefine_task(
    request: Request, task_id: str, task_request: TaskRedefinitionRequest
):
    try:
        task = await service(request).service.redefine_task(
            task_id,
            task_request.goal,
            task_request.description,
            task_request.metadata,
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if not task:
        raise HTTPException(404, "Task not found")
    return task.model_dump(mode="json")


@app.delete("/tasks/{task_id}")
async def delete_task(request: Request, task_id: str):
    try:
        deleted = await service(request).service.delete_task(task_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if not deleted:
        raise HTTPException(404, "Task not found")
    return {"deleted": True, "id": task_id}


@app.post("/tasks/{task_id}/input")
async def submit_task_input(request: Request, task_id: str, task_input: TaskInputRequest):
    try:
        task = await service(request).service.submit_task_input(
            task_id, task_input.input, task_input.node_id
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if not task:
        raise HTTPException(404, "Task not found")
    return task.model_dump(mode="json")


@app.post("/tasks/{task_id}/nodes/{node_id}/approval")
async def approve_action(request: Request, task_id: str, node_id: str, body: dict[str, bool]):
    try:
        task = await service(request).service.approve_action(
            task_id, node_id, bool(body.get("approved", False))
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if not task:
        raise HTTPException(404, "Task or node not found")
    return task.model_dump(mode="json")


@app.get("/tasks/{task_id}/graph")
async def get_graph(request: Request, task_id: str):
    graph = await service(request).service.graph(task_id)
    return {
        "nodes": [node.model_dump(mode="json") for node in graph.nodes.values()],
        "edges": [edge.model_dump(mode="json") for edge in graph.edges],
    }


@app.get("/tasks/{task_id}/events")
async def get_events(request: Request, task_id: str):
    context = service(request)
    return [
        event.model_dump(mode="json")
        for event in await context.service.repository.list_events(task_id)
    ]
