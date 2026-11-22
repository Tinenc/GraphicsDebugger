"""Persistent-host orchestration for safe ReplayLab project restoration.

The RDC section consumed here is only a disposable routing cache. Shaders
normally pass a bounded target-resource match and single-anchor live ABI
transaction. A separate same-file content snapshot can reuse Bridge-signed
successful validation while independently checking all current bytes. CBuffer
names and field aliases are restored from exact ABI/layout knowledge, while
numeric values and material snapshots remain explicit current-EID operations.

This module intentionally exposes synchronous functions. The persistent Host
runs them in its worker pool. Most progress originates on that worker; bounded
legacy export telemetry may arrive on a sidecar reader thread, so `_Progress`
serializes callbacks before the Host forwards them to UI code.
"""

from __future__ import annotations

import contextlib
import base64
import copy
import hashlib
import inspect
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import threading
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from .projects import MAX_CAPTURE_CATALOG_BYTES, MAX_PROJECT_BYTES, PROJECT_SCHEMA, ReplayProjectError, ReplayProjectService, _reuse_api_family
from . import rdc_project_index as _rdc_project_index
from .rdc_project_index import (
    RDC_PROJECT_INDEX_VERSION,
    RDC_PROJECT_INDEX_V2_VERSION,
    RdcProjectIndexError,
    encode_index,
    new_index,
    parse_index,
    select_project_binding,
    upsert_project_binding,
)
from .rdc_shader_inventory_cache import (
    RdcShaderInventoryCacheError,
    load_shader_inventory_cache,
    write_shader_inventory_cache,
)
from .shader_workspace.io import atomic_write_bytes, is_within, read_json_object
from .shader_source_publication import source_only_restore_reason
from .symbols.scanner import interface_hash, interface_is_complete, layout_hash
from .ipc_client import (
    IPCBridgeResponseError, IPCTransportError, _CALL_BINDING,
    resume_after_foreground_yield,
)


RESTORE_SCHEMA = "replaylab-project-restore/v1"
RESTORE_PROGRESS_SCHEMA = "replaylab-project-restore-progress/v1"
SHADER_SYNC_SCHEMA = "replaylab-project-shader-sync/v1"
MATERIAL_SYNC_SCHEMA = "replaylab-project-material-sync/v1"

_RESTORE_GUARD = threading.RLock()
_RESTORE_THREAD_STATE = threading.local()
_MAX_TRACKED_MATERIAL_SYNC_SESSIONS = 128
_MATERIAL_SYNC_PARAMETERS: dict[str, dict[str, Any]] = {}
_SHA256_CHARS = frozenset("0123456789abcdef")
_HEX32_CHARS = _SHA256_CHARS
CATALOG_INDEX_REQUIRED_REASON_ID = "catalog-index-required"
_MAX_FAST_PROJECT_SHADERS = 256
_MAX_TARGET_RAW_SHADER_HASHES = 16
_MAX_FAST_PROJECT_BLOCKS = 4096
_MAX_FAST_CATALOG_SHADERS = 65536
_MAX_FAST_EVIDENCE_USES = 65536
_MAX_FAST_CATALOG_BYTES = 16 * 1024 * 1024
_MAX_FAST_REFLECTION_DEPTH = 24
_MAX_FAST_REFLECTION_FIELDS = 4096
_MAX_FAST_REFLECTION_SIGNATURES = 2048
_MAX_FAST_REFLECTION_BINDINGS = 4096
_MAX_FAST_REFLECTION_SAMPLERS = 2048
_MAX_FAST_REFLECTION_STRING_BYTES = 1024
_MAX_FAST_REFLECTION_TOTAL_STRING_BYTES = 1024 * 1024
_MAX_RESTORE_ALIAS_TARGETS = 256
_MAX_RESTORE_ALIAS_CANDIDATES = 4096
_MAX_RESTORE_ALIASES = 65536
_FAST_INDEX_POLL_SECONDS = 0.025
_FAST_INDEX_TIMEOUT_SECONDS = 15.0
_MAX_INTERNAL_TOOL_SPILL_BYTES = 8 * 1024 * 1024


def _workspace_artifact_service(host: Any) -> Any:
    """Persisted bytes/metadata never depend on an unrelated installed tool.

    Execution paths keep the normal service. The fallback preserves older
    Host adapters which expose only the original service interface.
    """
    factory = getattr(host, "_shader_workspace_artifact_service", None)
    return factory() if callable(factory) else host._shader_workspace_service()


def _is_transient_bridge_poll_timeout(error: BaseException) -> bool:
    """Recognise a mailbox wait delayed by foreground RenderDoc work."""

    message = str(error or "").strip()
    return (
        message.startswith("Timeout (")
        and "Make sure RenderDoc is running with capture loaded" in message
    )


_PREPARE_PROGRESS_MESSAGES = {
    "prepare-export-start": "Starting full-capture Shader export",
    "prepare-export-wait": "Waiting for full-capture Shader export",
    "export-shader-bindings": "Exporting Shader bindings",
    "prepare-export-validate": "Validating the exported Shader catalog",
    "prepare-project-import": "Importing the Shader catalog into the project",
    "prepare-reuse-plan": "Matching project Shader and CBuffer knowledge",
    "prepare-catalog-register": "Registering the active capture catalog",
    "prepare-symbol-resolve": "Resolving project Shader and CBuffer aliases",
    "prepare-complete": "Current capture project preparation completed",
}
_PREPARE_PROGRESS_RANKS = {
    "prepare-export-start": 0,
    "prepare-export-wait": 0,
    "export-shader-bindings": 1,
    "prepare-export-validate": 2,
    "prepare-project-import": 3,
    "prepare-reuse-plan": 4,
    "prepare-catalog-register": 5,
    "prepare-symbol-resolve": 6,
    "prepare-complete": 7,
}


class CatalogPreparationRequired(RuntimeError):
    """The explicit full-capture project index is not available."""

    def __init__(
        self,
        message: str,
        reason_id: str = CATALOG_INDEX_REQUIRED_REASON_ID,
    ) -> None:
        super().__init__(message)
        self.reason_id = str(reason_id or CATALOG_INDEX_REQUIRED_REASON_ID)


class RestoreContextChanged(RuntimeError):
    """The active project/capture lifecycle changed during restoration."""


class RestoreContextUnavailable(RuntimeError):
    """The current lifecycle could not be read; no change was established."""


class RestoreRollbackFailed(RuntimeError):
    """A live Shader replacement could not be transactionally rolled back."""


class RestoreRetainedShaderApply(RuntimeError):
    """An exact handoff committed, but the current source is not yet verified."""


class RestoreControlStop(RuntimeError):
    """Base class for cooperative cancellation/deadline termination."""


class RestoreCancelled(RestoreControlStop):
    """The owning client or Host requested cooperative cancellation."""


class RestoreDeadlineExceeded(RestoreControlStop):
    """The server-owned monotonic restore budget elapsed."""


def _restore_control_checkpoint(
    cancel_event: Any,
    deadline_monotonic: float | None,
    phase: str,
) -> None:
    """Stop only at transaction-safe boundaries between blocking operations."""

    if cancel_event is not None and cancel_event.is_set():
        raise RestoreCancelled(
            "Bread project restore was cancelled at %s" % phase
        )
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        raise RestoreDeadlineExceeded(
            "Bread project restore exceeded its server deadline at %s" % phase
        )


def _host_module() -> Any:
    # Lazy import avoids a server -> project_restore -> server cycle when the
    # persistent host imports this module during process start.
    from . import server

    return server


def automatic_project_sync_suppressed() -> bool:
    """Return whether the current worker is already restoring project state."""

    return bool(getattr(_RESTORE_THREAD_STATE, "active", False))


def _material_sync_context(
    session: Mapping[str, Any], project_id: str, project_workspace_id: str
) -> str:
    """Bind accumulated leaf IDs to one exact project/session/block lifecycle."""

    return _canonical_sha256(
        {
            "projectId": _hex(project_id, 32, "material projectId"),
            "projectWorkspaceId": _hex(
                project_workspace_id, 32, "material project workspaceId"
            ),
            "captureId": _hex(session.get("captureId"), 64, "material captureId"),
            "captureSessionId": _hex(
                session.get("captureSessionId"), 32, "material captureSessionId"
            ),
            "eventId": _integer(session.get("eventId"), "material eventId", 1),
            "stage": _text(str(session.get("stage", "")), "material stage", 32),
            "shaderResourceId": _integer(
                session.get("shaderResourceId"), "material shaderResourceId", 1
            ),
            "constantBlockIndex": _integer(
                session.get("constantBlockIndex"), "material constantBlockIndex"
            ),
            "constantBlockPath": _text(
                str(session.get("constantBlockPath", "")),
                "material constantBlockPath",
                2048,
            ),
            "layoutSha256": _hex(
                session.get("layoutSha256"), 64, "material layoutSha256"
            ),
        }
    )


def _remember_material_sync_parameters(
    session_id: str,
    session: Mapping[str, Any],
    project_id: str,
    project_workspace_id: str,
    parameter_ids: Sequence[str],
) -> list[str]:
    """Return the deterministic union of leaves changed in one live session.

    Material knowledge stays append-only, but every newly committed head must
    describe the complete set of edits still represented by the current live
    override.  The context digest prevents a stale/foreign session entry from
    contributing IDs, and the bounded table cannot grow with Host uptime.
    """

    token = _hex(session_id, 32, "material sessionId")
    parameters = list(session.get("parameters", []) or [])
    if not parameters or len(parameters) > 256:
        raise RuntimeError("material session parameters are invalid")
    ordered_ids: list[str] = []
    seen: set[str] = set()
    for index, parameter in enumerate(parameters):
        if not isinstance(parameter, Mapping):
            raise RuntimeError("material session parameter[%d] is invalid" % index)
        parameter_id = _hex(
            parameter.get("parameterId"),
            32,
            "material session parameterId",
        )
        if parameter_id in seen:
            raise RuntimeError("material session contains duplicate parameter IDs")
        seen.add(parameter_id)
        ordered_ids.append(parameter_id)
    requested = {
        _hex(value, 32, "material parameterId") for value in parameter_ids
    }
    if not requested or not requested.issubset(seen):
        raise RuntimeError("material parameter IDs are absent from the live session")
    context = _material_sync_context(session, project_id, project_workspace_id)
    previous = _MATERIAL_SYNC_PARAMETERS.pop(token, None)
    if isinstance(previous, Mapping) and previous.get("contextSha256") == context:
        requested.update(list(previous.get("parameterIds", []) or []))
    accumulated = [parameter_id for parameter_id in ordered_ids if parameter_id in requested]
    _MATERIAL_SYNC_PARAMETERS[token] = {
        "contextSha256": context,
        "parameterIds": tuple(accumulated),
    }
    while len(_MATERIAL_SYNC_PARAMETERS) > _MAX_TRACKED_MATERIAL_SYNC_SESSIONS:
        oldest = next(iter(_MATERIAL_SYNC_PARAMETERS))
        _MATERIAL_SYNC_PARAMETERS.pop(oldest, None)
    return accumulated


def _path_key(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def _hex(value: Any, length: int, label: str) -> str:
    text = str(value or "").strip()
    if len(text) != length or any(character not in _SHA256_CHARS for character in text):
        raise RuntimeError("%s must be %d lowercase hexadecimal characters" % (label, length))
    return text


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RuntimeError("%s must be an integer >= %d" % (label, minimum))
    return value


def _text(value: Any, label: str, maximum: int = 4096, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise RuntimeError("%s must be bounded text" % label)
    result = value.strip()
    if (not result and not allow_empty) or len(result) > maximum:
        raise RuntimeError("%s must be bounded text" % label)
    return result


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _asset_api(value: Any) -> str:
    token = str(value or "").strip().lower().rsplit(".", 1)[-1]
    token = token.replace("_", "").replace(" ", "")
    return {
        "direct3d11": "d3d11",
        "direct3d12": "d3d12",
        "gl": "opengl",
        "openglcore": "opengl",
    }.get(token, token)


def _asset_encoding(value: Any) -> str:
    token = str(value or "").strip().lower().rsplit(".", 1)[-1]
    token = token.replace("_", "").replace(" ", "").replace("-", "")
    return {
        "directxbytecode": "dxbc",
        "directxil": "dxil",
        "dxilcontainer": "dxil",
        "spv": "spirv",
        "spirvshader": "spirv",
        "openglspirv": "opengl-spirv",
        "glslshader": "glsl",
        "openglglsl": "glsl",
    }.get(token, token)


def _project_asset_encoding(value: Any) -> str:
    """Return the established project-manifest spelling for one encoding.

    The Bridge protocol and the seven-field stable-key material deliberately
    use lowercase tokens, while the persistent project schema has always used
    display spellings such as ``DXIL`` and ``SPIR-V``.  Keep that representation
    boundary explicit so normalising an otherwise exact live inventory cannot
    make its snapshot compare unequal to the request that produced it.
    """

    protocol = _asset_encoding(value)
    display = {
        "dxbc": "DXBC",
        "dxil": "DXIL",
        "spirv": "SPIR-V",
        "opengl-spirv": "OpenGL-SPIR-V",
        "glsl": "GLSL",
        "metallib": "METALLIB",
    }.get(protocol)
    if display is None:
        raise RuntimeError("Shader encoding is unsupported or not canonicalizable")
    return display


def _asset_format_supported(api: str, encoding: str) -> bool:
    return encoding in {
        "d3d11": {"dxbc"},
        "d3d12": {"dxbc", "dxil"},
        "opengl": {"glsl", "spirv", "opengl-spirv"},
        "vulkan": {"spirv"},
    }.get(api, set())


def _reject_json_constant(value: str) -> None:
    raise RuntimeError("tool wrapper contains non-finite JSON: %s" % value)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError("tool wrapper contains duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _expand_internal_tool_spill(
    value: dict[str, Any], operation: str
) -> dict[str, Any]:
    """Read a Host-owned exact result when the public tool returned a preview.

    Public MCP responses are deliberately compacted so vendor clients do not
    lose a large result in their transport.  Project restore runs inside that
    same trusted Host process and needs the complete prepare receipt.  Treating
    the compact preview as the full schema caused one identical failure for
    every Shader in a capture.  Resolve only the content-addressed result-spill
    pointer, below the Host-owned directory and within the restore byte bound.
    """

    compacted = value.get("_compacted")
    pointer = value.get("completeContent")
    if compacted is None and pointer is None:
        return value
    if (
        not isinstance(compacted, Mapping)
        or compacted.get("schema") != "replaylab-complete-result-spill/v1"
        or compacted.get("mode") != "exact-content-pointer"
        or not isinstance(pointer, Mapping)
        or pointer.get("mediaType") != "application/json"
        or pointer.get("encoding") != "utf-8"
    ):
        raise RuntimeError("%s returned an invalid compact-result receipt" % operation)
    path_text = pointer.get("path")
    byte_size = pointer.get("byteSize")
    digest = pointer.get("sha256")
    if (
        not isinstance(path_text, str)
        or not path_text
        or "\0" in path_text
        or isinstance(byte_size, bool)
        or not isinstance(byte_size, int)
        or byte_size <= 0
        or byte_size > _MAX_INTERNAL_TOOL_SPILL_BYTES
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in _SHA256_CHARS for character in digest)
    ):
        raise RuntimeError("%s returned an invalid compact-result identity" % operation)
    from . import server

    root = (
        server._asset_tool_output_root() / server._RESULT_SPILL_PREFIX
    ).resolve(strict=True)
    raw_path = Path(path_text).expanduser()
    if not raw_path.is_absolute() or raw_path.is_symlink():
        raise RuntimeError("%s compact result path is invalid" % operation)
    target = raw_path.resolve(strict=True)
    if not target.is_file() or not is_within(root, target):
        raise RuntimeError("%s compact result escaped Host storage" % operation)
    data = target.read_bytes()
    if len(data) != byte_size or hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError("%s compact result content changed" % operation)
    try:
        exact = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("%s compact result is not exact JSON" % operation) from exc
    if not isinstance(exact, dict):
        raise RuntimeError("%s compact result is not an object" % operation)
    return exact


def _tool_json(
    payload: Any,
    operation: str,
    *,
    schema: str | None = None,
    schema_version: int | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, str):
        raise RuntimeError("%s returned a non-JSON wrapper" % operation)
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("%s returned invalid JSON" % operation) from exc
    if not isinstance(value, dict):
        raise RuntimeError("%s returned a non-object JSON wrapper" % operation)
    value = _expand_internal_tool_spill(value, operation)
    if value.get("error"):
        error = RuntimeError("%s failed: %s" % (operation, value.get("error")))
        if isinstance(value.get("data"), Mapping):
            error.tool_data = copy.deepcopy(value["data"])
        raise error
    if schema is not None and value.get("schema") != schema:
        raise RuntimeError(
            "%s returned an unsupported schema %r (expected %r)"
            % (operation, value.get("schema"), schema)
        )
    if schema_version is not None and value.get("schemaVersion") != schema_version:
        raise RuntimeError("%s returned an unsupported schema version" % operation)
    return value


class _Progress:
    def __init__(
        self,
        operation_id: str,
        callback: Callable[[dict[str, Any]], Any] | None,
        warnings: list[str],
    ):
        if callback is not None and not callable(callback):
            raise TypeError("progress_callback must be callable or None")
        self.operation_id = operation_id
        self.callback = callback
        self.warnings = warnings
        # Full-capture export telemetry is forwarded by a sidecar polling
        # thread while the restore worker owns the physical operation.  Keep
        # callbacks serialized and suppress exact duplicates so a slow UI
        # consumer cannot be entered concurrently or flooded by an unchanged
        # observation.
        self._lock = threading.RLock()
        self._last_signature: tuple[Any, ...] | None = None
        self._callback_available = callback is not None
        self._phase = ""
        self._phase_started = time.monotonic()
        self._phases_ms: dict[str, float] = {}

    def timings(self) -> dict[str, float]:
        with self._lock:
            result = dict(self._phases_ms)
            if self._phase:
                result[self._phase] = result.get(self._phase, 0) + (time.monotonic()-self._phase_started)*1000
            return {key: round(value,3) for key,value in result.items()}

    @staticmethod
    def _item_from_detail(detail: Mapping[str, Any]) -> str:
        for field in (
            "workspacePath",
            "constantBlockPath",
        ):
            value = detail.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        resource_id = detail.get("resourceId")
        stage = detail.get("stage")
        if isinstance(resource_id, int) and not isinstance(resource_id, bool):
            if isinstance(stage, str) and stage.strip():
                return "Shader %d (%s)" % (resource_id, stage.strip())
            return "Shader %d" % resource_id
        event_id = detail.get("eventId")
        if isinstance(event_id, int) and not isinstance(event_id, bool):
            return "EID %d" % event_id
        return ""

    @staticmethod
    def _safe_item(value: Any) -> str:
        """Return display-only item text without gaining failure authority."""

        if not isinstance(value, str):
            return ""
        clean = value.strip()
        if not clean or any(ord(character) < 0x20 for character in clean):
            return ""
        try:
            encoded = clean.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return ""
        if len(encoded) <= 1024:
            return clean
        return encoded[:1024].decode("utf-8", errors="ignore").rstrip()

    def emit(
        self,
        phase: str,
        message: str,
        *,
        current: int = 0,
        total: int = 0,
        item: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        clean_detail = copy.deepcopy(dict(detail or {}))
        clean_item = self._safe_item(item)
        if not clean_item:
            clean_item = self._safe_item(self._item_from_detail(clean_detail))
        event = {
            "schema": RESTORE_PROGRESS_SCHEMA,
            "schemaVersion": 1,
            "operationId": self.operation_id,
            "phase": _text(phase, "progress.phase", 128),
            "current": _integer(current, "progress.current"),
            "total": _integer(total, "progress.total"),
            "message": _text(message, "progress.message", 1024),
            "item": clean_item,
            "detail": clean_detail,
        }
        if len(event["phase"].encode("utf-8")) > 128:
            raise RuntimeError("progress.phase exceeds 128 UTF-8 bytes")
        if len(event["message"].encode("utf-8")) > 1024:
            raise RuntimeError("progress.message exceeds 1024 UTF-8 bytes")
        if len(event["item"].encode("utf-8")) > 1024:
            raise RuntimeError("progress.item exceeds 1024 UTF-8 bytes")
        if event["total"] and event["current"] > event["total"]:
            raise RuntimeError("progress.current exceeds progress.total")
        signature = (
            event["phase"],
            event["message"],
            event["current"],
            event["total"],
            event["item"],
        )
        with self._lock:
            if phase != self._phase:
                now = time.monotonic()
                if self._phase:
                    self._phases_ms[self._phase] = self._phases_ms.get(self._phase,0)+(now-self._phase_started)*1000
                self._phase, self._phase_started = phase, now
            if signature == self._last_signature:
                return
            self._last_signature = signature
            if not self._callback_available or self.callback is None:
                return
            try:
                self.callback(copy.deepcopy(event))
            except Exception as exc:
                # Progress is advisory. Disable a broken consumer after its
                # first failure instead of retrying it for every Shader/EID.
                self._callback_available = False
                warning = "progress callback failed: %s: %s" % (
                    type(exc).__name__,
                    exc,
                )
                if warning not in self.warnings:
                    self.warnings.append(warning)


def _validate_capture_and_project(
    host: Any,
    project_root: str | Path,
    capture_path: str | Path,
    capture_session_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any], Path, Path, str]:
    raw_project = Path(project_root).expanduser()
    raw_capture = Path(capture_path).expanduser()
    if not raw_project.is_absolute() or not raw_capture.is_absolute():
        raise ValueError("project_root and capture_path must be absolute")
    if raw_project.exists() and raw_project.is_symlink():
        raise RuntimeError("project_root must not be a symlink")
    if raw_capture.is_symlink() or not raw_capture.is_file() or raw_capture.suffix.lower() != ".rdc":
        raise RuntimeError("capture_path must be an existing regular non-symlink .rdc")
    project = raw_project.resolve()
    capture = raw_capture.resolve()
    active = host._active_project_service().status()
    if (
        not isinstance(active, dict)
        or active.get("scopeConfigured") is not True
        or _path_key(active.get("projectRoot", "")) != _path_key(project)
    ):
        raise RuntimeError("the requested bound project is not the active Bread project")
    workspace_id = _hex(active.get("workspaceId"), 32, "active workspaceId")
    replay_status = active.get("replayProject")
    paths = active.get("paths")
    if not isinstance(replay_status, dict) or not isinstance(paths, dict):
        raise RuntimeError("active project omitted its replay-project identity")
    replay_id = _hex(replay_status.get("projectId"), 32, "replay projectId")
    replay_path = Path(str(paths.get("replayProject", "") or "")).expanduser()
    if (
        replay_path.is_symlink()
        or not replay_path.is_dir()
        or _path_key(replay_path) != _path_key(replay_status.get("projectPath", ""))
    ):
        raise RuntimeError("active replay-project path is invalid")
    context = _read_restore_identity_context(host)
    live_capture = context.get("capture")
    if not isinstance(live_capture, dict):
        raise RuntimeError("RenderDoc current context omitted capture identity")
    live_session = _hex(
        live_capture.get("captureSessionId"), 32, "capture.captureSessionId"
    )
    if capture_session_id is not None:
        expected_session = _hex(capture_session_id, 32, "capture_session_id")
        if expected_session != live_session:
            raise RuntimeError("RenderDoc capture session changed before project restore")
    if _path_key(live_capture.get("path", "")) != _path_key(capture):
        raise RuntimeError("RenderDoc loaded capture differs from capture_path")
    # Validate the snapshot once more through the server's race guard when it
    # is available.  Later native calls repeat path/session checks themselves.
    host._require_same_active_project(active)
    return active, context, project, replay_path.resolve(), live_session


def _read_rdc_index(
    host: Any, capture_path: Path, capture_session_id: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    response = host.ipc.call(
        "read_rdc_project_index",
        {
            "expected_capture_path": str(capture_path),
            "expected_capture_session_id": capture_session_id,
        },
        timeout=30.0,
    )
    if (
        not isinstance(response, dict)
        or response.get("schema") != "replaylab-rdc-project-index-transport/v1"
        or response.get("schemaVersion") != 1
        or response.get("captureSessionId") != capture_session_id
        or _path_key(response.get("capturePath", "")) != _path_key(capture_path)
        or not isinstance(response.get("present"), bool)
    ):
        raise RuntimeError("RenderDoc returned an invalid RDC project-index wrapper")
    if response["present"] is False:
        if response.get("document") is not None:
            raise RuntimeError("absent RDC project index carried a document")
        return response, None
    if response.get("sectionVersion") not in (
        1,
        RDC_PROJECT_INDEX_V2_VERSION,
        RDC_PROJECT_INDEX_VERSION,
    ):
        raise RuntimeError("RDC project-index section version is unsupported")
    document = response.get("document")
    if not isinstance(document, Mapping):
        raise RuntimeError("RDC project-index wrapper omitted its document")
    # Canonical encoding performs the strict field/limit/seal validation even
    # though the Bridge has already parsed the transport JSON.
    clean = parse_index(encode_index(document))
    return response, clean


def _inventory_binding_state(
    snapshot: Mapping[str, Any], live_inventory: Mapping[str, Any]
) -> dict[str, Any]:
    """Adapt a sealed project inventory snapshot to the restore read model."""

    if (
        snapshot.get("schema")
        != "renderdoc-ultra-project-shader-inventory-reuse/v1"
        or snapshot.get("schemaVersion") != 1
        or not isinstance(snapshot.get("inventory"), Mapping)
        or not isinstance(snapshot.get("binding"), Mapping)
        or not isinstance(snapshot.get("knowledge"), Mapping)
    ):
        raise RuntimeError("project returned an invalid Shader inventory snapshot")
    binding = snapshot["binding"]
    knowledge = snapshot["knowledge"]
    project_inventory = snapshot["inventory"]
    live_shaders = live_inventory.get("shaders")
    if not isinstance(live_shaders, list):
        raise RuntimeError("live Shader inventory was lost during project matching")
    capture_shaders = [_normal_shader_identity(item) for item in live_shaders]
    capture_shaders.sort(
        key=lambda item: (
            item["resourceId"], item["stage"], item["entryPoint"], item["rawSha256"]
        )
    )

    raw_workspaces = knowledge.get("workspaces")
    if not isinstance(raw_workspaces, Mapping):
        raise RuntimeError("project Shader inventory snapshot omitted HLSL knowledge")
    workspaces: dict[str, dict[str, Any]] = {}
    for knowledge_id, value in raw_workspaces.items():
        if not isinstance(value, Mapping):
            raise RuntimeError("project Shader artifact is malformed")
        physical = value.get("physicalIdentity")
        source = value.get("source")
        if not isinstance(physical, Mapping) or not isinstance(source, Mapping):
            raise RuntimeError("project Shader artifact omitted physical identity")
        stable_key = _hex(
            physical.get("stableKeySha256"),
            64,
            "project Shader stableKeySha256",
        )
        source_sha256 = _hex(
            source.get("sha256"), 64, "project Shader sourceSha256"
        )
        source_format = value.get("sourceFormat", "hlsl")
        if source_format not in {"hlsl", "glsl"}:
            raise RuntimeError("project Shader source format is unsupported")
        compiled_candidate = copy.deepcopy(value.get("compiledCandidate"))
        if isinstance(compiled_candidate, Mapping) and "sha256" in compiled_candidate:
            # Inventory artifacts use sha256; persisted/workspace candidates
            # use rawSha256. Normalize at this read-model boundary so every
            # later source/ABI/artifact comparison still checks the same bytes.
            candidate_sha = _hex(compiled_candidate["sha256"], 64,
                                 "project Shader compiled artifact sha256")
            if ("rawSha256" in compiled_candidate
                    and compiled_candidate["rawSha256"] != candidate_sha):
                raise RuntimeError("project Shader compiled artifact hash fields disagree")
            compiled_candidate = {**compiled_candidate, "rawSha256": candidate_sha}
        workspaces[str(knowledge_id)] = {
            "knowledgeId": str(knowledge_id),
            "api": _asset_api(physical.get("api")),
            "encoding": _asset_encoding(physical.get("encoding")),
            "stage": _text(physical.get("stage"), "project Shader stage", 32),
            "entryPoint": _text(
                physical.get("entryPoint", ""),
                "project Shader entryPoint",
                512,
                allow_empty=True,
            ),
            "rawSha256": _hex(
                physical.get("rawSha256"), 64, "project Shader rawSha256"
            ),
            "abiHash": _hex(
                physical.get("abiHash"), 64, "project Shader abiHash"
            ) if physical.get("abiHash") else "",
            "baselineAbiSha256": _hex(
                physical.get("abiHash"), 64, "project Shader abiHash"
            ) if physical.get("abiHash") else "",
            "layoutHash": _hex(
                physical.get("layoutHash"), 64, "project Shader layoutHash"
            ) if physical.get("layoutHash") else "",
            "stableKeyComplete": value.get("interfaceComplete") is True,
            "stableKeySha256": stable_key,
            "sourceSha256": source_sha256,
            **({"sourceFormat": source_format} if source_format != "hlsl" else {}),
            "source": copy.deepcopy(dict(source)),
            "registeredWorkspace": copy.deepcopy(value.get("registeredWorkspace")),
            "publicationMode": value.get("publicationMode"),
            "sourceOriginPath": value.get("sourceOriginPath"),
            "workspaceId": (value.get("registeredWorkspace") or {}).get("workspaceId"),
            "workspacePath": (value.get("registeredWorkspace") or {}).get("path"),
            "registeredCompiledCandidatePresent": value.get("registeredCompiledCandidatePresent"),
            "targetProfile": copy.deepcopy(value.get("targetProfile")),
            "compiledCandidate": compiled_candidate,
            "compilerBinding": copy.deepcopy(value.get("compilerBinding")),
            "assemblyDisassemblyArtifacts": copy.deepcopy(
                value.get("assemblyDisassemblyArtifacts", [])
            ),
            "cbufferAliasRefs": copy.deepcopy(
                value.get("cbufferAliasRefs", [])
            ),
        }
    raw_materials = knowledge.get("materials")
    if not isinstance(raw_materials, Mapping) or raw_materials:
        raise RuntimeError(
            "resource inventory snapshot attempted event-scoped material reuse"
        )
    return {
        "projectId": binding.get("projectId"),
        "projectPath": snapshot.get("projectPath"),
        "scopeSha256": binding.get("scopeSha256"),
        "captureId": binding.get("captureId"),
        "catalogSha256": binding.get("catalogSha256"),
        "inventorySha256": binding.get("inventorySha256"),
        "knowledgeContentSha256": binding.get("knowledgeContentSha256"),
        "matcherPolicySha256": binding.get("matcherPolicySha256"),
        "capture": {
            "captureId": binding.get("captureId"),
            "catalogSha256": binding.get("catalogSha256"),
            "api": project_inventory.get("capture", {}).get("api"),
            "inventoryOnly": True,
            "shaders": capture_shaders,
        },
        "workspaces": workspaces,
        "materials": {},
        "reusePlan": copy.deepcopy(snapshot.get("reusePlan")),
        "inventoryBinding": copy.deepcopy(dict(binding)),
        "projectInventory": copy.deepcopy(dict(project_inventory)),
    }


def _project_binding_state(
    replay: ReplayProjectService,
    binding: Mapping[str, Any],
    live_inventory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if live_inventory is None:
        state = replay.restore_knowledge_snapshot(
            str(binding.get("captureId", "")),
            str(binding.get("catalogSha256", "")),
        )
    else:
        project_inventory = _project_shader_inventory(live_inventory)
        snapshot = replay.reuse_snapshot_for_shader_inventory(project_inventory)
        state = _inventory_binding_state(snapshot, live_inventory)
    for field in (
        "projectId",
        "scopeSha256",
        "captureId",
        "catalogSha256",
        "knowledgeContentSha256",
        "matcherPolicySha256",
    ):
        expected_length = 32 if field == "projectId" else 64
        _hex(state.get(field), expected_length, "project binding %s" % field)
        if binding.get(field) != state[field]:
            raise RuntimeError("RDC project binding %s is stale" % field)
    return state


def _capture_shader(
    state: Mapping[str, Any], identity: Mapping[str, Any]
) -> dict[str, Any]:
    capture = state.get("capture")
    shaders = capture.get("shaders") if isinstance(capture, Mapping) else None
    if not isinstance(shaders, list):
        raise RuntimeError("project binding capture omitted Shader identities")
    wanted = _normal_shader_identity(identity)
    matches: list[dict[str, Any]] = []
    for item in shaders:
        if not isinstance(item, Mapping):
            continue
        try:
            candidate = _normal_shader_identity(item)
        except Exception:
            continue
        if wanted.get("stableKeyComplete") is True:
            if candidate.get("stableKeyComplete") is True:
                matched = bool(
                    candidate.get("stableKeySha256")
                    == wanted.get("stableKeySha256")
                    and candidate.get("resourceId") == wanted.get("resourceId")
                )
            else:
                # Project catalog revisions predate the v2 stable-key fields.
                # They still bind the exact capture-local owner and reflection
                # layout. The strict catalog projection plus the expected-only
                # live job below must reproduce the missing physical ABI/stable
                # key before this compatibility match can reach an apply.
                matched = all(
                    candidate.get(field) == wanted.get(field)
                    for field in (
                        "resourceId",
                        "stage",
                        "entryPoint",
                        "rawSha256",
                        "layoutHash",
                    )
                )
                # A legacy catalog's representative is an observation, not
                # part of the shared Shader identity. Publication may have
                # checked another use of these exact original bytes. Preserve
                # that receipt's anchor for the later live binding check.
                matched = matched and all(
                    not item.get(field) or normalise(item[field]) == wanted.get(field)
                    for field, normalise in (("api", _asset_api), ("encoding", _asset_encoding))
                )
        else:
            # Legacy project revisions remain readable as routing hints, but
            # their incomplete keys are never upgraded into executable
            # authority. Keep their comparison exact and capture-local.
            matched = all(
                candidate.get(field) == wanted.get(field)
                for field in (
                    "resourceId",
                    "stage",
                    "entryPoint",
                    "rawSha256",
                    "abiHash",
                    "layoutHash",
                )
            )
        if matched:
            matches.append(dict(item))
    if len(matches) != 1:
        raise RuntimeError("RDC Shader association differs from the exact project catalog")
    matched_identity = _normal_shader_identity(matches[0])
    expected_anchor = matched_identity.get("anchorEventId")
    if (wanted.get("anchorEventId") != expected_anchor
            and not (wanted.get("stableKeyComplete") is True
                     and matched_identity.get("stableKeyComplete") is False)):
        raise RuntimeError(
            "RDC Shader representative EID differs from the project inventory"
        )
    return dict(matches[0])


def _resolve_workspace_path(
    project_root: Path, shader_root: Path, relative_path: str
) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        raise RuntimeError("managed workspace path is not project-relative")
    cursor = project_root
    for component in pure.parts:
        cursor = cursor / component
        if cursor.exists() and cursor.is_symlink():
            raise RuntimeError("managed workspace path traverses a symlink")
    workspace = project_root.joinpath(*pure.parts).resolve()
    if not workspace.is_dir() or not is_within(shader_root, workspace):
        raise RuntimeError("managed workspace escaped the active Shader workspace root")
    if workspace.relative_to(project_root).as_posix() != relative_path:
        raise RuntimeError("managed workspace path is not canonical")
    return workspace


def _validate_workspace_record(
    host: Any,
    replay: ReplayProjectService,
    active: Mapping[str, Any],
    project_root: Path,
    capture_path: Path,
    binding_state: Mapping[str, Any],
    record: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    identity = record.get("identity")
    managed = record.get("managedWorkspace")
    if not isinstance(identity, Mapping) or not isinstance(managed, Mapping):
        raise RuntimeError("RDC Shader association is incomplete")
    _capture_shader(binding_state, identity)
    shader_root = Path(str(active.get("paths", {}).get("shaderWorkspaces", ""))).resolve()
    if shader_root.is_symlink() or not shader_root.is_dir():
        raise RuntimeError("active Shader workspace root is unavailable")
    workspace = _resolve_workspace_path(
        project_root, shader_root, str(managed.get("path", ""))
    )
    status = _workspace_artifact_service(host).status(str(workspace))
    capture = status.get("capture") if isinstance(status, Mapping) else None
    shader = status.get("shader") if isinstance(status, Mapping) else None
    source = status.get("source") if isinstance(status, Mapping) else None
    latest = status.get("latestCompiled") if isinstance(status, Mapping) else None
    inventory_binding = bool(
        binding_state.get("capture", {}).get("inventoryOnly")
        if isinstance(binding_state.get("capture"), Mapping)
        else False
    )
    if (
        not isinstance(status, dict)
        or status.get("schema") != "renderdoc-ultra-shader-workspace-status/v1"
        or _path_key(status.get("workspacePath", "")) != _path_key(workspace)
        or status.get("workspaceId") != managed.get("workspaceId")
        or not isinstance(capture, Mapping)
        or not isinstance(shader, Mapping)
        or not isinstance(source, Mapping)
        or not isinstance(latest, Mapping)
        or (
            not inventory_binding
            and capture.get("captureId") != binding_state.get("captureId")
        )
        or (
            not inventory_binding
            and capture.get("catalogSha256") != binding_state.get("catalogSha256")
        )
        or _path_key(capture.get("capturePath", "")) != _path_key(capture_path)
        or shader.get("resourceId") != identity.get("resourceId")
        or status.get("stage") != identity.get("stage")
        or status.get("entryPoint", "") != identity.get("entryPoint", "")
        or shader.get("rawSha256") != identity.get("rawSha256")
        or source.get("present") is not True
        or source.get("currentSha256") != record.get("sourceSha256")
        or latest.get("revision") != record.get("candidateRevision")
        or latest.get("sourceSha256") != record.get("sourceSha256")
        or latest.get("rawSha256") != record.get("candidateRawSha256")
    ):
        raise RuntimeError("managed Shader workspace changed since the RDC index was written")
    knowledge = binding_state.get("workspaces", {}).get(
        str(record.get("knowledgeId", ""))
    )
    artifact_refs = record.get("artifactRefs")
    if not isinstance(knowledge, Mapping):
        raise RuntimeError("RDC Shader association references absent project knowledge")
    if source_only_restore_reason(knowledge, binding_state.get("projectPath")):
        raise RuntimeError("RDC Shader association references a source-only organization draft")
    if (
        knowledge.get("sourceSha256") != record.get("sourceSha256")
        or knowledge.get("stage") != identity.get("stage")
        or knowledge.get("entryPoint", "") != identity.get("entryPoint", "")
        or not _knowledge_matches_shader(knowledge, identity, executable=True)
    ):
        raise RuntimeError("RDC Shader association differs from project knowledge")
    if binding_state.get("capture", {}).get("inventoryOnly") is True:
        compiled = knowledge.get("compiledCandidate")
        compiler_binding = knowledge.get("compilerBinding")
        target_profile = knowledge.get("targetProfile")
        representations = knowledge.get("assemblyDisassemblyArtifacts", [])
        alias_refs = knowledge.get("cbufferAliasRefs", [])
        expected_artifact_refs = {
            "projectArtifactId": str(record.get("knowledgeId", "")),
            "revision": record.get("candidateRevision"),
            "compiledSha256": (
                compiled.get("rawSha256")
                if isinstance(compiled, Mapping)
                else ""
            ),
            "compileConfigSha256": _canonical_sha256(
                compiler_binding
                if isinstance(compiler_binding, Mapping)
                else target_profile
            ),
            "representationsSha256": (
                _canonical_sha256(representations)
                if isinstance(representations, list) and representations
                else ""
            ),
            "aliasesSha256": (
                _canonical_sha256(alias_refs)
                if isinstance(alias_refs, list) and alias_refs
                else ""
            ),
        }
        if (
            not isinstance(artifact_refs, Mapping)
            or dict(artifact_refs) != expected_artifact_refs
        ):
            raise RuntimeError(
                "RDC Shader artifact references differ from the project library"
            )
    return workspace, status


def _physical_enum(value: Any) -> str:
    return (
        str(value or "").strip().lower().rsplit(".", 1)[-1]
        .replace("_", "")
        .replace(" ", "")
    )


def _physical_base_type(value: Any) -> str:
    token = _physical_enum(value)
    return {
        "float32": "float",
        "single": "float",
        "sint": "int",
        "signedint": "int",
        "int32": "int",
        "unsignedint": "uint",
        "uint32": "uint",
        "float16": "half",
        "min16float": "half",
        "float64": "double",
    }.get(token, token)


def _new_fast_reflection_budget() -> dict[str, int]:
    return {
        "fields": 0,
        "signatures": 0,
        "bindings": 0,
        "samplers": 0,
        "stringBytes": 0,
    }


def _fast_reflection_charge(
    budget: dict[str, int], key: str, maximum: int, reason_id: str, label: str
) -> None:
    budget[key] = int(budget.get(key, 0)) + 1
    if budget[key] > maximum:
        raise CatalogPreparationRequired(
            "Shader catalog exceeds the bounded %s budget" % label, reason_id
        )


def _fast_reflection_text(value: Any, budget: dict[str, int]) -> str:
    text = str(value or "")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError as exc:
        raise CatalogPreparationRequired(
            "Shader catalog contains invalid Unicode text",
            "shader-resource-index-reflection-string-limit",
        ) from exc
    if size > _MAX_FAST_REFLECTION_STRING_BYTES:
        raise CatalogPreparationRequired(
            "Shader catalog contains an oversized reflection string",
            "shader-resource-index-reflection-string-limit",
        )
    budget["stringBytes"] = int(budget.get("stringBytes", 0)) + size
    if budget["stringBytes"] > _MAX_FAST_REFLECTION_TOTAL_STRING_BYTES:
        raise CatalogPreparationRequired(
            "Shader catalog reflection text exceeds the aggregate budget",
            "shader-resource-index-reflection-string-limit",
        )
    return text


def _validate_fast_reflection_fields(
    values: Any, budget: dict[str, int], depth: int = 0
) -> None:
    if depth > _MAX_FAST_REFLECTION_DEPTH:
        raise CatalogPreparationRequired(
            "Shader catalog reflection nesting exceeds the bounded depth",
            "shader-resource-index-reflection-depth-limit",
        )
    if not isinstance(values, list):
        return
    for value in values:
        if not isinstance(value, Mapping):
            continue
        _fast_reflection_charge(
            budget,
            "fields",
            _MAX_FAST_REFLECTION_FIELDS,
            "shader-resource-index-reflection-field-limit",
            "field-count",
        )
        for text in (
            value.get("name", ""),
            value.get("path", ""),
            value.get("type", ""),
            value.get("baseType", ""),
        ):
            _fast_reflection_text(text, budget)
        elements = value.get("elements", 1)
        if (
            isinstance(elements, bool)
            or not isinstance(elements, int)
            or elements < 0
            or elements > (1 << 20)
        ):
            raise CatalogPreparationRequired(
                "Shader catalog contains an invalid array element count",
                "shader-resource-index-reflection-field-limit",
            )
        _validate_fast_reflection_fields(
            value.get("members", value.get("fields", [])), budget, depth + 1
        )


def _validate_fast_catalog_shader_budget(shader: Mapping[str, Any]) -> None:
    """Reject pathological reflection before scanner hashing can recurse/expand."""

    budget = _new_fast_reflection_budget()
    for value in (
        shader.get("stage", ""),
        shader.get("entryPoint", shader.get("entry", "")),
        shader.get("encoding", shader.get("binaryFormat", "")),
        shader.get("outputTopology", ""),
    ):
        _fast_reflection_text(value, budget)

    for key in ("inputSignature", "outputSignature"):
        values = shader.get(key, [])
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, Mapping):
                continue
            _fast_reflection_charge(
                budget,
                "signatures",
                _MAX_FAST_REFLECTION_SIGNATURES,
                "shader-resource-index-reflection-signature-limit",
                "signature-count",
            )
            for text in (
                value.get("varName", ""),
                value.get("semanticName", value.get("semantic", "")),
                value.get("semanticIdxName", ""),
                value.get("systemValue", ""),
                value.get("compType", value.get("componentType", "")),
            ):
                _fast_reflection_text(text, budget)

    for key in ("readOnlyResources", "readWriteResources"):
        values = shader.get(key, [])
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, Mapping):
                continue
            _fast_reflection_charge(
                budget,
                "bindings",
                _MAX_FAST_REFLECTION_BINDINGS,
                "shader-resource-index-reflection-binding-limit",
                "resource-binding-count",
            )
            for text in (
                value.get("name", ""),
                value.get("descriptorType", value.get("resourceType", "")),
                value.get("textureType", ""),
            ):
                _fast_reflection_text(text, budget)
            variable_type = value.get("variableType")
            if isinstance(variable_type, Mapping):
                _fast_reflection_text(variable_type.get("name", ""), budget)
                _fast_reflection_text(variable_type.get("baseType", ""), budget)
                _validate_fast_reflection_fields(
                    variable_type.get("members", variable_type.get("fields", [])),
                    budget,
                )

    samplers = shader.get("samplers", [])
    if isinstance(samplers, list):
        for value in samplers:
            if not isinstance(value, Mapping):
                continue
            _fast_reflection_charge(
                budget,
                "samplers",
                _MAX_FAST_REFLECTION_SAMPLERS,
                "shader-resource-index-reflection-sampler-limit",
                "sampler-count",
            )
            _fast_reflection_text(value.get("name", ""), budget)

    blocks = shader.get("constantBlocks", shader.get("constantBuffers", []))
    if isinstance(blocks, list):
        if len(blocks) > _MAX_FAST_PROJECT_BLOCKS:
            raise CatalogPreparationRequired(
                "Shader catalog exceeds the bounded CBuffer budget",
                "shader-resource-index-block-limit",
            )
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            _fast_reflection_text(block.get("name", ""), budget)
            _validate_fast_reflection_fields(
                block.get("fields", block.get("variables", [])), budget
            )


def _physical_field(
    value: Mapping[str, Any],
    budget: dict[str, int] | None = None,
    depth: int = 0,
) -> dict[str, Any]:
    if budget is None:
        budget = _new_fast_reflection_budget()
    if depth > _MAX_FAST_REFLECTION_DEPTH:
        raise CatalogPreparationRequired(
            "Shader catalog reflection nesting exceeds the bounded depth",
            "shader-resource-index-reflection-depth-limit",
        )
    _fast_reflection_charge(
        budget,
        "fields",
        _MAX_FAST_REFLECTION_FIELDS,
        "shader-resource-index-reflection-field-limit",
        "field-count",
    )
    return {
        "byteOffset": int(value.get("byteOffset", value.get("offset", 0)) or 0),
        "byteSize": int(value.get("byteSize", 0) or 0),
        "baseType": _physical_base_type(value.get("baseType", value.get("type", ""))),
        "rows": int(value.get("rows", 0) or 0),
        "columns": int(value.get("columns", value.get("cols", 0)) or 0),
        "elements": int(value.get("elements", 0) or 0),
        "arrayByteStride": int(
            value.get("arrayByteStride", value.get("arrayStride", 0)) or 0
        ),
        "matrixByteStride": int(
            value.get("matrixByteStride", value.get("matrixStride", 0)) or 0
        ),
        "rowMajor": bool(value.get("rowMajor", False)),
        "flags": int(value.get("flags", 0) or 0),
        "pointerTypeID": int(value.get("pointerTypeID", 0xFFFFFFFF)),
        "bitFieldOffset": int(value.get("bitFieldOffset", 0) or 0),
        "bitFieldSize": int(value.get("bitFieldSize", 0) or 0),
        "members": sorted(
            [
                _physical_field(item, budget, depth + 1)
                for item in list(value.get("members", value.get("fields", [])) or [])
                if isinstance(item, Mapping)
            ],
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        ),
    }


def _catalog_block_projection(
    value: Mapping[str, Any],
    position: int,
    stage: str,
    budget: dict[str, int] | None = None,
) -> dict[str, Any]:
    if budget is None:
        budget = _new_fast_reflection_budget()
    index = _integer(value.get("index", position), "constant block index")
    name = str(value.get("name", "") or "")
    path = name or "cbuffer%d" % index
    space = _integer(
        value.get("space", value.get("registerSpace", 0)), "constant block space"
    )
    bind = _integer(
        value.get("bind", value.get("bindNumber", index)), "constant block bind"
    )
    byte_size = _integer(
        value.get("byteSize", value.get("size", 0)),
        "constant block byteSize",
        1,
    )
    fields = list(value.get("fields", value.get("variables", [])) or [])
    if not all(isinstance(item, Mapping) for item in fields):
        raise RuntimeError("constant block fields are invalid")
    completeness = str(value.get("layoutCompleteness", "") or "").strip().lower()
    if completeness not in {"complete", "partial", "opaque", "ambiguous"}:
        completeness = "complete" if fields else "opaque"
    canonical_block = {
        "index": index,
        "name": name,
        "bind": bind,
        "space": space,
        "bindArraySize": _integer(
            value.get("bindArraySize", value.get("arraySize", 1)),
            "constant block bindArraySize",
            1,
        ),
        "byteSize": byte_size,
        "bufferBacked": bool(value.get("bufferBacked", True)),
        "inlineDataBytes": bool(value.get("inlineDataBytes", False)),
        "compileConstants": bool(value.get("compileConstants", False)),
        "fields": copy.deepcopy(fields),
        "layoutCompleteness": completeness,
    }
    physical_block = {
        "space": space,
        "bind": bind,
        "bindArraySize": canonical_block["bindArraySize"],
        "byteSize": byte_size,
        "bufferBacked": canonical_block["bufferBacked"],
        "inlineDataBytes": canonical_block["inlineDataBytes"],
        "compileConstants": canonical_block["compileConstants"],
        "fields": sorted(
            [_physical_field(item, budget) for item in fields],
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        ),
        "layoutCompleteness": completeness,
    }
    return {
        "space": space,
        "bind": bind,
        "index": index,
        "path": path,
        "byteSize": byte_size,
        "layoutSha256": _canonical_sha256(
            {
                "schema": "renderdoc-ultra-event-cbuffer-layout/v1",
                "stage": stage,
                "block": canonical_block,
            }
        ),
        "abiSha256": _canonical_sha256(physical_block),
    }


def _catalog_inventory_leaf_base_type(value: Any) -> str:
    token = _physical_enum(value)
    return {
        "float": "Float",
        "float32": "Float",
        "single": "Float",
        "sint": "SInt",
        "int": "SInt",
        "int32": "SInt",
        "signedint": "SInt",
        "uint": "UInt",
        "uint32": "UInt",
        "unsignedint": "UInt",
        "double": "Double",
        "float64": "Double",
        "half": "Half",
        "float16": "Half",
        "min16float": "Half",
        "bool": "Bool",
        "boolean": "Bool",
        "slong": "SLong",
        "long": "SLong",
        "int64": "SLong",
        "sint64": "SLong",
        "ulong": "ULong",
        "uint64": "ULong",
        "byte": "Byte",
        "ubyte": "UByte",
        "short": "Short",
        "ushort": "UShort",
    }.get(token, token or "unknown")


def _catalog_inventory_fields(
    values: Any,
    budget: dict[str, int],
    *,
    prefix: str = "",
    parent_offset: int = 0,
    depth: int = 0,
) -> list[dict[str, Any]]:
    """Project catalog CBuffer leaves exactly as the live Bridge inventory."""

    if depth > _MAX_FAST_REFLECTION_DEPTH:
        raise CatalogPreparationRequired(
            "Shader catalog reflection nesting exceeds the bounded depth",
            "shader-resource-index-reflection-depth-limit",
        )
    if not isinstance(values, list) or not all(
        isinstance(value, Mapping) for value in values
    ):
        raise CatalogPreparationRequired(
            "Shader catalog CBuffer fields are malformed",
            "shader-resource-index-interface-mismatch",
        )
    leaves: list[dict[str, Any]] = []
    for position, value in enumerate(values):
        _fast_reflection_charge(
            budget,
            "fields",
            _MAX_FAST_REFLECTION_FIELDS,
            "shader-resource-index-reflection-field-limit",
            "field-count",
        )
        raw_name = str(value.get("name", "") or "")
        name = raw_name or "field%d" % position
        _fast_reflection_text(name, budget)
        canonical_path = name if not prefix else "%s.%s" % (prefix, name)
        _fast_reflection_text(canonical_path, budget)
        local_offset = _integer(
            value.get("byteOffset", value.get("offset", 0)),
            "CBuffer field byteOffset",
        )
        absolute_offset = parent_offset + local_offset
        members = value.get("members", value.get("fields", []))
        if not isinstance(members, list) or not all(
            isinstance(member, Mapping) for member in members
        ):
            raise CatalogPreparationRequired(
                "Shader catalog CBuffer members are malformed",
                "shader-resource-index-interface-mismatch",
            )
        if members:
            leaves.extend(
                _catalog_inventory_fields(
                    members,
                    budget,
                    prefix=canonical_path,
                    parent_offset=absolute_offset,
                    depth=depth + 1,
                )
            )
            continue
        row_major = value.get("rowMajor", False)
        if not isinstance(row_major, bool):
            raise CatalogPreparationRequired(
                "Shader catalog CBuffer row-major evidence is malformed",
                "shader-resource-index-interface-mismatch",
            )
        leaves.append(
            {
                "canonicalPath": canonical_path,
                "name": name,
                "absoluteByteOffset": absolute_offset,
                "byteSize": _integer(
                    value.get("byteSize", 0), "CBuffer field byteSize", 1
                ),
                "baseType": _catalog_inventory_leaf_base_type(
                    value.get("baseType", value.get("type", ""))
                ),
                "rows": _integer(value.get("rows", 0), "CBuffer field rows"),
                "columns": _integer(
                    value.get("columns", value.get("cols", 0)),
                    "CBuffer field columns",
                ),
                "elements": _integer(
                    value.get("elements", 0), "CBuffer field elements"
                ),
                "arrayByteStride": _integer(
                    value.get("arrayByteStride", value.get("arrayStride", 0)),
                    "CBuffer field arrayByteStride",
                ),
                "matrixByteStride": _integer(
                    value.get("matrixByteStride", value.get("matrixStride", 0)),
                    "CBuffer field matrixByteStride",
                ),
                "rowMajor": row_major,
                "flags": _integer(value.get("flags", 0), "CBuffer field flags"),
                "pointerTypeID": _integer(
                    value.get("pointerTypeID", 0xFFFFFFFF),
                    "CBuffer field pointerTypeID",
                ),
                "bitFieldOffset": _integer(
                    value.get("bitFieldOffset", 0), "CBuffer field bitFieldOffset"
                ),
                "bitFieldSize": _integer(
                    value.get("bitFieldSize", 0), "CBuffer field bitFieldSize"
                ),
            }
        )
    leaves.sort(
        key=lambda item: (
            item["absoluteByteOffset"],
            item["canonicalPath"],
            item["name"],
            item["byteSize"],
            item["baseType"],
            item["rows"],
            item["columns"],
            item["elements"],
            item["arrayByteStride"],
            item["matrixByteStride"],
            item["rowMajor"],
            item["flags"],
            item["pointerTypeID"],
            item["bitFieldOffset"],
            item["bitFieldSize"],
        )
    )
    return leaves


def _strict_fast_catalog(
    catalog_path: str | Path, expected_sha256: str
) -> dict[str, Any]:
    path = Path(catalog_path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("active capture catalog is unavailable")
    before = path.stat()
    if before.st_size <= 0:
        raise RuntimeError("active capture catalog is empty")
    if before.st_size > min(MAX_CAPTURE_CATALOG_BYTES, _MAX_FAST_CATALOG_BYTES):
        raise CatalogPreparationRequired(
            "active capture catalog exceeds the bounded resource-index budget",
            "shader-resource-index-catalog-limit",
        )
    raw = path.read_bytes()
    after = path.stat()
    if (
        len(raw) != before.st_size
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise RuntimeError("active capture catalog changed while it was read")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise RuntimeError("active capture catalog hash changed")
    try:
        value = json.loads(
            raw.decode("utf-8-sig", errors="strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("active capture catalog is not strict UTF-8 JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != "renderdoc-ultra-capture-symbols/v1"
        or value.get("schemaVersion") != 1
        or not isinstance(value.get("shaders"), list)
    ):
        raise RuntimeError("active capture catalog schema is unsupported")
    if len(value["shaders"]) > _MAX_FAST_CATALOG_SHADERS:
        raise CatalogPreparationRequired(
            "active capture catalog exceeds the bounded Shader-count budget",
            "shader-resource-index-catalog-shader-limit",
        )
    return value


def _fast_catalog_shader_index(
    catalog: Mapping[str, Any],
) -> dict[tuple[Any, str, str, str], list[Mapping[str, Any]]]:
    result: dict[tuple[Any, str, str, str], list[Mapping[str, Any]]] = {}
    for item in list(catalog.get("shaders", []) or []):
        if not isinstance(item, Mapping):
            continue
        key = (
            item.get("shaderResourceId", item.get("resourceId")),
            str(item.get("stage", "") or ""),
            str(item.get("entryPoint", item.get("entry", "")) or ""),
            str(item.get("rawSha256", "") or "").lower(),
        )
        result.setdefault(key, []).append(item)
    return result


def _expected_resource_shader(
    catalog: Mapping[str, Any],
    identity: Mapping[str, Any],
    api: str,
    anchor_event_id: int,
    shader_index: Mapping[
        tuple[Any, str, str, str], Sequence[Mapping[str, Any]]
    ] | None = None,
    *,
    inventory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    index = shader_index if shader_index is not None else _fast_catalog_shader_index(catalog)
    key = (
        identity.get("resourceId"),
        str(identity.get("stage", "") or ""),
        str(identity.get("entryPoint", "") or ""),
        str(identity.get("rawSha256", "") or "").lower(),
    )
    matches = list(index.get(key, []))
    if len(matches) != 1:
        raise CatalogPreparationRequired(
            "active catalog has no unique content-addressed Shader record",
            "shader-resource-index-ambiguous" if len(matches) > 1 else
            "shader-resource-index-mismatch",
        )
    shader = dict(matches[0])
    _validate_fast_catalog_shader_budget(shader)
    computed_interface = interface_hash(shader)
    computed_layout = layout_hash(shader)
    computed_complete = interface_is_complete(shader)
    if (
        str(shader.get("interfaceHash", "") or "") not in ("", computed_interface)
        or str(shader.get("layoutHash", "") or "") not in ("", computed_layout)
    ):
        raise CatalogPreparationRequired(
            "Shader interface/layout digest differs from the indexed catalog",
            "shader-resource-index-interface-mismatch",
        )
    encoding = _asset_encoding(
        shader.get("encoding", shader.get("binaryFormat", shader.get("format", "")))
    )
    if not encoding:
        raise CatalogPreparationRequired(
            "Shader format is absent from the indexed catalog",
            "shader-resource-index-format-unsupported",
        )
    if not _asset_format_supported(api, encoding):
        raise CatalogPreparationRequired(
            "Shader format is unsupported for the active capture API",
            "shader-resource-index-format-unsupported",
        )
    blocks_value = shader.get("constantBlocks", shader.get("constantBuffers", []))
    if not isinstance(blocks_value, list):
        raise CatalogPreparationRequired(
            "Shader constant-block set is unavailable",
            "shader-resource-index-interface-mismatch",
        )
    if len(blocks_value) > _MAX_FAST_PROJECT_BLOCKS:
        raise CatalogPreparationRequired(
            "Shader constant-block set exceeds the fast-index limit",
            "shader-resource-index-block-limit",
        )
    projection_budget = _new_fast_reflection_budget()
    blocks = [
        _catalog_block_projection(
            block, index, str(identity["stage"]), projection_budget
        )
        for index, block in enumerate(blocks_value)
        if isinstance(block, Mapping)
    ]
    if len(blocks) != len(blocks_value):
        raise CatalogPreparationRequired(
            "Shader constant-block set is malformed",
            "shader-resource-index-interface-mismatch",
        )
    blocks.sort(
        key=lambda item: (
            item["space"], item["bind"], item["index"], item["path"]
        )
    )
    block_keys = [
        (item["space"], item["bind"], item["index"], item["path"])
        for item in blocks
    ]
    if len(set(block_keys)) != len(block_keys):
        raise CatalogPreparationRequired(
            "Shader constant-block ownership is ambiguous",
            "shader-resource-index-ambiguous",
        )
    expected = {
        "resourceId": _integer(identity.get("resourceId"), "Shader resourceId", 1),
        "stage": str(identity["stage"]),
        "entryPoint": str(identity.get("entryPoint", "") or ""),
        "encoding": encoding,
        "rawSha256": _hex(identity.get("rawSha256"), 64, "Shader rawSha256"),
        "interfaceHash": computed_interface,
        "layoutHash": computed_layout,
        "interfaceComplete": computed_complete,
        "constantBlocks": blocks,
        "anchorEventId": _integer(anchor_event_id, "Shader anchorEventId", 1),
    }
    has_v2_identity = any(
        field in identity
        for field in (
            "api",
            "encoding",
            "abiHash",
            "stableKeyComplete",
            "stableKeySha256",
            "anchorEventId",
        )
    )
    if not has_v2_identity:
        # Legacy v1 identities retain their original reflection fields and may
        # authorise knowledge-only CBuffer rebinding after an exact catalog
        # check. They are never promoted to executable stable-key authority.
        if (
            identity.get("interfaceHash", "") != computed_interface
            or identity.get("layoutHash", "") != computed_layout
            or (identity.get("interfaceComplete") is True) != computed_complete
        ):
            raise CatalogPreparationRequired(
                "legacy Shader interface/layout digest differs from the indexed catalog",
                "shader-resource-index-interface-mismatch",
            )
        return expected

    normal_identity = _normal_shader_identity(identity)
    if normal_identity.get("stableKeyComplete") is not True:
        raise CatalogPreparationRequired(
            "v2 Shader identity has no complete stable key for live CBuffer rebinding",
            "shader-resource-index-key-incomplete",
        )
    if (
        normal_identity.get("api") != api
        or normal_identity.get("encoding") != encoding
        or normal_identity.get("layoutHash") != computed_layout
        or normal_identity.get("anchorEventId") != anchor_event_id
    ):
        raise CatalogPreparationRequired(
            "v2 Shader identity differs from the exact indexed catalog",
            "shader-resource-index-interface-mismatch",
        )
    expected.update(
        {
            "api": normal_identity["api"],
            "abiHash": normal_identity["abiHash"],
            "stableKeyComplete": True,
            "stableKeySha256": normal_identity["stableKeySha256"],
        }
    )
    if inventory is None:
        # This is request material only. It cannot authorise an apply until
        # `_resource_index_job` has freshly reproduced every field from the
        # live target RDC and committed its capture-session lease.
        return expected
    inventory_assets = (
        inventory.get("shaders") if isinstance(inventory, Mapping) else None
    )
    if not isinstance(inventory_assets, list):
        raise CatalogPreparationRequired(
            "v2 Shader identity has no validated live inventory evidence",
            "shader-resource-index-invalid-response",
        )
    identity_fields = (
        "api",
        "encoding",
        "resourceId",
        "stage",
        "entryPoint",
        "rawSha256",
        "abiHash",
        "layoutHash",
        "stableKeySha256",
        "anchorEventId",
    )
    live_matches: list[Mapping[str, Any]] = []
    for asset in inventory_assets:
        if not isinstance(asset, Mapping):
            continue
        try:
            live_identity = _normal_shader_identity(asset)
        except Exception:
            continue
        if (
            live_identity.get("stableKeyComplete") is True
            and all(
                live_identity.get(field) == normal_identity.get(field)
                for field in identity_fields
            )
        ):
            live_matches.append(asset)
    if len(live_matches) != 1:
        raise CatalogPreparationRequired(
            "v2 Shader identity has no unique exact live inventory match",
            (
                "shader-resource-index-ambiguous"
                if len(live_matches) > 1
                else "shader-resource-index-mismatch"
            ),
        )
    live = live_matches[0]
    inventory_blocks: list[dict[str, Any]] = []
    block_projection_budget = _new_fast_reflection_budget()
    field_projection_budget = _new_fast_reflection_budget()
    for position, raw_block in enumerate(blocks_value):
        core = _catalog_block_projection(
            raw_block,
            position,
            str(identity["stage"]),
            block_projection_budget,
        )
        fields = _catalog_inventory_fields(
            raw_block.get("fields", raw_block.get("variables", [])),
            field_projection_budget,
        )
        if len({field["canonicalPath"] for field in fields}) != len(fields):
            raise CatalogPreparationRequired(
                "Shader catalog contains duplicate CBuffer field paths",
                "shader-resource-index-ambiguous",
            )
        if any(
            field["absoluteByteOffset"] + field["byteSize"] > core["byteSize"]
            for field in fields
        ):
            raise CatalogPreparationRequired(
                "Shader catalog CBuffer field exceeds its block",
                "shader-resource-index-interface-mismatch",
            )
        name = str(raw_block.get("name", "") or "").strip()
        inventory_blocks.append(
            {
                **core,
                "name": name or core["path"],
                "fields": fields,
            }
        )
    inventory_blocks.sort(
        key=lambda item: (
            item["space"], item["bind"], item["index"], item["path"]
        )
    )
    if (
        normal_identity.get("api") != api
        or normal_identity.get("encoding") != encoding
        or live.get("exactExecutableMatchEligible") is not True
        or live.get("formatSupported") is not True
        or live.get("identityAmbiguous") is not False
        or live.get("interfaceHash") != computed_interface
        or live.get("interfaceComplete") is not computed_complete
        or live.get("constantBlocks") != inventory_blocks
    ):
        raise CatalogPreparationRequired(
            "v2 Shader ABI/reflection evidence differs from the live inventory",
            "shader-resource-index-interface-mismatch",
        )
    expected["constantBlocks"] = inventory_blocks
    return expected


def _target_shader_inventory_coverage(
    result: Mapping[str, Any], targets: list[str], assets: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate filtered coverage without promoting it to a full inventory."""
    if not targets:
        if result.get("inventoryScope", "all-shaders") != "all-shaders":
            raise CatalogPreparationRequired("unexpected filtered Shader inventory", "shader-resource-index-invalid-response")
        return {}

    def resource_ids(field: str) -> list[int]:
        values = result.get(field)
        if not isinstance(values, list) or len(values) > _MAX_FAST_CATALOG_SHADERS:
            raise RuntimeError("target Shader inventory has invalid " + field)
        clean = [_integer(value, field, 1) for value in values]
        if clean != sorted(set(clean)):
            raise RuntimeError("target Shader inventory repeats or reorders " + field)
        return clean

    def hashes(field: str) -> list[str]:
        values = result.get(field)
        if not isinstance(values, list) or len(values) > len(targets):
            raise RuntimeError("target Shader inventory has invalid " + field)
        clean = [_hex(value, 64, field) for value in values]
        if values != clean or clean != sorted(set(clean)) or not set(clean).issubset(targets):
            raise RuntimeError("target Shader inventory changed " + field)
        return clean

    try:
        filtered = resource_ids("filteredOutShaderResourceIds")
        unavailable = resource_ids("rawIdentityUnavailableShaderResourceIds")
        matched = resource_ids("targetMatchedShaderResourceIds")
        unindexed = resource_ids("unindexedShaderResourceIds")
        observed = hashes("observedTargetRawSha256s")
        unobserved = hashes("unobservedTargetRawSha256s")
        indexed = {asset["resourceId"] for asset in assets}
        known = indexed | set(unindexed)
        if (
            result.get("inventoryScope") != "target-raw-sha256"
            or result.get("rawHashPurpose") != "captured-executable-identity"
            or result.get("semanticEquivalenceVerified") is not False
            or result.get("targetRawSha256s") != targets
            or result.get("scanComplete") is not True
            or result.get("inventoryComplete") is not False
            or result.get("targetInventoryComplete") is not (not unavailable and not unindexed)
            or result.get("rawIdentitySearchComplete") is not (not unavailable)
            or result.get("filteredOutShaderResourceCount") != len(filtered)
            or isinstance(result.get("filteredOutShaderResourceCount"), bool)
            or set(filtered) & known
            or not set(unavailable).issubset(known)
            or not set(matched).issubset(known)
            or not indexed.issubset(matched)
            or not set(unindexed).issubset(set(unavailable) | set(matched))
            or unobserved != sorted(set(targets) - set(observed))
            or bool(matched) != bool(observed)
            or any(asset["rawSha256"] not in observed for asset in assets)
        ):
            raise RuntimeError("target Shader inventory scope/counts/identities are inconsistent")
    except (KeyError, RuntimeError) as exc:
        raise CatalogPreparationRequired(str(exc), "shader-resource-index-invalid-response") from exc
    return {key: copy.deepcopy(result[key]) for key in (
        "inventoryScope", "rawHashPurpose", "semanticEquivalenceVerified",
        "targetRawSha256s", "scanComplete", "targetInventoryComplete",
        "rawIdentitySearchComplete", "rawIdentityUnavailableShaderResourceIds",
        "filteredOutShaderResourceCount", "filteredOutShaderResourceIds",
        "targetMatchedShaderResourceIds", "observedTargetRawSha256s", "unobservedTargetRawSha256s",
    )}


def _inventory_shader_resources(
    host: Any,
    capture_path: Path,
    capture_session_id: str,
    api: str,
    progress: _Progress,
    control_checkpoint: Callable[[str], None] | None = None,
    *,
    target_raw_sha256s: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Inventory unique Shaders, optionally analyzing only exact raw matches.

    Target mode still reads each physical module's actual bytes, but only a
    matching module receives DXIL/ABI/CBuffer/GetUsage work. Filtered results
    cannot publish a full symbol snapshot or become a complete inventory cache.
    """

    targets: list[str] = []
    if target_raw_sha256s is not None:
        if (not isinstance(target_raw_sha256s, (list, tuple))
                or not 1 <= len(target_raw_sha256s) <= _MAX_TARGET_RAW_SHADER_HASHES):
            raise ValueError("target_raw_sha256s must contain 1..16 exact lowercase SHA-256 strings")
        for value in target_raw_sha256s:
            digest = _hex(value, 64, "target_raw_sha256s item")
            if not isinstance(value, str) or value != digest:
                raise ValueError("target_raw_sha256s must contain exact lowercase SHA-256 strings")
            targets.append(digest)
        targets = sorted(set(targets))

    canonical_api = _asset_api(api)
    if canonical_api not in {"d3d11", "d3d12", "opengl", "vulkan"}:
        raise CatalogPreparationRequired(
            "the active capture API does not support unique Shader indexing",
            "shader-resource-index-api-unsupported",
        )
    request_capture_id = _canonical_sha256(
        {
            "schema": "replaylab-project-shader-resource-inventory-request/v1",
            "capturePath": _path_key(capture_path),
            "captureSessionId": capture_session_id,
            "api": canonical_api,
            **({"targetRawSha256s": targets} if targets else {}),
        }
    )
    evidence_sha256 = _canonical_sha256(
        {
            "mode": "unique-shader-resources",
            "eventTraversal": False,
            "anchorPerShaderMaximum": 1,
            **({"targetRawSha256s": targets, "inventoryScope": "target-raw-sha256"} if targets else {}),
        }
    )
    request = {
        "expected_capture_path": str(capture_path),
        "expected_capture_session_id": capture_session_id,
        "expected_capture_id": request_capture_id,
        "expected_identity_evidence_sha256": evidence_sha256,
        "expected_api": canonical_api,
        "expected_shaders": [],
        "inventory_all_shaders": True,
        "discover_anchor_eids": True,
    }
    if targets:
        request["target_raw_sha256s"] = targets
    if control_checkpoint is not None:
        control_checkpoint("before unique Shader resource inventory")
    try:
        snapshot = host.ipc.call(
            "begin_rdc_project_shader_resource_index", request, timeout=10.0
        )
    except Exception as exc:
        raise CatalogPreparationRequired(
            "could not start unique Shader resource indexing: %s" % exc,
            "shader-resource-index-unavailable",
        ) from exc
    request_sha256 = ""
    job_id = ""
    deadline = time.monotonic() + max(
        _FAST_INDEX_TIMEOUT_SECONDS, 300.0
    )
    last_progress: tuple[int, int, int] | None = None
    while True:
        if control_checkpoint is not None:
            control_checkpoint("during unique Shader resource inventory")
        if (
            not isinstance(snapshot, Mapping)
            or snapshot.get("schema")
            != "replaylab-project-shader-resource-index-job/v1"
            or snapshot.get("schemaVersion") != 1
            or snapshot.get("captureSessionId") != capture_session_id
        ):
            raise CatalogPreparationRequired(
                "RenderDoc returned an invalid unique Shader inventory job",
                "shader-resource-index-invalid-response",
            )
        current_job_id = _hex(
            snapshot.get("jobId"), 32, "Shader inventory jobId"
        )
        current_request_sha256 = _hex(
            snapshot.get("requestSha256"),
            64,
            "Shader inventory requestSha256",
        )
        if job_id and current_job_id != job_id:
            raise CatalogPreparationRequired(
                "RenderDoc changed the Shader inventory job identity",
                "shader-resource-index-invalid-response",
            )
        if request_sha256 and current_request_sha256 != request_sha256:
            raise CatalogPreparationRequired(
                "RenderDoc changed the Shader inventory request identity",
                "shader-resource-index-invalid-response",
            )
        job_id = current_job_id
        request_sha256 = current_request_sha256
        observation = snapshot.get("progress")
        if isinstance(observation, Mapping):
            current = observation.get("processedResourceCount", 0)
            total = observation.get("totalResourceCount", 0)
            resource_id = observation.get("resourceId")
            if (
                not isinstance(current, bool)
                and isinstance(current, int)
                and not isinstance(total, bool)
                and isinstance(total, int)
                and 0 <= current <= total <= _MAX_FAST_CATALOG_SHADERS
                and (resource_id is None or (
                    not isinstance(resource_id, bool)
                    and isinstance(resource_id, int)
                    and resource_id > 0
                ))
                and observation.get("eventTraversalCount") == 0
            ):
                signature = (current, total, int(resource_id or 0))
                if signature != last_progress:
                    progress.emit(
                        "resource-inventory",
                        "Indexing unique Shader resources",
                        current=current,
                        total=total,
                        item=(
                            "Shader ResourceId %d" % resource_id
                            if resource_id else ""
                        ),
                        detail={
                            "resourceId": resource_id,
                            "eventTraversalCount": 0,
                            "inspectedEntryPointCount": observation.get(
                                "inspectedEntryPointCount", 0
                            ),
                        },
                    )
                    last_progress = signature
        status = str(snapshot.get("status", "") or "")
        if status == "completed":
            break
        if status == "failed":
            raise CatalogPreparationRequired(
                "unique Shader resource indexing failed: %s"
                % str(snapshot.get("error", "") or "unknown failure"),
                "shader-resource-index-failed",
            )
        if status not in {"queued", "running"}:
            raise CatalogPreparationRequired(
                "RenderDoc returned an invalid unique Shader inventory state",
                "shader-resource-index-invalid-response",
            )
        if time.monotonic() >= deadline:
            raise CatalogPreparationRequired(
                "unique Shader resource indexing timed out",
                "shader-resource-index-timeout",
            )
        time.sleep(_FAST_INDEX_POLL_SECONDS)
        try:
            snapshot = host.ipc.call(
                "poll_rdc_project_shader_resource_index",
                {
                    "expected_capture_path": str(capture_path),
                    "expected_capture_session_id": capture_session_id,
                    "job_id": job_id,
                },
                timeout=5.0,
            )
        except Exception as exc:
            # EID/pipeline work can briefly delay the GUI-owned IPC poller
            # while the already-started replay job remains healthy. Continue
            # polling that same idempotent job instead of starting a second
            # all-Shader inventory.
            if (
                _is_transient_bridge_poll_timeout(exc)
                and time.monotonic() < deadline
            ):
                continue
            raise CatalogPreparationRequired(
                "could not poll unique Shader resource indexing: %s" % exc,
                "shader-resource-index-unavailable",
            ) from exc

    result = snapshot.get("result")
    capability = result.get("capability") if isinstance(result, Mapping) else None
    assets = result.get("assets") if isinstance(result, Mapping) else None
    if (
        not isinstance(result, Mapping)
        or result.get("schema")
        != "replaylab-project-shader-resource-index/v2"
        or result.get("schemaVersion") != 2
        or result.get("captureSessionId") != capture_session_id
        or _path_key(result.get("capturePath", "")) != _path_key(capture_path)
        or result.get("captureId") != request_capture_id
        or result.get("identityEvidenceSha256") != evidence_sha256
        or result.get("requestSha256") != request_sha256
        or result.get("inventoryOnly") is not True
        or result.get("leaseEligible") is not False
        or result.get("eventTraversalCount") != 0
        or result.get("fullFrameTraversal") is not False
        or result.get("gpuReplayMutation") is not False
        or result.get("physicalApplyAuthority") is not False
        or result.get("anchorPerShaderMaximum") != 1
        or result.get("resourceOwnershipScope") != "target-capture-session"
        or result.get("crossCaptureResourceIdReuse") is not False
        or not isinstance(capability, Mapping)
        or capability.get("supported") is not True
        or capability.get("api") != canonical_api
        or capability.get("eventTraversalSupported") is not False
        or capability.get("fullFrameTraversalSupported") is not False
        or not isinstance(assets, list)
        or len(assets) > _MAX_FAST_CATALOG_SHADERS
    ):
        raise CatalogPreparationRequired(
            "RenderDoc returned invalid zero-EID Shader inventory evidence",
            "shader-resource-index-invalid-response",
        )

    # A bounded Bridge scan may finish with a structured diagnostic before
    # publishing success-only aggregates. Validate its capture/request and
    # non-mutating ownership above, then retain the actual cause rather than
    # replacing it with a misleading malformed-response error.
    reason = result.get("reasonId")
    if (isinstance(reason, str)
            and re.fullmatch(r"shader-resource-index-[a-z0-9-]{1,96}", reason)
            and result.get("matched") is False
            and result.get("inventoryComplete") is False):
        details = []
        diagnostics = result.get("diagnostics")
        for item in (diagnostics[:8] if isinstance(diagnostics, list) else []):
            if isinstance(item, Mapping):
                details.append({key: item[key] for key in ("code", "resourceId", "stage", "entryPoint")
                                if isinstance(item.get(key), (str, int))})
        raise CatalogPreparationRequired(
            "Shader resource inventory stopped: %s; diagnostics=%s" % (
                reason, json.dumps(details, ensure_ascii=False)[:2048]), reason)
    if (result.get("inventoryComplete") is not (not targets)
            or result.get("symbolSnapshotPublished") is not (not targets)):
        raise CatalogPreparationRequired(
            "RenderDoc returned invalid zero-EID Shader inventory completion evidence",
            "shader-resource-index-invalid-response",
        )

    clean_assets: list[dict[str, Any]] = []
    owner_anchors: dict[int, int | None] = {}
    owners: set[tuple[Any, ...]] = set()
    for position, value in enumerate(assets):
        if not isinstance(value, Mapping):
            raise CatalogPreparationRequired(
                "Shader inventory contains a malformed asset",
                "shader-resource-index-invalid-response",
            )
        identity = _normal_shader_identity(value)
        resource_id = identity["resourceId"]
        owner = (
            resource_id,
            identity["stage"],
            identity["entryPoint"],
            identity["rawSha256"],
        )
        anchor = identity.get("anchorEventId")
        if owner in owners:
            raise CatalogPreparationRequired(
                "Shader inventory contains duplicate resource identities",
                "shader-resource-index-ambiguous",
            )
        if resource_id in owner_anchors and owner_anchors[resource_id] != anchor:
            raise CatalogPreparationRequired(
                "one Shader resource received multiple representative EIDs",
                "shader-resource-index-invalid-response",
            )
        owners.add(owner)
        owner_anchors[resource_id] = anchor
        if (
            value.get("ownerResourceId") != resource_id
            or value.get("ownerCaptureSessionId") != capture_session_id
            or value.get("resourceOwnershipScope")
            != "target-capture-session"
            or value.get("crossCaptureResourceIdReuse") is not False
            or value.get("api") != canonical_api
            or value.get("requiresAnchorPreflight") is not True
            or value.get("exactExecutableMatchEligible")
            is not bool(identity["stableKeyComplete"] and anchor is not None)
            or not isinstance(value.get("constantBlocks"), list)
        ):
            raise CatalogPreparationRequired(
                "Shader inventory changed resource ownership or reuse policy",
                "shader-resource-index-invalid-response",
            )
        blocks: list[dict[str, Any]] = []
        block_keys: set[tuple[Any, ...]] = set()
        for block in value["constantBlocks"]:
            if not isinstance(block, Mapping):
                raise CatalogPreparationRequired(
                    "Shader inventory contains a malformed CBuffer layout",
                    "shader-resource-index-invalid-response",
                )
            raw_fields = block.get("fields")
            if not isinstance(raw_fields, list):
                raise CatalogPreparationRequired(
                    "Shader inventory CBuffer omitted reflected fields",
                    "shader-resource-index-invalid-response",
                )
            fields: list[dict[str, Any]] = []
            field_paths: set[str] = set()
            for raw_field in raw_fields:
                if not isinstance(raw_field, Mapping):
                    raise CatalogPreparationRequired(
                        "Shader inventory contains a malformed CBuffer field",
                        "shader-resource-index-invalid-response",
                    )
                if not isinstance(raw_field.get("rowMajor"), bool):
                    raise CatalogPreparationRequired(
                        "Shader inventory CBuffer row-major evidence is malformed",
                        "shader-resource-index-invalid-response",
                    )
                field = {
                    "canonicalPath": _text(
                        raw_field.get("canonicalPath"),
                        "CBuffer field canonicalPath",
                        2048,
                    ),
                    "name": _text(
                        raw_field.get("name"), "CBuffer field name", 1024
                    ),
                    "absoluteByteOffset": _integer(
                        raw_field.get("absoluteByteOffset"),
                        "CBuffer field absoluteByteOffset",
                    ),
                    "byteSize": _integer(
                        raw_field.get("byteSize"), "CBuffer field byteSize", 1
                    ),
                    "baseType": _text(
                        raw_field.get("baseType"), "CBuffer field baseType", 64
                    ),
                    "rows": _integer(raw_field.get("rows"), "CBuffer field rows"),
                    "columns": _integer(
                        raw_field.get("columns"), "CBuffer field columns"
                    ),
                    "elements": _integer(
                        raw_field.get("elements"), "CBuffer field elements"
                    ),
                    "arrayByteStride": _integer(
                        raw_field.get("arrayByteStride"),
                        "CBuffer field arrayByteStride",
                    ),
                    "matrixByteStride": _integer(
                        raw_field.get("matrixByteStride"),
                        "CBuffer field matrixByteStride",
                    ),
                    "rowMajor": raw_field["rowMajor"],
                    "flags": _integer(
                        raw_field.get("flags"), "CBuffer field flags"
                    ),
                    "pointerTypeID": _integer(
                        raw_field.get("pointerTypeID"),
                        "CBuffer field pointerTypeID",
                    ),
                    "bitFieldOffset": _integer(
                        raw_field.get("bitFieldOffset"),
                        "CBuffer field bitFieldOffset",
                    ),
                    "bitFieldSize": _integer(
                        raw_field.get("bitFieldSize"),
                        "CBuffer field bitFieldSize",
                    ),
                }
                if field["canonicalPath"] in field_paths:
                    raise CatalogPreparationRequired(
                        "Shader inventory contains duplicate CBuffer field paths",
                        "shader-resource-index-ambiguous",
                    )
                field_paths.add(field["canonicalPath"])
                fields.append(field)
            fields.sort(
                key=lambda item: (
                    item["absoluteByteOffset"],
                    item["canonicalPath"],
                    item["name"],
                    item["byteSize"],
                    item["baseType"],
                    item["rows"],
                    item["columns"],
                    item["elements"],
                    item["arrayByteStride"],
                    item["matrixByteStride"],
                    item["rowMajor"],
                    item["flags"],
                    item["pointerTypeID"],
                    item["bitFieldOffset"],
                    item["bitFieldSize"],
                )
            )
            projected = {
                "index": _integer(block.get("index"), "CBuffer index"),
                "path": _text(block.get("path"), "CBuffer path", 2048),
                "name": _text(block.get("name"), "CBuffer name", 1024),
                "space": _integer(block.get("space"), "CBuffer space"),
                "bind": _integer(block.get("bind"), "CBuffer bind"),
                "byteSize": _integer(
                    block.get("byteSize"), "CBuffer byteSize", 1
                ),
                "layoutSha256": _hex(
                    block.get("layoutSha256"), 64, "CBuffer layoutSha256"
                ),
                "abiSha256": _hex(
                    block.get("abiSha256"), 64, "CBuffer abiSha256"
                ),
                "fields": fields,
            }
            if any(
                field["absoluteByteOffset"] + field["byteSize"]
                > projected["byteSize"]
                for field in fields
            ):
                raise CatalogPreparationRequired(
                    "Shader inventory CBuffer field exceeds its block",
                    "shader-resource-index-invalid-response",
                )
            block_key = (
                projected["space"],
                projected["bind"],
                projected["index"],
                projected["path"],
            )
            if block_key in block_keys:
                raise CatalogPreparationRequired(
                    "Shader inventory contains duplicate CBuffer ownership",
                    "shader-resource-index-ambiguous",
                )
            block_keys.add(block_key)
            blocks.append(projected)
        blocks.sort(
            key=lambda item: (
                item["space"], item["bind"], item["index"], item["path"]
            )
        )
        clean_assets.append(
            {
                **identity,
                "interfaceHash": _hex(
                    value.get("interfaceHash"), 64, "Shader interfaceHash"
                ),
                "interfaceComplete": value.get("interfaceComplete") is True,
                "constantBlocks": blocks,
                "rawSize": _integer(value.get("rawSize"), "Shader rawSize"),
                "rawBytecodeIncluded": value.get("rawBytecodeIncluded") is True,
                "formatSupported": value.get("formatSupported") is True,
                "identityAmbiguous": value.get("identityAmbiguous") is True,
                "exactExecutableMatchEligible": (
                    value.get("exactExecutableMatchEligible") is True
                ),
            }
        )

    clean_assets.sort(
        key=lambda item: (
            item["resourceId"],
            item["stage"],
            item["entryPoint"],
            item["rawSha256"],
        )
    )
    try:
        resource_count = _integer(
            result.get("uniqueShaderResourceCount"),
            "unique Shader resource count",
        )
        processed_resource_count = _integer(
            result.get("processedShaderResourceCount"),
            "processed Shader resource count",
        )
    except RuntimeError as exc:
        raise CatalogPreparationRequired(
            "Shader inventory resource counts are malformed",
            "shader-resource-index-invalid-response",
        ) from exc
    if (
        resource_count > _MAX_FAST_CATALOG_SHADERS
        or processed_resource_count > _MAX_FAST_CATALOG_SHADERS
    ):
        raise CatalogPreparationRequired(
            "Shader inventory exceeds the unique-resource limit",
            "shader-resource-index-invalid-response",
        )
    target_coverage = _target_shader_inventory_coverage(result, targets, clean_assets)
    filtered_count = target_coverage.get("filteredOutShaderResourceCount", 0)
    indexed_fields = (
        "indexedShaderResourceCount",
        "unindexedShaderResourceCount",
        "unindexedShaderResourceIds",
    )
    indexed_field_presence = [field in result for field in indexed_fields]
    if any(indexed_field_presence) and not all(indexed_field_presence):
        raise CatalogPreparationRequired(
            "Shader inventory partially reports unindexed resources",
            "shader-resource-index-invalid-response",
        )
    if all(indexed_field_presence):
        try:
            indexed_resource_count = _integer(
                result.get("indexedShaderResourceCount"),
                "indexed Shader resource count",
            )
            unindexed_resource_count = _integer(
                result.get("unindexedShaderResourceCount"),
                "unindexed Shader resource count",
            )
        except RuntimeError as exc:
            raise CatalogPreparationRequired(
                "Shader inventory has malformed indexed-resource counts",
                "shader-resource-index-invalid-response",
            ) from exc
        raw_unindexed_resource_ids = result.get("unindexedShaderResourceIds")
        if (
            not isinstance(raw_unindexed_resource_ids, list)
            or len(raw_unindexed_resource_ids) > _MAX_FAST_CATALOG_SHADERS
        ):
            raise CatalogPreparationRequired(
                "Shader inventory has a malformed unindexed-resource ledger",
                "shader-resource-index-invalid-response",
            )
        try:
            unindexed_resource_ids = [
                _integer(value, "unindexed Shader ResourceId", 1)
                for value in raw_unindexed_resource_ids
            ]
        except RuntimeError as exc:
            raise CatalogPreparationRequired(
                "Shader inventory has a malformed unindexed ResourceId",
                "shader-resource-index-invalid-response",
            ) from exc
        if (
            unindexed_resource_ids != sorted(set(unindexed_resource_ids))
            or any(value in owner_anchors for value in unindexed_resource_ids)
            or indexed_resource_count != len(owner_anchors)
            or unindexed_resource_count != len(unindexed_resource_ids)
            or resource_count
            != indexed_resource_count + unindexed_resource_count + filtered_count
        ):
            raise CatalogPreparationRequired(
                "Shader inventory unindexed-resource ledger is inconsistent",
                "shader-resource-index-invalid-response",
            )
    else:
        # Backward compatibility for an older v2 Bridge is safe only when it
        # indexed every physical Shader resource. A gap needs the explicit,
        # bounded ResourceId ledger above and may never be inferred.
        indexed_resource_count = len(owner_anchors)
        unindexed_resource_count = 0
        unindexed_resource_ids = []
        if resource_count != indexed_resource_count:
            raise CatalogPreparationRequired(
                "Shader inventory omitted resources without an identity ledger",
                "shader-resource-index-invalid-response",
            )
    inventory_material = {
        "api": canonical_api,
        "shaders": [
            {
                "resourceId": item["resourceId"],
                "stableKeySha256": item["stableKeySha256"],
                "anchorEventId": item["anchorEventId"],
                "constantBlocks": item["constantBlocks"],
            }
            for item in clean_assets
        ],
    }
    inventory_sha256 = _canonical_sha256(inventory_material)
    stable_set_sha256 = _canonical_sha256(
        sorted({item["stableKeySha256"] for item in clean_assets})
    )
    live_sha256 = _canonical_sha256(
        {
            "api": canonical_api,
            "shaders": [
                {
                    "resourceId": item["resourceId"],
                    "stableKeySha256": item["stableKeySha256"],
                    "stableKeyComplete": item["stableKeyComplete"],
                    "anchorEventId": item["anchorEventId"],
                }
                for item in clean_assets
            ],
        }
    )
    if (
        result.get("resourceInventorySha256") != inventory_sha256
        or result.get("stableResourceSetSha256") != stable_set_sha256
        or result.get("liveIdentitySha256") != live_sha256
        or processed_resource_count != resource_count
        or result.get("matchedShaderCount") not in (0, len(clean_assets))
        or result.get("representativeEidCount")
        != sum(value is not None for value in owner_anchors.values())
        or result.get("symbolSnapshotShaderCount") != (0 if targets else len(clean_assets))
        or result.get("symbolSnapshotUseCount")
        != (0 if targets else sum(item["anchorEventId"] is not None for item in clean_assets))
        or result.get("symbolSnapshotCaptureId") != ("" if targets else stable_set_sha256)
    ):
        raise CatalogPreparationRequired(
            "Shader inventory aggregate hashes or counts are inconsistent",
            "shader-resource-index-invalid-response",
        )

    raw_blobs: dict[str, bytes] = {}
    encoded_blobs = result.get("rawBytecodeBlobs", [])
    if not isinstance(encoded_blobs, list):
        raise CatalogPreparationRequired(
            "Shader inventory bytecode table is malformed",
            "shader-resource-index-invalid-response",
        )
    for value in encoded_blobs:
        if (
            not isinstance(value, Mapping)
            or value.get("encoding") != "base64"
        ):
            raise CatalogPreparationRequired(
                "Shader inventory bytecode record is malformed",
                "shader-resource-index-invalid-response",
            )
        digest = _hex(value.get("sha256"), 64, "Shader bytecode sha256")
        size = _integer(value.get("size"), "Shader bytecode size", 1)
        try:
            payload = base64.b64decode(
                str(value.get("data", "") or "").encode("ascii"),
                validate=True,
            )
        except (UnicodeEncodeError, ValueError) as exc:
            raise CatalogPreparationRequired(
                "Shader inventory bytecode is not strict base64",
                "shader-resource-index-invalid-response",
            ) from exc
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
            raise CatalogPreparationRequired(
                "Shader inventory bytecode hash or size changed",
                "shader-resource-index-invalid-response",
            )
        if digest in raw_blobs:
            raise CatalogPreparationRequired(
                "Shader inventory repeats one bytecode blob",
                "shader-resource-index-ambiguous",
            )
        raw_blobs[digest] = payload
    for asset in clean_assets:
        if asset["rawBytecodeIncluded"] != (asset["rawSha256"] in raw_blobs):
            raise CatalogPreparationRequired(
                "Shader inventory bytecode inclusion evidence is inconsistent",
                "shader-resource-index-invalid-response",
            )
    progress.emit(
        "resource-inventory",
        "Unique Shader resource inventory completed",
        current=resource_count,
        total=resource_count,
        detail={
            "uniqueShaderResourceCount": resource_count,
            "indexedShaderResourceCount": indexed_resource_count,
            "unindexedShaderResourceCount": unindexed_resource_count,
            "unindexedShaderResourceIds": unindexed_resource_ids,
            "shaderEntryCount": len(clean_assets),
            "representativeEidCount": sum(
                value is not None for value in owner_anchors.values()
            ),
            "eventTraversalCount": 0,
        },
    )
    return {
        "schema": "renderdoc-ultra-project-shader-inventory/v1",
        "schemaVersion": 1,
        "capture": {
            "captureId": stable_set_sha256,
            "catalogSha256": inventory_sha256,
            "inventorySha256": inventory_sha256,
            "capturePath": str(capture_path),
            "captureSessionId": capture_session_id,
            "api": canonical_api,
        },
        "shaders": clean_assets,
        "rawBytecode": raw_blobs,
        "eventTraversalCount": 0,
        "fullFrameTraversal": False,
        "anchorPerShaderMaximum": 1,
        "originalShaderIdentityPolicy": result.get("originalShaderIdentityPolicy"),
        **target_coverage,
        "bridgeResult": dict(result),
    }


def _project_target_inventory_cache(
    host: Any, replay: ReplayProjectService, capture: Path, session: str,
    api: str, targets: Sequence[str], checkpoint: Callable[[str], None] | None,
    *, inventory: Mapping[str, Any] | None = None,
    cache_diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Keep same-session target/EID observations independently of apply coverage.

    This is routing data, not an ABI/apply receipt. Exact RDC bytes, project
    scope, replay session and original target set must match. Synthetic Shader
    ResourceIds can change when the same process reopens identical RDC bytes;
    the old session's IDs must never be relabelled as current observations.
    Candidate bytecode and signed ABI pair proofs have separate reuse gates.
    """
    if (not callable(getattr(host, "_apply_project_shader_pair_restore", None))
            or not isinstance(getattr(replay, "root", None), Path)):
        return None
    try:
        root = replay.root
        path = root / "derived" / "shader-target-inventories" / (
            hashlib.sha256(_path_key(capture).encode("utf-8")).hexdigest() + ".json")
        if not is_within(root, path.resolve()) or path.is_symlink():
            return None
        if inventory is None and not path.is_file():
            return None
        project = replay.status()
        binding = {
            "projectId": project["projectId"],
            "scopeSha256": _canonical_sha256(project["scope"]) if isinstance(project.get("scope"), Mapping) else project.get("scopeSha256"),
            "capturePath": _path_key(capture), "api": _asset_api(api),
            "targetRawSha256s": sorted(targets),
        }
        if inventory is None:
            if path.stat().st_size > _MAX_FAST_CATALOG_BYTES:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            seal = value.pop("sealSha256", None)
            if (seal != _canonical_sha256(value)
                    or value.get("schema") != "replaylab-project-shader-target-inventory/v1"
                    or value.get("schemaVersion") != 1 or value.get("binding") != binding
                    or value.get("captureSha256") != _verified_rdc_content_sha256(capture, checkpoint)):
                return None
            cached = value.get("inventory")
            if (not isinstance(cached, Mapping)
                    or cached.get("targetInventoryComplete") is not True
                    or cached.get("rawIdentitySearchComplete") is not True
                    or sorted(cached.get("targetRawSha256s") or []) != sorted(targets)
                    or cached.get("originalShaderIdentityPolicy") != "captured-original-owner-v1"):
                return None
            cached_capture = cached.get("capture")
            if (not isinstance(cached_capture, Mapping)
                    or cached_capture.get("captureSessionId") != session):
                if cache_diagnostic is not None:
                    cache_diagnostic["reason"] = "target-inventory-capture-session-changed"
                return None
            result = copy.deepcopy(dict(cached))
            result["capture"].update(capturePath=str(capture))
            result.update(rawBytecode={}, bridgeResult=None, cachedTargetInventory=True)
            # Exercise the strict projection before a cached row reaches any
            # source, alias or replacement operation.
            _project_shader_inventory(result, allow_target_lookup=True)
            return result
        if (inventory.get("targetInventoryComplete") is not True
                or inventory.get("rawIdentitySearchComplete") is not True
                or inventory.get("originalShaderIdentityPolicy") != "captured-original-owner-v1"):
            return None
        saved = {key: copy.deepcopy(value) for key, value in inventory.items()
                 if key not in {"rawBytecode", "bridgeResult"}}
        _project_shader_inventory(saved, allow_target_lookup=True)
        value = {
            "schema": "replaylab-project-shader-target-inventory/v1", "schemaVersion": 1,
            "binding": binding, "captureSha256": _verified_rdc_content_sha256(capture, checkpoint),
            "inventory": saved,
        }
        value["sealSha256"] = _canonical_sha256(value)
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_FAST_CATALOG_BYTES:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, encoded)
    except RestoreControlStop:
        raise
    except Exception:
        # Missing/stale optional observations retain the bounded live lookup.
        return None
    return None


def _inventory_project_shader_targets(
    host: Any, replay: ReplayProjectService, capture_path: Path,
    capture_session_id: str, api: str, progress: _Progress,
    control_checkpoint: Callable[[str], None] | None = None,
    *, cache_diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Index only current project heads, in fixed batches of 16 original hashes."""
    targets = replay.restorable_shader_raw_hashes()
    if not targets or len(targets) > 65536:
        raise CatalogPreparationRequired("project has no bounded original Shader lookup targets", "shader-resource-index-targets-unavailable")
    cached = _project_target_inventory_cache(
        host, replay, capture_path, capture_session_id, api, targets, control_checkpoint,
        cache_diagnostic=cache_diagnostic)
    if cached is not None:
        progress.emit("resource-inventory", "Reusing known original Shader targets and EIDs",
                      current=len(cached["shaders"]), total=len(cached["shaders"]))
        return cached
    deadline = time.monotonic() + 300.0
    def checkpoint(phase: str) -> None:
        if control_checkpoint is not None:
            control_checkpoint(phase)
        if time.monotonic() >= deadline:
            raise CatalogPreparationRequired("bounded project Shader target lookup timed out", "shader-resource-index-timeout")
    results = []
    for offset in range(0, len(targets), _MAX_TARGET_RAW_SHADER_HASHES):
        checkpoint("before project Shader target batch")
        result = _inventory_shader_resources(
            host, capture_path, capture_session_id, api, progress, checkpoint,
            target_raw_sha256s=targets[offset:offset + _MAX_TARGET_RAW_SHADER_HASHES],
        )
        if (result.get("targetInventoryComplete") is not True
                or result.get("rawIdentitySearchComplete") is not True):
            raise CatalogPreparationRequired(
                "target Shader lookup is incomplete; unobserved targets are unknown, not absent",
                "shader-resource-index-targets-incomplete",
            )
        results.append(result)
    if len(results) == 1:
        _project_target_inventory_cache(host, replay, capture_path, capture_session_id,
                                        api, targets, control_checkpoint, inventory=results[0])
        return results[0]
    merged = copy.deepcopy(results[0])
    by_target: dict[tuple[int, str], dict[str, Any]] = {}
    blobs: dict[str, bytes] = {}
    observed: set[str] = set()
    for result in results:
        if any(result["capture"].get(key) != results[0]["capture"].get(key)
               for key in ("capturePath", "captureSessionId", "api")):
            raise RuntimeError("project Shader target batches changed capture identity")
        for shader in result["shaders"]:
            key = (shader["resourceId"], shader["stableKeySha256"])
            if key in by_target and by_target[key] != shader:
                raise RuntimeError("project Shader target changed across lookup batches")
            by_target[key] = shader
        for digest, payload in result.get("rawBytecode", {}).items():
            if digest in blobs and blobs[digest] != payload:
                raise RuntimeError("project Shader target bytes changed across batches")
            blobs[digest] = payload
        observed.update(result["observedTargetRawSha256s"])
    shaders = sorted(by_target.values(), key=lambda item: (item["resourceId"], item["stage"], item["entryPoint"], item["rawSha256"]))
    digest = _canonical_sha256({"api": _asset_api(api), "shaders": [
        {key: shader[key] for key in ("resourceId", "stableKeySha256", "anchorEventId", "constantBlocks")}
        for shader in shaders]})
    merged["capture"].update(captureId=_canonical_sha256(sorted({item["stableKeySha256"] for item in shaders})),
                             catalogSha256=digest, inventorySha256=digest)
    filtered = set(results[0]["filteredOutShaderResourceIds"])
    for result in results[1:]:
        filtered.intersection_update(result["filteredOutShaderResourceIds"])
    merged.update(shaders=shaders, rawBytecode=blobs, bridgeResult=None,
                  originalShaderIdentityPolicy=("captured-original-owner-v1" if all(
                      item.get("originalShaderIdentityPolicy") == "captured-original-owner-v1"
                      for item in results) else None),
                  targetRawSha256s=targets, observedTargetRawSha256s=sorted(observed),
                  unobservedTargetRawSha256s=sorted(set(targets) - observed),
                  targetMatchedShaderResourceIds=sorted({item["resourceId"] for item in shaders}),
                  filteredOutShaderResourceIds=sorted(filtered), filteredOutShaderResourceCount=len(filtered),
                  targetBatchCount=len(results))
    _project_target_inventory_cache(host, replay, capture_path, capture_session_id,
                                    api, targets, control_checkpoint, inventory=merged)
    return merged


def _recover_equivalent_shader_sources(
    host: Any, replay: ReplayProjectService, active: Mapping[str, Any],
    project: Path, capture: Path, session: str, api: str,
    inventory: Mapping[str, Any], progress: _Progress,
    checkpoint: Callable[[str], None], diagnostic: dict[str, Any],
) -> Mapping[str, Any]:
    """Resolve missed container hashes through strict target source imports.

    Container debug data may differ between RDCs while executable instructions
    are identical. A raw miss therefore is not evidence that the Shader is
    absent. One unique-resource scan finds code candidates; only their anchor
    EIDs need a catalog. Code alone never authorizes source or binary reuse.
    The ordinary project catalog matcher and source importer check the complete
    interface/layout, then the normal restore recompiles and validates the ABI.
    """
    missing = inventory.get("unobservedTargetRawSha256s", [])
    lookup = getattr(replay, "restorable_shader_code_hashes", None)
    if not callable(lookup):
        return inventory
    codes = set(lookup(missing, api))
    if not codes:
        return inventory
    from .shader_workspace.binary_formats import fingerprint_shader_executable

    diagnostic.update(attempted=True, prepared=0, candidateCount=0, items=[])
    checkpoint("before equivalent Shader source lookup")
    # If all raw targets were observed, the bounded target scan already has
    # every known family member. Only a missing container needs a full unique
    # resource index; neither path traverses every EID.
    full = (_inventory_shader_resources(host, capture, session, api, progress, checkpoint)
            if missing else inventory)
    _require_restore_context(host, active, project, capture, session)
    candidates = []
    for shader in full["shaders"]:
        if (shader.get("encoding") not in {"dxbc", "dxil"}
                or shader.get("anchorEventId") is None
                or shader.get("interfaceComplete") is not True
                or shader.get("identityAmbiguous") is not False):
            continue
        payload = full.get("rawBytecode", {}).get(shader["rawSha256"])
        if not isinstance(payload, bytes):
            continue
        executable = fingerprint_shader_executable(
            payload, "current Shader source lookup", format_hint=shader["encoding"])
        if (executable is not None and executable.raw_sha256 == shader["rawSha256"]
                and executable.code_sha256 in codes):
            candidates.append(shader)
    diagnostic["candidateCount"] = len(candidates)
    for shader in candidates:
        checkpoint("before equivalent Shader target catalog")
        identity = _normal_shader_identity(shader)
        located = _tool_json(host.get_active_shader_workspace_path(
            identity["anchorEventId"], identity["stage"],
            expected_resource_id=identity["resourceId"],
            expected_entry_point=identity["entryPoint"],
            expected_raw_sha256=identity["rawSha256"],
            expected_capture_session_id=session),
            "resolve equivalent Shader source", schema="replaylab-active-shader-workspace-path/v1",
            schema_version=1)
        if (located.get("captureSessionId") != session
                or located.get("shaderResourceId") != identity["resourceId"]
                or located.get("rawSha256") != identity["rawSha256"]
                or located.get("stage") != identity["stage"]
                or located.get("entryPoint", "") != identity["entryPoint"]
                or located.get("eventId") != identity["anchorEventId"]):
            raise RestoreContextChanged("equivalent Shader target changed during catalog lookup")
        plan = replay.reuse_plan(located["captureId"], located["catalogSha256"])
        matches = [candidate for match in plan.get("matches", [])
            if all(match.get("shader", {}).get(key) == identity[key]
                   for key in ("resourceId", "stage", "entryPoint", "rawSha256"))
            for candidate in match.get("workspaceCandidates", [])
            if candidate.get("matchMode") == "code-interface-layout-exact"
            and candidate.get("activeCandidate") is True
            and candidate.get("sourceSeedEligible") is True]
        if len(matches) != 1:
            diagnostic["items"].append({"resourceId": identity["resourceId"],
                "status": "unmatched", "reason": "no-unique-code-interface-layout-match"})
            continue
        candidate = matches[0]
        workspace, status, unused_capture, unused_catalog = _prepare_reuse_workspace(
            host, active, identity, session, located["catalogSha256"],
            expected_anchor_event_id=identity["anchorEventId"],
            expected_source_sha256=candidate["sourceSha256"], inventory_mode=True,
            capture_path=capture, progress=progress)
        source = status.get("source") or {}
        if source.get("currentSha256") != candidate["sourceSha256"]:
            advance = replay.published_workspace_source_advance(candidate["knowledgeId"], status)
            if advance is None and source.get("modifiedSinceDecompile") is not False:
                raise RestoreLocalEdit("equivalent Shader target has an unpublished local source", workspace,
                                       str(source.get("currentSha256") or ""))
            suspension = _suspend_workspace_watchers(host, workspace)
            try:
                checkpoint("before advancing published equivalent Shader source")
                _require_restore_context(host, active, project, capture, session)
                replay.seed_workspace(candidate["knowledgeId"], status,
                    **({"published_source_advance": advance} if advance is not None else {}))
                status = _reuse_workspace_status(host, active, workspace, identity,
                                                 unused_capture, unused_catalog)
            finally:
                if suspension is not None:
                    suspension.__exit__(None, None, None)
            source = status.get("source") or {}
        receipt = source.get("projectSource") or {}
        if (source.get("currentSha256") != candidate["sourceSha256"]
                or receipt.get("knowledgeId") != candidate["knowledgeId"]
                or receipt.get("sourceSha256") != candidate["sourceSha256"]
                or receipt.get("originalRawSha256") != identity["rawSha256"]
                or receipt.get("projectId") != active["replayProject"]["projectId"]
                or receipt.get("requiresCompile") is not True
                or receipt.get("requiresLiveAbiRevalidation") is not True):
            raise RuntimeError("equivalent Shader source import has no exact target provenance")
        checkpoint("before equivalent Shader source registration")
        _require_restore_context(host, active, project, capture, session)
        # Publish the authenticated target-bound source, not an old binary or
        # ABI grant. With no current candidate this remains preparation-only;
        # the existing raw-exact restore below compiles and validates it.
        registered = replay.register_workspace(status, include_analysis=False)
        if (registered.get("knowledge", {}).get("rawSha256") != identity["rawSha256"]
                or registered["knowledge"].get("sourceSha256") != candidate["sourceSha256"]):
            raise RuntimeError("equivalent Shader source registration changed identity")
        diagnostic["prepared"] += 1
        diagnostic["items"].append({"resourceId": identity["resourceId"], "status": "prepared",
            "workspacePath": str(workspace), "matchMode": "code-interface-layout-exact"})
    checkpoint("after equivalent Shader source lookup")
    _require_restore_context(host, active, project, capture, session)
    return full if diagnostic["prepared"] else inventory


def _project_shader_inventory(inventory: Mapping[str, Any], *, allow_target_lookup: bool = False) -> dict[str, Any]:
    """Project the live Bridge inventory to the strict project-library contract.

    Transport-only data (capture paths/sessions, bytecode response blobs and
    Bridge diagnostics) must never become project matching authority.  The
    project service recomputes the aggregate hashes from this bounded view.
    """

    if (inventory.get("inventoryScope", "all-shaders") != "all-shaders"
            and not (allow_target_lookup and inventory.get("inventoryScope") == "target-raw-sha256"
                     and inventory.get("targetInventoryComplete") is True
                     and inventory.get("rawIdentitySearchComplete") is True)):
        raise RuntimeError("filtered Shader lookup cannot publish a complete project inventory")

    capture = inventory.get("capture")
    shaders = inventory.get("shaders")
    if not isinstance(capture, Mapping) or not isinstance(shaders, list):
        raise RuntimeError("live Shader inventory is incomplete")
    projected: list[dict[str, Any]] = []
    seen_targets: set[tuple[int, str]] = set()
    resource_anchors: dict[int, int | None] = {}
    for position, shader in enumerate(shaders):
        if not isinstance(shader, Mapping):
            raise RuntimeError(
                "live Shader inventory item %d is malformed" % position
            )
        identity = _normal_shader_identity(shader)
        resource_id = identity["resourceId"]
        target_key = (resource_id, identity["stableKeySha256"])
        if target_key in seen_targets:
            raise RuntimeError(
                "live Shader inventory repeats one resource/entry identity"
            )
        seen_targets.add(target_key)
        anchor_event_id = identity.get("anchorEventId")
        if (
            resource_id in resource_anchors
            and resource_anchors[resource_id] != anchor_event_id
        ):
            raise RuntimeError(
                "live Shader inventory assigns conflicting representative EIDs to one resource"
            )
        resource_anchors[resource_id] = anchor_event_id
        raw_blocks = shader.get("constantBlocks")
        if not isinstance(raw_blocks, list):
            raise RuntimeError("live Shader inventory omitted CBuffer layouts")
        blocks: list[dict[str, Any]] = []
        for block_index, block in enumerate(raw_blocks):
            if not isinstance(block, Mapping):
                raise RuntimeError(
                    "live Shader CBuffer %d is malformed" % block_index
                )
            raw_fields = block.get("fields")
            if not isinstance(raw_fields, list) or not all(
                isinstance(value, Mapping) for value in raw_fields
            ):
                raise RuntimeError(
                    "live Shader CBuffer %d omitted reflected fields" % block_index
                )
            blocks.append(
                {
                    "index": _integer(block.get("index"), "CBuffer index"),
                    "path": _text(block.get("path"), "CBuffer path", 2048),
                    "name": _text(block.get("name"), "CBuffer name", 1024),
                    "space": _integer(block.get("space"), "CBuffer space"),
                    "bind": _integer(block.get("bind"), "CBuffer bind"),
                    "byteSize": _integer(
                        block.get("byteSize"), "CBuffer byteSize", 1
                    ),
                    "layoutSha256": _hex(
                        block.get("layoutSha256"), 64, "CBuffer layoutSha256"
                    ),
                    "abiSha256": _hex(
                        block.get("abiSha256"), 64, "CBuffer abiSha256"
                    ),
                    "fields": [copy.deepcopy(dict(value)) for value in raw_fields],
                }
            )
        blocks.sort(
            key=lambda item: (
                item["space"], item["bind"], item["index"], item["path"]
            )
        )
        projected.append(
            {
                "resourceId": resource_id,
                "api": identity["api"],
                # Persistent project manifests use the established display
                # token.  ``stableKeySha256`` above was verified from the
                # lowercase protocol token and is intentionally unchanged.
                "encoding": _project_asset_encoding(identity["encoding"]),
                "rawSha256": identity["rawSha256"],
                "stage": identity["stage"],
                "entryPoint": identity["entryPoint"],
                "abiHash": identity["abiHash"],
                "layoutHash": identity["layoutHash"],
                "interfaceHash": _hex(
                    shader.get("interfaceHash"), 64, "Shader interfaceHash"
                ),
                # The project v1 inventory has no separate stable-key
                # completeness flag. A module-only reflection must remain
                # source-only even when its interface section is populated.
                "interfaceComplete": (shader.get("interfaceComplete") is True
                                      and identity.get("stableKeyComplete") is True),
                "stableKeySha256": identity["stableKeySha256"],
                "anchorEventId": identity["anchorEventId"],
                "constantBlocks": blocks,
            }
        )
    projected.sort(
        key=lambda item: (
            item["resourceId"], item["stage"], item["entryPoint"], item["rawSha256"]
        )
    )
    return {
        "schema": "renderdoc-ultra-project-shader-inventory/v1",
        "schemaVersion": 1,
        "capture": {
            "captureId": _hex(
                capture.get("captureId"), 64, "Shader inventory captureId"
            ),
            "catalogSha256": _hex(
                capture.get("catalogSha256"),
                64,
                "Shader inventory catalogSha256",
            ),
            "inventorySha256": _hex(
                capture.get("inventorySha256"),
                64,
                "Shader inventory inventorySha256",
            ),
            "api": _asset_api(capture.get("api")),
        },
        "shaders": projected,
    }


def _shader_inventory_cache_binding(
    replay: ReplayProjectService, active: Mapping[str, Any]
) -> tuple[str, str]:
    """Return the current project/scope binding for a disposable sidecar."""

    status = replay.status()
    replay_project = active.get("replayProject")
    if not isinstance(status, Mapping) or not isinstance(replay_project, Mapping):
        raise RuntimeError("active Replay Project status is unavailable")
    project_id = _hex(
        replay_project.get("projectId"), 32, "active Replay Project projectId"
    )
    if status.get("projectId") != project_id:
        raise RuntimeError("active Replay Project changed before cache access")
    scope = status.get("scope")
    if isinstance(scope, Mapping):
        scope_sha256 = _canonical_sha256(scope)
    else:
        # This compatibility branch is useful to older embedded Hosts which
        # exposed the already-canonical digest but not the scope object.
        scope_sha256 = _hex(
            status.get("scopeSha256"), 64, "Replay Project scopeSha256"
        )
    return project_id, scope_sha256


def _project_has_no_restorable_knowledge(
    replay: ReplayProjectService,
) -> bool:
    """Return true only for a positively identified empty project knowledge set."""

    try:
        status = replay.status()
    except Exception:
        return False
    counts = status.get("knowledgeCounts") if isinstance(status, Mapping) else None
    required = ("workspaces", "materials", "aliases")
    if not isinstance(counts, Mapping) or any(key not in counts for key in required):
        return False
    values: list[int] = []
    for value in counts.values():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False
        values.append(value)
    return bool(values) and all(value == 0 for value in values)


def _runtime_shader_inventory_from_cache_assets(
    assets: Sequence[Mapping[str, Any]],
    capture_path: Path,
    capture_session_id: str,
    api: str,
) -> dict[str, Any]:
    """Adapt sealed routing assets to the existing project-inventory shape.

    The returned ResourceIds and representative EIDs are still provisional.
    Callers must run the bounded target-capture verifier before any alias or
    executable restoration and should then rebuild this value from only the
    verified subset.
    """

    canonical_api = _asset_api(api)
    clean_assets: list[dict[str, Any]] = []
    owners: set[tuple[Any, ...]] = set()
    for position, value in enumerate(assets):
        if not isinstance(value, Mapping):
            raise RuntimeError(
                "cached Shader inventory item %d is malformed" % position
            )
        identity = _normal_shader_identity(value)
        if identity["api"] != canonical_api:
            raise RuntimeError("cached Shader inventory API changed")
        owner = (
            identity["resourceId"],
            identity["stage"],
            identity["entryPoint"],
            identity["rawSha256"],
        )
        if owner in owners:
            raise RuntimeError("cached Shader inventory repeats one owner")
        owners.add(owner)
        blocks = value.get("constantBlocks")
        if not isinstance(blocks, list):
            raise RuntimeError("cached Shader inventory omitted CBuffer layouts")
        anchor = identity.get("anchorEventId")
        format_supported = value.get("formatSupported") is True
        identity_ambiguous = value.get("identityAmbiguous") is True
        clean_assets.append(
            {
                **identity,
                "interfaceHash": str(value.get("interfaceHash", "") or ""),
                "interfaceComplete": value.get("interfaceComplete") is True,
                "constantBlocks": [
                    copy.deepcopy(dict(block)) for block in blocks
                ],
                "rawSize": _integer(
                    value.get("rawSize"), "cached Shader rawSize"
                ),
                "rawBytecodeIncluded": False,
                "formatSupported": format_supported,
                "identityAmbiguous": identity_ambiguous,
                "exactExecutableMatchEligible": bool(
                    identity.get("stableKeyComplete") is True
                    and anchor is not None
                    and format_supported
                    and not identity_ambiguous
                ),
            }
        )
    clean_assets.sort(
        key=lambda item: (
            item["resourceId"],
            item["stage"],
            item["entryPoint"],
            item["rawSha256"],
        )
    )
    inventory_material = {
        "api": canonical_api,
        "shaders": [
            {
                "resourceId": item["resourceId"],
                "stableKeySha256": item["stableKeySha256"],
                "anchorEventId": item["anchorEventId"],
                "constantBlocks": item["constantBlocks"],
            }
            for item in clean_assets
        ],
    }
    inventory_sha256 = _canonical_sha256(inventory_material)
    stable_set_sha256 = _canonical_sha256(
        sorted({item["stableKeySha256"] for item in clean_assets})
    )
    return {
        "schema": "renderdoc-ultra-project-shader-inventory/v1",
        "schemaVersion": 1,
        "capture": {
            "captureId": stable_set_sha256,
            "catalogSha256": inventory_sha256,
            "inventorySha256": inventory_sha256,
            "capturePath": str(capture_path),
            "captureSessionId": capture_session_id,
            "api": canonical_api,
        },
        "shaders": clean_assets,
        "rawBytecode": {},
        "eventTraversalCount": 0,
        "fullFrameTraversal": False,
        "anchorPerShaderMaximum": 1,
        "cacheRoutingOnly": True,
    }


def _runtime_shader_inventory_from_cache(
    document: Mapping[str, Any],
    capture_path: Path,
    capture_session_id: str,
    api: str,
) -> dict[str, Any]:
    if (
        document.get("routingOnly") is not True
        or document.get("freshTargetValidationRequired") is not True
        or document.get("crossCaptureResourceIdReuse") is not False
        or not isinstance(document.get("assets"), list)
    ):
        raise RuntimeError("Shader inventory cache weakened its routing policy")
    return _runtime_shader_inventory_from_cache_assets(
        document["assets"], capture_path, capture_session_id, api
    )


def _cached_inventory_expected_shader(asset: Mapping[str, Any]) -> dict[str, Any]:
    identity = _normal_shader_identity(asset)
    blocks = asset.get("constantBlocks")
    if not isinstance(blocks, list):
        raise RuntimeError("cached Shader route omitted CBuffer layouts")
    return {
        "resourceId": identity["resourceId"],
        "stage": identity["stage"],
        "entryPoint": identity["entryPoint"],
        "api": identity["api"],
        "encoding": identity["encoding"],
        "rawSha256": identity["rawSha256"],
        "abiHash": identity["abiHash"],
        "interfaceHash": _hex(
            asset.get("interfaceHash"), 64, "cached Shader interfaceHash"
        ),
        "layoutHash": identity["layoutHash"],
        "interfaceComplete": asset.get("interfaceComplete") is True,
        "stableKeyComplete": identity.get("stableKeyComplete") is True,
        "stableKeySha256": identity["stableKeySha256"],
        "anchorEventId": _shader_anchor_event_id(identity),
        # The Bridge request deliberately accepts only physical block cores.
        # Names and fields remain sealed sidecar evidence and are admitted only
        # after these hashes have matched fresh target reflection.
        "constantBlocks": [
            {
                "index": _integer(block.get("index"), "cached CBuffer index"),
                "path": _text(block.get("path"), "cached CBuffer path", 2048),
                "space": _integer(block.get("space"), "cached CBuffer space"),
                "bind": _integer(block.get("bind"), "cached CBuffer bind"),
                "byteSize": _integer(
                    block.get("byteSize"), "cached CBuffer byteSize", 1
                ),
                "layoutSha256": _hex(
                    block.get("layoutSha256"),
                    64,
                    "cached CBuffer layoutSha256",
                ),
                "abiSha256": _hex(
                    block.get("abiSha256"), 64, "cached CBuffer abiSha256"
                ),
            }
            for block in blocks
            if isinstance(block, Mapping)
        ],
    }


def _verify_cached_inventory_candidates(
    host: Any,
    capture_path: Path,
    capture_session_id: str,
    prepared: Mapping[str, Any],
    provisional_inventory: Mapping[str, Any],
    cache_document: Mapping[str, Any],
    control_checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Freshly validate every cached route that the current project may use."""

    plan = prepared.get("reusePlan")
    matches = plan.get("matches") if isinstance(plan, Mapping) else None
    assets = provisional_inventory.get("shaders")
    capture = provisional_inventory.get("capture")
    if (
        not isinstance(matches, list)
        or not isinstance(assets, list)
        or not isinstance(capture, Mapping)
    ):
        raise RuntimeError("cached Shader reuse plan is incomplete")
    if len(matches) > _MAX_FAST_PROJECT_SHADERS:
        raise CatalogPreparationRequired(
            "cached Shader candidates exceed the bounded verifier limit",
            "shader-resource-index-shader-limit",
        )
    api = _asset_api(capture.get("api"))
    if api == "vulkan":
        # A Vulkan null-pipeline route does not provide physical ABI authority;
        # rebuild the current inventory before admitting cached reflected data.
        raise CatalogPreparationRequired(
            "Vulkan cached routes require a fresh complete inventory",
            "shader-resource-index-vulkan-cache-refresh",
        )
    by_target: dict[tuple[Any, str], list[Mapping[str, Any]]] = {}
    for asset in assets:
        if not isinstance(asset, Mapping):
            continue
        identity = _normal_shader_identity(asset)
        by_target.setdefault(
            (identity["resourceId"], identity["stableKeySha256"]), []
        ).append(asset)

    selected_assets: list[dict[str, Any]] = []
    expected_shaders: list[dict[str, Any]] = []
    for match in matches:
        shader = match.get("shader") if isinstance(match, Mapping) else None
        if not isinstance(shader, Mapping):
            raise RuntimeError("cached Shader reuse plan contains a malformed match")
        identity = _normal_shader_identity(shader)
        candidates = by_target.get(
            (identity["resourceId"], identity["stableKeySha256"]), []
        )
        if len(candidates) != 1:
            raise CatalogPreparationRequired(
                "cached Shader route is not unique",
                "shader-resource-index-ambiguous"
                if len(candidates) > 1
                else "shader-resource-index-mismatch",
            )
        asset = candidates[0]
        if (
            identity.get("stableKeyComplete") is not True
            or identity.get("anchorEventId") is None
            or asset.get("formatSupported") is not True
            or asset.get("identityAmbiguous") is not False
        ):
            raise CatalogPreparationRequired(
                "cached Shader route has no complete executable identity",
                "shader-resource-index-key-incomplete",
            )
        selected_assets.append(copy.deepcopy(dict(asset)))
        expected_shaders.append(_cached_inventory_expected_shader(asset))

    if expected_shaders:
        expected_sha256 = _canonical_sha256(expected_shaders)
        evidence_sha256 = _canonical_sha256(
            {
                "schema": "replaylab-sidecar-shader-route-validation/v1",
                "cacheSealSha256": cache_document.get("sealSha256"),
                "capturePathSha256": cache_document.get("capturePathSha256"),
                "inventoryAlgorithmVersion": cache_document.get(
                    "inventoryAlgorithmVersion"
                ),
                "matchingPolicyVersion": cache_document.get(
                    "matchingPolicyVersion"
                ),
                "expectedShadersSha256": expected_sha256,
            }
        )
        selected_capture_id = _canonical_sha256(
            sorted(
                {
                    item["stableKeySha256"]
                    for item in expected_shaders
                }
            )
        )
        _resource_index_job(
            host,
            capture_path,
            capture_session_id,
            selected_capture_id,
            evidence_sha256,
            api,
            expected_shaders,
            control_checkpoint,
        )
    return _runtime_shader_inventory_from_cache_assets(
        selected_assets, capture_path, capture_session_id, api
    )


def _resource_index_job(
    host: Any,
    capture_path: Path,
    capture_session_id: str,
    capture_id: str,
    evidence_sha256: str,
    api: str,
    shaders: Sequence[Mapping[str, Any]],
    control_checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    request = {
        "expected_capture_path": str(capture_path),
        "expected_capture_session_id": capture_session_id,
        "expected_capture_id": capture_id,
        "expected_identity_evidence_sha256": evidence_sha256,
        "expected_api": api,
        "expected_shaders": [copy.deepcopy(dict(item)) for item in shaders],
        "inventory_all_shaders": False,
        "discover_anchor_eids": False,
    }
    if control_checkpoint is not None:
        control_checkpoint("before Shader resource-index start")
    try:
        snapshot = host.ipc.call(
            "begin_rdc_project_shader_resource_index", request, timeout=10.0
        )
    except Exception as exc:
        reason_id = (
            "shader-resource-index-capture-changed"
            if "capture changed" in str(exc).lower()
            else "shader-resource-index-unavailable"
        )
        raise CatalogPreparationRequired(
            "could not start the bounded Shader resource index: %s" % exc,
            reason_id,
        ) from exc
    if control_checkpoint is not None:
        control_checkpoint("after Shader resource-index start")
    deadline = time.monotonic() + _FAST_INDEX_TIMEOUT_SECONDS
    request_sha256 = ""
    while True:
        if control_checkpoint is not None:
            control_checkpoint("during Shader resource-index polling")
        if (
            not isinstance(snapshot, Mapping)
            or snapshot.get("schema")
            != "replaylab-project-shader-resource-index-job/v1"
            or snapshot.get("schemaVersion") != 1
            or snapshot.get("captureSessionId") != capture_session_id
        ):
            raise CatalogPreparationRequired(
                "RenderDoc returned an invalid Shader resource-index job",
                "shader-resource-index-invalid-response",
            )
        job_id = _hex(snapshot.get("jobId"), 32, "Shader resource-index jobId")
        current_request_sha256 = _hex(
            snapshot.get("requestSha256"), 64, "Shader resource-index requestSha256"
        )
        if request_sha256 and current_request_sha256 != request_sha256:
            raise CatalogPreparationRequired(
                "RenderDoc changed the Shader resource-index request identity",
                "shader-resource-index-invalid-response",
            )
        request_sha256 = current_request_sha256
        status = str(snapshot.get("status", "") or "")
        if status == "completed":
            break
        if status == "failed":
            error = str(snapshot.get("error", "") or "")
            reason = (
                "shader-resource-index-timeout"
                if "timed out" in error.lower()
                else "shader-resource-index-failed"
            )
            raise CatalogPreparationRequired(
                "bounded Shader resource indexing failed: %s" % (error or status),
                reason,
            )
        if status not in {"queued", "running"}:
            raise CatalogPreparationRequired(
                "RenderDoc returned an invalid Shader resource-index state",
                "shader-resource-index-invalid-response",
            )
        if time.monotonic() >= deadline:
            raise CatalogPreparationRequired(
                "bounded Shader resource indexing timed out",
                "shader-resource-index-timeout",
            )
        time.sleep(_FAST_INDEX_POLL_SECONDS)
        try:
            snapshot = host.ipc.call(
                "poll_rdc_project_shader_resource_index",
                {
                    "expected_capture_path": str(capture_path),
                    "expected_capture_session_id": capture_session_id,
                    "job_id": job_id,
                },
                timeout=5.0,
            )
        except Exception as exc:
            if (
                _is_transient_bridge_poll_timeout(exc)
                and time.monotonic() < deadline
            ):
                continue
            reason_id = (
                "shader-resource-index-capture-changed"
                if "capture changed" in str(exc).lower()
                else "shader-resource-index-unavailable"
            )
            raise CatalogPreparationRequired(
                "could not poll the bounded Shader resource index: %s" % exc,
                reason_id,
            ) from exc
        if control_checkpoint is not None:
            control_checkpoint("after Shader resource-index poll")

    result = snapshot.get("result")
    reason_id = str(result.get("reasonId", "") or "") if isinstance(result, Mapping) else ""
    capability = result.get("capability") if isinstance(result, Mapping) else None
    if (
        not isinstance(result, Mapping)
        or result.get("schema") != "replaylab-project-shader-resource-index/v2"
        or result.get("schemaVersion") != 2
        or result.get("captureSessionId") != capture_session_id
        or _path_key(result.get("capturePath", "")) != _path_key(capture_path)
        or result.get("captureId") != capture_id
        or result.get("identityEvidenceSha256") != evidence_sha256
        or result.get("requestSha256") != request_sha256
        or not isinstance(capability, Mapping)
        or capability.get("eventTraversalSupported") is not False
        or capability.get("fullFrameTraversalSupported") is not False
        or capability.get("anchorPreflightRequired") is not True
        or result.get("inventoryOnly") is not False
        or result.get("leaseEligible") is not True
        or result.get("eventTraversalCount") != 0
        or result.get("fullFrameTraversal") is not False
        or result.get("gpuReplayMutation") is not False
        or result.get("physicalApplyAuthority") is not False
        or result.get("resourceOwnershipScope") != "target-capture-session"
        or result.get("crossCaptureResourceIdReuse") is not False
    ):
        raise CatalogPreparationRequired(
            "RenderDoc returned invalid zero-EID Shader index evidence",
            "shader-resource-index-invalid-response",
        )
    if result.get("matched") is not True:
        raise CatalogPreparationRequired(
            "bounded Shader resource index did not uniquely match the project assets",
            reason_id or "shader-resource-index-mismatch",
        )
    if (
        capability.get("supported") is not True
        or capability.get("api") != api
        or capability.get("physicalAbiFromNullPipeline") != (api != "vulkan")
    ):
        raise CatalogPreparationRequired(
            "RenderDoc returned inconsistent Shader resource-index capability evidence",
            "shader-resource-index-invalid-response",
        )
    assets = result.get("assets")
    if (
        not isinstance(assets, list)
        or len(assets) != len(shaders)
        or result.get("expectedShaderCount") != len(shaders)
        or result.get("matchedShaderCount") != len(shaders)
    ):
        raise CatalogPreparationRequired(
            "bounded Shader resource index returned an incomplete asset set",
            "shader-resource-index-mismatch",
        )
    expected_by_owner = {
        (
            item["resourceId"], item["stage"], item["entryPoint"],
            item["encoding"], item["rawSha256"]
        ): item
        for item in shaders
    }
    seen_owners: set[tuple[Any, ...]] = set()
    for asset in assets:
        if not isinstance(asset, Mapping):
            raise CatalogPreparationRequired(
                "bounded Shader resource index returned a malformed asset",
                "shader-resource-index-invalid-response",
            )
        owner = (
            asset.get("resourceId"), asset.get("stage"), asset.get("entryPoint"),
            asset.get("encoding"), asset.get("rawSha256")
        )
        expected = expected_by_owner.get(owner)
        if (
            expected is None
            or owner in seen_owners
            or asset.get("ownerResourceId") != expected["resourceId"]
            or asset.get("ownerCaptureSessionId") != capture_session_id
            or asset.get("resourceOwnershipScope") != "target-capture-session"
            or asset.get("crossCaptureResourceIdReuse") is not False
            or asset.get("api") != api
            or asset.get("abiHash") != expected.get("abiHash")
            or asset.get("stableKeyComplete")
            != expected.get("stableKeyComplete")
            or asset.get("stableKeySha256")
            != expected.get("stableKeySha256")
            or asset.get("interfaceHash") != expected["interfaceHash"]
            or asset.get("layoutHash") != expected["layoutHash"]
            or asset.get("interfaceComplete") != expected["interfaceComplete"]
            or asset.get("requiresAnchorPreflight") is not True
            or asset.get("anchorEventId") != expected["anchorEventId"]
        ):
            raise CatalogPreparationRequired(
                "bounded Shader resource index changed asset ownership or ABI identity",
                "shader-resource-index-invalid-response",
            )
        seen_owners.add(owner)
        if api == "vulkan":
            if (
                asset.get("physicalAbiValidated") is not False
                or asset.get("physicalAbiSha256", "") != ""
                or asset.get("contentMatchMode")
                != "module-sha256-owner-rid-anchor-preflight"
            ):
                raise CatalogPreparationRequired(
                    "Vulkan null-pipeline reflection claimed physical ABI authority",
                    "shader-resource-index-invalid-response",
                )
        elif (
            asset.get("physicalAbiValidated") is not True
            or not isinstance(asset.get("physicalAbiSha256"), str)
            or len(asset.get("physicalAbiSha256")) != 64
            or any(
                character not in _SHA256_CHARS
                for character in str(asset.get("physicalAbiSha256", ""))
            )
            or asset.get("constantBlocksMatched") is not True
            or asset.get("interfaceMatched") is not True
            or asset.get("contentMatchMode") != "stable-key-owner-rid"
            or asset.get("constantBlocks") != expected["constantBlocks"]
        ):
            raise CatalogPreparationRequired(
                "bounded Shader resource index did not prove the complete ABI/block set",
                "shader-resource-index-interface-mismatch",
            )
    if seen_owners != set(expected_by_owner):
        raise CatalogPreparationRequired(
            "bounded Shader resource index omitted target-RDC ownership",
            "shader-resource-index-mismatch",
        )
    live_sha256 = _hex(
        result.get("liveIdentitySha256"), 64, "live Shader identitySha256"
    )
    if control_checkpoint is not None:
        control_checkpoint("before Shader resource-index commit")
    try:
        lease = host.ipc.call(
            "commit_rdc_project_shader_resource_index",
            {
                "expected_capture_path": str(capture_path),
                "expected_capture_session_id": capture_session_id,
                "job_id": job_id,
                "expected_capture_id": capture_id,
                "expected_identity_evidence_sha256": evidence_sha256,
                "live_identity_sha256": live_sha256,
            },
            timeout=10.0,
        )
    except Exception as exc:
        reason_id = (
            "shader-resource-index-capture-changed"
            if "capture changed" in str(exc).lower()
            else "shader-resource-index-unavailable"
        )
        raise CatalogPreparationRequired(
            "could not commit the bounded Shader resource index: %s" % exc,
            reason_id,
        ) from exc
    if control_checkpoint is not None:
        control_checkpoint("after Shader resource-index commit")
    if (
        not isinstance(lease, dict)
        or lease.get("schema")
        != "replaylab-project-shader-resource-index-lease/v1"
        or lease.get("schemaVersion") != 1
        or lease.get("jobId") != job_id
        or lease.get("captureId") != capture_id
        or lease.get("captureSessionId") != capture_session_id
        or lease.get("identityEvidenceSha256") != evidence_sha256
        or lease.get("liveIdentitySha256") != live_sha256
        or lease.get("committed") is not True
        or lease.get("eventTraversalCount") != 0
        or lease.get("gpuReplayMutation") is not False
        or lease.get("physicalApplyAuthority") is not False
        or lease.get("resourceOwnershipScope") != "target-capture-session"
        or lease.get("crossCaptureResourceIdReuse") is not False
    ):
        raise CatalogPreparationRequired(
            "RenderDoc returned an invalid Shader resource-index lease",
            "shader-resource-index-invalid-response",
        )
    return {"job": dict(snapshot), "result": dict(result), "lease": lease}


def _match_workspace_resources(
    host: Any,
    capture_path: Path,
    capture_session_id: str,
    validated: Sequence[tuple[Mapping[str, Any], Path, dict[str, Any]]],
    control_checkpoint: Callable[[str], None] | None = None,
    *,
    inventory: Mapping[str, Any] | None = None,
    progress: _Progress | None = None,
) -> list[dict[str, Any]]:
    """Match cached artifacts to the unique target-capture resource inventory."""

    if not validated:
        raise RuntimeError("cached project binding has no Shader workspaces")
    if len(validated) > _MAX_FAST_PROJECT_SHADERS:
        raise CatalogPreparationRequired(
            "cached project binding exceeds the fast Shader resource-index limit",
            "shader-resource-index-shader-limit",
        )
    if inventory is None:
        groups: dict[
            tuple[str, str, str, str, str],
            list[tuple[Mapping[str, Any], dict[str, Any], dict[str, Any]]],
        ] = {}
        for record, unused_workspace, status in validated:
            if control_checkpoint is not None:
                control_checkpoint("before cached Shader catalog projection")
            projection = host._workspace_catalog_binding_projection(status)
            group_key = (
                str(projection["captureId"]),
                _path_key(projection["capturePath"]),
                str(projection["api"]),
                _path_key(projection["catalogPath"]),
                str(projection["catalogSha256"]),
            )
            groups.setdefault(group_key, []).append((record, status, projection))
            if control_checkpoint is not None:
                control_checkpoint("after cached Shader catalog projection")

        reports: list[dict[str, Any]] = []
        matched_position = 0
        for group_key, entries in groups.items():
            first_status = entries[0][1]
            if control_checkpoint is not None:
                control_checkpoint("before cached Shader catalog identity evidence")
            scope = host._workspace_catalog_rebind_scope(first_status)
            evidence = scope.get("identityEvidence")
            if not isinstance(evidence, Mapping):
                raise RuntimeError("workspace catalog omitted exact identity evidence")
            evidence_sha256 = host._canonical_json_sha256(
                evidence, "RDC cached workspace identity evidence"
            )
            raw_use_tuples = evidence.get("useTuples", [])
            if (
                not isinstance(raw_use_tuples, list)
                or len(raw_use_tuples) > _MAX_FAST_EVIDENCE_USES
            ):
                raise CatalogPreparationRequired(
                    "workspace catalog evidence exceeds the bounded use-index budget",
                    "shader-resource-index-evidence-limit",
                )
            use_tuples = {
                (int(item[0]), str(item[1]))
                for item in raw_use_tuples
                if isinstance(item, list)
                and len(item) == 2
                and not isinstance(item[0], bool)
                and isinstance(item[0], int)
            }
            catalog = _strict_fast_catalog(group_key[3], group_key[4])
            catalog_shader_index = _fast_catalog_shader_index(catalog)
            api = _asset_api(group_key[2])
            capture_record = catalog.get("capture")
            catalog_api = catalog.get("api", "")
            if not catalog_api and isinstance(capture_record, Mapping):
                catalog_api = capture_record.get("api", "")
            if _asset_api(catalog_api) != api:
                raise CatalogPreparationRequired(
                    "active catalog API differs from the managed Shader workspace",
                    "shader-resource-index-api-mismatch",
                )
            if control_checkpoint is not None:
                control_checkpoint("after cached Shader catalog identity evidence")

            expected_shaders: list[dict[str, Any]] = []
            expected_owner_keys: set[tuple[Any, ...]] = set()
            total_blocks = 0
            for record, unused_status, projection in entries:
                current_key = (
                    str(projection["captureId"]),
                    _path_key(projection["capturePath"]),
                    str(projection["api"]),
                    _path_key(projection["catalogPath"]),
                    str(projection["catalogSha256"]),
                )
                if current_key != group_key:
                    raise RuntimeError("cached workspaces changed catalog grouping")
                identity = _normal_shader_identity(record.get("identity", {}))
                event_id = _shader_anchor_event_id(identity)
                if (event_id, str(projection["shaderKey"])) not in use_tuples:
                    raise RuntimeError(
                        "cached Shader anchor is absent from the exact workspace catalog"
                    )
                expected = _expected_resource_shader(
                    catalog,
                    identity,
                    api,
                    event_id,
                    catalog_shader_index,
                )
                owner_key = (
                    expected["resourceId"],
                    expected["stage"],
                    expected["entryPoint"],
                    expected["rawSha256"],
                )
                if owner_key in expected_owner_keys:
                    raise CatalogPreparationRequired(
                        "cached project binding repeats one target-RDC Shader owner",
                        "shader-resource-index-ambiguous",
                    )
                expected_owner_keys.add(owner_key)
                total_blocks += len(expected["constantBlocks"])
                if total_blocks > _MAX_FAST_PROJECT_BLOCKS:
                    raise CatalogPreparationRequired(
                        "cached project binding exceeds the fast CBuffer index limit",
                        "shader-resource-index-block-limit",
                    )
                expected_shaders.append(expected)
                matched_position += 1
                if progress is not None:
                    progress.emit(
                        "resource-match",
                        "Matching exact project Shader artifacts",
                        current=matched_position,
                        total=len(validated),
                        item="Shader ResourceId %d" % identity["resourceId"],
                        detail={
                            "resourceId": identity["resourceId"],
                            "stableKeySha256": identity["stableKeySha256"],
                            "eventTraversalCount": 0,
                        },
                    )

            reports.append(
                _resource_index_job(
                    host,
                    capture_path,
                    capture_session_id,
                    group_key[0],
                    evidence_sha256,
                    api,
                    expected_shaders,
                    control_checkpoint,
                )
            )
        return reports
    capture = inventory.get("capture") if isinstance(inventory, Mapping) else None
    assets = inventory.get("shaders") if isinstance(inventory, Mapping) else None
    if (
        not isinstance(capture, Mapping)
        or capture.get("captureSessionId") != capture_session_id
        or _path_key(capture.get("capturePath", "")) != _path_key(capture_path)
        or inventory.get("eventTraversalCount") != 0
        or inventory.get("fullFrameTraversal") is not False
        or inventory.get("anchorPerShaderMaximum") != 1
        or not isinstance(assets, list)
    ):
        raise CatalogPreparationRequired(
            "the target Shader resource inventory is invalid",
            "shader-resource-index-invalid-response",
        )
    by_owner: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for asset in assets:
        if not isinstance(asset, Mapping):
            continue
        key = (
            asset.get("resourceId"),
            asset.get("stage"),
            asset.get("entryPoint", ""),
            asset.get("rawSha256"),
        )
        by_owner.setdefault(key, []).append(asset)

    matched_assets: list[dict[str, Any]] = []
    for position, (record, unused_workspace, status) in enumerate(validated, 1):
        if control_checkpoint is not None:
            control_checkpoint("before cached Shader stable-key matching")
        identity = _normal_shader_identity(record.get("identity", {}))
        if identity.get("stableKeyComplete") is not True:
            raise CatalogPreparationRequired(
                "the legacy RDC Shader association has no complete stable key",
                "shader-resource-index-key-incomplete",
            )
        owner = (
            identity["resourceId"],
            identity["stage"],
            identity["entryPoint"],
            identity["rawSha256"],
        )
        candidates = list(by_owner.get(owner, []))
        exact = [
            item
            for item in candidates
            if item.get("stableKeyComplete") is True
            and item.get("stableKeySha256") == identity["stableKeySha256"]
            and item.get("api") == identity["api"]
            and item.get("encoding") == identity["encoding"]
            and item.get("abiHash") == identity["abiHash"]
            and item.get("layoutHash") == identity["layoutHash"]
            and item.get("anchorEventId") == identity.get("anchorEventId")
            and item.get("identityAmbiguous") is False
            and item.get("formatSupported") is True
        ]
        if len(exact) != 1:
            raise CatalogPreparationRequired(
                "the cached Shader association did not exactly match the target resource",
                (
                    "shader-resource-index-ambiguous"
                    if len(exact) > 1
                    else "shader-resource-index-mismatch"
                ),
            )
        target_profile = status.get("targetProfile")
        if (
            not isinstance(target_profile, Mapping)
            or _asset_api(status.get("capture", {}).get("api"))
            != identity["api"]
            or _asset_encoding(status.get("baseline", {}).get("kind"))
            != identity["encoding"]
        ):
            raise CatalogPreparationRequired(
                "the managed Shader workspace API/encoding changed",
                "shader-resource-index-format-unsupported",
            )
        matched_assets.append(dict(exact[0]))
        if progress is not None:
            progress.emit(
                "resource-match",
                "Matching exact project Shader artifacts",
                current=position,
                total=len(validated),
                item="Shader ResourceId %d" % identity["resourceId"],
                detail={
                    "resourceId": identity["resourceId"],
                    "stableKeySha256": identity["stableKeySha256"],
                    "eventTraversalCount": 0,
                },
            )
        if control_checkpoint is not None:
            control_checkpoint("after cached Shader stable-key matching")
    return [
        {
            "schema": "replaylab-project-shader-resource-match/v2",
            "schemaVersion": 2,
            "inventory": inventory,
            "assets": matched_assets,
            "eventTraversalCount": 0,
        }
    ]


def _validate_workspace_apply_response(
    host: Any,
    workspace: Path,
    status: Mapping[str, Any],
    wrapped: Mapping[str, Any],
    *,
    operation_id: str,
    expected_revision: int,
    expected_candidate_raw_sha256: str,
    expected_anchor_event_id: int,
    capture_session_id: str,
) -> dict[str, Any]:
    binding = wrapped.get("binding")
    validation = wrapped.get("validation")
    replacement = wrapped.get("replacement")
    rollback = wrapped.get("rollback")
    shader = status.get("shader")
    capture = status.get("capture")
    baseline = status.get("baseline")
    latest = status.get("latestCompiled")
    target_profile = status.get("targetProfile")
    if not all(
        isinstance(value, Mapping)
        for value in (binding, validation, replacement, rollback, shader, capture, baseline, latest, target_profile)
    ):
        raise RuntimeError("Shader apply returned incomplete live ABI/replacement evidence")
    original_raw_sha256 = _hex(
        baseline.get("rawSha256"), 64, "Shader apply baseline.rawSha256"
    )
    baseline_abi_sha256 = _hex(
        baseline.get("abiSha256"), 64, "Shader apply baseline.abiSha256"
    )
    expected_api = _asset_api(capture.get("api"))
    expected_encoding = _asset_encoding(latest.get("kind"))
    expected_profile = _text(
        str(target_profile.get("profile", "")),
        "Shader apply targetProfile.profile",
        64,
    ).lower()
    validation_scope = validation.get("validationScope", "")
    if validation_scope == "already-applied-live-resource":
        # This public route has just checked exact current files and the live
        # Bridge inventory. It deliberately has no new ABI request/report or
        # physical mutation. Validate that readback as such, not as a fresh
        # apply (which used to reject it and redo every successful batch item).
        current = _workspace_artifact_service(host).status(str(workspace))
        current_latest = current.get("latestCompiled") or {}
        row_revision = replacement.get("revision")
        physical = replacement.get("physicalAbiSha256")
        requested = wrapped.get("requestedBinding") or {}
        same_anchor = (replacement.get("anchorEventId") == expected_anchor_event_id or (
            requested.get("eventId") == expected_anchor_event_id
            and requested.get("originalResourceId") == shader.get("resourceId")
            and requested.get("originalRawSha256") == original_raw_sha256
            and requested.get("stage") == status.get("stage")
            and requested.get("entryPoint", "") == status.get("entryPoint", "")))
        valid = all((
            wrapped.get("schema") == "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
            wrapped.get("operation") == "apply-shared-resource", wrapped.get("operationId") == operation_id,
            wrapped.get("scope") == "captured-resource", wrapped.get("applied") is True,
            wrapped.get("idempotent") is True, wrapped.get("appliedByThisCall") is False,
            wrapped.get("replayMutation") is False, wrapped.get("evidenceSource") == "current-workspace-and-bridge",
            binding.get("workspaceId") == status.get("workspaceId"), binding.get("captureId") == capture.get("captureId"),
            binding.get("revision") == expected_revision, binding.get("candidateRawSha256") == expected_candidate_raw_sha256,
            binding.get("sourceSha256") == latest.get("sourceSha256"),
            binding.get("stage") == status.get("stage"), binding.get("entryPoint", "") == status.get("entryPoint", ""),
            binding.get("api") == expected_api, binding.get("encoding") == expected_encoding,
            validation.get("compatible") is True, validation.get("replayAbiValidated") is True,
            validation.get("reusedValidation") is True, validation.get("freshPhysicalValidationPerformed") is False,
            validation.get("captureSessionId") == capture_session_id,
            type(row_revision) is int and 0 < row_revision <= expected_revision,
            validation.get("physicalCandidateRevision") == row_revision,
            validation.get("baselineAbiSha256") == physical, physical in _workspace_physical_abi_hashes(status),
            replacement.get("workspaceId") == status.get("workspaceId"), same_anchor,
            replacement.get("originalResourceId") == shader.get("resourceId"), replacement.get("originalRawSha256") == original_raw_sha256,
            replacement.get("candidateRawSha256") == expected_candidate_raw_sha256,
            replacement.get("stage") == status.get("stage"), replacement.get("entryPoint", "") == status.get("entryPoint", ""),
            replacement.get("api") == expected_api, replacement.get("encoding") == expected_encoding,
            replacement.get("resourceScoped") is True, replacement.get("eventScoped") is False,
            replacement.get("allOriginalResourceUsesAffected") is True, replacement.get("originalShaderPreserved") is True,
            replacement.get("postApplyValidationFailed") is False,
            type(replacement.get("targetResourceId")) is int and replacement["targetResourceId"] > 0,
            rollback.get("tool") == "rollback_shader_workspace_shared_resource",
            _path_key(rollback.get("workspacePath", "")) == _path_key(workspace),
            rollback.get("replacementId") == replacement.get("replacementId"),
            current.get("workspaceId") == status.get("workspaceId"),
            current.get("capture") == capture, current.get("shader") == shader,
            current.get("revision") == expected_revision,
            current.get("source", {}).get("currentSha256") == binding.get("sourceSha256"),
            current_latest.get("sourceSha256") == binding.get("sourceSha256"),
            current_latest.get("rawSha256") == expected_candidate_raw_sha256,
            current_latest.get("replayAbiValidated") is True, current_latest.get("abiCompatible") is True,
            (current_latest.get("abiGate") or {}).get("status") == "compatible",
        ))
        _hex(replacement.get("replacementId"), 32, "live readback replacementId")
        _hex(replacement.get("operationId"), 32, "live readback physical operationId")
        if not valid:
            raise RuntimeError("Shader current-session readback differs from its source/candidate/physical binding")
        return dict(wrapped)
    # Project restore calls the public direct-MCP apply route. Lease/live-edit
    # wrappers have a different request binding and are not accepted here.
    ordinary_validation = validation_scope == ""
    pair_validation = validation_scope == "verified-shader-pair"
    request_id = ""
    report_sha256 = ""
    validation_evidence_valid = False
    if ordinary_validation or pair_validation:
        request_id = _hex(
            validation.get("requestId"), 64, "Shader apply validation.requestId"
        )
        report_sha256 = _hex(
            validation.get("abiReportSha256"),
            64,
            "Shader apply validation.abiReportSha256",
        )
        candidate_abi_sha256 = _hex(
            validation.get("candidateAbiSha256"), 64,
            "Shader apply validation.candidateAbiSha256",
        )
        # The canonical status reader revalidates the complete persisted ABI
        # report against its immutable candidate, reflection request and hash.
        # Compatibility may legitimately compare two different ABI digests
        # (for example, recovered CBuffer fields with proven physical access).
        readback = _tool_json(host.get_shader_workspace_status(str(workspace)),
            "read applied Shader ABI report", schema="renderdoc-ultra-shader-workspace-mcp/v1")
        current = readback.get("result") or {}
        current_latest = current.get("latestCompiled") or {}
        current_gate = current_latest.get("abiGate") or {}
        if (readback.get("operation") != "status"
                or current.get("workspaceId") != status.get("workspaceId")
                or current.get("capture") != capture
                or current.get("shader") != shader
                or current.get("baseline") != baseline
                or current.get("stage") != status.get("stage")
                or current.get("entryPoint", "") != status.get("entryPoint", "")
                or current.get("targetProfile") != target_profile
                or current_latest.get("revision") != expected_revision
                or current_latest.get("rawSha256") != expected_candidate_raw_sha256
                or current_latest.get("sourceSha256") != latest.get("sourceSha256")
                or current.get("source", {}).get("currentSha256") != latest.get("sourceSha256")
                or current_gate.get("status") != "compatible"
                or current_gate.get("baselineAbiSha256") != baseline_abi_sha256
                or current_gate.get("candidateAbiSha256") != candidate_abi_sha256
                or current_gate.get("requestId") != request_id
                or current_gate.get("reportSha256") != report_sha256
                or _path_key(workspace / str(current_gate.get("reportPath", "")))
                    != _path_key(validation.get("abiReportPath", ""))):
            raise RuntimeError("Shader apply returned invalid live ABI/replacement evidence: current ABI report differs")
        validation_evidence_valid = bool(
            validation.get("workspacePath") == str(workspace)
            and validation.get("revision") == expected_revision
            and validation.get("baselineAbiSha256") == (
                replacement.get("physicalAbiSha256") if pair_validation else baseline_abi_sha256)
            and isinstance(validation.get("differences"), list)
            and not validation.get("differences")
            and validation.get("differencesTruncated") is False
            and validation.get("applied") is False
        )
        if pair_validation:
            # GLSL is compiled by the replay driver when its target is rebuilt.
            # Its inline interface check is additional fresh evidence even
            # though the original/candidate pair has an authenticated receipt.
            expected_fresh = bool(_asset_encoding(latest.get("kind")) == "glsl"
                                  and replacement.get("targetBuiltOnce") is True)
            validation_evidence_valid = validation_evidence_valid and bool(
                validation.get("reusedValidation") is True
                and validation.get("freshPhysicalValidationPerformed") is expected_fresh
                and validation.get("captureSessionId") == capture_session_id
                and replacement.get("abiValidationMode") == "verified-shader-pair"
                and replacement.get("freshPhysicalAbiValidationPerformed") is expected_fresh
                # Pair replay describes the authenticated current physical
                # interface; the immutable report/binding above still uses
                # its original baseline spelling. Require the exact signed
                # physical digest here, and only the verified topology alias
                # set below, without rewriting either historical artifact.
                and replacement.get("physicalAbiSha256") == (replacement.get(
                    "restoreValidationReceipt") or {}).get("payload", {}).get("physicalAbiSha256")
                and _shader_pair_receipt_matches(
                    replacement.get("restoreValidationReceipt"),
                    _shader_pair_validation_material(status), status,
                )
            )
    replacement_id = str(replacement.get("replacementId", "") or "")
    anchor_event_id = replacement.get("anchorEventId")
    candidate_byte_size = binding.get("candidateByteSize")
    if (
        wrapped.get("operation") != "apply-shared-resource"
        or wrapped.get("operationId") != operation_id
        or wrapped.get("applied") is not True
        or wrapped.get("scope") != "captured-resource"
        or binding.get("workspaceId") != status.get("workspaceId")
        or binding.get("captureId") != capture.get("captureId")
        or binding.get("revision") != expected_revision
        or binding.get("candidateRawSha256") != expected_candidate_raw_sha256
        or isinstance(candidate_byte_size, bool)
        or not isinstance(candidate_byte_size, int)
        or candidate_byte_size <= 0
        or binding.get("baselineAbiSha256") != baseline_abi_sha256
        or binding.get("stage") != status.get("stage")
        or binding.get("entryPoint", "") != status.get("entryPoint", "")
        or binding.get("api") != expected_api
        or binding.get("encoding") != expected_encoding
        or binding.get("targetProfile") != expected_profile
        or validation.get("compatible") is not True
        or validation.get("requiresReplayAbiValidation") is not False
        or validation.get("replayAbiValidated") is not True
        or not validation_evidence_valid
        or replacement.get("schema")
        != "renderdoc-ultra-shared-shader-replacement/v1"
        or replacement.get("schemaVersion") != 1
        or replacement.get("operationId") != operation_id
        or not isinstance(replacement.get("operationCreatedReplacement"), bool)
        or replacement.get("captureId") != capture.get("captureId")
        or replacement.get("workspaceId") != status.get("workspaceId")
        or replacement.get("captureSessionId") != capture_session_id
        or replacement.get("revision") != expected_revision
        or isinstance(anchor_event_id, bool)
        or not isinstance(anchor_event_id, int)
        or anchor_event_id != expected_anchor_event_id
        or replacement.get("stage") != status.get("stage")
        or replacement.get("entryPoint", "") != status.get("entryPoint", "")
        or replacement.get("api") != expected_api
        or replacement.get("encoding") != expected_encoding
        or replacement.get("originalResourceId") != shader.get("resourceId")
        or replacement.get("originalRawSha256") != original_raw_sha256
        or replacement.get("candidateRawSha256") != expected_candidate_raw_sha256
        or replacement.get("physicalAbiSha256") not in _workspace_physical_abi_hashes(status)
        or replacement.get("abiRequestId") != request_id
        or replacement.get("abiReportSha256") != report_sha256
        or len(replacement_id) != 32
        or any(character not in _HEX32_CHARS for character in replacement_id)
        or replacement.get("resourceScoped") is not True
        or replacement.get("eventScoped") is not False
        or replacement.get("allOriginalResourceUsesAffected") is not True
        or replacement.get("configured") is not True
        or replacement.get("originalShaderPreserved") is not True
        or replacement.get("singleOwner") is not True
        or rollback.get("tool") != "rollback_shader_workspace_shared_resource"
        or _path_key(rollback.get("workspacePath", "")) != _path_key(workspace)
        or rollback.get("replacementId") != replacement_id
    ):
        raise RuntimeError("Shader apply returned invalid live ABI/replacement evidence")
    return wrapped


def _recover_uncertain_workspace_apply(
    host: Any,
    status: Mapping[str, Any],
    operation_id: str,
    capture_session_id: str,
) -> dict[str, Any]:
    capture_id = _hex(
        status.get("capture", {}).get("captureId"),
        64,
        "uncertain Shader apply captureId",
    )
    workspace_id = _hex(
        status.get("workspaceId"), 32, "uncertain Shader apply workspaceId"
    )
    receipt = host.ipc.call(
        "recover_shader_candidate_shared_resource_operation",
        {
            "operation_id": _hex(
                operation_id, 32, "uncertain Shader apply operationId"
            ),
            "expected_capture_id": capture_id,
            "expected_capture_session_id": _hex(
                capture_session_id,
                32,
                "uncertain Shader apply captureSessionId",
            ),
            "workspace_id": workspace_id,
        },
        timeout=60.0,
    )
    rollback = receipt.get("rollback") if isinstance(receipt, Mapping) else None
    status_value = receipt.get("status") if isinstance(receipt, Mapping) else None
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema")
        != "renderdoc-ultra-shared-shader-operation-recovery/v1"
        or receipt.get("schemaVersion") != 1
        or receipt.get("operationId") != operation_id
        or receipt.get("captureId") != capture_id
        or receipt.get("captureSessionId") != capture_session_id
        or receipt.get("workspaceId") != workspace_id
        or receipt.get("originalShaderPreserved") is not True
        or receipt.get("strictReceipt") is not True
        or receipt.get("recovered") is not True
        or receipt.get("mutationPresent") is not False
        or status_value
        not in {"absent", "idempotent-noop", "recovered", "already-recovered"}
        or not isinstance(receipt.get("operationKnown"), bool)
        or not isinstance(receipt.get("operationCreatedReplacement"), bool)
        or (
            status_value == "recovered"
            and (
                not isinstance(rollback, Mapping)
                or rollback.get("replacementId") != receipt.get("replacementId")
                or rollback.get("captureId") != capture_id
                or rollback.get("captureSessionId") != capture_session_id
                or rollback.get("workspaceId") != workspace_id
                or rollback.get("resourceScoped") is not True
                or rollback.get("eventScoped") is not False
                or rollback.get("cleared") is not True
                or rollback.get("originalShaderPreserved") is not True
            )
        )
        or (status_value != "recovered" and rollback is not None)
    ):
        raise RestoreRollbackFailed(
            "RenderDoc returned invalid uncertain Shader operation recovery evidence"
        )
    return dict(receipt)


_SHADER_PAIR_VALIDATION_SCHEMA = "replaylab-project-validated-shader-pair/v1"


def _workspace_physical_abi_hashes(status: Mapping[str, Any]) -> frozenset[str]:
    """Verify stored evidence before accepting the historical Unknown spelling.

    Keep cache keys, ABI report bindings and signed payloads byte-for-byte. A
    digest with no complete physical interface remains exact-only.
    """
    from .shader_workspace.abi import parse_shader_abi, physical_shader_abi_hashes

    baseline = status.get("baseline") or {}
    digest = _hex(baseline.get("abiSha256"), 64, "workspace physical ABI SHA-256")
    if baseline.get("compiledInterface") is None:
        return frozenset((digest,))
    abi = parse_shader_abi(
        baseline["compiledInterface"], expected_stage=str(status.get("stage") or ""),
        expected_entry_point=str(status.get("entryPoint") or ""))
    if abi.sha256 != digest:
        raise RuntimeError("workspace baseline physical ABI evidence differs from its hash")
    return physical_shader_abi_hashes(abi)


def _shader_pair_validation_material(status: Mapping[str, Any]) -> dict[str, Any]:
    latest = status.get("latestCompiled") or {}
    source = status.get("source") or {}
    baseline = status.get("baseline") or {}
    capture = status.get("capture") or {}
    if (source.get("present") is not True
            or source.get("currentSha256") != latest.get("sourceSha256")
            or latest.get("revision") != status.get("revision")):
        raise RuntimeError("cached Shader pair has no current source/candidate binding")
    return {
        "originalRawSha256": _hex(baseline.get("rawSha256"), 64, "pair originalRawSha256"),
        "candidateRawSha256": _hex(latest.get("rawSha256"), 64, "pair candidateRawSha256"),
        "physicalAbiSha256": _hex(baseline.get("abiSha256"), 64, "pair physicalAbiSha256"),
        "api": _asset_api(capture.get("api")),
        "stage": str(status.get("stage") or ""),
        "entryPoint": str(status.get("entryPoint") or ""),
        "encoding": _asset_encoding(latest.get("kind")),
        "targetProfile": copy.deepcopy(status.get("targetProfile")),
    }


def _shader_pair_validation_path(project: Path, status: Mapping[str, Any]) -> Path:
    capture_path = status.get("capture", {}).get("capturePath", "")
    capture_key = hashlib.sha256(_path_key(capture_path).encode("utf-8")).hexdigest()
    key = _canonical_sha256(_shader_pair_validation_material(status))
    return project / ".replaylab" / "replay-project" / "derived" / "validated-shader-pairs" / capture_key / (key + ".json")


def _shader_pair_receipt_matches(
    receipt: Any, material: Mapping[str, Any], status: Mapping[str, Any] | None = None,
) -> bool:
    payload = receipt.get("payload") if isinstance(receipt, Mapping) else None
    if not isinstance(payload, Mapping) or payload.get("schema") != "replaylab-validated-shader-restore/v1":
        return False
    try:
        _hex(receipt.get("signature"), 64, "Shader pair receipt signature")
        _hex(payload.get("captureSha256"), 64, "Shader pair RDC content hash")
    except RuntimeError:
        return False
    if "targetProfile" in payload:
        profile = payload["targetProfile"]
        expected_profile = material.get("targetProfile")
        if isinstance(profile, str):
            if not isinstance(expected_profile, Mapping) or profile != expected_profile.get("profile"):
                return False
        elif profile != expected_profile:
            return False
    physical_hashes = frozenset((material["physicalAbiSha256"],))
    if status is not None:
        try:
            physical_hashes = _workspace_physical_abi_hashes(status)
        except Exception:
            return False
        if material["physicalAbiSha256"] != (status.get("baseline") or {}).get("abiSha256"):
            return False
    return payload.get("physicalAbiSha256") in physical_hashes and all(payload.get(key) == material[key] for key in (
        "originalRawSha256", "candidateRawSha256",
        "api", "stage", "entryPoint", "encoding",
    ))


def _workspace_shader_pair_validation_path(status: Mapping[str, Any]) -> Path:
    workspace = Path(str(status.get("workspacePath", "")))
    path = workspace / "ai" / "validated-shader-pair.json"
    if (not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir()
            or path.is_symlink() or not is_within(workspace.resolve(), path.resolve())):
        raise RuntimeError("workspace Shader pair cache path is redirected")
    return path


def _persist_workspace_shader_pair_validation(
    status: Mapping[str, Any], replacement: Mapping[str, Any], abi_lease: Mapping[str, Any],
    *, allow_same_pair_revision_reuse: bool = False,
) -> bool:
    """Save an already checked AI/live-edit effect without another project read.

    The caller owns successful replacement validation. This optional local
    record checks its current source/candidate and signed byte-pair binding;
    only the Bridge can authenticate the proof when a future apply consumes it.
    """
    try:
        material = _shader_pair_validation_material(status)
        receipt = replacement.get("restoreValidationReceipt")
        physical_revision = replacement.get("revision")
        current_revision = status.get("revision")
        revision_matches = physical_revision == current_revision
        if allow_same_pair_revision_reuse and not revision_matches:
            revision_matches = bool(
                isinstance(physical_revision, int) and not isinstance(physical_revision, bool)
                and isinstance(current_revision, int) and 0 < physical_revision < current_revision
                and _current_compiled_pair_matches(status, current_revision, material["candidateRawSha256"])
                and isinstance(receipt, Mapping)
                and receipt.get("payload", {}).get("revision") == physical_revision
            )
            if (revision_matches and "targetProfile" not in receipt.get("payload", {})
                    and receipt["payload"].get("sourceSha256") != status["source"]["currentSha256"]):
                # Legacy signed receipts did not include profile metadata.
                # If the Bridge sealed this exact current source/candidate,
                # the validated compiled bytecode fixes its native profile.
                # A later source must instead retain the saved exact profile
                # and envelope; an old proof cannot infer a new source lane.
                old_path = _workspace_shader_pair_validation_path(status)
                if not old_path.is_file() or old_path.stat().st_size > _MAX_FAST_CATALOG_BYTES:
                    return False
                old = json.loads(old_path.read_text(encoding="utf-8"))
                seal = old.pop("sealSha256", None)
                if (seal != _canonical_sha256(old)
                        or old.get("schema") != _SHADER_PAIR_VALIDATION_SCHEMA
                        or old.get("workspaceId") != status.get("workspaceId")
                        or old.get("pair") != material
                        or old.get("replacement", {}).get("restoreValidationReceipt") != receipt):
                    return False
        if (replacement.get("schema") != "renderdoc-ultra-shared-shader-replacement/v1"
                or replacement.get("schemaVersion") != 1
                or replacement.get("workspaceId") != status.get("workspaceId")
                or not revision_matches
                or replacement.get("captureId") != status.get("capture", {}).get("captureId")
                or replacement.get("originalResourceId") != status.get("shader", {}).get("resourceId")
                or replacement.get("resourceScoped") is not True
                or replacement.get("eventScoped") is not False
                or replacement.get("allOriginalResourceUsesAffected") is not True
                or any(replacement.get(key) != material[key] for key in (
                    "originalRawSha256", "candidateRawSha256",
                    "api", "stage", "entryPoint", "encoding"))
                or replacement.get("physicalAbiSha256") not in _workspace_physical_abi_hashes(status)
                or not _shader_pair_receipt_matches(receipt, material, status)):
            return False
        lease = {key: abi_lease.get(key) for key in (
            "abiLeaseRequestId", "abiLeaseReportSha256", "abiLeaseCandidateRawSha256", "abiLeaseRevision")}
        for key in ("abiLeaseRequestId", "abiLeaseReportSha256", "abiLeaseCandidateRawSha256"):
            _hex(lease[key], 64, key)
        _integer(lease["abiLeaseRevision"], "ABI lease revision", 0)
        request_id = _hex(replacement.get("abiRequestId"), 64, "applied ABI request")
        report_sha = _hex(replacement.get("abiReportSha256"), 64, "applied ABI report")
        payload = receipt["payload"]
        if (payload.get("abiRequestId") != request_id
                or payload.get("abiReportSha256") != report_sha
                or report_sha != lease["abiLeaseReportSha256"]):
            return False
        if replacement.get("abiValidationMode") == "inline-physical-with-initial-lease":
            # A later save signs its NEW byte-bound request. The old report is
            # explicitly a lease; do not relabel its request as the new one.
            if (replacement.get("freshPhysicalAbiValidationPerformed") is not True
                    or any(replacement.get(key) != value for key, value in lease.items())):
                return False
        elif request_id != lease["abiLeaseRequestId"]:
            return False
        path = _workspace_shader_pair_validation_path(status)
        value = {
            "schema": _SHADER_PAIR_VALIDATION_SCHEMA, "schemaVersion": 1,
            "workspaceId": status["workspaceId"],
            "capturePath": str(status["capture"]["capturePath"]),
            "pair": material, "sourceSha256": status["source"]["currentSha256"],
            "candidateCodeSha256": status["latestCompiled"].get("codeSha256"),
            "replacement": copy.deepcopy(dict(replacement)), "abiLease": lease,
        }
        value["sealSha256"] = _canonical_sha256(value)
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_FAST_CATALOG_BYTES:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, encoded)
        return True
    except Exception:
        return False


def _load_workspace_shader_pair_validation(status: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        material = _shader_pair_validation_material(status)
        path = _workspace_shader_pair_validation_path(status)
        if not path.is_file() or path.stat().st_size > _MAX_FAST_CATALOG_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        seal = value.pop("sealSha256", None)
        if (seal == _canonical_sha256(value)
                and value.get("schema") == _SHADER_PAIR_VALIDATION_SCHEMA
                and value.get("schemaVersion") == 1
                and value.get("workspaceId") == status.get("workspaceId")
                and value.get("pair") == material
                and value.get("sourceSha256") == status.get("source", {}).get("currentSha256")
                and value.get("candidateCodeSha256") == status.get("latestCompiled", {}).get("codeSha256")
                and _path_key(value.get("capturePath", "")) == _path_key(status["capture"]["capturePath"])
                and _shader_pair_receipt_matches(value.get("replacement", {}).get("restoreValidationReceipt"), material, status)):
            return value
    except Exception:
        return None
    return None


def _persist_shader_pair_validation(
    project: Path, status: Mapping[str, Any], applied: Mapping[str, Any],
) -> bool:
    """Persist each successful immutable pair even when another Shader fails."""
    replacement = applied.get("replacement") or {}
    receipt = replacement.get("restoreValidationReceipt")
    material = _shader_pair_validation_material(status)
    if (applied.get("applied") is not True
            or applied.get("validation", {}).get("compatible") is not True
            or not _shader_pair_receipt_matches(receipt, material, status)):
        return False
    path = _shader_pair_validation_path(project, status)
    if not is_within(project.resolve(), path.resolve()) or path.is_symlink():
        raise RuntimeError("Shader pair cache path is redirected")
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "schema": _SHADER_PAIR_VALIDATION_SCHEMA, "schemaVersion": 1,
        "capturePath": str(status["capture"]["capturePath"]),
        "pair": material,
        "sourceSha256": status["source"]["currentSha256"],
        "candidateCodeSha256": status["latestCompiled"].get("codeSha256"),
        "replacement": copy.deepcopy(dict(replacement)),
        "validation": copy.deepcopy(dict(applied["validation"])),
    }
    value["sealSha256"] = _canonical_sha256(value)
    atomic_write_bytes(path, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return True


def _load_shader_pair_validation(
    project: Path, status: Mapping[str, Any], current_validation_policy_sha256: str | None = None,
    compatible_policy_sha256s: Sequence[str] = (),
) -> dict[str, Any] | None:
    """Find origin evidence; only the Bridge's HMAC can authorize pair reuse."""
    try:
        if (status.get("latestCompiled", {}).get("abiGate", {}).get("status") != "compatible"):
            return None
        def current_physical_policy(value: Mapping[str, Any]) -> bool:
            if not current_validation_policy_sha256:
                return True
            receipt = value.get("replacement", {}).get("restoreValidationReceipt")
            payload = receipt.get("payload") if isinstance(receipt, Mapping) else None
            physical = payload.get("physicalValidationBinding") if isinstance(payload, Mapping) else None
            return bool(
                isinstance(physical, Mapping)
                and physical.get("schema") == "replaylab-shader-physical-validation-binding/v1"
                and physical.get("schemaVersion") == 1
                and payload.get("validationPolicySha256") in (current_validation_policy_sha256, *compatible_policy_sha256s)
                and _canonical_sha256(physical) == payload.get("validationPolicySha256")
            )
        local = _load_workspace_shader_pair_validation(status)
        if local is not None and current_physical_policy(local):
            return local
        material = _shader_pair_validation_material(status)
        path = _shader_pair_validation_path(project, status)
        if path.is_file() and not path.is_symlink() and is_within(project.resolve(), path.resolve()):
            if path.stat().st_size > _MAX_FAST_CATALOG_BYTES:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            seal = value.pop("sealSha256", None)
            if (seal == _canonical_sha256(value)
                    and value.get("schema") == _SHADER_PAIR_VALIDATION_SCHEMA
                    and value.get("schemaVersion") == 1
                    and value.get("pair") == material
                    and current_physical_policy(value)
                    and _path_key(value.get("capturePath", "")) == _path_key(status["capture"]["capturePath"])
                    and _shader_pair_receipt_matches(value.get("replacement", {}).get("restoreValidationReceipt"), material, status)):
                return value
        # A previous complete RDC snapshot already contains independently
        # signed pairs. Package policy/source/catalog changes do not erase the
        # exact original/candidate bytes proven by those receipts.
        legacy = _verified_rdc_restore_path(
            project / ".replaylab" / "replay-project",
            Path(status["capture"]["capturePath"]),
        )
        if not legacy.is_file() or legacy.is_symlink() or legacy.stat().st_size > _MAX_FAST_CATALOG_BYTES:
            return None
        snapshot = json.loads(legacy.read_text(encoding="utf-8"))
        seal = snapshot.pop("sealSha256", None)
        if (seal != _canonical_sha256(snapshot)
                or _path_key(snapshot.get("capturePath", "")) != _path_key(status["capture"]["capturePath"])):
            return None
        for record in list(snapshot.get("shaders") or [])[:_MAX_FAST_PROJECT_SHADERS]:
            knowledge = snapshot.get("bindingState", {}).get("workspaces", {}).get(
                record.get("knowledgeId") if isinstance(record, Mapping) else "")
            if (isinstance(record, Mapping) and isinstance(knowledge, Mapping)
                    and current_physical_policy(record)
                    and knowledge.get("targetProfile") == material["targetProfile"]
                    and _shader_pair_receipt_matches(
                record.get("replacement", {}).get("restoreValidationReceipt"), material, status
            )):
                return {**copy.deepcopy(dict(record)), "pair": material}
    except Exception:
        return None
    return None


def _retained_shader_handoff_error(
    error: Exception, status: Mapping[str, Any], operation_id: str,
    capture_session_id: str, expected_revision: int,
    expected_candidate_raw_sha256: str, expected_anchor_event_id: int,
) -> str | None:
    """Recognise only this call's explicit, validated post-handoff failure.

    This grants no completion or replay authority. It prevents generic lost-
    response recovery from undoing a known committed handoff to the stock shader.
    Missing, malformed or foreign evidence keeps the ordinary recovery path.
    """
    data = getattr(error, "tool_data", None)
    if (not isinstance(data, Mapping)
            or set(data) != {"operationId", "replayReplacementRetained", "currentSourceVerified",
                             "replacement", "recordingError"}
            or data.get("operationId") != operation_id
            or data.get("replayReplacementRetained") is not True
            or data.get("currentSourceVerified") is not False
            or not isinstance(data.get("recordingError"), str)):
        return None
    replacement = data.get("replacement")
    if not isinstance(replacement, Mapping):
        return None
    expected = {
        "schema": "renderdoc-ultra-shared-shader-replacement/v1", "schemaVersion": 1,
        "operationId": operation_id, "captureSessionId": capture_session_id,
        "captureId": status.get("capture", {}).get("captureId"),
        "workspaceId": status.get("workspaceId"), "revision": expected_revision,
        "originalResourceId": status.get("shader", {}).get("resourceId"),
        "originalRawSha256": status.get("baseline", {}).get("rawSha256"),
        "candidateRawSha256": expected_candidate_raw_sha256,
        "anchorEventId": expected_anchor_event_id,
        "stage": status.get("stage"), "entryPoint": status.get("entryPoint", ""),
        "operationCreatedReplacement": True, "idempotent": False, "metadataOnly": False,
        "resourceScoped": True, "eventScoped": False, "configured": True,
        "singleOwner": True, "originalShaderPreserved": True, "allOriginalResourceUsesAffected": True,
    }
    if any(type(replacement.get(key)) is not type(value) or replacement.get(key) != value
           for key, value in expected.items()):
        return None
    try:
        replacement_id = _hex(replacement.get("replacementId"), 32, "retained replacementId")
        superseded_id = _hex(replacement.get("supersededReplacementId"), 32, "retained supersededReplacementId")
    except (ValueError, RuntimeError):
        return None
    if (replacement.get("replacementId") != replacement_id
            or replacement.get("supersededReplacementId") != superseded_id):
        return None
    return replacement_id if replacement_id != superseded_id else None


def _apply_workspace(
    host: Any, workspace: Path, status: Mapping[str, Any], *, expected_revision: int,
    expected_candidate_raw_sha256: str, expected_anchor_event_id: int,
    capture_session_id: str, project_root: Path | None = None,
) -> dict[str, Any]:
    scope = getattr(host, '_shader_artifact_replay_scope', None)
    with scope() if callable(scope) else contextlib.nullcontext():
        return _apply_workspace_binary(host, workspace, status,
            expected_revision=expected_revision,
            expected_candidate_raw_sha256=expected_candidate_raw_sha256,
            expected_anchor_event_id=expected_anchor_event_id,
            capture_session_id=capture_session_id, project_root=project_root)


def _pair_restore_rdc_hint(status: Mapping[str, Any], capture_session_id: str) -> str | None:
    """A restore-local routing hint; the Bridge remains the apply authority."""
    try:
        path = Path(status["capture"]["capturePath"])
        facts = path.stat()
        key = (capture_session_id, _path_key(path), facts.st_dev, facts.st_ino,
               facts.st_size, facts.st_mtime_ns, facts.st_ctime_ns)
        observations = getattr(_RESTORE_THREAD_STATE, "pair_rdc_observations", None)
        if observations is not None and key in observations:
            return observations[key]
        value = _verified_rdc_content_sha256(path)
        if observations is not None:
            if len(observations) >= 32:
                observations.clear()
            observations[key] = value
        return value
    except (OSError, RuntimeError, ValueError):
        return None


def _apply_workspace_binary(
    host: Any,
    workspace: Path,
    status: Mapping[str, Any],
    *,
    expected_revision: int,
    expected_candidate_raw_sha256: str,
    expected_anchor_event_id: int,
    capture_session_id: str,
    project_root: Path | None = None,
) -> dict[str, Any]:
    latest = status.get("latestCompiled") or {}
    if (latest.get("validationScope") == "manual-edit-unvalidated"
            or (latest.get("projectImport") or {}).get("manualEdit") is True):
        from .project_manual_shader import from_apply
        applied = host._shader_live_edit_apply_candidate(
            str(workspace), expected_revision, expected_candidate_raw_sha256,
            expected_anchor_event_id=expected_anchor_event_id)
        try:
            from_apply(applied, status)
            replacement = applied.get("replacement") or {}
            if (replacement.get("captureSessionId") != capture_session_id
                    or replacement.get("anchorEventId") != expected_anchor_event_id):
                raise RuntimeError("manual project Shader changed its current capture or event")
        except Exception as apply_exc:
            try:
                _recover_uncertain_workspace_apply(host, status,
                    applied.get("operationId"), capture_session_id)
            except Exception as recovery_exc:
                raise RestoreRollbackFailed(
                    "manual Shader apply identity changed and operation recovery failed: %s" % recovery_exc
                ) from apply_exc
            raise
        applied["pairValidationCache"] = {"hit": False, "written": False,
                                           "reason": "manual-edit-unvalidated"}
        return applied
    operation_id = uuid.uuid4().hex
    try:
        prior_pair = (
            _load_shader_pair_validation(project_root, status,
                getattr(_RESTORE_THREAD_STATE, "validation_policy_sha256", None),
                getattr(_RESTORE_THREAD_STATE, "compatible_policy_sha256s", ()))
            if project_root is not None else None
        )
        pair_apply = getattr(host, "_apply_project_shader_pair_restore", None)
        if prior_pair is not None and callable(pair_apply):
            # The Bridge binds these legacy receipts to the complete RDC.
            # Detect an ordinary cache miss before submitting an apply: an RDC
            # section edit otherwise produces one rejected mutation and a
            # recovery transaction for every Shader. Reuse this routing hint
            # within the restore; it never substitutes for native admission.
            payload = prior_pair.get("replacement", {}).get("restoreValidationReceipt", {}).get("payload", {})
            current_rdc = _pair_restore_rdc_hint(status, capture_session_id)
            if not current_rdc or payload.get("captureSha256") != current_rdc:
                prior_pair = None
        if prior_pair is not None and callable(pair_apply):
            try:
                wrapped = pair_apply(
                    dict(status), prior_pair, capture_session_id, operation_id,
                    expected_anchor_event_id,
                )
            except (RestoreContextChanged, RestoreContextUnavailable, RestoreRollbackFailed, RestoreControlStop):
                raise
            except Exception:
                # A stale/corrupt optional proof must not become a repeated
                # restore gate. Recover the exact attempt before falling back
                # to a newly identified ordinary validation transaction.
                _recover_uncertain_workspace_apply(host, status, operation_id, capture_session_id)
                prior_pair = None
                operation_id = uuid.uuid4().hex
        if prior_pair is None or not callable(pair_apply):
            wrapped = _tool_json(host.apply_shader_workspace_shared_resource(
                str(workspace),
                expected_revision,
                expected_candidate_raw_sha256,
                operation_id,
                expected_anchor_event_id=expected_anchor_event_id,
            ), "apply Shader workspace",
            schema="renderdoc-ultra-shader-shared-resource-apply-mcp/v1")
        checked = _validate_workspace_apply_response(
            host,
            workspace,
            status,
            wrapped,
            operation_id=operation_id,
            expected_revision=expected_revision,
            expected_candidate_raw_sha256=expected_candidate_raw_sha256,
            expected_anchor_event_id=expected_anchor_event_id,
            capture_session_id=capture_session_id,
        )
        if project_root is not None:
            try:
                current = _workspace_artifact_service(host).status(str(workspace))
                written = _persist_shader_pair_validation(project_root, current, checked)
            except Exception:
                # A derived proof cache is optional. Failure to save it must
                # never revoke a successfully checked replay replacement.
                written = False
            checked["pairValidationCache"] = {
                "hit": checked.get("validation", {}).get("reusedValidation") is True,
                "written": written,
            }
        return checked
    except RestoreRollbackFailed:
        raise
    except Exception as apply_exc:
        retained = _retained_shader_handoff_error(
            apply_exc, status, operation_id, capture_session_id, expected_revision,
            expected_candidate_raw_sha256, expected_anchor_event_id)
        if retained is not None:
            # No successful result is returned or added to applied_workspaces.
            # The per-item failure remains visible while the physically checked
            # effect survives; it must not enter uncertain-operation rollback.
            raise RestoreRetainedShaderApply(
                "Shader handoff replacement %s was retained; current source verification "
                "remains incomplete (%s)" % (retained, apply_exc)) from apply_exc
        try:
            recovery = _recover_uncertain_workspace_apply(
                host, status, operation_id, capture_session_id
            )
        except Exception as recovery_exc:
            raise RestoreRollbackFailed(
                "Shader apply response was uncertain and fail-safe recovery failed: "
                "%s: %s"
                % (type(recovery_exc).__name__, recovery_exc)
            ) from apply_exc
        raise RuntimeError(
            "Shader apply failed or returned invalid evidence; exact operation %s "
            "was fail-safe recovered with status %s (%s: %s)"
            % (
                operation_id,
                recovery.get("status"),
                type(apply_exc).__name__,
                apply_exc,
            )
        ) from apply_exc


def _adopt_exact_live_replacement(
    host: Any,
    workspace: Path,
    status: Mapping[str, Any],
    record: Mapping[str, Any],
    capture_session_id: str,
) -> dict[str, Any] | None:
    """Adopt an already-realized candidate in this exact capture session.

    This is a current-session idempotence optimisation, never persisted apply
    authority.  A replacement from an older session cannot appear in the
    Bridge response because both the request and wrapper are session-bound.
    """

    identity = _normal_shader_identity(record.get("identity", {}))
    managed = record.get("managedWorkspace")
    shader = status.get("shader")
    capture = status.get("capture")
    baseline = status.get("baseline")
    latest = status.get("latestCompiled")
    if not all(
        isinstance(value, Mapping)
        for value in (managed, shader, capture, baseline, latest)
    ):
        raise RuntimeError("live replacement adoption omitted workspace identity")
    workspace_id = _hex(
        managed.get("workspaceId"), 32, "live replacement workspaceId"
    )
    capture_id = _hex(
        capture.get("captureId"), 64, "live replacement captureId"
    )
    revision = _integer(
        record.get("candidateRevision"), "live replacement revision", 1
    )
    candidate_raw = _hex(
        record.get("candidateRawSha256"),
        64,
        "live replacement candidateRawSha256",
    )
    original_raw = _hex(
        baseline.get("rawSha256"), 64, "live replacement originalRawSha256"
    )
    physical_abi = _hex(
        baseline.get("abiSha256"), 64, "live replacement physicalAbiSha256"
    )
    if any(
        (
            identity.get("stableKeyComplete") is not True,
            identity.get("resourceId") != shader.get("resourceId"),
            identity.get("rawSha256") != original_raw,
            identity.get("abiHash") not in _workspace_physical_abi_hashes(status),
            status.get("workspaceId") != workspace_id,
            latest.get("revision") != revision,
            latest.get("rawSha256") != candidate_raw,
        )
    ):
        raise RuntimeError("live replacement adoption differs from desired Shader")
    gate = latest.get("abiGate")
    if (
        latest.get("replayAbiValidated") is not True
        or latest.get("abiCompatible") is not True
        or not isinstance(gate, Mapping)
        or gate.get("status") != "compatible"
    ):
        # A source-preserving restore may have reopened a historical candidate
        # for fresh ABI reflection.  Current-session replay state alone cannot
        # turn that pending candidate into apply authority.
        return None
    try:
        response = host.ipc.call(
            "get_shared_shader_replacements",
            {
                "expected_capture_id": capture_id,
                "expected_capture_session_id": capture_session_id,
            },
            timeout=30.0,
        )
    except Exception:
        # Compatibility with an older Bridge: the ordinary strict apply route
        # remains safe and idempotent on its own.
        return None
    replacements = (
        response.get("replacements") if isinstance(response, Mapping) else None
    )
    if (
        not isinstance(response, Mapping)
        or response.get("schema")
        != "renderdoc-ultra-shared-shader-replacement-list/v1"
        or response.get("schemaVersion") != 1
        or response.get("captureId") != capture_id
        or response.get("captureSessionId") != capture_session_id
        or response.get("originalShadersPreserved") is not True
        or response.get("replayMutation") is not False
        or not isinstance(replacements, list)
        or len(replacements) > _MAX_FAST_CATALOG_SHADERS
        or response.get("count") != len(replacements)
    ):
        # Malformed metadata cannot authorise adoption.  Fall through to the
        # public strict apply transaction instead of trusting any item.
        return None
    expected_api = _asset_api(capture.get("api"))
    expected_encoding = _asset_encoding(latest.get("kind"))
    anchor = _shader_anchor_event_id(identity)
    exact = [
        item
        for item in replacements
        if isinstance(item, Mapping)
        and item.get("workspaceId") == workspace_id
        and item.get("revision") == revision
        and item.get("candidateRawSha256") == candidate_raw
        and item.get("anchorEventId") == anchor
        and item.get("stage") == identity["stage"]
        and item.get("entryPoint", "") == identity["entryPoint"]
        and item.get("api") == expected_api
        and item.get("encoding") == expected_encoding
        and item.get("originalResourceId") == identity["resourceId"]
        and item.get("originalRawSha256") == original_raw
        and item.get("physicalAbiSha256") in _workspace_physical_abi_hashes(status)
        and item.get("resourceScoped") is True
        and item.get("eventScoped") is False
        and item.get("allOriginalResourceUsesAffected") is True
        and item.get("originalShaderPreserved") is True
    ]
    if len(exact) > 1:
        raise RuntimeError("multiple live replacements claim one desired Shader")
    if not exact:
        return None
    replacement_id = str(exact[0].get("replacementId", "") or "")
    if (
        len(replacement_id) != 32
        or any(character not in _HEX32_CHARS for character in replacement_id)
    ):
        return None
    return {
        "schema": "replaylab-project-live-replacement-adoption/v1",
        "schemaVersion": 1,
        "workspacePath": str(workspace),
        "replacement": copy.deepcopy(dict(exact[0])),
        "applied": True,
        "idempotentlyAdopted": True,
        "physicalApplyAuthority": False,
    }


def _rollback_applied_workspace(
    host: Any,
    workspace: Path,
    status: Mapping[str, Any],
    applied: Mapping[str, Any],
    capture_session_id: str,
) -> None:
    replacement = applied.get("replacement")
    if not isinstance(replacement, Mapping):
        raise RestoreRollbackFailed("applied Shader omitted replacement evidence")
    replacement_id = _hex(
        replacement.get("replacementId"), 32, "Shader rollback replacementId"
    )
    rolled_back = _tool_json(
        host.rollback_shader_workspace_shared_resource(
            str(workspace), replacement_id
        ),
        "rollback restored Shader workspace",
        schema="renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
    )
    result = rolled_back.get("replacement")
    if (
        rolled_back.get("operation") != "rollback-shared-resource"
        or rolled_back.get("workspaceId") != status.get("workspaceId")
        or rolled_back.get("captureId") != status.get("capture", {}).get("captureId")
        or rolled_back.get("applied") is not False
        or not isinstance(result, Mapping)
        or result.get("schema")
        != "renderdoc-ultra-shared-shader-replacement/v1"
        or result.get("schemaVersion") != 1
        or result.get("replacementId") != replacement_id
        or result.get("captureId") != status.get("capture", {}).get("captureId")
        or result.get("captureSessionId") != capture_session_id
        or result.get("workspaceId") != status.get("workspaceId")
        or result.get("resourceScoped") is not True
        or result.get("eventScoped") is not False
        or result.get("cleared") is not True
        or result.get("originalShaderPreserved") is not True
        or not isinstance(result.get("targetFreed"), bool)
    ):
        raise RestoreRollbackFailed(
            "RenderDoc returned invalid restored Shader rollback evidence"
        )


def _rollback_applied_workspaces(
    host: Any,
    applied_workspaces: list[
        tuple[Path, Mapping[str, Any], Mapping[str, Any], str]
    ],
) -> str:
    failures: list[str] = []
    while applied_workspaces:
        workspace, status, applied, capture_session_id = applied_workspaces.pop()
        try:
            _rollback_applied_workspace(
                host, workspace, status, applied, capture_session_id
            )
        except Exception as exc:
            failures.append(
                "%s: %s" % (type(exc).__name__, exc)
            )
    return "; ".join(failures)[:4096]


def _rollback_restored_material_sessions(
    host: Any,
    restored_sessions: list[tuple[str, str, str]],
) -> str:
    failures: list[str] = []
    while restored_sessions:
        session_id, capture_id, capture_session_id = restored_sessions.pop()
        try:
            rolled_back = _tool_json(
                host.rollback_material_session(session_id),
                "rollback restored material session",
                schema="renderdoc-ultra-material-session/v1",
                schema_version=1,
            )
            if (
                rolled_back.get("operation") != "rollback"
                or rolled_back.get("sessionId") != session_id
                or rolled_back.get("captureId") != capture_id
                or rolled_back.get("captureSessionId") != capture_session_id
                or rolled_back.get("applied") is not False
                or rolled_back.get("snapshotReleased") is not True
            ):
                raise RestoreRollbackFailed(
                    "RenderDoc returned invalid restored material rollback evidence"
                )
        except Exception as exc:
            failures.append("%s: %s" % (type(exc).__name__, exc))
        finally:
            _MATERIAL_SYNC_PARAMETERS.pop(session_id, None)
    return "; ".join(failures)[:4096]


def _normal_shader_identity(shader: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalise one resource identity without promoting EID into its key.

    V2 identities are capture-independent exact keys plus two capture-local
    attachments: ``resourceId`` and an optional representative
    ``anchorEventId``.  The legacy shape remains readable only as incomplete
    routing evidence; it can never authorise executable reuse by itself.
    """

    resource_id = _integer(shader.get("resourceId"), "shader.resourceId", 1)
    stage = _text(shader.get("stage"), "shader.stage", 32)
    entry_point = _text(
        shader.get("entryPoint", ""),
        "shader.entryPoint",
        512,
        allow_empty=True,
    )
    raw_sha256 = _hex(shader.get("rawSha256"), 64, "shader.rawSha256")
    legacy_catalog = bool(
        ("interfaceHash" in shader or "interfaceComplete" in shader)
        and not any(field in shader for field in ("abiHash", "stableKeyComplete", "stableKeySha256"))
    )
    if not legacy_catalog and any(
        field in shader
        for field in (
            "api",
            "abiHash",
            "stableKeyComplete",
            "stableKeySha256",
            "anchorEventId",
        )
    ):
        api = _asset_api(shader.get("api"))
        encoding = _asset_encoding(shader.get("encoding"))
        abi_hash = str(shader.get("abiHash", "") or "").lower()
        layout_digest = str(shader.get("layoutHash", "") or "").lower()
        for value, label in (
            (abi_hash, "shader.abiHash"),
            (layout_digest, "shader.layoutHash"),
        ):
            if value:
                _hex(value, 64, label)
        # The sealed project inventory calls this interfaceComplete; the live
        # Bridge read model calls it stableKeyComplete. Preserve an explicit
        # live false/null value, and still verify all physical key fields
        # and their supplied digest below before admitting the project form.
        complete = shader.get("stableKeyComplete", shader.get("interfaceComplete")) is True
        if complete and (not api or not encoding or not abi_hash or not layout_digest):
            raise RuntimeError(
                "complete Shader stable key omitted API, encoding, ABI, or layout"
            )
        material = {
            "api": api,
            "encoding": encoding,
            "rawSha256": raw_sha256,
            "stage": stage,
            "entryPoint": entry_point,
            "abiHash": abi_hash,
            "layoutHash": layout_digest,
        }
        stable_key = _canonical_sha256(material)
        supplied_key = str(shader.get("stableKeySha256", "") or "").lower()
        if supplied_key:
            _hex(supplied_key, 64, "shader.stableKeySha256")
            if supplied_key != stable_key:
                raise RuntimeError("Shader stable key digest does not match its fields")
        anchor = shader.get("anchorEventId")
        if anchor is not None:
            anchor = _integer(anchor, "shader.anchorEventId", 1)
        return {
            **material,
            "resourceId": resource_id,
            "stableKeyComplete": complete,
            "stableKeySha256": stable_key,
            "anchorEventId": anchor,
        }

    # Legacy v1 catalog rows already had encoding/interfaceComplete, and some
    # readers added API/anchor routing hints. These are not v2 discriminators
    # when a row still explicitly carries the old interface shape; they keep API on the capture and
    # their interfaceHash is not the physical ABI hash. Retain them only as
    # incomplete routing evidence until a checked publication/live job binds
    # the missing physical identity; explicit v2 fields above stay strict.
    code_sha256 = str(shader.get("codeSha256", "") or "")
    interface_hash = str(shader.get("interfaceHash", "") or "")
    layout_digest = str(shader.get("layoutHash", "") or "")
    for value, label in (
        (code_sha256, "shader.codeSha256"),
        (interface_hash, "shader.interfaceHash"),
        (layout_digest, "shader.layoutHash"),
    ):
        if value:
            _hex(value, 64, label)
    event_ids = sorted(
        {
            _integer(value, "shader.eventId", 1)
            for value in list(shader.get("eventIds", []) or [])
        }
    )
    legacy_anchor = shader.get("anchorEventId")
    if legacy_anchor is not None:
        legacy_anchor = _integer(legacy_anchor, "shader.anchorEventId", 1)
    if not event_ids and legacy_anchor is None:
        raise RuntimeError("legacy Shader identity has no representative event")
    legacy_material = {
        "api": "",
        "encoding": "",
        "rawSha256": raw_sha256,
        "stage": stage,
        "entryPoint": entry_point,
        "abiHash": interface_hash,
        "layoutHash": layout_digest,
    }
    return {
        **legacy_material,
        "resourceId": resource_id,
        "stableKeyComplete": False,
        "stableKeySha256": _canonical_sha256(legacy_material),
        "anchorEventId": legacy_anchor if legacy_anchor is not None else event_ids[0],
    }


def _shader_anchor_event_id(
    identity: Mapping[str, Any], *, required: bool = True
) -> int | None:
    value = identity.get("anchorEventId")
    if value is None:
        if required:
            raise RuntimeError(
                "Shader has no representative EID for live PSO/ABI validation"
            )
        return None
    return _integer(value, "shader.anchorEventId", 1)


def _shader_record_from_status(
    project_root: Path,
    identity: Mapping[str, Any],
    knowledge_id: str,
    workspace: Path,
    status: Mapping[str, Any],
    project_knowledge: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = status.get("source")
    latest = status.get("latestCompiled")
    target_profile = status.get("targetProfile")
    if (
        not isinstance(source, Mapping)
        or not isinstance(latest, Mapping)
        or not isinstance(target_profile, Mapping)
    ):
        raise RuntimeError("applied workspace omitted source/candidate state")
    relative = workspace.relative_to(project_root).as_posix()
    revision = _integer(latest.get("revision"), "candidateRevision", 1)
    candidate_sha256 = _hex(
        latest.get("rawSha256"), 64, "candidateRawSha256"
    )
    compile_config_sha256 = str(
        latest.get("compileConfigSha256", status.get("compileConfigSha256", ""))
        or ""
    )
    if compile_config_sha256:
        _hex(compile_config_sha256, 64, "compileConfigSha256")
    else:
        compile_config_sha256 = _canonical_sha256(target_profile)
    representations_sha256 = str(
        status.get("representationsSha256", "") or ""
    )
    if representations_sha256:
        _hex(representations_sha256, 64, "representationsSha256")
    aliases_sha256 = ""
    if isinstance(project_knowledge, Mapping):
        if project_knowledge.get("sourceSha256") != source.get("currentSha256"):
            raise RuntimeError(
                "project Shader artifact source differs from the applied workspace"
            )
        compiled = project_knowledge.get("compiledCandidate")
        if (
            not isinstance(compiled, Mapping)
            or compiled.get("rawSha256") != candidate_sha256
        ):
            raise RuntimeError(
                "project Shader artifact binary differs from the applied workspace"
            )
        compiler_binding = project_knowledge.get("compilerBinding")
        if isinstance(compiler_binding, Mapping):
            compile_config_sha256 = _canonical_sha256(compiler_binding)
        else:
            compile_config_sha256 = _canonical_sha256(
                project_knowledge.get("targetProfile")
            )
        representations = project_knowledge.get(
            "assemblyDisassemblyArtifacts", []
        )
        if isinstance(representations, list) and representations:
            representations_sha256 = _canonical_sha256(representations)
        alias_refs = project_knowledge.get("cbufferAliasRefs", [])
        if isinstance(alias_refs, list) and alias_refs:
            aliases_sha256 = _canonical_sha256(alias_refs)
    return {
        "identity": copy.deepcopy(dict(identity)),
        "knowledgeId": _hex(knowledge_id, 32, "workspace knowledgeId"),
        "managedWorkspace": {
            "workspaceId": _hex(status.get("workspaceId"), 32, "workspaceId"),
            "path": relative,
        },
        "candidateRevision": revision,
        "candidateRawSha256": candidate_sha256,
        "sourceSha256": _hex(
            source.get("currentSha256"), 64, "sourceSha256"
        ),
        "artifactRefs": {
            "projectArtifactId": _hex(
                knowledge_id, 32, "workspace knowledgeId"
            ),
            "revision": revision,
            "compiledSha256": candidate_sha256,
            "compileConfigSha256": compile_config_sha256,
            "representationsSha256": representations_sha256,
            "aliasesSha256": aliases_sha256,
        },
    }


def _knowledge_matches_shader(
    knowledge: Mapping[str, Any],
    identity: Mapping[str, Any],
    *,
    executable: bool = False,
) -> bool:
    if (
        knowledge.get("stage") != identity.get("stage")
        or knowledge.get("entryPoint", "") != identity.get("entryPoint", "")
        or knowledge.get("rawSha256") != identity.get("rawSha256")
    ):
        return False
    if executable:
        return bool(
            identity.get("stableKeyComplete") is True
            and knowledge.get("stableKeyComplete") is True
            and knowledge.get("stableKeySha256")
            == identity.get("stableKeySha256")
        )
    # Names, aliases and typed CBuffer knowledge are non-executable, but they
    # still require the same original program plus exact ABI/layout evidence.
    knowledge_abi = str(
        knowledge.get("abiHash", knowledge.get("baselineAbiSha256", "")) or ""
    )
    identity_abi = str(identity.get("abiHash", "") or "")
    return bool(
        knowledge.get("layoutHash", "") == identity.get("layoutHash", "")
        and (not knowledge_abi or not identity_abi or knowledge_abi == identity_abi)
    )


def _source_preparation_matches_shader(
    knowledge: Mapping[str, Any], identity: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    """A raw-exact source match is not a binary or replay permission."""
    return bool(
        candidate.get("sourcePreparationOnly") is True
        and candidate.get("sourceSeedEligible") is True
        and candidate.get("matchMode") == "raw-exact"
        and candidate.get("binaryCandidateEligible") is False
        and candidate.get("binaryCandidate") is None
        and candidate.get("knowledgeId") == knowledge.get("knowledgeId")
        and candidate.get("sourceSha256") == knowledge.get("sourceSha256")
        and identity.get("anchorEventId") is not None
        # Source lookup uses the original program, not the old reflection
        # snapshot. The prepared target receipt and live apply below validate
        # the current ABI; a changed reflection/layout digest is not a reason
        # to discard source belonging to exactly the same original bytes.
        and all(knowledge.get(key) == identity.get(key) and identity.get(key)
                for key in ("api", "encoding", "rawSha256", "stage"))
        and knowledge.get("entryPoint", "") == identity.get("entryPoint", "")
    )


class RestoreProjectSourceRefresh(RuntimeError):
    """Current project bytes need a fresh authenticated managed import receipt."""


class RestoreLocalEdit(RuntimeError):
    """The target workspace holds a source that is not the project's knowledge.

    A person's edit (or an earlier revision they kept) lives there; the project
    must not overwrite it. This is a stable outcome, not a failure: it is
    remembered so the next open of the same capture does not spend minutes
    rediscovering it.
    """

    def __init__(self, message: str, workspace: Path | None,
                 workspace_source_sha256: str) -> None:
        super().__init__(message)
        self.workspace = workspace
        self.workspace_source_sha256 = workspace_source_sha256


def _local_edit_memo_hit(project: Path, capture_path: str | Path,
                         replay: Any) -> dict[str, Any] | None:
    """Reuse last time's answer when it was "your local edits were kept".

    A restore whose every matched Shader was skipped for a local edit has
    nothing to apply, and the two-minute inventory/preparation would only
    rediscover that. The previous record is trusted when nothing it depended
    on has moved: the project's restorable heads, each knowledge source, and
    each workspace's current source bytes.
    """

    try:
        from .rdc_shader_inventory_cache import capture_path_sha256

        record_path = (project / ".replaylab" / "replay-project" / "derived"
                       / "restore-results" / (capture_path_sha256(capture_path) + ".json"))
        if not record_path.is_file():
            return None
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record.get("status") != "finished" or record.get("error"):
            return None
        shaders = record.get("shaders") or {}
        items = [item for item in shaders.get("items") or [] if isinstance(item, Mapping)]
        if not items or shaders.get("applied") or shaders.get("failed"):
            return None
        if any(item.get("status") != "skipped" or item.get("reason") != "local-edit"
               for item in items):
            return None
        lookup = record.get("targetLookup") or {}
        recorded_targets = sorted(str(value) for value in lookup.get("targetRawSha256s") or [])
        if not recorded_targets or recorded_targets != sorted(replay.restorable_shader_raw_hashes()):
            return None
        for item in items:
            knowledge = replay.get_workspace_knowledge(str(item.get("knowledgeId") or ""))
            if str(knowledge.get("sourceSha256") or "") != str(item.get("knowledgeSourceSha256") or ""):
                return None
            workspace = Path(str(item.get("workspacePath") or ""))
            language = knowledge.get("sourceFormat", "hlsl")
            if language not in {"hlsl", "glsl"}:
                return None
            source = workspace / "source" / ("shader." + language)
            if not item.get("workspaceSourceSha256") or not source.is_file():
                return None
            if hashlib.sha256(source.read_bytes()).hexdigest() != item.get("workspaceSourceSha256"):
                return None
        return {"items": items, "lookup": lookup, "recordedAt": record.get("finishedAt")}
    except Exception:
        return None


def _validate_prepared_project_source(
    status: Mapping[str, Any], knowledge: Mapping[str, Any],
    identity: Mapping[str, Any], binding_state: Mapping[str, Any],
    *, source_preparation_only: bool = False,
) -> None:
    """Recheck imported provenance and the current active project's source bytes."""
    source = status.get("source") or {}
    origin = source.get("projectSource") or {}
    # A toolchain refresh keeps the original verified import receipt in its
    # validated source-provenance chain. Later edits published from the same
    # registered workspace remain legitimate current heads after that refresh.
    refresh = source.get("toolchainRefresh") or {}
    provenance = refresh.get("sourceProvenance") or {}
    project_origin = source.get("origin") == "verified-project-source" or (
        source.get("origin") == "toolchain-refreshed-source"
        and provenance.get("origin") == "verified-project-source"
        and provenance.get("projectSource") == origin
        and provenance.get("sha256") == origin.get("sourceSha256")
        and refresh.get("preservedSourceSha256") == origin.get("sourceSha256"))
    baseline = status.get("baseline") or {}
    registered = knowledge.get("registeredWorkspace") or {}
    current_sha = source.get("currentSha256") or source.get("sha256")
    expected_abi = identity.get("abiHash")
    if (baseline.get("abiSha256") != expected_abi
            and baseline.get("compiledInterface") is not None
            and expected_abi in _workspace_physical_abi_hashes(status)):
        # Imported origin receipts authenticate the workspace's immutable old
        # baseline. A freshly observed equivalent inventory hash does not
        # rewrite that provenance; all receipt checks below remain exact.
        expected_abi = baseline["abiSha256"]
    if (source_preparation_only
            and binding_state.get("capture", {}).get("inventoryOnly") is True
            and identity.get("stableKeyComplete") is False
            and baseline.get("abiSha256") != expected_abi):
        # Incomplete inventories (notably Vulkan module-only reflection) use
        # an interface summary, not the workspace's physical ABI hash schema.
        # Source import binds the saved text to the authenticated current
        # workspace baseline. Historical knowledge can use another reflection
        # schema even for the exact same original bytes; it is not the new
        # import receipt's ABI authority. Verify the current physical evidence
        # when present, and keep all receipt/raw/profile checks below exact.
        # This is never a binary/apply grant: the caller still validates the
        # compiled candidate against the current live pipeline ABI.
        _workspace_physical_abi_hashes(status)
        expected_abi = baseline["abiSha256"]
    if (source.get("origin") != "verified-project-source"
            and source.get("projectSource") is None):
        # A locally decompiled/edited workspace never imported project source,
        # so it has no import receipt. Its status has already verified the
        # actual source bytes and captured baseline. Equal project bytes can
        # be reused under that current target evidence without inventing an
        # import receipt or inheriting an old binary/ABI grant.
        shader = status.get("shader") or {}
        mismatches = []
        if (source.get("present") is not True
                or source.get("lane") != "executable-editable"
                or source.get("format") not in {"hlsl", "glsl"}
                or source.get("format") != knowledge.get("sourceFormat", "hlsl")):
            mismatches.append("source")
        if (shader.get("rawSha256") != identity.get("rawSha256")
                or baseline.get("rawSha256") != identity.get("rawSha256")
                or knowledge.get("rawSha256") != identity.get("rawSha256")
                or not identity.get("rawSha256")):
            mismatches.append("originalRawSha256")
        if (baseline.get("abiSha256") != expected_abi or not expected_abi):
            mismatches.append("baseline.abiSha256")
        if _asset_encoding(baseline.get("kind")) != identity.get("encoding"):
            mismatches.append("encoding")
        if _asset_api((status.get("capture") or {}).get("api")) != identity.get("api"):
            mismatches.append("api")
        for field in ("stage", "entryPoint"):
            if (status.get(field, "") != identity.get(field, "")
                    or knowledge.get(field, "") != identity.get(field, "")):
                mismatches.append(field)
        if (not isinstance(status.get("targetProfile"), Mapping)
                or status.get("targetProfile") != knowledge.get("targetProfile")):
            mismatches.append("targetProfile")
        if mismatches:
            raise RuntimeError(
                "existing workspace source differs from the current original Shader "
                "or active knowledge (%s)" % ", ".join(mismatches)
            )
        _hex(current_sha, 64, "existing workspace sourceSha256")
        _hex(knowledge.get("sourceSha256"), 64, "active project sourceSha256")
        if current_sha != knowledge.get("sourceSha256"):
            raise RestoreLocalEdit(
                "workspace source differs from the project knowledge (a local edit or "
                "an earlier revision); it was kept, not overwritten",
                Path(str(status.get("workspacePath") or "")) if status.get("workspacePath") else None,
                str(current_sha),
            )
        return
    # projectSource authenticates the immutable imported input, not every
    # later edit. An edited source published from this same workspace has a
    # new head while its original import receipt intentionally stays unchanged.
    published_revision = bool(
        isinstance(origin, Mapping) and isinstance(registered, Mapping)
        and project_origin
        and current_sha == knowledge.get("sourceSha256")
        and origin.get("knowledgeId") != knowledge.get("knowledgeId")
        and origin.get("sourceSha256") != knowledge.get("sourceSha256")
        and origin.get("sourceSha256")
        and (
            source.get("decompiledSha256") == origin.get("sourceSha256")
            or source.get("sha256") == origin.get("sourceSha256")
        )
        and registered.get("workspaceId")
        and registered.get("workspaceId") == status.get("workspaceId")
        and registered.get("path") and status.get("workspacePath")
        and _path_key(registered["path"]) == _path_key(status["workspacePath"])
    )
    # Same organized bytes re-registered under a newer knowledgeId keep the
    # original import receipt. The workspace already matches the active head,
    # so restore must accept the rebind instead of failing closed.
    same_content_knowledge_rebind = bool(
        isinstance(origin, Mapping)
        and project_origin
        and current_sha
        and current_sha == knowledge.get("sourceSha256")
        and origin.get("sourceSha256") == knowledge.get("sourceSha256")
        and origin.get("knowledgeId")
        and origin.get("knowledgeId") != knowledge.get("knowledgeId")
        and origin.get("originalRawSha256") == identity.get("rawSha256")
        and origin.get("baselineAbiSha256") == expected_abi
        and origin.get("stage") == identity.get("stage")
        and origin.get("entryPoint", "") == identity.get("entryPoint", "")
        and origin.get("projectId") == binding_state.get("projectId")
    )
    mismatches: list[str] = []
    if not isinstance(origin, Mapping):
        mismatches.append("projectSource")
    elif origin.get("schema") != "replaylab-verified-project-shader-source/v1":
        mismatches.append("projectSource.schema")
    if origin.get("projectId") != binding_state.get("projectId"):
        mismatches.append("projectId")
    if _path_key(origin.get("projectPath", "")) != _path_key(
        binding_state.get("projectPath", "")
    ):
        mismatches.append("projectPath")
    if not published_revision and not same_content_knowledge_rebind:
        if origin.get("knowledgeId") != knowledge.get("knowledgeId"):
            mismatches.append(
                "knowledgeId(origin=%s,active=%s)"
                % (origin.get("knowledgeId"), knowledge.get("knowledgeId"))
            )
        if origin.get("sourceSha256") != knowledge.get("sourceSha256"):
            mismatches.append("projectSource.sourceSha256")
    if current_sha != knowledge.get("sourceSha256"):
        mismatches.append("currentSha256")
    if source.get("format") != knowledge.get("sourceFormat", "hlsl"):
        mismatches.append("source.format")
    if origin.get("sourceFormat", "hlsl") != knowledge.get("sourceFormat", "hlsl"):
        mismatches.append("projectSource.sourceFormat")
    if origin.get("originalRawSha256") != identity.get("rawSha256"):
        mismatches.append("originalRawSha256")
    if (baseline.get("rawSha256") != identity.get("rawSha256")
            or (status.get("shader") or {}).get("rawSha256") != identity.get("rawSha256")
            or knowledge.get("rawSha256") != identity.get("rawSha256")):
        mismatches.append("baseline.rawSha256")
    if origin.get("baselineAbiSha256") != expected_abi:
        mismatches.append("baselineAbiSha256")
    if baseline.get("abiSha256") != expected_abi:
        mismatches.append("baseline.abiSha256")
    if _asset_encoding(baseline.get("kind")) != identity.get("encoding"):
        mismatches.append("encoding")
    if _asset_api((status.get("capture") or {}).get("api")) != identity.get("api"):
        mismatches.append("api")
    if origin.get("stage") != identity.get("stage"):
        mismatches.append("stage")
    if origin.get("entryPoint", "") != identity.get("entryPoint", ""):
        mismatches.append("entryPoint")
    if origin.get("targetProfile") != knowledge.get("targetProfile"):
        mismatches.append("projectSource.targetProfile")
    if status.get("targetProfile") != knowledge.get("targetProfile"):
        mismatches.append("status.targetProfile")
    if origin.get("applied") is not False:
        mismatches.append("applied")
    if origin.get("requiresCompile") is not True:
        mismatches.append("requiresCompile")
    if origin.get("requiresLiveAbiRevalidation") is not True:
        mismatches.append("requiresLiveAbiRevalidation")
    if mismatches:
        if (project_origin
                and current_sha == knowledge.get("sourceSha256")
                and origin.get("sourceSha256")
                and origin.get("sourceSha256") in (
                    source.get("decompiledSha256"), source.get("sha256"))
                and all(value == "projectSource.sourceSha256"
                        or value.startswith("knowledgeId(") for value in mismatches)):
            # The original managed input is still valid, but a previous seed
            # copied a newer project head without renewing that input/receipt.
            # Require a real verified seed; do not waive this validation.
            raise RestoreProjectSourceRefresh("project source import receipt needs the current head")
        if (
            current_sha
            and current_sha != knowledge.get("sourceSha256")
        ):
            raise RestoreLocalEdit(
                "workspace source differs from the project knowledge (a local edit or "
                "an earlier revision); it was kept, not overwritten",
                Path(str(status.get("workspacePath") or "")) if status.get("workspacePath") else None,
                str(current_sha or ""))
        raise RuntimeError(
            "prepared project source differs from the current original Shader or "
            "active knowledge (%s)" % ", ".join(mismatches[:8])
        )

def _restore_material(
    host: Any,
    replay: ReplayProjectService,
    binding_state: Mapping[str, Any],
    project_workspace_id: str,
    identity: Mapping[str, Any],
    material_record: Mapping[str, Any],
    capture_session_id: str,
    validate_context: Callable[[], Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    knowledge = binding_state.get("materials", {}).get(
        str(material_record.get("knowledgeId", ""))
    )
    if not isinstance(knowledge, Mapping):
        raise RuntimeError("RDC material association references absent project knowledge")
    if not _knowledge_matches_shader(knowledge, identity):
        raise RuntimeError("material knowledge no longer matches the current Shader")
    if any(
        (
            material_record.get("shaderResourceId") != identity.get("resourceId"),
            material_record.get("stage") != identity.get("stage"),
            material_record.get("path") != knowledge.get("constantBlockPath"),
            material_record.get("layoutSha256")
            != knowledge.get("constantBlockLayoutSha256"),
        )
    ):
        raise RuntimeError("RDC material association differs from project knowledge")
    event_id = _integer(material_record.get("eventId"), "material.eventId", 1)
    # Material snapshots are event-scoped by design. They need not reuse the
    # representative Shader EID, but the live session below must prove that
    # this exact event binds the indexed physical Shader and CBuffer layout.
    block_index = _integer(material_record.get("blockIndex"), "material.blockIndex")
    session = _tool_json(
        host.prepare_material_session(
            event_id,
            block_index,
            str(identity["stage"]),
            str(binding_state["captureId"]),
        ),
        "prepare material session",
        schema="renderdoc-ultra-material-session/v1",
        schema_version=1,
    )
    session_id = _hex(session.get("sessionId"), 32, "material sessionId")
    try:
        if any(
            (
                session.get("captureId") != binding_state.get("captureId"),
                session.get("captureSessionId") != capture_session_id,
                session.get("eventId") != event_id,
                session.get("stage") != identity.get("stage"),
                session.get("constantBlockIndex") != block_index,
                session.get("constantBlockPath") != knowledge.get("constantBlockPath"),
                session.get("layoutSha256")
                != knowledge.get("constantBlockLayoutSha256"),
                session.get("shaderResourceId") != identity.get("resourceId"),
                not isinstance(session.get("parameters"), list),
            )
        ):
            raise RuntimeError("live material session changed block/layout identity")
        if validate_context is not None:
            validate_context()
        indexed_parameter_ids = {
            str(item.get("parameterId", "") or "")
            for item in list(material_record.get("parameters", []) or [])
            if isinstance(item, Mapping)
        }
        knowledge_parameters = list(knowledge.get("parameters", []) or [])
        if indexed_parameter_ids and indexed_parameter_ids != {
            str(item.get("parameterId", "") or "") for item in knowledge_parameters
        }:
            raise RuntimeError("RDC material parameter set differs from project knowledge")
        live_parameters = list(session["parameters"])
        updates = []
        batch_updates = []
        for saved in knowledge_parameters:
            matches = [
                item
                for item in live_parameters
                if isinstance(item, Mapping)
                and item.get("path") == saved.get("path")
                and item.get("baseType") == saved.get("baseType")
                and item.get("components") == saved.get("components")
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    "material parameter %s has no unique live typed match"
                    % saved.get("path")
                )
            live_parameter_id = _hex(
                matches[0].get("parameterId"), 32, "live material parameterId"
            )
            batch_updates.append(
                {
                    "parameter": live_parameter_id,
                    "value": copy.deepcopy(saved.get("value")),
                }
            )
            updates.append(
                {
                    "parameterId": saved["parameterId"],
                    "liveParameterId": live_parameter_id,
                    "path": saved["path"],
                }
            )
        if batch_updates:
            updated = _tool_json(
                host.update_material_parameters(session_id, batch_updates),
                "update material parameters",
                schema="renderdoc-ultra-material-session/v1",
                schema_version=1,
            )
            expected_live_ids = [item["liveParameterId"] for item in updates]
            if any(
                (
                    updated.get("operation") != "update-many",
                    updated.get("sessionId") != session_id,
                    updated.get("captureId") != binding_state.get("captureId"),
                    updated.get("captureSessionId") != capture_session_id,
                    updated.get("layoutSha256")
                    != knowledge.get("constantBlockLayoutSha256"),
                    updated.get("updatedParameterIds") != expected_live_ids,
                    updated.get("updatedParameterCount") != len(expected_live_ids),
                    updated.get("applied") is not True,
                )
            ):
                raise RuntimeError("material batch update returned invalid replay evidence")
            if validate_context is not None:
                validate_context()
        _remember_material_sync_parameters(
            session_id,
            session,
            str(binding_state["projectId"]),
            project_workspace_id,
            [str(item["liveParameterId"]) for item in updates],
        )
        clean_index_record = {
            "knowledgeId": knowledge["knowledgeId"],
            "shaderResourceId": identity["resourceId"],
            "stage": identity["stage"],
            "blockIndex": block_index,
            "path": knowledge["constantBlockPath"],
            "layoutSha256": knowledge["constantBlockLayoutSha256"],
            "eventId": event_id,
            "parameters": [
                {"parameterId": item["parameterId"]}
                for item in knowledge_parameters
            ],
        }
        return (
            {
                "status": "restored",
                "sessionId": session_id,
                "knowledgeId": knowledge["knowledgeId"],
                "eventId": event_id,
                "stage": identity["stage"],
                "blockIndex": block_index,
                "constantBlockPath": knowledge["constantBlockPath"],
                "parameterCount": len(updates),
                "updates": updates,
            },
            clean_index_record,
        )
    except Exception:
        try:
            _tool_json(
                host.rollback_material_session(session_id),
                "rollback material session",
                schema="renderdoc-ultra-material-session/v1",
                schema_version=1,
            )
        except Exception:
            pass
        _MATERIAL_SYNC_PARAMETERS.pop(session_id, None)
        raise


def _write_index_best_effort(
    host: Any,
    base_index: Mapping[str, Any] | None,
    binding: Mapping[str, Any],
    capture_path: Path,
    capture_session_id: str,
) -> tuple[bool, str, dict[str, Any] | None]:
    try:
        index = upsert_project_binding(base_index or new_index(), binding)
        # Validate/canonicalise before crossing into the Bridge.
        index = parse_index(encode_index(index))
        if base_index is not None:
            canonical_base = parse_index(encode_index(base_index))
            if canonical_base == index:
                # Avoid rewriting the RDC section when reopening an unchanged
                # project. Besides saving I/O, this preserves the cheap file
                # observation used by the disposable Shader-inventory sidecar.
                return True, "", index
        response = host.ipc.call(
            "write_rdc_project_index",
            {
                "expected_capture_path": str(capture_path),
                "expected_capture_session_id": capture_session_id,
                "document": index,
            },
            timeout=60.0,
        )
        if (
            not isinstance(response, dict)
            or response.get("schema")
            != "replaylab-rdc-project-index-transport/v1"
            or response.get("schemaVersion") != 1
            or response.get("captureSessionId") != capture_session_id
            or _path_key(response.get("capturePath", "")) != _path_key(capture_path)
            or response.get("present") is not True
            or response.get("written") is not True
            or response.get("physicalApplyAuthority") is not False
            or response.get("document") != index
        ):
            raise RuntimeError("RenderDoc returned invalid RDC index write evidence")
        return True, "", index
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc), None


def _binding_reference_components(
    shader_records: Sequence[Mapping[str, Any]],
    material_records: Sequence[Mapping[str, Any]],
) -> tuple[str, str]:
    """Hash only project heads referenced by this RDC association.

    The project-wide active revision is audit metadata.  It must not evict an
    otherwise exact RDC association when an unrelated project artifact is
    published, so the executable head is derived from the records actually
    named by this binding.
    """

    knowledge_ids = {
        "workspaces": sorted(
            {
                _hex(item.get("knowledgeId"), 32, "workspace knowledgeId")
                for item in shader_records
            }
        ),
        "aliases": [],
        "materials": sorted(
            {
                _hex(item.get("knowledgeId"), 32, "material knowledgeId")
                for item in material_records
            }
        ),
    }
    artifact_refs: list[dict[str, Any]] = []
    for item in shader_records:
        refs = item.get("artifactRefs")
        if not isinstance(refs, Mapping):
            raise RuntimeError("Shader binding omitted project artifact heads")
        artifact_refs.append(
            {
                "knowledgeId": _hex(
                    item.get("knowledgeId"), 32, "workspace knowledgeId"
                ),
                "artifactRefs": copy.deepcopy(dict(refs)),
            }
        )
    artifact_refs.sort(
        key=lambda item: (
            item["knowledgeId"],
            _canonical_sha256(item["artifactRefs"]),
        )
    )
    return _canonical_sha256(knowledge_ids), _canonical_sha256(artifact_refs)


def _binding_active_knowledge_revision(
    binding_state: Mapping[str, Any],
    previous_head: Mapping[str, Any] | None = None,
) -> int:
    raw_revision: Any = binding_state.get("activeKnowledgeRevision")
    inventory_binding = binding_state.get("inventoryBinding")
    if isinstance(inventory_binding, Mapping):
        raw_revision = inventory_binding.get(
            "activeKnowledgeRevision", raw_revision
        )
    if raw_revision is None and isinstance(previous_head, Mapping):
        raw_revision = previous_head.get("activeKnowledgeRevision")
    if raw_revision is None:
        return 0
    return _integer(raw_revision, "activeKnowledgeRevision")


def _desired_project_head(
    binding_state: Mapping[str, Any],
    shader_records: Sequence[Mapping[str, Any]],
    material_records: Sequence[Mapping[str, Any]],
    *,
    previous_head: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    derived_knowledge_ids_sha256, derived_artifact_refs_sha256 = (
        _binding_reference_components(shader_records, material_records)
    )
    knowledge_ids_sha256 = derived_knowledge_ids_sha256
    artifact_refs_sha256 = derived_artifact_refs_sha256
    inventory_binding = binding_state.get("inventoryBinding")
    if isinstance(inventory_binding, Mapping):
        knowledge_ids_sha256 = _hex(
            inventory_binding.get("knowledgeIdsSha256"),
            64,
            "inventoryBinding.knowledgeIdsSha256",
        )
        artifact_refs_sha256 = _hex(
            inventory_binding.get("artifactRefsSha256"),
            64,
            "inventoryBinding.artifactRefsSha256",
        )
    elif isinstance(previous_head, Mapping) and previous_head.get("complete") is True:
        # Full-catalog restore snapshots predate these two explicit hashes.
        # Preserve the last codec-verified values and independently recompute
        # referencedHeadsSha256 below; knowledgeContentSha256 plus strict
        # per-workspace validation still proves currentness.
        knowledge_ids_sha256 = _hex(
            previous_head.get("knowledgeIdsSha256"),
            64,
            "desiredProjectHead.knowledgeIdsSha256",
        )
        artifact_refs_sha256 = _hex(
            previous_head.get("artifactRefsSha256"),
            64,
            "desiredProjectHead.artifactRefsSha256",
        )
    referenced_sha256 = _rdc_project_index.referenced_heads_sha256(
        shader_records, material_records
    )
    return _rdc_project_index.build_desired_project_head(
        active_knowledge_revision=_binding_active_knowledge_revision(
            binding_state, previous_head
        ),
        knowledge_content_sha256=_hex(
            binding_state.get("knowledgeContentSha256"),
            64,
            "knowledgeContentSha256",
        ),
        knowledge_ids_sha256=knowledge_ids_sha256,
        artifact_refs_sha256=artifact_refs_sha256,
        referenced_heads_sha256=referenced_sha256,
        complete=True,
    )


def _realized_receipt_from_record(
    record: Mapping[str, Any],
    physical_abi_sha256: str,
    *,
    abi_report_sha256: str | None = None,
) -> dict[str, Any]:
    managed = record.get("managedWorkspace")
    if not isinstance(managed, Mapping):
        raise RuntimeError("Shader receipt omitted its managed workspace")
    return _rdc_project_index.build_realized_apply_receipt(
        _normal_shader_identity(record.get("identity", {})),
        _hex(managed.get("workspaceId"), 32, "receipt workspaceId"),
        _integer(record.get("candidateRevision"), "receipt candidateRevision", 1),
        _hex(
            record.get("candidateRawSha256"),
            64,
            "receipt candidateRawSha256",
        ),
        _hex(physical_abi_sha256, 64, "receipt physicalAbiSha256"),
        abi_report_sha256=(
            _hex(abi_report_sha256, 64, "receipt abiReportSha256")
            if abi_report_sha256
            else None
        ),
    )


def _realized_identity_abi(
    record: Mapping[str, Any], replacement: Mapping[str, Any],
    status: Mapping[str, Any] | None,
) -> str:
    """Project an already validated live alias onto the desired cache identity.

    This is a derived project receipt, not a rewritten Bridge observation or
    signed proof. Keep the downstream record schema and exact hash checks.
    """
    live = _hex(replacement.get("physicalAbiSha256"), 64, "live physical ABI")
    identity = _normal_shader_identity(record.get("identity", {}))
    desired = identity["abiHash"]
    if live == desired:
        return live
    if status is None:
        raise RuntimeError("realized Shader ABI alias lacks verified workspace evidence")
    allowed = _workspace_physical_abi_hashes(status)
    baseline, shader = status.get("baseline") or {}, status.get("shader") or {}
    latest, managed = status.get("latestCompiled") or {}, record.get("managedWorkspace") or {}
    expected = {
        "api": _asset_api(status.get("capture", {}).get("api")),
        "stage": status.get("stage"), "entryPoint": status.get("entryPoint", ""),
        "encoding": _asset_encoding(latest.get("kind")),
        "originalResourceId": shader.get("resourceId"),
        "originalRawSha256": baseline.get("rawSha256"),
        "candidateRawSha256": latest.get("rawSha256"),
        "workspaceId": status.get("workspaceId"), "revision": latest.get("revision"),
    }
    if (live not in allowed or desired not in allowed
            or any(replacement.get(key) != value for key, value in expected.items())
            or any(identity.get(key) != expected[key] for key in ("api", "stage", "entryPoint", "encoding"))
            or identity.get("resourceId") != expected["originalResourceId"]
            or identity.get("rawSha256") != expected["originalRawSha256"]
            or record.get("candidateRevision") != expected["revision"]
            or record.get("candidateRawSha256") != expected["candidateRawSha256"]
            or managed.get("workspaceId") != expected["workspaceId"]):
        raise RuntimeError("realized Shader ABI alias differs from verified desired identity")
    return desired


def _realized_receipt_from_apply(
    record: Mapping[str, Any], applied: Mapping[str, Any],
    status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    replacement = applied.get("replacement")
    if not isinstance(replacement, Mapping):
        raise RuntimeError("Shader apply omitted realized replacement evidence")
    if applied.get("validation", {}).get("manualEdit") is True:
        # This is a successful transaction observation, never a realized ABI
        # proof. Manual records are excluded from the strict RDC index below.
        return {"manualEdit": True, "validationScope": "manual-edit-unvalidated",
                "identity": copy.deepcopy(record["identity"]),
                "replacement": copy.deepcopy(dict(replacement))}
    return _realized_receipt_from_record(
        record,
        _realized_identity_abi(record, replacement, status),
        abi_report_sha256=str(replacement.get("abiReportSha256", "") or "")
        or None,
    )


def _normalised_receipts_for_records(
    shader_records: Sequence[Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if len(receipts) != len(shader_records):
        raise RuntimeError("realized Shader receipt coverage is incomplete")
    expected: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in shader_records:
        identity = _normal_shader_identity(record.get("identity", {}))
        managed = record.get("managedWorkspace")
        if not isinstance(managed, Mapping):
            raise RuntimeError("Shader receipt omitted its managed workspace")
        key = (
            identity["stableKeySha256"],
            identity["resourceId"],
            _hex(managed.get("workspaceId"), 32, "receipt workspaceId"),
            _integer(record.get("candidateRevision"), "receipt candidateRevision", 1),
            _hex(
                record.get("candidateRawSha256"),
                64,
                "receipt candidateRawSha256",
            ),
        )
        if key in expected:
            raise RuntimeError("realized Shader receipt target is ambiguous")
        expected[key] = dict(record)

    clean: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for raw_receipt in receipts:
        if not isinstance(raw_receipt, Mapping):
            raise RuntimeError("realized Shader receipt is malformed")
        key = (
            raw_receipt.get("stableKeySha256"),
            raw_receipt.get("originalResourceId"),
            raw_receipt.get("workspaceId"),
            raw_receipt.get("candidateRevision"),
            raw_receipt.get("candidateRawSha256"),
        )
        record = expected.get(key)
        if record is None or key in seen:
            raise RuntimeError("realized Shader receipt differs from desired binding")
        abi_report = str(raw_receipt.get("abiReportSha256", "") or "") or None
        rebuilt = _realized_receipt_from_record(
            record,
            str(raw_receipt.get("physicalAbiSha256", "")),
            abi_report_sha256=abi_report,
        )
        if rebuilt != dict(raw_receipt):
            raise RuntimeError("realized Shader receipt seal or policy is invalid")
        identity = _normal_shader_identity(record["identity"])
        if rebuilt["physicalAbiSha256"] != identity["abiHash"]:
            raise RuntimeError("realized Shader receipt ABI differs from desired identity")
        seen.add(key)
        clean.append(rebuilt)
    clean.sort(
        key=lambda item: (
            item["originalResourceId"],
            item["stage"],
            item["entryPoint"],
            item["workspaceId"],
        )
    )
    return clean


def _validate_cached_binding_accelerator(
    binding: Mapping[str, Any],
    binding_state: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    desired = binding.get("desiredProjectHead")
    realized = binding.get("realizedApplySnapshot")
    shaders = list(binding.get("shaders", []) or [])
    materials = list(binding.get("materials", []) or [])
    if (
        not isinstance(desired, Mapping)
        or desired.get("complete") is not True
        or not isinstance(realized, Mapping)
        or realized.get("coverage") != "complete"
        or realized.get("desiredHeadSha256") != desired.get("headSha256")
        or not isinstance(realized.get("receipts"), list)
    ):
        raise RuntimeError("RDC project binding has no complete realized head")
    current = _desired_project_head(
        binding_state, shaders, materials, previous_head=desired
    )
    # activeKnowledgeRevision is audit-only and is deliberately excluded from
    # headSha256 by the codec.  Referenced heads and immutable artifact hashes
    # remain exact.
    if current.get("headSha256") != desired.get("headSha256"):
        raise RuntimeError("RDC desired project head is stale")
    clean_receipts = _normalised_receipts_for_records(
        shaders, list(realized["receipts"])
    )
    return current, clean_receipts


def _revalidate_binding_state_before_commit(
    replay: ReplayProjectService,
    binding_state: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    """CAS-check the referenced project head immediately before RDC write."""

    if binding_state.get("capture", {}).get("inventoryOnly") is True:
        project_inventory = binding_state.get("projectInventory")
        inventory_binding = binding_state.get("inventoryBinding")
        if not isinstance(project_inventory, Mapping) or not isinstance(
            inventory_binding, Mapping
        ):
            raise RuntimeError("inventory project head cannot be revalidated")
        snapshot = replay.validate_shader_inventory_snapshot(
            project_inventory, inventory_binding
        )
        refreshed = _inventory_binding_state(
            snapshot,
            {
                "shaders": copy.deepcopy(
                    list(binding_state.get("capture", {}).get("shaders", []) or [])
                )
            },
        )
    else:
        refreshed = replay.restore_knowledge_snapshot(
            str(binding.get("captureId", "")),
            str(binding.get("catalogSha256", "")),
        )
    for field in (
        "projectId",
        "scopeSha256",
        "captureId",
        "catalogSha256",
        "knowledgeContentSha256",
        "matcherPolicySha256",
    ):
        if refreshed.get(field) != binding.get(field):
            raise RuntimeError(
                "project referenced head changed before RDC index commit"
            )
    expected_head = _desired_project_head(
        refreshed,
        list(binding.get("shaders", []) or []),
        list(binding.get("materials", []) or []),
        previous_head=(
            binding.get("desiredProjectHead")
            if isinstance(binding.get("desiredProjectHead"), Mapping)
            else None
        ),
    )
    desired_head = binding.get("desiredProjectHead")
    if (
        not isinstance(desired_head, Mapping)
        or expected_head.get("headSha256") != desired_head.get("headSha256")
    ):
        raise RuntimeError("project desired head changed before RDC index commit")
    return refreshed


def _binding_document(
    active: Mapping[str, Any],
    binding_state: Mapping[str, Any],
    shader_records: Sequence[Mapping[str, Any]],
    material_records: Sequence[Mapping[str, Any]],
    realized_receipts: Sequence[Mapping[str, Any]] = (),
    previous_desired_head: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    # A replay replacement is resource-wide, not entry-point-wide.  Never
    # persist two executable records that could make a later cache hit apply
    # the same physical Shader resource twice (or select an arbitrary entry
    # point).  Multi-entry-point metadata remains in the project inventory;
    # only the one validated executable association is eligible for the RDC
    # cache.
    seen_resource_ids: set[int] = set()
    for item in shader_records:
        if not isinstance(item, Mapping) or not isinstance(
            item.get("identity"), Mapping
        ):
            raise RuntimeError("Shader binding record is malformed")
        resource_id = _normal_shader_identity(item["identity"])["resourceId"]
        if resource_id in seen_resource_ids:
            raise RuntimeError(
                "RDC index cannot map one physical Shader resource more than once"
            )
        seen_resource_ids.add(resource_id)

    shader_keys = {
        (item["identity"]["resourceId"], item["identity"]["stage"])
        for item in shader_records
    }
    materials = [
        copy.deepcopy(dict(item))
        for item in material_records
        if (item.get("shaderResourceId"), item.get("stage")) in shader_keys
    ]
    desired_binding = {
        "projectId": binding_state["projectId"],
        "workspaceId": active["workspaceId"],
        "scopeSha256": binding_state["scopeSha256"],
        "captureId": binding_state["captureId"],
        "catalogSha256": binding_state["catalogSha256"],
        "knowledgeContentSha256": binding_state["knowledgeContentSha256"],
        "matcherPolicySha256": binding_state["matcherPolicySha256"],
        # Local editor hints are carried by the derived restore snapshot, not
        # the stable RDC executable-index schema or its authority hashes.
        "shaders": [{key: copy.deepcopy(value) for key, value in item.items()
                     if key != "preservedLocalSource"} for item in shader_records],
        "materials": materials,
    }
    desired_head = _desired_project_head(
        binding_state,
        desired_binding["shaders"],
        materials,
        previous_head=previous_desired_head,
    )
    if realized_receipts:
        clean_receipts = _normalised_receipts_for_records(
            desired_binding["shaders"], realized_receipts
        )
        coverage = "complete"
    else:
        clean_receipts = []
        coverage = "none"
    realized_snapshot = _rdc_project_index.build_realized_apply_snapshot(
        desired_head, clean_receipts, coverage=coverage
    )
    return _rdc_project_index.build_project_binding(
        desired_binding, desired_head, realized_snapshot
    )


def _read_restore_identity_context(
    host: Any, project_checkpoint: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Retry one read-only identity request on the same living admitted owner."""

    checkpoint = getattr(_RESTORE_THREAD_STATE, "control_checkpoint", None)
    recovery = getattr(_RESTORE_THREAD_STATE, "context_recovery", None)
    diagnostic = None
    for attempt in range(2):
        if callable(checkpoint):
            checkpoint("before restore identity read" if not attempt else "before restore identity retry")
        if project_checkpoint is not None:
            project_checkpoint()
        try:
            live = host._active_live_context(allow_missing_capture_id=True)
        except (RestoreContextChanged, RestoreContextUnavailable, RestoreControlStop):
            raise
        except IPCBridgeResponseError as exc:
            # The frozen request binding rejects a real reload before returning
            # its identity. These existing bridge diagnostics report an observed
            # lifecycle mismatch; other bridge rejections grant no such evidence.
            if exc.message in {
                "The RenderDoc capture changed before the background tool started",
                "The frozen capture session changed before the Bread tool started",
            }:
                raise RestoreContextChanged(
                    "active project or capture changed during project restore: %s" % exc) from exc
            raise RestoreContextUnavailable(
                "restore identity is unavailable: %s: %s" % (type(exc).__name__, exc)) from exc
        except (IPCTransportError, OSError, TimeoutError) as exc:
            binding = _CALL_BINDING.get()
            state_reader = getattr(getattr(host, "ipc", None), "bound_instance_state", None)
            try:
                owner = state_reader() if callable(state_reader) else None
            except Exception:
                owner = None
            can_retry = bool(
                not attempt and binding is not None and isinstance(owner, Mapping)
                and owner.get("alive") is True and owner.get("instanceId") == binding[0]
            )
            if not can_retry:
                raise RestoreContextUnavailable(
                    "restore identity is unavailable: %s: %s" % (type(exc).__name__, exc)) from exc
            diagnostic = {
                "instanceId": binding[0], "errorType": type(exc).__name__,
                "error": str(exc)[:4096], "recovered": False,
            }
            if isinstance(recovery, list):
                recovery.append(diagnostic)
                del recovery[:-16]
            continue
        except Exception as exc:
            if str(exc) == "No RenderDoc capture is loaded":
                raise RestoreContextChanged(
                    "active project or capture changed during project restore: %s" % exc) from exc
            raise RestoreContextUnavailable(
                "restore identity is unreadable: %s: %s" % (type(exc).__name__, exc)) from exc
        if callable(checkpoint):
            checkpoint("after restore identity read")
        if project_checkpoint is not None:
            project_checkpoint()
        if diagnostic is not None:
            diagnostic["recovered"] = True
        return live
    raise AssertionError("restore identity read exhausted its bounded attempts")


def _read_restore_project_identity(path: Path) -> dict[str, Any]:
    from .file_content_cache import file_stamp
    cache = getattr(_RESTORE_THREAD_STATE, "project_identities", None)
    if cache is None:
        return read_json_object(path, MAX_PROJECT_BYTES, "restore project identity")
    # Share stable project metadata, not mutable per-Shader state. A reliable
    # filesystem change identity includes same-size/mtime-preserving edits.
    before = file_stamp(path)
    key = _path_key(path)
    cached = cache.get(key) if before is not None else None
    if cached is not None and cached[0] == before:
        return copy.deepcopy(cached[1])
    value = read_json_object(path, MAX_PROJECT_BYTES, "restore project identity")
    if before is not None and file_stamp(path) == before:
        cache[key] = (before, copy.deepcopy(value))
    return value


def _require_restore_context(
    host: Any,
    active: Mapping[str, Any],
    project_root: Path,
    capture_path: Path,
    capture_session_id: str,
) -> dict[str, Any]:
    """Revalidate the exact project/capture lifecycle after a long operation."""

    service = host._active_project_service()
    snapshot_factory = getattr(service, "snapshot_active_project", None)
    snapshot_reader = getattr(service, "_bound_project_snapshot", None)
    assert_snapshot = getattr(service, "assert_active_project_snapshot", None)
    if all(callable(value) for value in (snapshot_factory, snapshot_reader, assert_snapshot)):
        # The initial admission has already validated all project artifacts.
        # Later lifecycle checks need the real current selection/configuration
        # and project identity, rather than revalidating every saved artifact
        # twice through status() and the server status race guard.
        existing = snapshot_reader()
        context = (contextlib.nullcontext(existing) if existing is not None else
                   snapshot_factory(active["workspaceId"], project_root))
        try:
            with context as snapshot:
                assert_snapshot(snapshot)
                manifest = json.loads(snapshot.manifest_json)
                if (snapshot.project_root != project_root
                        or snapshot.workspace_id != active.get("workspaceId")
                        or manifest.get("state") != active.get("state")
                        or manifest.get("scope") != active.get("scope")
                        or str(manifest.get("assetRoot") or "") != str(active.get("assetRoot") or "")):
                    raise RuntimeError("active project configuration changed")
                replay_path = Path(str(active["paths"]["replayProject"]))
                if replay_path.is_symlink() or not replay_path.is_dir():
                    raise RuntimeError("active replay project path changed")
                project_identity = _read_restore_project_identity(replay_path / "project.json")
                expected_replay = active["replayProject"]
                if (project_identity.get("schema") != PROJECT_SCHEMA
                        or project_identity.get("projectId") != expected_replay.get("projectId")
                        or project_identity.get("scope") != expected_replay.get("scope")):
                    raise RuntimeError("active replay project identity changed")
                live = _read_restore_identity_context(host, lambda: assert_snapshot(snapshot))
                capture = live.get("capture") or {}
                expected_api = expected_replay.get("scope", {}).get("apiFamily") or active.get("scope", {}).get("apiFamily")
                if (capture.get("captureSessionId") != capture_session_id
                        or _path_key(capture.get("path", "")) != _path_key(capture_path)
                        or (expected_api and _reuse_api_family(capture.get("api")) != _reuse_api_family(expected_api))):
                    raise RuntimeError("live capture session, path or API changed")
                assert_snapshot(snapshot)
            return dict(active)
        except (RestoreContextChanged, RestoreContextUnavailable, RestoreControlStop):
            raise
        except OSError as exc:
            raise RestoreContextUnavailable(
                "restore project identity is unreadable: %s: %s" % (type(exc).__name__, exc)) from exc
        except Exception as exc:
            raise RestoreContextChanged("active project or capture changed during project restore: %s" % exc) from exc

    current, unused_context, project, unused_replay, session = (
        _validate_capture_and_project(
            host,
            project_root,
            capture_path,
            capture_session_id,
        )
    )
    if (
        project != project_root
        or session != capture_session_id
        or current.get("workspaceId") != active.get("workspaceId")
        or current.get("replayProject", {}).get("projectId")
        != active.get("replayProject", {}).get("projectId")
    ):
        raise RestoreContextChanged(
            "active project or capture changed during project restore"
        )
    return current


def _prepare_response(
    host: Any,
    active: Mapping[str, Any],
    project_root: Path,
    capture_path: Path,
    capture_session_id: str,
    progress: _Progress,
    control_checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Build and strictly bind the full target-capture reuse plan."""

    progress.emit(
        "prepare",
        "Exporting the full Shader catalog for project matching",
    )
    if control_checkpoint is not None:
        control_checkpoint("before full-frame Shader catalog export")
    prepare_with_progress = getattr(
        host, "_prepare_active_capture_project_with_progress", None
    )
    if callable(prepare_with_progress):
        forward_lock = threading.RLock()
        highest_rank = -1
        export_position: tuple[int, int] | None = None

        def forward_export_progress(observation: Any) -> None:
            # Telemetry is display-only. Repeat a strict bounded projection
            # here because high-level worker observations and legacy sidecar
            # polling can arrive from different threads.
            nonlocal highest_rank, export_position
            try:
                if not isinstance(observation, Mapping):
                    return
                schema = observation.get("schema")
                phase = observation.get("phase")
                current = observation.get("current")
                total = observation.get("total")
                event_id = observation.get("eventId", 0)
                updated_unix_ms = observation.get("updatedUnixMs")
                item = observation.get("item", "")
                if (
                    any(
                        isinstance(value, bool) or not isinstance(value, int)
                        for value in (current, total, event_id, updated_unix_ms)
                    )
                    or not 0 <= current <= total <= _MAX_FAST_EVIDENCE_USES
                    or not 0 <= event_id <= 0xFFFFFFFF
                    or updated_unix_ms <= 0
                    or not isinstance(item, str)
                    or "\x00" in item
                    or len(item.encode("utf-8")) > 1024
                ):
                    return
                if schema == "replaylab-shader-catalog-progress/v1":
                    if (
                        phase != "export-shader-bindings"
                        or not 1 <= current <= total
                    ):
                        return
                    message = _PREPARE_PROGRESS_MESSAGES[phase]
                    item = "EID %d" % event_id if event_id else ""
                elif schema == "replaylab-active-capture-prepare-progress/v1":
                    message = observation.get("message")
                    if (
                        observation.get("schemaVersion") != 1
                        or phase not in _PREPARE_PROGRESS_MESSAGES
                        or message != _PREPARE_PROGRESS_MESSAGES[phase]
                    ):
                        return
                    if phase == "export-shader-bindings" and not 1 <= current <= total:
                        return
                else:
                    return
                rank = _PREPARE_PROGRESS_RANKS[phase]
                with forward_lock:
                    if rank < highest_rank:
                        return
                    if phase == "export-shader-bindings":
                        if export_position is not None and (
                            total != export_position[1]
                            or current < export_position[0]
                        ):
                            return
                        export_position = (current, total)
                    highest_rank = max(highest_rank, rank)
                    progress.emit(
                        phase,
                        message,
                        current=current,
                        total=total,
                        item=item,
                        detail={
                            "eventId": event_id,
                            "updatedUnixMs": updated_unix_ms,
                        },
                    )
            except Exception:
                return

        accepts_control = False
        try:
            signature = inspect.signature(prepare_with_progress)
            accepts_control = "control_checkpoint" in signature.parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
        except (TypeError, ValueError):
            pass
        if accepts_control:
            raw = prepare_with_progress(
                progress.operation_id,
                forward_export_progress,
                control_checkpoint=control_checkpoint,
            )
        else:
            raw = prepare_with_progress(
                progress.operation_id, forward_export_progress
            )
    else:
        prepare = getattr(host, "_prepare_active_capture_project", None)
        if callable(prepare):
            raw = prepare(progress.operation_id)
        else:
            prepare = getattr(host, "prepare_active_capture_project", None)
            if not callable(prepare):
                raise RuntimeError(
                    "Bread Host has no active-capture preparation route"
                )
            raw = prepare()
    if control_checkpoint is not None:
        control_checkpoint("after full-frame Shader catalog export")
    prepared = _tool_json(
        raw,
        "prepare active capture project",
        schema="replaylab-active-capture-prepare/v1",
        schema_version=1,
    )
    capture = prepared.get("capture")
    project = prepared.get("project")
    staged = prepared.get("stagedCatalog")
    exported = prepared.get("captureExport")
    plan = prepared.get("reusePlan")
    matches = plan.get("matches") if isinstance(plan, Mapping) else None
    if (
        not isinstance(capture, Mapping)
        or not isinstance(project, Mapping)
        or not isinstance(staged, Mapping)
        or not isinstance(exported, Mapping)
        or not isinstance(plan, Mapping)
        or not isinstance(matches, list)
        or len(matches) > _MAX_FAST_CATALOG_SHADERS
        or capture.get("captureSessionId") != capture_session_id
        or project.get("workspaceId") != active.get("workspaceId")
        or project.get("scopeConfigured") is not True
        or _path_key(project.get("projectRoot", "")) != _path_key(project_root)
        or project.get("replayProject", {}).get("projectId")
        != active.get("replayProject", {}).get("projectId")
        or plan.get("schema") != "renderdoc-ultra-project-reuse-plan/v1"
        or plan.get("schemaVersion") != 1
        or plan.get("projectId") != active.get("replayProject", {}).get("projectId")
        or plan.get("matchedShaderCount") != len(matches)
        or exported.get("fullFrame") is not True
        or prepared.get("applied") is not False
    ):
        raise RuntimeError("active capture preparation returned invalid project evidence")
    capture_id = _hex(capture.get("captureId"), 64, "prepared captureId")
    catalog_sha256 = _hex(
        staged.get("catalogSha256"), 64, "prepared catalogSha256"
    )
    if any(
        (
            staged.get("captureId") != capture_id,
            exported.get("captureId") != capture_id,
            exported.get("catalogSha256") != catalog_sha256,
            plan.get("captureId") != capture_id,
            plan.get("catalogSha256") != catalog_sha256,
        )
    ):
        raise RuntimeError("active capture preparation changed capture/catalog identity")
    policy = plan.get("policy")
    if (
        not isinstance(policy, Mapping)
        or policy.get("rawExactOrCodeInterfaceLayoutExactOnly") is not True
        or policy.get("oldWorkspaceManifestReused") is not False
        or policy.get("oldCaptureAbiReportReused") is not False
        or policy.get("oldMaterialSessionReused") is not False
        or policy.get("liveAbiRevalidationRequiredBeforeShaderApply") is not True
        or policy.get("liveMaterialLayoutRevalidationRequired") is not True
    ):
        raise RuntimeError("project reuse plan weakened the physical validation policy")
    _require_restore_context(
        host, active, project_root, capture_path, capture_session_id
    )
    progress.emit(
        "prepare",
        "Full capture catalog prepared; validating project reuse matches",
        current=len(matches),
        total=len(matches),
        detail={"captureId": capture_id, "matchedShaderCount": len(matches)},
    )
    return prepared


def _prepare_inventory_response(
    replay: ReplayProjectService,
    active: Mapping[str, Any],
    inventory: Mapping[str, Any],
    progress: _Progress,
    control_checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Match the unique-resource inventory directly against project artifacts."""

    project_inventory = _project_shader_inventory(inventory, allow_target_lookup=True)
    shaders = project_inventory["shaders"]
    progress.emit(
        "resource-match",
        "Matching exact project Shader artifacts",
        current=0,
        total=len(shaders),
        detail={
            "uniqueShaderResourceCount": len(shaders),
            "eventTraversalCount": 0,
        },
    )
    if control_checkpoint is not None:
        control_checkpoint("before exact project Shader inventory match")
    snapshot = replay.reuse_snapshot_for_shader_inventory(project_inventory)
    if control_checkpoint is not None:
        control_checkpoint("after exact project Shader inventory match")
    plan = snapshot.get("reusePlan") if isinstance(snapshot, Mapping) else None
    binding = snapshot.get("binding") if isinstance(snapshot, Mapping) else None
    knowledge = snapshot.get("knowledge") if isinstance(snapshot, Mapping) else None
    matches = plan.get("matches") if isinstance(plan, Mapping) else None
    policy = plan.get("policy") if isinstance(plan, Mapping) else None
    if (
        not isinstance(snapshot, Mapping)
        or snapshot.get("schema")
        != "renderdoc-ultra-project-shader-inventory-reuse/v1"
        or snapshot.get("schemaVersion") != 1
        or snapshot.get("projectId")
        != active.get("replayProject", {}).get("projectId")
        or snapshot.get("inventory") != project_inventory
        or not isinstance(plan, Mapping)
        or plan.get("schema") != "renderdoc-ultra-project-reuse-plan/v1"
        or plan.get("schemaVersion") != 1
        or plan.get("captureId")
        != project_inventory["capture"]["captureId"]
        or plan.get("catalogSha256")
        != project_inventory["capture"]["inventorySha256"]
        or not isinstance(matches, list)
        or plan.get("matchedShaderCount") != len(matches)
        or len(matches) > len(shaders)
        or not isinstance(binding, Mapping)
        or not isinstance(knowledge, Mapping)
        or not isinstance(policy, Mapping)
        or policy.get("inventoryOnly") is not True
        or policy.get("fuzzyExecutableMatchAllowed") is not False
        or not (
            policy.get("stableKeyExactRequiredForSourceOrBinary") is True
            or (
                policy.get("stableKeyExactRequiredForSource") is False
                and policy.get("stableKeyExactRequiredForBinary") is True
                and policy.get("rawExactSourcePreparationAllowed") is True
            )
        )
        or policy.get("targetAnchorRequiredBeforeExecutableApply") is not True
        or policy.get("binaryExecutableAutoApplyAuthorized") is not False
        or policy.get("inventoryMaterialValueReuseAllowed") is not False
    ):
        raise RuntimeError("project returned an invalid exact Shader inventory plan")

    live_by_target = {
        (item["resourceId"], item["stableKeySha256"]): item
        for item in inventory["shaders"]
    }
    prepared_plan = copy.deepcopy(dict(plan))
    if inventory.get("inventoryScope") == "target-raw-sha256":
        prepared_plan.update(inventoryScope="target-raw-sha256", fullFrame=False,
                             targetRawSha256s=list(inventory["targetRawSha256s"]))
    for match in prepared_plan["matches"]:
        shader = match.get("shader") if isinstance(match, Mapping) else None
        if not isinstance(shader, Mapping):
            raise RuntimeError("project Shader inventory plan contains a malformed match")
        target = live_by_target.get(
            (shader.get("resourceId"), shader.get("stableKeySha256"))
        )
        if target is None:
            raise RuntimeError("project Shader inventory plan changed target identity")
        # Preserve the Bridge's conservative completeness verdict. The project
        # service validates the seven-field key, while only fresh live evidence
        # can authorize an executable replacement.
        match["shader"] = {
            **dict(shader),
            "stableKeyComplete": target.get("stableKeyComplete") is True,
            "anchorEventId": target.get("anchorEventId"),
        }
        if list(match.get("materialCandidates", []) or []):
            raise RuntimeError(
                "project inventory plan included event-scoped material values"
            )
    progress.emit(
        "resource-match",
        "Exact project Shader artifact matching completed",
        current=len(shaders),
        total=len(shaders),
        detail={
            "matchedShaderCount": len(prepared_plan["matches"]),
            "eventTraversalCount": 0,
        },
    )
    return {
        "schema": "replaylab-active-shader-inventory-prepare/v1",
        "schemaVersion": 1,
        "inventory": project_inventory,
        "liveInventory": copy.deepcopy(dict(inventory)),
        "reusePlan": prepared_plan,
        "binding": copy.deepcopy(dict(binding)),
        "knowledge": copy.deepcopy(dict(knowledge)),
        "snapshotSha256": snapshot.get("snapshotSha256"),
        "applied": False,
    }


def _prepared_binding_state(
    replay: ReplayProjectService,
    active: Mapping[str, Any],
    prepared: Mapping[str, Any],
) -> dict[str, Any]:
    if prepared.get("schema") == "replaylab-active-shader-inventory-prepare/v1":
        project_inventory = prepared.get("inventory")
        live_inventory = prepared.get("liveInventory")
        binding = prepared.get("binding")
        if not all(
            isinstance(value, Mapping)
            for value in (project_inventory, live_inventory, binding)
        ):
            raise RuntimeError("prepared Shader inventory snapshot is incomplete")
        snapshot = replay.validate_shader_inventory_snapshot(
            project_inventory, binding
        )
        state = _inventory_binding_state(snapshot, live_inventory)
        plan = prepared.get("reusePlan")
        if (
            not isinstance(plan, Mapping)
            or state.get("projectId") != plan.get("projectId")
            or state.get("projectId")
            != active.get("replayProject", {}).get("projectId")
            or state.get("captureId") != plan.get("captureId")
            or state.get("catalogSha256") != plan.get("catalogSha256")
        ):
            raise RuntimeError(
                "prepared Shader inventory knowledge differs from its reuse plan"
            )
        return state
    plan = prepared["reusePlan"]
    state = replay.restore_knowledge_snapshot(
        str(plan["captureId"]), str(plan["catalogSha256"])
    )
    if not isinstance(state, dict):
        raise RuntimeError("prepared project returned invalid knowledge state")
    for field in (
        "scopeSha256",
        "captureId",
        "catalogSha256",
        "knowledgeContentSha256",
        "matcherPolicySha256",
    ):
        _hex(state.get(field), 64, "prepared project %s" % field)
    _hex(state.get("projectId"), 32, "prepared projectId")
    if any(
        (
            state.get("projectId") != plan.get("projectId"),
            state.get("projectId")
            != active.get("replayProject", {}).get("projectId"),
            state.get("captureId") != plan.get("captureId"),
            state.get("catalogSha256") != plan.get("catalogSha256"),
            not isinstance(state.get("capture"), Mapping),
            not isinstance(state.get("workspaces"), Mapping),
            not isinstance(state.get("materials"), Mapping),
        )
    ):
        raise RuntimeError("prepared project knowledge differs from the reuse plan")
    return state


def _reuse_workspace_status(
    host: Any,
    active: Mapping[str, Any],
    workspace: Path,
    identity: Mapping[str, Any],
    capture_id: str,
    catalog_sha256: str | None,
) -> dict[str, Any]:
    # Restore needs current source/candidate bytes, not the public status
    # endpoint's organization/draft recovery walk on every before/after read.
    # Keep fresh per-workspace observations across mutations; only omit that
    # unrelated UI enrichment. Older Host adapters retain their status route.
    factory = getattr(host, "_shader_workspace_artifact_service", None)
    if callable(factory):
        status = dict(factory().status(str(workspace)))
    else:
        wrapped = _tool_json(host.get_shader_workspace_status(str(workspace)),
            "get target Shader workspace status", schema="renderdoc-ultra-shader-workspace-mcp/v1")
        if wrapped.get("operation") != "status" or not isinstance(wrapped.get("result"), Mapping):
            raise RuntimeError("target Shader workspace status is invalid")
        status = dict(wrapped["result"])
    return _validate_reuse_workspace_status(
        status, active, workspace, identity, capture_id, catalog_sha256
    )


def _validate_reuse_workspace_status(
    status: dict[str, Any], active: Mapping[str, Any], workspace: Path,
    identity: Mapping[str, Any], capture_id: str, catalog_sha256: str | None,
) -> dict[str, Any]:
    capture = status.get("capture")
    shader = status.get("shader")
    baseline = status.get("baseline")
    source = status.get("source")
    shader_root = Path(
        str(active.get("paths", {}).get("shaderWorkspaces", ""))
    ).resolve()
    # A matching captured program can still have only its binary representation
    # while the saved project source is being imported. Report that missing step
    # separately: it does not mean the current ResourceId or original bytes changed.
    mismatches = []
    if status.get("schema") != "renderdoc-ultra-shader-workspace-status/v1":
        mismatches.append("schema")
    if (_path_key(status.get("workspacePath", "")) != _path_key(workspace)
            or workspace.is_symlink() or not workspace.is_dir()
            or not shader_root.is_dir() or not is_within(shader_root, workspace)):
        mismatches.append("workspacePath")
    if not isinstance(capture, Mapping):
        mismatches.append("capture")
    else:
        if capture.get("captureId") != capture_id:
            mismatches.append("capture.captureId")
        # A fresh bounded catalog can have a different snapshot/serialization
        # hash from this workspace's original catalog. It is provenance for
        # that snapshot, not the Shader's physical identity. Strict status has
        # already validated the stored catalog; retain its provenance and bind
        # this restore to the actual current capture/program below.
        if identity.get("api") and _asset_api(capture.get("api")) != identity["api"]:
            mismatches.append("capture.api")
    if not isinstance(shader, Mapping):
        mismatches.append("shader")
    else:
        for field in ("resourceId", "rawSha256"):
            if shader.get(field) != identity.get(field):
                mismatches.append("shader." + field)
    if not isinstance(baseline, Mapping):
        mismatches.append("baseline")
    else:
        if baseline.get("rawSha256") != identity.get("rawSha256"):
            mismatches.append("baseline.rawSha256")
        if identity.get("encoding") and _asset_encoding(baseline.get("kind")) != identity["encoding"]:
            mismatches.append("baseline.kind")
    for field in ("stage", "entryPoint"):
        if status.get(field, "") != identity.get(field, ""):
            mismatches.append(field)
    if mismatches:
        raise RuntimeError("target Shader workspace changed physical identity (%s)" %
                           ", ".join(mismatches))
    _hex(status.get("workspaceId"), 32, "target workspaceId")
    source_mismatches = []
    if not isinstance(source, Mapping):
        source_mismatches.append("source")
    else:
        if source.get("present") is not True:
            source_mismatches.append("source.present")
        if source.get("lane") != "executable-editable":
            source_mismatches.append("source.lane")
        if source.get("format") not in {"hlsl", "glsl"}:
            source_mismatches.append("source.format")
    if source_mismatches:
        raise RuntimeError("target Shader workspace has no executable editable source (%s); "
                           "the matching saved project source must be imported before compilation" %
                           ", ".join(source_mismatches))
    _hex(source.get("currentSha256"), 64, "target sourceSha256")
    return status


def _local_restore_candidate(status: Mapping[str, Any], project_source_sha256: str) -> bool:
    """Preserve a divergent edit; only its exact validated candidate may restore.

    Callers pass the ordinary strict workspace status after checking its current
    capture/original identity. A project snapshot is not permission to replace a
    person's working source, whether or not that source has compiled yet.
    """
    source = status.get("source") or {}
    current_sha = source.get("currentSha256")
    _preserve_unpublished_include_edit(status)
    if current_sha == project_source_sha256 or source.get("modifiedSinceDecompile") is False:
        return False
    latest = status.get("latestCompiled") or {}
    if (current_sha and latest.get("sourceSha256") == current_sha
            and type(latest.get("revision")) is int and latest["revision"] > 0
            and latest["revision"] == status.get("revision")
            and latest.get("replayAbiValidated") is True
            and latest.get("abiCompatible") is True
            and latest.get("requiresReplayAbiValidation") is False
            and (latest.get("abiGate") or {}).get("status") == "compatible"):
        _hex(latest.get("rawSha256"), 64, "local candidateRawSha256")
        return True
    raise RestoreLocalEdit(
        "The local Shader source differs from project knowledge and has no exact "
        "validated current candidate; the source was preserved. Compile and validate "
        "this edit, or explicitly select a saved revision before restoring it.",
        Path(str(status.get("workspacePath") or "")) if status.get("workspacePath") else None,
        str(current_sha or ""),
    )


def _preserve_unpublished_include_edit(status: Mapping[str, Any]) -> None:
    source = status.get("source") or {}
    latest = status.get("latestCompiled") or {}
    if (status.get("sourceDependenciesCurrent", True) is False and latest
            and latest.get("sourceSha256") == source.get("currentSha256")):
        raise RestoreLocalEdit(
            "A Shader include changed since the saved candidate was validated; "
            "the editable source was preserved and was not automatically applied.",
            Path(str(status.get("workspacePath") or "")) if status.get("workspacePath") else None,
            str(source.get("currentSha256") or ""),
        )


def _registered_reuse_workspace(
    host: Any, active: Mapping[str, Any], identity: Mapping[str, Any],
    knowledge: Mapping[str, Any] | None, capture_path: Path | None,
    expected_source_sha256: str,
) -> tuple[Path, dict[str, Any], str, str | None] | None:
    """Reuse the exact saved source owner already proven by the live inventory.

    The registered path is a hint, never apply authority. The strict workspace
    reader validates its source/candidate bytes, and the normal apply boundary
    still checks the current original Shader and representative EID.
    """

    if not isinstance(knowledge, Mapping) or capture_path is None:
        return None
    registered = knowledge.get("registeredWorkspace")
    factory = (getattr(host, "_shader_workspace_artifact_service", None)
               or getattr(host, "_shader_workspace_service", None))
    if not isinstance(registered, Mapping) or not callable(factory):
        return None
    try:
        raw_path = Path(str(registered.get("path") or "")).expanduser()
        root = Path(str(active.get("paths", {}).get("shaderWorkspaces", ""))).resolve()
        if (not raw_path.is_absolute() or raw_path.is_symlink() or not raw_path.is_dir()
                or not is_within(root, raw_path.resolve())):
            return None
        workspace = raw_path.resolve()
        status = factory().status(str(workspace))
        capture = status.get("capture") if isinstance(status, Mapping) else None
        source = status.get("source") if isinstance(status, Mapping) else None
        baseline = status.get("baseline") if isinstance(status, Mapping) else None
        if (not all(isinstance(value, Mapping) for value in (capture, source, baseline))
                or status.get("workspaceId") != registered.get("workspaceId")
                or _path_key(capture.get("capturePath", "")) != _path_key(capture_path)
                or _asset_api(capture.get("api")) != identity.get("api")
                or _asset_encoding(baseline.get("kind")) != identity.get("encoding")
                or knowledge.get("sourceSha256") != expected_source_sha256
                or status.get("targetProfile") != knowledge.get("targetProfile")):
            return None
        capture_id = _hex(capture.get("captureId"), 64, "registered workspace captureId")
        catalog_sha = str(capture.get("catalogSha256") or "") or None
        status = _validate_reuse_workspace_status(
            dict(status), active, workspace, identity, capture_id, catalog_sha
        )
        local_candidate = _local_restore_candidate(status, expected_source_sha256)
        if source.get("currentSha256") != expected_source_sha256 and not local_candidate:
            return None
        return workspace, status, capture_id, catalog_sha
    except RestoreLocalEdit:
        raise
    except Exception:
        # Stale/missing registered owners retain the ordinary source preparation
        # path. Never overwrite an existing source to make the hint fit.
        return None


def _snapshot_reuse_workspace(
    host: Any, active: Mapping[str, Any], identity: Mapping[str, Any],
    knowledge: Mapping[str, Any] | None, capture_path: Path | None,
    expected_source_sha256: str,
) -> tuple[Path, dict[str, Any], str, str | None] | None:
    """Read an unchanged item's route from a stale whole-project snapshot.

    Another Shader's edit can invalidate the snapshot as a transaction. Its
    saved path remains only a hint: canonical current source/candidate/profile
    and original identity are checked, then normal apply authenticates proof.
    """
    if not isinstance(knowledge, Mapping) or capture_path is None:
        return None
    try:
        project = Path(str(active["projectRoot"])).resolve()
        path = _verified_rdc_restore_path(Path(active["paths"]["replayProject"]), capture_path)
        if (not path.is_file() or path.is_symlink() or not is_within(project, path.resolve())
                or path.stat().st_size > _MAX_FAST_CATALOG_BYTES):
            return None
        saved = json.loads(path.read_text(encoding="utf-8"))
        seal = saved.pop("sealSha256", None)
        if (seal != _canonical_sha256(saved)
                or saved.get("schema") != _VERIFIED_RDC_RESTORE_SCHEMA
                or saved.get("schemaVersion") != _VERIFIED_RDC_RESTORE_VERSION
                or saved.get("projectId") != active["replayProject"]["projectId"]
                or saved.get("projectWorkspaceId") != active["workspaceId"]
                or _path_key(saved.get("capturePath", "")) != _path_key(capture_path)):
            return None
        records = saved.get("shaders")
        if not isinstance(records, list) or len(records) > _MAX_FAST_PROJECT_SHADERS:
            return None
        exact = [record for record in records if isinstance(record, Mapping)
                 and record.get("knowledgeId") == knowledge.get("knowledgeId")
                 and record.get("sourceSha256") == expected_source_sha256
                 and all(record.get("identity", {}).get(key) == identity.get(key)
                         for key in ("resourceId", "rawSha256", "api", "encoding", "stage", "entryPoint"))]
        if len(exact) != 1:
            return None
        record = exact[0]
        origin = saved.get("bindingState", {}).get("workspaces", {}).get(record["knowledgeId"])
        if not isinstance(origin, Mapping) or origin.get("targetProfile") != knowledge.get("targetProfile"):
            return None
        managed = record.get("managedWorkspace") or {}
        workspace = _resolve_workspace_path(project, Path(active["paths"]["shaderWorkspaces"]), managed.get("path", ""))
        hint = {**knowledge, "registeredWorkspace": {"path": str(workspace), "workspaceId": managed.get("workspaceId")}}
        reused = _registered_reuse_workspace(host, active, identity, hint, capture_path, expected_source_sha256)
        if reused is None:
            return None
        latest = reused[1].get("latestCompiled") or {}
        if (latest.get("rawSha256") != record.get("candidateRawSha256")
                or latest.get("sourceSha256") != expected_source_sha256
                or latest.get("revision") != reused[1].get("revision")
                or saved.get("captureSha256") != _verified_rdc_content_sha256(capture_path)):
            return None
        return reused
    except RestoreLocalEdit:
        raise
    except Exception:
        return None


def _suspend_workspace_watchers(host: Any, workspace: Path):
    """Pause the process's live-edit watchers of one workspace, if any.

    The restore compiles and applies into the same workspace the GUI may be
    watching for editor saves. Without this the watcher compiles the restore's
    own source write as revision N+1 and applies it, and the restore then finds
    that the workspace "no longer contains the expected saved candidate".
    Returns an entered context object (call ``__exit__`` when done) or None.
    """

    # Automatic resume may finish before restore reserves this capture. Retire
    # that exact in-memory writer without revoking durable consent or rolling
    # back the current replacement. Restore starts its editor after committing;
    # another GUI session is never paused by this transaction.
    from . import internal_tool_service
    binding = _CALL_BINDING.get()
    with internal_tool_service._SHADER_LIVE_EDIT_MANAGER_GUARD:
        manager = internal_tool_service._SHADER_LIVE_EDIT_MANAGER
    if manager is not None and binding is not None:
        with manager._lifecycle_lock:
            with manager._lock:
                jobs = [job for job in manager._jobs.values()
                    if _path_key(job.workspace) == _path_key(workspace)
                    and job.session is not None
                    and (job.session.binding.get("instanceId"), job.session.binding.get("captureSessionId")) == binding
                    and not job.is_terminal()]
            for job in jobs:
                job.stop()
                with manager._lock:
                    key = _path_key(workspace)
                    if manager._active_by_workspace.get(key) == job.live_edit_id:
                        manager._active_by_workspace.pop(key, None)
    factory = getattr(host, "_shader_workspace_watch_manager", None)
    if not callable(factory):
        return None
    try:
        manager = factory()
        suspend = getattr(manager, "suspend_workspace", None)
        if not callable(suspend):
            return None
        suspension = suspend(str(workspace), "project-restore")
        suspension.__enter__()
        return suspension
    except Exception:
        return None


def _local_source_hint(
    host: Any, active: Mapping[str, Any], workspace: Path,
    identity: Mapping[str, Any], capture_path: Path | None,
) -> dict[str, Any]:
    """Keep a source owner as a managed display hint, never replay authority."""
    project = Path(str(active["projectRoot"])).resolve()
    root = Path(str(active["paths"]["shaderWorkspaces"])).resolve()
    relative = workspace.resolve().relative_to(project).as_posix()
    workspace = _resolve_workspace_path(project, root, relative)
    status = _workspace_artifact_service(host).status(str(workspace))
    capture = status.get("capture") or {}
    if capture_path is None or _path_key(capture.get("capturePath", "")) != _path_key(capture_path):
        raise RuntimeError("preserved Shader source belongs to another capture")
    _validate_reuse_workspace_status(dict(status), active, workspace, identity,
        _hex(capture.get("captureId"), 64, "preserved source captureId"),
        str(capture.get("catalogSha256") or "") or None)
    return {"path": relative,
            "workspaceId": _hex(status.get("workspaceId"), 32, "preserved source workspaceId")}


def _prior_local_source_hint(
    active: Mapping[str, Any], workspace: Path, identity: Mapping[str, Any], capture: Path,
) -> dict[str, Any] | None:
    """Recover display-only ownership after the legacy RDC codec drops it."""
    try:
        project = Path(str(active["projectRoot"])).resolve()
        path = _verified_rdc_restore_path(Path(active["paths"]["replayProject"]), capture)
        if (path.is_symlink() or not path.is_file() or not is_within(project, path.resolve())
                or path.stat().st_size > _MAX_FAST_CATALOG_BYTES):
            return None
        saved = json.loads(path.read_text(encoding="utf-8"))
        seal = saved.pop("sealSha256", None)
        if (seal != _canonical_sha256(saved) or saved.get("schema") != _VERIFIED_RDC_RESTORE_SCHEMA
                or saved.get("projectId") != active["replayProject"]["projectId"]
                or saved.get("projectWorkspaceId") != active["workspaceId"]
                or _path_key(saved.get("capturePath", "")) != _path_key(capture)):
            return None
        rows = saved.get("shaders")
        if not isinstance(rows, list) or len(rows) > _MAX_FAST_PROJECT_SHADERS:
            return None
        matches = [row.get("preservedLocalSource") for row in rows if isinstance(row, Mapping)
                   and _path_key(project / str(row.get("managedWorkspace", {}).get("path", ""))) == _path_key(workspace)
                   and all(row.get("identity", {}).get(key) == identity.get(key)
                           for key in ("resourceId", "rawSha256", "api", "encoding", "stage", "entryPoint"))
                   and isinstance(row.get("preservedLocalSource"), Mapping)]
        return dict(matches[0]) if len(matches) == 1 else None
    except Exception:
        return None


def _restore_local_source_display(
    host: Any, active: Mapping[str, Any], project: Path, capture: Path,
    record: Mapping[str, Any], knowledge: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Read today's draft separately from the immutable restored executable.

    Hints are display-only and cannot veto an authenticated replay restoration.
    Missing/deleted drafts and untrusted paths are ignored, never displayed or
    watched. Draft edits do not invalidate the independent signed replay cache.
    """
    try:
        hint = record.get("preservedLocalSource")
        if not isinstance(hint, Mapping):
            replay_workspace = project / record["managedWorkspace"]["path"]
            hint = _prior_local_source_hint(active, replay_workspace, record["identity"], capture)
        if not isinstance(hint, Mapping) and isinstance(knowledge, Mapping):
            registered = knowledge.get("registeredWorkspace") or {}
            if registered.get("workspaceId") != record["managedWorkspace"]["workspaceId"]:
                hint = {"workspaceId": registered.get("workspaceId"),
                        "path": Path(str(registered.get("path") or "")).resolve().relative_to(project).as_posix()}
        if not isinstance(hint, Mapping):
            return None
        workspace = _resolve_workspace_path(project,
            Path(str(active["paths"]["shaderWorkspaces"])).resolve(), str(hint.get("path", "")))
        checked = _local_source_hint(host, active, workspace, record["identity"], capture)
        if checked != dict(hint) or checked.get("workspaceId") == record["managedWorkspace"]["workspaceId"]:
            return None
        status = _workspace_artifact_service(host).status(str(workspace))
        item = {"workspaceId": checked["workspaceId"], "workspacePath": str(workspace),
                "sourceSha256": _hex(status.get("source", {}).get("currentSha256"), 64,
                                     "preserved Shader sourceSha256"),
                "reason": "local-edit", "shown": False}
    except Exception:
        return None
    show = getattr(host, "_show_saved_shader_workspace_source", None)
    if callable(show):
        try:
            shown = _tool_json(show(str(workspace), activate_window=False),
                               "show preserved local Shader source")
            item["shown"] = shown.get("shown") is True
        except Exception:
            pass
    return item


def _isolated_replay_workspace_path(workspace: Path, knowledge_id: str) -> Path:
    """Keep replay owners within the normal workspace path budget on all hosts."""
    return workspace.with_name(_canonical_sha256({
        "domain": "replaylab-isolated-replay-workspace/v1",
        "originalWorkspace": workspace.name,
        "knowledgeId": _hex(knowledge_id, 32, "replay knowledge ID"),
    }))


def _prepare_reuse_workspace(
    host: Any,
    active: Mapping[str, Any],
    identity: Mapping[str, Any],
    capture_session_id: str,
    catalog_sha256: str,
    *,
    expected_anchor_event_id: int,
    expected_source_sha256: str,
    inventory_mode: bool = False,
    registered_knowledge: Mapping[str, Any] | None = None,
    capture_path: Path | None = None,
    progress: Any = None,
    diagnostic: dict[str, Any] | None = None,
    pending_source: Mapping[str, Any] | None = None,
) -> tuple[Path, dict[str, Any], str, str | None]:
    # Freeze the representative EID selected by the current unique-resource
    # inventory. Every later workspace admission and apply receipt must remain
    # bound to this exact anchor.
    event_id = _integer(
        expected_anchor_event_id, "expected Shader anchorEventId", 1
    )
    if event_id != _shader_anchor_event_id(identity):
        raise RuntimeError("target Shader inventory representative EID changed")
    preserved_local_edit = None
    # Saved executable source owns replay independently from a newer working
    # draft. Isolation is available only for knowledge belonging to this exact
    # captured original, never a fuzzy match or an arbitrary source path.
    can_isolate_source = bool(
        isinstance(registered_knowledge, Mapping)
        and callable(getattr(host, "_active_capture_catalog_selection", None))
        and callable(getattr(getattr(host, "_shader_workspace_service", lambda: None)(), "create", None))
        and registered_knowledge.get("sourceSha256") == expected_source_sha256
        and registered_knowledge.get("knowledgeId")
        and all(registered_knowledge.get(key) == identity.get(key)
                for key in ("rawSha256", "api", "encoding", "stage"))
        and registered_knowledge.get("entryPoint", "") == identity.get("entryPoint", ""))
    if inventory_mode:
        try:
            existing = _registered_reuse_workspace(
                host, active, identity, registered_knowledge, capture_path,
                expected_source_sha256,
            )
        except RestoreLocalEdit as exc:
            # The original knowledge owner may now contain an unfinished edit.
            # Preserve it, but first try the already saved exact-source owner
            # from this RDC's last successful restore. That path still checks
            # source/candidate/profile/capture bytes and grants no apply rights.
            preserved_local_edit = exc
            existing = None
        if (existing is not None and pending_source
                and existing[1].get("source", {}).get("currentSha256") == pending_source.get("sourceSha256")
                and pending_source.get("sourceSha256") != expected_source_sha256):
            existing = None
        from_snapshot = False
        if existing is None:
            try:
                existing = _snapshot_reuse_workspace(
                    host, active, identity, registered_knowledge, capture_path,
                    expected_source_sha256,
                )
            except RestoreLocalEdit as exc:
                if preserved_local_edit is None:
                    preserved_local_edit = exc
            if (existing is not None and pending_source
                    and existing[1].get("source", {}).get("currentSha256") == pending_source.get("sourceSha256")
                    and pending_source.get("sourceSha256") != expected_source_sha256):
                existing = None
            from_snapshot = existing is not None
        if (existing is None and preserved_local_edit is not None and not can_isolate_source
                and (not pending_source or preserved_local_edit.workspace_source_sha256 != pending_source.get("sourceSha256"))):
            raise preserved_local_edit
        if existing is not None:
            workspace, status, capture_id, located_catalog = existing
            if diagnostic is not None:
                diagnostic.update(
                    workspacePath=str(workspace), resolvedCatalogSha256=located_catalog,
                    requestedProjectSourceSha256=expected_source_sha256,
                    reusedRegisteredWorkspace=not from_snapshot,
                    reusedPriorSnapshotWorkspace=from_snapshot,
                )
                if (can_isolate_source and preserved_local_edit is not None
                        and preserved_local_edit.workspace is not None):
                    diagnostic["preservedLocalSource"] = _local_source_hint(
                        host, active, preserved_local_edit.workspace, identity, capture_path)
                elif capture_path is not None:
                    prior_hint = _prior_local_source_hint(active, workspace, identity, capture_path)
                    if prior_hint is not None:
                        diagnostic["preservedLocalSource"] = prior_hint
            if progress is not None:
                progress.emit("resource-anchor", "Reusing the exact registered Shader workspace",
                              item="Shader ResourceId %d" % identity["resourceId"])
            return existing
    if progress is not None:
        progress.emit(
            "resource-anchor",
            "Resolving the target Shader workspace (bounded catalog export)",
            item="Shader ResourceId %d" % identity["resourceId"],
        )
    located = _tool_json(
        host.get_active_shader_workspace_path(
            event_id, str(identity["stage"]),
            expected_resource_id=int(identity["resourceId"]),
            expected_entry_point=str(identity.get("entryPoint", "") or ""),
            expected_raw_sha256=str(identity["rawSha256"]),
            expected_capture_session_id=capture_session_id,
        ),
        "resolve target Shader workspace",
        schema="replaylab-active-shader-workspace-path/v1",
        schema_version=1,
    )
    located_catalog = str(located.get("catalogSha256") or "") or None
    if any(
        (
            located.get("captureId") is None,
            located.get("captureSessionId") != capture_session_id,
            located.get("eventId") != event_id,
            located.get("stage") != identity.get("stage"),
            located.get("entryPoint", "") != identity.get("entryPoint", ""),
            located.get("shaderResourceId") != identity.get("resourceId"),
            located.get("rawSha256") != identity.get("rawSha256"),
            (not inventory_mode and located_catalog != catalog_sha256),
            located.get("applied") is not False,
        )
    ):
        raise RuntimeError("target Shader workspace path changed identity")
    capture_id = _hex(located.get("captureId"), 64, "target captureId")
    raw_workspace = Path(str(located.get("workspacePath", ""))).expanduser()
    if not raw_workspace.is_absolute():
        raise RuntimeError("target Shader workspace path is not absolute")
    workspace = raw_workspace.resolve()
    shader_root = Path(
        str(active.get("paths", {}).get("shaderWorkspaces", ""))
    ).resolve()
    if not shader_root.is_dir() or not is_within(shader_root, workspace):
        raise RuntimeError("target Shader workspace escaped the active project")
    factory = getattr(host, "_shader_workspace_service", None)
    desired_format = (registered_knowledge or {}).get("sourceFormat", "hlsl")
    representation_changed = False
    if (pending_source or can_isolate_source) and workspace.exists() and callable(factory):
        try:
            observed = _workspace_artifact_service(host).status(str(workspace))
        except RuntimeError as exc:
            if "candidate ABI report does not prove exact compatibility" not in str(exc):
                raise
            factory().reopen_candidate_for_project_restore(
                workspace, expected_capture_id=capture_id,
                expected_resource_id=int(identity["resourceId"]), expected_stage=str(identity["stage"]),
                expected_entry_point=str(identity.get("entryPoint", "") or ""),
                expected_original_raw_sha256=str(identity["rawSha256"]),
                expected_source_sha256=_hex(expected_source_sha256, 64, "project Shader sourceSha256"))
            observed = _workspace_artifact_service(host).status(str(workspace))
        if (observed.get("source") or {}).get("present") is True:
            observed = _validate_reuse_workspace_status(
                dict(observed), active, workspace, identity, capture_id, located_catalog)
            representation_changed = bool(can_isolate_source
                and observed["source"].get("format") != desired_format)
            if (preserved_local_edit is not None and preserved_local_edit.workspace is not None
                    and _path_key(preserved_local_edit.workspace) != _path_key(workspace)
                    and observed["source"].get("currentSha256") == expected_source_sha256
                    and observed.get("sourceDependenciesCurrent", True) is True):
                # The active lookup may already return the isolated replay
                # owner. Retain its identity/cache instead of nesting suffixes.
                if diagnostic is not None:
                    diagnostic.update(workspacePath=str(workspace), resolvedCatalogSha256=located_catalog,
                        requestedProjectSourceSha256=expected_source_sha256,
                        preservedLocalSource=_local_source_hint(host, active,
                            preserved_local_edit.workspace, identity, capture_path))
                return workspace, observed, capture_id, located_catalog
            if (pending_source
                    and observed["source"].get("currentSha256") == pending_source.get("sourceSha256")
                    and pending_source.get("sourceSha256") != expected_source_sha256):
                preserved_local_edit = RestoreLocalEdit("pending project source", workspace,
                    observed["source"]["currentSha256"])
            else:
                try:
                    _local_restore_candidate(observed, expected_source_sha256)
                except RestoreLocalEdit as exc:
                    if not can_isolate_source:
                        raise
                    preserved_local_edit = exc
    if representation_changed:
        # An original program can have only one current project source. Keep
        # immutable old receipts/drafts at their recorded paths, and bind the
        # current representation to a stable owner, independent of revisions.
        # Feeding HLSL into a GLSL route is never a compatibility migration.
        if preserved_local_edit is not None and preserved_local_edit.workspace is not None and diagnostic is not None:
            diagnostic["preservedLocalSource"] = _local_source_hint(
                host, active, preserved_local_edit.workspace, identity, capture_path)
        workspace = workspace.with_name(_canonical_sha256({
            "domain": "replaylab-project-source-representation/v1",
            "captureId": capture_id, "resourceId": identity["resourceId"],
            "rawSha256": identity["rawSha256"], "stage": identity["stage"],
            "entryPoint": identity.get("entryPoint", ""), "sourceFormat": desired_format,
        }))
    if preserved_local_edit is not None and not representation_changed and (pending_source or can_isolate_source):
        # Never import the saved executable into a person's unfinished source.
        # The independent replay owner is prepared from the exact current
        # captured program and still passes normal seed/compile/apply checks.
        if can_isolate_source and preserved_local_edit.workspace is not None and diagnostic is not None:
            diagnostic["preservedLocalSource"] = _local_source_hint(
                host, active, preserved_local_edit.workspace, identity, capture_path)
        knowledge_id = _hex((registered_knowledge or {}).get("knowledgeId"), 32,
                            "replay knowledge ID")
        legacy_workspace = workspace.with_name(workspace.name + "-replay-" + knowledge_id)
        workspace = _isolated_replay_workspace_path(workspace, knowledge_id)
        if workspace.is_symlink() or not is_within(shader_root, workspace.resolve()):
            raise RuntimeError("validated replay source escaped the active project")
        if (not workspace.exists() and legacy_workspace.exists()
                and not legacy_workspace.is_symlink()
                and is_within(shader_root, legacy_workspace.resolve())):
            # Published older owners can keep their signed paths and compiled
            # cache. An unfinished legacy owner must not force a new compiler
            # job back onto the overlong path this naming scheme replaces.
            try:
                legacy_status = _validate_reuse_workspace_status(
                    _workspace_artifact_service(host).status(str(legacy_workspace)), active, legacy_workspace,
                    identity, capture_id, located_catalog)
                latest = legacy_status.get("latestCompiled") or {}
                if (legacy_status["source"].get("currentSha256") == expected_source_sha256
                        and legacy_status.get("sourceDependenciesCurrent", True) is True
                        and legacy_status.get("targetProfile") == (registered_knowledge or {}).get("targetProfile")
                        and latest.get("sourceSha256") == expected_source_sha256
                        and type(latest.get("revision")) is int and latest["revision"] > 0
                        and latest["revision"] == legacy_status.get("revision")):
                    _hex(latest.get("rawSha256"), 64, "legacy replay candidateRawSha256")
                    if diagnostic is not None:
                        diagnostic.update(workspacePath=str(legacy_workspace),
                            resolvedCatalogSha256=located_catalog,
                            requestedProjectSourceSha256=expected_source_sha256)
                    return legacy_workspace, legacy_status, capture_id, located_catalog
            except (RuntimeError, ValueError):
                pass
    if not workspace.exists() and (representation_changed or
            (preserved_local_edit is not None and (pending_source or can_isolate_source))):
        selection = host._active_capture_catalog_selection(event_id, str(identity["stage"]))
        if selection is None:
            raise RuntimeError("validated replay source has no current exact Shader catalog")
        exported, catalog_path, selected, unused_binding = selection
        if (exported.get("captureSessionId") != capture_session_id
                or selected.capture_id != capture_id
                or selected.key.resource_id != identity["resourceId"]
                or selected.key.raw_sha256 != identity["rawSha256"]
                or selected.key.stage != identity["stage"]
                or selected.key.entry_point != identity.get("entryPoint", "")):
            raise RuntimeError("validated replay source catalog changed its original Shader")
        factory().create(catalog_path, selected.key.catalog_key, workspace,
                         editable_format=desired_format)
    if diagnostic is not None:
        diagnostic.update(
            workspacePath=str(workspace),
            resolvedCatalogSha256=located_catalog,
            requestedProjectSourceSha256=expected_source_sha256,
        )
    workspace_existed = workspace.exists()
    def reopen_stale_candidate() -> None:
        factory = getattr(host, "_shader_workspace_service", None)
        if not callable(factory):
            raise RuntimeError("target workspace has no candidate recovery service")
        factory().reopen_candidate_for_project_restore(
            workspace,
            expected_capture_id=capture_id,
            expected_resource_id=int(identity["resourceId"]),
            expected_stage=str(identity["stage"]),
            expected_entry_point=str(identity.get("entryPoint", "") or ""),
            expected_original_raw_sha256=str(identity["rawSha256"]),
            expected_source_sha256=_hex(expected_source_sha256, 64, "project Shader sourceSha256"),
        )

    if workspace_existed:
        # prepare() may import project source. Inspect the existing owner's
        # source before entering that path, not after it has already been seeded.
        factory = (getattr(host, "_shader_workspace_artifact_service", None)
                   or getattr(host, "_shader_workspace_service", None))
        reader = getattr(factory(), "status", None) if callable(factory) else None
        def read_existing_status():
            if callable(reader):
                return reader(str(workspace))
            return _tool_json(host.get_shader_workspace_status(str(workspace)),
                             "read existing target Shader source",
                             schema="renderdoc-ultra-shader-workspace-mcp/v1")["result"]
        try:
            existing_status = read_existing_status()
        except RuntimeError as exc:
            if "candidate ABI report does not prove exact compatibility" not in str(exc):
                raise
            # The recovery API authenticates the exact current source digest
            # before clearing a stale ABI gate. It rejects divergent drafts,
            # so this bounded retry cannot make an old project overwrite one.
            reopen_stale_candidate()
            existing_status = read_existing_status()
        if (existing_status.get("source") or {}).get("present") is True:
            status = _validate_reuse_workspace_status(
                dict(existing_status), active, workspace, identity, capture_id, located_catalog)
            if _local_restore_candidate(status, expected_source_sha256):
                return workspace, status, capture_id, located_catalog
    if progress is not None:
        progress.emit(
            "resource-anchor",
            "Preparing the target Shader workspace from its captured program",
            item="Shader ResourceId %d" % identity["resourceId"],
        )
    def prepare_once() -> dict[str, Any]:
        return _tool_json(
            host.prepare_shader_workspace(
                event_id,
                str(identity["stage"]),
                str(workspace),
                expected_resource_id=int(identity["resourceId"]),
                expected_entry_point=str(identity.get("entryPoint", "") or ""),
                expected_raw_sha256=str(identity["rawSha256"]),
                expected_capture_id=capture_id,
                expected_capture_session_id=capture_session_id,
                admit_existing=workspace_existed,
                **({"editable_format": desired_format} if can_isolate_source else {}),
            ),
            "prepare target Shader workspace",
            schema="renderdoc-ultra-shader-workspace-mcp/v1",
        )

    try:
        prepared = prepare_once()
    except RuntimeError as exc:
        if (
            not workspace_existed
            or not str(exc).startswith(
                "prepare target Shader workspace failed: "
                "candidate ABI report does not prove exact compatibility"
            )
        ):
            raise
        reopen_stale_candidate()
        prepared = prepare_once()
    prepared_result = prepared.get("result")
    prepared_binding = (
        prepared_result.get("binding")
        if isinstance(prepared_result, Mapping)
        else None
    )
    prepared_created = (
        prepared_result.get("created")
        if isinstance(prepared_result, Mapping)
        else None
    )
    if (
        prepared.get("operation") != "prepare"
        or not isinstance(prepared_result, Mapping)
        or _path_key(prepared_result.get("workspacePath", ""))
        != _path_key(workspace)
        or not isinstance(prepared_binding, Mapping)
        or _path_key(prepared_binding.get("workspacePath", ""))
        != _path_key(workspace)
        or prepared_binding.get("captureId") != capture_id
        or prepared_binding.get("captureSessionId") != capture_session_id
        or prepared_binding.get("eventId") != event_id
        or prepared_binding.get("stage") != identity.get("stage")
        or prepared_binding.get("entryPoint", "")
        != identity.get("entryPoint", "")
        or prepared_binding.get("originalResourceId")
        != identity.get("resourceId")
        or prepared_binding.get("originalRawSha256")
        != identity.get("rawSha256")
        or not isinstance(prepared_created, Mapping)
        or prepared_created.get("admittedExisting") is not workspace_existed
        or prepared_created.get("sourcePreserved") is not workspace_existed
    ):
        raise RuntimeError(
            "target Shader workspace preparation changed the frozen representative EID"
        )
    status = _reuse_workspace_status(
        host,
        active,
        workspace,
        identity,
        capture_id,
        located_catalog,
    )
    return workspace, status, capture_id, located_catalog


def _compile_reuse_workspace(
    host: Any,
    active: Mapping[str, Any],
    workspace: Path,
    status: Mapping[str, Any],
    identity: Mapping[str, Any],
    capture_id: str,
    catalog_sha256: str | None,
    candidate_plan: Mapping[str, Any],
    progress: _Progress,
    position: int,
    total: int,
    control_checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if control_checkpoint is not None:
        control_checkpoint("before reused Shader candidate status")
    status = _reuse_workspace_status(
        host,
        active,
        workspace,
        identity,
        capture_id,
        catalog_sha256,
    )
    if control_checkpoint is not None:
        control_checkpoint("after reused Shader candidate status")
    expected_source_sha256 = _hex(
        candidate_plan.get("sourceSha256"), 64, "reuse sourceSha256"
    )
    _preserve_unpublished_include_edit(status)
    source = status.get("source")
    latest = status.get("latestCompiled")
    target_profile = status.get("targetProfile")
    if (
        not isinstance(source, Mapping)
        or source.get("currentSha256") != expected_source_sha256
        or not isinstance(target_profile, Mapping)
    ):
        raise RuntimeError("target Shader source/profile changed before candidate reuse")
    binary = candidate_plan.get("binaryCandidate")
    manual = isinstance(binary, Mapping) and binary.get("manualEdit") is True
    expected_compiler_binding_sha256: str | None = None
    if candidate_plan.get("binaryCandidateEligible") is True:
        source_revision = (
            binary.get("sourceRevision") if isinstance(binary, Mapping) else None
        )
        if (
            not isinstance(binary, Mapping)
            or binary.get("sourceSha256") != expected_source_sha256
            or isinstance(source_revision, bool)
            or not isinstance(source_revision, int)
            or source_revision <= 0
            or binary.get("targetProfile") != target_profile
        ):
            raise RuntimeError(
                "project compiled candidate changed source revision or target profile"
            )
        expected_compiler_binding_sha256 = _hex(
            binary.get("compilerBindingSha256"),
            64,
            "project compiled candidate compilerBindingSha256",
        )
    progress_detail = {
        "resourceId": identity["resourceId"],
        "stage": identity["stage"],
        "knowledgeId": candidate_plan.get("knowledgeId"),
    }
    existing_candidate_current = isinstance(source, Mapping) and isinstance(
        latest, Mapping
    ) and (
        latest.get("sourceSha256") == source.get("currentSha256")
        and status.get("sourceDependenciesCurrent", True) is True
        and latest.get("revision") == status.get("revision")
        and isinstance(latest.get("revision"), int)
        and latest.get("revision") > 0
        and isinstance(latest.get("rawSha256"), str)
    )
    existing_project_candidate_exact = True
    if existing_candidate_current and candidate_plan.get("binaryCandidateEligible") is True:
        # A target workspace can survive within the same project even though its
        # most recent candidate was produced with a different compiler route or
        # flags. Source/profile equality is not enough to bypass the reviewed
        # project artifact: require the immutable binary and its import receipt
        # to match every executable/configuration field before reusing it.
        receipt = latest.get("projectImport")
        expected_code_sha256 = binary.get("codeSha256") if isinstance(binary, Mapping) else None
        existing_project_candidate_exact = bool(
            isinstance(binary, Mapping)
            and isinstance(receipt, Mapping)
            and latest.get("rawSha256") == binary.get("sha256")
            and latest.get("codeSha256") == expected_code_sha256
            and latest.get("byteSize") == binary.get("byteSize")
            and str(latest.get("kind", "")).lower()
            == str(binary.get("kind", "")).lower()
            and receipt.get("artifactSha256") == binary.get("sha256")
            and receipt.get("codeSha256") == expected_code_sha256
            and receipt.get("byteSize") == binary.get("byteSize")
            and str(receipt.get("kind", "")).lower()
            == str(binary.get("kind", "")).lower()
            and receipt.get("sourceSha256") == expected_source_sha256
            and receipt.get("sourceRevision") == binary.get("sourceRevision")
            and receipt.get("targetProfile") == binary.get("targetProfile")
            and receipt.get("compilerBindingSha256")
            == expected_compiler_binding_sha256
            and receipt.get("originValidationEvidenceOnly") is True
            and receipt.get("freshCaptureRebindRequired") is True
            and receipt.get("freshLiveAbiRevalidationRequired") is True
            and receipt.get("applyAuthorization") is False
            and receipt.get("oldReplacementIdReused") is False
            and receipt.get("oldCaptureAbiAuthorityReused") is False
        )
        if manual:
            provenance = binary.get("manualApplyReceipt") or {}
            existing_project_candidate_exact = bool(
                latest.get("rawSha256") == binary.get("sha256")
                and latest.get("codeSha256") == binary.get("codeSha256")
                and latest.get("byteSize") == binary.get("byteSize")
                and latest.get("validationScope") == "manual-edit-unvalidated"
                and provenance.get("originalRawSha256") == (status.get("baseline") or {}).get("rawSha256")
                and provenance.get("candidateRawSha256") == latest.get("rawSha256")
                and provenance.get("sourceSha256") == expected_source_sha256
                and provenance.get("stage") == status.get("stage")
                and provenance.get("entryPoint", "") == status.get("entryPoint", "")
                and provenance.get("targetProfile") == target_profile)
    # Incomplete project reflection prevents importing a cross-workspace binary
    # or publishing an executable RDC index. It does not invalidate the exact
    # target workspace's own compiled source. status() already checks the
    # candidate bytes, original Shader, profile and bound compiler provenance;
    # the apply path below still obtains current-session ABI/anchor authority.
    # Recompiling this unchanged source on every open only creates identical
    # revisions (and source-only restores intentionally never acquire an index).
    if existing_candidate_current and existing_project_candidate_exact:
        _hex(latest.get("rawSha256"), 64, "existing candidateRawSha256")
        progress.emit(
            "shader-candidate-load",
            "Reusing the target workspace Shader candidate %d/%d"
            % (position, total),
            current=position,
            total=total,
            detail=progress_detail,
        )
        return dict(status)
    workspace_service = (
        host._shader_workspace_service()
        if hasattr(host, "_shader_workspace_service")
        else None
    )
    importer = getattr(workspace_service, "import_project_candidate", None)
    if (
        candidate_plan.get("binaryCandidateEligible") is True
        and isinstance(binary, Mapping)
        and callable(importer)
    ):
        progress.emit(
            "shader-candidate-load",
            "Loading the verified project Shader candidate %d/%d"
            % (position, total),
            current=position,
            total=total,
            detail=progress_detail,
        )
        try:
            if control_checkpoint is not None:
                control_checkpoint("before project Shader candidate import")
            imported = importer(str(workspace), binary)
            if control_checkpoint is not None:
                control_checkpoint("after project Shader candidate import")
            if (
                not isinstance(imported, Mapping)
                or imported.get("workspacePath") != str(workspace)
                or imported.get("sourceSha256")
                != status.get("source", {}).get("currentSha256")
                or imported.get("compilerSkipped") is not True
                or imported.get("compiledFromProjectArtifact") is not True
                or imported.get("requiresReplayAbiValidation") is not (not manual)
                or imported.get("replayAbiValidated") is not False
                or imported.get("applied") is not False
            ):
                raise RuntimeError("project candidate import returned invalid evidence")
            _hex(imported.get("rawSha256"), 64, "imported candidateRawSha256")
            if control_checkpoint is not None:
                control_checkpoint("before imported Shader candidate status")
            after_import = _reuse_workspace_status(
                host,
                active,
                workspace,
                identity,
                capture_id,
                catalog_sha256,
            )
            if control_checkpoint is not None:
                control_checkpoint("after imported Shader candidate status")
            imported_latest = after_import.get("latestCompiled")
            imported_receipt = (
                imported_latest.get("projectImport")
                if isinstance(imported_latest, Mapping)
                else None
            )
            if (
                not isinstance(imported_latest, Mapping)
                or imported_latest.get("rawSha256") != imported.get("rawSha256")
                or imported_latest.get("rawSha256") != binary.get("sha256")
                or imported_latest.get("codeSha256") != binary.get("codeSha256")
                or imported_latest.get("byteSize") != binary.get("byteSize")
                or str(imported_latest.get("kind", "")).lower()
                != str(binary.get("kind", "")).lower()
                or imported_latest.get("sourceSha256")
                != after_import.get("source", {}).get("currentSha256")
                or imported_latest.get("revision") != after_import.get("revision")
                or not isinstance(imported_receipt, Mapping)
                or imported_receipt.get("artifactSha256") != binary.get("sha256")
                or imported_receipt.get("codeSha256") != binary.get("codeSha256")
                or imported_receipt.get("byteSize") != binary.get("byteSize")
                or str(imported_receipt.get("kind", "")).lower()
                != str(binary.get("kind", "")).lower()
                or imported_receipt.get("sourceSha256") != expected_source_sha256
                or imported_receipt.get("sourceRevision")
                != binary.get("sourceRevision")
                or imported_receipt.get("targetProfile")
                != binary.get("targetProfile")
                or imported_receipt.get("compilerBindingSha256")
                != expected_compiler_binding_sha256
                or imported_receipt.get("originValidationEvidenceOnly") is not (not manual)
                or imported_receipt.get("freshCaptureRebindRequired") is not True
                or imported_receipt.get("freshLiveAbiRevalidationRequired") is not (not manual)
                or imported_receipt.get("applyAuthorization") is not False
                or imported_receipt.get("oldReplacementIdReused") is not False
                or imported_receipt.get("oldCaptureAbiAuthorityReused") is not False
            ):
                raise RuntimeError(
                    "project candidate import did not bind the target workspace"
                )
            return after_import
        except RestoreControlStop:
            raise
        except Exception as exc:
            # A project binary is only a compile optimisation. The managed
            # workspace service imports it transactionally; rejection never
            # weakens the source-compile and fresh-live-ABI fallback.
            progress_detail = {
                **progress_detail,
                "fallbackReason": (
                    "%s: %s" % (type(exc).__name__, exc)
                )[:1024],
            }
    if control_checkpoint is not None:
        control_checkpoint("before fallback Shader compile status")
    status = _reuse_workspace_status(
        host,
        active,
        workspace,
        identity,
        capture_id,
        catalog_sha256,
    )
    if control_checkpoint is not None:
        control_checkpoint("after fallback Shader compile status")
    if status.get("source", {}).get("currentSha256") != candidate_plan.get(
        "sourceSha256"
    ):
        raise RuntimeError("target Shader source changed before fallback compile")
    progress.emit(
        "shader-compile",
        "Compiling project Shader source %d/%d" % (position, total),
        current=position,
        total=total,
        detail=progress_detail,
    )
    if control_checkpoint is not None:
        control_checkpoint("before fallback Shader compile")
    before_compile_revision = _integer(status.get("revision"), "pre-compile revision")
    if manual:
        compiled = {"operation": "compile", "result":
                    host._shader_workspace_service().compile(str(workspace), manual_fast=True)}
    else:
        compiled = _tool_json(
            host.compile_shader_workspace(str(workspace)),
            "compile reused Shader source",
            schema="renderdoc-ultra-shader-workspace-mcp/v1",
        )
    if control_checkpoint is not None:
        control_checkpoint("after fallback Shader compile")
    if compiled.get("operation") != "compile":
        raise RuntimeError("reused Shader compile returned invalid evidence")
    if control_checkpoint is not None:
        control_checkpoint("before compiled Shader candidate status")
    after = _reuse_workspace_status(
        host,
        active,
        workspace,
        identity,
        capture_id,
        catalog_sha256,
    )
    if control_checkpoint is not None:
        control_checkpoint("after compiled Shader candidate status")
    source = after.get("source")
    latest = after.get("latestCompiled")
    if (
        not isinstance(source, Mapping)
        or not isinstance(latest, Mapping)
        or after.get("sourceDependenciesCurrent", True) is not True
        or source.get("currentSha256") != candidate_plan.get("sourceSha256")
        or latest.get("sourceSha256") != source.get("currentSha256")
        or latest.get("revision") != after.get("revision")
        or (candidate_plan.get("sourcePreparationOnly") is True
            and latest.get("revision") != before_compile_revision + 1)
    ):
        raise RuntimeError("reused Shader compile did not publish a bound candidate")
    _hex(latest.get("rawSha256"), 64, "compiled candidateRawSha256")
    return after


def _reuse_material_record(
    prepared: Mapping[str, Any],
    identity_source: Mapping[str, Any],
    candidate: Mapping[str, Any],
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    identity = _normal_shader_identity(identity_source)
    staged = prepared["stagedCatalog"]
    catalog = _strict_fast_catalog(
        str(staged.get("catalogPath", "")), str(staged["catalogSha256"])
    )
    catalog_api = catalog.get("api", "")
    capture = catalog.get("capture")
    if not catalog_api and isinstance(capture, Mapping):
        catalog_api = capture.get("api", "")
    expected = _expected_resource_shader(
        catalog,
        identity_source,
        _asset_api(catalog_api),
        _shader_anchor_event_id(identity),
        inventory=inventory,
    )
    matches = [
        block
        for block in expected["constantBlocks"]
        if block.get("path") == candidate.get("constantBlockPath")
        and block.get("layoutSha256")
        == candidate.get("constantBlockLayoutSha256")
    ]
    if len(matches) != 1:
        raise RuntimeError("material reuse candidate has no unique target CBuffer layout")
    return {
        "knowledgeId": _hex(
            candidate.get("knowledgeId"), 32, "material knowledgeId"
        ),
        "shaderResourceId": identity["resourceId"],
        "stage": identity["stage"],
        "blockIndex": matches[0]["index"],
        "path": matches[0]["path"],
        "layoutSha256": matches[0]["layoutSha256"],
        "eventId": _shader_anchor_event_id(identity),
        "parameters": [],
    }


def _validate_alias_rollback_receipt(
    receipt: Any,
    *,
    capture_id: str,
    capture_session_id: str,
    transaction_id: str,
) -> dict[str, Any]:
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema") != "renderdoc-ultra-alias-rollback/v1"
        or receipt.get("schemaVersion") != 1
        or receipt.get("captureId") != capture_id
        or receipt.get("captureSessionId") != capture_session_id
        or receipt.get("transactionId") != transaction_id
        or receipt.get("status") not in {"restored", "already-restored"}
        or receipt.get("restored") is not True
        or not isinstance(receipt.get("alreadyRestored"), bool)
        or receipt.get("verified") is not True
        or receipt.get("strictReceipt") is not True
        or receipt.get("captureTokenVerified") is not True
        or receipt.get("originalNamesVerified") is not True
        or receipt.get("recoveryRequired") is not False
        or receipt.get("overlayRestored") is not True
        or receipt.get("renameLedgerRestored") is not True
        or not isinstance(receipt.get("failures"), list)
        or receipt.get("failures")
    ):
        raise RestoreRollbackFailed(
            "RenderDoc returned invalid strict alias rollback evidence"
        )
    return dict(receipt)


def _rollback_alias_transaction(
    host: Any,
    *,
    capture_id: str,
    capture_session_id: str,
    transaction_id: str,
) -> dict[str, Any]:
    receipt = host.ipc.call(
        "rollback_symbol_aliases",
        {
            "expected_capture_id": _hex(
                capture_id, 64, "alias rollback captureId"
            ),
            "expected_capture_session_id": _hex(
                capture_session_id, 32, "alias rollback captureSessionId"
            ),
            "expected_transaction_id": _hex(
                transaction_id, 32, "alias rollback transactionId"
            ),
        },
        timeout=60.0,
    )
    return _validate_alias_rollback_receipt(
        receipt,
        capture_id=capture_id,
        capture_session_id=capture_session_id,
        transaction_id=transaction_id,
    )


def _recover_uncertain_alias_load(
    host: Any,
    *,
    capture_id: str,
    capture_session_id: str,
    transaction_id: str,
    manifest_sha256: str,
) -> dict[str, Any]:
    query_error: Exception | None = None
    try:
        status = host.ipc.call(
            "get_symbol_alias_transaction",
            {
                "expected_capture_id": capture_id,
                "expected_capture_session_id": capture_session_id,
                "transaction_id": transaction_id,
            },
            timeout=30.0,
        )
        if (
            not isinstance(status, Mapping)
            or status.get("schema")
            != "renderdoc-ultra-alias-transaction-status/v1"
            or status.get("schemaVersion") != 1
            or status.get("captureId") != capture_id
            or status.get("captureSessionId") != capture_session_id
            or status.get("transactionId") not in {"", transaction_id}
            or status.get("state")
            not in {"absent", "pending", "committed", "rolled-back"}
            or not isinstance(status.get("mutationPresent"), bool)
            or not isinstance(status.get("recoveryRequired"), bool)
            or status.get("replayMutation") is not False
            or (
                status.get("state") in {"pending", "committed"}
                and (
                    status.get("transactionId") != transaction_id
                    or status.get("mutationPresent") is not True
                    or status.get("recoveryRequired") is not True
                    or status.get("manifestSha256") != manifest_sha256
                )
            )
            or (
                status.get("state") in {"absent", "rolled-back"}
                and status.get("mutationPresent") is not False
            )
        ):
            raise RuntimeError(
                "RenderDoc returned invalid strict alias transaction status"
            )
        if status.get("mutationPresent") is False:
            return dict(status)
    except Exception as exc:
        query_error = exc

    try:
        return _rollback_alias_transaction(
            host,
            capture_id=capture_id,
            capture_session_id=capture_session_id,
            transaction_id=transaction_id,
        )
    except Exception as rollback_exc:
        diagnostic = (
            "strict alias response recovery failed"
            + (
                "; query failed: %s: %s" % (type(query_error).__name__, query_error)
                if query_error is not None
                else ""
            )
            + "; rollback failed: %s: %s"
            % (type(rollback_exc).__name__, rollback_exc)
        )
        raise RestoreRollbackFailed(diagnostic[:4096]) from rollback_exc


def _rollback_restored_alias_transactions(
    host: Any, restored_alias_transactions: list[dict[str, str]]
) -> str:
    failures: list[str] = []
    retryable: list[dict[str, str]] = []
    while restored_alias_transactions:
        transaction = restored_alias_transactions.pop()
        try:
            _rollback_alias_transaction(
                host,
                capture_id=transaction["captureId"],
                capture_session_id=transaction["captureSessionId"],
                transaction_id=transaction["transactionId"],
            )
        except Exception as exc:
            failures.append("%s: %s" % (type(exc).__name__, exc))
            retryable.append(transaction)
    # Preserve failed entries in their original order. The Bridge retains the
    # same capture-bound ledger, so a later cleanup attempt can only retry the
    # exact transaction rather than losing its recovery authority.
    restored_alias_transactions.extend(reversed(retryable))
    return "; ".join(failures)[:4096]


def _restore_reuse_aliases(
    host: Any,
    replay: ReplayProjectService,
    active: Mapping[str, Any],
    project_root: Path,
    capture_path: Path,
    capture_session_id: str,
    plan: Mapping[str, Any],
    matches: Sequence[Mapping[str, Any]],
    progress: _Progress,
    control_checkpoint: Callable[[str], None] | None = None,
    inventory: Mapping[str, Any] | None = None,
    inventory_binding: Mapping[str, Any] | None = None,
    restored_alias_transactions: list[dict[str, str]] | None = None,
) -> None:
    targets: list[Mapping[str, Any]] = []
    candidate_count = 0
    for match in matches:
        if not isinstance(match, Mapping):
            continue
        raw_candidates = match.get("aliasCandidates", [])
        if raw_candidates is None:
            raw_candidates = []
        if not isinstance(raw_candidates, list):
            raise RuntimeError("project alias candidates are not a bounded array")
        if not raw_candidates:
            continue
        if not isinstance(match.get("shader"), Mapping):
            raise RuntimeError("project alias target has no Shader identity")
        candidate_count += len(raw_candidates)
        if candidate_count > _MAX_RESTORE_ALIAS_CANDIDATES:
            raise RuntimeError("project alias candidate restore exceeds the safe limit")
        targets.append(match)
        if len(targets) > _MAX_RESTORE_ALIAS_TARGETS:
            raise RuntimeError("project alias target restore exceeds the safe limit")
    if not targets:
        return
    if control_checkpoint is not None:
        control_checkpoint("before project alias materialization")
    inventory_mode = plan.get("policy", {}).get("inventoryOnly") is True
    materialize_batch = getattr(replay, "materialize_alias_batch", None)
    materialize_inventory_batch = getattr(
        replay, "materialize_alias_batch_from_inventory", None
    )
    materialize = getattr(replay, "materialize_alias_candidates", None)
    if inventory_mode:
        if (
            not callable(materialize_inventory_batch)
            or not isinstance(inventory, Mapping)
            or not isinstance(inventory_binding, Mapping)
        ):
            raise RuntimeError(
                "Replay project has no verified inventory alias materialization route"
            )
    elif not callable(materialize_batch) and not callable(materialize):
        raise RuntimeError("Replay project has no verified alias materialization route")
    progress.emit(
        "alias-restore",
        "Rebinding verified Shader and CBuffer aliases",
        current=0,
        total=len(targets),
    )
    tables: list[dict[str, Any]] = []
    all_aliases: list[dict[str, Any]] = []
    all_shaders: list[dict[str, Any]] = []
    physical_targets: dict[tuple[Any, ...], str] = {}
    project_id = _hex(plan.get("projectId"), 32, "alias projectId")
    capture_id = _hex(plan.get("captureId"), 64, "alias captureId")
    catalog_sha256 = _hex(
        plan.get("catalogSha256"), 64, "alias catalogSha256"
    )
    materialization_targets: list[dict[str, Any]] = []
    inventory_shaders = (
        inventory.get("shaders", []) if isinstance(inventory, Mapping) else []
    )
    for match in targets:
        target_shader: Mapping[str, Any] = match["shader"]
        if inventory_mode:
            exact = [
                value
                for value in inventory_shaders
                if isinstance(value, Mapping)
                and value.get("resourceId") == target_shader.get("resourceId")
                and value.get("stableKeySha256")
                == target_shader.get("stableKeySha256")
            ]
            if len(exact) != 1:
                raise RuntimeError(
                    "project alias target is absent or ambiguous in the live inventory"
                )
            target_shader = exact[0]
        materialization_targets.append(
            {
                "shader": target_shader,
                "candidates": list(match.get("aliasCandidates", []) or []),
            }
        )
    if inventory_mode:
        batch = materialize_inventory_batch(
            project_id,
            inventory,
            inventory_binding,
            materialization_targets,
        )
        materializations = (
            batch.get("materializations") if isinstance(batch, Mapping) else None
        )
    elif callable(materialize_batch):
        batch = materialize_batch(
            project_id,
            capture_id,
            catalog_sha256,
            materialization_targets,
        )
        materializations = (
            batch.get("materializations") if isinstance(batch, Mapping) else None
        )
    else:
        materializations = []
        for match in targets:
            if control_checkpoint is not None:
                control_checkpoint("before project alias target materialization")
            materializations.append(
                materialize(
                    project_id,
                    capture_id,
                    catalog_sha256,
                    match["shader"],
                    list(match.get("aliasCandidates", []) or []),
                )
            )
            if control_checkpoint is not None:
                control_checkpoint("after project alias target materialization")
        batch = None
    if batch is not None and (
        not isinstance(batch, Mapping)
        or batch.get("schema")
        != "renderdoc-ultra-project-alias-materialization-batch/v1"
        or batch.get("schemaVersion") != 1
        or batch.get("projectId") != project_id
        or batch.get("captureId") != capture_id
        or batch.get("catalogSha256") != catalog_sha256
        or batch.get("targetCount") != len(targets)
        or batch.get("candidateCount") != candidate_count
        or batch.get("displayAliasesOnly") is not True
        or batch.get("applied") is not False
        or batch.get("projectChangedDuringBatch") is not False
        or (inventory_mode and batch.get("inventoryOnly") is not True)
        or not isinstance(materializations, list)
        or len(materializations) != len(targets)
    ):
        raise RuntimeError("project alias batch returned invalid evidence")
    if control_checkpoint is not None:
        control_checkpoint("after project alias materialization")
    for position, (match, materialized) in enumerate(
        zip(targets, materializations), 1
    ):
        table = materialized.get("aliasTable") if isinstance(materialized, Mapping) else None
        aliases = table.get("aliases") if isinstance(table, Mapping) else None
        shaders = table.get("shaders") if isinstance(table, Mapping) else None
        raw = materialized.get("jsonBytes") if isinstance(materialized, Mapping) else None
        if (
            not isinstance(materialized, Mapping)
            or materialized.get("schema")
            != "renderdoc-ultra-project-alias-materialization/v1"
            or materialized.get("schemaVersion") != 1
            or materialized.get("projectId") != project_id
            or materialized.get("captureId") != capture_id
            or materialized.get("catalogSha256") != catalog_sha256
            or materialized.get("displayAliasesOnly") is not True
            or (
                inventory_mode
                and materialized.get("inventoryOnly") is not True
            )
            or materialized.get("applied") is not False
            or not isinstance(raw, bytes)
            or hashlib.sha256(raw).hexdigest() != materialized.get("sha256")
            or not isinstance(table, Mapping)
            or table.get("schema") != "renderdoc-ultra-aliases/v1"
            or table.get("schemaVersion") != 1
            or table.get("captureId") != capture_id
            or table.get("catalogSha256") != catalog_sha256
            or table.get("verifiedOnly") is not True
            or not isinstance(aliases, list)
            or not aliases
            or len(aliases) != materialized.get("aliasCount")
            or not isinstance(shaders, list)
        ):
            raise RuntimeError("project alias materialization returned invalid evidence")
        for alias in aliases:
            if not isinstance(alias, Mapping) or alias.get("verified") is not True:
                raise RuntimeError("project alias materialization emitted an unverified alias")
            kind = str(alias.get("bindingKind", "") or "").strip().lower()
            target: tuple[Any, ...] = (
                kind,
                alias.get("shaderResourceId"),
                alias.get("stage"),
                alias.get("entryPoint", ""),
                alias.get("rawSha256", ""),
            )
            if kind in {"constantbuffer", "constantfield"}:
                target += (
                    alias.get("registerSpace", 0),
                    alias.get("bindNumber", 0),
                )
            if kind == "constantfield":
                target += (alias.get("byteOffset", 0),)
            name = str(alias.get("alias", "") or "")
            previous = physical_targets.setdefault(target, name)
            if previous != name:
                raise RuntimeError(
                    "project aliases conflict for one target-capture binding"
                )
            all_aliases.append(copy.deepcopy(dict(alias)))
            if len(all_aliases) > _MAX_RESTORE_ALIASES:
                raise RuntimeError("project alias restore exceeds the loader alias limit")
        all_shaders.extend(copy.deepcopy(list(shaders)))
        tables.append(copy.deepcopy(dict(table)))
        progress.emit(
            "alias-restore",
            "Rebinding verified Shader and CBuffer aliases %d/%d"
            % (position, len(targets)),
            current=position,
            total=len(targets),
            detail={"aliasCount": len(all_aliases)},
        )
    if isinstance(batch, Mapping) and batch.get("aliasCount") != len(all_aliases):
        raise RuntimeError("project alias batch alias count is inconsistent")
    merged = copy.deepcopy(tables[0])
    merged["aliases"] = all_aliases
    merged["shaders"] = all_shaders
    merged["projectMaterialization"] = {
        "schema": "renderdoc-ultra-project-alias-materialization/v1",
        "schemaVersion": 1,
        "projectId": project_id,
        "captureId": capture_id,
        "catalogSha256": catalog_sha256,
        "targetCount": len(targets),
        "displayAliasesOnly": True,
        "captureLocalReplayStateReused": False,
    }
    encoded = (
        json.dumps(
            merged,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if not encoded or len(encoded) > 32 * 1024 * 1024:
        raise RuntimeError("merged project alias table exceeds the loader limit")
    digest = hashlib.sha256(encoded).hexdigest()
    _require_restore_context(
        host, active, project_root, capture_path, capture_session_id
    )
    aliases_root = host._active_project_service().shared_dictionary_directory(
        "aliases"
    )
    if aliases_root.is_symlink() or not aliases_root.is_dir():
        raise RuntimeError("active project alias dictionary is unavailable")
    aliases_root = aliases_root.resolve()
    alias_path = (
        aliases_root
        / ("%s-%s.project-reuse.json" % (capture_id[:16], digest))
    ).resolve()
    if not is_within(aliases_root, alias_path) or alias_path.is_symlink():
        raise RuntimeError("capture-local alias path escaped the project")
    atomic_write_bytes(alias_path, encoded)
    if (
        alias_path.is_symlink()
        or not alias_path.is_file()
        or hashlib.sha256(alias_path.read_bytes()).hexdigest() != digest
    ):
        raise RuntimeError("capture-local alias table failed post-write verification")
    if control_checkpoint is not None:
        control_checkpoint("before capture-local alias overlay load")
    progress.emit(
        "resource-anchor",
        "Validating one representative EID per Shader",
        current=0,
        total=len(targets),
        detail={
            "purpose": "Shader and CBuffer alias validation",
            "eventTraversalCount": 0,
        },
    )
    alias_operation_id = uuid.uuid4().hex
    try:
        overlay = host.ipc.call(
            "load_symbol_aliases",
            {
                "path": str(alias_path),
                "confirmed_only": True,
                "expected_sha256": digest,
                "strict_atomic": True,
                "operation_id": alias_operation_id,
            },
            timeout=60.0,
        )
        counts = overlay.get("counts") if isinstance(overlay, Mapping) else None
        if (
            not isinstance(overlay, dict)
            or overlay.get("schema") != "renderdoc-ultra-aliases/v1"
            or overlay.get("schemaVersion") != 1
            or overlay.get("captureId") != capture_id
            or overlay.get("captureSessionId") != capture_session_id
            or overlay.get("operationId") != alias_operation_id
            or overlay.get("transactionId") != alias_operation_id
            or overlay.get("confirmedOnly") is not True
            or overlay.get("strictAtomic") is not True
            or overlay.get("committed") is not True
            or overlay.get("recoveryRequired") is not False
            or overlay.get("rollbackAvailable") is not True
            or overlay.get("recoveryLedgerPublished") is not True
            or not isinstance(counts, Mapping)
            or counts.get("manifestAliases") != len(all_aliases)
            or counts.get("activeValidatedAliases") != len(all_aliases)
            or counts.get("skipped") != 0
            or counts.get("conflicts") != 0
        ):
            raise RuntimeError(
                "RenderDoc rejected one or more verified project aliases"
            )
    except Exception as load_exc:
        try:
            recovery = _recover_uncertain_alias_load(
                host,
                capture_id=capture_id,
                capture_session_id=capture_session_id,
                transaction_id=alias_operation_id,
                manifest_sha256=digest,
            )
        except RestoreRollbackFailed:
            raise
        raise RuntimeError(
            "capture-local alias overlay failed or returned invalid evidence; "
            "transaction %s was fail-safe recovered with status %s (%s: %s)"
            % (
                alias_operation_id,
                recovery.get("status"),
                type(load_exc).__name__,
                load_exc,
            )
        ) from load_exc
    if restored_alias_transactions is not None:
        restored_alias_transactions.append(
            {
                "captureId": capture_id,
                "captureSessionId": capture_session_id,
                "transactionId": alias_operation_id,
            }
        )
    if control_checkpoint is not None:
        control_checkpoint("after capture-local alias overlay load")
    progress.emit(
        "resource-anchor",
        "Representative Shader EID validation completed",
        current=len(targets),
        total=len(targets),
        detail={
            "purpose": "Shader and CBuffer alias validation",
            "eventTraversalCount": 0,
        },
    )
    _require_restore_context(
        host, active, project_root, capture_path, capture_session_id
    )


def _restore_reuse_aliases_best_effort(
    host: Any,
    replay: ReplayProjectService,
    active: Mapping[str, Any],
    project_root: Path,
    capture_path: Path,
    capture_session_id: str,
    plan: Mapping[str, Any],
    matches: Sequence[Mapping[str, Any]],
    progress: _Progress,
    control_checkpoint: Callable[[str], None] | None = None,
    inventory: Mapping[str, Any] | None = None,
    inventory_binding: Mapping[str, Any] | None = None,
    restored_alias_transactions: list[dict[str, str]] | None = None,
) -> bool:
    """Restore display-only aliases without blocking executable reuse."""

    try:
        _restore_reuse_aliases(
            host,
            replay,
            active,
            project_root,
            capture_path,
            capture_session_id,
            plan,
            matches,
            progress,
            control_checkpoint,
            inventory,
            inventory_binding,
            restored_alias_transactions,
        )
        return True
    except RestoreRollbackFailed:
        raise
    except RestoreControlStop:
        raise
    except Exception as exc:
        # Never downgrade an active capture/project lifecycle failure to a
        # cosmetic warning. Revalidation raises here when the context changed.
        _require_restore_context(
            host, active, project_root, capture_path, capture_session_id
        )
        diagnostic = (
            "Project display aliases were not restored: %s: %s"
            % (type(exc).__name__, exc)
        )[:4096]
        if diagnostic not in progress.warnings:
            progress.warnings.append(diagnostic)
        progress.emit(
            "alias-restore",
            "Project display aliases could not be restored; continuing with Shader and CBuffer reuse",
            detail={"status": "failed", "error": diagnostic[:1024]},
        )
        return False


def _pending_project_source_item(
    host: Any, knowledge: Mapping[str, Any], identity: Mapping[str, Any], reason: str,
    *, project_id: str, capture_id: str, capture_path: Path, capture_session_id: str,
    replay_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Restore a saved draft's editor without granting replay/watch authority."""
    item = {"status": "skipped", "source": "reuse-plan",
            "resourceId": identity["resourceId"], "stage": identity["stage"],
            "knowledgeId": knowledge["knowledgeId"], "reason": reason,
            "knowledgeSourceSha256": knowledge["sourceSha256"]}
    workspace = Path(str(knowledge.get("workspacePath", "")))
    try:
        # The immutable project source, not the continued existence of its
        # original editor file, owns the shared head. The Host verifies that
        # artifact and opens/creates a current-target working copy with CAS.
        show = getattr(host, "_show_project_shader_source", None)
        if not callable(show):
            raise RuntimeError("saved source display is unavailable")
        shown = _tool_json(show(str(workspace), knowledge["sourceSha256"],
            project_id=project_id, knowledge_id=knowledge["knowledgeId"], identity=identity,
            capture_id=capture_id, capture_path=str(capture_path), capture_session_id=capture_session_id,
            activate_window=False,
            **({"replay_source": replay_source} if replay_source is not None else {})),
            "show pending project Shader source")
        if shown.get("shown") is not True:
            raise RuntimeError("saved project Shader source was not displayed")
        display_binding = shown.get("projectSourceBinding") or {}
        if display_binding:
            item["resourceId"] = display_binding["resourceId"]
        item.update(workspacePath=str(shown.get("sourceWorkspacePath") or workspace),
                    workspaceSourceSha256=knowledge["sourceSha256"],
                    detail="Saved source opened for editing; compile, validation and replay application remain pending")
    except Exception as error:
        item["detail"] = ("Source-only draft was not applied; " + str(error))[:4096]
    return item


def _fallback_pending_knowledge(candidate: Mapping[str, Any], state: Mapping[str, Any]) -> Mapping[str, Any] | None:
    pending = candidate.get("pendingSource")
    if pending is None:
        return None
    if not isinstance(pending, Mapping):
        raise RuntimeError("project pending source binding is invalid")
    knowledge = state.get("workspaces", {}).get(pending.get("knowledgeId"))
    applied = state.get("workspaces", {}).get(candidate.get("knowledgeId"))
    if (not isinstance(knowledge, Mapping) or not isinstance(applied, Mapping)
            or knowledge.get("sourceSha256") != pending.get("sourceSha256")
            or knowledge.get("knowledgeId") == applied.get("knowledgeId")
            or any(knowledge.get(key) != applied.get(key) for key in (
                "rawSha256", "stage", "entryPoint", "targetProfile"))
            or source_only_restore_reason(knowledge, state.get("projectPath")) != pending.get("reason")):
        raise RuntimeError("project pending source differs from its validated replay head")
    return knowledge


def _show_fallback_pending_source(host: Any, candidate: Mapping[str, Any], state: Mapping[str, Any],
    identity: Mapping[str, Any], capture_id: str, capture_path: Path, session: str) -> dict[str, Any] | None:
    pending = candidate.get("pendingSource")
    if pending is None:
        return None
    # The relationship is admitted before applying. Display cannot turn a
    # committed valid replacement into a second failed apply if a source-only
    # diagnostic changes while the editor is opening.
    knowledge = state["workspaces"][pending["knowledgeId"]]
    shown = _pending_project_source_item(host, knowledge, identity, candidate["pendingSource"]["reason"],
        project_id=state["projectId"], capture_id=capture_id, capture_path=capture_path,
        capture_session_id=session, replay_source={"knowledgeId": candidate["knowledgeId"],
            "sourceSha256": candidate["sourceSha256"], "applied": True})
    return {"knowledgeId": knowledge["knowledgeId"], "sourceSha256": knowledge["sourceSha256"],
            "reason": candidate["pendingSource"]["reason"], "shown": bool(shown.get("workspacePath")),
            **({"workspacePath": shown["workspacePath"]} if shown.get("workspacePath") else {})}


def _restore_from_reuse_plan(
    host: Any,
    replay: ReplayProjectService,
    active: dict[str, Any],
    project_root: Path,
    capture_path: Path,
    capture_session_id: str,
    prepared: Mapping[str, Any],
    inventory: Mapping[str, Any],
    progress: _Progress,
    shader_summary: dict[str, Any],
    material_summary: dict[str, Any],
    applied_workspaces: list[
        tuple[Path, Mapping[str, Any], Mapping[str, Any], str]
    ],
    restored_material_sessions: list[tuple[str, str, str]],
    restored_alias_transactions: list[dict[str, str]],
    control_checkpoint: Callable[[str], None] | None = None,
    realized_receipts: list[dict[str, Any]] | None = None,
    receipt_stats: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Materialize target-local workspaces/sessions from an exact reuse plan."""

    if realized_receipts is None:
        realized_receipts = []

    plan = prepared["reusePlan"]
    if control_checkpoint is not None:
        control_checkpoint("before prepared project binding snapshot")
    binding_state = _prepared_binding_state(replay, active, prepared)
    if control_checkpoint is not None:
        control_checkpoint("after prepared project binding snapshot")
    matches = list(plan.get("matches", []) or [])
    normalised_matches: list[
        tuple[
            Mapping[str, Any],
            dict[str, Any],
            list[Mapping[str, Any]],
        ]
    ] = []
    executable_resource_match_counts: dict[int, int] = {}
    for match in matches:
        if not isinstance(match, Mapping) or not isinstance(
            match.get("shader"), Mapping
        ):
            raise RuntimeError("project reuse plan contains an invalid Shader match")
        identity = _normal_shader_identity(match["shader"])
        workspace_candidates = [
            item
            for item in list(match.get("workspaceCandidates", []) or [])
            if isinstance(item, Mapping)
        ]
        active_candidates = [
            item
            for item in workspace_candidates
            if item.get("activeCandidate") is True
        ]
        if not active_candidates and len(workspace_candidates) == 1:
            active_candidates = [workspace_candidates[0]]
        if (
            len(active_candidates) == 1
            and active_candidates[0].get("sourceSeedEligible") is True
        ):
            resource_id = identity["resourceId"]
            executable_resource_match_counts[resource_id] = (
                executable_resource_match_counts.get(resource_id, 0) + 1
            )
        normalised_matches.append((match, identity, active_candidates))
    successful_shader_records: list[dict[str, Any]] = []
    successful_material_records: list[dict[str, Any]] = []
    capture_id = str(plan["captureId"])
    catalog_sha256 = str(plan["catalogSha256"])
    inventory_mode = (
        prepared.get("schema") == "replaylab-active-shader-inventory-prepare/v1"
    )

    _restore_reuse_aliases_best_effort(
        host,
        replay,
        active,
        project_root,
        capture_path,
        capture_session_id,
        plan,
        matches,
        progress,
        control_checkpoint,
        prepared.get("inventory") if inventory_mode else None,
        prepared.get("binding") if inventory_mode else None,
        restored_alias_transactions,
    )

    for position, (match, identity, active_candidates) in enumerate(
        normalised_matches, 1
    ):
        if control_checkpoint is not None:
            control_checkpoint("before project reuse-plan Shader match")
        _capture_shader(binding_state, identity)
        expected_anchor_event_id = _shader_anchor_event_id(identity)
        # A resource-level replacement may run at most once for one physical
        # ResourceId.  Multiple entry points may all keep their project HLSL
        # and CBuffer alias metadata, but two executable candidates for the
        # same module are ambiguous and therefore fail closed as a group.
        executable_conflict = (
            executable_resource_match_counts.get(identity["resourceId"], 0) > 1
        )
        if executable_conflict:
            active_candidates = []
        if len(active_candidates) != 1:
            shader_summary["skipped"] += 1
            shader_summary["items"].append(
                {
                    "status": "skipped",
                    "source": "reuse-plan",
                    "resourceId": identity["resourceId"],
                    "stage": identity["stage"],
                    "reason": (
                        "multiple executable entry points target one physical Shader resource"
                        if executable_conflict
                        else (
                            "project-shader-stable-identity-mismatch"
                            if match.get("sourcePreparationBlockedReason")
                            == "project-shader-stable-identity-mismatch"
                            else "no unique active project HLSL candidate matched"
                        )
                    ),
                }
            )
        else:
            candidate = active_candidates[0]
            source_preparation_only = candidate.get("sourcePreparationOnly") is True
            knowledge_id = _hex(
                candidate.get("knowledgeId"), 32, "workspace knowledgeId"
            )
            knowledge = binding_state.get("workspaces", {}).get(knowledge_id)
            _fallback_pending_knowledge(candidate, binding_state)
            pending_reason = (source_only_restore_reason(knowledge, binding_state.get("projectPath"))
                              if isinstance(knowledge, Mapping) else None)
            if pending_reason:
                shader_summary["skipped"] += 1
                shader_summary["items"].append(
                    _pending_project_source_item(host, knowledge, identity, pending_reason,
                        project_id=binding_state["projectId"], capture_id=capture_id,
                        capture_path=capture_path, capture_session_id=capture_session_id))
                continue
            if (
                candidate.get("sourceSeedEligible") is not True
                or identity.get("anchorEventId") is None
                or not isinstance(knowledge, Mapping)
                or (source_preparation_only and (
                    not inventory_mode
                    or plan.get("policy", {}).get("rawExactSourcePreparationAllowed") is not True
                    or plan.get("policy", {}).get("stableKeyExactRequiredForBinary") is not True
                    or not _source_preparation_matches_shader(knowledge, identity, candidate)
                ))
                or (not source_preparation_only and (
                    identity.get("stableKeyComplete") is not True
                    or not _knowledge_matches_shader(knowledge, identity, executable=True)
                ))
                or knowledge.get("sourceSha256") != candidate.get("sourceSha256")
            ):
                shader_summary["skipped"] += 1
                shader_summary["items"].append(
                    {
                        "status": "skipped",
                        "source": "reuse-plan",
                        "resourceId": identity["resourceId"],
                        "stage": identity["stage"],
                        "reason": "matched project HLSL is not executable source-seed knowledge",
                    }
                )
            else:
                shader_summary["attempted"] += 1
                watch_suspension = None
                workspace_diagnostic: dict[str, Any] = {}
                progress.emit(
                    "shader-prepare",
                    "Preparing project Shader source %d/%d" % (position, len(matches)),
                    current=position,
                    total=len(matches),
                    detail={
                        "resourceId": identity["resourceId"],
                        "stage": identity["stage"],
                        "knowledgeId": knowledge_id,
                    },
                )
                try:
                    progress.emit(
                        "resource-anchor",
                        "Validating one representative EID per Shader",
                        current=position,
                        total=len(matches),
                        item="Shader ResourceId %d" % identity["resourceId"],
                        detail={
                            "resourceId": identity["resourceId"],
                            "anchorEventId": expected_anchor_event_id,
                            "eventTraversalCount": 0,
                        },
                    )
                    if control_checkpoint is not None:
                        control_checkpoint("before target Shader workspace preparation")
                    (
                        workspace,
                        status,
                        live_capture_id,
                        live_catalog_sha256,
                    ) = _prepare_reuse_workspace(
                        host,
                        active,
                        identity,
                        capture_session_id,
                        catalog_sha256,
                        expected_anchor_event_id=expected_anchor_event_id,
                        expected_source_sha256=str(candidate["sourceSha256"]),
                        inventory_mode=inventory_mode,
                        registered_knowledge=knowledge,
                        capture_path=capture_path,
                        progress=progress,
                        diagnostic=workspace_diagnostic,
                        pending_source=_fallback_pending_knowledge(candidate, binding_state),
                    )
                    if control_checkpoint is not None:
                        control_checkpoint("after target Shader workspace preparation")
                    watch_suspension = _suspend_workspace_watchers(host, workspace)
                    local_candidate = _local_restore_candidate(status, str(candidate["sourceSha256"]))
                    published_source_advance = None
                    if local_candidate and inventory_mode:
                        advance = getattr(replay, "published_workspace_source_advance", None)
                        if callable(advance):
                            published_source_advance = advance(knowledge_id, status)
                            if published_source_advance is not None:
                                # Exact old published bytes are a prior project
                                # version, not an unpublished local edit. The
                                # seed rechecks head generation and source /
                                # candidate CAS under both ownership locks.
                                local_candidate = False
                    if local_candidate:
                        # This same-capture owner has an unpublished successful
                        # local source. Restore
                        # its exact binary through the ordinary apply/CAS path;
                        # do not publish it as shared project knowledge.
                        candidate = {**candidate,
                                     "sourceSha256": status["source"]["currentSha256"],
                                     "binaryCandidateEligible": False}
                        source_preparation_only = True
                    superseded_local_edit = None
                    # Older inventory seeds could copy the active project text
                    # without recording its import receipt. Retry through the
                    # ordinary verified seed before asking the physical gate
                    # to retire an old draft. Equal bytes alone never retire it.
                    refresh_project_source = bool(
                        not local_candidate
                        and status["source"].get("origin") != "verified-project-source"
                        and status["source"].get("projectSource") is None
                        and status["source"].get("currentSha256") == candidate.get("sourceSha256")
                        and (workspace / "ai" / "organize-code-gate.json").is_file()
                    )
                    if source_preparation_only and not local_candidate:
                        # The normal public prepare path must have imported the
                        # exact active project source. Never fall back to an old
                        # seed, reverse-engineer it again, or adopt its grant.
                        if published_source_advance is None:
                            try:
                                _validate_prepared_project_source(status, knowledge, identity, binding_state,
                                    source_preparation_only=source_preparation_only)
                            except RestoreLocalEdit:
                                if status["source"].get("modifiedSinceDecompile") is not False:
                                    raise
                            except RestoreProjectSourceRefresh:
                                refresh_project_source = True
                        _prepared_binding_state(replay, active, prepared)
                    source = status["source"]
                    if (source.get("currentSha256") != candidate.get("sourceSha256")
                            or refresh_project_source):
                        # Only untouched output, identical source needing a
                        # receipt refresh, or an exact previous publication may
                        # reach this seed. Unknown local edits remain protected.
                        if control_checkpoint is not None:
                            control_checkpoint("before target Shader source seed")
                        if inventory_mode:
                            project_inventory = prepared.get("inventory")
                            expected_binding = prepared.get("binding")
                            target_shader = None
                            if isinstance(project_inventory, Mapping):
                                for value in project_inventory.get("shaders", []):
                                    if (
                                        isinstance(value, Mapping)
                                        and value.get("resourceId")
                                        == identity.get("resourceId")
                                        and value.get("stableKeySha256")
                                        == identity.get("stableKeySha256")
                                    ):
                                        if target_shader is not None:
                                            raise RuntimeError(
                                                "target Shader inventory identity is ambiguous"
                                            )
                                        target_shader = value
                            if (
                                target_shader is None
                                or not isinstance(expected_binding, Mapping)
                            ):
                                raise RuntimeError(
                                    "target Shader inventory binding is incomplete"
                                )
                            seeded = replay.seed_workspace_from_inventory(
                                knowledge_id,
                                status,
                                target_shader,
                                project_inventory,
                                expected_binding,
                                **({"published_source_advance": published_source_advance}
                                   if published_source_advance is not None else {}),
                            )
                            if (
                                not isinstance(seeded, Mapping)
                                or seeded.get("schema")
                                != "renderdoc-ultra-project-workspace-inventory-seed/v1"
                                or seeded.get("schemaVersion") != 1
                                or seeded.get("captureId") != capture_id
                                or seeded.get("catalogSha256") != catalog_sha256
                                or seeded.get("targetResourceId")
                                != identity.get("resourceId")
                                or seeded.get("targetStableKeySha256")
                                != identity.get("stableKeySha256")
                                or seeded.get("workspacePath") != str(workspace)
                                or seeded.get("applied") is not False
                            ):
                                raise RuntimeError(
                                    "target Shader inventory seed changed identity"
                                )
                        else:
                            seeded = _tool_json(
                                host.seed_active_shader_workspace(
                                    knowledge_id, str(workspace)
                                ),
                                "seed target Shader workspace",
                                schema="replaylab-active-workspace-seed/v1",
                                schema_version=1,
                            )
                        seed_result = seeded.get("result") if isinstance(seeded.get("result"), Mapping) else seeded
                        if isinstance(seed_result, Mapping) and isinstance(
                                seed_result.get("supersededLocalEdit"), Mapping):
                            superseded_local_edit = dict(seed_result["supersededLocalEdit"])
                        if control_checkpoint is not None:
                            control_checkpoint("after target Shader source seed")
                        if not inventory_mode and (
                            seeded.get("captureId") != capture_id
                            or seeded.get("captureSessionId") != capture_session_id
                            or seeded.get("workspacePath") != str(workspace)
                            or seeded.get("applied") is not False
                        ):
                            raise RuntimeError(
                                "target Shader source seed changed capture identity"
                            )
                        status = _reuse_workspace_status(
                            host,
                            active,
                            workspace,
                            identity,
                            live_capture_id,
                            live_catalog_sha256,
                        )
                        if source_preparation_only and not local_candidate:
                            _validate_prepared_project_source(status, knowledge, identity, binding_state,
                                    source_preparation_only=source_preparation_only)
                    if status["source"].get("currentSha256") != candidate.get(
                        "sourceSha256"
                    ):
                        raise RuntimeError("target Shader source seed hash mismatch")
                    _require_restore_context(
                        host,
                        active,
                        project_root,
                        capture_path,
                        capture_session_id,
                    )
                    try:
                        from .shader_workspace.semantic_overview_storage import restore_overview
                        restore_overview(Path(active["paths"]["replayProject"]), workspace,
                            identity["rawSha256"], candidate["sourceSha256"],
                            origin_workspace=Path(knowledge["workspacePath"]) if knowledge.get("workspacePath") else None,
                            origin_workspace_id=knowledge.get("workspaceId"))
                    except (ValueError, OSError):
                        # Navigation is independent of executable authority.
                        pass
                    status = _compile_reuse_workspace(
                        host,
                        active,
                        workspace,
                        status,
                        identity,
                        live_capture_id,
                        live_catalog_sha256,
                        candidate,
                        progress,
                        position,
                        len(matches),
                        control_checkpoint,
                    )
                    latest = status["latestCompiled"]
                    revision = _integer(
                        latest.get("revision"), "candidateRevision", 1
                    )
                    candidate_raw = _hex(
                        latest.get("rawSha256"), 64, "candidateRawSha256"
                    )
                    if source_preparation_only and not local_candidate:
                        _validate_prepared_project_source(status, knowledge, identity, binding_state,
                                    source_preparation_only=source_preparation_only)
                        _prepared_binding_state(replay, active, prepared)
                    _require_restore_context(
                        host,
                        active,
                        project_root,
                        capture_path,
                        capture_session_id,
                    )
                    progress.emit(
                        "shader-apply",
                        "Validating ABI and applying project Shader %d/%d"
                        % (position, len(matches)),
                        current=position,
                        total=len(matches),
                        detail={
                            "resourceId": identity["resourceId"],
                            "stage": identity["stage"],
                            "workspacePath": str(workspace),
                        },
                    )
                    if control_checkpoint is not None:
                        control_checkpoint("before project Shader apply")
                    apply_record = {
                        "identity": copy.deepcopy(identity),
                        "managedWorkspace": {
                            "workspaceId": status["workspaceId"],
                            "path": workspace.relative_to(project_root).as_posix(),
                        },
                        "candidateRevision": revision,
                        "candidateRawSha256": candidate_raw,
                    }
                    manual_edit = (status.get("latestCompiled") or {}).get("validationScope") == "manual-edit-unvalidated"
                    applied = None if source_preparation_only or manual_edit else _adopt_exact_live_replacement(
                        host,
                        workspace,
                        status,
                        apply_record,
                        capture_session_id,
                    )
                    adopted = applied is not None
                    if applied is None:
                        applied = _apply_workspace(
                            host,
                            workspace,
                            status,
                            expected_revision=revision,
                            expected_candidate_raw_sha256=candidate_raw,
                            expected_anchor_event_id=expected_anchor_event_id,
                            capture_session_id=capture_session_id,
                            project_root=project_root,
                        )
                    operation_created = bool(
                        not adopted
                        and applied.get("replacement", {}).get(
                            "operationCreatedReplacement"
                        )
                        is True
                    )
                    try:
                        after = _reuse_workspace_status(
                            host,
                            active,
                            workspace,
                            identity,
                            live_capture_id,
                            live_catalog_sha256,
                        )
                        after_latest = after.get("latestCompiled") or {}
                        if (after.get("source", {}).get("currentSha256") != candidate["sourceSha256"]
                                or after_latest.get("sourceSha256") != candidate["sourceSha256"]
                                or after.get("revision") != revision
                                or after_latest.get("revision") != revision
                                or after_latest.get("rawSha256") != candidate_raw):
                            raise RuntimeError("applied Shader source/candidate changed before restore report")
                        if applied.get("validation", {}).get("manualEdit") is True:
                            from .project_manual_shader import from_apply
                            after = dict(after, manualApplyReceipt=from_apply(applied, after))
                        _require_restore_context(
                            host,
                            active,
                            project_root,
                            capture_path,
                            capture_session_id,
                        )
                        if source_preparation_only and not local_candidate:
                            _validate_prepared_project_source(after, knowledge, identity, binding_state,
                                source_preparation_only=source_preparation_only)
                            _prepared_binding_state(replay, active, prepared)
                        if inventory_mode and not source_preparation_only:
                            project_inventory = prepared.get("inventory")
                            live_inventory = prepared.get("liveInventory")
                            expected_binding = prepared.get("binding")
                            if not all(
                                isinstance(value, Mapping)
                                for value in (
                                    project_inventory,
                                    live_inventory,
                                    expected_binding,
                                )
                            ):
                                raise RuntimeError(
                                    "project Shader inventory publication binding is incomplete"
                                )
                            exact_targets = [
                                value
                                for value in project_inventory.get("shaders", [])
                                if isinstance(value, Mapping)
                                and value.get("resourceId")
                                == identity.get("resourceId")
                                and value.get("stableKeySha256")
                                == identity.get("stableKeySha256")
                            ]
                            if len(exact_targets) != 1:
                                raise RuntimeError(
                                    "project Shader inventory publication target is ambiguous"
                                )
                            published = replay.register_workspace_from_inventory(
                                after,
                                exact_targets[0],
                                project_inventory,
                                expected_binding,
                            )
                            published_knowledge = (
                                published.get("knowledge")
                                if isinstance(published, Mapping)
                                else None
                            )
                            published_binding = (
                                published.get("binding")
                                if isinstance(published, Mapping)
                                else None
                            )
                            if (
                                not isinstance(published_knowledge, Mapping)
                                or not isinstance(published_binding, Mapping)
                                or published.get("applied") is not False
                            ):
                                raise RuntimeError(
                                    "project rejected the validated Shader artifact publication"
                                )
                            knowledge_id = _hex(
                                published_knowledge.get("knowledgeId"),
                                32,
                                "published workspace knowledgeId",
                            )
                            refreshed = replay.validate_shader_inventory_snapshot(
                                project_inventory, published_binding
                            )
                            binding_state = _inventory_binding_state(
                                refreshed, live_inventory
                            )
                            published_state = binding_state.get(
                                "workspaces", {}
                            ).get(knowledge_id)
                            if (
                                not isinstance(published_state, Mapping)
                                or not _knowledge_matches_shader(
                                    published_state, identity, executable=True
                                )
                                or published_state.get("sourceSha256")
                                != after.get("source", {}).get("currentSha256")
                                or not isinstance(
                                    published_state.get("compiledCandidate"),
                                    Mapping,
                                )
                                or published_state["compiledCandidate"].get(
                                    "rawSha256"
                                )
                                != candidate_raw
                            ):
                                raise RuntimeError(
                                    "published project Shader artifact differs from live validation"
                                )
                            if isinstance(prepared, dict):
                                prepared["binding"] = copy.deepcopy(
                                    dict(published_binding)
                                )
                                prepared["knowledge"] = copy.deepcopy(
                                    dict(refreshed.get("knowledge", {}))
                                )
                        record = _shader_record_from_status(
                            project_root,
                            identity,
                            knowledge_id,
                            workspace,
                            after,
                            (
                                binding_state.get("workspaces", {}).get(
                                    knowledge_id
                                )
                                if inventory_mode and not source_preparation_only
                                else None
                            ),
                        )
                        if applied.get("validation", {}).get("manualEdit") is True:
                            record["manualEdit"] = True
                    except Exception:
                        if operation_created:
                            try:
                                _rollback_applied_workspace(
                                    host,
                                    workspace,
                                    status,
                                    applied,
                                    capture_session_id,
                                )
                            except Exception as rollback_exc:
                                raise RestoreRollbackFailed(
                                    "restored Shader post-apply validation failed and rollback failed: %s: %s"
                                    % (type(rollback_exc).__name__, rollback_exc)
                                ) from rollback_exc
                        raise
                    if operation_created:
                        applied_workspaces.append(
                            (
                                workspace,
                                copy.deepcopy(dict(after)),
                                copy.deepcopy(dict(applied)),
                                capture_session_id,
                            )
                        )
                    if source_preparation_only:
                        record["sourcePreparationOnly"] = True
                        if local_candidate:
                            record["localValidatedCandidate"] = True
                        # This is the freshly validated current-session apply
                        # observation, not the complete-key executable cache
                        # receipt. It is counted but never persisted in RDC.
                        receipt = {"sourcePreparationOnly": True,
                                   "identity": copy.deepcopy(identity),
                                   "replacement": copy.deepcopy(dict(applied["replacement"]))}
                    else:
                        receipt = _realized_receipt_from_apply(record, applied, after)
                    realized_receipts.append(receipt)
                    if receipt_stats is not None:
                        receipt_stats["produced"] += 1
                        if applied.get("validation", {}).get("reusedValidation") is True:
                            receipt_stats["reusedValidation"] = receipt_stats.get("reusedValidation", 0) + 1
                        if adopted:
                            receipt_stats["adoptedCurrentSession"] += 1
                        elif operation_created:
                            receipt_stats["mutationsCreated"] += 1
                        else:
                            receipt_stats["idempotentApplyResponses"] += 1
                    if control_checkpoint is not None:
                        control_checkpoint("after project Shader apply")
                    if workspace_diagnostic.get("preservedLocalSource") is not None:
                        record["preservedLocalSource"] = copy.deepcopy(workspace_diagnostic["preservedLocalSource"])
                    successful_shader_records.append(record)
                    shader_summary["applied"] += 1
                    reported_knowledge = binding_state.get("workspaces", {}).get(knowledge_id) or knowledge
                    shader_summary["items"].append(
                        {
                            "status": "applied",
                            "source": "reuse-plan",
                            "workspacePath": str(workspace),
                            "workspaceId": after["workspaceId"],
                            "resourceId": identity["resourceId"],
                            "stage": identity["stage"],
                            "replacementId": applied["replacement"][
                                "replacementId"
                            ],
                            "idempotentlyAdopted": adopted,
                            "reusedValidation": applied.get("validation", {}).get("reusedValidation") is True,
                            "pairValidationCache": copy.deepcopy(applied.get("pairValidationCache")),
                            "knowledgeId": knowledge_id,
                            "sourceSha256": after["source"]["currentSha256"],
                            "candidateRawSha256": candidate_raw,
                            "candidateRevision": revision,
                            "projectSourceSha256": reported_knowledge["sourceSha256"],
                            "projectCandidateRawSha256": (reported_knowledge.get("compiledCandidate") or {}).get("rawSha256", ""),
                            "sourcePreparationOnly": source_preparation_only,
                            "localValidatedCandidate": local_candidate,
                            "supersededLocalEdit": superseded_local_edit,
                        }
                    )
                    pending_display = _show_fallback_pending_source(host, candidate, binding_state,
                        identity, capture_id, capture_path, capture_session_id)
                    if pending_display is not None:
                        shader_summary["items"][-1]["pendingSource"] = pending_display
                    local_display = _restore_local_source_display(host, active, project_root,
                        capture_path, record, reported_knowledge)
                    if local_display is not None:
                        shader_summary["items"][-1]["preservedLocalSource"] = local_display
                        # Snapshot-format migration can recover this hint only
                        # at display time, after target preparation reused an
                        # unchanged registered replay owner. Retain that checked
                        # owner in the replacement snapshot as well as the UI.
                        record["preservedLocalSource"] = {
                            "workspaceId": local_display["workspaceId"],
                            "path": Path(local_display["workspacePath"]).relative_to(project_root).as_posix(),
                        }
                    if superseded_local_edit is not None:
                        shader_summary["supersededLocalEdits"] = int(
                            shader_summary.get("supersededLocalEdits", 0)) + 1
                except (
                    RestoreContextChanged,
                    RestoreContextUnavailable,
                    RestoreRollbackFailed,
                    RestoreControlStop,
                ):
                    raise
                except RestoreLocalEdit as exc:
                    # Admission never reached an apply attempt. Keep the
                    # established result contract: attempted = applied + failed.
                    shader_summary["attempted"] -= 1
                    shader_summary["skipped"] += 1
                    shader_summary["items"].append(
                        {
                            "status": "skipped",
                            "reason": "local-edit",
                            "detail": str(exc),
                            "source": "reuse-plan",
                            "knowledgeId": knowledge_id,
                            "knowledgeSourceSha256": str(knowledge.get("sourceSha256") or ""),
                            "resourceId": identity["resourceId"],
                            "stage": identity["stage"],
                            "rawSha256": identity.get("rawSha256"),
                            "workspacePath": str(exc.workspace) if exc.workspace else "",
                            "workspaceSourceSha256": exc.workspace_source_sha256,
                        }
                    )
                except Exception as exc:
                    shader_summary["failed"] += 1
                    diagnostic_text = (
                        " [workspacePath=%s; catalogSha256=%s; projectSourceSha256=%s]"
                        % (
                            workspace_diagnostic.get("workspacePath", "unresolved"),
                            workspace_diagnostic.get("resolvedCatalogSha256", "unresolved"),
                            workspace_diagnostic.get("requestedProjectSourceSha256", "unresolved"),
                        )
                    )
                    shader_summary["items"].append(
                        {
                            "status": "failed",
                            "source": "reuse-plan",
                            "knowledgeId": knowledge_id,
                            "resourceId": identity["resourceId"],
                            "stage": identity["stage"],
                            "error": "%s: %s%s" % (
                                type(exc).__name__, exc, diagnostic_text
                            ),
                        }
                    )
                finally:
                    if watch_suspension is not None:
                        watch_suspension.__exit__(None, None, None)
                _require_restore_context(
                    host,
                    active,
                    project_root,
                    capture_path,
                    capture_session_id,
                )

        # CBuffer names and field aliases are installed by the project alias
        # overlay above.  Event-scoped numeric values/material snapshots are
        # deliberately not restored while opening an RDC: they require the
        # current EID (or an explicit material-snapshot action) on demand.
        if control_checkpoint is not None:
            control_checkpoint("after project reuse-plan Shader match")
    return successful_shader_records, successful_material_records, binding_state


def _legacy_material_shader_identity(
    shader_identities: Mapping[tuple[Any, Any], Sequence[Mapping[str, Any]]],
    record: Mapping[str, Any],
) -> Mapping[str, Any]:
    identity_candidates = shader_identities.get(
        (record.get("shaderResourceId"), record.get("stage"))
    )
    if not identity_candidates:
        raise RuntimeError("material association has no Shader identity")
    if len(identity_candidates) != 1:
        # Legacy material rows predate entryPoint/stable-key binding.  Never
        # guess when one physical resource exposes more than one entry point.
        raise RuntimeError(
            "legacy material association is ambiguous across Shader entry points"
        )
    return identity_candidates[0]


def _restore_from_cache(
    host: Any,
    replay: ReplayProjectService,
    active: dict[str, Any],
    project_root: Path,
    capture_path: Path,
    capture_session_id: str,
    binding: Mapping[str, Any],
    binding_state: Mapping[str, Any],
    inventory: Mapping[str, Any] | None,
    progress: _Progress,
    shader_summary: dict[str, Any],
    material_summary: dict[str, Any],
    applied_workspaces: list[
        tuple[Path, Mapping[str, Any], Mapping[str, Any], str]
    ],
    restored_material_sessions: list[tuple[str, str, str]],
    restored_alias_transactions: list[dict[str, str]],
    control_checkpoint: Callable[[str], None] | None = None,
    realized_receipts: list[dict[str, Any]] | None = None,
    receipt_stats: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if realized_receipts is None:
        realized_receipts = []
    records = list(binding.get("shaders", []) or [])
    seen_workspaces: set[str] = set()
    seen_resource_ids: set[int] = set()
    preflight_records: list[tuple[Mapping[str, Any], str]] = []
    # Reject an ambiguous legacy/forged cache before validating even the first
    # workspace.  Validation is read-only today, but keeping the entire cache
    # preflight ahead of every external call prevents future validation hooks
    # from creating a partial side effect before the duplicate is discovered.
    for record in records:
        if not isinstance(record, Mapping) or not isinstance(
            record.get("identity"), Mapping
        ):
            raise RuntimeError("RDC index contains a malformed Shader record")
        resource_id = _normal_shader_identity(record["identity"])["resourceId"]
        if resource_id in seen_resource_ids:
            raise RuntimeError(
                "RDC index maps one physical Shader resource more than once"
            )
        seen_resource_ids.add(resource_id)
        workspace_id = str(record.get("managedWorkspace", {}).get("workspaceId", ""))
        if workspace_id in seen_workspaces:
            raise RuntimeError("RDC index maps one managed workspace more than once")
        seen_workspaces.add(workspace_id)
        preflight_records.append((record, workspace_id))

    validated: list[tuple[Mapping[str, Any], Path, dict[str, Any]]] = []
    for index, (record, workspace_id) in enumerate(preflight_records, 1):
        if control_checkpoint is not None:
            control_checkpoint("before cached Shader workspace validation")
        progress.emit(
            "cache-resource",
            "Validating cached Shader asset %d/%d" % (index, len(records)),
            current=index,
            total=len(records),
            detail={"workspaceId": workspace_id},
        )
        workspace, status = _validate_workspace_record(
            host,
            replay,
            active,
            project_root,
            capture_path,
            binding_state,
            record,
        )
        validated.append((record, workspace, status))
        if control_checkpoint is not None:
            control_checkpoint("after cached Shader workspace validation")

    _match_workspace_resources(
        host,
        capture_path,
        capture_session_id,
        validated,
        control_checkpoint,
        inventory=inventory,
        progress=progress,
    )
    _require_restore_context(
        host, active, project_root, capture_path, capture_session_id
    )

    # The qrenderdoc alias overlay is capture-session-local and is reset every
    # time an RDC is opened. The RDC index deliberately stores routing
    # evidence, not display aliases, so rematerialize the current project's
    # target-bound aliases before any cached Shader replacement is applied.
    if control_checkpoint is not None:
        control_checkpoint("before cached project alias reuse plan")
    if binding_state.get("capture", {}).get("inventoryOnly") is True:
        plan = copy.deepcopy(binding_state.get("reusePlan"))
    else:
        plan = replay.reuse_plan(
            str(binding_state["captureId"]),
            str(binding_state["catalogSha256"]),
        )
    if control_checkpoint is not None:
        control_checkpoint("after cached project alias reuse plan")
    matches = plan.get("matches") if isinstance(plan, Mapping) else None
    if (
        not isinstance(plan, Mapping)
        or plan.get("schema") != "renderdoc-ultra-project-reuse-plan/v1"
        or plan.get("schemaVersion") != 1
        or plan.get("projectId") != active.get("replayProject", {}).get("projectId")
        or plan.get("captureId") != binding_state.get("captureId")
        or plan.get("catalogSha256") != binding_state.get("catalogSha256")
        or not isinstance(matches, list)
        or len(matches) > _MAX_FAST_CATALOG_SHADERS
        or plan.get("matchedShaderCount") != len(matches)
    ):
        raise RuntimeError("cached project alias reuse plan changed project binding")
    _restore_reuse_aliases_best_effort(
        host,
        replay,
        active,
        project_root,
        capture_path,
        capture_session_id,
        plan,
        matches,
        progress,
        control_checkpoint,
        binding_state.get("projectInventory"),
        binding_state.get("inventoryBinding"),
        restored_alias_transactions,
    )

    successful_shader_records: list[dict[str, Any]] = []
    for index, (record, workspace, status) in enumerate(validated, 1):
        if control_checkpoint is not None:
            control_checkpoint("before cached Shader apply")
        shader_summary["attempted"] += 1
        _require_restore_context(
            host, active, project_root, capture_path, capture_session_id
        )
        progress.emit(
            "shader-apply",
            "Applying cached Shader %d/%d" % (index, len(validated)),
            current=index,
            total=len(validated),
            detail={
                "workspacePath": str(workspace),
                "resourceId": record["identity"]["resourceId"],
                "stage": record["identity"]["stage"],
            },
        )
        try:
            applied = _adopt_exact_live_replacement(
                host, workspace, status, record, capture_session_id
            )
            adopted = applied is not None
            if applied is None:
                applied = _apply_workspace(
                    host,
                    workspace,
                    status,
                    expected_revision=int(record["candidateRevision"]),
                    expected_candidate_raw_sha256=str(record["candidateRawSha256"]),
                    expected_anchor_event_id=_shader_anchor_event_id(
                        _normal_shader_identity(record["identity"])
                    ),
                    capture_session_id=capture_session_id,
                    project_root=project_root,
                )
            operation_created = bool(
                not adopted
                and applied.get("replacement", {}).get(
                    "operationCreatedReplacement"
                )
                is True
            )
            try:
                _require_restore_context(
                    host, active, project_root, capture_path, capture_session_id
                )
            except Exception:
                if operation_created:
                    try:
                        _rollback_applied_workspace(
                            host,
                            workspace,
                            status,
                            applied,
                            capture_session_id,
                        )
                    except Exception as rollback_exc:
                        raise RestoreRollbackFailed(
                            "cached Shader context changed and rollback failed: %s: %s"
                            % (type(rollback_exc).__name__, rollback_exc)
                        ) from rollback_exc
                raise
            if operation_created:
                applied_workspaces.append(
                    (
                        workspace,
                        copy.deepcopy(dict(status)),
                        copy.deepcopy(dict(applied)),
                        capture_session_id,
                    )
                )
            receipt = _realized_receipt_from_apply(record, applied, status)
            realized_receipts.append(receipt)
            if receipt_stats is not None:
                receipt_stats["produced"] += 1
                if applied.get("validation", {}).get("reusedValidation") is True:
                    receipt_stats["reusedValidation"] = receipt_stats.get("reusedValidation", 0) + 1
                if adopted:
                    receipt_stats["adoptedCurrentSession"] += 1
                elif operation_created:
                    receipt_stats["mutationsCreated"] += 1
                else:
                    receipt_stats["idempotentApplyResponses"] += 1
            if control_checkpoint is not None:
                control_checkpoint("after cached Shader apply")
            shader_summary["applied"] += 1
            item = {
                "status": "applied",
                "source": "rdc-index",
                "workspacePath": str(workspace),
                "workspaceId": status["workspaceId"],
                "resourceId": record["identity"]["resourceId"],
                "stage": record["identity"]["stage"],
                "replacementId": applied["replacement"]["replacementId"],
                "idempotentlyAdopted": adopted,
                "reusedValidation": applied.get("validation", {}).get("reusedValidation") is True,
                "pairValidationCache": copy.deepcopy(applied.get("pairValidationCache")),
            }
            local_display = _restore_local_source_display(host, active, project_root,
                capture_path, record, binding_state.get("workspaces", {}).get(record.get("knowledgeId")))
            restored_record = copy.deepcopy(dict(record))
            if local_display is not None:
                item["preservedLocalSource"] = local_display
                item["sourceSha256"] = record["sourceSha256"]
                # A legacy index omits editor hints. Preserve the independently
                # revalidated owner when publishing the upgraded derived
                # snapshot, otherwise the next warm reopen loses the draft.
                restored_record["preservedLocalSource"] = {
                    "workspaceId": local_display["workspaceId"],
                    "path": Path(local_display["workspacePath"]).relative_to(project_root).as_posix(),
                }
            shader_summary["items"].append(item)
            successful_shader_records.append(restored_record)
        except (
            RestoreContextChanged,
            RestoreContextUnavailable,
            RestoreRollbackFailed,
            RestoreControlStop,
        ):
            raise
        except Exception as exc:
            shader_summary["failed"] += 1
            shader_summary["items"].append(
                {
                    "status": "failed",
                    "source": "rdc-index",
                    "workspacePath": str(workspace),
                    "resourceId": record["identity"]["resourceId"],
                    "stage": record["identity"]["stage"],
                    "error": "%s: %s" % (type(exc).__name__, exc),
                }
            )

    # Old RDC bindings may still carry event-scoped CBuffer value snapshots.
    # They are history/routing hints only: automatic cross-RDC restore loads
    # names and field aliases through the project overlay above, while numeric
    # values require an explicit current-EID/material-snapshot action. Returning
    # an empty list also migrates the next successfully written cache away from
    # the obsolete automatic-value records.
    return successful_shader_records, []


def _enable_restored_shader_editing(
    host: Any, active: Mapping[str, Any], project_root: Path,
    capture_path: Path, capture_session_id: str,
    records: Sequence[Mapping[str, Any]], shader_items: Sequence[Mapping[str, Any]],
    *, cancel_event: Any = None, deadline_monotonic: float | None = None,
    progress: _Progress | None = None,
) -> dict[str, Any]:
    from .ipc_client import _CALL_BINDING, bind_direct_tool_call
    try:
        previous = _CALL_BINDING.get()
        if previous is not None and previous[1] != capture_session_id:
            raise RuntimeError("restore caller is bound to another capture session")
        instance = previous[0] if previous is not None else getattr(host.ipc, "instance_id", "")
        # The implicit instance is only a candidate. Re-check the requested
        # path/session INSIDE this binding before any start, then retain it for
        # every nested request even when another GUI becomes more active.
        with bind_direct_tool_call(instance, capture_session_id):
            batch = getattr(host, "_restored_shader_editor_batch", None)
            # Only the in-process Host supplies this private scope. Ordinary
            # tools have no parameter that can mint a cached capture identity.
            context = (batch(capture_path, capture_session_id)
                       if callable(batch) and any(item.get("status") == "applied" for item in shader_items)
                       else contextlib.nullcontext())
            active_service = host._active_project_service()
            snapshot_factory = getattr(active_service, "snapshot_active_project", None)
            snapshot_reader = getattr(active_service, "_bound_project_snapshot", None)
            existing_snapshot = snapshot_reader() if callable(snapshot_reader) else None
            # Read-only observations hold no project/selection lock across
            # watcher startup. Reuse this configuration snapshot; each lifecycle
            # checkpoint still checks the real current selection and session.
            project_context = (snapshot_factory(active["workspaceId"], project_root)
                               if existing_snapshot is None and callable(snapshot_factory)
                               and callable(snapshot_reader) else contextlib.nullcontext(existing_snapshot))
            with project_context, context:
                return _enable_restored_shader_editing_bound(
                    host, active, project_root, capture_path, capture_session_id,
                    records, shader_items, cancel_event=cancel_event,
                    deadline_monotonic=deadline_monotonic, progress=progress)
    except Exception as exc:
        items = [{"workspaceId": item["workspaceId"], "workspacePath": item["workspacePath"],
                  "status": "failed", "error": ("%s: %s" % (type(exc).__name__, exc))[:4096]}
                 for item in shader_items if item.get("status") == "applied"]
        return {"attempted": len(items), "enabled": 0, "failed": len(items), "items": items}


def _retire_restored_editor_job(live_id: str, workspace: Path, binding: Mapping[str, Any]) -> str:
    """Retire this Internal worker's exact new job, retaining durable consent."""
    from . import internal_tool_service
    with internal_tool_service._SHADER_LIVE_EDIT_MANAGER_GUARD:
        manager = internal_tool_service._SHADER_LIVE_EDIT_MANAGER
    if manager is None:
        return "exact editor owner unavailable; cleanup not confirmed"
    # Same retirement used by expired-capture reconciliation. Public stop also
    # revokes durable user consent, so it is not a cleanup route for restore.
    with manager._lifecycle_lock:
        with manager._lock:
            job = manager._jobs.get(live_id)
        if (job is None or job.session is None or job.session.binding != dict(binding)
                or _path_key(job.workspace) != _path_key(workspace)):
            return "exact editor ownership changed; cleanup not confirmed"
        job.stop()
        with manager._lock:
            key = os.path.normcase(str(workspace.resolve()))
            if manager._active_by_workspace.get(key) == live_id:
                manager._active_by_workspace.pop(key, None)
    return "exact editor job retired; durable authorization preserved"


def _enable_restored_shader_editing_bound(
    host: Any, active: Mapping[str, Any], project_root: Path,
    capture_path: Path, capture_session_id: str,
    records: Sequence[Mapping[str, Any]], shader_items: Sequence[Mapping[str, Any]],
    *, cancel_event: Any = None, deadline_monotonic: float | None = None,
    progress: _Progress | None = None,
) -> dict[str, Any]:
    """Authorize editing only after restore has committed and released its leases.

    Restore runs on its dedicated worker; live-start is served by the separate
    HTTP executor. No source transaction or active-project pin may remain held
    while that request acquires the watcher lease. An editor failure is separate
    from the already committed, physically validated replay replacement.
    """
    result: dict[str, Any] = {"attempted": 0, "enabled": 0, "deferred": 0, "failed": 0, "items": []}
    on_demand = getattr(host, "_shader_editors_on_demand", False) is True
    if on_demand:
        result["ready"] = 0
    by_id = {str(record.get("managedWorkspace", {}).get("workspaceId", "")): record
             for record in records}
    if not any(item.get("status") == "applied" for item in shader_items):
        return result
    editor_total = sum(item.get("status") == "applied" for item in shader_items)
    if progress is not None:
        progress.emit("shader-prepare", "Enabling restored Shader workspaces",
                      total=editor_total)
    _restore_control_checkpoint(cancel_event, deadline_monotonic, "before restored Shader editor batch")
    _require_restore_context(host, active, project_root, capture_path, capture_session_id)
    from .ipc_client import _CALL_BINDING
    expected_instance = _CALL_BINDING.get()[0]
    expected_binding = {"instanceId": expected_instance, "captureSessionId": capture_session_id,
                        "capturePath": str(capture_path)}

    def read_editors() -> Mapping[str, Any]:
        listing = _tool_json(host.list_shader_workspace_live_edits(),
            "list restored Shader editors", schema="replaylab-shader-live-edit-mcp/v1", schema_version=1)
        sessions = (listing.get("result") or {}).get("sessions")
        if (listing.get("operation") != "list" or listing.get("appliedByThisCall") is not False
                or not isinstance(sessions, list) or len(sessions) > 256
                or any(not isinstance(candidate, Mapping) for candidate in sessions)):
            raise RuntimeError("restored Shader editor list is invalid")
        return listing

    # Listing can reconcile persistent live-edit consent. A source-only draft
    # needs no watcher service at all, so defer that call until a live editor
    # is actually eligible below.
    listing = None
    created_jobs: dict[str, tuple[Path, Mapping[str, Any]]] = {}
    from .shader_workspace.alias_display_mirror import DisplayAliasRestoreCache, restore_display_alias_mirrors
    display_alias_cache = DisplayAliasRestoreCache(project_root)
    for applied in shader_items:
        if applied.get("status") != "applied":
            continue
        result["attempted"] += 1
        item = {"workspaceId": applied["workspaceId"], "workspacePath": applied["workspacePath"]}
        if progress is not None:
            progress.emit("shader-prepare", "Enabling restored Shader workspace editing",
                          current=result["attempted"], total=editor_total,
                          item=str(applied["workspacePath"]))
        started_live_id = ""
        started_binding = None
        created_editor = False
        try:
            _restore_control_checkpoint(cancel_event, deadline_monotonic, "before restored Shader editor start")
            record = by_id.get(str(applied["workspaceId"]))
            if not isinstance(record, Mapping):
                raise RuntimeError("applied Shader has no committed project record")
            workspace = _resolve_workspace_path(
                project_root, Path(active["paths"]["shaderWorkspaces"]).resolve(),
                str(record["managedWorkspace"]["path"]),
            )
            if _path_key(workspace) != _path_key(applied["workspacePath"]):
                raise RuntimeError("applied Shader workspace path changed before editor start")
            # Editor admission needs the current source/candidate bytes, not
            # organization summaries or the unrelated unpacked-asset catalog
            # appended by the public status tool (including another HTTP hop).
            status = _workspace_artifact_service(host).status(str(workspace))
            if (not isinstance(status, Mapping)
                    or status.get("schema") != "renderdoc-ultra-shader-workspace-status/v1"):
                raise RuntimeError("restored Shader status response is invalid")
            identity = record["identity"]
            latest = status.get("latestCompiled") or {}
            if (status.get("workspaceId") != applied["workspaceId"]
                    or _path_key(status.get("capture", {}).get("capturePath", "")) != _path_key(capture_path)
                    or status.get("shader", {}).get("resourceId") != identity.get("resourceId")
                    or status.get("shader", {}).get("rawSha256") != identity.get("rawSha256")
                    or status.get("stage") != identity.get("stage")
                    or status.get("entryPoint", "") != identity.get("entryPoint", "")
                    or status.get("source", {}).get("currentSha256") != record.get("sourceSha256")
                    or status.get("sourceDependenciesCurrent", True) is not True
                    or latest.get("sourceSha256") != record.get("sourceSha256")
                    or latest.get("revision") != record.get("candidateRevision")
                    or latest.get("rawSha256") != record.get("candidateRawSha256")):
                raise RuntimeError("applied Shader source or physical identity changed before editor start")
            if applied.get("preservedLocalSource") is not None:
                # The replay owner is deliberately separate from the user's
                # editable draft. Keep that original editor/snapshot identity;
                # only an explicit save may compile and hand over replay.
                item.update(status="deferred", reason="local-edit-preserved",
                            editingMode="source-only", readOnly=False,
                            backgroundAutoApplyAuthorized=False,
                            captureBinding=copy.deepcopy(expected_binding))
                result["deferred"] += 1
                result["items"].append(item)
                continue
            # Display metadata is part of restore even when file watching is on demand.
            # The shared snapshot/cache skips unchanged names without replay or compilation.
            alias_started = time.perf_counter()
            try:
                item["displayAliases"] = restore_display_alias_mirrors(
                    host, workspace, status, capture_session_id, project_root,
                    active_project=active, metadata_cache=display_alias_cache)
            except Exception as alias_error:
                item["displayAliases"] = {"status": "failed", "diagnostic": str(alias_error)[:400]}
            item["displayAliasElapsedMs"] = round((time.perf_counter() - alias_started) * 1000, 3)
            if on_demand:
                from .shader_workspace.organization import active_draft_id
                from .shader_workspace.project_source import archive_superseded_project_source_gate
                archive_superseded_project_source_gate(
                    workspace, restored_record=record, applied_item=applied)
                draft = active_draft_id(workspace)
                if draft:
                    item.update(status="deferred", reason="organization-draft-open",
                                organizationId=draft, editingMode="source-only", readOnly=True,
                                backgroundAutoApplyAuthorized=False,
                                captureBinding=copy.deepcopy(expected_binding))
                    result["deferred"] += 1
                else:
                    item.update(status="ready", editingMode="on-demand", readOnly=False,
                                backgroundAutoApplyAuthorized=False,
                                captureBinding=copy.deepcopy(expected_binding))
                    result["ready"] += 1
                result["items"].append(item)
                continue
            from .shader_workspace.project_source import archive_superseded_project_source_gate
            archive_superseded_project_source_gate(
                workspace, restored_record=record, applied_item=applied)
            from .shader_workspace.organization import active_draft_id
            organization_id = active_draft_id(workspace)
            if organization_id:
                # Restore has already proved/applied the saved candidate. The
                # separate unfinished proposal is durable source work, not
                # permission to compile or start an automatic watcher. Keep
                # its normal organization read/write route and the source
                # preview; do not close/rewrite the draft or create live consent.
                item.update(status="deferred", reason="organization-draft-open",
                            organizationId=organization_id, editingMode="source-only",
                            readOnly=True, backgroundAutoApplyAuthorized=False,
                            captureBinding=copy.deepcopy(expected_binding))
                result["deferred"] += 1
                result["items"].append(item)
                continue
            if listing is None:
                listing = read_editors()
            def current_editor() -> Mapping[str, Any] | None:
                matches = []
                for candidate in listing["result"]["sessions"]:
                    scope = candidate.get("captureBinding") or {}
                    if (candidate.get("state") in ("starting", "running")
                            and _path_key(candidate.get("workspacePath", "")) == _path_key(workspace)
                            and scope.get("instanceId") == expected_instance
                            and scope.get("captureSessionId") == capture_session_id
                            and _path_key(scope.get("capturePath", "")) == _path_key(capture_path)):
                        _hex(candidate.get("liveEditId"), 32, "existing restored liveEditId")
                        matches.append(candidate)
                if len(matches) > 1:
                    raise RuntimeError("multiple current editors own the restored workspace")
                return matches[0] if matches else None

            existing = current_editor()
            existing_id = str(existing.get("liveEditId")) if existing is not None else ""
            operation = "poll" if existing_id else "start"
            if (existing is not None and existing.get("sessionState") == "current"
                    and existing.get("state") == "running"
                    and existing.get("watch", {}).get("state") == "running"
                    and existing.get("watch", {}).get("caughtUp") is True
                    and existing.get("watch", {}).get("observedSourceSha256") == record.get("sourceSha256")):
                # list() returns the same owner-authenticated job snapshot as
                # poll(). A ready current-session snapshot needs no second hop.
                operation = "list"
                wrapped = {**listing, "result": existing}
            elif existing_id:
                wrapped = _tool_json(host.poll_shader_workspace_live_edit(existing_id),
                    "read resumed Shader editor", schema="replaylab-shader-live-edit-mcp/v1", schema_version=1)
            else:
                try:
                    _require_restore_context(host, active, project_root, capture_path, capture_session_id)
                    wrapped = _tool_json(host.start_shader_workspace_live_edit(str(workspace)),
                        "start restored Shader editing", schema="replaylab-shader-live-edit-mcp/v1", schema_version=1)
                    created_editor = wrapped.get("result", {}).get("joinedExistingSession") is not True
                except Exception as start_error:
                    # Owner reconciliation can win between list and start.
                    # Re-read one exact current session; never repeat start.
                    try:
                        listing = read_editors()
                        existing = current_editor()
                        existing_id = str(existing.get("liveEditId")) if existing is not None else ""
                    except Exception as lookup_error:
                        raise RuntimeError("%s; current editor recovery failed: %s" % (start_error, lookup_error)) from start_error
                    if not existing_id:
                        raise start_error
                    operation = "poll"
                    wrapped = _tool_json(host.poll_shader_workspace_live_edit(existing_id),
                        "read concurrently resumed Shader editor", schema="replaylab-shader-live-edit-mcp/v1", schema_version=1)
            live = wrapped.get("result") or {}
            if isinstance(live.get("startTiming"), Mapping):
                item["startTiming"] = copy.deepcopy(live["startTiming"])
            started_live_id = _hex(live.get("liveEditId"), 32, "restored liveEditId")
            if existing_id and started_live_id != existing_id:
                raise RuntimeError("resumed Shader editor returned another job")
            started_binding = expected_binding
            poll_deadline = min(time.monotonic() + 10.0, deadline_monotonic or float("inf"))
            while True:
                binding = live.get("captureBinding") or {}
                if (wrapped.get("operation") != operation
                        or wrapped.get("appliedByThisCall") is not False
                        or wrapped.get("backgroundAutoApplyAuthorized") is not True
                        or live.get("schema") != "replaylab-shader-live-edit/v1"
                        or _path_key(live.get("workspacePath", "")) != _path_key(workspace)
                        or binding.get("instanceId") != expected_instance
                        or binding.get("captureSessionId") != capture_session_id
                        or _path_key(binding.get("capturePath", "")) != _path_key(capture_path)):
                    raise RuntimeError("restored Shader editor changed its frozen scope")
                live_id = _hex(live.get("liveEditId"), 32, "restored liveEditId")
                if started_live_id and live_id != started_live_id:
                    raise RuntimeError("restored Shader poll changed its live-edit job")
                started_live_id = live_id
                started_binding = expected_binding
                watch = live.get("watch") or {}
                _restore_control_checkpoint(cancel_event, deadline_monotonic, "waiting for restored Shader editor")
                if (live.get("state") == "running" and watch.get("state") == "running"
                        and watch.get("caughtUp") is True
                        and watch.get("observedSourceSha256") == record.get("sourceSha256")):
                    break
                _require_restore_context(host, active, project_root, capture_path, capture_session_id)
                if live.get("state") not in ("starting", "running") or watch.get("state") not in ("starting", "running"):
                    raise RuntimeError("restored Shader editor stopped before becoming ready")
                if time.monotonic() >= poll_deadline:
                    raise RuntimeError("restored Shader editor did not become ready within ten seconds")
                if cancel_event is not None and callable(getattr(cancel_event, "wait", None)):
                    cancel_event.wait(min(0.1, max(0.0, poll_deadline - time.monotonic())))
                else:
                    time.sleep(min(0.1, max(0.0, poll_deadline - time.monotonic())))
                _restore_control_checkpoint(cancel_event, deadline_monotonic, "before restored Shader editor poll")
                operation = "poll"
                wrapped = _tool_json(host.poll_shader_workspace_live_edit(started_live_id),
                    "poll restored Shader editing", schema="replaylab-shader-live-edit-mcp/v1", schema_version=1)
                live = wrapped.get("result") or {}
            binding = live.get("captureBinding") or {}
            watch = live.get("watch") or {}
            if (wrapped.get("operation") != operation or wrapped.get("appliedByThisCall") is not False
                    or wrapped.get("backgroundAutoApplyAuthorized") is not True
                    or live.get("schema") != "replaylab-shader-live-edit/v1"
                    or live.get("state") != "running" or watch.get("state") != "running"
                    or watch.get("caughtUp") is not True
                    or _path_key(live.get("workspacePath", "")) != _path_key(workspace)
                    or binding.get("captureSessionId") != capture_session_id
                    or _path_key(binding.get("capturePath", "")) != _path_key(capture_path)
                    or not isinstance(binding.get("instanceId"), str) or not binding["instanceId"]
                    or (expected_instance is not None and binding["instanceId"] != expected_instance)
                    or watch.get("observedSourceSha256") != record.get("sourceSha256")):
                raise RuntimeError("restored Shader editor returned a different capture or source")
            item.update(status="enabled",
                        liveEditId=_hex(live.get("liveEditId"), 32, "restored liveEditId"),
                        watchId=_hex(live.get("watchId"), 32, "restored watchId"),
                        captureBinding=copy.deepcopy(binding))
            if created_editor:
                created_jobs[started_live_id] = (workspace, copy.deepcopy(started_binding))
            result["enabled"] += 1
        except Exception as exc:
            diagnostic = "%s: %s" % (type(exc).__name__, exc)
            data = getattr(exc, "tool_data", None)
            if isinstance(data, Mapping) and isinstance(data.get("startTiming"), Mapping):
                item["startTiming"] = copy.deepcopy(data["startTiming"])
            if created_editor and started_live_id and started_binding is not None:
                try:
                    diagnostic += "; " + _retire_restored_editor_job(started_live_id, workspace, started_binding)
                except Exception as cleanup_exc:
                    diagnostic += "; exact editor retirement failed: %s" % cleanup_exc
            item.update(status="failed", error=diagnostic[:4096])
            result["failed"] += 1
        result["items"].append(item)
        if progress is not None:
            progress.emit("shader-prepare", "Restored Shader workspace editor admission finished",
                          current=result["attempted"], total=editor_total,
                          item=str(applied["workspacePath"]))
    try:
        _restore_control_checkpoint(cancel_event, deadline_monotonic, "after restored Shader editor batch")
        _require_restore_context(host, active, project_root, capture_path, capture_session_id)
        if progress is not None:
            progress.emit("shader-prepare", "Restored Shader workspace editing batch finished",
                          current=editor_total, total=editor_total)
    except Exception as exc:
        for item in result["items"]:
            if item.get("status") not in {"enabled", "deferred", "ready"}:
                continue
            previous_status = item["status"]
            diagnostic = "%s: %s" % (type(exc).__name__, exc)
            owned = created_jobs.get(item.get("liveEditId"))
            if owned is not None:
                try:
                    diagnostic += "; " + _retire_restored_editor_job(item["liveEditId"], *owned)
                except Exception as cleanup_exc:
                    diagnostic += "; exact editor retirement failed: %s" % cleanup_exc
            workspace_id, workspace_path = item["workspaceId"], item["workspacePath"]
            item.clear()
            item.update(workspaceId=workspace_id, workspacePath=workspace_path,
                        status="failed", error=diagnostic[:4096])
            result[previous_status] -= 1
            result["failed"] += 1
    return result


_VERIFIED_RDC_RESTORE_SCHEMA = "replaylab-verified-rdc-restore/v1"
_VERIFIED_RDC_RESTORE_VERSION = 2


class _VerifiedRdcRestorePolicyMiss(RuntimeError):
    """Expected cache invalidation before any hashing or replay mutation."""


def _verified_rdc_restore_path(replay_path: Path, capture_path: Path) -> Path:
    key = hashlib.sha256(_path_key(capture_path).encode("utf-8")).hexdigest()
    return replay_path / "derived" / "validated-rdc-restores" / (key + ".json")


def _verified_rdc_content_sha256(
    capture_path: Path, checkpoint: Callable[[str], None] | None = None,
) -> str:
    from . import file_content_cache
    if checkpoint is not None:
        checkpoint("reading verified RDC contents")
    try:
        return file_content_cache.digest_file(capture_path, persistent=True, checkpoint=checkpoint)
    except ValueError as error:
        raise RuntimeError("RDC contents changed while hashing") from error


def _verified_replay_content_sha256(capture, checkpoint=None):
    from .rdc_replay_identity import digest_file
    return digest_file(capture, checkpoint)


def _refresh_snapshot_replay_identity(snapshot):
    """An unsigned sidecar may route only the scope signed by every item."""
    payloads = [record.get("replacement", {}).get("restoreValidationReceipt", {}).get("payload", {})
                for record in snapshot["shaders"]]
    values = {p.get("captureReplaySha256") for p in payloads}
    if (len(values) == 1 and None not in values and all(
            p.get("captureReplayIdentitySchema") == "bread-rdc-replay-content/v1" for p in payloads)):
        snapshot["captureReplaySha256"] = _hex(next(iter(values)), 64, "replay content hash")
        snapshot["captureReplayIdentitySchema"] = "bread-rdc-replay-content/v1"


def _verified_workspace_binding(status: Mapping[str, Any]) -> str:
    # The canonical status reader validates these artifacts from current disk
    # bytes. Exclude editor/last-apply state, which legitimately changes when a
    # capture is reopened; retain the complete persisted ABI/compiler inputs.
    return _canonical_sha256({field: status.get(field) for field in (
        "workspaceId", "capture", "shader", "stage", "entryPoint", "revision",
        "source", "baseline", "targetProfile", "compilerBinding", "routePlan",
        "latestCompiled", "assemblyDisassemblyArtifacts",
    )})


def _verified_restore_binding_state(
    replay: ReplayProjectService, state: Mapping[str, Any],
) -> dict[str, Any]:
    if state.get("capture", {}).get("inventoryOnly") is True:
        checked = replay.validate_shader_inventory_snapshot(
            state["projectInventory"], state["inventoryBinding"])
        current = _inventory_binding_state(checked, state["capture"])
    else:
        current = replay.restore_knowledge_snapshot(
            str(state["captureId"]), str(state["catalogSha256"]))
    for field in ("projectId", "projectPath", "scopeSha256", "captureId", "catalogSha256",
                  "knowledgeContentSha256", "matcherPolicySha256", "workspaces",
                  "materials", "inventoryBinding", "projectInventory"):
        if current.get(field) != state.get(field):
            raise RuntimeError("verified RDC project contents changed: " + field)
    return current


def _verified_restore_ticket(
    record: Mapping[str, Any], status: Mapping[str, Any], capture_sha256: str,
    replay_sha256: str | None = None,
) -> Mapping[str, Any]:
    replacement = record.get("replacement") or {}
    receipt = replacement.get("restoreValidationReceipt") or {}
    payload = receipt.get("payload") or {}
    latest = status.get("latestCompiled") or {}
    gate = latest.get("abiGate") or {}
    expected = {
        "schema": "replaylab-validated-shader-restore/v1",
        "captureSha256": capture_sha256,
        "captureId": status.get("capture", {}).get("captureId"),
        "workspaceId": status.get("workspaceId"),
        "revision": record.get("candidateRevision"),
        "sourceSha256": record.get("sourceSha256"),
        "candidateRawSha256": record.get("candidateRawSha256"),
        "originalRawSha256": status.get("baseline", {}).get("rawSha256"),
        "physicalAbiSha256": status.get("baseline", {}).get("abiSha256"),
        "api": _asset_api(status.get("capture", {}).get("api")),
        "stage": status.get("stage"),
        "entryPoint": status.get("entryPoint", ""),
        "encoding": _asset_encoding(latest.get("kind")),
        "abiRequestId": gate.get("requestId"),
        "abiReportSha256": gate.get("reportSha256"),
    }
    replay_bound = bool(replay_sha256 and payload.get("captureReplaySha256") == replay_sha256
        and payload.get("captureReplayIdentitySchema") == "bread-rdc-replay-content/v1")
    if (replay_sha256 is not None and not replay_bound):
        raise RuntimeError("verified RDC restore Shader has a different replay scope")
    if (not isinstance(receipt, Mapping) or not isinstance(payload, Mapping)
            or any(payload.get(key) != value for key, value in expected.items()
                   if key != "physicalAbiSha256" and not (key == "captureSha256" and replay_bound))
            or payload.get("physicalAbiSha256") not in _workspace_physical_abi_hashes(status)
            or gate.get("status") != "compatible"
            or not isinstance(record.get("validation"), Mapping)
            or record["validation"].get("compatible") is not True):
        raise RuntimeError("verified RDC restore has no matching signed validation receipt")
    _hex(receipt.get("signature"), 64, "verified RDC receipt signature")
    # Host and Bridge independently check the HMAC and current resource bytes.
    # This derived JSON seal is corruption detection, never apply authority.
    return receipt


def _verified_restore_workspace_statuses(
    host: Any, active: Mapping[str, Any], project: Path, capture: Path,
    snapshot: Mapping[str, Any], prepared_statuses: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    statuses = []
    shader_root = Path(str(active.get("paths", {}).get("shaderWorkspaces", ""))).resolve()
    workspaces = [_resolve_workspace_path(project, shader_root, record["managedWorkspace"]["path"])
                  for record in snapshot["shaders"]]
    if prepared_statuses is None:
        from .restore_file_preparation import artifact_statuses
        factory = getattr(host, "_shader_workspace_artifact_service", None)
        if callable(factory):
            prepared_statuses = artifact_statuses(factory, workspaces,
                getattr(_RESTORE_THREAD_STATE, "control_checkpoint", None))
        else:
            prepared_statuses = [_workspace_artifact_service(host).status(str(path)) for path in workspaces]
    if len(prepared_statuses) != len(workspaces):
        raise RuntimeError("verified RDC artifact preparation is incomplete")
    seen = set()
    for index, record in enumerate(snapshot["shaders"]):
        managed = record["managedWorkspace"]
        workspace = workspaces[index]
        status = prepared_statuses[index]
        identity = record["identity"]
        _capture_shader(snapshot["bindingState"], identity)
        knowledge = snapshot["bindingState"].get("workspaces", {}).get(record.get("knowledgeId"))
        if isinstance(knowledge, Mapping) and source_only_restore_reason(
                knowledge, snapshot["bindingState"].get("projectPath")):
            raise _VerifiedRdcRestorePolicyMiss("source-only-organization-draft")
        if (not isinstance(knowledge, Mapping)
                or knowledge.get("sourceSha256") != record.get("sourceSha256")
                or any(knowledge.get(field) != identity.get(field)
                       for field in ("rawSha256", "stage", "entryPoint"))):
            raise RuntimeError("verified RDC workspace differs from shared project source")
        latest = status.get("latestCompiled") or {}
        source = status.get("source") or {}
        if status.get("sourceDependenciesCurrent", True) is not True:
            raise _VerifiedRdcRestorePolicyMiss("source-dependencies-changed")
        if (managed.get("workspaceId") in seen
                or status.get("workspaceId") != managed.get("workspaceId")
                or _path_key(status.get("workspacePath", "")) != _path_key(workspace)
                or _path_key(status.get("capture", {}).get("capturePath", "")) != _path_key(capture)
                or status.get("shader", {}).get("rawSha256") != identity.get("rawSha256")
                or status.get("stage") != identity.get("stage")
                or status.get("entryPoint", "") != identity.get("entryPoint", "")
                or source.get("present") is not True
                or source.get("currentSha256") != record.get("sourceSha256")
                or latest.get("sourceSha256") != record.get("sourceSha256")
                or latest.get("revision") != record.get("candidateRevision")
                or latest.get("rawSha256") != record.get("candidateRawSha256")
                or _verified_workspace_binding(status) != snapshot["workspaceBindings"][index]):
            raise RuntimeError("verified RDC workspace contents changed")
        _verified_restore_ticket(record, status, snapshot["captureSha256"], snapshot.get("captureReplaySha256"))
        seen.add(managed["workspaceId"])
        statuses.append(status)
    return statuses


def _load_verified_rdc_restore(
    host: Any, replay: ReplayProjectService, active: Mapping[str, Any],
    project: Path, replay_path: Path, capture: Path,
    checkpoint: Callable[[str], None],
    current_validation_policy_sha256: str | None = None,
    compatible_policy_sha256s: Sequence[str] = (),
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    if not callable(getattr(host, "_apply_verified_project_restore", None)):
        return None
    path = _verified_rdc_restore_path(replay_path, capture)
    if not path.is_file():
        return None
    if path.stat().st_size > _MAX_FAST_CATALOG_BYTES:
        raise RuntimeError("verified RDC restore snapshot exceeds size limit")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    seal = snapshot.pop("sealSha256", None)
    if (seal != _canonical_sha256(snapshot)
            or snapshot.get("schema") != _VERIFIED_RDC_RESTORE_SCHEMA
            or snapshot.get("projectId") != active["replayProject"]["projectId"]
            or snapshot.get("projectWorkspaceId") != active["workspaceId"]
            or _path_key(snapshot.get("capturePath", "")) != _path_key(capture)
            or not isinstance(snapshot.get("shaders"), list)
            or not 0 < len(snapshot["shaders"]) <= _MAX_FAST_PROJECT_SHADERS
            or len(snapshot.get("workspaceBindings", [])) != len(snapshot["shaders"])):
        raise RuntimeError("verified RDC restore snapshot is invalid")
    if snapshot.get("schemaVersion") != _VERIFIED_RDC_RESTORE_VERSION:
        # Version 1 could be primed while a replacement hid an original
        # program from the resource inventory. Rebuild its coverage once.
        raise _VerifiedRdcRestorePolicyMiss("original-shader-inventory-policy-changed")
    deferred = snapshot.get("deferredShaders", [])
    if (not isinstance(deferred, list) or len(deferred) > _MAX_FAST_PROJECT_SHADERS
            or any(not isinstance(item, Mapping) or set(item) != {"identity", "knowledgeId", "requiresIndividualRecovery"}
                   or item.get("requiresIndividualRecovery") is not True for item in deferred)):
        raise RuntimeError("verified RDC deferred Shader inventory is invalid")
    # Deferred identities prove inventory coverage only. They carry no ticket
    # or candidate and always enter ordinary recovery on the next open.
    _verified_restore_coverage(snapshot["bindingState"], snapshot["reusePlan"],
                               snapshot["shaders"] + deferred, snapshot.get("inventoryEvidence"))
    if (not isinstance(current_validation_policy_sha256, str)
            or len(current_validation_policy_sha256) != 64
            or any(character not in "0123456789abcdef"
                   for character in current_validation_policy_sha256)):
        raise _VerifiedRdcRestorePolicyMiss("validation-policy-unavailable")
    policy_matches = lambda record: record.get("replacement", {}).get("restoreValidationReceipt", {}).get(
        "payload", {}).get("validationPolicySha256") in (current_validation_policy_sha256, *compatible_policy_sha256s)
    prepared = None
    if any(not policy_matches(record) for record in snapshot["shaders"]):
        refreshed = _refresh_verified_restore_pairs(host, active, project, capture, snapshot,
            current_validation_policy_sha256, compatible_policy_sha256s, checkpoint)
        if refreshed is None:
            raise _VerifiedRdcRestorePolicyMiss("validation-policy-changed")
        snapshot, prepared = refreshed
    for record in snapshot["shaders"]:
        replacement = record.get("replacement") if isinstance(record, Mapping) else None
        receipt = replacement.get("restoreValidationReceipt") if isinstance(replacement, Mapping) else None
        payload = receipt.get("payload") if isinstance(receipt, Mapping) else None
        if (not isinstance(payload, Mapping) or payload.get("validationPolicySha256") not in
                (current_validation_policy_sha256, *compatible_policy_sha256s)):
            # The current process supplied this hint in the already-required
            # identity read. Equality only permits the normal checks to run;
            # it does not replace Bridge HMAC/content/session validation.
            raise _VerifiedRdcRestorePolicyMiss("validation-policy-changed")
        physical_binding = payload.get("physicalValidationBinding")
        if physical_binding is not None and (
                not isinstance(physical_binding, Mapping)
                or physical_binding.get("schema") != "replaylab-shader-physical-validation-binding/v1"
                or physical_binding.get("schemaVersion") != 1
                or _canonical_sha256(physical_binding) != payload.get("validationPolicySha256")):
            # The Bridge's current identity hint is the physical-domain digest,
            # not its package/UI provenance. HMAC and native live-original
            # checks still occur inside Bridge before a replacement is built.
            raise _VerifiedRdcRestorePolicyMiss("physical-validation-binding-changed")
    if snapshot.get("captureReplaySha256") is not None:
        expected_replay = snapshot["captureReplaySha256"]
        if (snapshot.get("captureReplayIdentitySchema") != "bread-rdc-replay-content/v1" or
                any(record.get("replacement", {}).get("restoreValidationReceipt", {}).get("payload", {}).get("captureReplaySha256")
                    != expected_replay for record in snapshot["shaders"])):
            raise RuntimeError("verified RDC replay identity is not signed by every Shader")
        if _verified_replay_content_sha256(capture, checkpoint) != expected_replay:
            raise RuntimeError("verified RDC replay contents changed")
    elif _verified_rdc_content_sha256(capture, checkpoint) != snapshot["captureSha256"]:
        raise RuntimeError("verified RDC file contents changed")
    return _prepare_verified_restore_subset(host, replay, active, project, capture, snapshot, checkpoint,
        prepared_statuses=prepared, validation_policy=current_validation_policy_sha256,
        compatible_policies=compatible_policy_sha256s)


def _current_verified_pair_record(host, active, project, capture, snapshot, record, status, policy, compatible):
    prior = _load_shader_pair_validation(project, status, policy, compatible)
    if prior is None:
        raise RuntimeError("no current signed Shader pair")
    # Workspace-local proofs retain an ABI lease rather than a full apply
    # response. Reconstitute its report identity from the authenticated ticket
    # and the current artifact reader; never substitute a compatible boolean.
    gate = status["latestCompiled"]["abiGate"]
    validation = copy.deepcopy(prior.get("validation") or record.get("validation") or {})
    validation.update(compatible=True, workspacePath=status["workspacePath"], revision=status["revision"],
        requestId=gate["requestId"], abiReportSha256=gate["reportSha256"])
    if gate.get("candidateAbiSha256"):
        validation["candidateAbiSha256"] = gate["candidateAbiSha256"]
    if gate.get("reportPath"):
        validation["abiReportPath"] = str(Path(status["workspacePath"]) / gate["reportPath"])
    saved = copy.deepcopy(record)
    saved.update(replacement=copy.deepcopy(prior["replacement"]),
                 validation=validation,
                 sourceSha256=status["source"]["currentSha256"],
                 candidateRawSha256=status["latestCompiled"]["rawSha256"],
                 candidateRevision=status["latestCompiled"]["revision"])
    binding = _verified_workspace_binding(status)
    one = {**snapshot, "shaders": [saved], "workspaceBindings": [binding]}
    _verified_restore_workspace_statuses(host, active, project, capture, one, [status])
    return saved, binding


def _refresh_verified_restore_pairs(host, active, project, capture, snapshot, policy, compatible, checkpoint):
    """Recover already verified pairs after an interrupted whole-project memo write.

    Individual receipts are still authenticated by the Bridge on apply. This
    only avoids repeating compilation/ABI work whose exact signed result is
    already on disk under the current physical validation policy.
    """
    from .restore_file_preparation import artifact_statuses
    factory = getattr(host, "_shader_workspace_artifact_service", None) or (lambda: _workspace_artifact_service(host))
    shader_root = Path(active["paths"]["shaderWorkspaces"]).resolve()
    paths = [_resolve_workspace_path(project, shader_root, record["managedWorkspace"]["path"])
             for record in snapshot["shaders"]]
    prepared = artifact_statuses(factory, paths, checkpoint, collect_errors=True)
    selected, statuses, bindings = [], [], []
    deferred = list(snapshot.get("deferredShaders", []))
    for record, status in zip(snapshot["shaders"], prepared):
        checkpoint("before cached Shader pair recovery")
        try:
            if isinstance(status, Exception):
                raise status
            saved, binding = _current_verified_pair_record(host, active, project, capture, snapshot,
                record, status, policy, compatible)
            selected.append(saved); statuses.append(status); bindings.append(binding)
        except (RestoreControlStop, RestoreContextChanged, RestoreContextUnavailable):
            raise
        except Exception:
            deferred.append({"identity": copy.deepcopy(record["identity"]),
                "knowledgeId": record["knowledgeId"], "requiresIndividualRecovery": True})
    if not selected:
        return None
    return {**snapshot, "shaders": selected, "workspaceBindings": bindings,
            "deferredShaders": deferred, "_currentPairRecovery": True}, statuses


def _prepare_verified_restore_subset(host, replay, active, project, capture, snapshot, checkpoint,
                                     prepared_statuses=None, validation_policy=None, compatible_policies=()):
    """Read the current project head once, then isolate changed/broken items."""
    old = snapshot["bindingState"]
    if old.get("capture", {}).get("inventoryOnly") is True:
        current = replay.reuse_snapshot_for_shader_inventory(old["projectInventory"])
        state = _inventory_binding_state(current, old["capture"])
        plan = current["reusePlan"]
    else:
        state = replay.restore_knowledge_snapshot(str(old["captureId"]), str(old["catalogSha256"]))
        plan = replay.reuse_plan(str(old["captureId"]), str(old["catalogSha256"]))
    for field in ("projectId", "projectPath", "scopeSha256", "captureId", "catalogSha256", "matcherPolicySha256"):
        if state.get(field) != old.get(field):
            raise RuntimeError("verified RDC project domain changed: " + field)
    def key(identity, knowledge):
        return tuple(identity.get(field) for field in ("resourceId", "stage", "entryPoint", "rawSha256")) + (knowledge,)
    allowed = set()
    for match in plan.get("matches", []):
        candidates = match.get("workspaceCandidates") or []
        chosen = [c for c in candidates if c.get("activeCandidate") is True]
        if not chosen and len(candidates) == 1:
            chosen = candidates
        if len(chosen) == 1 and chosen[0].get("sourceSeedEligible") is True:
            knowledge = state.get("workspaces", {}).get(chosen[0].get("knowledgeId"))
            if isinstance(knowledge, Mapping) and source_only_restore_reason(knowledge, state.get("projectPath")):
                continue
            allowed.add(key(match["shader"], chosen[0].get("knowledgeId")))
    from .restore_file_preparation import artifact_statuses
    factory = getattr(host, "_shader_workspace_artifact_service", None) or (lambda: _workspace_artifact_service(host))
    shader_root = Path(active["paths"]["shaderWorkspaces"]).resolve()
    paths = [_resolve_workspace_path(project, shader_root, record["managedWorkspace"]["path"])
             for record in snapshot["shaders"]]
    prepared = (prepared_statuses if prepared_statuses is not None else
                artifact_statuses(factory, paths, checkpoint, collect_errors=True))
    selected, statuses, bindings, excluded = [], [], [], []
    for index, (record, status) in enumerate(zip(snapshot["shaders"], prepared)):
        try:
            if isinstance(status, Exception):
                raise status
            if key(record["identity"], record.get("knowledgeId")) not in allowed:
                raise RuntimeError("project source head changed")
            one = {**snapshot, "bindingState": state, "shaders": [record],
                   "workspaceBindings": [snapshot["workspaceBindings"][index]]}
            binding = snapshot["workspaceBindings"][index]
            try:
                _verified_restore_workspace_statuses(host, active, project, capture, one, [status])
            except (RestoreControlStop, RestoreContextChanged, RestoreContextUnavailable):
                raise
            except Exception:
                if validation_policy is None:
                    raise
                record, binding = _current_verified_pair_record(host, active, project, capture, snapshot,
                    record, status, validation_policy, compatible_policies)
                snapshot["_currentPairRecovery"] = True
            selected.append(record); statuses.append(status)
            bindings.append(binding)
        except (RestoreControlStop, RestoreContextChanged, RestoreContextUnavailable):
            raise
        except Exception as error:
            excluded.append({"workspaceId": record["managedWorkspace"]["workspaceId"], "error": str(error)[:1024]})
    if not selected:
        detail = excluded[0]["error"] if excluded else "no matching project source head"
        raise _VerifiedRdcRestorePolicyMiss("no-unchanged-verified-shaders: " + detail)
    partial = bool(excluded or {key(r["identity"], r.get("knowledgeId")) for r in selected} != allowed)
    deferred = list(snapshot.get("deferredShaders", []))
    selected_keys = {key(r["identity"], r.get("knowledgeId")) for r in selected}
    deferred.extend({"identity": copy.deepcopy(r["identity"]), "knowledgeId": r["knowledgeId"],
                     "requiresIndividualRecovery": True} for r in snapshot["shaders"]
                    if key(r["identity"], r.get("knowledgeId")) not in selected_keys)
    return {**snapshot, "shaders": selected, "workspaceBindings": bindings,
            "deferredShaders": deferred,
            "bindingState": state, "reusePlan": plan, "_partialRecovery": partial,
            "_individualRecovery": excluded}, statuses


def _verified_restore_coverage(
    state: Mapping[str, Any], plan: Mapping[str, Any], records: Sequence[Mapping[str, Any]],
    inventory_evidence: Mapping[str, Any] | None,
) -> None:
    """Require all matched originals, without requiring another capture's assets."""
    if state.get("capture", {}).get("inventoryOnly") is True:
        if (not isinstance(inventory_evidence, Mapping)
                or inventory_evidence.get("originalShaderIdentityPolicy") != "captured-original-owner-v1"
                or inventory_evidence.get("observedRawSha256s") != sorted({
                    item["rawSha256"] for item in state["capture"]["shaders"]})):
            raise RuntimeError("verified RDC restore requires an inventory of captured original Shaders")
    def key(identity, knowledge_id):
        return (identity.get("resourceId"), identity.get("stage"), identity.get("entryPoint", ""),
                identity.get("rawSha256"), knowledge_id)
    expected = set()
    for match in plan.get("matches") or []:
        candidates = match.get("workspaceCandidates") or []
        active = [item for item in candidates if item.get("activeCandidate") is True]
        if not active and len(candidates) == 1:
            active = candidates
        if len(active) == 1 and active[0].get("sourceSeedEligible") is True:
            knowledge_id = active[0].get("knowledgeId")
            knowledge = state.get("workspaces", {}).get(knowledge_id)
            if isinstance(knowledge, Mapping) and source_only_restore_reason(
                    knowledge, state.get("projectPath")):
                continue
            expected.add(key(match["shader"], knowledge_id))
    actual = {key(item["identity"], item.get("knowledgeId")) for item in records}
    if not expected or actual != expected or len(actual) != len(records):
        raise RuntimeError("verified RDC restore omits or repeats a matched original Shader")


def _build_verified_rdc_restore(
    host: Any, replay: ReplayProjectService, active: Mapping[str, Any],
    project: Path, capture: Path, binding_state: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]], applied_workspaces: Sequence[Any],
    checkpoint: Callable[[str], None],
    *, inventory: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if (not callable(getattr(host, "_apply_verified_project_restore", None))
            or not records or len(records) > _MAX_FAST_PROJECT_SHADERS):
        return None
    applied_by_id = {status["workspaceId"]: applied
                     for workspace, status, applied, session in applied_workspaces}
    # Old unsigned results cannot prime this accelerator. A subsequent full
    # apply will issue the first signed receipt without trusting old booleans.
    reusable, deferred = [], []
    for record in records:
        receipt = applied_by_id.get(record["managedWorkspace"]["workspaceId"], {})\
            .get("replacement", {}).get("restoreValidationReceipt")
        if receipt:
            reusable.append(record)
        elif record.get("manualEdit") is True:
            deferred.append({"identity": copy.deepcopy(record["identity"]),
                             "knowledgeId": record["knowledgeId"], "requiresIndividualRecovery": True})
        else:
            return None
    if not reusable:
        return None
    state = _verified_restore_binding_state(replay, binding_state)
    plan = state.get("reusePlan")
    if not isinstance(plan, Mapping):
        plan = replay.reuse_plan(str(state["captureId"]), str(state["catalogSha256"]))
    evidence = None
    if state.get("capture", {}).get("inventoryOnly") is True:
        evidence = {
            "originalShaderIdentityPolicy": (inventory or {}).get("originalShaderIdentityPolicy"),
            "observedRawSha256s": sorted({item["rawSha256"] for item in (inventory or {}).get("shaders", [])}),
        }
    _verified_restore_coverage(state, plan, records, evidence)
    capture_sha256 = _verified_rdc_content_sha256(capture, checkpoint)
    snapshot = {
        "schema": _VERIFIED_RDC_RESTORE_SCHEMA, "schemaVersion": _VERIFIED_RDC_RESTORE_VERSION,
        "capturePath": str(capture), "captureSha256": capture_sha256,
        "projectId": active["replayProject"]["projectId"],
        "projectWorkspaceId": active["workspaceId"],
        "bindingState": copy.deepcopy(dict(state)), "reusePlan": copy.deepcopy(plan),
        "inventoryEvidence": evidence,
        "shaders": [], "workspaceBindings": [],
    }
    if deferred:
        snapshot["deferredShaders"] = deferred
    prepared_statuses = []
    for record in reusable:
        applied = applied_by_id[record["managedWorkspace"]["workspaceId"]]
        saved = copy.deepcopy(dict(record))
        saved.update(validation=copy.deepcopy(applied["validation"]),
                     replacement=copy.deepcopy(applied["replacement"]))
        workspace = project / record["managedWorkspace"]["path"]
        status = _workspace_artifact_service(host).status(str(workspace))
        prepared_statuses.append(status)
        snapshot["shaders"].append(saved)
        snapshot["workspaceBindings"].append(_verified_workspace_binding(status))
    _refresh_snapshot_replay_identity(snapshot)
    _verified_restore_workspace_statuses(host, active, project, capture, snapshot, prepared_statuses)
    snapshot["sealSha256"] = _canonical_sha256(snapshot)
    return snapshot


def _write_verified_rdc_restore(replay_path: Path, capture: Path, snapshot: Mapping[str, Any]) -> None:
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    if len(encoded) > _MAX_FAST_CATALOG_BYTES:
        raise RuntimeError("verified RDC restore snapshot exceeds size limit")
    atomic_write_bytes(_verified_rdc_restore_path(replay_path, capture), encoded)


def _apply_verified_rdc_restore(
    host: Any, replay: ReplayProjectService, active: Mapping[str, Any],
    project: Path, capture: Path, session: str, snapshot: Mapping[str, Any],
    statuses: Sequence[Mapping[str, Any]], progress: _Progress, result: dict[str, Any],
    applied_workspaces: list[Any], alias_transactions: list[dict[str, str]],
    checkpoint: Callable[[str], None],
    *, restored_material_sessions=None,
) -> list[dict[str, Any]]:
    checkpoint("before verified RDC Shader apply")
    progress.emit("shader-apply", "Applying cached Shader %d/%d" % (0, len(statuses)),
                  current=0, total=len(statuses))
    _require_restore_context(host, active, project, capture, session)
    for match in snapshot["reusePlan"].get("matches", []):
        for candidate in match.get("workspaceCandidates", []):
            if candidate.get("pendingSource") is not None:
                _fallback_pending_knowledge(candidate, snapshot["bindingState"])
    scope = getattr(host, '_shader_artifact_replay_scope', None)
    # A saved compiled candidate does not execute a compiler. Use the same
    # artifact-only replay service as individual recovery, so a package/tool
    # route update cannot invalidate its already authenticated GPU proof.
    with scope() if callable(scope) else contextlib.nullcontext():
        response = host._apply_verified_project_restore(
            snapshot, list(statuses), session, result["operationId"])
    if isinstance(response, Mapping) and response.get("batchTimings"):
        progress.emit("shader-apply", "Applying cached Shader %d/%d" % (len(statuses), len(statuses)),
            current=len(statuses), total=len(statuses), detail={"restoreBatches": response["batchTimings"]})
    items = response.get("items") if isinstance(response, Mapping) else None
    if not isinstance(items, list):
        raise RuntimeError("verified RDC restore returned an incomplete apply batch")
    # Register every committed item before validating the response so a bad
    # later item or changed project unwinds the complete batch.
    statuses_by_id = {status["workspaceId"]: status for status in statuses}
    for index, applied in enumerate(items):
        binding = applied.get("binding") if isinstance(applied, Mapping) else None
        # Older Host responses have no binding envelope; their ordered result
        # is checked against every exact replacement field below as before.
        status = (statuses_by_id.get(binding.get("workspaceId")) if isinstance(binding, Mapping)
                  else statuses[index] if index < len(statuses) and isinstance(applied, Mapping) else None)
        if status is None:
            raise RuntimeError("verified RDC restore returned an unrelated Shader")
        if isinstance(applied, Mapping) and applied.get("replacement", {}).get("operationCreatedReplacement") is True:
            applied_workspaces.append((Path(status["workspacePath"]), copy.deepcopy(dict(status)),
                                       copy.deepcopy(dict(applied)), session))
    if response.get("itemErrors"):
        # Confirmed successes stay owned and will be adopted by the ordinary
        # per-item recovery path. Only failed/changed inputs require work again.
        raise _VerifiedRdcRestorePolicyMiss("batch-items-require-individual-recovery")
    if response.get("applied") is not True or len(items) != len(statuses):
        raise RuntimeError("verified RDC restore was not applied")
    shader_items = []
    upgraded_receipts = False
    applied_by_original: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record, status, applied in zip(snapshot["shaders"], statuses, items):
        replacement = applied.get("replacement") or {}
        validation = applied.get("validation") or {}
        if (applied.get("applied") is not True
                or applied.get("operation") != "apply-shared-resource"
                or replacement.get("abiValidationMode") != "verified-rdc-restore"
                or replacement.get("captureSessionId") != session
                or replacement.get("workspaceId") != status["workspaceId"]
                or replacement.get("revision") != record["candidateRevision"]
                or replacement.get("candidateRawSha256") != record["candidateRawSha256"]
                or replacement.get("originalRawSha256") != record["identity"]["rawSha256"]
                or replacement.get("restoreValidationReceipt") != record["replacement"]["restoreValidationReceipt"]
                or validation.get("validationScope") != "verified-rdc-restore"
                or validation.get("reusedValidation") is not True
                or validation.get("captureSessionId") != session):
            raise RuntimeError("verified RDC apply returned invalid current-session evidence")
        _hex(replacement.get("replacementId"), 32, "verified RDC replacementId")
        upgrade = replacement.get("restoreValidationReceiptUpgrade")
        if isinstance(upgrade, Mapping):
            payload = dict(upgrade.get("payload") or {})
            replay_sha = payload.pop("captureReplaySha256", None)
            schema = payload.pop("captureReplayIdentitySchema", None)
            if (schema == "bread-rdc-replay-content/v1" and
                    payload == record["replacement"]["restoreValidationReceipt"]["payload"]):
                _hex(replay_sha, 64, "upgraded replay-content hash")
                _hex(upgrade.get("signature"), 64, "upgraded replay signature")
                record["replacement"]["restoreValidationReceipt"] = copy.deepcopy(dict(upgrade))
                upgraded_receipts = True
        if (replacement.get("originalResourceId") != record["identity"]["resourceId"]
                and any(match.get("aliasCandidates") for match in snapshot["reusePlan"].get("matches", []))):
            # Shader bytes are authenticated, but display aliases still use the
            # saved inventory's capture-local owners. Rebuild that inventory if
            # RenderDoc assigned different IDs in this session; never attach a
            # saved alias table to an unrelated resource with the old number.
            raise RuntimeError("verified RDC resource ownership changed; alias inventory requires rebinding")
        shader_items.append({
            "status": "applied", "source": "verified-rdc-restore",
            "workspacePath": status["workspacePath"], "workspaceId": status["workspaceId"],
            "resourceId": replacement["originalResourceId"], "stage": record["identity"]["stage"],
            "replacementId": replacement["replacementId"], "knowledgeId": record["knowledgeId"],
            "reusedValidation": True, "sourceSha256": record["sourceSha256"],
        })
        local_display = _restore_local_source_display(host, active, project, capture, record,
            snapshot["bindingState"].get("workspaces", {}).get(record.get("knowledgeId")))
        if local_display is not None:
            shader_items[-1]["preservedLocalSource"] = local_display
        original = record["identity"]
        applied_by_original[(record["knowledgeId"], original["resourceId"], original["stage"],
                             original.get("entryPoint", ""), original["rawSha256"])] = shader_items[-1]
    checkpoint("after verified RDC Shader apply")
    _require_restore_context(host, active, project, capture, session)
    state = snapshot["bindingState"]
    _verified_restore_binding_state(replay, state)
    if snapshot.get("_currentPairRecovery"):
        # Publish only after the Bridge has authenticated/applied these exact
        # receipts in this capture. No failed/deferred item gains authority.
        renewed = {key: copy.deepcopy(value) for key, value in snapshot.items()
                   if not key.startswith("_") and key != "sealSha256"}
        try:
            _verified_restore_coverage(state, renewed["reusePlan"],
                renewed["shaders"] + renewed.get("deferredShaders", []), renewed.get("inventoryEvidence"))
            _refresh_snapshot_replay_identity(renewed)
            renewed["sealSha256"] = _canonical_sha256(renewed)
            _write_verified_rdc_restore(Path(state["projectPath"]), capture, renewed)
        except (OSError, ValueError, RuntimeError):
            pass  # A disposable memo failure cannot undo an authenticated apply.
    if upgraded_receipts and not snapshot.get("_partialRecovery"):
        _refresh_snapshot_replay_identity(snapshot)
        if snapshot.get("captureReplaySha256") is not None:
            upgraded_snapshot = copy.deepcopy(dict(snapshot))
            upgraded_snapshot.pop("_partialRecovery", None)
            upgraded_snapshot.pop("_individualRecovery", None)
            upgraded_snapshot["sealSha256"] = _canonical_sha256(upgraded_snapshot)
            try:
                _write_verified_rdc_restore(Path(state["projectPath"]), capture, upgraded_snapshot)
            except (OSError, ValueError, RuntimeError):
                # A disposable cache write cannot revoke the checked effect.
                pass
    plan = snapshot["reusePlan"]
    _restore_reuse_aliases_best_effort(
        host, replay, active, project, capture, session, plan, plan.get("matches", []),
        progress, checkpoint, state.get("projectInventory"), state.get("inventoryBinding"),
        alias_transactions)
    result["capture"]["captureId"] = state["captureId"]
    result["restoreMode"] = "verified-rdc-restore"
    result["cache"] = {"hit": True, "reason": "same-rdc-verified-content"}
    result["verifiedRdcCache"] = {"hit": True, "written": False, "reason": "same-rdc-verified-content"}
    pending_items = []
    for match in plan.get("matches", []):
        candidates = match.get("workspaceCandidates") or []
        active_candidates = [item for item in candidates if item.get("activeCandidate") is True]
        if not active_candidates and len(candidates) == 1:
            active_candidates = candidates
        if len(active_candidates) != 1:
            continue
        knowledge = state.get("workspaces", {}).get(active_candidates[0].get("knowledgeId"))
        candidate = active_candidates[0]
        if candidate.get("pendingSource") is not None:
            original = match["shader"]
            applied_item = applied_by_original.get((candidate["knowledgeId"], original["resourceId"],
                original["stage"], original.get("entryPoint", ""), original["rawSha256"]))
            if applied_item is None:
                raise RuntimeError("pending project source omitted its validated replay version")
            current_identity = {**match["shader"], "resourceId": applied_item["resourceId"]}
            pending_display = _show_fallback_pending_source(host, candidate, state, current_identity,
                state["captureId"], capture, session)
            applied_item["pendingSource"] = pending_display
        reason = (source_only_restore_reason(knowledge, state.get("projectPath"))
                  if isinstance(knowledge, Mapping) else None)
        if reason:
            checkpoint("before pending project source display")
            pending_items.append(_pending_project_source_item(host, knowledge, match["shader"], reason,
                project_id=state["projectId"], capture_id=state["captureId"], capture_path=capture,
                capture_session_id=session))
    result["shaders"].update(attempted=len(items), applied=len(items),
                             skipped=len(pending_items), items=shader_items + pending_items)
    result["realizedReceipts"].update(
        produced=len(items), reusedValidation=len(items),
        mutationsCreated=sum(item["replacement"].get("operationCreatedReplacement") is True for item in items))
    records = [dict(record) for record in snapshot["shaders"]]
    if snapshot.get("_partialRecovery"):
        recovered = _recover_verified_restore_exceptions(host, replay, active, project, capture, session,
            state, plan, records, result, progress, checkpoint, applied_workspaces, alias_transactions,
            restored_material_sessions=restored_material_sessions)
        records.extend(recovered)
        if not result["shaders"]["failed"] and any(r.get("manualEdit") is not True for r in recovered):
            # A repaired exception may now join its signed siblings. A still
            # manual draft cannot be promoted merely because replay succeeded.
            try:
                refreshed = _build_verified_rdc_restore(host, replay, active, project, capture,
                    state, records, applied_workspaces, checkpoint,
                    inventory={"originalShaderIdentityPolicy": (snapshot.get("inventoryEvidence") or {}).get(
                        "originalShaderIdentityPolicy"), "shaders": state["capture"]["shaders"]})
                if refreshed is not None:
                    _write_verified_rdc_restore(Path(state["projectPath"]), capture, refreshed)
            except (RestoreControlStop, RestoreContextChanged, RestoreContextUnavailable):
                raise
            except Exception:
                pass
    return records


def _recover_verified_restore_exceptions(host, replay, active, project, capture, session,
        state, plan, records, result, progress, checkpoint, applied_workspaces, alias_transactions,
        *, restored_material_sessions=None):
    # Preserve the successful native subset. Only the unmatched physical
    # resources enter ordinary inventory/preparation/compile/apply paths.
    restored = {tuple(record["identity"].get(field) for field in (
        "resourceId", "stage", "entryPoint", "rawSha256")) for record in records}
    missing = [match["shader"] for match in plan.get("matches", [])
               if tuple(match["shader"].get(field) for field in (
                   "resourceId", "stage", "entryPoint", "rawSha256")) not in restored]
    extra_records = []
    all_targets = sorted({item["rawSha256"] for item in missing})
    for offset in range(0, len(all_targets), _MAX_TARGET_RAW_SHADER_HASHES):
        targets = all_targets[offset:offset + _MAX_TARGET_RAW_SHADER_HASHES]
        try:
            inventory = _inventory_shader_resources(host, capture, session, state["capture"]["api"],
                progress, checkpoint, target_raw_sha256s=targets)
            prepared = _prepare_inventory_response(replay, active, inventory, progress, checkpoint)
        except (RestoreControlStop, RestoreContextChanged, RestoreContextUnavailable, RestoreRollbackFailed):
            raise
        except Exception as error:
            # An unavailable exception must not fall through to whole-project
            # recovery after its signed siblings were already applied.
            for identity in missing:
                if identity["rawSha256"] not in targets:
                    continue
                result["shaders"]["attempted"] += 1
                result["shaders"]["failed"] += 1
                match = next(item for item in plan["matches"] if item["shader"] == identity)
                candidates = match.get("workspaceCandidates") or []
                chosen = [item for item in candidates if item.get("activeCandidate") is True]
                if not chosen and len(candidates) == 1:
                    chosen = candidates
                knowledge_id = chosen[0]["knowledgeId"] if len(chosen) == 1 else None
                result["shaders"]["items"].append({"status": "failed", "source": "reuse-plan",
                    "knowledgeId": knowledge_id,
                    "resourceId": identity["resourceId"], "stage": identity["stage"],
                    "error": str(error)[:2048] or type(error).__name__})
            continue
        prepared["reusePlan"]["matches"] = [match for match in prepared["reusePlan"].get("matches", [])
            if tuple(match["shader"].get(field) for field in (
                "resourceId", "stage", "entryPoint", "rawSha256")) not in restored]
        prepared["reusePlan"]["matchedShaderCount"] = len(prepared["reusePlan"]["matches"])
        recovered, unused_materials, unused_state = _restore_from_reuse_plan(
            host, replay, active, project, capture, session, prepared, inventory, progress,
            result["shaders"], result["materials"], applied_workspaces,
            restored_material_sessions if restored_material_sessions is not None else [], alias_transactions,
            checkpoint, receipt_stats=result["realizedReceipts"])
        extra_records.extend(recovered)
    result["verifiedRdcCache"].update(reason="same-rdc-verified-subset")
    result["cache"] = {"hit": True, "reason": "same-rdc-verified-subset"}
    result["restoreMode"] = "verified-rdc-subset"
    result["realizedReceipts"]["manualEdit"] = sum(record.get("manualEdit") is True
                                                  for record in records + extra_records)
    return extra_records


def restore_active_capture_project(
    project_root: str | Path,
    capture_path: str | Path,
    capture_session_id: str | None = None,
    progress_callback: Callable[[dict[str, Any]], Any] | None = None,
    cancel_event: Any = None,
    deadline_monotonic: float | None = None,
    post_restore_callback: Callable[[Callable[[], dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    """Restore project Shader/CBuffer knowledge into the active local capture.

    The function is synchronous and safe to call from a persistent-host worker
    thread. An exact RDC association validates only its bounded Shader set. On
    a miss, a sealed project-side inventory can narrow the same bounded live
    validation; only a first open or stale cache inventories every unique
    Shader resource. Unchanged RDC/source/candidate/project contents may reuse
    a signed successful validation, with fresh byte and current-session checks.
    No path walks the EID stream or reuses a persisted GPU resource handle.
    """

    if cancel_event is not None and not callable(
        getattr(cancel_event, "is_set", None)
    ):
        raise TypeError("cancel_event must expose is_set()")
    clean_deadline: float | None = None
    if deadline_monotonic is not None:
        if isinstance(deadline_monotonic, bool) or not isinstance(
            deadline_monotonic, (int, float)
        ):
            raise TypeError("deadline_monotonic must be a finite monotonic timestamp")
        clean_deadline = float(deadline_monotonic)
        if not math.isfinite(clean_deadline) or clean_deadline < 0:
            raise ValueError(
                "deadline_monotonic must be a finite monotonic timestamp"
            )

    active_snapshot = None
    active_service = None

    def control_checkpoint(phase: str) -> None:
        _restore_control_checkpoint(cancel_event, clean_deadline, phase)
        if active_snapshot is not None:
            try:
                active_service.assert_active_project_snapshot(active_snapshot)
            except Exception as exc:
                raise RestoreContextChanged("active project changed: " + str(exc)) from exc

    def project_commit():
        if active_snapshot is not None:
            return active_service.commit_active_project_snapshot(active_snapshot)
        return contextlib.nullcontext()

    control_checkpoint("restore start")
    operation_id = uuid.uuid4().hex
    restore_started = time.monotonic()
    from .file_content_cache import statistics as digest_statistics
    initial_digest_statistics = digest_statistics()
    warnings: list[str] = []
    progress = _Progress(operation_id, progress_callback, warnings)
    result: dict[str, Any] = {
        "schema": RESTORE_SCHEMA,
        "schemaVersion": 1,
        "operationId": operation_id,
        "completed": False,
        "restoreMode": "unresolved",
        "desiredHead": None,
        "capture": {
            "path": str(Path(capture_path).expanduser()),
            "sessionId": capture_session_id,
            "captureId": None,
        },
        "project": {
            "root": str(Path(project_root).expanduser()),
            "workspaceId": None,
            "replayProjectId": None,
        },
        "cache": {"hit": False, "reason": "not-read"},
        "inventoryCache": {
            "hit": False,
            "written": False,
            "reason": "not-read",
        },
        "shaders": {
            "attempted": 0,
            "applied": 0,
            "skipped": 0,
            "failed": 0,
            "items": [],
        },
        "materials": {
            "attempted": 0,
            "restored": 0,
            "skipped": 0,
            "failed": 0,
            "sessionIds": [],
            "items": [],
        },
        "index": {
            "read": False,
            "present": False,
            "written": False,
            "warning": None,
        },
        "realizedReceipts": {
            "loadedFromIndex": 0,
            "validatedForRouting": 0,
            "produced": 0,
            "adoptedCurrentSession": 0,
            "mutationsCreated": 0,
            "idempotentApplyResponses": 0,
            "persisted": 0,
        },
        "warnings": warnings,
    }
    applied_workspaces: list[
        tuple[Path, Mapping[str, Any], Mapping[str, Any], str]
    ] = []
    restored_material_sessions: list[tuple[str, str, str]] = []
    restored_alias_transactions: list[dict[str, str]] = []
    realized_receipts: list[dict[str, Any]] = []
    host: Any = None
    pin_context: Any = None
    commit_checkpoint_passed = False
    inventory_cache_binding: tuple[str, str] | None = None
    inventory_cache_write_source: Mapping[str, Any] | None = None
    verified_cache_miss_reason: str | None = None

    def release_active_project_pin() -> None:
        nonlocal pin_context
        current = pin_context
        pin_context = None
        if current is not None:
            current.close()

    def enable_editing_after_restore(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        items = result["shaders"]["items"]
        if post_restore_callback is None:
            return _enable_restored_shader_editing(
                host, active, project, Path(capture_path).resolve(), live_session,
                records, items, cancel_event=cancel_event,
                deadline_monotonic=clean_deadline, progress=progress)
        # Retain the exact IPC owner, but no project pin, workspace lease or
        # restore guard. The registry invokes this only after publishing the
        # committed restore result. Editor startup has its own bounded budget.
        direct_binding = _CALL_BINDING.get()
        saved_records, saved_items = copy.deepcopy(list(records)), copy.deepcopy(items)
        def start_editors() -> dict[str, Any]:
            from .ipc_client import bind_direct_tool_call
            with bind_direct_tool_call(direct_binding[0] if direct_binding else host.ipc.instance_id, live_session):
                return _enable_restored_shader_editing(
                    host, active, project, Path(capture_path).resolve(), live_session,
                    saved_records, saved_items, cancel_event=cancel_event,
                    deadline_monotonic=time.monotonic() + 900, progress=None)
        pending_items = [{"workspaceId": item["workspaceId"],
                          "workspacePath": item["workspacePath"], "status": "pending",
                          "captureBinding": {"instanceId": (_CALL_BINDING.get() or ("", ""))[0],
                                             "captureSessionId": live_session,
                                             "capturePath": str(Path(capture_path).resolve())}}
                         for item in saved_items if item.get("status") == "applied"]
        if not pending_items:
            return {"attempted": 0, "enabled": 0, "deferred": 0, "failed": 0, "items": []}
        post_restore_callback(start_editors)
        return {"status": "pending", "pending": len(pending_items),
                "attempted": 0, "enabled": 0, "deferred": 0, "failed": 0, "items": pending_items}

    def rollback_restored_state() -> str:
        failures: list[str] = []
        material_error = _rollback_restored_material_sessions(
            host, restored_material_sessions
        )
        if material_error:
            failures.append("material rollback failed: " + material_error)
        shader_error = _rollback_applied_workspaces(host, applied_workspaces)
        if shader_error:
            failures.append("Shader rollback failed: " + shader_error)
        alias_error = _rollback_restored_alias_transactions(
            host, restored_alias_transactions
        )
        if alias_error:
            failures.append("alias rollback failed: " + alias_error)
        return "; ".join(failures)[:4096]

    with _RESTORE_GUARD:
        previous_restore_state = automatic_project_sync_suppressed()
        previous_restore_policy = getattr(_RESTORE_THREAD_STATE, "validation_policy_sha256", None)
        previous_compatible_policies = getattr(_RESTORE_THREAD_STATE, "compatible_policy_sha256s", ())
        previous_context_checkpoint = getattr(_RESTORE_THREAD_STATE, "control_checkpoint", None)
        previous_context_recovery = getattr(_RESTORE_THREAD_STATE, "context_recovery", None)
        previous_project_identities = getattr(_RESTORE_THREAD_STATE, "project_identities", None)
        previous_pair_rdc_observations = getattr(_RESTORE_THREAD_STATE, "pair_rdc_observations", None)
        _RESTORE_THREAD_STATE.active = True
        _RESTORE_THREAD_STATE.validation_policy_sha256 = None
        _RESTORE_THREAD_STATE.compatible_policy_sha256s = ()
        _RESTORE_THREAD_STATE.control_checkpoint = control_checkpoint
        _RESTORE_THREAD_STATE.context_recovery = []
        _RESTORE_THREAD_STATE.project_identities = {}
        _RESTORE_THREAD_STATE.pair_rdc_observations = {}
        transport_context = contextlib.ExitStack()
        try:
            host = _host_module()
            bind_transport = getattr(getattr(host, "ipc", None), "bind_capture_session", None)
            if callable(bind_transport):
                transport_context.enter_context(bind_transport(capture_session_id or ""))
            transport_context.enter_context(resume_after_foreground_yield(control_checkpoint))
            progress.emit("validate", "Validating active project and capture")
            control_checkpoint("before active project and capture validation")
            active, live_context, project, replay_path, live_session = (
                _validate_capture_and_project(
                    host, project_root, capture_path, capture_session_id
                )
            )
            _RESTORE_THREAD_STATE.validation_policy_sha256 = live_context.get("restoreValidationPolicySha256")
            policy_hints = live_context.get("restoreValidationCompatiblePolicySha256s", [])
            if isinstance(policy_hints, list) and len(policy_hints) <= 8:
                _RESTORE_THREAD_STATE.compatible_policy_sha256s = tuple(value for value in policy_hints
                    if isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value))
            if callable(bind_transport) and not capture_session_id:
                transport_context.enter_context(bind_transport(live_session))
            control_checkpoint("after active project and capture validation")
            batch_identity = getattr(host, "_project_shader_restore_batch", None)
            if callable(batch_identity):
                transport_context.enter_context(batch_identity(Path(capture_path).resolve(), live_session))
            active_service = host._active_project_service()
            snapshot_factory = getattr(active_service, "snapshot_active_project", None)
            pin = snapshot_factory if callable(snapshot_factory) else getattr(active_service, "pin_active_project", None)
            if not callable(pin):
                raise RuntimeError(
                    "active project service has no transaction pin route"
                )
            pin_context = contextlib.ExitStack()
            control_checkpoint("before active project transaction pin")
            project_token = pin_context.enter_context(pin(active["workspaceId"], project))
            if callable(snapshot_factory):
                active_snapshot = project_token
            control_checkpoint("after active project transaction pin")
            result["capture"].update({"path": str(Path(capture_path).resolve()), "sessionId": live_session})
            result["project"].update(
                {
                    "root": str(project),
                    "workspaceId": active["workspaceId"],
                    "replayProjectId": active["replayProject"]["projectId"],
                }
            )
            # A preceding editor save may have finished while this RDC was
            # opening. Commit its frozen successful source before consulting
            # any cached restore plan or selecting the project head.
            from .shader_saved_publication import drain as drain_saved_shader_publications
            drain_saved_shader_publications(project, replay_path, control_checkpoint)
            replay = ReplayProjectService(replay_path, cache_validated_reads=True)
            live_capture = live_context.get("capture")
            if not isinstance(live_capture, Mapping):
                raise RuntimeError("RenderDoc current context omitted capture API")
            if _project_has_no_restorable_knowledge(replay):
                # There is nothing that could be applied, so neither an RDC
                # association lookup nor a 500+ resource inventory can improve
                # the result. Original-resource indexing is created lazily once
                # this project actually owns HLSL/CBuffer/alias knowledge.
                result["restoreMode"] = "no-project-knowledge"
                result["cache"] = {
                    "hit": False,
                    "reason": "project-has-no-restorable-knowledge",
                }
                result["inventoryCache"] = {
                    "hit": False,
                    "written": False,
                    "reason": "not-required-for-empty-project",
                }
                result["capture"]["captureId"] = (
                    str(live_capture.get("captureId", "") or "") or None
                )
                control_checkpoint("before empty project restore completion")
                release_active_project_pin()
                result["completed"] = True
                progress.emit(
                    "complete",
                    "Bread project restore completed",
                    detail={
                        "shaderApplied": 0,
                        "materialRestored": 0,
                        "cacheHit": False,
                        "projectKnowledgeEmpty": True,
                    },
                )
                return result
            try:
                verified = _load_verified_rdc_restore(
                    host, replay, active, project, replay_path, Path(capture_path).resolve(),
                    control_checkpoint, live_context.get("restoreValidationPolicySha256"),
                    _RESTORE_THREAD_STATE.compatible_policy_sha256s)
                if verified is not None:
                    snapshot, workspace_statuses = verified
                    shader_records = _apply_verified_rdc_restore(
                        host, replay, active, project, Path(capture_path).resolve(), live_session,
                        snapshot, workspace_statuses, progress, result, applied_workspaces,
                        restored_alias_transactions, control_checkpoint,
                        restored_material_sessions=restored_material_sessions)
                    control_checkpoint("before verified RDC restore completion")
                    release_active_project_pin()
                    result["completed"] = True
                    result["editing"] = enable_editing_after_restore(shader_records)
                    progress.emit("complete", "Bread project restore completed", detail={
                        "shaderApplied": len(shader_records), "materialRestored": 0, "cacheHit": True})
                    return result
            except (RestoreContextChanged, RestoreContextUnavailable, RestoreRollbackFailed, RestoreControlStop):
                raise
            except _VerifiedRdcRestorePolicyMiss as exc:
                verified_cache_miss_reason = str(exc)[:4096]
                result["verifiedRdcCache"] = {
                    "hit": False, "written": False, "reason": str(exc)}
            except Exception as exc:
                verified_cache_miss_reason = ("invalid-or-stale: %s: %s" % (type(exc).__name__, exc))[:4096]
                cleanup_error = rollback_restored_state()
                if cleanup_error:
                    raise RestoreRollbackFailed("verified RDC restore cleanup failed: " + cleanup_error) from exc
                control_checkpoint("after verified RDC restore cache miss")
                _require_restore_context(host, active, project, Path(capture_path).resolve(), live_session)
                result["verifiedRdcCache"] = {
                    "hit": False, "written": False,
                    "reason": ("invalid-or-stale: %s: %s" % (type(exc).__name__, exc))[:4096]}
            inventory: dict[str, Any] | None = None
            index_transport: dict[str, Any] | None = None
            index_document: dict[str, Any] | None = None
            selected: dict[str, Any] | None = None
            binding_state: dict[str, Any] | None = None
            progress.emit("index-read", "Reading RDC project association cache")
            try:
                control_checkpoint("before RDC project index read")
                index_transport, index_document = _read_rdc_index(
                    host, Path(capture_path).resolve(), live_session
                )
                control_checkpoint("after RDC project index read")
                result["index"]["read"] = True
                result["index"]["present"] = index_document is not None
                if index_document is None:
                    result["cache"]["reason"] = "index-absent"
                else:
                    selected = select_project_binding(
                        index_document,
                        active["replayProject"]["projectId"],
                        active["workspaceId"],
                    )
                    if selected is None:
                        result["cache"]["reason"] = "project-binding-absent"
                    elif not selected.get("shaders"):
                        result["cache"]["reason"] = "project-binding-has-no-shaders"
                        selected = None
                    else:
                        control_checkpoint("before cached project binding snapshot")
                        binding_state = _project_binding_state(replay, selected)
                        desired_head, indexed_receipts = (
                            _validate_cached_binding_accelerator(
                                selected, binding_state
                            )
                        )
                        control_checkpoint("after cached project binding snapshot")
                        result["desiredHead"] = copy.deepcopy(desired_head)
                        result["realizedReceipts"]["loadedFromIndex"] = len(
                            selected.get("realizedApplySnapshot", {}).get(
                                "receipts", []
                            )
                        )
                        result["realizedReceipts"]["validatedForRouting"] = len(
                            indexed_receipts
                        )
                        result["restoreMode"] = "exact-rdc-head"
                        result["cache"] = {
                            "hit": True,
                            "reason": "exact-desired-project-head",
                        }
            except RestoreControlStop:
                raise
            except Exception as exc:
                result["cache"]["reason"] = "invalid-or-stale-index: %s: %s" % (
                    type(exc).__name__,
                    exc,
                )
                selected = None
                binding_state = None

            if selected is not None and binding_state is not None:
                progress.emit(
                    "cache-hit",
                    "RDC cache matched; indexing bounded live Shader resources",
                    detail={"shaderCount": len(selected["shaders"])},
                )
                try:
                    shader_records, material_records = _restore_from_cache(
                        host,
                        replay,
                        active,
                        project,
                        Path(capture_path).resolve(),
                        live_session,
                        selected,
                        binding_state,
                        None,
                        progress,
                        result["shaders"],
                        result["materials"],
                        applied_workspaces,
                        restored_material_sessions,
                        restored_alias_transactions,
                        control_checkpoint,
                        realized_receipts,
                        result["realizedReceipts"],
                    )
                except (
                    RestoreContextChanged,
                    RestoreContextUnavailable,
                    RestoreRollbackFailed,
                    RestoreControlStop,
                ):
                    raise
                except Exception as exc:
                    # Cache validation failed before every desired association
                    # could be freshly realized. The cache never has apply
                    # authority; rebuild routing from fresh target evidence.
                    cleanup_error = rollback_restored_state()
                    if cleanup_error:
                        raise RestoreRollbackFailed(
                            "cached restore validation failed and cleanup failed: "
                            + cleanup_error
                        ) from exc
                    result["cache"] = {
                        "hit": False,
                        "reason": "cache-live-validation-failed: %s: %s"
                        % (type(exc).__name__, exc),
                    }
                    shader_records = []
                    material_records = []
                    realized_receipts.clear()
                    for field in (
                        "produced",
                        "reusedValidation",
                        "adoptedCurrentSession",
                        "mutationsCreated",
                        "idempotentApplyResponses",
                    ):
                        result["realizedReceipts"][field] = 0
                    selected = None
                    binding_state = None
            else:
                shader_records = []
                material_records = []

            if selected is None or binding_state is None:
                fallback_reason = str(result["cache"].get("reason", "cache-miss"))
                capture = Path(capture_path).resolve()
                prepared: dict[str, Any] | None = None
                cache_document: dict[str, Any] | None = None
                try:
                    inventory_cache_binding = _shader_inventory_cache_binding(
                        replay, active
                    )
                except Exception:
                    # The cache is an optional derived accelerator. A legacy
                    # Host/project status without scope metadata keeps the
                    # established full-inventory path without adding noise.
                    inventory_cache_binding = None
                    result["inventoryCache"]["reason"] = "binding-unavailable"

                if inventory_cache_binding is not None:
                    cache_project_id, cache_scope_sha256 = inventory_cache_binding
                    try:
                        control_checkpoint("before Shader inventory cache read")
                        cache_document = load_shader_inventory_cache(
                            replay_path,
                            cache_project_id,
                            cache_scope_sha256,
                            capture,
                        )
                        control_checkpoint("after Shader inventory cache read")
                    except RestoreControlStop:
                        raise
                    except RdcShaderInventoryCacheError as exc:
                        result["inventoryCache"]["reason"] = (
                            "invalid: %s" % exc
                        )[:4096]
                        warnings.append(
                            ("Shader inventory cache was ignored: %s" % exc)[:4096]
                        )
                    except Exception as exc:
                        result["inventoryCache"]["reason"] = (
                            "read-failed: %s: %s"
                            % (type(exc).__name__, exc)
                        )[:4096]
                    else:
                        if cache_document is None:
                            result["inventoryCache"]["reason"] = (
                                "absent-or-stale"
                            )

                if cache_document is not None:
                    try:
                        provisional_inventory = _runtime_shader_inventory_from_cache(
                            cache_document,
                            capture,
                            live_session,
                            str(live_capture.get("api", "")),
                        )
                        provisional = _prepare_inventory_response(
                            replay,
                            active,
                            provisional_inventory,
                            progress,
                            control_checkpoint,
                        )
                        verified_inventory = _verify_cached_inventory_candidates(
                            host,
                            capture,
                            live_session,
                            provisional,
                            provisional_inventory,
                            cache_document,
                            control_checkpoint,
                        )
                        _require_restore_context(
                            host, active, project, capture, live_session
                        )
                        # Re-plan from only the freshly verified subset. This
                        # prevents the full cached aggregate, unmatched RIDs or
                        # stale fields from entering the final binding state.
                        prepared = _prepare_inventory_response(
                            replay,
                            active,
                            verified_inventory,
                            progress,
                            control_checkpoint,
                        )
                        inventory = verified_inventory
                        inventory_cache_write_source = cache_document
                        result["inventoryCache"] = {
                            "hit": True,
                            "written": False,
                            "reason": "fresh-target-validation",
                        }
                        result["restoreMode"] = "inventory-sidecar"
                    except (
                        RestoreContextChanged,
                        RestoreContextUnavailable,
                        RestoreRollbackFailed,
                        RestoreControlStop,
                    ):
                        raise
                    except CatalogPreparationRequired as exc:
                        # A valid sidecar has already reduced this restore to
                        # bounded fresh-target validation.  If the bridge is
                        # temporarily unavailable or the bounded job times
                        # out, starting a 500+ Shader inventory is both more
                        # expensive and less likely to succeed.  Preserve the
                        # accelerator and fail this background attempt without
                        # monopolising RenderDoc's replay queue.  A genuine
                        # identity/interface mismatch still falls through and
                        # rebuilds the stale sidecar below.
                        if exc.reason_id in {
                            "shader-resource-index-timeout",
                            "shader-resource-index-unavailable",
                            "shader-resource-index-capture-changed",
                        }:
                            raise
                        _require_restore_context(
                            host, active, project, capture, live_session
                        )
                        result["inventoryCache"]["reason"] = (
                            "live-validation-failed: %s: %s"
                            % (type(exc).__name__, exc)
                        )[:4096]
                        prepared = None
                        inventory = None
                    except Exception as exc:
                        # A target/session change must not be disguised as a
                        # normal cache miss. Otherwise the sidecar is simply
                        # discarded and the complete live inventory rebuilds it.
                        _require_restore_context(
                            host, active, project, capture, live_session
                        )
                        result["inventoryCache"]["reason"] = (
                            "live-validation-failed: %s: %s"
                            % (type(exc).__name__, exc)
                        )[:4096]
                        prepared = None
                        inventory = None

                if prepared is None:
                    # A failed full-cache validation cannot be re-published
                    # after a narrower lookup has succeeded.
                    inventory_cache_write_source = None
                    result["restoreMode"] = "project-target-inventory"
                    inventory = _inventory_project_shader_targets(
                        host,
                        replay,
                        capture,
                        live_session,
                        str(live_capture.get("api", "")),
                        progress,
                        control_checkpoint,
                        cache_diagnostic=result["inventoryCache"],
                    )
                    _require_restore_context(
                        host, active, project, capture, live_session
                    )
                    result["inventoryScope"] = "target-raw-sha256"
                    result["fullFrame"] = False
                    result["targetLookup"] = {key: copy.deepcopy(inventory[key]) for key in (
                        "targetRawSha256s", "observedTargetRawSha256s", "unobservedTargetRawSha256s",
                        "targetInventoryComplete", "rawIdentitySearchComplete")}
                    code_recovery: dict[str, Any] = {}
                    inventory = _recover_equivalent_shader_sources(
                        host, replay, active, project, Path(capture_path).resolve(), live_session,
                        str(live_capture.get("api", "")), inventory, progress,
                        control_checkpoint, code_recovery)
                    if code_recovery.get("prepared"):
                        if inventory.get("inventoryScope", "all-shaders") == "all-shaders":
                            result["restoreMode"] = "full-inventory"
                            for key in ("inventoryScope", "fullFrame", "targetLookup"):
                                result.pop(key, None)
                        progress.emit("resource-match", "Matching exact project Shader artifacts",
                                      detail={"equivalentSourcesPrepared": code_recovery["prepared"],
                                              "codeCandidateCount": code_recovery["candidateCount"]})
                    if not inventory["shaders"]:
                        result["capture"]["captureId"] = inventory["capture"]["captureId"]
                        result["cache"] = {"hit": False, "reason": "project-targets-not-present"}
                        result["inventoryCache"] = {"hit": False, "written": False, "reason": "target-lookup-is-not-a-full-inventory"}
                        control_checkpoint("before completed empty project target lookup")
                        release_active_project_pin()
                        result["completed"] = True
                        progress.emit("complete", "Bread project restore completed", detail={"matchedShaderCount": 0, "fullFrame": False})
                        return result
                    bridge_result = inventory.get("bridgeResult")
                    if (isinstance(bridge_result, Mapping)
                            and inventory.get("inventoryScope", "all-shaders") == "all-shaders"):
                        inventory_cache_write_source = bridge_result
                    if (
                        inventory_cache_binding is not None
                        and inventory_cache_write_source is not None
                    ):
                        cache_project_id, cache_scope_sha256 = (
                            inventory_cache_binding
                        )
                        try:
                            control_checkpoint(
                                "before Shader inventory cache publication"
                            )
                            write_shader_inventory_cache(
                                replay_path,
                                cache_project_id,
                                cache_scope_sha256,
                                capture,
                                inventory_cache_write_source,
                            )
                            control_checkpoint(
                                "after Shader inventory cache publication"
                            )
                            previous_reason = str(
                                result["inventoryCache"].get("reason", "cache-miss")
                            )
                            result["inventoryCache"] = {
                                "hit": False,
                                "written": True,
                                "reason": (
                                    "stored-after-full-inventory:"
                                    + previous_reason
                                )[:4096],
                            }
                        except RestoreControlStop:
                            raise
                        except Exception as exc:
                            result["inventoryCache"]["reason"] = (
                                "write-failed: %s: %s"
                                % (type(exc).__name__, exc)
                            )[:4096]
                            warnings.append(
                                (
                                    "Shader inventory cache could not be written: "
                                    "%s: %s" % (type(exc).__name__, exc)
                                )[:4096]
                            )
                    prepared = _prepare_inventory_response(
                        replay,
                        active,
                        inventory,
                        progress,
                        control_checkpoint,
                    )

                if inventory is None:
                    raise RuntimeError("Shader inventory preparation produced no data")
                shader_records, material_records, binding_state = (
                    _restore_from_reuse_plan(
                        host,
                        replay,
                        active,
                        project,
                        Path(capture_path).resolve(),
                        live_session,
                        prepared,
                        inventory,
                        progress,
                        result["shaders"],
                        result["materials"],
                        applied_workspaces,
                        restored_material_sessions,
                        restored_alias_transactions,
                        control_checkpoint,
                        realized_receipts,
                        result["realizedReceipts"],
                    )
                )
                result["cache"] = {
                    "hit": False,
                    "reason": ("project-reuse-plan:" + fallback_reason)[:4096],
                }

            result["capture"]["captureId"] = binding_state["captureId"]
            _require_restore_context(
                host,
                active,
                project,
                Path(capture_path).resolve(),
                live_session,
            )
            control_checkpoint("after final active restore context validation")
            incomplete_restore = bool(
                result["shaders"]["failed"]
                or any(item.get("status") == "skipped" and item.get("reason") not in {
                    "source-only-project-draft", "legacy-saved-organization-draft",
                    "legacy-organization-publication-needs-review",
                } for item in result["shaders"]["items"])
                or result["shaders"]["applied"]
                != result["shaders"]["attempted"]
                or result["materials"]["failed"]
                or result["materials"]["restored"]
                != result["materials"]["attempted"]
            )
            verified_snapshot = None
            result["realizedReceipts"]["manualEdit"] = sum(
                record.get("manualEdit") is True for record in shader_records)
            if not incomplete_restore and not result["materials"]["attempted"]:
                try:
                    progress.emit("index-write", "Publishing verified RDC restore snapshot")
                    control_checkpoint("before verified RDC restore snapshot")
                    verified_snapshot = _build_verified_rdc_restore(
                        host, replay, active, project, Path(capture_path).resolve(),
                        binding_state, shader_records, applied_workspaces, control_checkpoint,
                        inventory=inventory)
                    if verified_snapshot is not None:
                        # Keep RDC bytes unchanged after the Bridge signed them.
                        # Writing the legacy embedded index here would invalidate
                        # every receipt immediately, on every successful reopen.
                        with project_commit():
                            _write_verified_rdc_restore(
                                replay_path, Path(capture_path).resolve(), verified_snapshot)
                        result["verifiedRdcCache"] = {
                            "hit": False, "written": True,
                            "reason": ("validated-content-subset-snapshot" if verified_snapshot.get("deferredShaders")
                                       else "validated-content-snapshot")}
                        result["realizedReceipts"]["persisted"] = len(verified_snapshot["shaders"])
                        progress.emit("index-write", "Verified RDC restore snapshot published")
                except (RestoreContextChanged, RestoreContextUnavailable, RestoreControlStop):
                    raise
                except Exception as exc:
                    verified_snapshot = None
                    result["verifiedRdcCache"] = {
                        "hit": False, "written": False,
                        "reason": ("write-failed: %s: %s" % (type(exc).__name__, exc))[:4096]}
            if incomplete_restore:
                alias_error = _rollback_restored_alias_transactions(
                    host, restored_alias_transactions
                )
                if alias_error:
                    raise RestoreRollbackFailed(
                        "incomplete restore could not roll back its strict alias overlay: "
                        + alias_error
                    )
                warning = (
                    "RDC project association cache was not updated because one or "
                    "more reusable resources were not restored; the previous binding "
                    "was preserved for retry"
                )
                result["index"]["warning"] = warning
                warnings.append(warning)
            elif verified_snapshot is not None:
                if not result["cache"]["hit"]:
                    result["cache"]["reason"] = "verified-rdc-content-snapshot-written"
            elif any(record.get("manualEdit") is True for record in shader_records):
                # The portable project head already owns exact manual source
                # and bytes. Do not relabel them as ABI-proven RDC receipts.
                result["cache"]["reason"] = "manual-project-source-and-binary"
                result["index"]["written"] = False
                result["realizedReceipts"]["manualEdit"] = sum(
                    record.get("manualEdit") is True for record in shader_records)
            elif any(record.get("sourcePreparationOnly") is True for record in shader_records):
                # A partial-reflection source can pass the full current live
                # ABI gate without acquiring complete-index routing authority.
                # Keep the prior whole binding intact for mixed restores too.
                result["cache"]["reason"] = "project-source-preparation:fresh-live-abi-no-executable-index"
                result["index"]["warning"] = (
                    "Project Shader candidates were applied with current live ABI validation; "
                    "the previous RDC executable index was preserved because reflected layout knowledge is incomplete"
                )
                warnings.append(result["index"]["warning"])
                control_checkpoint("before completing source-preparation restore")
                _prepared_binding_state(replay, active, prepared)
            elif shader_records:
                binding = _binding_document(
                    active,
                    binding_state,
                    shader_records,
                    material_records,
                    realized_receipts,
                    (
                        selected.get("desiredProjectHead")
                        if isinstance(selected, Mapping)
                        and isinstance(
                            selected.get("desiredProjectHead"), Mapping
                        )
                        else None
                    ),
                )
                result["desiredHead"] = copy.deepcopy(
                    binding["desiredProjectHead"]
                )
                omitted_materials = len(material_records) - len(
                    binding["materials"]
                )
                if omitted_materials:
                    warnings.append(
                        "%d restored material association(s) could not be indexed because "
                        "the RDC project index requires an associated managed Shader workspace"
                        % omitted_materials
                    )
                progress.emit("index-write", "Writing updated RDC project association cache")
                if index_transport is None:
                    warning = "RDC project index was not read successfully; refusing to overwrite it"
                    result["index"]["warning"] = warning
                    warnings.append(warning)
                else:
                    control_checkpoint("before final referenced project head validation")
                    binding_state = _revalidate_binding_state_before_commit(
                        replay, binding_state, binding
                    )
                    control_checkpoint("after final referenced project head validation")
                    control_checkpoint("before RDC project index write")
                    # The RDC write is the final transaction commit operation.
                    # Cancellation racing after this checkpoint must let the
                    # completed restore win; rolling live state back after a
                    # successful write would leave stale routing receipts.
                    commit_checkpoint_passed = True
                    written, warning, unused_index = _write_index_best_effort(
                        host,
                        index_document,
                        binding,
                        Path(capture_path).resolve(),
                        live_session,
                    )
                    result["index"]["written"] = written
                    if written:
                        result["index"]["present"] = True
                        result["realizedReceipts"]["persisted"] = len(
                            realized_receipts
                        )
                    if warning:
                        result["index"]["warning"] = warning
                        warnings.append("RDC index write warning: " + warning)
            elif result["materials"]["restored"]:
                warning = (
                    "restored material sessions were not indexed because the RDC project index "
                    "requires an associated managed Shader workspace"
                )
                result["index"]["warning"] = warning
                warnings.append(warning)

            if (
                result["index"]["written"]
                and inventory_cache_binding is not None
                and inventory_cache_write_source is not None
            ):
                # A real RDC section write changes size/mtime and would make a
                # sidecar sealed before the write immediately stale. Refresh
                # only the cheap file observation; the allow-listed inventory
                # payload is unchanged and remains routing-only.
                cache_project_id, cache_scope_sha256 = inventory_cache_binding
                try:
                    write_shader_inventory_cache(
                        replay_path,
                        cache_project_id,
                        cache_scope_sha256,
                        Path(capture_path).resolve(),
                        inventory_cache_write_source,
                    )
                    result["inventoryCache"]["written"] = True
                except Exception as exc:
                    result["inventoryCache"]["reason"] = (
                        "refresh-failed: %s: %s" % (type(exc).__name__, exc)
                    )[:4096]
                    warnings.append(
                        (
                            "Shader inventory cache observation could not be "
                            "refreshed after the RDC index update: %s: %s"
                            % (type(exc).__name__, exc)
                        )[:4096]
                    )

            if not commit_checkpoint_passed:
                control_checkpoint("before restored state commit")
            release_active_project_pin()
            result["completed"] = True
            result["editing"] = enable_editing_after_restore(shader_records)
            if verified_cache_miss_reason is not None and not result["cache"]["hit"]:
                # The new write receipt describes the output of this run. Keep
                # the input miss separately so a successful fallback does not
                # conceal why reopening needed the full restoration path.
                result["cache"]["reason"] = (
                    "verified-rdc-cache-miss: " + verified_cache_miss_reason)[:4096]
            progress.emit(
                "complete",
                "Bread project restore completed",
                detail={
                    "shaderApplied": result["shaders"]["applied"],
                    "materialRestored": result["materials"]["restored"],
                    "cacheHit": result["cache"]["hit"],
                },
            )
        except RestoreControlStop as exc:
            # Keep the project pin held while reversing every live mutation.
            # Cancellation/deadline is a terminal Host failure rather than a
            # legacy completed result with ``completed: false``.
            rollback_error = rollback_restored_state()
            pin_error = ""
            try:
                release_active_project_pin()
            except Exception as release_exc:
                pin_error = "%s: %s" % (type(release_exc).__name__, release_exc)
            diagnostic = "%s: %s" % (type(exc).__name__, exc)
            if rollback_error:
                diagnostic += "; restored state cleanup failed: " + rollback_error
            if pin_error:
                diagnostic += "; active project pin release failed: " + pin_error
            progress.emit(
                "failed",
                "Bread project restore was cancelled",
                detail={"error": diagnostic[:4096]},
            )
            if rollback_error or pin_error:
                raise RestoreRollbackFailed(diagnostic[:4096]) from exc
            raise
        except CatalogPreparationRequired as exc:
            # This is an expected fail-closed product state, not a restore
            # fault. Keep the transport successful and expose stable protocol
            # values instead of leaking a Python exception class to qrenderdoc.
            rollback_error = rollback_restored_state()
            pin_error = ""
            try:
                release_active_project_pin()
            except Exception as release_exc:
                pin_error = "%s: %s" % (type(release_exc).__name__, release_exc)
            if rollback_error or pin_error:
                result["error"] = (
                    "%s: %s%s%s"
                    % (
                        type(exc).__name__,
                        exc,
                        "; active project pin release failed: " + pin_error
                        if pin_error else "",
                        "; restored state cleanup failed: " + rollback_error
                        if rollback_error else "",
                    )
                )[:4096]
                result["errorType"] = "RestoreRollbackFailed"
                progress.emit(
                    "failed",
                    "Bread project restore failed and restored state cleanup also failed",
                    detail={"error": result["error"]},
                )
            else:
                result["outcome"] = "action-required"
                result["reasonId"] = exc.reason_id
                result["reasonDetail"] = str(exc)[:4096]
        except Exception as exc:
            rollback_error = rollback_restored_state()
            pin_error = ""
            try:
                release_active_project_pin()
            except Exception as release_exc:
                pin_error = "%s: %s" % (type(release_exc).__name__, release_exc)
            result["error"] = "%s: %s" % (type(exc).__name__, exc)
            result["errorType"] = type(exc).__name__
            if rollback_error:
                result["error"] = (
                    result["error"]
                    + "; restored state cleanup failed: "
                    + rollback_error
                )[:4096]
                result["errorType"] = "RestoreRollbackFailed"
            if pin_error:
                result["error"] = (
                    result["error"]
                    + "; active project pin release failed: "
                    + pin_error
                )[:4096]
                if result["errorType"] != "RestoreRollbackFailed":
                    result["errorType"] = "RestoreContextChanged"
            progress.emit(
                "failed",
                "Bread project restore failed",
                detail={"error": result["error"]},
            )
        finally:
            try:
                if pin_context is not None:
                    release_active_project_pin()
            finally:
                transport_context.close()
                if _RESTORE_THREAD_STATE.context_recovery:
                    result["contextRecovery"] = copy.deepcopy(_RESTORE_THREAD_STATE.context_recovery)
                _RESTORE_THREAD_STATE.active = previous_restore_state
                _RESTORE_THREAD_STATE.validation_policy_sha256 = previous_restore_policy
                _RESTORE_THREAD_STATE.compatible_policy_sha256s = previous_compatible_policies
                _RESTORE_THREAD_STATE.control_checkpoint = previous_context_checkpoint
                _RESTORE_THREAD_STATE.context_recovery = previous_context_recovery
                _RESTORE_THREAD_STATE.project_identities = previous_project_identities
                _RESTORE_THREAD_STATE.pair_rdc_observations = previous_pair_rdc_observations
                result["performance"] = {"elapsedMs": round((time.monotonic()-restore_started)*1000,3),
                    "phasesMs": progress.timings(), "digestCache": {key:value-initial_digest_statistics.get(key,0)
                        for key,value in digest_statistics().items()}, "filePreparationWorkers": 2, "replayWorkers": 1}
    return result


def _current_compiled_pair_matches(
    status: Mapping[str, Any], revision: int, candidate_raw_sha256: str,
) -> bool:
    latest, source = status.get("latestCompiled") or {}, status.get("source") or {}
    return bool(
        status.get("revision") == revision and latest.get("revision") == revision
        and status.get("sourceDependenciesCurrent", True) is True
        and source.get("present") is True
        and latest.get("sourceSha256") == source.get("currentSha256")
        and latest.get("rawSha256") == candidate_raw_sha256
        and latest.get("abiGate", {}).get("status") == "compatible"
        and latest.get("replayAbiValidated") is True
        and latest.get("abiCompatible") is True
    )


def _publication_shader_identity(
    shader: Mapping[str, Any], status: Mapping[str, Any],
    live_replacement: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Complete publication metadata only after the live native pair check.

    Historical catalogs attach API to the capture, rather than each Shader.
    The exact live replacement proves the original physical ABI; missing
    routing fields may be filled from that same canonical workspace, while
    contradictory fields still fail. This does not grant replay authority.
    """
    capture, baseline = status.get("capture") or {}, status.get("baseline") or {}
    api = _reuse_api_family(capture.get("api"))
    encoding = _asset_encoding(baseline.get("kind"))
    abi_hash = _hex(baseline.get("abiSha256"), 64, "publication physical ABI")
    if (shader.get("resourceId") != (status.get("shader") or {}).get("resourceId")
            or shader.get("rawSha256") != baseline.get("rawSha256")
            or shader.get("stage") != status.get("stage")
            or shader.get("entryPoint", "") != status.get("entryPoint", "")):
        raise RuntimeError("project catalog differs from the live original Shader identity")
    catalog_identity = dict(shader)
    if (("interfaceComplete" in shader or "interfaceHash" in shader) and "stableKeyComplete" not in shader
            and "stableKeySha256" not in shader and "abiHash" not in shader):
        # V1 catalog rows already carry encoding/interfaceComplete but keep API
        # on the capture. Their interfaceHash describes the catalog interface,
        # not the physical ABI hash. Complete this legacy publication boundary
        # before the strict v2 parser sees its completeness flag. Never repair
        # an explicitly supplied v2 key or rewrite immutable catalog records.
        catalog_identity["api"] = shader.get("api") or api
        catalog_identity["encoding"] = shader.get("encoding") or encoding
        catalog_identity["abiHash"] = abi_hash
    original = _normal_shader_identity(catalog_identity)
    if (not api or not encoding
            or original.get("api") and _reuse_api_family(original["api"]) != api
            or original.get("encoding") and original["encoding"] != encoding
            or original.get("abiHash") and original["abiHash"] not in _workspace_physical_abi_hashes(status)):
        raise RuntimeError("project catalog differs from the live original Shader identity")
    # Preserve a complete desired catalog identity. The live receipt carries
    # its own spelling; the bounded projection below proves their equivalence.
    abi_hash = original.get("abiHash") or abi_hash
    identity = {**original, "api": api, "encoding": encoding, "abiHash": abi_hash}
    if live_replacement is not None:
        # A resource-wide replacement may have been checked at another use
        # than the historical catalog's representative EID. Preserve the
        # actual checked live anchor as capture-local delivery evidence.
        identity["anchorEventId"] = _integer(
            live_replacement.get("anchorEventId"), "publication live anchor EID", 1)
    identity["stableKeyComplete"] = bool(identity.get("layoutHash"))
    identity.pop("stableKeySha256", None)
    return _normal_shader_identity(identity)


def _live_replacement(
    host: Any,
    status: Mapping[str, Any],
    capture_session_id: str,
    replacement_id: str,
    revision: int,
    candidate_raw_sha256: str,
    *, include_validation_receipts: bool = False,
) -> dict[str, Any]:
    capture_id = _hex(status.get("capture", {}).get("captureId"), 64, "workspace captureId")
    baseline = status.get("baseline")
    latest = status.get("latestCompiled")
    capture = status.get("capture")
    shader = status.get("shader")
    if not all(
        isinstance(value, Mapping)
        for value in (baseline, latest, capture, shader)
    ):
        raise RuntimeError("workspace omitted live replacement identity")
    original_raw_sha256 = _hex(
        baseline.get("rawSha256"), 64, "workspace baseline.rawSha256"
    )
    physical_abi_sha256 = _hex(
        baseline.get("abiSha256"), 64, "workspace baseline.abiSha256"
    )
    expected_anchor = _shader_anchor_event_id(
        {
            "anchorEventId": status.get(
                "anchorEventId",
                status.get("eventId", status.get("binding", {}).get("eventId")),
            )
        },
        required=False,
    )
    query = {
            "expected_capture_id": capture_id,
            "expected_capture_session_id": capture_session_id,
        }
    if include_validation_receipts:
        query["include_validation_receipts"] = True
    response = host.ipc.call("get_shared_shader_replacements", query, timeout=30.0)
    replacements = response.get("replacements") if isinstance(response, Mapping) else None
    if (
        not isinstance(response, dict)
        or response.get("schema") != "renderdoc-ultra-shared-shader-replacement-list/v1"
        or response.get("schemaVersion") != 1
        or response.get("captureId") != capture_id
        or response.get("captureSessionId") != capture_session_id
        or response.get("originalShadersPreserved") is not True
        or response.get("replayMutation") is not False
        or not isinstance(replacements, list)
        or response.get("count") != len(replacements)
    ):
        raise RuntimeError("RenderDoc returned invalid shared-replacement metadata")
    matches = [
        item
        for item in replacements
        if isinstance(item, Mapping)
        and item.get("replacementId") == replacement_id
        and item.get("workspaceId") == status.get("workspaceId")
        and (
            item.get("revision") == revision
            or isinstance(item.get("revision"), int)
            and not isinstance(item.get("revision"), bool)
            and 0 < item["revision"] < revision
            and _current_compiled_pair_matches(status, revision, candidate_raw_sha256)
        )
        and item.get("candidateRawSha256") == candidate_raw_sha256
        and item.get("stage") == status.get("stage")
        and item.get("entryPoint", "") == status.get("entryPoint", "")
        and item.get("api") == _reuse_api_family(capture.get("api"))
        and item.get("encoding") == _asset_encoding(latest.get("kind"))
        and item.get("originalResourceId") == shader.get("resourceId")
        and item.get("originalRawSha256") == original_raw_sha256
        and item.get("physicalAbiSha256") in _workspace_physical_abi_hashes(status)
        and (
            expected_anchor is None
            or item.get("anchorEventId") == expected_anchor
        )
        and item.get("resourceScoped") is True
        and item.get("eventScoped") is False
        and item.get("allOriginalResourceUsesAffected") is True
        and item.get("originalShaderPreserved") is True
    ]
    if len(matches) != 1:
        raise RuntimeError("replacement_id is not the exact applied workspace candidate")
    return dict(matches[0])


def _persist_derived_shader_association(
    active: Mapping[str, Any], replay: Any, binding_state: Mapping[str, Any],
    capture_path: Path, capture_session_id: str, shader_record: Mapping[str, Any],
    realized_receipt: Mapping[str, Any], live_replacement: Mapping[str, Any],
) -> Path:
    """Keep current project/EID delivery receipts without changing RDC bytes."""
    binding = _binding_document(active, binding_state, [shader_record], [], [realized_receipt])
    _revalidate_binding_state_before_commit(replay, binding_state, binding)
    project = Path(active["projectRoot"]).resolve()
    replay_path = Path(active["paths"]["replayProject"]).resolve()
    capture_key = hashlib.sha256(_path_key(capture_path).encode("utf-8")).hexdigest()
    path = replay_path / "derived" / "shader-associations" / capture_key / (shader_record["knowledgeId"] + ".json")
    if not is_within(project, path.resolve()) or path.is_symlink() or replay_path.is_symlink():
        raise RuntimeError("derived Shader association path is redirected")
    value = {
        "schema": "replaylab-project-shader-association/v1", "schemaVersion": 1,
        "capturePath": str(capture_path), "captureSessionId": capture_session_id,
        "captureSha256": _verified_rdc_content_sha256(capture_path),
        "binding": binding, "replacement": copy.deepcopy(dict(live_replacement)),
        "physicalCandidateRevision": live_replacement["revision"],
        "currentSourceRevision": shader_record["candidateRevision"],
        "physicalApplyAuthority": False, "freshTargetValidationRequired": True,
    }
    value["sealSha256"] = _canonical_sha256(value)
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_FAST_CATALOG_BYTES:
        raise RuntimeError("derived Shader association exceeds its byte limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, encoded)
    return path


def _sync_index_binding(
    host: Any,
    active: Mapping[str, Any],
    replay: ReplayProjectService,
    binding_state: Mapping[str, Any],
    capture_path: Path,
    capture_session_id: str,
    *,
    shader_record: Mapping[str, Any] | None = None,
    material_record: Mapping[str, Any] | None = None,
    realized_receipt: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    inventory_cache_document: dict[str, Any] | None = None
    inventory_cache_root: Path | None = None
    inventory_cache_warning = ""
    try:
        raw_cache_root = getattr(replay, "root")
        inventory_cache_root = Path(raw_cache_root).resolve()
    except Exception:
        inventory_cache_root = None
    if inventory_cache_root is not None:
        try:
            inventory_cache_document = load_shader_inventory_cache(
                inventory_cache_root,
                _hex(binding_state.get("projectId"), 32, "projectId"),
                _hex(binding_state.get("scopeSha256"), 64, "scopeSha256"),
                capture_path,
            )
        except RdcShaderInventoryCacheError as exc:
            inventory_cache_warning = (
                "Shader inventory cache was ignored after project publication: %s"
                % exc
            )[:4096]
        except Exception:
            # The derived cache cannot make an otherwise valid explicit
            # project publication fail.
            inventory_cache_document = None
    try:
        unused_transport, index = _read_rdc_index(host, capture_path, capture_session_id)
    except Exception as exc:
        return False, "RDC index read failed after project commit: %s: %s" % (
            type(exc).__name__, exc
        )
    base = index or new_index()
    old = select_project_binding(
        base, binding_state["projectId"], active["workspaceId"]
    )
    if old is not None and any(
        old.get(field) != binding_state.get(field)
        for field in (
            "projectId",
            "scopeSha256",
            "captureId",
            "catalogSha256",
            "matcherPolicySha256",
        )
    ):
        old = None
    shaders = list(old.get("shaders", []) or []) if old else []
    materials = list(old.get("materials", []) or []) if old else []
    old_receipts = (
        list(old.get("realizedApplySnapshot", {}).get("receipts", []) or [])
        if old
        else []
    )
    # Project knowledge is immutable but the manifest can be repaired or
    # replaced outside this process. Preserve only associations that still
    # name exact current records and exact current-catalog Shader identities.
    current_shaders: list[dict[str, Any]] = []
    for item in shaders:
        try:
            knowledge = binding_state.get("workspaces", {}).get(
                item.get("knowledgeId")
            )
            if (
                not isinstance(knowledge, Mapping)
                or knowledge.get("sourceSha256") != item.get("sourceSha256")
            ):
                continue
            _capture_shader(binding_state, item["identity"])
            current_shaders.append(item)
        except Exception:
            continue
    shaders = current_shaders
    current_materials: list[dict[str, Any]] = []
    for item in materials:
        knowledge = binding_state.get("materials", {}).get(item.get("knowledgeId"))
        if (
            isinstance(knowledge, Mapping)
            and knowledge.get("constantBlockPath") == item.get("path")
            and knowledge.get("constantBlockLayoutSha256")
            == item.get("layoutSha256")
        ):
            current_materials.append(item)
    materials = current_materials
    if shader_record is not None:
        key = (
            shader_record["identity"]["resourceId"],
            shader_record["identity"]["stage"],
            shader_record["identity"].get("entryPoint", ""),
            shader_record["identity"]["stableKeySha256"],
        )
        shaders = [
            item
            for item in shaders
            if (
                item["identity"]["resourceId"],
                item["identity"]["stage"],
                item["identity"].get("entryPoint", ""),
                item["identity"]["stableKeySha256"],
            )
            != key
        ]
        shaders.append(copy.deepcopy(dict(shader_record)))
    if material_record is not None:
        key = (
            material_record["shaderResourceId"],
            material_record["stage"],
            material_record["eventId"],
            material_record["blockIndex"],
            material_record["path"],
        )
        materials = [
            item
            for item in materials
            if (
                item["shaderResourceId"],
                item["stage"],
                item["eventId"],
                item["blockIndex"],
                item["path"],
            )
            != key
        ]
        materials.append(copy.deepcopy(dict(material_record)))
    desired_receipt_keys = {
        (
            item["identity"]["stableKeySha256"],
            item["identity"]["resourceId"],
            item["managedWorkspace"]["workspaceId"],
            item["candidateRevision"],
            item["candidateRawSha256"],
        )
        for item in shaders
    }
    receipt_candidates = [
        copy.deepcopy(dict(item))
        for item in old_receipts
        if isinstance(item, Mapping)
        and (
            item.get("stableKeySha256"),
            item.get("originalResourceId"),
            item.get("workspaceId"),
            item.get("candidateRevision"),
            item.get("candidateRawSha256"),
        )
        in desired_receipt_keys
    ]
    if realized_receipt is not None:
        if shader_record is None:
            return False, "realized Shader receipt has no desired Shader record"
        replacement_key = (
            shader_record["identity"]["stableKeySha256"],
            shader_record["identity"]["resourceId"],
            shader_record["managedWorkspace"]["workspaceId"],
            shader_record["candidateRevision"],
            shader_record["candidateRawSha256"],
        )
        receipt_candidates = [
            item
            for item in receipt_candidates
            if (
                item.get("stableKeySha256"),
                item.get("originalResourceId"),
                item.get("workspaceId"),
                item.get("candidateRevision"),
                item.get("candidateRawSha256"),
            )
            != replacement_key
        ]
        receipt_candidates.append(copy.deepcopy(dict(realized_receipt)))
    try:
        complete_receipts = _normalised_receipts_for_records(
            shaders, receipt_candidates
        )
    except Exception:
        # A legacy/partial receipt set cannot be promoted.  Keep the desired
        # project binding but safely downgrade this optional accelerator.
        complete_receipts = []
    binding_state_for_write = dict(binding_state)
    if (
        "activeKnowledgeRevision" not in binding_state_for_write
        and old
        and isinstance(old.get("desiredProjectHead"), Mapping)
    ):
        binding_state_for_write["activeKnowledgeRevision"] = old[
            "desiredProjectHead"
        ].get("activeKnowledgeRevision", 0)
    binding = _binding_document(
        active,
        binding_state_for_write,
        shaders,
        materials,
        complete_receipts,
    )
    if material_record is not None and material_record not in binding["materials"]:
        return False, (
            "project committed, but the RDC project index cannot encode material knowledge "
            "without an associated managed Shader workspace"
        )
    try:
        _revalidate_binding_state_before_commit(
            replay, binding_state_for_write, binding
        )
    except Exception as exc:
        return False, (
            "project referenced head changed before RDC index commit: %s: %s"
            % (type(exc).__name__, exc)
        )[:4096]
    written, warning, unused_index = _write_index_best_effort(
        host, base, binding, capture_path, capture_session_id
    )
    if (
        written
        and inventory_cache_root is not None
        and inventory_cache_document is not None
    ):
        try:
            write_shader_inventory_cache(
                inventory_cache_root,
                _hex(binding_state.get("projectId"), 32, "projectId"),
                _hex(binding_state.get("scopeSha256"), 64, "scopeSha256"),
                capture_path,
                inventory_cache_document,
            )
        except Exception as exc:
            inventory_cache_warning = (
                "Shader inventory cache observation could not be refreshed "
                "after project publication: %s: %s"
                % (type(exc).__name__, exc)
            )[:4096]
    combined_warning = "; ".join(
        item for item in (warning, inventory_cache_warning) if item
    )[:4096]
    return written, combined_warning


def _register_applied_workspace_with_readback(
    project: Any,
    workspace_service: Any,
    workspace: Path,
    expected_status: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist a live-applied user revision without re-running AI organization.

    The caller has already verified the exact live replacement and owns the
    active-project pin. A running watcher owns the workspace source lease, so
    taking a second lease here would always reject successful live edits.
    Instead the writer requires the current source to match the immutable
    ABI-validated candidate, and both source/status and project artifacts are
    read back. A concurrent save can never be recorded as the applied version.
    """

    status = workspace_service.status(str(workspace))
    if status != expected_status:
        raise RuntimeError("workspace changed before applied project publication")
    source = status.get("source")
    latest = status.get("latestCompiled")
    if not isinstance(source, Mapping) or not isinstance(latest, Mapping):
        raise RuntimeError("applied workspace omitted source/candidate state")
    gate = latest.get("abiGate")
    if (
        not isinstance(gate, Mapping)
        or gate.get("status") != "compatible"
        or latest.get("replayAbiValidated") is not True
        or latest.get("abiCompatible") is not True
        or latest.get("requiresReplayAbiValidation") is not False
        or latest.get("validationScope") != "replay-reflection-abi-compatible"
        or latest.get("semanticEquivalenceVerified") is not False
        or status.get("replayAbiValidated") is not True
        or status.get("abiCompatible") is not True
        or latest.get("revision") != status.get("revision")
        or latest.get("sourceSha256") != source.get("currentSha256")
    ):
        raise RuntimeError("applied project publication requires current compatible ABI evidence")
    host = _host_module()
    context = host._workspace_active_capture_context(str(workspace), status=status)
    # The applied workspace may refer to a recovered/historical catalog. Admit
    # that exact validated revision before publication; importing the current
    # UI catalog cannot repair a different revision's missing provenance.
    host._ensure_active_workspace_project_capture(project, status, context)
    if workspace_service.status(str(workspace)) != status:
        raise RuntimeError("workspace changed during applied catalog registration")
    from .shader_final_publication import preserve_historical_artifacts
    publication_status = preserve_historical_artifacts(project, status)
    historical = publication_status.get("historicalPublicationEvidence")
    # Finish/source publication may already have committed this exact pair
    # with current analysis. Reuse that verified active head instead of trying
    # to reactivate its older analysis-free sibling as "new" knowledge.
    head = project.replay_service().find_current_workspace_source_head(
        source_sha256=source["currentSha256"],
        raw_sha256=status["baseline"]["rawSha256"],
        stage=status["stage"], entry_point=status.get("entryPoint", ""),
        workspace_id=status["workspaceId"], capture_id=status["capture"]["captureId"],
        target_profile=status["targetProfile"], baseline_abi_sha256=status["baseline"].get("abiSha256", ""),
    )
    existing = head.get("knowledge") if isinstance(head, Mapping) else None
    if not historical and isinstance(existing, Mapping) and (
        existing.get("workspaceId") == status.get("workspaceId")
        and existing.get("captureId") == status["capture"]["captureId"]
        and existing.get("originResourceId") == status["shader"]["resourceId"]
        and Path(str(existing.get("workspacePath", ""))).resolve() == workspace.resolve()
        and existing.get("workspaceManifestSha256")
        == host._bounded_file_sha256(Path(status["manifestPath"]), 64 * 1024 * 1024, "workspace manifest")
        and isinstance(existing.get("compiledCandidate"), Mapping)
        and existing["compiledCandidate"].get("sourceRevision") == status["revision"]
        and existing["compiledCandidate"].get("rawSha256") == latest["rawSha256"]
    ):
        from .projects import _workspace_publication_scope
        receipt = {"knowledge": dict(existing), "applied": False, "idempotent": True,
                   "reusedCurrentHead": True, **_workspace_publication_scope()}
        validated = host._validated_active_workspace_registration(receipt, status, existing)
        if workspace_service.status(str(workspace)) != status:
            raise RuntimeError("workspace changed during applied project readback")
        head_after = project.replay_service().find_current_workspace_source_head(
            source_sha256=source["currentSha256"], raw_sha256=status["baseline"]["rawSha256"],
            stage=status["stage"], entry_point=status.get("entryPoint", ""),
            workspace_id=status["workspaceId"], capture_id=status["capture"]["captureId"],
            target_profile=status["targetProfile"], baseline_abi_sha256=status["baseline"].get("abiSha256", ""),
        )
        if head_after != head:
            raise RuntimeError("project head changed during applied project readback")
        host._require_same_active_capture(context, allow_missing_capture_id=True)
        return validated
    from .shader_final_publication import recover_finished_binding, require_finish_binding
    recovered_finish = recover_finished_binding(workspace, status)
    if recovered_finish is not None:
        require_finish_binding(host, status, recovered_finish)
    # A watcher may already own the source lease. Keep this existing applied
    # transaction/readback path instead of nesting the finish-only lease.
    publication_options = {"include_explanatory": recovered_finish is not None}
    if historical:
        publication_options["expected_head"] = historical["expectedHead"]
    committed = project.register_workspace(publication_status, **publication_options)
    knowledge = committed.get("knowledge") if isinstance(committed, Mapping) else None
    if not isinstance(knowledge, Mapping):
        raise RuntimeError("applied project publication omitted workspace knowledge")
    knowledge_id = _hex(knowledge.get("knowledgeId"), 32, "workspace knowledgeId")
    published = project.get_workspace_knowledge(knowledge_id)
    if workspace_service.status(str(workspace)) != status:
        raise RuntimeError("workspace changed during applied project publication")
    if recovered_finish is None and (
        knowledge.get("analysisSha256")
        or committed.get("flowchartArtifacts") != []
        or committed.get("flowGraphArtifacts") != []
    ):
        raise RuntimeError("applied project publication reused stale AI documentation")
    host._require_same_active_capture(context, allow_missing_capture_id=True)
    validation_options = {"allow_historical_manifest": True} if historical else {}
    validated = host._validated_active_workspace_registration(committed, status, published, **validation_options)
    return {**validated, **({"recoveredCurrentReview": True} if recovered_finish is not None else {})}


def sync_active_shader_workspace_after_apply(
    workspace_path: str | Path,
    expected_revision: int,
    expected_candidate_raw_sha256: str,
    replacement_id: str,
    *, embed_capture_index: bool = False,
    expected_finish: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Commit current source/native-pair delivery and a derived RDC/EID receipt."""

    if not isinstance(embed_capture_index, bool):
        raise ValueError("embed_capture_index must be a boolean")
    with _RESTORE_GUARD:
        host = _host_module()
        active = host._active_project_service().status()
        workspace = Path(workspace_path).expanduser().resolve()
        managed = host._require_managed_active_workspace(active, str(workspace))
        status = _workspace_artifact_service(host).status(str(managed))
        revision = _integer(expected_revision, "expected_revision", 1)
        candidate_raw = _hex(
            expected_candidate_raw_sha256, 64, "expected_candidate_raw_sha256"
        )
        replacement = _hex(replacement_id, 32, "replacement_id")
        latest = status.get("latestCompiled") if isinstance(status, Mapping) else None
        source = status.get("source") if isinstance(status, Mapping) else None
        if (
            not isinstance(latest, Mapping)
            or not isinstance(source, Mapping)
            or latest.get("revision") != revision
            or latest.get("rawSha256") != candidate_raw
            or latest.get("sourceSha256") != source.get("currentSha256")
        ):
            raise RuntimeError("workspace changed after the applied candidate")
        if expected_finish is not None:
            from .shader_final_publication import require_finish_binding
            # A finish already validated this exact source. Never turn a late
            # source change into another ABI/compile pass during persistence.
            require_finish_binding(host, status, expected_finish)
        capture_path = Path(str(status.get("capture", {}).get("capturePath", ""))).resolve()
        live = host._active_live_context(allow_missing_capture_id=True)
        session_id = _hex(
            live.get("capture", {}).get("captureSessionId"),
            32,
            "capture.captureSessionId",
        )
        if _path_key(live.get("capture", {}).get("path", "")) != _path_key(capture_path):
            raise RuntimeError("active capture differs from the applied workspace")
        if active.get("scopeConfigured") is False:
            raise RuntimeError("project scope is not configured for applied Shader publication")
        if (latest.get("replayAbiValidated") is not True
                or latest.get("abiCompatible") is not True
                or latest.get("requiresReplayAbiValidation") is not False
                or latest.get("validationScope") != "replay-reflection-abi-compatible"):
            # The fast live-save route validates the complete physical ABI at
            # replacement time, but does not fabricate an immutable reflection
            # report for the new source. Project restoration needs that report
            # alongside the executable. Obtain it only when missing; keep the
            # installed Native target and its signed physical revision intact.
            unused_service, checked, unused_request, validation = host._fresh_shader_candidate_validation(
                str(managed), revision, candidate_raw)
            if (validation.get("replayAbiValidated") is not True
                    or validation.get("captureSessionId") != session_id
                    or checked.get("source", {}).get("currentSha256") != source.get("currentSha256")):
                raise RuntimeError("applied Shader project validation changed its source or capture")
            status = checked
            latest, source = status["latestCompiled"], status["source"]
        live_replacement = _live_replacement(
            host, status, session_id, replacement, revision, candidate_raw,
            include_validation_receipts=True,
        )
        active_service = host._active_project_service()
        with active_service.pin_active_project(active["workspaceId"], active["projectRoot"]):
            host._require_same_active_project(active)
            if expected_finish is not None:
                from .shader_final_publication import register_finished_workspace
                committed = register_finished_workspace(
                    host, active_service, _workspace_artifact_service(host), managed,
                    status, expected_finish, live)
            else:
                committed = _register_applied_workspace_with_readback(
                    active_service, _workspace_artifact_service(host), managed, status)
            knowledge = committed.get("knowledge") if isinstance(committed, Mapping) else None
            if not isinstance(knowledge, Mapping):
                raise RuntimeError("project workspace registration returned invalid knowledge")
        # Re-read after the durable project commit so a newer save cannot be
        # represented by the old applied revision in the RDC cache.
        after = _workspace_artifact_service(host).status(str(managed))
        if (
            after.get("revision") != status.get("revision")
            or after.get("source", {}).get("currentSha256")
            != source.get("currentSha256")
            or after.get("latestCompiled", {}).get("rawSha256") != candidate_raw
        ):
            raise RuntimeError("workspace changed while project knowledge was committed")
        live_replacement = _live_replacement(
            host, after, session_id, replacement, revision, candidate_raw,
            include_validation_receipts=True,
        )
        replay = ReplayProjectService(active["paths"]["replayProject"])
        # Workspace catalogSha256 hashes the on-disk capture catalog; the
        # project's originCatalogSha256 hashes its normalized immutable copy.
        # Recovery/export can change blob paths or formatting without changing
        # the Shader. Use the validated publication's project identity, never
        # the transport-file hash or whichever catalog happens to be active.
        origin_catalog_sha = _hex(
            knowledge.get("originCatalogSha256"), 64,
            "workspace knowledge originCatalogSha256",
        )
        binding_state = replay.restore_knowledge_snapshot(
            status["capture"]["captureId"], origin_catalog_sha
        )
        identity = next(
            (
                _publication_shader_identity(item, after, live_replacement)
                for item in binding_state["capture"]["shaders"]
                if item.get("resourceId") == status["shader"]["resourceId"]
                and item.get("stage") == status["stage"]
                and item.get("entryPoint", "") == status.get("entryPoint", "")
                and item.get("rawSha256") == status["shader"]["rawSha256"]
            ),
            None,
        )
        if identity is None:
            raise RuntimeError("committed workspace is absent from its project capture")
        shader_record = _shader_record_from_status(
            Path(active["projectRoot"]).resolve(),
            identity,
            str(knowledge["knowledgeId"]),
            managed,
            after,
            project_knowledge=knowledge,
        )
        realized_receipt = _realized_receipt_from_record(
            shader_record,
            _realized_identity_abi(shader_record, live_replacement, after),
            abi_report_sha256=(
                str(live_replacement.get("abiReportSha256", "") or "") or None
            ),
        )
        association_path = None
        warning = ""
        try:
            association_path = _persist_derived_shader_association(
                active, replay, binding_state, capture_path, session_id,
                shader_record, realized_receipt, live_replacement)
        except Exception as exc:
            warning = "derived Shader association could not be saved: %s: %s" % (type(exc).__name__, exc)
        # An unchanged native binary can represent a newer source revision.
        # Keep the signed physical revision/report intact while binding its
        # optional derived proof to the exact newly compiled source.
        pair_written = False
        lease = {key: live_replacement.get(key) for key in (
            "abiLeaseRequestId", "abiLeaseReportSha256", "abiLeaseCandidateRawSha256", "abiLeaseRevision")}
        if lease["abiLeaseRequestId"] is None:
            lease = {"abiLeaseRequestId": live_replacement.get("abiRequestId"),
                     "abiLeaseReportSha256": live_replacement.get("abiReportSha256"),
                     "abiLeaseCandidateRawSha256": live_replacement.get("candidateRawSha256"),
                     "abiLeaseRevision": live_replacement.get("revision")}
        pair_written = _persist_workspace_shader_pair_validation(
            after, live_replacement, lease, allow_same_pair_revision_reuse=True)
        written = False
        if embed_capture_index:
            written, index_warning = _sync_index_binding(
                host, active, replay, binding_state, capture_path, session_id,
                shader_record=shader_record, realized_receipt=realized_receipt)
            warning = "; ".join(item for item in (warning, index_warning) if item)
        from .shader_workspace.alias_display_mirror import restore_display_alias_mirrors
        try:
            display_aliases = restore_display_alias_mirrors(
                host, managed, after, session_id, active["projectRoot"])
        except Exception as alias_error:
            display_aliases = {"status": "failed", "diagnostic": str(alias_error)[:400]}
        return {
            "schema": SHADER_SYNC_SCHEMA,
            "schemaVersion": 1,
            "workspacePath": str(managed),
            "workspaceId": status["workspaceId"],
            "captureId": status["capture"]["captureId"],
            "captureSessionId": session_id,
            "replacementId": replacement,
            "revision": revision,
            "physicalCandidateRevision": live_replacement["revision"],
            "candidateRawSha256": candidate_raw,
            "projectCommitted": True,
            "knowledge": copy.deepcopy(dict(knowledge)),
            **({"workspaceRegistration": committed} if expected_finish is not None else {}),
            "rdcIndexWritten": written,
            "derivedAssociationWritten": association_path is not None,
            "derivedAssociationPath": str(association_path) if association_path else None,
            "workspacePairValidationWritten": pair_written,
            "displayAliases": display_aliases,
            "warning": warning[:4096] or None,
        }


def sync_active_material_session(
    session_id: str, parameter_ids: Sequence[str]
) -> dict[str, Any]:
    """Commit selected live values, then refresh their RDC association hint."""

    with _RESTORE_GUARD:
        host = _host_module()
        token = _hex(session_id, 32, "session_id")
        requested = [str(value or "").strip().lower() for value in parameter_ids]
        if not requested or len(set(requested)) != len(requested):
            raise ValueError("parameter_ids must be a non-empty unique sequence")
        session = _tool_json(
            host.get_material_session_status(token),
            "get material session status",
            schema="renderdoc-ultra-material-session/v1",
            schema_version=1,
        )
        active_service = host._active_project_service()
        active = active_service.status()
        replay_status = active.get("replayProject") if isinstance(active, Mapping) else None
        if not isinstance(replay_status, Mapping):
            raise RuntimeError("active project omitted its replay-project identity")
        project_id = _hex(replay_status.get("projectId"), 32, "replay projectId")
        with active_service.pin_active_project(active["workspaceId"], active["projectRoot"]):
            host._require_same_active_project(active)
            accumulated = _remember_material_sync_parameters(
                token,
                session,
                project_id,
                str(active["workspaceId"]),
                requested,
            )
            committed = active_service.save_material_knowledge(session, accumulated)
            knowledge = committed.get("knowledge") if isinstance(committed, Mapping) else None
            if not isinstance(knowledge, Mapping):
                raise RuntimeError("project material save returned invalid knowledge")
            raw_committed_parameters = knowledge.get("parameters")
            if (
                not isinstance(raw_committed_parameters, list)
                or not raw_committed_parameters
                or len(raw_committed_parameters) > 256
            ):
                raise RuntimeError(
                    "project material save omitted its complete parameter head"
                )
            committed_parameter_ids: list[str] = []
            for index, parameter in enumerate(raw_committed_parameters):
                if not isinstance(parameter, Mapping):
                    raise RuntimeError(
                        "project material parameter[%d] is invalid" % index
                    )
                committed_parameter_ids.append(
                    _hex(
                        parameter.get("parameterId"),
                        32,
                        "project material parameterId",
                    )
                )
            if (
                len(set(committed_parameter_ids)) != len(committed_parameter_ids)
                or not set(accumulated).issubset(committed_parameter_ids)
            ):
                raise RuntimeError(
                    "project material save returned an inconsistent parameter head"
                )
            # Durable project state is authoritative. Prime the in-memory table
            # with the complete committed head only as a subsequent-call
            # optimisation; Host restart/eviction cannot affect correctness.
            _remember_material_sync_parameters(
                token,
                session,
                project_id,
                str(active["workspaceId"]),
                committed_parameter_ids,
            )
        # Saving knowledge is the durable transaction.  Only then is the RDC
        # hint refreshed, and failure there is reported as a warning.
        replay = ReplayProjectService(active["paths"]["replayProject"])
        binding_state = replay.restore_knowledge_snapshot(session["captureId"], "")
        live = host._active_live_context(allow_missing_capture_id=True)
        capture_path = Path(str(live.get("capture", {}).get("path", ""))).resolve()
        capture_session_id = _hex(
            live.get("capture", {}).get("captureSessionId"),
            32,
            "capture.captureSessionId",
        )
        if capture_session_id != session.get("captureSessionId"):
            raise RuntimeError("material session belongs to another capture lifecycle")
        material_record = {
            "knowledgeId": knowledge["knowledgeId"],
            "shaderResourceId": session["shaderResourceId"],
            "stage": session["stage"],
            "blockIndex": session["constantBlockIndex"],
            "path": session["constantBlockPath"],
            "layoutSha256": session["layoutSha256"],
            "eventId": session["eventId"],
            "parameters": [
                {"parameterId": parameter_id}
                for parameter_id in committed_parameter_ids
            ],
        }
        written, warning = _sync_index_binding(
            host,
            active,
            replay,
            binding_state,
            capture_path,
            capture_session_id,
            material_record=material_record,
        )
        return {
            "schema": MATERIAL_SYNC_SCHEMA,
            "schemaVersion": 1,
            "sessionId": token,
            "captureId": session["captureId"],
            "captureSessionId": capture_session_id,
            "parameterIds": committed_parameter_ids,
            "projectCommitted": True,
            "knowledge": copy.deepcopy(dict(knowledge)),
            "rdcIndexWritten": written,
            "warning": warning or None,
        }


__all__ = [
    "MATERIAL_SYNC_SCHEMA",
    "RESTORE_PROGRESS_SCHEMA",
    "RESTORE_SCHEMA",
    "SHADER_SYNC_SCHEMA",
    "automatic_project_sync_suppressed",
    "restore_active_capture_project",
    "sync_active_material_session",
    "sync_active_shader_workspace_after_apply",
]
