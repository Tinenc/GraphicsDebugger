"""Read-only bridge receipt for the actual Shader Workspace projection."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import server
from replaylab_agent.policy import READ_ONLY_TOOLS
import test_shader_console as console_fixtures


BRIDGE_SOURCE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "__init__.py"


def _read_ui_state(panel):
    source = ast.parse(BRIDGE_SOURCE.read_text(encoding="utf-8"))
    function = next(
        item for item in source.body
        if isinstance(item, ast.FunctionDef)
        and item.name == "_get_shader_workspace_ui_state"
    )
    namespace = {
        "_shader_workspace_ui": panel,
        "_invoke_on_ui_sync": lambda callback: callback(),
        "hashlib": hashlib,
        "_shader_console_protocol": console_fixtures.console,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]),
                 str(BRIDGE_SOURCE), "exec"), namespace)
    return namespace["_get_shader_workspace_ui_state"]({})


def test_ui_state_distinguishes_tree_visibility_from_live_edit_and_replay():
    class TreeItem:
        def __init__(self, owner):
            self.owner = owner

        def treeWidget(self):
            return self.owner

    class Editor:
        def IsReadOnly(self):
            return False

    class Top:
        def isVisible(self):
            return True

    class Panel:
        def __init__(self):
            self.top = Top()
            self.file_tree = object()
            self._workspaces = {
                "a" * 32: {
                    "workspacePath": "C:/project/workspace-a",
                    "captureId": "c" * 64,
                    "stage": "fragment",
                    "entryPoint": "MainPS",
                    "resourceId": 1000000000000000723,
                    "sourcePath": "C:/project/workspace-a/source/shader.hlsl",
                    "editorEligible": True,
                    "autoApplyReady": True,
                },
                "b" * 32: {
                    "workspacePath": "C:/project/workspace-b",
                    "captureId": "c" * 64,
                    "stage": "fragment",
                    "entryPoint": "OtherPS",
                    "resourceId": 1000000000000000755,
                    "sourcePath": "C:/project/workspace-b/source/shader.hlsl",
                    "editorEligible": True,
                    "autoApplyReady": False,
                },
            }
            self._tree_items = {
                "a" * 32: TreeItem(self.file_tree),
                "b" * 32: TreeItem(None),
            }
            self._documents = {
                "a" * 32: {
                    "editor": Editor(),
                    "sourcePath": "C:/project/workspace-a/source/shader.hlsl",
                    "documentKind": "shader",
                    "available": True,
                    "dirty": False,
                    "rawSha256": "d" * 64,
                },
            }
            self._workspace_event_ids = {"a" * 32: [61408]}

        def _current_workspace_id(self):
            return "a" * 32

        def _tab_index(self, workspace_id):
            return 0 if workspace_id == "a" * 32 else -1

        def _document_source_text(self, document):
            return "float4 MainPS() : SV_Target { return 1; }"

    state = _read_ui_state(Panel())
    assert state["present"] is True
    assert state["workspaceTreeCount"] == 1
    assert state["selectedShader"]["workspaceId"] == "a" * 32
    assert state["selectedShader"]["resourceId"] == 1000000000000000723
    assert state["selectedShader"]["visibleInWorkspaceTree"] is True
    assert state["selectedShader"]["openInTab"] is True
    assert state["selectedShader"]["eventIds"] == [61408]
    assert state["documents"][0]["available"] is True
    assert state["documents"][0]["openInTab"] is True
    assert state["workspaces"][1]["visibleInWorkspaceTree"] is False
    assert state["workspaces"][1]["openInTab"] is False
    assert state["documents"][0]["loadedSourceSha256"] == "d" * 64
    assert state["documents"][0]["sourceSha256"] == hashlib.sha256(
        b"float4 MainPS() : SV_Target { return 1; }").hexdigest()
    assert "float4" not in repr(state)


def test_ui_state_reports_absent_panel_without_claiming_tree_visibility():
    state = _read_ui_state(None)
    assert state["present"] is False
    assert state["workspaceTreeCount"] == 0
    assert state["workspaces"] == []
    assert state["selectedShader"] is None


def test_outline_receipt_reads_actual_worker_projection_and_never_promotes_pending_metadata():
    from test_shader_source_flow_async import panel, SOURCE
    from src.shader_workspace.semantic_overview import prepare_overview
    instance, document = panel()
    model = instance._document_model(document["workspaceId"])
    source_sha = hashlib.sha256(SOURCE.encode("utf8")).hexdigest()
    overview = prepare_overview(SOURCE, [{"id": "purpose", "title": "Material response",
        "anchors": [{"function": "Helper"}]}], "MainPS")
    overview["analysisReview"] = {"schema": "bread-shader-semantic-overview-review/v1", "sourceSha256": source_sha,
            "author": "agent", "hostSemanticApproval": False, "scopeMappings": ["Helper"],
            "granularity_evidence": "actual one calculation", "data_flow_evidence": "Helper result reaches main",
            "remainingWork": []}
    model["semanticOverview"] = overview
    instance._workspaces = {document["workspaceId"]: model}
    instance._tree_items = {}
    instance._workspace_event_ids = {}
    instance._current_workspace_id = lambda: document["workspaceId"]
    instance._tab_index = lambda key: 0
    instance._document_source_text = lambda doc: doc["editor"].GetText()
    instance.top = SimpleNamespace(isVisible=lambda: True)
    document["editor"].IsReadOnly = lambda: False
    instance._sync_flow_graph()
    pending = _read_ui_state(instance)["documents"][0]
    assert pending["stageOutlinePending"] is True and pending["stageOutlineCurrent"] is False
    assert not pending["stageOutlineState"]
    instance._snapshot_executor.deliver()
    current = _read_ui_state(instance)["documents"][0]
    assert current["stageOutlineCurrent"] is True and current["stageOutlinePending"] is False
    assert current["stageOutlineState"]["state"] == "reviewed"
    assert current["stageOutlineStageIds"] == ["purpose"]
    assert current["stageOutlineSourceSha256"] == source_sha
    expected = hashlib.sha256(json.dumps(overview, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode("utf8")).hexdigest()
    assert current["stageOutlineOverviewSha256"] == expected
    # A new sidecar must not inherit the previous worker's completed receipt.
    overview["stages"][0]["title"] = "Updated metadata"
    stale = _read_ui_state(instance)["documents"][0]
    assert stale["stageOutlineCurrent"] is False
    assert stale["stageOutlineOverviewSha256"] == expected
    instance._sync_flow_graph()
    assert _read_ui_state(instance)["documents"][0]["stageOutlinePending"] is True
    instance._snapshot_executor.deliver(1)
    refreshed = _read_ui_state(instance)["documents"][0]
    assert refreshed["stageOutlineCurrent"] is True
    assert refreshed["stageOutlineOverviewSha256"] != expected


def test_agent_can_read_actual_tree_visibility_without_mutating_shader():
    observed = {
        "present": True,
        "visible": True,
        "selectedWorkspaceId": "a" * 32,
        "selectedShader": {"workspaceId": "a" * 32,
                           "visibleInWorkspaceTree": True},
        "workspaceTreeCount": 1,
        "workspaces": [{
            "workspaceId": "a" * 32,
            "captureId": "b" * 64,
            "stage": "fragment",
            "entryPoint": "MainPS",
            "resourceId": 1000000000000000723,
            "editorEligible": True,
            "autoApplyReady": True,
            "visibleInWorkspaceTree": True,
            "openInTab": True,
        }],
        "documents": [],
    }
    with mock.patch.object(server.ipc, "call", return_value=observed) as bridge:
        result = json.loads(server.get_shader_workspace_ui_state("a" * 32))
    bridge.assert_called_once_with("get_shader_workspace_ui_state", {},
                                   timeout=30.0)
    assert result["applied"] is False
    assert result["target"]["visibleInWorkspaceTree"] is True
    assert result["target"]["selected"] is True
    assert result["target"]["resourceId"] == 1000000000000000723
    assert "get_shader_workspace_ui_state" in READ_ONLY_TOOLS


def test_agent_ui_receipt_reports_absence_without_erecting_a_workflow_gate():
    observed = {"present": False, "documents": [], "workspaces": [],
                "workspaceTreeCount": 0, "selectedShader": None}
    with mock.patch.object(server.ipc, "call", return_value=observed):
        result = json.loads(server.get_shader_workspace_ui_state("a" * 32))
    assert result["target"]["found"] is False
    assert result["target"]["visibleInWorkspaceTree"] is False
    assert result["applied"] is False
