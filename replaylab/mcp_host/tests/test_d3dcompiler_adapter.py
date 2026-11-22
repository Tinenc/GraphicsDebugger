from __future__ import annotations

from contextlib import redirect_stderr
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from src.shader_workspace.adapters import d3dcompiler_adapter as adapter


class D3DCompilerAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dll = self.root / "d3dcompiler_47.dll"
        self.dll.write_bytes(b"pinned compiler fixture")
        self.dll_hash = hashlib.sha256(self.dll.read_bytes()).hexdigest()
        self.source = self.root / "shader.hlsl"
        self.source.write_text(
            "float4 main() : SV_Target { return float4(1, 0, 0, 1); }\n",
            encoding="utf-8",
        )
        self.output = self.root / "shader.dxbc"

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
            "--entry",
            "main",
            "--target",
            "ps_5_0",
        ]

    def test_fixed_compile_has_no_macro_include_or_flag_surface(self):
        captured = {}

        def compile_bytes(dll, source, source_name, entry, target):
            captured.update(
                dll=dll,
                source=source,
                source_name=source_name,
                entry=entry,
                target=target,
            )
            return 0, b"DXBCfixture", b"warning one\nwarning two\x00"

        diagnostics = io.StringIO()
        with mock.patch.object(adapter, "_compile_bytes", side_effect=compile_bytes), redirect_stderr(
            diagnostics
        ):
            self.assertEqual(0, adapter.main(self._arguments()))
        self.assertEqual(b"DXBCfixture", self.output.read_bytes())
        self.assertEqual(self.dll.resolve(), captured["dll"])
        self.assertEqual(self.source.read_bytes(), captured["source"])
        self.assertEqual("shader.hlsl", captured["source_name"])
        self.assertEqual("main", captured["entry"])
        self.assertEqual("ps_5_0", captured["target"])
        self.assertEqual("warning one\nwarning two\n", diagnostics.getvalue())

    def test_native_error_blob_is_reported_completely_and_output_is_absent(self):
        diagnostics = io.StringIO()
        with mock.patch.object(
            adapter,
            "_compile_bytes",
            return_value=(-2147467259, b"", b"first compiler error\nsecond compiler error\x00"),
        ), redirect_stderr(diagnostics):
            self.assertEqual(1, adapter.main(self._arguments()))
        value = diagnostics.getvalue()
        self.assertIn("first compiler error", value)
        self.assertIn("second compiler error", value)
        self.assertIn("HRESULT 0x80004005", value)
        self.assertFalse(self.output.exists())

    def test_hash_name_profile_entry_and_output_are_fail_closed(self):
        cases = [
            ("hash", ["--dll-sha256", "0" * 64], "SHA-256 changed"),
            ("entry", ["--entry", "main();"], "HLSL identifier"),
            ("profile", ["--target", "ps_6_0"], "Shader Model 4/5"),
        ]
        base = self._arguments()
        for label, replacement, pattern in cases:
            with self.subTest(label=label):
                arguments = list(base)
                key = replacement[0]
                arguments[arguments.index(key) + 1] = replacement[1]
                with self.assertRaisesRegex(RuntimeError, pattern):
                    adapter.main(arguments)

        wrong_dll = self.root / "arbitrary.dll"
        wrong_dll.write_bytes(self.dll.read_bytes())
        arguments = list(base)
        arguments[arguments.index("--dll") + 1] = str(wrong_dll)
        with self.assertRaisesRegex(RuntimeError, "must be named"):
            adapter.main(arguments)

        self.output.write_bytes(b"do not replace")
        with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
            adapter.main(base)
        self.assertEqual(b"do not replace", self.output.read_bytes())

    @unittest.skipUnless(os.name == "nt", "D3DCompile smoke requires Windows")
    def test_windows_sdk_d3dcompiler_real_minimal_compile_when_available(self):
        dll = Path(
            r"C:\Program Files (x86)\Windows Kits\10\Redist\D3D\x64\d3dcompiler_47.dll"
        )
        if not dll.is_file() or dll.is_symlink():
            self.skipTest("Windows SDK x64 d3dcompiler_47.dll is not installed")
        arguments = self._arguments()
        arguments[arguments.index("--dll") + 1] = str(dll)
        arguments[arguments.index("--dll-sha256") + 1] = hashlib.sha256(
            dll.read_bytes()
        ).hexdigest()
        self.assertEqual(0, adapter.main(arguments))
        compiled = self.output.read_bytes()
        self.assertGreater(len(compiled), 32)
        self.assertEqual(b"DXBC", compiled[:4])

    @unittest.skipUnless(os.name == "nt", "D3DCompile smoke requires Windows")
    def test_windows_sdk_compile_does_not_resolve_includes_when_available(self):
        dll = Path(
            r"C:\Program Files (x86)\Windows Kits\10\Redist\D3D\x64\d3dcompiler_47.dll"
        )
        if not dll.is_file() or dll.is_symlink():
            self.skipTest("Windows SDK x64 d3dcompiler_47.dll is not installed")
        self.source.write_text(
            '#include "replaylab-must-not-resolve.hlsli"\n'
            "float4 main() : SV_Target { return 1; }\n",
            encoding="utf-8",
        )
        arguments = self._arguments()
        arguments[arguments.index("--dll") + 1] = str(dll)
        arguments[arguments.index("--dll-sha256") + 1] = hashlib.sha256(
            dll.read_bytes()
        ).hexdigest()
        diagnostics = io.StringIO()
        with redirect_stderr(diagnostics):
            self.assertEqual(1, adapter.main(arguments))
        self.assertFalse(self.output.exists())
        self.assertIn("include", diagnostics.getvalue().casefold())


if __name__ == "__main__":
    unittest.main()
