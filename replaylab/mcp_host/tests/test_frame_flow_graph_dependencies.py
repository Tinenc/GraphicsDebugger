"""Resource-flow claims must preserve event versions and their observed roles."""
from copy import deepcopy

import pytest

from src.frame_flow_presentation import process_graph, process_graph_gaps


def case():
    # Execution order deliberately differs from numeric EID order.
    actions = {event: {"eventId": event, "executionIndex": order, "actionKind": "draw",
                       "flags": 2, "hasWork": True}
               for order, event in enumerate((10, 4, 30, 40))}
    observations = {
        "10": {"expectedOutputs": [{"kind": "rtv", "resourceId": 100}], "textureInputs": []},
        "4": {"expectedOutputs": [{"kind": "rtv", "resourceId": 200}],
              "textureInputs": [{"kind": "srv", "resourceId": 100}]},
        "30": {"expectedOutputs": [{"kind": "rtv", "resourceId": 300}],
               "textureInputs": [{"kind": "srv", "resourceId": 200}]},
    }
    stage = {"id": "lighting", "eventIds": [10, 4, 30], "images": [
        {"eventId": 10, "resourceId": "100", "group": "outputs"},
        {"eventId": 4, "resourceId": "ResourceId::200", "group": "outputs"}],
        "algorithm": {
            "inputs": [{"eventId": 4, "resourceId": 100, "producerEventId": 10,
                        "role": "Earlier illumination"}],
            "steps": [{"eventId": 4, "title": "Combine illumination", "evidence": "PS samples light, multiplies albedo and writes color"}],
            "outputs": [{"eventId": 4, "resourceId": 200, "role": "Lit color"}],
            "integration": []},
        "processGraph": {"nodes": [{"id": "source", "label": "Illumination", "imageIndex": 0},
                                    {"id": "result", "label": "Lit color", "imageIndex": 1}],
                         "edges": [{"from": "source", "to": "result"}]}}
    return stage, actions, observations


def codes(stage, actions, observations):
    gaps = process_graph_gaps(stage, actions, observations)
    assert all(set(gap) == {"code", "stageId", "message", "nextAction"} and gap["stageId"] == stage["id"] for gap in gaps)
    return {gap["code"] for gap in gaps}


def test_exact_producer_input_binding_and_calculation_support_resource_flow():
    stage, actions, observations = case()
    assert not codes(stage, actions, observations)


@pytest.mark.parametrize("mutation", [
    lambda stage, obs: stage.pop("algorithm"),
    lambda stage, obs: stage["algorithm"]["inputs"][0].pop("producerEventId"),
    lambda stage, obs: stage["algorithm"]["inputs"][0].update(producerEventId=30),
    lambda stage, obs: stage["algorithm"]["inputs"][0].update(resourceId=999),
    lambda stage, obs: stage["algorithm"]["steps"][0].update(evidence=""),
    lambda stage, obs: obs["10"].update(expectedOutputs=[]),
    lambda stage, obs: obs["4"].update(textureInputs=[]),
    lambda stage, obs: obs["4"].update(expectedOutputs=[]),
    lambda stage, obs: obs["4"].update(bindingsComplete=False),
])
def test_bindings_or_prose_alone_do_not_verify_dependency(mutation):
    stage, actions, observations = case()
    mutation(stage, observations)
    assert codes(stage, actions, observations) == {"unverified-process-data-flow"}


@pytest.mark.parametrize("text_bridge", [False, True])
def test_later_snapshot_cannot_feed_earlier_snapshot_even_through_unanchored_text(text_bridge):
    stage, actions, observations = case()
    graph = stage["processGraph"]
    graph["edges"] = [{"from": "result", "to": "source"}]
    if text_bridge:
        graph["nodes"].append({"id": "explanation", "label": "Reinterpret the data"})
        graph["edges"] = [{"from": "result", "to": "explanation"}, {"from": "explanation", "to": "source"}]
    assert codes(stage, actions, observations) == {"invalid-process-data-flow"}


def test_anchored_operation_time_cannot_be_hidden_by_text_nodes():
    stage, actions, observations = case()
    stage["processGraph"] = {"nodes": [{"id": "later", "label": "Later operation", "eventId": 30},
        {"id": "text", "label": "Description"}, {"id": "early", "label": "Earlier operation", "eventId": 4}],
        "edges": [{"from": "later", "to": "text"}, {"from": "text", "to": "early"}]}
    assert codes(stage, actions, observations) == {"invalid-process-data-flow"}


@pytest.mark.parametrize("text_bridge", [False, True])
def test_same_draw_mrt_outputs_are_parallel_not_a_dependency(text_bridge):
    stage, actions, observations = case()
    stage["images"][0].update(eventId=4, resourceId=201)
    observations["4"]["expectedOutputs"].append({"kind": "rtv", "resourceId": 201})
    if text_bridge:
        stage["processGraph"]["nodes"].append({"id": "text", "label": "Process"})
        stage["processGraph"]["edges"] = [{"from": "source", "to": "text"}, {"from": "text", "to": "result"}]
    assert codes(stage, actions, observations) == {"invalid-process-data-flow"}


def test_one_calculation_can_branch_to_multiple_mrt_outputs():
    stage, actions, observations = case()
    stage["images"][0].update(eventId=4, resourceId=201)
    observations["4"]["expectedOutputs"].append({"kind": "rtv", "resourceId": 201})
    stage["processGraph"]["nodes"].append({"id": "calculation", "label": "Shade surface", "eventId": 4})
    stage["processGraph"]["edges"] = [{"from": "calculation", "to": "source"}, {"from": "calculation", "to": "result"}]
    assert not codes(stage, actions, observations)


def test_input_snapshot_at_consumer_event_is_not_an_mrt_output():
    stage, actions, observations = case()
    stage["images"][0].update(eventId=4, group="inputs", attachmentKind="srv")
    assert not codes(stage, actions, observations)
    # Some APIs expose the same resource as both input and output. The source
    # card's inspected input role must still not be reclassified as parallel MRT.
    observations["4"]["expectedOutputs"].append({"kind": "rtv", "resourceId": 100})
    assert not codes(stage, actions, observations)


def test_input_gallery_can_show_an_earlier_producer_rtv():
    stage, actions, observations = case()
    stage["images"][0].update(group="inputs", attachmentKind="rtv")
    assert not codes(stage, actions, observations)


def test_branch_and_merge_with_multiple_resources_has_no_false_serial_mrt_claim():
    stage, actions, observations = case()
    stage["images"].extend([{"eventId": 4, "resourceId": 201}, {"eventId": 30, "resourceId": 300}])
    observations["4"]["expectedOutputs"].append({"kind": "rtv", "resourceId": 201})
    observations["30"]["textureInputs"].append({"kind": "srv", "resourceId": 201})
    stage["algorithm"]["inputs"].extend([{"eventId": 30, "resourceId": rid, "producerEventId": 4} for rid in (200, 201)])
    stage["algorithm"]["steps"].append({"eventId": 30, "evidence": "PS adds the two lighting layers"})
    stage["algorithm"]["outputs"].append({"eventId": 30, "resourceId": 300})
    stage["processGraph"]["nodes"].extend([{"id": "other", "label": "Second layer", "imageIndex": 2},
                                             {"id": "merged", "label": "Merged scene", "imageIndex": 3}])
    stage["processGraph"]["edges"].extend([{"from": "source", "to": "other"},
        {"from": "result", "to": "merged"}, {"from": "other", "to": "merged"}])
    assert not codes(stage, actions, observations)


def test_same_rid_different_event_versions_are_distinct_and_can_flow_forward():
    stage, actions, observations = case()
    stage["images"][1]["resourceId"] = 100
    observations["4"]["expectedOutputs"][0]["resourceId"] = 100
    stage["algorithm"]["outputs"][0]["resourceId"] = 100
    assert not codes(stage, actions, observations)
    stage["processGraph"]["edges"] = [{"from": "result", "to": "source"}]
    assert codes(stage, actions, observations) == {"invalid-process-data-flow"}


def test_same_rtv_accumulation_needs_exact_producer_and_evidenced_in_place_use():
    stage, actions, observations = case()
    stage["images"][1]["resourceId"] = 100
    observations["4"].update(expectedOutputs=[{"kind": "rtv", "resourceId": 100}], textureInputs=[])
    stage["algorithm"]["outputs"][0]["resourceId"] = 100
    stage["algorithm"]["integration"] = [{"sourceEventId": 4, "eventId": 4, "resourceId": 100,
        "mode": "in-place", "evidence": "Observed additive blending retains the previous scene contributions"}]
    assert not codes(stage, actions, observations)
    stage["algorithm"]["inputs"][0].pop("producerEventId")
    assert codes(stage, actions, observations) == {"unverified-process-data-flow"}


def test_integration_links_exact_producer_to_downstream_output():
    stage, actions, observations = case()
    stage["algorithm"]["inputs"] = []
    stage["algorithm"]["integration"] = [{"sourceEventId": 10, "eventId": 4, "resourceId": 100,
        "targetResourceId": 200, "mode": "downstream", "evidence": "Composition PS reads illumination and writes lit color"}]
    assert not codes(stage, actions, observations)
    stage["algorithm"]["integration"][0]["targetResourceId"] = 999
    assert codes(stage, actions, observations) == {"unverified-process-data-flow"}


def test_incomplete_image_identity_only_checks_known_execution_order():
    stage, actions, observations = case()
    stage["images"][0].pop("resourceId")
    assert not codes(stage, actions, observations)
    stage["images"][0]["eventId"] = 4
    assert not codes(stage, actions, observations)
    stage["images"][0]["eventId"] = 30
    assert codes(stage, actions, observations) == {"invalid-process-data-flow"}


def test_unobserved_parallel_bindings_are_not_asserted_as_mrt_fact():
    stage, actions, observations = case()
    stage["images"][0].update(eventId=4, resourceId=201)
    observations.clear()
    assert codes(stage, actions, observations) == {"unverified-process-data-flow"}


def test_comparison_cannot_conceal_an_all_earlier_set_of_snapshots():
    stage, actions, observations = case()
    stage["images"].append({"eventId": 30, "resourceId": 300})
    stage["comparisons"] = [{"leftImageIndex": 0, "rightImageIndex": 1}]
    stage["processGraph"] = {"nodes": [{"id": "late", "label": "Final scene", "imageIndex": 2},
        {"id": "comparison", "label": "Earlier snapshots", "comparisonIndex": 0}],
        "edges": [{"from": "late", "to": "comparison"}]}
    assert codes(stage, actions, observations) == {"invalid-process-data-flow"}


def test_unknown_indirect_execution_does_not_establish_a_resource_transfer():
    stage, actions, observations = case()
    actions[10].update(hasWork=None, flags=2 | 0x080000)
    assert codes(stage, actions, observations) == {"unverified-process-data-flow"}
    observations["10"]["indirectWorkload"] = {"status": "resolved", "source": "native-structured-indirect-arguments",
        "eventId": 10, "nonZeroWorkload": True}
    assert not codes(stage, actions, observations)


def test_related_external_events_are_allowed_explicitly_not_as_any_frame_event():
    stage, _actions, _observations = case()
    graph = deepcopy(stage["processGraph"])
    graph["nodes"][0]["eventId"] = 10
    with pytest.raises(ValueError, match="related event"):
        process_graph(graph, stage["images"], [], [], {4})
    accepted = process_graph(graph, stage["images"], [], [], {4}, allowed_events={10})
    assert accepted["nodes"][0]["eventId"] == 10
    graph["nodes"][0].pop("imageIndex")
    graph["nodes"][0]["eventId"] = 99
    with pytest.raises(ValueError, match="related event"):
        process_graph(graph, stage["images"], [], [], {4}, allowed_events={10})


def test_no_graph_needs_no_extra_presentation_dependencies():
    stage, actions, observations = case()
    stage.pop("processGraph")
    assert not process_graph_gaps(stage, actions, observations)


@pytest.mark.parametrize("reverse", [False, True])
def test_linear_process_steps_share_dependency_and_execution_order_checks(reverse):
    stage, actions, observations = case()
    stage.pop("processGraph")
    stage["processSteps"] = [{"label": "Illumination", "imageIndex": 0}, {"label": "Lit result", "imageIndex": 1}]
    if reverse:
        stage["processSteps"].reverse()
    before = deepcopy(stage)
    assert codes(stage, actions, observations) == ({"invalid-process-data-flow"} if reverse else set())
    assert stage == before and "processGraph" not in stage


def test_visible_graph_takes_precedence_over_unused_legacy_steps():
    stage, actions, observations = case()
    stage["processSteps"] = [{"label": "Later", "imageIndex": 1}, {"label": "Earlier", "imageIndex": 0}]
    assert not codes(stage, actions, observations)


def test_ui_accepts_only_algorithm_related_external_operation_events(tmp_path):
    from test_frame_flow_panel import document, load_module
    module = load_module()
    value = document(tmp_path)
    stage = value["stages"][0]
    stage["algorithm"] = {"inputs": [{"eventId": 10, "resourceId": 100, "producerEventId": 2,
                                     "role": "Earlier illumination"}], "steps": [], "outputs": [], "integration": []}
    stage["processGraph"] = {"nodes": [{"id": "earlier", "label": "Earlier producer", "eventId": 2},
        {"id": "current", "label": "Current output", "imageIndex": 0}], "edges": [{"from": "earlier", "to": "current"}]}
    assert module.normalize_document(value)["stages"][0]["processGraph"]["nodes"][0]["eventId"] == 2
    stage["images"].append(dict(stage["images"][0], group="inputs", eventId=99))
    stage["processGraph"]["nodes"][0]["eventId"] = 99
    with pytest.raises(ValueError, match="related event"):
        module.normalize_document(value)
