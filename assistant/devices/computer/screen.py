"""Screen inventory and capture for the computer branch.

A screenshot is only useful for driving a GUI if the model can map image pixels
back to absolute screen coordinates, so ``capture`` reports the monitor origin
and the image-to-screen scale next to the PNG. The PNG is published through the
artifact ledger like any other deliverable, which is what lets a later turn (or
a vision model) see it without re-capturing.

Implemented with ctypes + zlib only: the encoder lives here so no new binary
dependency is required on a locked-down host.
"""

from __future__ import annotations

import ctypes
import hashlib
import platform
import struct
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from assistant.domain.models import ErrorType, OperationResult
from assistant.tools import Tool, ToolDefinition, file_artifact

IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", ctypes.c_wchar * 32),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )

    SRCCOPY = 0x00CC0020
    HALFTONE = 4
    DIB_RGB_COLORS = 0
    MONITORINFOF_PRIMARY = 0x00000001
    MAX_MONITORS = 16

    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.StretchBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.StretchBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC,
        ctypes.c_void_p,
        MONITORENUMPROC,
        wintypes.LPARAM,
    ]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL

DEFAULT_MAX_WIDTH = 1600


def _make_dpi_aware() -> None:
    """Best effort: physical pixels make click coordinates match the image."""
    if not IS_WINDOWS:
        return
    try:  # pragma: no cover - depends on the host DPI context
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def list_monitors() -> list[dict[str, Any]]:
    """Return the monitors of the virtual desktop, primary first."""
    if not IS_WINDOWS:
        return []
    _make_dpi_aware()
    monitors: list[dict[str, Any]] = []

    def _callback(hmonitor, _hdc, _rect, _data) -> int:
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            return 1
        rect = info.rcMonitor
        monitors.append(
            {
                "index": len(monitors),
                "device": str(info.szDevice).rstrip("\x00"),
                "left": rect.left,
                "top": rect.top,
                "width": rect.right - rect.left,
                "height": rect.bottom - rect.top,
                "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
                "working_area": {
                    "left": info.rcWork.left,
                    "top": info.rcWork.top,
                    "width": info.rcWork.right - info.rcWork.left,
                    "height": info.rcWork.bottom - info.rcWork.top,
                },
            }
        )
        return 1

    callback = MONITORENUMPROC(_callback)
    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        return []
    monitors.sort(key=lambda item: (not item["primary"], item["left"], item["top"]))
    for position, monitor in enumerate(monitors):
        monitor["index"] = position
    return monitors[:MAX_MONITORS]


def virtual_desktop() -> dict[str, int] | None:
    """Union of every monitor, used when no monitor index is requested."""
    monitors = list_monitors()
    if not monitors:
        return None
    left = min(item["left"] for item in monitors)
    top = min(item["top"] for item in monitors)
    right = max(item["left"] + item["width"] for item in monitors)
    bottom = max(item["top"] + item["height"] for item in monitors)
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


def encode_png(width: int, height: int, bgra: bytes) -> bytes:
    """Encode top-down BGRA pixels as an 8-bit RGB PNG using only stdlib."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    rgb = bytearray(len(bgra) // 4 * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    stride = width * 3
    raw = b"".join(
        b"\x00" + bytes(rgb[offset : offset + stride])
        for offset in range(0, len(rgb), stride)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def grab_region(region: dict[str, int], max_width: int | None) -> tuple[bytes, int, int]:
    """Capture ``region`` as PNG bytes, optionally downscaled to ``max_width``."""
    if not IS_WINDOWS:
        raise OSError("screen capture is only implemented for Windows")
    _make_dpi_aware()
    source_width = int(region["width"])
    source_height = int(region["height"])
    if source_width <= 0 or source_height <= 0:
        raise ValueError("capture region must have a positive size")
    scale = 1.0
    target_width = source_width
    target_height = source_height
    if max_width and source_width > max_width:
        scale = max_width / source_width
        target_width = max(1, int(source_width * scale))
        target_height = max(1, int(source_height * scale))

    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise OSError("GetDC failed")
    memory_dc = None
    bitmap = None
    previous = None
    try:
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, target_width, target_height)
        previous = gdi32.SelectObject(memory_dc, bitmap)
        gdi32.SetStretchBltMode(memory_dc, HALFTONE)
        if not gdi32.StretchBlt(
            memory_dc,
            0,
            0,
            target_width,
            target_height,
            screen_dc,
            int(region["left"]),
            int(region["top"]),
            source_width,
            source_height,
            SRCCOPY,
        ):
            raise OSError(f"StretchBlt failed (error {ctypes.get_last_error()})")
        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = target_width
        header.biHeight = -target_height  # negative: top-down rows
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0  # BI_RGB
        buffer = ctypes.create_string_buffer(target_width * target_height * 4)
        if not gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            target_height,
            buffer,
            ctypes.byref(header),
            DIB_RGB_COLORS,
        ):
            raise OSError(f"GetDIBits failed (error {ctypes.get_last_error()})")
        return encode_png(target_width, target_height, buffer.raw), target_width, target_height
    finally:
        if memory_dc and previous:
            gdi32.SelectObject(memory_dc, previous)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)


class ScreenTool(Tool):
    definition = ToolDefinition(
        name="screen",
        description="List the monitors and take screenshots of this computer",
        methods=["list", "capture"],
        argument_schema={
            "monitor": {"type": "integer"},
            "path": {"type": "string"},
            "max_width": {"type": "integer"},
        },
        method_argument_schema={
            "list": {},
            "capture": {
                "monitor": {"type": "integer"},
                "path": {"type": "string"},
                "max_width": {"type": "integer"},
            },
        },
        permissions=["screen.read"],
        # A second capture must never be served from a cached result: the
        # screen is expected to have changed.
        idempotent=False,
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        started = datetime.now(UTC)
        try:
            monitors = list_monitors()
            if method == "list":
                return OperationResult(
                    success=True,
                    output={
                        "available": bool(monitors),
                        "platform": platform.system(),
                        "monitor_count": len(monitors),
                        "monitors": monitors,
                        "virtual_desktop": virtual_desktop(),
                        "reason": None
                        if monitors
                        else "no monitor was reported by the platform",
                    },
                    started_at=started,
                )
            if method == "capture":
                if not monitors and not IS_WINDOWS:
                    return OperationResult(
                        success=False,
                        error="screen capture is only implemented for Windows",
                        error_type=ErrorType.NOT_FOUND,
                        started_at=started,
                    )
                raw_index = args.get("monitor")
                if raw_index is None:
                    region = virtual_desktop()
                    label = "virtual-desktop"
                else:
                    index = int(raw_index)
                    if index < 0 or index >= len(monitors):
                        return OperationResult(
                            success=False,
                            error=f"monitor {index} does not exist ({len(monitors)} found)",
                            error_type=ErrorType.INVALID_ARGUMENT,
                            started_at=started,
                        )
                    selected = monitors[index]
                    region = {
                        "left": selected["left"],
                        "top": selected["top"],
                        "width": selected["width"],
                        "height": selected["height"],
                    }
                    label = f"monitor-{index}"
                if region is None:
                    return OperationResult(
                        success=False,
                        error="no monitor geometry available to capture",
                        error_type=ErrorType.NOT_FOUND,
                        started_at=started,
                    )
                max_width = int(args.get("max_width") or DEFAULT_MAX_WIDTH)
                data, width, height = grab_region(region, max_width)
                target = Path(
                    str(
                        args.get("path")
                        or Path("data") / "screenshots" / f"{uuid4().hex}.png"
                    )
                ).resolve()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                scale = round(width / region["width"], 6)
                return OperationResult(
                    success=True,
                    output={
                        "image": str(target),
                        "label": label,
                        "image_width": width,
                        "image_height": height,
                        "screen": region,
                        "scale": scale,
                        "bytes": len(data),
                        "monitors": len(monitors),
                        "coordinate_hint": (
                            "screen_x = screen.left + image_x / scale; "
                            "screen_y = screen.top + image_y / scale"
                        ),
                        "captured_at": datetime.now(UTC).isoformat(),
                    },
                    artifacts=[
                        file_artifact(
                            target,
                            description=f"screen.capture {label}",
                            metadata={
                                "monitor": raw_index,
                                "origin_x": region["left"],
                                "origin_y": region["top"],
                                "screen_width": region["width"],
                                "screen_height": region["height"],
                                "image_width": width,
                                "image_height": height,
                                "scale": scale,
                                "content_type": "image/png",
                            },
                            checksum=hashlib.sha256(data).hexdigest(),
                        )
                    ],
                    started_at=started,
                    side_effects=["screen.captured"],
                )
            raise ValueError(f"Unsupported screen method: {method}")
        except (OSError, ValueError, ctypes.ArgumentError) as error:
            return OperationResult(
                success=False,
                error=f"screen.{method} failed: {error}",
                error_type=ErrorType.TOOL_FAILURE,
                retryable=False,
                started_at=started,
            )
