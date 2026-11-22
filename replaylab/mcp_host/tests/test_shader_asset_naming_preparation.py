from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import server


@pytest.fixture
def naming_project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    workspace = root / "workspaces" / "shader"
    workspace.mkdir(parents=True)
    assets = tmp_path / "unpacked"
    assets.mkdir()
    dictionaries = root / "shared" / "dictionaries"
    (dictionaries / "assets").mkdir(parents=True)
    manifest = {"workspaceId": "1" * 32, "assetRoot": str(assets)}
    calls = []

    def local_context(*, validate_shared_artifacts):
        assert validate_shared_artifacts is False
        calls.append("local-configuration")
        return root, manifest

    def forbidden(*args, **kwargs):
        pytest.fail("Naming preparation must not scan, hash resources, validate replay or acquire live data")

    service = SimpleNamespace(
        _bound_project_snapshot=lambda: None,
        _live_active=local_context,
        _read_selection=lambda: {"projectRoot": str(root), "workspaceId": manifest["workspaceId"]},
        _paths=lambda unused: {"sharedDictionaries": str(dictionaries)},
        status=forbidden,
    )
    monkeypatch.setattr(server, "_active_project_service", lambda: service)
    monkeypatch.setattr(server, "_shader_asset_capture_features", forbidden)
    monkeypatch.setattr(server, "_shader_asset_memory", forbidden)
    monkeypatch.setattr(server, "_asset_evidence_binding_is_current", forbidden)
    monkeypatch.setattr(server, "_ensure_shader_asset_catalog", forbidden)
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=forbidden))
    context = {"stage": "fragment", "entryPoint": "MainPS", "rawSha256": "a" * 64,
               "captureId": "c" * 64}
    return SimpleNamespace(root=root, workspace=workspace, assets=assets,
                           dictionaries=dictionaries, manifest=manifest,
                           service=service, context=context, calls=calls)


def _write_record(project, **changes):
    path = project.workspace / "ai" / "asset-resolution.json"
    path.parent.mkdir(exist_ok=True)
    value = {
        "eventId": 11402, "stage": "fragment", "shaderRawSha256": "a" * 64,
        "assetBinding": {"assetRoot": str(project.assets)},
        "resolvedAssets": [{"assetRelativePath": "M_Wet.uasset", "links": {"textures": {"t0": "T_Wet"}}}],
    }
    value.update(changes)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _write_reader(project, reader="uex"):
    path = project.dictionaries / "assets" / "asset-reader-selection.json"
    path.write_text(json.dumps({
        "assetRoot": str(project.assets), "engine": "unreal",
        "selections": {"unreal": {"properties": {"reader": reader},
                                      "references": {"reader": reader}}},
    }), encoding="utf-8")
    return path


def test_begin_naming_facts_reuse_local_leads_without_claiming_fresh_or_applied(naming_project):
    project = naming_project
    record_path = _write_record(project)
    reader_path = _write_reader(project)
    result = server._shader_asset_naming_preparation(str(project.workspace), project.context, 11402)
    assert result["state"] == "recorded-evidence"
    assert result["assetRoot"] == str(project.assets)
    assert result["reader"]["selectionPath"] == str(reader_path)
    assert result["reader"]["selectedReaders"] == {"properties": "uex", "references": "uex"}
    assert result["reader"]["availabilityVerified"] is False
    retained = result["retainedMapping"]
    assert retained["recordPath"] == str(record_path)
    assert retained["targetMatches"] and retained["assetRootMatches"] and retained["linksPresent"]
    assert retained["workspaceMirrorAuthority"] is False
    for field in ("contentFreshness", "sourceNamesApplied", "liveAliasesApplied"):
        assert retained[field] == "not-checked"
    assert project.calls == ["local-configuration"]
    workflow = result["namingWorkflow"]
    assert workflow["retainedContextIsPublishedNames"] is False
    assert workflow["partialAssetMatchesUsable"] is True
    assert workflow["phases"][-1] == "reconcile-cbuffer-names-and-semantic-fields"
    assert workflow["panelGroupingArgument"] == "semantic_fields_json"
    assert workflow["recompileForPanelOnlyChanges"] is False
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 4096


@pytest.mark.parametrize("change", [
    {"eventId": 11384}, {"stage": "vertex"}, {"shaderRawSha256": "b" * 64},
    {"eventId": True}, {"captureId": "d" * 64},
])
def test_same_shader_or_nearby_event_does_not_make_mapping_current(naming_project, change):
    _write_record(naming_project, **change)
    result = server._shader_asset_naming_preparation(str(naming_project.workspace), naming_project.context, 11402)
    assert result["state"] == "needs-resolution"
    assert result["retainedMapping"]["state"] == "different-target-record"
    assert result["retainedMapping"]["targetMatches"] is False


def test_changed_asset_root_keeps_old_mapping_as_hint(naming_project):
    _write_record(naming_project, assetBinding={"assetRoot": str(naming_project.root / "old-assets")})
    result = server._shader_asset_naming_preparation(str(naming_project.workspace), naming_project.context, 11402)
    assert result["state"] == "needs-resolution"
    assert result["retainedMapping"]["targetMatches"] is True
    assert result["retainedMapping"]["assetRootMatches"] is False


def test_legacy_row_target_is_retained_without_inventing_asset_binding(naming_project):
    path = _write_record(naming_project)
    value = json.loads(path.read_text(encoding="utf-8"))
    value.pop("eventId")
    value.pop("stage")
    value.pop("assetBinding")
    value["resolvedAssets"][0].update(eventId=11402, stage="fragment")
    value["resolvedAssets"].append({"eventId": 11384, "stage": "fragment", "assetRelativePath": "other.uasset"})
    path.write_text(json.dumps(value), encoding="utf-8")
    result = server._shader_asset_naming_preparation(str(naming_project.workspace), naming_project.context, 11402)
    assert result["state"] == "recorded-evidence"
    retained = result["retainedMapping"]
    assert retained["targetMatches"] is True and retained["assetRootMatches"] is None
    assert retained["captureIdentityMatches"] is None
    assert retained["matchingTargetAssetCount"] == 1 and retained["resolvedAssetCount"] == 2
    assert retained["contentFreshness"] == "not-checked"


def test_unconfigured_root_is_a_nonblocking_fact(naming_project):
    naming_project.manifest["assetRoot"] = ""
    result = server._shader_asset_naming_preparation(str(naming_project.workspace), naming_project.context, 11402)
    assert result["state"] == "unconfigured" and result["assetRootConfigured"] is False
    assert not (naming_project.workspace / "ai").exists()


def test_saved_panel_receipt_is_available_without_asset_root_or_live_reads(naming_project):
    naming_project.manifest["assetRoot"] = ""
    path = naming_project.workspace / "ai" / "cbuffer-name-publication.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "schema": "replaylab-cbuffer-name-publication-receipt/v1",
        "operationId": "prior-publication", "applied": True, "partial": False,
        "counts": {"requestedSemanticFields": 57, "writtenSemanticFields": 57},
    }), encoding="utf-8")
    result = server._shader_asset_naming_preparation(str(naming_project.workspace), naming_project.context, 11402)
    assert result["state"] == "unconfigured"
    receipt = result["retainedPanelPublication"]
    assert receipt["recordPath"] == str(path)
    assert receipt["writtenSemanticFields"] == 57
    assert receipt["appliedAtPublication"] is True
    assert receipt["currentDisplayFreshness"] == "not-checked"


def test_unavailable_root_does_not_erase_retained_source_naming_leads(naming_project):
    _write_record(naming_project)
    naming_project.assets.rmdir()
    result = server._shader_asset_naming_preparation(str(naming_project.workspace), naming_project.context, 11402)
    assert result["state"] == "unavailable" and result["assetRootConfigured"] is True
    assert result["retainedMapping"]["state"] == "same-target-record"


def test_bad_optional_records_do_not_block_draft_preparation(naming_project):
    path = _write_record(naming_project)
    path.write_text("invalid json", encoding="utf-8")
    reader_path = _write_reader(naming_project)
    reader_path.write_bytes(b"x" * (1024 * 1024 + 1))
    result = server._shader_asset_naming_preparation(str(naming_project.workspace), naming_project.context, 11402)
    assert result["state"] == "needs-resolution"
    assert result["reader"]["state"] == result["retainedMapping"]["state"] == "unavailable"
    assert len(result["diagnostics"]) == 2


def test_bound_project_snapshot_avoids_reloading_project_configuration(naming_project):
    project = naming_project
    project.service._bound_project_snapshot = lambda: SimpleNamespace(
        project_root=project.root, manifest_json=json.dumps(project.manifest))
    result = server._shader_asset_naming_preparation(str(project.workspace), project.context, 11402)
    assert result["state"] == "needs-resolution" and project.calls == []


def test_changed_project_selection_is_diagnostic_not_wrong_project_context(naming_project):
    project = naming_project
    project.service._read_selection = lambda: {"projectRoot": str(project.root), "workspaceId": "2" * 32}
    result = server._shader_asset_naming_preparation(str(project.workspace), project.context, 11402)
    assert result["state"] == "unknown" and result["assetRootConfigured"] is None
    assert "changed" in result["diagnostics"][0]


def test_large_optional_reader_details_stay_bounded_without_cutting_paths(naming_project):
    _write_reader(naming_project, reader="reader" * 2000)
    result = server._shader_asset_naming_preparation(str(naming_project.workspace), naming_project.context, 11402)
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 4096
    assert result["pathsOmitted"] is True
    assert result["reader"]["detailsOmitted"] is True
    assert "selectionPath" not in result["reader"]


@pytest.mark.parametrize("copy_to_agent", [False, True])
def test_naming_context_failure_does_not_hide_created_draft(monkeypatch, copy_to_agent):
    from test_shader_organization import ShaderOrganizationTests

    fixture = ShaderOrganizationTests()
    fixture.setUp()
    try:
        monkeypatch.setattr(server, "_shader_organization_manager", lambda: fixture.manager)
        monkeypatch.setattr(server, "_shader_organization_semantic_context", lambda *args, **kwargs: {})

        def failed_context(*args, **kwargs):
            raise OSError("optional project state unavailable")

        monkeypatch.setattr(server, "_shader_asset_naming_preparation", failed_context)
        result = json.loads(server.begin_shader_workspace_organization(
            str(fixture.root), agent_file_name="draft.hlsl" if copy_to_agent else ""))
        assert "error" not in result and result["organizationId"]
        assert Path(result["proposalSourcePath"]).is_file()
        assert result["assetNamingPreparation"]["state"] == "unknown"
        assert "optional project state unavailable" in result["assetNamingPreparation"]["diagnostics"]
        assert fixture.service.revision == 0
    finally:
        fixture.tearDown()
