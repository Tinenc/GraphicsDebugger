"""Portable, capture-bound frame diagrams (stdlib only; Python 3.6 compatible).

RDC bytes are never changed. A small sidecar pointer is committed only after
the document, action outline and every preview have been copied successfully.
All filesystem work belongs on a worker, never in a capture-loaded UI callback.
"""
from __future__ import print_function

from copy import deepcopy
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import tempfile


SCHEMA = "bread-frame-render-flow-bundle/v1"
DOCUMENT_SCHEMA = "bread-frame-render-flow/v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_DOCUMENT = 8 * 1024 * 1024
MAX_OUTLINE = 96 * 1024 * 1024
MAX_MEDIA = 64 * 1024 * 1024
MAX_BUNDLE = 2 * 1024 * 1024 * 1024
# 512 ordinary previews, 1024 cached volume slices and up to 2048 stage GIFs
# fit under one shared limit. Saving and loading must use identical budgets.
MAX_MEDIA_ENTRIES = 4096
MAX_POINTER = 2 * 1024 * 1024
SAMPLE_BYTES = 64 * 1024
SAMPLE_COUNT = 17
_FRAME_FINGERPRINT_CACHE = OrderedDict()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def canonical_outline(value):
    """The identical public subset used by Host validate_outline()."""
    rows = []
    for source in value["actions"]:
        row = {"eventId": source["eventId"], "executionIndex": source["executionIndex"],
               "parentEventId": source.get("parentEventId", 0),
               "name": source.get("name", "").strip(), "flags": source.get("flags", 0),
               "outputs": list(source.get("outputs", []))}
        for key in ("depthOutput", "copySource", "copyDestination"):
            row[key] = source.get(key, 0)
        # These optional captured view coordinates are part of the Host's
        # snapshot. Dropping them here makes every diagram containing a copy
        # fail persistence despite belonging to the exact active capture.
        for key in ("copySourceSubresource", "copyDestinationSubresource"):
            if key in source:
                row[key] = {field: source[key][field] for field in ("mip", "slice", "sample")}
        for key in ("isAction", "hasWork"):
            if key in source:
                row[key] = source[key]
        if "actionKind" in source:
            row["actionKind"] = source["actionKind"].strip()
        rows.append(row)
    rows.sort(key=lambda row: row["executionIndex"])
    source = value["summary"]
    return {"schema": "bread-frame-render-flow-outline/v1",
            "captureSessionId": value["captureSessionId"], "captureId": value.get("captureId", ""),
            "capturePath": value.get("capturePath", "").strip(), "api": value.get("api", "").strip(),
            "frameNumber": value.get("frameNumber", 0), "actions": rows,
            "summary": {"complete": source["complete"],
                        "totalActionCount": source["totalActionCount"],
                        "omittedActionCount": source.get("omittedActionCount", 0)}}


def outline_identity(value):
    clean = canonical_outline(value)
    # Capture session/catalog identity and file path legitimately change on
    # reopen or when the RDC plus its sidecar are moved to another directory.
    # Indirect arguments are GPU-produced: their formatted names and hasWork
    # can change between replays of the same physical capture. Bind only stable
    # EIDs, ordering, flags and captured resource references, then recalculate
    # navigation from the newly decoded workload before displaying the graph.
    clean["actions"] = [{key: item for key, item in row.items()
                         if key not in ("name", "hasWork")} for row in clean["actions"]]
    return digest({key: clean[key] for key in ("api", "frameNumber", "actions", "summary")})


def _ordinary(path):
    path = Path(path)
    for component in (path,) + tuple(path.parents):
        if component.exists() or component.is_symlink():
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Frame diagram storage cannot traverse a redirected path")
    return path


def capture_fingerprint(path):
    """Identify the immutable FrameCapture section, excluding editable metadata.

    Native SaveChanges rewrites later EditedShaders, Notes, Bookmarks and Rename
    sections. Their size and file timestamp cannot identify a captured frame.
    Hash the compressed frame payload in bounded memory, without decompressing
    it or depending on its offset/header thumbnail. The native RDC 1.x format
    requires this first section to be binary FrameCapture (rdcfile.cpp).
    Non-RDC legacy fixtures retain the original strict file identity contract.
    """
    path = _ordinary(Path(path).absolute())
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Frame diagram requires an ordinary local capture file")
    cache_key = (os.path.normcase(os.path.realpath(str(path))), before.st_ino,
                 before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    cached = _FRAME_FINGERPRINT_CACHE.get(cache_key)
    if cached is not None:
        _check_capture_stat(path, before)
        _FRAME_FINGERPRINT_CACHE.move_to_end(cache_key)
        return dict(cached)
    with path.open("rb") as stream:
        prefix = stream.read(32)
        if prefix[:4] != b"RDOC":
            return _legacy_capture_fingerprint(path, before, prefix)
        if len(prefix) != 32:
            raise ValueError("Capture file has a truncated RDC header")
        magic, version, header_size, unused_program = struct.unpack("<QII16s", prefix)
        if (magic != 0x434f4452 or version not in (0x100, 0x101, 0x102) or
                not 32 <= header_size <= before.st_size - 40):
            raise ValueError("Capture file has an unsupported RDC header")
        stream.seek(header_size)
        raw_header = stream.read(40)
        if len(raw_header) != 40:
            raise ValueError("Capture file has a truncated frame section")
        tag, kind, compressed, uncompressed, section_version, flags, name_size = struct.unpack(
            "<4sIQQQII", raw_header)
        if (tag != b"\0\0\0\0" or kind != 1 or not 1 <= name_size <= 2048 or
                compressed <= 0 or uncompressed <= 0 or
                header_size + 40 + name_size + compressed > before.st_size):
            raise ValueError("Capture file does not start with a valid FrameCapture section")
        name = stream.read(name_size)
        if len(name) != name_size or not name.endswith(b"\0"):
            raise ValueError("Capture frame section has an invalid name")
        result, remaining = hashlib.sha256(), compressed
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("Capture frame section changed while reading")
            result.update(chunk)
            remaining -= len(chunk)
    _check_capture_stat(path, before)
    fingerprint = {"method": "rdc-frame-section-sha256-and-action-outline/v2",
                   "containerVersion": version, "sectionVersion": section_version,
                   "sectionFlags": flags, "compressedByteSize": compressed,
                   "uncompressedByteSize": uncompressed,
                   "sectionNameSha256": hashlib.sha256(name).hexdigest(),
                   "frameCaptureSha256": result.hexdigest()}
    _FRAME_FINGERPRINT_CACHE[cache_key] = fingerprint
    while len(_FRAME_FINGERPRINT_CACHE) > 8:
        _FRAME_FINGERPRINT_CACHE.popitem(last=False)
    return dict(fingerprint)


def _check_capture_stat(path, before):
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("Capture file changed while checking its frame diagram")


def _legacy_capture_fingerprint(path, before=None, prefix=None):
    """Strict v1 compatibility; a changed legacy capture is never auto-adopted."""
    path = _ordinary(Path(path).absolute())
    before = before or path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Frame diagram requires an ordinary local capture file")
    size = before.st_size
    positions = sorted(set((max(0, size - SAMPLE_BYTES) * index) // (SAMPLE_COUNT - 1)
                           for index in range(SAMPLE_COUNT)))
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for offset in positions:
            stream.seek(offset)
            if offset == 0 and prefix is not None:
                stream.seek(len(prefix))
                chunk = prefix + stream.read(SAMPLE_BYTES - len(prefix))
            else:
                chunk = stream.read(SAMPLE_BYTES)
            result.update(str(offset).encode("ascii") + b":" + chunk)
    _check_capture_stat(path, before)
    return {"method": "size-mtime-sampled-sha256-and-action-outline/v1", "byteSize": size,
            "mtimeNs": before.st_mtime_ns, "sampleSha256": result.hexdigest()}


def sidecar_directory(capture_path):
    path = Path(capture_path).absolute()
    return path.with_name(path.name + ".bread") / "frame-render-flow"


def fallback_directory(capture_path, artifact_root):
    key = os.path.normcase(os.path.abspath(str(capture_path)))
    return Path(artifact_root) / "frame-render-flow-captures" / hashlib.sha256(key.encode("utf-8")).hexdigest()


def _atomic(path, payload):
    path = _ordinary(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ordinary(path.parent)
    handle, temporary = tempfile.mkstemp(prefix=".flow-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _bounded(path, maximum):
    path = _ordinary(path)
    if not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise ValueError("Frame diagram file exceeds its supported size")
    # BufferedReader.read(n) can reserve n bytes before discovering EOF (notably
    # in the bundled Python 3.6). The 64/96 MiB safety ceilings are not the file
    # sizes: many tiny previews must not each allocate that entire ceiling.
    chunks, total = [], 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise ValueError("Frame diagram file changed size while reading")
            chunks.append(chunk)
    return b"".join(chunks)


def _asset_fields(document):
    for stage in document.get("stages", []):
        for image in stage.get("images", []):
            if image.get("path"):
                yield image, "path", "sha256", ".png"
            for volume_slice in (image.get("volumePreview") or {}).get("slices", []):
                if volume_slice.get("path"):
                    yield volume_slice, "path", "sha256", ".png"
        for animation in stage.get("animations", []):
            if animation.get("gifPath"):
                yield animation, "gifPath", "gifSha256", ".gif"


def _save_at(folder, capture_path, document, outline, fingerprint):
    folder = _ordinary(folder)
    folder.mkdir(parents=True, exist_ok=True)
    # JSON is the persistence contract: do not retain Python object aliases
    # when the same preview dictionary was referenced by several stages.
    source_document = encoded(document)
    if len(source_document) > MAX_DOCUMENT:
        raise ValueError("Frame diagram document exceeds its bundle budget")
    portable = json.loads(source_document.decode("utf-8"))
    total, media = 0, {}
    for owner, key, hash_key, suffix in _asset_fields(portable):
        source = Path(owner[key])
        if not source.is_absolute() or source.suffix.lower() != suffix:
            raise ValueError("Frame diagram media source is invalid")
        payload = _bounded(source, MAX_MEDIA)
        sha = hashlib.sha256(payload).hexdigest()
        if owner.get(hash_key) and owner[hash_key] != sha:
            raise ValueError("Frame diagram media content changed before persistence")
        relative = "media/" + sha + suffix
        if relative not in media:
            if len(media) >= MAX_MEDIA_ENTRIES:
                raise ValueError("Frame diagram media inventory exceeds its entry budget")
            total += len(payload)
            if total > MAX_BUNDLE:
                raise ValueError("Frame diagram media exceeds its bundle budget")
            destination = folder / relative
            if not destination.exists() or hashlib.sha256(_bounded(destination, MAX_MEDIA)).hexdigest() != sha:
                _atomic(destination, payload)
            media[relative] = {"sha256": sha, "byteSize": len(payload)}
        owner[key], owner[hash_key] = relative, sha
    document_bytes, outline_bytes = encoded(portable), encoded(canonical_outline(outline))
    if len(document_bytes) > MAX_DOCUMENT or len(outline_bytes) > MAX_OUTLINE:
        raise ValueError("Frame diagram document or outline exceeds its bundle budget")
    document_sha, outline_sha = hashlib.sha256(document_bytes).hexdigest(), hashlib.sha256(outline_bytes).hexdigest()
    _atomic(folder / (document_sha + ".json"), document_bytes)
    _atomic(folder / (outline_sha + ".outline.json"), outline_bytes)
    if capture_fingerprint(capture_path) != fingerprint:
        raise ValueError("Capture file changed before saving its frame diagram")
    pointer = {"schema": SCHEMA, "capture": fingerprint, "outlineIdentity": outline_identity(outline),
               "documentSha256": document_sha, "outlineSha256": outline_sha, "media": media}
    pointer_bytes = encoded(pointer)
    if len(pointer_bytes) > MAX_POINTER:
        raise ValueError("Frame diagram media inventory exceeds its pointer budget")
    _atomic(folder / "current.json", pointer_bytes)
    return {"saved": True, "directory": str(folder), "manifestPath": str(folder / "current.json"),
            "portable": folder == sidecar_directory(capture_path), "mediaCount": len(media)}


def save(capture_path, document, outline, artifact_root):
    if document.get("schema") != DOCUMENT_SCHEMA or document.get("captureSessionId") != outline.get("captureSessionId"):
        raise ValueError("Frame diagram and outline belong to different capture sessions")
    if os.path.normcase(os.path.abspath(str(capture_path))) != os.path.normcase(os.path.abspath(outline["capturePath"])):
        raise ValueError("Frame diagram does not belong to this capture file")
    if document.get("snapshotId") != digest(canonical_outline(outline)):
        raise ValueError("Frame diagram does not match its saved action outline")
    fingerprint = capture_fingerprint(capture_path)
    primary = sidecar_directory(capture_path)
    try:
        return _save_at(primary, capture_path, document, outline, fingerprint)
    except (OSError, PermissionError) as error:
        result = _save_at(fallback_directory(capture_path, artifact_root), capture_path,
                          document, outline, fingerprint)
        result["sidecarError"] = str(error)
        return result


def _load_at(folder, capture_path, pointer_override=None):
    capture_before = Path(capture_path).stat()
    pointer = (pointer_override if pointer_override is not None else
               json.loads(_bounded(folder / "current.json", MAX_POINTER).decode("utf-8")))
    saved_fingerprint = pointer.get("capture")
    legacy = (isinstance(saved_fingerprint, dict) and saved_fingerprint.get("method") ==
              "size-mtime-sampled-sha256-and-action-outline/v1")
    current_fingerprint = (_legacy_capture_fingerprint(capture_path) if legacy else
                           capture_fingerprint(capture_path))
    if pointer.get("schema") != SCHEMA or saved_fingerprint != current_fingerprint:
        raise ValueError("Saved frame diagram does not match the capture file identity")
    blobs = {}
    for field, suffix, limit in (("documentSha256", ".json", MAX_DOCUMENT),
                                 ("outlineSha256", ".outline.json", MAX_OUTLINE)):
        sha = pointer.get(field, "")
        if not isinstance(sha, str) or not HEX64.match(sha):
            raise ValueError("Invalid persisted frame diagram content ID")
        payload = _bounded(folder / (sha + suffix), limit)
        if hashlib.sha256(payload).hexdigest() != sha:
            raise ValueError("Persisted frame diagram content changed")
        blobs[field] = json.loads(payload.decode("utf-8"))
    document, outline = blobs["documentSha256"], blobs["outlineSha256"]
    if (document.get("schema") != DOCUMENT_SCHEMA or outline_identity(outline) != pointer.get("outlineIdentity") or
            document.get("snapshotId") != pointer["outlineSha256"] or
            document.get("captureSessionId") != outline.get("captureSessionId")):
        raise ValueError("Persisted frame diagram outline changed")
    media, total, verified = pointer.get("media", {}), 0, set()
    if not isinstance(media, dict) or len(media) > MAX_MEDIA_ENTRIES:
        raise ValueError("Persisted frame diagram media inventory is invalid")
    for owner, key, hash_key, suffix in _asset_fields(document):
        relative = owner[key]
        if not re.match(r"^media/[0-9a-f]{64}" + re.escape(suffix) + r"$", relative):
            raise ValueError("Persisted frame diagram media path escapes its bundle")
        expected = media.get(relative)
        if not isinstance(expected, dict) or expected.get("sha256") != owner.get(hash_key):
            raise ValueError("Persisted frame diagram media inventory changed")
        path = folder / relative
        if relative not in verified:
            payload = _bounded(path, MAX_MEDIA)
            total += len(payload)
            if total > MAX_BUNDLE or len(payload) != expected.get("byteSize") or hashlib.sha256(payload).hexdigest() != expected["sha256"]:
                raise ValueError("Persisted frame diagram media content changed")
            verified.add(relative)
        owner[key] = str(path)
    if legacy:
        # Upgrade only after the original strict v1 identity and every saved
        # document/media hash passed. This happens before a subsequent native
        # metadata save can invalidate v1. A mismatching legacy file never
        # reaches this branch and cannot be adopted merely from its outline.
        current_fingerprint = capture_fingerprint(capture_path)
    _check_capture_stat(Path(capture_path), capture_before)
    if pointer_override is None and legacy and current_fingerprint.get("method") != saved_fingerprint.get("method"):
        upgraded = dict(pointer, capture=current_fingerprint)
        payload = encoded(upgraded)
        if len(payload) > MAX_POINTER:
            raise ValueError("Frame diagram media inventory exceeds its pointer budget")
        _atomic(folder / "current.json", payload)
        pointer = upgraded
    return {"document": document, "outline": outline, "directory": str(folder),
            "captureFingerprint": pointer["capture"], "outlineIdentity": pointer["outlineIdentity"]}


def load(capture_path, artifact_root):
    """Return a checked bundle or None; invalid existing bundles fail explicitly."""
    for folder in (sidecar_directory(capture_path), fallback_directory(capture_path, artifact_root)):
        if (folder / "current.json").exists():
            return _load_at(folder, capture_path)
    return None


def restore_revision(capture_path, artifact_root, revision):
    """Restore a checked prior document without deleting the current revision."""
    if not isinstance(revision,str) or not HEX64.match(revision):
        raise ValueError('A content-addressed frame diagram revision is required')
    current=load(capture_path,artifact_root)
    if current is None:raise ValueError('No verified frame diagram bundle is available')
    folder=Path(current['directory'])
    old_pointer=_bounded(folder/'current.json',MAX_POINTER)
    pointer=json.loads(old_pointer.decode('utf-8'))
    raw=_bounded(folder/(revision+'.json'),MAX_DOCUMENT)
    if hashlib.sha256(raw).hexdigest()!=revision:raise ValueError('Historical frame document changed')
    document=json.loads(raw.decode('utf-8'));snapshot=document.get('snapshotId','')
    if not isinstance(snapshot,str) or not HEX64.match(snapshot):raise ValueError('Invalid historical outline identity')
    outline_raw=_bounded(folder/(snapshot+'.outline.json'),MAX_OUTLINE)
    if hashlib.sha256(outline_raw).hexdigest()!=snapshot:raise ValueError('Historical frame outline changed')
    outline=json.loads(outline_raw.decode('utf-8'))
    if (outline_identity(outline)!=current['outlineIdentity'] or
            os.path.normcase(os.path.abspath(document.get('capturePath','')))!=os.path.normcase(os.path.abspath(str(capture_path)))):
        raise ValueError('Historical diagram does not match the current capture/action tree')
    media={}
    for owner,key,hash_key,suffix in _asset_fields(document):
        relative=owner[key]
        if not re.match(r'^media/[0-9a-f]{64}'+re.escape(suffix)+r'$',relative):raise ValueError('Invalid historical media path')
        path=folder/relative;payload=_bounded(path,MAX_MEDIA)
        digest=hashlib.sha256(payload).hexdigest()
        if digest!=owner[hash_key]:raise ValueError('Historical frame media changed')
        media[relative]={'sha256':digest,'byteSize':len(payload)}
    proposed=dict(pointer,documentSha256=revision,outlineSha256=snapshot,media=media)
    checked=_load_at(folder,capture_path,proposed)
    _atomic(folder/(hashlib.sha256(old_pointer).hexdigest()+'.pointer.json'),old_pointer)
    # Commit only after the historical document, outline and media all passed.
    if _bounded(folder/'current.json',MAX_POINTER)!=old_pointer:raise ValueError('Current diagram changed during restoration')
    _atomic(folder/'current.json',encoded(proposed))
    return checked


def rebind(bundle, current_outline):
    current_outline = canonical_outline(current_outline)
    comparison = current_outline
    if outline_identity(current_outline) != bundle["outlineIdentity"]:
        # Older collectors did not record optional copy view coordinates.
        # Enrich those rows from the reopened, fingerprint-checked capture;
        # never ignore a coordinate that the saved outline actually recorded.
        # The immutable base action tree must still match in its entirety.
        saved = {row["eventId"]: row for row in bundle["outline"]["actions"]}
        comparison = deepcopy(current_outline)
        for row in comparison["actions"]:
            previous = saved.get(row["eventId"], {})
            for key in ("copySourceSubresource", "copyDestinationSubresource"):
                if key not in previous:
                    row.pop(key, None)
    if outline_identity(comparison) != bundle["outlineIdentity"]:
        raise ValueError("Saved frame diagram action tree does not match the opened capture")
    document = deepcopy(bundle["document"])
    # No saved session or ResourceId can acquire authority before both the
    # physical capture and complete action tree identity checks have passed.
    snapshot = digest(current_outline)
    document.update(captureSessionId=current_outline["captureSessionId"],
                    captureId=current_outline["captureId"], capturePath=current_outline["capturePath"],
                    snapshotId=snapshot, flowId="flow-" + snapshot[:32])
    by_event = {row["eventId"]: row for row in current_outline["actions"]}
    for stage in document.get("stages", []):
        rows = [by_event[event] for event in stage["eventIds"]]
        active = [row for row in rows if row.get("hasWork") is not False]
        graphics = [row for row in active if row.get("actionKind") == "draw" or row["flags"] & (0x0002 | 0x0008)]
        compute = [row for row in active if row.get("actionKind") == "dispatch" or row["flags"] & (0x0004 | 0x4000)]
        utility = [row for row in active if row.get("actionKind") not in ("draw", "dispatch")
                   and not row["flags"] & (0x0002 | 0x0008 | 0x0004 | 0x4000)]
        candidates = graphics or compute or utility or rows
        selected = candidates[-1]
        status = "last-draw" if graphics else "last-dispatch" if compute else "last-utility" if utility else "no-effective-work"
        stage.update(navigationEventId=selected["eventId"], representativeEventId=selected["eventId"],
                     navigationStatus=status, navigationHasWork=selected.get("hasWork"),
                     navigationPolicy="last-effective-draw-then-dispatch-or-utility",
                     navigationActionKind=selected.get("actionKind") or (
                         "draw" if selected["flags"] & (0x0002 | 0x0008) else
                         "dispatch" if selected["flags"] & (0x0004 | 0x4000) else "utility"))
    for observation in document.get("observations", {}).values():
        if isinstance(observation, dict):
            # Rebinding proves the RDC/action identity, not that a previous
            # replacement or CBuffer edit was reapplied by this new controller.
            observation["sourceCaptureSessionId"] = (
                observation.get("sourceCaptureSessionId") or observation.get("captureSessionId")
                or bundle["document"].get("captureSessionId", ""))
            observation["captureSessionId"] = current_outline["captureSessionId"]
            observation["evidenceFreshness"] = "previously-captured"
            observation["currentReplayVerified"] = False
    return {"document": document, "outline": current_outline,
            "persistence": {"restored": True, "directory": bundle["directory"], "captureMatched": True,
                            "actionOutlineMatched": True, "perEventReplay": False}}


def legacy_candidates(capture_path, artifact_root):
    """Bounded compatibility lookup; only trusted content-addressed Host files."""
    root = Path(artifact_root) / "frame-render-flow"
    if not root.is_dir():
        return []
    capture_key = os.path.normcase(os.path.abspath(str(capture_path)))
    capture_time = Path(capture_path).stat().st_mtime_ns
    candidates, examined = [], 0
    priority = lambda item: (bool(item["document"].get("coverage", {}).get("complete")), item["modifiedNs"])
    folders = sorted((path for path in root.iterdir() if path.is_dir() and
                      re.match(r"^[0-9a-f]{32}$", path.name)), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for folder in folders[:64]:
        for path in sorted(folder.glob("*.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True)[:128]:
            if not HEX64.match(path.stem) or path.stat().st_mtime_ns < capture_time:
                continue
            examined += 1
            if examined > 512:
                return sorted(candidates, key=priority, reverse=True)
            try:
                data = _bounded(path, MAX_DOCUMENT)
                if hashlib.sha256(data).hexdigest() != path.stem:
                    continue
                document = json.loads(data.decode("utf-8"))
                if document.get("schema") != DOCUMENT_SCHEMA or os.path.normcase(os.path.abspath(document.get("capturePath", ""))) != capture_key:
                    continue
                snapshot = document.get("snapshotId", "")
                if not isinstance(snapshot, str) or not HEX64.match(snapshot):
                    continue
                outline_data = _bounded(folder / (snapshot + ".outline.json"), MAX_OUTLINE)
                if hashlib.sha256(outline_data).hexdigest() != snapshot:
                    continue
                outline = json.loads(outline_data.decode("utf-8"))
                candidates.append({"document": document, "outline": outline, "directory": str(folder),
                                   "outlineIdentity": outline_identity(outline), "legacy": True,
                                   "modifiedNs": path.stat().st_mtime_ns})
                if len(candidates) >= 16:
                    return sorted(candidates, key=priority, reverse=True)
            except (OSError, ValueError, TypeError, KeyError):
                continue
    return sorted(candidates, key=priority, reverse=True)
