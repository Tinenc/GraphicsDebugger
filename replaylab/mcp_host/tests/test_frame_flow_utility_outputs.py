"""Utility buffer evidence must survive the same publication path as draw outputs."""
from types import SimpleNamespace

import pytest

from src.frame_render_flow import FrameRenderFlowStore, build_document
from test_frame_render_flow import outline, _reviewed_stage
from test_frame_stage_evidence import evidence, resource_id, texture


def observed(destination=505, source=0, buffers=(505,), textures=()):
    ctrl = SimpleNamespace(GetTextures=lambda: list(textures),
                           GetBuffers=lambda: [SimpleNamespace(resourceId=rid) for rid in buffers])
    # An inherited PS is deliberately present: no Shader bindings belong to a
    # query resolve, even if the prior draw left that pipeline bound.
    return evidence.inventory(ctrl, object(), {"actionKind": "utility", "shaders": {"fragment": {}}},
                              [("fragment", "PS")], resource_id, lambda fmt: fmt.name,
                              action=SimpleNamespace(copySource=source, copyDestination=destination))


@pytest.mark.parametrize("destination", [505, 2**55 + 3])
def test_query_resolve_uses_actual_buffer_destination_without_inherited_shader(destination):
    result = observed(destination=destination, buffers=(destination,))
    assert result["bindingsComplete"] and not result["bindingErrors"]
    assert result["textureInputs"] == []
    assert result["expectedOutputs"] == [{
        "kind": "copy-destination", "index": 0, "stage": "", "resourceId": destination,
        "resourceType": "buffer", "captureSupported": False, "mip": 0, "slice": 0,
        "sample": 0, "format": "", "width": 0, "height": 0, "componentCount": 0}]


def test_buffer_copy_records_both_exact_operands():
    result = observed(source=700, buffers=(505, 700))
    assert result["textureInputs"][0]["resourceId"] == 700
    assert result["expectedOutputs"][0]["resourceId"] == 505
    assert result["bindingsComplete"]


def test_missing_description_does_not_turn_unknown_resource_into_a_buffer():
    result = observed(buffers=())
    assert not result["bindingsComplete"]
    assert result["bindingErrors"][0]["resourceId"] == 505
    assert not result["expectedOutputs"]
    image = observed(buffers=(), textures=(texture(505),))
    assert not image["expectedOutputs"]  # never certify a texture as non-image


def utility_case():
    data = outline(1)
    data["actions"][0].update(flags=0x0800, actionKind="utility", outputs=[], copyDestination=505)
    store = FrameRenderFlowStore()
    record = store.prepare(data)
    proposal = _reviewed_stage(1, 1, title="Resolve visibility counters", summary="Copy visibility counters into a readback buffer.",
        algorithm={"inputs": [], "steps": [{"eventId": 1, "title": "Resolve counters",
            "calculation": "Copy query results to the captured buffer.",
            "evidence": "The native resolve command records the query range and destination byte offset."}],
            "outputs": [{"eventId": 1, "resourceId": 505, "role": "Visibility counters"}],
            "integration": [{"sourceEventId": 1, "resourceId": 505, "mode": "non-image", "eventId": 1,
                "explanation": "The readback buffer stores counters; this capture has no later GPU consumer.",
                "evidence": "Exact native resolve destination and complete resource-use inventory."}]})
    return store, record, proposal


def test_non_image_terminal_is_accepted_and_persisted_without_inventing_a_consumer():
    store, record, proposal = utility_case()
    store.record_observation(record["outline"]["captureSessionId"], 1, observed(), record["flowId"])
    current = store.get(record["outline"]["captureSessionId"], record["flowId"])
    doc = build_document(current["outline"], current["flowId"], current["snapshotId"], [proposal],
                         observations=current["observations"])
    assert doc["quality"]["analysisComplete"]
    assert doc["stages"][0]["outputCoverage"]["nonImageOutputs"][0]["resourceId"] == 505
    assert doc["observations"]["1"]["expectedOutputs"][0]["resourceType"] == "buffer"
    restored = build_document(current["outline"], current["flowId"], current["snapshotId"], [proposal],
                              observations=doc["observations"])
    assert restored["quality"]["analysisComplete"]


@pytest.mark.parametrize("change", [{"resourceType": "texture"}, {"captureSupported": True}, {"resourceId": 999}])
def test_non_image_certificate_requires_matching_native_action_and_buffer_type(change):
    store, record, _ = utility_case()
    result = observed()
    result["expectedOutputs"][0].update(change)
    with pytest.raises(ValueError, match="Action buffer output"):
        store.record_observation(record["outline"]["captureSessionId"], 1, result, record["flowId"])
