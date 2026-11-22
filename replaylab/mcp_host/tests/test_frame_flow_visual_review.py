from copy import deepcopy
from hashlib import sha256

from PIL import Image
import pytest

from src.frame_flow_visual_review import (
    comparison_compatibility, prepare_visuals, preview_similarity,
)
from src.frame_flow_presentation import process_graph_gaps


def preview(tmp_path, name, event, color, rid=50):
    path = tmp_path / (name + ".png")
    Image.new("RGBA", (192, 128), color).save(path)
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest(),
            "eventId": event, "resourceId": rid, "channel": "rgba", "group": "outputs",
            "attachmentKind": "rtv", "width": 192, "height": 128,
            "title": "Scene color", "description": "Scene color after the declared color operation.",
            "displayMapping": {"rangeMode": "native", "blackPoint": 0.0, "whitePoint": 1.0,
                               "depthInterpretation": None, "mip": 0, "slice": 0, "sample": 0}}


def case(tmp_path, *, graph=True):
    images = [preview(tmp_path, "before", 19, (80, 90, 100, 0)),
              preview(tmp_path, "after", 11, (82, 92, 102, 255))]
    # Execution order, not numeric EID order, defines the two resource versions.
    actions = {event: {"eventId": event, "executionIndex": order, "isAction": True,
                       "flags": 2, "hasWork": True} for order, event in enumerate((19, 11))}
    view = {"mip": 0, "numMips": 1, "slice": 0, "numSlices": 1, "sample": 0}
    observations = {str(event): {"expectedOutputs": [{"kind": "rtv", "resourceId": 50, **view}],
                                "textureInputs": [] if event == 19 else [{"kind": "srv", "resourceId": 50, **view}]}
                    for event in actions}
    algorithm = {"inputs": [{"eventId": 11, "resourceId": 50, "producerEventId": 19,
                              "evidence": "The color operation consumes the earlier scene version."}],
                 "steps": [{"eventId": event, "evidence": "Captured color calculation."} for event in actions],
                 "outputs": [{"eventId": event, "resourceId": 50} for event in actions],
                 "integration": [{"sourceEventId": 19, "resourceId": 50, "mode": "downstream",
                                  "eventId": 11, "targetResourceId": 50, "evidence": "The existing scene is updated."}]}
    stage = {"id": "color", "images": images, "comparisons": [], "animations": [],
             "processSteps": [], "algorithm": algorithm,
             "processGraph": {"nodes": [{"id": "before", "label": "Original scene", "eventId": 19},
                                        {"id": "after", "label": "Adjusted scene", "eventId": 11}],
                              "edges": [{"from": "before", "to": "after"}]} if graph else None}
    return stage, actions, observations


def test_unique_preview_attaches_and_subtle_declared_change_becomes_divider(tmp_path):
    stage, actions, observations = case(tmp_path)
    review = prepare_visuals(stage, actions, observations)
    assert review["linkedGraphImages"] == 2
    assert review["graphPresentation"] == "show"
    assert review["autoComparisons"] == 1
    assert stage["processGraph"]["nodes"][0]["imageIndex"] == 0
    assert stage["processGraph"]["nodes"][1]["comparisonIndex"] == 0
    assert "imageIndex" not in stage["processGraph"]["nodes"][1]
    comparison = stage["comparisons"][0]
    assert (comparison["leftImageIndex"], comparison["rightImageIndex"]) == (0, 1)
    assert comparison["relationship"] == "before-after"
    assert comparison["visualComparable"] and not comparison["numericDifferenceEvidence"]
    metric = review["similarityChecks"][0]
    assert metric["similar"] and metric["sampleWidth"] <= 96 and metric["sampleHeight"] <= 96


def test_algorithm_relationship_can_select_comparison_without_forcing_graph(tmp_path):
    stage, actions, observations = case(tmp_path, graph=False)
    review = prepare_visuals(stage, actions, observations)
    assert review["autoComparisons"] == 1 and review["graphPresentation"] == "omit"
    assert stage["processGraph"] is None


@pytest.mark.parametrize("mutation", [
    lambda stage, actions, obs: stage.update(algorithm={}),
    lambda stage, actions, obs: obs["11"].update(textureInputs=[]),
    lambda stage, actions, obs: stage["images"][1].update(resourceId=99),
    lambda stage, actions, obs: actions[11].update(hasWork=False),
    lambda stage, actions, obs: actions[11].update(executionIndex=-1),
])
def test_similar_images_do_not_invent_a_before_after_relationship(tmp_path, mutation):
    stage, actions, observations = case(tmp_path)
    mutation(stage, actions, observations)
    review = prepare_visuals(stage, actions, observations)
    assert not review["autoComparisons"] and not stage["comparisons"]


def test_ambiguous_mrt_is_not_assigned_to_a_text_operation(tmp_path):
    stage, actions, observations = case(tmp_path)
    stage["images"].append(preview(tmp_path, "another-target", 19, (80, 90, 100, 255), rid=60))
    stage["algorithm"]["outputs"].append({"eventId": 19, "resourceId": 60})
    review = prepare_visuals(stage, actions, observations)
    assert review["linkedGraphImages"] == 1
    assert "imageIndex" not in stage["processGraph"]["nodes"][0]


def test_operation_to_mrt_output_does_not_become_a_false_texture_dependency(tmp_path):
    stage, actions, observations = case(tmp_path)
    stage["images"][1] = preview(tmp_path, "normal", 19, (80, 90, 100, 255), rid=60)
    stage["algorithm"]["outputs"] = [{"eventId": 19, "resourceId": 50}]
    observations["19"]["expectedOutputs"].append({"kind": "rtv", "resourceId": 60})
    stage["processGraph"] = {
        "nodes": [{"id": "calculate", "label": "Calculate material properties", "eventId": 19},
                  {"id": "normal", "label": "Normal output", "eventId": 19, "imageIndex": 1}],
        "edges": [{"from": "calculate", "to": "normal"}],
    }
    assert not process_graph_gaps(stage, actions, observations)
    review = prepare_visuals(stage, actions, observations)
    assert not review["linkedGraphImages"]
    assert "imageIndex" not in stage["processGraph"]["nodes"][0]
    assert stage["processGraph"]["nodes"][1]["imageIndex"] == 1
    assert not process_graph_gaps(stage, actions, observations)


def test_existing_graph_error_does_not_trigger_automatic_media_reinterpretation(tmp_path):
    stage, actions, observations = case(tmp_path)
    stage["processGraph"]["edges"] = [{"from": "after", "to": "before"}]
    before = deepcopy(process_graph_gaps(stage, actions, observations))
    assert before
    review = prepare_visuals(stage, actions, observations)
    assert review["linkedGraphImages"] == 0
    assert all("imageIndex" not in node for node in stage["processGraph"]["nodes"])
    assert process_graph_gaps(stage, actions, observations) == before


@pytest.mark.parametrize("field", ["mip", "slice", "sample"])
def test_optional_wrong_subresource_preview_keeps_the_verified_operation_chain(tmp_path, field):
    stage, actions, observations = case(tmp_path)
    stage["processGraph"]["nodes"][0]["imageIndex"] = 0
    stage["images"][1]["displayMapping"][field] = 1
    # The operation's recorded target is still subresource zero; RID-only
    # checks used to accept this optional image and then hide the whole graph.
    assert not process_graph_gaps(stage, actions, observations)
    review = prepare_visuals(stage, actions, observations)
    assert review["graphPresentation"] == "show"
    assert review["graphPresentationReason"] == "verified-resource-flow"
    assert review["linkedGraphImages"] == 0
    assert "imageIndex" not in stage["processGraph"]["nodes"][1]
    assert not stage["comparisons"]


def test_optional_duplicate_preview_does_not_erase_a_real_later_transfer(tmp_path):
    stage, actions, observations = case(tmp_path)
    stage["processGraph"]["nodes"].insert(1, {"id": "source-output", "eventId": 19, "imageIndex": 0})
    stage["processGraph"]["edges"] = [{"from": "before", "to": "source-output"},
                                      {"from": "source-output", "to": "after"}]
    review = prepare_visuals(stage, actions, observations)
    assert review["graphPresentation"] == "show"
    assert "imageIndex" not in stage["processGraph"]["nodes"][0]
    assert stage["processGraph"]["nodes"][1]["imageIndex"] == 0
    assert review["repeatedGraphSnapshotPathCount"] == 0


def test_text_only_graph_is_retained_as_evidence_but_can_be_omitted_visually(tmp_path):
    stage, actions, observations = case(tmp_path)
    stage["images"] = []
    original = deepcopy(stage["processGraph"])
    review = prepare_visuals(stage, actions, observations)
    assert review["graphPresentation"] == "omit"
    assert stage["processGraph"] == original
    assert not review["autoComparisons"]


@pytest.mark.parametrize("key,value,warning", [
    ("blackPoint", -1, "different-display-mapping"),
    ("whitePoint", 8, "different-display-mapping"),
    ("rangeMode", "visible-distribution", "different-display-mapping"),
    ("depthInterpretation", "linear", "different-display-mapping"),
    ("valueDecode", "float32-bits", "different-value-decoding"),
    ("mip", 1, "different-subresource"),
    ("slice", 1, "different-subresource"),
    ("sample", 1, "different-subresource"),
])
def test_incompatible_preview_mapping_never_automatically_compares(tmp_path, key, value, warning):
    stage, actions, observations = case(tmp_path)
    stage["images"][1]["displayMapping"][key] = value
    if key in ("mip", "slice", "sample"):
        observations["11"]["expectedOutputs"][0][key] = value
    review = prepare_visuals(stage, actions, observations)
    assert not review["autoComparisons"]
    assert warning in review["similarityChecks"][0]["comparisonWarnings"]


def test_large_visual_change_remains_representative_images(tmp_path):
    stage, actions, observations = case(tmp_path)
    stage["images"][1] = preview(tmp_path, "different", 11, (230, 240, 250, 255))
    review = prepare_visuals(stage, actions, observations)
    assert not review["autoComparisons"]
    assert stage["processGraph"]["nodes"][1]["imageIndex"] == 1


def test_existing_comparison_is_not_duplicated_and_small_previews_are_cached(tmp_path):
    stage, actions, observations = case(tmp_path)
    prepare_visuals(stage, actions, observations)
    review = prepare_visuals(stage, actions, observations)
    assert len(stage["comparisons"]) == 1
    assert not review["autoComparisons"]
    assert review["similarityChecks"][0]["cacheHit"]


def test_alpha_data_does_not_hide_rgb_during_comparison(tmp_path):
    white = preview(tmp_path, "alpha-zero-white", 19, (255, 255, 255, 0))
    black = preview(tmp_path, "opaque-black", 11, (0, 0, 0, 255))
    metric = preview_similarity(white, black)
    assert metric["meanAbsoluteDifference"] == 1.0
    assert not metric["similar"]


def test_changed_preview_never_reuses_a_stale_cached_sample(tmp_path):
    stage, actions, observations = case(tmp_path)
    prepare_visuals(stage, actions, observations)
    path = tmp_path / "after.png"
    path.write_bytes(b"changed evidence file")
    metric = preview_similarity(*stage["images"])
    assert metric["status"] == "preview-unavailable" and not metric["similar"]


def test_missing_mapping_is_not_assumed_to_be_compatible_for_similarity(tmp_path):
    stage, _, _ = case(tmp_path)
    for image in stage["images"]:
        image["displayMapping"] = {}
    assert preview_similarity(*stage["images"])["status"] == "mapping-unavailable"


def test_rgb_opaque_overview_and_rgba_receipt_are_compatible(tmp_path):
    stage, _, _ = case(tmp_path)
    stage["images"][0]["channel"] = "rgb"
    assert not comparison_compatibility(*stage["images"])


def test_document_build_associates_existing_graph_media_without_extra_capture():
    from src.frame_render_flow import build_document
    from test_frame_flow_algorithm import case as document_case
    from test_frame_render_flow import _preview

    record, observations, proposal = document_case()
    for observation in observations.values():
        for row in observation["expectedOutputs"] + observation["textureInputs"]:
            row.update(mip=0, numMips=1, slice=0, numSlices=1, sample=0)
    def preview_with_subresource(image, members):
        result = _preview(image, members)
        result["displayMapping"].update(mip=0, slice=0, sample=0)
        return result
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal],
                              observations=observations, image_resolver=preview_with_subresource)
    stage = document["stages"][0]
    assert stage["visualReview"]["linkedGraphImages"] == 1
    assert stage["visualReview"]["graphPresentation"] == "show"
    assert stage["processGraph"]["nodes"][0]["imageIndex"] == 0
    assert not stage["qualityGaps"]
