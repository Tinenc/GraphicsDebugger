"""Functional restoration cannot finish from import or arbitrary image claims."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from PIL import Image
import pytest

from src import unity_restoration as restoration
from src.unity_shader_sources import bind_sources_to_bundle, collect_organized_sources


def write(path: Path, content) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(content).encode() if isinstance(content, dict) else content if isinstance(content, bytes) else content.encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def png(path: Path, color=(64, 96, 128)) -> dict:
    Image.new("RGB", (8, 8), color).save(path)
    return {"path": str(path), "sha256": restoration.file_digest(path), "width": 8, "height": 8}


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    project = tmp_path / "Unity"
    (project / "Assets").mkdir(parents=True)
    write(project / "ProjectSettings/ProjectVersion.txt", "m_EditorVersion: 6000.0.1f1\n")
    write(project / "Packages/manifest.json", {"dependencies": {}})
    root = tmp_path / "restorations"
    monkeypatch.setenv("BREAD_UNITY_RESTORATION_ROOT", str(root))
    store = restoration.RestorationStore(root)
    capture = {"captureSessionId": "a" * 32, "captureId": "b" * 64, "api": "Vulkan"}
    begun = store.begin(capture, 23, str(project), {"event_id": 23, "attachment_kind": "rtv"})
    task = begun["restorationId"]
    workspace = tmp_path / "workspace"
    source_sha = write(workspace / "source.hlsl", "float4 Main() : SV_Target { return 1; }")
    gate_sha = write(workspace / "ai/organize-code-gate.json", {"validated": True})
    sources = [{"workspacePath": str(workspace), "sourcePath": str(workspace / "source.hlsl"),
        "sourceSha256": source_sha, "stage": "fragment", "rawSha256": "c" * 64, "ready": True,
        "dependencyFiles": [], "validation": {"gateSha256": gate_sha}}]
    bundle = {"eventId": 23, "captureSessionId": capture["captureSessionId"],
              "shaders": [{"stage": "ps", "rawSha256": "c" * 64, "status": "captured"}]}
    spec = {"shader": {"organizedSources": sources}}
    package = tmp_path / "package"
    manifest_sha = write(package / "package-manifest.json", {"files": []})
    request = {"projectPath": str(project), "requestSha256": "e" * 64,
               "packageManifestSha256": manifest_sha, "gaps": [],
               "constantBufferCoverage": {"requiredLaneCount": 0, "mappedLaneCount": 0,
                                          "reviewedUnusedLaneCount": 0, "unmappedLaneCount": 0}}
    request_path = project / "Library/Bread/Requests/import.json"
    write(request_path, request)
    monkeypatch.setattr(restoration, "package_bundle", lambda unused: (bundle, spec))
    exported = {"operationId": "f" * 32, "status": "finalized", "packagePath": str(package)}
    store.bind(task, exported, {"requestPath": str(request_path)}, sources)
    scene = project / "Assets/Restored.unity"
    scene_sha = write(scene, "actual scene snapshot")
    shader = project / "Assets/Restored.shader"
    shader_sha = write(shader, "actual derived source")
    observation = {"schema": "bread-unity-capture-observation/v1", **request,
        "scenePath": str(scene), "files": [{"path": str(scene), "sha256": scene_sha}, {"path": str(shader), "sha256": shader_sha}],
        "material": {"shader": "Bread/Restored", "properties": {"Tint": [1, 1, 1, 1]}},
        "camera": {"fov": 45}, "renderPipeline": "Built-in", "targetGraphicsAPI": "Vulkan",
        "displayMapping": store.load(task)["displayMapping"], "targetOutput": store.load(task)["targetOutput"],
        "checks": {k: True for k in ("meshImport", "shaderCompile", "materialImport", "textureImport", "sceneIntegrity", "pipelineCompatible")}}
    reference = {"eventId": 23, "capture": capture, "image": png(tmp_path / "reference.png")}
    store.reference(task, reference)
    observation["image"] = png(tmp_path / "render.png")
    return store, task, observation, sources, bundle, project


def review(store, task):
    fingerprint = store.status(task)["inspection"]["stateFingerprint"]
    return store.review(task, fingerprint, {key: "Inspected current implementation and its mapping" for key in restoration.REVIEW_AREAS},
        [{"module": "Material", "source": "Main", "target": "EvaluateMaterial", "disposition": "ported",
          "evidence": "The ordered color computation and source parameters are preserved"}], [])


def test_import_and_semantic_review_cannot_skip_actual_render(prepared):
    store, task, observation, *_ = prepared
    store.inspect(task, observation)
    result = review(store, task)
    assert not result["complete"]
    assert "current-render-comparison-required" in {g["code"] for g in result["gaps"]}


def test_observed_render_review_and_fresh_dependencies_complete_then_shader_edit_stales(prepared):
    store, task, observation, *_ = prepared
    store.compare(task, observation)
    assert review(store, task)["complete"]
    reopened = restoration.RestorationStore(store.root)
    assert reopened.status(task)["complete"]
    write(Path(observation["files"][1]["path"]), "a changed shader")
    result = reopened.status(task)
    assert not result["complete"]
    assert "unity-artifact-changed" in {g["code"] for g in result["gaps"]}


def test_failed_render_requires_repair_and_comparison_does_not_widen_thresholds(prepared, tmp_path):
    store, task, observation, *_ = prepared
    observation["image"] = png(tmp_path / "wrong.png", (240, 10, 10))
    first = store.compare(task, observation)
    assert first["comparison"]["withinTolerance"] is False
    assert not review(store, task)["complete"]
    second = store.compare(task, observation)
    assert second["iterations"][-1]["progress"] == "unchanged"
    assert second["acceptance"] == first["acceptance"]
    observation["image"] = png(tmp_path / "fixed.png")
    assert store.compare(task, observation)["comparison"]["withinTolerance"]
    assert review(store, task)["complete"]


@pytest.mark.parametrize("field", ["requestSha256", "packageManifestSha256"])
def test_another_import_receipt_cannot_supply_evidence(prepared, field):
    store, task, observation, *_ = prepared
    observation[field] = "0" * 64
    with pytest.raises(ValueError, match="different import"):
        store.compare(task, observation)


def test_reference_must_match_selected_output_and_capture_session(prepared, tmp_path):
    store, task, *_ = prepared
    receipt = {"eventId": 24, "capture": {"captureSessionId": "a" * 32}, "image": png(tmp_path / "other.png")}
    with pytest.raises(ValueError, match="captured output"):
        store.reference(task, receipt)
    receipt.update(eventId=23, capture={"captureSessionId": "0" * 32})
    with pytest.raises(ValueError, match="captured output"):
        store.reference(task, receipt)


def test_scene_in_memory_change_invalidates_review_even_if_files_unchanged(prepared):
    store, task, observation, *_ = prepared
    store.compare(task, observation)
    assert review(store, task)["complete"]
    observation["camera"]["fov"] = 60
    result = store.inspect(task, observation)
    assert not result["complete"] and result["comparison"] is None and result["portingReview"] is None


def test_observation_requires_actual_checks_and_dependency_files(prepared):
    store, task, observation, *_ = prepared
    observation["files"] = []
    with pytest.raises(ValueError, match="checks or dependency"):
        store.inspect(task, observation)


def test_file_outside_target_project_rejected(prepared, tmp_path):
    store, task, observation, *_ = prepared
    external = tmp_path / "unrelated.scene"
    sha = write(external, "other project")
    observation["files"].append({"path": str(external), "sha256": sha})
    with pytest.raises(ValueError, match="outside"):
        store.inspect(task, observation)


def test_missing_render_state_check_cannot_be_overruled_by_review(prepared):
    store, task, observation, *_ = prepared
    observation["gaps"] = [{"code": "render_state_mismatch", "state": "Cull"}]
    store.compare(task, observation)
    assert not review(store, task)["complete"]


def test_export_gaps_and_unmapped_constants_remain_blocking_when_editor_reports_success(prepared):
    store, task, observation, *_ = prepared
    store.compare(task, observation)
    assert review(store, task)["complete"]
    with store.transaction(task) as state:
        state["binding"]["requestGaps"] = [{"code": "captured_buffer_binding_unmapped"}]
        state["binding"]["parameterCoverage"]["unmappedLaneCount"] = 4
    result = store.status(task)
    assert not result["complete"]
    assert {"export-configuration-gap", "cbuffer-coverage-incomplete"} <= {g["code"] for g in result["gaps"]}


def test_object_light_and_color_space_changes_stale_comparison(prepared):
    store, task, observation, *_ = prepared
    store.compare(task, observation)
    assert review(store, task)["complete"]
    observation.update(object={"scale": [2, 2, 2]}, lights=[{"intensity": 4}], colorSpace="Linear")
    state = store.inspect(task, observation)
    assert state["comparison"] is None and state["portingReview"] is None


def test_unordered_dependency_enumeration_is_not_new_work(prepared):
    store, task, observation, *_ = prepared
    store.compare(task, observation)
    assert review(store, task)["complete"]
    observation["files"].reverse()
    assert store.inspect(task, observation)["complete"]


def test_render_plan_requires_current_matching_execution_and_dependency_order(prepared):
    store, task, observation, *_ = prepared
    plan = {"passes": [{"id": "lighting"}, {"id": "bloom"}]}
    requirements = [{"pass": name, "asset": "Effects.cs", "reads": reads, "writes": writes}
        for name, reads, writes in [("lighting", [], ["lit"]), ("bloom", ["lit"], ["bloomed"])]]
    with store.transaction(task) as state:
        state["binding"].update(renderPlan=plan, renderPlanReview={"status": "valid", "descriptorValidation": "checked",
            "requiredUnityEvidence": requirements, "dependencies": {"lighting": [], "bloom": ["lighting"]}})
    observation["renderPlan"] = plan
    observation["renderExecutionId"] = "test-render"
    observation["renderCamera"] = {"name": "TestCamera"}
    store.compare(task, observation)
    result = review(store, task)
    assert "render-plan-pass-not-observed" in {g["code"] for g in result["gaps"]}
    observation["passExecution"] = [{"passId": row["pass"], "implementationAsset": row["asset"],
        "reads": row["reads"], "writes": row["writes"], "renderId": "test-render", "cameraName": "TestCamera"} for row in requirements]
    store.compare(task, observation)
    assert review(store, task)["complete"]
    observation["passExecution"].reverse()
    result = store.compare(task, observation)
    assert "render-plan-execution-order-mismatch" in {g["code"] for g in result["gaps"]}


def test_graphics_api_enum_prefix_does_not_reject_same_api(prepared):
    _, _, _, sources, bundle, _ = prepared
    bundle["api"] = "Vulkan"
    sources[0]["api"] = "GraphicsAPI.Vulkan"
    assert bind_sources_to_bundle(sources, bundle) == sources


def test_unity_resolved_external_package_files_are_bound_and_rechecked(prepared, tmp_path):
    store, task, observation, *_ = prepared
    root = tmp_path / "Editor/BuiltInPackages/com.unity.render-pipelines.universal"
    path = root / "Shaders/Lit.shader"
    sha = write(path, "real installed pipeline Shader")
    observation["externalDependencies"] = [{"path": str(path), "sha256": sha,
        "assetPath": "Packages/com.unity.render-pipelines.universal/Shaders/Lit.shader",
        "package": {"name": "com.unity.render-pipelines.universal", "version": "17.0.0",
                    "source": "BuiltIn", "resolvedPath": str(root)}}]
    store.compare(task, observation)
    assert review(store, task)["complete"]
    write(path, "changed pipeline Shader")
    assert "unity-package-dependency-changed" in {g["code"] for g in store.status(task)["gaps"]}


def test_external_file_cannot_claim_a_different_unity_package_asset(prepared, tmp_path):
    store, task, observation, *_ = prepared
    root = tmp_path / "package"
    path = root / "Real.shader"
    sha = write(path, "shader")
    observation["externalDependencies"] = [{"path": str(path), "sha256": sha,
        "assetPath": "Packages/com.unity.render-pipelines.universal/Other.shader",
        "package": {"name": "com.unity.render-pipelines.universal", "version": "17.0.0",
                    "source": "BuiltIn", "resolvedPath": str(root)}}]
    with pytest.raises(ValueError, match="resolved package path"):
        store.inspect(task, observation)


def test_system_constant_needs_specific_unity_interface_source_review(prepared):
    store, task, observation, *_ = prepared
    with store.transaction(task) as state:
        state["binding"]["systemInterfaces"] = [{"sourceIdentity": {"fieldId": "matrix-field"},
            "kind": "system", "name": "UNITY_MATRIX_VP", "value": [1, 0, 0, 0]}]
    store.compare(task, observation)
    result = review(store, task)
    assert "system-interface-review-required" in {g["code"] for g in result["gaps"]}
    result = store.review(task, result["inspection"]["stateFingerprint"], result["portingReview"]["assessments"],
        [{"module": "Captured view projection", "fieldId": "matrix-field", "source": "Scene.ViewProjection",
          "target": "UNITY_MATRIX_VP", "disposition": "unity-native",
          "evidence": "Derived source transforms world position with Unity VP and removes the captured register; verified matrix order and depth convention"}], [])
    assert result["complete"]


def test_identical_pixels_do_not_hide_captured_material_parameter_drift(prepared):
    store, task, observation, *_ = prepared
    with store.transaction(task) as state:
        path = Path(state["binding"]["requestPath"])
        request = json.loads(path.read_text())
        request["properties"] = [{"name": "Tint", "type": "vector", "scalarType": "float", "rows": 1,
            "columns": 4, "elements": 1, "value": [1, 1, 1, 1],
            "sourceIdentity": {"fieldId": "tint", "physicalId": "physical", "type": "float4"}}]
        state["binding"]["requestFileSha256"] = write(path, request)
    observation["material"]["propertyTypes"] = {"Tint": "Vector"}
    observation["material"]["properties"]["Tint"] = [0.5, 1, 1, 1]
    store.compare(task, observation)
    result = review(store, task)
    assert result["comparison"]["withinTolerance"]
    assert "unity-parameter-readback-gap" in {g["code"] for g in result["gaps"]}
    observation["material"]["properties"]["Tint"] = [1, 1, 1, 1]
    store.compare(task, observation)
    assert review(store, task)["complete"]


def test_sources_require_every_captured_stage_and_exact_original_bytes(prepared):
    _, _, _, sources, bundle, _ = prepared
    assert bind_sources_to_bundle(sources, bundle)[0]["stage"] == "fragment"
    bundle["shaders"].append({"stage": "vs", "rawSha256": "d" * 64, "status": "captured"})
    with pytest.raises(ValueError, match="every captured"):
        bind_sources_to_bundle(sources, bundle)
    sources[0]["rawSha256"] = "0" * 64
    with pytest.raises(ValueError, match="does not match"):
        bind_sources_to_bundle(sources, bundle)


def test_changed_organized_source_and_tampered_image_both_invalidate_acceptance(prepared):
    store, task, observation, sources, *_ = prepared
    store.compare(task, observation)
    assert review(store, task)["complete"]
    write(Path(sources[0]["sourcePath"]), "changed captured-compatible source")
    png(Path(observation["image"]["path"]), (1, 2, 3))
    codes = {g["code"] for g in store.status(task)["gaps"]}
    assert {"organized-source-evidence-changed", "comparison-image-changed"} <= codes


def test_project_read_keeps_observation_and_reported_mutation_invalidates_it(prepared):
    from src.unity_restoration_requirements import track, refresh
    store, task, observation, _, _, project = prepared
    store.compare(task, observation)
    complete = review(store, task)
    ledger = {"scopeId": "scope"}
    track(ledger, "review_unity_restoration", complete, {}, False)
    assert not refresh(ledger)
    track(ledger, "call_unity_project_tool", {"success": True}, {"project_path": str(project), "tool_name": "manage_material"}, False)
    assert not refresh(ledger)
    track(ledger, "call_unity_project_tool", {"success": True, "sceneModified": True},
          {"project_path": str(project), "tool_name": "manage_material"}, False)
    assert refresh(ledger)[0]["complete"] is False
    assert store.status(task)["inspection"] is None


@pytest.mark.parametrize("reference", [{"event_id": 1, "crop": {"x": 0}}, {"event_id": 1, "range_mode": "subresource-minmax"}])
def test_reference_cannot_hide_mismatch_by_crop_or_independent_normalization(reference):
    with pytest.raises(ValueError):
        restoration.reference_selection(reference)


def test_real_source_bytes_and_current_include_closure_are_checked(tmp_path, monkeypatch):
    from src import shader_organization_review, shader_semantic_delivery
    monkeypatch.setattr(shader_organization_review, "publication_review", lambda *a: {"ready": True, "sourceSha256": sha})
    monkeypatch.setattr(shader_semantic_delivery, "delivery_state", lambda *a, **kw: {"required": False, "status": "not-applicable"})
    root = tmp_path / "workspace"
    sha = write(root / "source.hlsl", '#include "common.hlsl"\n')
    header_sha = write(root / "common.hlsl", "float Compute() { return 1; }")
    write(root / "ai/organize-code-gate.json", {"finalSourceSha256": sha, "organizationId": "a" * 32,
        "wholeSourceValidated": True, "organizedCompiledAndAbiValidated": True, "finalCandidateRawSha256": "d" * 64,
        "publicationContract": "validated-source"})
    status = {"workspaceId": "workspace", "stage": "fragment", "capture": {"api": "OpenGL"},
        "baseline": {"rawSha256": "c" * 64}, "source": {"present": True, "path": str(root / "source.hlsl"),
            "format": "hlsl", "currentSha256": sha}, "abiCompatible": True, "sourceDependenciesCurrent": True,
        "latestCompiled": {"sourceSha256": sha, "rawSha256": "d" * 64,
            "sourceDependencies": {"documents": [{"sourcePath": str(root / "common.hlsl"), "sourceSha256": header_sha}]}}}
    result = collect_organized_sources([str(root)], lambda unused: status)
    assert result["ready"]
    assert result["sources"][0]["dependencyFiles"][0]["sha256"] == header_sha
    write(root / "common.hlsl", "float Compute() { return 2; }")
    assert not collect_organized_sources([str(root)], lambda unused: status)["ready"]
    write(root / "source.hlsl", "another source")
    with pytest.raises(ValueError, match="changed during"):
        collect_organized_sources([str(root)], lambda unused: status)
