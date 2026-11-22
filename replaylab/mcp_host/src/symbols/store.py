"""SQLite persistence for reusable shader symbol matches."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator


SCHEMA_VERSION = 3


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SymbolStore:
    def __init__(self, database_path: str | Path, *, read_only: bool = False):
        self.read_only = bool(read_only)
        raw_path = Path(database_path).expanduser()
        if self.read_only and raw_path.is_symlink():
            raise RuntimeError(
                "Read-only symbol database must not be a symbolic link: %s"
                % raw_path
            )
        self.path = raw_path.resolve()
        if self.read_only:
            if not self.path.is_file():
                raise RuntimeError(
                    "Read-only symbol database is unavailable: %s" % self.path
                )
            self._validate_read_only_schema()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            connection = sqlite3.connect(
                self.path.as_uri() + "?mode=ro",
                uri=True,
            )
        else:
            connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        if self.read_only:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            if not self.read_only:
                connection.commit()
        finally:
            connection.close()

    def _validate_read_only_schema(self) -> None:
        """Validate a prepared v3 database without creating or migrating it."""

        required_tables = {
            "metadata",
            "asset_scans",
            "asset_shaders",
            "capture_catalogs",
            "capture_shaders",
            "symbol_matches",
            "aliases",
            "symbol_rules",
        }
        with self.connection() as db:
            table_names = {
                str(row["name"])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if not str(row["name"]).startswith("sqlite_")
            }
            if not required_tables.issubset(table_names):
                raise RuntimeError(
                    "Read-only symbol database is not a complete v%d database"
                    % SCHEMA_VERSION
                )
            existing = db.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if existing is None:
                raise RuntimeError(
                    "Read-only symbol database has no schema version"
                )
            try:
                existing_version = int(existing["value"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "Invalid symbol database schema version: %r"
                    % existing["value"]
                ) from exc
            if existing_version != SCHEMA_VERSION:
                direction = (
                    "older" if existing_version < SCHEMA_VERSION else "newer"
                )
                raise RuntimeError(
                    "Symbol database schema %d is %s than supported schema %d. "
                    "Use a compatible prepared project database."
                    % (existing_version, direction, SCHEMA_VERSION)
                )

    def _initialize(self) -> None:
        with self.connection() as db:
            table_names = {
                str(row["name"])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if not str(row["name"]).startswith("sqlite_")
            }
            if "metadata" not in table_names:
                if table_names:
                    raise RuntimeError(
                        "Refusing to initialize an unversioned, non-empty SQLite database: %s"
                        % self.path
                    )
                db.execute(
                    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                existing = None
            else:
                existing = db.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
                if existing is None and table_names != {"metadata"}:
                    raise RuntimeError(
                        "Refusing to initialize a non-empty symbol database without a schema version"
                    )

            if existing is not None:
                try:
                    existing_version = int(existing["value"])
                except (TypeError, ValueError):
                    raise RuntimeError(
                        "Invalid symbol database schema version: %r" % existing["value"]
                    )
                if existing_version != SCHEMA_VERSION:
                    direction = "older" if existing_version < SCHEMA_VERSION else "newer"
                    raise RuntimeError(
                        "Symbol database schema %d is %s than supported schema %d. "
                        "Use a compatible database path; automatic migration is not available."
                        % (existing_version, direction, SCHEMA_VERSION)
                    )

            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS asset_scans (
                    scan_id TEXT PRIMARY KEY,
                    root_path TEXT NOT NULL,
                    build_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    summary_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS asset_shaders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id TEXT NOT NULL REFERENCES asset_scans(scan_id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL,
                    file_offset INTEGER NOT NULL DEFAULT 0,
                    byte_size INTEGER NOT NULL DEFAULT 0,
                    container_kind TEXT NOT NULL DEFAULT '',
                    raw_sha256 TEXT NOT NULL DEFAULT '',
                    code_sha256 TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL DEFAULT '',
                    entry_point TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    layout_hash TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    provenance TEXT NOT NULL DEFAULT 'scanner',
                    UNIQUE(scan_id, relative_path, file_offset, raw_sha256, code_sha256,
                           stage, entry_point)
                );
                CREATE INDEX IF NOT EXISTS asset_shader_raw_idx
                    ON asset_shaders(raw_sha256);
                CREATE INDEX IF NOT EXISTS asset_shader_code_idx
                    ON asset_shaders(code_sha256);
                CREATE INDEX IF NOT EXISTS asset_shader_layout_idx
                    ON asset_shaders(stage, layout_hash);

                CREATE TABLE IF NOT EXISTS capture_catalogs (
                    capture_id TEXT PRIMARY KEY,
                    catalog_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS capture_shaders (
                    capture_id TEXT NOT NULL REFERENCES capture_catalogs(capture_id) ON DELETE CASCADE,
                    shader_resource_id INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    entry_point TEXT NOT NULL DEFAULT '',
                    raw_sha256 TEXT NOT NULL DEFAULT '',
                    code_sha256 TEXT NOT NULL DEFAULT '',
                    layout_hash TEXT NOT NULL DEFAULT '',
                    raw_path TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(capture_id, shader_resource_id, stage, entry_point)
                );
                CREATE INDEX IF NOT EXISTS capture_shader_raw_idx
                    ON capture_shaders(raw_sha256);
                CREATE INDEX IF NOT EXISTS capture_shader_code_idx
                    ON capture_shaders(code_sha256);

                CREATE TABLE IF NOT EXISTS symbol_matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    capture_id TEXT NOT NULL,
                    shader_resource_id INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    entry_point TEXT NOT NULL DEFAULT '',
                    asset_shader_id INTEGER NOT NULL REFERENCES asset_shaders(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    score REAL NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'program',
                    confidence REAL,
                    rationale TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    UNIQUE(capture_id, shader_resource_id, stage, entry_point, asset_shader_id)
                );
                CREATE INDEX IF NOT EXISTS symbol_match_capture_idx
                    ON symbol_matches(capture_id, selected, status);

                CREATE TABLE IF NOT EXISTS aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    capture_id TEXT NOT NULL,
                    shader_resource_id INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    entry_point TEXT NOT NULL DEFAULT '',
                    binding_kind TEXT NOT NULL DEFAULT 'shader',
                    register_space INTEGER NOT NULL DEFAULT -1,
                    bind_number INTEGER NOT NULL DEFAULT -1,
                    byte_offset INTEGER NOT NULL DEFAULT -1,
                    canonical_path TEXT NOT NULL DEFAULT '',
                    alias TEXT NOT NULL,
                    original_name TEXT NOT NULL DEFAULT '',
                    provenance TEXT NOT NULL,
                    confidence REAL,
                    verified INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(capture_id, shader_resource_id, stage, entry_point, binding_kind,
                           register_space, bind_number, byte_offset, canonical_path, alias)
                );

                CREATE TABLE IF NOT EXISTS symbol_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id TEXT NOT NULL REFERENCES asset_scans(scan_id) ON DELETE CASCADE,
                    raw_sha256 TEXT NOT NULL DEFAULT '',
                    code_sha256 TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL DEFAULT '',
                    entry_point TEXT NOT NULL DEFAULT '',
                    layout_hash TEXT NOT NULL DEFAULT '',
                    asset_shader_id INTEGER NOT NULL REFERENCES asset_shaders(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    confidence REAL,
                    program_verified INTEGER NOT NULL DEFAULT 0,
                    user_confirmed INTEGER NOT NULL DEFAULT 0,
                    rationale TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    UNIQUE(scan_id, raw_sha256, code_sha256, stage, entry_point, layout_hash)
                );
                CREATE INDEX IF NOT EXISTS symbol_rules_lookup_idx
                    ON symbol_rules(scan_id, raw_sha256, code_sha256, stage, entry_point, layout_hash);
                """
            )
            if existing is None:
                db.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )

    def replace_asset_scan(
        self, scan_id: str, root_path: str, build_id: str, summary: dict
    ) -> None:
        with self.connection() as db:
            db.execute(
                """INSERT INTO asset_scans(scan_id, root_path, build_id, created_at, summary_json)
                   VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(scan_id) DO UPDATE SET
                     root_path=excluded.root_path,
                     build_id=excluded.build_id,
                     created_at=excluded.created_at,
                     summary_json=excluded.summary_json""",
                (scan_id, root_path, build_id, _utc_now(), _json(summary)),
            )

    def prune_asset_shaders(self, scan_id: str, retained_ids: list[int]) -> None:
        """Remove records no longer seen without changing IDs of retained shaders.

        Stable row IDs preserve reviewed cross-frame symbol rules across a
        rescan. Rules for assets that truly disappeared are removed by the
        foreign-key cascade.
        """

        with self.connection() as db:
            db.execute("CREATE TEMP TABLE retained_asset_shader_ids(id INTEGER PRIMARY KEY)")
            db.executemany(
                "INSERT OR IGNORE INTO retained_asset_shader_ids(id) VALUES(?)",
                ((int(value),) for value in retained_ids),
            )
            db.execute(
                """DELETE FROM asset_shaders
                   WHERE scan_id=?
                     AND id NOT IN (SELECT id FROM retained_asset_shader_ids)""",
                (scan_id,),
            )

    def update_asset_scan_summary(self, scan_id: str, summary: dict) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE asset_scans SET summary_json = ? WHERE scan_id = ?",
                (_json(summary), scan_id),
            )

    def add_asset_shader(self, scan_id: str, record: dict) -> int:
        values = (
            scan_id,
            record.get("relativePath", ""),
            int(record.get("fileOffset", 0)),
            int(record.get("byteSize", 0)),
            record.get("kind", ""),
            record.get("rawSha256", ""),
            record.get("codeSha256", ""),
            record.get("stage", ""),
            record.get("entryPoint", ""),
            record.get("displayName", ""),
            record.get("layoutHash", ""),
            _json(record.get("metadata", {})),
            record.get("provenance", "scanner"),
        )
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO asset_shaders(
                    scan_id, relative_path, file_offset, byte_size, container_kind,
                    raw_sha256, code_sha256, stage, entry_point, display_name,
                    layout_hash, metadata_json, provenance)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scan_id, relative_path, file_offset, raw_sha256, code_sha256,
                            stage, entry_point)
                DO UPDATE SET
                    byte_size=excluded.byte_size,
                    container_kind=excluded.container_kind,
                    stage=CASE WHEN excluded.stage != '' THEN excluded.stage ELSE asset_shaders.stage END,
                    entry_point=CASE WHEN excluded.entry_point != '' THEN excluded.entry_point ELSE asset_shaders.entry_point END,
                    display_name=CASE WHEN excluded.display_name != '' THEN excluded.display_name ELSE asset_shaders.display_name END,
                    layout_hash=CASE WHEN excluded.layout_hash != '' THEN excluded.layout_hash ELSE asset_shaders.layout_hash END,
                    metadata_json=CASE WHEN excluded.metadata_json != '{}' THEN excluded.metadata_json ELSE asset_shaders.metadata_json END,
                    provenance=excluded.provenance
                """,
                values,
            )
            row = db.execute(
                """SELECT id FROM asset_shaders
                   WHERE scan_id=? AND relative_path=? AND file_offset=?
                      AND raw_sha256=? AND code_sha256=? AND stage=? AND entry_point=?""",
                (
                    scan_id,
                    values[1],
                    values[2],
                    values[5],
                    values[6],
                    values[7],
                    values[8],
                ),
            ).fetchone()
            return int(row["id"])

    def list_asset_shaders(self, scan_id: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT * FROM asset_shaders WHERE scan_id=?
                   ORDER BY relative_path, file_offset, stage, entry_point, id""",
                (scan_id,),
            ).fetchall()
        return [self._asset_row(row) for row in rows]

    def get_asset_shader(self, asset_shader_id: int) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM asset_shaders WHERE id=?", (int(asset_shader_id),)
            ).fetchone()
        return self._asset_row(row) if row is not None else None

    def get_asset_scan(self, scan_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM asset_scans WHERE scan_id=?", (scan_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["summary"] = json.loads(item.pop("summary_json"))
        return item

    def find_asset_candidates(
        self,
        scan_id: str,
        raw_sha256: str,
        code_sha256: str,
        stage: str,
        layout_hash: str,
        limit: int = 50,
        entry_point: str = "",
    ) -> list[dict]:
        clauses: list[str] = []
        values: list[Any] = [scan_id]
        if raw_sha256:
            clauses.append("raw_sha256 = ?")
            values.append(raw_sha256)
        if code_sha256:
            clauses.append("code_sha256 = ?")
            values.append(code_sha256)
        if layout_hash:
            clauses.append("layout_hash = ?")
            values.append(layout_hash)
        if not clauses:
            return []
        requested_limit = max(1, min(int(limit), 1000))
        query = (
            "SELECT * FROM asset_shaders WHERE scan_id = ? AND ("
            + " OR ".join(clauses)
            + ") ORDER BY "
            + "CASE WHEN ? = '' OR stage = ? THEN 0 "
              "WHEN stage = '' THEN 1 ELSE 2 END, "
              "CASE WHEN ? = '' OR entry_point = ? THEN 0 "
              "WHEN entry_point = '' THEN 1 ELSE 2 END, "
              "CASE "
              "WHEN ? != '' AND raw_sha256 = ? THEN 0 "
              "WHEN ? != '' AND code_sha256 = ? THEN 1 "
              "WHEN ? != '' AND layout_hash = ? THEN 2 "
              "ELSE 3 END, "
              "CASE WHEN provenance LIKE 'manifest:%' THEN 0 ELSE 1 END, "
              "relative_path, file_offset, id LIMIT ?"
        )
        values.extend(
            (
                stage,
                stage,
                entry_point,
                entry_point,
                raw_sha256,
                raw_sha256,
                code_sha256,
                code_sha256,
                layout_hash,
                layout_hash,
                requested_limit,
            )
        )
        with self.connection() as db:
            rows = db.execute(query, values).fetchall()
        return [self._asset_row(row) for row in rows]

    def save_capture_catalog(self, catalog_path: str, catalog: dict) -> str:
        capture_id = str(catalog["captureId"])
        with self.connection() as db:
            db.execute(
                """INSERT OR REPLACE INTO capture_catalogs(
                       capture_id, catalog_path, created_at, metadata_json)
                   VALUES(?, ?, ?, ?)""",
                (capture_id, catalog_path, _utc_now(), _json(catalog)),
            )
            db.execute("DELETE FROM symbol_matches WHERE capture_id=?", (capture_id,))
            db.execute("DELETE FROM capture_shaders WHERE capture_id=?", (capture_id,))
            for shader in catalog.get("shaders", []):
                db.execute(
                    """INSERT INTO capture_shaders(
                           capture_id, shader_resource_id, stage, entry_point,
                           raw_sha256, code_sha256, layout_hash, raw_path, metadata_json)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        capture_id,
                        int(shader.get("shaderResourceId", 0)),
                        shader.get("stage", ""),
                        shader.get("entryPoint", ""),
                        shader.get("rawSha256", ""),
                        shader.get("codeSha256", ""),
                        shader.get("layoutHash", ""),
                        shader.get("rawPath", ""),
                        _json(shader),
                    ),
                )
            # Aliases of Shaders that left this catalog are orphans; the rest stay
            # (a rescan must not lose names the user approved under this identity),
            # and Shaders that have none yet inherit by their bytes.
            keys = {(int(shader.get("shaderResourceId", 0)), str(shader.get("stage", "") or ""),
                     str(shader.get("entryPoint", "") or ""))
                    for shader in catalog.get("shaders", [])}
            for row in db.execute(
                    "SELECT DISTINCT shader_resource_id, stage, entry_point FROM aliases"
                    " WHERE capture_id=?", (capture_id,)).fetchall():
                key = (int(row["shader_resource_id"]), str(row["stage"]), str(row["entry_point"]))
                if key not in keys:
                    db.execute(
                        "DELETE FROM aliases WHERE capture_id=? AND shader_resource_id=?"
                        " AND stage=? AND entry_point=?", (capture_id,) + key)
            inherited = self._inherit_aliases(db, capture_id, catalog.get("shaders", []))
        if inherited:
            self._log_inheritance(capture_id, inherited)
        return capture_id

    def _inherit_aliases(self, db: Any, capture_id: str, shaders: Iterable[dict]) -> int:
        """Carry aliases forward to a new capture identity by the Shader's own bytes.

        The capture identity hashes RenderDoc's ResourceIds, which are reassigned on
        every open, so reopening the very same .rdc produces a new ``capture_id`` --
        and every CBuffer name and asset match the user approved was left under the
        old one, unreachable. Nothing read them back. The Shader itself is the same
        bytes at the same stage and entry point, so that is the key that survives:
        for each Shader in the new catalog, aliases recorded for the same
        ``(stage, entry_point, raw_sha256)`` under any earlier capture are copied to
        the new identity and the new ResourceId, with their provenance saying where
        they came from. Verified stays verified; the user already said yes. A Shader
        that already has aliases under this identity keeps them and inherits nothing.
        """

        copied = 0
        for shader in shaders:
            raw = str(shader.get("rawSha256", "") or "").lower()
            stage = str(shader.get("stage", "") or "")
            entry_point = str(shader.get("entryPoint", "") or "")
            new_resource_id = int(shader.get("shaderResourceId", 0) or 0)
            if len(raw) != 64 or not stage:
                continue
            own = db.execute(
                """SELECT 1 FROM aliases
                   WHERE capture_id=? AND shader_resource_id=? AND stage=? AND entry_point=?
                   LIMIT 1""",
                (capture_id, new_resource_id, stage, entry_point),
            ).fetchone()
            if own is not None:
                # This identity already has names for the Shader; they are newer
                # than anything another capture could lend it.
                continue
            donors = db.execute(
                """SELECT capture_id, shader_resource_id FROM capture_shaders
                   WHERE raw_sha256=? AND stage=? AND entry_point=? AND capture_id<>?
                   ORDER BY rowid DESC LIMIT 8""",
                (raw, stage, entry_point, capture_id),
            ).fetchall()
            for donor in donors:
                rows = db.execute(
                    """SELECT * FROM aliases
                       WHERE capture_id=? AND shader_resource_id=? AND stage=? AND entry_point=?""",
                    (donor["capture_id"], donor["shader_resource_id"], stage, entry_point),
                ).fetchall()
                if not rows:
                    continue
                for row in rows:
                    item = dict(row)
                    metadata = json.loads(item.get("metadata_json") or "{}")
                    metadata.setdefault("inheritedFrom", {
                        "captureId": donor["capture_id"],
                        "shaderResourceId": donor["shader_resource_id"],
                    })
                    # The export path accepts a row only when its metadata names the
                    # capture it is read under; rebind, the origin stays in inheritedFrom.
                    metadata["captureId"] = capture_id
                    metadata["shaderResourceId"] = new_resource_id
                    cursor = db.execute(
                        """INSERT OR IGNORE INTO aliases(
                               capture_id, shader_resource_id, stage, entry_point, binding_kind,
                               register_space, bind_number, byte_offset, canonical_path,
                               alias, original_name, provenance, confidence, verified,
                               metadata_json)
                           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            capture_id, new_resource_id, stage, entry_point,
                            item["binding_kind"], item["register_space"], item["bind_number"],
                            item["byte_offset"], item["canonical_path"], item["alias"],
                            item["original_name"], item["provenance"], item["confidence"],
                            item["verified"], _json(metadata),
                        ),
                    )
                    copied += int(cursor.rowcount or 0)
                # The most recent donor that had anything is the one to inherit from;
                # older captures' names would only disagree with it.
                break
        return copied

    def _log_inheritance(self, capture_id: str, copied: int) -> None:
        try:
            import logging

            logging.getLogger("replaylab.symbols").info(
                "capture %s inherited %d alias row(s) by Shader bytes", capture_id[:12], copied)
        except Exception:  # noqa: BLE001 - logging must never fail a save
            pass

    def get_capture_catalog(self, capture_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM capture_catalogs WHERE capture_id=?", (capture_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        return item

    def get_capture_shader(
        self, capture_id: str, shader_resource_id: int, stage: str, entry_point: str = ""
    ) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT * FROM capture_shaders
                   WHERE capture_id=? AND shader_resource_id=? AND stage=?
                     AND entry_point=?""",
                (capture_id, int(shader_resource_id), stage, entry_point),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        return item

    def replace_matches(
        self,
        capture_id: str,
        shader_resource_id: int,
        stage: str,
        entry_point: str,
        matches: list[dict],
    ) -> None:
        with self.connection() as db:
            db.execute(
                """DELETE FROM symbol_matches
                   WHERE capture_id=? AND shader_resource_id=? AND stage=? AND entry_point=?""",
                (capture_id, shader_resource_id, stage, entry_point),
            )
            for match in matches:
                db.execute(
                    """INSERT INTO symbol_matches(
                           capture_id, shader_resource_id, stage, entry_point, asset_shader_id,
                           status, score, selected, source, confidence, rationale,
                           evidence_json, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        capture_id,
                        shader_resource_id,
                        stage,
                        entry_point,
                        int(match["assetShaderId"]),
                        match["status"],
                        float(match["score"]),
                        1 if match.get("selected") else 0,
                        match.get("source", "program"),
                        match.get("confidence"),
                        match.get("rationale", ""),
                        _json(match.get("evidence", {})),
                        _utc_now(),
                    ),
                )

    def save_alias(self, alias: dict) -> None:
        with self.connection() as db:
            db.execute(
                """INSERT INTO aliases(
                       capture_id, shader_resource_id, stage, entry_point, binding_kind,
                       register_space, bind_number, byte_offset, canonical_path,
                       alias, original_name, provenance, confidence, verified,
                       metadata_json)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(capture_id, shader_resource_id, stage, entry_point, binding_kind,
                               register_space, bind_number, byte_offset,
                               canonical_path, alias)
                   DO UPDATE SET
                       provenance=excluded.provenance,
                       confidence=excluded.confidence,
                       verified=excluded.verified,
                       metadata_json=excluded.metadata_json""",
                (
                    alias["captureId"],
                    int(alias["shaderResourceId"]),
                    alias["stage"],
                    alias.get("entryPoint", ""),
                    alias.get("bindingKind", "shader"),
                    int(alias.get("registerSpace", -1)),
                    int(alias.get("bindNumber", -1)),
                    int(alias.get("byteOffset", -1)),
                    alias.get("canonicalPath", ""),
                    alias["alias"],
                    alias.get("originalName", ""),
                    alias.get("provenance", "program"),
                    alias.get("confidence"),
                    1 if alias.get("verified") else 0,
                    _json(alias.get("metadata", {})),
                ),
            )

    def clear_aliases(
        self,
        capture_id: str,
        shader_resource_id: int | None = None,
        stage: str | None = None,
        entry_point: str | None = None,
    ) -> None:
        query = "DELETE FROM aliases WHERE capture_id=?"
        values: list[Any] = [capture_id]
        if shader_resource_id is not None:
            query += " AND shader_resource_id=?"
            values.append(int(shader_resource_id))
        if stage is not None:
            query += " AND stage=?"
            values.append(stage)
        if entry_point is not None:
            query += " AND entry_point=?"
            values.append(entry_point)
        with self.connection() as db:
            db.execute(query, values)

    def list_aliases(self, capture_id: str, verified_only: bool = False) -> list[dict]:
        query = "SELECT * FROM aliases WHERE capture_id=?"
        values: list[Any] = [capture_id]
        if verified_only:
            query += " AND verified=1"
        query += " ORDER BY shader_resource_id, stage, entry_point, binding_kind, bind_number, byte_offset"
        with self.connection() as db:
            rows = db.execute(query, values).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            item["verified"] = bool(item["verified"])
            result.append(item)
        return result

    def list_matches(self, capture_id: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT m.*, a.relative_path, a.display_name, a.raw_sha256 AS asset_raw_sha256,
                          a.code_sha256 AS asset_code_sha256, a.layout_hash AS asset_layout_hash,
                          a.metadata_json AS asset_metadata_json
                   FROM symbol_matches m
                   JOIN asset_shaders a ON a.id=m.asset_shader_id
                   WHERE m.capture_id=?
                   ORDER BY m.shader_resource_id, m.stage, m.entry_point,
                            m.score DESC, a.relative_path""",
                (capture_id,),
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            item["selected"] = bool(item["selected"])
            item["evidence"] = json.loads(item.pop("evidence_json"))
            item["assetMetadata"] = json.loads(item.pop("asset_metadata_json"))
            result.append(item)
        return result

    def set_ai_decision(
        self,
        capture_id: str,
        shader_resource_id: int,
        stage: str,
        entry_point: str,
        asset_shader_id: int,
        status: str,
        confidence: float,
        rationale: str,
        evidence: dict,
    ) -> None:
        with self.connection() as db:
            db.execute(
                """UPDATE symbol_matches SET selected=0
                   WHERE capture_id=? AND shader_resource_id=? AND stage=? AND entry_point=?""",
                (capture_id, shader_resource_id, stage, entry_point),
            )
            db.execute(
                """INSERT INTO symbol_matches(
                       capture_id, shader_resource_id, stage, entry_point, asset_shader_id,
                       status, score, selected, source, confidence, rationale,
                       evidence_json, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, 1, 'ai', ?, ?, ?, ?)
                   ON CONFLICT(capture_id, shader_resource_id, stage, entry_point, asset_shader_id)
                   DO UPDATE SET status=excluded.status, selected=1, source='ai',
                       confidence=excluded.confidence, rationale=excluded.rationale,
                       evidence_json=excluded.evidence_json, updated_at=excluded.updated_at""",
                (
                    capture_id,
                    shader_resource_id,
                    stage,
                    entry_point,
                    asset_shader_id,
                    status,
                    confidence * 100.0,
                    confidence,
                    rationale,
                    _json(evidence),
                    _utc_now(),
                ),
            )

    def save_symbol_rule(self, rule: dict) -> None:
        with self.connection() as db:
            db.execute(
                """INSERT INTO symbol_rules(
                       scan_id, raw_sha256, code_sha256, stage, entry_point, layout_hash,
                       asset_shader_id, source, confidence, program_verified,
                       user_confirmed, rationale, evidence_json, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(scan_id, raw_sha256, code_sha256, stage, entry_point, layout_hash)
                   DO UPDATE SET asset_shader_id=excluded.asset_shader_id,
                       source=excluded.source, confidence=excluded.confidence,
                       program_verified=excluded.program_verified,
                       user_confirmed=excluded.user_confirmed,
                       rationale=excluded.rationale,
                       evidence_json=excluded.evidence_json,
                       updated_at=excluded.updated_at""",
                (
                    rule["scanId"],
                    rule.get("rawSha256", ""),
                    rule.get("codeSha256", ""),
                    rule.get("stage", ""),
                    rule.get("entryPoint", ""),
                    rule.get("layoutHash", ""),
                    int(rule["assetShaderId"]),
                    rule.get("source", "ai"),
                    rule.get("confidence"),
                    1 if rule.get("programVerified") else 0,
                    1 if rule.get("userConfirmed") else 0,
                    rule.get("rationale", ""),
                    _json(rule.get("evidence", {})),
                    _utc_now(),
                ),
            )

    def find_symbol_rule(self, scan_id: str, shader: dict) -> dict | None:
        raw_hash = str(shader.get("rawSha256", "") or "")
        code_hash = str(shader.get("codeSha256", "") or "")
        if not raw_hash and not code_hash:
            return None
        with self.connection() as db:
            rows = db.execute(
                """SELECT r.*, a.relative_path, a.display_name,
                          a.raw_sha256 AS asset_raw_sha256,
                          a.code_sha256 AS asset_code_sha256,
                          a.layout_hash AS asset_layout_hash,
                          a.metadata_json AS asset_metadata_json
                   FROM symbol_rules r
                   JOIN asset_shaders a ON a.id=r.asset_shader_id
                   WHERE r.scan_id=?
                     AND ((r.raw_sha256!='' AND r.raw_sha256=?) OR
                          (r.code_sha256!='' AND r.code_sha256=?))
                     AND (r.stage='' OR r.stage=?)
                     AND r.entry_point=?
                     AND (r.layout_hash='' OR r.layout_hash=?)
                   ORDER BY r.program_verified DESC, r.user_confirmed DESC,
                            r.confidence DESC LIMIT 1""",
                (
                    scan_id,
                    raw_hash,
                    code_hash,
                    shader.get("stage", ""),
                    shader.get("entryPoint", ""),
                    shader.get("layoutHash", ""),
                ),
            ).fetchone()
        if rows is None:
            return None
        item = dict(rows)
        item["program_verified"] = bool(item["program_verified"])
        item["user_confirmed"] = bool(item["user_confirmed"])
        item["evidence"] = json.loads(item.pop("evidence_json"))
        item["assetMetadata"] = json.loads(item.pop("asset_metadata_json"))
        return item

    def list_symbol_rules(self, scan_id: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT r.*, a.relative_path, a.display_name
                   FROM symbol_rules r JOIN asset_shaders a ON a.id=r.asset_shader_id
                   WHERE r.scan_id=?
                   ORDER BY r.stage, r.entry_point, r.raw_sha256, r.code_sha256""",
                (scan_id,),
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            item["program_verified"] = bool(item["program_verified"])
            item["user_confirmed"] = bool(item["user_confirmed"])
            item["evidence"] = json.loads(item.pop("evidence_json"))
            result.append(item)
        return result

    @staticmethod
    def _asset_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        return item
