"""Bounded, untrusted shader-recovery asset discovery.

This module deliberately stops before trust, compilation, or replay mutation.
It only reads regular files below one caller-selected root, fingerprints them,
extracts a small set of deterministic structural facts, and ranks possible
recovery inputs.  In particular it never imports or runs code from the asset
tree and never builds a compiler command line.

The returned recovery levels have intentionally narrow meanings:

``0`` no useful relationship to the supplied capture facts
``1`` path/name/include/reference hint only
``2`` readable primary shader source
``3`` plausible compile input (compilation has *not* been attempted)
``4`` supplied structural facts describe the same ABI
``5`` executable-code fingerprint equality
``6`` raw binary-container equality

Even level 6 is discovery evidence, not permission to seed, compile, replace,
or apply a shader.  Those operations remain behind the existing capture-bound
workspace, trusted-toolchain, and full ABI gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import threading
import ctypes
from ctypes import wintypes
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .dxbc import ShaderContainer, fingerprint_bytes

RECOVERY_ASSET_SCHEMA = "renderdoc-ultra-recovery-assets/v1"
RECOVERY_ASSET_SCHEMA_VERSION = 1

DEFAULT_MAX_FILES = 50_000
DEFAULT_MAX_FILE_SIZE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_TOTAL_READ_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_CANDIDATES = 20_000

_MAX_SOURCE_INSPECTION_BYTES = 2 * 1024 * 1024
_MAX_CONTAINERS_PER_FILE = 1_024
_MAX_DXBC_MAGIC_PROBES_PER_FILE = 65_536
_MAX_HINT_ITEMS = 16
_MAX_BINARY_CHUNK_HINTS = 32
_MAX_SKIP_DETAILS = 128
_MIN_FILESYSTEM_ENTRY_LIMIT = 1_024
_MAX_FILESYSTEM_ENTRY_LIMIT = 1_000_000
_MAX_RELATIVE_PATH_UTF8_BYTES = 4 * 1024
_MAX_REPORT_CANDIDATES = 1_024
_MAX_CANDIDATE_JSON_BYTES = 64 * 1024
_MAX_CANDIDATE_PAYLOAD_JSON_BYTES = 6 * 1024 * 1024
_MAX_REPORT_JSON_BYTES = 8 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_D3D_PROFILE_RE = re.compile(r"^(?:ps|vs|gs|hs|ds|cs|as|ms|lib)_\d+_\d+$")


# ctypes caches every generated ``POINTER(SomeStructure)`` class for the life of
# the process. Defining these structures inside the per-file read path creates a
# fresh type (and therefore permanently cached pointer classes) for every asset.
# Keep both the structures and their pointer types at module scope.
class _WIN_UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _WIN_OBJECT_ATTRIBUTES(ctypes.Structure):
    pass


_WIN_UNICODE_STRING_PTR = ctypes.POINTER(_WIN_UNICODE_STRING)
_WIN_OBJECT_ATTRIBUTES._fields_ = [
    ("Length", wintypes.ULONG),
    ("RootDirectory", wintypes.HANDLE),
    ("ObjectName", _WIN_UNICODE_STRING_PTR),
    ("Attributes", wintypes.ULONG),
    ("SecurityDescriptor", wintypes.LPVOID),
    ("SecurityQualityOfService", wintypes.LPVOID),
]


class _WIN_IO_STATUS_UNION(ctypes.Union):
    _fields_ = [("Status", wintypes.LONG), ("Pointer", wintypes.LPVOID)]


class _WIN_IO_STATUS_BLOCK(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("value", _WIN_IO_STATUS_UNION), ("Information", ctypes.c_size_t)]


class _WIN_FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", wintypes.DWORD),
        ("ReparseTag", wintypes.DWORD),
    ]


_WIN_HANDLE_PTR = ctypes.POINTER(wintypes.HANDLE)
_WIN_OBJECT_ATTRIBUTES_PTR = ctypes.POINTER(_WIN_OBJECT_ATTRIBUTES)
_WIN_IO_STATUS_BLOCK_PTR = ctypes.POINTER(_WIN_IO_STATUS_BLOCK)


class _WindowsSecureFileApi:
    """One process-wide, immutable binding of the Windows secure-open APIs."""

    __slots__ = (
        "create_file",
        "close_handle",
        "get_information",
        "nt_create_file",
        "open_osfhandle",
    )

    def __init__(self) -> None:
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise OSError("Windows secure-open APIs are unavailable")
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        get_information = kernel32.GetFileInformationByHandleEx
        get_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        get_information.restype = wintypes.BOOL
        nt_create_file = ntdll.NtCreateFile
        nt_create_file.argtypes = [
            _WIN_HANDLE_PTR,
            wintypes.DWORD,
            _WIN_OBJECT_ATTRIBUTES_PTR,
            _WIN_IO_STATUS_BLOCK_PTR,
            wintypes.LPVOID,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.LPVOID,
            wintypes.ULONG,
        ]
        nt_create_file.restype = wintypes.LONG
        self.create_file = create_file
        self.close_handle = close_handle
        self.get_information = get_information
        self.nt_create_file = nt_create_file
        self.open_osfhandle = msvcrt.open_osfhandle


_WINDOWS_SECURE_FILE_API_LOCK = threading.Lock()
_WINDOWS_SECURE_FILE_API: _WindowsSecureFileApi | None = None
_WINDOWS_SECURE_FILE_API_UNAVAILABLE = False


def _windows_secure_file_api() -> _WindowsSecureFileApi | None:
    """Return the lazy singleton without rebuilding ctypes function metadata."""

    global _WINDOWS_SECURE_FILE_API, _WINDOWS_SECURE_FILE_API_UNAVAILABLE
    if _WINDOWS_SECURE_FILE_API is not None:
        return _WINDOWS_SECURE_FILE_API
    if _WINDOWS_SECURE_FILE_API_UNAVAILABLE or os.name != "nt":
        return None
    with _WINDOWS_SECURE_FILE_API_LOCK:
        if _WINDOWS_SECURE_FILE_API is not None:
            return _WINDOWS_SECURE_FILE_API
        if _WINDOWS_SECURE_FILE_API_UNAVAILABLE:
            return None
        try:
            _WINDOWS_SECURE_FILE_API = _WindowsSecureFileApi()
        except (AttributeError, ImportError, OSError):
            _WINDOWS_SECURE_FILE_API_UNAVAILABLE = True
            return None
        return _WINDOWS_SECURE_FILE_API


def _windows_extended_path(path: Path) -> str:
    value = str(path.absolute())
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


# Values are (language, role, extension stage hint).  These are classifications
# of the file type, not claims about provenance or semantic identity.
_SOURCE_EXTENSIONS: dict[str, tuple[str, str, str]] = {
    ".hlsl": ("hlsl", "source", ""),
    ".hlsli": ("hlsl", "include", ""),
    ".fx": ("hlsl", "source", ""),
    ".fxh": ("hlsl", "include", ""),
    ".cg": ("hlsl", "source", ""),
    ".cginc": ("hlsl", "include", ""),
    ".compute": ("hlsl", "source", "compute"),
    ".raytrace": ("hlsl", "source", ""),
    ".usf": ("hlsl", "source", ""),
    ".ush": ("hlsl", "include", ""),
    ".glsl": ("glsl", "source", ""),
    ".glslinc": ("glsl", "include", ""),
    ".vert": ("glsl", "source", "vertex"),
    ".vsh": ("glsl", "source", "vertex"),
    ".frag": ("glsl", "source", "fragment"),
    ".fsh": ("glsl", "source", "fragment"),
    ".geom": ("glsl", "source", "geometry"),
    ".tesc": ("glsl", "source", "hull"),
    ".tese": ("glsl", "source", "domain"),
    ".comp": ("glsl", "source", "compute"),
    ".mesh": ("glsl", "source", "mesh"),
    ".task": ("glsl", "source", "task"),
    ".rgen": ("glsl", "source", "ray-generation"),
    ".rint": ("glsl", "source", "intersection"),
    ".rahit": ("glsl", "source", "any-hit"),
    ".rchit": ("glsl", "source", "closest-hit"),
    ".rmiss": ("glsl", "source", "miss"),
    ".rcall": ("glsl", "source", "callable"),
    ".wgsl": ("wgsl", "source", ""),
    ".wgslinc": ("wgsl", "include", ""),
    ".slang": ("slang", "source", ""),
    ".slangh": ("slang", "include", ""),
    ".metal": ("msl", "source", ""),
    ".metalinc": ("msl", "include", ""),
    ".shader": ("shaderlab", "source", ""),
    ".shadergraph": ("shadergraph", "source", ""),
    ".shadersubgraph": ("shadergraph", "include", ""),
    ".spvasm": ("spir-v-assembly", "source", ""),
    ".inc": ("unknown-shader", "include", ""),
}

_PROFILE_STAGE_PREFIX = {
    "ps": "fragment",
    "vs": "vertex",
    "gs": "geometry",
    "hs": "hull",
    "ds": "domain",
    "cs": "compute",
    "as": "task",
    "ms": "mesh",
}

_D3D_KIND_TO_STAGE_PREFIX = {
    0: ("fragment", "ps"),
    1: ("vertex", "vs"),
    2: ("geometry", "gs"),
    3: ("hull", "hs"),
    4: ("domain", "ds"),
    5: ("compute", "cs"),
}

_DXIL_EXTRA_KIND_TO_STAGE_PREFIX = {
    6: ("library", "lib"),
    7: ("raygen", "lib"),
    8: ("intersection", "lib"),
    9: ("anyhit", "lib"),
    10: ("closesthit", "lib"),
    11: ("miss", "lib"),
    12: ("callable", "lib"),
    13: ("mesh", "ms"),
    14: ("task", "as"),
}

_SPIRV_EXECUTION_MODELS = {
    0: "vertex",
    1: "hull",
    2: "domain",
    3: "geometry",
    4: "fragment",
    5: "compute",
    5267: "task",
    5268: "mesh",
    5313: "ray-generation",
    5314: "intersection",
    5315: "any-hit",
    5316: "closest-hit",
    5317: "miss",
    5318: "callable",
    5364: "task",
    5365: "mesh",
}

_STAGE_ALIASES = {
    "pixel": "fragment",
    "ps": "fragment",
    "frag": "fragment",
    "vs": "vertex",
    "vert": "vertex",
    "gs": "geometry",
    "geom": "geometry",
    "hs": "hull",
    "tess-ctrl": "hull",
    "tessellation-control": "hull",
    "ds": "domain",
    "tess-eval": "domain",
    "tessellation-evaluation": "domain",
    "cs": "compute",
    "comp": "compute",
    "as": "task",
    "amplification": "task",
    "ms": "mesh",
    "raygen": "ray-generation",
    "raygeneration": "ray-generation",
    "rgen": "ray-generation",
    "rint": "intersection",
    "rahit": "any-hit",
    "rchit": "closest-hit",
    "rmiss": "miss",
    "rcall": "callable",
}

_INCLUDE_RE = re.compile(
    r"^\s*#\s*include\s*(?P<open>[<\"])(?P<path>[^>\"\r\n]{1,512})[>\"]",
    re.MULTILINE,
)
_UNITY_PRAGMA_RE = re.compile(
    r"^\s*#\s*pragma\s+(?P<stage>vertex|fragment|geometry|hull|domain|kernel)\s+"
    r"(?P<entry>[A-Za-z_]\w*)",
    re.IGNORECASE | re.MULTILINE,
)
_WGSL_ENTRY_RE = re.compile(
    r"@(?P<stage>vertex|fragment|compute)\b(?:(?!\bfn\b)[\s\S]){0,512}?"
    r"\bfn\s+(?P<entry>[A-Za-z_]\w*)\s*\(",
    re.IGNORECASE,
)
_MSL_ENTRY_RE = re.compile(
    r"^\s*(?P<stage>vertex|fragment|kernel)\s+[A-Za-z_]\w*(?:\s*<[^>\r\n]{1,128}>)?"
    r"(?:\s+[*&])?\s+(?P<entry>[A-Za-z_]\w*)\s*\(",
    re.IGNORECASE | re.MULTILINE,
)
_SHADER_ATTRIBUTE_RE = re.compile(
    r"\[\s*shader\s*\(\s*\"(?P<stage>[^\"]{1,64})\"\s*\)\s*\]"
    r"(?:(?!\().){0,512}?\b(?P<entry>[A-Za-z_]\w*)\s*\(",
    re.IGNORECASE | re.DOTALL,
)
_HLSL_FUNCTION_RE = re.compile(
    r"^\s*(?:[A-Za-z_]\w*(?:\s*<[^>\r\n]{1,128}>)?(?:\s+|\s*[*&]\s*))+"
    r"(?P<entry>[A-Za-z_]\w*)\s*\([^;{}\r\n]{0,512}\)"
    r"(?:\s*:\s*[^\r\n{]{1,256})?\s*\{",
    re.MULTILINE,
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{3,}")


def _positive_limit(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_symlink_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        int(getattr(metadata, "st_file_attributes", 0) or 0) & 0x400
    )


def _normalize_stage(value: Any) -> str:
    stage = str(value or "").strip().casefold().replace("_", "-")
    if "\x00" in stage or len(stage) > 64:
        raise ValueError("stage is invalid or too long")
    return _STAGE_ALIASES.get(stage, stage)


def _bounded_text(value: Any, name: str, maximum: int = 512) -> str:
    text = str(value or "").strip()
    if "\x00" in text or len(text) > maximum:
        raise ValueError(f"{name} is invalid or too long")
    return text


def _digest(value: Any, name: str) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (Mapping, list, tuple)):
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    digest = str(value).strip().casefold()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError(f"{name} must be a SHA-256 digest or structured value")
    return digest


def _profile(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("profile", "")
    return _bounded_text(value, "profile", 128).casefold()


def _first(mapping: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return ""


def _normalize_capture_facts(value: Mapping[str, Any] | None) -> dict[str, str]:
    if value is None:
        return {
            "rawSha256": "",
            "codeSha256": "",
            "stage": "",
            "profile": "",
            "entryPoint": "",
            "interfaceHash": "",
            "layoutHash": "",
            "name": "",
            "path": "",
        }
    if not isinstance(value, Mapping):
        raise TypeError("capture_facts must be a mapping")
    return {
        "rawSha256": _digest(_first(value, ("rawSha256", "raw_sha256")), "rawSha256"),
        "codeSha256": _digest(
            _first(value, ("codeSha256", "code_sha256", "executableSha256")),
            "codeSha256",
        ),
        "stage": _normalize_stage(_first(value, ("stage", "shaderStage"))),
        "profile": _profile(
            _first(value, ("profile", "targetProfile", "target_profile"))
        ),
        "entryPoint": _bounded_text(
            _first(value, ("entryPoint", "entry", "entry_point")),
            "entryPoint",
            256,
        ),
        "interfaceHash": _digest(
            _first(value, ("interfaceHash", "interface_hash", "interface")),
            "interfaceHash",
        ),
        "layoutHash": _digest(
            _first(value, ("layoutHash", "layout_hash", "layout")),
            "layoutHash",
        ),
        "name": _bounded_text(
            _first(value, ("name", "displayName", "debugName")), "name", 512
        ),
        "path": _bounded_text(
            _first(value, ("path", "sourcePath", "debugPath")), "path", 2048
        ),
    }


def _candidate_shader_facts(candidate: Mapping[str, Any]) -> dict[str, str]:
    nested = candidate.get("shaderFacts", {})
    facts = nested if isinstance(nested, Mapping) else {}
    fingerprints = candidate.get("fingerprints", {})
    hashes = fingerprints if isinstance(fingerprints, Mapping) else {}
    return {
        "rawSha256": _digest(
            _first(hashes, ("rawSha256", "raw_sha256"))
            or _first(candidate, ("rawSha256", "raw_sha256")),
            "candidate.rawSha256",
        ),
        "codeSha256": _digest(
            _first(hashes, ("codeSha256", "code_sha256"))
            or _first(candidate, ("codeSha256", "code_sha256")),
            "candidate.codeSha256",
        ),
        "stage": _normalize_stage(
            _first(facts, ("stage", "shaderStage"))
            or _first(candidate, ("stage", "shaderStage"))
        ),
        "profile": _profile(
            _first(facts, ("profile", "targetProfile"))
            or _first(candidate, ("profile", "targetProfile"))
        ),
        "entryPoint": _bounded_text(
            _first(facts, ("entryPoint", "entry"))
            or _first(candidate, ("entryPoint", "entry")),
            "candidate.entryPoint",
            256,
        ),
        "interfaceHash": _digest(
            _first(facts, ("interfaceHash", "interface"))
            or _first(candidate, ("interfaceHash", "interface")),
            "candidate.interfaceHash",
        ),
        "layoutHash": _digest(
            _first(facts, ("layoutHash", "layout"))
            or _first(candidate, ("layoutHash", "layout")),
            "candidate.layoutHash",
        ),
    }


def _text_tokens(value: str) -> set[str]:
    return {
        item.casefold()
        for item in _TOKEN_RE.findall(value.replace("\\", "/"))
        if not item.isdigit()
    }


def _path_name_hint(candidate: Mapping[str, Any], capture: Mapping[str, str]) -> bool:
    capture_tokens = _text_tokens(capture.get("name", "")) | _text_tokens(
        capture.get("path", "")
    )
    if not capture_tokens:
        return False
    relative_path = str(candidate.get("relativePath", ""))
    display_name = str(candidate.get("displayName", ""))
    candidate_tokens = _text_tokens(relative_path) | _text_tokens(display_name)
    return bool(capture_tokens & candidate_tokens)


def _source_values(
    candidate: Mapping[str, Any], capture: Mapping[str, str]
) -> tuple[int, int, list[str]]:
    role = str(candidate.get("role", ""))
    language = str(candidate.get("language", ""))
    metadata = candidate.get("sourceMetadata", {})
    source = metadata if isinstance(metadata, Mapping) else {}
    reasons: list[str] = []

    if role == "include":
        return 1, 1, ["untrusted shader include/reference"]
    if role != "source":
        return 0, 0, reasons
    if language == "shadergraph":
        return 1, 1, ["engine graph source requires an external importer"]

    source_value = 2
    compile_utility = 2
    reasons.append("readable untrusted primary shader source")
    if bool(source.get("decodeErrors")):
        source_value = 1
        compile_utility = 1
        reasons.append("source decoding was lossy")

    capture_profile = capture.get("profile", "")
    if (
        capture_profile
        and _D3D_PROFILE_RE.fullmatch(capture_profile)
        and language not in {"hlsl", "slang"}
    ):
        compile_utility = min(compile_utility, 1)
        reasons.append("source language is not a direct input for the D3D profile")

    stage_hints = {
        _normalize_stage(item)
        for item in source.get("stageHints", [])
        if str(item).strip()
    }
    capture_stage = capture.get("stage", "")
    if capture_stage and stage_hints and capture_stage not in stage_hints:
        compile_utility = min(compile_utility, 1)
        reasons.append("lexical stage hints conflict with the capture stage")

    entry_hints = {
        str(item.get("name", ""))
        for item in source.get("entryPointHints", [])
        if isinstance(item, Mapping) and str(item.get("name", ""))
    }
    capture_entry = capture.get("entryPoint", "")
    include_count = int(source.get("includeDirectiveCount", 0) or 0)
    if (
        source_value == 2
        and compile_utility == 2
        and capture_entry
        and capture_entry in entry_hints
        and (not capture_stage or not stage_hints or capture_stage in stage_hints)
        and include_count == 0
        and not bool(source.get("inspectionTruncated"))
    ):
        compile_utility = 3
        reasons.append(
            "capture entry is present and no include dependency was observed"
        )
    elif include_count:
        reasons.append("include closure has not been resolved")

    return source_value, compile_utility, reasons


def evaluate_recovery_candidate(
    candidate: Mapping[str, Any],
    capture_facts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, non-authorising recovery value assessment.

    Candidate facts may come from this module or from another fixed parser.  A
    caller cannot upgrade trust by adding flags such as ``trusted=True``: this
    function ignores all such claims and always returns ``applyAuthorized`` as
    false.
    """

    if not isinstance(candidate, Mapping):
        raise TypeError("candidate must be a mapping")
    capture = _normalize_capture_facts(capture_facts)
    facts = _candidate_shader_facts(candidate)

    matches = {
        "rawSha256Equal": bool(
            capture["rawSha256"]
            and facts["rawSha256"]
            and capture["rawSha256"] == facts["rawSha256"]
        ),
        "codeSha256Equal": bool(
            capture["codeSha256"]
            and facts["codeSha256"]
            and capture["codeSha256"] == facts["codeSha256"]
        ),
        "stageEqual": bool(
            capture["stage"] and facts["stage"] and capture["stage"] == facts["stage"]
        ),
        "profileEqual": bool(
            capture["profile"]
            and facts["profile"]
            and capture["profile"] == facts["profile"]
        ),
        "entryPointEqual": bool(
            capture["entryPoint"]
            and facts["entryPoint"]
            and capture["entryPoint"] == facts["entryPoint"]
        ),
        "interfaceHashEqual": bool(
            capture["interfaceHash"]
            and facts["interfaceHash"]
            and capture["interfaceHash"] == facts["interfaceHash"]
        ),
        "layoutHashEqual": bool(
            capture["layoutHash"]
            and facts["layoutHash"]
            and capture["layoutHash"] == facts["layoutHash"]
        ),
        "pathOrNameHint": _path_name_hint(candidate, capture),
    }

    conflicts: list[str] = []
    for key, label in (
        ("stage", "stage"),
        ("profile", "profile"),
        ("entryPoint", "entry point"),
        ("interfaceHash", "interface"),
        ("layoutHash", "layout"),
    ):
        if capture[key] and facts[key] and capture[key] != facts[key]:
            conflicts.append(f"{label} conflicts with capture facts")

    selector_keys = ("stage", "profile", "entryPoint")
    selectors_complete = all(
        not capture[key] or (facts[key] and capture[key] == facts[key])
        for key in selector_keys
    )
    abi_equal = bool(
        matches["interfaceHashEqual"]
        and matches["layoutHashEqual"]
        and selectors_complete
    )

    identity_value = 0
    identity_reason = "no deterministic identity relationship"
    if matches["rawSha256Equal"]:
        identity_value = 6
        identity_reason = "raw binary-container SHA-256 equality"
    elif matches["codeSha256Equal"]:
        identity_value = 5
        identity_reason = "executable-code SHA-256 equality"
    elif abi_equal:
        identity_value = 3
        identity_reason = "ABI-compatible facts; semantic identity is not proven"
    elif matches["pathOrNameHint"]:
        identity_value = 1
        identity_reason = "path/name token overlap only"

    source_value, compile_utility, source_reasons = _source_values(candidate, capture)
    if conflicts:
        # Exact bytes plus contradictory structural facts indicate inconsistent
        # evidence (or the wrong export in a multi-entry module).  Do not award
        # a high recovery level until the contradiction is resolved.
        recovery_level = min(
            max(source_value, compile_utility, int(matches["pathOrNameHint"])), 2
        )
        level_reason = "candidate facts conflict; high-value recovery is blocked"
    elif matches["rawSha256Equal"]:
        recovery_level = 6
        level_reason = "raw binary-container equality"
    elif matches["codeSha256Equal"]:
        recovery_level = 5
        level_reason = "executable-code equality"
    elif abi_equal:
        recovery_level = 4
        level_reason = "ABI facts match; source semantics remain unproven"
    elif compile_utility >= 3:
        recovery_level = 3
        level_reason = "plausible compile input; compilation was not attempted"
    elif source_value >= 2:
        recovery_level = 2
        level_reason = "readable untrusted primary source"
    elif max(identity_value, source_value, compile_utility) >= 1:
        recovery_level = 1
        level_reason = "low-value hint or reference only"
    else:
        recovery_level = 0
        level_reason = "no useful capture relationship"

    return {
        "identityValue": identity_value,
        "sourceValue": source_value,
        "compileUtility": compile_utility,
        "recoveryLevel": recovery_level,
        "identityReason": identity_reason,
        "recoveryReason": level_reason,
        "sourceReasons": source_reasons,
        "matches": matches,
        "conflicts": conflicts,
        "abiFactsEqual": abi_equal,
        "applyAuthorized": False,
        "workspaceSeedAuthorized": False,
        "trustState": "untrusted-discovery",
        "requiresIndependentVerification": [
            "trusted provenance or independently reproduced bytes",
            "capture-bound stage/profile/entry validation",
            "fixed toolchain route when compilation is requested",
            "full reflected ABI and live-capture validation before apply",
        ],
    }


# A discoverable verb for callers that prefer "score" over "evaluate".
score_recovery_candidate = evaluate_recovery_candidate


def _decode_source(data: bytes) -> tuple[str, str, bool]:
    inspected = data[:_MAX_SOURCE_INSPECTION_BYTES]
    if inspected.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return inspected.decode("utf-16"), "utf-16", False
        except UnicodeDecodeError:
            return inspected.decode("utf-16", errors="replace"), "utf-16", True
    try:
        return inspected.decode("utf-8-sig"), "utf-8", False
    except UnicodeDecodeError:
        return inspected.decode("utf-8", errors="replace"), "utf-8", True


def _entry_hint(name: str, stage: str, basis: str) -> dict[str, str]:
    return {
        "name": _bounded_text(name, "entry hint", 256),
        "stage": _normalize_stage(stage),
        "basis": basis,
    }


def _source_metadata(
    relative_path: str,
    extension: str,
    language: str,
    extension_stage: str,
    data: bytes,
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    text, encoding, decode_errors = _decode_source(data)
    entry_hints: dict[tuple[str, str, str], dict[str, str]] = {}
    stage_hints: set[str] = set()

    def add_stage(value: str) -> None:
        if len(stage_hints) >= _MAX_HINT_ITEMS:
            return
        try:
            stage = _normalize_stage(value)
        except ValueError:
            return
        if stage:
            stage_hints.add(stage)

    if extension_stage:
        add_stage(extension_stage)

    def add_entry(name: str, stage: str, basis: str) -> None:
        if len(entry_hints) >= _MAX_HINT_ITEMS:
            return
        try:
            item = _entry_hint(name, stage, basis)
        except ValueError:
            return
        entry_hints[(item["name"], item["stage"], item["basis"])] = item

    for match in _UNITY_PRAGMA_RE.finditer(text):
        raw_stage = match.group("stage").casefold()
        stage = "compute" if raw_stage == "kernel" else raw_stage
        add_entry(match.group("entry"), stage, "pragma")
        add_stage(stage)
    for match in _WGSL_ENTRY_RE.finditer(text):
        add_entry(match.group("entry"), match.group("stage"), "wgsl-attribute")
        add_stage(match.group("stage"))
    for match in _MSL_ENTRY_RE.finditer(text):
        raw_stage = match.group("stage").casefold()
        stage = "compute" if raw_stage == "kernel" else raw_stage
        add_entry(match.group("entry"), stage, "msl-keyword")
        add_stage(stage)
    for match in _SHADER_ATTRIBUTE_RE.finditer(text):
        add_entry(match.group("entry"), match.group("stage"), "shader-attribute")
        add_stage(match.group("stage"))
    if language in {"hlsl", "slang", "shaderlab", "unknown-shader"}:
        for match in _HLSL_FUNCTION_RE.finditer(text):
            add_entry(match.group("entry"), "", "function-lexeme")
    if language == "glsl" and re.search(r"\bvoid\s+main\s*\(", text):
        add_entry("main", extension_stage, "glsl-main")

    sorted_entry_hints = [
        entry_hints[key]
        for key in sorted(
            entry_hints, key=lambda item: (item[0].casefold(), item[1], item[2])
        )
    ]

    includes: list[dict[str, str]] = []
    include_count = 0
    for match in _INCLUDE_RE.finditer(text):
        include_count += 1
        if len(includes) < _MAX_HINT_ITEMS:
            includes.append(
                {
                    "path": match.group("path"),
                    "style": "system" if match.group("open") == "<" else "local",
                }
            )

    engine_basis: dict[str, set[str]] = {}

    def add_engine(engine: str, basis: str) -> None:
        engine_basis.setdefault(engine, set()).add(basis)

    relative_casefold = "/" + relative_path.casefold().strip("/") + "/"
    if extension in {".usf", ".ush"}:
        add_engine("unreal-engine", f"extension:{extension}")
    if (
        "/engine/shaders/" in relative_casefold
        or "/shaders/private/" in relative_casefold
    ):
        add_engine("unreal-engine", "path-marker")
    if re.search(
        r"\b(?:BEGIN_SHADER_PARAMETER_STRUCT|SHADER_PARAMETER|RWTexture\w*)\b", text
    ):
        add_engine("unreal-engine", "source-marker")

    if extension in {
        ".shader",
        ".shadergraph",
        ".shadersubgraph",
        ".cginc",
        ".compute",
        ".raytrace",
    }:
        add_engine("unity", f"extension:{extension}")
    if (
        "/assets/shaders/" in relative_casefold
        or "/library/shadercache/" in relative_casefold
    ):
        add_engine("unity", "path-marker")
    if re.search(r"\b(?:SubShader|HLSLPROGRAM|CGPROGRAM|UNITY_[A-Z0-9_]+)\b", text):
        add_engine("unity", "source-marker")

    engine_hints = sorted(engine_basis)
    engine_evidence = [
        {
            "engine": engine,
            "basis": sorted(engine_basis[engine]),
            "trusted": False,
        }
        for engine in engine_hints
    ]
    metadata = {
        "encoding": encoding,
        "decodeErrors": decode_errors,
        "inspectionBytes": min(len(data), _MAX_SOURCE_INSPECTION_BYTES),
        "inspectionTruncated": len(data) > _MAX_SOURCE_INSPECTION_BYTES,
        "stageHints": sorted(stage_hints),
        "entryPointHints": sorted_entry_hints,
        "includeDirectiveCount": include_count,
        "includeDirectives": includes,
        "hintsTrusted": False,
    }
    return metadata, engine_hints, engine_evidence


def _d3d_profile_from_container(
    data: bytes, container: ShaderContainer
) -> tuple[str, str]:
    executable = [
        chunk for chunk in container.chunks if chunk.fourcc in {"SHDR", "SHEX", "DXIL"}
    ]
    if len(executable) != 1 or any(
        chunk.fourcc == "Aon9" for chunk in container.chunks
    ):
        return "", ""
    chunk = executable[0]
    offset = chunk.offset + 8
    if offset < 0 or chunk.size > len(data) - offset:
        return "", ""

    if chunk.fourcc in {"SHDR", "SHEX"}:
        if chunk.size < 8 or chunk.size & 3:
            return "", ""
        try:
            token, length_in_dwords = struct.unpack_from("<2I", data, offset)
        except struct.error:
            return "", ""
        if length_in_dwords != chunk.size // 4 or token & 0x0000FF00:
            return "", ""
        mapping = _D3D_KIND_TO_STAGE_PREFIX
    else:
        if chunk.size < 24:
            return "", ""
        try:
            token, size_words, magic, _version, bitcode_offset, bitcode_size = (
                struct.unpack_from("<6I", data, offset)
            )
        except struct.error:
            return "", ""
        if token & 0x0000FF00 or size_words * 4 != chunk.size or magic != 0x4C495844:
            return "", ""
        if bitcode_offset < 16 or bitcode_size == 0:
            return "", ""
        available = chunk.size - 8
        if bitcode_offset > available or bitcode_size > available - bitcode_offset:
            return "", ""
        if (8 + bitcode_offset + bitcode_size + 3) & ~3 != chunk.size:
            return "", ""
        mapping = dict(_D3D_KIND_TO_STAGE_PREFIX)
        mapping.update(_DXIL_EXTRA_KIND_TO_STAGE_PREFIX)

    kind = (token >> 16) & 0xFFFF
    major = (token >> 4) & 0xF
    minor = token & 0xF
    if kind not in mapping:
        return "", ""
    if chunk.fourcc == "DXIL":
        if major < 6:
            return "", ""
    elif (
        major not in {4, 5}
        or (major == 4 and minor not in {0, 1})
        or (major == 5 and minor not in {0, 1})
    ):
        return "", ""
    stage, prefix = mapping[kind]
    return stage, f"{prefix}_{major}_{minor}"


def _find_dxbc_containers(
    data: bytes, maximum: int
) -> tuple[list[tuple[int, ShaderContainer]], bool]:
    found: list[tuple[int, ShaderContainer]] = []
    cursor = 0
    probes = 0
    hit_limit = False
    maximum = min(maximum, _MAX_CONTAINERS_PER_FILE)
    while cursor < len(data):
        offset = data.find(b"DXBC", cursor)
        if offset < 0:
            break
        probes += 1
        if len(found) >= maximum or probes > _MAX_DXBC_MAGIC_PROBES_PER_FILE:
            hit_limit = True
            break
        # Read the declared container size before slicing.  This avoids an
        # O(n^2) sequence of successively large copies in a hostile file that
        # contains many invalid ``DXBC`` strings.
        if offset + 32 > len(data):
            break
        try:
            total_size = struct.unpack_from("<I", data, offset + 24)[0]
            chunk_count = struct.unpack_from("<I", data, offset + 28)[0]
        except struct.error:
            break
        header_size = 32 + chunk_count * 4
        if (
            chunk_count == 0
            or chunk_count > 4_096
            or total_size < header_size
            or total_size > len(data) - offset
        ):
            cursor = offset + 4
            continue
        parsed = fingerprint_bytes(
            data[offset : offset + total_size], source="<bounded-memory>"
        )
        if parsed is None:
            cursor = offset + 4
            continue
        found.append((offset, parsed))
        cursor = offset + parsed.byte_size
    return found, hit_limit


def _spirv_string(
    data: bytes, endian: str, start_word: int, word_count: int
) -> str | None:
    # Do not allocate in proportion to a hostile instruction's declared word
    # count.  Entry names longer than the public schema allows are irrelevant.
    raw = bytearray()
    maximum_words = min(word_count, (256 // 4) + 1)
    for index in range(maximum_words):
        try:
            word = struct.unpack_from(f"{endian}I", data, (start_word + index) * 4)[0]
        except struct.error:
            return None
        encoded = word.to_bytes(4, "little")
        nul = encoded.find(b"\x00")
        raw.extend(encoded if nul < 0 else encoded[:nul])
        if nul >= 0:
            break
    else:
        return None
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not value or len(value) > 256 or "\x00" in value:
        return None
    return value


def _parse_spirv(data: bytes) -> dict[str, Any] | None:
    if len(data) < 20 or len(data) & 3:
        return None
    little_magic = struct.unpack_from("<I", data, 0)[0]
    if little_magic == 0x07230203:
        endian = "<"
    elif little_magic == 0x03022307:
        endian = ">"
    else:
        return None

    def word(index: int) -> int:
        return struct.unpack_from(f"{endian}I", data, index * 4)[0]

    version = word(1)
    major = (version >> 16) & 0xFF
    minor = (version >> 8) & 0xFF
    bound = word(3)
    if major != 1 or minor > 6 or bound == 0 or word(4) != 0:
        return None

    entries: list[dict[str, Any]] = []
    total_words = len(data) // 4
    cursor = 5
    while cursor < total_words:
        instruction = word(cursor)
        word_count = instruction >> 16
        opcode = instruction & 0xFFFF
        if word_count == 0 or word_count > total_words - cursor:
            return None
        if opcode == 15 and word_count >= 4 and len(entries) < _MAX_HINT_ITEMS:
            model = word(cursor + 1)
            name = _spirv_string(data, endian, cursor + 3, word_count - 3)
            if name is not None:
                entries.append(
                    {
                        "name": name,
                        "stage": _SPIRV_EXECUTION_MODELS.get(model, "unknown"),
                        "executionModel": model,
                    }
                )
        cursor += word_count

    unique = {
        (entry["name"], entry["stage"], entry["executionModel"]): entry
        for entry in entries
    }
    entries = [unique[key] for key in sorted(unique)]
    return {
        "version": f"{major}.{minor}",
        "generator": word(2),
        "bound": bound,
        "entryPoints": entries,
    }


def _file_candidate_id(
    relative_path: str,
    file_sha256: str,
    file_offset: int,
    byte_size: int,
    artifact_format: str,
) -> str:
    material = {
        "schema": RECOVERY_ASSET_SCHEMA,
        "relativePath": relative_path,
        "fileSha256": file_sha256,
        "fileOffset": file_offset,
        "byteSize": byte_size,
        "format": artifact_format,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()[:32]


def _base_candidate(
    relative_path: str,
    file_size: int,
    file_sha256: str,
    file_offset: int,
    byte_size: int,
    artifact_format: str,
    language: str,
    role: str,
) -> dict[str, Any]:
    return {
        "candidateId": _file_candidate_id(
            relative_path, file_sha256, file_offset, byte_size, artifact_format
        ),
        "relativePath": relative_path,
        "displayName": Path(relative_path).stem,
        "fileByteSize": file_size,
        "fileSha256": file_sha256,
        "fileOffset": file_offset,
        "byteSize": byte_size,
        "format": artifact_format,
        "language": language,
        "role": role,
        "fingerprints": {"rawSha256": "", "codeSha256": ""},
        "shaderFacts": {
            "stage": "",
            "profile": "",
            "entryPoint": "",
            "interfaceHash": "",
            "layoutHash": "",
        },
        "factSources": {},
        "engineHints": [],
        "engineHintEvidence": [],
        "trusted": False,
        "applyAuthorized": False,
    }


def _source_candidate(
    relative_path: str, file_size: int, file_sha256: str, data: bytes
) -> dict[str, Any] | None:
    extension = Path(relative_path).suffix.casefold()
    classification = _SOURCE_EXTENSIONS.get(extension)
    if classification is None:
        return None
    language, role, extension_stage = classification
    candidate = _base_candidate(
        relative_path,
        file_size,
        file_sha256,
        0,
        file_size,
        "text",
        language,
        role,
    )
    metadata, engine_hints, engine_evidence = _source_metadata(
        relative_path, extension, language, extension_stage, data
    )
    candidate["sourceMetadata"] = metadata
    candidate["engineHints"] = engine_hints
    candidate["engineHintEvidence"] = engine_evidence
    return candidate


def _binary_candidates(
    relative_path: str,
    file_size: int,
    file_sha256: str,
    data: bytes,
    maximum: int,
) -> tuple[list[dict[str, Any]], bool]:
    candidates: list[dict[str, Any]] = []
    dxbc, hit_limit = _find_dxbc_containers(data, maximum)
    for offset, container in dxbc:
        if len(candidates) >= maximum:
            return candidates, True
        container_bytes = data[offset : offset + container.byte_size]
        stage, profile = _d3d_profile_from_container(container_bytes, container)
        language = container.kind.casefold()
        candidate = _base_candidate(
            relative_path,
            file_size,
            file_sha256,
            offset,
            container.byte_size,
            language,
            language,
            "binary",
        )
        candidate["fingerprints"] = {
            "rawSha256": container.raw_sha256,
            "codeSha256": container.code_sha256,
        }
        candidate["shaderFacts"]["stage"] = stage
        candidate["shaderFacts"]["profile"] = profile
        candidate["factSources"] = {
            "rawSha256": "parsed binary container",
            "codeSha256": "parsed executable chunks",
        }
        if stage:
            candidate["factSources"]["stage"] = "compiled executable token"
            candidate["factSources"]["profile"] = "compiled executable token"
        candidate["binaryMetadata"] = {
            "containerKind": container.kind,
            "chunkCount": len(container.chunks),
            "chunks": [
                {"fourcc": chunk.fourcc, "offset": chunk.offset, "size": chunk.size}
                for chunk in container.chunks[:_MAX_BINARY_CHUNK_HINTS]
            ],
            "chunkListTruncated": len(container.chunks) > _MAX_BINARY_CHUNK_HINTS,
            "embedded": offset != 0 or container.byte_size != file_size,
        }
        candidates.append(candidate)

    if len(candidates) < maximum:
        spirv = _parse_spirv(data)
        if spirv is not None:
            candidate = _base_candidate(
                relative_path,
                file_size,
                file_sha256,
                0,
                file_size,
                "spir-v",
                "spir-v",
                "binary",
            )
            candidate["fingerprints"]["rawSha256"] = file_sha256
            candidate["factSources"]["rawSha256"] = (
                "structurally parsed whole-file SPIR-V"
            )
            entries = spirv["entryPoints"]
            if len(entries) == 1 and entries[0]["stage"] != "unknown":
                candidate["shaderFacts"]["stage"] = entries[0]["stage"]
                candidate["shaderFacts"]["entryPoint"] = entries[0]["name"]
                candidate["factSources"]["stage"] = "SPIR-V OpEntryPoint"
                candidate["factSources"]["entryPoint"] = "SPIR-V OpEntryPoint"
            candidate["binaryMetadata"] = {
                "containerKind": "SPIR-V",
                **spirv,
                "embedded": False,
            }
            candidates.append(candidate)

    if len(candidates) < maximum and data.startswith(b"MTLB"):
        candidate = _base_candidate(
            relative_path,
            file_size,
            file_sha256,
            0,
            file_size,
            "metallib",
            "msl",
            "binary",
        )
        candidate["fingerprints"]["rawSha256"] = file_sha256
        candidate["factSources"]["rawSha256"] = "whole-file MTLB magic and SHA-256"
        candidate["binaryMetadata"] = {
            "containerKind": "MetalLib",
            "embedded": False,
            "headerOnlyRecognition": True,
        }
        candidates.append(candidate)

    return candidates, hit_limit


def _read_bound_fd(fd: int, expected_size: int) -> tuple[bytes | None, str, int]:
    """Read one already-bound descriptor and fstat the same object twice."""

    consumed = 0
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            return None, "file-changed-or-not-regular", consumed
        if int(getattr(before, "st_file_attributes", 0) or 0) & 0x400:
            return None, "reparse-or-symlink", consumed
        chunks: list[bytes] = []
        remaining = expected_size
        while remaining:
            chunk = os.read(fd, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                return None, "file-changed-during-read", consumed
            chunks.append(chunk)
            consumed += len(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            return None, "file-changed-during-read", consumed
        return b"".join(chunks), "", consumed
    except (OSError, ValueError):
        return None, "read-error", consumed


def _safe_relative_parts(relative_path: str | Path) -> tuple[str, ...]:
    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts:
        raise ValueError("relative_path must remain below asset_root")
    parts = tuple(str(part) for part in relative.parts)
    if any(part in {"", ".", ".."} or "\x00" in part for part in parts):
        raise ValueError("relative_path must remain below asset_root")
    if os.name == "nt" and any(":" in part for part in parts):
        raise ValueError("relative_path must not select a drive or alternate stream")
    return parts


def _read_regular_file_posix(
    root: Path, parts: tuple[str, ...], expected_size: int
) -> tuple[bytes | None, str, int]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if (
        no_follow is None
        or directory is None
        or os.open not in getattr(os, "supports_dir_fd", set())
    ):
        return None, "secure-open-unavailable", 0
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    descriptors: list[int] = []
    file_fd = -1
    try:
        root_fd = os.open(
            root,
            os.O_RDONLY | directory | no_follow | close_on_exec,
        )
        descriptors.append(root_fd)
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            return None, "secure-root-open-failed", 0
        for component in parts[:-1]:
            child_fd = os.open(
                component,
                os.O_RDONLY | directory | no_follow | close_on_exec,
                dir_fd=descriptors[-1],
            )
            descriptors.append(child_fd)
            if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                return None, "reparse-or-symlink", 0
        file_fd = os.open(
            parts[-1],
            os.O_RDONLY | no_follow | close_on_exec,
            dir_fd=descriptors[-1],
        )
        return _read_bound_fd(file_fd, expected_size)
    except (OSError, ValueError):
        return None, "reparse-or-path-race", 0
    finally:
        if file_fd >= 0:
            try:
                os.close(file_fd)
            except OSError:
                pass
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_regular_file_windows(
    root: Path, parts: tuple[str, ...], expected_size: int
) -> tuple[bytes | None, str, int]:
    """Open a file relative to a held root handle without traversing reparses.

    ``NtCreateFile`` and ``OBJ_DONT_REPARSE`` are declared by the Windows SDK's
    ``winternl.h``.  Root-relative object lookup is the Windows equivalent of a
    POSIX ``openat`` chain: a path rename cannot redirect the already-held root
    handle, and any reparse point in the relative lookup fails closed.
    """

    api = _windows_secure_file_api()
    if api is None:
        return None, "secure-open-unavailable", 0

    invalid_handle = ctypes.c_void_p(-1).value
    root_handle = api.create_file(
        _windows_extended_path(root),
        0x0001 | 0x0080 | 0x00100000,  # LIST_DIRECTORY|READ_ATTRIBUTES|SYNCHRONIZE
        0x0001 | 0x0002 | 0x0004,  # share read/write/delete
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS|OPEN_REPARSE_POINT
        None,
    )
    if not root_handle or int(root_handle) == invalid_handle:
        return None, "secure-root-open-failed", 0
    file_handle = wintypes.HANDLE()
    fd = -1
    try:
        root_attributes = _WIN_FILE_ATTRIBUTE_TAG_INFO()
        if not api.get_information(
            root_handle,
            9,  # FileAttributeTagInfo
            ctypes.byref(root_attributes),
            ctypes.sizeof(root_attributes),
        ):
            return None, "secure-root-open-failed", 0
        if (
            root_attributes.FileAttributes & 0x400  # REPARSE_POINT
            or not root_attributes.FileAttributes & 0x10  # DIRECTORY
        ):
            return None, "reparse-or-symlink", 0

        relative_value = "\\".join(parts)
        encoded_length = len(relative_value.encode("utf-16-le"))
        if encoded_length <= 0 or encoded_length > 0xFFFC:
            return None, "relative-path-too-long", 0
        relative_buffer = ctypes.create_unicode_buffer(relative_value)
        unicode_name = _WIN_UNICODE_STRING(
            encoded_length,
            encoded_length + 2,
            ctypes.cast(relative_buffer, wintypes.LPWSTR),
        )
        attributes = _WIN_OBJECT_ATTRIBUTES(
            ctypes.sizeof(_WIN_OBJECT_ATTRIBUTES),
            root_handle,
            ctypes.pointer(unicode_name),
            0x0040 | 0x1000,  # OBJ_CASE_INSENSITIVE|OBJ_DONT_REPARSE
            None,
            None,
        )
        io_status = _WIN_IO_STATUS_BLOCK()
        status = api.nt_create_file(
            ctypes.byref(file_handle),
            0x80000000 | 0x00100000,  # GENERIC_READ|SYNCHRONIZE
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            0x80,  # FILE_ATTRIBUTE_NORMAL
            0x0001 | 0x0002 | 0x0004,
            1,  # FILE_OPEN
            0x00200000 | 0x00000040 | 0x00000020,  # REPARSE|NON_DIRECTORY|SYNC
            None,
            0,
        )
        if status < 0 or not file_handle.value:
            return None, "reparse-or-path-race", 0
        file_attributes = _WIN_FILE_ATTRIBUTE_TAG_INFO()
        if not api.get_information(
            file_handle,
            9,
            ctypes.byref(file_attributes),
            ctypes.sizeof(file_attributes),
        ):
            return None, "read-error", 0
        if (
            file_attributes.FileAttributes & 0x400
            or file_attributes.FileAttributes & 0x10
        ):
            return None, "reparse-or-symlink", 0

        fd = api.open_osfhandle(
            int(file_handle.value), os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
        file_handle = wintypes.HANDLE()
        return _read_bound_fd(fd, expected_size)
    except (OSError, OverflowError, ValueError):
        return None, "read-error", 0
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if file_handle.value:
            api.close_handle(file_handle)
        api.close_handle(root_handle)


def read_recovery_regular_file(
    asset_root: str | Path,
    relative_path: str | Path,
    expected_size: int,
) -> tuple[bytes | None, str, int]:
    """Securely read one regular file beneath a fixed root.

    The file is opened relative to a held root/directory handle with reparse or
    symlink traversal disabled, then the same descriptor is checked with
    ``fstat`` before and after the bounded read. No path check is treated as an
    authority boundary.
    """

    if isinstance(expected_size, bool) or not isinstance(expected_size, int):
        raise TypeError("expected_size must be a non-negative integer")
    if expected_size < 0:
        raise ValueError("expected_size must be a non-negative integer")
    parts = _safe_relative_parts(relative_path)
    root = Path(asset_root)
    if os.name == "nt":
        return _read_regular_file_windows(root, parts, expected_size)
    return _read_regular_file_posix(root, parts, expected_size)


def _candidate_rank_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return the stable best-first assessment order used by every bound."""

    matches = candidate.get("matches")
    match_values = matches if isinstance(matches, Mapping) else {}
    exact_fingerprint_tier = (
        2
        if match_values.get("rawSha256Equal") is True
        else 1 if match_values.get("codeSha256Equal") is True else 0
    )
    return (
        -exact_fingerprint_tier,
        -int(candidate["recoveryLevel"]),
        -int(candidate["identityValue"]),
        -int(candidate["sourceValue"]),
        -int(candidate["compileUtility"]),
        str(candidate["relativePath"]).casefold(),
        str(candidate["relativePath"]),
        int(candidate["fileOffset"]),
        str(candidate["format"]),
        str(candidate["candidateId"]),
    )


def _json_utf8_bytes(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    )


def _bounded_report_path(value: str) -> str:
    """Keep diagnostic path fields useful without reflecting unbounded names."""

    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_RELATIVE_PATH_UTF8_BYTES:
        return value
    prefix = encoded[: _MAX_RELATIVE_PATH_UTF8_BYTES - 96].decode(
        "utf-8", errors="ignore"
    )
    return "%s...#sha256=%s" % (prefix, hashlib.sha256(encoded).hexdigest())


def _bounded_candidate_for_report(candidate: dict[str, Any]) -> dict[str, Any]:
    """Apply a last-resort per-candidate metadata serialization bound.

    All parsers above already bound their fields and arrays.  This projection is
    deliberately defensive so a future parser cannot silently make one cached
    candidate arbitrarily large.
    """

    if _json_utf8_bytes(candidate) <= _MAX_CANDIDATE_JSON_BYTES:
        return candidate

    bounded = dict(candidate)
    source = candidate.get("sourceMetadata")
    if isinstance(source, Mapping):
        bounded["sourceMetadata"] = {
            "encoding": source.get("encoding", ""),
            "decodeErrors": bool(source.get("decodeErrors")),
            "inspectionBytes": int(source.get("inspectionBytes", 0) or 0),
            "inspectionTruncated": bool(source.get("inspectionTruncated")),
            "stageHints": list(source.get("stageHints", []))[:8],
            "entryPointHints": list(source.get("entryPointHints", []))[:8],
            "includeDirectiveCount": int(
                source.get("includeDirectiveCount", 0) or 0
            ),
            "includeDirectives": list(source.get("includeDirectives", []))[:4],
            "hintsTrusted": False,
            "metadataTruncatedForReport": True,
        }
    binary = candidate.get("binaryMetadata")
    if isinstance(binary, Mapping):
        bounded_binary = {
            key: binary[key]
            for key in (
                "containerKind",
                "embedded",
                "headerOnlyRecognition",
                "chunkCount",
                "chunkListTruncated",
                "version",
                "generator",
                "bound",
            )
            if key in binary
        }
        if isinstance(binary.get("chunks"), list):
            bounded_binary["chunks"] = list(binary["chunks"][:8])
        if isinstance(binary.get("entryPoints"), list):
            bounded_binary["entryPoints"] = list(binary["entryPoints"][:8])
        bounded_binary["metadataTruncatedForReport"] = True
        bounded["binaryMetadata"] = bounded_binary
    bounded["engineHintEvidence"] = list(
        candidate.get("engineHintEvidence", [])
    )[:8]
    bounded["metadataTruncatedForReport"] = True
    if _json_utf8_bytes(bounded) > _MAX_CANDIDATE_JSON_BYTES:
        # This is unreachable for candidates produced by the fixed parsers, but
        # fail closed if a future field violates the public serialization cap.
        raise RuntimeError("candidate metadata exceeds the fixed report bound")
    return bounded


def discover_recovery_assets(
    asset_root: str | Path,
    capture_facts: Mapping[str, Any] | None = None,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    max_total_read_bytes: int = DEFAULT_MAX_TOTAL_READ_BYTES,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> dict[str, Any]:
    """Discover and rank shader recovery assets under one safe root.

    The traversal never follows symlinks.  Every opened path is resolved and
    required to remain within ``asset_root``.  A file is read only when its
    complete contents fit both the per-file and remaining aggregate budgets so
    every reported ``fileSha256`` covers the whole observed file.
    """

    max_files = _positive_limit(max_files, "max_files")
    max_file_size_bytes = _positive_limit(max_file_size_bytes, "max_file_size_bytes")
    max_total_read_bytes = _positive_limit(max_total_read_bytes, "max_total_read_bytes")
    max_candidates = _positive_limit(max_candidates, "max_candidates")
    max_filesystem_entries = min(
        max(max_files * 4, _MIN_FILESYSTEM_ENTRY_LIMIT),
        _MAX_FILESYSTEM_ENTRY_LIMIT,
    )
    capture = _normalize_capture_facts(capture_facts)

    requested_root = Path(asset_root)
    if _is_symlink_or_reparse(requested_root):
        raise ValueError("asset_root must not be a symlink or reparse point")
    secure_read_root = requested_root.absolute()
    try:
        root = requested_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("asset_root does not exist or cannot be resolved") from exc
    if not root.is_dir():
        raise ValueError("asset_root must be a directory")
    if len(str(root).encode("utf-8", errors="replace")) > _MAX_RELATIVE_PATH_UTF8_BYTES:
        raise ValueError("asset_root path is too long for the bounded report")

    counters: Counter[str] = Counter()
    partial_reasons: set[str] = set()
    skip_details: list[dict[str, str]] = []
    candidate_limit = min(max_candidates, _MAX_REPORT_CANDIDATES)
    candidate_buffer: list[dict[str, Any]] = []
    candidates_discovered = 0
    inventory_records = hashlib.sha256()
    inventory_file_count = 0
    bytes_read = 0
    files_visited = 0
    filesystem_entries_visited = 0
    stop = False

    def skipped(reason: str, relative_path: str) -> None:
        counters[reason] += 1
        if len(skip_details) < _MAX_SKIP_DETAILS:
            skip_details.append(
                {
                    "relativePath": _bounded_report_path(relative_path),
                    "reason": reason,
                }
            )

    def retain_candidate(candidate: dict[str, Any]) -> None:
        """Keep only a bounded best-first working set while scanning onward."""

        nonlocal candidates_discovered
        candidate.update(evaluate_recovery_candidate(candidate, capture))
        candidate = _bounded_candidate_for_report(candidate)
        candidates_discovered += 1
        candidate_buffer.append(candidate)
        # Batched bounded sorting avoids per-candidate O(log K) wrapper objects,
        # while never retaining more than roughly 2K assessed candidates.
        if len(candidate_buffer) > candidate_limit * 2:
            candidate_buffer.sort(key=_candidate_rank_key)
            del candidate_buffer[candidate_limit:]

    stack = [root]
    while stack and not stop:
        directory = stack.pop()
        try:
            unsorted_entries: list[os.DirEntry[str]] = []
            with os.scandir(directory) as iterator:
                for item in iterator:
                    if filesystem_entries_visited >= max_filesystem_entries:
                        partial_reasons.add("max-filesystem-entries-reached")
                        stop = True
                        unsorted_entries.clear()
                        break
                    filesystem_entries_visited += 1
                    unsorted_entries.append(item)
            entries = sorted(
                unsorted_entries, key=lambda item: (item.name.casefold(), item.name)
            )
        except OSError:
            partial_reasons.add("directory-read-errors")
            relative = directory.relative_to(root).as_posix() or "."
            skipped("directoryReadErrors", relative)
            continue
        if stop:
            break

        child_directories: list[Path] = []
        for entry in entries:
            entry_path = Path(entry.path)
            try:
                relative_path = entry_path.relative_to(root).as_posix()
            except ValueError:
                skipped("outsideRoot", entry.name)
                continue
            if (
                len(relative_path.encode("utf-8", errors="replace"))
                > _MAX_RELATIVE_PATH_UTF8_BYTES
            ):
                partial_reasons.add("path-length-bound-reached")
                skipped("pathTooLong", relative_path)
                continue
            try:
                entry_metadata = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or (
                    int(getattr(entry_metadata, "st_file_attributes", 0) or 0)
                    & 0x400
                ):
                    skipped("symlinks", relative_path)
                    continue
                resolved = entry_path.resolve(strict=True)
                if not _is_within(resolved, root):
                    skipped("outsideRoot", relative_path)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    child_directories.append(resolved)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    skipped("nonRegularFiles", relative_path)
                    continue
                stat_result = entry_metadata
            except (OSError, RuntimeError):
                partial_reasons.add("filesystem-errors")
                skipped("filesystemErrors", relative_path)
                continue

            if files_visited >= max_files:
                partial_reasons.add("max-files-reached")
                stop = True
                break
            files_visited += 1
            file_size = stat_result.st_size
            if file_size < 0 or file_size > max_file_size_bytes:
                partial_reasons.add("max-file-size-exceeded")
                skipped("tooLarge", relative_path)
                continue
            if file_size > max_total_read_bytes - bytes_read:
                partial_reasons.add("max-total-read-bytes-reached")
                skipped("totalBudget", relative_path)
                continue

            data, _read_error, consumed = read_recovery_regular_file(
                secure_read_root, relative_path, file_size
            )
            # Failed or raced reads still consume aggregate I/O budget.  This
            # prevents a changing/denied asset set from bypassing the total
            # bound through a sequence of discarded partial reads.
            bytes_read += consumed
            if data is None:
                partial_reasons.add("read-or-change-errors")
                if _read_error == "secure-open-unavailable":
                    partial_reasons.add("secure-open-unavailable")
                    skipped("secureOpenUnavailable", relative_path)
                elif _read_error in {
                    "reparse-or-symlink",
                    "reparse-or-path-race",
                }:
                    partial_reasons.add("reparse-or-symlink-races-rejected")
                    skipped("reparseOrSymlinkRaces", relative_path)
                else:
                    skipped("readErrors", relative_path)
                continue
            file_sha256 = hashlib.sha256(data).hexdigest()

            source_candidate = _source_candidate(
                relative_path, file_size, file_sha256, data
            )
            file_candidates: list[dict[str, Any]] = []
            if source_candidate is not None:
                file_candidates.append(source_candidate)
            else:
                binary, container_limit = _binary_candidates(
                    relative_path,
                    file_size,
                    file_sha256,
                    data,
                    _MAX_CONTAINERS_PER_FILE,
                )
                file_candidates.extend(binary)
                if container_limit:
                    partial_reasons.add("max-containers-per-file-reached")

            for candidate in file_candidates:
                retain_candidate(candidate)
            inventory_record = {
                "relativePath": relative_path,
                "byteSize": file_size,
                "fileSha256": file_sha256,
                "candidateIds": sorted(
                    item["candidateId"] for item in file_candidates
                ),
            }
            inventory_records.update(
                json.dumps(
                    inventory_record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            inventory_records.update(b"\n")
            inventory_file_count += 1

        # Reverse insertion keeps a case-insensitive lexical DFS when popped.
        stack.extend(reversed(child_directories))

    candidate_buffer.sort(key=_candidate_rank_key)
    del candidate_buffer[candidate_limit:]
    if candidates_discovered > max_candidates:
        partial_reasons.add("max-candidates-reached")
    if candidates_discovered > candidate_limit and max_candidates > candidate_limit:
        partial_reasons.add("max-report-candidates-reached")

    candidates: list[dict[str, Any]] = []
    candidate_payload_bytes = 0
    for candidate in candidate_buffer:
        candidate_bytes = _json_utf8_bytes(candidate)
        if candidate_payload_bytes + candidate_bytes > _MAX_CANDIDATE_PAYLOAD_JSON_BYTES:
            partial_reasons.add("max-report-json-bytes-reached")
            break
        candidates.append(candidate)
        candidate_payload_bytes += candidate_bytes
    skip_details.sort(
        key=lambda item: (item["relativePath"].casefold(), item["reason"])
    )
    def build_report(candidate_values: list[dict[str, Any]]) -> dict[str, Any]:
        status = "partial" if partial_reasons else "complete"
        inventory_material = {
            "status": status,
            "fileCount": inventory_file_count,
            "fileRecordsSha256": inventory_records.hexdigest(),
        }
        inventory_sha256 = hashlib.sha256(
            json.dumps(
                inventory_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        report = {
            "schema": RECOVERY_ASSET_SCHEMA,
            "schemaVersion": RECOVERY_ASSET_SCHEMA_VERSION,
            "root": str(root),
            "status": status,
            "partialReasons": sorted(partial_reasons),
            "limits": {
                "maxFiles": max_files,
                "maxFileSizeBytes": max_file_size_bytes,
                "maxTotalReadBytes": max_total_read_bytes,
                "maxCandidates": max_candidates,
                "maxFilesystemEntries": max_filesystem_entries,
                "maximumReportCandidates": _MAX_REPORT_CANDIDATES,
                "maximumCandidateJsonUtf8Bytes": _MAX_CANDIDATE_JSON_BYTES,
                "maximumCandidatePayloadJsonUtf8Bytes": (
                    _MAX_CANDIDATE_PAYLOAD_JSON_BYTES
                ),
                "maximumReportJsonUtf8Bytes": _MAX_REPORT_JSON_BYTES,
                "maximumHintItemsPerField": _MAX_HINT_ITEMS,
                "maximumRelativePathUtf8Bytes": _MAX_RELATIVE_PATH_UTF8_BYTES,
            },
            "filesystemEntriesVisited": filesystem_entries_visited,
            "filesVisited": files_visited,
            "filesRead": files_visited
            - counters["tooLarge"]
            - counters["totalBudget"]
            - counters["readErrors"]
            - counters["secureOpenUnavailable"]
            - counters["reparseOrSymlinkRaces"],
            "totalBytesRead": bytes_read,
            "candidateCountDiscovered": candidates_discovered,
            "candidateCount": len(candidate_values),
            "inventorySha256": inventory_sha256,
            "skipped": {
                "symlinks": counters["symlinks"],
                "outsideRoot": counters["outsideRoot"],
                "nonRegularFiles": counters["nonRegularFiles"],
                "pathTooLong": counters["pathTooLong"],
                "tooLarge": counters["tooLarge"],
                "totalBudget": counters["totalBudget"],
                "filesystemErrors": counters["filesystemErrors"],
                "directoryReadErrors": counters["directoryReadErrors"],
                "readErrors": counters["readErrors"],
                "secureOpenUnavailable": counters["secureOpenUnavailable"],
                "reparseOrSymlinkRaces": counters["reparseOrSymlinkRaces"],
            },
            "skippedDetails": skip_details,
            "captureFacts": capture if capture_facts is not None else None,
            "policy": {
                "assetCodeExecuted": False,
                "compilerInvoked": False,
                "allSourceAndEngineHintsTrusted": False,
                "applyAuthorized": False,
                "workspaceSeedAuthorized": False,
            },
            "reportPolicy": {
                "candidateSelection": "assessment-ranked-bounded-top-k",
                "metadataBounded": True,
                "candidateDetailsMayBeTruncated": True,
                "secureFileOpen": (
                    "windows-root-handle-ntcreatefile-obj-dont-reparse"
                    if os.name == "nt"
                    else "posix-openat-dirfd-o-nofollow"
                ),
                "pathChecksAreAuthority": False,
                "sameHandleFstatBeforeAndAfter": True,
                "secureOpenUnavailableFailsClosed": True,
                "secureOpenBoundary": (
                    "all relative components below the opened asset root; "
                    "namespace ancestors above asset_root remain process-owner scope"
                ),
            },
            "reportJsonUtf8Bytes": 0,
            "candidates": candidate_values,
        }
        # Include the size field in its own accounting.  At most the number of
        # decimal digits changes, so this converges immediately in practice.
        for _ in range(4):
            measured = _json_utf8_bytes(report)
            if report["reportJsonUtf8Bytes"] == measured:
                break
            report["reportJsonUtf8Bytes"] = measured
        return report

    report = build_report(candidates)
    if report["reportJsonUtf8Bytes"] > _MAX_REPORT_JSON_BYTES:
        partial_reasons.add("max-report-json-bytes-reached")
        # Find the longest best-first prefix which satisfies the total report
        # budget.  This preserves top-K semantics under the secondary byte cap.
        low, high = 0, len(candidates)
        while low < high:
            middle = (low + high + 1) // 2
            candidate_report = build_report(candidates[:middle])
            if candidate_report["reportJsonUtf8Bytes"] <= _MAX_REPORT_JSON_BYTES:
                low = middle
            else:
                high = middle - 1
        candidates = candidates[:low]
        report = build_report(candidates)
    if report["reportJsonUtf8Bytes"] > _MAX_REPORT_JSON_BYTES:
        raise RuntimeError("recovery inventory summary exceeds the fixed report bound")
    return report


# Short alias for integrations that call the module by its purpose.
scan_recovery_assets = discover_recovery_assets


__all__ = [
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_FILE_SIZE_BYTES",
    "DEFAULT_MAX_TOTAL_READ_BYTES",
    "RECOVERY_ASSET_SCHEMA",
    "RECOVERY_ASSET_SCHEMA_VERSION",
    "discover_recovery_assets",
    "evaluate_recovery_candidate",
    "read_recovery_regular_file",
    "scan_recovery_assets",
    "score_recovery_candidate",
]
