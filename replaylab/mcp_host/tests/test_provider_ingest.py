from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.symbols.dxbc import fingerprint_bytes
from src.symbols.providers import (
    ProviderIntegrityError,
    TrustedProviderResult,
    run_provider,
)
from src.symbols.scanner import (
    _exclusive_scan_lock,
    ingest_trusted_provider_result,
)
from src.symbols.service import SymbolService
from src.symbols.store import SymbolStore


PROVIDER_SCRIPT = r'''from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import struct


def make_dxbc():
    code = b"trusted provider instructions"
    body = code + b"\x00" * (-len(code) % 4)
    program = struct.pack("<2I", 0x00000050, 2 + len(body) // 4) + body
    chunk = b"SHDR" + struct.pack("<I", len(program)) + program
    offset = 36
    total_size = offset + len(chunk)
    blob = (
        b"DXBC"
        + b"\x00" * 16
        + struct.pack("<I", 1)
        + struct.pack("<I", total_size)
        + struct.pack("<I", 1)
        + struct.pack("<I", offset)
        + chunk
    )
    return blob, hashlib.sha256(chunk).hexdigest()


parser = argparse.ArgumentParser()
parser.add_argument("--manifest", required=True)
parser.add_argument("--build", required=True)
parser.add_argument("--provider-id", required=True)
parser.add_argument("--mode", required=True)
args = parser.parse_args()

blob, code_hash = make_dxbc()
shader = {
    "path": "Game/Shaders/Hero.shader",
    "name": "Hero/Forward",
    "stage": "pixel",
    "entryPoint": "main",
    "rawSha256": hashlib.sha256(blob).hexdigest(),
    "codeSha256": code_hash,
    "interfaceComplete": True,
    "inputSignature": [],
    "outputSignature": [],
    "readOnlyResources": [],
    "readWriteResources": [],
    "samplers": [],
    "threadGroupSize": [],
    "outputTopology": "Unknown",
    "dynamicLinkageInterfaceCount": 0,
    "constantBuffers": [
        {
            "name": "PerMaterial",
            "space": 0,
            "bind": 2,
            "arraySize": 1,
            "byteSize": 16,
            "bufferBacked": True,
            "inlineDataBytes": False,
            "compileConstants": False,
            "fields": [
                {
                    "name": "Tint",
                    "byteOffset": 0,
                    "byteSize": 16,
                    "type": "float",
                    "rows": 1,
                    "columns": 4,
                    "elements": 1,
                    "arrayByteStride": 0,
                    "matrixByteStride": 0,
                    "rowMajor": False,
                    "flags": 0,
                    "pointerTypeID": 4294967295,
                    "bitFieldOffset": 0,
                    "bitFieldSize": 0
                }
            ]
        }
    ]
}
if args.mode == "blob":
    (Path(args.manifest).parent / "hero.dxbc").write_bytes(blob)
    shader["blobPath"] = "hero.dxbc"

manifest = {
    "schema": "renderdoc-ultra-symbols/v1",
    "schemaVersion": 1,
    "buildId": args.build,
    "provider": {"providerId": args.provider_id, "version": "1.0.0"},
    "shaders": [shader],
}
Path(args.manifest).write_text(json.dumps(manifest), encoding="utf-8")
'''


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_dxbc() -> bytes:
    code = b"trusted provider instructions"
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


def complete_shader_interface() -> dict:
    return {
        "name": "captured-shader",
        "interfaceComplete": True,
        "inputSignature": [],
        "outputSignature": [],
        "readOnlyResources": [],
        "readWriteResources": [],
        "samplers": [],
        "threadGroupSize": [],
        "outputTopology": "Unknown",
        "dynamicLinkageInterfaceCount": 0,
        "constantBuffers": [
            {
                "name": "cb2",
                "space": 0,
                "bind": 2,
                "arraySize": 1,
                "byteSize": 16,
                "bufferBacked": True,
                "inlineDataBytes": False,
                "compileConstants": False,
                "fields": [
                    {
                        "name": "_child0",
                        "byteOffset": 0,
                        "byteSize": 16,
                        "type": "float",
                        "rows": 1,
                        "columns": 4,
                        "elements": 1,
                        "arrayByteStride": 0,
                        "matrixByteStride": 0,
                        "rowMajor": False,
                        "flags": 0,
                        "pointerTypeID": 4294967295,
                        "bitFieldOffset": 0,
                        "bitFieldSize": 0,
                    }
                ],
            }
        ],
    }


class TrustedProviderIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.assets = self.base / "assets"
        self.config = self.base / "config"
        self.tools = self.base / "tools"
        self.capture = self.base / "capture"
        for directory in (self.assets, self.config, self.tools, self.capture):
            directory.mkdir()
        self.database = self.base / "symbols.sqlite3"
        self.script = self.tools / "provider.py"
        self.script.write_text(PROVIDER_SCRIPT, encoding="utf-8")
        self.registry = self.config / "trusted_providers.v1.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_registry(self, provider_id: str, mode: str) -> None:
        entry = {
            "providerId": provider_id,
            "version": "1.0.0",
            "executable": sys.executable,
            "sha256": file_hash(Path(sys.executable)),
            "argsTemplate": [
                str(self.script),
                "--manifest",
                "{manifestPath}",
                "--build",
                "{buildId}",
                "--provider-id",
                provider_id,
                "--mode",
                mode,
            ],
            "supportedSchemas": ["renderdoc-ultra-symbols/v1"],
            "buildPatterns": ["build-*"],
            "timeoutSeconds": 5,
            "fixedFileHashes": {str(self.script): file_hash(self.script)},
        }
        self.registry.write_text(
            json.dumps(
                {
                    "schema": "renderdoc-ultra-provider-registry/v1",
                    "schemaVersion": 1,
                    "providers": [entry],
                }
            ),
            encoding="utf-8",
        )

    def _run_provider(self, provider_id: str, mode: str) -> TrustedProviderResult:
        self._write_registry(provider_id, mode)
        with patch(
            "src.symbols.providers.default_registry_path",
            return_value=self.registry,
        ):
            return run_provider(provider_id, self.assets, build_id="build-1")

    def test_forged_capability_is_rejected_before_database_creation(self) -> None:
        forged = object.__new__(TrustedProviderResult)
        with self.assertRaises(ProviderIntegrityError):
            ingest_trusted_provider_result(
                forged,
                self.assets,
                self.database,
                build_id="build-1",
            )
        self.assertFalse(self.database.exists())

    def test_no_artifact_blob_stays_provisional_and_audit_is_persisted(self) -> None:
        self._write_registry("no-blob", "no-blob")
        service = SymbolService(self.database)
        with patch(
            "src.symbols.providers.default_registry_path",
            return_value=self.registry,
        ):
            summary = service.scan(
                self.assets,
                build_id="build-1",
                provider_id="no-blob",
            )

        rows = service.store.list_asset_shaders(summary["scanId"])
        provider_rows = [
            row for row in rows if row["provenance"] == "trusted-provider-provisional"
        ]
        self.assertEqual(len(provider_rows), 1)
        self.assertFalse(provider_rows[0]["metadata"]["_symbolMetadataTrusted"])
        self.assertFalse(provider_rows[0]["metadata"]["_hashVerified"])
        audit = summary["trustedProvider"]
        self.assertEqual(audit["providerId"], "no-blob")
        self.assertEqual(audit["trustedShaderRecords"], 0)
        self.assertEqual(audit["provisionalShaderRecords"], 1)
        stored = SymbolStore(self.database).get_asset_scan(summary["scanId"])
        self.assertEqual(stored["summary"]["trustedProvider"], audit)

    def test_real_artifact_and_complete_abi_enter_program_matcher(self) -> None:
        result = self._run_provider("complete", "blob")
        summary = ingest_trusted_provider_result(
            result,
            self.assets,
            self.database,
            build_id="build-1",
        )
        store = SymbolStore(self.database)
        rows = store.list_asset_shaders(summary["scanId"])
        trusted = [
            row for row in rows if row["provenance"] == "trusted-provider-artifact"
        ]
        self.assertEqual(len(trusted), 1)
        self.assertTrue(trusted[0]["metadata"]["_symbolMetadataTrusted"])
        self.assertTrue(trusted[0]["metadata"]["_hashVerified"])
        self.assertTrue(
            all(trusted[0]["metadata"]["_providerAudit"]["checks"].values())
        )

        blob = make_dxbc()
        fingerprint = fingerprint_bytes(blob)
        self.assertIsNotNone(fingerprint)
        blob_path = self.capture / "shader.dxbc"
        blob_path.write_bytes(blob)
        shader = {
            "shaderResourceId": 77,
            "stage": "fragment",
            "entryPoint": "main",
            "rawPath": str(blob_path),
            "rawSize": len(blob),
            "rawSha256": fingerprint.raw_sha256,
            "codeSha256": fingerprint.code_sha256,
            **complete_shader_interface(),
        }
        catalog = self.capture / "shader_catalog.v1.json"
        catalog.write_text(
            json.dumps(
                {
                    "schema": "renderdoc-ultra-capture-symbols/v1",
                    "schemaVersion": 1,
                    "capturePath": str(self.capture / "frame.rdc"),
                    "api": "D3D11",
                    "shaders": [shader],
                    "uses": [],
                }
            ),
            encoding="utf-8",
        )

        report = SymbolService(self.database).match(catalog, summary["scanId"])
        self.assertEqual(report["statusCounts"], {"ExactBlobUnique": 1})
        self.assertGreaterEqual(report["aliasCount"], 3)
        matches = store.list_matches(report["captureId"])
        selected = next(item for item in matches if item["selected"])
        self.assertEqual(selected["status"], "ExactBlob")
        self.assertTrue(selected["evidence"]["assetSymbolMetadataTrusted"])
        self.assertEqual(
            SymbolService(self.database).unresolved(report["captureId"])["count"],
            0,
        )

    def test_provider_ingest_uses_same_fail_closed_scan_lock(self) -> None:
        result = self._run_provider("concurrent", "no-blob")
        root = self.assets.resolve()
        scan_key = "%s\n%s" % (os.path.normcase(str(root)), "build-1")
        scan_id = hashlib.sha256(scan_key.encode("utf-8")).hexdigest()[:24]
        lock_path = self.database.parent / (scan_id + ".scan.lock")
        with _exclusive_scan_lock(lock_path):
            with self.assertRaisesRegex(RuntimeError, "already locked"):
                ingest_trusted_provider_result(
                    result,
                    self.assets,
                    self.database,
                    build_id="build-1",
                )
            self.assertTrue(lock_path.is_file())
        self.assertFalse(lock_path.exists())

    def test_mcp_provider_contract_has_no_command_or_registry_override(self) -> None:
        server = Path(__file__).resolve().parents[1] / "src" / "server.py"
        tree = ast.parse(server.read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        forbidden = {
            "registry",
            "registry_path",
            "executable",
            "executable_path",
            "args",
            "arguments",
            "command",
            "env",
            "environment",
        }
        for name in (
            "scan_asset_symbols",
            "match_capture_assets",
            "resolve_capture_symbols",
        ):
            arguments = {argument.arg for argument in functions[name].args.args}
            self.assertIn("provider_id", arguments)
            self.assertFalse(arguments.intersection(forbidden), (name, arguments))


if __name__ == "__main__":
    unittest.main()
