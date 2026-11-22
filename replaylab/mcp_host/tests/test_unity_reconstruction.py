"""Reconstruction is executable, reviewed evidence, never a missing-data waiver."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src import unity_restoration as restoration
from test_unity_restoration import prepared  # noqa: F401 - shared pytest fixture
from test_unity_restoration import png, review, write
from test_unity_render_plan import implementation_plan


def reconstruction(prepared, *, kind="pass"):
    store, task, observation, _, _, project = prepared
    source = project / "Assets/BreadRecovery/History.compute"
    source_sha = write(source, "float4 ReconstructHistory() { return UNITY_LIGHTMODEL_AMBIENT; }")
    observation["files"].append({"path": str(source), "sha256": source_sha})
    observation.update(renderExecutionId="render-current", renderCamera={"name": "MainCamera"},
        passExecution=[{"passId": "reconstruct", "implementationAsset": "Assets/BreadRecovery/History.compute",
                        "renderId": "render-current", "cameraName": "MainCamera"}])
    with store.transaction(task) as state:
        state["binding"]["requestGaps"] = [{"code": "history-input-unavailable", "resource": "history"}]
    status = store.inspect(task, observation)
    gap = next(row for row in status["gaps"] if row["code"] == "export-configuration-gap")
    verification = {"kind": kind, "passId": "reconstruct"}
    if kind == "unity-interface":
        verification = {"kind": kind, "interface": "UNITY_LIGHTMODEL_AMBIENT", "sourcePath": str(source)}
    elif kind == "runtime-binding":
        verification["name"] = "_ReconstructedHistoryWeight"
        observation["runtimeBindings"] = {"readback": [{"name": verification["name"], "value": 0.5}]}
    elif kind == "component":
        verification["type"] = "Bread.HistoryWarmup"
        observation["components"] = [{"type": verification["type"]}]
    record = {"id": "history-v1", "sourceGapIds": [gap["gapId"]], "classification": "inferred-history",
        "reason": "The capture has only the current frame; prior temporal state is reconstructed with warmup.",
        "evidence": ["The organized Shader blends previous irradiance with current scene radiance."],
        "implementationFiles": [str(source)], "verification": verification}
    return record


def reconstruction_review(store, task, *, reconstruction_id="history-v1"):
    status = store.status(task)
    return store.review(task, status["inspection"]["stateFingerprint"],
        {key: "Inspected the current source, runtime input and visible effect" for key in restoration.REVIEW_AREAS},
        [{"module": "Temporal irradiance", "source": "PreviousHistory", "target": "ReconstructHistory",
          "disposition": "approximation", "reconstructionId": reconstruction_id,
          "evidence": "The current pass uses the reconstructed input and preserves the temporal blend algorithm."}],
        ["Prior camera motion and disocclusion cannot be inferred exactly from this single frame."])


def codes(status):
    return {row["code"] for row in status["gaps"]}


@pytest.mark.parametrize("kind", ["pass", "runtime-binding", "component", "unity-interface"])
def test_missing_history_can_finish_as_an_explicit_verified_reconstruction(prepared, kind):
    store, task, observation, *_ = prepared
    record = reconstruction(prepared, kind=kind)
    initial = store.record_reconstruction(task, [record])
    assert not initial["complete"]
    assert "export-configuration-gap" in codes(initial)
    assert initial["reconstructions"][0]["capturedDataClaimed"] is False
    assert initial["reconstructions"][0]["origin"] == "inferred-or-derived"
    store.compare(task, observation)
    accepted = reconstruction_review(store, task)
    assert accepted["complete"], accepted["gaps"]
    assert accepted["resolvedReconstructions"][0]["status"] == "validated-reconstruction"
    assert accepted["binding"]["requestGaps"], "Original evidence must remain auditable"
    assert restoration.RestorationStore(store.root).status(task)["complete"]


def test_generic_source_review_cannot_approve_an_unmentioned_reconstruction(prepared):
    store, task, observation, *_ = prepared
    record = reconstruction(prepared)
    store.record_reconstruction(task, [record])
    store.compare(task, observation)
    status = review(store, task)
    assert not status["complete"]
    assert "specific-reconstruction-review-required" in status["reconstructionChecks"][0]["problems"]
    assert not reconstruction_review(store, task, reconstruction_id="unrelated-history")["complete"]


@pytest.mark.parametrize("field,value", [
    ("renderId", "older-render"), ("cameraName", "AnotherCamera"),
    ("implementationAsset", "Assets/Unrelated.compute"), ("passId", "unrelated-pass")])
def test_current_execution_is_bound_to_the_actual_reconstruction_implementation(prepared, field, value):
    store, task, observation, *_ = prepared
    record = reconstruction(prepared)
    store.record_reconstruction(task, [record])
    observation["passExecution"][0][field] = value
    store.compare(task, observation)
    status = reconstruction_review(store, task)
    assert not status["complete"]
    assert "export-configuration-gap" in codes(status)
    assert "reconstruction-validation-required" in codes(status)


def test_unobserved_input_or_component_does_not_count_as_executed_recovery(prepared):
    store, task, observation, *_ = prepared
    record = reconstruction(prepared, kind="runtime-binding")
    store.record_reconstruction(task, [record])
    observation["runtimeBindings"]["readback"] = []
    store.compare(task, observation)
    status = reconstruction_review(store, task)
    assert "replacement-input-not-observed" in status["reconstructionChecks"][0]["problems"]


def test_file_must_be_an_actual_observed_unity_dependency(prepared):
    store, task, observation, *_ = prepared
    record = reconstruction(prepared)
    store.record_reconstruction(task, [record])
    observation["files"] = observation["files"][:-1]
    store.compare(task, observation)
    status = reconstruction_review(store, task)
    assert not status["complete"]
    assert "implementation-not-current-in-unity" in status["reconstructionChecks"][0]["problems"]


def test_changed_implementation_stales_accepted_reconstruction(prepared):
    store, task, observation, *_ = prepared
    record = reconstruction(prepared)
    store.record_reconstruction(task, [record])
    store.compare(task, observation)
    assert reconstruction_review(store, task)["complete"]
    write(Path(record["implementationFiles"][0]), "float4 ReconstructHistory() { return 0; }")
    status = store.status(task)
    assert not status["complete"]
    assert {"unity-artifact-changed", "reconstruction-validation-required"} <= codes(status)


@pytest.mark.parametrize("failure", ["shaderCompile", "sceneIntegrity", "pipelineCompatible"])
def test_reconstruction_cannot_waive_actual_unity_failures(prepared, failure):
    store, task, observation, *_ = prepared
    record = reconstruction(prepared)
    store.record_reconstruction(task, [record])
    observation["checks"][failure] = False
    store.compare(task, observation)
    status = reconstruction_review(store, task)
    assert not status["complete"] and "unity-check-failed" in codes(status)
    gap = next(row for row in status["gaps"] if row["code"] == "unity-check-failed")
    assert not gap["reconstructable"]
    record["sourceGapIds"] = [gap["gapId"]]
    with pytest.raises(ValueError, match="Only exact current"):
        store.record_reconstruction(task, [record])


def test_known_cbuffer_parameter_drift_cannot_be_reclassified_as_inferred(prepared):
    store, task, observation, *_ = prepared
    record = reconstruction(prepared)
    with store.transaction(task) as state:
        path = Path(state["binding"]["requestPath"])
        request = json.loads(path.read_text())
        request["properties"] = [{"name": "Tint", "type": "vector", "scalarType": "float", "rows": 1,
            "columns": 4, "elements": 1, "value": [1, 1, 1, 1],
            "sourceIdentity": {"fieldId": "tint", "physicalId": "physical", "type": "float4"}}]
        state["binding"]["requestFileSha256"] = write(path, request)
    observation["material"]["propertyTypes"] = {"Tint": "Vector"}
    observation["material"]["properties"]["Tint"] = [0.5, 1, 1, 1]
    store.record_reconstruction(task, [record])
    store.compare(task, observation)
    status = reconstruction_review(store, task)
    assert "unity-parameter-readback-gap" in codes(status) and not status["complete"]
    gap = next(row for row in status["gaps"] if row["code"] == "unity-parameter-readback-gap")
    record["sourceGapIds"] = [gap["gapId"]]
    with pytest.raises(ValueError, match="Only exact current"):
        store.record_reconstruction(task, [record])


def test_large_visual_difference_cannot_be_waived_by_reconstruction_review(prepared, tmp_path):
    store, task, observation, *_ = prepared
    record = reconstruction(prepared)
    store.record_reconstruction(task, [record])
    observation["image"] = png(tmp_path / "wrong-reconstruction.png", (230, 0, 0))
    store.compare(task, observation)
    status = reconstruction_review(store, task)
    assert not status["complete"]
    assert {"render-mismatch", "export-configuration-gap", "reconstruction-validation-required"} <= codes(status)


def test_explicit_wrong_project_is_rejected_before_using_a_render(prepared, tmp_path):
    store, task, observation, *_ = prepared
    reconstruction(prepared)
    observation["projectPath"] = str(tmp_path / "UnrelatedUnityProject")
    with pytest.raises(ValueError, match="project"):
        store.compare(task, observation)


def test_strict_goal_disallows_inferred_inputs_until_fixed_visual_goal_is_adopted(prepared):
    store, task, observation, *_ = prepared
    record = reconstruction(prepared)
    strict = restoration._acceptance({"mode": "strict-pixel", "absoluteTolerance": 0})
    with store.transaction(task) as state:
        state["acceptance"] = strict
    with pytest.raises(ValueError, match="visual-fidelity"):
        store.record_reconstruction(task, [record])
    store.compare(task, observation)
    review(store, task)
    adopted = store.use_visual_goal(task)
    assert adopted["previousAcceptance"] == strict
    assert adopted["acceptance"] == restoration._acceptance({"mode": "visual-fidelity"})
    assert adopted["comparison"] is None and adopted["portingReview"] is None
    again = store.use_visual_goal(task)
    assert again["revision"] == adopted["revision"]
    assert again["acceptance"] == adopted["acceptance"]
    store.record_reconstruction(task, [record])
    store.compare(task, observation)
    assert reconstruction_review(store, task)["complete"]


def observed_plan(prepared, *, include_asset=True):
    store, task, observation, _, _, project = prepared
    plan = implementation_plan()
    plan["target"] = {"pipeline": "Built-in", "editorVersion": "6000.0.1f1", "insertionPoint": "after-camera"}
    asset = "Assets/BreadRecovery/History.cs"
    plan["implementationAssets"] = [asset]
    plan["passes"][0]["implementation"].update(asset=asset, kind="custom", entryPoint="History.Execute")
    plan["passes"][0]["insertionPoint"] = "after-camera"
    observation.update(renderPlan=plan, unityVersion="6000.0.1f1")
    if include_asset:
        sha = write(project / asset, "class History { void Execute() {} }")
        observation["files"].append({"path": str(project / asset), "sha256": sha})
    store.inspect(task, observation)
    return plan


def test_platform_adapted_plan_requires_current_observed_implementation_files(prepared):
    store, task, *_ = prepared
    observed_plan(prepared, include_asset=False)
    with pytest.raises(ValueError, match="implementation|asset|observed"):
        store.adopt_observed_plan(task)


def test_platform_adapted_plan_is_bound_then_requires_new_render_and_review(prepared):
    store, task, observation, *_ = prepared
    plan = observed_plan(prepared)
    result = store.adopt_observed_plan(task)
    assert result["effectiveRenderPlan"]["plan"] == plan
    assert result["effectiveRenderPlan"]["origin"] == "observed-platform-adaptation"
    assert result["comparison"] is None and result["portingReview"] is None
    store.compare(task, observation)
    assert "render-plan-pass-not-observed" in codes(review(store, task))


def test_changed_plan_source_requires_fresh_unity_inspection(prepared):
    store, task, _, _, _, project = prepared
    observed_plan(prepared)
    write(project / "Assets/BreadRecovery/History.cs", "changed implementation")
    with pytest.raises(ValueError, match="changed"):
        store.adopt_observed_plan(task)


def test_platform_plan_cannot_claim_another_pipeline(prepared):
    store, task, observation, *_ = prepared
    observed_plan(prepared)
    observation["renderPlan"]["target"]["pipeline"] = "HDRP"
    store.inspect(task, observation)
    with pytest.raises(ValueError, match="pipeline|target"):
        store.adopt_observed_plan(task)


def test_platform_plan_cannot_claim_another_editor_version(prepared):
    store, task, observation, *_ = prepared
    observed_plan(prepared)
    observation["renderPlan"]["target"]["editorVersion"] = "2021.3.1f1"
    store.inspect(task, observation)
    with pytest.raises(ValueError, match="version|target"):
        store.adopt_observed_plan(task)


@pytest.mark.parametrize("location", ["embedded", "cache", "external"])
def test_plan_can_use_actual_tracked_package_implementation(prepared, tmp_path, location):
    store, task, observation, _, _, project = prepared
    plan = observed_plan(prepared, include_asset=False)
    asset = "Packages/com.bread.renderdoc/Runtime/BreadCaptureEffect.cs"
    plan["implementationAssets"] = [asset]
    plan["passes"][0]["implementation"]["asset"] = asset
    package_root = (project / "Packages/com.bread.renderdoc" if location == "embedded" else
                    project / "Library/PackageCache/com.bread.renderdoc@1.0" if location == "cache" else
                    tmp_path / "shared-package")
    path = package_root / "Runtime/BreadCaptureEffect.cs"
    row = {"path": str(path), "sha256": write(path, "class ActualPackageCopy {}"), "assetPath": asset}
    if location == "external":
        row["package"] = {"name": "com.bread.renderdoc", "version": "1.0", "source": "Local", "resolvedPath": str(package_root)}
        observation["externalDependencies"] = [row]
    else:
        observation["files"].append(row)
    store.inspect(task, observation)
    assert store.adopt_observed_plan(task)["effectiveRenderPlan"]["plan"] == plan
    write(path, "changed package implementation")
    with pytest.raises(ValueError, match="changed"):
        store.adopt_observed_plan(task)


def test_plan_cannot_invent_a_package_source_file(prepared):
    store, task, observation, *_ = prepared
    plan = observed_plan(prepared, include_asset=False)
    asset = "Packages/com.bread.renderdoc/Runtime/Imaginary.cs"
    plan["implementationAssets"] = [asset]
    plan["passes"][0]["implementation"]["asset"] = asset
    store.inspect(task, observation)
    with pytest.raises(ValueError, match="absent"):
        store.adopt_observed_plan(task)


def test_reconstruction_cannot_reference_an_invented_gap_or_nonproject_file(prepared, tmp_path):
    store, task, *_ = prepared
    record = reconstruction(prepared)
    invented = copy.deepcopy(record)
    invented["sourceGapIds"] = ["0" * 64]
    with pytest.raises(ValueError, match="Only exact current"):
        store.record_reconstruction(task, [invented])
    external = tmp_path / "History.compute"
    write(external, "valid-looking implementation outside the project")
    record["implementationFiles"] = [str(external)]
    with pytest.raises(ValueError, match="selected project's Assets"):
        store.record_reconstruction(task, [record])


def test_native_substitution_must_reference_a_real_interface_in_tracked_source(prepared):
    store, task, *_ = prepared
    record = reconstruction(prepared, kind="unity-interface")
    record["verification"]["interface"] = "UnityInterfaceNotUsedBySource"
    with pytest.raises(ValueError, match="absent from"):
        store.record_reconstruction(task, [record])


def test_reconstruction_registration_is_idempotent_without_new_review_churn(prepared):
    store, task, observation, *_ = prepared
    record = reconstruction(prepared)
    store.record_reconstruction(task, [record])
    store.compare(task, observation)
    accepted = reconstruction_review(store, task)
    repeated = store.record_reconstruction(task, [copy.deepcopy(record)])
    assert repeated["revision"] == accepted["revision"]
    assert repeated["complete"]


def test_repaired_missing_input_can_retire_stale_reconstruction_with_current_evidence(prepared):
    store, task, observation, *_ = prepared
    record = reconstruction(prepared)
    # Dynamic Editor diagnostics can disappear after an exact implementation is
    # supplied, without recreating the immutable captured export.
    with store.transaction(task) as state:
        state["binding"]["requestGaps"] = []
    observation["gaps"] = [{"code": "history-input-unavailable", "resource": "history"}]
    status = store.inspect(task, observation)
    record["sourceGapIds"] = [next(g["gapId"] for g in status["gaps"] if g["code"] == "unity-configuration-gap")]
    store.record_reconstruction(task, [record])
    store.compare(task, observation)
    assert reconstruction_review(store, task)["complete"]

    implementation = Path(record["implementationFiles"][0])
    new_sha = write(implementation, "float4 ReadCapturedHistory() { return CapturedHistory.Load(0); }")
    next(row for row in observation["files"] if row["path"] == str(implementation))["sha256"] = new_sha
    observation["gaps"] = []
    store.compare(task, observation)
    status = review(store, task)
    assert not status["complete"] and codes(status) == {"reconstruction-validation-required"}
    retired = store.record_reconstruction(task, [{"id": record["id"], "retire": True,
        "reason": "The exact captured historical input is now supplied by the revised source."}])
    assert retired["complete"], retired["gaps"]
    assert retired["reconstructions"] == []
    assert any(row.get("id", (row.get("record") or {}).get("id")) == record["id"]
               for row in retired["reconstructionHistory"])


def test_reconstruction_cannot_be_retired_while_original_gap_still_exists(prepared):
    store, task, observation, *_ = prepared
    record = reconstruction(prepared)
    store.record_reconstruction(task, [record])
    store.compare(task, observation)
    assert reconstruction_review(store, task)["complete"]
    with pytest.raises(ValueError, match="gap|source|reconstruct"):
        store.record_reconstruction(task, [{"id": record["id"], "retire": True,
            "reason": "The visual approximation appears close enough."}])


@pytest.mark.parametrize("fresh_review", [False, True])
def test_reconstruction_retirement_requires_a_current_comparison_and_review(prepared, fresh_review):
    store, task, observation, *_ = prepared
    record = reconstruction(prepared)
    store.record_reconstruction(task, [record])
    with store.transaction(task) as state:
        state["binding"]["requestGaps"] = []
    if fresh_review:
        review(store, task)
    else:
        store.compare(task, observation)
    with pytest.raises(ValueError, match="current|render|review|verif"):
        store.record_reconstruction(task, [{"id": record["id"], "retire": True,
            "reason": "The exact input is now available."}])
