from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from .application import TaskService
from .config import get_settings
from .domain.models import TaskRequest
from .infrastructure.db import Database
from .llm import OllamaLLMProvider
from .tools import ToolRegistry

settings = get_settings()
database = Database(settings.database_url)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.create_all()
    yield
    await database.close()


app = FastAPI(title="Assistant Core", version="0.1.0", lifespan=lifespan)


async def service():
    async with database.sessions() as session:
        provider = OllamaLLMProvider(settings.ollama_url, settings.ollama_model)
        yield TaskService(session, provider, ToolRegistry())
        await provider.close()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/tasks")
async def create_task(request: TaskRequest):
    async for item in service():
        task = await item.create_task(request)
        return {"id": task.id, "status": task.status}


@app.get("/tasks")
async def list_tasks():
    async for item in service():
        return [task.model_dump(mode="json") for task in await item.repository.list_tasks()]


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    async for item in service():
        task = await item.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        return task.model_dump(mode="json")


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    async for item in service():
        task = await item.cancel_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        return task.model_dump(mode="json")


@app.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str):
    async for item in service():
        task = await item.resume_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        return task.model_dump(mode="json")


@app.get("/tasks/{task_id}/graph")
async def get_graph(task_id: str):
    async for item in service():
        graph = await item.graph(task_id)
        return {
            "nodes": [node.model_dump(mode="json") for node in graph.nodes.values()],
            "edges": [edge.model_dump(mode="json") for edge in graph.edges],
        }


@app.get("/tasks/{task_id}/events")
async def get_events(task_id: str):
    async for item in service():
        return [
            event.model_dump(mode="json") for event in await item.repository.list_events(task_id)
        ]
