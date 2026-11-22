"""qrenderdoc-owned high-level ReplayLab project service.

This process is deliberately not an MCP server and contains no model/provider
entry point.  It exposes only the bounded project-management and project-
restore operations needed by the built-in Bread AI product.  The listener
is fixed to ``127.0.0.1``, every non-health route requires a per-generation
random bearer token, and owner PID records bound its lifetime to qrenderdoc.

``ReplayLabMCP.exe`` remains the separately packaged external Codex/IDE
adapter.  Internal qrenderdoc code must connect to this service instead of
spawning that adapter for each project operation.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .active_projects import ActiveProjectError
from .internal_tool_service import (
    InternalToolService,
    ProjectRestoreRequestError,
    ShaderEditorSaveRequestError,
    MAX_ASSET_INDEX_OPERATION_BYTES,
    MAX_SHADER_LIVE_EDIT_OPERATION_BYTES,
    _atomic_json,
    _load_json,
    _process_alive,
    _read_shader_editor_save_body,
    _secure_directory,
    project_association_guard,
    project_restore_delete_response,
    project_restore_get_response,
    project_restore_post_response,
)
from .version import (
    INTERNAL_HOST_CAPABILITY_SCHEMA,
    INTERNAL_HOST_CAPABILITY_VERSIONS,
    INTERNAL_HOST_STATE_PROTOCOL_VERSION,
    SERVICE_VERSION,
)


SERVICE_NAME = "ReplayLabInternal"
STATE_SCHEMA = "replaylab-internal-host/v1"
OWNER_SCHEMA = "replaylab-internal-host-owner/v1"
PROJECT_OPERATION_SCHEMA = "replaylab-internal-project-operation/v1"
PROJECT_OPERATION_RESULT_SCHEMA = "replaylab-internal-project-operation-result/v1"
DEFAULT_PORT = 49743
MAX_PORT_SCAN = 16
OWNER_IDLE_GRACE_SECONDS = 15.0
OWNER_POLL_SECONDS = 1.0
HOST_STARTUP_TIMEOUT_SECONDS = 60.0
MAX_HEARTBEAT_AGE_MS = 5000
MAX_HEARTBEAT_FUTURE_SKEW_MS = 2000
MAX_PROJECT_REQUEST_BYTES = 16 * 1024 * 1024
MAX_PROJECT_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_PROJECT_PATH_BYTES = 32 * 1024
MAX_PROJECT_TEXT_BYTES = 4096
MAX_PROJECT_ITEMS = 4096
MAX_OPERATION_CACHE = 256
OPERATION_CACHE_TTL_SECONDS = 10 * 60.0
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,128}$")
INSTANCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
INTERNAL_HOST_CAPABILITIES = tuple(INTERNAL_HOST_CAPABILITY_VERSIONS)
REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# This service is fixed to the IPv4 loopback interface.  Explicitly bypass
# ambient HTTP(S) proxy configuration so a corporate/user proxy cannot turn a
# healthy private Host into a false cold-start failure.
_LOOPBACK_PROXY_HANDLER = ProxyHandler({})
_LOOPBACK_OPENER = build_opener(_LOOPBACK_PROXY_HANDLER)


def runtime_root(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    configured = str(env.get("REPLAYLAB_INTERNAL_RUNTIME_DIR", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local = str(env.get("LOCALAPPDATA", "") or "").strip()
    if local:
        return (Path(local).expanduser() / "ReplayLab" / "internal-runtime").resolve()
    user_suffix = str(getattr(os, "getuid", lambda: os.getpid())())
    return (
        Path(tempfile.gettempdir()) / ("ReplayLabInternal-" + user_suffix)
    ).resolve()


def _paths(root: Path) -> dict[str, Path]:
    return {
        "root": root,
        "owners": root / "owners",
        "token": root / "internal-host.token",
        "state": root / "internal-host.json",
        "shutdown": root / "shutdown.request",
        "ensure_lock": root / "ensure.lock",
    }


def _write_generation_token(root: Path) -> str:
    """Rotate the bearer for every successfully launched Host generation."""

    _secure_directory(root)
    token = secrets.token_urlsafe(32)
    if TOKEN_PATTERN.fullmatch(token) is None:
        raise RuntimeError("generated Bread internal token is invalid")
    target = _paths(root)["token"]
    temporary = target.with_name(
        "%s.tmp.%d.%d" % (target.name, os.getpid(), time.time_ns())
    )
    descriptor = os.open(
        str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        try:
            target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return token


def _owner_path(root: Path, pid: int) -> Path:
    if isinstance(pid, bool) or int(pid) <= 0:
        raise ValueError("owner PID must be positive")
    return _paths(root)["owners"] / ("%d.json" % int(pid))


def register_owner(root: Path, pid: int) -> Path:
    owner = _owner_path(root, pid)
    _atomic_json(
        owner,
        {
            "schema": OWNER_SCHEMA,
            "pid": int(pid),
            "registeredUnixMs": int(time.time() * 1000),
        },
    )
    return owner


def release_owner(root: Path, pid: int) -> bool:
    try:
        _owner_path(root, pid).unlink()
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
            pid = -1
        value = _load_json(owner)
        if (
            pid <= 0
            or value is None
            or value.get("schema") != OWNER_SCHEMA
            or value.get("pid") != pid
            or not _process_alive(pid)
        ):
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
    except (TypeError, ValueError, OverflowError):
        return None
    return port if 1024 <= port <= 65535 else None


def choose_port(root: Path, requested: int | None = None) -> int:
    configured = requested
    if configured is None:
        raw = str(os.environ.get("REPLAYLAB_INTERNAL_PORT", "") or "").strip()
        if raw:
            configured = int(raw)
    if configured is not None:
        if not 1024 <= int(configured) <= 65535:
            raise ValueError("Bread internal port must be between 1024 and 65535")
        return int(configured)
    previous = _state_port(root)
    candidates = ([previous] if previous is not None else []) + list(
        range(DEFAULT_PORT, DEFAULT_PORT + MAX_PORT_SCAN)
    )
    for candidate in dict.fromkeys(candidates):
        if _port_is_free(candidate):
            return int(candidate)
    raise RuntimeError("no free Bread internal loopback port is available")


def health(port: int, timeout: float = 0.75) -> dict[str, Any] | None:
    request = Request("http://127.0.0.1:%d/health" % int(port), method="GET")
    try:
        with _LOOPBACK_OPENER.open(request, timeout=timeout) as response:
            raw = response.read(32 * 1024 + 1)
        if len(raw) > 32 * 1024:
            return None
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, HTTPError, URLError, UnicodeError, ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("service") != SERVICE_NAME:
        return None
    return value


def _fresh_state(root: Path, port: int) -> dict[str, Any] | None:
    state = _load_json(_paths(root)["state"])
    if (
        state is None
        or state.get("schema") != STATE_SCHEMA
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
    if any(
        isinstance(state.get(field), bool)
        for field in ("port", "pid", "heartbeatUnixMs")
    ):
        return None
    age = int(time.time() * 1000) - heartbeat
    if (
        state_port != int(port)
        or pid <= 0
        or age < -MAX_HEARTBEAT_FUTURE_SKEW_MS
        or age > MAX_HEARTBEAT_AGE_MS
        or not _process_alive(pid)
    ):
        return None
    return state


def _validate_generation(
    root: Path, port: int, current: Mapping[str, Any]
) -> str:
    state = _fresh_state(root, port)
    if state is None:
        raise RuntimeError("Bread internal Host state is stale or unavailable")
    instance = current.get("hostInstanceId")
    if (
        current.get("version") != SERVICE_VERSION
        or current.get("transport") != "internal-http"
        or current.get("hostStateProtocolVersion")
        != INTERNAL_HOST_STATE_PROTOCOL_VERSION
        or not isinstance(instance, str)
        or INSTANCE_PATTERN.fullmatch(instance) is None
        or state.get("version") != SERVICE_VERSION
        or state.get("hostStateProtocolVersion")
        != INTERNAL_HOST_STATE_PROTOCOL_VERSION
        or state.get("hostInstanceId") != instance
        or state.get("pid") != current.get("pid")
        or state.get("endpoint")
        != "http://127.0.0.1:%d/replaylab/internal" % int(port)
    ):
        raise RuntimeError("Bread internal Host identity is incompatible")
    return instance


@contextmanager
def _ensure_lock(root: Path, timeout_seconds: float = 15.0):
    """Serialise generation launch so a losing process cannot rotate its token."""

    _secure_directory(root)
    path = _paths(root)["ensure_lock"]
    stream = path.open("a+b")
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            while time.monotonic() < deadline:
                try:
                    stream.seek(0)
                    if stream.tell() == 0 and path.stat().st_size == 0:
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
            raise RuntimeError("timed out acquiring Bread internal launch lock")
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


def _self_command(arguments: Sequence[str]) -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), *arguments]
    return [
        str(Path(sys.executable).resolve()),
        "-m",
        "src.internal_host",
        *arguments,
    ]


def _spawn_detached(arguments: Sequence[str]) -> None:
    child_environment = os.environ.copy()
    if getattr(sys, "frozen", False):
        # `serve` outlives this short-lived `ensure` process. A same-executable
        # PyInstaller child otherwise reuses the parent's extraction directory,
        # which the parent removes before the service's later lazy imports.
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


def ensure(root: Path, owner_pid: int, port: int | None = None) -> dict[str, Any]:
    with _ensure_lock(root):
        selected_port = port if port is not None else _state_port(root)
        current = health(selected_port) if selected_port else None
        if current is not None:
            instance = _validate_generation(root, int(selected_port), current)
            register_owner(root, owner_pid)
            return {
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "hostStateProtocolVersion": INTERNAL_HOST_STATE_PROTOCOL_VERSION,
                "hostInstanceId": instance,
                "started": False,
                "port": int(selected_port),
                "endpoint": "http://127.0.0.1:%d/replaylab/internal"
                % int(selected_port),
            }

        selected_port = choose_port(root, port)
        _spawn_detached(
            _self_command(
                [
                    "serve",
                    "--runtime-dir",
                    str(root),
                    "--port",
                    str(selected_port),
                ]
            )
        )
        deadline = time.monotonic() + HOST_STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            current = health(selected_port)
            if current is not None:
                instance = _validate_generation(root, selected_port, current)
                register_owner(root, owner_pid)
                return {
                    "service": SERVICE_NAME,
                    "version": SERVICE_VERSION,
                    "hostStateProtocolVersion": INTERNAL_HOST_STATE_PROTOCOL_VERSION,
                    "hostInstanceId": instance,
                    "started": True,
                    "port": selected_port,
                    "endpoint": "http://127.0.0.1:%d/replaylab/internal"
                    % selected_port,
                }
            time.sleep(0.1)
    raise RuntimeError(
        "Bread internal Host did not become healthy within %d seconds"
        % int(HOST_STARTUP_TIMEOUT_SECONDS)
    )


def _duplicate_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON value: %s" % value)


def _text(value: Any, label: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % label)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("%s must be valid UTF-8" % label) from exc
    if len(encoded) > maximum or any(ord(character) < 32 for character in value):
        raise ValueError("%s is invalid" % label)
    if not allow_empty and not value.strip():
        raise ValueError("%s must not be empty" % label)
    return value.strip()


def _path_text(value: Any, label: str) -> str:
    text = _text(value, label, MAX_PROJECT_PATH_BYTES)
    raw = Path(text).expanduser()
    if not raw.is_absolute():
        raise ValueError("%s must be absolute" % label)
    return str(raw)


def _string_list(
    value: Any,
    label: str,
    maximum_item_bytes: int,
    *,
    paths: bool = False,
    sha256: bool = False,
) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_PROJECT_ITEMS:
        raise ValueError("%s is invalid" % label)
    result: list[str] = []
    for index, item in enumerate(value):
        if paths:
            clean = _path_text(item, "%s[%d]" % (label, index))
        else:
            clean = _text(
                item, "%s[%d]" % (label, index), maximum_item_bytes
            ).lower() if sha256 else _text(
                item, "%s[%d]" % (label, index), maximum_item_bytes
            )
        if sha256 and SHA256_PATTERN.fullmatch(clean) is None:
            raise ValueError("%s[%d] is invalid" % (label, index))
        result.append(clean)
    return result


_PROJECT_PARAMETER_FIELDS = {
    "list-projects": set(),
    "create-project": {
        "name",
        "projectRoot",
        "assetRoot",
        "captureFiles",
        "gameId",
        "buildId",
        "platform",
        "apiFamily",
    },
    "register-project": {"projectRoot"},
    "unregister-project": {"projectRoot"},
    "activate-project": {"projectRoot"},
    "configure-project": {
        "projectRoot",
        "gameId",
        "buildId",
        "platform",
        "apiFamily",
        "assetRootMode",
        "assetRoot",
    },
    "add-captures": {"projectRoot", "captureFiles"},
    "remove-captures": {"projectRoot", "captureRefIds"},
    "match-capture": {
        "capturePath",
        "associationLease",
        "associationToken",
    },
}


def parse_project_operation_request(payload: bytes) -> dict[str, Any]:
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("project operation body must be non-empty bytes")
    if len(payload) > MAX_PROJECT_REQUEST_BYTES:
        raise OverflowError("project operation body exceeds its byte limit")
    value = json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=_duplicate_json_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "requestId",
        "operation",
        "parameters",
    }:
        raise ValueError("project operation fields are invalid")
    if value.get("schema") != PROJECT_OPERATION_SCHEMA:
        raise ValueError("project operation schema is invalid")
    request_id = value.get("requestId")
    if (
        not isinstance(request_id, str)
        or REQUEST_ID_PATTERN.fullmatch(request_id) is None
    ):
        raise ValueError("project operation requestId is invalid")
    operation = value.get("operation")
    parameters = value.get("parameters")
    expected = _PROJECT_PARAMETER_FIELDS.get(operation)
    if expected is None or not isinstance(parameters, dict) or set(parameters) != expected:
        raise ValueError("project operation parameters are invalid")

    clean: dict[str, Any] = {}
    if "projectRoot" in parameters:
        clean["projectRoot"] = _path_text(parameters["projectRoot"], "projectRoot")
    if operation == "create-project":
        clean.update(
            {
                "name": _text(parameters["name"], "name", 256),
                "assetRoot": _text(
                    parameters["assetRoot"], "assetRoot", MAX_PROJECT_PATH_BYTES, True
                ),
                "captureFiles": _string_list(
                    parameters["captureFiles"],
                    "captureFiles",
                    MAX_PROJECT_PATH_BYTES,
                    paths=True,
                ),
                "gameId": _text(parameters["gameId"], "gameId", 256, True),
                "buildId": _text(parameters["buildId"], "buildId", 256, True),
                "platform": _text(parameters["platform"], "platform", 128, True),
                "apiFamily": _text(parameters["apiFamily"], "apiFamily", 64, True),
            }
        )
        if clean["assetRoot"]:
            clean["assetRoot"] = _path_text(clean["assetRoot"], "assetRoot")
    elif operation == "configure-project":
        mode = _text(parameters["assetRootMode"], "assetRootMode", 16)
        if mode not in ("unchanged", "set", "clear"):
            raise ValueError("assetRootMode is invalid")
        asset = _text(
            parameters["assetRoot"], "assetRoot", MAX_PROJECT_PATH_BYTES, True
        )
        if mode == "set":
            asset = _path_text(asset, "assetRoot")
        elif asset:
            raise ValueError("assetRoot must be empty unless assetRootMode is set")
        clean.update(
            {
                "gameId": _text(parameters["gameId"], "gameId", 256, True),
                "buildId": _text(parameters["buildId"], "buildId", 256, True),
                "platform": _text(parameters["platform"], "platform", 128, True),
                "apiFamily": _text(parameters["apiFamily"], "apiFamily", 64, True),
                "assetRootMode": mode,
                "assetRoot": asset,
            }
        )
    elif operation == "add-captures":
        clean["captureFiles"] = _string_list(
            parameters["captureFiles"],
            "captureFiles",
            MAX_PROJECT_PATH_BYTES,
            paths=True,
        )
        if not clean["captureFiles"]:
            raise ValueError("captureFiles must not be empty")
    elif operation == "remove-captures":
        clean["captureRefIds"] = _string_list(
            parameters["captureRefIds"], "captureRefIds", 64, sha256=True
        )
        if not clean["captureRefIds"]:
            raise ValueError("captureRefIds must not be empty")
    elif operation == "match-capture":
        clean["capturePath"] = _path_text(parameters["capturePath"], "capturePath")
        lease = _text(
            parameters["associationLease"],
            "associationLease",
            MAX_PROJECT_PATH_BYTES,
            True,
        )
        token = _text(
            parameters["associationToken"], "associationToken", 64, True
        )
        if bool(lease) != bool(token):
            raise ValueError("association lease and token must be supplied together")
        if lease:
            lease = _path_text(lease, "associationLease")
            if INSTANCE_PATTERN.fullmatch(token.lower()) is None:
                raise ValueError("associationToken is invalid")
        clean["associationLease"] = lease
        clean["associationToken"] = token.lower()

    return {
        "requestId": request_id,
        "operation": operation,
        "parameters": clean,
    }


def _dispatch_project_operation(
    service: Any, operation: str, parameters: Mapping[str, Any]
) -> dict[str, Any]:
    if operation == "list-projects":
        return service.list_projects()
    if operation == "create-project":
        return service.create_project(
            name=parameters["name"],
            game_id=parameters["gameId"],
            build_id=parameters["buildId"],
            platform=parameters["platform"],
            api_family=parameters["apiFamily"],
            asset_root=parameters["assetRoot"] or None,
            capture_files=parameters["captureFiles"],
            project_root=parameters["projectRoot"],
        )
    if operation == "register-project":
        return service.register_existing_project(parameters["projectRoot"])
    if operation == "unregister-project":
        return service.unregister_project(parameters["projectRoot"])
    if operation == "activate-project":
        return service.activate(parameters["projectRoot"])
    if operation == "configure-project":
        arguments: dict[str, Any] = {
            "game_id": parameters["gameId"],
            "build_id": parameters["buildId"],
            "platform": parameters["platform"],
            "api_family": parameters["apiFamily"],
        }
        if parameters["assetRootMode"] == "set":
            arguments["asset_root"] = parameters["assetRoot"]
        elif parameters["assetRootMode"] == "clear":
            arguments["asset_root"] = None
        return service.configure_project_settings(
            parameters["projectRoot"], **arguments
        )
    if operation == "add-captures":
        return service.add_capture_files(
            parameters["projectRoot"], parameters["captureFiles"]
        )
    if operation == "remove-captures":
        return service.remove_capture_references(
            parameters["projectRoot"], parameters["captureRefIds"]
        )
    if operation == "match-capture":
        guard = None
        if parameters["associationLease"]:
            guard = project_association_guard(
                parameters["associationLease"],
                parameters["associationToken"],
                parameters["capturePath"],
            )
        return service.match_and_activate_capture(
            parameters["capturePath"], activation_guard=guard
        )
    raise AssertionError("unreachable project operation")


class ProjectOperationRegistry:
    """Serialise and deduplicate bounded high-level project mutations."""

    # How long a request waits for the operation before it; past that the
    # caller is told what is running instead of timing out its own transport.
    LOCK_WAIT_SECONDS = 20.0
    # Operations without side effects run outside the serialising lock: the
    # project window's list must open while an activation is still validating.
    READ_ONLY_OPERATIONS = frozenset({"list-projects"})

    def __init__(self, service: Any):
        self.service = service
        self._lock = threading.RLock()
        self._results: OrderedDict[str, tuple[float, str, int, dict[str, Any]]] = (
            OrderedDict()
        )
        self._running: tuple[str, float] | None = None

    def _purge(self, now: float) -> None:
        expired = [
            key
            for key, item in self._results.items()
            if now - item[0] > OPERATION_CACHE_TTL_SECONDS
        ]
        for key in expired:
            self._results.pop(key, None)
        while len(self._results) > MAX_OPERATION_CACHE:
            self._results.popitem(last=False)

    def execute(self, request: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        canonical = json.dumps(
            {
                "operation": request["operation"],
                "parameters": request["parameters"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        request_id = str(request["requestId"])
        now = time.monotonic()
        operation = str(request["operation"])
        if operation in self.READ_ONLY_OPERATIONS:
            return self._execute_read(request, request_id, digest, now)
        if not self._lock.acquire(timeout=self.LOCK_WAIT_SECONDS):
            running = self._running
            detail = ("%s has been running for %d s" % (
                running[0], int(time.monotonic() - running[1])) if running else
                "another operation is still running")
            return 503, {
                "schema": PROJECT_OPERATION_RESULT_SCHEMA,
                "requestId": request_id,
                "error": {
                    "code": "project-service-busy",
                    "message": "Bread project service is busy: %s; try again when "
                               "it finishes" % detail,
                },
            }
        try:
            return self._execute_locked(request, request_id, digest, now)
        finally:
            self._lock.release()

    def _replay(self, request_id: str, digest: str) -> tuple[int, dict[str, Any]] | None:
        """The cached answer for a repeated requestId, or the 409 for a reused one."""

        prior = self._results.get(request_id)
        if prior is None:
            return None
        if not hmac.compare_digest(prior[1], digest):
            return 409, {
                "schema": PROJECT_OPERATION_RESULT_SCHEMA,
                "requestId": request_id,
                "error": {
                    "code": "request-id-conflict",
                    "message": "requestId was already used for another operation",
                },
            }
        self._results.move_to_end(request_id)
        return prior[2], dict(prior[3])

    def _execute_read(self, request: Mapping[str, Any], request_id: str,
                      digest: str, now: float) -> tuple[int, dict[str, Any]]:
        # Same requestId contract as a mutation, but the read itself runs
        # outside the lock; if the lock is busy the read is simply not
        # deduplicated -- it has no side effect to duplicate.
        if self._lock.acquire(timeout=0.5):
            try:
                self._purge(now)
                replayed = self._replay(request_id, digest)
                if replayed is not None:
                    return replayed
            finally:
                self._lock.release()
        try:
            result = _dispatch_project_operation(
                self.service, request["operation"], request["parameters"])
            if not isinstance(result, dict):
                raise RuntimeError("project service returned a non-object")
            status, response = 200, {"schema": PROJECT_OPERATION_RESULT_SCHEMA,
                                     "requestId": request_id, "result": result}
        except PermissionError as exc:
            status, response = 403, {
                "schema": PROJECT_OPERATION_RESULT_SCHEMA, "requestId": request_id,
                "error": {"code": "project-access-denied", "message": str(exc)[:4096]}}
        except (ActiveProjectError, ValueError) as exc:
            status, response = 400, {
                "schema": PROJECT_OPERATION_RESULT_SCHEMA, "requestId": request_id,
                "error": {"code": "project-operation-rejected", "message": str(exc)[:4096]}}
        except Exception as exc:
            status, response = 503, {
                "schema": PROJECT_OPERATION_RESULT_SCHEMA, "requestId": request_id,
                "error": {"code": "project-service-failed",
                          "message": ("%s: %s" % (type(exc).__name__, exc))[:4096]}}
        if self._lock.acquire(timeout=0.5):
            try:
                self._results[request_id] = (now, digest, status, dict(response))
                self._purge(now)
            finally:
                self._lock.release()
        return status, response

    def _execute_locked(self, request: Mapping[str, Any], request_id: str,
                        digest: str, now: float) -> tuple[int, dict[str, Any]]:
        self._purge(now)
        replayed = self._replay(request_id, digest)
        if replayed is not None:
            return replayed
        self._running = (str(request["operation"]), time.monotonic())
        try:
            result = _dispatch_project_operation(
                self.service, request["operation"], request["parameters"]
            )
            if not isinstance(result, dict):
                raise RuntimeError("project service returned a non-object")
            status = 200
            response = {
                "schema": PROJECT_OPERATION_RESULT_SCHEMA,
                "requestId": request_id,
                "result": result,
            }
        except PermissionError as exc:
            status, response = 403, {
                "schema": PROJECT_OPERATION_RESULT_SCHEMA, "requestId": request_id,
                "error": {"code": "project-access-denied", "message": str(exc)[:4096]}}
        except (ActiveProjectError, ValueError) as exc:
            status = 400
            response = {
                "schema": PROJECT_OPERATION_RESULT_SCHEMA,
                "requestId": request_id,
                "error": {
                    "code": "project-operation-rejected",
                    "message": str(exc)[:4096],
                },
            }
        except Exception as exc:
            status = 503
            response = {
                "schema": PROJECT_OPERATION_RESULT_SCHEMA,
                "requestId": request_id,
                "error": {
                    "code": "project-service-failed",
                    "message": ("%s: %s" % (type(exc).__name__, exc))[:4096],
                },
            }
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > MAX_PROJECT_RESPONSE_BYTES:
            status = 503
            response = {
                "schema": PROJECT_OPERATION_RESULT_SCHEMA,
                "requestId": request_id,
                "error": {
                    "code": "project-response-oversized",
                    "message": "project service response exceeds its byte limit",
                },
            }
        self._running = None
        self._results[request_id] = (now, digest, status, dict(response))
        self._purge(now)
        return status, response


async def _read_bounded_body(request: Any, maximum: int) -> bytes:
    headers = getattr(request, "headers", {})
    supplied = headers.get("content-length") if hasattr(headers, "get") else None
    if supplied not in (None, ""):
        try:
            declared = int(supplied)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("content-length is invalid") from exc
        if declared < 0 or declared > maximum:
            raise OverflowError("request body exceeds its byte limit")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        clean = chunk if isinstance(chunk, bytes) else bytes(chunk)
        total += len(clean)
        if total > maximum:
            raise OverflowError("request body exceeds its byte limit")
        chunks.append(clean)
    return b"".join(chunks)


class BearerAuthApp:
    """Authenticate every internal endpoint except the identity-only health."""

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


class _OwnerShaderSessionRefresh:
    """One reconciliation worker, independent of HTTP workers and health ticks."""

    def __init__(self, tool_service: InternalToolService):
        self._tool_service = tool_service
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="shader-owner-reconcile"
        )
        self._lock = threading.Lock()
        self._pending: Future | None = None
        self._closed = False

    @staticmethod
    def _consume(future: Future) -> None:
        try:
            future.result()
        except CancelledError:
            pass
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Shader owner reconciliation failed (%s): %s",
                type(exc).__name__, str(exc)[:1024],
            )

    def tick(self, owner_pids: list[int]) -> bool:
        with self._lock:
            if self._closed:
                return False
            if self._pending is not None:
                if not self._pending.done():
                    return False
                self._consume(self._pending)
                self._pending = None
            if not owner_pids:
                return False
            self._pending = self._executor.submit(
                self._tool_service.refresh_shader_sessions, list(owner_pids)
            )
            return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pending = self._pending
            self._pending = None
            if pending is not None:
                pending.cancel()
        # Cancelling a future cannot stop an already running synchronous job.
        # Drain it before shutting down the Shader manager or withdrawing Host
        # state, so reconciliation cannot create a watcher after service exit.
        self._executor.shutdown(wait=True, cancel_futures=True)
        if pending is not None:
            self._consume(pending)


def _serve(root: Path, port: int) -> int:
    import uvicorn
    from starlette.requests import Request as StarletteRequest
    from starlette.responses import JSONResponse
    from starlette.routing import Route, Router

    if not 1024 <= int(port) <= 65535:
        raise ValueError("Bread internal port must be between 1024 and 65535")
    _secure_directory(root)
    paths = _paths(root)
    token = _write_generation_token(root)
    instance_id = secrets.token_hex(16)
    os.environ["REPLAYLAB_HOST_INSTANCE_ID"] = instance_id
    try:
        paths["shutdown"].unlink()
    except FileNotFoundError:
        pass

    tool_service = InternalToolService()
    project_operations = ProjectOperationRegistry(tool_service.project)
    restore_jobs = tool_service.restore_jobs

    async def health_endpoint(_request: StarletteRequest) -> JSONResponse:
        return JSONResponse(
            {
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "hostStateProtocolVersion": INTERNAL_HOST_STATE_PROTOCOL_VERSION,
                "transport": "internal-http",
                "pid": os.getpid(),
                "hostInstanceId": instance_id,
                "capabilities": list(INTERNAL_HOST_CAPABILITIES),
                "capabilitySchema": INTERNAL_HOST_CAPABILITY_SCHEMA,
                "capabilityVersions": dict(INTERNAL_HOST_CAPABILITY_VERSIONS),
            }
        )

    async def project_operation_endpoint(request: StarletteRequest) -> JSONResponse:
        request_id = ""
        try:
            payload = await _read_bounded_body(request, MAX_PROJECT_REQUEST_BYTES)
            value = parse_project_operation_request(payload)
            request_id = value["requestId"]
            status_code, response = await asyncio.to_thread(
                project_operations.execute, value
            )
        except OverflowError as exc:
            status_code = 413
            response = {
                "schema": PROJECT_OPERATION_RESULT_SCHEMA,
                "requestId": request_id,
                "error": {"code": "request-oversized", "message": str(exc)[:4096]},
            }
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            status_code = 400
            response = {
                "schema": PROJECT_OPERATION_RESULT_SCHEMA,
                "requestId": request_id,
                "error": {"code": "invalid-request", "message": str(exc)[:4096]},
            }
        return JSONResponse(response, status_code=status_code)

    async def restore_post_endpoint(request: StarletteRequest) -> JSONResponse:
        status_code, value = await project_restore_post_response(request, restore_jobs)
        return JSONResponse(value, status_code=status_code)

    async def restore_get_endpoint(request: StarletteRequest) -> JSONResponse:
        status_code, value = project_restore_get_response(
            request.path_params.get("job_id"),
            request.query_params.get("after", "0"),
            restore_jobs,
        )
        return JSONResponse(value, status_code=status_code)

    async def restore_delete_endpoint(request: StarletteRequest) -> JSONResponse:
        status_code, value = project_restore_delete_response(
            request.path_params.get("job_id"), restore_jobs
        )
        return JSONResponse(value, status_code=status_code)

    # Initialize the closed Shader owner before health. Durable resume runs
    # through the registered-owner tick once the GUI can service replay calls.
    try:
        tool_service.start()
    except Exception:
        tool_service.shutdown()
        raise

    async def shader_source_save_endpoint(
        request: StarletteRequest,
    ) -> JSONResponse:
        try:
            payload = await _read_shader_editor_save_body(request)
        except ShaderEditorSaveRequestError as exc:
            return JSONResponse(
                {"error": str(exc)[:2048]}, status_code=exc.status_code
            )
        status_code, value = await asyncio.to_thread(
            tool_service.save_shader_source, payload
        )
        return JSONResponse(value, status_code=status_code)

    async def shader_source_check_endpoint(
        request: StarletteRequest,
    ) -> JSONResponse:
        # Read-only compile-check of an unsaved buffer: same bounded body
        # reader and byte limits as save, but it never writes or applies.
        try:
            payload = await _read_shader_editor_save_body(request)
        except ShaderEditorSaveRequestError as exc:
            return JSONResponse(
                {"error": str(exc)[:2048]}, status_code=exc.status_code
            )
        status_code, value = await asyncio.to_thread(
            tool_service.check_shader_source, payload
        )
        return JSONResponse(value, status_code=status_code)

    async def shader_live_edit_endpoint(request: StarletteRequest) -> JSONResponse:
        try:
            payload = await _read_bounded_body(request, MAX_SHADER_LIVE_EDIT_OPERATION_BYTES)
            status_code, value = await asyncio.to_thread(tool_service.shader_live_edit_operation, payload)
        except OverflowError as exc:
            return JSONResponse({"error": str(exc)[:2048]}, status_code=413)
        value["hostInstanceId"] = instance_id
        return JSONResponse(value, status_code=status_code)

    async def asset_index_endpoint(request: StarletteRequest) -> JSONResponse:
        try:
            payload = await _read_bounded_body(request, MAX_ASSET_INDEX_OPERATION_BYTES)
            status_code, value = await asyncio.to_thread(tool_service.asset_index_operation, payload)
        except OverflowError as exc:
            return JSONResponse({"error": str(exc)[:2048]}, status_code=413)
        value["hostInstanceId"] = instance_id
        return JSONResponse(value, status_code=status_code)

    # This is intentionally a plain Router, never a general MCP application.
    # Adding a route requires an explicit product-level contract review.
    # asset-index (2026-09): the on-demand unpacked-asset scan must outlive one
    # AI task, so it runs here and task processes only start/poll it.
    app = Router(
        routes=[
            Route("/health", health_endpoint, methods=["GET"]),
            Route(
                "/replaylab/internal/project-operation",
                project_operation_endpoint,
                methods=["POST"],
            ),
            Route(
                "/replaylab/project-restore",
                restore_post_endpoint,
                methods=["POST"],
            ),
            Route(
                "/replaylab/project-restore/{job_id}",
                restore_get_endpoint,
                methods=["GET"],
            ),
            Route(
                "/replaylab/project-restore/{job_id}",
                restore_delete_endpoint,
                methods=["DELETE"],
            ),
            Route(
                "/replaylab/shader-source/save",
                shader_source_save_endpoint,
                methods=["POST"],
            ),
            Route(
                "/replaylab/shader-source/check",
                shader_source_check_endpoint,
                methods=["POST"],
            ),
            Route("/replaylab/shader-live-edit", shader_live_edit_endpoint, methods=["POST"]),
            Route("/replaylab/asset-index", asset_index_endpoint, methods=["POST"]),
        ]
    )
    protected = BearerAuthApp(app, token)
    started_ms = int(time.time() * 1000)
    empty_since: float | None = None
    server_box: dict[str, Any] = {}
    shader_session_refresh = _OwnerShaderSessionRefresh(tool_service)

    def publish_state() -> None:
        _atomic_json(
            paths["state"],
            {
                "schema": STATE_SCHEMA,
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "hostStateProtocolVersion": INTERNAL_HOST_STATE_PROTOCOL_VERSION,
                "pid": os.getpid(),
                "hostInstanceId": instance_id,
                "host": "127.0.0.1",
                "port": int(port),
                "endpoint": "http://127.0.0.1:%d/replaylab/internal" % int(port),
                "tokenFile": str(paths["token"]),
                "startedUnixMs": started_ms,
                "heartbeatUnixMs": int(time.time() * 1000),
                "ownerPids": live_owners(root),
                "capabilities": list(INTERNAL_HOST_CAPABILITIES),
                "capabilitySchema": INTERNAL_HOST_CAPABILITY_SCHEMA,
                "capabilityVersions": dict(INTERNAL_HOST_CAPABILITY_VERSIONS),
            },
        )

    async def monitor() -> None:
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
        if owners and not paths["shutdown"].exists():
            # The private Host can outlive a GUI. Its existing owner tick must
            # retire closed capture sessions and resume durable consent for a
            # newly opened registered GUI even when no MCP client lists jobs.
            # Uvicorn awaits this notification before scheduling its next
            # heartbeat. Never await reconciliation here: a lifecycle lock or
            # queued HTTP poll can outlast the five-second liveness contract.
            # A dedicated single worker also avoids the HTTP executor queue.
            shader_session_refresh.tick(owners)

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
    try:
        server.run()
    finally:
        server.should_exit = True
        shader_session_refresh.close()
        tool_service.shutdown()
        state = _load_json(paths["state"])
        if state is not None and state.get("pid") == os.getpid():
            try:
                paths["state"].unlink()
            except FileNotFoundError:
                pass
        try:
            paths["shutdown"].unlink()
        except FileNotFoundError:
            pass
    return 0


def status(root: Path) -> dict[str, Any]:
    port = _state_port(root)
    current = health(port) if port else None
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "running": current is not None,
        "port": port,
        "endpoint": (
            "http://127.0.0.1:%d/replaylab/internal" % port if port else None
        ),
        "owners": live_owners(root),
        "state": _load_json(_paths(root)["state"]),
        "tokenFile": str(_paths(root)["token"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ReplayLabInternal",
        description="Private high-level project service for qrenderdoc",
    )
    parser.add_argument(
        "command", choices=("serve", "ensure", "release", "status", "project-request")
    )
    parser.add_argument("--runtime-dir")
    parser.add_argument("--port", type=int)
    parser.add_argument("--owner-pid", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if arguments.command=="project-request":
        from .project_http_helper import main as project_request
        return project_request()
    root = (
        Path(arguments.runtime_dir).expanduser().resolve()
        if arguments.runtime_dir
        else runtime_root()
    )
    if arguments.command == "serve":
        return _serve(root, choose_port(root, arguments.port))
    if arguments.command == "ensure":
        if not arguments.owner_pid:
            raise SystemExit("ensure requires --owner-pid")
        print(
            json.dumps(
                ensure(root, arguments.owner_pid, arguments.port),
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 0
    if arguments.command == "release":
        if not arguments.owner_pid:
            raise SystemExit("release requires --owner-pid")
        print(
            json.dumps(
                {"released": release_owner(root, arguments.owner_pid)},
                sort_keys=True,
            )
        )
        return 0
    if arguments.command == "status":
        print(json.dumps(status(root), ensure_ascii=True, sort_keys=True))
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BearerAuthApp",
    "PROJECT_OPERATION_RESULT_SCHEMA",
    "PROJECT_OPERATION_SCHEMA",
    "ProjectOperationRegistry",
    "SERVICE_NAME",
    "STATE_SCHEMA",
    "ensure",
    "main",
    "parse_project_operation_request",
    "runtime_root",
]
