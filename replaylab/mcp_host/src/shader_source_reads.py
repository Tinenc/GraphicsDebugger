"""Task-scoped evidence of delivered Shader bytes, not semantic understanding.

Only Host source reads and runtime-observed owned-file reads count. Copying a
file, its outline, a prior task's review or an Agent assertion never does.
"""
from collections.abc import Mapping

SCHEMA = "bread-shader-source-read-coverage/v1"
MAX_BYTES = 4 * 1024 * 1024


def bounded_read_payload(payload, text):
    """Keep the serialized page below 10 KiB, including escaped text/metadata.

    Even a second JSON string encoding plus its connector envelope stays below
    the 24 KiB shared output spill threshold. Offsets remain Unicode characters;
    coverage remains UTF-8 bytes, never estimated character counts.
    """
    import json
    start = payload["offset"]
    candidate = payload["text"]

    def page(count):
        chunk = candidate[:count]
        end = start + len(chunk)
        return {**payload, "mode": "range" if end < len(text) else payload["mode"],
            "requestedMode": payload["mode"], "text": chunk, "chars": len(chunk),
            "truncated": end < len(text), "nextOffset": end if end < len(text) else None,
            "coverage": {"byteOffset": len(text[:start].encode("utf-8")),
                         "returnedBytes": len(chunk.encode("utf-8")),
                         "complete": start == 0 and end == len(text)}}

    low, high = 0, len(candidate)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(page(middle), ensure_ascii=True, indent=2).encode("utf-8")) <= 10 * 1024:
            low = middle
        else:
            high = middle - 1
    result = page(low)
    if candidate and not result["text"]:
        raise ValueError("Source read metadata exceeds the inline budget; retry without an Agent-file copy")
    return result


def begin(target, epoch, snapshot, size):
    old = target.get("sourceReadCoverage") or {}
    if old.get("epoch") == epoch and old.get("sourceSha256") == snapshot["sourceSha256"]:
        return
    target["sourceReadCoverage"] = {"schema": SCHEMA, "epoch": epoch,
        "sourceSha256": snapshot["sourceSha256"], "sourcePath": snapshot["sourcePath"],
        "sizeBytes": size, "intervals": []}
    target["initialSourceSha256"] = snapshot["sourceSha256"]
    target.pop("review", None)
    target.pop("sourceReviewEpoch", None)


def observe(targets, observation, *, workspace="", epoch=""):
    if not isinstance(observation, Mapping):
        return
    coverage = observation.get("coverage")
    if not isinstance(coverage, Mapping):
        return
    start, size = coverage.get("byteOffset"), coverage.get("returnedBytes")
    total = observation.get("sizeBytes", observation.get("byteSize"))
    if (any(type(value) is not int for value in (start, size, total))
            or not 0 <= start <= start + size <= total <= MAX_BYTES or not size):
        return
    for key, target in targets.items():
        gate = target.get("sourceReadCoverage")
        if (not isinstance(gate, dict) or (workspace and key != workspace)
                or (epoch and gate.get("epoch") != epoch)):
            continue
        if gate.get("sourceSha256") != observation.get("sha256"):
            # An external edit cannot inherit old intervals, but an actual read
            # of the new current bytes must be able to repair that stale state.
            from .shader_organization_review import source_snapshot
            try:
                snapshot = source_snapshot(target["workspacePath"], target["organizationId"])
                if snapshot["sourceSha256"] == observation.get("sha256"):
                    advance(target, "", snapshot, total)
                    gate = target["sourceReadCoverage"]
            except (OSError, ValueError, KeyError):
                continue
        if gate.get("sourceSha256") != observation.get("sha256") or gate.get("sizeBytes") != total:
            continue
        intervals = list(gate.get("intervals") or []) + [[start, start + size]]
        merged = []
        for first, last in sorted(intervals):
            if merged and first <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], last)
            else:
                merged.append([first, last])
        # Bound fragmented/hostile reads without letting them imply completion.
        gate["intervals"] = merged[:4096]


def state(target, source_sha):
    gate = target.get("sourceReadCoverage")
    if not isinstance(gate, Mapping):
        return {"required": False, "complete": True}
    total = gate.get("sizeBytes", 0)
    current = gate.get("sourceSha256") == source_sha
    intervals = gate.get("intervals", []) if current else []
    missing, position = [], 0
    for first, last in intervals:
        if first > position:
            missing.append([position, first])
        position = max(position, last)
    if position < total:
        missing.append([position, total])
    return {"required": True, "complete": bool(current and total and not missing),
        "sourceSha256": source_sha, "readSourceSha256": gate.get("sourceSha256"),
        "sizeBytes": total, "missingByteRanges": missing[:64],
        "missingRangeCount": len(missing), "sourcePath": gate.get("sourcePath"),
        "reason": "source-read-incomplete" if current else "source-read-identity-changed"}


def advance(target, previous_sha, snapshot, size):
    gate = target.get("sourceReadCoverage")
    if not isinstance(gate, dict) or gate.get("sourceSha256") == snapshot["sourceSha256"]:
        return
    # A complete read plus an observed same-epoch CAS edit covers an authored
    # revision. Untracked/external changes never inherit that evidence.
    inherited = previous_sha == gate.get("sourceSha256") and state(target, previous_sha)["complete"]
    target["sourceReadCoverage"] = {**gate, "sourceSha256": snapshot["sourceSha256"],
        "sourcePath": snapshot["sourcePath"], "sizeBytes": size,
        "intervals": [[0, size]] if inherited else [],
        "readBasisSha256": gate.get("readBasisSha256", previous_sha) if inherited else ""}


def require_current(store, workspace, organization, source_sha, *, require_begin=False, require_review=False):
    if not getattr(store, "enabled", False):
        return
    from pathlib import Path
    rows = store.shader_organization_review_state().get("targets", [])
    for row in rows:
        if row.get("organizationId") == organization and Path(row["workspacePath"]).resolve() == Path(workspace).resolve():
            current = state(row, source_sha)
            if require_begin and not current["required"]:
                break
            if current["required"]:
                epoch = store.shader_source_read_epoch()
                if row["sourceReadCoverage"].get("epoch") != epoch:
                    raise ValueError("Shader source read evidence belongs to another task execution; begin this organization in the current task")
            if current["required"] and not current["complete"]:
                import json
                from .shader_organization_review import _read_bytes
                root = Path(workspace).resolve()
                path, raw = _read_bytes(root, row["sourcePath"], MAX_BYTES)
                first = current["missingByteRanges"][0][0] if current["missingByteRanges"] else 0
                if current["readSourceSha256"] != source_sha:
                    first = 0
                offset = len(raw[:first].decode("utf-8"))
                arguments = {"workspace_path": str(root), "relative_path": path.relative_to(root).as_posix(),
                             "mode": "range", "offset": offset, "limit": 6000}
                raise ValueError("Current organization requires a full source read in this task. "
                    "Call read_shader_workspace_file with " + json.dumps(arguments, ensure_ascii=False)
                    + "; follow returned nextOffset (characters) until null. Owned-file reads with runtime coverage "
                    "also count. Copy/file mode and old reviews do not count.")
            if require_begin or (require_review and current["required"]):
                review = row.get("review") or {}
                if (row.get("sourceReviewEpoch") != epoch or review.get("sourceSha256") != source_sha
                        or review.get("organizationId") != organization or review.get("verdict") != "complete"
                        or not row.get("fresh") or not row.get("scopeCoverageComplete")
                        or not row.get("semanticOverviewComplete")):
                    raise ValueError("Record a new complete source assessment in this task with "
                                     "record_shader_organization_review before finalizing; a persisted review "
                                     "from an earlier request does not prove this reorganization was reassessed.")
            return
    if require_begin or (require_review and store.shader_source_read_required(workspace)):
        raise ValueError("Begin this organization in the current task with begin_shader_workspace_organization, "
                         "read its current source using returned nextOffset pages, then assess the source again. "
                         "An earlier task's completed organization review cannot finalize a new organization request.")
