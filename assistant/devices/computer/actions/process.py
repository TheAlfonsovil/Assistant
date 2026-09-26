from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from assistant.domain.models import ErrorType, OperationResult
from assistant.tools import Tool, ToolDefinition

class ProcessTool(Tool):
    """Manage explicitly started long-lived local processes by exact PID."""

    definition = ToolDefinition(
        name="process",
        description="Start, inspect, read logs from, and stop local project processes",
        methods=["start", "status", "log", "stop"],
        argument_schema={
            "command": {"type": "string"},
            "cwd": {"type": "string"},
            "label": {"type": "string"},
            "process_id": {"type": "integer"},
            "tail": {"type": "integer"},
        },
        permissions=["process"],
        idempotent=False,
    )

    def __init__(self, log_dir: str | Path = "data/processes"):
        self.log_dir = Path(log_dir)
        self._processes: dict[int, tuple[asyncio.subprocess.Process, Any, Any, str]] = {}

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        started = datetime.now(UTC)
        if method == "start":
            return await self._start(args, started)
        process_id = args.get("process_id")
        if method == "status" and process_id is None:
            return OperationResult(success=True, output=await self._list_status())
        if not isinstance(process_id, int):
            return OperationResult(
                success=False,
                error=f"process.{method} requires integer process_id",
                error_type=ErrorType.INVALID_ARGUMENT,
                started_at=started,
            )
        record = self._processes.get(process_id)
        if record is None:
            return OperationResult(
                success=False,
                error=f"process not managed by this worker: {process_id}",
                error_type=ErrorType.NOT_FOUND,
                started_at=started,
            )
        process, stdout, stderr, label = record
        if method == "status":
            return OperationResult(success=True, output=self._status(process, label), started_at=started)
        if method == "log":
            tail = min(max(int(args.get("tail", 4000)), 1), 20000)
            return OperationResult(
                success=True,
                output={"process_id": process_id, "stdout": self._tail(stdout, tail), "stderr": self._tail(stderr, tail)},
                started_at=started,
            )
        if method == "stop":
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=min(timeout, 10.0))
                except TimeoutError:
                    process.kill()
                    await process.wait()
            stdout.close()
            stderr.close()
            self._processes.pop(process_id, None)
            return OperationResult(
                success=True,
                output={"process_id": process_id, "stopped": True},
                side_effects=["process.stopped"],
                started_at=started,
            )
        return OperationResult(
            success=False,
            error=f"Unsupported process method: {method}",
            error_type=ErrorType.INVALID_ARGUMENT,
            started_at=started,
        )

    async def _start(self, args: dict[str, Any], started: datetime) -> OperationResult:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return OperationResult(
                success=False,
                error="process.start requires command",
                error_type=ErrorType.INVALID_ARGUMENT,
                started_at=started,
            )
        self.log_dir.mkdir(parents=True, exist_ok=True)
        label = str(args.get("label") or command[:80])
        log_id = uuid4().hex
        stdout_path = self.log_dir / f"{log_id}-stdout.log"
        stderr_path = self.log_dir / f"{log_id}-stderr.log"
        stdout = stdout_path.open("ab")
        stderr = stderr_path.open("ab")
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=args.get("cwd"),
                stdout=stdout,
                stderr=stderr,
                start_new_session=os.name != "nt",
            )
        except (OSError, ValueError) as error:
            stdout.close()
            stderr.close()
            return OperationResult(
                success=False,
                error=str(error),
                error_type=ErrorType.TOOL_FAILURE,
                started_at=started,
            )
        self._processes[process.pid] = (process, stdout, stderr, label)
        return OperationResult(
            success=True,
            output={"process_id": process.pid, "label": label, "stdout": str(stdout_path), "stderr": str(stderr_path)},
            side_effects=["process.started"],
            started_at=started,
        )

    async def _list_status(self) -> list[dict[str, Any]]:
        return [self._status(process, label) for process, _, _, label in self._processes.values()]

    @staticmethod
    def _status(process: asyncio.subprocess.Process, label: str) -> dict[str, Any]:
        return {"process_id": process.pid, "label": label, "running": process.returncode is None, "exit_code": process.returncode}

    @staticmethod
    def _tail(stream, limit: int) -> str:
        stream.flush()
        try:
            with open(stream.name, "rb") as current:
                return current.read()[-limit:].decode(errors="replace")
        except OSError as error:
            return f"log unavailable: {error}"
