from __future__ import annotations

from contextlib import redirect_stderr
import hashlib
import io
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

from src.shader_workspace.adapters import dxil_assembler_adapter as adapter
from src.shader_workspace.profiles import extract_target_profile


def make_dxil_container(program: bytes = b"program") -> bytes:
    part = b"DXIL" + struct.pack("<I", len(program)) + program
    total_size = 36 + len(part)
    return (
        b"DXBC"
        + b"\x00" * 16
        + struct.pack("<IIII", 1, total_size, 1, 36)
        + part
    )


class DxilAssemblerAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dll = self.root / "dxcompiler.dll"
        self.dll.write_bytes(b"pinned DXC runtime fixture")
        self.dll_hash = hashlib.sha256(self.dll.read_bytes()).hexdigest()
        self.source = self.root / "shader.dxil.ll"
        self.source.write_text(
            "; ModuleID = 'fixture'\n"
            'target triple = "dxil-ms-dx"\n'
            "define void @main() { ret void }\n",
            encoding="utf-8",
        )
        self.output = self.root / "shader.dxil"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _arguments(self) -> list[str]:
        return [
            "--dll",
            str(self.dll),
            "--dll-sha256",
            self.dll_hash,
            "--input",
            str(self.source),
            "--output",
            str(self.output),
        ]

    def test_official_guids_and_vtable_slots_match_dxcapi(self):
        self.assertEqual(
            uuid.UUID("6245d6af-66e0-48fd-80b4-4d271796748c"),
            uuid.UUID(bytes_le=bytes(adapter.CLSID_DXC_LIBRARY)),
        )
        self.assertEqual(
            uuid.UUID("e5204dc7-d18c-4c3c-bdfb-851673980fe7"),
            uuid.UUID(bytes_le=bytes(adapter.IID_IDXC_LIBRARY)),
        )
        self.assertEqual(
            uuid.UUID("d728db68-f903-4f80-94cd-dccf76ec7151"),
            uuid.UUID(bytes_le=bytes(adapter.CLSID_DXC_ASSEMBLER)),
        )
        self.assertEqual(
            uuid.UUID("091f7a26-1c1f-4948-904b-e6e3a8a771d5"),
            uuid.UUID(bytes_le=bytes(adapter.IID_IDXC_ASSEMBLER)),
        )
        self.assertEqual(7, adapter._LIBRARY_CREATE_HEAP_COPY_SLOT)
        self.assertEqual(3, adapter._ASSEMBLER_ASSEMBLE_SLOT)
        self.assertEqual(
            (3, 4, 5),
            (
                adapter._OPERATION_GET_STATUS_SLOT,
                adapter._OPERATION_GET_RESULT_SLOT,
                adapter._OPERATION_GET_ERRORS_SLOT,
            ),
        )

    def test_fixed_assembly_has_no_command_profile_or_flag_surface(self):
        captured = {}
        container = make_dxil_container()

        def assemble_bytes(dll, source):
            captured.update(dll=dll, source=source)
            return 0, container, b"assembler warning\x00"

        diagnostics = io.StringIO()
        with mock.patch.object(
            adapter, "_assemble_bytes", side_effect=assemble_bytes
        ), redirect_stderr(diagnostics):
            self.assertEqual(0, adapter.main(self._arguments()))
        self.assertEqual(container, self.output.read_bytes())
        self.assertEqual(self.dll.resolve(), captured["dll"])
        self.assertEqual(self.source.read_bytes(), captured["source"])
        self.assertEqual("assembler warning\n", diagnostics.getvalue())

        with self.assertRaises(SystemExit):
            adapter.main(self._arguments() + ["--command", "anything.exe"])

    def test_operation_failure_reports_diagnostics_without_output(self):
        diagnostics = io.StringIO()
        with mock.patch.object(
            adapter,
            "_assemble_bytes",
            return_value=(-2147467259, b"", b"first error\nsecond error\x00"),
        ), redirect_stderr(diagnostics):
            self.assertEqual(1, adapter.main(self._arguments()))
        value = diagnostics.getvalue()
        self.assertIn("first error", value)
        self.assertIn("second error", value)
        self.assertIn("HRESULT 0x80004005", value)
        self.assertFalse(self.output.exists())

    def test_hash_name_ir_output_and_container_are_fail_closed(self):
        arguments = self._arguments()
        bad_hash = list(arguments)
        bad_hash[bad_hash.index("--dll-sha256") + 1] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "SHA-256 changed"):
            adapter.main(bad_hash)

        wrong_dll = self.root / "arbitrary.dll"
        wrong_dll.write_bytes(self.dll.read_bytes())
        wrong_name = list(arguments)
        wrong_name[wrong_name.index("--dll") + 1] = str(wrong_dll)
        with self.assertRaisesRegex(RuntimeError, "must be named"):
            adapter.main(wrong_name)

        self.source.write_text("define void @main() { ret void }\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "target triple"):
            adapter.main(arguments)

        self.source.write_bytes(b'target triple = "dxil-ms-dx"\n\x00')
        with self.assertRaisesRegex(RuntimeError, "no NUL"):
            adapter.main(arguments)

        self.source.write_text('target triple = "dxil-ms-dx"\n', encoding="utf-8")
        self.output.write_bytes(b"do not replace")
        with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
            adapter.main(arguments)
        self.assertEqual(b"do not replace", self.output.read_bytes())

        self.output.unlink()
        with mock.patch.object(
            adapter, "_assemble_bytes", return_value=(0, b"not a container", b"")
        ):
            with self.assertRaisesRegex(RuntimeError, "container size"):
                adapter.main(arguments)
        self.assertFalse(self.output.exists())

    def test_container_parser_requires_bounded_dxil_part(self):
        adapter._validate_dxil_container(make_dxil_container())
        missing_dxil = bytearray(make_dxil_container())
        missing_dxil[36:40] = b"STAT"
        with self.assertRaisesRegex(RuntimeError, "no DXIL"):
            adapter._validate_dxil_container(bytes(missing_dxil))
        truncated = bytearray(make_dxil_container())
        struct.pack_into("<I", truncated, 40, 0xFFFFFFFF)
        with self.assertRaisesRegex(RuntimeError, "truncated part"):
            adapter._validate_dxil_container(bytes(truncated))

    @unittest.skipUnless(os.name == "nt", "IDxcAssembler smoke requires Windows")
    def test_real_dxc_round_trip_when_explicitly_enabled(self):
        if os.environ.get("REPLAYLAB_RUN_DXC_ASSEMBLER_SMOKE") != "1":
            self.skipTest("set REPLAYLAB_RUN_DXC_ASSEMBLER_SMOKE=1 for native smoke")
        repository = Path(__file__).resolve().parents[2]
        configured_tools = str(
            os.environ.get("REPLAYLAB_DXC_SMOKE_DIR", "") or ""
        ).strip()
        tools = (
            Path(configured_tools).expanduser().resolve()
            if configured_tools
            else repository
            / "distribution"
            / "ReplayLab"
            / "replaylab"
            / "tools"
        )
        dxc = tools / "dxc.exe"
        dll = tools / "dxcompiler.dll"
        if not dxc.is_file() or not dll.is_file():
            self.skipTest("explicit DXC runtime is unavailable")

        hlsl = self.root / "native.hlsl"
        baseline = self.root / "native.dxil"
        assembly = self.root / "native.ll"
        rebuilt = self.root / "native.rebuilt.dxil"
        hlsl.write_text(
            "float4 main() : SV_Target { return float4(1, 0, 0, 1); }\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                str(dxc),
                "-T",
                "ps_6_0",
                "-E",
                "main",
                "-Vd",
                "-Fo",
                str(baseline),
                str(hlsl),
            ],
            cwd=str(tools),
            check=True,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            [str(dxc), "-dumpbin", "-Fc", str(assembly), str(baseline)],
            cwd=str(tools),
            check=True,
            capture_output=True,
            timeout=30,
        )
        arguments = [
            "--dll",
            str(dll),
            "--dll-sha256",
            hashlib.sha256(dll.read_bytes()).hexdigest(),
            "--input",
            str(assembly),
            "--output",
            str(rebuilt),
        ]
        self.assertEqual(0, adapter.main(arguments))
        adapter._validate_dxil_container(rebuilt.read_bytes())

        ray_hlsl = self.root / "native-ray-library.hlsl"
        ray_baseline = self.root / "native-ray-library.dxil"
        ray_assembly = self.root / "native-ray-library.ll"
        ray_rebuilt = self.root / "native-ray-library.rebuilt.dxil"
        ray_rebuilt_assembly = self.root / "native-ray-library.rebuilt.ll"
        ray_hlsl.write_text(
            "struct Payload { float4 color; };\n"
            '[shader("raygeneration")]\n'
            "void RayMain() { }\n"
            '[shader("miss")]\n'
            "void MissMain(inout Payload payload)\n"
            "{ payload.color = float4(1, 0, 0, 1); }\n",
            encoding="utf-8",
        )
        subprocess.run(
            [str(dxc), "-T", "lib_6_3", "-Fo", str(ray_baseline), str(ray_hlsl)],
            cwd=str(tools),
            check=True,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            [str(dxc), "-dumpbin", "-Fc", str(ray_assembly), str(ray_baseline)],
            cwd=str(tools),
            check=True,
            capture_output=True,
            timeout=30,
        )
        ray_arguments = list(arguments)
        ray_arguments[ray_arguments.index("--input") + 1] = str(ray_assembly)
        ray_arguments[ray_arguments.index("--output") + 1] = str(ray_rebuilt)
        self.assertEqual(0, adapter.main(ray_arguments))
        adapter._validate_dxil_container(ray_rebuilt.read_bytes())
        subprocess.run(
            [
                str(dxc),
                "-dumpbin",
                "-Fc",
                str(ray_rebuilt_assembly),
                str(ray_rebuilt),
            ],
            cwd=str(tools),
            check=True,
            capture_output=True,
            timeout=30,
        )
        for path in (ray_baseline, ray_rebuilt):
            profile = extract_target_profile(path.read_bytes(), "raygen")
            self.assertEqual("lib_6_3", profile.profile)
            self.assertEqual("library", profile.stage)
        baseline_ir = ray_assembly.read_text(encoding="utf-8")
        rebuilt_ir = ray_rebuilt_assembly.read_text(encoding="utf-8")
        for export_name in ("RayMain", "MissMain"):
            self.assertIn(export_name, baseline_ir)
            self.assertIn(export_name, rebuilt_ir)


if __name__ == "__main__":
    unittest.main()
