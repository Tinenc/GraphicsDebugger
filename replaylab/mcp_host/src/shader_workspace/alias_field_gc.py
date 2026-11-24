"""Collect retired naming pages without discarding interrupted publications.

Every public function requires the alias-store transaction lock. A publication
pins both its rollback and staged manifests before it changes any live manifest.
Pins are released only after durable completion and staging cleanup. The fixed
first-run page set protects older interrupted transactions which had no pins;
it is never enlarged on later runs and does not retain new naming history.
"""

from __future__ import annotations

import hashlib
import json
from itertools import islice
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from . import alias_field_storage as storage
from .io import atomic_write_bytes, canonical_workspace_identity

EPOCH_FILE = ".semantic-field-gc.json"
PIN_DIRECTORY = ".semantic-field-pins"
EPOCH_SCHEMA = "replaylab-cbuffer-field-gc/v1"
PIN_SCHEMA = "replaylab-cbuffer-field-pin/v1"
MAX_FILES = 32768
MAX_PINS = 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PAGE_NAME = re.compile(r"([0-9a-f]{64})\.json\Z")
_PIN_TEMP_NAME = re.compile(r"\.[0-9a-f]{64}\.json\.tmp-[0-9a-f]{32}\Z")


def _identity(path: Path) -> tuple[int, int, int, int]:
    if storage._redirected(path):
        raise ValueError("CBuffer field cleanup path is redirected")
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _directory(path: Path) -> tuple[int, int]:
    identity = _identity(path)
    if not path.is_dir():
        raise ValueError("CBuffer field cleanup directory is invalid")
    return identity[:2]


def _entries(path: Path, limit: int = MAX_FILES) -> list[Path]:
    _directory(path)
    result = list(islice(path.iterdir(), limit + 1))
    if len(result) > limit:
        raise ValueError("CBuffer field cleanup directory exceeds its bound")
    return result


def _read_raw(path: Path, limit: int) -> tuple[bytes, tuple[int, int, int, int]]:
    before = _identity(path)
    if not path.is_file() or not 1 <= before[2] <= limit:
        raise ValueError("CBuffer field cleanup metadata exceeds its bound")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) != before[2] or _identity(path) != before:
        raise ValueError("CBuffer field cleanup metadata changed while reading")
    return raw, before


def _read(path: Path, limit: int) -> tuple[Any, tuple[int, int, int, int]]:
    raw, identity = _read_raw(path, limit)
    return json.loads(raw.decode("utf-8")), identity


def _digests(value: Any) -> set[str]:
    if (not isinstance(value, list) or len(value) > MAX_FILES
            or any(not isinstance(item, str) or not _DIGEST.fullmatch(item) for item in value)
            or len(set(value)) != len(value)):
        raise ValueError("CBuffer field cleanup page references are invalid")
    return set(value)


def _manifest_pages(payload: Any) -> set[str]:
    if not isinstance(payload, Mapping):
        raise ValueError("CBuffer field cleanup manifest is invalid")
    views = payload.get("semanticViews", [])
    if not isinstance(views, list) or len(views) > 4096:
        raise ValueError("CBuffer field cleanup semantic views are invalid")
    result: set[str] = set()
    for view in views:
        if not isinstance(view, Mapping):
            raise ValueError("CBuffer field cleanup semantic view is invalid")
        if "fieldChunks" in view:
            result.update(item["sha256"] for item in storage.page_descriptors(view))
        elif not isinstance(view.get("fields"), list):
            raise ValueError("CBuffer field cleanup inline view is invalid")
        if isinstance(view.get("usageReview"), Mapping):
            result.update(item["sha256"] for item in storage.usage_page_descriptors(view["usageReview"]))
        if len(result) > MAX_FILES:
            raise ValueError("CBuffer field cleanup references exceed their bound")
    return result


def _root(root: Path) -> Path:
    root = Path(root).absolute()
    root.mkdir(parents=True, exist_ok=True)
    _directory(root)
    return root


def _page_paths(root: Path) -> dict[str, Path]:
    directory = root / storage.DIRECTORY
    if not directory.exists():
        if storage._redirected(directory):
            raise ValueError("CBuffer field page directory is redirected")
        return {}
    result = {}
    for path in _entries(directory):
        match = _PAGE_NAME.fullmatch(path.name)
        if match:
            _identity(path)
            if not path.is_file():
                raise ValueError("CBuffer field page is not a regular file")
            result[match.group(1)] = path
        # Atomic-write leftovers and unrelated files are never deleted.
    return result


def ensure_epoch(root: Path) -> dict[str, Any]:
    """Initialize before staging; conservatively preserve pre-upgrade pages."""
    root = _root(root)
    path = root / EPOCH_FILE
    if path.exists() or storage._redirected(path):
        payload, unused_identity = _read(path, MAX_METADATA_BYTES)
        if not isinstance(payload, Mapping) or payload.get("schema") != EPOCH_SCHEMA:
            raise ValueError("CBuffer field cleanup migration marker is invalid")
        _digests(payload.get("legacyPages"))
        return dict(payload)
    payload = {"schema": EPOCH_SCHEMA, "legacyPages": sorted(_page_paths(root))}
    raw = storage.json_bytes(payload)
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError("CBuffer field cleanup migration marker exceeds its bound")
    atomic_write_bytes(path, raw)
    return payload


def _pin_identity(root: Path, transaction_dir: Path, operation_id: str,
                  request_sha256: str) -> tuple[str, dict[str, str]]:
    if (not isinstance(operation_id, str) or not operation_id or len(operation_id) > 2048
            or not isinstance(request_sha256, str) or not _DIGEST.fullmatch(request_sha256)):
        raise ValueError("CBuffer field cleanup transaction identity is invalid")
    owner = {"aliasRoot": canonical_workspace_identity(root),
             "transactionDirectory": canonical_workspace_identity(transaction_dir),
             "operationId": operation_id, "requestSha256": request_sha256}
    key = hashlib.sha256(storage.json_bytes(owner)).hexdigest()
    return key, owner


def pin_transaction(root: Path, *, transaction_dir: Path, operation_id: str,
                    request_sha256: str, manifest_paths: Sequence[Path]) -> str:
    """Protect backup and staged pages before entering the applying phase."""
    root = _root(root)
    ensure_epoch(root)
    transaction_dir = Path(transaction_dir).absolute()
    _directory(transaction_dir)
    if len(manifest_paths) > MAX_FILES:
        raise ValueError("CBuffer field pin contains too many manifests")
    key, owner = _pin_identity(root, transaction_dir, operation_id, request_sha256)
    pages: set[str] = set()
    for path in manifest_paths:
        path = Path(path).absolute()
        if not path.is_relative_to(transaction_dir):
            raise ValueError("CBuffer field pin manifest is outside its transaction")
        for parent in path.parents:
            _directory(parent)
            if parent == transaction_dir:
                break
        payload, unused_identity = _read(path, MAX_MANIFEST_BYTES)
        pages.update(_manifest_pages(payload))
    if len(pages) > MAX_FILES:
        raise ValueError("CBuffer field pin exceeds its page bound")
    directory = root / PIN_DIRECTORY
    directory.mkdir(exist_ok=True)
    _directory(directory)
    path = directory / (key + ".json")
    if storage._redirected(path):
        raise ValueError("CBuffer field pin is redirected")
    payload = {"schema": PIN_SCHEMA, **owner, "pages": sorted(pages)}
    raw = storage.json_bytes(payload)
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError("CBuffer field pin metadata exceeds its bound")
    atomic_write_bytes(path, raw)
    return key


def release_transaction_pin(root: Path, *, transaction_dir: Path, operation_id: str,
                            request_sha256: str) -> None:
    """Release only the caller's completed transaction, never another old pin."""
    root = _root(root)
    key, owner = _pin_identity(root, transaction_dir, operation_id, request_sha256)
    directory = root / PIN_DIRECTORY
    if not directory.exists() and not storage._redirected(directory):
        return
    _directory(directory)
    path = directory / (key + ".json")
    if not path.exists() and not storage._redirected(path):
        return
    payload, identity = _read(path, MAX_METADATA_BYTES)
    if (not isinstance(payload, Mapping) or payload.get("schema") != PIN_SCHEMA
            or any(payload.get(name) != value for name, value in owner.items())):
        raise ValueError("CBuffer field pin does not belong to its transaction")
    _digests(payload.get("pages"))
    if _identity(path) != identity:
        raise ValueError("CBuffer field pin changed before release")
    path.unlink()


def collect_unreferenced_pages(root: Path) -> dict[str, int]:
    """Mark live/pending/legacy references, validate the sweep, then unlink."""
    from . import aliases

    root = _root(root)
    root_identity = _directory(root)
    epoch = ensure_epoch(root)
    legacy = _digests(epoch["legacyPages"])
    marked = set(legacy)
    required: set[str] = set()
    snapshots: list[tuple[Path, tuple[int, int, int, int]]] = []
    unused_epoch, epoch_identity = _read(root / EPOCH_FILE, MAX_METADATA_BYTES)
    snapshots.append((root / EPOCH_FILE, epoch_identity))
    for path in _entries(root):
        match = _PAGE_NAME.fullmatch(path.name)
        if not match:
            continue
        payload, identity = _read(path, MAX_MANIFEST_BYTES)
        if (not isinstance(payload, Mapping) or payload.get("schema") != aliases.SCHEMA
                or payload.get("layoutSha256") != match.group(1)):
            raise ValueError("CBuffer field cleanup live manifest identity is invalid")
        required.update(_manifest_pages(payload))
        if len(required) > MAX_FILES:
            raise ValueError("CBuffer field cleanup live references exceed their bound")
        snapshots.append((path, identity))
    pin_count = 0
    directory = root / PIN_DIRECTORY
    pin_directory_identity = None
    if directory.exists() or storage._redirected(directory):
        pin_directory_identity = _directory(directory)
        for path in _entries(directory, MAX_PINS):
            match = _PAGE_NAME.fullmatch(path.name)
            if not match:
                if _PIN_TEMP_NAME.fullmatch(path.name):
                    # A crash before the atomic pin replacement cannot have
                    # entered applying. Keep the temporary, but it is no pin.
                    continue
                # Unknown entries can represent a newer recovery format.
                raise ValueError("CBuffer field cleanup pin directory contains an unknown entry")
            payload, identity = _read(path, MAX_METADATA_BYTES)
            if not isinstance(payload, Mapping) or payload.get("schema") != PIN_SCHEMA:
                raise ValueError("CBuffer field cleanup pin is invalid")
            key, owner = _pin_identity(root, Path(str(payload.get("transactionDirectory") or "")),
                                       payload.get("operationId"), payload.get("requestSha256"))
            if key != match.group(1) or any(payload.get(name) != value for name, value in owner.items()):
                raise ValueError("CBuffer field cleanup pin identity does not match")
            required.update(_digests(payload.get("pages")))
            if len(required) > MAX_FILES:
                raise ValueError("CBuffer field cleanup pending references exceed their bound")
            snapshots.append((path, identity))
            pin_count += 1
    paths = _page_paths(root)
    if required - paths.keys():
        raise ValueError("CBuffer field cleanup live or pending page is missing")
    marked.update(required)
    candidates = []
    for digest, path in paths.items():
        if digest in marked:
            continue
        raw, identity = _read_raw(path, storage.MAX_CHUNK_BYTES)
        values = json.loads(raw.decode("utf-8"))
        if (not isinstance(values, list) or not 1 <= len(values) <= storage.MAX_CHUNK_FIELDS
                or hashlib.sha256(raw).hexdigest() != digest):
            raise ValueError("CBuffer field cleanup orphan page is not valid content-addressed data")
        if _identity(path) != identity:
            raise ValueError("CBuffer field cleanup orphan page changed while validating")
        candidates.append((path, identity))
    # No deletion until every manifest, pin and candidate has been validated.
    for path, identity in snapshots:
        if _identity(path) != identity:
            raise ValueError("CBuffer field cleanup references changed before deletion")
    page_directory = root / storage.DIRECTORY
    page_directory_identity = _directory(page_directory) if paths else None
    deleted_bytes = 0
    for path, identity in candidates:
        if (_directory(root) != root_identity
                or _directory(page_directory) != page_directory_identity
                or (pin_directory_identity is not None
                    and _directory(directory) != pin_directory_identity)
                or _identity(path) != identity):
            raise ValueError("CBuffer field cleanup paths changed before deletion")
        path.unlink()
        deleted_bytes += identity[2]
    return {"deletedPages": len(candidates), "deletedBytes": deleted_bytes,
            "retainedPages": len(paths) - len(candidates), "legacyProtectedPages": len(legacy),
            "pendingTransactions": pin_count}
