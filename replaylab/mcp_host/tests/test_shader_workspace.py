from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src.shader_workspace import (
    CatalogValidationError,
    ManifestValidationError,
    ShaderKey,
    ShaderWorkspaceService,
    ToolConfigurationError,
    ToolExecutionError,
)
from src.shader_workspace.catalog import capture_identity, normalize_stage
from src.shader_workspace.toolchains import route_record, toolchain_document
from src.shader_workspace.tools import ToolRegistry
import src.shader_workspace.workspace as workspace_module


class ShaderStageNormalizationTests(unittest.TestCase):
    def test_task_and_ray_generation_aliases_are_canonical(self) -> None:
        self.assertEqual(normalize_stage("amplification"), "task")
        self.assertEqual(normalize_stage("raygeneration"), "raygen")
        self.assertEqual(normalize_stage("ray generation"), "raygen")


def make_dxbc(instructions: bytes = b"shader instructions") -> bytes:
    body = instructions + b"\x00" * (-len(instructions) % 4)
    code = struct.pack("<2I", 0x00000050, 2 + len(body) // 4) + body
    chunk = b"SHDR" + struct.pack("<I", len(code)) + code
    header_size = 36
    total_size = header_size + len(chunk)
    return (
        b"DXBC"
        + b"\x00" * 16
        + struct.pack("<I", 1)
        + struct.pack("<I", total_size)
        + struct.pack("<I", 1)
        + struct.pack("<I", header_size)
        + chunk
    )


FAKE_TOOL = r"""from pathlib import Path
import os
import shutil
import subprocess
import sys
import time

mode = sys.argv[1]
input_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])

if mode == "decompile":
    stage = sys.argv[4]
    entry = sys.argv[5]
    output_path.write_text(
        "// fake deterministic decompile: %s/%s\n"
        "float4 %s() : SV_Target { return 1; }\n"
        % (stage, entry, entry or "main"),
        encoding="utf-8",
    )
elif mode == "decompile-zero":
    output_path.write_bytes(b"")
elif mode == "decompile-nonutf8":
    output_path.write_bytes(b"\xff\xfe\x00")
elif mode == "decompile-spam":
    sys.stdout.write("x" * 4096)
    output_path.write_text(
        "float4 main() : SV_Target { return 1; }\n", encoding="utf-8"
    )
elif mode == "decompile-env":
    output_path.write_text(
        "// secret=%s path=%s temp=%s\n"
        % (
            os.environ.get("SHADER_WORKSPACE_SECRET", "<missing>"),
            os.environ.get("PATH", "<missing>"),
            os.environ.get("TEMP", "<missing>"),
        ),
        encoding="utf-8",
    )
elif mode == "decompile-self-tamper":
    with Path(__file__).open("a", encoding="utf-8") as stream:
        stream.write("\n# changed while running\n")
    output_path.write_text(
        "float4 main() : SV_Target { return 1; }\n", encoding="utf-8"
    )
elif mode == "decompile-timeout-tree":
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(60)
elif mode == "disassemble-dxbc":
    output_path.write_text(
        "ps_5_0\n"
        "dcl_globalFlags refactoringAllowed\n"
        "dcl_output o0.xyzw\n"
        "mov o0.xyzw, l(1.000000, 1.000000, 1.000000, 1.000000)\n"
        "ret\n",
        encoding="utf-8",
    )
elif mode == "assemble-dxbc":
    shutil.copyfile(Path(sys.argv[4]), output_path)
elif mode == "compile":
    baseline_path = Path(sys.argv[4])
    shutil.copyfile(baseline_path, output_path)
elif mode == "compile-invalid":
    output_path.write_bytes(b"not a DXBC container")
elif mode == "fail":
    print("intentional fake tool failure", file=sys.stderr)
    raise SystemExit(7)
else:
    raise SystemExit("unknown fake mode")
"""


class ShaderWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_root = Path(
            os.environ.get(
                "RENDERDOC_ULTRA_TEST_TEMP",
                os.environ.get("TEMP", tempfile.gettempdir()),
            )
        )
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=str(temp_root))
        self.root = Path(self.temporary.name)
        self.fake_tool = self.root / "fake_tool.py"
        self.fake_tool.write_text(FAKE_TOOL, encoding="utf-8")
        self.fake_tool_sha256 = hashlib.sha256(self.fake_tool.read_bytes()).hexdigest()
        self.python_executable = Path(sys.executable).resolve()
        self.python_sha256 = hashlib.sha256(
            self.python_executable.read_bytes()
        ).hexdigest()
        self.tamper_executable = self.root / "tamper-python.exe"
        shutil.copyfile(self.python_executable, self.tamper_executable)
        self.tamper_executable_sha256 = hashlib.sha256(
            self.tamper_executable.read_bytes()
        ).hexdigest()
        self.config = self.root / "shader-tools.json"
        self._write_config()
        self.catalog_dir = self.root / "capture-catalog"
        self.catalog_dir.mkdir()
        self.blob = make_dxbc()
        self.key = ShaderKey(
            resource_id=91,
            stage="fragment",
            entry_point="main",
            raw_sha256=hashlib.sha256(self.blob).hexdigest(),
        )
        self.catalog = self._write_catalog(self.blob)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_config(self, extra_argument: str | None = None) -> None:
        executable = str(self.python_executable)
        script = str(self.fake_tool.resolve())
        decompile_args = [
            script,
            "decompile",
            "{input}",
            "{output}",
            "{stage}",
            "{entry_point}",
        ]
        if extra_argument is not None:
            decompile_args.append(extra_argument)
        value = {
            "schema": "renderdoc-ultra-shader-tools/v1",
            "schemaVersion": 1,
            "tools": {
                "good": {
                    "executable": executable,
                    "executableSha256": self.python_sha256,
                    "fixedFileHashes": {
                        str(self.fake_tool.resolve()): self.fake_tool_sha256
                    },
                    "decompile": {"args": decompile_args, "timeoutSeconds": 10},
                    "compile": {
                        "args": [
                            script,
                            "compile",
                            "{input}",
                            "{output}",
                            "{baseline}",
                            "{target_profile}",
                        ],
                        "timeoutSeconds": 10,
                    },
                },
                "zero": {
                    "executable": executable,
                    "executableSha256": self.python_sha256,
                    "fixedFileHashes": {
                        str(self.fake_tool.resolve()): self.fake_tool_sha256
                    },
                    "decompile": {
                        "args": [script, "decompile-zero", "{input}", "{output}"],
                        "timeoutSeconds": 10,
                    },
                    "compile": {
                        "args": [
                            script,
                            "compile",
                            "{input}",
                            "{output}",
                            "{baseline}",
                            "{target_profile}",
                        ],
                        "timeoutSeconds": 10,
                    },
                },
                "nonutf8": {
                    "executable": executable,
                    "executableSha256": self.python_sha256,
                    "fixedFileHashes": {
                        str(self.fake_tool.resolve()): self.fake_tool_sha256
                    },
                    "decompile": {
                        "args": [script, "decompile-nonutf8", "{input}", "{output}"],
                        "timeoutSeconds": 10,
                    },
                    "compile": {
                        "args": [
                            script,
                            "compile",
                            "{input}",
                            "{output}",
                            "{baseline}",
                            "{target_profile}",
                        ],
                        "timeoutSeconds": 10,
                    },
                },
                "spam": {
                    "executable": executable,
                    "executableSha256": self.python_sha256,
                    "fixedFileHashes": {
                        str(self.fake_tool.resolve()): self.fake_tool_sha256
                    },
                    "decompile": {
                        "args": [script, "decompile-spam", "{input}", "{output}"],
                        "timeoutSeconds": 10,
                    },
                    "compile": {
                        "args": [
                            script,
                            "compile",
                            "{input}",
                            "{output}",
                            "{baseline}",
                            "{target_profile}",
                        ],
                        "timeoutSeconds": 10,
                    },
                },
                "invalid-compiler": {
                    "executable": executable,
                    "executableSha256": self.python_sha256,
                    "fixedFileHashes": {
                        str(self.fake_tool.resolve()): self.fake_tool_sha256
                    },
                    "decompile": {
                        "args": [
                            script,
                            "decompile",
                            "{input}",
                            "{output}",
                            "{stage}",
                            "{entry_point}",
                        ],
                        "timeoutSeconds": 10,
                    },
                    "compile": {
                        "args": [
                            script,
                            "compile-invalid",
                            "{input}",
                            "{output}",
                            "{target_profile}",
                        ],
                        "timeoutSeconds": 10,
                    },
                },
                "tamper": {
                    "executable": str(self.tamper_executable),
                    "executableSha256": self.tamper_executable_sha256,
                    "fixedFileHashes": {
                        str(self.fake_tool.resolve()): self.fake_tool_sha256
                    },
                    "decompile": {
                        "args": [script, "decompile-zero", "{input}", "{output}"],
                        "timeoutSeconds": 10,
                    },
                    "compile": {
                        "args": [
                            script,
                            "compile-invalid",
                            "{input}",
                            "{output}",
                            "{target_profile}",
                        ],
                        "timeoutSeconds": 10,
                    },
                },
                "minimal-env": {
                    "executable": executable,
                    "executableSha256": self.python_sha256,
                    "fixedFileHashes": {
                        str(self.fake_tool.resolve()): self.fake_tool_sha256
                    },
                    "decompile": {
                        "args": [script, "decompile-env", "{input}", "{output}"],
                        "timeoutSeconds": 10,
                    },
                    "compile": {
                        "args": [
                            script,
                            "compile",
                            "{input}",
                            "{output}",
                            "{baseline}",
                            "{target_profile}",
                        ],
                        "timeoutSeconds": 10,
                    },
                },
                "self-tamper": {
                    "executable": executable,
                    "executableSha256": self.python_sha256,
                    "fixedFileHashes": {
                        str(self.fake_tool.resolve()): self.fake_tool_sha256
                    },
                    "decompile": {
                        "args": [
                            script,
                            "decompile-self-tamper",
                            "{input}",
                            "{output}",
                        ],
                        "timeoutSeconds": 10,
                    },
                    "compile": {
                        "args": [
                            script,
                            "compile",
                            "{input}",
                            "{output}",
                            "{baseline}",
                            "{target_profile}",
                        ],
                        "timeoutSeconds": 10,
                    },
                },
                "timeout-tree": {
                    "executable": executable,
                    "executableSha256": self.python_sha256,
                    "fixedFileHashes": {
                        str(self.fake_tool.resolve()): self.fake_tool_sha256
                    },
                    "decompile": {
                        "args": [
                            script,
                            "decompile-timeout-tree",
                            "{input}",
                            "{output}",
                        ],
                        "timeoutSeconds": 1,
                    },
                    "compile": {
                        "args": [
                            script,
                            "compile",
                            "{input}",
                            "{output}",
                            "{baseline}",
                            "{target_profile}",
                        ],
                        "timeoutSeconds": 10,
                    },
                },
            },
        }
        self.config.write_text(json.dumps(value), encoding="utf-8")

    def _write_catalog(self, blob: bytes) -> Path:
        blob_dir = self.catalog_dir / "blobs"
        blob_dir.mkdir(exist_ok=True)
        blob_path = blob_dir / (self.key.raw_sha256 + ".bin")
        blob_path.write_bytes(blob)
        shader = {
            "shaderKey": self.key.catalog_key,
            "resourceId": self.key.resource_id,
            "shaderResourceId": self.key.resource_id,
            "stage": self.key.stage,
            "entry": self.key.entry_point,
            "entryPoint": self.key.entry_point,
            "rawPath": "blobs/%s.bin" % self.key.raw_sha256,
            "rawSize": len(blob),
            "rawSha256": self.key.raw_sha256,
        }
        catalog = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": "D:/captures/frame.rdc",
            "api": "D3D11",
            "shaders": [shader],
            "uses": [],
        }
        catalog["captureId"] = capture_identity(catalog, [self.key])
        path = self.catalog_dir / "shader_catalog.v1.json"
        path.write_text(json.dumps(catalog), encoding="utf-8")
        return path

    def _create(self, name: str, decompiler: str = "good", compiler: str = "good"):
        workspace = self.root / name
        service = ShaderWorkspaceService(self.config)
        created = service.create(
            self.catalog,
            self.key.catalog_key,
            workspace,
            decompiler,
            compiler,
        )
        return service, workspace, created

    def test_source_edit_status_ignores_broken_candidate_and_toolchain(self):
        service, workspace, _ = self._create("text-only")
        service.decompile(workspace)
        manifest_path = workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["latestCompiled"] = {"path": "missing-candidate.dxbc"}
        manifest["tools"]["compiler"]["executableSha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        before = manifest_path.read_bytes()
        with mock.patch.object(service, "_load_manifest", side_effect=AssertionError("strict status")), \
             mock.patch.object(service, "_process_catalog", side_effect=AssertionError("toolchain")), \
             mock.patch.object(workspace_module, "fingerprint_shader_executable", side_effect=AssertionError("ABI")):
            result = service.source_edit_status(workspace)
        self.assertEqual(result["validationScope"], "source-edit-only")
        self.assertTrue(result["source"]["present"])
        self.assertEqual(result["shader"], self.key.as_dict())
        self.assertFalse(result["replayAbiValidated"])
        self.assertFalse(result["abiCompatible"])
        self.assertIsNone(result["latestCompiled"])
        self.assertEqual(before, manifest_path.read_bytes())
        with self.assertRaises(ManifestValidationError):
            service.status(workspace)

    def test_source_edit_status_reads_current_text_without_incrementing_revision(self):
        service, workspace, _ = self._create("text-edited")
        service.decompile(workspace)
        before = service.source_edit_status(workspace)
        path = Path(before["source"]["path"])
        revised = path.read_bytes() + b"\n// all branches can be edited together\n"
        path.write_bytes(revised)
        after = service.source_edit_status(workspace)
        self.assertEqual(after["source"]["currentSha256"], hashlib.sha256(revised).hexdigest())
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["source"]["decompiledSha256"], before["source"]["decompiledSha256"])

    def test_source_edit_status_does_not_require_compile_eligibility(self):
        service, workspace, _ = self._create("text-not-compiled")
        service.decompile(workspace)
        manifest_path = workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"]["compileEligible"] = False
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        before = manifest_path.read_bytes()
        status = service.source_edit_status(workspace)
        self.assertTrue(status["source"]["present"])
        self.assertFalse(status["source"]["compileEligible"])
        self.assertFalse(status["representation"]["applyEligible"])
        self.assertEqual(before, manifest_path.read_bytes())
        with self.assertRaises(ManifestValidationError):
            service.status(workspace)

    def test_source_edit_status_rejects_wrong_text_identity(self):
        service, workspace, _ = self._create("text-invalid")
        service.decompile(workspace)
        manifest_path = workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for change in ({"path": "../fake_tool.py"}, {"baselineRawSha256": "0" * 64}):
            with self.subTest(change=change):
                changed = json.loads(json.dumps(manifest))
                changed["source"].update(change)
                manifest_path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(ManifestValidationError):
                    service.source_edit_status(workspace)

    def test_source_edit_status_rejects_changed_baseline_and_invalid_text(self):
        service, workspace, _ = self._create("text-corrupt")
        service.decompile(workspace)
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        path = workspace / manifest["source"]["path"]
        original = path.read_bytes()
        for invalid in (b"", b"\xff", b"float\x00 x;"):
            with self.subTest(invalid=invalid):
                path.write_bytes(invalid)
                with self.assertRaises(ManifestValidationError):
                    service.source_edit_status(workspace)
        path.write_bytes(original)
        (workspace / manifest["baseline"]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ManifestValidationError, "baseline hash mismatch"):
            service.source_edit_status(workspace)

    def _dxbc_assembly_route_service(self) -> tuple[ShaderWorkspaceService, Path]:
        document = json.loads(self.config.read_text(encoding="utf-8"))
        script = str(self.fake_tool.resolve())
        document["tools"]["dxbc-native"] = {
            "executable": str(self.python_executable),
            "executableSha256": self.python_sha256,
            "fixedFileHashes": {script: self.fake_tool_sha256},
            "decompile": {
                "args": [script, "disassemble-dxbc", "{input}", "{output}"],
                "timeoutSeconds": 10,
            },
            "compile": {
                "args": [
                    script,
                    "assemble-dxbc",
                    "{input}",
                    "{output}",
                    "{baseline}",
                ],
                "timeoutSeconds": 10,
            },
        }
        self.config.write_text(json.dumps(document), encoding="utf-8")
        registry = ToolRegistry(self.config)
        routes = [
            route_record(
                registry,
                route_id="test-portable-dxbc-disassembly",
                tool_id="dxbc-native",
                operation="decompile",
                input_format="dxbc",
                output_format="dxbc-asm",
                stages=["fragment"],
                fidelity="ir-disassembly",
                priority=1,
                implementation="fixture DXBC disassembler",
                source_url="https://example.invalid/dxbc-disassembly",
            ),
            route_record(
                registry,
                route_id="test-portable-dxbc-assembly",
                tool_id="dxbc-native",
                operation="compile",
                input_format="dxbc-asm",
                output_format="dxbc",
                stages=["fragment"],
                fidelity="ir-assembly",
                priority=1,
                implementation="fixture DXBC assembler",
                source_url="https://example.invalid/dxbc-assembly",
            ),
        ]
        routes_path = self.root / "shader-toolchains.v1.json"
        routes_path.write_text(
            json.dumps(toolchain_document(self.config, routes, [])),
            encoding="utf-8",
        )
        return ShaderWorkspaceService(self.config, routes_path), routes_path

    def test_create_decompile_compile_records_auditable_candidate(self) -> None:
        service, workspace, created = self._create("workspace-good")
        self.assertEqual(created["baselineRawSha256"], self.key.raw_sha256)
        self.assertEqual(
            (workspace / "baseline" / "shader.bin").read_bytes(), self.blob
        )

        decompiled = service.decompile(workspace)
        source = Path(decompiled["sourcePath"]).read_text(encoding="utf-8")
        self.assertIn("fake deterministic decompile: fragment/main", source)
        self.assertTrue(Path(decompiled["stdoutPath"]).is_file())
        self.assertTrue(Path(decompiled["stderrPath"]).is_file())

        compiled = service.compile(workspace)
        self.assertEqual(compiled["revision"], 1)
        self.assertEqual(compiled["rawSha256"], self.key.raw_sha256)
        self.assertTrue(compiled["requiresReplayAbiValidation"])
        self.assertFalse(compiled["replayAbiValidated"])

        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["stage"], "fragment")
        self.assertEqual(manifest["entryPoint"], "main")
        self.assertEqual(manifest["revision"], 1)
        self.assertEqual(manifest["lastGood"]["revision"], 0)
        self.assertEqual(manifest["lastGood"]["rawSha256"], self.key.raw_sha256)
        self.assertTrue(manifest["latestCompiled"]["requiresReplayAbiValidation"])
        self.assertEqual(
            manifest["latestCompiled"]["validationScope"], "container-structure-only"
        )
        self.assertEqual(
            [item["status"] for item in manifest["history"]], ["succeeded", "succeeded"]
        )

    def test_status_exports_portable_dxbc_disassembly_as_display_evidence(
        self,
    ) -> None:
        service, unused_routes = self._dxbc_assembly_route_service()
        workspace = self.root / "workspace-portable-dxbc-disassembly"
        service.create(
            self.catalog,
            self.key.catalog_key,
            workspace,
            editable_format="dxbc-asm",
        )
        service.decompile(workspace)

        status = service.status(workspace)

        self.assertEqual(len(status["assemblyDisassemblyArtifacts"]), 1)
        artifact = status["assemblyDisassemblyArtifacts"][0]
        self.assertEqual(artifact["kind"], "native-disassembly")
        self.assertEqual(artifact["role"], "baseline")
        self.assertEqual(artifact["format"], "dxbc-asm")
        self.assertEqual(artifact["inputFormat"], "dxbc")
        self.assertEqual(artifact["inputRawSha256"], self.key.raw_sha256)
        self.assertTrue(artifact["physicalEvidence"])
        self.assertTrue(artifact["displayEvidenceOnly"])
        self.assertFalse(artifact["editableSource"])
        self.assertFalse(artifact["applyAuthorization"])
        self.assertNotEqual(Path(artifact["path"]), Path(status["source"]["path"]))
        self.assertIn(
            "dcl_output", Path(artifact["path"]).read_text(encoding="utf-8")
        )

        Path(artifact["path"]).write_text("ps_5_0\nret\n", encoding="utf-8")
        with self.assertRaisesRegex(
            ManifestValidationError, "physical/hash binding mismatch"
        ):
            service.status(workspace)

    def test_portable_dxbc_disassembly_rejects_changed_tool_configuration(
        self,
    ) -> None:
        service, routes_path = self._dxbc_assembly_route_service()
        workspace = self.root / "workspace-portable-dxbc-config-change"
        service.create(
            self.catalog,
            self.key.catalog_key,
            workspace,
            editable_format="dxbc-asm",
        )
        service.decompile(workspace)
        document = json.loads(self.config.read_text(encoding="utf-8"))
        document["tools"]["dxbc-native"]["decompile"]["timeoutSeconds"] = 11
        self.config.write_text(json.dumps(document), encoding="utf-8")

        with self.assertRaises((ToolConfigurationError, ManifestValidationError)):
            ShaderWorkspaceService(self.config, routes_path).status(workspace)
        self.assertEqual(len(list((workspace / "jobs").iterdir())), 2)

    def test_legacy_decompile_manifest_failure_rolls_back_source_and_history(
        self,
    ) -> None:
        for publish_before_raise in (False, True):
            with self.subTest(publish_before_raise=publish_before_raise):
                service, workspace, unused = self._create(
                    "workspace-decompile-manifest-%s"
                    % ("after" if publish_before_raise else "before")
                )
                manifest_path = workspace / "manifest.json"
                manifest_before = manifest_path.read_bytes()
                real_write = workspace_module._write_manifest_with_retry
                write_calls = 0

                def fail_manifest_write(path, manifest):
                    nonlocal write_calls
                    write_calls += 1
                    if publish_before_raise:
                        real_write(path, manifest)
                    raise OSError("injected manifest commit failure")

                with mock.patch.object(
                    workspace_module,
                    "_write_manifest_with_retry",
                    side_effect=fail_manifest_write,
                ):
                    for unused_attempt in range(2):
                        with self.assertRaisesRegex(OSError, "injected manifest"):
                            service.decompile(workspace)
                        self.assertEqual(
                            manifest_path.read_bytes(), manifest_before
                        )
                        self.assertFalse(
                            (workspace / "source" / "shader.hlsl").exists()
                        )
                        self.assertEqual(write_calls, unused_attempt + 1)

    def test_legacy_compile_manifest_failure_rolls_back_revision_and_history(
        self,
    ) -> None:
        for publish_before_raise in (False, True):
            with self.subTest(publish_before_raise=publish_before_raise):
                service, workspace, unused = self._create(
                    "workspace-compile-manifest-%s"
                    % ("after" if publish_before_raise else "before")
                )
                service.decompile(workspace)
                manifest_path = workspace / "manifest.json"
                manifest_before = manifest_path.read_bytes()
                real_write = workspace_module._write_manifest_with_retry
                write_calls = 0

                def fail_manifest_write(path, manifest):
                    nonlocal write_calls
                    write_calls += 1
                    if publish_before_raise:
                        real_write(path, manifest)
                    raise OSError("injected manifest commit failure")

                with mock.patch.object(
                    workspace_module,
                    "_write_manifest_with_retry",
                    side_effect=fail_manifest_write,
                ):
                    for unused_attempt in range(2):
                        with self.assertRaisesRegex(OSError, "injected manifest"):
                            service.compile(workspace)
                        self.assertEqual(
                            manifest_path.read_bytes(), manifest_before
                        )
                        self.assertEqual(
                            list((workspace / "revisions").iterdir()), []
                        )
                        self.assertEqual(write_calls, unused_attempt + 1)

    def test_zero_byte_and_non_utf8_decompiler_outputs_are_rejected(self) -> None:
        for index, (tool_id, message) in enumerate(
            (("zero", "empty"), ("nonutf8", "UTF-8"))
        ):
            with self.subTest(tool_id=tool_id):
                service, workspace, unused = self._create(
                    "workspace-bad-source-%d" % index, decompiler=tool_id
                )
                with self.assertRaisesRegex(ToolExecutionError, message):
                    service.decompile(workspace)
                self.assertFalse((workspace / "source" / "shader.hlsl").exists())
                manifest = json.loads(
                    (workspace / "manifest.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["history"][-1]["status"], "failed")
                job = workspace / manifest["history"][-1]["jobPath"]
                self.assertTrue((job / "stdout.log").is_file())
                self.assertTrue((job / "stderr.log").is_file())

    def test_representation_falls_back_to_nonempty_hash_bound_binary_view(self) -> None:
        service, workspace, unused = self._create(
            "workspace-binary-representation", decompiler="zero"
        )
        result = service.representation(workspace)

        self.assertEqual(result["lane"], "binary-only")
        self.assertEqual(result["kind"], "captured-binary-byte-view")
        self.assertFalse(result["compileEligible"])
        self.assertFalse(result["applyEligible"])
        self.assertFalse(result["abiCompatible"])
        self.assertFalse(result["semanticEquivalenceVerified"])
        artifact = Path(result["path"])
        self.assertTrue(artifact.is_file())
        self.assertGreater(artifact.stat().st_size, 0)
        self.assertIn(self.key.raw_sha256, artifact.read_text(encoding="utf-8"))
        self.assertTrue(
            any(
                item["code"] == "external-decompile-failed"
                for item in result["diagnostics"]
            )
        )

        status = service.status(workspace)
        self.assertFalse(status["source"]["present"])
        self.assertEqual(status["representation"]["lane"], "binary-only")
        fidelity = status["reconstructionFidelity"]
        self.assertFalse(fidelity["available"])
        self.assertIsNone(fidelity["codeSha256Equal"])
        self.assertEqual(fidelity["classification"], "not-measured")
        self.assertFalse(fidelity["semanticEquivalenceVerified"])
        with self.assertRaisesRegex(ToolExecutionError, "executable-editable"):
            service.compile(workspace)

    def test_renderdoc_disassembly_is_explanatory_ir_and_identity_checked(self) -> None:
        service, workspace, unused = self._create(
            "workspace-ir-representation", decompiler="zero"
        )
        binary = service.representation(workspace)
        result = service.representation(
            workspace,
            bridge_event_id=123,
            bridge_response={
                "shaderId": self.key.resource_id,
                "stage": self.key.stage,
                "entryPoint": self.key.entry_point,
                "rawSha256": self.key.raw_sha256,
                "disassembly": {
                    "DXBC": "dcl_output o0.xyzw\nmov o0.xyzw, l(1,1,1,1)\nret",
                    "Broken": "Error: unavailable",
                },
            },
        )
        self.assertEqual(binary["lane"], "binary-only")
        self.assertEqual(result["lane"], "explanatory-ir")
        self.assertEqual(result["kind"], "renderdoc-native-disassembly")
        self.assertFalse(result["compileEligible"])
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("mov o0.xyzw", text)
        self.assertNotIn("Error: unavailable", text)
        self.assertEqual(result["provenance"]["eventId"], 123)
        self.assertEqual(result["provenance"]["targets"], ["DXBC"])

        rejected = service.representation(
            workspace,
            bridge_event_id=124,
            bridge_response={
                "shaderId": self.key.resource_id + 1,
                "stage": self.key.stage,
                "entryPoint": self.key.entry_point,
                "rawSha256": self.key.raw_sha256,
                "disassembly": {"DXBC": "ret"},
            },
        )
        self.assertEqual(rejected["lane"], "binary-only")
        self.assertTrue(
            any(
                item["code"] == "renderdoc-disassembly-rejected"
                for item in rejected["diagnostics"]
            )
        )

    def test_explanatory_promotion_requires_ack_and_both_exact_hashes(self) -> None:
        service, workspace, unused = self._create(
            "workspace-promoted-explanation", decompiler="zero"
        )
        representation = service.representation(workspace)
        promoted_path = workspace / "explanation" / "promoted.hlsl"
        promoted_path.write_text(
            "float4 main() : SV_Target { return float4(0.5, 0.5, 0.5, 1); }\n",
            encoding="utf-8",
        )
        promoted_hash = hashlib.sha256(promoted_path.read_bytes()).hexdigest()

        with self.assertRaisesRegex(ToolExecutionError, "acknowledgement"):
            service.promote_explanatory_source(
                workspace,
                expected_representation_sha256=representation["sha256"],
                expected_source_sha256=promoted_hash,
                acknowledge_semantic_equivalence_unverified=False,
            )
        with self.assertRaisesRegex(ToolExecutionError, "representation changed"):
            service.promote_explanatory_source(
                workspace,
                expected_representation_sha256="0" * 64,
                expected_source_sha256=promoted_hash,
                acknowledge_semantic_equivalence_unverified=True,
            )
        with self.assertRaisesRegex(ToolExecutionError, "hash does not match"):
            service.promote_explanatory_source(
                workspace,
                expected_representation_sha256=representation["sha256"],
                expected_source_sha256="0" * 64,
                acknowledge_semantic_equivalence_unverified=True,
            )

        promoted = service.promote_explanatory_source(
            workspace,
            expected_representation_sha256=representation["sha256"],
            expected_source_sha256=promoted_hash,
            acknowledge_semantic_equivalence_unverified=True,
        )
        self.assertEqual(promoted["lane"], "executable-editable")
        self.assertFalse(promoted["semanticEquivalenceVerified"])
        status = service.status(workspace)
        self.assertEqual(status["source"]["origin"], "explicit-explanatory-promotion")
        self.assertEqual(status["representation"]["lane"], "executable-editable")
        self.assertFalse(status["representation"]["abiCompatible"])
        self.assertFalse(status["reconstructionFidelity"]["available"])
        self.assertEqual(
            status["reconstructionFidelity"]["classification"], "not-measured"
        )
        self.assertFalse(status["semanticEquivalenceVerified"])

        compiled = service.compile(workspace)
        self.assertFalse(compiled["replayAbiValidated"])
        compiled_status = service.status(workspace)
        self.assertFalse(compiled_status["latestCompiled"]["abiCompatible"])
        self.assertFalse(
            compiled_status["latestCompiled"]["semanticEquivalenceVerified"]
        )

    def test_invalid_compiler_container_does_not_advance_revision_or_last_good(
        self,
    ) -> None:
        service, workspace, unused = self._create(
            "workspace-invalid-compile", compiler="invalid-compiler"
        )
        service.decompile(workspace)
        with self.assertRaisesRegex(ToolExecutionError, "valid DXBC/DXIL"):
            service.compile(workspace)
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["revision"], 0)
        self.assertEqual(manifest["lastGood"]["revision"], 0)
        self.assertIsNone(manifest["latestCompiled"])
        self.assertEqual(manifest["history"][-1]["status"], "failed")

    def test_catalog_hash_tampering_is_rejected_without_creating_workspace(
        self,
    ) -> None:
        value = json.loads(self.catalog.read_text(encoding="utf-8"))
        value["shaders"][0]["rawSha256"] = "0" * 64
        self.catalog.write_text(json.dumps(value), encoding="utf-8")
        workspace = self.root / "must-not-exist"
        service = ShaderWorkspaceService(self.config)
        with self.assertRaises(CatalogValidationError):
            service.create(
                self.catalog,
                self.key.catalog_key,
                workspace,
                "good",
                "good",
            )
        self.assertFalse(workspace.exists())

    def test_catalog_allows_unselected_shader_without_raw_bytes(self) -> None:
        value = json.loads(self.catalog.read_text(encoding="utf-8"))
        missing = ShaderKey(92, "vertex", "main", "")
        value["shaders"].append(
            {
                "shaderKey": missing.catalog_key,
                "resourceId": missing.resource_id,
                "stage": missing.stage,
                "entryPoint": missing.entry_point,
                "rawPath": None,
                "rawSize": 0,
                "rawSha256": None,
            }
        )
        value["captureId"] = capture_identity(value, [self.key, missing])
        self.catalog.write_text(json.dumps(value), encoding="utf-8")

        service = ShaderWorkspaceService(self.config)
        workspace = self.root / "workspace-mixed-catalog"
        created = service.create(
            self.catalog,
            self.key.catalog_key,
            workspace,
            "good",
            "good",
        )
        self.assertEqual(created["baselineRawSha256"], self.key.raw_sha256)

    def test_executable_byte_tamper_is_rejected_before_launch(self) -> None:
        service, workspace, unused = self._create(
            "workspace-executable-tamper", decompiler="tamper"
        )
        with self.tamper_executable.open("ab") as stream:
            stream.write(b"tampered")
        with self.assertRaisesRegex(ToolExecutionError, "SHA-256 changed"):
            service.decompile(workspace)
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["history"][-1]["status"], "failed")
        self.assertIn("SHA-256 changed", manifest["history"][-1]["launchError"])

    def test_fixed_script_byte_tamper_is_rejected_before_launch(self) -> None:
        service, workspace, unused = self._create("workspace-script-tamper")
        with self.fake_tool.open("a", encoding="utf-8") as stream:
            stream.write("\n# tampered\n")
        with self.assertRaisesRegex(ToolExecutionError, "fixed file SHA-256 changed"):
            service.decompile(workspace)
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["history"][-1]["status"], "failed")
        self.assertIn(
            "fixed file SHA-256 changed", manifest["history"][-1]["launchError"]
        )

    def test_fixed_script_tamper_during_run_rejects_tool_output(self) -> None:
        service, workspace, unused = self._create(
            "workspace-script-runtime-tamper", decompiler="self-tamper"
        )
        with self.assertRaisesRegex(ToolExecutionError, "post-run integrity failure"):
            service.decompile(workspace)
        self.assertFalse((workspace / "source" / "shader.hlsl").exists())
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["history"][-1]["status"], "failed")

    def test_timeout_terminates_process_tree_and_never_waits_forever_on_pipes(
        self,
    ) -> None:
        service, workspace, unused = self._create(
            "workspace-timeout-tree", decompiler="timeout-tree"
        )
        started = time.monotonic()
        with self.assertRaisesRegex(ToolExecutionError, "timed out"):
            service.decompile(workspace)
        self.assertLess(time.monotonic() - started, 10)
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["history"][-1]["timedOut"])

    def test_decompile_never_overwrites_existing_edited_source(self) -> None:
        service, workspace, unused = self._create("workspace-source-preservation")
        result = service.decompile(workspace)
        source_path = Path(result["sourcePath"])
        edited = "// user edit that must survive\n"
        source_path.write_text(edited, encoding="utf-8")
        with self.assertRaisesRegex(ToolExecutionError, "refusing to overwrite"):
            service.decompile(workspace)
        self.assertEqual(source_path.read_text(encoding="utf-8"), edited)

    def test_workspace_file_lock_fails_closed_without_stealing_token(self) -> None:
        service, workspace, unused = self._create("workspace-file-lock")
        lock_path = workspace / ".workspace-operation.lock"
        lock_path.write_text("another-process-token\n", encoding="ascii")
        with self.assertRaisesRegex(ManifestValidationError, "never assumed stale"):
            service.decompile(workspace)
        self.assertEqual(
            lock_path.read_text(encoding="ascii"), "another-process-token\n"
        )

    def test_external_tool_receives_a_minimal_environment(self) -> None:
        service, workspace, unused = self._create(
            "workspace-minimal-env", decompiler="minimal-env"
        )
        with mock.patch.dict(
            os.environ,
            {
                "SHADER_WORKSPACE_SECRET": "must-not-leak",
                "HTTP_PROXY": "http://must-not-leak.invalid",
                "PATH": "must-not-leak",
            },
        ):
            result = service.decompile(workspace)
        source = Path(result["sourcePath"]).read_text(encoding="utf-8")
        self.assertIn("secret=<missing>", source)
        self.assertIn("path=<missing>", source)
        self.assertNotIn("must-not-leak", source)
        self.assertIn("temp=%s" % (Path(result["jobPath"]) / "temp"), source)

    def test_stdout_log_is_drained_but_bounded(self) -> None:
        service, workspace, unused = self._create(
            "workspace-bounded-log", decompiler="spam"
        )
        with mock.patch("src.shader_workspace.tools.MAX_LOG_BYTES", 1024):
            result = service.decompile(workspace)
        self.assertEqual(Path(result["stdoutPath"]).stat().st_size, 1024)
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["history"][-1]["stdoutTruncated"])

    def test_unknown_argument_placeholder_is_rejected_at_registry_load(self) -> None:
        self._write_config(extra_argument="{arbitrary_command}")
        with self.assertRaisesRegex(ToolConfigurationError, "unknown placeholder"):
            ShaderWorkspaceService(self.config)

    def test_compound_literal_script_path_must_be_hash_pinned(self) -> None:
        operation = {
            "args": [
                "--script=%s" % self.fake_tool.resolve(),
                "{input}",
                "{output}",
            ],
            "timeoutSeconds": 10,
        }
        value = {
            "schema": "renderdoc-ultra-shader-tools/v1",
            "schemaVersion": 1,
            "tools": {
                "unpinned": {
                    "executable": str(self.python_executable),
                    "executableSha256": self.python_sha256,
                    "fixedFileHashes": {},
                    "decompile": operation,
                    "compile": operation,
                }
            },
        }
        self.config.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ToolConfigurationError, "must be pinned"):
            ShaderWorkspaceService(self.config)

    def test_timeout_must_be_finite(self) -> None:
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["tools"]["good"]["decompile"]["timeoutSeconds"] = float("nan")
        self.config.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(
            ToolConfigurationError, "timeoutSeconds must be between 1 and 600"
        ):
            ShaderWorkspaceService(self.config)

    def test_compact_inline_python_code_and_module_are_rejected(self) -> None:
        for argument in ("-cprint('unsafe')", "-msuspicious_module"):
            with self.subTest(argument=argument):
                value = json.loads(self.config.read_text(encoding="utf-8"))
                value["tools"]["good"]["decompile"]["args"] = [
                    argument,
                    "{input}",
                    "{output}",
                ]
                self.config.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(
                    ToolConfigurationError, "pinned script file"
                ):
                    ShaderWorkspaceService(self.config)
                self._write_config()

    def test_batch_file_cannot_be_registered_as_an_executable(self) -> None:
        batch = self.root / "unsafe.cmd"
        batch.write_text("@echo off\r\n", encoding="ascii")
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["tools"]["good"]["executable"] = str(batch.resolve())
        value["tools"]["good"]["executableSha256"] = hashlib.sha256(
            batch.read_bytes()
        ).hexdigest()
        self.config.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ToolConfigurationError, "command shell"):
            ShaderWorkspaceService(self.config)

    def test_status_decompile_and_compile_require_the_original_catalog(self) -> None:
        for operation_name in ("status", "decompile", "compile"):
            with self.subTest(operation=operation_name):
                service, workspace, unused = self._create(
                    "workspace-missing-catalog-%s" % operation_name
                )
                if operation_name == "compile":
                    service.decompile(workspace)
                self.catalog.unlink()
                with self.assertRaisesRegex(
                    ManifestValidationError,
                    "catalog provenance cannot be verified",
                ):
                    getattr(service, operation_name)(workspace)
                self.catalog = self._write_catalog(self.blob)

    def test_workspace_binds_exact_catalog_revision_and_legacy_manifest_loads(
        self,
    ) -> None:
        service, workspace, created = self._create(
            "workspace-catalog-revision-bound"
        )
        expected = hashlib.sha256(self.catalog.read_bytes()).hexdigest()
        manifest_path = workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["capture"]["catalogSha256"], expected)
        status = service.status(workspace)
        self.assertEqual(status["capture"]["catalogSha256"], expected)
        self.assertEqual(created["captureId"], status["capture"]["captureId"])

        # A v1 workspace created before exact revision binding remains readable;
        # it still receives the pre-existing catalog/capture/ABI/blob checks.
        del manifest["capture"]["catalogSha256"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        legacy_status = service.status(workspace)
        self.assertNotIn("catalogSha256", legacy_status["capture"])

    def test_catalog_revision_tampering_is_rejected_after_workspace_creation(
        self,
    ) -> None:
        service, workspace, unused = self._create(
            "workspace-catalog-revision-tamper"
        )
        catalog = json.loads(self.catalog.read_text(encoding="utf-8"))
        # This metadata is outside captureId and the selected Shader bytes, so
        # only the exact catalog revision binding can detect the rewrite.
        catalog["exportDiagnostics"] = {"reflectionRevision": 2}
        self.catalog.write_text(json.dumps(catalog), encoding="utf-8")

        with self.assertRaisesRegex(
            ManifestValidationError, "catalogSha256"
        ):
            service.status(workspace)

    def test_manifest_capture_and_shader_identity_tampering_fails_closed(
        self,
    ) -> None:
        cases = (
            ("capture-id", "captureId", "0" * 64),
            ("capture-path", "capturePath", "D:/captures/other-frame.rdc"),
            ("api", "api", "Vulkan"),
            ("resource-id", "resourceId", self.key.resource_id + 1),
            ("stage", "stage", "vertex"),
            ("entry-point", "entryPoint", "different_main"),
            ("raw-sha256", "rawSha256", "0" * 64),
        )
        for label, field, replacement in cases:
            with self.subTest(field=field):
                service, workspace, unused = self._create(
                    "workspace-manifest-tamper-%s" % label
                )
                manifest_path = workspace / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if field in {"captureId", "capturePath", "api"}:
                    manifest["capture"][field] = replacement
                else:
                    manifest["shader"][field] = replacement
                    if field == "stage":
                        manifest["stage"] = replacement
                    elif field == "entryPoint":
                        manifest["entryPoint"] = replacement
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(ManifestValidationError):
                    service.status(workspace)

    def test_internally_valid_rewritten_catalog_does_not_rebind_workspace(
        self,
    ) -> None:
        service, workspace, unused = self._create("workspace-catalog-rewrite")
        catalog = json.loads(self.catalog.read_text(encoding="utf-8"))
        catalog["capturePath"] = "D:/captures/different-frame.rdc"
        catalog["captureId"] = capture_identity(catalog, [self.key])
        self.catalog.write_text(json.dumps(catalog), encoding="utf-8")

        with self.assertRaisesRegex(ManifestValidationError, "catalogSha256"):
            service.status(workspace)

    def test_catalog_blob_tampering_is_rejected_after_workspace_creation(
        self,
    ) -> None:
        service, workspace, unused = self._create("workspace-catalog-blob-tamper")
        raw_path = self.catalog_dir / "blobs" / (self.key.raw_sha256 + ".bin")
        with raw_path.open("ab") as stream:
            stream.write(b"tampered")

        with self.assertRaisesRegex(
            ManifestValidationError, "catalog provenance cannot be verified"
        ):
            service.status(workspace)

    def test_tool_definition_change_requires_a_new_workspace(self) -> None:
        unused_service, workspace, unused = self._create("workspace-config-bound")
        original = json.loads(self.config.read_text(encoding="utf-8"))
        original["tools"]["good"]["decompile"]["timeoutSeconds"] = 11
        self.config.write_text(json.dumps(original), encoding="utf-8")
        changed_service = ShaderWorkspaceService(self.config)
        with self.assertRaisesRegex(ToolConfigurationError, "configuration changed"):
            changed_service.decompile(workspace)

    def test_target_profile_is_token_derived_and_manifest_tamper_fails(self) -> None:
        service, workspace, created = self._create("workspace-target-profile")
        self.assertEqual(created["targetProfile"]["profile"], "ps_5_0")
        manifest_path = workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["targetProfile"]["profile"] = "vs_5_0"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ManifestValidationError, "targetProfile"):
            service.status(workspace)

    def test_workspace_rejects_compiler_that_does_not_consume_exact_profile(self) -> None:
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["tools"]["good"]["compile"]["args"].remove("{target_profile}")
        self.config.write_text(json.dumps(value), encoding="utf-8")
        service = ShaderWorkspaceService(self.config)
        with self.assertRaisesRegex(ToolConfigurationError, "target_profile"):
            service.create(
                self.catalog,
                self.key.catalog_key,
                self.root / "must-not-create-with-guessed-profile",
                "good",
                "good",
            )


if __name__ == "__main__":
    unittest.main()
