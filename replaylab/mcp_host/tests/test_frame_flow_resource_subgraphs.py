"""Mini graphs describe resource versions, not repeated pictures or arithmetic."""
from copy import deepcopy

import pytest

from src.frame_flow_presentation import process_graph_gaps, process_presentation_review
from src.frame_flow_visual_review import prepare_visuals


def binding(kind, rid, **view):
    return dict({"kind": kind, "resourceId": rid, "mip": 0, "numMips": 1,
                 "slice": 0, "numSlices": 1, "sample": 0}, **view)


def case(*, legacy=False):
    # Execution order intentionally differs from numeric event order.
    actions = {event: {"eventId": event, "executionIndex": order, "isAction": True,
                       "flags": 2, "hasWork": True} for order, event in enumerate((31, 7, 46))}
    observations = {
        "31": {"expectedOutputs": [binding("rtv", 90)], "textureInputs": []},
        "7": {"expectedOutputs": [binding("rtv", 91)], "textureInputs": [binding("srv", 90)]},
        "46": {"expectedOutputs": [binding("rtv", 92)], "textureInputs": [binding("srv", 91)]},
    }
    stage = {"id": "module", "images": [
        {"eventId": 31, "resourceId": 90, "group": "outputs", "channel": "rgba"},
        {"eventId": 7, "resourceId": 91, "group": "outputs", "channel": "rgba"}],
        "comparisons": [], "animations": [], "processSteps": [],
        "algorithm": {"inputs": [{"eventId": 7, "resourceId": 90, "producerEventId": 31}],
                      "steps": [{"eventId": 7, "evidence": "The Shader samples the earlier light target."}],
                      "outputs": [{"eventId": 7, "resourceId": 91}], "integration": []},
        "processGraph": {"nodes": [{"id": "light", "label": "Light", "imageIndex": 0},
                                   {"id": "color", "label": "Lit color", "imageIndex": 1}],
                         "edges": [{"from": "light", "to": "color"}]}}
    if legacy:
        stage["processSteps"] = [{"label": "Light", "imageIndex": 0}, {"label": "Lit color", "imageIndex": 1}]
        stage["processGraph"] = None
    return stage, actions, observations


def display(stage, actions, observations, legacy=False):
    before = deepcopy(stage)
    review = prepare_visuals(stage, actions, observations)
    # These fixtures have no preview bytes and must never cause recapture.
    assert stage["images"] == before["images"]
    assert not review["autoComparisons"]
    return review["processStepsPresentation" if legacy else "graphPresentation"], review


@pytest.mark.parametrize("legacy", [False, True])
def test_distinct_targets_with_proven_cross_event_dependency_keep_graph(legacy):
    stage, actions, observations = case(legacy=legacy)
    assert display(stage, actions, observations, legacy)[0] == "show"
    assert not process_graph_gaps(stage, actions, observations)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("different_display", [False, True])
def test_same_exact_snapshot_under_three_arithmetic_labels_is_only_one_result(legacy, different_display):
    stage, actions, observations = case(legacy=legacy)
    stage["images"][1] = deepcopy(stage["images"][0])
    stage["images"][1]["resourceId"] = "ResourceId::90"
    if different_display:
        stage["images"][1].update(channel="r", displayMapping={"blackPoint": 0, "whitePoint": 20})
    if legacy:
        stage["processSteps"].append({"label": "Output", "imageIndex": 0})
    else:
        stage["processGraph"]["nodes"].append({"id": "output", "label": "Output", "imageIndex": 0})
        stage["processGraph"]["edges"].append({"from": "color", "to": "output"})
    original = deepcopy(stage)
    decision, review = display(stage, actions, observations, legacy)
    assert decision == "omit"
    assert review["repeatedStepSnapshotPathCount" if legacy else "repeatedGraphSnapshotPathCount"] == 3
    assert stage == original
    # Redundant presentation alone does not block otherwise valid analysis.
    assert not process_graph_gaps(stage, actions, observations)


@pytest.mark.parametrize("legacy", [False, True])
def test_text_between_duplicate_snapshots_cannot_fabricate_a_conversion(legacy):
    stage, actions, observations = case(legacy=legacy)
    stage["images"][1] = deepcopy(stage["images"][0])
    if legacy:
        stage["processSteps"].insert(1, {"label": "Internal arithmetic"})
    else:
        stage["processGraph"]["nodes"].append({"id": "math", "label": "Internal arithmetic"})
        stage["processGraph"]["edges"] = [{"from": "light", "to": "math"}, {"from": "math", "to": "color"}]
    assert display(stage, actions, observations, legacy)[0] == "omit"


@pytest.mark.parametrize("legacy", [False, True])
def test_single_draw_input_and_output_are_results_not_a_multidraw_subgraph(legacy):
    stage, actions, observations = case(legacy=legacy)
    stage["images"][0].update(eventId=7, attachmentKind="srv", group="inputs")
    assert not process_graph_gaps(stage, actions, observations)
    assert display(stage, actions, observations, legacy)[0] == "omit"


def test_single_draw_mrt_branch_remains_parallel_results():
    stage, actions, observations = case()
    stage["images"][0].update(eventId=7, resourceId=93)
    observations["7"]["expectedOutputs"].append(binding("rtv", 93))
    stage["processGraph"]["nodes"].append({"id": "shade", "label": "Shade", "eventId": 7})
    stage["processGraph"]["edges"] = [{"from": "shade", "to": "light"}, {"from": "shade", "to": "color"}]
    assert not process_graph_gaps(stage, actions, observations)
    assert display(stage, actions, observations)[0] == "omit"


@pytest.mark.parametrize("legacy", [False, True])
def test_parallel_mrt_outputs_cannot_be_presented_as_serial_processing(legacy):
    stage, actions, observations = case(legacy=legacy)
    stage["images"][0].update(eventId=7, resourceId=93)
    observations["7"]["expectedOutputs"].append(binding("rtv", 93))
    assert {gap["code"] for gap in process_graph_gaps(stage, actions, observations)} == {"invalid-process-data-flow"}
    assert display(stage, actions, observations, legacy)[0] == "omit"


@pytest.mark.parametrize("legacy", [False, True])
def test_different_events_using_same_target_are_not_collapsed(legacy):
    stage, actions, observations = case(legacy=legacy)
    stage["images"][1]["resourceId"] = 90
    observations["7"]["expectedOutputs"][0]["resourceId"] = 90
    stage["algorithm"]["outputs"][0]["resourceId"] = 90
    decision, review = display(stage, actions, observations, legacy)
    assert decision == "show"
    assert review["repeatedStepSnapshotPathCount" if legacy else "repeatedGraphSnapshotPathCount"] == 0


@pytest.mark.parametrize("field", ["mip", "slice", "sample"])
def test_distinct_subresources_of_one_event_are_not_claimed_as_duplicate_or_new_work(field):
    stage, actions, observations = case()
    stage["images"][1] = deepcopy(stage["images"][0])
    stage["images"][1]["displayMapping"] = {field: 1}
    decision, review = display(stage, actions, observations)
    assert decision == "omit"
    assert review["repeatedGraphSnapshotPathCount"] == 0
    assert review["verifiedGraphTransferCount"] == 0


@pytest.mark.parametrize("mutation", [
    lambda stage, actions, observations: stage.update(algorithm={}),
    lambda stage, actions, observations: observations["7"].update(textureInputs=[]),
    lambda stage, actions, observations: actions[31].update(hasWork=False),
    lambda stage, actions, observations: actions[31].update(hasWork=None, flags=2 | 0x080000),
    lambda stage, actions, observations: observations["7"].update(bindingsComplete=False),
])
def test_event_order_and_images_alone_do_not_establish_a_subgraph(mutation):
    stage, actions, observations = case()
    mutation(stage, actions, observations)
    assert display(stage, actions, observations)[0] == "omit"


def test_supported_branch_and_merge_across_targets_remains_visible():
    stage, actions, observations = case()
    stage["images"].extend([{"eventId": 7, "resourceId": 93, "channel": "rgba", "group": "outputs"},
                            {"eventId": 46, "resourceId": 92, "channel": "rgba", "group": "outputs"}])
    observations["7"]["expectedOutputs"].append(binding("rtv", 93))
    observations["46"]["textureInputs"].append(binding("srv", 93))
    stage["algorithm"]["inputs"].extend([{"eventId": 46, "resourceId": rid, "producerEventId": 7} for rid in (91, 93)])
    stage["algorithm"]["steps"].append({"eventId": 46, "evidence": "The Shader adds the two light layers."})
    stage["algorithm"]["outputs"].append({"eventId": 46, "resourceId": 92})
    stage["processGraph"]["nodes"].extend([{"id": "other", "label": "Other layer", "imageIndex": 2},
                                         {"id": "merged", "label": "Merged scene", "imageIndex": 3}])
    stage["processGraph"]["edges"].extend([{"from": "light", "to": "other"},
                                         {"from": "color", "to": "merged"}, {"from": "other", "to": "merged"}])
    assert display(stage, actions, observations)[0] == "show"
    assert not process_graph_gaps(stage, actions, observations)


def test_shared_source_in_separate_branches_is_not_a_serial_duplicate():
    stage, actions, observations = case()
    stage["processGraph"]["nodes"].append({"id": "same-source", "label": "Source for second branch", "imageIndex": 0})
    stage["processGraph"]["edges"].append({"from": "same-source", "to": "color"})
    decision, review = display(stage, actions, observations)
    assert decision == "show" and review["repeatedGraphSnapshotPathCount"] == 0


def test_comparison_of_real_versions_does_not_collapse_to_its_shared_before_snapshot():
    stage, actions, observations = case()
    stage["comparisons"] = [{"leftImageIndex": 0, "rightImageIndex": 1}]
    stage["processGraph"]["nodes"][1].pop("imageIndex")
    stage["processGraph"]["nodes"][1]["comparisonIndex"] = 0
    assert display(stage, actions, observations)[0] == "show"


def test_graph_and_legacy_are_reviewed_independently_without_rewriting_either():
    stage, actions, observations = case()
    stage["processSteps"] = [{"label": "Arithmetic", "imageIndex": 0}, {"label": "Result", "imageIndex": 0}]
    original = deepcopy(stage)
    _, review = display(stage, actions, observations)
    assert review["graphPresentation"] == "show" and review["processStepsPresentation"] == "omit"
    assert stage == original


def test_text_operations_need_evidenced_cross_event_transfer_even_with_one_image():
    stage, actions, observations = case()
    stage["processGraph"]["nodes"][1].pop("imageIndex")
    stage["processGraph"]["nodes"][1]["eventId"] = 7
    assert process_presentation_review(stage, actions, observations)["presentation"] == "show"
    stage["algorithm"] = {}
    assert process_presentation_review(stage, actions, observations)["presentation"] == "omit"


def test_one_valid_path_does_not_license_an_unrelated_cross_draw_text_arrow():
    stage, actions, observations = case()
    stage["processGraph"]["nodes"].append({"id": "later", "label": "Uninvestigated later work", "eventId": 46})
    stage["processGraph"]["edges"].append({"from": "color", "to": "later"})
    decision, review = display(stage, actions, observations)
    assert decision == "omit"
    assert review["verifiedGraphTransferCount"] > 0
    assert review["unverifiedGraphTransferPathCount"] > 0
    assert review["graphPresentationReason"] == "unverified-resource-transfer"


def test_presentation_reason_is_stable_and_no_new_analysis_blocker_is_added():
    stage, actions, observations = case()
    assert display(stage, actions, observations)[1]["graphPresentationReason"] == "verified-resource-flow"
    stage["images"][1] = deepcopy(stage["images"][0])
    assert display(stage, actions, observations)[1]["graphPresentationReason"] == "repeated-resource-snapshot"
    assert not process_graph_gaps(stage, actions, observations)
    stage["processGraph"] = None
    assert display(stage, actions, observations)[1]["graphPresentationReason"] == "no-subgraph"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("field", ["mip", "slice", "sample"])
def test_same_rid_cannot_transfer_a_different_subresource_to_consumer(legacy, field):
    stage, actions, observations = case(legacy=legacy)
    stage["images"][0]["displayMapping"] = {field: 1}
    observations["31"]["expectedOutputs"][0][field] = 1
    decision, review = display(stage, actions, observations, legacy)
    assert decision == "omit"
    prefix = "processSteps" if legacy else "graph"
    assert review[prefix + "PresentationReason"] == "unverified-subresource-mapping"
    assert not review["verifiedStepTransferCount" if legacy else "verifiedGraphTransferCount"]


@pytest.mark.parametrize("field", ["mip", "slice", "sample"])
def test_unknown_subresource_mapping_is_not_claimed_as_verified(field):
    stage, actions, observations = case()
    observations["7"]["textureInputs"][0].pop(field)
    decision, review = display(stage, actions, observations)
    assert decision == "omit" and review["graphPresentationReason"] == "unverified-subresource-mapping"
    assert not process_graph_gaps(stage, actions, observations)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("missing", [True, False])
def test_legacy_inventory_retains_proven_base_subresource_without_range_extent(legacy, missing):
    stage, actions, observations = case(legacy=legacy)
    for observation in observations.values():
        for row in observation["expectedOutputs"] + observation["textureInputs"]:
            for field in ("numMips", "numSlices"):
                if missing:
                    row.pop(field)
                else:
                    row[field] = None
    assert display(stage, actions, observations, legacy)[0] == "show"
    # This is a read-only interpretation of old evidence, not a data migration
    # claiming the unknown native view consisted of a single mip/layer.
    assert all((field not in row if missing else row[field] is None)
               for observation in observations.values()
               for row in observation["expectedOutputs"] + observation["textureInputs"]
               for field in ("numMips", "numSlices"))


@pytest.mark.parametrize("field,count", [("mip", "numMips"), ("slice", "numSlices")])
def test_legacy_unknown_extent_never_proves_subresource_above_known_base(field, count):
    stage, actions, observations = case()
    observations["7"]["textureInputs"][0].pop(count)
    observations["31"]["expectedOutputs"][0][field] = 1
    stage["images"][0]["displayMapping"] = {field: 1}
    assert display(stage, actions, observations)[0] == "omit"
    # A non-zero base is just as valid when the consumer explicitly has it.
    observations["7"]["textureInputs"][0][field] = 1
    assert display(stage, actions, observations)[0] == "show"


@pytest.mark.parametrize("count", ["numMips", "numSlices"])
@pytest.mark.parametrize("value", [0, -1, False, "1"])
def test_explicit_invalid_extent_is_not_treated_as_legacy_unknown(count, value):
    stage, actions, observations = case()
    observations["7"]["textureInputs"][0][count] = value
    assert display(stage, actions, observations)[0] == "omit"


@pytest.mark.parametrize("field,count", [("mip", "numMips"), ("slice", "numSlices")])
def test_srv_range_can_include_producer_subresource_above_base(field, count):
    stage, actions, observations = case()
    stage["images"][0]["displayMapping"] = {field: 2}
    observations["31"]["expectedOutputs"][0][field] = 2
    observations["7"]["textureInputs"][0][count] = 3
    assert display(stage, actions, observations)[0] == "show"
    # The upper bound is exclusive; another subresource is not covered.
    observations["7"]["textureInputs"][0][count] = 2
    assert display(stage, actions, observations)[0] == "omit"


def test_media_must_anchor_the_transferred_mip_when_one_rid_has_multiple_outputs():
    stage, actions, observations = case()
    observations["31"]["expectedOutputs"].append(binding("rtv", 90, mip=1))
    stage["images"][0]["displayMapping"] = {"mip": 1}
    # Producer wrote both mips, but the consumer only reads mip 0.
    assert display(stage, actions, observations)[0] == "omit"
    stage["images"][0]["displayMapping"]["mip"] = 0
    assert display(stage, actions, observations)[0] == "show"


def test_explicit_copy_mapping_may_read_one_mip_and_write_another():
    stage, actions, observations = case()
    actions[7].update(flags=0x0400, copySource=90, copyDestination=91,
                      copySourceSubresource={"mip": 1, "slice": 2, "sample": 0},
                      copyDestinationSubresource={"mip": 3, "slice": 4, "sample": 0})
    observations["31"]["expectedOutputs"][0].update(mip=1, slice=2)
    # Native copy operands provide exact mapping even with no Shader bindings.
    observations["7"].update(expectedOutputs=[], textureInputs=[])
    stage["images"][0]["displayMapping"] = {"mip": 1, "slice": 2, "sample": 0}
    stage["images"][1]["displayMapping"] = {"mip": 3, "slice": 4, "sample": 0}
    stage["algorithm"]["steps"][0]["evidence"] = "Native CopyTextureRegion maps source mip 1/slice 2 to destination mip 3/slice 4."
    assert display(stage, actions, observations)[0] == "show"
    actions[7].pop("copySourceSubresource")
    assert display(stage, actions, observations)[0] == "omit"


def test_unrelated_media_on_a_text_branch_cannot_decorate_verified_operations():
    stage, actions, observations = case()
    stage["images"] = [{"eventId": 31, "resourceId": 99, "channel": "rgba", "group": "outputs"}]
    observations["31"]["expectedOutputs"].append(binding("rtv", 99))
    stage["processGraph"] = {
        "nodes": [{"id": "producer", "eventId": 31}, {"id": "consumer", "eventId": 7},
                  {"id": "picture", "imageIndex": 0}, {"id": "note", "label": "Explanation"}],
        "edges": [{"from": "producer", "to": "consumer"}, {"from": "producer", "to": "note"},
                  {"from": "picture", "to": "note"}]}
    decision, review = display(stage, actions, observations)
    assert decision == "omit" and review["graphPresentationReason"] == "no-representative-media"
    assert review["verifiedGraphTransferCount"] == 1 and review["verifiedGraphMediaTransferCount"] == 0


def test_reversed_producer_edge_does_not_create_a_precise_resource_path():
    stage, actions, observations = case()
    observations["46"]["expectedOutputs"] = [binding("rtv", 90)]
    stage["algorithm"]["inputs"][0]["producerEventId"] = 46
    stage["images"][0]["eventId"] = 46
    assert display(stage, actions, observations)[0] == "omit"


@pytest.mark.parametrize("field,count", [("mip", "numMips"), ("slice", "numSlices")])
def test_wide_producer_view_cannot_move_selected_image_to_other_subresource(field, count):
    stage, actions, observations = case()
    observations["31"]["expectedOutputs"][0][count] = 4
    observations["7"]["textureInputs"][0][field] = 1
    stage["images"][0]["displayMapping"] = {field: 2}
    assert display(stage, actions, observations)[0] == "omit"
    stage["images"][0]["displayMapping"][field] = 1
    assert display(stage, actions, observations)[0] == "show"


def test_multiple_transfer_edges_preserve_subresource_until_actual_calculation():
    stage, actions, observations = case()
    # A Present transfer preserves the resource; it is not an input->output
    # algorithm that could remap it into a different mip.
    actions[7].update(flags=0x0100, copyDestination=90)
    observations["31"]["expectedOutputs"][0].update(numMips=4)
    observations["7"].update(expectedOutputs=[binding("present", 90, numMips=4)], textureInputs=[])
    stage["images"][0]["displayMapping"] = {"mip": 1}
    stage["images"][1].update(resourceId=90, displayMapping={"mip": 2})
    stage["algorithm"] = {"inputs": [], "steps": [], "outputs": [], "integration": [
        {"sourceEventId": 31, "eventId": 7, "resourceId": 90, "mode": "present", "evidence": "Presents the unchanged resource."}]}
    assert display(stage, actions, observations)[0] == "omit"
    stage["images"][1]["displayMapping"]["mip"] = 1
    assert display(stage, actions, observations)[0] == "show"
