"""Complete per-parameter offset observations from one bounded CBuffer snapshot.

This is an investigation aid for the Agent, not an automatic rename map. Values
locate candidates; source consumers or compiled fields establish their meaning.
No array-order assumption or generic platform simulator is used here.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping

from . import shader_anchors

SCHEMA = "bread-material-parameter-localization/v1"
MAX_LOCATIONS = 8
REASONS = {
    "typed-storage": "Read reflected integer/boolean lanes; a float interpretation cannot locate this parameter.",
    "unavailable": "No readable captured numeric block was returned.",
    "tuple": "A value tuple locates candidate bytes, but does not prove compiled component width or parameter ownership.",
    "unique": "Unique component values still require consumer or compiled-layout corroboration.",
    "ambiguous": "Repeated or partially present values require a neighbouring layout, current override, or Shader consumer.",
    "not-found": "Inspect overrides, folded/unused parameters, typed storage and supported transforms only where relevant.",
    "competing": "Other material parameters fit these values too; compare consumers or compiled output ownership.",
}


def parameter_localization(material, blocks, *, relations=None, member_candidates=()):
    """Account for every parsed numeric parameter, including ordinary defaults.

    Search components independently as well as complete tuples. This exposes
    compiler lane splitting without claiming that unrelated equal lanes form a
    vector. Cap returned positions, but retain the real match count/ambiguity.
    """
    prepared, snapshots = [], []
    for block in blocks:
        values = shader_anchors.decode_float_lanes(block.get("floats") or [])
        prepared.append((block, values, shader_anchors.lane_indexes(values)))
        snapshots.append({"bind": block.get("bind"), "space": block.get("space", 0),
                          "values": [v if math.isfinite(v) else None for v in values]})
    chosen = tuple(relations) if relations else ("exact",)
    records = []
    location_budget = 512
    value_cache, tuple_cache = {}, {}
    value_owners, candidate_anchors = {}, {}
    for field, kind in (("scalars", "scalar"), ("vectors", "vector"),
                        ("integerParameters", "integer")):
        for parameter in material.get(field) or []:
            values = parameter.get("value") if kind == "vector" else [parameter.get("value")]
            row = {"parameter": parameter["name"], "kind": kind, "value": parameter.get("value"),
                   "components": []}
            if parameter.get("parameterInfo"):
                row["parameterInfo"] = parameter["parameterInfo"]
            if kind == "integer":
                row.update(status="unsupported-representation", reasonCode="typed-storage")
                records.append(row)
                continue
            tuple_hits = []
            tuple_anchors = []
            by_component = [[] for _ in values]
            component_anchors = [[] for _ in values]
            for block_index, (block, captured, indexes) in enumerate(prepared):
                hits = []
                for component, value in enumerate(values):
                    key = block_index, value
                    value_owners.setdefault(key, set()).add(parameter["name"])
                    if key not in value_cache:
                        positions = dict(shader_anchors.lanes_for_value(value, indexes, chosen))
                        value_cache[key] = positions, [{"bind": block.get("bind"),
                            "space": block.get("space", 0), "byteOffset": lane * 4,
                            "capturedValue": captured[lane], "relation": relation}
                            for lane, relation in positions.items()]
                    positions, locations = value_cache[key]
                    hits.append(positions)
                    by_component[component].extend(locations)
                    # Retain identity before trimming display locations. A
                    # display budget must never turn ambiguity into confidence.
                    if len(component_anchors[component]) < 2:
                        component_anchors[component].extend((block_index, lane)
                            for lane in list(positions)[:2 - len(component_anchors[component])])
                if kind == "vector":
                    key = block_index, tuple(values)
                    if key not in tuple_cache:
                        tuple_cache[key] = [{"bind": block.get("bind"), "space": block.get("space", 0),
                            "byteOffsets": [(start + i) * 4 for i in range(len(values))]}
                            for start in hits[0]
                            if all(start + i in positions for i, positions in enumerate(hits))]
                    tuple_hits.extend(tuple_cache[key])
                    if not tuple_anchors and tuple_cache[key]:
                        tuple_anchors = [(block_index, offset // 4)
                            for offset in tuple_cache[key][0]["byteOffsets"]]
            for component, positions in enumerate(by_component):
                keep = min(MAX_LOCATIONS, location_budget, len(positions))
                location_budget -= keep
                row["components"].append({"component": component,
                    "candidateCount": len(positions), "locations": positions[:keep],
                    "omittedLocationCount": len(positions) - keep})
            if not any(values for _, values, _ in prepared):
                status, reason = "unavailable", "unavailable"
            elif kind == "vector" and tuple_hits:
                status = "candidate" if len(tuple_hits) == 1 else "ambiguous"
                reason = "tuple"
            elif all(len(positions) == 1 for positions in by_component):
                status, reason = "candidate", "unique"
            elif any(by_component):
                status, reason = "ambiguous", "ambiguous"
            else:
                status, reason = "not-found", "not-found"
            keep = min(MAX_LOCATIONS, location_budget, len(tuple_hits))
            location_budget -= keep
            row.update(status=status, reasonCode=reason)
            if status == "candidate":
                candidate_anchors[len(records)] = (tuple_anchors if tuple_hits else
                    [anchor for anchors in component_anchors for anchor in anchors])
            if kind == "vector":
                row.update(tupleCandidateCount=len(tuple_hits), tupleLocations=tuple_hits[:keep],
                           omittedTupleLocationCount=len(tuple_hits) - keep)
            evidence = [dict(candidate) for candidate in member_candidates
                        if candidate.get("parameter") == parameter["name"]]
            if evidence:
                row["layoutCandidateCount"] = len(evidence)
                row["layoutCandidates"] = [{key: item.get(key) for key in
                    ("bind", "space", "byteOffset", "component", "basis", "relations")}
                    for item in evidence[:2]]
            records.append(row)
    wanted = set(anchor for anchors in candidate_anchors.values() for anchor in anchors)
    claims = {}
    for key, (positions, unused_locations) in value_cache.items():
        for lane in positions:
            anchor = key[0], lane
            if anchor in wanted:
                claims.setdefault(anchor, set()).update(value_owners[key])
    for row_index, anchors in candidate_anchors.items():
        row = records[row_index]
        competitors = set()
        for anchor in anchors:
            competitors.update(claims.get(anchor, ()))
        competitors.discard(row["parameter"])
        if competitors:
            row["competingParameters"] = sorted(competitors)[:8]
            if row["status"] == "candidate":
                row.update(status="ambiguous", reasonCode="competing")
    return {"schema": SCHEMA, "parameters": records,
            "parameterCount": len(records), "accountedParameterCount": len(records),
            "statusCounts": dict(Counter(row["status"] for row in records)),
            "captureValuesSha256": hashlib.sha256(json.dumps(snapshots, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
            "blockCount": len(blocks), "relationsTried": list(chosen), "reasonDescriptions": REASONS,
            "physicalAliasAuthority": False, "applied": False,
            "nextAction": "Use corroborated asset names first; inspect candidate consumers, retain ambiguity explicitly, then infer remaining roles. Do not rescan assets or compile for naming."}


def material_from_semantic_context(context):
    """Rehydrate the already-read dictionary; never carry old offset candidates."""
    from .shader_asset_semantics import SCHEMA as CONTEXT_SCHEMA, _canonical
    if not isinstance(context, Mapping) or context.get("schema") != CONTEXT_SCHEMA:
        raise ValueError("No retained material dictionary; read the exact associated material first")
    parameters = context.get("parameterDictionary")
    if (not isinstance(parameters, list) or not parameters or
            hashlib.sha256(_canonical(parameters)).hexdigest() !=
            (context.get("binding") or {}).get("parameterDocumentSha256")):
        raise ValueError("Retained material dictionary changed; refresh that material document")
    info = context.get("material") or {}
    material = {"objectName": info.get("name", ""), "className": info.get("class", ""),
                "parameterSource": info.get("parameterSource", "generic"),
                "defaultValuesOnly": info.get("defaultValuesOnly", False),
                "scalars": [], "vectors": [], "textures": [], "integerParameters": []}
    groups = {"scalar": "scalars", "vector": "vectors", "texture": "textures", "integer": "integerParameters"}
    for item in parameters:
        if item.get("kind") in groups:
            material[groups[item["kind"]]].append({key: value for key, value in item.items()
                if key in {"name", "value", "parameterInfo", "objectPath", "objectReference", "scale", "offset"}})
    return material
