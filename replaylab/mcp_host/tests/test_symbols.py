from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from src.symbols.dxbc import fingerprint_bytes, scan_file
from src.symbols.matcher import _normalise_catalog, aliases_for_match, capture_identity
from src.symbols.scanner import layout_hash
from src.symbols.service import SymbolService


def make_dxbc(code: bytes = b"compiled instructions", metadata: bytes = b"") -> bytes:
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


def shader_layout(name: str = "PerMaterial", field: str = "BaseColor") -> dict:
    return {
        "constantBuffers": [
            {
                "name": name,
                "space": 0,
                "bind": 0,
                "arraySize": 1,
                "byteSize": 16,
                "bufferBacked": True,
                "inlineDataBytes": False,
                "compileConstants": False,
                "fields": [
                    {
                        "name": field,
                        "byteOffset": 0,
                        "byteSize": 16,
                        "type": "float",
                        "rows": 1,
                        "columns": 4,
                        "bitFieldOffset": 0,
                        "bitFieldSize": 0,
                    }
                ],
            }
        ]
    }


class DxbcTests(unittest.TestCase):
    def test_fingerprint_and_embedded_scan(self) -> None:
        blob = make_dxbc()
        fingerprint = fingerprint_bytes(blob)
        self.assertIsNotNone(fingerprint)
        self.assertEqual(fingerprint.raw_sha256, hashlib.sha256(blob).hexdigest())
        self.assertEqual(fingerprint.kind, "DXBC")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "archive.bin"
            path.write_bytes(b"prefix-data" + blob + b"suffix-data")
            found = scan_file(path)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].file_offset, len(b"prefix-data"))
            self.assertEqual(found[0].raw_sha256, fingerprint.raw_sha256)

    def test_rejects_malformed_container(self) -> None:
        self.assertIsNone(fingerprint_bytes(b"DXBC" + b"\x00" * 40))

    def test_layout_hash_normalises_tool_type_spellings_and_ignores_names(self) -> None:
        asset = shader_layout("RealPerMaterial", "RealTint")
        asset["constantBuffers"][0]["fields"][0]["type"] = "float4"
        captured = shader_layout("cb0", "_child0")
        field = captured["constantBuffers"][0]["fields"][0]
        field["type"] = "VarType.Float4"
        field["baseType"] = "VarType.Float"
        self.assertEqual(layout_hash(asset), layout_hash(captured))


class SymbolWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.assets = self.base / "assets"
        self.assets.mkdir()
        self.capture_dir = self.base / "capture"
        self.capture_dir.mkdir()
        self.database = self.base / "symbols.sqlite3"
        self.service = SymbolService(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_manifest(
        self,
        blob: bytes,
        metadata: dict,
        name: str = "HeroSkin",
        blob_path: str = "",
    ) -> None:
        fingerprint = fingerprint_bytes(blob)
        shader = {
            "name": name,
            "path": "Game/Shaders/%s.shader" % name,
            "stage": "pixel",
            "entryPoint": "main",
            "rawSha256": fingerprint.raw_sha256,
            "codeSha256": fingerprint.code_sha256,
            **metadata,
        }
        if blob_path:
            shader["blobPath"] = blob_path
        (self.assets / "symbols.json").write_text(
            json.dumps({"schema": "renderdoc-ultra-symbols/v1", "shaders": [shader]}),
            encoding="utf-8",
        )

    def _write_catalog(self, blob: bytes, metadata: dict, label: str) -> tuple[Path, str]:
        raw_path = self.capture_dir / (label + ".bin")
        raw_path.write_bytes(blob)
        shader = {
            "shaderResourceId": 123,
            "stage": "fragment",
            "entryPoint": "main",
            "rawPath": str(raw_path),
            "rawSize": len(blob),
            "rawSha256": hashlib.sha256(blob).hexdigest(),
            **metadata,
        }
        value = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": str(self.capture_dir / "frame.rdc"),
            "api": "D3D11",
            "shaders": [shader],
            "uses": [],
        }
        canonical_id = capture_identity(value, [shader])
        value["captureId"] = canonical_id
        path = self.capture_dir / (label + ".json")
        path.write_text(
            json.dumps(value),
            encoding="utf-8",
        )
        return path, canonical_id

    def test_unique_exact_bytes_export_only_physically_bound_aliases(self) -> None:
        blob = make_dxbc()
        metadata = shader_layout()
        self._write_manifest(blob, metadata, blob_path="packed_shader_cache.bin")
        (self.assets / "packed_shader_cache.bin").write_bytes(blob)
        catalog, capture_id = self._write_catalog(blob, metadata, "capture-exact")

        scan = self.service.scan(self.assets, build_id="build-1")
        report = self.service.match(catalog, scan["scanId"])

        self.assertEqual(report["statusCounts"], {"ExactBlobUnique": 1})
        output = self.base / "aliases.json"
        exported = self.service.export_alias_table(
            capture_id, output, verified_only=True, scan_id=scan["scanId"]
        )
        self.assertEqual(exported["aliasCount"], 1)
        aliases = json.loads(output.read_text(encoding="utf-8"))["aliases"]
        self.assertTrue(all(alias["verified"] for alias in aliases))
        self.assertTrue(all(alias["captureId"] == capture_id for alias in aliases))
        self.assertTrue(all(alias["entryPoint"] == "main" for alias in aliases))
        self.assertTrue(all(alias["rawSha256"] for alias in aliases))
        self.assertNotIn("BaseColor", {alias["alias"] for alias in aliases})
        self.assertEqual({alias["alias"] for alias in aliases}, {"packed_shader_cache"})

        review = self.service.unresolved(capture_id)
        self.assertEqual(review["count"], 1)
        self.assertTrue(review["cases"][0]["identityProgramVerified"])
        self.assertTrue(review["cases"][0]["needsSymbolMetadataReview"])
        manifest_candidate = next(
            candidate
            for candidate in review["cases"][0]["candidates"]
            if candidate["assetMetadata"].get("_hashVerification")
            == "scanner-bound-container"
        )
        evidence = self.service.evidence(
            capture_id,
            123,
            "fragment",
            "main",
            manifest_candidate["candidateId"],
        )
        proposal = self.service.submit_decision(
            capture_id,
            scan["scanId"],
            123,
            "fragment",
            "main",
            manifest_candidate["candidateId"],
            0.95,
            "The bound manifest proposes semantic names for review.",
            evidence["candidates"][0]["evidenceId"],
        )
        self.assertEqual(proposal["status"], "AIProvisional")
        self.assertFalse(proposal["committed"])

    def test_layout_only_ai_choice_is_always_provisional(self) -> None:
        asset_blob = make_dxbc(b"asset code")
        capture_blob = make_dxbc(b"different capture code")
        metadata = shader_layout("CameraOrMaterial", "Tint")
        self._write_manifest(asset_blob, metadata, name="PossibleMaterial")
        catalog, capture_id = self._write_catalog(capture_blob, metadata, "capture-ai")

        scan = self.service.scan(self.assets, build_id="build-2")
        report = self.service.match(catalog, scan["scanId"])
        self.assertEqual(report["statusCounts"], {"NeedsAI": 1})
        cases = self.service.unresolved(capture_id)
        candidate = cases["cases"][0]["candidates"][0]
        evidence = self.service.evidence(
            capture_id, 123, "fragment", "main", candidate["candidateId"]
        )
        self.assertEqual(len(evidence["candidates"]), 1)
        file_context = evidence["candidates"][0]["asset"]["fileContext"]
        self.assertTrue(file_context["available"])
        self.assertTrue(
            any("PossibleMaterial" in value for value in file_context["asciiStrings"])
        )
        evidence_id = evidence["candidates"][0]["evidenceId"]

        provisional = self.service.submit_decision(
            capture_id,
            scan["scanId"],
            123,
            "fragment",
            "main",
            candidate["candidateId"],
            0.9,
            "Layout and material graph are consistent.",
            evidence_id,
        )
        self.assertEqual(provisional["status"], "AIProvisional")
        self.assertFalse(provisional["committed"])
        self.assertTrue(provisional["requiresTrustedUIApproval"])
        self.assertEqual(len(self.service.store.list_symbol_rules(scan["scanId"])), 0)

        rescanned = self.service.scan(self.assets, build_id="build-2")
        self.assertEqual(rescanned["scanId"], scan["scanId"])
        self.assertEqual(len(self.service.store.list_symbol_rules(scan["scanId"])), 0)

        next_catalog, next_capture_id = self._write_catalog(
            capture_blob, metadata, "capture-next-frame"
        )
        reused = self.service.match(next_catalog, scan["scanId"])
        self.assertEqual(reused["statusCounts"], {"NeedsAI": 1})
        self.assertIsNone(reused["resolutions"][0]["selectedAssetShaderId"])
        self.assertEqual(self.service.unresolved(next_capture_id)["count"], 1)

    def test_ai_cannot_auto_commit_an_exact_but_ambiguous_asset_owner(self) -> None:
        blob = make_dxbc(b"shared variant")
        fingerprint = fingerprint_bytes(blob)
        metadata = shader_layout()
        shaders = []
        for name in ("Material_A", "Material_B"):
            shaders.append(
                {
                    "name": name,
                    "path": "Game/Shaders/%s.shader" % name,
                    "stage": "pixel",
                    "hashVerified": True,
                    "rawSha256": fingerprint.raw_sha256,
                    "codeSha256": fingerprint.code_sha256,
                    **metadata,
                }
            )
        (self.assets / "symbols.json").write_text(
            json.dumps({"schema": "renderdoc-ultra-symbols/v1", "shaders": shaders}),
            encoding="utf-8",
        )
        catalog, capture_id = self._write_catalog(
            blob, metadata, "capture-ambiguous"
        )
        scan = self.service.scan(self.assets, build_id="build-ambiguous")
        report = self.service.match(catalog, scan["scanId"])
        self.assertEqual(report["statusCounts"], {"NeedsAI": 1})

        evidence = self.service.evidence(
            capture_id, 123, "fragment", "main", top_k=1
        )
        candidate = evidence["candidates"][0]
        result = self.service.submit_decision(
            capture_id,
            scan["scanId"],
            123,
            "fragment",
            "main",
            candidate["match"]["candidateId"],
            0.99,
            "The material dependency graph suggests this candidate.",
            candidate["evidenceId"],
        )
        self.assertEqual(result["status"], "AIProvisional")
        self.assertFalse(result["committed"])

    def test_executable_and_cb_layout_without_complete_interface_is_not_auto(self) -> None:
        asset_blob = make_dxbc(b"same executable", b"asset reflection")
        capture_blob = make_dxbc(b"same executable", b"capture reflection")
        fingerprint = fingerprint_bytes(asset_blob)
        metadata = shader_layout()
        shader = {
            "name": "IncompleteInterface",
            "path": "Game/Shaders/Incomplete.shader",
            "stage": "pixel",
            "rawSha256": fingerprint.raw_sha256,
            "codeSha256": fingerprint.code_sha256,
            "blobPath": "observed-interface.bin",
            **metadata,
        }
        (self.assets / "symbols.json").write_text(
            json.dumps({"schema": "renderdoc-ultra-symbols/v1", "shaders": [shader]}),
            encoding="utf-8",
        )
        (self.assets / "observed-interface.bin").write_bytes(asset_blob)
        catalog, unused_capture_id = self._write_catalog(
            capture_blob, metadata, "capture-interface-gate"
        )
        scan = self.service.scan(self.assets, build_id="build-interface")
        report = self.service.match(catalog, scan["scanId"])
        self.assertEqual(report["statusCounts"], {"NeedsAI": 1})
        validation = report["resolutions"][0]["candidates"][0]["validation"]
        self.assertEqual(validation["status"], "ExecutableAndLayoutIncompleteInterface")
        self.assertFalse(validation["programVerified"])

    def test_executable_layout_and_complete_interface_can_match_stripped_container(self) -> None:
        asset_blob = make_dxbc(b"same executable", b"asset reflection")
        capture_blob = make_dxbc(b"same executable", b"stripped reflection")
        asset_fingerprint = fingerprint_bytes(asset_blob)
        metadata = shader_layout()
        metadata.update(
            {
                "interfaceComplete": True,
                "inputSignature": [
                    {
                        "semanticName": "TEXCOORD",
                        "semanticIndex": 0,
                        "regIndex": 0,
                        "systemValue": "Undefined",
                        "compType": "float",
                        "compCount": 2,
                        "stream": 0,
                        "regChannelMask": 3,
                        "channelUsedMask": 3,
                    }
                ],
                "outputSignature": [],
                "readOnlyResources": [],
                "readWriteResources": [],
                "samplers": [],
                "threadGroupSize": [],
            }
        )
        shader = {
            "name": "StrippedButEquivalent",
            "path": "Game/Shaders/Equivalent.shader",
            "stage": "pixel",
            "hashVerified": True,
            "rawSha256": asset_fingerprint.raw_sha256,
            "codeSha256": asset_fingerprint.code_sha256,
            "blobPath": "equivalent-asset.bin",
            **metadata,
        }
        (self.assets / "symbols.json").write_text(
            json.dumps({"schema": "renderdoc-ultra-symbols/v1", "shaders": [shader]}),
            encoding="utf-8",
        )
        (self.assets / "equivalent-asset.bin").write_bytes(asset_blob)
        catalog, unused_capture_id = self._write_catalog(
            capture_blob, metadata, "capture-equivalent"
        )
        scan = self.service.scan(self.assets, build_id="build-equivalent")
        report = self.service.match(catalog, scan["scanId"])
        self.assertEqual(report["statusCounts"], {"NeedsAI": 1})
        validation = report["resolutions"][0]["candidates"][0]["validation"]
        self.assertFalse(validation["programVerified"])

    def test_manifest_hash_verified_flag_is_not_trusted_without_bytes(self) -> None:
        blob = make_dxbc(b"declared only")
        metadata = shader_layout()
        fingerprint = fingerprint_bytes(blob)
        shader = {
            "name": "UntrustedDeclaration",
            "path": "Game/Shaders/Untrusted.shader",
            "stage": "pixel",
            "entryPoint": "main",
            "hashVerified": True,
            "rawSha256": fingerprint.raw_sha256,
            "codeSha256": fingerprint.code_sha256,
            **metadata,
        }
        (self.assets / "symbols.json").write_text(
            json.dumps({"schema": "renderdoc-ultra-symbols/v1", "shaders": [shader]}),
            encoding="utf-8",
        )
        catalog, unused_capture_id = self._write_catalog(
            blob, metadata, "capture-untrusted-declaration"
        )
        scan = self.service.scan(self.assets, build_id="build-untrusted")
        report = self.service.match(catalog, scan["scanId"])
        self.assertEqual(report["statusCounts"], {"NeedsAI": 1})
        validation = report["resolutions"][0]["candidates"][0]["validation"]
        self.assertEqual(validation["status"], "DeclaredExactBlobUnverified")
        self.assertFalse(validation["evidence"]["assetRawHashVerified"])

    def test_exact_blob_cannot_smuggle_wrong_constant_field_abi(self) -> None:
        blob = make_dxbc(b"same bytecode")
        capture_metadata = shader_layout("cb0", "_child0")
        malicious_metadata = shader_layout("FakeMaterial", "FakeSecret")
        malicious_field = malicious_metadata["constantBuffers"][0]["fields"][0]
        malicious_field["byteOffset"] = 4
        malicious_field["columns"] = 3
        self._write_manifest(
            blob,
            malicious_metadata,
            name="MaliciousNames",
            blob_path="observed-malicious.bin",
        )
        (self.assets / "observed-malicious.bin").write_bytes(blob)
        catalog, capture_id = self._write_catalog(
            blob, capture_metadata, "capture-malicious-fields"
        )
        scan = self.service.scan(self.assets, build_id="build-malicious")
        report = self.service.match(catalog, scan["scanId"])
        self.assertEqual(report["statusCounts"], {"ExactBlobUnique": 1})

        capture = report["resolutions"][0]["candidates"][0]["validation"]["evidence"]
        capture_shader = self.service.store.get_capture_shader(
            capture_id, 123, "fragment", "main"
        )["metadata"]
        malicious_asset = {
            "id": 999,
            "relative_path": "Game/Shaders/Malicious.shader",
            "display_name": "MaliciousNames",
            "metadata": {
                **malicious_metadata,
                "_symbolMetadataTrusted": True,
            },
        }
        aliases = aliases_for_match(
            capture_id,
            capture_shader,
            malicious_asset,
            "program",
            1.0,
            True,
            {
                "status": "ExactBlob",
                "programVerified": True,
                "evidence": {
                    **capture,
                    "layoutHashEqual": True,
                    "captureHasLayout": True,
                    "assetHasLayout": True,
                },
            },
        )
        self.assertEqual([alias["bindingKind"] for alias in aliases], ["shader"])

    def test_catalog_rejects_escape_and_hash_tampering(self) -> None:
        blob = make_dxbc(b"catalog validation")
        metadata = shader_layout()
        catalog, unused_capture_id = self._write_catalog(
            blob, metadata, "capture-strict"
        )
        scan = self.service.scan(self.assets, build_id="build-strict")

        value = json.loads(catalog.read_text(encoding="utf-8"))
        value["shaders"][0]["rawSha256"] = "0" * 64
        catalog.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "rawSha256"):
            self.service.match(catalog, scan["scanId"])

        outside = self.base / "outside.bin"
        outside.write_bytes(blob)
        value["shaders"][0]["rawSha256"] = hashlib.sha256(blob).hexdigest()
        value["shaders"][0]["rawPath"] = str(outside)
        catalog.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.service.match(catalog, scan["scanId"])

    def test_same_resource_stage_with_two_entry_points_stays_separate(self) -> None:
        blob = make_dxbc(b"multi entry")
        (self.assets / "multi-entry.bin").write_bytes(blob)
        blob_path = self.capture_dir / "multi-entry.bin"
        blob_path.write_bytes(blob)
        raw_hash = hashlib.sha256(blob).hexdigest()
        shaders = [
            {
                "shaderResourceId": 321,
                "stage": "fragment",
                "entryPoint": entry,
                "rawPath": str(blob_path),
                "rawSize": len(blob),
                "rawSha256": raw_hash,
                **shader_layout(),
            }
            for entry in ("mainA", "mainB")
        ]
        value = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": str(self.capture_dir / "frame.rdc"),
            "api": "D3D11",
            "shaders": shaders,
            "uses": [],
        }
        value["captureId"] = capture_identity(value, shaders)
        catalog = self.capture_dir / "multi-entry.json"
        catalog.write_text(json.dumps(value), encoding="utf-8")
        scan = self.service.scan(self.assets, build_id="build-multi-entry")
        report = self.service.match(catalog, scan["scanId"])
        self.assertEqual(report["shaderCount"], 2)
        self.assertEqual(
            {resolution["entryPoint"] for resolution in report["resolutions"]},
            {"mainA", "mainB"},
        )
        rows = self.service.store.list_matches(value["captureId"])
        self.assertEqual({row["entry_point"] for row in rows}, {"mainA", "mainB"})

    def test_catalog_use_index_preserves_exact_and_wildcard_matching(self) -> None:
        shaders = [
            {
                "shaderResourceId": 101,
                "stage": "vertex",
                "entryPoint": "mainA",
            },
            {
                "shaderResourceId": 101,
                "stage": "vertex",
                "entryPoint": "mainB",
            },
            {
                "shaderResourceId": 202,
                "stage": "fragment",
                "entryPoint": "main",
            },
        ]
        uses = [
            {
                "shaderResourceId": 101,
                "stage": "vertex",
                "entryPoint": "mainA",
                "eventId": 1,
            },
            {
                "shaderResourceId": 101,
                "stage": "vertex",
                "entryPoint": "mainB",
                "eventId": 2,
            },
            {
                "shaderResourceId": 101,
                "stage": "vertex",
                "entryPoint": "",
                "eventId": 3,
            },
            {
                "shaderResourceId": 101,
                "stage": "fragment",
                "entryPoint": "mainA",
                "eventId": 4,
            },
            {
                "shaderResourceId": 202,
                "stage": "fragment",
                "entryPoint": "main",
                "eventId": 5,
            },
            {
                "shaderResourceId": 303,
                "stage": "fragment",
                "entryPoint": "main",
                "eventId": 6,
            },
        ]
        catalog_path = self.capture_dir / "indexed-uses.json"
        catalog_path.write_text(
            json.dumps(
                {
                    "schema": "renderdoc-ultra-capture-symbols/v1",
                    "schemaVersion": 1,
                    "capturePath": str(self.capture_dir / "frame.rdc"),
                    "api": "D3D12",
                    "shaders": shaders,
                    "uses": uses,
                }
            ),
            encoding="utf-8",
        )

        _, normalised = _normalise_catalog(catalog_path)
        uses_by_entry = {
            shader["entryPoint"]: [use["eventId"] for use in shader["uses"]]
            for shader in normalised["shaders"]
        }
        self.assertEqual(uses_by_entry["mainA"], [1, 3])
        self.assertEqual(uses_by_entry["mainB"], [2, 3])
        self.assertEqual(uses_by_entry["main"], [5])


if __name__ == "__main__":
    unittest.main()
