from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from src.shader_workspace import (
    ToolConfigurationError,
    ToolExecutionError,
    ToolchainCatalog,
    ToolchainRoutingError,
    detect_shader_format,
    generate_official_toolchain_configs,
    extract_target_profile,
    validate_target_profile,
)
from src.shader_workspace.io import atomic_write_json
from src.shader_workspace.adapters.spirv_cross_adapter import (
    derive_spirv_cross_arguments,
)
from src.shader_workspace.toolchains import route_record, toolchain_document, validator_record
from src.shader_workspace.tools import ToolRegistry, run_operation


def make_container_from_chunks(chunks: list[tuple[bytes, bytes]]) -> bytes:
    header_size = 32 + 4 * len(chunks)
    offsets: list[int] = []
    encoded_chunks: list[bytes] = []
    cursor = header_size
    for fourcc, payload in chunks:
        encoded = fourcc + struct.pack("<I", len(payload)) + payload
        offsets.append(cursor)
        encoded_chunks.append(encoded)
        cursor += len(encoded)
    return (
        b"DXBC"
        + b"\x00" * 16
        + struct.pack("<I", 1)
        + struct.pack("<I", cursor)
        + struct.pack("<I", len(chunks))
        + b"".join(struct.pack("<I", value) for value in offsets)
        + b"".join(encoded_chunks)
    )


def make_container(fourcc: bytes = b"SHDR", code: bytes | None = None) -> bytes:
    if code is None:
        code = (
            struct.pack("<I", 0x00000060) + b"instructions"
            if fourcc == b"DXIL"
            else make_dxbc_program(0x00000050, b"instructions")
        )
    return make_container_from_chunks([(fourcc, code)])


def make_dxbc_program(program_version: int, instructions: bytes = b"\x00" * 4) -> bytes:
    padding = b"\x00" * (-len(instructions) % 4)
    body = instructions + padding
    return struct.pack("<2I", program_version, 2 + len(body) // 4) + body


def make_dxil_program(program_version: int, bitcode: bytes = b"BC\xc0\xdefixture") -> bytes:
    bitcode_offset = 16  # relative to DxilBitcodeHeader
    unpadded_size = 24 + len(bitcode)
    padded_size = (unpadded_size + 3) & ~3
    return (
        struct.pack(
            "<6I",
            program_version,
            padded_size // 4,
            0x4C495844,
            0x00000100,
            bitcode_offset,
            len(bitcode),
        )
        + bitcode
        + b"\x00" * (padded_size - unpadded_size)
    )


def replace_dxil_header_field(container: bytes, index: int, value: int) -> bytes:
    changed = bytearray(container)
    chunk_offset = struct.unpack_from("<I", changed, 32)[0]
    struct.pack_into("<I", changed, chunk_offset + 8 + index * 4, value)
    return bytes(changed)


FAKE_TOOL = r'''from pathlib import Path
import shutil
import sys

mode = sys.argv[1]
input_path = Path(sys.argv[2])
output_path = Path(sys.argv[3]) if len(sys.argv) > 3 else None
if mode == "decompile-hlsl":
    output_path.write_text(
        "float4 main() : SV_Target { return float4(1, 0, 0, 1); }\n",
        encoding="utf-8",
    )
elif mode == "compile-baseline":
    shutil.copyfile(Path(sys.argv[4]), output_path)
elif mode == "validate":
    if input_path.stat().st_size <= 0:
        raise SystemExit(2)
else:
    raise SystemExit("unknown mode")
'''


class ShaderToolchainTests(unittest.TestCase):
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
        self.script = self.root / "fake_tool.py"
        self.script.write_text(FAKE_TOOL, encoding="utf-8")
        self.script_hash = hashlib.sha256(self.script.read_bytes()).hexdigest()
        self.python = Path(sys.executable).resolve()
        self.python_hash = hashlib.sha256(self.python.read_bytes()).hexdigest()
        self.tools_path = self.root / "shader-tools.v1.json"
        self.routes_path = self.root / "shader-toolchains.v1.json"
        self._write_tools()
        self._write_routes()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_tools(self) -> None:
        base = {
            "executable": str(self.python),
            "executableSha256": self.python_hash,
            "fixedFileHashes": {str(self.script.resolve()): self.script_hash},
        }
        atomic_write_json(
            self.tools_path,
            {
                "schema": "renderdoc-ultra-shader-tools/v1",
                "schemaVersion": 1,
                "tools": {
                    "fake-decompiler": {
                        **base,
                        "decompile": {
                            "args": [
                                str(self.script.resolve()),
                                "decompile-hlsl",
                                "{input}",
                                "{output}",
                            ],
                            "timeoutSeconds": 10,
                        },
                    },
                    "fake-compiler": {
                        **base,
                        "compile": {
                            "args": [
                                str(self.script.resolve()),
                                "compile-baseline",
                                "{input}",
                                "{output}",
                                "{baseline}",
                                "{target_profile}",
                            ],
                            "timeoutSeconds": 10,
                        },
                    },
                    "fake-validator": {
                        **base,
                        "validate": {
                            "args": [
                                str(self.script.resolve()),
                                "validate",
                                "{input}",
                            ],
                            "timeoutSeconds": 10,
                        },
                    },
                },
            },
        )

    def _write_routes(self, ambiguous: bool = False) -> None:
        registry = ToolRegistry(self.tools_path)
        routes = [
            route_record(
                registry,
                route_id="fake-dxbc-hlsl",
                tool_id="fake-decompiler",
                operation="decompile",
                input_format="dxbc",
                output_format="hlsl",
                stages=["fragment"],
                fidelity="reconstructed-source",
                priority=100,
                implementation="test decompiler",
                source_url="https://example.invalid/decompiler",
                caveats=["Test reconstruction is not original source."],
            ),
            route_record(
                registry,
                route_id="fake-hlsl-dxbc",
                tool_id="fake-compiler",
                operation="compile",
                input_format="hlsl",
                output_format="dxbc",
                stages=["fragment"],
                fidelity="native-compile",
                priority=10,
                implementation="test compiler",
                source_url="https://example.invalid/compiler",
            ),
        ]
        if ambiguous:
            duplicate = dict(routes[0])
            duplicate["routeId"] = "fake-dxbc-hlsl-equal"
            routes.append(duplicate)
        validators = [
            validator_record(
                registry,
                validator_id="fake-dxbc-validator",
                tool_id="fake-validator",
                input_format="dxbc",
                stages=["fragment"],
                priority=10,
                implementation="test validator",
                source_url="https://example.invalid/validator",
            )
        ]
        atomic_write_json(
            self.routes_path, toolchain_document(self.tools_path, routes, validators)
        )

    def test_detects_containers_ir_and_conservative_source_languages(self) -> None:
        self.assertEqual(detect_shader_format(make_container()).format, "dxbc")
        self.assertEqual(detect_shader_format(make_container(b"DXIL")).format, "dxil")
        spirv = struct.pack("<5I", 0x07230203, 0x00010600, 0, 1, 0)
        self.assertEqual(detect_shader_format(spirv).format, "spirv")
        opengl_spirv = detect_shader_format(spirv, "OpenGLSPIRV")
        self.assertEqual(opengl_spirv.format, "opengl-spirv")
        self.assertEqual(
            opengl_spirv.confidence, "declared-semantic-environment"
        )
        self.assertEqual(
            detect_shader_format(b"; SPIR-V\nOpCapability Shader\nOpMemoryModel Logical GLSL450\n").format,
            "spirv-asm",
        )
        self.assertEqual(
            detect_shader_format(
                b"; SPIR-V\nOpCapability Shader\nOpMemoryModel Logical GLSL450\n",
                "OpenGLSPIRVAsm",
            ).format,
            "opengl-spirv-asm",
        )
        self.assertEqual(
            detect_shader_format(b"float4 main() : SV_Target { return 1; }\n").format,
            "hlsl",
        )
        self.assertEqual(
            detect_shader_format(b"#version 450\nvoid main(){gl_Position=vec4(0);}\n").format,
            "glsl",
        )
        essl_100 = (
            b"#version 100\nprecision mediump float;\n"
            b"void main(){gl_FragColor=vec4(1.0);}\n"
        )
        self.assertEqual(detect_shader_format(essl_100).format, "essl")
        self.assertEqual(detect_shader_format(essl_100, "essl").format, "essl")
        self.assertIsNone(detect_shader_format(b"void main() {}\n").format)
        with self.assertRaisesRegex(ToolchainRoutingError, "contradicts"):
            detect_shader_format(make_container(), "spirv")

    def test_all_high_level_compile_paths_reject_untracked_dependencies(self) -> None:
        registry = ToolRegistry(self.tools_path)
        operation = registry.operation("fake-compiler", "compile")
        baseline = self.root / "baseline.dxbc"
        baseline.write_bytes(make_container())
        for index, (suffix, source) in enumerate(
            (
                (".hlsl", '#include "outside.hlsli"\nfloat4 main():SV_Target{return 1;}\n'),
                (".glsl", "#line 99\n#version 450\nvoid main(){}\n"),
                (".slang", "import external_module;\nfloat4 main(){return 1;}\n"),
            )
        ):
            source_path = self.root / ("unsafe-%d%s" % (index, suffix))
            source_path.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(
                ToolExecutionError, "hash-bound dependency|Slang import"
            ):
                run_operation(
                    operation,
                    {
                        "input": str(source_path),
                        "output": str(self.root / ("unsafe-%d.dxbc" % index)),
                        "baseline": str(baseline),
                        "job_dir": str(self.root / ("job-%d" % index)),
                        "workspace_dir": str(self.root),
                        "stage": "fragment",
                        "entry_point": "main",
                        "revision": "1",
                        "capture_id": "capture",
                        "raw_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
                        "target_profile": "ps_5_0",
                        "target_api": "d3d11",
                    },
                    self.root / ("job-%d" % index),
                )
            self.assertFalse((self.root / ("job-%d" % index)).exists())

    def test_exact_target_profile_is_read_from_compiled_tokens_never_stage_guessed(self) -> None:
        cases = (
            (0x00010040, "vs_4_0", "vertex"),
            (0x00000041, "ps_4_1", "fragment"),
            (0x00050050, "cs_5_0", "compute"),
            (0x00020051, "gs_5_1", "geometry"),
        )
        for token, expected, stage in cases:
            with self.subTest(expected=expected):
                profile = extract_target_profile(
                    make_container(code=make_dxbc_program(token, b"code")), stage
                )
                self.assertEqual(profile.profile, expected)
                self.assertFalse(profile.as_dict()["guessed"])
        dxil = extract_target_profile(
            make_container(b"DXIL", make_dxil_program(0x00000067)), "fragment"
        )
        self.assertEqual(dxil.profile, "ps_6_7")
        task = extract_target_profile(
            make_container(b"DXIL", make_dxil_program(0x000E0067)),
            "amplification",
        )
        self.assertEqual(task.profile, "as_6_7")
        self.assertEqual(task.stage, "task")
        with self.assertRaisesRegex(ToolchainRoutingError, "contradicts"):
            extract_target_profile(make_container(), "vertex")
        library = extract_target_profile(
            make_container(b"DXIL", make_dxil_program(0x00060063)),
            "library",
        )
        self.assertEqual(library.profile, "lib_6_3")
        self.assertEqual(library.stage, "library")
        # A DXR capture selects one exported stage independently from the
        # container-level Library ProgramType.  The physical profile remains
        # lib_6_3 and is never relabelled as a single-entry target.
        exported_raygen = extract_target_profile(
            make_container(b"DXIL", make_dxil_program(0x00060063)),
            "raygen",
        )
        self.assertEqual(exported_raygen.profile, "lib_6_3")
        self.assertEqual(exported_raygen.stage, "library")
        raygen_program = extract_target_profile(
            make_container(b"DXIL", make_dxil_program(0x00070065)),
            "raygen",
        )
        self.assertEqual(raygen_program.profile, "lib_6_5")
        self.assertEqual(raygen_program.stage, "raygen")
        self.assertEqual(validate_target_profile("lib_6_3", "miss"), "lib_6_3")
        with self.assertRaisesRegex(ToolchainRoutingError, "contradicts"):
            validate_target_profile("lib_6_3", "compute")

    def test_dxbc_profile_requires_complete_exact_tokenized_program_header(self) -> None:
        valid = make_container(code=make_dxbc_program(0x00010040, b"code"))
        shex = extract_target_profile(
            make_container(b"SHEX", make_dxbc_program(0x00050050)), "compute"
        )
        self.assertEqual(shex.profile, "cs_5_0")

        chunk_offset = struct.unpack_from("<I", valid, 32)[0]
        short = make_container(code=struct.pack("<I", 0x00010040))
        unaligned = make_container(code=make_dxbc_program(0x00010040) + b"x")
        too_short_length = bytearray(valid)
        struct.pack_into("<I", too_short_length, chunk_offset + 12, 1)
        contradictory_length = bytearray(valid)
        struct.pack_into("<I", contradictory_length, chunk_offset + 12, 2)
        oversized_length = bytearray(valid)
        struct.pack_into("<I", oversized_length, chunk_offset + 12, 0xFFFFFFFF)
        reserved_version = bytearray(valid)
        struct.pack_into("<I", reserved_version, chunk_offset + 8, 0x00010140)

        malformed = (
            ("missing Length", short, "too small"),
            ("non-DWORD chunk", unaligned, "not DWORD-aligned"),
            ("Length below header", bytes(too_short_length), "Length.*smaller"),
            ("contradictory Length", bytes(contradictory_length), "Length.*match"),
            ("hostile Length", bytes(oversized_length), "Length.*match"),
            ("reserved version bits", bytes(reserved_version), "version.*reserved"),
        )
        for label, fixture, message in malformed:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ToolchainRoutingError, message):
                    extract_target_profile(fixture, "vertex")

    def test_dxil_profile_requires_a_complete_consistent_program_header(self) -> None:
        valid = make_container(b"DXIL", make_dxil_program(0x00000067))
        malformed = (
            (
                "truncated headers",
                make_container(b"DXIL", b"\x00" * 23),
                "too small",
            ),
            (
                "reserved ProgramVersion bits",
                replace_dxil_header_field(valid, 0, 0x00000167),
                "ProgramVersion.*reserved",
            ),
            (
                "undersized SizeInUint32",
                replace_dxil_header_field(valid, 1, 5),
                "SizeInUint32.*smaller",
            ),
            (
                "mismatched SizeInUint32",
                replace_dxil_header_field(valid, 1, 6),
                "SizeInUint32.*match",
            ),
            (
                "wrong DXIL magic",
                replace_dxil_header_field(valid, 2, 0x41414141),
                "invalid DXIL magic",
            ),
            (
                "bitcode overlaps header",
                replace_dxil_header_field(valid, 4, 12),
                "BitcodeOffset overlaps",
            ),
            (
                "hostile offset",
                replace_dxil_header_field(valid, 4, 0xFFFFFFFC),
                "BitcodeOffset exceeds",
            ),
            (
                "empty bitcode",
                replace_dxil_header_field(valid, 5, 0),
                "BitcodeSize.*non-zero",
            ),
            (
                "bitcode crosses part end",
                replace_dxil_header_field(valid, 5, 0xFFFFFFFF),
                "bitcode range exceeds",
            ),
            (
                "bitcode and padded program disagree",
                replace_dxil_header_field(valid, 5, len(b"BC\xc0\xdefixture") - 4),
                "inconsistent",
            ),
        )
        for label, fixture, message in malformed:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ToolchainRoutingError, message):
                    extract_target_profile(fixture, "fragment")

    def test_downlevel_aon9_profile_fails_closed_instead_of_guessing_sm40(self) -> None:
        level_9 = make_container_from_chunks(
            [
                (b"SHDR", make_dxbc_program(0x00010040, b"code")),
                (b"Aon9", b"undocumented-downlevel-metadata"),
            ]
        )
        with self.assertRaisesRegex(ToolchainRoutingError, "10Level9/downlevel.*not be guessed"):
            extract_target_profile(level_9, "vertex")

    def test_auto_route_is_explainable_and_fails_closed_on_equal_choices(self) -> None:
        catalog = ToolchainCatalog(self.tools_path, self.routes_path)
        selected = catalog.select_round_trip(
            "dxbc",
            "fragment",
            editable_format="hlsl",
            capture_encoding="dxbc",
            target_api="d3d11",
        )
        self.assertEqual(selected["decompilerToolIds"], ["fake-decompiler"])
        self.assertEqual(selected["compilerToolIds"], ["fake-compiler"])
        self.assertTrue(selected["containsReconstructedSource"])
        self.assertFalse(selected["semanticEquivalenceVerified"])
        self.assertIn("No route can guarantee", selected["guarantee"])

        self._write_routes(ambiguous=True)
        ambiguous = ToolchainCatalog(self.tools_path, self.routes_path)
        with self.assertRaisesRegex(ToolchainRoutingError, "ambiguous"):
            ambiguous.select_round_trip("dxbc", "fragment", target_api="d3d11")

        recovery = ambiguous.plan_recovery(
            "dxbc", "fragment", editable_format="hlsl", target_api="d3d11"
        )
        self.assertEqual("round-trip", recovery["mode"])
        self.assertEqual(
            [["fake-dxbc-hlsl"], ["fake-dxbc-hlsl-equal"]],
            recovery["decompileCandidates"],
        )
        self.assertEqual(["fake-dxbc-hlsl"], recovery["decompileRouteIds"])
        self.assertNotIn("allowedActions", recovery)
        diagnostic = next(
            row
            for row in ambiguous.diagnostic_matrix("fragment")["matrix"]
            if row["inputFormat"] == "dxbc" and row["outputFormat"] == "hlsl"
        )
        self.assertTrue(diagnostic["roundTripReady"])
        self.assertFalse(diagnostic["automaticRoundTripReady"])
        self.assertFalse(diagnostic["editable"])
        self.assertIn("ambiguous", diagnostic["unsupportedReason"])
        hlsl_capability = next(
            item
            for item in ambiguous.diagnostic_matrix("fragment")[
                "formatCapabilities"
            ]
            if item["format"] == "hlsl"
        )
        self.assertFalse(hlsl_capability["editable"])
        self.assertIn("uniquely selectable", hlsl_capability["unsupportedReason"])

    def test_recovery_plan_keeps_compile_only_and_evidence_only_lanes(self) -> None:
        value = json.loads(self.routes_path.read_text(encoding="utf-8"))
        value["routes"] = [
            route
            for route in value["routes"]
            if route["routeId"] != "fake-dxbc-hlsl"
        ]
        atomic_write_json(self.routes_path, value)
        compile_only = ToolchainCatalog(
            self.tools_path, self.routes_path
        ).plan_recovery("dxbc", "fragment", target_api="d3d11")
        self.assertEqual("compile-only-recovery", compile_only["mode"])
        self.assertEqual([], compile_only["decompileRouteIds"])
        self.assertEqual(["fake-hlsl-dxbc"], compile_only["compileRouteIds"])
        self.assertNotIn("aiPolicy", compile_only)
        self.assertFalse(compile_only["directReplayApplyAuthorized"])

        value["routes"] = []
        atomic_write_json(self.routes_path, value)
        evidence_only = ToolchainCatalog(
            self.tools_path, self.routes_path
        ).plan_recovery("dxbc", "fragment", target_api="d3d11")
        self.assertEqual("evidence-only", evidence_only["mode"])
        self.assertEqual([], evidence_only["compileRouteIds"])
        self.assertNotIn("aiPolicy", evidence_only)
        self.assertNotIn("allowedActions", evidence_only)

    def test_recovery_plan_enumerates_alternate_compilers_without_retry_policy(self) -> None:
        value = json.loads(self.routes_path.read_text(encoding="utf-8"))
        compiler = next(
            route
            for route in value["routes"]
            if route["routeId"] == "fake-hlsl-dxbc"
        )
        alternate = json.loads(json.dumps(compiler))
        alternate["routeId"] = "fake-hlsl-dxbc-alternate"
        alternate["priority"] = 20
        value["routes"].append(alternate)
        atomic_write_json(self.routes_path, value)

        plan = ToolchainCatalog(
            self.tools_path, self.routes_path
        ).plan_recovery("dxbc", "fragment", target_api="d3d11")
        self.assertEqual(plan["decompileCandidates"], [["fake-dxbc-hlsl"]])
        self.assertEqual(plan["compileCandidates"], [
            ["fake-hlsl-dxbc"], ["fake-hlsl-dxbc-alternate"]
        ])
        self.assertNotIn("allowedActions", plan)

    def test_route_catalog_is_bound_to_exact_registry_and_operation_digest(self) -> None:
        value = json.loads(self.routes_path.read_text(encoding="utf-8"))
        value["toolRegistrySha256"] = "0" * 64
        atomic_write_json(self.routes_path, value)
        with self.assertRaisesRegex(ToolConfigurationError, "toolRegistrySha256"):
            ToolchainCatalog(self.tools_path, self.routes_path)

        self._write_routes()
        value = json.loads(self.routes_path.read_text(encoding="utf-8"))
        value["routes"][0]["toolConfigDigest"] = "0" * 64
        atomic_write_json(self.routes_path, value)
        with self.assertRaisesRegex(ToolConfigurationError, "toolConfigDigest"):
            ToolchainCatalog(self.tools_path, self.routes_path)

    def test_diagnostic_matrix_rehashes_tools_without_executing_them(self) -> None:
        catalog = ToolchainCatalog(self.tools_path, self.routes_path)
        before = catalog.diagnostic_matrix("fragment")
        dxbc_hlsl = next(
            row
            for row in before["matrix"]
            if row["inputFormat"] == "dxbc" and row["outputFormat"] == "hlsl"
        )
        self.assertEqual(dxbc_hlsl["status"], "ready")
        self.assertTrue(dxbc_hlsl["recognized"])
        self.assertTrue(dxbc_hlsl["analyzable"])
        self.assertTrue(dxbc_hlsl["explanatory"])
        self.assertTrue(dxbc_hlsl["editable"])
        self.assertTrue(dxbc_hlsl["compilable"])
        self.assertFalse(dxbc_hlsl["replayAbiValidated"])
        self.assertFalse(dxbc_hlsl["replayReplaceable"])
        slang = next(
            item
            for item in before["formatCapabilities"]
            if item["format"] == "slang"
        )
        self.assertTrue(slang["recognized"])
        self.assertFalse(slang["analyzable"])
        self.assertFalse(slang["explanatory"])
        self.assertFalse(slang["editable"])
        self.assertFalse(slang["compilable"])
        self.assertIn("not an end-to-end", slang["unsupportedReason"])
        ray = catalog.diagnostic_matrix("raygen")
        self.assertTrue(
            all(not row["editable"] for row in ray["matrix"])
        )
        self.assertTrue(
            all(not row["replayReplaceable"] for row in ray["matrix"])
        )
        self.assertTrue(
            all(
                "No provider declares" in row["unsupportedReason"]
                for row in ray["matrix"]
            )
        )
        self.assertTrue(
            all(not row["replayStageSupported"] for row in ray["matrix"])
        )
        self.assertTrue(
            all("SBT" in row["replayUnsupportedReason"] for row in ray["matrix"])
        )
        with self.script.open("a", encoding="utf-8") as stream:
            stream.write("\n# tampered\n")
        after = catalog.diagnostic_matrix("fragment")
        dxbc_hlsl = next(
            row
            for row in after["matrix"]
            if row["inputFormat"] == "dxbc" and row["outputFormat"] == "hlsl"
        )
        self.assertEqual(dxbc_hlsl["status"], "integrity-failed")
        self.assertFalse(next(iter(after["operations"]))["available"])

    def test_round_trip_smoke_records_format_evidence_without_claiming_abi(self) -> None:
        shader = self.root / "captured.dxbc"
        shader.write_bytes(make_container())
        catalog = ToolchainCatalog(self.tools_path, self.routes_path)
        report = catalog.round_trip_smoke(
            shader,
            "hlsl",
            self.root / "smoke",
            "fragment",
        )
        self.assertTrue(report["formatSmokePassed"])
        self.assertTrue(report["byteIdentical"])
        self.assertFalse(report["semanticEquivalenceVerified"])
        self.assertFalse(report["replayAbiValidated"])
        self.assertTrue(report["externalValidationConfigured"])
        self.assertTrue(report["externalValidationPassed"])
        self.assertEqual(len(report["validatorJobs"]), 1)
        self.assertEqual(len(report["jobs"]), 2)
        self.assertEqual(report["targetProfile"], "ps_5_0")
        self.assertTrue((self.root / "smoke" / "smoke-report.json").is_file())

    def test_official_config_generator_hashes_explicit_paths_and_never_downloads(self) -> None:
        generated_root = self.root / "generated"
        generated_root.mkdir()
        spirv_dis = generated_root / "spirv-dis.exe"
        spirv_as = generated_root / "spirv-as.exe"
        spirv_val = generated_root / "spirv-val.exe"
        spirv_dis.write_bytes(b"explicit fake binary A")
        spirv_as.write_bytes(b"explicit fake binary B")
        spirv_val.write_bytes(b"explicit fake binary C")
        result = generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            spirv_dis=spirv_dis.resolve(),
            spirv_as=spirv_as.resolve(),
            spirv_val=spirv_val.resolve(),
            spirv_target_env="vulkan1.1",
        )
        self.assertEqual(result["toolCount"], 3)
        self.assertEqual(result["routeCount"], 2)
        self.assertEqual(result["validatorCount"], 1)
        self.assertFalse(result["binaryPathsWereDiscovered"])
        self.assertFalse(result["downloadsPerformed"])
        self.assertEqual(result["spirvTargetEnv"], "vulkan1.1")
        tools = json.loads((generated_root / "tools.json").read_text(encoding="utf-8"))
        self.assertEqual(
            tools["tools"]["spirv-tools-val"]["validate"]["args"],
            ["--target-env", "vulkan1.1", "{input}"],
        )
        catalog = ToolchainCatalog(
            generated_root / "tools.json", generated_root / "routes.json"
        )
        with self.assertRaisesRegex(
            ToolchainRoutingError, "cannot be derived from the module"
        ):
            catalog.select_round_trip(
                "spirv", "fragment", editable_format="spirv-asm"
            )
        selected = catalog.select_round_trip(
            "spirv",
            "fragment",
            editable_format="spirv-asm",
            target_api="vulkan",
        )
        self.assertEqual(selected["targetApi"], "vulkan")
        self.assertTrue(selected["strictNativeIrValidationRequired"])
        self.assertEqual(
            [
                item["validatorId"]
                for item in selected["requiredValidators"]
            ],
            ["spirv-tools-validate"],
        )
        automatic = catalog.plan_automatic_recovery(
            "spirv", "fragment", target_api="vulkan"
        )
        self.assertEqual(automatic["editableFormat"], "spirv-asm")
        self.assertTrue(automatic["strictNativeIrValidationRequired"])
        self.assertEqual(
            [
                item["validatorId"]
                for item in automatic["requiredValidators"]
            ],
            ["spirv-tools-validate"],
        )
        with self.assertRaisesRegex(ToolConfigurationError, "overwrite"):
            generate_official_toolchain_configs(
                generated_root / "tools.json",
                generated_root / "routes.json",
                spirv_dis=spirv_dis.resolve(),
            )

    def test_spirv_environment_is_explicit_and_invalid_values_fail_closed(self) -> None:
        generated_root = self.root / "spirv-env"
        generated_root.mkdir()
        spirv_val = generated_root / "spirv-val.exe"
        glslang = generated_root / "glslangValidator.exe"
        spirv_val.write_bytes(b"fake spirv-val")
        glslang.write_bytes(b"fake glslang")
        for supplied in ({"spirv_val": spirv_val}, {"glslang": glslang}):
            with self.assertRaisesRegex(ToolConfigurationError, "will not be guessed"):
                generate_official_toolchain_configs(
                    generated_root / ("tools-%s.json" % next(iter(supplied))),
                    generated_root / ("routes-%s.json" % next(iter(supplied))),
                    **supplied,
                )
        with self.assertRaisesRegex(ToolConfigurationError, "unsupported Vulkan"):
            generate_official_toolchain_configs(
                generated_root / "tools-invalid.json",
                generated_root / "routes-invalid.json",
                spirv_val=spirv_val,
                spirv_target_env="opengl4.5",
            )
        generate_official_toolchain_configs(
            generated_root / "tools-valid.json",
            generated_root / "routes-valid.json",
            glslang=glslang,
            spirv_target_env="vulkan1.3",
        )
        valid_tools = json.loads(
            (generated_root / "tools-valid.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            valid_tools["tools"]["glslang-fragment-vulkan"]["compile"]["args"][:3],
            ["-V", "--target-env", "vulkan1.3"],
        )

    def test_vulkan_ray_glsl_round_trip_is_registered_but_replay_apply_is_separate(self) -> None:
        generated_root = self.root / "vulkan-ray"
        generated_root.mkdir()
        spirv_cross = generated_root / "spirv-cross.exe"
        glslang = generated_root / "glslangValidator.exe"
        spirv_val = generated_root / "spirv-val.exe"
        for path in (spirv_cross, glslang, spirv_val):
            path.write_bytes(("fake " + path.name).encode("ascii"))

        generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            spirv_cross=spirv_cross,
            glslang=glslang,
            spirv_val=spirv_val,
            spirv_target_env="vulkan1.2",
        )
        catalog = ToolchainCatalog(
            generated_root / "tools.json", generated_root / "routes.json"
        )
        plan = catalog.select_round_trip(
            "spirv", "raygen", editable_format="glsl", target_api="vulkan"
        )
        self.assertEqual(plan["decompileRouteIds"], ["spirv-cross-spirv-glsl"])
        self.assertEqual(plan["compileRouteIds"], ["glslang-glsl-spirv-raygen"])

        matrix = catalog.diagnostic_matrix("raygen")
        row = next(
            item
            for item in matrix["matrix"]
            if item["inputFormat"] == "spirv" and item["outputFormat"] == "glsl"
        )
        self.assertTrue(row["roundTripReady"])
        self.assertTrue(row["automaticRoundTripReady"])
        self.assertTrue(row["editable"])
        self.assertTrue(row["compilable"])
        self.assertFalse(row["replayAbiValidated"])
        self.assertFalse(row["replayReplaceable"])
        self.assertFalse(row["replayStageSupported"])
        self.assertIsNone(row["unsupportedReason"])
        self.assertIn("SBT", row["replayUnsupportedReason"])

        tools = json.loads((generated_root / "tools.json").read_text(encoding="utf-8"))
        arguments = tools["tools"]["glslang-raygen-vulkan"]["compile"]["args"]
        self.assertEqual(arguments[arguments.index("-S") + 1], "rgen")
        self.assertNotIn("glslang-raygen-opengl", tools["tools"])

        cross_arguments = derive_spirv_cross_arguments(
            source="ray.spv",
            output="ray.rgen.glsl",
            mode="vulkan-glsl",
            entry_point="RayMain",
            stage="raygen",
            target_api="Vulkan",
        )
        self.assertEqual(cross_arguments[cross_arguments.index("--stage") + 1], "rgen")

    @unittest.skipUnless(os.name == "nt", "native SPIR-V smoke requires Windows")
    def test_real_spirv_source_and_ir_round_trips_when_explicitly_enabled(self) -> None:
        if os.environ.get("REPLAYLAB_RUN_SPIRV_SMOKE") != "1":
            self.skipTest("set REPLAYLAB_RUN_SPIRV_SMOKE=1 for native smoke")
        variables = {
            "glslang": "REPLAYLAB_GLSLANG_SMOKE_EXE",
            "spirv_cross": "REPLAYLAB_SPIRV_CROSS_SMOKE_EXE",
            "spirv_dis": "REPLAYLAB_SPIRV_DIS_SMOKE_EXE",
            "spirv_as": "REPLAYLAB_SPIRV_AS_SMOKE_EXE",
            "spirv_val": "REPLAYLAB_SPIRV_VAL_SMOKE_EXE",
        }
        binaries = {
            name: Path(str(os.environ.get(variable, "") or "")).resolve()
            for name, variable in variables.items()
        }
        missing = [name for name, path in binaries.items() if not path.is_file()]
        if missing:
            self.skipTest("explicit SPIR-V smoke tools are unavailable: %s" % missing)

        config = self.root / "native-spirv-config"
        config.mkdir()
        generate_official_toolchain_configs(
            config / "tools.json",
            config / "routes.json",
            glslang=binaries["glslang"],
            spirv_cross=binaries["spirv_cross"],
            spirv_dis=binaries["spirv_dis"],
            spirv_as=binaries["spirv_as"],
            spirv_val=binaries["spirv_val"],
            spirv_target_env="vulkan1.2",
            enable_opengl_spirv=True,
        )
        catalog = ToolchainCatalog(config / "tools.json", config / "routes.json")
        cases = (
            (
                "fragment",
                "vulkan",
                "frag",
                "spirv",
                "#version 460\nlayout(location=0) out vec4 color;\n"
                "void main(){ color=vec4(1,0,0,1); }\n",
            ),
            (
                "compute",
                "vulkan",
                "comp",
                "spirv",
                "#version 460\nlayout(local_size_x=8) in;\n"
                "layout(set=0,binding=0) buffer Data { uint values[]; };\n"
                "void main(){ values[gl_GlobalInvocationID.x] += 1; }\n",
            ),
            (
                "raygen",
                "vulkan",
                "rgen",
                "spirv",
                "#version 460\n#extension GL_EXT_ray_tracing : require\n"
                "void main(){ }\n",
            ),
            (
                "fragment",
                "opengl",
                "frag",
                "opengl-spirv",
                "#version 460\nlayout(location=0) out vec4 color;\n"
                "void main(){ color=vec4(0,1,0,1); }\n",
            ),
        )
        for index, (stage, api, suffix, format_hint, source) in enumerate(cases):
            with self.subTest(stage=stage, api=api):
                case_root = self.root / ("native-spirv-%d" % index)
                case_root.mkdir()
                source_path = case_root / ("source." + suffix)
                binary_path = case_root / "captured.spv"
                source_path.write_text(source, encoding="utf-8")
                command = [
                    str(binaries["glslang"]),
                    "-G" if api == "opengl" else "-V",
                    "--target-env",
                    "opengl" if api == "opengl" else "vulkan1.2",
                    "-S",
                    suffix,
                    "-e",
                    "main",
                    "-o",
                    str(binary_path),
                    str(source_path),
                ]
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                direct_cross_output = case_root / "direct-cross.glsl"
                direct_cross = subprocess.run(
                    [str(binaries["spirv_cross"])]
                    + derive_spirv_cross_arguments(
                        source=str(binary_path),
                        output=str(direct_cross_output),
                        mode=(
                            "opengl-glsl" if api == "opengl" else "vulkan-glsl"
                        ),
                        entry_point="main",
                        stage=stage,
                        target_api=api,
                    ),
                    check=False,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(
                    0,
                    direct_cross.returncode,
                    direct_cross.stderr.decode("utf-8", errors="replace"),
                )
                editable_formats = (
                    "glsl",
                    "opengl-spirv-asm" if api == "opengl" else "spirv-asm",
                )
                for editable in editable_formats:
                    smoke = catalog.round_trip_smoke(
                        binary_path,
                        editable,
                        case_root / ("smoke-" + editable),
                        stage,
                        entry_point="main",
                        input_format_hint=format_hint,
                        target_api=api,
                    )
                    diagnostics = {
                        str(path.relative_to(case_root)): path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        for path in case_root.rglob("stderr.log")
                    }
                    self.assertIsNone(
                        smoke["error"], {"report": smoke, "diagnostics": diagnostics}
                    )
                    self.assertTrue(smoke["formatSmokePassed"])
                    self.assertTrue(smoke["externalValidationConfigured"])
                    self.assertTrue(smoke["externalValidationPassed"])
                    self.assertFalse(smoke["semanticEquivalenceVerified"])
                    self.assertFalse(smoke["replayAbiValidated"])

    @unittest.skipUnless(os.name == "nt", "native DXBC smoke requires Windows")
    def test_real_dxbc_hlsl_round_trip_when_explicitly_enabled(self) -> None:
        if os.environ.get("REPLAYLAB_RUN_DXBC_SMOKE") != "1":
            self.skipTest("set REPLAYLAB_RUN_DXBC_SMOKE=1 for native smoke")
        decompiler = Path(
            str(os.environ.get("REPLAYLAB_DXBC_DECOMPILER_SMOKE_EXE", "") or "")
        ).resolve()
        fxc = Path(
            str(os.environ.get("REPLAYLAB_FXC_SMOKE_EXE", "") or "")
        ).resolve()
        dependency = decompiler.parent / "d3dcompiler_46.dll"
        missing = [
            label
            for label, path in (
                ("cmd_Decompiler", decompiler),
                ("D3DCompiler_46", dependency),
                ("FXC", fxc),
            )
            if not path.is_file()
        ]
        if missing:
            self.skipTest("explicit DXBC smoke tools are unavailable: %s" % missing)

        config = self.root / "native-dxbc-config"
        config.mkdir()
        generate_official_toolchain_configs(
            config / "tools.json",
            config / "routes.json",
            fxc=fxc,
            hlsl_decompiler=decompiler,
            extra_fixed_files={"hlsl-decompiler": [dependency]},
        )
        catalog = ToolchainCatalog(config / "tools.json", config / "routes.json")
        case_root = self.root / "native-dxbc"
        case_root.mkdir()
        source_path = case_root / "source.hlsl"
        binary_path = case_root / "captured.dxbc"
        source_path.write_text(
            "cbuffer Params : register(b0) { float4 tint; };\n"
            "Texture2D texture0 : register(t0);\n"
            "SamplerState sampler0 : register(s0);\n"
            "struct PSInput { float4 position : SV_Position; float2 uv : TEXCOORD0; };\n"
            "float4 main(PSInput input) : SV_Target0 {\n"
            "  return texture0.Sample(sampler0, input.uv) * tint;\n"
            "}\n",
            encoding="utf-8",
        )
        compile_baseline = subprocess.run(
            [
                str(fxc),
                "/nologo",
                "/T",
                "ps_5_0",
                "/E",
                "main",
                "/Fo",
                str(binary_path),
                str(source_path),
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(
            0,
            compile_baseline.returncode,
            compile_baseline.stderr.decode("utf-8", errors="replace"),
        )
        for editable in ("hlsl", "dxbc-asm"):
            smoke = catalog.round_trip_smoke(
                binary_path,
                editable,
                case_root / ("smoke-" + editable),
                "fragment",
                entry_point="main",
                input_format_hint="dxbc",
                target_api="d3d11",
            )
            diagnostics = {
                str(path.relative_to(case_root)): path.read_text(
                    encoding="utf-8", errors="replace"
                )
                for path in case_root.rglob("stderr.log")
            }
            self.assertIsNone(
                smoke["error"], {"report": smoke, "diagnostics": diagnostics}
            )
            self.assertEqual("ps_5_0", smoke["targetProfile"])
            self.assertTrue(smoke["formatSmokePassed"])
            self.assertFalse(smoke["semanticEquivalenceVerified"])
            self.assertFalse(smoke["replayAbiValidated"])

    @unittest.skipUnless(os.name == "nt", "native RenoDX smoke requires Windows")
    def test_real_renodx_dxil_hlsl_round_trip_when_explicitly_enabled(self) -> None:
        if os.environ.get("REPLAYLAB_RUN_RENODX_SMOKE") != "1":
            self.skipTest("set REPLAYLAB_RUN_RENODX_SMOKE=1 for native smoke")
        decompiler = Path(
            str(os.environ.get("REPLAYLAB_RENODX_SMOKE_EXE", "") or "")
        ).resolve()
        dxc = Path(str(os.environ.get("REPLAYLAB_DXC_SMOKE_EXE", "") or "")).resolve()
        dxcompiler = decompiler.parent / "dxcompiler.dll"
        missing = [
            label
            for label, path in (
                ("RenoDX decomp", decompiler),
                ("DXC", dxc),
                ("RenoDX dxcompiler", dxcompiler),
            )
            if not path.is_file()
        ]
        if missing:
            self.skipTest("explicit RenoDX smoke tools are unavailable: %s" % missing)

        case_root = self.root / "native-renodx"
        case_root.mkdir()
        source_path = case_root / "source.hlsl"
        binary_path = case_root / "captured.dxil"
        decompiled_path = case_root / "decompiled.hlsl"
        round_trip_path = case_root / "round-trip.dxil"
        source_path.write_text(
            "float4 main(float4 position : SV_Position) : SV_Target0 {\n"
            "  return float4(0.125, 0.25, 0.5, 1.0);\n"
            "}\n",
            encoding="utf-8",
        )

        def run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=30,
            )

        baseline = run(
            [
                str(dxc),
                "-T",
                "ps_6_0",
                "-E",
                "main",
                "-Fo",
                str(binary_path),
                str(source_path),
            ]
        )
        self.assertEqual(
            0, baseline.returncode, baseline.stderr.decode("utf-8", errors="replace")
        )
        decompile = run([str(decompiler), str(binary_path), str(decompiled_path)])
        self.assertEqual(
            0,
            decompile.returncode,
            decompile.stderr.decode("utf-8", errors="replace"),
        )
        self.assertTrue(decompiled_path.is_file())
        recompile = run(
            [
                str(dxc),
                "-T",
                "ps_6_0",
                "-E",
                "main",
                "-Fo",
                str(round_trip_path),
                str(decompiled_path),
            ]
        )
        self.assertEqual(
            0,
            recompile.returncode,
            recompile.stderr.decode("utf-8", errors="replace"),
        )
        rebuilt = extract_target_profile(round_trip_path.read_bytes(), "fragment")
        self.assertEqual("ps_6_0", rebuilt.profile)

    @unittest.skipUnless(os.name == "nt", "native dxil-spirv smoke requires Windows")
    def test_real_dxil_spirv_hlsl_bridge_when_explicitly_enabled(self) -> None:
        if os.environ.get("REPLAYLAB_RUN_DXIL_SPIRV_SMOKE") != "1":
            self.skipTest("set REPLAYLAB_RUN_DXIL_SPIRV_SMOKE=1 for native smoke")
        dxc = Path(str(os.environ.get("REPLAYLAB_DXC_SMOKE_EXE", "") or "")).resolve()
        dxil_spirv = Path(
            str(os.environ.get("REPLAYLAB_DXIL_SPIRV_SMOKE_EXE", "") or "")
        ).resolve()
        spirv_cross = Path(
            str(os.environ.get("REPLAYLAB_SPIRV_CROSS_SMOKE_EXE", "") or "")
        ).resolve()
        missing = [
            label
            for label, path in (
                ("DXC", dxc),
                ("dxil-spirv", dxil_spirv),
                ("SPIRV-Cross", spirv_cross),
            )
            if not path.is_file()
        ]
        if missing:
            self.skipTest("explicit DXIL bridge tools are unavailable: %s" % missing)

        config = self.root / "native-dxil-spirv-config"
        config.mkdir()
        generate_official_toolchain_configs(
            config / "tools.json",
            config / "routes.json",
            dxc=dxc,
            dxil_spirv=dxil_spirv,
            spirv_cross=spirv_cross,
        )
        catalog = ToolchainCatalog(config / "tools.json", config / "routes.json")
        case_root = self.root / "native-dxil-spirv"
        case_root.mkdir()
        source_path = case_root / "source.hlsl"
        binary_path = case_root / "captured.dxil"
        source_path.write_text(
            "float4 CapturedPS(float4 position : SV_Position) : SV_Target0 {\n"
            "  return float4(position.xy * 0.001, 0.5, 1.0);\n"
            "}\n",
            encoding="utf-8",
        )
        baseline = subprocess.run(
            [
                str(dxc),
                "-T",
                "ps_6_0",
                "-E",
                "CapturedPS",
                "-Fo",
                str(binary_path),
                str(source_path),
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(
            0, baseline.returncode, baseline.stderr.decode("utf-8", errors="replace")
        )
        smoke = catalog.round_trip_smoke(
            binary_path,
            "hlsl",
            case_root / "smoke-hlsl",
            "fragment",
            entry_point="CapturedPS",
            input_format_hint="dxil",
            target_api="d3d12",
        )
        diagnostics = {
            str(path.relative_to(case_root)): path.read_text(
                encoding="utf-8", errors="replace"
            )
            for path in case_root.rglob("stderr.log")
        }
        self.assertIsNone(
            smoke["error"], {"report": smoke, "diagnostics": diagnostics}
        )
        self.assertEqual("ps_6_0", smoke["targetProfile"])
        self.assertTrue(smoke["formatSmokePassed"])
        self.assertEqual(smoke["jobs"][0]["physicalEntryPoint"], "main")
        self.assertEqual(smoke["jobs"][1]["physicalEntryPoint"], "main")
        self.assertEqual(smoke["jobs"][1]["emittedEntryPoint"], "CapturedPS")
        self.assertFalse(smoke["semanticEquivalenceVerified"])
        self.assertFalse(smoke["replayAbiValidated"])

    def test_spirv_cross_adapter_uses_physical_multi_entry_stage_and_sm6_profile(self) -> None:
        arguments = derive_spirv_cross_arguments(
            source=r"D:\jobs\multi-entry.spv",
            output=r"D:\jobs\selected.hlsl",
            mode="hlsl",
            entry_point="LightingPS",
            stage="fragment",
            target_api="GraphicsAPI.D3D12",
            target_profile="ps_6_7",
        )
        self.assertEqual(
            arguments,
            [
                r"D:\jobs\multi-entry.spv",
                "--entry",
                "LightingPS",
                "--stage",
                "frag",
                "--hlsl",
                "--shader-model",
                "67",
                "--rename-entry-point",
                "LightingPS",
                "LightingPS",
                "frag",
                "--output",
                r"D:\jobs\selected.hlsl",
            ],
        )
        renamed = derive_spirv_cross_arguments(
            source=r"D:\jobs\translated.spv",
            output=r"D:\jobs\reconstructed.hlsl",
            mode="hlsl",
            entry_point="main",
            captured_entry_point="MainPS",
            stage="fragment",
            target_api="D3D12",
            target_profile="ps_6_6",
        )
        rename_index = renamed.index("--rename-entry-point")
        entry_index = renamed.index("--entry")
        self.assertEqual(renamed[entry_index + 1], "MainPS")
        self.assertEqual(
            renamed[rename_index + 1 : rename_index + 4],
            ["main", "MainPS", "frag"],
        )
        with self.assertRaisesRegex(ValueError, "contradicts physical stage"):
            derive_spirv_cross_arguments(
                source="in.spv",
                output="out.hlsl",
                mode="hlsl",
                entry_point="LightingPS",
                stage="fragment",
                target_api="D3D12",
                target_profile="vs_6_7",
            )
        with self.assertRaisesRegex(ValueError, "physical Direct3D API"):
            derive_spirv_cross_arguments(
                source="in.spv",
                output="out.hlsl",
                mode="hlsl",
                entry_point="LightingPS",
                stage="fragment",
                target_api="Vulkan",
                target_profile="ps_6_7",
            )
        with self.assertRaisesRegex(ValueError, "contradicts physical D3D11"):
            derive_spirv_cross_arguments(
                source="in.spv",
                output="out.hlsl",
                mode="hlsl",
                entry_point="LightingPS",
                stage="fragment",
                target_api="D3D11",
                target_profile="ps_6_7",
            )

    def test_vulkan_spirv_never_reports_direct3d_hlsl_route_ready(self) -> None:
        generated_root = self.root / "spirv-api-applicability"
        generated_root.mkdir()
        spirv_cross = generated_root / "spirv-cross.exe"
        spirv_cross.write_bytes(b"fake spirv-cross")
        generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            spirv_cross=spirv_cross,
        )
        catalog = ToolchainCatalog(
            generated_root / "tools.json", generated_root / "routes.json"
        )
        hlsl_route = next(
            route
            for route in catalog.routes()
            if route.route_id == "spirv-cross-spirv-hlsl"
        )
        self.assertEqual(hlsl_route.target_apis, ("d3d11", "d3d12"))
        plan = catalog.plan_recovery(
            "spirv",
            "fragment",
            editable_format="hlsl",
            target_api="vulkan",
        )
        self.assertEqual(plan["mode"], "evidence-only")
        self.assertEqual(plan["decompileRouteIds"], ["spirv-cross-vulkan-hlsl"])
        self.assertEqual(plan["compileRouteIds"], [])

    def test_opengl_spirv_routes_never_alias_vulkan_semantics(self) -> None:
        generated_root = self.root / "opengl-spirv"
        generated_root.mkdir()
        binaries = {}
        for name in (
            "spirv-cross.exe",
            "glslangValidator.exe",
            "spirv-dis.exe",
            "spirv-as.exe",
            "spirv-val.exe",
        ):
            path = generated_root / name
            path.write_bytes(("fake " + name).encode("ascii"))
            binaries[name] = path.resolve()
        result = generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            spirv_cross=binaries["spirv-cross.exe"],
            glslang=binaries["glslangValidator.exe"],
            spirv_dis=binaries["spirv-dis.exe"],
            spirv_as=binaries["spirv-as.exe"],
            spirv_val=binaries["spirv-val.exe"],
            spirv_target_env="vulkan1.2",
            enable_opengl_spirv=True,
        )
        self.assertTrue(result["openGlSpirvEnabled"])
        catalog = ToolchainCatalog(
            generated_root / "tools.json", generated_root / "routes.json"
        )
        selected = catalog.select_round_trip(
            "OpenGLSPIRV",
            "fragment",
            editable_format="glsl",
            capture_encoding="OpenGLSPIRV",
            target_api="GraphicsAPI.OpenGL",
        )
        self.assertEqual(selected["targetApi"], "opengl")
        self.assertEqual(
            selected["decompileRouteIds"],
            ["spirv-cross-opengl-spirv-glsl"],
        )
        self.assertEqual(
            selected["compileRouteIds"],
            ["glslang-glsl-opengl-spirv-fragment"],
        )
        with self.assertRaisesRegex(ToolchainRoutingError, "does not accept"):
            catalog.select_round_trip(
                "OpenGLSPIRV", "fragment", target_api="vulkan"
            )
        with self.assertRaisesRegex(ToolchainRoutingError, "does not accept"):
            catalog.select_round_trip("spirv", "fragment", target_api="opengl")

        tools = json.loads((generated_root / "tools.json").read_text(encoding="utf-8"))
        self.assertEqual(
            tools["tools"]["glslang-fragment-opengl"]["compile"]["args"][:3],
            ["-G", "--target-env", "opengl"],
        )
        self.assertEqual(
            tools["tools"]["spirv-tools-val-opengl"]["validate"]["args"],
            ["--target-env", "opengl4.5", "{input}"],
        )

        arguments = derive_spirv_cross_arguments(
            source="in.opengl.spv",
            output="out.glsl",
            mode="opengl-glsl",
            entry_point="main",
            stage="fragment",
            target_api="OpenGL",
        )
        self.assertNotIn("--vulkan-semantics", arguments)
        with self.assertRaisesRegex(ValueError, "physical OpenGL API"):
            derive_spirv_cross_arguments(
                source="in.opengl.spv",
                output="out.glsl",
                mode="opengl-glsl",
                entry_point="main",
                stage="fragment",
                target_api="Vulkan",
            )

    def test_generated_spirv_cross_routes_pin_adapter_and_physical_placeholders(self) -> None:
        generated_root = self.root / "spirv-cross-adapter"
        generated_root.mkdir()
        spirv_cross = generated_root / "spirv-cross.exe"
        spirv_cross.write_bytes(b"fake spirv-cross")
        generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            spirv_cross=spirv_cross,
        )
        tools = json.loads((generated_root / "tools.json").read_text(encoding="utf-8"))
        operation = tools["tools"]["spirv-cross-hlsl"]
        args = operation["decompile"]["args"]
        self.assertIn("{entry_point}", args)
        self.assertIn("{stage}", args)
        self.assertIn("{target_profile}", args)
        self.assertIn("{target_api}", args)
        self.assertIn(str(spirv_cross.resolve()), operation["fixedFileHashes"])
        self.assertTrue(
            any(
                path.endswith("spirv_cross_adapter.py")
                for path in operation["fixedFileHashes"]
            )
        )

    def test_dxc_generator_pins_official_sibling_dlls_when_present(self) -> None:
        generated_root = self.root / "dxc-generated"
        generated_root.mkdir()
        dxc = generated_root / "dxc.exe"
        compiler = generated_root / "dxcompiler.dll"
        validator = generated_root / "dxil.dll"
        dxc.write_bytes(b"fake dxc")
        compiler.write_bytes(b"fake compiler dll")
        validator.write_bytes(b"fake validator dll")
        generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            dxc=dxc.resolve(),
        )
        tools = json.loads((generated_root / "tools.json").read_text(encoding="utf-8"))
        fixed = tools["tools"]["dxc-exact-profile-dxil"]["fixedFileHashes"]
        self.assertIn(str(compiler.resolve()), fixed)
        self.assertIn(str(validator.resolve()), fixed)
        self.assertNotIn("dxc-exact-profile-spirv", tools["tools"])
        self.assertEqual(
            tools["tools"]["dxc-exact-profile-dxil"]["decompile"]["args"],
            ["-dumpbin", "-Fc", "{output}", "{input}"],
        )
        routes = json.loads(
            (generated_root / "routes.json").read_text(encoding="utf-8")
        )["routes"]
        native_dump = next(
            item for item in routes if item["routeId"] == "dxc-dxil-native-disassembly"
        )
        self.assertEqual(native_dump["outputFormat"], "dxil-asm")
        self.assertEqual(native_dump["fidelity"], "ir-disassembly")

    def test_fxc_generator_exposes_read_only_native_disassembly(self) -> None:
        generated_root = self.root / "fxc-generated"
        generated_root.mkdir()
        fxc = generated_root / "fxc.exe"
        fxc.write_bytes(b"fake fxc")
        generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            fxc=fxc.resolve(),
        )
        tools = json.loads((generated_root / "tools.json").read_text(encoding="utf-8"))
        self.assertEqual(
            tools["tools"]["fxc-exact-profile"]["decompile"]["args"],
            ["/nologo", "/dumpbin", "/Fc", "{output}", "{input}"],
        )
        catalog = ToolchainCatalog(
            generated_root / "tools.json", generated_root / "routes.json"
        )
        route = catalog.matching_routes("dxbc", "dxbc-asm", "fragment")[0]
        self.assertEqual(route.fidelity, "ir-disassembly")
        row = next(
            item
            for item in catalog.diagnostic_matrix("fragment")["matrix"]
            if item["inputFormat"] == "dxbc"
            and item["outputFormat"] == "dxbc-asm"
        )
        self.assertEqual(row["lane"], "explanation")
        self.assertFalse(row["directReplayApplyAuthorized"])

    def test_d3dcompiler_generator_pins_dll_adapter_and_uniquely_beats_fxc(self) -> None:
        generated_root = self.root / "d3dcompiler-generated"
        generated_root.mkdir()
        dll = generated_root / "d3dcompiler_47.dll"
        fxc = generated_root / "fxc.exe"
        dll.write_bytes(b"fake pinned d3dcompiler")
        fxc.write_bytes(b"fake fxc")
        generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            d3dcompiler=dll,
            fxc=fxc,
        )
        tools = json.loads((generated_root / "tools.json").read_text("utf-8"))
        record = tools["tools"]["d3dcompiler47-exact-profile"]
        self.assertEqual(str(Path(sys.executable).resolve()), record["executable"])
        self.assertIn(str(dll.resolve()), record["fixedFileHashes"])
        self.assertTrue(
            any(
                path.endswith("d3dcompiler_adapter.py")
                for path in record["fixedFileHashes"]
            )
        )
        arguments = record["compile"]["args"]
        self.assertIn("{input}", arguments)
        self.assertIn("{output}", arguments)
        self.assertIn("{entry_point}", arguments)
        self.assertIn("{target_profile}", arguments)
        self.assertNotIn("{stage}", arguments)
        self.assertNotIn("{target_api}", arguments)
        self.assertFalse(any("include" in value.casefold() for value in arguments))

        catalog = ToolchainCatalog(
            generated_root / "tools.json", generated_root / "routes.json"
        )
        selected = catalog.find_path("hlsl", "dxbc", "fragment")
        self.assertEqual(
            [route.route_id for route in selected],
            ["d3dcompiler47-hlsl-dxbc-exact-profile"],
        )
        fxc_route = next(
            route
            for route in catalog.matching_routes("hlsl", "dxbc", "fragment")
            if route.route_id == "fxc-hlsl-dxbc-exact-profile"
        )
        self.assertLess(selected[0].priority, fxc_route.priority)

        wrong_name = generated_root / "compiler.dll"
        wrong_name.write_bytes(b"same API, wrong trusted package identity")
        with self.assertRaisesRegex(ToolConfigurationError, "must be named"):
            generate_official_toolchain_configs(
                generated_root / "wrong-tools.json",
                generated_root / "wrong-routes.json",
                d3dcompiler=wrong_name,
            )

    def test_community_adapters_are_explicit_lossy_routes_with_round_trip_backends(self) -> None:
        generated_root = self.root / "community-generated"
        generated_root.mkdir()
        binaries = {}
        for name in (
            "cmd_Decompiler.exe",
            "dxil-spirv.exe",
            "spirv-cross.exe",
            "fxc.exe",
            "dxc.exe",
        ):
            path = generated_root / name
            path.write_bytes(("fake " + name).encode("ascii"))
            binaries[name] = path.resolve()
        generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            hlsl_decompiler=binaries["cmd_Decompiler.exe"],
            dxil_spirv=binaries["dxil-spirv.exe"],
            spirv_cross=binaries["spirv-cross.exe"],
            fxc=binaries["fxc.exe"],
            dxc=binaries["dxc.exe"],
        )
        catalog = ToolchainCatalog(
            generated_root / "tools.json", generated_root / "routes.json"
        )
        dxbc = catalog.select_round_trip("dxbc", "fragment", target_api="d3d11")
        self.assertEqual(
            dxbc["decompileRouteIds"],
            ["community-hlsl-decompiler-dxbc-hlsl"],
        )
        self.assertEqual(
            dxbc["compileRouteIds"], ["fxc-hlsl-dxbc-exact-profile"]
        )
        assembly = catalog.select_round_trip(
            "dxbc",
            "fragment",
            editable_format="dxbc-asm",
            target_api="d3d11",
        )
        self.assertEqual(
            assembly["decompileRouteIds"],
            ["community-3dmigoto-dxbc-disassemble"],
        )
        self.assertEqual(
            assembly["compileRouteIds"],
            ["community-3dmigoto-dxbc-assemble"],
        )
        self.assertFalse(assembly["containsReconstructedSource"])
        self.assertFalse(assembly["semanticEquivalenceVerified"])
        dxil = catalog.select_round_trip("dxil", "fragment", target_api="d3d12")
        self.assertEqual(
            dxil["decompileRouteIds"],
            ["community-dxil-spirv", "spirv-cross-spirv-hlsl"],
        )
        self.assertEqual(
            dxil["compileRouteIds"], ["dxc-hlsl-dxil-exact-profile"]
        )
        legacy_dxil_spirv = catalog.matching_routes(
            "dxbc", "spirv", "fragment"
        )
        self.assertEqual(
            [item.route_id for item in legacy_dxil_spirv],
            ["community-dxbc-spirv"],
        )
        self.assertIn(
            "dxbc-spirv integration",
            " ".join(legacy_dxil_spirv[0].caveats),
        )
        self.assertIn(
            "reverse-compile probe",
            " ".join(legacy_dxil_spirv[0].caveats),
        )
        community = {
            item.implementation: item
            for item in catalog.routes()
            if item.implementation in {"3DMigoto cmd_Decompiler", "dxil-spirv"}
        }
        self.assertEqual(set(community), {"3DMigoto cmd_Decompiler", "dxil-spirv"})
        self.assertIn("GPL-3.0", community["3DMigoto cmd_Decompiler"].license_name)
        self.assertEqual(community["dxil-spirv"].license_name, "MIT")
        self.assertTrue(all(item.caveats for item in community.values()))
        generated_tools = json.loads(
            (generated_root / "tools.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            generated_tools["tools"]["dxil-spirv-transform"]["transform"]["args"],
            [
                "{input}",
                "--output",
                "{output}",
                "--validate",
            ],
        )
        self.assertNotIn(
            "{entry_point}",
            generated_tools["tools"]["dxil-spirv-transform"]["transform"]["args"],
        )
        assembly_tool = generated_tools["tools"]["community-dxbc-assembly"]
        decompile_args = assembly_tool["decompile"]["args"]
        compile_args = assembly_tool["compile"]["args"]
        self.assertIn("--mode", decompile_args)
        self.assertIn("disassemble", decompile_args)
        self.assertIn("{raw_sha256}", decompile_args)
        self.assertIn("--mode", compile_args)
        self.assertIn("assemble", compile_args)
        self.assertIn("--baseline", compile_args)
        self.assertIn("{baseline}", compile_args)
        self.assertNotIn("-V", compile_args)
        self.assertTrue(
            any(
                path.endswith("dxbc_assembly_adapter.py")
                for path in assembly_tool["fixedFileHashes"]
            )
        )
        row = next(
            item
            for item in catalog.diagnostic_matrix("fragment")["matrix"]
            if item["inputFormat"] == "dxbc"
            and item["outputFormat"] == "dxbc-asm"
        )
        self.assertTrue(row["roundTripReady"])
        self.assertEqual("executable-editable", row["lane"])

    def test_renodx_direct_dxil_reconstruction_precedes_spirv_bridge(self) -> None:
        generated_root = self.root / "renodx-generated"
        generated_root.mkdir()
        binaries = {}
        for name in (
            "decomp.exe",
            "dxcompiler.dll",
            "dxil-spirv.exe",
            "spirv-cross.exe",
            "dxc.exe",
        ):
            path = generated_root / name
            path.write_bytes(("fake " + name).encode("ascii"))
            binaries[name] = path.resolve()

        generate_official_toolchain_configs(
            generated_root / "tools.json",
            generated_root / "routes.json",
            renodx_decompiler=binaries["decomp.exe"],
            dxil_spirv=binaries["dxil-spirv.exe"],
            spirv_cross=binaries["spirv-cross.exe"],
            dxc=binaries["dxc.exe"],
        )
        registry = json.loads(
            (generated_root / "tools.json").read_text(encoding="utf-8")
        )
        operation = registry["tools"]["community-renodx-dxil-decompiler"]
        self.assertEqual(str(Path(sys.executable).resolve()), operation["executable"])
        arguments = operation["decompile"]["args"]
        self.assertTrue(arguments[0].endswith("renodx_decompiler_adapter.py"))
        self.assertEqual(
            str(binaries["decomp.exe"]), arguments[arguments.index("--tool") + 1]
        )
        self.assertEqual(
            hashlib.sha256(binaries["decomp.exe"].read_bytes()).hexdigest(),
            arguments[arguments.index("--tool-sha256") + 1],
        )
        self.assertEqual("{input}", arguments[arguments.index("--input") + 1])
        self.assertEqual("{output}", arguments[arguments.index("--output") + 1])
        self.assertEqual(
            "{workspace_dir}", arguments[arguments.index("--workspace") + 1]
        )
        self.assertEqual(
            "{raw_sha256}", arguments[arguments.index("--raw-sha256") + 1]
        )
        self.assertEqual(
            "{entry_point}", arguments[arguments.index("--entry") + 1]
        )
        self.assertEqual("{stage}", arguments[arguments.index("--stage") + 1])
        self.assertEqual(
            hashlib.sha256(binaries["decomp.exe"].read_bytes()).hexdigest(),
            operation["fixedFileHashes"][str(binaries["decomp.exe"])],
        )
        self.assertEqual(
            hashlib.sha256(binaries["dxcompiler.dll"].read_bytes()).hexdigest(),
            operation["fixedFileHashes"][str(binaries["dxcompiler.dll"])],
        )
        self.assertTrue(
            any(
                path.endswith("renodx_decompiler_adapter.py")
                for path in operation["fixedFileHashes"]
            )
        )

        catalog = ToolchainCatalog(
            generated_root / "tools.json", generated_root / "routes.json"
        )
        candidates = catalog.candidate_paths("dxil", "hlsl", "fragment")
        self.assertGreaterEqual(len(candidates), 2)
        self.assertEqual(
            [route.route_id for route in candidates[0]],
            ["community-renodx-dxil-hlsl"],
        )
        self.assertEqual(
            [route.route_id for route in candidates[1]],
            ["community-dxil-spirv", "spirv-cross-spirv-hlsl"],
        )
        selected = catalog.select_round_trip(
            "dxil", "fragment", target_api="d3d12"
        )
        self.assertEqual(
            ["community-renodx-dxil-hlsl"],
            selected["decompileRouteIds"],
        )
        direct = candidates[0][0]
        self.assertEqual("reconstructed-source", direct.fidelity)
        self.assertEqual("RenoDX decomp.exe", direct.implementation)
        self.assertIn("MIT", direct.license_name)
        self.assertIn("Third-party/community", " ".join(direct.caveats))
        self.assertIn("not proof of semantic equivalence", " ".join(direct.caveats))

        missing_root = self.root / "renodx-missing-dependency"
        missing_root.mkdir()
        lone_decompiler = missing_root / "decomp.exe"
        lone_decompiler.write_bytes(b"lone provider")
        with self.assertRaisesRegex(
            ToolConfigurationError, "side-by-side dxcompiler.dll"
        ):
            generate_official_toolchain_configs(
                missing_root / "tools.json",
                missing_root / "routes.json",
                renodx_decompiler=lone_decompiler,
            )

    def test_official_dxc_ir_assembler_is_an_executable_round_trip(self) -> None:
        generated_root = self.root / "dxc-assembler-generated"
        generated_root.mkdir()
        dxc = generated_root / "dxc.exe"
        dxcompiler = generated_root / "dxcompiler.dll"
        dxil = generated_root / "dxil.dll"
        dxc.write_bytes(b"fake dxc cli")
        dxcompiler.write_bytes(b"fake official dxc runtime")
        dxil.write_bytes(b"fake optional validator runtime")
        tool_path = generated_root / "tools.json"
        route_path = generated_root / "routes.json"
        generate_official_toolchain_configs(
            tool_path,
            route_path,
            dxc=dxc,
        )

        registry = json.loads(tool_path.read_text(encoding="utf-8"))
        operation = registry["tools"]["dxc-official-dxil-assembler"]
        arguments = operation["compile"]["args"]
        self.assertEqual(str(Path(sys.executable).resolve()), operation["executable"])
        self.assertTrue(arguments[0].endswith("dxil_assembler_adapter.py"))
        self.assertEqual(str(dxcompiler.resolve()), arguments[arguments.index("--dll") + 1])
        self.assertEqual(
            hashlib.sha256(dxcompiler.read_bytes()).hexdigest(),
            arguments[arguments.index("--dll-sha256") + 1],
        )
        self.assertEqual("{input}", arguments[arguments.index("--input") + 1])
        self.assertEqual("{output}", arguments[arguments.index("--output") + 1])
        self.assertNotIn("{entry_point}", arguments)
        self.assertNotIn("{target_profile}", arguments)
        self.assertEqual(
            hashlib.sha256(dxcompiler.read_bytes()).hexdigest(),
            operation["fixedFileHashes"][str(dxcompiler.resolve())],
        )
        self.assertEqual(
            hashlib.sha256(dxil.read_bytes()).hexdigest(),
            operation["fixedFileHashes"][str(dxil.resolve())],
        )
        self.assertTrue(
            any(
                path.endswith("dxil_assembler_adapter.py")
                for path in operation["fixedFileHashes"]
            )
        )

        catalog = ToolchainCatalog(tool_path, route_path)
        assembly_route = catalog.matching_routes(
            "dxil-asm", "dxil", "fragment"
        )
        self.assertEqual(
            ["dxc-dxil-assemble-to-container"],
            [route.route_id for route in assembly_route],
        )
        self.assertEqual("ir-assembly", assembly_route[0].fidelity)
        self.assertIn("Microsoft DXC", assembly_route[0].implementation)
        self.assertIn("do not prove", " ".join(assembly_route[0].caveats))
        round_trip = catalog.select_round_trip(
            "dxil",
            "fragment",
            editable_format="dxil-asm",
            target_api="d3d12",
        )
        self.assertEqual(
            ["dxc-dxil-native-disassembly"],
            round_trip["decompileRouteIds"],
        )
        self.assertEqual(
            ["dxc-dxil-assemble-to-container"],
            round_trip["compileRouteIds"],
        )
        self.assertFalse(round_trip["containsReconstructedSource"])
        self.assertFalse(round_trip["semanticEquivalenceVerified"])
        automatic = catalog.select_round_trip(
            "dxil", "fragment", target_api="d3d12"
        )
        self.assertEqual("dxil-asm", automatic["editableFormat"])
        self.assertEqual(
            ["dxc-dxil-native-disassembly"],
            automatic["decompileRouteIds"],
        )
        recovery = catalog.plan_automatic_recovery(
            "dxil", "fragment", target_api="d3d12"
        )
        self.assertEqual("round-trip", recovery["mode"])
        self.assertEqual("dxil-asm", recovery["editableFormat"])
        self.assertEqual(
            "automatic-native-ir-round-trip-fallback",
            recovery["diagnostics"][0]["category"],
        )
        shader_path = generated_root / "captured.dxil"
        shader_path.write_bytes(
            make_container(b"DXIL", make_dxil_program(0x00000060))
        )
        file_recovery = catalog.plan_recovery_for_file(
            shader_path,
            "fragment",
            target_api="d3d12",
        )
        self.assertEqual("round-trip", file_recovery["mode"])
        self.assertEqual("dxil-asm", file_recovery["editableFormat"])

        ray_library_path = generated_root / "captured-ray-library.dxil"
        ray_library_path.write_bytes(
            make_container(b"DXIL", make_dxil_program(0x00060063))
        )
        ray_library_recovery = catalog.plan_recovery_for_file(
            ray_library_path,
            "raygen",
            target_api="d3d12",
        )
        self.assertEqual("round-trip", ray_library_recovery["mode"])
        self.assertEqual("dxil-asm", ray_library_recovery["editableFormat"])
        self.assertEqual(
            "lib_6_3", ray_library_recovery["targetProfile"]["profile"]
        )
        self.assertEqual(
            "library", ray_library_recovery["targetProfile"]["stage"]
        )
        ray_matrix = catalog.diagnostic_matrix("raygen")
        ray_ir = next(
            row
            for row in ray_matrix["matrix"]
            if row["inputFormat"] == "dxil"
            and row["outputFormat"] == "dxil-asm"
        )
        self.assertTrue(ray_ir["editable"])
        self.assertTrue(ray_ir["compilable"])
        self.assertFalse(ray_ir["replayStageSupported"])

        unavailable = self.root / "dxc-assembler-unavailable"
        unavailable.mkdir()
        lone_dxc = unavailable / "dxc.exe"
        lone_dxc.write_bytes(b"dxc without runtime")
        generate_official_toolchain_configs(
            unavailable / "tools.json",
            unavailable / "routes.json",
            dxc=lone_dxc,
        )
        unavailable_registry = json.loads(
            (unavailable / "tools.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "dxc-official-dxil-assembler", unavailable_registry["tools"]
        )
        unavailable_catalog = ToolchainCatalog(
            unavailable / "tools.json", unavailable / "routes.json"
        )
        self.assertEqual(
            (),
            unavailable_catalog.matching_routes(
                "dxil-asm", "dxil", "fragment"
            ),
        )

    def test_route_schema_rejects_false_fidelity_and_native_family_edges(self) -> None:
        generated_root = self.root / "dxc-automatic-fallback-safety"
        generated_root.mkdir()
        dxc = generated_root / "dxc.exe"
        dxcompiler = generated_root / "dxcompiler.dll"
        dxil = generated_root / "dxil.dll"
        dxc.write_bytes(b"fake dxc cli")
        dxcompiler.write_bytes(b"fake official dxc runtime")
        dxil.write_bytes(b"fake optional validator runtime")
        tool_path = generated_root / "tools.json"
        route_path = generated_root / "routes.json"
        generate_official_toolchain_configs(tool_path, route_path, dxc=dxc)
        original = json.loads(route_path.read_text(encoding="utf-8"))
        dump = next(
            route
            for route in original["routes"]
            if route["routeId"] == "dxc-dxil-native-disassembly"
        )
        assemble = next(
            route
            for route in original["routes"]
            if route["routeId"] == "dxc-dxil-assemble-to-container"
        )

        malformed_cases = (
            ("source-labelled-ir", dump, "dxil", "hlsl", "ir-disassembly"),
            ("wrong-native-family", dump, "dxil", "spirv-asm", "ir-disassembly"),
            ("source-labelled-assembly", assemble, "hlsl", "dxil", "ir-assembly"),
            ("wrong-assembly-family", assemble, "spirv-asm", "dxil", "ir-assembly"),
            ("custom-on-decompile", dump, "dxil", "slang", "custom-transform"),
        )
        for route_id, template, source, target, fidelity in malformed_cases:
            malformed = json.loads(json.dumps(original))
            route = json.loads(json.dumps(template))
            route.update(
                {
                    "routeId": route_id,
                    "inputFormat": source,
                    "outputFormat": target,
                    "fidelity": fidelity,
                }
            )
            malformed["routes"] = [route]
            malformed_path = generated_root / ("routes-%s.json" % route_id)
            atomic_write_json(malformed_path, malformed)
            with self.assertRaises(ToolConfigurationError, msg=route_id):
                ToolchainCatalog(tool_path, malformed_path)

    def test_automatic_native_ir_fallback_rejects_ambiguity(self) -> None:
        generated_root = self.root / "dxc-automatic-fallback-ambiguity"
        generated_root.mkdir()
        dxc = generated_root / "dxc.exe"
        dxcompiler = generated_root / "dxcompiler.dll"
        dxil = generated_root / "dxil.dll"
        dxc.write_bytes(b"fake dxc cli")
        dxcompiler.write_bytes(b"fake official dxc runtime")
        dxil.write_bytes(b"fake optional validator runtime")
        tool_path = generated_root / "tools.json"
        route_path = generated_root / "routes.json"
        generate_official_toolchain_configs(tool_path, route_path, dxc=dxc)
        original = json.loads(route_path.read_text(encoding="utf-8"))
        dump = next(
            route
            for route in original["routes"]
            if route["routeId"] == "dxc-dxil-native-disassembly"
        )
        ambiguous = json.loads(json.dumps(original))
        duplicate = json.loads(json.dumps(dump))
        duplicate["routeId"] = "dxc-dxil-native-disassembly-alternate"
        ambiguous["routes"].append(duplicate)
        ambiguous_path = generated_root / "routes-ambiguous.json"
        atomic_write_json(ambiguous_path, ambiguous)
        ambiguous_plan = ToolchainCatalog(
            tool_path, ambiguous_path
        ).plan_automatic_recovery("dxil", "fragment", target_api="d3d12")
        self.assertEqual("compile-only-recovery", ambiguous_plan["mode"])
        self.assertEqual("hlsl", ambiguous_plan["editableFormat"])

if __name__ == "__main__":
    unittest.main()
