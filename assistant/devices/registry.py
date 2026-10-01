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
            output={
                "device": self.device.name,
                "platform": self.device.platform,
                "transport": self.device.transport,
                "status": "MOCK",
                "message": "adapter not connected",
            },
            metadata={"mock": True, "platform": self.device.platform},
        )


DEVICE_BRANCHES = (
    DeviceBranch("computer", "ACTIVE", "Windows computer and local capabilities", "windows", "local"),
    DeviceBranch("mobile", "MOCK", "Android mobile device adapter", "android", "adb"),
    DeviceBranch("home", "MOCK", "Home automation adapter"),
    DeviceBranch("robot", "MOCK", "Robotics adapter"),
)


class DeviceRegistry:
    """Registers one branch at a time so new devices stay isolated."""

    def __init__(
        self,
        branches: tuple[DeviceBranch, ...] = DEVICE_BRANCHES,
        enable_input: bool = False,
        workspace_root: str = ".",
        langserver_path: str = "",
    ):
        self.branches = branches
        self.enable_input = enable_input
        self.workspace_root = workspace_root
        self.langserver_path = langserver_path

    def register(self, registry: ToolRegistry) -> None:
        from assistant.devices.computer.actions import register_actions

        registry.register(NotificationTool())
        for branch in self.branches:
            if branch.name == "computer" and branch.status == "ACTIVE":
                register_actions(
                    registry,
                    enable_input=self.enable_input,
                    workspace_root=self.workspace_root,
                    langserver_path=self.langserver_path,
                )
            elif branch.status == "MOCK":
                registry.register(MockDeviceTool(branch))


def build_tool_registry(
    policy=None,
    enable_input: bool = False,
    repository=None,
    rate_limit=None,
    workspace_root: str = ".",
    langserver_path: str = "",
) -> ToolRegistry:
    registry = ToolRegistry([], policy=policy, rate_limit=rate_limit)
    DeviceRegistry(
        enable_input=enable_input,
        workspace_root=workspace_root,
        langserver_path=langserver_path,
    ).register(registry)
    if repository is not None:
        # Task-scoped capabilities need the ledger, so they are only registered
        # when a repository exists (the API and CLI always provide one).
        from assistant.capabilities.artifacts import ArtifactTool
        from assistant.capabilities.memory import MemoryTool

        registry.register(MemoryTool(repository))
        registry.register(ArtifactTool(repository))
    return registry


__all__ = ["DEVICE_BRANCHES", "DeviceBranch", "DeviceRegistry", "MockDeviceTool", "build_tool_registry"]
