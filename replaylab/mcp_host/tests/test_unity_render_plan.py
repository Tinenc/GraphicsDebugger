import copy

import pytest

from src.unity_render_plan import validate_render_plan


def plan():
    return {"target": {"pipeline": "custom", "editorVersion": "6000.0.55f1", "insertionPoint": "before-display-map"},
            "externalInputs": ["camera-color", "previous-history"],
            "passes": [{"id": "combine", "reads": ["working-v2", "camera-color"], "writes": ["result"]},
                       {"id": "prepare", "reads": ["camera-color"], "writes": ["working-v1"]},
                       {"id": "filter", "reads": ["working-v1", "previous-history"], "writes": ["working-v2"]}],
            "outputs": ["result"]}


def test_resource_versions_produce_order_without_fixed_effect_or_pass_list():
    value = plan()
    original = copy.deepcopy(value)
    result = validate_render_plan(value)
    assert result["status"] == "valid"
    assert result["executionOrder"] == ["prepare", "filter", "combine"]
    assert result["dependencies"]["combine"] == ["filter"]
    assert not result["implementedInUnity"] and not result["visualEquivalenceVerified"]
    assert value == original


def test_missing_producer_is_not_treated_as_unconnected_optional_effect():
    value = plan()
    value["passes"][2]["reads"].append("missing-input")
    result = validate_render_plan(value)
    assert result["status"] == "needs-repair"
    assert {error["code"] for error in result["errors"]} == {"input-has-no-producer"}


def test_cycles_and_same_version_multiple_writers_are_detected():
    value = plan()
    value["passes"][1]["reads"] = ["result"]
    value["passes"][2]["writes"].append("camera-color")
    result = validate_render_plan(value)
    assert {error["code"] for error in result["errors"]} == {"dependency-cycle", "resource-version-has-multiple-writers"}


def test_previous_frame_version_is_external_not_a_cycle():
    value = plan()
    value["passes"][1]["reads"] = ["previous-history"]
    assert validate_render_plan(value)["status"] == "valid"


def test_self_sampling_output_and_unproduced_final_output_are_errors():
    value = plan()
    value["passes"][0]["reads"] = ["result"]
    value["outputs"] = ["camera-color"]
    assert {error["code"] for error in validate_render_plan(value)["errors"]} == {
        "pass-reads-its-new-output", "effect-output-not-produced"}


def test_pipeline_insertion_point_is_required_without_hardcoded_names():
    value = plan()
    value["target"]["insertionPoint"] = ""
    with pytest.raises(ValueError, match="insertionPoint"):
        validate_render_plan(value)


def implementation_plan():
    return {"schemaVersion": 2, "target": {"pipeline": "urp", "editorVersion": "6000.0.55f1", "insertionPoint": "AfterRenderingTransparents"},
            "externalInputs": ["camera-v1"], "passes": [{"id": "effect", "reads": ["camera-v1"], "writes": ["effect-v1"],
                "insertionPoint": "AfterRenderingTransparents", "implementation": {"asset": "Runtime/Effect.cs", "kind": "renderer-feature", "entryPoint": "Effect.RecordRenderGraph"},
                "attachments": [{"version": "effect-v1", "load": "clear", "store": "store", "clearValue": [0, 0, 0, 0]}]}],
            "outputs": ["effect-v1"], "implementationAssets": ["Runtime/Effect.cs"],
            "resources": [{"version": name, "format": "R16G16B16A16_SFloat", "width": 1920, "height": 1080, "samples": 1,
                           "lifetime": "external" if name == "camera-v1" else "frame"} for name in ("camera-v1", "effect-v1")]}


def test_detailed_plan_binds_outputs_and_scheduling_to_manifested_implementation():
    result = validate_render_plan(implementation_plan())
    assert result["status"] == "valid" and result["descriptorValidation"] == "checked"
    assert result["requiredUnityEvidence"][0]["asset"] == "Runtime/Effect.cs"
    assert result["requiredUnityEvidence"][0]["runtimeEvidence"] == "bread_render_capture.passExecution"
    assert not result["implementedInUnity"]


def test_detailed_plan_rejects_missing_physical_output_and_implementation_contracts():
    value = implementation_plan()
    value["resources"][1].pop("format")
    value["passes"][0]["implementation"]["asset"] = "NotInManifest.cs"
    value["passes"][0]["attachments"][0].pop("clearValue")
    codes = {error["code"] for error in validate_render_plan(value)["errors"]}
    assert codes == {"resource-format-required", "pass-implementation-not-manifested", "attachment-clear-value-required"}
