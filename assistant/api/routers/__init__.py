"""API routers grouped by domain."""

from .chat import router as chat_router
from .dashboard import router as dashboard_router
from .memory import router as memory_router
from .projects import router as projects_router
from .runtime import router as runtime_router
from .series import router as series_router
from .system import router as system_router
from .tasks import router as tasks_router

__all__ = [
    "chat_router",
    "dashboard_router",
    "memory_router",
    "projects_router",
    "runtime_router",
    "series_router",
    "system_router",
    "tasks_router",
]
