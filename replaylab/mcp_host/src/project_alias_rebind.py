"""Fail-closed materialization of project aliases for one target capture.

Project alias knowledge contains display names only.  This module never copies
capture-local identities from the origin alias table: it rebinds every alias
to an exact, project-owned target catalog revision and emits a fresh verified
``renderdoc-ultra-aliases/v1`` document for qrenderdoc's live loader.
"""

from __future__ import annotations

import copy
import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

from . import projects as project_model

MATERIALIZATION_SCHEMA = "renderdoc-ultra-project-alias-materialization/v1"
MATERIALIZATION_VERSION = 1
BATCH_MATERIALIZATION_SCHEMA = (
    "renderdoc-ultra-project-alias-materialization-batch/v1"
)
BATCH_MATERIALIZATION_VERSION = 1
MAX_MATERIALIZED_ALIAS_BYTES = 32 * 1024 * 1024
MAX_BATCH_TARGETS = 256
MAX_BATCH_CANDIDATES = 4096
MAX_BATCH_ALIASES = 65536
MAX_BATCH_ALIAS_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_BATCH_CATALOG_BYTES = 256 * 1024 * 1024
MAX_BATCH_CATALOGS = 257
_ALLOWED_KINDS = {"shader", "constantBuffer", "constantField"}
_PROVISIONAL_FIELDS = (
    "status",
    "authority",
    "reviewStatus",
    "verificationStatus",
)


def _nonnegative(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise project_model.ReplayProjectError(
            "%s must be a non-negative integer" % field
        )
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise project_model.ReplayProjectError(
            "%s must be a non-negative integer" % field
        ) from exc
    if result < 0:
        raise project_model.ReplayProjectError(
            "%s must be a non-negative integer" % field
        )
    return result


def _integer(value: Any, field: str, default: int = 0) -> int:
    """Return one exact integer used by canonical physical-ABI hashing."""

    if value is None:
        return default
    if isinstance(value, bool):
        raise project_model.ReplayProjectError("%s must be an integer" % field)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise project_model.ReplayProjectError(
            "%s must be an integer" % field
        ) from exc
    if isinstance(value, float) and not value.is_integer():
        raise project_model.ReplayProjectError("%s must be an integer" % field)
    return result


def _binding_kind(value: Any) -> str:
    token = str(value or "").strip().replace("_", "").replace("-", "").lower()
    aliases = {
        "shader": "shader",
        "shaderalias": "shader",
        "shadername": "shader",
        "constantbuffer": "constantBuffer",
        "cbuffer": "constantBuffer",
        # Older project exports used this spelling for a reflected field.
        "constantbuffervariable": "constantField",
        "constantfield": "constantField",
        "cbufferfield": "constantField",
    }
    result = aliases.get(token, "")
    if result not in _ALLOWED_KINDS:
        raise project_model.ReplayProjectError(
            "project alias bindingKind is unsupported"
        )
    return result


def _is_ai_provisional(value: Mapping[str, Any]) -> bool:
    for field in _PROVISIONAL_FIELDS:
        if str(value.get(field, "") or "").strip().casefold() == "aiprovisional":
            return True
    metadata = value.get("metadata")
    return isinstance(metadata, Mapping) and any(
        str(metadata.get(field, "") or "").strip().casefold() == "aiprovisional"
        for field in _PROVISIONAL_FIELDS
    )


def _compact_alias(alias: Mapping[str, Any]) -> dict[str, Any]:
    compact = {
        "bindingKind": project_model._text(
            str(alias.get("bindingKind", "")), "alias.bindingKind", 64
        ),
        "registerSpace": _nonnegative(
            alias.get("registerSpace", 0), "alias.registerSpace"
        ),
        "bindNumber": _nonnegative(alias.get("bindNumber", 0), "alias.bindNumber"),
        "byteOffset": _nonnegative(alias.get("byteOffset", 0), "alias.byteOffset"),
        "canonicalPath": project_model._text(
            str(alias.get("canonicalPath", "") or ""),
            "alias.canonicalPath",
            2048,
            True,
        ),
        "alias": project_model._text(
            str(alias.get("alias", "")), "alias.alias", 1024
        ),
    }
    kind = _binding_kind(compact["bindingKind"])
    if kind == "shader":
        if any(
            compact[field]
            for field in ("registerSpace", "bindNumber", "byteOffset")
        ) or compact["canonicalPath"]:
            raise project_model.ReplayProjectError(
                "shader display alias contains CBuffer coordinates"
            )
    elif kind == "constantBuffer":
        if compact["byteOffset"] or compact["canonicalPath"]:
            raise project_model.ReplayProjectError(
                "CBuffer display alias contains field coordinates"
            )
    elif not compact["canonicalPath"]:
        raise project_model.ReplayProjectError(
            "CBuffer field alias requires canonicalPath"
        )
    return compact


def _catalog_for_capture(
    service: Any,
    capture: Mapping[str, Any],
    *,
    catalog_cache: dict[tuple[str, str, str], dict[str, Any]] | None = None,
    budget: dict[str, int] | None = None,
) -> dict[str, Any]:
    catalog_path = service._artifact(
        capture.get("catalogPath"), "capture alias materialization catalog"
    )
    cache_key = (
        str(capture.get("captureId", "") or ""),
        str(capture.get("catalogSha256", "") or ""),
        str(catalog_path),
    )
    if catalog_cache is not None and cache_key in catalog_cache:
        return catalog_cache[cache_key]
    if budget is not None:
        try:
            catalog_size = catalog_path.stat().st_size
        except OSError as exc:
            raise project_model.ReplayProjectError(
                "capture alias materialization catalog is unavailable"
            ) from exc
        if len(catalog_cache or {}) >= MAX_BATCH_CATALOGS:
            raise project_model.ReplayProjectError(
                "alias materialization exceeds the catalog count limit"
            )
        if budget["catalogBytes"] + catalog_size > MAX_BATCH_CATALOG_BYTES:
            raise project_model.ReplayProjectError(
                "alias materialization exceeds the catalog byte limit"
            )
        budget["catalogBytes"] += catalog_size
    _unused_source, catalog, payload, _unused_blobs = (
        project_model._normalised_catalog_evidence(catalog_path)
    )
    if project_model._sha256_bytes(payload) != project_model._sha(
        capture.get("catalogSha256"), "capture.catalogSha256"
    ):
        raise project_model.ReplayProjectError(
            "capture alias materialization catalog hash mismatch"
        )
    if project_model._sha(catalog.get("captureId"), "catalog.captureId") != project_model._sha(
        capture.get("captureId"), "capture.captureId"
    ):
        raise project_model.ReplayProjectError(
            "capture alias materialization catalog identity mismatch"
        )
    compact = [project_model._compact_shader(shader) for shader in catalog["shaders"]]
    if compact != capture["shaders"]:
        raise project_model.ReplayProjectError(
            "capture alias materialization catalog Shader evidence mismatch"
        )
    if catalog_cache is not None:
        catalog_cache[cache_key] = catalog
    return catalog


def _full_shader_index(
    catalog: Mapping[str, Any],
) -> dict[tuple[int, str, str, str], dict[str, Any]]:
    shaders = catalog.get("shaders", [])
    if not isinstance(shaders, list):
        raise project_model.ReplayProjectError(
            "alias materialization catalog Shaders are invalid"
        )
    result: dict[tuple[int, str, str, str], dict[str, Any]] = {}
    for shader in shaders:
        if not isinstance(shader, Mapping):
            raise project_model.ReplayProjectError(
                "alias materialization catalog Shader is invalid"
            )
        compact = project_model._compact_shader(shader)
        key = project_model._shader_identity(compact)
        if key in result:
            raise project_model.ReplayProjectError(
                "alias materialization catalog has duplicate Shader identity"
            )
        result[key] = dict(shader)
    return result


def _full_shader(
    catalog: Mapping[str, Any],
    compact: Mapping[str, Any],
    shader_index: Mapping[tuple[int, str, str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    index = shader_index if shader_index is not None else _full_shader_index(catalog)
    key = project_model._shader_identity(compact)
    shader = index.get(key)
    if shader is None:
        raise project_model.ReplayProjectError(
            "target alias materialization does not select one catalog Shader"
        )
    if project_model._compact_shader(shader) != compact:
        raise project_model.ReplayProjectError(
            "target alias materialization Shader identity is inconsistent"
        )
    return shader


def _artifact_alias_groups(
    service: Any,
    manifest: Mapping[str, Any],
    alias_set: Mapping[str, Any],
    *,
    artifact_bytes: Mapping[str, bytes] | None = None,
    group_cache: dict[tuple[str, str, str, str, str], tuple[
        list[dict[str, Any]],
        dict[tuple[int, str, str, str], dict[str, Any]],
    ]] | None = None,
    catalog_cache: dict[tuple[str, str, str], dict[str, Any]] | None = None,
    shader_index_cache: dict[
        tuple[str, str, str],
        dict[tuple[int, str, str, str], dict[str, Any]],
    ] | None = None,
    budget: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], dict[tuple[int, str, str, str], dict[str, Any]]]:
    artifact = service._artifact(alias_set.get("artifactPath"), "alias artifact")
    knowledge_id = str(alias_set.get("knowledgeId", "") or "")
    artifact_sha256 = project_model._sha(
        alias_set.get("artifactSha256"), "alias.artifactSha256"
    )
    source_capture_id = project_model._sha(
        alias_set.get("captureId"), "alias.captureId"
    )
    source_catalog_sha256 = project_model._sha(
        alias_set.get("originCatalogSha256"), "alias.originCatalogSha256"
    )
    cache_key = (
        knowledge_id,
        str(artifact),
        artifact_sha256,
        source_capture_id,
        source_catalog_sha256,
    )
    if group_cache is not None and cache_key in group_cache:
        return group_cache[cache_key]
    raw = artifact_bytes.get(knowledge_id) if artifact_bytes is not None else None
    if raw is None:
        raw = project_model._file_bytes(
            artifact, project_model.MAX_ALIAS_BYTES, "alias artifact"
        )
        if budget is not None:
            if budget["aliasArtifactBytes"] + len(raw) > MAX_BATCH_ALIAS_ARTIFACT_BYTES:
                raise project_model.ReplayProjectError(
                    "alias materialization exceeds the artifact byte limit"
                )
            budget["aliasArtifactBytes"] += len(raw)
    if project_model._sha256_bytes(raw) != artifact_sha256:
        raise project_model.ReplayProjectError("project alias artifact hash mismatch")
    try:
        table = json.loads(raw.decode("utf-8-sig", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise project_model.ReplayProjectError(
            "project alias artifact is not UTF-8 JSON"
        ) from exc
    if (
        not isinstance(table, Mapping)
        or table.get("schema") != project_model.ALIAS_SCHEMA
        or table.get("schemaVersion") != project_model.ALIAS_VERSION
        or table.get("verifiedOnly") is not True
    ):
        raise project_model.ReplayProjectError(
            "project alias artifact is not verified-only"
        )
    if table.get("captureId") != source_capture_id:
        raise project_model.ReplayProjectError(
            "project alias artifact capture identity mismatch"
        )
    source_capture = service._capture(
        manifest,
        source_capture_id,
        source_catalog_sha256,
    )
    source_catalog = _catalog_for_capture(
        service,
        source_capture,
        catalog_cache=catalog_cache,
        budget=budget,
    )
    source_catalog_key = (
        str(source_capture.get("captureId", "") or ""),
        str(source_capture.get("catalogSha256", "") or ""),
        str(
            service._artifact(
                source_capture.get("catalogPath"),
                "capture alias materialization catalog",
            )
        ),
    )
    source_full_by_identity = (
        shader_index_cache.get(source_catalog_key)
        if shader_index_cache is not None
        else None
    )
    if source_full_by_identity is None:
        source_full_by_identity = _full_shader_index(source_catalog)
        if shader_index_cache is not None:
            shader_index_cache[source_catalog_key] = source_full_by_identity
    for compact in source_capture["shaders"]:
        _full_shader(source_catalog, compact, source_full_by_identity)

    raw_aliases = table.get("aliases")
    if not isinstance(raw_aliases, list) or len(raw_aliases) > project_model.MAX_ALIASES:
        raise project_model.ReplayProjectError(
            "project alias artifact aliases are invalid or oversized"
        )
    raw_aliases, alias_recovery = project_model._recover_legacy_alias_rows(
        raw_aliases, source_capture
    )
    groups: OrderedDict[tuple[int, str, str, str], dict[str, Any]] = OrderedDict()
    for index, alias in enumerate(raw_aliases):
        if (
            not isinstance(alias, Mapping)
            or alias.get("verified") is not True
            or alias.get("captureId") != source_capture_id
        ):
            raise project_model.ReplayProjectError(
                "project alias artifact aliases[%d] is not verified/capture-bound"
                % index
            )
        if _is_ai_provisional(alias):
            raise project_model.ReplayProjectError(
                "AIProvisional alias cannot be materialized from a project"
            )
        resource_id = project_model._positive(
            alias.get("shaderResourceId"), "alias.shaderResourceId"
        )
        stage = project_model._text(
            str(alias.get("stage", "")), "alias.stage", 64
        )
        entry_point = project_model._text(
            str(alias.get("entryPoint", "") or ""),
            "alias.entryPoint",
            512,
            True,
        )
        raw_sha256 = project_model._sha(
            alias.get("rawSha256"), "alias.rawSha256"
        )
        key = (resource_id, stage, entry_point, raw_sha256)
        compact_shader = project_model.ReplayProjectService._capture_shader(
            source_capture,
            resource_id=resource_id,
            stage=stage,
            entry_point=entry_point,
            raw_sha256=raw_sha256,
        )
        code_sha256 = project_model._sha(
            alias.get("codeSha256"), "alias.codeSha256", True
        )
        if code_sha256 and code_sha256 != compact_shader.get("codeSha256", ""):
            raise project_model.ReplayProjectError(
                "project alias artifact codeSha256 differs from its source catalog"
            )
        group = groups.setdefault(
            key,
            {
                "sourceShaderResourceId": resource_id,
                "stage": stage,
                "entryPoint": entry_point,
                "rawSha256": raw_sha256,
                "codeSha256": compact_shader.get("codeSha256", ""),
                "layoutHash": compact_shader.get("layoutHash", ""),
                "interfaceHash": compact_shader.get("interfaceHash", ""),
                "interfaceComplete": compact_shader.get("interfaceComplete") is True,
                "aliases": [],
                "sourceAliases": [],
                "aliasRecovery": alias_recovery,
                "sourceShader": {
                    **source_full_by_identity[key],
                    "_projectApi": source_capture["api"],
                },
            },
        )
        group["aliases"].append(_compact_alias(alias))
        group["sourceAliases"].append(copy.deepcopy(dict(alias)))
    result = (list(groups.values()), source_full_by_identity)
    if group_cache is not None:
        group_cache[cache_key] = result
    return result


def _compare_group_record(
    record: Mapping[str, Any], reconstructed: Mapping[str, Any]
) -> None:
    expected = {
        field: reconstructed[field]
        for field in (
            "stage",
            "entryPoint",
            "rawSha256",
            "codeSha256",
            "layoutHash",
            "interfaceHash",
            "interfaceComplete",
            "aliases",
        )
    }
    actual = {field: record.get(field) for field in expected}
    if actual != expected:
        raise project_model.ReplayProjectError(
            "project alias knowledge differs from its hash-bound artifact"
        )
    origin_resource = record.get("sourceShaderResourceId")
    if origin_resource not in (None, "") and project_model._positive(
        origin_resource, "alias.sourceShaderResourceId"
    ) != reconstructed["sourceShaderResourceId"]:
        raise project_model.ReplayProjectError(
            "project alias source Shader identity is inconsistent"
        )


def _physical_enum(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .lower()
        .rsplit(".", 1)[-1]
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


def _physical_sorted(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (dict(value) for value in values),
        key=lambda item: json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
    )


def _source_physical_constant_field(
    field: Mapping[str, Any], label: str
) -> dict[str, Any]:
    members = field.get("members", field.get("fields", [])) or []
    if not isinstance(members, list) or any(
        not isinstance(member, Mapping) for member in members
    ):
        raise project_model.ReplayProjectError(
            "%s CBuffer field members are invalid" % label
        )
    return {
        "byteOffset": _integer(
            field.get("byteOffset", field.get("offset", 0)),
            "%s field byteOffset" % label,
        ),
        "byteSize": _integer(
            field.get("byteSize", field.get("size", 0)),
            "%s field byteSize" % label,
        ),
        "baseType": _physical_base_type(
            field.get("baseType", field.get("type", ""))
        ),
        "rows": _integer(field.get("rows", 0), "%s field rows" % label),
        "columns": _integer(
            field.get("columns", field.get("cols", 0)),
            "%s field columns" % label,
        ),
        "elements": _integer(
            field.get("elements", 0), "%s field elements" % label
        ),
        "arrayByteStride": _integer(
            field.get("arrayByteStride", field.get("arrayStride", 0)),
            "%s field arrayByteStride" % label,
        ),
        "matrixByteStride": _integer(
            field.get("matrixByteStride", field.get("matrixStride", 0)),
            "%s field matrixByteStride" % label,
        ),
        "rowMajor": bool(field.get("rowMajor", False)),
        "flags": _integer(field.get("flags", 0), "%s field flags" % label),
        "pointerTypeID": _integer(
            field.get("pointerTypeID", 0xFFFFFFFF),
            "%s field pointerTypeID" % label,
        ),
        "bitFieldOffset": _integer(
            field.get("bitFieldOffset", 0), "%s field bitFieldOffset" % label
        ),
        "bitFieldSize": _integer(
            field.get("bitFieldSize", 0), "%s field bitFieldSize" % label
        ),
        "members": _physical_sorted(
            [
                _source_physical_constant_field(member, label)
                for member in members
            ]
        ),
    }


def _source_physical_variable_type(value: Any, label: str) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    members = source.get("members", source.get("fields", [])) or []
    if not isinstance(members, list) or any(
        not isinstance(member, Mapping) for member in members
    ):
        raise project_model.ReplayProjectError(
            "%s Shader resource type members are invalid" % label
        )
    return {
        "baseType": _physical_base_type(source.get("baseType", "")),
        "rows": _integer(source.get("rows", 0), "%s type rows" % label),
        "columns": _integer(
            source.get("columns", source.get("cols", 0)),
            "%s type columns" % label,
        ),
        "elements": _integer(
            source.get("elements", 0), "%s type elements" % label
        ),
        "arrayByteStride": _integer(
            source.get("arrayByteStride", source.get("arrayStride", 0)),
            "%s type arrayByteStride" % label,
        ),
        "matrixByteStride": _integer(
            source.get("matrixByteStride", source.get("matrixStride", 0)),
            "%s type matrixByteStride" % label,
        ),
        "rowMajor": bool(source.get("rowMajor", False)),
        "flags": _integer(source.get("flags", 0), "%s type flags" % label),
        "pointerTypeID": _integer(
            source.get("pointerTypeID", 0xFFFFFFFF),
            "%s type pointerTypeID" % label,
        ),
        "byteSize": _integer(
            source.get("byteSize", 0), "%s type byteSize" % label
        ),
        "members": _physical_sorted(
            [
                _source_physical_constant_field(member, label)
                for member in members
            ]
        ),
    }


def _source_block_material(
    block: Mapping[str, Any], block_index: int, label: str
) -> dict[str, Any]:
    fields = block.get("fields", block.get("variables", [])) or []
    if not isinstance(fields, list) or any(
        not isinstance(field, Mapping) for field in fields
    ):
        raise project_model.ReplayProjectError(
            "%s CBuffer fields are invalid" % label
        )
    completeness = str(block.get("layoutCompleteness", "") or "").strip().lower()
    if completeness not in {"complete", "partial", "opaque", "ambiguous"}:
        completeness = "complete" if fields else "opaque"
    return {
        "space": _integer(
            block.get("space", block.get("registerSpace", 0)),
            "%s CBuffer space" % label,
        ),
        "bind": _integer(
            block.get("bind", block.get("bindNumber", block_index)),
            "%s CBuffer bind" % label,
        ),
        "bindArraySize": _integer(
            block.get("bindArraySize", block.get("arraySize", 1)),
            "%s CBuffer bindArraySize" % label,
        ),
        "byteSize": _integer(
            block.get("byteSize", block.get("size", 0)),
            "%s CBuffer byteSize" % label,
        ),
        "bufferBacked": bool(block.get("bufferBacked", True)),
        "inlineDataBytes": bool(block.get("inlineDataBytes", False)),
        "compileConstants": bool(block.get("compileConstants", False)),
        "fields": _physical_sorted(
            [
                _source_physical_constant_field(field, label)
                for field in fields
            ]
        ),
        "layoutCompleteness": completeness,
    }


def _source_block_abi_sha256(
    block: Mapping[str, Any], block_index: int, label: str = "source alias"
) -> str:
    return project_model._canonical_sha256(
        _source_block_material(block, block_index, label)
    )


def _source_block_layout_sha256(
    block: Mapping[str, Any], block_index: int, stage: str
) -> str:
    material = _source_block_material(block, block_index, "source alias")
    original_fields = copy.deepcopy(
        block.get("fields", block.get("variables", [])) or []
    )
    block_material = {
        "index": block_index,
        "name": str(block.get("name", "") or ""),
        "bind": material["bind"],
        "space": material["space"],
        "bindArraySize": material["bindArraySize"],
        "byteSize": material["byteSize"],
        "bufferBacked": material["bufferBacked"],
        "inlineDataBytes": material["inlineDataBytes"],
        "compileConstants": material["compileConstants"],
        "fields": original_fields,
        "layoutCompleteness": material["layoutCompleteness"],
    }
    return project_model._canonical_sha256(
        {
            "schema": "renderdoc-ultra-event-cbuffer-layout/v1",
            "stage": project_model._normalised_stage(stage),
            "block": block_material,
        }
    )


def _source_shader_abi_sha256(shader: Mapping[str, Any]) -> str:
    """Recompute the Bridge's name-free physical Shader ABI from a catalog."""

    def signature(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "semanticName": str(
                item.get("semanticName", item.get("semantic", "")) or ""
            ).lower(),
            "semanticIdxName": str(item.get("semanticIdxName", "") or "").lower(),
            "semanticIndex": _integer(
                item.get("semanticIndex", item.get("index", 0)),
                "source Shader signature semanticIndex",
            ),
            "regIndex": _integer(
                item.get("regIndex", item.get("location", -1)),
                "source Shader signature regIndex",
            ),
            "systemValue": _physical_enum(item.get("systemValue", "")),
            "compType": _physical_base_type(
                item.get("compType", item.get("componentType", ""))
            ),
            "compCount": _integer(
                item.get("compCount", item.get("componentCount", 0)),
                "source Shader signature compCount",
            ),
            "stream": _integer(
                item.get("stream", 0), "source Shader signature stream"
            ),
            "regChannelMask": _integer(
                item.get("regChannelMask", item.get("registerMask", 0)),
                "source Shader signature regChannelMask",
            ),
            "perPrimitiveRate": bool(item.get("perPrimitiveRate", False)),
            "needSemanticIndex": bool(item.get("needSemanticIndex", False)),
        }

    def binding(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "space": _integer(
                item.get("space", item.get("registerSpace", 0)),
                "source Shader binding space",
            ),
            "bind": _integer(
                item.get("bind", item.get("bindNumber", -1)),
                "source Shader binding bind",
            ),
            "arraySize": _integer(
                item.get("arraySize", item.get("bindArraySize", 1)),
                "source Shader binding arraySize",
            ),
            "descriptorType": _physical_enum(
                item.get("descriptorType", item.get("resourceType", ""))
            ),
            "textureType": _physical_enum(
                item.get("textureType", item.get("dimension", ""))
            ),
            "variableType": _source_physical_variable_type(
                item.get("variableType", {}), "source Shader binding"
            ),
            "isTexture": bool(item.get("isTexture", False)),
            "hasSampler": bool(item.get("hasSampler", False)),
            "isInputAttachment": bool(item.get("isInputAttachment", False)),
            "isReadOnly": bool(item.get("isReadOnly", False)),
        }

    def sampler(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "space": _integer(
                item.get("space", item.get("registerSpace", 0)),
                "source Shader sampler space",
            ),
            "bind": _integer(
                item.get("bind", item.get("bindNumber", -1)),
                "source Shader sampler bind",
            ),
            "arraySize": _integer(
                item.get("arraySize", item.get("bindArraySize", 1)),
                "source Shader sampler arraySize",
            ),
        }

    def mapping_list(field: str, fallback: str = "") -> list[Mapping[str, Any]]:
        values = shader.get(field, shader.get(fallback, []) if fallback else []) or []
        if not isinstance(values, list) or any(
            not isinstance(value, Mapping) for value in values
        ):
            raise project_model.ReplayProjectError(
                "source Shader %s reflection is invalid" % field
            )
        return values

    blocks = _blocks(shader)
    dimensions = shader.get("threadGroupSize", []) or []
    if not isinstance(dimensions, list):
        raise project_model.ReplayProjectError(
            "source Shader threadGroupSize is invalid"
        )
    interface = {
        "stage": project_model._normalised_stage(shader.get("stage")),
        "entryPoint": str(shader.get("entryPoint", "") or ""),
        "inputSignature": _physical_sorted(
            [signature(item) for item in mapping_list("inputSignature", "inputs")]
        ),
        "outputSignature": _physical_sorted(
            [signature(item) for item in mapping_list("outputSignature", "outputs")]
        ),
        "readOnlyResources": _physical_sorted(
            [binding(item) for item in mapping_list("readOnlyResources", "resources")]
        ),
        "readWriteResources": _physical_sorted(
            [binding(item) for item in mapping_list("readWriteResources")]
        ),
        "samplers": _physical_sorted(
            [sampler(item) for item in mapping_list("samplers")]
        ),
        "constantBlocks": _physical_sorted(
            [
                _source_block_material(block, index, "source Shader")
                for index, block in enumerate(blocks)
            ]
        ),
        "threadGroupSize": [
            _integer(value, "source Shader threadGroupSize")
            for value in dimensions
        ],
        "outputTopology": _physical_enum(shader.get("outputTopology", "")),
        "dynamicLinkageInterfaceCount": _integer(
            shader.get("dynamicLinkageInterfaceCount", 0),
            "source Shader dynamicLinkageInterfaceCount",
        ),
    }
    return project_model._canonical_sha256(interface)


def _blocks(shader: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = shader.get(
        "constantBuffers",
        shader.get("constantBlocks", shader.get("constant_blocks", [])),
    )
    if not isinstance(value, list):
        raise project_model.ReplayProjectError(
            "alias target Shader CBuffer reflection is invalid"
        )
    return [block for block in value if isinstance(block, Mapping)]


def _select_block(
    shader: Mapping[str, Any], register_space: int, bind_number: int, label: str
) -> tuple[Mapping[str, Any], int]:
    matches = [
        (
            block,
            _nonnegative(
                block.get("index", block_index), "%s CBuffer index" % label
            ),
        )
        for block_index, block in enumerate(_blocks(shader))
        if _nonnegative(
            block.get("space", block.get("registerSpace", 0)),
            "%s.registerSpace" % label,
        )
        == register_space
        and _nonnegative(
            block.get("bind", block.get("bindNumber", 0)),
            "%s.bindNumber" % label,
        )
        == bind_number
    ]
    if len(matches) != 1:
        raise project_model.ReplayProjectError(
            "%s CBuffer binding is absent or ambiguous" % label
        )
    return matches[0]


def _block_name_path(
    block: Mapping[str, Any], block_index: int, label: str
) -> tuple[str, str]:
    name = str(block.get("name", "") or "")
    path = str(block.get("path", "") or "")
    fallback = name or "cbuffer%d" % block_index
    canonical_name = project_model._text(
        name or fallback, "%s CBuffer name" % label, 1024
    )
    canonical_path = project_model._text(
        path or fallback, "%s CBuffer path" % label, 2048
    )
    return canonical_name, canonical_path


def _leaf_fields(
    block: Mapping[str, Any], label: str
) -> dict[int, list[dict[str, Any]]]:
    fields = block.get("fields", block.get("variables", []))
    if not isinstance(fields, list):
        raise project_model.ReplayProjectError("%s CBuffer fields are invalid" % label)
    result: dict[int, list[dict[str, Any]]] = {}

    def visit(values: Sequence[Any], prefix: str, parent_offset: int) -> None:
        for index, field in enumerate(values):
            if not isinstance(field, Mapping):
                raise project_model.ReplayProjectError(
                    "%s CBuffer field is invalid" % label
                )
            name = project_model._text(
                str(
                    field.get(
                        "name", field.get("alias", field.get("path", "field%d" % index))
                    )
                ),
                "%s field name" % label,
                1024,
            )
            local_offset = _nonnegative(
                field.get("byteOffset", field.get("offset", 0)),
                "%s field byteOffset" % label,
            )
            absolute_offset = parent_offset + local_offset
            path = name if not prefix else "%s.%s" % (prefix, name)
            children = field.get("members", field.get("fields", [])) or []
            if not isinstance(children, list):
                raise project_model.ReplayProjectError(
                    "%s CBuffer field members are invalid" % label
                )
            if children:
                visit(children, path, absolute_offset)
            else:
                declared_path = field.get("canonicalPath")
                if declared_path is not None:
                    path = project_model._text(
                        declared_path,
                        "%s field canonicalPath" % label,
                        2048,
                    )
                result.setdefault(absolute_offset, []).append(
                    {"path": path, "name": name, "field": field}
                )

    visit(fields, "", 0)
    return result


def _field_physical_signature(field: Mapping[str, Any], label: str) -> tuple[Any, ...]:
    base_type = project_model._text(
        str(field.get("baseType", field.get("type", "")) or ""),
        "%s field baseType" % label,
        64,
    ).rsplit(".", 1)[-1]
    supported_types = {
        "Float",
        "SInt",
        "UInt",
        "Double",
        "Half",
        "Bool",
        "SLong",
        "ULong",
        "Byte",
        "UByte",
        "Short",
        "UShort",
    }
    if base_type not in supported_types:
        raise project_model.ReplayProjectError(
            "%s field baseType is not safe for direct alias rebinding" % label
        )
    row_major = field.get("rowMajor", False)
    if not isinstance(row_major, bool):
        raise project_model.ReplayProjectError(
            "%s field rowMajor is invalid" % label
        )
    return (
        base_type,
        _nonnegative(field.get("byteSize", field.get("size", 0)), "%s field byteSize" % label),
        _nonnegative(field.get("rows", 0), "%s field rows" % label),
        _nonnegative(field.get("columns", field.get("cols", 0)), "%s field columns" % label),
        _nonnegative(field.get("elements", 0), "%s field elements" % label),
        _nonnegative(field.get("arrayByteStride", field.get("arrayStride", 0)), "%s field arrayByteStride" % label),
        _nonnegative(field.get("matrixByteStride", field.get("matrixStride", 0)), "%s field matrixByteStride" % label),
        row_major,
        _nonnegative(field.get("flags", 0), "%s field flags" % label),
        _nonnegative(field.get("pointerTypeID", 0), "%s field pointerTypeID" % label),
        _nonnegative(field.get("bitFieldOffset", 0), "%s field bitFieldOffset" % label),
        _nonnegative(field.get("bitFieldSize", 0), "%s field bitFieldSize" % label),
    )


def _block_physical_signature(
    block: Mapping[str, Any], label: str
) -> tuple[Any, ...]:
    """Bind aliases only when the complete physical CBuffer layout agrees."""

    leaves = _leaf_fields(block, label)
    flattened: list[tuple[Any, ...]] = []
    for offset, fields in leaves.items():
        for item in fields:
            flattened.append(
                (
                    offset,
                    *_field_physical_signature(item["field"], label),
                )
            )
    flattened.sort()
    return (
        _nonnegative(
            block.get("byteSize", block.get("size", 0)),
            "%s CBuffer byteSize" % label,
        ),
        tuple(flattened),
    )


def _ensure_inventory_shader_identity(
    source_shader: Mapping[str, Any], target_shader: Mapping[str, Any]
) -> None:
    """Require the direct-inventory target to retain the source physical ABI."""

    source_api = project_model._api_family(source_shader.get("_projectApi"))
    source_encoding = project_model._shader_encoding(source_shader.get("encoding"))
    source_raw = project_model._sha(
        source_shader.get("rawSha256"), "source alias Shader rawSha256"
    )
    source_stage = project_model._normalised_stage(source_shader.get("stage"))
    source_entry = project_model._text(
        str(source_shader.get("entryPoint", "") or ""),
        "source alias Shader entryPoint",
        512,
        allow_empty=True,
    )
    source_abi = _source_shader_abi_sha256(source_shader)
    source_layout = project_model._sha(
        source_shader.get("layoutHash"),
        "source alias Shader layoutHash",
        allow_empty=True,
    )
    source_interface = project_model._sha(
        source_shader.get("interfaceHash"),
        "source alias Shader interfaceHash",
    )
    source_stable_identity = {
        "api": source_api,
        "encoding": source_encoding,
        "rawSha256": source_raw,
        "stage": source_stage,
        "entryPoint": source_entry,
        "abiHash": source_abi,
        "layoutHash": source_layout,
    }
    source_stable_key = project_model._stable_shader_identity_sha256(
        source_stable_identity
    )
    target_stable_identity = {
        "api": project_model._api_family(target_shader.get("api")),
        "encoding": project_model._shader_encoding(target_shader.get("encoding")),
        "rawSha256": project_model._sha(
            target_shader.get("rawSha256"), "target alias Shader rawSha256"
        ),
        "stage": project_model._normalised_stage(target_shader.get("stage")),
        "entryPoint": project_model._text(
            str(target_shader.get("entryPoint", "") or ""),
            "target alias Shader entryPoint",
            512,
            allow_empty=True,
        ),
        "abiHash": project_model._sha(
            target_shader.get("abiHash"), "target alias Shader abiHash"
        ),
        "layoutHash": project_model._sha(
            target_shader.get("layoutHash"),
            "target alias Shader layoutHash",
            allow_empty=True,
        ),
    }
    target_stable_key = project_model._sha(
        target_shader.get("stableKeySha256"),
        "target alias Shader stableKeySha256",
    )
    if target_stable_key != project_model._stable_shader_identity_sha256(
        target_stable_identity
    ):
        raise project_model.ReplayProjectError(
            "target alias Shader stable key is internally inconsistent"
        )
    target_interface = project_model._sha(
        target_shader.get("interfaceHash"), "target alias Shader interfaceHash"
    )
    identical = (
        source_stable_key == target_stable_key
        and source_stable_identity == target_stable_identity
        and target_interface == source_interface
    )
    if not identical and _physical_enum(source_shader.get("outputTopology", "")) in (
        "", "unknown"
    ):
        # Old Bridge versions serialized Topology.Unknown (zero) as an empty
        # string. Preserve every stored digest, but recognize the other spelling
        # only by recomputing BOTH complete interfaces from the verified source
        # catalog. The inventory's hashes alone cannot justify compatibility.
        from .symbols.scanner import interface_hash

        source_material = dict(source_shader)
        alternate = dict(source_material)
        alternate["outputTopology"] = (
            "unknown" if not _physical_enum(source_material.get("outputTopology", ""))
            else ""
        )
        alternate_identity = dict(source_stable_identity)
        alternate_identity["abiHash"] = _source_shader_abi_sha256(alternate)
        identical = (
            interface_hash(source_material) == source_interface
            and target_stable_identity == alternate_identity
            and target_stable_key
            == project_model._stable_shader_identity_sha256(alternate_identity)
            and target_interface == interface_hash(alternate)
        )
    if target_shader.get("interfaceComplete") is not True or not identical:
        raise project_model.ReplayProjectError(
            "project CBuffer alias Shader stable key or ABI differs from the target"
        )


def _resolve_physical_target(
    compact_alias: Mapping[str, Any],
    source_shader: Mapping[str, Any],
    target_shader: Mapping[str, Any],
    *,
    reflection_cache: dict[tuple[Any, ...], Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    kind = _binding_kind(compact_alias.get("bindingKind"))
    if kind == "shader":
        return kind, {
            "originalName": str(
                target_shader.get("name", target_shader.get("debugName", "")) or ""
            )
        }

    source_layout = str(source_shader.get("layoutHash", "") or "")
    target_layout = str(target_shader.get("layoutHash", "") or "")
    if not source_layout or source_layout != target_layout:
        raise project_model.ReplayProjectError(
            "project CBuffer alias layout differs from the target catalog"
        )
    register_space = compact_alias["registerSpace"]
    bind_number = compact_alias["bindNumber"]
    source_block_key = (
        "block",
        id(source_shader),
        register_space,
        bind_number,
        "source",
    )
    target_block_key = (
        "block",
        id(target_shader),
        register_space,
        bind_number,
        "target",
    )
    source_selection = (
        reflection_cache.get(source_block_key)
        if reflection_cache is not None
        else None
    )
    if source_selection is None:
        source_selection = _select_block(
            source_shader, register_space, bind_number, "source alias"
        )
        if reflection_cache is not None:
            reflection_cache[source_block_key] = source_selection
    source_block, source_block_index = source_selection
    target_selection = (
        reflection_cache.get(target_block_key)
        if reflection_cache is not None
        else None
    )
    if target_selection is None:
        target_selection = _select_block(
            target_shader, register_space, bind_number, "target alias"
        )
        if reflection_cache is not None:
            reflection_cache[target_block_key] = target_selection
    target_block, target_block_index = target_selection

    def ensure_complete_block_match() -> None:
        if target_shader.get("_inventoryIdentity") is True:
            if source_block_index != target_block_index:
                raise project_model.ReplayProjectError(
                    "project CBuffer block index differs from the target"
                )
            source_name_path = _block_name_path(
                source_block, source_block_index, "source alias"
            )
            target_name_path = _block_name_path(
                target_block, target_block_index, "target alias"
            )
            if source_name_path != target_name_path:
                raise project_model.ReplayProjectError(
                    "project CBuffer block name or path differs from the target"
                )
            source_block_abi = _source_block_abi_sha256(
                source_block, source_block_index
            )
            target_block_abi = project_model._sha(
                target_block.get("abiSha256"), "target alias CBuffer abiSha256"
            )
            if source_block_abi != target_block_abi:
                raise project_model.ReplayProjectError(
                    "project CBuffer block ABI differs from the target"
                )
            source_layout = _source_block_layout_sha256(
                source_block,
                source_block_index,
                str(source_shader.get("stage", "") or ""),
            )
            target_layout = project_model._sha(
                target_block.get("layoutSha256"),
                "target alias CBuffer layoutSha256",
            )
            if source_layout != target_layout:
                raise project_model.ReplayProjectError(
                    "project CBuffer block layout differs from the target"
                )
        source_signature_key = ("block-signature", id(source_block), "source")
        target_signature_key = ("block-signature", id(target_block), "target")
        source_block_signature = (
            reflection_cache.get(source_signature_key)
            if reflection_cache is not None
            else None
        )
        if source_block_signature is None:
            source_block_signature = _block_physical_signature(
                source_block, "source alias"
            )
            if reflection_cache is not None:
                reflection_cache[source_signature_key] = source_block_signature
        target_block_signature = (
            reflection_cache.get(target_signature_key)
            if reflection_cache is not None
            else None
        )
        if target_block_signature is None:
            target_block_signature = _block_physical_signature(
                target_block, "target alias"
            )
            if reflection_cache is not None:
                reflection_cache[target_signature_key] = target_block_signature
        if source_block_signature != target_block_signature:
            raise project_model.ReplayProjectError(
                "project CBuffer physical layout differs from the target"
            )

    if kind == "constantBuffer":
        ensure_complete_block_match()
        return kind, {
            "originalName": str(target_block.get("name", "") or ""),
            "registerSpace": register_space,
            "bindNumber": bind_number,
        }

    offset = compact_alias["byteOffset"]
    source_fields_key = ("fields", id(source_block), "source")
    target_fields_key = ("fields", id(target_block), "target")
    source_field_map = (
        reflection_cache.get(source_fields_key)
        if reflection_cache is not None
        else None
    )
    if source_field_map is None:
        source_field_map = _leaf_fields(source_block, "source alias")
        if reflection_cache is not None:
            reflection_cache[source_fields_key] = source_field_map
    target_field_map = (
        reflection_cache.get(target_fields_key)
        if reflection_cache is not None
        else None
    )
    if target_field_map is None:
        target_field_map = _leaf_fields(target_block, "target alias")
        if reflection_cache is not None:
            reflection_cache[target_fields_key] = target_field_map
    source_fields = source_field_map.get(offset, [])
    target_fields = target_field_map.get(offset, [])
    if len(source_fields) != 1 or len(target_fields) != 1:
        raise project_model.ReplayProjectError(
            "CBuffer field byteOffset is absent or ambiguous"
        )
    if _field_physical_signature(
        source_fields[0]["field"], "source alias"
    ) != _field_physical_signature(target_fields[0]["field"], "target alias"):
        raise project_model.ReplayProjectError(
            "CBuffer field physical type differs from the target"
        )
    ensure_complete_block_match()
    return kind, {
        "originalName": target_fields[0]["name"],
        "registerSpace": register_space,
        "bindNumber": bind_number,
        "byteOffset": offset,
        "canonicalPath": target_fields[0]["path"],
    }


def _candidate_binding_sha256(
    *,
    project_id: str,
    capture_id: str,
    catalog_sha256: str,
    target_identity: Mapping[str, Any],
    knowledge_id: str,
    group_index: int,
    match_mode: str,
    alias_count: int,
) -> str:
    return project_model._canonical_sha256(
        {
            "schema": MATERIALIZATION_SCHEMA,
            "schemaVersion": MATERIALIZATION_VERSION,
            "projectId": project_id,
            "captureId": capture_id,
            "catalogSha256": catalog_sha256,
            "targetShader": dict(target_identity),
            "knowledgeId": knowledge_id,
            "groupIndex": group_index,
            "matchMode": match_mode,
            "aliasCount": alias_count,
            "matcherPolicySha256": project_model.REUSE_MATCH_POLICY_SHA256,
        }
    )


def alias_candidate_binding_sha256(
    *,
    project_id: str,
    capture_id: str,
    catalog_sha256: str,
    target_shader: Mapping[str, Any],
    knowledge_id: str,
    group_index: int,
    match_mode: str,
    alias_count: int,
) -> str:
    """Public deterministic seal used when exposing a reuse-plan candidate."""

    target_identity = project_model.ReplayProjectService._reuse_shader_identity(
        target_shader
    )
    return _candidate_binding_sha256(
        project_id=project_model._text(project_id, "projectId", 32),
        capture_id=project_model._sha(capture_id, "captureId"),
        catalog_sha256=project_model._sha(catalog_sha256, "catalogSha256"),
        target_identity=target_identity,
        knowledge_id=project_model._text(knowledge_id, "knowledgeId", 32),
        group_index=_nonnegative(group_index, "groupIndex"),
        match_mode=project_model._text(match_mode, "matchMode", 64),
        alias_count=_nonnegative(alias_count, "aliasCount"),
    )


def _materialize_alias_candidates_snapshot(
    service: Any,
    project_id: str,
    capture_id: str,
    catalog_sha256: str,
    target_shader: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    manifest: Mapping[str, Any],
    capture: Mapping[str, Any],
    target_catalog: Mapping[str, Any] | None,
    state: Mapping[str, Any],
    artifact_bytes: Mapping[str, bytes],
    artifact_group_cache: dict[tuple[str, str, str, str, str], tuple[
        list[dict[str, Any]],
        dict[tuple[int, str, str, str], dict[str, Any]],
    ]],
    catalog_cache: dict[tuple[str, str, str], dict[str, Any]],
    shader_index_cache: dict[
        tuple[str, str, str],
        dict[tuple[int, str, str, str], dict[str, Any]],
    ],
    reflection_cache: dict[tuple[Any, ...], Any],
    budget: dict[str, int],
    inventory_target_shader: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize display aliases for exactly one target catalog Shader.

    ``candidates`` should normally be the complete ``aliasCandidates`` array
    from one reuse-plan match.  Combining the full set here is important: a
    pair of project records that assigns different names to the same physical
    target is rejected rather than being applied in last-writer-wins order.
    """

    if not isinstance(target_shader, Mapping):
        raise project_model.ReplayProjectError(
            "target Shader identity must be an object"
        )
    if (
        not isinstance(candidates, Sequence)
        or isinstance(candidates, (str, bytes, bytearray))
        or not candidates
        or len(candidates) > project_model.MAX_ALIASES
    ):
        raise project_model.ReplayProjectError(
            "alias candidates must be a bounded non-empty array"
        )

    expected_project_id = project_model._text(
        str(project_id or ""), "projectId", 32
    )
    if manifest.get("projectId") != expected_project_id:
        raise project_model.ReplayProjectError(
            "alias candidate belongs to a different Replay project"
        )
    target_capture_id = project_model._sha(capture_id, "captureId")
    target_catalog_sha256 = project_model._sha(
        catalog_sha256, "catalogSha256"
    )
    target_identity = service._reuse_shader_identity(target_shader)
    compact_target = service._capture_shader(
        capture,
        resource_id=target_identity["resourceId"],
        stage=target_identity["stage"],
        entry_point=target_identity["entryPoint"],
        raw_sha256=target_identity["rawSha256"],
    )
    if service._reuse_shader_identity(compact_target) != target_identity:
        raise project_model.ReplayProjectError(
            "target Shader identity differs from the exact project catalog"
        )
    if inventory_target_shader is None:
        if not isinstance(target_catalog, Mapping):
            raise project_model.ReplayProjectError(
                "target alias materialization catalog is missing"
            )
        target_catalog_key = (
            target_capture_id,
            target_catalog_sha256,
            str(
                service._artifact(
                    capture.get("catalogPath"),
                    "capture alias materialization catalog",
                )
            ),
        )
        target_shader_index = shader_index_cache.get(target_catalog_key)
        if target_shader_index is None:
            target_shader_index = _full_shader_index(target_catalog)
            shader_index_cache[target_catalog_key] = target_shader_index
        full_target_shader = _full_shader(
            target_catalog, compact_target, target_shader_index
        )
    else:
        full_target_shader = inventory_target_shader

    resolved: list[tuple[str, int, str, Mapping[str, Any], Mapping[str, Any]]] = []
    seen_candidates: set[tuple[str, int]] = set()
    for candidate_index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            raise project_model.ReplayProjectError(
                "alias candidate[%d] is invalid" % candidate_index
            )
        knowledge_id = str(candidate.get("knowledgeId", "") or "")
        if not project_model.HEX32_RE.fullmatch(knowledge_id):
            raise project_model.ReplayProjectError(
                "alias candidate knowledgeId is invalid"
            )
        match_mode = project_model._text(
            str(candidate.get("matchMode", "")), "alias candidate matchMode", 64
        )
        alias_count = _nonnegative(
            candidate.get("aliasCount"), "alias candidate aliasCount"
        )
        if candidate.get("candidateOnly") is not True:
            raise project_model.ReplayProjectError(
                "alias candidate is not marked candidateOnly"
            )

        raw_group_index = candidate.get("groupIndex")
        if raw_group_index is None:
            # Legacy reuse plans omitted groupIndex.  Preserve compatibility
            # only when the exact knowledge/matcher/count resolves uniquely.
            inferred = []
            for (known_id, known_index), pair in state["aliases"].items():
                group = pair[1]
                if (
                    known_id == knowledge_id
                    and project_model._match_mode(group, compact_target) == match_mode
                    and isinstance(group.get("aliases"), list)
                    and len(group["aliases"]) == alias_count
                ):
                    inferred.append(known_index)
            if len(inferred) != 1:
                raise project_model.ReplayProjectError(
                    "legacy alias candidate group is absent or ambiguous"
                )
            group_index = inferred[0]
        else:
            group_index = _nonnegative(
                raw_group_index, "alias candidate groupIndex"
            )
        key = (knowledge_id, group_index)
        if key in seen_candidates:
            raise project_model.ReplayProjectError(
                "alias candidate is duplicated"
            )
        seen_candidates.add(key)
        pair = state["aliases"].get(key)
        if pair is None:
            raise project_model.ReplayProjectError(
                "alias candidate group is absent from the project"
            )
        alias_set, group_record = pair
        actual_mode = project_model._match_mode(group_record, compact_target)
        aliases = group_record.get("aliases")
        if (
            not actual_mode
            or actual_mode != match_mode
            or not isinstance(aliases, list)
            or len(aliases) != alias_count
        ):
            raise project_model.ReplayProjectError(
                "alias candidate failed exact project matching"
            )
        expected_binding = _candidate_binding_sha256(
            project_id=expected_project_id,
            capture_id=target_capture_id,
            catalog_sha256=target_catalog_sha256,
            target_identity=target_identity,
            knowledge_id=knowledge_id,
            group_index=group_index,
            match_mode=match_mode,
            alias_count=alias_count,
        )
        supplied_binding = str(candidate.get("bindingSha256", "") or "")
        if supplied_binding and project_model._sha(
            supplied_binding, "alias candidate bindingSha256"
        ) != expected_binding:
            raise project_model.ReplayProjectError(
                "alias candidate binding was tampered"
            )
        artifact_groups, _unused_source_full = _artifact_alias_groups(
            service,
            manifest,
            alias_set,
            artifact_bytes=artifact_bytes,
            group_cache=artifact_group_cache,
            catalog_cache=catalog_cache,
            shader_index_cache=shader_index_cache,
            budget=budget,
        )
        if group_index >= len(artifact_groups):
            raise project_model.ReplayProjectError(
                "alias candidate group is absent from its artifact"
            )
        reconstructed = artifact_groups[group_index]
        _compare_group_record(group_record, reconstructed)
        if project_model._match_mode(reconstructed, compact_target) != match_mode:
            raise project_model.ReplayProjectError(
                "alias artifact failed exact target Shader matching"
            )
        resolved.append(
            (knowledge_id, group_index, match_mode, group_record, reconstructed)
        )

    fresh_candidates = {
        (str(reference["knowledgeId"]), int(reference["groupIndex"])): mode
        for reference, mode in service._indexed_reuse_matches(
            state["indexes"]["aliases"], compact_target
        )
    }
    if seen_candidates != set(fresh_candidates):
        raise project_model.ReplayProjectError(
            "alias candidate set is stale, incomplete, or contains unrelated knowledge"
        )
    for knowledge_id, group_index, match_mode, unused_record, unused_reconstructed in resolved:
        if fresh_candidates.get((knowledge_id, group_index)) != match_mode:
            raise project_model.ReplayProjectError(
                "alias candidate set differs from the current project matcher"
            )
    resolved.sort(key=lambda item: (item[0], item[1]))

    output_by_target: dict[tuple[Any, ...], dict[str, Any]] = {}
    canonical_paths: dict[str, tuple[Any, ...]] = {}
    source_paths_by_target: dict[tuple[Any, ...], str] = {}
    source_bindings: list[dict[str, Any]] = []
    for knowledge_id, group_index, match_mode, unused_record, reconstructed in resolved:
        source_shader = reconstructed["sourceShader"]
        if inventory_target_shader is not None:
            _ensure_inventory_shader_identity(source_shader, full_target_shader)
        source_aliases = reconstructed["sourceAliases"]
        compact_aliases = reconstructed["aliases"]
        for compact_alias, source_alias in zip(compact_aliases, source_aliases):
            kind, physical = _resolve_physical_target(
                compact_alias,
                source_shader,
                full_target_shader,
                reflection_cache=reflection_cache,
            )
            if kind == "shader":
                target_key: tuple[Any, ...] = (
                    kind,
                    compact_target["resourceId"],
                    compact_target["stage"],
                    compact_target.get("entryPoint", ""),
                    compact_target.get("rawSha256", ""),
                )
            elif kind == "constantBuffer":
                target_key = (
                    kind,
                    compact_target["resourceId"],
                    compact_alias["registerSpace"],
                    compact_alias["bindNumber"],
                )
            else:
                target_key = (
                    kind,
                    compact_target["resourceId"],
                    compact_alias["registerSpace"],
                    compact_alias["bindNumber"],
                    compact_alias["byteOffset"],
                )
                path = physical.get(
                    "canonicalPath", compact_alias["canonicalPath"]
                )
                previous_path_target = canonical_paths.setdefault(path, target_key)
                if previous_path_target != target_key:
                    raise project_model.ReplayProjectError(
                        "project alias canonicalPath maps to conflicting targets"
                    )
                source_path = compact_alias["canonicalPath"]
                previous_source_path = source_paths_by_target.setdefault(
                    target_key, source_path
                )
                if previous_source_path != source_path:
                    raise project_model.ReplayProjectError(
                        "project alias paths are ambiguous for one target binding"
                    )

            alias_name = compact_alias["alias"]
            existing = output_by_target.get(target_key)
            if existing is not None:
                if existing["alias"] != alias_name:
                    raise project_model.ReplayProjectError(
                        "project aliases conflict for one target binding"
                    )
                if (
                    kind == "constantField"
                    and existing.get("canonicalPath", "")
                    != physical.get(
                        "canonicalPath", compact_alias["canonicalPath"]
                    )
                ):
                    raise project_model.ReplayProjectError(
                        "project alias paths are ambiguous for one target binding"
                    )
                continue
            metadata = {
                "programVerified": True,
                "projectAliasRebound": True,
                "projectId": expected_project_id,
                "knowledgeId": knowledge_id,
                "groupIndex": group_index,
                "matchMode": match_mode,
                "captureId": target_capture_id,
                "captureRawSha256": compact_target.get("rawSha256", ""),
                "captureCodeSha256": compact_target.get("codeSha256", ""),
                "stage": compact_target["stage"],
                "entryPoint": compact_target.get("entryPoint", ""),
                "catalogSha256": target_catalog_sha256,
                "sourceCaptureId": source_alias.get("captureId"),
                "sourceShaderResourceId": reconstructed["sourceShaderResourceId"],
            }
            output = {
                "captureId": target_capture_id,
                "shaderResourceId": compact_target["resourceId"],
                "stage": compact_target["stage"],
                "entryPoint": compact_target.get("entryPoint", ""),
                "rawSha256": compact_target.get("rawSha256", ""),
                "codeSha256": compact_target.get("codeSha256", ""),
                "bindingKind": kind,
                "registerSpace": compact_alias["registerSpace"],
                "bindNumber": compact_alias["bindNumber"],
                "byteOffset": compact_alias["byteOffset"],
                "canonicalPath": physical.get(
                    "canonicalPath", compact_alias["canonicalPath"]
                ),
                "alias": alias_name,
                "originalName": physical.get("originalName", ""),
                "provenance": "program",
                "confidence": 1.0 if match_mode == "raw-exact" else 0.92,
                "verified": True,
                "metadata": metadata,
            }
            output_by_target[target_key] = output
        source_bindings.append(
            {
                "knowledgeId": knowledge_id,
                "groupIndex": group_index,
                "matchMode": match_mode,
                "sourceCaptureId": reconstructed["sourceAliases"][0].get(
                    "captureId", ""
                )
                if reconstructed["sourceAliases"]
                else "",
                "sourceShaderResourceId": reconstructed["sourceShaderResourceId"],
            }
        )

    aliases = [output_by_target[key] for key in sorted(output_by_target, key=str)]
    if not aliases:
        raise project_model.ReplayProjectError(
            "alias candidate contains no materializable display aliases"
        )
    alias_recovery = {
        knowledge_id: reconstructed["aliasRecovery"]
        for knowledge_id, _, _, _, reconstructed in resolved
        if reconstructed.get("aliasRecovery")
    }
    alias_table: dict[str, Any] = {
        "schema": project_model.ALIAS_SCHEMA,
        "schemaVersion": project_model.ALIAS_VERSION,
        "captureId": target_capture_id,
        "catalogSha256": target_catalog_sha256,
        "captureCatalog": (
            str(service._artifact(capture["catalogPath"], "capture catalog"))
            if capture.get("catalogPath")
            else ""
        ),
        "verifiedOnly": True,
        "aliases": aliases,
        "shaders": [
            {
                "shaderResourceId": compact_target["resourceId"],
                "stage": compact_target["stage"],
                "entryPoint": compact_target.get("entryPoint", ""),
                "rawSha256": compact_target.get("rawSha256", ""),
                "codeSha256": compact_target.get("codeSha256", ""),
                "aliases": aliases,
                "verified": True,
            }
        ],
        "symbolRules": [],
        "projectMaterialization": {
            "schema": MATERIALIZATION_SCHEMA,
            "schemaVersion": MATERIALIZATION_VERSION,
            "projectId": expected_project_id,
            "targetShader": target_identity,
            "sourceBindings": source_bindings,
            **({"aliasRecovery": alias_recovery} if alias_recovery else {}),
            "displayAliasesOnly": True,
            "captureLocalReplayStateReused": False,
        },
    }
    json_bytes = (
        json.dumps(
            alias_table,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(json_bytes) > MAX_MATERIALIZED_ALIAS_BYTES:
        raise project_model.ReplayProjectError(
            "materialized alias table exceeds qrenderdoc's loader limit"
        )
    return {
        "schema": MATERIALIZATION_SCHEMA,
        "schemaVersion": MATERIALIZATION_VERSION,
        "projectId": expected_project_id,
        "captureId": target_capture_id,
        "catalogSha256": target_catalog_sha256,
        "shader": copy.deepcopy(compact_target),
        "aliasCount": len(aliases),
        "aliasTable": alias_table,
        "jsonBytes": json_bytes,
        "sha256": project_model._sha256_bytes(json_bytes),
        "displayAliasesOnly": True,
        "applied": False,
        **({"aliasRecovery": alias_recovery} if alias_recovery else {}),
    }


def _inventory_full_shader(shader: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt one strictly normalized inventory record to reflection helpers."""

    blocks = []
    for block in shader["constantBlocks"]:
        fields = [
            {
                "canonicalPath": field["canonicalPath"],
                "name": field["name"],
                "byteOffset": field["absoluteByteOffset"],
                "byteSize": field["byteSize"],
                "baseType": field["baseType"],
                "rows": field["rows"],
                "columns": field["columns"],
                "elements": field["elements"],
                "arrayByteStride": field["arrayByteStride"],
                "matrixByteStride": field["matrixByteStride"],
                "rowMajor": field["rowMajor"],
                "flags": field["flags"],
                "pointerTypeID": field["pointerTypeID"],
                "bitFieldOffset": field["bitFieldOffset"],
                "bitFieldSize": field["bitFieldSize"],
                "members": [],
            }
            for field in block["fields"]
        ]
        blocks.append(
            {
                "index": block["index"],
                "name": block["name"],
                "path": block["path"],
                "space": block["space"],
                "bind": block["bind"],
                "byteSize": block["byteSize"],
                "layoutSha256": block["layoutSha256"],
                "abiSha256": block["abiSha256"],
                "fields": fields,
            }
        )
    return {
        "_inventoryIdentity": True,
        "shaderResourceId": shader["resourceId"],
        "resourceId": shader["resourceId"],
        "stage": shader["stage"],
        "entryPoint": shader["entryPoint"],
        "api": shader["api"],
        "encoding": shader["encoding"],
        "rawSha256": shader["rawSha256"],
        "abiHash": shader["abiHash"],
        "stableKeySha256": shader["stableKeySha256"],
        "codeSha256": "",
        "layoutHash": shader["layoutHash"],
        "interfaceHash": shader["interfaceHash"],
        "interfaceComplete": shader["interfaceComplete"],
        "constantBuffers": blocks,
    }


def materialize_alias_batch_from_inventory(
    service: Any,
    project_id: str,
    inventory: Mapping[str, Any],
    expected_binding: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Materialize aliases from a live resource inventory, without an EID catalog."""

    normalized = project_model._normalise_shader_inventory(inventory)
    if (
        not isinstance(targets, Sequence)
        or isinstance(targets, (str, bytes, bytearray))
        or not targets
        or len(targets) > MAX_BATCH_TARGETS
    ):
        raise project_model.ReplayProjectError(
            "alias materialization targets must contain 1..%d entries"
            % MAX_BATCH_TARGETS
        )
    inventory_targets = {
        shader["resourceId"]: shader for shader in normalized["shaders"]
    }
    normalised_targets: list[tuple[Mapping[str, Any], list[Mapping[str, Any]]]] = []
    requested_knowledge_ids: set[str] = set()
    seen_resources: set[int] = set()
    candidate_count = 0
    for target_index, raw_target in enumerate(targets):
        if not isinstance(raw_target, Mapping) or set(raw_target) != {
            "shader",
            "candidates",
        }:
            raise project_model.ReplayProjectError(
                "alias materialization target[%d] fields are invalid" % target_index
            )
        raw_shader = raw_target.get("shader")
        if not isinstance(raw_shader, Mapping):
            raise project_model.ReplayProjectError(
                "alias materialization target[%d] Shader is invalid" % target_index
            )
        resource_id = project_model._positive(
            raw_shader.get("resourceId"), "inventory target resourceId"
        )
        shader = inventory_targets.get(resource_id)
        if shader is None or dict(raw_shader) != shader:
            raise project_model.ReplayProjectError(
                "alias target is not one exact normalized inventory Shader"
            )
        if resource_id in seen_resources:
            raise project_model.ReplayProjectError(
                "alias materialization target Shader is duplicated"
            )
        seen_resources.add(resource_id)
        raw_candidates = raw_target.get("candidates")
        if (
            not isinstance(raw_candidates, Sequence)
            or isinstance(raw_candidates, (str, bytes, bytearray))
            or not raw_candidates
        ):
            raise project_model.ReplayProjectError(
                "alias materialization target[%d] candidates are invalid"
                % target_index
            )
        candidate_count += len(raw_candidates)
        if candidate_count > MAX_BATCH_CANDIDATES:
            raise project_model.ReplayProjectError(
                "alias materialization exceeds the %d candidate limit"
                % MAX_BATCH_CANDIDATES
            )
        candidates: list[Mapping[str, Any]] = []
        for candidate in raw_candidates:
            if not isinstance(candidate, Mapping):
                raise project_model.ReplayProjectError(
                    "alias materialization candidate is invalid"
                )
            knowledge_id = str(candidate.get("knowledgeId", "") or "")
            if not project_model.HEX32_RE.fullmatch(knowledge_id):
                raise project_model.ReplayProjectError(
                    "alias candidate knowledgeId is invalid"
                )
            requested_knowledge_ids.add(knowledge_id)
            candidates.append(candidate)
        normalised_targets.append((shader, candidates))

    expected_project_id = project_model._text(
        str(project_id or ""), "projectId", 32
    )
    capture_binding = normalized["capture"]
    target_capture_id = capture_binding["captureId"]
    target_catalog_sha256 = capture_binding["inventorySha256"]
    with project_model._thread_lock(service.root), project_model._project_lock(
        service.root
    ):
        manifest_before = project_model._file_bytes(
            service.manifest_path,
            project_model.MAX_PROJECT_BYTES,
            "project manifest",
        )
        preloaded_artifacts: dict[str, bytes] = {}
        manifest = service._load(
            alias_artifact_cache=preloaded_artifacts,
            alias_artifact_knowledge_ids=requested_knowledge_ids,
            alias_artifact_cache_max_bytes=MAX_BATCH_ALIAS_ARTIFACT_BYTES,
        )
        if project_model._file_bytes(
            service.manifest_path,
            project_model.MAX_PROJECT_BYTES,
            "project manifest",
        ) != manifest_before:
            raise project_model.ReplayProjectError(
                "project changed while the alias batch snapshot was loaded"
            )
        if manifest.get("projectId") != expected_project_id:
            raise project_model.ReplayProjectError(
                "alias candidate belongs to a different Replay project"
            )
        snapshot = service._reuse_snapshot_for_normalized_inventory(
            manifest, normalized
        )
        if (
            not isinstance(expected_binding, Mapping)
            or dict(expected_binding) != snapshot["binding"]
        ):
            raise project_model.ReplayProjectError(
                "shader inventory project binding is stale or changed"
            )
        missing_artifacts = requested_knowledge_ids - set(preloaded_artifacts)
        if missing_artifacts:
            raise project_model.ReplayProjectError(
                "alias candidate knowledge is absent from the project snapshot"
            )
        plan_candidates = {
            match["shader"]["resourceId"]: match["aliasCandidates"]
            for match in snapshot["reusePlan"]["matches"]
            if match["aliasCandidates"]
        }
        for shader, candidates in normalised_targets:
            if candidates != plan_candidates.get(shader["resourceId"]):
                raise project_model.ReplayProjectError(
                    "alias candidate set differs from the bound inventory plan"
                )
        capture = service._capture_from_shader_inventory(normalized)
        state = service._reuse_knowledge_state(manifest)
        catalog_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        shader_index_cache: dict[
            tuple[str, str, str],
            dict[tuple[int, str, str, str], dict[str, Any]],
        ] = {}
        reflection_cache: dict[tuple[Any, ...], Any] = {}
        artifact_group_cache: dict[
            tuple[str, str, str, str, str],
            tuple[
                list[dict[str, Any]],
                dict[tuple[int, str, str, str], dict[str, Any]],
            ],
        ] = {}
        unique_artifacts = {
            project_model._sha256_bytes(raw): len(raw)
            for raw in preloaded_artifacts.values()
        }
        budget = {
            "aliasArtifactBytes": sum(unique_artifacts.values()),
            "catalogBytes": 0,
            "materializedBytes": 0,
            "aliases": 0,
        }
        if budget["aliasArtifactBytes"] > MAX_BATCH_ALIAS_ARTIFACT_BYTES:
            raise project_model.ReplayProjectError(
                "alias materialization exceeds the artifact byte limit"
            )
        materializations: list[dict[str, Any]] = []
        for shader, candidates in normalised_targets:
            materialized = _materialize_alias_candidates_snapshot(
                service,
                expected_project_id,
                target_capture_id,
                target_catalog_sha256,
                shader,
                candidates,
                manifest=manifest,
                capture=capture,
                target_catalog=None,
                state=state,
                artifact_bytes=preloaded_artifacts,
                artifact_group_cache=artifact_group_cache,
                catalog_cache=catalog_cache,
                shader_index_cache=shader_index_cache,
                reflection_cache=reflection_cache,
                budget=budget,
                inventory_target_shader=_inventory_full_shader(shader),
            )
            budget["aliases"] += int(materialized["aliasCount"])
            budget["materializedBytes"] += len(materialized["jsonBytes"])
            if budget["aliases"] > MAX_BATCH_ALIASES:
                raise project_model.ReplayProjectError(
                    "alias materialization exceeds the %d alias limit"
                    % MAX_BATCH_ALIASES
                )
            if budget["materializedBytes"] > MAX_MATERIALIZED_ALIAS_BYTES:
                raise project_model.ReplayProjectError(
                    "alias materialization batch exceeds the loader byte limit"
                )
            materialized["inventoryOnly"] = True
            materializations.append(materialized)
        if project_model._file_bytes(
            service.manifest_path,
            project_model.MAX_PROJECT_BYTES,
            "project manifest",
        ) != manifest_before:
            raise project_model.ReplayProjectError(
                "project changed while aliases were materialized"
            )

    return {
        "schema": BATCH_MATERIALIZATION_SCHEMA,
        "schemaVersion": BATCH_MATERIALIZATION_VERSION,
        "projectId": expected_project_id,
        "captureId": target_capture_id,
        "catalogSha256": target_catalog_sha256,
        "manifestSha256": project_model._sha256_bytes(manifest_before),
        "targetCount": len(materializations),
        "candidateCount": candidate_count,
        "aliasCount": budget["aliases"],
        "materializations": materializations,
        "budgets": {
            "maxTargets": MAX_BATCH_TARGETS,
            "maxCandidates": MAX_BATCH_CANDIDATES,
            "maxAliases": MAX_BATCH_ALIASES,
            "maxAliasArtifactBytes": MAX_BATCH_ALIAS_ARTIFACT_BYTES,
            "maxCatalogBytes": MAX_BATCH_CATALOG_BYTES,
            "maxMaterializedBytes": MAX_MATERIALIZED_ALIAS_BYTES,
            "aliasArtifactBytes": budget["aliasArtifactBytes"],
            "catalogBytes": budget["catalogBytes"],
            "materializedBytes": budget["materializedBytes"],
        },
        "displayAliasesOnly": True,
        "inventoryOnly": True,
        "applied": False,
        "projectChangedDuringBatch": False,
    }


def materialize_alias_batch(
    service: Any,
    project_id: str,
    capture_id: str,
    catalog_sha256: str,
    targets: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Materialize one bounded, project-consistent alias restore batch.

    The project manifest, matcher indexes and target catalog are snapshotted
    once.  Candidate alias artifacts and source catalogs are content-verified
    and parsed at most once per immutable identity.  A project mutation during
    the operation invalidates the entire batch before any caller can publish
    its merged overlay.
    """

    if (
        not isinstance(targets, Sequence)
        or isinstance(targets, (str, bytes, bytearray))
        or not targets
        or len(targets) > MAX_BATCH_TARGETS
    ):
        raise project_model.ReplayProjectError(
            "alias materialization targets must contain 1..%d entries"
            % MAX_BATCH_TARGETS
        )
    normalised_targets: list[tuple[Mapping[str, Any], list[Mapping[str, Any]]]] = []
    requested_knowledge_ids: set[str] = set()
    seen_target_identities: set[tuple[int, str, str, str]] = set()
    candidate_count = 0
    for target_index, target in enumerate(targets):
        if not isinstance(target, Mapping) or not isinstance(
            target.get("shader"), Mapping
        ):
            raise project_model.ReplayProjectError(
                "alias materialization target[%d] is invalid" % target_index
            )
        target_identity = service._reuse_shader_identity(target["shader"])
        target_key = project_model._shader_identity(target_identity)
        if target_key in seen_target_identities:
            raise project_model.ReplayProjectError(
                "alias materialization target Shader is duplicated"
            )
        seen_target_identities.add(target_key)
        raw_candidates = target.get("candidates")
        if (
            not isinstance(raw_candidates, Sequence)
            or isinstance(raw_candidates, (str, bytes, bytearray))
            or not raw_candidates
        ):
            raise project_model.ReplayProjectError(
                "alias materialization target[%d] candidates are invalid"
                % target_index
            )
        candidate_count += len(raw_candidates)
        if candidate_count > MAX_BATCH_CANDIDATES:
            raise project_model.ReplayProjectError(
                "alias materialization exceeds the %d candidate limit"
                % MAX_BATCH_CANDIDATES
            )
        candidates: list[Mapping[str, Any]] = []
        for candidate_index, candidate in enumerate(raw_candidates):
            if not isinstance(candidate, Mapping):
                raise project_model.ReplayProjectError(
                    "alias materialization target[%d] candidate[%d] is invalid"
                    % (target_index, candidate_index)
                )
            knowledge_id = str(candidate.get("knowledgeId", "") or "")
            if not project_model.HEX32_RE.fullmatch(knowledge_id):
                raise project_model.ReplayProjectError(
                    "alias candidate knowledgeId is invalid"
                )
            requested_knowledge_ids.add(knowledge_id)
            candidates.append(candidate)
        normalised_targets.append((target["shader"], candidates))

    expected_project_id = project_model._text(
        str(project_id or ""), "projectId", 32
    )
    target_capture_id = project_model._sha(capture_id, "captureId")
    target_catalog_sha256 = project_model._sha(
        catalog_sha256, "catalogSha256"
    )
    with project_model._thread_lock(service.root), project_model._project_lock(
        service.root
    ):
        manifest_before = project_model._file_bytes(
            service.manifest_path,
            project_model.MAX_PROJECT_BYTES,
            "project manifest",
        )
        preloaded_artifacts: dict[str, bytes] = {}
        manifest = service._load(
            alias_artifact_cache=preloaded_artifacts,
            alias_artifact_knowledge_ids=requested_knowledge_ids,
            alias_artifact_cache_max_bytes=MAX_BATCH_ALIAS_ARTIFACT_BYTES,
        )
        if project_model._file_bytes(
            service.manifest_path,
            project_model.MAX_PROJECT_BYTES,
            "project manifest",
        ) != manifest_before:
            raise project_model.ReplayProjectError(
                "project changed while the alias batch snapshot was loaded"
            )
        if manifest.get("projectId") != expected_project_id:
            raise project_model.ReplayProjectError(
                "alias candidate belongs to a different Replay project"
            )
        missing_artifacts = requested_knowledge_ids - set(preloaded_artifacts)
        if missing_artifacts:
            raise project_model.ReplayProjectError(
                "alias candidate knowledge is absent from the project snapshot"
            )
        capture = service._capture(
            manifest, target_capture_id, target_catalog_sha256
        )
        state = service._reuse_knowledge_state(manifest)
        catalog_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        shader_index_cache: dict[
            tuple[str, str, str],
            dict[tuple[int, str, str, str], dict[str, Any]],
        ] = {}
        reflection_cache: dict[tuple[Any, ...], Any] = {}
        artifact_group_cache: dict[
            tuple[str, str, str, str, str],
            tuple[
                list[dict[str, Any]],
                dict[tuple[int, str, str, str], dict[str, Any]],
            ],
        ] = {}
        unique_artifacts = {
            project_model._sha256_bytes(raw): len(raw)
            for raw in preloaded_artifacts.values()
        }
        budget = {
            "aliasArtifactBytes": sum(unique_artifacts.values()),
            "catalogBytes": 0,
            "materializedBytes": 0,
            "aliases": 0,
        }
        if budget["aliasArtifactBytes"] > MAX_BATCH_ALIAS_ARTIFACT_BYTES:
            raise project_model.ReplayProjectError(
                "alias materialization exceeds the artifact byte limit"
            )
        target_catalog = _catalog_for_capture(
            service,
            capture,
            catalog_cache=catalog_cache,
            budget=budget,
        )
        materializations: list[dict[str, Any]] = []
        for target_shader, candidates in normalised_targets:
            materialized = _materialize_alias_candidates_snapshot(
                service,
                expected_project_id,
                target_capture_id,
                target_catalog_sha256,
                target_shader,
                candidates,
                manifest=manifest,
                capture=capture,
                target_catalog=target_catalog,
                state=state,
                artifact_bytes=preloaded_artifacts,
                artifact_group_cache=artifact_group_cache,
                catalog_cache=catalog_cache,
                shader_index_cache=shader_index_cache,
                reflection_cache=reflection_cache,
                budget=budget,
            )
            budget["aliases"] += int(materialized["aliasCount"])
            budget["materializedBytes"] += len(materialized["jsonBytes"])
            if budget["aliases"] > MAX_BATCH_ALIASES:
                raise project_model.ReplayProjectError(
                    "alias materialization exceeds the %d alias limit"
                    % MAX_BATCH_ALIASES
                )
            if budget["materializedBytes"] > MAX_MATERIALIZED_ALIAS_BYTES:
                raise project_model.ReplayProjectError(
                    "alias materialization batch exceeds the loader byte limit"
                )
            materializations.append(materialized)
        if project_model._file_bytes(
            service.manifest_path,
            project_model.MAX_PROJECT_BYTES,
            "project manifest",
        ) != manifest_before:
            raise project_model.ReplayProjectError(
                "project changed while aliases were materialized"
            )

    return {
        "schema": BATCH_MATERIALIZATION_SCHEMA,
        "schemaVersion": BATCH_MATERIALIZATION_VERSION,
        "projectId": expected_project_id,
        "captureId": target_capture_id,
        "catalogSha256": target_catalog_sha256,
        "manifestSha256": project_model._sha256_bytes(manifest_before),
        "targetCount": len(materializations),
        "candidateCount": candidate_count,
        "aliasCount": budget["aliases"],
        "materializations": materializations,
        "budgets": {
            "maxTargets": MAX_BATCH_TARGETS,
            "maxCandidates": MAX_BATCH_CANDIDATES,
            "maxAliases": MAX_BATCH_ALIASES,
            "maxAliasArtifactBytes": MAX_BATCH_ALIAS_ARTIFACT_BYTES,
            "maxCatalogBytes": MAX_BATCH_CATALOG_BYTES,
            "maxMaterializedBytes": MAX_MATERIALIZED_ALIAS_BYTES,
            "aliasArtifactBytes": budget["aliasArtifactBytes"],
            "catalogBytes": budget["catalogBytes"],
            "materializedBytes": budget["materializedBytes"],
        },
        "displayAliasesOnly": True,
        "applied": False,
        "projectChangedDuringBatch": False,
    }


def materialize_alias_candidates(
    service: Any,
    project_id: str,
    capture_id: str,
    catalog_sha256: str,
    target_shader: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compatibility route for one target, implemented as a batch of one."""

    result = materialize_alias_batch(
        service,
        project_id,
        capture_id,
        catalog_sha256,
        [{"shader": target_shader, "candidates": candidates}],
    )
    return result["materializations"][0]


def materialize_alias_candidate(
    service: Any,
    project_id: str,
    capture_id: str,
    catalog_sha256: str,
    target_shader: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Compatibility wrapper for callers that have exactly one candidate."""

    return materialize_alias_candidates(
        service,
        project_id,
        capture_id,
        catalog_sha256,
        target_shader,
        [candidate],
    )
