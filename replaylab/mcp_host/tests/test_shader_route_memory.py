"""Project route memory: remembered lanes run first, failures are skipped."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from src.shader_workspace.route_memory import (
    ShaderRouteMemory,
    dxil_route_features,
    plan_route_ids,
    route_key,
    route_key_from_manifest,
)
from src.shader_workspace.workspace import ShaderWorkspaceService

KEY = route_key(target_api="d3d12", capture_encoding="dxil", stage="fragment",
                target_profile="ps_6_6", editable_format="hlsl", features="static")
DYNKEY = route_key(target_api="d3d12", capture_encoding="dxil", stage="fragment",
                   target_profile="ps_6_6", editable_format="hlsl", features="dyncb")
RENODX = ["community-renodx-dxil-hlsl"]
SPIRV = ["dxil->spirv@dxil-spirv", "spirv->hlsl@spirv-cross"]


def _selection() -> dict:
    return {
        "editableFormat": "hlsl",
        "decompileRouteIds": list(RENODX),
        "decompilerToolIds": ["renodx"],
        "compileRouteIds": ["hlsl->dxil@dxc"],
        "decompileCandidates": [list(RENODX), list(SPIRV)],
        "compileCandidates": [["hlsl->dxil@dxc"]],
        "capabilities": [
            {"routeId": RENODX[0], "toolId": "renodx"},
            {"routeId": SPIRV[0], "toolId": "dxil-spirv"},
            {"routeId": SPIRV[1], "toolId": "spirv-cross"},
            {"routeId": "hlsl->dxil@dxc", "toolId": "dxc"},
        ],
    }


class RouteMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = ShaderRouteMemory(Path(self.tmp.name) / "shaders" / "route-memory.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_empty_memory_keeps_catalog_default(self) -> None:
        selection, memo = self.memory.prefer(_selection(), KEY)
        self.assertEqual(selection["decompileRouteIds"], RENODX)
        self.assertFalse(memo["applied"])
        self.assertEqual(memo["source"], "none")

    def test_validated_success_becomes_primary_lane(self) -> None:
        self.memory.record_success(KEY, decompile_route_ids=SPIRV, compile_route_ids=["hlsl->dxil@dxc"],
                                   workspace_path="D:/p/ws-alt1")
        selection, memo = self.memory.prefer(_selection(), KEY)
        self.assertEqual(selection["decompileRouteIds"], SPIRV)
        self.assertEqual(selection["decompilerToolIds"], ["dxil-spirv", "spirv-cross"])
        self.assertTrue(memo["applied"])
        self.assertEqual(memo["source"], "validated-success")
        self.assertEqual(memo["catalogDefault"], RENODX)
        # candidates stay available for fallback
        self.assertEqual(selection["decompileCandidates"], [RENODX, SPIRV])
        # persisted
        payload = json.loads(self.memory.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["keys"][KEY]["preferred"]["successCount"], 1)
        self.memory.record_success(KEY, decompile_route_ids=SPIRV, compile_route_ids=["hlsl->dxil@dxc"])
        self.assertEqual(self.memory.lookup(KEY)["preferred"]["successCount"], 2)

    def test_known_failure_is_skipped_before_any_success(self) -> None:
        self.memory.record_decompile_failure(KEY, decompile_route_ids=RENODX,
                                             reason="Unexpected dynamic cbuffer offset",
                                             category="tool-rejected-input")
        selection, memo = self.memory.prefer(_selection(), KEY)
        self.assertEqual(selection["decompileRouteIds"], SPIRV)
        self.assertEqual(memo["source"], "skip-known-failure")
        self.assertIn("dynamic cbuffer", memo["skipped"]["reason"])

    def test_remembered_lane_missing_from_catalog_is_ignored(self) -> None:
        self.memory.record_success(KEY, decompile_route_ids=["dxil->hlsl@gone"], compile_route_ids=["x"])
        selection, memo = self.memory.prefer(_selection(), KEY)
        self.assertEqual(selection["decompileRouteIds"], RENODX)
        self.assertFalse(memo["applied"])
        self.assertIn("no longer offered", memo["reason"])

    def test_success_clears_failure_and_other_keys_are_isolated(self) -> None:
        self.memory.record_decompile_failure(KEY, decompile_route_ids=SPIRV, reason="boom")
        self.memory.record_success(KEY, decompile_route_ids=SPIRV, compile_route_ids=["c"])
        self.assertEqual(self.memory.failed_routes(KEY), {})
        other = route_key(target_api="d3d12", capture_encoding="dxil", stage="vertex",
                          target_profile="vs_6_6", editable_format="hlsl")
        self.assertIsNone(self.memory.lookup(other))

    def test_manifest_key_and_plan_ids(self) -> None:
        manifest = {
            "capture": {"api": "d3d12"}, "stage": "fragment", "targetProfile": "ps_6_6",
            "routeFeatures": {"signature": "static"},
            "routePlan": {"inputFormat": "dxil", "editableFormat": "hlsl",
                          "decompile": [{"capability": {"routeId": SPIRV[0]}}, {"capability": {"routeId": SPIRV[1]}}],
                          "compile": [{"capability": {"routeId": "hlsl->dxil@dxc"}}]},
        }
        self.assertEqual(route_key_from_manifest(manifest), KEY)
        self.assertEqual(plan_route_ids(manifest["routePlan"], "decompile"), SPIRV)
        self.assertEqual(plan_route_ids(manifest["routePlan"], "compile"), ["hlsl->dxil@dxc"])

    def test_service_applies_memory_only_through_its_provider(self) -> None:
        self.memory.record_success(KEY, decompile_route_ids=SPIRV, compile_route_ids=["hlsl->dxil@dxc"])
        service = ShaderWorkspaceService.__new__(ShaderWorkspaceService)
        service._route_memory_provider = lambda: self.memory
        selection, memo = service._apply_route_memory(
            _selection(), target_api="d3d12", capture_encoding="dxil", stage="fragment", target_profile="ps_6_6",
            features="static")
        self.assertEqual(selection["decompileRouteIds"], SPIRV)
        self.assertTrue(memo["applied"])
        service._route_memory_provider = None
        selection, memo = service._apply_route_memory(
            _selection(), target_api="d3d12", capture_encoding="dxil", stage="fragment", target_profile="ps_6_6",
            features="static")
        self.assertEqual(selection["decompileRouteIds"], RENODX)
        self.assertFalse(memo["applied"])
        # without a project the built-in incompatibility table still applies
        selection, memo = service._apply_route_memory(
            _selection(), target_api="d3d12", capture_encoding="dxil", stage="fragment", target_profile="ps_6_6",
            features="dyncb")
        self.assertEqual(selection["decompileRouteIds"], SPIRV)
        self.assertEqual(memo["source"], "known-incompatibility")

    def test_dxil_features_detect_dynamic_cbuffer_indexing(self) -> None:
        static = """
  %1 = call %dx.types.CBufRet.f32 @dx.op.cbufferLoadLegacy.f32(i32 59, %dx.types.Handle %h, i32 14)
  %2 = call %dx.types.CBufRet.f32 @dx.op.cbufferLoadLegacy.f32(i32 59, %dx.types.Handle %h, i32 3)
"""
        dynamic = static + """
  %7 = add i32 %idx, 2
  %8 = call %dx.types.CBufRet.f32 @dx.op.cbufferLoadLegacy.f32(i32 59, %dx.types.Handle %h2, i32 %7)
"""
        self.assertEqual(dxil_route_features(static)["signature"], "static")
        features = dxil_route_features(dynamic)
        self.assertEqual((features["cbufferLoads"], features["dynamicCBufferLoads"], features["signature"]), (3, 1, "dyncb"))
        self.assertEqual(dxil_route_features("define void @main() {}")["signature"], "nocb")

    def test_known_incompatibility_only_moves_dynamic_shaders_off_renodx(self) -> None:
        static_selection, memo = self.memory.prefer(_selection(), KEY)
        self.assertEqual(static_selection["decompileRouteIds"], RENODX)
        self.assertFalse(memo["applied"])
        dynamic_selection, memo = self.memory.prefer(_selection(), DYNKEY)
        self.assertEqual(dynamic_selection["decompileRouteIds"], SPIRV)
        self.assertEqual(memo["source"], "known-incompatibility")
        self.assertIn("Unexpected dynamic cbuffer offset", memo["skipped"]["reason"])
        # a success on the dynamic shape does not touch the static shape
        self.memory.record_success(DYNKEY, decompile_route_ids=SPIRV, compile_route_ids=["hlsl->dxil@dxc"])
        static_selection, _ = self.memory.prefer(_selection(), KEY)
        self.assertEqual(static_selection["decompileRouteIds"], RENODX)


if __name__ == "__main__":
    unittest.main()
