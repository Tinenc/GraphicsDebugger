"""User-owned, ID-addressed external shader tool registry and runner."""

from __future__ import annotations

import ctypes as _ctypes
import hashlib
import hmac
import json
import math
import os
import re
import signal
import string
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ToolConfigurationError, ToolExecutionError
from .io import atomic_write_json, read_json_object

TOOLS_SCHEMA = "renderdoc-ultra-shader-tools/v1"
TOOLS_VERSION = 1
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_ARGUMENTS = 128
MAX_ARGUMENT_CHARS = 32768
MAX_LOG_BYTES = 8 * 1024 * 1024
TOOL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
PLACEHOLDERS = frozenset(
    {
        "input",
        "output",
        "baseline",
        "job_dir",
        "workspace_dir",
        "stage",
        "entry_point",
        "captured_entry_point",
        "revision",
        "capture_id",
        "raw_sha256",
        "target_profile",
        "target_api",
    }
)
_REJECTED_SHELL_HOSTS = frozenset(
    {
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "sh",
        "bash",
        "zsh",
    }
)
_INLINE_CODE_FLAGS = {
    "python": {"-c", "-m"},
    "python.exe": {"-c", "-m"},
    "python3": {"-c", "-m"},
    "python3.exe": {"-c", "-m"},
    "node": {"-e", "--eval"},
    "node.exe": {"-e", "--eval"},
    "py": {"-c", "-m"},
    "py.exe": {"-c", "-m"},
}
_HIGH_LEVEL_SOURCE_SUFFIXES = (
    ".hlsl",
    ".glsl",
    ".essl",
    ".slang",
    ".wgsl",
    ".metal",
)
_UNTRACKED_PREPROCESSOR_DEPENDENCY = re.compile(
    r"(?im)^[ \t]*#[ \t]*(?:include(?:_next)?|line)\b"
)
_UNTRACKED_SLANG_IMPORT = re.compile(r"(?im)^[ \t]*import\s+[A-Za-z_]\w*")


@dataclass(frozen=True, slots=True)
class _WindowsJobApi:
    create_job: Any
    set_information: Any
    assign_process: Any
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
    """Return one typed WinAPI table for all external shader tools."""

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
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [_ctypes.c_void_p]
        close_handle.restype = _ctypes.c_int
        _WINDOWS_JOB_API = _WindowsJobApi(
            create_job=create_job,
            set_information=set_information,
            assign_process=assign_process,
            close_handle=close_handle,
        )
        return _WINDOWS_JOB_API


def _reject_untracked_compile_dependencies(raw_input: str, *,
                                           source_unit: Mapping[str, Any] | None = None,
                                           job_dir: Path | None = None) -> None:
    """Reject source dependencies absent from the immutable compile receipt.

    Include files are permitted only when the Host supplies and revalidates the
    frozen compilation-unit graph. Ordinary untracked includes/modules remain
    unavailable; a path merely living under a job directory is not evidence.
    """

    input_path = Path(str(raw_input or ""))
    if source_unit is not None:
        from .compile_source_unit import validate_snapshot
        try:
            root = Path(source_unit["workspaceRoot"])
            proof = source_unit["proof"]
            if (input_path.resolve() != (root / proof["inputPath"]).resolve()
                    or job_dir is None or input_path.resolve().parent != job_dir.resolve()):
                raise ValueError("compiler input does not match its include snapshot")
            validate_snapshot(root, Path(source_unit["sourcePath"]), source_unit["source"], proof)
            for document in proof["documents"]:
                text = (root / document["inputPath"]).read_text(encoding="utf-8")
                if re.search(r"(?im)^[ \t]*#[ \t]*line\b", text) or _UNTRACKED_SLANG_IMPORT.search(text):
                    raise ValueError("line/module directives are not tracked include edges")
            return
        except (ValueError, TypeError, KeyError, OSError) as error:
            raise ToolExecutionError("compile include snapshot is invalid: %s" % error) from error
    if not input_path.name.casefold().endswith(_HIGH_LEVEL_SOURCE_SUFFIXES):
        return
    if input_path.is_symlink() or not input_path.is_file():
        raise ToolExecutionError(
            "compile input must be a regular non-symlink source snapshot"
        )
    size = input_path.stat().st_size
    if size <= 0 or size > 64 * 1024 * 1024:
        raise ToolExecutionError("compile source size is invalid")
    try:
        text = input_path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ToolExecutionError("compile source must be strict UTF-8") from exc
    if _UNTRACKED_PREPROCESSOR_DEPENDENCY.search(text):
        raise ToolExecutionError(
            "compile source contains #include/#line without a hash-bound dependency graph"
        )
    if input_path.name.casefold().endswith(".slang") and _UNTRACKED_SLANG_IMPORT.search(
        text
    ):
        raise ToolExecutionError(
            "Slang import is unavailable until module dependencies are hash-bound"
        )


def _inline_code_flags(host_name: str) -> set[str]:
    flags = _INLINE_CODE_FLAGS.get(host_name)
    if flags is not None:
        return flags
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", host_name):
        return {"-c", "-m"}
    return set()


def _uses_inline_code_flag(argument: str, rejected_flags: set[str]) -> bool:
    lowered = argument.casefold()
    for flag in rejected_flags:
        flag = flag.casefold()
        if lowered == flag or lowered.startswith(flag + "="):
            return True
        # CPython and Node accept compact forms such as -cCODE, -mmodule,
        # and -eCODE. Only apply this rule to the one-letter switches.
        if len(flag) == 2 and lowered.startswith(flag) and len(lowered) > 2:
            return True
    return False


@dataclass(frozen=True)
class ToolOperation:
    tool_id: str
    operation: str
    executable: Path
    executable_sha256: str
    fixed_file_hashes: tuple[tuple[Path, str], ...]
    arguments: tuple[str, ...]
    placeholders: frozenset[str]
    timeout_seconds: float
    config_digest: str

    def expand(self, values: Mapping[str, str]) -> list[str]:
        # ``captured_entry_point`` was added after the original registry
        # contract so a SPIR-V module's physical entry can be kept separate
        # from the capture-visible HLSL name.  Old/custom operations which do
        # not use that distinction remain deterministic: their captured name
        # is exactly their already supplied entry point.  First-party routes
        # pass both values explicitly.
        expanded_values = dict(values)
        if "captured_entry_point" not in expanded_values:
            entry_point = expanded_values.get("entry_point")
            if entry_point is not None:
                expanded_values["captured_entry_point"] = entry_point
        missing_values = PLACEHOLDERS.difference(expanded_values)
        if missing_values:
            raise ToolConfigurationError(
                "internal placeholder values are missing: %s"
                % ", ".join(sorted(missing_values))
            )
        return [str(self.executable)] + [
            argument.format_map(expanded_values) for argument in self.arguments
        ]

    def fixed_file_hashes_dict(self) -> dict[str, str]:
        return {str(path): expected for path, expected in self.fixed_file_hashes}


@dataclass(frozen=True)
class ProcessResult:
    job_dir: Path
    command: tuple[str, ...]
    return_code: int | None
    timed_out: bool
    launch_error: str | None
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_path: Path
    stderr_path: Path


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _minimal_environment(job_dir: Path) -> dict[str, str]:
    temporary = job_dir / "temp"
    temporary.mkdir(parents=False, exist_ok=False)
    environment = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "TEMP": str(temporary),
        "TMP": str(temporary),
    }
    for name in ("SystemRoot", "SYSTEMROOT", "WINDIR", "ComSpec", "COMSPEC"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _integrity_error(operation: ToolOperation) -> str | None:
    if operation.executable.is_symlink() or not operation.executable.is_file():
        return "executable is no longer a regular non-symlink file"
    try:
        actual = _file_sha256(operation.executable)
    except OSError as exc:
        return "%s: %s" % (type(exc).__name__, exc)
    if not hmac.compare_digest(actual, operation.executable_sha256):
        return "executable SHA-256 changed after tool registry load"
    for path, expected in operation.fixed_file_hashes:
        if path.is_symlink() or not path.is_file():
            return "fixed file is no longer regular/non-symlink: %s" % path
        try:
            actual = _file_sha256(path)
        except OSError as exc:
            return "fixed file cannot be hashed (%s): %s" % (path, exc)
        if not hmac.compare_digest(actual, expected):
            return "fixed file SHA-256 changed after tool registry load: %s" % path
    return None


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
        value = value.strip().strip('"')
        candidate = Path(value)
        if candidate.is_absolute() and candidate.is_file():
            result.append(candidate)
    return result


def _attach_windows_kill_job(process: subprocess.Popen) -> int | None:
    """Best-effort Job Object that kills descendants when its handle closes."""

    if os.name != "nt":
        return None
    api = _windows_job_api()
    if api is None:
        return None
    return _attach_windows_kill_job_with_api(process, api)


def _attach_windows_kill_job_with_api(
    process: subprocess.Popen, api: _WindowsJobApi
) -> int | None:
    """Attach one tool process while retaining the full-width job handle."""

    job: Any = None
    keep_job = False
    try:
        job = api.create_job(None, None)
        if not job:
            return None
        information = _WindowsJobExtendedLimits()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not api.set_information(
            job, 9, _ctypes.byref(information), _ctypes.sizeof(information)
        ):
            return None
        if not api.assign_process(job, int(process._handle)):
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


def _close_windows_handle(handle: int) -> None:
    if os.name != "nt" or not handle:
        return
    api = _windows_job_api()
    if api is None:
        return
    try:
        api.close_handle(int(handle))
    except (AttributeError, OSError, TypeError, ValueError):
        pass


def _terminate_process_tree(
    process: subprocess.Popen,
    windows_job: int | None,
    environment: Mapping[str, str],
    force_fallback: bool,
) -> None:
    if windows_job:
        _close_windows_handle(windows_job)
    elif os.name == "nt" and force_fallback:
        system_root = environment.get("SystemRoot") or environment.get("WINDIR")
        taskkill = Path(system_root or "C:/Windows") / "System32" / "taskkill.exe"
        if taskkill.is_file():
            try:
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    env=dict(environment),
                    timeout=5,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def _declared_sha256(value: Any, field: str) -> str:
    result = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", result):
        raise ToolConfigurationError("%s must be a SHA-256 hex digest" % field)
    return result


def _drain_bounded(
    source, destination: Path, maximum_bytes: int, state: dict, key: str
):
    written = 0
    truncated = False
    try:
        with destination.open("wb") as output:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                remaining = maximum_bytes - written
                if remaining > 0:
                    accepted = chunk[:remaining]
                    output.write(accepted)
                    written += len(accepted)
                if len(chunk) > max(remaining, 0):
                    truncated = True
    except (OSError, ValueError) as exc:
        state[key + "Error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            source.close()
        except (OSError, ValueError):
            pass
        state[key] = truncated


def _validate_template(argument: str, field: str) -> set[str]:
    if not isinstance(argument, str):
        raise ToolConfigurationError("%s must be a string" % field)
    if "\x00" in argument:
        raise ToolConfigurationError("%s must not contain NUL" % field)
    names: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(argument))
    except ValueError as exc:
        raise ToolConfigurationError("%s has invalid template braces" % field) from exc
    for unused_literal, name, format_spec, conversion in parsed:
        if name is None:
            continue
        if name not in PLACEHOLDERS:
            raise ToolConfigurationError(
                "%s uses unknown placeholder {%s}" % (field, name)
            )
        if format_spec or conversion:
            raise ToolConfigurationError(
                "%s must not use placeholder conversions or format specs" % field
            )
        names.add(name)
    return names


class ToolRegistry:
    """Immutable snapshot of a local JSON tool registry.

    Runtime APIs only accept a tool ID. Executables and argument templates are
    loaded here and cannot be supplied through a decompile/compile request.
    """

    def __init__(self, config_path: str | Path):
        self.path = Path(config_path).expanduser().resolve()
        try:
            config = read_json_object(self.path, MAX_CONFIG_BYTES, "shader tool config")
        except ValueError as exc:
            raise ToolConfigurationError(str(exc)) from exc
        allowed_top = {"schema", "schemaVersion", "tools"}
        unknown_top = set(config).difference(allowed_top)
        if unknown_top:
            raise ToolConfigurationError(
                "unknown shader tool config fields: %s" % ", ".join(sorted(unknown_top))
            )
        if (
            config.get("schema") != TOOLS_SCHEMA
            or config.get("schemaVersion") != TOOLS_VERSION
        ):
            raise ToolConfigurationError(
                "unsupported shader tool config schema/version"
            )
        tools = config.get("tools")
        if not isinstance(tools, dict) or not tools:
            raise ToolConfigurationError(
                "shader tool config tools must be a non-empty object"
            )
        self._operations: dict[tuple[str, str], ToolOperation] = {}
        for tool_id, record in tools.items():
            self._load_tool(tool_id, record)

    def _load_tool(self, tool_id: Any, record: Any) -> None:
        if not isinstance(tool_id, str) or not TOOL_ID_RE.fullmatch(tool_id):
            raise ToolConfigurationError("invalid tool id: %s" % tool_id)
        if not isinstance(record, dict):
            raise ToolConfigurationError("tool %s must be an object" % tool_id)
        unknown = set(record).difference(
            {
                "executable",
                "executableSha256",
                "fixedFileHashes",
                "decompile",
                "compile",
                "transform",
                "validate",
            }
        )
        if unknown:
            raise ToolConfigurationError(
                "tool %s has unknown fields: %s" % (tool_id, ", ".join(sorted(unknown)))
            )
        executable_value = record.get("executable")
        if not isinstance(executable_value, str) or not executable_value.strip():
            raise ToolConfigurationError("tool %s executable is required" % tool_id)
        executable_source = Path(executable_value).expanduser()
        if not executable_source.is_absolute():
            raise ToolConfigurationError(
                "tool %s executable must be absolute" % tool_id
            )
        if executable_source.is_symlink() or not executable_source.is_file():
            raise ToolConfigurationError(
                "tool %s executable must be a regular non-symlink file: %s"
                % (tool_id, executable_source)
            )
        executable = executable_source.resolve()
        host_name = executable.name.lower()
        if host_name in _REJECTED_SHELL_HOSTS or executable.suffix.lower() in {
            ".bat",
            ".cmd",
        }:
            raise ToolConfigurationError(
                "tool %s executable must not be a command shell" % tool_id
            )
        executable_sha256 = _declared_sha256(
            record.get("executableSha256"),
            "tools.%s.executableSha256" % tool_id,
        )
        actual_executable_sha256 = _file_sha256(executable)
        if executable_sha256 != actual_executable_sha256:
            raise ToolConfigurationError(
                "tool %s executableSha256 does not match executable bytes" % tool_id
            )
        if "fixedFileHashes" not in record:
            raise ToolConfigurationError(
                "tool %s fixedFileHashes is required (use an empty object when none)"
                % tool_id
            )
        fixed_value = record.get("fixedFileHashes")
        if not isinstance(fixed_value, dict):
            raise ToolConfigurationError(
                "tool %s fixedFileHashes must be an object" % tool_id
            )
        fixed: list[tuple[Path, str]] = []
        fixed_keys: set[str] = set()
        for raw_path, raw_hash in fixed_value.items():
            if not isinstance(raw_path, str) or not raw_path:
                raise ToolConfigurationError(
                    "tool %s fixedFileHashes keys must be paths" % tool_id
                )
            source_path = Path(raw_path).expanduser()
            if not source_path.is_absolute():
                raise ToolConfigurationError(
                    "tool %s fixedFileHashes paths must be absolute" % tool_id
                )
            if source_path.is_symlink() or not source_path.is_file():
                raise ToolConfigurationError(
                    "tool %s fixed file must be regular and non-symlink: %s"
                    % (tool_id, source_path)
                )
            fixed_path = source_path.resolve()
            key = _path_key(fixed_path)
            if key in fixed_keys or key == _path_key(executable):
                raise ToolConfigurationError(
                    "tool %s fixedFileHashes has a duplicate/executable path" % tool_id
                )
            fixed_keys.add(key)
            expected_hash = _declared_sha256(
                raw_hash, "tools.%s.fixedFileHashes value" % tool_id
            )
            if not hmac.compare_digest(_file_sha256(fixed_path), expected_hash):
                raise ToolConfigurationError(
                    "tool %s fixed file SHA-256 mismatch: %s" % (tool_id, fixed_path)
                )
            fixed.append((fixed_path, expected_hash))
        fixed.sort(key=lambda pair: str(pair[0]).casefold())
        for operation_name in ("decompile", "compile", "transform", "validate"):
            operation = record.get(operation_name)
            if operation is None:
                continue
            if not isinstance(operation, dict):
                raise ToolConfigurationError(
                    "tool %s %s must be an object" % (tool_id, operation_name)
                )
            unknown_operation = set(operation).difference({"args", "timeoutSeconds"})
            if unknown_operation:
                raise ToolConfigurationError(
                    "tool %s %s has unknown fields: %s"
                    % (tool_id, operation_name, ", ".join(sorted(unknown_operation)))
                )
            arguments = operation.get("args")
            if (
                not isinstance(arguments, list)
                or not arguments
                or len(arguments) > MAX_ARGUMENTS
            ):
                raise ToolConfigurationError(
                    "tool %s %s args must contain 1..%d strings"
                    % (tool_id, operation_name, MAX_ARGUMENTS)
                )
            placeholders: set[str] = set()
            character_count = 0
            rejected_flags = _inline_code_flags(host_name)
            if any(
                isinstance(argument, str)
                and _uses_inline_code_flag(argument, rejected_flags)
                for argument in arguments
            ):
                raise ToolConfigurationError(
                    "tool %s must use a pinned script file, not inline/module code"
                    % tool_id
                )
            for index, argument in enumerate(arguments):
                placeholders.update(
                    _validate_template(
                        argument,
                        "tools.%s.%s.args[%d]" % (tool_id, operation_name, index),
                    )
                )
                character_count += len(argument) if isinstance(argument, str) else 0
                if isinstance(argument, str):
                    for literal_path in _literal_file_candidates(argument):
                        key = _path_key(literal_path.resolve())
                        if key != _path_key(executable) and key not in fixed_keys:
                            raise ToolConfigurationError(
                                "tool %s literal file argument must be pinned in "
                                "fixedFileHashes: %s" % (tool_id, literal_path)
                            )
            if character_count > MAX_ARGUMENT_CHARS:
                raise ToolConfigurationError(
                    "tool %s %s argument templates are too large"
                    % (tool_id, operation_name)
                )
            required_placeholders = {"input"}
            if operation_name != "validate":
                required_placeholders.add("output")
            if not required_placeholders.issubset(placeholders):
                raise ToolConfigurationError(
                    "tool %s %s args must reference %s"
                    % (
                        tool_id,
                        operation_name,
                        " and ".join("{%s}" % item for item in sorted(required_placeholders)),
                    )
                )
            timeout = operation.get("timeoutSeconds", 120)
            if isinstance(timeout, bool):
                raise ToolConfigurationError("timeoutSeconds must be numeric")
            try:
                timeout_value = float(timeout)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ToolConfigurationError("timeoutSeconds must be numeric") from exc
            if (
                not math.isfinite(timeout_value)
                or timeout_value < 1
                or timeout_value > 600
            ):
                raise ToolConfigurationError("timeoutSeconds must be between 1 and 600")
            digest_material = {
                "toolId": tool_id,
                "operation": operation_name,
                "executable": str(executable),
                "executableSha256": executable_sha256,
                "fixedFileHashes": {str(path): expected for path, expected in fixed},
                "args": arguments,
                "timeoutSeconds": timeout_value,
            }
            self._operations[(tool_id, operation_name)] = ToolOperation(
                tool_id=tool_id,
                operation=operation_name,
                executable=executable,
                executable_sha256=executable_sha256,
                fixed_file_hashes=tuple(fixed),
                arguments=tuple(arguments),
                placeholders=frozenset(placeholders),
                timeout_seconds=timeout_value,
                config_digest=_canonical_digest(digest_material),
            )

    def operation(self, tool_id: str, operation: str) -> ToolOperation:
        if not isinstance(tool_id, str) or not TOOL_ID_RE.fullmatch(tool_id):
            raise ToolConfigurationError("invalid tool id")
        value = self._operations.get((tool_id, operation))
        if value is None:
            raise ToolConfigurationError(
                "tool %s does not define %s" % (tool_id, operation)
            )
        return value

    def operations(self) -> tuple[ToolOperation, ...]:
        """Return the immutable operations in deterministic ID/name order.

        This intentionally returns the already validated operation records, not
        the original JSON.  Callers can build capability diagnostics without
        gaining a way to replace the executable or argument template at run
        time.
        """

        return tuple(
            self._operations[key]
            for key in sorted(self._operations, key=lambda item: (item[0], item[1]))
        )

    def operation_integrity(self, tool_id: str, operation: str) -> dict[str, Any]:
        """Re-hash one pinned operation and return a non-executing probe result."""

        value = self.operation(tool_id, operation)
        error = _integrity_error(value)
        return {
            "toolId": value.tool_id,
            "operation": value.operation,
            "configDigest": value.config_digest,
            "executable": str(value.executable),
            "executableSha256": value.executable_sha256,
            "fixedFileHashes": value.fixed_file_hashes_dict(),
            "available": error is None,
            "integrityError": error,
        }


def run_operation(
    operation: ToolOperation,
    values: Mapping[str, str],
    job_dir: Path,
    *,
    source_unit: Mapping[str, Any] | None = None,
) -> ProcessResult:
    """Run one fixed command with no shell and binary stdout/stderr logs."""

    if operation.operation == "compile":
        _reject_untracked_compile_dependencies(values.get("input", ""),
                                               source_unit=source_unit, job_dir=job_dir)

    # The workspace may stage immutable input snapshots before launch. The
    # process metadata/log files themselves must still be new for this job.
    job_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = job_dir / "stdout.log"
    stderr_path = job_dir / "stderr.log"
    for reserved in (job_dir / "job.json", stdout_path, stderr_path):
        if reserved.exists():
            raise ToolConfigurationError(
                "job directory is already in use: %s" % job_dir
            )
    command = operation.expand(values)
    fixed_file_hashes = operation.fixed_file_hashes_dict()
    minimal_environment = _minimal_environment(job_dir)
    atomic_write_json(
        job_dir / "job.json",
        {
            "schema": "renderdoc-ultra-shader-job/v1",
            "schemaVersion": 1,
            "toolId": operation.tool_id,
            "operation": operation.operation,
            "toolConfigDigest": operation.config_digest,
            "executableSha256": operation.executable_sha256,
            "fixedFileHashes": fixed_file_hashes,
            "command": command,
            "cwd": str(job_dir),
            "environmentKeys": sorted(minimal_environment),
            "timeoutSeconds": operation.timeout_seconds,
            "status": "running",
        },
    )
    return_code: int | None = None
    timed_out = False
    launch_error: str | None = None
    stream_state: dict[str, bool] = {
        "stdoutTruncated": False,
        "stderrTruncated": False,
    }
    launch_error = _integrity_error(operation)
    if launch_error is not None:
        stdout_path.write_bytes(b"")
        stderr_path.write_text(launch_error + "\n", encoding="utf-8")
    else:
        process = None
        windows_job = None
        stdout_thread = None
        stderr_thread = None
        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
            process = subprocess.Popen(
                command,
                cwd=str(job_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=minimal_environment,
                close_fds=True,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            windows_job = _attach_windows_kill_job(process)
            stdout_thread = threading.Thread(
                target=_drain_bounded,
                args=(
                    process.stdout,
                    stdout_path,
                    MAX_LOG_BYTES,
                    stream_state,
                    "stdoutTruncated",
                ),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_drain_bounded,
                args=(
                    process.stderr,
                    stderr_path,
                    MAX_LOG_BYTES,
                    stream_state,
                    "stderrTruncated",
                ),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            try:
                return_code = process.wait(timeout=operation.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                _terminate_process_tree(
                    process,
                    windows_job,
                    minimal_environment,
                    force_fallback=timed_out,
                )
                windows_job = None
            if timed_out:
                try:
                    return_code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    launch_error = (
                        "timed-out process did not terminate within 5 seconds"
                    )
            for thread in (stdout_thread, stderr_thread):
                thread.join(timeout=5)
            live_threads = [
                thread for thread in (stdout_thread, stderr_thread) if thread.is_alive()
            ]
            if live_threads:
                for stream in (process.stdout, process.stderr):
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
                for thread in live_threads:
                    thread.join(timeout=1)
            if any(thread.is_alive() for thread in (stdout_thread, stderr_thread)):
                launch_error = launch_error or "tool output streams did not close"
            stream_errors = [
                str(value)
                for key, value in stream_state.items()
                if key.endswith("Error") and value
            ]
            if stream_errors and launch_error is None:
                launch_error = "tool log stream failed: %s" % "; ".join(stream_errors)
        except OSError as exc:
            launch_error = "%s: %s" % (type(exc).__name__, exc)
            if process is not None:
                _terminate_process_tree(
                    process,
                    windows_job,
                    minimal_environment,
                    force_fallback=True,
                )
            stdout_path.touch(exist_ok=True)
            with stderr_path.open("ab") as stderr_stream:
                stderr_stream.write(
                    (launch_error + "\n").encode("utf-8", errors="replace")
                )
    post_run_integrity_error = _integrity_error(operation)
    if post_run_integrity_error is not None and launch_error is None:
        launch_error = "post-run integrity failure: %s" % post_run_integrity_error
        with stderr_path.open("ab") as stderr_stream:
            stderr_stream.write((launch_error + "\n").encode("utf-8", errors="replace"))
    atomic_write_json(
        job_dir / "job.json",
        {
            "schema": "renderdoc-ultra-shader-job/v1",
            "schemaVersion": 1,
            "toolId": operation.tool_id,
            "operation": operation.operation,
            "toolConfigDigest": operation.config_digest,
            "executableSha256": operation.executable_sha256,
            "fixedFileHashes": fixed_file_hashes,
            "command": command,
            "cwd": str(job_dir),
            "environmentKeys": sorted(minimal_environment),
            "timeoutSeconds": operation.timeout_seconds,
            "status": (
                "timed-out"
                if timed_out
                else (
                    "succeeded"
                    if return_code == 0 and launch_error is None
                    else "failed"
                )
            ),
            "returnCode": return_code,
            "launchError": launch_error,
            "stdoutTruncated": stream_state["stdoutTruncated"],
            "stderrTruncated": stream_state["stderrTruncated"],
            "stdoutPath": "stdout.log",
            "stderrPath": "stderr.log",
        },
    )
    return ProcessResult(
        job_dir=job_dir,
        command=tuple(command),
        return_code=return_code,
        timed_out=timed_out,
        launch_error=launch_error,
        stdout_truncated=stream_state["stdoutTruncated"],
        stderr_truncated=stream_state["stderrTruncated"],
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
