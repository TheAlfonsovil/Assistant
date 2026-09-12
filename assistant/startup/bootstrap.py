"""Build and close the complete Assistant application context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assistant.application import TaskService
from assistant.config import Settings, get_settings
from assistant.devices.registry import build_tool_registry
from assistant.infrastructure.db import Database
from assistant.llm import MockLLMProvider, OllamaLLMProvider
from assistant.startup.manager import StartupManager
from assistant.startup.models import StartupReport


@dataclass
class AssistantContext:
    """Ready application dependencies plus the startup report that produced them."""

    database: Database
    session: Any
    service: TaskService
    startup: StartupReport

    async def close(self) -> None:
        await self.session.close()
        if hasattr(self.service.llm, "close"):
            await self.service.llm.close()
        await self.database.close()


async def create_context(
    *, use_mock: bool = False, event_sink=None, trace_sink=None, settings: Settings | None = None
) -> AssistantContext:
    """Load dependencies, check the LLM, recover state, then expose the service."""
    resolved_settings = settings or get_settings()
    database = Database(resolved_settings.database_url)
    provider = (
        MockLLMProvider()
        if use_mock
        else OllamaLLMProvider(
            resolved_settings.ollama_url,
            resolved_settings.ollama_model,
            resolved_settings.ollama_timeout,
            temperature=resolved_settings.ollama_temperature,
            num_ctx=resolved_settings.ollama_num_ctx,
            trace_sink=trace_sink,
            failure_threshold=resolved_settings.ollama_failure_threshold,
            recovery_timeout=resolved_settings.ollama_recovery_timeout,
            max_prompt_chars=resolved_settings.ollama_max_prompt_chars,
            max_response_chars=resolved_settings.ollama_max_response_chars,
        )
    )
    startup = await StartupManager(database, resolved_settings, provider).initialize()
    session = database.sessions()
    service = TaskService(
        session,
        provider,
        build_tool_registry(),
        event_sink=event_sink,
        workspace_root=resolved_settings.workspace_root,
    )
    return AssistantContext(database, session, service, startup)
