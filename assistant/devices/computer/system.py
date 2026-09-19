"""Read-only diagnostics for the local computer."""

from __future__ import annotations

import os
import platform
import asyncio
import json
import subprocess
import sys
from typing import Any

from assistant.domain.models import OperationResult
from assistant.tools import Tool, ToolDefinition


class SystemInfoTool(Tool):
    definition = ToolDefinition(
        name="system",
        description="Read safe basic information about the local computer",
        methods=["info", "processes"],
        permissions=["system.read", "process.read"],
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        if method != "info":
            if method != "processes":
                return OperationResult(success=False, error=f"Unsupported system method: {method}")
            if os.name != "nt":
                return OperationResult(success=False, error="system.processes requires Windows")
            command = "Get-Process | Select-Object Id,ProcessName,CPU,WorkingSet64,MainWindowTitle | ConvertTo-Json -Compress"
            completed = await asyncio.to_thread(subprocess.run, ["powershell", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, check=False)
            if completed.returncode != 0:
                return OperationResult(success=False, error=completed.stderr.strip() or "process listing failed")
            try:
                processes = json.loads(completed.stdout) if completed.stdout.strip() else []
            except json.JSONDecodeError as error:
                return OperationResult(success=False, error=str(error))
            return OperationResult(success=True, output={"processes": processes if isinstance(processes, list) else [processes]})
        return OperationResult(
            success=True,
            output={
                "os": platform.system(),
                "os_release": platform.release(),
                "architecture": platform.machine(),
                "python_version": platform.python_version(),
                "runtime": sys.implementation.name,
                "cpu_count": os.cpu_count(),
                "hostname": platform.node(),
                "working_directory": os.getcwd(),
            },
        )


__all__ = ["SystemInfoTool"]
