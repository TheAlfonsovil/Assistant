from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assistant.domain.models import OperationResult
from assistant.tools import Tool, ToolDefinition, ToolRegistry


@dataclass(frozen=True)
class DeviceBranch:
    name: str
    status: str
    description: str


class MockDeviceTool(Tool):
    def __init__(self, device: DeviceBranch):
        self.device = device
        self.definition = ToolDefinition(
            name=f"device.{device.name}",
            description=device.description,
            methods=["status"],
            permissions=[f"device.{device.name}"],
        )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        return OperationResult(
            success=True,
            output={"device": self.device.name, "status": "MOCK", "message": "adapter not connected in V1"},
            metadata={"mock": True},
        )


DEVICE_BRANCHES = (
    DeviceBranch("computer", "ACTIVE", "Local Windows computer and its capabilities"),
    DeviceBranch("mobile", "MOCK", "Future mobile device adapter"),
    DeviceBranch("home", "MOCK", "Future home automation adapter"),
    DeviceBranch("robot", "MOCK", "Future robotics adapter"),
)


def build_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for branch in DEVICE_BRANCHES:
        if branch.status == "MOCK":
            registry.register(MockDeviceTool(branch))
    return registry
