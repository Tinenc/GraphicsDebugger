from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock
from unittest import mock

from src.shader_workspace import (
    ManifestValidationError,
    ShaderKey,
    ShaderWorkspaceError,
    ShaderWorkspaceService,
    ToolchainCatalog,
    ToolConfigurationError,
    ToolchainRoutingError,
    ToolExecutionError,
)
from src.shader_workspace.catalog import capture_identity
from src.shader_workspace.io import atomic_write_json
from src.shader_workspace.leases import (
    SOURCE_TRANSACTION_BACKUP_FILE,
    SOURCE_TRANSACTION_JOURNAL_FILE,
    WorkspaceSourceLeaseError,
    WorkspaceSourceTransactionLease,
)
from src.shader_workspace.toolchains import route_record, toolchain_document
from src.shader_workspace.tools import ToolRegistry
from src.shader_workspace.watcher import ShaderWorkspaceWatchManager
from src.shader_workspace.workspace import _canonical_sha256, _reconstruction_fidelity_status


def make_container(chunks: list[tuple[bytes, bytes]]) -> bytes:
    header_size = 32 + 4 * len(chunks)
    offsets = []
    encoded = []
    cursor = header_size
    for fourcc, payload in chunks:
        value = fourcc + struct.pack("<I", len(payload)) + payload
        offsets.append(cursor)
        encoded.append(value)
        cursor += len(value)
    return (
        b"DXBC"
        + b"\x00" * 16
        + struct.pack("<I", 1)
        + struct.pack("<I", cursor)
        + struct.pack("<I", len(chunks))
        + b"".join(struct.pack("<I", value) for value in offsets)
        + b"".join(encoded)
    )


def make_dxil() -> bytes:
    bitcode = b"BC\xc0\xdefixed-dxil-test-bitcode"
    unpadded_size = 24 + len(bitcode)
    padded_size = (unpadded_size + 3) & ~3
    # ProgramVersion 0x60 is pixel/fragment Shader Model 6.0.
    program = (
        struct.pack(
            "<6I",
            0x00000060,
            padded_size // 4,
            0x4C495844,
            0x00000100,
            16,
            len(bitcode),
        )
        + bitcode
        + b"\x00" * (padded_size - unpadded_size)
    )
    return make_container([(b"DXIL", program)])


FAKE_ROUTE_TOOL = r'''from pathlib import Path
import shutil
import struct
import sys

mode = sys.argv[1]
input_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])
if mode in {"dxil-to-spirv", "hlsl-to-spirv"}:
    if mode == "hlsl-to-spirv" and b"missing_bread_symbol" in input_path.read_bytes():
        print("error: unknown identifier missing_bread_symbol", file=sys.stderr)
        raise SystemExit(2)
    name = b"main\0"
    name += b"\0" * (-len(name) % 4)
    name_words = struct.unpack("<%dI" % (len(name) // 4), name)
    op_entry = [((3 + len(name_words)) << 16) | 15, 4, 1, *name_words]
    output_path.write_bytes(
        struct.pack(
            "<%dI" % (5 + len(op_entry)),
            0x07230203, 0x00010000, 0, 2, 0, *op_entry,
        )
    )
elif mode == "dxil-to-asm":
    output_path.write_text(
        'target triple = "dxil-ms-dx"\n'
        '; fixed native IR fixture\n'
        '%dx.types.Handle = type { i8* }\n'
        '%dx.types.CBufRet.i32 = type { i32, i32, i32, i32 }\n'
        'define void @main() {\n'
        '  %1 = call %dx.types.Handle @dx.op.createHandle(i32 57, i8 2, i32 0, i32 0, i1 false)\n'
        '  %2 = call %dx.types.CBufRet.i32 @dx.op.cbufferLoadLegacy.i32(i32 59, %dx.types.Handle %1, i32 0)\n'
        '  %3 = extractvalue %dx.types.CBufRet.i32 %2, 0\n'
        '  ret void\n'
        '}\n'
        'declare %dx.types.Handle @dx.op.createHandle(i32, i8, i32, i32, i1)\n'
        'declare %dx.types.CBufRet.i32 @dx.op.cbufferLoadLegacy.i32(i32, %dx.types.Handle, i32)\n'
        '!dx.resources = !{!4}\n'
        '!4 = !{null, null, !5, null}\n'
        '!5 = !{!6}\n'
        '!6 = !{i32 0, %B0* undef, !"", i32 0, i32 0, i32 1, i32 8, null}\n',
        encoding="utf-8",
    )
elif mode == "asm-to-dxil":
    shutil.copyfile(Path(sys.argv[4]), output_path)
elif mode == "spirv-to-hlsl":
    if len(sys.argv) < 5 or sys.argv[4] != "main":
        raise SystemExit(21)
    output_path.write_text(
        "cbuffer B0 : register(b0, space0)\n"
        "{\n"
        "    float4 _cb0[1] : packoffset(c0);\n"
        "};\n"
        "float4 main() : SV_Target { return float4(_cb0[0].x, 0, 0, 1); }\n",
        encoding="utf-8",
    )
elif mode == "spirv-to-hlsl-fail":
    raise SystemExit(19)
elif mode == "spirv-to-dxil":
    shutil.copyfile(Path(sys.argv[4]), output_path)
elif mode == "spirv-to-dxil-fail":
    raise SystemExit(23)
else:
    raise SystemExit(3)
'''


class ShaderWorkspaceRouteChainTests(unittest.TestCase):
    def test_project_source_import_compiles_without_decompile_and_preserves_user_edits(self):
        service = self.service()
        workspace = self.root / "project-source-direct"
        self._install_interface(self.catalog_path, self._complete_dxil_interface())
        service.create(self.catalog_path, self.key.catalog_key, workspace, editable_format="hlsl")
        before = service.status(workspace)
        bundle = {
            "schema": "replaylab-verified-project-shader-source/v1",
            "projectId": "a" * 32, "projectPath": str(self.root / "project"),
            "knowledgeId": "b" * 32,
            "sourceText": "float4 main() : SV_Target { return 1; }\n",
            "sourceEntryPoint": "main", "sourceSha256": "",
            "originalRawSha256": before["baseline"]["rawSha256"],
            "baselineAbiSha256": before["baseline"]["abiSha256"],
            "stage": before["stage"], "entryPoint": before["entryPoint"],
            "targetProfile": before["targetProfile"], "originWorkspaceId": "c" * 32,
            "originWorkspacePath": str(self.root / "origin"), "analysisSha256": "",
            "applied": False, "requiresCompile": True, "requiresLiveAbiRevalidation": True,
            "semanticEquivalenceVerified": False,
        }
        bundle["sourceSha256"] = hashlib.sha256(bundle["sourceText"].encode()).hexdigest()
        with mock.patch("src.projects.ReplayProjectService.prepare_workspace_source", return_value=bundle), mock.patch.object(service, "decompile", side_effect=AssertionError("must not decompile")):
            imported = service.import_project_source(workspace, bundle)
            self.assertFalse(imported["decompileInvoked"])
            after = service.status(workspace)
            self.assertEqual(after["source"]["origin"], "verified-project-source")
            self.assertFalse(after["source"]["baselineCompileVerified"])
            self.assertEqual(after["source"]["projectSource"]["knowledgeId"], "b" * 32)
            compiled = service.compile(workspace)
            self.assertTrue(compiled["requiresReplayAbiValidation"])
            self.assertEqual([item["operation"] for item in json.loads((workspace / "manifest.json").read_text())["history"]], ["import-project-source", "compile-route-chain"])
            Path(after["source"]["path"]).write_text("float4 main() : SV_Target { return 0; }\n", encoding="utf-8")
            self.assertTrue(service.status(workspace)["source"]["modifiedSinceDecompile"])
            with self.assertRaisesRegex(ToolExecutionError, "overwrite"):
                service.import_project_source(workspace, bundle)
        source_path = Path(after["source"]["path"])
        user_bytes = source_path.read_bytes()
        self._upgrade_in_place_compiler(remove_decompiler=True)
        self._refresh_same_workspace(workspace, before["routePlan"], source_path)
        refreshed = self.service().status(workspace)
        self.assertEqual(refreshed["source"]["origin"], "toolchain-refreshed-source")
        self.assertEqual(refreshed["source"]["projectSource"], after["source"]["projectSource"])
        self.assertEqual(source_path.read_bytes(), user_bytes)
        self.assertEqual(self.service().compile(workspace)["revision"], 2)
        Path(workspace / imported["inputPath"]).write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ManifestValidationError, "immutable input changed"):
            self.service().status(workspace)

    def setUp(self) -> None:
        temporary_root = Path(
            os.environ.get(
                "RENDERDOC_ULTRA_TEST_TEMP",
                os.environ.get("TEMP", tempfile.gettempdir()),
            )
        )
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=str(temporary_root))
        self.root = Path(self.temporary.name)
        self.script = self.root / "route_tool.py"
        self.script.write_text(FAKE_ROUTE_TOOL, encoding="utf-8")
        executable = Path(sys.executable).resolve()
        base = {
            "executable": str(executable),
            "executableSha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "fixedFileHashes": {
                str(self.script.resolve()): hashlib.sha256(
                    self.script.read_bytes()
                ).hexdigest()
            },
        }

        def operation(name: str, operation_name: str, args: list[str]) -> dict:
            record = dict(base)
            record[operation_name] = {"args": args, "timeoutSeconds": 10}
            return record

        script = str(self.script.resolve())
        self.tools_path = self.root / "shader-tools.v1.json"
        atomic_write_json(
            self.tools_path,
            {
                "schema": "renderdoc-ultra-shader-tools/v1",
                "schemaVersion": 1,
                "tools": {
                    "dxil-spv": operation(
                        "dxil-spv",
                        "transform",
                        [script, "dxil-to-spirv", "{input}", "{output}"],
                    ),
                    "spv-hlsl": operation(
                        "spv-hlsl",
                        "decompile",
                        [
                            script,
                            "spirv-to-hlsl",
                            "{input}",
                            "{output}",
                            "{entry_point}",
                        ],
                    ),
                    "spv-hlsl-fail": operation(
                        "spv-hlsl-fail",
                        "decompile",
                        [script, "spirv-to-hlsl-fail", "{input}", "{output}"],
                    ),
                    "hlsl-spv": operation(
                        "hlsl-spv",
                        "compile",
                        [
                            script,
                            "hlsl-to-spirv",
                            "{input}",
                            "{output}",
                            "{target_profile}",
                        ],
                    ),
                    "spv-dxil": operation(
                        "spv-dxil",
                        "transform",
                        [
                            script,
                            "spirv-to-dxil",
                            "{input}",
                            "{output}",
                            "{baseline}",
                        ],
                    ),
                    "spv-dxil-fail": operation(
                        "spv-dxil-fail",
                        "transform",
                        [
                            script,
                            "spirv-to-dxil-fail",
                            "{input}",
                            "{output}",
                            "{baseline}",
                        ],
                    ),
                    "dxil-asm": operation(
                        "dxil-asm",
                        "decompile",
                        [script, "dxil-to-asm", "{input}", "{output}"],
                    ),
                    "asm-dxil": operation(
                        "asm-dxil",
                        "compile",
                        [
                            script,
                            "asm-to-dxil",
                            "{input}",
                            "{output}",
                            "{baseline}",
                        ],
                    ),
                },
            },
        )
        registry = ToolRegistry(self.tools_path)
        routes = [
            route_record(
                registry,
                route_id="test-dxil-spirv",
                tool_id="dxil-spv",
                operation="transform",
                input_format="dxil",
                output_format="spirv",
                stages=["fragment"],
                fidelity="custom-transform",
                priority=10,
                implementation="fixture DXIL transform",
                source_url="https://example.invalid/dxil-spv",
            ),
            route_record(
                registry,
                route_id="test-spirv-hlsl",
                tool_id="spv-hlsl",
                operation="decompile",
                input_format="spirv",
                output_format="hlsl",
                stages=["fragment"],
                fidelity="reconstructed-source",
                priority=10,
                implementation="Khronos SPIRV-Cross fixture",
                source_url="https://example.invalid/spv-hlsl",
                caveats=["Fixture HLSL is reconstructed."],
            ),
            route_record(
                registry,
                route_id="test-spirv-hlsl-fail",
                tool_id="spv-hlsl-fail",
                operation="decompile",
                input_format="spirv",
                output_format="hlsl",
                stages=["fragment"],
                fidelity="reconstructed-source",
                priority=500,
                implementation="fixture failing recovery",
                source_url="https://example.invalid/spv-hlsl-fail",
                caveats=["This route deliberately fails."],
            ),
            route_record(
                registry,
                route_id="test-hlsl-spirv",
                tool_id="hlsl-spv",
                operation="compile",
                input_format="hlsl",
                output_format="spirv",
                stages=["fragment"],
                # This synthetic SPIR-V is an intermediate in the fixture's
                # HLSL -> SPIR-V -> DXIL path, not a Vulkan target module.
                target_apis=["d3d12"],
                fidelity="native-compile",
                priority=10,
                implementation="fixture HLSL compiler",
                source_url="https://example.invalid/hlsl-spv",
            ),
            route_record(
                registry,
                route_id="test-spirv-dxil",
                tool_id="spv-dxil",
                operation="transform",
                input_format="spirv",
                output_format="dxil",
                stages=["fragment"],
                fidelity="custom-transform",
                priority=10,
                implementation="fixture DXIL packer",
                source_url="https://example.invalid/spv-dxil",
            ),
            route_record(
                registry,
                route_id="test-spirv-dxil-fail",
                tool_id="spv-dxil-fail",
                operation="transform",
                input_format="spirv",
                output_format="dxil",
                stages=["fragment"],
                fidelity="custom-transform",
                priority=500,
                implementation="fixture failing DXIL packer",
                source_url="https://example.invalid/spv-dxil-fail",
            ),
            route_record(
                registry,
                route_id="test-dxil-native-disassembly",
                tool_id="dxil-asm",
                operation="decompile",
                input_format="dxil",
                output_format="dxil-asm",
                stages=["fragment"],
                fidelity="ir-disassembly",
                priority=1,
                implementation="fixture DXIL native disassembly",
                source_url="https://example.invalid/dxil-asm",
            ),
            route_record(
                registry,
                route_id="test-dxil-native-assembly",
                tool_id="asm-dxil",
                operation="compile",
                input_format="dxil-asm",
                output_format="dxil",
                stages=["fragment"],
                fidelity="ir-assembly",
                priority=1,
                implementation="fixture DXIL native assembly",
                source_url="https://example.invalid/asm-dxil",
            ),
        ]
        self.routes_path = self.root / "shader-toolchains.v1.json"
        atomic_write_json(
            self.routes_path, toolchain_document(self.tools_path, routes, [])
        )
        self.blob = make_dxil()
        self.key = ShaderKey(
            91,
            "fragment",
            "main",
            hashlib.sha256(self.blob).hexdigest(),
        )
        catalog_dir = self.root / "capture-catalog"
        (catalog_dir / "blobs").mkdir(parents=True)
        (catalog_dir / "blobs" / (self.key.raw_sha256 + ".bin")).write_bytes(
            self.blob
        )
        shader = {
            "shaderKey": self.key.catalog_key,
            "resourceId": self.key.resource_id,
            "shaderResourceId": self.key.resource_id,
            "stage": self.key.stage,
            "entry": self.key.entry_point,
            "entryPoint": self.key.entry_point,
            "rawPath": "blobs/%s.bin" % self.key.raw_sha256,
            "rawSize": len(self.blob),
            "rawSha256": self.key.raw_sha256,
        }
        catalog = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": "D:/captures/dxil-frame.rdc",
            "api": "D3D12",
            "shaders": [shader],
            "uses": [
                {
                    "eventId": 44,
                    "shaderKey": self.key.catalog_key,
                    "resourceId": self.key.resource_id,
                    "stage": self.key.stage,
                    "entryPoint": self.key.entry_point,
                    "rawSha256": self.key.raw_sha256,
                }
            ],
        }
        catalog["captureId"] = capture_identity(catalog, [self.key])
        self.catalog_path = catalog_dir / "shader_catalog.v1.json"
        atomic_write_json(self.catalog_path, catalog)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(self) -> ShaderWorkspaceService:
        return ShaderWorkspaceService(self.tools_path, self.routes_path)

    def test_legacy_recovery_plan_is_readable_without_exposing_agent_instructions(self) -> None:
        workspace = self.root / "workspace-legacy-recovery-plan"
        service = self.service()
        created = service.create(self.catalog_path, self.key.catalog_key, workspace)
        self.assertNotIn("allowedActions", created["recoveryPlan"])
        manifest_path = workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        recovery = manifest["recoveryPlan"]
        recovery["allowedActions"] = [{"id": "obsolete-retry-order"}]
        recovery["aiPolicy"] = {"mayChooseOnlyAllowedActions": True}
        recovery["planSha256"] = _canonical_sha256({
            key: value for key, value in recovery.items() if key != "planSha256"
        })
        atomic_write_json(manifest_path, manifest)

        status = service.status(workspace)
        self.assertEqual(status["recoveryPlan"]["planSha256"], recovery["planSha256"])
        self.assertNotIn("allowedActions", status["recoveryPlan"])
        self.assertNotIn("aiPolicy", status["recoveryPlan"])

    def _catalog_with_entry(self, entry_point: str) -> tuple[Path, ShaderKey]:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        key = ShaderKey(
            self.key.resource_id,
            self.key.stage,
            entry_point,
            self.key.raw_sha256,
        )
        shader = catalog["shaders"][0]
        shader.update(
            {
                "shaderKey": key.catalog_key,
                "entry": entry_point,
                "entryPoint": entry_point,
            }
        )
        use = catalog["uses"][0]
        use.update({"shaderKey": key.catalog_key, "entryPoint": entry_point})
        catalog["captureId"] = capture_identity(catalog, [key])
        path = self.catalog_path.with_name("shader_catalog-%s.v1.json" % entry_point)
        atomic_write_json(path, catalog)
        return path, key

    def _complete_dxil_interface(self) -> dict:
        # Import locally so unittest discovery of this module does not collect
        # the support module's TestCase classes a second time.
        from tests.test_shader_abi import complete_interface

        interface = complete_interface()
        interface["encoding"] = "ShaderEncoding.DXIL"
        # This fixture's native-IR disassembler intentionally has no CBuffer
        # annotations. Keep these import tests focused on the project binary
        # transaction and target replay ABI binding instead of the separate
        # ambiguous-DXIL-layout evidence path.
        interface["constantBlocks"] = []
        return interface

    def _install_interface(
        self, catalog_path: Path, interface: dict
    ) -> dict:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["shaders"][0].update(json.loads(json.dumps(interface)))
        atomic_write_json(catalog_path, catalog)
        return catalog

    def _write_target_catalog(
        self,
        interface: dict,
        *,
        resource_id: int = 191,
        blob: bytes | None = None,
    ) -> tuple[Path, ShaderKey, dict]:
        target_dir = self.root / "target-capture-catalog"
        blob_dir = target_dir / "blobs"
        blob_dir.mkdir(parents=True)
        target_blob = self.blob if blob is None else blob
        target_key = ShaderKey(
            resource_id,
            self.key.stage,
            self.key.entry_point,
            hashlib.sha256(target_blob).hexdigest(),
        )
        (blob_dir / (target_key.raw_sha256 + ".bin")).write_bytes(target_blob)
        shader = json.loads(json.dumps(interface))
        shader.update(
            {
                "shaderKey": target_key.catalog_key,
                "resourceId": target_key.resource_id,
                "shaderResourceId": target_key.resource_id,
                "stage": target_key.stage,
                "entry": target_key.entry_point,
                "entryPoint": target_key.entry_point,
                "rawPath": "blobs/%s.bin" % target_key.raw_sha256,
                "rawSize": len(target_blob),
                "rawSha256": target_key.raw_sha256,
            }
        )
        catalog = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": "D:/captures/dxil-frame-second.rdc",
            "api": "D3D12",
            "shaders": [shader],
            "uses": [
                {
                    "eventId": 144,
                    "shaderKey": target_key.catalog_key,
                    "resourceId": target_key.resource_id,
                    "stage": target_key.stage,
                    "entryPoint": target_key.entry_point,
                    "rawSha256": target_key.raw_sha256,
                }
            ],
        }
        catalog["captureId"] = capture_identity(catalog, [target_key])
        catalog_path = target_dir / "shader_catalog.v1.json"
        atomic_write_json(catalog_path, catalog)
        return catalog_path, target_key, catalog

    @staticmethod
    def _project_candidate_plan(
        artifact_path: Path,
        *,
        raw_sha256: str,
        code_sha256: str,
        byte_size: int,
        kind: str,
        source_sha256: str,
        source_revision: int,
        target_profile: dict,
    ) -> dict:
        return {
            "schema": "renderdoc-ultra-project-compiled-candidate/v1",
            "schemaVersion": 1,
            "artifactPath": str(artifact_path.resolve()),
            "sha256": raw_sha256,
            "codeSha256": code_sha256,
            "byteSize": byte_size,
            "kind": kind,
            "sourceSha256": source_sha256,
            "sourceRevision": source_revision,
            "targetProfile": json.loads(json.dumps(target_profile)),
            "compilerBindingSha256": "d" * 64,
            "candidateOnly": True,
            "contentAddressed": True,
            "integrityVerified": True,
            "originValidationEvidenceOnly": True,
            "applyAuthorization": False,
            "freshCaptureRebindRequired": True,
            "freshLiveAbiRevalidationRequired": True,
            "oldReplacementIdReused": False,
            "oldCaptureAbiAuthorityReused": False,
        }

    def test_project_candidate_import_requires_fresh_target_replay_abi(self) -> None:
        from tests.test_shader_abi import reflection_response

        interface = self._complete_dxil_interface()
        origin_catalog = self._install_interface(self.catalog_path, interface)
        service = self.service()

        origin_workspace = self.root / "workspace-project-candidate-origin"
        service.create(self.catalog_path, self.key.catalog_key, origin_workspace)
        service.decompile(origin_workspace)
        origin_compiled = service.compile(origin_workspace)
        origin_response = reflection_response(
            interface,
            origin_compiled["rawSha256"],
            Path(origin_compiled["candidateBlobPath"]).stat().st_size,
        )
        origin_response["encoding"] = "dxil"
        origin_validated = service.validate_candidate_reflection(
            origin_workspace, origin_response
        )
        self.assertTrue(origin_validated["replayAbiValidated"])
        origin_status = service.status(origin_workspace)
        origin_latest = origin_status["latestCompiled"]
        self.assertIsNotNone(origin_latest)

        dxil_chunk_offset = struct.unpack_from("<I", self.blob, 32)[0]
        dxil_chunk_size = struct.unpack_from(
            "<I", self.blob, dxil_chunk_offset + 4
        )[0]
        dxil_program = self.blob[
            dxil_chunk_offset + 8 : dxil_chunk_offset + 8 + dxil_chunk_size
        ]
        target_blob = make_container(
            [(b"DXIL", dxil_program), (b"NAME", b"second-capture-metadata")]
        )
        target_catalog_path, target_key, target_catalog = (
            self._write_target_catalog(interface, blob=target_blob)
        )
        self.assertNotEqual(
            origin_catalog["captureId"], target_catalog["captureId"]
        )
        self.assertNotEqual(self.key.resource_id, target_key.resource_id)
        self.assertNotEqual(self.key.raw_sha256, target_key.raw_sha256)
        target_workspace = self.root / "workspace-project-candidate-target"
        target_created = service.create(
            target_catalog_path,
            target_key.catalog_key,
            target_workspace,
        )
        service.decompile(target_workspace)
        target_before = service.status(target_workspace)
        self.assertEqual(
            origin_latest["sourceSha256"],
            target_before["source"]["currentSha256"],
        )
        self.assertEqual(
            origin_status["baseline"]["codeSha256"],
            target_before["baseline"]["codeSha256"],
        )
        self.assertEqual(target_before["revision"], 0)
        self.assertIsNone(target_before["latestCompiled"])

        plan = self._project_candidate_plan(
            Path(origin_latest["candidateBlobPath"]),
            raw_sha256=origin_latest["rawSha256"],
            code_sha256=origin_latest["codeSha256"],
            byte_size=origin_latest["byteSize"],
            kind=origin_latest["kind"],
            source_sha256=origin_latest["sourceSha256"],
            source_revision=origin_status["revision"],
            target_profile=target_created["targetProfile"],
        )
        imported = service.import_project_candidate(target_workspace, plan)
        self.assertTrue(imported["compilerSkipped"])
        self.assertTrue(imported["compiledFromProjectArtifact"])
        self.assertTrue(imported["requiresReplayAbiValidation"])
        self.assertFalse(imported["replayAbiValidated"])
        self.assertFalse(imported["abiCompatible"])
        self.assertFalse(imported["applied"])
        self.assertEqual(imported["revision"], 1)
        self.assertEqual(
            Path(imported["candidateBlobPath"]).read_bytes(),
            Path(origin_latest["candidateBlobPath"]).read_bytes(),
        )

        pending = service.status(target_workspace)
        pending_latest = pending["latestCompiled"]
        self.assertEqual(pending["revision"], 1)
        self.assertTrue(pending["requiresReplayAbiValidation"])
        self.assertFalse(pending["replayAbiValidated"])
        self.assertFalse(pending["abiCompatible"])
        self.assertEqual(
            pending_latest["validationScope"], "container-structure-only"
        )
        self.assertEqual(
            pending_latest["abiGate"]["status"],
            "pending-replay-reflection",
        )
        self.assertNotIn("toolId", pending_latest)
        self.assertNotIn("compileHops", pending_latest)
        self.assertNotIn("originRequestId", pending_latest["projectImport"])
        self.assertNotIn("captureId", pending_latest["projectImport"])
        self.assertNotIn("resourceId", pending_latest["projectImport"])
        self.assertNotIn("replacementId", pending_latest["projectImport"])
        self.assertEqual(
            pending_latest["projectImport"]["compilerBindingSha256"],
            plan["compilerBindingSha256"],
        )

        target_request = service.candidate_reflection_request(target_workspace)
        self.assertEqual(
            target_request["binding"]["captureId"],
            target_catalog["captureId"],
        )
        self.assertEqual(
            target_request["binding"]["workspaceId"], pending["workspaceId"]
        )
        self.assertEqual(target_request["binding"]["revision"], 1)
        self.assertNotEqual(
            target_request["requestId"],
            origin_latest["abiGate"]["requestId"],
        )
        self.assertNotEqual(
            target_request["binding"]["captureId"], origin_catalog["captureId"]
        )

        target_response = reflection_response(
            interface,
            imported["rawSha256"],
            Path(imported["candidateBlobPath"]).stat().st_size,
        )
        target_response["encoding"] = "dxil"
        target_validated = service.validate_candidate_reflection(
            target_workspace, target_response
        )
        self.assertTrue(target_validated["replayAbiValidated"])
        self.assertFalse(target_validated["requiresReplayAbiValidation"])
        self.assertEqual(
            target_validated["requestId"], target_request["requestId"]
        )
        validated_status = service.status(target_workspace)
        self.assertTrue(validated_status["replayAbiValidated"])
        self.assertTrue(validated_status["abiCompatible"])
        self.assertEqual(
            validated_status["latestCompiled"]["validationScope"],
            "replay-reflection-abi-compatible",
        )
        self.assertEqual(
            validated_status["latestCompiled"]["abiGate"]["requestId"],
            target_request["requestId"],
        )
        report = json.loads(
            Path(target_validated["abiReportPath"]).read_text(encoding="utf-8")
        )
        self.assertEqual(report["captureId"], target_catalog["captureId"])
        self.assertEqual(report["workspaceId"], pending["workspaceId"])
        self.assertEqual(report["request"], target_request)

    def test_project_candidate_import_rejects_changed_artifact_atomically(self) -> None:
        interface = self._complete_dxil_interface()
        self._install_interface(self.catalog_path, interface)
        service = self.service()
        workspace = self.root / "workspace-project-candidate-tampered"
        created = service.create(self.catalog_path, self.key.catalog_key, workspace)
        service.decompile(workspace)
        status = service.status(workspace)
        artifact = self.root / "project-candidate-tampered.bin"
        artifact.write_bytes(self.blob)
        plan = self._project_candidate_plan(
            artifact,
            raw_sha256=hashlib.sha256(self.blob).hexdigest(),
            code_sha256=status["baseline"]["codeSha256"],
            byte_size=len(self.blob),
            kind=status["baseline"]["kind"],
            source_sha256=status["source"]["currentSha256"],
            source_revision=1,
            target_profile=created["targetProfile"],
        )
        manifest_path = workspace / "manifest.json"
        manifest_before = manifest_path.read_bytes()
        artifact.write_bytes(self.blob + b"changed-after-plan")

        with self.assertRaisesRegex(
            ToolExecutionError, r"artifact/(?:size|hash) mismatch"
        ):
            service.import_project_candidate(workspace, plan)

        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(list((workspace / "revisions").iterdir()), [])
        self.assertFalse((workspace / ".workspace-operation.lock").exists())
        after = service.status(workspace)
        self.assertEqual(after["revision"], 0)
        self.assertIsNone(after["latestCompiled"])

    def test_project_candidate_import_rejects_source_mismatch_atomically(self) -> None:
        interface = self._complete_dxil_interface()
        self._install_interface(self.catalog_path, interface)
        service = self.service()
        workspace = self.root / "workspace-project-candidate-source-mismatch"
        created = service.create(self.catalog_path, self.key.catalog_key, workspace)
        service.decompile(workspace)
        status = service.status(workspace)
        artifact = self.root / "project-candidate-source-bound.bin"
        artifact.write_bytes(self.blob)
        plan = self._project_candidate_plan(
            artifact,
            raw_sha256=hashlib.sha256(self.blob).hexdigest(),
            code_sha256=status["baseline"]["codeSha256"],
            byte_size=len(self.blob),
            kind=status["baseline"]["kind"],
            source_sha256=status["source"]["currentSha256"],
            source_revision=1,
            target_profile=created["targetProfile"],
        )
        manifest_path = workspace / "manifest.json"
        manifest_before = manifest_path.read_bytes()
        source_path = Path(status["source"]["path"])
        changed_source = source_path.read_bytes() + b"// changed after reuse plan\n"
        source_path.write_bytes(changed_source)

        with self.assertRaisesRegex(
            ToolExecutionError,
            "not safely bound to this source and target",
        ):
            service.import_project_candidate(workspace, plan)

        self.assertEqual(source_path.read_bytes(), changed_source)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(list((workspace / "revisions").iterdir()), [])
        self.assertFalse((workspace / ".workspace-operation.lock").exists())
        after = service.status(workspace)
        self.assertEqual(after["revision"], 0)
        self.assertTrue(after["source"]["modifiedSinceDecompile"])
        self.assertIsNone(after["latestCompiled"])

    def test_project_candidate_import_rejects_injected_old_authority(self) -> None:
        interface = self._complete_dxil_interface()
        self._install_interface(self.catalog_path, interface)
        service = self.service()
        workspace = self.root / "workspace-project-candidate-old-authority"
        created = service.create(self.catalog_path, self.key.catalog_key, workspace)
        service.decompile(workspace)
        status = service.status(workspace)
        artifact = self.root / "project-candidate-authority-free.bin"
        artifact.write_bytes(self.blob)
        plan = self._project_candidate_plan(
            artifact,
            raw_sha256=hashlib.sha256(self.blob).hexdigest(),
            code_sha256=status["baseline"]["codeSha256"],
            byte_size=len(self.blob),
            kind=status["baseline"]["kind"],
            source_sha256=status["source"]["currentSha256"],
            source_revision=1,
            target_profile=created["targetProfile"],
        )
        service.import_project_candidate(workspace, plan)
        manifest_path = workspace / "manifest.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))

        tampered_latest = json.loads(json.dumps(original))
        tampered_latest["latestCompiled"]["replacementId"] = 991
        atomic_write_json(manifest_path, tampered_latest)
        with self.assertRaisesRegex(
            ManifestValidationError,
            "project candidate import receipt is incomplete",
        ):
            service.status(workspace)

        tampered_receipt = json.loads(json.dumps(original))
        tampered_receipt["latestCompiled"]["projectImport"][
            "originRequestId"
        ] = "old-capture-request"
        atomic_write_json(manifest_path, tampered_receipt)
        with self.assertRaisesRegex(
            ManifestValidationError,
            "project candidate import receipt is incomplete",
        ):
            service.status(workspace)

        tampered_history = json.loads(json.dumps(original))
        imported_history = next(
            entry
            for entry in tampered_history["history"]
            if entry.get("operation") == "project-candidate-import"
            and entry.get("status") == "succeeded"
        )
        imported_history["candidate"]["captureId"] = "a" * 64
        atomic_write_json(manifest_path, tampered_history)
        with self.assertRaisesRegex(
            ManifestValidationError,
            "project candidate import history binding mismatch",
        ):
            service.status(workspace)

        legacy_receipt = json.loads(json.dumps(original))
        legacy_receipt["latestCompiled"]["projectImport"].pop(
            "compilerBindingSha256"
        )
        legacy_history = next(
            entry
            for entry in legacy_receipt["history"]
            if entry.get("operation") == "project-candidate-import"
            and entry.get("status") == "succeeded"
        )
        legacy_history["projectImport"].pop("compilerBindingSha256")
        legacy_history["candidate"]["projectImport"].pop(
            "compilerBindingSha256"
        )
        atomic_write_json(manifest_path, legacy_receipt)
        self.assertEqual(service.status(workspace)["revision"], 1)

        atomic_write_json(manifest_path, original)
        restored = service.status(workspace)
        self.assertEqual(restored["revision"], 1)
        self.assertFalse(restored["replayAbiValidated"])
        self.assertNotIn("replacementId", restored["latestCompiled"])

    def test_dxil_spirv_hlsl_uses_physical_intermediate_entry_point(self) -> None:
        catalog_path, key = self._catalog_with_entry("MainPS")
        workspace = self.root / "workspace-physical-spirv-entry"

        self.service().create(catalog_path, key.catalog_key, workspace)
        decompiled = self.service().decompile(workspace)

        self.assertEqual(
            decompiled["routeIds"], ["test-dxil-spirv", "test-spirv-hlsl"]
        )
        self.assertIn(
            "float4 main()",
            Path(decompiled["sourcePath"]).read_text(encoding="utf-8"),
        )
        manifest = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["entryPoint"], "MainPS")
        self.assertEqual(manifest["source"]["entryPoint"], "main")

        compiled = self.service().compile(workspace)
        self.assertEqual(
            compiled["routeIds"], ["test-hlsl-spirv", "test-spirv-dxil"]
        )

    def test_dxil_two_hop_workspace_binds_and_executes_both_directions(self) -> None:
        workspace = self.root / "workspace-two-hop"
        created = self.service().create(
            self.catalog_path, self.key.catalog_key, workspace
        )
        self.assertEqual(
            [item["capability"]["routeId"] for item in created["routePlan"]["decompile"]],
            ["test-dxil-spirv", "test-spirv-hlsl"],
        )
        self.assertEqual(
            [item["capability"]["routeId"] for item in created["routePlan"]["compile"]],
            ["test-hlsl-spirv", "test-spirv-dxil"],
        )
        self.assertEqual(
            created["routePlan"]["routeCatalogSha256"],
            hashlib.sha256(self.routes_path.read_bytes()).hexdigest(),
        )

        decompiled = self.service().decompile(workspace)
        self.assertEqual(len(decompiled["jobPaths"]), 2)
        self.assertTrue(decompiled["baselineCompileVerified"])
        self.assertEqual(len(decompiled["baselineCompileJobPaths"]), 2)
        self.assertEqual(
            decompiled["routeIds"], ["test-dxil-spirv", "test-spirv-hlsl"]
        )
        source = Path(decompiled["sourcePath"])
        self.assertIn("SV_Target", source.read_text(encoding="utf-8"))
        untouched_status = self.service().status(workspace)
        untouched_fidelity = untouched_status["reconstructionFidelity"]
        self.assertTrue(untouched_fidelity["available"])
        self.assertTrue(untouched_fidelity["baselineCompileVerified"])
        self.assertTrue(untouched_fidelity["codeSha256Equal"])
        self.assertTrue(
            untouched_fidelity["currentSourceMatchesUntouchedReconstruction"]
        )
        self.assertFalse(
            untouched_fidelity["currentCandidateMatchesUntouchedReconstruction"]
        )
        self.assertFalse(
            untouched_fidelity["currentCandidateReplayAbiCompatible"]
        )
        self.assertEqual(
            untouched_fidelity["classification"],
            "exact-executable-code-match",
        )
        self.assertFalse(untouched_fidelity["semanticEquivalenceVerified"])
        source.write_text(
            source.read_text(encoding="utf-8") + "// edited\n", encoding="utf-8"
        )

        compiled = self.service().compile(workspace)
        self.assertEqual(len(compiled["jobPaths"]), 2)
        self.assertEqual(
            compiled["routeIds"], ["test-hlsl-spirv", "test-spirv-dxil"]
        )
        self.assertEqual(Path(compiled["candidateBlobPath"]).read_bytes(), self.blob)
        status = ShaderWorkspaceService(
            self.tools_path, self.routes_path
        ).status(workspace)
        self.assertTrue(status["source"]["baselineCompileVerified"])
        self.assertTrue(status["representation"]["baselineCompileVerified"])
        self.assertEqual(status["targetProfile"]["profile"], "ps_6_0")
        self.assertEqual(
            status["latestCompiled"]["compileRouteIds"],
            ["test-hlsl-spirv", "test-spirv-dxil"],
        )
        manifest = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        # This fixture has no captured ABI, so a physical access proof would
        # have no safe consumer. Ordinary HLSL routing remains unchanged and
        # does not manufacture a DXIL disassembly evidence claim.
        self.assertEqual("unavailable", manifest["baseline"]["abiStatus"])
        self.assertNotIn("dxilDisassemblyEvidence", manifest)
        self.assertNotIn(
            "baselineDxilDisassemblyEvidence",
            next(
                entry
                for entry in manifest["history"]
                if entry.get("operation") == "decompile-route-chain"
            ),
        )

    def test_dxil_spirv_cross_hlsl_repairs_only_evidence_bound_cbuffer(self) -> None:
        from tests.test_dxil_access_signature import ambiguous_interface

        interface = ambiguous_interface()
        interface["encoding"] = "ShaderEncoding.DXIL"
        self._install_interface(self.catalog_path, interface)
        workspace = self.root / "workspace-cbuffer-repair"
        service = self.service()
        created = service.create(
            self.catalog_path, self.key.catalog_key, workspace
        )
        self.assertEqual(
            "captured-reflection-dxil-access-signature-required",
            created["baselineAbiStatus"],
        )

        decompiled = service.decompile(workspace)
        repair = decompiled["hlslCBufferAbiRepair"]
        self.assertEqual("applied", repair["status"])
        self.assertEqual(1, len(repair["repairedBlocks"]))
        self.assertEqual(
            {"space": 0, "bind": 0, "byteSize": 8},
            {
                key: repair["repairedBlocks"][0][key]
                for key in ("space", "bind", "byteSize")
            },
        )
        self.assertFalse(repair["physicalAbiVerified"])
        self.assertFalse(repair["semanticEquivalenceVerified"])

        published = Path(decompiled["sourcePath"]).read_text(encoding="utf-8")
        self.assertIn("__replaylab_s0_b0_c0_x_i32", published)
        self.assertIn("__replaylab_s0_b0_c0_y_tail", published)
        self.assertIn("asfloat(__replaylab_s0_b0_c0_x_i32)", published)
        self.assertNotIn("_cb0[0].x", published)

        manifest = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        history = next(
            entry
            for entry in manifest["history"]
            if entry.get("operation") == "decompile-route-chain"
        )
        raw_output = (
            workspace / history["hops"][-1]["outputPath"]
        ).read_text(encoding="utf-8")
        self.assertIn("_cb0[0].x", raw_output)
        self.assertEqual(
            repair, manifest["source"]["hlslCBufferAbiRepair"]
        )
        self.assertEqual(
            "succeeded",
            history["baselineDxilDisassemblyEvidence"]["status"],
        )
        status = service.status(workspace)
        self.assertTrue(status["source"]["baselineCompileVerified"])
        self.assertFalse(status["replayAbiValidated"])
        Path(decompiled["sourcePath"]).write_text(
            published + "// reviewed edit\n", encoding="utf-8"
        )
        edited = service.status(workspace)
        self.assertTrue(edited["source"]["modifiedSinceDecompile"])
        self.assertEqual(
            "applied",
            edited["source"]["hlslCBufferAbiRepair"]["status"],
        )

        tampered = json.loads(json.dumps(manifest))
        tampered["source"]["hlslCBufferAbiRepair"]["repairedBlocks"][0][
            "accessCount"
        ] = 99
        atomic_write_json(workspace / "manifest.json", tampered)
        with self.assertRaisesRegex(
            ManifestValidationError, "CBuffer repair"
        ):
            service.status(workspace)

    def test_reconstruction_fidelity_exposes_abi_compatible_code_mismatch(self) -> None:
        captured_code = "a" * 64
        reconstructed_code = "b" * 64
        source_sha = "c" * 64
        manifest = {"baseline": {"codeSha256": captured_code}}
        source_record = {
            "origin": "trusted-decompile-route",
            "baselineCompile": {
                "codeSha256": reconstructed_code,
                # Raw identity deliberately does not participate in this gate.
                "rawSha256": "d" * 64,
            },
        }
        source_result = {
            "baselineCompileVerified": True,
            "decompiledSha256": source_sha,
            "currentSha256": source_sha,
        }
        latest = {
            "sourceSha256": source_sha,
            "rawSha256": "e" * 64,
            "codeSha256": reconstructed_code,
            "requiresReplayAbiValidation": False,
            "replayAbiValidated": True,
            "abiCompatible": True,
            "abiGate": {
                "status": "compatible",
                "requestId": "1" * 64,
                "reportSha256": "2" * 64,
            },
        }

        fidelity = _reconstruction_fidelity_status(
            manifest, source_record, source_result, latest
        )

        self.assertTrue(fidelity["available"])
        self.assertFalse(fidelity["codeSha256Equal"])
        self.assertTrue(fidelity["currentCandidateReplayAbiCompatible"])
        self.assertEqual(
            fidelity["classification"],
            "abi-compatible-executable-code-mismatch",
        )
        self.assertFalse(fidelity["semanticEquivalenceVerified"])
        latest["rawSha256"] = "f" * 64
        self.assertEqual(
            _reconstruction_fidelity_status(
                manifest, source_record, source_result, latest
            )["classification"],
            "abi-compatible-executable-code-mismatch",
        )
        latest["requiresReplayAbiValidation"] = True
        pending = _reconstruction_fidelity_status(
            manifest, source_record, source_result, latest
        )
        self.assertFalse(pending["currentCandidateReplayAbiCompatible"])
        self.assertEqual(pending["classification"], "executable-code-mismatch")
        latest["requiresReplayAbiValidation"] = False
        latest["abiGate"]["reportSha256"] = "not-a-bound-report"
        unbound = _reconstruction_fidelity_status(
            manifest, source_record, source_result, latest
        )
        self.assertFalse(unbound["currentCandidateReplayAbiCompatible"])
        self.assertEqual(unbound["classification"], "executable-code-mismatch")

    def test_failed_second_hop_never_publishes_source(self) -> None:
        catalog = ToolchainCatalog(self.tools_path, self.routes_path)
        selection = catalog.select_round_trip(
            "dxil", "fragment", editable_format="hlsl", target_api="d3d12"
        )
        selection["decompileRouteIds"] = [
            "test-dxil-spirv",
            "test-spirv-hlsl-fail",
        ]
        workspace = self.root / "workspace-failed-hop"
        self.service().create(
            self.catalog_path,
            self.key.catalog_key,
            workspace,
            toolchain_catalog=catalog,
            toolchain_selection=selection,
        )
        with self.assertRaises(ToolExecutionError):
            self.service().decompile(workspace)
        self.assertFalse((workspace / "source" / "shader.hlsl").exists())
        manifest = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["history"][-1]["status"], "failed")
        self.assertEqual(
            [item["status"] for item in manifest["history"][-1]["hops"]],
            ["succeeded", "failed"],
        )

    def test_default_recovers_to_complete_native_ir_before_compile_only_hlsl(self) -> None:
        document = json.loads(self.routes_path.read_text(encoding="utf-8"))
        document["routes"] = [
            route
            for route in document["routes"]
            if route["routeId"]
            not in {
                "test-dxil-spirv",
                "test-spirv-hlsl",
                "test-spirv-hlsl-fail",
            }
        ]
        atomic_write_json(self.routes_path, document)

        service = self.service()
        automatic_workspace = self.root / "workspace-automatic-native-ir"
        automatic = service.create(
            self.catalog_path, self.key.catalog_key, automatic_workspace
        )
        self.assertEqual("dxil-asm", automatic["routePlan"]["editableFormat"])
        self.assertEqual("round-trip", automatic["recoveryPlan"]["mode"])
        self.assertEqual(
            "automatic-native-ir-round-trip-fallback",
            automatic["recoveryPlan"]["diagnostics"][0]["category"],
        )
        decompiled = service.decompile(automatic_workspace)
        self.assertTrue(Path(decompiled["sourcePath"]).name.endswith(".ll"))
        self.assertTrue(decompiled["baselineCompileVerified"])
        self.assertEqual(
            "executable-editable",
            service.status(automatic_workspace)["representation"]["lane"],
        )

        explicit_workspace = self.root / "workspace-explicit-hlsl"
        explicit = service.create(
            self.catalog_path,
            self.key.catalog_key,
            explicit_workspace,
            editable_format="hlsl",
        )
        self.assertEqual("hlsl", explicit["routePlan"]["editableFormat"])
        self.assertEqual("compile-only-recovery", explicit["recoveryPlan"]["mode"])
        self.assertEqual([], explicit["routePlan"]["decompile"])

    def test_dxil_library_export_uses_exact_lib_profile_for_native_ir_round_trip(self) -> None:
        document = json.loads(self.routes_path.read_text(encoding="utf-8"))
        document["routes"] = [
            route
            for route in document["routes"]
            if route["routeId"] in {
                "test-dxil-native-disassembly",
                "test-dxil-native-assembly",
            }
        ]
        for route in document["routes"]:
            route["stages"] = ["raygen"]
        atomic_write_json(self.routes_path, document)

        library_blob = bytearray(self.blob)
        dxil_chunk_offset = struct.unpack_from("<I", library_blob, 32)[0]
        # ProgramType Library (6), Shader Model 6.3.
        struct.pack_into("<I", library_blob, dxil_chunk_offset + 8, 0x00060063)
        library_blob = bytes(library_blob)
        library_key = ShaderKey(
            92,
            "raygen",
            "RayMain",
            hashlib.sha256(library_blob).hexdigest(),
        )
        catalog_dir = self.root / "ray-library-catalog"
        blob_dir = catalog_dir / "blobs"
        blob_dir.mkdir(parents=True)
        (blob_dir / (library_key.raw_sha256 + ".bin")).write_bytes(library_blob)
        shader = {
            "shaderKey": library_key.catalog_key,
            "resourceId": library_key.resource_id,
            "shaderResourceId": library_key.resource_id,
            "stage": library_key.stage,
            "entry": library_key.entry_point,
            "entryPoint": library_key.entry_point,
            "rawPath": "blobs/%s.bin" % library_key.raw_sha256,
            "rawSize": len(library_blob),
            "rawSha256": library_key.raw_sha256,
        }
        capture_catalog = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": "D:/captures/ray-library.rdc",
            "api": "D3D12",
            "shaders": [shader],
            "uses": [{
                "eventId": 52,
                "shaderKey": library_key.catalog_key,
                "resourceId": library_key.resource_id,
                "stage": library_key.stage,
                "entryPoint": library_key.entry_point,
                "rawSha256": library_key.raw_sha256,
            }],
        }
        capture_catalog["captureId"] = capture_identity(
            capture_catalog, [library_key]
        )
        catalog_path = catalog_dir / "shader_catalog.v1.json"
        atomic_write_json(catalog_path, capture_catalog)

        service = self.service()
        workspace = self.root / "workspace-ray-library-native-ir"
        created = service.create(catalog_path, library_key.catalog_key, workspace)
        self.assertEqual("dxil-asm", created["routePlan"]["editableFormat"])
        self.assertEqual("round-trip", created["recoveryPlan"]["mode"])
        self.assertEqual("lib_6_3", created["targetProfile"]["profile"])
        self.assertEqual("library", created["targetProfile"]["stage"])

        decompiled = service.decompile(workspace)
        self.assertTrue(decompiled["baselineCompileVerified"])
        compiled = service.compile(workspace)
        self.assertEqual(library_blob, Path(compiled["candidateBlobPath"]).read_bytes())
        self.assertEqual(
            "lib_6_3", service.status(workspace)["targetProfile"]["profile"]
        )

    def test_failed_untouched_baseline_compile_never_publishes_source(self) -> None:
        catalog = ToolchainCatalog(self.tools_path, self.routes_path)
        selection = catalog.select_round_trip(
            "dxil", "fragment", editable_format="hlsl", target_api="d3d12"
        )
        selection["compileRouteIds"] = [
            "test-hlsl-spirv",
            "test-spirv-dxil-fail",
        ]
        workspace = self.root / "workspace-baseline-compile-failed"
        self.service().create(
            self.catalog_path,
            self.key.catalog_key,
            workspace,
            toolchain_catalog=catalog,
            toolchain_selection=selection,
        )
        with self.assertRaises(ToolExecutionError):
            self.service().decompile(workspace)
        self.assertFalse((workspace / "source" / "shader.hlsl").exists())
        manifest = json.loads(
            (workspace / "manifest.json").read_text(encoding="utf-8")
        )
        history = manifest["history"][-1]
        self.assertEqual(history["status"], "failed")
        self.assertEqual([item["status"] for item in history["hops"]], ["succeeded", "succeeded"])
        probe = history["baselineCompileProbe"]
        self.assertEqual(probe["status"], "failed")
        self.assertEqual(
            [item["status"] for item in probe["hops"]],
            ["succeeded", "failed"],
        )

    def test_compile_only_recovery_workspace_promotes_hash_bound_source(self) -> None:
        from tests.test_shader_abi import reflection_response

        routes = json.loads(self.routes_path.read_text(encoding="utf-8"))
        routes["routes"] = [
            route
            for route in routes["routes"]
            if route["inputFormat"] == "hlsl"
            or (route["inputFormat"] == "spirv" and route["outputFormat"] == "dxil")
        ]
        atomic_write_json(self.routes_path, routes)

        workspace = self.root / "workspace-compile-only"
        service = self.service()
        interface = self._complete_dxil_interface()
        self._install_interface(self.catalog_path, interface)
        created = service.create(self.catalog_path, self.key.catalog_key, workspace)
        self.assertEqual("compile-only-recovery", created["recoveryPlan"]["mode"])
        self.assertEqual([], created["routePlan"]["decompile"])
        self.assertEqual(
            ["test-hlsl-spirv", "test-spirv-dxil"],
            [
                item["capability"]["routeId"]
                for item in created["routePlan"]["compile"]
            ],
        )

        represented = service.representation(workspace)
        self.assertEqual("binary-only", represented["lane"])
        candidate = workspace / "explanation" / "promoted.hlsl"
        source = b"float4 main() : SV_Target { return float4(1, 0, 0, 1); }\n"
        candidate.write_bytes(source)
        promoted = service.promote_explanatory_source(
            workspace,
            expected_representation_sha256=represented["sha256"],
            expected_source_sha256=hashlib.sha256(source).hexdigest(),
            acknowledge_semantic_equivalence_unverified=True,
        )
        self.assertEqual("executable-editable", promoted["lane"])
        compiled = service.compile(workspace)
        self.assertEqual(self.blob, Path(compiled["candidateBlobPath"]).read_bytes())
        status = service.status(workspace)
        self.assertTrue(status["source"]["compileEligible"])
        self.assertFalse(status["semanticEquivalenceVerified"])
        self.assertFalse(status["applied"])

        reflected = reflection_response(
            interface, compiled["rawSha256"], Path(compiled["candidateBlobPath"]).stat().st_size
        )
        reflected["encoding"] = "dxil"
        self.assertTrue(service.validate_candidate_reflection(workspace, reflected)["replayAbiValidated"])
        original_record = json.loads((workspace / "manifest.json").read_bytes())["source"]
        editable = Path(promoted["sourcePath"])

        edited = source.replace(b"float4(1,", b"float4(0.2,")
        editable.write_bytes(edited)
        stale = self.service().status(workspace)
        self.assertTrue(stale["source"]["compileEligible"])
        self.assertTrue(stale["source"]["modifiedSinceDecompile"])
        self.assertEqual(stale["source"]["decompiledSha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual(stale["abiValidationStatus"], "source-changed")
        self.assertFalse(stale["representation"]["abiCompatible"])
        recompiled = self.service().compile(workspace)
        self.assertEqual(recompiled["sourceSha256"], hashlib.sha256(edited).hexdigest())
        self.assertEqual(recompiled["revision"], compiled["revision"] + 1)
        self.assertFalse(recompiled["replayAbiValidated"])

        editable.write_bytes(edited.replace(b"0.2", b"missing_bread_symbol"))
        with self.assertRaises(ToolExecutionError):
            self.service().compile(workspace)
        failed = self.service().status(workspace)
        self.assertTrue(failed["source"]["compileEligible"])
        self.assertEqual(failed["revision"], recompiled["revision"])
        self.assertEqual(failed["abiValidationStatus"], "source-changed")

        corrected = edited.replace(b"0.2", b"0.3")
        editable.write_bytes(corrected)
        self.assertTrue(self.service().status(workspace)["source"]["compileEligible"])
        retried = self.service().compile(workspace)
        self.assertEqual(retried["revision"], recompiled["revision"] + 1)
        self.assertEqual(retried["sourceSha256"], hashlib.sha256(corrected).hexdigest())
        self.assertFalse(retried["replayAbiValidated"])
        self.assertEqual(candidate.read_bytes(), source)
        self.assertEqual(
            json.loads((workspace / "manifest.json").read_bytes())["source"], original_record
        )

        # Mutable editor contents never replace the original reviewed evidence.
        candidate.write_bytes(corrected)
        with self.assertRaisesRegex(ManifestValidationError, "promoted source evidence/hash binding mismatch"):
            self.service().status(workspace)

    def test_compile_only_recovery_promotes_instruction_assembly(self) -> None:
        original_routes = json.loads(
            self.routes_path.read_text(encoding="utf-8")
        )["routes"]
        routes = [
            next(
                route
                for route in original_routes
                if route["routeId"] == "test-dxil-native-assembly"
            ),
        ]
        atomic_write_json(
            self.routes_path,
            toolchain_document(self.tools_path, routes, []),
        )
        workspace = self.root / "workspace-compile-only-assembly"
        service = self.service()
        created = service.create(
            self.catalog_path,
            self.key.catalog_key,
            workspace,
            editable_format="dxil-asm",
        )
        self.assertEqual("compile-only-recovery", created["recoveryPlan"]["mode"])
        self.assertTrue(created["recoveryPlan"]["sourceConstructionEligible"])
        compiler_attempt = service.create_recovery_attempt(
            workspace,
            self.root / "workspace-compile-only-assembly-attempt",
            expected_recovery_plan_sha256=created["recoveryPlan"]["planSha256"],
            expected_source_workspace_id=created["workspaceId"],
            expected_baseline_raw_sha256=created["baselineRawSha256"],
            expected_capture_id=created["captureId"],
            decompile_candidate_index=None,
            compile_candidate_index=0,
        )
        self.assertEqual([], compiler_attempt["routePlan"]["decompile"])
        self.assertTrue(compiler_attempt["routePlan"]["compile"])
        represented = service.representation(workspace)
        source = b"target triple = \"dxil-ms-dx\"\n; reviewed fixture IR\n"
        candidate = workspace / "explanation" / "promoted.ll"
        candidate.write_bytes(source)
        promoted = service.promote_explanatory_source(
            workspace,
            expected_representation_sha256=represented["sha256"],
            expected_source_sha256=hashlib.sha256(source).hexdigest(),
            acknowledge_semantic_equivalence_unverified=True,
        )
        self.assertTrue(Path(promoted["sourcePath"]).name.endswith(".ll"))
        self.assertEqual("dxil-asm", service.status(workspace)["source"]["format"])
        compiled = service.compile(workspace)
        self.assertEqual(self.blob, Path(compiled["candidateBlobPath"]).read_bytes())

    def test_one_way_decompiler_publishes_explanatory_tool_ir(self) -> None:
        document = json.loads(self.routes_path.read_text(encoding="utf-8"))
        document["routes"] = [
            route
            for route in document["routes"]
            if route["routeId"] in {"test-dxil-spirv", "test-spirv-hlsl"}
        ]
        atomic_write_json(self.routes_path, document)

        workspace = self.root / "workspace-one-way-explanation"
        service = self.service()
        created = service.create(self.catalog_path, self.key.catalog_key, workspace)
        self.assertEqual("evidence-only", created["recoveryPlan"]["mode"])
        self.assertNotIn("allowedActions", created["recoveryPlan"])
        self.assertNotIn("aiPolicy", created["recoveryPlan"])
        self.assertEqual([], created["routePlan"]["compile"])
        self.assertEqual(
            ["test-dxil-spirv", "test-spirv-hlsl"],
            [
                item["capability"]["routeId"]
                for item in created["routePlan"]["decompile"]
            ],
        )

        result = service.decompile(workspace)
        self.assertEqual("explanatory-ir", result["lane"])
        self.assertFalse(result["compileEligible"])
        self.assertFalse((workspace / "source" / "shader.hlsl").exists())
        representation = service.status(workspace)["representation"]
        self.assertEqual("trusted-tool-one-way-decompile", representation["kind"])
        self.assertFalse(representation["compileEligible"])
        self.assertEqual(
            ["test-dxil-spirv", "test-spirv-hlsl"],
            representation["provenance"]["routeIds"],
        )
        self.assertIn(
            "SV_Target", Path(representation["path"]).read_text(encoding="utf-8")
        )
        with self.assertRaisesRegex(ToolExecutionError, "executable-editable"):
            service.compile(workspace)

        descriptor_path = workspace / "explanation" / "representation.json"
        original = json.loads(descriptor_path.read_text(encoding="utf-8"))
        forged_records = []
        forged_kind = json.loads(json.dumps(original))
        forged_kind["kind"] = "forged-claim"
        forged_records.append(forged_kind)
        forged_route = json.loads(json.dumps(original))
        forged_route["provenance"]["routeIds"] = ["not-a-real-route"]
        forged_records.append(forged_route)
        forged_job = json.loads(json.dumps(original))
        forged_job["provenance"]["producedByJobs"] = ["jobs/forged"] * 2
        forged_records.append(forged_job)
        for forged in forged_records:
            atomic_write_json(descriptor_path, forged)
            with self.assertRaises(ManifestValidationError):
                service.status(workspace)
        atomic_write_json(descriptor_path, original)

        manifest_path = workspace / "manifest.json"
        manifest_original = json.loads(manifest_path.read_text(encoding="utf-8"))
        forged_manifest = json.loads(json.dumps(manifest_original))
        forged_hop = forged_manifest["history"][-1]["hops"][0]
        forged_hop["toolId"] = "forged-tool"
        forged_hop["toolConfigDigest"] = "0" * 64
        forged_hop["routeBinding"]["operation"] = {"forged": True}
        atomic_write_json(manifest_path, forged_manifest)
        with self.assertRaises(ManifestValidationError):
            service.status(workspace)
        atomic_write_json(manifest_path, manifest_original)

    def test_recovery_attempt_selects_only_a_listed_alternative_in_new_workspace(self) -> None:
        document = json.loads(self.routes_path.read_text(encoding="utf-8"))
        for route in document["routes"]:
            if route["routeId"] == "test-spirv-hlsl-fail":
                route["priority"] = 1
            elif route["routeId"] == "test-spirv-hlsl":
                route["priority"] = 20
        atomic_write_json(self.routes_path, document)

        service = self.service()
        source_workspace = self.root / "workspace-failed-primary"
        created = service.create(
            self.catalog_path, self.key.catalog_key, source_workspace
        )
        candidates = created["recoveryPlan"]["decompileCandidates"]
        self.assertGreaterEqual(len(candidates), 2)
        self.assertEqual("test-spirv-hlsl-fail", candidates[0][-1])
        failed_representation = service.representation(source_workspace)
        self.assertEqual("binary-only", failed_representation["lane"])

        attempt_workspace = self.root / "workspace-alternative-attempt"
        attempted = service.create_recovery_attempt(
            source_workspace,
            attempt_workspace,
            expected_recovery_plan_sha256=created["recoveryPlan"]["planSha256"],
            expected_source_workspace_id=created["workspaceId"],
            expected_baseline_raw_sha256=created["baselineRawSha256"],
            expected_capture_id=created["captureId"],
            decompile_candidate_index=1,
        )
        self.assertEqual(
            candidates[1],
            [
                hop["capability"]["routeId"]
                for hop in attempted["routePlan"]["decompile"]
            ],
        )
        self.assertEqual(
            source_workspace.resolve(),
            Path(
                attempted["recoveryAttempt"]["sourceWorkspacePath"]
            ).resolve(),
        )
        recovered = service.representation(attempt_workspace)
        self.assertEqual("executable-editable", recovered["lane"])
        status = json.loads(
            (attempt_workspace / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            "recovery-candidate-attempt", status["history"][0]["operation"]
        )

        repeated_primary = self.root / "workspace-repeated-primary"
        with self.assertRaisesRegex(
            ToolchainRoutingError, "repeats the failed source route pair"
        ):
            service.create_recovery_attempt(
                source_workspace,
                repeated_primary,
                expected_recovery_plan_sha256=created["recoveryPlan"][
                    "planSha256"
                ],
                expected_source_workspace_id=created["workspaceId"],
                expected_baseline_raw_sha256=created["baselineRawSha256"],
                expected_capture_id=created["captureId"],
                decompile_candidate_index=0,
            )
        self.assertFalse(repeated_primary.exists())

        chained_target = self.root / "workspace-chained-attempt"
        with self.assertRaisesRegex(
            ToolchainRoutingError, "chained recovery is not allowed"
        ):
            service.create_recovery_attempt(
                attempt_workspace,
                chained_target,
                expected_recovery_plan_sha256=attempted["recoveryPlan"][
                    "planSha256"
                ],
                expected_source_workspace_id=attempted["workspaceId"],
                expected_baseline_raw_sha256=attempted["baselineRawSha256"],
                expected_capture_id=attempted["captureId"],
                decompile_candidate_index=0,
            )
        self.assertFalse(chained_target.exists())

        with self.assertRaisesRegex(
            ToolchainRoutingError, "outside the fixed recovery plan"
        ):
            service.create_recovery_attempt(
                source_workspace,
                self.root / "workspace-invalid-attempt",
                expected_recovery_plan_sha256=created["recoveryPlan"][
                    "planSha256"
                ],
                expected_source_workspace_id=created["workspaceId"],
                expected_baseline_raw_sha256=created["baselineRawSha256"],
                expected_capture_id=created["captureId"],
                decompile_candidate_index=999,
            )
        with self.assertRaisesRegex(ToolchainRoutingError, "plan changed"):
            service.create_recovery_attempt(
                source_workspace,
                self.root / "workspace-stale-plan-attempt",
                expected_recovery_plan_sha256="0" * 64,
                expected_source_workspace_id=created["workspaceId"],
                expected_baseline_raw_sha256=created["baselineRawSha256"],
                expected_capture_id=created["captureId"],
                decompile_candidate_index=1,
            )

        nested_attempt = source_workspace / "nested-recovery-attempt"
        with self.assertRaisesRegex(
            ShaderWorkspaceError, "outside the failed source workspace"
        ):
            service.create_recovery_attempt(
                source_workspace,
                nested_attempt,
                expected_recovery_plan_sha256=created["recoveryPlan"][
                    "planSha256"
                ],
                expected_source_workspace_id=created["workspaceId"],
                expected_baseline_raw_sha256=created["baselineRawSha256"],
                expected_capture_id=created["captureId"],
                decompile_candidate_index=1,
            )
        self.assertFalse(nested_attempt.exists())

    def test_recovery_attempt_catalog_swap_never_publishes_target(self) -> None:
        document = json.loads(self.routes_path.read_text(encoding="utf-8"))
        for route in document["routes"]:
            if route["routeId"] == "test-spirv-hlsl-fail":
                route["priority"] = 1
            elif route["routeId"] == "test-spirv-hlsl":
                route["priority"] = 20
        atomic_write_json(self.routes_path, document)

        service = self.service()
        source_workspace = self.root / "workspace-catalog-swap-source"
        created = service.create(
            self.catalog_path, self.key.catalog_key, source_workspace
        )
        original_catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        original_catalog_for_create = service._catalog_for_create
        swapped = False

        def swap_capture_before_fresh_plan(supplied):
            nonlocal swapped
            if not swapped:
                swapped = True
                replacement = json.loads(json.dumps(original_catalog))
                replacement["capturePath"] = "D:/captures/different-frame.rdc"
                replacement["captureId"] = capture_identity(replacement, [self.key])
                atomic_write_json(self.catalog_path, replacement)
            return original_catalog_for_create(supplied)

        service._catalog_for_create = swap_capture_before_fresh_plan
        target = self.root / "workspace-catalog-swap-target"
        try:
            with self.assertRaisesRegex(
                ToolchainRoutingError, "different capture/shader identity"
            ):
                service.create_recovery_attempt(
                    source_workspace,
                    target,
                    expected_recovery_plan_sha256=created["recoveryPlan"][
                        "planSha256"
                    ],
                    expected_source_workspace_id=created["workspaceId"],
                    expected_baseline_raw_sha256=created["baselineRawSha256"],
                    expected_capture_id=created["captureId"],
                    decompile_candidate_index=1,
                )
        finally:
            atomic_write_json(self.catalog_path, original_catalog)
        self.assertFalse(target.exists())

    def test_route_catalog_reload_and_manifest_chain_tamper_fail_closed(self) -> None:
        workspace = self.root / "workspace-tamper"
        original_service = self.service()
        original_service.create(self.catalog_path, self.key.catalog_key, workspace)
        ShaderWorkspaceService(self.tools_path, self.routes_path).status(workspace)

        original_catalog = self.routes_path.read_bytes()
        self.routes_path.write_bytes(original_catalog + b"\n")
        with self.assertRaises(ToolConfigurationError):
            original_service.status(workspace)
        # A new service verifies the current config snapshot. Formatting alone
        # does not alter any operation selected by the workspace's pinned plan.
        self.service().status(workspace)
        self.routes_path.write_bytes(original_catalog)

        manifest_path = workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["routePlan"]["compile"][0]["capability"]["routeId"] = (
            "test-spirv-dxil"
        )
        atomic_write_json(manifest_path, manifest)
        with self.assertRaises(ManifestValidationError):
            self.service().status(workspace)

    def test_unselected_tool_and_route_update_preserves_existing_workspace_binding(self) -> None:
        workspace = self.root / "workspace-unrelated-tool-update"
        original_service = self.service()
        created = original_service.create(
            self.catalog_path, self.key.catalog_key, workspace
        )
        decompiled = original_service.decompile(workspace)
        source_path = Path(decompiled["sourcePath"])
        original_source = source_path.read_bytes()
        selected_ids = {
            hop["capability"]["toolId"]
            for direction in ("decompile", "compile")
            for hop in created["routePlan"][direction]
        }
        self.assertNotIn("spv-hlsl-fail", selected_ids)
        tools = json.loads(self.tools_path.read_bytes())
        tools["tools"]["spv-hlsl-fail"]["decompile"]["args"].append(
            "--unrelated-provider-update"
        )
        atomic_write_json(self.tools_path, tools)
        registry = ToolRegistry(self.tools_path)
        routes = json.loads(self.routes_path.read_bytes())
        routes["toolRegistrySha256"] = hashlib.sha256(
            self.tools_path.read_bytes()
        ).hexdigest()
        for route in routes["routes"]:
            if route["toolId"] == "spv-hlsl-fail":
                route["implementation"] += " updated"
                route["toolConfigDigest"] = registry.operation(
                    route["toolId"], route["operation"]
                ).config_digest
        atomic_write_json(self.routes_path, routes)

        with self.assertRaises(ToolConfigurationError):
            original_service.status(workspace)
        current_service = self.service()
        status = current_service.status(workspace)
        self.assertEqual(created["routePlan"], status["routePlan"])
        compiled = current_service.compile(workspace)
        self.assertEqual(1, compiled["revision"])
        self.assertEqual(original_source, source_path.read_bytes())

    def _refresh_test_registry(self, tools: dict) -> None:
        atomic_write_json(self.tools_path, tools)
        registry = ToolRegistry(self.tools_path)
        routes = json.loads(self.routes_path.read_bytes())
        routes["toolRegistrySha256"] = hashlib.sha256(self.tools_path.read_bytes()).hexdigest()
        routes["routes"] = [route for route in routes["routes"] if route["toolId"] in tools["tools"]]
        for route in routes["routes"]:
            route["toolConfigDigest"] = registry.operation(route["toolId"], route["operation"]).config_digest
        atomic_write_json(self.routes_path, routes)

    def _prepare_in_place_refresh(self, label: str, *, split_decompiler: bool = False) -> tuple:
        from tests.test_shader_abi import reflection_response

        decompiler = None
        if split_decompiler:
            decompiler = self.root / "old_decompiler.py"
            decompiler.write_bytes(self.script.read_bytes())
            tools = json.loads(self.tools_path.read_bytes())
            for tool_id in ("dxil-spv", "spv-hlsl"):
                tool = tools["tools"][tool_id]
                tool["fixedFileHashes"] = {str(decompiler.resolve()): hashlib.sha256(decompiler.read_bytes()).hexdigest()}
                operation = "decompile" if tool_id == "spv-hlsl" else "transform"
                tool[operation]["args"][0] = str(decompiler.resolve())
            self._refresh_test_registry(tools)
        interface = self._complete_dxil_interface()
        self._install_interface(self.catalog_path, interface)
        service = self.service()
        workspace = self.root / label
        created = service.create(self.catalog_path, self.key.catalog_key, workspace)
        decompiled = service.decompile(workspace)
        source = Path(decompiled["sourcePath"])
        source.write_bytes(source.read_bytes().replace(b", 0, 0, 1)", b", 1, 0, 1)") + b"// user edit preserved\n")
        compiled = service.compile(workspace)
        response = reflection_response(interface, compiled["rawSha256"], Path(compiled["candidateBlobPath"]).stat().st_size)
        response["encoding"] = "dxil"
        self.assertTrue(service.validate_candidate_reflection(workspace, response)["replayAbiValidated"])
        before = (workspace / "manifest.json").read_bytes()
        return workspace, created, source, before, decompiler

    def _upgrade_in_place_compiler(self, *, remove_decompiler: bool = False) -> None:
        tools = json.loads(self.tools_path.read_bytes())
        tools["tools"]["hlsl-spv"]["compile"]["args"].append("--new-fixed-compiler-option")
        if remove_decompiler:
            for tool_id in ("dxil-spv", "spv-hlsl"):
                tools["tools"].pop(tool_id)
        self._refresh_test_registry(tools)

    def _refresh_same_workspace(self, workspace: Path, plan: dict, source: Path) -> dict:
        return self.service().migrate_toolchain(
            workspace, workspace, in_place=True,
            expected_route_plan_sha256=plan["planSha256"],
            expected_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        )

    def test_in_place_refresh_preserves_source_identity_revision_and_drops_candidate_authority(self) -> None:
        workspace, created, source, before, unused_decompiler = self._prepare_in_place_refresh("refresh-same-workspace")
        source_before = source.read_bytes()
        old_manifest = json.loads(before)
        old_candidate = workspace / old_manifest["latestCompiled"]["blobPath"]
        old_candidate_bytes = old_candidate.read_bytes()
        self._upgrade_in_place_compiler()
        stale = self.service().status(workspace)
        self.assertEqual(stale["toolchainStatus"], "refresh-required")
        self.assertFalse(stale["source"]["compileEligible"])
        self.assertFalse(stale["replayAbiValidated"])
        with self.assertRaises(ManifestValidationError):
            self.service()._load_manifest(workspace)

        refreshed = self._refresh_same_workspace(workspace, created["routePlan"], source)

        current = refreshed["status"]
        self.assertTrue(refreshed["inPlace"])
        self.assertEqual(current["workspaceId"], created["workspaceId"])
        self.assertEqual(current["revision"], old_manifest["revision"])
        self.assertEqual(Path(current["workspacePath"]), workspace)
        self.assertEqual(source.read_bytes(), source_before)
        self.assertIsNone(current["latestCompiled"])
        self.assertFalse(current["replayAbiValidated"])
        self.assertFalse(current["abiCompatible"])
        self.assertEqual(current["routePlan"]["decompile"], [])
        self.assertEqual(
            [hop["capability"]["routeId"] for hop in current["routePlan"]["compile"]],
            [hop["capability"]["routeId"] for hop in created["routePlan"]["compile"]],
        )
        receipt = current["source"]["toolchainRefresh"]
        self.assertEqual((workspace / receipt["previousManifestPath"]).read_bytes(), before)
        self.assertEqual(receipt["preservedSourceSha256"], hashlib.sha256(source_before).hexdigest())
        self.assertEqual(old_candidate.read_bytes(), old_candidate_bytes)
        with self.assertRaises(ShaderWorkspaceError):
            self.service().candidate_reflection_request(workspace)
        next_compile = self.service().compile(workspace)
        self.assertEqual(next_compile["revision"], old_manifest["revision"] + 1)
        self.assertFalse(self.service().status(workspace)["replayAbiValidated"])

    def test_in_place_refresh_reads_and_compiles_without_original_decompiler(self) -> None:
        workspace, created, source, before, decompiler = self._prepare_in_place_refresh(
            "refresh-lost-decompiler", split_decompiler=True
        )
        source_before = source.read_bytes()
        decompiler.unlink()
        self._upgrade_in_place_compiler(remove_decompiler=True)
        stale = self.service().status(workspace)
        self.assertEqual(stale["toolchainStatus"], "refresh-required")
        self.assertTrue(stale["source"]["present"])
        self.assertEqual(stale["source"]["currentSha256"], hashlib.sha256(source_before).hexdigest())
        refreshed = self._refresh_same_workspace(workspace, created["routePlan"], source)
        self.assertTrue(refreshed["status"]["source"]["compileEligible"])
        self.assertEqual(refreshed["status"]["routePlan"]["decompile"], [])
        self.assertEqual(self.service().compile(workspace)["revision"], json.loads(before)["revision"] + 1)
        self.assertEqual(source.read_bytes(), source_before)
        self.assertFalse(decompiler.exists())

    def test_in_place_refresh_requires_explicit_rebind_after_compiler_bytes_change(self) -> None:
        workspace, created, source, before, unused_decompiler = self._prepare_in_place_refresh("refresh-compiler-bytes")
        preserved = source.read_bytes()
        self.script.write_bytes(self.script.read_bytes() + b"\n# independently rebuilt compiler fixture\n")
        tools = json.loads(self.tools_path.read_bytes())
        digest = hashlib.sha256(self.script.read_bytes()).hexdigest()
        for operation in tools["tools"].values():
            operation["fixedFileHashes"][str(self.script.resolve())] = digest
        self._refresh_test_registry(tools)
        stale = self.service().status(workspace)
        self.assertEqual(stale["toolchainStatus"], "refresh-required")
        self.assertFalse(stale["source"]["compileEligible"])
        self.assertIsNone(stale["latestCompiled"])
        with self.assertRaises(ManifestValidationError):
            self.service()._load_manifest(workspace)
        self._refresh_same_workspace(workspace, created["routePlan"], source)
        self.assertEqual(self.service().compile(workspace)["revision"], json.loads(before)["revision"] + 1)
        self.assertEqual(source.read_bytes(), preserved)

    def test_in_place_refresh_rejects_stale_cas_but_not_another_participant(self) -> None:
        workspace, created, source, before, unused_decompiler = self._prepare_in_place_refresh("refresh-cas-lease")
        self._upgrade_in_place_compiler()
        values = {"expected_route_plan_sha256": created["routePlan"]["planSha256"],
                  "expected_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        for field in values:
            with self.subTest(field=field):
                invalid = {**values, field: "f" * 64}
                with self.assertRaisesRegex(ToolchainRoutingError, "changed before"):
                    self.service().migrate_toolchain(workspace, workspace, in_place=True, **invalid)
                self.assertEqual((workspace / "manifest.json").read_bytes(), before)
        # Another participant on the workspace (an editor watching it) is not
        # an exclusion: the refresh proceeds, guarded by its own CAS checks.
        with WorkspaceSourceTransactionLease(workspace):
            refreshed = self.service().migrate_toolchain(workspace, workspace, in_place=True, **values)
        self.assertNotEqual((workspace / "manifest.json").read_bytes(), before)
        self.assertTrue((workspace / "toolchain-history").exists())
        self.assertEqual(refreshed["sourceWorkspacePath"], str(workspace.resolve()))

    def test_in_place_refresh_rechecks_source_at_commit(self) -> None:
        from src.shader_workspace import toolchain_refresh

        workspace, created, source, before, unused_decompiler = self._prepare_in_place_refresh("refresh-commit-cas")
        self._upgrade_in_place_compiler()
        expected_source = hashlib.sha256(source.read_bytes()).hexdigest()
        concurrent = source.read_bytes() + b"// concurrent edit must survive\n"
        original_write = toolchain_refresh.atomic_write_bytes

        def archive_then_change(path, data):
            original_write(path, data)
            source.write_bytes(concurrent)

        with mock.patch.object(toolchain_refresh, "atomic_write_bytes", side_effect=archive_then_change):
            with self.assertRaisesRegex(ToolchainRoutingError, "changed before.*commit"):
                self.service().migrate_toolchain(
                    workspace, workspace, in_place=True,
                    expected_route_plan_sha256=created["routePlan"]["planSha256"],
                    expected_source_sha256=expected_source,
                )
        self.assertEqual((workspace / "manifest.json").read_bytes(), before)
        self.assertEqual(source.read_bytes(), concurrent)

    def test_in_place_refresh_rejects_tampered_archive_and_revived_candidate(self) -> None:
        workspace, created, source, before, unused_decompiler = self._prepare_in_place_refresh("refresh-tamper")
        self._upgrade_in_place_compiler()
        refreshed = self._refresh_same_workspace(workspace, created["routePlan"], source)
        manifest_path = workspace / "manifest.json"
        valid = manifest_path.read_bytes()
        archive = workspace / refreshed["status"]["source"]["toolchainRefresh"]["previousManifestPath"]
        archive.write_bytes(before + b"\n")
        with self.assertRaisesRegex(ManifestValidationError, "archive bytes changed"):
            self.service().status(workspace)
        archive.write_bytes(before)
        revived = json.loads(valid)
        revived["latestCompiled"] = json.loads(before)["latestCompiled"]
        atomic_write_json(manifest_path, revived)
        with self.assertRaises(ManifestValidationError):
            self.service().status(workspace)
        manifest_path.write_bytes(valid)
        missing_invalidation = json.loads(valid)
        missing_invalidation["history"].pop()
        atomic_write_json(manifest_path, missing_invalidation)
        with self.assertRaises(ManifestValidationError):
            self.service().status(workspace)
        manifest_path.write_bytes(valid)
        self.assertIsNone(self.service().status(workspace)["latestCompiled"])

    def test_repeated_in_place_refresh_flattens_reconstruction_provenance(self) -> None:
        workspace, created, source, before, unused_decompiler = self._prepare_in_place_refresh("refresh-twice")
        original = json.loads(before)
        self._upgrade_in_place_compiler()
        first = self._refresh_same_workspace(workspace, created["routePlan"], source)
        compiled = self.service().compile(workspace)
        source.write_bytes(source.read_bytes() + b"// further user edit\n")
        preserved = source.read_bytes()
        self._upgrade_in_place_compiler()
        second = self._refresh_same_workspace(workspace, first["status"]["routePlan"], source)
        current = second["status"]
        receipt = current["source"]["toolchainRefresh"]
        self.assertEqual(receipt["sourceProvenance"], original["source"])
        self.assertEqual(receipt["reconstructionRoutePlan"], original["routePlan"])
        self.assertNotIn("toolchainRefresh", receipt["sourceProvenance"])
        self.assertEqual(receipt["fromRoutePlanSha256"], first["routePlanSha256"])
        self.assertEqual(current["workspaceId"], created["workspaceId"])
        self.assertEqual(current["revision"], compiled["revision"])
        self.assertIsNone(current["latestCompiled"])
        self.assertEqual(source.read_bytes(), preserved)
        self.assertEqual(len(list((workspace / "toolchain-history").glob("*/manifest.json"))), 2)
        self.assertEqual(self.service().compile(workspace)["revision"], compiled["revision"] + 1)

    def test_tool_binary_or_command_change_requires_explicit_workspace_migration(self) -> None:
        original_tools = self.tools_path.read_bytes()
        original_routes = self.routes_path.read_bytes()
        original_script = self.script.read_bytes()
        for change in ("tool-bytes", "command-arguments"):
            with self.subTest(change=change):
                self.tools_path.write_bytes(original_tools)
                self.routes_path.write_bytes(original_routes)
                self.script.write_bytes(original_script)
                source_workspace = self.root / ("workspace-before-" + change)
                created = self.service().create(
                    self.catalog_path, self.key.catalog_key, source_workspace
                )
                decompiled = self.service().decompile(source_workspace)
                source = Path(decompiled["sourcePath"]).read_bytes()
                tools = json.loads(original_tools)
                if change == "tool-bytes":
                    # The fixture's pinned script is the tool implementation;
                    # keep it runnable while changing its exact executable code.
                    self.script.write_bytes(original_script + b"\n# upgraded tool\n")
                    digest = hashlib.sha256(self.script.read_bytes()).hexdigest()
                    for tool in tools["tools"].values():
                        tool["fixedFileHashes"][str(self.script.resolve())] = digest
                else:
                    tools["tools"]["hlsl-spv"]["compile"]["args"].append(
                        "--fixture-new-option"
                    )
                atomic_write_json(self.tools_path, tools)
                registry = ToolRegistry(self.tools_path)
                routes = json.loads(original_routes)
                routes["toolRegistrySha256"] = hashlib.sha256(
                    self.tools_path.read_bytes()
                ).hexdigest()
                for route in routes["routes"]:
                    route["toolConfigDigest"] = registry.operation(
                        route["toolId"], route["operation"]
                    ).config_digest
                atomic_write_json(self.routes_path, routes)

                stale = self.service().status(source_workspace)
                self.assertEqual(stale["toolchainStatus"], "refresh-required")
                self.assertEqual(stale["source"]["currentSha256"], hashlib.sha256(source).hexdigest())
                self.assertFalse(stale["source"]["compileEligible"])
                self.assertIsNone(stale["latestCompiled"])
                with self.assertRaises(ManifestValidationError):
                    self.service()._load_manifest(source_workspace)

                target = self.root / ("workspace-after-" + change)
                migrated = self.service().migrate_toolchain(
                    source_workspace,
                    target,
                    expected_route_plan_sha256=created["routePlan"]["planSha256"],
                    expected_source_sha256=hashlib.sha256(source).hexdigest(),
                )
                self.assertFalse(migrated["latestCandidateImported"])
                self.assertEqual(source, (target / "source" / "shader.hlsl").read_bytes())
                current = self.service().status(target)
                self.assertEqual(0, current["revision"])
                self.assertNotEqual(
                    created["routePlan"]["planSha256"],
                    current["routePlan"]["planSha256"],
                )

    def test_toolchain_upgrade_migrates_exact_source_without_old_candidate(self) -> None:
        source_workspace = self.root / "workspace-before-toolchain-upgrade"
        created = self.service().create(
            self.catalog_path, self.key.catalog_key, source_workspace
        )
        decompiled = self.service().decompile(source_workspace)
        source_path = Path(decompiled["sourcePath"])
        source_path.write_bytes(source_path.read_bytes() + b"// preserved edit\n")
        compiled = self.service().compile(source_workspace)
        self.assertEqual(compiled["revision"], 1)
        source_bytes = source_path.read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        source_manifest_path = source_workspace / "manifest.json"
        source_manifest_before = source_manifest_path.read_bytes()

        upgraded_registry_document = json.loads(
            self.tools_path.read_text(encoding="utf-8")
        )
        upgraded_registry_document["tools"]["hlsl-spv"]["compile"][
            "timeoutSeconds"
        ] = 11
        atomic_write_json(self.tools_path, upgraded_registry_document)
        upgraded_registry = ToolRegistry(self.tools_path)

        upgraded_catalog = json.loads(self.routes_path.read_text(encoding="utf-8"))
        upgraded_catalog["toolRegistrySha256"] = hashlib.sha256(
            self.tools_path.read_bytes()
        ).hexdigest()
        for route in upgraded_catalog["routes"]:
            route["implementation"] = route["implementation"] + " upgraded"
            if route["toolId"] == "hlsl-spv":
                route["toolConfigDigest"] = upgraded_registry.operation(
                    route["toolId"], route["operation"]
                ).config_digest
        atomic_write_json(self.routes_path, upgraded_catalog)
        stale = self.service().status(source_workspace)
        self.assertEqual(stale["toolchainStatus"], "refresh-required")
        self.assertEqual(stale["source"]["currentSha256"], source_hash)
        self.assertIsNone(stale["latestCompiled"])
        self.assertFalse(stale["replayAbiValidated"])

        target_workspace = self.root / "workspace-after-toolchain-upgrade"
        migrated = self.service().migrate_toolchain(
            source_workspace,
            target_workspace,
            expected_route_plan_sha256=created["routePlan"]["planSha256"],
            expected_source_sha256=source_hash,
        )

        self.assertEqual(migrated["revision"], 0)
        self.assertFalse(migrated["latestCandidateImported"])
        self.assertFalse(migrated["semanticEquivalenceVerified"])
        self.assertEqual(
            migrated["sourceWorkspaceDisposition"], "preserved-archive"
        )
        self.assertEqual(source_manifest_path.read_bytes(), source_manifest_before)
        self.assertEqual(source_path.read_bytes(), source_bytes)
        migrated_source = target_workspace / "source" / "shader.hlsl"
        self.assertEqual(migrated_source.read_bytes(), source_bytes)

        transfer_evidence = (
            self.service().live_edit_authorization_migration_evidence(
                source_workspace,
                target_workspace,
                expected_source_sha256=source_hash,
            )
        )
        self.assertEqual(transfer_evidence["sourceSha256"], source_hash)
        self.assertEqual(
            transfer_evidence["sourceWorkspaceIdentity"]["workspaceId"],
            created["workspaceId"],
        )
        self.assertNotEqual(
            transfer_evidence["workspaceIdentity"]["workspaceId"],
            created["workspaceId"],
        )
        self.assertEqual(
            {
                key: value
                for key, value in transfer_evidence[
                    "sourceWorkspaceIdentity"
                ].items()
                if key != "workspaceId"
            },
            {
                key: value
                for key, value in transfer_evidence["workspaceIdentity"].items()
                if key != "workspaceId"
            },
        )
        self.assertFalse(transfer_evidence["sourceCandidatesImported"])
        self.assertFalse(transfer_evidence["semanticEquivalenceVerified"])

        migrated_source.write_bytes(source_bytes + b"// unbound target edit\n")
        with self.assertRaisesRegex(
            ToolchainRoutingError, "source bytes changed"
        ):
            self.service().live_edit_authorization_migration_evidence(
                source_workspace,
                target_workspace,
                expected_source_sha256=source_hash,
            )
        migrated_source.write_bytes(source_bytes)

        status = self.service().status(target_workspace)
        self.assertEqual(status["revision"], 0)
        self.assertIsNone(status["latestCompiled"])
        self.assertEqual(
            status["source"]["origin"],
            "toolchain-migration-preserved-source",
        )
        self.assertFalse(status["source"]["baselineCompileVerified"])
        self.assertFalse(status["reconstructionFidelity"]["available"])
        target_manifest = json.loads(
            (target_workspace / "manifest.json").read_text(encoding="utf-8")
        )
        migration = target_manifest["source"]["migration"]
        self.assertFalse(migration["sourceCandidatesImported"])
        self.assertFalse(migration["semanticEquivalenceVerified"])
        self.assertEqual(
            migration["invalidatedSourceCandidate"]["declaredRevision"], 1
        )
        self.assertEqual(
            migration["invalidatedSourceCandidate"]["declaredRawSha256"],
            compiled["rawSha256"],
        )
        self.assertFalse(migration["invalidatedSourceCandidate"]["trusted"])
        self.assertFalse(migration["invalidatedSourceCandidate"]["imported"])
        self.assertEqual(status["source"]["toolchainMigration"], migration)
        self.assertEqual(list((target_workspace / "revisions").iterdir()), [])
        self.assertNotEqual(
            target_manifest["routePlan"]["planSha256"],
            created["routePlan"]["planSha256"],
        )
        self.assertEqual(
            target_manifest["routePlan"]["routeCatalogSha256"],
            hashlib.sha256(self.routes_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            target_manifest["routePlan"]["toolRegistrySha256"],
            hashlib.sha256(self.tools_path.read_bytes()).hexdigest(),
        )
        new_plan_sha256 = target_manifest["routePlan"]["planSha256"]
        newly_compiled = self.service().compile(target_workspace)
        self.assertEqual(
            newly_compiled["revision"], 1
        )
        self.assertEqual(
            self.service().status(target_workspace)["latestCompiled"][
                "routePlanSha256"
            ],
            new_plan_sha256,
        )

    def test_toolchain_migration_rejects_route_or_source_identity_change(self) -> None:
        source_workspace = self.root / "workspace-migration-rejected"
        created = self.service().create(
            self.catalog_path, self.key.catalog_key, source_workspace
        )
        decompiled = self.service().decompile(source_workspace)
        source_path = Path(decompiled["sourcePath"])
        source_bytes = source_path.read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        source_manifest = (source_workspace / "manifest.json").read_bytes()

        upgraded_catalog = json.loads(self.routes_path.read_text(encoding="utf-8"))
        upgraded_catalog["routes"] = [
            route
            for route in upgraded_catalog["routes"]
            if route["routeId"] != "test-hlsl-spirv"
        ]
        atomic_write_json(self.routes_path, upgraded_catalog)
        target = self.root / "workspace-migration-route-rejected"
        with self.assertRaisesRegex(
            ToolchainRoutingError, "unknown route ID"
        ):
            self.service().migrate_toolchain(
                source_workspace,
                target,
                expected_route_plan_sha256=created["routePlan"]["planSha256"],
                expected_source_sha256=source_hash,
            )
        self.assertFalse(target.exists())
        self.assertEqual(source_path.read_bytes(), source_bytes)
        self.assertEqual(
            (source_workspace / "manifest.json").read_bytes(), source_manifest
        )

        with self.assertRaisesRegex(
            ToolchainRoutingError, "source bytes changed"
        ):
            self.service().migrate_toolchain(
                source_workspace,
                self.root / "workspace-migration-source-rejected",
                expected_route_plan_sha256=created["routePlan"]["planSha256"],
                expected_source_sha256="0" * 64,
            )

    def test_project_restore_reopens_only_matching_source_with_old_abi_receipt(self) -> None:
        from tests.test_dxil_access_signature import ambiguous_interface
        from tests.test_shader_abi import reflection_response

        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        interface = ambiguous_interface()
        interface["encoding"] = "ShaderEncoding.DXIL"
        catalog["shaders"][0].update(interface)
        atomic_write_json(self.catalog_path, catalog)

        service = self.service()
        workspace = self.root / "restore-old-abi-receipt"
        service.create(self.catalog_path, self.key.catalog_key, workspace)
        source_path = Path(service.decompile(workspace)["sourcePath"])
        source_path.write_bytes(source_path.read_bytes() + b"// project source\n")
        compiled = service.compile(workspace)
        response = reflection_response(
            interface,
            compiled["rawSha256"],
            Path(compiled["candidateBlobPath"]).stat().st_size,
        )
        response["encoding"] = "dxil"
        validated = service.validate_candidate_reflection(workspace, response)
        self.assertTrue(validated["replayAbiValidated"])
        # One unavailable DXIL evidence probe must be retried; persistent
        # absence still denies the old compatible receipt.
        real_access = service._dxil_access_evidence
        access_calls = 0

        def transient_access(*args):
            nonlocal access_calls
            access_calls += 1
            return (None, None) if access_calls == 1 else real_access(*args)

        with mock.patch.object(service, "_dxil_access_evidence", side_effect=transient_access):
            self.assertTrue(service.status(workspace)["latestCompiled"]["replayAbiValidated"])
        self.assertGreaterEqual(access_calls, 2)
        with mock.patch.object(service, "_dxil_access_evidence", return_value=(None, None)) as missing:
            with self.assertRaisesRegex(
                ManifestValidationError,
                "DXIL access evidence unavailable after bounded recheck",
            ):
                service.status(workspace)
            self.assertGreaterEqual(missing.call_count, 2)
        report_path = Path(validated["abiReportPath"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["comparison"]["compatible"] = False
        atomic_write_json(report_path, report)
        manifest_path = workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["latestCompiled"]["abiGate"]["reportSha256"] = hashlib.sha256(
            report_path.read_bytes()
        ).hexdigest()
        atomic_write_json(manifest_path, manifest)
        old_manifest = manifest_path.read_bytes()
        old_report = report_path.read_bytes()
        old_source = source_path.read_bytes()
        source_sha = hashlib.sha256(old_source).hexdigest()
        key = manifest["shader"]
        capture_id = manifest["capture"]["captureId"]

        with self.assertRaisesRegex(
            ManifestValidationError,
            "candidate ABI report does not prove exact compatibility",
        ):
            service.status(workspace)
        with self.assertRaisesRegex(
            ManifestValidationError, "source target or ABI state is invalid"
        ):
            service.reopen_candidate_for_project_restore(
                workspace,
                expected_capture_id=capture_id,
                expected_resource_id=key["resourceId"],
                expected_stage=key["stage"],
                expected_entry_point=key["entryPoint"],
                expected_original_raw_sha256=key["rawSha256"],
                expected_source_sha256="not-a-project-source-sha",
            )
        self.assertEqual(manifest_path.read_bytes(), old_manifest)

        self.assertTrue(
            service.reopen_candidate_for_project_restore(
                workspace,
                expected_capture_id=capture_id,
                expected_resource_id=key["resourceId"],
                expected_stage=key["stage"],
                expected_entry_point=key["entryPoint"],
                expected_original_raw_sha256=key["rawSha256"],
                expected_source_sha256=source_sha,
            )
        )
        pending = service.status(workspace)
        self.assertEqual(pending["source"]["currentSha256"], source_sha)
        self.assertEqual(pending["latestCompiled"]["abiGate"]["status"], "pending-replay-reflection")
        self.assertFalse(pending["latestCompiled"]["replayAbiValidated"])
        self.assertEqual(source_path.read_bytes(), old_source)
        self.assertEqual(report_path.read_bytes(), old_report)
        archives = list((workspace / "candidate-evidence-archives").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertEqual((archives[0] / "manifest.json").read_bytes(), old_manifest)
        self.assertEqual((archives[0] / "abi-report.json").read_bytes(), old_report)
        self.assertFalse(
            service.reopen_candidate_for_project_restore(
                workspace,
                expected_capture_id=capture_id,
                expected_resource_id=key["resourceId"],
                expected_stage=key["stage"],
                expected_entry_point=key["entryPoint"],
                expected_original_raw_sha256=key["rawSha256"],
                expected_source_sha256=source_sha,
            )
        )
        self.assertTrue(
            service.validate_candidate_reflection(workspace, response)["replayAbiValidated"]
        )

    def test_project_restore_reopens_stale_import_candidate_before_new_source_compile(self) -> None:
        from tests.test_shader_abi import reflection_response

        interface = self._complete_dxil_interface()
        self._install_interface(self.catalog_path, interface)
        service = self.service()
        workspace = self.root / "restore-project-import-old-candidate"
        service.create(self.catalog_path, self.key.catalog_key, workspace)
        before = service.status(workspace)
        bundle = {
            "schema": "replaylab-verified-project-shader-source/v1",
            "projectId": "a" * 32,
            "projectPath": str(self.root / "project"),
            "knowledgeId": "b" * 32,
            "sourceText": "float4 main() : SV_Target { return 1; }\n",
            "sourceEntryPoint": "main",
            "originalRawSha256": before["baseline"]["rawSha256"],
            "baselineAbiSha256": before["baseline"]["abiSha256"],
            "stage": before["stage"],
            "entryPoint": before["entryPoint"],
            "targetProfile": before["targetProfile"],
            "originWorkspaceId": "c" * 32,
            "originWorkspacePath": str(self.root / "origin"),
            "analysisSha256": "",
            "applied": False,
            "requiresCompile": True,
            "requiresLiveAbiRevalidation": True,
            "semanticEquivalenceVerified": False,
        }
        bundle["sourceSha256"] = hashlib.sha256(bundle["sourceText"].encode()).hexdigest()
        with mock.patch(
            "src.projects.ReplayProjectService.prepare_workspace_source",
            return_value=bundle,
        ):
            service.import_project_source(workspace, bundle)
        compiled = service.compile(workspace)
        response = reflection_response(
            interface,
            compiled["rawSha256"],
            Path(compiled["candidateBlobPath"]).stat().st_size,
        )
        response["encoding"] = "dxil"
        validated = service.validate_candidate_reflection(workspace, response)
        self.assertTrue(validated["replayAbiValidated"])
        source_path = workspace / "source" / "shader.hlsl"
        desired_source = b"float4 main() : SV_Target { return 0; }\n"
        source_path.write_bytes(desired_source)
        desired_sha = hashlib.sha256(desired_source).hexdigest()
        self.assertNotEqual(compiled["sourceSha256"], desired_sha)
        report_path = Path(validated["abiReportPath"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["comparison"]["compatible"] = False
        atomic_write_json(report_path, report)
        manifest_path = workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["latestCompiled"]["abiGate"]["reportSha256"] = hashlib.sha256(
            report_path.read_bytes()
        ).hexdigest()
        atomic_write_json(manifest_path, manifest)
        old_report = report_path.read_bytes()
        key = manifest["shader"]

        self.assertTrue(
            service.reopen_candidate_for_project_restore(
                workspace,
                expected_capture_id=manifest["capture"]["captureId"],
                expected_resource_id=key["resourceId"],
                expected_stage=key["stage"],
                expected_entry_point=key["entryPoint"],
                expected_original_raw_sha256=key["rawSha256"],
                expected_source_sha256=desired_sha,
            )
        )
        pending = service.status(workspace)
        self.assertEqual(pending["source"]["origin"], "verified-project-source")
        self.assertEqual(pending["source"]["currentSha256"], desired_sha)
        self.assertEqual(pending["latestCompiled"]["sourceSha256"], compiled["sourceSha256"])
        self.assertEqual(pending["latestCompiled"]["abiGate"]["status"], "pending-replay-reflection")
        self.assertFalse(pending["latestCompiled"]["replayAbiValidated"])
        self.assertEqual(report_path.read_bytes(), old_report)
        replacement = service.compile(workspace)
        self.assertEqual(replacement["sourceSha256"], desired_sha)
        self.assertTrue(replacement["requiresReplayAbiValidation"])

    def test_toolchain_migration_invalidates_incomplete_old_abi_report(self) -> None:
        # Import locally so unittest discovery of this module does not collect
        # the support module's TestCase classes a second time.
        from tests.test_shader_abi import complete_interface, reflection_response

        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        interface = complete_interface()
        interface["encoding"] = "ShaderEncoding.DXIL"
        catalog["shaders"][0].update(interface)
        atomic_write_json(self.catalog_path, catalog)

        service = self.service()
        source_workspace = self.root / "workspace-invalid-old-abi-candidate"
        created = service.create(
            self.catalog_path, self.key.catalog_key, source_workspace
        )
        decompiled = service.decompile(source_workspace)
        source_path = Path(decompiled["sourcePath"])
        source_path.write_bytes(source_path.read_bytes() + b"// preserve me\n")
        compiled = service.compile(source_workspace)
        response = reflection_response(
            interface,
            compiled["rawSha256"],
            Path(compiled["candidateBlobPath"]).stat().st_size,
        )
        response["encoding"] = "dxil"
        validated = service.validate_candidate_reflection(
            source_workspace, response
        )
        self.assertTrue(validated["replayAbiValidated"])

        report_path = Path(validated["abiReportPath"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["response"]["compiledInterface"]["interfaceComplete"] = False
        atomic_write_json(report_path, report)
        manifest_path = source_workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["latestCompiled"]["abiGate"]["reportSha256"] = hashlib.sha256(
            report_path.read_bytes()
        ).hexdigest()
        atomic_write_json(manifest_path, manifest)
        invalid_manifest_bytes = manifest_path.read_bytes()
        invalid_report_bytes = report_path.read_bytes()
        source_bytes = source_path.read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()

        with self.assertRaisesRegex(
            ManifestValidationError,
            "candidate ABI report evidence is invalid: .*interfaceComplete must be true",
        ):
            service.status(source_workspace)

        upgraded_catalog = json.loads(
            self.routes_path.read_text(encoding="utf-8")
        )
        for route in upgraded_catalog["routes"]:
            route["implementation"] = route["implementation"] + " upgraded"
        atomic_write_json(self.routes_path, upgraded_catalog)

        target_workspace = self.root / "workspace-after-invalid-candidate"
        # A ShaderWorkspaceService is bound to the exact process-owned tool and
        # route config snapshot that existed when it was constructed.  The
        # production server factory replaces that service after either small
        # config changes, so exercise migration through the same fresh snapshot
        # instead of asking the stale instance to accept changed catalog bytes.
        migration_service = self.service()
        migrated = migration_service.migrate_toolchain(
            source_workspace,
            target_workspace,
            expected_route_plan_sha256=created["routePlan"]["planSha256"],
            expected_source_sha256=source_hash,
        )

        self.assertEqual(migrated["revision"], 0)
        self.assertFalse(migrated["latestCandidateImported"])
        self.assertEqual(manifest_path.read_bytes(), invalid_manifest_bytes)
        self.assertEqual(report_path.read_bytes(), invalid_report_bytes)
        self.assertEqual(source_path.read_bytes(), source_bytes)
        self.assertEqual(
            (target_workspace / "source" / "shader.hlsl").read_bytes(),
            source_bytes,
        )
        status = migration_service.status(target_workspace)
        self.assertEqual(status["revision"], 0)
        self.assertIsNone(status["latestCompiled"])
        self.assertEqual(list((target_workspace / "revisions").iterdir()), [])
        summary = status["source"]["toolchainMigration"][
            "invalidatedSourceCandidate"
        ]
        self.assertEqual(summary["declaredAbiGateStatus"], "compatible")
        self.assertFalse(summary["trusted"])
        self.assertFalse(summary["imported"])
        self.assertFalse(status["semanticEquivalenceVerified"])

    def test_toolchain_migration_proceeds_alongside_another_participant(self) -> None:
        source_workspace = self.root / "workspace-migration-leased"
        created = self.service().create(
            self.catalog_path, self.key.catalog_key, source_workspace
        )
        decompiled = self.service().decompile(source_workspace)
        source_path = Path(decompiled["sourcePath"])
        source_bytes = source_path.read_bytes()

        upgraded_catalog = json.loads(self.routes_path.read_text(encoding="utf-8"))
        for route in upgraded_catalog["routes"]:
            route["implementation"] = route["implementation"] + " upgraded"
        atomic_write_json(self.routes_path, upgraded_catalog)

        target = self.root / "workspace-migration-while-watched"
        lease = WorkspaceSourceTransactionLease(source_workspace)
        lease.acquire()
        try:
            migrated = self.service().migrate_toolchain(
                source_workspace,
                target,
                expected_route_plan_sha256=created["routePlan"]["planSha256"],
                expected_source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            )
        finally:
            self.assertTrue(lease.release())
        self.assertEqual(migrated["sourceWorkspacePath"], str(source_workspace.resolve()))
        self.assertEqual((target / "source" / "shader.hlsl").read_bytes(), source_bytes)
        self.assertEqual(source_path.read_bytes(), source_bytes)

    def test_toolchain_migration_retires_a_stale_exclusive_lock(self) -> None:
        """A lock file left by an older build's crash is archived, never obeyed."""

        source_workspace = self.root / "workspace-migration-legacy-lease"
        created = self.service().create(
            self.catalog_path, self.key.catalog_key, source_workspace
        )
        decompiled = self.service().decompile(source_workspace)
        source_path = Path(decompiled["sourcePath"])
        source_bytes = source_path.read_bytes()

        upgraded_catalog = json.loads(self.routes_path.read_text(encoding="utf-8"))
        for route in upgraded_catalog["routes"]:
            route["implementation"] = route["implementation"] + " upgraded"
        atomic_write_json(self.routes_path, upgraded_catalog)

        stale = source_workspace / WorkspaceSourceTransactionLease.FILE_NAME
        stale.write_text("0123456789abcdef0123456789abcdef\n", encoding="ascii")
        target = self.root / "workspace-migration-after-legacy-lease"
        migrated = self.service().migrate_toolchain(
            source_workspace,
            target,
            expected_route_plan_sha256=created["routePlan"]["planSha256"],
            expected_source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            host_instance_id="2" * 32,
            recover_legacy_source_lease=True,
        )

        self.assertEqual(migrated["sourceWorkspacePath"], str(source_workspace.resolve()))
        self.assertEqual((target / "source" / "shader.hlsl").read_bytes(), source_bytes)
        self.assertFalse(stale.exists())
        retired = list((source_workspace / "watch").glob("retired-exclusive-lease-*.lock"))
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired[0].read_text(encoding="ascii"),
                         "0123456789abcdef0123456789abcdef\n")


    def test_interactive_compile_ignores_removed_historical_decompiler(self):
        root, created, source, before, decompiler = self._prepare_in_place_refresh(
            "interactive-old-decompiler", split_decompiler=True)
        tools = json.loads(self.tools_path.read_bytes())
        for key in ("dxil-spv", "spv-hlsl"):
            tools["tools"].pop(key)
        self._refresh_test_registry(tools)
        decompiler.unlink()
        service = self.service()
        recipe = service.compiler_contract(root, created["workspaceId"], json.loads(before)["baseline"]["rawSha256"])
        self.assertEqual(recipe["backend"], "bound-route")
        self.assertEqual((root / "manifest.json").read_bytes(), before)
        result = service.compile_for_live_edit(root)
        self.assertTrue(result["manualFastCompile"])
        self.assertFalse(result["replayAbiValidated"])
        self.assertTrue(service.compile_for_live_edit(root)["compilationCacheHit"])

    def test_interactive_compiler_upgrade_repairs_once_preserving_source_and_history(self):
        root, created, source, before, unused = self._prepare_in_place_refresh("interactive-upgrade")
        original_source = source.read_bytes()
        old = json.loads(before)
        previous_blob = root / old["latestCompiled"]["blobPath"]
        previous_bytes = previous_blob.read_bytes()
        self._upgrade_in_place_compiler()
        service = self.service()
        with self.assertRaisesRegex(ManifestValidationError, "identity changed"):
            service.compiler_contract(root, "0" * 32, old["baseline"]["rawSha256"])
        self.assertEqual((root / "manifest.json").read_bytes(), before)
        service.compiler_contract(root, old["workspaceId"], old["baseline"]["rawSha256"])
        repaired = json.loads((root / "manifest.json").read_bytes())
        self.assertIsNone(repaired["latestCompiled"])
        self.assertEqual(repaired["revision"], old["revision"])
        self.assertEqual(source.read_bytes(), original_source)
        self.assertEqual(previous_blob.read_bytes(), previous_bytes)
        self.assertEqual(len(list((root / "toolchain-history").iterdir())), 1)
        service.compiler_contract(root, old["workspaceId"], old["baseline"]["rawSha256"])
        self.assertEqual(len(list((root / "toolchain-history").iterdir())), 1)
        self.assertTrue(service.compile_for_live_edit(root)["manualFastCompile"])

    def test_previous_candidate_damage_does_not_block_preview_or_fresh_manual_compile(self):
        root, created, source, before, unused = self._prepare_in_place_refresh("interactive-bad-candidate")
        old = json.loads(before)
        (root / old["latestCompiled"]["blobPath"]).unlink()
        service = self.service()
        recipe = service.compiler_contract(root, old["workspaceId"], old["baseline"]["rawSha256"])
        self.assertEqual(recipe["stage"], old["stage"])
        self.assertEqual((root / "manifest.json").read_bytes(), before)
        compiled = service.compile_for_live_edit(root)
        self.assertGreater(compiled["revision"], old["revision"])
        self.assertFalse(compiled["replayAbiValidated"])

    def test_removed_compiler_route_uses_an_installed_compatible_route_once(self):
        root, created, source, before, unused = self._prepare_in_place_refresh("interactive-removed-route")
        original = source.read_bytes()
        routes = json.loads(self.routes_path.read_bytes())
        for route in routes["routes"]:
            if route["routeId"] == "test-hlsl-spirv":
                route["routeId"] = "test-hlsl-spirv-upgraded"
        atomic_write_json(self.routes_path, routes)
        service = self.service()
        old = json.loads(before)
        recipe = service.compiler_contract(root, old["workspaceId"], old["baseline"]["rawSha256"])
        self.assertEqual(recipe["stage"], old["stage"])
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(len(list((root / "toolchain-history").iterdir())), 1)
        self.assertTrue(service.compile_for_live_edit(root)["manualFastCompile"])

    def test_baseline_reflection_enrichment_does_not_invalidate_compile_history(self):
        from tests.test_shader_abi import reflection_response
        root = self.root / "refresh-then-reflection-recovery"
        created = self.service().create(self.catalog_path, self.key.catalog_key, root)
        self.service().decompile(root)
        before = (root / "source/shader.hlsl").read_bytes()
        self._upgrade_in_place_compiler()
        service = self.service()
        service.compiler_contract(root, created["workspaceId"], self.key.raw_sha256)
        status = service.status(root)
        self.assertEqual(status["baseline"]["abiStatus"], "unavailable")
        response = reflection_response(self._complete_dxil_interface(), self.key.raw_sha256, len(self.blob))
        response.update(captureId=status["capture"]["captureId"], encoding="dxil")
        service.recover_baseline_reflection(root, service.baseline_reflection_request(root), response)
        ready = service.status(root)
        self.assertEqual(ready["baseline"]["abiStatus"], "captured-reflection-complete")
        service.compiler_contract(root, created["workspaceId"], self.key.raw_sha256)
        self.assertEqual((root / "source/shader.hlsl").read_bytes(), before)
        self.assertTrue(service.compile_for_live_edit(root)["manualFastCompile"])
        manifest = json.loads((root / "manifest.json").read_bytes())
        manifest["baselineInterfaceRecovery"]["baselineRawSha256"] = "0" * 64
        atomic_write_json(root / "manifest.json", manifest)
        with self.assertRaises(ManifestValidationError):
            service.status(root)




DXBC_ASSEMBLY_ROUTE_TOOL = r'''from pathlib import Path
import shutil
import sys

mode = sys.argv[1]
input_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])
if mode == "disassemble":
    output_path.write_text(
        "ps_5_0\n"
        "dcl_globalFlags refactoringAllowed\n"
        "dcl_output o0.xyzw\n"
        "mov o0.xyzw, l(1.000000, 0.000000, 0.000000, 1.000000)\n"
        "ret\n",
        encoding="utf-8",
    )
elif mode == "assemble":
    shutil.copyfile(Path(sys.argv[4]), output_path)
else:
    raise SystemExit(9)
'''



class DxbcAssemblyWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_root = Path(
            os.environ.get(
                "RENDERDOC_ULTRA_TEST_TEMP",
                os.environ.get("TEMP", tempfile.gettempdir()),
            )
        )
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=str(temporary_root))
        self.root = Path(self.temporary.name)
        self.script = self.root / "dxbc_assembly_route_tool.py"
        self.script.write_text(DXBC_ASSEMBLY_ROUTE_TOOL, encoding="utf-8")
        executable = Path(sys.executable).resolve()
        identity = {
            "executable": str(executable),
            "executableSha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "fixedFileHashes": {
                str(self.script.resolve()): hashlib.sha256(
                    self.script.read_bytes()
                ).hexdigest()
            },
            "decompile": {
                "args": [
                    str(self.script.resolve()),
                    "disassemble",
                    "{input}",
                    "{output}",
                    "{raw_sha256}",
                ],
                "timeoutSeconds": 10,
            },
            "compile": {
                "args": [
                    str(self.script.resolve()),
                    "assemble",
                    "{input}",
                    "{output}",
                    "{baseline}",
                    "{raw_sha256}",
                ],
                "timeoutSeconds": 10,
            },
        }
        self.tools_path = self.root / "shader-tools.v1.json"
        atomic_write_json(
            self.tools_path,
            {
                "schema": "renderdoc-ultra-shader-tools/v1",
                "schemaVersion": 1,
                "tools": {"dxbc-assembly": identity},
            },
        )
        registry = ToolRegistry(self.tools_path)
        routes = [
            route_record(
                registry,
                route_id="test-dxbc-disassemble",
                tool_id="dxbc-assembly",
                operation="decompile",
                input_format="dxbc",
                output_format="dxbc-asm",
                stages=["fragment"],
                fidelity="ir-disassembly",
                priority=1,
                implementation="fixture instruction disassembler",
                source_url="https://example.invalid/dxbc-disassemble",
            ),
            route_record(
                registry,
                route_id="test-dxbc-assemble",
                tool_id="dxbc-assembly",
                operation="compile",
                input_format="dxbc-asm",
                output_format="dxbc",
                stages=["fragment"],
                fidelity="ir-assembly",
                priority=1,
                implementation="fixture instruction assembler",
                source_url="https://example.invalid/dxbc-assemble",
            ),
        ]
        self.routes_path = self.root / "shader-toolchains.v1.json"
        atomic_write_json(
            self.routes_path, toolchain_document(self.tools_path, routes, [])
        )
        program = struct.pack("<2I", 0x00000050, 3) + b"\x00\x00\x00\x00"
        self.blob = make_container([(b"SHDR", program)])
        self.key = ShaderKey(
            207,
            "fragment",
            "main",
            hashlib.sha256(self.blob).hexdigest(),
        )
        catalog_dir = self.root / "capture-catalog"
        (catalog_dir / "blobs").mkdir(parents=True)
        (catalog_dir / "blobs" / (self.key.raw_sha256 + ".bin")).write_bytes(
            self.blob
        )
        shader = {
            "shaderKey": self.key.catalog_key,
            "resourceId": self.key.resource_id,
            "shaderResourceId": self.key.resource_id,
            "stage": self.key.stage,
            "entry": self.key.entry_point,
            "entryPoint": self.key.entry_point,
            "rawPath": "blobs/%s.bin" % self.key.raw_sha256,
            "rawSize": len(self.blob),
            "rawSha256": self.key.raw_sha256,
        }
        catalog = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": "D:/captures/dxbc-frame.rdc",
            "api": "D3D11",
            "shaders": [shader],
            "uses": [
                {
                    "eventId": 81,
                    "shaderKey": self.key.catalog_key,
                    "resourceId": self.key.resource_id,
                    "stage": self.key.stage,
                    "entryPoint": self.key.entry_point,
                    "rawSha256": self.key.raw_sha256,
                }
            ],
        }
        catalog["captureId"] = capture_identity(catalog, [self.key])
        self.catalog_path = catalog_dir / "shader_catalog.v1.json"
        atomic_write_json(self.catalog_path, catalog)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(self) -> ShaderWorkspaceService:
        return ShaderWorkspaceService(self.tools_path, self.routes_path)

    def test_explicit_assembly_round_trip_uses_distinct_source_and_revision_names(self) -> None:
        workspace = self.root / "workspace-dxbc-assembly"
        automatic = self.service().create(
            self.catalog_path,
            self.key.catalog_key,
            self.root / "workspace-default-hlsl",
        )
        self.assertEqual("round-trip", automatic["recoveryPlan"]["mode"])
        self.assertEqual("dxbc-asm", automatic["routePlan"]["editableFormat"])
        evidence = self.service().create(
            self.catalog_path,
            self.key.catalog_key,
            self.root / "workspace-explicit-hlsl",
            editable_format="hlsl",
        )
        self.assertEqual("evidence-only", evidence["recoveryPlan"]["mode"])
        self.assertEqual([], evidence["routePlan"]["decompile"])
        self.assertEqual([], evidence["routePlan"]["compile"])
        created = self.service().create(
            self.catalog_path,
            self.key.catalog_key,
            workspace,
            editable_format="dxbc-asm",
        )
        self.assertEqual("dxbc-asm", created["routePlan"]["editableFormat"])
        decompiled = self.service().decompile(workspace)
        source = Path(decompiled["sourcePath"])
        self.assertEqual("shader.dxbc.asm", source.name)
        self.assertFalse((workspace / "source" / "shader.hlsl").exists())
        source.write_text(
            source.read_text(encoding="utf-8").replace("1.000000", "0.500000"),
            encoding="utf-8",
        )
        compiled = self.service().compile(workspace)
        self.assertEqual(self.blob, Path(compiled["candidateBlobPath"]).read_bytes())
        revision_source = workspace / "revisions" / "0001" / "shader.dxbc.asm"
        self.assertTrue(revision_source.is_file())
        status = self.service().status(workspace)
        self.assertEqual("dxbc-asm", status["source"]["format"])
        self.assertEqual("editable-dxbc-assembly", status["representation"]["kind"])
        self.assertFalse(status["representation"]["semanticEquivalenceVerified"])

    def test_save_watcher_tracks_assembly_source_and_compiles_candidate(self) -> None:
        workspace = self.root / "workspace-dxbc-assembly-watch"
        service = self.service()
        service.create(
            self.catalog_path,
            self.key.catalog_key,
            workspace,
            editable_format="dxbc-asm",
        )
        source = Path(service.decompile(workspace)["sourcePath"])
        manager = ShaderWorkspaceWatchManager(self.service)
        try:
            started = manager.start(workspace, debounce_ms=100)
            self.assertEqual(source, Path(started["sourcePath"]))
            source.write_text(
                source.read_text(encoding="utf-8") + "// assembly edit\n",
                encoding="utf-8",
            )
            deadline = time.monotonic() + 5.0
            snapshot = started
            while time.monotonic() < deadline:
                snapshot = manager.poll(started["watchId"])
                if snapshot["compileSuccesses"] >= 1:
                    break
                time.sleep(0.05)
            self.assertEqual(1, snapshot["compileSuccesses"])
            self.assertEqual(0, snapshot["compileFailures"])
            self.assertEqual(1, snapshot["latestCandidateRevision"])
        finally:
            manager.shutdown()




if __name__ == "__main__":
    unittest.main()
