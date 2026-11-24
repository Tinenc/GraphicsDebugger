"""Observe the exact process that owns a GUI-launched AgentHost run.

stdin is a prompt payload, not a lifetime channel: qrenderdoc closes it before
the model starts. A Windows process handle remains bound to the original process
even if its PID is subsequently reused. No timeout or missing heartbeat is taken
as evidence that a slow GUI has exited.
"""

from __future__ import annotations

import asyncio
import contextvars
import ctypes
import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Iterator, TypeVar


class _WindowsProcess:
    def __init__(self, pid: int) -> None:
        from ctypes import wintypes

        self.pid = pid
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.api.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        self.api.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.api.GetProcessTimes.restype = wintypes.BOOL
        self.handle = self.api.OpenProcess(0x00100000 | 0x1000, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.creation_time = 0
        times = [wintypes.FILETIME() for _ in range(4)]
        if self.api.GetProcessTimes(self.handle, *(ctypes.byref(t) for t in times)):
            self.creation_time = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

    def exited(self) -> bool:
        result = self.api.WaitForSingleObject(self.handle, 0)
        if result == 0:
            return True
        if result == 258:  # WAIT_TIMEOUT: process is still alive, not a task timeout.
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def image_path(self) -> str:
        from ctypes import wintypes

        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not self.api.QueryFullProcessImageNameW(self.handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return os.path.normcase(os.path.abspath(buffer.value))

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _windows_parent_pid(pid: int) -> int:
    """Resolve the onefile bootloader's parent using a read-only process snapshot."""
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    for name in ("Process32FirstW", "Process32NextW"):
        fn = getattr(api, name)
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        fn.restype = wintypes.BOOL
    snapshot = api.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        found = api.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32ProcessID == pid:
                return int(entry.th32ParentProcessID)
            found = api.Process32NextW(snapshot, ctypes.byref(entry))
        raise ProcessLookupError("AgentHost launcher was not found in the process snapshot")
    finally:
        api.CloseHandle(snapshot)


def capture_gui_owner(args: Any) -> _WindowsProcess | None:
    # These two flags together are qrenderdoc's run protocol. Ordinary CLI runs,
    # MCP child modules and connection probes do not inherit this observer.
    if os.name != "nt" or not getattr(args, "prompt_stdin", False) or not getattr(args, "cancel_file", None):
        return None
    owner = _WindowsProcess(os.getppid())
    try:
        # PyInstaller --onefile has a launcher process between Python and Qt.
        # Pin Qt, not that launcher (which waits for Python itself to exit).
        if getattr(sys, "frozen", False) and owner.image_path() == os.path.normcase(os.path.abspath(sys.executable)):
            launcher = owner
            owner = _WindowsProcess(_windows_parent_pid(launcher.pid))
            launcher.close()
        return owner
    except BaseException:
        owner.close()
        raise


@dataclass
class RunLifetime:
    owner: Any = None
    cancellation_reason: str = ""

    def metadata(self) -> dict[str, Any]:
        if self.owner is None:
            return {}
        return {
            "ownerProcessId": self.owner.pid,
            "ownerProcessCreationTime": self.owner.creation_time,
            "ownerMonitor": "process-handle",
        }


_LIFETIME: contextvars.ContextVar[RunLifetime | None] = contextvars.ContextVar("bread_run_lifetime", default=None)


def current_run_lifetime() -> RunLifetime | None:
    return _LIFETIME.get()


@contextmanager
def run_lifetime(owner: Any) -> Iterator[RunLifetime]:
    lifetime = RunLifetime(owner)
    token = _LIFETIME.set(lifetime)
    try:
        yield lifetime
    finally:
        _LIFETIME.reset(token)
        if owner is not None:
            owner.close()


async def _wait_for_owner_exit(owner: Any) -> None:
    while not owner.exited():
        await asyncio.sleep(0.5)


_T = TypeVar("_T")


async def run_until_cancelled(work: Awaitable[_T], *, cancel_waiter: Awaitable[None] | None, log: logging.Logger) -> _T:
    """Cancel one engine run, letting its existing transport/finally cleanup run."""
    lifetime = current_run_lifetime()
    run = asyncio.ensure_future(work)
    watchers: dict[asyncio.Task[None], str] = {}
    if cancel_waiter is not None:
        watchers[asyncio.create_task(cancel_waiter)] = "user"
    if lifetime and lifetime.owner is not None:
        watchers[asyncio.create_task(_wait_for_owner_exit(lifetime.owner))] = "owner-process-exited"
    try:
        while watchers and not run.done():
            done, _ = await asyncio.wait({run, *watchers}, return_when=asyncio.FIRST_COMPLETED)
            if run in done:
                break
            for watcher in done:
                reason = watchers.pop(watcher)
                try:
                    watcher.result()
                except Exception as error:
                    # Failed monitoring is not proof of an owner exit/cancel.
                    log.warning("run %s watcher failed: %s", reason, error)
                    continue
                if lifetime is not None:
                    lifetime.cancellation_reason = reason
                log.info("cancelling run: %s; owner_pid=%s", reason, getattr(lifetime.owner if lifetime else None, "pid", None))
                run.cancel()
                return await run
        return await run
    finally:
        for watcher in watchers:
            watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
