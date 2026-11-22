from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest

from src.symbols.dxbc import fingerprint_bytes
from src.symbols.scanner import (
    _exclusive_scan_lock,
    interface_hash,
    interface_is_complete,
    layout_hash,
    normalize_stage,
    scan_asset_root,
)
from src.symbols.store import SCHEMA_VERSION, SymbolStore


def _record(
    path: str,
    raw_hash: str,
    code_hash: str,
    stage: str = "fragment",
    entry_point: str = "main",
    layout: str = "",
) -> dict:
    return {
        "relativePath": path,
        "fileOffset": 0,
        "byteSize": 64,
        "kind": "DXBC",
        "rawSha256": raw_hash,
        "codeSha256": code_hash,
        "stage": stage,
        "entryPoint": entry_point,
        "displayName": Path(path).stem,
        "layoutHash": layout,
        "metadata": {"name": Path(path).stem},
        "provenance": "manifest:test:0",
    }


def _complete_interface() -> dict:
    return {
        "interfaceComplete": True,
        "inputSignature": [],
        "outputSignature": [],
        "readOnlyResources": [],
        "readWriteResources": [],
        "samplers": [],
        "constantBuffers": [],
        "threadGroupSize": [],
        "outputTopology": "Unknown",
        "dynamicLinkageInterfaceCount": 0,
    }


def _make_dxbc(code: bytes = b"compiled instructions") -> bytes:
    body = code + b"\x00" * (-len(code) % 4)
    program = struct.pack("<2I", 0x00000050, 2 + len(body) // 4) + body
    chunk = b"SHDR" + struct.pack("<I", len(program)) + program
    offset = 36
    total_size = offset + len(chunk)
    return (
        b"DXBC"
        + b"\x00" * 16
        + struct.pack("<I", 1)
        + struct.pack("<I", total_size)
        + struct.pack("<I", 1)
        + struct.pack("<I", offset)
        + chunk
    )


class ScannerHardeningTests(unittest.TestCase):
    def test_stage_aliases_use_project_canonical_names(self) -> None:
        self.assertEqual(normalize_stage("amplification"), "task")
        self.assertEqual(normalize_stage("raygeneration"), "raygen")
        self.assertEqual(normalize_stage("ray generation"), "raygen")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.assets = self.base / "assets"
        self.assets.mkdir()
        self.database = self.base / "symbols.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_manifest(self, **overrides: object) -> Path:
        shader = {
            "name": "RealMaterial",
            "path": "Game/Shaders/RealMaterial.shader",
            "stage": "pixel",
            "entryPoint": "main",
            "rawSha256": "a" * 64,
            "codeSha256": "b" * 64,
            **overrides,
        }
        path = self.assets / "symbols.json"
        path.write_text(
            json.dumps(
                {"schema": "renderdoc-ultra-symbols/v1", "shaders": [shader]}
            ),
            encoding="utf-8",
        )
        return path

    def test_manifest_cannot_self_declare_hash_or_incomplete_abi_verification(self) -> None:
        self._write_manifest(hashVerified=True, interfaceComplete=True)
        summary = scan_asset_root(self.assets, self.database, build_id="build")
        self.assertEqual(summary["status"], "complete")

        rows = SymbolStore(self.database).list_asset_shaders(summary["scanId"])
        self.assertEqual(len(rows), 1)
        metadata = rows[0]["metadata"]
        self.assertTrue(metadata["_claimedHashVerified"])
        self.assertFalse(metadata["_hashVerified"])
        self.assertEqual(metadata["_hashVerification"], "untrusted-manifest-claim")
        self.assertFalse(metadata["_symbolMetadataTrusted"])
        self.assertFalse(metadata["_interfaceComplete"])

    def test_manifest_is_verified_only_when_bound_bytes_are_reparsed(self) -> None:
        blob = _make_dxbc()
        fingerprint = fingerprint_bytes(blob)
        self.assertIsNotNone(fingerprint)
        (self.assets / "packed.bin").write_bytes(blob)
        self._write_manifest(
            blobPath="packed.bin",
            fileOffset=0,
            byteSize=len(blob),
            rawSha256=fingerprint.raw_sha256,
            codeSha256=fingerprint.code_sha256,
            hashVerified=False,
        )

        summary = scan_asset_root(self.assets, self.database, build_id="build")
        rows = SymbolStore(self.database).list_asset_shaders(summary["scanId"])
        bound = [
            item for item in rows if item["provenance"] == "manifest-bound-container"
        ]
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0]["relative_path"], "Game/Shaders/RealMaterial.shader")
        self.assertEqual(
            bound[0]["metadata"]["_boundContainer"]["relativePath"], "packed.bin"
        )
        self.assertTrue(bound[0]["metadata"]["_hashVerified"])
        self.assertEqual(
            bound[0]["metadata"]["_hashVerification"], "scanner-bound-container"
        )
        self.assertFalse(bound[0]["metadata"]["_symbolMetadataTrusted"])

    def test_same_scan_id_lock_fails_closed_and_preserves_owner_lock(self) -> None:
        normalized_root = self.assets.resolve()
        scan_key = "%s\n%s" % (os.path.normcase(str(normalized_root)), "build")
        scan_id = hashlib.sha256(scan_key.encode("utf-8")).hexdigest()[:24]
        lock_path = self.database.parent / (scan_id + ".scan.lock")
        with _exclusive_scan_lock(lock_path):
            with self.assertRaisesRegex(RuntimeError, "already locked"):
                scan_asset_root(self.assets, self.database, build_id="build")
            self.assertTrue(lock_path.is_file())
        self.assertFalse(lock_path.exists())

    def test_partial_scan_preserves_rows_until_a_complete_scan_can_prune(self) -> None:
        manifest = self._write_manifest()
        first = scan_asset_root(self.assets, self.database, build_id="build")
        self.assertEqual(first["shaderRecords"], 1)
        manifest.unlink()
        (self.assets / "a.bin").write_bytes(b"a")
        (self.assets / "b.bin").write_bytes(b"b")

        partial = scan_asset_root(
            self.assets, self.database, build_id="build", max_files=1
        )
        self.assertEqual(partial["status"], "partial")
        self.assertFalse(partial["prunedMissingRecords"])
        self.assertIn("max-files-reached", partial["partialReasons"])
        self.assertEqual(partial["shaderRecords"], 1)

        complete = scan_asset_root(
            self.assets, self.database, build_id="build", max_files=100
        )
        self.assertEqual(complete["status"], "complete")
        self.assertTrue(complete["prunedMissingRecords"])
        self.assertEqual(complete["shaderRecords"], 0)

    def test_broken_previous_manifest_is_partial_and_keeps_last_good_rows(self) -> None:
        manifest = self._write_manifest()
        first = scan_asset_root(self.assets, self.database, build_id="build")
        manifest.write_text("{broken", encoding="utf-8")

        second = scan_asset_root(self.assets, self.database, build_id="build")
        self.assertEqual(second["status"], "partial")
        self.assertIn("read-or-parse-errors", second["partialReasons"])
        self.assertEqual(second["shaderRecords"], first["shaderRecords"])

    def test_future_manifest_schema_is_refused(self) -> None:
        (self.assets / "symbols.json").write_text(
            json.dumps(
                {
                    "schema": "renderdoc-ultra-symbols/v999",
                    "shaders": [{"rawSha256": "a" * 64}],
                }
            ),
            encoding="utf-8",
        )
        summary = scan_asset_root(self.assets, self.database)
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["shaderRecords"], 0)
        self.assertTrue(any("unsupported" in item["error"] for item in summary["errors"]))

    def test_abi_hash_covers_masks_bindings_block_kind_and_thread_dimensions(self) -> None:
        base = _complete_interface()
        base["inputSignature"] = [
            {
                "semanticName": "TEXCOORD",
                "semanticIdxName": "TEXCOORD0",
                "semanticIndex": 0,
                "regIndex": 0,
                "systemValue": "Undefined",
                "compType": "Float",
                "compCount": 2,
                "stream": 0,
                "regChannelMask": 3,
                "channelUsedMask": 3,
                "perPrimitiveRate": False,
                "needSemanticIndex": False,
            }
        ]
        base["constantBuffers"] = [
            {
                "space": 0,
                "bind": 0,
                "bindArraySize": 1,
                "byteSize": 16,
                "bufferBacked": True,
                "inlineDataBytes": False,
                "compileConstants": False,
                "fields": [
                    {
                        "byteOffset": 0,
                        "byteSize": 16,
                        "baseType": "Float",
                        "rows": 1,
                        "columns": 4,
                        "elements": 1,
                        "arrayByteStride": 0,
                        "matrixByteStride": 0,
                        "rowMajor": False,
                        "flags": 0,
                        "pointerTypeID": 0xFFFFFFFF,
                        "members": [],
                    }
                ],
            }
        ]
        base["readOnlyResources"] = [
            {
                "space": 0,
                "bind": 4,
                "arraySize": 1,
                "descriptorType": "ReadOnlyBuffer",
                "textureType": "Buffer",
                "variableType": {
                    "baseType": "Float",
                    "rows": 1,
                    "columns": 4,
                    "elements": 1,
                    "arrayByteStride": 0,
                    "matrixByteStride": 0,
                    "rowMajor": False,
                    "flags": 0,
                    "pointerTypeID": 0xFFFFFFFF,
                    "byteSize": 16,
                    "members": [
                        {
                            "byteOffset": 0,
                            "byteSize": 16,
                            "baseType": "Float",
                            "rows": 1,
                            "columns": 4,
                            "elements": 1,
                            "arrayByteStride": 0,
                            "matrixByteStride": 0,
                            "rowMajor": False,
                            "flags": 0,
                            "pointerTypeID": 0xFFFFFFFF,
                            "members": [],
                        }
                    ],
                },
                "isTexture": False,
                "hasSampler": False,
                "isInputAttachment": False,
                "isReadOnly": True,
            }
        ]
        base["samplers"] = [{"space": 0, "bind": 1, "arraySize": 1}]
        self.assertTrue(interface_is_complete(base))
        for key in (
            "descriptorType",
            "hasSampler",
            "isInputAttachment",
            "isReadOnly",
        ):
            incomplete = json.loads(json.dumps(base))
            incomplete["readOnlyResources"][0].pop(key)
            self.assertFalse(interface_is_complete(incomplete), key)
        for key in ("flags", "pointerTypeID"):
            incomplete = json.loads(json.dumps(base))
            incomplete["readOnlyResources"][0]["variableType"].pop(key)
            self.assertFalse(interface_is_complete(incomplete), key)
            incomplete = json.loads(json.dumps(base))
            incomplete["constantBuffers"][0]["fields"][0].pop(key)
            self.assertFalse(interface_is_complete(incomplete), key)
        original_hash = interface_hash(base)

        bridge_spelling = json.loads(json.dumps(base))
        bridge_spelling["constantBlocks"] = bridge_spelling.pop("constantBuffers")
        self.assertEqual(original_hash, interface_hash(bridge_spelling))
        self.assertEqual(layout_hash(base), layout_hash(bridge_spelling))

        changed_mask = json.loads(json.dumps(base))
        changed_mask["inputSignature"][0]["regChannelMask"] = 12
        changed_mask["inputSignature"][0]["compCount"] = 2
        self.assertNotEqual(original_hash, interface_hash(changed_mask))

        changed_semantic_name = json.loads(json.dumps(base))
        changed_semantic_name["inputSignature"][0]["semanticIdxName"] = "TEXCOORD1"
        self.assertNotEqual(original_hash, interface_hash(changed_semantic_name))

        changed_rate = json.loads(json.dumps(base))
        changed_rate["inputSignature"][0]["perPrimitiveRate"] = True
        self.assertNotEqual(original_hash, interface_hash(changed_rate))

        changed_block_kind = json.loads(json.dumps(base))
        changed_block_kind["constantBuffers"][0]["inlineDataBytes"] = True
        self.assertNotEqual(original_hash, interface_hash(changed_block_kind))

        changed_resource_type = json.loads(json.dumps(base))
        changed_resource_type["readOnlyResources"][0]["variableType"]["columns"] = 3
        self.assertNotEqual(original_hash, interface_hash(changed_resource_type))

        changed_resource_access = json.loads(json.dumps(base))
        changed_resource_access["readOnlyResources"][0]["isReadOnly"] = False
        self.assertNotEqual(original_hash, interface_hash(changed_resource_access))

        changed_resource_flags = json.loads(json.dumps(base))
        changed_resource_flags["readOnlyResources"][0]["hasSampler"] = True
        self.assertNotEqual(original_hash, interface_hash(changed_resource_flags))

        changed_resource_pointer = json.loads(json.dumps(base))
        changed_resource_pointer["readOnlyResources"][0]["variableType"][
            "pointerTypeID"
        ] = 7
        self.assertNotEqual(original_hash, interface_hash(changed_resource_pointer))

        changed_resource_member = json.loads(json.dumps(base))
        changed_resource_member["readOnlyResources"][0]["variableType"]["members"][
            0
        ]["flags"] = 4
        self.assertNotEqual(original_hash, interface_hash(changed_resource_member))

        changed_field_flags = json.loads(json.dumps(base))
        changed_field_flags["constantBuffers"][0]["fields"][0]["flags"] = 1
        self.assertNotEqual(original_hash, interface_hash(changed_field_flags))

        changed_field_pointer = json.loads(json.dumps(base))
        changed_field_pointer["constantBuffers"][0]["fields"][0][
            "pointerTypeID"
        ] = 11
        self.assertNotEqual(original_hash, interface_hash(changed_field_pointer))

        changed_topology = json.loads(json.dumps(base))
        changed_topology["outputTopology"] = "TriangleList"
        self.assertNotEqual(original_hash, interface_hash(changed_topology))

        changed_dynamic_linkage = json.loads(json.dumps(base))
        changed_dynamic_linkage["dynamicLinkageInterfaceCount"] = 1
        self.assertNotEqual(original_hash, interface_hash(changed_dynamic_linkage))

        compute_a = json.loads(json.dumps(base))
        compute_b = json.loads(json.dumps(base))
        compute_a["threadGroupSize"] = [8, 8, 1]
        compute_b["threadGroupSize"] = ["8", "8", "1"]
        self.assertEqual(interface_hash(compute_a), interface_hash(compute_b))

        changed_bitfield = json.loads(json.dumps(base))
        changed_bitfield["constantBuffers"][0]["fields"] = [
            {"offset": 0, "type": "uint", "bitFieldOffset": 1, "bitFieldSize": 3}
        ]
        another_bitfield = json.loads(json.dumps(changed_bitfield))
        another_bitfield["constantBuffers"][0]["fields"][0]["bitFieldOffset"] = 2
        self.assertNotEqual(layout_hash(changed_bitfield), layout_hash(another_bitfield))

    def test_complete_interface_requires_the_new_physical_abi_fields(self) -> None:
        base = _complete_interface()
        base["inputSignature"] = [
            {
                "semanticName": "POSITION",
                "semanticIdxName": "POSITION0",
                "semanticIndex": 0,
                "regIndex": 0,
                "systemValue": "Undefined",
                "compType": "Float",
                "compCount": 4,
                "stream": 0,
                "regChannelMask": 15,
                "channelUsedMask": 15,
                "perPrimitiveRate": False,
                "needSemanticIndex": False,
            }
        ]
        self.assertTrue(interface_is_complete(base))

        for key in (
            "semanticIdxName",
            "perPrimitiveRate",
            "needSemanticIndex",
        ):
            incomplete = json.loads(json.dumps(base))
            incomplete["inputSignature"][0].pop(key)
            self.assertFalse(interface_is_complete(incomplete), key)

        for key in ("outputTopology", "dynamicLinkageInterfaceCount"):
            incomplete = json.loads(json.dumps(base))
            incomplete.pop(key)
            self.assertFalse(interface_is_complete(incomplete), key)

    def test_interface_hash_is_name_and_declaration_order_independent(self) -> None:
        base = _complete_interface()
        binding = {
            "space": 0,
            "bind": 1,
            "arraySize": 1,
            "descriptorType": "ReadOnlyBuffer",
            "resourceType": "ReadOnlyBuffer",
            "textureType": "Buffer",
            "variableType": {
                "baseType": "Float",
                "rows": 1,
                "columns": 4,
                "elements": 1,
                "arrayByteStride": 0,
                "matrixByteStride": 0,
                "rowMajor": False,
                "flags": 0,
                "pointerTypeID": 0xFFFFFFFF,
                "byteSize": 16,
                "members": [],
            },
            "isTexture": False,
            "hasSampler": False,
            "isInputAttachment": False,
            "isReadOnly": True,
        }
        another = json.loads(json.dumps(binding))
        another["bind"] = 2
        another["name"] = "provider-name"
        base["readOnlyResources"] = [binding, another]
        reordered = json.loads(json.dumps(base))
        reordered["readOnlyResources"].reverse()
        reordered["readOnlyResources"][0]["name"] = "different-display-name"
        self.assertEqual(interface_hash(base), interface_hash(reordered))


class StoreV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.database = self.base / "symbols.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _store_with_scan(self) -> tuple[SymbolStore, str]:
        store = SymbolStore(self.database)
        scan_id = "scan"
        store.replace_asset_scan(
            scan_id,
            str(self.base),
            "build",
            {"scanId": scan_id, "status": "complete"},
        )
        return store, scan_id

    def test_future_database_schema_is_rejected_without_downgrade_or_mutation(self) -> None:
        db = sqlite3.connect(str(self.database))
        try:
            db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute(
                "INSERT INTO metadata(key, value) VALUES('schema_version', '999')"
            )
            db.execute("CREATE TABLE future_only(value TEXT)")
            db.commit()
        finally:
            db.close()

        with self.assertRaisesRegex(RuntimeError, "newer"):
            SymbolStore(self.database)

        db = sqlite3.connect(str(self.database))
        try:
            version = db.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            db.close()
        self.assertEqual(version, "999")
        self.assertIn("future_only", tables)
        self.assertNotIn("asset_scans", tables)

    def test_current_schema_version_is_written_once(self) -> None:
        SymbolStore(self.database)
        db = sqlite3.connect(str(self.database))
        try:
            version = db.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(version, str(SCHEMA_VERSION))

    def test_multi_entry_assets_captures_and_matches_are_isolated(self) -> None:
        store, scan_id = self._store_with_scan()
        raw_hash = hashlib.sha256(b"raw").hexdigest()
        code_hash = hashlib.sha256(b"code").hexdigest()
        first_id = store.add_asset_shader(
            scan_id, _record("shared.bin", raw_hash, code_hash, entry_point="first")
        )
        second_id = store.add_asset_shader(
            scan_id, _record("shared.bin", raw_hash, code_hash, entry_point="second")
        )
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(len(store.list_asset_shaders(scan_id)), 2)

        store.save_capture_catalog(
            "catalog.json",
            {
                "captureId": "capture",
                "shaders": [
                    {
                        "shaderResourceId": 7,
                        "stage": "fragment",
                        "entryPoint": "first",
                        "rawSha256": raw_hash,
                        "name": "first-reflection",
                    },
                    {
                        "shaderResourceId": 7,
                        "stage": "fragment",
                        "entryPoint": "second",
                        "rawSha256": raw_hash,
                        "name": "second-reflection",
                    },
                ],
            },
        )
        self.assertIsNone(store.get_capture_shader("capture", 7, "fragment", ""))
        first_capture = store.get_capture_shader("capture", 7, "fragment", "first")
        second_capture = store.get_capture_shader("capture", 7, "fragment", "second")
        self.assertEqual(first_capture["metadata"]["name"], "first-reflection")
        self.assertEqual(second_capture["metadata"]["name"], "second-reflection")

        store.replace_matches(
            "capture",
            7,
            "fragment",
            "first",
            [{"assetShaderId": first_id, "status": "ExactBlob", "score": 100}],
        )
        store.replace_matches(
            "capture",
            7,
            "fragment",
            "second",
            [{"assetShaderId": second_id, "status": "ExactBlob", "score": 100}],
        )
        matches = store.list_matches("capture")
        self.assertEqual(
            {(item["entry_point"], item["asset_shader_id"]) for item in matches},
            {("first", first_id), ("second", second_id)},
        )

    def test_candidate_ranking_happens_in_sql_before_limit(self) -> None:
        store, scan_id = self._store_with_scan()
        raw_hash = "a" * 64
        code_hash = "b" * 64
        layout = "layout"
        wrong_id = store.add_asset_shader(
            scan_id,
            _record(
                "a-wrong-stage.bin",
                raw_hash,
                code_hash,
                stage="vertex",
                entry_point="other",
                layout=layout,
            ),
        )
        right_id = store.add_asset_shader(
            scan_id,
            _record(
                "z-right-stage.bin",
                raw_hash,
                code_hash,
                stage="fragment",
                entry_point="main",
                layout=layout,
            ),
        )
        self.assertNotEqual(wrong_id, right_id)

        candidates = store.find_asset_candidates(
            scan_id,
            raw_hash,
            code_hash,
            "fragment",
            layout,
            limit=1,
            entry_point="main",
        )
        self.assertEqual([item["id"] for item in candidates], [right_id])


if __name__ == "__main__":
    unittest.main()
