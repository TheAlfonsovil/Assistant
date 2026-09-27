"""Shared FastAPI dependencies for the Assistant API routers."""

from __future__ import annotations

from fastapi import Request

from ..startup.bootstrap import AssistantContext


def get_context(request: Request) -> AssistantContext:
    return request.app.state.context


def get_runtime(request: Request):
    return request.app.state.runtime

