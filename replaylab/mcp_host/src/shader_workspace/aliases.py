"""Per-user CBuffer alias store shared by the RenderDoc panel and the Host.

An alias is a *display name* for a reflected constant-buffer block or variable. The
captured shader, the decompiled baseline and every compiled candidate keep
their original identifiers; only what the user sees changes.

Aliases normally follow the **constant-block layout**, not one shader or one
capture: the key is the SHA-256 of the block name plus its top-level members
(``name@byteOffset`` in declaration order). Stripped ``unknown`` placeholder
layouts do not carry material semantic identity, so AI aliases for them also
carry a physical Shader scope: original raw Shader SHA-256, optional captured
Shader resource ID, stage, bind, space, and the layout SHA-256. The resource ID
is stored as a decimal string so JSON cannot round it. Explicit user aliases
stay layout-wide and always win.

Store layout (identical for qrenderdoc ``ReplayLabCBufferAliases`` and this
module — change both together). One block layout has exactly one alias file:

    <selected project>/shared/dictionaries/cbuffer-aliases/<layoutSha256>.json

when a project is selected (``config/active-project.json``) -- the names travel
with the project folder, and every .rdc opened under it shows them -- and only
without a project the per-user

    %LOCALAPPDATA%\\ReplayLab\\aliases\\cbuffer\\<layoutSha256>.json

The first time a layout is touched under a project, a per-user file for it
(names made before the project existed) is adopted into the project once and
is not consulted again. There is no merging of two stores: the project file is
the record, and inside it a ``user`` entry always beats an ``ai`` publish.

    {
      "schema": "replaylab-cbuffer-aliases/v2",
      "layoutSha256": "<sha-256 of the layout signature>",
      "block": "cb0",
      "members": [{"name": "v0", "offset": 0}, ...],
      "blockAlias": {"alias": "MaterialConstants", "source": "user",
                     "updatedAt": "..."},
      "updatedAt": "2026-09-03T12:00:00Z",
      "aliases": [
        {"path": "v0", "offset": 0, "alias": "tintAndAlpha",
         "source": "user", "updatedAt": "..."}
      ]
    }

``path`` is the *reflected* variable path RenderDoc shows (dotted:
``light.color`` or ``arr[2]``); ``offset`` is the absolute byte offset of a
leaf inside the block or ``-1`` when unknown. The panel matches an entry by
path, then by offset for leaves.

``source`` is ``"user"`` (typed in the panel) or ``"ai"`` (published by
``rename_shader_workspace_symbols``). A user entry is never overwritten by an
AI entry.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

from .io import atomic_write_bytes, atomic_write_json, canonical_workspace_identity, read_json_object
from . import alias_field_storage

SCHEMA = "replaylab-cbuffer-aliases/v2"
MAX_ALIAS_CHARS = 64
MAX_ENTRIES = 4096
MAX_FILE_BYTES = 4 * 1024 * 1024
# A previous writer checked compact bytes but persisted indentation. This is
# only an import envelope for that legacy format; new manifests remain 4 MiB.
MAX_LEGACY_FILE_BYTES = MAX_FILE_BYTES * 4
UNSCOPED_PLACEHOLDER_LAYOUT = "unscoped-placeholder-layout"
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_CBUFFER_RE = re.compile(
    rf"\bcbuffer\s+(?P<block>{_IDENTIFIER})\s*"
    rf"(?::\s*register\s*\(\s*b(?P<bind>\d+)\s*(?:,\s*space(?P<space>\d+)\s*)?\))?\s*"
    rf"\{{(?P<body>[^{{}}]*)\}}",
    re.DOTALL,
)
_STRUCT_RE = re.compile(
    rf"\bstruct\s+(?P<type>{_IDENTIFIER})\s*\{{(?P<body>[^{{}}]*)\}}\s*;",
    re.DOTALL,
)
_CONSTANT_BUFFER_RE = re.compile(
    rf"\b(?:ConstantBuffer|TextureBuffer)\s*<\s*(?P<type>{_IDENTIFIER})\s*>\s*"
    rf"(?P<block>{_IDENTIFIER})(?:\s*\[[^\]]*\])?\s*"
    rf"(?::\s*register\s*\(\s*b(?P<bind>\d+)\s*(?:,\s*space(?P<space>\d+)\s*)?\))?\s*;",
    re.DOTALL,
)
_DECL_RE = re.compile(
    rf"^\s*(?:(?P<layout>row_major|column_major)\s+)?"
    rf"(?P<type>{_IDENTIFIER}(?:\s*<[^>]*>)?)\s+(?P<name>{_IDENTIFIER})"
    rf"(?P<arrays>(?:\s*\[\s*\d+\s*\])*)\s*"
    rf"(?::\s*packoffset\s*\(\s*c(?P<reg>\d+)(?:\.(?P<comp>[xyzw]))?\s*\))?\s*$"
)
_SCALAR_BYTES = {
    "float": 4, "int": 4, "uint": 4, "bool": 4, "half": 4, "min16float": 4,
    "min16int": 4, "min16uint": 4, "dword": 4, "float16_t": 2, "int16_t": 2,
    "uint16_t": 2, "double": 8, "int64_t": 8, "uint64_t": 8,
}
_TYPE_RE = re.compile(r"^(?P<scalar>[a-z0-9_]+?)(?P<rows>[1-4])?(?:x(?P<cols>[1-4]))?$")
_COMPONENT_INDEX = {"x": 0, "y": 1, "z": 2, "w": 3}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RESOURCE_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_SHADER_STAGES = {
    "vertex", "hull", "domain", "geometry", "fragment", "compute", "task", "mesh",
}
_ALIAS_LOCKS_GUARD = threading.Lock()
_ALIAS_LOCKS: dict[str, tuple[Any, int]] = {}
_ALIAS_LOCK_STATE = threading.local()
_ALIAS_LOCK_RETRY_SECONDS = 5.0


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


PROJECT_STORE_RELATIVE = Path("shared") / "dictionaries" / "cbuffer-aliases"
ACTIVE_PROJECT_STATE_ENV = "RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE"


def _replaylab_data_root() -> Path:
    local = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
    if local:
        return Path(local).expanduser() / "ReplayLab"
    xdg = str(os.environ.get("XDG_DATA_HOME", "") or "").strip()
    return (Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share") / "ReplayLab"


def user_store_root() -> Path:
    """The per-user alias root; ``REPLAYLAB_ALIAS_ROOT`` overrides it (tests, portable setups)."""

    override = str(os.environ.get("REPLAYLAB_ALIAS_ROOT", "") or "").strip()
    if override:
        return Path(override)
    return _replaylab_data_root() / "aliases" / "cbuffer"


def active_project_root() -> Path | None:
    """The selected project's folder, read from the same selection file the Host writes.

    ``REPLAYLAB_ALIAS_PROJECT_ROOT`` pins it (tests). No selection, or a selection
    whose folder is gone, means no project store.
    """

    pinned = str(os.environ.get("REPLAYLAB_ALIAS_PROJECT_ROOT", "") or "").strip()
    if pinned:
        root = Path(pinned).expanduser()
        return root if root.is_dir() else None
    configured = str(os.environ.get(ACTIVE_PROJECT_STATE_ENV, "") or "").strip()
    state = (Path(configured).expanduser() if configured
             else _replaylab_data_root() / "config" / "active-project.json")
    try:
        if not state.is_file() or state.stat().st_size > 64 * 1024:
            return None
        payload = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    root_text = str(payload.get("projectRoot", "") or "").strip()
    if not root_text:
        return None
    root = Path(root_text).expanduser()
    return root if root.is_dir() else None


def project_store_root() -> Path | None:
    root = active_project_root()
    return None if root is None else root / PROJECT_STORE_RELATIVE


def _store_selection() -> tuple[Path, Path | None, Path]:
    """Resolve project/user roots once for one read-modify-write operation."""

    project = project_store_root()
    user = user_store_root()
    return (project if project is not None else user), project, user


def store_root() -> Path:
    """The one alias root in scope: the selected project's share, else the per-user root."""

    selected, unused_project, unused_user = _store_selection()
    return selected


def _alias_lock_key(path: Path) -> str:
    return canonical_workspace_identity(path)


@contextmanager
def _local_alias_lock(lock_key: str, deadline: float):
    with _ALIAS_LOCKS_GUARD:
        entry = _ALIAS_LOCKS.get(lock_key)
        if entry is None:
            lock = threading.Lock()
            references = 0
        else:
            lock, references = entry
        _ALIAS_LOCKS[lock_key] = (lock, references + 1)
    acquired = False
    try:
        acquired = lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if not acquired:
            raise RuntimeError(
                "another thread still owns this CBuffer alias store lock"
            )
        yield
    finally:
        if acquired:
            lock.release()
        with _ALIAS_LOCKS_GUARD:
            current = _ALIAS_LOCKS.get(lock_key)
            if current is not None and current[0] is lock:
                if current[1] <= 1:
                    _ALIAS_LOCKS.pop(lock_key, None)
                else:
                    _ALIAS_LOCKS[lock_key] = (lock, current[1] - 1)


@contextmanager
def alias_store_transaction(
    root: Path | None = None, *, _deadline: float | None = None
):
    """Serialize Host alias read/modify/write transactions across processes.

    The qrenderdoc panel uses atomic file replacement, while MCP publication can
    touch several layout files.  One store lock prevents two Host processes from
    taking the same stale snapshot.  The lock is re-entrant in one thread so a
    multi-layout transaction can call :func:`publish_aliases` for staged files.
    """

    resolved = (store_root() if root is None else Path(root)).resolve()
    lock_key = _alias_lock_key(resolved)
    held = dict(getattr(_ALIAS_LOCK_STATE, "held", {}) or {})
    if lock_key in held:
        held[lock_key] += 1
        _ALIAS_LOCK_STATE.held = held
        try:
            yield resolved
        finally:
            restored = dict(getattr(_ALIAS_LOCK_STATE, "held", {}) or {})
            restored[lock_key] -= 1
            if restored[lock_key] <= 0:
                restored.pop(lock_key, None)
            _ALIAS_LOCK_STATE.held = restored
        return

    # Acquiring several stores in different orders can deadlock across
    # processes.  Multi-root callers sort first; reject an inverse nested
    # acquisition instead of waiting forever.
    if held and lock_key < max(held):
        raise RuntimeError(
            "CBuffer alias store locks must be acquired in canonical order"
        )

    deadline = (
        time.monotonic() + _ALIAS_LOCK_RETRY_SECONDS
        if _deadline is None
        else float(_deadline)
    )
    with _local_alias_lock(lock_key, deadline):
        resolved.mkdir(parents=True, exist_ok=True)
        lock_path = resolved / ".alias-publication.lock"
        stream = lock_path.open("a+b")
        locked = False
        try:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            stream.seek(0)
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "another CBuffer alias publication still owns the store lock"
                        )
                    time.sleep(0.02)
            held[lock_key] = 1
            _ALIAS_LOCK_STATE.held = held
            yield resolved
        finally:
            restored = dict(getattr(_ALIAS_LOCK_STATE, "held", {}) or {})
            restored.pop(lock_key, None)
            _ALIAS_LOCK_STATE.held = restored
            if locked:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            stream.close()


@contextmanager
def alias_store_transactions(roots):
    """Acquire distinct alias roots once, in the cross-process canonical order."""

    ordered: dict[str, Path] = {}
    for root in roots:
        resolved = Path(root).resolve()
        ordered.setdefault(_alias_lock_key(resolved), resolved)
    deadline = time.monotonic() + _ALIAS_LOCK_RETRY_SECONDS
    with ExitStack() as locks:
        for key in sorted(ordered):
            locks.enter_context(
                alias_store_transaction(ordered[key], _deadline=deadline)
            )
        yield tuple(ordered[key] for key in sorted(ordered))


def is_valid_alias(alias: str) -> bool:
    text = str(alias or "")
    if not text.strip() or text != text.strip() or len(text) > MAX_ALIAS_CHARS:
        return False
    return all(ch.isprintable() and ch not in "\r\n\t" for ch in text)


def layout_signature(block: str, members: list[Mapping[str, Any]]) -> str:
    """Canonical text hashed into the layout key; ``""`` when any offset is unknown.

    Keep byte-identical with ``ReplayLabCBufferAliases::LayoutSignature``.
    """

    lines = [str(block or "")]
    for member in members:
        offset = _int(member.get("offset"), -1)
        name = str(member.get("name", "") or "")
        if offset < 0 or not name:
            return ""
        lines.append("%s@%d" % (name, offset))
    return "\n".join(lines)


def layout_sha256(block: str, members: list[Mapping[str, Any]]) -> str:
    signature = layout_signature(block, members)
    if not signature:
        return ""
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def has_placeholder_members(members: list[Mapping[str, Any]]) -> bool:
    """Keep identical to the panel's placeholder-name check.

    A byte offset in an ``unknown`` array cannot identify material semantics.
    The v2 store has no semantic scope, even when its publisher knows the shader.
    """

    return any(str(member.get("name", "") or "").strip().lower() == "unknown"
               or str(member.get("name", "") or "").strip().lower().startswith("unknown[")
               for member in members)


def normalise_shader_scope(
    value: Mapping[str, Any], *, expected_layout_sha256: str = ""
) -> dict[str, Any]:
    """Validate and canonicalise the physical scope of an AI CBuffer alias.

    The digest is the original captured Shader bytecode digest, not a generated
    HLSL or live-edit candidate digest.  A scope is data identity, not workflow
    authorization: it deliberately contains no task, conversation, owner, path,
    or session fields.
    """

    if not isinstance(value, Mapping):
        raise ValueError("shaderScope must be an object")
    raw_sha256 = str(
        value.get("shaderRawSha256", value.get("originalRawSha256", "")) or ""
    ).strip().lower()
    resource_value = value.get("shaderResourceId")
    resource_id = ""
    if resource_value not in (None, ""):
        if not isinstance(resource_value, str):
            raise ValueError("shaderScope.shaderResourceId must be a decimal string")
        resource_id = resource_value.strip()
        if (
            not _RESOURCE_ID_RE.fullmatch(resource_id)
            or int(resource_id, 10) > 0xFFFFFFFFFFFFFFFF
        ):
            raise ValueError(
                "shaderScope.shaderResourceId must be a positive uint64 decimal string"
            )
    stage = str(value.get("stage", "") or "").strip().lower()
    if stage == "pixel":
        stage = "fragment"
    bind = _int(value.get("bind"), -1)
    space = _int(value.get("space"), -1)
    layout = str(value.get("layoutSha256", "") or "").strip().lower()
    expected = str(expected_layout_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(raw_sha256):
        raise ValueError("shaderScope.shaderRawSha256 must be a SHA-256 digest")
    if stage not in _SHADER_STAGES:
        raise ValueError("shaderScope.stage is invalid")
    if bind < 0 or space < 0:
        raise ValueError("shaderScope bind and space must be non-negative")
    if not _SHA256_RE.fullmatch(layout):
        raise ValueError("shaderScope.layoutSha256 must be a SHA-256 digest")
    if expected and layout != expected:
        raise ValueError("shaderScope.layoutSha256 does not match the CBuffer layout")
    result = {
        "shaderRawSha256": raw_sha256,
        "stage": stage,
        "bind": bind,
        "space": space,
        "layoutSha256": layout,
    }
    if resource_id:
        result["shaderResourceId"] = resource_id
    return result


def _clean_shader_scope(
    value: Any, *, expected_layout_sha256: str = ""
) -> dict[str, Any] | None:
    try:
        return normalise_shader_scope(
            value, expected_layout_sha256=expected_layout_sha256
        )
    except (TypeError, ValueError):
        return None


def _same_shader_scope(
    left: Mapping[str, Any] | None, right: Mapping[str, Any] | None
) -> bool:
    if left is None or right is None:
        return left is None and right is None
    # ResourceIds are reassigned when a capture is reopened. They help the
    # panel recognize a live replacement, but are not part of the persistent
    # identity of names attached to the original program. Match the panel's
    # byte-identical scope path, including older records without an ID.
    return all(left.get(field) == right.get(field) for field in (
        "shaderRawSha256", "stage", "bind", "space",
        "layoutSha256"
    ))


def layout_alias_path(layout_sha256_value: str, root: Path | None = None) -> Path:
    value = str(layout_sha256_value or "").strip().lower()
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError("layoutSha256 must be a lowercase SHA-256 hex digest")
    return (store_root() if root is None else root) / ("%s.json" % value)


def _validate_legacy_payload_bytes(raw: bytes) -> dict[str, Any]:
    payload = json.loads(raw.decode("utf-8"))
    if (not isinstance(payload, dict) or payload.get("schema") != SCHEMA
            or len(alias_field_storage.json_bytes(payload)) > MAX_FILE_BYTES):
        raise ValueError("CBuffer alias legacy file exceeds its canonical size limit")
    return payload


def _read_alias_payload(path: Path) -> dict[str, Any]:
    payload = read_json_object(path, MAX_LEGACY_FILE_BYTES, "cbuffer alias store")
    if path.stat().st_size > MAX_FILE_BYTES:
        _validate_legacy_payload_bytes(alias_field_storage.json_bytes(payload))
    return payload


def _adopt_user_file_unlocked(
    layout_sha256_value: str, project: Path, user: Path
) -> None:
    """Move a pre-project per-user alias file into the project share, once.

    Names typed or published before the project existed are not lost, and they
    are not a second store either: after this the project file is the record.
    """

    target = layout_alias_path(layout_sha256_value, project)
    if target.exists():
        return
    source = layout_alias_path(layout_sha256_value, user)
    if not source.is_file():
        return
    entries, block_alias = _read_store(source)
    if not entries and block_alias is None and not _read_semantic_views(source, expand_fields=False):
        return
    payload = _read_alias_payload(source)
    payload["adoptedFrom"] = str(source)
    payload["adoptedAt"] = _now()
    alias_field_storage.copy_pages(payload, source.parent, target.parent)
    serialized = alias_field_storage.json_bytes(payload)
    if len(serialized) > MAX_FILE_BYTES:
        raise ValueError("CBuffer alias manifest exceeds its bounded size")
    atomic_write_bytes(target, serialized)


def _adopt_user_file(layout_sha256_value: str) -> None:
    """Adopt under the same selected-root lock used by every reader/writer."""

    selected, project, user = _store_selection()
    if project is None:
        return
    with alias_store_transactions([selected, user]):
        _adopt_user_file_unlocked(layout_sha256_value, project, user)


def _adopt_user_file_at(layout_sha256_value: str, selected_root: Path) -> None:
    """Adopt only if ``selected_root`` is still the one selected project root."""

    selected, project, user = _store_selection()
    captured = Path(selected_root).resolve()
    if project is None or selected.resolve() != captured:
        return
    with alias_store_transactions([captured, user]):
        _adopt_user_file_unlocked(layout_sha256_value, captured, user)


def _current_store(
    layout_sha256_value: str,
    *,
    include_unscoped_ai: bool = True,
    shader_scope: Mapping[str, Any] | None = None,
    root: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if root is None:
        selected, project, user = _store_selection()
    else:
        selected, project, user = Path(root), None, user_store_root()
    lock_roots = [selected, user] if project is not None else [selected]
    with alias_store_transactions(lock_roots):
        locked_root = Path(selected).resolve()
        if project is not None:
            _adopt_user_file_unlocked(layout_sha256_value, project, user)
        return _read_store(
            layout_alias_path(layout_sha256_value, locked_root),
            include_unscoped_ai=include_unscoped_ai,
            shader_scope=shader_scope,
        )


# --------------------------------------------------------------------------- HLSL parsing


def _type_bytes(type_text: str, layout: str | None) -> tuple[int, bool] | None:
    """Byte size of a scalar/vector/matrix type and whether it is a matrix."""

    match = _TYPE_RE.match(type_text.strip())
    if not match:
        return None
    scalar = _SCALAR_BYTES.get(match.group("scalar"))
    if scalar is None:
        return None
    rows = int(match.group("rows") or 1)
    cols = match.group("cols")
    if cols is None:
        return scalar * rows, False
    columns = int(cols)
    # HLSL packs a matrix as one 16-byte register per column (column_major, the
    # default) or per row (row_major).
    vectors = rows if layout == "row_major" else columns
    return vectors * 16, True


def _parsed_block_members(block: str, bind: int, space: int, body: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    cursor: int | None = 0
    for statement in body.split(";"):
        statement = re.sub(r"//[^\n]*|/\*.*?\*/", "", statement, flags=re.DOTALL).strip()
        if not statement:
            continue
        decl = _DECL_RE.match(statement)
        if not decl:
            cursor = None
            continue
        name = decl.group("name")
        counts = [int(n) for n in re.findall(r"\d+", decl.group("arrays") or "")]
        elements = 1
        for count in counts:
            elements *= max(count, 1)
        sized = _type_bytes(decl.group("type"), decl.group("layout"))
        offset: int | None = None
        if decl.group("reg") is not None:
            offset = int(decl.group("reg")) * 16 + _COMPONENT_INDEX.get(decl.group("comp") or "x", 0) * 4
        if sized is None:
            cursor = None
        else:
            size, is_matrix = sized
            if offset is None and cursor is not None:
                if counts or is_matrix or (cursor % 16) + size > 16:
                    cursor = (cursor + 15) // 16 * 16
                offset = cursor
            if offset is not None:
                stride = (size + 15) // 16 * 16 if (counts or is_matrix) else size
                cursor = offset + (stride * (elements - 1) + size if counts else size)
            else:
                cursor = None
        result.append({
            "block": block, "bind": bind, "space": space, "name": name,
            "offset": offset if offset is not None else -1,
            "leaf": sized is not None and not counts,
            "byteSize": sized[0] if sized is not None and not counts else 0,
        })
    return result


def cbuffer_members(source: str, *, limit: int | None = MAX_ENTRIES) -> list[dict[str, Any]]:
    """Top-level cbuffer variables declared in HLSL with best-effort byte offsets.

    ``offset`` is exact when the member carries ``packoffset`` or when every
    earlier member of the block is a scalar/vector/matrix/array of those; it
    is ``-1`` from the first struct or unknown type onwards (the panel then
    matches that member by name only).
    """

    members: list[dict[str, Any]] = []
    for block_match in _CBUFFER_RE.finditer(source or ""):
        block = block_match.group("block")
        bind = int(block_match.group("bind")) if block_match.group("bind") is not None else -1
        space = int(block_match.group("space")) if block_match.group("space") is not None else (0 if bind >= 0 else -1)
        members.extend(_parsed_block_members(block, bind, space, block_match.group("body")))
        if limit is not None and len(members) >= max(1, int(limit)):
            return members[:max(1, int(limit))]
    # DX12 decompilers frequently preserve typed ConstantBuffer<T> bindings
    # instead of spelling an inline cbuffer. Their struct fields have the same
    # HLSL packing rules and must feed the same panel-alias receipt path.
    structs = {match.group("type"): match.group("body")
               for match in _STRUCT_RE.finditer(source or "")}
    for binding in _CONSTANT_BUFFER_RE.finditer(source or ""):
        body = structs.get(binding.group("type"))
        if body is None:
            continue
        block = binding.group("block")
        bind = int(binding.group("bind")) if binding.group("bind") is not None else -1
        space = int(binding.group("space")) if binding.group("space") is not None else (0 if bind >= 0 else -1)
        members.extend(_parsed_block_members(block, bind, space, body))
        if limit is not None and len(members) >= max(1, int(limit)):
            return members[:max(1, int(limit))]
    return members


# --------------------------------------------------------------------------- store I/O


def _int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _clean_block_alias(
    value: Any, *, expected_layout_sha256: str = ""
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    alias = str(value.get("alias", "") or "")
    if not is_valid_alias(alias):
        return None
    result = {
        "alias": alias,
        "source": "user" if value.get("source") == "user" else "ai",
        "updatedAt": str(value.get("updatedAt", "") or ""),
    }
    scope = _clean_shader_scope(
        value.get("shaderScope"), expected_layout_sha256=expected_layout_sha256
    )
    if (
        result["source"] != "user"
        and "shaderScope" in value
        and scope is None
    ):
        return None
    if scope is not None:
        result["shaderScope"] = scope
    return result


def _read_store_records(
    path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    if not path.is_file():
        return [], [], False
    payload = _read_alias_payload(path)
    if payload.get("schema") != SCHEMA:
        return [], [], False
    members = payload.get("members")
    valid_members = (
        isinstance(members, list) and all(isinstance(member, Mapping) for member in members)
        and layout_sha256(str(payload.get("block") or ""), members) == path.stem)
    placeholder_layout = bool(valid_members and has_placeholder_members(members))
    entries = payload.get("aliases")
    clean: list[dict[str, Any]] = []
    for item in entries if isinstance(entries, list) else []:
        if not isinstance(item, dict):
            continue
        alias = str(item.get("alias", "") or "")
        path_value = str(item.get("path", "") or "")
        if not is_valid_alias(alias) or not path_value:
            continue
        record = {
            "path": path_value,
            "offset": _int(item.get("offset"), -1),
            "alias": alias,
            "source": "user" if item.get("source") == "user" else "ai",
            "updatedAt": str(item.get("updatedAt", "") or ""),
        }
        scope = _clean_shader_scope(
            item.get("shaderScope"), expected_layout_sha256=path.stem
        )
        if (
            record["source"] != "user"
            and "shaderScope" in item
            and scope is None
        ):
            continue
        if scope is not None:
            record["shaderScope"] = scope
        clean.append(record)
        if len(clean) >= MAX_ENTRIES:
            break
    block_aliases: list[dict[str, Any]] = []
    legacy = _clean_block_alias(
        payload.get("blockAlias"), expected_layout_sha256=path.stem
    )
    if legacy is not None:
        block_aliases.append(legacy)
    values = payload.get("blockAliases")
    for value in values if isinstance(values, list) else []:
        item = _clean_block_alias(value, expected_layout_sha256=path.stem)
        if item is not None:
            block_aliases.append(item)
        if len(block_aliases) >= MAX_ENTRIES:
            break
    return clean, block_aliases, placeholder_layout


def _read_store(
    path: Path,
    *,
    include_unscoped_ai: bool = True,
    shader_scope: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    entries, block_aliases, placeholder_layout = _read_store_records(path)
    if include_unscoped_ai:
        return entries, (block_aliases[0] if block_aliases else None)

    requested_scope = _clean_shader_scope(
        shader_scope, expected_layout_sha256=path.stem
    )

    def ai_visible(item: Mapping[str, Any]) -> bool:
        if item.get("source") == "user":
            return True
        item_scope = item.get("shaderScope")
        if isinstance(item_scope, Mapping):
            return requested_scope is not None and _same_shader_scope(
                item_scope, requested_scope
            )
        return not placeholder_layout

    visible = [item for item in entries if ai_visible(item)]
    user_paths = {
        str(item.get("path") or "")
        for item in visible
        if item.get("source") == "user"
    }
    visible = [
        item for item in visible
        if item.get("source") == "user" or item.get("path") not in user_paths
    ]
    user_block = next(
        (item for item in block_aliases if item.get("source") == "user"), None
    )
    if user_block is not None:
        return visible, user_block
    return visible, next(
        (item for item in block_aliases if ai_visible(item)), None
    )


def _read_entries(path: Path) -> list[dict[str, Any]]:
    return _read_store(path)[0]


def read_aliases(
    layout_sha256_value: str,
    *,
    shader_scope: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return _current_store(
        layout_sha256_value,
        include_unscoped_ai=False,
        shader_scope=shader_scope,
    )[0]


def read_block_alias(
    layout_sha256_value: str,
    *,
    shader_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the stored display alias for a block layout, if any."""

    return _current_store(
        layout_sha256_value,
        include_unscoped_ai=False,
        shader_scope=shader_scope,
    )[1]


def _read_semantic_views(path: Path, *, fields_root: Path | None = None,
                         expand_fields: bool = True,
                         shader_scope: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Read bounded display views; malformed metadata never becomes ABI data."""
    from .semantic_usage import clean_usage_review, clean_view_fields

    if not path.is_file():
        return []
    payload = _read_alias_payload(path)
    if payload.get("schema") != SCHEMA:
        return []
    views = payload.get("semanticViews")
    result = []
    for raw in views[:MAX_ENTRIES] if isinstance(views, list) else []:
        if not isinstance(raw, Mapping):
            continue
        scope = _clean_shader_scope(raw.get("shaderScope"), expected_layout_sha256=path.stem)
        digest = str(raw.get("sourceSha256") or "")
        if scope is None or (digest and not _SHA256_RE.fullmatch(digest)):
            continue
        if shader_scope is not None and not _same_shader_scope(scope, shader_scope):
            continue
        usage_review = None
        if "usageReview" in raw and digest:
            try:
                usage_review = alias_field_storage.read_usage_review(
                    raw["usageReview"], fields_root or path.parent, expand=expand_fields)
                if "unusedComponentChunks" not in usage_review:
                    usage_review = clean_usage_review(usage_review)
                else:
                    # Validate the observation header without expanding unchanged immutable pages.
                    clean_usage_review({"schema": usage_review.get("schema"),
                        "evidence": usage_review.get("evidence"), "unusedComponents": []})
            except (TypeError, ValueError):
                if isinstance(raw["usageReview"], Mapping) and "unusedComponentChunks" in raw["usageReview"]:
                    raise  # A broken stored page must not erase previously retained observations.
                usage_review = None
                pass  # Malformed observations never label physical fields unused.
        # A missing/corrupt immutable page must not silently drop a previously
        # valid user's view during the next publication.
        paged = "fieldChunks" in raw
        if paged and not expand_fields:
            alias_field_storage.page_descriptors(raw)
            fields = None
        else:
            field_values = alias_field_storage.read_fields(raw, fields_root or path.parent)
            try:
                fields = ([] if field_values == [] and usage_review is not None
                          and "unusedComponentChunks" in usage_review
                          else clean_view_fields(field_values, usage_review))
            except (TypeError, ValueError):
                if paged:
                    raise
                continue
        result.append({"shaderScope": scope, "sourceSha256": digest,
                       "source": "user" if raw.get("source") == "user" else "ai",
                       "updatedAt": str(raw.get("updatedAt") or ""), "fields": fields})
        if usage_review is not None:
            result[-1]["usageReview"] = usage_review
        captured_sha = str(raw.get("capturedShaderRawSha256") or "")
        if _SHA256_RE.fullmatch(captured_sha):
            result[-1]["capturedShaderRawSha256"] = captured_sha
        if paged and not expand_fields:
            result[-1].pop("fields")
            result[-1]["fieldChunks"] = raw["fieldChunks"]
            result[-1]["fieldCount"] = raw["fieldCount"]
    return result


def read_semantic_view(layout_sha256_value: str, *, shader_scope: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Return the exact Shader's persisted display projection, user view first."""
    scope = _clean_shader_scope(shader_scope, expected_layout_sha256=layout_sha256_value)
    if scope is None:
        return None
    selected, project, user = _store_selection()
    with alias_store_transactions([selected, user] if project is not None else [selected]):
        if project is not None:
            _adopt_user_file_unlocked(layout_sha256_value, project, user)
        views = _read_semantic_views(layout_alias_path(layout_sha256_value, selected),
                                     shader_scope=scope, expand_fields=False)
        view = next((view for view in views if view["source"] == "user"), views[-1] if views else None)
        if view is not None:
            from .semantic_usage import clean_view_fields
            if "usageReview" in view:
                view["usageReview"] = alias_field_storage.read_usage_review(view["usageReview"], selected)
            view["fields"] = clean_view_fields(alias_field_storage.read_fields(view, selected), view.get("usageReview"))
            view.pop("fieldChunks", None)
            view.pop("fieldCount", None)
        return view


def _previous_semantic_views(layout_sha256_value: str, *, captured_sha256: str,
                             stage: str, bind: int, space: int,
                             active_sha256: str) -> list[dict[str, Any]]:
    """Return bounded review leads, never aliases to apply to new bytecode.

    Looking only at the current physical layout keeps this cheap. An unchanged
    register layout is not proof of unchanged meaning, so callers must keep
    these records separate from the current ``semanticView``.
    """
    if not _SHA256_RE.fullmatch(captured_sha256):
        return []
    selected, project, user = _store_selection()
    with alias_store_transactions([selected, user] if project is not None else [selected]):
        if project is not None:
            _adopt_user_file_unlocked(layout_sha256_value, project, user)
        views = _read_semantic_views(layout_alias_path(layout_sha256_value, selected), expand_fields=False)
    candidates = [view for view in views
                  if view.get("capturedShaderRawSha256") == captured_sha256
                  and view["shaderScope"]["stage"] == stage
                  and view["shaderScope"]["bind"] == bind
                  and view["shaderScope"]["space"] == space
                  and view["shaderScope"]["shaderRawSha256"] != active_sha256]
    candidates.sort(key=lambda view: view.get("updatedAt", ""), reverse=True)
    candidates = candidates[:2]
    for view in candidates:
        view["fields"] = alias_field_storage.read_fields(view, selected)
    return [{**view, "reviewRequired": True, "appliedToCurrentShader": False,
             "reason": "candidate-bytecode-changed", "fields": view["fields"][:128],
             "totalFields": len(view["fields"]), "fieldsTruncated": len(view["fields"]) > 128}
            for view in candidates]


def prune_semantic_history(
    views: list[dict[str, Any]],
    *,
    shader_scope: Mapping[str, Any],
    captured_shader_sha256: str,
    protected_shader_sha256: list[str],
    keep_previous: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select obsolete AI revisions without reading field pages or changing records.

    The Host supplies live/project references after a complete naming review.
    A shared layout alone is not a lineage: another captured Shader, binding,
    stage or descriptor space keeps its names. Legacy views without a captured
    bytecode identity also remain until their lineage can be established.
    """
    scope = normalise_shader_scope(shader_scope)
    if not _SHA256_RE.fullmatch(captured_shader_sha256):
        raise ValueError("semantic retention requires the captured Shader SHA256")
    if (not isinstance(protected_shader_sha256, list)
            or len(protected_shader_sha256) > MAX_ENTRIES
            or any(not isinstance(value, str) or not _SHA256_RE.fullmatch(value)
                   for value in protected_shader_sha256)):
        raise ValueError("protected Shader identities must be a bounded list of SHA256 digests")
    if type(keep_previous) is not int or keep_previous not in (0, 1):
        raise ValueError("semantic retention may keep zero or one previous revision")
    protected = set(protected_shader_sha256)
    protected.update((scope["shaderRawSha256"], captured_shader_sha256))
    candidates = []
    for index, view in enumerate(views):
        candidate_scope = view.get("shaderScope")
        if (view.get("source") != "ai"
                or view.get("capturedShaderRawSha256") != captured_shader_sha256
                or not isinstance(candidate_scope, Mapping)
                or any(candidate_scope.get(field) != scope[field]
                       for field in ("stage", "bind", "space", "layoutSha256"))
                or not _SHA256_RE.fullmatch(str(candidate_scope.get("shaderRawSha256") or ""))
                or candidate_scope.get("shaderRawSha256") in protected):
            continue
        candidates.append(index)
    # Publication order breaks same-second timestamp ties deterministically.
    newest = sorted(candidates, key=lambda i: (str(views[i].get("updatedAt") or ""), i),
                    reverse=True)[:keep_previous]
    removed_indices = set(candidates).difference(newest)
    return ([view for i, view in enumerate(views) if i not in removed_indices],
            [view for i, view in enumerate(views) if i in removed_indices])


def _publish_aliases_unlocked(
    block: str,
    members: list[Mapping[str, Any]],
    entries: list[Mapping[str, Any]],
    source: str = "ai",
    *,
    block_alias: str | None = None,
    shader_scope: Mapping[str, Any] | None = None,
    semantic_fields: list[Mapping[str, Any]] | None = None,
    semantic_source_sha256: str = "",
    semantic_captured_shader_sha256: str = "",
    semantic_retention: Mapping[str, Any] | None = None,
    semantic_usage_review: Mapping[str, Any] | None = None,
    root: Path | None = None,
    semantic_fields_root: Path | None = None,
    store_kind: str,
) -> dict[str, Any]:
    """Merge ``entries`` into the alias file of this block layout.

    Each entry needs ``path`` and ``alias``; ``offset`` is optional. Existing
    ``user`` entries for the same path win over an ``ai`` publish; a ``user``
    publish replaces the layout-wide user record. Placeholder layouts accept AI
    names only with an exact physical Shader scope; the scope is stored beside
    each AI record so another Shader with the same ``unknown[N]`` layout cannot
    consume it.
    """

    key = layout_sha256(block, members)
    if not key:
        raise ValueError("block layout is incomplete (a member offset is unknown)")
    path = layout_alias_path(key, root)
    # The public wrapper owns the selected store lock.  Private staging roots
    # are transaction-local and intentionally call this helper without taking
    # another cross-process lock.
    existing, existing_block_aliases, unused_placeholder = _read_store_records(path)
    fields_root = semantic_fields_root or path.parent
    existing_semantic_views = _read_semantic_views(path, fields_root=fields_root, expand_fields=False)
    source = "user" if source == "user" else "ai"
    placeholder_layout = has_placeholder_members(members)
    scope = None
    if (source != "user" or semantic_fields is not None) and shader_scope is not None:
        # The layout is Host-derived from the exact reflected block above. Let
        # callers omit the duplicate field, but never replace a non-empty,
        # conflicting digest.
        scope_value = dict(shader_scope)
        if not str(scope_value.get("layoutSha256") or "").strip():
            scope_value["layoutSha256"] = key
        scope = normalise_shader_scope(
            scope_value, expected_layout_sha256=key
        )
    if scope is not None:
        from .semantic_usage import clean_view_fields
        for view in existing_semantic_views:
            if _same_shader_scope(view["shaderScope"], scope):
                # Validate the view being replaced/preserved. Other revisions
                # retain their exact immutable references without loading them.
                review = view.get("usageReview")
                if review is not None:
                    review = alias_field_storage.read_usage_review(review, fields_root)
                clean_view_fields(alias_field_storage.read_fields(view, fields_root), review)
    history_cleanup = None
    if semantic_retention is not None:
        from .semantic_usage import clean_usage_review, clean_view_fields

        if (not isinstance(semantic_retention, Mapping)
                or "protectedShaderRawSha256" not in semantic_retention
                or set(semantic_retention).difference(("protectedShaderRawSha256", "keepPrevious"))):
            raise ValueError("semantic retention requires explicit protected Shader identities")
        if source != "ai" or scope is None or not (semantic_fields or semantic_usage_review is not None):
            raise ValueError("semantic retention requires a complete non-empty AI semantic view")
        # Invalid new fields must never cause old names to be removed. Pruning
        # is staged in memory before merging so obsolete aliases cannot exhaust
        # the entry budget needed by the replacement publication.
        clean_view_fields(semantic_fields or [], semantic_usage_review)
        if semantic_usage_review is not None:
            clean_usage_review(semantic_usage_review)
        existing_semantic_views, removed_views = prune_semantic_history(
            existing_semantic_views,
            shader_scope=scope,
            captured_shader_sha256=semantic_captured_shader_sha256,
            protected_shader_sha256=semantic_retention["protectedShaderRawSha256"],
            keep_previous=semantic_retention.get("keepPrevious", 0),
        )
        def scope_key(value: Any) -> tuple | None:
            if not isinstance(value, Mapping):
                return None
            return tuple(value.get(field) for field in (
                "shaderRawSha256", "stage", "bind", "space", "layoutSha256"))

        retained_scopes = {scope_key(view["shaderScope"]) for view in existing_semantic_views}
        removed_scopes = {scope_key(view["shaderScope"]) for view in removed_views} - retained_scopes

        def obsolete_alias(item: Mapping[str, Any]) -> bool:
            return (item.get("source") == "ai"
                    and scope_key(item.get("shaderScope")) in removed_scopes)

        kept_aliases = [item for item in existing if not obsolete_alias(item)]
        kept_blocks = [item for item in existing_block_aliases if not obsolete_alias(item)]
        history_cleanup = {"removedViews": len(removed_views),
                           "removedAliases": len(existing) - len(kept_aliases),
                           "removedBlockAliases": len(existing_block_aliases) - len(kept_blocks)}
        existing, existing_block_aliases = kept_aliases, kept_blocks
    blocked = source != "user" and placeholder_layout and scope is None
    written: list[str] = []
    kept_user: list[str] = []
    rejected: list[dict[str, str]] = []
    block_alias_written = False
    block_alias_kept_user = False
    now = _now()
    for item in entries:
        member = str(item.get("path", "") or "")
        alias = str(item.get("alias", "") or "")
        label = "%s.%s" % (block, member) if block else member
        if not member or not is_valid_alias(alias):
            rejected.append({"path": label, "reason": "alias must be 1-%d printable characters" % MAX_ALIAS_CHARS})
            continue
        user_index = next((
            i for i, existing_item in enumerate(existing)
            if existing_item["path"] == member
            and existing_item["source"] == "user"
        ), -1)
        if user_index >= 0 and source != "user":
            kept_user.append(label)
            continue
        if blocked:
            rejected.append({"path": label, "reason": UNSCOPED_PLACEHOLDER_LAYOUT,
                             "reasonId": UNSCOPED_PLACEHOLDER_LAYOUT})
            continue
        record = {
            "path": member,
            "offset": _int(item.get("offset"), -1),
            "alias": alias,
            "source": source,
            "updatedAt": now,
        }
        if scope is not None:
            record["shaderScope"] = scope
        if source == "user":
            index = user_index
        else:
            index = next((
                i for i, existing_item in enumerate(existing)
                if existing_item["path"] == member
                and existing_item["source"] != "user"
                and _same_shader_scope(
                    existing_item.get("shaderScope"), scope
                )
            ), -1)
        if index >= 0:
            existing[index] = record
        else:
            if len(existing) >= MAX_ENTRIES:
                rejected.append({
                    "path": label,
                    "reason": "alias store already holds %d entries" % MAX_ENTRIES,
                })
                continue
            existing.append(record)
        written.append(label)
    if block_alias is not None:
        requested_block_alias = str(block_alias or "")
        if not is_valid_alias(requested_block_alias):
            rejected.append({
                "path": str(block or ""),
                "reason": "block alias must be 1-%d printable characters" % MAX_ALIAS_CHARS,
            })
        elif source != "user" and any(
            item["source"] == "user" for item in existing_block_aliases
        ):
            block_alias_kept_user = True
        elif blocked:
            rejected.append({"path": str(block or ""), "reason": UNSCOPED_PLACEHOLDER_LAYOUT,
                             "reasonId": UNSCOPED_PLACEHOLDER_LAYOUT})
        else:
            record = {
                "alias": requested_block_alias,
                "source": source,
                "updatedAt": now,
            }
            if scope is not None:
                record["shaderScope"] = scope
            if source == "user":
                # Keep scoped AI history, but retain only one layout-wide block
                # alias.  The user record remains the global highest priority.
                existing_block_aliases = [
                    item for item in existing_block_aliases
                    if isinstance(item.get("shaderScope"), Mapping)
                ]
                existing_block_aliases.append(record)
            else:
                index = next((
                    i for i, existing_item in enumerate(existing_block_aliases)
                    if existing_item["source"] != "user"
                    and _same_shader_scope(
                        existing_item.get("shaderScope"), scope
                    )
                ), -1)
                if index >= 0:
                    existing_block_aliases[index] = record
                else:
                    existing_block_aliases.append(record)
            block_alias_written = True
    semantic_view = None
    semantic_written = False
    semantic_kept_user = False
    if semantic_fields is not None or semantic_usage_review is not None:
        from .semantic_usage import clean_usage_review, clean_view_fields

        usage_review = clean_usage_review(semantic_usage_review) if semantic_usage_review is not None else None
        fields = semantic_fields
        if fields is None:
            prior_view = next((view for view in existing_semantic_views
                               if _same_shader_scope(view["shaderScope"], scope)
                               and view.get("sourceSha256") == semantic_source_sha256), None)
            fields = alias_field_storage.read_fields(prior_view, fields_root) if prior_view else []
        fields = clean_view_fields(fields, usage_review)
        if usage_review is not None:
            if not _SHA256_RE.fullmatch(semantic_source_sha256):
                raise ValueError("CBuffer usage review requires the current source SHA256")
            named_offsets = {component["offset"] for field in fields for component in field["components"]}
            if any(component["offset"] in named_offsets for component in usage_review["unusedComponents"]):
                raise ValueError("CBuffer unused components overlap semantic names")
        if scope is None:
            raise ValueError("semantic CBuffer display views require an exact Shader scope")
        if semantic_source_sha256 and not _SHA256_RE.fullmatch(semantic_source_sha256):
            raise ValueError("semantic source SHA256 must be a lowercase digest")
        existing_user = next((view for view in existing_semantic_views
                              if view["source"] == "user" and _same_shader_scope(view["shaderScope"], scope)), None)
        if existing_user is not None and source != "user":
            semantic_view, semantic_kept_user = dict(existing_user), True
            semantic_view["fields"] = alias_field_storage.read_fields(existing_user, fields_root)
            semantic_view.pop("fieldChunks", None)
            semantic_view.pop("fieldCount", None)
        else:
            semantic_view = {"shaderScope": scope, "sourceSha256": semantic_source_sha256,
                             "source": source, "updatedAt": now, "fields": fields}
            if usage_review is not None:
                semantic_view["usageReview"] = usage_review
            if semantic_captured_shader_sha256:
                if not _SHA256_RE.fullmatch(semantic_captured_shader_sha256):
                    raise ValueError("captured semantic Shader SHA256 must be a lowercase digest")
                semantic_view["capturedShaderRawSha256"] = semantic_captured_shader_sha256
            existing_semantic_views = [view for view in existing_semantic_views
                                      if not (view["source"] == source and _same_shader_scope(view["shaderScope"], scope))]
            if len(existing_semantic_views) >= MAX_ENTRIES:
                raise ValueError("semantic view store is full")
            existing_semantic_views.append(semantic_view)
            semantic_written = True
    history_removed = bool(history_cleanup and any(history_cleanup.values()))
    if history_cleanup is not None:
        history_cleanup["remainingViews"] = len(existing_semantic_views)
    if written or block_alias_written or semantic_written or history_removed:
        payload = {
            "schema": SCHEMA,
            "layoutSha256": key,
            "block": str(block or ""),
            "members": [{"name": str(m.get("name")), "offset": _int(m.get("offset"), -1)} for m in members],
            "updatedAt": now,
            "aliases": existing,
        }
        unscoped_block_alias = next((
            item for item in existing_block_aliases
            if not isinstance(item.get("shaderScope"), Mapping)
        ), None)
        scoped_block_aliases = [
            item for item in existing_block_aliases
            if isinstance(item.get("shaderScope"), Mapping)
        ]
        if unscoped_block_alias is not None:
            payload["blockAlias"] = unscoped_block_alias
        if scoped_block_aliases:
            payload["blockAliases"] = scoped_block_aliases
        if existing_semantic_views:
            payload["semanticViews"] = [alias_field_storage.store_view(view, fields_root)
                                        for view in existing_semantic_views]
        # Measure exactly the bytes that are written. The former compact check
        # followed by an indented writer could create an unreadable >4 MiB file.
        serialized = alias_field_storage.json_bytes(payload)
        if len(serialized) > MAX_FILE_BYTES:
            raise ValueError("CBuffer alias manifest exceeds its bounded size")
        atomic_write_bytes(path, serialized)
    selected_block_alias = next((
        item for item in existing_block_aliases if item["source"] == "user"
    ), None)
    if selected_block_alias is None and scope is not None:
        selected_block_alias = next((
            item for item in existing_block_aliases
            if item["source"] != "user"
            and _same_shader_scope(item.get("shaderScope"), scope)
        ), None)
    if selected_block_alias is None and not placeholder_layout:
        selected_block_alias = next((
            item for item in existing_block_aliases
            if item["source"] != "user" and not item.get("shaderScope")
        ), None)
    return {
        "block": str(block or ""),
        "layoutSha256": key,
        "path": str(path),
        "store": store_kind,
        "written": written,
        "keptUser": kept_user,
        "blockAliasWritten": block_alias_written,
        "blockAliasKeptUser": block_alias_kept_user,
        "blockAlias": selected_block_alias,
        "shaderScope": scope,
        "rejected": rejected,
        "total": len(existing),
        # Receipts describe the persisted view; they do not echo thousands of
        # field records into every provider message and durable operation log.
        "semanticView": (alias_field_storage.store_view(semantic_view, fields_root)
                         if semantic_view is not None else None),
        "semanticViewWritten": semantic_written,
        "semanticViewKeptUser": semantic_kept_user,
        "semanticFieldsWritten": len(semantic_fields or []) if semantic_written else 0,
        "semanticFieldsKeptUser": len(semantic_fields or []) if semantic_kept_user else 0,
        "applied": bool(written or block_alias_written or semantic_written or history_removed),
        **({"semanticHistoryCleanup": history_cleanup} if history_cleanup is not None else {}),
        **({"reasonId": UNSCOPED_PLACEHOLDER_LAYOUT} if blocked else {}),
    }


def publish_aliases(
    block: str,
    members: list[Mapping[str, Any]],
    entries: list[Mapping[str, Any]],
    source: str = "ai",
    *,
    block_alias: str | None = None,
    shader_scope: Mapping[str, Any] | None = None,
    semantic_fields: list[Mapping[str, Any]] | None = None,
    semantic_source_sha256: str = "",
    semantic_captured_shader_sha256: str = "",
    semantic_retention: Mapping[str, Any] | None = None,
    semantic_usage_review: Mapping[str, Any] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Thread/process-safe wrapper around one alias-file merge.

    ``root`` is reserved for Host-owned staged transactions.  Normal callers
    omit it and retain the active-project adoption behavior.
    """

    if root is None:
        selected_root, project_root, user_root = _store_selection()
        store_kind = "project" if project_root is not None else "user"
    else:
        # Capture the selection once so the receipt cannot change meaning if
        # another thread switches projects during this publication.
        unused_selected, current_project, user_root = _store_selection()
        selected_root, project_root = Path(root), None
        store_kind = "project" if (
            current_project is not None
            and current_project.resolve() == selected_root.resolve()
        ) else "user"
    lock_roots = (
        [selected_root, user_root] if project_root is not None else [selected_root]
    )
    with alias_store_transactions(lock_roots):
        locked_root = Path(selected_root).resolve()
        if project_root is not None:
            _adopt_user_file_unlocked(
                layout_sha256(block, members), project_root, user_root
            )
        return _publish_aliases_unlocked(
            block,
            members,
            entries,
            source,
            block_alias=block_alias,
            shader_scope=shader_scope,
            semantic_fields=semantic_fields,
            semantic_source_sha256=semantic_source_sha256,
            semantic_captured_shader_sha256=semantic_captured_shader_sha256,
            semantic_retention=semantic_retention,
            semantic_usage_review=semantic_usage_review,
            root=locked_root,
            store_kind=store_kind,
        )


def blocks_from_source(
    source: str, *, limit: int | None = MAX_ENTRIES
) -> list[dict[str, Any]]:
    """Declared cbuffer blocks with their top-level members, in source order."""

    blocks: list[dict[str, Any]] = []
    for member in cbuffer_members(source, limit=limit):
        if not blocks or blocks[-1]["block"] != member["block"]:
            blocks.append({"block": member["block"], "bind": member["bind"], "space": member["space"], "members": []})
        blocks[-1]["members"].append(member)
    return blocks


def aliases_from_renames(
    baseline_source: str, renames: Mapping[str, str],
    captured_identity: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Per-block alias publications implied by a Host rename map.

    Members declared inside a ``cbuffer`` and the block name itself are aliases
    the panel can show; functions, textures and locals remain only in the
    workspace ``ai/aliases.json``. A block whose layout
    cannot be computed (struct members, unknown types) is reported, not
    published.
    """

    publications: list[dict[str, Any]] = []
    for block in blocks_from_source(baseline_source):
        block_alias = str(
            renames.get("cbuffer:" + block["block"], renames.get(block["block"], ""))
            or ""
        )
        target = block
        captured_targets = []
        if captured_identity is not None:
            captured_targets = [item for item in captured_identity.get("blocks", [])
                                if isinstance(item, Mapping)
                                and item.get("bind") == block["bind"]
                                and item.get("space") == block["space"]]
            if len(captured_targets) == 1:
                target = captured_targets[0]
        entries = []
        unmapped = []
        for member in block["members"]:
            alias = renames.get(block["block"] + "." + member["name"],
                                  renames.get(member["name"]))
            if not alias or alias == member["name"]:
                continue
            path = member["name"]
            offset = member["offset"] if member["leaf"] else -1
            if captured_identity is not None:
                matches = [leaf for leaf in target.get("leaves", [])
                           if isinstance(leaf, Mapping)
                           and leaf.get("offset") == offset
                           and leaf.get("byteSize") == member["byteSize"]]
                if (len(captured_targets) != 1
                        or target.get("memberMappingAvailable") is not True
                        or not member["leaf"] or member["byteSize"] <= 0
                        or len(matches) != 1 or not matches[0].get("path")):
                    unmapped.append(member["name"])
                    continue
                path = str(matches[0]["path"])
            entries.append({"path": path, "offset": offset, "alias": alias,
                            "byteSize": member["byteSize"], "original": member["name"]})
        if not entries and not block_alias and not unmapped:
            continue
        target_members = [{"name": m["name"], "offset": m["offset"]} for m in target["members"]]
        target_key = layout_sha256(target["block"], target_members)
        if captured_identity is not None and len(captured_targets) != 1:
            target_key = ""
        publications.append({
            "block": target["block"],
            "sourceBlock": block["block"],
            "bind": block["bind"],
            "space": block["space"],
            "members": target_members,
            "layoutSha256": target_key,
            "entries": entries,
            "blockAlias": block_alias or None,
            "unmapped": unmapped,
            "capturedIdentityMatched": captured_identity is not None and len(captured_targets) == 1,
            # "annotated" (debug reflection), "placeholder" (stripped DXIL rows the
            # panel shows as unknown[N]) or "none": says why a member is unmapped.
            "memberMappingKind": (str(target.get("memberMappingKind") or "")
                                  if captured_identity is not None else "source"),
        })
    return publications


def evidence_for_source(source: str, captured_identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Aliases already stored for every cbuffer block declared in ``source``.

    ``user`` entries were typed in RenderDoc's constant-buffer panel and are
    the names an organizer must reuse; ``ai`` entries are earlier publishes.
    """

    blocks: list[dict[str, Any]] = []
    for block in blocks_from_source(source):
        target = block
        matched = False
        if captured_identity is not None:
            targets = [item for item in captured_identity.get("blocks", [])
                       if isinstance(item, Mapping) and item.get("bind") == block["bind"]
                       and item.get("space") == block["space"]]
            if len(targets) == 1:
                target, matched = targets[0], True
        key = layout_sha256(target["block"], target["members"])
        if captured_identity is not None and not matched:
            key = ""
        record: dict[str, Any] = {"block": block["block"], "layoutSha256": key or None, "user": [], "ai": []}
        if captured_identity is not None:
            record.update({"capturedBlock": target["block"] if matched else None,
                           "capturedIdentityMatched": matched, "unmapped": []})
        if key:
            try:
                scope = None
                if captured_identity is not None and matched:
                    scope = _clean_shader_scope({
                        "shaderRawSha256": captured_identity.get("rawSha256"),
                        "stage": captured_identity.get("stage"),
                        "bind": target.get("bind"),
                        "space": target.get("space"),
                        "layoutSha256": key,
                    }, expected_layout_sha256=key)
                allow_ai = not has_placeholder_members(target["members"]) or scope is not None
                if not allow_ai:
                    record["aiSuppressedReasonId"] = UNSCOPED_PLACEHOLDER_LAYOUT
                entries, block_alias = _current_store(
                    key, include_unscoped_ai=False, shader_scope=scope
                )
                semantic_view = read_semantic_view(key, shader_scope=scope)
                if captured_identity is not None:
                    active_blocks = [item for item in captured_identity.get("activeDisplayBlocks", [])
                                     if isinstance(item, Mapping) and item.get("bind") == block["bind"]
                                     and item.get("space", 0) == block["space"]]
                    if len(active_blocks) == 1:
                        active_block = active_blocks[0]
                        active_key = layout_sha256(active_block["block"], active_block["members"])
                        active_view = read_semantic_view(active_key, shader_scope={
                            "shaderRawSha256": captured_identity.get("activeDisplayShaderRawSha256"),
                            "stage": captured_identity.get("stage"), "bind": block["bind"],
                            "space": block["space"], "layoutSha256": active_key}) if active_key else None
                        if active_view is not None:
                            semantic_view = active_view
                            record["semanticViewBasis"] = "verified-active-reflection"
                        elif active_key:
                            history = _previous_semantic_views(
                                active_key,
                                captured_sha256=str(captured_identity.get("rawSha256") or ""),
                                stage=str(captured_identity.get("stage") or ""),
                                bind=block["bind"], space=block["space"],
                                active_sha256=str(captured_identity.get("activeDisplayShaderRawSha256") or ""))
                            if history:
                                record["semanticViewHistory"] = history
                                record["semanticViewHistoryInstruction"] = (
                                    "Review these previous field meanings against the current source, "
                                    "then publish a new exact-source semantic view. They are not "
                                    "current panel names or proof of completed naming.")
                if semantic_view is not None:
                    record["semanticView"] = semantic_view
                for entry in entries:
                    if not allow_ai and entry["source"] != "user":
                        continue
                    source_path = entry["path"]
                    if captured_identity is not None:
                        leaves = [leaf for leaf in target.get("leaves", [])
                                  if leaf.get("path") == entry["path"]]
                        sources = [member for member in block["members"]
                                   if len(leaves) == 1 and member["leaf"]
                                   and member["offset"] == leaves[0].get("offset")
                                   and member["byteSize"] == leaves[0].get("byteSize")]
                        if target.get("memberMappingAvailable") is not True or len(sources) != 1:
                            record["unmapped"].append({"path": entry["path"], "alias": entry["alias"], "source": entry["source"]})
                            continue
                        source_path = sources[0]["name"]
                    record["user" if entry["source"] == "user" else "ai"].append(
                        {"path": source_path, "alias": entry["alias"]}
                    )
                if block_alias is not None and (allow_ai or block_alias["source"] == "user"):
                    record["blockAlias"] = block_alias
            except (OSError, ValueError) as exc:
                record["error"] = str(exc)
        else:
            record["error"] = "layout incomplete: member offsets unknown"
        blocks.append(record)
    return {"schema": SCHEMA, "storeRoot": str(store_root()), "blocks": blocks}
