from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from src import internal_tool_service


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


class _PassIPC:
    def __init__(self, capture_id: str, session_id: str):
        self.capture_id = capture_id
        self.session_id = session_id
        self.calls = []

    def call(self, method, params=None, timeout=None):
        params = copy.deepcopy(params or {})
        self.calls.append((method, params, timeout))
        if method in (
            "get_pass_classification_inputs", "get_current_frame_flow_inputs"
        ):
            return {
                "schema": (
                    "replaylab-current-frame-flow-inputs/v1"
                    if method == "get_current_frame_flow_inputs"
                    else "replaylab-pass-classification-inputs/v1"
                ),
                "schemaVersion": 1,
                "captureId": str(
                    params.get("expected_capture_id", self.capture_id)
                ),
                "captureSessionId": self.session_id,
                "actions": [
                    {
                        "eventId": 10,
                        "name": "BasePass Opaque",
                        "flags": 0x0002,
                        "numIndices": 3,
                        "numInstances": 1,
                        "markerPath": ["Scene", "BasePass"],
                    }
                ],
                "pipelineStates": {
                    "10": {
                        "shaders": {
                            "fragment": {
                                "shaderId": 77,
                                "entryPoint": "main",
                                "rawSha256": "c" * 64,
                            }
                        },
                        "renderTargets": [
                            {"resourceId": 4, "name": "SceneColor", "format": "RGBA8"}
                        ],
                        "depthTarget": {
                            "resourceId": 5,
                            "name": "SceneDepth",
                            "format": "D32",
                        },
                        "depthTestEnabled": True,
                        "depthWriteEnabled": True,
                    }
                },
                "summary": {
                    "actionCount": 1,
                    "pipelineStateCount": 1,
                    "singleReplayThreadTransaction": True,
                    "truncated": False,
                },
            }
        if method == "load_pass_classification":
            classification = params["classification"]
            return {
                "schema": "replaylab-pass-panel-load/v1",
                "schemaVersion": 1,
                "captureId": classification["captureId"],
                "captureSessionId": self.session_id,
                "captureBindingMode": params.get(
                    "capture_binding_mode", "project-catalog"
                ),
                "panel": {"visible": True},
                "loaded": True,
                "replayMutation": False,
                "classificationAuthority": "FixedProgram",
                "aiProposalAuthority": "AIProvisional",
            }
        raise AssertionError("unexpected IPC method %s" % method)


class _PassStore:
    instances = []

    def __init__(self, project_path):
        self.project_path = Path(project_path).resolve()
        self.saved = []
        type(self).instances.append(self)

    def load(self):
        return {
            "schema": "replaylab-project-pass-knowledge/v1",
            "schemaVersion": 1,
            "records": [],
        }

    def save(self, classification, *, capture_id=""):
        self.saved.append((copy.deepcopy(classification), capture_id))
        return {
            "schema": "replaylab-project-pass-knowledge/v1",
            "schemaVersion": 1,
            "path": str(self.project_path / "knowledge" / "passes.json"),
            "captureId": capture_id,
            "provisionalOverlayCount": classification.get(
                "provisionalOverlayCount", 0
            ),
        }


class ServerPassClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _load_server()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.capture_id = "a" * 64
        self.session_id = "b" * 32
        self.workspace_id = "d" * 32
        self.context = {
            "schema": "renderdoc-ultra-current-context/v1",
            "schemaVersion": 1,
            "capture": {
                "loaded": True,
                "captureId": self.capture_id,
                "captureSessionId": self.session_id,
                "path": str(self.root / "capture.rdc"),
                "api": "GraphicsAPI.D3D12",
            },
            "events": {"currentEventId": 10},
            "shaders": {"bound": []},
        }
        self.catalog_root = self.root / "catalogs"
        self.catalog = self.catalog_root / self.capture_id / "catalog.json"
        self.catalog.parent.mkdir(parents=True)
        self.catalog_value = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "captureId": self.capture_id,
            "shaders": [],
            "uses": [],
        }
        self.catalog.write_text(json.dumps(self.catalog_value), encoding="utf-8")
        self.replay_project = self.root / "replay-project"
        self.replay_project.mkdir()
        self.prepared = {
            "schema": "replaylab-active-capture-prepare/v1",
            "schemaVersion": 1,
            "capture": {
                "captureId": self.capture_id,
                "captureSessionId": self.session_id,
            },
            "stagedCatalog": {
                "captureId": self.capture_id,
                "catalogPath": str(self.catalog),
                "catalogSha256": hashlib.sha256(self.catalog.read_bytes()).hexdigest(),
            },
            "project": {
                "workspaceId": self.workspace_id,
                "scopeConfigured": True,
                "paths": {
                    "captureCatalogs": str(self.catalog_root),
                    "replayProject": str(self.replay_project),
                },
                "replayProject": {"projectPath": str(self.replay_project)},
            },
            "applied": False,
        }
        self.ipc = _PassIPC(self.capture_id, self.session_id)
        self.active = mock.Mock()
        self.active.status.return_value = copy.deepcopy(self.prepared["project"])
        self.old_ipc = self.server.ipc
        self.old_active = self.server._ACTIVE_PROJECT_SERVICE
        self.server.ipc = self.ipc
        self.server._ACTIVE_PROJECT_SERVICE = self.active
        self.server._PASS_CLASSIFICATION_CACHE.clear()
        _PassStore.instances.clear()
        self.patches = [
            mock.patch.object(
                self.server,
                "_active_live_context",
                side_effect=lambda **unused: copy.deepcopy(self.context),
            ),
            mock.patch.object(
                self.server,
                "prepare_active_capture_project",
                return_value=json.dumps(self.prepared),
            ),
            mock.patch.object(self.server, "PassKnowledgeStore", _PassStore),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.server.ipc = self.old_ipc
        self.server._ACTIVE_PROJECT_SERVICE = self.old_active
        self.server._PASS_CLASSIFICATION_CACHE.clear()
        self.temporary.cleanup()

    def _fixed(self, **arguments):
        result = json.loads(
            self.server.classify_current_frame_passes(**arguments)
        )
        self.assertNotIn("error", result, result)
        return result

    def test_fixed_classification_defaults_to_display_only_without_project_write(self):
        result = self._fixed()
        self.assertEqual(result["operation"], "classify-fixed")
        display_capture_id = self.server._display_session_capture_id(
            self.session_id
        )
        self.assertEqual(result["classification"]["captureId"], display_capture_id)
        self.assertEqual(result["classification"]["summary"]["actionCount"], 1)
        self.assertEqual(len(result["classificationId"]), 32)
        self.assertEqual(result["captureBindingMode"], "display-session")
        self.assertEqual(result["effectMode"], "display-only")
        self.assertFalse(result["projectKnowledge"]["projectPrepared"])
        self.assertFalse(result["projectKnowledge"]["persistRequested"])
        self.assertFalse(result["projectKnowledge"]["persisted"])
        self.assertFalse(result["panel"]["publishRequested"])
        self.assertFalse(result["panel"]["published"])
        self.assertEqual(result["panel"]["publicationMode"], "not-published")
        self.assertEqual(len(_PassStore.instances), 0)
        self.server.prepare_active_capture_project.assert_not_called()
        self.assertFalse(
            any(
                item[0] == "get_pass_classification_inputs"
                for item in self.ipc.calls
            )
        )
        inputs_call = next(
            item for item in self.ipc.calls
            if item[0] == "get_current_frame_flow_inputs"
        )
        self.assertEqual(
            inputs_call[1]["expected_capture_id"], display_capture_id
        )
        self.assertEqual(
            inputs_call[1]["expected_capture_session_id"], self.session_id
        )
        self.assertFalse(any(
            item[0] == "load_pass_classification" for item in self.ipc.calls
        ))
        summary = result["classification"]["summary"]
        self.assertFalse(summary["sourceTruncated"])
        self.assertEqual(summary["omittedActionCountLowerBound"], 0)
        self.assertEqual(summary["truncationReasons"], [])
        self.assertEqual(
            summary["effectiveMaxActions"], self.server.MAX_PASS_PANEL_ACTIONS
        )

    def test_explicit_persistence_prepares_project_without_opening_retired_ui(self):
        result = self._fixed(persist_project=True)
        self.assertEqual(result["captureId"], self.capture_id)
        self.assertEqual(result["captureBindingMode"], "project-catalog")
        self.assertEqual(result["effectMode"], "project-persisted")
        self.assertTrue(result["projectKnowledge"]["projectPrepared"])
        self.assertTrue(result["projectKnowledge"]["persistRequested"])
        self.assertTrue(result["projectKnowledge"]["persisted"])
        self.assertFalse(result["panel"]["published"])
        self.assertEqual(result["panel"]["publicationMode"], "not-published")
        self.assertFalse(any(
            item[0] == "load_pass_classification" for item in self.ipc.calls
        ))
        self.assertEqual(len(_PassStore.instances), 1)
        self.assertEqual(_PassStore.instances[0].saved[0][1], self.capture_id)
        self.server.prepare_active_capture_project.assert_called_once_with()
        self.assertTrue(any(
            item[0] == "get_pass_classification_inputs"
            for item in self.ipc.calls
        ))

    def test_retired_ui_request_fails_before_capture_replay_or_project_work(self):
        with mock.patch.object(
            self.server, "_active_live_context",
            side_effect=AssertionError("retired UI must not query capture"),
        ) as live_context, mock.patch.object(
            self.server, "_cached_fixed_pass_classification",
            side_effect=AssertionError("retired UI must not resolve classification"),
        ) as cached:
            fixed = json.loads(self.server.classify_current_frame_passes(
                publish_to_ui=True, persist_project=True,
            ))
            overlay = json.loads(self.server.apply_current_frame_pass_ai_proposals(
                "unused", "unused", "unused", [],
                publish_to_ui=True, persist_project=True,
            ))
        for result in (fixed, overlay):
            self.assertIn("retired", str(result["error"]))
        live_context.assert_not_called()
        cached.assert_not_called()
        self.server.prepare_active_capture_project.assert_not_called()
        self.assertEqual(self.ipc.calls, [])
        self.assertEqual(_PassStore.instances, [])

    def test_readonly_frame_flow_uses_one_composite_without_project_or_ui(self):
        with mock.patch.object(
            self.server,
            "prepare_active_capture_project",
            side_effect=AssertionError("read-only flow must not prepare a project"),
        ):
            result = json.loads(self.server.analyze_current_frame_flow())
        self.assertNotIn("error", result, result)
        self.assertEqual("analyze-current-frame-flow", result["operation"])
        self.assertFalse(result["applied"])
        self.assertTrue(result["policy"]["readOnly"])
        self.assertEqual([10], result["flow"]["summary"]["executionTimeline"])
        item = result["flow"]["items"][0]
        self.assertIn("workload", item)
        self.assertIn("inputs", item)
        self.assertIn("outputs", item)
        self.assertIn("drawState", item)
        self.assertIn("participation", item)
        flow_call = next(
            value for value in self.ipc.calls
            if value[0] == "get_current_frame_flow_inputs"
        )
        self.assertEqual(8000, flow_call[1]["max_actions"])
        self.assertFalse(any(
            value[0] == "load_pass_classification" for value in self.ipc.calls
        ))

    def test_panel_projection_accepts_the_2500_action_ui_boundary(self):
        actions = [
            {
                "eventId": event_id,
                "name": "DrawIndirect",
                "flags": 0x080002,
            }
            for event_id in range(1, self.server.MAX_PASS_PANEL_ACTIONS + 1)
        ]
        rich = self.server.classify_passes(
            actions, capture_id=self.capture_id
        )
        compact = self.server._pass_panel_classification_view(rich)
        panel_path = (
            Path(__file__).resolve().parents[2]
            / "renderdoc_mcp_bridge"
            / "project_pass_panel.py"
        )
        spec = importlib.util.spec_from_file_location(
            "replaylab_pass_panel_ui_boundary", panel_path
        )
        panel_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(panel_module)
        validated = panel_module.validate_classification(compact)
        self.assertEqual(
            self.server.MAX_PASS_PANEL_ACTIONS,
            len(validated["items"]),
        )

    def test_frame_flow_result_byte_gate_truncates_with_an_exact_receipt(self):
        actions = [
            {
                "eventId": event_id,
                "name": "BasePass Opaque %04d" % event_id,
                "flags": 0x0002,
                "numVertices": 3,
                "numInstances": 1,
            }
            for event_id in range(1, 201)
        ]
        states = {
            str(action["eventId"]): {
                "colorWriteTargets": [
                    {
                        "resourceId": action["eventId"] + 1000,
                        "name": "SceneColor-%04d" % action["eventId"],
                        "format": "R16G16B16A16_FLOAT",
                    }
                ],
                "depthTestEnabled": False,
                "depthWriteEnabled": False,
            }
            for action in actions
        }
        flow_inputs = {
            "schema": "replaylab-current-frame-flow-inputs/v1",
            "schemaVersion": 1,
            "captureId": self.capture_id,
            "captureSessionId": self.session_id,
            "actions": actions,
            "pipelineStates": states,
            "summary": {
                "actionCount": len(actions),
                "pipelineStateCount": len(states),
                "singleReplayThreadTransaction": True,
                "truncated": False,
                "omittedActionCountLowerBound": 0,
            },
        }
        flow_ipc = mock.Mock()
        flow_ipc.call.return_value = flow_inputs
        self.server.ipc = flow_ipc
        byte_budget = 64 * 1024
        with mock.patch.object(
            self.server, "MAX_FRAME_FLOW_RESULT_BYTES", byte_budget
        ):
            encoded = self.server.analyze_current_frame_flow(max_actions=200)
        result = json.loads(encoded)
        self.assertNotIn("error", result, result)
        self.assertLessEqual(len(encoded.encode("utf-8")), byte_budget)
        self.assertTrue(result["transport"]["truncated"])
        self.assertGreater(
            result["transport"]["omittedActionCountLowerBound"], 0
        )
        self.assertEqual(
            len(encoded.encode("utf-8")),
            result["transport"]["serializedUtf8Bytes"],
        )
        self.assertEqual(
            len(encoded.encode("utf-8")),
            result["flow"]["summary"]["resultSerializedBytes"],
        )

    def test_agent_facing_pass_receipts_fit_the_smallest_connector_envelope(self):
        # Codex app-server rejects dynamic-tool responses above one Mi-character.
        # The Host must reserve space for JSON escaping and protocol framing.
        self.assertLess(self.server.MAX_AGENT_TOOL_RESULT_BYTES, 1024 * 1024)
        self.assertEqual(
            self.server.MAX_FRAME_FLOW_RESULT_BYTES,
            self.server.MAX_AGENT_TOOL_RESULT_BYTES,
        )
        self.assertEqual(
            self.server.MAX_PASS_TOOL_RESULT_BYTES,
            self.server.MAX_AGENT_TOOL_RESULT_BYTES,
        )

    def test_pass_tool_projection_is_bounded_and_self_measured(self):
        classification = self.server.classify_passes(
            [{"eventId": 1, "name": "Draw", "flags": 2}],
            capture_id=self.capture_id,
        )
        classification["items"][0]["evidence"] = ["x" * 500_000]
        response = {
            "operation": "classification-test",
            "classification": classification,
        }
        byte_budget = 64 * 1024
        with mock.patch.object(
            self.server, "MAX_PASS_TOOL_RESULT_BYTES", byte_budget
        ):
            encoded = self.server._bounded_pass_tool_json(response)
        result = json.loads(encoded)
        self.assertTrue(result["transport"]["projected"])
        self.assertLessEqual(len(encoded.encode("utf-8")), byte_budget)
        self.assertEqual(
            len(encoded.encode("utf-8")),
            result["transport"]["serializedUtf8Bytes"],
        )

    def test_pass_tool_projection_truncates_compact_items_and_tree(self):
        classification = self.server.classify_passes(
            [
                {
                    "eventId": event_id,
                    "name": "Long compact action %04d %s"
                    % (event_id, "x" * 480),
                    "flags": 2,
                }
                for event_id in range(1, 2501)
            ],
            capture_id=self.capture_id,
        )
        response = {
            "operation": "classification-test",
            "classification": classification,
        }
        byte_budget = self.server.MAX_AGENT_TOOL_RESULT_BYTES
        with mock.patch.object(
            self.server, "MAX_PASS_TOOL_RESULT_BYTES", byte_budget
        ):
            encoded = self.server._bounded_pass_tool_json(response)
        result = json.loads(encoded)
        transport = result["transport"]
        compact = result["classification"]
        self.assertTrue(transport["projected"])
        self.assertTrue(transport["truncated"])
        self.assertTrue(transport["itemsTruncated"])
        self.assertGreater(transport["omittedItemCount"], 0)
        self.assertEqual(len(compact["items"]), transport["returnedItemCount"])
        self.assertEqual(2500, transport["sourceItemCount"])
        self.assertLessEqual(len(encoded.encode("utf-8")), byte_budget)
        self.assertEqual(
            len(encoded.encode("utf-8")), transport["serializedUtf8Bytes"]
        )
        retained = {item["eventId"] for item in compact["items"]}

        def tree_events(nodes):
            result = set()
            for node in nodes:
                if node.get("nodeType") == "action":
                    result.add(node["eventId"])
                result.update(tree_events(node.get("children", [])))
            return result

        self.assertEqual(retained, tree_events(compact["tree"]))
        self.assertIn(
            "host-result-byte-budget",
            compact["summary"]["truncationReasons"],
        )

    def test_ai_overlay_uses_only_current_server_cached_fixed_result(self):
        fixed = self._fixed()
        result = json.loads(
            self.server.apply_current_frame_pass_ai_proposals(
                fixed["classificationId"],
                fixed["captureId"],
                self.session_id,
                [
                    {
                        "eventId": 10,
                        "category": "BasePass",
                        "subcategory": "Opaque",
                        "displayLabel": "Main opaque objects",
                        "rationale": "Asset-verified material naming suggests BasePass",
                    }
                ],
            )
        )
        self.assertNotIn("error", result, result)
        self.assertEqual(result["operation"], "attach-ai-provisional")
        proposal = result["classification"]["items"][0]["semanticProposal"]
        self.assertEqual(proposal["authority"], "AIProvisional")
        self.assertFalse(proposal["verified"])
        self.assertFalse(proposal["applied"])
        self.assertEqual(
            result["fixedClassificationSha256"],
            fixed["fixedClassificationSha256"],
        )
        self.assertEqual(result["captureBindingMode"], "display-session")
        self.assertEqual(result["effectMode"], "display-only")
        self.assertFalse(result["projectKnowledge"]["persistRequested"])
        self.assertFalse(result["projectKnowledge"]["persisted"])
        self.assertFalse(result["panel"]["published"])
        self.assertEqual(result["panel"]["publicationMode"], "not-published")
        self.assertEqual(len(_PassStore.instances), 0)

    def test_display_only_overlay_cannot_upgrade_itself_to_project_persistence(self):
        fixed = self._fixed()
        panel_call_count = sum(
            1 for method, unused_params, unused_timeout in self.ipc.calls
            if method == "load_pass_classification"
        )

        result = json.loads(
            self.server.apply_current_frame_pass_ai_proposals(
                fixed["classificationId"],
                fixed["captureId"],
                self.session_id,
                [{"eventId": 10, "category": "BasePass"}],
                persist_project=True,
            )
        )

        self.assertIn("display-only", result["error"])
        self.assertIn("persist_project=true", result["error"])
        self.assertEqual(len(_PassStore.instances), 0)
        self.assertEqual(
            panel_call_count,
            sum(
                1 for method, unused_params, unused_timeout in self.ipc.calls
                if method == "load_pass_classification"
            ),
        )

    def test_ai_overlay_never_persists_a_truncated_fixed_result(self):
        fixed = self._fixed(persist_project=True)
        record = self.server._PASS_CLASSIFICATION_CACHE[
            fixed["classificationId"]
        ]
        record["classification"]["summary"]["sourceTruncated"] = True
        record["classification"]["summary"]["truncated"] = True
        record["fixedSha256"] = self.server._canonical_json_sha256(
            self.server._fixed_pass_classification_view(
                record["classification"]
            ),
            "test truncated fixed classification",
        )
        store_count = len(_PassStore.instances)

        result = json.loads(
            self.server.apply_current_frame_pass_ai_proposals(
                fixed["classificationId"],
                fixed["captureId"],
                self.session_id,
                [{"eventId": 10, "category": "BasePass"}],
                persist_project=True,
            )
        )

        self.assertNotIn("error", result, result)
        self.assertFalse(result["projectKnowledge"]["persisted"])
        self.assertIn("truncated", result["projectKnowledge"]["reason"])
        self.assertEqual(store_count, len(_PassStore.instances))

    def test_ai_overlay_rejects_panel_unsafe_text_before_persist_or_publish(self):
        fixed = self._fixed()
        store_count = len(_PassStore.instances)
        panel_call_count = sum(
            1 for method, unused_params, unused_timeout in self.ipc.calls
            if method == "load_pass_classification"
        )

        result = json.loads(
            self.server.apply_current_frame_pass_ai_proposals(
                fixed["classificationId"],
                fixed["captureId"],
                self.session_id,
                [{"eventId": 10, "displayLabel": "x" * 513}],
            )
        )

        self.assertIn("panel-safe text limit", result["error"])
        self.assertEqual(store_count, len(_PassStore.instances))
        self.assertEqual(
            panel_call_count,
            sum(
                1 for method, unused_params, unused_timeout in self.ipc.calls
                if method == "load_pass_classification"
            ),
        )

    def test_ai_overlay_text_policy_allows_html_and_rationale_lf(self):
        fixed = self._fixed()
        result = json.loads(
            self.server.apply_current_frame_pass_ai_proposals(
                fixed["classificationId"],
                fixed["captureId"],
                self.session_id,
                [{
                    "eventId": 10,
                    "displayLabel": "<b>literal label</b>",
                    "rationale": "line one\nline two",
                }],
                publish_to_ui=False,
                persist_project=False,
            )
        )
        self.assertNotIn("error", result, result)
        proposal = result["classification"]["items"][0]["semanticProposal"]
        self.assertEqual(proposal["displayLabel"], "<b>literal label</b>")
        self.assertEqual(proposal["rationale"], "line one\nline two")

    def test_ai_overlay_rejects_label_lf_and_bidi_override_before_side_effects(self):
        fixed = self._fixed()
        store_count = len(_PassStore.instances)
        panel_call_count = sum(
            1 for method, unused_params, unused_timeout in self.ipc.calls
            if method == "load_pass_classification"
        )
        for unsafe in ("line one\nline two", "safe\u202ereversed"):
            with self.subTest(unsafe=repr(unsafe)):
                result = json.loads(
                    self.server.apply_current_frame_pass_ai_proposals(
                        fixed["classificationId"],
                        fixed["captureId"],
                        self.session_id,
                        [{"eventId": 10, "displayLabel": unsafe}],
                    )
                )
                self.assertIn("forbidden", result["error"])
        self.assertEqual(store_count, len(_PassStore.instances))
        self.assertEqual(
            panel_call_count,
            sum(
                1 for method, unused_params, unused_timeout in self.ipc.calls
                if method == "load_pass_classification"
            ),
        )

    def test_ai_overlay_rejects_physical_fields_and_changed_session(self):
        fixed = self._fixed()
        forbidden = json.loads(
            self.server.apply_current_frame_pass_ai_proposals(
                fixed["classificationId"],
                fixed["captureId"],
                self.session_id,
                [{"eventId": 10, "category": "BasePass", "byteOffset": 16}],
            )
        )
        self.assertIn("forbidden fields", forbidden["error"])
        self.context["capture"]["captureSessionId"] = "e" * 32
        stale = json.loads(
            self.server.apply_current_frame_pass_ai_proposals(
                fixed["classificationId"],
                fixed["captureId"],
                self.session_id,
                [{"eventId": 10, "category": "BasePass"}],
            )
        )
        self.assertIn("capture/session changed", stale["error"])


class _LiveManager:
    def __init__(self):
        self.calls = []

    @staticmethod
    def _result(live_edit_id="f" * 32):
        return {
            "schema": "replaylab-shader-live-edit/v1",
            "schemaVersion": 1,
            "liveEditId": live_edit_id,
            "state": "running",
            "rollback": None,
        }

    def start(self, workspace_path, debounce_ms):
        self.calls.append(("start", workspace_path, debounce_ms))
        return self._result()

    def poll(self, live_edit_id):
        self.calls.append(("poll", live_edit_id))
        return self._result(live_edit_id)

    def stop(self, live_edit_id):
        self.calls.append(("stop", live_edit_id))
        value = self._result(live_edit_id)
        value["state"] = "stopped"
        return value

    def list(self):
        self.calls.append(("list",))
        return {
            "persistenceEnabled": True,
            "sessions": [self._result()],
            "authorizations": [],
            "resumeResults": [],
        }

    def migrate_authorization(self, source_workspace, workspace, source_sha256):
        self.calls.append(
            ("migrate-authorization", source_workspace, workspace, source_sha256)
        )
        return {
            "authorizationMigrated": True,
            "authorizationId": "e" * 32,
            "sourceWorkspacePath": source_workspace,
            "workspacePath": workspace,
            "sourceSha256": source_sha256,
            "watchStarted": False,
            "restartResumeAuthorized": True,
            "newSaveRequired": True,
            "semanticEquivalenceVerified": False,
        }


class ServerLiveEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _load_server()

    def setUp(self):
        self.manager = _LiveManager()
        def remote(operation, parameters):
            if operation == "migrate":
                return self.manager.migrate_authorization(parameters["source_workspace_path"], parameters["workspace_path"], parameters["expected_source_sha256"])
            return getattr(self.manager, operation)(**parameters)
        self.remote = mock.patch.object(self.server, "_shader_live_edit_remote", side_effect=remote)
        self.remote.start()

    def tearDown(self):
        self.remote.stop()

    def test_start_poll_stop_expose_shared_resource_policy(self):
        started = json.loads(
            self.server.start_shader_workspace_live_edit("D:/workspace", 450)
        )
        self.assertEqual(started["operation"], "start")
        self.assertTrue(started["backgroundAutoApplyAuthorized"])
        self.assertEqual(started["applyScope"], "captured-shader-resource")
        self.assertTrue(started["policy"]["distinctResourceIdsNeverMerged"])
        self.assertTrue(
            started["policy"]["failedSavePreservesLastGoodReplacement"]
        )
        token = started["result"]["liveEditId"]
        self.assertEqual(
            json.loads(self.server.poll_shader_workspace_live_edit(token))["operation"],
            "poll",
        )
        stopped = json.loads(self.server.stop_shader_workspace_live_edit(token))
        self.assertEqual(stopped["result"]["state"], "stopped")
        self.assertFalse(stopped["backgroundAutoApplyAuthorized"])
        self.assertEqual(
            self.manager.calls,
            [
                ("start", "D:/workspace", 450),
                ("poll", token),
                ("stop", token),
            ],
        )

    def test_list_exposes_persistent_sessions(self):
        listed = json.loads(self.server.list_shader_workspace_live_edits())
        self.assertEqual(listed["operation"], "list")
        self.assertTrue(listed["result"]["persistenceEnabled"])
        self.assertEqual(self.manager.calls, [("list",)])

    def test_explicit_authorization_migration_is_exposed_without_applying(self):
        source_hash = "a" * 64
        migrated = json.loads(
            self.server.migrate_shader_workspace_live_edit_authorization(
                "D:/old-workspace",
                "D:/new-workspace",
                source_hash,
            )
        )
        self.assertEqual(migrated["operation"], "migrate-authorization")
        self.assertTrue(migrated["result"]["authorizationMigrated"])
        self.assertFalse(migrated["result"]["watchStarted"])
        self.assertFalse(migrated["appliedByThisCall"])
        self.assertEqual(
            self.manager.calls,
            [
                (
                    "migrate-authorization",
                    "D:/old-workspace",
                    "D:/new-workspace",
                    source_hash,
                )
            ],
        )

    def test_explicit_strict_apply_callback_parses_success_and_raises_error_envelope(self):
        success = {
            "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
            "operation": "apply-shared-resource",
            "applied": False,
            "binding": {"revision": 2, "candidateRawSha256": "a" * 64},
            "validation": {"replayAbiValidated": False},
        }
        with mock.patch.object(
            self.server,
            "apply_shader_workspace_shared_resource",
            return_value=json.dumps(success),
        ) as apply:
            result = self.server._shader_live_edit_apply_candidate_with_abi(
                "D:/workspace", 2, "a" * 64
            )
        self.assertEqual(result, success)
        apply.assert_called_once_with(
            "D:/workspace",
            expected_revision=2,
            expected_candidate_raw_sha256="a" * 64,
        )
        with mock.patch.object(
            self.server,
            "apply_shader_workspace_shared_resource",
            return_value=json.dumps({"error": "ABI changed", "type": "RuntimeError"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "ABI changed"):
                self.server._shader_live_edit_apply_candidate_with_abi(
                    "D:/workspace", 3, "b" * 64
                )

    def test_explicit_strict_apply_uses_initial_abi_lease_and_one_bridge_apply(self):
        for catalog_mode in ("verified", "legacy", "unavailable"):
            with self.subTest(catalog=catalog_mode):
                self._check_later_live_save_uses_initial_abi_lease(catalog_mode)

    def _check_later_live_save_uses_initial_abi_lease(self, catalog_mode):
        workspace = "D:/workspace-live-abi-lease"
        capture_id = "c" * 64
        session_id = "8" * 32
        workspace_id = "b" * 32
        baseline_hash = "a" * 64
        candidate_hash = "d" * 64
        baseline_abi = "9" * 64
        lease_request = "e" * 64
        lease_report = "f" * 64
        current_request = "7" * 64
        old_replacement = "1" * 32
        new_replacement = "2" * 32
        source_hash = "3" * 64
        request = {
            "requestId": current_request,
            "binding": {
                "workspaceId": workspace_id,
                "captureId": capture_id,
                "revision": 4,
                "candidateRawSha256": candidate_hash,
                "baselineAbiSha256": baseline_abi,
                "stage": "fragment",
                "entryPoint": "main",
                "api": "d3d12",
                "encoding": "dxil",
            },
            "params": {
                "candidate_path": "D:/fixed/candidate.dxil",
                "expected_sha256": candidate_hash,
                "stage": "fragment",
                "entry_point": "main",
                "encoding": "dxil",
            },
        }
        status = {
            "workspacePath": workspace,
            "workspaceId": workspace_id,
            "capture": {
                "captureId": capture_id,
                "catalogPath": "D:/fixed/catalog.json",
            },
            "shader": {
                "shaderKey": "77:fragment:main:%s" % baseline_hash,
                "resourceId": 77,
            },
            "stage": "fragment",
            "entryPoint": "main",
            "revision": 4,
            "source": {"currentSha256": source_hash},
            "representation": {
                "lane": "executable-editable",
                "compileEligible": True,
            },
            "baseline": {"rawSha256": baseline_hash},
            "latestCompiled": {
                "revision": 4,
                "sourceSha256": source_hash,
                "rawSha256": candidate_hash,
            },
        }
        status["targetProfile"] = {"profile": "ps_6_6", "kind": "dxil"}
        if catalog_mode != "legacy":
            status["capture"]["catalogSha256"] = "5" * 64
        catalog_binding = {"projection": {"catalogPath": status["capture"]["catalogPath"]},
                           "scope": {"stableCaptureId": capture_id}}

        class Service:
            def status(inner_self, _workspace):
                return copy.deepcopy(status)

            def candidate_reflection_request(inner_self, _workspace):
                return copy.deepcopy(request)

        class Bridge:
            def __init__(inner_self):
                inner_self.calls = []

            def call(inner_self, method, params, timeout=None):
                inner_self.calls.append((method, copy.deepcopy(params), timeout))
                if method == "get_shared_shader_replacements":
                    self.assertTrue(params["lease_metadata_only"])
                    self.assertEqual(params["workspace_id"], workspace_id)
                    self.assertEqual(params["replacement_id"], old_replacement)
                    return {
                        "schema": "renderdoc-ultra-shared-shader-replacement-list/v1",
                        "schemaVersion": 1, "captureId": capture_id,
                        "captureSessionId": session_id, "count": 1,
                        "mutationEpoch": 1, "originalShadersPreserved": True,
                        "replayMutation": False, "replacements": [{
                            **lease, "resourceScoped": True, "eventScoped": False,
                            "allOriginalResourceUsesAffected": True,
                            "originalShaderPreserved": True,
                        }], "leaseMetadataOnly": True,
                        "queryWorkspaceId": workspace_id, "queryReplacementId": old_replacement,
                    }
                self.assertEqual(method, "apply_shader_candidate_shared_resource")
                self.assertEqual(
                    params["abi_validation_mode"],
                    "inline-physical-with-initial-lease",
                )
                self.assertEqual(params["abi_report_sha256"], lease_report)
                self.assertEqual(params["abi_request_id"], current_request)
                self.assertEqual(params["abi_lease_revision"], 3)
                self.assertEqual(params["expected_source_sha256"], source_hash)
                self.assertEqual(params["expected_profile"], "ps_6_6")
                if catalog_mode == "verified":
                    self.assertEqual(params["workspace_catalog_binding"], catalog_binding)
                else:
                    self.assertNotIn("workspace_catalog_binding", params)
                return {
                    "schema": "renderdoc-ultra-shared-shader-replacement/v1",
                    "schemaVersion": 1,
                    "replacementId": new_replacement,
                    "operationId": params["operation_id"],
                    "operationCreatedReplacement": True,
                    "captureId": capture_id,
                    "captureSessionId": session_id,
                    "workspaceId": workspace_id,
                    "revision": 4,
                    "anchorEventId": 321,
                    "stage": "fragment",
                    "entryPoint": "main",
                    "api": "d3d12",
                    "encoding": "dxil",
                    "originalResourceId": 77,
                    "originalRawSha256": baseline_hash,
                    "candidateRawSha256": candidate_hash,
                    "physicalAbiSha256": baseline_abi,
                    "shaderFamilyId": "6" * 64,
                    "targetResourceId": 101,
                    "abiRequestId": current_request,
                    "abiReportSha256": lease_report,
                    "resourceScoped": True,
                    "eventScoped": False,
                    "allOriginalResourceUsesAffected": True,
                    "configured": True,
                    "originalShaderPreserved": True,
                    "singleOwner": True,
                    "checkedDerivedPipelineRebuild": True,
                    "capturedRootSignaturesReused": True,
                    "postApplyAnchorVerified": False,
                    "idempotent": False,
                    "metadataOnly": False,
                    "targetBuiltOnce": True,
                    "targetReused": False,
                    "supersededReplacementId": old_replacement,
                    "supersededTargetFreed": True,
                    "abiValidationMode": (
                        "inline-physical-with-initial-lease"
                    ),
                    "preliminaryReflectionSkipped": True,
                    "freshPhysicalAbiValidationPerformed": True,
                    "abiLeaseRequestId": lease_request,
                    "abiLeaseReportSha256": lease_report,
                    "abiLeaseCandidateRawSha256": "4" * 64,
                    "abiLeaseRevision": 3,
                }

        lease = {
            "replacementId": old_replacement,
            "captureId": capture_id,
            "captureSessionId": session_id,
            "workspaceId": workspace_id,
            "workspacePath": workspace,
            "revision": 3,
            "stage": "fragment",
            "anchorEventId": 321,
            "originalResourceId": 77,
            "originalRawSha256": baseline_hash,
            "candidateRawSha256": "4" * 64,
            "abiLeaseRequestId": lease_request,
            "abiLeaseReportSha256": lease_report,
            "abiLeaseCandidateRawSha256": "4" * 64,
            "abiLeaseRevision": 3,
            "physicalAbiSha256": baseline_abi,
        }
        bridge = Bridge()
        with self.server._SHADER_OVERRIDE_GUARD:
            previous_capture_key = self.server._SHADER_OVERRIDE_CAPTURE_KEY
            previous_records = copy.deepcopy(
                self.server._SHARED_SHADER_REPLACEMENT_RECORDS
            )
            self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()
            self.server._SHARED_SHADER_REPLACEMENT_RECORDS[
                old_replacement
            ] = copy.deepcopy(lease)
            self.server._SHADER_OVERRIDE_CAPTURE_KEY = session_id
        try:
            with mock.patch.object(
                self.server, "_shader_workspace_service", return_value=Service()
            ), mock.patch.object(
                self.server, "_verified_restore_catalog_binding", return_value=catalog_binding,
                side_effect=(RuntimeError("optional catalog metadata unavailable")
                             if catalog_mode == "unavailable" else None),
            ), mock.patch.object(
                self.server, "_ensure_workspace_capture_catalog_identity",
                return_value={"captureSessionId": session_id},
            ), mock.patch.object(
                self.server,
                "list_exact_shader_family_uses",
                side_effect=AssertionError(
                    "a live ABI lease must reuse its frozen representative EID"
                ),
            ), mock.patch.object(
                self.server, "ipc", bridge
            ), mock.patch.object(
                self.server,
                "apply_shader_workspace_shared_resource",
                side_effect=AssertionError("ordinary duplicate ABI pass was used"),
            ), mock.patch.object(
                self.server, "_shader_cbuffer_delivery_from_status", return_value={},
            ):
                result = self.server._shader_live_edit_apply_candidate_with_abi(
                    workspace, 4, candidate_hash
                )
            self.assertTrue(result["applied"])
            self.assertTrue(result["validation"]["replayAbiValidated"])
            self.assertTrue(
                result["validation"]["preliminaryReplayReflectionSkipped"]
            )
            self.assertTrue(
                result["validation"]["freshPhysicalValidationPerformed"]
            )
            self.assertEqual([item[0] for item in bridge.calls], [
                "get_shared_shader_replacements", "apply_shader_candidate_shared_resource"])
            with self.server._SHADER_OVERRIDE_GUARD:
                remembered = self.server._SHARED_SHADER_REPLACEMENT_RECORDS[
                    new_replacement
                ]
            self.assertEqual(remembered["abiLeaseRequestId"], lease_request)
            self.assertEqual(
                remembered["abiLeaseReportSha256"], lease_report
            )
        finally:
            with self.server._SHADER_OVERRIDE_GUARD:
                self.server._SHARED_SHADER_REPLACEMENT_RECORDS.clear()
                self.server._SHARED_SHADER_REPLACEMENT_RECORDS.update(
                    previous_records
                )
                self.server._SHADER_OVERRIDE_CAPTURE_KEY = previous_capture_key


class ServerRestoreCurrentShaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _load_server()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.capture_id = "1" * 64
        self.session_id = "2" * 32
        self.original_raw = "3" * 64
        self.candidate_raw = "4" * 64
        self.workspace_id = "5" * 32
        self.replacement_id = "6" * 32
        self.operation_id = "7" * 32
        self.physical_abi = "8" * 64
        self.family_id = "9" * 64
        self.context = {
            "schema": "renderdoc-ultra-current-context/v1",
            "schemaVersion": 1,
            "capture": {
                "loaded": True,
                "captureId": self.capture_id,
                "captureSessionId": self.session_id,
            },
            "events": {"currentEventId": 22},
            "shaders": {
                "bound": [
                    {
                        "stage": "fragment",
                        "resourceId": 77,
                        "entryPoint": "main",
                        "rawSha256": self.candidate_raw,
                    }
                ]
            },
        }
        self.shader_root = self.root / "workspaces"
        self.workspace = (
            self.shader_root
            / self.capture_id
            / "fragment"
            / ("rid-77-%s" % self.original_raw)
        )
        self.workspace.mkdir(parents=True)

        class Active:
            def status(inner):
                return {"paths": {"shaderWorkspaces": str(self.shader_root)}}

            def shader_workspace_path(inner, **unused):
                return self.workspace

        class Workspaces:
            def status(inner, path):
                return {
                    "schema": "renderdoc-ultra-shader-workspace-status/v1",
                    "workspacePath": str(self.workspace),
                    "workspaceId": self.workspace_id,
                    "revision": 3,
                    "capture": {
                        "captureId": self.capture_id,
                        "api": "D3D11",
                    },
                    "shader": {
                        "stage": "fragment",
                        "resourceId": 77,
                        "entryPoint": "main",
                        "rawSha256": self.original_raw,
                    },
                    "targetProfile": {"containerFormat": "dxbc"},
                    "baseline": {
                        "rawSha256": self.original_raw,
                        "abiSha256": self.physical_abi,
                    },
                }

        self.active = Active()
        self.workspaces = Workspaces()
        self.replacements = [
            {
                "replacementId": self.replacement_id,
                "operationId": self.operation_id,
                "workspaceId": self.workspace_id,
                "revision": 2,
                "anchorEventId": 22,
                "stage": "fragment",
                "entryPoint": "main",
                "api": "d3d11",
                "encoding": "dxbc",
                "originalResourceId": 77,
                "originalRawSha256": self.original_raw,
                "candidateRawSha256": self.candidate_raw,
                "physicalAbiSha256": self.physical_abi,
                "shaderFamilyId": self.family_id,
                "targetResourceId": 88,
                "resourceScoped": True,
                "eventScoped": False,
                "allOriginalResourceUsesAffected": True,
                "originalShaderPreserved": True,
            }
        ]

        class IPC:
            def __init__(inner):
                inner.calls = []
                inner.epoch = 1

            def call(inner, method, params=None, timeout=None):
                inner.calls.append((method, copy.deepcopy(params or {}), timeout))
                if method == "get_shared_shader_replacements":
                    return {
                        "schema": "renderdoc-ultra-shared-shader-replacement-list/v1",
                        "schemaVersion": 1,
                        "captureId": self.capture_id,
                        "captureSessionId": self.session_id,
                        "replacements": copy.deepcopy(self.replacements),
                        "count": len(self.replacements),
                        "mutationEpoch": inner.epoch,
                        "originalShadersPreserved": True,
                        "replayMutation": False,
                    }
                if method != "restore_shader_candidate_shared_resource_exact":
                    raise AssertionError(method)
                matching = [
                    item
                    for item in self.replacements
                    if item["originalResourceId"]
                    == params["expected_original_resource_id"]
                ]
                base = {
                    "schema": "renderdoc-ultra-exact-shared-shader-restore/v1",
                    "schemaVersion": 1,
                    "captureId": self.capture_id,
                    "captureSessionId": self.session_id,
                    "workspaceId": self.workspace_id,
                    "eventId": params["expected_event_id"],
                    "stage": params["stage"],
                    "entryPoint": params["entry_point"],
                    "api": "d3d11",
                    "encoding": "dxbc",
                    "originalResourceId": params["expected_original_resource_id"],
                    "originalRawSha256": params["expected_original_sha256"],
                    "physicalAbiSha256": self.physical_abi,
                    "linearized": True,
                    "stateAtLinearizationVerified": True,
                    "originalShaderPreserved": True,
                    "restored": True,
                    "activeReplacementCountForResource": 0,
                }
                if not matching:
                    return {
                        **base,
                        "state": "absent",
                        "alreadyRestored": True,
                        "mutationEpoch": inner.epoch,
                        "rollback": None,
                    }
                if len(matching) != 1:
                    raise RuntimeError("replacement owner conflict")
                item = matching[0]
                if item["operationId"] != params.get("expected_operation_id"):
                    raise RuntimeError("replacement operation conflict")
                self.replacements.remove(item)
                inner.epoch += 1
                rollback = {
                    "schema": "renderdoc-ultra-shared-shader-replacement/v1",
                    "schemaVersion": 1,
                    "replacementId": item["replacementId"],
                    "operationId": item["operationId"],
                    "captureId": self.capture_id,
                    "captureSessionId": self.session_id,
                    "workspaceId": item["workspaceId"],
                    "stage": item["stage"],
                    "entryPoint": item["entryPoint"],
                    "api": item["api"],
                    "encoding": item["encoding"],
                    "originalResourceId": item["originalResourceId"],
                    "originalRawSha256": item["originalRawSha256"],
                    "candidateRawSha256": item["candidateRawSha256"],
                    "physicalAbiSha256": item["physicalAbiSha256"],
                    "shaderFamilyId": item["shaderFamilyId"],
                    "targetResourceId": item["targetResourceId"],
                    "resourceScoped": True,
                    "eventScoped": False,
                    "cleared": True,
                    "targetFreed": True,
                    "originalShaderPreserved": True,
                    "alreadyRestored": False,
                    "idempotentRollback": False,
                }
                return {
                    **base,
                    "state": "rolled-back",
                    "alreadyRestored": False,
                    "mutationEpoch": inner.epoch,
                    "rollback": rollback,
                }

        self.ipc = IPC()
        self.old_ipc = self.server.ipc
        self.old_active = self.server._ACTIVE_PROJECT_SERVICE
        self.server.ipc = self.ipc
        self.server._ACTIVE_PROJECT_SERVICE = self.active

        def current_context(**options):
            context = copy.deepcopy(self.context)
            if not options.get("require_event_state"):
                context["events"] = {"currentEventId": 0, "selectedEventId": 0}
                context["shaders"] = {"bound": []}
            return context

        self.patches = [
            mock.patch.object(
                self.server,
                "_active_live_context",
                side_effect=current_context,
            ),
            mock.patch.object(
                self.server,
                "_shader_workspace_service",
                return_value=self.workspaces,
            ),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.server.ipc = self.old_ipc
        self.server._ACTIVE_PROJECT_SERVICE = self.old_active
        self.temporary.cleanup()

    def _restore(self, stage="fragment", **overrides):
        arguments = {
            "expected_event_id": 22,
            "expected_resource_id": 77,
            "expected_entry_point": "main",
            "expected_raw_sha256": self.original_raw,
            "expected_capture_id": self.capture_id,
            "expected_capture_session_id": self.session_id,
            "expected_workspace_path": str(self.workspace),
            "expected_workspace_id": self.workspace_id,
            "expected_operation_id": self.operation_id,
            "stage": stage,
        }
        arguments.update(overrides)
        return json.loads(self.server.restore_current_shader(**arguments))

    def test_restore_uses_bridge_metadata_and_deterministic_workspace(self):
        result = self._restore("pixel")
        self.assertNotIn("error", result, result)
        self.assertTrue(result["restored"])
        self.assertTrue(result["applied"])
        self.assertEqual(result["replacementId"], self.replacement_id)
        self.assertEqual(result["api"], "d3d11")
        self.assertEqual(result["encoding"], "dxbc")
        self.assertEqual(result["physicalAbiSha256"], self.physical_abi)
        self.assertFalse(result["policy"]["hostMemoryRequired"])
        query = self.ipc.calls[0]
        self.assertEqual(query[1]["expected_capture_id"], self.capture_id)
        self.assertEqual(
            query[1]["expected_capture_session_id"], self.session_id
        )

    def test_no_current_replacement_returns_strict_already_restored_receipt(self):
        self.replacements.clear()
        self.context["shaders"]["bound"][0]["rawSha256"] = self.original_raw
        result = self._restore()
        self.assertTrue(result["restored"])
        self.assertTrue(result["alreadyRestored"])
        self.assertFalse(result["applied"])
        self.assertTrue(result["stateAtLinearizationVerified"])
        self.assertTrue(result["noActiveReplacementForTargetAtLinearization"])
        self.assertEqual(result["replacementQueryCount"], 0)
        self.assertEqual(result["matchingReplacementCount"], 0)
        self.assertEqual(result["workspaceId"], self.workspace_id)
        self.assertEqual(result["api"], "d3d11")
        self.assertEqual(result["encoding"], "dxbc")
        self.assertEqual(result["physicalAbiSha256"], self.physical_abi)
        self.assertIn("already", result["reason"])

    def test_exact_absence_accepts_only_verified_legacy_topology_hash_alias(self):
        from tests.test_shader_abi import complete_interface
        from src.shader_workspace.abi import parse_shader_abi

        self.replacements.clear()
        self.context["shaders"]["bound"][0]["rawSha256"] = self.original_raw
        original_status = self.workspaces.status
        for topology, live_topology in (("", "Topology.Unknown"), ("Topology.Unknown", "")):
            with self.subTest(topology=topology):
                interface = complete_interface()
                interface["outputTopology"] = topology
                baseline_hash = parse_shader_abi(interface).sha256
                live = copy.deepcopy(interface)
                live["outputTopology"] = live_topology
                self.physical_abi = parse_shader_abi(live).sha256
                status = original_status(str(self.workspace))
                status["baseline"].update(abiSha256=baseline_hash, compiledInterface=interface)
                with mock.patch.object(self.workspaces, "status", return_value=status):
                    result = self._restore()
                    self.assertNotIn("error", result, result)
                    self.assertTrue(result["alreadyRestored"])
                    self.assertEqual(result["physicalAbiSha256"], baseline_hash)
                    self.physical_abi = "f" * 64
                    self.assertIn("invalid exact Shader absence", self._restore()["error"])
                    status["baseline"]["abiSha256"] = "e" * 64
                    self.assertIn("baseline ABI hash mismatch", self._restore()["error"])

    def test_event_change_after_exact_absence_prevents_restore_success(self):
        self.replacements.clear()
        self.context["shaders"]["bound"][0]["rawSha256"] = self.original_raw
        original = self.ipc.call

        def navigate_after_restore(method, params=None, timeout=None):
            result = original(method, params, timeout)
            if method == "restore_shader_candidate_shared_resource_exact":
                self.context["events"]["currentEventId"] = 23
            return result

        with mock.patch.object(self.ipc, "call", side_effect=navigate_after_restore):
            result = self._restore()
        self.assertFalse(result["restored"])
        self.assertIn("current Shader binding changed", result["error"])

    def test_unrelated_replacement_count_is_reported_without_claiming_a_global_empty_list(self):
        self.replacements[0]["originalResourceId"] = 99
        self.context["shaders"]["bound"][0]["rawSha256"] = self.original_raw
        result = self._restore()
        self.assertNotIn("error", result, result)
        self.assertTrue(result["alreadyRestored"])
        self.assertEqual(result["replacementQueryCount"], 1)
        self.assertEqual(result["matchingReplacementCount"], 0)
        self.assertTrue(result["noActiveReplacementForTargetAtLinearization"])

    def test_same_resource_replacement_with_mismatched_identity_is_not_already_restored(self):
        self.replacements[0]["stage"] = "vertex"
        self.context["shaders"]["bound"][0]["rawSha256"] = self.original_raw
        result = self._restore()
        self.assertFalse(result["restored"])
        self.assertIn("owns the frozen Shader ResourceId", result["error"])

    def test_replacement_created_during_absence_verification_blocks_noop_success(self):
        original_call = self.ipc.call
        self.replacements.clear()
        self.context["shaders"]["bound"][0]["rawSha256"] = self.original_raw
        calls = 0

        def racing_call(method, params=None, timeout=None):
            nonlocal calls
            result = original_call(method, params, timeout)
            if method == "get_shared_shader_replacements":
                calls += 1
                if calls == 1:
                    self.replacements.append(
                        {
                            "replacementId": self.replacement_id,
                            "operationId": "a" * 32,
                            "workspaceId": self.workspace_id,
                            "revision": 2,
                            "anchorEventId": 22,
                            "stage": "fragment",
                            "entryPoint": "main",
                            "api": "d3d11",
                            "encoding": "dxbc",
                            "originalResourceId": 77,
                            "originalRawSha256": self.original_raw,
                            "candidateRawSha256": self.candidate_raw,
                            "physicalAbiSha256": self.physical_abi,
                            "shaderFamilyId": self.family_id,
                            "targetResourceId": 88,
                            "resourceScoped": True,
                            "eventScoped": False,
                            "allOriginalResourceUsesAffected": True,
                            "originalShaderPreserved": True,
                        }
                    )
            return result

        self.ipc.call = racing_call
        result = self._restore()
        self.assertFalse(result["restored"])
        self.assertIn("operation conflict", result["error"])

    def test_workspace_identity_mismatch_blocks_rollback(self):
        self.replacements[0]["workspaceId"] = "7" * 32
        result = self._restore()
        self.assertFalse(result["restored"])
        self.assertIn("does not match", result["error"])

    def test_navigation_change_cannot_redirect_frozen_shader_restore(self):
        self.context["events"]["currentEventId"] = 23
        self.context["shaders"]["bound"][0]["resourceId"] = 99
        result = self._restore()
        self.assertFalse(result["restored"])
        self.assertIn("current event changed", result["error"])
        self.assertEqual(self.ipc.calls, [])


class ServerOutputFingerprintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _load_server()

    def test_fingerprint_is_capture_session_bound_and_validated(self):
        session = "b" * 32
        calls = []

        class IPC:
            def call(self, method, params=None, timeout=None):
                calls.append((method, copy.deepcopy(params or {}), timeout))
                if method == "get_capture_status":
                    return {
                        "loaded": True,
                        "captureSessionId": session,
                        "capturePath": "D:/capture.rdc",
                    }
                if method == "fingerprint_event_outputs":
                    return {
                        "schema": "replaylab-event-output-fingerprint/v1",
                        "schemaVersion": 1,
                        "captureSessionId": session,
                        "eventId": 12892,
                        "gridSize": 2,
                        "outputs": [
                            {
                                "kind": "color",
                                "index": 0,
                                "resourceId": 7,
                                "textureByteSize": 16,
                                "fullHashByteLimit": 32 * 1024 * 1024,
                                "rawHashStatus": "hashed",
                                "rawByteSize": 16,
                                "rawSha256": "c" * 64,
                                "samples": [
                                    {"x": 0, "y": 0, "value": [0.0, 0.0, 0.0, 1.0]}
                                ],
                            }
                        ],
                        "observationalOnly": True,
                        "semanticEquivalenceVerified": False,
                    }
                raise AssertionError(method)

        previous = self.server.ipc
        self.server.ipc = IPC()
        try:
            result = json.loads(self.server.fingerprint_event_outputs(12892, 2))
        finally:
            self.server.ipc = previous
        self.assertNotIn("error", result, result)
        self.assertEqual(result["captureSessionId"], session)
        self.assertEqual(
            result["validationScope"],
            "single-captured-event-output-observation",
        )
        self.assertEqual(calls[1][1]["expected_capture_session_id"], session)
        self.assertFalse(result["semanticEquivalenceVerified"])


if __name__ == "__main__":
    unittest.main()
