"""Registration for the isolated computer action tools."""

from ..browser import BrowserTool
from ..codegraph import CodeGraphTool
from .audit import AuditTool
from .deployment import DeploymentTool
from .filesystem import FilesystemTool
from .git import GitTool
from .process import ProcessTool
from .project_tool import ProjectTool
from .shell import ShellTool
from ..system import SystemInfoTool
from ..web import WebTool


def register_actions(registry) -> None:
    """Register every real computer action in one discoverable place."""
    for action in (
        FilesystemTool(),
        AuditTool(),
        ShellTool(),
        ProcessTool(),
        GitTool(),
        DeploymentTool(),
        ProjectTool(),
        CodeGraphTool(),
        SystemInfoTool(),
        WebTool(),
        BrowserTool(),
    ):
        registry.register(action)
