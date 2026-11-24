import copy

import pytest

from src.frame_render_flow import FrameRenderFlowStore, build_document, editable_stage
from src.frame_flow_presentation import channel_groups, process_graph
from test_frame_render_flow import outline, stage


def test_saved_stage_has_a_publishable_round_trip_without_host_fields():
    store = FrameRenderFlowStore()
    record = store.prepare(outline(4))
    original = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 10)])
    store.publish(record["flowId"], original)
    stored = store.get(record["outline"]["captureSessionId"], record["flowId"])
    proposals = store.page(stored, section="proposals")["items"]
    assert "eventIds" not in proposals[0] and "qualityGaps" not in proposals[0]
    rebuilt = build_document(record["outline"], record["flowId"], record["snapshotId"], proposals)
    assert rebuilt == original
    quality = store.page(stored, section="quality")
    assert quality["items"] == original["quality"]["gaps"]


def test_reanalysis_retains_evidence_but_redisplay_does_not_complete_review():
    store = FrameRenderFlowStore()
    record = store.prepare(outline(4))
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 4), stage(7, 10)])
    store.publish(record["flowId"], document)
    record = store.begin_reanalysis(record["outline"]["captureSessionId"], record["flowId"])
    assert record["document"] is None
    assert record["baselineDocument"] == document
    assert store.page(record, section="proposals")["items"] == []
    assert store.page(record)["items"] == record["candidates"]
    copy_document = copy.deepcopy(document)
    store.annotate_reanalysis(record, copy_document)
    assert copy_document["reanalysis"]["pendingStageIds"] == ["stage-1", "stage-2"]
    assert len([gap for gap in copy_document["quality"]["gaps"] if gap["code"] == "stage-review-not-refreshed"]) == 2
    store.record_observation(record["outline"]["captureSessionId"], 1, {"expectedOutputs": []}, record["flowId"])
    record = store.get(record["outline"]["captureSessionId"], record["flowId"])
    document["stages"][1]["summary"] = "Explain the newly inspected composition."
    status = store.reanalysis_status(record, document)
    assert not status["reviewComplete"] and status["newlyInspectedEventIds"] == [1]
    assert status["pendingStageIds"] == ["stage-1", "stage-2"]
    document["stages"][0]["investigation"]["evidence"] = (
        "Rechecked the cached source: the earlier draw fills the surface buffer read by the later composite.")
    document["stages"][1]["investigation"]["evidence"] = (
        "Rechecked the sampled input and blend state: retain this composition stage and its earlier producer.")
    status = store.reanalysis_status(record, document)
    assert status["reviewComplete"]
    assert status["semanticCorrectnessProven"] is False


def test_reanalysis_cannot_be_completed_by_renumbering_or_splitting_old_stages():
    store = FrameRenderFlowStore()
    record = store.prepare(outline(4))
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 10)])
    store.publish(record["flowId"], document)
    record = store.begin_reanalysis(record["outline"]["captureSessionId"], record["flowId"])
    original = document["stages"][0]
    document["stages"] = [dict(copy.deepcopy(original), id="new-a", eventIds=[1, 4]),
                          dict(copy.deepcopy(original), id="new-b", eventIds=[7, 10])]
    assert store.reanalysis_status(record, document)["pendingStageIds"] == ["new-a", "new-b"]
    document["stages"][0]["investigation"]["evidence"] = "Source and bindings distinguish buffer preparation from composition."
    assert store.reanalysis_status(record, document)["pendingStageIds"] == ["new-b"]


def test_reanalysis_allows_valid_static_evidence_without_forced_replay():
    store = FrameRenderFlowStore()
    record = store.prepare(outline(1))
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 1)])
    store.publish(record["flowId"], document)
    record = store.begin_reanalysis(record["outline"]["captureSessionId"], record["flowId"])
    document["stages"][0]["investigation"]["evidence"] = (
        "Validated cached Shader reads and the captured blend equation: retain the existing composition interpretation.")
    status = store.reanalysis_status(record, document)
    assert status["reviewComplete"] and status["newlyInspectedEventIds"] == []
    # Another requested analysis starts empty against the latest publication.
    store.publish(record["flowId"], document)
    next_record = store.begin_reanalysis(record["outline"]["captureSessionId"], record["flowId"])
    assert not store.reanalysis_status(next_record)["reviewComplete"]
    assert next_record["document"] is None
    assert next_record["baselineDocument"] == document


def test_reanalysis_deleting_or_reordering_old_evidence_is_not_new_reasoning():
    store = FrameRenderFlowStore()
    record = store.prepare(outline(4))
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 10)])
    item = document["stages"][0]
    item["investigation"]["evidence"] = "Earlier source inspection."
    item["algorithm"] = {"steps": [{"eventId": 1, "calculation": "Prepare", "evidence": "VS writes positions."},
                                    {"eventId": 7, "calculation": "Shade", "evidence": "PS reads surface data."}]}
    store.publish(record["flowId"], document)
    record = store.begin_reanalysis(record["outline"]["captureSessionId"], record["flowId"])
    item["algorithm"]["steps"].reverse()
    assert not store.reanalysis_status(record, document)["reviewComplete"]
    item["algorithm"]["steps"].pop()
    item["investigation"]["evidence"] = ""
    assert not store.reanalysis_status(record, document)["reviewComplete"]


def test_reanalysis_pages_report_current_pending_review_without_mutating_old_publication():
    store = FrameRenderFlowStore()
    record = store.prepare(outline(1))
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 1)])
    # A previously accepted publication is historical acceptance, regardless of
    # whether the new review has collected any additional live data yet.
    document["quality"] = {"analysisComplete": True, "gaps": [], "gapCount": 0}
    store.publish(record["flowId"], document)
    record = store.begin_reanalysis(record["outline"]["captureSessionId"], record["flowId"])
    for section in ("stages", "quality", "proposals"):
        page = store.page(record, section=section)
        assert page["quality"] is None and not page["published"]
        assert page["reanalysis"]["reviewComplete"] is False
        assert page["reanalysis"]["draftStarted"] is False
    baseline = store.page(record, section="baseline")
    assert baseline["items"][0]["summary"] == document["stages"][0]["summary"]
    assert "eventIds" not in baseline["items"][0]
    assert store.get(record["outline"]["captureSessionId"], record["flowId"])["baselineDocument"] == document
    # Retrying preparation without publishing must not erase the comparison.
    retry = store.begin_reanalysis(record["outline"]["captureSessionId"], record["flowId"])
    assert retry["baselineDocument"] == document
    assert not store.reanalysis_status(retry)["reviewComplete"]


def graph():
    return {"nodes": [{"id": "scene", "label": "Scene", "imageIndex": 0},
                      {"id": "effect", "label": "Transform"},
                      {"id": "blend", "label": "Combine", "eventId": 10}],
            "edges": [{"from": "scene", "to": "effect"}, {"from": "effect", "to": "blend"},
                      {"from": "scene", "to": "blend", "label": "Original color"}]}


def test_process_graph_accepts_transform_nodes_and_real_branching_without_images_for_every_step():
    result = process_graph(graph(), [{"eventId": 1}], [], [], {1, 10})
    assert len(result["edges"]) == 3 and "imageIndex" not in result["nodes"][1]


@pytest.mark.parametrize("mutate", [
    lambda value: value["edges"].append({"from": "blend", "to": "scene"}),
    lambda value: value["edges"].append({"from": "scene", "to": "missing"}),
    lambda value: value["nodes"][0].update(eventId=10),
    lambda value: value["nodes"][2].update(eventId=999),
    lambda value: value["nodes"][1].update(imageIndex=1),
    lambda value: value["nodes"].append({"id": "orphan", "label": "Unrelated"}),
])
def test_process_graph_rejects_invalid_resource_links_or_cycles(mutate):
    value = graph()
    mutate(value)
    with pytest.raises(ValueError):
        process_graph(value, [{"eventId": 1}], [], [], {1, 10})


def test_semantic_color_groups_are_disjoint_and_use_actual_components():
    assert [item["channel"] for item in channel_groups([{"channel": "rgb"}, {"channel": "a"}], 4)] == ["rgb", "a"]
    assert channel_groups([], 4) == []
    with pytest.raises(ValueError, match="absent"):
        channel_groups([{"channel": "a"}], 3)
    with pytest.raises(ValueError, match="overlap"):
        channel_groups([{"channel": "rgb"}, {"channel": "r"}], 4)


def test_proposal_strips_only_host_owned_comparison_and_gif_fields():
    value = {"id": "one", "images": [{"manifestPath": "m", "title": "Color", "sha256": "host", "channelGroups": []}],
             "comparisons": [{"leftImageIndex": 0, "rightImageIndex": 1, "visualComparable": True}],
             "animations": [{"imageIndices": [0, 1], "label": "Progress", "gifPath": "host"}]}
    proposal = editable_stage(value)
    assert proposal["images"] == [{"manifestPath": "m", "title": "Color", "channelGroups": []}]
    assert "visualComparable" not in proposal["comparisons"][0]
    assert "gifPath" not in proposal["animations"][0]
