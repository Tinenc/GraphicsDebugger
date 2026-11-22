from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from src.shader_workspace.adapters import renodx_decompiler_adapter as adapter


def _row(index: int, register: int) -> dict[str, object]:
    return {
        "semanticName": "TEXCOORD",
        "semanticIdxName": f"TEXCOORD{index}",
        "semanticIndex": index,
        "needSemanticIndex": True,
        "regIndex": register,
        "regChannelMask": 15,
        # Deliberately present to prove that repair does not use this coarse
        # reflection field as permission to synthesize a missing declaration.
        "channelUsedMask": 0,
    }


def _interface() -> dict[str, object]:
    return {
        "interfaceComplete": True,
        "entryPoint": "MainPS",
        "inputSignature": [_row(0, 0), _row(1, 1)],
    }


FULL_SOURCE = """// A comment containing main( is not a function definition.
float4 main(
  linear float4 TEXCOORD_1 : TEXCOORD1,
  linear float4 TEXCOORD : TEXCOORD
) : SV_Target {
  float4 SV_Target;
  SV_Target = TEXCOORD_1;
  return SV_Target;
}
"""


class RepairRenoDxHlslTests(unittest.TestCase):
    def test_complete_signature_is_reordered_and_only_entry_name_changes(self):
        repaired = adapter.repair_renodx_hlsl(
            FULL_SOURCE,
            baseline_interface=_interface(),
            entry_point="MainPS",
            stage="fragment",
        )

        self.assertIn("float4 MainPS(", repaired)
        self.assertIn("// A comment containing main(", repaired)
        self.assertLess(
            repaired.index("linear float4 TEXCOORD : TEXCOORD"),
            repaired.index("linear float4 TEXCOORD_1 : TEXCOORD1"),
        )
        self.assertIn("SV_Target = TEXCOORD_1;", repaired)
        self.assertNotIn("__replaylab_unused_input", repaired)

    def test_any_missing_baseline_row_fails_closed_even_when_used_mask_is_zero(self):
        source = FULL_SOURCE.replace(
            "  linear float4 TEXCOORD_1 : TEXCOORD1,\n", ""
        )
        with self.assertRaisesRegex(
            adapter.RenoDxAdapterError, "omitted a baseline input signature row"
        ):
            adapter.repair_renodx_hlsl(
                source,
                baseline_interface=_interface(),
                entry_point="MainPS",
                stage="fragment",
            )

    def test_extra_or_duplicate_generated_semantics_fail_closed(self):
        extra = FULL_SOURCE.replace(
            "  linear float4 TEXCOORD : TEXCOORD\n",
            "  linear float4 TEXCOORD : TEXCOORD,\n"
            "  linear float4 COLOR : COLOR\n",
        )
        with self.assertRaisesRegex(adapter.RenoDxAdapterError, "absent from the baseline"):
            adapter.repair_renodx_hlsl(
                extra,
                baseline_interface=_interface(),
                entry_point="MainPS",
                stage="fragment",
            )

        duplicate = FULL_SOURCE.replace(
            "  linear float4 TEXCOORD : TEXCOORD\n",
            "  linear float4 TEXCOORD : TEXCOORD,\n"
            "  linear float4 TEXCOORD_COPY : TEXCOORD\n",
        )
        with self.assertRaisesRegex(adapter.RenoDxAdapterError, "duplicate input semantic"):
            adapter.repair_renodx_hlsl(
                duplicate,
                baseline_interface=_interface(),
                entry_point="MainPS",
                stage="fragment",
            )

    def test_compute_with_empty_physical_signature_only_renames_entry(self):
        source = "[numthreads(8, 8, 1)]\nvoid main() { return; }\n"
        repaired = adapter.repair_renodx_hlsl(
            source,
            baseline_interface={"interfaceComplete": True, "inputSignature": []},
            entry_point="MainCS",
            stage="compute",
        )
        self.assertEqual("[numthreads(8, 8, 1)]\nvoid MainCS() { return; }\n", repaired)


class RenoDxAdapterMainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.tool = self.root / "decomp.exe"
        self.tool.write_bytes(b"hash-pinned RenoDX fixture")
        self.tool_hash = hashlib.sha256(self.tool.read_bytes()).hexdigest()
        self.shader = self.root / "shader.dxil"
        self.shader.write_bytes(b"hash-bound DXIL fixture")
        self.shader_hash = hashlib.sha256(self.shader.read_bytes()).hexdigest()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        manifest = {
            "stage": "fragment",
            "entryPoint": "MainPS",
            "baseline": {
                "rawSha256": self.shader_hash,
                "compiledInterface": _interface(),
            },
        }
        (self.workspace / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        self.output = self.root / "shader.hlsl"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _arguments(self) -> list[str]:
        return [
            "--tool",
            str(self.tool),
            "--tool-sha256",
            self.tool_hash,
            "--input",
            str(self.shader),
            "--output",
            str(self.output),
            "--workspace",
            str(self.workspace),
            "--raw-sha256",
            self.shader_hash,
            "--entry",
            "MainPS",
            "--stage",
            "fragment",
        ]

    def test_main_invokes_only_hash_bound_tool_and_preserves_raw_evidence(self):
        captured: dict[str, object] = {}

        def run(arguments, **kwargs):
            captured["arguments"] = arguments
            captured["kwargs"] = kwargs
            Path(arguments[2]).write_text(FULL_SOURCE, encoding="utf-8")
            return subprocess.CompletedProcess(arguments, 0)

        with mock.patch.object(adapter.subprocess, "run", side_effect=run):
            self.assertEqual(0, adapter.main(self._arguments()))

        self.assertEqual(
            [str(self.tool.resolve()), str(self.shader.resolve()), str(self.output) + ".renodx-raw.hlsl"],
            captured["arguments"],
        )
        kwargs = captured["kwargs"]
        self.assertIsInstance(kwargs, dict)
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["check"], False)
        self.assertEqual(str(self.shader.parent.resolve()), kwargs["cwd"])
        self.assertEqual(FULL_SOURCE, Path(str(self.output) + ".renodx-raw.hlsl").read_text(encoding="utf-8"))
        self.assertIn("float4 MainPS(", self.output.read_text(encoding="utf-8"))

    def test_tool_hash_mismatch_prevents_process_launch_and_output(self):
        arguments = self._arguments()
        arguments[arguments.index("--tool-sha256") + 1] = "0" * 64
        with mock.patch.object(adapter.subprocess, "run") as run:
            with self.assertRaisesRegex(adapter.RenoDxAdapterError, "SHA-256 mismatch"):
                adapter.main(arguments)
        run.assert_not_called()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
