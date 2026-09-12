"""Read-only diagnostics for the local computer."""

from __future__ import annotations

import os
import platform
import sys
from typing import Any

from assistant.domain.models import OperationResult
from assistant.tools import Tool, ToolDefinition


class SystemInfoTool(Tool):
    definition = ToolDefinition(
        name="system",
        description="Read safe basic information about the local computer",
        methods=["info"],
        permissions=["system.read"],
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        if method != "info":
            return OperationResult(success=False, error="Unsupported system method: info")
        return OperationResult(
            success=True,
            output={
                "os": platform.system(),
                "os_release": platform.release(),
                "architecture": platform.machine(),
                "python_version": platform.python_version(),
                "runtime": sys.implementation.name,
                "cpu_count": os.cpu_count(),
            },
        )


__all__ = ["SystemInfoTool"]
