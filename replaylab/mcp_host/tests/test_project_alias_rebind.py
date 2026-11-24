from __future__ import annotations

import copy
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import project_alias_rebind
from src import projects as project_model
from src.projects import ReplayProjectError, ReplayProjectService
from src.symbols.dxbc import fingerprint_bytes
from src.symbols.matcher import ALIAS_SCHEMA, ALIAS_VERSION, capture_identity


def _dxbc(code: bytes = b"compiled instructions", metadata: bytes = b"") -> bytes:
    body = code + b"\x00" * (-len(code) % 4)
    program = struct.pack("<2I", 0x00000050, 2 + len(body) // 4) + body
    chunks = [b"SHDR" + struct.pack("<I", len(program)) + program]
    if metadata:
        chunks.append(b"RDEF" + struct.pack("<I", len(metadata)) + metadata)
    header_size = 32 + len(chunks) * 4
    offsets = []
    cursor = header_size
    for chunk in chunks:
        offsets.append(cursor)
        cursor += len(chunk)
    return (
        b"DXBC"
        + b"\x00" * 16
        + struct.pack("<I", 1)
        + struct.pack("<I", cursor)
        + struct.pack("<I", len(chunks))
        + b"".join(struct.pack("<I", offset) for offset in offsets)
        + b"".join(chunks)
    )


def _interface(bind_number: int = 0) -> dict:
    return {
        "interfaceComplete": True,
        "inputSignature": [],
        "outputSignature": [],
        "readOnlyResources": [],
        "readWriteResources": [],
        "samplers": [],
        "constantBuffers": [
            {
                "name": "PerMaterial",
                "space": 0,
                "bind": bind_number,
                "arraySize": 1,
                "byteSize": 16,
                "bufferBacked": True,
                "inlineDataBytes": False,
                "compileConstants": False,
                "fields": [
                    {
                        "name": "captured_field_0",
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
                        "pointerTypeID": 0,
                        "bitFieldOffset": 0,
                        "bitFieldSize": 0,
                        "members": [],
                    }
                ],
            }
        ],
        "threadGroupSize": [],
        "outputTopology": "TriangleList",
        "dynamicLinkageInterfaceCount": 0,
    }


_BRIDGE_FIXTURE_SHADER_ABI_SHA256 = (
    "d4bde8934a2f1a24f7fbf3b47eec39f77c47de4a073122023945be1c55fc566d"
)
_BRIDGE_FIXTURE_BLOCK_ABI_SHA256 = (
    "acc2f7230b2f12253d03b8f57f609640b7ccfdd5dabbe208b73bec49538e90cc"
)
_BRIDGE_FIXTURE_BLOCK_LAYOUT_SHA256 = (
    "e5ad1118a64494179be5fa9f8d923049de1b4232f81f143a66f18bc9fbd8f31f"
)


class ProjectAliasRebindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        ReplayProjectService.create(
            self.project,
            "alias-rebind",
            game_id="example-game",
            build_id="build-1",
            platform="windows-x64",
            api_family="d3d11",
        )
        self.service = ReplayProjectService(self.project)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _catalog(
        self,
        label: str,
        blob: bytes,
        *,
        resource_id: int,
        bind_number: int = 0,
    ) -> tuple[Path, dict, str]:
        directory = self.root / label
        directory.mkdir()
        blob_path = directory / "shader.dxbc"
        blob_path.write_bytes(blob)
        fingerprint = fingerprint_bytes(blob)
        self.assertIsNotNone(fingerprint)
        shader = {
            "shaderResourceId": resource_id,
            "stage": "fragment",
            "entryPoint": "main",
            "rawPath": str(blob_path),
            "rawSize": len(blob),
            "rawSha256": hashlib.sha256(blob).hexdigest(),
            "codeSha256": fingerprint.code_sha256,
            **_interface(bind_number),
        }
        value = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": str(directory / (label + ".rdc")),
            "api": "D3D11",
            "shaders": [shader],
            "uses": [],
        }
        capture_id = capture_identity(value, [shader])
        value["captureId"] = capture_id
        path = directory / "catalog.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path, shader, capture_id

    def _catalog_many(
        self,
        label: str,
        blob: bytes,
        resource_ids: list[int],
    ) -> tuple[Path, list[dict], str]:
        directory = self.root / label
        directory.mkdir()
        blob_path = directory / "shader.dxbc"
        blob_path.write_bytes(blob)
        fingerprint = fingerprint_bytes(blob)
        self.assertIsNotNone(fingerprint)
        shaders = [
            {
                "shaderResourceId": resource_id,
                "stage": "fragment",
                "entryPoint": "main",
                "rawPath": str(blob_path),
                "rawSize": len(blob),
                "rawSha256": hashlib.sha256(blob).hexdigest(),
                "codeSha256": fingerprint.code_sha256,
                **_interface(),
            }
            for resource_id in resource_ids
        ]
        value = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": str(directory / (label + ".rdc")),
            "api": "D3D11",
            "shaders": shaders,
            "uses": [],
        }
        capture_id = capture_identity(value, shaders)
        value["captureId"] = capture_id
        path = directory / "catalog.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path, shaders, capture_id

    def _register_aliases(
        self,
        capture_id: str,
        shader: dict,
        aliases: list[dict],
    ) -> dict:
        path = self.root / ("aliases-%d.json" % len(list(self.root.glob("aliases-*.json"))))
        records = []
        for alias in aliases:
            records.append(
                {
                    "verified": True,
                    "captureId": capture_id,
                    "shaderResourceId": shader["shaderResourceId"],
                    "stage": shader["stage"],
                    "entryPoint": shader["entryPoint"],
                    "rawSha256": shader["rawSha256"],
                    "codeSha256": shader["codeSha256"],
                    "registerSpace": 0,
                    "bindNumber": 0,
                    "byteOffset": 0,
                    "canonicalPath": "",
                    **alias,
                }
            )
        path.write_text(
            json.dumps(
                {
                    "schema": ALIAS_SCHEMA,
                    "schemaVersion": ALIAS_VERSION,
                    "verifiedOnly": True,
                    "captureId": capture_id,
                    "aliases": records,
                }
            ),
            encoding="utf-8",
        )
        return self.service.register_alias_table(path)

    def _inventory(
        self,
        shader: dict,
        *,
        resource_id: int,
        base_type: str = "Float",
        duplicate_offset: bool = False,
        block_byte_size: int = 16,
        shader_abi_hash: str = "",
        block_index: int = 0,
        block_name: str = "PerMaterial",
        block_path: str = "PerMaterial",
        block_layout_sha256: str = "",
        block_abi_sha256: str = "",
    ) -> dict:
        shader_abi_hash = (
            shader_abi_hash
            or _BRIDGE_FIXTURE_SHADER_ABI_SHA256
        )
        block_layout_sha256 = (
            block_layout_sha256
            or _BRIDGE_FIXTURE_BLOCK_LAYOUT_SHA256
        )
        block_abi_sha256 = (
            block_abi_sha256
            or _BRIDGE_FIXTURE_BLOCK_ABI_SHA256
        )
        stable_identity = {
            "api": "d3d11",
            "encoding": "DXBC",
            "rawSha256": shader["rawSha256"],
            "stage": shader["stage"],
            "entryPoint": shader["entryPoint"],
            "abiHash": shader_abi_hash,
            "layoutHash": shader["layoutHash"],
        }
        # Inventory keeps the human-facing ``DXBC`` spelling, while the
        # seven-field executable identity hashes the protocol token ``dxbc``.
        stable_key = project_model._stable_shader_identity_sha256(
            stable_identity
        )
        fields = [
            {
                "canonicalPath": "captured_field_0",
                "name": "captured_field_0",
                "absoluteByteOffset": 0,
                "byteSize": 16,
                "baseType": base_type,
                "rows": 1,
                "columns": 4,
                "elements": 1,
                "arrayByteStride": 0,
                "matrixByteStride": 0,
                "rowMajor": False,
                "flags": 0,
                "pointerTypeID": 0,
                "bitFieldOffset": 0,
                "bitFieldSize": 0,
            }
        ]
        if duplicate_offset:
            fields.append({**fields[0], "canonicalPath": "Renamed.Other", "name": "other"})
        fields.sort(
            key=lambda item: (
                item["absoluteByteOffset"],
                item["canonicalPath"],
                item["name"],
                item["byteSize"],
                item["baseType"],
                item["rows"],
                item["columns"],
                item["elements"],
                item["arrayByteStride"],
                item["matrixByteStride"],
                item["rowMajor"],
                item["flags"],
                item["pointerTypeID"],
                item["bitFieldOffset"],
                item["bitFieldSize"],
            )
        )
        block = {
            "index": block_index,
            "path": block_path,
            "name": block_name,
            "space": 0,
            "bind": 0,
            "byteSize": block_byte_size,
            "layoutSha256": block_layout_sha256,
            "abiSha256": block_abi_sha256,
            "fields": fields,
        }
        target = {
            "resourceId": resource_id,
            **stable_identity,
            "interfaceHash": shader["interfaceHash"],
            "interfaceComplete": True,
            "stableKeySha256": stable_key,
            "anchorEventId": 37,
            "constantBlocks": [block],
        }
        capture_id = project_model._canonical_sha256([stable_key])
        inventory_sha = project_model._canonical_sha256(
            {
                "api": "d3d11",
                "shaders": [
                    {
                        "resourceId": resource_id,
                        "stableKeySha256": stable_key,
                        "anchorEventId": 37,
                        "constantBlocks": [block],
                    }
                ],
            }
        )
        return {
            "schema": "renderdoc-ultra-project-shader-inventory/v1",
            "schemaVersion": 1,
            "capture": {
                "captureId": capture_id,
                "catalogSha256": inventory_sha,
                "inventorySha256": inventory_sha,
                "api": "d3d11",
            },
            "shaders": [target],
        }

    def _materialize(self, plan: dict, candidate_override: dict | None = None) -> dict:
        match = plan["matches"][0]
        candidates = [
            candidate_override
            if candidate_override is not None
            else match["aliasCandidates"][0]
        ]
        return self.service.materialize_alias_candidates(
            plan["projectId"],
            plan["captureId"],
            plan["catalogSha256"],
            match["shader"],
            candidates,
        )

    def test_rebinds_code_match_to_target_resource_capture_and_raw_identity(self) -> None:
        source_path, source_shader, source_capture = self._catalog(
            "source-code", _dxbc(b"same-code", b"source-reflection"), resource_id=101
        )
        target_path, target_shader, target_capture = self._catalog(
            "target-code", _dxbc(b"same-code", b"target-reflection"), resource_id=909
        )
        self.service.import_capture(source_path)
        self._register_aliases(
            source_capture,
            source_shader,
            [
                {
                    "bindingKind": "constantBuffer",
                    "alias": "MaterialParameters",
                },
                {
                    "bindingKind": "constantBufferVariable",
                    "canonicalPath": "Material.BaseColor",
                    "alias": "BaseColor",
                }
            ],
        )

        plan = self.service.import_capture(target_path)
        self.assertEqual(
            plan["matches"][0]["aliasCandidates"][0]["matchMode"],
            "code-interface-layout-exact",
        )
        result = self._materialize(plan)
        table = json.loads(result["jsonBytes"].decode("utf-8"))
        aliases = {alias["bindingKind"]: alias for alias in table["aliases"]}
        block_alias = aliases["constantBuffer"]
        alias = aliases["constantField"]

        self.assertEqual(alias["captureId"], target_capture)
        self.assertEqual(alias["shaderResourceId"], 909)
        self.assertEqual(alias["rawSha256"], target_shader["rawSha256"])
        self.assertEqual(alias["codeSha256"], target_shader["codeSha256"])
        self.assertEqual(alias["bindingKind"], "constantField")
        self.assertEqual(alias["alias"], "BaseColor")
        self.assertEqual(block_alias["alias"], "MaterialParameters")
        self.assertEqual(block_alias["shaderResourceId"], 909)
        self.assertTrue(alias["verified"])
        self.assertTrue(alias["metadata"]["programVerified"])
        self.assertEqual(result["sha256"], hashlib.sha256(result["jsonBytes"]).hexdigest())
        self.assertNotEqual(alias["shaderResourceId"], source_shader["shaderResourceId"])

    def test_raw_exact_fallback_materializes_without_reusing_origin_resource_id(self) -> None:
        blob = _dxbc(b"identical-container", b"identical-reflection")
        source_path, source_shader, source_capture = self._catalog(
            "source-raw", blob, resource_id=11
        )
        target_path, target_shader, target_capture = self._catalog(
            "target-raw", blob, resource_id=77
        )
        self.service.import_capture(source_path)
        self._register_aliases(
            source_capture,
            source_shader,
            [{"bindingKind": "shader", "alias": "MainSurfacePS"}],
        )
        plan = self.service.import_capture(target_path)
        candidate = copy.deepcopy(plan["matches"][0]["aliasCandidates"][0])
        self.assertEqual(candidate["matchMode"], "raw-exact")
        # Compatibility with reuse plans written before groupIndex/bindingSha256.
        candidate.pop("groupIndex", None)
        candidate.pop("bindingSha256", None)
        result = self._materialize(plan, candidate)
        alias = result["aliasTable"]["aliases"][0]

        self.assertEqual(alias["captureId"], target_capture)
        self.assertEqual(alias["shaderResourceId"], 77)
        self.assertEqual(alias["rawSha256"], target_shader["rawSha256"])
        self.assertEqual(alias["alias"], "MainSurfacePS")

    def test_direct_inventory_materializes_field_alias_without_target_catalog(self) -> None:
        source_path, source_shader, source_capture = self._catalog(
            "source-direct-inventory", _dxbc(b"direct-inventory"), resource_id=111
        )
        self.service.import_capture(source_path)
        self._register_aliases(
            source_capture,
            source_shader,
            [
                {
                    "bindingKind": "constantBufferVariable",
                    "canonicalPath": "Material.BaseColor",
                    "alias": "BaseColor",
                }
            ],
        )
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        compact = manifest["captures"][0]["shaders"][0]
        inventory = self._inventory(compact, resource_id=9111)
        snapshot = self.service.reuse_snapshot_for_shader_inventory(inventory)
        match = snapshot["reusePlan"]["matches"][0]

        result = self.service.materialize_alias_batch_from_inventory(
            snapshot["projectId"],
            inventory,
            snapshot["binding"],
            [
                {
                    "shader": inventory["shaders"][0],
                    "candidates": match["aliasCandidates"],
                }
            ],
        )

        self.assertTrue(result["inventoryOnly"])
        self.assertEqual(result["captureId"], inventory["capture"]["captureId"])
        materialized = result["materializations"][0]
        alias = materialized["aliasTable"]["aliases"][0]
        self.assertEqual(alias["shaderResourceId"], 9111)
        self.assertNotEqual(alias["shaderResourceId"], 111)
        self.assertEqual(alias["originalName"], "captured_field_0")
        self.assertEqual(alias["canonicalPath"], "captured_field_0")
        self.assertEqual(materialized["aliasTable"]["captureCatalog"], "")
        self.assertEqual(alias["alias"], "BaseColor")
        self.assertTrue(alias["verified"])

    def test_direct_inventory_alias_rejects_type_and_offset_ambiguity(self) -> None:
        source_path, source_shader, source_capture = self._catalog(
            "source-direct-invalid", _dxbc(b"direct-invalid"), resource_id=112
        )
        self.service.import_capture(source_path)
        self._register_aliases(
            source_capture,
            source_shader,
            [
                {
                    "bindingKind": "constantField",
                    "canonicalPath": "Material.BaseColor",
                    "alias": "BaseColor",
                }
            ],
        )
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        compact = manifest["captures"][0]["shaders"][0]
        for inventory, message in (
            (self._inventory(compact, resource_id=9112, base_type="UInt"), "physical type differs"),
            (self._inventory(compact, resource_id=9112, duplicate_offset=True), "absent or ambiguous"),
            (self._inventory(compact, resource_id=9112, block_byte_size=32), "physical layout differs"),
        ):
            snapshot = self.service.reuse_snapshot_for_shader_inventory(inventory)
            match = snapshot["reusePlan"]["matches"][0]
            with self.assertRaisesRegex(ReplayProjectError, message):
                self.service.materialize_alias_batch_from_inventory(
                    snapshot["projectId"],
                    inventory,
                    snapshot["binding"],
                    [
                        {
                            "shader": inventory["shaders"][0],
                            "candidates": match["aliasCandidates"],
                        }
                    ],
                )

    def test_direct_inventory_alias_rejects_shader_and_block_identity_changes(
        self,
    ) -> None:
        source_path, source_shader, source_capture = self._catalog(
            "source-direct-identity", _dxbc(b"direct-identity"), resource_id=113
        )
        self.service.import_capture(source_path)
        self._register_aliases(
            source_capture,
            source_shader,
            [
                {
                    "bindingKind": "constantField",
                    "canonicalPath": "Material.BaseColor",
                    "alias": "BaseColor",
                }
            ],
        )
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        compact = manifest["captures"][0]["shaders"][0]
        cases = (
            (
                {"shader_abi_hash": "d" * 64},
                "Shader stable key or ABI differs",
            ),
            ({"block_index": 1}, "block index differs"),
            (
                {"block_name": "DifferentMaterial"},
                "block name or path differs",
            ),
            (
                {"block_layout_sha256": "e" * 64},
                "block layout differs",
            ),
            ({"block_abi_sha256": "f" * 64}, "block ABI differs"),
        )
        for kwargs, message in cases:
            with self.subTest(message=message):
                inventory = self._inventory(
                    compact, resource_id=9113, **kwargs
                )
                snapshot = self.service.reuse_snapshot_for_shader_inventory(
                    inventory
                )
                match = snapshot["reusePlan"]["matches"][0]
                with self.assertRaisesRegex(ReplayProjectError, message):
                    self.service.materialize_alias_batch_from_inventory(
                        snapshot["projectId"],
                        inventory,
                        snapshot["binding"],
                        [
                            {
                                "shader": inventory["shaders"][0],
                                "candidates": match["aliasCandidates"],
                            }
                        ],
                    )

    def test_raw_match_rejects_changed_cbuffer_layout_or_binding(self) -> None:
        blob = _dxbc(b"same-raw-layout-check")
        source_path, source_shader, source_capture = self._catalog(
            "source-layout", blob, resource_id=1, bind_number=0
        )
        target_path, unused_target_shader, unused_target_capture = self._catalog(
            "target-layout", blob, resource_id=2, bind_number=1
        )
        self.service.import_capture(source_path)
        self._register_aliases(
            source_capture,
            source_shader,
            [
                {
                    "bindingKind": "constantField",
                    "canonicalPath": "Material.BaseColor",
                    "alias": "BaseColor",
                }
            ],
        )
        plan = self.service.import_capture(target_path)
        self.assertEqual(
            plan["matches"][0]["aliasCandidates"][0]["matchMode"], "raw-exact"
        )

        with self.assertRaisesRegex(ReplayProjectError, "layout differs"):
            self._materialize(plan)

    def test_conflicting_canonical_paths_for_one_binding_fail_closed(self) -> None:
        blob = _dxbc(b"alias-conflict")
        source_path, source_shader, source_capture = self._catalog(
            "source-conflict", blob, resource_id=3
        )
        target_path, unused_target_shader, unused_target_capture = self._catalog(
            "target-conflict", blob, resource_id=4
        )
        self.service.import_capture(source_path)
        self._register_aliases(
            source_capture,
            source_shader,
            [
                {
                    "bindingKind": "constantField",
                    "canonicalPath": "Material.BaseColor",
                    "alias": "BaseColor",
                },
                {
                    "bindingKind": "constantField",
                    "canonicalPath": "Material.Tint",
                    "alias": "BaseColor",
                },
            ],
        )
        plan = self.service.import_capture(target_path)

        with self.assertRaisesRegex(ReplayProjectError, "paths are ambiguous"):
            self._materialize(plan)

    def test_current_alias_head_supersedes_historical_candidate(self) -> None:
        blob = _dxbc(b"cross-record-conflict")
        source_path, source_shader, source_capture = self._catalog(
            "source-cross-record", blob, resource_id=31
        )
        target_path, unused_target_shader, unused_target_capture = self._catalog(
            "target-cross-record", blob, resource_id=32
        )
        self.service.import_capture(source_path)
        first = self._register_aliases(
            source_capture,
            source_shader,
            [{"bindingKind": "shader", "alias": "SurfacePS"}],
        )
        first_plan = self.service.import_capture(target_path)
        first_candidate = copy.deepcopy(
            first_plan["matches"][0]["aliasCandidates"][0]
        )
        self.assertEqual(
            first_candidate["knowledgeId"], first["knowledge"]["knowledgeId"]
        )

        current = self._register_aliases(
            source_capture,
            source_shader,
            [{"bindingKind": "shader", "alias": "ConflictingSurfacePS"}],
        )
        plan = self.service.import_capture(target_path)
        match = plan["matches"][0]
        self.assertEqual(len(match["aliasCandidates"]), 1)
        self.assertEqual(
            match["aliasCandidates"][0]["knowledgeId"],
            current["knowledge"]["knowledgeId"],
        )

        with self.assertRaisesRegex(ReplayProjectError, "stale, incomplete"):
            self.service.materialize_alias_candidates(
                plan["projectId"],
                plan["captureId"],
                plan["catalogSha256"],
                match["shader"],
                [first_candidate],
            )
        materialized = self.service.materialize_alias_candidates(
            plan["projectId"],
            plan["captureId"],
            plan["catalogSha256"],
            match["shader"],
            match["aliasCandidates"],
        )
        self.assertEqual(
            materialized["aliasTable"]["aliases"][0]["alias"],
            "ConflictingSurfacePS",
        )

    def test_candidate_tampering_and_artifact_tampering_fail_closed(self) -> None:
        blob = _dxbc(b"tamper")
        source_path, source_shader, source_capture = self._catalog(
            "source-tamper", blob, resource_id=5
        )
        target_path, unused_target_shader, unused_target_capture = self._catalog(
            "target-tamper", blob, resource_id=6
        )
        self.service.import_capture(source_path)
        registered = self._register_aliases(
            source_capture,
            source_shader,
            [{"bindingKind": "shader", "alias": "TamperProofPS"}],
        )
        plan = self.service.import_capture(target_path)
        candidate = copy.deepcopy(plan["matches"][0]["aliasCandidates"][0])
        binding_tamper = copy.deepcopy(candidate)
        binding_tamper["bindingSha256"] = "0" * 64
        with self.assertRaisesRegex(ReplayProjectError, "binding was tampered"):
            self._materialize(plan, binding_tamper)

        candidate["aliasCount"] += 1
        with self.assertRaisesRegex(
            ReplayProjectError, "candidate group is absent or ambiguous|failed exact project matching"
        ):
            self._materialize(plan, candidate)

        artifact = self.project / registered["knowledge"]["artifactPath"]
        artifact.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ReplayProjectError, "artifact hash mismatch"):
            self._materialize(plan)

    def test_verified_flag_cannot_launder_ai_provisional_alias(self) -> None:
        blob = _dxbc(b"provisional")
        source_path, source_shader, source_capture = self._catalog(
            "source-provisional", blob, resource_id=7
        )
        target_path, unused_target_shader, unused_target_capture = self._catalog(
            "target-provisional", blob, resource_id=8
        )
        self.service.import_capture(source_path)
        self._register_aliases(
            source_capture,
            source_shader,
            [
                {
                    "bindingKind": "shader",
                    "alias": "MaybePS",
                    "status": "AIProvisional",
                }
            ],
        )
        plan = self.service.import_capture(target_path)

        with self.assertRaisesRegex(ReplayProjectError, "AIProvisional"):
            self._materialize(plan)

    def test_batch_snapshots_and_parses_shared_artifact_once(self) -> None:
        blob = _dxbc(b"batch-shared-artifact")
        source_path, source_shader, source_capture = self._catalog(
            "source-batch", blob, resource_id=51
        )
        target_path, target_shaders, unused_target_capture = self._catalog_many(
            "target-batch", blob, [61, 62]
        )
        self.service.import_capture(source_path)
        registered = self._register_aliases(
            source_capture,
            source_shader,
            [{"bindingKind": "shader", "alias": "SharedSurfacePS"}],
        )
        plan = self.service.import_capture(target_path)
        self.assertEqual(len(plan["matches"]), 2)
        artifact = (self.project / registered["knowledge"]["artifactPath"]).resolve()
        original_file_bytes = project_model._file_bytes
        original_loads = project_alias_rebind.json.loads
        original_catalog = project_model._normalised_catalog_evidence
        counters = {"artifactReads": 0, "artifactParses": 0, "catalogLoads": 0}

        def counted_file_bytes(path, maximum, label):
            if Path(path).resolve() == artifact:
                counters["artifactReads"] += 1
            return original_file_bytes(path, maximum, label)

        def counted_loads(value, *args, **kwargs):
            if isinstance(value, (bytes, bytearray)) and b'"verifiedOnly"' in value:
                counters["artifactParses"] += 1
            elif isinstance(value, str) and '"verifiedOnly"' in value:
                counters["artifactParses"] += 1
            return original_loads(value, *args, **kwargs)

        def counted_catalog(*args, **kwargs):
            counters["catalogLoads"] += 1
            return original_catalog(*args, **kwargs)

        targets = [
            {
                "shader": match["shader"],
                "candidates": match["aliasCandidates"],
            }
            for match in plan["matches"]
        ]
        with mock.patch.object(project_model, "_file_bytes", counted_file_bytes), mock.patch.object(
            project_alias_rebind.json, "loads", counted_loads
        ), mock.patch.object(
            project_model, "_normalised_catalog_evidence", counted_catalog
        ):
            result = self.service.materialize_alias_batch(
                plan["projectId"],
                plan["captureId"],
                plan["catalogSha256"],
                targets,
            )

        self.assertEqual(result["targetCount"], 2)
        self.assertEqual(result["candidateCount"], 2)
        self.assertEqual(result["aliasCount"], 2)
        self.assertEqual(counters["artifactReads"], 1)
        self.assertEqual(counters["artifactParses"], 1)
        self.assertEqual(counters["catalogLoads"], 2)
        self.assertEqual(
            [item["shader"]["resourceId"] for item in result["materializations"]],
            [shader["shaderResourceId"] for shader in target_shaders],
        )
        self.assertEqual(result["budgets"]["maxTargets"], 256)
        self.assertEqual(result["budgets"]["maxCandidates"], 4096)

    def test_batch_bounds_targets_candidates_artifacts_and_project_snapshot(self) -> None:
        blob = _dxbc(b"batch-bounds")
        source_path, source_shader, source_capture = self._catalog(
            "source-bounds", blob, resource_id=71
        )
        target_path, unused_target_shader, unused_target_capture = self._catalog(
            "target-bounds", blob, resource_id=72
        )
        self.service.import_capture(source_path)
        self._register_aliases(
            source_capture,
            source_shader,
            [{"bindingKind": "shader", "alias": "BoundedPS"}],
        )
        plan = self.service.import_capture(target_path)
        match = plan["matches"][0]
        target = {
            "shader": match["shader"],
            "candidates": match["aliasCandidates"],
        }
        with self.assertRaisesRegex(ReplayProjectError, "1..256"):
            self.service.materialize_alias_batch(
                plan["projectId"],
                plan["captureId"],
                plan["catalogSha256"],
                [target] * 257,
            )
        too_many_candidates = copy.deepcopy(target)
        too_many_candidates["candidates"] = (
            too_many_candidates["candidates"] * 4097
        )
        with self.assertRaisesRegex(ReplayProjectError, "4096 candidate"):
            self.service.materialize_alias_batch(
                plan["projectId"],
                plan["captureId"],
                plan["catalogSha256"],
                [too_many_candidates],
            )
        with mock.patch.object(
            project_alias_rebind, "MAX_BATCH_ALIAS_ARTIFACT_BYTES", 1
        ), self.assertRaisesRegex(ReplayProjectError, "byte limit"):
            self.service.materialize_alias_batch(
                plan["projectId"],
                plan["captureId"],
                plan["catalogSha256"],
                [target],
            )
        with mock.patch.object(
            project_alias_rebind, "MAX_BATCH_CATALOG_BYTES", 1
        ), self.assertRaisesRegex(ReplayProjectError, "catalog byte limit"):
            self.service.materialize_alias_batch(
                plan["projectId"],
                plan["captureId"],
                plan["catalogSha256"],
                [target],
            )

        original_load = self.service._load

        def mutate_after_load(**kwargs):
            manifest = original_load(**kwargs)
            self.service.manifest_path.write_bytes(
                self.service.manifest_path.read_bytes() + b"\n"
            )
            return manifest

        with mock.patch.object(
            self.service, "_load", side_effect=mutate_after_load
        ), self.assertRaisesRegex(ReplayProjectError, "project changed"):
            self.service.materialize_alias_batch(
                plan["projectId"],
                plan["captureId"],
                plan["catalogSha256"],
                [target],
            )


if __name__ == "__main__":
    unittest.main()
