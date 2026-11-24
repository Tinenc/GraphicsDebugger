"""Unpacked-asset CBuffer layout hints for the fixed SPIRV-Cross HLSL repair.

A hint carries, per ``"space:bind"``, the member layout an unpacked asset
declares for a captured constant buffer, plus the relation that produced it.
It is *evidence for naming and completeness*, never ABI authority: the repair
admits a block only when the captured byte size and every DXIL-accessed lane
agree with it, and the provenance record keeps a hash-bound copy so a later
status check can reproduce the exact repair without the live asset index.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import re
from typing import Any

ASSET_LAYOUT_SCHEMA = "renderdoc-ultra-asset-cbuffer-layout-hint/v1"
MAX_HINT_BYTES = 2 * 1024 * 1024
MAX_BLOCKS = 256
MAX_FIELDS = 4096
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RELATION_KEYS = (
    "candidateId", "assetPath", "displayName", "provenance", "rawSha256Equal",
    "executableHashEqual", "layoutHashEqual", "metadataTrusted",
    "physicalIdentityProgramVerified", "retriever", "score",
)


class AssetLayoutHintError(ValueError):
    """The hint is malformed or exceeds its fixed bounds."""


def _int(value: Any, label: str, *, minimum: int = 0, maximum: int = 1 << 31) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise AssetLayoutHintError("%s is invalid" % label)
    return value


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def hint_sha256(hint: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(hint)).hexdigest()


def normalize_layout_hint(raw: Any) -> dict[str, Any]:
    """Validate and canonicalize a hint; returns a fresh, bounded dict."""

    if not isinstance(raw, Mapping):
        raise AssetLayoutHintError("asset layout hint must be an object")
    if raw.get("schema") != ASSET_LAYOUT_SCHEMA or raw.get("schemaVersion") != 1:
        raise AssetLayoutHintError("asset layout hint schema is unsupported")
    relation_raw = raw.get("relation")
    if not isinstance(relation_raw, Mapping):
        raise AssetLayoutHintError("asset layout hint relation is missing")
    relation: dict[str, Any] = {}
    for key in _RELATION_KEYS:
        if key not in relation_raw:
            continue
        value = relation_raw[key]
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            relation[key] = value
        elif isinstance(value, str):
            if len(value) > 2048 or "\x00" in value:
                raise AssetLayoutHintError("asset layout relation %s is invalid" % key)
            relation[key] = value
        else:
            raise AssetLayoutHintError("asset layout relation %s is invalid" % key)
    blocks_raw = raw.get("blocks")
    if not isinstance(blocks_raw, Mapping) or not blocks_raw:
        raise AssetLayoutHintError("asset layout hint has no blocks")
    if len(blocks_raw) > MAX_BLOCKS:
        raise AssetLayoutHintError("asset layout hint block count exceeds its limit")
    blocks: dict[str, Any] = {}
    for key in sorted(blocks_raw):
        if not re.fullmatch(r"\d{1,5}:\d{1,5}", str(key)):
            raise AssetLayoutHintError("asset layout block key %r is invalid" % str(key)[:32])
        block = blocks_raw[key]
        if not isinstance(block, Mapping):
            raise AssetLayoutHintError("asset layout block %s is not an object" % key)
        byte_size = _int(block.get("byteSize"), "asset layout block byteSize", minimum=1)
        fields_raw = block.get("fields")
        if not isinstance(fields_raw, Sequence) or isinstance(fields_raw, (str, bytes)) or not fields_raw:
            raise AssetLayoutHintError("asset layout block %s has no fields" % key)
        if len(fields_raw) > MAX_FIELDS:
            raise AssetLayoutHintError("asset layout block %s field count exceeds its limit" % key)
        fields = []
        for field in fields_raw:
            if not isinstance(field, Mapping):
                raise AssetLayoutHintError("asset layout field is not an object")
            name = str(field.get("name", "") or "")
            if not _IDENTIFIER.fullmatch(name) or len(name) > 128:
                raise AssetLayoutHintError("asset layout field name %r is invalid" % name[:32])
            fields.append({
                "name": name,
                "byteOffset": _int(field.get("byteOffset"), "asset layout field byteOffset"),
                "byteSize": _int(field.get("byteSize"), "asset layout field byteSize", minimum=1),
                "baseType": str(field.get("baseType", "") or "")[:32].casefold(),
                "rows": _int(field.get("rows", 1), "asset layout field rows", minimum=1, maximum=4),
                "columns": _int(field.get("columns", 1), "asset layout field columns", minimum=1, maximum=4),
                "elements": _int(field.get("elements", 1), "asset layout field elements", minimum=1, maximum=65536),
            })
        fields.sort(key=lambda item: item["byteOffset"])
        entry: dict[str, Any] = {"byteSize": byte_size, "fields": fields}
        block_name = block.get("name")
        if isinstance(block_name, str) and _IDENTIFIER.fullmatch(block_name) and len(block_name) <= 128:
            entry["name"] = block_name
        blocks[str(key)] = entry
    hint = {"schema": ASSET_LAYOUT_SCHEMA, "schemaVersion": 1, "relation": relation, "blocks": blocks}
    if len(canonical_bytes(hint)) > MAX_HINT_BYTES:
        raise AssetLayoutHintError("asset layout hint exceeds its byte bound")
    return hint


def _first(mapping: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _field_from_asset(field: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(field, Mapping):
        return None
    if _first(field, "members", "fields"):
        return None  # structs are not flattened here; the block falls back
    base = str(_first(field, "baseType", "type", default="") or "").strip().casefold()
    base = base.rsplit(".", 1)[-1]
    base = re.sub(r"\d+(x\d+)?$", "", base) or base
    return {
        "name": str(field.get("name", "") or ""),
        "byteOffset": _first(field, "byteOffset", "offset", default=-1),
        "byteSize": _first(field, "byteSize", "size", default=0),
        "baseType": base,
        "rows": _first(field, "rows", default=1),
        "columns": _first(field, "columns", "cols", default=1),
        "elements": _first(field, "elements", default=1) or 1,
    }


def hint_from_asset_blocks(blocks: Sequence[Mapping[str, Any]], relation: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build a normalized hint from asset ``constantBlocks``/``constantBuffers`` rows."""

    converted: dict[str, Any] = {}
    for block in blocks or ():
        if not isinstance(block, Mapping):
            continue
        space = _first(block, "space", "registerSpace", default=0)
        bind = _first(block, "bind", "bindNumber", "slot", default=None)
        if bind is None:
            continue
        fields = [_field_from_asset(item) for item in (_first(block, "fields", "variables", default=()) or ())]
        if not fields or any(item is None for item in fields):
            continue
        entry = {"byteSize": _first(block, "byteSize", "size", default=0), "fields": fields}
        name = block.get("name")
        if isinstance(name, str):
            entry["name"] = name
        converted["%s:%s" % (space, bind)] = entry
    if not converted:
        return None
    try:
        return normalize_layout_hint({
            "schema": ASSET_LAYOUT_SCHEMA, "schemaVersion": 1,
            "relation": dict(relation), "blocks": converted,
        })
    except AssetLayoutHintError:
        return None


def hint_from_relation_view(relation_view: Mapping[str, Any]) -> dict[str, Any] | None:
    """Pick the strongest asset candidate of a relation view and hint from it.

    Order: a program-verified candidate (same raw/executable bytes), then a
    layout-hash match with trusted metadata.  Weaker candidates are left to
    the retriever/AI matching flow, which records its decisions as rules.
    """

    candidates = relation_view.get("assetCandidates") if isinstance(relation_view, Mapping) else None
    if not isinstance(candidates, list):
        return None
    ranked: list[tuple[int, float, Mapping[str, Any]]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        identity = candidate.get("identityRelation") or {}
        cbuffer = candidate.get("cbufferEvidence") or {}
        layout_relation = cbuffer.get("layoutRelation") or {}
        asset_evidence = cbuffer.get("asset") or {}
        if identity.get("physicalIdentityProgramVerified") is True:
            tier = 3
        elif layout_relation.get("layoutHashEqual") is True and asset_evidence.get("metadataTrusted") is True:
            tier = 2
        elif identity.get("selected") is True and asset_evidence.get("blocks"):
            # A reviewed/rule-selected match: the AI (or a rule it created)
            # already judged this asset to be the shader's source of names.
            tier = 1
        else:
            continue
        score = identity.get("score")
        ranked.append((tier, float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else 0.0, candidate))
    ranked.sort(key=lambda item: (-item[0], -item[1]))
    for tier, score, candidate in ranked:
        record = candidate.get("assetRecord") or {}
        identity = candidate.get("identityRelation") or {}
        basis = identity.get("basis") or {}
        cbuffer = candidate.get("cbufferEvidence") or {}
        relation = {
            "candidateId": candidate.get("candidateId"),
            "assetPath": record.get("assetPath"),
            "displayName": record.get("displayName"),
            "provenance": record.get("provenance"),
            "rawSha256Equal": basis.get("rawHashEqual") is True,
            "executableHashEqual": basis.get("executableHashEqual") is True,
            "layoutHashEqual": (cbuffer.get("layoutRelation") or {}).get("layoutHashEqual") is True,
            "metadataTrusted": (cbuffer.get("asset") or {}).get("metadataTrusted") is True,
            "physicalIdentityProgramVerified": tier == 3,
            "retriever": {3: "symbol-database-identity", 2: "symbol-database-layout-hash",
                          1: "symbol-database-selected-match"}[tier],
            "score": score,
        }
        hint = hint_from_asset_blocks((cbuffer.get("asset") or {}).get("blocks") or [], relation)
        if hint is not None:
            return hint
    return None
