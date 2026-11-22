"""Persist source-scoped non-use observations without inventing parameter roles."""
from __future__ import annotations

from collections.abc import Mapping
import re

from .cbuffer_semantics import clean_fields, reflected_component_offsets

SCHEMA = "bread-cbuffer-usage-review/v1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_TYPES = frozenset(("float", "int", "uint", "bool"))


def clean_usage_review(value: object) -> dict:
    if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
        raise ValueError("CBuffer usage review schema is invalid")
    components, evidence = value.get("unusedComponents"), value.get("evidence")
    if (not isinstance(components, list) or len(components) > 16384
            or not isinstance(evidence, str) or not evidence.strip()
            or len(evidence) > 4000 or "\x00" in evidence):
        raise ValueError("CBuffer usage review needs bounded components and evidence")
    result, seen = [], set()
    for item in components:
        offset = item.get("offset") if isinstance(item, Mapping) else None
        scalar = item.get("type") if isinstance(item, Mapping) else None
        if (type(offset) is not int or offset < 0 or offset % 4
                or offset > 16 * 1024 * 1024 or offset in seen or scalar not in _TYPES):
            raise ValueError("CBuffer unused component needs a distinct typed 32-bit offset")
        seen.add(offset)
        result.append({"offset": offset, "type": scalar})
    return {"schema": SCHEMA, "unusedComponents": sorted(result, key=lambda item: item["offset"]),
            "evidence": evidence.strip()}


def clean_view_fields(fields: object, usage_review: object = None) -> list:
    """A wholly unused block can have a reviewed view without semantic names."""
    if fields == [] and usage_review is not None:
        clean_usage_review(usage_review)
        return []
    return clean_fields(fields)


def _typed_lanes(block: Mapping) -> dict[int, str | None]:
    if (block.get("memberMappingComplete", True) is not True
            or block.get("memberMappingIssueCount", 0) or block.get("memberMappingIssues")):
        return {}
    lanes = {}
    for leaf in block.get("leaves") or []:
        if not isinstance(leaf, Mapping):
            continue
        scalar = str(leaf.get("baseType") or "").lower().split(".")[-1]
        supported = (scalar in _TYPES and leaf.get("componentByteSize", 4) == 4
                     and leaf.get("semanticMappingAvailable") is not False)
        for offset in reflected_component_offsets(leaf):
            lanes[offset] = scalar if supported and offset not in lanes else None
    return lanes


def attach_usage_reviews(identity: Mapping, coverage: Mapping, publications: list,
                         source_sha256: str, reviewed_keys: set[tuple[int, int]]) -> list[dict]:
    """Attach exact typed review facts to the same atomic alias publication.

    Coverage describes the current display. A captured-layout mirror is allowed
    only when every reviewed lane has the same physical offset and scalar type.
    Incomplete/stripped reflection leaves its non-use status unspecified.
    """
    from . import aliases

    if not _SHA.fullmatch(source_sha256):
        return []  # Unbound legacy publications cannot establish current non-use.
    original_sha = str(identity.get("rawSha256") or "")
    display_sha = str(identity.get("activeDisplayShaderRawSha256") or original_sha)
    display_blocks = (identity.get("activeDisplayBlocks") if display_sha != original_sha
                      else identity.get("blocks")) or []
    blocks_by_sha = {original_sha: identity.get("blocks") or [], display_sha: display_blocks}
    reviews = {(row.get("bind"), row.get("space", 0)): row
               for row in coverage.get("bindings", []) if isinstance(row, Mapping)}
    # A review-only call, including a wholly unused block, must reach the panel.
    # The store preserves same-source existing fields when semanticFields is absent.
    for raw_sha, blocks in blocks_by_sha.items():
        for block in blocks:
            key = (block.get("bind"), block.get("space", 0))
            row = reviews.get(key, {})
            if key not in reviewed_keys or not row.get("reviewEvidence"):
                continue
            if any((item.get("bind"), item.get("space", 0)) == key
                   and (item.get("shaderScope") or {}).get("shaderRawSha256") == raw_sha
                   for item in publications):
                continue
            members = [{"name": item.get("name"), "offset": item.get("offset")}
                       for item in block.get("members", []) if isinstance(item, Mapping)]
            publications.append({"bind": key[0], "space": key[1], "block": block.get("block", ""),
                "members": members, "entries": [], "blockAlias": None,
                "shaderScope": {"shaderRawSha256": raw_sha,
                    "shaderResourceId": str(identity.get("shaderResourceId") or ""),
                    "stage": identity.get("stage", ""), "bind": key[0], "space": key[1],
                    "layoutSha256": aliases.layout_sha256(block.get("block", ""), members)},
                "semanticSourceSha256": source_sha256,
                "semanticCapturedShaderSha256": original_sha,
                "semanticBindingBasis": ("verified-active-reflection" if raw_sha != original_sha
                                          else "captured-reflection"),
                **({"displayMirror": True} if raw_sha != original_sha else {})})
    skipped = []
    for item in publications:
        key = (item.get("bind"), item.get("space", 0))
        row = reviews.get(key)
        if row is None or ("semanticFields" not in item and key not in reviewed_keys):
            continue
        raw_sha = (item.get("shaderScope") or {}).get("shaderRawSha256")
        source_blocks = [block for block in display_blocks
                         if (block.get("bind"), block.get("space", 0)) == key]
        target_blocks = [block for block in blocks_by_sha.get(raw_sha, [])
                         if (block.get("bind"), block.get("space", 0)) == key]
        offsets = row.get("unusedOffsets") or []
        evidence = row.get("unusedEvidence") if offsets else row.get("reviewEvidence")
        if not evidence:
            continue
        source_lanes = _typed_lanes(source_blocks[0]) if len(source_blocks) == 1 else {}
        target_lanes = _typed_lanes(target_blocks[0]) if len(target_blocks) == 1 else {}
        if any(not source_lanes.get(offset) or target_lanes.get(offset) != source_lanes[offset]
               for offset in offsets):
            skipped.append({"bind": key[0], "space": key[1], "shaderRawSha256": raw_sha,
                            "reason": "unused-component-reflection-mismatch"})
            continue
        item["semanticUsageReview"] = clean_usage_review({"schema": SCHEMA,
            "unusedComponents": [{"offset": offset, "type": source_lanes[offset]} for offset in offsets],
            "evidence": evidence})
        item["semanticSourceSha256"] = source_sha256
        item["semanticCapturedShaderSha256"] = original_sha
    # Do not manufacture an empty publication when a stripped original could not
    # receive the active reflection's review. Existing row/name writes still run.
    publications[:] = [item for item in publications if item.get("entries") or item.get("blockAlias")
                       or "semanticFields" in item or "semanticUsageReview" in item]
    return skipped
