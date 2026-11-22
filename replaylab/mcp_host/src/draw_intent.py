"""What a draw is probably drawing: recorded, revised, and never decided by this file.

An earlier version of this module scored a table of signals and announced an answer.
That was the wrong shape. A weighted table is a classifier wearing a hypothesis's
clothes: it cannot look at the picture, it does not know this game, and once it says
"GBuffer" the reading that follows bends to fit. Deciding is the model's job — it can
see the render target before and after the draw, read the marker the engine wrote,
and hold ten kinds of evidence at once.

So what lives here is the *record*, not the judgement:

* an intent is free text, written by whoever knows — the user, or the model after
  looking. There is no enum, because a draw can be "the stockings", "the second
  specular pass on hair", or something this codebase has never heard of.
* it is scoped to what it applies to — this capture, these event ids, this shader —
  so it can be found again and so it is obvious when it stops applying.
* it is revised, not duplicated. Recording over an existing scope supersedes it and
  keeps the history; the same shader seen at another event id joins the record it
  already has instead of starting a second one.
* checks against it are deduplicated. Confirming the same expectation twice is
  wasted work, and a contradiction downgrades the record rather than being quietly
  dropped.

A recorded intent is guidance, never authority. It orders which search terms to try
and what to expect while reading a shader. It never settles an identification, and
evidence that contradicts it wins.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "replaylab-draw-intents/v1"
INTENTS_FILENAME = "draw-intents.json"

MAX_RECORDS = 512
MAX_EVENT_IDS = 64
MAX_TERMS = 32
MAX_CHECKS = 32
MAX_TEXT = 512
MAX_INTENT = 200

# Deliberately coarse. A finer scale would invite arguing about the difference between
# 0.6 and 0.7 confidence in something nobody measured.
CONFIDENCE_ORDER = ("contradicted", "working", "corroborated", "user-stated")
CHECK_OUTCOMES = ("found", "absent", "contradicted", "inconclusive")


class DrawIntentError(ValueError):
    """A record that would not mean anything if it were stored."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any, label: str, *, limit: int = MAX_TEXT, required: bool = False) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[:limit]
    if required and not text:
        raise DrawIntentError(f"{label} is required")
    return text


def _terms(values: Any, limit: int = MAX_TERMS) -> list[str]:
    if isinstance(values, (str, bytes)):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    seen: list[str] = []
    for item in values:
        text = _text(item, "term", limit=128)
        if text and text not in seen:
            seen.append(text)
    return seen[:limit]


def check_key(expected: str) -> str:
    """Two checks that say the same thing are one check.

    Verification is the expensive half of this loop, so the same expectation asked a
    second time returns what it answered the first time.
    """

    normalized = re.sub(r"[^a-z0-9]+", " ", str(expected or "").lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def scope_identity(capture_path: str = "", shader_fingerprint: str = "",
                   event_id: int = 0) -> str:
    """What this record is about, stable enough to find again.

    The shader is the better key: the same shader drawn at a different event id is the
    same thing being drawn. An event id alone is the fallback when nothing else is
    known, and it does not survive a recapture — which is why a record says which
    capture it came from.
    """

    if shader_fingerprint:
        basis = "shader:%s" % str(shader_fingerprint).strip().lower()
    else:
        basis = "capture:%s|event:%d" % (str(capture_path or "").strip().lower(),
                                         int(event_id or 0))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def default_intents_path(project_directory: Path | str | None = None) -> Path:
    if project_directory is not None:
        return Path(project_directory) / INTENTS_FILENAME
    local = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
    if local:
        root = Path(local).expanduser()
    else:
        # Never the working directory: the Host is started from wherever the UI
        # happened to be, so a relative root scatters one project's intents across
        # several folders and loses them on the next launch.
        xdg = str(os.environ.get("XDG_DATA_HOME", "") or "").strip()
        root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return root / "ReplayLab" / "config" / INTENTS_FILENAME


def validate_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    intent = _text(raw.get("intent"), "intent", limit=MAX_INTENT, required=True)
    confidence = _text(raw.get("confidence") or "working", "confidence", limit=32).lower()
    if confidence not in CONFIDENCE_ORDER:
        raise DrawIntentError("confidence must be one of: " + ", ".join(CONFIDENCE_ORDER))
    source = _text(raw.get("source") or "ai", "source", limit=16).lower()
    if source not in {"user", "ai"}:
        raise DrawIntentError("source must be 'user' or 'ai'")
    event_ids: list[int] = []
    for value in (raw.get("eventIds") or []):
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in event_ids:
            event_ids.append(number)
    checks: list[dict[str, Any]] = []
    for item in list(raw.get("checks") or [])[:MAX_CHECKS]:
        if not isinstance(item, Mapping):
            continue
        expected = _text(item.get("expected"), "check expectation", required=True)
        outcome = _text(item.get("outcome") or "inconclusive", "check outcome", limit=32).lower()
        if outcome not in CHECK_OUTCOMES:
            raise DrawIntentError("check outcome must be one of: " + ", ".join(CHECK_OUTCOMES))
        checks.append({
            "key": check_key(expected),
            "expected": expected,
            "outcome": outcome,
            "note": _text(item.get("note"), "check note"),
            "at": _text(item.get("at") or _now(), "check time", limit=64),
        })
    return {
        "id": _text(raw.get("id"), "record id", limit=64) or scope_identity(
            str(raw.get("capturePath", "")), str(raw.get("shaderFingerprint", "")),
            event_ids[0] if event_ids else 0),
        "intent": intent,
        "confidence": confidence,
        "source": source,
        "because": _terms(raw.get("because"), limit=12),
        "searchTerms": _terms(raw.get("searchTerms")),
        "algorithms": _terms(raw.get("algorithms")),
        "capturePath": _text(raw.get("capturePath"), "capture path", limit=1024),
        "shaderFingerprint": _text(raw.get("shaderFingerprint"), "shader fingerprint", limit=128),
        "targetSignature": _text(raw.get("targetSignature"), "target signature", limit=256),
        "markerPath": _text(raw.get("markerPath"), "marker path", limit=512),
        "eventIds": event_ids[:MAX_EVENT_IDS],
        "checks": checks,
        "revision": max(1, int(raw.get("revision") or 1)),
        "supersededIntent": _text(raw.get("supersededIntent"), "superseded intent",
                                  limit=MAX_INTENT),
        "recordedAt": _text(raw.get("recordedAt") or _now(), "record time", limit=64),
    }


def load_records(path: Path | str) -> tuple[list[dict[str, Any]], list[str]]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError) as error:
        return [], [f"draw intents unreadable: {error}"[:400]]
    if not isinstance(payload, Mapping):
        return [], ["draw intents file is not an object"]
    records: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for raw in list(payload.get("intents") or [])[:MAX_RECORDS]:
        if not isinstance(raw, Mapping):
            continue
        try:
            records.append(validate_record(raw))
        except DrawIntentError as error:
            diagnostics.append(f"record dropped: {error}"[:400])
    return records, diagnostics[:32]


def save_records(records: Sequence[Mapping[str, Any]], path: Path | str) -> Path:
    target = Path(path)
    validated = [validate_record(record) for record in records][:MAX_RECORDS]
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps({"schema": SCHEMA, "intents": validated}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.replace(temporary, target)
    return target


def find_record(records: Sequence[Mapping[str, Any]], *, capture_path: str = "",
                shader_fingerprint: str = "", event_id: int = 0) -> dict[str, Any] | None:
    """The record this draw already has, by shader first and event id second."""

    fingerprint = str(shader_fingerprint or "").strip().lower()
    if fingerprint:
        for record in records:
            if str(record.get("shaderFingerprint", "")).lower() == fingerprint:
                return dict(record)
    if event_id:
        capture = str(capture_path or "").strip().lower()
        for record in records:
            if int(event_id) not in (record.get("eventIds") or []):
                continue
            if capture and str(record.get("capturePath", "")).lower() not in {"", capture}:
                continue
            return dict(record)
    return None


def upsert_record(raw: Mapping[str, Any], path: Path | str) -> dict[str, Any]:
    """Record an intent, revising what is already there rather than duplicating it."""

    record = validate_record(raw)
    records, _ = load_records(path)
    existing = find_record(records, capture_path=record["capturePath"],
                           shader_fingerprint=record["shaderFingerprint"],
                           event_id=record["eventIds"][0] if record["eventIds"] else 0)
    if existing is not None:
        record["id"] = existing["id"]
        # The same shader seen at another event id joins the record it already has.
        merged = list(existing.get("eventIds") or [])
        for number in record["eventIds"]:
            if number not in merged:
                merged.append(number)
        record["eventIds"] = merged[:MAX_EVENT_IDS]
        if existing.get("intent") != record["intent"]:
            record["revision"] = int(existing.get("revision") or 1) + 1
            record["supersededIntent"] = existing.get("intent", "")
            # A revised intent starts its checks again: they were about the old claim.
            record["checks"] = [item for item in record["checks"]]
        else:
            record["revision"] = int(existing.get("revision") or 1)
            record["supersededIntent"] = existing.get("supersededIntent", "")
            record["checks"] = _merge_checks(existing.get("checks") or [], record["checks"])
        for key in ("shaderFingerprint", "targetSignature", "markerPath", "capturePath"):
            if not record[key]:
                record[key] = existing.get(key, "")
    records = [item for item in records if item["id"] != record["id"]]
    if len(records) >= MAX_RECORDS:
        records = records[-(MAX_RECORDS - 1):]
    records.append(record)
    save_records(records, path)
    return record


def _merge_checks(existing: Sequence[Mapping[str, Any]],
                  incoming: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged = {item["key"]: dict(item) for item in existing if isinstance(item, Mapping)
              and item.get("key")}
    for item in incoming:
        merged[item["key"]] = dict(item)
    return list(merged.values())[:MAX_CHECKS]


def apply_checks(record: Mapping[str, Any],
                 checks: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Fold verification results in, skipping anything already answered.

    Returns the updated record and the keys that were repeats. A contradiction always
    lowers confidence: a label kept after the evidence went against it is worse than
    no label, because everything downstream keeps trusting it.
    """

    updated = dict(record)
    stored = {item["key"]: dict(item) for item in (record.get("checks") or [])}
    repeats: list[str] = []
    contradicted = False
    corroborated = False
    for item in checks:
        expected = _text(item.get("expected"), "check expectation", required=True)
        key = check_key(expected)
        outcome = _text(item.get("outcome") or "inconclusive", "check outcome", limit=32).lower()
        if outcome not in CHECK_OUTCOMES:
            raise DrawIntentError("check outcome must be one of: " + ", ".join(CHECK_OUTCOMES))
        if key in stored and stored[key].get("outcome") == outcome:
            repeats.append(expected)
            continue
        stored[key] = {"key": key, "expected": expected, "outcome": outcome,
                       "note": _text(item.get("note"), "check note"), "at": _now()}
        if outcome == "contradicted":
            contradicted = True
        elif outcome == "found":
            corroborated = True
    updated["checks"] = list(stored.values())[:MAX_CHECKS]
    if contradicted:
        updated["confidence"] = "contradicted"
    elif corroborated and updated.get("confidence") == "working":
        updated["confidence"] = "corroborated"
    return updated, repeats


__all__ = [
    "CHECK_OUTCOMES",
    "CONFIDENCE_ORDER",
    "DrawIntentError",
    "INTENTS_FILENAME",
    "SCHEMA",
    "apply_checks",
    "check_key",
    "default_intents_path",
    "find_record",
    "load_records",
    "save_records",
    "scope_identity",
    "upsert_record",
    "validate_record",
]
