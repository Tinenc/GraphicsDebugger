"""Module presentation follows captured relationships, not draw or ID counts."""
from copy import deepcopy

import pytest

from src.frame_render_flow import _stage_quality, _has_rendered_texture_transfer
from test_frame_flow_algorithm import case, build


@pytest.mark.parametrize("events,resources", [((1, 4, 10), (100, 200, 300)),
                                             ((902, 3, 71), (875, 24, 601))])
def test_rendered_transfer_is_independent_of_specific_ids_and_numeric_order(events, resources):
    first, middle, last = events
    source, intermediate, final = resources
    stage = {"algorithm": {"inputs": [{"eventId": middle, "producerEventId": first, "resourceId": source}],
                           "integration": [{"mode": "downstream", "sourceEventId": middle,
                                            "eventId": last, "resourceId": intermediate,
                                            "targetResourceId": final}]}}
    observations = {str(event): {"expectedOutputs": [{"kind": "rtv", "resourceId": rid}],
                                "textureInputs": [{"resourceId": input_id}] if input_id else []}
                    for event, rid, input_id in ((first, source, None), (middle, intermediate, source),
                                                (last, final, intermediate))}
    assert _has_rendered_texture_transfer(stage, observations)
    # Repeating the same-target accumulation is not a cross-target algorithm.
    for row in observations.values():
        row["expectedOutputs"] = [{"kind": "rtv", "resourceId": source}]
    assert not _has_rendered_texture_transfer(stage, observations)


def test_independent_mrt_or_static_material_inputs_do_not_force_a_process_graph():
    stage = {"algorithm": {"steps": [{"eventId": 5}, {"eventId": 9}],
                           "inputs": [{"eventId": 9, "resourceId": 100,
                                       "evidence": "Static material texture"}]}}
    observations = {"9": {"textureInputs": [{"resourceId": 100}],
                           "expectedOutputs": [{"kind": "rtv", "resourceId": 200},
                                               {"kind": "rtv", "resourceId": 300}]}}
    assert not _has_rendered_texture_transfer(stage, observations)


def test_buffer_processing_does_not_force_a_texture_graph():
    stage = {"algorithm": {"inputs": [{"eventId": 9, "producerEventId": 5, "resourceId": 100}]}}
    observations = {"5": {"expectedOutputs": [{"kind": "uav", "resourceType": "buffer", "resourceId": 100}]},
                    "9": {"inputs": [{"resourceId": 100}],
                          "expectedOutputs": [{"kind": "uav", "resourceType": "buffer", "resourceId": 200}]}}
    assert not _has_rendered_texture_transfer(stage, observations)


def test_observed_transfer_does_not_force_an_unhelpful_text_graph():
    record, observations, proposal = case()
    proposal.pop("processGraph")
    doc = build(record, observations, proposal)
    assert len(proposal["algorithm"]["steps"]) == 1
    assert "missing-module-data-flow" not in {row["code"] for row in doc["quality"]["gaps"]}
    assert not doc["stages"][0]["qualityGaps"]
    assert doc["stages"][0]["visualReview"]["graphPresentation"] == "omit"
    stage = deepcopy(doc["stages"][0])
    stage["algorithm"] = {"steps": [{"eventId": 4}, {"eventId": 7}]}
    assert "missing-module-data-flow" not in {row["code"] for row in _stage_quality(stage, None, observations)}


def test_supported_function_does_not_need_an_invented_technique_name():
    record, observations, proposal = case()
    proposal["techniques"] = []
    assert not build(record, observations, proposal)["stages"][0]["qualityGaps"]
    # Removing technique labels never waives investigation or calculation evidence.
    proposal["investigation"]["evidence"] = ""
    assert "missing-investigation-evidence" in {row["code"] for row in build(record, observations, proposal)["quality"]["gaps"]}
