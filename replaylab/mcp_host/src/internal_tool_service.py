"""Controlled high-level services shared by ReplayLab's private runtime.

This module deliberately contains no MCP transport, model/provider integration,
or desktop-app routing. It owns the bounded project-restore and Shader editor
save contracts consumed by ReplayLabInternal.exe. External adapters may reuse
these data-plane helpers, but cannot turn this module into a general tool
surface.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
import copy
from dataclasses import dataclass
from functools import lru_cache
import hmac
import inspect
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from typing import Any, Callable, Mapping

from .active_projects import ActiveReplayProjectService
from .shader_live_edit import (
    ShaderLiveEditDeferred,
    ShaderLiveEditManager,
    ShaderLiveEditSession,
    ShaderLiveEditSessionUnavailable,
    capture_bindings_match,
)


_AUTOMATIC_SHADER_RESUME_ALLOWED: ContextVar[Callable[[], bool] | None] = ContextVar(
    "automatic_shader_resume_allowed", default=None)
_SHADER_LIVE_START_TIMING: ContextVar[dict[str, Any] | None] = ContextVar(
    "shader_live_start_timing", default=None)


@contextmanager
def _shader_live_start_phase(phase: str):
    """Measure private owner-start work without adding filesystem or IPC reads."""
    timing = _SHADER_LIVE_START_TIMING.get()
    if timing is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    except BaseException:
        if not timing["failedPhase"]:
            timing["failedPhase"] = phase
        raise
    finally:
        phases = timing["phasesMs"]
        phases[phase] = round(phases.get(phase, 0.0) + (time.perf_counter() - started) * 1000, 3)


def _shader_live_start_timing_result(timing: dict[str, Any], started: float) -> dict[str, Any]:
    return {"totalMs": round((time.perf_counter() - started) * 1000, 3),
            "phasesMs": dict(timing["phasesMs"]), "failedPhase": timing["failedPhase"]}


def _automatic_shader_resume_checkpoint() -> None:
    allowed = _AUTOMATIC_SHADER_RESUME_ALLOWED.get()
    if allowed is not None and not allowed():
        raise ShaderLiveEditDeferred("automatic Shader resume deferred to project restore")


ACTIVE_PROJECT_STATE_ENV = "RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE"
ACTIVE_PROJECTS_HOME_ENV = "RENDERDOC_ULTRA_PROJECTS_HOME"
PROJECT_ASSOCIATION_LEASE_SCHEMA = "replaylab-project-association-lease/v1"
PROJECT_RESTORE_JOB_SCHEMA = "replaylab-project-restore-job/v1"
PROJECT_RESTORE_JOB_VERSION = 1
PROJECT_RESTORE_SESSION_PATTERN = re.compile(r"^[0-9a-f]{32}$")
PROJECT_RESTORE_JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MAX_PROJECT_RESTORE_REQUEST_BYTES = 64 * 1024
MAX_PROJECT_RESTORE_PATH_BYTES = 32 * 1024
MAX_PROJECT_RESTORE_PROGRESS_EVENTS = 512
MAX_PROJECT_RESTORE_PROGRESS_PAGE = 128
MAX_PROJECT_RESTORE_PROGRESS_BYTES = 16 * 1024
MAX_PROJECT_RESTORE_PROGRESS_COUNT = 1_000_000_000
MAX_PROJECT_RESTORE_RESULT_BYTES = 8 * 1024 * 1024
MAX_PROJECT_RESTORE_ERROR_BYTES = 4096
MAX_PROJECT_RESTORE_ACTIVE_JOBS = 8
MAX_PROJECT_RESTORE_HISTORY_JOBS = 32
PROJECT_RESTORE_HISTORY_TTL_SECONDS = 15 * 60.0
# One restore may spend most of this budget in the full-frame RenderDoc export.
# Cancellation/deadline enforcement is cooperative: a single in-flight IPC is
# allowed to return (or hit its own timeout), then the restore checkpoints and
# rolls back all transaction-ledger state.  The desktop client keeps an
# additional cleanup/transport margin beyond this server-owned deadline.
PROJECT_RESTORE_DEADLINE_SECONDS = 30 * 60.0
PROJECT_RESTORE_CLEANUP_GRACE_SECONDS = 5 * 60.0
PROJECT_RESTORE_CANCELLED_ERROR = "Bread project restore was cancelled"
PROJECT_RESTORE_DEADLINE_ERROR = (
    "Bread project restore exceeded its server deadline"
)
SHADER_EDITOR_SAVE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SHADER_EDITOR_SAVE_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_SHADER_EDITOR_SOURCE_BYTES = 4 * 1024 * 1024
MAX_SHADER_EDITOR_SAVE_REQUEST_BYTES = MAX_SHADER_EDITOR_SOURCE_BYTES + 64 * 1024
MAX_SHADER_EDITOR_CHECK_DIAGNOSTICS = 128
SHADER_LIVE_EDIT_OPERATION_SCHEMA = "replaylab-internal-shader-live-edit/v1"
MAX_SHADER_LIVE_EDIT_OPERATION_BYTES = MAX_SHADER_EDITOR_SAVE_REQUEST_BYTES
ASSET_INDEX_OPERATION_SCHEMA = "replaylab-internal-asset-index/v1"
MAX_ASSET_INDEX_OPERATION_BYTES = 4 * 1024
_ASSET_INDEX_DEPTHS = frozenset({"names", "headers", "full"})


def parse_asset_index_operation(payload: bytes) -> dict[str, Any]:
    """``ensure`` (start or join the background scan) / ``status``; no paths accepted.

    The active project, its asset root and the managed catalog path are
    resolved by this process from the project state, never from the request.
    """

    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_ASSET_INDEX_OPERATION_BYTES:
        raise ValueError("asset index request has an invalid size")
    value = json.loads(payload.decode("utf-8"), object_pairs_hook=_duplicate_shader_editor_json_object)
    if not isinstance(value, dict) or set(value) != {"schema", "requestId", "operation", "depth"}:
        raise ValueError("asset index request fields are invalid")
    if value["schema"] != ASSET_INDEX_OPERATION_SCHEMA or SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(str(value["requestId"])) is None:
        raise ValueError("asset index request identity is invalid")
    if value["operation"] not in {"ensure", "status"}:
        raise ValueError("asset index operation is invalid")
    if not isinstance(value["depth"], str) or value["depth"] not in _ASSET_INDEX_DEPTHS:
        raise ValueError("asset index depth is invalid")
    return value


def _save_workspace_source_now(**parameters) -> dict[str, Any]:
    from . import server

    active = server._active_project_service()
    selected = active.status()
    root = server._require_managed_active_workspace(selected, parameters["workspace_path"])
    parameters["workspace_path"] = str(root)
    with active.snapshot_active_project(
        str(selected.get("workspaceId") or ""), str(selected.get("projectRoot") or "")
    ) as snapshot:
        return server._shader_organization_manager().save_editor_source(
            **parameters,
            commit_guard=lambda: active.commit_active_project_snapshot(snapshot))


def _source_document_now(operation, parameters):
    from . import server
    from .shader_workspace.source_documents import open_source_document, save_source_document
    active = server._active_project_service()
    selected = active.status()
    parameters["workspace_path"] = str(server._require_managed_active_workspace(
        selected, parameters["workspace_path"]))
    with active.snapshot_active_project(str(selected.get("workspaceId") or ""),
                                       str(selected.get("projectRoot") or "")) as snapshot:
        method = open_source_document if operation == "open" else save_source_document
        return method(server._shader_organization_manager(), **parameters,
                      commit_guard=lambda: active.commit_active_project_snapshot(snapshot))


def _open_workspace_source_document_now(**parameters):
    return _source_document_now("open", parameters)


def _save_workspace_source_document_now(**parameters):
    return _source_document_now("save", parameters)


def _save_and_apply_editor_source(manager, parameters, *, binding, include=False):
    """A user save owns both durable text and a separate manual compile request.

    Compilation/application is asynchronous and checks the physical interface,
    never organization equivalence: the user is allowed to change the image.
    """
    saved = (_save_workspace_source_document_now(**parameters) if include else
             _save_workspace_source_now(**parameters))
    result = dict(saved, sourceOnly=False, compileScheduled=False)
    try:
        expected = saved.get("rootSourceSha256") if include else saved.get("rawSha256")
        queued = manager.compile_saved_source(parameters["workspace_path"],
            parameters["source_workspace_id"], expected, capture_binding=binding)
        result.update(queued)
    except Exception as error:
        # A successfully saved file stays saved even when the current capture
        # cannot start replay. Expose that failure without losing the text or
        # pretending its effects were applied. Ctrl+S can retry the same bytes.
        result["autoApplyError"] = str(error)[:2048]
    return result


def parse_shader_live_edit_operation(payload: bytes) -> dict[str, Any]:
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_SHADER_LIVE_EDIT_OPERATION_BYTES:
        raise ValueError("Shader live-edit request has an invalid size")
    value = json.loads(payload.decode("utf-8"), object_pairs_hook=_duplicate_shader_editor_json_object)
    if not isinstance(value, dict) or set(value) != {"schema", "requestId", "operation", "parameters", "binding"}:
        raise ValueError("Shader live-edit request fields are invalid")
    if value["schema"] != SHADER_LIVE_EDIT_OPERATION_SCHEMA or SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(str(value["requestId"])) is None:
        raise ValueError("Shader live-edit request identity is invalid")
    fields = {"start": {"workspace_path", "debounce_ms"}, "list": set(),
              "poll": {"live_edit_id"}, "stop": {"live_edit_id"},
              "migrate": {"source_workspace_path", "workspace_path", "expected_source_sha256"},
              "convert-hlsl": {"source_workspace_path", "expected_source_sha256"},
              # The editor's "use compiled HLSL / use original Shader" switch.
              "apply": {"workspace_path"},
              "compiler-contract": {"workspace_path", "source_workspace_id", "expected_raw_sha256"},
              "save-source": {"workspace_path", "source_workspace_id", "expected_raw_sha256", "text"},
              "save-and-apply-source": {"workspace_path", "source_workspace_id", "expected_raw_sha256", "text"},
              "open-source-document": {"workspace_path", "source_workspace_id", "requested_path"},
              "save-source-document": {"workspace_path", "source_workspace_id", "document_id",
                                       "expected_raw_sha256", "expected_source_sha256", "text"},
              "save-and-apply-source-document": {"workspace_path", "source_workspace_id", "document_id",
                                       "expected_raw_sha256", "expected_source_sha256", "text"},
              "restore": {"workspace_path", "replacement_id"}}
    parameters = value["parameters"]
    if value["operation"] not in fields or not isinstance(parameters, dict) or set(parameters) != fields[value["operation"]]:
        raise ValueError("Shader live-edit operation or parameters are invalid")
    for key, item in parameters.items():
        if key == "debounce_ms":
            if isinstance(item, bool) or not isinstance(item, int) or not 100 <= item <= 5000:
                raise ValueError("Shader live-edit debounce is invalid")
        elif key.endswith("_path"):
            if not isinstance(item, str) or not item or len(item) > 32768 or "\0" in item or not Path(item).is_absolute():
                raise ValueError("Shader live-edit workspace path is invalid")
        elif key == "text":
            if (not isinstance(item, str) or
                    (value["operation"] not in {"save-source-document", "save-and-apply-source-document"} and not item.strip()) or "\0" in item
                    or len(item.encode("utf-8", errors="strict")) > MAX_SHADER_EDITOR_SOURCE_BYTES):
                raise ValueError("Shader source text is invalid")
        elif key in {"live_edit_id", "replacement_id", "source_workspace_id"}:
            if not isinstance(item, str) or SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(item) is None:
                raise ValueError("Shader live-edit ID is invalid")
        elif not isinstance(item, str) or SHADER_EDITOR_SAVE_SHA256_PATTERN.fullmatch(item) is None:
            raise ValueError("Shader live-edit source hash is invalid")
    binding = value["binding"]
    if not isinstance(binding, dict) or set(binding) != {"instanceId", "captureSessionId", "capturePath"}:
        raise ValueError("Shader live-edit capture binding is invalid")
    if (not isinstance(binding["instanceId"], str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,512}", binding["instanceId"]) is None
            or binding["instanceId"] in {".", ".."}
            or not isinstance(binding["captureSessionId"], str)
            or SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(binding["captureSessionId"]) is None
            or not isinstance(binding["capturePath"], str)
            or not binding["capturePath"] or "\0" in binding["capturePath"]
            or not Path(binding["capturePath"]).is_absolute()):
        raise ValueError("Shader live-edit capture binding is invalid")
    return value

def active_project_paths(
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, Path]:
    """Resolve the exact process-owner project roots used by the MCP server."""

    env = os.environ if environment is None else environment
    local = str(env.get("LOCALAPPDATA", "") or "").strip()
    if local:
        replaylab_root = Path(local).expanduser() / "ReplayLab"
    else:
        xdg = str(env.get("XDG_DATA_HOME", "") or "").strip()
        data_root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
        replaylab_root = data_root / "ReplayLab"
    configured_state = str(env.get(ACTIVE_PROJECT_STATE_ENV, "") or "").strip()
    configured_home = str(env.get(ACTIVE_PROJECTS_HOME_ENV, "") or "").strip()
    state_path = (
        Path(configured_state).expanduser()
        if configured_state
        else replaylab_root / "config" / "active-project.json"
    )
    projects_home = (
        Path(configured_home).expanduser()
        if configured_home
        else replaylab_root / "projects"
    )
    return state_path.resolve(), projects_home.resolve()


def project_service(environment: Mapping[str, str] | None = None):
    state_path, projects_home = active_project_paths(environment)
    env = os.environ if environment is None else environment
    return _project_service_cached(str(state_path), str(projects_home),
        str(env.get("RENDERDOC_ULTRA_PROJECT_REGISTRY") or ""))


@lru_cache(maxsize=16)
def _project_service_cached(state_path, projects_home, registry_path):
    # Reuse service/index state for one application. Individual metadata reads
    # still observe current file stamps; caching is not a project-wide lock.
    return ActiveReplayProjectService(state_path, projects_home, registry_path=registry_path or None)


def project_association_guard(
    lease_path: str, lease_token: str, expected_capture_path: str
):
    """Return a fail-closed live-capture lease checker for qrenderdoc."""

    token = str(lease_token or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("project association lease token is invalid")
    raw = Path(str(lease_path or "")).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise ValueError("project association lease path is invalid")
    lease = raw.resolve()
    expected_capture = os.path.normcase(
        str(Path(expected_capture_path).expanduser().resolve())
    )
    local = (str(os.environ.get("LOCALAPPDATA", "") or "").strip()
             or str(os.environ.get("XDG_DATA_HOME", "") or "").strip())
    boundary = (
        (Path(local).expanduser() if local else Path.home() / ".local" / "share")
        / "ReplayLab" / "ipc-v2" / "instances"
    ).resolve()
    try:
        if os.path.commonpath((str(boundary), str(lease))) != str(boundary):
            raise ValueError("project association lease escapes Bread IPC")
    except ValueError as exc:
        raise ValueError("project association lease escapes Bread IPC") from exc

    def _guard() -> bool:
        if lease.is_symlink() or not lease.is_file():
            return False
        try:
            if lease.stat().st_size > 16 * 1024:
                return False
            value = json.loads(lease.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        try:
            lease_capture = os.path.normcase(
                str(Path(value.get("capturePath", "")).expanduser().resolve())
            )
        except (OSError, TypeError, ValueError):
            return False
        return bool(
            isinstance(value, dict)
            and value.get("schema") == PROJECT_ASSOCIATION_LEASE_SCHEMA
            and hmac.compare_digest(str(value.get("token", "")), token)
            and value.get("active") is True
            and lease_capture == expected_capture
        )

    return _guard

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


class ProjectRestoreRequestError(ValueError):
    """A bounded project-restore HTTP request failed validation."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = int(status_code)


class ProjectRestoreCapacityError(RuntimeError):
    """The bounded restore queue cannot accept another distinct active key."""


@dataclass
class _ProjectRestoreJob:
    job_id: str
    key: tuple[str, str, str]
    project_root: str
    capture_path: str
    capture_session_id: str
    status: str
    created_monotonic: float
    created_unix_ms: int
    updated_unix_ms: int
    latest_sequence: int
    progress: deque[dict[str, Any]]
    cancel_event: threading.Event
    deadline_monotonic: float
    completed_monotonic: float | None = None
    completed_order: int | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    persisted_sequence: int = 0
    result_finalized: bool = False
    final_progress: list[dict[str, Any]] | None = None


def _bounded_restore_text(
    value: Any,
    label: str,
    maximum_bytes: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ProjectRestoreRequestError("%s must be a string" % label)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProjectRestoreRequestError("%s must be valid UTF-8" % label) from exc
    if len(encoded) > maximum_bytes:
        raise ProjectRestoreRequestError("%s exceeds its byte limit" % label)
    if any(ord(character) < 0x20 for character in value):
        raise ProjectRestoreRequestError("%s contains control characters" % label)
    return value


def _canonical_restore_path(value: Any, label: str, *, directory: bool) -> str:
    text = _bounded_restore_text(value, label, MAX_PROJECT_RESTORE_PATH_BYTES)
    raw = Path(text)
    if not raw.is_absolute():
        raise ProjectRestoreRequestError("%s must be an absolute path" % label)
    if raw.is_symlink():
        raise ProjectRestoreRequestError("%s must not be a symlink" % label)
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectRestoreRequestError("%s does not exist" % label) from exc
    lexical = Path(os.path.abspath(str(raw)))
    if os.path.normcase(str(lexical)) != os.path.normcase(str(resolved)):
        raise ProjectRestoreRequestError("%s must not be redirected or aliased" % label)
    if directory:
        if not resolved.is_dir():
            raise ProjectRestoreRequestError("%s must be a directory" % label)
    else:
        if not resolved.is_file():
            raise ProjectRestoreRequestError("%s must be a regular file" % label)
        if resolved.suffix.casefold() != ".rdc":
            raise ProjectRestoreRequestError("capturePath must name an .rdc file")
    return str(resolved)


def _duplicate_restore_json_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectRestoreRequestError(
                "project restore request contains duplicate JSON key: %s" % key
            )
        result[key] = value
    return result


def _reject_restore_json_constant(value: str) -> None:
    raise ProjectRestoreRequestError(
        "project restore request contains non-finite JSON: %s" % value
    )


def parse_project_restore_request(payload: bytes | bytearray | memoryview) -> dict[str, str]:
    """Parse the exact, bounded POST body and bind it to local physical paths."""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ProjectRestoreRequestError("project restore body must be bytes")
    raw = bytes(payload)
    if not raw:
        raise ProjectRestoreRequestError("project restore body is empty")
    if len(raw) > MAX_PROJECT_RESTORE_REQUEST_BYTES:
        raise ProjectRestoreRequestError(
            "project restore body exceeds its byte limit", status_code=413
        )
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_restore_json_object,
            parse_constant=_reject_restore_json_constant,
        )
    except UnicodeDecodeError as exc:
        raise ProjectRestoreRequestError(
            "project restore body must be UTF-8 JSON"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ProjectRestoreRequestError("project restore body is invalid JSON") from exc
    expected = {"projectRoot", "capturePath", "captureSessionId"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ProjectRestoreRequestError("project restore fields are invalid")
    session_id = value["captureSessionId"]
    if (
        not isinstance(session_id, str)
        or PROJECT_RESTORE_SESSION_PATTERN.fullmatch(session_id) is None
    ):
        raise ProjectRestoreRequestError(
            "captureSessionId must be 32 lowercase hexadecimal characters"
        )
    return {
        "projectRoot": _canonical_restore_path(
            value["projectRoot"], "projectRoot", directory=True
        ),
        "capturePath": _canonical_restore_path(
            value["capturePath"], "capturePath", directory=False
        ),
        "captureSessionId": session_id,
    }


async def _read_project_restore_body(request: Any) -> bytes:
    """Read a Starlette-like request without buffering an unbounded body."""

    headers = getattr(request, "headers", {})
    supplied_length = headers.get("content-length") if hasattr(headers, "get") else None
    if supplied_length not in (None, ""):
        try:
            declared = int(supplied_length)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProjectRestoreRequestError("content-length is invalid") from exc
        if declared < 0:
            raise ProjectRestoreRequestError("content-length is invalid")
        if declared > MAX_PROJECT_RESTORE_REQUEST_BYTES:
            raise ProjectRestoreRequestError(
                "project restore body exceeds its byte limit", status_code=413
            )
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        total += len(chunk)
        if total > MAX_PROJECT_RESTORE_REQUEST_BYTES:
            raise ProjectRestoreRequestError(
                "project restore body exceeds its byte limit", status_code=413
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _bounded_restore_json_object(
    value: Any, label: str, maximum_bytes: int
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("%s must be an object" % label)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("%s must be finite JSON" % label) from exc
    if len(encoded) > maximum_bytes:
        raise ValueError("%s exceeds its byte limit" % label)
    decoded = json.loads(encoded.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("%s must be an object" % label)
    return decoded


def _bounded_restore_error(error: BaseException) -> str:
    text = "%s: %s" % (type(error).__name__, error)
    text = "".join(character if ord(character) >= 0x20 else " " for character in text)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_PROJECT_RESTORE_ERROR_BYTES:
        return encoded.decode("utf-8")
    suffix = b"..."
    clipped = encoded[: MAX_PROJECT_RESTORE_ERROR_BYTES - len(suffix)]
    return clipped.decode("utf-8", errors="ignore") + suffix.decode("ascii")


def _normalise_restore_progress(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("restore progress must be an object")
    phase = _bounded_restore_text(value.get("phase"), "progress.phase", 128)
    message = _bounded_restore_text(
        value.get("message"), "progress.message", 1024, allow_empty=True
    )
    item = _bounded_restore_text(
        value.get("item", ""), "progress.item", 1024, allow_empty=True
    )
    current = value.get("current")
    total = value.get("total")
    for count, label in ((current, "progress.current"), (total, "progress.total")):
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or count > MAX_PROJECT_RESTORE_PROGRESS_COUNT
        ):
            raise ValueError("%s must be a bounded non-negative integer" % label)
    clean = {
        "phase": phase,
        "message": message,
        "item": item,
        "current": current,
        "total": total,
    }
    _bounded_restore_json_object(clean, "restore progress", MAX_PROJECT_RESTORE_PROGRESS_BYTES)
    return clean


def _run_project_restore(
    project_root: str,
    capture_path: str,
    capture_session_id: str,
    *,
    progress_callback: Callable[[dict[str, Any]], None],
    cancel_event: threading.Event,
    deadline_monotonic: float,
    post_restore_callback: Callable[[Callable[[], dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    """Import the restore implementation only inside the background worker."""

    from .project_restore import restore_active_capture_project

    return restore_active_capture_project(
        project_root=project_root,
        capture_path=capture_path,
        capture_session_id=capture_session_id,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
        deadline_monotonic=deadline_monotonic,
        post_restore_callback=post_restore_callback,
    )


def _restore_result_record_path(project_root: str, capture_path: str) -> Path:
    from .rdc_shader_inventory_cache import capture_path_sha256

    return (Path(project_root) / ".replaylab" / "replay-project" / "derived"
            / "restore-results" / (capture_path_sha256(capture_path) + ".json"))


def _convert_workspace_to_hlsl_now(source_workspace_path: str,
                                   expected_source_sha256: str) -> dict[str, Any]:
    """Convert saved source without replacing it or activating an invalid result."""
    from . import server

    active = server._active_project_service()
    selected = active.status()
    root = server._require_managed_active_workspace(selected, source_workspace_path)
    with active.snapshot_active_project(str(selected.get("workspaceId") or ""),
                                       str(selected.get("projectRoot") or "")) as snapshot:
        envelope = json.loads(server.convert_shader_workspace_to_hlsl(
            str(root), expected_source_sha256))
        if envelope.get("error"):
            raise RuntimeError(str(envelope["error"])[:4096])
        result = envelope.get("result") or {}
        if result.get("readyForEditing") is not True:
            validation = result.get("abiValidation") or {}
            raise RuntimeError(str(validation.get("error") or
                                   "HLSL conversion did not pass interface validation")[:4096])
        active.commit_active_project_snapshot(snapshot)
        shown = json.loads(server.show_shader_workspace_source(result["workspacePath"]))
        if shown.get("error"):
            raise RuntimeError(str(shown["error"])[:4096])
    # Compiler/reflection receipts can exceed the editor transport budget. Keep
    # them in the workspace and return only source/navigation facts here.
    return {key: result[key] for key in (
        "workspacePath", "sourcePath", "sourceWorkspacePath", "sourcePreserved",
        "previousSourceSha256", "sourceSha256", "preservedSourcePath",
        "conversionReceiptPath", "readyForEditing", "applied") if key in result}


def _apply_workspace_candidate_now(workspace_path: str) -> dict[str, Any]:
    """Switch the captured Shader to this workspace's compiled HLSL.

    The latest compiled candidate is applied as it is when it was built from the
    source currently on disk; only a changed source is compiled first. Nothing
    is recompiled for a switch back and forth.
    """

    from . import server

    service = server._shader_workspace_service()
    status = service.status(workspace_path)
    source = status.get("source") or {}
    latest = status.get("latestCompiled") or {}
    current = str(source.get("currentSha256") or "")
    compiled = False
    if not (isinstance(latest, dict) and latest.get("sourceSha256") == current
            and status.get("sourceDependenciesCurrent", True) is True
            and isinstance(latest.get("revision"), int) and latest.get("rawSha256")):
        built = getattr(service, "compile_for_live_edit", service.compile)(workspace_path)
        latest = {"revision": built.get("revision"), "rawSha256": built.get("rawSha256"),
                  "sourceSha256": built.get("sourceSha256")}
        compiled = True
    result = server._shader_live_edit_apply_candidate(
        workspace_path, int(latest["revision"]), str(latest["rawSha256"]))
    replacement = result.get("replacement") if isinstance(result, dict) else None
    if not isinstance(result, dict) or result.get("error") or not isinstance(replacement, dict):
        detail = (result.get("error") if isinstance(result, dict) else None) or "no replacement evidence"
        raise RuntimeError("apply did not replace the captured Shader: %s" % str(detail)[:1500])
    return {
        "operation": "apply",
        "workspacePath": workspace_path,
        "revision": int(latest["revision"]),
        "candidateRawSha256": str(latest["rawSha256"]),
        "compiled": compiled,
        "applied": True,
        "replacementId": replacement.get("replacementId"),
        "result": _switch_result_summary(result),
    }


_SWITCH_RESULT_KEYS = ("scope", "replacement", "rollback", "verification", "status",
                       "revision", "rawSha256", "sourceSha256", "evidence", "message")


def _switch_result_summary(value: Any) -> Any:
    """Keep the parts of an apply/rollback reply the switch button reads.

    The full replacement receipt carries manifest excerpts that can exceed the
    bridge client's response budget; the button needs the identifiers only.
    """

    if not isinstance(value, dict):
        return value
    summary = {key: value[key] for key in _SWITCH_RESULT_KEYS if key in value}
    encoded = json.dumps(summary, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) > 16384:
        summary = {key: summary[key] for key in ("scope", "replacement", "rollback", "status")
                   if key in summary}
    return summary


def _restore_workspace_original(workspace_path: str, replacement_id: str) -> dict[str, Any]:
    """Put the captured Shader back for every use; the workspace keeps its candidate."""

    from . import server

    raw = server.rollback_shader_workspace_shared_resource(workspace_path, replacement_id)
    value = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(value, dict) and value.get("error"):
        raise RuntimeError(str(value.get("error"))[:2048])
    return {"operation": "restore", "workspacePath": workspace_path,
            "replacementId": replacement_id, "result": _switch_result_summary(value)}


def persist_restore_result(project_root: str, capture_path: str,
                           result: Mapping[str, Any] | None, error: str | None,
                           *, running: bool = False,
                           progress: list[Mapping[str, Any]] | None = None,
                           job_metadata: Mapping[str, Any] | None = None) -> None:
    """Keep the last restore outcome for this capture where a person can read it.

    The job registry forgets a result minutes after the GUI reads it, and the
    GUI shows one status line. When a Shader the project knows was not restored,
    the question is always "was it looked for, and what did the lookup say" --
    ``targetLookup`` (observed / unobserved project heads) and the per-Shader
    items answer it, so they are written next to the project's other derived
    files. Best effort: a write failure never changes the restore's outcome.
    """

    try:
        nested_error = result.get("error") if isinstance(result, Mapping) else None
        effective_error = error or nested_error
        record: dict[str, Any] = {
            "schema": "replaylab-project-restore-result/v1",
            "schemaVersion": 1,
            "capturePath": str(capture_path),
            "status": "running" if running else ("failed" if effective_error else "finished"),
            ("updatedAt" if running else "finishedAt"):
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error": (str(effective_error)[:4096] if effective_error else None),
        }
        if progress:
            # Timed phases: where a nine-minute restore spent its time.
            record["progress"] = copy.deepcopy(list(progress)[-48:])
        if running and isinstance(result, Mapping):
            record.update({key: copy.deepcopy(value) for key, value in result.items()})
        elif isinstance(result, Mapping):
            for key in ("operationId", "completed", "outcome", "restoreMode", "reasonId",
                        "errorType", "cache", "inventoryCache", "inventoryScope", "fullFrame",
                        "targetLookup", "index", "realizedReceipts", "warnings",
                        "verifiedRdcCache", "editing", "contextRecovery", "performance"):
                if key in result:
                    record[key] = copy.deepcopy(result[key])
            for section in ("shaders", "materials"):
                value = result.get(section)
                if isinstance(value, Mapping):
                    trimmed = {k: v for k, v in value.items() if k != "items"}
                    items = value.get("items")
                    if isinstance(items, list):
                        # The result was already protocol/byte bounded. Cutting
                        # this list at 64 hid later failures and broke its own
                        # attempted/applied/failed counts on ordinary projects.
                        trimmed["items"] = copy.deepcopy(items)
                    record[section] = trimmed
        if job_metadata:
            record.update(job_metadata)
        path = _restore_result_record_path(project_root, capture_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(record, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        if len(encoded) > 2 * 1024 * 1024:
            record.pop("shaders", None)
            record.pop("materials", None)
            encoded = json.dumps(record, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        # Readers on Windows can briefly deny replacement while polling this
        # file. Reuse the unique temporary and bounded sharing-error retries.
        from .shader_workspace.io import atomic_write_bytes
        atomic_write_bytes(path, encoded)
    except Exception:  # noqa: BLE001 - diagnostics must never fail a restore
        pass


def _runner_accepts_restore_control(runner: Callable[..., Any]) -> bool:
    """Return whether a custom runner accepts the v2 cooperative controls.

    Existing tests and embedders may supply the historical callback-only
    runner.  Inspecting once avoids catching a TypeError raised *inside* the
    runner and accidentally retrying physical work.
    """

    try:
        signature = inspect.signature(runner)
    except (TypeError, ValueError):
        return False
    parameters = signature.parameters
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return True
    return all(
        name in parameters
        and parameters[name].kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        for name in ("cancel_event", "deadline_monotonic")
    )


class ProjectRestoreJobRegistry:
    """Thread-safe, bounded single-worker registry for project restore jobs."""

    _ACTIVE_STATUSES = frozenset(("queued", "running"))
    _TERMINAL_STATUSES = frozenset(("completed", "failed"))

    def __init__(
        self,
        *,
        runner: Callable[..., dict[str, Any]] | None = None,
        max_active_jobs: int = MAX_PROJECT_RESTORE_ACTIVE_JOBS,
        max_history_jobs: int = MAX_PROJECT_RESTORE_HISTORY_JOBS,
        history_ttl_seconds: float = PROJECT_RESTORE_HISTORY_TTL_SECONDS,
        max_progress_events: int = MAX_PROJECT_RESTORE_PROGRESS_EVENTS,
        progress_page_size: int = MAX_PROJECT_RESTORE_PROGRESS_PAGE,
        deadline_seconds: float = PROJECT_RESTORE_DEADLINE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        unix_time: Callable[[], float] = time.time,
    ):
        if (
            isinstance(max_active_jobs, bool)
            or int(max_active_jobs) <= 0
            or isinstance(max_history_jobs, bool)
            or int(max_history_jobs) <= 0
            or float(history_ttl_seconds) <= 0
            or isinstance(max_progress_events, bool)
            or int(max_progress_events) <= 0
            or isinstance(progress_page_size, bool)
            or int(progress_page_size) <= 0
            or isinstance(deadline_seconds, bool)
            or not math.isfinite(float(deadline_seconds))
            or float(deadline_seconds) <= 0
        ):
            raise ValueError("project restore registry bounds must be positive")
        self._runner = runner or _run_project_restore
        self._max_active_jobs = int(max_active_jobs)
        self._max_history_jobs = int(max_history_jobs)
        self._history_ttl_seconds = float(history_ttl_seconds)
        self._max_progress_events = int(max_progress_events)
        self._progress_page_size = min(
            int(progress_page_size), self._max_progress_events
        )
        self._deadline_seconds = float(deadline_seconds)
        self._monotonic = monotonic
        self._unix_time = unix_time
        self._lock = threading.Lock()
        self._result_write_lock = threading.Lock()
        self._shader_resume_condition = threading.Condition(self._lock)
        self._shader_resume_readers: dict[tuple[str, str], int] = {}
        self._project_editor_scopes: dict[tuple[str, str, str], None] = {}
        self._jobs: dict[str, _ProjectRestoreJob] = {}
        self._active_by_key: dict[tuple[str, str, str], str] = {}
        self._next_completed_order = 0
        self._closed = False
        self._runner_accepts_control = (
            runner is None or _runner_accepts_restore_control(self._runner)
        )
        parameters = inspect.signature(self._runner).parameters if runner is None else {}
        if runner is not None:
            try:
                parameters = inspect.signature(runner).parameters
            except (TypeError, ValueError):
                pass
        self._runner_accepts_post_restore = "post_restore_callback" in parameters
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="replaylab-project-restore"
        )

    @staticmethod
    def _shader_scope(capture_path: str, capture_session_id: str) -> tuple[str, str]:
        # Callers already canonicalize these paths; bookkeeping must not do
        # filesystem resolution while holding the short scheduling lock.
        return (os.path.normcase(os.path.abspath(capture_path)), capture_session_id)

    def _shader_restore_active_locked(self, scope: tuple[str, str]) -> bool:
        return any(job.status in self._ACTIVE_STATUSES
                   and self._shader_scope(job.capture_path, job.capture_session_id) == scope
                   for job in self._jobs.values())

    def shader_resume_allowed(self, binding: Mapping[str, str]) -> bool:
        scope = self._shader_scope(binding["capturePath"], binding["captureSessionId"])
        with self._lock:
            return (not self._closed and not self._shader_restore_active_locked(scope)
                    and (*scope, binding["instanceId"]) not in self._project_editor_scopes)

    def _remember_project_editors_locked(self, job: _ProjectRestoreJob, result: dict) -> None:
        editing = result.get("editing")
        enabled = editing.get("enabled") if isinstance(editing, dict) else None
        deferred = editing.get("deferred", 0) if isinstance(editing, dict) else 0
        pending = editing.get("pending", 0) if isinstance(editing, dict) else 0
        if (result.get("completed") is not True or isinstance(enabled, bool)
                or not isinstance(enabled, int) or isinstance(deferred, bool)
                or not isinstance(deferred, int) or type(pending) is not int or enabled + deferred + pending <= 0):
            return
        scope = self._shader_scope(job.capture_path, job.capture_session_id)
        items = editing.get("items")
        if not isinstance(items, list):
            return
        for item in items[:MAX_PROJECT_RESTORE_PROGRESS_EVENTS]:
            binding = item.get("captureBinding") if isinstance(item, dict) else None
            if (not isinstance(item, dict) or item.get("status") not in {"enabled", "deferred", "pending"} or not isinstance(binding, dict)
                    or not isinstance(binding.get("instanceId"), str) or not binding["instanceId"]
                    or self._shader_scope(str(binding.get("capturePath", "")),
                                          str(binding.get("captureSessionId", ""))) != scope):
                continue
            # A restore-owned source-only decision also suppresses background
            # live-edit reconciliation for this capture. It grants no editor
            # or replay authority; explicit final publication remains separate.
            key = (*scope, binding["instanceId"])
            self._project_editor_scopes.pop(key, None)
            self._project_editor_scopes[key] = None
        # These are only automatic-resume scheduling hints for exact live
        # sessions, not permanent consent or mutation authority.
        while len(self._project_editor_scopes) > 128:
            self._project_editor_scopes.pop(next(iter(self._project_editor_scopes)))

    @contextmanager
    def reserve_shader_resume(self, binding: Mapping[str, str]):
        """Admit an automatic reader or defer it, without holding a long lock.

        Submitting restore immediately blocks new readers. A reader admitted
        first is allowed to finish its in-flight replay call before mutation;
        checkpoints stop it from starting additional workspace preflights.
        """
        binding = dict(binding)
        scope = self._shader_scope(binding["capturePath"], binding["captureSessionId"])
        with self._lock:
            admitted = (not self._closed and not self._shader_restore_active_locked(scope)
                        and (*scope, binding["instanceId"]) not in self._project_editor_scopes)
            if admitted:
                self._shader_resume_readers[scope] = self._shader_resume_readers.get(scope, 0) + 1
        if not admitted:
            yield False
            return
        token = _AUTOMATIC_SHADER_RESUME_ALLOWED.set(lambda: self.shader_resume_allowed(binding))
        try:
            yield True
        finally:
            _AUTOMATIC_SHADER_RESUME_ALLOWED.reset(token)
            with self._shader_resume_condition:
                count = self._shader_resume_readers[scope] - 1
                if count:
                    self._shader_resume_readers[scope] = count
                else:
                    self._shader_resume_readers.pop(scope, None)
                self._shader_resume_condition.notify_all()

    def _unix_ms(self) -> int:
        return int(self._unix_time() * 1000)

    def _cleanup_locked(self, now: float) -> None:
        terminal = [
            job
            for job in self._jobs.values()
            if job.status in self._TERMINAL_STATUSES
            and job.completed_monotonic is not None
            and not (isinstance(job.result, dict)
                     and (job.result.get("editing") or {}).get("status") == "pending")
        ]
        expired = {
            job.job_id
            for job in terminal
            if now - float(job.completed_monotonic) >= self._history_ttl_seconds
        }
        remaining = sorted(
            (job for job in terminal if job.job_id not in expired),
            key=lambda job: int(job.completed_order or 0),
        )
        excess = max(0, len(remaining) - self._max_history_jobs)
        expired.update(job.job_id for job in remaining[:excess])
        for job_id in expired:
            self._jobs.pop(job_id, None)

    def _fail_locked(
        self, job: _ProjectRestoreJob, error: str, now: float
    ) -> None:
        if job.status in self._TERMINAL_STATUSES:
            return
        job.status = "failed"
        job.error = str(error or "project restore failed")[
            :MAX_PROJECT_RESTORE_ERROR_BYTES
        ]
        job.completed_monotonic = now
        self._next_completed_order += 1
        job.completed_order = self._next_completed_order
        job.updated_unix_ms = self._unix_ms()
        if self._active_by_key.get(job.key) == job.job_id:
            self._active_by_key.pop(job.key, None)

    def _expire_queued_locked(self, now: float) -> None:
        for job in list(self._jobs.values()):
            if job.status == "queued" and now >= job.deadline_monotonic:
                job.cancel_event.set()
                self._fail_locked(job, PROJECT_RESTORE_DEADLINE_ERROR, now)

    def _snapshot_locked(self, job: _ProjectRestoreJob, after: int) -> dict[str, Any]:
        progress = [
            copy.deepcopy(event)
            for event in job.progress
            if event["sequence"] > after
        ][: self._progress_page_size]
        oldest = job.progress[0]["sequence"] if job.progress else job.latest_sequence + 1
        value: dict[str, Any] = {
            "schema": PROJECT_RESTORE_JOB_SCHEMA,
            "schemaVersion": PROJECT_RESTORE_JOB_VERSION,
            "jobId": job.job_id,
            "status": job.status,
            "latestSequence": job.latest_sequence,
            "progress": progress,
            "progressTruncated": bool(job.progress and after < oldest - 1),
        }
        if job.status == "completed":
            value["result"] = copy.deepcopy(job.result or {})
        elif job.status == "failed":
            value["error"] = str(job.error or "project restore failed")
        return value

    def submit(self, request: Mapping[str, str]) -> tuple[dict[str, Any], bool]:
        expected = {"projectRoot", "capturePath", "captureSessionId"}
        if not isinstance(request, Mapping) or set(request) != expected:
            raise ProjectRestoreRequestError("project restore fields are invalid")
        project_root = str(request["projectRoot"])
        capture_path = str(request["capturePath"])
        capture_session_id = str(request["captureSessionId"])
        key = (
            os.path.normcase(project_root),
            os.path.normcase(capture_path),
            capture_session_id,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("project restore registry is closed")
            now = self._monotonic()
            self._expire_queued_locked(now)
            self._cleanup_locked(now)
            active_id = self._active_by_key.get(key)
            active = self._jobs.get(active_id) if active_id is not None else None
            if active is not None and active.status in self._ACTIVE_STATUSES:
                return self._snapshot_locked(active, 0), True
            active_count = sum(
                job.status in self._ACTIVE_STATUSES for job in self._jobs.values()
            )
            if active_count >= self._max_active_jobs:
                raise ProjectRestoreCapacityError("project restore queue is full")
            job_id = secrets.token_hex(16)
            while job_id in self._jobs:
                job_id = secrets.token_hex(16)
            unix_ms = self._unix_ms()
            job = _ProjectRestoreJob(
                job_id=job_id,
                key=key,
                project_root=project_root,
                capture_path=capture_path,
                capture_session_id=capture_session_id,
                status="queued",
                created_monotonic=now,
                created_unix_ms=unix_ms,
                updated_unix_ms=unix_ms,
                latest_sequence=0,
                progress=deque(maxlen=self._max_progress_events),
                cancel_event=threading.Event(),
                deadline_monotonic=now + self._deadline_seconds,
            )
            self._jobs[job_id] = job
            self._active_by_key[key] = job_id
            scope = self._shader_scope(capture_path, capture_session_id)
            for owner_scope in list(self._project_editor_scopes):
                if owner_scope[:2] == scope:
                    self._project_editor_scopes.pop(owner_scope, None)
            try:
                self._executor.submit(self._execute, job_id)
            except BaseException:
                self._active_by_key.pop(key, None)
                self._jobs.pop(job_id, None)
                raise
            return self._snapshot_locked(job, 0), False

    def _append_progress(self, job_id: str, value: Any) -> None:
        try:
            clean = _normalise_restore_progress(value)
        except (TypeError, ValueError, ProjectRestoreRequestError):
            clean = {
                "phase": "progress-warning",
                "message": "Ignored malformed project restore progress.",
                "item": "",
                "current": 0,
                "total": 0,
            }
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in self._ACTIVE_STATUSES:
                return
            if job.latest_sequence >= 0x7FFFFFFF:
                return
            if job.progress and all(
                job.progress[-1].get(field) == clean.get(field)
                for field in ("phase", "message", "item", "current", "total")
            ):
                return
            job.latest_sequence += 1
            event = {"sequence": job.latest_sequence, **clean}
            job.progress.append(event)
            job.updated_unix_ms = self._unix_ms()
            timed = self.__dict__.setdefault("_progress_trails", {}).setdefault(job_id, [])
            timed.append({"unixMs": job.updated_unix_ms, **event})
            del timed[:-48]
            trail = list(timed)
            persisted_at = self.__dict__.setdefault("_progress_persisted_at", {})
            now = time.monotonic()
            last = persisted_at.get(job_id, 0.0)
            persist = now - last >= 1.0 or clean.get("phase") in ("complete", "failed")
            if persist:
                persisted_at[job_id] = now
        # Outside the lock: a restore that stops answering leaves its last
        # phases where a person can read them (derived/restore-results), which
        # the in-memory registry cannot do once the GUI stops polling.
        if persist:
            self._persist_result(job_id, None, None, running=True, progress=trail)

    def _persist_result(self, job_id: str, result: Mapping[str, Any] | None,
                        error: str | None, *, running: bool = False,
                        progress: list[Mapping[str, Any]] | None = None,
                        editor_update: bool = False) -> None:
        # A callback may have left the registry lock before its worker finishes.
        # Serialize publication without blocking HTTP status/cancel, and recheck
        # this exact job so delayed progress cannot overwrite its terminal result
        # or the next job's result for the same capture.
        with self._result_write_lock:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or (job.result_finalized and not editor_update):
                    return
                if editor_update and (job.status != "completed" or job.result is not result):
                    return
                if running:
                    sequence = int((progress or [{}])[-1].get("sequence", 0))
                    if job.status not in self._ACTIVE_STATUSES or sequence <= job.persisted_sequence:
                        return
                else:
                    job.result_finalized = True
                    sequence = job.latest_sequence
                    if editor_update:
                        progress = list(job.final_progress or [])
                    else:
                        trails = self.__dict__.get("_progress_trails") or {}
                        progress = list(trails.pop(job_id, []))
                        job.final_progress = copy.deepcopy(progress[-48:])
                    self.__dict__.get("_progress_persisted_at", {}).pop(job_id, None)
                job.persisted_sequence = sequence
                project_root, capture_path = job.project_root, job.capture_path
                metadata = {"jobId": job_id, "captureSessionId": job.capture_session_id,
                            "startedUnixMs": job.created_unix_ms, "latestSequence": sequence}
            persist_restore_result(project_root, capture_path, result, error,
                                   running=running, progress=progress,
                                   job_metadata=metadata)

    def _execute(self, job_id: str) -> None:
        from .shader_restore_admission import restore_admission
        with restore_admission(getattr(self, "_local_shader_dispatcher", None)):
            self._execute_admitted(job_id)

    def _execute_admitted(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "queued":
                return
            scope = self._shader_scope(job.capture_path, job.capture_session_id)
            while self._shader_resume_readers.get(scope, 0):
                remaining = job.deadline_monotonic - self._monotonic()
                if job.cancel_event.is_set() or remaining <= 0:
                    break
                # Condition.wait releases the registry lock. Status, cancel,
                # unrelated capture reads and new job submissions stay live.
                self._shader_resume_condition.wait(min(0.05, remaining))
                if job.status != "queued":
                    return
            now = self._monotonic()
            if job.cancel_event.is_set():
                self._fail_locked(job, PROJECT_RESTORE_CANCELLED_ERROR, now)
                self._cleanup_locked(now)
                return
            if now >= job.deadline_monotonic:
                job.cancel_event.set()
                self._fail_locked(job, PROJECT_RESTORE_DEADLINE_ERROR, now)
                self._cleanup_locked(now)
                return
            job.status = "running"
            job.updated_unix_ms = self._unix_ms()
            inputs = (job.project_root, job.capture_path, job.capture_session_id)
            cancel_event = job.cancel_event
            deadline_monotonic = job.deadline_monotonic
        try:
            post_restore_work: list[Callable[[], dict[str, Any]]] = []
            runner_kwargs: dict[str, Any] = {
                "progress_callback": lambda event: self._append_progress(
                    job_id, event
                )
            }
            if self._runner_accepts_control:
                runner_kwargs.update(
                    {
                        "cancel_event": cancel_event,
                        "deadline_monotonic": deadline_monotonic,
                    }
                )
            if self._runner_accepts_post_restore:
                runner_kwargs["post_restore_callback"] = post_restore_work.append
            value = self._runner(inputs[0], inputs[1], inputs[2], **runner_kwargs)
            result = _bounded_restore_json_object(
                value, "project restore result", MAX_PROJECT_RESTORE_RESULT_BYTES
            )
        except BaseException as exc:
            self._persist_result(job_id, None, "%s: %s" % (type(exc).__name__, exc))
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                now = self._monotonic()
                exception_name = type(exc).__name__
                if exception_name == "RestoreCancelled":
                    self._fail_locked(job, PROJECT_RESTORE_CANCELLED_ERROR, now)
                elif exception_name == "RestoreDeadlineExceeded":
                    self._fail_locked(job, PROJECT_RESTORE_DEADLINE_ERROR, now)
                else:
                    # In particular, never hide RestoreRollbackFailed merely
                    # because its triggering cancellation Event remains set:
                    # that diagnostic means live mutation may still exist.
                    self._fail_locked(job, _bounded_restore_error(exc), now)
                self._cleanup_locked(now)
            return
        self._persist_result(job_id, result, None)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            now = self._monotonic()
            # ``completed: true`` is the restore transaction's commit marker:
            # its final checkpoint ran while the project pin and rollback
            # ledgers were still owned, then the pin was released.  A DELETE
            # racing after that return must not relabel committed live state as
            # failed, because no worker remains that could roll it back.
            committed = result.get("completed") is True
            if not committed and job.cancel_event.is_set():
                self._fail_locked(job, PROJECT_RESTORE_CANCELLED_ERROR, now)
                self._cleanup_locked(now)
                return
            if not committed and now >= job.deadline_monotonic:
                self._fail_locked(job, PROJECT_RESTORE_DEADLINE_ERROR, now)
                self._cleanup_locked(now)
                return
            job.status = "completed"
            job.result = result
            self._remember_project_editors_locked(job, result)
            job.completed_monotonic = now
            self._next_completed_order += 1
            job.completed_order = self._next_completed_order
            job.updated_unix_ms = self._unix_ms()
            if self._active_by_key.get(job.key) == job_id:
                self._active_by_key.pop(job.key, None)
            self._cleanup_locked(job.completed_monotonic)

        # Publish completion before starting watchers. Keep this ordered on the
        # same worker, outside every registry/project/restore lock. A slow or
        # failed editor never relabels already committed replay state as failed.
        if committed and post_restore_work:
            editor_started = self._monotonic()
            try:
                editing = post_restore_work[0]()
                editing = _bounded_restore_json_object(editing, "restored editor result", MAX_PROJECT_RESTORE_RESULT_BYTES)
            except Exception as exc:
                pending_items = (result.get("editing") or {}).get("items", [])
                failed_items = [{"workspaceId": item["workspaceId"], "workspacePath": item["workspacePath"],
                                 "status": "failed", "error": _bounded_restore_error(exc)} for item in pending_items]
                editing = {"attempted": len(failed_items), "enabled": 0, "deferred": 0,
                           "failed": len(failed_items), "items": failed_items}
            from .shader_restore_admission import statistics
            editing["initializationMs"] = round((self._monotonic() - editor_started) * 1000, 3)
            editing["startupReuse"] = statistics()
            with self._lock:
                if self._jobs.get(job_id) is not job:
                    return
                for item in (result.get("editing") or {}).get("items", []):
                    binding = item.get("captureBinding") or {}
                    if binding.get("instanceId"):
                        scope = self._shader_scope(job.capture_path, job.capture_session_id)
                        self._project_editor_scopes.pop((*scope, binding["instanceId"]), None)
                # Replace the result atomically for readers taking snapshots.
                result = dict(result, editing=editing)
                job.result = result
                # Retain the final editor receipt for a full history window,
                # even if automatic startup used most of its own time budget.
                job.completed_monotonic = self._monotonic()
                job.updated_unix_ms = self._unix_ms()
                self._remember_project_editors_locked(job, result)
            self._persist_result(job_id, result, None, editor_update=True)

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        if (
            not isinstance(job_id, str)
            or PROJECT_RESTORE_JOB_ID_PATTERN.fullmatch(job_id) is None
        ):
            raise ProjectRestoreRequestError("project restore job id is invalid")
        with self._lock:
            now = self._monotonic()
            self._expire_queued_locked(now)
            self._cleanup_locked(now)
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status in self._ACTIVE_STATUSES:
                job.cancel_event.set()
                job.updated_unix_ms = self._unix_ms()
                if job.status == "queued":
                    self._fail_locked(job, PROJECT_RESTORE_CANCELLED_ERROR, now)
            return self._snapshot_locked(job, 0)

    def get(self, job_id: str, *, after: int = 0) -> dict[str, Any] | None:
        if (
            not isinstance(job_id, str)
            or PROJECT_RESTORE_JOB_ID_PATTERN.fullmatch(job_id) is None
        ):
            raise ProjectRestoreRequestError("project restore job id is invalid")
        if (
            isinstance(after, bool)
            or not isinstance(after, int)
            or after < 0
            or after > (1 << 63) - 1
        ):
            raise ProjectRestoreRequestError("after must be a non-negative integer")
        with self._lock:
            now = self._monotonic()
            self._expire_queued_locked(now)
            self._cleanup_locked(now)
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return self._snapshot_locked(job, after)

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            now = self._monotonic()
            for job in list(self._jobs.values()):
                if (job.status == "completed" and isinstance(job.result, dict)
                        and (job.result.get("editing") or {}).get("status") == "pending"):
                    job.cancel_event.set()
                if job.status not in self._ACTIVE_STATUSES:
                    continue
                job.cancel_event.set()
                if job.status == "queued":
                    self._fail_locked(job, PROJECT_RESTORE_CANCELLED_ERROR, now)
        self._executor.shutdown(wait=wait, cancel_futures=True)


def _parse_project_restore_after(value: Any) -> int:
    token = str(value if value is not None else "0")
    if not re.fullmatch(r"0|[1-9][0-9]{0,18}", token):
        raise ProjectRestoreRequestError("after must be a non-negative integer")
    result = int(token)
    if result > (1 << 63) - 1:
        raise ProjectRestoreRequestError("after must be a bounded non-negative integer")
    return result


async def project_restore_post_response(
    request: Any, registry: ProjectRestoreJobRegistry
) -> tuple[int, dict[str, Any]]:
    """Purely testable POST endpoint adapter returning ``(status, JSON)``."""

    try:
        value = parse_project_restore_request(await _read_project_restore_body(request))
        snapshot, deduplicated = registry.submit(value)
        return (200 if deduplicated else 202), snapshot
    except ProjectRestoreRequestError as exc:
        return exc.status_code, {"error": str(exc)[:2048]}
    except ProjectRestoreCapacityError as exc:
        return 429, {"error": str(exc)[:2048]}
    except RuntimeError as exc:
        return 503, {"error": str(exc)[:2048]}


def project_restore_get_response(
    job_id: Any,
    after: Any,
    registry: ProjectRestoreJobRegistry,
) -> tuple[int, dict[str, Any]]:
    """Purely testable GET endpoint adapter returning ``(status, JSON)``."""

    try:
        snapshot = registry.get(str(job_id), after=_parse_project_restore_after(after))
    except ProjectRestoreRequestError as exc:
        return exc.status_code, {"error": str(exc)[:2048]}
    if snapshot is None:
        return 404, {"error": "project restore job was not found"}
    return 200, snapshot


def project_restore_delete_response(
    job_id: Any, registry: ProjectRestoreJobRegistry
) -> tuple[int, dict[str, Any]]:
    """Request cooperative cancellation and return the bounded job snapshot."""

    try:
        snapshot = registry.cancel(str(job_id))
    except ProjectRestoreRequestError as exc:
        return exc.status_code, {"error": str(exc)[:2048]}
    if snapshot is None:
        return 404, {"error": "project restore job was not found"}
    # Queued cancellation is terminal immediately. A running job acknowledges
    # the request and transitions to failed at its next safe checkpoint.
    return (202 if snapshot.get("status") == "running" else 200), snapshot


class ShaderEditorSaveRequestError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _duplicate_shader_editor_json_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ShaderEditorSaveRequestError(
                "shader editor save contains a duplicate JSON key: %s" % key
            )
        result[key] = value
    return result


def parse_shader_editor_save_request(
    payload: bytes | bytearray | memoryview,
) -> dict[str, str]:
    """Parse one exact, bounded save request without accepting path authority."""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ShaderEditorSaveRequestError("shader editor save body must be bytes")
    raw = bytes(payload)
    if not raw:
        raise ShaderEditorSaveRequestError("shader editor save body is empty")
    if len(raw) > MAX_SHADER_EDITOR_SAVE_REQUEST_BYTES:
        raise ShaderEditorSaveRequestError(
            "shader editor save body exceeds its byte limit", status_code=413
        )
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_shader_editor_json_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ShaderEditorSaveRequestError(
                    "shader editor save contains non-finite JSON: %s" % token
                )
            ),
        )
    except UnicodeDecodeError as exc:
        raise ShaderEditorSaveRequestError(
            "shader editor save body must be UTF-8 JSON"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ShaderEditorSaveRequestError(
            "shader editor save body is invalid JSON"
        ) from exc
    expected_fields = {
        "liveEditId", "authorizationId", "workspaceId",
        "expectedRawSha256", "text",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ShaderEditorSaveRequestError("shader editor save fields are invalid")
    for field in ("liveEditId", "authorizationId", "workspaceId"):
        token = value.get(field)
        if (not isinstance(token, str) or
                SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(token) is None):
            raise ShaderEditorSaveRequestError(
                "%s must be 32 lowercase hexadecimal characters" % field
            )
    expected_sha256 = value.get("expectedRawSha256")
    if (not isinstance(expected_sha256, str) or
            SHADER_EDITOR_SAVE_SHA256_PATTERN.fullmatch(expected_sha256) is None):
        raise ShaderEditorSaveRequestError(
            "expectedRawSha256 must be 64 lowercase hexadecimal characters"
        )
    text = value.get("text")
    if not isinstance(text, str) or not text:
        raise ShaderEditorSaveRequestError("shader editor source must not be empty")
    if "\x00" in text:
        raise ShaderEditorSaveRequestError(
            "shader editor source contains NUL characters"
        )
    try:
        source_size = len(text.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ShaderEditorSaveRequestError(
            "shader editor source is not valid UTF-8"
        ) from exc
    if source_size > MAX_SHADER_EDITOR_SOURCE_BYTES:
        raise ShaderEditorSaveRequestError(
            "shader editor source exceeds its byte limit", status_code=413
        )
    return value


def parse_shader_editor_check_request(
    payload: bytes | bytearray | memoryview,
) -> dict[str, str]:
    """Parse one bounded compile-check request (unsaved-buffer diagnostics).

    A check is read-only: it needs the live/authorization/workspace identity
    and the buffer text, but no ``expectedRawSha256`` because it never writes
    the source and so has no on-disk revision to guard against.
    """

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ShaderEditorSaveRequestError("shader editor check body must be bytes")
    raw = bytes(payload)
    if not raw:
        raise ShaderEditorSaveRequestError("shader editor check body is empty")
    if len(raw) > MAX_SHADER_EDITOR_SAVE_REQUEST_BYTES:
        raise ShaderEditorSaveRequestError(
            "shader editor check body exceeds its byte limit", status_code=413
        )
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_shader_editor_json_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ShaderEditorSaveRequestError(
                    "shader editor check contains non-finite JSON: %s" % token
                )
            ),
        )
    except UnicodeDecodeError as exc:
        raise ShaderEditorSaveRequestError(
            "shader editor check body must be UTF-8 JSON"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ShaderEditorSaveRequestError(
            "shader editor check body is invalid JSON"
        ) from exc
    expected_fields = {"liveEditId", "authorizationId", "workspaceId", "text"}
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ShaderEditorSaveRequestError("shader editor check fields are invalid")
    for field in ("liveEditId", "authorizationId", "workspaceId"):
        token = value.get(field)
        if (not isinstance(token, str) or
                SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(token) is None):
            raise ShaderEditorSaveRequestError(
                "%s must be 32 lowercase hexadecimal characters" % field
            )
    text = value.get("text")
    if not isinstance(text, str) or not text:
        raise ShaderEditorSaveRequestError("shader editor source must not be empty")
    if "\x00" in text:
        raise ShaderEditorSaveRequestError(
            "shader editor source contains NUL characters"
        )
    try:
        source_size = len(text.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ShaderEditorSaveRequestError(
            "shader editor source is not valid UTF-8"
        ) from exc
    if source_size > MAX_SHADER_EDITOR_SOURCE_BYTES:
        raise ShaderEditorSaveRequestError(
            "shader editor source exceeds its byte limit", status_code=413
        )
    return value


async def _read_shader_editor_save_body(request: Any) -> bytes:
    headers = getattr(request, "headers", {})
    supplied_length = headers.get("content-length") if hasattr(headers, "get") else None
    if supplied_length not in (None, ""):
        try:
            declared = int(supplied_length)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ShaderEditorSaveRequestError("content-length is invalid") from exc
        if declared < 0:
            raise ShaderEditorSaveRequestError("content-length is invalid")
        if declared > MAX_SHADER_EDITOR_SAVE_REQUEST_BYTES:
            raise ShaderEditorSaveRequestError(
                "shader editor save body exceeds its byte limit", status_code=413
            )
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        value = chunk if isinstance(chunk, bytes) else bytes(chunk)
        total += len(value)
        if total > MAX_SHADER_EDITOR_SAVE_REQUEST_BYTES:
            raise ShaderEditorSaveRequestError(
                "shader editor save body exceeds its byte limit", status_code=413
            )
        chunks.append(value)
    return b"".join(chunks)


def shader_editor_save_response(
    payload: bytes | bytearray | memoryview, manager: Any
) -> tuple[int, dict[str, Any]]:
    """Execute a save only through an in-memory Host-owned live-edit job."""

    try:
        value = parse_shader_editor_save_request(payload)
        result = manager.save_authorized_source(
            value["liveEditId"],
            value["authorizationId"],
            value["workspaceId"],
            value["expectedRawSha256"],
            value["text"],
        )
        if not isinstance(result, dict):
            raise RuntimeError("shader editor save returned a non-object result")
        watch_id = str(result.get("watchId", "") or "")
        saved_sha256 = str(result.get("rawSha256", "") or "")
        changed = result.get("changed")
        compile_scheduled = result.get("compileScheduled")
        size = result.get("size")
        modified_ns = result.get("modifiedNs")
        if (
            SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(watch_id) is None
            or SHADER_EDITOR_SAVE_SHA256_PATTERN.fullmatch(saved_sha256) is None
            or not isinstance(changed, bool)
            or not isinstance(compile_scheduled, bool)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > MAX_SHADER_EDITOR_SOURCE_BYTES
            or isinstance(modified_ns, bool)
            or not isinstance(modified_ns, int)
            or modified_ns <= 0
        ):
            raise RuntimeError("shader editor save returned an invalid result")
        return 200, {
            "schema": "replaylab-shader-source-save/v1",
            "schemaVersion": 1,
            "liveEditId": value["liveEditId"],
            "authorizationId": value["authorizationId"],
            "workspaceId": value["workspaceId"],
            "watchId": watch_id,
            "savedRawSha256": saved_sha256,
            "size": size,
            "modifiedNs": modified_ns,
            "changed": changed,
            "compileScheduled": compile_scheduled,
        }
    except ShaderEditorSaveRequestError as exc:
        return exc.status_code, {"error": str(exc)[:2048]}
    except Exception as exc:
        from .shader_workspace.errors import WorkspaceWatchConflictError

        if isinstance(exc, WorkspaceWatchConflictError):
            return 409, {"error": str(exc)[:2048], "conflict": True}
        # Unknown/expired IDs, an inactive job, or a stopped watcher are all
        # safe client conflicts. Internal persistence/I/O faults remain 503 so
        # the editor does not claim that the source was published.
        if type(exc).__name__ in {"ShaderLiveEditError", "WorkspaceWatchError"}:
            return 409, {"error": str(exc)[:2048]}
        return 503, {"error": ("%s: %s" % (type(exc).__name__, exc))[:2048]}


def shader_editor_check_response(
    payload: bytes | bytearray | memoryview, manager: Any
) -> tuple[int, dict[str, Any]]:
    """Compile an unsaved buffer through the Host-owned live-edit job only.

    Read-only counterpart to :func:`shader_editor_save_response`: it returns
    compiler diagnostics for the current buffer without writing, publishing,
    or applying anything. An ``ok`` of ``None`` means the buffer could not be
    checked live (e.g. a route-plan workspace or an unavailable compiler); the
    editor then keeps whatever it already shows rather than inventing errors.
    """

    try:
        value = parse_shader_editor_check_request(payload)
        result = manager.check_authorized_source(
            value["liveEditId"],
            value["authorizationId"],
            value["workspaceId"],
            value["text"],
        )
        if not isinstance(result, dict):
            raise RuntimeError("shader editor check returned a non-object result")
        ok = result.get("ok")
        if ok is not None and not isinstance(ok, bool):
            raise RuntimeError("shader editor check returned an invalid ok flag")
        raw_diagnostics = result.get("diagnostics") or []
        if not isinstance(raw_diagnostics, list):
            raise RuntimeError("shader editor check returned invalid diagnostics")
        diagnostics: list[dict[str, Any]] = []
        for item in raw_diagnostics[:MAX_SHADER_EDITOR_CHECK_DIAGNOSTICS]:
            if not isinstance(item, dict):
                continue
            try:
                line = int(item.get("line", 0) or 0)
            except (TypeError, ValueError):
                line = 0
            try:
                column = int(item.get("column", 0) or 0)
            except (TypeError, ValueError):
                column = 0
            severity = str(item.get("severity", "error") or "error")
            if severity not in ("error", "warning", "information", "hint"):
                severity = "error"
            message = str(item.get("message", "") or "")[:4096]
            diagnostics.append({
                "line": max(0, line),
                "column": max(0, column),
                "severity": severity,
                "message": message,
            })
        return 200, {
            "schema": "replaylab-shader-source-check/v1",
            "schemaVersion": 1,
            "liveEditId": value["liveEditId"],
            "authorizationId": value["authorizationId"],
            "workspaceId": value["workspaceId"],
            "ok": ok,
            "diagnostics": diagnostics,
            "reason": str(result.get("reason") or "")[:512],
        }
    except ShaderEditorSaveRequestError as exc:
        return exc.status_code, {"error": str(exc)[:2048]}
    except Exception as exc:
        # A check never publishes, so every failure is a safe client-side
        # conflict or an internal fault; neither must be shown as a code error.
        if type(exc).__name__ in {"ShaderLiveEditError", "WorkspaceWatchError"}:
            return 409, {"error": str(exc)[:2048]}
        return 503, {"error": ("%s: %s" % (type(exc).__name__, exc))[:2048]}


_SHADER_LIVE_EDIT_MANAGER: ShaderLiveEditManager | None = None
_SHADER_LIVE_EDIT_MANAGER_GUARD = threading.Lock()
_SHADER_LIVE_EDIT_BINDINGS: dict[str, ShaderLiveEditSession] = {}
_SHADER_LIVE_EDIT_BINDING_GUARD = threading.Lock()


def require_local_shader_apply_owner(workspace_path, expected, direct_binding):
    """Read existing owner state only; never start a manager or resume a job."""
    with _SHADER_LIVE_EDIT_MANAGER_GUARD:
        manager = _SHADER_LIVE_EDIT_MANAGER
        if manager is None:
            raise RuntimeError("Shader live-edit apply owner is unavailable")
        manager.require_local_apply_owner(workspace_path, expected, direct_binding)


def _shader_capture_mailbox(directory: Path) -> dict[str, Any] | None:
    path = directory / "instance.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, TypeError, ValueError):
        return None


_SHADER_SESSION_OBSERVATIONS: dict[tuple, tuple] = {}
_SHADER_SESSION_OBSERVATION_GUARD = threading.Lock()
_SHADER_SESSION_OBSERVATION_SECONDS = 0.25


def _shader_capture_session_state(binding: dict[str, str], pid: int, *, fresh: bool = False) -> str:
    """Share one idle mailbox observation across stages of the same GUI.

    A changed mailbox is observed immediately. The short expiry also notices
    process exit and path redirection without a mailbox update. A write/replay
    operation requests a fresh observation and retains its exact IPC binding.
    """
    from .ipc_client import INSTANCE_ROOT, IPCClient

    if not IPCClient._process_alive(pid):
        return "expired"
    if fresh:
        return _read_shader_capture_session_state(binding, pid)
    try:
        path = Path(INSTANCE_ROOT) / binding["instanceId"] / "instance.json"
        info = path.stat(follow_symlinks=False)
        stamp = (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                 info.st_mtime_ns, info.st_ctime_ns, getattr(info, "st_file_attributes", 0))
        key = (str(INSTANCE_ROOT), pid, binding["instanceId"],
               binding["captureSessionId"], binding["capturePath"])
    except (OSError, KeyError, TypeError):
        return _read_shader_capture_session_state(binding, pid)
    with _SHADER_SESSION_OBSERVATION_GUARD:
        now = time.monotonic()
        previous = _SHADER_SESSION_OBSERVATIONS.get(key)
        if previous is not None and previous[0] == stamp and now < previous[1]:
            return previous[2]
        state = _read_shader_capture_session_state(binding, pid)
        if len(_SHADER_SESSION_OBSERVATIONS) >= 128:
            _SHADER_SESSION_OBSERVATIONS.clear()
        _SHADER_SESSION_OBSERVATIONS[key] = (stamp, now + _SHADER_SESSION_OBSERVATION_SECONDS, state)
        return state


def _read_shader_capture_session_state(binding: dict[str, str], pid: int) -> str:
    """Distinguish a closed capture from an unavailable but possibly live owner.

    A stalled UI heartbeat is not evidence that another GUI may take over.
    Physical process/session facts are read locally; no startup UI RPC occurs.
    """
    from .ipc_client import INSTANCE_ROOT, IPCClient

    if not IPCClient._process_alive(pid):
        return "expired"
    try:
        value = _shader_capture_mailbox(Path(INSTANCE_ROOT) / binding["instanceId"])
        if (not isinstance(value, dict) or value.get("pid") != pid
                or value.get("instanceId") != binding["instanceId"]):
            return "unavailable"
        if value.get("stopped") is True or value.get("captureLoaded") is False:
            return "expired"
        session = value.get("captureSessionId")
        capture_path = value.get("capturePath")
        if (value.get("captureLoaded") is not True or not isinstance(session, str)
                or SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(session) is None
                or not isinstance(capture_path, str) or not capture_path):
            return "unavailable"
        if (os.path.normcase(str(Path(capture_path).resolve()))
                != os.path.normcase(str(Path(binding["capturePath"]).resolve()))):
            return "expired"
        if session != binding["captureSessionId"]:
            # Reloading the same RDC destroys its GPU replacements and ABI
            # leases. Retire this job without changing its frozen identity;
            # reconcile_capture may reuse durable consent in a newly admitted
            # job, but must never carry an old replacement token into it.
            return "expired"
        return "current"
    except (OSError, TypeError, ValueError):
        return "unavailable"


def _freeze_shader_live_session(instance: str, session: str, pid: int,
                                capture_path: str) -> ShaderLiveEditSession:
    from .ipc_client import bind_direct_tool_call

    # Exactly the three keys the persisted-binding validator and the bridge accept.
    binding = {"instanceId": instance, "captureSessionId": session,
               "capturePath": capture_path}

    def state() -> str:
        return _shader_capture_session_state(binding, pid)

    def apply_candidate(workspace_path: str, revision: int, candidate_sha256: str) -> dict[str, Any]:
        from . import server

        session_state = _shader_capture_session_state(binding, pid, fresh=True)
        if session_state == "unavailable":
            raise ShaderLiveEditSessionUnavailable(
                "Shader live-edit capture session is temporarily unavailable"
            )
        if session_state != "current":
            raise RuntimeError("Shader live-edit capture session is no longer current")
        # This closure belongs to one job. A later job for the same workspace
        # cannot retarget an old in-flight worker or inherit its ABI lease. The session
        # A reload expires this frozen binding and requires a newly admitted job.
        with bind_direct_tool_call(instance, binding["captureSessionId"]):
            from . import shader_saved_publication
            from .ipc_client import _CALL_BINDING
            from .shader_live_edit import current_live_apply_owner
            prepared = None
            preparation_error = ""
            expected = {"revision": revision, "candidateRawSha256": candidate_sha256}
            if current_live_apply_owner(workspace_path, expected, _CALL_BINDING.get()) is not None:
                try:
                    prepared = shader_saved_publication.prepare(
                        server, workspace_path, revision, candidate_sha256, binding, manual_edit=True)
                except Exception as exc:
                    preparation_error = str(exc)[:512]
            try:
                result = server._shader_live_edit_apply_candidate(workspace_path, revision, candidate_sha256)
            except Exception:
                try:
                    shader_saved_publication.cancel(prepared)
                except Exception:
                    pass
                raise
            source_publication = None
            if prepared is not None and result.get("applied") is not True:
                try:
                    shader_saved_publication.cancel(prepared)
                except Exception:
                    # Cleanup must not hide the actual compiler/ABI rejection.
                    pass
            elif prepared is not None:
                try:
                    source_publication = shader_saved_publication.complete(prepared, result)
                except Exception as exc:
                    source_publication = {"status": "pending", "message": str(exc)[:512]}
            replacement = result.get("replacement") or {}
            applied_binding = result.get("binding") or {}
            if (result.get("applied") is True
                    and result.get("validation", {}).get("manualEdit") is not True
                    and applied_binding.get("revision") == revision
                    and applied_binding.get("candidateRawSha256") == candidate_sha256
                    and isinstance(replacement.get("replacementId"), str)):
                # A saved source that was actually applied is the project head
                # other RDCs must restore. This is a delivery step after the
                # physical transaction: a disk/project failure cannot undo or
                # misreport the successful live replacement.
                try:
                    if state() != "current":
                        raise RuntimeError("capture changed before project source publication")
                    from .project_restore import _RESTORE_GUARD, sync_active_shader_workspace_after_apply
                    # Restore can wait for this apply worker to stop. Do not
                    # wait back on its lock while the RDC is being replaced.
                    if not _RESTORE_GUARD.acquire(blocking=False):
                        raise RuntimeError("project restore is busy; source publication is pending")
                    try:
                        publication = sync_active_shader_workspace_after_apply(
                            workspace_path, revision, candidate_sha256, replacement["replacementId"],
                            embed_capture_index=False)
                    finally:
                        _RESTORE_GUARD.release()
                    result["projectPublication"] = {
                        "status": "committed" if publication.get("projectCommitted") is True else "pending",
                        "knowledgeId": str((publication.get("knowledge") or {}).get("knowledgeId") or ""),
                        "message": str(publication.get("warning") or "")[:512],
                    }
                except Exception as exc:
                    result["projectPublication"] = {"status": "pending", "message": str(exc)[:512]}
            if source_publication is not None:
                if source_publication.get("status") in {"committed", "superseded"}:
                    # Auxiliary RDC association/alias receipts must not turn a
                    # durable source-head commit into a false pending result.
                    auxiliary = result.get("projectPublication") or {}
                    if auxiliary.get("status") == "pending":
                        source_publication["message"] = auxiliary.get("message", "")
                result["projectPublication"] = source_publication
            elif preparation_error and result.get("projectPublication", {}).get("status") not in {"committed", "superseded"}:
                result["projectPublication"] = {"status": "pending", "message": preparation_error}
            if (result.get("applied") is True and applied_binding.get("revision") == revision
                    and applied_binding.get("candidateRawSha256") == candidate_sha256):
                # Names are display metadata. Restore them after the successful
                # native transaction, outside the compile/apply critical path.
                try:
                    from .shader_workspace.alias_live_restore import schedule
                    result["displayAliases"] = schedule(
                        server, workspace_path, revision, candidate_sha256, binding)
                except Exception as exc:
                    result["displayAliases"] = {"status": "failed", "diagnostic": str(exc)[:400]}
            return result

    def recover_active_candidate(workspace_path: str) -> dict[str, Any] | None:
        from . import server
        if state() != "current":
            raise ShaderLiveEditSessionUnavailable("Shader resume capture session is unavailable")
        with bind_direct_tool_call(instance, binding["captureSessionId"]), _shader_live_start_phase("nativeRecovery"):
            result = server._shader_live_edit_recover_active_candidate(workspace_path, binding)
        if state() != "current":
            raise ShaderLiveEditSessionUnavailable("Shader resume capture session changed")
        return result

    return ShaderLiveEditSession(binding, state, apply_candidate, recover_active_candidate)


@_shader_live_start_phase("workspaceIdentity")
def _bound_shader_workspace_identity(workspace_path: str) -> dict[str, Any]:
    from . import server
    from .ipc_client import _CALL_BINDING, bind_direct_tool_call
    from .shader_workspace.watcher import validate_watch_start_status
    from .shader_workspace.status_snapshot import bound_status_snapshot

    service = server._shader_workspace_service()
    snapshot = bound_status_snapshot(workspace_path)
    status = snapshot.validated_status() if snapshot is not None else service.status(workspace_path)
    if status.get("toolchainStatus") == "refresh-required":
        service.prepare_compile_binding(workspace_path)
        status = service.status(workspace_path)
    # Historical authorizations can survive a toolchain upgrade. Reject their
    # non-executable lane before any replay/catalog request: starting a watcher
    # would reject the same status after an otherwise unnecessary catalog export.
    validate_watch_start_status(status)
    _automatic_shader_resume_checkpoint()
    identity = server._shader_live_edit_workspace_identity(workspace_path, status=status)
    from .ipc_client import INSTANCE_ROOT, IPCClient

    # Discovery only chooses a unique live mailbox. A new job must then prove
    # the workspace's exact catalog before it publishes any watcher or editor
    # authority. Automatic resume runs on the healthy Host's owner worker, not
    # its startup/heartbeat thread, so that preflight may safely wait for Qt.
    direct = _CALL_BINDING.get()
    directories = ([Path(INSTANCE_ROOT) / direct[0]] if direct is not None
                   else list(Path(INSTANCE_ROOT).glob("*"))[:1024])
    matches = []
    expected_path = os.path.normcase(str(Path(identity["capturePath"]).resolve()))
    for directory in directories:
        manifest = IPCClient._manifest(str(directory))
        if not manifest or manifest.get("captureLoaded") is not True:
            continue
        session = manifest.get("captureSessionId", "")
        if (not isinstance(session, str) or SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(session) is None
                or (direct is not None and session != direct[1])):
            continue
        if os.path.normcase(str(Path(manifest.get("capturePath", "")).resolve())) == expected_path:
            matches.append((directory.name, session, manifest.get("pid")))
    if len(matches) != 1:
        raise RuntimeError("Shader live-edit requires one matching live qrenderdoc capture")
    instance, session, pid = matches[0]
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise RuntimeError("Shader live-edit mailbox has an invalid process identity")
    frozen = _freeze_shader_live_session(instance, session, pid, str(identity["capturePath"]))
    frozen_state = frozen.state()
    if frozen_state == "unavailable":
        raise ShaderLiveEditDeferred(
            "Shader live-edit capture mailbox is temporarily unavailable"
        )
    if frozen_state != "current" or frozen.binding["captureSessionId"] != session:
        raise RuntimeError("Shader live-edit capture changed while binding its job")
    with bind_direct_tool_call(instance, session):
        _automatic_shader_resume_checkpoint()
        with _shader_live_start_phase("capturePreflight"):
            context = server._workspace_active_capture_context(workspace_path, status=status)
    capture = context.get("capture") if isinstance(context, dict) else None
    final_state = frozen.state()
    if final_state == "unavailable":
        raise ShaderLiveEditDeferred(
            "Shader live-edit capture mailbox became temporarily unavailable"
        )
    if (
        not isinstance(capture, dict)
        or capture.get("captureId") != identity["captureId"]
        or capture.get("captureSessionId") != session
        or not isinstance(capture.get("path"), str)
        or os.path.normcase(str(Path(capture["path"]).resolve())) != expected_path
        or final_state != "current"
        or frozen.binding["captureSessionId"] != session
    ):
        raise RuntimeError("Shader live-edit capture changed during catalog identity validation")
    _automatic_shader_resume_checkpoint()
    with _SHADER_LIVE_EDIT_BINDING_GUARD:
        _SHADER_LIVE_EDIT_BINDINGS[os.path.normcase(str(Path(workspace_path).resolve()))] = frozen
    return identity


def _bound_shader_session(workspace_path: str) -> ShaderLiveEditSession:
    with _SHADER_LIVE_EDIT_BINDING_GUARD:
        binding = _SHADER_LIVE_EDIT_BINDINGS.get(os.path.normcase(str(Path(workspace_path).resolve())))
    if binding is None:
        raise RuntimeError("Shader live-edit has no bound qrenderdoc capture session")
    return binding


@contextmanager
def _shader_live_edit_start_context(workspace_path: str):
    from . import server
    from .shader_workspace.status_snapshot import bind_status_snapshot, capture_status_snapshot
    from .shader_workspace.watcher import validate_watch_start_status

    _automatic_shader_resume_checkpoint()
    with _shader_live_start_phase("statusSnapshot"):
        service = server._shader_workspace_service()
        from .shader_restore_admission import restored_snapshot
        snapshot = restored_snapshot(service, workspace_path)
        if snapshot is None:
            snapshot = capture_status_snapshot(service, workspace_path)
        validate_watch_start_status(snapshot.validated_status())
    _automatic_shader_resume_checkpoint()
    with bind_status_snapshot(snapshot):
        yield


def shader_live_edit_manager() -> ShaderLiveEditManager:
    """Return the one bounded internal Shader live-edit manager.

    The neutral service owns manager lifetime.  The implementation functions
    are imported explicitly from the transport-free high-level tool module;
    server.py defers all FastMCP imports/construction until an external
    transport calls run() or streamable_http_app().
    """

    global _SHADER_LIVE_EDIT_MANAGER
    with _SHADER_LIVE_EDIT_MANAGER_GUARD:
        if _SHADER_LIVE_EDIT_MANAGER is None:
            from .server import (
                _SHADER_LIVE_EDIT_REGISTRY_PATH,
                _shader_live_edit_workspace_migration_evidence,
                _shader_workspace_watch_manager,
                _shader_live_edit_apply_candidate,
            )

            _SHADER_LIVE_EDIT_MANAGER = ShaderLiveEditManager(
                _shader_workspace_watch_manager(),
                _shader_live_edit_apply_candidate,
                registry_path=_SHADER_LIVE_EDIT_REGISTRY_PATH,
                workspace_identity=_bound_shader_workspace_identity,
                bind_session=_bound_shader_session,
                start_context=_shader_live_edit_start_context,
                phase_context=_shader_live_start_phase,
                workspace_migration_evidence=(
                    _shader_live_edit_workspace_migration_evidence
                ),
            )
        return _SHADER_LIVE_EDIT_MANAGER


def shutdown_shader_live_edits() -> None:
    with _SHADER_LIVE_EDIT_MANAGER_GUARD:
        manager = _SHADER_LIVE_EDIT_MANAGER
    if manager is not None:
        manager.shutdown()


class InternalToolService:
    """Closed process-owned facade for qrenderdoc's high-level operations."""

    def __init__(
        self,
        *,
        project: Any | None = None,
        restore_jobs: ProjectRestoreJobRegistry | None = None,
        shader_manager: Any | None = None,
    ):
        self.project = project or project_service()
        self.restore_jobs = restore_jobs or ProjectRestoreJobRegistry()
        self.shader_manager = shader_manager
        self._shader_refresh_guard = threading.Lock()
        if isinstance(self.restore_jobs, ProjectRestoreJobRegistry):
            self.restore_jobs._local_shader_dispatcher = self.shader_live_edit_operation

    def start(self) -> dict[str, Any]:
        if self.shader_manager is None:
            self.shader_manager = shader_live_edit_manager()
        # Publication of health must precede capture resume. The existing
        # registered-owner tick handles cold start and GUI reopen alike.
        return {"enabled": True, "attempted": False, "resumeDeferredToOwnerTick": True}

    def save_shader_source(
        self, payload: bytes | bytearray | memoryview
    ) -> tuple[int, dict[str, Any]]:
        if self.shader_manager is None:
            raise RuntimeError("internal Shader service has not started")
        return shader_editor_save_response(payload, self.shader_manager)

    def check_shader_source(
        self, payload: bytes | bytearray | memoryview
    ) -> tuple[int, dict[str, Any]]:
        if self.shader_manager is None:
            raise RuntimeError("internal Shader service has not started")
        return shader_editor_check_response(payload, self.shader_manager)

    @contextmanager
    def _automatic_shader_resume(self, scope: Mapping[str, str]):
        from . import server
        if getattr(server, "_shader_editors_on_demand", False) is True:
            # Listing and idle owner discovery must not activate all historical
            # edit subscriptions. Explicit start and save still bind one owner.
            yield False
            return
        reserve = getattr(self.restore_jobs, "reserve_shader_resume", None)
        if callable(reserve):
            with reserve(scope) as admitted:
                yield admitted
        else:
            # Historical embedders can supply a registry without scheduling.
            yield True

    def refresh_shader_sessions(self, owner_pids: list[int]) -> dict[str, Any]:
        """Reconcile registered GUI captures during the existing owner tick.

        This reads at most 1024 mailboxes and never calls back into the UI.
        Multiple live GUIs with the same capture are ambiguous; only an
        explicit current-capture operation may choose between them.
        """
        from .ipc_client import INSTANCE_ROOT, IPCClient, bind_direct_tool_call
        from . import server
        if getattr(server, "_shader_editors_on_demand", False) is True:
            return {"attempted": False, "results": [], "reason": "editors-on-demand"}

        reconcile = getattr(self.shader_manager, "reconcile_capture", None)
        if not callable(reconcile) or not self._shader_refresh_guard.acquire(blocking=False):
            return {"attempted": False, "results": []}
        try:
            owners = {pid for pid in owner_pids if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0}
            seen_owners: set[int] = set()
            unavailable_owner = False
            by_capture: dict[str, list[dict[str, str]]] = {}
            directories = []
            for directory in Path(INSTANCE_ROOT).glob("*"):
                if len(directories) >= 1024:
                    return {"attempted": False, "results": [], "reason": "mailbox scan limit reached"}
                directories.append(directory)
            for directory in directories:
                # Include a physically live but busy GUI in uniqueness. The
                # ordinary IPC discovery filters old heartbeats, which would
                # incorrectly erase a still-active competing capture here.
                manifest = _shader_capture_mailbox(directory)
                if (not manifest or manifest.get("pid") not in owners
                        or manifest.get("instanceId") != directory.name):
                    continue
                seen_owners.add(manifest["pid"])
                if manifest.get("stopped") is True or manifest.get("captureLoaded") is False:
                    continue
                session = manifest.get("captureSessionId")
                path = manifest.get("capturePath")
                if (not isinstance(session, str) or SHADER_EDITOR_SAVE_ID_PATTERN.fullmatch(session) is None
                        or not isinstance(path, str) or not path or not Path(path).is_absolute()):
                    unavailable_owner = True
                    continue
                scope = {"instanceId": directory.name, "captureSessionId": session, "capturePath": path}
                state = _shader_capture_session_state(scope, int(manifest["pid"]))
                if state != "current":
                    unavailable_owner |= state == "unavailable"
                    continue
                by_capture.setdefault(os.path.normcase(str(Path(path).resolve())), []).append(scope)
            if unavailable_owner or any(IPCClient._process_alive(pid) for pid in owners - seen_owners):
                return {"attempted": False, "results": [], "reason": "registered GUI mailbox is unavailable"}
            results = []
            for candidates in by_capture.values():
                if len(candidates) != 1:
                    continue
                scope = candidates[0]
                if IPCClient._manifest(str(Path(INSTANCE_ROOT) / scope["instanceId"])) is None:
                    continue
                try:
                    with self._automatic_shader_resume(scope) as admitted:
                        if not admitted:
                            results.append({"binding": scope, "deferred": True,
                                            "reason": "project-restore"})
                            continue
                        with bind_direct_tool_call(scope["instanceId"], scope["captureSessionId"]):
                            result = reconcile(scope)
                    results.append({"binding": scope, "result": result})
                except Exception as exc:
                    results.append({"binding": scope, "errorType": type(exc).__name__, "message": str(exc)[:2048]})
            return {"attempted": True, "results": results}
        finally:
            self._shader_refresh_guard.release()

    def asset_index_operation(self, payload: bytes) -> tuple[int, dict[str, Any]]:
        """Run the project asset scan in this long-lived process (see asset_index_jobs)."""

        from . import server
        from .asset_index_jobs import ensure_index_job, index_job_status

        try:
            value = parse_asset_index_operation(payload)
            unused_service, status, asset_root, database_path = server._active_asset_catalog_context(
                require_catalog=False, create_dictionary=(value["operation"] == "ensure"))
            build_id = str(status.get("scope", {}).get("buildId", "") or "")
            if value["operation"] == "ensure":
                job = ensure_index_job(database_path, asset_root, build_id, depth=value["depth"], owner="ReplayLabInternal")
            else:
                job = index_job_status(database_path)
            catalog = None
            try:
                if database_path.is_file():
                    catalog = server.AssetCatalogService(database_path).status()
            except Exception as exc:  # noqa: BLE001 - a half-written catalog is reported, not fatal
                catalog = {"ready": False, "reason": str(exc)[:200]}
            return 200, {"schema": ASSET_INDEX_OPERATION_SCHEMA, "requestId": value["requestId"],
                         "result": {"job": job, "catalog": catalog, "databasePath": str(database_path),
                                    "assetRoot": str(asset_root), "buildId": build_id}}
        except (ValueError, UnicodeError) as exc:
            return 400, {"error": str(exc)[:2048]}
        except Exception as exc:
            return 409, {"error": str(exc)[:2048]}

    def shader_live_edit_operation(self, payload: bytes) -> tuple[int, dict[str, Any]]:
        from . import server
        from .ipc_client import bind_direct_tool_call

        timing = None
        timing_token = None
        started = time.perf_counter()
        try:
            value = parse_shader_live_edit_operation(payload)
            if value["operation"] == "start":
                timing = {"phasesMs": {}, "failedPhase": ""}
                timing_token = _SHADER_LIVE_START_TIMING.set(timing)
            if self.shader_manager is None:
                raise RuntimeError("internal Shader service has not started")
            binding = value["binding"]
            with bind_direct_tool_call(binding["instanceId"], binding["captureSessionId"]):
                with _shader_live_start_phase("captureIdentity"):
                    context = server._active_live_context(allow_missing_capture_id=True)
                    capture = context["capture"]
                    if (capture["captureSessionId"] != binding["captureSessionId"]
                            or os.path.normcase(str(Path(capture["path"]).resolve())) != os.path.normcase(str(Path(binding["capturePath"]).resolve()))):
                        raise RuntimeError("Shader live-edit capture session changed")
                operation = value["operation"]
                if operation == "list":
                    reconcile = getattr(self.shader_manager, "reconcile_capture", None)
                    if callable(reconcile):
                        with self._automatic_shader_resume(binding) as admitted:
                            if admitted:
                                reconcile(binding)
                elif operation in {"poll", "stop"}:
                    require_binding = getattr(self.shader_manager, "require_capture_binding", None)
                    if callable(require_binding):
                        require_binding(value["parameters"]["live_edit_id"], binding)
                method = {"start": self.shader_manager.start, "list": self.shader_manager.list,
                          "poll": self.shader_manager.poll, "stop": self.shader_manager.stop,
                          "migrate": self.shader_manager.migrate_authorization,
                          "convert-hlsl": _convert_workspace_to_hlsl_now,
                          "apply": _apply_workspace_candidate_now,
                          "compiler-contract": lambda workspace_path, source_workspace_id, expected_raw_sha256:
                              server._shader_workspace_service().compiler_contract(
                                  workspace_path, source_workspace_id, expected_raw_sha256),
                          "save-source": _save_workspace_source_now,
                          "save-and-apply-source": lambda **args: _save_and_apply_editor_source(self.shader_manager, args, binding=binding),
                          "open-source-document": _open_workspace_source_document_now,
                          "save-source-document": _save_workspace_source_document_now,
                          "save-and-apply-source-document": lambda **args: _save_and_apply_editor_source(self.shader_manager, args, binding=binding, include=True),
                          "restore": _restore_workspace_original}[operation]
                with _shader_live_start_phase("startAdmission"):
                    result = method(**value["parameters"])
                if operation == "start" and isinstance(result, dict) and result.get("joinedExistingSession") is True:
                    self.shader_manager.require_capture_binding(result["liveEditId"], binding)
                if operation == "list" and isinstance(result, dict):
                    # Retained historical jobs and a still-live foreign GUI
                    # are not active editor authority for this caller.
                    result["sessions"] = [item for item in result.get("sessions", [])
                                          if capture_bindings_match(item.get("captureBinding"), binding)]
            if not isinstance(result, dict):
                raise RuntimeError("Shader live-edit operation returned invalid state")
            if timing is not None:
                result["startTiming"] = _shader_live_start_timing_result(timing, started)
            return 200, {"schema": SHADER_LIVE_EDIT_OPERATION_SCHEMA, "requestId": value["requestId"],
                         "binding": binding, "result": result}
        except (ValueError, UnicodeError) as exc:
            return 400, {"error": str(exc)[:2048], **({"startTiming": _shader_live_start_timing_result(timing, started)} if timing is not None else {})}
        except Exception as exc:
            from .shader_workspace.errors import WorkspaceWatchConflictError
            return 409, {"error": str(exc)[:2048],
                         "conflict": isinstance(exc, WorkspaceWatchConflictError),
                         **({"startTiming": _shader_live_start_timing_result(timing, started)} if timing is not None else {})}
        finally:
            if timing_token is not None:
                _SHADER_LIVE_START_TIMING.reset(timing_token)

    def shutdown(self) -> None:
        self.restore_jobs.shutdown(wait=True)
        shutdown_shader_live_edits()
