"""Host-owned, capture-bound resource export lifecycle.

The replay bridge is allowed to write capture evidence only through a ticket
created here.  Model-visible tools never choose an output directory.  This
module then validates the bridge bundle and performs the deterministic part of
the conversion: a portable evidence copy, glTF 2.0 mesh generation, and fixed
Unity URP / Blender adapter scaffolds.  Semantic choices remain explicit in a
hash-bound transfer specification.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence


RESOURCE_EXPORT_ROOT_ENV = "RENDERDOC_ULTRA_RESOURCE_EXPORT_ROOT"
GLTF_VALIDATOR_ENV = "RENDERDOC_ULTRA_GLTF_VALIDATOR"
BLENDER_ENV = "RENDERDOC_ULTRA_BLENDER"
UNITY_VALIDATOR_ENV = "RENDERDOC_ULTRA_UNITY_VALIDATOR"
OPERATION_SCHEMA = "replaylab-resource-export-operation/v1"
TICKET_SCHEMA = "replaylab-resource-export-ticket/v1"
CAPTURE_RESPONSE_SCHEMA = "replaylab-resource-export-capture/v1"
BUNDLE_SCHEMA = "replaylab-resource-export-bundle/v1"
TRANSFER_SCHEMA = "replaylab-resource-transfer-spec/v1"
FINAL_MANIFEST_SCHEMA = "replaylab-resource-export-package/v1"
FINALIZE_INTENT_SCHEMA = "replaylab-resource-export-finalize-intent/v1"
VALIDATION_SCHEMA = "replaylab-resource-export-validation/v1"
TARGET_VALIDATION_REQUEST_SCHEMA = (
    "replaylab-resource-export-target-validation-request/v1"
)
TARGET_VALIDATION_RECEIPT_SCHEMA = (
    "replaylab-resource-export-target-validation-receipt/v1"
)
TARGET_VALIDATION_RUN_SCHEMA = "replaylab-resource-export-target-validation-run/v1"
BLENDER_PROVIDER_RECEIPT_SCHEMA = (
    "replaylab-resource-export-blender-provider-receipt/v1"
)
UNITY_PROVIDER_REQUEST_SCHEMA = (
    "replaylab-resource-export-unity-provider-request/v1"
)
UNITY_PROVIDER_RECEIPT_SCHEMA = (
    "replaylab-resource-export-unity-provider-receipt/v1"
)
CAPABILITIES_SCHEMA = "replaylab-resource-export-capabilities/v1"
POSTCONDITION_SCHEMA = "replaylab-resource-export-postcondition/v1"

_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ATTRIBUTE = re.compile(r"[^A-Z0-9_]+")
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_TRANSFER_SPEC_BYTES = 1024 * 1024
_MAX_SOURCE_FILE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_TOTAL_SOURCE_BYTES = 8 * 1024 * 1024 * 1024
_MAX_ATTRIBUTES = 128
_MAX_VERTEX_COUNT = 50_000_000
_MAX_CAPTURE_COMPLETION_BYTES = 256 * 1024
_MAX_CAPTURE_INCOMPLETE_ITEMS = 1024
_MAX_CAPTURE_DIAGNOSTIC_SUMMARIES = 64
_MAX_CAPTURE_MESSAGE_BYTES = 512
_MAX_PROVIDER_FILE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_PROVIDER_LOG_BYTES = 16 * 1024 * 1024
_MAX_TARGET_VALIDATION_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
_MAX_TARGET_VALIDATION_FILES = 256
_GLTF_VALIDATOR_TIMEOUT_SECONDS = 120.0
_BLENDER_VALIDATOR_TIMEOUT_SECONDS = 600.0
_UNITY_VALIDATOR_TIMEOUT_SECONDS = 900.0
_PROVIDER_POLL_SECONDS = 0.05
_TARGET_PROVIDER_ENV = frozenset(
    {GLTF_VALIDATOR_ENV, BLENDER_ENV, UNITY_VALIDATOR_ENV}
)
_PROVIDER_PROCESS_ENV = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
)
_IDENTITY_4X4 = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
)


# Unity and Blender scaffolds are produced by default: a package the user cannot open
# in their engine is not a finished export. glTF is always included as the evidence
# format; obj and fbx are opt-in.
DEFAULT_EXPORT_TARGETS = ("gltf", "unity-urp", "blender")
_FBX_CONVERSION_TIMEOUT_SECONDS = 600


class ResourceExportError(RuntimeError):
    """A stable validation or lifecycle error."""


def resolve_resource_export_root(environment: Mapping[str, str] | None = None) -> Path:
    """Resolve the process-owner-controlled export root.

    Requests never supply this location.  Avoid ``resolve`` here so a
    configured reparse point can be rejected when the root is first used.
    """

    values = os.environ if environment is None else environment
    configured = str(values.get(RESOURCE_EXPORT_ROOT_ENV, "") or "").strip()
    if configured:
        root = Path(configured).expanduser()
    else:
        local_app_data = str(values.get("LOCALAPPDATA", "") or "").strip()
        if local_app_data:
            base = Path(local_app_data).expanduser()
        else:
            xdg_cache = str(values.get("XDG_CACHE_HOME", "") or "").strip()
            base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
        root = base / "RenderDocUltra" / "resource-exports"
    return Path(os.path.abspath(str(root)))


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ResourceExportError("value is not canonical JSON: %s" % error) from error


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _file_sha256(path: Path, maximum_bytes: int = _MAX_SOURCE_FILE_BYTES) -> str:
    before = path.stat()
    if not path.is_file() or before.st_size < 0 or before.st_size > maximum_bytes:
        raise ResourceExportError("file size is outside the resource-export limit")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(min(1024 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise ResourceExportError("file grew beyond the resource-export limit")
            digest.update(chunk)
    after = path.stat()
    if (
        total != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ResourceExportError("file changed while it was validated")
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
    except OSError:
        return False
    return bool(attributes & int(getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


def _strict_hex(value: Any, length: int, label: str) -> str:
    text = str(value or "")
    if len(text) != length or any(c not in "0123456789abcdef" for c in text):
        raise ResourceExportError("%s must be %d lowercase hexadecimal characters" % (label, length))
    return text


def _strict_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResourceExportError("%s must be an integer" % label)
    if value < minimum or value > maximum:
        raise ResourceExportError("%s is outside the supported range" % label)
    return value


def _strict_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ResourceExportError("asset_name must be a string")
    name = value.strip() or "RenderDocAsset"
    if len(name) > 128 or "\x00" in name or any(ord(c) < 32 for c in name):
        raise ResourceExportError("asset_name is invalid")
    return name


def _within(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(candidate))) == str(root)
    except (ValueError, OSError):
        return False


def _relative_parts(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ResourceExportError("%s must be a non-empty relative path" % label)
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ResourceExportError("%s must be relative" % label)
    parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        raise ResourceExportError("%s escapes its managed directory" % label)
    return parts


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(path, _canonical_json_bytes(value) + b"\n")


def _read_json(path: Path, maximum_bytes: int = _MAX_JSON_BYTES) -> dict[str, Any]:
    if _is_reparse(path) or not path.is_file():
        raise ResourceExportError("managed JSON file is missing or redirected")
    size = path.stat().st_size
    if size <= 0 or size > maximum_bytes:
        raise ResourceExportError("managed JSON file has an invalid size")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ResourceExportError("managed JSON file is invalid: %s" % error) from error
    if not isinstance(value, dict):
        raise ResourceExportError("managed JSON root must be an object")
    return value


def _manifest_file_records(bundle: Mapping[str, Any], response: Mapping[str, Any]) -> list[dict[str, Any]]:
    records = bundle.get("files")
    if not isinstance(records, list):
        records = response.get("files")
    if not isinstance(records, list):
        raise ResourceExportError("capture bundle omitted its files list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            raise ResourceExportError("capture bundle file %d is invalid" % index)
        relative = str(item.get("path", item.get("relativePath", "")) or "")
        parts = _relative_parts(relative, "capture bundle file path")
        key = "/".join(parts)
        if key in seen:
            raise ResourceExportError("capture bundle contains a duplicate file path")
        seen.add(key)
        sha256 = _strict_hex(item.get("sha256"), 64, "capture bundle file sha256")
        size = _strict_int(item.get("size", item.get("byteLength", -1)), "capture bundle file size", 0, _MAX_SOURCE_FILE_BYTES)
        result.append({"path": key, "sha256": sha256, "size": size})
    return sorted(result, key=lambda item: item["path"])


def _bounded_capture_text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ResourceExportError("%s must be a bounded string" % label)
    text = value.strip()
    if not text and not allow_empty:
        raise ResourceExportError("%s must not be empty" % label)
    if len(text.encode("utf-8")) > _MAX_CAPTURE_MESSAGE_BYTES:
        raise ResourceExportError("%s exceeds its receipt size limit" % label)
    return text


def _capture_completion_receipt(
    bundle: Mapping[str, Any], response: Mapping[str, Any]
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Project bridge completion/diagnostics into a small canonical receipt."""

    raw_completion = bundle.get("completion")
    if raw_completion is None:
        raw_completion = {}
    if not isinstance(raw_completion, dict):
        raise ResourceExportError("capture bundle completion must be an object")
    raw_status = raw_completion.get("status", response.get("status", "unknown"))
    status = _bounded_capture_text(raw_status, "capture completion status")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", status):
        raise ResourceExportError("capture completion status is invalid")
    response_status = response.get("status")
    if response_status is not None:
        response_status = _bounded_capture_text(
            response_status, "bridge capture status"
        )
        if response_status != status:
            raise ResourceExportError(
                "bridge capture status disagrees with bundle completion"
            )
    incomplete = raw_completion.get("incomplete", [])
    if not isinstance(incomplete, list):
        raise ResourceExportError("capture completion incomplete must be a list")
    if len(incomplete) > _MAX_CAPTURE_INCOMPLETE_ITEMS:
        raise ResourceExportError("capture completion has too many incomplete items")
    clean_incomplete = [
        _bounded_capture_text(item, "capture incomplete item")
        for item in incomplete
    ]
    if status == "complete" and clean_incomplete:
        raise ResourceExportError(
            "complete capture bundle cannot list incomplete artifacts"
        )
    completion = {
        "status": status,
        "incomplete": clean_incomplete,
        "incompleteCount": len(clean_incomplete),
    }
    if len(_canonical_json_bytes(completion)) > _MAX_CAPTURE_COMPLETION_BYTES:
        raise ResourceExportError("capture completion receipt exceeds its size limit")

    raw_diagnostics = bundle.get("diagnostics", [])
    if not isinstance(raw_diagnostics, list):
        raise ResourceExportError("capture diagnostics must be a list")
    summaries: list[dict[str, str]] = []
    for diagnostic in raw_diagnostics[:_MAX_CAPTURE_DIAGNOSTIC_SUMMARIES]:
        summary: dict[str, str] = {}
        if isinstance(diagnostic, str):
            candidates = (("message", diagnostic),)
        elif isinstance(diagnostic, dict):
            candidates = tuple(
                (key, diagnostic.get(key))
                for key in ("id", "code", "severity", "scope", "message", "reason")
                if diagnostic.get(key) is not None
            )
        else:
            candidates = (("message", str(diagnostic)),)
        for key, value in candidates:
            text = str(value).replace("\x00", "").strip()
            encoded = text.encode("utf-8")
            if len(encoded) > _MAX_CAPTURE_MESSAGE_BYTES:
                encoded = encoded[:_MAX_CAPTURE_MESSAGE_BYTES]
                while True:
                    try:
                        text = encoded.decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        encoded = encoded[:-1]
                text += "..."
            if text:
                summary[key] = text
        if summary:
            summaries.append(summary)
    diagnostics_summary = {
        "count": len(raw_diagnostics),
        "includedCount": len(summaries),
        "omittedCount": max(0, len(raw_diagnostics) - len(summaries)),
        "summaries": summaries,
    }
    return status, completion, diagnostics_summary


class ResourceExportManager:
    """Own resource-export tickets, receipts, conversion and validation."""

    def __init__(
        self,
        root: str | Path,
        environment: Mapping[str, str] | None = None,
    ):
        self.root = Path(os.path.abspath(str(Path(root).expanduser())))
        values = os.environ if environment is None else environment
        admitted = _TARGET_PROVIDER_ENV | _PROVIDER_PROCESS_ENV
        self._environment = {
            str(key).upper(): str(value)
            for key, value in values.items()
            if str(key).upper() in admitted
        }

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "ResourceExportManager":
        values = os.environ if environment is None else environment
        return cls(resolve_resource_export_root(values), values)

    def _ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        if _is_reparse(self.root) or not self.root.is_dir():
            raise ResourceExportError("resource-export root is redirected or unavailable")
        return self.root.resolve(strict=True)

    def _operation_dir(self, operation_id: str, *, must_exist: bool = True) -> Path:
        operation = _strict_hex(operation_id, 32, "operation_id")
        root = self._ensure_root()
        operations = root / "operations"
        if must_exist and (_is_reparse(operations) or not operations.is_dir()):
            raise ResourceExportError("resource-export operations root is redirected or unavailable")
        path = operations / operation
        if must_exist:
            if _is_reparse(path) or not path.is_dir():
                raise ResourceExportError("resource-export operation does not exist")
            resolved = path.resolve(strict=True)
            if not _within(root, resolved):
                raise ResourceExportError("resource-export operation escaped its root")
            return resolved
        return path

    def _state_path(self, operation_id: str) -> Path:
        return self._operation_dir(operation_id) / "operation.json"

    def _load_state(self, operation_id: str) -> dict[str, Any]:
        state = _read_json(self._state_path(operation_id))
        if (
            state.get("schema") != OPERATION_SCHEMA
            or state.get("schemaVersion") != 1
            or state.get("operationId") != operation_id
        ):
            raise ResourceExportError("resource-export operation state is incompatible")
        _strict_hex(state.get("requestSha256"), 64, "operation requestSha256")
        return state

    def _provider_configuration(
        self, layer: str
    ) -> tuple[dict[str, Any], Path | None]:
        definitions = {
            "externalGltfValidator": (
                "khronos-gltf-validator",
                GLTF_VALIDATOR_ENV,
            ),
            "blenderValidator": ("blender-headless", BLENDER_ENV),
            "unityUrpValidator": (
                "unity-urp-external-validator",
                UNITY_VALIDATOR_ENV,
            ),
        }
        if layer not in definitions:
            raise ResourceExportError("target validator layer is unsupported")
        provider_id, environment_name = definitions[layer]
        configured = str(self._environment.get(environment_name, "") or "").strip()
        binding: dict[str, Any] = {
            "providerId": provider_id,
            "configuration": environment_name,
            "status": "unavailable",
        }
        if not configured:
            binding["reason"] = "provider is not configured in the Host process"
            return binding, None
        configured_path = Path(configured).expanduser()
        binding["configuredPathSha256"] = hashlib.sha256(
            configured.encode("utf-8")
        ).hexdigest()
        if not configured_path.is_absolute():
            binding["reason"] = "configured provider path is not absolute"
            return binding, None
        try:
            if _is_reparse(configured_path) or not configured_path.is_file():
                binding["reason"] = "configured provider is missing or redirected"
                return binding, None
            executable = configured_path.resolve(strict=True)
            binding["executableSha256"] = _file_sha256(
                executable, _MAX_PROVIDER_FILE_BYTES
            )
        except (OSError, ResourceExportError):
            binding["reason"] = "configured provider could not be fingerprinted"
            return binding, None
        binding["status"] = "available"
        return binding, executable

    def _target_validation_request(
        self,
        operation_id: str,
        state: Mapping[str, Any],
        package_manifest_sha256: str,
        targets: Sequence[str],
    ) -> tuple[dict[str, Any], dict[str, Path | None]]:
        requested = tuple(dict.fromkeys(str(item) for item in targets))
        if not requested or requested[0] != "gltf":
            raise ResourceExportError("target validation requires the canonical glTF target")
        layers = ["externalGltfValidator"]
        if "unity-urp" in requested:
            layers.append("unityUrpValidator")
        if "blender" in requested:
            layers.append("blenderValidator")
        bindings: dict[str, dict[str, Any]] = {}
        executables: dict[str, Path | None] = {}
        for layer in layers:
            binding, executable = self._provider_configuration(layer)
            bindings[layer] = binding
            executables[layer] = executable
        request = {
            "schema": TARGET_VALIDATION_REQUEST_SCHEMA,
            "schemaVersion": 1,
            "operationId": operation_id,
            "operationRequestSha256": _strict_hex(
                state.get("requestSha256"),
                64,
                "operation requestSha256",
            ),
            "packageManifestSha256": _strict_hex(
                package_manifest_sha256,
                64,
                "package manifest sha256",
            ),
            "finalizeSha256": _strict_hex(
                state.get("finalize", {}).get("finalizeSha256")
                if isinstance(state.get("finalize"), dict)
                else None,
                64,
                "finalize sha256",
            ),
            "targets": list(requested),
            "providers": bindings,
        }
        request["requestSha256"] = _json_sha256(request)
        return request, executables

    def _verify_target_provider_bindings(
        self, request: Mapping[str, Any]
    ) -> None:
        """Re-fingerprint every requested provider before admitting a receipt."""

        providers = request.get("providers")
        if not isinstance(providers, dict) or not providers:
            raise ResourceExportError("target validation request omitted providers")
        for layer, expected in providers.items():
            if not isinstance(expected, dict):
                raise ResourceExportError(
                    "target validation provider binding is invalid"
                )
            observed, unused_executable = self._provider_configuration(str(layer))
            if observed != expected:
                raise ResourceExportError(
                    "target validator %s changed during validation" % layer
                )

    def _provider_process_environment(self, provider_root: Path) -> dict[str, str]:
        result = {
            key: value
            for key, value in self._environment.items()
            if key in _PROVIDER_PROCESS_ENV
        }
        result["PYTHONIOENCODING"] = "utf-8:strict"
        result["PYTHONUTF8"] = "1"
        blender_user = provider_root / "blender-user"
        blender_user.mkdir(parents=True, exist_ok=True)
        result["BLENDER_USER_CONFIG"] = str(blender_user / "config")
        result["BLENDER_USER_SCRIPTS"] = str(blender_user / "scripts")
        result["BLENDER_USER_DATAFILES"] = str(blender_user / "datafiles")
        return result

    @staticmethod
    def _managed_file_record(root: Path, path: Path) -> dict[str, Any]:
        if _is_reparse(path) or not path.is_file():
            raise ResourceExportError("target validation artifact is missing or redirected")
        resolved = path.resolve(strict=True)
        if not _within(root, resolved):
            raise ResourceExportError("target validation artifact escaped its root")
        size = path.stat().st_size
        if size < 0 or size > _MAX_PROVIDER_FILE_BYTES:
            raise ResourceExportError("target validation artifact exceeds its size limit")
        return {
            "path": path.relative_to(root).as_posix(),
            "size": size,
            "sha256": _file_sha256(path, _MAX_PROVIDER_FILE_BYTES),
        }

    def _run_process(
        self,
        *,
        provider: Mapping[str, Any],
        executable: Path,
        arguments: Sequence[str],
        validation_root: Path,
        provider_root: Path,
        timeout_seconds: float,
        stdout_name: str,
        stderr_name: str,
    ) -> dict[str, Any]:
        """Run one fixed Host provider with bounded logs and no shell."""

        expected_sha256 = _strict_hex(
            provider.get("executableSha256"), 64, "provider executable sha256"
        )
        if _file_sha256(executable, _MAX_PROVIDER_FILE_BYTES) != expected_sha256:
            raise ResourceExportError("target validator changed before execution")
        if not arguments or str(arguments[0]) != str(executable):
            raise ResourceExportError("target validator command is not provider-bound")
        if any(not isinstance(item, str) or "\x00" in item for item in arguments):
            raise ResourceExportError("target validator command contains an invalid argument")
        if timeout_seconds <= 0.0 or timeout_seconds > _UNITY_VALIDATOR_TIMEOUT_SECONDS:
            raise ResourceExportError("target validator timeout is invalid")
        provider_root.mkdir(parents=True, exist_ok=True)
        stdout_path = provider_root.joinpath(
            *_relative_parts(stdout_name, "provider stdout path")
        )
        stderr_path = provider_root.joinpath(
            *_relative_parts(stderr_name, "provider stderr path")
        )
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        launched = False
        exit_code: int | None = None
        timed_out = False
        log_limit_exceeded = False
        launch_failed = False
        with stdout_path.open("xb") as stdout_stream, stderr_path.open("xb") as stderr_stream:
            try:
                process = subprocess.Popen(
                    list(arguments),
                    cwd=str(provider_root),
                    env=self._provider_process_environment(provider_root),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                    shell=False,
                    close_fds=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                launched = True
            except OSError:
                process = None
                launch_failed = True
            if process is not None:
                deadline = started + timeout_seconds
                while True:
                    polled = process.poll()
                    if polled is not None:
                        exit_code = int(polled)
                        break
                    stdout_stream.flush()
                    stderr_stream.flush()
                    if (
                        stdout_path.stat().st_size > _MAX_PROVIDER_LOG_BYTES
                        or stderr_path.stat().st_size > _MAX_PROVIDER_LOG_BYTES
                    ):
                        log_limit_exceeded = True
                    if time.monotonic() >= deadline:
                        timed_out = True
                    if timed_out or log_limit_exceeded:
                        process.kill()
                        try:
                            exit_code = int(process.wait(timeout=10.0))
                        except subprocess.TimeoutExpired:
                            exit_code = None
                        break
                    time.sleep(_PROVIDER_POLL_SECONDS)
        provider_changed = False
        for log_path in (stdout_path, stderr_path):
            try:
                if log_path.stat().st_size > _MAX_PROVIDER_LOG_BYTES:
                    with log_path.open("r+b") as stream:
                        stream.truncate(_MAX_PROVIDER_LOG_BYTES)
                    log_limit_exceeded = True
            except OSError as error:
                raise ResourceExportError(
                    "target validator log could not be bounded"
                ) from error
        try:
            provider_changed = (
                _file_sha256(executable, _MAX_PROVIDER_FILE_BYTES)
                != expected_sha256
            )
        except (OSError, ResourceExportError):
            provider_changed = True
        return {
            "launched": launched,
            "exitCode": exit_code,
            "timedOut": timed_out,
            "logLimitExceeded": log_limit_exceeded,
            "launchFailed": launch_failed,
            "providerChanged": provider_changed,
            "durationMs": max(0, int((time.monotonic() - started) * 1000)),
            "argvSha256": _json_sha256(list(arguments)),
            "stdout": self._managed_file_record(validation_root, stdout_path),
            "stderr": self._managed_file_record(validation_root, stderr_path),
        }

    def _target_validation_file_records(self, root: Path) -> list[dict[str, Any]]:
        if _is_reparse(root) or not root.is_dir():
            raise ResourceExportError("target validation directory is redirected")
        records: list[dict[str, Any]] = []
        total = 0
        for path in sorted(root.rglob("*")):
            if path.is_dir():
                if _is_reparse(path):
                    raise ResourceExportError(
                        "target validation contains a redirected directory"
                    )
                continue
            if path.name == "receipt.json" and path.parent == root:
                continue
            record = self._managed_file_record(root, path)
            total += int(record["size"])
            if total > _MAX_TARGET_VALIDATION_TOTAL_BYTES:
                raise ResourceExportError(
                    "target validation artifacts exceed their total size limit"
                )
            records.append(record)
            if len(records) > _MAX_TARGET_VALIDATION_FILES:
                raise ResourceExportError("target validation has too many artifacts")
        return records

    def capabilities(self) -> dict[str, Any]:
        configured_blender = str(self._environment.get(BLENDER_ENV, "") or "").strip()
        blender = configured_blender or shutil.which("blender") or ""
        configured_assimp = str(os.environ.get("RENDERDOC_ULTRA_ASSIMP", "") or "").strip()
        assimp = configured_assimp or shutil.which("assimp") or ""
        gltf_validator, unused_gltf_path = self._provider_configuration(
            "externalGltfValidator"
        )
        blender_validator, unused_blender_path = self._provider_configuration(
            "blenderValidator"
        )
        unity_validator, unused_unity_path = self._provider_configuration(
            "unityUrpValidator"
        )
        return {
            "schema": CAPABILITIES_SCHEMA,
            "schemaVersion": 1,
            "managedRoot": str(self.root),
            "capture": {
                "mesh": "all raw vertex/index inputs plus supported canonical float32le/uint32le views",
                "textures": ("exact DDS evidence plus readable copies (auto: TGA/PNG for LDR, "
                             "EXR for floating-point) and resource/view/sampler metadata"),
                "textureFormats": ["auto", "dds", "tga", "exr", "png", "jpg", "hdr", "bmp"],
                "shaders": "capture-bound binaries/reflection and child-Agent-produced semantic plans",
                "constantBuffers": "reflected layout and live draw values",
                "constantBufferTransfer": "hash-checked typed physical values, current semantic lane names, material/system/raw-buffer mappings and used-lane coverage",
                "buffers": "bounded pre-action read/storage/per-instance buffer windows plus post-action UAV references; missing counters can be reconstructed with explicit provenance and GPU readback",
                "dispatchRoute": "geometry-free effect packages with executable ComputeShader, fullscreen or custom CPU passes, explicit input initialization and temporal warmup",
                "viewSelection": {
                    "supportedViews": [0],
                    "reason": "the current bridge captures only view 0",
                },
            },
            "targets": {
                "gltf": {"available": True, "profile": "glTF 2.0 + portable evidence sidecar"},
                "unity-urp": {"available": True, "profile": "canonical captured mesh/effect + managed Unity import preserving the existing pipeline with platform and sampling evidence"},
                "blender": {"available": True, "profile": "glTF base + fixed bpy material scaffold"},
                "fbx": {
                    "available": bool(blender),
                    "profile": "glTF converted by a detected headless Blender; the receipt names the binary",
                    "detectedProviders": [
                        item for item in (
                            {"name": "blender", "executable": blender} if blender else None,
                            {"name": "assimp", "executable": assimp} if assimp else None,
                        ) if item is not None
                    ],
                    "reason": ("" if blender else
                               "no Blender was found; set RENDERDOC_ULTRA_BLENDER or install Blender to enable FBX"),
                },
            },
            "coordinatePolicy": {
                "default": "preserve",
                "explicit": "row-major target-from-source 4x4 matrix; normals use inverse transpose; negative determinant flips triangle winding and tangent sign",
                "apiDoesNotImplyObjectBasis": True,
            },
            "validation": {
                "internalStructuralAndHashValidation": True,
                "externalKhronosValidator": {
                    **gltf_validator,
                    "available": gltf_validator["status"] == "available",
                    "executedByThisWorkflow": True,
                    "invocation": "gltf_validator --stdout --validate-resources --no-write-timestamp --no-absolute-path <managed asset.gltf>",
                },
                "blenderHeadlessValidator": {
                    **blender_validator,
                    "available": blender_validator["status"] == "available",
                    "executedByThisWorkflow": True,
                    "invocation": "Blender background/factory-startup with a Host-generated validation script",
                },
                "unityUrpValidator": {
                    **unity_validator,
                    "available": unity_validator["status"] == "available",
                    "executedByThisWorkflow": True,
                    "receiptSchema": UNITY_PROVIDER_RECEIPT_SCHEMA,
                    "invocation": "fixed external provider --replaylab-resource-export-request <Host request.json>",
                },
                "runTool": "run_resource_export_target_validation",
            },
            "limits": {
                "maximumAttributes": _MAX_ATTRIBUTES,
                "maximumVertexCount": _MAX_VERTEX_COUNT,
                "maximumSourceFileBytes": _MAX_SOURCE_FILE_BYTES,
                "maximumTotalSourceBytes": _MAX_TOTAL_SOURCE_BYTES,
            },
        }

    def prepare_ticket(
        self,
        *,
        operation_id: str,
        event_id: int,
        instance: int,
        view: int,
        asset_name: str,
        capture_session_id: str,
        capture_id: str = "",
        api: str = "",
        texture_formats: Sequence[str] = ("auto",),
        capture_path: str = "",
        capture_source_identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        operation_id = _strict_hex(operation_id, 32, "operation_id")
        event_id = _strict_int(event_id, "event_id", 1, 0xFFFFFFFF)
        instance = _strict_int(instance, "instance", 0, 0x7FFFFFFF)
        view = _strict_int(view, "view", 0, 0x7FFFFFFF)
        if view != 0:
            raise ResourceExportError(
                "view must be 0 because the current resource-export bridge does not capture alternate views"
            )
        capture_session_id = _strict_hex(capture_session_id, 32, "capture_session_id")
        if capture_id:
            capture_id = _strict_hex(capture_id, 64, "capture_id")
        asset_name = _strict_name(asset_name)
        if not isinstance(api, str) or "\x00" in api or len(api) > 128:
            raise ResourceExportError("api is invalid")
        allowed_formats = ("auto", "dds", "tga", "exr", "png", "jpg", "hdr", "bmp")
        formats: list[str] = []
        for item in (texture_formats or ("auto",)):
            text = str(item or "").strip().lower()
            if not text:
                continue
            if text not in allowed_formats:
                raise ResourceExportError(
                    "unsupported texture format %r; use one of %s" % (text, ", ".join(allowed_formats)))
            if text not in formats:
                formats.append(text)
        if not formats:
            formats = ["auto"]
        if len(formats) > 6:
            raise ResourceExportError("at most six texture formats can be requested")
        request = {
            "schema": "replaylab-resource-export-request/v1",
            "schemaVersion": 1,
            "operationId": operation_id,
            "captureSessionId": capture_session_id,
            "captureId": capture_id,
            "eventId": event_id,
            "instance": instance,
            "view": view,
            "assetName": asset_name,
            "api": api,
            "textureFormats": formats,
        }
        if capture_source_identity:
            request["captureSource"] = dict(capture_source_identity)
        elif capture_path:
            from .capture_source import capture_source
            request["captureSource"] = capture_source({"path": capture_path, "api": api})
        request_sha256 = _json_sha256(request)
        root = self._ensure_root()
        operations = root / "operations"
        operations.mkdir(exist_ok=True)
        if _is_reparse(operations):
            raise ResourceExportError("resource-export operations root is redirected")
        operation_dir = operations / operation_id
        try:
            operation_dir.mkdir()
            created = True
        except FileExistsError:
            created = False
        if not created:
            state = self._load_state(operation_id)
            if state.get("requestSha256") != request_sha256 or state.get("request") != request:
                raise ResourceExportError("operation_id is already bound to a different request")
            return {
                "operation": state,
                "ticket": _read_json(operation_dir / "ticket.json"),
                "ticketPath": str(operation_dir / "ticket.json"),
                "ticketSha256": _file_sha256(operation_dir / "ticket.json", _MAX_JSON_BYTES),
                "needsCapture": state.get("status") == "ticketed",
                "idempotent": True,
            }

        relative_dir = "operations/%s/capture" % operation_id
        capture_dir = operation_dir / "capture"
        capture_dir.mkdir()
        if _is_reparse(capture_dir) or not capture_dir.is_dir():
            raise ResourceExportError("resource-export capture directory is redirected")
        ticket = {
            "schema": TICKET_SCHEMA,
            "schemaVersion": 1,
            "ticketId": operation_id,
            "managedRoot": str(root),
            "relativeDir": relative_dir,
            "requestSha256": request_sha256,
            "eventId": event_id,
            "instance": instance,
            "view": view,
            "captureSessionId": capture_session_id,
            "captureId": capture_id,
            "textureFormats": formats,
        }
        now = int(time.time() * 1000)
        state = {
            "schema": OPERATION_SCHEMA,
            "schemaVersion": 1,
            "operationId": operation_id,
            "requestSha256": request_sha256,
            "request": request,
            "status": "ticketed",
            "ticket": {
                "relativePath": "ticket.json",
                "relativeOutputDirectory": relative_dir,
            },
            "capture": None,
            "finalize": None,
            "finalizeIntent": None,
            "createdUnixMs": now,
            "updatedUnixMs": now,
        }
        try:
            _atomic_write_json(operation_dir / "ticket.json", ticket)
            _atomic_write_json(operation_dir / "operation.json", state)
        except Exception:
            # Do not recursively remove an ambiguous path.  The exact newly
            # created empty/partial operation can be inspected and a new
            # operation ID used.
            raise
        return {
            "operation": state,
            "ticket": ticket,
            "ticketPath": str(operation_dir / "ticket.json"),
            "ticketSha256": _file_sha256(operation_dir / "ticket.json", _MAX_JSON_BYTES),
            "needsCapture": True,
            "idempotent": False,
        }

    def _capture_dir(self, operation_id: str, state: Mapping[str, Any]) -> Path:
        operation_dir = self._operation_dir(operation_id)
        ticket = _read_json(operation_dir / "ticket.json")
        if ticket.get("requestSha256") != state.get("requestSha256"):
            raise ResourceExportError("resource-export ticket request binding changed")
        parts = _relative_parts(ticket.get("relativeDir"), "ticket relativeDir")
        root = self._ensure_root()
        path = root.joinpath(*parts)
        if not path.exists() or _is_reparse(path) or not path.is_dir():
            raise ResourceExportError("bridge capture directory is missing or redirected")
        resolved = path.resolve(strict=True)
        if not _within(root, resolved):
            raise ResourceExportError("bridge capture directory escaped the managed root")
        expected = operation_dir / "capture"
        if os.path.normcase(str(resolved)) != os.path.normcase(str(expected.resolve(strict=True))):
            raise ResourceExportError("bridge capture directory does not match its ticket")
        return resolved

    def fork_capture(self, operation_id: str, expected_bundle_sha256: str,
                     revision_key: str = "") -> dict[str, Any]:
        """Create a new export revision from immutable, verified capture bytes.

        Only the new ticket/request/manifest identity changes. This never calls
        the replay bridge, reads an Editor or rewrites a published package.
        A stable revision_key resumes a lost response or interrupted copy;
        omitting it intentionally creates another independent revision.
        """
        operation_id = _strict_hex(operation_id, 32, "operation_id")
        expected_bundle_sha256 = _strict_hex(expected_bundle_sha256, 64, "expected_bundle_sha256")
        if (not isinstance(revision_key, str) or "\x00" in revision_key
                or len(revision_key.encode("utf-8")) > 256):
            raise ResourceExportError("revision_key must be bounded text without NUL")
        source_state, source_dir, source_bundle = self._validated_capture(operation_id)
        if source_state["capture"]["manifestSha256"] != expected_bundle_sha256:
            raise ResourceExportError("expected_bundle_sha256 does not match the source capture")
        request = source_state["request"]
        if _json_sha256(request) != source_state["requestSha256"]:
            raise ResourceExportError("source capture request changed after acceptance")
        for field, expected in (("ticketId", operation_id),
                ("requestSha256", source_state["requestSha256"]),
                *((field, request[field]) for field in
                  ("captureSessionId", "eventId", "instance", "view"))):
            if source_bundle.get(field) != expected:
                raise ResourceExportError("source capture %s identity changed" % field)
        if request.get("captureId") and source_bundle.get("captureId") != request["captureId"]:
            raise ResourceExportError("source capture captureId identity changed")
        provenance = {
            "schema": "bread-resource-export-capture-reuse/v1",
            "sourceOperationId": operation_id,
            "sourceBundleSha256": expected_bundle_sha256,
            "sourceRequestSha256": source_state["requestSha256"],
            "revisionKeySha256": hashlib.sha256(revision_key.encode("utf-8")).hexdigest() if revision_key else "",
            "replayPerformed": False,
        }
        new_id = (_json_sha256(provenance)[:32] if revision_key else uuid.uuid4().hex)
        if new_id == operation_id:
            raise ResourceExportError("a capture revision must use a new operation identity")
        prepared = self.prepare_ticket(
            operation_id=new_id, event_id=request["eventId"], instance=request["instance"],
            view=request["view"], asset_name=request["assetName"],
            capture_session_id=request["captureSessionId"], capture_id=request.get("captureId", ""),
            api=request.get("api", ""), texture_formats=request.get("textureFormats", ("auto",)),
            capture_source_identity=request.get("captureSource"))
        state = prepared["operation"]
        prior = state.get("captureReuse")
        if prior is not None and prior != provenance:
            raise ResourceExportError("revision operation is bound to different source evidence")
        if prepared["idempotent"] and prior is None:
            raise ResourceExportError("revision identity belongs to an unrelated operation")
        if prior is None:
            state["captureReuse"] = provenance
            state["updatedUnixMs"] = int(time.time() * 1000)
            _atomic_write_json(self._state_path(new_id), state)

        # Reuse the original physical selection and all resource payloads. The
        # new manifest explicitly identifies the Host copy and its parent;
        # inherited replayRestoration describes the original readback only.
        bundle = json.loads(_canonical_json_bytes(source_bundle).decode("utf-8"))
        bundle.update(ticketId=new_id, requestSha256=state["requestSha256"], captureReuse=provenance)
        source_origin = source_bundle.get("captureOrigin")
        bundle["captureOrigin"] = source_origin if isinstance(source_origin, dict) else {
            "operationId": operation_id, "bundleSha256": expected_bundle_sha256,
            "requestSha256": source_state["requestSha256"],
            "captureId": source_bundle.get("captureId", request.get("captureId", "")),
            "captureSessionId": request["captureSessionId"],
            "eventId": request["eventId"], "instance": request["instance"], "view": request["view"],
        }
        manifest_payload = _canonical_json_bytes(bundle)
        manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()

        def response(current: Mapping[str, Any], idempotent: bool) -> dict[str, Any]:
            return {"schema": "bread-resource-export-fork/v1", "schemaVersion": 1,
                "operationId": new_id, "operation": current,
                "bundleSha256": manifest_sha256, "sourceOperationId": operation_id,
                "sourceBundleSha256": expected_bundle_sha256,
                "needsCapture": False, "replayPerformed": False, "idempotent": idempotent,
                "nextTool": "finalize_resource_export", "captureReuse": provenance}

        if state.get("status") in ("captured", "finalized"):
            current, unused_directory, reused_bundle = self._validated_capture(new_id)
            if (current["capture"]["manifestSha256"] != manifest_sha256
                    or reused_bundle.get("captureReuse") != provenance):
                raise ResourceExportError("existing revision capture changed")
            return response(current, True)
        if state.get("status") != "ticketed":
            raise ResourceExportError("revision is not awaiting its verified source copy")
        destination_dir = self._capture_dir(new_id, state)
        manifest_relative = source_state["capture"]["manifestRelativePath"]
        expected_files = {record["path"] for record in source_state["capture"]["files"]}
        expected_files.add(manifest_relative)
        if self._actual_regular_files(destination_dir) - expected_files:
            raise ResourceExportError("uncommitted revision contains files outside its source capture")

        def destination_for(relative: str) -> Path:
            parts = _relative_parts(relative, "revision capture file")
            current = destination_dir
            for part in parts[:-1]:
                current = current / part
                if current.exists():
                    if _is_reparse(current) or not current.is_dir():
                        raise ResourceExportError("revision capture directory is redirected")
                else:
                    current.mkdir()
            destination = current / parts[-1]
            if _is_reparse(destination):
                raise ResourceExportError("revision capture file is redirected")
            if not _within(destination_dir, current.resolve(strict=True)):
                raise ResourceExportError("revision capture file escaped its operation")
            return destination

        for record in source_state["capture"]["files"]:
            source_path = self._capture_file(source_dir, record["path"], "revision source file")
            destination = destination_for(record["path"])
            if destination.exists():
                if (not destination.is_file() or destination.stat().st_size != record["size"]
                        or _file_sha256(destination) != record["sha256"]):
                    raise ResourceExportError("existing revision file differs from its source")
                continue
            # Copy, never link: later external corruption of either operation
            # cannot modify the other's already validated immutable bytes.
            temporary = destination.parent / (".capture-copy-%s.tmp" % uuid.uuid4().hex)
            try:
                shutil.copyfile(source_path, temporary)
                if (temporary.stat().st_size != record["size"]
                        or _file_sha256(temporary) != record["sha256"]):
                    raise ResourceExportError("source capture changed during revision copy")
                if destination.exists():
                    if (not destination.is_file() or _is_reparse(destination)
                            or _file_sha256(destination) != record["sha256"]):
                        raise ResourceExportError("revision destination changed during copy")
                else:
                    os.replace(str(temporary), str(destination))
            finally:
                if temporary.is_file() and not _is_reparse(temporary):
                    temporary.unlink()
        # A source mutation during copying must not publish a seemingly valid
        # child, even if the changed source file happened to be copied earlier.
        verified_source, unused_directory, unused_bundle = self._validated_capture(operation_id)
        if verified_source["capture"]["manifestSha256"] != expected_bundle_sha256:
            raise ResourceExportError("source capture changed before revision publication")
        manifest_path = destination_for(manifest_relative)
        if manifest_path.exists():
            if not manifest_path.is_file() or _file_sha256(manifest_path, _MAX_JSON_BYTES) != manifest_sha256:
                raise ResourceExportError("existing revision manifest differs from its source binding")
        else:
            _atomic_write_bytes(manifest_path, manifest_payload)
        accepted = self.record_capture(new_id, {
            "schema": CAPTURE_RESPONSE_SCHEMA, "schemaVersion": 1,
            "ticketId": new_id, "requestSha256": state["requestSha256"],
            "captureId": bundle.get("captureId", ""), "captureSessionId": request["captureSessionId"],
            "eventId": request["eventId"], "instance": request["instance"], "view": request["view"],
            "status": bundle.get("completion", {}).get("status", source_state["capture"]["status"]),
            "manifestPath": str(manifest_path), "manifestSha256": manifest_sha256,
            "files": bundle["files"],
        })
        return response(accepted, bool(prepared["idempotent"]))

    def _capture_file(self, capture_dir: Path, relative: Any, label: str) -> Path:
        parts = _relative_parts(relative, label)
        path = capture_dir.joinpath(*parts)
        current = capture_dir
        for part in parts[:-1]:
            current = current / part
            if _is_reparse(current) or not current.is_dir():
                raise ResourceExportError("%s has a redirected path component" % label)
        if _is_reparse(path) or not path.is_file():
            raise ResourceExportError("%s is missing or redirected" % label)
        resolved = path.resolve(strict=True)
        if not _within(capture_dir, resolved):
            raise ResourceExportError("%s escaped the capture directory" % label)
        return resolved

    @staticmethod
    def _actual_regular_files(root: Path) -> set[str]:
        result: set[str] = set()
        for path in root.rglob("*"):
            if path.is_dir():
                if _is_reparse(path):
                    raise ResourceExportError("managed artifact contains a redirected directory")
                continue
            if _is_reparse(path) or not path.is_file():
                raise ResourceExportError("managed artifact contains a redirected file")
            resolved = path.resolve(strict=True)
            if not _within(root, resolved):
                raise ResourceExportError("managed artifact escaped its root")
            result.add(path.relative_to(root).as_posix())
        return result

    def adopt_completed_capture(self, operation_id: str) -> dict[str, Any] | None:
        """Adopt a bridge manifest written before a lost/crashed IPC response.

        The completion manifest is the commit marker.  Any other files without
        that marker are an uncertain partial effect and must not be overwritten
        by blindly replaying the capture operation.
        """

        state = self._load_state(operation_id)
        if state.get("status") in ("captured", "finalized"):
            self._validated_capture(operation_id)
            return state
        if state.get("status") != "ticketed":
            raise ResourceExportError("resource-export operation cannot adopt capture evidence")
        capture_dir = self._capture_dir(operation_id, state)
        actual_files = self._actual_regular_files(capture_dir)
        manifest_path = capture_dir / "manifest.json"
        if "manifest.json" not in actual_files:
            if actual_files:
                raise ResourceExportError(
                    "resource-export capture has partial files without a completion manifest"
                )
            return None
        bundle = _read_json(manifest_path)
        response = {
            "schema": CAPTURE_RESPONSE_SCHEMA,
            "schemaVersion": 1,
            "status": (
                bundle.get("completion", {}).get("status", "unknown")
                if isinstance(bundle.get("completion"), Mapping)
                else "unknown"
            ),
            "ticketId": bundle.get("ticketId"),
            "requestSha256": bundle.get("requestSha256"),
            "captureId": bundle.get("captureId", ""),
            "captureSessionId": bundle.get("captureSessionId"),
            "eventId": bundle.get("eventId"),
            "instance": bundle.get("instance"),
            "view": bundle.get("view"),
            "manifestPath": str(manifest_path),
            "manifestSha256": _file_sha256(manifest_path, _MAX_JSON_BYTES),
            "files": bundle.get("files"),
        }
        return self.record_capture(operation_id, response)

    def record_capture(self, operation_id: str, response: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(response, Mapping):
            raise ResourceExportError("bridge capture response must be an object")
        state = self._load_state(operation_id)
        if (
            response.get("schema") not in (CAPTURE_RESPONSE_SCHEMA, BUNDLE_SCHEMA)
            or response.get("schemaVersion") != 1
        ):
            raise ResourceExportError("bridge returned an unsupported resource-export schema")
        request_sha256 = _strict_hex(state.get("requestSha256"), 64, "requestSha256")
        request = state["request"]
        for field, expected in (
            ("ticketId", operation_id),
            ("requestSha256", request_sha256),
            ("captureSessionId", request["captureSessionId"]),
            ("eventId", request["eventId"]),
            ("instance", request["instance"]),
            ("view", request["view"]),
        ):
            if response.get(field) != expected:
                raise ResourceExportError("bridge response %s identity changed" % field)
        expected_capture_id = str(request.get("captureId", "") or "")
        if expected_capture_id and response.get("captureId") != expected_capture_id:
            raise ResourceExportError("bridge response captureId identity changed")
        if state.get("status") in ("captured", "finalized"):
            capture = state.get("capture")
            if not isinstance(capture, dict):
                raise ResourceExportError("captured operation omitted its receipt")
            if response.get("manifestSha256") != capture.get("manifestSha256"):
                raise ResourceExportError("idempotent bridge response changed manifestSha256")
            self._validated_capture(operation_id)
            return state
        if state.get("status") != "ticketed":
            raise ResourceExportError("resource-export operation is not awaiting capture")
        capture_dir = self._capture_dir(operation_id, state)
        raw_manifest_path = response.get("manifestPath")
        if not isinstance(raw_manifest_path, str) or not raw_manifest_path:
            raise ResourceExportError("bridge response omitted manifestPath")
        manifest_path = Path(raw_manifest_path)
        if not manifest_path.is_absolute():
            manifest_path = capture_dir.joinpath(*_relative_parts(raw_manifest_path, "manifestPath"))
        if _is_reparse(manifest_path) or not manifest_path.is_file():
            raise ResourceExportError("bridge manifest is missing or redirected")
        manifest_path = manifest_path.resolve(strict=True)
        if not _within(capture_dir, manifest_path):
            raise ResourceExportError("bridge manifest escaped its ticket directory")
        manifest_sha256 = _strict_hex(response.get("manifestSha256"), 64, "manifestSha256")
        if _file_sha256(manifest_path, _MAX_JSON_BYTES) != manifest_sha256:
            raise ResourceExportError("bridge manifest hash does not match its receipt")
        bundle = _read_json(manifest_path)
        if bundle.get("schema") not in (BUNDLE_SCHEMA, CAPTURE_RESPONSE_SCHEMA):
            raise ResourceExportError("capture manifest schema is unsupported")
        if bundle.get("schemaVersion") != 1:
            raise ResourceExportError("capture manifest version is unsupported")
        for field, expected in (
            ("ticketId", operation_id),
            ("requestSha256", request_sha256),
            ("captureSessionId", request["captureSessionId"]),
            ("eventId", request["eventId"]),
            ("instance", request["instance"]),
            ("view", request["view"]),
        ):
            if bundle.get(field) != expected:
                raise ResourceExportError("capture manifest %s changed" % field)
        if expected_capture_id and bundle.get("captureId") != expected_capture_id:
            raise ResourceExportError("capture manifest captureId changed")
        mesh = bundle.get("mesh")
        if not isinstance(mesh, dict) or not isinstance(mesh.get("attributes"), list):
            raise ResourceExportError("capture manifest omitted mesh attributes")
        if len(mesh["attributes"]) > _MAX_ATTRIBUTES:
            raise ResourceExportError("capture manifest contains too many mesh attributes")
        records = _manifest_file_records(bundle, response)
        total = 0
        for record in records:
            path = self._capture_file(capture_dir, record["path"], "capture bundle file")
            if path.stat().st_size != record["size"]:
                raise ResourceExportError("capture bundle file size changed")
            if _file_sha256(path) != record["sha256"]:
                raise ResourceExportError("capture bundle file hash changed")
            total += record["size"]
            if total > _MAX_TOTAL_SOURCE_BYTES:
                raise ResourceExportError("capture bundle exceeds the total size limit")
        relative_manifest = manifest_path.relative_to(capture_dir).as_posix()
        expected_files = {record["path"] for record in records}
        expected_files.add(relative_manifest)
        if self._actual_regular_files(capture_dir) != expected_files:
            raise ResourceExportError("capture bundle contains untracked or missing files")
        capture_status, completion, diagnostics_summary = (
            _capture_completion_receipt(bundle, response)
        )
        state["capture"] = {
            "manifestRelativePath": relative_manifest,
            "manifestSha256": manifest_sha256,
            "files": records,
            "totalBytes": total,
            "bridgeSchema": str(response.get("schema")),
            "status": capture_status,
            "completion": completion,
            "diagnosticsSummary": diagnostics_summary,
        }
        state["status"] = "captured"
        state["updatedUnixMs"] = int(time.time() * 1000)
        _atomic_write_json(self._state_path(operation_id), state)
        return state

    def _validated_capture(self, operation_id: str) -> tuple[dict[str, Any], Path, dict[str, Any]]:
        state = self._load_state(operation_id)
        if state.get("status") not in ("captured", "finalized"):
            raise ResourceExportError("resource-export capture is not complete")
        receipt = state.get("capture")
        if not isinstance(receipt, dict):
            raise ResourceExportError("resource-export capture receipt is missing")
        capture_dir = self._capture_dir(operation_id, state)
        manifest_path = self._capture_file(capture_dir, receipt.get("manifestRelativePath"), "capture manifest")
        if _file_sha256(manifest_path, _MAX_JSON_BYTES) != receipt.get("manifestSha256"):
            raise ResourceExportError("capture manifest changed after acceptance")
        bundle = _read_json(manifest_path)
        records = receipt.get("files")
        if not isinstance(records, list):
            raise ResourceExportError("capture receipt files are invalid")
        total = 0
        for record in records:
            if not isinstance(record, dict):
                raise ResourceExportError("capture receipt file is invalid")
            path = self._capture_file(capture_dir, record.get("path"), "capture receipt file")
            size = _strict_int(record.get("size"), "capture receipt file size", 0, _MAX_SOURCE_FILE_BYTES)
            if path.stat().st_size != size or _file_sha256(path) != record.get("sha256"):
                raise ResourceExportError("capture receipt file changed after acceptance")
            total += size
        if total != receipt.get("totalBytes"):
            raise ResourceExportError("capture receipt total changed")
        expected_files = {str(record.get("path", "")) for record in records}
        expected_files.add(str(receipt.get("manifestRelativePath", "")))
        if self._actual_regular_files(capture_dir) != expected_files:
            raise ResourceExportError("capture bundle file set changed after acceptance")
        capture_status, completion, diagnostics_summary = (
            _capture_completion_receipt(
                bundle, {"status": receipt.get("status", "unknown")}
            )
        )
        if (
            receipt.get("status") != capture_status
            or receipt.get("completion") != completion
            or receipt.get("diagnosticsSummary") != diagnostics_summary
        ):
            raise ResourceExportError(
                "capture completion receipt changed after acceptance"
            )
        return state, capture_dir, bundle

    def capture_payload(self, operation_id: str) -> dict[str, Any]:
        """Read the captured files directly, without translation/publication gates."""
        state = self._load_state(operation_id)
        directory = self._capture_dir(operation_id, state)
        receipt = state.get("capture") or {}
        manifest = self._capture_file(directory, receipt.get("manifestRelativePath"), "capture manifest")
        bundle = _read_json(manifest)
        diagnostics = []
        if _file_sha256(manifest, _MAX_JSON_BYTES) != receipt.get("manifestSha256"):
            diagnostics.append({"code": "capture-manifest-changed", "expectedSha256": receipt.get("manifestSha256")})
        return {"schema": "bread-resource-export-payload/v1", "operationId": operation_id,
            "eventId": bundle["eventId"], "captureSessionId": bundle.get("captureSessionId"),
            "captureSource": state["request"].get("captureSource"),
            "captureDirectory": str(directory), "bundlePath": str(directory / state["capture"]["manifestRelativePath"]),
            "bundleSha256": state["capture"]["manifestSha256"], "bundle": bundle,
            "dataOnly": True, "requiresFinalization": False, "requiresShaderValidation": False,
            "diagnostics": diagnostics, "fileValidation": "not-rerun; recorded hashes supplied for selective checks",
            "nextActions": ["Read manifested model, vertex/index bytes, textures, CBuffers, Shader/reflection and render state",
                "Use these files directly for Unity adaptation if a convenience exporter/importer is unavailable",
                "Retain missing data/check diagnostics and judge the resulting Unity execution/images"]}

    def transfer_spec_template(self, operation_id: str, constant_buffer_offset: int = 0,
                               constant_buffer_limit: int = 128) -> dict[str, Any]:
        """A fillable specification built from this capture's own streams.

        The Agent used to discover the shape of ``transfer_spec`` by triggering
        errors, one field per attempt, because the only description of it lived in a
        response too large to read. This is that description: small, concrete, and
        made of the attributes actually captured, so the first finalize can succeed.
        """

        state, capture_dir, bundle = self._validated_capture(operation_id)
        _strict_int(constant_buffer_offset, "constant_buffer_offset", 0, 65536)
        _strict_int(constant_buffer_limit, "constant_buffer_limit", 1, 256)
        cbuffer_transfer = self._resolve_constant_buffers(bundle, {}, capture_dir)
        cbuffer_fields = cbuffer_transfer["fields"]
        cbuffer_end = min(len(cbuffer_fields), constant_buffer_offset + constant_buffer_limit)
        mesh = bundle.get("mesh") if isinstance(bundle, Mapping) else None
        channels: list[dict[str, Any]] = []
        if isinstance(mesh, Mapping):
            for index, attribute in enumerate(mesh.get("attributes") or []):
                if not isinstance(attribute, Mapping):
                    continue
                canonical = attribute.get("canonical")
                decodable = isinstance(canonical, Mapping) and str(
                    canonical.get("status", "")).lower() not in (
                        "unsupported", "error", "unavailable", "missing")
                channels.append({
                    "attributeIndex": index,
                    "name": attribute.get("name"),
                    "semantic": attribute.get("semantic"),
                    "location": attribute.get("location"),
                    "semanticIndex": attribute.get("semanticIndex"),
                    "vertexBuffer": attribute.get("vertexBuffer"),
                    "byteOffset": attribute.get("byteOffset"),
                    "byteStride": attribute.get("byteStride"),
                    "sourceFormat": attribute.get("sourceFormat"),
                    "buffer": attribute.get("buffer"),
                    "format": (attribute.get("sourceFormat") or {}).get("name")
                    if isinstance(attribute.get("sourceFormat"), Mapping) else None,
                    "components": (attribute.get("sourceFormat") or {}).get("compCount")
                    if isinstance(attribute.get("sourceFormat"), Mapping) else None,
                    "perInstance": attribute.get("perInstance"),
                    "usedByShader": attribute.get("used"),
                    "exportable": decodable,
                })
        example = {
            "targets": list(DEFAULT_EXPORT_TARGETS),
            "attributeMappings": [
                {"source": {"attributeIndex": 0}, "target": "POSITION"},
                {"source": {"attributeIndex": 1}, "target": "NORMAL"},
            ],
            "coordinateSystem": {"policy": "preserve"},
            "uvPolicy": "preserve",
            "material": {"name": "BreadMaterial", "alphaMode": "OPAQUE"},
            "textureMappings": [{"sourceResourceId": 0, "role": "baseColor", "colorSpace": "sRGB"}],
        }
        return {
            "schema": "replaylab-resource-export-spec-template/v1",
            "schemaVersion": 1,
            "operationId": operation_id,
            "bundleSha256": state["capture"]["manifestSha256"],
            "vertexChannels": channels,
            "constantBufferFields": cbuffer_fields[constant_buffer_offset:cbuffer_end],
            "constantBufferFieldCount": len(cbuffer_fields),
            "constantBufferNextOffset": cbuffer_end if cbuffer_end < len(cbuffer_fields) else None,
            "constantBufferCoverage": cbuffer_transfer["coverage"],
            "constantBufferMappingPolicy": (
                "Use source {physicalId, componentOffsets, type}, or a unique fieldId/name. "
                "Omit value to read the captured bytes. Supplied values must encode those exact "
                "scalar values. Cover every used physical lane as material or system; unresolved "
                "values remain gaps. shader.organizedSources enables current semantic lane names."),
            "howToDecide": (
                "Read the vertex shader for this draw. Name POSITION, NORMAL and TANGENT "
                "explicitly; leave every other channel unmapped and it is carried through in "
                "capture slot order as _ATTR<attributeIndex>. Custom channels need an explicit "
                "Unity vertexChannels mapping and matching Shader input; a glTF attribute name "
                "does not bind a Unity Shader automatically."
            ),
            "targetsAvailable": ["gltf", "unity-urp", "blender", "fbx"],
            "example": example,
            "rules": [
                "every attributeMappings entry needs a source object and a target semantic",
                "source identifies exactly one captured stream: {\"attributeIndex\": n} is the "
                "least ambiguous form; {\"name\": ...} and {\"location\": n} also work",
                "target is a standard glTF semantic (POSITION, NORMAL, TANGENT, TEXCOORD_n, "
                "COLOR_n, JOINTS_n, WEIGHTS_n) or a _CUSTOM name",
                "JOINTS_n and WEIGHTS_n are retained as _CAPTURE_JOINTS_n/_CAPTURE_WEIGHTS_n "
                "Shader inputs with requestedSemantic preserved. A captured draw does not establish "
                "a skeleton, bind poses or animation clips; no glTF skin is invented. Map these "
                "channels explicitly to Unity inputs and use the captured skinning Shader/buffers.",
                "textureMappings entries need sourceResourceId from the captured bindings",
                "Unity texture colorSpace must follow the selected binding view and Shader decode; "
                "resource format or target project color space alone does not prove source sampling",
                "optional texture uvTransform has scale:[u,v], offset:[u,v], and "
                "space:captured-uv or exported-uv; Unity accounts for an already baked uvPolicy, "
                "and never infers a texture flip from the graphics API",
                "preserve captured sampler and mip evidence; unsupported border, comparison, "
                "LOD, array, or mip behavior remains a restoration gap",
                "a rejected finalize keeps the capture: correct the specification and call "
                "finalize_resource_export again with the same operation_id",
            ],
        }

    def _transfer_spec_problems(self, value: Mapping[str, Any]) -> list[str]:
        """Every structural problem in one pass.

        The normaliser fails on the first problem, which used to cost one whole
        capture-and-finalize round trip per mistake. This lists them together so a
        rejected specification can be corrected in a single next attempt.
        """

        problems: list[str] = []
        if not isinstance(value, Mapping):
            return ["transfer_spec must be an object"]
        targets = value.get("targets", ["gltf"])
        if not isinstance(targets, (list, tuple)) or not targets:
            problems.append("targets must be a non-empty list, for example [\"gltf\"]")
        else:
            for target in targets:
                if target == "obj":
                    problems.append(
                        "obj is not produced: it loses skinning, vertex colour and extra UV sets. "
                        "Use fbx or the canonical gltf")
                elif target not in ("gltf", "unity-urp", "blender", "fbx"):
                    problems.append("unsupported target %r" % (target,))
        mappings = value.get("attributeMappings", [])
        if not isinstance(mappings, list):
            problems.append("attributeMappings must be a list")
        else:
            seen: set[str] = set()
            for position, mapping in enumerate(mappings):
                label = "attributeMappings[%d]" % position
                if not isinstance(mapping, Mapping):
                    problems.append("%s must be an object" % label)
                    continue
                source = mapping.get("source")
                if not isinstance(source, Mapping) or not source:
                    problems.append(
                        '%s needs a source object identifying one captured stream, for example '
                        '{"name": "POSITION0"} or {"location": 0}' % label)
                target = mapping.get("target")
                if not isinstance(target, str) or not target:
                    problems.append("%s needs a target semantic (POSITION, NORMAL, TEXCOORD_0, ...)" % label)
                    continue
                upper = target.upper()
                if not re.fullmatch(
                        r"(?:POSITION|NORMAL|TANGENT|TEXCOORD_[0-9]+|COLOR_[0-9]+|JOINTS_[0-9]+|WEIGHTS_[0-9]+|_[A-Z0-9_]+)",
                        upper):
                    problems.append(
                        "%s target %r must be a standard glTF semantic or a _CUSTOM name" % (label, target))
                elif upper in seen:
                    problems.append("%s repeats the target %s" % (label, upper))
                else:
                    seen.add(upper)
        textures = value.get("textureMappings", [])
        if isinstance(textures, list):
            for position, mapping in enumerate(textures):
                if not isinstance(mapping, Mapping):
                    problems.append("textureMappings[%d] must be an object" % position)
                    continue
                if not mapping.get("sourceResourceId"):
                    problems.append(
                        "textureMappings[%d] needs sourceResourceId from the captured bindings"
                        % position)
        elif textures is not None and not isinstance(textures, list):
            problems.append("textureMappings must be a list")
        return problems

    def _normalize_transfer_spec(self, value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ResourceExportError("transfer_spec must be an object")
        spec = json.loads(_canonical_json_bytes(value).decode("utf-8"))
        if len(_canonical_json_bytes(spec)) > _MAX_JSON_BYTES:
            raise ResourceExportError("transfer_spec exceeds its size limit")
        schema = spec.get("schema")
        if schema not in (None, "", TRANSFER_SCHEMA):
            raise ResourceExportError("transfer_spec schema is unsupported")
        spec["schema"] = TRANSFER_SCHEMA
        spec["schemaVersion"] = 1
        if "assetName" in spec:
            spec["assetName"] = _strict_name(spec["assetName"])
        raw_targets = spec.get("targets", list(DEFAULT_EXPORT_TARGETS))
        if isinstance(raw_targets, str):
            raw_targets = [raw_targets]
        if not isinstance(raw_targets, list) or not raw_targets:
            raise ResourceExportError("transfer_spec targets must be a non-empty list")
        targets: list[str] = []
        for target in raw_targets:
            if target == "obj":
                raise ResourceExportError(
                    "obj is not produced: it cannot carry skinning, vertex colour or extra UV "
                    "sets. Use fbx (converted by a detected Blender) or the canonical gltf")
            if target not in ("gltf", "unity-urp", "blender", "fbx"):
                raise ResourceExportError("unsupported resource-export target %r" % target)
            if target not in targets:
                targets.append(target)
        if "gltf" not in targets:
            targets.insert(0, "gltf")
        spec["targets"] = targets
        coordinate = spec.get("coordinateSystem", {})
        if not isinstance(coordinate, dict):
            raise ResourceExportError("coordinateSystem must be an object")
        policy = str(coordinate.get("policy", "preserve") or "preserve")
        if policy not in ("preserve", "explicit"):
            raise ResourceExportError("coordinateSystem.policy must be preserve or explicit")
        matrix = coordinate.get("matrix", list(_IDENTITY_4X4))
        if policy == "explicit" and "matrix" not in coordinate:
            raise ResourceExportError("explicit coordinate policy requires matrix")
        if not isinstance(matrix, list) or len(matrix) != 16:
            raise ResourceExportError("coordinateSystem.matrix must contain 16 numbers")
        clean_matrix: list[float] = []
        for component in matrix:
            if isinstance(component, bool) or not isinstance(component, (int, float)):
                raise ResourceExportError("coordinateSystem.matrix contains a non-number")
            number = float(component)
            if not math.isfinite(number):
                raise ResourceExportError("coordinateSystem.matrix contains a non-finite number")
            clean_matrix.append(number)
        unit_scale = coordinate.get("unitScale", 1.0)
        if isinstance(unit_scale, bool) or not isinstance(unit_scale, (int, float)):
            raise ResourceExportError("coordinateSystem.unitScale must be a number")
        unit_scale = float(unit_scale)
        if not math.isfinite(unit_scale) or unit_scale <= 0.0 or unit_scale > 1_000_000.0:
            raise ResourceExportError("coordinateSystem.unitScale is invalid")
        if policy == "preserve":
            if any(
                abs(clean_matrix[index] - _IDENTITY_4X4[index]) > 1.0e-12
                for index in range(16)
            ) or abs(unit_scale - 1.0) > 1.0e-12:
                raise ResourceExportError(
                    "preserve coordinate policy requires the identity matrix and unitScale 1"
                )
            clean_matrix = list(_IDENTITY_4X4)
            unit_scale = 1.0
        elif any(
            abs(clean_matrix[index] - expected) > 1.0e-12
            for index, expected in zip((12, 13, 14, 15), (0.0, 0.0, 0.0, 1.0))
        ):
            raise ResourceExportError(
                "explicit coordinateSystem.matrix must be affine with bottom row [0,0,0,1]"
            )
        coordinate["policy"] = policy
        coordinate["matrix"] = clean_matrix
        coordinate["unitScale"] = unit_scale
        coordinate["apiDoesNotImplyObjectBasis"] = True
        spec["coordinateSystem"] = coordinate
        uv_policy = str(spec.get("uvPolicy", "preserve") or "preserve")
        if uv_policy not in ("preserve", "flip-v"):
            raise ResourceExportError("uvPolicy must be preserve or flip-v")
        spec["uvPolicy"] = uv_policy
        mappings = spec.get("attributeMappings", [])
        if not isinstance(mappings, list) or len(mappings) > _MAX_ATTRIBUTES:
            raise ResourceExportError("attributeMappings is invalid")
        explicit_targets: set[str] = set()
        for mapping in mappings:
            if not isinstance(mapping, dict) or not isinstance(mapping.get("source"), dict):
                raise ResourceExportError("attributeMappings entries require source objects")
            source = mapping["source"]
            if "attributeIndex" in source:
                index = _strict_int(source.pop("attributeIndex"), "attributeIndex", 0, _MAX_ATTRIBUTES - 1)
                if "index" in source and source["index"] != index:
                    raise ResourceExportError("attributeIndex conflicts with index")
                source["index"] = index
            if not source or set(source) - {"index", "location", "name", "semantic", "semanticIndex"}:
                raise ResourceExportError("attribute mapping source has no supported unique identity")
            target = mapping.get("target")
            if not isinstance(target, str) or not target:
                raise ResourceExportError("attributeMappings target is required")
            target = target.upper()
            if not re.fullmatch(
                r"(?:POSITION|NORMAL|TANGENT|TEXCOORD_[0-9]+|COLOR_[0-9]+|JOINTS_[0-9]+|WEIGHTS_[0-9]+|_[A-Z0-9_]+)",
                target,
            ):
                raise ResourceExportError(
                    "attributeMappings target must be a standard glTF semantic or _CUSTOM"
                )
            if target in explicit_targets:
                raise ResourceExportError("attributeMappings contains a duplicate target semantic")
            explicit_targets.add(target)
            mapping["target"] = target
        shader = spec.get("shader", {})
        if not isinstance(shader, dict):
            raise ResourceExportError("shader must be an object")
        strategy = str(shader.get("strategy", "evidence-only") or "evidence-only")
        if strategy not in ("target-native-pbr", "generated-forward", "separate-pass", "evidence-only"):
            raise ResourceExportError("shader.strategy is unsupported")
        shader["strategy"] = strategy
        generated = shader.get("unityShaderSource")
        if generated is not None and (
            not isinstance(generated, str)
            or "\x00" in generated
            or len(generated.encode("utf-8")) > _MAX_TRANSFER_SPEC_BYTES
        ):
            raise ResourceExportError("shader.unityShaderSource is invalid")
        provenance = shader.get("provenance", {})
        if not isinstance(provenance, dict):
            raise ResourceExportError("shader.provenance must be an object")
        if isinstance(generated, str) and generated.strip() and not provenance:
            raise ResourceExportError("generated Unity Shader source requires provenance")
        if isinstance(generated, str) and generated.strip():
            stage = provenance.get("stage")
            if not isinstance(stage, str) or not stage.strip() or "\x00" in stage:
                raise ResourceExportError(
                    "generated Unity Shader provenance requires captured stage"
                )
            provenance["stage"] = stage.strip().lower()
            provenance["rawSha256"] = _strict_hex(
                provenance.get("rawSha256"),
                64,
                "shader provenance rawSha256",
            )
            provenance["evidenceSha256"] = _strict_hex(
                provenance.get("evidenceSha256"),
                64,
                "shader provenance evidenceSha256",
            )
        shader["provenance"] = provenance
        spec["shader"] = shader
        material = spec.get("material", {})
        if not isinstance(material, dict):
            raise ResourceExportError("material must be an object")
        alpha = str(material.get("alphaMode", "OPAQUE") or "OPAQUE").upper()
        if alpha not in ("OPAQUE", "MASK", "BLEND"):
            raise ResourceExportError("material.alphaMode is invalid")
        material["alphaMode"] = alpha
        double_sided = material.get("doubleSided", False)
        if not isinstance(double_sided, bool):
            raise ResourceExportError("material.doubleSided must be a boolean")
        material["doubleSided"] = double_sided
        base_color = material.get("baseColorFactor")
        if base_color is not None:
            if not isinstance(base_color, list) or len(base_color) != 4:
                raise ResourceExportError(
                    "material.baseColorFactor must contain four values"
                )
            clean_color: list[float] = []
            for value in base_color:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ResourceExportError(
                        "material.baseColorFactor values must be numbers"
                    )
                number = float(value)
                if not math.isfinite(number) or not 0.0 <= number <= 1.0:
                    raise ResourceExportError(
                        "material.baseColorFactor values must be in [0,1]"
                    )
                clean_color.append(number)
            material["baseColorFactor"] = clean_color
        for field in ("metallicFactor", "roughnessFactor", "alphaCutoff"):
            if field not in material:
                continue
            value = material[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ResourceExportError("material.%s must be a number" % field)
            number = float(value)
            if not math.isfinite(number) or not 0.0 <= number <= 1.0:
                raise ResourceExportError(
                    "material.%s must be in [0,1]" % field
                )
            material[field] = number
        spec["material"] = material
        textures = spec.get("textures", [])
        if not isinstance(textures, list) or len(textures) > 256:
            raise ResourceExportError("textures must be a bounded list")
        for item in textures:
            if not isinstance(item, dict) or "sourceResourceId" not in item:
                raise ResourceExportError("texture mappings require sourceResourceId")
            color_space = str(item.get("colorSpace", "") or "")
            aliases = {"srgb": "sRGB", "linear": "linear", "raw": "raw"}
            normalized_color_space = aliases.get(color_space.lower())
            if normalized_color_space is None:
                raise ResourceExportError("texture colorSpace must be sRGB, linear or raw")
            item["colorSpace"] = normalized_color_space
            sampler_policy = str(item.get("samplerPolicy", "preserve-when-representable") or "preserve-when-representable")
            if sampler_policy not in ("preserve-when-representable", "target-default", "explicit"):
                raise ResourceExportError("texture samplerPolicy is invalid")
            item["samplerPolicy"] = sampler_policy
            source_use = item.get("sourceUse")
            if source_use is not None:
                if not isinstance(source_use, dict) or not source_use:
                    raise ResourceExportError(
                        "texture sourceUse must be a non-empty binding selector"
                    )
                if len(_canonical_json_bytes(source_use)) > 8 * 1024:
                    raise ResourceExportError(
                        "texture sourceUse selector exceeds its size limit"
                    )
        spec["textures"] = textures
        constant_buffers = spec.get("constantBuffers", [])
        if not isinstance(constant_buffers, list) or len(constant_buffers) > 1024:
            raise ResourceExportError("constantBuffers must be a bounded list")
        for item in constant_buffers:
            if not isinstance(item, dict) or not isinstance(item.get("source"), (str, dict)):
                raise ResourceExportError("constant-buffer mappings require source")
            classification = str(item.get("classification", "") or "")
            if classification not in ("material", "system", "unknown"):
                raise ResourceExportError(
                    "constant-buffer classification must be material, system or unknown"
                )
            if item.get("binding") is not None and (item["binding"] != "raw-buffer" or classification != "material"):
                raise ResourceExportError("constant-buffer binding supports raw-buffer only for material data")
            target = item.get("target")
            if classification in ("material", "system") and (
                not isinstance(target, str) or not target or "\x00" in target
            ):
                raise ResourceExportError("constant-buffer target is required")
            if classification == "unknown":
                if target is not None and (
                    not isinstance(target, str) or "\x00" in target
                ):
                    raise ResourceExportError(
                        "unknown constant-buffer target must be a sidecar identifier"
                    )
                item["target"] = str(target or "")
                item["disposition"] = "sidecar-only"
        spec["constantBuffers"] = constant_buffers
        unity = spec.get("unity", {})
        if not isinstance(unity, dict):
            raise ResourceExportError("unity must be an object")
        assets = unity.get("assets", [])
        if not isinstance(assets, list) or len(assets) > 256:
            raise ResourceExportError("unity.assets must be a bounded list")
        paths = {"adapter.json", "readme.md", "shaders/breadexport.shader"}
        extensions = {"shader": {".shader"}, "include": {".hlsl", ".cginc"},
                      "compute": {".compute"}, "script": {".cs", ".asmdef", ".asmref"},
                      "data": {".json", ".bytes", ".txt"}}
        for asset in assets:
            if not isinstance(asset, dict):
                raise ResourceExportError("unity.assets entries must be objects")
            relative = "/".join(_relative_parts(asset.get("path"), "Unity asset path"))
            key = relative.casefold()
            kind = asset.get("kind")
            if (key in paths or key.startswith("capturesources/") or
                    kind not in extensions or Path(relative).suffix.lower() not in extensions[kind]):
                raise ResourceExportError("Unity asset path collides or its extension does not match kind")
            paths.add(key)
            asset["path"] = relative
            payload = self._unity_asset_bytes(asset)
            asset["sha256"] = hashlib.sha256(payload).hexdigest()
        unity["assets"] = assets
        spec["unity"] = unity
        return spec

    @staticmethod
    def _unity_asset_bytes(asset: Mapping[str, Any]) -> bytes:
        has_content, has_path = "content" in asset, "sourcePath" in asset
        if has_content == has_path:
            raise ResourceExportError("Unity asset requires exactly one of content or sourcePath")
        if has_content:
            content = asset["content"]
            if not isinstance(content, str) or "\x00" in content:
                raise ResourceExportError("Unity asset content must be text without NUL")
            payload = content.encode("utf-8")
        else:
            path = Path(str(asset["sourcePath"]))
            if not path.is_absolute() or not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
                raise ResourceExportError("Unity asset source must be a bounded absolute file")
            if any(_is_reparse(part) for part in (path, *path.parents)):
                raise ResourceExportError("Unity asset source is redirected")
            _strict_hex(asset.get("sha256"), 64, "Unity asset source sha256")
            payload = path.read_bytes()
        if not payload or len(payload) > _MAX_JSON_BYTES:
            raise ResourceExportError("Unity asset is empty or exceeds its size limit")
        if asset.get("sha256") and hashlib.sha256(payload).hexdigest() != asset["sha256"]:
            raise ResourceExportError("Unity asset source hash changed")
        return payload

    def _cbuffer_source_identities(
        self, bundle: Mapping[str, Any], capture_dir: Path
    ) -> set[str]:
        blocks: list[Any] = []
        for key in ("constantBuffers", "cbuffers"):
            value = bundle.get(key)
            if isinstance(value, list):
                blocks.extend(value)
        shaders = bundle.get("shaders")
        if isinstance(shaders, list):
            for shader in shaders:
                if not isinstance(shader, dict):
                    continue
                for key in ("constantBuffers", "cbuffers"):
                    value = shader.get(key)
                    if isinstance(value, list):
                        blocks.extend(value)
                evidence_path = shader.get("evidencePath")
                evidence_sha256 = shader.get("evidenceSha256")
                if isinstance(evidence_path, str) and evidence_path:
                    path = self._capture_file(
                        capture_dir, evidence_path, "Shader evidence for CBuffer mapping"
                    )
                    evidence_sha256 = _strict_hex(
                        evidence_sha256, 64, "Shader evidence sha256"
                    )
                    if _file_sha256(path, _MAX_JSON_BYTES) != evidence_sha256:
                        raise ResourceExportError("Shader evidence hash changed before CBuffer mapping")
                    evidence = _read_json(path)
                    value = evidence.get("constantBuffers")
                    if isinstance(value, list):
                        stage = str(shader.get("stage", "") or "")
                        for block in value:
                            if isinstance(block, dict) and stage:
                                tagged = dict(block)
                                tagged["_replaylabStage"] = stage
                                blocks.append(tagged)
                            else:
                                blocks.append(block)
        identities: set[str] = set()

        def walk(value: Any, prefix: str = "") -> None:
            if not isinstance(value, dict):
                return
            name = str(value.get("name", "") or "")
            current = "%s.%s" % (prefix, name) if prefix and name else name or prefix
            for key in ("id", "parameterId", "sourceId", "stableId"):
                candidate = value.get(key)
                if isinstance(candidate, (str, int)) and not isinstance(candidate, bool):
                    identities.add(str(candidate))
            if name:
                identities.add(name)
            if current:
                identities.add(current)
            for key in ("variables", "members", "children"):
                children = value.get(key)
                if isinstance(children, list):
                    for child in children:
                        walk(child, current)

        for block in blocks:
            walk(block)
            if isinstance(block, dict):
                stage = str(
                    block.get("stage", block.get("_replaylabStage", "")) or ""
                )
                if stage:
                    walk(block, stage)
        return identities

    def _validate_transfer_references(
        self,
        bundle: Mapping[str, Any],
        spec: Mapping[str, Any],
        capture_dir: Path,
    ) -> None:
        mesh = bundle.get("mesh")
        attributes = mesh.get("attributes", []) if isinstance(mesh, dict) else []
        for mapping in spec.get("attributeMappings", []):
            source = mapping["source"]
            matches = []
            for attribute in attributes:
                if not isinstance(attribute, dict):
                    continue
                if all(attribute.get(field) == expected for field, expected in source.items()):
                    matches.append(attribute)
            if len(matches) != 1:
                raise ResourceExportError("attribute mapping source must identify exactly one captured input")

        captured_textures: dict[str, list[dict[str, Any]]] = {}
        textures = bundle.get("textures")
        if isinstance(textures, list):
            for texture in textures:
                if not isinstance(texture, dict):
                    continue
                for key in ("resourceId", "resource", "id"):
                    if key in texture and texture[key] not in (None, ""):
                        captured_textures.setdefault(str(texture[key]), []).append(
                            texture
                        )
                        break
        for mapping in spec.get("textures", []):
            resources = captured_textures.get(str(mapping["sourceResourceId"]), [])
            if len(resources) != 1:
                raise ResourceExportError("texture mapping references a resource absent from the capture bundle")
            bindings = resources[0].get("bindings")
            if not isinstance(bindings, list):
                bindings = []
            selector = mapping.get("sourceUse")
            if selector is None:
                if len(bindings) != 1:
                    raise ResourceExportError(
                        "texture mapping must select one binding/use when the captured resource has zero or multiple observed uses"
                    )
            else:
                matches = [
                    binding
                    for binding in bindings
                    if isinstance(binding, dict)
                    and self._mapping_subset(selector, binding)
                ]
                if len(matches) != 1:
                    raise ResourceExportError(
                        "texture sourceUse must identify exactly one captured binding"
                    )

        self._resolve_constant_buffers(bundle, spec, capture_dir)

        shader = spec.get("shader", {})
        generated_source = shader.get("unityShaderSource") if isinstance(
            shader, dict
        ) else None
        if isinstance(generated_source, str) and generated_source.strip():
            provenance = shader["provenance"]
            matches = []
            captured_shaders = bundle.get("shaders")
            if isinstance(captured_shaders, list):
                for record in captured_shaders:
                    if not isinstance(record, dict):
                        continue
                    if (
                        str(record.get("stage", "") or "").lower()
                        == provenance["stage"]
                        and record.get("rawSha256")
                        == provenance["rawSha256"]
                        and record.get("evidenceSha256")
                        == provenance["evidenceSha256"]
                    ):
                        matches.append(record)
            if len(matches) != 1:
                raise ResourceExportError(
                    "generated Shader provenance must identify exactly one captured stage/raw/evidence record"
                )

    @staticmethod
    def _mapping_subset(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
        for key, expected_value in expected.items():
            if key not in actual:
                return False
            actual_value = actual[key]
            if isinstance(expected_value, dict):
                if not isinstance(actual_value, dict) or not ResourceExportManager._mapping_subset(
                    expected_value, actual_value
                ):
                    return False
            elif actual_value != expected_value:
                return False
        return True

    @staticmethod
    def _resolve_constant_buffers(bundle: Mapping[str, Any], spec: Mapping[str, Any],
                                  capture_dir: Path) -> dict[str, Any]:
        try:
            from .cbuffer_transfer import resolve_cbuffer_transfer, CBufferTransferError
        except ImportError:  # Standalone compatibility/test loader.
            from src.cbuffer_transfer import resolve_cbuffer_transfer, CBufferTransferError
        try:
            return resolve_cbuffer_transfer(bundle, spec, capture_dir)
        except CBufferTransferError as error:
            raise ResourceExportError(str(error)) from error

    def _validated_target_candidate(
        self,
        operation_id: str,
        state: Mapping[str, Any],
        intent: Mapping[str, Any],
    ) -> tuple[Path, str]:
        operation_dir = self._operation_dir(operation_id)
        target = operation_dir / "target"
        if _is_reparse(target) or not target.is_dir():
            raise ResourceExportError(
                "published target is missing or redirected during recovery"
            )
        target = target.resolve(strict=True)
        if not _within(operation_dir, target):
            raise ResourceExportError("published target escaped its operation")
        manifest_path = target / "package-manifest.json"
        manifest = _read_json(manifest_path)
        if (
            manifest.get("schema") != FINAL_MANIFEST_SCHEMA
            or manifest.get("schemaVersion") != 1
            or manifest.get("operationId") != operation_id
            or manifest.get("requestSha256") != state.get("requestSha256")
            or manifest.get("captureBundleSha256")
            != intent.get("bundleSha256")
            or manifest.get("finalizeSha256")
            != intent.get("finalizeSha256")
        ):
            raise ResourceExportError(
                "published target does not match its finalize intent"
            )
        provenance = manifest.get("provenance")
        if (
            not isinstance(provenance, dict)
            or provenance.get("transferSpecSha256")
            != intent.get("transferSpecSha256")
        ):
            raise ResourceExportError(
                "published target transfer specification changed"
            )
        records = manifest.get("files")
        if not isinstance(records, list):
            raise ResourceExportError("published target file list is invalid")
        expected_files: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise ResourceExportError("published target file record is invalid")
            relative = "/".join(
                _relative_parts(record.get("path"), "published target file")
            )
            if relative in expected_files:
                raise ResourceExportError(
                    "published target contains a duplicate file record"
                )
            expected_files.add(relative)
            path = target.joinpath(*relative.split("/"))
            if (
                _is_reparse(path)
                or not path.is_file()
                or not _within(target, path.resolve(strict=True))
            ):
                raise ResourceExportError(
                    "published target file escaped or disappeared"
                )
            size = _strict_int(
                record.get("size"),
                "published target file size",
                0,
                _MAX_SOURCE_FILE_BYTES,
            )
            if path.stat().st_size != size or _file_sha256(path) != record.get(
                "sha256"
            ):
                raise ResourceExportError("published target file changed")
        actual_files = self._actual_regular_files(target)
        actual_files.discard("package-manifest.json")
        if actual_files != expected_files:
            raise ResourceExportError("published target file set changed")
        gltf = _read_json(target / "asset.gltf")
        effect = (gltf.get("extras", {}).get("replaylab", {}).get("executionKind") == "effect"
                  and gltf.get("extras", {}).get("replaylab", {}).get("geometryApplicable") is False)
        if gltf.get("asset", {}).get("version") != "2.0" or (not gltf.get("meshes") and not effect):
            raise ResourceExportError("published target glTF is invalid")
        buffers = gltf.get("buffers")
        if not effect and (not isinstance(buffers, list) or not buffers):
            raise ResourceExportError("published target glTF omitted its buffer")
        expected_binary = 0 if effect else buffers[0].get("byteLength")
        if (
            isinstance(expected_binary, bool)
            or not isinstance(expected_binary, int)
            or expected_binary < 0
            or (target / "asset.bin").stat().st_size != expected_binary
        ):
            raise ResourceExportError("published target glTF binary changed")
        return target, _file_sha256(manifest_path, _MAX_JSON_BYTES)

    def _adopt_finalized_target(
        self, operation_id: str, state: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        intent = state.get("finalizeIntent")
        if not isinstance(intent, dict):
            return state, False
        operation_dir = self._operation_dir(operation_id)
        if not (operation_dir / "target").exists():
            return state, False
        for field in ("finalizeSha256", "transferSpecSha256", "bundleSha256"):
            _strict_hex(intent.get(field), 64, "finalize intent %s" % field)
        if (
            intent.get("schema") != FINALIZE_INTENT_SCHEMA
            or intent.get("schemaVersion") != 1
        ):
            raise ResourceExportError("finalize intent schema is invalid")
        unused_target, manifest_sha256 = self._validated_target_candidate(
            operation_id, state, intent
        )
        state["status"] = "finalized"
        state["finalize"] = {
            "finalizeSha256": intent["finalizeSha256"],
            "transferSpecSha256": intent["transferSpecSha256"],
            "bundleSha256": intent["bundleSha256"],
            "targetRelativePath": "target",
            "packageManifestRelativePath": "target/package-manifest.json",
            "packageManifestSha256": manifest_sha256,
            "recoveredPublication": True,
        }
        state["updatedUnixMs"] = int(time.time() * 1000)
        _atomic_write_json(self._state_path(operation_id), state)
        return state, True

    def finalize(self, operation_id: str, expected_bundle_sha256: str, transfer_spec: Mapping[str, Any]) -> dict[str, Any]:
        expected_bundle_sha256 = _strict_hex(expected_bundle_sha256, 64, "expected_bundle_sha256")
        state, capture_dir, bundle = self._validated_capture(operation_id)
        if state["capture"]["manifestSha256"] != expected_bundle_sha256:
            raise ResourceExportError("expected_bundle_sha256 does not match the accepted capture bundle")
        try:
            spec = self._normalize_transfer_spec(transfer_spec)
        except ResourceExportError as error:
            problems = self._transfer_spec_problems(transfer_spec)
            first = str(error)
            if first not in problems:
                problems.insert(0, first)
            if len(problems) > 1:
                raise ResourceExportError(
                    "the transfer specification has %d problems; fix them together and call "
                    "finalize_resource_export again with the same operation (the captured bundle "
                    "is still valid, do not prepare it again): %s"
                    % (len(problems), "; ".join(problems[:12]))
                ) from error
            raise
        if "assetName" not in spec:
            spec["assetName"] = state["request"]["assetName"]
        self._validate_transfer_references(bundle, spec, capture_dir)
        constant_buffer_transfer = self._resolve_constant_buffers(bundle, spec, capture_dir)
        spec["constantBuffers"] = constant_buffer_transfer["mappings"]
        spec["constantBufferCoverage"] = constant_buffer_transfer["coverage"]
        # Expanded naming/layout/review evidence is useful while resolving the
        # CBuffers, but it is not the transfer specification. Keep it losslessly
        # in manifested sidecars rather than forcing the Agent to trim proofs.
        from .unity_shader_sources import compact_source_evidence
        source_evidence = []
        for source in spec.get("shader", {}).get("organizedSources", []):
            payload = _canonical_json_bytes(source)
            relative = "shader-evidence/%s-%s.json" % (source["stage"], hashlib.sha256(payload).hexdigest())
            source_evidence.append((relative, payload))
        if source_evidence:
            spec["shader"]["organizedSources"] = [compact_source_evidence(source, relative, hashlib.sha256(payload).hexdigest())
                for source, (relative, payload) in zip(spec["shader"]["organizedSources"], source_evidence)]
        if len(_canonical_json_bytes(spec)) > _MAX_TRANSFER_SPEC_BYTES:
            raise ResourceExportError("transfer_spec exceeds its size limit after extracting Shader evidence")
        finalize_binding = {
            "operationId": operation_id,
            "requestSha256": state["requestSha256"],
            "bundleSha256": expected_bundle_sha256,
            "transferSpec": spec,
        }
        finalize_sha256 = _json_sha256(finalize_binding)
        if state.get("status") == "finalized":
            existing = state.get("finalize")
            if not isinstance(existing, dict) or existing.get("finalizeSha256") != finalize_sha256:
                raise ResourceExportError("operation is already finalized with a different transfer specification")
            operation_dir = self._operation_dir(operation_id)
            return {
                "operation": state,
                "packagePath": str(operation_dir / "target"),
                "packageManifestPath": str(
                    operation_dir / "target" / "package-manifest.json"
                ),
                "validation": self.validate(operation_id),
                "idempotent": True,
            }

        operation_dir = self._operation_dir(operation_id)
        output_dir = operation_dir / "target"
        intent = {
            "schema": FINALIZE_INTENT_SCHEMA,
            "schemaVersion": 1,
            "finalizeSha256": finalize_sha256,
            "transferSpecSha256": _json_sha256(spec),
            "bundleSha256": expected_bundle_sha256,
        }
        existing_intent = state.get("finalizeIntent")
        if existing_intent is not None and existing_intent != intent:
            if (operation_dir / "target").exists():
                raise ResourceExportError(
                    "operation is already bound to a different finalize intent"
                )
            # The bound attempt produced nothing, so it holds no authority over this
            # operation: a corrected specification may reuse the same capture bundle.
            existing_intent = None
        if existing_intent is None:
            state["finalizeIntent"] = intent
            state["updatedUnixMs"] = int(time.time() * 1000)
            _atomic_write_json(self._state_path(operation_id), state)
        if output_dir.exists():
            state, recovered = self._adopt_finalized_target(operation_id, state)
            if not recovered:
                raise ResourceExportError(
                    "target directory exists without a recoverable finalize receipt"
                )
            return {
                "operation": state,
                "packagePath": str(output_dir),
                "packageManifestPath": str(
                    output_dir / "package-manifest.json"
                ),
                "validation": self.validate(operation_id),
                "idempotent": True,
                "recoveredPublication": True,
            }
        staging = operation_dir / (".target-%s.tmp" % uuid.uuid4().hex)
        staging.mkdir()
        try:
            source_dir = staging / "source"
            source_dir.mkdir()
            manifest_relative = state["capture"]["manifestRelativePath"]
            source_manifest = source_dir / manifest_relative
            source_manifest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(capture_dir / manifest_relative, source_manifest)
            for record in state["capture"]["files"]:
                source = self._capture_file(capture_dir, record["path"], "portable source file")
                destination = source_dir.joinpath(*_relative_parts(record["path"], "portable source path"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)

            gltf_result = self._write_gltf(staging, capture_dir, bundle, expected_bundle_sha256, spec)
            for relative, payload in source_evidence:
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_bytes(destination, payload)
            _atomic_write_json(staging / "cbuffer-transfer.json", constant_buffer_transfer)
            adapter_files: list[str] = []
            if "unity-urp" in spec["targets"]:
                adapter_files.extend(self._write_unity_scaffold(staging, spec, gltf_result))
            if "blender" in spec["targets"]:
                adapter_files.extend(self._write_blender_scaffold(staging, spec, gltf_result))
            if "fbx" in spec["targets"]:
                adapter_files.extend(self._write_fbx(staging, spec))
            _atomic_write_json(staging / "transfer-spec.json", spec)
            package_files = self._package_file_records(staging)
            package_manifest = {
                "schema": FINAL_MANIFEST_SCHEMA,
                "schemaVersion": 1,
                "operationId": operation_id,
                "requestSha256": state["requestSha256"],
                "captureBundleSha256": expected_bundle_sha256,
                "captureSource": state["request"].get("captureSource"),
                "finalizeSha256": finalize_sha256,
                "targets": spec["targets"],
                "gltf": gltf_result,
                "adapterFiles": sorted(adapter_files),
                "files": package_files,
                "provenance": {
                    "mechanicalAuthority": "FixedProgram",
                    "semanticAuthority": "AIOrUserProposal",
                    "transferSpecSha256": _json_sha256(spec),
                    "sourceBundlePreserved": True,
                },
            }
            _atomic_write_json(staging / "package-manifest.json", package_manifest)
            os.replace(str(staging), str(output_dir))
        except Exception:
            if staging.exists() and not _is_reparse(staging):
                shutil.rmtree(staging)
            # Nothing was published, so release the binding: the next attempt corrects
            # the specification against the same capture instead of re-capturing.
            if not (operation_dir / "target").exists() and state.get("finalizeIntent") == intent:
                state["finalizeIntent"] = None
                state["updatedUnixMs"] = int(time.time() * 1000)
                with contextlib.suppress(Exception):
                    _atomic_write_json(self._state_path(operation_id), state)
            raise

        state, recovered = self._adopt_finalized_target(operation_id, state)
        if not recovered:
            raise ResourceExportError(
                "published target could not be adopted into the operation receipt"
            )
        # A normal publication uses the same deterministic adoption gate as a
        # crash recovery, but is not itself reported as a recovered crash.
        state["finalize"]["recoveredPublication"] = False
        _atomic_write_json(self._state_path(operation_id), state)
        return {
            "operation": state,
            "packagePath": str(output_dir),
            "packageManifestPath": str(output_dir / "package-manifest.json"),
            "validation": self.validate(operation_id),
            "idempotent": False,
        }

    def _package_file_records(self, root: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        total = 0
        for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
            if path.is_dir():
                if _is_reparse(path):
                    raise ResourceExportError("package directory is redirected")
                continue
            if _is_reparse(path) or not path.is_file():
                raise ResourceExportError("package file is redirected")
            relative = path.relative_to(root).as_posix()
            if relative == "package-manifest.json":
                continue
            size = path.stat().st_size
            total += size
            if total > _MAX_TOTAL_SOURCE_BYTES:
                raise ResourceExportError("portable package exceeds its size limit")
            records.append({"path": relative, "size": size, "sha256": _file_sha256(path)})
        return records

    def _canonical_blob(self, capture_dir: Path, canonical: Mapping[str, Any], encoding: str) -> tuple[bytes, int, int]:
        status = str(canonical.get("status", "") or "").lower()
        if status in ("unsupported", "error", "unavailable", "missing"):
            raise ResourceExportError("canonical mesh data is unavailable")
        if canonical.get("encoding") != encoding:
            raise ResourceExportError("canonical mesh encoding is unsupported")
        count = _strict_int(canonical.get("count"), "canonical count", 0, _MAX_VERTEX_COUNT)
        components = _strict_int(canonical.get("components"), "canonical components", 1, 4)
        path = self._capture_file(capture_dir, canonical.get("path"), "canonical mesh file")
        expected_size = count * components * 4
        if path.stat().st_size != expected_size:
            raise ResourceExportError("canonical mesh file size does not match its layout")
        sha = _strict_hex(canonical.get("sha256"), 64, "canonical mesh sha256")
        if _file_sha256(path) != sha:
            raise ResourceExportError("canonical mesh hash changed")
        return path.read_bytes(), count, components

    @staticmethod
    def _automatic_target(attribute: Mapping[str, Any]) -> str:
        semantic = str(attribute.get("semantic", "") or attribute.get("name", "") or "ATTRIBUTE").upper()
        semantic = semantic.replace(" ", "")
        index = attribute.get("semanticIndex", 0)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            index = 0
        if semantic in ("POSITION", "SV_POSITION", "POS"):
            return "POSITION"
        if semantic in ("NORMAL", "NORM"):
            return "NORMAL"
        if semantic in ("TANGENT", "TANGENT0"):
            return "TANGENT"
        if semantic.startswith("TEXCOORD") or semantic.startswith("UV"):
            suffix = re.search(r"([0-9]+)$", semantic)
            return "TEXCOORD_%d" % (int(suffix.group(1)) if suffix else index)
        if semantic.startswith("COLOR") or semantic.startswith("COLOUR"):
            suffix = re.search(r"([0-9]+)$", semantic)
            return "COLOR_%d" % (int(suffix.group(1)) if suffix else index)
        custom = _SAFE_ATTRIBUTE.sub("_", semantic).strip("_") or "ATTRIBUTE"
        location = attribute.get("location")
        if isinstance(location, int) and not isinstance(location, bool) and location >= 0:
            custom = "%s_%d" % (custom, location)
        return "_" + custom

    @staticmethod
    def _mapping_for(attribute: Mapping[str, Any], mappings: Sequence[Mapping[str, Any]]) -> str | None:
        for mapping in mappings:
            source = mapping.get("source", {})
            matches = bool(source) and all(
                attribute.get("index" if field == "attributeIndex" else field) == value
                for field, value in source.items())
            if matches:
                return str(mapping["target"])
        return None

    @staticmethod
    def _linear_determinant(matrix: Sequence[float]) -> float:
        a, b, c = matrix[0], matrix[1], matrix[2]
        d, e, f = matrix[4], matrix[5], matrix[6]
        g, h, i = matrix[8], matrix[9], matrix[10]
        return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)

    @staticmethod
    def _inverse_transpose3(matrix: Sequence[float]) -> tuple[float, ...]:
        a, b, c = matrix[0], matrix[1], matrix[2]
        d, e, f = matrix[4], matrix[5], matrix[6]
        g, h, i = matrix[8], matrix[9], matrix[10]
        determinant = ResourceExportManager._linear_determinant(matrix)
        if abs(determinant) < 1.0e-12:
            raise ResourceExportError("coordinate transform is singular")
        inverse = (
            (e * i - f * h) / determinant,
            (c * h - b * i) / determinant,
            (b * f - c * e) / determinant,
            (f * g - d * i) / determinant,
            (a * i - c * g) / determinant,
            (c * d - a * f) / determinant,
            (d * h - e * g) / determinant,
            (b * g - a * h) / determinant,
            (a * e - b * d) / determinant,
        )
        return (
            inverse[0], inverse[3], inverse[6],
            inverse[1], inverse[4], inverse[7],
            inverse[2], inverse[5], inverse[8],
        )

    @staticmethod
    def _normalize3(x: float, y: float, z: float) -> tuple[float, float, float]:
        length = math.sqrt(x * x + y * y + z * z)
        if length <= 1.0e-20:
            return 0.0, 0.0, 0.0
        return x / length, y / length, z / length

    def _transform_attribute(self, payload: bytes, count: int, components: int, target: str, coordinate: Mapping[str, Any]) -> bytes:
        if target not in ("POSITION", "NORMAL", "TANGENT") or components < 3:
            return payload
        matrix = coordinate["matrix"]
        unit = float(coordinate["unitScale"])
        normal_matrix = self._inverse_transpose3(matrix)
        determinant_sign = -1.0 if self._linear_determinant(matrix) < 0.0 else 1.0
        values = list(struct.unpack("<%df" % (count * components), payload))
        for index in range(count):
            offset = index * components
            x, y, z = values[offset], values[offset + 1], values[offset + 2]
            if target == "POSITION":
                values[offset] = unit * (matrix[0] * x + matrix[1] * y + matrix[2] * z + matrix[3])
                values[offset + 1] = unit * (matrix[4] * x + matrix[5] * y + matrix[6] * z + matrix[7])
                values[offset + 2] = unit * (matrix[8] * x + matrix[9] * y + matrix[10] * z + matrix[11])
            elif target == "NORMAL":
                transformed = self._normalize3(
                    normal_matrix[0] * x + normal_matrix[1] * y + normal_matrix[2] * z,
                    normal_matrix[3] * x + normal_matrix[4] * y + normal_matrix[5] * z,
                    normal_matrix[6] * x + normal_matrix[7] * y + normal_matrix[8] * z,
                )
                values[offset:offset + 3] = transformed
            else:
                transformed = self._normalize3(
                    matrix[0] * x + matrix[1] * y + matrix[2] * z,
                    matrix[4] * x + matrix[5] * y + matrix[6] * z,
                    matrix[8] * x + matrix[9] * y + matrix[10] * z,
                )
                values[offset:offset + 3] = transformed
                if components == 4:
                    values[offset + 3] *= determinant_sign
        return struct.pack("<%df" % len(values), *values)

    @staticmethod
    def _flip_tangent_handedness(
        payload: bytes, count: int, components: int, uv_policy: str
    ) -> bytes:
        if uv_policy == "flip-v" and components == 4:
            values = list(struct.unpack("<%df" % (count * components), payload))
            for index in range(count):
                values[index * components + 3] *= -1.0
            return struct.pack("<%df" % len(values), *values)
        return payload

    @staticmethod
    def _transform_uv(payload: bytes, count: int, components: int, policy: str) -> bytes:
        if policy != "flip-v" or components < 2:
            return payload
        values = list(struct.unpack("<%df" % (count * components), payload))
        for index in range(count):
            values[index * components + 1] = 1.0 - values[index * components + 1]
        return struct.pack("<%df" % len(values), *values)

    @staticmethod
    def _topology_mode(value: Any) -> tuple[int, str]:
        compact = re.sub(r"[^a-z]", "", str(value or "").lower())
        mapping = {
            "pointlist": (0, "POINTS"),
            "linelist": (1, "LINES"),
            "linestrip": (3, "LINE_STRIP"),
            "trianglelist": (4, "TRIANGLES"),
            "trianglestrip": (5, "TRIANGLE_STRIP"),
            "trianglefan": (6, "TRIANGLE_FAN"),
        }
        if compact not in mapping:
            raise ResourceExportError("mesh topology %r cannot be represented faithfully in glTF" % value)
        return mapping[compact]

    @staticmethod
    def _append_aligned(buffer: bytearray, payload: bytes) -> tuple[int, int]:
        while len(buffer) % 4:
            buffer.append(0)
        offset = len(buffer)
        buffer.extend(payload)
        return offset, len(payload)

    def _write_gltf(self, output: Path, capture_dir: Path, bundle: Mapping[str, Any], bundle_sha256: str, spec: Mapping[str, Any]) -> dict[str, Any]:
        mesh = bundle.get("mesh")
        if not isinstance(mesh, dict):
            raise ResourceExportError("capture bundle omitted mesh")
        attributes = mesh.get("attributes")
        if mesh.get("source") == "not-applicable-dispatch":
            # glTF permits an empty scene. Do not fabricate a mesh from stale IA
            # state merely to transport a ComputeShader/effect package.
            provenance = {"executionKind": "effect", "geometryApplicable": False,
                          "source": "not-applicable-dispatch", "captureBundleSha256": bundle_sha256,
                          "uvPolicy": spec.get("uvPolicy", "preserve")}
            gltf = {"asset": {"version": "2.0", "generator": "Bread"}, "scene": 0,
                    "scenes": [{"name": str(spec.get("assetName") or "Bread Effect")}],
                    "extras": {"replaylab": provenance}}
            _atomic_write_json(output / "asset.gltf", gltf)
            _atomic_write_bytes(output / "asset.bin", b"")
            return {"path": "asset.gltf", "binaryPath": "asset.bin", "vertexCount": 0, "indexCount": 0,
                    "attributes": [], "vertexChannels": [], "omittedAttributes": [],
                    "geometryApplicable": False, "executionKind": "effect", "extras": gltf["extras"]}
        if not isinstance(attributes, list) or not attributes:
            raise ResourceExportError("capture bundle has no vertex attributes")
        mode, mode_name = self._topology_mode(mesh.get("topology"))
        coordinate = spec["coordinateSystem"]
        determinant = self._linear_determinant(coordinate["matrix"])
        if abs(determinant) < 1.0e-12:
            raise ResourceExportError("coordinate transform is singular")
        binary = bytearray()
        views: list[dict[str, Any]] = []
        accessors: list[dict[str, Any]] = []
        primitive_attributes: dict[str, int] = {}
        omitted: list[dict[str, Any]] = []
        vertex_count: int | None = None
        mappings = spec.get("attributeMappings", [])
        used_targets: set[str] = set()
        # Decode once, then decide. Channel roles are inferred from the data for streams
        # whose semantic carries no meaning (SPIR-V reconstructions name them
        # ``__replaylab_s0_b0_c0_x_f32``), so an anonymous mesh still exports.
        decoded_attributes: list[dict[str, Any]] = []
        for index, attribute in enumerate(attributes):
            if not isinstance(attribute, dict):
                raise ResourceExportError("mesh attribute entry is invalid")
            if attribute.get("perInstance") is True:
                omitted.append({"name": attribute.get("name"), "reason": "glTF mesh primitives do not encode per-instance vertex streams"})
                continue
            canonical = attribute.get("canonical")
            if not isinstance(canonical, dict) or str(canonical.get("status", "")).lower() in ("unsupported", "error", "unavailable", "missing"):
                omitted.append({"name": attribute.get("name"), "reason": "canonical decoding unavailable; raw bytes preserved in source bundle"})
                continue
            payload, count, components = self._canonical_blob(capture_dir, canonical, "float32le")
            decoded_values = struct.unpack("<%df" % (count * components), payload)
            if any(not math.isfinite(value) for value in decoded_values):
                target = self._mapping_for(attribute, mappings) or self._automatic_target(attribute)
                if target == "POSITION":
                    raise ResourceExportError("POSITION contains NaN or infinity; supply a decoded position adapter")
                omitted.append({"name": attribute.get("name"), "attributeIndex": index,
                    "target": target, "reason": "non-finite float view; preserve raw bits for a Shader buffer adapter",
                    "raw": attribute.get("raw"), "canonical": canonical,
                    "shaderInputPolicy": "raw-bits", "adapterRequired": True})
                continue
            decoded_attributes.append({
                "index": index,
                "attribute": attribute,
                "payload": payload,
                "count": count,
                "components": components,
            })
        passthrough_receipt: list[dict[str, Any]] = []
        channel_receipts: list[dict[str, Any]] = []
        forced_targets: dict[int, str] = {}
        assumed_position: dict[str, Any] | None = None
        if not any(str(mapping.get("target", "")).upper() == "POSITION" for mapping in mappings):
            declared = [
                entry for entry in decoded_attributes
                if not self._automatic_target(entry["attribute"]).startswith("_")
                and self._automatic_target(entry["attribute"]) == "POSITION"
            ]
            if not declared:
                # glTF cannot describe a primitive without POSITION. Rather than refuse,
                # take the first stream the vertex shader actually reads that can hold a
                # position, and say so in the receipt: slot order and the shader's own
                # "used" flag, never a statistical guess about the values.
                for entry in decoded_attributes:
                    if entry["components"] < 3 or entry["attribute"].get("used") is False:
                        continue
                    assumed_position = entry
                    forced_targets[entry["index"]] = "POSITION"
                    break
                if assumed_position is None:
                    observed = ", ".join(
                        "%s(%d components)" % (
                            entry["attribute"].get("name") or "attribute %d" % entry["index"],
                            entry["components"])
                        for entry in decoded_attributes[:12]
                    ) or "none"
                    raise ResourceExportError(
                        "glTF needs a POSITION stream and no captured stream has three components. "
                        "Captured per-vertex streams: %s. Read the vertex shader and name the "
                        'position stream: attributeMappings=[{"source": {"attributeIndex": <n>}, '
                        '"target": "POSITION"}].' % observed
                    )
        for entry in decoded_attributes:
            attribute = entry["attribute"]
            payload, count, components = entry["payload"], entry["count"], entry["components"]
            if vertex_count is None:
                vertex_count = count
            elif vertex_count != count:
                raise ResourceExportError("per-vertex canonical attribute counts disagree")
            target = self._mapping_for(attribute, mappings) or forced_targets.get(entry["index"])
            if target is None:
                # Nothing here guesses what a stream means. A named semantic is honoured;
                # anything else is carried through under its own slot so the data survives
                # intact and can be identified later (or by the shader that reads it).
                automatic = self._automatic_target(attribute)
                if automatic.startswith("_"):
                    target = "_ATTR%d" % entry["index"]
                    passthrough_receipt.append({
                        "attributeIndex": entry["index"],
                        "attribute": attribute.get("name"),
                        "location": attribute.get("location"),
                        "vertexBuffer": attribute.get("vertexBuffer"),
                        "components": components,
                        "target": target,
                        "note": ("carried through in capture slot order; no semantic was declared "
                                 "for it and the Host does not invent one"),
                    })
                else:
                    target = automatic
            target = target.upper()
            requested_target = target
            if re.fullmatch(r"(?:JOINTS|WEIGHTS)_[0-9]+", target):
                # glTF JOINTS/WEIGHTS claim a skin and integer accessors. The
                # draw only establishes exact Shader inputs, not a hierarchy
                # or inverse bind poses. Preserve inputs as custom channels;
                # Unity can bind them to the captured skinning implementation.
                target = "_CAPTURE_" + target
                passthrough_receipt.append({"attributeIndex": entry["index"],
                    "attribute": attribute.get("name"), "target": target,
                    "requestedSemantic": requested_target, "runtimeSkeletonCreated": False,
                    "note": "captured skinning Shader input; skeleton and inverse bind poses are not inferred"})
            if not re.fullmatch(r"(?:POSITION|NORMAL|TANGENT|TEXCOORD_[0-9]+|COLOR_[0-9]+|_[A-Z0-9_]+)", target):
                raise ResourceExportError("attribute mapping target %r is not a glTF semantic" % target)
            required_components = {"POSITION": 3, "NORMAL": 3, "TANGENT": 4}.get(target)
            if required_components is not None and components != required_components:
                omitted.append({"name": attribute.get("name"), "target": target, "reason": "component count is incompatible with the standard semantic"})
                target = self._automatic_target({**attribute, "semantic": "CUSTOM_%s" % target})
            if target.startswith("TEXCOORD_") and components != 2:
                if spec["uvPolicy"] != "preserve":
                    raise ResourceExportError("packed TEXCOORD requires an explicit per-component UV adaptation; "
                                              "preserve it and implement the verified swizzle/flip in the target Shader")
                target = self._automatic_target({**attribute, "semantic": "CUSTOM_%s" % target})
            if target.startswith("COLOR_") and components not in (3, 4):
                target = self._automatic_target({**attribute, "semantic": "CUSTOM_%s" % target})
            base_target = target
            suffix = 1
            while target in used_targets:
                target = "_%s_%d" % (base_target.lstrip("_"), suffix)
                suffix += 1
            used_targets.add(target)
            payload = self._transform_attribute(payload, count, components, target, coordinate)
            if target == "TANGENT":
                payload = self._flip_tangent_handedness(
                    payload, count, components, spec["uvPolicy"]
                )
            if target.startswith("TEXCOORD_"):
                payload = self._transform_uv(payload, count, components, spec["uvPolicy"])
            offset, length = self._append_aligned(binary, payload)
            view_index = len(views)
            views.append({"buffer": 0, "byteOffset": offset, "byteLength": length, "target": 34962})
            accessor: dict[str, Any] = {
                "bufferView": view_index,
                "byteOffset": 0,
                "componentType": 5126,
                "count": count,
                "type": ("SCALAR", "VEC2", "VEC3", "VEC4")[components - 1],
            }
            if target == "POSITION" and count:
                unpacked = struct.unpack("<%df" % (count * components), payload)
                accessor["min"] = [min(unpacked[i::components]) for i in range(3)]
                accessor["max"] = [max(unpacked[i::components]) for i in range(3)]
            accessor_index = len(accessors)
            accessors.append(accessor)
            primitive_attributes[target] = accessor_index
            channel_receipts.append({
                "attributeIndex": entry["index"], "gltfSemantic": target,
                "requestedSemantic": requested_target, "components": components,
                "canonicalEncoding": "float32le", "accessorIndex": accessor_index,
                "source": {key: attribute.get(key) for key in (
                    "name", "semantic", "semanticIndex", "location", "vertexBuffer",
                    "byteOffset", "byteStride", "sourceFormat", "buffer", "used",
                    "perInstance", "instanceStepRate", "floatCastWrong")},
                "shaderInputPolicy": "decoded numeric values; preserve component count and explicit target semantic",
            })
        if vertex_count is None or vertex_count <= 0:
            raise ResourceExportError("no canonical per-vertex attribute can be exported")
        if assumed_position is not None:
            passthrough_receipt.append({
                "attributeIndex": assumed_position["index"],
                "attribute": assumed_position["attribute"].get("name"),
                "target": "POSITION",
                "assumed": True,
                "note": ("no stream declared POSITION; the first vertex-shader-read stream with "
                         "three or more components was used. Confirm it against the vertex shader "
                         "and override with attributeMappings when it is wrong"),
            })

        indices_record = mesh.get("indices")
        indexed = bool(mesh.get("indexed", isinstance(indices_record, dict)))
        if isinstance(indices_record, dict) and isinstance(indices_record.get("canonical"), dict):
            index_payload, index_count, index_components = self._canonical_blob(capture_dir, indices_record["canonical"], "uint32le")
            if index_components != 1:
                raise ResourceExportError("canonical index data must be scalar")
            indices = list(struct.unpack("<%dI" % index_count, index_payload))
        elif indexed:
            raise ResourceExportError("indexed draw has no canonical uint32 index stream")
        else:
            indices = list(range(vertex_count))
            index_count = vertex_count
        if any(index >= vertex_count for index in indices):
            if 0xFFFFFFFF in indices:
                raise ResourceExportError(
                    "primitive-restart indices require deterministic primitive splitting before glTF export"
                )
            raise ResourceExportError("canonical index references a vertex outside the exported range")
        winding_flipped = False
        if determinant < 0.0:
            if mode != 4:
                raise ResourceExportError("negative-determinant basis conversion is supported only for TriangleList")
            if len(indices) % 3:
                raise ResourceExportError("triangle-list index count is not divisible by three")
            for offset in range(0, len(indices), 3):
                indices[offset + 1], indices[offset + 2] = indices[offset + 2], indices[offset + 1]
            winding_flipped = True
        index_payload = struct.pack("<%dI" % len(indices), *indices)
        index_offset, index_length = self._append_aligned(binary, index_payload)
        index_view = len(views)
        views.append({"buffer": 0, "byteOffset": index_offset, "byteLength": index_length, "target": 34963})
        index_accessor = len(accessors)
        accessors.append({"bufferView": index_view, "byteOffset": 0, "componentType": 5125, "count": len(indices), "type": "SCALAR"})

        material_spec = spec.get("material", {})
        material: dict[str, Any] = {
            "name": str(material_spec.get("name", "BreadMaterial") or "BreadMaterial")[:128],
            "alphaMode": material_spec.get("alphaMode", "OPAQUE"),
            "doubleSided": bool(material_spec.get("doubleSided", False)),
            "pbrMetallicRoughness": {},
        }
        base_color = material_spec.get("baseColorFactor")
        if isinstance(base_color, list) and len(base_color) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) for v in base_color):
            material["pbrMetallicRoughness"]["baseColorFactor"] = [float(v) for v in base_color]
        for source, target in (("metallicFactor", "metallicFactor"), ("roughnessFactor", "roughnessFactor")):
            value = material_spec.get(source)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                material["pbrMetallicRoughness"][target] = max(0.0, min(1.0, float(value)))
        if material["alphaMode"] == "MASK":
            cutoff = material_spec.get("alphaCutoff", 0.5)
            material["alphaCutoff"] = float(cutoff) if isinstance(cutoff, (int, float)) and not isinstance(cutoff, bool) else 0.5
        asset_name = str(spec.get("assetName", "") or "") or "RenderDocAsset"
        gltf = {
            "asset": {"version": "2.0", "generator": "Bread resource export"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0, "name": asset_name}],
            "meshes": [{"name": asset_name, "primitives": [{"attributes": primitive_attributes, "indices": index_accessor, "material": 0, "mode": mode}]}],
            "materials": [material],
            "buffers": [{"uri": "asset.bin", "byteLength": len(binary)}],
            "bufferViews": views,
            "accessors": accessors,
            "extras": {
                "replaylab": {
                    "captureBundleSha256": bundle_sha256,
                    "topology": mode_name,
                    "coordinatePolicy": coordinate["policy"],
                    "basisMatrix": coordinate["matrix"],
                    "unitScale": coordinate["unitScale"],
                    "uvPolicy": spec["uvPolicy"],
                    "windingFlipped": winding_flipped,
                    "omittedAttributes": omitted,
                    "passthroughAttributes": passthrough_receipt,
                    "vertexChannels": channel_receipts,
                }
            },
        }
        _atomic_write_bytes(output / "asset.bin", bytes(binary))
        _atomic_write_json(output / "asset.gltf", gltf)
        return {
            "path": "asset.gltf",
            "binaryPath": "asset.bin",
            "vertexCount": vertex_count,
            "indexCount": len(indices),
            "attributes": sorted(primitive_attributes),
            "omittedAttributes": omitted,
            "passthroughAttributes": passthrough_receipt,
            "vertexChannels": channel_receipts,
            "topology": mode_name,
            "windingFlipped": winding_flipped,
        }

    def _write_fbx(self, output: Path, spec: Mapping[str, Any]) -> list[str]:
        """Convert the published glTF to FBX with a detected headless Blender.

        There is no complete open FBX writer to embed, and inventing one would be a
        worse answer than driving the converter the user already has. The receipt
        names the binary and its version so the artifact is auditable.
        """

        blender = str(self._environment.get(BLENDER_ENV, "") or "").strip() or shutil.which("blender") or ""
        name = str(spec.get("assetName", "asset") or "asset")
        receipt: dict[str, Any] = {
            "schema": "replaylab-resource-export-fbx-receipt/v1",
            "schemaVersion": 1,
            "source": "asset.gltf",
            "converter": "blender",
        }
        if not blender:
            receipt["status"] = "unavailable"
            receipt["reason"] = ("no Blender was found; set RENDERDOC_ULTRA_BLENDER or install "
                                 "Blender, then finalize again with the fbx target")
            _atomic_write_json(output / "fbx-receipt.json", receipt)
            return ["fbx-receipt.json"]
        script = (
            "import bpy, sys\n"
            "bpy.ops.wm.read_factory_settings(use_empty=True)\n"
            "bpy.ops.import_scene.gltf(filepath=sys.argv[-2])\n"
            "bpy.ops.export_scene.fbx(filepath=sys.argv[-1], add_leaf_bones=False,\n"
            "                         bake_anim=False, path_mode='COPY', embed_textures=False)\n"
        )
        script_path = output / "fbx-convert.py"
        _atomic_write_bytes(script_path, script.encode("utf-8"))
        target = output / ("%s.fbx" % name)
        try:
            completed = subprocess.run(
                [blender, "--background", "--factory-startup", "--python", str(script_path),
                 "--", str(output / "asset.gltf"), str(target)],
                capture_output=True, timeout=_FBX_CONVERSION_TIMEOUT_SECONDS, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as error:
            receipt["status"] = "failed"
            receipt["error"] = str(error)[:400]
            _atomic_write_json(output / "fbx-receipt.json", receipt)
            return ["fbx-receipt.json", "fbx-convert.py"]
        receipt["executable"] = blender
        receipt["exitCode"] = completed.returncode
        receipt["stderrTail"] = completed.stderr.decode("utf-8", "replace")[-2000:]
        if completed.returncode != 0 or not target.is_file():
            receipt["status"] = "failed"
            _atomic_write_json(output / "fbx-receipt.json", receipt)
            return ["fbx-receipt.json", "fbx-convert.py"]
        receipt["status"] = "converted"
        receipt["path"] = "%s.fbx" % name
        receipt["sha256"] = _file_sha256(target, _MAX_SOURCE_FILE_BYTES)
        _atomic_write_json(output / "fbx-receipt.json", receipt)
        return ["%s.fbx" % name, "fbx-receipt.json", "fbx-convert.py"]

    @staticmethod
    def _write_unity_scaffold(output: Path, spec: Mapping[str, Any], gltf: Mapping[str, Any]) -> list[str]:
        root = output / "UnityURP"
        (root / "Editor").mkdir(parents=True)
        (root / "Shaders").mkdir()
        settings = {
            "schema": "replaylab-unity-urp-adapter/v1",
            "schemaVersion": 1,
            "mesh": "../asset.gltf",
            "requiredPackage": "com.bread.renderdoc",
            "importTool": "import_resource_export_to_unity",
            "pipeline": "UniversalRenderPipeline",
            "material": spec.get("material", {}),
            "textures": spec.get("textures", []),
            "constantBuffers": spec.get("constantBuffers", []),
            "shader": spec.get("shader", {}),
            "systemValuesUseTargetDefaults": True,
            "textureImportAuthority": "Use each transfer-spec texture colorSpace (sRGB/linear/raw), exact captured view format, and captured sampler unless the target cannot represent it",
            "constantBufferPolicy": "material entries become material properties; system entries use Unity frame/camera/object globals; unknown entries remain sidecar-only and are not applied",
            "shaderSourceProvenance": spec.get("shader", {}).get("provenance", {}),
            "vertexChannels": gltf.get("extras", {}).get("replaylab", {}).get("vertexChannels", []),
        }
        _atomic_write_json(root / "adapter.json", settings)
        readme = (
            "# Bread Unity URP adapter\n\n"
            "Call `import_resource_export_to_unity` with the export operation ID and Unity "
            "project directory. Bread prepares its pinned Unity MCP and built-in importer, "
            "then imports the canonical mesh, mapped texture images and material parameters. "
            "A supplied Unity Shader is compiled in the target project; URP/Lit is used only "
            "for an explicit `target-native-pbr` strategy. Exact DDS files and view/sampler "
            "evidence remain under `../source/`. Unmapped channels, unsupported images and "
            "missing Shader properties are reported in the import receipt. Camera, object, "
            "view and projection system values must use Unity's current frame values. "
            "The generated preview scene is a starting point; import and compilation do not "
            "prove visual equivalence. The glTF remains available for other importers.\n"
        )
        _atomic_write_bytes(root / "README.md", readme.encode("utf-8"))
        source = spec.get("shader", {}).get("unityShaderSource")
        files = ["UnityURP/adapter.json", "UnityURP/README.md"]
        if isinstance(source, str) and source.strip():
            _atomic_write_bytes(root / "Shaders" / "BreadExport.shader", source.encode("utf-8"))
            files.append("UnityURP/Shaders/BreadExport.shader")
        for asset in spec.get("unity", {}).get("assets", []):
            relative = "/".join(_relative_parts(asset["path"], "Unity asset path"))
            destination = root.joinpath(*relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(destination, ResourceExportManager._unity_asset_bytes(asset))
            files.append("UnityURP/" + relative)
        snapshots = []
        for organized in spec.get("shader", {}).get("organizedSources", []):
            stage = str(organized.get("stage", ""))
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", stage):
                raise ResourceExportError("organized Shader stage is invalid")
            source_path = Path(str(organized.get("sourcePath", "")))
            dependencies = [{"path": str(source_path), "sha256": organized.get("sourceSha256")}]
            dependencies.extend(organized.get("dependencyFiles", []))
            for index, dependency in enumerate(dependencies):
                path = Path(str(dependency.get("path", "")))
                payload = ResourceExportManager._unity_asset_bytes({
                    "sourcePath": str(path), "sha256": dependency.get("sha256")})
                name = path.name if index == 0 else "Dependencies/" + str(dependency["sha256"])[:16] + "_" + path.name
                relative = "CaptureSources/" + stage + "/" + str(organized["sourceSha256"])[:16] + "/" + name
                destination = root.joinpath(*_relative_parts(relative, "captured Shader snapshot"))
                if destination.exists():
                    if destination.read_bytes() == payload:
                        continue
                    raise ResourceExportError("captured Shader snapshots collide")
                destination.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_bytes(destination, payload)
                files.append("UnityURP/" + relative)
                snapshots.append({"stage": stage, "sourcePath": str(path), "packagePath": relative,
                                  "sha256": dependency["sha256"], "kind": "source" if index == 0 else "dependency"})
        if snapshots:
            _atomic_write_json(root / "CaptureSources" / "manifest.json", {"schema": "bread-captured-shader-snapshots/v1", "files": snapshots})
            files.append("UnityURP/CaptureSources/manifest.json")
        return files

    @staticmethod
    def _write_blender_scaffold(output: Path, spec: Mapping[str, Any], gltf: Mapping[str, Any]) -> list[str]:
        root = output / "Blender"
        root.mkdir()
        _atomic_write_json(root / "adapter.json", {
            "schema": "replaylab-blender-adapter/v1",
            "schemaVersion": 1,
            "mesh": "../asset.gltf",
            "material": spec.get("material", {}),
            "textures": spec.get("textures", []),
            "constantBuffers": spec.get("constantBuffers", []),
            "shader": spec.get("shader", {}),
            "systemValuesUseTargetDefaults": True,
            "textureImportAuthority": "Apply each transfer-spec texture colorSpace to the Blender Image Texture node and preserve captured sampler choices when representable",
            "constantBufferPolicy": "material entries become node inputs; system entries use Blender scene/object/camera state; unknown entries remain sidecar-only and are not applied",
            "shaderSourceProvenance": spec.get("shader", {}).get("provenance", {}),
        })
        script = '''# Fixed Bread Blender adapter. Run with Blender 4.x.
import json
from pathlib import Path
import bpy

here = Path(__file__).resolve().parent
package = here.parent
settings = json.loads((here / "adapter.json").read_text(encoding="utf-8"))
bpy.ops.import_scene.gltf(filepath=str((package / "asset.gltf").resolve()))
material_settings = settings.get("material", {})
material = bpy.data.materials.new(str(material_settings.get("name") or "BreadMaterial"))
material.use_nodes = True
principled = next((n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
if principled is not None:
    color = material_settings.get("baseColorFactor")
    if isinstance(color, list) and len(color) == 4:
        principled.inputs["Base Color"].default_value = tuple(float(v) for v in color)
    if "metallicFactor" in material_settings:
        principled.inputs["Metallic"].default_value = float(material_settings["metallicFactor"])
    if "roughnessFactor" in material_settings:
        principled.inputs["Roughness"].default_value = float(material_settings["roughnessFactor"])
for obj in bpy.context.selected_objects:
    if getattr(obj.data, "materials", None) is not None:
        obj.data.materials.append(material)
print("Bread import complete; review adapter.json texture/CBuffer decisions before visual acceptance.")
'''
        _atomic_write_bytes(root / "import_resource.py", script.encode("utf-8"))
        readme = (
            "# Bread Blender adapter\n\n"
            "Open Blender 4.x and run `import_resource.py`. The fixed script imports the glTF and "
            "creates a Principled BSDF baseline. Review texture color-space/sampler choices and "
            "shader-specific node decisions recorded in `adapter.json`; captured camera matrices "
            "are evidence, not persistent Blender scene values.\n"
        )
        _atomic_write_bytes(root / "README.md", readme.encode("utf-8"))
        return ["Blender/adapter.json", "Blender/import_resource.py", "Blender/README.md"]

    @staticmethod
    def _process_passed(process: Mapping[str, Any]) -> bool:
        return (
            process.get("launched") is True
            and process.get("exitCode") == 0
            and process.get("timedOut") is False
            and process.get("logLimitExceeded") is False
            and process.get("launchFailed") is False
            and process.get("providerChanged") is False
        )

    def _run_gltf_target_validator(
        self,
        request: Mapping[str, Any],
        executable: Path | None,
        validation_root: Path,
    ) -> dict[str, Any]:
        provider = request["providers"]["externalGltfValidator"]
        if executable is None or provider.get("status") != "available":
            return {
                "status": "unavailable",
                "requested": True,
                "provider": provider,
                "reason": provider.get("reason", "Khronos glTF Validator is unavailable"),
            }
        root = validation_root / "gltf"
        target = self._operation_dir(str(request["operationId"])) / "target"
        process = self._run_process(
            provider=provider,
            executable=executable,
            arguments=(
                str(executable),
                "--stdout",
                "--validate-resources",
                "--no-write-timestamp",
                "--no-absolute-path",
                str(target / "asset.gltf"),
            ),
            validation_root=validation_root,
            provider_root=root,
            timeout_seconds=_GLTF_VALIDATOR_TIMEOUT_SECONDS,
            stdout_name="report.json",
            stderr_name="validator.log",
        )
        status = "failed"
        reason = "Khronos glTF Validator did not produce a passing report"
        summary: dict[str, Any] = {}
        try:
            report = _read_json(root / "report.json", _MAX_PROVIDER_LOG_BYTES)
            issues = report.get("issues")
            if not isinstance(issues, dict):
                raise ResourceExportError("Khronos report omitted issues")
            counts: dict[str, int] = {}
            for key in ("numErrors", "numWarnings", "numInfos", "numHints"):
                value = issues.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ResourceExportError("Khronos report issue count is invalid")
                counts[key] = value
            truncated = issues.get("truncated")
            if not isinstance(truncated, bool):
                raise ResourceExportError("Khronos report truncated flag is invalid")
            info = report.get("info")
            version = ""
            if isinstance(info, dict):
                version = str(
                    info.get("version", info.get("validatorVersion", "")) or ""
                )[:128]
            summary = {
                **counts,
                "truncated": truncated,
                "validatorVersion": version,
                "report": self._managed_file_record(
                    validation_root, root / "report.json"
                ),
            }
            if (
                self._process_passed(process)
                and counts["numErrors"] == 0
                and truncated is False
            ):
                status = "passed"
                reason = ""
            elif counts["numErrors"]:
                reason = "Khronos glTF Validator reported errors"
            elif truncated:
                reason = "Khronos glTF Validator report was truncated"
        except (OSError, ResourceExportError, ValueError):
            reason = "Khronos glTF Validator report failed strict validation"
        layer = {
            "status": status,
            "requested": True,
            "provider": provider,
            "process": process,
            "report": summary,
        }
        if reason:
            layer["reason"] = reason
        return layer

    @staticmethod
    def _blender_validation_script() -> str:
        return r'''import hashlib
import json
import sys
from pathlib import Path

import bpy


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


separator = sys.argv.index("--")
request_path = Path(sys.argv[separator + 1]).resolve(strict=True)
request = json.loads(request_path.read_text(encoding="utf-8"))
provider_root = request_path.parent
package_root = Path(request["packageRoot"]).resolve(strict=True)
artifact_root = provider_root / request["artifactRoot"]
receipt_path = provider_root / request["receiptPath"]
artifact_root.mkdir(parents=True, exist_ok=False)
checks = {
    "gltfImport": False,
    "meshIntegrity": False,
    "materialNodeConstruction": False,
    "imageColorSpaces": False,
    "sceneLinkIntegrity": False,
}
errors = []
artifacts = []
try:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    imported = bpy.ops.import_scene.gltf(filepath=str(package_root / "asset.gltf"))
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    checks["gltfImport"] = bool(meshes) and "FINISHED" in set(imported)
    checks["meshIntegrity"] = bool(meshes) and all(
        len(obj.data.vertices) > 0 for obj in meshes
    )
    settings = json.loads(
        (package_root / "Blender" / "adapter.json").read_text(encoding="utf-8")
    )
    material_settings = settings.get("material", {})
    material = bpy.data.materials.new(
        str(material_settings.get("name") or "BreadMaterial")
    )
    material.use_nodes = True
    principled = next(
        (node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"),
        None,
    )
    if principled is not None:
        color = material_settings.get("baseColorFactor")
        if isinstance(color, list) and len(color) == 4:
            principled.inputs["Base Color"].default_value = tuple(float(v) for v in color)
        if "metallicFactor" in material_settings:
            principled.inputs["Metallic"].default_value = float(
                material_settings["metallicFactor"]
            )
        if "roughnessFactor" in material_settings:
            principled.inputs["Roughness"].default_value = float(
                material_settings["roughnessFactor"]
            )
    for obj in meshes:
        obj.data.materials.append(material)
    material_cbuffer = any(
        isinstance(item, dict) and item.get("classification") == "material"
        for item in settings.get("constantBuffers", [])
    )
    shader = settings.get("shader", {})
    shader_supported = isinstance(shader, dict) and shader.get("strategy") == "target-native-pbr"
    checks["materialNodeConstruction"] = (
        principled is not None and not material_cbuffer and shader_supported
    )
    if material_cbuffer:
        errors.append("material CBuffer mappings are not constructed by the fixed Blender adapter")
    if not shader_supported:
        errors.append("the requested Shader strategy is not a validated native Blender PBR route")
    textures = settings.get("textures", [])
    checks["imageColorSpaces"] = isinstance(textures, list) and not textures
    if textures:
        errors.append("texture bindings are metadata-only and have no validated Blender image nodes")
    checks["sceneLinkIntegrity"] = bool(meshes) and all(
        obj.name in bpy.context.scene.objects and len(obj.data.materials) > 0
        for obj in meshes
    )
    output = artifact_root / "BreadValidation.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(output), check_existing=False)
    if output.is_file():
        artifacts.append({
            "path": output.relative_to(artifact_root).as_posix(),
            "size": output.stat().st_size,
            "sha256": file_sha256(output),
        })
except Exception as error:
    errors.append("Blender provider exception: " + type(error).__name__)

passed = all(checks.values()) and not errors and bool(artifacts)
receipt = {
    "schema": "replaylab-resource-export-blender-provider-receipt/v1",
    "schemaVersion": 1,
    "operationId": request["operationId"],
    "validationRequestSha256": request["validationRequestSha256"],
    "operationRequestSha256": request["operationRequestSha256"],
    "packageManifestSha256": request["packageManifestSha256"],
    "providerExecutableSha256": request["providerExecutableSha256"],
    "target": "blender",
    "status": "passed" if passed else "failed",
    "providerVersion": bpy.app.version_string,
    "checks": checks,
    "artifacts": artifacts,
    "errors": errors[:16],
}
receipt_path.write_text(
    json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
if not passed:
    raise SystemExit(18)
'''

    def _strict_provider_receipt(
        self,
        *,
        receipt_path: Path,
        artifact_root: Path,
        expected_schema: str,
        target: str,
        checks: Sequence[str],
        request: Mapping[str, Any],
        provider: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        receipt = _read_json(receipt_path, _MAX_JSON_BYTES)
        expected = {
            "schema": expected_schema,
            "schemaVersion": 1,
            "operationId": request["operationId"],
            "validationRequestSha256": request["requestSha256"],
            "operationRequestSha256": request["operationRequestSha256"],
            "packageManifestSha256": request["packageManifestSha256"],
            "providerExecutableSha256": provider["executableSha256"],
            "target": target,
        }
        for field, value in expected.items():
            if receipt.get(field) != value:
                raise ResourceExportError(
                    "%s provider receipt %s binding changed" % (target, field)
                )
        if receipt.get("status") not in ("passed", "failed"):
            raise ResourceExportError("%s provider receipt status is invalid" % target)
        version = receipt.get("providerVersion")
        if not isinstance(version, str) or not version.strip() or len(version) > 256:
            raise ResourceExportError("%s provider version is invalid" % target)
        observed_checks = receipt.get("checks")
        if not isinstance(observed_checks, dict):
            raise ResourceExportError("%s provider receipt omitted checks" % target)
        for check in checks:
            if not isinstance(observed_checks.get(check), bool):
                raise ResourceExportError(
                    "%s provider check %s is invalid" % (target, check)
                )
        records = receipt.get("artifacts")
        if not isinstance(records, list) or not records:
            raise ResourceExportError("%s provider receipt omitted artifacts" % target)
        expected_files: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise ResourceExportError("%s provider artifact record is invalid" % target)
            relative = "/".join(
                _relative_parts(record.get("path"), "%s provider artifact" % target)
            )
            if relative in expected_files:
                raise ResourceExportError("%s provider artifact is duplicated" % target)
            expected_files.add(relative)
            path = artifact_root.joinpath(*relative.split("/"))
            actual = self._managed_file_record(artifact_root, path)
            size = _strict_int(
                record.get("size"),
                "%s provider artifact size" % target,
                0,
                _MAX_PROVIDER_FILE_BYTES,
            )
            sha256 = _strict_hex(
                record.get("sha256"), 64, "%s provider artifact sha256" % target
            )
            if actual["size"] != size or actual["sha256"] != sha256:
                raise ResourceExportError("%s provider artifact changed" % target)
        if self._actual_regular_files(artifact_root) != expected_files:
            raise ResourceExportError("%s provider artifact file set changed" % target)
        passed = receipt["status"] == "passed" and all(
            observed_checks.get(check) is True for check in checks
        )
        return receipt, passed

    def _run_blender_target_validator(
        self,
        request: Mapping[str, Any],
        executable: Path | None,
        validation_root: Path,
    ) -> dict[str, Any]:
        provider = request["providers"].get("blenderValidator")
        if not isinstance(provider, dict):
            return {
                "status": "not-applicable",
                "requested": False,
                "reason": "Blender was not requested",
            }
        if executable is None or provider.get("status") != "available":
            return {
                "status": "unavailable",
                "requested": True,
                "provider": provider,
                "reason": provider.get("reason", "Blender is unavailable"),
            }
        root = validation_root / "blender"
        root.mkdir(parents=True)
        script_path = root / "validate_blender.py"
        request_path = root / "request.json"
        _atomic_write_bytes(script_path, self._blender_validation_script().encode("utf-8"))
        provider_request = {
            "schema": "replaylab-resource-export-blender-provider-request/v1",
            "schemaVersion": 1,
            "operationId": request["operationId"],
            "validationRequestSha256": request["requestSha256"],
            "operationRequestSha256": request["operationRequestSha256"],
            "packageManifestSha256": request["packageManifestSha256"],
            "providerExecutableSha256": provider["executableSha256"],
            "packageRoot": str(
                self._operation_dir(str(request["operationId"])) / "target"
            ),
            "artifactRoot": "artifacts",
            "receiptPath": "provider-receipt.json",
        }
        _atomic_write_json(request_path, provider_request)
        process = self._run_process(
            provider=provider,
            executable=executable,
            arguments=(
                str(executable),
                "--background",
                "--factory-startup",
                "--disable-autoexec",
                "--python-exit-code",
                "17",
                "--python",
                str(script_path),
                "--",
                str(request_path),
            ),
            validation_root=validation_root,
            provider_root=root,
            timeout_seconds=_BLENDER_VALIDATOR_TIMEOUT_SECONDS,
            stdout_name="stdout.log",
            stderr_name="stderr.log",
        )
        receipt_summary: dict[str, Any] = {}
        passed = False
        reason = "Blender headless validation did not produce a passing receipt"
        try:
            receipt, receipt_passed = self._strict_provider_receipt(
                receipt_path=root / "provider-receipt.json",
                artifact_root=root / "artifacts",
                expected_schema=BLENDER_PROVIDER_RECEIPT_SCHEMA,
                target="blender",
                checks=(
                    "gltfImport",
                    "meshIntegrity",
                    "materialNodeConstruction",
                    "imageColorSpaces",
                    "sceneLinkIntegrity",
                ),
                request=request,
                provider=provider,
            )
            passed = self._process_passed(process) and receipt_passed
            receipt_summary = {
                "path": "blender/provider-receipt.json",
                "sha256": _file_sha256(root / "provider-receipt.json", _MAX_JSON_BYTES),
                "providerVersion": receipt["providerVersion"],
                "checks": receipt["checks"],
                "artifacts": receipt["artifacts"],
            }
            if passed:
                reason = ""
        except (OSError, ResourceExportError, ValueError):
            reason = "Blender provider receipt failed strict validation"
        layer = {
            "status": "passed" if passed else "failed",
            "requested": True,
            "provider": provider,
            "process": process,
            "receipt": receipt_summary,
        }
        if reason:
            layer["reason"] = reason
        return layer

    def _run_unity_target_validator(
        self,
        request: Mapping[str, Any],
        executable: Path | None,
        validation_root: Path,
    ) -> dict[str, Any]:
        provider = request["providers"].get("unityUrpValidator")
        if not isinstance(provider, dict):
            return {
                "status": "not-applicable",
                "requested": False,
                "reason": "Unity URP was not requested",
            }
        if executable is None or provider.get("status") != "available":
            return {
                "status": "unavailable",
                "requested": True,
                "provider": provider,
                "reason": provider.get("reason", "Unity validator is unavailable"),
            }
        root = validation_root / "unity"
        root.mkdir(parents=True)
        request_path = root / "request.json"
        provider_request = {
            "schema": UNITY_PROVIDER_REQUEST_SCHEMA,
            "schemaVersion": 1,
            "operationId": request["operationId"],
            "validationRequestSha256": request["requestSha256"],
            "operationRequestSha256": request["operationRequestSha256"],
            "packageManifestSha256": request["packageManifestSha256"],
            "providerExecutableSha256": provider["executableSha256"],
            "target": "unity-urp",
            "profile": "UniversalRenderPipeline",
            "packageRoot": str(
                self._operation_dir(str(request["operationId"])) / "target"
            ),
            "artifactRoot": "artifacts",
            "receiptPath": "provider-receipt.json",
            "requiredChecks": [
                "meshImport",
                "materialImport",
                "textureImport",
                "shaderCompile",
                "sceneIntegrity",
            ],
        }
        _atomic_write_json(request_path, provider_request)
        process = self._run_process(
            provider=provider,
            executable=executable,
            arguments=(
                str(executable),
                "--replaylab-resource-export-request",
                str(request_path),
            ),
            validation_root=validation_root,
            provider_root=root,
            timeout_seconds=_UNITY_VALIDATOR_TIMEOUT_SECONDS,
            stdout_name="stdout.log",
            stderr_name="stderr.log",
        )
        receipt_summary: dict[str, Any] = {}
        passed = False
        reason = "Unity URP validation did not produce a passing receipt"
        try:
            receipt, receipt_passed = self._strict_provider_receipt(
                receipt_path=root / "provider-receipt.json",
                artifact_root=root / "artifacts",
                expected_schema=UNITY_PROVIDER_RECEIPT_SCHEMA,
                target="unity-urp",
                checks=(
                    "meshImport",
                    "materialImport",
                    "textureImport",
                    "shaderCompile",
                    "sceneIntegrity",
                ),
                request=request,
                provider=provider,
            )
            passed = self._process_passed(process) and receipt_passed
            receipt_summary = {
                "path": "unity/provider-receipt.json",
                "sha256": _file_sha256(root / "provider-receipt.json", _MAX_JSON_BYTES),
                "providerVersion": receipt["providerVersion"],
                "checks": receipt["checks"],
                "artifacts": receipt["artifacts"],
            }
            if passed:
                reason = ""
        except (OSError, ResourceExportError, ValueError):
            reason = "Unity provider receipt failed strict validation"
        layer = {
            "status": "passed" if passed else "failed",
            "requested": True,
            "provider": provider,
            "process": process,
            "receipt": receipt_summary,
        }
        if reason:
            layer["reason"] = reason
        return layer

    def _validated_target_validation_receipt(
        self,
        operation_id: str,
        state: Mapping[str, Any],
        request: Mapping[str, Any],
        validation_dir: Path,
    ) -> tuple[dict[str, Any], str]:
        operation_dir = self._operation_dir(operation_id)
        self._verify_target_provider_bindings(request)
        if _is_reparse(validation_dir) or not validation_dir.is_dir():
            raise ResourceExportError("target validation receipt directory is redirected")
        resolved = validation_dir.resolve(strict=True)
        if not _within(operation_dir, resolved):
            raise ResourceExportError("target validation receipt escaped its operation")
        if _read_json(validation_dir / "request.json", _MAX_JSON_BYTES) != request:
            raise ResourceExportError("target validation request evidence changed")
        receipt_path = validation_dir / "receipt.json"
        receipt = _read_json(receipt_path, _MAX_JSON_BYTES)
        expected = {
            "schema": TARGET_VALIDATION_RECEIPT_SCHEMA,
            "schemaVersion": 1,
            "operationId": operation_id,
            "requestSha256": request["requestSha256"],
            "operationRequestSha256": request["operationRequestSha256"],
            "packageManifestSha256": request["packageManifestSha256"],
            "finalizeSha256": request["finalizeSha256"],
            "targets": request["targets"],
            "providers": request["providers"],
        }
        for field, value in expected.items():
            if receipt.get(field) != value:
                raise ResourceExportError(
                    "target validation receipt %s binding changed" % field
                )
        layers = receipt.get("layers")
        if not isinstance(layers, dict):
            raise ResourceExportError("target validation receipt omitted layers")
        requested_layers = ["externalGltfValidator"]
        if "unity-urp" in request["targets"]:
            requested_layers.append("unityUrpValidator")
        if "blender" in request["targets"]:
            requested_layers.append("blenderValidator")
        for layer in requested_layers:
            value = layers.get(layer)
            if not isinstance(value, dict) or value.get("status") not in (
                "passed",
                "failed",
                "unavailable",
            ):
                raise ResourceExportError(
                    "target validation layer %s is invalid" % layer
                )
            if value.get("requested") is not True:
                raise ResourceExportError(
                    "target validation layer %s lost its requested flag" % layer
                )
            provider = request["providers"].get(layer)
            if value.get("provider") != provider:
                raise ResourceExportError(
                    "target validation layer %s provider binding changed" % layer
                )
            if value.get("status") == "unavailable":
                if not isinstance(provider, dict) or provider.get("status") == "available":
                    raise ResourceExportError(
                        "target validation layer %s availability is inconsistent"
                        % layer
                    )
                continue
            if value.get("status") != "passed":
                continue
            process = value.get("process")
            if not isinstance(process, dict) or not self._process_passed(process):
                raise ResourceExportError(
                    "target validation layer %s has no passing process receipt"
                    % layer
                )
            if layer == "externalGltfValidator":
                report = _read_json(
                    validation_dir / "gltf" / "report.json",
                    _MAX_PROVIDER_LOG_BYTES,
                )
                issues = report.get("issues")
                if (
                    not isinstance(issues, dict)
                    or issues.get("numErrors") != 0
                    or issues.get("truncated") is not False
                ):
                    raise ResourceExportError(
                        "cached Khronos glTF report is not passing"
                    )
            elif layer == "blenderValidator":
                unused_receipt, passed = self._strict_provider_receipt(
                    receipt_path=validation_dir
                    / "blender"
                    / "provider-receipt.json",
                    artifact_root=validation_dir / "blender" / "artifacts",
                    expected_schema=BLENDER_PROVIDER_RECEIPT_SCHEMA,
                    target="blender",
                    checks=(
                        "gltfImport",
                        "meshIntegrity",
                        "materialNodeConstruction",
                        "imageColorSpaces",
                        "sceneLinkIntegrity",
                    ),
                    request=request,
                    provider=provider,
                )
                if not passed:
                    raise ResourceExportError(
                        "cached Blender provider receipt is not passing"
                    )
            elif layer == "unityUrpValidator":
                unused_receipt, passed = self._strict_provider_receipt(
                    receipt_path=validation_dir
                    / "unity"
                    / "provider-receipt.json",
                    artifact_root=validation_dir / "unity" / "artifacts",
                    expected_schema=UNITY_PROVIDER_RECEIPT_SCHEMA,
                    target="unity-urp",
                    checks=(
                        "meshImport",
                        "materialImport",
                        "textureImport",
                        "shaderCompile",
                        "sceneIntegrity",
                    ),
                    request=request,
                    provider=provider,
                )
                if not passed:
                    raise ResourceExportError(
                        "cached Unity provider receipt is not passing"
                    )
        complete = all(layers[layer].get("status") == "passed" for layer in requested_layers)
        if receipt.get("targetValidationComplete") is not complete:
            raise ResourceExportError("target validation completion flag is inconsistent")
        records = receipt.get("files")
        if not isinstance(records, list):
            raise ResourceExportError("target validation receipt omitted files")
        actual_records = self._target_validation_file_records(validation_dir)
        if records != actual_records:
            raise ResourceExportError("target validation artifact manifest changed")
        receipt_sha256 = _file_sha256(receipt_path, _MAX_JSON_BYTES)
        pointer = state.get("targetValidation")
        if isinstance(pointer, dict) and pointer.get("requestSha256") == request["requestSha256"]:
            expected_relative = "validation/%s/receipt.json" % request["requestSha256"]
            if (
                pointer.get("receiptRelativePath") != expected_relative
                or pointer.get("receiptSha256") != receipt_sha256
            ):
                raise ResourceExportError("target validation operation pointer changed")
        return receipt, receipt_sha256

    def _cached_target_validation(
        self,
        operation_id: str,
        state: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str] | None:
        validation_dir = (
            self._operation_dir(operation_id)
            / "validation"
            / str(request["requestSha256"])
        )
        if not validation_dir.exists():
            return None
        return self._validated_target_validation_receipt(
            operation_id, state, request, validation_dir
        )

    def _record_target_validation_pointer(
        self,
        operation_id: str,
        state: dict[str, Any],
        request: Mapping[str, Any],
        receipt_sha256: str,
    ) -> dict[str, Any]:
        latest = self._load_state(operation_id)
        if (
            latest.get("requestSha256") != state.get("requestSha256")
            or latest.get("finalize") != state.get("finalize")
        ):
            raise ResourceExportError("resource-export operation changed during target validation")
        latest["targetValidation"] = {
            "requestSha256": request["requestSha256"],
            "receiptRelativePath": "validation/%s/receipt.json"
            % request["requestSha256"],
            "receiptSha256": receipt_sha256,
        }
        latest["updatedUnixMs"] = int(time.time() * 1000)
        _atomic_write_json(self._state_path(operation_id), latest)
        return latest

    def run_target_validation(self, operation_id: str) -> dict[str, Any]:
        """Run only fixed Host target validators for one immutable package."""

        operation_id = _strict_hex(operation_id, 32, "operation_id")
        state = self._load_state(operation_id)
        if state.get("status") != "finalized" or not isinstance(
            state.get("finalizeIntent"), dict
        ):
            raise ResourceExportError(
                "target validation requires a finalized resource-export package"
            )
        target, package_manifest_sha256 = self._validated_target_candidate(
            operation_id, state, state["finalizeIntent"]
        )
        package = _read_json(target / "package-manifest.json")
        targets = package.get("targets")
        if not isinstance(targets, list) or not targets:
            raise ResourceExportError("target package omitted its target list")
        request, executables = self._target_validation_request(
            operation_id, state, package_manifest_sha256, targets
        )
        cached = self._cached_target_validation(operation_id, state, request)
        if cached is not None:
            receipt, receipt_sha256 = cached
            self._record_target_validation_pointer(
                operation_id, state, request, receipt_sha256
            )
            return {
                "schema": TARGET_VALIDATION_RUN_SCHEMA,
                "schemaVersion": 1,
                "operationId": operation_id,
                "requestSha256": request["requestSha256"],
                "receiptPath": str(
                    self._operation_dir(operation_id)
                    / "validation"
                    / request["requestSha256"]
                    / "receipt.json"
                ),
                "receiptSha256": receipt_sha256,
                "targetValidationComplete": receipt["targetValidationComplete"],
                "layers": receipt["layers"],
                "idempotent": True,
            }

        operation_dir = self._operation_dir(operation_id)
        validation_root = operation_dir / "validation"
        validation_root.mkdir(exist_ok=True)
        if _is_reparse(validation_root) or not validation_root.is_dir():
            raise ResourceExportError("target validation root is redirected")
        staging = operation_dir / (".validation-%s.tmp" % uuid.uuid4().hex)
        staging.mkdir()
        destination = validation_root / request["requestSha256"]
        try:
            _atomic_write_json(staging / "request.json", request)
            layers = {
                "externalGltfValidator": self._run_gltf_target_validator(
                    request,
                    executables.get("externalGltfValidator"),
                    staging,
                ),
                "unityUrpValidator": self._run_unity_target_validator(
                    request,
                    executables.get("unityUrpValidator"),
                    staging,
                ),
                "blenderValidator": self._run_blender_target_validator(
                    request,
                    executables.get("blenderValidator"),
                    staging,
                ),
            }
            # External programs receive a read-only package path. Re-hash the
            # entire immutable package before admitting any receipt.
            unused_target, observed_manifest_sha256 = self._validated_target_candidate(
                operation_id, state, state["finalizeIntent"]
            )
            if observed_manifest_sha256 != package_manifest_sha256:
                raise ResourceExportError(
                    "target package changed during external validation"
                )
            self._verify_target_provider_bindings(request)
            requested_layers = ["externalGltfValidator"]
            if "unity-urp" in targets:
                requested_layers.append("unityUrpValidator")
            if "blender" in targets:
                requested_layers.append("blenderValidator")
            complete = all(
                layers[layer].get("status") == "passed"
                for layer in requested_layers
            )
            files = self._target_validation_file_records(staging)
            receipt = {
                "schema": TARGET_VALIDATION_RECEIPT_SCHEMA,
                "schemaVersion": 1,
                "operationId": operation_id,
                "requestSha256": request["requestSha256"],
                "operationRequestSha256": request["operationRequestSha256"],
                "packageManifestSha256": package_manifest_sha256,
                "finalizeSha256": request["finalizeSha256"],
                "targets": request["targets"],
                "providers": request["providers"],
                "targetValidationComplete": complete,
                "layers": layers,
                "files": files,
                "createdUnixMs": int(time.time() * 1000),
            }
            _atomic_write_json(staging / "receipt.json", receipt)
            try:
                os.replace(str(staging), str(destination))
            except OSError:
                # Windows reports an existing directory as a generic OSError,
                # while POSIX commonly raises FileExistsError. Only adopt an
                # already-published directory; all other publication failures
                # must remain fatal.
                if not destination.is_dir() or _is_reparse(destination):
                    raise
                if staging.exists() and not _is_reparse(staging):
                    shutil.rmtree(staging)
            receipt, receipt_sha256 = self._validated_target_validation_receipt(
                operation_id, state, request, destination
            )
        except Exception:
            if staging.exists() and not _is_reparse(staging):
                shutil.rmtree(staging)
            raise
        self._record_target_validation_pointer(
            operation_id, state, request, receipt_sha256
        )
        return {
            "schema": TARGET_VALIDATION_RUN_SCHEMA,
            "schemaVersion": 1,
            "operationId": operation_id,
            "requestSha256": request["requestSha256"],
            "receiptPath": str(destination / "receipt.json"),
            "receiptSha256": receipt_sha256,
            "targetValidationComplete": receipt["targetValidationComplete"],
            "layers": receipt["layers"],
            "idempotent": False,
        }

    def status(self, operation_id: str) -> dict[str, Any]:
        state = self._load_state(operation_id)
        operation_dir = self._operation_dir(operation_id)
        if (
            state.get("status") == "captured"
            and isinstance(state.get("finalizeIntent"), dict)
            and (operation_dir / "target").exists()
        ):
            self._validated_capture(operation_id)
            state, unused_recovered = self._adopt_finalized_target(
                operation_id, state
            )
        result = json.loads(_canonical_json_bytes(state).decode("utf-8"))
        result["managedOperationPath"] = str(operation_dir)
        if result.get("status") == "finalized":
            result["packagePath"] = str(operation_dir / "target")
            result["packageManifestPath"] = str(
                operation_dir / "target" / "package-manifest.json"
            )
        return result

    def reconcile_tool_postcondition(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        expected_capture_session_id: str = "",
    ) -> dict[str, Any]:
        """Prove one interrupted export call completed or is safe to retry.

        This is a Host-only recovery primitive.  It never launches RenderDoc or
        an external validator.  Existing completion manifests/atomic publish
        markers may be adopted, but an ambiguous partial directory remains
        ``unresolved`` and can never authorize a blind retry.
        """

        if tool not in {
            "prepare_resource_export",
            "finalize_resource_export",
            "run_resource_export_target_validation",
        }:
            raise ResourceExportError(
                "resource-export postcondition tool is unsupported"
            )
        if not isinstance(arguments, Mapping):
            raise ResourceExportError(
                "resource-export postcondition arguments must be an object"
            )
        operation_id = _strict_hex(
            arguments.get("operation_id", arguments.get("operationId")),
            32,
            "operation_id",
        )
        expected_session = str(expected_capture_session_id or "")
        if expected_session:
            expected_session = _strict_hex(
                expected_session, 32, "expected_capture_session_id"
            )

        def response(
            outcome: str,
            reason: str,
            *,
            state: Mapping[str, Any] | None = None,
            result: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            if outcome not in {"completed", "not-applied", "unresolved"}:
                raise ResourceExportError(
                    "resource-export postcondition outcome is invalid"
                )
            value: dict[str, Any] = {
                "schema": POSTCONDITION_SCHEMA,
                "schemaVersion": 1,
                "tool": tool,
                "operationId": operation_id,
                "outcome": outcome,
                "reason": reason,
                "verification": {
                    "operationExists": state is not None,
                    "operationStatus": (
                        str(state.get("status", "") or "") if state else ""
                    ),
                    "requestSha256": (
                        str(state.get("requestSha256", "") or "")
                        if state
                        else ""
                    ),
                },
            }
            if result is not None:
                value["result"] = json.loads(
                    _canonical_json_bytes(result).decode("utf-8")
                )
            return value

        operation_dir = self._operation_dir(operation_id, must_exist=False)
        if not operation_dir.exists() and not operation_dir.is_symlink():
            return response("not-applied", "operation-directory-absent")
        try:
            state = self.status(operation_id)
        except ResourceExportError:
            return response("unresolved", "operation-state-invalid")
        request = state.get("request")
        if not isinstance(request, dict):
            return response(
                "unresolved", "operation-request-missing", state=state
            )
        if expected_session and request.get("captureSessionId") != expected_session:
            return response(
                "unresolved", "capture-session-mismatch", state=state
            )

        if tool == "prepare_resource_export":
            expected_request = {
                "eventId": arguments.get("event_id", arguments.get("eventId")),
                "instance": arguments.get("instance", 0),
                "view": arguments.get("view", 0),
                "assetName": arguments.get(
                    "asset_name", arguments.get("assetName", "RenderDocAsset")
                ),
            }
            if any(request.get(key) != value for key, value in expected_request.items()):
                return response(
                    "unresolved", "prepare-request-mismatch", state=state
                )
            if state.get("status") in {"captured", "finalized"}:
                return response(
                    "completed",
                    "capture-completion-manifest-verified",
                    state=state,
                    result={
                        "schema": "replaylab-resource-export-prepare/v1",
                        "schemaVersion": 1,
                        "idempotent": True,
                        "recoveredAfterLostResponse": True,
                        "operation": state,
                        "applied": True,
                    },
                )
            if state.get("status") == "ticketed":
                try:
                    adopted = self.adopt_completed_capture(operation_id)
                except ResourceExportError:
                    return response(
                        "unresolved", "capture-files-are-partial", state=state
                    )
                if adopted is not None:
                    return response(
                        "completed",
                        "capture-completion-manifest-adopted",
                        state=adopted,
                        result={
                            "schema": "replaylab-resource-export-prepare/v1",
                            "schemaVersion": 1,
                            "idempotent": True,
                            "recoveredAfterLostResponse": True,
                            "operation": adopted,
                            "applied": True,
                        },
                    )
                return response(
                    "not-applied", "capture-postcondition-absent", state=state
                )
            return response("unresolved", "prepare-state-unsupported", state=state)

        if tool == "finalize_resource_export":
            bundle_sha256 = _strict_hex(
                arguments.get(
                    "expected_bundle_sha256",
                    arguments.get("expectedBundleSha256"),
                ),
                64,
                "expected_bundle_sha256",
            )
            transfer_spec = arguments.get(
                "transfer_spec", arguments.get("transferSpec")
            )
            if not isinstance(transfer_spec, Mapping):
                return response(
                    "unresolved", "transfer-spec-invalid", state=state
                )
            if state.get("status") == "finalized":
                try:
                    finalized = self.finalize(
                        operation_id, bundle_sha256, transfer_spec
                    )
                except ResourceExportError:
                    return response(
                        "unresolved", "finalize-binding-mismatch", state=state
                    )
                finalized["applied"] = True
                return response(
                    "completed",
                    "atomic-target-publication-verified",
                    state=finalized.get("operation", state),
                    result=finalized,
                )
            staging = list(operation_dir.glob(".target-*.tmp"))
            if staging:
                return response(
                    "unresolved", "partial-finalize-staging-present", state=state
                )
            # Validate the exact request against the captured bundle before
            # saying the final package postcondition is absent and retryable.
            try:
                captured, unused_capture_dir, bundle = self._validated_capture(
                    operation_id
                )
                if captured["capture"]["manifestSha256"] != bundle_sha256:
                    raise ResourceExportError("bundle digest changed")
                spec = self._normalize_transfer_spec(transfer_spec)
                if "assetName" not in spec:
                    spec["assetName"] = captured["request"]["assetName"]
                self._validate_transfer_references(
                    bundle, spec, self._capture_dir(operation_id, captured)
                )
                binding = {
                    "operationId": operation_id,
                    "requestSha256": captured["requestSha256"],
                    "bundleSha256": bundle_sha256,
                    "transferSpec": spec,
                }
                finalize_sha256 = _json_sha256(binding)
                intent = captured.get("finalizeIntent")
                if intent is not None and (
                    not isinstance(intent, dict)
                    or intent.get("finalizeSha256") != finalize_sha256
                ):
                    raise ResourceExportError("finalize intent changed")
            except ResourceExportError:
                return response(
                    "unresolved", "finalize-binding-mismatch", state=state
                )
            return response(
                "not-applied", "target-publication-absent", state=state
            )

        if state.get("status") != "finalized" or not isinstance(
            state.get("finalizeIntent"), dict
        ):
            return response(
                "unresolved", "validation-package-not-finalized", state=state
            )
        try:
            target, package_manifest_sha256 = self._validated_target_candidate(
                operation_id, state, state["finalizeIntent"]
            )
            package = _read_json(target / "package-manifest.json")
            targets = package.get("targets")
            if not isinstance(targets, list) or not targets:
                raise ResourceExportError("target package omitted targets")
            request_value, unused_executables = self._target_validation_request(
                operation_id, state, package_manifest_sha256, targets
            )
            cached = self._cached_target_validation(
                operation_id, state, request_value
            )
        except ResourceExportError:
            return response(
                "unresolved", "validation-binding-invalid", state=state
            )
        if cached is not None:
            receipt, receipt_sha256 = cached
            result = {
                "schema": TARGET_VALIDATION_RUN_SCHEMA,
                "schemaVersion": 1,
                "operationId": operation_id,
                "requestSha256": request_value["requestSha256"],
                "receiptPath": str(
                    operation_dir
                    / "validation"
                    / request_value["requestSha256"]
                    / "receipt.json"
                ),
                "receiptSha256": receipt_sha256,
                "targetValidationComplete": receipt[
                    "targetValidationComplete"
                ],
                "layers": receipt["layers"],
                "idempotent": True,
                "applied": True,
            }
            return response(
                "completed",
                "target-validation-receipt-verified",
                state=state,
                result=result,
            )
        if list(operation_dir.glob(".validation-*.tmp")):
            return response(
                "unresolved", "partial-validation-staging-present", state=state
            )
        return response(
            "not-applied", "target-validation-receipt-absent", state=state
        )

    def validate(self, operation_id: str) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        checks: dict[str, bool] = {}
        constant_buffer_complete = False
        mesh_transfer_complete = False
        try:
            state, unused_capture_dir, unused_bundle = self._validated_capture(operation_id)
            checks["captureBundleHashes"] = True
            checks["captureIdentity"] = True
        except Exception as error:
            return {
                "schema": VALIDATION_SCHEMA,
                "schemaVersion": 1,
                "operationId": operation_id,
                "valid": False,
                "complete": False,
                "internalComplete": False,
                "targetValidationComplete": False,
                "layers": {
                    "captureAndPackageStructure": {
                        "status": "failed",
                        "authority": "FixedProgram",
                    },
                    "externalGltfValidator": {"status": "unavailable"},
                    "unityUrpValidator": {"status": "not-applicable"},
                    "blenderValidator": {"status": "not-applicable"},
                    "replayPostcondition": {
                        "status": "not-applicable",
                        "reason": "Host validation does not mutate replay state",
                    },
                },
                "checks": checks,
                "errors": [str(error)],
                "warnings": warnings,
            }
        capture_receipt = state.get("capture")
        if (
            isinstance(capture_receipt, dict)
            and capture_receipt.get("status") == "partial"
        ):
            completion = capture_receipt.get("completion", {})
            count = (
                completion.get("incompleteCount", 0)
                if isinstance(completion, dict)
                else 0
            )
            warnings.append(
                "capture bundle is partial with %d explicitly incomplete artifact(s)"
                % count
            )
        if state.get("status") != "finalized":
            warnings.append("capture bundle is valid but target conversion is not finalized")
            return {
                "schema": VALIDATION_SCHEMA,
                "schemaVersion": 1,
                "operationId": operation_id,
                "valid": True,
                "complete": False,
                "internalComplete": False,
                "targetValidationComplete": False,
                "layers": {
                    "captureBundleStructure": {
                        "status": "passed",
                        "authority": "FixedProgram",
                    },
                    "targetPackageStructure": {"status": "not-run"},
                    "externalGltfValidator": {"status": "unavailable"},
                    "unityUrpValidator": {"status": "not-applicable"},
                    "blenderValidator": {"status": "not-applicable"},
                    "replayPostcondition": {
                        "status": "not-applicable",
                        "reason": "Host validation does not mutate replay state",
                    },
                },
                "checks": checks,
                "errors": errors,
                "warnings": warnings,
            }
        requested_targets: list[str] = []
        package_manifest_sha256 = ""
        try:
            finalize = state.get("finalize")
            if not isinstance(finalize, dict):
                raise ResourceExportError("finalized operation omitted its receipt")
            operation_dir = self._operation_dir(operation_id)
            target = operation_dir / "target"
            if _is_reparse(target) or not target.is_dir() or not _within(operation_dir, target.resolve(strict=True)):
                raise ResourceExportError("target package is missing or redirected")
            manifest = target / "package-manifest.json"
            package_manifest_sha256 = _file_sha256(manifest, _MAX_JSON_BYTES)
            if package_manifest_sha256 != finalize.get("packageManifestSha256"):
                raise ResourceExportError("target package manifest changed")
            package = _read_json(manifest)
            if package.get("schema") != FINAL_MANIFEST_SCHEMA or package.get("operationId") != operation_id:
                raise ResourceExportError("target package manifest identity is invalid")
            if package.get("captureBundleSha256") != state["capture"]["manifestSha256"]:
                raise ResourceExportError("target package source binding changed")
            records = package.get("files")
            if not isinstance(records, list):
                raise ResourceExportError("target package file list is invalid")
            expected_files: set[str] = set()
            for record in records:
                if not isinstance(record, dict):
                    raise ResourceExportError("target package file record is invalid")
                relative = "/".join(
                    _relative_parts(record.get("path"), "target package file")
                )
                if relative in expected_files:
                    raise ResourceExportError("target package manifest contains a duplicate file")
                expected_files.add(relative)
                path = target.joinpath(*relative.split("/"))
                if _is_reparse(path) or not path.is_file() or not _within(target, path.resolve(strict=True)):
                    raise ResourceExportError("target package file escaped or disappeared")
                if path.stat().st_size != record.get("size") or _file_sha256(path) != record.get("sha256"):
                    raise ResourceExportError("target package file changed")
            actual_files: set[str] = set()
            for path in target.rglob("*"):
                if path.is_dir():
                    if _is_reparse(path):
                        raise ResourceExportError("target package contains a redirected directory")
                    continue
                if _is_reparse(path) or not path.is_file():
                    raise ResourceExportError("target package contains a redirected file")
                relative = path.relative_to(target).as_posix()
                if relative != "package-manifest.json":
                    actual_files.add(relative)
            if actual_files != expected_files:
                extra = sorted(actual_files - expected_files)
                missing = sorted(expected_files - actual_files)
                raise ResourceExportError(
                    "target package file set changed (extra=%s, missing=%s)"
                    % (extra[:8], missing[:8])
                )
            gltf = _read_json(target / "asset.gltf")
            effect = (gltf.get("extras", {}).get("replaylab", {}).get("executionKind") == "effect"
                      and gltf.get("extras", {}).get("replaylab", {}).get("geometryApplicable") is False)
            if gltf.get("asset", {}).get("version") != "2.0" or (not gltf.get("meshes") and not effect):
                raise ResourceExportError("generated glTF structure is invalid")
            expected_binary = 0 if effect else gltf.get("buffers", [{}])[0].get("byteLength")
            if (target / "asset.bin").stat().st_size != expected_binary:
                raise ResourceExportError("generated glTF binary length changed")
            checks["targetPackageHashes"] = True
            checks["gltfStructure"] = True
            gltf_result = package.get("gltf", {})
            omitted = gltf_result.get("omittedAttributes", []) if isinstance(gltf_result, dict) else []
            if omitted:
                warnings.append("%d vertex input(s) remain only in the portable raw/canonical sidecar" % len(omitted))
            mesh_transfer_complete = not omitted
            checks["meshInputCoverage"] = mesh_transfer_complete
            transfer = _read_json(target / "transfer-spec.json", _MAX_TRANSFER_SPEC_BYTES + 1)
            # Older published packages remain readable, but cannot claim this
            # newer source-value/coverage guarantee without its checked report.
            cbuffer_report_path = target / "cbuffer-transfer.json"
            if cbuffer_report_path.is_file() and "cbuffer-transfer.json" in expected_files:
                cbuffer_report = _read_json(cbuffer_report_path)
                constant_buffer_complete = (cbuffer_report.get("schema") == "bread-cbuffer-transfer/v1"
                    and cbuffer_report.get("coverage", {}).get("complete") is True)
                if not constant_buffer_complete:
                    warnings.append("constant-buffer transfer has unresolved captured values or unmapped physical lanes")
            else:
                warnings.append("constant-buffer transfer coverage has not been verified")
            checks["constantBufferCoverage"] = constant_buffer_complete
            targets = transfer.get("targets")
            if isinstance(targets, list):
                requested_targets = [
                    str(target) for target in targets if isinstance(target, str)
                ]
            if transfer.get("coordinateSystem", {}).get("policy") == "preserve":
                warnings.append("object-space basis was preserved; API family alone is not coordinate-system evidence")
            if transfer.get("shader", {}).get("strategy") == "evidence-only":
                warnings.append("shader conversion remains evidence-only")
        except Exception as error:
            errors.append(str(error))
        internal_complete = not errors
        target_complete = False
        target_receipt_summary: dict[str, Any] | None = None
        target_layers: dict[str, dict[str, Any]] = {
            "externalGltfValidator": {
                "status": "unavailable",
                "requested": "gltf" in requested_targets,
                "reason": "No Host target-validation receipt is available",
            },
            "unityUrpValidator": {
                "status": (
                    "unavailable"
                    if "unity-urp" in requested_targets
                    else "not-applicable"
                ),
                "requested": "unity-urp" in requested_targets,
                "reason": (
                    "No Host target-validation receipt is available"
                    if "unity-urp" in requested_targets
                    else "Unity URP was not requested"
                ),
            },
            "blenderValidator": {
                "status": (
                    "unavailable"
                    if "blender" in requested_targets
                    else "not-applicable"
                ),
                "requested": "blender" in requested_targets,
                "reason": (
                    "No Host target-validation receipt is available"
                    if "blender" in requested_targets
                    else "Blender was not requested"
                ),
            },
        }
        target_errors: list[str] = []
        if internal_complete:
            try:
                target_request, unused_executables = self._target_validation_request(
                    operation_id,
                    state,
                    package_manifest_sha256,
                    requested_targets,
                )
                for layer_name, target_name in (
                    ("externalGltfValidator", "glTF"),
                    ("unityUrpValidator", "unity-urp"),
                    ("blenderValidator", "blender"),
                ):
                    requested = (
                        layer_name == "externalGltfValidator"
                        or target_name in requested_targets
                    )
                    provider = target_request["providers"].get(layer_name)
                    if not requested:
                        continue
                    if isinstance(provider, dict):
                        status = (
                            "unavailable"
                            if provider.get("status") != "available"
                            else "not-run"
                        )
                        target_layers[layer_name] = {
                            "status": status,
                            "requested": True,
                            "provider": provider,
                            "reason": (
                                provider.get(
                                    "reason", "configured provider has not been run"
                                )
                                if status == "unavailable"
                                else "run_resource_export_target_validation has not produced a receipt"
                            ),
                        }
                cached = self._cached_target_validation(
                    operation_id, state, target_request
                )
                if cached is not None:
                    target_receipt, target_receipt_sha256 = cached
                    for layer_name in target_layers:
                        layer = target_receipt["layers"].get(layer_name)
                        if isinstance(layer, dict):
                            target_layers[layer_name] = layer
                    target_complete = (
                        target_receipt.get("targetValidationComplete") is True
                    )
                    target_receipt_summary = {
                        "requestSha256": target_request["requestSha256"],
                        "receiptRelativePath": "validation/%s/receipt.json"
                        % target_request["requestSha256"],
                        "receiptSha256": target_receipt_sha256,
                    }
                    checks["targetValidationReceipt"] = True
            except Exception as error:
                target_errors.append(str(error))
        for layer_name, layer in target_layers.items():
            if layer.get("requested") is not True:
                continue
            if layer.get("status") == "failed":
                target_errors.append("%s failed" % layer_name)
            elif layer.get("status") in ("unavailable", "not-run"):
                warnings.append(
                    "%s is %s" % (layer_name, layer.get("status"))
                )
        source_complete = (constant_buffer_complete and mesh_transfer_complete
                           and (state.get("capture") or {}).get("status") == "complete")
        layers = {
            "captureBundleStructure": {
                "status": "passed" if internal_complete else "failed",
                "authority": "FixedProgram",
            },
            "targetPackageStructure": {
                "status": "passed" if internal_complete else "failed",
                "authority": "FixedProgram",
            },
            "semanticTransfer": {
                "status": (
                    "partial" if internal_complete and not source_complete else
                    "passed-with-warnings"
                    if internal_complete and warnings
                    else "passed" if internal_complete else "failed"
                ),
                "authority": "FixedProgramPlusReviewedTransferSpec",
            },
            "externalGltfValidator": target_layers["externalGltfValidator"],
            "unityUrpValidator": target_layers["unityUrpValidator"],
            "blenderValidator": target_layers["blenderValidator"],
            "replayPostcondition": {
                "status": "not-applicable",
                "reason": "Host finalization and validation do not mutate replay state",
            },
        }
        result = {
            "schema": VALIDATION_SCHEMA,
            "schemaVersion": 1,
            "operationId": operation_id,
            "valid": internal_complete and not target_errors,
            "complete": internal_complete and source_complete and target_complete and not target_errors,
            "internalComplete": internal_complete,
            "sourceValueCoverageComplete": constant_buffer_complete,
            "sourceFidelityComplete": source_complete,
            "targetValidationComplete": target_complete,
            "layers": layers,
            "checks": checks,
            "errors": errors + target_errors,
            "warnings": warnings,
        }
        if target_receipt_summary is not None:
            result["targetValidationReceipt"] = target_receipt_summary
        return result


__all__ = [
    "BLENDER_ENV",
    "BLENDER_PROVIDER_RECEIPT_SCHEMA",
    "BUNDLE_SCHEMA",
    "CAPABILITIES_SCHEMA",
    "CAPTURE_RESPONSE_SCHEMA",
    "FINAL_MANIFEST_SCHEMA",
    "GLTF_VALIDATOR_ENV",
    "OPERATION_SCHEMA",
    "RESOURCE_EXPORT_ROOT_ENV",
    "ResourceExportError",
    "ResourceExportManager",
    "TARGET_VALIDATION_RECEIPT_SCHEMA",
    "TARGET_VALIDATION_REQUEST_SCHEMA",
    "TARGET_VALIDATION_RUN_SCHEMA",
    "TICKET_SCHEMA",
    "TRANSFER_SCHEMA",
    "UNITY_PROVIDER_RECEIPT_SCHEMA",
    "UNITY_PROVIDER_REQUEST_SCHEMA",
    "UNITY_VALIDATOR_ENV",
    "VALIDATION_SCHEMA",
    "resolve_resource_export_root",
]
