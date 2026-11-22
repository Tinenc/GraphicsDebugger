"""Work done on one Shader, kept where the next capture of the same game can find it.

Organizing a Shader is expensive and the result is about the Shader, not about the
capture it was first seen in. Byte-identical Shader programs can reuse organized
source as a candidate under the existing validation path. CBuffer names, aliases,
texture identities and draw intents also depend on the current material and bound
resources; their stored evidence must be checked before reusing those conclusions.

The library lives in the project, at ``shared/shaders``, and is keyed by the captured
Shader's own ``rawSha256``. A second index maps ``interfaceHash`` to the same entries,
which is what answers "the game was rebuilt, is any of this still usable" -- see
``shader_reuse`` for what each physical tier establishes and what remains to verify.

Entries are additive and provenance-carrying: every one records which capture and
workspace produced it and when. Nothing here overwrites a body without being told to,
because a later capture is not automatically a better one.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = "replaylab-shared-shader-library/v1"
ENTRY_SCHEMA = "replaylab-shared-shader-entry/v1"
INDEX_FILENAME = "index.json"
ENTRY_FILENAME = "entry.json"

# What an entry may carry. Each is the product of a different piece of work, and a
# reuse decision is made per part. Storing or finding names is not evidence that they
# describe the current material; matching physical ABI alone cannot establish that.
PARTS = ("organizedSource", "cbufferNames", "aliasTable", "drawIntents",
         "algorithmGraph", "sourceFingerprint", "notes")

MAX_ENTRIES = 20000
MAX_PART_BYTES = 8 * 1024 * 1024
MAX_TEXT = 2000


class SharedShaderLibraryError(RuntimeError):
    """The library refused a read or a write, with the reason in the message."""


def library_root(project_root: str | Path) -> Path:
    """Where the library lives inside a project."""

    return Path(project_root).expanduser().resolve() / "shared" / "shaders"


def _hex64(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise SharedShaderLibraryError("%s must be a lowercase SHA-256" % label)
    return text


def _text(value: Any, label: str, *, limit: int = MAX_TEXT, required: bool = False) -> str:
    text = " ".join(str(value or "").split()) if value is not None else ""
    if required and not text:
        raise SharedShaderLibraryError("%s is required" % label)
    if len(text) > limit:
        raise SharedShaderLibraryError("%s is longer than %d characters" % (label, limit))
    return text


def _entry_directory(root: Path, raw_sha256: str) -> Path:
    # Two levels of fan-out: a project with thousands of Shaders should not put
    # thousands of directories in one folder, which is slow to list on Windows.
    return root / raw_sha256[:2] / raw_sha256


def validate_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Check one entry's identity and provenance before it is written."""

    raw_sha256 = _hex64(entry.get("rawSha256"), "rawSha256")
    record: dict[str, Any] = {
        "schema": ENTRY_SCHEMA,
        "schemaVersion": 1,
        "rawSha256": raw_sha256,
        "stage": _text(entry.get("stage"), "stage", limit=64),
        "entryPoint": _text(entry.get("entryPoint"), "entryPoint", limit=256),
    }
    for optional, label in (("codeSha256", "codeSha256"),
                            ("interfaceHash", "interfaceHash"),
                            ("layoutHash", "layoutHash")):
        value = str(entry.get(optional, "") or "").strip().lower()
        if value:
            record[optional] = _hex64(value, label)
    # Scope, so a library shared between games never offers one game's work for
    # another's Shader. Empty is allowed and simply means "not stated".
    record["gameId"] = _text(entry.get("gameId"), "gameId", limit=128)
    record["buildId"] = _text(entry.get("buildId"), "buildId", limit=128)
    provenance = entry.get("provenance") if isinstance(entry.get("provenance"), Mapping) else {}
    record["provenance"] = {
        "capturePath": _text(provenance.get("capturePath"), "capturePath", limit=1024),
        "workspaceId": _text(provenance.get("workspaceId"), "workspaceId", limit=128),
        "eventIds": [int(value) for value in list(provenance.get("eventIds") or [])[:64]
                     if not isinstance(value, bool) and str(value).lstrip("-").isdigit()],
        "recordedAt": _text(provenance.get("recordedAt"), "recordedAt", limit=64)
                      or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    parts: dict[str, Any] = {}
    for name in PARTS:
        if name not in entry or entry[name] is None:
            continue
        value = entry[name]
        encoded = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_PART_BYTES:
            raise SharedShaderLibraryError(
                "%s is larger than the %d byte library limit" % (name, MAX_PART_BYTES))
        parts[name] = value
    if not parts:
        raise SharedShaderLibraryError(
            "an entry with no parts stores nothing; supply at least one of: %s"
            % ", ".join(PARTS))
    record["parts"] = parts
    record["partNames"] = sorted(parts)
    return record



@contextlib.contextmanager
def _library_lock(root: Path):
    """Serialise read-modify-write of the shared files across processes.

    ``_atomic_write`` makes each replace atomic, which is not the same thing: two
    RenderDoc windows on one project both read the index, both add their entry, and
    the second write loses the first. The library is explicitly shared, so the pair
    has to be held together.

    A lock that cannot be taken in time is an error, not a licence to proceed:
    writing unlocked is precisely the lost update this exists to prevent, and the
    caller can retry, whereas an index entry that vanished cannot be noticed.
    """

    root.mkdir(parents=True, exist_ok=True)
    try:
        handle = open(root / ".lock", "a+b")
    except OSError as error:
        # No lock file means no serialisation; writing anyway is the lost update
        # this exists to prevent.
        raise SharedShaderLibraryError(
            "the shared Shader library lock could not be opened: %s" % error) from error
    try:
        if handle is not None:
            deadline = time.monotonic() + _LOCK_SECONDS
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise SharedShaderLibraryError(
                            "another RenderDoc instance is writing to the shared "
                            "Shader library; try again in a moment")
                    time.sleep(0.02)
        yield
    finally:
        if handle is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, str(path))
    except BaseException:
        # A half-written temporary must never be left behind to be mistaken for an
        # entry; the original failure is what the caller needs to see, so re-raise it.
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def publish_entry(project_root: str | Path, entry: Mapping[str, Any], *,
                  replace_parts: bool = False) -> dict[str, Any]:
    """Store one Shader's work, merging with whatever is already there.

    Parts are merged by default rather than replaced: a capture that only produced
    CBuffer names must not silently drop the organized source someone spent an hour
    on. ``replace_parts`` is the explicit way to overwrite, for the case where the new
    work is genuinely better.
    """

    record = validate_entry(entry)
    root = library_root(project_root)
    directory = _entry_directory(root, record["rawSha256"])
    with _library_lock(root):
        return _publish_locked(root, directory, record, replace_parts=replace_parts)


def _publish_locked(root: Path, directory: Path, record: dict[str, Any], *,
                    replace_parts: bool) -> dict[str, Any]:
    existing = _read_json(directory / ENTRY_FILENAME)
    if existing and not replace_parts:
        merged = dict(existing.get("parts") or {})
        merged.update(record["parts"])
        record["parts"] = merged
        record["partNames"] = sorted(merged)
        # Identity is carried forward the same way the parts are. A publish that only
        # adds a note states no interface hash, and rebuilding the record from that
        # call alone silently erased what this Shader *is* -- which then made it
        # invisible to every interface-tier lookup, so the next build reported it as
        # new work. Anything the caller does state still wins.
        for key in ("codeSha256", "interfaceHash", "layoutHash", "gameId", "buildId",
                    "stage", "entryPoint"):
            if not record.get(key) and existing.get(key):
                record[key] = existing[key]
        history = list(existing.get("history") or [])[-31:]
        history.append(existing.get("provenance") or {})
        record["history"] = history
    _atomic_write(directory / ENTRY_FILENAME, record)
    _update_index(root, record)
    return {
        "schema": SCHEMA,
        "rawSha256": record["rawSha256"],
        "path": str(directory / ENTRY_FILENAME),
        "parts": record["partNames"],
        "merged": bool(existing) and not replace_parts,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _index_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    summary = {
        key: record.get(key, "")
        for key in ("interfaceHash", "layoutHash", "codeSha256", "stage",
                    "entryPoint", "gameId", "buildId")
        if record.get(key)
    }
    summary["parts"] = list(record.get("partNames") or sorted(record.get("parts") or {}))
    return summary


def rebuild_index(project_root: str | Path) -> dict[str, Any]:
    """Rebuild ``index.json`` from the entry files, which are the source of truth.

    The index is a cache. A truncated or half-synced index used to be read as empty,
    and the next publish then wrote a complete replacement holding one entry -- every
    other Shader's work was still on disk and invisible to every lookup, forever,
    because nothing could rebuild it. This can.
    """

    root = library_root(project_root)
    with _library_lock(root):
        index = _rebuilt_index(root)
        _atomic_write(root / INDEX_FILENAME, index)
    return {"schema": SCHEMA, "entryCount": index["entryCount"],
            "path": str(root / INDEX_FILENAME)}


def _rebuilt_index(root: Path) -> dict[str, Any]:
    """The index as the entry files say it should be. Reads only; never writes."""

    entries: dict[str, dict[str, Any]] = {}
    for entry_file in sorted(root.glob("??/*/" + ENTRY_FILENAME)):
        record = _read_json(entry_file)
        raw = str(record.get("rawSha256", "") or "").lower()
        if len(raw) != 64 or entry_file.parent.name != raw:
            continue
        entries[raw] = _index_summary(record)
        if len(entries) >= MAX_ENTRIES:
            break
    return {
        "schema": SCHEMA,
        "schemaVersion": 1,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entryCount": len(entries),
        "entries": entries,
        "rebuilt": True,
    }


def _read_index(root: Path) -> dict[str, Any]:
    """The index -- or, if it is unreadable while entries exist, what it should be.

    Readers get the rebuilt view in memory and write nothing, so a reader can never
    race a writer. The writer path (`_update_index`, under the lock) persists it.
    """

    path = root / INDEX_FILENAME
    index = _read_json(path)
    entries = index.get("entries")
    if isinstance(entries, dict):
        # An index that parses can still be the shrunk one an older publish left
        # behind (one entry, every other Shader's work invisible). The entry
        # folders are cheap to count; when they outnumber the index, the folders win.
        if len(entries) < MAX_ENTRIES and _entry_folder_count(root) > len(entries):
            return _rebuilt_index(root)
        return index
    if path.exists() or any(root.glob("??/*/" + ENTRY_FILENAME)):
        return _rebuilt_index(root)
    return {}


def _entry_folder_count(root: Path) -> int:
    """How many entry folders exist, without opening any of them."""

    count = 0
    try:
        for shard in root.iterdir():
            if len(shard.name) == 2 and shard.is_dir():
                count += sum(1 for item in shard.iterdir() if len(item.name) == 64)
    except OSError:
        return 0
    return count


def _update_index(root: Path, record: Mapping[str, Any]) -> None:
    """Keep a small index so an interface lookup does not walk the whole library."""

    index = _read_index(root)
    entries = index.get("entries") if isinstance(index.get("entries"), dict) else {}
    if len(entries) >= MAX_ENTRIES and record["rawSha256"] not in entries:
        raise SharedShaderLibraryError(
            "the shared Shader library is full (%d entries)" % MAX_ENTRIES)
    entries[record["rawSha256"]] = _index_summary(record)
    _atomic_write(root / INDEX_FILENAME, {
        "schema": SCHEMA,
        "schemaVersion": 1,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entryCount": len(entries),
        "entries": entries,
    })


def load_entry(project_root: str | Path, raw_sha256: str, *,
               parts: Iterable[str] | None = None) -> dict[str, Any] | None:
    """One entry by its Shader's bytes, or None. ``parts`` limits what is read back."""

    root = library_root(project_root)
    record = _read_json(_entry_directory(root, _hex64(raw_sha256, "rawSha256"))
                        / ENTRY_FILENAME)
    if not record:
        return None
    if parts is not None:
        wanted = set(parts)
        record["parts"] = {name: value for name, value in (record.get("parts") or {}).items()
                           if name in wanted}
        record["partNames"] = sorted(record["parts"])
    return record


def candidates(project_root: str | Path, *, interface_hash: str = "",
               stage: str = "", game_id: str = "", raw_sha256: str = "",
               limit: int = 64) -> list[dict[str, Any]]:
    """Entries worth comparing against, cheaply, from the index alone.

    This is deliberately a shortlist and not an answer: it narrows by the facts an
    index can hold, and ``shader_reuse.best_match`` decides what any of them means.
    """

    root = library_root(project_root)
    index = _read_index(root)
    entries = index.get("entries") if isinstance(index.get("entries"), dict) else {}
    wanted_interface = str(interface_hash or "").strip().lower()
    wanted_stage = str(stage or "").strip()
    wanted_game = str(game_id or "").strip()
    wanted_bytes = str(raw_sha256 or "").strip().lower()
    found: list[dict[str, Any]] = []
    # The entry with these exact bytes is the same Shader, full stop, and no weaker
    # key may filter it out: a stale or differently-computed interface hash, or a
    # stage recorded under another name, used to hide the one candidate that needed
    # no judgement at all. It also goes first, so a truncated shortlist keeps it.
    ordered = sorted(entries.items(), key=lambda item: item[0] != wanted_bytes)
    for raw_sha256, summary in ordered:
        if not isinstance(summary, Mapping):
            continue
        exact = bool(wanted_bytes) and raw_sha256 == wanted_bytes
        if not exact and wanted_game and summary.get("gameId") and summary["gameId"] != wanted_game:
            continue
        if not exact and wanted_stage and summary.get("stage") and summary["stage"] != wanted_stage:
            continue
        if not exact and wanted_interface and summary.get("interfaceHash") != wanted_interface:
            # Still a candidate: the bytes may match even when the index has no
            # interface hash recorded for this entry.
            if summary.get("interfaceHash"):
                continue
        candidate = {"entryId": raw_sha256, "rawSha256": raw_sha256}
        candidate.update({key: value for key, value in summary.items() if key != "parts"})
        candidate["parts"] = list(summary.get("parts") or [])
        found.append(candidate)
        if len(found) >= max(1, int(limit)):
            break
    return found


def library_summary(project_root: str | Path) -> dict[str, Any]:
    """What the library holds, without reading every entry."""

    root = library_root(project_root)
    index = _read_index(root)
    entries = index.get("entries") if isinstance(index.get("entries"), dict) else {}
    part_counts: dict[str, int] = {}
    stages: dict[str, int] = {}
    for summary in entries.values():
        if not isinstance(summary, Mapping):
            continue
        for name in list(summary.get("parts") or []):
            part_counts[name] = part_counts.get(name, 0) + 1
        stage = str(summary.get("stage", "") or "unknown")
        stages[stage] = stages.get(stage, 0) + 1
    return {
        "schema": SCHEMA,
        "schemaVersion": 1,
        "path": str(root),
        "exists": (root / INDEX_FILENAME).is_file(),
        "entryCount": len(entries),
        "byStage": stages,
        "byPart": part_counts,
        "updatedAt": index.get("updatedAt", ""),
    }


__all__ = [
    "APPLIED_SCHEMA",
    "ENTRY_SCHEMA",
    "MAX_PART_BYTES",
    "PARTS",
    "SCHEMA",
    "SharedShaderLibraryError",
    "applied_for_capture",
    "candidates",
    "library_root",
    "library_summary",
    "load_entry",
    "publish_entry",
    "rebuild_index",
    "record_applied",
    "validate_entry",
]


APPLIED_SCHEMA = "replaylab-shader-reuse-applied/v1"
APPLIED_FILENAME = "applied.json"
MAX_APPLIED_CAPTURES = 512
# How long a writer waits for another process before giving up with an error.
_LOCK_SECONDS = 5.0
MAX_APPLIED_SHADERS = 20000


def _capture_key(capture_path: str) -> str:
    """One capture file, spelled the same way however the caller wrote it."""

    text = str(capture_path or "").strip().strip('"')
    if not text:
        raise SharedShaderLibraryError("capture_path is required")
    return os.path.normcase(os.path.abspath(os.path.expanduser(text)))


def applied_for_capture(project_root: str | Path,
                        capture_path: str) -> dict[str, Any]:
    """What has already been matched and applied for this capture file.

    Reopening a capture that was worked on last week should not re-derive what was
    already decided. This is a cache and never an authority: it records that a
    decision was made, which the library entry it points at still has to back up.
    """

    ledger = _read_json(library_root(project_root) / APPLIED_FILENAME)
    captures = ledger.get("captures") if isinstance(ledger.get("captures"), dict) else {}
    record = captures.get(_capture_key(capture_path))
    shaders = record.get("shaders") if isinstance(record, Mapping) else {}
    return {
        "schema": APPLIED_SCHEMA,
        "capturePath": str(capture_path),
        "known": bool(record),
        "shaderCount": len(shaders if isinstance(shaders, dict) else {}),
        "shaders": shaders if isinstance(shaders, dict) else {},
        "updatedAt": (record or {}).get("updatedAt", ""),
    }


def record_applied(project_root: str | Path, capture_path: str,
                   applications: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Note that these Shaders in this capture now carry work from the library.

    Keyed by the Shader's own bytes, so the note survives the ResourceIds and event
    ids changing; keyed *within* a capture path, so the fast path on reopening that
    file is a single lookup rather than a match against the whole library.
    """

    root = library_root(project_root)
    with _library_lock(root):
        return _record_applied_locked(root, capture_path, applications)


def _record_applied_locked(root: Path, capture_path: str,
                           applications: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    ledger = _read_json(root / APPLIED_FILENAME)
    captures = ledger.get("captures") if isinstance(ledger.get("captures"), dict) else {}
    key = _capture_key(capture_path)
    record = captures.get(key) if isinstance(captures.get(key), Mapping) else {}
    shaders = dict(record.get("shaders") or {})
    written = 0
    for application in applications:
        raw_sha256 = _hex64(application.get("rawSha256"), "rawSha256")
        tier = _text(application.get("tier"), "tier", limit=32)
        if tier not in ("bytes", "interface", "source", "manual"):
            raise SharedShaderLibraryError(
                "tier must be one of bytes, interface, source, manual")
        if raw_sha256 not in shaders and len(shaders) >= MAX_APPLIED_SHADERS:
            raise SharedShaderLibraryError(
                "this capture already has %d recorded Shaders" % MAX_APPLIED_SHADERS)
        shaders[raw_sha256] = {
            "tier": tier,
            "entryId": _text(application.get("entryId"), "entryId", limit=128)
                       or raw_sha256,
            "workspaceId": _text(application.get("workspaceId"), "workspaceId",
                                 limit=128),
            "parts": sorted({str(name) for name in
                             list(application.get("parts") or [])
                             if str(name) in PARTS}),
            "appliedAt": _text(application.get("appliedAt"), "appliedAt", limit=64)
                         or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        written += 1
    if not written:
        raise SharedShaderLibraryError("no applications were supplied")
    captures[key] = {
        "capturePath": str(capture_path),
        "shaders": shaders,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if len(captures) > MAX_APPLIED_CAPTURES:
        # Drop the least recently touched capture rather than refusing the write: this
        # is a cache, and losing an old entry costs one re-match, not any work.
        for stale in sorted(captures, key=lambda name: str(
                captures[name].get("updatedAt", "")
                if isinstance(captures[name], Mapping) else ""
        ))[:len(captures) - MAX_APPLIED_CAPTURES]:
            captures.pop(stale, None)
    _atomic_write(root / APPLIED_FILENAME, {
        "schema": APPLIED_SCHEMA,
        "schemaVersion": 1,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "captureCount": len(captures),
        "captures": captures,
    })
    return {
        "schema": APPLIED_SCHEMA,
        "capturePath": str(capture_path),
        "recorded": written,
        "shaderCount": len(shaders),
        "path": str(root / APPLIED_FILENAME),
    }
