"""Retriever + memory that connects a captured shader to unpacked assets.

Strict identity (hash equality) is one signal among several, not the gate:
unpacked trees differ by engine, version, compression and completeness, so
this module ranks *documents* — materials, material instances, shader sources
— against what the capture shows (bound texture names, shader stage, pass
hints, the shader's own resource/cbuffer names) and returns per-signal
evidence.  The AI makes the final call through ``submit`` and that decision
is remembered per game build, per shader identity (raw hash) and per
structural signature, so the next capture or the next shader with the same
shape never repeats the search.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
from typing import Any, Callable

RETRIEVAL_SCHEMA = "renderdoc-ultra-shader-asset-retrieval/v1"
RESOLUTION_SCHEMA = "renderdoc-ultra-shader-asset-resolution/v1"
RESOLUTION_DB_SCHEMA_VERSION = 1
MAX_RESOLUTION_EVIDENCE_BYTES = 1024 * 1024
MAX_TOKENS = 128
MAX_CANDIDATES = 25
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSET_ID = re.compile(r"^[0-9a-f]{16,128}$")
_MATERIAL_CLASSES = ("Material", "MaterialInstanceConstant", "MaterialInstanceDynamic",
                     "MaterialFunction", "MaterialFunctionInstance")


class ShaderAssetRetrievalError(RuntimeError):
    """Invalid retrieval/resolution input."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def structural_key(stage: str, constant_block_sizes: Sequence[int], texture_count: int,
                   sampler_count: int, uav_count: int, entry_point: str = "") -> str:
    """A hash-free shape signature: same layout ⇒ likely the same material family."""

    material = {
        "stage": str(stage or ""),
        "cbufferSizes": sorted(int(size) for size in constant_block_sizes),
        "textures": int(texture_count),
        "samplers": int(sampler_count),
        "uavs": int(uav_count),
        "entryPoint": str(entry_point or ""),
    }
    return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()


def abi_block_shape_sha256(block: Mapping[str, Any]) -> str:
    """Hash a physical CBuffer layout without its API-specific slot or names.

    Register/descriptor locations routinely change between DX11, DX12, OpenGL
    and Vulkan builds.  Member aliases may follow a verified material family to
    a new slot only when the current reflected byte envelope and every reflected
    leaf have the same physical shape.  Names and ``space:bind`` are deliberately
    excluded; offsets, sizes and scalar/matrix/array layout remain binding facts.
    """

    if not isinstance(block, Mapping):
        return ""
    try:
        block_size = int(block.get("byteSize") or 0)
    except (TypeError, ValueError):
        return ""
    raw_fields = block.get("fields")
    if not isinstance(raw_fields, Sequence) or isinstance(raw_fields, (str, bytes)):
        raw_fields = block.get("leaves")
    if block_size <= 0 or not isinstance(raw_fields, Sequence) or isinstance(
        raw_fields, (str, bytes)
    ):
        return ""
    fields: list[dict[str, Any]] = []
    for raw in raw_fields:
        if not isinstance(raw, Mapping):
            return ""
        try:
            offset = int(raw.get("byteOffset", raw.get("offset", -1)))
            size = int(raw.get("byteSize", raw.get("size", 0)))
            rows = int(raw.get("rows", 1) or 1)
            columns = int(raw.get("columns", raw.get("cols", 1)) or 1)
            elements = int(raw.get("elements", 1) or 1)
            array_stride = int(raw.get("arrayByteStride", 0) or 0)
            matrix_stride = int(raw.get("matrixByteStride", 0) or 0)
        except (TypeError, ValueError):
            return ""
        if (
            offset < 0 or size <= 0 or offset + size > block_size
            or rows <= 0 or columns <= 0 or elements <= 0
            or array_stride < 0 or matrix_stride < 0
        ):
            return ""
        base = str(raw.get("baseType") or raw.get("type") or "").casefold()
        base = base.rsplit(".", 1)[-1]
        base = re.sub(r"\d+(?:x\d+)?$", "", base) or base
        fields.append({
            "byteOffset": offset,
            "byteSize": size,
            "baseType": base[:32],
            "rows": rows,
            "columns": columns,
            "elements": elements,
            "arrayByteStride": array_stride,
            "matrixByteStride": matrix_stride,
            "rowMajor": raw.get("rowMajor") is True,
        })
    if not fields:
        return ""
    fields.sort(key=lambda item: (
        item["byteOffset"], item["byteSize"], item["baseType"],
        item["rows"], item["columns"], item["elements"],
    ))
    return hashlib.sha256(_canonical({
        "byteSize": block_size,
        "fields": fields,
    }).encode("utf-8")).hexdigest()


def bound_texture_names(names: Sequence[str]) -> list[dict[str, Any]]:
    """Report the debug names a capture carries for its bound textures, verbatim.

    Nothing here interprets them. A list of prefixes and suffixes that "mean"
    diffuse or normal is a naming convention borrowed from some other game, and
    the moment the Host applies one it has decided something about this game that
    nobody checked -- while a name it dismisses as generic may be the one name
    that identifies the asset. The names are handed to the Agent as they came off
    the GPU, alongside the pixels and the constant-buffer values, and the Agent
    decides which of them mean anything here.
    """

    seen: set[str] = set()
    reported: list[dict[str, Any]] = []
    for raw in names:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        # The only transformation is undoing RenderDoc's own decoration, which is
        # the Host's format, not the game's: "Texture2D 1234 <name>" and paths.
        stem = re.sub(r"^(?:Texture2D|Texture|RenderTarget|Tex)\s+\d+\s+", "",
                      name.replace("\\", "/").rsplit("/", 1)[-1])
        reported.append({"texture": name, "name": stem})
        if len(reported) >= MAX_TOKENS:
            break
    return reported


class ShaderAssetResolutionStore:
    """Durable lookup table of AI/rule/identity decisions (positive and negative)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS resolutions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_key TEXT NOT NULL,
                shader_raw_sha256 TEXT NOT NULL,
                structural_key TEXT NOT NULL,
                stage TEXT NOT NULL,
                entry_point TEXT NOT NULL DEFAULT '',
                asset_relative_path TEXT NOT NULL,
                asset_content_sha256 TEXT NOT NULL DEFAULT '',
                asset_class TEXT NOT NULL DEFAULT '',
                catalog_sha256 TEXT NOT NULL DEFAULT '',
                negative INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL,
                decided_by TEXT NOT NULL,
                rationale TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                decided_at TEXT NOT NULL,
                UNIQUE(game_key, shader_raw_sha256, stage, entry_point, asset_relative_path)
            );
            CREATE INDEX IF NOT EXISTS resolutions_structural_idx
                ON resolutions(game_key, structural_key, negative);
            CREATE TABLE IF NOT EXISTS member_rules (
                game_key TEXT NOT NULL,
                family_key TEXT NOT NULL,
                space INTEGER NOT NULL,
                bind INTEGER NOT NULL,
                byte_offset INTEGER NOT NULL,
                byte_size INTEGER NOT NULL,
                name TEXT NOT NULL,
                base_type TEXT NOT NULL DEFAULT 'float',
                columns INTEGER NOT NULL DEFAULT 1,
                confidence REAL NOT NULL,
                source TEXT NOT NULL,
                shader_raw_sha256 TEXT NOT NULL DEFAULT '',
                decided_at TEXT NOT NULL,
                PRIMARY KEY(game_key, family_key, space, bind, byte_offset)
            );
            CREATE TABLE IF NOT EXISTS member_rule_sets (
                game_key TEXT NOT NULL,
                family_key TEXT NOT NULL,
                abi_shape_sha256 TEXT NOT NULL,
                semantic_block TEXT NOT NULL DEFAULT '',
                block_byte_size INTEGER NOT NULL,
                source_space INTEGER NOT NULL,
                source_bind INTEGER NOT NULL,
                byte_offset INTEGER NOT NULL,
                byte_size INTEGER NOT NULL,
                name TEXT NOT NULL,
                base_type TEXT NOT NULL DEFAULT 'float',
                columns INTEGER NOT NULL DEFAULT 1,
                elements INTEGER NOT NULL DEFAULT 1,
                confidence REAL NOT NULL,
                source TEXT NOT NULL,
                shader_raw_sha256 TEXT NOT NULL DEFAULT '',
                asset_version_sha256 TEXT NOT NULL DEFAULT '',
                asset_resolution_hmac_sha256 TEXT NOT NULL DEFAULT '',
                host_proof_hmac_sha256 TEXT NOT NULL DEFAULT '',
                decided_at TEXT NOT NULL,
                PRIMARY KEY(
                    game_key, family_key, abi_shape_sha256,
                    semantic_block, byte_offset
                )
            );
            CREATE INDEX IF NOT EXISTS member_rule_sets_family_idx
                ON member_rule_sets(game_key, family_key, abi_shape_sha256);
            """
        )
        existing_rule_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(member_rule_sets)")
        }
        for column, declaration in (
            ("asset_version_sha256", "TEXT NOT NULL DEFAULT ''"),
            ("asset_resolution_hmac_sha256", "TEXT NOT NULL DEFAULT ''"),
            ("host_proof_hmac_sha256", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in existing_rule_columns:
                connection.execute(
                    "ALTER TABLE member_rule_sets ADD COLUMN %s %s"
                    % (column, declaration)
                )
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES('schema', ?)", (RESOLUTION_SCHEMA,)
        )
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES('schemaVersion', ?)",
            (str(RESOLUTION_DB_SCHEMA_VERSION),),
        )
        return connection

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        evidence_error = ""
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
            if not isinstance(evidence, dict):
                raise ValueError("stored evidence is not an object")
        except (TypeError, ValueError) as error:
            # Older versions sliced the JSON string at 64 KiB. Preserve that
            # decision as a historical lead without letting one damaged row
            # break all later asset investigations.
            evidence = {}
            evidence_error = str(error)[:200]
        result = {
            "resolutionId": int(row["id"]),
            "gameKey": row["game_key"],
            "shaderRawSha256": row["shader_raw_sha256"],
            "structuralKey": row["structural_key"],
            "stage": row["stage"],
            "entryPoint": row["entry_point"],
            "assetRelativePath": row["asset_relative_path"],
            "assetContentSha256": row["asset_content_sha256"],
            "assetClass": row["asset_class"],
            "catalogSha256": row["catalog_sha256"],
            "negative": bool(row["negative"]),
            "confidence": float(row["confidence"]),
            "decidedBy": row["decided_by"],
            "rationale": row["rationale"],
            "evidence": evidence,
            "decidedAt": row["decided_at"],
        }
        if evidence_error:
            result["evidenceReadError"] = evidence_error
        return result

    def lookup(self, game_key: str, shader_raw_sha256: str, structural: str,
               stage: str, entry_point: str = "") -> dict[str, Any]:
        """Exact-shader decisions first, then same-shape decisions as suggestions."""

        with closing(self._connect()) as db:
            exact = [self._row(row) for row in db.execute(
                "SELECT * FROM resolutions WHERE game_key=? AND shader_raw_sha256=? AND stage=? "
                "AND entry_point=? ORDER BY negative, confidence DESC, decided_at DESC",
                (game_key, shader_raw_sha256, stage, entry_point),
            )]
            similar = [self._row(row) for row in db.execute(
                "SELECT * FROM resolutions WHERE game_key=? AND structural_key=? AND negative=0 "
                "AND shader_raw_sha256<>? ORDER BY confidence DESC, decided_at DESC LIMIT 10",
                (game_key, structural, shader_raw_sha256),
            )]
        positives = [item for item in exact if not item["negative"]]
        return {
            "resolved": bool(positives),
            "resolution": positives[0] if positives else None,
            # Several assets can legitimately explain one shader (material
            # instance + parent material + a shared function); all positives
            # are returned so partial evidence is not lost.
            "resolutions": positives,
            "rejected": [item for item in exact if item["negative"]],
            "sameShapeSuggestions": similar,
        }

    def record(self, *, game_key: str, shader_raw_sha256: str, structural: str, stage: str,
               entry_point: str, asset_relative_path: str, asset_content_sha256: str,
               asset_class: str, catalog_sha256: str, negative: bool, confidence: float,
               decided_by: str, rationale: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
        if not _SHA256.fullmatch(shader_raw_sha256 or ""):
            raise ShaderAssetRetrievalError("shader raw SHA-256 is invalid")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0.0 <= float(confidence) <= 1.0:
            raise ShaderAssetRetrievalError("confidence must be between 0 and 1")
        rationale = str(rationale or "")[:2000]
        try:
            payload = json.dumps(dict(evidence), ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError, OverflowError) as error:
            raise ShaderAssetRetrievalError("resolution evidence is not valid finite JSON") from error
        if len(payload.encode("utf-8")) > MAX_RESOLUTION_EVIDENCE_BYTES:
            raise ShaderAssetRetrievalError("resolution evidence exceeds 1 MiB")
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO resolutions(game_key, shader_raw_sha256, structural_key, stage, entry_point, "
                "asset_relative_path, asset_content_sha256, asset_class, catalog_sha256, negative, "
                "confidence, decided_by, rationale, evidence_json, decided_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(game_key, shader_raw_sha256, stage, entry_point, asset_relative_path) DO UPDATE SET "
                "structural_key=excluded.structural_key, asset_content_sha256=excluded.asset_content_sha256, "
                "asset_class=excluded.asset_class, catalog_sha256=excluded.catalog_sha256, negative=excluded.negative, "
                "confidence=excluded.confidence, decided_by=excluded.decided_by, rationale=excluded.rationale, "
                "evidence_json=excluded.evidence_json, decided_at=excluded.decided_at",
                (game_key, shader_raw_sha256, structural, stage, entry_point, asset_relative_path,
                 asset_content_sha256, asset_class, catalog_sha256, 1 if negative else 0, float(confidence),
                 decided_by, rationale, payload, _utc_now()),
            )
            db.commit()
            row = db.execute(
                "SELECT * FROM resolutions WHERE game_key=? AND shader_raw_sha256=? AND stage=? AND entry_point=? "
                "AND asset_relative_path=?",
                (game_key, shader_raw_sha256, stage, entry_point, asset_relative_path),
            ).fetchone()
        return self._row(row)

    def record_member_rules(
        self, *, game_key: str, family_keys: Sequence[str],
        members: Sequence[Mapping[str, Any]], source: str, confidence: float,
        shader_raw_sha256: str = "",
        family_authorities: Mapping[str, Mapping[str, Any]] | None = None,
        proof_signer: Callable[[Mapping[str, Any]], str] | None = None,
    ) -> int:
        """Remember confirmed CBuffer member names per material family and offset.

        A later shader resolved to the same family (an instance of the same
        parent material, or the same material in another capture) receives
        these names through the fixed repair's layout hint, even when another
        API assigns a different slot, after its reflected ABI shape matches.
        A higher-confidence rule replaces a lower one; equal confidence keeps
        the newest.
        """

        written = 0
        with closing(self._connect()) as db:
            for family in family_keys:
                for member in members:
                    try:
                        space = int(member.get("space", 0) or 0)
                        bind = int(member.get("bind"))
                        offset = int(member.get("byteOffset"))
                        size = int(member.get("byteSize"))
                    except (TypeError, ValueError):
                        continue
                    name = str(member.get("name") or "")
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or size <= 0 or offset < 0 or bind < 0:
                        continue
                    columns = max(1, min(4, size // 4)) if size in (4, 8, 12, 16) else 1
                    try:
                        requested_columns = int(member.get("columns") or columns)
                        elements = int(member.get("elements") or 1)
                        block_byte_size = int(member.get("blockByteSize") or 0)
                    except (TypeError, ValueError):
                        requested_columns, elements, block_byte_size = columns, 1, 0
                    columns = max(1, min(4, requested_columns))
                    elements = max(1, min(65536, elements))
                    base_type = str(member.get("baseType") or "float")[:16]
                    existing = db.execute(
                        "SELECT confidence FROM member_rules WHERE game_key=? AND family_key=? AND space=? AND bind=? AND byte_offset=?",
                        (game_key, family, space, bind, offset),
                    ).fetchone()
                    if existing is not None and float(existing["confidence"]) > float(confidence):
                        continue
                    db.execute(
                        "INSERT INTO member_rules(game_key, family_key, space, bind, byte_offset, byte_size, name, base_type, "
                        "columns, confidence, source, shader_raw_sha256, decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(game_key, family_key, space, bind, byte_offset) DO UPDATE SET byte_size=excluded.byte_size, "
                        "name=excluded.name, base_type=excluded.base_type, columns=excluded.columns, confidence=excluded.confidence, "
                        "source=excluded.source, shader_raw_sha256=excluded.shader_raw_sha256, decided_at=excluded.decided_at",
                        (game_key, family, space, bind, offset, size, name, base_type, columns, float(confidence),
                         str(source)[:64], shader_raw_sha256, _utc_now()),
                    )
                    abi_shape = str(member.get("abiShapeSha256") or "").lower()
                    semantic_block = str(member.get("semanticBlock") or "")[:128]
                    if (
                        _SHA256.fullmatch(abi_shape)
                        and block_byte_size > 0
                        and (not semantic_block or re.fullmatch(
                            r"[A-Za-z_][A-Za-z0-9_]*", semantic_block
                        ))
                    ):
                        authority = (
                            family_authorities.get(family)
                            if isinstance(family_authorities, Mapping) else None
                        )
                        authority = authority if isinstance(authority, Mapping) else {}
                        rule_material = {
                            "gameKey": str(game_key),
                            "familyKey": str(family),
                            "abiShapeSha256": abi_shape,
                            "semanticBlock": semantic_block,
                            "blockByteSize": block_byte_size,
                            "sourceSpace": space,
                            "sourceBind": bind,
                            "byteOffset": offset,
                            "byteSize": size,
                            "name": name,
                            "baseType": base_type,
                            "columns": columns,
                            "elements": elements,
                            "confidence": float(confidence),
                            "source": str(source)[:64],
                            "shaderRawSha256": str(shader_raw_sha256).lower(),
                            "assetVersionSha256": str(
                                authority.get("assetVersionSha256") or ""
                            ).lower(),
                            "assetResolutionHmacSha256": str(
                                authority.get("assetResolutionHmacSha256") or ""
                            ).lower(),
                        }
                        host_proof = str(
                            proof_signer(rule_material) if proof_signer else ""
                        ).lower()
                        if host_proof and _SHA256.fullmatch(host_proof) is None:
                            raise ShaderAssetRetrievalError(
                                "member-rule Host proof signer returned an invalid receipt"
                            )
                        previous = db.execute(
                            "SELECT confidence FROM member_rule_sets WHERE game_key=? AND family_key=? "
                            "AND abi_shape_sha256=? AND semantic_block=? AND byte_offset=?",
                            (game_key, family, abi_shape, semantic_block, offset),
                        ).fetchone()
                        if previous is None or float(previous["confidence"]) <= float(confidence):
                            db.execute(
                                "INSERT INTO member_rule_sets(game_key, family_key, abi_shape_sha256, semantic_block, "
                                "block_byte_size, source_space, source_bind, byte_offset, byte_size, name, base_type, "
                                "columns, elements, confidence, source, shader_raw_sha256, asset_version_sha256, "
                                "asset_resolution_hmac_sha256, host_proof_hmac_sha256, decided_at) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                                "ON CONFLICT(game_key, family_key, abi_shape_sha256, semantic_block, byte_offset) "
                                "DO UPDATE SET block_byte_size=excluded.block_byte_size, source_space=excluded.source_space, "
                                "source_bind=excluded.source_bind, byte_size=excluded.byte_size, name=excluded.name, "
                                "base_type=excluded.base_type, columns=excluded.columns, elements=excluded.elements, "
                                "confidence=excluded.confidence, source=excluded.source, "
                                "shader_raw_sha256=excluded.shader_raw_sha256, "
                                "asset_version_sha256=excluded.asset_version_sha256, "
                                "asset_resolution_hmac_sha256=excluded.asset_resolution_hmac_sha256, "
                                "host_proof_hmac_sha256=excluded.host_proof_hmac_sha256, "
                                "decided_at=excluded.decided_at",
                                (
                                    game_key, family, abi_shape, semantic_block,
                                    block_byte_size, space, bind, offset, size, name,
                                    base_type, columns, elements, float(confidence),
                                    str(source)[:64], shader_raw_sha256,
                                    rule_material["assetVersionSha256"],
                                    rule_material["assetResolutionHmacSha256"],
                                    host_proof, _utc_now(),
                                ),
                            )
                    written += 1
            db.commit()
        return written

    def member_rules(
        self,
        game_key: str,
        family_keys: Sequence[str],
        *,
        constant_blocks: Sequence[Mapping[str, Any]] | None = None,
        proof_verifier: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return family rules projected onto the current reflected ABI.

        With no ``constant_blocks`` this preserves the legacy exact-slot view.
        When reflection is supplied, v2 rules follow one verified physical block
        shape to its current slot.  An ambiguous duplicate shape is used only
        when its semantic block name or original slot selects exactly one block.
        """

        result: dict[str, dict[int, dict[str, Any]]] = {}
        with closing(self._connect()) as db:
            targets: list[dict[str, Any]] = []
            if constant_blocks is not None:
                for block in constant_blocks:
                    if not isinstance(block, Mapping):
                        continue
                    try:
                        space = int(block.get("space", 0) or 0)
                        bind = int(block.get("bind"))
                        byte_size = int(block.get("byteSize") or 0)
                    except (TypeError, ValueError):
                        continue
                    shape = abi_block_shape_sha256(block)
                    if bind < 0 or space < 0 or byte_size <= 0 or not shape:
                        continue
                    targets.append({
                        "key": "%d:%d" % (space, bind),
                        "space": space,
                        "bind": bind,
                        "byteSize": byte_size,
                        "name": str(block.get("name") or ""),
                        "abiShapeSha256": shape,
                    })
            for family in family_keys:
                if constant_blocks is not None and targets:
                    rows_v2 = db.execute(
                        "SELECT * FROM member_rule_sets WHERE game_key=? AND family_key=? "
                        "ORDER BY abi_shape_sha256, semantic_block, byte_offset",
                        (game_key, family),
                    ).fetchall()
                    grouped: dict[tuple[str, str, int, int, int], list[sqlite3.Row]] = {}
                    for row in rows_v2:
                        rule_material = {
                            "gameKey": str(row["game_key"]),
                            "familyKey": str(row["family_key"]),
                            "abiShapeSha256": str(row["abi_shape_sha256"]),
                            "semanticBlock": str(row["semantic_block"]),
                            "blockByteSize": int(row["block_byte_size"]),
                            "sourceSpace": int(row["source_space"]),
                            "sourceBind": int(row["source_bind"]),
                            "byteOffset": int(row["byte_offset"]),
                            "byteSize": int(row["byte_size"]),
                            "name": str(row["name"]),
                            "baseType": str(row["base_type"]),
                            "columns": int(row["columns"]),
                            "elements": int(row["elements"]),
                            "confidence": float(row["confidence"]),
                            "source": str(row["source"]),
                            "shaderRawSha256": str(row["shader_raw_sha256"]).lower(),
                            "assetVersionSha256": str(row["asset_version_sha256"]).lower(),
                            "assetResolutionHmacSha256": str(
                                row["asset_resolution_hmac_sha256"]
                            ).lower(),
                            "hostProofHmacSha256": str(
                                row["host_proof_hmac_sha256"]
                            ).lower(),
                        }
                        if proof_verifier is not None and not proof_verifier(rule_material):
                            continue
                        group_key = (
                            str(row["abi_shape_sha256"]),
                            str(row["semantic_block"]),
                            int(row["block_byte_size"]),
                            int(row["source_space"]),
                            int(row["source_bind"]),
                        )
                        grouped.setdefault(group_key, []).append(row)
                    for (
                        shape, semantic, block_size, source_space, source_bind
                    ), rows in grouped.items():
                        candidates = [
                            item for item in targets
                            if item["abiShapeSha256"] == shape
                            and item["byteSize"] == block_size
                        ]
                        if len(candidates) > 1 and semantic:
                            named = [
                                item for item in candidates
                                if item["name"].casefold() == semantic.casefold()
                            ]
                            if len(named) == 1:
                                candidates = named
                        if len(candidates) > 1:
                            exact_slot = [
                                item for item in candidates
                                if item["space"] == source_space
                                and item["bind"] == source_bind
                            ]
                            if len(exact_slot) == 1:
                                candidates = exact_slot
                        if len(candidates) != 1:
                            continue
                        target = candidates[0]
                        projected = result.setdefault(target["key"], {})
                        for row in rows:
                            offset = int(row["byte_offset"])
                            if offset in projected:
                                continue
                            projected[offset] = {
                                "name": row["name"],
                                "byteOffset": offset,
                                "byteSize": int(row["byte_size"]),
                                "baseType": row["base_type"],
                                "columns": int(row["columns"]),
                                "elements": int(row["elements"]),
                                "confidence": float(row["confidence"]),
                                "source": row["source"],
                                "familyKey": family,
                                "semanticBlock": semantic,
                                "abiShapeSha256": shape,
                                "mappedFrom": {
                                    "space": source_space, "bind": source_bind,
                                },
                            }
                # The legacy exact-slot table predates Host provenance.  It is
                # kept for manual/backward-compatible callers, but a production
                # projection that supplied a proof verifier must never let an
                # unsigned row bypass the v2 family/ABI receipt.
                rows = [] if proof_verifier is not None else db.execute(
                    "SELECT * FROM member_rules WHERE game_key=? AND family_key=? ORDER BY space, bind, byte_offset",
                    (game_key, family),
                ).fetchall()
                for row in rows:
                    key = "%d:%d" % (int(row["space"]), int(row["bind"]))
                    if constant_blocks is not None and key not in {
                        item["key"] for item in targets
                    }:
                        continue
                    block = result.setdefault(key, {})
                    if int(row["byte_offset"]) in block:
                        continue
                    block[int(row["byte_offset"])] = {
                        "name": row["name"], "byteOffset": int(row["byte_offset"]), "byteSize": int(row["byte_size"]),
                        "baseType": row["base_type"], "columns": int(row["columns"]), "elements": 1,
                        "confidence": float(row["confidence"]), "source": row["source"], "familyKey": family,
                    }
        return {key: [block[offset] for offset in sorted(block)] for key, block in result.items()}

    def forget(self, game_key: str, shader_raw_sha256: str, stage: str, entry_point: str) -> int:
        with closing(self._connect()) as db:
            cursor = db.execute(
                "DELETE FROM resolutions WHERE game_key=? AND shader_raw_sha256=? AND stage=? AND entry_point=?",
                (game_key, shader_raw_sha256, stage, entry_point),
            )
            db.commit()
            return int(cursor.rowcount)


def _material_document(catalog: Any, asset: Mapping[str, Any]) -> dict[str, Any]:
    """Compact, AI-readable view of one candidate and its cooked export data."""

    metadata = asset.get("metadata") or {}
    document: dict[str, Any] = {
        "assetId": asset.get("assetId"),
        "relativePath": asset.get("relativePath"),
        "className": metadata.get("className", ""),
        "engine": metadata.get("engine", ""),
        "names": list(metadata.get("names") or [])[:128],
        "textures": [str(item).rsplit(".", 1)[-1] for item in (metadata.get("textures") or [])][:64],
        "parentMaterials": [str(item).rsplit(".", 1)[-1] for item in (metadata.get("parentMaterials") or [])][:8],
        "materialFunctions": [str(item).rsplit(".", 1)[-1] for item in (metadata.get("materialFunctions") or [])][:32],
        "shaderTypes": [],
        "vertexFactories": [],
        "defines": [],
        "hasInlineShaderMap": False,
        "exportParseStatus": "missing",
    }
    # The cooked export sibling carries shader types/defines; find it through
    # its reverse relationship (uexp -> cooked-export-of -> uasset).
    # One level of related assets: the parent material (names/textures the
    # instance inherits) and how many instances point at this material.
    parent_relation = next((item for item in asset.get("relationships") or []
                            if item.get("relation") == "parent-material" and item.get("targetAssetId")), None)
    if parent_relation is not None:
        try:
            parents = catalog.query(asset_ids=[str(parent_relation["targetAssetId"])], limit=1,
                                    include_relationships=False).get("assets", [])
        except Exception:
            parents = []
        if parents:
            parent_metadata = parents[0].get("metadata") or {}
            document["parent"] = {
                "assetId": parents[0].get("assetId"),
                "relativePath": parents[0].get("relativePath"),
                "className": parent_metadata.get("className", ""),
                "names": list(parent_metadata.get("names") or [])[:128],
                "textures": [str(item).rsplit(".", 1)[-1] for item in (parent_metadata.get("textures") or [])][:64],
            }
    try:
        referrers = catalog.referrers(str(asset.get("assetId")), relation="parent-material", limit=50)
        document["instanceCount"] = len(referrers.get("referrers") or [])
        document["instances"] = [item.get("relativePath") for item in (referrers.get("referrers") or [])[:8]]
    except Exception:
        document["instanceCount"] = None
    try:
        users = catalog.referrers(str(asset.get("assetId")), relation="uses-material", limit=50)
        document["usedByMeshes"] = [
            {"relativePath": item.get("relativePath"), "className": item.get("className")}
            for item in (users.get("referrers") or [])[:12]]
        document["usedByMeshCount"] = len(users.get("referrers") or [])
    except Exception:
        document["usedByMeshes"] = []
    sibling = PurePosixPath(str(asset.get("relativePath") or "")).with_suffix(".uexp").as_posix()
    try:
        found = catalog.query(relative_paths=[sibling], kinds=("package",), limit=1,
                              include_relationships=False)
    except Exception:
        found = {"assets": []}
    for item in found.get("assets", []):
        if str(item.get("relativePath")) != sibling:
            continue
        document["exportParseStatus"] = str(item.get("parseStatus") or "unknown")
        export_metadata = item.get("metadata") or {}
        document["shaderTypes"] = list(export_metadata.get("shaderTypes") or [])[:64]
        document["vertexFactories"] = list(export_metadata.get("vertexFactories") or [])[:16]
        document["defines"] = list(export_metadata.get("defines") or [])[:64]
        document["hasInlineShaderMap"] = bool(export_metadata.get("hasInlineShaderMap"))
        document["exportParser"] = str(export_metadata.get("parser") or "unknown")
        document["numericEvidenceCount"] = len(export_metadata.get("numericValues") or [])
        document["parameterDecodeAvailable"] = bool(export_metadata.get("parameterDecodeAvailable", False))
        break
    return document


def _consistency(document: Mapping[str, Any], features: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Cheap agreement checks between a candidate document and capture facts."""

    checks: list[dict[str, Any]] = []
    stage = str(features.get("stage") or "")
    shader_types = [str(item) for item in document.get("shaderTypes") or []]
    if shader_types:
        wants = {"fragment": "PS", "vertex": "VS", "compute": "CS", "geometry": "GS", "hull": "HS", "domain": "DS"}.get(stage)
        if wants:
            has_stage = any(item.endswith(wants) for item in shader_types)
            checks.append({"signal": "shader-stage-present", "agrees": has_stage,
                           "detail": "material shader map %s a %s shader type" % ("lists" if has_stage else "lists no", wants)})
    pass_hint = str(features.get("passHint") or "").casefold()
    if pass_hint and shader_types:
        pass_map = {"depth": ("DepthOnly", "Shadow"), "shadow": ("ShadowDepth", "Shadow"), "basepass": ("BasePass",),
                    "velocity": ("Velocity",), "outline": ("Outline",), "lumen": ("LumenCard",)}
        for key, needles in pass_map.items():
            if key in pass_hint:
                agrees = any(needle in item for item in shader_types for needle in needles)
                checks.append({"signal": "pass-shader-type", "agrees": agrees,
                               "detail": "pass hint %r vs shader types containing %s" % (pass_hint, "/".join(needles))})
                break
    mesh_hint = str(features.get("meshHint") or "").casefold()
    factories = [str(item).casefold() for item in document.get("vertexFactories") or []]
    if mesh_hint and factories:
        if "skeletal" in mesh_hint or "skin" in mesh_hint:
            agrees = any("gpuskin" in item for item in factories)
            checks.append({"signal": "vertex-factory", "agrees": agrees, "detail": "skinned draw vs GPU-skin vertex factory"})
        elif "static" in mesh_hint:
            agrees = any("local" in item or "static" in item for item in factories)
            checks.append({"signal": "vertex-factory", "agrees": agrees, "detail": "static draw vs local vertex factory"})
    # A stage also binds scene/depth targets, engine defaults and resources from
    # material parents. Its total slot count is not a material texture count.
    # Compare individual content links below; missing links remain unknown.
    # Code logic structure vs. what the asset declares about itself.
    hints = features.get("codeHints") or {}
    names = set(str(item) for item in (document.get("names") or [])) | set(
        str(item) for item in ((document.get("parent") or {}).get("names") or []))
    defines = set(str(item) for item in (document.get("defines") or []))
    if "usesDiscard" in hints:
        masked = bool({"BLEND_Masked", "Masked", "OpacityMaskClipValue", "DitheredLODTransition"} & names) or \
            bool({"MATERIALBLENDING_MASKED", "MATERIAL_DITHER_OPACITY_MASK"} & defines)
        translucent_only = bool({"BLEND_Translucent", "BLEND_Additive"} & names) and not masked
        if hints["usesDiscard"] and masked:
            checks.append({"signal": "blend-mode", "agrees": True,
                           "detail": "shader discards pixels and the asset declares a masked blend mode"})
        elif hints["usesDiscard"] and translucent_only:
            checks.append({"signal": "blend-mode", "agrees": False,
                           "detail": "shader discards pixels but the asset declares translucent/additive blending only"})
        elif not hints["usesDiscard"] and masked and names:
            checks.append({"signal": "blend-mode", "agrees": False,
                           "detail": "asset declares a masked blend mode but the shader never discards"})
    # A single texture can be sampled repeatedly (filters, loops, shadows).
    # Neither instruction count nor all stage slots is a material texture count.
    mesh_hint = str(features.get("meshHint") or "").casefold()
    if mesh_hint == "skeletal" and names:
        skeletal_ok = "bUsedWithSkeletalMesh" in names or any("skin" in str(item).casefold() for item in document.get("vertexFactories") or [])
        if skeletal_ok:
            checks.append({"signal": "usage-flag", "agrees": True, "detail": "skinned draw and the asset is marked for skeletal meshes"})
    return checks


def _owner_path(relative: str) -> str:
    """Map a cooked export/bulk file to the package that owns it."""

    path = PurePosixPath(relative)
    if path.suffix.casefold() in {".uexp", ".ubulk", ".uptnl"}:
        return path.with_suffix(".uasset").as_posix()
    return relative


def value_evidence(catalog: Any, lanes: Sequence[Mapping[str, Any]], *,
                   diagnostics: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Match distinctive cbuffer lane values against indexed asset values.

    Returns evidence keyed by *owner* asset path: matched keys, how many of
    them are rare in the catalog, and adjacency — asset runs whose
    consecutive values sit in consecutive cbuffer lanes. These serialized
    float coincidences rank candidates; they do not prove material identity or
    a parameter's physical output offset, even in a completed directory scan.
    """

    from .symbols.engines.unreal_package import is_distinctive_value, value_key
    import struct as _struct

    lane_keys: dict[str, list[tuple[str, int]]] = {}
    for lane in lanes:
        try:
            value = _struct.unpack("<f", _struct.pack("<f", float(lane.get("value"))))[0]
        except (TypeError, ValueError, OverflowError, _struct.error):
            continue
        if not is_distinctive_value(value):
            continue
        block = "b%s:s%s" % (lane.get("bind"), lane.get("space"))
        lane_keys.setdefault(value_key(value), []).append((block, int(lane.get("lane", 0))))
    if not lane_keys:
        return {}
    try:
        search = catalog.search_values(list(lane_keys), limit=100)
    except Exception as error:
        if diagnostics is not None:
            diagnostics.update(status="unavailable", reason=str(error)[:200], absenceProven=False)
        return {}
    if diagnostics is not None:
        diagnostics.update(status="searched", indexCoverage=search.get("indexCoverage", "unknown"),
                           popularityScope="indexed-assets-only", physicalIdentityVerified=False,
                           commonValueNeighborhoods="not-provided", absenceProven=False)
    popularity = search.get("keyPopularity") or {}
    evidence: dict[str, dict[str, Any]] = {}
    for candidate in search.get("candidates", []):
        owner = _owner_path(str(candidate.get("relativePath") or ""))
        entry = evidence.setdefault(owner, {
            "matchedValues": set(), "rareValues": set(), "adjacentRuns": [], "sourceFiles": set(),
            "laneRefs": {}, "blocks": {},
        })
        entry["sourceFiles"].add(str(candidate.get("relativePath") or ""))
        for key in candidate.get("matchedValues") or []:
            entry["matchedValues"].add(key)
            if popularity.get(key, 0) <= 3:
                entry["rareValues"].add(key)
            entry["laneRefs"][key] = ["%s lane %d" % (block, index) for block, index in lane_keys.get(key, [])][:4]
            for block, unused in lane_keys.get(key, []):
                entry["blocks"].setdefault(block, set()).add(key)
        for run in candidate.get("numericRuns") or []:
            values = [str(item) for item in run.get("values") or []]
            # Each chain must stay in one block and advance one physical lane.
            # Pairwise matches in different blocks must not form a longer run.
            previous: dict[tuple[str, int], tuple[int, int]] = {}
            best = (0, "", 0, 0)
            for position, key in enumerate(values):
                current = {}
                for block, lane in lane_keys.get(key, []):
                    length, start = previous.get((block, lane - 1), (0, position))
                    current[(block, lane)] = (length + 1, start)
                    if length + 1 > best[0]:
                        best = (length + 1, block, lane - length, start)
                previous = current
            length, block, lane, start = best
            if length >= 2:
                row = {"assetOffset": run.get("offset"), "values": values[start:start + length][:8],
                       "consecutiveLanes": length, "block": block, "firstLane": lane,
                       "assetRunValueIndex": start}
                # Header/export observations may repeat the same physical run.
                if not any((r["block"], r["firstLane"], r["values"]) ==
                           (block, lane, row["values"]) for r in entry["adjacentRuns"]):
                    entry["adjacentRuns"].append(row)
    result: dict[str, dict[str, Any]] = {}
    for owner, entry in evidence.items():
        matched = sorted(entry["matchedValues"])
        rare = sorted(entry["rareValues"])
        # Do not add unrelated view/scene/material blocks into one identity
        # claim. Rarity is measured only in the indexed subset, not the game.
        block_evidence = []
        for block, keys in sorted(entry["blocks"].items()):
            block_rare = len(keys & entry["rareValues"])
            adjacency = max((r["consecutiveLanes"] - 1 for r in entry["adjacentRuns"]
                             if r["block"] == block), default=0)
            block_evidence.append({"block": block, "matchedCount": len(keys),
                                   "rareMatchedCount": block_rare,
                                   "longestAdjacentRun": adjacency + 1 if adjacency else 0,
                                   "strength": max(0, block_rare - 1) + 2 * adjacency})
        strength = max((row["strength"] for row in block_evidence), default=0)
        if not rare:
            confidence = min(0.2, 0.1 * len(matched)) if matched else 0.0
        elif strength == 0:
            confidence = 0.25
        else:
            confidence = min(0.65, 1.0 - 0.5 ** strength)
        result[owner] = {
            "matchedValues": matched[:32],
            "matchedCount": len(matched),
            "rareMatchedCount": len(rare),
            "adjacentRuns": entry["adjacentRuns"][:8],
            "laneRefs": {key: entry["laneRefs"][key] for key in matched[:16]},
            "sourceFiles": sorted(entry["sourceFiles"])[:4],
            "blocks": block_evidence,
            "physicalIdentityVerified": False,
            "confidenceKind": "heuristic-ranking",
            "popularityScope": "indexed-assets-only",
            "confidence": round(confidence, 3),
            "summary": (
                "shares %d distinctive value(s) (%d rare within indexed assets)%s; numeric candidate only"
                % (len(matched), len(rare),
                   ", %d adjacent run(s) line up with neighbouring cbuffer lanes" % len(entry["adjacentRuns"])
                   if entry["adjacentRuns"] else "")
            ),
        }
    return result


def texture_identity_evidence(catalog: Any, fingerprints: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Match bound-texture thumbnails against indexed texture fingerprints.

    Returns per-bound-texture matches (asset, hash distance, dihedral
    transform, verdict) and, keyed by *material* path, the textures each
    material is known to reference among those matches (via the catalog's
    ``uses-texture`` relations).  A same-image match is strong evidence on
    its own and a bridge to every asset related to that texture.
    """

    from .symbols.imaging import fingerprint_from_thumbnail

    per_texture: list[dict[str, Any]] = []
    by_material: dict[str, dict[str, Any]] = {}
    for item in fingerprints:
        entry: dict[str, Any] = {"texture": item.get("name") or item.get("resourceId"), "resourceId": item.get("resourceId"),
                                 "matches": []}
        if not item.get("available") or not item.get("thumbnail"):
            entry["reason"] = item.get("reason") or "no snapshot"
            per_texture.append(entry)
            continue
        try:
            query = fingerprint_from_thumbnail(str(item["thumbnail"]))
        except Exception as error:
            entry["reason"] = "thumbnail unusable: %s" % str(error)[:120]
            per_texture.append(entry)
            continue
        if query.get("flat"):
            entry["reason"] = "uniform texture (flat colour) cannot identify an asset"
            per_texture.append(entry)
            continue
        try:
            search = catalog.search_thumbnails(int(query["dhash"]), mean_color=item.get("meanColor"), max_distance=12, limit=8)
        except Exception as error:
            entry["reason"] = str(error)[:160]
            entry["status"] = "unavailable"
            per_texture.append(entry)
            continue
        seen_owners: set[str] = set()
        for match in search.get("candidates", []):
            owner = _owner_path(str(match.get("relativePath") or ""))
            # A package preview and its cooked mip can both match. They are
            # one observation of one bound texture, not independent support.
            if owner in seen_owners:
                continue
            seen_owners.add(owner)
            record = {**match, "ownerPath": owner}
            entry["matches"].append(record)
            # materials referencing this texture asset
            try:
                found = catalog.query(relative_paths=[owner], limit=1, include_relationships=False)
                owner_asset = next((asset for asset in found.get("assets", []) if str(asset.get("relativePath")) == owner), None)
                if owner_asset is not None:
                    record["ownerAssetId"] = owner_asset["assetId"]
                referrers = catalog.referrers(str(owner_asset["assetId"]), relation="uses-texture", limit=100) if owner_asset else {"referrers": []}
            except Exception:
                referrers = {"referrers": []}
            record["referencedByMaterialCount"] = len(referrers.get("referrers") or [])
            for material in referrers.get("referrers") or []:
                bucket = by_material.setdefault(str(material.get("relativePath")), {"textures": [], "sameImage": 0, "plausible": 0})
                bucket["textures"].append({"boundTexture": entry["texture"], "asset": owner, "distance": match.get("distance"),
                                           "transform": match.get("transform"), "verdict": match.get("verdict")})
                if match.get("verdict") == "same-image":
                    bucket["sameImage"] += 1
                else:
                    bucket["plausible"] += 1
        entry["indexCoverage"] = search.get("indexCoverage", "unknown")
        entry["indexedThumbnails"] = int(search.get("indexedThumbnails") or 0)
        entry["unavailableEvidence"] = search.get("unavailableEvidence") or []
        if not entry["matches"]:
            entry["status"] = "unavailable" if not entry["indexedThumbnails"] else "no-indexed-candidate"
            entry["absenceProven"] = False
            entry["reason"] = ("no candidate in the currently indexed texture evidence (%d thumbnails; %s coverage); "
                               "unindexed or undecodable textures remain unknown" %
                               (entry["indexedThumbnails"], entry["indexCoverage"]))
        per_texture.append(entry)
    for bucket in by_material.values():
        strength = 2 * bucket["sameImage"] + bucket["plausible"]
        bucket["confidence"] = round(min(0.97, 1.0 - 0.5 ** strength), 3) if strength else 0.0
        bucket["physicalIdentityVerified"] = False
        bucket["summary"] = "%d bound texture(s) matched by content in this material (%d strong perceptual matches, %d plausible)" % (
            len(bucket["textures"]), bucket["sameImage"], bucket["plausible"])
    return {"perTexture": per_texture, "byMaterial": by_material}


def _in_scope(relative: str, prefixes: Sequence[str]) -> bool:
    if not prefixes:
        return True
    folded = str(relative).casefold()
    return any(folded.startswith(prefix) for prefix in prefixes)


def _asset_search_document(asset: Mapping[str, Any]) -> bool:
    """Use adapter-reported roles, without imposing one engine's classes."""
    kind = asset.get("kind")
    class_name = str((asset.get("metadata") or {}).get("className") or asset.get("className") or "")
    return (kind in {"material", "shader"} or class_name in _MATERIAL_CLASSES or
            (kind in {"package", "metadata"} and not class_name and
             not str(asset.get("relativePath") or "").endswith((".uexp", ".ubulk", ".meta"))))


def anchor_expansion(catalog: Any, per_texture: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A few successful pixel anchors expand search; other slots may stay unknown."""
    anchors: dict[str, dict[str, Any]] = {}
    for texture in per_texture:
        for match in texture.get("matches") or []:
            if match.get("verdict") != "same-image":
                continue
            owner = str(match.get("ownerPath") or match.get("relativePath") or "")
            identity = str(match.get("ownerAssetId") or match.get("assetId") or "")
            if not re.fullmatch(r"asset-v1-[0-9a-f]{64}", identity) or not owner:
                continue
            if identity not in anchors and len(anchors) >= 8:
                continue
            anchor = anchors.setdefault(identity, {"assetId": identity, "relativePath": owner,
                "resources": [], "evidenceKind": "strong-perceptual-search-anchor",
                "physicalIdentityVerified": False})
            resource = {"resourceId": texture.get("resourceId"), "texture": texture.get("texture"),
                        "distance": match.get("distance"), "transform": match.get("transform")}
            if resource not in anchor["resources"]:
                anchor["resources"].append(resource)
    result: dict[str, Any] = {"anchors": list(anchors.values()), "candidates": [], "pathCandidates": [],
        "truncated": False, "missingOtherTexturesVeto": False, "automaticNamePublication": False,
        "physicalIdentityVerified": False, "absenceProven": False,
        "note": "one distinctive content lead can narrow dependencies; untested texture slots do not invalidate it"}
    if not anchors:
        result["status"] = "no-strong-indexed-anchor"
        return result
    if not callable(getattr(catalog, "expand_reference_neighborhood", None)):
        result["status"] = "relationship-expansion-unavailable"
        return result
    try:
        neighborhood = catalog.expand_reference_neighborhood(list(anchors), max_depth=4, max_assets=64)
    except Exception as error:
        result.update(status="relationship-expansion-unavailable", reason=str(error)[:300])
        return result
    result.update(status="expanded", truncated=neighborhood.get("truncated") is True,
                  bounds=neighborhood.get("bounds"), unresolvedEdges=neighborhood.get("unresolvedEdges") or [])
    for asset in neighborhood.get("assets") or []:
        paths = [path for path in asset.get("anchorPaths") or [] if path.get("steps")]
        if not paths or not _asset_search_document(asset):
            continue
        supporting = [anchors[path["anchorAssetId"]] for path in paths if path["anchorAssetId"] in anchors]
        result["candidates"].append({"assetId": asset["assetId"], "relativePath": asset["relativePath"],
            "kind": asset.get("kind"), "className": (asset.get("metadata") or {}).get("className", ""),
            "parseStatus": asset.get("parseStatus"), "anchorPaths": paths,
            "supportingAnchors": supporting, "physicalIdentityVerified": False,
            "minimumHops": min(len(path["steps"]) for path in paths)})
    # Already indexed adjacent paths can find an unread material whose edges
    # have not been decoded. They remain weak, explicit reader work items.
    scopes, seen = [], {item["relativePath"] for item in result["candidates"]}
    for anchor in anchors.values():
        parent = PurePosixPath(anchor["relativePath"]).parent
        # At most the containing folder and one parent, never catalog root.
        for folder in (parent, parent.parent):
            if len(folder.parts) >= 2 and str(folder) not in scopes:
                scopes.append(str(folder))
    if callable(getattr(catalog, "list_scope", None)):
        for scope in scopes[:4]:
            try:
                nearby = catalog.list_scope([scope], kinds=("material", "shader", "package", "metadata"), limit=16)
            except Exception:
                continue
            result["truncated"] = result["truncated"] or len(nearby) >= 16
            for asset in nearby:
                path = str(asset.get("relativePath") or "")
                if path in seen or not _asset_search_document(asset):
                    continue
                seen.add(path)
                result["pathCandidates"].append({**asset, "scope": scope,
                    "evidenceKind": "adjacent-index-path-only", "physicalIdentityVerified": False})
                if len(result["pathCandidates"]) >= 16:
                    result["truncated"] = True
                    break
            if len(result["pathCandidates"]) >= 16:
                break
    return result


def _retrieval_route(features: Mapping[str, Any], expansion: Mapping[str, Any]) -> dict[str, Any]:
    geometry = features.get("drawGeometry") or {}
    code = features.get("codeHints") or {}
    stage = str(features.get("stage") or "")
    count = geometry.get("indexCount") or geometry.get("vertexCount")
    screen_candidate = (stage == "fragment" and geometry.get("available") is True and
                        type(count) is int and 0 < count <= 6 and
                        int(features.get("textureSlotCount") or 0) > 0)
    if stage == "compute" or screen_candidate:
        route = "compute-code-bindings" if stage == "compute" else "screen-space-code-bindings"
        primary = ["shader-code", "resource-bindings", "draw-topology"]
    else:
        route = "content-anchor-dependencies" if expansion.get("anchors") else "content-evidence-search"
        primary = ["texture-content", "mesh-content-when-supported", "exact-index-dependencies"]
    return {"route": route, "inferredScreenSpaceCandidate": screen_candidate,
            "inferenceIsIdentity": False, "primaryEvidence": primary,
            "secondaryEvidence": ["named-index", "candidate-local-numeric-values"],
            "codeEvidence": dict(code),
            "bindingEvidence": {"stage": stage, "textureSlotCount": features.get("textureSlotCount"),
                                "constantBlockSizes": features.get("constantBlockSizes") or []},
            "topologyEvidence": {key: geometry.get(key) for key in
                                 ("available", "topology", "indexCount", "vertexCount", "instanceCount")},
            "materialIdentityRequired": False,
            "unmatchedResourcesAreUnknown": True,
            "shaderContentIdentityStatus": "not-compared",
            "meshContentIdentityStatus": "not-compared",
            "note": "a small draw can suggest a screen-space pass, but counts and binding shape do not identify its source asset"}


def _content_coverage(coverage: Mapping[str, Any], features: Mapping[str, Any]) -> dict[str, Any]:
    """Separate filesystem discovery from content that can actually be compared."""

    def count(name: str) -> int | None:
        value = coverage.get(name)
        return value if type(value) is int and value >= 0 else None

    numeric_rows, texture_rows, deferred = (count("valueIndexRows"), count("thumbnailRows"),
                                            count("deferredCount"))
    geometry = features.get("drawGeometry") or {}
    return {
        "directoryScan": {"status": coverage.get("indexCoverage", "unknown"),
                          "deferredFiles": deferred,
                          "note": "directory completion does not mean payloads, pixels or material layouts were decoded"},
        "numeric": {"indexedRows": numeric_rows,
                    "status": "unavailable" if numeric_rows == 0 else "indexed-subset" if numeric_rows else "unknown",
                    "popularityScope": "indexed-assets-only", "absenceProven": False,
                    "commonValueNeighborhoods": "not-provided",
                    "note": "distinctive float keys only; complete candidate-local sequences require decoded parameter/output evidence"},
        "texture": {"indexedThumbnails": texture_rows,
                    "status": "unavailable" if texture_rows == 0 else "indexed-subset" if texture_rows else "unknown",
                    "absenceProven": False},
        "mesh": {"captureGeometryAvailable": isinstance(geometry, Mapping) and geometry.get("available") is True,
                 "status": "not-compared", "physicalIdentityVerified": False,
                 "note": "draw counts/input formats are context; no mesh content identity was compared"},
    }


def retrieve(catalog: Any, features: Mapping[str, Any], *, top_k: int = 10,
             scope_prefixes: Sequence[str] = ()) -> dict[str, Any]:
    """Rank unpacked-asset documents for one captured shader with evidence.

    Names a capture happens to carry for its textures are not evidence: a
    cooked game rarely keeps asset names on GPU resources, and when it does
    they are the engine's, not the artist's. Ranking therefore rests on what
    can be compared: the material parameter values against the constant
    buffers, the texture pixels against the asset textures, the shader map /
    vertex factory / blend mode against the code, the user's own words, and
    -- with ``scope_prefixes`` -- the folders the reader chose after looking at
    the tree. Every check nudges a score; none of them vetoes a candidate.
    """

    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_CANDIDATES:
        raise ShaderAssetRetrievalError("top_k must be between 1 and %d" % MAX_CANDIDATES)
    prefixes = []
    for prefix in scope_prefixes or ():
        text = str(prefix or "").replace("\\", "/").strip("/").casefold()
        if text:
            prefixes.append(text + "/")
    texture_evidence = bound_texture_names(list(features.get("boundTextureNames") or []))
    extra_tokens = [str(item) for item in (features.get("extraTokens") or []) if isinstance(item, str)][:32]
    resource_tokens = [str(item) for item in (features.get("resourceNames") or []) if isinstance(item, str)
                       and not re.fullmatch(r"(?:t|s|u|b|cb)\d+|Material_Texture2D_\d+|.*_\d+", item)][:32]
    tokens = extra_tokens + resource_tokens
    signals = {
        "boundTextures": {"names": texture_evidence, "usedForRanking": False,
                          "note": ("reported as the capture spells them, uninterpreted: a cooked game's "
                                   "GPU debug names are the engine's, not the artist's, and what a name "
                                   "means differs per game -- ranking compares pixels and constant-buffer "
                                   "values instead, and you decide what these names are worth here")},
        "extraTokens": extra_tokens,
        "resourceNames": resource_tokens,
        "scopePrefixes": prefixes,
        "retrievalRoute": _retrieval_route(features, {}),
    }
    lanes = list(features.get("constantBufferLanes") or [])
    fingerprints = list(features.get("boundTextureFingerprints") or [])
    try:
        catalog_coverage = catalog.coverage()
    except Exception as error:
        catalog_coverage = {"available": False, "reason": str(error)[:200], "limitations": []}
    evidence_coverage = _content_coverage(catalog_coverage, features)
    if not tokens and not lanes and not fingerprints and not prefixes:
        return {
            "schema": RETRIEVAL_SCHEMA, "schemaVersion": 1, "queryTokens": [], "signals": signals,
            "coverage": catalog_coverage, "evidenceCoverage": evidence_coverage,
            "candidates": [], "reason": ("no retrieval evidence: no user words, no folders chosen, no readable "
                                         "texture pixels and no distinctive constant-buffer values; profile the "
                                         "asset tree (profile_unpacked_asset_directories) and pass scope_prefixes"),
        }
    try:
        engine = str(catalog.status().get("engine") or "unknown")
    except Exception:
        engine = "unknown"
    # Class filtering only makes sense where the extractor knows classes
    # (Unreal packages); a generic engine ranks every probed document.
    requested_classes = features.get("classNames")
    if requested_classes is None:
        class_filter = list(_MATERIAL_CLASSES) if engine == "unreal" else []
    else:
        class_filter = [str(item) for item in requested_classes]
    kinds = ("package", "material", "shader", "metadata") if engine == "unreal" else \
        ("package", "material", "shader", "metadata", "other", "binary", "scene", "mesh")
    if tokens:
        search = catalog.search_tokens(tokens, kinds=kinds, class_names=class_filter,
                                       limit=min(MAX_CANDIDATES, top_k * 3))
    else:
        search = {"catalogSha256": None, "engine": None, "queryTokens": [], "tokenWeights": {}, "candidates": []}
    value_diagnostics: dict[str, Any] = {}
    values = value_evidence(catalog, lanes, diagnostics=value_diagnostics)
    signals["constantBufferValues"] = {
        "distinctiveLanes": len(lanes),
        "assetsSharingValues": len(values),
        **value_diagnostics,
    }
    identity = texture_identity_evidence(catalog, fingerprints) if fingerprints else {"perTexture": [], "byMaterial": {}}
    signals["textureIdentity"] = identity["perTexture"]
    signals["meshIdentity"] = evidence_coverage["mesh"]
    expansion = anchor_expansion(catalog, identity["perTexture"])
    signals["anchorExpansion"] = expansion
    signals["retrievalRoute"] = _retrieval_route(features, expansion)
    anchor_candidates = {item["relativePath"]: item for item in expansion["candidates"]}
    by_material_identity = identity["byMaterial"]
    if search.get("catalogSha256") is None:
        try:
            search["catalogSha256"] = catalog.status().get("catalogSha256")
            search["engine"] = catalog.status().get("engine")
        except Exception:
            pass
    if prefixes:
        search["candidates"] = [item for item in search.get("candidates", [])
                                if _in_scope(str(item.get("relativePath")), prefixes)]
    token_paths = {str(item.get("relativePath")) for item in search.get("candidates", [])}
    # Folder-first: every parsed material under the chosen folders is a
    # candidate, ranked by content agreement even when no word matched it.
    scope_seeds = 0
    if prefixes and hasattr(catalog, "list_scope"):
        try:
            seeds = catalog.list_scope(
                [prefix.rstrip("/") for prefix in prefixes],
                kinds=("package", "material", "metadata", "shader") if engine == "unreal" else (),
                class_names=class_filter, parsed_only=True, limit=400)
        except Exception:
            seeds = []
        for seed in seeds:
            relative = str(seed.get("relativePath"))
            if relative in token_paths or relative.casefold().endswith((".uexp", ".ubulk")):
                continue
            search.setdefault("candidates", []).append({
                "assetId": seed["assetId"], "relativePath": relative, "kind": seed.get("kind"),
                "format": seed.get("format"), "size": seed.get("size"), "contentSha256": "",
                "className": seed.get("className", ""), "engine": engine,
                "score": 0.0, "matchedTokens": [], "matchedCount": 0, "queryCoverage": 0.0,
                "fromScope": True,
            })
            token_paths.add(relative)
            scope_seeds += 1
    signals["scopeSeeds"] = scope_seeds
    extra_owners = [(owner, entry["confidence"]) for owner, entry in values.items()]
    extra_owners += [(owner, entry["confidence"]) for owner, entry in by_material_identity.items()]
    extra_owners += [(owner, 0.75) for owner in anchor_candidates]
    extra_owners += [(item["relativePath"], 0.1) for item in expansion["pathCandidates"]]
    for owner, confidence in sorted(extra_owners, key=lambda item: -item[1]):
        if owner in token_paths or confidence <= 0 or not _in_scope(owner, prefixes):
            continue
        # Candidate found by content alone (cbuffer values or texture pixels),
        # not by any name token.
        try:
            found = catalog.query(relative_paths=[owner], limit=1, include_relationships=False)
        except Exception:
            found = {"assets": []}
        asset = next((item for item in found.get("assets", []) if str(item.get("relativePath")) == owner), None)
        if asset is None:
            continue
        search.setdefault("candidates", []).append({
            "assetId": asset["assetId"], "relativePath": owner, "kind": asset.get("kind"),
            "format": asset.get("format"), "size": asset.get("size"), "contentSha256": asset.get("contentSha256"),
            "className": (asset.get("metadata") or {}).get("className", ""),
            "engine": (asset.get("metadata") or {}).get("engine", ""),
            "score": 0.0, "matchedTokens": [], "matchedCount": 0, "queryCoverage": 0.0,
        })
        token_paths.add(owner)
    ids = [item["assetId"] for item in search.get("candidates", [])]
    details: dict[str, Mapping[str, Any]] = {}
    if ids:
        try:
            queried = catalog.query(asset_ids=ids[:100], limit=100, include_relationships=True)
            details = {str(item["assetId"]): item for item in queried.get("assets", [])}
        except Exception:
            details = {}
    candidates = []
    for item in search.get("candidates", []):
        asset = details.get(str(item["assetId"]), {"assetId": item["assetId"], "relativePath": item["relativePath"],
                                                    "metadata": {"className": item.get("className")}})
        document = _material_document(catalog, asset)
        checks = _consistency(document, features)
        agree = sum(1 for check in checks if check["agrees"])
        disagree = sum(1 for check in checks if not check["agrees"])
        # Tolerant by design: a disagreeing check lowers the score, it never
        # removes the candidate; a wrong pass hint or an unusual material must
        # not hide the right asset.
        factor = max(0.25, 1.0 + 0.15 * agree - 0.25 * disagree)
        adjusted = (item["score"] + 1.0) * factor
        value_entry = values.get(str(item.get("relativePath")))
        if value_entry is None:
            parent_path = (document.get("parent") or {}).get("relativePath")
            value_entry = values.get(str(parent_path)) if parent_path else None
            if value_entry is not None:
                value_entry = {**value_entry, "viaParent": True}
        if value_entry:
            adjusted += max((3.0 * block["rareMatchedCount"] +
                             4.0 * max(0, block["longestAdjacentRun"] - 1)
                             for block in value_entry["blocks"]), default=0.0)
        identity_entry = by_material_identity.get(str(item.get("relativePath")))
        if identity_entry is None:
            parent_path = (document.get("parent") or {}).get("relativePath")
            identity_entry = by_material_identity.get(str(parent_path)) if parent_path else None
            if identity_entry is not None:
                identity_entry = {**identity_entry, "viaParent": True}
        if identity_entry:
            adjusted += 6.0 * identity_entry["sameImage"] + 2.0 * identity_entry["plausible"]
        anchor_entry = anchor_candidates.get(str(item.get("relativePath")))
        if anchor_entry and not identity_entry:
            adjusted += 6.0 / max(1, anchor_entry["minimumHops"])
        why = "matched %s; %d consistency check(s) agree, %d disagree" % (
            ", ".join(item.get("matchedTokens") or []) or "no name tokens", agree, disagree)
        if value_entry:
            why += "; " + value_entry["summary"] + (" (through its parent material)" if value_entry.get("viaParent") else "")
        if identity_entry:
            why += "; " + identity_entry["summary"] + (" (through its parent material)" if identity_entry.get("viaParent") else "")
        if anchor_entry:
            why += "; linked to a content search anchor through %d exact indexed relationship(s)" % anchor_entry["minimumHops"]
        candidates.append({
            **item,
            "adjustedScore": round(adjusted, 4),
            "consistency": checks,
            "document": document,
            "valueEvidence": value_entry,
            "textureIdentity": identity_entry,
            "anchorEvidence": anchor_entry,
            "why": why,
        })
    candidates.sort(key=lambda entry: (-entry["adjustedScore"], entry["relativePath"]))
    candidates = candidates[:top_k]
    top = candidates[0]["adjustedScore"] if candidates else 0.0
    second = candidates[1]["adjustedScore"] if len(candidates) > 1 else 0.0
    weights = search.get("tokenWeights") or {}
    total_mass = sum(float(value) for value in weights.values()) or 1.0
    for index, candidate in enumerate(candidates):
        matched = set(candidate.get("matchedTokens") or [])
        mass = sum(float(weights.get(token, 0.0)) for token in matched)
        coverage = mass / total_mass
        checks = candidate.get("consistency") or []
        agree_ratio = (sum(1 for check in checks if check["agrees"]) / len(checks)) if checks else 0.5
        lead = candidate["adjustedScore"] / top if top > 0 else 0.0
        unique_leader = index == 0 and (second == 0 or top >= 1.5 * second)
        confidence = 0.10 + 0.50 * coverage + 0.20 * agree_ratio + 0.10 * lead + (0.10 if unique_leader else 0.0)
        document = candidate.get("document") or {}
        document_textures = [str(item).casefold() for item in document.get("textures") or []]
        parent_textures = [str(item).casefold() for item in (document.get("parent") or {}).get("textures") or []]
        explained = []
        unexplained = []
        for item in texture_evidence:
            if not item.get("used"):
                continue
            produced = [str(token).casefold() for token in item.get("tokens") or []]
            hit = any(core and (core in name) for core in produced for name in document_textures + parent_textures)
            (explained if hit else unexplained).append(item["texture"])
        texture_facet = (len(explained) / (len(explained) + len(unexplained))) if (explained or unexplained) else None
        # Names are reference only: shipped captures rarely carry them, and a
        # path word proves little. Without structural/value/pixel support a
        # name-only match stays below 0.6.
        if texture_facet is not None and texture_facet < 0.5:
            confidence = min(confidence, 0.6)
        elif texture_facet is None and matched:
            confidence = min(confidence, 0.6)
        value_entry = candidate.get("valueEvidence") or {}
        value_confidence = float(value_entry.get("confidence") or 0.0)
        identity_entry = candidate.get("textureIdentity") or {}
        identity_confidence = float(identity_entry.get("confidence") or 0.0)
        if value_confidence >= 0.5:
            # A useful numerical search lead remains a lead. Sparse catalogs,
            # shared defaults and runtime values cannot establish identity.
            confidence = max(confidence, value_confidence)
        if identity_confidence >= 0.5:
            confidence = max(confidence, identity_confidence)
        if not identity_entry:
            confidence = min(confidence, 0.65 if value_entry else 0.6)
        if candidate.get("anchorEvidence"):
            confidence = max(confidence, 0.65)
        candidate["confidence"] = round(min(0.98, max(0.01, confidence)), 3)
        candidate["confidenceKind"] = "heuristic-ranking"
        candidate["physicalIdentityVerified"] = False
        candidate["identityStatus"] = "unresolved"
        candidate["evidenceChannels"] = {
            "numeric": "candidate-values" if value_entry else "no-indexed-support",
            "texture": "perceptual-candidate" if identity_entry else "no-indexed-support",
            "mesh": "not-compared",
        }
        candidate["facets"] = {
            "textures": {
                "explained": explained, "unexplained": unexplained,
                "confidence": round(len(explained) / (len(explained) + len(unexplained)), 3)
                if (explained or unexplained) else None,
                "note": "explained = a bound texture whose name appears in this material or its parent",
            },
            "shaderTypes": {
                "available": bool(document.get("shaderTypes")),
                "agrees": [check["signal"] for check in checks if check["agrees"]],
                "disagrees": [check["signal"] for check in checks if not check["agrees"]],
            },
            "names": {
                "available": len(document.get("names") or []) + len((document.get("parent") or {}).get("names") or []),
                "note": "parameter/property names to validate against code usage; usable even when the whole match is uncertain",
            },
            "textureIdentity": {
                "textures": identity_entry.get("textures") or [],
                "sameImage": identity_entry.get("sameImage") or 0,
                "plausible": identity_entry.get("plausible") or 0,
                "confidence": identity_confidence or None,
                "viaParent": bool(identity_entry.get("viaParent")),
                "note": ("bound textures whose pixels (perceptual hash over mirror/rotation variants) match a texture "
                         "this material references; independent of names"),
            },
            "values": {
                "matched": value_entry.get("matchedValues") or [],
                "rareMatchedCount": value_entry.get("rareMatchedCount") or 0,
                "adjacentRuns": value_entry.get("adjacentRuns") or [],
                "laneRefs": value_entry.get("laneRefs") or {},
                "blocks": value_entry.get("blocks") or [],
                "confidence": value_confidence or None,
                "physicalIdentityVerified": False,
                "popularityScope": "indexed-assets-only",
                "viaParent": bool(value_entry.get("viaParent")),
                "note": ("cbuffer float keys found in serialized data; runs stay in one physical block. "
                         "This does not decode parameter names or establish their output offsets"),
            },
            "mesh": evidence_coverage["mesh"],
        }
    for candidate in candidates:
        document = candidate.get("document") or {}
        issues = []
        if document.get("exportParseStatus") in {"deferred", "missing", "parse-error"}:
            issues.append("cooked export data %s: shader types/values for this candidate are incomplete"
                          % document.get("exportParseStatus"))
        elif document.get("engine") == "unreal" and not document.get("parameterDecodeAvailable"):
            issues.append("package/export probing is not a decoded material parameter document; "
                          "read this exact candidate with a configured asset reader before assigning parameter names")
        if not document.get("names"):
            issues.append("no readable names in this package (foreign format, encrypted, or stripped)")
        candidate["parseIssues"] = issues
    return {
        "schema": RETRIEVAL_SCHEMA,
        "schemaVersion": 1,
        "catalogSha256": search.get("catalogSha256"),
        "engine": search.get("engine"),
        "coverage": catalog_coverage,
        "evidenceCoverage": evidence_coverage,
        "limitations": list(catalog_coverage.get("limitations") or []),
        "queryTokens": search.get("queryTokens"),
        "tokenWeights": search.get("tokenWeights"),
        "signals": signals,
        "candidates": candidates,
        "decisionHint": (
            "no candidate in indexed evidence; absence is not proven" if not candidates else
            "ranked search lead: identity remains unresolved" if top > 0 and (second == 0 or top >= 1.5 * second) else
            "ambiguous: compare documents (parent chain, shader types, texture set) before deciding"
        ),
        "bestConfidence": candidates[0]["confidence"] if candidates else 0.0,
        "policy": {
            "hashRequired": False,
            "aiDecides": True,
            "partialEvidenceUsable": True,
            "confidenceKind": "heuristic-ranking-not-identity-probability",
            "automaticNamePublication": False,
            "allBoundTexturesRequired": False,
            "boundedReferenceExpansion": True,
            "submitTool": "submit_shader_asset_resolution",
            "rememberedPerGameBuild": True,
            "aiMustStillSearch": True,
            "note": ("confidence is per candidate and per facet; a facet that matched (e.g. textures) is "
                     "valid reference even when another facet is unexplained — record the match with the "
                     "confidence you actually have instead of discarding it. These Host signals supplement, "
                     "not replace, the AI's own catalog search when they are weak or absent"),
        },
    }
