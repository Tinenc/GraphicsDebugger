from copy import deepcopy

import pytest

from src.frame_render_flow import FrameRenderFlowStore, build_document
from test_frame_render_flow import outline, _reviewed_stage, _preview


def case():
    data = outline(5)
    for row, rid in zip(data["actions"], (100, 200, 999, 300, 300)):
        row["outputs"] = [rid]
    data["actions"][-1].update(flags=0x100, copyDestination=300, outputs=[])
    record = FrameRenderFlowStore().prepare(data)
    observations = {
        str(event): {"expectedOutputs": [{"kind": "rtv", "index": 0, "resourceId": rid}],
                     "textureInputs": [] if input_rid is None else [{"kind": "srv", "resourceId": input_rid}]}
        for event, rid, input_rid in ((1, 100, None), (4, 200, 100), (10, 300, 200))
    }
    algorithm = {
        "inputs": [{"eventId": 4, "resourceId": 100, "role": "Indirect surface illumination", "producerEventId": 1}],
        "steps": [{"eventId": 4, "title": "Combine direct and indirect illumination",
                   "calculation": "Read the indirect light and combine it with the surface light response.",
                   "evidence": "PS lighting expression samples the illumination SRV, scales by albedo and writes SV_Target0."}],
        "outputs": [{"eventId": 4, "resourceId": 200, "role": "Independent lighting layer"}],
        "integration": [{"sourceEventId": 4, "resourceId": 200, "mode": "downstream", "eventId": 10,
                         "targetResourceId": 300, "explanation": "Add the independent lighting layer to scene color.",
                         "evidence": "The composition PS samples resource 200; One+One blending writes scene color 300."}],
    }
    proposal = _reviewed_stage(4, 7, algorithm=algorithm, images=[{"manifestPath": "4-200"}])
    proposal["processGraph"] = {
        "nodes": [{"id": "light", "label": "Surface lighting", "eventId": 4},
                  {"id": "combine", "label": "Combine with scene color", "eventId": 10}],
        "edges": [{"from": "light", "to": "combine"}],
    }
    return record, observations, proposal


def build(record, observations, proposal):
    return build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal],
                          observations=observations, image_resolver=_preview)


def test_key_lighting_event_is_independent_of_later_navigation_copy():
    record, observations, proposal = case()
    doc = build(record, observations, proposal)
    stage = doc["stages"][0]
    assert stage["navigationEventId"] == 7
    assert stage["outputCoverage"]["keyOutputEventIds"] == [4]
    assert stage["outputCoverage"]["expectedResourceIds"] == [200]
    assert not stage["qualityGaps"]
    # Producer and composition can be outside the stage; both survive a reload.
    assert set(doc["observations"]) == {"1", "4", "10"}
    assert doc["observations"]["10"]["textureInputs"] == observations["10"]["textureInputs"]
    restored = build(record, doc["observations"], proposal)
    assert not restored["stages"][0]["qualityGaps"]


@pytest.mark.parametrize("event", [1, 4, 10])
def test_unknown_indirect_workload_preserves_producer_calculation_and_consumer(event):
    record, observations, proposal = case()
    action = next(row for row in record["outline"]["actions"] if row["eventId"] == event)
    action.update(hasWork=None, flags=action["flags"] | 0x080000)  # ActionFlags.Indirect
    doc = build(record, observations, proposal)
    stage = doc["stages"][0]
    assert stage["qualityGaps"][0]["code"] == "unverified-algorithm-binding"
    assert "indirect workload" in stage["qualityGaps"][0]["message"]
    assert set(doc["observations"]) == {"1", "4", "10"}
    assert stage["algorithm"]["inputs"][0]["producerEventId"] == 1
    assert stage["algorithm"]["steps"][0]["eventId"] == 4
    assert stage["algorithm"]["integration"][0]["eventId"] == 10
    assert action["hasWork"] is None
    observations[str(event)]["indirectWorkload"] = {
        "status": "resolved", "source": "native-structured-indirect-arguments",
        "eventId": event, "nonZeroWorkload": True, "workload": {"vertexCount": 3, "instanceCount": 1}}
    assert not build(record, observations, proposal)["stages"][0]["qualityGaps"]
    # Unknown work does not waive observed input/output evidence or imply pixels
    # changed: a missing consumer binding must still leave the explanation partial.
    observations["10"]["textureInputs"] = []
    assert "unverified-algorithm-binding" in {
        gap["code"] for gap in build(record, observations, proposal)["quality"]["gaps"]}


@pytest.mark.parametrize("event", [1, 4, 10])
@pytest.mark.parametrize("indirect", [False, True])
def test_known_zero_workload_cannot_be_claimed_as_executed_algorithm(event, indirect):
    record, observations, proposal = case()
    action = next(row for row in record["outline"]["actions"] if row["eventId"] == event)
    action["hasWork"] = False
    if indirect:
        action["flags"] |= 0x080000
    with pytest.raises(ValueError, match="effective frame action"):
        build(record, observations, proposal)


@pytest.mark.parametrize("mutation,code", [
    (lambda p, o: (p.pop("algorithm"), p.pop("processGraph")), "missing-stage-algorithm"),
    (lambda p, o: p["algorithm"].update(steps=[]), "incomplete-stage-algorithm"),
    (lambda p, o: p["algorithm"].update(inputs=[]), "incomplete-stage-algorithm"),
    (lambda p, o: p["algorithm"]["steps"][0].update(evidence=""), "missing-algorithm-step-evidence"),
    (lambda p, o: (p["algorithm"].update(integration=[]), p.pop("processGraph")), "missing-algorithm-integration"),
    (lambda p, o: p["algorithm"]["integration"][0].pop("targetResourceId"), "missing-algorithm-integration"),
    (lambda p, o: o["10"].update(textureInputs=[]), "unverified-algorithm-binding"),
    (lambda p, o: o["1"].update(expectedOutputs=[]), "unverified-algorithm-binding"),
    (lambda p, o: o["10"].update(expectedOutputs=[]), "unverified-algorithm-binding"),
])
def test_effect_only_or_unproven_composition_cannot_pass_review(mutation, code):
    record, observations, proposal = case()
    mutation(proposal, observations)
    doc = build(record, observations, proposal)
    assert code in {g["code"] for g in doc["stages"][0]["qualityGaps"]}
    assert not doc["quality"]["analysisComplete"]


@pytest.mark.parametrize("mutation,match", [
    (lambda a: a["steps"][0].update(eventId=2), "effective frame action"),
    (lambda a: a["steps"][0].update(eventId=10), "belong to their stage"),
    (lambda a: a["inputs"][0].update(producerEventId=10), "precede its consumer"),
    (lambda a: a["integration"][0].update(eventId=1), "follow its producer"),
    (lambda a: a["integration"][0].update(mode="in-place"), "producing event"),
    (lambda a: a["integration"][0].update(resourceId=999), "declared key output"),
    (lambda a: a.update(steps=a["steps"] * 33), "at most 32"),
    (lambda a: a["steps"][0].update(calculation="bad\x00text"), "bounded plain text"),
])
def test_invalid_calculation_path_is_rejected(mutation, match):
    record, observations, proposal = case()
    mutation(proposal["algorithm"])
    with pytest.raises(ValueError, match=match):
        build(record, observations, proposal)


def test_history_input_needs_explicit_evidence_boundary():
    record, observations, proposal = case()
    source = proposal["algorithm"]["inputs"][0]
    source.pop("producerEventId")
    assert "incomplete-stage-algorithm" in {g["code"] for g in build(record, observations, proposal)["stages"][0]["qualityGaps"]}
    source["evidence"] = "Captured persistent input; usage inventory contains no writer before this draw. Shader samples the previous frame history."
    assert not build(record, observations, proposal)["stages"][0]["qualityGaps"]


def test_non_image_cannot_be_used_to_hide_a_texture_composition():
    record, observations, proposal = case()
    proposal["algorithm"]["integration"][0].update(mode="non-image", eventId=4)
    proposal.pop("processGraph")
    doc = build(record, observations, proposal)
    assert "missing-algorithm-integration" in {g["code"] for g in doc["stages"][0]["qualityGaps"]}


def test_every_key_draw_requires_actual_inspection_not_just_a_description():
    record, observations, proposal = case()
    proposal["algorithm"]["steps"].append({"eventId": 7, "title": "Process another contribution",
        "calculation": "A plausible but uninspected calculation.", "evidence": "A claimed Shader explanation."})
    doc = build(record, observations, proposal)
    assert "unverified-algorithm-binding" in {g["code"] for g in doc["stages"][0]["qualityGaps"]}


def test_intervening_writer_requires_version_explanation_and_survives_reload():
    record, observations, proposal = case()
    # A compute write omitted from action.outputs is still known from its Host
    # inventory. It must not disappear from the verdict when the diagram reloads.
    record["outline"]["actions"][1]["outputs"] = []
    observations["4"]["expectedOutputs"] = [{"kind": "uav", "resourceId": 100}]
    observations["7"] = {"expectedOutputs": [{"kind": "rtv", "index": 0, "resourceId": 200}],
                         "textureInputs": [{"kind": "srv", "resourceId": 100}]}
    algorithm = proposal["algorithm"]
    algorithm["inputs"][0]["eventId"] = 7
    algorithm["steps"][0]["eventId"] = 7
    algorithm["outputs"][0]["eventId"] = 7
    algorithm["integration"][0]["sourceEventId"] = 7
    proposal["images"][0]["manifestPath"] = "7-200"
    before = build(record, observations, proposal)
    assert "4" in before["observations"]
    assert "unverified-algorithm-binding" in {g["code"] for g in before["stages"][0]["qualityGaps"]}
    after = build(record, before["observations"], proposal)
    assert before["quality"]["gaps"] == after["quality"]["gaps"]
    algorithm["inputs"][0]["evidence"] = "EID4 performs additive UAV accumulation, retaining EID1's contribution; the consumer reads the accumulated result."
    assert not build(record, observations, proposal)["stages"][0]["qualityGaps"]


def test_downstream_composition_cannot_ignore_an_intervening_source_write():
    record, observations, proposal = case()
    observations["7"] = {"expectedOutputs": [{"kind": "uav", "resourceId": 200}]}
    doc = build(record, observations, proposal)
    assert "7" in doc["observations"]
    assert "missing-algorithm-integration" in {g["code"] for g in doc["stages"][0]["qualityGaps"]}


@pytest.mark.parametrize("flags", [0x0001, 0x100001, 0x0400, 0x0800, 0x1000])
def test_utility_actual_destination_does_not_inherit_preceding_render_targets(flags):
    record, observations, proposal = case()
    action = next(row for row in record["outline"]["actions"] if row["eventId"] == 7)
    action.update(flags=flags, actionKind="clear", outputs=[200], depthOutput=200,
                  copyDestination=999, hasWork=True)
    # Older pipeline receipts can retain the same unrelated bound RTV/DSV too.
    observations["7"] = {"expectedOutputs": [{"kind": "rtv", "resourceId": 200}]}
    before = build(record, observations, proposal)
    assert not before["stages"][0]["qualityGaps"]
    assert not build(record, before["observations"], proposal)["stages"][0]["qualityGaps"]
    # Clearing/discarding/copying to the layer itself really breaks its version.
    action["copyDestination"] = 200
    assert "missing-algorithm-integration" in {
        gap["code"] for gap in build(record, observations, proposal)["stages"][0]["qualityGaps"]}


@pytest.mark.parametrize("flags,kind", [(0x0001, "clear"), (0x0002, "draw"), (0x0004, "dispatch")])
def test_unresolved_utility_and_actual_shader_writes_remain_candidates(flags, kind):
    record, observations, proposal = case()
    action = next(row for row in record["outline"]["actions"] if row["eventId"] == 7)
    action.update(flags=flags, actionKind=kind, outputs=[200], copyDestination=0)
    assert "missing-algorithm-integration" in {
        gap["code"] for gap in build(record, observations, proposal)["stages"][0]["qualityGaps"]}


def test_markers_and_present_do_not_mutate_the_observed_resource():
    from src.frame_flow_algorithm import write_index

    actions = {
        1: {"flags": 0x0040, "isAction": False, "outputs": [100], "executionIndex": 1},
        2: {"flags": 0x0100, "outputs": [100], "copyDestination": 100, "executionIndex": 2},
        3: {"flags": 0x0002, "outputs": [100], "executionIndex": 3},
    }
    assert write_index(actions, {}) == {100: [(3, 3)]}


@pytest.mark.parametrize("proof_location", ["outline", "observation"])
def test_decoded_zero_indirect_is_not_an_intervening_writer(proof_location):
    record, observations, proposal = case()
    action = next(row for row in record["outline"]["actions"] if row["eventId"] == 7)
    action.update(flags=2 | 0x080000, outputs=[200], hasWork=None)
    observations["7"] = {"expectedOutputs": [{"kind": "rtv", "resourceId": 200}]}
    assert "missing-algorithm-integration" in {
        g["code"] for g in build(record, observations, proposal)["stages"][0]["qualityGaps"]}
    if proof_location == "outline":
        action["hasWork"] = False
    else:
        observations["7"]["details"] = {"indirectWorkload": {
            "status": "resolved", "source": "native-structured-indirect-arguments",
            "eventId": 7, "nonZeroWorkload": False, "workload": {"indexCount": 36, "instanceCount": 0}}}
    assert not build(record, observations, proposal)["stages"][0]["qualityGaps"]


@pytest.mark.parametrize("event", [1, 4, 10, 7])
@pytest.mark.parametrize("proof_location", ["top", "details"])
def test_native_indirect_proof_survives_sidecar_save_load_and_rebuild(tmp_path, event, proof_location):
    from test_frame_flow_persistence import storage

    record, observations, proposal = case()
    capture = tmp_path / "frame.rdc"
    capture.write_bytes(b"captured native indirect arguments")
    record["outline"]["capturePath"] = str(capture)
    action = next(row for row in record["outline"]["actions"] if row["eventId"] == event)
    action.update(hasWork=None, flags=action["flags"] | 0x080000)
    if event == 7:
        # This zero draw is neither a navigation target nor an algorithm event.
        # Its retained target would appear to overwrite the lighting layer if
        # the native proof were dropped. API-only receipts need no binding list.
        action["outputs"] = [200]
        proposal["endEventId"] = 4
        observations["7"] = {}
    decoded = {"status": "resolved", "source": "native-structured-indirect-arguments",
               "eventId": event, "chunkIndex": 123, "commandIndex": 0, "argumentIndex": 0,
               "argumentType": "D3D12_DRAW_INDEXED_ARGUMENTS", "nonZeroWorkload": event != 7,
               "workload": {"indexCount": 36, "instanceCount": 0 if event == 7 else 1}}
    target = observations[str(event)]
    if proof_location == "details":
        target = target.setdefault("details", {"unrelatedInspection": "must not be persisted"})
    target["indirectWorkload"] = decoded
    record = FrameRenderFlowStore().prepare(record["outline"])
    before = build(record, observations, proposal)
    assert not before["stages"][0]["qualityGaps"]
    assert before["observations"][str(event)]["indirectWorkload"] == decoded
    assert "details" not in before["observations"][str(event)]
    if event == 7:
        assert before["stages"][0]["navigationEventId"] == 4
        assert "expectedOutputs" not in before["observations"]["7"]

    # Exercise the actual portable bundle, including its JSON serialization.
    image = tmp_path / "preview.png"
    image.write_bytes(b"previously verified preview fixture")
    for preview in before["stages"][0]["images"]:
        preview["path"] = str(image)
        preview.pop("sha256", None)
    root = tmp_path / "artifacts"
    storage.save(capture, before, record["outline"], root)
    loaded = storage.load(capture, root)
    after = build(record, loaded["document"]["observations"], proposal)
    assert after["quality"]["gaps"] == before["quality"]["gaps"]
    assert after["observations"][str(event)]["indirectWorkload"] == decoded
    assert action["hasWork"] is None


def test_repeated_writes_of_same_rt_need_previews_for_each_key_result():
    record, observations, proposal = case()
    observations["7"] = deepcopy(observations["4"])
    proposal["algorithm"]["steps"].append({"eventId": 7, "title": "Filter the layer", "calculation": "Filter the layer in the current target.", "evidence": "The inspected PS applies the filter before storing the result."})
    proposal["algorithm"]["inputs"].append({"eventId": 7, "resourceId": 100, "role": "Illumination", "producerEventId": 1})
    proposal["algorithm"]["outputs"].append({"eventId": 7, "resourceId": 200, "role": "Filtered lighting"})
    proposal["algorithm"]["integration"].append({"sourceEventId": 7, "eventId": 7, "resourceId": 200, "mode": "in-place", "explanation": "Filter the current layer.", "evidence": "The inspected output binding retains the layer."})
    doc = build(record, observations, proposal)
    assert doc["stages"][0]["outputCoverage"]["missingAttachments"] == [{"kind": "rtv", "index": 0, "resourceId": 200, "eventId": 7}]
    proposal["images"].append(dict(proposal["images"][0], manifestPath="7-200"))
    proposal["processSteps"] = [{"label": "Compute the light layer", "imageIndex": 0},
                                {"label": "Filter the result", "imageIndex": 1}]
    assert not build(record, observations, proposal)["stages"][0]["qualityGaps"]
