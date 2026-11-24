"""Dispatch capture packages and portable temporal effects use executable inputs."""
import copy
import base64
import json
from pathlib import Path

import pytest

from src.unity_effects import validate_effect
from src.unity_restore import prepare_unity_import
from src.unity_restore import _buffer_bindings, UnityRestoreError
from tests.test_unity_restore import finalized_package, project

COMPUTE = '''#pragma kernel CSMain
RWTexture2D<float4> Result;
Texture2D<float4> History;
float4 _BaseColor;
[numthreads(8,8,1)] void CSMain(uint3 id:SV_DispatchThreadID) {
 uint w,h; Result.GetDimensions(w,h); if(id.x>=w || id.y>=h) return;
 Result[id.xy]=lerp(History[id.xy],_BaseColor,0.5);
}
'''

def effect_options():
    def resource(name):
        return {"name": name, "width": 64, "height": 64, "format": "R32G32B32A32_SFloat",
            "initialization": {"kind": "clear", "value": [0,0,0,1],
                "provenance": {"kind": "inferred", "reason": "Synthetic zero-history seed; warmup approximates convergence."}}}
    history, output = resource("history"), resource("output")
    history["historyFrom"] = "output"
    effect = {"resources": [history, output], "warmupFrames": 3, "output": "output", "passes": [
        {"id": "temporal", "kind": "compute", "asset": "Effects/Temporal.compute", "kernel": "CSMain",
         "entryPoint": "CSMain", "insertionPoint": "AfterRendering", "reads": ["history"], "writes": ["resolved"],
         "output": "output", "outputVersion": "resolved", "textures": {"Result": "output", "History": "history"}}]}
    plan = {"schemaVersion": 2, "target": {"pipeline": "built-in", "editorVersion": "6000.0.55f1", "insertionPoint": "AfterRendering"},
        "externalInputs": ["history"], "outputs": ["resolved"], "implementationAssets": ["Effects/Temporal.compute"],
        "resources": [{"version": "resolved", "width": 64, "height": 64, "format": "R32G32B32A32_SFloat", "samples": 1, "lifetime": "frame"},
            {"version": "history", "width": 64, "height": 64, "format": "R32G32B32A32_SFloat", "samples": 1,
             "lifetime": "history", "historyInitialization": "inferred zero seed with three temporal warmup iterations"}],
        "passes": [{"id": "temporal", "reads": ["history"], "writes": ["resolved"], "insertionPoint": "AfterRendering",
            "implementation": {"kind": "compute", "asset": "Effects/Temporal.compute", "entryPoint": "CSMain"},
            "attachments": [{"version": "resolved", "load": "clear", "store": "store", "clearValue": [0,0,0,1]}]}]}
    return {"pipelinePolicy": {"targetPipeline": "built-in"}, "effect": effect, "renderPlan": plan,
        "assets": [{"path": "Effects/Temporal.compute", "kind": "compute", "content": COMPUTE}]}

def test_dispatch_finalization_and_import_have_no_fabricated_mesh(tmp_path, project):
    package = finalized_package(tmp_path / "Managed", dispatch=True, shader_source=COMPUTE,
        material_color=[0.2,0.4,0.6,1], unity_options=effect_options())
    result = prepare_unity_import(package, project)
    request = json.loads(Path(result["requestPath"]).read_text())
    assert request["executionKind"] == "effect" and request["geometryApplicable"] is False
    assert request["mesh"] is None and request["properties"] == []
    assert request["runtimeBindings"][0]["sourceIdentity"]["stage"] == "cs"
    assert request["runtimeBindings"][0]["value"] == pytest.approx([0.2,0.4,0.6,1])
    assert request["options"]["effect"]["warmupFrames"] == 3
    assert request["options"]["effect"]["resources"][0]["initialization"]["provenance"]["kind"] == "inferred"
    assert not request["gaps"]

@pytest.mark.parametrize("mutation,match", [
    (lambda e: e["resources"][0].pop("initialization"), "explicit"),
    (lambda e: e["resources"][0]["initialization"]["provenance"].update(kind="assumed-exact"), "origin"),
    (lambda e: e["passes"][0].update(asset="../bad.compute"), "manifested"),
    (lambda e: e["passes"][0].update(groups=[0,1,1]), "groups"),
    (lambda e: e.update(warmupFrames=65), "warmup"),
    (lambda e: e["resources"][0].update(historyFrom="history"), "separate"),
    (lambda e: e["passes"][0].update(output="missing"), "output"),
    (lambda e: e["passes"][0].update(insertionPoint="BeforeRendering"), "RendererFeature or CustomPass"),
])
def test_effect_rejects_undefined_or_inconsistent_inputs(mutation, match):
    options = effect_options()
    effect = copy.deepcopy(options["effect"])
    mutation(effect)
    with pytest.raises(ValueError, match=match):
        validate_effect(effect, options["assets"])

def test_inferred_counter_is_explicit_and_bounded(tmp_path):
    entry = {"name": "Items", "type": "buffer", "bufferType": "append", "stride": 4,
        "bufferBase64": base64.b64encode(bytes(16)).decode(), "initialCounter": 2,
        "counterProvenance": {"kind": "inferred", "reason": "Two active entries inferred from the consumer bounds."}}
    assert _buffer_bindings([entry], {}, tmp_path, {}) == [entry]
    for patch, match in [({"initialCounter": 5}, "capacity"), ({"initialCounter": True}, "capacity"),
                         ({"counterProvenance": None}, "counterProvenance")]:
        with pytest.raises(UnityRestoreError, match=match):
            _buffer_bindings([{**entry, **patch}], {}, tmp_path, {})

def test_copy_names_its_real_packaged_implementation():
    options = effect_options()
    step = options["effect"]["passes"][0]
    step.update(kind="copy", source="history", entryPoint="BreadCaptureEffect.Execute")
    step.pop("asset")
    result = validate_effect(options["effect"], [])
    assert result["passes"][0]["asset"] == "Packages/com.bread.renderdoc/Runtime/BreadCaptureEffect.cs"
    assert result["passes"][0]["insertionPoint"] == "AfterRendering"
    assert "asset" not in step  # Validation does not mutate the authored spec.
    step["asset"] = "BreadCaptureEffect.Copy"
    with pytest.raises(ValueError, match="actual BreadCaptureEffect.cs"):
        validate_effect(options["effect"], [])

def test_generated_history_texture_requires_manifested_asset_and_unambiguous_source():
    options = effect_options()
    initial = options["effect"]["resources"][0]["initialization"]
    initial.update(kind="texture", textureAsset="History/Seed.png")
    with pytest.raises(ValueError, match="manifested data"):
        validate_effect(options["effect"], options["assets"])
    assets = options["assets"] + [{"path": "History/Seed.png", "kind": "data"}]
    assert validate_effect(options["effect"], assets)["resources"][0]["initialization"]["textureAsset"] == "History/Seed.png"
    initial["textureProperty"] = "_CapturedHistory"
    with pytest.raises(ValueError, match="exactly one"):
        validate_effect(options["effect"], assets)
