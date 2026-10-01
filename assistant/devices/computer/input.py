"""Mouse and keyboard control for the computer branch.

This is the most powerful and most dangerous capability in the branch, so it is
opt-in: it is only registered when ``ASSISTANT_ENABLE_INPUT_CONTROL`` is true,
which keeps it out of the model's advertised actions on a default deployment.

Every method here is a real side effect, so ``idempotent`` is False: a replayed
turn must not click twice.
"""

from __future__ import annotations

import ctypes
import platform
from datetime import UTC, datetime
from typing import Any

from assistant.domain.models import ErrorType, OperationResult
from assistant.tools import Tool, ToolDefinition

IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    ULONG_PTR = (
        ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32
    )

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    INPUT_MOUSE = 0
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    MOUSEEVENTF_WHEEL = 0x0800

    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
    user32.VkKeyScanW.restype = ctypes.c_short
    user32.keybd_event.argtypes = [
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.DWORD,
        ULONG_PTR,
    ]
    user32.mouse_event.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ULONG_PTR,
    ]

BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

NAMED_KEYS = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "win": 0x5B,
    "windows": 0x5B,
    "cmd": 0x5B,
}
NAMED_KEYS.update({f"f{index}": 0x6F + index for index in range(1, 13)})

_MODIFIER_ORDER = (0x11, 0x10, 0x12, 0x5B)


def unicode_units(text: str) -> list[int]:
    """UTF-16 code units for ``text``; astral characters become surrogate pairs."""
    encoded = text.encode("utf-16-le")
    return [
        int.from_bytes(encoded[offset : offset + 2], "little")
        for offset in range(0, len(encoded), 2)
    ]


def parse_key_combination(keys: str) -> list[int]:
    """Translate ``ctrl+shift+s`` into virtual-key codes, modifiers first."""
    tokens = [token.strip().lower() for token in str(keys).split("+") if token.strip()]
    if not tokens:
        raise ValueError("keys must not be empty")
    codes: list[int] = []
    for token in tokens:
        code = NAMED_KEYS.get(token)
        if code is None and len(token) == 1:
            scanned = user32.VkKeyScanW(token) if IS_WINDOWS else -1
            code = (scanned & 0xFF) if scanned not in (-1, 0xFFFF) else ord(token.upper())
        if code is None:
            raise ValueError(f"unsupported key: {token}")
        codes.append(code)
    modifiers = [code for code in codes if code in _MODIFIER_ORDER]
    others = [code for code in codes if code not in _MODIFIER_ORDER]
    return [*modifiers, *others]


def _send_inputs(inputs: list) -> int:
    if not inputs:
        return 0
    array = (INPUT * len(inputs))(*inputs)
    return int(user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT)))


def _unicode_inputs(text: str) -> list:
    inputs = []
    for unit in unicode_units(text):
        inputs.append(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE, 0, 0)))
        inputs.append(
            INPUT(
                type=INPUT_KEYBOARD,
                ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0),
            )
        )
    return inputs


def _press_keys(codes: list[int]) -> None:
    for code in codes:
        user32.keybd_event(code, 0, 0, 0)
    for code in reversed(codes):
        user32.keybd_event(code, 0, KEYEVENTF_KEYUP, 0)


class InputTool(Tool):
    definition = ToolDefinition(
        name="input",
        description="Move and click the mouse and send keyboard input to this computer",
        methods=["move", "click", "type", "key", "scroll"],
        argument_schema={
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "clicks": {"type": "integer"},
            "text": {"type": "string"},
            "keys": {"type": "string"},
            "amount": {"type": "integer"},
        },
        method_argument_schema={
            "move": {"x": {"type": "integer", "required": True}, "y": {"type": "integer", "required": True}},
            "click": {
                "x": {"type": "integer", "required": True},
                "y": {"type": "integer", "required": True},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "clicks": {"type": "integer"},
            },
            "type": {"text": {"type": "string", "required": True}},
            "key": {"keys": {"type": "string", "required": True}},
            "scroll": {
                "amount": {"type": "integer", "required": True},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
            },
        },
        permissions=["input.control"],
        idempotent=False,
        # Acting on the desktop changes what a capture would show, so the runtime
        # takes a fresh one and hands it to the next turn.
        observable_methods=["click", "type", "key", "scroll"],
    )

    def _move_to(self, x: int, y: int) -> None:
        if not user32.SetCursorPos(int(x), int(y)):
            raise OSError("SetCursorPos failed")

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        started = datetime.now(UTC)
        if not IS_WINDOWS:
            return OperationResult(
                success=False,
                error="input control is only implemented for Windows",
                error_type=ErrorType.NOT_FOUND,
                started_at=started,
            )
        try:
            if method == "move":
                self._move_to(args["x"], args["y"])
                return OperationResult(
                    success=True,
                    output={"moved_to": {"x": int(args["x"]), "y": int(args["y"])}},
                    started_at=started,
                    side_effects=["input.mouse"],
                )
            if method == "click":
                button = str(args.get("button") or "left").lower()
                if button not in BUTTON_FLAGS:
                    return OperationResult(
                        success=False,
                        error=f"unsupported button: {button}",
                        error_type=ErrorType.INVALID_ARGUMENT,
                        started_at=started,
                    )
                clicks = min(max(int(args.get("clicks") or 1), 1), 3)
                down, up = BUTTON_FLAGS[button]
                self._move_to(args["x"], args["y"])
                for _ in range(clicks):
                    user32.mouse_event(down, 0, 0, 0, 0)
                    user32.mouse_event(up, 0, 0, 0, 0)
                return OperationResult(
                    success=True,
                    output={
                        "clicked": {"x": int(args["x"]), "y": int(args["y"])},
                        "button": button,
                        "clicks": clicks,
                    },
                    started_at=started,
                    side_effects=["input.mouse"],
                )
            if method == "type":
                text = str(args["text"])
                if not text:
                    return OperationResult(
                        success=False,
                        error="text must not be empty",
                        error_type=ErrorType.INVALID_ARGUMENT,
                        started_at=started,
                    )
                sent = _send_inputs(_unicode_inputs(text))
                if sent != len(unicode_units(text)) * 2:
                    raise OSError(f"SendInput delivered {sent} of {len(text)} key events")
                return OperationResult(
                    success=True,
                    output={"typed_chars": len(text)},
                    started_at=started,
                    side_effects=["input.keyboard"],
                )
            if method == "key":
                codes = parse_key_combination(str(args["keys"]))
                _press_keys(codes)
                return OperationResult(
                    success=True,
                    output={"keys": str(args["keys"]), "virtual_codes": codes},
                    started_at=started,
                    side_effects=["input.keyboard"],
                )
            if method == "scroll":
                amount = int(args["amount"])
                if "x" in args and "y" in args:
                    self._move_to(args["x"], args["y"])
                user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, amount, 0)
                return OperationResult(
                    success=True,
                    output={"scrolled": amount},
                    started_at=started,
                    side_effects=["input.mouse"],
                )
            return OperationResult(
                success=False,
                error=f"Unsupported input method: {method}",
                error_type=ErrorType.INVALID_ARGUMENT,
                started_at=started,
            )
        except (KeyError, OSError, ValueError, ctypes.ArgumentError) as error:
            return OperationResult(
                success=False,
                error=f"input.{method} failed: {error}",
                error_type=ErrorType.TOOL_FAILURE,
                retryable=False,
                started_at=started,
            )
