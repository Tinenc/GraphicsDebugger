"""Immutable, bounded pages for CBuffer display fields.

The layout manifest remains the atomic publication point. Pages contain no
authority or physical layout: they become visible only through a scope-bound
manifest. Keeping large field arrays here prevents each compiled revision from
making every later publication rewrite the entire naming history.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .io import atomic_write_bytes

DIRECTORY = "semantic-fields"
MAX_CHUNK_BYTES = 512 * 1024
MAX_CHUNK_FIELDS = 256
MAX_CHUNKS = 256
MAX_VIEW_BYTES = 64 * 1024 * 1024
INLINE_FIELD_LIMIT = 32
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _redirected(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _path(root: Path, digest: str) -> Path:
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("CBuffer field page digest is invalid")
    directory = Path(root) / DIRECTORY
    if _redirected(directory):
        raise ValueError("CBuffer field page directory is redirected")
    path = directory / (digest + ".json")
    if _redirected(path):
        raise ValueError("CBuffer field page is redirected")
    return path


def read_page(root: Path, descriptor: Mapping[str, Any]) -> tuple[bytes, list]:
    digest, count, size = (descriptor.get(key) for key in ("sha256", "count", "sizeBytes"))
    if (type(count) is not int or not 1 <= count <= MAX_CHUNK_FIELDS
            or type(size) is not int or not 1 <= size <= MAX_CHUNK_BYTES):
        raise ValueError("CBuffer field page bounds are invalid")
    path = _path(root, digest)
    before = path.stat()
    if not path.is_file() or before.st_size != size:
        raise ValueError("CBuffer field page is missing or has changed size")
    with path.open("rb") as stream:
        raw = stream.read(MAX_CHUNK_BYTES + 1)
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("CBuffer field page digest does not match")
    values = json.loads(raw.decode("utf-8"))
    if not isinstance(values, list) or len(values) != count:
        raise ValueError("CBuffer field page count does not match")
    return raw, values


def page_descriptors(view: Mapping[str, Any]) -> list:
    """Validate bounded references without loading other Shader revisions."""
    chunks, count = view.get("fieldChunks"), view.get("fieldCount")
    if ("fields" in view or not isinstance(chunks, list) or not 1 <= len(chunks) <= MAX_CHUNKS
            or type(count) is not int or not 1 <= count <= 16384):
        raise ValueError("CBuffer field page manifest is invalid")
    total_count, total_bytes = 0, 0
    for descriptor in chunks:
        if not isinstance(descriptor, Mapping):
            raise ValueError("CBuffer field page descriptor is invalid")
        digest, size, fields = (descriptor.get(key) for key in ("sha256", "sizeBytes", "count"))
        if (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                or type(size) is not int or not 1 <= size <= MAX_CHUNK_BYTES
                or type(fields) is not int or not 1 <= fields <= MAX_CHUNK_FIELDS):
            raise ValueError("CBuffer field page descriptor bounds are invalid")
        total_count += fields
        total_bytes += size
    if total_count != count or total_bytes > MAX_VIEW_BYTES:
        raise ValueError("CBuffer field pages exceed their declared bounds")
    return chunks


def read_fields(view: Mapping[str, Any], root: Path) -> list:
    if "fieldChunks" not in view:
        return view.get("fields")
    chunks = page_descriptors(view)
    count = view["fieldCount"]
    result, total_bytes = [], 0
    for descriptor in chunks:
        if not isinstance(descriptor, Mapping):
            raise ValueError("CBuffer field page descriptor is invalid")
        raw, values = read_page(root, descriptor)
        total_bytes += len(raw)
        result.extend(values)
        if len(result) > count or total_bytes > MAX_VIEW_BYTES:
            raise ValueError("CBuffer field pages exceed their declared bounds")
    if len(result) != count:
        raise ValueError("CBuffer field page manifest count does not match")
    return result


def usage_page_descriptors(review: Mapping[str, Any]) -> list:
    if "unusedComponentChunks" not in review:
        return []
    if "unusedComponents" in review:
        raise ValueError("CBuffer usage review has both inline and paged components")
    return page_descriptors({"fieldChunks": review["unusedComponentChunks"],
                             "fieldCount": review.get("unusedComponentCount")})


def read_usage_review(review: Mapping[str, Any], root: Path, *, expand: bool = True) -> dict:
    result = dict(review)
    chunks = usage_page_descriptors(result)
    if chunks and expand:
        result["unusedComponents"] = read_fields(
            {"fieldChunks": chunks, "fieldCount": result["unusedComponentCount"]}, root)
        result.pop("unusedComponentChunks")
        result.pop("unusedComponentCount")
    return result


def _write_page(root: Path, values: list) -> dict[str, Any]:
    raw = json_bytes(values)
    if not values or len(values) > MAX_CHUNK_FIELDS or len(raw) > MAX_CHUNK_BYTES:
        raise ValueError("CBuffer field page exceeds its bounded size")
    descriptor = {"sha256": hashlib.sha256(raw).hexdigest(), "count": len(values), "sizeBytes": len(raw)}
    path = _path(root, descriptor["sha256"])
    if path.exists():
        read_page(root, descriptor)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        _path(root, descriptor["sha256"])
        atomic_write_bytes(path, raw)
    return descriptor


def store_view(view: Mapping[str, Any], root: Path) -> dict[str, Any]:
    result = dict(view)
    review = result.get("usageReview")
    if isinstance(review, Mapping):
        components = review.get("unusedComponents")
        if isinstance(components, list) and len(components) > INLINE_FIELD_LIMIT:
            packed = store_view({"fields": components}, root)
            review = dict(review)
            review.pop("unusedComponents")
            review["unusedComponentChunks"] = packed["fieldChunks"]
            review["unusedComponentCount"] = packed["fieldCount"]
            result["usageReview"] = review
    fields = result.get("fields")
    if not isinstance(fields, list) or len(fields) <= INLINE_FIELD_LIMIT:
        return result
    pages, pending, pending_size, total_bytes = [], [], 3, 0
    for field in fields:
        size = len(json_bytes(field))
        if pending and (len(pending) >= MAX_CHUNK_FIELDS or pending_size + size > MAX_CHUNK_BYTES):
            page = _write_page(root, pending)
            pages.append(page)
            total_bytes += page["sizeBytes"]
            pending, pending_size = [], 3
        pending.append(field)
        pending_size += size
    if pending:
        page = _write_page(root, pending)
        pages.append(page)
        total_bytes += page["sizeBytes"]
    if len(pages) > MAX_CHUNKS or total_bytes > MAX_VIEW_BYTES:
        raise ValueError("CBuffer field view exceeds its bounded storage")
    result.pop("fields")
    result["fieldChunks"], result["fieldCount"] = pages, len(fields)
    return result


def copy_pages(payload: Mapping[str, Any], source: Path, target: Path) -> None:
    """Copy verified immutable dependencies before adopting their manifest."""
    for view in payload.get("semanticViews", []):
        if not isinstance(view, Mapping):
            continue
        chunks = []
        if "fieldChunks" in view:
            read_fields(view, source)
            chunks.extend(view["fieldChunks"])
        if isinstance(view.get("usageReview"), Mapping):
            read_usage_review(view["usageReview"], source)
            chunks.extend(usage_page_descriptors(view["usageReview"]))
        for descriptor in chunks:
            raw, unused_values = read_page(source, descriptor)
            path = _path(target, descriptor["sha256"])
            if path.exists():
                read_page(target, descriptor)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(path, raw)
