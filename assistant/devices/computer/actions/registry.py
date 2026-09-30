"""Registration for the isolated computer action tools."""

from ..browser import BrowserTool
from ..codegraph import CodeGraphTool
from ..http_client import HttpTool
from ..input import InputTool
from ..screen import ScreenTool
from .audit import AuditTool
from .deployment import DeploymentTool
from .filesystem import FilesystemTool
from .git import GitTool
from .process import ProcessTool
from .project_tool import ProjectTool
from .shell import ShellTool
from ..system import SystemInfoTool
from ..web import WebTool


def register_actions(registry, *, enable_input: bool = False) -> None:
    """Register every real computer action in one discoverable place.

    ``screen`` is read-only and always available. ``input`` (mouse/keyboard)
    can type into any window, so it is registered only when the deployment
    explicitly opts in: when disabled the model never even sees it.
    """
    actions = [
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
        ScreenTool(),
        HttpTool(),
    ]
    if enable_input:
        actions.append(InputTool())
    for action in actions:
        registry.register(action)
