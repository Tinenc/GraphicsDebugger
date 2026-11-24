"""Keep narrow route lookups bounded without trusting stale tool bytes."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from src.shader_workspace.io import atomic_write_json
from src.shader_workspace.toolchains import ToolchainCatalog, route_record, toolchain_document
from src.shader_workspace.tools import ToolRegistry


class ShaderRouteIntegrityScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        tools = {}
        for tool_id in ("selected", "other-format", "other-stage", "other-api", "reconstructed"):
            executable = self.root / (tool_id + ".exe")
            dependency = self.root / (tool_id + ".dll")
            executable.write_bytes((tool_id + " executable").encode("ascii"))
            dependency.write_bytes((tool_id + " dependency").encode("ascii"))
            tools[tool_id] = {
                "executable": str(executable),
                "executableSha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "fixedFileHashes": {
                    str(dependency): hashlib.sha256(dependency.read_bytes()).hexdigest(),
                },
                "decompile": {"args": ["{input}", "{output}"], "timeoutSeconds": 10},
            }
        self.tools_path = self.root / "tools.json"
        self.routes_path = self.root / "routes.json"
        atomic_write_json(self.tools_path, {
            "schema": "renderdoc-ultra-shader-tools/v1",
            "schemaVersion": 1,
            "tools": tools,
        })
        registry = ToolRegistry(self.tools_path)

        def route(tool_id, *, route_id=None, priority=10, source="dxil", target="dxil-asm",
                  stage="fragment", api="d3d12", fidelity="ir-disassembly"):
            return route_record(
                registry, route_id=route_id or tool_id, tool_id=tool_id, operation="decompile",
                input_format=source, output_format=target, stages=[stage], target_apis=[api],
                fidelity=fidelity, priority=priority, implementation="synthetic integrity fixture",
                source_url="https://example.invalid/integrity-fixture",
                caveats=["Synthetic route only; reconstruction is not original source."],
            )

        atomic_write_json(self.routes_path, toolchain_document(self.tools_path, [
            route("selected", route_id="selected-later", priority=20),
            route("selected", route_id="selected-first"),
            route("other-format", source="dxbc", target="dxbc-asm"),
            route("other-stage", stage="vertex"),
            route("other-api", source="dxbc", target="hlsl", api="d3d11",
                  fidelity="reconstructed-source"),
            route("reconstructed", target="hlsl", fidelity="reconstructed-source"),
        ]))
        self.catalog = ToolchainCatalog(self.tools_path, self.routes_path)

    def test_only_matching_operations_are_hashed_once_per_lookup(self) -> None:
        registry = self.catalog.registry
        with mock.patch.object(registry, "operation_integrity", wraps=registry.operation_integrity) as probe:
            routes = self.catalog.matching_routes("dxil", "dxil-asm", "fragment")
        self.assertEqual([route.route_id for route in routes], ["selected-first", "selected-later"])
        self.assertEqual(probe.call_args_list, [mock.call("selected", "decompile")])

    def test_unmatched_or_unrequested_integrity_does_not_read_any_tools(self) -> None:
        registry = self.catalog.registry
        with mock.patch.object(registry, "operation_integrity", wraps=registry.operation_integrity) as probe:
            self.assertEqual((), self.catalog.matching_routes("dxil", "dxil-asm", "compute"))
            self.assertEqual((), self.catalog.matching_routes(
                "dxbc", "hlsl", "fragment", target_api="d3d12"))
            self.assertEqual((), self.catalog.matching_routes(
                "dxil", "hlsl", "fragment", allow_reconstructed=False))
            self.assertEqual(2, len(self.catalog.matching_routes(
                "dxil", "dxil-asm", "fragment", require_integrity=False)))
        probe.assert_not_called()

    def test_each_lookup_rechecks_executable_and_dependency_bytes(self) -> None:
        for suffix in (".exe", ".dll"):
            with self.subTest(suffix=suffix):
                artifact = self.root / ("selected" + suffix)
                original = artifact.read_bytes()
                self.assertEqual(2, len(self.catalog.matching_routes("dxil", "dxil-asm", "fragment")))
                artifact.write_bytes(bytes([original[0] ^ 1]) + original[1:])
                self.assertEqual((), self.catalog.matching_routes("dxil", "dxil-asm", "fragment"))
                artifact.write_bytes(original)
                self.assertEqual(2, len(self.catalog.matching_routes("dxil", "dxil-asm", "fragment")))

    def test_unrelated_tool_changes_do_not_hide_an_eligible_route(self) -> None:
        (self.root / "other-format.dll").write_bytes(b"changed unrelated dependency")
        self.assertEqual(2, len(self.catalog.matching_routes("dxil", "dxil-asm", "fragment")))
        self.assertEqual((), self.catalog.matching_routes("dxbc", "dxbc-asm", "fragment"))


if __name__ == "__main__":
    unittest.main()
