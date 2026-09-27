import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from httpx import HTTPError

from ..idle import IdleCycle
from ..runtime import TaskRuntime
from ..startup.bootstrap import create_context
from .routers import (
    chat_router,
    dashboard_router,
    memory_router,
    projects_router,
    runtime_router,
    series_router,
    system_router,
    tasks_router,
)

# The Vue single-page app is compiled by Vite into ``frontend/dist``.
# Build it with ``npm --prefix frontend run build`` before starting the dashboard.
FRONTEND_ROOT = Path(__file__).resolve().parents[2] / "frontend" / "dist"
API_PREFIX = "/api/v1"


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
        lambda task_id: context.service.run_task(
            task_id, wait_for_retry=False, single_step=True
        ),
        idle_cycle=IdleCycle(
            on_idle=context.service.reconcile_idle,
            supervise=lambda has_work: context.service.reconcile_idle(has_work),
            interval=context.settings.maintenance_interval,
            supervision_interval=context.settings.maintenance_interval,
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


app = FastAPI(title="Assistant Core", version="0.5.5", lifespan=lifespan)


@app.middleware("http")
async def release_request_session(request: Request, call_next):
    try:
        return await call_next(request)
    finally:
        context = getattr(request.app.state, "context", None)
        if context is not None:
            await context.session.remove()


app.include_router(system_router, prefix=API_PREFIX)
app.include_router(dashboard_router, prefix=API_PREFIX)
app.include_router(tasks_router, prefix=API_PREFIX)
app.include_router(projects_router, prefix=API_PREFIX)
app.include_router(memory_router, prefix=API_PREFIX)
app.include_router(runtime_router, prefix=API_PREFIX)
app.include_router(series_router, prefix=API_PREFIX)
app.include_router(chat_router, prefix=API_PREFIX)


BUILD_HINT = (
    "Frontend build not found. Build the Vue dashboard with:\n"
    "  npm --prefix frontend install\n"
    "  npm --prefix frontend run build\n"
    f"then reload this page. Expected output at: {FRONTEND_ROOT}"
)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/dashboard")


@app.get("/dashboard", include_in_schema=False)
@app.get("/dashboard/{asset_path:path}", include_in_schema=False)
async def dashboard_spa(request: Request, asset_path: str = ""):
    """Serve the compiled Vue SPA with history-mode fallback to index.html."""
    index = FRONTEND_ROOT / "index.html"
    if not index.is_file():
        return PlainTextResponse(BUILD_HINT, status_code=503)
    if asset_path:
        candidate = (FRONTEND_ROOT / asset_path).resolve()
        if candidate.is_file() and candidate.is_relative_to(FRONTEND_ROOT):
            return FileResponse(candidate)
    return FileResponse(index)
