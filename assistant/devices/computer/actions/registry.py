"""Registration for the isolated computer action tools."""

from ..browser import BrowserTool
from ..codegraph import CodeGraphTool
from ..debugger import DebugTool
from ..http_client import HttpTool
from ..input import InputTool
from ..screen import ScreenTool
from ..semantics import SemanticTool
from .audit import AuditTool
from .deployment import DeploymentTool
from .filesystem import FilesystemTool
from .git import GitTool
from .process import ProcessTool
from .project_tool import ProjectTool
from .shell import ShellTool
from ..system import SystemInfoTool
from ..web import WebTool
from ..window import WindowTool


def register_actions(
    registry,
    *,
    enable_input: bool = False,
    workspace_root: str = ".",
    langserver_path: str = "",
) -> None:
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
        SemanticTool(workspace_root=workspace_root, langserver_path=langserver_path),
        DebugTool(workspace_root=workspace_root),
        SystemInfoTool(),
        WebTool(),
        BrowserTool(),
        ScreenTool(),
        HttpTool(),
        WindowTool(workspace_root=workspace_root),
    ]
    if enable_input:
        actions.append(InputTool())
    for action in actions:
        registry.register(action)
