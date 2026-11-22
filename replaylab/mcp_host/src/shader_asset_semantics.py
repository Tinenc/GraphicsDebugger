"""Bounded material vocabulary for an Agent, independent of alias publication.

The existing asset-resolution evidence stores these records. A material can be
useful before every compiled parameter has been located: its names and values
help explain calculations, while candidate offsets remain draw-local evidence.
This module neither decides material meaning nor grants physical rename rights.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
from pathlib import Path
from typing import Any


SCHEMA = "bread-shader-material-semantic-context/v1"
MAX_CONTEXT_BYTES = 512 * 1024
MAX_PARAMETERS = 1536
MAX_CANDIDATES = 1024
MAX_HYPOTHESES = 64


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _text(value: Any, maximum: int = 256) -> str:
    return str(value or "").strip()[:maximum]


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _bounded_json(value: Any, maximum: int = 4096) -> Any:
    """Keep a complete small JSON value, never a truncated serialization."""
    try:
        encoded = _canonical(value)
        return json.loads(encoded) if len(encoded) <= maximum else None
    except (ValueError, TypeError, OverflowError):
        return None


def _binding(identity: Mapping[str, Any], project_id: str,
             asset_binding: Mapping[str, Any], parameter_sha: str) -> dict[str, Any]:
    # The catalog revision can change while these exact material/parent/texture
    # files do not. Reuse depends on their content, not unrelated index growth.
    dependency_rows = [{"assetId": _text(item.get("assetId")),
                        "relativePath": _text(item.get("relativePath"), 2048),
                        "contentSha256": _text(item.get("contentSha256")).lower()}
                       for item in _rows(asset_binding.get("evidenceAssets"))]
    dependency_rows.sort(key=lambda item: (item["relativePath"], item["assetId"]))
    dependency_sha = (hashlib.sha256(_canonical(dependency_rows)).hexdigest()
                      if dependency_rows and all(row["relativePath"] and row["contentSha256"]
                                                 for row in dependency_rows) else "")
    return {
        "projectId": _text(project_id),
        "captureId": _text(identity.get("captureId")),
        "captureSessionId": _text(identity.get("captureSessionId")),
        "eventId": identity.get("eventId") if type(identity.get("eventId")) is int else 0,
        "stage": _text(identity.get("stage")),
        "entryPoint": _text(identity.get("entryPoint")),
        "shaderResourceId": _text(identity.get("shaderResourceId")),
        "shaderRawSha256": _text(identity.get("rawSha256") or identity.get("shaderRawSha256")).lower(),
        "captureApi": _text(identity.get("captureApi") or identity.get("api")),
        "assetRoot": _text(asset_binding.get("assetRoot"), 4096),
        "assetRelativePath": _text(asset_binding.get("assetRelativePath") or asset_binding.get("relativePath"), 2048),
        "assetContentSha256": _text(asset_binding.get("assetContentSha256")).lower(),
        "assetEvidenceSha256": _text(asset_binding.get("assetEvidenceSha256")).lower(),
        "assetDependencyContentSha256": dependency_sha,
        "catalogSha256": _text(asset_binding.get("catalogSha256")).lower(),
        "parameterDocumentSha256": parameter_sha,
    }


def _agent_hypotheses(notes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _rows(notes)[:MAX_HYPOTHESES]:
        note = {key: _text(item.get(key), 1600 if key in ("reason", "evidence") else 256)
                for key in ("parameter", "sourceSymbol", "suggestedName", "role", "reason", "evidence")
                if _text(item.get(key))}
        if note:
            note.update(status="agent-inferred", physicalAliasAuthority=False)
            if note not in result:
                result.append(note)
    return result


def with_agent_hypotheses(context: Mapping[str, Any], notes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge Agent interpretations without changing parameter or asset identity."""
    if context.get("schema") != SCHEMA:
        raise ValueError("unsupported material semantic context schema")
    if not isinstance(notes, Sequence) or isinstance(notes, (str, bytes)) or any(
            not isinstance(item, Mapping) for item in notes):
        raise ValueError("material hypotheses must be an array of objects")
    result = dict(context)
    # Latest interpretation wins for the same role/symbol while older unrelated
    # observations remain available; the bounded helper strips authority flags.
    newest = _agent_hypotheses(notes)
    keys = {(item.get("parameter"), item.get("sourceSymbol"), item.get("role")) for item in newest}
    retained = [item for item in _agent_hypotheses(context.get("agentHypotheses") or [])
                if (item.get("parameter"), item.get("sourceSymbol"), item.get("role")) not in keys]
    result["agentHypotheses"] = (newest + retained)[:MAX_HYPOTHESES]
    if len(_canonical(result)) > MAX_CONTEXT_BYTES:
        raise ValueError("material semantic context exceeds 512 KiB; scope the material evidence")
    return result


def build_material_semantic_context(
    material: Mapping[str, Any], *, identity: Mapping[str, Any], project_id: str,
    asset_binding: Mapping[str, Any] | None = None,
    member_candidates: Sequence[Mapping[str, Any]] = (),
    texture_candidates: Sequence[Mapping[str, Any]] = (),
    agent_notes: Sequence[Mapping[str, Any]] = (),
    parameter_localization: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Preserve all parsed parameters, including names with no offset match.

    Supply the output of ``material_naming.parse_material_parameters``. Evidence
    from different engines shares this representation without assuming any
    universal packing convention. Agent notes are hypotheses even if a caller
    attempts to label them verified.
    """
    parameters: list[dict[str, Any]] = []
    seen: set[str] = set()
    omitted = 0
    for field, kind in (("scalars", "scalar"), ("vectors", "vector"),
                        ("textures", "texture"), ("integerParameters", "integer")):
        for item in _rows(material.get(field)):
            name = _text(item.get("name"))
            if not name:
                continue
            parameter: dict[str, Any] = {"name": name, "kind": kind}
            if kind == "texture":
                parameter["objectPath"] = _text(item.get("objectPath"), 2048)
                for key in ("objectReference", "scale", "offset"):
                    value = _bounded_json(item.get(key))
                    if value is not None:
                        parameter[key] = value
            elif kind == "vector":
                value = item.get("value")
                if (not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 4
                        or not all(_number(component) for component in value)):
                    continue
                parameter["value"] = list(value)
                parameter["serializedComponentCount"] = len(value)
                parameter["compiledComponentWidthKnown"] = False
            else:
                if not _number(item.get("value")):
                    continue
                parameter["value"] = item["value"]
            info = _bounded_json(item.get("parameterInfo"))
            if isinstance(info, dict):
                parameter["parameterInfo"] = info
            parameter_id = hashlib.sha256(_canonical({key: parameter.get(key)
                for key in ("name", "kind", "parameterInfo")})).hexdigest()[:24]
            if parameter_id in seen:
                continue
            seen.add(parameter_id)
            if len(parameters) >= MAX_PARAMETERS:
                omitted += 1
                continue
            parameter["parameterId"] = parameter_id
            parameter["valueSource"] = ("material-default" if material.get("defaultValuesOnly")
                                        else "parsed-material-document")
            parameters.append(parameter)

    candidates: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    for source, kind in ((member_candidates, "cbuffer"), (texture_candidates, "texture")):
        for item in _rows(source):
            name = _text(item.get("parameter") or item.get("name"))
            matches = [parameter for parameter in parameters if parameter["name"] == name
                       and (parameter["kind"] == "texture") == (kind == "texture")]
            if not matches or len(candidates) >= MAX_CANDIDATES:
                continue
            candidate: dict[str, Any] = {"parameter": name, "kind": kind,
                "parameterIds": [parameter["parameterId"] for parameter in matches],
                "status": "candidate", "physicalAliasAuthority": False}
            # Preserve observed locations and transformations, never publication
            # flags/claims supplied by a matcher or Agent.
            for key in ("block", "bind", "space", "lane", "byteOffset", "byteSize",
                        "component", "value", "capturedValue", "relations", "packing",
                        "resourceId", "bindPoint", "slot", "asset", "workspaceSymbol",
                        "reason", "why", "evidenceKind", "hostEvidenceKind", "withheldReason"):
                value = _bounded_json(item.get(key), 2048)
                if value is not None:
                    candidate[key] = value
            signature = hashlib.sha256(_canonical(candidate)).hexdigest()
            if signature not in seen_candidates:
                seen_candidates.add(signature)
                candidates.append(candidate)

    hypotheses = _agent_hypotheses(agent_notes)
    located = {parameter_id for item in candidates for parameter_id in item["parameterIds"]}
    context: dict[str, Any] = {
        "schema": SCHEMA, "schemaVersion": 1,
        "binding": _binding(identity, project_id, dict(asset_binding or {}),
                            hashlib.sha256(_canonical(parameters)).hexdigest()),
        "material": {"name": _text(material.get("objectName")),
                     "class": _text(material.get("className")),
                     "parameterSource": _text(material.get("parameterSource")),
                     "defaultValuesOnly": material.get("defaultValuesOnly") is True},
        "parameterDictionary": parameters,
        "candidateCorrespondences": candidates,
        "unlocatedParameterNames": list(dict.fromkeys(parameter["name"] for parameter in parameters
            if parameter["parameterId"] not in located)),
        "agentHypotheses": hypotheses,
        "coverage": {"parameterCount": len(parameters), "candidateCount": len(candidates),
                     "omittedParameterCount": omitted,
                     "parameterDictionaryComplete": omitted == 0,
                     "allCompiledParametersLocated": False},
        "authority": {"semanticOnly": True, "physicalAliasAuthority": False,
                      "sourceNamesApplied": False, "panelAliasesApplied": False,
                      "inferenceOwner": "agent", "partialMatchBlocksOrganization": False},
    }
    if parameter_localization is not None:
        # The dictionary already retains names, values and parameter identities.
        # Keep observations within the existing evidence bound without silently
        # dropping an unlocated parameter or claiming omitted positions were read.
        retained = json.loads(_canonical(parameter_localization))
        for row in retained.get("parameters") or []:
            row.pop("value", None)
            row.pop("parameterInfo", None)
        context["parameterLocalization"] = retained
        context["parameterLocalization"]["binding"] = dict(context["binding"])
        if len(_canonical(context)) > MAX_CONTEXT_BYTES:
            for row in retained.get("parameters") or []:
                row.pop("layoutCandidates", None)
                row.pop("tupleLocations", None)
                row.pop("omittedTupleLocationCount", None)
                for component in row.get("components") or []:
                    component["omittedLocationCount"] = component.get("candidateCount", 0)
                    component.pop("locations", None)
            retained["locationDetailsRetained"] = False
            retained["detailNote"] = "The tool result retains sampled positions; this bounded dictionary keeps every parameter/count. Request only the needed parameter subset if its positions are no longer in context."
    if len(_canonical(context)) > MAX_CONTEXT_BYTES:
        raise ValueError("material semantic context exceeds 512 KiB; scope the material evidence")
    return context


def _same_root(left: Any, right: Any) -> bool:
    if not left or not right:
        return False
    return Path(str(left)).resolve() == Path(str(right)).resolve()


def assess_material_semantic_context(
    context: Mapping[str, Any], *, identity: Mapping[str, Any], project_id: str,
    asset_binding: Mapping[str, Any] | None = None, asset_root: str = "",
) -> dict[str, Any]:
    """Classify reuse without replay reads; missing freshness is never verified.

    The returned projection removes physical candidate locations after a change
    of draw, capture session, Shader binding or platform. A compatible project
    and raw Shader may still reuse historical material vocabulary, explicitly
    requiring the Agent to establish the current draw-to-material association.
    """
    bound = context.get("binding") if isinstance(context.get("binding"), Mapping) else {}
    current = _binding(identity, project_id, dict(asset_binding or {}), "")
    reasons: list[str] = []
    stale = False
    if context.get("schema") != SCHEMA:
        reasons.append("unsupported-context-schema")
        stale = True
    for key in ("projectId", "shaderRawSha256", "stage", "entryPoint"):
        if not bound.get(key) or bound.get(key) != current.get(key):
            reasons.append(key + "-mismatch-or-missing")
            stale = True
    root = asset_root or current.get("assetRoot")
    if root and not _same_root(bound.get("assetRoot"), root):
        reasons.append("asset-root-changed-or-missing")
        stale = True
    parameters = context.get("parameterDictionary")
    if not isinstance(parameters, list) or hashlib.sha256(_canonical(parameters)).hexdigest() != bound.get("parameterDocumentSha256"):
        reasons.append("parameter-dictionary-changed-or-missing")
        stale = True
    asset_current = isinstance(asset_binding, Mapping) and bool(asset_binding)
    dependency_key = ("assetDependencyContentSha256"
                      if bound.get("assetDependencyContentSha256") and current.get("assetDependencyContentSha256")
                      else "assetEvidenceSha256")
    for key in ("assetRelativePath", "assetContentSha256", dependency_key):
        if asset_current and (not bound.get(key) or bound.get(key) != current.get(key)):
            reasons.append(key + "-changed-or-missing")
            stale = True
    same_draw = True
    for key in ("captureId", "captureSessionId", "eventId", "shaderResourceId"):
        if not bound.get(key) or bound.get(key) != current.get(key):
            reasons.append(key + "-requires-current-evidence")
            same_draw = False
    if bound.get("captureApi") != current.get("captureApi"):
        reasons.append("capture-api-changed")
        same_draw = False
    if not asset_current:
        reasons.append("asset-content-freshness-not-checked")
    scope = "stale" if stale else "current-draw" if same_draw and asset_current else "shader-context-only"
    result = dict(context)
    result["reuse"] = {"scope": scope, "reasons": reasons,
        "drawMaterialAssociationConfirmed": False,
        "physicalAliasAuthority": False,
        "currentDrawEvidenceRequired": scope != "current-draw"}
    if scope != "current-draw":
        result["candidateCorrespondences"] = []
        result["withheldHistoricalCandidateCount"] = len(_rows(context.get("candidateCorrespondences")))
        result["unlocatedParameterNames"] = list(dict.fromkeys(
            _text(item.get("name")) for item in _rows(parameters) if _text(item.get("name"))))
        if "parameterLocalization" in result:
            result["parameterLocalization"] = {
                "status": "refresh-current-draw", "parameterCount": len(_rows(parameters)),
                "nextAction": "Reuse the retained parameter dictionary, compare current EID CBuffer values once; do not rescan assets."}
    if stale:
        result["parameterDictionary"] = []
        result["unlocatedParameterNames"] = []
        result["agentHypotheses"] = []
    result["coverage"] = {**dict(context.get("coverage") or {}),
        "parameterCount": len(_rows(result.get("parameterDictionary"))),
        "candidateCount": len(_rows(result.get("candidateCorrespondences"))),
        "observationScope": scope}
    return result


def semantic_context_summary(context: Mapping[str, Any], context_path: str = "") -> dict[str, Any]:
    """Small enough for begin/tool-result routing, with the full evidence path."""
    bound = context.get("binding") if isinstance(context.get("binding"), Mapping) else {}
    numeric_count = sum(item.get("kind") in {"scalar", "vector", "integer"}
                        for item in _rows(context.get("parameterDictionary")))
    localization = context.get("parameterLocalization") or {}
    localization_summary = {key: localization[key] for key in
        ("schema", "status", "parameterCount", "accountedParameterCount", "statusCounts", "nextAction")
        if key in localization}
    if numeric_count and not localization.get("schema"):
        localization_summary.update(required=True, parameterCount=numeric_count,
            status=localization.get("status") or "pending",
            tool="propose_shader_names_from_asset", reuse_material_context=True,
            nextAction="Before interpreting the Shader, compare every retained numeric parameter with this EID's CBuffer offsets once; reuse the dictionary and investigate unresolved candidates.")
    return {"schema": SCHEMA, "contextPath": str(context_path),
            "parameterCount": len(_rows(context.get("parameterDictionary"))),
            "candidateCount": len(_rows(context.get("candidateCorrespondences"))),
            "unlocatedCount": len(context.get("unlocatedParameterNames") or []),
            "hypothesisCount": len(_rows(context.get("agentHypotheses"))),
            "parameterLocalization": localization_summary,
            "binding": {key: bound.get(key) for key in ("projectId", "captureId", "eventId", "stage",
                "entryPoint", "shaderRawSha256", "assetRelativePath", "assetContentSha256",
                "parameterDocumentSha256")},
            "reuse": dict(context.get("reuse") or {"scope": "new-observations", "physicalAliasAuthority": False}),
            "authority": dict(context.get("authority") or {"semanticOnly": True, "physicalAliasAuthority": False})}
