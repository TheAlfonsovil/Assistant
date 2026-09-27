"""Host facts: what machine the assistant is actually running on.

Single source of truth for the runtime platform. Two consumers:

- ``seed_durable_memory`` persists the facts as long-term memory, so they
  survive a restart and a memory reset, and workers find them there.
- ``/resources`` returns them on the ACTIVE device, so the machine stays
  identifiable in the dashboard even when memory was cleared.

Workers are pointed at long-term memory for the host interpreter, so keeping
this row present is what stops a Windows host from being handed a POSIX launch
script. Deriving the facts at runtime rather than hardcoding them in a prompt
keeps the templates generic: the same wording renders ``powershell`` on Windows
and ``sh`` on Linux.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path


def default_shell() -> tuple[str, str, str]:
    """Detect the command interpreter the host actually uses.

    Generated scripts and one-off commands must match the host interpreter;
    emitting a POSIX ``.sh`` on Windows produces a file the host cannot run.
    """
    if platform.system() != "Windows":
        return "posix", "sh", ""
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    # PowerShell 7 installs to Program Files, not System32, so the system
    # lookup below only finds Windows PowerShell 5.1. Ask the PATH too before
    # settling, otherwise a machine with both reports the older one.
    candidates = [
        (root / "System32" / "WindowsPowerShell" / "v1.0" / "pwsh.exe", "pwsh"),
        (root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe", "powershell"),
    ]
    for name in ("pwsh", "powershell"):
        from shutil import which

        found = which(name)
        if found:
            candidates.insert(0, (Path(found), name))
    for path, name in candidates:
        if path.is_file():
            shell = "pwsh" if name == "pwsh" else "powershell"
            return "windows", shell, (
                f"Write launch scripts as .ps1 and run them with {shell} -File."
            )
    return "windows", "powershell", (
        "Write launch scripts as .ps1 and run them with powershell -File."
    )


def system_facts() -> dict[str, str]:
    """Return stable technical facts; never inspect personal files or secrets."""
    on_windows = platform.system() == "Windows"
    windows_version = platform.win32_ver()[0] if on_windows else ""
    build_number = platform.win32_ver()[2] if on_windows else ""
    windows_generation = (
        "Windows 11"
        if build_number.isdigit() and int(build_number) >= 22000
        else "Windows"
        if on_windows
        else ""
    )
    shell_family, shell_name, shell_hint = default_shell()
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": windows_version or platform.version(),
        "os_generation": windows_generation,
        "device_platform": "windows" if on_windows else platform.system().lower(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "assistant_runtime": __import__("sys").implementation.name,
        "hostname": platform.node(),
        "cpu_count": str(os.cpu_count() or 0),
        "working_directory": str(Path.cwd()),
        "shell_family": shell_family,
        "default_shell": shell_name,
        "script_extension": ".ps1" if shell_family == "windows" else ".sh",
        "script_interpreter": shell_hint or "Write launch scripts as .sh and run them with sh.",
    }


# The subset worth spending prompt tokens on. The full set above is what the
# memory view shows; a worker only needs enough to pick an interpreter and a
# script extension.
PROMPT_FACT_KEYS = (
    "os",
    "os_generation",
    "architecture",
    "hostname",
    "device_platform",
    "default_shell",
    "shell_family",
    "script_extension",
    "script_interpreter",
    "working_directory",
)


def prompt_facts() -> dict[str, str]:
    """Compact host block for worker prompts."""
    facts = system_facts()
    return {key: facts[key] for key in PROMPT_FACT_KEYS if key in facts}


__all__ = ["PROMPT_FACT_KEYS", "default_shell", "prompt_facts", "system_facts"]
