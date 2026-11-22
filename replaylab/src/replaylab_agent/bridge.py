"""Direct client for the bundled qrenderdoc bridge protocol v2.

AgentHost uses this client for instance discovery, frozen capture context and
connection diagnostics. Agent task tools use the task-scoped MCP filter and
ReplayLabMCP, which forwards authorized calls to the bundled bridge.
"""

from __future__ import annotations

import ctypes as _ctypes
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BridgeError


@dataclass(frozen=True, slots=True)
class _WindowsProcessApi:
    open_process: Any
    get_exit_code: Any
    close_handle: Any


_WINDOWS_PROCESS_API_LOCK = threading.Lock()
_WINDOWS_PROCESS_API: _WindowsProcessApi | None = None
_WINDOWS_DWORD_POINTER = _ctypes.POINTER(_ctypes.c_uint32)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _windows_process_api() -> _WindowsProcessApi | None:
    """Return one pointer-width-safe binding used by every liveness probe."""

    global _WINDOWS_PROCESS_API
    if sys.platform != "win32":
        return None
    if _WINDOWS_PROCESS_API is not None:
        return _WINDOWS_PROCESS_API
    with _WINDOWS_PROCESS_API_LOCK:
        if _WINDOWS_PROCESS_API is not None:
            return _WINDOWS_PROCESS_API
        kernel32 = _ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [
            _ctypes.c_uint32,
            _ctypes.c_int,
            _ctypes.c_uint32,
        ]
        open_process.restype = _ctypes.c_void_p
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [_ctypes.c_void_p, _WINDOWS_DWORD_POINTER]
        get_exit_code.restype = _ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [_ctypes.c_void_p]
        close_handle.restype = _ctypes.c_int
        _WINDOWS_PROCESS_API = _WindowsProcessApi(
            open_process=open_process,
            get_exit_code=get_exit_code,
            close_handle=close_handle,
        )
        return _WINDOWS_PROCESS_API


def _pid_is_live_windows(pid: int, api: _WindowsProcessApi) -> bool:
    """Query one PID while retaining and closing its full-width HANDLE."""

    handle = api.open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = _ctypes.c_uint32(0)
        if not api.get_exit_code(handle, _ctypes.byref(exit_code)):
            return False
        return int(exit_code.value) == _STILL_ACTIVE
    finally:
        api.close_handle(handle)


def default_ipc_root() -> Path:
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    # Never the shared temp directory: on Linux/macOS that put the task event
    # stream, the log and the tool settings under a world-readable /tmp that any
    # local user could pre-plant and that vanished on reboot. XDG data home is the
    # per-user equivalent of LOCALAPPDATA, which the rest of the product uses.
    root = Path(local).expanduser() if local else Path.home() / ".local" / "share"
    return root / "ReplayLab" / "ipc-v2"


def _pid_is_live(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) is not a harmless existence probe on every Windows
        # Python build. Query a minimal process handle instead.
        try:
            api = _windows_process_api()
            return bool(api is not None and _pid_is_live_windows(pid, api))
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class BridgeInstance:
    id: str
    pid: int
    directory: Path
    protocol_version: int
    bridge_version: str
    heartbeat_ms: int
    activity_ms: int
    capture_loaded: bool
    capture_path: str


class BridgeClient:
    def __init__(
        self,
        ipc_root: Path | str | None = None,
        timeout: float = 30.0,
        heartbeat_ttl: float = 15.0,
    ) -> None:
        self.ipc_root = Path(ipc_root) if ipc_root else default_ipc_root()
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        self.heartbeat_ttl = float(heartbeat_ttl)
        if self.heartbeat_ttl <= 0:
            raise ValueError("heartbeat_ttl must be positive")

    def instances(self, *, live_only: bool = True) -> list[BridgeInstance]:
        instance_root = self.ipc_root / "instances"
        if not instance_root.is_dir():
            return []
        result: list[BridgeInstance] = []
        for directory in instance_root.iterdir():
            manifest_path = directory / "instance.json"
            if not directory.is_dir() or not manifest_path.is_file():
                continue
            try:
                value = json.loads(manifest_path.read_text(encoding="utf-8"))
                pid = int(value.get("pid", 0))
                stopped = bool(value.get("stopped", False))
                heartbeat_ms = int(value.get("heartbeatUnixMs", 0))
                heartbeat_age_ms = int(time.time() * 1000) - heartbeat_ms
                if live_only and (
                    stopped
                    or not _pid_is_live(pid)
                    or heartbeat_ms <= 0
                    or heartbeat_age_ms < -5000
                    or heartbeat_age_ms > int(self.heartbeat_ttl * 1000)
                ):
                    continue
                protocol = int(value.get("protocolVersion", 0))
                if protocol < 2:
                    continue
                result.append(
                    BridgeInstance(
                        id=str(value.get("instanceId", directory.name)),
                        pid=pid,
                        directory=directory,
                        protocol_version=protocol,
                        bridge_version=str(value.get("bridgeVersion", "")),
                        heartbeat_ms=heartbeat_ms,
                        activity_ms=int(value.get("lastActivityUnixMs", 0)),
                        capture_loaded=bool(value.get("captureLoaded", False)),
                        capture_path=str(value.get("capturePath", "")),
                    )
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        result.sort(
            key=lambda item: (item.activity_ms, item.heartbeat_ms, item.id), reverse=True
        )
        return result

    def select_instance(self, instance_id: str = "") -> BridgeInstance:
        candidates = self.instances()
        if instance_id:
            for candidate in candidates:
                if candidate.id == instance_id:
                    return candidate
            raise BridgeError(f"qrenderdoc instance is not live: {instance_id}")
        if not candidates:
            raise BridgeError("no live qrenderdoc Bread bridge was found")
        loaded = [candidate for candidate in candidates if candidate.capture_loaded]
        return (loaded or candidates)[0]

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        instance_id: str = "",
        timeout: float | None = None,
    ) -> Any:
        if not method or not isinstance(method, str):
            raise BridgeError("bridge method name is required")
        instance = self.select_instance(instance_id)
        request_dir = instance.directory / "requests"
        response_dir = instance.directory / "responses"
        request_dir.mkdir(parents=True, exist_ok=True)
        response_dir.mkdir(parents=True, exist_ok=True)
        request_id = uuid.uuid4().hex
        request_path = request_dir / f"{request_id}.json"
        response_path = response_dir / f"{request_id}.json"
        payload = {"id": request_id, "method": method, "params": params or {}}
        self._atomic_json(request_path, payload)
        request_timeout = self.timeout if timeout is None else float(timeout)
        if request_timeout <= 0:
            raise BridgeError("bridge timeout must be positive")
        deadline = time.monotonic() + request_timeout
        try:
            while time.monotonic() < deadline:
                if response_path.is_file():
                    try:
                        response = json.loads(response_path.read_text(encoding="utf-8"))
                    except OSError:
                        time.sleep(0.02)
                        continue
                    except UnicodeDecodeError as exc:
                        raise BridgeError("bridge returned a response that is not valid UTF-8") from exc
                    except json.JSONDecodeError as exc:
                        raise BridgeError("bridge returned malformed JSON") from exc
                    if not isinstance(response, dict):
                        raise BridgeError("bridge returned a non-object JSON response")
                    if str(response.get("id", "")) != request_id:
                        raise BridgeError("bridge response id does not match the request")
                    error = response.get("error")
                    if isinstance(error, dict):
                        raise BridgeError(str(error.get("message", "bridge request failed")))
                    return response.get("result")
                time.sleep(0.02)
        finally:
            try:
                response_path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                request_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise BridgeError(f"bridge request timed out after {request_timeout:.1f}s: {method}")

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise BridgeError(f"cannot write bridge request: {exc}") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
