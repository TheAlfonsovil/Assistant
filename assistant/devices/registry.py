"""Composition root for the four device branches."""

from __future__ import annotations

from typing import Any

from assistant.devices.base import DeviceBranch
from assistant.domain.models import OperationResult
from assistant.tools import NotificationTool, Tool, ToolDefinition, ToolRegistry


class MockDeviceTool(Tool):
    """Stable placeholder until a physical adapter is implemented."""

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
            output={"device": self.device.name, "status": "MOCK", "message": "adapter not connected"},
            metadata={"mock": True},
        )


DEVICE_BRANCHES = (
    DeviceBranch("computer", "ACTIVE", "Local computer and its capabilities"),
    DeviceBranch("mobile", "MOCK", "Mobile device adapter"),
    DeviceBranch("home", "MOCK", "Home automation adapter"),
    DeviceBranch("robot", "MOCK", "Robotics adapter"),
)


class DeviceRegistry:
    """Registers one branch at a time so new devices stay isolated."""

    def __init__(self, branches: tuple[DeviceBranch, ...] = DEVICE_BRANCHES):
        self.branches = branches

    def register(self, registry: ToolRegistry) -> None:
        from assistant.devices.computer.actions import register_actions

        registry.register(NotificationTool())
        for branch in self.branches:
            if branch.name == "computer" and branch.status == "ACTIVE":
                register_actions(registry)
            elif branch.status == "MOCK":
                registry.register(MockDeviceTool(branch))


def build_tool_registry() -> ToolRegistry:
    registry = ToolRegistry([])
    DeviceRegistry().register(registry)
    return registry


__all__ = ["DEVICE_BRANCHES", "DeviceBranch", "DeviceRegistry", "MockDeviceTool", "build_tool_registry"]
