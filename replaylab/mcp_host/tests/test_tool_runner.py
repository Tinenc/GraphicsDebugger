"""An extractor is a program; declaring it like an MCP tool is enough to call it.

These tests pin the two halves. The declaration half is MCP's own shape -- name,
description, inputSchema -- so an Agent calls an .exe exactly the way it calls a
server. The execution half is a template with named slots where every slot must be a
declared parameter, checked before anything runs and substituted into an argument list
that never reaches a shell.

They also pin the parts that exist because the first version of this idea would have
been wrong without them: an invocation nobody watched succeed is not offered, a failure
that was observed is recorded so it is not rediscovered, and a recipe proved against a
program that has since been upgraded says so instead of failing mysteriously.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from src import tool_providers, tool_runner


def _operation(**overrides):
    declaration = {
        "kind": "cli",
        "role": "asset-reader",
        "name": "extract_texture",
        "description": "Extract a texture from a cooked package.",
        "argv": ["{program}", "--in", "{assetPath}", "--out", "{outputDir}",
                 "--format", "{format}"],
        "inputSchema": {
            "type": "object",
            "properties": {
                "assetPath": {"type": "string"},
                "format": {"type": "string", "enum": ["png", "dds"], "default": "png"},
                "mip": {"type": "integer", "default": 0},
            },
            "required": ["assetPath"],
        },
        "result": {"kind": "files-in-output-dir",
                   "successWhen": {"exitCode": 0, "minFiles": 1}},
        "proved": {"sample": "E:/assets/a.uasset"},
    }
    declaration.update(overrides)
    return tool_providers.validate_operation(declaration)


class DeclarationTests(unittest.TestCase):
    def test_an_operation_is_declared_the_way_an_mcp_tool_is(self) -> None:
        operation = _operation()
        self.assertEqual(operation["name"], "extract_texture")
        self.assertTrue(operation["description"])
        self.assertEqual(operation["inputSchema"]["type"], "object")
        self.assertEqual(operation["inputSchema"]["required"], ["assetPath"])

    def test_a_template_slot_that_is_not_a_parameter_is_refused(self) -> None:
        """Otherwise only whoever wrote the template could ever call it."""

        with self.assertRaises(tool_providers.ToolConfigError) as refusal:
            _operation(argv=["{program}", "--in", "{assetPath}", "--key", "{secret}"])
        self.assertIn("secret", str(refusal.exception))

    def test_program_and_output_dir_are_supplied_rather_than_declared(self) -> None:
        operation = _operation(argv=["{program}", "{outputDir}", "{assetPath}"])
        self.assertEqual(operation["name"], "extract_texture")

    def test_the_registered_program_is_always_what_runs(self) -> None:
        """A recipe cannot execute anything but the tool the user registered.

        Reader recipes are recorded as arguments only, because the Host supplies the
        command itself. Run through the named catalog, such a template would put its
        own first argument in the program position.
        """

        arguments_only = _operation(argv=["--in", "{assetPath}", "--out", "{outputDir}"])
        argv = tool_runner.build_argv(
            arguments_only, {"assetPath": "a.uasset"},
            program="C:/tools/unpacker.exe", output_dir="C:/out")
        self.assertEqual(argv[0], "C:/tools/unpacker.exe")
        self.assertEqual(argv[1], "--in")
        # A template that already names the program is left exactly as written.
        explicit = tool_runner.build_argv(
            _operation(), {"assetPath": "a.uasset"},
            program="C:/tools/unpacker.exe", output_dir="C:/out")
        self.assertEqual(explicit.count("C:/tools/unpacker.exe"), 1)
        self.assertEqual(explicit[0], "C:/tools/unpacker.exe")

    def test_a_parameter_type_the_command_line_cannot_carry_is_refused(self) -> None:
        with self.assertRaises(tool_providers.ToolConfigError):
            _operation(inputSchema={"type": "object", "properties": {
                "options": {"type": "array"}}, "required": []})

    def test_requiring_something_undeclared_is_refused(self) -> None:
        with self.assertRaises(tool_providers.ToolConfigError):
            _operation(inputSchema={"type": "object",
                                    "properties": {"assetPath": {"type": "string"}},
                                    "required": ["assetPath", "ghost"]})

    def test_what_a_tool_cannot_do_is_recorded_next_to_what_it_can(self) -> None:
        operation = _operation(knownFailures=[
            {"case": "SkeletalMesh", "symptom": "exit 3221225477"}])
        self.assertEqual(operation["knownFailures"][0]["case"], "SkeletalMesh")
        self.assertTrue(operation["knownFailures"][0]["at"])

    def test_proof_records_the_program_it_was_proved_against(self) -> None:
        operation = _operation(proved={"sample": "E:/a.uasset",
                                       "programSha256": "ab" * 32,
                                       "producedFiles": 3})
        self.assertEqual(operation["proved"]["producedFiles"], 3)
        self.assertEqual(operation["proved"]["programSha256"], "ab" * 32)

    def test_an_operation_with_no_sample_is_not_proved(self) -> None:
        self.assertIsNone(_operation(proved={})["proved"])


class ArgumentCheckingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.operation = _operation()

    def _argv(self, arguments):
        return tool_runner.build_argv(self.operation, arguments,
                                      program="/x/uex", output_dir="/out")

    def test_defaults_are_applied_and_values_land_in_the_right_slots(self) -> None:
        self.assertEqual(
            self._argv({"assetPath": "/e/a.uasset"}),
            ["/x/uex", "--in", "/e/a.uasset", "--out", "/out", "--format", "png"])

    def test_an_undeclared_argument_is_refused_rather_than_ignored(self) -> None:
        """A silently dropped parameter runs, succeeds, and did the wrong thing."""

        with self.assertRaises(tool_runner.ToolRunError) as refusal:
            self._argv({"assetPath": "/e/a", "recursive": True})
        self.assertIn("recursive", str(refusal.exception))

    def test_a_missing_required_argument_is_named(self) -> None:
        with self.assertRaises(tool_runner.ToolRunError) as refusal:
            self._argv({})
        self.assertIn("assetPath", str(refusal.exception))

    def test_a_value_outside_a_declared_enum_is_refused(self) -> None:
        with self.assertRaises(tool_runner.ToolRunError):
            self._argv({"assetPath": "/e/a", "format": "tga"})

    def test_a_value_cannot_smuggle_in_a_newline_or_a_null(self) -> None:
        for hostile in ("a\nb", "a\r\nb", "a\x00b"):
            with self.assertRaises(tool_runner.ToolRunError):
                self._argv({"assetPath": hostile})

    def test_a_value_containing_a_slot_stays_data(self) -> None:
        """One substitution pass, so a value cannot introduce another slot."""

        argv = self._argv({"assetPath": "/e/{outputDir}.uasset"})
        self.assertIn("/e/{outputDir}.uasset", argv)

    def test_a_number_is_checked_as_a_number(self) -> None:
        operation = _operation(argv=["{program}", "--mip", "{mip}", "{assetPath}"])
        with self.assertRaises(tool_runner.ToolRunError):
            tool_runner.build_argv(operation, {"assetPath": "/e/a", "mip": "three"},
                                   program="/x/uex", output_dir="/out")
        self.assertIn("0", tool_runner.build_argv(
            operation, {"assetPath": "/e/a"}, program="/x/uex", output_dir="/out"))


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)

    def test_umodel_gui_fallback_is_rejected_before_process_creation(self) -> None:
        from unittest import mock

        program = self.root / "umodel_64.exe"
        program.write_bytes(b"MZ")
        cooked = self.root / "A.uasset"
        cooked.write_bytes(b"package")
        for arguments in ([], ["{assetPath}"], ["-view", "{assetPath}"],
                          ["-gui", "-list", "{assetPath}"], ["-list"],
                          ["-list", "{assetPath}"]):
            operation = _operation(argv=["{program}", *arguments])
            with mock.patch.object(tool_runner.subprocess, "run") as run:
                with self.assertRaises(tool_runner.ToolRunError):
                    tool_runner.run_operation(operation, {"assetPath": str(cooked)},
                                              program=str(program), output_root=self.root)
                run.assert_not_called()
        legacy = self.root / "A.upk"
        legacy.write_bytes(b"package")
        tool_runner.validate_headless_argv([str(program), "-list", str(legacy)])
        tool_runner.validate_headless_argv([str(program), "-export", "-out=target", str(legacy)])
        tool_runner.validate_headless_argv([str(program), "-help"])
        with self.assertRaises(tool_runner.ToolRunError):
            tool_runner.validate_headless_argv([str(program), "-list", str(self.root)])
        with self.assertRaises(tool_runner.ToolRunError):
            tool_runner.validate_headless_argv([str(program), "-list", str(self.root / "missing.upk")])

    def _program(self, script: str) -> str:
        path = self.root / "extractor.sh"
        path.write_text("#!/bin/sh\n" + script, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    @unittest.skipIf(os.name == "nt", "the shell-script stand-in is POSIX only")
    def test_a_successful_run_reports_what_it_produced(self) -> None:
        program = self._program('echo "read $2"\ntouch "$4/out.png"\n')
        receipt = tool_runner.run_operation(
            _operation(), {"assetPath": "/e/a.uasset"},
            program=program, output_root=self.root)
        self.assertTrue(receipt["succeeded"])
        self.assertEqual(receipt["producedFileCount"], 1)
        self.assertIn("read /e/a.uasset", receipt["stdout"])
        self.assertEqual(len(receipt["programSha256"]), 64)

    @unittest.skipIf(os.name == "nt", "the shell-script stand-in is POSIX only")
    def test_a_failure_comes_back_described_rather_than_raised(self) -> None:
        """A described failure is evidence; a raised one is a dead end."""

        program = self._program('echo "cannot read that" >&2\nexit 3\n')
        receipt = tool_runner.run_operation(
            _operation(), {"assetPath": "/e/a.uasset"},
            program=program, output_root=self.root)
        self.assertFalse(receipt["succeeded"])
        self.assertEqual(receipt["exitCode"], 3)
        self.assertIn("cannot read that", receipt["stderr"])

    @unittest.skipIf(os.name == "nt", "the shell-script stand-in is POSIX only")
    def test_exiting_cleanly_without_producing_anything_is_not_success(self) -> None:
        program = self._program("exit 0\n")
        receipt = tool_runner.run_operation(
            _operation(), {"assetPath": "/e/a.uasset"},
            program=program, output_root=self.root)
        self.assertEqual(receipt["exitCode"], 0)
        self.assertFalse(receipt["succeeded"])

    @unittest.skipIf(os.name == "nt", "the shell-script stand-in is POSIX only")
    def test_a_program_that_hangs_is_stopped_and_says_so(self) -> None:
        program = self._program("sleep 30\n")
        receipt = tool_runner.run_operation(
            _operation(), {"assetPath": "/e/a.uasset"},
            program=program, output_root=self.root, timeout_seconds=1.0)
        self.assertTrue(receipt["timedOut"])
        self.assertFalse(receipt["succeeded"])

    def test_a_program_that_is_not_there_is_refused_before_anything_runs(self) -> None:
        with self.assertRaises(tool_runner.ToolRunError):
            tool_runner.run_operation(
                _operation(), {"assetPath": "/e/a"},
                program=str(self.root / "absent"), output_root=self.root)

    @unittest.skipIf(os.name == "nt", "the shell-script stand-in is POSIX only")
    def test_output_goes_to_a_host_owned_directory_per_run(self) -> None:
        program = self._program('touch "$4/out.png"\n')
        first = tool_runner.run_operation(
            _operation(), {"assetPath": "/e/a"}, program=program, output_root=self.root)
        second = tool_runner.run_operation(
            _operation(), {"assetPath": "/e/b"}, program=program, output_root=self.root)
        self.assertNotEqual(first["outputDirectory"], second["outputDirectory"])
        for receipt in (first, second):
            self.assertTrue(Path(receipt["outputDirectory"]).is_relative_to(self.root))


class CatalogTests(unittest.TestCase):
    def test_the_built_in_parsers_are_declared_like_everything_else(self) -> None:
        catalog = tool_runner.declared_operations([])
        self.assertEqual(len(catalog), 3)
        for entry in catalog:
            self.assertTrue(entry["name"])
            self.assertTrue(entry["description"])
            self.assertTrue(entry["purpose"])
            self.assertEqual(entry["inputSchema"]["required"], ["assetPath"])
            self.assertTrue(entry["proved"])
            self.assertEqual(entry["source"], "built-in")

    def test_a_registered_tool_carries_the_purpose_the_user_typed(self) -> None:
        """The Add/Edit dialog writes it as `description`; the catalog must read
        that exact key. Reading a key no writer produces makes every registered
        extractor look purposeless to the Agent choosing between them."""

        registration = tool_providers.validate_tool({
            "id": "uex", "title": "UE Extractor",
            "description": "Unreal 4.27 pak and uasset; SkeletalMesh crashes this build",
            "connection": {"kind": "cli", "command": "C:/tools/uex.exe"},
            "interface": {"operations": [_operation()]},
        })
        entry = next(item for item in tool_runner.declared_operations([registration])
                     if item.get("toolId") == "uex")
        self.assertIn("Unreal 4.27", entry["purpose"])

    def test_an_unproved_operation_is_not_offered_unless_asked_for(self) -> None:
        tool = {"id": "uex", "enabled": True, "title": "uex",
                "connection": {"kind": "cli", "command": "/x/uex"},
                "interface": {"operations": [
                    dict(_operation(), proved=None),
                    _operation(name="listed_and_proved")]}}
        offered = {item["name"] for item in tool_runner.declared_operations([tool])}
        self.assertEqual(offered - {entry["name"] for entry in
                                    tool_runner.BUILTIN_OPERATIONS},
                         {"listed_and_proved"})
        everything = {item["name"] for item in
                      tool_runner.declared_operations([tool], proved_only=False)}
        self.assertIn("extract_texture", everything)

    def test_a_disabled_tool_offers_nothing(self) -> None:
        tool = {"id": "uex", "enabled": False,
                "connection": {"kind": "cli", "command": "/x/uex"},
                "interface": {"operations": [_operation()]}}
        self.assertEqual(len(tool_runner.declared_operations([tool])), 3)

    def test_a_recipe_proved_against_an_older_program_is_reported_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / "uex"
            program.write_bytes(b"version 2")
            tool = {"id": "uex", "enabled": True,
                    "connection": {"kind": "cli", "command": str(program)},
                    "interface": {"operations": [
                        _operation(proved={"sample": "E:/a", "programSha256": "cd" * 32})]}}
            stale = tool_runner.stale_operations([tool])
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["operation"], "extract_texture")
        self.assertIn("re-run it", stale[0]["reason"])

    def test_a_recipe_with_no_recorded_program_digest_is_not_called_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / "uex"
            program.write_bytes(b"anything")
            tool = {"id": "uex", "enabled": True,
                    "connection": {"kind": "cli", "command": str(program)},
                    "interface": {"operations": [_operation()]}}
            self.assertEqual(tool_runner.stale_operations([tool]), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

    def test_a_recipe_that_names_the_program_is_not_run_twice(self) -> None:
        """A recipe recorded from a working command line starts with the bare name.

        Inserting the program in front of it produced `uex uex extract ...`; leaving
        the bare name in place would run whatever PATH resolves rather than the
        binary the user registered. It becomes the resolved program instead.
        """

        named = _operation(argv=["unpacker", "extract", "{assetPath}"])
        argv = tool_runner.build_argv(named, {"assetPath": "a.uasset"},
                                      program="C:/tools/unpacker.exe", output_dir="C:/out")
        self.assertEqual(argv, ["C:/tools/unpacker.exe", "extract", "a.uasset"])

        # A different program in argv[0] is still an argument, never the executable.
        other = _operation(argv=["C:/elsewhere/other.exe", "{assetPath}"])
        argv = tool_runner.build_argv(other, {"assetPath": "a.uasset"},
                                      program="C:/tools/unpacker.exe", output_dir="C:/out")
        self.assertEqual(argv[0], "C:/tools/unpacker.exe")
        self.assertEqual(argv[1], "C:/elsewhere/other.exe")
