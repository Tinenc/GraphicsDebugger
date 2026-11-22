"""Effect completion requires current measured execution, not matching uploads alone."""
import copy
import json
from pathlib import Path

import pytest

from src.unity_render_plan import validate_render_plan
from test_unity_restoration import prepared, review, write  # noqa: F401


@pytest.fixture
def effect(prepared):
    store, task, observation, *rest = prepared
    plan = {"schemaVersion": 2, "target": {"pipeline": "Built-in", "editorVersion": "6000.0.1f1", "insertionPoint": "after-camera"},
        "externalInputs": [], "outputs": ["result"], "implementationAssets": ["Assets/Restored.shader"],
        "resources": [{"version": "result", "format": "R8G8B8A8_UNorm", "width": 8, "height": 8, "samples": 1, "lifetime": "frame"}],
        "passes": [{"id": "algorithm", "reads": [], "writes": ["result"], "insertionPoint": "after-camera",
            "implementation": {"kind": "compute", "asset": "Assets/Restored.shader", "entryPoint": "CSMain"},
            "attachments": [{"version": "result", "load": "clear", "store": "store", "clearValue": [0, 0, 0, 0]}]}]}
    with store.transaction(task) as state:
        path = Path(state["binding"]["requestPath"])
        request = json.loads(path.read_text())
        request["executionKind"] = "effect"
        state["binding"].update(executionKind="effect", requestFileSha256=write(path, request),
            renderPlan=plan, renderPlanReview=validate_render_plan(plan))
    snapshot = {"kind": "executable-effect", "uploadsVerified": True, "error": None, "warmupFrames": 2,
        "output": "result", "resources": [], "passes": [{"id": "algorithm", "kind": "compute",
        "asset": "Assets/Restored.shader", "entryPoint": "CSMain", "output": "result"}]}
    observation.update(executionKind="effect", geometryApplicable=False, effect=snapshot,
        effectExecution={**copy.deepcopy(snapshot), "renderExecutionId": "render-1", "executedIterations": 3},
        renderExecutionId="render-1", renderCamera={"name": "Capture Camera"}, renderPlan=plan,
        passExecution=[{"passId": "algorithm", "kind": "compute", "entryPoint": "CSMain", "insertionPoint": "after-camera",
            "implementationAsset": "Assets/Restored.shader", "reads": [], "writes": ["result"], "renderId": "render-1",
            "cameraName": "Capture Camera", "outputVersion": "result", "width": 8, "height": 8, "samples": 1, "format": "R8G8B8A8_UNorm"}])
    return store, task, observation


def test_effect_requires_real_execution_before_completion(effect):
    store, task, observation = effect
    store.compare(task, observation)
    assert review(store, task)["complete"]


@pytest.mark.parametrize("change", ["no_execution", "old_render", "no_pass", "wrong_camera", "wrong_asset", "missing_warmup", "failed_upload"])
def test_effect_evidence_failure_survives_matching_pixels_and_semantic_review(effect, change):
    store, task, observation = effect
    if change == "no_execution":
        observation.pop("effectExecution")
    elif change == "old_render":
        observation["effectExecution"]["renderExecutionId"] = "old"
    elif change == "no_pass":
        observation["passExecution"] = []
    elif change in {"wrong_camera", "wrong_asset"}:
        observation["passExecution"][0]["cameraName" if change == "wrong_camera" else "implementationAsset"] = "other"
    elif change == "missing_warmup":
        observation["effectExecution"]["executedIterations"] = 1
    elif change == "failed_upload":
        observation["effect"]["uploadsVerified"] = False
    store.compare(task, observation)
    assert not review(store, task)["complete"]


def test_changed_effect_configuration_stales_image_and_review(effect):
    store, task, observation = effect
    store.compare(task, observation)
    assert review(store, task)["complete"]
    observation["effect"]["warmupFrames"] = 3
    state = store.inspect(task, observation)
    assert state["comparison"] is None and state["portingReview"] is None


@pytest.mark.parametrize("field,value", [("executionKind", "draw"), ("geometryApplicable", True)])
def test_effect_cannot_use_draw_applicability_to_hide_missing_execution(effect, field, value):
    store, task, observation = effect
    observation[field] = value
    with pytest.raises(ValueError):
        store.inspect(task, observation)
