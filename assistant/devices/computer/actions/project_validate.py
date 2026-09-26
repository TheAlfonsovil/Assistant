from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from assistant.domain.models import ErrorType, OperationResult


async def validate_project(args: dict[str, Any], timeout: float) -> OperationResult:
    """Run only validation commands explicitly selected by the caller."""
    root = Path(args["root"]).resolve()
    if not root.is_dir():
        return OperationResult(success=False, error=f"project directory does not exist: {root}", error_type=ErrorType.NOT_FOUND)
    commands = args.get("commands")
    if not isinstance(commands, list) or not commands or any(
        not isinstance(item, str) or not item.strip() for item in commands
    ):
        return OperationResult(success=False, error="project.validate requires explicit non-empty commands", error_type=ErrorType.INVALID_ARGUMENT)
    results = []
    for index, command in enumerate(commands, start=1):
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=max(1.0, timeout))
        except TimeoutError:
            process.kill()
            await process.wait()
            results.append({"id": f"command-{index}", "command": command, "status": "TIMEOUT"})
            continue
        except OSError as error:
            results.append({"id": f"command-{index}", "command": command, "status": "FAIL", "error": str(error)})
            continue
        results.append({
            "id": f"command-{index}",
            "command": command,
            "status": "PASS" if process.returncode == 0 else "FAIL",
            "exit_code": process.returncode,
            "stdout": stdout.decode(errors="replace")[-4000:],
            "stderr": stderr.decode(errors="replace")[-4000:],
        })
    failures = [item for item in results if item["status"] != "PASS"]
    return OperationResult(
        success=not failures,
        output={
            "root": str(root),
            "commands": results,
            "validation_status": "PASS" if not failures else "FAIL",
        },
        error=("project validation failed: " + ", ".join(item["id"] for item in failures)) if failures else None,
        error_type=ErrorType.TOOL_FAILURE if failures else None,
        retryable=False,
    )