"""Portable ReplayLab MCP entry point and qrenderdoc-owned loopback sidecar.

qrenderdoc loads a first-party bridge from its fixed built-in integration
directory. The bridge runs in embedded Python 3.6 and deliberately remains a
thin replay/UI adapter. This module is packaged as ``BreadMCP.exe`` with a
Python 3.10+ runtime and supports two transports:

``stdio``
    Spawned and owned by an MCP client such as Codex.
``serve`` / ``ensure``
    A qrenderdoc-owned, bearer-protected streamable-HTTP endpoint bound only to
    loopback. Multiple qrenderdoc processes register owner PID files and the
    host exits after the last owner disappears.

No model SDK, ChatGPT credential, Codex token, or OpenAI API key is read here.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import ctypes
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .console_events import emit_console_event
from .version import HOST_STATE_PROTOCOL_VERSION, SERVICE_VERSION


SERVICE_NAME = "ReplayLab"
DEFAULT_PORT = 49643
MAX_PORT_SCAN = 16
OWNER_IDLE_GRACE_SECONDS = 15.0
OWNER_POLL_SECONDS = 1.0
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,128}$")
HOST_INSTANCE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MAX_HOST_HEARTBEAT_AGE_MS = 5000
MAX_HOST_HEARTBEAT_FUTURE_SKEW_MS = 2000
PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
PROCESS_INFORMATION_CLASS_POWER_THROTTLING = 4


def _configure_utf8_stdio() -> None:
    """Make every host entry path deterministic on Windows ANSI locales.

    The packaged entry point already applies this before importing the host,
    but ``python -m src.replaylab_host`` and unit/adapter entry paths call this
    module directly.  Repeating the idempotent configuration here prevents a
    Chinese path in a traceback, project result, or MCP diagnostic from being
    encoded as CP936 before a UTF-8 client sees it.
    """

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="strict")
            except (AttributeError, OSError, ValueError):
                # StringIO/test embedders and already-detached streams need no
                # encoding change; the binary packaged streams do.
                pass


class _ProcessPowerThrottlingState(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_uint32),
        ("ControlMask", ctypes.c_uint32),
        ("StateMask", ctypes.c_uint32),
    ]


def disable_windows_execution_speed_throttling(
    kernel32: Any | None = None,
    *,
    platform_name: str | None = None,
) -> dict[str, Any]:
    """Keep the host's watcher/apply workers responsive in the background.

    Windows may opt a background/minimised process into execution-speed power
    throttling. Setting the documented control bit while clearing its state bit
    explicitly opts this process out. This is process-local, does not alter a
    system power plan, and is reported rather than treated as an apply-safety
    boundary on platforms where the API is unavailable.

    ``kernel32`` and ``platform_name`` are injectable only for deterministic
    unit tests; production callers provide neither.
    """

    platform = os.name if platform_name is None else platform_name
    if platform != "nt":
        return {
            "supported": False,
            "executionSpeedThrottlingDisabled": False,
            "reason": "not-windows",
        }
    try:
        api = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetCurrentProcess.argtypes = []
        api.GetCurrentProcess.restype = ctypes.c_void_p
        api.SetProcessInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        api.SetProcessInformation.restype = ctypes.c_int
        state = _ProcessPowerThrottlingState(
            Version=PROCESS_POWER_THROTTLING_CURRENT_VERSION,
            ControlMask=PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
            StateMask=0,
        )
        succeeded = bool(
            api.SetProcessInformation(
                api.GetCurrentProcess(),
                PROCESS_INFORMATION_CLASS_POWER_THROTTLING,
                ctypes.byref(state),
                ctypes.sizeof(state),
            )
        )
        report = {
            "supported": True,
            "executionSpeedThrottlingDisabled": succeeded,
            "processInformationClass": PROCESS_INFORMATION_CLASS_POWER_THROTTLING,
            "controlMask": PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
            "stateMask": 0,
        }
        if not succeeded:
            report["errorCode"] = int(ctypes.get_last_error())
        return report
    except Exception as error:
        return {
            "supported": False,
            "executionSpeedThrottlingDisabled": False,
            "reason": "%s: %s" % (type(error).__name__, error),
        }


def _prepare_shader_tools() -> None:
    """Discover/hash-pin process-owner tools before importing the MCP server."""
    try:
        from .replaylab_tools import configure_environment

        report = configure_environment(Path(sys.executable).resolve().parent)
        if report.get("error"):
            print("Bread shader tools: %s" % report["error"], file=sys.stderr)
    except Exception as error:
        # Capture inspection remains useful without an external compiler. Tool
        # operations will expose the precise missing/invalid registry error.
        print(
            "Bread shader tool discovery failed: %s: %s"
            % (type(error).__name__, error),
            file=sys.stderr,
        )


def runtime_root(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    configured = str(env.get("REPLAYLAB_RUNTIME_DIR", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local = str(env.get("LOCALAPPDATA", "") or "").strip()
    if local:
        return (Path(local).expanduser() / "ReplayLab" / "runtime").resolve()
    user_suffix = str(getattr(os, "getuid", lambda: os.getpid())())
    return (Path(tempfile.gettempdir()) / ("ReplayLab-" + user_suffix)).resolve()


def _paths(root: Path) -> dict[str, Path]:
    return {
        "root": root,
        "owners": root / "owners",
        "token": root / "host.token",
        "state": root / "host.json",
        "shutdown": root / "shutdown.request",
    }


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(stat.S_IRWXU)
    except OSError:
        pass


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _secure_directory(path.parent)
    temporary = path.with_name(
        "%s.tmp.%d.%d" % (path.name, os.getpid(), time.time_ns())
    )
    # Manifests cross qrenderdoc, Python and occasionally legacy Windows
    # launchers.  ASCII JSON escapes remove the last code-page-sensitive wire
    # boundary while json.load reconstructs the exact Unicode paths.
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _load_or_create_token(root: Path) -> str:
    paths = _paths(root)
    _secure_directory(root)
    try:
        token = paths["token"].read_text(encoding="ascii").strip()
    except OSError:
        token = ""
    if TOKEN_PATTERN.fullmatch(token):
        return token

    token = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(str(paths["token"]), flags, 0o600)
    except FileExistsError:
        token = paths["token"].read_text(encoding="ascii").strip()
        if not TOKEN_PATTERN.fullmatch(token):
            raise RuntimeError("Bread token file is corrupt")
        return token
    with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
        stream.write(token + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        paths["token"].chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return token


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is not a reliable existence probe in a frozen
        # Windows application. In particular, CPython can leave WinError 87 set
        # while returning from the builtin, which surfaces as ``SystemError``
        # under PyInstaller. Query a non-destructive process handle instead.
        try:
            import ctypes

            process_query_limited_information = 0x1000
            synchronize = 0x00100000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.OpenProcess(
                process_query_limited_information | synchronize, False, int(pid)
            )
            if not handle:
                # Access denied proves that a process owns the PID even though
                # this user cannot query it. Other failures mean no live owner.
                return ctypes.get_last_error() == 5
            try:
                exit_code = ctypes.c_uint32(0)
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return int(exit_code.value) == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # Owner liveness is a shutdown/lifetime boundary. If the native
            # probe itself is unavailable, do not keep a privileged host alive
            # indefinitely on an unverifiable PID.
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # A process owned by another security principal is still alive; owner
        # files are in this user's private runtime directory, so fail closed.
        return True
    except (OSError, ProcessLookupError):
        return False


def _owner_path(root: Path, pid: int) -> Path:
    if int(pid) <= 0:
        raise ValueError("owner PID must be positive")
    return _paths(root)["owners"] / ("%d.json" % int(pid))


def register_owner(root: Path, pid: int) -> Path:
    owner = _owner_path(root, pid)
    _atomic_json(
        owner,
        {
            "schema": "replaylab-host-owner/v1",
            "pid": int(pid),
            "registeredUnixMs": int(time.time() * 1000),
        },
    )
    return owner


def release_owner(root: Path, pid: int) -> bool:
    owner = _owner_path(root, pid)
    try:
        owner.unlink()
        return True
    except FileNotFoundError:
        return False


def live_owners(root: Path) -> list[int]:
    directory = _paths(root)["owners"]
    _secure_directory(directory)
    result: list[int] = []
    for owner in directory.glob("*.json"):
        try:
            pid = int(owner.stem)
        except ValueError:
            try:
                owner.unlink()
            except OSError:
                pass
            continue
        value = _load_json(owner)
        if value is None or int(value.get("pid", -1)) != pid or not _process_alive(pid):
            try:
                owner.unlink()
            except OSError:
                pass
            continue
        result.append(pid)
    return sorted(set(result))


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", int(port)))
        except OSError:
            return False
    return True


def _state_port(root: Path) -> int | None:
    state = _load_json(_paths(root)["state"])
    try:
        port = int((state or {}).get("port", 0))
    except (TypeError, ValueError):
        return None
    return port if 1024 <= port <= 65535 else None


def choose_port(root: Path, requested: int | None = None) -> int:
    configured = requested
    if configured is None:
        raw = os.environ.get("REPLAYLAB_MCP_PORT", "").strip()
        if raw:
            configured = int(raw)
    if configured is not None:
        if not 1024 <= int(configured) <= 65535:
            raise ValueError("Bread port must be between 1024 and 65535")
        return int(configured)
    previous = _state_port(root)
    candidates = ([previous] if previous is not None else []) + list(
        range(DEFAULT_PORT, DEFAULT_PORT + MAX_PORT_SCAN)
    )
    for candidate in dict.fromkeys(candidates):
        if _port_is_free(candidate):
            return candidate
    raise RuntimeError("No free Bread loopback port is available")


def health(port: int, timeout: float = 0.75) -> dict[str, Any] | None:
    request = Request("http://127.0.0.1:%d/health" % int(port), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read(32 * 1024).decode("utf-8"))
    except (OSError, HTTPError, URLError, ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("service") != SERVICE_NAME:
        return None
    return value


def _different_host_version_error() -> RuntimeError:
    return RuntimeError(
        "A different Bread Host version is already running; close all "
        "qrenderdoc instances before upgrading"
    )


def _host_identity_contract_error(source: str) -> RuntimeError:
    return RuntimeError(
        "A same-version Bread Host has an incompatible identity contract "
        "in %s; expected hostStateProtocolVersion=%d and a 32-character "
        "lowercase hexadecimal hostInstanceId. Close all qrenderdoc instances "
        "before retrying"
        % (source, HOST_STATE_PROTOCOL_VERSION)
    )


def _identity_from_record(value: Mapping[str, Any], source: str) -> str:
    protocol_version = value.get("hostStateProtocolVersion")
    host_instance_id = value.get("hostInstanceId")
    if (
        isinstance(protocol_version, bool)
        or not isinstance(protocol_version, int)
        or protocol_version != HOST_STATE_PROTOCOL_VERSION
        or not isinstance(host_instance_id, str)
        or HOST_INSTANCE_ID_PATTERN.fullmatch(host_instance_id) is None
    ):
        raise _host_identity_contract_error(source)
    return host_instance_id


def _fresh_live_host_state(root: Path, port: int) -> dict[str, Any] | None:
    """Return state only when its heartbeat and process still prove liveness."""

    state = _load_json(_paths(root)["state"])
    if (
        state is None
        or state.get("schema") != "replaylab-host/v1"
        or state.get("service") != SERVICE_NAME
        or state.get("host") != "127.0.0.1"
    ):
        return None
    try:
        state_port = int(state.get("port"))
        pid = int(state.get("pid"))
        heartbeat = int(state.get("heartbeatUnixMs"))
    except (TypeError, ValueError, OverflowError):
        return None
    if any(isinstance(state.get(field), bool) for field in (
        "port", "pid", "heartbeatUnixMs"
    )):
        return None
    heartbeat_age = int(time.time() * 1000) - heartbeat
    if (
        state_port != int(port)
        or pid <= 0
        or heartbeat <= 0
        or heartbeat_age < -MAX_HOST_HEARTBEAT_FUTURE_SKEW_MS
        or heartbeat_age > MAX_HOST_HEARTBEAT_AGE_MS
        or not _process_alive(pid)
    ):
        return None
    return state


def _validate_running_host_identity(
    root: Path, port: int, current: Mapping[str, Any]
) -> str:
    """Bind one live health response to the exact fresh on-disk generation."""

    if current.get("version") != SERVICE_VERSION:
        raise _different_host_version_error()
    health_instance_id = _identity_from_record(current, "/health")
    state = _fresh_live_host_state(root, port)
    if state is None:
        raise _host_identity_contract_error("host.json")
    if (
        state.get("version") != SERVICE_VERSION
        or state.get("endpoint") != "http://127.0.0.1:%d/mcp" % int(port)
        or current.get("transport") != "streamable-http"
    ):
        raise _host_identity_contract_error("host.json and /health")
    state_instance_id = _identity_from_record(state, "host.json")
    state_pid = state.get("pid")
    health_pid = current.get("pid")
    if (
        isinstance(state_pid, bool)
        or not isinstance(state_pid, int)
        or isinstance(health_pid, bool)
        or not isinstance(health_pid, int)
        or state_pid != health_pid
        or state_instance_id != health_instance_id
    ):
        raise _host_identity_contract_error("host.json and /health")
    return health_instance_id


def _self_command(arguments: Sequence[str]) -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), *arguments]
    return [
        str(Path(sys.executable).resolve()),
        "-m",
        "src.replaylab_host",
        *arguments,
    ]


def _spawn_detached(arguments: Sequence[str]) -> None:
    child_environment = os.environ.copy()
    if getattr(sys, "frozen", False):
        # Give the detached service its own one-file extraction lifetime;
        # its same-executable `ensure` parent exits as soon as it is healthy.
        child_environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    creation_flags = 0
    if os.name == "nt":
        # The long-lived serve child is a PyInstaller onefile executable: this
        # spawns its *bootloader*, which in turn spawns the real Python child.
        # With DETACHED_PROCESS the bootloader has no console at all, so when
        # it spawns the console-subsystem Python child Windows allocates that
        # child a brand-new *visible* console window (Windows also ignores
        # CREATE_NO_WINDOW whenever DETACHED_PROCESS is set). CREATE_NO_WINDOW
        # without DETACHED_PROCESS instead gives the bootloader a hidden
        # console that its Python child inherits, so no window ever appears.
        # A new process group still isolates it from the parent's Ctrl+C.
        creation_flags = (
            int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
            | int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        )
    subprocess.Popen(
        list(arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creation_flags,
        cwd=str(Path(sys.executable).resolve().parent),
        env=child_environment,
        shell=False,
    )


@contextmanager
def _ensure_generation_lock(root: Path, timeout_seconds: float = 15.0):
    """Serialise external Host launch without sharing Internal's lock file."""

    _secure_directory(root)
    path = root / "mcp-ensure.lock"
    stream = path.open("a+b")
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            while time.monotonic() < deadline:
                try:
                    if path.stat().st_size == 0:
                        stream.seek(0)
                        stream.write(b"0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl

            while time.monotonic() < deadline:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.05)
        if not locked:
            raise RuntimeError("timed out acquiring Bread MCP launch lock")
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        stream.close()


def _ensure_unlocked(
    root: Path, owner_pid: int, port: int | None = None
) -> dict[str, Any]:
    _secure_directory(root)
    selected_port = port if port is not None else _state_port(root)
    current = health(selected_port) if selected_port else None
    if current is not None:
        host_instance_id = _validate_running_host_identity(
            root, int(selected_port), current
        )
        register_owner(root, owner_pid)
        return {
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "hostStateProtocolVersion": HOST_STATE_PROTOCOL_VERSION,
            "hostInstanceId": host_instance_id,
            "started": False,
            "port": int(selected_port),
            "endpoint": "http://127.0.0.1:%d/mcp" % int(selected_port),
        }

    waiting_for_existing = False
    if selected_port is not None:
        live_state = _fresh_live_host_state(root, int(selected_port))
        if live_state is not None:
            if live_state.get("version") != SERVICE_VERSION:
                raise _different_host_version_error()
            _identity_from_record(live_state, "host.json")
            waiting_for_existing = True

    if not waiting_for_existing:
        selected_port = choose_port(root, port)
        command = _self_command(
            [
                "serve",
                "--runtime-dir",
                str(root),
                "--port",
                str(selected_port),
            ]
        )
        # The child starts ownerless. Only a fully compatible host generation
        # earns an owner record below; failed or incompatible launches then
        # retire naturally after the normal idle grace period.
        _spawn_detached(command)

    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        current = health(selected_port)
        if current is not None:
            host_instance_id = _validate_running_host_identity(
                root, int(selected_port), current
            )
            register_owner(root, owner_pid)
            return {
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "hostStateProtocolVersion": HOST_STATE_PROTOCOL_VERSION,
                "hostInstanceId": host_instance_id,
                "started": not waiting_for_existing,
                "port": selected_port,
                "endpoint": "http://127.0.0.1:%d/mcp" % selected_port,
            }
        live_state = _fresh_live_host_state(root, int(selected_port))
        if live_state is not None:
            if live_state.get("version") != SERVICE_VERSION:
                raise _different_host_version_error()
            _identity_from_record(live_state, "host.json")
        time.sleep(0.1)
    raise RuntimeError("Bread host did not become healthy within 12 seconds")


def ensure(root: Path, owner_pid: int, port: int | None = None) -> dict[str, Any]:
    """Attach to or launch exactly one external MCP Host generation."""

    with _ensure_generation_lock(root):
        return _ensure_unlocked(root, owner_pid, port)


class BearerAuthApp:
    """Small ASGI gate that leaves only ``/health`` unauthenticated."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self.expected = ("Bearer " + token).encode("ascii")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") != "/health":
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            supplied = headers.get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                body = b'{"error":"unauthorized"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                            (b"www-authenticate", b"Bearer"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def _serve(root: Path, port: int, owner_pid: int | None = None) -> int:
    # Heavy MCP/ASGI imports occur only in the packaged Python 3.10+ host.
    import uvicorn
    from starlette.requests import Request as StarletteRequest
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    if not 1024 <= int(port) <= 65535:
        raise ValueError("Bread port must be between 1024 and 65535")
    power_policy = disable_windows_execution_speed_throttling()
    if (
        power_policy.get("supported") is True
        and power_policy.get("executionSpeedThrottlingDisabled") is not True
    ):
        emit_console_event(
            "host",
            "warning",
            "Windows 后台执行速度限制未能关闭，文件监听延迟可能增加。",
            phase="lifecycle",
            code="host.background-throttling-warning",
            context={"operation": "serve"},
        )
        print(
            "Bread background execution-speed throttling could not be disabled "
            "(Win32 error %s)" % power_policy.get("errorCode", "unknown"),
            file=sys.stderr,
        )
    _secure_directory(root)
    paths = _paths(root)
    token = _load_or_create_token(root)
    if owner_pid is not None:
        register_owner(root, owner_pid)
    try:
        paths["shutdown"].unlink()
    except FileNotFoundError:
        pass

    # Process-owned generation marker for recoverable workspace leases.  It is
    # deliberately replaced for every singleton host generation and cannot be
    # supplied by an MCP request.
    host_instance_id = secrets.token_hex(16)
    os.environ["REPLAYLAB_HOST_INSTANCE_ID"] = host_instance_id
    _prepare_shader_tools()
    from .server import mcp

    async def health_endpoint(_request: StarletteRequest) -> JSONResponse:
        return JSONResponse(
            {
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "hostStateProtocolVersion": HOST_STATE_PROTOCOL_VERSION,
                "transport": "streamable-http",
                "pid": os.getpid(),
                "hostInstanceId": host_instance_id,
                "backgroundExecution": power_policy,
            }
        )

    app = mcp.streamable_http_app()
    app.routes.insert(0, Route("/health", health_endpoint, methods=["GET"]))
    protected = BearerAuthApp(app, token)
    started_ms = int(time.time() * 1000)
    empty_since: float | None = None
    server_box: dict[str, Any] = {}

    def publish_state() -> None:
        _atomic_json(
            paths["state"],
            {
                "schema": "replaylab-host/v1",
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "hostStateProtocolVersion": HOST_STATE_PROTOCOL_VERSION,
                "pid": os.getpid(),
                "hostInstanceId": host_instance_id,
                "host": "127.0.0.1",
                "port": int(port),
                "endpoint": "http://127.0.0.1:%d/mcp" % int(port),
                "tokenFile": str(paths["token"]),
                "startedUnixMs": started_ms,
                "heartbeatUnixMs": int(time.time() * 1000),
                "ownerPids": live_owners(root),
                "backgroundExecution": power_policy,
            },
        )

    async def monitor() -> None:
        """Run owner lifecycle checks on uvicorn's own event loop.

        A PyInstaller-frozen Windows executable can receive a console control
        interruption while ``threading.Thread.start()`` waits for its bootstrap
        event. Keeping the lifecycle callback on uvicorn's loop avoids that
        packaging-specific failure and also avoids a second mutable execution
        context around ``server.should_exit``.
        """

        nonlocal empty_since
        server = server_box.get("server")
        owners = live_owners(root)
        if paths["shutdown"].exists():
            if server is not None:
                server.should_exit = True
        elif owners:
            empty_since = None
        elif empty_since is None:
            empty_since = time.monotonic()
        elif time.monotonic() - empty_since >= OWNER_IDLE_GRACE_SECONDS:
            if server is not None:
                server.should_exit = True
        publish_state()

    config = uvicorn.Config(
        protected,
        host="127.0.0.1",
        port=int(port),
        log_level="warning",
        access_log=False,
        server_header=False,
        callback_notify=monitor,
        timeout_notify=max(1, int(OWNER_POLL_SECONDS)),
    )
    server = uvicorn.Server(config)
    server_box["server"] = server

    publish_state()
    emit_console_event(
        "host",
        "info",
        "Bread AI 服务已完成初始化并开始监听。",
        phase="lifecycle",
        code="host.ready",
        context={"operation": "serve"},
    )
    try:
        server.run()
    finally:
        server.should_exit = True
        state = _load_json(paths["state"])
        if state is not None and int(state.get("pid", -1)) == os.getpid():
            try:
                paths["state"].unlink()
            except FileNotFoundError:
                pass
        try:
            paths["shutdown"].unlink()
        except FileNotFoundError:
            pass
    return 0


def serve(root: Path, port: int, owner_pid: int | None = None) -> int:
    """Run the host while publishing non-authoritative lifecycle telemetry."""

    emit_console_event(
        "host",
        "info",
        "Bread AI 服务正在启动。",
        phase="lifecycle",
        code="host.starting",
        context={"operation": "serve"},
    )
    try:
        return _serve(root, port, owner_pid)
    except Exception as exc:
        emit_console_event(
            "host",
            "error",
            "Bread AI 服务运行失败：%s" % str(exc)[:2048],
            phase="lifecycle",
            code="host.failed",
            context={"operation": "serve"},
        )
        raise
    finally:
        emit_console_event(
            "host",
            "info",
            "Bread AI 服务已停止。",
            phase="lifecycle",
            code="host.stopped",
            context={"operation": "serve"},
        )


def status(root: Path) -> dict[str, Any]:
    state = _load_json(_paths(root)["state"])
    port = _state_port(root)
    current = health(port) if port else None
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "running": current is not None,
        "port": port,
        "endpoint": ("http://127.0.0.1:%d/mcp" % port) if port else None,
        "owners": live_owners(root),
        "state": state,
        # Deliberately disclose only the path, never the bearer secret.
        "tokenFile": str(_paths(root)["token"]),
    }


def codex_toml(executable: Path) -> str:
    path = str(executable.resolve()).replace("\\", "/").replace("'", "\\'")
    return "\n".join(
        [
            "[mcp_servers.Bread]",
            "command = '%s'" % path,
            "args = ['stdio']",
            "startup_timeout_sec = 30",
            "tool_timeout_sec = 3600",
            "",
        ]
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="BreadMCP",
        description="External Bread MCP adapter for Codex and IDE clients",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "stdio",
            "serve",
            "ensure",
            "release",
            "status",
            "codex-config",
        ),
        default="stdio",
    )
    parser.add_argument("--runtime-dir")
    parser.add_argument("--port", type=int)
    parser.add_argument("--owner-pid", type=int)
    parser.add_argument("--executable")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_stdio()
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    if raw_arguments and raw_arguments[0] == "adapter-hlsl":
        from .shader_workspace.adapters.hlsl_decompiler_adapter import main as adapter

        return int(adapter(raw_arguments[1:]))
    if raw_arguments and raw_arguments[0] == "adapter-dxbc-assembly":
        from .shader_workspace.adapters.dxbc_assembly_adapter import main as adapter

        return int(adapter(raw_arguments[1:]))
    if raw_arguments and raw_arguments[0] == "adapter-d3dcompile":
        from .shader_workspace.adapters.d3dcompiler_adapter import main as adapter

        return int(adapter(raw_arguments[1:]))
    if raw_arguments and raw_arguments[0] == "adapter-dxil-assemble":
        from .shader_workspace.adapters.dxil_assembler_adapter import main as adapter

        return int(adapter(raw_arguments[1:]))
    if raw_arguments and raw_arguments[0] == "adapter-spirv-cross":
        from .shader_workspace.adapters.spirv_cross_adapter import main as adapter

        return int(adapter(raw_arguments[1:]))
    if raw_arguments and raw_arguments[0] == "adapter-renodx":
        from .shader_workspace.adapters.renodx_decompiler_adapter import main as adapter

        return int(adapter(raw_arguments[1:]))
    arguments = _parser().parse_args(raw_arguments)
    root = (
        Path(arguments.runtime_dir).expanduser().resolve()
        if arguments.runtime_dir
        else runtime_root()
    )
    if arguments.command == "stdio":
        power_policy = disable_windows_execution_speed_throttling()
        if (
            power_policy.get("supported") is True
            and power_policy.get("executionSpeedThrottlingDisabled") is not True
        ):
            emit_console_event(
                "host",
                "warning",
                "Windows 后台执行速度限制未能关闭，文件监听延迟可能增加。",
                phase="lifecycle",
                code="host.background-throttling-warning",
                context={"operation": "stdio"},
            )
            print(
                "Bread background execution-speed throttling could not be disabled "
                "(Win32 error %s)" % power_policy.get("errorCode", "unknown"),
                file=sys.stderr,
            )
        _prepare_shader_tools()
        from .server import main as stdio_main

        stdio_main()
        return 0
    if arguments.command == "serve":
        port = choose_port(root, arguments.port)
        return serve(root, port, arguments.owner_pid)
    if arguments.command == "ensure":
        if not arguments.owner_pid:
            raise SystemExit("ensure requires --owner-pid")
        print(json.dumps(ensure(root, arguments.owner_pid, arguments.port), indent=2))
        return 0
    if arguments.command == "release":
        if not arguments.owner_pid:
            raise SystemExit("release requires --owner-pid")
        print(json.dumps({"released": release_owner(root, arguments.owner_pid)}))
        return 0
    if arguments.command == "status":
        print(json.dumps(status(root), ensure_ascii=False, indent=2))
        return 0
    if arguments.command == "codex-config":
        executable = (
            Path(arguments.executable).expanduser().resolve()
            if arguments.executable
            else Path(sys.executable).resolve()
        )
        print(codex_toml(executable), end="")
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
