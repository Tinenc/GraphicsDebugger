import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.shader_overview_delivery import display_receipt, observe_display
from src.shader_workspace.semantic_overview import prepare_reviewed_overview, save_overview, overview_digest


def fixture_data(tmp_path, source="float MainPS(float x) { return x; }\n"):
    root = tmp_path / "shader"
    root.mkdir()
    (root / "source.hlsl").write_bytes(source.encode())
    (root / "manifest.json").write_text(json.dumps({"source": {"path": "source.hlsl"}, "entryPoint": "MainPS"}))
    sha = hashlib.sha256(source.encode()).hexdigest()
    overview = prepare_reviewed_overview(source, {
        "stages": [{"id": "output", "title": "Copy input", "anchors": [{"function": "MainPS"}]}],
        "review": {"source_sha256": sha, "granularity_evidence": "The only operation copies the input; no other calculation is hidden.",
                   "data_flow_evidence": "The input flows directly to the output without branches.", "remaining_work": [],
                   "scope_mappings": [{"scope_id": "copy", "stage_ids": ["output"],
                                       "source_functions": ["MainPS"], "evidence": "The sole return copies x."}]}}, "MainPS")
    save_overview(root, overview)
    ui = {"selectedWorkspaceId": "w", "captureBinding": {"captureSessionId": "s"},
          "workspaces": [{"workspaceId": "w", "workspacePath": str(root), "visibleInWorkspaceTree": True,
                          "logicalShaderId": "logical-original", "shaderRawSha256": "a" * 64,
                          "captureApi": "Vulkan", "captureId": "capture", "stage": "fragment", "entryPoint": "MainPS",
                          "editorEligible": True, "openInTab": True, "projectSourceIdentity": {"knowledgeId": "k"}}],
          "documents": [{"workspaceId": "w", "available": True, "dirty": False, "readOnly": False,
                         "loadedSourceSha256": sha, "sourceSha256": sha,
                         "stageOutlineState": {"state": "reviewed", "complete": True,
                                               "source": "semantic-overview", "unresolvedCount": 0},
                         "stageOutlineCurrent": True, "stageOutlinePending": False,
                         "stageOutlineSourceSha256": overview["normalizedSourceSha256"],
                         "stageOutlineOverviewSha256": overview_digest(overview),
                         "stageOutlineStageIds": ["output"]}]}
    return root, sha, overview, ui


@pytest.mark.parametrize("source", ["float MainPS(float x) { return x; }\n",
                                    "float MainPS(float x) { return x; }\r\n",
                                    "vec4 MainPS(vec4 x) { return x; }\n"])
def test_reviewed_display_accepts_exact_projection_without_a_stage_count_minimum(tmp_path, source):
    root, sha, overview, ui = fixture_data(tmp_path, source)
    receipt = display_receipt(str(root), sha, ui)
    assert receipt["complete"] and receipt["state"] == "reviewed" and receipt["stageCount"] == 1
    assert receipt["overviewSha256"] == overview_digest(overview)


@pytest.mark.parametrize("damage", ["missing", "provisional", "wrong-digest", "wrong-source", "missing-row",
                                     "wrong-workspace", "unselected", "stale", "dirty", "pending", "unresolved"])
def test_source_review_does_not_authorize_missing_or_wrong_ui_directory(tmp_path, damage):
    root, sha, _overview, ui = fixture_data(tmp_path)
    doc = ui["documents"][0]
    if damage == "missing": doc.pop("stageOutlineState")
    elif damage == "provisional": doc["stageOutlineState"].update(state="provisional", complete=False)
    elif damage == "wrong-digest": doc["stageOutlineOverviewSha256"] = "f" * 64
    elif damage == "wrong-source": doc["stageOutlineSourceSha256"] = "f" * 64
    elif damage == "missing-row": doc["stageOutlineStageIds"] = []
    elif damage == "wrong-workspace": ui["workspaces"][0]["workspacePath"] = str(tmp_path / "other")
    elif damage == "unselected": ui["selectedWorkspaceId"] = "other"
    elif damage == "stale": doc["stageOutlineCurrent"] = False
    elif damage == "dirty": doc["dirty"] = True
    elif damage == "pending": doc.update(stageOutlineCurrent=False, stageOutlinePending=True)
    elif damage == "unresolved": doc["stageOutlineState"]["unresolvedCount"] = 1
    receipt = display_receipt(str(root), sha, ui)
    assert not receipt["complete"] and receipt["recompileRequired"] is False


def test_complete_source_alone_cannot_replace_independent_directory_review(tmp_path):
    root, sha, overview, ui = fixture_data(tmp_path)
    overview.pop("analysisReview")
    save_overview(root, overview)
    result = display_receipt(str(root), sha, ui)
    assert not result["complete"] and result["nextAction"] == "update-semantic-overview-without-recompiling"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("duplicate", ["canonical", "dirty", "retained", "different-shader", "different-api"])
def test_canonical_tree_uniqueness_uses_original_shader_not_source_version(tmp_path, legacy, duplicate):
    root, sha, _overview, ui = fixture_data(tmp_path)
    extra = dict(ui["workspaces"][0], workspaceId="old", workspacePath=str(tmp_path / "old-glsl"),
                 projectSourceIdentity={"knowledgeId": "old-knowledge"})
    ui["workspaces"].append(extra)
    if legacy:
        for row in ui["workspaces"]:
            row.pop("logicalShaderId")
    if duplicate == "dirty": ui["documents"].append({"workspaceId": "old", "dirty": True})
    elif duplicate == "retained": extra["retainedWorkingCopy"] = True
    elif duplicate == "different-shader": extra.update(logicalShaderId="other", shaderRawSha256="b" * 64)
    elif duplicate == "different-api": extra.update(logicalShaderId="other-api", captureApi="OpenGL")
    result = display_receipt(str(root), sha, ui)
    assert result["complete"] is (duplicate != "canonical")
    if duplicate == "canonical":
        assert result["duplicateCanonicalWorkspaceIds"] == ["w", "old"]


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_finish_readback_verifies_display_and_only_waits_for_matching_pending_projection(tmp_path, monkeypatch, line_ending):
    from src import server
    root, sha, overview, ui = fixture_data(tmp_path, "float MainPS(float x) { return x; }" + line_ending)
    ui["documents"][0]["sourceSha256"] = overview["normalizedSourceSha256"]
    pending = copy.deepcopy(ui)
    pending["documents"][0].update(stageOutlineCurrent=False, stageOutlinePending=True)
    responses = iter([pending, ui])
    calls = []
    def call(name, params, **kwargs):
        calls.append(name)
        assert name == "get_shader_workspace_ui_state"
        return next(responses)
    monkeypatch.setattr(server.ipc, "call", call)
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
    preview = {"shown": True, "sourceSha256": sha, "readOnly": False, "workspaceId": "w", "captureSessionId": "s"}
    result = server._finished_shader_ui_readback(preview, sha, {"result": {"knowledge": {"knowledgeId": "k"}}}, workspace_path=str(root))
    assert result["ok"] and result["semanticOverviewDelivery"]["complete"]
    assert len(calls) == 2


def test_failed_finish_display_obligation_survives_and_ui_read_repairs_without_finish(tmp_path, monkeypatch):
    from src import shader_organization_review
    root, sha, overview, ui = fixture_data(tmp_path)
    targets = {"key": {"workspacePath": str(root), "organizationId": "o", "sourceWorkObserved": True}}
    arguments = {"workspace_path": str(root), "organization_id": "o", "show_in_workspace": True}
    shader_organization_review.track_result(targets, "finish_shader_workspace_organization", {"ok": False}, arguments, False)
    assert targets["key"]["semanticOverviewDisplayRequired"]
    assert not targets["key"]["semanticOverviewDisplay"]["complete"]
    monkeypatch.setattr(shader_organization_review, "source_snapshot", lambda *args: {"sourceSha256": sha})
    observe_display(targets, "get_shader_workspace_ui_state", {"ui": ui}, {})
    assert targets["key"]["semanticOverviewDisplay"]["complete"]
    assert targets["key"]["semanticOverviewDisplay"]["overviewSha256"] == overview_digest(overview)


def test_task_complete_refuses_missing_overview_or_requested_display(tmp_path, monkeypatch):
    from src.task_report import TaskReportStore
    from src import shader_organization_review
    store = TaskReportStore("overview", tmp_path / "events")
    store.begin_task_review("a" * 64)
    row = {"sourceWorkObserved": True, "semanticOverviewComplete": False}
    monkeypatch.setattr(shader_organization_review, "review_state", lambda targets: {"targets": [row]})
    criteria = [{"requirement": "Deliver the organized shader", "status": "satisfied", "evidence": "Source reviewed"}]
    with pytest.raises(ValueError, match="calculation directory is incomplete"):
        store.record_task_review("complete", criteria, [], [])
    row.update(semanticOverviewComplete=True, semanticOverviewDisplayRequired=True, semanticOverviewDisplayComplete=False)
    with pytest.raises(ValueError, match="UI delivery is incomplete"):
        store.record_task_review("complete", criteria, [], [])
    row["semanticOverviewDisplayComplete"] = True
    assert store.record_task_review("complete", criteria, [], [])["verdict"] == "complete"


def test_legacy_source_only_task_does_not_acquire_a_ui_display_obligation(tmp_path):
    targets = {"key": {"workspacePath": str(tmp_path), "organizationId": "o"}}
    observe_display(targets, "finish_shader_workspace_organization", {"ok": True},
                    {"workspace_path": str(tmp_path), "organization_id": "o"})
    assert "semanticOverviewDisplayRequired" not in targets["key"]


def test_finish_missing_overview_after_source_review_reports_metadata_only_repair(monkeypatch):
    from src import server
    from src.shader_organization_review import publication_review
    from test_shader_organization import ShaderOrganizationTests
    from test_shader_finish_review_sequence import assess
    fixture = ShaderOrganizationTests(methodName="runTest")
    fixture.setUp()
    try:
        opened = fixture._begin()
        assess(fixture, opened)
        (fixture.root / "ai/semantic-overview.json").unlink()
        state = publication_review(str(fixture.root), opened["organizationId"])
        assert not state["ready"] and state["metadataOnlyRepair"]
        def forbidden():
            raise AssertionError("Missing directory review must be rejected before any compile manager")
        monkeypatch.setattr(server, "_shader_organization_manager", forbidden)
        result = json.loads(server.finish_shader_workspace_organization(str(fixture.root), opened["organizationId"],
                            source_review_policy="strict"))
        assert not result["ok"] and not result["applied"]
        assert result["delivery"]["nextAction"] == "update-semantic-overview-without-recompiling"
        assert result["delivery"]["recompileRequired"] is False
    finally:
        fixture.tearDown()
        fixture.doCleanups()
