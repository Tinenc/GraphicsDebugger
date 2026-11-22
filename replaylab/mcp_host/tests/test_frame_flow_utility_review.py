from copy import deepcopy

import pytest

from src.frame_flow_algorithm import algorithm_gaps
from src.frame_render_flow import FrameRenderFlowStore, build_document
from test_frame_render_flow import outline


def utility_case(flags=0x0400):
    actions = {11: {"eventId": 11, "executionIndex": 0, "flags": flags, "isAction": True,
                    "copySource": 30, "copyDestination": 40}}
    stage = {"id": "transfer", "eventIds": [11], "category": "other", "confidence": "captured",
             "summary": "Copies the resolved scene color into the display buffer.",
             "investigation": {"status": "investigated",
                               "evidence": "The captured copy action records source 30 and destination 40."}}
    return stage, actions


@pytest.mark.parametrize("flags", [0x0001, 0x0100, 0x0400, 0x0800, 0x1000, 0x8000])
@pytest.mark.parametrize("category", ["other", "lighting", "postprocess"])
def test_observed_non_shader_operation_needs_no_artificial_algorithm_tables(flags, category):
    stage, actions = utility_case(flags)
    stage["category"] = category
    assert not algorithm_gaps(stage, actions, {})


@pytest.mark.parametrize("flags,kind", [(2, ""), (4, ""), (8, ""), (0x4000, ""),
                                      (0x0400, "draw"), (0x0400, "dispatch")])
def test_shader_members_cannot_bypass_calculation_review_as_utility(flags, kind):
    stage, actions = utility_case(flags)
    actions[11]["actionKind"] = kind
    actions[11]["hasWork"] = False  # Zero work does not make a Shader action a copy.
    stage["category"] = "copy"
    assert algorithm_gaps(stage, actions, {})[0]["code"] == "missing-stage-algorithm"


@pytest.mark.parametrize("mutation", [
    lambda s, a: s.update(eventIds=[]),
    lambda s, a: s.update(eventIds=[99]),
    lambda s, a: a[11].update(isAction=False),
    lambda s, a: a[11].pop("isAction"),
    lambda s, a: s.update(confidence="unknown"),
    lambda s, a: s.update(summary="  "),
    lambda s, a: s.update(summary=None),
    lambda s, a: s["investigation"].update(status="partial"),
    lambda s, a: s["investigation"].update(evidence="  "),
    lambda s, a: s["investigation"].update(evidence=None),
    lambda s, a: s["investigation"].update(evidence={"claimed": "proof"}),
    lambda s, a: s.pop("investigation"),
])
def test_missing_members_or_unconfirmed_explanation_remains_incomplete(mutation):
    stage, actions = utility_case()
    mutation(stage, actions)
    assert algorithm_gaps(stage, actions, {})[0]["code"] == "missing-stage-algorithm"


def test_mixed_stage_requires_algorithm_even_when_navigation_is_a_copy():
    stage, actions = utility_case()
    actions[7] = {"eventId": 7, "executionIndex": -1, "flags": 2, "isAction": True}
    stage["eventIds"] = [7, 11]
    stage["navigationEventId"] = 11
    assert algorithm_gaps(stage, actions, {})[0]["code"] == "missing-stage-algorithm"


def test_optional_utility_algorithm_still_validates_all_declared_links():
    stage, actions = utility_case()
    stage["algorithm"] = {
        "inputs": [{"eventId": 11, "resourceId": 99, "role": "Claimed source", "evidence": "Claimed input"}],
        "steps": [{"eventId": 11, "title": "Copy", "calculation": "Copies the source.", "evidence": "Captured copy"}],
        "outputs": [{"eventId": 11, "resourceId": 40, "role": "Display buffer"}],
        "integration": [],
    }
    codes = {gap["code"] for gap in algorithm_gaps(stage, actions, {})}
    assert {"unverified-algorithm-binding", "missing-algorithm-integration"} <= codes
    stage["algorithm"] = {"inputs": [], "steps": [], "outputs": [], "integration": []}
    assert algorithm_gaps(stage, actions, {})[0]["code"] == "incomplete-stage-algorithm"


def test_explained_copy_publishes_complete_without_shader_shaped_fields():
    data = outline(1)
    data["actions"][0].update(flags=0x0400, outputs=[], copySource=30, copyDestination=40)
    record = FrameRenderFlowStore().prepare(data)
    proposal, _ = utility_case()
    proposal = deepcopy(proposal)
    proposal.pop("eventIds")
    proposal.update(title="Copy the completed image", startEventId=1, endEventId=1)
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal])
    assert document["quality"]["analysisComplete"]
    assert document["stages"][0]["algorithm"] is None
