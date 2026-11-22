"""Provider-neutral browser and Windows desktop adapters; lazily initialized."""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
from typing import Any

from .portable_capabilities import bundle_root, require_components, manifest

_browser = None
_playwright = None
_page = None
_lock = None


def system_browser(environ=None) -> str:
    """Use an installed browser only; never download one or import its profile."""
    env = os.environ if environ is None else environ
    explicit = env.get("BREAD_BROWSER_EXECUTABLE", "")
    candidates = [Path(explicit)] if explicit else []
    for key in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        if env.get(key):
            candidates.extend(Path(env[key]) / suffix for suffix in (
                "Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"))
    for candidate in candidates:
        if candidate.is_absolute() and candidate.is_file():
            return str(candidate.resolve())
    return ""


async def browser(action: str = "snapshot", url: str = "", selector: str = "", text: str = "",
                  width: int = 1280, height: int = 800) -> Any:
    """Isolated browser session: open, snapshot, click, fill, key, resize, screenshot, close.

    Selectors come from this browser's snapshot. This does not attach to another
    application's authenticated browser or share its cookies.
    """
    global _browser, _playwright, _page, _lock
    from mcp.server.fastmcp import Image
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if action == "close":
            if _browser:
                await _browser.close()
            if _playwright:
                await _playwright.stop()
            _browser = _playwright = _page = None
            return {"closed": True}
        if _browser is None:
            bundled = manifest().get("features", {}).get("browser", True)
            executable = "" if bundled else system_browser()
            if not bundled and not executable:
                raise RuntimeError("No system browser is available. Configure a browser MCP or BREAD_BROWSER_EXECUTABLE; document/Shader tools do not require it.")
            components = ("python", "browsers") if bundled else ("python",)
            await asyncio.to_thread(require_components, *components)
            root = bundle_root()
            site = str(root / "python/Lib/site-packages")
            if site not in sys.path:
                sys.path.append(site)
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root / "browsers")
            from playwright.async_api import async_playwright
            _playwright = await async_playwright().start()
            try:
                _browser = await _playwright.chromium.launch(headless=True, **({"executable_path": executable} if executable else {}))
            except BaseException:
                await _playwright.stop(); _playwright = None
                raise
            _page = await _browser.new_page(viewport={"width": max(320, min(width, 3840)),
                                                     "height": max(200, min(height, 2160))})
            _page.set_default_timeout(15000)
        if action == "open":
            if not url.startswith(("http://", "https://", "file://", "data:text/html")):
                raise ValueError("Browser URL must be HTTP(S), file, or HTML data")
            await _page.goto(url, wait_until="domcontentloaded", timeout=30000)
        elif action == "click":
            await _page.locator(selector).click()
        elif action == "fill":
            await _page.locator(selector).fill(text)
        elif action == "key":
            await _page.keyboard.press(text)
        elif action == "resize":
            await _page.set_viewport_size({"width": max(320, min(width, 3840)), "height": max(200, min(height, 2160))})
        elif action == "screenshot":
            return Image(data=await _page.screenshot(), format="png")
        elif action != "snapshot":
            raise ValueError("Unknown browser action")
        return {"url": _page.url, "title": await _page.title(),
                "snapshot": (await _page.locator("body").aria_snapshot())[:64000]}


def windows(action: str = "snapshot", window_id: int = 0, text: str = "", x: int = 0, y: int = 0) -> Any:
    """Windows snapshot/screenshot/focus/click/type. Prefer app APIs for structured work.

    Coordinates are physical pixels in the virtual desktop, including negative
    monitor origins. Use window_id from a snapshot to bind input to the intended
    foreground window. Reinspect the screenshot after an input action.
    """
    if sys.platform != "win32":
        raise RuntimeError("Windows desktop adapter requires Windows")
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.WindowFromPoint.argtypes = [wintypes.POINT]
    user.WindowFromPoint.restype = wintypes.HWND
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    try:
        user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (OSError, AttributeError):
        pass
    if action == "screenshot":
        from mcp.server.fastmcp import Image
        from PIL import ImageGrab
        import io
        data = io.BytesIO()
        ImageGrab.grab(all_screens=True).save(data, format="PNG")
        return Image(data=data.getvalue(), format="png")
    if window_id and action in {"click", "type"} and user.GetForegroundWindow() != window_id:
        raise RuntimeError("The selected window is no longer in the foreground; inspect it again")
    if action == "focus":
        if user.GetForegroundWindow() != window_id and not user.SetForegroundWindow(window_id):
            raise RuntimeError("Windows did not grant foreground focus")
    elif action == "click":
        if window_id and user.GetAncestor(user.WindowFromPoint(wintypes.POINT(x, y)), 2) != window_id:
            raise RuntimeError("The click would land outside the selected window; inspect it again")
        if not user.SetCursorPos(x, y):
            raise RuntimeError("Windows rejected cursor movement")
        user.mouse_event(0x0002, 0, 0, 0, 0)
        user.mouse_event(0x0004, 0, 0, 0, 0)
    elif action == "type":
        class Mouse(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("data", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]
        class Keyboard(ctypes.Structure):
            _fields_ = [("vk", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]
        class Hardware(ctypes.Structure):
            _fields_ = [("msg", wintypes.DWORD), ("low", wintypes.WORD), ("high", wintypes.WORD)]
        class Union(ctypes.Union):
            _fields_ = [("mi", Mouse), ("ki", Keyboard), ("hi", Hardware)]
        class Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", Union)]
        if len(text) > 16000:
            raise ValueError("Desktop text exceeds 16000 characters")
        units = text.encode("utf-16-le")
        for index in range(0, len(units), 2):
            unit = int.from_bytes(units[index:index + 2], "little")
            events = (Input * 2)(Input(1, Union(ki=Keyboard(0, unit, 4, 0, 0))),
                                 Input(1, Union(ki=Keyboard(0, unit, 6, 0, 0))))
            if user.SendInput(2, events, ctypes.sizeof(Input)) != 2:
                raise RuntimeError("Windows rejected input")
    elif action != "snapshot":
        raise ValueError("Unknown Windows action")
    windows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    @callback_type
    def collect(hwnd, _):
        length = user.GetWindowTextLengthW(hwnd)
        if length and user.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(length + 1)
            user.GetWindowTextW(hwnd, title, length + 1)
            rect = wintypes.RECT()
            user.GetWindowRect(hwnd, ctypes.byref(rect))
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            windows.append({"id": int(hwnd), "pid": pid.value, "title": title.value,
                            "bounds": [rect.left, rect.top, rect.right, rect.bottom]})
        return True
    user.EnumWindows(collect, 0)
    return {"windows": windows}
