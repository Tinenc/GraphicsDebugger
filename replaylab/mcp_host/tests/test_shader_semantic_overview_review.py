"""Independent directory assessment is required, but no stage count is imposed."""
import copy
import hashlib
import json
from threading import RLock
from types import SimpleNamespace
from pathlib import Path

import pytest

from src.shader_workspace.semantic_overview import (
    prepare_overview, prepare_reviewed_overview, reviewed_overview_state, save_overview,
)


def semantic_assessment(snapshot, scopes, entry="MainPS"):
    return {"stages": [{"id": "output", "title": "Constant output", "anchors": [{"function": entry}]}],
            "review": {"source_sha256": snapshot["sourceSha256"],
                       "granularity_evidence": "This fixture only forwards a scalar/constant to output; helpers have no independent rendering effects.",
                       "data_flow_evidence": "The entry returns its constant or calls the scalar helper; no independent temporal or material stages exist.",
                       "scope_mappings": [{"scope_id": row["id"], "stage_ids": ["output"],
                                           "evidence": row["evidence"]} for row in scopes["results"]],
                       "remaining_work": []}}


def prepared_fixture(snapshot, scopes, entry="MainPS"):
    source = Path(snapshot["sourcePath"]).read_bytes().decode("utf-8")
    result = prepare_reviewed_overview(source, semantic_assessment(snapshot, scopes, entry), entry, scopes)
    save_overview(Path(snapshot["workspacePath"]), result)
    return result


SOURCE = "float MainPS(float x) { return x; }\n"


def payload():
    return {"stages": [{"id": "value", "title": "Copy value", "anchors": [{"function": "MainPS"}]}],
            "review": {"source_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
                       "granularity_evidence": "Only one direct value copy exists; there are no hidden calculations in called functions.",
                       "data_flow_evidence": "The captured input x becomes the returned output without branches.",
                       "scope_mappings": [{"scope_id": "copy", "stage_ids": ["value"],
                                           "source_functions": ["MainPS"], "evidence": "The sole return copies x."}],
                       "remaining_work": []}}


def test_legacy_navigation_is_not_independent_analysis_and_one_simple_stage_is_valid():
    old = prepare_overview(SOURCE, payload()["stages"], "MainPS")
    assert reviewed_overview_state(SOURCE, old)["state"] == "unreviewed"
    reviewed = prepare_reviewed_overview(SOURCE, payload(), "MainPS")
    assert reviewed_overview_state(SOURCE, reviewed)["complete"]
    assert len(reviewed["stages"]) == 1


def test_reviewed_v1_without_optional_history_keeps_its_receipt():
    legacy = prepare_reviewed_overview(SOURCE, payload(), "MainPS")
    legacy.pop('navigationBodies')
    receipt = reviewed_overview_state(SOURCE, legacy)
    assert receipt['complete']
    assert reviewed_overview_state(SOURCE, legacy, receipt)['complete']
    damaged = prepare_reviewed_overview(SOURCE, payload(), "MainPS")
    next(iter(damaged['navigationBodies'].values())).append('wrong')
    assert reviewed_overview_state(SOURCE, damaged)['state'] == 'invalid'


@pytest.mark.parametrize("change", ["hash", "granularity", "flow", "mapping", "stage", "remaining"])
def test_missing_or_stale_independent_evidence_cannot_be_published(change):
    value = payload()
    if change == "hash": value["review"]["source_sha256"] = "0" * 64
    elif change == "granularity": value["review"]["granularity_evidence"] = ""
    elif change == "flow": value["review"]["data_flow_evidence"] = ""
    elif change == "mapping": value["review"]["scope_mappings"] = []
    elif change == "stage": value["review"]["scope_mappings"][0]["stage_ids"] = ["nonexistent"]
    else: value["review"]["remaining_work"] = ["Analyze another helper"]
    with pytest.raises(ValueError):
        prepare_reviewed_overview(SOURCE, value, "MainPS")


def test_same_source_directory_edit_invalidates_receipt_and_source_edit_requires_reanalysis():
    value = prepare_reviewed_overview(SOURCE, payload(), "MainPS")
    receipt = reviewed_overview_state(SOURCE, value)
    changed = copy.deepcopy(value)
    changed["stages"][0]["title"] = "Different meaning"
    assert not reviewed_overview_state(SOURCE, changed, receipt)["complete"]
    assert reviewed_overview_state("\n" + SOURCE, value)["state"] == "stale"


def test_ui_marks_coarse_fallback_provisional_and_current_analysis_reviewed():
    from test_shader_stage_outline import Hierarchy, outline
    index = Hierarchy(SOURCE)
    assert outline.outline_state(index, "MainPS")["complete"] is False
    value = prepare_reviewed_overview(SOURCE, payload(), "MainPS")
    assert outline.outline_state(index, "MainPS", value)["state"] == "reviewed"
    assert outline.outline_state(Hierarchy("\n" + SOURCE), "MainPS", value)["state"] == "stale"


def test_scope_ownership_covers_helpers_without_promoting_each_helper_to_stage():
    source = "float Helper(float x) { return x; }\nfloat MainPS(float x) { return Helper(x); }\n"
    value = payload()
    value["review"]["source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
    with pytest.raises(ValueError, match="omits source functions"):
        prepare_reviewed_overview(source, value, "MainPS")
    value["review"]["scope_mappings"].append({"scope_id": "helper", "stage_ids": ["value"],
        "source_functions": ["Helper"], "disposition": "supporting", "evidence": "Helper only forwards the same scalar."})
    assert len(prepare_reviewed_overview(source, value, "MainPS")["stages"]) == 1


def test_metadata_only_repair_preserves_source_and_never_contacts_compiler_or_replay(tmp_path, monkeypatch):
    from src import server
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "source.hlsl").write_bytes(SOURCE.encode())
    (root / "manifest.json").write_text(json.dumps({"source": {"path": "source.hlsl"}, "entryPoint": "MainPS"}))
    monkeypatch.setattr(server, "_workspace_root_for_files", lambda path: root)
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: SimpleNamespace(_guard=RLock()))
    monkeypatch.setattr(server, "_active_project_service", lambda: SimpleNamespace(status=lambda: {}))
    def forbidden(*args, **kwargs):
        raise AssertionError("Metadata-only operations must not compile, replay or prepare Shader source")
    monkeypatch.setattr(server, "_shader_workspace_service", forbidden)
    monkeypatch.setattr(server, "ipc", SimpleNamespace(request=forbidden, call=forbidden))
    context = json.loads(server.get_shader_semantic_overview_context(str(root)))
    assert context["functions"][0]["name"] == "MainPS" and context["overviewState"]["state"] == "missing"
    (root / "ai").mkdir()
    (root / "ai/semantic-overview.json").write_text("{old interrupted metadata")
    repair_context = json.loads(server.get_shader_semantic_overview_context(str(root)))
    assert repair_context["metadataDiagnostics"][0]["artifact"] == "overview"
    result = json.loads(server.update_shader_semantic_overview(str(root), context["sourceSha256"], payload()))
    assert result["sourceUnchanged"] and result["stageCount"] == 1
    assert result["overviewPublication"] == {"updatedLocal": True, "projectShared": False,
                                              "projectStatus": "skipped", "reason": "no-active-project"}
    assert (root / "source.hlsl").read_bytes() == SOURCE.encode()
    context = json.loads(server.get_shader_semantic_overview_context(str(root)))
    assert context["overviewState"]["complete"]
    original = (root / "ai/semantic-overview.json").read_bytes()
    (root / "source.hlsl").write_bytes((SOURCE + "// edit\n").encode())
    failure = json.loads(server.update_shader_semantic_overview(str(root), result["sourceSha256"], payload()))
    assert "changed" in failure["error"]
    assert (root / "ai/semantic-overview.json").read_bytes() == original


def test_metadata_share_uses_same_source_and_active_project_without_registration(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from src import server
    project = tmp_path / "project"
    root = project / "workspaces/shaders/current"
    root.mkdir(parents=True)
    (root / "source.hlsl").write_bytes(SOURCE.encode())
    overview = prepare_reviewed_overview(SOURCE, payload(), "MainPS")
    overview["originalRawSha256"] = "a" * 64
    save_overview(root, overview)
    status = {"projectRoot": str(project), "workspaceId": "project",
              "paths": {"shaderWorkspaces": str(project / "workspaces/shaders")}}
    monkeypatch.setattr(server, "_active_project_service", lambda: SimpleNamespace(
        status=lambda: status, pin_active_project=lambda *args: nullcontext()))
    result = server._publish_semantic_overview_metadata(root, overview, "source.hlsl")
    assert result["projectShared"]
    target = project / "derived/shader-overviews" / ("a" * 64) / (overview["sourceSha256"] + ".json")
    assert json.loads(target.read_text("utf-8")) == overview
    (root / "source.hlsl").write_bytes(b"changed")
    assert not server._publish_semantic_overview_metadata(root, overview, "source.hlsl")["projectShared"]
