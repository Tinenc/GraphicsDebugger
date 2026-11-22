from __future__ import annotations

import json
import sqlite3

import pytest

from src.material_naming import parse_material_parameters, propose_member_names
from src.shader_asset_retrieval import ShaderAssetResolutionStore, ShaderAssetRetrievalError
from src.shader_asset_semantics import (
    SCHEMA,
    assess_material_semantic_context,
    build_material_semantic_context,
    semantic_context_summary,
)


def identity(**changes):
    return {"captureId": "capture-a", "captureSessionId": "session-a", "eventId": 61523,
            "stage": "fragment", "entryPoint": "MainPS", "shaderResourceId": "754",
            "rawSha256": "a" * 64, "captureApi": "D3D12", **changes}


def asset(tmp_path, **changes):
    return {"assetRoot": str(tmp_path / "unpacked"), "assetRelativePath": "Materials/Wet.mat",
            "assetContentSha256": "b" * 64, "assetEvidenceSha256": "c" * 64,
            "catalogSha256": "d" * 64, **changes}


def context(tmp_path):
    material = parse_material_parameters({"name": "Wet", "scalars": {
        "Wetness": 0.9573, "RainEnabled": 1, "DetailBlend": 0.5},
        "vectors": {"TrailScale": [80, 60, 0, 0]},
        "textures": {"Droplets": "/Textures/Droplets"}})
    candidates = propose_member_names(material, [{"bind": 3, "space": 2,
        "floats": [0, 0.9573, 0, 0]}], fill_gaps=False)["proposals"]
    return build_material_semantic_context(material, identity=identity(), project_id="project-a",
        asset_binding=asset(tmp_path), member_candidates=candidates)


def assess(record, tmp_path, **changes):
    return assess_material_semantic_context(record, identity=identity(**changes), project_id="project-a",
                                            asset_binding=asset(tmp_path))


def test_partial_match_preserves_all_parameter_names_and_values(tmp_path):
    record = context(tmp_path)
    assert record["schema"] == SCHEMA
    parameters = {item["name"]: item for item in record["parameterDictionary"]}
    assert set(parameters) == {"Wetness", "RainEnabled", "DetailBlend", "TrailScale", "Droplets"}
    assert parameters["RainEnabled"]["value"] == 1
    assert parameters["DetailBlend"]["value"] == 0.5
    assert set(record["unlocatedParameterNames"]) == {"RainEnabled", "DetailBlend", "TrailScale", "Droplets"}
    assert record["candidateCorrespondences"][0]["byteOffset"] == 4
    assert record["candidateCorrespondences"][0]["physicalAliasAuthority"] is False
    assert record["authority"]["partialMatchBlocksOrganization"] is False


def test_serialized_vector_and_texture_transform_do_not_claim_gpu_packing(tmp_path):
    material = parse_material_parameters({"m_Name": "Wet", "sourceFile": "level0.assets",
        "m_SavedProperties": {"m_Floats": [["Wetness", 0.9573]], "m_Colors": [["Tint", {"r": 1, "g": 0.5, "b": 0, "a": 1}]],
        "m_TexEnvs": [{"Key": "Base", "Value": {"m_Texture": {"m_FileID": 0, "m_PathID": 44},
            "m_Scale": {"x": 2, "y": 3}, "m_Offset": {"x": 0.25, "y": 0.5}}}]}})
    record = build_material_semantic_context(material, identity=identity(), project_id="project-a",
                                             asset_binding=asset(tmp_path))
    parameters = {item["name"]: item for item in record["parameterDictionary"]}
    assert parameters["Tint"]["serializedComponentCount"] == 4
    assert parameters["Tint"]["compiledComponentWidthKnown"] is False
    assert parameters["Base"]["objectReference"]["pathId"] == 44
    assert parameters["Base"]["scale"] == [2, 3]
    assert "Base_ST" not in parameters


def test_candidate_and_agent_flags_cannot_promote_inference_to_physical_authority(tmp_path):
    material = parse_material_parameters({"scalars": {"Wetness": 0.9573}})
    record = build_material_semantic_context(material, identity=identity(), project_id="project-a",
        asset_binding=asset(tmp_path), member_candidates=[{"parameter": "Wetness", "byteOffset": 16,
            "relations": ["float16"], "autoApplyEligible": True, "physicalAliasAuthority": True,
            "verified": True}], agent_notes=[{"parameter": "Wetness", "sourceSymbol": "cb5_c1_x",
                "suggestedName": "surfaceWetness", "role": "wet surface mask", "reason": "used in roughness blending",
                "verified": True, "physicalAliasAuthority": True}])
    candidate = record["candidateCorrespondences"][0]
    assert candidate["relations"] == ["float16"]
    assert candidate["status"] == "candidate" and not candidate["physicalAliasAuthority"]
    assert "verified" not in candidate and "autoApplyEligible" not in candidate
    assert record["agentHypotheses"][0]["status"] == "agent-inferred"
    assert not record["agentHypotheses"][0]["physicalAliasAuthority"]


def test_current_exact_context_keeps_candidates_without_claiming_material_identity(tmp_path):
    result = assess(context(tmp_path), tmp_path)
    assert result["reuse"]["scope"] == "current-draw"
    assert result["candidateCorrespondences"]
    assert not result["reuse"]["drawMaterialAssociationConfirmed"]
    assert not result["reuse"]["physicalAliasAuthority"]


@pytest.mark.parametrize("changes", [{"captureId": "capture-b"}, {"eventId": 61408},
    {"captureSessionId": "session-b"}, {"shaderResourceId": "999"}, {"captureApi": "Vulkan"}])
def test_same_shader_in_different_draw_or_platform_keeps_only_semantic_vocabulary(tmp_path, changes):
    record = context(tmp_path)
    result = assess(record, tmp_path, **changes)
    assert result["reuse"]["scope"] == "shader-context-only"
    assert result["parameterDictionary"] == record["parameterDictionary"]
    assert not result["candidateCorrespondences"]
    assert len(result["unlocatedParameterNames"]) == 5
    assert result["coverage"]["candidateCount"] == 0
    assert result["withheldHistoricalCandidateCount"] == 1
    assert record["candidateCorrespondences"]  # Assessment does not mutate persisted history.


def test_no_fresh_asset_read_never_claims_current_draw_match(tmp_path):
    result = assess_material_semantic_context(context(tmp_path), identity=identity(), project_id="project-a",
                                             asset_root=str(tmp_path / "unpacked"))
    assert result["reuse"]["scope"] == "shader-context-only"
    assert "asset-content-freshness-not-checked" in result["reuse"]["reasons"]
    assert not result["candidateCorrespondences"]


@pytest.mark.parametrize("what", ["project", "shader", "stage", "entry", "root", "asset", "dependencies", "parameters"])
def test_incompatible_identity_does_not_reuse_parameter_dictionary(tmp_path, what):
    record = context(tmp_path)
    current_identity, current_asset, project = identity(), asset(tmp_path), "project-a"
    if what == "project":
        project = "project-b"
    elif what == "shader":
        current_identity["rawSha256"] = "e" * 64
    elif what == "stage":
        current_identity["stage"] = "vertex"
    elif what == "entry":
        current_identity["entryPoint"] = "Other"
    elif what == "root":
        current_asset["assetRoot"] = str(tmp_path / "other-game")
    elif what == "asset":
        current_asset["assetContentSha256"] = "f" * 64
    elif what == "dependencies":
        current_asset["assetEvidenceSha256"] = "f" * 64
    else:
        record["parameterDictionary"][0]["value"] = 0.1
    result = assess_material_semantic_context(record, identity=current_identity, project_id=project,
                                             asset_binding=current_asset)
    assert result["reuse"]["scope"] == "stale"
    assert not result["parameterDictionary"] and not result["candidateCorrespondences"]
    assert result["coverage"]["parameterCount"] == 0


def test_incremental_catalog_growth_does_not_discard_unchanged_material_dependencies(tmp_path):
    result = assess_material_semantic_context(context(tmp_path), identity=identity(), project_id="project-a",
        asset_binding=asset(tmp_path, catalogSha256="f" * 64))
    assert result["reuse"]["scope"] == "current-draw"


def test_catalog_bound_aggregate_change_keeps_unchanged_actual_dependency_files(tmp_path):
    evidence_files = [{"assetId": "material", "relativePath": "Materials/Wet.mat", "contentSha256": "b" * 64},
                      {"assetId": "parent", "relativePath": "Materials/Base.mat", "contentSha256": "e" * 64}]
    material = parse_material_parameters({"scalars": {"Wetness": 0.9573}})
    record = build_material_semantic_context(material, identity=identity(), project_id="project-a",
        asset_binding=asset(tmp_path, evidenceAssets=evidence_files))
    new_binding = asset(tmp_path, evidenceAssets=list(reversed(evidence_files)),
                        catalogSha256="f" * 64, assetEvidenceSha256="1" * 64)
    result = assess_material_semantic_context(record, identity=identity(), project_id="project-a",
                                             asset_binding=new_binding)
    assert result["reuse"]["scope"] == "current-draw"
    new_binding["evidenceAssets"][0] = {**new_binding["evidenceAssets"][0], "contentSha256": "2" * 64}
    changed = assess_material_semantic_context(record, identity=identity(), project_id="project-a",
                                              asset_binding=new_binding)
    assert changed["reuse"]["scope"] == "stale"


def test_summary_has_full_evidence_locator_and_small_identity_without_parameter_dump(tmp_path):
    record = assess(context(tmp_path), tmp_path)
    summary = semantic_context_summary(record, str(tmp_path / "ai/asset-resolution.json"))
    assert summary["parameterCount"] == 5 and summary["candidateCount"] == 1
    assert summary["unlocatedCount"] == 4 and summary["contextPath"].endswith("asset-resolution.json")
    assert summary["binding"]["shaderRawSha256"] == "a" * 64
    assert "parameterDictionary" not in summary
    assert len(json.dumps(summary).encode()) < 4096


def record_resolution(store, evidence):
    return store.record(game_key="game@build", shader_raw_sha256="a" * 64,
        structural="shape", stage="fragment", entry_point="MainPS", asset_relative_path="M_Wet",
        asset_content_sha256="b" * 64, asset_class="Material", catalog_sha256="c" * 64,
        negative=False, confidence=0.8, decided_by="ai", rationale="distinctive texture anchor",
        evidence=evidence)


def test_existing_resolution_store_roundtrips_larger_semantic_evidence_without_truncation(tmp_path):
    store = ShaderAssetResolutionStore(tmp_path / "resolutions.sqlite3")
    evidence = {"materialSemanticContext": context(tmp_path), "notes": "材质" * 30000}
    record_resolution(store, evidence)
    memory = store.lookup("game@build", "a" * 64, "shape", "fragment", "MainPS")
    assert memory["resolution"]["evidence"] == evidence


def test_oversized_or_nonfinite_evidence_cannot_replace_existing_decision(tmp_path):
    store = ShaderAssetResolutionStore(tmp_path / "resolutions.sqlite3")
    record_resolution(store, {"keep": "previous evidence"})
    for bad in ({"notes": "字" * 400000}, {"value": float("nan")}):
        with pytest.raises(ShaderAssetRetrievalError):
            record_resolution(store, bad)
    memory = store.lookup("game@build", "a" * 64, "shape", "fragment", "MainPS")
    assert memory["resolution"]["evidence"] == {"keep": "previous evidence"}


def test_legacy_truncated_json_remains_diagnostic_historical_lead(tmp_path):
    store = ShaderAssetResolutionStore(tmp_path / "resolutions.sqlite3")
    record_resolution(store, {"keep": "previous evidence"})
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE resolutions SET evidence_json=?", ('{"notes":"truncated',))
    memory = store.lookup("game@build", "a" * 64, "shape", "fragment", "MainPS")
    assert memory["resolution"]["evidence"] == {}
    assert memory["resolution"]["evidenceReadError"]
