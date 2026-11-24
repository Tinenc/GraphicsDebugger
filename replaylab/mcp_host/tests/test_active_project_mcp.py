from __future__ import annotations

import copy
from contextlib import nullcontext
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from src import asset_catalog as asset_catalog_module
from src.active_projects import ActiveReplayProjectService
from src.symbols.dxbc import fingerprint_bytes
from src.symbols.matcher import capture_identity


class _FastMCP:
    def __init__(self, *unused_args, **unused_kwargs):
        pass

    def tool(self):
        return lambda function: function

    def run(self, **unused_kwargs):
        pass


def _load_server():
    try:
        return importlib.import_module("src.server")
    except ModuleNotFoundError as exc:
        if exc.name != "mcp":
            raise
    mcp_package = types.ModuleType("mcp")
    mcp_package.__path__ = []
    mcp_server = types.ModuleType("mcp.server")
    mcp_server.__path__ = []
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = _FastMCP
    with mock.patch.dict(
        sys.modules,
        {
            "mcp": mcp_package,
            "mcp.server": mcp_server,
            "mcp.server.fastmcp": fastmcp,
        },
    ):
        return importlib.import_module("src.server")


def _dxbc(code: bytes = b"active project shader") -> bytes:
    body = code + b"\x00" * (-len(code) % 4)
    program = struct.pack("<2I", 0x00000050, 2 + len(body) // 4) + body
    chunk = b"SHDR" + struct.pack("<I", len(program)) + program
    header_size = 36
    return (
        b"DXBC"
        + b"\x00" * 16
        + struct.pack("<I", 1)
        + struct.pack("<I", header_size + len(chunk))
        + struct.pack("<I", 1)
        + struct.pack("<I", header_size)
        + chunk
    )


def _interface() -> dict:
    return {
        "interfaceComplete": True,
        "inputSignature": [],
        "outputSignature": [],
        "readOnlyResources": [],
        "readWriteResources": [],
        "samplers": [],
        "constantBuffers": [],
        "threadGroupSize": [],
        "outputTopology": "TriangleList",
        "dynamicLinkageInterfaceCount": 0,
    }


class _CatalogIPC:
    def __init__(self, root: Path):
        self.instance_id = "123-" + "b" * 32
        self.root = root
        self.blob = _dxbc()
        fingerprint = fingerprint_bytes(self.blob)
        assert fingerprint is not None
        self.shader = {
            "shaderResourceId": 902,
            "resourceId": 902,
            "stage": "fragment",
            "entryPoint": "main",
            "rawPath": "blobs/%s.bin" % fingerprint.raw_sha256,
            "rawSize": len(self.blob),
            "rawSha256": fingerprint.raw_sha256,
            "codeSha256": fingerprint.code_sha256,
            **_interface(),
        }
        self.shader["shaderKey"] = f"902:fragment:main:{fingerprint.raw_sha256}"
        self.use = {
            "eventId": 142,
            "stage": "fragment",
            "entryPoint": "main",
            "resourceId": 902,
            "rawSha256": fingerprint.raw_sha256,
            "shaderKey": self.shader["shaderKey"],
            "constantBlocks": [],
        }
        base = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": "D:/captures/frame.rdc",
            "api": "D3D11",
            "gameId": "example-game",
            "buildId": "build-1",
            "platform": "windows-x64",
            "shaders": [dict(self.shader)],
            "uses": [dict(self.use)],
        }
        self.capture_id = capture_identity(base, [self.shader])
        self.context = {
            "schema": "renderdoc-ultra-current-context/v1",
            "schemaVersion": 1,
            "snapshotId": "f" * 64,
            "capture": {
                "loaded": True,
                "captureSessionId": "a" * 32,
                "captureId": self.capture_id,
                "path": "D:/captures/frame.rdc",
                "api": "D3D11",
                "local": True,
            },
            "events": {"currentEventId": 142, "selectedEventId": 142},
            "shaders": {
                "bound": [
                    {
                        "stage": "fragment",
                        "resourceId": 902,
                        "entryPoint": "main",
                        "rawSha256": fingerprint.raw_sha256,
                    }
                ],
                "focused": None,
                "focusIsExplicit": False,
            },
            "texture": {"available": False},
        }
        self.export_revision = 1
        self.shader_query_result = None
        self.calls: list[tuple[str, dict, float | None]] = []

    def call(self, method: str, params: dict | None = None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, params, timeout))
        if method == "begin_export_shader_catalog":
            # Most tests exercise the legacy bridge compatibility path. The
            # Host may fall back only for this explicit JSON-RPC response.
            raise RuntimeError(
                "Bridge error [-32601]: Unknown method: begin_export_shader_catalog"
            )
        if method == "get_current_renderdoc_context":
            context = copy.deepcopy(self.context)
            if params.get("identity_only"):
                # Match the real Bridge: its cheap identity response carries
                # no current event or bound-Shader observation.
                context["events"] = {"currentEventId": 0, "selectedEventId": 0}
                context["shaders"] = {"bound": [], "focused": None, "focusIsExplicit": False}
            return context
        if method == "get_shader_info":
            if int(params.get("event_id", 0)) != int(self.use["eventId"]):
                return {"error": "No Shader is bound at the requested event"}
            stage = str(params.get("stage", "") or "").strip().lower()
            if stage not in {"fragment", "pixel", "ps"}:
                return {"error": "No Shader is bound at the requested stage"}
            if self.shader_query_result is not None:
                return copy.deepcopy(self.shader_query_result)
            return {
                "shaderId": self.shader["resourceId"],
                "stage": "fragment",
                "entryPoint": self.shader["entryPoint"],
                "rawSha256": self.shader["rawSha256"],
            }
        if method == "export_shader_catalog":
            self.assert_export_scope(params)
            output = Path(params["output_dir"])
            (output / "blobs").mkdir(parents=True, exist_ok=True)
            raw_path = output / self.shader["rawPath"]
            raw_path.write_bytes(self.blob)
            value = {
                "schema": "renderdoc-ultra-capture-symbols/v1",
                "schemaVersion": 1,
                "captureId": self.capture_id,
                "capturePath": "D:/captures/frame.rdc",
                "api": "D3D11",
                "gameId": "example-game",
                "buildId": "build-1",
                "platform": "windows-x64",
                "shaders": [dict(self.shader)],
                "uses": [dict(self.use)],
                "selection": {
                    "mode": "explicit" if params["event_ids"] else "all_draw_dispatch",
                    "eventIds": params["event_ids"] or [self.use["eventId"]],
                    "stages": params.get("stages", ["fragment"]),
                },
                "exportDiagnostics": {
                    "reflectionRevision": self.export_revision
                },
            }
            path = output / "shader_catalog.v1.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            return {
                "schema": value["schema"],
                "schemaVersion": 1,
                "captureId": self.capture_id,
                "captureSessionId": self.context["capture"]["captureSessionId"],
                "catalogPath": str(path),
            }
        if method == "get_shared_shader_replacements":
            return {
                "schema": "renderdoc-ultra-shared-shader-replacement-list/v1",
                "schemaVersion": 1, "captureId": params["expected_capture_id"],
                "captureSessionId": self.context["capture"]["captureSessionId"],
                "originalShadersPreserved": True, "replayMutation": False,
                "count": 0, "replacements": [],
            }
        if method == "load_symbol_aliases":
            return {
                "schema": "renderdoc-ultra-aliases/v1",
                "schemaVersion": 1,
                "captureId": self.capture_id,
                "confirmedOnly": True,
                "counts": {"activeValidatedAliases": 0},
                "applied": [],
            }
        raise AssertionError("unexpected IPC method %s" % method)

    def assert_export_scope(self, params: dict) -> None:
        if params.get("event_ids") == []:
            return
        if (params.get("event_ids") != [self.use["eventId"]]
                or params.get("stages") != ["fragment"]):
            raise AssertionError("Shader preparation must export exactly one event/stage")


class _FakeSymbolService:
    scan_calls = 0

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path).resolve()

    @classmethod
    def for_asset_root(cls, asset_root, database_path=None):
        return cls(Path(database_path or (Path(asset_root) / "symbols.sqlite3")))

    def scan(self, asset_root, build_id="", **unused_kwargs):
        type(self).scan_calls += 1
        return {
            "schema": "renderdoc-ultra-asset-scan/v1",
            "scanId": "1" * 24,
            "assetRoot": str(Path(asset_root).resolve()),
            "buildId": build_id,
        }

    def run(self, asset_root, catalog_path, build_id="", **unused_kwargs):
        scan = self.scan(asset_root, build_id=build_id)
        return {"scan": scan, "match": self.match(catalog_path, scan["scanId"])}

    def match(self, catalog_path, scan_id):
        catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
        return {
            "schema": "renderdoc-ultra-symbol-match/v1",
            "captureId": catalog["captureId"],
            "scanId": scan_id,
            "matches": [],
        }

    def export_alias_table(
        self,
        capture_id,
        output_path,
        verified_only=True,
        scan_id="",
        catalog_sha256="",
    ):
        assert verified_only is True
        path = Path(output_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "schema": "renderdoc-ultra-aliases/v1",
            "schemaVersion": 1,
            "captureId": capture_id,
            "catalogSha256": catalog_sha256,
            "verifiedOnly": True,
            "aliases": [],
            "shaders": [],
            "symbolRules": [],
        }
        path.write_text(json.dumps(value), encoding="utf-8")
        return {
            "schema": value["schema"],
            "schemaVersion": 1,
            "captureId": capture_id,
            "catalogSha256": catalog_sha256,
            "path": str(path),
            "aliasCount": 0,
            "shaderCount": 0,
            "verifiedOnly": True,
        }

    def unresolved(self, capture_id):
        return {"captureId": capture_id, "count": 0, "items": []}


class ActiveProjectMcpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _load_server()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = ActiveReplayProjectService(
            self.root / "config" / "active-project.json",
            self.root / "projects",
        )
        self.ipc = _CatalogIPC(self.root)
        self.old_service = self.server._ACTIVE_PROJECT_SERVICE
        self.old_ipc = self.server.ipc
        self.old_route_binding = self.server._active_shader_route_binding_sha256
        self.server._ACTIVE_PROJECT_SERVICE = self.service
        self.server.ipc = self.ipc
        # This unit suite exercises active-project identity and path binding,
        # not the machine-global installed tool registry.  Isolate it from a
        # developer's previously generated LocalAppData configuration while
        # retaining a strict, deterministic route-binding identity.
        self.server._active_shader_route_binding_sha256 = lambda: "e" * 64
        self.server._ACTIVE_SYMBOL_SCAN_CACHE.clear()
        self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS.clear()
        _FakeSymbolService.scan_calls = 0

    def tearDown(self):
        self.server._ACTIVE_PROJECT_SERVICE = self.old_service
        self.server.ipc = self.old_ipc
        self.server._active_shader_route_binding_sha256 = self.old_route_binding
        self.server._ACTIVE_SYMBOL_SCAN_CACHE.clear()
        self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS.clear()
        self.temporary.cleanup()

    def _json(self, value: str) -> dict:
        result = json.loads(value)
        self.assertNotIn("error", result, result)
        self.assertIs(result["applied"], False)
        return result

    def _bound_with_assets(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        configured = self._json(
            self.server.configure_active_replay_project(
                "example-game",
                "build-1",
                "windows-x64",
                "d3d11",
            )
        )
        self.assertTrue(configured["project"]["scopeConfigured"])
        assets = self.root / "unpacked"
        assets.mkdir()
        (assets / "main.hlsl").write_text(
            "float4 main() : SV_Target { return float4(1, 0, 0, 1); }\n",
            encoding="utf-8",
        )
        self._json(self.server.configure_active_project_asset_root(str(assets)))

    def test_process_fixed_default_paths_and_tool_signatures(self):
        local = self.root / "LocalAppData"
        state, projects = self.server._resolve_active_project_paths(
            {"LOCALAPPDATA": str(local)}
        )
        self.assertEqual(
            state,
            (local / "ReplayLab" / "config" / "active-project.json").resolve(),
        )
        self.assertEqual(projects, (local / "ReplayLab" / "projects").resolve())

        custom_state = self.root / "owner" / "selection.json"
        custom_home = self.root / "owner" / "projects"
        state, projects = self.server._resolve_active_project_paths(
            {
                "LOCALAPPDATA": str(local),
                self.server.ACTIVE_PROJECT_STATE_ENV: str(custom_state),
                self.server.ACTIVE_PROJECTS_HOME_ENV: str(custom_home),
            }
        )
        self.assertEqual(state, custom_state.resolve())
        self.assertEqual(projects, custom_home.resolve())

        expected = {
            "ensure_active_replay_project",
            "get_active_replay_project_status",
            "get_active_capture_catalog_binding_status",
            "configure_active_replay_project",
            "configure_active_project_asset_root",
            "activate_active_replay_project",
            "prepare_active_capture_project",
            "query_active_shader_asset_evidence",
            "get_unpacked_asset_index_capabilities",
            "get_active_unpacked_asset_index_status",
            "index_active_unpacked_assets",
            "query_active_unpacked_assets",
            "resolve_active_unpacked_asset",
            "get_unpacked_asset_provider_capabilities",
            "get_unpacked_asset_provider_decisions",
            "query_unpacked_asset_inventory_assets",
            "start_unpacked_asset_report",
            "start_unpacked_asset_blender_export",
            "continue_unpacked_asset_blender_export",
            "get_unpacked_asset_job_status",
            "run_resource_export_target_validation",
            "get_active_shader_workspace_path",
            "register_active_project_workspace",
            "get_active_project_future_frame_plan",
            "seed_active_shader_workspace",
        }
        for name in expected:
            parameters = inspect.signature(getattr(self.server, name)).parameters
            self.assertNotIn("projects_home", parameters)
            self.assertNotIn("state_path", parameters)
            if name != "configure_active_project_asset_root":
                self.assertNotIn("asset_root", parameters)
                self.assertNotIn("database_path", parameters)
                self.assertNotIn("output_path", parameters)
        self.assertEqual(
            list(
                inspect.signature(
                    self.server.prepare_active_capture_project
                ).parameters
            ),
            [],
        )

        self.assertEqual(
            list(
                inspect.signature(
                    self.server.query_active_shader_asset_evidence
                ).parameters
            ),
            ["event_id", "stage", "top_k"],
        )
        blender_parameters = inspect.signature(
            self.server.start_unpacked_asset_blender_export
        ).parameters
        self.assertIs(blender_parameters["inventory_id"].default, inspect.Parameter.empty)
        self.assertIs(
            blender_parameters["expected_inventory_decision_sha256"].default,
            inspect.Parameter.empty,
        )
        self.assertEqual(
            list(
                inspect.signature(
                    self.server.continue_unpacked_asset_blender_export
                ).parameters
            ),
            [
                "operation_id",
                "parent_operation_id",
                "expected_parent_request_sha256",
                "expected_parent_result_sha256",
            ],
        )

    def test_resource_export_target_validation_accepts_only_operation_nonce(self):
        manager = mock.Mock()
        manager.run_target_validation.return_value = {
            "schema": "replaylab-resource-export-target-validation-run/v1",
            "schemaVersion": 1,
            "operationId": "a" * 32,
            "targetValidationComplete": False,
        }
        with mock.patch.object(self.server, "_RESOURCE_EXPORT_MANAGER", manager):
            result = json.loads(
                self.server.run_resource_export_target_validation("a" * 32)
            )
        self.assertNotIn("error", result, result)
        manager.run_target_validation.assert_called_once_with("a" * 32)
        self.assertEqual(result["operationId"], "a" * 32)
        self.assertEqual(
            list(
                inspect.signature(
                    self.server.run_resource_export_target_validation
                ).parameters
            ),
            ["operation_id"],
        )

    def test_uex_decision_discovery_tools_use_host_owned_provider_inputs(self):
        calls = []

        class FakeJobs:
            def capabilities(inner_self):
                return {"schema": "providers", "routes": {}}

            def discover_uex_decisions(
                inner_self, *, profile="", inventory_limit=20
            ):
                calls.append(("discover", profile, inventory_limit))
                return {
                    "schema": "renderdoc-ultra-uex-decision-discovery/v1",
                    "available": True,
                    "status": "ready",
                    "profiles": [{"profile": "Demo", "inventories": []}],
                }

            def query_uex_inventory_assets(inner_self, **kwargs):
                calls.append(("query", kwargs))
                return {
                    "schema": "renderdoc-ultra-uex-inventory-assets/v1",
                    "profile": kwargs["profile"],
                    "items": [],
                }

        with mock.patch.object(
            self.server, "_UNPACKED_ASSET_JOB_MANAGER", FakeJobs()
        ):
            capabilities = json.loads(
                self.server.get_unpacked_asset_provider_capabilities()
            )
            decisions = json.loads(
                self.server.get_unpacked_asset_provider_decisions("Demo", 7)
            )
            query = json.loads(
                self.server.query_unpacked_asset_inventory_assets(
                    "Demo", 41, "SkeletalMesh", "Hero", 3, 9
                )
            )

        self.assertFalse(capabilities["applied"])
        self.assertEqual(
            capabilities["decisionDiscovery"]["profiles"][0]["profile"], "Demo"
        )
        self.assertFalse(decisions["applied"])
        self.assertFalse(query["applied"])
        self.assertEqual(calls[0], ("discover", "", 20))
        self.assertEqual(calls[1], ("discover", "Demo", 7))
        self.assertEqual(
            calls[2],
            (
                "query",
                {
                    "profile": "Demo",
                    "inventory_id": 41,
                    "kind": "SkeletalMesh",
                    "search": "Hero",
                    "offset": 3,
                    "limit": 9,
                },
            ),
        )

    def test_active_unpacked_asset_catalog_is_path_free_persistent_and_hash_bound(self):
        # This test starts before indexing and explicitly indexes below. Root
        # configuration normally starts a background refresh; suppress only
        # that launch so it cannot race the unindexed fixture or its cleanup.
        with mock.patch.object(
            self.server, "_ensure_asset_activation_content_refresh", return_value=None
        ) as refresh:
            self._bound_with_assets()
        refresh.assert_called_once_with(force_activation=True, depth="names")
        assets = Path(self.service.status()["assetRoot"])
        (assets / "hero.obj").write_text(
            "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
            encoding="utf-8",
        )

        before = json.loads(self.server.get_active_unpacked_asset_index_status())
        self.assertFalse(before["ready"])
        # A configured root remains usable while indexing is incomplete.
        self.assertTrue(before["assetRootConfigured"])
        self.assertTrue(before["directAssetReadsAvailable"])
        self.assertNotIn("requiredAction", before)
        self.assertNotIn("next", before)

        indexed = json.loads(self.server.index_active_unpacked_assets(wait_seconds=120))
        self.assertNotIn("error", indexed, indexed)
        self.assertTrue(indexed["applied"])
        self.assertEqual(indexed["buildId"], "build-1")
        self.assertGreaterEqual(indexed["assetCount"], 2)

        query = self._json(
            self.server.query_active_unpacked_assets(
                text="hero", kinds=["mesh"], limit=10
            )
        )
        self.assertEqual(query["catalogSha256"], indexed["catalogSha256"])
        self.assertEqual(query["count"], 1)
        selected = query["assets"][0]
        delivery = self._json(
            self.server.resolve_active_unpacked_asset(
                selected["assetId"], query["catalogSha256"]
            )
        )
        self.assertTrue(delivery["verified"])
        self.assertEqual(delivery["relativePath"], "hero.obj")
        self.assertTrue(Path(delivery["absolutePath"]).is_file())

        stale = json.loads(
            self.server.resolve_active_unpacked_asset(
                selected["assetId"], "0" * 64
            )
        )
        self.assertIn("error", stale)
        self.assertIn("changed", stale["error"])

    def test_unpacked_asset_report_requires_catalog_bound_strong_inventory_evidence(self):
        self._bound_with_assets()
        assets = Path(self.service.status()["assetRoot"])
        (assets / "inventory-selection.json").write_text(
            json.dumps(
                {
                    "schema": "uex.inventory-selection.v1",
                    "inventory": {
                        "inventoryId": 41,
                        "profile": "example",
                        "discoveryConfidence": "Strong",
                        "complete": True,
                        "truncated": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        (assets / "asset-report-spec.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "name": "Example",
                    "characterPathRules": [
                        {
                            "name": "project-character-layout",
                            "regex": (
                                r"^(?<category>[^/]+)/(?<character>[^/]+)/"
                                r"(?<variant>[^/]+)/(?<state>[^/]+)/(?<component>[^/]+)"
                            ),
                        }
                    ],
                    "scene": {"scopeGlobs": ["Game/Content/**"]},
                }
            ),
            encoding="utf-8",
        )
        indexed = json.loads(self.server.index_active_unpacked_assets(wait_seconds=120))
        query = json.loads(
            self.server.query_active_unpacked_assets(kinds=["metadata"], limit=20)
        )
        identities = {item["name"]: item["assetId"] for item in query["assets"]}

        calls = []

        class FakeJobs:
            def start_report(inner_self, **kwargs):
                calls.append(kwargs)
                return {"schema": "renderdoc-ultra-unpacked-asset-job/v1", "status": "running"}

        with mock.patch.object(
            self.server, "_UNPACKED_ASSET_JOB_MANAGER", FakeJobs()
        ):
            result = json.loads(
                self.server.start_unpacked_asset_report(
                    "a" * 32,
                    "example",
                    41,
                    identities["inventory-selection.json"],
                    identities["asset-report-spec.json"],
                    indexed["catalogSha256"],
                )
            )
        self.assertNotIn("error", result, result)
        self.assertTrue(result["applied"])
        self.assertEqual(calls[0]["inventory_id"], 41)
        self.assertEqual(
            calls[0]["inventory_evidence_asset_id"],
            identities["inventory-selection.json"],
        )
        self.assertRegex(calls[0]["inventory_evidence_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(calls[0]["spec_asset_id"], identities["asset-report-spec.json"])
        self.assertIsNone(calls[0]["spec_path"])
        self.assertEqual(calls[0]["report_spec"]["name"], "Example")
        self.assertRegex(calls[0]["project_binding_sha256"], r"^[0-9a-f]{64}$")

        (assets / "inventory-selection.json").write_text(
            json.dumps(
                {
                    "inventory": {
                        "inventoryId": 41,
                        "discoveryConfidence": "Moderate",
                        "complete": True,
                        "truncated": False,
                    }
                }
            ),
            encoding="utf-8",
        )
        stale = json.loads(
            self.server.start_unpacked_asset_report(
                "b" * 32,
                "example",
                41,
                identities["inventory-selection.json"],
                identities["asset-report-spec.json"],
                indexed["catalogSha256"],
            )
        )
        self.assertIn("error", stale)
        self.assertIn("changed", stale["error"])
        self.assertEqual(len(calls), 1)
        refreshed = json.loads(self.server.index_active_unpacked_assets(wait_seconds=120))
        refreshed_query = json.loads(
            self.server.query_active_unpacked_assets(kinds=["metadata"], limit=20)
        )
        refreshed_identities = {
            item["name"]: item["assetId"] for item in refreshed_query["assets"]
        }
        with mock.patch.object(
            self.server, "_UNPACKED_ASSET_JOB_MANAGER", FakeJobs()
        ):
            weak = json.loads(
                self.server.start_unpacked_asset_report(
                    "c" * 32,
                    "example",
                    41,
                    refreshed_identities["inventory-selection.json"],
                    refreshed_identities["asset-report-spec.json"],
                    refreshed["catalogSha256"],
                )
            )
        self.assertIn("error", weak)
        self.assertIn("not Strong", weak["error"])
        self.assertEqual(len(calls), 1)

    def test_unpacked_asset_report_accepts_bounded_inline_spec_without_catalog(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        self._json(
            self.server.configure_active_replay_project(
                "example-game",
                "build-1",
                "windows-x64",
                "d3d11",
            )
        )
        spec = {
            "version": 1,
            "name": "Project Rules",
            "characterPathRules": [
                {
                    "name": "project-character-layout",
                    "regex": (
                        r"^(?<category>[^/]+)/(?<character>[^/]+)/"
                        r"(?<variant>[^/]+)/(?<state>[^/]+)/(?<component>[^/]+)"
                    ),
                }
            ],
            "scene": {"scopeGlobs": ["Project/Content/**"]},
        }
        calls = []

        class FakeJobs:
            def start_report(inner_self, **kwargs):
                calls.append(kwargs)
                return {
                    "schema": "renderdoc-ultra-unpacked-asset-job/v1",
                    "status": "running",
                    "request": {"specSource": "inline-managed-v1"},
                }

        with mock.patch.object(
            self.server, "_UNPACKED_ASSET_JOB_MANAGER", FakeJobs()
        ):
            result = json.loads(
                self.server.start_unpacked_asset_report(
                    "d" * 32,
                    "example",
                    41,
                    report_spec=spec,
                )
            )
            missing = json.loads(
                self.server.start_unpacked_asset_report(
                    "e" * 32,
                    "example",
                    41,
                    report_spec={
                        "version": 1,
                        "name": "Missing rules",
                        "characterPathRules": [],
                        "scene": {"scopeGlobs": []},
                    },
                )
            )

        self.assertNotIn("error", result, result)
        self.assertTrue(result["applied"])
        self.assertEqual(result["project"]["catalogSha256"], "")
        self.assertRegex(result["project"]["bindingSha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(calls[0]["inventory_evidence_asset_id"], "")
        self.assertEqual(calls[0]["inventory_evidence_sha256"], "")
        self.assertEqual(calls[0]["spec_asset_id"], "")
        self.assertEqual(calls[0]["catalog_sha256"], "")
        self.assertEqual(calls[0]["report_spec"]["name"], "Project Rules")
        self.assertRegex(calls[0]["project_binding_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("error", missing)
        self.assertIn("characterPathRules", missing["error"])
        self.assertEqual(len(calls), 1)

    def test_unpacked_asset_blender_export_forwards_required_inventory_binding(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        self._json(
            self.server.configure_active_replay_project(
                "example-game", "build-1", "windows-x64", "d3d11"
            )
        )
        calls = []

        class FakeJobs:
            def start_blender_export(inner_self, **kwargs):
                calls.append(kwargs)
                return {
                    "schema": "renderdoc-ultra-unpacked-asset-job/v1",
                    "status": "running",
                    "request": {
                        "inventoryId": kwargs["inventory_id"],
                        "inventoryDecisionSha256": kwargs[
                            "expected_inventory_decision_sha256"
                        ],
                    },
                }

        with mock.patch.object(
            self.server, "_UNPACKED_ASSET_JOB_MANAGER", FakeJobs()
        ):
            result = json.loads(
                self.server.start_unpacked_asset_blender_export(
                    "a" * 32,
                    "Demo",
                    42,
                    "f" * 64,
                    ["Project/Hero/SK_Hero"],
                    prefer_uemodel=False,
                    export_only=True,
                )
            )

        self.assertNotIn("error", result, result)
        self.assertTrue(result["applied"])
        self.assertEqual(calls[0]["inventory_id"], 42)
        self.assertEqual(
            calls[0]["expected_inventory_decision_sha256"], "f" * 64
        )
        self.assertEqual(calls[0]["virtual_assets"], ["Project/Hero/SK_Hero"])

    def test_unpacked_asset_blender_continuation_forwards_only_parent_hashes(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        self._json(
            self.server.configure_active_replay_project(
                "example-game", "build-1", "windows-x64", "d3d11"
            )
        )
        calls = []

        class FakeJobs:
            def start_blender_continuation(inner_self, **kwargs):
                calls.append(kwargs)
                return {
                    "schema": "renderdoc-ultra-unpacked-asset-job/v1",
                    "status": "running",
                    "operationId": kwargs["operation_id"],
                }

        with mock.patch.object(
            self.server, "_UNPACKED_ASSET_JOB_MANAGER", FakeJobs()
        ):
            result = json.loads(
                self.server.continue_unpacked_asset_blender_export(
                    "b" * 32,
                    "a" * 32,
                    "c" * 64,
                    "d" * 64,
                )
            )
        self.assertNotIn("error", result, result)
        self.assertTrue(result["applied"])
        self.assertEqual(
            calls,
            [
                {
                    "operation_id": "b" * 32,
                    "parent_operation_id": "a" * 32,
                    "expected_parent_request_sha256": "c" * 64,
                    "expected_parent_result_sha256": "d" * 64,
                }
            ],
        )

    def test_unpacked_asset_incremental_reuse_preserves_and_deduplicates_relationships(self):
        self._bound_with_assets()
        assets = Path(self.service.status()["assetRoot"])
        (assets / "shaders").mkdir()
        (assets / "shared").mkdir()
        (assets / "shared" / "common.hlsli").write_text(
            "float4 helper();\n", encoding="utf-8"
        )
        (assets / "shaders" / "main.hlsl").write_text(
            '#include "../shared/common.hlsli"\n'
            '#include "../shared/common.hlsli"\n'
            "float4 main() : SV_Target { return helper(); }\n",
            encoding="utf-8",
        )

        first = json.loads(self.server.index_active_unpacked_assets("full", wait_seconds=120))
        self.assertNotIn("error", first, first)
        first_query = self._json(
            self.server.query_active_unpacked_assets(
                text="shaders/main.hlsl", kinds=["shader"]
            )
        )
        relations = first_query["assets"][0]["relationships"]
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["targetPath"], "shared/common.hlsli")
        self.assertTrue(relations[0]["verified"])
        self.assertEqual(relations[0]["derivationClass"], "deterministic-derived")
        self.assertEqual(relations[0]["evidence"]["observationCount"], 2)

        second = json.loads(self.server.index_active_unpacked_assets("incremental", wait_seconds=120))
        self.assertNotIn("error", second, second)
        self.assertEqual(second["catalogSha256"], first["catalogSha256"])
        second_query = self._json(
            self.server.query_active_unpacked_assets(
                text="shaders/main.hlsl", kinds=["shader"]
            )
        )
        self.assertEqual(
            second_query["assets"][0]["relationships"], relations
        )

    def test_unpacked_asset_query_rejects_sqlite_content_not_bound_by_catalog_hash(self):
        self._bound_with_assets()
        indexed = json.loads(self.server.index_active_unpacked_assets(wait_seconds=120))
        self.assertNotIn("error", indexed, indexed)
        database = sqlite3.connect(indexed["databasePath"])
        try:
            database.execute("UPDATE assets SET kind='other' WHERE kind='shader'")
            database.commit()
        finally:
            database.close()

        rejected = json.loads(self.server.query_active_unpacked_assets())
        self.assertFalse(rejected["applied"])
        self.assertIn("error", rejected)
        self.assertIn("embedded manifest hash", rejected["error"])

    def test_unpacked_asset_reference_bound_is_explicit_and_not_a_complete_claim(self):
        self._bound_with_assets()
        assets = Path(self.service.status()["assetRoot"])
        (assets / "many.hlsl").write_text(
            "".join('#include "missing-%03d.hlsli"\n' % index for index in range(300)),
            encoding="utf-8",
        )

        indexed = json.loads(self.server.index_active_unpacked_assets(wait_seconds=120))
        self.assertNotIn("error", indexed, indexed)
        self.assertEqual(indexed["diagnostics"]["referenceTruncatedAssets"], 1)
        self.assertTrue(indexed["coverage"]["complete"])
        self.assertFalse(indexed["coverage"]["truncated"])
        query = self._json(
            self.server.query_active_unpacked_assets(text="many.hlsl")
        )
        selected = query["assets"][0]
        self.assertEqual(selected["metadata"]["referenceCountObserved"], 300)
        self.assertEqual(selected["metadata"]["referenceCountIndexed"], 256)
        self.assertTrue(selected["metadata"]["referencesTruncated"])
        self.assertEqual(len(selected["relationships"]), 256)

    def test_unpacked_asset_file_limit_is_truthful_and_keeps_previous_catalog(self):
        """A scan that hits its file bound says so and leaves the last catalog readable.

        The bound belongs to the scan, not to the MCP tool: indexing runs in the
        background job now, so this exercises the catalog service the job drives.
        """

        from src.asset_catalog import AssetCatalogScanLimitError, AssetCatalogService

        self._bound_with_assets()
        first = json.loads(self.server.index_active_unpacked_assets(wait_seconds=120))
        self.assertNotIn("error", first, first)
        assets = Path(self.service.status()["assetRoot"])
        (assets / "second.hlsl").write_text("float4 second();\n", encoding="utf-8")
        database = Path(first["databasePath"])

        with self.assertRaises(AssetCatalogScanLimitError) as raised:
            AssetCatalogService(database).scan(assets, build_id="build-1", max_files=1)
        self.assertGreaterEqual(raised.exception.observed_files, 1)
        self.assertEqual(raised.exception.maximum_files, 1)

        status = json.loads(self.server.get_active_unpacked_asset_index_status())
        self.assertTrue(status["ready"])
        self.assertEqual(status["catalogSha256"], first["catalogSha256"])

    def test_unpacked_asset_scan_does_not_follow_redirected_entries(self):
        self._bound_with_assets()
        assets = Path(self.service.status()["assetRoot"])
        outside = self.root / "outside-secret.hlsl"
        outside.write_text("float4 secret();\n", encoding="utf-8")
        redirected = assets / "redirected-secret.hlsl"
        try:
            redirected.symlink_to(outside)
        except OSError as error:
            self.skipTest("symbolic links are unavailable: %s" % error)

        indexed = json.loads(self.server.index_active_unpacked_assets(wait_seconds=120))
        self.assertNotIn("error", indexed, indexed)
        self.assertGreaterEqual(indexed["diagnostics"]["skippedSymlinks"], 1)
        query = self._json(
            self.server.query_active_unpacked_assets(text="redirected-secret")
        )
        self.assertEqual(query["count"], 0)

    def test_unpacked_asset_rejects_windows_reparse_attribute(self):
        fake_status = types.SimpleNamespace(st_file_attributes=0x0400)
        with mock.patch.object(Path, "lstat", return_value=fake_status), mock.patch.object(
            Path, "is_symlink", return_value=False
        ):
            self.assertTrue(asset_catalog_module._is_redirected(Path("redirected")))

    def test_active_shader_asset_evidence_not_ready_is_non_blocking_and_read_only(self):
        ensured = self._json(
            self.server.ensure_active_replay_project("Example Game")
        )
        missing_root = self._json(
            self.server.query_active_shader_asset_evidence(142, "fragment")
        )
        self.assertFalse(missing_root["ready"])
        self.assertEqual(missing_root["reasonCode"], "asset-root-not-configured")
        self.assertIn(
            "configure_active_project_asset_root",
            missing_root["prerequisites"],
        )
        self.assertIn(
            "configure_active_project_asset_root",
            missing_root["availableCapabilities"],
        )
        self.assertNotIn("requiredAction", missing_root)
        self.assertTrue(missing_root["policy"]["readOnlyDatabase"])

        self._json(
            self.server.configure_active_replay_project(
                "example-game", "build-1", "windows-x64", "d3d11"
            )
        )
        assets = self.root / "not-yet-prepared-assets"
        assets.mkdir()
        # Keep this read-only query fixture unindexed; automatic activation
        # refresh is exercised separately from database-unavailable behavior.
        with mock.patch.object(
            self.server, "_ensure_asset_activation_content_refresh", return_value=None
        ) as refresh:
            self._json(self.server.configure_active_project_asset_root(str(assets)))
        refresh.assert_called_once_with(force_activation=True, depth="names")
        asset_dictionary = (
            Path(ensured["project"]["paths"]["sharedDictionaries"]) / "assets"
        )
        self.assertFalse(asset_dictionary.exists())

        unprepared = self._json(
            self.server.query_active_shader_asset_evidence(142, "pixel")
        )

        self.assertFalse(unprepared["ready"])
        self.assertEqual(unprepared["reasonCode"], "shader-catalog-not-prepared")
        self.assertIn(
            "get_active_shader_workspace_path",
            unprepared["availableCapabilities"],
        )
        self.assertNotIn("requiredAction", unprepared)
        self.assertEqual(unprepared["requestedShader"], {"event_id": 142, "stage": "fragment"})
        self.assertFalse(unprepared["assetEvidenceBlocked"])
        self.assertFalse(unprepared["policy"]["fullAssetScanRequired"])
        self.assertFalse(unprepared["policy"]["activeFullCatalogOnly"])
        self.assertNotIn("prepare_active_capture_project", json.dumps(unprepared))
        self.assertFalse(asset_dictionary.exists())
        self.assertFalse(unprepared["policy"]["assetScanTriggered"])
        self.assertFalse(unprepared["policy"]["compilerInvoked"])
        self.assertFalse(unprepared["policy"]["aliasMutation"])

    def test_active_shader_asset_evidence_requires_prepared_managed_database(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        self._json(
            self.server.configure_active_replay_project(
                "example-game", "build-1", "windows-x64", "d3d11"
            )
        )
        self._json(self.server.prepare_active_capture_project())
        assets = self.root / "configured-after-prepare"
        assets.mkdir()
        # Deliberately leave the managed database absent for this query test.
        with mock.patch.object(
            self.server, "_ensure_asset_activation_content_refresh", return_value=None
        ) as refresh:
            self._json(self.server.configure_active_project_asset_root(str(assets)))
        refresh.assert_called_once_with(force_activation=True, depth="names")
        assets_dictionary = (
            Path(self.service.status()["paths"]["sharedDictionaries"]) / "assets"
        )
        self.assertFalse(assets_dictionary.exists())

        result = self._json(
            self.server.query_active_shader_asset_evidence(142, "fragment")
        )

        self.assertFalse(result["ready"])
        self.assertEqual(
            result["reasonCode"], "managed-symbol-database-unavailable"
        )
        self.assertFalse(result["assetEvidenceBlocked"])
        self.assertIn("search_unpacked_assets", result["availableCapabilities"])
        self.assertNotIn("recommendedActions", result)
        self.assertFalse(assets_dictionary.exists())

    def test_active_shader_asset_evidence_accepts_only_covered_partial_scope(self):
        self._bound_with_assets()
        prepared = self._json(self.server.prepare_active_capture_project())
        session_id = prepared["capture"]["captureSessionId"]
        record = self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS[session_id]
        record.update(catalogScope="event-stage", coveredEventIds=[142], coveredStages=["fragment"])
        calls_before = list(self.ipc.calls)

        with mock.patch.object(self.server.SymbolService, "scan", side_effect=AssertionError("query must not scan")), \
                mock.patch.object(self.server, "_bootstrap_active_shader_catalog", side_effect=AssertionError("query must not export")):
            covered = self._json(self.server.query_active_shader_asset_evidence(142, "pixel"))
            other_event = self._json(self.server.query_active_shader_asset_evidence(143, "fragment"))
            other_stage = self._json(self.server.query_active_shader_asset_evidence(142, "vertex"))

        self.assertTrue(covered["ready"], covered)
        self.assertEqual(covered["catalogCoverage"], {
            "catalogScope": "event-stage", "fullFrame": False,
            "coveredEventIds": [142], "coveredStages": ["fragment"],
        })
        self.assertFalse(covered["policy"]["activeFullCatalogOnly"])
        for result in (other_event, other_stage):
            self.assertFalse(result["ready"])
            self.assertEqual(result["reasonCode"], "shader-not-covered-by-catalog")
            self.assertIn(
                "get_active_shader_workspace_path",
                result["availableCapabilities"],
            )
            self.assertNotIn("requiredAction", result)
            self.assertFalse(result["policy"]["assetScanTriggered"])
        effects = {"export_shader_catalog", "load_symbol_aliases", "apply_shader_candidate_shared_resource"}
        self.assertEqual([call for call in self.ipc.calls if call[0] in effects],
                         [call for call in calls_before if call[0] in effects])

    def test_active_shader_asset_evidence_partial_scope_retains_content_validation(self):
        self._bound_with_assets()
        prepared = self._json(self.server.prepare_active_capture_project())
        record = self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS[prepared["capture"]["captureSessionId"]]
        record.update(catalogScope="event-stage", coveredEventIds=[142], coveredStages=["fragment"])
        catalog = Path(record["catalogPath"])
        catalog.write_bytes(catalog.read_bytes() + b"\n")
        result = self._json(self.server.query_active_shader_asset_evidence(142, "fragment"))
        self.assertFalse(result["ready"])
        self.assertEqual(result["reasonCode"], "prepared-evidence-unavailable")
        self.assertIn("hash validation", result["reason"])
        self.assertNotIn("requiredAction", result)
        self.assertFalse(result["policy"]["assetScanTriggered"])

    def test_active_shader_asset_evidence_binds_identity_and_labels_relations(self):
        self._bound_with_assets()
        assets = Path(self.service.status()["assetRoot"])
        constant_buffer = {
            "name": "cb0",
            "space": 0,
            "bind": 0,
            "arraySize": 1,
            "byteSize": 16,
            "bufferBacked": True,
            "inlineDataBytes": False,
            "compileConstants": False,
            "fields": [
                {
                    "name": "_child0",
                    "byteOffset": 0,
                    "byteSize": 16,
                    "type": "float4",
                    "rows": 1,
                    "columns": 4,
                    "bitFieldOffset": 0,
                    "bitFieldSize": 0,
                }
            ],
        }
        self.ipc.shader["constantBuffers"] = [copy.deepcopy(constant_buffer)]
        self.ipc.use["constantBlocks"] = [copy.deepcopy(constant_buffer)]
        fingerprint = fingerprint_bytes(self.ipc.blob)
        self.assertIsNotNone(fingerprint)
        (assets / "HeroMaterial.bin").write_bytes(self.ipc.blob)
        # Keep physical ownership ambiguous so project preparation records the
        # candidates but does not create/apply a verified display alias. This
        # test is exclusively about the later read-only evidence query.
        (assets / "HeroMaterialCopy.bin").write_bytes(self.ipc.blob)
        (assets / "HeroMaterial.hlsl").write_text(
            "float4 main() : SV_Target { return 1; }\n", encoding="utf-8"
        )
        asset_interface = _interface()
        asset_interface["constantBuffers"] = [
            {
                **copy.deepcopy(constant_buffer),
                "name": "PerMaterial",
                "fields": [
                    {
                        **copy.deepcopy(constant_buffer["fields"][0]),
                        "name": "BaseColor",
                    }
                ],
            }
        ]
        (assets / "symbols.json").write_text(
            json.dumps(
                {
                    "schema": "renderdoc-ultra-symbols/v1",
                    "shaders": [
                        {
                            "name": "HeroMaterial",
                            "path": "Game/Shaders/HeroMaterial.shader",
                            "stage": "pixel",
                            "entryPoint": "main",
                            "blobPath": "HeroMaterial.bin",
                            "rawSha256": fingerprint.raw_sha256,
                            "codeSha256": fingerprint.code_sha256,
                            "materialName": "HeroSurface",
                            **asset_interface,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        prepared = self._json(self.server.prepare_active_capture_project())
        database = (
            Path(prepared["project"]["paths"]["sharedDictionaries"])
            / "assets"
            / "symbols-v3.sqlite3"
        )
        self.assertTrue(database.is_file())
        before = database.stat()
        exports_before = len(
            [call for call in self.ipc.calls if call[0] == "export_shader_catalog"]
        )
        alias_loads_before = len(
            [call for call in self.ipc.calls if call[0] == "load_symbol_aliases"]
        )

        with mock.patch.object(
            self.server.SymbolService,
            "scan",
            side_effect=AssertionError("read-only query must not scan assets"),
        ):
            result = self._json(
                self.server.query_active_shader_asset_evidence(
                    142, "pixel", top_k=10
                )
            )

        after = database.stat()
        self.assertTrue(result["ready"])
        self.assertEqual(result["boundIdentity"]["captureId"], self.ipc.capture_id)
        self.assertEqual(result["boundIdentity"]["eventId"], 142)
        self.assertEqual(result["boundIdentity"]["stage"], "fragment")
        self.assertEqual(result["boundIdentity"]["shaderResourceId"], 902)
        self.assertEqual(
            result["boundIdentity"]["rawSha256"], self.ipc.shader["rawSha256"]
        )
        self.assertEqual(
            result["boundIdentity"]["catalogSha256"],
            prepared["stagedCatalog"]["catalogSha256"],
        )
        self.assertRegex(
            result["boundIdentity"]["catalogBindingSha256"], r"^[0-9a-f]{64}$"
        )
        relation = result["relationView"]
        self.assertEqual(
            relation["captureShader"]["cbufferEvidence"]["evidenceClass"],
            "observed",
        )
        self.assertEqual(
            relation["captureShader"]["cbufferEvidence"]["blocks"][0]["name"],
            "cb0",
        )
        self.assertGreater(relation["candidateCount"], 0)
        manifest_relation = next(
            candidate
            for candidate in relation["assetCandidates"]
            if candidate["assetRecord"]["displayName"] == "HeroMaterial"
        )
        self.assertEqual(
            manifest_relation["assetRecord"]["evidenceClass"], "observed"
        )
        self.assertEqual(
            manifest_relation["cbufferEvidence"]["asset"]["evidenceClass"],
            "inferred",
        )
        self.assertEqual(
            manifest_relation["cbufferEvidence"]["asset"]["blocks"][0]["name"],
            "PerMaterial",
        )
        self.assertTrue(manifest_relation["relatedTextEvidence"])
        self.assertTrue(
            all(
                item["evidenceClass"] == "inferred"
                for item in manifest_relation["relatedTextEvidence"]
            )
        )
        self.assertEqual(
            manifest_relation["materialEvidence"]["observedEntities"], []
        )
        self.assertFalse(
            manifest_relation["materialEvidence"]["materialEntitySynthesized"]
        )
        self.assertTrue(
            manifest_relation["materialEvidence"]["inferredHints"]
        )
        self.assertTrue(result["policy"]["readOnlyDatabase"])
        self.assertFalse(result["policy"]["assetScanTriggered"])
        self.assertFalse(result["policy"]["compilerInvoked"])
        self.assertFalse(result["policy"]["aliasMutation"])
        self.assertEqual(
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns),
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
        )
        self.assertEqual(
            len([call for call in self.ipc.calls if call[0] == "export_shader_catalog"]),
            exports_before,
        )
        self.assertEqual(
            len([call for call in self.ipc.calls if call[0] == "load_symbol_aliases"]),
            alias_loads_before,
        )

    def test_active_shader_asset_evidence_rejects_unbounded_top_k(self):
        for value in (0, 11, True):
            with self.subTest(top_k=value):
                result = json.loads(
                    self.server.query_active_shader_asset_evidence(
                        142, "fragment", top_k=value
                    )
                )
                self.assertIn("error", result)

    def test_pending_project_stages_full_capture_and_blocks_physical_reuse(self):
        ensured = self._json(self.server.ensure_active_replay_project("Example Game"))
        self.assertEqual(ensured["project"]["state"], "pending-scope")
        prepared = self._json(self.server.prepare_active_capture_project())
        self.assertEqual(
            prepared["schema"], "replaylab-active-capture-prepare/v1"
        )
        self.assertEqual(prepared["capture"]["captureId"], self.ipc.capture_id)
        self.assertEqual(prepared["stagedCatalog"]["captureId"], self.ipc.capture_id)
        self.assertFalse(prepared["stagedCatalog"]["physicalKnowledgeImportAllowed"])
        self.assertTrue(prepared["reusePlan"]["blocked"])
        self.assertFalse(prepared["symbolResolution"]["attempted"])
        self.assertFalse(prepared["policy"]["aiProvisionalAutoApply"])
        self.assertTrue(prepared["captureExport"]["temporaryExportRemoved"])
        self.assertTrue(Path(prepared["captureExport"]["catalogPath"]).is_file())
        export_calls = [item for item in self.ipc.calls if item[0] == "export_shader_catalog"]
        self.assertEqual(len(export_calls), 1)
        self.assertEqual(export_calls[0][1]["event_ids"], [])
        self.assertEqual(
            export_calls[0][2],
            self.server.ACTIVE_CAPTURE_EXPORT_TIMEOUT_SECONDS,
        )
        self.assertGreaterEqual(export_calls[0][2], 900.0)
        catalog_root = Path(prepared["project"]["paths"]["captureCatalogs"])
        self.assertFalse(any(item.name.startswith(".current-") for item in catalog_root.iterdir()))

    def test_internal_prepare_passes_only_token_bound_progress_parameters(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        token = "d" * 32

        prepared = self._json(
            self.server._prepare_active_capture_project(progress_token=token)
        )

        self.assertEqual(prepared["capture"]["captureSessionId"], "a" * 32)
        export = next(
            params
            for method, params, unused_timeout in self.ipc.calls
            if method == "export_shader_catalog"
        )
        self.assertEqual(export["progress_token"], token)
        self.assertEqual(export["progress_capture_session_id"], "a" * 32)
        self.assertNotIn("progress_path", export)

    def test_internal_prepare_forwards_live_progress_on_a_sidecar_thread(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        instances = self.root / "ipc-v2" / "instances"
        instance = instances / ("123-%s" % ("b" * 32))
        requests = instance / "requests"
        responses = instance / "responses"
        requests.mkdir(parents=True)
        responses.mkdir()
        self.ipc._instance_mailbox = lambda: (str(requests), str(responses))
        token = "d" * 32
        progress_path = instance / (
            "shader-catalog-progress-%s.json" % token
        )
        callback_seen = threading.Event()
        callback_threads = []
        observations = []
        export_threads = []
        caller_thread = threading.get_ident()
        original_call = self.ipc.call

        def publish(current, event_id, updated_ms):
            value = {
                "schema": self.server.SHADER_CATALOG_PROGRESS_SCHEMA,
                "schemaVersion": 1,
                "operationToken": token,
                "instanceId": instance.name,
                "captureSessionId": "a" * 32,
                "phase": "export-shader-bindings",
                "eventId": event_id,
                "current": current,
                "total": 2,
                "updatedUnixMs": updated_ms,
            }
            temporary = progress_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(value), encoding="utf-8")
            os.replace(temporary, progress_path)

        def call_with_progress(method, params=None, timeout=None):
            if method != "export_shader_catalog":
                return original_call(method, params, timeout)
            export_threads.append(threading.get_ident())
            publish(1, 101, 1001)
            if not callback_seen.wait(timeout=2.0):
                raise AssertionError("progress was not forwarded during export")
            result = original_call(method, params, timeout)
            publish(2, 202, 1002)
            return result

        def receive(observation):
            callback_threads.append(threading.get_ident())
            observations.append(observation)
            if observation.get("schema") == self.server.SHADER_CATALOG_PROGRESS_SCHEMA:
                callback_seen.set()

        with mock.patch.object(
            self.server, "INSTANCE_ROOT", str(instances)
        ), mock.patch.object(
            self.server,
            "SHADER_CATALOG_PROGRESS_POLL_SECONDS",
            0.005,
        ), mock.patch.object(
            self.ipc, "call", side_effect=call_with_progress
        ):
            prepared = self._json(
                self.server._prepare_active_capture_project_with_progress(
                    token, receive
                )
            )

        self.assertEqual(prepared["capture"]["captureSessionId"], "a" * 32)
        self.assertEqual(export_threads, [caller_thread])
        export_observations = [
            item
            for item in observations
            if item.get("schema") == self.server.SHADER_CATALOG_PROGRESS_SCHEMA
        ]
        self.assertTrue(export_observations)
        self.assertEqual(
            [item["current"] for item in export_observations], [1, 2]
        )
        self.assertTrue(
            all(
                set(item)
                == {
                    "schema",
                    "schemaVersion",
                    "operationToken",
                    "instanceId",
                    "captureSessionId",
                    "phase",
                    "eventId",
                    "current",
                    "total",
                    "updatedUnixMs",
                }
                for item in export_observations
            )
        )
        self.assertFalse(progress_path.exists())

    def test_progress_callback_failure_does_not_change_prepare_or_cleanup(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        instances = self.root / "ipc-v2" / "instances"
        instance = instances / ("456-%s" % ("c" * 32))
        requests = instance / "requests"
        responses = instance / "responses"
        requests.mkdir(parents=True)
        responses.mkdir()
        self.ipc._instance_mailbox = lambda: (str(requests), str(responses))
        token = "e" * 32
        progress_path = instance / (
            "shader-catalog-progress-%s.json" % token
        )
        original_call = self.ipc.call
        callback_calls = []

        def call_with_final_progress(method, params=None, timeout=None):
            result = original_call(method, params, timeout)
            if method == "export_shader_catalog":
                value = {
                    "schema": self.server.SHADER_CATALOG_PROGRESS_SCHEMA,
                    "schemaVersion": 1,
                    "operationToken": token,
                    "instanceId": instance.name,
                    "captureSessionId": "a" * 32,
                    "phase": "export-shader-bindings",
                    "eventId": 202,
                    "current": 1,
                    "total": 1,
                    "updatedUnixMs": 2001,
                }
                progress_path.write_text(json.dumps(value), encoding="utf-8")
            return result

        def broken_callback(observation):
            callback_calls.append(observation)
            raise RuntimeError("display stopped")

        with mock.patch.object(
            self.server, "INSTANCE_ROOT", str(instances)
        ), mock.patch.object(
            self.server,
            "SHADER_CATALOG_PROGRESS_POLL_SECONDS",
            10.0,
        ), mock.patch.object(
            self.ipc, "call", side_effect=call_with_final_progress
        ), mock.patch.object(self.server.logger, "warning") as warning:
            prepared = self._json(
                self.server._prepare_active_capture_project_with_progress(
                    token, broken_callback
                )
            )

        self.assertEqual(prepared["capture"]["captureId"], self.ipc.capture_id)
        self.assertEqual(len(callback_calls), 1)
        self.assertEqual(callback_calls[0]["current"], 1)
        self.assertTrue(warning.called)
        self.assertFalse(progress_path.exists())

    def test_unavailable_progress_channel_falls_back_to_physical_prepare(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        observations = []

        with mock.patch.object(self.server.logger, "warning") as warning:
            prepared = self._json(
                self.server._prepare_active_capture_project_with_progress(
                    "f" * 32, observations.append
                )
            )

        self.assertEqual(prepared["capture"]["captureId"], self.ipc.capture_id)
        self.assertEqual(
            observations[0]["phase"], "prepare-export-start"
        )
        self.assertEqual(observations[-1]["phase"], "prepare-complete")
        self.assertTrue(
            all(
                item["schema"]
                == self.server.ACTIVE_CAPTURE_PREPARE_PROGRESS_SCHEMA
                for item in observations
            )
        )
        self.assertTrue(warning.called)
        export = next(
            params
            for method, params, unused_timeout in self.ipc.calls
            if method == "export_shader_catalog"
        )
        self.assertNotIn("progress_token", export)
        self.assertNotIn("progress_capture_session_id", export)

    def test_prepare_prefers_responsive_export_job_and_forwards_poll_progress(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        original_call = self.ipc.call
        job_id = "9" * 32
        begin_params = {}
        poll_count = [0]
        observations = []
        async_methods = []

        def snapshot(status, current, total, event_id, **extra):
            return {
                "schema": self.server.SHADER_CATALOG_EXPORT_JOB_SCHEMA,
                "schemaVersion": 1,
                "jobId": job_id,
                "status": status,
                "captureSessionId": "a" * 32,
                "cancellationRequested": False,
                "current": current,
                "total": total,
                "eventId": event_id,
                **extra,
            }

        def async_call(method, params=None, timeout=None):
            async_methods.append(method)
            if method == "begin_export_shader_catalog":
                begin_params.update(dict(params or {}))
                return snapshot("queued", 0, 0, 0)
            if method == "poll_export_shader_catalog":
                self.assertEqual(params, {"job_id": job_id})
                poll_count[0] += 1
                if poll_count[0] == 1:
                    return snapshot("running", 1, 2, 101)
                return snapshot("completed", 2, 2, 202)
            if method == "consume_export_shader_catalog":
                self.assertEqual(params, {"job_id": job_id})
                result = original_call(
                    "export_shader_catalog", begin_params, timeout
                )
                # The helper call only materializes deterministic catalog test
                # bytes; it is not a Host legacy fallback request.
                self.ipc.calls.pop()
                return snapshot("completed", 2, 2, 202, result=result)
            if method == "cancel_export_shader_catalog":
                self.fail("completed export job must not be cancelled")
            return original_call(method, params, timeout)

        with mock.patch.object(
            self.ipc, "call", side_effect=async_call
        ), mock.patch.object(
            self.server, "SHADER_CATALOG_EXPORT_JOB_POLL_SECONDS", 0.0
        ):
            prepared = self._json(
                self.server._prepare_active_capture_project(
                    progress_callback=observations.append
                )
            )

        self.assertEqual(prepared["capture"]["captureId"], self.ipc.capture_id)
        self.assertEqual(begin_params["event_ids"], [])
        self.assertIn("begin_export_shader_catalog", async_methods)
        self.assertIn("poll_export_shader_catalog", async_methods)
        self.assertIn("consume_export_shader_catalog", async_methods)
        self.assertNotIn("export_shader_catalog", async_methods)
        export_progress = [
            item
            for item in observations
            if item["phase"] == "export-shader-bindings"
        ]
        self.assertEqual(
            [(item["current"], item["total"], item["item"])
             for item in export_progress],
            [(1, 2, "EID 101"), (2, 2, "EID 202")],
        )
        self.assertEqual(observations[-1]["phase"], "prepare-complete")

    def test_async_export_cancel_checkpoint_requests_bridge_cancel(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        original_call = self.ipc.call
        job_id = "8" * 32
        methods = []
        checkpoints = []

        class RestoreCancelled(RuntimeError):
            pass

        def snapshot(status, *, error=None):
            value = {
                "schema": self.server.SHADER_CATALOG_EXPORT_JOB_SCHEMA,
                "schemaVersion": 1,
                "jobId": job_id,
                "status": status,
                "captureSessionId": "a" * 32,
                "cancellationRequested": status == "cancelled",
                "current": 0,
                "total": 0,
                "eventId": 0,
            }
            if error is not None:
                value["error"] = error
            return value

        def async_call(method, params=None, timeout=None):
            methods.append(method)
            if method == "begin_export_shader_catalog":
                return snapshot("running")
            if method == "cancel_export_shader_catalog":
                return snapshot("cancelled", error="cancelled by test")
            return original_call(method, params, timeout)

        def checkpoint(phase):
            checkpoints.append(phase)
            if phase == "during asynchronous full-frame Shader catalog export":
                raise RestoreCancelled("stop export")

        with mock.patch.object(self.ipc, "call", side_effect=async_call):
            with self.assertRaisesRegex(RestoreCancelled, "stop export"):
                self.server._prepare_active_capture_project(
                    progress_callback=lambda unused: None,
                    control_checkpoint=checkpoint,
                )

        self.assertIn("begin_export_shader_catalog", methods)
        self.assertIn("cancel_export_shader_catalog", methods)
        self.assertNotIn("export_shader_catalog", methods)

    def test_async_export_transport_failure_never_duplicates_legacy_export(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        original_call = self.ipc.call
        methods = []

        def failed_begin(method, params=None, timeout=None):
            methods.append(method)
            if method == "begin_export_shader_catalog":
                raise RuntimeError("bridge response was lost after begin")
            return original_call(method, params, timeout)

        with mock.patch.object(self.ipc, "call", side_effect=failed_begin):
            result = json.loads(self.server._prepare_active_capture_project())

        self.assertIn("error", result)
        self.assertIn("begin_export_shader_catalog", methods)
        self.assertNotIn("export_shader_catalog", methods)

    def test_progress_sidecar_reader_binds_instance_token_and_capture_session(self):
        instances = self.root / "ipc-v2" / "instances"
        instance = instances / ("123-%s" % ("b" * 32))
        requests = instance / "requests"
        responses = instance / "responses"
        requests.mkdir(parents=True)
        responses.mkdir()

        class Selector:
            @staticmethod
            def _instance_mailbox():
                return str(requests), str(responses)

        old_ipc = self.server.ipc
        self.server.ipc = Selector()
        try:
            with mock.patch.object(self.server, "INSTANCE_ROOT", str(instances)):
                channel = self.server._shader_catalog_progress_channel("a" * 32)
                path = Path(channel["path"])
                value = {
                    "schema": self.server.SHADER_CATALOG_PROGRESS_SCHEMA,
                    "schemaVersion": 1,
                    "operationToken": channel["operationToken"],
                    "instanceId": channel["instanceId"],
                    "captureSessionId": channel["captureSessionId"],
                    "phase": "export-shader-bindings",
                    "eventId": 42,
                    "current": 2,
                    "total": 9,
                    "updatedUnixMs": 123456,
                }
                path.write_text(json.dumps(value), encoding="utf-8")
                self.assertEqual(
                    self.server._read_shader_catalog_progress(channel), value
                )

                changed = dict(value)
                changed["captureSessionId"] = "c" * 32
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "binding"):
                    self.server._read_shader_catalog_progress(channel)

                self.server._cleanup_shader_catalog_progress(channel)
                self.assertFalse(path.exists())
        finally:
            self.server.ipc = old_ipc

    def test_new_capture_without_live_id_uses_exported_canonical_identity(self):
        self.ipc.context["capture"]["captureId"] = ""
        self._json(self.server.ensure_active_replay_project("Example Game"))

        prepared = self._json(self.server.prepare_active_capture_project())

        self.assertEqual(prepared["capture"]["captureId"], self.ipc.capture_id)
        self.assertEqual(prepared["stagedCatalog"]["captureId"], self.ipc.capture_id)
        export = next(
            params
            for method, params, unused_timeout in self.ipc.calls
            if method == "export_shader_catalog"
        )
        self.assertEqual(export["event_ids"], [])
        self.assertNotIn("capture_id", export)
        self.assertNotIn("expected_capture_id", export)
        selected = self.server._active_capture_catalog_selection(142, "fragment")
        self.assertIsNotNone(selected)
        path_result = self._json(
            self.server.get_active_shader_workspace_path(142, "fragment")
        )
        self.assertNotIn("error", path_result, path_result)
        self.assertEqual(path_result["captureId"], self.ipc.capture_id)

    def _historical_applied_workspace(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        historical = self._json(
            self.server.get_active_shader_workspace_path(142, "fragment")
        )
        self.ipc.export_revision += 1
        self._json(self.server.prepare_active_capture_project())
        current = self._json(
            self.server.get_active_shader_workspace_path(142, "fragment")
        )
        self.assertNotEqual(historical["catalogSha256"], current["catalogSha256"])
        self.assertNotEqual(historical["workspacePath"], current["workspacePath"])
        workspace = Path(historical["workspacePath"])
        workspace.mkdir(parents=True)
        workspace_id = "b" * 32
        replacement_id = "c" * 32
        status = {
            "schema": "renderdoc-ultra-shader-workspace-status/v1",
            "workspacePath": str(workspace),
            "workspaceId": workspace_id,
            "capture": {
                "captureId": self.ipc.capture_id,
                "capturePath": self.ipc.context["capture"]["path"],
                "api": self.ipc.context["capture"]["api"],
                "catalogSha256": historical["catalogSha256"],
            },
            "shader": {
                "resourceId": 902,
                "stage": "fragment",
                "entryPoint": "main",
                "rawSha256": self.ipc.shader["rawSha256"],
            },
            "baseline": {"rawSha256": self.ipc.shader["rawSha256"]},
            "stage": "fragment",
            "entryPoint": "main",
            "routePlan": {
                "schema": "renderdoc-ultra-shader-workspace-route-plan/v2",
                "schemaVersion": 2,
                "editableFormat": "hlsl",
            },
            "source": {"present": True, "format": "hlsl"},
            "representation": {"lane": "executable-editable"},
            "recoveryPlan": {},
        }
        remembered = {
            "replacementId": replacement_id,
            "captureId": self.ipc.capture_id,
            "captureSessionId": self.ipc.context["capture"]["captureSessionId"],
            "workspaceId": workspace_id,
            "workspacePath": str(workspace),
            "originalResourceId": 902,
            "originalRawSha256": self.ipc.shader["rawSha256"],
            "stage": "fragment",
            "entryPoint": "main",
            "candidateRawSha256": "d" * 64,
            "revision": 1,
            "api": "D3D11",
            "encoding": "dxbc",
        }
        live = {
            "schema": "renderdoc-ultra-shared-shader-replacement-list/v1",
            "schemaVersion": 1,
            "captureId": self.ipc.capture_id,
            "captureSessionId": self.ipc.context["capture"]["captureSessionId"],
            "originalShadersPreserved": True,
            "replayMutation": False,
            "count": 1,
            "replacements": [{
                "replacementId": replacement_id,
                "workspaceId": workspace_id,
                "originalResourceId": 902,
                "originalRawSha256": self.ipc.shader["rawSha256"],
                "stage": "fragment",
                "entryPoint": "main",
                "candidateRawSha256": "d" * 64,
                "revision": 1,
                "api": "D3D11",
                "encoding": "dxbc",
                "resourceScoped": True,
                "eventScoped": False,
                "allOriginalResourceUsesAffected": True,
                "originalShaderPreserved": True,
            }],
        }
        return historical, current, status, remembered, live

    def test_active_resolver_returns_exact_live_restored_workspace(self):
        historical, current, status, remembered, live = (
            self._historical_applied_workspace()
        )
        original_call = self.ipc.call

        def bridge_call(method, params=None, timeout=None):
            if method == "get_shared_shader_replacements":
                return copy.deepcopy(live)
            return original_call(method, params, timeout)

        self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()
        self.server._SHARED_SHADER_REPLACEMENT_RECORDS[
            remembered["replacementId"]
        ] = remembered
        workspace_service = mock.Mock()
        workspace_service.status.return_value = status
        try:
            with mock.patch.object(self.ipc, "call", side_effect=bridge_call), \
                    mock.patch.object(self.server, "_shader_workspace_service",
                                      return_value=workspace_service):
                with mock.patch.object(self.service, "future_frame_plan",
                                       side_effect=RuntimeError("optional project discovery unavailable")) as future, \
                        mock.patch.object(self.server, "_existing_project_shader_sources",
                                          side_effect=RuntimeError("optional source search unavailable")) as discover:
                    resolved = self._json(
                        self.server.get_active_shader_workspace_path(142, "fragment")
                    )
                future.assert_not_called()
                discover.assert_not_called()
                workspace_service.status.assert_called_once_with(str(Path(historical["workspacePath"])))
                self.assertEqual(resolved["projectReuse"]["status"], "deferred")
                self.assertFalse(resolved["projectReuse"]["performed"])
                self.assertEqual(resolved["projectReuse"]["availableCapability"],
                                 "get_active_project_future_frame_plan")
                self.assertEqual(resolved["sourceDiscoveryStatus"], "deferred")
                self.assertEqual(resolved["existingSourceWorkspaces"], [])
                self.assertEqual(resolved["sourceDiscoveryDiagnostics"], [])
                self.assertEqual(resolved["workspacePath"], historical["workspacePath"])
                self.assertEqual(resolved["workspaceId"], status["workspaceId"])
                self.assertEqual(resolved["catalogSha256"], historical["catalogSha256"])
                self.assertEqual(resolved["activeCatalogSha256"], current["catalogSha256"])
                self.assertEqual(
                    resolved["activeReplacementId"], remembered["replacementId"]
                )
                with mock.patch.object(
                    self.server, "_ensure_workspace_capture_catalog_identity"
                ):
                    prepared = json.loads(self.server.prepare_shader_workspace(
                        142, "fragment", historical["workspacePath"],
                        expected_resource_id=902,
                        expected_entry_point="main",
                        expected_raw_sha256=self.ipc.shader["rawSha256"],
                        expected_capture_id=self.ipc.capture_id,
                        expected_capture_session_id=live["captureSessionId"],
                        admit_existing=True,
                    ))
                self.assertNotIn("error", prepared, prepared)
                self.assertEqual(
                    prepared["result"]["binding"]["workspaceId"], status["workspaceId"]
                )
                self.assertTrue(prepared["result"]["created"]["admittedExisting"])
                wrong_workspace = str(Path(historical["workspacePath"]).parent / ("f" * 64))
                denied = json.loads(self.server.prepare_shader_workspace(
                    142, "fragment", wrong_workspace, admit_existing=True,
                ))
                self.assertIn("deterministic path", denied["error"])
                self.assertFalse(Path(wrong_workspace).exists())
        finally:
            self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()

    def test_active_resolver_edits_latest_project_source_while_retaining_replay_owner(self):
        historical, current, status, remembered, live = self._historical_applied_workspace()
        self._json(self.server.configure_active_replay_project(
            "example-game", "build-1", "windows-x64", "d3d11"))
        status["source"].update(currentSha256="1" * 64,
                                projectSource={"knowledgeId": "2" * 32})
        status["latestCompiled"] = {"sourceSha256": "1" * 64}
        editable = copy.deepcopy(status)
        editable.update(workspacePath=current["workspacePath"], workspaceId="3" * 32)
        editable["capture"]["catalogSha256"] = current["catalogSha256"]
        editable["source"]["currentSha256"] = "4" * 64
        head = {"headKey": "5" * 64, "generation": 2,
                "knowledge": {"knowledgeId": "6" * 32, "sourceSha256": "4" * 64}}
        original_call = self.ipc.call

        def bridge_call(method, params=None, timeout=None):
            if method == "get_shared_shader_replacements":
                return copy.deepcopy(live)
            return original_call(method, params, timeout)

        workspace_service = mock.Mock()
        workspace_service.status.side_effect = lambda path: copy.deepcopy(
            editable if Path(path) == Path(current["workspacePath"]) else status)
        self.server._SHARED_SHADER_REPLACEMENT_RECORDS[remembered["replacementId"]] = remembered
        try:
            with mock.patch.object(self.ipc, "call", side_effect=bridge_call), \
                    mock.patch.object(self.server, "_shader_workspace_service", return_value=workspace_service), \
                    mock.patch.object(self.server.ReplayProjectService, "current_workspace_source_head",
                                      return_value=head) as read_head, \
                    mock.patch.object(self.server, "_project_source_editor_workspace",
                                      return_value=Path(current["workspacePath"])) as materialize, \
                    mock.patch.object(self.service, "future_frame_plan",
                                      side_effect=AssertionError("no full discovery for a known source")):
                result = self._json(self.server.get_active_shader_workspace_path(142, "fragment"))
            read_head.assert_called_once_with("2" * 32)
            self.assertEqual(materialize.call_args.args[3], head["knowledge"])
            self.assertEqual(result["workspacePath"], current["workspacePath"])
            self.assertEqual(result["workspaceId"], "3" * 32)
            self.assertEqual(result["replayWorkspacePath"], historical["workspacePath"])
            self.assertEqual(result["replaySourceSha256"], "1" * 64)
            self.assertEqual(result["activeReplacementId"], remembered["replacementId"])
            self.assertEqual(result["latestProjectSource"]["sourceSha256"], "4" * 64)
            self.assertFalse(result["latestProjectSource"]["applied"])
            workspace_service.create.assert_not_called()
            workspace_service.compile.assert_not_called()
            # Unpublished local edits in this same head must remain the
            # editable source; a hash difference is not a newer publication.
            head["knowledge"]["knowledgeId"] = "2" * 32
            with mock.patch.object(self.ipc, "call", side_effect=bridge_call), \
                    mock.patch.object(self.server, "_shader_workspace_service", return_value=workspace_service), \
                    mock.patch.object(self.server.ReplayProjectService, "current_workspace_source_head", return_value=head), \
                    mock.patch.object(self.server, "_project_source_editor_workspace",
                                      side_effect=AssertionError("must retain this head's local edit")):
                local = self._json(self.server.get_active_shader_workspace_path(142, "fragment"))
            self.assertEqual(local["workspacePath"], historical["workspacePath"])
            self.assertNotIn("latestProjectSource", local)
        finally:
            self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()

    def test_active_resolver_reuses_same_original_at_another_catalog_event(self):
        original_call = self.ipc.call

        def export_second_use(method, params=None, timeout=None):
            result = original_call(method, params, timeout)
            if method == "export_shader_catalog" and params.get("event_ids") == []:
                path = Path(result["catalogPath"])
                catalog = json.loads(path.read_text(encoding="utf-8"))
                catalog["uses"].append({**catalog["uses"][0], "eventId": 143})
                catalog["selection"]["eventIds"].append(143)
                path.write_text(json.dumps(catalog), encoding="utf-8")
            return result

        with mock.patch.object(self.ipc, "call", side_effect=export_second_use):
            historical, unused_current, status, remembered, live = self._historical_applied_workspace()
        live["replacements"][0]["anchorEventId"] = 142

        def bridge_call(method, params=None, timeout=None):
            if method == "get_shared_shader_replacements":
                return copy.deepcopy(live)
            return original_call(method, params, timeout)

        self.server._SHARED_SHADER_REPLACEMENT_RECORDS[remembered["replacementId"]] = remembered
        workspace_service = mock.Mock()
        workspace_service.status.return_value = status
        try:
            with mock.patch.object(self.ipc, "call", side_effect=bridge_call), \
                    mock.patch.object(self.server, "_shader_workspace_service", return_value=workspace_service), \
                    mock.patch.object(self.service, "future_frame_plan", side_effect=AssertionError("discovery deferred")), \
                    mock.patch.object(self.server, "_existing_project_shader_sources", side_effect=AssertionError("discovery deferred")):
                result = self._json(self.server.get_active_shader_workspace_path(143, "fragment"))
            self.assertEqual(result["eventId"], 143)
            self.assertEqual(result["workspacePath"], historical["workspacePath"])
            self.assertEqual(result["activeReplacementId"], remembered["replacementId"])
            self.assertEqual(result["rawSha256"], self.ipc.shader["rawSha256"])
            self.assertEqual(result["sourceDiscoveryStatus"], "deferred")
            self.assertEqual(live["replacements"][0]["anchorEventId"], 142)
        finally:
            self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()

    def test_session_inventory_resolves_other_catalog_owner_without_restore_hint(self):
        historical, current, status, remembered, live = self._historical_applied_workspace()
        owner_capture = "7" * 64
        status["capture"]["captureId"] = owner_capture
        live["replacements"][0].update(
            captureId=owner_capture, captureSessionId=live["captureSessionId"])
        self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()
        ui = {"ui": {
            "captureBinding": {"captureSessionId": live["captureSessionId"],
                               "capturePath": self.ipc.context["capture"]["path"]},
            "workspaces": [{"workspaceId": status["workspaceId"],
                            "workspacePath": historical["workspacePath"],
                            "captureId": owner_capture, **status["shader"]}],
        }}
        original_call = self.ipc.call
        inventory_calls = []

        def bridge_call(method, params=None, timeout=None):
            if method == "get_shared_shader_replacements":
                inventory_calls.append(dict(params))
                return copy.deepcopy(live)
            if method == "get_shader_workspace_ui_state":
                return copy.deepcopy(ui)
            return original_call(method, params, timeout)

        workspace_service = mock.Mock()
        workspace_service.status.return_value = status
        with mock.patch.object(self.ipc, "call", side_effect=bridge_call), \
                mock.patch.object(self.server, "_shader_workspace_service", return_value=workspace_service):
            resolved = self._json(self.server.get_active_shader_workspace_path(142, "fragment"))
            self.assertEqual(resolved["workspacePath"], historical["workspacePath"])
            self.assertEqual(resolved["workspaceId"], status["workspaceId"])
            self.assertNotEqual(resolved["workspacePath"], current["workspacePath"])
            self.assertTrue(inventory_calls[-1]["include_all_capture_catalogs"])
            with mock.patch.object(self.server, "_ensure_workspace_capture_catalog_identity"):
                direct = json.loads(self.server.prepare_shader_workspace(
                    142, "fragment", current["workspacePath"]))
            self.assertNotIn("error", direct, direct)
            self.assertEqual(direct["result"]["workspacePath"], historical["workspacePath"])
            self.assertEqual(direct["result"]["requestedWorkspacePath"], current["workspacePath"])
            self.assertTrue(direct["result"]["reusedLiveOwner"])
            self.assertTrue(direct["result"]["created"]["sourcePreserved"])
            self.assertFalse(Path(current["workspacePath"]).exists())
            with mock.patch.object(self.server, "_ensure_workspace_capture_catalog_identity"):
                admitted = json.loads(self.server.prepare_shader_workspace(
                    142, "fragment", historical["workspacePath"], admit_existing=True))
            self.assertNotIn("error", admitted, admitted)
            self.assertTrue(admitted["result"]["created"]["sourcePreserved"])
            workspace_service.create.assert_not_called()
            workspace_service.decompile.assert_not_called()
            workspace_service.import_project_source.assert_not_called()
            explicit_path = Path(current["workspacePath"])
            explicit_path.mkdir()
            draft = explicit_path / "unsaved-local-source.hlsl"
            draft.write_bytes(b"// independent local draft\n")
            explicit_status = copy.deepcopy(status)
            explicit_status.update(workspacePath=str(explicit_path), workspaceId="9" * 32)
            explicit_status["capture"]["captureId"] = self.ipc.capture_id
            workspace_service.status.side_effect = lambda path: copy.deepcopy(
                explicit_status if Path(path) == explicit_path else status)
            before_queries = len(inventory_calls)
            with mock.patch.object(self.server, "_ensure_workspace_capture_catalog_identity"):
                separate = json.loads(self.server.prepare_shader_workspace(
                    142, "fragment", str(explicit_path), admit_existing=True))
            self.assertNotIn("error", separate, separate)
            self.assertEqual(separate["result"]["workspacePath"], str(explicit_path))
            self.assertFalse(separate["result"]["reusedLiveOwner"])
            self.assertEqual(len(inventory_calls), before_queries)
            self.assertEqual(draft.read_bytes(), b"// independent local draft\n")
            # Source admission also works without a live owner: the original
            # same-file catalog/bytes are checked above. An unrelated/stale
            # replacement is never used as physical apply authority here.
            for changes in ({"originalResourceId": 903}, {"captureSessionId": "0" * 32}):
                previous = copy.deepcopy(live["replacements"][0])
                live["replacements"][0].update(changes)
                with mock.patch.object(self.server, "_ensure_workspace_capture_catalog_identity"):
                    denied = json.loads(self.server.prepare_shader_workspace(
                        142, "fragment", historical["workspacePath"], admit_existing=True))
                self.assertNotIn("error", denied)
                self.assertTrue(denied["result"]["created"]["sourcePreserved"])
                self.assertFalse(denied["renderDocReplacement"]["applied"])
                live["replacements"][0] = previous

    def test_verified_restore_resource_mapping_reuses_only_its_exact_candidate(self):
        historical, unused_current, status, remembered, live = self._historical_applied_workspace()
        status["shader"]["resourceId"] = 901
        status["latestCompiled"] = {"revision": 1, "rawSha256": "d" * 64}
        row = live["replacements"][0]
        row.update(captureId=self.ipc.capture_id, captureSessionId=live["captureSessionId"],
                   restoredOriginalResourceId=901, abiValidationMode="verified-rdc-restore",
                   physicalAbiSha256="f" * 64)
        row["restoreValidationReceipt"] = {"payload": {key: row[key] for key in (
            "candidateRawSha256", "originalRawSha256", "stage", "entryPoint", "encoding",
            "physicalAbiSha256")}, "signature": "bridge-authenticated-origin"}
        original_row = copy.deepcopy(row)
        self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()
        ui = {"ui": {
            "captureBinding": {"captureSessionId": live["captureSessionId"],
                               "capturePath": self.ipc.context["capture"]["path"]},
            "workspaces": [{"workspaceId": status["workspaceId"],
                            "workspacePath": historical["workspacePath"],
                            "captureId": self.ipc.capture_id, **status["shader"]}],
        }}
        original_call = self.ipc.call

        def bridge_call(method, params=None, timeout=None):
            if method == "get_shared_shader_replacements":
                self.assertTrue(params["include_validation_receipts"])
                return copy.deepcopy(live)
            if method == "get_shader_workspace_ui_state":
                return copy.deepcopy(ui)
            return original_call(method, params, timeout)

        service = mock.Mock()
        service.status.return_value = status
        with mock.patch.object(self.ipc, "call", side_effect=bridge_call), \
                mock.patch.object(self.server, "_shader_workspace_service", return_value=service):
            resolved = self._json(self.server.get_active_shader_workspace_path(142, "fragment"))
            self.assertEqual(resolved["workspacePath"], historical["workspacePath"])
            self.assertEqual(resolved["shaderResourceId"], 902)
            with mock.patch.object(self.server, "_ensure_workspace_capture_catalog_identity"):
                prepared = json.loads(self.server.prepare_shader_workspace(
                    142, "fragment", historical["workspacePath"], admit_existing=True))
            self.assertNotIn("error", prepared, prepared)
            self.assertEqual(prepared["result"]["created"]["shader"]["resourceId"], 901)
            self.assertEqual(prepared["result"]["binding"]["originalResourceId"], 902)
            for change in ({"restoredOriginalResourceId": None}, {"restoredOriginalResourceId": 900},
                           {"revision": 2}, {"candidateRawSha256": "0" * 64},
                           {"abiValidationMode": "full-replay-report-and-inline-physical"},
                           {"restoreValidationReceipt": None}):
                with self.subTest(change=change):
                    live["replacements"][0] = {**original_row, **change}
                    result = json.loads(self.server.get_active_shader_workspace_path(142, "fragment"))
                    self.assertIn("error", result)
                    self.assertNotIn("workspacePath", result)
            service.create.assert_not_called()

    def test_active_resolver_keeps_workspace_bytes_and_final_capture_catalog_checks(self):
        historical, unused_current, status, remembered, live = self._historical_applied_workspace()
        session = self.ipc.context["capture"]["captureSessionId"]
        context = copy.deepcopy(self.ipc.context)
        record = copy.deepcopy(self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS[session])
        catalog_path = Path(record["catalogPath"])
        catalog_bytes = catalog_path.read_bytes()
        original_call = self.ipc.call

        def bridge_call(method, params=None, timeout=None):
            if method == "get_shared_shader_replacements":
                return copy.deepcopy(live)
            return original_call(method, params, timeout)

        def checked_status(change):
            if change == "workspace-original-bytes":
                return {**status, "baseline": {"rawSha256": "f" * 64}}
            if change == "workspace-validator-error":
                raise ValueError("workspace candidate content/hash mismatch")
            if change == "catalog-bytes":
                catalog_path.write_bytes(catalog_bytes + b" ")
            if change == "capture-session":
                self.ipc.context["capture"]["captureSessionId"] = "f" * 32
            return copy.deepcopy(status)

        workspace_service = mock.Mock()
        for change in ("workspace-original-bytes", "workspace-validator-error", "catalog-bytes", "capture-session"):
            with self.subTest(change=change):
                self.server._SHARED_SHADER_REPLACEMENT_RECORDS[remembered["replacementId"]] = copy.deepcopy(remembered)
                workspace_service.status.side_effect = lambda unused_path: checked_status(change)
                try:
                    with mock.patch.object(self.ipc, "call", side_effect=bridge_call), \
                            mock.patch.object(self.server, "_shader_workspace_service", return_value=workspace_service), \
                            mock.patch.object(self.service, "future_frame_plan", side_effect=AssertionError("discovery must not mask validation")) as future, \
                            mock.patch.object(self.server, "_existing_project_shader_sources", side_effect=AssertionError("discovery must not mask validation")) as discover:
                        result = json.loads(self.server.get_active_shader_workspace_path(142, "fragment"))
                    self.assertIn("error", result)
                    self.assertNotIn("workspacePath", result)
                    future.assert_not_called()
                    discover.assert_not_called()
                finally:
                    catalog_path.write_bytes(catalog_bytes)
                    self.ipc.context = copy.deepcopy(context)
                    self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS[session] = copy.deepcopy(record)
                    self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()

    def test_prepare_admits_agent_selected_existing_source_without_live_apply(self):
        historical, current, status, unused_owner, unused_live = (
            self._historical_applied_workspace()
        )
        self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()
        workspaces = mock.Mock()
        workspaces.status.side_effect = lambda unused_path: copy.deepcopy(status)
        with mock.patch.object(self.server, "_shader_workspace_service", return_value=workspaces), \
                mock.patch.object(self.server, "_ensure_workspace_capture_catalog_identity"):
            prepared = json.loads(self.server.prepare_shader_workspace(
                142, "fragment", historical["workspacePath"], admit_existing=True,
            ))
            self.assertNotIn("error", prepared, prepared)
            self.assertTrue(prepared["result"]["created"]["sourcePreserved"])
            self.assertNotEqual(historical["workspacePath"], current["workspacePath"])
            status["shader"]["rawSha256"] = "f" * 64
            crossed = json.loads(self.server.prepare_shader_workspace(
                142, "fragment", historical["workspacePath"], admit_existing=True,
            ))
            self.assertIn("exact selected Shader", crossed["error"])
        workspaces.create.assert_not_called()
        self.assertFalse(any(method == "apply_shared_shader_replacement"
                             for method, unused_params, unused_timeout in self.ipc.calls))

    def test_no_live_owner_resolves_and_prepares_existing_source_across_catalog_versions(self):
        historical, current, status, unused_owner, unused_live = self._historical_applied_workspace()
        self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()
        workspace = Path(historical["workspacePath"])
        source = workspace / "shader.hlsl"
        original_source = b"// current organized user source\n"
        source.write_bytes(original_source)
        status["capture"]["captureId"] = "f" * 64  # Historical partial export identity.
        status["source"].update(currentSha256=hashlib.sha256(original_source).hexdigest(),
                                decompiledSha256=hashlib.sha256(original_source).hexdigest())
        (workspace / "manifest.json").write_text(json.dumps(status), encoding="utf-8")
        manifest_before = (workspace / "manifest.json").read_bytes()
        reader = mock.Mock()
        reader.source_edit_status.return_value = copy.deepcopy(status)
        reader.status.return_value = copy.deepcopy(status)
        workspaces = mock.Mock()
        workspaces.status.side_effect = lambda unused_path: copy.deepcopy(status)
        with mock.patch.object(self.server, "_active_applied_shader_workspace", return_value=None), \
                mock.patch.object(self.server, "_shader_workspace_artifact_service", return_value=reader), \
                mock.patch.object(self.server, "_shader_workspace_service", return_value=workspaces), \
                mock.patch.object(self.server, "_ensure_workspace_capture_catalog_identity"):
            resolved = self._json(self.server.get_active_shader_workspace_path(142, "fragment"))
            self.assertEqual(resolved["workspacePath"], historical["workspacePath"])
            self.assertTrue(resolved["admitExistingRequired"])
            self.assertEqual(resolved["sourceDiscoveryStatus"], "latest-existing-source")
            self.assertEqual(resolved["activeCatalogSha256"], current["catalogSha256"])
            prepared = json.loads(self.server.prepare_shader_workspace(
                142, "fragment", current["workspacePath"]))
            self.assertNotIn("error", prepared, prepared)
            self.assertEqual(prepared["result"]["binding"]["workspaceId"], status["workspaceId"])
            self.assertTrue(prepared["result"]["created"]["sourcePreserved"])
        workspaces.create.assert_not_called()
        self.assertFalse(Path(current["workspacePath"]).exists())
        self.assertEqual(source.read_bytes(), original_source)
        self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)

    def test_existing_source_discovery_returns_choices_without_path_selection(self):
        historical, unused_current, status, unused_owner, unused_live = (
            self._historical_applied_workspace()
        )
        workspace = Path(historical["workspacePath"])
        source = workspace / "source" / "shader.hlsl"
        source.parent.mkdir()
        source.write_text("// latest local draft\n", encoding="utf-8")
        shader = dict(status["shader"])
        knowledge = {
            "knowledgeId": "a" * 32,
            "workspaceId": status["workspaceId"],
            "captureId": self.ipc.capture_id,
            "originResourceId": shader["resourceId"],
            "stage": shader["stage"],
            "entryPoint": shader["entryPoint"],
            "rawSha256": shader["rawSha256"],
            "workspacePath": str(workspace),
            "sourceOriginPath": str(source),
            "sourceSha256": "c" * 64,
        }
        plan = {"matches": [{"shader": shader, "workspaceCandidates": [{
            "knowledgeId": "a" * 32, "matchMode": "raw-exact",
        }]}]}
        service = mock.Mock()
        service.get_workspace_knowledge.return_value = knowledge
        sources, diagnostics = self.server._existing_project_shader_sources(
            service, self.service.status(), plan, self.ipc.capture_id, shader
        )
        self.assertEqual(diagnostics, [])
        self.assertEqual(sources[0]["workspacePath"], str(workspace))
        self.assertEqual(sources[0]["sourceSha256"],
                         hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertFalse(sources[0]["applyAuthorization"])
        service.seed_workspace.assert_not_called()
        knowledge["captureId"] = "f" * 64
        legacy_catalog, diagnostics = self.server._existing_project_shader_sources(
            service, self.service.status(), plan, self.ipc.capture_id, shader
        )
        self.assertEqual(legacy_catalog, sources)
        knowledge["originResourceId"] += 1
        rejected, diagnostics = self.server._existing_project_shader_sources(
            service, self.service.status(), plan, self.ipc.capture_id, shader)
        self.assertEqual(rejected, [])

    def test_active_resolver_finds_restore_from_other_host_process(self):
        historical, current, status, remembered, live = (
            self._historical_applied_workspace()
        )
        self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()
        project_root = Path(self.service.status()["projectRoot"])
        record_path = (project_root / ".replaylab" / "replay-project" / "derived"
                       / "restore-results" / (("e" * 64) + ".json"))
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps({
            "schema": "replaylab-project-restore-result/v1",
            "status": "finished",
            "shaders": {"items": [{
                "status": "applied",
                "resourceId": 902,
                "stage": "fragment",
                "replacementId": remembered["replacementId"],
                "workspaceId": remembered["workspaceId"],
                "workspacePath": historical["workspacePath"],
            }]},
        }), encoding="utf-8")
        ui = {
            "present": True,
            "captureBinding": {
                "captureSessionId": live["captureSessionId"],
                "capturePath": self.ipc.context["capture"]["path"],
            },
            "workspaces": [{
                "workspaceId": remembered["workspaceId"],
                "workspacePath": historical["workspacePath"],
                "captureId": self.ipc.capture_id,
                "resourceId": 902,
                "stage": "fragment",
                "entryPoint": "main",
                "visibleInWorkspaceTree": True,
                "openInTab": True,
            }],
        }
        original_call = self.ipc.call

        def bridge_call(method, params=None, timeout=None):
            if method == "get_shared_shader_replacements":
                return copy.deepcopy(live)
            if method == "get_shader_workspace_ui_state":
                return copy.deepcopy(ui)
            return original_call(method, params, timeout)

        workspace_service = mock.Mock()
        workspace_service.status.return_value = status
        with mock.patch("src.rdc_shader_inventory_cache.capture_path_sha256",
                        return_value="e" * 64), \
                mock.patch.object(self.ipc, "call", side_effect=bridge_call), \
                mock.patch.object(self.server, "_shader_workspace_service",
                                  return_value=workspace_service):
            resolved = self._json(
                self.server.get_active_shader_workspace_path(142, "fragment")
            )
            self.assertEqual(resolved["workspacePath"], historical["workspacePath"])
            self.assertEqual(resolved["workspaceId"], status["workspaceId"])
            self.assertNotEqual(resolved["workspacePath"], current["workspacePath"])
            with mock.patch.object(
                self.server, "_ensure_workspace_capture_catalog_identity"
            ):
                prepared = json.loads(self.server.prepare_shader_workspace(
                    142, "fragment", historical["workspacePath"],
                    admit_existing=True,
                ))
            self.assertNotIn("error", prepared, prepared)
            self.assertEqual(
                prepared["result"]["binding"]["workspaceId"], status["workspaceId"]
            )
            status["workspaceId"] = "f" * 32
            rejected = json.loads(
                self.server.get_active_shader_workspace_path(142, "fragment")
            )
            self.assertIn("changed original Shader identity", rejected["error"])
            self.assertNotIn("workspacePath", rejected)

    def test_active_resolver_rejects_stale_or_mismatched_replacement(self):
        historical, current, status, remembered, live = (
            self._historical_applied_workspace()
        )
        original_call = self.ipc.call

        def bridge_call(method, params=None, timeout=None):
            if method == "get_shared_shader_replacements":
                return copy.deepcopy(live)
            return original_call(method, params, timeout)

        self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()
        self.server._SHARED_SHADER_REPLACEMENT_RECORDS[
            remembered["replacementId"]
        ] = remembered
        workspace_service = mock.Mock()
        workspace_service.status.return_value = status
        try:
            with mock.patch.object(self.ipc, "call", side_effect=bridge_call), \
                    mock.patch.object(self.server, "_shader_workspace_service",
                                      return_value=workspace_service):
                live["replacements"][0]["originalRawSha256"] = "f" * 64
                mismatch = self._json(
                    self.server.get_active_shader_workspace_path(142, "fragment")
                )
                self.assertEqual(mismatch["workspacePath"], current["workspacePath"])
                self.assertIsNone(mismatch["activeReplacementId"])
                self.assertEqual(mismatch["sourceDiscoveryStatus"], "completed")
                self.assertNotIn("status", mismatch["projectReuse"])
                live["replacements"] = []
                live["count"] = 0
                rolled_back = self._json(
                    self.server.get_active_shader_workspace_path(142, "fragment")
                )
                self.assertEqual(rolled_back["workspacePath"], current["workspacePath"])
                self.assertNotEqual(rolled_back["workspacePath"], historical["workspacePath"])
                remembered["captureSessionId"] = "f" * 32
                old_session = self._json(
                    self.server.get_active_shader_workspace_path(142, "fragment")
                )
                self.assertEqual(old_session["workspacePath"], current["workspacePath"])
                remembered["captureSessionId"] = live["captureSessionId"]
                live["captureSessionId"] = "f" * 32
                malformed = json.loads(
                    self.server.get_active_shader_workspace_path(142, "fragment")
                )
                self.assertIn("invalid live replacement metadata", malformed["error"])
                self.assertNotIn("workspacePath", malformed)
        finally:
            self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()

    def test_empty_ui_id_bootstraps_one_shader_and_reuses_exact_partial_catalog(self):
        self.ipc.context["capture"]["captureId"] = ""
        first = self._json(self.server.get_active_shader_workspace_path(142, "pixel"))
        self.assertEqual(first["captureId"], self.ipc.capture_id)
        self.assertEqual(first["catalogScope"], "event-stage")
        self.assertFalse(first["fullFrame"])
        self.assertEqual(first["coveredEventIds"], [142])
        self.assertEqual(first["coveredStages"], ["fragment"])
        self.assertTrue(first["projectReuse"]["blocked"])
        self.assertFalse(Path(first["workspacePath"]).exists())
        context_calls = [args for method, args, _ in self.ipc.calls
                         if method == "get_current_renderdoc_context"]
        self.assertTrue(context_calls)
        self.assertTrue(all(args.get("identity_only") is True for args in context_calls))
        before_reuse = len(self.ipc.calls)
        again = self._json(self.server.get_active_shader_workspace_path(142, "fragment"))
        reused_context_calls = [args for method, args, unused in self.ipc.calls[before_reuse:]
                                if method == "get_current_renderdoc_context"]
        self.assertTrue(reused_context_calls)
        self.assertTrue(all(args.get("identity_only") is True for args in reused_context_calls))
        self.assertEqual(first["workspacePath"], again["workspacePath"])
        self.assertEqual(again["sourceDiscoveryStatus"], "completed")
        exports = [args for method, args, unused in self.ipc.calls if method == "export_shader_catalog"]
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0]["event_ids"], [142])
        self.assertEqual(exports[0]["stages"], ["fragment"])
        self.assertEqual([args for method, args, _ in self.ipc.calls
                          if method == "get_shader_info"],
                         [{"event_id": 142, "stage": "fragment"}])
        self.assertIsNone(self.server._active_capture_catalog_selection(999, "fragment"))
        self.assertIsNone(self.server._active_capture_catalog_selection(142, "vertex"))
        status = self._json(self.server.get_active_capture_catalog_binding_status())
        self.assertFalse(status["fullFrame"])
        self.assertEqual(status["coveredEventIds"], [142])
        catalog = json.loads(Path(status["catalogPath"]).read_text(encoding="utf-8"))
        self.assertEqual(catalog["selection"]["mode"], "explicit")

    def test_partial_bootstrap_uses_proved_original_while_replacement_is_active(self):
        self.ipc.context["capture"]["captureId"] = ""
        shader = self.ipc.context["shaders"]["bound"][0]
        original = {key: shader[key] for key in ("stage", "resourceId", "entryPoint", "rawSha256")}
        original["captureSessionId"] = self.ipc.context["capture"]["captureSessionId"]
        original["resourceId"] = str(original["resourceId"])
        shader["capturedOriginalIdentity"] = original
        shader["rawSha256"] = "d" * 64
        self.ipc.shader_query_result = copy.deepcopy(shader)
        result = self._json(self.server.get_active_shader_workspace_path(142, "fragment"))
        self.assertEqual(result["rawSha256"], original["rawSha256"])
        self.assertFalse(result["fullFrame"])
        exports = [args for method, args, _ in self.ipc.calls if method == "export_shader_catalog"]
        self.assertEqual([args["event_ids"] for args in exports], [[142]])

    def test_warm_shader_path_uses_two_capture_checks_without_repeated_project_status(self):
        first = self._json(self.server.get_active_shader_workspace_path(142, "fragment"))
        before = len(self.ipc.calls)
        with mock.patch.object(self.service, "status", wraps=self.service.status) as status:
            again = self._json(self.server.get_active_shader_workspace_path(142, "fragment"))
        self.assertEqual(first["workspacePath"], again["workspacePath"])
        calls = self.ipc.calls[before:]
        self.assertEqual([item[0] for item in calls], [
            "get_current_renderdoc_context", "get_shared_shader_replacements",
            "get_current_renderdoc_context"])
        self.assertTrue(all(item[1].get("identity_only") is True for item in calls
                            if item[0] == "get_current_renderdoc_context"))
        self.assertTrue(calls[1][1]["include_all_capture_catalogs"])
        # ensure_pending and the existing future-plan operation each retain
        # their own status check; catalog helper nesting adds none.
        self.assertEqual(status.call_count, 2)

    def test_warm_shader_path_rejects_changes_during_reuse_plan(self):
        self._json(self.server.get_active_shader_workspace_path(142, "fragment"))
        session = self.ipc.context["capture"]["captureSessionId"]
        record = copy.deepcopy(self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS[session])
        path = Path(record["catalogPath"])
        original_bytes, original_stat = path.read_bytes(), path.stat()
        context = copy.deepcopy(self.ipc.context)
        original_plan = self.service.future_frame_plan
        project_root, manifest = self.service._active()
        config_path = project_root / ".replaylab" / "workspace.json"
        original_config = config_path.read_bytes()

        def changed_catalog_bytes():
            # Defeat a path/size/mtime-only cache without changing file length.
            changed = original_bytes.replace(b'"stage"', b'"Stage"', 1)
            self.assertNotEqual(changed, original_bytes)
            self.assertEqual(len(changed), len(original_bytes))
            path.write_bytes(changed)
            os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

        def changed_project_configuration():
            self.service._write_workspace(project_root, {**manifest, "name": "Changed project name"})

        mutations = {
            "catalog-content": changed_catalog_bytes,
            "catalog-binding": lambda: self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS[session].update(captureId="d" * 64),
            "capture-session": lambda: self.ipc.context["capture"].update(captureSessionId="c" * 32),
            "capture-path": lambda: self.ipc.context["capture"].update(path="D:/captures/other.rdc"),
            "capture-api": lambda: self.ipc.context["capture"].update(api="Vulkan"),
            "project-configuration": changed_project_configuration,
        }
        for name, mutate in mutations.items():
            with self.subTest(change=name):
                def plan_then_change(*args, **kwargs):
                    result = original_plan(*args, **kwargs)
                    mutate()
                    return result
                try:
                    with mock.patch.object(self.service, "future_frame_plan", side_effect=plan_then_change):
                        result = json.loads(self.server.get_active_shader_workspace_path(142, "fragment"))
                    self.assertIn("error", result)
                    self.assertNotIn("workspacePath", result)
                finally:
                    path.write_bytes(original_bytes)
                    config_path.write_bytes(original_config)
                    self.ipc.context = copy.deepcopy(context)
                    self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS[session] = copy.deepcopy(record)

    def test_partial_bootstrap_rejects_stale_captured_original_identity(self):
        self.ipc.context["capture"]["captureId"] = ""
        shader = self.ipc.context["shaders"]["bound"][0]
        shader["capturedOriginalIdentity"] = {
            **shader, "captureSessionId": "f" * 32}
        self.ipc.shader_query_result = copy.deepcopy(shader)
        result = json.loads(self.server.get_active_shader_workspace_path(142, "fragment"))
        self.assertIn("different live binding", result["error"])
        self.assertNotIn("export_shader_catalog", [item[0] for item in self.ipc.calls])

    def test_partial_bootstrap_imports_only_its_scope_and_future_plan_is_partial(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        self._json(self.server.configure_active_replay_project("example-game", "build-1", "windows-x64", "d3d11"))
        self.ipc.context["capture"]["captureId"] = ""
        result = self._json(self.server.get_active_shader_workspace_path(142, "fragment"))
        self.assertFalse(result["projectReuse"]["blocked"])
        self.assertEqual(self.service.replay_service().status()["captureCount"], 1)
        future = self._json(self.server.get_active_project_future_frame_plan())
        self.assertFalse(future["fullFrame"])
        self.assertEqual(future["plan"]["coveredEventIds"], [142])
        self.assertFalse(future["plan"]["fullFrame"])
        self.assertFalse(future["applied"])
        self.ipc.use["eventId"] = 143
        self.ipc.context["events"] = {"currentEventId": 143, "selectedEventId": 143}
        changed = self._json(self.server.get_active_shader_workspace_path(143, "fragment"))
        self.assertEqual(changed["coveredEventIds"], [143])
        self.assertNotEqual(changed["catalogSha256"], result["catalogSha256"])
        self.assertIsNone(self.server._active_capture_catalog_selection(142, "fragment"))
        full = self._json(self.server.prepare_active_capture_project())
        self.assertTrue(full["captureExport"]["fullFrame"])
        self.assertTrue(self._json(self.server.get_active_capture_catalog_binding_status())["fullFrame"])

    def test_bootstrap_rejects_session_change_while_querying_explicit_shader(self):
        self.ipc.context["capture"]["captureId"] = ""
        original = self.ipc.call

        def switch_before_shader_query(method, params=None, timeout=None):
            if method == "get_shader_info":
                self.ipc.context["capture"]["captureSessionId"] = "c" * 32
            return original(method, params, timeout)

        with mock.patch.object(self.ipc, "call", side_effect=switch_before_shader_query):
            result = json.loads(self.server.get_active_shader_workspace_path(142, "fragment"))
        self.assertIn("capture/session changed", result["error"])
        self.assertNotIn("export_shader_catalog", [item[0] for item in self.ipc.calls])
        self.assertFalse(self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS)

    def test_frozen_event_bootstraps_from_present_without_ui_selection(self):
        self.ipc.context["capture"]["captureId"] = ""
        self.ipc.context["events"] = {"currentEventId": 66327, "selectedEventId": 66327}
        self.ipc.context["shaders"]["bound"] = []
        before = copy.deepcopy(self.ipc.context)
        result = self._json(
            self.server.get_active_shader_workspace_path(142, "fragment")
        )
        self.assertEqual(result["eventId"], 142)
        self.assertEqual(result["coveredEventIds"], [142])
        self.assertEqual(self.ipc.context, before)
        identity_queries = [
            item[1]
            for item in self.ipc.calls
            if item[0] == "get_shader_info"
        ]
        self.assertEqual(
            identity_queries,
            [{"event_id": 142, "stage": "fragment"}],
        )
        exports = [item[1] for item in self.ipc.calls if item[0] == "export_shader_catalog"]
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0]["event_ids"], [142])
        self.assertEqual(exports[0]["stages"], ["fragment"])

    def test_explicit_shader_bootstrap_survives_ui_navigation_during_export(self):
        self.ipc.context["capture"]["captureId"] = ""
        original_call = self.ipc.call

        def navigate_after_export(method, params=None, timeout=None):
            result = original_call(method, params, timeout)
            if method == "export_shader_catalog":
                self.ipc.context["events"] = {
                    "currentEventId": 900, "selectedEventId": 901,
                }
                self.ipc.context["shaders"]["bound"] = [{
                    "stage": "fragment", "resourceId": 999,
                    "entryPoint": "otherMain", "rawSha256": "d" * 64,
                }]
            return result

        with mock.patch.object(self.ipc, "call", side_effect=navigate_after_export):
            result = self._json(self.server.get_active_shader_workspace_path(142, "pixel"))

        self.assertEqual(result["eventId"], 142)
        self.assertEqual(result["rawSha256"], self.ipc.shader["rawSha256"])
        self.assertEqual(result["coveredEventIds"], [142])
        self.assertEqual(self.ipc.context["events"], {
            "currentEventId": 900, "selectedEventId": 901,
        })
        exports = [args for method, args, _ in self.ipc.calls if method == "export_shader_catalog"]
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0]["event_ids"], [142])
        self.assertEqual(exports[0]["stages"], ["fragment"])
        self.assertNotIn("set_event", [method for method, _, _ in self.ipc.calls])

    def test_partial_frozen_inventory_anchor_rejects_actual_physical_mismatch(self):
        self.ipc.context["capture"]["captureId"] = ""
        self.ipc.context["events"] = {"currentEventId": 66327, "selectedEventId": 66327}
        self.ipc.context["shaders"]["bound"] = []
        expected = dict(expected_resource_id=902, expected_entry_point="main",
                        expected_raw_sha256=self.ipc.shader["rawSha256"],
                        expected_capture_session_id="a" * 32)
        for field, wrong in (("expected_resource_id", 903), ("expected_raw_sha256", "d" * 64),
                             ("expected_entry_point", ""), ("expected_capture_session_id", "c" * 32)):
            with self.subTest(field=field):
                error = json.loads(self.server.get_active_shader_workspace_path(
                    142, "fragment", **{field: wrong}))
                self.assertIn("error", error)
                self.assertFalse(self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS)
                self.assertEqual(list(Path(self.service.status()["paths"]["captureCatalogs"]).iterdir()), [])
        self.assertEqual(self.ipc.context["events"]["currentEventId"], 66327)

    def test_each_partial_frozen_anchor_resolves_and_returns_complete_original_binding(self):
        self.ipc.context["capture"]["captureId"] = ""
        self.ipc.context["events"] = {"currentEventId": 66327, "selectedEventId": 66327}
        self.ipc.context["shaders"]["bound"] = []
        expected = dict(expected_resource_id="902", expected_entry_point="main",
                        expected_raw_sha256=self.ipc.shader["rawSha256"],
                        expected_capture_session_id="a" * 32)
        fields = list(expected)
        for bits in range(1, 16):
            args = {field: expected[field] for index, field in enumerate(fields) if bits & (1 << index)}
            with self.subTest(fields=list(args)):
                result = self._json(self.server.get_active_shader_workspace_path(142, "fragment", **args))
                self.assertEqual(result["shaderResourceId"], 902)
                self.assertEqual(result["entryPoint"], "main")
                self.assertEqual(result["rawSha256"], self.ipc.shader["rawSha256"])
                self.assertEqual(result["captureSessionId"], "a" * 32)
        self.assertEqual(self.ipc.context["events"]["currentEventId"], 66327)
        self.assertEqual(sum(method == "get_shader_info" for method, _, _ in self.ipc.calls), 1)
        self.assertEqual(sum(method == "export_shader_catalog" for method, _, _ in self.ipc.calls), 1)

    def test_partial_anchor_checks_captured_original_instead_of_replacement_raw(self):
        self.ipc.context["capture"]["captureId"] = ""
        self.ipc.context["events"] = {"currentEventId": 66327, "selectedEventId": 66327}
        self.ipc.context["shaders"]["bound"] = []
        original = self.ipc.call

        def replacement_shader(method, params=None, timeout=None):
            result = original(method, params, timeout)
            if method == "get_shader_info":
                result = {**result, "rawSha256": "d" * 64, "capturedOriginalIdentity": {
                    "captureSessionId": "a" * 32, "resourceId": 902,
                    "stage": "fragment", "entryPoint": "main",
                    "rawSha256": self.ipc.shader["rawSha256"],
                }}
            return result

        with mock.patch.object(self.ipc, "call", side_effect=replacement_shader):
            result = self._json(self.server.get_active_shader_workspace_path(
                142, "fragment", expected_raw_sha256=self.ipc.shader["rawSha256"]))
        self.assertEqual(result["rawSha256"], self.ipc.shader["rawSha256"])
        self.assertEqual(sum(method == "get_shader_info" for method, _, _ in self.ipc.calls), 1)

    def test_partial_bootstrap_rejects_capture_changes_before_import(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        before = copy.deepcopy(self.ipc.context)
        cases = (
            ("path", lambda: self.ipc.context["capture"].update(path="D:/captures/other.rdc")),
            ("session", lambda: self.ipc.context["capture"].update(captureSessionId="c" * 32)),
            ("api", lambda: self.ipc.context["capture"].update(api="Vulkan")),
        )
        original = self.ipc.call
        for name, mutate in cases:
            with self.subTest(name=name):
                self.ipc.context = copy.deepcopy(before)
                self.ipc.context["capture"]["captureId"] = ""
                def changed(method, params=None, timeout=None):
                    value = original(method, params, timeout)
                    if method == "export_shader_catalog":
                        mutate()
                    return value
                with mock.patch.object(self.ipc, "call", side_effect=changed):
                    result = json.loads(self.server.get_active_shader_workspace_path(142, "fragment"))
                self.assertIn("error", result)
                self.assertFalse(self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS)
                self.assertEqual(list(Path(self.service.status()["paths"]["captureCatalogs"]).iterdir()), [])

    def test_partial_bootstrap_rejects_project_switch_before_import(self):
        first_project = self.service.ensure_pending("First project")
        second_project = self.service.create_pending(self.root / "projects" / "second-project")
        self.service.activate(first_project["projectRoot"])
        original_call = self.ipc.call

        def switch_after_export(method, params=None, timeout=None):
            result = original_call(method, params, timeout)
            if method == "export_shader_catalog":
                self.service.activate(second_project["projectRoot"])
            return result

        with mock.patch.object(self.ipc, "call", side_effect=switch_after_export):
            result = json.loads(self.server.get_active_shader_workspace_path(142, "fragment"))

        self.assertIn("active project", result["error"])
        self.assertIn("changed", result["error"])
        self.assertNotIn("workspacePath", result)
        self.assertFalse(self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS)
        for project in (first_project, second_project):
            self.assertEqual(list(Path(project["paths"]["captureCatalogs"]).iterdir()), [])

    def test_partial_bootstrap_rejects_changed_explicit_target_identity(self):
        self.service.ensure_pending("Example Game")
        original_call = self.ipc.call

        for field in ("resourceId", "entryPoint", "rawSha256"):
            with self.subTest(changed=field):
                def change_exported_target(method, params=None, timeout=None):
                    result = original_call(method, params, timeout)
                    if method != "export_shader_catalog":
                        return result
                    path = Path(result["catalogPath"])
                    catalog = json.loads(path.read_text(encoding="utf-8"))
                    shader, use = catalog["shaders"][0], catalog["uses"][0]
                    if field == "resourceId":
                        shader["resourceId"] = shader["shaderResourceId"] = use["resourceId"] = 903
                    elif field == "entryPoint":
                        shader["entryPoint"] = use["entryPoint"] = "differentMain"
                    else:
                        blob = _dxbc(b"a different original Shader")
                        fingerprint = fingerprint_bytes(blob)
                        self.assertIsNotNone(fingerprint)
                        (path.parent / shader["rawPath"]).write_bytes(blob)
                        shader.update(rawSha256=fingerprint.raw_sha256,
                                      codeSha256=fingerprint.code_sha256, rawSize=len(blob))
                        use["rawSha256"] = fingerprint.raw_sha256
                    shader["shaderKey"] = use["shaderKey"] = ":".join(str(shader[key]) for key in (
                        "resourceId", "stage", "entryPoint", "rawSha256"))
                    catalog["captureId"] = capture_identity(catalog, catalog["shaders"])
                    result["captureId"] = catalog["captureId"]
                    path.write_text(json.dumps(catalog), encoding="utf-8")
                    return result

                with mock.patch.object(self.ipc, "call", side_effect=change_exported_target):
                    result = json.loads(self.server.get_active_shader_workspace_path(142, "fragment"))

                self.assertIn("Shader catalog differs from the requested Shader identity", result["error"])
                self.assertNotIn("workspacePath", result)
                self.assertFalse(self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS)
                self.assertEqual(list(Path(self.service.status()["paths"]["captureCatalogs"]).iterdir()), [])

    def test_partial_plan_keeps_its_exact_revision_when_another_revision_is_imported(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        self.ipc.context["capture"]["captureId"] = ""
        staged = self._json(self.server.get_active_shader_workspace_path(142, "fragment"))
        self._json(self.server.configure_active_replay_project("example-game", "build-1", "windows-x64", "d3d11"))
        imported = self._json(self.server.get_active_shader_workspace_path(142, "fragment"))
        self.assertEqual(imported["catalogSha256"], staged["catalogSha256"])
        export_count = len([item for item in self.ipc.calls if item[0] == "export_shader_catalog"])
        self.assertEqual(export_count, 1)
        full = self.ipc.call("export_shader_catalog", {"output_dir": str(self.root / "other-revision"), "event_ids": []})
        changed = self.service.import_capture(full["catalogPath"])
        self.assertNotEqual(changed["stagedCatalog"]["catalogSha256"], staged["catalogSha256"])
        plan = self._json(self.server.get_active_project_future_frame_plan())
        self.assertEqual(plan["plan"]["reusePlan"]["catalogSha256"], staged["catalogSha256"])
        self.assertFalse(plan["fullFrame"])
        self.assertEqual(plan["coveredEventIds"], [142])

    def test_partial_catalog_without_symbol_database_keeps_direct_asset_reads_available(self):
        self._bound_with_assets()
        self._json(self.server.get_active_shader_workspace_path(142, "fragment"))
        result = json.loads(self.server.query_active_shader_asset_evidence(142, "fragment"))
        self.assertFalse(result["ready"])
        self.assertEqual(result["reasonCode"], "managed-symbol-database-unavailable")
        self.assertFalse(result["assetEvidenceBlocked"])
        self.assertFalse(result["policy"]["fullAssetScanRequired"])
        self.assertIn("describe_unpacked_asset", result["availableCapabilities"])
        self.assertNotIn("recommendedActions", result)
        self.assertNotIn("prepare_active_capture_project", json.dumps(result))

    def test_current_shader_reuse_projection_omits_unrelated_frame_matches(self):
        shader = {key: self.ipc.shader[key] for key in ("resourceId", "stage", "entryPoint", "rawSha256")}
        item = {"shader": {**shader, "eventIds": list(range(1, 1000))},
                "workspaceCandidates": [{"knowledgeId": "c" * 32, "sourceSeedEligible": True}]}
        other = copy.deepcopy(item)
        other["shader"]["resourceId"] += 1
        source = {"reusePlan": {"matches": [item, other], "matchedShaderCount": 2,
                                "preparationSteps": [item, other]},
                  "preparationSteps": [item, other]}
        result = self.server._current_shader_reuse_projection(source, shader, 142)
        self.assertEqual(result["reusePlan"]["matchedShaderCount"], 1)
        self.assertEqual(len(result["preparationSteps"]), 1)
        projected = result["reusePlan"]["matches"][0]["shader"]
        self.assertEqual(projected["eventIds"], [142])
        self.assertEqual(projected["catalogUseCount"], 999)
        self.assertEqual(source["reusePlan"]["matchedShaderCount"], 2)

    def test_partial_bootstrap_rejects_extra_scope_and_wrong_exported_identity(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        original = self.ipc.call
        def wrong(method, params=None, timeout=None):
            value = original(method, params, timeout)
            if method == "export_shader_catalog":
                path = Path(value["catalogPath"])
                catalog = json.loads(path.read_text(encoding="utf-8"))
                catalog["selection"]["eventIds"].append(999)
                path.write_text(json.dumps(catalog), encoding="utf-8")
            return value
        with mock.patch.object(self.ipc, "call", side_effect=wrong):
            result = json.loads(self.server.get_active_shader_workspace_path(142, "fragment"))
        self.assertIn("exactly the requested Shader", result["error"])
        self.assertFalse(self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS)
        self.assertEqual(list(Path(self.service.status()["paths"]["captureCatalogs"]).iterdir()), [])

    def test_partial_bootstrap_rejects_wrong_receipt_bytes_and_restore_failure(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        original = self.ipc.call
        for kind in ("capture", "session", "bytes", "restore"):
            with self.subTest(kind=kind):
                def corrupt(method, params=None, timeout=None):
                    value = original(method, params, timeout)
                    if method == "export_shader_catalog":
                        path = Path(value["catalogPath"])
                        if kind == "capture":
                            value["captureId"] = "0" * 64
                        elif kind == "session":
                            value["captureSessionId"] = "c" * 32
                        elif kind == "bytes":
                            (path.parent / self.ipc.shader["rawPath"]).write_bytes(_dxbc(b"wrong bytes"))
                        else:
                            catalog = json.loads(path.read_text(encoding="utf-8"))
                            catalog["errors"] = [{"scope": "restore_event", "message": "restore failed"}]
                            path.write_text(json.dumps(catalog), encoding="utf-8")
                    return value
                with mock.patch.object(self.ipc, "call", side_effect=corrupt):
                    result = json.loads(self.server.get_active_shader_workspace_path(142, "fragment"))
                self.assertIn("error", result)
                self.assertFalse(self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS)
                self.assertEqual(list(Path(self.service.status()["paths"]["captureCatalogs"]).iterdir()), [])

    def test_bound_symbol_resolution_keeps_canonical_id_when_live_id_stays_empty(self):
        self._bound_with_assets()
        self.ipc.context["capture"]["captureId"] = ""

        with mock.patch.object(self.server, "SymbolService", _FakeSymbolService):
            prepared = self._json(self.server.prepare_active_capture_project())

        self.assertEqual(prepared["capture"]["captureId"], self.ipc.capture_id)
        self.assertTrue(prepared["symbolResolution"]["attempted"])
        self.assertEqual(
            prepared["symbolResolution"]["match"]["captureId"],
            self.ipc.capture_id,
        )

    def test_nonempty_filtered_catalog_id_may_differ_from_full_export(self):
        self.ipc.context["capture"]["captureId"] = "0" * 64
        self._json(self.server.ensure_active_replay_project("Example Game"))

        result = self._json(self.server.prepare_active_capture_project())

        self.assertEqual(result["capture"]["captureId"], self.ipc.capture_id)
        self.assertEqual(result["stagedCatalog"]["captureId"], self.ipc.capture_id)
        self.assertEqual(
            self.ipc.context["capture"]["captureId"],
            "0" * 64,
        )

    def test_same_session_shader_selection_reuses_staged_full_catalog(self):
        self.ipc.context["capture"]["captureId"] = "0" * 64
        self._json(self.server.ensure_active_replay_project("Example Game"))
        prepared = self._json(self.server.prepare_active_capture_project())
        exports_before = len(
            [call for call in self.ipc.calls if call[0] == "export_shader_catalog"]
        )

        reused = self.server._active_capture_catalog_selection(142, "fragment")

        self.assertIsNotNone(reused)
        exported, catalog_path, selected, binding_sha256 = reused
        self.assertTrue(exported["reusedActiveProjectCatalog"])
        self.assertFalse(exported["exportPerformed"])
        self.assertEqual(exported["captureId"], self.ipc.capture_id)
        self.assertEqual(Path(catalog_path), Path(prepared["stagedCatalog"]["catalogPath"]))
        self.assertEqual(selected.key.catalog_key, self.ipc.shader["shaderKey"])
        self.assertRegex(binding_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(
            len([call for call in self.ipc.calls if call[0] == "export_shader_catalog"]),
            exports_before,
        )

    def test_public_prepare_rebinds_same_capture_id_to_new_catalog_revision(self):
        self.ipc.context["capture"]["captureId"] = "0" * 64
        self._json(self.server.ensure_active_replay_project("Example Game"))
        unbound = self._json(
            self.server.get_active_capture_catalog_binding_status()
        )
        self.assertFalse(unbound["bound"])

        first = self._json(self.server.prepare_active_capture_project())
        self.ipc.export_revision = 2
        second = self._json(self.server.prepare_active_capture_project())

        self.assertEqual(first["capture"]["captureId"], self.ipc.capture_id)
        self.assertEqual(second["capture"]["captureId"], self.ipc.capture_id)
        self.assertNotEqual(
            first["stagedCatalog"]["catalogSha256"],
            second["stagedCatalog"]["catalogSha256"],
        )
        self.assertNotEqual(
            first["stagedCatalog"]["catalogPath"],
            second["stagedCatalog"]["catalogPath"],
        )
        self.assertTrue(Path(first["stagedCatalog"]["catalogPath"]).is_file())
        self.assertTrue(Path(second["stagedCatalog"]["catalogPath"]).is_file())

        status = self._json(
            self.server.get_active_capture_catalog_binding_status()
        )
        self.assertTrue(status["bound"])
        self.assertEqual(
            status["catalogSha256"],
            second["stagedCatalog"]["catalogSha256"],
        )
        self.assertEqual(
            status["catalogPath"], second["stagedCatalog"]["catalogPath"]
        )
        reused = self.server._active_capture_catalog_selection(142, "fragment")
        self.assertIsNotNone(reused)
        self.assertEqual(Path(reused[1]), Path(status["catalogPath"]))

    def test_bound_prepare_imports_two_revisions_without_capture_id_conflict(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        configured = self._json(
            self.server.configure_active_replay_project(
                "example-game",
                "build-1",
                "windows-x64",
                "d3d11",
            )
        )
        self.assertTrue(configured["project"]["scopeConfigured"])

        first = self._json(self.server.prepare_active_capture_project())
        self.ipc.export_revision = 2
        second = self._json(self.server.prepare_active_capture_project())

        self.assertEqual(first["capture"]["captureId"], self.ipc.capture_id)
        self.assertEqual(second["capture"]["captureId"], self.ipc.capture_id)
        self.assertNotEqual(
            first["stagedCatalog"]["catalogSha256"],
            second["stagedCatalog"]["catalogSha256"],
        )
        self.assertTrue(second["reusePlan"]["catalogRevisionAdded"])
        self.assertTrue(second["reusePlan"]["activeRevisionChanged"])
        replay = self.service.replay_service().status()
        self.assertEqual(replay["captureCount"], 1)
        self.assertEqual(replay["captureRevisionCount"], 2)
        self.assertEqual(
            second["reusePlan"]["catalogSha256"],
            second["stagedCatalog"]["catalogSha256"],
        )

    def test_managed_workspace_path_is_keyed_by_exact_catalog_revision(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        first = self._json(self.server.prepare_active_capture_project())
        path_a = self._json(
            self.server.get_active_shader_workspace_path(142, "fragment")
        )

        self.ipc.export_revision = 2
        second = self._json(self.server.prepare_active_capture_project())
        path_b = self._json(
            self.server.get_active_shader_workspace_path(142, "fragment")
        )

        self.assertEqual(path_a["captureId"], path_b["captureId"])
        self.assertEqual(path_a["rawSha256"], path_b["rawSha256"])
        self.assertEqual(
            path_a["catalogSha256"], first["stagedCatalog"]["catalogSha256"]
        )
        self.assertEqual(
            path_b["catalogSha256"], second["stagedCatalog"]["catalogSha256"]
        )
        self.assertNotEqual(path_a["catalogSha256"], path_b["catalogSha256"])
        self.assertNotEqual(path_a["workspacePath"], path_b["workspacePath"])
        self.assertRegex(path_a["routeBindingSha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            path_a["routeBindingSha256"], path_b["routeBindingSha256"]
        )
        self.assertEqual(path_a["workspaceLayout"], "content-addressed-v2")
        self.assertEqual(path_b["workspaceLayout"], "content-addressed-v2")
        self.assertRegex(path_a["workspaceBindingSha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(path_b["workspaceBindingSha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            Path(path_a["workspacePath"]).name,
            path_a["workspaceBindingSha256"],
        )
        self.assertEqual(
            Path(path_b["workspacePath"]).name,
            path_b["workspaceBindingSha256"],
        )

    def test_managed_workspace_path_keeps_frozen_event_after_ui_navigation(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        self._json(self.server.prepare_active_capture_project())
        self.ipc.context["events"] = {
            "currentEventId": 999,
            "selectedEventId": 999,
        }
        self.ipc.context["shaders"]["bound"] = []

        resolved = self._json(
            self.server.get_active_shader_workspace_path(142, "fragment")
        )

        self.assertEqual(142, resolved["eventId"])
        self.assertEqual(902, resolved["shaderResourceId"])
        self.assertEqual(self.ipc.shader["rawSha256"], resolved["rawSha256"])
        self.assertFalse(
            any(method == "get_shader_info" for method, _params, _timeout in self.ipc.calls)
        )

    def test_tampered_full_catalog_binding_fails_without_subset_fallback(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        prepared = self._json(self.server.prepare_active_capture_project())
        catalog = Path(prepared["stagedCatalog"]["catalogPath"])
        catalog.write_bytes(catalog.read_bytes() + b"\n")
        exports_before = len(
            [call for call in self.ipc.calls if call[0] == "export_shader_catalog"]
        )

        with self.assertRaisesRegex(RuntimeError, "path/hash validation"):
            self.server._active_capture_catalog_selection(142, "fragment")

        self.assertEqual(
            len([call for call in self.ipc.calls if call[0] == "export_shader_catalog"]),
            exports_before,
        )

    def test_oversized_full_catalog_binding_fails_before_hashing(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        prepared = self._json(self.server.prepare_active_capture_project())
        catalog = Path(prepared["stagedCatalog"]["catalogPath"])
        with catalog.open("r+b") as stream:
            stream.truncate(self.server.MAX_ACTIVE_CAPTURE_CATALOG_BYTES + 1)

        with mock.patch.object(
            self.server,
            "_file_sha256",
            side_effect=AssertionError("unbounded hash helper must not be used"),
        ):
            with self.assertRaisesRegex(RuntimeError, "size is invalid"):
                self.server._active_capture_catalog_selection(142, "fragment")

    def test_prepare_rejects_live_path_and_api_mismatch_before_staging(self):
        cases = (
            ("path", "D:/captures/other.rdc", "catalog path differs"),
            ("api", "Vulkan", "catalog API differs"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                self.ipc.context["capture"]["captureId"] = ""
                self.ipc.context["capture"][field] = value
                status = self._json(
                    self.server.ensure_active_replay_project("Example Game")
                )["project"]

                result = json.loads(self.server.prepare_active_capture_project())

                self.assertIn(message, result["error"])
                self.assertFalse(
                    (
                        Path(status["paths"]["captureCatalogs"])
                        / self.ipc.capture_id
                    ).exists()
                )
                self.ipc.context["capture"][field] = (
                    "D:/captures/frame.rdc" if field == "path" else "D3D11"
                )

    def test_prepare_rejects_capture_session_change_before_staging(self):
        self.ipc.context["capture"]["captureId"] = ""
        status = self._json(
            self.server.ensure_active_replay_project("Example Game")
        )["project"]
        original_call = self.ipc.call

        def change_session_after_export(method, params=None, timeout=None):
            result = original_call(method, params, timeout)
            if method == "export_shader_catalog":
                self.ipc.context["capture"]["captureSessionId"] = "b" * 32
            return result

        with mock.patch.object(
            self.ipc, "call", side_effect=change_session_after_export
        ):
            result = json.loads(self.server.prepare_active_capture_project())

        self.assertIn("capture/session changed", result["error"])
        self.assertFalse(
            (Path(status["paths"]["captureCatalogs"]) / self.ipc.capture_id).exists()
        )

    def test_bound_project_scans_once_per_session_and_registers_verified_aliases(self):
        self._bound_with_assets()
        original_discover = self.server.discover_recovery_assets
        original_report = self.server._active_asset_recovery_report
        guard_available: list[bool] = []

        def assert_guard_available(function, *args, **kwargs):
            acquired: list[bool] = []

            def probe():
                value = self.server._ACTIVE_SYMBOL_SCAN_GUARD.acquire(timeout=1.0)
                acquired.append(value)
                if value:
                    self.server._ACTIVE_SYMBOL_SCAN_GUARD.release()

            worker = threading.Thread(target=probe)
            worker.start()
            worker.join(timeout=2.0)
            guard_available.append(acquired == [True])
            return function(*args, **kwargs)

        with mock.patch.object(
            self.server, "SymbolService", _FakeSymbolService
        ), mock.patch.object(
            self.server,
            "discover_recovery_assets",
            side_effect=lambda *args, **kwargs: assert_guard_available(
                original_discover, *args, **kwargs
            ),
        ), mock.patch.object(
            self.server,
            "_active_asset_recovery_report",
            side_effect=lambda *args, **kwargs: assert_guard_available(
                original_report, *args, **kwargs
            ),
        ):
            first = self._json(self.server.prepare_active_capture_project())
            self.ipc.export_revision = 2
            second = self._json(self.server.prepare_active_capture_project())
        self.assertTrue(guard_available)
        self.assertTrue(all(guard_available))
        self.assertEqual(_FakeSymbolService.scan_calls, 1)
        self.assertTrue(first["symbolResolution"]["attempted"])
        self.assertFalse(first["symbolResolution"]["scanCacheHit"])
        self.assertTrue(second["symbolResolution"]["scanCacheHit"])
        self.assertTrue(first["symbolResolution"]["aliasTable"]["verifiedOnly"])
        self.assertEqual(
            first["symbolResolution"]["aliasTable"]["catalogSha256"],
            first["stagedCatalog"]["catalogSha256"],
        )
        self.assertEqual(
            second["symbolResolution"]["aliasTable"]["catalogSha256"],
            second["stagedCatalog"]["catalogSha256"],
        )
        self.assertNotEqual(
            first["symbolResolution"]["aliasTable"]["path"],
            second["symbolResolution"]["aliasTable"]["path"],
        )
        self.assertIs(first["symbolResolution"]["projectRegistration"]["applied"], False)
        self.assertTrue(
            first["symbolResolution"]["renderDocAliasOverlay"]["confirmedOnly"]
        )
        self.assertFalse(first["symbolResolution"]["policy"]["aiProvisionalAutoApply"])
        recovery = first["symbolResolution"]["assetShaderRecovery"]
        self.assertEqual(recovery["schema"], "replaylab-active-asset-shader-recovery/v1")
        self.assertEqual(recovery["boundShaderInputCount"], 1)
        self.assertEqual(len(recovery["boundShaders"]), 1)
        full_context_calls = [args for method, args, unused in self.ipc.calls
                              if method == "get_current_renderdoc_context" and not args.get("identity_only")]
        self.assertEqual(len(full_context_calls), 2)  # One current-event report per prepare, including cache hit.
        self.assertEqual(recovery["inventory"]["candidateCount"], 1)
        self.assertEqual(
            recovery["boundShaders"][0]["topCandidates"][0]["relativePath"],
            "main.hlsl",
        )
        self.assertGreaterEqual(
            recovery["boundShaders"][0]["topCandidates"][0]["assessment"][
                "recoveryLevel"
            ],
            2,
        )
        self.assertFalse(recovery["policy"]["workspaceSeedAuthorized"])
        self.assertFalse(recovery["policy"]["applyAuthorized"])
        self.assertEqual(recovery["sourcePreviews"][0]["candidateId"], recovery[
            "boundShaders"
        ][0]["topCandidates"][0]["candidateId"])
        preview = recovery["sourcePreviews"][0]
        self.assertEqual(
            hashlib.sha256(preview["sourcePreview"].encode("utf-8")).hexdigest(),
            preview["sourcePreviewSha256"],
        )
        self.assertTrue(recovery["sourcePreviewPolicy"]["fullFileHashRevalidated"])
        self.assertIn("activeProjectPolicy", first["reusePlan"])
        future = self._json(self.server.get_active_project_future_frame_plan())
        self.assertEqual(future["captureId"], self.ipc.capture_id)
        self.assertFalse(future["plan"]["blocked"])
        self.assertFalse(future["plan"]["policy"]["aiProvisionalAutoApply"])

    def test_workspace_path_uses_live_capture_stage_resource_and_hash(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        result = self._json(
            self.server.get_active_shader_workspace_path(142, "pixel")
        )
        self.assertEqual(result["captureId"], self.ipc.capture_id)
        self.assertEqual(result["captureSessionId"], "a" * 32)
        self.assertEqual(result["stage"], "fragment")
        self.assertEqual(result["shaderResourceId"], 902)
        workspace = Path(result["workspacePath"])
        self.assertFalse(workspace.exists())
        self.assertEqual(result["workspaceLayout"], "content-addressed-v2")
        self.assertEqual(workspace.parent.name, "v2")
        self.assertEqual(workspace.name, result["workspaceBindingSha256"])
        repeated = self._json(
            self.server.get_active_shader_workspace_path(142, "fragment")
        )
        self.assertEqual(repeated["workspacePath"], result["workspacePath"])

        error = json.loads(self.server.get_active_shader_workspace_path(999, "fragment"))
        self.assertEqual(error["schema"], "replaylab-active-project-mcp-error/v1")
        self.assertIn("frozen event Shader identity", error["error"])
        self.assertFalse(error["applied"])

    def test_decimal_context_resource_id_returns_integer_at_mcp_boundaries(self):
        resource_id = 1000000000000000627
        self.ipc.context["shaders"]["bound"][0]["resourceId"] = str(resource_id)

        active = self.server._active_bound_shader(
            self.ipc.context, 142, "fragment"
        )
        recovery = self.server._active_asset_recovery_report(
            {"candidates": []}, self.ipc.context, str(self.root)
        )

        self.assertIs(type(active["resourceId"]), int)
        self.assertEqual(active["resourceId"], resource_id)
        self.assertIs(type(recovery["boundShaders"][0]["shaderResourceId"]), int)
        self.assertEqual(
            recovery["boundShaders"][0]["shaderResourceId"], resource_id
        )

    def test_explicit_asset_recovery_inspection_is_read_only(self):
        assets = self.root / "shader-assets"
        assets.mkdir()
        (assets / "surface.frag").write_text(
            "#version 450\nlayout(location=0) out vec4 color;\n"
            "void main() { color = vec4(1.0); }\n",
            encoding="utf-8",
        )
        result = json.loads(
            self.server.inspect_asset_shader_recovery(
                str(assets),
                json.dumps({"stage": "fragment", "entryPoint": "main"}),
                max_files=10,
                max_file_size_mb=1,
                max_total_read_mb=2,
                max_candidates=10,
            )
        )
        self.assertNotIn("error", result, result)
        self.assertEqual(result["schema"], "renderdoc-ultra-recovery-assets/v1")
        self.assertEqual(result["candidateCount"], 1)
        self.assertEqual(result["candidates"][0]["relativePath"], "surface.frag")
        self.assertFalse(result["policy"]["assetCodeExecuted"])
        self.assertFalse(result["policy"]["compilerInvoked"])
        self.assertFalse(result["policy"]["workspaceSeedAuthorized"])
        self.assertFalse(result["policy"]["applyAuthorized"])

    def test_source_preview_rejects_parent_reparse_even_with_same_bytes(self):
        assets = self.root / "preview-assets"
        original = assets / "shader-dir"
        replacement = assets / "replacement"
        original.mkdir(parents=True)
        replacement.mkdir()
        source = "float4 main() : SV_Target { return 1; }\n"
        (original / "main.hlsl").write_text(source, encoding="utf-8")
        (replacement / "main.hlsl").write_text(source, encoding="utf-8")
        inventory = self.server.discover_recovery_assets(assets)
        candidate = next(
            item
            for item in inventory["candidates"]
            if item["relativePath"] == "shader-dir/main.hlsl"
        )
        original.rename(assets / "held-original")
        redirected = assets / "shader-dir"
        try:
            if os.name == "nt":
                import _winapi

                _winapi.CreateJunction(str(replacement), str(redirected))
                self.assertFalse(redirected.is_symlink())
            else:
                os.symlink(replacement, redirected, target_is_directory=True)
        except (ImportError, OSError, NotImplementedError):
            self.skipTest("reparse/symlink creation is unavailable")

        preview = self.server._active_recovery_source_preview(
            assets, candidate, 1024
        )
        self.assertIsNone(preview)

    def test_explicit_recovery_limits_reject_values_above_server_hard_caps(self):
        assets = self.root / "shader-assets"
        assets.mkdir()
        cases = (
            {"max_files": self.server._RECOVERY_INSPECT_MAX_FILES + 1},
            {
                "max_file_size_mb": (
                    self.server._RECOVERY_INSPECT_MAX_FILE_SIZE_MB + 1
                )
            },
            {
                "max_total_read_mb": (
                    self.server._RECOVERY_INSPECT_MAX_TOTAL_READ_MB + 1
                )
            },
            {
                "max_candidates": (
                    self.server._RECOVERY_INSPECT_MAX_CANDIDATES + 1
                )
            },
        )
        for override in cases:
            arguments = {
                "asset_root": str(assets),
                "max_files": 1,
                "max_file_size_mb": 1,
                "max_total_read_mb": 1,
                "max_candidates": 1,
                **override,
            }
            result = json.loads(
                self.server.inspect_asset_shader_recovery(**arguments)
            )
            self.assertEqual(result["type"], "ValueError")
            self.assertIn("fixed hard cap", result["error"])

    def test_legacy_scan_defaults_recovery_inventory_off(self):
        parameter = inspect.signature(self.server.scan_asset_symbols).parameters[
            "include_recovery_candidates"
        ]
        self.assertIs(parameter.default, False)

    def test_recommended_one_shot_returns_bounded_shader_recovery(self):
        assets = self.root / "one-shot-assets"
        assets.mkdir()
        (assets / "main.hlsl").write_text(
            "float4 main() : SV_Target { return 1; }\n", encoding="utf-8"
        )
        workspace = self.root / "one-shot-workspace"
        with mock.patch.object(self.server, "SymbolService", _FakeSymbolService):
            result = json.loads(
                self.server.resolve_capture_symbols(
                    str(assets),
                    output_dir=str(workspace),
                    apply_verified=False,
                )
            )
        self.assertNotIn("error", result, result)
        recovery = result["assetShaderRecovery"]
        self.assertEqual(
            recovery["schema"], "replaylab-active-asset-shader-recovery/v1"
        )
        self.assertEqual(recovery["inventory"]["candidateCount"], 1)
        self.assertEqual(
            recovery["boundShaders"][0]["topCandidates"][0]["relativePath"],
            "main.hlsl",
        )
        self.assertLessEqual(
            len(json.dumps(recovery, ensure_ascii=False).encode("utf-8")),
            recovery["reportPolicy"]["maximumJsonUtf8Bytes"],
        )
        self.assertFalse(recovery["policy"]["workspaceSeedAuthorized"])
        self.assertFalse(recovery["policy"]["applyAuthorized"])

    def test_active_symbol_scan_cache_is_bounded_lru_with_ttl(self):
        def value(index: int) -> dict:
            return {
                "scan": {"scanId": str(index)},
                "databasePath": "db",
                "recoveryInventory": {"candidates": []},
            }

        keys = [
            ("workspace", "session-%d" % index, "asset", "build")
            for index in range(self.server._ACTIVE_SYMBOL_SCAN_CACHE_MAX_ENTRIES + 1)
        ]
        with mock.patch.object(self.server.time, "monotonic", return_value=0.0):
            for index, key in enumerate(keys):
                self.server._active_symbol_scan_cache_put_if_absent(key, value(index))
        self.assertEqual(
            len(self.server._ACTIVE_SYMBOL_SCAN_CACHE),
            self.server._ACTIVE_SYMBOL_SCAN_CACHE_MAX_ENTRIES,
        )
        self.assertNotIn(keys[0], self.server._ACTIVE_SYMBOL_SCAN_CACHE)
        with mock.patch.object(
            self.server.time,
            "monotonic",
            return_value=self.server._ACTIVE_SYMBOL_SCAN_CACHE_TTL_SECONDS + 1,
        ):
            self.assertIsNone(
                self.server._active_symbol_scan_cache_get(keys[-1], "db")
            )
        self.assertFalse(self.server._ACTIVE_SYMBOL_SCAN_CACHE)

    def test_activation_rejects_unregistered_project_root(self):
        self._json(self.server.ensure_active_replay_project("Example Game"))
        outside = self.root / "outside-project"
        outside.mkdir()
        result = json.loads(self.server.activate_active_replay_project(str(outside)))
        self.assertEqual(result["schema"], "replaylab-active-project-mcp-error/v1")
        self.assertIn("not registered", result["error"])
        self.assertFalse(result["applied"])


class ActiveWorkspaceWrapperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _load_server()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "projects" / "p" / "workspaces" / "shaders" / "w"
        self.workspace.mkdir(parents=True)
        source_path = self.workspace / "source" / "shader.hlsl"
        source_path.parent.mkdir()
        source_path.write_text("float4 main() : SV_Target { return 1; }\n", encoding="utf-8")
        manifest_path = self.workspace / "manifest.json"
        manifest_path.write_text('{"schema":"test-workspace"}\n', encoding="utf-8")
        self.source_path = source_path.resolve()
        self.manifest_path = manifest_path.resolve()
        self.source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        self.manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        candidate_path = self.workspace / "build" / "candidate.dxbc"
        candidate_path.parent.mkdir()
        candidate_path.write_bytes(b"DXBC\x01\x02\x03\x04")
        self.candidate_path = candidate_path.resolve()
        self.candidate_sha256 = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        self.code_sha256 = "2" * 64
        self.baseline_abi_sha256 = "5" * 64
        self.candidate_abi_sha256 = "6" * 64
        self.abi_request_id = "8" * 64
        self.abi_report_sha256 = "9" * 64
        self.capture_id = "c" * 64
        gate_path = self.workspace / "ai" / "organize-code-gate.json"
        gate_path.parent.mkdir()
        gate_path.write_text(
            json.dumps(
                {
                    "schema": "renderdoc-ai-shader-organization-gate/v1",
                    "schemaVersion": 1,
                    "policyVersion": "renderdoc-ai-organize-workspace/v2",
                    "organizationId": "7" * 32,
                    "reviewedAt": "2026-08-28T00:00:00Z",
                    "requestedTransformation": "organize-only",
                    "workspaceId": "a" * 32,
                    "captureId": self.capture_id,
                    "stage": "fragment",
                    "entryPoint": "main",
                    "baselineSourceSha256": self.source_sha256,
                    "proposalSourceSha256": self.source_sha256,
                    "baselineCodeSha256": self.code_sha256,
                    "organizedCodeSha256": self.code_sha256,
                    "codeSha256Equal": True,
                    "equivalenceLevel": "exact-code",
                    "decision": "accepted-executable-editable",
                    "organizedSourcePromoted": True,
                    "executableEditableSourcePath": str(self.source_path),
                    "baselineCompiledAndAbiValidated": True,
                    "organizedCompiledAndAbiValidated": True,
                    "rawRecoveryCompiledAndAbiValidated": False,
                    "semanticEquivalenceVerified": False,
                    "replayApplied": False,
                    "safeToStartLiveEdit": True,
                    "finalBinding": {
                        "complete": True,
                        "sourceSha256": self.source_sha256,
                        "revision": 1,
                        "candidateRawSha256": self.candidate_sha256,
                        "codeSha256": self.code_sha256,
                        "abiRequestId": self.abi_request_id,
                        "abiReportSha256": self.abi_report_sha256,
                    },
                    "finalSourceSha256": self.source_sha256,
                    "finalRevision": 1,
                    "finalCandidateRawSha256": self.candidate_sha256,
                    "finalCodeSha256": self.code_sha256,
                    "finalAbiRequestId": self.abi_request_id,
                    "finalAbiReportSha256": self.abi_report_sha256,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.gate_path = gate_path.resolve()
        self.context = {
            "schema": "renderdoc-ultra-current-context/v1",
            "schemaVersion": 1,
            "capture": {
                "loaded": True,
                "captureId": self.capture_id,
                "captureSessionId": "d" * 32,
                "path": "D:/captures/frame.rdc",
                "api": "D3D11",
            },
            "events": {"currentEventId": 7},
            "shaders": {"bound": []},
        }

        class IPC:
            def __init__(inner):
                inner.context = self.context

            def call(inner, method, params=None, timeout=None):
                if method != "get_current_renderdoc_context":
                    raise AssertionError(method)
                return copy.deepcopy(inner.context)

        class Active:
            def __init__(inner):
                inner.registered = None
                inner.seeded = None
                inner.published = None

            def status(inner):
                return {
                    "paths": {
                        "shaderWorkspaces": str(
                            self.root / "projects" / "p" / "workspaces" / "shaders"
                        )
                    }
                }

            def pin_active_project(inner, *unused_args, **unused_kwargs):
                return nullcontext()

            def register_workspace(
                inner, status, *, require_current_candidate=False,
                include_compiled_candidate=True,
            ):
                if require_current_candidate is not include_compiled_candidate:
                    raise AssertionError("project publication mode is inconsistent")
                inner.registered = status
                compiled = None
                if include_compiled_candidate:
                    latest = status["latestCompiled"]
                    abi_gate = latest["abiGate"]
                    compiled = {
                        "schema": "renderdoc-ultra-project-compiled-candidate/v1",
                        "schemaVersion": 1,
                        "rawSha256": latest["rawSha256"],
                        "codeSha256": latest["codeSha256"],
                        "byteSize": latest["byteSize"],
                        "kind": latest["kind"],
                        "sourceSha256": status["source"]["currentSha256"],
                        "sourceRevision": status["revision"],
                        "targetProfile": status["targetProfile"],
                        "originBaselineAbiSha256": abi_gate["baselineAbiSha256"],
                        "originCandidateAbiSha256": abi_gate["candidateAbiSha256"],
                        "originAbiReportSha256": abi_gate["reportSha256"],
                        "originRequestId": abi_gate["requestId"],
                        "validationScope": "replay-reflection-abi-compatible",
                        "semanticEquivalenceVerified": False,
                        "artifactPath": (
                            "knowledge/workspaces/binaries/%s.bin"
                            % latest["rawSha256"]
                        ),
                    }
                material = {
                    "workspaceId": status["workspaceId"],
                    "captureId": status["capture"]["captureId"],
                    "originCatalogSha256": "1" * 64,
                    "originResourceId": status["shader"]["resourceId"],
                    "stage": status["stage"],
                    "entryPoint": status["entryPoint"],
                    "rawSha256": status["baseline"]["rawSha256"],
                    "codeSha256": self.code_sha256,
                    "layoutHash": "3" * 64,
                    "interfaceHash": "4" * 64,
                    "interfaceComplete": True,
                    "baselineAbiSha256": "5" * 64,
                    "targetProfile": status["targetProfile"],
                    "sourceSha256": status["source"]["currentSha256"],
                    "analysisSha256": "",
                }
                if compiled is not None:
                    material["compiledCandidate"] = {
                        key: value
                        for key, value in compiled.items()
                        if key != "artifactPath"
                    }
                else:
                    material["publicationMode"] = "source-only-draft"
                knowledge_id = hashlib.sha256(
                    json.dumps(
                        material,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()[:32]
                knowledge = {
                    **material,
                    "knowledgeId": knowledge_id,
                    "sourcePath": "knowledge/workspaces/%s.hlsl"
                    % knowledge_id,
                    "sourceOriginPath": status["source"]["path"],
                    "workspacePath": status["workspacePath"],
                    "workspaceManifestSha256": self.manifest_sha256,
                    "registeredAt": "2026-08-28T00:00:00Z",
                }
                if compiled is not None:
                    knowledge["compiledCandidate"] = compiled
                inner.published = knowledge
                return {
                    "knowledge": knowledge,
                    "idempotent": False,
                    "flowchartArtifacts": [],
                    "policy": {
                        "flowchartExplanatoryOnly": True,
                        "physicalEvidence": False,
                        "applyAuthorization": False,
                    },
                    "applied": False,
                }

            def get_workspace_knowledge(inner, knowledge_id):
                if (
                    not isinstance(inner.published, dict)
                    or inner.published.get("knowledgeId") != knowledge_id
                ):
                    raise RuntimeError("published workspace is unavailable")
                return copy.deepcopy(inner.published)

            def seed_workspace(inner, knowledge_id, status):
                inner.seeded = (knowledge_id, status)
                return {"sourceSha256": self.source_sha256, "applied": False}

        class Workspaces:
            def __init__(inner):
                inner.calls = 0

            def status(inner, workspace_path):
                inner.calls += 1
                modified = inner.calls > 1
                return {
                    "schema": "renderdoc-ultra-shader-workspace-status/v1",
                    "workspacePath": workspace_path,
                    "manifestPath": str(self.manifest_path),
                    "workspaceId": "a" * 32,
                    "capture": {
                        "captureId": self.capture_id,
                        "capturePath": "D:/captures/frame.rdc",
                        "api": "D3D11",
                    },
                    "shader": {
                        "resourceId": 77,
                        "stage": "fragment",
                        "entryPoint": "main",
                    },
                    "stage": "fragment",
                    "entryPoint": "main",
                    "baseline": {"rawSha256": "b" * 64},
                    "revision": 1,
                    "targetProfile": {
                        "profile": "ps_5_0",
                        "containerFormat": "dxbc",
                    },
                    "source": {
                        "present": True,
                        "path": str(self.source_path),
                        "format": "hlsl",
                        "lane": "executable-editable",
                        "compileEligible": True,
                        "currentSha256": self.source_sha256,
                        "modifiedSinceDecompile": modified,
                    },
                    "latestCompiled": {
                        "rawSha256": self.candidate_sha256,
                        "codeSha256": self.code_sha256,
                        "byteSize": self.candidate_path.stat().st_size,
                        "kind": "dxbc",
                        "sourceSha256": self.source_sha256,
                        "revision": 1,
                        "candidateBlobPath": str(self.candidate_path),
                        "replayAbiValidated": True,
                        "abiCompatible": True,
                        "requiresReplayAbiValidation": False,
                        "validationScope": "replay-reflection-abi-compatible",
                        "semanticEquivalenceVerified": False,
                        "abiGate": {
                            "status": "compatible",
                            "baselineAbiSha256": self.baseline_abi_sha256,
                            "candidateAbiSha256": self.candidate_abi_sha256,
                            "requestId": self.abi_request_id,
                            "reportSha256": self.abi_report_sha256,
                        },
                    },
                    "replayAbiValidated": True,
                    "abiCompatible": True,
                }

        class Organization:
            def validate_publication_gate(inner, workspace_path, status=None):
                del status
                if Path(workspace_path).resolve() != self.workspace.resolve():
                    raise RuntimeError("organization publication workspace differs")
                if not self.gate_path.is_file():
                    raise RuntimeError("organization publication gate is missing")
                return json.loads(self.gate_path.read_text(encoding="utf-8"))

        self.active = Active()
        self.workspaces = Workspaces()
        self.organization = Organization()
        self.artifact_service_patch = mock.patch.object(
            self.server, "_shader_workspace_artifact_service", return_value=self.workspaces)
        self.artifact_service_patch.start()
        self.addCleanup(self.artifact_service_patch.stop)
        self.old_service = self.server._ACTIVE_PROJECT_SERVICE
        self.old_organization = self.server._SHADER_ORGANIZATION_MANAGER
        self.old_ipc = self.server.ipc
        self.server._ACTIVE_PROJECT_SERVICE = self.active
        self.server._SHADER_ORGANIZATION_MANAGER = self.organization
        self.server.ipc = IPC()
        self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS.clear()
        # Exact catalog byte/use checks are covered by the workspace rebind
        # tests; these wrappers exercise publication against its live witness.
        self.catalog_binding_patch = mock.patch.object(
            self.server,
            "_ensure_workspace_capture_catalog_identity",
            return_value={
                "workspaceId": "a" * 32,
                "captureId": self.capture_id,
                "captureSessionId": "d" * 32,
            },
        )
        self.catalog_binding = self.catalog_binding_patch.start()
        self.addCleanup(self.catalog_binding_patch.stop)
        self.project_capture_patch = mock.patch.object(
            self.server, "_ensure_active_workspace_project_capture"
        )
        self.project_capture = self.project_capture_patch.start()
        self.addCleanup(self.project_capture_patch.stop)

    def tearDown(self):
        self.server._ACTIVE_PROJECT_SERVICE = self.old_service
        self.server._SHADER_ORGANIZATION_MANAGER = self.old_organization
        self.server.ipc = self.old_ipc
        self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS.clear()
        self.temporary.cleanup()

    def test_archived_manifest_provenance_keeps_current_source_candidate_checks(self):
        registered = json.loads(self.server.register_active_project_workspace(str(self.workspace)))
        self.assertNotIn("error", registered, registered)
        receipt = copy.deepcopy(registered["result"])
        status = self.workspaces.status(str(self.workspace))
        receipt["knowledge"]["workspaceManifestSha256"] = "f" * 64
        with self.assertRaisesRegex(RuntimeError, "exact workspace"):
            self.server._validated_active_workspace_registration(
                receipt, status, copy.deepcopy(receipt["knowledge"]))
        self.server._validated_active_workspace_registration(
            receipt, status, copy.deepcopy(receipt["knowledge"]), allow_historical_manifest=True)
        for field, value in (("sourceSha256", "a" * 64), ("rawSha256", "c" * 64),
                             ("workspacePath", "other-workspace")):
            changed = copy.deepcopy(receipt)
            changed["knowledge"][field] = value
            with self.assertRaises(RuntimeError):
                self.server._validated_active_workspace_registration(
                    changed, status, copy.deepcopy(changed["knowledge"]), allow_historical_manifest=True)

    def test_register_and_seed_are_capture_bound_and_never_apply(self):
        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=self.workspaces
        ):
            registered = json.loads(
                self.server.register_active_project_workspace(str(self.workspace))
            )
            self.assertNotIn("error", registered)
            self.assertFalse(registered["applied"])
            self.assertEqual(registered["captureSessionId"], "d" * 32)
            seeded = json.loads(
                self.server.seed_active_shader_workspace(
                    "e" * 32, str(self.workspace)
                )
            )
        self.assertNotIn("error", seeded)
        self.assertFalse(seeded["applied"])
        self.assertTrue(
            seeded["destinationStatus"]["source"]["modifiedSinceDecompile"]
        )
        self.assertEqual(self.active.seeded[0], "e" * 32)

    def test_registration_uses_exact_workspace_catalog_with_empty_ui_capture_id(self):
        self.context["capture"]["captureId"] = ""
        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=self.workspaces
        ):
            result = json.loads(self.server.register_active_project_workspace(str(self.workspace)))
        self.assertNotIn("error", result, result)
        self.assertEqual(result["captureId"], self.capture_id)
        self.assertEqual(result["captureSessionId"], "d" * 32)
        self.assertIsNotNone(self.active.registered)
        self.catalog_binding.assert_called_once()
        self.assertEqual(self.server._ACTIVE_CAPTURE_CATALOG_BINDINGS, {})

    def test_registration_rejects_wrong_live_path_before_publication(self):
        self.context["capture"].update(captureId="", path="D:/captures/different.rdc")
        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=self.workspaces
        ):
            result = json.loads(self.server.register_active_project_workspace(str(self.workspace)))
        self.assertIn("does not match this workspace", result["error"])
        self.assertIsNone(self.active.registered)

    def test_finishing_draft_does_not_register_project_without_explicit_effect(self):
        finished = {
            "ok": True,
            "workspacePath": str(self.workspace),
            "organizationId": "f" * 32,
            "expectedRevision": 2,
            "expectedCandidateRawSha256": "3" * 64,
        }
        manager = mock.Mock()
        manager.save_draft.return_value = copy.deepcopy(finished)
        with mock.patch.object(
            self.server, "_shader_organization_manager", return_value=manager
        ):
            result = json.loads(
                self.server.finish_shader_workspace_organization(
                    str(self.workspace), "f" * 32, validate_for_apply=False
                )
            )
        self.assertNotIn("projectSync", result)
        self.assertNotIn("registration", result)
        manager.save_draft.assert_called_once_with(
            str(self.workspace), "f" * 32, generate_flow_graph=False
        )
        manager.finish_draft.assert_not_called()
        self.assertIsNone(self.active.registered)

        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=self.workspaces
        ):
            registered = json.loads(
                self.server.register_active_project_workspace(str(self.workspace))
            )
        self.assertNotIn("error", registered)
        self.assertFalse(registered["applied"])
        self.assertIsNotNone(self.active.registered)

    def test_registration_preserves_reviewed_rename_only_with_changed_code_hash(self):
        gate = json.loads(self.gate_path.read_text(encoding="utf-8"))
        gate.update(
            baselineCodeSha256="3" * 64,
            codeSha256Equal=False,
            equivalenceLevel="rename-only",
        )
        self.gate_path.write_text(json.dumps(gate), encoding="utf-8")
        # This wrapper fixture represents an already trusted review. The
        # organization tests exercise the sealed source/rename proof itself.
        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=self.workspaces
        ):
            result = json.loads(
                self.server.register_active_project_workspace(str(self.workspace))
            )
        self.assertNotIn("error", result, result)
        self.assertIsNotNone(self.active.registered)
        self.assertFalse(result["applied"])

    def test_registration_ignores_legacy_organization_verdict_metadata(self):
        original = json.loads(self.gate_path.read_text(encoding="utf-8"))
        for changes in (
            {"baselineCodeSha256": "3" * 64},
            {"codeSha256Equal": False, "equivalenceLevel": "rename-only"},
            {"equivalenceLevel": "rename-only"},
            {"equivalenceLevel": "rejected"},
        ):
            with self.subTest(changes=changes):
                self.active.registered = None
                self.gate_path.write_text(
                    json.dumps({**original, **changes}), encoding="utf-8"
                )
                with mock.patch.object(
                    self.server, "_shader_workspace_service", return_value=self.workspaces
                ):
                    result = json.loads(
                        self.server.register_active_project_workspace(str(self.workspace))
                    )
                self.assertNotIn("error", result, result)
                self.assertIsNotNone(self.active.registered)

    def test_registration_accepts_trusted_observational_pass(self):
        gate = json.loads(self.gate_path.read_text(encoding="utf-8"))
        gate.update(baselineCodeSha256="3" * 64, codeSha256Equal=False,
                    equivalenceLevel="observational-pass")
        self.gate_path.write_text(json.dumps(gate), encoding="utf-8")
        with mock.patch.object(self.server, "_shader_workspace_service", return_value=self.workspaces):
            result = json.loads(self.server.register_active_project_workspace(str(self.workspace)))
        self.assertNotIn("error", result, result)
        self.assertIsNotNone(self.active.registered)

    def test_registration_accepts_trusted_soft_baseline_binding(self):
        gate = json.loads(self.gate_path.read_text(encoding="utf-8"))
        gate.update(decision="soft-accepted-baseline", softPublishAllowed=True,
                    organizedSourcePromoted=False, rawRecoveryCompiledAndAbiValidated=True,
                    codeSha256Equal=False, equivalenceLevel="rejected",
                    baselineSourceSha256=gate["finalSourceSha256"],
                    proposalSourceSha256="4" * 64, organizedCodeSha256="3" * 64)
        self.gate_path.write_text(json.dumps(gate), encoding="utf-8")
        with mock.patch.object(self.server, "_shader_workspace_service", return_value=self.workspaces):
            result = json.loads(self.server.register_active_project_workspace(str(self.workspace)))
        self.assertNotIn("error", result, result)
        self.assertIsNotNone(self.active.registered)

    def test_registration_rejects_missing_shared_artifact_provenance(self):
        original = self.active.register_workspace

        def incomplete(status, **kwargs):
            result = original(status, **kwargs)
            result["knowledge"].pop("workspaceManifestSha256")
            return result

        self.active.register_workspace = incomplete
        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=self.workspaces
        ):
            result = json.loads(
                self.server.register_active_project_workspace(str(self.workspace))
            )
        self.assertEqual(result["schema"], "replaylab-active-project-mcp-error/v1")
        self.assertIn("exact workspace", result["error"])
        self.assertFalse(result["applied"])

    def test_registration_requires_exact_persisted_readback(self):
        original = self.active.get_workspace_knowledge

        def tampered(knowledge_id):
            record = original(knowledge_id)
            record["sourceSha256"] = "0" * 64
            return record

        self.active.get_workspace_knowledge = tampered
        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=self.workspaces
        ):
            result = json.loads(
                self.server.register_active_project_workspace(str(self.workspace))
            )
        self.assertEqual(result["schema"], "replaylab-active-project-mcp-error/v1")
        self.assertIn("read back exactly", result["error"])
        self.assertFalse(result["applied"])

    def test_registration_accepts_current_candidate_without_organization_gate(self):
        self.gate_path.unlink()
        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=self.workspaces
        ):
            result = json.loads(
                self.server.register_active_project_workspace(str(self.workspace))
            )
        self.assertNotIn("error", result, result)
        self.assertIsNotNone(self.active.registered)
        self.project_capture.assert_called_once()

    def test_source_only_draft_registers_without_compile_or_abi(self):
        original_status = self.workspaces.status

        def draft_status(workspace_path):
            status = original_status(workspace_path)
            status["latestCompiled"] = None
            status["replayAbiValidated"] = False
            status["abiCompatible"] = False
            return status

        with mock.patch.object(self.workspaces, "status", side_effect=draft_status), \
             mock.patch.object(
                 self.server, "_shader_workspace_service", return_value=self.workspaces
             ):
            strict = json.loads(
                self.server.register_active_project_workspace(str(self.workspace))
            )
            draft = json.loads(self.server.register_active_project_workspace(
                str(self.workspace), source_only_draft=True
            ))

        self.assertIn("error", strict)
        self.assertNotIn("error", draft, draft)
        self.assertTrue(draft["sourceOnlyDraft"])
        self.assertFalse(draft["applied"])
        self.assertEqual(
            draft["result"]["knowledge"]["sourceSha256"], self.source_sha256
        )
        self.assertNotIn("compiledCandidate", draft["result"]["knowledge"])
        self.assertEqual(
            self.active.get_workspace_knowledge(
                draft["result"]["knowledge"]["knowledgeId"]
            ), draft["result"]["knowledge"]
        )

    def test_source_only_draft_still_checks_exact_current_source(self):
        def change_source(unused_project, unused_status, unused_context):
            self.source_path.write_text(
                "float4 main() : SV_Target { return 2; }\n", encoding="utf-8"
            )

        self.project_capture.side_effect = change_source
        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=self.workspaces
        ):
            result = json.loads(self.server.register_active_project_workspace(
                str(self.workspace), source_only_draft=True
            ))
        self.assertIn("error", result)
        self.assertIsNone(self.active.registered)

    def test_registration_rejects_source_change_during_capture_import(self):
        def change_source(unused_project, unused_status, unused_context):
            self.source_path.write_text("float4 main() : SV_Target { return 2; }\n", encoding="utf-8")

        self.project_capture.side_effect = change_source
        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=self.workspaces
        ):
            result = json.loads(self.server.register_active_project_workspace(str(self.workspace)))
        self.assertIn("error", result)
        self.assertIsNone(self.active.registered)

    def test_reviewed_workspace_imports_exact_capture_into_empty_real_project(self):
        self.project_capture_patch.stop()
        project = ActiveReplayProjectService(
            self.root / "real-config" / "active.json", self.root / "real-projects"
        )
        project.create_project(name="Reviewed", game_id="example-game", build_id="build-1",
                               platform="windows-x64", api_family="d3d11")
        fixture = _CatalogIPC(self.root)
        exported = fixture.call("export_shader_catalog", {
            "output_dir": str(self.root / "exact-catalog"), "event_ids": []
        })
        catalog_path = Path(exported["catalogPath"])
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["selection"] = {"mode": "explicit", "eventIds": [142], "stages": ["fragment"]}
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        status = self.workspaces.status(str(self.workspace))
        status["capture"].update(
            captureId=fixture.capture_id, catalogPath=str(catalog_path),
            catalogSha256=hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        )
        status["shader"].update(resourceId=902, shaderKey=fixture.shader["shaderKey"],
                                 rawSha256=fixture.shader["rawSha256"])
        status["baseline"].update(rawSha256=fixture.shader["rawSha256"],
                                   codeSha256=fixture.shader["codeSha256"],
                                   abiSha256=self.baseline_abi_sha256)
        latest = status["latestCompiled"]
        latest.update(blobPath="build/candidate.dxbc", sourcePath="source/shader.hlsl")
        report = self.workspace / "build" / "abi.json"
        report.write_text("{}", encoding="utf-8")
        latest["abiGate"].update(reportPath="build/abi.json",
                                reportSha256=hashlib.sha256(report.read_bytes()).hexdigest())
        persisted = copy.deepcopy(latest)
        persisted.pop("candidateBlobPath")
        self.manifest_path.write_text(json.dumps({"revision": 1, "latestCompiled": persisted}), encoding="utf-8")
        gate = json.loads(self.gate_path.read_text(encoding="utf-8"))
        gate["captureId"] = fixture.capture_id
        gate["finalAbiReportSha256"] = latest["abiGate"]["reportSha256"]
        gate["finalBinding"]["abiReportSha256"] = latest["abiGate"]["reportSha256"]
        self.gate_path.write_text(json.dumps(gate), encoding="utf-8")
        self.context["capture"]["captureId"] = ""
        context = self.server._canonical_active_capture_context(self.context, fixture.capture_id)
        workspaces = mock.Mock()
        workspaces.status.side_effect = lambda unused_path: copy.deepcopy(status)
        publish = lambda: self.server._register_reviewed_workspace_with_readback(
            project, workspaces, str(self.workspace),
            before_publish=lambda reviewed: self.server._ensure_active_workspace_project_capture(
                project, reviewed, context
            ),
        )
        self.assertEqual(project.replay_service().status()["captureCount"], 0)
        with mock.patch.object(project, "import_capture", wraps=project.import_capture) as importer:
            catalog_bytes = catalog_path.read_bytes()
            catalog_path.write_bytes(catalog_bytes + b"\n")
            with self.assertRaisesRegex(RuntimeError, "catalog hash changed"):
                publish()
            catalog_path.write_bytes(catalog_bytes)
            self.context["capture"]["captureSessionId"] = "f" * 32
            with self.assertRaisesRegex(RuntimeError, "capture/session changed"):
                publish()
            self.context["capture"]["captureSessionId"] = "d" * 32
            self.assertEqual(project.replay_service().status()["captureCount"], 0)
            importer.assert_not_called()
            result = publish()
            self.assertEqual(project.replay_service().status()["captureCount"], 1)
            self.assertEqual(project.replay_service().status()["knowledgeCounts"]["workspaces"], 1)
            self.assertEqual(result["knowledge"]["captureId"], fixture.capture_id)
            published_source = project.replay_service().root / result["knowledge"]["sourcePath"]
            self.assertEqual(published_source.read_bytes(), self.source_path.read_bytes())
            publish()
            self.assertEqual(importer.call_count, 1)
            self.assertEqual(importer.call_args.kwargs, {
                "expected_capture_id": fixture.capture_id,
                "expected_capture_path": "D:/captures/frame.rdc", "expected_api": "D3D11",
            })
            self.gate_path.unlink()
            publish()
            self.assertEqual(importer.call_count, 1)

            # The explicit-project endpoint uses the same strict readback as
            # active-project publication. Its base writer publishes no UI
            # diagrams, but must still describe that empty scope truthfully.
            with mock.patch.object(self.server, "_shader_workspace_service", return_value=workspaces):
                for attempt in range(2):
                    explicit = json.loads(self.server.register_replay_project_workspace(
                        str(project.replay_service().root), str(self.workspace)))
                    self.assertNotIn("error", explicit, explicit)
                    self.assertEqual(explicit["knowledge"]["knowledgeId"], result["knowledge"]["knowledgeId"])
                    self.assertEqual(explicit["flowchartArtifacts"], [])
                    self.assertEqual(explicit["flowGraphArtifacts"], [])
                    self.assertIs(explicit["policy"]["applyAuthorization"], False)

            # A later explanation of the same saved source advances the head.
            # Final replacement registration must keep it, not attempt to
            # republish the historical analysis-free record and fail its CAS.
            from src import project_restore
            analysis = self.workspace / "ai" / "analysis.md"
            analysis.parent.mkdir(exist_ok=True)
            analysis.write_text("Current source explanation.\n", encoding="utf-8")
            documented = project.register_workspace(status)
            self.assertNotEqual(documented["knowledge"]["knowledgeId"], result["knowledge"]["knowledgeId"])
            manifest_before = project.replay_service().manifest_path.read_bytes()
            with mock.patch.object(self.server, "_workspace_active_capture_context", return_value=context):
                for attempt in range(2):
                    applied = project_restore._register_applied_workspace_with_readback(
                        project, workspaces, self.workspace, status)
                    self.assertTrue(applied["reusedCurrentHead"])
                    self.assertEqual(applied["knowledge"], documented["knowledge"])
            self.assertEqual(project.replay_service().manifest_path.read_bytes(), manifest_before)

    def test_registration_rejects_changed_compiled_candidate_before_write(self):
        self.candidate_path.write_bytes(b"DXBC\x09\x09\x09\x09")
        with mock.patch.object(
            self.server, "_shader_workspace_service", return_value=self.workspaces
        ):
            result = json.loads(
                self.server.register_active_project_workspace(str(self.workspace))
            )
        self.assertEqual(result["schema"], "replaylab-active-project-mcp-error/v1")
        self.assertIn("stale", result["error"])
        self.assertIsNone(self.active.registered)


if __name__ == "__main__":
    unittest.main()
