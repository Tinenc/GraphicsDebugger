"""Strict codec for ReplayLab project-association hints embedded in an RDC.

The document produced here is a disposable candidate index.  It deliberately
does not contain replay handles, absolute paths, compiled-Shader authority, or
captured CBuffer bytes.  A v3 realized receipt is historical, routing-only
evidence and never live replacement authority.  Callers must still revalidate
every Shader, workspace artifact, CBuffer layout, and live binding before
applying project knowledge.

This module only owns the versioned JSON payload.  Native code remains
responsible for placing the payload in an RDC section transactionally.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Any


RDC_PROJECT_INDEX_SECTION_NAME = "replaylab/cache/project-reuse-index"
RDC_PROJECT_INDEX_SCHEMA = "replaylab-rdc-project-reuse-index/v3"
RDC_PROJECT_INDEX_VERSION = 3
RDC_PROJECT_INDEX_V2_SCHEMA = "replaylab-rdc-project-reuse-index/v2"
RDC_PROJECT_INDEX_V2_VERSION = 2
RDC_PROJECT_INDEX_LEGACY_SCHEMA = "replaylab-rdc-project-reuse-index/v1"
RDC_PROJECT_INDEX_LEGACY_VERSION = 1

MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_PROJECT_BINDINGS = 32
MAX_SHADER_RECORDS = 8192
MAX_MATERIAL_RECORDS = 8192
MAX_EVENT_IDS_PER_SHADER = 4096
MAX_TOTAL_EVENT_IDS = 262144
MAX_MATERIAL_PARAMETERS = 256
MAX_TEXT_BYTES = 4096
MAX_ENTRY_POINT_BYTES = 512
MAX_STAGE_BYTES = 32
MAX_IDENTITY_TOKEN_BYTES = 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_STAGE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_IDENTITY_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._+-]{0,63}$")
_PROTOCOL_SHADER_ENCODINGS_BY_API = {
    "d3d11": frozenset(("dxbc",)),
    "d3d12": frozenset(("dxbc", "dxil")),
    "opengl": frozenset(("glsl", "spirv", "opengl-spirv")),
    "vulkan": frozenset(("spirv",)),
}
_PROTOCOL_SHADER_ENCODINGS = frozenset(
    encoding
    for encodings in _PROTOCOL_SHADER_ENCODINGS_BY_API.values()
    for encoding in encodings
)
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

_INDEX_FIELDS = frozenset(("schema", "schemaVersion", "bindings", "sealSha256"))
_V2_BINDING_FIELDS = frozenset(
    (
        "projectId",
        "workspaceId",
        "scopeSha256",
        "captureId",
        "catalogSha256",
        "knowledgeContentSha256",
        "matcherPolicySha256",
        "shaders",
        "materials",
    )
)
_BINDING_FIELDS = _V2_BINDING_FIELDS | frozenset(
    ("desiredProjectHead", "realizedApplySnapshot")
)
_DESIRED_PROJECT_HEAD_FIELDS = frozenset(
    (
        "activeKnowledgeRevision",
        "knowledgeContentSha256",
        "knowledgeIdsSha256",
        "artifactRefsSha256",
        "referencedHeadsSha256",
        "headSha256",
        "complete",
    )
)
_DESIRED_PROJECT_HEAD_SEMANTIC_FIELDS = frozenset(
    (
        "knowledgeContentSha256",
        "knowledgeIdsSha256",
        "artifactRefsSha256",
        "referencedHeadsSha256",
        "complete",
    )
)
_REALIZED_APPLY_SNAPSHOT_FIELDS = frozenset(
    ("desiredHeadSha256", "coverage", "receipts", "receiptsSha256")
)
_REALIZED_APPLY_RECEIPT_FIELDS = frozenset(
    (
        "stableKeySha256",
        "api",
        "encoding",
        "stage",
        "entryPoint",
        "originalResourceId",
        "originalRawSha256",
        "anchorEventId",
        "workspaceId",
        "candidateRevision",
        "candidateRawSha256",
        "physicalAbiSha256",
        "routingOnly",
        "freshTargetValidationRequired",
        "physicalApplyAuthority",
        "replacementHandleReusable",
        "receiptSha256",
    )
)
_REALIZED_APPLY_RECEIPT_FIELDS_WITH_ABI_REPORT = (
    _REALIZED_APPLY_RECEIPT_FIELDS | frozenset(("abiReportSha256",))
)
_REALIZED_APPLY_COVERAGE = frozenset(
    ("legacy-unknown", "none", "partial", "complete")
)
_SHADER_FIELDS = frozenset(
    (
        "identity",
        "knowledgeId",
        "managedWorkspace",
        "candidateRevision",
        "candidateRawSha256",
        "sourceSha256",
        "artifactRefs",
    )
)
_LEGACY_SHADER_FIELDS = _SHADER_FIELDS - frozenset(("artifactRefs",))
_SHADER_IDENTITY_FIELDS = frozenset(
    (
        "api",
        "encoding",
        "resourceId",
        "stage",
        "entryPoint",
        "rawSha256",
        "abiHash",
        "layoutHash",
        "stableKeyComplete",
        "stableKeySha256",
        "anchorEventId",
    )
)
_LEGACY_SHADER_IDENTITY_FIELDS = frozenset(
    (
        "resourceId",
        "stage",
        "entryPoint",
        "rawSha256",
        "codeSha256",
        "interfaceHash",
        "layoutHash",
        "interfaceComplete",
        "eventIds",
    )
)
_ARTIFACT_REF_FIELDS = frozenset(
    (
        "projectArtifactId",
        "revision",
        "compiledSha256",
        "compileConfigSha256",
        "representationsSha256",
        "aliasesSha256",
    )
)
_MANAGED_WORKSPACE_FIELDS = frozenset(("workspaceId", "path"))
_MATERIAL_FIELDS = frozenset(
    (
        "knowledgeId",
        "shaderResourceId",
        "stage",
        "blockIndex",
        "path",
        "layoutSha256",
        "eventId",
        "parameters",
    )
)
_PARAMETER_ID_FIELDS = frozenset(("parameterId",))
_PARAMETER_TYPED_FIELDS = frozenset(
    (
        "parameterId",
        "path",
        "baseType",
        "components",
        "arrayIndex",
        "byteOffset",
        "byteSize",
    )
)
_MATERIAL_BASE_TYPES = frozenset(("Float", "SInt", "UInt"))


class RdcProjectIndexError(ValueError):
    """Raised when an embedded RDC project index is malformed or stale."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RdcProjectIndexError("RDC project index is not canonical JSON") from exc


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise RdcProjectIndexError("RDC project index contains non-finite JSON: %s" % value)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RdcProjectIndexError(
                "RDC project index contains duplicate JSON key: %s" % key
            )
        result[key] = value
    return result


def _require_fields(value: Any, expected: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise RdcProjectIndexError("%s fields are invalid" % label)
    return value


def _hex32(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HEX32_RE.fullmatch(value) is None:
        raise RdcProjectIndexError("%s must be 32 lowercase hexadecimal characters" % label)
    return value


def _sha256(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise RdcProjectIndexError("%s must be a lowercase SHA-256" % label)
    return value


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RdcProjectIndexError("%s must be an integer" % label)
    if value < minimum or value > maximum:
        raise RdcProjectIndexError(
            "%s must be between %d and %d" % (label, minimum, maximum)
        )
    return value


def _text(
    value: Any,
    label: str,
    *,
    maximum_bytes: int = MAX_TEXT_BYTES,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise RdcProjectIndexError("%s must be text" % label)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RdcProjectIndexError("%s must be valid UTF-8 text" % label) from exc
    if len(encoded) > maximum_bytes:
        raise RdcProjectIndexError("%s exceeds its UTF-8 byte limit" % label)
    if any(ord(character) < 0x20 for character in value):
        raise RdcProjectIndexError("%s contains control characters" % label)
    return value


def _stage(value: Any, label: str) -> str:
    stage = _text(value, label, maximum_bytes=MAX_STAGE_BYTES)
    if _STAGE_RE.fullmatch(stage) is None or stage not in _RENDERDOC_STAGES:
        raise RdcProjectIndexError("%s is not a supported canonical RenderDoc stage" % label)
    return stage


def _identity_token(
    value: Any, label: str, *, allow_empty: bool = False
) -> str:
    token = _text(
        value,
        label,
        maximum_bytes=MAX_IDENTITY_TOKEN_BYTES,
        allow_empty=allow_empty,
    )
    if token and _IDENTITY_TOKEN_RE.fullmatch(token) is None:
        raise RdcProjectIndexError("%s is not a canonical identity token" % label)
    return token


def _project_relative_path(value: Any, label: str) -> str:
    text = _text(value, label)
    if "\\" in text or ":" in text:
        raise RdcProjectIndexError("%s must use a portable project-relative path" % label)
    windows = PureWindowsPath(text)
    posix = PurePosixPath(text)
    if windows.is_absolute() or windows.drive or posix.is_absolute():
        raise RdcProjectIndexError("%s must be project-relative" % label)
    if text.startswith("/") or text.endswith("/") or "//" in text:
        raise RdcProjectIndexError("%s is not a canonical project-relative path" % label)
    parts = posix.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise RdcProjectIndexError("%s escapes or aliases the project root" % label)
    canonical = "/".join(parts)
    if canonical != text:
        raise RdcProjectIndexError("%s is not a canonical project-relative path" % label)
    return canonical


def _semantic_path(value: Any, label: str) -> str:
    text = _text(value, label, maximum_bytes=2048)
    # These are reflected member/block paths, not filesystem paths.  Still
    # reject absolute-looking values so an index never carries an absolute path.
    if (
        text.startswith(("/", "\\"))
        or PureWindowsPath(text).is_absolute()
        or PureWindowsPath(text).drive
    ):
        raise RdcProjectIndexError("%s must not be an absolute path" % label)
    return text


def _stable_shader_key_fields(identity: Mapping[str, Any]) -> dict[str, str]:
    """Return the exact, capture-independent Shader key payload.

    ResourceId and anchorEventId are deliberately absent.  Keep this in lockstep
    with the replay bridge: UTF-8 canonical JSON over these seven named fields.
    """

    return {
        "api": str(identity["api"]),
        "encoding": str(identity["encoding"]),
        "rawSha256": str(identity["rawSha256"]),
        "stage": str(identity["stage"]),
        "entryPoint": str(identity["entryPoint"]),
        "abiHash": str(identity["abiHash"]),
        "layoutHash": str(identity["layoutHash"]),
    }


def _protocol_shader_api(
    value: str, label: str, *, allow_legacy: bool
) -> tuple[str, bool]:
    if not value:
        return "", False
    if value in _PROTOCOL_SHADER_ENCODINGS_BY_API:
        return value, False
    legacy_token = value.lower().rsplit(".", 1)[-1].replace("_", "")
    canonical = {
        "d3d11": "d3d11",
        "direct3d11": "d3d11",
        "d3d12": "d3d12",
        "direct3d12": "d3d12",
        "gl": "opengl",
        "opengl": "opengl",
        "openglcore": "opengl",
        "vulkan": "vulkan",
    }.get(legacy_token)
    if canonical is None:
        raise RdcProjectIndexError("%s is not a supported protocol API" % label)
    if not allow_legacy:
        raise RdcProjectIndexError(
            "%s must use a lowercase protocol API token" % label
        )
    return canonical, True


def _protocol_shader_encoding(
    value: str, label: str, *, allow_legacy: bool
) -> tuple[str, bool]:
    if not value:
        return "", False
    if value in _PROTOCOL_SHADER_ENCODINGS:
        return value, False
    legacy_token = (
        value.lower()
        .rsplit(".", 1)[-1]
        .replace("_", "")
        .replace("-", "")
    )
    canonical = {
        "dxbc": "dxbc",
        "directxbytecode": "dxbc",
        "dxil": "dxil",
        "directxil": "dxil",
        "dxilcontainer": "dxil",
        "spirv": "spirv",
        "spv": "spirv",
        "spirvshader": "spirv",
        "openglspirv": "opengl-spirv",
        "glsl": "glsl",
        "glslshader": "glsl",
        "openglglsl": "glsl",
    }.get(legacy_token)
    if canonical is None:
        raise RdcProjectIndexError(
            "%s is not a supported protocol Shader encoding" % label
        )
    if not allow_legacy:
        raise RdcProjectIndexError(
            "%s must use a lowercase protocol encoding token" % label
        )
    return canonical, True


def _normalise_v2_shader_identity(
    value: Any,
    label: str,
    *,
    allow_legacy_protocol_tokens: bool = False,
    preserve_legacy_protocol_tokens: bool = False,
) -> dict[str, Any]:
    source = _require_fields(value, _SHADER_IDENTITY_FIELDS, label)
    complete = source["stableKeyComplete"]
    if not isinstance(complete, bool):
        raise RdcProjectIndexError("%s.stableKeyComplete must be a boolean" % label)
    sealed_api = _identity_token(
        source["api"], "%s.api" % label, allow_empty=not complete
    )
    sealed_encoding = _identity_token(
        source["encoding"], "%s.encoding" % label, allow_empty=not complete
    )
    api, legacy_api = _protocol_shader_api(
        sealed_api,
        "%s.api" % label,
        allow_legacy=allow_legacy_protocol_tokens,
    )
    encoding, legacy_encoding = _protocol_shader_encoding(
        sealed_encoding,
        "%s.encoding" % label,
        allow_legacy=allow_legacy_protocol_tokens,
    )
    if bool(api) != bool(encoding):
        raise RdcProjectIndexError(
            "%s API/encoding identity pair is incomplete" % label
        )
    if encoding and encoding not in _PROTOCOL_SHADER_ENCODINGS_BY_API.get(api, ()):
        raise RdcProjectIndexError(
            "%s API/encoding identity pair is unsupported" % label
        )
    resource_id = _integer(
        source["resourceId"], "%s.resourceId" % label, minimum=1, maximum=(1 << 64) - 1
    )
    stage = _stage(source["stage"], "%s.stage" % label)
    entry_point = _text(
        source["entryPoint"],
        "%s.entryPoint" % label,
        maximum_bytes=MAX_ENTRY_POINT_BYTES,
        allow_empty=True,
    )
    raw_sha256 = _sha256(source["rawSha256"], "%s.rawSha256" % label)
    abi_hash = _sha256(source["abiHash"], "%s.abiHash" % label, allow_empty=True)
    layout_hash = _sha256(
        source["layoutHash"], "%s.layoutHash" % label, allow_empty=True
    )
    if complete and (not api or not encoding or not abi_hash or not layout_hash):
        raise RdcProjectIndexError(
            "%s is marked complete without every stable-key component" % label
        )
    anchor_event_id = source["anchorEventId"]
    if anchor_event_id is not None:
        anchor_event_id = _integer(
            anchor_event_id,
            "%s.anchorEventId" % label,
            minimum=1,
            maximum=(1 << 32) - 1,
        )
    identity = {
        "api": sealed_api,
        "encoding": sealed_encoding,
        "resourceId": resource_id,
        "stage": stage,
        "entryPoint": entry_point,
        "rawSha256": raw_sha256,
        "abiHash": abi_hash,
        "layoutHash": layout_hash,
        "stableKeyComplete": complete,
        "stableKeySha256": "",
        "anchorEventId": anchor_event_id,
    }
    expected_key = _canonical_sha256(_stable_shader_key_fields(identity))
    supplied_key = _sha256(
        source["stableKeySha256"], "%s.stableKeySha256" % label
    )
    if not hmac.compare_digest(supplied_key, expected_key):
        raise RdcProjectIndexError(
            "%s.stableKeySha256 does not match its stable-key fields" % label
        )
    identity["stableKeySha256"] = expected_key
    if (legacy_api or legacy_encoding) and not preserve_legacy_protocol_tokens:
        # A legacy digest and its enclosing v2 seal are verified over the exact
        # historical spellings before this branch is reached.  Canonicalising
        # changes the stable key, so the migrated association is deliberately
        # downgraded to a non-executable/stale hint.
        identity["api"] = api
        identity["encoding"] = encoding
        identity["stableKeyComplete"] = False
        identity["stableKeySha256"] = _canonical_sha256(
            _stable_shader_key_fields(identity)
        )
    return identity


def _normalise_legacy_shader_identity_document(
    value: Any, label: str
) -> dict[str, Any]:
    source = _require_fields(value, _LEGACY_SHADER_IDENTITY_FIELDS, label)
    resource_id = _integer(
        source["resourceId"], "%s.resourceId" % label, minimum=1, maximum=(1 << 64) - 1
    )
    stage = _stage(source["stage"], "%s.stage" % label)
    entry_point = _text(
        source["entryPoint"],
        "%s.entryPoint" % label,
        maximum_bytes=MAX_ENTRY_POINT_BYTES,
        allow_empty=True,
    )
    raw_sha256 = _sha256(source["rawSha256"], "%s.rawSha256" % label)
    code_sha256 = _sha256(
        source["codeSha256"], "%s.codeSha256" % label, allow_empty=True
    )
    interface_hash = _sha256(
        source["interfaceHash"], "%s.interfaceHash" % label, allow_empty=True
    )
    layout_hash = _sha256(
        source["layoutHash"], "%s.layoutHash" % label, allow_empty=True
    )
    interface_complete = source["interfaceComplete"]
    if not isinstance(interface_complete, bool):
        raise RdcProjectIndexError("%s.interfaceComplete must be a boolean" % label)
    if interface_complete and not interface_hash:
        raise RdcProjectIndexError(
            "%s.interfaceHash is required for a complete interface" % label
        )
    raw_events = source["eventIds"]
    if (
        not isinstance(raw_events, list)
        or not raw_events
        or len(raw_events) > MAX_EVENT_IDS_PER_SHADER
    ):
        raise RdcProjectIndexError("%s.eventIds is empty or oversized" % label)
    event_ids = [
        _integer(
            event_id,
            "%s.eventIds[%d]" % (label, index),
            minimum=1,
            maximum=(1 << 32) - 1,
        )
        for index, event_id in enumerate(raw_events)
    ]
    if len(set(event_ids)) != len(event_ids):
        raise RdcProjectIndexError("%s.eventIds contains duplicates" % label)
    event_ids.sort()
    return {
        "resourceId": resource_id,
        "stage": stage,
        "entryPoint": entry_point,
        "rawSha256": raw_sha256,
        "codeSha256": code_sha256,
        "interfaceHash": interface_hash,
        "layoutHash": layout_hash,
        "interfaceComplete": interface_complete,
        "eventIds": event_ids,
    }


def _normalise_legacy_shader_identity(value: Any, label: str) -> dict[str, Any]:
    """Validate one v1 identity and demote its EID list to one anchor.

    A v1 document did not record API, encoding, or a physical ABI authority.
    Its interface hash remains useful evidence but must never become an exact
    executable-reuse grant, hence ``stableKeyComplete`` is always false.
    """

    source = _normalise_legacy_shader_identity_document(value, label)
    identity: dict[str, Any] = {
        "api": "",
        "encoding": "",
        "resourceId": source["resourceId"],
        "stage": source["stage"],
        "entryPoint": source["entryPoint"],
        "rawSha256": source["rawSha256"],
        "abiHash": source["interfaceHash"],
        "layoutHash": source["layoutHash"],
        "stableKeyComplete": False,
        "stableKeySha256": "",
        "anchorEventId": min(source["eventIds"]),
    }
    identity["stableKeySha256"] = _canonical_sha256(
        _stable_shader_key_fields(identity)
    )
    return identity


def _normalise_shader_identity(
    value: Any,
    label: str,
    *,
    allow_legacy_protocol_tokens: bool = False,
    preserve_legacy_protocol_tokens: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RdcProjectIndexError("%s must be an object" % label)
    fields = frozenset(value)
    if fields == _SHADER_IDENTITY_FIELDS:
        return _normalise_v2_shader_identity(
            value,
            label,
            allow_legacy_protocol_tokens=allow_legacy_protocol_tokens,
            preserve_legacy_protocol_tokens=preserve_legacy_protocol_tokens,
        )
    if fields == _LEGACY_SHADER_IDENTITY_FIELDS:
        return _normalise_legacy_shader_identity(value, label)
    raise RdcProjectIndexError("%s fields are invalid" % label)


def _normalise_shader_record(
    value: Any,
    label: str,
    *,
    allow_legacy_protocol_tokens: bool = False,
    preserve_legacy_protocol_tokens: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RdcProjectIndexError("%s must be an object" % label)
    fields = frozenset(value)
    if fields not in (_SHADER_FIELDS, _LEGACY_SHADER_FIELDS):
        raise RdcProjectIndexError("%s fields are invalid" % label)
    source = value
    workspace = _require_fields(
        source["managedWorkspace"],
        _MANAGED_WORKSPACE_FIELDS,
        "%s.managedWorkspace" % label,
    )
    knowledge_id = _hex32(source["knowledgeId"], "%s.knowledgeId" % label)
    candidate_revision = _integer(
        source["candidateRevision"],
        "%s.candidateRevision" % label,
        minimum=1,
        maximum=(1 << 32) - 1,
    )
    candidate_raw_sha256 = _sha256(
        source["candidateRawSha256"], "%s.candidateRawSha256" % label
    )
    if fields == _SHADER_FIELDS:
        artifact_source = _require_fields(
            source["artifactRefs"],
            _ARTIFACT_REF_FIELDS,
            "%s.artifactRefs" % label,
        )
        artifact_refs = {
            "projectArtifactId": _hex32(
                artifact_source["projectArtifactId"],
                "%s.artifactRefs.projectArtifactId" % label,
            ),
            "revision": _integer(
                artifact_source["revision"],
                "%s.artifactRefs.revision" % label,
                minimum=1,
                maximum=(1 << 32) - 1,
            ),
            "compiledSha256": _sha256(
                artifact_source["compiledSha256"],
                "%s.artifactRefs.compiledSha256" % label,
            ),
            "compileConfigSha256": _sha256(
                artifact_source["compileConfigSha256"],
                "%s.artifactRefs.compileConfigSha256" % label,
                allow_empty=True,
            ),
            "representationsSha256": _sha256(
                artifact_source["representationsSha256"],
                "%s.artifactRefs.representationsSha256" % label,
                allow_empty=True,
            ),
            "aliasesSha256": _sha256(
                artifact_source["aliasesSha256"],
                "%s.artifactRefs.aliasesSha256" % label,
                allow_empty=True,
            ),
        }
        if (
            artifact_refs["projectArtifactId"] != knowledge_id
            or artifact_refs["revision"] != candidate_revision
            or artifact_refs["compiledSha256"] != candidate_raw_sha256
        ):
            raise RdcProjectIndexError(
                "%s.artifactRefs conflicts with the bound project artifact" % label
            )
    else:
        artifact_refs = {
            "projectArtifactId": knowledge_id,
            "revision": candidate_revision,
            "compiledSha256": candidate_raw_sha256,
            "compileConfigSha256": "",
            "representationsSha256": "",
            "aliasesSha256": "",
        }
    return {
        "identity": _normalise_shader_identity(
            source["identity"],
            "%s.identity" % label,
            allow_legacy_protocol_tokens=allow_legacy_protocol_tokens,
            preserve_legacy_protocol_tokens=preserve_legacy_protocol_tokens,
        ),
        "knowledgeId": knowledge_id,
        "managedWorkspace": {
            "workspaceId": _hex32(
                workspace["workspaceId"], "%s.managedWorkspace.workspaceId" % label
            ),
            "path": _project_relative_path(
                workspace["path"], "%s.managedWorkspace.path" % label
            ),
        },
        "candidateRevision": candidate_revision,
        "candidateRawSha256": candidate_raw_sha256,
        "sourceSha256": _sha256(source["sourceSha256"], "%s.sourceSha256" % label),
        "artifactRefs": artifact_refs,
    }


def _normalise_legacy_shader_record_document(
    value: Any, label: str
) -> dict[str, Any]:
    source = _require_fields(value, _LEGACY_SHADER_FIELDS, label)
    workspace = _require_fields(
        source["managedWorkspace"],
        _MANAGED_WORKSPACE_FIELDS,
        "%s.managedWorkspace" % label,
    )
    return {
        "identity": _normalise_legacy_shader_identity_document(
            source["identity"], "%s.identity" % label
        ),
        "knowledgeId": _hex32(source["knowledgeId"], "%s.knowledgeId" % label),
        "managedWorkspace": {
            "workspaceId": _hex32(
                workspace["workspaceId"], "%s.managedWorkspace.workspaceId" % label
            ),
            "path": _project_relative_path(
                workspace["path"], "%s.managedWorkspace.path" % label
            ),
        },
        "candidateRevision": _integer(
            source["candidateRevision"],
            "%s.candidateRevision" % label,
            minimum=1,
            maximum=(1 << 32) - 1,
        ),
        "candidateRawSha256": _sha256(
            source["candidateRawSha256"], "%s.candidateRawSha256" % label
        ),
        "sourceSha256": _sha256(source["sourceSha256"], "%s.sourceSha256" % label),
    }


def _normalise_parameter(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RdcProjectIndexError("%s must be an object" % label)
    fields = frozenset(value)
    if fields == _PARAMETER_ID_FIELDS:
        return {"parameterId": _hex32(value["parameterId"], "%s.parameterId" % label)}
    if fields != _PARAMETER_TYPED_FIELDS:
        raise RdcProjectIndexError("%s fields are invalid" % label)
    base_type = value["baseType"]
    if not isinstance(base_type, str) or base_type not in _MATERIAL_BASE_TYPES:
        raise RdcProjectIndexError("%s.baseType is unsupported" % label)
    components = _integer(
        value["components"], "%s.components" % label, minimum=1, maximum=4
    )
    byte_size = _integer(
        value["byteSize"], "%s.byteSize" % label, minimum=4, maximum=16
    )
    if byte_size != components * 4:
        raise RdcProjectIndexError("%s.byteSize does not match its 32-bit components" % label)
    return {
        "parameterId": _hex32(value["parameterId"], "%s.parameterId" % label),
        "path": _semantic_path(value["path"], "%s.path" % label),
        "baseType": base_type,
        "components": components,
        "arrayIndex": _integer(
            value["arrayIndex"], "%s.arrayIndex" % label, minimum=0, maximum=(1 << 31) - 1
        ),
        "byteOffset": _integer(
            value["byteOffset"], "%s.byteOffset" % label, minimum=0, maximum=(1 << 32) - 1
        ),
        "byteSize": byte_size,
    }


def _normalise_material_record(value: Any, label: str) -> dict[str, Any]:
    source = _require_fields(value, _MATERIAL_FIELDS, label)
    raw_parameters = source["parameters"]
    if not isinstance(raw_parameters, list) or len(raw_parameters) > MAX_MATERIAL_PARAMETERS:
        raise RdcProjectIndexError("%s.parameters is invalid or oversized" % label)
    parameters = [
        _normalise_parameter(parameter, "%s.parameters[%d]" % (label, index))
        for index, parameter in enumerate(raw_parameters)
    ]
    parameter_ids = [parameter["parameterId"] for parameter in parameters]
    if len(set(parameter_ids)) != len(parameter_ids):
        raise RdcProjectIndexError("%s.parameters contains duplicate IDs" % label)
    parameters.sort(key=lambda parameter: parameter["parameterId"])
    return {
        "knowledgeId": _hex32(source["knowledgeId"], "%s.knowledgeId" % label),
        "shaderResourceId": _integer(
            source["shaderResourceId"],
            "%s.shaderResourceId" % label,
            minimum=1,
            maximum=(1 << 64) - 1,
        ),
        "stage": _stage(source["stage"], "%s.stage" % label),
        "blockIndex": _integer(
            source["blockIndex"], "%s.blockIndex" % label, minimum=0, maximum=65535
        ),
        "path": _semantic_path(source["path"], "%s.path" % label),
        "layoutSha256": _sha256(source["layoutSha256"], "%s.layoutSha256" % label),
        "eventId": _integer(
            source["eventId"], "%s.eventId" % label, minimum=1, maximum=(1 << 32) - 1
        ),
        "parameters": parameters,
    }


def _shader_sort_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    identity = value["identity"]
    workspace = value["managedWorkspace"]
    return (
        identity["resourceId"],
        identity["stage"],
        identity["entryPoint"],
        identity["rawSha256"],
        value["knowledgeId"],
        workspace["workspaceId"],
        workspace["path"],
        value["candidateRevision"],
    )


def _shader_association_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    identity = value["identity"]
    return (
        identity["resourceId"],
        identity["stage"],
        identity["entryPoint"],
        identity["rawSha256"],
    )


def _material_sort_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        value["shaderResourceId"],
        value["stage"],
        value["eventId"],
        value["blockIndex"],
        value["path"],
        value["knowledgeId"],
    )


def _material_association_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        value["shaderResourceId"],
        value["stage"],
        value["eventId"],
        value["blockIndex"],
        value["path"],
    )


def _normalise_v2_binding(
    value: Any,
    label: str = "binding",
    *,
    allow_legacy_protocol_tokens: bool = False,
    preserve_legacy_protocol_tokens: bool = False,
) -> dict[str, Any]:
    source = _require_fields(value, _V2_BINDING_FIELDS, label)
    raw_shaders = source["shaders"]
    raw_materials = source["materials"]
    if (
        not isinstance(raw_shaders, list)
        or len(raw_shaders) > MAX_SHADER_RECORDS
    ):
        raise RdcProjectIndexError("%s.shaders is invalid or oversized" % label)
    if not isinstance(raw_materials, list) or len(raw_materials) > MAX_MATERIAL_RECORDS:
        raise RdcProjectIndexError("%s.materials is invalid or oversized" % label)

    shaders = [
        _normalise_shader_record(
            shader,
            "%s.shaders[%d]" % (label, index),
            allow_legacy_protocol_tokens=allow_legacy_protocol_tokens,
            preserve_legacy_protocol_tokens=preserve_legacy_protocol_tokens,
        )
        for index, shader in enumerate(raw_shaders)
    ]
    shader_keys = [_shader_association_key(shader) for shader in shaders]
    if len(set(shader_keys)) != len(shader_keys):
        raise RdcProjectIndexError("%s.shaders contains duplicate associations" % label)
    shaders.sort(key=_shader_sort_key)

    materials = [
        _normalise_material_record(material, "%s.materials[%d]" % (label, index))
        for index, material in enumerate(raw_materials)
    ]
    material_keys = [_material_association_key(material) for material in materials]
    if len(set(material_keys)) != len(material_keys):
        raise RdcProjectIndexError("%s.materials contains duplicate associations" % label)
    shader_resources = {
        (shader["identity"]["resourceId"], shader["identity"]["stage"])
        for shader in shaders
    }
    for material in materials:
        if (material["shaderResourceId"], material["stage"]) not in shader_resources:
            raise RdcProjectIndexError(
                "%s.materials references a Shader/stage absent from the binding" % label
            )
    materials.sort(key=_material_sort_key)

    return {
        "projectId": _hex32(source["projectId"], "%s.projectId" % label),
        "workspaceId": _hex32(source["workspaceId"], "%s.workspaceId" % label),
        "scopeSha256": _sha256(source["scopeSha256"], "%s.scopeSha256" % label),
        "captureId": _sha256(source["captureId"], "%s.captureId" % label),
        "catalogSha256": _sha256(source["catalogSha256"], "%s.catalogSha256" % label),
        "knowledgeContentSha256": _sha256(
            source["knowledgeContentSha256"], "%s.knowledgeContentSha256" % label
        ),
        "matcherPolicySha256": _sha256(
            source["matcherPolicySha256"], "%s.matcherPolicySha256" % label
        ),
        "shaders": shaders,
        "materials": materials,
    }


def _referenced_heads_document(
    shaders: Sequence[Mapping[str, Any]], materials: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Project immutable heads without capture-local routing identifiers."""

    return {
        "schema": "replaylab-rdc-project-referenced-heads/v1",
        "shaders": [
            {
                "stableKeySha256": shader["identity"]["stableKeySha256"],
                "knowledgeId": shader["knowledgeId"],
                "managedWorkspace": copy.deepcopy(shader["managedWorkspace"]),
                "candidateRevision": shader["candidateRevision"],
                "candidateRawSha256": shader["candidateRawSha256"],
                "sourceSha256": shader["sourceSha256"],
                "artifactRefs": copy.deepcopy(shader["artifactRefs"]),
            }
            for shader in shaders
        ],
        "materials": [
            {
                key: copy.deepcopy(material[key])
                for key in (
                    "knowledgeId",
                    "stage",
                    "blockIndex",
                    "path",
                    "layoutSha256",
                    "parameters",
                )
            }
            for material in materials
        ],
    }


def referenced_heads_sha256(
    shaders: Sequence[Mapping[str, Any]], materials: Sequence[Mapping[str, Any]]
) -> str:
    """Hash the exact project heads referenced by desired Shader/material records.

    ResourceId, EID, and anchor routing are intentionally excluded.  The hash is
    therefore stable when the same project heads are rebound to another capture.
    """

    if (
        not isinstance(shaders, Sequence)
        or isinstance(shaders, (str, bytes, bytearray))
        or len(shaders) > MAX_SHADER_RECORDS
    ):
        raise RdcProjectIndexError("referenced Shader heads are invalid or oversized")
    if (
        not isinstance(materials, Sequence)
        or isinstance(materials, (str, bytes, bytearray))
        or len(materials) > MAX_MATERIAL_RECORDS
    ):
        raise RdcProjectIndexError("referenced material heads are invalid or oversized")
    clean_shaders = [
        _normalise_shader_record(shader, "shaders[%d]" % index)
        for index, shader in enumerate(shaders)
    ]
    shader_keys = [_shader_association_key(shader) for shader in clean_shaders]
    if len(set(shader_keys)) != len(shader_keys):
        raise RdcProjectIndexError("shaders contains duplicate associations")
    clean_shaders.sort(key=_shader_sort_key)
    clean_materials = [
        _normalise_material_record(material, "materials[%d]" % index)
        for index, material in enumerate(materials)
    ]
    material_keys = [
        _material_association_key(material) for material in clean_materials
    ]
    if len(set(material_keys)) != len(material_keys):
        raise RdcProjectIndexError("materials contains duplicate associations")
    shader_resources = {
        (shader["identity"]["resourceId"], shader["identity"]["stage"])
        for shader in clean_shaders
    }
    for material in clean_materials:
        if (material["shaderResourceId"], material["stage"]) not in shader_resources:
            raise RdcProjectIndexError(
                "materials references a Shader/stage absent from the binding"
            )
    clean_materials.sort(key=_material_sort_key)
    return _canonical_sha256(
        _referenced_heads_document(clean_shaders, clean_materials)
    )


def _desired_project_head_semantic_fields(
    value: Mapping[str, Any]
) -> dict[str, Any]:
    # activeKnowledgeRevision is intentionally excluded.  It is an audit/order
    # diagnostic covered by the outer document seal, not semantic currentness;
    # unrelated project activations must not invalidate this capture's head.
    return {
        key: copy.deepcopy(value[key])
        for key in (
            "knowledgeContentSha256",
            "knowledgeIdsSha256",
            "artifactRefsSha256",
            "referencedHeadsSha256",
            "complete",
        )
    }


def _normalise_desired_project_head(value: Any, label: str) -> dict[str, Any]:
    source = _require_fields(value, _DESIRED_PROJECT_HEAD_FIELDS, label)
    complete = source["complete"]
    if not isinstance(complete, bool):
        raise RdcProjectIndexError("%s.complete must be a boolean" % label)
    result = {
        "activeKnowledgeRevision": _integer(
            source["activeKnowledgeRevision"],
            "%s.activeKnowledgeRevision" % label,
            minimum=0,
            maximum=(1 << 63) - 1,
        ),
        "knowledgeContentSha256": _sha256(
            source["knowledgeContentSha256"],
            "%s.knowledgeContentSha256" % label,
        ),
        "knowledgeIdsSha256": _sha256(
            source["knowledgeIdsSha256"],
            "%s.knowledgeIdsSha256" % label,
            allow_empty=not complete,
        ),
        "artifactRefsSha256": _sha256(
            source["artifactRefsSha256"],
            "%s.artifactRefsSha256" % label,
            allow_empty=not complete,
        ),
        "referencedHeadsSha256": _sha256(
            source["referencedHeadsSha256"],
            "%s.referencedHeadsSha256" % label,
            allow_empty=not complete,
        ),
        "headSha256": "",
        "complete": complete,
    }
    if not complete and (
        result["activeKnowledgeRevision"] != 0
        or result["knowledgeIdsSha256"]
        or result["artifactRefsSha256"]
        or result["referencedHeadsSha256"]
    ):
        raise RdcProjectIndexError(
            "%s incomplete legacy head must not invent unavailable project state"
            % label
        )
    expected = _canonical_sha256(_desired_project_head_semantic_fields(result))
    supplied = _sha256(source["headSha256"], "%s.headSha256" % label)
    if not hmac.compare_digest(supplied, expected):
        raise RdcProjectIndexError(
            "%s.headSha256 does not match its semantic project head" % label
        )
    result["headSha256"] = expected
    return result


def build_desired_project_head(
    *,
    active_knowledge_revision: int,
    knowledge_content_sha256: str,
    knowledge_ids_sha256: str,
    artifact_refs_sha256: str,
    referenced_heads_sha256: str,
    complete: bool = True,
) -> dict[str, Any]:
    """Construct one content-bound desired project head.

    ``active_knowledge_revision`` remains an outer-sealed audit diagnostic and
    is deliberately absent from ``headSha256``.
    """

    value: dict[str, Any] = {
        "activeKnowledgeRevision": active_knowledge_revision,
        "knowledgeContentSha256": knowledge_content_sha256,
        "knowledgeIdsSha256": knowledge_ids_sha256,
        "artifactRefsSha256": artifact_refs_sha256,
        "referencedHeadsSha256": referenced_heads_sha256,
        "headSha256": "",
        "complete": complete,
    }
    value["headSha256"] = _canonical_sha256(
        _desired_project_head_semantic_fields(value)
    )
    return _normalise_desired_project_head(value, "desiredProjectHead")


def _normalise_realized_apply_receipt(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RdcProjectIndexError("%s must be an object" % label)
    fields = frozenset(value)
    if fields not in (
        _REALIZED_APPLY_RECEIPT_FIELDS,
        _REALIZED_APPLY_RECEIPT_FIELDS_WITH_ABI_REPORT,
    ):
        raise RdcProjectIndexError("%s fields are invalid" % label)
    source = value
    api, unused_legacy_api = _protocol_shader_api(
        _identity_token(source["api"], "%s.api" % label),
        "%s.api" % label,
        allow_legacy=False,
    )
    encoding, unused_legacy_encoding = _protocol_shader_encoding(
        _identity_token(source["encoding"], "%s.encoding" % label),
        "%s.encoding" % label,
        allow_legacy=False,
    )
    if encoding not in _PROTOCOL_SHADER_ENCODINGS_BY_API.get(api, ()):
        raise RdcProjectIndexError(
            "%s API/encoding identity pair is unsupported" % label
        )
    for field, expected in (
        ("routingOnly", True),
        ("freshTargetValidationRequired", True),
        ("physicalApplyAuthority", False),
        ("replacementHandleReusable", False),
    ):
        if source[field] is not expected:
            raise RdcProjectIndexError("%s.%s has an unsafe value" % (label, field))
    result: dict[str, Any] = {
        "stableKeySha256": _sha256(
            source["stableKeySha256"], "%s.stableKeySha256" % label
        ),
        "api": api,
        "encoding": encoding,
        "stage": _stage(source["stage"], "%s.stage" % label),
        "entryPoint": _text(
            source["entryPoint"],
            "%s.entryPoint" % label,
            maximum_bytes=MAX_ENTRY_POINT_BYTES,
            allow_empty=True,
        ),
        "originalResourceId": _integer(
            source["originalResourceId"],
            "%s.originalResourceId" % label,
            minimum=1,
            maximum=(1 << 64) - 1,
        ),
        "originalRawSha256": _sha256(
            source["originalRawSha256"], "%s.originalRawSha256" % label
        ),
        "anchorEventId": _integer(
            source["anchorEventId"],
            "%s.anchorEventId" % label,
            minimum=1,
            maximum=(1 << 32) - 1,
        ),
        "workspaceId": _hex32(source["workspaceId"], "%s.workspaceId" % label),
        "candidateRevision": _integer(
            source["candidateRevision"],
            "%s.candidateRevision" % label,
            minimum=1,
            maximum=(1 << 32) - 1,
        ),
        "candidateRawSha256": _sha256(
            source["candidateRawSha256"], "%s.candidateRawSha256" % label
        ),
        "physicalAbiSha256": _sha256(
            source["physicalAbiSha256"], "%s.physicalAbiSha256" % label
        ),
        "routingOnly": True,
        "freshTargetValidationRequired": True,
        "physicalApplyAuthority": False,
        "replacementHandleReusable": False,
    }
    if "abiReportSha256" in source:
        result["abiReportSha256"] = _sha256(
            source["abiReportSha256"], "%s.abiReportSha256" % label
        )
    expected = _canonical_sha256(result)
    supplied = _sha256(source["receiptSha256"], "%s.receiptSha256" % label)
    if not hmac.compare_digest(supplied, expected):
        raise RdcProjectIndexError(
            "%s.receiptSha256 does not match its realized apply fields" % label
        )
    result["receiptSha256"] = expected
    return result


def build_realized_apply_receipt(
    identity: Mapping[str, Any],
    workspace_id: str,
    candidate_revision: int,
    candidate_raw_sha256: str,
    physical_abi_sha256: str,
    *,
    abi_report_sha256: str | None = None,
) -> dict[str, Any]:
    """Construct a non-authoritative receipt from stable and capture routing data."""

    clean_identity = _normalise_shader_identity(identity, "identity")
    if (
        clean_identity["stableKeyComplete"] is not True
        or clean_identity["anchorEventId"] is None
    ):
        raise RdcProjectIndexError(
            "realized apply receipt requires a complete identity and one anchor EID"
        )
    if physical_abi_sha256 != clean_identity["abiHash"]:
        raise RdcProjectIndexError(
            "physicalAbiSha256 conflicts with the stable Shader identity"
        )
    value: dict[str, Any] = {
        "stableKeySha256": clean_identity["stableKeySha256"],
        "api": clean_identity["api"],
        "encoding": clean_identity["encoding"],
        "stage": clean_identity["stage"],
        "entryPoint": clean_identity["entryPoint"],
        "originalResourceId": clean_identity["resourceId"],
        "originalRawSha256": clean_identity["rawSha256"],
        "anchorEventId": clean_identity["anchorEventId"],
        "workspaceId": workspace_id,
        "candidateRevision": candidate_revision,
        "candidateRawSha256": candidate_raw_sha256,
        "physicalAbiSha256": physical_abi_sha256,
        "routingOnly": True,
        "freshTargetValidationRequired": True,
        "physicalApplyAuthority": False,
        "replacementHandleReusable": False,
    }
    if abi_report_sha256 is not None:
        value["abiReportSha256"] = abi_report_sha256
    value["receiptSha256"] = _canonical_sha256(value)
    return _normalise_realized_apply_receipt(value, "receipt")


def _receipt_sort_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        value["originalResourceId"],
        value["stage"],
        value["entryPoint"],
        value["stableKeySha256"],
        value["workspaceId"],
        value["candidateRevision"],
    )


def _normalise_realized_apply_snapshot(value: Any, label: str) -> dict[str, Any]:
    source = _require_fields(value, _REALIZED_APPLY_SNAPSHOT_FIELDS, label)
    coverage = source["coverage"]
    if not isinstance(coverage, str) or coverage not in _REALIZED_APPLY_COVERAGE:
        raise RdcProjectIndexError("%s.coverage is unsupported" % label)
    raw_receipts = source["receipts"]
    if not isinstance(raw_receipts, list) or len(raw_receipts) > MAX_SHADER_RECORDS:
        raise RdcProjectIndexError("%s.receipts is invalid or oversized" % label)
    receipts = [
        _normalise_realized_apply_receipt(
            receipt, "%s.receipts[%d]" % (label, index)
        )
        for index, receipt in enumerate(raw_receipts)
    ]
    resource_ids = [receipt["originalResourceId"] for receipt in receipts]
    if len(set(resource_ids)) != len(resource_ids):
        raise RdcProjectIndexError(
            "%s.receipts contains duplicate physical Shader resources" % label
        )
    receipts.sort(key=_receipt_sort_key)
    supplied = _sha256(source["receiptsSha256"], "%s.receiptsSha256" % label)
    expected = _canonical_sha256(receipts)
    if not hmac.compare_digest(supplied, expected):
        raise RdcProjectIndexError(
            "%s.receiptsSha256 does not match its receipts" % label
        )
    if coverage in ("legacy-unknown", "none") and receipts:
        raise RdcProjectIndexError("%s.coverage conflicts with its receipts" % label)
    if coverage == "partial" and not receipts:
        raise RdcProjectIndexError("%s.partial coverage requires a receipt" % label)
    return {
        "desiredHeadSha256": _sha256(
            source["desiredHeadSha256"], "%s.desiredHeadSha256" % label
        ),
        "coverage": coverage,
        "receipts": receipts,
        "receiptsSha256": expected,
    }


def build_realized_apply_snapshot(
    desired_project_head: Mapping[str, Any],
    receipts: Sequence[Mapping[str, Any]],
    *,
    coverage: str,
) -> dict[str, Any]:
    """Construct a receipt set bound to one semantic desired project head."""

    head = _normalise_desired_project_head(
        desired_project_head, "desiredProjectHead"
    )
    if not isinstance(receipts, Sequence) or isinstance(
        receipts, (str, bytes, bytearray)
    ):
        raise RdcProjectIndexError("receipts must be a sequence")
    clean_receipts = [
        _normalise_realized_apply_receipt(receipt, "receipts[%d]" % index)
        for index, receipt in enumerate(receipts)
    ]
    clean_receipts.sort(key=_receipt_sort_key)
    value = {
        "desiredHeadSha256": head["headSha256"],
        "coverage": coverage,
        "receipts": clean_receipts,
        "receiptsSha256": _canonical_sha256(clean_receipts),
    }
    return _normalise_realized_apply_snapshot(value, "realizedApplySnapshot")


def _receipt_matches_shader(
    receipt: Mapping[str, Any], shader: Mapping[str, Any]
) -> bool:
    identity = shader["identity"]
    return bool(
        identity["stableKeyComplete"] is True
        and identity["anchorEventId"] is not None
        and receipt["stableKeySha256"] == identity["stableKeySha256"]
        and receipt["api"] == identity["api"]
        and receipt["encoding"] == identity["encoding"]
        and receipt["stage"] == identity["stage"]
        and receipt["entryPoint"] == identity["entryPoint"]
        and receipt["originalResourceId"] == identity["resourceId"]
        and receipt["originalRawSha256"] == identity["rawSha256"]
        and receipt["anchorEventId"] == identity["anchorEventId"]
        and receipt["workspaceId"] == shader["managedWorkspace"]["workspaceId"]
        and receipt["candidateRevision"] == shader["candidateRevision"]
        and receipt["candidateRawSha256"] == shader["candidateRawSha256"]
        and receipt["physicalAbiSha256"] == identity["abiHash"]
    )


def _normalise_binding(value: Any, label: str = "binding") -> dict[str, Any]:
    source = _require_fields(value, _BINDING_FIELDS, label)
    desired = _normalise_v2_binding(
        {key: source[key] for key in _V2_BINDING_FIELDS}, label
    )
    head = _normalise_desired_project_head(
        source["desiredProjectHead"], "%s.desiredProjectHead" % label
    )
    snapshot = _normalise_realized_apply_snapshot(
        source["realizedApplySnapshot"], "%s.realizedApplySnapshot" % label
    )
    if head["knowledgeContentSha256"] != desired["knowledgeContentSha256"]:
        raise RdcProjectIndexError(
            "%s desired project head conflicts with knowledgeContentSha256" % label
        )
    if head["complete"] and head["referencedHeadsSha256"] != referenced_heads_sha256(
        desired["shaders"], desired["materials"]
    ):
        raise RdcProjectIndexError(
            "%s desired project head conflicts with its referenced heads" % label
        )
    if snapshot["desiredHeadSha256"] != head["headSha256"]:
        raise RdcProjectIndexError(
            "%s realized snapshot targets another desired project head" % label
        )
    coverage = snapshot["coverage"]
    receipts = snapshot["receipts"]
    if not head["complete"]:
        if coverage != "legacy-unknown" or receipts:
            raise RdcProjectIndexError(
                "%s incomplete desired head can only carry legacy-unknown coverage"
                % label
            )
    elif coverage == "legacy-unknown":
        raise RdcProjectIndexError(
            "%s complete desired head cannot carry legacy-unknown coverage" % label
        )
    for receipt in receipts:
        if sum(
            1 for shader in desired["shaders"] if _receipt_matches_shader(receipt, shader)
        ) != 1:
            raise RdcProjectIndexError(
                "%s realized receipt does not match one exact desired Shader" % label
            )
    if coverage == "none" and receipts:
        raise RdcProjectIndexError("%s none coverage must have no receipts" % label)
    if coverage == "partial" and not (0 < len(receipts) < len(desired["shaders"])):
        raise RdcProjectIndexError(
            "%s partial coverage does not describe a strict Shader subset" % label
        )
    if coverage == "complete" and len(receipts) != len(desired["shaders"]):
        raise RdcProjectIndexError(
            "%s complete coverage requires one receipt per desired Shader" % label
        )
    return {
        **desired,
        "desiredProjectHead": head,
        "realizedApplySnapshot": snapshot,
    }


def build_project_binding(
    desired_binding: Mapping[str, Any],
    desired_project_head: Mapping[str, Any],
    realized_apply_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine a v2-shaped desired association with strict v3 head/receipts."""

    desired = _normalise_v2_binding(desired_binding)
    return _normalise_binding(
        {
            **desired,
            "desiredProjectHead": copy.deepcopy(dict(desired_project_head)),
            "realizedApplySnapshot": copy.deepcopy(dict(realized_apply_snapshot)),
        }
    )


def normalise_project_binding(binding: Mapping[str, Any]) -> dict[str, Any]:
    """Return one detached, strict canonical v3 project binding."""

    return copy.deepcopy(_normalise_binding(binding))


def _migrated_incomplete_desired_head(knowledge_content_sha256: str) -> dict[str, Any]:
    return build_desired_project_head(
        active_knowledge_revision=0,
        knowledge_content_sha256=knowledge_content_sha256,
        knowledge_ids_sha256="",
        artifact_refs_sha256="",
        referenced_heads_sha256="",
        complete=False,
    )


def _migrate_v2_binding(binding: Mapping[str, Any]) -> dict[str, Any]:
    desired = _normalise_v2_binding(binding)
    head = _migrated_incomplete_desired_head(desired["knowledgeContentSha256"])
    snapshot = build_realized_apply_snapshot(
        head, [], coverage="legacy-unknown"
    )
    return build_project_binding(desired, head, snapshot)


def _normalise_legacy_binding_document(
    value: Any, label: str = "binding"
) -> dict[str, Any]:
    """Canonicalise one v1 binding without changing its sealed shape."""

    source = _require_fields(value, _V2_BINDING_FIELDS, label)
    raw_shaders = source["shaders"]
    raw_materials = source["materials"]
    if not isinstance(raw_shaders, list) or len(raw_shaders) > MAX_SHADER_RECORDS:
        raise RdcProjectIndexError("%s.shaders is invalid or oversized" % label)
    if not isinstance(raw_materials, list) or len(raw_materials) > MAX_MATERIAL_RECORDS:
        raise RdcProjectIndexError("%s.materials is invalid or oversized" % label)

    shaders = [
        _normalise_legacy_shader_record_document(
            shader, "%s.shaders[%d]" % (label, index)
        )
        for index, shader in enumerate(raw_shaders)
    ]
    shader_keys = [_shader_association_key(shader) for shader in shaders]
    if len(set(shader_keys)) != len(shader_keys):
        raise RdcProjectIndexError("%s.shaders contains duplicate associations" % label)
    shaders.sort(key=_shader_sort_key)

    materials = [
        _normalise_material_record(material, "%s.materials[%d]" % (label, index))
        for index, material in enumerate(raw_materials)
    ]
    material_keys = [_material_association_key(material) for material in materials]
    if len(set(material_keys)) != len(material_keys):
        raise RdcProjectIndexError("%s.materials contains duplicate associations" % label)
    shader_resources = {
        (shader["identity"]["resourceId"], shader["identity"]["stage"])
        for shader in shaders
    }
    for material in materials:
        if (material["shaderResourceId"], material["stage"]) not in shader_resources:
            raise RdcProjectIndexError(
                "%s.materials references a Shader/stage absent from the binding" % label
            )
    materials.sort(key=_material_sort_key)

    return {
        "projectId": _hex32(source["projectId"], "%s.projectId" % label),
        "workspaceId": _hex32(source["workspaceId"], "%s.workspaceId" % label),
        "scopeSha256": _sha256(source["scopeSha256"], "%s.scopeSha256" % label),
        "captureId": _sha256(source["captureId"], "%s.captureId" % label),
        "catalogSha256": _sha256(source["catalogSha256"], "%s.catalogSha256" % label),
        "knowledgeContentSha256": _sha256(
            source["knowledgeContentSha256"], "%s.knowledgeContentSha256" % label
        ),
        "matcherPolicySha256": _sha256(
            source["matcherPolicySha256"], "%s.matcherPolicySha256" % label
        ),
        "shaders": shaders,
        "materials": materials,
    }


def _binding_key(value: Mapping[str, Any]) -> tuple[str, str]:
    return str(value["projectId"]), str(value["workspaceId"])


def _seal_unsigned(bindings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    unsigned = {
        "schema": RDC_PROJECT_INDEX_SCHEMA,
        "schemaVersion": RDC_PROJECT_INDEX_VERSION,
        "bindings": copy.deepcopy(list(bindings)),
    }
    return {**unsigned, "sealSha256": _canonical_sha256(unsigned)}


def _normalise_index_header(value: Any) -> Mapping[str, Any]:
    source = _require_fields(value, _INDEX_FIELDS, "RDC project index")
    if (
        not isinstance(source["schema"], str)
        or isinstance(source["schemaVersion"], bool)
        or not isinstance(source["schemaVersion"], int)
    ):
        raise RdcProjectIndexError("RDC project index schema/version is unsupported")
    return source


def _normalise_binding_collection(
    bindings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    binding_keys = [_binding_key(binding) for binding in bindings]
    if len(set(binding_keys)) != len(binding_keys):
        raise RdcProjectIndexError("RDC project index contains duplicate project bindings")
    bindings.sort(key=_binding_key)
    shader_count = sum(len(binding["shaders"]) for binding in bindings)
    material_count = sum(len(binding["materials"]) for binding in bindings)
    if shader_count > MAX_SHADER_RECORDS:
        raise RdcProjectIndexError("RDC project index has too many Shader records")
    if material_count > MAX_MATERIAL_RECORDS:
        raise RdcProjectIndexError("RDC project index has too many material records")
    return bindings


def _normalise_v3_index(source: Mapping[str, Any]) -> dict[str, Any]:
    if (
        source["schema"] != RDC_PROJECT_INDEX_SCHEMA
        or source["schemaVersion"] != RDC_PROJECT_INDEX_VERSION
    ):
        raise RdcProjectIndexError("RDC project index schema/version is unsupported")
    raw_bindings = source["bindings"]
    if not isinstance(raw_bindings, list) or len(raw_bindings) > MAX_PROJECT_BINDINGS:
        raise RdcProjectIndexError("RDC project index bindings are invalid or oversized")
    bindings = _normalise_binding_collection([
        _normalise_binding(
            binding, "bindings[%d]" % index
        )
        for index, binding in enumerate(raw_bindings)
    ])

    seal = _sha256(source["sealSha256"], "RDC project index sealSha256")
    unsigned = {
        "schema": RDC_PROJECT_INDEX_SCHEMA,
        "schemaVersion": RDC_PROJECT_INDEX_VERSION,
        "bindings": bindings,
    }
    expected = _canonical_sha256(unsigned)
    if not hmac.compare_digest(seal, expected):
        raise RdcProjectIndexError("RDC project index sealSha256 does not match its contents")
    return {**unsigned, "sealSha256": seal}


def _normalise_v2_index(source: Mapping[str, Any]) -> dict[str, Any]:
    """Verify the exact v2 seal before migrating to non-authoritative v3."""

    if (
        source["schema"] != RDC_PROJECT_INDEX_V2_SCHEMA
        or source["schemaVersion"] != RDC_PROJECT_INDEX_V2_VERSION
    ):
        raise RdcProjectIndexError("RDC project index schema/version is unsupported")
    raw_bindings = source["bindings"]
    if not isinstance(raw_bindings, list) or len(raw_bindings) > MAX_PROJECT_BINDINGS:
        raise RdcProjectIndexError("RDC project index bindings are invalid or oversized")
    # Preserve historical API/encoding spellings while checking the v2 digest
    # and outer seal. Canonicalisation is allowed only after both checks pass.
    sealed_bindings = _normalise_binding_collection([
        _normalise_v2_binding(
            binding,
            "bindings[%d]" % index,
            allow_legacy_protocol_tokens=True,
            preserve_legacy_protocol_tokens=True,
        )
        for index, binding in enumerate(raw_bindings)
    ])
    seal = _sha256(source["sealSha256"], "RDC project index sealSha256")
    unsigned = {
        "schema": RDC_PROJECT_INDEX_V2_SCHEMA,
        "schemaVersion": RDC_PROJECT_INDEX_V2_VERSION,
        "bindings": sealed_bindings,
    }
    if not hmac.compare_digest(seal, _canonical_sha256(unsigned)):
        raise RdcProjectIndexError("RDC project index sealSha256 does not match its contents")
    canonical_bindings = _normalise_binding_collection([
        _normalise_v2_binding(
            binding,
            "bindings[%d]" % index,
            allow_legacy_protocol_tokens=True,
            preserve_legacy_protocol_tokens=False,
        )
        for index, binding in enumerate(sealed_bindings)
    ])
    migrated = [_migrate_v2_binding(binding) for binding in canonical_bindings]
    return _normalise_v3_index(_seal_unsigned(migrated))


def _normalise_legacy_index(source: Mapping[str, Any]) -> dict[str, Any]:
    """Verify a v1 seal first, then return a newly sealed canonical v3 index."""

    if (
        source["schema"] != RDC_PROJECT_INDEX_LEGACY_SCHEMA
        or source["schemaVersion"] != RDC_PROJECT_INDEX_LEGACY_VERSION
    ):
        raise RdcProjectIndexError("RDC project index schema/version is unsupported")
    raw_bindings = source["bindings"]
    if not isinstance(raw_bindings, list) or len(raw_bindings) > MAX_PROJECT_BINDINGS:
        raise RdcProjectIndexError("RDC project index bindings are invalid or oversized")
    legacy_bindings = [
        _normalise_legacy_binding_document(binding, "bindings[%d]" % index)
        for index, binding in enumerate(raw_bindings)
    ]
    binding_keys = [_binding_key(binding) for binding in legacy_bindings]
    if len(set(binding_keys)) != len(binding_keys):
        raise RdcProjectIndexError("RDC project index contains duplicate project bindings")
    legacy_bindings.sort(key=_binding_key)

    shader_count = sum(len(binding["shaders"]) for binding in legacy_bindings)
    material_count = sum(len(binding["materials"]) for binding in legacy_bindings)
    event_count = sum(
        len(shader["identity"]["eventIds"])
        for binding in legacy_bindings
        for shader in binding["shaders"]
    )
    if shader_count > MAX_SHADER_RECORDS:
        raise RdcProjectIndexError("RDC project index has too many Shader records")
    if material_count > MAX_MATERIAL_RECORDS:
        raise RdcProjectIndexError("RDC project index has too many material records")
    if event_count > MAX_TOTAL_EVENT_IDS:
        raise RdcProjectIndexError("RDC project index has too many event references")

    seal = _sha256(source["sealSha256"], "RDC project index sealSha256")
    legacy_unsigned = {
        "schema": RDC_PROJECT_INDEX_LEGACY_SCHEMA,
        "schemaVersion": RDC_PROJECT_INDEX_LEGACY_VERSION,
        "bindings": legacy_bindings,
    }
    if not hmac.compare_digest(seal, _canonical_sha256(legacy_unsigned)):
        raise RdcProjectIndexError("RDC project index sealSha256 does not match its contents")

    desired_bindings = [
        _normalise_v2_binding(binding, "bindings[%d]" % index)
        for index, binding in enumerate(legacy_bindings)
    ]
    migrated_bindings = [
        _migrate_v2_binding(binding) for binding in desired_bindings
    ]
    return _normalise_v3_index(_seal_unsigned(migrated_bindings))


def _normalise_index(value: Any) -> dict[str, Any]:
    source = _normalise_index_header(value)
    pair = (source["schema"], source["schemaVersion"])
    if pair == (RDC_PROJECT_INDEX_SCHEMA, RDC_PROJECT_INDEX_VERSION):
        return _normalise_v3_index(source)
    if pair == (RDC_PROJECT_INDEX_V2_SCHEMA, RDC_PROJECT_INDEX_V2_VERSION):
        return _normalise_v2_index(source)
    if pair == (
        RDC_PROJECT_INDEX_LEGACY_SCHEMA,
        RDC_PROJECT_INDEX_LEGACY_VERSION,
    ):
        return _normalise_legacy_index(source)
    raise RdcProjectIndexError("RDC project index schema/version is unsupported")


def new_index() -> dict[str, Any]:
    """Return a new sealed, empty canonical v3 RDC project index."""

    return _seal_unsigned([])


def parse_index(payload: bytes | bytearray | memoryview | str) -> dict[str, Any]:
    """Parse and verify a v1/v2/v3 payload, returning canonical sealed v3."""

    if isinstance(payload, str):
        try:
            raw = payload.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RdcProjectIndexError("RDC project index is not valid UTF-8") from exc
    elif isinstance(payload, (bytes, bytearray, memoryview)):
        raw = bytes(payload)
    else:
        raise RdcProjectIndexError("RDC project index payload must be bytes or text")
    if not raw or len(raw) > MAX_INDEX_BYTES:
        raise RdcProjectIndexError(
            "RDC project index payload must contain 1..%d bytes" % MAX_INDEX_BYTES
        )
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except UnicodeDecodeError as exc:
        raise RdcProjectIndexError("RDC project index is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise RdcProjectIndexError("RDC project index is not valid JSON") from exc
    result = _normalise_index(value)
    if len(_canonical_bytes(result)) > MAX_INDEX_BYTES:
        raise RdcProjectIndexError("RDC project index canonical payload is oversized")
    return copy.deepcopy(result)


def encode_index(index: Mapping[str, Any]) -> bytes:
    """Validate and encode one sealed index as canonical UTF-8 JSON."""

    clean = _normalise_index(index)
    payload = _canonical_bytes(clean)
    if len(payload) > MAX_INDEX_BYTES:
        raise RdcProjectIndexError("RDC project index payload is oversized")
    return payload


def upsert_project_binding(
    index: Mapping[str, Any], binding: Mapping[str, Any]
) -> dict[str, Any]:
    """Insert or replace one exact ``(projectId, workspaceId)`` binding."""

    clean_index = _normalise_index(index)
    clean_binding = _normalise_binding(binding)
    wanted = _binding_key(clean_binding)
    bindings = [
        existing
        for existing in clean_index["bindings"]
        if _binding_key(existing) != wanted
    ]
    if len(bindings) >= MAX_PROJECT_BINDINGS:
        raise RdcProjectIndexError("RDC project index binding limit reached")
    bindings.append(clean_binding)
    bindings.sort(key=_binding_key)
    result = _seal_unsigned(bindings)
    # Re-run aggregate limits and the seal check before returning publishable data.
    return _normalise_index(result)


def remove_project_binding(
    index: Mapping[str, Any], project_id: str, workspace_id: str | None = None
) -> dict[str, Any]:
    """Remove one workspace binding, or every binding for one project."""

    clean_index = _normalise_index(index)
    project = _hex32(project_id, "projectId")
    workspace = _hex32(workspace_id, "workspaceId") if workspace_id is not None else None
    bindings = [
        binding
        for binding in clean_index["bindings"]
        if not (
            binding["projectId"] == project
            and (workspace is None or binding["workspaceId"] == workspace)
        )
    ]
    return _normalise_index(_seal_unsigned(bindings))


def binding_knowledge_is_current(
    binding: Mapping[str, Any], knowledge_content_sha256: str
) -> bool:
    """Return whether a binding targets the exact current project knowledge."""

    clean = _normalise_binding(binding)
    expected = _sha256(knowledge_content_sha256, "knowledgeContentSha256")
    return hmac.compare_digest(clean["knowledgeContentSha256"], expected)


def select_project_binding(
    index: Mapping[str, Any],
    project_id: str,
    workspace_id: str,
    *,
    knowledge_content_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Select one binding, returning ``None`` when absent or knowledge-stale."""

    clean_index = _normalise_index(index)
    wanted = (_hex32(project_id, "projectId"), _hex32(workspace_id, "workspaceId"))
    expected_knowledge = (
        _sha256(knowledge_content_sha256, "knowledgeContentSha256")
        if knowledge_content_sha256 is not None
        else None
    )
    for binding in clean_index["bindings"]:
        if _binding_key(binding) != wanted:
            continue
        if expected_knowledge is not None and not hmac.compare_digest(
            binding["knowledgeContentSha256"], expected_knowledge
        ):
            return None
        return copy.deepcopy(binding)
    return None


def new() -> dict[str, Any]:
    """Short public alias for :func:`new_index`."""

    return new_index()


def parse(payload: bytes | bytearray | memoryview | str) -> dict[str, Any]:
    """Short public alias for :func:`parse_index`."""

    return parse_index(payload)


def encode(index: Mapping[str, Any]) -> bytes:
    """Short public alias for :func:`encode_index`."""

    return encode_index(index)


def upsert(index: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    """Short public alias for :func:`upsert_project_binding`."""

    return upsert_project_binding(index, binding)


def remove(
    index: Mapping[str, Any], project_id: str, workspace_id: str | None = None
) -> dict[str, Any]:
    """Short public alias for :func:`remove_project_binding`."""

    return remove_project_binding(index, project_id, workspace_id)


def select(
    index: Mapping[str, Any],
    project_id: str,
    workspace_id: str,
    *,
    knowledge_content_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Short public alias for :func:`select_project_binding`."""

    return select_project_binding(
        index,
        project_id,
        workspace_id,
        knowledge_content_sha256=knowledge_content_sha256,
    )


__all__ = [
    "MAX_EVENT_IDS_PER_SHADER",
    "MAX_INDEX_BYTES",
    "MAX_MATERIAL_PARAMETERS",
    "MAX_MATERIAL_RECORDS",
    "MAX_PROJECT_BINDINGS",
    "MAX_SHADER_RECORDS",
    "RDC_PROJECT_INDEX_LEGACY_SCHEMA",
    "RDC_PROJECT_INDEX_LEGACY_VERSION",
    "RDC_PROJECT_INDEX_SCHEMA",
    "RDC_PROJECT_INDEX_SECTION_NAME",
    "RDC_PROJECT_INDEX_VERSION",
    "RDC_PROJECT_INDEX_V2_SCHEMA",
    "RDC_PROJECT_INDEX_V2_VERSION",
    "RdcProjectIndexError",
    "binding_knowledge_is_current",
    "build_desired_project_head",
    "build_project_binding",
    "build_realized_apply_receipt",
    "build_realized_apply_snapshot",
    "encode",
    "encode_index",
    "new",
    "new_index",
    "normalise_project_binding",
    "parse",
    "parse_index",
    "remove",
    "remove_project_binding",
    "referenced_heads_sha256",
    "select",
    "select_project_binding",
    "upsert",
    "upsert_project_binding",
]
