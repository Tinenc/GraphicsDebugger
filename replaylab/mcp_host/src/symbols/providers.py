"""Trusted, deterministic asset-provider process runner.

This module is deliberately independent from the asset scanner.  A caller may
select a pre-registered provider by ID, but cannot supply an executable,
arguments, environment, or output location.  Provider output becomes trusted
only as an in-memory :class:`TrustedProviderResult` carrying an HMAC made by
this process; JSON fields such as ``trusted`` or ``verified`` are rejected.

The runner is an authenticity and policy boundary, not an operating-system
sandbox.  Only binaries the user has deliberately registered should be run.
"""

from __future__ import annotations

import ctypes as _ctypes
from dataclasses import dataclass, field
import fnmatch
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
from types import MappingProxyType
from typing import Any, Mapping, Sequence


REGISTRY_SCHEMA = "renderdoc-ultra-provider-registry/v1"
SYMBOL_SCHEMA = "renderdoc-ultra-symbols/v1"
SCHEMA_VERSION = 1
PROVENANCE = "renderdoc-ultra-trusted-provider-process/v1"

REGISTRY_FILENAME = "trusted_providers.v1.json"
OUTPUT_MANIFEST_FILENAME = "symbols.v1.json"

MAX_REGISTRY_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_STDIO_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_SHADERS = 250_000
MAX_ARGUMENTS = 128
MAX_ARGUMENT_BYTES = 32 * 1024
MAX_TIMEOUT_SECONDS = 300.0
MIN_TIMEOUT_SECONDS = 0.05

_PROVIDER_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9]*)\}")
_ALLOWED_PLACEHOLDERS = frozenset(
    ("assetRoot", "outputDir", "manifestPath", "buildId")
)
_REJECTED_SHELL_HOSTS = frozenset(
    (
        "bash",
        "cmd",
        "cmd.exe",
        "dash",
        "fish",
        "ksh",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "sh",
        "tcsh",
        "wsl",
        "wsl.exe",
        "zsh",
    )
)
_INTERPRETER_INLINE_FLAGS = {
    "node": {"-e", "--eval", "-p", "--print"},
    "node.exe": {"-e", "--eval", "-p", "--print"},
    "nodejs": {"-e", "--eval", "-p", "--print"},
    "nodejs.exe": {"-e", "--eval", "-p", "--print"},
    "perl": {"-e"},
    "perl.exe": {"-e"},
    "php": {"-r"},
    "php.exe": {"-r"},
    "py": {"-c", "-m"},
    "py.exe": {"-c", "-m"},
    "python": {"-c", "-m"},
    "python.exe": {"-c", "-m"},
    "python3": {"-c", "-m"},
    "python3.exe": {"-c", "-m"},
    "pythonw": {"-c", "-m"},
    "pythonw.exe": {"-c", "-m"},
    "ruby": {"-e"},
    "ruby.exe": {"-e"},
}
_RESERVED_TRUST_KEYS = frozenset(
    (
        "attestation",
        "hashverified",
        "programverified",
        "serviceattestation",
        "serviceowned",
        "symbolmetadatatrusted",
        "trusted",
        "verified",
    )
)
_OUTPUT_PATH_KEYS = frozenset(
    ("artifactpath", "blobpath", "containerpath", "outputpath")
)
_LOGICAL_PATH_KEYS = frozenset(("assetpath", "path", "sourcepath"))
_ATTESTATION_KEY = secrets.token_bytes(32)
_RESULT_CONSTRUCTOR_TOKEN = object()
_OUTPUT_VERIFICATION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class _WindowsJobApi:
    create_job: Any
    set_information: Any
    assign_process: Any
    terminate_job: Any
    close_handle: Any


class _WindowsJobIoCounters(_ctypes.Structure):
    _fields_ = [
        (name, _ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _WindowsJobBasicLimits(_ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _ctypes.c_longlong),
        ("PerJobUserTimeLimit", _ctypes.c_longlong),
        ("LimitFlags", _ctypes.c_uint32),
        ("MinimumWorkingSetSize", _ctypes.c_size_t),
        ("MaximumWorkingSetSize", _ctypes.c_size_t),
        ("ActiveProcessLimit", _ctypes.c_uint32),
        ("Affinity", _ctypes.c_size_t),
        ("PriorityClass", _ctypes.c_uint32),
        ("SchedulingClass", _ctypes.c_uint32),
    ]


class _WindowsJobExtendedLimits(_ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _WindowsJobBasicLimits),
        ("IoInfo", _WindowsJobIoCounters),
        ("ProcessMemoryLimit", _ctypes.c_size_t),
        ("JobMemoryLimit", _ctypes.c_size_t),
        ("PeakProcessMemoryUsed", _ctypes.c_size_t),
        ("PeakJobMemoryUsed", _ctypes.c_size_t),
    ]


_WINDOWS_JOB_API_LOCK = threading.Lock()
_WINDOWS_JOB_API: _WindowsJobApi | None = None


def _windows_job_api() -> _WindowsJobApi | None:
    """Return one typed WinAPI table for all provider child processes."""

    global _WINDOWS_JOB_API
    if os.name != "nt":
        return None
    if _WINDOWS_JOB_API is not None:
        return _WINDOWS_JOB_API
    with _WINDOWS_JOB_API_LOCK:
        if _WINDOWS_JOB_API is not None:
            return _WINDOWS_JOB_API
        kernel32 = _ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = [_ctypes.c_void_p, _ctypes.c_wchar_p]
        create_job.restype = _ctypes.c_void_p
        set_information = kernel32.SetInformationJobObject
        set_information.argtypes = [
            _ctypes.c_void_p,
            _ctypes.c_int,
            _ctypes.c_void_p,
            _ctypes.c_uint32,
        ]
        set_information.restype = _ctypes.c_int
        assign_process = kernel32.AssignProcessToJobObject
        assign_process.argtypes = [_ctypes.c_void_p, _ctypes.c_void_p]
        assign_process.restype = _ctypes.c_int
        terminate_job = kernel32.TerminateJobObject
        terminate_job.argtypes = [_ctypes.c_void_p, _ctypes.c_uint32]
        terminate_job.restype = _ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [_ctypes.c_void_p]
        close_handle.restype = _ctypes.c_int
        _WINDOWS_JOB_API = _WindowsJobApi(
            create_job=create_job,
            set_information=set_information,
            assign_process=assign_process,
            terminate_job=terminate_job,
            close_handle=close_handle,
        )
        return _WINDOWS_JOB_API


class ProviderError(RuntimeError):
    """Base error for trusted provider operations."""


class ProviderRegistryError(ProviderError):
    """The local provider registry is absent or invalid."""


class UnknownProviderError(ProviderError):
    """The requested provider ID is not registered."""


class ProviderIntegrityError(ProviderError):
    """A registered executable, fixed input, or registry changed."""


class ProviderExecutionError(ProviderError):
    """The provider failed or exceeded an output limit."""


class ProviderTimeoutError(ProviderExecutionError):
    """The provider exceeded its registered timeout."""


class ProviderOutputError(ProviderError):
    """The provider output is malformed or leaves its output directory."""


@dataclass(frozen=True)
class ProviderSpec:
    """Validated registry policy for one provider."""

    provider_id: str
    version: str
    executable: Path
    executable_sha256: str
    args_template: tuple[str, ...]
    supported_schemas: tuple[str, ...]
    build_patterns: tuple[str, ...]
    timeout_seconds: float
    fixed_file_hashes: tuple[tuple[Path, str], ...] = ()


@dataclass(frozen=True)
class ProviderRegistry:
    """Immutable provider registry loaded from the user's config directory."""

    path: Path
    sha256: str
    providers: Mapping[str, ProviderSpec]

    def get(self, provider_id: str) -> ProviderSpec:
        try:
            return self.providers[provider_id]
        except KeyError as error:
            raise UnknownProviderError(
                "Unknown trusted provider: %s" % provider_id
            ) from error


@dataclass(frozen=True)
class ProviderArtifact:
    """A path-checked output artifact retained in memory after cleanup."""

    relative_path: str
    sha256: str
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class ProviderAttestation:
    """Process-local proof that the service verified one provider result."""

    run_id: str
    provider_id: str
    provider_version: str
    provider_binary_sha256: str
    registry_sha256: str
    asset_root_sha256: str
    output_schema: str
    manifest_sha256: str
    artifacts_sha256: str
    build_id: str
    provenance: str
    issued_at_unix_ns: int
    duration_ms: int
    signature: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "providerId": self.provider_id,
            "providerVersion": self.provider_version,
            "providerBinarySha256": self.provider_binary_sha256,
            "registrySha256": self.registry_sha256,
            "assetRootSha256": self.asset_root_sha256,
            "outputSchema": self.output_schema,
            "manifestSha256": self.manifest_sha256,
            "artifactsSha256": self.artifacts_sha256,
            "buildId": self.build_id,
            "provenance": self.provenance,
            "issuedAtUnixNs": self.issued_at_unix_ns,
            "durationMs": self.duration_ms,
            "signature": self.signature,
        }


@dataclass(frozen=True, init=False)
class TrustedProviderResult:
    """Immutable, process-attested provider output.

    The manifest is stored as canonical bytes and returned as a fresh object so
    a consumer cannot mutate already-attested content.  Construction is kept
    inside this module; a JSON document cannot instantiate this capability.
    """

    _manifest_bytes: bytes = field(repr=False)
    artifacts: tuple[ProviderArtifact, ...]
    attestation: ProviderAttestation
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)

    def __init__(
        self,
        manifest_bytes: bytes,
        artifacts: tuple[ProviderArtifact, ...],
        attestation: ProviderAttestation,
        stdout: str,
        stderr: str,
        *,
        _constructor_token: object,
    ) -> None:
        if _constructor_token is not _RESULT_CONSTRUCTOR_TOKEN:
            raise TypeError("TrustedProviderResult is service-owned")
        object.__setattr__(self, "_manifest_bytes", manifest_bytes)
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "attestation", attestation)
        object.__setattr__(self, "stdout", stdout)
        object.__setattr__(self, "stderr", stderr)

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_bytes.decode("utf-8"))

    @property
    def manifest_bytes(self) -> bytes:
        return bytes(self._manifest_bytes)


def default_registry_path() -> Path:
    """Return the platform user-config path for the trusted registry."""

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "RenderDocUltra" / REGISTRY_FILENAME
        return Path.home() / "AppData" / "Local" / "RenderDocUltra" / REGISTRY_FILENAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base_path = Path(xdg) if xdg else Path.home() / ".config"
    return base_path / "renderdoc-ultra" / REGISTRY_FILENAME


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ProviderOutputError("Output is not canonical JSON: %s" % error) from error


def _duplicate_rejecting_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number: %s" % value)


def _strict_json_loads(payload: bytes, description: str) -> Any:
    try:
        text = payload.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise ProviderOutputError("Invalid %s: %s" % (description, error)) from error


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as error:
        raise ProviderIntegrityError("Cannot hash %s: %s" % (path, error)) from error
    return digest.hexdigest()


def _normalise_sha256(value: Any, label: str) -> str:
    result = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(result):
        raise ProviderRegistryError("%s must be a lowercase SHA-256" % label)
    return result


def _inline_code_flags(host_name: str) -> set[str]:
    flags = _INTERPRETER_INLINE_FLAGS.get(host_name)
    if flags is not None:
        return flags
    if re.fullmatch(r"pythonw?(?:\d+(?:\.\d+)*)?(?:\.exe)?", host_name):
        return {"-c", "-m"}
    return set()


def _uses_inline_code_flag(argument: str, rejected_flags: set[str]) -> bool:
    lowered = argument.casefold()
    for flag in rejected_flags:
        lowered_flag = flag.casefold()
        if lowered == lowered_flag or lowered.startswith(lowered_flag + "="):
            return True
        if (
            len(lowered_flag) == 2
            and lowered.startswith(lowered_flag)
            and len(lowered) > 2
        ):
            return True
    return False


def _literal_file_candidates(argument: str) -> list[Path]:
    if "{" in argument or "}" in argument:
        return []
    values = [argument]
    if "=" in argument:
        values.append(argument.split("=", 1)[1])
    if argument.startswith("@"):
        values.append(argument[1:])
    if argument.startswith("-") and ":" in argument:
        values.append(argument.split(":", 1)[1])
    result: list[Path] = []
    for value in values:
        candidate = Path(value.strip().strip('"'))
        if candidate.is_absolute() and candidate.is_file():
            result.append(candidate)
    return result


def _require_string(value: Any, label: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise ProviderRegistryError("%s must be a non-empty bounded string" % label)
    return value


def _absolute_registry_file(value: Any, label: str) -> Path:
    raw = _require_string(value, label, maximum=32 * 1024)
    path = Path(raw)
    if not path.is_absolute():
        raise ProviderRegistryError("%s must be an absolute path" % label)
    return path


def _parse_provider(value: Any, index: int) -> ProviderSpec:
    if not isinstance(value, dict):
        raise ProviderRegistryError("providers[%d] must be an object" % index)
    allowed = {
        "providerId",
        "version",
        "executable",
        "sha256",
        "argsTemplate",
        "supportedSchemas",
        "buildPatterns",
        "timeoutSeconds",
        "fixedFileHashes",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ProviderRegistryError(
            "providers[%d] has unknown fields: %s" % (index, ", ".join(unknown))
        )

    provider_id = _require_string(value.get("providerId"), "providerId", maximum=128)
    if not _PROVIDER_ID_RE.fullmatch(provider_id):
        raise ProviderRegistryError("Invalid providerId: %s" % provider_id)
    version = _require_string(value.get("version"), "version", maximum=128)
    executable = _absolute_registry_file(value.get("executable"), "executable")
    host_name = executable.name.casefold()
    if host_name in _REJECTED_SHELL_HOSTS or executable.suffix.casefold() in {
        ".bat",
        ".cmd",
        ".ps1",
    }:
        raise ProviderRegistryError("provider executable must not be a command shell")
    executable_sha256 = _normalise_sha256(value.get("sha256"), "sha256")

    args_value = value.get("argsTemplate")
    if not isinstance(args_value, list) or len(args_value) > MAX_ARGUMENTS:
        raise ProviderRegistryError("argsTemplate must be a bounded JSON array")
    args: list[str] = []
    for argument in args_value:
        if not isinstance(argument, str) or len(argument.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise ProviderRegistryError("argsTemplate contains an invalid argument")
        placeholders = set(_PLACEHOLDER_RE.findall(argument))
        unknown_placeholders = placeholders - _ALLOWED_PLACEHOLDERS
        if unknown_placeholders:
            raise ProviderRegistryError(
                "Unknown argsTemplate placeholders: %s"
                % ", ".join(sorted(unknown_placeholders))
            )
        remainder = _PLACEHOLDER_RE.sub("", argument)
        if "{" in remainder or "}" in remainder or "\x00" in argument:
            raise ProviderRegistryError("argsTemplate has malformed braces or NUL")
        args.append(argument)

    schemas_value = value.get("supportedSchemas")
    if not isinstance(schemas_value, list) or not schemas_value:
        raise ProviderRegistryError("supportedSchemas must be a non-empty array")
    schemas = tuple(_require_string(item, "supportedSchemas[]") for item in schemas_value)
    if len(set(schemas)) != len(schemas) or set(schemas) - {SYMBOL_SCHEMA}:
        raise ProviderRegistryError(
            "supportedSchemas contains duplicates or unsupported service schemas"
        )

    patterns_value = value.get("buildPatterns")
    if not isinstance(patterns_value, list) or not patterns_value:
        raise ProviderRegistryError("buildPatterns must be a non-empty array")
    patterns = tuple(
        _require_string(item, "buildPatterns[]", maximum=512)
        for item in patterns_value
    )

    timeout = value.get("timeoutSeconds")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ProviderRegistryError("timeoutSeconds must be numeric")
    try:
        timeout_seconds = float(timeout)
    except (OverflowError, ValueError) as error:
        raise ProviderRegistryError("timeoutSeconds must be finite and numeric") from error
    if (
        not math.isfinite(timeout_seconds)
        or not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS
    ):
        raise ProviderRegistryError(
            "timeoutSeconds must be between %.2f and %.0f"
            % (MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)
        )

    fixed_value = value.get("fixedFileHashes", {})
    if not isinstance(fixed_value, dict):
        raise ProviderRegistryError("fixedFileHashes must be an object")
    fixed: list[tuple[Path, str]] = []
    fixed_path_keys: set[str] = set()
    executable_key = os.path.normcase(os.path.abspath(os.fspath(executable)))
    for raw_path, expected in fixed_value.items():
        path = _absolute_registry_file(raw_path, "fixedFileHashes key")
        path_key = os.path.normcase(os.path.abspath(os.fspath(path)))
        if path_key in fixed_path_keys or path_key == executable_key:
            raise ProviderRegistryError(
                "fixedFileHashes contains a duplicate or executable path"
            )
        fixed_path_keys.add(path_key)
        fixed.append((path, _normalise_sha256(expected, "fixedFileHashes value")))
    fixed.sort(key=lambda pair: os.fspath(pair[0]).casefold())

    # If the executable is an interpreter/host (Python, Java, dotnet, etc.),
    # hashing only that host would leave the actual provider program mutable.
    # Any literal absolute file argument that exists at registry load time must
    # therefore be pinned in fixedFileHashes. Placeholder paths are run data.
    for argument in args:
        for argument_path in _literal_file_candidates(argument):
            key = os.path.normcase(os.path.abspath(os.fspath(argument_path)))
            if key != executable_key and key not in fixed_path_keys:
                raise ProviderRegistryError(
                    "Literal file argument must be pinned in fixedFileHashes: %s"
                    % argument_path
                )

    rejected_flags = _inline_code_flags(host_name)
    if rejected_flags:
        if any(_uses_inline_code_flag(argument, rejected_flags) for argument in args):
            raise ProviderRegistryError(
                "interpreter provider must use a pinned script file, not inline/module code"
            )
        first_pinned_script: int | None = None
        for argument_index, argument in enumerate(args):
            if _PLACEHOLDER_RE.search(argument):
                continue
            argument_path = Path(argument)
            if not argument_path.is_absolute() or not argument_path.is_file():
                continue
            key = os.path.normcase(os.path.abspath(os.fspath(argument_path)))
            if key in fixed_path_keys:
                first_pinned_script = argument_index
                break
        if first_pinned_script is None:
            raise ProviderRegistryError(
                "interpreter provider requires an absolute pinned script file"
            )
        if any(
            _PLACEHOLDER_RE.search(argument)
            for argument in args[:first_pinned_script]
        ):
            raise ProviderRegistryError(
                "interpreter placeholders must occur after the pinned script file"
            )

    return ProviderSpec(
        provider_id=provider_id,
        version=version,
        executable=executable,
        executable_sha256=executable_sha256,
        args_template=tuple(args),
        supported_schemas=schemas,
        build_patterns=patterns,
        timeout_seconds=timeout_seconds,
        fixed_file_hashes=tuple(fixed),
    )


def load_registry(path: str | os.PathLike[str] | None = None) -> ProviderRegistry:
    """Load and strictly validate the local trusted-provider registry.

    Production callers should omit ``path`` so the user-config location is
    used.  An explicit path exists for administrative tooling and tests; the
    runner still refuses a registry located below the scanned asset root.
    """

    registry_path = Path(path) if path is not None else default_registry_path()
    if not registry_path.is_absolute():
        registry_path = registry_path.resolve()
    try:
        if registry_path.is_symlink() or not registry_path.is_file():
            raise ProviderRegistryError(
                "Provider registry must be a regular non-symlink file: %s"
                % registry_path
            )
        size = registry_path.stat().st_size
        if size <= 0 or size > MAX_REGISTRY_BYTES:
            raise ProviderRegistryError("Provider registry size is invalid")
        payload = registry_path.read_bytes()
    except ProviderRegistryError:
        raise
    except OSError as error:
        raise ProviderRegistryError(
            "Cannot read provider registry %s: %s" % (registry_path, error)
        ) from error

    try:
        value = _strict_json_loads(payload, "provider registry")
    except ProviderOutputError as error:
        raise ProviderRegistryError(str(error)) from error
    if not isinstance(value, dict):
        raise ProviderRegistryError("Provider registry root must be an object")
    if set(value) != {"schema", "schemaVersion", "providers"}:
        raise ProviderRegistryError("Provider registry root fields are not schema v1")
    if value.get("schema") != REGISTRY_SCHEMA or type(value.get("schemaVersion")) is not int:
        raise ProviderRegistryError("Unsupported provider registry schema")
    if value["schemaVersion"] != SCHEMA_VERSION:
        raise ProviderRegistryError("Unsupported provider registry schemaVersion")
    entries = value.get("providers")
    if not isinstance(entries, list):
        raise ProviderRegistryError("providers must be an array")

    providers: dict[str, ProviderSpec] = {}
    for index, entry in enumerate(entries):
        spec = _parse_provider(entry, index)
        if spec.provider_id in providers:
            raise ProviderRegistryError(
                "Duplicate providerId: %s" % spec.provider_id
            )
        providers[spec.provider_id] = spec
    return ProviderRegistry(
        path=registry_path.resolve(),
        sha256=hashlib.sha256(payload).hexdigest(),
        providers=MappingProxyType(providers),
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _verify_regular_hashed_file(
    path: Path, expected_hash: str, label: str, asset_root: Path
) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise ProviderIntegrityError("%s is not a regular file: %s" % (label, path))
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ProviderIntegrityError("Cannot resolve %s: %s" % (label, error)) from error
    if _is_within(resolved, asset_root):
        raise ProviderIntegrityError("%s must not reside in the asset tree" % label)
    actual = _hash_file(resolved)
    if not hmac.compare_digest(actual, expected_hash):
        raise ProviderIntegrityError(
            "%s SHA-256 mismatch for %s" % (label, resolved)
        )
    return actual


def _verify_provider_integrity(spec: ProviderSpec, asset_root: Path) -> str:
    executable_hash = _verify_regular_hashed_file(
        spec.executable, spec.executable_sha256, "provider executable", asset_root
    )
    for path, expected in spec.fixed_file_hashes:
        _verify_regular_hashed_file(path, expected, "provider fixed file", asset_root)
    return executable_hash


def _expand_arguments(
    spec: ProviderSpec,
    asset_root: Path,
    output_dir: Path,
    manifest_path: Path,
    build_id: str,
) -> list[str]:
    substitutions = {
        "assetRoot": os.fspath(asset_root),
        "outputDir": os.fspath(output_dir),
        "manifestPath": os.fspath(manifest_path),
        "buildId": build_id,
    }
    result: list[str] = []
    for template in spec.args_template:
        argument = _PLACEHOLDER_RE.sub(
            lambda match: substitutions[match.group(1)], template
        )
        if "\x00" in argument or len(argument.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise ProviderExecutionError("Expanded provider argument is invalid")
        result.append(argument)
    return result


def _minimal_environment(output_dir: Path) -> dict[str, str]:
    environment: dict[str, str] = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "TEMP": os.fspath(output_dir),
        "TMP": os.fspath(output_dir),
    }
    for name in ("SystemRoot", "SYSTEMROOT", "WINDIR", "ComSpec", "COMSPEC"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _stream_reader(
    stream: Any,
    destination: bytearray,
    exceeded: threading.Event,
    process: subprocess.Popen[bytes],
) -> None:
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            remaining = MAX_STDIO_BYTES - len(destination)
            if remaining > 0:
                destination.extend(chunk[:remaining])
            if len(chunk) > remaining:
                exceeded.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _assign_windows_kill_job(process: subprocess.Popen[bytes]) -> int | None:
    """Put a child in a kill-on-close Job Object when Windows permits it."""

    if os.name != "nt":
        return None
    api = _windows_job_api()
    if api is None:
        return None
    return _assign_windows_kill_job_with_api(process, api)


def _assign_windows_kill_job_with_api(
    process: subprocess.Popen[bytes], api: _WindowsJobApi
) -> int | None:
    """Assign one provider process while retaining the full-width job handle."""

    job: Any = None
    keep_job = False
    try:
        job = api.create_job(None, None)
        if not job:
            return None
        information = _WindowsJobExtendedLimits()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        configured = api.set_information(
            job, 9, _ctypes.byref(information), _ctypes.sizeof(information)
        )
        assigned = configured and api.assign_process(
            job, int(process._handle)
        )
        if not assigned:
            return None
        value = getattr(job, "value", job)
        keep_job = True
        return int(value)
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    finally:
        if job and not keep_job:
            try:
                api.close_handle(job)
            except Exception:
                pass


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    windows_job: int | None,
    output_dir: Path,
) -> None:
    """Best-effort bounded termination of the provider and its descendants."""

    if os.name == "nt" and windows_job:
        api = _windows_job_api()
        terminated = False
        closed = False
        if api is not None:
            try:
                terminated = bool(api.terminate_job(int(windows_job), 1))
            except (AttributeError, OSError, TypeError, ValueError):
                pass
            finally:
                try:
                    closed = bool(api.close_handle(int(windows_job)))
                except (AttributeError, OSError, TypeError, ValueError):
                    pass
        if terminated or closed:
            return
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass

    # Job assignment can be unavailable under an older/nonnestable host job.
    # Use the absolute Windows system tool only as a bounded fallback.
    if os.name == "nt" and not windows_job:
        system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT")
        taskkill = Path(system_root) / "System32" / "taskkill.exe" if system_root else None
        if taskkill is not None and taskkill.is_file():
            try:
                subprocess.run(
                    [
                        os.fspath(taskkill),
                        "/PID",
                        str(int(process.pid)),
                        "/T",
                        "/F",
                    ],
                    cwd=os.fspath(output_dir),
                    env=_minimal_environment(output_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    timeout=1.0,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass


def _execute(
    spec: ProviderSpec, command: list[str], output_dir: Path
) -> tuple[bytes, bytes, int]:
    creationflags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    try:
        process = subprocess.Popen(
            command,
            cwd=os.fspath(output_dir),
            env=_minimal_environment(output_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    except OSError as error:
        raise ProviderExecutionError("Cannot start trusted provider: %s" % error) from error

    windows_job = _assign_windows_kill_job(process)

    assert process.stdout is not None
    assert process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    exceeded = threading.Event()
    threads = (
        threading.Thread(
            target=_stream_reader,
            args=(process.stdout, stdout, exceeded, process),
            daemon=True,
        ),
        threading.Thread(
            target=_stream_reader,
            args=(process.stderr, stderr, exceeded, process),
            daemon=True,
        ),
    )
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        process.wait(timeout=spec.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            _terminate_process_tree(process, windows_job, output_dir)
            windows_job = None
        except Exception:
            # Termination errors must not skip the bounded wait below.
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if windows_job is not None or os.name != "nt":
            _terminate_process_tree(process, windows_job, output_dir)
            windows_job = None
        join_deadline = time.monotonic() + 1.0
        for thread in threads:
            thread.join(timeout=max(0.0, join_deadline - time.monotonic()))

    pipe_join_failed = any(thread.is_alive() for thread in threads)

    if timed_out:
        raise ProviderTimeoutError(
            "Trusted provider %s exceeded %.3f seconds"
            % (spec.provider_id, spec.timeout_seconds)
        )
    if exceeded.is_set():
        raise ProviderExecutionError(
            "Trusted provider stdout or stderr exceeded %d bytes"
            % MAX_STDIO_BYTES
        )
    if pipe_join_failed:
        raise ProviderExecutionError(
            "Trusted provider pipe readers did not close after process-tree termination"
        )
    if process.returncode != 0:
        stderr_text = bytes(stderr).decode("utf-8", errors="replace")
        raise ProviderExecutionError(
            "Trusted provider %s exited with code %d: %s"
            % (spec.provider_id, process.returncode, stderr_text[-4096:])
        )
    return bytes(stdout), bytes(stderr), int(process.returncode)


def _safe_relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProviderOutputError("%s must be a non-empty relative path" % label)
    normalised = value.replace("\\", "/")
    if (
        normalised.startswith("/")
        or normalised.startswith("//")
        or re.match(r"^[A-Za-z]:", normalised)
    ):
        raise ProviderOutputError("%s must not be absolute" % label)
    parts = normalised.split("/")
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise ProviderOutputError("%s contains an unsafe path component" % label)
    return "/".join(parts)


def _read_artifact(
    output_dir: Path,
    relative: str,
    collected: dict[str, ProviderArtifact],
    current_total: int,
) -> int:
    if relative in collected:
        return current_total
    candidate = output_dir.joinpath(*relative.split("/"))
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise ProviderOutputError("Referenced artifact is not a regular file: %s" % relative)
        resolved = candidate.resolve(strict=True)
        if not _is_within(resolved, output_dir):
            raise ProviderOutputError("Referenced artifact escapes output directory: %s" % relative)
        size = resolved.stat().st_size
        if size < 0 or current_total + size > MAX_ARTIFACT_BYTES:
            raise ProviderOutputError("Provider artifacts exceed the in-memory limit")
        data = resolved.read_bytes()
    except ProviderOutputError:
        raise
    except OSError as error:
        raise ProviderOutputError("Cannot read artifact %s: %s" % (relative, error)) from error
    collected[relative] = ProviderArtifact(
        relative_path=relative,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )
    return current_total + len(data)


def _validate_nested_paths(
    value: Any,
    output_dir: Path,
    collected: dict[str, ProviderArtifact],
    current_total: int,
    path: str,
) -> int:
    if isinstance(value, dict):
        for key, child in value.items():
            key_normalised = key.replace("-", "").replace("_", "").casefold()
            child_path = "%s.%s" % (path, key)
            if key_normalised in _OUTPUT_PATH_KEYS and child:
                relative = _safe_relative_path(child, child_path)
                current_total = _read_artifact(
                    output_dir, relative, collected, current_total
                )
            elif key_normalised in _LOGICAL_PATH_KEYS and child:
                _safe_relative_path(child, child_path)
            else:
                current_total = _validate_nested_paths(
                    child, output_dir, collected, current_total, child_path
                )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            current_total = _validate_nested_paths(
                child,
                output_dir,
                collected,
                current_total,
                "%s[%d]" % (path, index),
            )
    return current_total


def _validate_no_trust_assertions(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ProviderOutputError("Non-string JSON key at %s" % path)
            normalised = key.lstrip("_").replace("-", "").replace("_", "").casefold()
            if key.startswith("_") or normalised in _RESERVED_TRUST_KEYS:
                raise ProviderOutputError(
                    "Provider output may not assert service trust at %s.%s"
                    % (path, key)
                )
            _validate_no_trust_assertions(child, "%s.%s" % (path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_no_trust_assertions(child, "%s[%d]" % (path, index))


def _validate_manifest_shape(
    manifest: Any,
    spec: ProviderSpec,
    build_id: str,
    output_dir: Path,
) -> tuple[dict[str, Any], tuple[ProviderArtifact, ...]]:
    if not isinstance(manifest, dict):
        raise ProviderOutputError("Provider manifest root must be an object")
    allowed_root = {
        "schema",
        "schemaVersion",
        "buildId",
        "provider",
        "engine",
        "platform",
        "api",
        "generatedAt",
        "metadata",
        "artifacts",
        "shaders",
    }
    unknown_root = sorted(set(manifest) - allowed_root)
    if unknown_root:
        raise ProviderOutputError(
            "Provider manifest has unknown root fields: %s"
            % ", ".join(unknown_root)
        )
    schema = manifest.get("schema")
    if schema not in spec.supported_schemas or schema != SYMBOL_SCHEMA:
        raise ProviderOutputError("Unsupported provider output schema: %s" % schema)
    if type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 1:
        raise ProviderOutputError("Provider output must declare integer schemaVersion 1")
    declared_build = manifest.get("buildId", "")
    if declared_build and not isinstance(declared_build, str):
        raise ProviderOutputError("Provider buildId must be a string")
    if build_id and declared_build and declared_build != build_id:
        raise ProviderOutputError("Provider output buildId does not match the run")

    provider = manifest.get("provider", {})
    if provider and not isinstance(provider, dict):
        raise ProviderOutputError("provider metadata must be an object")
    if isinstance(provider, dict):
        declared_id = provider.get("providerId", provider.get("id", provider.get("name", "")))
        declared_version = provider.get("version", "")
        if declared_id and declared_id != spec.provider_id:
            raise ProviderOutputError("Provider output ID does not match the registry")
        if declared_version and declared_version != spec.version:
            raise ProviderOutputError("Provider output version does not match the registry")

    shaders = manifest.get("shaders")
    if not isinstance(shaders, list) or len(shaders) > MAX_SHADERS:
        raise ProviderOutputError("shaders must be a bounded array")

    _validate_no_trust_assertions(manifest)
    collected: dict[str, ProviderArtifact] = {}
    total = 0

    artifacts = manifest.get("artifacts", [])
    if artifacts and not isinstance(artifacts, list):
        raise ProviderOutputError("artifacts must be an array")
    for index, artifact in enumerate(artifacts or []):
        if not isinstance(artifact, dict) or set(artifact) - {"path", "sha256", "size"}:
            raise ProviderOutputError("artifacts[%d] is not schema v1" % index)
        relative = _safe_relative_path(artifact.get("path"), "artifacts[%d].path" % index)
        total = _read_artifact(output_dir, relative, collected, total)
        actual = collected[relative]
        declared_hash = artifact.get("sha256", "")
        if declared_hash and (
            not isinstance(declared_hash, str)
            or not _SHA256_RE.fullmatch(declared_hash.lower())
            or not hmac.compare_digest(declared_hash.lower(), actual.sha256)
        ):
            raise ProviderOutputError("Artifact SHA-256 mismatch: %s" % relative)
        declared_size = artifact.get("size")
        if declared_size is not None and (
            type(declared_size) is not int or declared_size != len(actual.data)
        ):
            raise ProviderOutputError("Artifact size mismatch: %s" % relative)

    # Validate path-like fields at every nesting level so a future consumer
    # cannot accidentally start trusting a path hidden in metadata we did not
    # understand when schema v1 was implemented.
    total = _validate_nested_paths(
        manifest, output_dir, collected, total, "$"
    )

    for index, shader in enumerate(shaders):
        if not isinstance(shader, dict):
            raise ProviderOutputError("shaders[%d] must be an object" % index)
        raw_hash = shader.get("rawSha256", "")
        code_hash = shader.get("codeSha256", shader.get("executableSha256", ""))
        if not raw_hash and not code_hash:
            raise ProviderOutputError("shaders[%d] lacks a stable hash" % index)
        for label, digest in (("rawSha256", raw_hash), ("codeSha256", code_hash)):
            if digest and (
                not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest.lower())
            ):
                raise ProviderOutputError("shaders[%d].%s is invalid" % (index, label))
        blob_value = shader.get("blobPath", shader.get("containerPath", ""))
        if blob_value:
            relative = _safe_relative_path(blob_value, "shaders[%d].blobPath" % index)
            artifact = collected[relative]
            if raw_hash and not hmac.compare_digest(raw_hash.lower(), artifact.sha256):
                raise ProviderOutputError(
                    "shaders[%d] rawSha256 does not match blobPath" % index
                )

    ordered = tuple(collected[key] for key in sorted(collected))
    return manifest, ordered


def _attestation_payload(attestation: ProviderAttestation) -> bytes:
    value = attestation.as_dict()
    value.pop("signature", None)
    return _canonical_json_bytes(value)


def _artifact_set_hash(artifacts: Sequence[ProviderArtifact]) -> str:
    material = [
        {
            "path": artifact.relative_path,
            "sha256": artifact.sha256,
            "size": len(artifact.data),
        }
        for artifact in artifacts
    ]
    return hashlib.sha256(_canonical_json_bytes(material)).hexdigest()


def _verify_output(
    output_directory: str | os.PathLike[str],
    provider: ProviderSpec,
    *,
    build_id: str,
    executable_sha256: str,
    registry_sha256: str,
    asset_root_sha256: str,
    run_id: str,
    duration_ms: int,
    stdout: bytes = b"",
    stderr: bytes = b"",
    _verification_token: object,
) -> TrustedProviderResult:
    """Validate schema-v1 output and issue a process-local service attestation.

    This helper is callable only with the module-owned token after a successful
    provider process run. Scanner integration consumes the resulting capability
    object and never calls this output validator directly.
    """

    if _verification_token is not _OUTPUT_VERIFICATION_TOKEN:
        raise ProviderIntegrityError("Provider output verification is service-owned")

    if not hmac.compare_digest(executable_sha256, provider.executable_sha256):
        raise ProviderIntegrityError("Attested executable hash does not match registry policy")
    if not _SHA256_RE.fullmatch(registry_sha256):
        raise ProviderIntegrityError("Registry SHA-256 is invalid")
    if not _SHA256_RE.fullmatch(asset_root_sha256):
        raise ProviderIntegrityError("Asset-root SHA-256 is invalid")
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ProviderIntegrityError("Provider run ID is invalid")
    if not any(
        fnmatch.fnmatchcase(build_id, pattern) for pattern in provider.build_patterns
    ):
        raise ProviderOutputError("Provider output build is outside registered patterns")
    if len(stdout) > MAX_STDIO_BYTES or len(stderr) > MAX_STDIO_BYTES:
        raise ProviderOutputError("Provider stdio exceeds the verification limit")

    supplied_output_dir = Path(output_directory)
    if supplied_output_dir.is_symlink():
        raise ProviderOutputError("Provider output directory must not be a symlink")
    output_dir = supplied_output_dir.resolve(strict=True)
    manifest_path = output_dir / OUTPUT_MANIFEST_FILENAME
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ProviderOutputError("Provider did not create %s" % OUTPUT_MANIFEST_FILENAME)
        resolved_manifest = manifest_path.resolve(strict=True)
        if not _is_within(resolved_manifest, output_dir):
            raise ProviderOutputError("Provider manifest escapes output directory")
        size = resolved_manifest.stat().st_size
        if size <= 0 or size > MAX_MANIFEST_BYTES:
            raise ProviderOutputError("Provider manifest size is invalid")
        payload = resolved_manifest.read_bytes()
    except ProviderOutputError:
        raise
    except OSError as error:
        raise ProviderOutputError("Cannot read provider manifest: %s" % error) from error

    manifest = _strict_json_loads(payload, "provider output")
    manifest, artifacts = _validate_manifest_shape(
        manifest, provider, build_id, output_dir
    )
    canonical = _canonical_json_bytes(manifest)
    manifest_hash = hashlib.sha256(canonical).hexdigest()
    issued_at = time.time_ns()
    unsigned = ProviderAttestation(
        run_id=run_id,
        provider_id=provider.provider_id,
        provider_version=provider.version,
        provider_binary_sha256=executable_sha256,
        registry_sha256=registry_sha256,
        asset_root_sha256=asset_root_sha256,
        output_schema=manifest["schema"],
        manifest_sha256=manifest_hash,
        artifacts_sha256=_artifact_set_hash(artifacts),
        build_id=build_id,
        provenance=PROVENANCE,
        issued_at_unix_ns=issued_at,
        duration_ms=max(0, int(duration_ms)),
        signature="",
    )
    signature = hmac.new(
        _ATTESTATION_KEY, _attestation_payload(unsigned), hashlib.sha256
    ).hexdigest()
    attestation = ProviderAttestation(
        **{
            **unsigned.__dict__,
            "signature": signature,
        }
    )
    return TrustedProviderResult(
        canonical,
        artifacts,
        attestation,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
        _constructor_token=_RESULT_CONSTRUCTOR_TOKEN,
    )


def verify_attestation(result: TrustedProviderResult) -> bool:
    """Return whether a result is intact and was attested by this process."""

    if not isinstance(result, TrustedProviderResult):
        return False
    try:
        attestation = result.attestation
        if (
            hashlib.sha256(result.manifest_bytes).hexdigest()
            != attestation.manifest_sha256
        ):
            return False
        if _artifact_set_hash(result.artifacts) != attestation.artifacts_sha256:
            return False
        expected = hmac.new(
            _ATTESTATION_KEY, _attestation_payload(attestation), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, attestation.signature)
    except (AttributeError, TypeError, ValueError, RecursionError):
        return False


def run_provider(
    provider_id: str,
    asset_root: str | os.PathLike[str],
    *,
    build_id: str = "",
) -> TrustedProviderResult:
    """Run one pre-registered provider with fixed policy and arguments.

    ``asset_root`` and ``build_id`` are data context, not command selection.
    Executable, fixed arguments, schemas, timeout, and code hashes all come
    exclusively from the user-owned registry.
    """

    # Intentionally no registry/executable/arguments override exists here.
    # Tests may replace default_registry_path in-process, while production
    # always resolves policy from the fixed per-user config location.
    loaded = load_registry()
    spec = loaded.get(provider_id)
    if not isinstance(build_id, str) or len(build_id) > 512 or "\x00" in build_id:
        raise ProviderExecutionError("build_id is invalid")
    if not any(fnmatch.fnmatchcase(build_id, pattern) for pattern in spec.build_patterns):
        raise ProviderExecutionError(
            "Provider %s does not support build %r" % (provider_id, build_id)
        )

    try:
        root = Path(asset_root).resolve(strict=True)
    except OSError as error:
        raise ProviderExecutionError("Cannot resolve asset root: %s" % error) from error
    if not root.is_dir():
        raise ProviderExecutionError("asset_root must be a directory")
    if _is_within(loaded.path, root):
        raise ProviderRegistryError("Provider registry must not reside in the asset tree")

    executable_hash = _verify_provider_integrity(spec, root)
    run_id = secrets.token_hex(16)
    start_ns = time.monotonic_ns()
    with tempfile.TemporaryDirectory(
        prefix=".renderdoc-ultra-provider-", dir=os.fspath(loaded.path.parent)
    ) as temporary:
        output_dir = Path(temporary).resolve(strict=True)
        if _is_within(output_dir, root):
            raise ProviderExecutionError("Provider output directory overlaps the asset tree")
        manifest_path = output_dir / OUTPUT_MANIFEST_FILENAME
        output_stat = output_dir.stat()
        output_identity = (output_stat.st_dev, output_stat.st_ino)
        arguments = _expand_arguments(
            spec, root, output_dir, manifest_path, build_id
        )
        command = [os.fspath(spec.executable.resolve(strict=True)), *arguments]
        stdout = b""
        stderr = b""
        execution_error: ProviderError | None = None
        try:
            stdout, stderr, _returncode = _execute(spec, command, output_dir)
        except ProviderError as error:
            execution_error = error

        # Detect policy/code changes during the run before accepting any output.
        executable_hash_after = _verify_provider_integrity(spec, root)
        if not hmac.compare_digest(executable_hash, executable_hash_after):
            raise ProviderIntegrityError("Provider executable changed during execution")
        if loaded.path.is_symlink() or not loaded.path.is_file():
            raise ProviderIntegrityError("Provider registry was replaced during execution")
        if _hash_file(loaded.path) != loaded.sha256:
            raise ProviderIntegrityError("Provider registry changed during execution")
        if output_dir.is_symlink():
            raise ProviderOutputError("Provider replaced its output directory with a link")
        try:
            resolved_output = output_dir.resolve(strict=True)
            current_stat = resolved_output.stat()
        except OSError as error:
            raise ProviderOutputError(
                "Provider output directory disappeared: %s" % error
            ) from error
        if resolved_output != output_dir or (
            output_identity[1]
            and current_stat.st_ino
            and output_identity != (current_stat.st_dev, current_stat.st_ino)
        ):
            raise ProviderOutputError("Provider replaced or redirected its output directory")
        if execution_error is not None:
            raise execution_error

        duration_ms = (time.monotonic_ns() - start_ns) // 1_000_000
        result = _verify_output(
            output_dir,
            spec,
            build_id=build_id,
            executable_sha256=executable_hash,
            registry_sha256=loaded.sha256,
            asset_root_sha256=hashlib.sha256(
                os.path.normcase(os.fspath(root)).encode("utf-8")
            ).hexdigest(),
            run_id=run_id,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            _verification_token=_OUTPUT_VERIFICATION_TOKEN,
        )
        if not verify_attestation(result):
            raise ProviderIntegrityError("Internal provider attestation failure")
        return result


__all__ = [
    "ProviderArtifact",
    "ProviderAttestation",
    "ProviderError",
    "ProviderExecutionError",
    "ProviderIntegrityError",
    "ProviderOutputError",
    "ProviderRegistry",
    "ProviderRegistryError",
    "ProviderSpec",
    "ProviderTimeoutError",
    "TrustedProviderResult",
    "UnknownProviderError",
    "default_registry_path",
    "load_registry",
    "run_provider",
    "verify_attestation",
]
