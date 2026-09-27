from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ...domain.models import (
    TaskInputRequest,
    TaskRedefinitionRequest,
    TaskRequest,
)
from ..deps import get_context, get_runtime
from ..serializers import _event_json, _task_json

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("")
async def list_tasks(request: Request, limit: int = 100, status: str | None = None) -> dict:
    tasks = await get_context(request).service.dashboard_tasks(limit=limit, status=status)
    return {"tasks": [_task_json(task) for task in tasks], "limit": max(1, min(limit, 500))}


@router.post("")
async def create_task(request: Request, task_request: TaskRequest) -> dict:
    try:
        task = await get_context(request).service.create_task(task_request)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    get_runtime(request).wake()
    return {"id": task.id, "status": task.status}


@router.get("/{task_id}")
async def get_task(request: Request, task_id: str) -> dict:
    detail = await get_context(request).service.dashboard_task_detail(task_id)
    if detail is None:
        raise HTTPException(404, "Task not found")
    return {
        "task": _task_json(detail["task"]),
        "nodes": [node.model_dump(mode="json") for node in detail["nodes"]],
        "edges": [edge.model_dump(mode="json") for edge in detail["edges"]],
        "events": [_event_json(event, include_prompt=True) for event in detail["events"]],
    }


@router.post("/{task_id}/cancel")
async def cancel_task(request: Request, task_id: str) -> dict:
    task = await get_context(request).service.cancel_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    get_runtime(request).wake()
    return task.model_dump(mode="json")


@router.post("/{task_id}/resume")
async def resume_task(request: Request, task_id: str) -> dict:
    try:
        task = await get_context(request).service.resume_task(task_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if not task:
        raise HTTPException(404, "Task not found")
    get_runtime(request).wake()
    return task.model_dump(mode="json")


@router.post("/{task_id}/redefine")
async def redefine_task(
    request: Request, task_id: str, task_request: TaskRedefinitionRequest
) -> dict:
    try:
        task = await get_context(request).service.redefine_task(
            task_id, task_request.goal, task_request.description, task_request.metadata
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if not task:
        raise HTTPException(404, "Task not found")
    get_runtime(request).wake()
    return task.model_dump(mode="json")


@router.post("/{task_id}/replan")
async def replan_task(request: Request, task_id: str) -> dict:
    try:
        task = await get_context(request).service.replan_task(task_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if not task:
        raise HTTPException(404, "Task not found")
    get_runtime(request).wake()
    return task.model_dump(mode="json")


@router.delete("/{task_id}")
async def delete_task(request: Request, task_id: str) -> dict:
    try:
        deleted = await get_context(request).service.delete_task(task_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if not deleted:
        raise HTTPException(404, "Task not found")
    return {"deleted": True, "id": task_id}


@router.post("/{task_id}/input")
async def submit_task_input(request: Request, task_id: str, task_input: TaskInputRequest) -> dict:
    try:
        task = await get_context(request).service.submit_task_input(
            task_id, task_input.input, task_input.node_id
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if not task:
        raise HTTPException(404, "Task not found")
    get_runtime(request).wake()
    return task.model_dump(mode="json")


@router.post("/{task_id}/nodes/{node_id}/approval")
async def approve_action(request: Request, task_id: str, node_id: str, body: dict[str, bool]) -> dict:
    try:
        task = await get_context(request).service.approve_action(
            task_id, node_id, bool(body.get("approved", False))
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if not task:
        raise HTTPException(404, "Task or node not found")
    get_runtime(request).wake()
    return task.model_dump(mode="json")


@router.get("/{task_id}/graph")
async def get_graph(request: Request, task_id: str) -> dict:
    graph = await get_context(request).service.graph(task_id)
    return {
        "nodes": [node.model_dump(mode="json") for node in graph.nodes.values()],
        "edges": [edge.model_dump(mode="json") for edge in graph.edges],
    }


@router.get("/{task_id}/nodes/{node_id}")
async def get_task_node(request: Request, task_id: str, node_id: str) -> dict:
    """Detail for a single node of a task (used by observability drill-down)."""
    repository = get_context(request).service.repository
    node = await repository.get_node(node_id)
    if node is None or node.task_id != task_id:
        raise HTTPException(404, "Node not found")
    return node.model_dump(mode="json")


@router.get("/{task_id}/events")
async def get_events(request: Request, task_id: str) -> list:
    events = await get_context(request).service.repository.list_events(task_id)
    return [_event_json(event) for event in events]
