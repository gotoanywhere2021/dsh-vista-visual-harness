"""Windows capture + synthetic-input layer for the VISTA harness.

Two capabilities, both needed by the paper's harness:

* visual observation  -- grab a window / screen / region as a lossless array;
* play               -- deliver clicks, keys and text through SendInput.

The module is deliberately dependency-light: ctypes + Pillow only.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageGrab

IS_WINDOWS = True
if __import__("sys").platform != "win32":  # pragma: no cover - harness is Windows-first
    IS_WINDOWS = False

# --------------------------------------------------------------------------- #
# DPI + user32/gdi32 bindings
# --------------------------------------------------------------------------- #

user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)  # type: ignore[attr-defined]


def enable_dpi_awareness() -> None:
    """Make window rectangles and screen grabs agree on physical pixels."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]

    @property
    def size(self) -> tuple[int, int]:
        return self.right - self.left, self.bottom - self.top


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


# Handle-returning prototypes must be declared, otherwise 64-bit handles are
# truncated to 32-bit ints and every downstream GDI call fails.
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetWindowDC.restype = wintypes.HDC
user32.GetWindowDC.argtypes = [wintypes.HWND]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetDIBits.argtypes = [
    wintypes.HDC,
    wintypes.HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.mouse_event.argtypes = [
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]


# --------------------------------------------------------------------------- #
# Window discovery
# --------------------------------------------------------------------------- #

@dataclass
class WindowInfo:
    hwnd: int
    title: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom

    def as_dict(self) -> dict[str, Any]:
        left, top, right, bottom = self.rect
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "rect": [left, top, right - left, bottom - top],
        }


def _title_of(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def list_windows(filter_text: str | None = None) -> list[WindowInfo]:
    results: list[WindowInfo] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _title_of(hwnd)
        if not title:
            return True
        if filter_text is not None and filter_text.lower() not in title.lower():
            return True
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        width, height = rect.size
        if width < 40 or height < 40:
            return True
        results.append(WindowInfo(int(hwnd), title, (rect.left, rect.top, rect.right, rect.bottom)))
        return True

    callback = callback_type(_callback)
    user32.EnumWindows(callback, 0)
    return results


def find_window(title: str) -> WindowInfo:
    """Find a visible top-level window by exact, then case-insensitive, then substring title."""
    windows = list_windows()
    if not windows:
        raise RuntimeError("no visible top-level windows found")
    lowered = title.lower()
    exact = [w for w in windows if w.title == title]
    if exact:
        return max(exact, key=lambda w: (w.rect[2] - w.rect[0]) * (w.rect[3] - w.rect[1]))
    ci_exact = [w for w in windows if w.title.lower() == lowered]
    if ci_exact:
        return max(ci_exact, key=lambda w: (w.rect[2] - w.rect[0]) * (w.rect[3] - w.rect[1]))
    partial = [w for w in windows if lowered in w.title.lower()]
    if partial:
        return max(partial, key=lambda w: (w.rect[2] - w.rect[0]) * (w.rect[3] - w.rect[1]))
    raise RuntimeError(f"no visible window title matching {title!r}; found: "
                       + ", ".join(repr(w.title) for w in windows[:12]))


def window_info(hwnd: int) -> WindowInfo:
    if not user32.IsWindow(hwnd):
        raise RuntimeError(f"hwnd {hwnd} is no longer a window")
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    return WindowInfo(hwnd, _title_of(hwnd), (rect.left, rect.top, rect.right, rect.bottom))


HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010


def set_topmost(hwnd: int, enabled: bool = True) -> bool:
    """Pin the observed window above other windows (needed when the IDE covers the screen)."""
    insert_after = HWND_TOPMOST if enabled else HWND_NOTOPMOST
    return bool(user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))


def focus_window(hwnd: int, topmost: bool | None = None) -> bool:
    """Restore + raise the window, then try to make it the foreground window.

    SetForegroundWindow legitimately fails when another process owns the
    foreground lock (a full-screen IDE is the common case). The harness then
    pins the window topmost so synthetic clicks land on it. Pass ``topmost=False``
    to avoid changing z-order at all.
    """
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if topmost is False:
        # Release a pin set by an earlier act/focus call.
        set_topmost(hwnd, False)
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)  # HWND_TOP
    user32.BringWindowToTop(hwnd)
    if user32.SetForegroundWindow(hwnd):
        return True
    time.sleep(0.05)
    if user32.SetForegroundWindow(hwnd):
        return True
    if topmost is False:
        return False
    pinned = set_topmost(hwnd, True)
    user32.SetForegroundWindow(hwnd)
    return pinned


# --------------------------------------------------------------------------- #
# Visual observation
# --------------------------------------------------------------------------- #

def _is_blank(image: Image.Image) -> bool:
    extrema = image.convert("RGB").getextrema()
    return all(low == high for low, high in extrema)


def _grab_region(box: tuple[int, int, int, int]) -> Image.Image:
    return ImageGrab.grab(bbox=box, all_screens=True).convert("RGB")


def capture_window(hwnd: int) -> Image.Image:
    """Capture a window through PrintWindow (works while occluded) with a screen fallback."""
    info = window_info(hwnd)
    left, top, right, bottom = info.rect
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise RuntimeError(f"window {hwnd} has empty rect {info.rect}")

    screen_dc = user32.GetDC(0)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    previous = gdi32.SelectObject(mem_dc, bitmap)
    image: Image.Image | None = None
    try:
        # PW_RENDERFULLCONTENT (0x2) captures DirectComposition-rendered windows.
        rendered = bool(user32.PrintWindow(hwnd, mem_dc, 0x2))
        if rendered:
            header = BITMAPINFO()
            header.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.bmiHeader.biWidth = width
            header.bmiHeader.biHeight = -height  # top-down rows
            header.bmiHeader.biPlanes = 1
            header.bmiHeader.biBitCount = 32
            header.bmiHeader.biCompression = 0  # BI_RGB
            buffer = ctypes.create_string_buffer(width * height * 4)
            copied = gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
            if copied == height:
                image = Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1).copy()
    finally:
        if previous:
            gdi32.SelectObject(mem_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)

    if image is None or _is_blank(image):
        focus_window(hwnd)
        time.sleep(0.08)
        try:
            image = _grab_region((left, top, right, bottom))
        except Exception as error:  # pragma: no cover - platform failure path
            raise RuntimeError(f"window capture failed: {error}") from error
    return image


def capture_screen(region: tuple[int, int, int, int] | None = None) -> tuple[Image.Image, tuple[int, int]]:
    """Capture the whole virtual screen, optionally cropped. Returns (image, origin)."""
    if region is None:
        image = ImageGrab.grab(all_screens=True).convert("RGB")
        return image, (0, 0)
    x, y, width, height = region
    return _grab_region((x, y, x + width, y + height)), (x, y)


# --------------------------------------------------------------------------- #
# Synthetic input
# --------------------------------------------------------------------------- #

if IS_WINDOWS:  # pragma: no branch - definitions only meaningful on Windows
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
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

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]

    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]

    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004

    NAMED_KEYS: dict[str, int] = {
        "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
        "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
        "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B,
        "space": 0x20, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
        "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
        "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
        "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
        "numlock": 0x90, "scrolllock": 0x91,
        "printscreen": 0x2C, "prtsc": 0x2C,
    }
    for _index in range(1, 13):
        NAMED_KEYS[f"f{_index}"] = 0x6F + _index

    MODIFIER_KEYS: dict[str, int] = {
        "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    }

    def _key_down(vk: int, unicode_scan: int | None = None) -> INPUT:
        if unicode_scan is not None:
            return INPUT(type=1, u=INPUT_UNION(ki=KEYBDINPUT(0, unicode_scan, KEYEVENTF_UNICODE, 0, 0)))
        return INPUT(type=1, u=INPUT_UNION(ki=KEYBDINPUT(vk, 0, 0, 0, 0)))

    def _key_up(vk: int, unicode_scan: int | None = None) -> INPUT:
        if unicode_scan is not None:
            return INPUT(type=1, u=INPUT_UNION(
                ki=KEYBDINPUT(0, unicode_scan, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)))
        return INPUT(type=1, u=INPUT_UNION(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0)))

    def send_inputs(inputs: list[INPUT]) -> None:
        if not inputs:
            return
        array = (INPUT * len(inputs))(*inputs)
        sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            raise ctypes.WinError(ctypes.get_last_error())

    def _vk_for_char(char: str) -> tuple[int, bool]:
        """Return (virtual-key, needs_shift) for one printable character."""
        result = user32.VkKeyScanW(ord(char))
        if result == -1:
            raise ValueError(f"cannot map character {char!r} to a virtual key")
        vk = result & 0xFF
        shift = bool((result >> 8) & 0x01)
        return vk, shift

    def press_keys(combo: str) -> None:
        """Press one key chord such as ``ctrl+shift+s`` or ``enter``."""
        pieces = [piece.strip().lower() for piece in combo.split("+") if piece.strip()]
        if not pieces:
            raise ValueError("empty key chord")
        modifiers: list[int] = []
        for piece in pieces[:-1]:
            if piece not in MODIFIER_KEYS:
                raise ValueError(f"unknown modifier {piece!r} in chord {combo!r}")
            modifiers.append(MODIFIER_KEYS[piece])
        key = pieces[-1]
        if key in NAMED_KEYS:
            vk = NAMED_KEYS[key]
            shift_needed = False
        elif len(key) == 1:
            vk, shift_needed = _vk_for_char(key)
        else:
            raise ValueError(f"unknown key {key!r} in chord {combo!r}")
        if shift_needed and 0x10 not in modifiers:
            modifiers.append(0x10)
        sequence: list[INPUT] = [_key_down(mod) for mod in modifiers]
        sequence.append(_key_down(vk))
        sequence.append(_key_up(vk))
        sequence.extend(_key_up(mod) for mod in reversed(modifiers))
        send_inputs(sequence)

    def type_text(text: str, delay: float = 0.0) -> None:
        for char in text:
            if char == "\n":
                press_keys("enter")
                continue
            if char == "\t":
                press_keys("tab")
                continue
            sequence = [_key_down(0, ord(char)), _key_up(0, ord(char))]
            send_inputs(sequence)
            if delay:
                time.sleep(delay)

    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_WHEEL = 0x0800
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_VIRTUALDESK = 0x4000

    def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
        return INPUT(type=0, u=INPUT_UNION(mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)))

    def _absolute(x: int, y: int) -> tuple[int, int]:
        """Map screen pixels to the 0..65535 absolute space of the whole virtual desktop.

        SendInput is used for the pointer as well: SetCursorPos fails under some
        host policies where injected pointer motion is still permitted.
        """
        vx = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        vy = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
        vw = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        vh = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        nx = int(round((int(x) - vx) * 65535 / max(1, vw - 1)))
        ny = int(round((int(y) - vy) * 65535 / max(1, vh - 1)))
        return nx, ny

    def _cursor_position() -> tuple[int, int]:
        point = POINT()
        user32.GetCursorPos(ctypes.byref(point))
        return int(point.x), int(point.y)

    def move_to(x: int, y: int) -> None:
        """Move the pointer, tolerating a transient secure desktop (UAC prompt).

        While the secure desktop owns input, SetCursorPos/SendInput fail with
        ERROR_INVALID_HANDLE (6); retry briefly so a prompt that closes does not
        break the run.
        """
        target = (int(x), int(y))
        last_error: Exception | None = None
        for attempt in range(6):
            try:
                nx, ny = _absolute(*target)
                try:
                    send_inputs([_mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny)])
                except OSError:
                    if not user32.SetCursorPos(*target):
                        raise ctypes.WinError(ctypes.get_last_error())
                time.sleep(0.05)
                if _cursor_position() == target:
                    return
                last_error = RuntimeError(f"pointer did not reach {target}; at {_cursor_position()}")
            except OSError as error:
                last_error = error
            time.sleep(0.25 * (attempt + 1))
        raise RuntimeError(f"cannot move pointer to {target}: {last_error}")

    def click_at(x: int, y: int, button: str = "left", double: bool = False) -> None:
        if button == "left":
            down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
        else:
            down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                move_to(x, y)
                time.sleep(0.04)
                send_inputs([_mouse_input(down), _mouse_input(up)])
                if double:
                    time.sleep(0.04)
                    send_inputs([_mouse_input(down), _mouse_input(up)])
                return
            except (OSError, RuntimeError) as error:
                last_error = error
                time.sleep(0.2 * (attempt + 1))
        raise RuntimeError(f"click at {(x, y)} failed: {last_error}")

    def scroll(amount: int) -> None:
        delta = 120 if amount >= 0 else -120
        for _ in range(abs(amount)):
            send_inputs([_mouse_input(MOUSEEVENTF_WHEEL, data=delta)])

    def drag(from_xy: tuple[int, int], to_xy: tuple[int, int], seconds: float = 0.25) -> None:
        move_to(int(from_xy[0]), int(from_xy[1]))
        time.sleep(0.03)
        send_inputs([_mouse_input(MOUSEEVENTF_LEFTDOWN)])
        steps = max(2, int(seconds / 0.02))
        start_x, start_y = from_xy
        end_x, end_y = to_xy
        for step in range(1, steps + 1):
            x = int(start_x + (end_x - start_x) * step / steps)
            y = int(start_y + (end_y - start_y) * step / steps)
            move_to(x, y)
            time.sleep(seconds / steps)
        send_inputs([_mouse_input(MOUSEEVENTF_LEFTUP)])
