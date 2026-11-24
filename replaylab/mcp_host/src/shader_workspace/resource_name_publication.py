"""Durable preparation for capture texture-resource name publication.

The RenderDoc bridge owns one strict symbol-alias transaction.  A texture-name
update must therefore merge the currently committed manifest before the old
transaction is rolled back; loading a texture-only manifest would silently
discard project Shader/CBuffer overlays.  This module performs the bounded
read/merge and persists a recovery state before any bridge mutation occurs.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..symbols.recovery_assets import read_recovery_regular_file
from .io import atomic_write_bytes, canonical_workspace_identity, read_json_object


ALIAS_SCHEMA = "renderdoc-ultra-aliases/v1"
ALIAS_VERSION = 1
LEDGER_SCHEMA = "replaylab-resource-name-publication-operation/v1"
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_ALIAS_ROWS = 100_000
KNOWN_LEDGER_PHASES = frozenset({
    "prepared",
    "old-rolled-back",
    "committed",
    "failed-old-restored",
    "failed-new-rolled-back",
    "recovery-required",
})


def _caller_request_hashes(
    caller_request_sha256: str,
    compatible_caller_request_sha256: Sequence[str],
) -> frozenset[str]:
    primary = str(caller_request_sha256 or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", primary) is None:
        raise ValueError("caller_request_sha256 must be a SHA-256 digest")
    compatible = tuple(compatible_caller_request_sha256 or ())
    if len(compatible) > 8:
        raise ValueError("compatible caller request has too many digests")
    accepted = {primary}
    for value in compatible:
        candidate = str(value or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", candidate) is None:
            raise ValueError("compatible caller request digest is not a SHA-256")
        accepted.add(candidate)
    return frozenset(accepted)


def ledger_phase(ledger: Mapping[str, Any]) -> str:
    phase = str(ledger.get("phase") or "")
    if phase not in KNOWN_LEDGER_PHASES:
        raise RuntimeError(
            "resource-name publication ledger has an unrecognized phase; "
            "manual recovery is required"
        )
    return phase


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _safe_int(value: Any, default: int = -1) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first(value: Mapping[str, Any], names: Sequence[str], default: Any = "") -> Any:
    for name in names:
        if name in value and value[name] not in (None, ""):
            return value[name]
    return default


def _kind(alias: Mapping[str, Any]) -> str:
    value = str(
        _first(alias, ("kind", "bindingKind", "targetKind", "type"), "") or ""
    )
    return value.replace("_", "").replace("-", "").casefold()


def texture_target(alias: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Return the bridge's physical texture target identity."""

    if _kind(alias) != "textureresource":
        return None
    metadata = alias.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    target = (
        _safe_int(_first(alias, ("shaderResourceId",), 0), 0),
        str(alias.get("stage") or "").strip().casefold(),
        str(_first(alias, ("entryPoint", "entry"), "") or ""),
        str(_first(alias, ("rawSha256", "shaderSha256", "blobSha256"), "") or "").lower(),
        _safe_int(_first(alias, ("resourceId", "textureResourceId"), 0), 0),
        _safe_int(_first(alias, ("registerSpace", "space", "fixedBindSetOrSpace"), 0), 0),
        _safe_int(_first(alias, ("bindNumber", "bind", "fixedBindNumber"), -1), -1),
        _safe_int(metadata.get("eventId"), 0),
    )
    if (
        target[0] <= 0 or target[4] <= 0 or target[6] < 0 or target[7] <= 0
        or not target[1] or not target[2] or len(target[3]) != 64
    ):
        raise ValueError("texture alias has an incomplete physical target")
    return target


def texture_physical_target(alias: Mapping[str, Any]) -> tuple[int] | None:
    """Return the capture's physical custom-name key for one texture.

    Live proof remains bound to Shader/EID/stage/register in ``texture_target``.
    RenderDoc's displayed custom resource name, however, is owned by ResourceId;
    an older managed row for another draw/register must therefore be replaced
    when the same physical texture receives current evidence.
    """

    if _kind(alias) != "textureresource":
        return None
    resource_id = _safe_int(
        _first(alias, ("resourceId", "textureResourceId"), 0), 0
    )
    if resource_id <= 0:
        raise ValueError("texture alias has no physical resourceId")
    return (resource_id,)


def read_alias_manifest_exact(
    path_text: str,
    expected_sha256: str,
    capture_id: str,
) -> tuple[dict[str, Any], bytes]:
    """Read the bridge-reported committed manifest without path races."""

    digest = str(expected_sha256 or "").strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("committed alias manifest SHA-256 is invalid")
    if not isinstance(path_text, str) or not path_text or len(path_text) > 32768:
        raise ValueError("committed alias manifest path is unavailable")
    path = Path(path_text)
    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        raise ValueError("committed alias manifest path is not an absolute regular file")
    size = path.stat().st_size
    if size <= 0 or size > MAX_MANIFEST_BYTES:
        raise ValueError("committed alias manifest exceeds the 32 MiB bound")
    raw, reason, unused_consumed = read_recovery_regular_file(
        path.parent, path.name, size
    )
    if raw is None or len(raw) != size:
        raise ValueError(
            "committed alias manifest could not be read safely: %s" % (reason or "changed")
        )
    if _sha256(raw) != digest:
        raise ValueError("committed alias manifest bytes do not match bridge status")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("committed alias manifest is not valid UTF-8 JSON") from exc
    aliases = document.get("aliases") if isinstance(document, Mapping) else None
    if (
        not isinstance(document, dict)
        or document.get("schema") != ALIAS_SCHEMA
        or document.get("schemaVersion") != ALIAS_VERSION
        or document.get("captureId") != capture_id
        or not isinstance(aliases, list)
        or len(aliases) > MAX_ALIAS_ROWS
        or not all(isinstance(item, dict) for item in aliases)
    ):
        raise ValueError("committed alias manifest has an invalid schema or capture binding")
    return document, raw


def merge_alias_manifest(
    capture_id: str,
    previous_aliases: Sequence[Mapping[str, Any]],
    resource_aliases: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], set[tuple[Any, ...]]]:
    """Preserve old project aliases and replace only exact texture targets."""

    if len(previous_aliases) + len(resource_aliases) > MAX_ALIAS_ROWS:
        raise ValueError("merged alias manifest exceeds the 100000-row bound")
    new_targets: set[tuple[int]] = set()
    normalized_new: list[dict[str, Any]] = []
    for raw in resource_aliases:
        if not isinstance(raw, Mapping):
            raise ValueError("resource alias manifest rows must be objects")
        row = dict(raw)
        # Validate the full current live proof, then deduplicate on the actual
        # custom-name ownership key used by qrenderdoc.
        exact_target = texture_target(row)
        target = texture_physical_target(row)
        if exact_target is None or target is None or target in new_targets:
            raise ValueError("resource alias manifest contains a duplicate/invalid target")
        new_targets.add(target)
        normalized_new.append(row)

    replaced_targets: set[tuple[int]] = set()
    merged: list[dict[str, Any]] = []
    for raw in previous_aliases:
        if not isinstance(raw, Mapping):
            raise ValueError("committed alias manifest row is not an object")
        row = dict(raw)
        target = texture_physical_target(row)
        if target is not None and target in new_targets:
            replaced_targets.add(target)
            continue
        merged.append(row)
    merged.extend(normalized_new)
    document = {
        "schema": ALIAS_SCHEMA,
        "schemaVersion": ALIAS_VERSION,
        "captureId": capture_id,
        "aliases": merged,
    }
    return document, replaced_targets


def publication_paths(root: Path, operation_id: str) -> tuple[Path, Path]:
    suffix = hashlib.sha256(str(operation_id).encode("utf-8")).hexdigest()[:32]
    directory = root / "ai" / "resource-name-publications"
    return directory / (suffix + ".manifest.json"), directory / (suffix + ".operation.json")


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> str:
    raw = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("merged alias manifest exceeds the 32 MiB bound")
    atomic_write_bytes(path, raw)
    return _sha256(raw)


def write_ledger(path: Path, ledger: Mapping[str, Any]) -> None:
    raw = (
        json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(raw) > 1024 * 1024:
        raise ValueError("resource-name publication ledger exceeds 1 MiB")
    atomic_write_bytes(path, raw)


def read_ledger(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = read_json_object(path, 1024 * 1024, "resource-name publication ledger")
    if value.get("schema") != LEDGER_SCHEMA or value.get("schemaVersion") != 1:
        raise ValueError("resource-name publication ledger schema is invalid")
    return value


def replay_committed_receipt(
    *,
    workspace_root: Path,
    operation_id: str,
    caller_request_sha256: str,
    compatible_caller_request_sha256: Sequence[str] = (),
) -> dict[str, Any] | None:
    """Return a caller-bound committed receipt without rebuilding live proof."""

    root = Path(workspace_root).resolve()
    unused_manifest, ledger_path = publication_paths(root, operation_id)
    ledger = read_ledger(ledger_path)
    if ledger is None:
        return None
    if ledger.get("operationId") != operation_id:
        raise RuntimeError(
            "resource-name operation id is bound to a different caller request"
        )
    if not ledger.get("callerRequestSha256") or not ledger.get("workspaceRoot"):
        return None
    accepted_caller_hashes = _caller_request_hashes(
        caller_request_sha256, compatible_caller_request_sha256
    )
    if (
        ledger.get("callerRequestSha256") not in accepted_caller_hashes
        or canonical_workspace_identity(str(ledger.get("workspaceRoot") or ""))
        != canonical_workspace_identity(root)
    ):
        raise RuntimeError(
            "resource-name operation id is bound to a different caller request"
        )
    if ledger_phase(ledger) != "committed":
        return None
    result = ledger.get("result")
    if not isinstance(result, Mapping):
        raise RuntimeError("committed resource-name receipt is missing")
    replay = dict(result)
    replay.setdefault("operationId", operation_id)
    replay.setdefault("requestSha256", ledger.get("requestSha256"))
    replay["idempotentReplay"] = True
    return replay
