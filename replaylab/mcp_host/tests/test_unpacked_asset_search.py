"""Searching the asset root needs no catalog, no scan and no waiting."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from src import server
from tests.test_shader_asset_retrieval import crow_instance, crow_material


def _run_tool(coroutine):
    """Host tools that may call an external reader are awaited, never blocked on."""

    return asyncio.run(coroutine)


class UnpackedAssetSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        (self.root / "Content" / "Characters").mkdir(parents=True)
        (self.root / "Content" / "Environment").mkdir(parents=True)
        (self.root / "Content" / "Characters" / "M_Hero_Body.uasset").write_bytes(
            b"\x00\x01Material\x00" + "T_Hero_Body_BC".encode("ascii") + b"\x00")
        (self.root / "Content" / "Characters" / "T_Hero_Body_BC.uasset").write_bytes(b"Texture2D\x00")
        (self.root / "Content" / "Environment" / "M_Rock.uasset").write_bytes(b"Material\x00")
        (self.root / "Content" / "readme.txt").write_text("notes", encoding="utf-8")
        patcher = mock.patch.object(
            server, "_active_project_service",
            return_value=mock.Mock(status=mock.Mock(return_value={
                "assetRootConfigured": True, "assetRootAvailable": True,
                "assetRoot": str(self.root)})))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._directory.cleanup)

    def test_name_and_extension_search_finds_assets_without_any_index(self) -> None:
        result = json.loads(_run_tool(server.search_unpacked_assets(name_contains="hero", extensions=["uasset"])))
        names = sorted(item["relativePath"] for item in result["matches"])
        self.assertEqual(names, ["Content/Characters/M_Hero_Body.uasset",
                                 "Content/Characters/T_Hero_Body_BC.uasset"])
        self.assertGreater(result["scannedFiles"], 0)
        self.assertFalse(result["truncated"])
        self.assertNotIn("assetBinding", result)
        self.assertNotIn("readerAttempt", result)

    def test_content_search_finds_the_material_that_references_a_texture(self) -> None:
        result = json.loads(_run_tool(server.search_unpacked_assets(text_contains="T_Hero_Body_BC")))
        found = sorted(item["relativePath"] for item in result["matches"])
        self.assertIn("Content/Characters/M_Hero_Body.uasset", found)
        self.assertTrue(all("content contains" in item["why"] for item in result["matches"]))

    def test_a_subtree_and_a_glob_narrow_the_walk(self) -> None:
        result = json.loads(_run_tool(server.search_unpacked_assets(
            under="Content/Environment", name_glob="*.uasset")))
        self.assertEqual([item["relativePath"] for item in result["matches"]],
                         ["Content/Environment/M_Rock.uasset"])

    def test_results_are_bounded_and_resumable(self) -> None:
        first = json.loads(_run_tool(server.search_unpacked_assets(limit=1, extensions=["uasset"])))
        self.assertEqual(len(first["matches"]), 1)
        self.assertTrue(first["cursor"])
        second = json.loads(_run_tool(server.search_unpacked_assets(
            limit=5, extensions=["uasset"], cursor=first["cursor"])))
        self.assertNotIn(first["matches"][0]["relativePath"],
                         [item["relativePath"] for item in second["matches"]])

    def test_the_walk_cannot_leave_the_configured_root(self) -> None:
        for escape in ("../secrets", "/etc", "Content/../../outside", "C:/Windows"):
            payload = json.loads(_run_tool(server.search_unpacked_assets(under=escape)))
            self.assertIn("error", payload)
            listing = json.loads(server.list_unpacked_asset_directory(escape))
            self.assertIn("error", listing)
            read = json.loads(_run_tool(server.read_unpacked_asset(escape)))
            self.assertIn("error", read)

    def test_listing_shows_the_layout(self) -> None:
        listing = json.loads(server.list_unpacked_asset_directory("Content"))
        self.assertEqual(listing["directories"], ["Characters", "Environment"])
        self.assertEqual([item["name"] for item in listing["files"]], ["readme.txt"])

    def test_reading_an_asset_returns_its_embedded_names(self) -> None:
        payload = json.loads(_run_tool(server.read_unpacked_asset("Content/Characters/M_Hero_Body.uasset")))
        self.assertIn("T_Hero_Body_BC", payload["strings"])
        self.assertIn("Material", " ".join(payload["strings"]))
        self.assertEqual(payload["relativePath"], "Content/Characters/M_Hero_Body.uasset")
        self.assertFalse(payload["moreBytes"])
        expected = self.root / payload["relativePath"]
        self.assertEqual(payload["contentSha256"], hashlib.sha256(expected.read_bytes()).hexdigest())
        self.assertNotIn("assetContentProofs", payload)
        self.assertNotIn("assetBinding", payload)

    def test_a_missing_asset_root_is_reported_not_raised(self) -> None:
        with mock.patch.object(server, "_active_project_service",
                               return_value=mock.Mock(status=mock.Mock(return_value={
                                   "assetRootConfigured": False}))):
            payload = json.loads(_run_tool(server.search_unpacked_assets(name_contains="x")))
            self.assertIn("error", payload)

    def test_search_walk_yields_to_concurrent_runtime_status(self) -> None:
        from src import host_runtime

        host_runtime.reset_long_tool_state()
        self.addCleanup(host_runtime.reset_long_tool_state)
        started = threading.Event()
        release = threading.Event()
        real_walk = server.os.walk

        def slow_walk(*args, **kwargs):
            started.set()
            if not release.wait(2.0):
                raise AssertionError("search worker was not released")
            yield from real_walk(*args, **kwargs)

        async def exercise() -> dict:
            task = asyncio.create_task(
                server.search_unpacked_assets(name_contains="hero")
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
            before = time.monotonic()
            status = json.loads(server.get_host_runtime_status())
            self.assertLess(time.monotonic() - before, 0.5)
            self.assertEqual(status["toolStatus"], "running")
            self.assertEqual(status["currentTool"]["name"], "search_unpacked_assets")
            release.set()
            return json.loads(await asyncio.wait_for(task, 2.0))

        try:
            with mock.patch.object(server.os, "walk", side_effect=slow_walk):
                payload = asyncio.run(exercise())
        finally:
            release.set()
        self.assertTrue(payload["matches"])


if __name__ == "__main__":
    unittest.main()


class AssetReaderTests(unittest.TestCase):
    """Cooked assets are identified by reading the container, never by extension."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        (self.root / "Content").mkdir()
        # A real cooked package: the extension says nothing, the header declares the class.
        (self.root / "Content" / "MI_Rock.uasset").write_bytes(crow_instance())
        (self.root / "Content" / "M_Base.uasset").write_bytes(crow_material())
        (self.root / "Content" / "T_Rock_D.dds").write_bytes(b"DDS " + b"\x00" * 124)
        (self.root / "Content" / "notes.ini").write_text("[a]\nb=1\n", encoding="utf-8")
        (self.root / "Content" / "mystery.bin").write_bytes(b"\x7f\x01\x02\x03")
        patcher = mock.patch.object(
            server, "_active_project_service",
            return_value=mock.Mock(status=mock.Mock(return_value={
                "assetRootConfigured": True, "assetRootAvailable": True,
                "assetRoot": str(self.root)})))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._directory.cleanup)

    def test_a_cooked_package_is_read_not_guessed(self) -> None:
        payload = json.loads(_run_tool(server.describe_unpacked_asset("Content/MI_Rock.uasset")))
        self.assertEqual(payload["reader"], "unreal-package")
        self.assertEqual(payload["engine"], "unreal")
        # The class comes from the package header, so ".uasset" never decides the kind.
        self.assertEqual(payload["className"], "MaterialInstanceConstant")
        self.assertEqual(payload["kind"], "material")
        expected = self.root / "Content" / "MI_Rock.uasset"
        self.assertEqual(payload["relativePath"], "Content/MI_Rock.uasset")
        self.assertEqual(payload["bytes"], expected.stat().st_size)
        self.assertEqual(payload["contentSha256"], hashlib.sha256(expected.read_bytes()).hexdigest())
        self.assertNotIn("authorizationStatus", payload)
        self.assertNotIn("assetContentProofs", payload)
        self.assertNotIn("assetBinding", payload)

    def test_standalone_containers_are_read_by_magic(self) -> None:
        payload = json.loads(_run_tool(server.describe_unpacked_asset("Content/T_Rock_D.dds")))
        self.assertEqual(payload["reader"], "container-magic")
        self.assertEqual(payload["kind"], "texture")
        text = json.loads(_run_tool(server.describe_unpacked_asset("Content/notes.ini")))
        self.assertEqual(text["kind"], "metadata")

    def test_an_unknown_container_says_so_and_points_at_registration(self) -> None:
        payload = json.loads(_run_tool(server.describe_unpacked_asset("Content/mystery.bin")))
        self.assertEqual(payload["kind"], "unknown")
        self.assertEqual(payload["reader"], "none")
        self.assertIn("add the tool that reads it", payload["note"])

    def test_search_filters_on_the_kind_the_reader_determined(self) -> None:
        materials = json.loads(_run_tool(server.search_unpacked_assets(kinds=["material"])))
        self.assertEqual(sorted(item["relativePath"] for item in materials["matches"]),
                         ["Content/MI_Rock.uasset", "Content/M_Base.uasset"])
        textures = json.loads(_run_tool(server.search_unpacked_assets(kinds=["texture"])))
        self.assertEqual([item["relativePath"] for item in textures["matches"]],
                         ["Content/T_Rock_D.dds"])

    def test_a_registered_tool_is_asked_only_about_files_it_claims(self) -> None:
        from src import tool_providers

        script = self.root.parent / "reader.py"
        script.write_text(
            "import json, sys\n"
            "print(json.dumps({'kind': 'mesh', 'className': 'CustomMesh',\n"
            "                  'metadata': {'path': sys.argv[-1]}}))\n",
            encoding="utf-8")
        registration = tool_providers.validate_tool({
            "id": "custom", "title": "Custom", "roles": ["asset-reader"],
            "connection": {"kind": "cli", "command": sys.executable},
            "interface": {"source": "test", "operations": [{
                "role": "asset-reader", "kind": "cli",
                "argv": [str(script), "{path}"], "patterns": ["*.bin"],
                "proved": {"sample": "Content/mystery.bin"},
            }]},
        })
        with mock.patch.object(server, "_registered_tools", return_value=(registration,)):
            claimed = json.loads(_run_tool(server.describe_unpacked_asset("Content/mystery.bin")))
            self.assertEqual(claimed["reader"], "custom")
            self.assertEqual(claimed["kind"], "mesh")
            self.assertEqual(claimed["readerStatus"], "parsed")
            # The package is still read by the built-in parser: the pattern excludes it,
            # and a built-in answer is never overridden by a tool.
            package = json.loads(_run_tool(server.describe_unpacked_asset("Content/MI_Rock.uasset")))
            self.assertEqual(package["reader"], "unreal-package")

    def test_an_unproved_operation_is_never_used(self) -> None:
        """A shape nobody watched work is not an interface."""

        from src import tool_providers

        registration = tool_providers.validate_tool({
            "id": "guessed", "roles": ["asset-reader"],
            "connection": {"kind": "cli", "command": sys.executable},
            "interface": {"operations": [{"role": "asset-reader", "kind": "cli",
                                          "argv": ["{path}"], "patterns": ["*.bin"]}]},
        })
        self.assertIsNone(tool_providers.asset_reader_operation(registration))
        with mock.patch.object(server, "_registered_tools", return_value=(registration,)):
            payload = json.loads(_run_tool(server.describe_unpacked_asset("Content/mystery.bin")))
        self.assertEqual(payload["kind"], "unknown")

    def test_reader_registrations_are_validated(self) -> None:
        from src.asset_readers import AssetReaderError, validate_reader

        valid = validate_reader({"id": "Custom", "command": "python", "args": ["r.py", "{path}"],
                                 "patterns": ["*.bin"]})
        self.assertEqual(valid["id"], "custom")
        self.assertEqual(valid["timeoutSeconds"], 20.0)
        for invalid in (
            {"id": "bad id", "command": "x", "patterns": ["*"]},
            {"id": "ok", "patterns": ["*"]},
            {"id": "ok", "command": "x"},
            {"id": "ok", "command": "x", "patterns": ["*"], "magic": "zz"},
            {"id": "ok", "command": "x", "patterns": ["*"], "timeoutSeconds": 9999},
        ):
            with self.assertRaises(AssetReaderError):
                validate_reader(invalid)

    def test_the_tool_catalog_reports_what_can_be_read(self) -> None:
        payload = json.loads(server.list_bread_tools())
        built_in = {item["id"] for item in payload["builtInReaders"]}
        self.assertIn("unreal-package", built_in)
        self.assertIn("unity-serialized", built_in)
        self.assertIn("interface", payload["registration"])
        self.assertTrue(payload["presets"]["mcp"])


class AssetRelationTests(unittest.TestCase):
    """Following one confirmed match into its neighbours is how a material is found."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        (self.root / "Content").mkdir()
        (self.root / "Content" / "M_CrowFeather.uasset").write_bytes(crow_material())
        (self.root / "Content" / "MI_CrowFeather.uasset").write_bytes(crow_instance())
        patcher = mock.patch.object(
            server, "_active_project_service",
            return_value=mock.Mock(status=mock.Mock(return_value={
                "assetRootConfigured": True, "assetRootAvailable": True,
                "assetRoot": str(self.root)})))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._directory.cleanup)

    def test_references_and_referenced_by_walk_both_directions(self) -> None:
        payload = json.loads(_run_tool(server.find_unpacked_asset_relations("Content/MI_CrowFeather.uasset")))
        self.assertEqual(payload["classification"]["kind"], "material")
        self.assertEqual(payload["classification"]["className"], "MaterialInstanceConstant")
        # The instance's parent material is found by name in the tree.
        self.assertIn("Content/M_CrowFeather.uasset",
                      [item["relativePath"] for item in payload["references"]])
        self.assertNotIn("next", payload)
        expected = self.root / "Content" / "MI_CrowFeather.uasset"
        self.assertEqual(payload["contentSha256"], hashlib.sha256(expected.read_bytes()).hexdigest())
        self.assertNotIn("assetContentProofs", payload)
        self.assertNotIn("assetBinding", payload)

    def test_relations_stay_inside_the_asset_root(self) -> None:
        payload = json.loads(_run_tool(server.find_unpacked_asset_relations("../escape.uasset")))
        self.assertIn("error", payload)

    def test_relation_walk_yields_to_concurrent_runtime_status(self) -> None:
        from src import host_runtime

        host_runtime.reset_long_tool_state()
        self.addCleanup(host_runtime.reset_long_tool_state)
        started = threading.Event()
        release = threading.Event()
        real_walk = server.os.walk

        def slow_walk(*args, **kwargs):
            started.set()
            if not release.wait(2.0):
                raise AssertionError("relation worker was not released")
            yield from real_walk(*args, **kwargs)

        async def exercise() -> dict:
            task = asyncio.create_task(server.find_unpacked_asset_relations(
                "Content/MI_CrowFeather.uasset"
            ))
            self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
            before = time.monotonic()
            status = json.loads(server.get_host_runtime_status())
            self.assertLess(time.monotonic() - before, 0.5)
            self.assertEqual(status["toolStatus"], "running")
            self.assertEqual(
                status["currentTool"]["name"], "find_unpacked_asset_relations"
            )
            release.set()
            return json.loads(await asyncio.wait_for(task, 2.0))

        try:
            with mock.patch.object(server.os, "walk", side_effect=slow_walk):
                payload = asyncio.run(exercise())
        finally:
            release.set()
        self.assertEqual(payload["classification"]["kind"], "material")


class AssetHydrationConcurrencyTests(unittest.TestCase):
    def test_hydration_yields_to_concurrent_runtime_status(self) -> None:
        from src import host_runtime

        host_runtime.reset_long_tool_state()
        self.addCleanup(host_runtime.reset_long_tool_state)
        started = threading.Event()
        release = threading.Event()

        class SlowCatalog:
            def __init__(self, unused_path):
                pass

            def hydrate_scope(self, *args, **kwargs):
                started.set()
                if not release.wait(2.0):
                    raise AssertionError("hydration worker was not released")
                return {
                    "schema": "test-hydrate/v1",
                    "ready": True,
                    "hydrated": 1,
                }

        async def exercise() -> dict:
            task = asyncio.create_task(
                server.hydrate_unpacked_asset_scope(["Content/Materials"])
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
            before = time.monotonic()
            status = json.loads(server.get_host_runtime_status())
            self.assertLess(time.monotonic() - before, 0.5)
            self.assertEqual(status["toolStatus"], "running")
            self.assertEqual(
                status["currentTool"]["name"], "hydrate_unpacked_asset_scope"
            )
            release.set()
            return json.loads(await asyncio.wait_for(task, 2.0))

        try:
            with mock.patch.object(
                server,
                "_ensure_shader_asset_catalog",
                return_value=(mock.Mock(), {}, Path("D:/assets"), Path("D:/catalog.db"), {}),
            ), mock.patch.object(server, "AssetCatalogService", SlowCatalog):
                payload = asyncio.run(exercise())
        finally:
            release.set()
        self.assertTrue(payload["ready"])
