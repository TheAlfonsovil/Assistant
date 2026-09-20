import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from httpx import HTTPError

from .devices.registry import DEVICE_BRANCHES
from .domain.models import (
    ChatRequest,
    IdleConfigurationRequest,
    Operation,
    Project,
    ProjectRequest,
    TaskInputRequest,
    TaskRedefinitionRequest,
    TaskRequest,
)
from .idle import IdleCycle
from .llm import AssistantResponse, NodeDecision, PlanProposal
from .prompts.v1.template import render
from .runtime import TaskRuntime
from .startup.bootstrap import AssistantContext, create_context


def _event_json(event) -> dict:
    payload = event.payload or {}
    if event.event_type == "LLM_REQUEST" and payload.get("role"):
        role = str(payload["role"])
        request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
        if not request.get("rendered_instructions"):
            context = payload.get("context")
            prompt_path = Path(__file__).parent / "prompts" / "v1" / f"{role.lower()}.md"
            schemas = {
                "PLANNER": PlanProposal,
                "NODE_RESOLVER": NodeDecision,
                "FINAL_RESPONSE": AssistantResponse,
            }
            schema = schemas.get(role)
            if isinstance(context, dict) and schema is not None and prompt_path.is_file():
                instructions = prompt_path.read_text(encoding="utf-8")
                rendered = render(instructions, context, schema.model_json_schema())
                request = {
                    "role": role,
                    "prompt": {
                        "role": role,
                        "instructions": rendered,
                    },
                    "rendered_instructions": rendered,
                }
                request["prompt_chars"] = len(request["rendered_instructions"])
        if request:
            payload = {**payload, "request": request}
            payload.pop("context", None)
    return {
        **event.model_dump(mode="json"),
        "payload": payload,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    context = await create_context()
    app.state.context = context

    async def check_llm_ready() -> bool:
        try:
            return await asyncio.wait_for(context.service.llm.check_ready(), timeout=5.0)
        except (HTTPError, OSError, TimeoutError, ValueError):
            return False

    runtime = TaskRuntime(
        context.service.repository,
        lambda task_id: context.service.run_task(task_id, wait_for_retry=False),
        idle_cycle=IdleCycle(
            on_idle=context.service.reconcile_idle,
            supervise=lambda has_work: context.service.reconcile_idle(),
            enabled=context.settings.idle_enabled,
        ),
        is_ready=check_llm_ready,
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


app = FastAPI(title="Assistant Core", version="0.1.16", lifespan=lifespan)
dashboard_root = Path(__file__).resolve().parent.parent / "dashboard"


def service(request: Request) -> AssistantContext:
    return request.app.state.context


def _dashboard_analytics(context, tasks, task_nodes, events):
    model = getattr(context.service.llm, "model", "configured-provider")
    status_counts = {}
    tool_counts = {}
    phase_counts = {}
    estimated_prompt_tokens = 0
    estimated_response_tokens = 0
    actual_prompt_tokens = 0
    actual_response_tokens = 0
    actual_usage_events = 0
    llm_response_events = 0
    prefill_seconds = 0.0
    generation_seconds = 0.0
    measured_generation_tokens = 0
    llm_latency = []
    tool_latency = []
    retries = 0
    llm_requests = {
        (event.task_id, event.node_id, (event.payload or {}).get("role", "unknown")): event.created_at
        for event in events
        if event.event_type == "LLM_REQUEST"
    }
    for event in events:
        payload = event.payload or {}
        status_counts[event.event_type] = status_counts.get(event.event_type, 0) + 1
        if event.event_type == "TOOL_RESULT":
            tool = payload.get("tool", "unknown")
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
            if isinstance(payload.get("duration"), (int, float)):
                tool_latency.append(payload["duration"])
        if event.event_type in {"LLM_REQUEST", "LLM_RESPONSE"}:
            phase = payload.get("role", "unknown")
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
        if event.event_type == "LLM_REQUEST":
            chars = payload.get("context_chars", 0)
            estimated_prompt_tokens += int(chars / 4) if isinstance(chars, (int, float)) else 0
        if event.event_type == "LLM_RESPONSE":
            llm_response_events += 1
            chars = payload.get("response_chars", 0)
            estimated_response_tokens += int(chars / 4) if isinstance(chars, (int, float)) else 0
            usage = payload.get("usage", {})
            actual_prompt_tokens += int(usage.get("prompt_eval_count", 0) or 0)
            actual_response_tokens += int(usage.get("eval_count", 0) or 0)
            if usage.get("prompt_eval_count") or usage.get("eval_count"):
                actual_usage_events += 1
            prefill_seconds += float(usage.get("prompt_eval_duration", 0) or 0) / 1_000_000_000
            generation_seconds += float(usage.get("eval_duration", 0) or 0) / 1_000_000_000
            measured_generation_tokens += int(usage.get("eval_count", 0) or 0)
            request_key = (event.task_id, event.node_id, payload.get("role", "unknown"))
            requested_at = llm_requests.get(request_key)
            if requested_at is not None:
                llm_latency.append(max(0, (event.created_at - requested_at).total_seconds()))
        if event.event_type in {"LLM_RESPONSE", "LLM_RESPONSE_PARSED"} and isinstance(
            payload.get("elapsed_seconds"), (int, float)
        ):
            llm_latency.append(payload["elapsed_seconds"])
        if event.event_type == "RETRY_SCHEDULED":
            retries += 1
    durations = []
    task_usage = []
    node_usage = {}
    for task in tasks:
        task_task_events = [event for event in events if event.task_id == task.id]
        task_prompt = sum(
            int((event.payload or {}).get("context_chars", 0) / 4)
            for event in task_task_events
            if event.event_type == "LLM_REQUEST"
        )
        task_response = sum(
            int((event.payload or {}).get("response_chars", 0) / 4)
            for event in task_task_events
            if event.event_type == "LLM_RESPONSE"
        )
        task_actual_prompt = sum(
            int(((event.payload or {}).get("usage") or {}).get("prompt_eval_count", 0) or 0)
            for event in task_task_events
            if event.event_type == "LLM_RESPONSE"
        )
        task_actual_response = sum(
            int(((event.payload or {}).get("usage") or {}).get("eval_count", 0) or 0)
            for event in task_task_events
            if event.event_type == "LLM_RESPONSE"
        )
        task_actual_available = bool(task_actual_prompt or task_actual_response)
        task_measured_total = task_actual_prompt + task_actual_response
        for event in task_task_events:
            if not event.node_id:
                continue
            usage = node_usage.setdefault(
                event.node_id,
                {
                    "llm_calls": 0,
                    "tool_calls": 0,
                    "estimated_tokens": 0,
                    "actual_tokens": 0,
                    "actual_tokens_available": False,
                },
            )
            payload = event.payload or {}
            if event.event_type == "LLM_REQUEST":
                usage["llm_calls"] += 1
                usage["estimated_tokens"] += int(payload.get("context_chars", 0) / 4)
            elif event.event_type == "LLM_RESPONSE":
                usage["estimated_tokens"] += int(payload.get("response_chars", 0) / 4)
                provider_usage = payload.get("usage") or {}
                measured = int(provider_usage.get("prompt_eval_count", 0) or 0) + int(
                    provider_usage.get("eval_count", 0) or 0
                )
                usage["actual_tokens"] += measured
                usage["actual_tokens_available"] = bool(measured)
            elif event.event_type == "TOOL_CALLED":
                usage["tool_calls"] += 1
        task_usage.append({
            "id": task.id,
            "goal": task.goal,
            "status": task.status.value,
            "nodes": len(task_nodes.get(task.id, [])),
            "llm_calls": sum(1 for event in task_task_events if event.event_type == "LLM_REQUEST"),
            "tool_calls": sum(1 for event in task_task_events if event.event_type == "TOOL_CALLED"),
            "estimated_tokens": task_prompt + task_response,
            "actual_tokens": task_measured_total,
            "actual_tokens_available": task_actual_available,
            "token_source": "ollama" if task_actual_available else "estimated_chars_divided_by_4",
            "duration_seconds": round(
                max(0, (task.finished_at - task.started_at).total_seconds())
                if task.started_at and task.finished_at else 0,
                2,
            ),
        })
        if task.started_at and task.finished_at:
            durations.append(max(0, (task.finished_at - task.started_at).total_seconds()))
    completed = sum(1 for task in tasks if task.status.value == "SUCCEEDED")
    terminal = sum(
        1 for task in tasks
        if task.status.value in {"SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"}
    )
    return {
        "model": model,
        "event_counts": status_counts,
        "tool_counts": tool_counts,
        "phase_counts": phase_counts,
        "estimated_tokens": {
            "prompt": estimated_prompt_tokens,
            "response": estimated_response_tokens,
            "total": estimated_prompt_tokens + estimated_response_tokens,
            "basis": "context/response characters divided by 4",
        },
        "actual_tokens": {
            "prompt": actual_prompt_tokens,
            "response": actual_response_tokens,
            "total": actual_prompt_tokens + actual_response_tokens,
            "available": bool(actual_prompt_tokens or actual_response_tokens),
            "basis": "Ollama prompt_eval_count/eval_count when provided",
        },
        "display_tokens": {
            "prompt": actual_prompt_tokens if actual_prompt_tokens else estimated_prompt_tokens,
            "response": actual_response_tokens if actual_response_tokens else estimated_response_tokens,
            "total": (
                actual_prompt_tokens + actual_response_tokens
                if actual_prompt_tokens or actual_response_tokens
                else estimated_prompt_tokens + estimated_response_tokens
            ),
            "source": (
                "ollama"
                if actual_usage_events == llm_response_events and actual_usage_events
                else "ollama_partial"
                if actual_usage_events
                else "estimated_chars_divided_by_4"
            ),
            "measured_responses": actual_usage_events,
            "response_events": llm_response_events,
        },
        "latency": {
            "average_task_seconds": round(sum(durations) / len(durations), 2) if durations else 0,
            "average_llm_seconds": round(sum(llm_latency) / len(llm_latency), 2) if llm_latency else 0,
            "average_tool_seconds": round(sum(tool_latency) / len(tool_latency), 2) if tool_latency else 0,
            "prefill_seconds": round(prefill_seconds, 2),
            "generation_seconds": round(generation_seconds, 2),
            "generation_tokens_per_second": round(
                measured_generation_tokens / generation_seconds, 2
            ) if generation_seconds else 0,
        },
        "throughput": {
            "completed_tasks": completed,
            "terminal_tasks": terminal,
            "success_rate": round(completed / terminal * 100, 1) if terminal else 0,
            "retries": retries,
        },
        "task_durations": [
            {"id": task.id, "goal": task.goal, "seconds": round(duration, 2)}
            for task, duration in zip(
                [task for task in tasks if task.started_at and task.finished_at], durations
            )
        ][:20],
        "task_usage": task_usage[:50],
        "node_usage": node_usage,
    }


def _dashboard_devices(context) -> list[dict[str, object]]:
    definitions = context.service.tools.definitions()
    device_tools = {"device.mobile", "device.home", "device.robot"}
    computer_capabilities = sorted(
        definition.name for definition in definitions if definition.name not in device_tools
    )
    devices = []
    for branch in DEVICE_BRANCHES:
        capabilities = (
            computer_capabilities
            if branch.name == "computer"
            else [f"device.{branch.name}"] if f"device.{branch.name}" in device_tools else []
        )
        devices.append(
            {
                "name": branch.name,
                "status": branch.status,
                "description": branch.description,
                "platform": branch.platform,
                "transport": branch.transport,
                "capabilities": capabilities,
            }
        )
    return devices


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
            "idle": runtime.idle_snapshot(),
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
    analytics = _dashboard_analytics(context, tasks, task_nodes, events)
    startup = context.startup
    runtime = request.app.state.runtime
    system_result = await context.service.tools.execute(Operation(tool="system", method="info"))
    return {
        "health": {
            "status": startup.status,
            "llm_ready": runtime.readiness_snapshot()
            if runtime.readiness_snapshot() is not None
            else startup.llm_ready,
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
                "llm_ready": runtime.readiness_snapshot(),
                "idle": runtime.idle_snapshot(),
            },
        },
        "tasks": [
            {
                **task.model_dump(mode="json"),
                "final_response": task.metadata.get("final_response"),
            }
            for task in tasks
        ],
        "task_nodes": {
            task_id: [node.model_dump(mode="json") for node in nodes]
            for task_id, nodes in task_nodes.items()
        },
        "task_edges": {
            task_id: [edge.model_dump(mode="json") for edge in edges]
            for task_id, edges in task_edges.items()
        },
        "events": [_event_json(event) for event in events[:200]],
        "task_events": {
            task_id: [_event_json(event) for event in task_events[task_id]]
            for task_id in task_events
        },
        "status_counts": status_counts,
        "node_status_counts": node_status_counts,
        "projects": [project.model_dump(mode="json") for project in projects],
        "tools": [definition.model_dump(mode="json") for definition in context.service.tools.definitions()],
        "memories": [memory.model_dump(mode="json") for memory in memories],
        "devices": _dashboard_devices(context),
        "system": system_result.output if system_result.success else {"error": system_result.error},
        "analytics": analytics,
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


@app.post("/memory/reset")
@app.post("/runtime/reset")
async def reset_memory(request: Request):
    runtime = request.app.state.runtime
    worker = request.app.state.worker
    runtime.stop()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass
    deleted = await service(request).service.repository.reset_state()
    runtime.stop_requested = False
    request.app.state.worker = asyncio.create_task(
        runtime.run_forever(), name="assistant-task-runtime"
    )
    return {"reset": True, "deleted": deleted, "total": sum(deleted.values())}


@app.get("/runtime/idle")
async def runtime_idle(request: Request):
    return request.app.state.runtime.idle_snapshot()


@app.put("/runtime/idle")
async def configure_runtime_idle(request: Request, configuration: IdleConfigurationRequest):
    runtime = request.app.state.runtime
    runtime.set_idle_enabled(configuration.enabled)
    return runtime.idle_snapshot()


@app.post("/chat")
async def create_chat_message(request: Request, chat_request: ChatRequest):
    task = await service(request).service.create_task(
        TaskRequest(
            goal=chat_request.message,
            source="DASHBOARD_CHAT",
            project_id=chat_request.project_id,
            target_type=chat_request.target_type,
            target_id=chat_request.target_id,
            metadata={"interaction": "chat", "requested_format": "answer"},
        )
    )
    return task.model_dump(mode="json")


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
async def audit_project(request: Request, project_id: str, run_tests: bool = False):
    task = await service(request).service.create_project_audit_task(project_id, run_tests=run_tests)
    if not task:
        raise HTTPException(404, "Project not found or disabled")
    return {
        "id": task.id,
        "status": task.status,
        "project_id": task.project_id,
        "run_tests": run_tests,
    }


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
        _event_json(event)
        for event in await context.service.repository.list_events(task_id)
    ]
