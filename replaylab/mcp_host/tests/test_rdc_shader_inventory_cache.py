from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from src import rdc_shader_inventory_cache as inventory_cache
from src.rdc_shader_inventory_cache import (
    DEFAULT_INVENTORY_ALGORITHM_VERSION,
    DEFAULT_MATCHING_POLICY_VERSION,
    MAX_RDC_SHADER_INVENTORY_CACHE_BYTES,
    RDC_SHADER_INVENTORY_CACHE_DIRECTORY,
    RdcShaderInventoryCacheError,
    build_shader_inventory_cache_document,
    capture_path_sha256,
    encode_shader_inventory_cache,
    load_shader_inventory_cache,
    parse_shader_inventory_cache,
    shader_inventory_cache_path,
    write_shader_inventory_cache,
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _runtime_inventory() -> dict:
    material = {
        "api": "d3d12",
        "encoding": "dxil",
        "rawSha256": "a" * 64,
        "stage": "fragment",
        "entryPoint": "main",
        "abiHash": "b" * 64,
        "layoutHash": "d" * 64,
    }
    return {
        "schema": "replaylab-project-shader-resource-index/v2",
        "schemaVersion": 2,
        "capturePath": "C:/must-not-persist/capture.rdc",
        "captureSessionId": "9" * 32,
        "bridgeResult": {"private": True},
        "rawBytecodeBlobs": [
            {"sha256": "a" * 64, "encoding": "base64", "data": "c2VjcmV0"}
        ],
        "physicalApplyAuthority": True,
        "inventoryComplete": True,
        "scannedResourceCount": 3,
        "shaderResourceCount": 2,
        "uniqueShaderResourceCount": 2,
        "processedShaderResourceCount": 2,
        "indexedShaderResourceCount": 1,
        "unindexedShaderResourceCount": 1,
        "unindexedShaderResourceIds": [17],
        "inspectedEntryPointCount": 1,
        "anchorQueryCount": 1,
        "anchorUsageRecordCount": 1,
        "representativeEidCount": 1,
        "anchorPerShaderMaximum": 1,
        "eventTraversalCount": 0,
        "fullFrameTraversal": False,
        "gpuReplayMutation": False,
        "diagnostics": [
            {
                "code": "shader-stage-unavailable",
                "resourceId": 17,
                "privatePath": "C:/must-not-persist/private.pdb",
            }
        ],
        "assets": [
            {
                "resourceId": 7,
                "ownerResourceId": 7,
                "ownerCaptureSessionId": "9" * 32,
                "resourceOwnershipScope": "target-capture-session",
                "crossCaptureResourceIdReuse": False,
                "api": "d3d12",
                "encoding": "dxil",
                "stage": "fragment",
                "entryPoint": "main",
                "rawSha256": "a" * 64,
                "rawSize": 128,
                "rawBytecode": "must-not-persist",
                "rawBytecodeIncluded": True,
                "abiHash": "b" * 64,
                "interfaceHash": "c" * 64,
                "layoutHash": "d" * 64,
                "interfaceComplete": True,
                "interfaceEvidence": "null-pipeline-reflection",
                "physicalAbiSha256": "e" * 64,
                "physicalAbiValidated": True,
                "stableKeyMaterial": material,
                "stableKeySha256": _sha256(material),
                "stableKeyComplete": True,
                "identityAmbiguous": False,
                "formatSupported": True,
                "anchorEventId": 42,
                "anchorSource": "resource-usage-index",
                "requiresAnchorPreflight": True,
                "exactExecutableMatchEligible": True,
                "leaseEligible": True,
                "replacement": {"resourceId": 99},
                "bridgeResult": {"private": True},
                "path": "C:/must-not-persist/shader.bin",
                "constantBlocks": [
                    {
                        "index": 0,
                        "path": "Globals",
                        "name": "Globals",
                        "space": 0,
                        "bind": 2,
                        "byteSize": 64,
                        "layoutSha256": "f" * 64,
                        "abiSha256": "0" * 64,
                        "privateValueBytes": "must-not-persist",
                        "fields": [
                            {
                                "canonicalPath": "Color",
                                "name": "Color",
                                "absoluteByteOffset": 0,
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
                                "bitFieldOffset": 0,
                                "bitFieldSize": 0,
                                "capturedValue": [1.0, 0.5, 0.25, 1.0],
                            }
                        ],
                    }
                ],
            }
        ],
    }


class RdcShaderInventoryCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.capture = self.base / "frame.rdc"
        self.capture.write_bytes(b"renderdoc-capture")
        self.project_id = "1" * 32
        self.scope_sha256 = "2" * 64

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self) -> dict:
        return write_shader_inventory_cache(
            self.project,
            self.project_id,
            self.scope_sha256,
            self.capture,
            _runtime_inventory(),
        )

    def test_windows_file_identity_is_stable_across_python_dev_widths(self) -> None:
        wide_device = 7123484975334838747
        legacy_device = 3718926811
        inode = 3940649673966784
        self.assertEqual(wide_device & 0xFFFFFFFF, legacy_device)
        self.assertEqual(
            inventory_cache._capture_file_identity(
                SimpleNamespace(st_dev=wide_device, st_ino=inode), "nt"
            ),
            "%d:%d" % (legacy_device, inode),
        )

        common = {"sizeBytes": 2175545845, "mtimeNs": 1784896985074110400}
        legacy = {
            **common,
            "fileIdentity": "%d:%d" % (legacy_device, inode),
        }
        wide = {
            **common,
            "fileIdentity": "%d:%d" % (wide_device, inode),
        }
        with mock.patch.object(inventory_cache.os, "name", "nt"):
            self.assertTrue(
                inventory_cache._file_observations_match(legacy, wide)
            )
            self.assertFalse(
                inventory_cache._file_observations_match(
                    legacy, {**wide, "fileIdentity": "%d:%d" % (wide_device, inode + 1)}
                )
            )
            self.assertFalse(
                inventory_cache._file_observations_match(
                    legacy, {**wide, "sizeBytes": common["sizeBytes"] + 1}
                )
            )

    def test_roundtrip_is_canonical_allowlisted_and_routing_only(self) -> None:
        written = self._write()
        expected_path = shader_inventory_cache_path(self.project, self.capture)
        self.assertEqual(
            expected_path.parent.name, RDC_SHADER_INVENTORY_CACHE_DIRECTORY
        )
        self.assertEqual(expected_path.stem, capture_path_sha256(self.capture))
        payload = expected_path.read_bytes()
        self.assertEqual(payload, encode_shader_inventory_cache(written))
        self.assertEqual(payload, _canonical_bytes(json.loads(payload)))
        self.assertFalse(any(expected_path.parent.glob("*.tmp")))

        loaded = load_shader_inventory_cache(
            self.project,
            self.project_id,
            self.scope_sha256,
            self.capture,
        )
        self.assertEqual(loaded, written)
        self.assertTrue(loaded["routingOnly"])
        self.assertTrue(loaded["freshTargetValidationRequired"])
        self.assertFalse(loaded["crossCaptureResourceIdReuse"])
        asset = loaded["assets"][0]
        self.assertEqual(asset["resourceId"], 7)
        self.assertEqual(asset["anchorEventId"], 42)
        self.assertEqual(asset["stableKeyMaterial"]["rawSha256"], "a" * 64)
        self.assertEqual(asset["constantBlocks"][0]["fields"][0]["byteSize"], 16)
        self.assertTrue(asset["routingOnly"])
        self.assertTrue(asset["freshTargetValidationRequired"])
        self.assertFalse(asset["crossCaptureResourceIdReuse"])
        self.assertEqual(asset["resourceOwnershipScope"], "cached-capture-routing-only")

        text = payload.decode("utf-8")
        for forbidden in (
            '"capturePath":',
            '"captureSessionId":',
            '"ownerCaptureSessionId":',
            '"rawBytecode":',
            '"rawBytecodeBlobs":',
            '"bridgeResult":',
            '"replacement":',
            '"physicalApplyAuthority":',
            '"exactExecutableMatchEligible":',
            '"leaseEligible":',
            '"capturedValue":',
            '"privatePath":',
        ):
            self.assertNotIn(forbidden, text)

    def test_tampering_duplicate_keys_nan_and_noncanonical_json_are_rejected(self) -> None:
        self._write()
        path = shader_inventory_cache_path(self.project, self.capture)
        tampered = json.loads(path.read_bytes())
        tampered["assets"][0]["resourceId"] = 8
        path.write_bytes(_canonical_bytes(tampered))
        with self.assertRaisesRegex(RdcShaderInventoryCacheError, "sealSha256"):
            load_shader_inventory_cache(
                self.project,
                self.project_id,
                self.scope_sha256,
                self.capture,
            )

        with self.assertRaisesRegex(RdcShaderInventoryCacheError, "duplicate JSON key"):
            parse_shader_inventory_cache(b'{"schema":1,"schema":2}')
        with self.assertRaisesRegex(RdcShaderInventoryCacheError, "non-finite JSON"):
            parse_shader_inventory_cache(b'{"schema":NaN}')

        document = build_shader_inventory_cache_document(
            self.project_id,
            self.scope_sha256,
            self.capture,
            _runtime_inventory(),
        )
        with self.assertRaisesRegex(RdcShaderInventoryCacheError, "not canonical JSON"):
            parse_shader_inventory_cache(encode_shader_inventory_cache(document) + b"\n")

    def test_size_and_mtime_observation_changes_are_cache_misses(self) -> None:
        self._write()
        with self.capture.open("ab") as stream:
            stream.write(b"-changed-size")
        self.assertIsNone(
            load_shader_inventory_cache(
                self.project,
                self.project_id,
                self.scope_sha256,
                self.capture,
            )
        )

        self._write()
        current = self.capture.stat()
        changed_ns = current.st_mtime_ns + 2_000_000_000
        os.utime(self.capture, ns=(current.st_atime_ns, changed_ns))
        self.assertEqual(self.capture.stat().st_size, current.st_size)
        self.assertNotEqual(self.capture.stat().st_mtime_ns, current.st_mtime_ns)
        self.assertIsNone(
            load_shader_inventory_cache(
                self.project,
                self.project_id,
                self.scope_sha256,
                self.capture,
            )
        )

    def test_project_scope_policy_and_capture_path_mismatches_are_misses(self) -> None:
        self._write()
        self.assertIsNone(
            load_shader_inventory_cache(
                self.project, "3" * 32, self.scope_sha256, self.capture
            )
        )
        self.assertIsNone(
            load_shader_inventory_cache(
                self.project, self.project_id, "4" * 64, self.capture
            )
        )
        self.assertIsNone(
            load_shader_inventory_cache(
                self.project,
                self.project_id,
                self.scope_sha256,
                self.capture,
                inventory_algorithm_version="unique-shader-resource-reflection/v2",
            )
        )
        self.assertIsNone(
            load_shader_inventory_cache(
                self.project,
                self.project_id,
                self.scope_sha256,
                self.capture,
                matching_policy_version="stable-key-fresh-target-validation/v2",
            )
        )

        other_capture = self.base / "other.rdc"
        other_capture.write_bytes(self.capture.read_bytes())
        other_path = shader_inventory_cache_path(self.project, other_capture)
        other_path.parent.mkdir(parents=True, exist_ok=True)
        original_path = shader_inventory_cache_path(self.project, self.capture)
        other_path.write_bytes(original_path.read_bytes())
        self.assertIsNone(
            load_shader_inventory_cache(
                self.project,
                self.project_id,
                self.scope_sha256,
                other_capture,
            )
        )

    def test_directory_symlink_is_rejected(self) -> None:
        target = self.base / "redirected-derived"
        target.mkdir()
        derived = self.project / "derived"
        try:
            os.symlink(str(target), str(derived), target_is_directory=True)
        except (OSError, NotImplementedError):
            # Windows without Developer Mode cannot create symlinks. Exercise
            # the same fail-closed branch without weakening the test suite.
            original = Path.is_symlink
            with mock.patch.object(
                Path,
                "is_symlink",
                new=lambda candidate: candidate == derived or original(candidate),
            ):
                with self.assertRaisesRegex(RdcShaderInventoryCacheError, "symlink"):
                    self._write()
            return
        with self.assertRaisesRegex(RdcShaderInventoryCacheError, "symlink"):
            self._write()

    def test_cache_file_symlink_is_rejected(self) -> None:
        path = shader_inventory_cache_path(self.project, self.capture)
        path.parent.mkdir(parents=True)
        target = self.base / "redirected-cache.json"
        target.write_bytes(b"{}")
        try:
            os.symlink(str(target), str(path))
        except (OSError, NotImplementedError):
            path.write_bytes(b"{}")
            original = Path.is_symlink
            with mock.patch.object(
                Path,
                "is_symlink",
                new=lambda candidate: candidate == path or original(candidate),
            ):
                with self.assertRaisesRegex(RdcShaderInventoryCacheError, "symlink"):
                    load_shader_inventory_cache(
                        self.project,
                        self.project_id,
                        self.scope_sha256,
                        self.capture,
                    )
                with self.assertRaisesRegex(RdcShaderInventoryCacheError, "symlink"):
                    self._write()
            return
        with self.assertRaisesRegex(RdcShaderInventoryCacheError, "symlink"):
            load_shader_inventory_cache(
                self.project,
                self.project_id,
                self.scope_sha256,
                self.capture,
            )
        with self.assertRaisesRegex(RdcShaderInventoryCacheError, "symlink"):
            self._write()

    def test_oversized_cache_is_rejected_before_json_parsing(self) -> None:
        path = shader_inventory_cache_path(self.project, self.capture)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x" * (MAX_RDC_SHADER_INVENTORY_CACHE_BYTES + 1))
        with self.assertRaisesRegex(RdcShaderInventoryCacheError, "8 MiB"):
            load_shader_inventory_cache(
                self.project,
                self.project_id,
                self.scope_sha256,
                self.capture,
            )

    def test_unknown_schema_version_is_rejected_even_with_a_matching_seal(self) -> None:
        document = build_shader_inventory_cache_document(
            self.project_id,
            self.scope_sha256,
            self.capture,
            _runtime_inventory(),
        )
        document["schemaVersion"] = 999
        unsigned = dict(document)
        unsigned.pop("sealSha256")
        document["sealSha256"] = _sha256(unsigned)
        with self.assertRaisesRegex(RdcShaderInventoryCacheError, "unsupported"):
            parse_shader_inventory_cache(_canonical_bytes(document))

    def test_sealed_nested_schema_is_exact_after_runtime_allowlisting(self) -> None:
        document = build_shader_inventory_cache_document(
            self.project_id,
            self.scope_sha256,
            self.capture,
            _runtime_inventory(),
        )
        document["coverage"]["diagnostics"][0]["futureAuthority"] = True
        unsigned = dict(document)
        unsigned.pop("sealSha256")
        document["sealSha256"] = _sha256(unsigned)
        with self.assertRaisesRegex(RdcShaderInventoryCacheError, "fields are invalid"):
            parse_shader_inventory_cache(_canonical_bytes(document))

    def test_anchor_query_without_a_usage_record_is_valid_coverage(self) -> None:
        inventory = _runtime_inventory()
        inventory["assets"][0]["anchorEventId"] = None
        inventory["assets"][0]["anchorSource"] = None
        inventory["anchorQueryCount"] = 1
        inventory["anchorUsageRecordCount"] = 0
        inventory["representativeEidCount"] = 0
        document = build_shader_inventory_cache_document(
            self.project_id,
            self.scope_sha256,
            self.capture,
            inventory,
        )
        self.assertEqual(document["coverage"]["anchorQueryCount"], 1)
        self.assertEqual(document["coverage"]["anchorUsageRecordCount"], 0)
        self.assertIsNone(document["assets"][0]["anchorEventId"])

    def test_defaults_are_part_of_the_cache_binding(self) -> None:
        document = self._write()
        self.assertEqual(
            document["inventoryAlgorithmVersion"],
            DEFAULT_INVENTORY_ALGORITHM_VERSION,
        )
        self.assertEqual(
            document["matchingPolicyVersion"], DEFAULT_MATCHING_POLICY_VERSION
        )


if __name__ == "__main__":
    unittest.main()
