import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from src.shader_workspace.adapters import dxbc_assembly_adapter as adapter


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DxbcAssemblyAdapterTests(unittest.TestCase):
    def test_disassemble_uses_upstream_1316_cli_and_publishes_new_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tool = root / "cmd_Decompiler.exe"
            source = root / "input.dxbc"
            output = root / "output.dxbc.txt"
            tool.write_bytes(b"pinned 3dmigoto")
            source.write_bytes(b"captured dxbc")
            commands = []

            def run(command, **kwargs):
                commands.append((command, kwargs))
                source.with_suffix(".asm").write_text(
                    "ps_5_0\ndcl_globalFlags refactoringAllowed\nret\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(adapter.subprocess, "run", side_effect=run):
                result = adapter.main(
                    [
                        "--mode",
                        "disassemble",
                        "--tool",
                        str(tool),
                        "--tool-sha256",
                        _sha(tool.read_bytes()),
                        "--input",
                        str(source),
                        "--output",
                        str(output),
                        "--baseline-sha256",
                        _sha(source.read_bytes()),
                    ]
                )

            self.assertEqual(0, result)
            self.assertEqual(
                [
                    str(tool),
                    "-d",
                    "-V",
                    "--stop-on-failure",
                    str(source),
                ],
                commands[0][0],
            )
            self.assertFalse(commands[0][1]["shell"])
            self.assertTrue(output.is_file())
            self.assertFalse(source.with_suffix(".asm").exists())

    def test_assemble_copies_exact_baseline_reflection_without_fake_validation_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tool = root / "cmd_Decompiler.exe"
            baseline = root / "captured.dxbc"
            source = root / "input.dxbc.txt"
            output = root / "candidate.dxbc"
            tool.write_bytes(b"pinned 3dmigoto")
            baseline.write_bytes(b"captured reflection and signatures")
            source.write_text("ps_5_0\ndcl_globalFlags refactoringAllowed\nret\n")
            commands = []

            def run(command, **kwargs):
                commands.append(command)
                source.with_suffix(".shdr").write_bytes(b"DXBC candidate")
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(adapter.subprocess, "run", side_effect=run):
                result = adapter.main(
                    [
                        "--mode",
                        "assemble",
                        "--tool",
                        str(tool),
                        "--tool-sha256",
                        _sha(tool.read_bytes()),
                        "--input",
                        str(source),
                        "--output",
                        str(output),
                        "--baseline",
                        str(baseline),
                        "--baseline-sha256",
                        _sha(baseline.read_bytes()),
                    ]
                )

            snapshot = root / "reflection-baseline.dxbc"
            self.assertEqual(0, result)
            self.assertEqual(baseline.read_bytes(), snapshot.read_bytes())
            self.assertEqual(
                [
                    str(tool),
                    "-a",
                    "--copy-reflection",
                    str(snapshot),
                    "--stop-on-failure",
                    str(source),
                ],
                commands[0],
            )
            self.assertNotIn("-V", commands[0])
            self.assertEqual(b"DXBC candidate", output.read_bytes())

    def test_hash_mismatch_and_existing_derived_output_fail_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tool = root / "cmd_Decompiler.exe"
            source = root / "input.dxbc"
            output = root / "output.txt"
            tool.write_bytes(b"tool")
            source.write_bytes(b"baseline")
            arguments = [
                "--mode",
                "disassemble",
                "--tool",
                str(tool),
                "--tool-sha256",
                _sha(tool.read_bytes()),
                "--input",
                str(source),
                "--output",
                str(output),
                "--baseline-sha256",
                "0" * 64,
            ]
            with mock.patch.object(adapter.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "captured baseline"):
                    adapter.main(arguments)
                run.assert_not_called()

            source.with_suffix(".asm").write_text("do not overwrite")
            arguments[-1] = _sha(source.read_bytes())
            with mock.patch.object(adapter.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "derived output"):
                    adapter.main(arguments)
                run.assert_not_called()

    def test_tool_mutation_during_run_blocks_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tool = root / "cmd_Decompiler.exe"
            source = root / "input.dxbc"
            output = root / "output.txt"
            tool.write_bytes(b"tool")
            source.write_bytes(b"baseline")

            def run(command, **kwargs):
                source.with_suffix(".asm").write_text(
                    "ps_5_0\ndcl_globalFlags refactoringAllowed\nret\n"
                )
                tool.write_bytes(b"changed")
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(adapter.subprocess, "run", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "changed while"):
                    adapter.main(
                        [
                            "--mode",
                            "disassemble",
                            "--tool",
                            str(tool),
                            "--tool-sha256",
                            _sha(b"tool"),
                            "--input",
                            str(source),
                            "--output",
                            str(output),
                            "--baseline-sha256",
                            _sha(source.read_bytes()),
                        ]
                    )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
