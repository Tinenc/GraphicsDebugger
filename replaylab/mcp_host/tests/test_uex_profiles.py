"""uex on an already-unpacked tree: the profile is derived, never hand-written.

What these pin is the claim the design rests on. A tree that has been unpacked is
plain files on disk: CUE4Parse mounts such a directory directly, so there is no
pak to open, nothing to decrypt, and therefore no AES key and no ``.usmap`` to
configure. The only two facts uex still needs -- *which directory is the mount
point* and *which engine version to read the packages with* -- are both things
the Host already knows or can probe, so it writes them itself, and ``discover``
pins the version once it has proved one.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src import tool_providers, tool_runner, uex_profiles


def _unpacked_tree(root: Path) -> Path:
    """The shape extract-raw leaves behind: raw/<Game>/Content and raw/Engine/Content."""

    (root / "raw" / "SilverPalace" / "Content" / "ArtRes" / "MatRes").mkdir(parents=True)
    (root / "raw" / "Engine" / "Content").mkdir(parents=True)
    (root / "analysis").mkdir()
    asset = root / "raw" / "SilverPalace" / "Content" / "ArtRes" / "MatRes" / "MI_Skin.uasset"
    asset.write_bytes(b"\xc1\x83\x2a\x9e" + b"\0" * 64)
    return asset


class UexProfileTests(unittest.TestCase):
    def test_the_profile_is_generated_from_the_tree_with_no_keys(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = _unpacked_tree(root)
            config = root / "generated" / "profiles.json"

            facts = uex_profiles.ensure_profile(root, path=config)

            # The mount point is the directory whose children are <Game>/Content --
            # not the tree's top, where /Game/... would resolve against nothing.
            self.assertEqual(Path(facts["mountRoot"]), (root / "raw").resolve())
            self.assertEqual(facts["gameName"], "SilverPalace")
            self.assertTrue(facts["profile"].startswith("unpacked-"))
            written = json.loads(config.read_text(encoding="utf-8"))
            record = written["profiles"][facts["profile"]]
            # No paks, no keys, no usmap: this is the whole per-project configuration.
            self.assertEqual(record["keys"], [])
            self.assertIsNone(record["usmap"])
            self.assertEqual(Path(record["paksDir"]), (root / "raw").resolve())
            # The game-specific version is tried first, generic UE versions after it.
            self.assertEqual(record["game"], "GAME_SilverPalace")
            self.assertIn("latest", record["versions"])

            # A file is addressed the way CUE4Parse keys a loose file: relative to
            # the mounted directory.
            self.assertEqual(
                uex_profiles.uex_relative_path(facts["mountRoot"], asset),
                "SilverPalace/Content/ArtRes/MatRes/MI_Skin.uasset")

            # Asking again is the same profile, and the file is not rewritten.
            before = config.read_text(encoding="utf-8")
            again = uex_profiles.ensure_profile(root, path=config)
            self.assertEqual(again["profile"], facts["profile"])
            self.assertEqual(config.read_text(encoding="utf-8"), before)

    def test_discover_pins_the_version_it_proved(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _unpacked_tree(root)
            config = root / "generated" / "profiles.json"
            facts = uex_profiles.ensure_profile(root, path=config)

            weak = uex_profiles.record_discovery(
                facts["profile"], '{"version":"GAME_UE5_4","confidence":"Moderate"}',
                path=config)
            self.assertFalse(weak["pinned"])
            self.assertEqual(
                json.loads(config.read_text(encoding="utf-8"))["profiles"][facts["profile"]]["game"],
                "GAME_SilverPalace")

            strong = uex_profiles.record_discovery(
                facts["profile"],
                'mounting...\n{"profile":"x","version":"GAME_UE5_4","confidence":"Strong"}',
                path=config)
            self.assertTrue(strong["pinned"])
            record = json.loads(config.read_text(encoding="utf-8"))["profiles"][facts["profile"]]
            self.assertEqual(record["game"], "GAME_UE5_4")
            self.assertEqual(record["versions"], [])
            self.assertTrue(uex_profiles.ensure_profile(root, path=config)["pinned"])


def _package_header(file_version_ue4: int, file_version_ue5: int) -> bytes:
    """Just enough FPackageFileSummary for read_summary to reach the version fields."""

    import struct

    out = struct.pack("<II", 0x9E2A83C1, 0xFFFFFFF8)          # tag, legacy version -8
    out += struct.pack("<i", -1)                               # LegacyUE3Version
    out += struct.pack("<iii", file_version_ue4, file_version_ue5, 0)
    out += struct.pack("<i", 0)                                # custom version count
    out += struct.pack("<i", 256)                              # TotalHeaderSize
    out += struct.pack("<i", 0)                                # FolderName (empty FString)
    out += struct.pack("<I", 0x80000000)                       # PKG_FilterEditorOnly
    out += struct.pack("<ii", 0, 256)                          # NameCount, NameOffset
    if file_version_ue5 >= 1004:
        out += struct.pack("<ii", 0, 0)                        # soft object paths
    if file_version_ue4 >= 459:
        out += struct.pack("<ii", 0, 0)                        # gatherable text
    out += struct.pack("<iiiii", 0, 256, 0, 256, 256)          # exports, imports, depends
    return out.ljust(512, b"\0")


class EngineVersionTests(unittest.TestCase):
    """The version is read from the files, not guessed and not brute-forced."""

    def test_object_versions_map_to_the_engine_that_writes_them(self):
        # CUE4Parse's EGame.GetVersion, read backwards (5.4 writes UE5 version 1012).
        self.assertEqual(uex_profiles.game_for_package_versions(522, 1012), "GAME_UE5_4")
        self.assertEqual(uex_profiles.game_for_package_versions(522, 1008), "GAME_UE5_1")
        self.assertEqual(uex_profiles.game_for_package_versions(522, 1004), "GAME_UE5_0")
        self.assertEqual(uex_profiles.game_for_package_versions(522, 0), "GAME_UE4_27")
        self.assertEqual(uex_profiles.game_for_package_versions(518, 0), "GAME_UE4_25")
        # A number between two listed ones still places the cook.
        self.assertEqual(uex_profiles.game_for_package_versions(519, 0), "GAME_UE4_25")
        self.assertEqual(uex_profiles.game_for_package_versions(0, 0), "")

    def test_one_asset_decides_the_version_of_the_tree(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _unpacked_tree(root)
            content = root / "raw" / "SilverPalace" / "Content" / "ArtRes" / "MatRes"
            (content / "MI_Skin.uasset").write_bytes(_package_header(522, 1012))

            detected = uex_profiles.detect_engine_version(root / "raw")
            self.assertEqual(detected["version"], "GAME_UE5_4")
            self.assertEqual(detected["evidence"][0]["fileVersionUE5"], 1012)

            config = root / "generated" / "profiles.json"
            facts = uex_profiles.ensure_profile(root, path=config)
            self.assertEqual(facts["detectedVersion"], "GAME_UE5_4")
            # The project's own EGame stays first (custom serialization), what the
            # headers said comes next, generic newest-first after that.
            self.assertEqual(facts["game"], "GAME_SilverPalace")
            self.assertEqual(facts["versions"][0], "GAME_UE5_4")
            self.assertEqual(facts["mountRootSource"], "found under the configured root")

    def test_an_answer_from_the_user_ends_the_probing(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _unpacked_tree(root)
            config = root / "generated" / "profiles.json"
            facts = uex_profiles.ensure_profile(root, path=config)

            uex_profiles.set_version(facts["profile"], "GAME_UE5_6", path=config)
            record = json.loads(config.read_text(encoding="utf-8"))["profiles"][facts["profile"]]
            self.assertEqual(record["game"], "GAME_UE5_6")
            self.assertEqual(record["versions"], [])
            self.assertTrue(record["pinned"])
            with self.assertRaises(ValueError):
                uex_profiles.set_version(facts["profile"], "rm -rf /", path=config)

    def test_a_tree_already_at_its_mount_point_is_mounted_as_configured(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "SilverPalace" / "Content").mkdir(parents=True)
            facts = uex_profiles.ensure_profile(root, path=root / "p.json")
            self.assertEqual(Path(facts["mountRoot"]), root.resolve())
            self.assertEqual(facts["mountRootSource"], "configured")


class AgentAgreementTests(unittest.TestCase):
    """The AgentHost decides whether to attach uex's MCP server by reading the same
    generated file the Host writes. Two spellings of that path would mean the server
    is attached with a config that has no profiles, which uex refuses to start on."""

    def test_both_sides_name_the_same_generated_profiles_file(self):
        from replaylab_agent import builtin_unpackers

        with TemporaryDirectory() as temporary:
            import os

            previous = os.environ.get(uex_profiles.ENV_PROFILES_PATH)
            os.environ[uex_profiles.ENV_PROFILES_PATH] = str(Path(temporary) / "p.json")
            try:
                self.assertEqual(uex_profiles.profiles_path(),
                                 builtin_unpackers.generated_uex_profiles_path())
            finally:
                if previous is None:
                    os.environ.pop(uex_profiles.ENV_PROFILES_PATH, None)
                else:
                    os.environ[uex_profiles.ENV_PROFILES_PATH] = previous
        # ... and without the override, too (LOCALAPPDATA / XDG, same layout).
        self.assertEqual(uex_profiles.profiles_path(),
                         builtin_unpackers.generated_uex_profiles_path())


class HostSlotTests(unittest.TestCase):
    """The uex slots are the Host's to fill: declared by nobody, passed by nobody."""

    def _operation(self, argv):
        return tool_providers.validate_operation({
            "kind": "cli", "role": "general", "name": "uex_search", "argv": argv,
            "description": "search", "timeoutSeconds": 30,
            "inputSchema": {"type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"]},
            "result": {"kind": "stdout-text", "successWhen": {"exitCode": 0}},
            "proved": {"sample": "test"},
        })

    def test_host_slots_are_accepted_and_filled(self):
        operation = self._operation(
            ["--config", "{uexConfig}", "--profile", "{uexProfile}", "search", "{query}"])
        self.assertEqual(sorted(tool_runner.host_slots_needed(operation)),
                         ["uexConfig", "uexProfile"])
        argv = tool_runner.build_argv(
            operation, {"query": "Skin"}, program="/bin/uex", output_dir="/out",
            host_slots={"uexConfig": "/generated/profiles.json", "uexProfile": "unpacked-abcd1234"})
        self.assertEqual(argv, ["/bin/uex", "--config", "/generated/profiles.json",
                                "--profile", "unpacked-abcd1234", "search", "Skin"])

    def test_a_caller_cannot_pass_a_host_slot(self):
        operation = self._operation(
            ["--config", "{uexConfig}", "--profile", "{uexProfile}", "search", "{query}"])
        with self.assertRaises(tool_runner.ToolRunError):
            tool_runner.build_argv(
                operation, {"query": "Skin", "uexProfile": "somebody-elses"},
                program="/bin/uex", output_dir="/out",
                host_slots={"uexConfig": "/c.json", "uexProfile": "unpacked-abcd1234"})

    def test_an_unfilled_host_slot_is_refused_before_anything_runs(self):
        operation = self._operation(["--config", "{uexConfig}", "search", "{query}"])
        with self.assertRaises(tool_runner.ToolRunError) as raised:
            tool_runner.build_argv(operation, {"query": "Skin"}, program="/bin/uex",
                                   output_dir="/out")
        self.assertIn("uexConfig", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
