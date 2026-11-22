"""Fail-closed project index for knowledge shared across capture frames.

Projects persist only portable, content-bound knowledge. Replay handles,
capture sessions, ABI reports and material snapshots remain capture-local and
are never promoted into project authority.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Any, Iterator, Mapping, Sequence
import uuid

from .shader_workspace.io import (
    atomic_write_bytes as _atomic_write_bytes,
    atomic_write_json as _atomic_write_json,
    is_within, read_json_object,
)
from .shader_workspace.explanations import source_binding_metadata
from .shader_source_publication import SOURCE_ONLY_PUBLICATION
from .symbols.matcher import ALIAS_SCHEMA, ALIAS_VERSION, _normalise_catalog


def atomic_write_bytes(path: Path, value: bytes) -> None:
    """Project artifacts use short same-directory staging names."""
    _atomic_write_bytes(path, value, compact_temporary=True)


def atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_json(path, value, compact_temporary=True)


PROJECT_SCHEMA = "renderdoc-ultra-replay-project/v1"
PROJECT_VERSION = 1
MAX_PROJECT_BYTES = 64 * 1024 * 1024
MAX_CAPTURE_COUNT = 256
MAX_CAPTURE_REVISION_COUNT = 4096
MAX_SHADER_COUNT = 65536
MAX_KNOWLEDGE_COUNT = 4096
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_COMPILED_CANDIDATE_BYTES = 512 * 1024 * 1024
MAX_ABI_REPORT_BYTES = 16 * 1024 * 1024
MAX_ANALYSIS_BYTES = 1024 * 1024
MAX_ALIAS_BYTES = 64 * 1024 * 1024
MAX_CAPTURE_CATALOG_BYTES = 256 * 1024 * 1024
MAX_CAPTURE_SHADER_BYTES = 512 * 1024 * 1024
MAX_ALIASES = 65536
MAX_MATERIAL_PARAMETERS = 256
MAX_INVENTORY_OBSERVATIONS = 4096
MAX_REUSE_CACHE_BYTES = 256 * 1024 * 1024
MAX_ACTIVE_KNOWLEDGE_REVISION = (1 << 63) - 1
REUSE_CACHE_SCHEMA = "renderdoc-ultra-project-reuse-cache/v1"
REUSE_CACHE_VERSION = 1
REUSE_MATCH_POLICY = "raw-exact-or-code-interface-layout-exact-v4-current-source-family"
REUSE_MATCH_POLICY_SHA256 = hashlib.sha256(REUSE_MATCH_POLICY.encode("utf-8")).hexdigest()
RESTORE_KNOWLEDGE_SCOPE_SCHEMA = (
    "renderdoc-ultra-project-referenced-knowledge/v1"
)
ACTIVE_HEAD_RECEIPT_SCHEMA = "renderdoc-ultra-project-active-head/v1"
ACTIVE_HEAD_RECEIPT_VERSION = 1
COMPILED_CANDIDATE_SCHEMA = "renderdoc-ultra-project-compiled-candidate/v1"
COMPILED_CANDIDATE_VERSION = 1
SHADER_INVENTORY_SCHEMA = "renderdoc-ultra-project-shader-inventory/v1"
SHADER_INVENTORY_VERSION = 1
SHADER_INVENTORY_REUSE_SCHEMA = (
    "renderdoc-ultra-project-shader-inventory-reuse/v1"
)
SHADER_INVENTORY_REUSE_VERSION = 1
SHADER_INVENTORY_BINDING_SCHEMA = (
    "renderdoc-ultra-project-shader-inventory-binding/v1"
)
SHADER_INVENTORY_BINDING_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


def _workspace_publication_scope() -> dict[str, Any]:
    """The base project stores source/analysis, not navigable UI diagrams.

    ActiveReplayProjectService adds its own published diagrams to this receipt.
    Explicit-project publication must return the same truthful empty scope so
    the shared readback validator does not reject an already committed source.
    """
    return {
        "flowchartArtifacts": [],
        "flowGraphArtifacts": [],
        "policy": {
            "flowchartExplanatoryOnly": True,
            "flowGraphNavigationOnly": True,
            "physicalEvidence": False,
            "applyAuthorization": False,
        },
    }

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class ReplayProjectError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_bytes(path: Path, maximum: int, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ReplayProjectError("%s must be a regular non-symlink file" % label)
    size = path.stat().st_size
    if size <= 0 or size > maximum:
        raise ReplayProjectError("%s must contain 1..%d bytes" % (label, maximum))
    from .project_read_cache import reading
    with reading(path):
        value = path.read_bytes()
    if len(value) != size:
        raise ReplayProjectError("%s changed while it was read" % label)
    return value


def _sha(value: Any, field: str, allow_empty: bool = False) -> str:
    text = str(value or "").strip().lower()
    if not text and allow_empty:
        return ""
    if not SHA256_RE.fullmatch(text):
        raise ReplayProjectError("%s must be a lowercase SHA-256" % field)
    return text


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ReplayProjectError("%s must be a positive integer" % field)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReplayProjectError("%s must be a positive integer" % field) from exc
    if result <= 0:
        raise ReplayProjectError("%s must be a positive integer" % field)
    return result


def _canonical_positive(value: Any, field: str) -> int:
    """Validate an integer stored inside a canonical persisted identity."""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReplayProjectError("%s must be a positive integer" % field)
    return value


def _nonnegative(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ReplayProjectError("%s must be a non-negative integer" % field)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReplayProjectError(
            "%s must be a non-negative integer" % field
        ) from exc
    if result < 0:
        raise ReplayProjectError("%s must be a non-negative integer" % field)
    return result


def _text(value: Any, field: str, maximum: int = 4096, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ReplayProjectError("%s must be a string" % field)
    result = value.strip()
    if (not result and not allow_empty) or len(result) > maximum or "\x00" in result:
        raise ReplayProjectError("%s has an invalid length/content" % field)
    return result


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _compiled_candidate_identity(value: Any) -> dict[str, Any]:
    """Validate the immutable, source-bound identity of a published binary.

    The project keeps the origin ABI receipt only as publication provenance.
    It is deliberately not an apply lease and contains no capture-local
    replacement/session identity.
    """

    if not isinstance(value, Mapping):
        raise ReplayProjectError("workspace compiled candidate is invalid")
    if value.get("validationScope") == "manual-edit-unvalidated":
        from .project_manual_shader import compiled_identity
        try:
            return compiled_identity(value, COMPILED_CANDIDATE_SCHEMA, COMPILED_CANDIDATE_VERSION)
        except (ValueError, KeyError, TypeError) as exc:
            raise ReplayProjectError(str(exc)) from exc
    allowed = {
        "schema",
        "schemaVersion",
        "rawSha256",
        "codeSha256",
        "byteSize",
        "kind",
        "sourceSha256",
        "sourceRevision",
        "targetProfile",
        "originBaselineAbiSha256",
        "originCandidateAbiSha256",
        "originAbiReportSha256",
        "originRequestId",
        "validationScope",
        "semanticEquivalenceVerified",
        "artifactPath",
    }
    if set(value) - allowed:
        raise ReplayProjectError("workspace compiled candidate has unsupported fields")
    if (
        value.get("schema") != COMPILED_CANDIDATE_SCHEMA
        or value.get("schemaVersion") != COMPILED_CANDIDATE_VERSION
    ):
        raise ReplayProjectError("workspace compiled candidate schema/version is invalid")
    profile = value.get("targetProfile")
    if not isinstance(profile, Mapping):
        raise ReplayProjectError("workspace compiled candidate targetProfile is invalid")
    if value.get("validationScope") != "replay-reflection-abi-compatible":
        raise ReplayProjectError("workspace compiled candidate lacks full ABI validation")
    if value.get("semanticEquivalenceVerified") is not False:
        raise ReplayProjectError(
            "workspace compiled candidate semantic-equivalence policy is invalid"
        )
    if value.get("kind") == "GLSL" and (
        profile.get("apiEnvironment") != "opengl"
        or profile.get("containerFormat") not in {"glsl", "opengl-spirv"}
        or value.get("rawSha256") != value.get("sourceSha256")
        or value.get("codeSha256") != value.get("rawSha256")
    ):
        raise ReplayProjectError("native GLSL candidate source/API binding is invalid")
    return {
        "schema": COMPILED_CANDIDATE_SCHEMA,
        "schemaVersion": COMPILED_CANDIDATE_VERSION,
        "rawSha256": _sha(
            value.get("rawSha256"), "compiledCandidate.rawSha256"
        ),
        "codeSha256": _sha(
            value.get("codeSha256"), "compiledCandidate.codeSha256"
        ),
        "byteSize": _positive(
            value.get("byteSize"), "compiledCandidate.byteSize"
        ),
        "kind": _text(
            str(value.get("kind", "")), "compiledCandidate.kind", 64
        ),
        "sourceSha256": _sha(
            value.get("sourceSha256"), "compiledCandidate.sourceSha256"
        ),
        "sourceRevision": _positive(
            value.get("sourceRevision"), "compiledCandidate.sourceRevision"
        ),
        "targetProfile": copy.deepcopy(dict(profile)),
        "originBaselineAbiSha256": _sha(
            value.get("originBaselineAbiSha256"),
            "compiledCandidate.originBaselineAbiSha256",
        ),
        "originCandidateAbiSha256": _sha(
            value.get("originCandidateAbiSha256"),
            "compiledCandidate.originCandidateAbiSha256",
        ),
        "originAbiReportSha256": _sha(
            value.get("originAbiReportSha256"),
            "compiledCandidate.originAbiReportSha256",
        ),
        "originRequestId": _text(
            str(value.get("originRequestId", "")),
            "compiledCandidate.originRequestId",
            256,
        ),
        "validationScope": "replay-reflection-abi-compatible",
        "semanticEquivalenceVerified": False,
    }


def _check_manual_candidate_original(candidate: Mapping[str, Any], original: Mapping[str, Any]) -> None:
    if candidate.get("validationScope") != "manual-edit-unvalidated":
        return
    receipt = candidate["manualApplyReceipt"]
    if (receipt["originalRawSha256"] != original.get("rawSha256")
            or receipt["stage"] != original.get("stage")
            or receipt["entryPoint"] != original.get("entryPoint", "")):
        raise ReplayProjectError("manual project Shader original identity differs")


def _compiler_binding_identity(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping) and value.get("schema") == "bread-native-shader-compiler-binding/v1":
        expected = {
            "schema", "schemaVersion", "backend", "api", "sourceFormat", "outputFormat",
            "compilerContract", "targetProfile", "routePlanSha256", "executionAuthority",
            "evidenceOnly", "bindingSha256",
        }
        profile = value.get("targetProfile")
        if (set(value) != expected or value.get("schemaVersion") != 1
                or value.get("backend") != "renderdoc-native" or value.get("api") != "opengl"
                or value.get("sourceFormat") != "glsl" or value.get("outputFormat") != "glsl"
                or value.get("compilerContract") != "bread-native-shader-compile/v1"
                or value.get("executionAuthority") is not False or value.get("evidenceOnly") is not True
                or not isinstance(profile, Mapping) or profile.get("apiEnvironment") != "opengl"
                or profile.get("containerFormat") not in {"glsl", "opengl-spirv"}):
            raise ReplayProjectError("native compiler binding fields/policy are invalid")
        result = copy.deepcopy(dict(value))
        claimed = result.pop("bindingSha256")
        _sha(result["routePlanSha256"], "compilerBinding.routePlanSha256")
        if _sha(claimed, "compilerBinding.bindingSha256") != _canonical_sha256(result):
            raise ReplayProjectError("workspace compiler binding hash mismatch")
        result["bindingSha256"] = claimed
        return result
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "schemaVersion",
        "targetProfile",
        "routePlanSha256",
        "hops",
        "executionAuthority",
        "evidenceOnly",
        "bindingSha256",
    }:
        raise ReplayProjectError("workspace compiler binding fields are invalid")
    if (
        value.get("schema") != "renderdoc-ultra-shader-compiler-binding/v1"
        or value.get("schemaVersion") != 1
        or value.get("executionAuthority") is not False
        or value.get("evidenceOnly") is not True
        or not isinstance(value.get("targetProfile"), Mapping)
    ):
        raise ReplayProjectError("workspace compiler binding policy is invalid")
    route_plan_value = value.get("routePlanSha256")
    route_plan_sha256 = (
        None
        if route_plan_value is None
        else _sha(route_plan_value, "compilerBinding.routePlanSha256")
    )
    raw_hops = value.get("hops")
    if not isinstance(raw_hops, list) or not raw_hops or len(raw_hops) > 4:
        raise ReplayProjectError("workspace compiler binding hops are invalid")
    hops: list[dict[str, Any]] = []
    for index, raw_hop in enumerate(raw_hops):
        label = "compilerBinding.hops[%d]" % index
        if not isinstance(raw_hop, Mapping) or set(raw_hop) != {
            "hop",
            "routeId",
            "inputFormat",
            "outputFormat",
            "toolId",
            "operation",
            "toolConfigDigest",
            "executableSha256",
            "fixedFileHashes",
            "argumentTemplate",
            "placeholders",
        }:
            raise ReplayProjectError("%s fields are invalid" % label)
        fixed_hashes = raw_hop.get("fixedFileHashes")
        arguments = raw_hop.get("argumentTemplate")
        placeholders = raw_hop.get("placeholders")
        if (
            not isinstance(fixed_hashes, Mapping)
            or len(fixed_hashes) > 64
            or not isinstance(arguments, list)
            or len(arguments) > 128
            or not isinstance(placeholders, list)
            or len(placeholders) > 32
        ):
            raise ReplayProjectError("%s compiler metadata is invalid" % label)
        clean_fixed: dict[str, str] = {}
        for path, digest in fixed_hashes.items():
            clean_fixed[_text(path, label + ".fixedFilePath", 4096)] = _sha(
                digest, label + ".fixedFileSha256"
            )
        clean_arguments = [
            _text(argument, label + ".argumentTemplate", 8192)
            for argument in arguments
        ]
        clean_placeholders = [
            _text(placeholder, label + ".placeholder", 128)
            for placeholder in placeholders
        ]
        if len(set(clean_placeholders)) != len(clean_placeholders):
            raise ReplayProjectError("workspace compiler placeholders are duplicated")
        hops.append(
            {
                "hop": _positive(raw_hop.get("hop"), label + ".hop"),
                "routeId": _text(raw_hop.get("routeId"), label + ".routeId", 256),
                "inputFormat": _text(
                    raw_hop.get("inputFormat"), label + ".inputFormat", 64
                ),
                "outputFormat": _text(
                    raw_hop.get("outputFormat"), label + ".outputFormat", 64
                ),
                "toolId": _text(raw_hop.get("toolId"), label + ".toolId", 128),
                "operation": _text(
                    raw_hop.get("operation"), label + ".operation", 64
                ),
                "toolConfigDigest": _sha(
                    raw_hop.get("toolConfigDigest"),
                    label + ".toolConfigDigest",
                ),
                "executableSha256": _sha(
                    raw_hop.get("executableSha256"),
                    label + ".executableSha256",
                ),
                "fixedFileHashes": clean_fixed,
                "argumentTemplate": clean_arguments,
                "placeholders": clean_placeholders,
            }
        )
        if hops[-1]["hop"] != index + 1:
            raise ReplayProjectError("workspace compiler hop order is invalid")
    result: dict[str, Any] = {
        "schema": "renderdoc-ultra-shader-compiler-binding/v1",
        "schemaVersion": 1,
        "targetProfile": copy.deepcopy(dict(value["targetProfile"])),
        "routePlanSha256": route_plan_sha256,
        "hops": hops,
        "executionAuthority": False,
        "evidenceOnly": True,
    }
    if _sha(
        value.get("bindingSha256"), "compilerBinding.bindingSha256"
    ) != _canonical_sha256(result):
        raise ReplayProjectError("workspace compiler binding hash mismatch")
    result["bindingSha256"] = _canonical_sha256(result)
    return result


def _validate_native_candidate_binding(candidate: Mapping[str, Any], binding: Any,
                                       source_format: str) -> None:
    if candidate.get("kind") != "GLSL":
        return
    clean = _compiler_binding_identity(binding)
    if (clean["schema"] != "bread-native-shader-compiler-binding/v1"
            or source_format != "glsl" or clean["targetProfile"] != candidate["targetProfile"]):
        raise ReplayProjectError("native GLSL candidate requires its exact native compiler binding")


def _assembly_disassembly_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReplayProjectError("assembly/disassembly artifact is invalid")
    allowed = {
        "kind",
        "role",
        "format",
        "path",
        "artifactPath",
        "sha256",
        "byteSize",
        "routePlanSha256",
        "toolId",
        "toolConfigDigest",
        "executableSha256",
        "inputFormat",
        "inputRawSha256",
        "routeId",
        "operation",
        "physicalEvidence",
        "displayEvidenceOnly",
        "editableSource",
        "applyAuthorization",
    }
    if set(value) - allowed or not {"kind", "role", "format", "sha256", "byteSize"}.issubset(value):
        raise ReplayProjectError("assembly/disassembly artifact fields are invalid")
    kind = value.get("kind")
    role = value.get("role")
    artifact_format = value.get("format")
    if role not in {"baseline", "candidate"}:
        raise ReplayProjectError("assembly/disassembly artifact policy is invalid")
    common = {
        "role": role,
        "sha256": _sha(value.get("sha256"), "disassembly.sha256"),
        "byteSize": _positive(value.get("byteSize"), "disassembly.byteSize"),
        "routePlanSha256": _sha(
            value.get("routePlanSha256"), "disassembly.routePlanSha256"
        ),
        "toolId": _text(value.get("toolId"), "disassembly.toolId", 128),
        "toolConfigDigest": _sha(
            value.get("toolConfigDigest"), "disassembly.toolConfigDigest"
        ),
        "executableSha256": _sha(
            value.get("executableSha256"), "disassembly.executableSha256"
        ),
        "physicalEvidence": True,
        "editableSource": False,
        "applyAuthorization": False,
    }
    if kind == "dxil-disassembly":
        if (
            artifact_format != "dxil-asm"
            or value.get("physicalEvidence") is not True
            or value.get("editableSource") is not False
            or value.get("applyAuthorization") is not False
            or any(
                field in value
                for field in (
                    "inputFormat",
                    "inputRawSha256",
                    "routeId",
                    "operation",
                    "displayEvidenceOnly",
                )
            )
        ):
            raise ReplayProjectError(
                "assembly/disassembly artifact policy is invalid"
            )
        return {
            "kind": "dxil-disassembly",
            **common,
            "format": "dxil-asm",
        }
    expected_inputs = {
        "dxbc-asm": "dxbc",
        "spirv-asm": "spirv",
        "opengl-spirv-asm": "opengl-spirv",
    }
    if (
        kind != "native-disassembly"
        or artifact_format not in expected_inputs
        or value.get("inputFormat") != expected_inputs.get(artifact_format)
        or value.get("operation") != "decompile"
        or value.get("physicalEvidence") is not True
        or value.get("displayEvidenceOnly") is not True
        or value.get("editableSource") is not False
        or value.get("applyAuthorization") is not False
    ):
        raise ReplayProjectError("assembly/disassembly artifact policy is invalid")
    return {
        "kind": "native-disassembly",
        **common,
        "format": artifact_format,
        "inputFormat": expected_inputs[artifact_format],
        "inputRawSha256": _sha(
            value.get("inputRawSha256"), "disassembly.inputRawSha256"
        ),
        "routeId": _text(value.get("routeId"), "disassembly.routeId", 256),
        "operation": "decompile",
        "displayEvidenceOnly": True,
    }


def _assembly_disassembly_relative(identity: Mapping[str, Any]) -> str:
    suffixes = {
        "dxil-asm": ".dxil-asm",
        "dxbc-asm": ".dxbc-asm",
        "spirv-asm": ".spvasm",
        "opengl-spirv-asm": ".opengl.spvasm",
    }
    suffix = suffixes.get(identity.get("format"))
    if suffix is None:
        raise ReplayProjectError("assembly/disassembly artifact format is invalid")
    return "knowledge/workspaces/disassembly/%s%s" % (
        identity["sha256"],
        suffix,
    )


def _inventory_origin_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "schemaVersion",
        "api",
        "encoding",
        "rawSha256",
        "stage",
        "entryPoint",
        "abiHash",
        "layoutHash",
        "stableKeySha256",
    }:
        raise ReplayProjectError("workspace inventory origin fields are invalid")
    if (
        value.get("schema")
        != "renderdoc-ultra-project-shader-inventory-origin/v1"
        or value.get("schemaVersion") != 1
    ):
        raise ReplayProjectError("workspace inventory origin schema is invalid")
    identity = {
        "api": _api_family(value.get("api")),
        "encoding": _shader_encoding(value.get("encoding")),
        "rawSha256": _sha(
            value.get("rawSha256"), "workspace.inventoryOrigin.rawSha256"
        ),
        "stage": _normalised_stage(value.get("stage")),
        "entryPoint": _text(
            value.get("entryPoint"),
            "workspace.inventoryOrigin.entryPoint",
            512,
            allow_empty=True,
        ),
        "abiHash": _sha(
            value.get("abiHash"),
            "workspace.inventoryOrigin.abiHash",
            allow_empty=True,
        ),
        "layoutHash": _sha(
            value.get("layoutHash"),
            "workspace.inventoryOrigin.layoutHash",
            allow_empty=True,
        ),
    }
    if _stable_shader_encoding(identity["encoding"]) not in (
        _SHADER_ENCODINGS_BY_API.get(identity["api"], ())
    ):
        raise ReplayProjectError(
            "workspace inventory origin API/encoding pair is unsupported"
        )
    stable = _stable_shader_identity_sha256(identity)
    if _sha(
        value.get("stableKeySha256"),
        "workspace.inventoryOrigin.stableKeySha256",
    ) != stable:
        raise ReplayProjectError("workspace inventory origin stable key mismatch")
    return {
        "schema": "renderdoc-ultra-project-shader-inventory-origin/v1",
        "schemaVersion": 1,
        **identity,
        "stableKeySha256": stable,
    }


def _inventory_observation(value: Any) -> dict[str, Any]:
    required = {
        "schema",
        "schemaVersion",
        "captureId",
        "inventorySha256",
        "stableKeySha256",
        "targetResourceId",
        "anchorEventId",
        "constantBlocksSha256",
        "workspaceManifestSha256",
        "sourceSha256",
        "compiledCandidateSha256",
        "applyAuthorization",
        "crossCaptureResourceIdReuse",
        "observedAt",
        "receiptSha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ReplayProjectError("workspace inventory observation fields are invalid")
    if (
        value.get("schema")
        != "renderdoc-ultra-project-shader-inventory-observation/v1"
        or value.get("schemaVersion") != 1
        or value.get("applyAuthorization") is not False
        or value.get("crossCaptureResourceIdReuse") is not False
    ):
        raise ReplayProjectError("workspace inventory observation policy is invalid")
    material = {
        "schema": "renderdoc-ultra-project-shader-inventory-observation/v1",
        "schemaVersion": 1,
        "captureId": _sha(value.get("captureId"), "inventoryObservation.captureId"),
        "inventorySha256": _sha(
            value.get("inventorySha256"),
            "inventoryObservation.inventorySha256",
        ),
        "stableKeySha256": _sha(
            value.get("stableKeySha256"),
            "inventoryObservation.stableKeySha256",
        ),
        "targetResourceId": _positive(
            value.get("targetResourceId"),
            "inventoryObservation.targetResourceId",
        ),
        "anchorEventId": (
            None
            if value.get("anchorEventId") is None
            else _positive(
                value.get("anchorEventId"),
                "inventoryObservation.anchorEventId",
            )
        ),
        "constantBlocksSha256": _sha(
            value.get("constantBlocksSha256"),
            "inventoryObservation.constantBlocksSha256",
        ),
        "workspaceManifestSha256": _sha(
            value.get("workspaceManifestSha256"),
            "inventoryObservation.workspaceManifestSha256",
        ),
        "sourceSha256": _sha(
            value.get("sourceSha256"), "inventoryObservation.sourceSha256"
        ),
        "compiledCandidateSha256": _sha(
            value.get("compiledCandidateSha256"),
            "inventoryObservation.compiledCandidateSha256",
            allow_empty=True,
        ),
        "applyAuthorization": False,
        "crossCaptureResourceIdReuse": False,
    }
    observed_at = _text(
        value.get("observedAt"), "inventoryObservation.observedAt", 128
    )
    receipt_sha256 = _canonical_sha256(material)
    if _sha(
        value.get("receiptSha256"), "inventoryObservation.receiptSha256"
    ) != receipt_sha256:
        raise ReplayProjectError("workspace inventory observation hash mismatch")
    return {
        **material,
        "observedAt": observed_at,
        "receiptSha256": receipt_sha256,
    }


def _workspace_source_format(value: Mapping[str, Any]) -> str:
    language = value.get("sourceFormat", "hlsl")
    if language not in {"hlsl", "glsl"}:
        raise ReplayProjectError("workspace knowledge source format must be HLSL or GLSL")
    return language


def _inventory_workspace_artifact_identity(value: Any) -> dict[str, Any]:
    """Return the capture-independent identity of inventory-published work.

    Inventory capture/resource/anchor values are validation receipts, not the
    identity of portable HLSL or compiled representations.  In particular,
    source revision numbers and ABI request/report IDs describe how a binary
    was validated in one temporary workspace; they do not change the binary,
    compiler configuration, or physical Shader ABI which may be reused.
    """

    if not isinstance(value, Mapping):
        raise ReplayProjectError("inventory workspace artifact is invalid")
    origin = _inventory_origin_identity(value.get("inventoryOrigin"))
    profile = value.get("targetProfile")
    if not isinstance(profile, Mapping):
        raise ReplayProjectError("inventory workspace targetProfile is invalid")
    identity: dict[str, Any] = {
        "schema": "renderdoc-ultra-project-shader-artifact-identity/v1",
        "schemaVersion": 1,
        "stableShader": origin,
        "physicalIdentity": {
            "stage": _normalised_stage(value.get("stage")),
            "entryPoint": _text(
                value.get("entryPoint"),
                "inventoryWorkspace.entryPoint",
                512,
                allow_empty=True,
            ),
            "rawSha256": _sha(
                value.get("rawSha256"), "inventoryWorkspace.rawSha256"
            ),
            "codeSha256": _sha(
                value.get("codeSha256"),
                "inventoryWorkspace.codeSha256",
                allow_empty=True,
            ),
            "layoutHash": _sha(
                value.get("layoutHash"),
                "inventoryWorkspace.layoutHash",
                allow_empty=True,
            ),
            "interfaceHash": _sha(
                value.get("interfaceHash"),
                "inventoryWorkspace.interfaceHash",
                allow_empty=True,
            ),
            "interfaceComplete": value.get("interfaceComplete") is True,
            "baselineAbiSha256": _sha(
                value.get("baselineAbiSha256"),
                "inventoryWorkspace.baselineAbiSha256",
                allow_empty=True,
            ),
        },
        "targetProfile": copy.deepcopy(dict(profile)),
        "sourceSha256": _sha(
            value.get("sourceSha256"), "inventoryWorkspace.sourceSha256"
        ),
    }
    if _workspace_source_format(value) != "hlsl":
        identity["sourceFormat"] = _workspace_source_format(value)
    physical = identity["physicalIdentity"]
    if any(
        (
            physical["stage"] != origin["stage"],
            physical["entryPoint"] != origin["entryPoint"],
            physical["rawSha256"] != origin["rawSha256"],
            physical["layoutHash"] != origin["layoutHash"],
            physical["baselineAbiSha256"] != origin["abiHash"],
        )
    ):
        raise ReplayProjectError(
            "inventory workspace physical identity differs from its stable Shader"
        )
    compiler_binding = value.get("compilerBinding")
    if compiler_binding is not None:
        clean_compiler = _compiler_binding_identity(compiler_binding)
        if clean_compiler["targetProfile"] != identity["targetProfile"]:
            raise ReplayProjectError(
                "inventory workspace compiler binding target profile differs"
            )
        identity["compilerBinding"] = clean_compiler
    disassembly = value.get("assemblyDisassemblyArtifacts", [])
    if not isinstance(disassembly, list) or len(disassembly) > 2:
        raise ReplayProjectError(
            "inventory workspace assembly/disassembly artifacts are invalid"
        )
    if disassembly:
        clean_disassembly = [
            _assembly_disassembly_identity(item) for item in disassembly
        ]
        if len({item["role"] for item in clean_disassembly}) != len(
            clean_disassembly
        ):
            raise ReplayProjectError(
                "inventory workspace disassembly role is duplicated"
            )
        if any(
            item["kind"] == "native-disassembly"
            and (
                item["role"] != "baseline"
                or item["inputRawSha256"] != physical["rawSha256"]
            )
            for item in clean_disassembly
        ):
            raise ReplayProjectError(
                "inventory workspace native disassembly physical binding mismatch"
            )
        identity["assemblyDisassemblyArtifacts"] = clean_disassembly
    compiled = value.get("compiledCandidate")
    if compiled is not None:
        clean_compiled = _compiled_candidate_identity(compiled)
        _check_manual_candidate_original(clean_compiled, value)
        _validate_native_candidate_binding(clean_compiled, compiler_binding, _workspace_source_format(value))
        if (
            clean_compiled["sourceSha256"] != identity["sourceSha256"]
            or clean_compiled["targetProfile"] != identity["targetProfile"]
            or clean_compiled["originBaselineAbiSha256"]
            != physical["baselineAbiSha256"]
        ):
            raise ReplayProjectError(
                "inventory workspace compiled candidate binding mismatch"
            )
        identity["compiledArtifact"] = {
            "schema": clean_compiled["schema"],
            "schemaVersion": clean_compiled["schemaVersion"],
            "rawSha256": clean_compiled["rawSha256"],
            "codeSha256": clean_compiled["codeSha256"],
            "byteSize": clean_compiled["byteSize"],
            "kind": clean_compiled["kind"],
            "sourceSha256": clean_compiled["sourceSha256"],
            "targetProfile": clean_compiled["targetProfile"],
            "originBaselineAbiSha256": clean_compiled[
                "originBaselineAbiSha256"
            ],
            "validationScope": clean_compiled["validationScope"],
            "semanticEquivalenceVerified": clean_compiled[
                "semanticEquivalenceVerified"
            ],
        }
        if clean_compiled.get("validationScope") == "manual-edit-unvalidated":
            identity["compiledArtifact"]["manualApplyReceipt"] = clean_compiled["manualApplyReceipt"]
        else:
            identity["compiledArtifact"]["originCandidateAbiSha256"] = clean_compiled["originCandidateAbiSha256"]
    return identity


def _normal_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _api_family(value: Any, allow_empty: bool = False) -> str:
    text = re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())
    aliases = {
        "direct3d11": "d3d11",
        "directx11": "d3d11",
        "direct3d12": "d3d12",
        "directx12": "d3d12",
        "opengl": "opengl",
        "vulkan": "vulkan",
        "metal": "metal",
    }
    result = aliases.get(text, text)
    if not result and allow_empty:
        return ""
    if not result or len(result) > 64:
        raise ReplayProjectError("api_family is invalid")
    return result


def _reuse_api_family(value: Any) -> str:
    """Canonical API spelling for derived reuse matching, not stored v1 heads.

    Older capture catalogs stored RenderDoc enum names and their immutable head
    keys include that spelling. Keep those historical hashes readable, and map
    only known enum values when comparing/exporting current physical identities.
    """
    spelling = str(value or "").strip().lower()
    return {
        "graphicsapi.d3d11": "d3d11",
        "graphicsapid3d11": "d3d11",
        "graphicsapi.d3d12": "d3d12",
        "graphicsapid3d12": "d3d12",
        "graphicsapi.opengl": "opengl",
        "graphicsapiopengl": "opengl",
        "graphicsapi.vulkan": "vulkan",
        "graphicsapivulkan": "vulkan",
    }.get(spelling, _api_family(value))


_SHADER_ENCODINGS_BY_API = {
    "d3d11": frozenset(("dxbc",)),
    "d3d12": frozenset(("dxbc", "dxil")),
    "opengl": frozenset(("glsl", "spirv", "opengl-spirv")),
    "vulkan": frozenset(("spirv",)),
}


def _shader_protocol_encoding(value: Any) -> str:
    """Canonicalise Bridge enum/alias spellings to a stable protocol token."""

    token = str(value or "").strip().lower().rsplit(".", 1)[-1]
    token = re.sub(r"[^a-z0-9]+", "", token)
    aliases = {
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
        "metallib": "metallib",
    }
    result = aliases.get(token)
    if result is None:
        raise ReplayProjectError("shader encoding is unsupported or not canonicalizable")
    return result


def _shader_encoding(value: Any) -> str:
    return {
        "dxbc": "DXBC",
        "dxil": "DXIL",
        "spirv": "SPIR-V",
        "opengl-spirv": "OpenGL-SPIR-V",
        "glsl": "GLSL",
        "metallib": "METALLIB",
    }[_shader_protocol_encoding(value)]


def _stable_shader_encoding(value: Any) -> str:
    """Return the lowercase protocol token used inside the seven-field key.

    Project manifests retain the established display spelling (for example
    ``DXBC``), but Bridge/Host stable keys use lowercase protocol tokens.  A
    display-only spelling difference must never produce a different physical
    Shader identity.
    """

    return _shader_protocol_encoding(value)


def _stable_shader_identity_sha256(identity: Mapping[str, Any]) -> str:
    material = dict(identity)
    material["encoding"] = _stable_shader_encoding(identity.get("encoding"))
    return _canonical_sha256(material)


def _normalised_stage(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())
    aliases = {
        "vs": "vertex",
        "vertex": "vertex",
        "hs": "hull",
        "hull": "hull",
        "tesscontrol": "hull",
        "ds": "domain",
        "domain": "domain",
        "tesseval": "domain",
        "gs": "geometry",
        "geometry": "geometry",
        "ps": "fragment",
        "pixel": "fragment",
        "fs": "fragment",
        "fragment": "fragment",
        "cs": "compute",
        "compute": "compute",
        "as": "task",
        "amplification": "task",
        "task": "task",
        "ms": "mesh",
        "mesh": "mesh",
        "raygen": "raygen",
        "raygeneration": "raygen",
        "intersection": "intersection",
        "anyhit": "anyhit",
        "closesthit": "closesthit",
        "miss": "miss",
        "callable": "callable",
    }
    result = aliases.get(text)
    if result is None:
        raise ReplayProjectError("shader stage is unsupported or not canonicalizable")
    return result


def _normalise_shader_inventory(value: Any) -> dict[str, Any]:
    """Validate the bridge-independent, content-bound live Shader inventory.

    Aggregate digests are always recomputed here.  The inventory deliberately
    contains no capture session, old replacement handle, or apply authority.
    ``captureId`` identifies the set of stable Shader keys, while
    ``inventorySha256``/``catalogSha256`` bind the current ResourceIds,
    representative EIDs, and CBuffer attachments used by this restore.
    """

    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "schemaVersion",
        "capture",
        "shaders",
    }:
        raise ReplayProjectError("shader inventory fields are invalid")
    if (
        value.get("schema") != SHADER_INVENTORY_SCHEMA
        or value.get("schemaVersion") != SHADER_INVENTORY_VERSION
    ):
        raise ReplayProjectError("shader inventory schema/version is invalid")
    raw_capture = value.get("capture")
    if not isinstance(raw_capture, Mapping) or set(raw_capture) != {
        "captureId",
        "catalogSha256",
        "inventorySha256",
        "api",
    }:
        raise ReplayProjectError("shader inventory capture binding is invalid")
    api = _api_family(raw_capture.get("api"))
    raw_shaders = value.get("shaders")
    if (
        not isinstance(raw_shaders, list)
        or not raw_shaders
        or len(raw_shaders) > MAX_SHADER_COUNT
    ):
        raise ReplayProjectError("shader inventory shaders are invalid or oversized")

    shaders: list[dict[str, Any]] = []
    seen_targets: set[tuple[int, str]] = set()
    resource_anchors: dict[int, int | None] = {}
    for shader_index, raw_shader in enumerate(raw_shaders):
        label = "shader inventory shaders[%d]" % shader_index
        required_shader_fields = {
            "resourceId",
            "api",
            "encoding",
            "rawSha256",
            "stage",
            "entryPoint",
            "abiHash",
            "layoutHash",
            "interfaceHash",
            "interfaceComplete",
            "stableKeySha256",
            "anchorEventId",
            "constantBlocks",
        }
        if not isinstance(raw_shader, Mapping) or set(raw_shader) != required_shader_fields:
            raise ReplayProjectError("%s fields are invalid" % label)
        resource_id = _positive(raw_shader.get("resourceId"), label + ".resourceId")
        shader_api = _api_family(raw_shader.get("api"))
        if shader_api != api:
            raise ReplayProjectError("shader inventory Shader API differs from capture API")
        encoding = _shader_encoding(raw_shader.get("encoding"))
        protocol_encoding = _stable_shader_encoding(encoding)
        if protocol_encoding not in _SHADER_ENCODINGS_BY_API.get(shader_api, ()):
            raise ReplayProjectError(
                "shader inventory API/encoding pair is unsupported"
            )
        raw_sha256 = _sha(raw_shader.get("rawSha256"), label + ".rawSha256")
        stage = _normalised_stage(raw_shader.get("stage"))
        entry_point = _text(
            raw_shader.get("entryPoint"),
            label + ".entryPoint",
            512,
            allow_empty=True,
        )
        abi_hash = _sha(
            raw_shader.get("abiHash"), label + ".abiHash", allow_empty=True
        )
        layout_hash = _sha(
            raw_shader.get("layoutHash"),
            label + ".layoutHash",
            allow_empty=True,
        )
        interface_hash = _sha(
            raw_shader.get("interfaceHash"),
            label + ".interfaceHash",
            allow_empty=True,
        )
        if not isinstance(raw_shader.get("interfaceComplete"), bool):
            raise ReplayProjectError("%s.interfaceComplete must be a boolean" % label)
        interface_complete = raw_shader["interfaceComplete"]
        if interface_complete and (not abi_hash or not interface_hash):
            raise ReplayProjectError(
                "%s complete interface lacks ABI/interface identity" % label
            )
        anchor_value = raw_shader.get("anchorEventId")
        anchor_event_id = (
            None
            if anchor_value is None
            else _positive(anchor_value, label + ".anchorEventId")
        )
        raw_blocks = raw_shader.get("constantBlocks")
        if not isinstance(raw_blocks, list) or len(raw_blocks) > MAX_MATERIAL_PARAMETERS:
            raise ReplayProjectError("%s.constantBlocks is invalid or oversized" % label)
        blocks: list[dict[str, Any]] = []
        block_keys: set[tuple[int, str, int, int]] = set()
        for block_index, raw_block in enumerate(raw_blocks):
            block_label = "%s.constantBlocks[%d]" % (label, block_index)
            if not isinstance(raw_block, Mapping) or set(raw_block) != {
                "index",
                "path",
                "name",
                "space",
                "bind",
                "byteSize",
                "layoutSha256",
                "abiSha256",
                "fields",
            }:
                raise ReplayProjectError("%s fields are invalid" % block_label)
            raw_fields = raw_block.get("fields")
            if not isinstance(raw_fields, list) or len(raw_fields) > MAX_ALIASES:
                raise ReplayProjectError(
                    "%s.fields is invalid or oversized" % block_label
                )
            fields: list[dict[str, Any]] = []
            field_paths: set[str] = set()
            for field_index, raw_field in enumerate(raw_fields):
                field_label = "%s.fields[%d]" % (block_label, field_index)
                if not isinstance(raw_field, Mapping) or set(raw_field) != {
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
                }:
                    raise ReplayProjectError("%s fields are invalid" % field_label)
                if not isinstance(raw_field.get("rowMajor"), bool):
                    raise ReplayProjectError("%s.rowMajor must be a boolean" % field_label)
                field = {
                    "canonicalPath": _text(
                        raw_field.get("canonicalPath"),
                        field_label + ".canonicalPath",
                        2048,
                    ),
                    "name": _text(
                        raw_field.get("name"), field_label + ".name", 1024
                    ),
                    "absoluteByteOffset": _nonnegative(
                        raw_field.get("absoluteByteOffset"),
                        field_label + ".absoluteByteOffset",
                    ),
                    "byteSize": _positive(
                        raw_field.get("byteSize"), field_label + ".byteSize"
                    ),
                    "baseType": _text(
                        raw_field.get("baseType"),
                        field_label + ".baseType",
                        64,
                    ),
                    "rows": _nonnegative(
                        raw_field.get("rows"), field_label + ".rows"
                    ),
                    "columns": _nonnegative(
                        raw_field.get("columns"), field_label + ".columns"
                    ),
                    "elements": _nonnegative(
                        raw_field.get("elements"), field_label + ".elements"
                    ),
                    "arrayByteStride": _nonnegative(
                        raw_field.get("arrayByteStride"),
                        field_label + ".arrayByteStride",
                    ),
                    "matrixByteStride": _nonnegative(
                        raw_field.get("matrixByteStride"),
                        field_label + ".matrixByteStride",
                    ),
                    "rowMajor": raw_field["rowMajor"],
                    "flags": _nonnegative(
                        raw_field.get("flags"), field_label + ".flags"
                    ),
                    "pointerTypeID": _nonnegative(
                        raw_field.get("pointerTypeID"),
                        field_label + ".pointerTypeID",
                    ),
                    "bitFieldOffset": _nonnegative(
                        raw_field.get("bitFieldOffset"),
                        field_label + ".bitFieldOffset",
                    ),
                    "bitFieldSize": _nonnegative(
                        raw_field.get("bitFieldSize"),
                        field_label + ".bitFieldSize",
                    ),
                }
                if field["canonicalPath"] in field_paths:
                    raise ReplayProjectError(
                        "shader inventory CBuffer canonicalPath is duplicated"
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
            block = {
                "index": _nonnegative(raw_block.get("index"), block_label + ".index"),
                "path": _text(raw_block.get("path"), block_label + ".path", 2048),
                "name": _text(raw_block.get("name"), block_label + ".name", 1024),
                "space": _nonnegative(raw_block.get("space"), block_label + ".space"),
                "bind": _nonnegative(raw_block.get("bind"), block_label + ".bind"),
                "byteSize": _positive(raw_block.get("byteSize"), block_label + ".byteSize"),
                "layoutSha256": _sha(
                    raw_block.get("layoutSha256"), block_label + ".layoutSha256"
                ),
                "abiSha256": _sha(
                    raw_block.get("abiSha256"), block_label + ".abiSha256"
                ),
                "fields": fields,
            }
            if any(
                field["absoluteByteOffset"] + field["byteSize"]
                > block["byteSize"]
                for field in fields
            ):
                raise ReplayProjectError(
                    "shader inventory CBuffer field exceeds block byteSize"
                )
            block_key = (
                block["index"],
                block["path"],
                block["space"],
                block["bind"],
            )
            if block_key in block_keys:
                raise ReplayProjectError("shader inventory constant block is duplicated")
            block_keys.add(block_key)
            blocks.append(block)
        # Match the Bridge inventory's binding order before recomputing its
        # aggregate digest; reflection indices need not follow register order.
        blocks.sort(
            key=lambda item: (
                item["space"], item["bind"], item["index"], item["path"]
            )
        )
        stable_identity = {
            "api": api,
            "encoding": encoding,
            "rawSha256": raw_sha256,
            "stage": stage,
            "entryPoint": entry_point,
            "abiHash": abi_hash,
            "layoutHash": layout_hash,
        }
        stable_key_sha256 = _stable_shader_identity_sha256(stable_identity)
        if _sha(
            raw_shader.get("stableKeySha256"), label + ".stableKeySha256"
        ) != stable_key_sha256:
            raise ReplayProjectError("shader inventory stableKeySha256 mismatch")
        target_key = (resource_id, stable_key_sha256)
        if target_key in seen_targets:
            raise ReplayProjectError(
                "shader inventory resource/entry identity is duplicated"
            )
        seen_targets.add(target_key)
        if (
            resource_id in resource_anchors
            and resource_anchors[resource_id] != anchor_event_id
        ):
            raise ReplayProjectError(
                "shader inventory resource has conflicting representative EIDs"
            )
        resource_anchors[resource_id] = anchor_event_id
        shaders.append(
            {
                "resourceId": resource_id,
                **stable_identity,
                "interfaceHash": interface_hash,
                "interfaceComplete": interface_complete,
                "stableKeySha256": stable_key_sha256,
                "anchorEventId": anchor_event_id,
                "constantBlocks": blocks,
            }
        )

    shaders.sort(
        key=lambda item: (
            item["resourceId"],
            item["stage"],
            item["entryPoint"],
            item["rawSha256"],
        )
    )
    stable_key_set = sorted({item["stableKeySha256"] for item in shaders})
    capture_id = _canonical_sha256(stable_key_set)
    inventory_material = {
        "api": api,
        "shaders": [
            {
                "resourceId": item["resourceId"],
                "stableKeySha256": item["stableKeySha256"],
                "anchorEventId": item["anchorEventId"],
                "constantBlocks": item["constantBlocks"],
            }
            for item in shaders
        ],
    }
    inventory_sha256 = _canonical_sha256(inventory_material)
    if _sha(raw_capture.get("captureId"), "shader inventory captureId") != capture_id:
        raise ReplayProjectError("shader inventory captureId mismatch")
    declared_inventory = _sha(
        raw_capture.get("inventorySha256"), "shader inventory inventorySha256"
    )
    declared_catalog = _sha(
        raw_capture.get("catalogSha256"), "shader inventory catalogSha256"
    )
    if declared_inventory != inventory_sha256 or declared_catalog != inventory_sha256:
        raise ReplayProjectError("shader inventory aggregate hash mismatch")
    return {
        "schema": SHADER_INVENTORY_SCHEMA,
        "schemaVersion": SHADER_INVENTORY_VERSION,
        "capture": {
            "captureId": capture_id,
            "catalogSha256": inventory_sha256,
            "inventorySha256": inventory_sha256,
            "api": api,
        },
        "shaders": shaders,
    }


def _thread_lock(root: Path) -> threading.RLock:
    key = os.path.normcase(str(root))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _project_lock(root: Path) -> Iterator[None]:
    """Cross-process exclusive lock for one replay project's mutations.

    The lock file carries its owner (pid + process start identity, the same v2
    payload the active-project selection lock uses), so a lock left behind by a
    Host that was killed mid-operation is moved aside and reacquired instead of
    turning the whole project "unavailable" until someone deletes a file by
    hand. A legacy 32-hex token (written by builds before this) still needs
    that manual step -- it recorded no owner, so nothing can prove it dead.
    """

    from . import active_projects as _selection_locks

    lock_path = root / ".project-operation.lock"
    token = uuid.uuid4().hex
    payload = _selection_locks._exclusive_project_lock_payload(token)
    recovered: Path | None = None
    for attempt in range(2):
        try:
            _selection_locks._publish_exclusive_project_lock(lock_path, payload)
            break
        except FileExistsError as exc:
            if attempt or recovered is not None:
                raise ReplayProjectError(
                    "project is locked; its owner could not be proven inactive "
                    "(%s)" % lock_path
                ) from exc
            recovered = _selection_locks._archive_recoverable_exclusive_project_lock(lock_path)
            if recovered is None:
                raise ReplayProjectError(
                    "project is locked; a stale lock must be reviewed and removed "
                    "manually (%s)" % lock_path
                ) from exc
    try:
        yield
    finally:
        try:
            snapshot = _selection_locks._lock_snapshot(lock_path)
            if snapshot is not None and snapshot[0] == payload:
                lock_path.unlink()
        except OSError:
            pass
        if recovered is not None:
            try:
                recovered.unlink()
            except OSError:
                pass


def _backup_local_edit(workspace_root: Path, destination: Path, current_bytes: bytes,
                       *, superseding: bool) -> dict[str, Any] | None:
    """Keep a person's edit next to the workspace before the project source replaces it.

    The project version is the one every capture applies; a local edit is not
    lost for that -- it is written to ``ai/local-edits/<time>-<sha8>.hlsl`` so it
    can be diffed or restored by hand.
    """

    if not superseding:
        return None
    digest = _sha256_bytes(current_bytes)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups = workspace_root / "ai" / "local-edits"
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / ("%s-%s%s" % (stamp, digest[:8], destination.suffix or ".hlsl"))
    if not backup.exists():
        atomic_write_bytes(backup, current_bytes)
    return {"backupPath": str(backup), "sourceSha256": digest, "bytes": len(current_bytes)}


def _write_seeded_workspace_source(
    root: Path, destination: Path, current_bytes: bytes, source_bytes: bytes,
    status: Mapping[str, Any], bundle: Mapping[str, Any] | None,
) -> None:
    """Replace source and its managed import provenance in one locked transaction.

    A project import's immutable input authenticates the imported revision.
    Copying a newer head into that workspace must renew that input/receipt too;
    preserving the previous receipt makes the next restore reject valid bytes.
    The caller owns both the project and workspace locks and checked source CAS.
    """
    if bundle is None:
        atomic_write_bytes(destination, source_bytes)
        return
    from .shader_workspace.project_source import source_receipt, validate_project_source

    manifest_path = root / "manifest.json"
    before_bytes = _file_bytes(manifest_path, MAX_PROJECT_BYTES, "destination manifest")
    manifest = json.loads(before_bytes.decode("utf-8"))
    original = manifest.get("source") or {}
    if (manifest.get("workspaceId") != status.get("workspaceId")
            or original.get("projectSource") != status["source"].get("projectSource")
            or (root / str(original.get("path", ""))).resolve() != destination
            or manifest.get("baseline", {}).get("rawSha256") != status["baseline"].get("rawSha256")
            or manifest.get("baseline", {}).get("abiSha256") != status["baseline"].get("abiSha256")
            or manifest.get("targetProfile") != status.get("targetProfile")
            or manifest.get("stage") != status.get("stage")
            or manifest.get("entryPoint", "") != status.get("entryPoint", "")):
        raise ReplayProjectError("destination project source provenance changed after validation")
    if source_bytes != bundle["sourceText"].encode("utf-8"):
        raise ReplayProjectError("verified project source changed before seed")
    language = bundle.get("sourceFormat", "hlsl")
    inputs = root / "source-inputs"
    if inputs.is_symlink() or (inputs.exists() and not inputs.is_dir()):
        raise ReplayProjectError("project source input directory is redirected")
    inputs.mkdir(exist_ok=True)
    input_path = inputs / ("project-%s.%s" % (bundle["sourceSha256"], language))
    input_existed = input_path.exists()
    if input_existed or input_path.is_symlink():
        if _file_bytes(input_path, MAX_SOURCE_BYTES, "project source immutable input") != source_bytes:
            raise ReplayProjectError("project source immutable input changed")
    receipt = source_receipt(bundle, input_path.relative_to(root).as_posix())
    updated = {
        "path": destination.relative_to(root).as_posix(), "sha256": bundle["sourceSha256"],
        "entryPoint": bundle["sourceEntryPoint"], "encoding": "utf-8",
        "lane": "executable-editable", "origin": "verified-project-source",
        "compileEligible": True, "semanticEquivalenceVerified": False,
        "baselineRawSha256": manifest["baseline"]["rawSha256"], "projectSource": receipt,
    }
    writing = False
    try:
        if not input_existed:
            atomic_write_bytes(input_path, source_bytes)
        validate_project_source(root, manifest, updated)
        # Include the provenance CAS in the same operation as the source CAS.
        if manifest_path.read_bytes() != before_bytes or destination.read_bytes() != current_bytes:
            raise ReplayProjectError("destination source or manifest changed before seed")
        writing = True
        atomic_write_bytes(destination, source_bytes)
        manifest["source"] = updated
        manifest["updatedAt"] = _utc_now()
        atomic_write_json(manifest_path, manifest)
    except Exception:
        # Do not roll back a concurrent writer if the CAS failed before writes.
        if writing:
            atomic_write_bytes(destination, current_bytes)
            atomic_write_bytes(manifest_path, before_bytes)
        if not input_existed:
            input_path.unlink(missing_ok=True)
        raise


@contextmanager
def _workspace_seed_lock(root: Path) -> Iterator[None]:
    """Participate in the workspace service's cross-process operation lock.

    Seed/admission uses the same OS ownership and crash recovery as compile;
    a separate token implementation would strand a recovered workspace here.
    """
    from .shader_workspace.errors import ManifestValidationError
    from .shader_workspace.workspace import _WorkspaceFileLock
    try:
        with _WorkspaceFileLock(root):
            yield
    except ManifestValidationError as error:
        raise ReplayProjectError(str(error)) from error


def _compact_shader(shader: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(shader.get("rawSha256", "") or "").lower()
    code = str(shader.get("codeSha256", "") or "").lower()
    raw_verified = shader.get("_rawHashVerified") is True
    code_verified = shader.get("_codeHashVerified") is True
    return {
        "resourceId": _positive(shader.get("shaderResourceId"), "shader.resourceId"),
        "stage": _text(str(shader.get("stage", "")), "shader.stage", 64),
        "entryPoint": _text(
            str(shader.get("entryPoint", "") or ""),
            "shader.entryPoint",
            512,
            allow_empty=True,
        ),
        "rawSha256": _sha(raw, "shader.rawSha256") if raw_verified else "",
        "codeSha256": _sha(code, "shader.codeSha256") if code_verified else "",
        "layoutHash": _sha(shader.get("layoutHash"), "shader.layoutHash", True),
        "interfaceHash": _sha(
            shader.get("interfaceHash"), "shader.interfaceHash", True
        ),
        "interfaceComplete": shader.get("interfaceComplete") is True,
        "encoding": _text(
            str(shader.get("encoding", "") or ""),
            "shader.encoding",
            64,
            allow_empty=True,
        ),
        "eventIds": sorted(
            {
                int(use.get("eventId"))
                for use in list(shader.get("uses", []) or [])
                if isinstance(use, Mapping)
                and not isinstance(use.get("eventId"), bool)
                and isinstance(use.get("eventId"), int)
                and int(use.get("eventId")) > 0
            }
        )[:4096],
    }


def _shader_identity(value: Mapping[str, Any]) -> tuple[int, str, str, str]:
    return (
        int(value.get("resourceId", 0)),
        str(value.get("stage", "")),
        str(value.get("entryPoint", "")),
        str(value.get("rawSha256", "")),
    )


def _recover_legacy_alias_rows(
    rows: Sequence[Any], capture: Mapping[str, Any]
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Resolve omitted legacy IDs from this already verified capture catalog.

    Names and physical coordinates are not matching evidence. Only one exact
    original bytecode/stage/entry-point match can repair a missing ID. Explicit
    IDs and provenance are left to the normal validators; a conflicting ID is
    never silently rebound. Unresolvable legacy rows stay in the source artifact
    and are omitted from this derived view with an actionable diagnostic.
    """

    by_program: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for shader in capture.get("shaders", []):
        key = (str(shader.get("rawSha256") or ""),
               str(shader.get("stage") or ""), str(shader.get("entryPoint") or ""))
        by_program.setdefault(key, []).append(shader)
    recovered: list[Any] = []
    diagnostics: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if (not isinstance(row, Mapping)
                or row.get("verified") is not True
                or row.get("captureId") != capture.get("captureId")
                or row.get("shaderResourceId") not in (None, "")):
            recovered.append(row)
            continue
        raw = str(row.get("rawSha256") or "").strip().lower()
        stage = str(row.get("stage") or "").strip()
        entry = str(row.get("entryPoint") or "").strip()
        candidates = by_program.get((raw, stage, entry), []) if SHA256_RE.fullmatch(raw) else []
        diagnostic: dict[str, Any] = {
            "rowIndex": index,
            "status": "skipped",
            "reasonId": "legacy-alias-shader-identity-unresolved",
            "candidateCount": len(candidates),
            "sourcePreserved": True,
        }
        if len(candidates) == 1:
            resource_id = _positive(candidates[0].get("resourceId"), "shader.resourceId")
            recovered.append({**row, "shaderResourceId": resource_id})
            diagnostic.update({
                "status": "repaired",
                "reasonId": "legacy-alias-shader-resource-id-recovered",
                "shaderResourceId": resource_id,
                "matchedBy": "original-bytecode-stage-entry-point",
            })
        diagnostics.append(diagnostic)
    return recovered, diagnostics


def _match_mode(knowledge: Mapping[str, Any], shader: Mapping[str, Any]) -> str:
    if (
        knowledge.get("stage") != shader.get("stage")
        or knowledge.get("entryPoint", "") != shader.get("entryPoint", "")
    ):
        return ""
    raw = str(knowledge.get("rawSha256", "") or "")
    if raw and raw == shader.get("rawSha256"):
        return "raw-exact"
    if (knowledge.get("compiledCandidate") or {}).get("validationScope") == "manual-edit-unvalidated":
        # A manual edit is associated only with the original captured bytes;
        # it carries no interface-validation authority for another program.
        return ""
    code = str(knowledge.get("codeSha256", "") or "")
    if not code or code != shader.get("codeSha256"):
        return ""
    if not (
        knowledge.get("interfaceComplete") is True
        and shader.get("interfaceComplete") is True
        and knowledge.get("interfaceHash")
        and knowledge.get("interfaceHash") == shader.get("interfaceHash")
        # A complete interface proves that an empty CB list is known-empty,
        # rather than missing reflection. Its canonical layout hash is empty.
        and knowledge.get("layoutHash", "") == shader.get("layoutHash", "")
    ):
        return ""
    return "code-interface-layout-exact"


def _normalised_catalog_evidence(
    catalog_path: str | Path,
) -> tuple[Path, dict[str, Any], bytes, dict[str, bytes]]:
    """Return one deterministic, self-contained catalog revision payload."""

    raw_source = Path(catalog_path).expanduser()
    if raw_source.exists() and raw_source.is_symlink():
        raise ReplayProjectError("capture catalog must not be a symlink")
    source = raw_source.resolve()
    before = _file_bytes(source, MAX_CAPTURE_CATALOG_BYTES, "capture catalog")
    try:
        normalised_path, catalog = _normalise_catalog(source)
    except Exception as exc:
        raise ReplayProjectError(
            "capture catalog validation failed: %s" % exc
        ) from exc
    after = _file_bytes(source, MAX_CAPTURE_CATALOG_BYTES, "capture catalog")
    if before != after or normalised_path != source:
        raise ReplayProjectError("capture catalog changed while it was imported")

    total_blob_bytes = 0
    blobs: dict[str, bytes] = {}
    for shader in catalog["shaders"]:
        raw_path = str(shader.get("rawPath", "") or "")
        if not raw_path:
            continue
        blob = _file_bytes(
            Path(raw_path), MAX_CAPTURE_CATALOG_BYTES, "capture shader blob"
        )
        total_blob_bytes += len(blob)
        if total_blob_bytes > MAX_CAPTURE_SHADER_BYTES:
            raise ReplayProjectError(
                "capture shader blobs exceed the project revision limit"
            )
        digest = _sha256_bytes(blob)
        if digest != shader.get("rawSha256"):
            raise ReplayProjectError(
                "capture shader blob changed after catalog validation"
            )
        blobs.setdefault(digest, blob)
        shader["rawPath"] = "blobs/%s.bin" % digest

    payload = (
        json.dumps(
            catalog,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if not payload or len(payload) > MAX_CAPTURE_CATALOG_BYTES:
        raise ReplayProjectError(
            "normalised capture catalog exceeds the project limit"
        )
    return source, catalog, payload, blobs


class ReplayProjectService:
    def __init__(self, project_path: str | Path, *, cache_validated_reads: bool = False):
        self.root = _normal_path(project_path)
        from .project_read_cache import ProjectReadCache
        self._validated_read = ProjectReadCache()
        self._cache_validated_reads = cache_validated_reads

    @property
    def manifest_path(self) -> Path:
        return self.root / "project.json"

    @classmethod
    def create(
        cls,
        project_path: str | Path,
        name: str = "",
        game_id: str = "",
        build_id: str = "",
        platform: str = "",
        api_family: str = "",
    ) -> dict[str, Any]:
        root = _normal_path(project_path)
        if root.exists():
            raise ReplayProjectError("project path already exists: %s" % root)
        root.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".%s.staging-" % root.name, dir=str(root.parent)))
        now = _utc_now()
        manifest = {
            "schema": PROJECT_SCHEMA,
            "schemaVersion": PROJECT_VERSION,
            "projectId": uuid.uuid4().hex,
            "name": _text(str(name or ""), "name", 256, allow_empty=True),
            "scope": {
                "gameId": _text(str(game_id or ""), "game_id", 256),
                "buildId": _text(str(build_id or ""), "build_id", 256),
                "platform": _text(
                    str(platform or ""), "platform", 128, allow_empty=True
                ),
                "apiFamily": _api_family(api_family, allow_empty=True),
            },
            "createdAt": now,
            "updatedAt": now,
            "captures": [],
            "knowledge": {"workspaces": [], "aliases": [], "materials": []},
            "activeKnowledge": {
                "revision": 0,
                "workspaces": {},
                "materials": {},
            },
            "activeHeads": {
                "workspaces": {},
                "materials": {},
                "aliases": {},
            },
        }
        try:
            (staging / "captures" / "catalogs").mkdir(parents=True)
            (staging / "knowledge" / "workspaces").mkdir(parents=True)
            (staging / "knowledge" / "aliases").mkdir(parents=True)
            atomic_write_json(staging / "project.json", manifest)
            staging.replace(root)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return cls(root).status()

    @staticmethod
    def _active_knowledge_state(manifest: dict[str, Any]) -> dict[str, Any]:
        """Return the validated persistent activation state.

        Early v1 projects did not have an explicit active-head index.  For
        those manifests only, derive the exact ordering they previously used
        from the immutable knowledge arrays.  The derived state is attached to
        the in-memory manifest and is persisted by the next ordinary project
        mutation.  Once the field exists it is strict: partial/tampered state
        is never silently repaired.
        """

        knowledge = manifest.get("knowledge")
        if not isinstance(knowledge, Mapping):
            raise ReplayProjectError("project knowledge index is invalid")

        ids: dict[str, list[str]] = {}
        for kind in ("workspaces", "materials"):
            records = knowledge.get(kind)
            if not isinstance(records, list):
                raise ReplayProjectError("project %s knowledge is invalid" % kind)
            ordered: list[str] = []
            seen: set[str] = set()
            for index, record in enumerate(records):
                if not isinstance(record, Mapping):
                    raise ReplayProjectError(
                        "project %s knowledge[%d] is invalid" % (kind, index)
                    )
                token = str(record.get("knowledgeId", "") or "")
                if not HEX32_RE.fullmatch(token) or token in seen:
                    raise ReplayProjectError(
                        "project %s knowledgeId is invalid or duplicated" % kind
                    )
                seen.add(token)
                ordered.append(token)
            ids[kind] = ordered

        if "activeKnowledge" not in manifest:
            revision = 0
            derived: dict[str, Any] = {
                "revision": 0,
                "workspaces": {},
                "materials": {},
            }
            for kind in ("workspaces", "materials"):
                for token in ids[kind]:
                    revision += 1
                    derived[kind][token] = revision
            derived["revision"] = revision
            manifest["activeKnowledge"] = derived
            return derived

        active = manifest.get("activeKnowledge")
        if not isinstance(active, dict) or set(active) != {
            "revision",
            "workspaces",
            "materials",
        }:
            raise ReplayProjectError("project activeKnowledge index is invalid")
        revision = active.get("revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
            or revision > MAX_ACTIVE_KNOWLEDGE_REVISION
        ):
            raise ReplayProjectError("project activeKnowledge revision is invalid")

        serials: set[int] = set()
        for kind in ("workspaces", "materials"):
            activations = active.get(kind)
            if not isinstance(activations, dict) or set(activations) != set(ids[kind]):
                raise ReplayProjectError(
                    "project activeKnowledge %s index is incomplete" % kind
                )
            for token, serial in activations.items():
                if (
                    not HEX32_RE.fullmatch(str(token))
                    or isinstance(serial, bool)
                    or not isinstance(serial, int)
                    or serial <= 0
                    or serial > revision
                    or serial in serials
                ):
                    raise ReplayProjectError(
                        "project activeKnowledge activation is invalid"
                    )
                serials.add(serial)
        if revision != (max(serials) if serials else 0):
            raise ReplayProjectError(
                "project activeKnowledge revision does not name its newest activation"
            )
        return active

    @classmethod
    def _head_shader_identity(
        cls,
        value: Mapping[str, Any],
        *,
        baseline_abi: bool = False,
        manifest: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return only physical Shader fields used to partition active heads."""

        inventory_origin = value.get("inventoryOrigin")
        if inventory_origin is not None:
            origin = _inventory_origin_identity(inventory_origin)
            api = origin["api"]
            encoding = origin["encoding"]
            abi_hash = origin["abiHash"]
        else:
            api_value = str(value.get("api", "") or "")
            encoding_value = str(value.get("encoding", "") or "")
            api = _api_family(api_value) if api_value else ""
            encoding = _shader_encoding(encoding_value) if encoding_value else ""
            abi_hash = ""
            origin_record = provenance if provenance is not None else value
            if (not api or not encoding) and manifest is not None:
                capture_id = _sha(
                    origin_record.get("captureId"), "head.captureId"
                )
                catalog_sha256 = _sha(
                    origin_record.get("originCatalogSha256"),
                    "head.originCatalogSha256",
                )
                captures = manifest.get("captures")
                if not isinstance(captures, list):
                    raise ReplayProjectError("project captures are invalid")
                capture_matches = [
                    capture
                    for capture in captures
                    if isinstance(capture, Mapping)
                    and capture.get("captureId") == capture_id
                ]
                if len(capture_matches) != 1:
                    raise ReplayProjectError(
                        "active head origin capture is absent or ambiguous"
                    )
                capture = capture_matches[0]
                revisions = capture.get("catalogRevisions")
                if isinstance(revisions, list):
                    revision_matches = [
                        revision
                        for revision in revisions
                        if isinstance(revision, Mapping)
                        and revision.get("catalogSha256") == catalog_sha256
                    ]
                    if len(revision_matches) != 1:
                        raise ReplayProjectError(
                            "active head origin catalog revision is absent"
                        )
                    shaders = revision_matches[0].get("shaders")
                else:
                    if capture.get("catalogSha256") != catalog_sha256:
                        raise ReplayProjectError(
                            "active head origin catalog revision is absent"
                        )
                    shaders = capture.get("shaders")
                if not isinstance(shaders, list):
                    raise ReplayProjectError(
                        "active head origin Shader index is invalid"
                    )
                stage = _normalised_stage(value.get("stage"))
                entry_point = _text(
                    str(value.get("entryPoint", "") or ""),
                    "head.entryPoint",
                    512,
                    allow_empty=True,
                )
                raw_sha256 = _sha(
                    value.get("rawSha256"), "head.rawSha256"
                )
                resource_id = origin_record.get("originResourceId")
                shader_matches = [
                    shader
                    for shader in shaders
                    if isinstance(shader, Mapping)
                    and _normalised_stage(shader.get("stage")) == stage
                    and str(shader.get("entryPoint", "") or "") == entry_point
                    and shader.get("rawSha256") == raw_sha256
                    and (
                        resource_id is None
                        or shader.get("resourceId") == resource_id
                    )
                ]
                encodings = {
                    _shader_encoding(shader.get("encoding"))
                    for shader in shader_matches
                }
                if not shader_matches or len(encodings) != 1:
                    raise ReplayProjectError(
                        "active head origin Shader encoding is absent or ambiguous"
                    )
                api = _api_family(capture.get("api"))
                encoding = next(iter(encodings))
                abi_values = {
                    _sha(
                        shader.get("abiHash"),
                        "head.abiHash",
                        allow_empty=True,
                    )
                    for shader in shader_matches
                }
                if len(abi_values) == 1:
                    abi_hash = next(iter(abi_values))
            if not api or not encoding:
                raise ReplayProjectError(
                    "active head physical API/encoding identity is unavailable"
                )
        identity = {
            "api": api,
            "encoding": encoding,
            "stage": _normalised_stage(value.get("stage")),
            "entryPoint": _text(
                str(value.get("entryPoint", "") or ""),
                "head.entryPoint",
                512,
                allow_empty=True,
            ),
            "rawSha256": _sha(value.get("rawSha256"), "head.rawSha256"),
            "codeSha256": _sha(
                value.get("codeSha256"), "head.codeSha256", allow_empty=True
            ),
            "layoutHash": _sha(
                value.get("layoutHash"), "head.layoutHash", allow_empty=True
            ),
            "interfaceHash": _sha(
                value.get("interfaceHash"),
                "head.interfaceHash",
                allow_empty=True,
            ),
            "interfaceComplete": value.get("interfaceComplete") is True,
            "abiHash": (
                _sha(
                    value.get("baselineAbiSha256"),
                    "head.baselineAbiSha256",
                    allow_empty=True,
                )
                if baseline_abi
                else abi_hash
            ),
        }
        return identity

    @classmethod
    def _legacy_workspace_head_key(
        cls,
        record: Mapping[str, Any],
        manifest: Mapping[str, Any] | None = None,
    ) -> str:
        profile = record.get("targetProfile")
        if not isinstance(profile, Mapping):
            raise ReplayProjectError("workspace head targetProfile is invalid")
        physical_identity = cls._head_shader_identity(
            record, baseline_abi=True, manifest=manifest
        )
        return _canonical_sha256(
            {
                "schema": "renderdoc-ultra-project-workspace-head-key/v1",
                "physicalIdentity": physical_identity,
                "physicalIdentitySha256": _canonical_sha256(
                    physical_identity
                ),
                "stableKeySha256": _stable_shader_identity_sha256(
                    {
                        key: physical_identity[key]
                        for key in (
                            "api",
                            "encoding",
                            "rawSha256",
                            "stage",
                            "entryPoint",
                            "abiHash",
                            "layoutHash",
                        )
                    }
                ),
                "targetProfile": copy.deepcopy(dict(profile)),
                "sourceLane": _workspace_source_format(record),
            }
        )

    @classmethod
    def _workspace_head_key(
        cls, record: Mapping[str, Any], manifest: Mapping[str, Any] | None = None,
    ) -> str:
        # Source language, compiler versions and reflected names describe a
        # representation, not another original program. Actual replay admission
        # still checks the target profile/layout in the destination capture.
        identity = cls._head_shader_identity(record, baseline_abi=True, manifest=manifest)
        return _canonical_sha256({
            "schema": "renderdoc-ultra-project-workspace-head-key/v2",
            **{key: identity[key] for key in
               ("api", "encoding", "rawSha256", "stage", "entryPoint")},
        })

    @classmethod
    def _material_head_key(
        cls,
        record: Mapping[str, Any],
        manifest: Mapping[str, Any] | None = None,
    ) -> str:
        physical_identity = cls._head_shader_identity(
            record, manifest=manifest
        )
        return _canonical_sha256(
            {
                "schema": "renderdoc-ultra-project-material-head-key/v1",
                "physicalIdentity": physical_identity,
                "stableKeySha256": _stable_shader_identity_sha256(
                    {
                        key: physical_identity[key]
                        for key in (
                            "api",
                            "encoding",
                            "rawSha256",
                            "stage",
                            "entryPoint",
                            "abiHash",
                            "layoutHash",
                        )
                    }
                ),
                "constantBlockPath": _text(
                    str(record.get("constantBlockPath", "")),
                    "material head constantBlockPath",
                    2048,
                ),
                "constantBlockLayoutSha256": _sha(
                    record.get("constantBlockLayoutSha256"),
                    "material head constantBlockLayoutSha256",
                ),
                "layer": "values",
            }
        )

    @classmethod
    def _alias_head_key(
        cls,
        group: Mapping[str, Any],
        manifest: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> str:
        # Alias names are deliberately absent. One verified alias publication
        # supersedes another for the same physical Shader/group layout without
        # conflating it with material values or HLSL source authority.
        physical_identity = cls._head_shader_identity(
            group, manifest=manifest, provenance=provenance
        )
        return _canonical_sha256(
            {
                "schema": "renderdoc-ultra-project-alias-head-key/v1",
                "physicalIdentity": physical_identity,
                "stableKeySha256": _stable_shader_identity_sha256(
                    {
                        key: physical_identity[key]
                        for key in (
                            "api",
                            "encoding",
                            "rawSha256",
                            "stage",
                            "entryPoint",
                            "abiHash",
                            "layoutHash",
                        )
                    }
                ),
                "groupLayout": {
                    "layoutHash": _sha(
                        group.get("layoutHash"),
                        "alias head layoutHash",
                        allow_empty=True,
                    ),
                    "interfaceHash": _sha(
                        group.get("interfaceHash"),
                        "alias head interfaceHash",
                        allow_empty=True,
                    ),
                    "interfaceComplete": group.get("interfaceComplete") is True,
                },
                "layer": "display-aliases",
            }
        )

    @classmethod
    def _knowledge_head_members(
        cls, manifest: Mapping[str, Any], *, legacy_workspaces: bool = False,
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        knowledge = manifest.get("knowledge")
        if not isinstance(knowledge, Mapping):
            raise ReplayProjectError("project knowledge index is invalid")
        result: dict[str, dict[str, list[dict[str, Any]]]] = {
            "workspaces": {},
            "materials": {},
            "aliases": {},
        }
        for kind, key_builder in (
            ("workspaces", cls._legacy_workspace_head_key if legacy_workspaces else cls._workspace_head_key),
            ("materials", cls._material_head_key),
        ):
            records = knowledge.get(kind)
            if not isinstance(records, list):
                raise ReplayProjectError("project %s knowledge is invalid" % kind)
            for ordinal, record in enumerate(records):
                if not isinstance(record, Mapping):
                    raise ReplayProjectError(
                        "project %s knowledge record is invalid" % kind
                    )
                knowledge_id = str(record.get("knowledgeId", "") or "")
                if not HEX32_RE.fullmatch(knowledge_id):
                    raise ReplayProjectError("project knowledgeId is invalid")
                head_key = key_builder(record, manifest)
                result[kind].setdefault(head_key, []).append(
                    {
                        "knowledgeId": knowledge_id,
                        "ordinal": (ordinal, 0),
                        "publishedAt": str(
                            record.get("registeredAt")
                            or manifest.get("updatedAt")
                            or manifest.get("createdAt")
                            or "1970-01-01T00:00:00Z"
                        ),
                    }
                )

        aliases = knowledge.get("aliases")
        if not isinstance(aliases, list):
            raise ReplayProjectError("project aliases knowledge is invalid")
        for record_ordinal, record in enumerate(aliases):
            if not isinstance(record, Mapping):
                raise ReplayProjectError("project alias knowledge record is invalid")
            knowledge_id = str(record.get("knowledgeId", "") or "")
            if not HEX32_RE.fullmatch(knowledge_id):
                raise ReplayProjectError("project alias knowledgeId is invalid")
            groups = record.get("groups")
            if not isinstance(groups, list):
                raise ReplayProjectError("project alias knowledge groups are invalid")
            seen_record_heads: set[str] = set()
            for group_ordinal, group in enumerate(groups):
                if not isinstance(group, Mapping):
                    raise ReplayProjectError("project alias knowledge group is invalid")
                head_key = cls._alias_head_key(
                    group, manifest=manifest, provenance=record
                )
                if head_key in seen_record_heads:
                    raise ReplayProjectError(
                        "alias knowledge contains duplicate physical head groups"
                    )
                seen_record_heads.add(head_key)
                result["aliases"].setdefault(head_key, []).append(
                    {
                        "knowledgeId": knowledge_id,
                        "ordinal": (record_ordinal, group_ordinal),
                        "groupIndex": group_ordinal,
                        "publishedAt": str(
                            record.get("registeredAt")
                            or manifest.get("updatedAt")
                            or manifest.get("createdAt")
                            or "1970-01-01T00:00:00Z"
                        ),
                    }
                )
        return result

    @classmethod
    def _active_heads_state(cls, manifest: dict[str, Any]) -> dict[str, Any]:
        """Validate or migrate the per-identity authoritative head index."""

        members = cls._knowledge_head_members(manifest)
        if "activeHeads" not in manifest:
            legacy = cls._active_knowledge_state(manifest)
            derived: dict[str, dict[str, dict[str, Any]]] = {
                "workspaces": {},
                "materials": {},
                "aliases": {},
            }
            for kind in ("workspaces", "materials", "aliases"):
                for head_key, candidates in members[kind].items():
                    if kind == "aliases":
                        selected = max(candidates, key=lambda item: item["ordinal"])
                    else:
                        selected = max(
                            candidates,
                            key=lambda item: (
                                legacy[kind][item["knowledgeId"]],
                                item["ordinal"],
                            ),
                        )
                    derived[kind][head_key] = {
                        "generation": max(
                            1,
                            len(
                                {
                                    item["knowledgeId"]
                                    for item in candidates
                                }
                            ),
                        ),
                        "knowledgeId": selected["knowledgeId"],
                        "publishedAt": _text(
                            selected["publishedAt"],
                            "active head publishedAt",
                            128,
                        ),
                    }
            manifest["activeHeads"] = derived
            return derived

        active_heads = manifest.get("activeHeads")
        if not isinstance(active_heads, dict) or set(active_heads) != {
            "workspaces",
            "materials",
            "aliases",
        }:
            raise ReplayProjectError("project activeHeads index is invalid")
        legacy_members = None
        if not isinstance(active_heads.get("workspaces"), dict):
            raise ReplayProjectError("project activeHeads workspaces index is invalid")
        if set(active_heads["workspaces"]) != set(members["workspaces"]):
            # Authenticate the old index using its original algorithm before
            # merging lanes. Never repair an incomplete/corrupt index by taking
            # the last arbitrary history record.
            legacy_members = cls._knowledge_head_members(manifest, legacy_workspaces=True)
        validation_members = legacy_members or members
        for kind in ("workspaces", "materials", "aliases"):
            heads = active_heads.get(kind)
            if not isinstance(heads, dict) or set(heads) != set(validation_members[kind]):
                raise ReplayProjectError(
                    "project activeHeads %s index is incomplete" % kind
                )
            for head_key, head in heads.items():
                if not SHA256_RE.fullmatch(str(head_key)):
                    raise ReplayProjectError("project active head key is invalid")
                if not isinstance(head, dict) or set(head) != {
                    "generation",
                    "knowledgeId",
                    "publishedAt",
                }:
                    raise ReplayProjectError("project active head record is invalid")
                generation = head.get("generation")
                knowledge_id = str(head.get("knowledgeId", "") or "")
                if (
                    isinstance(generation, bool)
                    or not isinstance(generation, int)
                    or generation <= 0
                    or generation > MAX_ACTIVE_KNOWLEDGE_REVISION
                    or not HEX32_RE.fullmatch(knowledge_id)
                    or knowledge_id
                    not in {
                        item["knowledgeId"] for item in validation_members[kind][head_key]
                    }
                ):
                    raise ReplayProjectError("project active head binding is invalid")
                _text(head.get("publishedAt"), "active head publishedAt", 128)
        if legacy_members is not None:
            activation = cls._active_knowledge_state(manifest)["workspaces"]
            current_by_id = {head["knowledgeId"]: head
                             for head in active_heads["workspaces"].values()}
            migrated = {}
            for key, candidates in members["workspaces"].items():
                active_ids = [item["knowledgeId"] for item in candidates
                              if item["knowledgeId"] in current_by_id]
                selected = max(active_ids, key=lambda token: activation[token])
                migrated[key] = {**current_by_id[selected], "generation": max(
                    current_by_id[token]["generation"] for token in active_ids)}
            active_heads["workspaces"] = migrated
        return active_heads

    @staticmethod
    def _expected_head_token(
        expected: Mapping[str, Any] | None, head_key: str
    ) -> dict[str, Any] | None:
        if expected is None:
            return None
        if not isinstance(expected, Mapping):
            raise ReplayProjectError("expected_head must be a head receipt")
        expected_key = str(expected.get("headKey", "") or "")
        generation = expected.get("generation")
        knowledge_id = str(expected.get("knowledgeId", "") or "")
        if (
            expected_key != head_key
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 0
            or generation > MAX_ACTIVE_KNOWLEDGE_REVISION
            or (generation == 0 and knowledge_id != "")
            or (generation > 0 and not HEX32_RE.fullmatch(knowledge_id))
        ):
            raise ReplayProjectError("expected_head receipt is invalid")
        return {
            "headKey": head_key,
            "generation": generation,
            "knowledgeId": knowledge_id,
        }

    @classmethod
    def _expected_heads_for_keys(
        cls,
        expected: Mapping[str, Any] | None,
        head_keys: Sequence[str],
    ) -> dict[str, Mapping[str, Any] | None]:
        ordered_keys = tuple(sorted(set(head_keys)))
        if len(ordered_keys) != len(head_keys):
            raise ReplayProjectError("publication contains duplicate head keys")
        if expected is None:
            return {head_key: None for head_key in ordered_keys}
        if not isinstance(expected, Mapping):
            raise ReplayProjectError("expected_head must be a head receipt")
        if "headKey" in expected:
            if len(ordered_keys) != 1:
                raise ReplayProjectError(
                    "multi-head publication requires expected_head.heads"
                )
            head_key = ordered_keys[0]
            return {head_key: cls._expected_head_token(expected, head_key)}
        raw_heads = expected.get("heads")
        if not isinstance(raw_heads, list) or len(raw_heads) != len(ordered_keys):
            raise ReplayProjectError("expected_head.heads is invalid")
        result: dict[str, Mapping[str, Any] | None] = {}
        for item in raw_heads:
            if not isinstance(item, Mapping):
                raise ReplayProjectError("expected_head.heads is invalid")
            head_key = str(item.get("headKey", "") or "")
            if head_key in result or head_key not in ordered_keys:
                raise ReplayProjectError("expected_head.heads is invalid")
            result[head_key] = cls._expected_head_token(item, head_key)
        if set(result) != set(ordered_keys):
            raise ReplayProjectError("expected_head.heads is incomplete")
        return result

    @classmethod
    def _plan_head_publication(
        cls,
        manifest: dict[str, Any],
        kind: str,
        head_key: str,
        knowledge_id: str,
        *,
        record_preexisting: bool,
        expected_head: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if kind not in {"workspaces", "materials", "aliases"}:
            raise ReplayProjectError("active head kind is invalid")
        if not SHA256_RE.fullmatch(str(head_key)) or not HEX32_RE.fullmatch(
            str(knowledge_id)
        ):
            raise ReplayProjectError("active head publication identity is invalid")
        heads = cls._active_heads_state(manifest)[kind]
        current = heads.get(head_key)
        expected = cls._expected_head_token(expected_head, head_key)
        if expected is not None:
            current_token = (
                {
                    "headKey": head_key,
                    "generation": current["generation"],
                    "knowledgeId": current["knowledgeId"],
                }
                if current is not None
                else {"headKey": head_key, "generation": 0, "knowledgeId": ""}
            )
            if current_token != expected:
                raise ReplayProjectError(
                    "active head compare-and-swap conflict"
                )
        if current is not None and current["knowledgeId"] == knowledge_id:
            return {
                "headKey": head_key,
                **copy.deepcopy(dict(current)),
                "changed": False,
            }
        if record_preexisting and expected is None:
            raise ReplayProjectError(
                "historical knowledge cannot replace the active head without expected_head"
            )
        generation = 1 if current is None else current["generation"] + 1
        if generation > MAX_ACTIVE_KNOWLEDGE_REVISION:
            raise ReplayProjectError("active head generation is exhausted")
        return {
            "headKey": head_key,
            "generation": generation,
            "knowledgeId": knowledge_id,
            "publishedAt": _utc_now(),
            "changed": True,
        }

    @classmethod
    def _apply_head_publication(
        cls, manifest: dict[str, Any], kind: str, receipt: Mapping[str, Any]
    ) -> None:
        if receipt.get("changed") is not True:
            return
        active_heads = manifest.get("activeHeads")
        if not isinstance(active_heads, dict) or not isinstance(
            active_heads.get(kind), dict
        ):
            raise ReplayProjectError("project activeHeads index is invalid")
        active_heads[kind][receipt["headKey"]] = {
            "generation": receipt["generation"],
            "knowledgeId": receipt["knowledgeId"],
            "publishedAt": receipt["publishedAt"],
        }

    @staticmethod
    def _head_receipt(kind: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema": ACTIVE_HEAD_RECEIPT_SCHEMA,
            "schemaVersion": ACTIVE_HEAD_RECEIPT_VERSION,
            "kind": kind,
            "headKey": receipt["headKey"],
            "generation": receipt["generation"],
            "knowledgeId": receipt["knowledgeId"],
            "publishedAt": receipt["publishedAt"],
            "changed": receipt.get("changed") is True,
        }

    @classmethod
    def _activate_knowledge(
        cls, manifest: dict[str, Any], kind: str, knowledge_id: str
    ) -> int:
        if kind not in {"workspaces", "materials"}:
            raise ReplayProjectError("active knowledge kind is invalid")
        token = str(knowledge_id or "")
        if not HEX32_RE.fullmatch(token):
            raise ReplayProjectError("active knowledgeId is invalid")
        active = manifest.get("activeKnowledge")
        if not isinstance(active, dict) or kind not in active:
            raise ReplayProjectError("project activeKnowledge index is invalid")
        record_ids = {
            str(record.get("knowledgeId", "") or "")
            for record in manifest["knowledge"][kind]
            if isinstance(record, Mapping)
        }
        if token not in record_ids:
            raise ReplayProjectError("active knowledgeId is absent")
        revision = active.get("revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
            or revision >= MAX_ACTIVE_KNOWLEDGE_REVISION
        ):
            raise ReplayProjectError("project activeKnowledge revision is exhausted")
        revision += 1
        active["revision"] = revision
        active[kind][token] = revision
        # This validates both the previously loaded state and the one allowed
        # new immutable record after its activation has been inserted.
        cls._active_knowledge_state(manifest)
        return revision

    def _load(
        self,
        *,
        alias_artifact_cache: dict[str, bytes] | None = None,
        alias_artifact_knowledge_ids: set[str] | None = None,
        alias_artifact_cache_max_bytes: int = MAX_ALIAS_BYTES,
        persist_migrations: bool = False,
    ) -> dict[str, Any]:
        # Materialization explicitly requests artifact bytes, so it retains
        # the ordinary reader. Cached manifests are defensive copies and all
        # consumed file change identities are checked on every reuse.
        def load():
            return self._load_uncached(alias_artifact_cache=alias_artifact_cache,
                alias_artifact_knowledge_ids=alias_artifact_knowledge_ids,
                alias_artifact_cache_max_bytes=alias_artifact_cache_max_bytes,
                persist_migrations=persist_migrations)
        if (not self._cache_validated_reads or alias_artifact_cache is not None
                or self.root.is_symlink() or not self.root.is_dir()):
            return load()
        return self._validated_read.load(self.manifest_path, load, key=persist_migrations)

    def _load_uncached(
        self,
        *,
        alias_artifact_cache: dict[str, bytes] | None = None,
        alias_artifact_knowledge_ids: set[str] | None = None,
        alias_artifact_cache_max_bytes: int = MAX_ALIAS_BYTES,
        persist_migrations: bool = False,
    ) -> dict[str, Any]:
        """Load and validate one project manifest snapshot.

        Alias materialization may request the already-validated bytes for a
        bounded set of alias knowledge records.  This avoids reading the same
        immutable artifact again while constructing a cross-RDC restore
        batch.  Ordinary callers use the historical no-argument path.
        """

        requested_alias_artifacts = set(alias_artifact_knowledge_ids or ())
        if alias_artifact_cache is not None:
            if not isinstance(alias_artifact_cache, dict):
                raise ReplayProjectError("alias artifact cache must be a dictionary")
            if alias_artifact_cache:
                raise ReplayProjectError("alias artifact cache must start empty")
            if len(requested_alias_artifacts) > MAX_KNOWLEDGE_COUNT:
                raise ReplayProjectError(
                    "alias artifact cache request exceeds the knowledge limit"
                )
            if (
                isinstance(alias_artifact_cache_max_bytes, bool)
                or not isinstance(alias_artifact_cache_max_bytes, int)
                or alias_artifact_cache_max_bytes <= 0
                or alias_artifact_cache_max_bytes > MAX_ALIAS_BYTES
            ):
                raise ReplayProjectError("alias artifact cache byte limit is invalid")
        cached_alias_artifacts: dict[tuple[str, str], bytes] = {}
        cached_alias_artifact_bytes = 0
        if self.root.is_symlink() or not self.root.is_dir():
            raise ReplayProjectError("project path must be a regular directory")
        try:
            manifest = read_json_object(self.manifest_path, MAX_PROJECT_BYTES, "project manifest")
        except ValueError as exc:
            raise ReplayProjectError(str(exc)) from exc
        migration_required = (
            "activeKnowledge" not in manifest or "activeHeads" not in manifest
        )
        if manifest.get("schema") != PROJECT_SCHEMA or manifest.get("schemaVersion") != PROJECT_VERSION:
            raise ReplayProjectError("unsupported project manifest schema/version")
        if not HEX32_RE.fullmatch(str(manifest.get("projectId", ""))):
            raise ReplayProjectError("projectId is invalid")
        scope = manifest.get("scope")
        if not isinstance(scope, dict) or set(scope) != {
            "gameId",
            "buildId",
            "platform",
            "apiFamily",
        }:
            raise ReplayProjectError("project scope is invalid")
        _text(scope.get("gameId"), "scope.gameId", 256)
        _text(scope.get("buildId"), "scope.buildId", 256)
        _text(scope.get("platform"), "scope.platform", 128, allow_empty=True)
        if _api_family(scope.get("apiFamily"), allow_empty=True) != scope.get(
            "apiFamily"
        ):
            raise ReplayProjectError("project apiFamily is not canonical")
        captures = manifest.get("captures")
        knowledge = manifest.get("knowledge")
        if not isinstance(captures, list) or len(captures) > MAX_CAPTURE_COUNT:
            raise ReplayProjectError("project captures are invalid or exceed the limit")
        if not isinstance(knowledge, dict) or set(knowledge) != {"workspaces", "aliases", "materials"}:
            raise ReplayProjectError("project knowledge index is invalid")
        capture_ids: set[str] = set()
        capture_revision_total = 0
        shader_total = 0

        def validate_shaders(value: Any, label: str) -> list[dict[str, Any]]:
            if not isinstance(value, list):
                raise ReplayProjectError("%s shaders are invalid" % label)
            seen: set[tuple[int, str, str, str]] = set()
            for shader in value:
                if not isinstance(shader, dict):
                    raise ReplayProjectError("%s shader record is invalid" % label)
                identity = _shader_identity(shader)
                if identity in seen:
                    raise ReplayProjectError(
                        "%s has duplicate shader identity" % label
                    )
                seen.add(identity)
                _positive(identity[0], "shader.resourceId")
                _text(identity[1], "shader.stage", 64)
                _sha(identity[3], "shader.rawSha256", True)
                _sha(shader.get("codeSha256"), "shader.codeSha256", True)
                _sha(shader.get("layoutHash"), "shader.layoutHash", True)
                _sha(shader.get("interfaceHash"), "shader.interfaceHash", True)
            return value

        for capture in captures:
            if not isinstance(capture, dict):
                raise ReplayProjectError("project capture record is invalid")
            capture_id = _sha(capture.get("captureId"), "capture.captureId")
            if capture_id in capture_ids:
                raise ReplayProjectError("project contains duplicate captureId")
            capture_ids.add(capture_id)
            _text(capture.get("capturePath"), "capture.capturePath", 4096, True)
            _text(capture.get("api"), "capture.api", 128, True)
            current_catalog_path = _text(
                capture.get("catalogPath"), "capture.catalogPath", 4096
            )
            current_catalog_sha256 = _sha(
                capture.get("catalogSha256"), "capture.catalogSha256"
            )
            current_imported_at = _text(
                capture.get("importedAt"), "capture.importedAt", 128
            )
            current_shaders = validate_shaders(
                capture.get("shaders"), "project capture"
            )
            revisions = capture.get("catalogRevisions")
            if revisions is not None:
                if not isinstance(revisions, list) or not revisions:
                    raise ReplayProjectError(
                        "project capture catalogRevisions must be a non-empty array"
                    )
                revision_hashes: set[str] = set()
                active_catalog_sha256 = _sha(
                    capture.get("activeCatalogSha256"),
                    "capture.activeCatalogSha256",
                )
                if active_catalog_sha256 != current_catalog_sha256:
                    raise ReplayProjectError(
                        "active capture revision does not match the top-level mirror"
                    )
                active_revision: dict[str, Any] | None = None
                for revision in revisions:
                    if not isinstance(revision, dict) or set(revision) != {
                        "catalogPath",
                        "catalogSha256",
                        "importedAt",
                        "shaders",
                    }:
                        raise ReplayProjectError(
                            "project capture catalog revision is invalid"
                        )
                    revision_path = _text(
                        revision.get("catalogPath"),
                        "capture.revision.catalogPath",
                        4096,
                    )
                    revision_sha256 = _sha(
                        revision.get("catalogSha256"),
                        "capture.revision.catalogSha256",
                    )
                    _text(
                        revision.get("importedAt"),
                        "capture.revision.importedAt",
                        128,
                    )
                    if revision_sha256 in revision_hashes:
                        raise ReplayProjectError(
                            "project capture has duplicate catalog revision"
                        )
                    revision_hashes.add(revision_sha256)
                    revision_shaders = validate_shaders(
                        revision.get("shaders"), "project capture revision"
                    )
                    shader_total += len(revision_shaders)
                    snapshot = self._artifact(
                        revision_path, "capture revision catalog"
                    )
                    snapshot_bytes = _file_bytes(
                        snapshot,
                        MAX_CAPTURE_CATALOG_BYTES,
                        "capture revision catalog",
                    )
                    if _sha256_bytes(snapshot_bytes) != revision_sha256:
                        raise ReplayProjectError(
                            "capture revision catalog hash mismatch"
                        )
                    if revision_sha256 == active_catalog_sha256:
                        active_revision = revision
                if active_revision is None:
                    raise ReplayProjectError(
                        "current capture catalog is absent from its revisions"
                    )
                if (
                    active_revision["catalogPath"] != current_catalog_path
                    or active_revision["catalogSha256"]
                    != current_catalog_sha256
                    or active_revision["importedAt"] != current_imported_at
                    or active_revision["shaders"] != current_shaders
                ):
                    raise ReplayProjectError(
                        "active capture revision mirror is inconsistent"
                    )
                capture_revision_total += len(revisions)
            else:
                # Backward-compatible v1 manifests predate explicit revision
                # history and represent exactly one revision.
                capture_revision_total += 1
                if capture.get("activeCatalogSha256") not in (None, ""):
                    raise ReplayProjectError(
                        "legacy capture must not contain an active revision pointer"
                    )
                shader_total += len(current_shaders)
        if shader_total > MAX_SHADER_COUNT:
            raise ReplayProjectError("project shader count exceeds the limit")
        if capture_revision_total > MAX_CAPTURE_REVISION_COUNT:
            raise ReplayProjectError("project capture revision count exceeds the limit")
        for kind in ("workspaces", "aliases", "materials"):
            records = knowledge.get(kind)
            if not isinstance(records, list) or len(records) > MAX_KNOWLEDGE_COUNT:
                raise ReplayProjectError("project %s knowledge exceeds the limit" % kind)
        # Missing state is the sole legacy-v1 migration path. It is derived in
        # memory here and atomically persisted by the next locked mutation.
        self._active_knowledge_state(manifest)
        heads_before = copy.deepcopy(manifest.get("activeHeads"))
        self._active_heads_state(manifest)
        migration_required = migration_required or heads_before != manifest.get("activeHeads")
        for record in knowledge["workspaces"]:
            artifact = self._artifact(record.get("sourcePath"), "workspace source")
            if _sha256_bytes(_file_bytes(artifact, MAX_SOURCE_BYTES, "workspace source")) != _sha(record.get("sourceSha256"), "sourceSha256"):
                raise ReplayProjectError("project workspace source hash mismatch")
            compiler_binding = record.get("compilerBinding")
            if compiler_binding is not None:
                clean_compiler_binding = _compiler_binding_identity(
                    compiler_binding
                )
                if clean_compiler_binding["targetProfile"] != record.get(
                    "targetProfile"
                ):
                    raise ReplayProjectError(
                        "project compiler binding target profile differs"
                    )
            compiled_candidate = record.get("compiledCandidate")
            if compiled_candidate is not None:
                identity = _compiled_candidate_identity(compiled_candidate)
                _check_manual_candidate_original(identity, record)
                _validate_native_candidate_binding(identity, compiler_binding, _workspace_source_format(record))
                if (
                    identity["sourceSha256"] != record.get("sourceSha256")
                    or identity["targetProfile"] != record.get("targetProfile")
                    or identity["originBaselineAbiSha256"]
                    != record.get("baselineAbiSha256")
                ):
                    raise ReplayProjectError(
                        "project compiled candidate knowledge binding mismatch"
                    )
                expected_relative = (
                    "knowledge/workspaces/binaries/%s.bin"
                    % identity["rawSha256"]
                )
                if compiled_candidate.get("artifactPath") != expected_relative:
                    raise ReplayProjectError(
                        "project compiled candidate is not content-addressed"
                    )
                compiled_artifact = self._artifact(
                    expected_relative, "workspace compiled candidate"
                )
                compiled_bytes = _file_bytes(
                    compiled_artifact,
                    MAX_COMPILED_CANDIDATE_BYTES,
                    "workspace compiled candidate",
                )
                if (
                    len(compiled_bytes) != identity["byteSize"]
                    or _sha256_bytes(compiled_bytes) != identity["rawSha256"]
                ):
                    raise ReplayProjectError(
                        "project compiled candidate content/hash mismatch"
                    )
            disassembly_artifacts = record.get(
                "assemblyDisassemblyArtifacts", []
            )
            if (
                not isinstance(disassembly_artifacts, list)
                or len(disassembly_artifacts) > 2
            ):
                raise ReplayProjectError(
                    "project assembly/disassembly artifacts are invalid"
                )
            seen_disassembly_roles: set[str] = set()
            for disassembly in disassembly_artifacts:
                identity = _assembly_disassembly_identity(disassembly)
                if identity["role"] in seen_disassembly_roles:
                    raise ReplayProjectError(
                        "project disassembly role is duplicated"
                    )
                if (
                    identity["kind"] == "native-disassembly"
                    and (
                        identity["role"] != "baseline"
                        or identity["inputRawSha256"]
                        != _sha(record.get("rawSha256"), "workspace.rawSha256")
                    )
                ):
                    raise ReplayProjectError(
                        "project native disassembly physical binding mismatch"
                    )
                expected_relative = _assembly_disassembly_relative(identity)
                if disassembly.get("artifactPath") != expected_relative:
                    raise ReplayProjectError(
                        "project disassembly artifact is not content-addressed"
                    )
                disassembly_bytes = _file_bytes(
                    self._artifact(
                        expected_relative, "workspace disassembly artifact"
                    ),
                    MAX_SOURCE_BYTES,
                    "workspace disassembly artifact",
                )
                if (
                    len(disassembly_bytes) != identity["byteSize"]
                    or _sha256_bytes(disassembly_bytes) != identity["sha256"]
                ):
                    raise ReplayProjectError(
                        "project disassembly artifact content/hash mismatch"
                    )
                seen_disassembly_roles.add(identity["role"])
            analysis_path = record.get("analysisPath")
            if analysis_path is not None:
                analysis = self._artifact(analysis_path, "workspace analysis")
                if _sha256_bytes(
                    _file_bytes(analysis, MAX_ANALYSIS_BYTES, "workspace analysis")
                ) != _sha(record.get("analysisSha256"), "analysisSha256"):
                    raise ReplayProjectError("project workspace analysis hash mismatch")
            # The knowledgeId is the immutable metadata/content binding. This
            # also preserves the historical v1 identity when compiledCandidate
            # is absent and extends it only for newly published binary records.
            self._workspace_knowledge_from_manifest(
                manifest,
                str(record.get("knowledgeId", "")),
                record=record,
                source_artifact_verified=True,
            )
        for record in knowledge["aliases"]:
            artifact = self._artifact(record.get("artifactPath"), "alias artifact")
            artifact_sha256 = _sha(
                record.get("artifactSha256"), "artifactSha256"
            )
            knowledge_id = str(record.get("knowledgeId", "") or "")
            should_cache = (
                alias_artifact_cache is not None
                and knowledge_id in requested_alias_artifacts
            )
            cache_key = (str(artifact), artifact_sha256)
            raw = cached_alias_artifacts.get(cache_key) if should_cache else None
            if raw is None:
                raw = _file_bytes(artifact, MAX_ALIAS_BYTES, "alias artifact")
                if should_cache:
                    if (
                        cached_alias_artifact_bytes + len(raw)
                        > alias_artifact_cache_max_bytes
                    ):
                        raise ReplayProjectError(
                            "alias materialization artifact snapshot exceeds the byte limit"
                        )
                    cached_alias_artifact_bytes += len(raw)
                    cached_alias_artifacts[cache_key] = raw
            if _sha256_bytes(raw) != artifact_sha256:
                raise ReplayProjectError("project alias artifact hash mismatch")
            if should_cache:
                existing = alias_artifact_cache.get(knowledge_id)
                if existing is not None and existing != raw:
                    raise ReplayProjectError(
                        "alias knowledgeId resolves to conflicting artifact bytes"
                    )
                alias_artifact_cache[knowledge_id] = raw
        for record in knowledge["materials"]:
            self._material_knowledge_from_manifest(
                manifest,
                str(record.get("knowledgeId", "")),
                record=record,
            )
        if persist_migrations and migration_required:
            self._write(manifest)
        return manifest

    def _artifact(self, relative: Any, label: str) -> Path:
        value = _text(relative, label, 4096)
        path = (self.root / value).resolve()
        if not is_within(self.root, path):
            raise ReplayProjectError("%s escapes the project" % label)
        from .project_read_cache import observe_path
        observe_path(self.root / value, path)
        return path

    def _stage_catalog_snapshot(
        self,
        capture_id: str,
        catalog_sha256: str,
        payload: bytes,
        blobs: Mapping[str, bytes],
    ) -> str:
        """Publish one immutable project-owned catalog revision."""

        capture = _sha(capture_id, "captureId")
        revision = _sha(catalog_sha256, "catalogSha256")
        catalogs_root = self.root / "captures" / "catalogs"
        revisions_root = catalogs_root / capture / "revisions"
        revision_root = revisions_root / revision
        catalog_path = revision_root / "catalog.json"
        for path, label in (
            (catalogs_root, "capture catalog root"),
            (catalogs_root / capture, "capture catalog family"),
            (revisions_root, "capture catalog revisions"),
        ):
            path.mkdir(parents=True, exist_ok=True)
            if path.is_symlink() or not path.is_dir():
                raise ReplayProjectError("%s is redirected" % label)

        if revision_root.exists() or revision_root.is_symlink():
            if revision_root.is_symlink() or not revision_root.is_dir():
                raise ReplayProjectError("capture catalog revision is redirected")
            existing = _file_bytes(
                catalog_path,
                MAX_CAPTURE_CATALOG_BYTES,
                "project capture catalog snapshot",
            )
            if existing != payload or _sha256_bytes(existing) != revision:
                raise ReplayProjectError(
                    "project capture catalog revision bytes conflict"
                )
            for digest, blob in blobs.items():
                existing_blob = _file_bytes(
                    revision_root / "blobs" / (digest + ".bin"),
                    MAX_CAPTURE_CATALOG_BYTES,
                    "project capture shader snapshot",
                )
                if existing_blob != blob or _sha256_bytes(existing_blob) != digest:
                    raise ReplayProjectError(
                        "project capture shader snapshot hash mismatch"
                    )
        else:
            staging = Path(
                tempfile.mkdtemp(
                    prefix=".%s.staging-" % revision, dir=str(revisions_root)
                )
            )
            try:
                (staging / "blobs").mkdir()
                for digest, blob in blobs.items():
                    atomic_write_bytes(
                        staging / "blobs" / (digest + ".bin"), blob
                    )
                atomic_write_bytes(staging / "catalog.json", payload)
                staging.replace(revision_root)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return catalog_path.relative_to(self.root).as_posix()

    def _write(self, manifest: dict[str, Any]) -> None:
        self._active_knowledge_state(manifest)
        self._active_heads_state(manifest)
        manifest["updatedAt"] = _utc_now()
        atomic_write_json(self.manifest_path, manifest)

    # How long a status read waits for a mutation on the same project in this
    # process before reading the manifest as it is on disk.
    STATUS_LOCK_WAIT_SECONDS = 2.0

    def status(self) -> dict[str, Any]:
        # A read. It used to queue behind every mutation of this project in the
        # process -- a workspace registration validating DXIL for a minute held
        # the same RLock -- so the project window's list request timed out at
        # 90 s and the user saw "Host request outcome is uncertain". Wait
        # briefly for a consistent snapshot; past that, the manifest on disk is
        # atomically written and good enough for a status line.
        # A read takes no file lock: another process holding the project for a
        # mutation (or a lock it left behind) must not make the project
        # "unavailable" in the window. Migrations persist on the next mutation.
        lock = _thread_lock(self.root)
        busy = not lock.acquire(timeout=self.STATUS_LOCK_WAIT_SECONDS)
        try:
              manifest = self._load(persist_migrations=False)
        finally:
            if not busy:
                lock.release()
        knowledge = manifest["knowledge"]
        return {
            "busy": busy,
            "schema": PROJECT_SCHEMA,
            "schemaVersion": PROJECT_VERSION,
            "projectId": manifest["projectId"],
            "name": manifest.get("name", ""),
            "scope": dict(manifest["scope"]),
            "projectPath": str(self.root),
            "manifestPath": str(self.manifest_path),
            "captureCount": len(manifest["captures"]),
            "captureRevisionCount": sum(
                len(item.get("catalogRevisions", [])) or 1
                for item in manifest["captures"]
            ),
            "shaderCount": sum(len(item["shaders"]) for item in manifest["captures"]),
            "knowledgeCounts": {key: len(value) for key, value in knowledge.items()},
            "policy": {
                "crossFrameReplayStateReused": False,
                "oldAbiReportsReused": False,
                "autoApplyAuthorized": False,
                "liveAbiRevalidationRequired": True,
                "oneGameBuildPlatformApiScope": True,
            },
        }

    def import_capture(self, catalog_path: str | Path) -> dict[str, Any]:
        source, catalog, payload, blobs = _normalised_catalog_evidence(catalog_path)
        shaders = [_compact_shader(item) for item in catalog["shaders"]]
        if len(shaders) > MAX_SHADER_COUNT:
            raise ReplayProjectError("capture shader count exceeds the project limit")
        imported_at = _utc_now()
        catalog_sha256 = _sha256_bytes(payload)
        capture_id = _sha(catalog.get("captureId"), "captureId")
        capture_path = _text(
            str(catalog.get("capturePath", "") or ""),
            "capturePath",
            4096,
            True,
        )
        api = _text(str(catalog.get("api", "") or ""), "api", 128, True)
        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load()
            scope = manifest["scope"]
            catalog_game = str(catalog.get("gameId", "") or "").strip()
            catalog_build = str(catalog.get("buildId", "") or "").strip()
            catalog_platform = str(catalog.get("platform", "") or "").strip()
            if catalog_game and catalog_game != scope["gameId"]:
                raise ReplayProjectError("capture catalog gameId conflicts with project scope")
            if catalog_build and catalog_build != scope["buildId"]:
                raise ReplayProjectError("capture catalog buildId conflicts with project scope")
            if scope["platform"] and catalog_platform and catalog_platform != scope["platform"]:
                raise ReplayProjectError("capture catalog platform conflicts with project scope")
            if not scope["platform"] and catalog_platform:
                scope["platform"] = _text(catalog_platform, "catalog.platform", 128)
            capture_api = _api_family(catalog.get("api"))
            if (scope["apiFamily"] and
                    _reuse_api_family(capture_api) != _reuse_api_family(scope["apiFamily"])):
                raise ReplayProjectError("capture API conflicts with project scope")
            if not scope["apiFamily"]:
                scope["apiFamily"] = capture_api
            snapshot_path = self._stage_catalog_snapshot(
                capture_id, catalog_sha256, payload, blobs
            )
            revision = {
                "catalogPath": snapshot_path,
                "catalogSha256": catalog_sha256,
                "importedAt": imported_at,
                "shaders": shaders,
            }
            record = {
                "captureId": capture_id,
                "capturePath": capture_path,
                "api": api,
                "catalogPath": snapshot_path,
                "catalogSha256": catalog_sha256,
                "importedAt": imported_at,
                "activeCatalogSha256": catalog_sha256,
                "catalogRevisions": [revision],
                "shaders": shaders,
            }
            existing_index = next(
                (
                    index
                    for index, item in enumerate(manifest["captures"])
                    if item["captureId"] == record["captureId"]
                ),
                None,
            )
            existing = (
                manifest["captures"][existing_index]
                if existing_index is not None
                else None
            )
            if existing is not None:
                if (
                    existing.get("capturePath") != capture_path
                    or existing.get("api") != api
                ):
                    raise ReplayProjectError(
                        "captureId conflicts with its imported transport evidence"
                    )
                revisions = list(existing.get("catalogRevisions", []))
                if not revisions:
                    # Upgrade an old v1 record by reconstructing an immutable,
                    # canonical project-owned snapshot from its original
                    # catalog. Failure is closed: compact claims alone are not
                    # enough to manufacture historical catalog evidence.
                    (
                        unused_legacy_source,
                        legacy_catalog,
                        legacy_payload,
                        legacy_blobs,
                    ) = _normalised_catalog_evidence(
                        (
                            Path(existing["catalogPath"])
                            if Path(existing["catalogPath"]).is_absolute()
                            else self.root / existing["catalogPath"]
                        )
                    )
                    if _sha(legacy_catalog.get("captureId"), "legacy.captureId") != capture_id:
                        raise ReplayProjectError(
                            "legacy capture catalog identity changed before migration"
                        )
                    legacy_shaders = [
                        _compact_shader(item) for item in legacy_catalog["shaders"]
                    ]
                    if legacy_shaders != existing["shaders"]:
                        raise ReplayProjectError(
                            "legacy capture catalog evidence changed before migration"
                        )
                    legacy_sha256 = _sha256_bytes(legacy_payload)
                    legacy_path = self._stage_catalog_snapshot(
                        capture_id,
                        legacy_sha256,
                        legacy_payload,
                        legacy_blobs,
                    )
                    revisions = [
                        {
                            "catalogPath": legacy_path,
                            "catalogSha256": legacy_sha256,
                            "importedAt": existing["importedAt"],
                            "shaders": legacy_shaders,
                        }
                    ]
                    legacy_head = revisions[0]
                    existing = {
                        **existing,
                        "catalogPath": legacy_head["catalogPath"],
                        "catalogSha256": legacy_head["catalogSha256"],
                        "activeCatalogSha256": legacy_head["catalogSha256"],
                        "catalogRevisions": revisions,
                        "shaders": legacy_head["shaders"],
                    }

                matching_revisions = [
                    item
                    for item in revisions
                    if item["catalogSha256"] == catalog_sha256
                ]
                if len(matching_revisions) > 1:
                    raise ReplayProjectError(
                        "project contains duplicate capture catalog revision"
                    )
                if matching_revisions:
                    known = matching_revisions[0]
                    if known["shaders"] != shaders:
                        raise ReplayProjectError(
                            "known catalog revision has contradictory shader evidence"
                        )
                    exact_capture = self._capture_from_revision(existing, known)
                    plan = self._reuse_plan(manifest, exact_capture)
                    plan.update(
                        {
                            "idempotent": True,
                            "catalogRevisionKnown": True,
                            "catalogRevisionAdded": False,
                            "activeRevisionChanged": False,
                            "activeCatalogSha256": existing.get(
                                "activeCatalogSha256",
                                existing["catalogSha256"],
                            ),
                        }
                    )
                    # Persist only a necessary legacy migration. Reimporting a
                    # known historical revision never moves the active head.
                    if manifest["captures"][existing_index] != existing:
                        manifest["captures"][existing_index] = existing
                        self._write(manifest)
                    return plan

                if {
                    _shader_identity(item) for item in existing["shaders"]
                } != {_shader_identity(item) for item in shaders}:
                    raise ReplayProjectError(
                        "captureId is already imported with different Shader identities"
                    )
                revision_total = sum(
                    len(item.get("catalogRevisions", [])) or 1
                    for item in manifest["captures"]
                )
                if revision_total >= MAX_CAPTURE_REVISION_COUNT:
                    raise ReplayProjectError(
                        "project capture revision limit reached"
                    )
                shader_revision_total = sum(
                    sum(
                        len(revision_item.get("shaders", item["shaders"]))
                        for revision_item in item.get("catalogRevisions", [{}])
                    )
                    for item in manifest["captures"]
                )
                if shader_revision_total + len(shaders) > MAX_SHADER_COUNT:
                    raise ReplayProjectError(
                        "project capture revision shaders exceed the limit"
                    )
                revisions.append(revision)

                # captureId is a catalog-family identity (transport + Shader
                # byte identities), not a hash of reflection, use-sites, or
                # diagnostics. Select the just-imported validated revision for
                # matching while retaining every prior content hash/path.
                record["catalogRevisions"] = revisions
                manifest["captures"][existing_index] = record
                self._write(manifest)
                plan = self._reuse_plan(manifest, record)
                plan.update(
                    {
                        "idempotent": False,
                        "catalogRevisionKnown": False,
                        "catalogRevisionAdded": True,
                        "activeRevisionChanged": True,
                        "activeCatalogSha256": catalog_sha256,
                    }
                )
                return plan
            if len(manifest["captures"]) >= MAX_CAPTURE_COUNT:
                raise ReplayProjectError("project capture limit reached")
            manifest["captures"].append(record)
            self._write(manifest)
            plan = self._reuse_plan(manifest, record)
            plan.update(
                {
                    "idempotent": False,
                    "catalogRevisionKnown": False,
                    "catalogRevisionAdded": True,
                    "activeRevisionChanged": True,
                    "activeCatalogSha256": catalog_sha256,
                }
            )
            return plan

    @staticmethod
    def _capture_from_revision(
        capture: Mapping[str, Any], revision: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "captureId": capture["captureId"],
            "capturePath": capture.get("capturePath", ""),
            "api": capture.get("api", ""),
            "catalogPath": revision["catalogPath"],
            "catalogSha256": revision["catalogSha256"],
            "importedAt": revision["importedAt"],
            "shaders": revision["shaders"],
        }

    @staticmethod
    def _capture(
        manifest: Mapping[str, Any],
        capture_id: str,
        catalog_sha256: str = "",
    ) -> dict[str, Any]:
        digest = _sha(capture_id, "capture_id")
        matches = [item for item in manifest["captures"] if item["captureId"] == digest]
        if len(matches) != 1:
            raise ReplayProjectError("capture_id is not imported in this project")
        capture = matches[0]
        if not catalog_sha256:
            return capture
        revision_sha256 = _sha(catalog_sha256, "catalog_sha256")
        revisions = capture.get("catalogRevisions")
        if revisions is None:
            if capture.get("catalogSha256") != revision_sha256:
                raise ReplayProjectError(
                    "catalog revision is not imported in this project"
                )
            return capture
        revision_matches = [
            item
            for item in revisions
            if item["catalogSha256"] == revision_sha256
        ]
        if len(revision_matches) != 1:
            raise ReplayProjectError(
                "catalog revision is not imported in this project"
            )
        return ReplayProjectService._capture_from_revision(
            capture, revision_matches[0]
        )

    @staticmethod
    def _capture_for_provenance(
        manifest: Mapping[str, Any],
        capture_id: str,
        catalog_sha256: str = "",
    ) -> dict[str, Any]:
        if catalog_sha256:
            return ReplayProjectService._capture(
                manifest, capture_id, catalog_sha256
            )
        capture = ReplayProjectService._capture(manifest, capture_id)
        revisions = capture.get("catalogRevisions")
        if isinstance(revisions, list) and len(revisions) > 1:
            raise ReplayProjectError(
                "catalog revision is ambiguous; exact catalogSha256 provenance is required"
            )
        return capture

    @staticmethod
    def _capture_status_catalog_sha256(
        capture_status: Mapping[str, Any]
    ) -> str:
        declared_text = str(capture_status.get("catalogSha256", "") or "")
        declared = (
            _sha(declared_text, "workspace.capture.catalogSha256")
            if declared_text
            else ""
        )
        catalog_path = str(capture_status.get("catalogPath", "") or "")
        if not catalog_path:
            if declared:
                raise ReplayProjectError(
                    "workspace catalogSha256 requires catalogPath provenance"
                )
            return ""
        raw_path = Path(catalog_path).expanduser()
        if raw_path.exists() and raw_path.is_symlink():
            raise ReplayProjectError("workspace capture catalog is redirected")
        canonical_path = raw_path.resolve()
        physical = _file_bytes(
            canonical_path,
            MAX_CAPTURE_CATALOG_BYTES,
            "workspace capture catalog",
        )
        if declared and _sha256_bytes(physical) != declared:
            raise ReplayProjectError(
                "workspace catalogSha256 does not match catalogPath"
            )
        (
            unused_source,
            catalog,
            payload,
            unused_blobs,
        ) = _normalised_catalog_evidence(canonical_path)
        if catalog.get("captureId") != capture_status.get("captureId"):
            raise ReplayProjectError(
                "workspace capture catalog belongs to a different capture"
            )
        if capture_status.get("capturePath") not in (None, "") and catalog.get(
            "capturePath"
        ) != capture_status.get("capturePath"):
            raise ReplayProjectError(
                "workspace capturePath differs from its catalog"
            )
        if capture_status.get("api") not in (None, "") and catalog.get(
            "api"
        ) != capture_status.get("api"):
            raise ReplayProjectError("workspace capture API differs from its catalog")
        return _sha256_bytes(payload)

    @staticmethod
    def _capture_shader(capture: Mapping[str, Any], *, resource_id: int, stage: str, entry_point: str, raw_sha256: str = "") -> dict[str, Any]:
        matches = [
            item
            for item in capture["shaders"]
            if item["resourceId"] == resource_id
            and item["stage"] == stage
            and item.get("entryPoint", "") == entry_point
            and (not raw_sha256 or item.get("rawSha256") == raw_sha256)
        ]
        if len(matches) != 1:
            raise ReplayProjectError("knowledge does not select one imported capture shader")
        return matches[0]

    @staticmethod
    def _validated_compiled_candidate(
        status: Mapping[str, Any],
        *,
        source_bytes: bytes,
        workspace_root: Path,
        manifest_bytes: bytes,
        baseline: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bytes] | None:
        """Extract only a fully ABI-validated, current-source binary.

        Pending/container-only candidates remain valid workspace state but are
        intentionally not published to the project.  A fully validated binary
        compiled from an older editable-source snapshot is likewise omitted so
        the current HLSL can still be registered.  Any status which claims
        completed validation is checked against its on-disk manifest, compiled
        source revision, candidate bytes, and immutable ABI receipt before that
        stale/current decision is made.
        """

        if status.get("manualApplyReceipt") is not None:
            from .project_manual_shader import candidate_from_status
            try:
                return candidate_from_status(status, source_bytes=source_bytes,
                    workspace_root=workspace_root, manifest_bytes=manifest_bytes,
                    baseline=baseline, schema=COMPILED_CANDIDATE_SCHEMA,
                    version=COMPILED_CANDIDATE_VERSION)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                raise ReplayProjectError(str(exc)) from exc
        latest = status.get("latestCompiled")
        if latest is None:
            if (
                status.get("replayAbiValidated") is True
                or status.get("abiCompatible") is True
            ):
                raise ReplayProjectError(
                    "workspace claims ABI validation without a compiled candidate"
                )
            return None
        if not isinstance(latest, Mapping):
            raise ReplayProjectError("workspace latestCompiled is invalid")
        if latest.get("replayAbiValidated") is not True:
            if any(
                (
                    status.get("replayAbiValidated") is True,
                    status.get("abiCompatible") is True,
                    latest.get("abiCompatible") is True,
                    latest.get("validationScope")
                    == "replay-reflection-abi-compatible",
                )
            ):
                raise ReplayProjectError(
                    "workspace compiled candidate ABI validation state is inconsistent"
                )
            return None
        if any(
            (
                status.get("replayAbiValidated") is not True,
                status.get("abiCompatible") is not True,
                latest.get("abiCompatible") is not True,
                latest.get("requiresReplayAbiValidation") is not False,
                latest.get("validationScope")
                != "replay-reflection-abi-compatible",
                latest.get("semanticEquivalenceVerified") is not False,
            )
        ):
            raise ReplayProjectError(
                "workspace compiled candidate lacks complete ABI validation"
            )

        revision = _positive(status.get("revision"), "workspace.revision")
        if latest.get("revision") != revision:
            raise ReplayProjectError(
                "workspace compiled candidate revision is not current"
            )
        source_sha256 = _sha256_bytes(source_bytes)
        candidate_source_sha256 = _sha(
            latest.get("sourceSha256"),
            "workspace.latestCompiled.sourceSha256",
        )

        try:
            workspace_manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayProjectError("workspace manifest is not valid UTF-8 JSON") from exc
        if not isinstance(workspace_manifest, Mapping):
            raise ReplayProjectError("workspace manifest must be a JSON object")
        persisted_latest = workspace_manifest.get("latestCompiled")
        projected_latest = dict(latest)
        projected_latest.pop("candidateBlobPath", None)
        projected_latest.pop("applied", None)
        if (
            workspace_manifest.get("revision") != revision
            or persisted_latest != projected_latest
        ):
            raise ReplayProjectError(
                "workspace compiled candidate differs from its validated manifest"
            )

        relative_blob = _text(
            str(latest.get("blobPath", "")),
            "workspace.latestCompiled.blobPath",
            4096,
        )
        declared_blob_path = _normal_path(
            str(latest.get("candidateBlobPath", ""))
        )
        expected_blob_path = (workspace_root / relative_blob).resolve()
        if (
            declared_blob_path != expected_blob_path
            or not is_within(workspace_root, expected_blob_path)
        ):
            raise ReplayProjectError(
                "workspace compiled candidate path escapes its workspace"
            )
        compiled = _file_bytes(
            expected_blob_path,
            MAX_COMPILED_CANDIDATE_BYTES,
            "workspace compiled candidate",
        )
        compiled_sha256 = _sha256_bytes(compiled)
        if (
            _sha(
                latest.get("rawSha256"),
                "workspace.latestCompiled.rawSha256",
            )
            != compiled_sha256
            or _positive(
                latest.get("byteSize"), "workspace.latestCompiled.byteSize"
            )
            != len(compiled)
        ):
            raise ReplayProjectError(
                "workspace compiled candidate content/hash binding is invalid"
            )

        relative_revision_source = _text(
            str(latest.get("sourcePath", "")),
            "workspace.latestCompiled.sourcePath",
            4096,
        )
        revision_source = (workspace_root / relative_revision_source).resolve()
        if not is_within(workspace_root, revision_source):
            raise ReplayProjectError(
                "workspace compiled candidate source revision escapes its workspace"
            )
        revision_source_bytes = _file_bytes(
            revision_source,
            MAX_SOURCE_BYTES,
            "workspace compiled candidate source revision",
        )
        if _sha256_bytes(revision_source_bytes) != candidate_source_sha256:
            raise ReplayProjectError(
                "workspace compiled candidate source revision changed"
            )

        abi_gate = latest.get("abiGate")
        if not isinstance(abi_gate, Mapping) or abi_gate.get("status") != "compatible":
            raise ReplayProjectError(
                "workspace compiled candidate ABI gate is not compatible"
            )
        baseline_abi_sha256 = _sha(
            abi_gate.get("baselineAbiSha256"),
            "workspace.latestCompiled.abiGate.baselineAbiSha256",
        )
        if baseline_abi_sha256 != _sha(
            baseline.get("abiSha256"), "workspace.baseline.abiSha256"
        ):
            raise ReplayProjectError(
                "workspace compiled candidate baseline ABI binding differs"
            )
        candidate_abi_sha256 = _sha(
            abi_gate.get("candidateAbiSha256"),
            "workspace.latestCompiled.abiGate.candidateAbiSha256",
        )
        report_sha256 = _sha(
            abi_gate.get("reportSha256"),
            "workspace.latestCompiled.abiGate.reportSha256",
        )
        request_id = _text(
            str(abi_gate.get("requestId", "")),
            "workspace.latestCompiled.abiGate.requestId",
            256,
        )
        relative_report = _text(
            str(abi_gate.get("reportPath", "")),
            "workspace.latestCompiled.abiGate.reportPath",
            4096,
        )
        report_path = (workspace_root / relative_report).resolve()
        if not is_within(workspace_root, report_path):
            raise ReplayProjectError("workspace ABI report escapes its workspace")
        if _sha256_bytes(
            _file_bytes(report_path, MAX_ABI_REPORT_BYTES, "workspace ABI report")
        ) != report_sha256:
            raise ReplayProjectError("workspace ABI report hash mismatch")

        profile = status.get("targetProfile")
        if not isinstance(profile, Mapping):
            raise ReplayProjectError(
                "workspace compiled candidate targetProfile is invalid"
            )
        # Editing HLSL does not rewrite or invalidate the immutable compiled
        # revision.  Preserve that audit history in the workspace, but do not
        # let an older binary block publication of the newer project source or
        # masquerade as a current-source reuse candidate.
        if candidate_source_sha256 != source_sha256:
            return None
        identity = _compiled_candidate_identity(
            {
                "schema": COMPILED_CANDIDATE_SCHEMA,
                "schemaVersion": COMPILED_CANDIDATE_VERSION,
                "rawSha256": compiled_sha256,
                "codeSha256": _sha(
                    latest.get("codeSha256"),
                    "workspace.latestCompiled.codeSha256",
                ),
                "byteSize": len(compiled),
                "kind": _text(
                    str(latest.get("kind", "")),
                    "workspace.latestCompiled.kind",
                    64,
                ),
                "sourceSha256": source_sha256,
                "sourceRevision": revision,
                "targetProfile": copy.deepcopy(dict(profile)),
                "originBaselineAbiSha256": baseline_abi_sha256,
                "originCandidateAbiSha256": candidate_abi_sha256,
                "originAbiReportSha256": report_sha256,
                "originRequestId": request_id,
                "validationScope": "replay-reflection-abi-compatible",
                "semanticEquivalenceVerified": False,
            }
        )
        _validate_native_candidate_binding(identity, status.get("compilerBinding"),
                                           str((status.get("source") or {}).get("format")))
        if identity["kind"] == "GLSL":
            if (_reuse_api_family((status.get("capture") or {}).get("api")) != "opengl"
                    or baseline.get("kind") not in {"GLSL", "OPENGL-SPIRV"}
                    or compiled != revision_source_bytes):
                raise ReplayProjectError("native GLSL candidate differs from its OpenGL source revision")
        return identity, compiled

    def register_workspace(
        self,
        status: Mapping[str, Any],
        expected_head: Mapping[str, Any] | None = None,
        *,
        include_analysis: bool = True,
        require_current_candidate: bool = False,
        include_compiled_candidate: bool = True,
    ) -> dict[str, Any]:
        if type(include_compiled_candidate) is not bool:
            raise ReplayProjectError("include_compiled_candidate must be a boolean")
        if require_current_candidate and not include_compiled_candidate:
            raise ReplayProjectError(
                "a current compiled candidate cannot be required and omitted"
            )
        if not isinstance(status, Mapping) or status.get("schema") != "renderdoc-ultra-shader-workspace-status/v1":
            raise ReplayProjectError("workspace status must come from strict workspace validation")
        source = status.get("source")
        capture_status = status.get("capture")
        shader_status = status.get("shader")
        baseline = status.get("baseline")
        if not all(isinstance(value, Mapping) for value in (source, capture_status, shader_status, baseline)):
            raise ReplayProjectError("workspace status provenance is incomplete")
        if source.get("present") is not True:
            raise ReplayProjectError("workspace has no decompiled/edited source to register")
        # Keep textual source languages separate from instruction-level IR.
        if (
            source.get("lane") != "executable-editable"
            or source.get("format") not in {"hlsl", "glsl"}
        ):
            raise ReplayProjectError(
                "project workspace source reuse supports executable-editable HLSL or GLSL"
            )
        source_path = _normal_path(str(source.get("path", "")))
        source_bytes = _file_bytes(source_path, MAX_SOURCE_BYTES, "workspace source")
        workspace_root = _normal_path(str(status.get("workspacePath", "")))
        analysis_path = workspace_root / "ai" / "analysis.md"
        analysis_bytes: bytes | None = None
        analysis_sha256 = ""
        explanatory_diagnostics: list[dict[str, str]] = []
        if include_analysis and (analysis_path.exists() or analysis_path.is_symlink()):
            if not is_within(workspace_root, analysis_path.resolve()):
                raise ReplayProjectError("workspace analysis escapes its workspace")
            analysis_bytes = _file_bytes(
                analysis_path, MAX_ANALYSIS_BYTES, "workspace ai/analysis.md"
            )
            provenance = source_binding_metadata(workspace_root, analysis_path, analysis_bytes, status)
            if provenance["currentSourceMatches"] is False:
                explanatory_diagnostics.append({"artifact": "ai/analysis.md", "status": "skipped",
                                                "reason": provenance["reason"]})
                analysis_bytes, analysis_sha256 = None, ""
            else:
                try:
                    analysis_text = analysis_bytes.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise ReplayProjectError("workspace analysis must be UTF-8 text") from exc
                if "\x00" in analysis_text:
                    raise ReplayProjectError("workspace analysis contains NUL characters")
                analysis_sha256 = _sha256_bytes(analysis_bytes)
        manifest_path = _normal_path(str(status.get("manifestPath", "")))
        manifest_bytes = _file_bytes(manifest_path, MAX_PROJECT_BYTES, "workspace manifest")
        capture_id = _sha(capture_status.get("captureId"), "workspace.captureId")
        catalog_revision_sha256 = self._capture_status_catalog_sha256(
            capture_status
        )
        stage = _text(str(status.get("stage", "")), "workspace.stage", 64)
        entry = _text(str(status.get("entryPoint", "") or ""), "workspace.entryPoint", 512, True)
        resource_id = _positive(shader_status.get("resourceId"), "workspace.resourceId")
        raw = _sha(baseline.get("rawSha256"), "workspace.rawSha256")
        try:
            from .shader_workspace.semantic_overview_storage import publish_overview
            publish_overview(self.root, workspace_root, raw, _sha256_bytes(source_bytes))
        except (ValueError, OSError) as exc:
            explanatory_diagnostics.append({"artifact": "ai/semantic-overview.json",
                                            "status": "skipped", "reason": str(exc)[:800]})
        compiler_binding = (
            _compiler_binding_identity(status.get("compilerBinding"))
            if status.get("compilerBinding") is not None
            else None
        )
        if (
            compiler_binding is not None
            and compiler_binding["targetProfile"] != status.get("targetProfile")
        ):
            raise ReplayProjectError(
                "workspace compiler binding target profile differs"
            )
        raw_disassembly = status.get("assemblyDisassemblyArtifacts", [])
        if not isinstance(raw_disassembly, list) or len(raw_disassembly) > 2:
            raise ReplayProjectError(
                "workspace assembly/disassembly artifacts are invalid"
            )
        prepared_disassembly: list[tuple[dict[str, Any], bytes]] = []
        seen_disassembly_roles: set[str] = set()
        for raw_artifact in raw_disassembly:
            identity = _assembly_disassembly_identity(raw_artifact)
            if identity["role"] in seen_disassembly_roles:
                raise ReplayProjectError(
                    "workspace has duplicate disassembly evidence roles"
                )
            if (
                identity["kind"] == "native-disassembly"
                and (
                    identity["role"] != "baseline"
                    or identity["inputRawSha256"] != raw
                )
            ):
                raise ReplayProjectError(
                    "workspace native disassembly physical binding mismatch"
                )
            unresolved_path = Path(
                str(raw_artifact.get("path", ""))
            ).expanduser()
            if unresolved_path.is_symlink():
                raise ReplayProjectError(
                    "workspace disassembly artifact must not be a symlink"
                )
            path = unresolved_path.resolve()
            payload = _file_bytes(
                path, MAX_SOURCE_BYTES, "workspace disassembly artifact"
            )
            if (
                len(payload) != identity["byteSize"]
                or _sha256_bytes(payload) != identity["sha256"]
            ):
                raise ReplayProjectError(
                    "workspace disassembly artifact content/hash mismatch"
                )
            prepared_disassembly.append((identity, payload))
            seen_disassembly_roles.add(identity["role"])
        prepared_compiled = (
            self._validated_compiled_candidate(
                status,
                source_bytes=source_bytes,
                workspace_root=workspace_root,
                manifest_bytes=manifest_bytes,
                baseline=baseline,
            )
            if include_compiled_candidate else None
        )
        if require_current_candidate and (
            prepared_compiled is None
            or source.get("currentSha256") != _sha256_bytes(source_bytes)
        ):
            raise ReplayProjectError(
                "applied workspace publication requires the current source and "
                "its ABI-validated compiled candidate"
            )
        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load()
            capture = self._capture_for_provenance(
                manifest, capture_id, catalog_revision_sha256
            )
            shader = self._capture_shader(capture, resource_id=resource_id, stage=stage, entry_point=entry, raw_sha256=raw)
            catalog_code_sha = _sha(shader.get("codeSha256"), "capture.codeSha256", True)
            workspace_code_sha = _sha(baseline.get("codeSha256"), "workspace.codeSha256", True)
            # Some executable formats have no separate normalized instruction
            # hash in the capture catalog. The workspace fingerprints those
            # bytes with their raw hash. Preserve the catalog's representation
            # instead of publishing a record that its own reader rejects.
            if (workspace_code_sha != catalog_code_sha
                    and not (not catalog_code_sha and workspace_code_sha == raw)):
                raise ReplayProjectError("workspace code identity differs from its capture")
            material = {
                "workspaceId": _text(str(status.get("workspaceId", "")), "workspaceId", 64),
                "captureId": capture_id,
                "originCatalogSha256": capture["catalogSha256"],
                "originResourceId": resource_id,
                "stage": stage,
                "entryPoint": entry,
                "rawSha256": raw,
                "codeSha256": catalog_code_sha,
                "layoutHash": shader.get("layoutHash", ""),
                "interfaceHash": shader.get("interfaceHash", ""),
                "interfaceComplete": shader.get("interfaceComplete") is True,
                "baselineAbiSha256": _sha(baseline.get("abiSha256"), "workspace.abiSha256", True),
                "targetProfile": status.get("targetProfile"),
                "sourceSha256": _sha256_bytes(source_bytes),
                "analysisSha256": analysis_sha256,
            }
            if source["format"] != "hlsl":
                material["sourceFormat"] = source["format"]
            if not include_compiled_candidate:
                # Persist this in the content-bound identity: source delivery
                # must not become auto-apply authority after a fresh compile.
                material["publicationMode"] = SOURCE_ONLY_PUBLICATION
            if compiler_binding is not None:
                material["compilerBinding"] = compiler_binding
            if prepared_disassembly:
                material["assemblyDisassemblyArtifacts"] = [
                    identity for identity, unused_payload in prepared_disassembly
                ]
            if prepared_compiled is not None:
                compiled_identity, unused_compiled_bytes = prepared_compiled
                material["compiledCandidate"] = compiled_identity
            knowledge_id = _canonical_sha256(material)[:32]
            head_key = self._workspace_head_key(material, manifest)
            relative = "knowledge/workspaces/%s.%s" % (knowledge_id, source["format"])
            destination = self._artifact(relative, "workspace knowledge path")
            analysis_relative = (
                "knowledge/workspaces/%s.analysis.md" % analysis_sha256
                if analysis_bytes is not None
                else ""
            )
            existing = next((item for item in manifest["knowledge"]["workspaces"] if item["knowledgeId"] == knowledge_id), None)
            head_publication = self._plan_head_publication(
                manifest,
                "workspaces",
                head_key,
                knowledge_id,
                record_preexisting=existing is not None,
                expected_head=expected_head,
            )
            if existing is not None:
                if _sha256_bytes(_file_bytes(destination, MAX_SOURCE_BYTES, "workspace source")) != material["sourceSha256"]:
                    raise ReplayProjectError("existing workspace knowledge artifact changed")
                if head_publication["changed"]:
                    self._activate_knowledge(manifest, "workspaces", knowledge_id)
                    self._apply_head_publication(
                        manifest, "workspaces", head_publication
                    )
                    self._write(manifest)
                head_receipt = self._head_receipt(
                    "workspaces", head_publication
                )
                return {
                    "knowledge": existing,
                    **_workspace_publication_scope(),
                    "explanatoryDiagnostics": explanatory_diagnostics,
                    "idempotent": not head_publication["changed"],
                    "applied": False,
                    "head": {
                        key: head_receipt[key]
                        for key in (
                            "headKey",
                            "generation",
                            "knowledgeId",
                            "publishedAt",
                        )
                    },
                    "headReceipt": head_receipt,
                    "headChanged": head_publication["changed"],
                }
            if len(manifest["knowledge"]["workspaces"]) >= MAX_KNOWLEDGE_COUNT:
                raise ReplayProjectError("workspace knowledge limit reached")
            atomic_write_bytes(destination, source_bytes)
            compiled_record: dict[str, Any] | None = None
            if prepared_compiled is not None:
                compiled_identity, compiled_bytes = prepared_compiled
                binary_root = self.root / "knowledge" / "workspaces" / "binaries"
                binary_root.mkdir(parents=True, exist_ok=True)
                if binary_root.is_symlink() or not binary_root.is_dir():
                    raise ReplayProjectError(
                        "project compiled-candidate directory is redirected"
                    )
                compiled_relative = (
                    "knowledge/workspaces/binaries/%s.bin"
                    % compiled_identity["rawSha256"]
                )
                compiled_destination = self._artifact(
                    compiled_relative, "workspace compiled candidate path"
                )
                if compiled_destination.exists() or compiled_destination.is_symlink():
                    if compiled_destination.is_symlink() or _file_bytes(
                        compiled_destination,
                        MAX_COMPILED_CANDIDATE_BYTES,
                        "project compiled candidate",
                    ) != compiled_bytes:
                        raise ReplayProjectError(
                            "existing content-addressed compiled candidate changed"
                        )
                else:
                    atomic_write_bytes(compiled_destination, compiled_bytes)
                compiled_record = {
                    **compiled_identity,
                    "artifactPath": compiled_relative,
                }
            if analysis_bytes is not None:
                analysis_destination = self._artifact(
                    analysis_relative, "workspace analysis knowledge path"
                )
                if analysis_destination.exists():
                    if _sha256_bytes(
                        _file_bytes(
                            analysis_destination,
                            MAX_ANALYSIS_BYTES,
                            "workspace analysis",
                        )
                    ) != analysis_sha256:
                        raise ReplayProjectError(
                            "existing content-addressed analysis changed"
                        )
                else:
                    atomic_write_bytes(analysis_destination, analysis_bytes)
            disassembly_records: list[dict[str, Any]] = []
            for identity, payload in prepared_disassembly:
                disassembly_relative = _assembly_disassembly_relative(identity)
                disassembly_destination = self._artifact(
                    disassembly_relative, "workspace disassembly knowledge path"
                )
                disassembly_destination.parent.mkdir(parents=True, exist_ok=True)
                if (
                    disassembly_destination.parent.is_symlink()
                    or not disassembly_destination.parent.is_dir()
                ):
                    raise ReplayProjectError(
                        "project disassembly directory is redirected"
                    )
                if (
                    disassembly_destination.exists()
                    or disassembly_destination.is_symlink()
                ):
                    if (
                        disassembly_destination.is_symlink()
                        or _file_bytes(
                            disassembly_destination,
                            MAX_SOURCE_BYTES,
                            "project disassembly artifact",
                        )
                        != payload
                    ):
                        raise ReplayProjectError(
                            "existing content-addressed disassembly changed"
                        )
                else:
                    atomic_write_bytes(disassembly_destination, payload)
                disassembly_records.append(
                    {**identity, "artifactPath": disassembly_relative}
                )
            record = dict(material)
            if compiled_record is not None:
                record["compiledCandidate"] = compiled_record
            if disassembly_records:
                record["assemblyDisassemblyArtifacts"] = disassembly_records
            record.update({
                "knowledgeId": knowledge_id,
                "sourcePath": relative,
                "sourceOriginPath": str(source_path),
                "workspacePath": str(status.get("workspacePath", "")),
                "workspaceManifestSha256": _sha256_bytes(manifest_bytes),
                "registeredAt": _utc_now(),
            })
            if analysis_bytes is not None:
                record.update(
                    {
                        "analysisPath": analysis_relative,
                        "analysisKind": "ai-explanatory-text",
                        "analysisPolicy": {
                            "physicalEvidence": False,
                            "applyAuthorization": False,
                            "reviewRequired": True,
                        },
                    }
                )
            manifest["knowledge"]["workspaces"].append(record)
            self._activate_knowledge(manifest, "workspaces", knowledge_id)
            self._apply_head_publication(
                manifest, "workspaces", head_publication
            )
            self._write(manifest)
            head_receipt = self._head_receipt("workspaces", head_publication)
            return {
                "knowledge": record,
                **_workspace_publication_scope(),
                "explanatoryDiagnostics": explanatory_diagnostics,
                "idempotent": False,
                "applied": False,
                "head": {
                    key: head_receipt[key]
                    for key in (
                        "headKey",
                        "generation",
                        "knowledgeId",
                        "publishedAt",
                    )
                },
                "headReceipt": head_receipt,
                "headChanged": True,
            }

    @staticmethod
    def _workspace_inventory_abi_matches(status: Mapping[str, Any], target_hash: str) -> bool:
        """Only complete, self-consistent workspace evidence proves a hash alias."""
        baseline = status.get("baseline") or {}
        digest = _sha(baseline.get("abiSha256"), "workspace.abiSha256", allow_empty=True)
        if digest == target_hash:
            return True
        if not digest or baseline.get("compiledInterface") is None:
            return False
        from .shader_workspace.abi import parse_shader_abi, physical_shader_abi_hashes
        try:
            abi = parse_shader_abi(
                baseline["compiledInterface"], expected_stage=str(status.get("stage") or ""),
                expected_entry_point=str(status.get("entryPoint") or ""))
        except Exception as error:
            raise ReplayProjectError("workspace physical ABI evidence is invalid") from error
        if abi.sha256 != digest:
            raise ReplayProjectError("workspace physical ABI evidence/hash differs")
        return target_hash in physical_shader_abi_hashes(abi)

    def register_workspace_from_inventory(
        self,
        status: Mapping[str, Any],
        target_shader: Mapping[str, Any],
        inventory: Mapping[str, Any],
        expected_binding: Mapping[str, Any] | None = None,
        expected_head: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Publish current HLSL/binary evidence without importing a target catalog."""

        normalized = _normalise_shader_inventory(inventory)
        exact_targets = [
            shader
            for shader in normalized["shaders"]
            if isinstance(target_shader, Mapping) and dict(target_shader) == shader
        ]
        if len(exact_targets) != 1:
            raise ReplayProjectError(
                "target Shader is not one exact normalized inventory record"
            )
        target = exact_targets[0]
        if (
            not isinstance(status, Mapping)
            or status.get("schema")
            != "renderdoc-ultra-shader-workspace-status/v1"
        ):
            raise ReplayProjectError(
                "workspace status must come from strict workspace validation"
            )
        source = status.get("source")
        capture_status = status.get("capture")
        shader_status = status.get("shader")
        baseline = status.get("baseline")
        if not all(
            isinstance(value, Mapping)
            for value in (source, capture_status, shader_status, baseline)
        ):
            raise ReplayProjectError("workspace status provenance is incomplete")
        if (
            source.get("present") is not True
            or source.get("lane") != "executable-editable"
            or source.get("format") not in {"hlsl", "glsl"}
        ):
            raise ReplayProjectError(
                "inventory workspace registration requires executable-editable HLSL or GLSL"
            )
        if (
            _positive(shader_status.get("resourceId"), "workspace.resourceId")
            != target["resourceId"]
            or _normalised_stage(status.get("stage")) != target["stage"]
            or _text(
                str(status.get("entryPoint", "") or ""),
                "workspace.entryPoint",
                512,
                allow_empty=True,
            )
            != target["entryPoint"]
            or _sha(baseline.get("rawSha256"), "workspace.rawSha256")
            != target["rawSha256"]
            or not self._workspace_inventory_abi_matches(status, target["abiHash"])
            or _shader_encoding(baseline.get("kind")) != target["encoding"]
        ):
            raise ReplayProjectError(
                "workspace physical Shader identity differs from inventory"
            )
        if not target["interfaceComplete"] or not target["abiHash"]:
            raise ReplayProjectError(
                "inventory workspace registration requires a complete physical ABI"
            )
        capture_api = str(capture_status.get("api", "") or "")
        if capture_api and _reuse_api_family(capture_api) != _reuse_api_family(target["api"]):
            raise ReplayProjectError("workspace capture API differs from inventory")
        source_path = _normal_path(str(source.get("path", "")))
        source_bytes = _file_bytes(
            source_path, MAX_SOURCE_BYTES, "workspace source"
        )
        if _sha(
            source.get("currentSha256"), "workspace.source.currentSha256"
        ) != _sha256_bytes(source_bytes):
            raise ReplayProjectError(
                "workspace source changed after strict status validation"
            )
        workspace_root = _normal_path(str(status.get("workspacePath", "")))
        if not is_within(workspace_root, source_path):
            raise ReplayProjectError("workspace source escapes its workspace")
        analysis_path = workspace_root / "ai" / "analysis.md"
        analysis_bytes: bytes | None = None
        analysis_sha256 = ""
        explanatory_diagnostics: list[dict[str, str]] = []
        if analysis_path.exists() or analysis_path.is_symlink():
            if not is_within(workspace_root, analysis_path.resolve()):
                raise ReplayProjectError("workspace analysis escapes its workspace")
            analysis_bytes = _file_bytes(
                analysis_path, MAX_ANALYSIS_BYTES, "workspace ai/analysis.md"
            )
            provenance = source_binding_metadata(workspace_root, analysis_path, analysis_bytes, status)
            if provenance["currentSourceMatches"] is False:
                explanatory_diagnostics.append({"artifact": "ai/analysis.md", "status": "skipped",
                                                "reason": provenance["reason"]})
                analysis_bytes, analysis_sha256 = None, ""
            else:
                try:
                    analysis_text = analysis_bytes.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise ReplayProjectError("workspace analysis must be UTF-8 text") from exc
                if "\x00" in analysis_text:
                    raise ReplayProjectError("workspace analysis contains NUL characters")
                analysis_sha256 = _sha256_bytes(analysis_bytes)
        manifest_path = _normal_path(str(status.get("manifestPath", "")))
        manifest_bytes = _file_bytes(
            manifest_path, MAX_PROJECT_BYTES, "workspace manifest"
        )
        workspace_manifest_sha256 = _sha256_bytes(manifest_bytes)
        compiler_binding = (
            _compiler_binding_identity(status.get("compilerBinding"))
            if status.get("compilerBinding") is not None
            else None
        )
        if (
            compiler_binding is not None
            and compiler_binding["targetProfile"] != status.get("targetProfile")
        ):
            raise ReplayProjectError(
                "workspace compiler binding target profile differs"
            )
        raw_disassembly = status.get("assemblyDisassemblyArtifacts", [])
        if not isinstance(raw_disassembly, list) or len(raw_disassembly) > 2:
            raise ReplayProjectError(
                "workspace assembly/disassembly artifacts are invalid"
            )
        prepared_disassembly: list[tuple[dict[str, Any], bytes]] = []
        seen_roles: set[str] = set()
        for raw_artifact in raw_disassembly:
            identity = _assembly_disassembly_identity(raw_artifact)
            if identity["role"] in seen_roles:
                raise ReplayProjectError(
                    "workspace has duplicate disassembly evidence roles"
                )
            if (
                identity["kind"] == "native-disassembly"
                and (
                    identity["role"] != "baseline"
                    or identity["inputRawSha256"] != target["rawSha256"]
                )
            ):
                raise ReplayProjectError(
                    "workspace native disassembly physical binding mismatch"
                )
            unresolved_path = Path(
                str(raw_artifact.get("path", ""))
            ).expanduser()
            if unresolved_path.is_symlink():
                raise ReplayProjectError(
                    "workspace disassembly artifact must not be a symlink"
                )
            payload = _file_bytes(
                unresolved_path.resolve(),
                MAX_SOURCE_BYTES,
                "workspace disassembly artifact",
            )
            if (
                len(payload) != identity["byteSize"]
                or _sha256_bytes(payload) != identity["sha256"]
            ):
                raise ReplayProjectError(
                    "workspace disassembly artifact content/hash mismatch"
                )
            prepared_disassembly.append((identity, payload))
            seen_roles.add(identity["role"])
        prepared_compiled = self._validated_compiled_candidate(
            status,
            source_bytes=source_bytes,
            workspace_root=workspace_root,
            manifest_bytes=manifest_bytes,
            baseline=baseline,
        )

        cache_diagnostics: dict[str, Any] = {}
        if baseline.get("abiSha256") != target["abiHash"]:
            # The fresh inventory is the new knowledge's exact physical origin.
            # A historical binary report still binds the old spelling. Keep it
            # in its original workspace, but do not rewrite/promote its origin
            # into this new record. Source reuse can compile and validate anew.
            prepared_compiled = None
            cache_diagnostics = {"compiledCandidateCache": {
                "retained": False,
                "reasonId": "historical-topology-abi-recompile-required",
            }}

        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load()
            before_snapshot = self._reuse_snapshot_for_normalized_inventory(
                manifest, normalized
            )
            if expected_binding is not None and (
                not isinstance(expected_binding, Mapping)
                or dict(expected_binding) != before_snapshot["binding"]
            ):
                raise ReplayProjectError(
                    "shader inventory project binding is stale or changed"
                )
            inventory_origin = {
                "schema": "renderdoc-ultra-project-shader-inventory-origin/v1",
                "schemaVersion": 1,
                **{
                    key: target[key]
                    for key in (
                        "api",
                        "encoding",
                        "rawSha256",
                        "stage",
                        "entryPoint",
                        "abiHash",
                        "layoutHash",
                        "stableKeySha256",
                    )
                },
            }
            material: dict[str, Any] = {
                "workspaceId": _text(
                    str(status.get("workspaceId", "")), "workspaceId", 64
                ),
                "captureId": normalized["capture"]["captureId"],
                "originCatalogSha256": normalized["capture"]["inventorySha256"],
                "stage": target["stage"],
                "entryPoint": target["entryPoint"],
                "rawSha256": target["rawSha256"],
                "codeSha256": _sha(
                    baseline.get("codeSha256"),
                    "workspace.codeSha256",
                    allow_empty=True,
                ),
                "layoutHash": target["layoutHash"],
                "interfaceHash": target["interfaceHash"],
                "interfaceComplete": target["interfaceComplete"],
                "baselineAbiSha256": target["abiHash"],
                "targetProfile": copy.deepcopy(status.get("targetProfile")),
                "sourceSha256": _sha256_bytes(source_bytes),
                "analysisSha256": analysis_sha256,
                "inventoryOrigin": inventory_origin,
            }
            if source["format"] != "hlsl":
                material["sourceFormat"] = source["format"]
            if compiler_binding is not None:
                material["compilerBinding"] = compiler_binding
            if prepared_disassembly:
                material["assemblyDisassemblyArtifacts"] = [
                    identity for identity, unused_payload in prepared_disassembly
                ]
            if prepared_compiled is not None:
                compiled_identity, unused_compiled = prepared_compiled
                material["compiledCandidate"] = compiled_identity
            knowledge_id = _canonical_sha256(
                _inventory_workspace_artifact_identity(material)
            )[:32]
            observation_material = {
                "schema": "renderdoc-ultra-project-shader-inventory-observation/v1",
                "schemaVersion": 1,
                "captureId": normalized["capture"]["captureId"],
                "inventorySha256": normalized["capture"]["inventorySha256"],
                "stableKeySha256": target["stableKeySha256"],
                "targetResourceId": target["resourceId"],
                "anchorEventId": target["anchorEventId"],
                "constantBlocksSha256": _canonical_sha256(
                    target["constantBlocks"]
                ),
                "workspaceManifestSha256": workspace_manifest_sha256,
                "sourceSha256": material["sourceSha256"],
                "compiledCandidateSha256": (
                    material["compiledCandidate"]["rawSha256"]
                    if "compiledCandidate" in material
                    else ""
                ),
                "applyAuthorization": False,
                "crossCaptureResourceIdReuse": False,
            }
            observation = _inventory_observation(
                {
                    **observation_material,
                    "observedAt": _utc_now(),
                    "receiptSha256": _canonical_sha256(observation_material),
                }
            )
            source_relative = "knowledge/workspaces/%s.%s" % (knowledge_id, source["format"])
            destination = self._artifact(
                source_relative, "workspace knowledge path"
            )
            existing = next(
                (
                    item
                    for item in manifest["knowledge"]["workspaces"]
                    if item.get("knowledgeId") == knowledge_id
                ),
                None,
            )
            head_key = self._workspace_head_key(material, manifest)
            current_head = self._active_heads_state(manifest)[
                "workspaces"
            ].get(head_key)
            derived_expected_head = expected_head
            if expected_binding is not None and derived_expected_head is None:
                derived_expected_head = (
                    {
                        "headKey": head_key,
                        "generation": current_head["generation"],
                        "knowledgeId": current_head["knowledgeId"],
                    }
                    if current_head is not None
                    else None
                )
            head_publication = self._plan_head_publication(
                manifest,
                "workspaces",
                head_key,
                knowledge_id,
                record_preexisting=existing is not None,
                expected_head=derived_expected_head,
            )
            if head_publication["changed"] and expected_binding is None:
                raise ReplayProjectError(
                    "inventory workspace head publication requires expected_binding"
                )
            if existing is not None:
                if _sha256_bytes(
                    _file_bytes(destination, MAX_SOURCE_BYTES, "workspace source")
                ) != material["sourceSha256"]:
                    raise ReplayProjectError(
                        "existing workspace knowledge artifact changed"
                    )
                observations = existing.get("inventoryObservations")
                if (
                    not isinstance(observations, list)
                    or len(observations) > MAX_INVENTORY_OBSERVATIONS
                ):
                    raise ReplayProjectError(
                        "workspace inventory observations are invalid or oversized"
                    )
                receipts = {
                    _inventory_observation(item)["receiptSha256"]
                    for item in observations
                }
                observation_recorded = observation["receiptSha256"] not in receipts
                if observation_recorded:
                    if len(observations) >= MAX_INVENTORY_OBSERVATIONS:
                        raise ReplayProjectError(
                            "workspace inventory observation limit reached"
                        )
                    observations.append(observation)
                if head_publication["changed"]:
                    self._activate_knowledge(
                        manifest, "workspaces", knowledge_id
                    )
                    self._apply_head_publication(
                        manifest, "workspaces", head_publication
                    )
                if observation_recorded or head_publication["changed"]:
                    self._write(manifest)
                after_snapshot = self._reuse_snapshot_for_normalized_inventory(
                    manifest, normalized
                )
                head_receipt = self._head_receipt(
                    "workspaces", head_publication
                )
                return {
                    "knowledge": copy.deepcopy(existing),
                    **cache_diagnostics,
                    "explanatoryDiagnostics": explanatory_diagnostics,
                    "idempotent": not head_publication["changed"],
                    "applied": False,
                    "observationRecorded": observation_recorded,
                    "observation": copy.deepcopy(observation),
                    "binding": after_snapshot["binding"],
                    "snapshotSha256": after_snapshot["snapshotSha256"],
                    "head": {
                        key: head_receipt[key]
                        for key in (
                            "headKey",
                            "generation",
                            "knowledgeId",
                            "publishedAt",
                        )
                    },
                    "headReceipt": head_receipt,
                    "headChanged": head_publication["changed"],
                    "headCasDerivedFromBinding": expected_binding is not None,
                }
            if len(manifest["knowledge"]["workspaces"]) >= MAX_KNOWLEDGE_COUNT:
                raise ReplayProjectError("workspace knowledge limit reached")
            atomic_write_bytes(destination, source_bytes)
            compiled_record: dict[str, Any] | None = None
            if prepared_compiled is not None:
                compiled_identity, compiled_bytes = prepared_compiled
                binary_root = self.root / "knowledge" / "workspaces" / "binaries"
                binary_root.mkdir(parents=True, exist_ok=True)
                if binary_root.is_symlink() or not binary_root.is_dir():
                    raise ReplayProjectError(
                        "project compiled-candidate directory is redirected"
                    )
                compiled_relative = (
                    "knowledge/workspaces/binaries/%s.bin"
                    % compiled_identity["rawSha256"]
                )
                compiled_destination = self._artifact(
                    compiled_relative, "workspace compiled candidate path"
                )
                if (
                    compiled_destination.exists()
                    or compiled_destination.is_symlink()
                ):
                    if (
                        compiled_destination.is_symlink()
                        or _file_bytes(
                            compiled_destination,
                            MAX_COMPILED_CANDIDATE_BYTES,
                            "project compiled candidate",
                        )
                        != compiled_bytes
                    ):
                        raise ReplayProjectError(
                            "existing content-addressed compiled candidate changed"
                        )
                else:
                    atomic_write_bytes(compiled_destination, compiled_bytes)
                compiled_record = {
                    **compiled_identity,
                    "artifactPath": compiled_relative,
                }
            if analysis_bytes is not None:
                analysis_relative = (
                    "knowledge/workspaces/%s.analysis.md" % analysis_sha256
                )
                analysis_destination = self._artifact(
                    analysis_relative, "workspace analysis knowledge path"
                )
                if analysis_destination.exists():
                    if _sha256_bytes(
                        _file_bytes(
                            analysis_destination,
                            MAX_ANALYSIS_BYTES,
                            "workspace analysis",
                        )
                    ) != analysis_sha256:
                        raise ReplayProjectError(
                            "existing content-addressed analysis changed"
                        )
                else:
                    atomic_write_bytes(analysis_destination, analysis_bytes)
            disassembly_records: list[dict[str, Any]] = []
            for identity, payload in prepared_disassembly:
                relative = _assembly_disassembly_relative(identity)
                artifact = self._artifact(
                    relative, "workspace disassembly knowledge path"
                )
                artifact.parent.mkdir(parents=True, exist_ok=True)
                if artifact.parent.is_symlink() or not artifact.parent.is_dir():
                    raise ReplayProjectError(
                        "project disassembly directory is redirected"
                    )
                if artifact.exists() or artifact.is_symlink():
                    if artifact.is_symlink() or _file_bytes(
                        artifact,
                        MAX_SOURCE_BYTES,
                        "project disassembly artifact",
                    ) != payload:
                        raise ReplayProjectError(
                            "existing content-addressed disassembly changed"
                        )
                else:
                    atomic_write_bytes(artifact, payload)
                disassembly_records.append({**identity, "artifactPath": relative})
            record = dict(material)
            if compiled_record is not None:
                record["compiledCandidate"] = compiled_record
            if disassembly_records:
                record["assemblyDisassemblyArtifacts"] = disassembly_records
            record.update(
                {
                    "knowledgeId": knowledge_id,
                    "inventoryObservations": [observation],
                    "sourcePath": source_relative,
                    "sourceOriginPath": str(source_path),
                    "workspacePath": str(workspace_root),
                    "workspaceManifestSha256": workspace_manifest_sha256,
                    "registeredAt": _utc_now(),
                }
            )
            if analysis_bytes is not None:
                record.update(
                    {
                        "analysisPath": analysis_relative,
                        "analysisKind": "ai-explanatory-text",
                        "analysisPolicy": {
                            "physicalEvidence": False,
                            "applyAuthorization": False,
                            "reviewRequired": True,
                        },
                    }
                )
            manifest["knowledge"]["workspaces"].append(record)
            self._activate_knowledge(manifest, "workspaces", knowledge_id)
            self._apply_head_publication(
                manifest, "workspaces", head_publication
            )
            self._write(manifest)
            after_snapshot = self._reuse_snapshot_for_normalized_inventory(
                manifest, normalized
            )
            head_receipt = self._head_receipt("workspaces", head_publication)
            return {
                "knowledge": copy.deepcopy(record),
                **cache_diagnostics,
                "explanatoryDiagnostics": explanatory_diagnostics,
                "idempotent": False,
                "applied": False,
                "observationRecorded": True,
                "observation": copy.deepcopy(observation),
                "binding": after_snapshot["binding"],
                "snapshotSha256": after_snapshot["snapshotSha256"],
                "head": {
                    key: head_receipt[key]
                    for key in (
                        "headKey",
                        "generation",
                        "knowledgeId",
                        "publishedAt",
                    )
                },
                "headReceipt": head_receipt,
                "headChanged": True,
                "headCasDerivedFromBinding": True,
            }

    def register_alias_table(
        self,
        alias_table_path: str | Path,
        expected_head: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        source = _normal_path(alias_table_path)
        raw = _file_bytes(source, MAX_ALIAS_BYTES, "alias table")
        try:
            table = json.loads(raw.decode("utf-8-sig", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayProjectError("alias table is not UTF-8 JSON") from exc
        if not isinstance(table, dict) or table.get("schema") != ALIAS_SCHEMA or table.get("schemaVersion") != ALIAS_VERSION or table.get("verifiedOnly") is not True:
            raise ReplayProjectError("only a verified exported alias table can enter a project")
        aliases = table.get("aliases")
        if not isinstance(aliases, list) or len(aliases) > MAX_ALIASES:
            raise ReplayProjectError("alias table aliases are invalid or oversized")
        capture_id = _sha(table.get("captureId"), "alias.captureId")
        alias_catalog_text = str(table.get("catalogSha256", "") or "")
        alias_catalog_sha256 = (
            _sha(alias_catalog_text, "alias.catalogSha256")
            if alias_catalog_text
            else ""
        )
        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load()
            capture = self._capture_for_provenance(
                manifest, capture_id, alias_catalog_sha256
            )
            aliases, alias_recovery = _recover_legacy_alias_rows(aliases, capture)
            recovery_result = ({"aliasRecovery": alias_recovery} if alias_recovery else {})
            if alias_recovery and not aliases:
                return {
                    "knowledge": None, "registered": False, "applied": False,
                    "idempotent": True, "headChanged": False,
                    "aliasCount": 0, **recovery_result,
                }
            groups: dict[tuple[int, str, str, str], dict[str, Any]] = {}
            for index, alias in enumerate(aliases):
                if not isinstance(alias, Mapping) or alias.get("verified") is not True or alias.get("captureId") != capture_id:
                    raise ReplayProjectError("aliases[%d] is not verified/capture-bound" % index)
                resource = _positive(alias.get("shaderResourceId"), "alias.shaderResourceId")
                stage = _text(str(alias.get("stage", "")), "alias.stage", 64)
                entry = _text(str(alias.get("entryPoint", "") or ""), "alias.entryPoint", 512, True)
                raw_hash = _sha(alias.get("rawSha256"), "alias.rawSha256")
                shader = self._capture_shader(capture, resource_id=resource, stage=stage, entry_point=entry, raw_sha256=raw_hash)
                code_hash = _sha(alias.get("codeSha256"), "alias.codeSha256", True)
                if code_hash and code_hash != shader.get("codeSha256"):
                    raise ReplayProjectError("alias codeSha256 differs from imported capture")
                key = (resource, stage, entry, raw_hash)
                group = groups.setdefault(key, {
                    "stage": stage,
                    "entryPoint": entry,
                    "rawSha256": raw_hash,
                    "codeSha256": shader.get("codeSha256", ""),
                    "layoutHash": shader.get("layoutHash", ""),
                    "interfaceHash": shader.get("interfaceHash", ""),
                    "interfaceComplete": shader.get("interfaceComplete") is True,
                    "aliases": [],
                })
                compact = {
                    "bindingKind": _text(str(alias.get("bindingKind", "")), "alias.bindingKind", 64),
                    "registerSpace": int(alias.get("registerSpace", 0)),
                    "bindNumber": int(alias.get("bindNumber", 0)),
                    "byteOffset": int(alias.get("byteOffset", 0)),
                    "canonicalPath": _text(str(alias.get("canonicalPath", "") or ""), "alias.canonicalPath", 2048, True),
                    "alias": _text(str(alias.get("alias", "")), "alias.alias", 1024),
                }
                if min(compact["registerSpace"], compact["bindNumber"], compact["byteOffset"]) < 0:
                    raise ReplayProjectError("alias physical coordinates must be non-negative")
                group["aliases"].append(compact)
            group_values = list(groups.values())
            identity = {
                "captureId": capture_id,
                "originCatalogSha256": capture["catalogSha256"],
                "groups": group_values,
                "artifactSha256": _sha256_bytes(raw),
            }
            knowledge_id = _canonical_sha256(identity)[:32]
            relative = "knowledge/aliases/%s.json" % knowledge_id
            destination = self._artifact(relative, "alias knowledge path")
            existing = next((item for item in manifest["knowledge"]["aliases"] if item["knowledgeId"] == knowledge_id), None)
            alias_provenance = {
                "captureId": capture_id,
                "originCatalogSha256": capture["catalogSha256"],
            }
            head_keys = [
                self._alias_head_key(
                    group, manifest=manifest, provenance=alias_provenance
                )
                for group in group_values
            ]
            expected_heads = self._expected_heads_for_keys(
                expected_head, head_keys
            )
            head_publications = [
                self._plan_head_publication(
                    manifest,
                    "aliases",
                    head_key,
                    knowledge_id,
                    record_preexisting=existing is not None,
                    expected_head=expected_heads[head_key],
                )
                for head_key in head_keys
            ]
            if existing is not None:
                changed = any(item["changed"] for item in head_publications)
                if changed:
                    for publication in head_publications:
                        self._apply_head_publication(
                            manifest, "aliases", publication
                        )
                    self._write(manifest)
                head_receipts = [
                    self._head_receipt("aliases", item)
                    for item in head_publications
                ]
                result = {
                    "knowledge": existing,
                    **recovery_result,
                    "idempotent": not changed,
                    "applied": False,
                    "heads": [
                        {
                            key: receipt[key]
                            for key in (
                                "headKey",
                                "generation",
                                "knowledgeId",
                                "publishedAt",
                            )
                        }
                        for receipt in head_receipts
                    ],
                    "headReceipts": head_receipts,
                    "headChanged": changed,
                }
                if len(head_receipts) == 1:
                    result["head"] = result["heads"][0]
                    result["headReceipt"] = head_receipts[0]
                return result
            if len(manifest["knowledge"]["aliases"]) >= MAX_KNOWLEDGE_COUNT:
                raise ReplayProjectError("alias knowledge limit reached")
            atomic_write_bytes(destination, raw)
            record = {
                "knowledgeId": knowledge_id,
                "captureId": capture_id,
                "originCatalogSha256": capture["catalogSha256"],
                "artifactPath": relative,
                "artifactSha256": _sha256_bytes(raw),
                "groups": group_values,
                "aliasCount": len(aliases),
                "registeredAt": _utc_now(),
            }
            manifest["knowledge"]["aliases"].append(record)
            for publication in head_publications:
                self._apply_head_publication(
                    manifest, "aliases", publication
                )
            self._write(manifest)
            head_receipts = [
                self._head_receipt("aliases", item)
                for item in head_publications
            ]
            result = {
                "knowledge": record,
                **recovery_result,
                "idempotent": False,
                "applied": False,
                "heads": [
                    {
                        key: receipt[key]
                        for key in (
                            "headKey",
                            "generation",
                            "knowledgeId",
                            "publishedAt",
                        )
                    }
                    for receipt in head_receipts
                ],
                "headReceipts": head_receipts,
                "headChanged": bool(head_receipts),
            }
            if len(head_receipts) == 1:
                result["head"] = result["heads"][0]
                result["headReceipt"] = head_receipts[0]
            return result

    @staticmethod
    def _live_material_parameters(session: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_parameters = session.get("parameters")
        if (
            not isinstance(raw_parameters, list)
            or not raw_parameters
            or len(raw_parameters) > MAX_MATERIAL_PARAMETERS
        ):
            raise ReplayProjectError("material session parameters are invalid")
        parameters: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_typed: set[tuple[str, str, int]] = set()
        for index, raw_parameter in enumerate(raw_parameters):
            if not isinstance(raw_parameter, Mapping):
                raise ReplayProjectError(
                    "material session parameter[%d] is invalid" % index
                )
            parameter_id = str(raw_parameter.get("parameterId", "") or "").lower()
            if not HEX32_RE.fullmatch(parameter_id) or parameter_id in seen_ids:
                raise ReplayProjectError(
                    "material session parameter ID is invalid or duplicated"
                )
            path = _text(
                str(raw_parameter.get("path", "")),
                "material.path",
                2048,
            )
            base_type = _text(
                str(raw_parameter.get("baseType", "")),
                "material.baseType",
                32,
            )
            if base_type not in {"Float", "SInt", "UInt"}:
                raise ReplayProjectError("material parameter baseType is unsupported")
            components = _positive(
                raw_parameter.get("components"), "material.components"
            )
            if components > 4:
                raise ReplayProjectError("material parameter components exceeds vec4")
            typed = (path, base_type, components)
            if typed in seen_typed:
                raise ReplayProjectError(
                    "material session contains duplicate typed parameter paths"
                )
            value = raw_parameter.get("value")
            values = value if isinstance(value, list) else [value]
            if len(values) != components:
                raise ReplayProjectError(
                    "material parameter value shape differs from components"
                )
            if base_type == "Float":
                if any(
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not math.isfinite(float(number))
                    or abs(float(number)) > 3.4028234663852886e38
                    for number in values
                ):
                    raise ReplayProjectError(
                        "material Float value is not finite float32 data"
                    )
            else:
                lower, upper = (
                    (-2147483648, 2147483647)
                    if base_type == "SInt"
                    else (0, 4294967295)
                )
                if any(
                    isinstance(number, bool)
                    or not isinstance(number, int)
                    or number < lower
                    or number > upper
                    for number in values
                ):
                    raise ReplayProjectError(
                        "material integer value is outside its reflected 32-bit type"
                    )
            seen_ids.add(parameter_id)
            seen_typed.add(typed)
            parameters.append(
                {
                    "parameterId": parameter_id,
                    "path": path,
                    "baseType": base_type,
                    "components": components,
                    "value": copy.deepcopy(value),
                }
            )
        return parameters

    def save_material_knowledge(
        self,
        session: Mapping[str, Any],
        parameter_ids: Sequence[str],
        expected_head: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(session, Mapping) or session.get("schema") != "renderdoc-ultra-material-session/v1":
            raise ReplayProjectError("material status must come from a live material session")
        requested = [str(value or "").strip().lower() for value in parameter_ids]
        if not requested or len(requested) > MAX_MATERIAL_PARAMETERS or len(set(requested)) != len(requested):
            raise ReplayProjectError("parameter_ids must contain unique bounded parameter IDs")
        live_parameters = self._live_material_parameters(session)
        available = {item["parameterId"]: item for item in live_parameters}
        if any(value not in available for value in requested):
            raise ReplayProjectError("parameter_ids contains an unknown material parameter")
        capture_id = _sha(session.get("captureId"), "material.captureId")
        material_catalog_text = str(session.get("catalogSha256", "") or "")
        material_catalog_sha256 = (
            _sha(material_catalog_text, "material.catalogSha256")
            if material_catalog_text
            else ""
        )
        resource = _positive(session.get("shaderResourceId"), "material.shaderResourceId")
        stage = _text(str(session.get("stage", "")), "material.stage", 64)
        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load()
            capture = self._capture_for_provenance(
                manifest, capture_id, material_catalog_sha256
            )
            matches = [item for item in capture["shaders"] if item["resourceId"] == resource and item["stage"] == stage]
            if len(matches) != 1:
                raise ReplayProjectError("material session does not select one imported shader")
            shader = matches[0]
            block_path = _text(
                str(session.get("constantBlockPath", "")),
                "material.constantBlockPath",
                2048,
            )
            block_layout_sha256 = _sha(
                session.get("layoutSha256"), "material.layoutSha256"
            )
            material_head_identity = {
                "captureId": capture_id,
                "originCatalogSha256": capture["catalogSha256"],
                "originResourceId": resource,
                "stage": shader["stage"],
                "entryPoint": shader.get("entryPoint", ""),
                "rawSha256": shader.get("rawSha256", ""),
                "codeSha256": shader.get("codeSha256", ""),
                "layoutHash": shader.get("layoutHash", ""),
                "interfaceHash": shader.get("interfaceHash", ""),
                "interfaceComplete": shader.get("interfaceComplete") is True,
                "constantBlockPath": block_path,
                "constantBlockLayoutSha256": block_layout_sha256,
            }
            head_key = self._material_head_key(
                material_head_identity, manifest
            )
            current_head = self._active_heads_state(manifest)["materials"].get(
                head_key
            )

            live_by_typed = {
                (item["path"], item["baseType"], item["components"]): item
                for item in live_parameters
            }
            merged: dict[tuple[str, str, int], dict[str, Any]] = {}
            if current_head is not None:
                head_id = current_head["knowledgeId"]
                head = self._material_knowledge_from_manifest(manifest, head_id)
                for saved in head["parameters"]:
                    typed = (
                        saved["path"],
                        saved["baseType"],
                        saved["components"],
                    )
                    live_parameter = live_by_typed.get(typed)
                    if (
                        not isinstance(live_parameter, Mapping)
                        or live_parameter.get("parameterId")
                        != saved.get("parameterId")
                    ):
                        raise ReplayProjectError(
                            "durable material head differs from the exact live layout"
                        )
                    merged[typed] = {
                        "parameterId": live_parameter["parameterId"],
                        "path": live_parameter["path"],
                        "baseType": live_parameter["baseType"],
                        "components": live_parameter["components"],
                        "value": copy.deepcopy(saved["value"]),
                    }

            for parameter_id in requested:
                item = available[parameter_id]
                typed = (item["path"], item["baseType"], item["components"])
                merged[typed] = copy.deepcopy(item)
            parameters = [
                merged[(item["path"], item["baseType"], item["components"])]
                for item in live_parameters
                if (item["path"], item["baseType"], item["components"])
                in merged
            ]
            material = {
                "captureId": capture_id,
                "originCatalogSha256": capture["catalogSha256"],
                "originResourceId": resource,
                "stage": shader["stage"],
                "entryPoint": shader.get("entryPoint", ""),
                "rawSha256": shader.get("rawSha256", ""),
                "codeSha256": shader.get("codeSha256", ""),
                "layoutHash": shader.get("layoutHash", ""),
                "interfaceHash": shader.get("interfaceHash", ""),
                "interfaceComplete": shader.get("interfaceComplete") is True,
                "constantBlockPath": block_path,
                "constantBlockLayoutSha256": block_layout_sha256,
                "parameters": parameters,
            }
            knowledge_id = _canonical_sha256(material)[:32]
            existing = next((item for item in manifest["knowledge"]["materials"] if item["knowledgeId"] == knowledge_id), None)
            head_publication = self._plan_head_publication(
                manifest,
                "materials",
                head_key,
                knowledge_id,
                record_preexisting=existing is not None,
                expected_head=expected_head,
            )
            if existing is not None:
                if head_publication["changed"]:
                    self._activate_knowledge(manifest, "materials", knowledge_id)
                    self._apply_head_publication(
                        manifest, "materials", head_publication
                    )
                    self._write(manifest)
                head_receipt = self._head_receipt("materials", head_publication)
                return {
                    "knowledge": existing,
                    "idempotent": not head_publication["changed"],
                    "applied": False,
                    "head": {
                        key: head_receipt[key]
                        for key in (
                            "headKey",
                            "generation",
                            "knowledgeId",
                            "publishedAt",
                        )
                    },
                    "headReceipt": head_receipt,
                    "headChanged": head_publication["changed"],
                }
            if len(manifest["knowledge"]["materials"]) >= MAX_KNOWLEDGE_COUNT:
                raise ReplayProjectError("material knowledge limit reached")
            record = dict(material)
            record.update({"knowledgeId": knowledge_id, "registeredAt": _utc_now()})
            manifest["knowledge"]["materials"].append(record)
            self._activate_knowledge(manifest, "materials", knowledge_id)
            self._apply_head_publication(
                manifest, "materials", head_publication
            )
            self._write(manifest)
            head_receipt = self._head_receipt("materials", head_publication)
            return {
                "knowledge": record,
                "idempotent": False,
                "applied": False,
                "head": {
                    key: head_receipt[key]
                    for key in (
                        "headKey",
                        "generation",
                        "knowledgeId",
                        "publishedAt",
                    )
                },
                "headReceipt": head_receipt,
                "headChanged": True,
            }

    def restore_binding_state(
        self, capture_id: str, catalog_sha256: str
    ) -> dict[str, Any]:
        """Return the exact immutable project hashes used by an RDC hint.

        This is deliberately read-only.  The returned capture/catalog and
        knowledge hashes are recomputed from a fully validated manifest; an
        embedded RDC index cannot supply or relax any of them.
        """

        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load(persist_migrations=True)
            capture = self._capture(manifest, capture_id, catalog_sha256)
            state = self._reuse_knowledge_state(manifest)
            restore_scope = self._capture_referenced_knowledge_scope(capture, state)
            binding = self._reuse_cache_binding(
                manifest, capture, restore_scope["knowledgeContentSha256"]
            )
        return {
            "projectId": binding["projectId"],
            "projectPath": str(self.root),
            "scopeSha256": binding["scopeSha256"],
            "captureId": binding["captureId"],
            "catalogSha256": binding["catalogSha256"],
            "knowledgeContentSha256": binding["knowledgeContentSha256"],
            "matcherPolicySha256": binding["matcherPolicySha256"],
            "capture": copy.deepcopy(capture),
        }

    def _workspace_knowledge_from_manifest(
        self,
        manifest: Mapping[str, Any],
        knowledge_id: str,
        *,
        record: Mapping[str, Any] | None = None,
        source_artifact_verified: bool = False,
        provenance_index: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        token = str(knowledge_id or "").strip().lower()
        if not HEX32_RE.fullmatch(token):
            raise ReplayProjectError(
                "knowledge_id must be 32 lowercase hexadecimal characters"
            )
        if record is None:
            matches = [
                item
                for item in manifest["knowledge"]["workspaces"]
                if isinstance(item, Mapping) and item.get("knowledgeId") == token
            ]
            if len(matches) != 1:
                raise ReplayProjectError(
                    "workspace knowledge_id does not select one project record"
                )
            record = matches[0]
        elif (
            not isinstance(record, Mapping)
            or record.get("knowledgeId") != token
        ):
            raise ReplayProjectError(
                "indexed workspace knowledge differs from its knowledge_id"
            )
        workspace_id = str(record.get("workspaceId", "") or "")
        if not HEX32_RE.fullmatch(workspace_id):
            raise ReplayProjectError("workspace knowledge workspaceId is invalid")
        capture_id = _sha(record.get("captureId"), "workspace.captureId")
        origin_catalog_sha256 = _sha(
            record.get("originCatalogSha256"), "workspace.originCatalogSha256"
        )
        origin_resource_id = (
            _canonical_positive(
                record.get("originResourceId"),
                "workspace.originResourceId",
            )
            if "originResourceId" in record
            else None
        )
        stage = _text(str(record.get("stage", "")), "workspace.stage", 64)
        entry_point = _text(
            str(record.get("entryPoint", "") or ""),
            "workspace.entryPoint",
            512,
            allow_empty=True,
        )
        raw_sha256 = _sha(record.get("rawSha256"), "workspace.rawSha256")
        code_sha256 = _sha(
            record.get("codeSha256"), "workspace.codeSha256", allow_empty=True
        )
        layout_hash = _sha(
            record.get("layoutHash"), "workspace.layoutHash", allow_empty=True
        )
        interface_hash = _sha(
            record.get("interfaceHash"),
            "workspace.interfaceHash",
            allow_empty=True,
        )
        interface_complete = record.get("interfaceComplete") is True
        baseline_abi_sha256 = _sha(
            record.get("baselineAbiSha256"),
            "workspace.baselineAbiSha256",
            allow_empty=True,
        )
        source_sha256 = _sha(
            record.get("sourceSha256"), "workspace.sourceSha256"
        )
        analysis_sha256 = _sha(
            record.get("analysisSha256"),
            "workspace.analysisSha256",
            allow_empty=True,
        )
        compiled_candidate = record.get("compiledCandidate")
        compiled_identity: dict[str, Any] | None = None
        if compiled_candidate is not None:
            compiled_identity = _compiled_candidate_identity(compiled_candidate)
            _check_manual_candidate_original(compiled_identity, record)
            if (
                compiled_identity["sourceSha256"] != source_sha256
                or compiled_identity["targetProfile"]
                != record.get("targetProfile")
                or compiled_identity["originBaselineAbiSha256"]
                != baseline_abi_sha256
            ):
                raise ReplayProjectError(
                    "workspace compiled candidate knowledge binding mismatch"
                )
            expected_compiled_relative = (
                "knowledge/workspaces/binaries/%s.bin"
                % compiled_identity["rawSha256"]
            )
            if compiled_candidate.get("artifactPath") != expected_compiled_relative:
                raise ReplayProjectError(
                    "workspace compiled candidate is not content-addressed"
                )
            compiled_bytes = _file_bytes(
                self._artifact(
                    expected_compiled_relative, "workspace compiled candidate"
                ),
                MAX_COMPILED_CANDIDATE_BYTES,
                "workspace compiled candidate",
            )
            if (
                len(compiled_bytes) != compiled_identity["byteSize"]
                or _sha256_bytes(compiled_bytes)
                != compiled_identity["rawSha256"]
            ):
                raise ReplayProjectError(
                    "workspace compiled candidate content/hash mismatch"
                )
        inventory_origin = record.get("inventoryOrigin")
        inventory_observations: list[dict[str, Any]] = []
        if inventory_origin is not None:
            if origin_resource_id is not None:
                raise ReplayProjectError(
                    "inventory workspace must not claim a capture-local originResourceId"
                )
            origin = _inventory_origin_identity(inventory_origin)
            if any(
                (
                    origin["rawSha256"] != raw_sha256,
                    origin["stage"] != stage,
                    origin["entryPoint"] != entry_point,
                    origin["abiHash"] != baseline_abi_sha256,
                    origin["layoutHash"] != layout_hash,
                )
            ):
                raise ReplayProjectError(
                    "workspace inventory origin differs from project knowledge"
                )
            raw_observations = record.get("inventoryObservations")
            if (
                not isinstance(raw_observations, list)
                or not raw_observations
                or len(raw_observations) > MAX_INVENTORY_OBSERVATIONS
            ):
                raise ReplayProjectError(
                    "workspace inventory observations are invalid or oversized"
                )
            seen_receipts: set[str] = set()
            expected_compiled_sha256 = (
                compiled_identity["rawSha256"]
                if compiled_identity is not None
                else ""
            )
            for raw_observation in raw_observations:
                observation = _inventory_observation(raw_observation)
                if dict(raw_observation) != observation:
                    raise ReplayProjectError(
                        "workspace inventory observation is not canonical"
                    )
                if observation["receiptSha256"] in seen_receipts:
                    raise ReplayProjectError(
                        "workspace inventory observation receipt is duplicated"
                    )
                if (
                    observation["stableKeySha256"]
                    != origin["stableKeySha256"]
                    or observation["sourceSha256"] != source_sha256
                    or observation["compiledCandidateSha256"]
                    != expected_compiled_sha256
                ):
                    raise ReplayProjectError(
                        "workspace inventory observation differs from its artifact"
                    )
                seen_receipts.add(observation["receiptSha256"])
                inventory_observations.append(observation)
            if (
                inventory_observations[0]["captureId"] != capture_id
                or inventory_observations[0]["inventorySha256"]
                != origin_catalog_sha256
                or inventory_observations[0]["workspaceManifestSha256"]
                != _sha(
                    record.get("workspaceManifestSha256"),
                    "workspace.workspaceManifestSha256",
                )
            ):
                raise ReplayProjectError(
                    "workspace first inventory observation differs from publication provenance"
                )
            shader = {
                "codeSha256": code_sha256,
                "layoutHash": layout_hash,
                "interfaceHash": interface_hash,
                "interfaceComplete": interface_complete,
            }
        elif provenance_index is None:
            capture = self._capture_for_provenance(
                manifest, capture_id, origin_catalog_sha256
            )
            shader_matches = [
                item
                for item in capture["shaders"]
                if item.get("stage") == stage
                and item.get("entryPoint", "") == entry_point
                and item.get("rawSha256") == raw_sha256
                and (
                    origin_resource_id is None
                    or item.get("resourceId") == origin_resource_id
                )
            ]
            if len(shader_matches) != 1:
                raise ReplayProjectError(
                    "workspace knowledge does not select one imported Shader"
                )
            shader = shader_matches[0]
        else:
            provenance = provenance_index.get(
                (capture_id, origin_catalog_sha256)
            )
            shader_matches = (
                provenance.get("shaders", {}).get(
                    (stage, entry_point, raw_sha256), []
                )
                if isinstance(provenance, Mapping)
                else []
            )
            shader_matches = [
                item
                for item in shader_matches
                if isinstance(item, Mapping)
                and (
                    origin_resource_id is None
                    or item.get("resourceId") == origin_resource_id
                )
            ]
            if len(shader_matches) != 1:
                raise ReplayProjectError(
                    "workspace knowledge does not select one imported Shader"
                )
            shader = shader_matches[0]
        # Older native-format workspaces used the complete executable hash as
        # their optional code fingerprint when the capture had no separate
        # code hash. The exact raw hash, stage, entry and resource have already
        # selected this Shader above. Preserve the content-bound record;
        # accept only that known fallback, never an arbitrary missing hash.
        catalog_code_sha256 = str(shader.get("codeSha256", "") or "")
        legacy_raw_code_fallback = (
            not catalog_code_sha256 and code_sha256 == raw_sha256
        )
        if any(
            (
                catalog_code_sha256 != code_sha256 and not legacy_raw_code_fallback,
                shader.get("layoutHash", "") != layout_hash,
                shader.get("interfaceHash", "") != interface_hash,
                (shader.get("interfaceComplete") is True) != interface_complete,
            )
        ):
            raise ReplayProjectError(
                "workspace knowledge physical identity differs from its capture"
            )
        source_path = self._artifact(
            record.get("sourcePath"), "workspace knowledge source"
        )
        source_format = _workspace_source_format(record)
        if record.get("sourcePath") != "knowledge/workspaces/%s.%s" % (token, source_format):
            raise ReplayProjectError("workspace knowledge source format/path differs")
        if not source_artifact_verified and (
            _sha256_bytes(
                _file_bytes(source_path, MAX_SOURCE_BYTES, "workspace knowledge source")
            )
            != source_sha256
        ):
            raise ReplayProjectError("workspace knowledge source hash mismatch")
        identity = {
            "workspaceId": workspace_id,
            "captureId": capture_id,
            "originCatalogSha256": origin_catalog_sha256,
            "stage": stage,
            "entryPoint": entry_point,
            "rawSha256": raw_sha256,
            "codeSha256": code_sha256,
            "layoutHash": layout_hash,
            "interfaceHash": interface_hash,
            "interfaceComplete": interface_complete,
            "baselineAbiSha256": baseline_abi_sha256,
            "targetProfile": copy.deepcopy(record.get("targetProfile")),
            "sourceSha256": source_sha256,
            "analysisSha256": analysis_sha256,
        }
        if source_format != "hlsl":
            identity["sourceFormat"] = source_format
        publication_mode = record.get("publicationMode")
        if publication_mode is not None:
            if publication_mode != SOURCE_ONLY_PUBLICATION or compiled_identity is not None:
                raise ReplayProjectError("workspace source-only publication mode is invalid")
            identity["publicationMode"] = publication_mode
        if origin_resource_id is not None:
            identity["originResourceId"] = origin_resource_id
        if inventory_origin is not None:
            identity["inventoryOrigin"] = _inventory_origin_identity(
                inventory_origin
            )
        compiler_binding = record.get("compilerBinding")
        if compiler_binding is not None:
            clean_compiler_binding = _compiler_binding_identity(
                compiler_binding
            )
            if clean_compiler_binding["targetProfile"] != record.get(
                "targetProfile"
            ):
                raise ReplayProjectError(
                    "workspace compiler binding target profile differs"
                )
            identity["compilerBinding"] = clean_compiler_binding
        disassembly_artifacts = record.get("assemblyDisassemblyArtifacts", [])
        if disassembly_artifacts:
            if not isinstance(disassembly_artifacts, list):
                raise ReplayProjectError(
                    "workspace assembly/disassembly artifacts are invalid"
                )
            clean_disassembly = [
                _assembly_disassembly_identity(item)
                for item in disassembly_artifacts
            ]
            if any(
                item["kind"] == "native-disassembly"
                and (
                    item["role"] != "baseline"
                    or item["inputRawSha256"] != raw_sha256
                )
                for item in clean_disassembly
            ):
                raise ReplayProjectError(
                    "workspace native disassembly physical binding mismatch"
                )
            identity["assemblyDisassemblyArtifacts"] = clean_disassembly
        if compiled_identity is not None:
            _validate_native_candidate_binding(compiled_identity, compiler_binding, source_format)
            identity["compiledCandidate"] = compiled_identity
        expected_knowledge_id = (
            _canonical_sha256(_inventory_workspace_artifact_identity(identity))[:32]
            if inventory_origin is not None
            else _canonical_sha256(identity)[:32]
        )
        if expected_knowledge_id != token:
            raise ReplayProjectError("workspace knowledgeId does not match its contents")
        return copy.deepcopy(dict(record))

    def get_workspace_knowledge(self, knowledge_id: str) -> dict[str, Any]:
        """Read one content-bound HLSL knowledge record with full provenance."""

        return self._workspace_knowledge_from_manifest(self._load(), knowledge_id)

    def current_workspace_source_head(self, knowledge_id: str) -> dict[str, Any]:
        """Read this source family's latest head without scanning capture matches."""
        manifest = self._load()
        records = [item for item in manifest["knowledge"]["workspaces"]
                   if item.get("knowledgeId") == knowledge_id]
        if len(records) != 1:
            raise ReplayProjectError("unknown project workspace knowledge ID")
        head_key = self._workspace_head_key(records[0], manifest)
        head = self._active_heads_state(manifest)["workspaces"][head_key]
        knowledge = self._workspace_knowledge_from_manifest(manifest, head["knowledgeId"])
        return {"headKey": head_key, "generation": head["generation"],
                "knowledge": knowledge}

    def find_current_workspace_source_head(
        self, *, source_sha256: str, raw_sha256: str, stage: str, entry_point: str,
        workspace_id: str | None = None, capture_id: str | None = None,
        target_profile: Mapping[str, Any] | None = None, baseline_abi_sha256: str | None = None,
    ) -> dict[str, Any] | None:
        """Find a current source head, optionally bound to one physical workspace.

        Callers publishing an applied workspace know its exact scope. Filter it
        before ambiguity detection: identical source/raw bytes may legitimately
        have separate active heads for different target profiles or ABI layouts.
        Unscoped callers retain the existing ambiguity error.
        """
        manifest = self._load()
        heads = self._active_heads_state(manifest)["workspaces"]
        active = {value["knowledgeId"]: (key, value) for key, value in heads.items()}
        matches = [item for item in manifest["knowledge"]["workspaces"]
                   if item["knowledgeId"] in active
                   and item.get("sourceSha256") == source_sha256
                   and item.get("rawSha256") == raw_sha256
                   and item.get("stage") == stage
                   and item.get("entryPoint", "") == entry_point
                   and (workspace_id is None or item.get("workspaceId") == workspace_id)
                   and (capture_id is None or item.get("captureId") == capture_id)
                   and (target_profile is None or item.get("targetProfile") == target_profile)
                   and (baseline_abi_sha256 is None or item.get("baselineAbiSha256") == baseline_abi_sha256)]
        if not matches:
            return None
        if len(matches) != 1:
            raise ReplayProjectError("current project Shader source head is ambiguous")
        knowledge_id = matches[0]["knowledgeId"]
        key, head = active[knowledge_id]
        return {"projectId": manifest["projectId"], "headKey": key,
                "generation": head["generation"],
                "knowledge": self._workspace_knowledge_from_manifest(manifest, knowledge_id)}

    def _published_workspace_source_advance(
        self, manifest: dict[str, Any], knowledge_id: str,
        status: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Recognize an unchanged old publication, never an arbitrary local edit."""
        source = status.get("source") or {}
        latest = status.get("latestCompiled") or {}
        revision = status.get("revision")
        records = manifest["knowledge"]["workspaces"]
        heads = self._active_heads_state(manifest)["workspaces"]
        activation = self._active_knowledge_state(manifest)["workspaces"]
        requested = [record for record in records if record.get("knowledgeId") == knowledge_id]
        if len(requested) != 1:
            raise ReplayProjectError("unknown project workspace knowledge ID")
        target = requested[0]
        head_key = self._workspace_head_key(target, manifest)
        head = heads[head_key]
        if (head["knowledgeId"] != knowledge_id
                and knowledge_id not in self._workspace_restore_fallbacks(manifest)):
            raise ReplayProjectError("project source is no longer the active knowledge head")
        if (source.get("currentSha256") == target.get("sourceSha256")
                or source.get("present") is not True
                or source.get("modifiedSinceDecompile") is False
                or type(revision) is not int or revision <= 0
                or latest.get("revision") != revision
                or latest.get("sourceSha256") != source.get("currentSha256")
                or latest.get("replayAbiValidated") is not True
                or latest.get("abiCompatible") is not True
                or latest.get("requiresReplayAbiValidation") is not False
                or (latest.get("abiGate") or {}).get("status") != "compatible"):
            return None
        for previous in records:
            compiled = previous.get("compiledCandidate") or {}
            if (previous.get("knowledgeId") == knowledge_id
                    or previous.get("workspaceId") != status.get("workspaceId")
                    or previous.get("sourceSha256") != source.get("currentSha256")
                    or previous.get("captureId") != (status.get("capture") or {}).get("captureId")
                    or previous.get("rawSha256") != (status.get("baseline") or {}).get("rawSha256")
                    or previous.get("baselineAbiSha256") != (status.get("baseline") or {}).get("abiSha256")
                    or previous.get("stage") != status.get("stage")
                    or previous.get("entryPoint", "") != status.get("entryPoint", "")
                    or previous.get("targetProfile") != status.get("targetProfile")
                    or _workspace_source_format(previous) != source.get("format")
                    or compiled.get("sourceRevision") != revision
                    or compiled.get("sourceSha256") != source.get("currentSha256")
                    or compiled.get("rawSha256") != latest.get("rawSha256")
                    or compiled.get("targetProfile") != status.get("targetProfile")
                    or (previous.get("originResourceId") is not None
                        and previous["originResourceId"] != (status.get("shader") or {}).get("resourceId"))):
                continue
            if (_normal_path(str(previous.get("workspacePath") or ""))
                    != _normal_path(str(status.get("workspacePath") or ""))):
                continue
            previous_head_key = self._workspace_head_key(previous, manifest)
            if previous_head_key != head_key:
                # Separate captures can wrap identical DirectX instructions
                # in different debug containers. Only the registered target
                # catalog can establish that these are the same source family.
                try:
                    capture = self._capture_for_provenance(
                        manifest, status["capture"]["captureId"],
                        self._capture_status_catalog_sha256(status["capture"]))
                    shader = self._capture_shader(
                        capture, resource_id=status["shader"]["resourceId"],
                        stage=status["stage"], entry_point=status.get("entryPoint", ""),
                        raw_sha256=status["baseline"]["rawSha256"])
                    physical = self._head_shader_identity(target, baseline_abi=True, manifest=manifest)
                    if (_match_mode(target, shader) != "code-interface-layout-exact"
                            or _reuse_api_family(capture.get("api")) != _reuse_api_family(physical["api"])
                            or _shader_encoding(shader.get("encoding")) != _shader_encoding(physical["encoding"])
                            or status["baseline"].get("codeSha256") != shader.get("codeSha256")
                            or target.get("baselineAbiSha256") != status["baseline"].get("abiSha256")
                            or target.get("targetProfile") != status.get("targetProfile")
                            or _workspace_source_format(target) != source.get("format")
                            or activation[knowledge_id] <= activation[heads[previous_head_key]["knowledgeId"]]):
                        continue
                except (KeyError, TypeError, ValueError, ReplayProjectError):
                    continue
            if not self._published_workspace_candidate_current(status):
                return None
            # Metadata alone cannot authorize replacing source. Authenticate
            # the historical source, binary and publication's content identity.
            self._workspace_knowledge_from_manifest(
                manifest, previous["knowledgeId"], record=previous)
            return {
                "schema": "renderdoc-ultra-published-workspace-source-advance/v1",
                "schemaVersion": 1, "projectId": manifest["projectId"],
                "headKey": head_key, "generation": head["generation"],
                "knowledgeId": knowledge_id, "previousKnowledgeId": previous["knowledgeId"],
                "workspaceId": status["workspaceId"],
                "workspacePath": str(_normal_path(str(status["workspacePath"]))),
                "sourceSha256": source["currentSha256"], "revision": revision,
                "candidateRawSha256": latest["rawSha256"],
                **({"previousHeadKey": previous_head_key,
                    "previousHeadGeneration": heads[previous_head_key]["generation"],
                    "previousHeadKnowledgeId": heads[previous_head_key]["knowledgeId"]}
                   if previous_head_key != head_key else {}),
            }
        return None

    @staticmethod
    def _published_workspace_candidate_current(status: Mapping[str, Any]) -> bool:
        """An unpublished binary/header edit must survive source-head recovery."""
        try:
            from .shader_workspace.compile_source_unit import current_dependencies
            root = _normal_path(str(status["workspacePath"]))
            latest = status["latestCompiled"]
            candidate = root / latest["blobPath"]
            source = Path(status["source"]["path"])
            if (candidate.is_symlink() or source.is_symlink()
                    or not is_within(root, candidate.resolve()) or not is_within(root, source.resolve())
                    or _sha256_bytes(_file_bytes(candidate, MAX_COMPILED_CANDIDATE_BYTES, "workspace candidate")) != latest["rawSha256"]):
                return False
            source_bytes = _file_bytes(source, MAX_SOURCE_BYTES, "workspace source")
            # The caller separately compares root-source bytes under its seed
            # lock. Keep the legacy source-mismatch diagnostic at that boundary.
            if _sha256_bytes(source_bytes) != status["source"]["currentSha256"]:
                return True
            dependencies = current_dependencies(source, source_bytes, latest)
            return dependencies["current"] or (
                "sourceDependencies" not in latest and dependencies["documentCount"] == 1
                and not dependencies["error"])
        except (OSError, ValueError, KeyError, TypeError):
            return False

    @classmethod
    def _recheck_published_source_seed(cls, root: Path, status: Mapping[str, Any]) -> None:
        saved = json.loads(_file_bytes(root / "manifest.json", MAX_PROJECT_BYTES,
                                     "destination manifest").decode("utf-8"))
        latest = saved.get("latestCompiled") or {}
        if (saved.get("workspaceId") != status.get("workspaceId")
                or saved.get("revision") != status.get("revision")
                or any(latest.get(key) != status["latestCompiled"].get(key)
                       for key in ("revision", "sourceSha256", "rawSha256", "abiGate",
                                   "replayAbiValidated", "requiresReplayAbiValidation", "abiCompatible",
                                   "sourceDependencies"))):
            raise ReplayProjectError("published workspace revision changed before seed")
        if not cls._published_workspace_candidate_current(status):
            raise ReplayProjectError("published workspace candidate or includes changed before seed")

    def published_workspace_source_advance(
        self, knowledge_id: str, status: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Read an exact old-publication receipt; seeding rechecks it under locks."""
        with _thread_lock(self.root), _project_lock(self.root):
            return self._published_workspace_source_advance(self._load(), knowledge_id, status)

    def restorable_shader_raw_hashes(self) -> list[str]:
        """Return original executable identities of current project heads only."""
        with _thread_lock(self.root), _project_lock(self.root):
            state = self._reuse_knowledge_state(self._load())
            values = {
                entry["matcher"]["rawSha256"]
                for index in state["indexes"].values()
                for entries in index["raw"].values()
                for entry in entries
            }
            return sorted(_sha(value, "restorable original Shader hash") for value in values)

    def restorable_shader_code_hashes(
        self, missing_raw_hashes: Sequence[str], api: str,
    ) -> list[str]:
        """Route a bounded source lookup after exact container lookup misses.

        Only DirectX containers currently expose a separate parsed executable
        hash. This is a search accelerator, never permission to reuse source
        or binaries: the selected target catalog must still prove its complete
        interface/layout and the workspace must compile against the live ABI.
        """
        missing = {_sha(value, "missing original Shader hash") for value in missing_raw_hashes}
        family = _reuse_api_family(api)
        if family not in {"d3d11", "d3d12"}:
            return []
        with _thread_lock(self.root), _project_lock(self.root):
            state = self._reuse_knowledge_state(self._load())
            hashes: set[str] = set()
            for bucket in state["indexes"]["workspaces"]["code"].values():
                entries = [row for row in bucket if not row.get("rawOnly")
                           and _reuse_api_family(row["api"]) == family
                           and row["encoding"] in {"DXBC", "DXIL"}]
                for entry in entries:
                    origin = state["workspaces"][entry["reference"]["knowledgeId"]]
                    newer = self._newer_program_source(state, origin)
                    if origin["rawSha256"] not in missing:
                        if newer is not None:
                            hashes.add(entry["matcher"]["codeSha256"])
                        continue
                    desired = newer or origin
                    # Another container of this exact program may already be
                    # present with the latest source. Do not turn that common
                    # reopen into a full unique-Shader lookup just because an
                    # older debug container is absent from this capture.
                    represented = any(
                        row["matcher"]["rawSha256"] not in missing
                        and row["encoding"] == entry["encoding"]
                        and (candidate := state["workspaces"][row["reference"]["knowledgeId"]]).get("sourceSha256") == desired.get("sourceSha256")
                        and candidate.get("targetProfile") == desired.get("targetProfile")
                        and candidate.get("baselineAbiSha256") == desired.get("baselineAbiSha256")
                        and _workspace_source_format(candidate) == _workspace_source_format(desired)
                        for row in entries)
                    if not represented:
                        hashes.add(entry["matcher"]["codeSha256"])
            return sorted(hashes)

    def _newer_program_source(
        self, state: Mapping[str, Any], origin: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """Find a newer compatible source publication in this exact program family.

        Per-head generations are local counters and cannot compare two raw
        containers. Global publication activation orders them. Code families
        require the same complete interface/layout and physical ABI/profile;
        this lookup grants no binary or replay authority.
        """
        from .shader_source_publication import source_only_restore_reason
        origin_id = origin["knowledgeId"]
        unused_raw, code_key = self._reuse_index_keys(origin)
        if code_key is None:
            return None
        entries = [entry for entry in state["indexes"]["workspaces"]["code"].get(code_key, [])
                   if entry["reference"].get("knowledgeId") == origin_id]
        if len(entries) != 1:
            return None
        physical = entries[0]
        target = {**origin, "api": physical["api"], "encoding": physical["encoding"]}
        candidates = []
        for reference, mode in self._indexed_reuse_matches(state["indexes"]["workspaces"], target):
            candidate = state["workspaces"][reference["knowledgeId"]]
            if (candidate.get("targetProfile") != origin.get("targetProfile")
                    or not candidate.get("baselineAbiSha256")
                    or candidate.get("baselineAbiSha256") != origin.get("baselineAbiSha256")
                    or _workspace_source_format(candidate) != _workspace_source_format(origin)
                    or source_only_restore_reason(candidate, self.root)):
                continue
            candidates.append(candidate)
        if not candidates:
            return None
        activation = state["activation"]["workspaces"]
        newest = max(candidates, key=lambda row: (activation.get(row["knowledgeId"], 0), row["knowledgeId"]))
        if (activation.get(newest["knowledgeId"], 0) > activation.get(origin_id, 0)
                and newest.get("sourceSha256") != origin.get("sourceSha256")):
            return newest
        return None

    def prepare_workspace_source(
        self, knowledge_id: str, destination_status: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Read a verified project source for a fresh physical workspace.

        No decompiler output is required. The destination must already bind
        real captured bytes and their ABI; compilation and live ABI validation
        are separate and no old replay authorization is imported.
        """
        if (not isinstance(destination_status, Mapping)
                or destination_status.get("schema") != "renderdoc-ultra-shader-workspace-status/v1"):
            raise ReplayProjectError("destination workspace status is invalid")
        baseline = destination_status.get("baseline")
        shader = destination_status.get("shader")
        capture = destination_status.get("capture")
        source = destination_status.get("source")
        if not all(isinstance(item, Mapping) for item in (baseline, shader, capture, source)):
            raise ReplayProjectError("destination workspace physical provenance is incomplete")
        from .shader_workspace.project_source import can_import_project_source
        if not can_import_project_source(destination_status):
            raise ReplayProjectError("direct project source import requires an empty destination or untouched captured GLSL")
        with _thread_lock(self.root), _project_lock(self.root):
            return self._prepare_workspace_source_locked(knowledge_id, destination_status, self._load())

    def _prepare_workspace_source_locked(
        self, knowledge_id: str, destination_status: Mapping[str, Any],
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Verify current project bytes while the caller owns the project lock."""
        baseline = destination_status["baseline"]
        shader = destination_status["shader"]
        capture = destination_status["capture"]
        source = destination_status["source"]
        knowledge = self._workspace_knowledge_from_manifest(manifest, knowledge_id)
        source_format = _workspace_source_format(knowledge)
        if source.get("format", "hlsl") != source_format:
            raise ReplayProjectError("project and destination source formats differ")
        head_key = self._workspace_head_key(knowledge, manifest)
        if (self._active_heads_state(manifest)["workspaces"][head_key]["knowledgeId"] != knowledge_id
                and knowledge_id not in self._workspace_restore_fallbacks(dict(manifest))):
            raise ReplayProjectError("project source is no longer the active knowledge head")
        if (shader.get("rawSha256") != baseline.get("rawSha256")
                or knowledge.get("stage") != destination_status.get("stage")
                or knowledge.get("entryPoint", "") != destination_status.get("entryPoint", "")
                or knowledge.get("targetProfile") != destination_status.get("targetProfile")):
            raise ReplayProjectError("project source differs from the current original Shader/profile")
        # A prior reflection hash does not identify the source program.
        # Bind the imported text to the freshly observed target ABI and
        # require a new compile/validation before any executable apply.
        target_abi_sha256 = _sha(baseline.get("abiSha256"), "destination.abiSha256")
        physical = self._head_shader_identity(knowledge, baseline_abi=True, manifest=manifest)
        if (_reuse_api_family(capture.get("api")) != _reuse_api_family(physical["api"])
                or _shader_encoding(baseline.get("kind")) != _shader_encoding(physical["encoding"])):
            raise ReplayProjectError("project source capture API/encoding differs from the destination")
        if knowledge.get("rawSha256") != baseline.get("rawSha256"):
            # Debug/container metadata can differ while the captured program is
            # identical. Resolve the target from its verified project catalog,
            # never from a caller-supplied code hash alone.
            registered_capture = self._capture_for_provenance(
                manifest, _sha(capture.get("captureId"), "destination.captureId"),
                self._capture_status_catalog_sha256(capture))
            registered_shader = self._capture_shader(
                registered_capture,
                resource_id=_positive(shader.get("resourceId"), "destination.resourceId"),
                stage=destination_status["stage"],
                entry_point=destination_status["entryPoint"],
                raw_sha256=_sha(baseline.get("rawSha256"), "destination.rawSha256"))
            if (_reuse_api_family(registered_capture.get("api")) != _reuse_api_family(physical["api"])
                    or _shader_encoding(registered_shader.get("encoding")) != _shader_encoding(physical["encoding"])):
                raise ReplayProjectError("project source capture API/encoding differs from the destination")
            if (_match_mode(knowledge, registered_shader) != "code-interface-layout-exact"
                    or baseline.get("codeSha256") != registered_shader.get("codeSha256")):
                raise ReplayProjectError("project source differs from the current original Shader code/interface/layout")
            if not knowledge.get("baselineAbiSha256") or knowledge["baselineAbiSha256"] != target_abi_sha256:
                raise ReplayProjectError("destination target profile/physical ABI differs from project knowledge")
        payload = _file_bytes(self._artifact(knowledge["sourcePath"], "project source"), MAX_SOURCE_BYTES, "project source")
        if _sha256_bytes(payload) != knowledge["sourceSha256"]:
            raise ReplayProjectError("project source bytes changed")
        text = payload.decode("utf-8", errors="strict")
        if not text.strip() or "\x00" in text:
            raise ReplayProjectError("project source must be nonempty UTF-8 Shader text")
        # Existing knowledge records retain the captured entry name. The
        # fixed SPIR-V round trip may emit main instead; bind only an
        # actual uniquely declared function, never an invented entry.
        from .shader_workspace.flow_graph import _function_records
        names = [item["name"] for item in _function_records(text)]
        entry = str(knowledge.get("entryPoint", ""))
        if names.count(entry) != 1:
            if names.count("main") != 1:
                raise ReplayProjectError("project source has no unique captured/main compile entry")
            entry = "main"
        return {
            "schema": "replaylab-verified-project-shader-source/v1",
            "projectId": manifest["projectId"], "projectPath": str(self.root),
            "knowledgeId": knowledge_id, "sourceSha256": knowledge["sourceSha256"],
            "sourceText": text, "sourceEntryPoint": entry,
            **({"sourceFormat": source_format} if source_format != "hlsl" else {}),
            "originalRawSha256": baseline["rawSha256"],
            "baselineAbiSha256": target_abi_sha256,
            "stage": destination_status["stage"], "entryPoint": destination_status["entryPoint"],
            "targetProfile": copy.deepcopy(destination_status["targetProfile"]),
            "originWorkspacePath": knowledge.get("workspacePath"),
            "originWorkspaceId": knowledge["workspaceId"],
            "analysisSha256": knowledge.get("analysisSha256", ""),
            "applied": False, "requiresCompile": True, "requiresLiveAbiRevalidation": True,
            "semanticEquivalenceVerified": False,
        }

    def _material_knowledge_from_manifest(
        self,
        manifest: Mapping[str, Any],
        knowledge_id: str,
        *,
        record: Mapping[str, Any] | None = None,
        provenance_index: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        token = str(knowledge_id or "").strip().lower()
        if not HEX32_RE.fullmatch(token):
            raise ReplayProjectError(
                "knowledge_id must be 32 lowercase hexadecimal characters"
            )
        if record is None:
            matches = [
                item
                for item in manifest["knowledge"]["materials"]
                if isinstance(item, Mapping) and item.get("knowledgeId") == token
            ]
            if len(matches) != 1:
                raise ReplayProjectError(
                    "material knowledge_id does not select one project record"
                )
            record = matches[0]
        elif (
            not isinstance(record, Mapping)
            or record.get("knowledgeId") != token
        ):
            raise ReplayProjectError(
                "indexed material knowledge differs from its knowledge_id"
            )
        capture_id = _sha(record.get("captureId"), "material.captureId")
        origin_catalog_sha256 = _sha(
            record.get("originCatalogSha256"), "material.originCatalogSha256"
        )
        origin_resource_id = (
            _canonical_positive(
                record.get("originResourceId"),
                "material.originResourceId",
            )
            if "originResourceId" in record
            else None
        )
        stage = _text(str(record.get("stage", "")), "material.stage", 64)
        entry_point = _text(
            str(record.get("entryPoint", "") or ""),
            "material.entryPoint",
            512,
            allow_empty=True,
        )
        raw_sha256 = _sha(record.get("rawSha256"), "material.rawSha256")
        code_sha256 = _sha(
            record.get("codeSha256"), "material.codeSha256", allow_empty=True
        )
        layout_hash = _sha(
            record.get("layoutHash"), "material.layoutHash", allow_empty=True
        )
        interface_hash = _sha(
            record.get("interfaceHash"),
            "material.interfaceHash",
            allow_empty=True,
        )
        interface_complete = record.get("interfaceComplete") is True
        block_path = _text(
            str(record.get("constantBlockPath", "")),
            "material.constantBlockPath",
            2048,
        )
        block_layout_sha256 = _sha(
            record.get("constantBlockLayoutSha256"),
            "material.constantBlockLayoutSha256",
        )
        raw_parameters = record.get("parameters")
        if (
            not isinstance(raw_parameters, list)
            or not raw_parameters
            or len(raw_parameters) > MAX_MATERIAL_PARAMETERS
        ):
            raise ReplayProjectError("material knowledge parameters are invalid")
        parameters: list[dict[str, Any]] = []
        seen_parameter_ids: set[str] = set()
        for index, raw_parameter in enumerate(raw_parameters):
            if not isinstance(raw_parameter, Mapping) or set(raw_parameter) != {
                "parameterId",
                "path",
                "baseType",
                "components",
                "value",
            }:
                raise ReplayProjectError(
                    "material parameter[%d] fields are invalid" % index
                )
            parameter_id = str(raw_parameter.get("parameterId", "") or "")
            if (
                not HEX32_RE.fullmatch(parameter_id)
                or parameter_id in seen_parameter_ids
            ):
                raise ReplayProjectError(
                    "material parameter[%d] ID is invalid or duplicated" % index
                )
            seen_parameter_ids.add(parameter_id)
            path = _text(
                str(raw_parameter.get("path", "")),
                "material.parameter.path",
                2048,
            )
            base_type = _text(
                str(raw_parameter.get("baseType", "")),
                "material.parameter.baseType",
                32,
            )
            if base_type not in {"Float", "SInt", "UInt"}:
                raise ReplayProjectError("material parameter baseType is unsupported")
            components = _positive(
                raw_parameter.get("components"), "material.parameter.components"
            )
            if components > 4:
                raise ReplayProjectError(
                    "material parameter components exceeds vec4"
                )
            raw_value = raw_parameter.get("value")
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            if len(values) != components:
                raise ReplayProjectError(
                    "material parameter value shape differs from components"
                )
            if base_type == "Float":
                if any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or abs(float(value)) > 3.4028234663852886e38
                    for value in values
                ):
                    raise ReplayProjectError(
                        "material Float value is not finite float32 data"
                    )
            else:
                lower, upper = (
                    (-2147483648, 2147483647)
                    if base_type == "SInt"
                    else (0, 4294967295)
                )
                if any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < lower
                    or value > upper
                    for value in values
                ):
                    raise ReplayProjectError(
                        "material integer value is outside its reflected 32-bit type"
                    )
            parameters.append(
                {
                    "parameterId": parameter_id,
                    "path": path,
                    "baseType": base_type,
                    "components": components,
                    "value": copy.deepcopy(raw_value),
                }
            )
        if provenance_index is None:
            capture = self._capture_for_provenance(
                manifest, capture_id, origin_catalog_sha256
            )
            matching_shaders = [
                item
                for item in capture["shaders"]
                if item.get("stage") == stage
                and item.get("entryPoint", "") == entry_point
                and item.get("rawSha256") == raw_sha256
            ]
        else:
            provenance = provenance_index.get(
                (capture_id, origin_catalog_sha256)
            )
            matching_shaders = (
                list(
                    provenance.get("shaders", {}).get(
                        (stage, entry_point, raw_sha256), []
                    )
                )
                if isinstance(provenance, Mapping)
                else []
            )
        exact_shaders = [
            shader
            for shader in matching_shaders
            if origin_resource_id is None
            or shader.get("resourceId") == origin_resource_id
        ]
        if len(exact_shaders) != 1:
            raise ReplayProjectError(
                "material knowledge does not select one imported Shader"
            )
        shader = exact_shaders[0]
        if any(
            (
                shader.get("codeSha256", "") != code_sha256,
                shader.get("layoutHash", "") != layout_hash,
                shader.get("interfaceHash", "") != interface_hash,
                (shader.get("interfaceComplete") is True) != interface_complete,
            )
        ):
            raise ReplayProjectError(
                "material knowledge physical identity differs from its capture"
            )
        identity = {
            "captureId": capture_id,
            "originCatalogSha256": origin_catalog_sha256,
            "stage": stage,
            "entryPoint": entry_point,
            "rawSha256": raw_sha256,
            "codeSha256": code_sha256,
            "layoutHash": layout_hash,
            "interfaceHash": interface_hash,
            "interfaceComplete": interface_complete,
            "constantBlockPath": block_path,
            "constantBlockLayoutSha256": block_layout_sha256,
            "parameters": parameters,
        }
        if origin_resource_id is not None:
            identity["originResourceId"] = origin_resource_id
        if _canonical_sha256(identity)[:32] != token:
            raise ReplayProjectError("material knowledgeId does not match its contents")
        _text(record.get("registeredAt"), "material.registeredAt", 128)
        return copy.deepcopy(dict(record))

    def get_material_knowledge(self, knowledge_id: str) -> dict[str, Any]:
        """Read and strictly validate one portable material-value record."""

        return self._material_knowledge_from_manifest(self._load(), knowledge_id)

    @staticmethod
    def _restore_provenance_index(
        manifest: Mapping[str, Any],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Index exact capture revisions and their shaders for one restore pass."""

        result: dict[tuple[str, str], dict[str, Any]] = {}
        for capture in manifest["captures"]:
            revisions = capture.get("catalogRevisions")
            exact_captures = (
                [
                    ReplayProjectService._capture_from_revision(capture, revision)
                    for revision in revisions
                ]
                if isinstance(revisions, list)
                else [capture]
            )
            for exact_capture in exact_captures:
                key = (
                    _sha(exact_capture.get("captureId"), "capture.captureId"),
                    _sha(
                        exact_capture.get("catalogSha256"),
                        "capture.catalogSha256",
                    ),
                )
                if key in result:
                    raise ReplayProjectError(
                        "capture provenance revision is duplicated"
                    )
                shaders: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
                for shader in exact_capture["shaders"]:
                    shader_key = (
                        _text(str(shader.get("stage", "")), "shader.stage", 64),
                        _text(
                            str(shader.get("entryPoint", "") or ""),
                            "shader.entryPoint",
                            512,
                            allow_empty=True,
                        ),
                        _sha(shader.get("rawSha256"), "shader.rawSha256"),
                    )
                    shaders.setdefault(shader_key, []).append(shader)
                result[key] = {
                    "capture": exact_capture,
                    "shaders": shaders,
                }
        return result

    def restore_knowledge_snapshot(
        self, capture_id: str, catalog_sha256: str
    ) -> dict[str, Any]:
        """Validate one project revision once for a bounded restore operation.

        The returned dictionaries contain only knowledge referenced by exact
        matches for this capture. The manifest loader has already hash-verified
        each project artifact once; indexed per-record validation therefore
        stays linear without weakening source/provenance/ABI checks.
        """

        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load(persist_migrations=True)
            capture = self._capture(manifest, capture_id, catalog_sha256)
            state = self._reuse_knowledge_state(manifest)
            restore_scope = self._capture_referenced_knowledge_scope(capture, state)
            return self._restore_knowledge_snapshot_from_manifest(
                manifest, capture, state, restore_scope
            )

    def _restore_knowledge_snapshot_from_manifest(
        self,
        manifest: Mapping[str, Any],
        capture: Mapping[str, Any],
        state: Mapping[str, Any],
        restore_scope: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Materialize one hash-verified snapshot from a single manifest read."""

        binding = self._reuse_cache_binding(
            manifest, capture, restore_scope["knowledgeContentSha256"]
        )
        provenance_index = self._restore_provenance_index(manifest)
        workspaces: dict[str, dict[str, Any]] = {}
        for knowledge_id_value in restore_scope["workspaceIds"]:
            workspaces[knowledge_id_value] = self._workspace_knowledge_from_manifest(
                manifest,
                knowledge_id_value,
                record=state["workspaces"][knowledge_id_value],
                source_artifact_verified=True,
                provenance_index=provenance_index,
            )
        materials: dict[str, dict[str, Any]] = {}
        for knowledge_id_value in restore_scope["materialIds"]:
            materials[knowledge_id_value] = self._material_knowledge_from_manifest(
                manifest,
                knowledge_id_value,
                record=state["materials"][knowledge_id_value],
                provenance_index=provenance_index,
            )
        return {
            "projectId": binding["projectId"],
            "projectPath": str(self.root),
            "scopeSha256": binding["scopeSha256"],
            "captureId": binding["captureId"],
            "catalogSha256": binding["catalogSha256"],
            "knowledgeContentSha256": binding["knowledgeContentSha256"],
            "matcherPolicySha256": binding["matcherPolicySha256"],
            "capture": copy.deepcopy(capture),
            "workspaces": workspaces,
            "materials": materials,
        }

    @staticmethod
    def _reuse_shader_identity(shader: Mapping[str, Any]) -> dict[str, Any]:
        """Project only the physical fields which can influence reuse matching."""

        return {
            "resourceId": _positive(shader.get("resourceId"), "shader.resourceId"),
            "stage": _text(str(shader.get("stage", "")), "shader.stage", 64),
            "entryPoint": _text(
                str(shader.get("entryPoint", "") or ""),
                "shader.entryPoint",
                512,
                allow_empty=True,
            ),
            "rawSha256": _sha(
                shader.get("rawSha256"), "shader.rawSha256", allow_empty=True
            ),
            "codeSha256": _sha(
                shader.get("codeSha256"), "shader.codeSha256", allow_empty=True
            ),
            "interfaceHash": _sha(
                shader.get("interfaceHash"),
                "shader.interfaceHash",
                allow_empty=True,
            ),
            "layoutHash": _sha(
                shader.get("layoutHash"), "shader.layoutHash", allow_empty=True
            ),
            "interfaceComplete": shader.get("interfaceComplete") is True,
        }

    @staticmethod
    def _reuse_index_keys(value: Mapping[str, Any]) -> tuple[tuple[Any, ...] | None, tuple[Any, ...] | None]:
        stage = _text(str(value.get("stage", "")), "knowledge.stage", 64)
        entry = _text(
            str(value.get("entryPoint", "") or ""),
            "knowledge.entryPoint",
            512,
            allow_empty=True,
        )
        raw = _sha(value.get("rawSha256"), "knowledge.rawSha256", True)
        code = _sha(value.get("codeSha256"), "knowledge.codeSha256", True)
        interface_hash = _sha(
            value.get("interfaceHash"), "knowledge.interfaceHash", True
        )
        layout_hash = _sha(value.get("layoutHash"), "knowledge.layoutHash", True)
        raw_key = (stage, entry, raw) if raw else None
        code_key = (
            (stage, entry, code, interface_hash, layout_hash)
            if code and interface_hash and value.get("interfaceComplete") is True
            else None
        )
        return raw_key, code_key

    @staticmethod
    def _semantic_knowledge_record(
        kind: str, record: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Project semantic bytes, excluding local/audit-only observations."""

        result = copy.deepcopy(dict(record))
        result.pop("registeredAt", None)
        if kind == "workspaces":
            for field in (
                "inventoryObservations",
                "sourceOriginPath",
                "workspacePath",
                "workspaceManifestSha256",
            ):
                result.pop(field, None)
        return result

    @staticmethod
    def _semantic_head(
        head_key: str, head: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "headKey": head_key,
            "generation": head["generation"],
            "knowledgeId": head["knowledgeId"],
        }

    def _workspace_restore_fallbacks(self, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Keep a pending source head while selecting its last verified executable.

        This is a derived restore view, never a publication or head rewrite.
        History must share the complete physical head key (including original
        bytes, API, encoding and profile), and carry a verified binary artifact.
        """
        from .shader_source_publication import source_only_restore_reason

        heads = self._active_heads_state(manifest)["workspaces"]
        members = self._knowledge_head_members(manifest)["workspaces"]
        activation = self._active_knowledge_state(manifest)["workspaces"]
        records = {record["knowledgeId"]: record for record in manifest["knowledge"]["workspaces"]}
        result: dict[str, dict[str, Any]] = {}
        for head_key, head in heads.items():
            pending = records[head["knowledgeId"]]
            reason = source_only_restore_reason(pending, self.root)
            if not reason:
                continue
            history = sorted(members[head_key],
                key=lambda item: (activation.get(item["knowledgeId"], 0), item["ordinal"]), reverse=True)
            for item in history:
                token = item["knowledgeId"]
                prior = records[token]
                if (token == pending["knowledgeId"] or not prior.get("compiledCandidate")
                        or source_only_restore_reason(prior, self.root)):
                    continue
                # The normal reader authenticates source, binary and ABI origin
                # evidence. An old workspace path alone never makes a fallback.
                try:
                    self._workspace_knowledge_from_manifest(manifest, token, record=prior)
                except (ReplayProjectError, OSError):
                    # A missing/corrupt historical artifact cannot authorize
                    # replay or prevent a still older verified version.
                    continue
                result[token] = {"knowledgeId": pending["knowledgeId"],
                    "sourceSha256": pending["sourceSha256"], "reason": reason,
                    "headKey": head_key, "generation": head["generation"]}
                break
        return result

    def _reuse_knowledge_state(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        """Build linear-time lookup tables over the exact manifest knowledge revision."""

        knowledge = manifest["knowledge"]
        if not isinstance(manifest, dict):
            raise ReplayProjectError("project manifest must be a JSON object")
        active = self._active_knowledge_state(manifest)
        active_heads = self._active_heads_state(manifest)
        head_members = self._knowledge_head_members(manifest)
        replay_fallbacks = self._workspace_restore_fallbacks(manifest)
        pending_with_fallback = {item["knowledgeId"] for item in replay_fallbacks.values()}
        state: dict[str, Any] = {
            "knowledgeContentSha256": "",
            "replayFallbacks": replay_fallbacks,
            "indexes": {
                kind: {"raw": {}, "code": {}}
                for kind in ("workspaces", "aliases", "materials")
            },
            "workspaces": {},
            "aliases": {},
            "materials": {},
            "activation": {
                "workspaces": dict(active["workspaces"]),
                "materials": dict(active["materials"]),
            },
            "activeHeads": copy.deepcopy(active_heads),
            "headBindings": {
                "workspaces": {},
                "materials": {},
                "aliases": {},
            },
            "ordinals": {
                "workspaces": {},
                "materials": {},
            },
        }

        def knowledge_id(record: Mapping[str, Any], label: str) -> str:
            value = str(record.get("knowledgeId", "") or "")
            if not HEX32_RE.fullmatch(value):
                raise ReplayProjectError("%s knowledgeId is invalid" % label)
            return value

        def index_entry(
            kind: str,
            token: str,
            ordinal: tuple[int, int],
            matcher: Mapping[str, Any],
            reference: Mapping[str, Any],
            physical_identity: Mapping[str, Any],
        ) -> None:
            raw_key, code_key = self._reuse_index_keys(matcher)
            entry = {
                "token": token,
                "ordinal": ordinal,
                "matcher": matcher,
                "reference": dict(reference),
                "api": physical_identity["api"],
                "encoding": physical_identity["encoding"],
                "rawOnly": kind == "workspaces" and str(reference.get("knowledgeId", "")) in replay_fallbacks,
            }
            if raw_key is not None:
                state["indexes"][kind]["raw"].setdefault(raw_key, []).append(entry)
            if code_key is not None:
                state["indexes"][kind]["code"].setdefault(code_key, []).append(entry)

        for index, workspace in enumerate(knowledge["workspaces"]):
            if not isinstance(workspace, Mapping):
                raise ReplayProjectError("workspace knowledge record is invalid")
            key = knowledge_id(workspace, "workspace")
            if key in state["workspaces"]:
                raise ReplayProjectError("workspace knowledgeId is duplicated")
            state["workspaces"][key] = workspace
            state["ordinals"]["workspaces"][key] = (index, 0)
            head_key = self._workspace_head_key(workspace, manifest)
            head = active_heads["workspaces"][head_key]
            if head["knowledgeId"] == key or key in replay_fallbacks:
                physical_identity = self._head_shader_identity(
                    workspace, baseline_abi=True, manifest=manifest
                )
                state["headBindings"]["workspaces"][key] = {
                    **self._semantic_head(head_key, head),
                    "knowledgeId": key,
                    "historyCount": len(head_members["workspaces"][head_key]),
                    "api": physical_identity["api"],
                    "encoding": physical_identity["encoding"],
                }
                if key not in pending_with_fallback:
                    index_entry(
                        "workspaces", "workspace:%s" % key, (index, 0), workspace,
                        {"knowledgeId": key}, physical_identity)

        for alias_index, alias_set in enumerate(knowledge["aliases"]):
            if not isinstance(alias_set, Mapping):
                raise ReplayProjectError("alias knowledge record is invalid")
            key = knowledge_id(alias_set, "alias")
            groups = alias_set.get("groups")
            if not isinstance(groups, list):
                raise ReplayProjectError("alias knowledge groups are invalid")
            for group_index, group in enumerate(groups):
                if not isinstance(group, Mapping):
                    raise ReplayProjectError("alias knowledge group is invalid")
                group_key = (key, group_index)
                if group_key in state["aliases"]:
                    raise ReplayProjectError("alias knowledge group is duplicated")
                state["aliases"][group_key] = (alias_set, group)
                head_key = self._alias_head_key(
                    group, manifest=manifest, provenance=alias_set
                )
                head = active_heads["aliases"][head_key]
                if head["knowledgeId"] == key:
                    physical_identity = self._head_shader_identity(
                        group, manifest=manifest, provenance=alias_set
                    )
                    state["headBindings"]["aliases"][group_key] = {
                        **self._semantic_head(head_key, head),
                        "historyCount": len(
                            head_members["aliases"][head_key]
                        ),
                        "api": physical_identity["api"],
                        "encoding": physical_identity["encoding"],
                    }
                    index_entry(
                        "aliases",
                        "alias:%s:%d" % (key, group_index),
                        (alias_index, group_index),
                        group,
                        {"knowledgeId": key, "groupIndex": group_index},
                        physical_identity,
                    )

        for index, material in enumerate(knowledge["materials"]):
            if not isinstance(material, Mapping):
                raise ReplayProjectError("material knowledge record is invalid")
            key = knowledge_id(material, "material")
            if key in state["materials"]:
                raise ReplayProjectError("material knowledgeId is duplicated")
            state["materials"][key] = material
            state["ordinals"]["materials"][key] = (index, 0)
            head_key = self._material_head_key(material, manifest)
            head = active_heads["materials"][head_key]
            if head["knowledgeId"] == key:
                physical_identity = self._head_shader_identity(
                    material, manifest=manifest
                )
                state["headBindings"]["materials"][key] = {
                    **self._semantic_head(head_key, head),
                    "historyCount": len(head_members["materials"][head_key]),
                    "api": physical_identity["api"],
                    "encoding": physical_identity["encoding"],
                }
                index_entry(
                    "materials",
                    "material:%s" % key,
                    (index, 0),
                    material,
                    {"knowledgeId": key},
                    physical_identity,
                )

        global_semantics = {
            "schema": "renderdoc-ultra-project-active-head-semantics/v1",
            **({"replayFallbacks": copy.deepcopy(replay_fallbacks)} if replay_fallbacks else {}),
            "workspaces": [
                {
                    "head": {
                        key: value
                        for key, value in state["headBindings"][
                            "workspaces"
                        ][knowledge_id].items()
                        if key in {"headKey", "generation", "knowledgeId"}
                    },
                    "knowledge": self._semantic_knowledge_record(
                        "workspaces", state["workspaces"][knowledge_id]
                    ),
                }
                for knowledge_id in sorted(
                    state["headBindings"]["workspaces"]
                )
            ],
            "materials": [
                {
                    "head": {
                        key: value
                        for key, value in state["headBindings"][
                            "materials"
                        ][knowledge_id].items()
                        if key in {"headKey", "generation", "knowledgeId"}
                    },
                    "knowledge": self._semantic_knowledge_record(
                        "materials", state["materials"][knowledge_id]
                    ),
                }
                for knowledge_id in sorted(
                    state["headBindings"]["materials"]
                )
            ],
            "aliases": [
                {
                    "head": {
                        field: value
                        for field, value in state["headBindings"][
                            "aliases"
                        ][key].items()
                        if field in {"headKey", "generation", "knowledgeId"}
                    },
                    "knowledgeId": key[0],
                    "groupIndex": key[1],
                    "group": copy.deepcopy(dict(state["aliases"][key][1])),
                    "artifactSha256": state["aliases"][key][0][
                        "artifactSha256"
                    ],
                }
                for key in sorted(state["headBindings"]["aliases"])
            ],
        }
        state["knowledgeContentSha256"] = _canonical_sha256(global_semantics)
        return state

    @staticmethod
    def _indexed_reuse_matches(
        index: Mapping[str, Any], shader: Mapping[str, Any]
    ) -> list[tuple[dict[str, Any], str]]:
        raw_key, code_key = ReplayProjectService._reuse_index_keys(shader)
        candidates: list[Mapping[str, Any]] = []
        if raw_key is not None:
            candidates.extend(index["raw"].get(raw_key, []))
        if code_key is not None:
            candidates.extend(index["code"].get(code_key, []))
        target_api_value = str(
            shader.get("_projectApi", shader.get("api", "")) or ""
        )
        target_encoding_value = str(shader.get("encoding", "") or "")
        target_api = _reuse_api_family(target_api_value) if target_api_value else ""
        target_encoding = (
            _shader_encoding(target_encoding_value)
            if target_encoding_value
            else ""
        )
        unique: dict[str, Mapping[str, Any]] = {}
        for candidate in candidates:
            if (
                (target_api and _reuse_api_family(candidate.get("api")) != target_api)
                or (
                    target_encoding
                    and candidate.get("encoding") != target_encoding
                )
            ):
                continue
            unique.setdefault(str(candidate["token"]), candidate)
        result: list[tuple[dict[str, Any], str]] = []
        for candidate in sorted(unique.values(), key=lambda item: item["ordinal"]):
            # Keep the original matcher as the final authority. The indexes are
            # only candidate routing and can never relax raw/interface/layout.
            mode = _match_mode(candidate["matcher"], shader)
            if mode and (not candidate.get("rawOnly") or mode == "raw-exact"):
                result.append((dict(candidate["reference"]), mode))
        return result

    def _build_reuse_mappings(
        self, capture: Mapping[str, Any], state: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        mappings: list[dict[str, Any]] = []
        for shader in capture["shaders"]:
            target_shader = {
                **dict(shader),
                "_projectApi": capture.get("api", ""),
            }
            workspace_refs = [
                {**reference, "matchMode": mode}
                for reference, mode in self._indexed_reuse_matches(
                    state["indexes"]["workspaces"], target_shader
                )
            ]
            alias_refs = [
                {**reference, "matchMode": mode}
                for reference, mode in self._indexed_reuse_matches(
                    state["indexes"]["aliases"], target_shader
                )
            ]
            material_refs = [
                {**reference, "matchMode": mode}
                for reference, mode in self._indexed_reuse_matches(
                    state["indexes"]["materials"], target_shader
                )
            ]
            if workspace_refs or alias_refs or material_refs:
                mappings.append(
                    {
                        "shader": self._reuse_shader_identity(shader),
                        "workspaces": workspace_refs,
                        "aliases": alias_refs,
                        "materials": material_refs,
                    }
                )
        return mappings

    def _capture_referenced_knowledge_scope(
        self, capture: Mapping[str, Any], state: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Bind an RDC only to exact knowledge referenced by this capture."""

        mappings = self._build_reuse_mappings(capture, state)
        return self._referenced_knowledge_scope_from_mappings(
            capture, state, mappings
        )

    def _referenced_knowledge_scope_from_mappings(
        self,
        capture: Mapping[str, Any],
        state: Mapping[str, Any],
        mappings: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Hash exactly the already-authorized mapping set for one target."""

        workspace_ids: set[str] = set()
        material_ids: set[str] = set()
        alias_keys: set[tuple[str, int]] = set()
        for mapping in mappings:
            if not isinstance(mapping, Mapping):
                raise ReplayProjectError("capture reuse mapping is invalid")
            for kind, destination, records in (
                ("workspaces", workspace_ids, state["workspaces"]),
                ("materials", material_ids, state["materials"]),
            ):
                references = mapping.get(kind)
                if not isinstance(references, list):
                    raise ReplayProjectError(
                        "capture reuse %s references are invalid" % kind
                    )
                for reference in references:
                    knowledge_id = (
                        str(reference.get("knowledgeId", "") or "")
                        if isinstance(reference, Mapping)
                        else ""
                    )
                    if (
                        not HEX32_RE.fullmatch(knowledge_id)
                        or knowledge_id not in records
                    ):
                        raise ReplayProjectError(
                            "capture reuse %s reference is absent" % kind
                        )
                    destination.add(knowledge_id)

            references = mapping.get("aliases")
            if not isinstance(references, list):
                raise ReplayProjectError(
                    "capture reuse aliases references are invalid"
                )
            for reference in references:
                knowledge_id = (
                    str(reference.get("knowledgeId", "") or "")
                    if isinstance(reference, Mapping)
                    else ""
                )
                group_index = (
                    reference.get("groupIndex")
                    if isinstance(reference, Mapping)
                    else None
                )
                key = (knowledge_id, group_index)
                if (
                    not HEX32_RE.fullmatch(knowledge_id)
                    or isinstance(group_index, bool)
                    or not isinstance(group_index, int)
                    or group_index < 0
                    or key not in state["aliases"]
                ):
                    raise ReplayProjectError(
                        "capture reuse alias reference is absent"
                    )
                alias_keys.add((knowledge_id, group_index))

        workspace_ids.update(state.get("replayFallbacks", {})[token]["knowledgeId"]
                             for token in tuple(workspace_ids) if token in state.get("replayFallbacks", {}))
        ordered_workspace_ids = tuple(sorted(workspace_ids))
        ordered_material_ids = tuple(sorted(material_ids))
        aliases: list[dict[str, Any]] = []
        for knowledge_id, group_index in sorted(alias_keys):
            alias_set, group = state["aliases"][(knowledge_id, group_index)]
            aliases.append(
                {
                    "knowledgeId": knowledge_id,
                    "groupIndex": group_index,
                    "captureId": _sha(
                        alias_set.get("captureId"), "alias.captureId"
                    ),
                    "originCatalogSha256": _sha(
                        alias_set.get("originCatalogSha256"),
                        "alias.originCatalogSha256",
                    ),
                    "artifactPath": _text(
                        alias_set.get("artifactPath"), "alias.artifactPath", 4096
                    ),
                    "artifactSha256": _sha(
                        alias_set.get("artifactSha256"), "alias.artifactSha256"
                    ),
                    "group": copy.deepcopy(dict(group)),
                }
            )

        digest_document = {
            "schema": RESTORE_KNOWLEDGE_SCOPE_SCHEMA,
            **({"replayFallbacks": {token: copy.deepcopy(state["replayFallbacks"][token])
                for token in ordered_workspace_ids if token in state["replayFallbacks"]}}
               if any(token in state.get("replayFallbacks", {}) for token in ordered_workspace_ids) else {}),
            "captureId": _sha(capture.get("captureId"), "capture.captureId"),
            "catalogSha256": _sha(
                capture.get("catalogSha256"), "capture.catalogSha256"
            ),
            "matcherPolicySha256": REUSE_MATCH_POLICY_SHA256,
            "workspaces": [
                self._semantic_knowledge_record(
                    "workspaces", state["workspaces"][knowledge_id]
                )
                for knowledge_id in ordered_workspace_ids
            ],
            "aliases": aliases,
            "materials": [
                self._semantic_knowledge_record(
                    "materials", state["materials"][knowledge_id]
                )
                for knowledge_id in ordered_material_ids
            ],
            # Bind only the referenced authoritative heads. publishedAt,
            # registeredAt, inventory observations, the legacy global serial,
            # inactive history, and unrelated heads are audit/routing data and
            # cannot perturb this semantic restore digest.
            "activeHeads": {
                "workspaces": [
                    {
                        key: value
                        for key, value in state["headBindings"][
                            "workspaces"
                        ][knowledge_id].items()
                        if key in {"headKey", "generation", "knowledgeId"}
                    }
                    for knowledge_id in ordered_workspace_ids
                ],
                "materials": [
                    {
                        key: value
                        for key, value in state["headBindings"][
                            "materials"
                        ][knowledge_id].items()
                        if key in {"headKey", "generation", "knowledgeId"}
                    }
                    for knowledge_id in ordered_material_ids
                ],
                "aliases": [
                    {
                        key: value
                        for key, value in state["headBindings"]["aliases"][
                            (knowledge_id, group_index)
                        ].items()
                        if key in {"headKey", "generation", "knowledgeId"}
                    }
                    for knowledge_id, group_index in sorted(alias_keys)
                ],
            },
        }
        return {
            "knowledgeContentSha256": _canonical_sha256(digest_document),
            "workspaceIds": ordered_workspace_ids,
            "materialIds": ordered_material_ids,
            "aliasRefs": tuple(
                {
                    "knowledgeId": knowledge_id,
                    "groupIndex": group_index,
                }
                for knowledge_id, group_index in sorted(alias_keys)
            ),
        }

    def _reuse_plan_from_mappings(
        self,
        manifest: Mapping[str, Any],
        capture: Mapping[str, Any],
        state: Mapping[str, Any],
        mappings: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        shaders: dict[str, Mapping[str, Any]] = {}
        for shader in capture["shaders"]:
            identity = self._reuse_shader_identity(shader)
            key = _canonical_sha256(identity)
            if key in shaders:
                raise ReplayProjectError("capture has duplicate reusable Shader identity")
            shaders[key] = shader

        results: list[dict[str, Any]] = []
        used_shaders: set[str] = set()
        if len(mappings) > MAX_SHADER_COUNT:
            raise ReplayProjectError("reuse cache mapping count exceeds the project limit")
        for raw_mapping in mappings:
            if not isinstance(raw_mapping, Mapping) or set(raw_mapping) != {
                "shader",
                "workspaces",
                "aliases",
                "materials",
            }:
                raise ReplayProjectError("reuse cache mapping is invalid")
            identity = raw_mapping["shader"]
            if not isinstance(identity, Mapping):
                raise ReplayProjectError("reuse cache Shader identity is invalid")
            clean_identity = self._reuse_shader_identity(identity)
            if dict(identity) != clean_identity:
                raise ReplayProjectError("reuse cache Shader identity is not canonical")
            shader_key = _canonical_sha256(clean_identity)
            shader = shaders.get(shader_key)
            if shader is None or self._reuse_shader_identity(shader) != clean_identity:
                raise ReplayProjectError("reuse cache Shader is absent from the exact catalog")
            if shader_key in used_shaders:
                raise ReplayProjectError("reuse cache maps one Shader more than once")
            used_shaders.add(shader_key)
            item: dict[str, Any] = {
                "shader": shader,
                "workspaceCandidates": [],
                "aliasCandidates": [],
                "materialCandidates": [],
            }

            workspace_refs = raw_mapping["workspaces"]
            if not isinstance(workspace_refs, list) or len(workspace_refs) > MAX_KNOWLEDGE_COUNT:
                raise ReplayProjectError("reuse cache workspace references are invalid")
            seen_workspace: set[str] = set()
            workspace_candidates: list[tuple[int, dict[str, Any]]] = []
            for reference in workspace_refs:
                if not isinstance(reference, Mapping) or set(reference) != {
                    "knowledgeId",
                    "matchMode",
                }:
                    raise ReplayProjectError("reuse cache workspace reference is invalid")
                knowledge_id = str(reference["knowledgeId"])
                if knowledge_id in seen_workspace:
                    raise ReplayProjectError("reuse cache workspace reference is duplicated")
                seen_workspace.add(knowledge_id)
                workspace = state["workspaces"].get(knowledge_id)
                mode = _match_mode(workspace or {}, shader)
                if not mode or mode != reference["matchMode"]:
                    raise ReplayProjectError(
                        "reuse cache workspace reference failed exact matching"
                    )
                if knowledge_id in state.get("replayFallbacks", {}) and mode != "raw-exact":
                    raise ReplayProjectError("validated replay fallback requires the same captured original bytes")
                origin_abi_available = bool(workspace.get("baselineAbiSha256"))
                # Exact captured bytes identify their editable source even if
                # a legacy record did not persist its reflection ABI. Such a
                # record never supplies executable validation authority.
                seed_eligible = mode == "raw-exact" or origin_abi_available
                binary_candidate = workspace.get("compiledCandidate")
                compiler_binding = workspace.get("compilerBinding")
                compiler_binding_identity = (
                    _compiler_binding_identity(compiler_binding)
                    if compiler_binding is not None
                    else None
                )
                binary_identity: dict[str, Any] | None = None
                binary_plan: dict[str, Any] | None = None
                if (
                    binary_candidate is not None
                    and compiler_binding_identity is not None
                    and origin_abi_available
                ):
                    binary_identity = _compiled_candidate_identity(binary_candidate)
                    binary_artifact = self._artifact(
                        binary_candidate.get("artifactPath"),
                        "workspace compiled candidate",
                    )
                    binary_bytes = _file_bytes(
                        binary_artifact,
                        MAX_COMPILED_CANDIDATE_BYTES,
                        "workspace compiled candidate",
                    )
                    if (
                        len(binary_bytes) != binary_identity["byteSize"]
                        or _sha256_bytes(binary_bytes)
                        != binary_identity["rawSha256"]
                    ):
                        raise ReplayProjectError(
                            "workspace compiled candidate changed during reuse planning"
                        )
                    binary_plan = {
                        "schema": binary_identity["schema"],
                        "schemaVersion": binary_identity["schemaVersion"],
                        "artifactPath": str(binary_artifact),
                        "sha256": binary_identity["rawSha256"],
                        "codeSha256": binary_identity["codeSha256"],
                        "byteSize": binary_identity["byteSize"],
                        "kind": binary_identity["kind"],
                        "sourceSha256": binary_identity["sourceSha256"],
                        "sourceRevision": binary_identity["sourceRevision"],
                        "targetProfile": copy.deepcopy(
                            binary_identity["targetProfile"]
                        ),
                        "compilerBindingSha256": compiler_binding_identity[
                            "bindingSha256"
                        ],
                        "candidateOnly": True,
                        "contentAddressed": True,
                        "integrityVerified": True,
                        "originValidationEvidenceOnly": True,
                        "applyAuthorization": False,
                        "freshCaptureRebindRequired": True,
                        "freshLiveAbiRevalidationRequired": True,
                        "oldReplacementIdReused": False,
                        "oldCaptureAbiAuthorityReused": False,
                    }
                    if binary_identity.get("validationScope") == "manual-edit-unvalidated":
                        binary_plan.update(manualEdit=True, validationScope="manual-edit-unvalidated",
                            manualApplyReceipt=copy.deepcopy(binary_identity["manualApplyReceipt"]),
                            originValidationEvidenceOnly=False, freshLiveAbiRevalidationRequired=False)
                candidate = {
                    "knowledgeId": knowledge_id,
                    "matchMode": mode,
                    "sourceSha256": workspace["sourceSha256"],
                    "sourceSeedEligible": seed_eligible,
                    "binaryCandidateEligible": bool(
                        origin_abi_available and binary_plan is not None
                    ),
                    "binaryCandidate": (
                        binary_plan if origin_abi_available else None
                    ),
                    "analysisCandidate": (
                        {
                            "path": str(
                                self._artifact(
                                    workspace["analysisPath"],
                                    "workspace analysis",
                                )
                            ),
                            "sha256": workspace["analysisSha256"],
                            "explanatoryOnly": True,
                            "physicalEvidence": False,
                            "applyAuthorization": False,
                        }
                        if workspace.get("analysisPath")
                        else None
                    ),
                }
                if knowledge_id in state.get("replayFallbacks", {}):
                    candidate["pendingSource"] = copy.deepcopy(state["replayFallbacks"][knowledge_id])
                if not seed_eligible:
                    candidate["blockedReason"] = (
                        "origin workspace has no complete captured baseline ABI"
                    )
                elif not origin_abi_available:
                    candidate["binaryBlockedReason"] = (
                        "origin workspace has no captured baseline ABI; fresh compile required"
                    )
                elif (
                    binary_candidate is not None
                    and compiler_binding_identity is None
                ):
                    candidate["binaryBlockedReason"] = (
                        "origin compiled candidate has no verified compiler/flags binding"
                    )
                head = state["headBindings"]["workspaces"].get(knowledge_id)
                if not isinstance(head, Mapping):
                    raise ReplayProjectError(
                        "workspace knowledge is not an active head"
                    )
                candidate["supersededCandidateCount"] = (
                    head["historyCount"] - 1
                )
                # Generation counts belong to individual raw-container heads.
                # Equivalent programs in other RDCs can have different debug
                # chunks and different head ages; use global publication order.
                workspace_candidates.append((state["activation"]["workspaces"].get(knowledge_id, 0), candidate))

            if workspace_candidates:
                # Knowledge remains append-only, but automatic restore consumes
                # one deterministic active head. Prefer the newest record that
                # can actually seed an executable source; retain a newest
                # blocked record only when no valid source head exists.
                eligible = [
                    entry
                    for entry in workspace_candidates
                    if entry[1].get("sourceSeedEligible") is True
                ]
                unused_activation, active_workspace = max(
                    eligible or workspace_candidates,
                    key=lambda entry: (entry[0], entry[1]["knowledgeId"]),
                )
                active_workspace["activeCandidate"] = True
                item["workspaceCandidates"] = [active_workspace]

            alias_refs = raw_mapping["aliases"]
            if not isinstance(alias_refs, list) or len(alias_refs) > MAX_ALIASES:
                raise ReplayProjectError("reuse cache alias references are invalid")
            from .project_alias_rebind import alias_candidate_binding_sha256

            seen_alias: set[tuple[str, int]] = set()
            for reference in alias_refs:
                if not isinstance(reference, Mapping) or set(reference) != {
                    "knowledgeId",
                    "groupIndex",
                    "matchMode",
                }:
                    raise ReplayProjectError("reuse cache alias reference is invalid")
                knowledge_id = str(reference["knowledgeId"])
                if isinstance(reference["groupIndex"], bool):
                    raise ReplayProjectError("reuse cache alias groupIndex is invalid")
                group_index = int(reference["groupIndex"])
                alias_key = (knowledge_id, group_index)
                if group_index < 0 or alias_key in seen_alias:
                    raise ReplayProjectError("reuse cache alias reference is duplicated")
                seen_alias.add(alias_key)
                alias_pair = state["aliases"].get(alias_key)
                group = alias_pair[1] if alias_pair is not None else {}
                mode = _match_mode(group, shader)
                if not mode or mode != reference["matchMode"]:
                    raise ReplayProjectError(
                        "reuse cache alias reference failed exact matching"
                    )
                aliases = group.get("aliases")
                if not isinstance(aliases, list) or len(aliases) > MAX_ALIASES:
                    raise ReplayProjectError("alias knowledge bindings are invalid")
                if not isinstance(
                    state["headBindings"]["aliases"].get(alias_key), Mapping
                ):
                    raise ReplayProjectError("alias knowledge is not an active head")
                # Bind the candidate to this exact project/catalog/target
                # Shader.  The materializer re-computes this seal from
                # hash-verified project artifacts; it is an integrity check,
                # not an authorization to mutate live replay state.
                candidate = {
                    "knowledgeId": knowledge_id,
                    "groupIndex": group_index,
                    "matchMode": mode,
                    "aliasCount": len(aliases),
                    "candidateOnly": True,
                }
                candidate["bindingSha256"] = alias_candidate_binding_sha256(
                    project_id=manifest["projectId"],
                    capture_id=capture["captureId"],
                    catalog_sha256=capture["catalogSha256"],
                    target_shader=shader,
                    knowledge_id=knowledge_id,
                    group_index=group_index,
                    match_mode=mode,
                    alias_count=len(aliases),
                )
                item["aliasCandidates"].append(candidate)

            material_refs = raw_mapping["materials"]
            if not isinstance(material_refs, list) or len(material_refs) > MAX_KNOWLEDGE_COUNT:
                raise ReplayProjectError("reuse cache material references are invalid")
            seen_material: set[str] = set()
            material_candidates: dict[str, list[tuple[int, dict[str, Any]]]] = {}
            for reference in material_refs:
                if not isinstance(reference, Mapping) or set(reference) != {
                    "knowledgeId",
                    "matchMode",
                }:
                    raise ReplayProjectError("reuse cache material reference is invalid")
                knowledge_id = str(reference["knowledgeId"])
                if knowledge_id in seen_material:
                    raise ReplayProjectError("reuse cache material reference is duplicated")
                seen_material.add(knowledge_id)
                material = state["materials"].get(knowledge_id)
                mode = _match_mode(material or {}, shader)
                if not mode or mode != reference["matchMode"]:
                    raise ReplayProjectError(
                        "reuse cache material reference failed exact matching"
                    )
                parameters = material.get("parameters")
                if not isinstance(parameters, list) or len(parameters) > MAX_MATERIAL_PARAMETERS:
                    raise ReplayProjectError("material knowledge parameters are invalid")
                block_path = _text(
                    str(material.get("constantBlockPath", "")),
                    "material constantBlockPath",
                    2048,
                )
                head = state["headBindings"]["materials"].get(knowledge_id)
                if not isinstance(head, Mapping):
                    raise ReplayProjectError("material knowledge is not an active head")
                material_candidates.setdefault(block_path, []).append(
                    (
                        head["generation"],
                        {
                            "knowledgeId": knowledge_id,
                            "matchMode": mode,
                            "parameterCount": len(parameters),
                            "constantBlockPath": block_path,
                            "constantBlockLayoutSha256": material[
                                "constantBlockLayoutSha256"
                            ],
                            "materialLayoutLiveRevalidationRequired": True,
                            "supersededCandidateCount": head["historyCount"] - 1,
                        },
                    )
                )
            for block_path in sorted(material_candidates):
                history = material_candidates[block_path]
                unused_activation, active_material = max(
                    history,
                    key=lambda entry: (entry[0], entry[1]["knowledgeId"]),
                )
                active_material["activeCandidate"] = True
                item["materialCandidates"].append(active_material)
            results.append(item)

        return {
            "schema": "renderdoc-ultra-project-reuse-plan/v1",
            "schemaVersion": 1,
            "projectId": manifest["projectId"],
            "projectPath": str(self.root),
            "captureId": capture["captureId"],
            "catalogSha256": capture["catalogSha256"],
            "matchedShaderCount": len(results),
            "matches": results,
            "policy": {
                "automaticHashMatching": True,
                "rawExactOrCodeInterfaceLayoutExactOnly": True,
                "oldWorkspaceManifestReused": False,
                "oldCaptureAbiReportReused": False,
                "oldMaterialSessionReused": False,
                "autoApplyAuthorized": False,
                "compileRequiredAfterSourceSeed": True,
                "verifiedBinaryCandidateMaySkipCompile": True,
                "binaryCandidateRequiresFreshCaptureRebind": True,
                "binaryCandidateRequiresFreshLiveAbiRevalidation": True,
                "oldCompiledCandidateAbiAuthorityReused": False,
                "oldReplacementIdReused": False,
                "liveAbiRevalidationRequiredBeforeShaderApply": True,
                "liveMaterialLayoutRevalidationRequired": True,
                "aliasCandidateDirectLoadAuthorized": False,
                "aliasCandidateMaterializationRequired": True,
                "materializedDisplayAliasAutoLoadAuthorized": True,
                "appendOnlyKnowledgeHistoryPreserved": True,
                "newestValidActiveHeadOnly": True,
            },
        }

    def _reuse_cache_binding(
        self,
        manifest: Mapping[str, Any],
        capture: Mapping[str, Any],
        knowledge_content_sha256: str,
    ) -> dict[str, str]:
        material = {
            "projectId": str(manifest["projectId"]),
            "scopeSha256": _canonical_sha256(manifest["scope"]),
            "captureId": _sha(capture.get("captureId"), "capture.captureId"),
            "catalogSha256": _sha(
                capture.get("catalogSha256"), "capture.catalogSha256"
            ),
            "knowledgeContentSha256": _sha(
                knowledge_content_sha256, "knowledgeContentSha256"
            ),
            "matcherPolicySha256": REUSE_MATCH_POLICY_SHA256,
        }
        return {**material, "cacheKey": _canonical_sha256(material)}

    def _reuse_cache_path(self, cache_key: str, *, create: bool) -> Path:
        digest = _sha(cache_key, "reuse cache key")
        derived = self.root / "derived"
        directory = derived / "reuse-plans"
        for path, label in (
            (derived, "project derived directory"),
            (directory, "project reuse-cache directory"),
        ):
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_dir():
                    raise ReplayProjectError("%s is redirected" % label)
            elif create:
                path.mkdir()
        raw_result = directory / (digest + ".json")
        if raw_result.is_symlink():
            raise ReplayProjectError("reuse cache path is redirected")
        result = raw_result.resolve()
        if not is_within(self.root, result):
            raise ReplayProjectError("reuse cache path escapes the project")
        return result

    def _read_reuse_cache(
        self, binding: Mapping[str, str]
    ) -> tuple[list[dict[str, Any]] | None, Path]:
        path = self._reuse_cache_path(binding["cacheKey"], create=False)
        if not path.exists() and not path.is_symlink():
            return None, path
        if path.is_symlink() or not path.is_file():
            raise ReplayProjectError("reuse cache must be a regular non-symlink file")
        try:
            value = read_json_object(path, MAX_REUSE_CACHE_BYTES, "project reuse cache")
        except ValueError:
            return None, path
        required = {
            "schema",
            "schemaVersion",
            "projectId",
            "scopeSha256",
            "captureId",
            "catalogSha256",
            "knowledgeContentSha256",
            "matcherPolicySha256",
            "cacheKey",
            "mappings",
            "sealSha256",
        }
        if set(value) != required:
            return None, path
        if (
            value.get("schema") != REUSE_CACHE_SCHEMA
            or value.get("schemaVersion") != REUSE_CACHE_VERSION
            or any(value.get(key) != expected for key, expected in binding.items())
        ):
            return None, path
        seal = str(value.get("sealSha256", "") or "")
        if not SHA256_RE.fullmatch(seal):
            return None, path
        unsigned = {key: item for key, item in value.items() if key != "sealSha256"}
        if _canonical_sha256(unsigned) != seal:
            return None, path
        mappings = value.get("mappings")
        if not isinstance(mappings, list) or len(mappings) > MAX_SHADER_COUNT:
            return None, path
        return mappings, path

    def _write_reuse_cache(
        self, binding: Mapping[str, str], mappings: Sequence[Mapping[str, Any]]
    ) -> tuple[bool, Path]:
        path = self._reuse_cache_path(binding["cacheKey"], create=True)
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise ReplayProjectError("reuse cache destination is redirected")
        document: dict[str, Any] = {
            "schema": REUSE_CACHE_SCHEMA,
            "schemaVersion": REUSE_CACHE_VERSION,
            **dict(binding),
            "mappings": list(mappings),
        }
        document["sealSha256"] = _canonical_sha256(document)
        encoded = (
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_REUSE_CACHE_BYTES:
            return False, path
        atomic_write_bytes(path, encoded)
        verified, unused_path = self._read_reuse_cache(binding)
        if verified != list(mappings):
            raise ReplayProjectError("reuse cache failed post-write verification")
        return True, path

    def _reuse_plan(self, manifest: Mapping[str, Any], capture: Mapping[str, Any]) -> dict[str, Any]:
        state = self._reuse_knowledge_state(manifest)
        binding = self._reuse_cache_binding(
            manifest, capture, state["knowledgeContentSha256"]
        )
        mappings, cache_path = self._read_reuse_cache(binding)
        cache_hit = mappings is not None
        stored = cache_hit
        plan = None
        if mappings is not None:
            try:
                plan = self._reuse_plan_from_mappings(
                    manifest, capture, state, mappings
                )
            except (KeyError, TypeError, ValueError, ReplayProjectError):
                # Derived cache bytes never become authority. Any stale or
                # malformed correspondence is recomputed from exact knowledge.
                mappings = None
                cache_hit = False
        if mappings is None:
            mappings = self._build_reuse_mappings(capture, state)
            plan = self._reuse_plan_from_mappings(manifest, capture, state, mappings)
            stored, cache_path = self._write_reuse_cache(binding, mappings)
        if plan is None:
            raise ReplayProjectError("reuse plan was not materialized")
        plan["derivedCache"] = {
            "schema": REUSE_CACHE_SCHEMA,
            "schemaVersion": REUSE_CACHE_VERSION,
            "hit": cache_hit,
            "stored": stored,
            "path": str(cache_path),
            "cacheKey": binding["cacheKey"],
            "catalogSha256": binding["catalogSha256"],
            "knowledgeContentSha256": binding["knowledgeContentSha256"],
            "matcherPolicySha256": binding["matcherPolicySha256"],
            "mappingCount": len(mappings),
            "physicalGateBypassed": False,
        }
        return plan

    def reuse_plan(
        self, capture_id: str, catalog_sha256: str = ""
    ) -> dict[str, Any]:
        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load(persist_migrations=True)
            return self._reuse_plan(
                manifest,
                self._capture(manifest, capture_id, catalog_sha256),
            )

    @staticmethod
    def _capture_from_shader_inventory(
        inventory: Mapping[str, Any],
    ) -> dict[str, Any]:
        capture = inventory["capture"]
        return {
            "captureId": capture["captureId"],
            "catalogSha256": capture["inventorySha256"],
            "api": capture["api"],
            "inventoryOnly": True,
            "shaders": [
                {
                    "resourceId": shader["resourceId"],
                    "stage": shader["stage"],
                    "entryPoint": shader["entryPoint"],
                    "rawSha256": shader["rawSha256"],
                    # The normalized inventory intentionally carries no fuzzy
                    # executable-only key. Source reuse is raw-container exact;
                    # old binary reuse also requires the complete stable key.
                    "codeSha256": "",
                    "interfaceHash": shader["interfaceHash"],
                    "layoutHash": shader["layoutHash"],
                    "interfaceComplete": shader["interfaceComplete"],
                    "api": shader["api"],
                    "encoding": shader["encoding"],
                    "abiHash": shader["abiHash"],
                    "stableKeySha256": shader["stableKeySha256"],
                    "anchorEventId": shader["anchorEventId"],
                    "constantBlocks": copy.deepcopy(shader["constantBlocks"]),
                }
                for shader in inventory["shaders"]
            ],
        }

    @staticmethod
    def _workspace_inventory_stable_identity(
        record: Mapping[str, Any],
        provenance_index: Mapping[tuple[str, str], Mapping[str, Any]],
    ) -> dict[str, Any]:
        inventory_origin = record.get("inventoryOrigin")
        if inventory_origin is not None:
            origin = _inventory_origin_identity(inventory_origin)
            if any(
                (
                    origin["rawSha256"] != record.get("rawSha256"),
                    origin["stage"] != record.get("stage"),
                    origin["entryPoint"] != record.get("entryPoint", ""),
                    origin["abiHash"] != record.get("baselineAbiSha256", ""),
                    origin["layoutHash"] != record.get("layoutHash", ""),
                )
            ):
                raise ReplayProjectError(
                    "workspace inventory origin differs from knowledge identity"
                )
            identity = {
                key: origin[key]
                for key in (
                    "api",
                    "encoding",
                    "rawSha256",
                    "stage",
                    "entryPoint",
                    "abiHash",
                    "layoutHash",
                )
            }
            identity["api"] = _reuse_api_family(identity["api"])
            return {**identity, "stableKeySha256": _stable_shader_identity_sha256(identity)}
        provenance = provenance_index.get(
            (
                _sha(record.get("captureId"), "workspace.captureId"),
                _sha(
                    record.get("originCatalogSha256"),
                    "workspace.originCatalogSha256",
                ),
            )
        )
        candidates = (
            provenance.get("shaders", {}).get(
                (
                    _text(str(record.get("stage", "")), "workspace.stage", 64),
                    _text(
                        str(record.get("entryPoint", "") or ""),
                        "workspace.entryPoint",
                        512,
                        allow_empty=True,
                    ),
                    _sha(record.get("rawSha256"), "workspace.rawSha256"),
                ),
                [],
            )
            if isinstance(provenance, Mapping)
            else []
        )
        if not candidates:
            raise ReplayProjectError(
                "workspace inventory identity has no exact origin Shader"
            )
        identities: dict[str, dict[str, Any]] = {}
        origin_capture = provenance.get("capture")
        if not isinstance(origin_capture, Mapping):
            raise ReplayProjectError("workspace inventory origin capture is invalid")
        for shader in candidates:
            if not isinstance(shader, Mapping):
                raise ReplayProjectError("workspace inventory origin Shader is invalid")
            identity = {
                "api": _reuse_api_family(origin_capture.get("api")),
                "encoding": _shader_encoding(shader.get("encoding")),
                "rawSha256": _sha(
                    record.get("rawSha256"), "workspace.rawSha256"
                ),
                "stage": _normalised_stage(record.get("stage")),
                "entryPoint": _text(
                    str(record.get("entryPoint", "") or ""),
                    "workspace.entryPoint",
                    512,
                    allow_empty=True,
                ),
                "abiHash": _sha(
                    record.get("baselineAbiSha256"),
                    "workspace.baselineAbiSha256",
                    allow_empty=True,
                ),
                "layoutHash": _sha(
                    record.get("layoutHash"),
                    "workspace.layoutHash",
                    allow_empty=True,
                ),
            }
            identities[_stable_shader_identity_sha256(identity)] = identity
        if len(identities) != 1:
            raise ReplayProjectError(
                "workspace origin Shaders have ambiguous stable identities"
            )
        identity = next(iter(identities.values()))
        return {
            **identity,
            "stableKeySha256": _stable_shader_identity_sha256(identity),
        }

    def _inventory_reuse_mappings(
        self,
        manifest: Mapping[str, Any],
        capture: Mapping[str, Any],
        inventory: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        mappings = self._build_reuse_mappings(capture, state)
        def inventory_target_key(shader: Mapping[str, Any]) -> tuple[Any, ...]:
            return (
                shader.get("resourceId"),
                shader.get("stage"),
                shader.get("entryPoint", ""),
                shader.get("rawSha256", ""),
                shader.get("interfaceHash", ""),
                shader.get("layoutHash", ""),
            )

        inventory_by_target: dict[tuple[Any, ...], Mapping[str, Any]] = {}
        for shader in inventory["shaders"]:
            key = inventory_target_key(shader)
            if key in inventory_by_target:
                raise ReplayProjectError(
                    "inventory contains an ambiguous resource/entry target"
                )
            inventory_by_target[key] = shader
        filtered: list[dict[str, Any]] = []
        for raw_mapping in mappings:
            mapping = copy.deepcopy(dict(raw_mapping))
            target = inventory_by_target.get(inventory_target_key(mapping["shader"]))
            if target is None:
                raise ReplayProjectError(
                    "inventory reuse mapping selected an absent target Shader"
                )
            workspace_refs: list[dict[str, Any]] = []
            for reference in mapping["workspaces"]:
                record = state["workspaces"].get(reference.get("knowledgeId"))
                if not isinstance(record, Mapping):
                    raise ReplayProjectError(
                        "inventory reuse mapping references absent workspace knowledge"
                    )
                # An exact original program can recover its source even when
                # old reflection/ABI metadata differs. The plan below keeps
                # the original evidence separate and disables binary reuse
                # until a fresh compile and target ABI validation succeed.
                if reference.get("matchMode") == "raw-exact":
                    if self._newer_program_source(state, record) is not None:
                        raise ReplayProjectError("newer equivalent Shader source requires target preparation")
                    workspace_refs.append(dict(reference))
            mapping["workspaces"] = workspace_refs
            # Resource inventories are not event/value snapshots. Persisted
            # material values stay on the explicit EID/live-session path.
            mapping["materials"] = []
            if workspace_refs or mapping["aliases"] or raw_mapping["workspaces"]:
                # Preserve an exact raw/API/encoding target. No code-only or
                # fuzzy match can authorize an inventory source seed.
                filtered.append(mapping)
        return filtered

    def _inventory_knowledge_snapshot(
        self,
        manifest: Mapping[str, Any],
        capture: Mapping[str, Any],
        inventory: Mapping[str, Any],
        state: Mapping[str, Any],
        restore_scope: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        provenance_index = self._restore_provenance_index(manifest)
        workspaces: dict[str, dict[str, Any]] = {}
        aliases: list[dict[str, Any]] = []
        materials: dict[str, dict[str, Any]] = {}
        artifact_refs: list[dict[str, Any]] = []

        alias_refs_by_workspace: dict[str, set[tuple[str, int]]] = {}
        binary_knowledge_ids: set[str] = set()
        for match in plan["matches"]:
            refs = {
                (str(item["knowledgeId"]), int(item["groupIndex"]))
                for item in match["aliasCandidates"]
            }
            for candidate in match["workspaceCandidates"]:
                if candidate.get("binaryCandidateEligible") is True:
                    binary_knowledge_ids.add(str(candidate["knowledgeId"]))
                alias_refs_by_workspace.setdefault(
                    str(candidate["knowledgeId"]), set()
                ).update(refs)

        for knowledge_id in restore_scope["workspaceIds"]:
            record = self._workspace_knowledge_from_manifest(
                manifest,
                knowledge_id,
                record=state["workspaces"][knowledge_id],
                source_artifact_verified=True,
                provenance_index=provenance_index,
            )
            source_path = self._artifact(record["sourcePath"], "workspace source")
            source_bytes = _file_bytes(
                source_path, MAX_SOURCE_BYTES, "workspace source"
            )
            stable_identity = self._workspace_inventory_stable_identity(
                record, provenance_index
            )
            source_ref = {
                "kind": "verified-" + _workspace_source_format(record),
                "knowledgeId": knowledge_id,
                "artifactPath": str(source_path),
                "sha256": record["sourceSha256"],
                "byteSize": len(source_bytes),
            }
            artifact_refs.append(source_ref)
            compiled_plan: dict[str, Any] | None = None
            compiler_binding = record.get("compilerBinding")
            compiler_binding_identity = (
                _compiler_binding_identity(compiler_binding)
                if compiler_binding is not None
                else None
            )
            if (
                knowledge_id in binary_knowledge_ids
                and record.get("compiledCandidate") is not None
                and compiler_binding_identity is not None
            ):
                compiled = _compiled_candidate_identity(
                    record["compiledCandidate"]
                )
                compiled_path = self._artifact(
                    record["compiledCandidate"]["artifactPath"],
                    "workspace compiled candidate",
                )
                compiled_bytes = _file_bytes(
                    compiled_path,
                    MAX_COMPILED_CANDIDATE_BYTES,
                    "workspace compiled candidate",
                )
                compiled_plan = {
                    "schema": compiled["schema"],
                    "schemaVersion": compiled["schemaVersion"],
                    "artifactPath": str(compiled_path),
                    "sha256": compiled["rawSha256"],
                    "codeSha256": compiled["codeSha256"],
                    "byteSize": compiled["byteSize"],
                    "kind": compiled["kind"],
                    "sourceSha256": compiled["sourceSha256"],
                    "sourceRevision": compiled["sourceRevision"],
                    "targetProfile": copy.deepcopy(compiled["targetProfile"]),
                    "compilerBindingSha256": compiler_binding_identity[
                        "bindingSha256"
                    ],
                    "candidateOnly": True,
                    "contentAddressed": True,
                    "integrityVerified": True,
                    "originValidationEvidenceOnly": True,
                    "applyAuthorization": False,
                    "freshCaptureRebindRequired": True,
                    "freshLiveAbiRevalidationRequired": True,
                    "oldReplacementIdReused": False,
                    "oldCaptureAbiAuthorityReused": False,
                }
                if compiled.get("validationScope") == "manual-edit-unvalidated":
                    compiled_plan.update(manualEdit=True, validationScope="manual-edit-unvalidated",
                        manualApplyReceipt=copy.deepcopy(compiled["manualApplyReceipt"]),
                        originValidationEvidenceOnly=False, freshLiveAbiRevalidationRequired=False)
                artifact_refs.append(
                    {
                        "kind": "verified-compiled-candidate",
                        "knowledgeId": knowledge_id,
                        "artifactPath": str(compiled_path),
                        "sha256": compiled["rawSha256"],
                        "byteSize": len(compiled_bytes),
                    }
                )
            analysis_ref = None
            if record.get("analysisPath"):
                analysis_path = self._artifact(
                    record["analysisPath"], "workspace analysis"
                )
                analysis_bytes = _file_bytes(
                    analysis_path, MAX_ANALYSIS_BYTES, "workspace analysis"
                )
                analysis_ref = {
                    "kind": "explanatory-analysis",
                    "knowledgeId": knowledge_id,
                    "artifactPath": str(analysis_path),
                    "sha256": record["analysisSha256"],
                    "byteSize": len(analysis_bytes),
                    "physicalEvidence": False,
                    "applyAuthorization": False,
                }
                artifact_refs.append(analysis_ref)
            portable_disassembly: list[dict[str, Any]] = []
            for raw_disassembly in record.get(
                "assemblyDisassemblyArtifacts", []
            ):
                disassembly = _assembly_disassembly_identity(raw_disassembly)
                disassembly_path = self._artifact(
                    raw_disassembly["artifactPath"],
                    "workspace disassembly artifact",
                )
                disassembly_bytes = _file_bytes(
                    disassembly_path,
                    MAX_SOURCE_BYTES,
                    "workspace disassembly artifact",
                )
                disassembly_ref = {
                    "kind": "verified-%s" % disassembly["kind"],
                    "knowledgeId": knowledge_id,
                    "artifactPath": str(disassembly_path),
                    "sha256": disassembly["sha256"],
                    "byteSize": len(disassembly_bytes),
                }
                artifact_refs.append(disassembly_ref)
                portable_disassembly.append(
                    {**disassembly, "artifactPath": str(disassembly_path)}
                )
            workspaces[knowledge_id] = {
                "schema": "renderdoc-ultra-project-shader-artifact/v1",
                "schemaVersion": 1,
                "knowledgeId": knowledge_id,
                "source": source_ref,
                **({"sourceFormat": _workspace_source_format(record)}
                   if _workspace_source_format(record) != "hlsl" else {}),
                "registeredWorkspace": {
                    "workspaceId": record["workspaceId"],
                    "path": str(record.get("workspacePath") or ""),
                },
                "publicationMode": record.get("publicationMode"),
                "sourceOriginPath": record.get("sourceOriginPath"),
                "registeredCompiledCandidatePresent": record.get("compiledCandidate") is not None,
                "targetProfile": copy.deepcopy(record["targetProfile"]),
                "physicalIdentity": stable_identity,
                "interfaceHash": record["interfaceHash"],
                "interfaceComplete": record["interfaceComplete"] is True,
                "compiledCandidate": compiled_plan,
                "compilerBinding": copy.deepcopy(
                    record.get("compilerBinding")
                ),
                "assemblyDisassemblyArtifacts": portable_disassembly,
                "analysis": analysis_ref,
                "cbufferAliasRefs": [
                    {"knowledgeId": alias_id, "groupIndex": group_index}
                    for alias_id, group_index in sorted(
                        alias_refs_by_workspace.get(knowledge_id, set())
                    )
                ],
                "originResourceIdReused": False,
                "originReplacementHandleReused": False,
                "originSessionAuthorityReused": False,
            }

        for alias_ref in restore_scope.get("aliasRefs", ()):
            key = (alias_ref["knowledgeId"], alias_ref["groupIndex"])
            alias_set, group = state["aliases"][key]
            alias_path = self._artifact(
                alias_set["artifactPath"], "alias artifact"
            )
            alias_bytes = _file_bytes(alias_path, MAX_ALIAS_BYTES, "alias artifact")
            artifact_ref = {
                "kind": "verified-cbuffer-alias-table",
                "knowledgeId": key[0],
                "artifactPath": str(alias_path),
                "sha256": alias_set["artifactSha256"],
                "byteSize": len(alias_bytes),
            }
            if artifact_ref not in artifact_refs:
                artifact_refs.append(artifact_ref)
            aliases.append(
                {
                    "knowledgeId": key[0],
                    "groupIndex": key[1],
                    "group": copy.deepcopy(dict(group)),
                    "artifact": artifact_ref,
                    "displayAliasesOnly": True,
                    "directLoadAuthorized": False,
                    "targetReflectionMaterializationRequired": True,
                }
            )

        for knowledge_id in restore_scope["materialIds"]:
            materials[knowledge_id] = self._material_knowledge_from_manifest(
                manifest,
                knowledge_id,
                record=state["materials"][knowledge_id],
                provenance_index=provenance_index,
            )

        artifact_refs.sort(
            key=lambda item: (
                item["kind"], item["knowledgeId"], item["sha256"]
            )
        )
        knowledge_ids = {
            "workspaces": list(restore_scope["workspaceIds"]),
            "aliases": [copy.deepcopy(dict(item)) for item in restore_scope.get("aliasRefs", ())],
            "materials": list(restore_scope["materialIds"]),
        }
        scope_sha256 = _canonical_sha256(manifest["scope"])
        binding: dict[str, Any] = {
            "schema": SHADER_INVENTORY_BINDING_SCHEMA,
            "schemaVersion": SHADER_INVENTORY_BINDING_VERSION,
            "projectId": manifest["projectId"],
            "scopeSha256": scope_sha256,
            "captureId": capture["captureId"],
            "catalogSha256": capture["catalogSha256"],
            "inventorySha256": inventory["capture"]["inventorySha256"],
            "knowledgeContentSha256": restore_scope["knowledgeContentSha256"],
            "matcherPolicySha256": REUSE_MATCH_POLICY_SHA256,
            "activeKnowledgeRevision": self._active_knowledge_state(manifest)[
                "revision"
            ],
            "knowledgeIds": knowledge_ids,
            "knowledgeIdsSha256": _canonical_sha256(knowledge_ids),
            "artifactRefs": artifact_refs,
            "artifactRefsSha256": _canonical_sha256(artifact_refs),
        }
        binding["bindingSha256"] = _canonical_sha256(binding)
        knowledge = {
            "workspaces": workspaces,
            "aliases": aliases,
            "materials": materials,
        }
        snapshot_material = {
            "binding": binding,
            "knowledge": knowledge,
        }
        return {
            "binding": binding,
            "knowledge": knowledge,
            "snapshotSha256": _canonical_sha256(snapshot_material),
        }

    def _reuse_snapshot_for_normalized_inventory(
        self,
        manifest: Mapping[str, Any],
        inventory: Mapping[str, Any],
    ) -> dict[str, Any]:
        capture = self._capture_from_shader_inventory(inventory)
        state = self._reuse_knowledge_state(manifest)
        mappings = self._inventory_reuse_mappings(
            manifest, capture, inventory, state
        )
        plan = self._reuse_plan_from_mappings(
            manifest, capture, state, mappings
        )
        provenance_index = self._restore_provenance_index(manifest)
        for match in plan["matches"]:
            if (not match["workspaceCandidates"] and
                    self._indexed_reuse_matches(state["indexes"]["workspaces"], match["shader"])):
                match["sourcePreparationBlockedReason"] = "project-shader-stable-identity-mismatch"
            anchor_available = match["shader"].get("anchorEventId") is not None
            match["targetStableKeySha256"] = match["shader"][
                "stableKeySha256"
            ]
            match["targetAnchorAvailable"] = anchor_available
            for candidate in match["workspaceCandidates"]:
                origin = state["workspaces"][candidate["knowledgeId"]]
                origin_identity = self._workspace_inventory_stable_identity(
                    origin, provenance_index
                )
                stable_key_matched = (
                    origin_identity["stableKeySha256"]
                    == match["shader"]["stableKeySha256"]
                )
                source_preparation_only = (
                    origin.get("interfaceComplete") is not True
                    or match["shader"].get("interfaceComplete") is not True
                    or not stable_key_matched
                )
                candidate["sourcePreparationOnly"] = source_preparation_only
                candidate["originStableKeySha256"] = origin_identity["stableKeySha256"]
                candidate["targetStableKeySha256"] = match["shader"]["stableKeySha256"]
                candidate["stableKeyMatched"] = stable_key_matched
                candidate["requiresFreshCompile"] = source_preparation_only
                if source_preparation_only:
                    # Source recovery does not inherit old ABI or binary
                    # authority. Resolve both from the current target.
                    candidate["sourceSeedEligible"] = True
                    candidate.pop("blockedReason", None)
                    candidate["binaryCandidateEligible"] = False
                    candidate["binaryCandidate"] = None
                    candidate["binaryBlockedReason"] = (
                        "fresh compile required for changed ABI/layout evidence"
                        if not stable_key_matched else
                        "fresh compile required for incomplete interface evidence"
                    )
                candidate["targetAnchorAvailable"] = anchor_available
                candidate["targetAnchorRequiredBeforeExecutableApply"] = True
                candidate["binaryExecutableAutoApplyEligible"] = False
        plan["inventory"] = copy.deepcopy(inventory["capture"])
        plan["policy"].update(
            {
                "inventoryOnly": True,
                "fuzzyExecutableMatchAllowed": False,
                "stableKeyExactRequiredForSourceOrBinary": False,
                "stableKeyExactRequiredForSource": False,
                "rawExactSourcePreparationAllowed": True,
                "stableKeyExactRequiredForBinary": True,
                "targetAnchorRequiredBeforeExecutableApply": True,
                "binaryExecutableAutoApplyAuthorized": False,
                "inventoryMaterialValueReuseAllowed": False,
            }
        )
        restore_scope = self._referenced_knowledge_scope_from_mappings(
            capture, state, mappings
        )
        snapshot = self._inventory_knowledge_snapshot(
            manifest,
            capture,
            inventory,
            state,
            restore_scope,
            plan,
        )
        return {
            "schema": SHADER_INVENTORY_REUSE_SCHEMA,
            "schemaVersion": SHADER_INVENTORY_REUSE_VERSION,
            "projectId": manifest["projectId"],
            "projectPath": str(self.root),
            "inventory": copy.deepcopy(inventory),
            "reusePlan": plan,
            **snapshot,
        }

    def reuse_snapshot_for_shader_inventory(
        self, inventory: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Match and snapshot project knowledge without importing an EID catalog."""

        normalized = _normalise_shader_inventory(inventory)
        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load(persist_migrations=True)
            return self._reuse_snapshot_for_normalized_inventory(
                manifest, normalized
            )

    def reuse_plan_for_shader_inventory(
        self, inventory: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Return the plan half of one strict inventory snapshot."""

        return self.reuse_snapshot_for_shader_inventory(inventory)["reusePlan"]

    def restore_knowledge_snapshot_for_shader_inventory(
        self,
        inventory: Mapping[str, Any],
        matched_knowledge_ids: Mapping[str, Any] | None = None,
        expected_binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Revalidate a live-inventory snapshot and optional RDC cache binding."""

        result = self.reuse_snapshot_for_shader_inventory(inventory)
        binding = result["binding"]
        if matched_knowledge_ids is not None:
            if (
                not isinstance(matched_knowledge_ids, Mapping)
                or dict(matched_knowledge_ids) != binding["knowledgeIds"]
            ):
                raise ReplayProjectError(
                    "shader inventory matched knowledge IDs changed"
                )
        if expected_binding is not None:
            if not isinstance(expected_binding, Mapping) or dict(expected_binding) != binding:
                raise ReplayProjectError(
                    "shader inventory project binding is stale or changed"
                )
        return result

    def validate_shader_inventory_snapshot(
        self,
        inventory: Mapping[str, Any],
        binding: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Hash-verify an RDC cache hint against the current project revision."""

        if not isinstance(binding, Mapping) or set(binding) != {
            "schema",
            "schemaVersion",
            "projectId",
            "scopeSha256",
            "captureId",
            "catalogSha256",
            "inventorySha256",
            "knowledgeContentSha256",
            "matcherPolicySha256",
            "activeKnowledgeRevision",
            "knowledgeIds",
            "knowledgeIdsSha256",
            "artifactRefs",
            "artifactRefsSha256",
            "bindingSha256",
        }:
            raise ReplayProjectError("shader inventory binding fields are invalid")
        unsigned = dict(binding)
        declared_binding_sha256 = _sha(
            unsigned.pop("bindingSha256"), "shader inventory bindingSha256"
        )
        if (
            binding.get("schema") != SHADER_INVENTORY_BINDING_SCHEMA
            or binding.get("schemaVersion") != SHADER_INVENTORY_BINDING_VERSION
            or _canonical_sha256(unsigned) != declared_binding_sha256
        ):
            raise ReplayProjectError("shader inventory binding seal is invalid")
        return self.restore_knowledge_snapshot_for_shader_inventory(
            inventory,
            binding.get("knowledgeIds"),
            binding,
        )

    def materialize_alias_candidates(
        self,
        project_id: str,
        capture_id: str,
        catalog_sha256: str,
        target_shader: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Rebind project display aliases to one exact target catalog Shader.

        The returned ``jsonBytes`` are ready to be atomically written and
        passed to qrenderdoc's ``load_symbol_aliases`` with the returned
        ``sha256``.  This method never applies aliases or reuses source capture
        ResourceIds, sessions, replacements, or raw CBuffer values.
        """

        from .project_alias_rebind import materialize_alias_candidates

        return materialize_alias_candidates(
            self,
            project_id,
            capture_id,
            catalog_sha256,
            target_shader,
            candidates,
        )

    def materialize_alias_batch(
        self,
        project_id: str,
        capture_id: str,
        catalog_sha256: str,
        targets: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Rebind all aliases for one restore from one project snapshot."""

        from .project_alias_rebind import materialize_alias_batch

        return materialize_alias_batch(
            self,
            project_id,
            capture_id,
            catalog_sha256,
            targets,
        )

    def materialize_alias_batch_from_inventory(
        self,
        project_id: str,
        inventory: Mapping[str, Any],
        expected_binding: Mapping[str, Any],
        targets: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Rebind display aliases using only strict live inventory reflection."""

        from .project_alias_rebind import materialize_alias_batch_from_inventory

        return materialize_alias_batch_from_inventory(
            self,
            project_id,
            inventory,
            expected_binding,
            targets,
        )

    def materialize_alias_candidate(
        self,
        project_id: str,
        capture_id: str,
        catalog_sha256: str,
        target_shader: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Compatibility wrapper for a single unambiguous alias candidate."""

        return self.materialize_alias_candidates(
            project_id,
            capture_id,
            catalog_sha256,
            target_shader,
            [candidate],
        )

    def seed_workspace(self, knowledge_id: str, destination_status: Mapping[str, Any],
                       *, supersede_local_edit: bool = False,
                       published_source_advance: Mapping[str, Any] | None = None) -> dict[str, Any]:
        token = str(knowledge_id or "").strip().lower()
        if not HEX32_RE.fullmatch(token):
            raise ReplayProjectError("knowledge_id must be 32 lowercase hexadecimal characters")
        if not isinstance(destination_status, Mapping) or destination_status.get("schema") != "renderdoc-ultra-shader-workspace-status/v1":
            raise ReplayProjectError("destination workspace status is invalid")
        source_status = destination_status.get("source")
        baseline = destination_status.get("baseline")
        capture_status = destination_status.get("capture")
        shader_status = destination_status.get("shader")
        if not all(isinstance(value, Mapping) for value in (source_status, baseline, capture_status, shader_status)):
            raise ReplayProjectError("destination workspace provenance is incomplete")
        if source_status.get("present") is not True:
            raise ReplayProjectError("destination source must be present and still unmodified")
        if (
            source_status.get("lane") != "executable-editable"
            or source_status.get("format") not in {"hlsl", "glsl"}
        ):
            raise ReplayProjectError(
                "project source seeding supports executable-editable HLSL or GLSL"
            )
        capture_id = _sha(
            capture_status.get("captureId"), "destination.captureId"
        )
        catalog_revision_sha256 = self._capture_status_catalog_sha256(
            capture_status
        )
        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load(persist_migrations=True)
            records = [item for item in manifest["knowledge"]["workspaces"] if item["knowledgeId"] == token]
            if len(records) != 1:
                raise ReplayProjectError("unknown project workspace knowledge ID")
            knowledge = records[0]
            if published_source_advance is not None:
                current_advance = self._published_workspace_source_advance(manifest, token, destination_status)
                if (not isinstance(published_source_advance, Mapping) or current_advance is None
                        or set(published_source_advance) != set(current_advance)
                        or any(type(published_source_advance[key]) is not type(value)
                               or published_source_advance[key] != value
                               for key, value in current_advance.items())):
                    raise ReplayProjectError("published workspace source/head changed before seed")
            if (source_status.get("modifiedSinceDecompile") is not False
                    and not supersede_local_edit
                    and published_source_advance is None
                    and source_status.get("currentSha256") != knowledge.get("sourceSha256")):
                raise ReplayProjectError("destination source must be present and still unmodified")
            if _workspace_source_format(knowledge) != source_status["format"]:
                raise ReplayProjectError("project and destination source formats differ")
            capture = self._capture_for_provenance(
                manifest, capture_id, catalog_revision_sha256
            )
            shader = self._capture_shader(
                capture,
                resource_id=_positive(shader_status.get("resourceId"), "destination.resourceId"),
                stage=_text(str(destination_status.get("stage", "")), "destination.stage", 64),
                entry_point=_text(str(destination_status.get("entryPoint", "") or ""), "destination.entryPoint", 512, True),
                raw_sha256=_sha(baseline.get("rawSha256"), "destination.rawSha256"),
            )
            mode = _match_mode(knowledge, shader)
            if not mode:
                raise ReplayProjectError("project source knowledge does not match the destination shader")
            if (
                knowledge.get("targetProfile") != destination_status.get("targetProfile")
                or (mode != "raw-exact" and (
                    not knowledge.get("baselineAbiSha256")
                    or knowledge.get("baselineAbiSha256") != baseline.get("abiSha256")
                ))
            ):
                raise ReplayProjectError("destination target profile/physical ABI differs from project knowledge")
            _sha(baseline.get("abiSha256"), "destination.abiSha256")
            project_source = self._artifact(knowledge["sourcePath"], "workspace source")
            source_bytes = _file_bytes(project_source, MAX_SOURCE_BYTES, "workspace source")
            if _sha256_bytes(source_bytes) != knowledge["sourceSha256"]:
                raise ReplayProjectError("project source bytes changed")
            destination = _normal_path(str(source_status.get("path", "")))
            workspace_root = _normal_path(str(destination_status.get("workspacePath", "")))
            if destination.is_symlink() or not destination.is_file() or not is_within(workspace_root, destination):
                raise ReplayProjectError("destination source path is redirected or escapes its workspace")
            expected_destination_hash = _sha(
                source_status.get("currentSha256"),
                "destination.source.currentSha256",
            )
            project_bundle = (
                self._prepare_workspace_source_locked(token, destination_status, manifest)
                if (source_status.get("projectSource") is not None
                    or published_source_advance is not None
                    or source_status.get("currentSha256") == knowledge["sourceSha256"]) else None
            )
            with _workspace_seed_lock(workspace_root):
                current_bytes = _file_bytes(destination, MAX_SOURCE_BYTES, "destination source")
                if _sha256_bytes(current_bytes) != expected_destination_hash:
                    raise ReplayProjectError(
                        "destination source changed after workspace validation"
                    )
                if published_source_advance is not None:
                    self._recheck_published_source_seed(workspace_root, destination_status)
                superseded = _backup_local_edit(
                    workspace_root, destination, current_bytes,
                    superseding=bool(source_status.get("modifiedSinceDecompile"))
                    and (supersede_local_edit or published_source_advance is not None))
                _write_seeded_workspace_source(
                    workspace_root, destination, current_bytes, source_bytes,
                    destination_status, project_bundle)
                if (
                    _sha256_bytes(
                        _file_bytes(destination, MAX_SOURCE_BYTES, "seeded source")
                    )
                    != knowledge["sourceSha256"]
                ):
                    raise ReplayProjectError("seeded workspace source hash mismatch")
            return {
                "supersededLocalEdit": superseded,
                "schema": "renderdoc-ultra-project-workspace-seed/v1",
                "schemaVersion": 1,
                "projectId": manifest["projectId"],
                "captureId": capture["captureId"],
                "knowledgeId": token,
                "workspacePath": str(workspace_root),
                "sourcePath": str(destination),
                "sourceSha256": knowledge["sourceSha256"],
                "matchMode": mode,
                "applied": False,
                "compiled": False,
                "requiresCompile": True,
                "requiresLiveAbiRevalidation": True,
            }

    def seed_workspace_from_inventory(
        self,
        knowledge_id: str,
        destination_status: Mapping[str, Any],
        target_shader: Mapping[str, Any],
        inventory: Mapping[str, Any],
        expected_binding: Mapping[str, Any] | None = None,
        supersede_local_edit: bool = False,
        published_source_advance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Seed verified editable source using a fresh resource inventory.

        This path intentionally does not require the target RDC to be imported
        as a full one-EID catalog.  The current target ResourceId, profile,
        baseline ABI, container kind and stable key are all revalidated before
        the source file is replaced.  No binary is applied here.
        """

        token = str(knowledge_id or "").strip().lower()
        if not HEX32_RE.fullmatch(token):
            raise ReplayProjectError(
                "knowledge_id must be 32 lowercase hexadecimal characters"
            )
        normalized = _normalise_shader_inventory(inventory)
        if not isinstance(target_shader, Mapping):
            raise ReplayProjectError("target inventory Shader is invalid")
        exact_targets = [
            shader
            for shader in normalized["shaders"]
            if dict(target_shader) == shader
        ]
        if len(exact_targets) != 1:
            raise ReplayProjectError(
                "target Shader is not one exact normalized inventory record"
            )
        target = exact_targets[0]
        if (
            not isinstance(destination_status, Mapping)
            or destination_status.get("schema")
            != "renderdoc-ultra-shader-workspace-status/v1"
        ):
            raise ReplayProjectError("destination workspace status is invalid")
        source_status = destination_status.get("source")
        baseline = destination_status.get("baseline")
        capture_status = destination_status.get("capture")
        shader_status = destination_status.get("shader")
        if not all(
            isinstance(value, Mapping)
            for value in (source_status, baseline, capture_status, shader_status)
        ):
            raise ReplayProjectError("destination workspace provenance is incomplete")
        baseline_abi = _sha(baseline.get("abiSha256"), "destination.abiSha256",
                            allow_empty=True)
        incomplete_inventory = target.get("interfaceComplete") is not True
        if (
            source_status.get("present") is not True
            or source_status.get("lane") != "executable-editable"
            or source_status.get("format") not in {"hlsl", "glsl"}
        ):
            raise ReplayProjectError(
                "destination must contain unmodified executable-editable HLSL or GLSL"
            )
        if _positive(
            shader_status.get("resourceId"), "destination.resourceId"
        ) != target["resourceId"]:
            raise ReplayProjectError(
                "destination uses a different fresh target ResourceId"
            )
        if (
            _normalised_stage(destination_status.get("stage")) != target["stage"]
            or _text(
                str(destination_status.get("entryPoint", "") or ""),
                "destination.entryPoint",
                512,
                allow_empty=True,
            )
            != target["entryPoint"]
            or _sha(baseline.get("rawSha256"), "destination.rawSha256")
            != target["rawSha256"]
            or _sha(shader_status.get("rawSha256"), "destination.shader.rawSha256")
            != target["rawSha256"]
            or (not incomplete_inventory and not self._workspace_inventory_abi_matches(
                destination_status, target["abiHash"]))
            or _shader_encoding(baseline.get("kind")) != target["encoding"]
        ):
            raise ReplayProjectError(
                "destination physical Shader identity differs from inventory"
            )
        capture_api = str(capture_status.get("api", "") or "")
        if capture_api and _reuse_api_family(capture_api) != _reuse_api_family(target["api"]):
            raise ReplayProjectError("destination capture API differs from inventory")

        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load(persist_migrations=True)
            snapshot = self._reuse_snapshot_for_normalized_inventory(
                manifest, normalized
            )
            if expected_binding is not None and (
                not isinstance(expected_binding, Mapping)
                or dict(expected_binding) != snapshot["binding"]
            ):
                raise ReplayProjectError(
                    "shader inventory project binding is stale or changed"
                )
            if token not in snapshot["binding"]["knowledgeIds"]["workspaces"]:
                raise ReplayProjectError(
                    "workspace knowledge is not matched by this inventory"
                )
            if incomplete_inventory:
                # An incomplete catalog ABI is not the workspace's live ABI.
                # Only an exact original-program source match may cross this
                # boundary; no cached executable gains replacement authority.
                matched_source = any(
                    match["shader"].get("resourceId") == target["resourceId"]
                    and match["shader"].get("stableKeySha256") == target["stableKeySha256"]
                    and any(candidate.get("knowledgeId") == token
                            and candidate.get("sourcePreparationOnly") is True
                            and candidate.get("sourceSeedEligible") is True
                            and candidate.get("binaryCandidateEligible") is False
                            and candidate.get("binaryCandidate") is None
                            for candidate in match["workspaceCandidates"])
                    for match in snapshot["reusePlan"]["matches"])
                if not matched_source:
                    raise ReplayProjectError("incomplete inventory has no verified source-only target match")
            records = [
                item
                for item in manifest["knowledge"]["workspaces"]
                if item.get("knowledgeId") == token
            ]
            if len(records) != 1:
                raise ReplayProjectError("unknown project workspace knowledge ID")
            knowledge = records[0]
            if published_source_advance is not None:
                current_advance = self._published_workspace_source_advance(
                    manifest, token, destination_status)
                if (not isinstance(published_source_advance, Mapping)
                        or current_advance is None
                        or set(published_source_advance) != set(current_advance)
                        or any(type(published_source_advance[key]) is not type(value)
                               or published_source_advance[key] != value
                               for key, value in current_advance.items())):
                    raise ReplayProjectError("published workspace source/head changed before seed")
            if (source_status.get("modifiedSinceDecompile") is not False
                    and not supersede_local_edit
                    and published_source_advance is None
                    and source_status.get("currentSha256") != knowledge.get("sourceSha256")):
                raise ReplayProjectError("destination source must be present and still unmodified")
            if _workspace_source_format(knowledge) != source_status["format"]:
                raise ReplayProjectError("project and destination source formats differ")
            provenance_index = self._restore_provenance_index(manifest)
            origin_identity = self._workspace_inventory_stable_identity(
                knowledge, provenance_index
            )
            if (
                _match_mode(knowledge, target) != "raw-exact"
                or origin_identity["api"] != target["api"]
                or origin_identity["encoding"] != target["encoding"]
            ):
                raise ReplayProjectError(
                    "project source does not match the exact original target Shader"
                )
            if knowledge.get("targetProfile") != destination_status.get("targetProfile"):
                raise ReplayProjectError(
                    "destination target profile differs from project knowledge"
                )
            stable_key_matched = origin_identity["stableKeySha256"] == target["stableKeySha256"]
            binary_eligible = bool(
                stable_key_matched
                and knowledge.get("interfaceComplete") is True
                and target.get("interfaceComplete") is True
                and knowledge.get("compiledCandidate")
                and knowledge.get("compilerBinding")
            )
            project_source = self._artifact(
                knowledge["sourcePath"], "workspace source"
            )
            source_bytes = _file_bytes(
                project_source, MAX_SOURCE_BYTES, "workspace source"
            )
            if _sha256_bytes(source_bytes) != knowledge["sourceSha256"]:
                raise ReplayProjectError("project source bytes changed")
            destination = _normal_path(str(source_status.get("path", "")))
            workspace_root = _normal_path(
                str(destination_status.get("workspacePath", ""))
            )
            if (
                destination.is_symlink()
                or not destination.is_file()
                or not is_within(workspace_root, destination)
            ):
                raise ReplayProjectError(
                    "destination source path is redirected or escapes its workspace"
                )
            expected_destination_hash = _sha(
                source_status.get("currentSha256"),
                "destination.source.currentSha256",
            )
            project_bundle = (
                self._prepare_workspace_source_locked(token, destination_status, manifest)
                if (source_status.get("projectSource") is not None or incomplete_inventory
                    or published_source_advance is not None
                    or source_status.get("currentSha256") == knowledge["sourceSha256"]) else None
            )
            with _workspace_seed_lock(workspace_root):
                current_bytes = _file_bytes(destination, MAX_SOURCE_BYTES, "destination source")
                if _sha256_bytes(current_bytes) != expected_destination_hash:
                    raise ReplayProjectError(
                        "destination source changed after workspace validation"
                    )
                if published_source_advance is not None:
                    self._recheck_published_source_seed(workspace_root, destination_status)
                superseded = _backup_local_edit(
                    workspace_root, destination, current_bytes,
                    superseding=bool(source_status.get("modifiedSinceDecompile"))
                    and (supersede_local_edit or published_source_advance is not None))
                _write_seeded_workspace_source(
                    workspace_root, destination, current_bytes, source_bytes,
                    destination_status, project_bundle)
                if _sha256_bytes(
                    _file_bytes(destination, MAX_SOURCE_BYTES, "seeded source")
                ) != knowledge["sourceSha256"]:
                    raise ReplayProjectError("seeded workspace source hash mismatch")
            return {
                "supersededLocalEdit": superseded,
                "schema": "renderdoc-ultra-project-workspace-inventory-seed/v1",
                "schemaVersion": 1,
                "projectId": manifest["projectId"],
                "captureId": normalized["capture"]["captureId"],
                "catalogSha256": normalized["capture"]["inventorySha256"],
                "knowledgeId": token,
                "targetResourceId": target["resourceId"],
                "targetStableKeySha256": target["stableKeySha256"],
                "workspacePath": str(workspace_root),
                "sourcePath": str(destination),
                "sourceSha256": knowledge["sourceSha256"],
                "matchMode": "raw-and-stable-key-exact" if stable_key_matched else "raw-exact",
                "originStableKeySha256": origin_identity["stableKeySha256"],
                "stableKeyMatched": stable_key_matched,
                "applied": False,
                "compiled": False,
                "requiresCompile": True,
                "verifiedBinaryCandidateMaySkipCompile": binary_eligible,
                "requiresFreshTargetResourceBind": True,
                "requiresLiveAbiRevalidation": True,
                "oldResourceIdReused": False,
                "oldReplacementIdReused": False,
                "oldCaptureSessionReused": False,
            }
