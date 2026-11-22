from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from src.asset_catalog import AssetCatalogError, AssetCatalogService


def _png(width: int, height: int, *, srgb: bool = True) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0) + b"\x00\x00\x00\x00"
    chunks = ihdr
    if srgb:
        chunks += struct.pack(">I", 1) + b"sRGB" + b"\x00" + b"\x00\x00\x00\x00"
    chunks += struct.pack(">I", 0) + b"IEND" + b"\x00\x00\x00\x00"
    return signature + chunks


class AssetCatalogTests(unittest.TestCase):
    def test_indexes_metadata_relationships_and_verified_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            managed = Path(temporary) / "project" / "shared" / "assets"
            (root / "models").mkdir(parents=True)
            (root / "textures").mkdir()
            (root / "materials").mkdir()
            (root / "shaders").mkdir()
            managed.mkdir(parents=True)
            (root / "textures" / "hero.png").write_bytes(_png(64, 32))
            (root / "materials" / "hero.mtl").write_text(
                "newmtl Hero\nmap_Kd ../textures/hero.png\n", encoding="utf-8"
            )
            (root / "models" / "hero.obj").write_text(
                "mtllib ../materials/hero.mtl\n"
                "v 0 0 0\nv 1 0 0\nv 0 1 0\n"
                "vt 0 0\nvt 1 0\nvt 0 1\n"
                "vn 0 0 1\nf 1/1/1 2/2/1 3/3/1\n",
                encoding="utf-8",
            )
            (root / "shaders" / "common.hlsli").write_text(
                "float4 tint;\n", encoding="utf-8"
            )
            (root / "shaders" / "hero.hlsl").write_text(
                '#include "common.hlsli"\nfloat4 main():SV_Target{return tint;}\n',
                encoding="utf-8",
            )
            service = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            result = service.scan(root, build_id="build-1")
            self.assertEqual(result["assetCount"], 5)
            self.assertGreaterEqual(result["verifiedRelationshipCount"], 3)
            query = service.query(text="hero", limit=20)
            by_path = {item["relativePath"]: item for item in query["assets"]}
            texture = by_path["textures/hero.png"]
            self.assertEqual(texture["metadata"]["width"], 64)
            self.assertEqual(texture["metadata"]["height"], 32)
            self.assertEqual(texture["metadata"]["colorSpaceEvidence"], "srgb-chunk")
            obj = by_path["models/hero.obj"]
            self.assertEqual(obj["metadata"]["faceCount"], 1)
            self.assertTrue(
                any(
                    relation["relation"] == "uses-material-library"
                    and relation["verified"] is True
                    for relation in obj["relationships"]
                )
            )
            delivery = service.resolve(
                root,
                asset_id=texture["assetId"],
                expected_catalog_sha256=result["catalogSha256"],
            )
            self.assertTrue(delivery["verified"])
            self.assertEqual(Path(delivery["absolutePath"]), root / "textures" / "hero.png")

    def test_incremental_scan_reuses_unchanged_content_and_full_rehashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            managed = Path(temporary) / "managed"
            root.mkdir()
            managed.mkdir()
            (root / "a.bin").write_bytes(b"same")
            (root / "b.bin").write_bytes(b"same")
            service = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            first = service.scan(root, build_id="b")
            second = service.scan(root, build_id="b", scan_mode="incremental")
            self.assertEqual(first["catalogSha256"], second["catalogSha256"])
            self.assertEqual(second["cache"]["reusedFiles"], 2)
            self.assertEqual(second["cache"]["hashedFiles"], 0)
            duplicate = service.query(
                sha256=hashlib.sha256((root / "a.bin").read_bytes()).hexdigest(),
                limit=20,
            )
            self.assertEqual(duplicate["count"], 2)
            full = service.scan(root, build_id="b", scan_mode="full")
            self.assertEqual(full["cache"]["hashedFiles"], 2)
            rows = service.query(kinds=["binary"], limit=20)["assets"]
            self.assertNotEqual(rows[0]["assetId"], rows[1]["assetId"])
            self.assertTrue(
                any(
                    relation["relation"] == "same-content" and relation["verified"]
                    for row in rows
                    for relation in row["relationships"]
                )
            )

    def test_interrupted_scan_leaves_a_resumable_checkpoint(self) -> None:
        import itertools
        from unittest import mock

        from src import asset_catalog

        # The checkpoint clock is stubbed, not raced. With a real clock and a
        # 100us interval, a fast disk finishes all twelve cached files inside a
        # single tick and no checkpoint fires at all -- which makes this test
        # measure the machine rather than the behaviour. One tick per call means
        # every file trips the checkpoint, on any machine.
        ticks = itertools.count(0.0, 1.0)
        clock = mock.patch.object(asset_catalog.time, "monotonic", lambda: next(ticks))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            managed = Path(temporary) / "managed"
            root.mkdir()
            managed.mkdir()
            for index in range(12):
                (root / ("f%02d.bin" % index)).write_bytes(b"content-%d" % index)
            service = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            # Every file trips the checkpoint clock; the sixth checkpoint aborts the scan.
            calls = {"n": 0}
            original = service.scan

            def _explode_after(count):
                def _progress(update):
                    if update.get("phase") == "checkpoint":
                        calls["n"] += 1
                        if calls["n"] >= count:
                            raise KeyboardInterrupt()  # simulate the task process dying
                return _progress

            with clock, self.assertRaises(KeyboardInterrupt):
                original(root, build_id="b", checkpoint_seconds=0.5, progress=_explode_after(6))
            status = service.status()
            self.assertFalse(status["complete"])
            self.assertGreaterEqual(status["assetCount"], 5)
            self.assertLess(status["assetCount"], 12)
            self.assertIn("walked", status["progress"])
            # Resume: the checkpointed files are reused, only the rest is hashed.
            resumed = service.scan(root, build_id="b", checkpoint_seconds=0)
            self.assertTrue(resumed["complete"])
            self.assertEqual(resumed["assetCount"], 12)
            self.assertGreaterEqual(resumed["cache"]["reusedFiles"], 5)
            self.assertEqual(resumed["cache"]["reusedFiles"] + resumed["cache"]["hashedFiles"], 12)
            self.assertTrue(service.status()["complete"])
            # A checkpoint never shrinks coverage below the previous complete catalog.
            (root / "f00.bin").write_bytes(b"changed")
            calls["n"] = 0
            with clock, self.assertRaises(KeyboardInterrupt):
                service.scan(root, build_id="b", checkpoint_seconds=0.5, progress=_explode_after(2))
            partial = service.status()
            self.assertFalse(partial["complete"])
            self.assertEqual(partial["assetCount"], 12)

    def test_failed_bounded_scan_preserves_previous_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            managed = Path(temporary) / "managed"
            root.mkdir()
            managed.mkdir()
            (root / "first.obj").write_text("v 0 0 0\n", encoding="utf-8")
            service = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            accepted = service.scan(root)
            (root / "second.obj").write_text("v 1 0 0\n", encoding="utf-8")
            with self.assertRaises(AssetCatalogError):
                service.scan(root, max_files=1)
            status = service.status()
            self.assertEqual(status["catalogSha256"], accepted["catalogSha256"])
            self.assertEqual(status["assetCount"], 1)

    def test_names_only_limit_counts_supported_assets_not_unrelated_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            managed = Path(temporary) / "managed"
            root.mkdir()
            managed.mkdir()
            (root / "first.uasset").write_bytes(b"package-name-only")
            (root / "second.png").write_bytes(b"texture-name-only")
            for index in range(20):
                (root / ("cache-%02d.tmp" % index)).write_bytes(b"irrelevant")

            result = AssetCatalogService(managed / "asset-catalog-v1.sqlite3").scan(
                root, depth="names", max_files=2
            )

            self.assertEqual(result["assetCount"], 2)
            self.assertEqual(result["coverage"]["walkedFiles"], 2)
            self.assertEqual(result["coverage"]["visitedFiles"], 22)
            self.assertEqual(result["coverage"]["skippedUnsupportedFiles"], 20)
            self.assertEqual(result["cache"]["hashedFiles"], 0)

    def test_names_only_still_enforces_supported_asset_and_physical_walk_bounds(self) -> None:
        from src import asset_catalog

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            managed = Path(temporary) / "managed"
            root.mkdir()
            managed.mkdir()
            for index in range(3):
                (root / ("asset-%02d.uasset" % index)).write_bytes(b"asset")
            service = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            with self.assertRaisesRegex(AssetCatalogError, "more than 2 files"):
                service.scan(root, depth="names", max_files=2)

            for path in root.iterdir():
                path.unlink()
            for index in range(4):
                (root / ("ignored-%02d.tmp" % index)).write_bytes(b"ignored")
            with mock.patch.object(asset_catalog, "HARD_MAX_FILESYSTEM_ENTRIES", 3):
                with self.assertRaisesRegex(AssetCatalogError, "filesystem traversal bound"):
                    service.scan(root, depth="names")

    def test_engine_detection_keeps_only_a_bounded_supported_name_sample(self) -> None:
        from src import asset_catalog
        from src.symbols import engines

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            root.mkdir()
            for index in range(8):
                (root / ("ignored-%02d.tmp" % index)).write_bytes(b"ignored")
            for index in range(7):
                (root / ("asset-%02d.uasset" % index)).write_bytes(b"asset")

            captured: list[list[str]] = []

            def observe(paths):
                captured.append(list(paths))
                return engines.EngineContext("unknown", {}, {})

            with mock.patch.object(engines, "detect_engine", side_effect=observe):
                asset_catalog.AssetCatalogService._detect_engine(root, 3)

            self.assertEqual(len(captured[0]), 3)
            self.assertTrue(all(path.endswith(".uasset") for path in captured[0]))

    def test_switching_roots_replaces_assets_and_derived_indexes(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root_a = base / "root-a"
            root_b = base / "root-b"
            managed = base / "managed"
            root_a.mkdir()
            root_b.mkdir()
            managed.mkdir()
            (root_a / "only-a.bin").write_bytes(b"root-a")
            (root_b / "only-b.bin").write_bytes(b"root-b")
            database = managed / "asset-catalog-v1.sqlite3"
            service = AssetCatalogService(database)
            service.scan(root_a, build_id="build")

            # Seed both derived accelerator tables with valid root-A ownership.
            # Their parser inputs are deliberately irrelevant to this regression:
            # changing roots must replace every table owned by the old catalog.
            with closing(sqlite3.connect(str(database))) as connection:
                old_asset_id = connection.execute(
                    "SELECT asset_id FROM assets WHERE relative_path='only-a.bin'"
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO asset_values(value_key, asset_id) VALUES(?, ?)",
                    ("1.25", old_asset_id),
                )
                connection.execute(
                    "INSERT INTO asset_thumbnails(asset_id) VALUES(?)", (old_asset_id,)
                )
                connection.commit()

            switched = service.scan(root_b, build_id="build")

            self.assertEqual(switched["assetCount"], 1)
            self.assertEqual(service.status()["rootPath"], str(root_b.resolve()))
            with closing(sqlite3.connect(str(database))) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT relative_path FROM assets ORDER BY relative_path"
                    ).fetchall(),
                    [("only-b.bin",)],
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM asset_values WHERE asset_id=?",
                        (old_asset_id,),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM asset_thumbnails WHERE asset_id=?",
                        (old_asset_id,),
                    ).fetchone()[0],
                    0,
                )

    def test_incompatible_catalog_fails_closed_when_old_database_cannot_be_removed(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root_a = base / "root-a"
            root_b = base / "root-b"
            managed = base / "managed"
            root_a.mkdir()
            root_b.mkdir()
            managed.mkdir()
            (root_a / "only-a.bin").write_bytes(b"root-a")
            (root_b / "only-b.bin").write_bytes(b"root-b")
            database = managed / "asset-catalog-v1.sqlite3"
            service = AssetCatalogService(database)
            accepted = service.scan(root_a, build_id="build")
            with closing(sqlite3.connect(str(database))) as connection:
                old_asset_id = connection.execute(
                    "SELECT asset_id FROM assets WHERE relative_path='only-a.bin'"
                ).fetchone()[0]
                connection.execute(
                    "UPDATE metadata SET value='999' WHERE key='schemaVersion'"
                )
                connection.commit()

            original_unlink = Path.unlink

            def reject_locked_catalog(path: Path, *args, **kwargs):
                if path == database:
                    raise PermissionError(32, "catalog database is locked", str(path))
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", new=reject_locked_catalog):
                with self.assertRaisesRegex(
                    AssetCatalogError, "retire|remove|replace|locked|catalog"
                ):
                    service.scan(root_b, build_id="build")

            # A failed incompatible-database replacement must not relabel root-A
            # rows as root B or publish a mixed generation. The schema remains
            # deliberately invalid, so inspect the untouched bytes directly.
            with closing(sqlite3.connect(str(database))) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT key, value FROM metadata WHERE key IN "
                        "('rootPath', 'catalogSha256', 'schemaVersion') ORDER BY key"
                    ).fetchall(),
                    [
                        ("catalogSha256", accepted["catalogSha256"]),
                        ("rootPath", str(root_a.resolve())),
                        ("schemaVersion", "999"),
                    ],
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT asset_id, relative_path FROM assets"
                    ).fetchall(),
                    [(old_asset_id, "only-a.bin")],
                )

    def test_root_switch_rolls_back_when_transactional_reset_fails(self) -> None:
        import sqlite3

        from src import asset_catalog

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root_a = base / "root-a"
            root_b = base / "root-b"
            managed = base / "managed"
            root_a.mkdir()
            root_b.mkdir()
            managed.mkdir()
            (root_a / "only-a.bin").write_bytes(b"root-a")
            (root_b / "only-b.bin").write_bytes(b"root-b")
            service = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            accepted = service.scan(root_a, build_id="build")

            # Fail after the reset transaction has dropped the old tables but
            # before it can publish new metadata.  Rollback must restore the
            # complete root-A catalog rather than leave an empty/mixed root B.
            with mock.patch.object(
                asset_catalog,
                "_fts5_available",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    service.scan(root_b, build_id="build")

            status = service.status()
            self.assertEqual(status["rootPath"], str(root_a.resolve()))
            self.assertEqual(status["catalogSha256"], accepted["catalogSha256"])
            self.assertEqual(
                [item["relativePath"] for item in service.query(text="only-a")["assets"]],
                ["only-a.bin"],
            )

    def test_delivery_rejects_stale_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            managed = Path(temporary) / "managed"
            root.mkdir()
            managed.mkdir()
            path = root / "hero.obj"
            path.write_text("v 0 0 0\n", encoding="utf-8")
            service = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            indexed = service.scan(root)
            item = service.query(text="hero")["assets"][0]
            path.write_text("v 9 9 9\n", encoding="utf-8")
            with self.assertRaises(AssetCatalogError):
                service.resolve(
                    root,
                    asset_id=item["assetId"],
                    expected_catalog_sha256=indexed["catalogSha256"],
                )

    def test_indexes_uex_bundle_metadata_and_transitive_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            managed = Path(temporary) / "managed"
            bundle = root / "bundle"
            (bundle / "meshes").mkdir(parents=True)
            (bundle / "materials").mkdir()
            (bundle / "textures").mkdir()
            managed.mkdir()
            (bundle / "meshes" / "lod0.glb").write_bytes(b"glTF")
            (bundle / "materials" / "body.json").write_text("{}\n", encoding="utf-8")
            (bundle / "textures" / "base.png").write_bytes(_png(8, 8))
            (bundle / "textures" / "normal.png").write_bytes(_png(8, 8, srgb=False))
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "uex.blender-bundle.v1",
                        "sourceAsset": "/Game/Hero/SK_Hero",
                        "assetType": "USkeletalMesh",
                        "meshName": "Hero",
                        "skeleton": "/Game/Hero/SKEL_Hero",
                        "boneCount": 32,
                        "sourceLodCount": 1,
                        "morphTargetCount": 2,
                        "meshes": [{"lodIndex": 0, "relativePath": "meshes/lod0.glb"}],
                        "materials": [
                            {
                                "materialJson": "materials/body.json",
                                "rawMaterialJson": None,
                                "baseColor": {"relativePath": "textures/base.png"},
                                "directTextureParameters": [
                                    {"relativePath": "textures/normal.png"}
                                ],
                                "textureParameters": [],
                            }
                        ],
                        "textures": [
                            {"relativePath": "textures/base.png"},
                            {"relativePath": "textures/normal.png"},
                        ],
                        "warnings": [],
                    }
                ),
                encoding="utf-8",
            )

            service = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            service.scan(root)
            item = service.query(text="manifest.json", kinds=["metadata"])["assets"][0]

            self.assertEqual(item["metadata"]["parser"], "uex.blender-bundle.v1")
            self.assertEqual(item["metadata"]["assetType"], "USkeletalMesh")
            self.assertEqual(item["metadata"]["sourceAsset"], "/Game/Hero/SK_Hero")
            relations = {(edge["relation"], edge["targetPath"]): edge for edge in item["relationships"]}
            self.assertTrue(relations[("contains-lod-geometry", "bundle/meshes/lod0.glb")]["verified"])
            self.assertTrue(relations[("uses-material-record", "bundle/materials/body.json")]["verified"])
            self.assertTrue(relations[("uses-texture-parameter", "bundle/textures/normal.png")]["verified"])


    def test_index_is_updated_in_place_not_rewritten(self) -> None:
        """One live index: unchanged rows stay, gone files drop, bulk data is names-only."""

        import sqlite3
        import time as _time

        from src import asset_catalog

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            managed = Path(temporary) / "managed"
            (root / "Content").mkdir(parents=True)
            managed.mkdir()
            (root / "Content" / "hero.mtl").write_text("newmtl Hero\nKd 0.12345 0.2 0.3\n", encoding="utf-8")
            (root / "Content" / "hero.png").write_bytes(_png(8, 8))
            (root / "Content" / "hero.ubulk").write_bytes(bytes(range(256)) * 64)
            # Leftovers of the old whole-file rewrite are cleaned up, not accumulated.
            stale = managed / ".asset-catalog-v1.sqlite3.tmp-deadbeef"
            stale.write_bytes(b"x" * 10)
            os.utime(stale, (_time.time() - 8 * 3600, _time.time() - 8 * 3600))
            service = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            first = service.scan(root, build_id="b", depth="headers")
            self.assertFalse(stale.exists())
            self.assertEqual(first["assetCount"], 3)
            bulk = service.query(text="hero.ubulk")["assets"][0]
            self.assertEqual(bulk["hashStatus"], "deferred")
            database = managed / "asset-catalog-v1.sqlite3"
            inode_before = database.stat().st_ino
            connection = sqlite3.connect(str(database))
            try:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(str(mode).lower(), "wal")

            # A changed file rewrites only its own row; a removed one loses its row.
            (root / "Content" / "hero.mtl").write_text("newmtl Hero\nKd 0.54321 0.2 0.3\n", encoding="utf-8")
            (root / "Content" / "hero.png").unlink()
            second = service.scan(root, build_id="b", depth="headers")
            self.assertEqual(database.stat().st_ino, inode_before)
            self.assertEqual(second["assetCount"], 2)
            self.assertEqual(second["cache"]["writtenFiles"], 1)
            self.assertEqual(second["cache"]["removedFiles"], 1)
            self.assertEqual(second["cache"]["reusedFiles"], 1)
            self.assertNotEqual(first["catalogSha256"], second["catalogSha256"])
            self.assertEqual(service.status()["generation"], 2)
            # Identity is content-derived: nothing changed, nothing moves.
            third = service.scan(root, build_id="b", depth="headers")
            self.assertEqual(third["catalogSha256"], second["catalogSha256"])
            self.assertTrue(service.verify()["verified"])

            # Hydration upgrades one deferred row without walking the tree.
            with mock.patch.object(asset_catalog.AssetCatalogService, "_detect_engine",
                                   side_effect=AssertionError("hydrate must not walk")):
                hydrated = service.hydrate(root, ["Content/hero.ubulk"])
            self.assertEqual(hydrated["hydrated"], ["Content/hero.ubulk"])
            bulk = service.query(text="hero.ubulk")["assets"][0]
            self.assertEqual(bulk["hashStatus"], "sha256")

            # A tampered row is refused when it is delivered.
            connection = sqlite3.connect(str(database))
            try:
                connection.execute("UPDATE assets SET kind='other' WHERE relative_path='Content/hero.mtl'")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(AssetCatalogError, "embedded manifest hash"):
                service.query(text="hero.mtl")

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links unavailable")
    def test_child_symlink_is_never_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "assets"
            managed = base / "managed"
            root.mkdir()
            managed.mkdir()
            outside = base / "outside.bin"
            outside.write_bytes(b"secret")
            link = root / "redirect.bin"
            try:
                os.symlink(outside, link)
            except OSError:
                self.skipTest("test process cannot create symbolic links")
            result = AssetCatalogService(managed / "asset-catalog-v1.sqlite3").scan(root)
            self.assertEqual(result["assetCount"], 0)
            self.assertEqual(result["diagnostics"]["skippedSymlinks"], 1)


if __name__ == "__main__":
    unittest.main()
