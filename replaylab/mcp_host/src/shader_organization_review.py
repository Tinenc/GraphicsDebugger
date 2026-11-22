"""Agent-authored source reviews, bound to bytes rather than Host quality scores.

Reading this state never compiles, replays, or scans a workspace catalog. The
Agent owns the semantic verdict; these helpers only check identity and shape.
"""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_left
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


REVIEW_SCHEMA = "bread-shader-organization-review/v1"
STATE_SCHEMA = "bread-shader-organization-review-state/v1"
CHECKS = ("readability", "unity_style_structure", "function_calls", "redundant_temporaries")
_MAX_SOURCE = 4 * 1024 * 1024
_ORGANIZATION_TOOLS = frozenset({
    "begin_shader_workspace_organization", "write_shader_workspace_organization_file",
    "restructure_shader_workspace_source", "finish_shader_workspace_organization",
})
_SEMANTIC_OBSERVATION_TOOLS = frozenset({
    "get_shader_workspace_status", "compile_shader_workspace",
    "apply_shader_workspace_shared_resource",
})


def _semantic_contract(delivery: Mapping[str, Any], prior: Mapping[str, Any]) -> dict[str, Any]:
    previous = prior.get("cbufferSemanticDelivery")
    previous = previous if isinstance(previous, Mapping) else {}
    result = {"required": delivery["required"] or previous.get("required") is True}
    if "cbufferRequired" in delivery:
        result["cbufferRequired"] = bool(delivery["cbufferRequired"] or previous.get("cbufferRequired"))
    candidate = delivery.get("candidateRawSha256") or previous.get("candidateRawSha256")
    if re.fullmatch(r"[0-9a-f]{64}", str(candidate or "")):
        result["candidateRawSha256"] = candidate
    return result


def _regular_path(root: Path, value: str) -> Path:
    path = Path(value)
    path = path if path.is_absolute() else root / path
    if not value or path.is_symlink():
        raise ValueError("Shader review source path is absent or redirected")
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if not resolved.is_file():
        raise ValueError("Shader review source is not a regular file")
    # Reject redirected intermediate directories, including Windows junctions.
    current = path
    while current != root and current != current.parent:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Shader review path contains a redirected directory")
        current = current.parent
    return resolved


def _read_bytes(root: Path, value: str, limit: int) -> tuple[Path, bytes]:
    path = _regular_path(root, value)
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Shader review input exceeds its size limit")
    return path, data


def canonical_source_path(workspace: str) -> str:
    """Resolve saved source from its manifest without a compiler or replay read."""
    root = Path(workspace).resolve(strict=True)
    _, raw = _read_bytes(root, "manifest.json", 2 * 1024 * 1024)
    manifest = json.loads(raw)
    source = manifest.get("source") if isinstance(manifest, dict) else None
    value = source.get("path") if isinstance(source, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError("Shader workspace manifest has no current source path")
    return _regular_path(root, value).relative_to(root).as_posix()


def source_snapshot(workspace: str, organization: str) -> dict[str, Any]:
    """Resolve a draft or published source using its existing session identity."""
    if not re.fullmatch(r"[0-9a-f]{32}", organization):
        raise ValueError("organization_id must be the current 32-character organization ID")
    root = Path(workspace).resolve(strict=True)
    session_path, raw = _read_bytes(
        root, "ai/organization-sessions/%s/session.json" % organization, 2 * 1024 * 1024,
    )
    session = json.loads(raw)
    if not isinstance(session, dict) or session.get("organizationId") != organization:
        raise ValueError("Shader review organization identity differs from its session")
    draft = session.get("state") in {"drafting", "awaiting-proposal-review"}
    field = "proposalSourcePath" if draft else "sourcePath"
    source_path, data = _read_bytes(root, str(session.get(field) or ""), _MAX_SOURCE)
    source_sha = hashlib.sha256(data).hexdigest()
    baseline_sha = str(session.get("baselineSourceSha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", baseline_sha):
        raise ValueError("Shader review baseline identity is unavailable")
    canonical_sha = source_sha
    if draft:
        _canonical_path, canonical = _read_bytes(root, str(session.get("sourcePath") or ""), _MAX_SOURCE)
        canonical_sha = hashlib.sha256(canonical).hexdigest()
    source_conflict = canonical_sha not in {
        source_sha, baseline_sha, str(session.get("syncedDraftSourceSha256") or ""),
    } if draft else False
    return {
        "workspacePath": str(root), "organizationId": organization,
        "sourceKind": "proposal" if draft else "source",
        "sourcePath": str(source_path), "sourceSha256": source_sha,
        "baselineSourceSha256": baseline_sha, "changed": source_sha != baseline_sha,
        "baselineSourcePath": str(session.get("baselineSourcePath") or ""),
        "saved": canonical_sha == source_sha, "canonicalSourceSha256": canonical_sha,
        "sourceConflict": source_conflict,
        "reviewPath": str(session_path.parent / "self-review.json"),
    }


@lru_cache(maxsize=4)
def _scope_functions(source: bytes) -> tuple[str, ...]:
    # Declaration coverage only. Names, counts and body length never score
    # readability; a group may cover related functions and all their overloads.
    from .shader_workspace.flow_graph import ShaderFlowGraphError, _function_records
    try:
        names = {row["name"] for row in _function_records(source.decode("utf-8-sig"))}
    except ShaderFlowGraphError as error:
        raise ValueError("Source scope declarations could not be inventoried: " + str(error)) from error
    return tuple(sorted(names)) or ("<source>",)


def source_scope_catalog(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(str(snapshot["workspacePath"]))
    _, current = _read_bytes(root, str(snapshot["sourcePath"]), _MAX_SOURCE)
    if hashlib.sha256(current).hexdigest() != snapshot["sourceSha256"]:
        raise ValueError("Shader source changed while checking scope coverage")
    baseline_path = str(snapshot.get("baselineSourcePath") or "")
    if baseline_path:
        _, baseline = _read_bytes(root, baseline_path, _MAX_SOURCE)
    else:
        baseline = current
    if hashlib.sha256(baseline).hexdigest() != snapshot["baselineSourceSha256"]:
        raise ValueError("Initial whole-source bytes for the review scope are unavailable or changed")
    return {"baselineSourceSha256": snapshot["baselineSourceSha256"],
            "sourceSha256": snapshot["sourceSha256"],
            "initialFunctions": list(_scope_functions(baseline)),
            "currentFunctions": list(_scope_functions(current))}


def _review_text(value: Any, label: str, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(label + " needs nonempty concise evidence")
    return value.strip()


def _scope_names(value: Any, label: str) -> list[str]:
    if (not isinstance(value, list) or len(value) > 1024 or
            any(not isinstance(name, str) or not name.strip() or len(name) > 256 for name in value) or
            len(set(value)) != len(value)):
        raise ValueError(label + " must list unique source function names")
    return list(value)


def _source_anchor_context(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    from .shader_workspace.flow_graph import _function_records, _tokens

    root = Path(str(snapshot["workspacePath"]))
    _, raw = _read_bytes(root, str(snapshot["sourcePath"]), _MAX_SOURCE)
    if hashlib.sha256(raw).hexdigest() != snapshot["sourceSha256"]:
        raise ValueError("Shader source changed while checking retention anchors")
    # Accept platform newline conventions; all other source text stays exact.
    original = raw.decode("utf-8-sig")
    crlf_offsets = [match.start() for match in re.finditer("\r\n", original)]
    source = original.replace("\r\n", "\n")
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    records: dict[str, list[dict[str, Any]]] = {}
    for record in _function_records(source):
        records.setdefault(record["name"], []).append(record)
    tokens = _tokens(source)
    return {"source": source, "lines": lines, "offsets": offsets,
            "records": records, "tokens": tokens, "tokenOffsets": [token.offset for token in tokens],
            "crlfOffsets": crlf_offsets}


def _retention_anchor(value: Any, candidate: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a local source citation, not the Agent's mathematical argument."""
    if not isinstance(value, Mapping):
        raise ValueError("A retained candidate needs source_anchor with start_line, end_line and exact source text")
    start, end = value.get("start_line"), value.get("end_line")
    if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int)
            or not isinstance(end, int) or not 1 <= start <= end <= len(context["lines"])
            or end - start >= 64):
        raise ValueError("source_anchor must identify a local current-source span of at most 64 lines")
    text = _review_text(value.get("text"), "source_anchor.text", 4000).replace("\r\n", "\n")
    span = "".join(context["lines"][start - 1:end])
    local = span.find(text)
    if local < 0 or span.find(text, local + 1) >= 0:
        raise ValueError("source_anchor.text must match one exact occurrence within its current-source lines")
    begin = context["offsets"][start - 1] + local
    finish = begin + len(text)
    first = bisect_left(context["tokenOffsets"], begin)
    last = bisect_left(context["tokenOffsets"], finish)
    tokens = [token for token in context["tokens"][first:last]
              if token.offset + len(token.text) <= finish]
    locations = candidate.get("locations", [])
    # Catalog spans refer to the original decoded text, whereas anchor matching
    # accepts LF/CRLF conventions. Convert offsets without weakening them to
    # line-level matches: a line may contain several unrelated expressions.
    def normalized_offset(offset: int) -> int:
        return offset - bisect_left(context["crlfOffsets"], offset)
    records = [record for name in candidate["functions"] for record in context["records"].get(name, [])]
    for record in records:
        body = [token for token in tokens if record["bodyStartOffset"] <= token.offset < record["bodyEndOffset"]]
        words = [token.text for token in body]
        # Names, comments, signatures and punctuation alone are not an account
        # of the calculation. Actual expression/return/declaration tokens are.
        has_expression = any(word in {"=", "+=", "-=", "*=", "/=", "+", "-", "*", "/", "%", "?"}
                             for word in words)
        has_return = any(word == "return" and index + 1 < len(words) and words[index + 1] != ";"
                         for index, word in enumerate(words))
        has_call = any(re.fullmatch(r"[A-Za-z_]\w*", word) and index + 1 < len(words)
                       and words[index + 1] == "(" for index, word in enumerate(words))
        has_declaration = any(re.fullmatch(r"(?:bool|int|uint|half|float|double|min16float)[1-4]?(?:x[1-4])?", word)
                              and index + 1 < len(words) and re.fullmatch(r"[A-Za-z_]\w*", words[index + 1])
                              for index, word in enumerate(words))
        has_value = any(re.fullmatch(r"[A-Za-z_]\w*|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[A-Za-z0-9.+-]*)", word)
                        for word in words)
        if not has_value or not (has_expression or has_return or has_call or has_declaration):
            continue
        for location in locations:
            if location.get("function") != record["name"]:
                continue
            first_offset, last_offset = location.get("startOffset"), location.get("endOffset")
            if not isinstance(first_offset, int) or isinstance(first_offset, bool):
                continue
            first_offset = normalized_offset(first_offset)
            if isinstance(last_offset, int) and not isinstance(last_offset, bool):
                last_offset = normalized_offset(last_offset)
            elif (candidate.get("category") == "functionComments"
                  and location.get("endLine") == record["bodyEndLine"]
                  and first_offset <= record["bodyStartOffset"]):
                # A missing summary concerns the function as a whole, unlike
                # expression candidates whose exact spans are mandatory.
                last_offset = record["bodyEndOffset"]
            else:
                continue
            if any(first_offset <= token.offset < last_offset for token in body):
                return {"start_line": start, "end_line": end, "text": text}
    raise ValueError("source_anchor must cite a relevant expression in the candidate's function body, not only a name, comment, signature or unrelated code")


def _observation_dispositions(
    snapshot: Mapping[str, Any], value: Any, results: list[dict[str, Any]], verdict: str,
) -> list[dict[str, Any]]:
    """Check complete current-source candidate coverage, never semantic quality.

    The existing begin/write receipt links the complete candidate catalog. Their
    continued presence means they can be retained with a local explanation or
    identified as work to do; claiming they were removed would contradict the
    current bytes. The Agent, not this shape check, judges that explanation.
    """
    from .source_review_observations import review_candidates, source_review_catalog

    if not isinstance(value, list):
        raise ValueError("observation_dispositions must list the complete current-source candidate catalog")
    catalog = source_review_catalog(snapshot)
    if catalog.get("complete") is not True or catalog.get("sourceSha256") != snapshot["sourceSha256"]:
        raise ValueError("Source review requires the complete candidate catalog for these exact current-source bytes")
    candidates = {row["id"]: row for row in review_candidates(catalog)}
    if len(value) > len(candidates):
        raise ValueError("observation_dispositions exceeds the complete current-source candidate count")
    scopes = {row["id"]: row for row in results}
    normalized = []
    seen: set[str] = set()
    anchor_context = None
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("Each observation disposition must be an object")
        key = _review_text(item.get("candidate_id"), "observation candidate_id", 128)
        if key in seen or key not in candidates:
            raise ValueError("Observation dispositions must reference unique current surfaced candidate IDs")
        seen.add(key)
        scope_ids = _scope_names(item.get("scope_ids"), "observation scope_ids")
        if not scope_ids or set(scope_ids) - set(scopes):
            raise ValueError("Observation scope_ids must reference the matching reviewed scopes")
        functions = {name for scope_id in scope_ids for name in scopes[scope_id]["source_functions"]}
        if set(candidates[key]["functions"]) - functions:
            raise ValueError("Observation scopes do not cover the candidate's current source functions")
        status = item.get("status")
        if status not in {"retained", "needs_work"}:
            raise ValueError("A current surfaced candidate is still present: use retained with its specific local reason or needs_work, never resolved")
        if status == "needs_work":
            if verdict == "complete":
                raise ValueError("A complete review cannot retain a source observation needing work")
            if all(scopes[scope_id]["status"] == "satisfied" for scope_id in scope_ids):
                raise ValueError("A source observation needing work must belong to an unmet or blocked scope")
        row = {"candidate_id": key, "scope_ids": scope_ids, "status": status,
               "evidence": _review_text(item.get("evidence"), "observation-specific evidence")}
        if status == "retained":
            row["alternative"] = _review_text(item.get("alternative"), "retained candidate alternative")
            if anchor_context is None:
                anchor_context = _source_anchor_context(snapshot)
            row["source_anchor"] = _retention_anchor(item.get("source_anchor"), candidates[key], anchor_context)
        normalized.append(row)
    if verdict == "complete" and seen != set(candidates):
        missing = sorted(set(candidates) - seen)
        raise ValueError("Complete source review omits current catalog candidates (%d): " % len(missing)
                         + ", ".join(missing[:16]) + ("; read the linked full catalog" if len(missing) > 16 else ""))
    return normalized


def _scope_review(snapshot: Mapping[str, Any], value: Any, verdict: str,
                  *, require_observations: bool = False) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("scope_review must bind the initial whole source, planned scopes and their results")
    if value.get("baseline_source_sha256") != snapshot["baselineSourceSha256"]:
        raise ValueError("scope_review baseline_source_sha256 differs from the initial whole source")
    plan, results = value.get("plan"), value.get("results")
    if (not isinstance(plan, list) or not 1 <= len(plan) <= 256 or
            not isinstance(results, list) or len(results) != len(plan)):
        raise ValueError("scope_review needs a nonempty plan and exactly one result per planned scope")
    catalog = source_scope_catalog(snapshot)
    initial = set(catalog["initialFunctions"])
    current = set(catalog["currentFunctions"])
    planned: dict[str, dict[str, Any]] = {}
    initial_covered: set[str] = set()
    for item in plan:
        if not isinstance(item, Mapping):
            raise ValueError("Each planned scope must be an object")
        key = _review_text(item.get("id"), "scope id", 128)
        if key in planned:
            raise ValueError("Planned scope ids must be unique")
        functions = _scope_names(item.get("functions"), "plan.functions")
        if not functions or set(functions) - initial:
            raise ValueError("Planned scopes must reference initial source functions")
        initial_covered.update(functions)
        planned[key] = {"id": key, "requirement": _review_text(item.get("requirement"), "scope requirement"),
                        "functions": functions}
    if initial_covered != initial:
        raise ValueError("scope_review omits initial source functions: " + ", ".join(sorted(initial - initial_covered)))
    normalized = []
    seen: set[str] = set()
    current_covered: set[str] = set()
    statuses = {"satisfied", "unmet", "blocked"}
    for item in results:
        if not isinstance(item, Mapping):
            raise ValueError("Each scope result must be an object")
        key = _review_text(item.get("id"), "result scope id", 128)
        if key not in planned or key in seen or item.get("status") not in statuses:
            raise ValueError("Scope results must match unique planned ids and satisfied/unmet/blocked status")
        seen.add(key)
        functions = _scope_names(item.get("source_functions"), "result.source_functions")
        if set(functions) - current:
            raise ValueError("Scope result references functions absent from the current source")
        current_covered.update(functions)
        branches = item.get("branches")
        if not isinstance(branches, list) or not 1 <= len(branches) <= 256:
            raise ValueError("Each scope needs its branch/path assessment (one straight-line path is valid)")
        paths = []
        for branch in branches:
            if not isinstance(branch, Mapping) or branch.get("status") not in statuses:
                raise ValueError("Each branch/path needs satisfied/unmet/blocked status")
            paths.append({"path": _review_text(branch.get("path"), "branch/path", 512),
                          "status": branch["status"], "evidence": _review_text(branch.get("evidence"), "branch evidence")})
        remaining = item.get("remaining_work")
        if not isinstance(remaining, list) or len(remaining) > 32:
            raise ValueError("remaining_work must list remaining repairs for each scope")
        remaining = [_review_text(text, "remaining scope work") for text in remaining]
        if item["status"] == "satisfied" and (remaining or any(p["status"] != "satisfied" for p in paths)):
            raise ValueError("A satisfied scope cannot retain unmet/blocked paths or remaining_work")
        if item["status"] != "satisfied" and not remaining:
            raise ValueError("An unmet/blocked scope must identify its remaining_work")
        if verdict == "complete" and item["status"] != "satisfied":
            raise ValueError("A complete review cannot retain unmet/blocked scopes")
        normalized.append({"id": key, "status": item["status"], "source_functions": functions,
                           "evidence": _review_text(item.get("evidence"), "scope evidence"), "branches": paths,
                           "retained_intermediates": _review_text(item.get("retained_intermediates"), "retained intermediate rationale"),
                           "remaining_work": remaining})
    if verdict == "complete" and current_covered != current:
        raise ValueError("Complete scope review omits current source functions: " + ", ".join(sorted(current - current_covered)))
    normalized_value = {"baseline_source_sha256": snapshot["baselineSourceSha256"],
                        "plan": list(planned.values()), "results": normalized}
    if "observation_dispositions" in value:
        normalized_value["observation_dispositions"] = _observation_dispositions(
            snapshot, value["observation_dispositions"], normalized, verdict)
    elif require_observations and verdict == "complete":
        raise ValueError("scope_review.observation_dispositions is required: assess the current surfaced source candidates in this same review")
    if len(json.dumps(normalized_value, ensure_ascii=False).encode("utf-8")) > 192 * 1024:
        raise ValueError("scope_review exceeds its bounded evidence size")
    return normalized_value


def make_review(
    snapshot: Mapping[str, Any], expected_source_sha256: str, verdict: str,
    checks: Mapping[str, Any], findings: list[str], next_actions: list[str], blocker: str,
    no_change_reason: str = "",
    scope_review: Mapping[str, Any] | None = None,
    *, require_scope: bool = False, prepared_overview: dict | None = None,
    require_overview: bool = False,
) -> dict[str, Any]:
    if expected_source_sha256 != snapshot["sourceSha256"]:
        raise ValueError("Shader source changed since the Agent reviewed it; review the current bytes")
    if verdict not in {"needs_work", "complete", "blocked"}:
        raise ValueError("verdict must be needs_work, complete or blocked")
    if not isinstance(checks, Mapping) or set(checks) != set(CHECKS):
        raise ValueError("checks must contain readability, unity_style_structure, function_calls and redundant_temporaries")
    for value in checks.values():
        if not isinstance(value, str) or not value.strip() or len(value) > 4000:
            raise ValueError("Each review check needs concise source-specific evidence (1-4000 characters)")
    for values in (findings, next_actions):
        if not isinstance(values, list) or len(values) > 32 or any(
            not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in values
        ):
            raise ValueError("Review findings and next_actions must contain at most 32 nonempty short strings")
    if not isinstance(blocker, str) or len(blocker) > 4000:
        raise ValueError("blocker must be a short explanation")
    if not isinstance(no_change_reason, str) or len(no_change_reason) > 4000:
        raise ValueError("no_change_reason must be a short explanation")
    if verdict == "complete" and (findings or next_actions or blocker.strip()):
        raise ValueError("A complete review cannot retain unresolved findings, actions or a blocker")
    if (verdict == "complete" and snapshot.get("taskSourceChanged", snapshot.get("changed")) is not True
            and not no_change_reason.strip()):
        raise ValueError("No source organization change is present; a complete unchanged review requires no_change_reason and must not claim a new rewrite")
    if verdict == "needs_work" and (not findings or not next_actions):
        raise ValueError("needs_work requires concrete findings and next_actions")
    if verdict == "blocked" and not blocker.strip():
        raise ValueError("blocked requires the actual reason work cannot continue")
    if require_scope and verdict == "complete" and scope_review is None:
        raise ValueError("scope_review is required: assess the planned whole-source functions and branches before claiming completion")
    coverage = (_scope_review(snapshot, scope_review, verdict, require_observations=require_scope)
                if scope_review is not None else None)
    overview_receipt = None
    if prepared_overview is not None:
        from .shader_workspace.semantic_overview import reviewed_overview_state
        _, source_bytes = _read_bytes(Path(snapshot["workspacePath"]), snapshot["sourcePath"], 4 * 1024 * 1024)
        overview_receipt = reviewed_overview_state(source_bytes.decode("utf-8"), prepared_overview)
        if not overview_receipt["complete"]:
            raise ValueError(overview_receipt["reason"])
        overview_receipt["path"] = str(Path(snapshot["workspacePath"]) / "ai" / "semantic-overview.json")
    if require_overview and verdict == "complete" and overview_receipt is None:
        raise ValueError("semantic_overview with an independent current-source calculation review is required; use get_shader_semantic_overview_context to inspect existing evidence")
    return {
        "schema": REVIEW_SCHEMA, **snapshot, "verdict": verdict,
        "checks": dict(checks), "findings": list(findings),
        "nextActions": list(next_actions), "blocker": blocker,
        "noChangeReason": no_change_reason,
        "scopeReview": coverage,
        "semanticOverview": overview_receipt,
        "author": "agent", "hostSemanticApproval": False,
    }


def track_result(
    targets: dict[str, dict[str, Any]], name: str, payload: Any,
    arguments: Mapping[str, Any], failed: bool,
    *, read_epoch: str = "",
) -> None:
    from .shader_overview_delivery import observe_display
    observe_display(targets, name, payload, arguments)
    if failed or not isinstance(payload, Mapping) or payload.get("error") or payload.get("ok") is False:
        return
    if name == "read_shader_workspace_file":
        if (payload.get("mode") != "file" and isinstance(payload.get("text"), str)
                and len(json.dumps(payload, ensure_ascii=True, default=str).encode("utf-8")) <= 10 * 1024):
            coverage = payload.get("coverage") or {}
            if coverage.get("returnedBytes") == len(payload["text"].encode("utf-8")):
                from .shader_source_reads import observe
                workspace = str(Path(str(payload.get("workspacePath") or "")).resolve()).replace("\\", "/").casefold()
                observe(targets, payload, workspace=workspace, epoch=read_epoch)
        return
    if name in _SEMANTIC_OBSERVATION_TOOLS:
        # A normal status/compile/apply route may run after finish (including
        # recovery of the same draft). Update only an existing organization:
        # inspecting a workspace never starts an unsolicited rewrite task.
        observed = payload.get("result", payload)
        if not isinstance(observed, Mapping):
            return
        workspace = str(observed.get("workspacePath") or arguments.get("workspace_path") or "")
        semantic = observed.get("cbufferSemanticDelivery")
        if not workspace or len(workspace) > 1024 or not isinstance(semantic, Mapping) or type(semantic.get("required")) is not bool:
            return
        key = str(Path(workspace).resolve()).replace("\\", "/").casefold()
        prior = targets.get(key)
        if not isinstance(prior, Mapping):
            return
        target = dict(prior)
        target["cbufferSemanticDelivery"] = _semantic_contract(semantic, prior)
        if name != "get_shader_workspace_status":
            target["sourceWorkObserved"] = True
        targets[key] = target
        return
    is_review = name in {"record_shader_organization_review", "update_shader_semantic_overview"}
    if name not in _ORGANIZATION_TOOLS and not is_review:
        return
    if name == "write_shader_workspace_organization_file" and (arguments.get("kind") or payload.get("kind")) != "proposal":
        return
    if name == "restructure_shader_workspace_source" and str(arguments.get("operation") or "").strip().lower() == "list":
        return
    workspace = str(payload.get("workspacePath") or arguments.get("workspace_path") or "")
    organization = str(payload.get("organizationId") or arguments.get("organization_id") or "")
    if not workspace or len(workspace) > 1024 or not re.fullmatch(r"[0-9a-f]{32}", organization):
        return
    key = str(Path(workspace).resolve()).replace("\\", "/").casefold()
    prior = targets.get(key, {})
    target = dict(prior) if prior.get("organizationId") == organization else {}
    # Opening a draft can be used to inspect its source. Keep that distinct
    # from actually undertaking source work in this task, so an explanatory
    # follow-up does not acquire an unintended rewrite obligation. Missing
    # provenance in old ledgers remains unknown rather than guessed read-only.
    if name == "begin_shader_workspace_organization":
        if not prior:
            target["sourceWorkObserved"] = False
        elif "sourceWorkObserved" in prior:
            target["sourceWorkObserved"] = prior["sourceWorkObserved"]
    elif name != "begin_shader_workspace_organization":
        target["sourceWorkObserved"] = True
    target.update(workspacePath=workspace, organizationId=organization)
    if read_epoch and name == "begin_shader_workspace_organization":
        from .shader_source_reads import begin
        try:
            snapshot = source_snapshot(workspace, organization)
            size = Path(snapshot["sourcePath"]).stat().st_size
        except (OSError, ValueError, KeyError):
            snapshot = {"sourceSha256": str(payload.get("proposalSourceSha256") or ""),
                        "sourcePath": str(payload.get("proposalSourcePath") or "")}
            size = 0
        begin(target, read_epoch, snapshot, size)
    elif name in {"write_shader_workspace_organization_file", "restructure_shader_workspace_source"}:
        from .shader_source_reads import advance
        if target.get("sourceReadCoverage"):
            snapshot = source_snapshot(workspace, organization)
            previous = str(payload.get("previousProposalSha256") or arguments.get("expected_proposal_sha256") or "")
            advance(target, previous, snapshot, Path(snapshot["sourcePath"]).stat().st_size)
    semantic_delivery = payload.get("cbufferSemanticDelivery")
    if isinstance(semantic_delivery, Mapping) and type(semantic_delivery.get("required")) is bool:
        # This obligation is supplied by the actual organization tool, not
        # guessed from the Agent's prose criteria or from a capability lookup.
        # Once required in this task, a later receipt cannot silently drop it.
        target["cbufferSemanticDelivery"] = _semantic_contract(semantic_delivery, prior)
    if "initialSourceSha256" not in target:
        if name == "begin_shader_workspace_organization":
            initial_sha = str(payload.get("proposalSourceSha256") or "")
        elif name in {"write_shader_workspace_organization_file", "restructure_shader_workspace_source"}:
            initial_sha = str(payload.get("previousProposalSha256") or arguments.get("expected_proposal_sha256") or "")
        else:
            initial_sha = ""
        if not re.fullmatch(r"[0-9a-f]{64}", initial_sha):
            try:
                initial_sha = source_snapshot(workspace, organization)["sourceSha256"]
            except (OSError, ValueError, KeyError, TypeError):
                initial_sha = ""
        if re.fullmatch(r"[0-9a-f]{64}", initial_sha):
            target["initialSourceSha256"] = initial_sha
    if is_review and payload.get("schema") == REVIEW_SCHEMA:
        target["review"] = dict(payload)
        if read_epoch:
            target["sourceReviewEpoch"] = read_epoch
    targets[key] = target


def review_state(targets: Mapping[str, Any]) -> dict[str, Any]:
    rows = []
    for target in targets.values():
        if not isinstance(target, Mapping):
            continue
        row = dict(target)
        review = row.get("review")
        try:
            row.update(source_snapshot(str(row["workspacePath"]), str(row["organizationId"])))
            from .shader_source_reads import state as source_read_state
            row["sourceReadState"] = source_read_state(row, row["sourceSha256"])
            # Old/missing ledger hints cannot exempt a fresh source containing
            # resources. Recompute cheap local obligations from the exact bytes.
            from .shader_semantic_delivery import delivery_state
            row["cbufferSemanticDelivery"] = delivery_state(
                row["workspacePath"], row["sourceSha256"], row.get("cbufferSemanticDelivery") or {},
                source_path=row["sourcePath"])
            initial_sha = str(row.get("initialSourceSha256") or "")
            row["taskSourceChanged"] = bool(initial_sha and row["sourceSha256"] != initial_sha)
            review_valid = False
            if isinstance(review, Mapping):
                try:
                    if (review.get("schema") != REVIEW_SCHEMA or review.get("author") != "agent"
                            or review.get("hostSemanticApproval") is not False
                            or review.get("organizationId") != row["organizationId"]
                            or Path(str(review.get("workspacePath") or "")).resolve() != Path(row["workspacePath"])):
                        raise ValueError("Stored Shader self-review identity is invalid")
                    make_review(row, str(review.get("sourceSha256") or ""), str(review.get("verdict") or ""),
                                review.get("checks"), review.get("findings"), review.get("nextActions"),
                                review.get("blocker", ""), review.get("noChangeReason", ""), review.get("scopeReview"))
                    review_valid = True
                except (OSError, ValueError, TypeError):
                    row["diagnostic"] = "Stored Shader self-review is stale or incomplete"
            row["overallChanged"] = row["changed"]
            row["changed"] = row["taskSourceChanged"]
            row["fresh"] = review_valid and not row["sourceConflict"] and row["sourceReadState"]["complete"]
            if not row["sourceReadState"]["complete"]:
                row["diagnostic"] = "Current organization requires a complete source read in this task; copying a file or an old review does not count"
            scope = review.get("scopeReview") if isinstance(review, Mapping) else None
            row["scopeCoverageComplete"] = bool(review_valid and isinstance(scope, Mapping)
                                                 and "observation_dispositions" in scope)
            if review_valid and not row["scopeCoverageComplete"]:
                row["diagnostic"] = ("Source review lacks current surfaced-candidate dispositions; retain valid scope evidence and assess these concrete source observations"
                                     if scope else "Legacy source review has no structured whole-source scope coverage; retain prior evidence and assess omitted scopes")
            from .shader_workspace.semantic_overview import reviewed_overview_state
            overview = None
            try:
                _, overview_bytes = _read_bytes(Path(row["workspacePath"]), "ai/semantic-overview.json", 256 * 1024)
                overview = json.loads(overview_bytes)
            except (OSError, ValueError):
                pass
            _, source_bytes = _read_bytes(Path(row["workspacePath"]), row["sourcePath"], 4 * 1024 * 1024)
            overview_receipt = review.get("semanticOverview") if isinstance(review, Mapping) else None
            overview_state = reviewed_overview_state(source_bytes.decode("utf-8"), overview, overview_receipt or {})
            row["semanticOverviewState"] = overview_state
            row["semanticOverviewComplete"] = bool(review_valid and overview_state["complete"])
            display = row.get("semanticOverviewDisplay") or {}
            row["semanticOverviewDisplayComplete"] = bool(
                row.get("semanticOverviewDisplayRequired") is not True or (
                    display.get("complete") is True and display.get("state") == "reviewed"
                    and display.get("sourceSha256") == row["sourceSha256"]
                    and display.get("overviewSha256") == overview_state.get("overviewSha256")))
            if review_valid and row["scopeCoverageComplete"] and not row["semanticOverviewComplete"]:
                row["diagnostic"] = overview_state.get("reason", "Independent calculation overview has not been reviewed")
            elif row["semanticOverviewComplete"] and not row["semanticOverviewDisplayComplete"]:
                row["diagnostic"] = ("Show this saved Shader source and read get_shader_workspace_ui_state to verify its reviewed calculation directory; do not repeat compilation, ABI validation or finish")
        except (OSError, ValueError, KeyError, TypeError) as error:
            row.update(fresh=False, changed=False, diagnostic=str(error)[:800])
        rows.append(row)
    return {"schema": STATE_SCHEMA, "targets": rows}


def publication_review(workspace: str, organization: str) -> dict[str, Any]:
    """Check the Agent's source assessment before costly final publication.

    This is deliberately independent of compile/apply evidence: source quality
    is assessed first, physical validation follows. Intermediate diagnostics can
    explicitly use the checkpoint publication mode without closing the draft.
    """
    snapshot = source_snapshot(workspace, organization)
    root = Path(snapshot["workspacePath"])
    review: dict[str, Any] = {}
    state: dict[str, Any] = {}
    diagnostic = "A current complete Agent source self-review is required before final validation/apply"
    try:
        _path, raw = _read_bytes(root, snapshot["reviewPath"], 256 * 1024)
        value = json.loads(raw)
        if isinstance(value, dict):
            review = value
        state = review_state({"target": {
            **snapshot, "initialSourceSha256": snapshot["baselineSourceSha256"], "review": review,
        }})["targets"][0]
        if (state.get("fresh") and state.get("scopeCoverageComplete")
                and state.get("semanticOverviewComplete") and review.get("verdict") == "complete"):
            return {"ready": True, "sourceSha256": snapshot["sourceSha256"]}
        diagnostic = str(state.get("diagnostic") or diagnostic)
    except (OSError, ValueError, TypeError):
        pass
    return {
        "ready": False, "sourceSha256": snapshot["sourceSha256"],
        "reviewVerdict": str(review.get("verdict") or "missing"), "reason": diagnostic,
        "findings": review.get("findings", []), "nextActions": review.get("nextActions", []),
        "metadataOnlyRepair": bool(state.get("fresh") and state.get("scopeCoverageComplete")
                                   and not state.get("semanticOverviewComplete")),
        "semanticOverviewState": state.get("semanticOverviewState"),
    }
