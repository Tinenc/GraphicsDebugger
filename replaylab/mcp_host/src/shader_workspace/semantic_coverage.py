"""Local naming inventory and reflected per-binding delivery accounting.

Source aliases are suggestions, not recovered original symbols. The publication
gate accounts for physical lanes; only the Agent decides their semantic roles.
"""
from __future__ import annotations

from collections.abc import Mapping
import re

from .cbuffer_semantics import reflected_component_offsets

SCHEMA = "bread-cbuffer-binding-coverage/v1"


def source_inventory(source: str) -> list[dict]:
    from .quality import declared_binding_inventory
    from .outline import _strip_comments
    declarations = declared_binding_inventory(source)
    blocks = {}
    symbols = {}
    for row in declarations:
        key = (row.get("bind"), row.get("space", 0))
        if type(key[0]) is not int or key[0] < 0:
            continue
        if row.get("kind") == "cbuffer":
            blocks[key] = {"bind": key[0], "space": key[1], "block": row["name"],
                           "members": [], "candidates": []}
        elif row.get("kind") == "member" and key in blocks:
            member = {name: row.get(name) for name in ("name", "type", "byteOffset", "byteSize")}
            blocks[key]["members"].append(member)
            if (re.fullmatch(r"(?:float|int|uint)([234])?", str(member["type"]))
                    and type(member["byteOffset"]) is int and member["byteOffset"] >= 0
                    and type(member["byteSize"]) is int and 0 < member["byteSize"] <= 16):
                symbols[row["name"]] = (key, member)
    # Only direct scalar aliases/constructors are physical identities. Reject
    # arithmetic, function calls, casts, cross-buffer groups and repeated lanes.
    clean = _strip_comments(source)
    for match in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]+([^\r\n]+)", clean, re.M):
        name, expression = match.group(1), match.group(2).strip()
        constructor = re.fullmatch(r"(float|int|uint)([234])\s*\(([^()]*)\)", expression)
        parts = [part.strip() for part in constructor.group(3).split(",")] if constructor else [expression]
        if constructor and len(parts) != int(constructor.group(2)):
            continue
        picked, channels, key, scalar = [], [], None, None
        valid = True
        for index, part in enumerate(parts):
            if constructor and re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?[fu]?", part):
                continue  # Literal padding is not a physical buffer channel.
            value = symbols.get(part)
            if value is None or value[1]["byteSize"] != 4:
                valid = False
                break
            field_key, member = value
            if key is not None and (key != field_key or scalar != member["type"]):
                valid = False
                break
            key, scalar = field_key, member["type"]
            picked.append({"offset": member["byteOffset"]})
            channels.append("xyzw"[index] if constructor else "x")
        if (not valid or not picked or len({row["offset"] for row in picked}) != len(picked)
                or (constructor and constructor.group(1) != scalar)):
            continue
        line = source.count("\n", 0, match.start()) + 1
        blocks[key]["candidates"].append({"name": name, "type": scalar + (str(len(picked)) if len(picked) > 1 else ""),
            "components": picked, "sourceChannels": "".join(channels), "line": line,
            "evidence": "Direct source alias at line %d; review its consumers before publishing" % line,
            "semanticReviewRequired": True})
    return list(blocks.values())


def reflected_bindings(identity: Mapping) -> list[dict]:
    active = str(identity.get("activeDisplayShaderRawSha256") or "")
    use_active = bool(active and active != identity.get("rawSha256"))
    blocks = identity.get("activeDisplayBlocks") if use_active else identity.get("blocks")
    result = []
    for block in blocks or []:
        if not isinstance(block, Mapping) or type(block.get("bind")) is not int:
            continue
        offsets = set()
        invalid_components = []
        for leaf in block.get("leaves") or []:
            if not isinstance(leaf, Mapping):
                continue
            lanes = reflected_component_offsets(leaf)
            offsets.update(lanes)
            if "componentOffsets" in leaf and not lanes:
                invalid_components.append({"path": str(leaf.get("path") or "")[:512],
                                           "reason": "invalid-component-offsets"})
        issues = [{"path": str(issue.get("path") or "")[:512],
                   "reason": str(issue.get("reason") or "")[:128]}
                  for issue in (block.get("memberMappingIssues") or [])[:64]
                  if isinstance(issue, Mapping)]
        issue_count = block.get("memberMappingIssueCount", len(issues))
        issue_count = issue_count if type(issue_count) is int and issue_count >= 0 else len(issues)
        issue_count += len(invalid_components)
        issues = (issues + invalid_components)[:64]
        # Legacy identities did not publish the completeness flag. New partial
        # reflections must not be certified just because their surviving rows
        # have all been named or reviewed.
        mapping_complete = block.get("memberMappingComplete", True) is True and not issue_count and not issues
        result.append({"bind": block["bind"], "space": block.get("space", 0),
                       "block": str(block.get("block") or ""), "requiredOffsets": sorted(offsets),
                       "memberMappingComplete": mapping_complete,
                       "memberMappingIssues": issues,
                       "memberMappingIssueCount": issue_count,
                       "memberMappingIssuesTruncated": bool(block.get("memberMappingIssuesTruncated", False))})
    return result


def binding_coverage(identity: Mapping, fields: list, reviews: list,
                     previous: Mapping | None = None) -> dict:
    """Account every active reflected block, retaining untouched same-source rows."""
    if not isinstance(reviews, list) or len(reviews) > 128:
        raise ValueError("binding_reviews_json must contain at most 128 binding reviews")
    bindings = reflected_bindings(identity)
    keys = {(row["bind"], row["space"]) for row in bindings}
    review_by_key = {}
    for review in reviews:
        if not isinstance(review, Mapping):
            raise ValueError("binding review must be an object")
        key = (review.get("bind"), review.get("space", 0))
        offsets, evidence = review.get("unresolvedOffsets"), review.get("evidence")
        unused = review.get("unusedOffsets", [])
        unused_evidence = review.get("unusedEvidence", "")
        if (key not in keys or key in review_by_key or type(key[0]) is not int or type(key[1]) is not int
                or not isinstance(offsets, list) or any(type(item) is not int for item in offsets)
                or len(offsets) != len(set(offsets)) or not isinstance(evidence, str)
                or not evidence.strip() or len(evidence) > 4000):
            raise ValueError("binding review requires a unique reflected bind/space, unresolvedOffsets and concrete evidence")
        if (not isinstance(unused, list) or any(type(item) is not int for item in unused)
                or len(unused) != len(set(unused)) or not isinstance(unused_evidence, str)
                or len(unused_evidence) > 4000 or (unused and not unused_evidence.strip())):
            raise ValueError("unusedOffsets requires distinct physical offsets and unusedEvidence from all current Shader consumers")
        review_by_key[key] = {"unresolvedOffsets": sorted(offsets), "reviewEvidence": evidence.strip(),
                              "unusedOffsets": sorted(unused), "unusedEvidence": unused_evidence.strip()}
    prior_rows = (previous or {}).get("bindings", [])
    prior = {(row.get("bind"), row.get("space", 0)): row for row in prior_rows if isinstance(row, Mapping)}
    for row in bindings:
        key = (row["bind"], row["space"])
        requested = [field for field in fields if (field.get("bind"), field.get("space", 0)) == key]
        old = prior.get(key, {})
        if not requested and key not in review_by_key and old.get("requiredOffsets") == row["requiredOffsets"]:
            row.update({name: old[name] for name in ("semanticOffsets", "unresolvedOffsets", "reviewEvidence",
                                                    "unusedOffsets", "unusedEvidence") if name in old})
        else:
            row["semanticOffsets"] = sorted({item["offset"] for field in requested for item in field["components"]})
            row.update(review_by_key.get(key, {"unresolvedOffsets": [], "reviewEvidence": ""}))
        mapped, unresolved, required = (set(row.get(name) or []) for name in ("semanticOffsets", "unresolvedOffsets", "requiredOffsets"))
        unused = set(row.get("unusedOffsets") or [])
        if (not mapped <= required or not unresolved <= required or not unused <= required
                or mapped & unresolved or mapped & unused or unresolved & unused):
            raise ValueError("binding review offsets must be reflected, disjoint from semantic fields and in the named buffer")
        row["missingOffsets"] = sorted(required - mapped - unresolved - unused)
        row["status"] = "complete" if required and not row["missingOffsets"] and row["memberMappingComplete"] else "pending"
        row["namingComplete"] = row["status"] == "complete" and not unresolved
    pending = []
    for row in bindings:
        if row["status"] == "complete":
            continue
        record = {"bind": row["bind"], "space": row["space"], "missingLaneCount": len(row["missingOffsets"])}
        if not row["memberMappingComplete"]:
            record.update(reason="reflected-member-mapping-incomplete",
                          mappingIssues=row["memberMappingIssues"],
                          mappingIssueCount=row["memberMappingIssueCount"],
                          mappingIssuesTruncated=row["memberMappingIssuesTruncated"])
        pending.append(record)
    return {"schema": SCHEMA, "complete": bool(bindings) and not pending,
            "namingComplete": bool(bindings) and all(row["namingComplete"] for row in bindings),
            "bindingCount": len(bindings), "reviewedBindingCount": len(bindings) - len(pending),
            "namedLaneCount": sum(len(row.get("semanticOffsets", [])) for row in bindings),
            "reviewedUnusedLaneCount": sum(len(row.get("unusedOffsets", [])) for row in bindings),
            "unresolvedLaneCount": sum(len(row.get("unresolvedOffsets", [])) for row in bindings),
            "pendingBindings": pending, "bindings": bindings}


def coverage_state(receipt: Mapping) -> dict:
    coverage = receipt.get("bindingCoverage")
    if not isinstance(coverage, Mapping) or coverage.get("schema") != SCHEMA:
        return {"complete": False, "reason": "all-binding-review-missing"}
    rows = coverage.get("bindings")
    if not isinstance(rows, list) or not rows:
        return {"complete": False, "reason": "all-binding-review-missing"}
    summary = {name: coverage.get(name) for name in ("bindingCount", "reviewedBindingCount", "unresolvedLaneCount", "pendingBindings")}
    # Review coverage and recovered/inferred names are different facts. Older
    # receipts without lane details cannot establish that every role was named.
    def lane_count(row, name):
        value = row.get(name) if isinstance(row, Mapping) else None
        return len(value) if isinstance(value, list) else 0

    counts_known = all(isinstance(row, Mapping) and all(
        isinstance(row.get(name), list) for name in ("requiredOffsets", "semanticOffsets", "unresolvedOffsets"))
        for row in rows)
    invalid_lanes = False
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        lanes = {}
        for name in ("requiredOffsets", "semanticOffsets", "unresolvedOffsets", "unusedOffsets"):
            if name not in row:
                continue  # Legacy receipts still prove publication, not naming completeness.
            value = row[name]
            if (not isinstance(value, list) or any(type(item) is not int or item < 0 for item in value)
                    or len(value) != len(set(value))):
                invalid_lanes = True
                continue
            lanes[name] = set(value)
        if lanes.get("unusedOffsets") and not str(row.get("unusedEvidence") or "").strip():
            invalid_lanes = True
        if all(name in lanes for name in ("requiredOffsets", "semanticOffsets", "unresolvedOffsets")):
            named, unknown, unused = (lanes.get(name, set()) for name in
                                      ("semanticOffsets", "unresolvedOffsets", "unusedOffsets"))
            if (named & unknown or named & unused or unknown & unused
                    or named | unknown | unused != lanes["requiredOffsets"]):
                invalid_lanes = True
    summary.update(namingComplete=False,
        namedLaneCount=sum(lane_count(row, "semanticOffsets") for row in rows),
        reviewedUnusedLaneCount=sum(lane_count(row, "unusedOffsets") for row in rows))
    if counts_known:
        summary["unresolvedLaneCount"] = sum(lane_count(row, "unresolvedOffsets") for row in rows)
    if invalid_lanes or coverage.get("complete") is not True or any(
            not isinstance(row, Mapping) or row.get("status") != "complete" or row.get("missingOffsets")
            or row.get("memberMappingComplete", True) is not True
            or row.get("memberMappingIssueCount", 0) or row.get("memberMappingIssues") for row in rows):
        return {**summary, "complete": False, "reason": "all-binding-review-incomplete"}
    return {**summary, "complete": True,
            "namingComplete": counts_known and not any(row["unresolvedOffsets"] for row in rows)}
