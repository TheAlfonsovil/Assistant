from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from .domain.models import TaskRequest
from .startup.bootstrap import AssistantContext, create_context


@asynccontextmanager
async def lifespan(app: FastAPI):
    context = await create_context()
    app.state.context = context
    try:
        yield
    finally:
        await context.close()


app = FastAPI(title="Assistant Core", version="0.1.0", lifespan=lifespan)


def service(request: Request) -> AssistantContext:
    return request.app.state.context


@app.get("/health")
async def health(request: Request):
    startup = service(request).startup
    return {
        "status": startup.status,
        "llm_ready": startup.llm_ready,
        "first_initialization": startup.first_initialization,
        "loaded_memories": len(startup.loaded_memories),
        "unfinished_tasks": startup.unfinished_tasks,
    }


@app.post("/tasks")
async def create_task(request: Request, task_request: TaskRequest):
    task = await service(request).service.create_task(task_request)
    return {"id": task.id, "status": task.status}


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
    task = await service(request).service.resume_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
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
