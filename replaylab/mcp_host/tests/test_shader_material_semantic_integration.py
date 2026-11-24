from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src import server
from src.material_naming import parse_material_parameters
from src.shader_asset_retrieval import ShaderAssetResolutionStore
from src.shader_asset_semantics import build_material_semantic_context, with_agent_hypotheses


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    first, second = root / "workspaces/first", root / "workspaces/second"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    assets = tmp_path / "assets"
    assets.mkdir()
    dictionaries = root / "shared/dictionaries"
    database = dictionaries / "assets" / server.SHADER_ASSET_RESOLUTIONS_FILENAME
    manifest = {"workspaceId": "project-a", "assetRoot": str(assets)}
    forbidden = lambda *a, **k: pytest.fail("local semantic preparation must not query replay or scan assets")
    service = SimpleNamespace(
        _bound_project_snapshot=lambda: SimpleNamespace(project_root=root, manifest_json=json.dumps(manifest)),
        _paths=lambda unused: {"sharedDictionaries": str(dictionaries)},
        status=forbidden, _live_active=forbidden, _read_selection=forbidden)
    monkeypatch.setattr(server, "_active_project_service", lambda: service)
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=forbidden))
    monkeypatch.setattr(server, "_shader_asset_capture_features", forbidden)
    monkeypatch.setattr(server, "_ensure_shader_asset_catalog", forbidden)
    monkeypatch.setattr(server, "_asset_evidence_binding_is_current", forbidden)
    monkeypatch.setattr(server, "_seal_resolution_evidence", lambda unused, evidence: evidence)
    identity = {"captureId": "capture-a", "captureSessionId": "session-a", "eventId": 61523,
                "stage": "fragment", "entryPoint": "MainPS", "shaderResourceId": "754", "rawSha256": "a" * 64}
    binding = {"assetRoot": str(assets), "assetRelativePath": "Materials/Wet.mat",
               "assetContentSha256": "b" * 64, "assetEvidenceSha256": "c" * 64, "catalogSha256": "d" * 64}
    material = parse_material_parameters({"name": "Wet", "scalars": {"Wetness": 0.9573, "RainEnabled": 1}})
    context = build_material_semantic_context(material, identity=identity, project_id="project-a",
        asset_binding=binding, member_candidates=[{"parameter": "Wetness", "bind": 3, "space": 0, "byteOffset": 12}])
    return SimpleNamespace(root=root, first=first, second=second, assets=assets, manifest=manifest,
        service=service, identity=identity, binding=binding, material=material, context=context, database=database)


def remember(project, context=None):
    store = ShaderAssetResolutionStore(project.database)
    evidence = {"shaderIdentity": project.identity, "assetBinding": project.binding}
    if context is not None:
        evidence["materialSemanticContext"] = context
    return store.record(game_key=server._shader_asset_game_key(project.manifest, project.assets),
        shader_raw_sha256="a" * 64, structural="shape", stage="fragment", entry_point="MainPS",
        asset_relative_path="Materials/Wet.mat", asset_content_sha256="b" * 64, asset_class="Material",
        catalog_sha256="d" * 64, negative=False, confidence=0.85, decided_by="ai",
        rationale="distinctive texture and material reference agree", evidence=evidence)


def prepare(project, remembered, target=None, workspace=None):
    return server._shader_material_context_preparation(workspace or project.second, target or project.identity,
        project.service, project.root, project.manifest, remembered)


def read_context(result):
    return json.loads(Path(result["materialSemanticContextPath"]).read_text(encoding="utf-8"))["materialSemanticContext"]


def test_partial_proposal_ready_false_keeps_unlocated_dictionary_and_agent_notes(project, monkeypatch):
    calls = []

    def call(method, params, **unused):
        calls.append(method)
        if method == "get_bound_textures":
            return []
        assert method == "get_constant_buffer_values"
        return {"constantBuffers": [{"bind": 3, "space": 0, "floats": [0, 0.9573]}]}

    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=call))
    monkeypatch.setattr(server, "_shader_asset_authorization_identity", lambda *args: {
        "identity": project.identity, "current": False, "resolvedAssets": []})
    monkeypatch.setattr(server, "_workspace_declarations", lambda unused: {"available": False})
    result = json.loads(asyncio.run(server.propose_shader_names_from_asset(
        61523, workspace_path=str(project.first), material_json=json.dumps({"scalars": {
            "Wetness": 0.9573, "RainEnabled": 1}}), fill_gaps=False,
        semantic_notes_json=json.dumps([{"parameter": "RainEnabled", "role": "rain branch selector",
            "reason": "the source gates droplet calculation", "verified": True}]))))
    assert "error" not in result, result
    assert result["ready"] is False and result["cbufferAliasRows"] == []
    context = result["materialSemanticContext"]
    assert {row["name"] for row in context["parameterDictionary"]} == {"Wetness", "RainEnabled"}
    assert "RainEnabled" in context["unlocatedParameterNames"]
    assert context["agentHypotheses"][0]["status"] == "agent-inferred"
    assert context["candidateCorrespondences"][0]["byteOffset"] == 4
    assert context["authority"]["physicalAliasAuthority"] is False
    assert read_context(result) == context
    assert calls == ["get_constant_buffer_values", "get_bound_textures"]


def test_local_mirror_reuse_does_not_lookup_database_or_read_live_capture(project, monkeypatch):
    remember(project, project.context)
    monkeypatch.setattr(server.shader_asset_retrieval, "ShaderAssetResolutionStore",
        lambda *a, **k: pytest.fail("usable local semantic context must avoid another store lookup"))
    result = prepare(project, {"materialSemanticContext": project.context})
    assert result["materialSemanticContextSummary"]["reuse"]["scope"] == "shader-context-only"
    assert read_context(result)["candidateCorrespondences"] == []


def test_shared_context_enrichment_reaches_new_rdc_workspace_without_retrieval(project):
    row = remember(project)
    selected = {"resolutionId": row["resolutionId"], "assetRelativePath": row["assetRelativePath"],
                "assetBinding": project.binding}
    delivered = server._material_semantic_delivery(project.material, str(project.first), 61523, "fragment",
        row["assetRelativePath"], {"identity": project.identity}, selected,
        {"proposals": [{"parameter": "Wetness", "byteOffset": 12, "bind": 3, "space": 0}]},
        {"proposals": []}, [])
    assert "materialSemanticContextDiagnostics" not in delivered
    target = {**project.identity, "captureId": "capture-b", "captureSessionId": "session-b",
              "eventId": 11402, "shaderResourceId": "728"}
    result = prepare(project, {}, target)
    reused = read_context(result)
    assert reused["reuse"]["scope"] == "shader-context-only"
    assert {item["name"] for item in reused["parameterDictionary"]} == {"Wetness", "RainEnabled"}
    assert reused["candidateCorrespondences"] == []
    store = ShaderAssetResolutionStore(project.database)
    memory = store.lookup(server._shader_asset_game_key(project.manifest, project.assets), "a" * 64,
                          "shape", "fragment", "MainPS")
    assert memory["resolution"]["resolutionId"] == row["resolutionId"]
    assert memory["resolution"]["evidence"]["materialSemanticContext"]["candidateCorrespondences"]


def test_stale_local_mirror_does_not_hide_valid_shared_context(project):
    remember(project, project.context)
    wrong = build_material_semantic_context(parse_material_parameters({"scalars": {"Old": 0.5}}),
        identity={**project.identity, "rawSha256": "f" * 64}, project_id="project-a", asset_binding=project.binding)
    result = prepare(project, {"materialSemanticContext": wrong})
    assert result["materialSemanticContextSummary"]["parameterCount"] == 2
    assert read_context(result)["binding"]["shaderRawSha256"] == "a" * 64


@pytest.mark.parametrize("change", ["raw", "root"])
def test_changed_root_or_raw_cannot_reuse_shared_context_as_current(project, change):
    remember(project, project.context)
    target = dict(project.identity)
    if change == "raw":
        target["rawSha256"] = "e" * 64
    else:
        project.manifest["assetRoot"] = str(project.root / "other-game-assets")
    result = prepare(project, {"materialSemanticContext": project.context}, target)
    assert result["materialSemanticContextSummary"]["reuse"]["scope"] == "stale"
    assert result["materialSemanticContextSummary"]["parameterCount"] == 0
    assert "materialSemanticContextPath" not in result


def test_same_raw_shader_different_event_does_not_inherit_candidate_offsets(project):
    result = prepare(project, {"materialSemanticContext": project.context},
                     {**project.identity, "eventId": 61408})
    reused = read_context(result)
    assert reused["candidateCorrespondences"] == []
    assert reused["parameterDictionary"]
    assert not reused["reuse"]["drawMaterialAssociationConfirmed"]


def test_delivery_outside_active_project_returns_vocabulary_without_writing_foreign_workspace(project, tmp_path):
    outside = tmp_path / "another-project/shader"
    outside.mkdir(parents=True)
    result = server._material_semantic_delivery(project.material, str(outside), 61523, "fragment",
        "Materials/Wet.mat", {"identity": project.identity}, None, {"proposals": []}, {"proposals": []}, [])
    assert result["materialSemanticContextSummary"]["parameterCount"] == 2
    assert result["materialSemanticContext"]["binding"]["projectId"] == ""
    assert "materialSemanticContextPath" not in result
    assert not (outside / "ai/asset-resolution.json").exists()
    assert "outside the active project" in result["materialSemanticContextDiagnostics"][0]


def test_agent_notes_preserve_identity_and_cannot_escalate_alias_authority(project):
    old = with_agent_hypotheses(project.context, [{"parameter": "Wetness", "role": "roughness blend",
        "suggestedName": "wetSurface", "reason": "earlier reading"}])
    result = with_agent_hypotheses(old, [{"parameter": "Wetness", "role": "roughness blend",
        "suggestedName": "surfaceWetness", "reason": "confirmed data flow through blend",
        "physicalAliasAuthority": True, "status": "verified", "byteOffset": 128}])
    assert result["binding"] == old["binding"]
    assert result["parameterDictionary"] == old["parameterDictionary"]
    assert len(result["agentHypotheses"]) == 1
    note = result["agentHypotheses"][0]
    assert note["suggestedName"] == "surfaceWetness"
    assert note["status"] == "agent-inferred" and not note["physicalAliasAuthority"]
    assert "byteOffset" not in note
    assert old["agentHypotheses"][0]["suggestedName"] == "wetSurface"


@pytest.mark.parametrize("notes", ["not an array", {"parameter": "Wetness"}, [1], [None]])
def test_agent_note_shape_is_bounded_and_not_arbitrary_json(project, notes):
    with pytest.raises(ValueError, match="array of objects"):
        with_agent_hypotheses(project.context, notes)


@pytest.mark.parametrize("asset_case", ["same", "different-path", "changed-content"])
def test_public_submit_reuses_parameter_context_only_for_the_selected_material(project, monkeypatch, asset_case):
    server._write_workspace_asset_resolution(project.first, {
        "shaderRawSha256": "a" * 64, "stage": "fragment", "eventId": 61523,
        "captureId": "capture-a", "assetBinding": project.binding,
        "materialSemanticContext": project.context})
    selected_path = "Materials/Dry.mat" if asset_case == "different-path" else "Materials/Wet.mat"
    content_sha = "e" * 64 if asset_case == "changed-content" else "b" * 64
    selected_binding = {**project.binding, "assetRelativePath": selected_path, "assetContentSha256": content_sha}
    selected_asset = {"assetId": "chosen-material", "relativePath": selected_path,
        "contentSha256": content_sha, "metadata": {"className": "Material"}}
    catalog = Mock()
    catalog.query.return_value = {"assets": [selected_asset]}
    capture_features = Mock(return_value=(project.identity, {"structuralKey": "shape", "boundTextureNames": []}))
    reader = AsyncMock(side_effect=AssertionError("submit must reuse parsed material context without rereading it"))
    calls = []

    def call(method, params, **unused):
        calls.append(method)
        assert method == "release_texture_fingerprint_cache"
        return {"released": True}

    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=call))
    monkeypatch.setattr(server, "_shader_asset_capture_features", capture_features)
    monkeypatch.setattr(server, "_read_material_asset", reader)
    monkeypatch.setattr(server, "_active_asset_catalog_context", lambda **unused: (
        project.service, project.manifest, project.assets, project.database.parent / "catalog.sqlite3"))
    monkeypatch.setattr(server, "AssetCatalogService", lambda unused: catalog)
    monkeypatch.setattr(server, "_asset_resolution_evidence_binding", lambda *unused: selected_binding)
    monkeypatch.setattr(server, "_seal_asset_content_proof", lambda proof: proof)
    monkeypatch.setattr(server.shader_asset_retrieval, "_material_document", lambda *unused: {})
    receipt = json.loads(server.submit_shader_asset_resolution(61523, "fragment", "chosen-material", 0.85,
        "Distinctive bound texture and material reference agree", workspace_path=str(project.first),
        links_json=json.dumps({"materialHypotheses": [{"parameter": "RainEnabled", "role": "rain branch selector",
            "reason": "controls droplet calculation", "verified": True, "physicalAliasAuthority": True}]})))
    assert "error" not in receipt, receipt
    reader.assert_not_called()
    capture_features.assert_called_once()
    catalog.hydrate.assert_not_called()
    assert calls == ["release_texture_fingerprint_cache"]
    persisted = server._read_workspace_asset_resolution(project.first)
    evidence = receipt["resolution"]["evidence"]
    if asset_case == "same":
        summary = receipt["materialSemanticContextSummary"]
        assert summary["parameterCount"] == 2 and summary["hypothesisCount"] == 1
        assert summary["reuse"]["scope"] == "current-draw"
        assert Path(receipt["materialSemanticContextPath"]).is_file()
        assert evidence["materialSemanticContext"] == persisted["materialSemanticContext"]
        candidates = evidence["materialSemanticContext"]["candidateCorrespondences"]
        assert candidates[0]["byteOffset"] == 12
        assert candidates[0]["physicalAliasAuthority"] is False
        note = evidence["materialSemanticContext"]["agentHypotheses"][0]
        assert note["status"] == "agent-inferred" and note["physicalAliasAuthority"] is False
    else:
        assert "materialSemanticContext" not in evidence
        assert "materialSemanticContextSummary" not in receipt
        assert "materialSemanticContextPath" not in receipt
        assert not persisted.get("materialSemanticContext"), "different material bytes must not inherit the old parameter dictionary"
