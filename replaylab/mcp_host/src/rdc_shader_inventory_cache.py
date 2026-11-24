"""Strict sidecar cache for first-open ReplayLab Shader inventories.

The cache is deliberately *not* replay or replacement authority.  It stores a
bounded, allow-listed projection of one RDC's runtime Shader inventory so a
later open can route a small set of candidates to fresh target-capture
validation.  ``ResourceId`` and representative EIDs remain capture-local hints;
callers must re-read the target Shader and revalidate its ABI before applying
project knowledge.

Each cache lives below the Replay Project that owns the matching scope::

    derived/shader-resource-inventories/<normalised-capture-path-sha256>.json

The RDC file observation is only a cheap invalidation key.  It is intentionally
not a content identity and never grants apply authority.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any


RDC_SHADER_INVENTORY_CACHE_SCHEMA = (
    "replaylab-project-shader-resource-inventory-cache/v1"
)
RDC_SHADER_INVENTORY_CACHE_VERSION = 1
RDC_SHADER_INVENTORY_CACHE_DIRECTORY = "shader-resource-inventories"
MAX_RDC_SHADER_INVENTORY_CACHE_BYTES = 8 * 1024 * 1024

DEFAULT_INVENTORY_ALGORITHM_VERSION = "unique-shader-resource-reflection/v1"
DEFAULT_MATCHING_POLICY_VERSION = "stable-key-fresh-target-validation/v1"

MAX_SHADER_ASSETS = 8192
MAX_CONSTANT_BLOCKS_PER_ASSET = 256
MAX_CBUFFER_FIELDS = 131072
MAX_UNINDEXED_RESOURCE_IDS = 65536
MAX_DIAGNOSTICS = 4096
MAX_TEXT_BYTES = 4096
MAX_PATH_TEXT_BYTES = 2048
MAX_ENTRY_POINT_BYTES = 512

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9._+/-]{0,127}$")
_STAGE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

_SUPPORTED_APIS = frozenset(("d3d11", "d3d12", "opengl", "vulkan"))
_SUPPORTED_ENCODINGS_BY_API = {
    "d3d11": frozenset(("dxbc",)),
    "d3d12": frozenset(("dxbc", "dxil")),
    "opengl": frozenset(("glsl", "spirv", "opengl-spirv")),
    "vulkan": frozenset(("spirv",)),
}
_RENDERDOC_STAGES = frozenset(
    (
        "vertex",
        "hull",
        "domain",
        "geometry",
        "fragment",
        "compute",
        "task",
        "mesh",
        "raygen",
        "intersection",
        "anyhit",
        "closesthit",
        "miss",
        "callable",
    )
)

_DOCUMENT_FIELDS = frozenset(
    (
        "schema",
        "schemaVersion",
        "projectId",
        "scopeSha256",
        "capturePathSha256",
        "fileObservation",
        "inventoryAlgorithmVersion",
        "matchingPolicyVersion",
        "routingOnly",
        "freshTargetValidationRequired",
        "crossCaptureResourceIdReuse",
        "resourceOwnershipScope",
        "coverage",
        "assets",
        "sealSha256",
    )
)
_OBSERVATION_FIELDS = frozenset(("sizeBytes", "mtimeNs", "fileIdentity"))
_COVERAGE_FIELDS = frozenset(
    (
        "inventoryComplete",
        "assetCount",
        "scannedResourceCount",
        "shaderResourceCount",
        "uniqueShaderResourceCount",
        "processedShaderResourceCount",
        "indexedShaderResourceCount",
        "unindexedShaderResourceCount",
        "unindexedShaderResourceIds",
        "inspectedEntryPointCount",
        "anchorQueryCount",
        "anchorUsageRecordCount",
        "representativeEidCount",
        "anchorPerShaderMaximum",
        "eventTraversalCount",
        "fullFrameTraversal",
        "gpuReplayMutation",
        "diagnostics",
    )
)
_DIAGNOSTIC_FIELDS = frozenset(
    ("code", "resourceId", "stage", "entryPoint", "rawSha256", "rawSize", "count")
)
_ASSET_FIELDS = frozenset(
    (
        "resourceId",
        "resourceOwnershipScope",
        "crossCaptureResourceIdReuse",
        "routingOnly",
        "freshTargetValidationRequired",
        "api",
        "encoding",
        "stage",
        "entryPoint",
        "rawSha256",
        "rawSize",
        "abiHash",
        "interfaceHash",
        "layoutHash",
        "interfaceComplete",
        "interfaceEvidence",
        "physicalAbiSha256",
        "physicalAbiValidated",
        "stableKeyMaterial",
        "stableKeySha256",
        "stableKeyComplete",
        "identityAmbiguous",
        "formatSupported",
        "anchorEventId",
        "anchorSource",
        "requiresAnchorPreflight",
        "constantBlocks",
    )
)
_STABLE_KEY_FIELDS = frozenset(
    ("api", "encoding", "rawSha256", "stage", "entryPoint", "abiHash", "layoutHash")
)
_CONSTANT_BLOCK_FIELDS = frozenset(
    (
        "index",
        "path",
        "name",
        "space",
        "bind",
        "byteSize",
        "layoutSha256",
        "abiSha256",
        "fields",
    )
)
_CBUFFER_FIELD_FIELDS = frozenset(
    (
        "canonicalPath",
        "name",
        "absoluteByteOffset",
        "byteSize",
        "baseType",
        "rows",
        "columns",
        "elements",
        "arrayByteStride",
        "matrixByteStride",
        "rowMajor",
        "flags",
        "pointerTypeID",
        "bitFieldOffset",
        "bitFieldSize",
    )
)


class RdcShaderInventoryCacheError(ValueError):
    """Raised when a Shader inventory cache is malformed or unsafe."""


class RdcShaderInventoryCacheMiss(RdcShaderInventoryCacheError):
    """Raised by a writer when its RDC observation changes before publication."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache is not canonical JSON"
        ) from exc


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise RdcShaderInventoryCacheError(
        "Shader inventory cache contains non-finite JSON: %s" % value
    )


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RdcShaderInventoryCacheError(
                "Shader inventory cache contains duplicate JSON key: %s" % key
            )
        result[key] = value
    return result


def _require_fields(
    value: Any, expected: frozenset[str], label: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise RdcShaderInventoryCacheError("%s fields are invalid" % label)
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise RdcShaderInventoryCacheError("%s must be a boolean" % label)
    return value


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = (1 << 63) - 1,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RdcShaderInventoryCacheError("%s must be an integer" % label)
    if value < minimum or value > maximum:
        raise RdcShaderInventoryCacheError(
            "%s must be between %d and %d" % (label, minimum, maximum)
        )
    return value


def _optional_integer(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = (1 << 63) - 1,
) -> int | None:
    if value is None:
        return None
    return _integer(value, label, minimum=minimum, maximum=maximum)


def _text(
    value: Any,
    label: str,
    *,
    maximum_bytes: int = MAX_TEXT_BYTES,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise RdcShaderInventoryCacheError("%s must be text" % label)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RdcShaderInventoryCacheError("%s must be valid UTF-8" % label) from exc
    if len(encoded) > maximum_bytes:
        raise RdcShaderInventoryCacheError("%s exceeds its UTF-8 byte limit" % label)
    if any(ord(character) < 0x20 for character in value):
        raise RdcShaderInventoryCacheError("%s contains control characters" % label)
    return value


def _optional_text(
    value: Any, label: str, *, maximum_bytes: int = MAX_TEXT_BYTES
) -> str | None:
    if value is None:
        return None
    return _text(value, label, maximum_bytes=maximum_bytes)


def _sha256(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise RdcShaderInventoryCacheError("%s must be a lowercase SHA-256" % label)
    return value


def _optional_sha256(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, label)


def _project_id(value: Any, label: str = "projectId") -> str:
    if not isinstance(value, str) or _HEX32_RE.fullmatch(value) is None:
        raise RdcShaderInventoryCacheError(
            "%s must be 32 lowercase hexadecimal characters" % label
        )
    return value


def _version_text(value: Any, label: str) -> str:
    result = _text(value, label, maximum_bytes=128)
    if _TOKEN_RE.fullmatch(result) is None:
        raise RdcShaderInventoryCacheError("%s is not a canonical version token" % label)
    return result


def _stage(value: Any, label: str) -> str:
    result = _text(value, label, maximum_bytes=32)
    if _STAGE_RE.fullmatch(result) is None or result not in _RENDERDOC_STAGES:
        raise RdcShaderInventoryCacheError(
            "%s is not a supported canonical RenderDoc stage" % label
        )
    return result


def _api(value: Any, label: str) -> str:
    result = _text(value, label, maximum_bytes=16)
    if result not in _SUPPORTED_APIS:
        raise RdcShaderInventoryCacheError("%s is not a supported protocol API" % label)
    return result


def _encoding(value: Any, label: str) -> str:
    result = _text(value, label, maximum_bytes=64)
    if re.fullmatch(r"[a-z][a-z0-9._+-]{0,63}", result) is None:
        raise RdcShaderInventoryCacheError("%s is not a canonical encoding" % label)
    return result


def _normalised_capture_path(capture_path: str | Path) -> Path:
    raw = Path(capture_path).expanduser()
    if not raw.is_absolute():
        raise RdcShaderInventoryCacheError("capture path must be absolute")
    if raw.is_symlink():
        raise RdcShaderInventoryCacheError("capture path must not be a symlink")
    try:
        value = raw.lstat()
    except OSError as exc:
        raise RdcShaderInventoryCacheError("capture file is unavailable") from exc
    if not stat.S_ISREG(value.st_mode):
        raise RdcShaderInventoryCacheError("capture path must name a regular file")
    try:
        return raw.resolve(strict=True)
    except OSError as exc:
        raise RdcShaderInventoryCacheError("capture path could not be resolved") from exc


def capture_path_sha256(capture_path: str | Path) -> str:
    """Return the project-standard digest of one normalised absolute RDC path."""

    path = _normalised_capture_path(capture_path)
    canonical = os.path.normcase(str(path)).replace("\\", "/")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _capture_file_identity(value: Any, platform_name: str | None = None) -> str:
    """Return one stable file identity across supported Python builds.

    On Windows, CPython builds have exposed ``st_dev`` both as the 32-bit
    volume serial number and as a wider value whose low 32 bits are that same
    serial number. Persisting the raw value made a valid RDC sidecar miss when
    ReplayLab moved between those runtimes. The Windows kernel identity used
    here is the stable volume serial plus file index; other platforms retain
    their native device/inode pair unchanged.
    """

    device = int(getattr(value, "st_dev", 0))
    inode = int(getattr(value, "st_ino", 0))
    current_platform = os.name if platform_name is None else platform_name
    if current_platform == "nt":
        device &= 0xFFFFFFFF
    return "%d:%d" % (device, inode)


def capture_file_observation(capture_path: str | Path) -> dict[str, Any]:
    """Return a cheap invalidation observation, never a capture identity."""

    path = _normalised_capture_path(capture_path)
    try:
        value = path.stat()
    except OSError as exc:
        raise RdcShaderInventoryCacheError(
            "capture file observation could not be read"
        ) from exc
    if not stat.S_ISREG(value.st_mode):
        raise RdcShaderInventoryCacheError("capture path must name a regular file")
    return {
        "sizeBytes": _integer(int(value.st_size), "capture sizeBytes"),
        "mtimeNs": _integer(
            int(
                getattr(
                    value,
                    "st_mtime_ns",
                    int(float(value.st_mtime) * 1000000000),
                )
            ),
            "capture mtimeNs",
        ),
        "fileIdentity": _capture_file_identity(value),
    }


def _normalise_observation(value: Any, label: str) -> dict[str, Any]:
    source = _require_fields(value, _OBSERVATION_FIELDS, label)
    identity = _text(source["fileIdentity"], "%s.fileIdentity" % label, maximum_bytes=128)
    if re.fullmatch(r"[0-9]+:[0-9]+", identity) is None:
        raise RdcShaderInventoryCacheError(
            "%s.fileIdentity is not a canonical file identity" % label
        )
    return {
        "sizeBytes": _integer(source["sizeBytes"], "%s.sizeBytes" % label),
        "mtimeNs": _integer(source["mtimeNs"], "%s.mtimeNs" % label),
        "fileIdentity": identity,
    }


def _file_observations_match(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> bool:
    """Compare observations, accepting the two historical Windows dev forms."""

    if (
        left.get("sizeBytes") != right.get("sizeBytes")
        or left.get("mtimeNs") != right.get("mtimeNs")
    ):
        return False
    left_identity = str(left.get("fileIdentity", ""))
    right_identity = str(right.get("fileIdentity", ""))
    if left_identity == right_identity:
        return True
    if os.name != "nt":
        return False
    try:
        left_device, left_inode = (
            int(part) for part in left_identity.split(":", 1)
        )
        right_device, right_inode = (
            int(part) for part in right_identity.split(":", 1)
        )
    except (TypeError, ValueError):
        return False
    return (
        left_inode == right_inode
        and (left_device & 0xFFFFFFFF) == (right_device & 0xFFFFFFFF)
    )


def _normalise_cbuffer_field(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RdcShaderInventoryCacheError("%s must be an object" % label)
    # Runtime bridge objects are projected through this allowlist.  A sealed
    # document is checked separately against the exact field set.
    return {
        "canonicalPath": _text(
            value.get("canonicalPath"),
            "%s.canonicalPath" % label,
            maximum_bytes=MAX_PATH_TEXT_BYTES,
        ),
        "name": _text(value.get("name"), "%s.name" % label, maximum_bytes=512),
        "absoluteByteOffset": _integer(
            value.get("absoluteByteOffset"),
            "%s.absoluteByteOffset" % label,
            maximum=(1 << 32) - 1,
        ),
        "byteSize": _integer(
            value.get("byteSize"),
            "%s.byteSize" % label,
            minimum=1,
            maximum=(1 << 32) - 1,
        ),
        "baseType": _text(
            value.get("baseType"), "%s.baseType" % label, maximum_bytes=64
        ),
        "rows": _integer(
            value.get("rows"), "%s.rows" % label, maximum=(1 << 20)
        ),
        "columns": _integer(
            value.get("columns"), "%s.columns" % label, maximum=(1 << 20)
        ),
        "elements": _integer(
            value.get("elements"), "%s.elements" % label, maximum=(1 << 20)
        ),
        "arrayByteStride": _integer(
            value.get("arrayByteStride"),
            "%s.arrayByteStride" % label,
            maximum=(1 << 32) - 1,
        ),
        "matrixByteStride": _integer(
            value.get("matrixByteStride"),
            "%s.matrixByteStride" % label,
            maximum=(1 << 32) - 1,
        ),
        "rowMajor": _boolean(value.get("rowMajor"), "%s.rowMajor" % label),
        "flags": _integer(
            value.get("flags"), "%s.flags" % label, maximum=(1 << 64) - 1
        ),
        "pointerTypeID": _integer(
            value.get("pointerTypeID"),
            "%s.pointerTypeID" % label,
            maximum=(1 << 64) - 1,
        ),
        "bitFieldOffset": _integer(
            value.get("bitFieldOffset"),
            "%s.bitFieldOffset" % label,
            maximum=(1 << 32) - 1,
        ),
        "bitFieldSize": _integer(
            value.get("bitFieldSize"),
            "%s.bitFieldSize" % label,
            maximum=(1 << 32) - 1,
        ),
    }


def _normalise_constant_block(
    value: Any, label: str, field_budget: list[int]
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RdcShaderInventoryCacheError("%s must be an object" % label)
    raw_fields = value.get("fields", [])
    if not isinstance(raw_fields, Sequence) or isinstance(
        raw_fields, (str, bytes, bytearray)
    ):
        raise RdcShaderInventoryCacheError("%s.fields must be an array" % label)
    fields = []
    for index, field in enumerate(raw_fields):
        field_budget[0] += 1
        if field_budget[0] > MAX_CBUFFER_FIELDS:
            raise RdcShaderInventoryCacheError(
                "Shader inventory cache has too many CBuffer fields"
            )
        fields.append(
            _normalise_cbuffer_field(field, "%s.fields[%d]" % (label, index))
        )
    fields.sort(
        key=lambda item: (
            item["absoluteByteOffset"],
            item["canonicalPath"],
            item["name"],
            item["byteSize"],
        )
    )
    field_paths = [item["canonicalPath"] for item in fields]
    if len(set(field_paths)) != len(field_paths):
        raise RdcShaderInventoryCacheError(
            "%s.fields contains duplicate canonical paths" % label
        )
    path = _text(
        value.get("path"), "%s.path" % label, maximum_bytes=MAX_PATH_TEXT_BYTES
    )
    return {
        "index": _integer(
            value.get("index"), "%s.index" % label, maximum=(1 << 16) - 1
        ),
        "path": path,
        "name": _text(
            value.get("name", path), "%s.name" % label, maximum_bytes=512
        ),
        "space": _integer(
            value.get("space"), "%s.space" % label, maximum=(1 << 32) - 1
        ),
        "bind": _integer(
            value.get("bind"), "%s.bind" % label, maximum=(1 << 32) - 1
        ),
        "byteSize": _integer(
            value.get("byteSize"),
            "%s.byteSize" % label,
            minimum=1,
            maximum=(1 << 32) - 1,
        ),
        "layoutSha256": _sha256(
            value.get("layoutSha256"), "%s.layoutSha256" % label
        ),
        "abiSha256": _sha256(value.get("abiSha256"), "%s.abiSha256" % label),
        "fields": fields,
    }


def _stable_key_material(value: Any, label: str) -> dict[str, str]:
    source = _require_fields(value, _STABLE_KEY_FIELDS, label)
    return {
        "api": _api(source["api"], "%s.api" % label),
        "encoding": _encoding(source["encoding"], "%s.encoding" % label),
        "rawSha256": _sha256(source["rawSha256"], "%s.rawSha256" % label),
        "stage": _stage(source["stage"], "%s.stage" % label),
        "entryPoint": _text(
            source["entryPoint"],
            "%s.entryPoint" % label,
            maximum_bytes=MAX_ENTRY_POINT_BYTES,
            allow_empty=True,
        ),
        "abiHash": _sha256(
            source["abiHash"], "%s.abiHash" % label, allow_empty=True
        ),
        "layoutHash": _sha256(
            source["layoutHash"], "%s.layoutHash" % label, allow_empty=True
        ),
    }


def _normalise_asset(
    value: Any, label: str, field_budget: list[int]
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RdcShaderInventoryCacheError("%s must be an object" % label)

    api = _api(value.get("api"), "%s.api" % label)
    encoding = _encoding(value.get("encoding"), "%s.encoding" % label)
    stage = _stage(value.get("stage"), "%s.stage" % label)
    entry_point = _text(
        value.get("entryPoint", ""),
        "%s.entryPoint" % label,
        maximum_bytes=MAX_ENTRY_POINT_BYTES,
        allow_empty=True,
    )
    raw_sha256 = _sha256(value.get("rawSha256"), "%s.rawSha256" % label)
    abi_hash = _sha256(
        value.get("abiHash", ""), "%s.abiHash" % label, allow_empty=True
    )
    interface_hash = _sha256(
        value.get("interfaceHash", ""),
        "%s.interfaceHash" % label,
        allow_empty=True,
    )
    layout_hash = _sha256(
        value.get("layoutHash", ""), "%s.layoutHash" % label, allow_empty=True
    )
    format_supported = _boolean(
        value.get("formatSupported"), "%s.formatSupported" % label
    )
    computed_format_supported = encoding in _SUPPORTED_ENCODINGS_BY_API[api]
    if format_supported != computed_format_supported:
        raise RdcShaderInventoryCacheError(
            "%s.formatSupported disagrees with its API/encoding" % label
        )

    material = _stable_key_material(
        value.get("stableKeyMaterial"), "%s.stableKeyMaterial" % label
    )
    expected_material = {
        "api": api,
        "encoding": encoding,
        "rawSha256": raw_sha256,
        "stage": stage,
        "entryPoint": entry_point,
        "abiHash": abi_hash,
        "layoutHash": layout_hash,
    }
    if material != expected_material:
        raise RdcShaderInventoryCacheError(
            "%s.stableKeyMaterial disagrees with the Shader identity" % label
        )
    stable_key_sha256 = _sha256(
        value.get("stableKeySha256"), "%s.stableKeySha256" % label
    )
    if not hmac.compare_digest(stable_key_sha256, _canonical_sha256(material)):
        raise RdcShaderInventoryCacheError(
            "%s.stableKeySha256 does not match its material" % label
        )

    physical_abi_validated = _boolean(
        value.get("physicalAbiValidated"), "%s.physicalAbiValidated" % label
    )
    stable_key_complete = _boolean(
        value.get("stableKeyComplete"), "%s.stableKeyComplete" % label
    )
    if stable_key_complete and (
        not physical_abi_validated
        or not format_supported
        or not all(
            material[name]
            for name in ("api", "encoding", "rawSha256", "stage", "abiHash", "layoutHash")
        )
    ):
        raise RdcShaderInventoryCacheError(
            "%s.stableKeyComplete has incomplete physical identity evidence" % label
        )

    anchor_event_id = _optional_integer(
        value.get("anchorEventId"),
        "%s.anchorEventId" % label,
        minimum=1,
        maximum=(1 << 32) - 1,
    )
    anchor_source = _optional_text(
        value.get("anchorSource"), "%s.anchorSource" % label, maximum_bytes=128
    )
    if (anchor_event_id is None) != (anchor_source is None):
        raise RdcShaderInventoryCacheError(
            "%s anchorEventId and anchorSource must be present together" % label
        )

    raw_blocks = value.get("constantBlocks", [])
    if not isinstance(raw_blocks, Sequence) or isinstance(
        raw_blocks, (str, bytes, bytearray)
    ):
        raise RdcShaderInventoryCacheError("%s.constantBlocks must be an array" % label)
    if len(raw_blocks) > MAX_CONSTANT_BLOCKS_PER_ASSET:
        raise RdcShaderInventoryCacheError("%s has too many constant blocks" % label)
    blocks = [
        _normalise_constant_block(
            block, "%s.constantBlocks[%d]" % (label, index), field_budget
        )
        for index, block in enumerate(raw_blocks)
    ]
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
        raise RdcShaderInventoryCacheError(
            "%s.constantBlocks contains duplicate bindings" % label
        )

    # Owner session, raw bytecode, bridge receipts, replacement state, and all
    # executable/apply eligibility flags are intentionally absent.  The fixed
    # fields below make the remaining ResourceId's routing-only semantics local
    # even if an asset is copied out of its parent document.
    return {
        "resourceId": _integer(
            value.get("resourceId"),
            "%s.resourceId" % label,
            minimum=1,
            maximum=(1 << 64) - 1,
        ),
        "resourceOwnershipScope": "cached-capture-routing-only",
        "crossCaptureResourceIdReuse": False,
        "routingOnly": True,
        "freshTargetValidationRequired": True,
        "api": api,
        "encoding": encoding,
        "stage": stage,
        "entryPoint": entry_point,
        "rawSha256": raw_sha256,
        "rawSize": _integer(
            value.get("rawSize"), "%s.rawSize" % label, maximum=(1 << 32) - 1
        ),
        "abiHash": abi_hash,
        "interfaceHash": interface_hash,
        "layoutHash": layout_hash,
        "interfaceComplete": _boolean(
            value.get("interfaceComplete"), "%s.interfaceComplete" % label
        ),
        "interfaceEvidence": _text(
            value.get("interfaceEvidence"),
            "%s.interfaceEvidence" % label,
            maximum_bytes=128,
        ),
        "physicalAbiSha256": _sha256(
            value.get("physicalAbiSha256", ""),
            "%s.physicalAbiSha256" % label,
            allow_empty=True,
        ),
        "physicalAbiValidated": physical_abi_validated,
        "stableKeyMaterial": material,
        "stableKeySha256": stable_key_sha256,
        "stableKeyComplete": stable_key_complete,
        "identityAmbiguous": _boolean(
            value.get("identityAmbiguous"), "%s.identityAmbiguous" % label
        ),
        "formatSupported": format_supported,
        "anchorEventId": anchor_event_id,
        "anchorSource": anchor_source,
        # Persisted anchors can route a preflight but can never skip it.
        "requiresAnchorPreflight": True,
        "constantBlocks": blocks,
    }


def _normalise_diagnostic(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RdcShaderInventoryCacheError("%s must be an object" % label)
    stage_value = value.get("stage")
    entry_value = value.get("entryPoint")
    return {
        "code": _text(value.get("code"), "%s.code" % label, maximum_bytes=128),
        "resourceId": _optional_integer(
            value.get("resourceId"),
            "%s.resourceId" % label,
            minimum=1,
            maximum=(1 << 64) - 1,
        ),
        "stage": None if stage_value is None else _stage(stage_value, "%s.stage" % label),
        "entryPoint": (
            None
            if entry_value is None
            else _text(
                entry_value,
                "%s.entryPoint" % label,
                maximum_bytes=MAX_ENTRY_POINT_BYTES,
                allow_empty=True,
            )
        ),
        "rawSha256": _optional_sha256(
            value.get("rawSha256"), "%s.rawSha256" % label
        ),
        "rawSize": _optional_integer(
            value.get("rawSize"), "%s.rawSize" % label, maximum=(1 << 32) - 1
        ),
        "count": _optional_integer(
            value.get("count"), "%s.count" % label, maximum=(1 << 32) - 1
        ),
    }


def _normalise_coverage(
    value: Any, assets: Sequence[Mapping[str, Any]], *, sealed: bool
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RdcShaderInventoryCacheError("coverage must be an object")
    if sealed:
        _require_fields(value, _COVERAGE_FIELDS, "coverage")

    indexed_ids = sorted({int(asset["resourceId"]) for asset in assets})
    raw_unindexed = value.get("unindexedShaderResourceIds", [])
    if not isinstance(raw_unindexed, Sequence) or isinstance(
        raw_unindexed, (str, bytes, bytearray)
    ):
        raise RdcShaderInventoryCacheError(
            "coverage.unindexedShaderResourceIds must be an array"
        )
    if len(raw_unindexed) > MAX_UNINDEXED_RESOURCE_IDS:
        raise RdcShaderInventoryCacheError(
            "coverage has too many unindexed Shader ResourceIds"
        )
    unindexed_ids = sorted(
        _integer(
            item,
            "coverage.unindexedShaderResourceIds[%d]" % index,
            minimum=1,
            maximum=(1 << 64) - 1,
        )
        for index, item in enumerate(raw_unindexed)
    )
    if len(set(unindexed_ids)) != len(unindexed_ids):
        raise RdcShaderInventoryCacheError(
            "coverage.unindexedShaderResourceIds contains duplicates"
        )
    if set(indexed_ids).intersection(unindexed_ids):
        raise RdcShaderInventoryCacheError(
            "coverage indexes and omits the same Shader ResourceId"
        )

    raw_diagnostics = value.get("diagnostics", [])
    if not isinstance(raw_diagnostics, Sequence) or isinstance(
        raw_diagnostics, (str, bytes, bytearray)
    ):
        raise RdcShaderInventoryCacheError("coverage.diagnostics must be an array")
    if len(raw_diagnostics) > MAX_DIAGNOSTICS:
        raise RdcShaderInventoryCacheError("coverage has too many diagnostics")
    if sealed:
        for index, item in enumerate(raw_diagnostics):
            _require_fields(
                item,
                _DIAGNOSTIC_FIELDS,
                "coverage.diagnostics[%d]" % index,
            )
    diagnostics = [
        _normalise_diagnostic(item, "coverage.diagnostics[%d]" % index)
        for index, item in enumerate(raw_diagnostics)
    ]
    diagnostics.sort(key=_canonical_bytes)

    unique_count = len(indexed_ids) + len(unindexed_ids)
    anchor_count = len(
        {
            (int(asset["resourceId"]), int(asset["anchorEventId"]))
            for asset in assets
            if asset["anchorEventId"] is not None
        }
    )
    result = {
        "inventoryComplete": _boolean(
            value.get("inventoryComplete"), "coverage.inventoryComplete"
        ),
        "assetCount": _integer(
            value.get("assetCount", len(assets)), "coverage.assetCount"
        ),
        "scannedResourceCount": _integer(
            value.get("scannedResourceCount", unique_count),
            "coverage.scannedResourceCount",
        ),
        "shaderResourceCount": _integer(
            value.get("shaderResourceCount", unique_count),
            "coverage.shaderResourceCount",
        ),
        "uniqueShaderResourceCount": _integer(
            value.get("uniqueShaderResourceCount", unique_count),
            "coverage.uniqueShaderResourceCount",
        ),
        "processedShaderResourceCount": _integer(
            value.get("processedShaderResourceCount", unique_count),
            "coverage.processedShaderResourceCount",
        ),
        "indexedShaderResourceCount": _integer(
            value.get("indexedShaderResourceCount", len(indexed_ids)),
            "coverage.indexedShaderResourceCount",
        ),
        "unindexedShaderResourceCount": _integer(
            value.get("unindexedShaderResourceCount", len(unindexed_ids)),
            "coverage.unindexedShaderResourceCount",
        ),
        "unindexedShaderResourceIds": unindexed_ids,
        "inspectedEntryPointCount": _integer(
            value.get("inspectedEntryPointCount", len(assets)),
            "coverage.inspectedEntryPointCount",
        ),
        "anchorQueryCount": _integer(
            value.get("anchorQueryCount", anchor_count),
            "coverage.anchorQueryCount",
        ),
        "anchorUsageRecordCount": _integer(
            value.get("anchorUsageRecordCount", anchor_count),
            "coverage.anchorUsageRecordCount",
        ),
        "representativeEidCount": _integer(
            value.get("representativeEidCount", anchor_count),
            "coverage.representativeEidCount",
        ),
        "anchorPerShaderMaximum": _integer(
            value.get("anchorPerShaderMaximum", 1),
            "coverage.anchorPerShaderMaximum",
            maximum=1,
        ),
        "eventTraversalCount": _integer(
            value.get("eventTraversalCount", 0), "coverage.eventTraversalCount"
        ),
        "fullFrameTraversal": _boolean(
            value.get("fullFrameTraversal", False), "coverage.fullFrameTraversal"
        ),
        "gpuReplayMutation": _boolean(
            value.get("gpuReplayMutation", False), "coverage.gpuReplayMutation"
        ),
        "diagnostics": diagnostics,
    }
    if result["inventoryComplete"] is not True:
        raise RdcShaderInventoryCacheError(
            "only a complete Shader inventory may be cached"
        )
    if result["assetCount"] != len(assets):
        raise RdcShaderInventoryCacheError("coverage.assetCount does not match assets")
    if result["indexedShaderResourceCount"] != len(indexed_ids):
        raise RdcShaderInventoryCacheError(
            "coverage.indexedShaderResourceCount does not match assets"
        )
    if result["unindexedShaderResourceCount"] != len(unindexed_ids):
        raise RdcShaderInventoryCacheError(
            "coverage.unindexedShaderResourceCount does not match its ledger"
        )
    if result["uniqueShaderResourceCount"] != unique_count:
        raise RdcShaderInventoryCacheError(
            "coverage.uniqueShaderResourceCount is inconsistent"
        )
    if result["shaderResourceCount"] != unique_count:
        raise RdcShaderInventoryCacheError(
            "coverage.shaderResourceCount is inconsistent"
        )
    if result["processedShaderResourceCount"] != unique_count:
        raise RdcShaderInventoryCacheError(
            "coverage.processedShaderResourceCount is inconsistent"
        )
    if result["scannedResourceCount"] < result["shaderResourceCount"]:
        raise RdcShaderInventoryCacheError(
            "coverage.scannedResourceCount is inconsistent"
        )
    if result["inspectedEntryPointCount"] < len(assets):
        raise RdcShaderInventoryCacheError(
            "coverage.inspectedEntryPointCount is incomplete"
        )
    if result["representativeEidCount"] != anchor_count:
        raise RdcShaderInventoryCacheError(
            "coverage.representativeEidCount does not match assets"
        )
    if result["anchorQueryCount"] > unique_count:
        raise RdcShaderInventoryCacheError("coverage.anchorQueryCount is inconsistent")
    if (
        result["anchorPerShaderMaximum"] != 1
        or result["eventTraversalCount"] != 0
        or result["fullFrameTraversal"] is not False
        or result["gpuReplayMutation"] is not False
    ):
        raise RdcShaderInventoryCacheError(
            "coverage is not a zero-EID-mutation Shader inventory"
        )
    return result


def _normalise_assets(value: Any, *, sealed: bool) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise RdcShaderInventoryCacheError("assets must be an array")
    if len(value) > MAX_SHADER_ASSETS:
        raise RdcShaderInventoryCacheError("Shader inventory cache has too many assets")
    field_budget = [0]
    assets = []
    for index, item in enumerate(value):
        if sealed:
            _require_fields(item, _ASSET_FIELDS, "assets[%d]" % index)
            raw_blocks = item.get("constantBlocks")
            if not isinstance(raw_blocks, Sequence) or isinstance(
                raw_blocks, (str, bytes, bytearray)
            ):
                raise RdcShaderInventoryCacheError(
                    "assets[%d].constantBlocks must be an array" % index
                )
            for block_index, block in enumerate(raw_blocks):
                _require_fields(
                    block,
                    _CONSTANT_BLOCK_FIELDS,
                    "assets[%d].constantBlocks[%d]" % (index, block_index),
                )
                raw_fields = block.get("fields")
                if not isinstance(raw_fields, Sequence) or isinstance(
                    raw_fields, (str, bytes, bytearray)
                ):
                    raise RdcShaderInventoryCacheError(
                        "assets[%d].constantBlocks[%d].fields must be an array"
                        % (index, block_index)
                    )
                for field_index, field in enumerate(raw_fields):
                    _require_fields(
                        field,
                        _CBUFFER_FIELD_FIELDS,
                        "assets[%d].constantBlocks[%d].fields[%d]"
                        % (index, block_index, field_index),
                    )
        assets.append(_normalise_asset(item, "assets[%d]" % index, field_budget))
    assets.sort(
        key=lambda item: (
            item["resourceId"],
            item["stage"],
            item["entryPoint"],
            item["encoding"],
            item["rawSha256"],
            item["abiHash"],
            item["layoutHash"],
            item["stableKeySha256"],
        )
    )
    canonical_assets = [_canonical_bytes(item) for item in assets]
    if len(set(canonical_assets)) != len(canonical_assets):
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache contains duplicate assets"
        )
    return assets


def _unsigned_document(
    *,
    project_id: str,
    scope_sha256: str,
    capture_path_digest: str,
    file_observation: Mapping[str, Any],
    inventory_algorithm_version: str,
    matching_policy_version: str,
    coverage: Mapping[str, Any],
    assets: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": RDC_SHADER_INVENTORY_CACHE_SCHEMA,
        "schemaVersion": RDC_SHADER_INVENTORY_CACHE_VERSION,
        "projectId": project_id,
        "scopeSha256": scope_sha256,
        "capturePathSha256": capture_path_digest,
        "fileObservation": copy.deepcopy(dict(file_observation)),
        "inventoryAlgorithmVersion": inventory_algorithm_version,
        "matchingPolicyVersion": matching_policy_version,
        "routingOnly": True,
        "freshTargetValidationRequired": True,
        "crossCaptureResourceIdReuse": False,
        "resourceOwnershipScope": "cached-capture-routing-only",
        "coverage": copy.deepcopy(dict(coverage)),
        "assets": copy.deepcopy(list(assets)),
    }


def build_shader_inventory_cache_document(
    project_id: str,
    scope_sha256: str,
    capture_path: str | Path,
    runtime_inventory: Mapping[str, Any],
    *,
    file_observation: Mapping[str, Any] | None = None,
    inventory_algorithm_version: str = DEFAULT_INVENTORY_ALGORITHM_VERSION,
    matching_policy_version: str = DEFAULT_MATCHING_POLICY_VERSION,
) -> dict[str, Any]:
    """Project a bridge runtime inventory into a sealed routing-only document."""

    if not isinstance(runtime_inventory, Mapping):
        raise RdcShaderInventoryCacheError("runtime inventory must be an object")
    clean_project_id = _project_id(project_id)
    clean_scope = _sha256(scope_sha256, "scopeSha256")
    path_digest = capture_path_sha256(capture_path)
    observation = _normalise_observation(
        file_observation
        if file_observation is not None
        else capture_file_observation(capture_path),
        "fileObservation",
    )
    algorithm = _version_text(
        inventory_algorithm_version, "inventoryAlgorithmVersion"
    )
    policy = _version_text(matching_policy_version, "matchingPolicyVersion")
    assets = _normalise_assets(runtime_inventory.get("assets"), sealed=False)
    coverage_source = runtime_inventory.get("coverage", runtime_inventory)
    coverage = _normalise_coverage(coverage_source, assets, sealed=False)
    unsigned = _unsigned_document(
        project_id=clean_project_id,
        scope_sha256=clean_scope,
        capture_path_digest=path_digest,
        file_observation=observation,
        inventory_algorithm_version=algorithm,
        matching_policy_version=policy,
        coverage=coverage,
        assets=assets,
    )
    result = {**unsigned, "sealSha256": _canonical_sha256(unsigned)}
    # Exercise the exact sealed-document validator before publication.
    return _normalise_document(result)


def _normalise_document(value: Any) -> dict[str, Any]:
    source = _require_fields(value, _DOCUMENT_FIELDS, "Shader inventory cache")
    if (
        source["schema"] != RDC_SHADER_INVENTORY_CACHE_SCHEMA
        or isinstance(source["schemaVersion"], bool)
        or source["schemaVersion"] != RDC_SHADER_INVENTORY_CACHE_VERSION
    ):
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache schema/version is unsupported"
        )
    if (
        source["routingOnly"] is not True
        or source["freshTargetValidationRequired"] is not True
        or source["crossCaptureResourceIdReuse"] is not False
        or source["resourceOwnershipScope"] != "cached-capture-routing-only"
    ):
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache attempts to carry apply authority"
        )
    project_id = _project_id(source["projectId"])
    scope_sha256 = _sha256(source["scopeSha256"], "scopeSha256")
    path_digest = _sha256(source["capturePathSha256"], "capturePathSha256")
    observation = _normalise_observation(source["fileObservation"], "fileObservation")
    algorithm = _version_text(
        source["inventoryAlgorithmVersion"], "inventoryAlgorithmVersion"
    )
    policy = _version_text(source["matchingPolicyVersion"], "matchingPolicyVersion")
    assets = _normalise_assets(source["assets"], sealed=True)
    coverage = _normalise_coverage(source["coverage"], assets, sealed=True)
    unsigned = _unsigned_document(
        project_id=project_id,
        scope_sha256=scope_sha256,
        capture_path_digest=path_digest,
        file_observation=observation,
        inventory_algorithm_version=algorithm,
        matching_policy_version=policy,
        coverage=coverage,
        assets=assets,
    )
    seal = _sha256(source["sealSha256"], "sealSha256")
    expected_seal = _canonical_sha256(unsigned)
    if not hmac.compare_digest(seal, expected_seal):
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache sealSha256 does not match its contents"
        )
    return {**unsigned, "sealSha256": seal}


def encode_shader_inventory_cache(document: Mapping[str, Any]) -> bytes:
    """Validate and encode one cache as exact canonical UTF-8 JSON."""

    clean = _normalise_document(document)
    payload = _canonical_bytes(clean)
    if len(payload) > MAX_RDC_SHADER_INVENTORY_CACHE_BYTES:
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache exceeds the 8 MiB limit"
        )
    return payload


def parse_shader_inventory_cache(
    payload: bytes | bytearray | memoryview | str,
) -> dict[str, Any]:
    """Parse an exact canonical v1 payload and verify its SHA-256 seal."""

    if isinstance(payload, str):
        try:
            raw = payload.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RdcShaderInventoryCacheError(
                "Shader inventory cache is not valid UTF-8"
            ) from exc
    elif isinstance(payload, (bytes, bytearray, memoryview)):
        raw = bytes(payload)
    else:
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache payload must be bytes or text"
        )
    if not raw or len(raw) > MAX_RDC_SHADER_INVENTORY_CACHE_BYTES:
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache payload must contain 1..%d bytes"
            % MAX_RDC_SHADER_INVENTORY_CACHE_BYTES
        )
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except UnicodeDecodeError as exc:
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache is not valid UTF-8"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache is not valid JSON"
        ) from exc
    clean = _normalise_document(value)
    canonical = _canonical_bytes(clean)
    if not hmac.compare_digest(raw, canonical):
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache payload is not canonical JSON"
        )
    return copy.deepcopy(clean)


def _project_root(project_root: str | Path) -> Path:
    raw = Path(project_root).expanduser()
    if not raw.is_absolute():
        raise RdcShaderInventoryCacheError("Replay Project root must be absolute")
    if raw.is_symlink():
        raise RdcShaderInventoryCacheError("Replay Project root must not be a symlink")
    try:
        value = raw.lstat()
    except OSError as exc:
        raise RdcShaderInventoryCacheError("Replay Project root is unavailable") from exc
    if not stat.S_ISDIR(value.st_mode):
        raise RdcShaderInventoryCacheError("Replay Project root must be a directory")
    try:
        return raw.resolve(strict=True)
    except OSError as exc:
        raise RdcShaderInventoryCacheError(
            "Replay Project root could not be resolved"
        ) from exc


def _validate_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        raise RdcShaderInventoryCacheError("%s must not be a symlink" % label)
    try:
        value = path.lstat()
    except OSError as exc:
        raise RdcShaderInventoryCacheError("%s is unavailable" % label) from exc
    if not stat.S_ISDIR(value.st_mode):
        raise RdcShaderInventoryCacheError("%s must be a directory" % label)


def _cache_directory(project_root: str | Path, *, create: bool) -> Path | None:
    root = _project_root(project_root)
    directories = (
        (root / "derived", "Replay Project derived directory"),
        (
            root / "derived" / RDC_SHADER_INVENTORY_CACHE_DIRECTORY,
            "Shader inventory cache directory",
        ),
    )
    for path, label in directories:
        if path.is_symlink():
            raise RdcShaderInventoryCacheError("%s must not be a symlink" % label)
        if path.exists():
            _validate_directory(path, label)
            continue
        if not create:
            return None
        try:
            path.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise RdcShaderInventoryCacheError("%s could not be created" % label) from exc
        _validate_directory(path, label)
    return directories[-1][0]


def shader_inventory_cache_path(
    project_root: str | Path, capture_path: str | Path
) -> Path:
    """Return the required sidecar path without creating its directories."""

    root = _project_root(project_root)
    digest = capture_path_sha256(capture_path)
    return (
        root
        / "derived"
        / RDC_SHADER_INVENTORY_CACHE_DIRECTORY
        / (digest + ".json")
    )


def _read_regular_payload(path: Path) -> bytes | None:
    if path.is_symlink():
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache file must not be a symlink"
        )
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache file could not be inspected"
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache path must be a regular file"
        )

    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache file could not be opened safely"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
            raise RdcShaderInventoryCacheError(
                "Shader inventory cache file changed before it was opened"
            )
        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                chunk = os.read(
                    descriptor,
                    min(
                        1024 * 1024,
                        MAX_RDC_SHADER_INVENTORY_CACHE_BYTES + 1 - total,
                    ),
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_RDC_SHADER_INVENTORY_CACHE_BYTES:
                    raise RdcShaderInventoryCacheError(
                        "Shader inventory cache exceeds the 8 MiB limit"
                    )
        except RdcShaderInventoryCacheError:
            raise
        except OSError as exc:
            raise RdcShaderInventoryCacheError(
                "Shader inventory cache file could not be read safely"
            ) from exc
    finally:
        os.close(descriptor)
    try:
        after = path.lstat()
    except OSError as exc:
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache file changed while it was read"
        ) from exc
    if path.is_symlink() or not os.path.samestat(opened, after):
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache file changed while it was read"
        )
    return b"".join(chunks)


def _destination_is_safe(path: Path) -> None:
    if path.is_symlink():
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache file must not be a symlink"
        )
    try:
        value = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache destination could not be inspected"
        ) from exc
    if not stat.S_ISREG(value.st_mode):
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache destination must be a regular file"
        )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = None
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        # The file itself is already fsynced and atomically renamed.  Some
        # filesystems do not permit directory fsync; treat that as best-effort.
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def write_shader_inventory_cache(
    project_root: str | Path,
    project_id: str,
    scope_sha256: str,
    capture_path: str | Path,
    runtime_inventory: Mapping[str, Any],
    *,
    inventory_algorithm_version: str = DEFAULT_INVENTORY_ALGORITHM_VERSION,
    matching_policy_version: str = DEFAULT_MATCHING_POLICY_VERSION,
) -> dict[str, Any]:
    """Atomically publish a complete sidecar and verify its exact readback."""

    capture = _normalised_capture_path(capture_path)
    observation = capture_file_observation(capture)
    document = build_shader_inventory_cache_document(
        project_id,
        scope_sha256,
        capture,
        runtime_inventory,
        file_observation=observation,
        inventory_algorithm_version=inventory_algorithm_version,
        matching_policy_version=matching_policy_version,
    )
    payload = encode_shader_inventory_cache(document)
    directory = _cache_directory(project_root, create=True)
    if directory is None:  # pragma: no cover - create=True is total.
        raise RdcShaderInventoryCacheError(
            "Shader inventory cache directory could not be created"
        )
    destination = directory / (document["capturePathSha256"] + ".json")
    _destination_is_safe(destination)

    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".shader-inventory-", suffix=".tmp", dir=str(directory)
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

        if capture_file_observation(capture) != observation:
            raise RdcShaderInventoryCacheMiss(
                "capture changed before Shader inventory cache publication"
            )
        _validate_directory(directory, "Shader inventory cache directory")
        _destination_is_safe(destination)
        os.replace(str(temporary_path), str(destination))
        temporary_path = None
        _fsync_directory(directory)

        readback = _read_regular_payload(destination)
        if readback is None or not hmac.compare_digest(readback, payload):
            raise RdcShaderInventoryCacheError(
                "Shader inventory cache exact readback did not match"
            )
        verified = parse_shader_inventory_cache(readback)
        if verified != document:
            raise RdcShaderInventoryCacheError(
                "Shader inventory cache readback changed its canonical document"
            )
        if capture_file_observation(capture) != observation:
            raise RdcShaderInventoryCacheMiss(
                "capture changed while Shader inventory cache was published"
            )
        return verified
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def load_shader_inventory_cache(
    project_root: str | Path,
    project_id: str,
    scope_sha256: str,
    capture_path: str | Path,
    *,
    inventory_algorithm_version: str = DEFAULT_INVENTORY_ALGORITHM_VERSION,
    matching_policy_version: str = DEFAULT_MATCHING_POLICY_VERSION,
) -> dict[str, Any] | None:
    """Load one exact cache, returning ``None`` for absence or stale binding.

    Malformed, oversized, non-canonical, sealed-tampered, or symlinked cache
    files raise :class:`RdcShaderInventoryCacheError`.  A valid document whose
    project/scope/path/policy or current RDC observation differs is a normal
    cache miss.
    """

    clean_project_id = _project_id(project_id)
    clean_scope = _sha256(scope_sha256, "scopeSha256")
    algorithm = _version_text(
        inventory_algorithm_version, "inventoryAlgorithmVersion"
    )
    policy = _version_text(matching_policy_version, "matchingPolicyVersion")
    capture = _normalised_capture_path(capture_path)
    path_digest = capture_path_sha256(capture)
    before_observation = capture_file_observation(capture)

    directory = _cache_directory(project_root, create=False)
    if directory is None:
        return None
    path = directory / (path_digest + ".json")
    payload = _read_regular_payload(path)
    if payload is None:
        return None
    document = parse_shader_inventory_cache(payload)
    after_observation = capture_file_observation(capture)
    if before_observation != after_observation:
        return None
    if (
        document["projectId"] != clean_project_id
        or document["scopeSha256"] != clean_scope
        or document["capturePathSha256"] != path_digest
        or document["inventoryAlgorithmVersion"] != algorithm
        or document["matchingPolicyVersion"] != policy
        or not _file_observations_match(
            document["fileObservation"], after_observation
        )
    ):
        return None
    # Defensive restatement at the API boundary: callers may only use the
    # persisted ResourceIds and anchors to route fresh target validation.
    document["routingOnly"] = True
    document["freshTargetValidationRequired"] = True
    document["crossCaptureResourceIdReuse"] = False
    return document


__all__ = [
    "DEFAULT_INVENTORY_ALGORITHM_VERSION",
    "DEFAULT_MATCHING_POLICY_VERSION",
    "MAX_RDC_SHADER_INVENTORY_CACHE_BYTES",
    "RDC_SHADER_INVENTORY_CACHE_DIRECTORY",
    "RDC_SHADER_INVENTORY_CACHE_SCHEMA",
    "RDC_SHADER_INVENTORY_CACHE_VERSION",
    "RdcShaderInventoryCacheError",
    "RdcShaderInventoryCacheMiss",
    "build_shader_inventory_cache_document",
    "capture_file_observation",
    "capture_path_sha256",
    "encode_shader_inventory_cache",
    "load_shader_inventory_cache",
    "parse_shader_inventory_cache",
    "shader_inventory_cache_path",
    "write_shader_inventory_cache",
]
