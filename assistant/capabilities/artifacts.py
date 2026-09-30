"""The artifact ledger as a tool: what this task actually produced.

A worker can see artifact references in its context, but until now it could not
open one. ``read`` returns bounded text for text-like artifacts and metadata
plus the path for images, so a screenshot never floods the prompt with binary.

The task id comes from the service (``_task_id``), so a task can only inspect
its own ledger.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from assistant.domain.contracts import ArtifactKind
from assistant.domain.models import ErrorType, OperationResult
from assistant.tools import Tool, ToolDefinition

TEXT_KINDS = {
    ArtifactKind.FILE,
    ArtifactKind.REPORT,
    ArtifactKind.TEST_RESULT,
    ArtifactKind.COMMAND_OUTPUT,
    ArtifactKind.CALCULATION,
    ArtifactKind.CODEGRAPH,
    ArtifactKind.DATASET,
    ArtifactKind.ANSWER,
}

DEFAULT_MAX_CHARS = 20_000
MAX_MAX_CHARS = 200_000


class ArtifactTool(Tool):
    definition = ToolDefinition(
        name="artifact",
        description=(
            "Inspect the artifact ledger of this task: 'list' every published "
            "artifact, 'read' one by id. Images return metadata and a path "
            "instead of bytes."
        ),
        methods=["list", "read"],
        argument_schema={
            "artifact_id": {"type": "string"},
            "node_id": {"type": "string"},
            "max_chars": {"type": "integer"},
        },
        method_argument_schema={
            "list": {"node_id": {"type": "string"}},
            "read": {
                "artifact_id": {"type": "string", "required": True},
                "max_chars": {"type": "integer"},
            },
        },
        permissions=["artifact.read"],
        idempotent=True,
    )

    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _summary(artifact) -> dict[str, Any]:
        return {
            "id": artifact.id,
            "kind": artifact.kind.value,
            "description": artifact.description,
            "path": artifact.path,
            "producer_node_id": artifact.producer_node_id,
            "checksum": artifact.checksum,
            "metadata": artifact.metadata,
        }

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        task_id = args.get("_task_id")
        if not task_id:
            return OperationResult(
                success=False,
                error="artifact tool requires a task context",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        task_id = str(task_id)
        try:
            if method == "list":
                node_id = args.get("node_id")
                artifacts = await self.repository.list_artifacts(
                    task_id, str(node_id) if node_id else None
                )
                return OperationResult(
                    success=True,
                    output={
                        "count": len(artifacts),
                        "artifacts": [self._summary(item) for item in artifacts],
                    },
                )
            if method == "read":
                artifact_id = str(args["artifact_id"])
                artifact = await self.repository.get_artifact(task_id, artifact_id)
                if artifact is None:
                    return OperationResult(
                        success=False,
                        error=f"unknown artifact for this task: {artifact_id}",
                        error_type=ErrorType.NOT_FOUND,
                    )
                summary = self._summary(artifact)
                if artifact.kind not in TEXT_KINDS:
                    summary["note"] = (
                        "Binary artifact: use its path with the appropriate tool "
                        "(and vision, when enabled) instead of inlining it."
                    )
                    return OperationResult(success=True, output=summary)
                if not artifact.path:
                    summary["note"] = "This artifact has no readable path."
                    return OperationResult(success=True, output=summary)
                limit = min(
                    max(int(args.get("max_chars") or DEFAULT_MAX_CHARS), 1), MAX_MAX_CHARS
                )
                try:
                    text = await asyncio.to_thread(
                        Path(artifact.path).read_text, encoding="utf-8"
                    )
                except (OSError, UnicodeDecodeError) as error:
                    summary["note"] = f"Artifact content is not readable as UTF-8 text: {error}"
                    return OperationResult(success=True, output=summary)
                summary["chars"] = len(text)
                summary["truncated"] = len(text) > limit
                summary["content"] = text[:limit]
                return OperationResult(success=True, output=summary)
            return OperationResult(
                success=False,
                error=f"Unsupported artifact method: {method}",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        except (AttributeError, KeyError, OSError, TypeError, ValueError) as error:
            return OperationResult(
                success=False,
                error=f"artifact.{method} failed: {error}",
                error_type=ErrorType.TOOL_FAILURE,
            )
