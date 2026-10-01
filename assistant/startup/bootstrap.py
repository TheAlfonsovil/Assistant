"""Build and close the complete Assistant application context."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import async_scoped_session

from assistant.application import TaskService
from assistant.config import Settings, get_settings
from assistant.devices.registry import build_tool_registry
from assistant.infrastructure.db import Database
from assistant.infrastructure.repositories import TaskRepository
from assistant.llm import DeepSeekLLMProvider, MockLLMProvider
from assistant.startup.manager import StartupManager
from assistant.startup.models import StartupReport


@dataclass
class AssistantContext:
    """Ready application dependencies plus the startup report that produced them."""

    database: Database
    session: Any
    service: TaskService
    startup: StartupReport
    settings: Settings

    async def close(self) -> None:
        await self.session.remove()
        if hasattr(self.service.llm, "close"):
            await self.service.llm.close()
        # Semantic queries run a language-server process: stop it with the app.
        from assistant.devices.computer.lsp import shutdown_language_server

        await shutdown_language_server()
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
        else DeepSeekLLMProvider(
            resolved_settings.deepseek_url,
            resolved_settings.deepseek_model,
            resolved_settings.deepseek_api_key,
            timeout=resolved_settings.deepseek_timeout,
            temperature=resolved_settings.deepseek_temperature,
            thinking=resolved_settings.deepseek_thinking,
            reasoning_policy=resolved_settings.deepseek_reasoning_policy,
            max_tokens=resolved_settings.deepseek_max_tokens,
            trace_sink=trace_sink,
            failure_threshold=resolved_settings.deepseek_failure_threshold,
            recovery_timeout=resolved_settings.deepseek_recovery_timeout,
            max_prompt_chars=resolved_settings.deepseek_max_prompt_chars,
            max_response_chars=resolved_settings.deepseek_max_response_chars,
            supports_vision=resolved_settings.deepseek_supports_vision,
            max_image_bytes=resolved_settings.attachment_max_bytes,
            stream_responses=resolved_settings.deepseek_stream_responses,
            max_vision_images=resolved_settings.vision_max_images,
            vision_detail=resolved_settings.vision_detail,
            workspace_root=resolved_settings.workspace_root,
        )
    )
    startup = await StartupManager(database, resolved_settings, provider).initialize()
    session = async_scoped_session(database.sessions, scopefunc=asyncio.current_task)
    # Built before the tools so task-scoped capabilities (memory, artifacts) can
    # read the same ledger the service writes.
    repository = TaskRepository(session, event_sink=event_sink)
    tools = build_tool_registry(
        policy=resolved_settings.tool_policy(),
        enable_input=resolved_settings.enable_input_control,
        repository=repository,
        rate_limit=resolved_settings.rate_limiter(),
        workspace_root=resolved_settings.workspace_root,
        langserver_path=resolved_settings.pyright_langserver,
    )
    service = TaskService(
        session,
        provider,
        tools,
        event_sink=event_sink,
        workspace_root=resolved_settings.workspace_root,
        projects_root=resolved_settings.projects_root,
        default_execution_time=resolved_settings.task_max_execution_time,
        final_response_timeout=resolved_settings.final_response_timeout,
        max_steps=resolved_settings.task_max_steps,
        event_retention_days=resolved_settings.event_retention_days,
        event_retention_keep_recent=resolved_settings.event_retention_keep_recent,
        attachment_max_bytes=resolved_settings.attachment_max_bytes,
        attachment_max_count=resolved_settings.attachment_max_count,
        vision_enabled=resolved_settings.deepseek_supports_vision,
        schedule_timezone=resolved_settings.schedule_timezone,
        agent_context_chars=resolved_settings.agent_context_chars,
        codegraph_max_files=resolved_settings.codegraph_max_files,
        codegraph_max_symbols=resolved_settings.codegraph_max_symbols,
        codegraph_max_edges=resolved_settings.codegraph_max_edges,
        codegraph_refresh_seconds=resolved_settings.codegraph_refresh_seconds,
        task_budgets={
            "max_llm_calls": resolved_settings.task_max_llm_calls,
            "max_tool_calls": resolved_settings.task_max_tool_calls,
            "max_codegraph_queries": resolved_settings.task_max_codegraph_queries,
            "max_project_reads": resolved_settings.task_max_project_reads,
            "max_source_bytes": resolved_settings.task_max_source_bytes,
            "max_plan_nodes": resolved_settings.task_max_plan_nodes,
            "max_retries": resolved_settings.task_max_retries,
            "max_recovery_attempts": resolved_settings.task_max_recovery_attempts,
        },
    )
    # Registered after the service exists: recurrence is expressed by creating
    # and cloning tasks, so this capability needs the service, not just tools.
    from assistant.capabilities.scheduling import ScheduleTool

    tools.register(ScheduleTool(service))
    return AssistantContext(database, session, service, startup, resolved_settings)