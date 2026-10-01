"""Hardware and peripheral inventory for the computer branch.

Read-only, best effort and dependency-free: the dashboard needs to show what
this machine actually has (screens, memory, storage, cores) so an operator can
see whether a GUI task will even be possible, and so a worker can pick a monitor
index without guessing.

Nothing here inspects user files or personal data; it only reports capacities.
Every platform-specific call degrades to ``None`` instead of raising, because a
missing peripheral detail must never break the resources view.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from .screen import IS_WINDOWS, list_monitors, virtual_desktop

if IS_WINDOWS:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]


def physical_memory() -> dict[str, int] | None:
    """Total/available RAM and load, or None when the host will not say."""
    if IS_WINDOWS:
        try:
            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return {
                "total_bytes": int(status.ullTotalPhys),
                "available_bytes": int(status.ullAvailPhys),
                "load_percent": int(status.dwMemoryLoad),
            }
        except (AttributeError, OSError):
            return None
    try:  # POSIX fallback: pages * page size, no external dependency.
        pages = os.sysconf("SC_PHYS_PAGES")
        available = os.sysconf("SC_AVPHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        total = int(pages) * int(size)
        free = int(available) * int(size)
        load = round(100 * (total - free) / total) if total else 0
        return {"total_bytes": total, "available_bytes": free, "load_percent": load}
    except (AttributeError, OSError, ValueError):
        return None


def _candidate_roots(workspace_root: str | Path | None) -> list[Path]:
    roots: list[Path] = []
    if IS_WINDOWS and hasattr(os, "listdrives"):
        try:
            roots = [Path(drive) for drive in os.listdrives()]
        except OSError:
            roots = []
    if not roots:
        anchor = Path(workspace_root) if workspace_root else Path.cwd()
        roots = [Path(anchor.anchor or "/")]
    return roots


def storage(workspace_root: str | Path | None = None, limit: int = 8) -> list[dict[str, Any]]:
    """Free/total space per mount point, workspace drive first."""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in _candidate_roots(workspace_root)[:limit]:
        try:
            usage = shutil.disk_usage(str(root))
        except OSError:
            continue
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "mount": key,
                "total_bytes": int(usage.total),
                "free_bytes": int(usage.free),
                "used_percent": round(100 * usage.used / usage.total) if usage.total else 0,
            }
        )
    entries.sort(key=lambda item: item["used_percent"], reverse=True)
    return entries


def display() -> dict[str, Any]:
    """Monitor inventory plus the bounding box of the whole virtual desktop."""
    monitors = list_monitors()
    desktop = virtual_desktop()
    primary = next((item for item in monitors if item.get("primary")), monitors[0] if monitors else None)
    return {
        "available": bool(monitors),
        "monitor_count": len(monitors),
        "monitors": monitors,
        "virtual_desktop": desktop,
        "primary_index": primary.get("index") if primary else None,
        "note": (
            "Coordinates are absolute screen pixels. A capture reports its own "
            "origin and scale; screen_x = origin_x + image_x / scale."
            if monitors
            else "No monitors reported by this host."
        ),
    }


def capabilities(*, input_control: bool = False) -> dict[str, Any]:
    """What the computer branch can actually do right now."""
    return {
        "screen_capture": IS_WINDOWS,
        "input_control": bool(input_control),
        "input_control_hint": (
            "enabled"
            if input_control
            else "Set ASSISTANT_ENABLE_INPUT_CONTROL=true to let a worker move the "
            "mouse and type. It can act on any window, so it is opt-in."
        ),
        "process_control": True,
        "shell": True,
    }


def hardware_report(
    workspace_root: str | Path | None = None, *, input_control: bool = False
) -> dict[str, Any]:
    """One bounded snapshot of this machine's relevant peripherals."""
    memory = physical_memory()
    return {
        "host": {
            "hostname": platform.node(),
            "os": platform.system(),
            "os_version": platform.version(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "implementation": sys.implementation.name,
        },
        "cpu": {"logical_cores": os.cpu_count() or 0},
        "memory": memory,
        "storage": storage(workspace_root),
        "display": display(),
        "capabilities": capabilities(input_control=input_control),
    }
