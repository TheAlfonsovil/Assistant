from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...domain.models import ChatFastRequest, ChatRequest, TaskRequest, TaskStatus
from ..deps import get_context, get_runtime
from ..serializers import _task_json

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("")
async def create_chat_message(request: Request, chat_request: ChatRequest) -> dict:
    task = await get_context(request).service.create_task(
        TaskRequest(
            title=chat_request.title,
            goal=chat_request.message,
            description=chat_request.description,
            source="DASHBOARD_CHAT",
            project_id=chat_request.project_id,
            target_type=chat_request.target_type,
            target_id=chat_request.target_id,
            attachments=chat_request.attachments,
            metadata={"interaction": "chat", "requested_format": "answer"},
        )
    )
    get_runtime(request).wake()
    return task.model_dump(mode="json")


@router.post("/agent")
async def chat_agent_command(request: Request, chat_request: ChatRequest) -> dict:
    """Execute explicit queue operations from dashboard agent mode."""
    context = get_context(request)
    runtime = get_runtime(request)
    message = chat_request.message.strip()
    normalized = message.casefold().lstrip("/")
    command, _, argument = normalized.partition(" ")
    argument = argument.strip()
    if command in {"quiero", "necesito", "puedes", "haz", "hazme"}:
        for keyword in ("crear ", "crea ", "ejecuta ", "lanza "):
            if normalized.startswith(keyword):
                command, argument = "crear", message[len(keyword):].strip()
                break
    if command in {"cancela", "cancelar", "detén", "detener", "para"}:
        command = "cancelar"
    if command in {"elimina", "eliminar", "borra", "borrar"}:
        command = "borrar"
    if command in {"reanuda", "reanudar", "continua", "continuar"}:
        command = "reanudar"
    if command in {"replanifica", "replanificar", "reintenta"}:
        command = "replanificar"

    if command in {"listar", "lista", "list"}:
        tasks = await context.service.repository.list_tasks()
        active = [
            task
            for task in tasks
            if task.status
            not in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        ]
        return {
            "action": "list",
            "message": f"{len(active)} tareas activas de {len(tasks)} persistidas",
            "tasks": [_task_json(task) for task in tasks],
        }

    if command in {"crear", "create"}:
        if not argument:
            raise HTTPException(422, "Indica el objetivo después de 'crear'.")
        task = await context.service.create_task(
            TaskRequest(
                goal=argument,
                source="DASHBOARD_AGENT",
                project_id=chat_request.project_id,
                target_type=chat_request.target_type,
                target_id=chat_request.target_id,
                metadata={"interaction": "chat", "mode": "agent"},
            )
        )
        runtime.wake()
        return {
            "action": "create",
            "message": f"Tarea creada ({task.id[:8]})",
            "task": _task_json(task),
        }

    operations = {
        "cancelar": "cancel", "cancel": "cancel",
        "borrar": "delete", "delete": "delete",
        "reanudar": "resume", "resume": "resume",
        "replanificar": "replan", "replan": "replan",
    }
    action = operations.get(command)
    if action:
        if not argument:
            raise HTTPException(422, f"Indica el id después de '{command}'.")
        task = await context.service.get_task(argument)
        if task is None:
            raise HTTPException(404, "No se encontró esa tarea. Usa el id completo.")
        if action in {"cancel", "delete"} and not chat_request.confirm:
            verb = "cancelar" if action == "cancel" else "borrar"
            return {
                "action": "confirmation_required",
                "message": f"Voy a {verb} la tarea {task.id[:8]} ({task.goal}). Confirma la operación.",
                "task": _task_json(task),
            }
        try:
            if action == "cancel":
                task = await context.service.cancel_task(task.id)
            elif action == "delete":
                deleted = await context.service.delete_task(task.id)
                return {"action": action, "message": f"Tarea {task.id[:8]} borrada", "deleted": deleted}
            elif action == "resume":
                task = await context.service.resume_task(task.id)
            else:
                task = await context.service.replan_task(task.id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        if action == "cancel":
            runtime.wake()
        return {
            "action": action,
            "message": f"Tarea {task.id[:8]} actualizada: {task.status.value}",
            "task": _task_json(task),
        }

    raise HTTPException(
        422, "Comando no reconocido. Usa crear, cancelar, borrar, reanudar, replanificar o listar."
    )



async def _chat_fast_stream(request: Request, chat_request: ChatFastRequest):
    """Stream task progress while the persistent runtime owns execution."""
    try:
        task = await get_context(request).service.create_task(
            TaskRequest(
                title=chat_request.title,
                goal=chat_request.message,
                description=chat_request.description,
                source="DASHBOARD_CHAT_FAST",
                project_id=chat_request.project_id,
                target_type=chat_request.target_type,
                target_id=chat_request.target_id,
                attachments=chat_request.attachments,
                metadata={"interaction": "chat", "requested_format": "answer"},
            )
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    get_runtime(request).wake()

    async def events():
        terminal = {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"}
        last_status = None
        accepted = {"task_id": task.id, "status": task.status}
        yield f"event: accepted\ndata: {json.dumps(accepted, default=str)}\n\n"
        while True:
            if await request.is_disconnected():
                return
            current = await get_context(request).service.get_task(task.id)
            if current is None:
                yield 'event: error\ndata: {"message":"Task disappeared while streaming"}\n\n'
                return
            status = current.status.value
            if status != last_status:
                last_status = status
                payload = {"task_id": current.id, "status": status}
                if current.failure_reason:
                    payload["error"] = current.failure_reason
                yield f"event: status\ndata: {json.dumps(payload, default=str)}\n\n"
            if status in terminal:
                result = current.runtime.final_response or current.result_summary
                if result:
                    body = {"task_id": current.id, "status": status, "result": result}
                    yield f"event: result\ndata: {json.dumps(body, default=str)}\n\n"
                done = {"task_id": current.id, "status": status}
                yield f"event: done\ndata: {json.dumps(done, default=str)}\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/fast")
async def chat_fast(request: Request, chat_request: ChatFastRequest):
    return await _chat_fast_stream(request, chat_request)
