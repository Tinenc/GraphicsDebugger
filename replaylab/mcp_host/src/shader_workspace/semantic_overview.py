"""Prepare portable semantic stage metadata without changing Shader source."""
from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_right
from functools import lru_cache
from pathlib import Path
from typing import Any

from .flow_graph import _function_records, _tokens, _matching_pairs
from .io import atomic_write_json


SCHEMA = "bread-shader-semantic-overview/v1"
REVIEW_SCHEMA = "bread-shader-semantic-overview-review/v1"
MAX_OVERVIEW_BYTES = 256 * 1024
MAX_STAGES = 256
MAX_ANCHORS = 512
# Keep identical to project_shader_stage_outline._ANCHOR_TOKEN. Signatures are
# intentionally independent of either structural parser's lexer choices.
_ANCHOR_TOKEN = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[uUfFlL]*|::|->|==|!=|<=|>=|&&|\|\||\+\+|--|\+=|-=|\*=|/=|<<|>>|[^\s]")
_COMMENTS = re.compile(r'//[^\r\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"')


def _canonical_tokens(source: str) -> list[tuple[str, int]]:
    masked = list(source)
    for match in _COMMENTS.finditer(source):
        for offset in range(match.start(), match.end()):
            if masked[offset] not in "\r\n":
                masked[offset] = " "
    continuation, offset = False, 0
    for line in "".join(masked).splitlines(True):
        if continuation or line.lstrip().startswith("#"):
            continuation = line.rstrip().endswith("\\")
            for position in range(offset, offset + len(line)):
                if masked[position] not in "\r\n":
                    masked[position] = " "
        else:
            continuation = False
        offset += len(line)
    return [(match.group(), match.start()) for match in _ANCHOR_TOKEN.finditer("".join(masked))]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value: Any, maximum: int, field: str, optional: bool = False) -> str:
    if not isinstance(value, str) or (not optional and not value.strip()) or len(value) > maximum:
        raise ValueError("Invalid semantic overview " + field)
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError("Invalid semantic overview " + field)
    return value.strip()


def prepare_overview(source: str, stages: list, entry_point: str) -> dict:
    """Bind Agent-authored major responsibilities to exact current source.

    Input stages contain id, title, optional description and anchors with a
    unique function/functionSignature and optional startLine/endLine. Each id
    identifies one responsibility; several anchors may belong to it. Equal
    titles never silently merge different branches or independent stage ids.
    """
    if not isinstance(source, str) or len(source.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("Shader source exceeds the semantic overview limit")
    if not isinstance(stages, list) or not stages or len(stages) > MAX_STAGES:
        raise ValueError("Semantic overview requires a bounded non-empty stage list")
    records = _function_records(source)
    parser_tokens = _tokens(source)
    token_positions = {(token.line, token.column): i for i, token in enumerate(parser_tokens)}
    source_lines = [0] + [offset + 1 for offset, char in enumerate(source) if char == "\n"]
    pairs = _matching_pairs(parser_tokens, "(", ")")
    for record in records:
        declaration = token_positions[(record["line"], record["column"])]
        close = pairs[declaration + 1]
        signature_source = source[parser_tokens[declaration].offset:parser_tokens[close].offset + 1]
        record["signature"] = " ".join(value for value, _ in _canonical_tokens(signature_source))
    by_name = {}
    for record in records:
        by_name.setdefault(record["name"], []).append(record)
    entry = by_name.get(entry_point, [])
    if len(entry) != 1:
        raise ValueError("Semantic overview entry must identify one source function")
    reachable, pending = set(), [entry_point]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        for record in by_name.get(name, []):
            pending.extend(record["calls"])
    source_sha = _sha(source)
    prepared, identities, anchor_count, duplicate_rows = [], set(), 0, set()
    navigation_bodies = {}
    for raw in stages:
        if not isinstance(raw, dict):
            raise ValueError("Semantic overview stage must be an object")
        identity = _text(raw.get("id"), 128, "id")
        if identity in identities:
            raise ValueError("Semantic overview stage ids must be unique; use several anchors for one phase")
        identities.add(identity)
        anchors = raw.get("anchors")
        if not isinstance(anchors, list) or not anchors:
            raise ValueError("Semantic overview stage requires source anchors")
        row = {"id": identity, "title": _text(raw.get("title"), 96, "title"),
               "description": _text(raw.get("description", ""), 1024, "description", True), "anchors": []}
        seen_anchors = set()
        for anchor in anchors:
            anchor_count += 1
            if not isinstance(anchor, dict) or anchor_count > MAX_ANCHORS:
                raise ValueError("Semantic overview has invalid or excessive anchors")
            signature = anchor.get("functionSignature")
            name = anchor.get("function", anchor.get("functionName"))
            candidates = [record for record in records if
                          (record["signature"] == " ".join(value for value, _ in _canonical_tokens(signature))
                           if isinstance(signature, str) and signature else record["name"] == name)]
            if len(candidates) != 1 or candidates[0]["name"] not in reachable:
                raise ValueError("Semantic overview anchor must identify one reachable source function")
            record = candidates[0]
            body = source[record["bodyStartOffset"]:record["bodyEndOffset"]]
            tokens = _canonical_tokens(body)
            body_signature = " ".join(value for value, _ in tokens)
            if len(tokens) <= 8192:
                navigation_bodies[_sha(body_signature)] = [value for value, _ in tokens]
            result = {"kind": "function", "functionName": record["name"],
                      "functionSignature": record["signature"], "functionBodySha256": _sha(body_signature),
                      "sourceSha256": source_sha,
                      "sourceRange": {"startLine": record["line"], "endLine": record["bodyEndLine"]}}
            if "startLine" in anchor or "endLine" in anchor:
                start, end = anchor.get("startLine"), anchor.get("endLine")
                if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int)
                        or not isinstance(end, int) or start > end
                        or start < record["line"] or end > record["bodyEndLine"]):
                    raise ValueError("Semantic overview range lies outside its function")
                selected = [i for i, (_, offset) in enumerate(tokens)
                            if start <= bisect_right(source_lines, record["bodyStartOffset"] + offset) <= end]
                if not selected:
                    raise ValueError("Semantic overview range contains no executable source tokens")
                first, stop = selected[0], selected[-1] + 1
                token_signature = " ".join(value for value, _ in tokens[first:stop])
                if len(token_signature) > 16384:
                    raise ValueError("Semantic overview range is too large; use a function anchor")
                result.update(kind="range", tokenStart=first, tokenEnd=stop, tokenSignature=token_signature,
                              sourceRange={"startLine": start, "endLine": end})
            key = json.dumps(result, sort_keys=True)
            if key not in seen_anchors:
                seen_anchors.add(key)
                row["anchors"].append(result)
        helpers = raw.get("helperSignatures", [])
        if not isinstance(helpers, list) or len(helpers) > MAX_ANCHORS:
            raise ValueError("Semantic overview helpers must be a bounded signature list")
        normalized_helpers = []
        for helper in helpers:
            if not isinstance(helper, str) or len(helper) > 1024:
                raise ValueError("Invalid semantic overview helper signature")
            signature = " ".join(value for value, _ in _canonical_tokens(helper))
            matches = [record for record in records
                       if record["signature"] == signature or record["name"] == helper]
            if len(matches) != 1 or matches[0]["name"] not in reachable:
                raise ValueError("Semantic overview helper must identify one reachable source function")
            if matches[0]["signature"] not in normalized_helpers:
                normalized_helpers.append(matches[0]["signature"])
                helper_body = source[matches[0]["bodyStartOffset"]:matches[0]["bodyEndOffset"]]
                helper_tokens = [value for value, _ in _canonical_tokens(helper_body)]
                if len(helper_tokens) <= 8192:
                    navigation_bodies[matches[0]["signature"]] = helper_tokens
        if normalized_helpers:
            row["helperSignatures"] = normalized_helpers
        duplicate_key = (row["title"], tuple(sorted(seen_anchors)))
        if duplicate_key in duplicate_rows:
            raise ValueError("Semantic overview repeats the same title and source anchors")
        duplicate_rows.add(duplicate_key)
        prepared.append(row)
    result = {"schema": SCHEMA, "version": 1, "sourceSha256": source_sha,
              "normalizedSourceSha256": _sha(source.replace("\r\n", "\n")),
              "entryPoint": entry_point, "stages": prepared}
    # Optional navigation evidence, outside reviewed stage identities. Old v1
    # records remain valid without it. Bound the addition independently so a
    # previously admissible overview cannot fail because tracking was added.
    navigation = {}
    for signature, body in sorted(navigation_bodies.items()):
        trial = dict(navigation, **{signature: body})
        if len(json.dumps(dict(result, navigationBodies=trial), ensure_ascii=False).encode('utf-8')) <= MAX_OVERVIEW_BYTES // 2:
            navigation[signature] = body
    if navigation:
        result['navigationBodies'] = navigation
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_OVERVIEW_BYTES:
        raise ValueError("Semantic overview exceeds its storage limit")
    return result


def save_overview(workspace: Path, overview: dict) -> Path:
    """Write the prepared metadata atomically; source remains untouched."""
    target = Path(workspace) / "ai" / "semantic-overview.json"
    atomic_write_json(target, overview)
    return target


def overview_digest(overview: dict) -> str:
    return _sha(json.dumps(overview, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def prepare_reviewed_overview(source: str, payload: dict, entry_point: str,
                              scope_review: dict | None = None) -> dict:
    """Bind the Agent's independent calculation assessment, never infer it.

    Scope mappings document why significant calculations deserve separate
    entries and where small supporting calculations belong. The Host verifies
    identities/coverage, not the semantics or a preferred number of stages.
    """
    if not isinstance(payload, dict):
        raise ValueError("semantic_overview requires stages and an independent review")
    overview = prepare_overview(source, payload.get("stages"), entry_point)
    review = payload.get("review")
    if not isinstance(review, dict) or review.get("source_sha256") != overview["sourceSha256"]:
        raise ValueError("semantic_overview.review must assess the exact current source_sha256")
    if review.get("remaining_work") != []:
        raise ValueError("Semantic overview review must explicitly contain remaining_work=[] before publication")
    evidence = {key: _text(review.get(key), 6000, key)
                for key in ("granularity_evidence", "data_flow_evidence")}
    mappings = review.get("scope_mappings")
    if not isinstance(mappings, list) or not mappings or len(mappings) > MAX_ANCHORS:
        raise ValueError("Semantic overview review requires bounded scope_mappings")
    records = _function_records(source)
    functions = {row["name"] for row in records}
    expected = {str(row["id"]): set(row.get("source_functions", []))
                for row in (scope_review or {}).get("results", [])}
    stage_ids = {row["id"] for row in overview["stages"]}
    seen, covered_functions, covered_stages, normalized = set(), set(), set(), []
    for item in mappings:
        if not isinstance(item, dict):
            raise ValueError("Invalid semantic overview scope mapping")
        identity = _text(item.get("scope_id"), 128, "scope_id")
        if identity in seen or (expected and identity not in expected):
            raise ValueError("Semantic overview scope mappings must uniquely match the source review scopes")
        seen.add(identity)
        mapped = item.get("stage_ids")
        if (not isinstance(mapped, list) or len(mapped) != len(set(mapped))
                or any(not isinstance(value, str) or value not in stage_ids for value in mapped)):
            raise ValueError("Semantic overview mapping references an absent or repeated stage")
        disposition = item.get("disposition", "represented")
        if disposition not in {"represented", "supporting", "inactive"} or (not mapped and disposition != "inactive"):
            raise ValueError("Every active calculation scope needs an owning directory stage")
        names = item.get("source_functions", sorted(expected.get(identity, [])))
        if (not isinstance(names, list) or not names or any(not isinstance(name, str) or name not in functions for name in names)
                or (expected and set(names) != expected[identity])):
            raise ValueError("Semantic overview scope functions must identify the current source assessment")
        normalized.append({"scope_id": identity, "stage_ids": mapped, "source_functions": sorted(set(names)),
                           "disposition": disposition, "evidence": _text(item.get("evidence"), 6000, "scope evidence")})
        covered_functions.update(names)
        covered_stages.update(mapped)
    if expected and seen != set(expected):
        raise ValueError("Semantic overview review omits source assessment scopes")
    if covered_functions != functions:
        raise ValueError("Semantic overview review omits source functions: " + ", ".join(sorted(functions - covered_functions)))
    if covered_stages != stage_ids:
        raise ValueError("Semantic overview review omits directory stages")
    overview["analysisReview"] = {"schema": REVIEW_SCHEMA, "sourceSha256": overview["sourceSha256"],
                                  "scopeMappings": normalized, **evidence, "remainingWork": [],
                                  "author": "agent", "hostSemanticApproval": False}
    if len(json.dumps(overview, ensure_ascii=False).encode("utf-8")) > MAX_OVERVIEW_BYTES:
        raise ValueError("Semantic overview and review exceed their storage limit")
    return overview


def reviewed_overview_state(source: str, overview: dict | None, receipt: dict | None = None) -> dict:
    # End-turn/status polling repeatedly checks the same immutable source and
    # metadata. Cache only exact bytes, never mutable paths or editor revisions.
    serialized = json.dumps(overview, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_OVERVIEW_BYTES:
        return {"complete": False, "state": "invalid", "reason": "Calculation overview exceeds its storage limit"}
    state = dict(_reviewed_overview_state_cached(source, serialized))
    if state["complete"] and receipt is not None and (
            receipt.get("overviewSha256") != state["overviewSha256"] or receipt.get("sourceSha256") != state["sourceSha256"]):
        return {"complete": False, "state": "invalid", "reason": "Source review does not identify the current calculation overview"}
    return state


@lru_cache(maxsize=4)
def _reviewed_overview_state_cached(source: str, serialized: str) -> dict:
    """Recheck persisted evidence. Navigation reanchoring is not re-review."""
    overview = json.loads(serialized)
    if not isinstance(overview, dict):
        return {"complete": False, "state": "missing", "reason": "Independent calculation overview is missing"}
    source_sha = _sha(source)
    if overview.get("sourceSha256") != source_sha:
        return {"complete": False, "state": "stale", "reason": "Calculation overview must be reviewed against the current source"}
    review = overview.get("analysisReview")
    if not isinstance(review, dict) or review.get("schema") != REVIEW_SCHEMA:
        return {"complete": False, "state": "unreviewed", "reason": "Calculation overview has no independent analysis review"}
    try:
        stages = []
        for row in overview["stages"]:
            anchors = []
            for anchor in row["anchors"]:
                value = {"functionSignature": anchor["functionSignature"]}
                if anchor["kind"] == "range":
                    value.update(anchor["sourceRange"])
                anchors.append(value)
            stages.append({"id": row["id"], "title": row["title"], "description": row.get("description", ""),
                           "anchors": anchors, "helperSignatures": row.get("helperSignatures", [])})
        prepared = prepare_reviewed_overview(source, {"stages": stages, "review": {
            "source_sha256": review.get("sourceSha256"), "scope_mappings": review.get("scopeMappings"),
            "granularity_evidence": review.get("granularity_evidence"),
            "data_flow_evidence": review.get("data_flow_evidence"), "remaining_work": review.get("remainingWork"),
        }}, overview["entryPoint"])
        if (prepared["stages"] != overview["stages"] or review.get("author") != "agent"
                or review.get("hostSemanticApproval") is not False):
            raise ValueError("Calculation overview source anchors or review provenance changed")
        if 'navigationBodies' in overview and overview['navigationBodies'] != prepared.get('navigationBodies', {}):
            raise ValueError('Calculation overview navigation evidence changed')
        digest = overview_digest(overview)
        return {"complete": True, "state": "reviewed", "sourceSha256": source_sha,
                "overviewSha256": digest, "stageCount": len(stages)}
    except (KeyError, TypeError, ValueError) as error:
        return {"complete": False, "state": "invalid", "reason": str(error)}


def overview_analysis_context(source: str, entry_point: str, overview: dict | None = None) -> dict:
    """Return source locations and prior evidence, without creating phases."""
    return {"sourceSha256": _sha(source), "entryPoint": entry_point,
            "functions": [{"name": row["name"], "startLine": row["line"], "endLine": row["bodyEndLine"],
                           "calls": row["calls"]} for row in _function_records(source)],
            "overview": overview, "overviewState": reviewed_overview_state(source, overview)}
