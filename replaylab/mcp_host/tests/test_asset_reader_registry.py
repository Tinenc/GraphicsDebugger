"""Readers are proved against the tree, remembered per project, and honest when absent."""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import server
from src.asset_readers import PRESETS, presets_for_engine
from src.symbols.engines import unity_serialized
from tests.test_shader_asset_retrieval import crow_instance, crow_material


def _unity_serialized_file(class_ids: list[int], version: str = "2022.3.16f1") -> bytes:
    """A SerializedFile header: big-endian sizes, then metadata in the declared order.

    The endianness byte at offset 16 governs everything after it; on PC builds that is
    little-endian while the four leading sizes stay big-endian.
    """

    payload = bytearray()
    payload += version.encode("ascii") + b"\x00"
    payload += struct.pack("<i", 19)                  # target platform (StandaloneWindows64)
    payload += b"\x00"                                # type tree stripped
    payload += struct.pack("<i", len(class_ids))
    for class_id in class_ids:
        payload += struct.pack("<i", class_id)
        payload += b"\x00" + struct.pack("<h", -1)    # stripped + script type index
        payload += b"\x00" * 16                       # type hash
    header = struct.pack(">IIII", len(payload), len(payload) + 20, 17, 20)
    header += struct.pack("<i", 0)                    # little-endian metadata + reserved
    return header + bytes(payload)


def _run_tool(coroutine):
    """Host tools that may call an external reader are awaited, never blocked on."""

    return asyncio.run(coroutine)


class UnityReaderTests(unittest.TestCase):
    def test_a_serialized_file_is_classified_by_its_object_classes(self) -> None:
        parsed = unity_serialized.parse(_unity_serialized_file([28, 21]))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["engine"], "unity")
        self.assertEqual(parsed["kind"], "texture")
        self.assertEqual(parsed["className"], "Texture2D")
        self.assertEqual(parsed["unityVersion"], "2022.3.16f1")

    def test_a_mesh_file_reads_as_a_mesh(self) -> None:
        parsed = unity_serialized.parse(_unity_serialized_file([43]))
        self.assertEqual(parsed["kind"], "mesh")

    def test_bundles_are_recognised_as_containers(self) -> None:
        blob = b"UnityFS\x00" + struct.pack(">I", 6) + b"5.x.x\x00" + b"2022.3.16f1\x00"
        parsed = unity_serialized.parse(blob)
        self.assertEqual(parsed["kind"], "package")
        self.assertEqual(parsed["container"], "UnityFS")
        self.assertIn("extractor", parsed["note"])

    def test_unrelated_bytes_are_not_claimed(self) -> None:
        self.assertIsNone(unity_serialized.parse(b"\x00" * 64))


class ReaderVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.project = self.root / "project"
        (self.project / "dictionaries").mkdir(parents=True)
        self.tree = self.root / "tree"
        (self.tree / "Content").mkdir(parents=True)
        patcher = mock.patch.object(
            server, "_active_project_service",
            return_value=mock.Mock(status=mock.Mock(return_value={
                "assetRootConfigured": True, "assetRootAvailable": True,
                "assetRoot": str(self.tree)})))
        patcher.start()
        self.addCleanup(patcher.stop)
        catalog = mock.patch.object(
            server, "_active_asset_catalog_context",
            return_value=(None, {}, self.tree, self.project / "dictionaries" / "assets.sqlite3"))
        catalog.start()
        self.addCleanup(catalog.stop)
        self.addCleanup(self._directory.cleanup)

    def test_a_readable_tree_selects_a_reader_and_remembers_it(self) -> None:
        (self.tree / "Content" / "MI_A.uasset").write_bytes(crow_instance())
        (self.tree / "Content" / "M_A.uasset").write_bytes(crow_material())
        payload = json.loads(_run_tool(server.verify_unpacked_asset_readers(sample_size=8)))
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["selectedReader"], "unreal-package")
        self.assertIn("material", payload["kindsRead"])
        # The decision is written into the project and reused without probing again.
        remembered = server._load_asset_reader_selection()
        self.assertEqual(remembered["reader"], "unreal-package")
        self.assertEqual(remembered["assetRoot"], str(self.tree))
        self.assertTrue(remembered["decidedAt"])

    def test_an_unreadable_tree_says_so_and_offers_the_presets(self) -> None:
        (self.tree / "Content" / "a.bin").write_bytes(b"\x7f\x11\x22\x33" * 32)
        (self.tree / "Content" / "b.bin").write_bytes(b"\x7f\x44\x55\x66" * 32)
        payload = json.loads(_run_tool(server.verify_unpacked_asset_readers(sample_size=4)))
        self.assertEqual(payload["state"], "no-reader")
        self.assertNotIn("requiredAction", payload)
        self.assertTrue(any(item["id"] == "uex" for item in payload["presets"]))
        self.assertNotIn("next", payload)
        self.assertIsNone(server._load_asset_reader_selection())


    def test_presets_are_ordered_by_the_detected_engine(self) -> None:
        unreal = presets_for_engine("unreal")
        self.assertEqual(unreal[0]["engines"], ["unreal"])
        unity = presets_for_engine("unity")
        self.assertEqual(unity[0]["engines"], ["unity"])
        self.assertEqual(len(presets_for_engine("")), len(PRESETS))


class ToolInterfaceDiscoveryTests(unittest.TestCase):
    """Registering a tool costs one path; the interface is discovered and written back."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        base = Path(self._directory.name)
        self.root = base / "assets"
        self.tools_dir = base / "tools"
        self.registry = base / "tools.json"
        (self.root / "Content").mkdir(parents=True)
        self.tools_dir.mkdir()
        for name in ("A.uasset", "B.uasset", "C.uasset"):
            (self.root / "Content" / name).write_bytes(b"\xc1\x83\x2a\x9e" + b"\0" * 64)
        self.addCleanup(self._directory.cleanup)

    def _register(self, argv: list[str] | None = None) -> None:
        from src import tool_providers

        tool_providers.save_tools([{
            "id": "mine", "title": "Mine", "roles": ["asset-reader"],
            "connection": {"kind": "cli", "command": sys.executable},
        }], self.registry)

    def test_umodel_discovery_uses_help_only_and_never_guesses_viewer_invocations(self) -> None:
        from src.asset_readers import candidate_invocations

        self.assertEqual(candidate_invocations("umodel_64.exe", "  view  open a viewer"), ())
        completed = mock.Mock(stdout=b"UE Viewer usage", stderr=b"", returncode=0)
        with mock.patch.object(server.subprocess, "run", return_value=completed) as run:
            self.assertIn("UE Viewer", server._probe_reader_help("umodel_64.exe"))
        self.assertEqual(run.call_args.args[0], ["umodel_64.exe", "-help"])

    def test_builtin_batch_interface_is_returned_without_probing_or_erasing_it(self) -> None:
        from src import tool_providers

        tool_providers.save_tools([{
            "id": "mine", "builtinKind": "umodel", "source": "bundled",
            "roles": ["general"],
            "connection": {"kind": "cli", "command": "umodel_64.exe"},
            "interface": {"source": "builtin:umodel", "operations": [{
                "name": "umodel_list", "kind": "cli", "role": "general",
                "argv": ["-list", "{path}"], "proved": {"sample": "built-in recipe: umodel"},
                "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                                "required": ["path"]},
            }]},
        }], self.registry)
        before = self.registry.read_bytes()
        with mock.patch.object(server.subprocess, "run") as run:
            result = self._discover()
        self.assertEqual(result["state"], "ready")
        self.assertFalse(result["assetReader"])
        run.assert_not_called()
        self.assertEqual(self.registry.read_bytes(), before)

    def _script(self, body: str) -> Path:
        script = self.tools_dir / "reader.py"
        script.write_text(body, encoding="utf-8")
        return script

    def _discover(self, **kwargs: object) -> dict:
        from src import tool_providers

        with mock.patch.object(server, "_unpacked_asset_root", return_value=(self.root, {})), \
                mock.patch.object(tool_providers, "default_tools_path",
                                  return_value=self.registry):
            return json.loads(asyncio.run(server.discover_tool_interface("mine", **kwargs)))

    def test_a_proved_invocation_is_written_back_and_gives_the_tool_its_role(self) -> None:
        script = self._script(
            "import json, sys\n"
            "if len(sys.argv) == 3 and sys.argv[1] == 'preview':\n"
            "    print(json.dumps({'kind': 'texture', 'className': 'Texture2D'}))\n"
            "else:\n"
            "    sys.exit(2)\n")
        self._register()
        result = self._discover(extra_argv=[str(script), "preview", "{path}"])
        self.assertEqual(result["state"], "ready")
        operation = result["interface"]["operations"][0]
        self.assertEqual(operation["kind"], "cli")
        self.assertIn("preview", operation["argv"])
        self.assertEqual(operation["patterns"], ["*.uasset"])
        self.assertTrue(operation["proved"]["sample"])
        # Written back, so the next task starts from the answer.
        from src import tool_providers

        stored, _ = tool_providers.load_tools(self.registry)
        self.assertIsNotNone(tool_providers.asset_reader_operation(stored[0]))

    def test_a_tool_that_answers_nothing_asks_for_help_instead_of_guessing(self) -> None:
        self._script("import sys\nsys.stderr.write('usage: reader <path>\\n')\nsys.exit(1)\n")
        self._register()
        result = self._discover(extra_argv=["-c", "import sys; sys.exit(3)"])
        self.assertEqual(result["state"], "no-operation")
        self.assertTrue(result["attempts"])
        self.assertNotIn("next", result)

    def test_a_missing_executable_says_so_without_touching_the_project(self) -> None:
        from src import tool_providers

        tool_providers.save_tools([{
            "id": "mine", "roles": ["asset-reader"],
            "connection": {"kind": "cli", "command": str(self.tools_dir / "absent.exe")},
        }], self.registry)
        self.assertEqual(self._discover()["state"], "not-found")

    def test_the_mcp_operation_that_reads_one_asset_is_the_one_picked(self) -> None:
        """tools/list is a typed answer, so nothing has to be guessed."""

        listing = [
            {"name": "export_all", "description": "bulk export", "arguments": ["out"]},
            {"name": "preview_asset", "description": "one asset as JSON",
             "arguments": ["path"]},
            {"name": "scan_project", "description": "scan everything", "arguments": ["path"]},
        ]
        ranked = server._ranked_asset_reader_candidates(listing)
        self.assertEqual(ranked[0]["name"], "preview_asset")
        self.assertEqual(ranked[0]["pathArgument"], "path")
        self.assertNotIn("export_all", [item["name"] for item in ranked])

    def test_discovery_reports_installed_tools_without_registering_them(self) -> None:
        from src.asset_readers import discover_installed_readers

        (self.tools_dir / "uex.exe").write_bytes(b"MZ")
        found = discover_installed_readers([self.tools_dir])
        self.assertEqual([item["id"] for item in found], ["uex"])
        self.assertTrue(found[0]["description"])


class ConfigurationToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.tools = self.root / "agent-tools.json"
        self.readers = self.root / "asset-readers.json"
        self.skills = self.root / "skills"
        (self.skills / "my-skill").mkdir(parents=True)
        (self.skills / "my-skill" / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: Do the thing.\n---\nStep 1.\n", encoding="utf-8")
        self.addCleanup(self._directory.cleanup)

    def test_reading_reports_discovered_skills_and_their_state(self) -> None:
        from src.replaylab_configuration import apply_configuration_change, read_agent_tools

        apply_configuration_change(add_skill_roots=[str(self.skills)],
                                   tools_path=self.tools, readers_path=self.readers)
        view = read_agent_tools(self.tools)
        self.assertEqual([item["id"] for item in view["skills"]], ["my-skill"])
        self.assertEqual(view["skills"][0]["description"], "Do the thing.")
        self.assertTrue(view["skills"][0]["enabled"])

    def test_an_agent_can_register_a_reader_and_a_server_and_disable_a_skill(self) -> None:
        from src.replaylab_configuration import apply_configuration_change, read_agent_tools

        result = apply_configuration_change(
            add_skill_roots=[str(self.skills)],
            disable_skills=["my-skill"],
            upsert_mcp_servers=[{"id": "uex", "command": "uex.exe", "args": ["mcp"]}],
            upsert_asset_readers=[{"id": "uex", "command": "uex.exe",
                                   "args": ["preview", "{path}"], "patterns": ["*.uasset"]}],
            tools_path=self.tools, readers_path=self.readers)
        self.assertEqual(result["rejected"], [])
        self.assertEqual(result["mcpServers"], ["uex"])
        self.assertEqual(result["assetReaders"], ["uex"])
        view = read_agent_tools(self.tools)
        self.assertEqual(view["disabledSkills"], ["my-skill"])
        self.assertFalse(view["skills"][0]["enabled"])

    def test_core_skill_can_be_disabled(self) -> None:
        from src.replaylab_configuration import apply_configuration_change, read_agent_tools

        result = apply_configuration_change(disable_skills=["replaylab-core"],
                                            tools_path=self.tools, readers_path=self.readers)
        self.assertEqual(result["rejected"], [])
        self.assertIn("disabled Skill replaylab-core", result["changed"])
        self.assertIn("replaylab-core", read_agent_tools(self.tools)["disabledSkills"])

    def test_a_bad_registration_is_reported_not_written(self) -> None:
        from src.replaylab_configuration import apply_configuration_change

        result = apply_configuration_change(
            upsert_asset_readers=[{"id": "no-claim", "command": "x"}],
            add_skill_roots=["relative/path"],
            tools_path=self.tools, readers_path=self.readers)
        self.assertEqual(result["assetReaders"], [])
        self.assertEqual(len(result["rejected"]), 2)

    def test_the_write_tool_requires_a_quoted_request(self) -> None:
        payload = json.loads(server.update_bread_configuration(enable_skills=["x"]))
        self.assertIn("user_confirmation", payload["error"])

    def test_a_skill_written_from_a_task_lands_in_the_user_folder_and_is_offered(self) -> None:
        """"Write me a Skill for this" must not write into the installed package."""

        from src.replaylab_configuration import (
            apply_configuration_change,
            read_agent_tools,
            user_skill_overlay_root,
        )

        document = ("---\nname: capture-triage\ndescription: Triage a capture.\n---\n"
                    "1. Read the event list.\n")
        result = apply_configuration_change(
            write_skills=[{"id": "capture-triage", "content": document}],
            tools_path=self.tools, readers_path=self.readers)
        overlay = user_skill_overlay_root(self.tools)
        written = overlay / "capture-triage" / "SKILL.md"
        self.assertEqual(result["writtenSkills"], [str(written)])
        self.assertEqual(written.read_text(encoding="utf-8"), document)
        # The folder is registered, so the next task actually sees the Skill.
        self.assertIn(str(overlay), result["skillRoots"])
        view = read_agent_tools(self.tools)
        self.assertIn("capture-triage", [skill["id"] for skill in view["skills"]])

    def test_an_existing_skill_is_not_overwritten_without_being_asked(self) -> None:
        from src.replaylab_configuration import apply_configuration_change, user_skill_overlay_root

        first = "---\nname: notes\ndescription: One.\n---\nA.\n"
        apply_configuration_change(write_skills=[{"id": "notes", "content": first}],
                                   tools_path=self.tools, readers_path=self.readers)
        second = "---\nname: notes\ndescription: Two.\n---\nB.\n"
        refused = apply_configuration_change(write_skills=[{"id": "notes", "content": second}],
                                             tools_path=self.tools, readers_path=self.readers)
        document = user_skill_overlay_root(self.tools) / "notes" / "SKILL.md"
        self.assertEqual(refused["writtenSkills"], [])
        self.assertTrue(any("replace" in item for item in refused["rejected"]))
        self.assertEqual(document.read_text(encoding="utf-8"), first)
        replaced = apply_configuration_change(
            write_skills=[{"id": "notes", "content": second, "replace": True}],
            tools_path=self.tools, readers_path=self.readers)
        self.assertEqual(replaced["writtenSkills"], [str(document)])
        self.assertEqual(document.read_text(encoding="utf-8"), second)

    def test_an_unusable_skill_is_reported_and_nothing_is_written(self) -> None:
        from src.replaylab_configuration import apply_configuration_change, user_skill_overlay_root

        result = apply_configuration_change(
            write_skills=[{"id": "Bad Id", "content": "x"}, {"id": "empty", "content": "   "}],
            tools_path=self.tools, readers_path=self.readers)
        self.assertEqual(result["writtenSkills"], [])
        self.assertEqual(len(result["rejected"]), 2)
        self.assertFalse((user_skill_overlay_root(self.tools) / "empty").exists())


if __name__ == "__main__":
    unittest.main()
