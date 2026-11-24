"""Deterministic matching between a RenderDoc shader catalog and asset symbols.

This module deliberately does not call a model.  It produces auditable evidence,
auto-selects only unique byte-identical or ABI-validated matches, and leaves all
other choices for an AI/user proposal that must pass :func:`validate_candidate`.
"""

from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

from .dxbc import fingerprint_bytes
from .scanner import interface_hash, interface_is_complete, layout_hash, normalize_stage
from .store import SymbolStore


CATALOG_SCHEMA = "renderdoc-ultra-capture-symbols/v1"
CATALOG_VERSION = 1
ALIAS_SCHEMA = "renderdoc-ultra-aliases/v1"
ALIAS_VERSION = 1
MAX_CATALOG_BYTES = 256 * 1024 * 1024
MAX_SHADER_BLOB_BYTES = 512 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _resource_id(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    match = re.search(r"(\d+)\s*$", str(value or ""))
    return int(match.group(1)) if match else 0


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise ValueError("Capture catalog is not a file: %s" % path)
    size = path.stat().st_size
    if size <= 0 or size > MAX_CATALOG_BYTES:
        raise ValueError(
            "Capture catalog size must be between 1 and %d bytes: %s"
            % (MAX_CATALOG_BYTES, path)
        )
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object in %s" % path)
    return data


def _sha256(value: Any, field: str, allow_empty: bool = True) -> str:
    result = str(value or "").strip().lower()
    if not result and allow_empty:
        return ""
    if not SHA256_RE.fullmatch(result):
        raise ValueError("%s must be a lowercase/uppercase SHA-256 hex digest" % field)
    return result


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def capture_identity(catalog: dict, shaders: Iterable[dict]) -> str:
    """Return the bridge-compatible, content-bound capture catalog identity."""

    identity = {
        "capturePath": str(catalog.get("capturePath", "") or ""),
        "api": str(catalog.get("api", "") or ""),
        "shaders": sorted(
            (
                {
                    "resourceId": int(shader["shaderResourceId"]),
                    "stage": str(shader.get("stage", "")),
                    "entryPoint": str(shader.get("entryPoint", "")),
                    "rawSha256": str(shader.get("rawSha256", "") or "").lower(),
                }
                for shader in shaders
            ),
            key=lambda item: (
                item["resourceId"],
                item["stage"],
                item["entryPoint"],
                item["rawSha256"],
            ),
        ),
    }
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalise_catalog(catalog_path: str | Path) -> tuple[Path, dict]:
    path = Path(catalog_path).expanduser().resolve()
    catalog = _read_json(path)
    schema = str(catalog.get("schema", ""))
    if schema != CATALOG_SCHEMA:
        raise ValueError("Unsupported capture symbol schema: %s" % schema)
    if catalog.get("schemaVersion") != CATALOG_VERSION:
        raise ValueError(
            "Unsupported capture symbol schemaVersion: %s"
            % catalog.get("schemaVersion")
        )
    if not isinstance(catalog.get("shaders"), list):
        raise ValueError("Capture catalog shaders must be an array")
    if not isinstance(catalog.get("uses", []), list):
        raise ValueError("Capture catalog uses must be an array")

    shaders: list[dict] = []
    shader_keys: set[tuple[int, str, str]] = set()
    for source in catalog.get("shaders", []):
        if not isinstance(source, dict):
            raise ValueError("Every capture shader record must be an object")
        shader = copy.deepcopy(source)
        shader["shaderResourceId"] = _resource_id(
            shader.get("shaderResourceId", shader.get("resourceId", 0))
        )
        shader["stage"] = normalize_stage(shader.get("stage", ""))
        shader["entryPoint"] = str(
            shader.get("entryPoint", shader.get("entry", "")) or ""
        )
        if shader["shaderResourceId"] <= 0:
            raise ValueError("Capture shaderResourceId must be a positive integer")
        if not shader["stage"]:
            raise ValueError("Capture shader stage is required")
        key = (
            shader["shaderResourceId"],
            shader["stage"],
            shader["entryPoint"],
        )
        if key in shader_keys:
            raise ValueError(
                "Duplicate capture shader key: %d/%s/%s" % key
            )
        shader_keys.add(key)

        declared_raw_hash = _sha256(shader.get("rawSha256", ""), "rawSha256")
        declared_code_hash = _sha256(shader.get("codeSha256", ""), "codeSha256")
        raw_path = str(shader.get("rawPath", "") or "")
        if raw_path:
            resolved = Path(raw_path)
            if not resolved.is_absolute():
                resolved = (path.parent / resolved).resolve()
            else:
                resolved = resolved.resolve()
            if not _inside(path.parent, resolved):
                raise ValueError(
                    "Shader rawPath escapes the catalog directory: %s" % raw_path
                )
            if not resolved.is_file():
                raise ValueError("Shader rawPath is not a file: %s" % resolved)
            raw_size = resolved.stat().st_size
            if raw_size > MAX_SHADER_BLOB_BYTES:
                raise ValueError(
                    "Shader blob exceeds %d bytes: %s"
                    % (MAX_SHADER_BLOB_BYTES, resolved)
                )
            try:
                declared_size = int(shader.get("rawSize"))
            except (TypeError, ValueError):
                raise ValueError("Shader rawSize is required when rawPath is present")
            if declared_size != raw_size:
                raise ValueError(
                    "Shader rawSize mismatch for %s: declared %d, actual %d"
                    % (resolved, declared_size, raw_size)
                )
            shader["rawPath"] = str(resolved)
            raw = resolved.read_bytes()
            physical_raw_hash = hashlib.sha256(raw).hexdigest()
            if declared_raw_hash and declared_raw_hash != physical_raw_hash:
                raise ValueError("Shader rawSha256 does not match rawPath: %s" % resolved)
            shader["rawSha256"] = physical_raw_hash
            shader["_rawHashVerified"] = True
            container = fingerprint_bytes(raw, str(resolved))
            if container is not None:
                if declared_code_hash and declared_code_hash != container.code_sha256:
                    raise ValueError(
                        "Shader codeSha256 does not match rawPath: %s" % resolved
                    )
                shader["codeSha256"] = container.code_sha256
                shader["_codeHashVerified"] = True
                shader.setdefault("encoding", container.kind)
            else:
                shader["codeSha256"] = declared_code_hash
                shader["_codeHashVerified"] = False
        else:
            try:
                declared_size = int(shader.get("rawSize", 0) or 0)
            except (TypeError, ValueError):
                raise ValueError("Shader rawSize must be an integer")
            if declared_size or declared_raw_hash:
                raise ValueError("Shader records with bytes must provide rawPath")
            shader["rawPath"] = ""
            shader["rawSha256"] = ""
            shader["codeSha256"] = declared_code_hash
            shader["_rawHashVerified"] = False
            shader["_codeHashVerified"] = False

        if "constantBuffers" not in shader and isinstance(
            shader.get("constantBlocks"), list
        ):
            shader["constantBuffers"] = copy.deepcopy(shader["constantBlocks"])
        computed_layout = layout_hash(shader)
        declared_layout = _sha256(shader.get("layoutHash", ""), "layoutHash")
        if declared_layout and computed_layout and declared_layout != computed_layout:
            raise ValueError("Capture layoutHash does not match reflected constant buffers")
        shader["layoutHash"] = computed_layout or declared_layout
        computed_interface = interface_hash(shader)
        declared_interface = _sha256(
            shader.get("interfaceHash", ""), "interfaceHash"
        )
        if declared_interface and declared_interface != computed_interface:
            raise ValueError("Capture interfaceHash does not match reflected interface")
        shader["interfaceHash"] = computed_interface
        shader["interfaceComplete"] = interface_is_complete(shader)
        shaders.append(shader)

    uses_by_resource_stage: dict[tuple[int, str], list[dict]] = {}
    for source_use in catalog.get("uses", []):
        if not isinstance(source_use, dict):
            raise ValueError("Every capture use record must be an object")
        use = copy.deepcopy(source_use)
        use["resourceId"] = _resource_id(
            use.get("shaderResourceId", use.get("resourceId", 0))
        )
        use["stage"] = normalize_stage(use.get("stage", ""))
        use["entryPoint"] = str(
            use.get("entryPoint", use.get("entry", "")) or ""
        )
        use["rawSha256"] = _sha256(use.get("rawSha256", ""), "uses.rawSha256")
        uses_by_resource_stage.setdefault(
            (use["resourceId"], use["stage"]), []
        ).append(use)
    for shader in shaders:
        shader["uses"] = [
            use
            for use in uses_by_resource_stage.get(
                (shader["shaderResourceId"], shader["stage"]), ()
            )
            if (
                not use.get("rawSha256")
                or not shader.get("rawSha256")
                or use.get("rawSha256") == shader.get("rawSha256")
            )
            and (
                not use.get("entryPoint")
                or not shader.get("entryPoint")
                or use.get("entryPoint") == shader.get("entryPoint")
            )
        ]

    capture_id = capture_identity(catalog, shaders)
    declared_capture_id = str(catalog.get("captureId", "") or "")
    if declared_capture_id and declared_capture_id != capture_id:
        raise ValueError(
            "captureId does not match the catalog's canonical capture identity"
        )
    catalog["schema"] = CATALOG_SCHEMA
    catalog["schemaVersion"] = CATALOG_VERSION
    catalog["captureId"] = capture_id
    catalog["shaders"] = shaders
    return path, catalog


def _stage_compatible(capture_stage: str, asset_stage: str) -> bool:
    return not capture_stage or not asset_stage or capture_stage == asset_stage


def _entry_compatible(capture_entry: str, asset_entry: str) -> bool:
    return not capture_entry or not asset_entry or capture_entry == asset_entry


def _candidate_evidence(
    capture: dict,
    asset: dict,
) -> dict:
    capture_layout = str(capture.get("layoutHash", ""))
    asset_layout = str(asset.get("layout_hash", ""))
    asset_metadata = asset.get("metadata", {})
    scanner_verified = asset.get("provenance") in (
        "embedded-container",
        "manifest-bound-container",
        "trusted-provider-artifact",
    )
    # A manifest's digest is a claim, even when the same digest happens to occur
    # in some other file in the scan. Only bytes parsed from this physical asset
    # record establish provenance. A trusted-provider row reaches this set only
    # after its process attestation, in-memory artifact, DXBC/DXIL fingerprint,
    # declared hashes, complete interface, and CB layout were all revalidated.
    # A global hash-membership join is not equivalent evidence.
    raw_hash_verified = scanner_verified
    code_hash_verified = scanner_verified
    capture_raw_hash_verified = bool(capture.get("_rawHashVerified", False))
    capture_code_hash_verified = bool(capture.get("_codeHashVerified", False))
    capture_interface = str(capture.get("interfaceHash", "") or interface_hash(capture))
    asset_interface = str(
        asset_metadata.get("_computedInterfaceHash", "") or interface_hash(asset_metadata)
    )
    capture_interface_complete = bool(capture.get("interfaceComplete", False))
    asset_interface_complete = bool(asset_metadata.get("_interfaceComplete", False))
    asset_symbol_metadata_trusted = bool(
        asset_metadata.get("_symbolMetadataTrusted", False)
    )
    raw_equal = bool(
        capture.get("rawSha256")
        and capture.get("rawSha256") == asset.get("raw_sha256")
    )
    code_equal = bool(
        capture.get("codeSha256")
        and capture.get("codeSha256") == asset.get("code_sha256")
    )
    layout_equal = bool(capture_layout and capture_layout == asset_layout)
    interface_equal = bool(
        capture_interface_complete
        and asset_interface_complete
        and capture_interface
        and capture_interface == asset_interface
    )
    stage_ok = _stage_compatible(capture.get("stage", ""), asset.get("stage", ""))
    entry_ok = _entry_compatible(
        str(capture.get("entryPoint", "") or ""),
        str(asset.get("entry_point", "") or ""),
    )
    return {
        "rawHashEqual": raw_equal,
        "executableHashEqual": code_equal,
        "assetRawHashVerified": raw_hash_verified,
        "assetExecutableHashVerified": code_hash_verified,
        "captureRawHashVerified": capture_raw_hash_verified,
        "captureExecutableHashVerified": capture_code_hash_verified,
        "layoutHashEqual": layout_equal,
        "interfaceHashEqual": interface_equal,
        "captureInterfaceComplete": capture_interface_complete,
        "assetInterfaceComplete": asset_interface_complete,
        "assetSymbolMetadataTrusted": asset_symbol_metadata_trusted,
        "captureHasLayout": bool(capture_layout),
        "assetHasLayout": bool(asset_layout),
        "stageCompatible": stage_ok,
        "entryPointCompatible": entry_ok,
        "captureRawSha256": capture.get("rawSha256", ""),
        "captureCodeSha256": capture.get("codeSha256", ""),
        "captureLayoutHash": capture_layout,
        "assetRawSha256": asset.get("raw_sha256", ""),
        "assetCodeSha256": asset.get("code_sha256", ""),
        "assetLayoutHash": asset_layout,
        "captureInterfaceHash": capture_interface,
        "assetInterfaceHash": asset_interface,
        "assetPath": asset.get("relative_path", ""),
        "assetProvenance": asset.get("provenance", ""),
    }


def _score(evidence: dict) -> float:
    if not evidence["stageCompatible"] or not evidence["entryPointCompatible"]:
        return 0.0
    if (
        evidence["rawHashEqual"]
        and evidence["captureRawHashVerified"]
        and evidence["assetRawHashVerified"]
    ):
        return 100.0
    if evidence["rawHashEqual"]:
        return 88.0
    if (
        evidence["executableHashEqual"]
        and evidence["captureExecutableHashVerified"]
        and evidence["assetExecutableHashVerified"]
        and evidence["assetSymbolMetadataTrusted"]
        and evidence["layoutHashEqual"]
        and evidence["interfaceHashEqual"]
    ):
        return 92.0
    if evidence["executableHashEqual"] and evidence["layoutHashEqual"]:
        return 84.0
    if evidence["executableHashEqual"]:
        return 78.0
    if evidence["layoutHashEqual"]:
        return 50.0
    return 0.0


def _has_symbol_metadata(asset: dict) -> bool:
    metadata = asset.get("metadata", {})
    return bool(
        isinstance(metadata, dict)
        and metadata.get("_symbolMetadataTrusted") is True
        and (
            _block_list(metadata)
            or metadata.get("resources")
            or metadata.get("samplers")
            or metadata.get("sourceName")
        )
    )


def validate_candidate(
    capture: dict,
    asset: dict,
) -> dict:
    """Re-run non-AI validation for one proposed association."""

    evidence = _candidate_evidence(capture, asset)
    if not evidence["stageCompatible"]:
        return {
            "accepted": False,
            "programVerified": False,
            "status": "RejectedStageMismatch",
            "reason": "The asset shader stage conflicts with the captured stage.",
            "evidence": evidence,
        }
    if not evidence["entryPointCompatible"]:
        return {
            "accepted": False,
            "programVerified": False,
            "status": "RejectedEntryPointMismatch",
            "reason": "The asset entry point conflicts with the captured entry point.",
            "evidence": evidence,
        }
    if (
        evidence["rawHashEqual"]
        and evidence["captureRawHashVerified"]
        and evidence["assetRawHashVerified"]
    ):
        return {
            "accepted": True,
            "programVerified": True,
            "status": "ExactBlob",
            "reason": "The complete compiled shader containers are byte-identical.",
            "evidence": evidence,
        }
    if evidence["rawHashEqual"]:
        return {
            "accepted": True,
            "programVerified": False,
            "status": "DeclaredExactBlobUnverified",
            "reason": "The declared blob hashes match, but physical capture and asset bytes did not both prove them.",
            "evidence": evidence,
        }
    if (
        evidence["executableHashEqual"]
        and evidence["captureExecutableHashVerified"]
        and evidence["assetExecutableHashVerified"]
        and evidence["assetSymbolMetadataTrusted"]
        and evidence["layoutHashEqual"]
        and evidence["interfaceHashEqual"]
    ):
        return {
            "accepted": True,
            "programVerified": True,
            "status": "ExecutableAndLayout",
            "reason": "Executable instructions, complete shader interface, and CB layout match.",
            "evidence": evidence,
        }
    if (
        evidence["executableHashEqual"]
        and evidence["layoutHashEqual"]
        and evidence["interfaceHashEqual"]
    ):
        return {
            "accepted": True,
            "programVerified": False,
            "status": "DeclaredExecutableAndLayoutUnverified",
            "reason": "The declared executable/interface/layout matches, but this asset record is not physically bound to those bytes.",
            "evidence": evidence,
        }
    if evidence["executableHashEqual"] and evidence["layoutHashEqual"]:
        return {
            "accepted": True,
            "programVerified": False,
            "status": "ExecutableAndLayoutIncompleteInterface",
            "reason": "Code and CB layout match, but a complete IO/resource interface was not proven.",
            "evidence": evidence,
        }
    if evidence["executableHashEqual"]:
        return {
            "accepted": True,
            "programVerified": False,
            "status": "ExecutableOnly",
            "reason": "Executable instructions match, but no equal ABI layout was proven.",
            "evidence": evidence,
        }
    if evidence["layoutHashEqual"]:
        return {
            "accepted": True,
            "programVerified": False,
            "status": "LayoutOnly",
            "reason": "The CB layout matches, but shader code identity was not proven.",
            "evidence": evidence,
        }
    return {
        "accepted": False,
        "programVerified": False,
        "status": "RejectedNoDeterministicEvidence",
        "reason": "No deterministic code or layout evidence connects this candidate.",
        "evidence": evidence,
    }


def _block_list(shader: dict) -> list[dict]:
    blocks = shader.get(
        "constantBuffers", shader.get("constantBlocks", shader.get("constant_blocks", []))
    ) or []
    return [block for block in blocks if isinstance(block, dict)]


def _block_identity(block: dict) -> tuple[int, int]:
    return (
        _safe_int(block.get("space", block.get("registerSpace", 0))),
        _safe_int(block.get("bind", block.get("bindNumber", -1)), -1),
    )


def _block_physical_signature(block: dict) -> tuple[Any, ...]:
    return (
        _safe_int(block.get("space", block.get("registerSpace", 0))),
        _safe_int(block.get("bind", block.get("bindNumber", -1)), -1),
        _safe_int(block.get("arraySize", block.get("bindArraySize", 1)), 1),
        _safe_int(block.get("byteSize", block.get("size", 0))),
        bool(block.get("bufferBacked", True)),
        bool(block.get("inlineDataBytes", False)),
        bool(block.get("compileConstants", False)),
    )


def _field_list(block: dict) -> list[dict]:
    return block.get("fields", block.get("variables", [])) or []


def _flatten_fields(
    fields: Iterable[dict], prefix: str = "", parent_offset: int = 0
) -> list[dict]:
    flattened: list[dict] = []
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            continue
        name = str(
            field.get("name", field.get("alias", field.get("path", "field%d" % index)))
        )
        path = name if not prefix else prefix + "." + name
        local_offset = _safe_int(field.get("byteOffset", field.get("offset", 0)))
        absolute = parent_offset + local_offset
        item = dict(field)
        item["name"] = name
        item["canonicalPath"] = path
        item["absoluteByteOffset"] = absolute
        flattened.append(item)
        members = field.get("members", field.get("fields", [])) or []
        flattened.extend(_flatten_fields(members, path, absolute))
    return flattened


def _normalise_field_base_type(field: dict) -> str:
    if field.get("members") or field.get("fields"):
        return "struct"
    value = str(field.get("baseType", field.get("type", "")) or "")
    value = value.strip().lower().rsplit(".", 1)[-1].replace("_", "").replace(" ", "")
    value = re.sub(r"\d+(x\d+)?$", "", value)
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
    }.get(value, value)


def _field_physical_signature(field: dict) -> tuple[Any, ...]:
    """Name-independent ABI identity for one reflected constant field."""

    return (
        _normalise_field_base_type(field),
        _safe_int(field.get("rows", 1), 1),
        _safe_int(field.get("columns", field.get("cols", 1)), 1),
        _safe_int(field.get("elements", 1), 1),
        _safe_int(field.get("arrayStride", field.get("arrayByteStride", 0))),
        _safe_int(field.get("matrixStride", field.get("matrixByteStride", 0))),
        bool(field.get("rowMajor", False)),
        _safe_int(field.get("byteSize", field.get("size", 0))),
        _safe_int(field.get("bitFieldOffset", 0)),
        _safe_int(field.get("bitFieldSize", 0)),
    )


def _field_is_physically_described(field: dict) -> bool:
    signature = _field_physical_signature(field)
    return bool(signature[0]) and signature[1] > 0 and signature[2] > 0


def aliases_for_match(
    capture_id: str,
    capture: dict,
    asset: dict,
    provenance: str,
    confidence: float,
    verified: bool,
    validation: dict,
) -> list[dict]:
    """Build display-only aliases; physical reflection data is never mutated."""

    shader_id = int(capture["shaderResourceId"])
    stage = capture.get("stage", "")
    entry_point = str(capture.get("entryPoint", "") or "")
    raw_sha256 = str(capture.get("rawSha256", "") or "")
    code_sha256 = str(capture.get("codeSha256", "") or "")
    verified = bool(verified and validation.get("programVerified"))
    asset_meta = asset.get("metadata", {})
    symbol_metadata_trusted = bool(asset_meta.get("_symbolMetadataTrusted", False))
    if symbol_metadata_trusted:
        display = str(
            asset.get("display_name", "") or Path(asset["relative_path"]).stem
        )
    elif asset.get("provenance") == "embedded-container":
        display = Path(str(asset.get("relative_path", "") or "")).stem
    elif asset.get("provenance") == "manifest-bound-container":
        physical_relative = str(
            asset_meta.get("_boundContainer", {}).get("relativePath", "") or ""
        )
        display = Path(physical_relative).stem if physical_relative else ""
    else:
        display = ""
    if not display:
        return []
    common_metadata = {
        "assetShaderId": int(asset["id"]),
        "assetPath": asset.get("relative_path", ""),
        "matchStatus": validation.get("status", ""),
        "programVerified": bool(validation.get("programVerified")),
        "symbolMetadataTrusted": symbol_metadata_trusted,
        "captureId": capture_id,
        "captureRawSha256": raw_sha256,
        "captureCodeSha256": code_sha256,
        "stage": stage,
        "entryPoint": entry_point,
        "evidence": validation.get("evidence", {}),
    }
    aliases: list[dict] = [
        {
            "captureId": capture_id,
            "shaderResourceId": shader_id,
            "stage": stage,
            "entryPoint": entry_point,
            "rawSha256": raw_sha256,
            "codeSha256": code_sha256,
            "bindingKind": "shader",
            "alias": display,
            "originalName": str(capture.get("name", "")),
            "provenance": provenance,
            "confidence": confidence,
            "verified": verified,
            "metadata": common_metadata,
        }
    ]

    if not symbol_metadata_trusted:
        return aliases
    asset_blocks = _block_list(asset_meta)
    capture_blocks = _block_list(capture)
    layout_evidence = validation.get("evidence", {})
    if not (
        layout_evidence.get("layoutHashEqual")
        and layout_evidence.get("captureHasLayout")
        and layout_evidence.get("assetHasLayout")
    ):
        return aliases
    capture_by_binding = {_block_identity(block): block for block in capture_blocks}

    for asset_block in asset_blocks:
        space, bind = _block_identity(asset_block)
        capture_block = capture_by_binding.get((space, bind))
        if capture_block is None:
            continue
        asset_signature = _block_physical_signature(asset_block)
        capture_signature = _block_physical_signature(capture_block)
        asset_size = int(asset_signature[3])
        capture_size = int(capture_signature[3])
        if (
            asset_size <= 0
            or capture_size <= 0
            or asset_signature != capture_signature
        ):
            continue
        asset_fields = _flatten_fields(_field_list(asset_block))
        capture_fields_flat = _flatten_fields(_field_list(capture_block))
        if not asset_fields or not capture_fields_flat:
            continue
        capture_field_keys = {
            (int(field["absoluteByteOffset"]), _field_physical_signature(field))
            for field in capture_fields_flat
            if _field_is_physically_described(field)
        }
        asset_field_keys = {
            (int(field["absoluteByteOffset"]), _field_physical_signature(field))
            for field in asset_fields
            if _field_is_physically_described(field)
        }
        # A CB display name is safe only after the complete reflected field ABI
        # for this binding has been proved. A manifest cannot smuggle a name by
        # reusing a correct blob hash with fabricated offsets or types.
        if not asset_field_keys or asset_field_keys != capture_field_keys:
            continue
        block_name = str(asset_block.get("name", "") or "cb%d" % bind)
        block_common = dict(common_metadata)
        block_common["byteSize"] = asset_size or capture_size
        aliases.append(
            {
                "captureId": capture_id,
                "shaderResourceId": shader_id,
                "stage": stage,
                "entryPoint": entry_point,
                "rawSha256": raw_sha256,
                "codeSha256": code_sha256,
                "bindingKind": "constantBuffer",
                "registerSpace": space,
                "bindNumber": bind,
                "alias": block_name,
                "originalName": str(capture_block.get("name", "")),
                "provenance": provenance,
                "confidence": confidence,
                "verified": verified,
                "metadata": block_common,
            }
        )

        capture_fields: dict[tuple[int, tuple[Any, ...]], list[dict]] = {}
        for capture_field in capture_fields_flat:
            if not _field_is_physically_described(capture_field):
                continue
            field_key = (
                int(capture_field["absoluteByteOffset"]),
                _field_physical_signature(capture_field),
            )
            capture_fields.setdefault(field_key, []).append(capture_field)
        for field in asset_fields:
            alias = str(field.get("name", "") or "")
            if not alias or not _field_is_physically_described(field):
                continue
            offset = int(field["absoluteByteOffset"])
            signature = _field_physical_signature(field)
            physical_matches = capture_fields.get((offset, signature), [])
            if not physical_matches:
                continue
            original = physical_matches[0]
            aliases.append(
                {
                    "captureId": capture_id,
                    "shaderResourceId": shader_id,
                    "stage": stage,
                    "entryPoint": entry_point,
                    "rawSha256": raw_sha256,
                    "codeSha256": code_sha256,
                    "bindingKind": "constantField",
                    "registerSpace": space,
                    "bindNumber": bind,
                    "byteOffset": offset,
                    "canonicalPath": str(field.get("canonicalPath", alias)),
                    "alias": alias,
                    "originalName": str(original.get("name", "")),
                    "provenance": provenance,
                    "confidence": confidence,
                    "verified": verified,
                    "metadata": {
                        **common_metadata,
                        "type": field.get("type", field.get("baseType", "")),
                        "byteSize": _safe_int(
                            field.get("byteSize", field.get("size", 0))
                        ),
                        "physicalSignature": list(signature),
                    },
                }
            )
    return aliases


def match_capture_catalog(
    catalog_path: str | Path,
    scan_id: str,
    database_path: str | Path,
) -> dict:
    path, catalog = _normalise_catalog(catalog_path)
    store = SymbolStore(database_path)
    if store.get_asset_scan(scan_id) is None:
        raise ValueError("Unknown asset scan: %s" % scan_id)
    # save_capture_catalog resets this identity's aliases to the rows inherited by
    # Shader bytes; each Shader the matcher can prove again is replaced below.
    capture_id = store.save_capture_catalog(str(path), catalog)
    resolutions: list[dict] = []
    status_counts: Counter[str] = Counter()
    alias_count = 0
    for shader in catalog["shaders"]:
        candidates = store.find_asset_candidates(
            scan_id,
            shader.get("rawSha256", ""),
            shader.get("codeSha256", ""),
            shader.get("stage", ""),
            shader.get("layoutHash", ""),
            entry_point=shader.get("entryPoint", ""),
        )
        ranked: list[tuple[dict, dict, float]] = []
        for asset in candidates:
            validation = validate_candidate(shader, asset)
            score = _score(validation["evidence"])
            if score > 0:
                ranked.append((asset, validation, score))
        ranked.sort(key=lambda value: (-value[2], value[0]["relative_path"]))

        raw = [item for item in ranked if item[1]["status"] == "ExactBlob"]
        abi = [
            item
            for item in ranked
            if item[1]["status"] == "ExecutableAndLayout"
        ]
        selected: tuple[dict, dict, float] | None = None
        selected_verified = False
        selected_source = "program"
        rich_raw = [item for item in raw if _has_symbol_metadata(item[0])]
        rich_abi = [item for item in abi if _has_symbol_metadata(item[0])]
        physical_raw = [
            item for item in raw if item[0].get("provenance") == "embedded-container"
        ]
        if len(raw) == 1:
            selected = raw[0]
            selected_verified = bool(raw[0][1].get("programVerified"))
            resolution_status = "ExactBlobUnique"
        elif len(rich_raw) == 1:
            # Reserved for a future builtin/signed provider whose metadata is
            # explicitly trusted by the scanner. Ordinary and blob-bound JSON
            # manifests never reach this branch.
            selected = rich_raw[0]
            selected_verified = bool(rich_raw[0][1].get("programVerified"))
            resolution_status = "ExactBlobUniqueMetadata"
        elif len(physical_raw) == 1:
            # A bound manifest and the raw scanner may describe the same file
            # and offset. Select the sole parser-owned physical record so the
            # untrusted manifest cannot contribute display names or ABI fields.
            selected = physical_raw[0]
            selected_verified = True
            resolution_status = "ExactBlobUnique"
        elif len(raw) > 1:
            resolution_status = "ExactBlobAmbiguous"
        elif len(abi) == 1:
            selected = abi[0]
            selected_verified = bool(abi[0][1].get("programVerified"))
            resolution_status = "ExecutableAndLayoutUnique"
        elif len(rich_abi) == 1:
            selected = rich_abi[0]
            selected_verified = bool(rich_abi[0][1].get("programVerified"))
            resolution_status = "ExecutableAndLayoutUniqueMetadata"
        elif len(abi) > 1:
            resolution_status = "ExecutableAndLayoutAmbiguous"
        elif ranked:
            resolution_status = "NeedsAI"
        else:
            resolution_status = "Unmatched"

        # A previously reviewed rule may disambiguate identical variants in a
        # later frame.  The rule is accepted only when its asset is still among
        # the freshly generated candidates and its captured code identity key
        # matches (enforced by find_symbol_rule).
        if selected is None:
            rule = store.find_symbol_rule(scan_id, shader)
            if rule is not None and rule.get("program_verified"):
                for item in ranked:
                    if int(item[0]["id"]) == int(rule["asset_shader_id"]):
                        current_verified = bool(item[1].get("programVerified"))
                        if not current_verified:
                            continue
                        selected = item
                        selected_verified = True
                        selected_source = "rule:%s" % rule.get("source", "review")
                        resolution_status = "ReusedSymbolRule"
                        break

        match_rows: list[dict] = []
        for asset, validation, score in ranked:
            is_selected = selected is not None and asset["id"] == selected[0]["id"]
            match_rows.append(
                {
                    "assetShaderId": asset["id"],
                    "status": (
                        "ReusedSymbolRule"
                        if is_selected and selected_source.startswith("rule:")
                        else validation["status"]
                    ),
                    "score": score,
                    "selected": is_selected,
                    "source": selected_source if is_selected else "program",
                    "confidence": score / 100.0,
                    "rationale": validation["reason"],
                    "evidence": validation["evidence"],
                }
            )
        store.replace_matches(
            capture_id,
            shader["shaderResourceId"],
            shader["stage"],
            shader.get("entryPoint", ""),
            match_rows,
        )

        if selected is not None:
            asset, validation, score = selected
            store.clear_aliases(
                capture_id, shader["shaderResourceId"], shader["stage"],
                shader.get("entryPoint", ""))
            for alias in aliases_for_match(
                capture_id,
                shader,
                asset,
                selected_source,
                score / 100.0,
                selected_verified,
                validation,
            ):
                store.save_alias(alias)
                alias_count += 1

        status_counts[resolution_status] += 1
        resolutions.append(
            {
                "shaderResourceId": shader["shaderResourceId"],
                "stage": shader["stage"],
                "entryPoint": shader.get("entryPoint", ""),
                "rawSha256": shader.get("rawSha256", ""),
                "status": resolution_status,
                "selectedAssetShaderId": selected[0]["id"] if selected else None,
                "candidateCount": len(ranked),
                "candidates": [
                    {
                        "assetShaderId": asset["id"],
                        "assetPath": asset["relative_path"],
                        "displayName": asset["display_name"],
                        "score": score,
                        "validation": validation,
                    }
                    for asset, validation, score in ranked[:20]
                ],
            }
        )

    return {
        "schema": "renderdoc-ultra-match-report/v1",
        "captureId": capture_id,
        "scanId": scan_id,
        "databasePath": str(Path(database_path).expanduser().resolve()),
        "captureCatalog": str(path),
        "shaderCount": len(catalog["shaders"]),
        "aliasCount": alias_count,
        "statusCounts": dict(status_counts),
        "resolutions": resolutions,
    }
