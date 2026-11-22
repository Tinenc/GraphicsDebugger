"""Dependency disputes must expose evidence/routes and resolve proven disjoint views."""
from copy import deepcopy

import pytest

from src.frame_flow_algorithm import algorithm_gaps, _dependency_investigation
from src.frame_render_flow import FrameRenderFlowStore, editable_stage
from src.frame_resource_usage import usage_query
from test_frame_flow_algorithm import case, build


def disputed():
    record, observations, proposal = case()
    record["outline"]["actions"][2]["outputs"] = [200]
    observations["7"] = {"bindingsComplete": True, "expectedOutputs": [
        {"kind": "uav", "resourceId": 200, "resourceType": "texture",
         "mip": 1, "numMips": 1, "slice": 0, "numSlices": 1}]}
    observations["10"]["textureInputs"][0].update(
        resourceType="texture", mip=0, numMips=1, slice=0, numSlices=1)
    return record, observations, proposal


def algorithm_review(record, observations, proposal):
    actions = {row["eventId"]: row for row in record["outline"]["actions"]}
    return algorithm_gaps(dict(proposal, id="effect"), actions, observations, flow_id=record["flowId"])


@pytest.mark.parametrize("axis", ["mip", "slice", "byte"])
def test_known_disjoint_write_does_not_break_dependency(axis):
    record, observations, proposal = disputed()
    writer = observations["7"]["expectedOutputs"][0]
    reader = observations["10"]["textureInputs"][0]
    if axis == "slice":
        writer.update(mip=0, slice=2)
    elif axis == "byte":
        writer.update(resourceType="buffer", mip=0, byteOffset=64, byteSize=32)
        reader.update(resourceType="buffer", byteOffset=0, byteSize=64)
    assert not algorithm_review(record, observations, proposal)
    # Intervals are half-open, and even one byte/mip/layer of overlap matters.
    writer[{"mip": "mip", "slice": "slice", "byte": "byteOffset"}[axis]] = 63 if axis == "byte" else 0
    assert any(gap["code"] == "missing-algorithm-integration"
               for gap in algorithm_review(record, observations, proposal))


@pytest.mark.parametrize("mutation", [
    lambda w, r, o: w.pop("numMips"),
    lambda w, r, o: w.update(numMips=None),
    lambda w, r, o: w.update(numMips=0),
    lambda w, r, o: w.update(numMips=True),
    lambda w, r, o: r.pop("numMips"),
    lambda w, r, o: r.update(numMips=2),
    lambda w, r, o: o["7"].update(bindingsComplete=False),
    lambda w, r, o: o["10"].update(bindingErrors=[{"error": "SRV inspection failed"}]),
    lambda w, r, o: o["7"]["expectedOutputs"].append(dict(w, mip=0)),
    lambda w, r, o: o["10"]["textureInputs"].append(dict(r, mip=1)),
    lambda w, r, o: o["10"].update(inputs=[{"resourceId": 200}]),
    lambda w, r, o: (w.update(resourceType="buffer"), r.update(resourceType="buffer")),
])
def test_incomplete_or_overlapping_view_evidence_cannot_be_pruned(mutation):
    record, observations, proposal = disputed()
    mutation(observations["7"]["expectedOutputs"][0], observations["10"]["textureInputs"][0], observations)
    assert any(gap["code"] == "missing-algorithm-integration"
               for gap in algorithm_review(record, observations, proposal))


@pytest.mark.parametrize("flags", [0x1, 0x400, 0x800, 0x1000])
def test_utility_default_subresource_never_means_single_mip(flags):
    record, observations, proposal = disputed()
    action = record["outline"]["actions"][2]
    action.update(flags=flags, actionKind="utility", copyDestination=200,
                  copyDestinationSubresource={"mip": 0, "slice": 0, "sample": 0})
    observations["10"]["textureInputs"][0]["mip"] = 2
    # The inherited UAV says mip1, the generic native action says mip0, but a
    # whole-resource copy/clear can affect mip2. Only exact API operands decide.
    gaps = algorithm_review(record, observations, proposal)
    dispute = next(gap for gap in gaps if gap.get("dependencyEvidence", {}).get("writeCandidateCount"))
    candidate = dispute["dependencyEvidence"]["writeCandidates"][0]
    assert candidate["eventId"] == 7 and candidate["copyDestination"] == 200
    assert candidate["copyDestinationSubresource"]["mip"] == 0
    assert any(row["tool"] == "inspect_event_api_parameters" and row["arguments"]["event_id"] == 7
               for row in dispute["recoveryActions"])


def test_input_explanation_does_not_waive_missing_writer_observation():
    record, observations, proposal = case()
    record["outline"]["actions"][1]["outputs"] = [100]
    observations["7"] = deepcopy(observations["4"])
    observations.pop("4")
    proposal.pop("processGraph")
    algorithm = proposal["algorithm"]
    for key in ("inputs", "steps", "outputs"):
        algorithm[key][0]["eventId"] = 7
    algorithm["integration"][0]["sourceEventId"] = 7
    algorithm["inputs"][0]["evidence"] = "The original contribution survives additive accumulation."
    gaps = algorithm_review(record, observations, proposal)
    dispute = next(gap for gap in gaps if gap.get("dependencyEvidence", {}).get("writeCandidateCount"))
    assert dispute["dependencyEvidence"]["producer"]["eventId"] == 1
    assert dispute["dependencyEvidence"]["consumer"]["eventId"] == 7
    assert dispute["dependencyEvidence"]["writeCandidates"][0]["bindingsObserved"] is False
    # Native observations establish what the calculation explanation refers to.
    observations["4"] = {"expectedOutputs": [{"kind": "uav", "resourceId": 100}]}
    assert not algorithm_review(record, observations, proposal)
    record["outline"]["actions"][1].update(flags=0x80002, hasWork=None)
    assert algorithm_review(record, observations, proposal)


def test_input_with_proven_disjoint_writer_needs_no_invented_accumulation_claim():
    record, observations, proposal = case()
    record["outline"]["actions"][1]["outputs"] = [100]
    observations["7"] = deepcopy(observations["4"])
    observations["7"]["textureInputs"][0].update(mip=0, numMips=1)
    observations["4"] = {"expectedOutputs": [{"kind": "uav", "resourceId": 100, "mip": 2, "numMips": 1}]}
    proposal.pop("processGraph")
    for key in ("inputs", "steps", "outputs"):
        proposal["algorithm"][key][0]["eventId"] = 7
    proposal["algorithm"]["integration"][0]["sourceEventId"] = 7
    assert not proposal["algorithm"]["inputs"][0].get("evidence")
    assert not algorithm_review(record, observations, proposal)


def test_recovery_reports_execution_order_bounded_candidates_and_valid_usage_arguments():
    actions = {event: {"eventId": event, "executionIndex": order, "flags": 2, "hasWork": True,
                       "outputs": [200]} for order, event in enumerate(range(100, 83, -1))}
    rows = _dependency_investigation(actions, {}, 200, 100, 84, list(range(85, 100)), flow_id="flow")
    evidence = rows["dependencyEvidence"]
    assert evidence["writeCandidateCount"] == 15 and evidence["writeCandidatesTruncated"]
    assert [row["eventId"] for row in evidence["writeCandidates"]] == list(range(99, 87, -1))
    assert evidence["candidateOnly"] and rows["recoveryPolicy"]["incompleteUsageDoesNotProveNoWriter"]
    for action in rows["recoveryActions"]:
        if action["tool"] == "inspect_frame_resource_usage":
            arguments = dict(action["arguments"])
            assert arguments.pop("flow_id") == "flow"
            assert arguments.pop("refresh") is False
            checked = usage_query({"outline": {"actions": list(actions.values())}}, **arguments)
            assert checked["requestedOrder"] == "nearest"
            assert checked["anchorEventId"] in (100, 84)


def test_gap_metadata_survives_paging_sidecar_and_new_session_rebuild(tmp_path):
    from test_frame_flow_persistence import storage

    record, observations, proposal = disputed()
    observations["7"]["expectedOutputs"][0]["mip"] = 0
    capture = tmp_path / "frame.rdc"
    capture.write_bytes(b"dependency investigation capture fixture")
    record["outline"]["capturePath"] = str(capture)
    record = FrameRenderFlowStore().prepare(record["outline"])
    document = build(record, observations, proposal)
    record["document"] = document
    page = FrameRenderFlowStore.page(record, section="quality")
    dispute = next(gap for gap in page["items"] if gap.get("dependencyEvidence", {}).get("writeCandidateCount"))
    assert dispute == next(gap for gap in document["quality"]["gaps"] if gap.get("dependencyEvidence", {}).get("writeCandidateCount"))
    assert all(row["arguments"].get("flow_id", record["flowId"]) == record["flowId"] for row in dispute["recoveryActions"])
    image = tmp_path / "preview.png"
    image.write_bytes(b"already verified preview fixture")
    for preview in document["stages"][0]["images"]:
        preview.update(path=str(image))
        preview.pop("sha256", None)
    storage.save(capture, document, record["outline"], tmp_path / "artifacts")
    loaded = storage.load(capture, tmp_path / "artifacts")["document"]
    assert loaded["quality"]["gaps"] == document["quality"]["gaps"]
    current = deepcopy(record["outline"])
    current.update(captureSessionId="b" * 32)
    new_record = FrameRenderFlowStore().prepare(current)
    rebuilt = build(new_record, loaded["observations"], editable_stage(loaded["stages"][0]))
    assert new_record["flowId"] != record["flowId"]
    for gap in rebuilt["quality"]["gaps"]:
        for action in gap.get("recoveryActions", []):
            if "flow_id" in action["arguments"]:
                assert action["arguments"]["flow_id"] == new_record["flowId"]


def test_native_view_extents_survive_observation_validation_and_reload():
    record, observations, proposal = disputed()
    store = FrameRenderFlowStore()
    record = store.prepare(record["outline"])
    session = record["outline"]["captureSessionId"]
    for event, observation in observations.items():
        store.record_observation(session, int(event), observation, record["flowId"])
    stored = store.get(session, record["flowId"])["observations"]
    document = build(record, stored, proposal)
    assert not document["stages"][0]["qualityGaps"]
    assert "7" in document["observations"]  # disjoint proof is retained too
    assert not build(record, document["observations"], proposal)["stages"][0]["qualityGaps"]


def test_partial_inventory_does_not_erase_an_exact_known_binding():
    record, observations, proposal = case()
    observations["4"].update(bindingsComplete=False, bindingErrors=[
        {"kind": "srv", "stage": "vertex", "error": "unrelated vertex SRV read failed"}])
    gaps = algorithm_review(record, observations, proposal)
    assert any("incomplete input/output inspection" in gap["message"] for gap in gaps)
    assert not any("no recorded" in gap["message"] for gap in gaps)


def test_texture_descriptors_do_not_gain_byte_range_exclusions():
    record, observations, proposal = disputed()
    observations["7"]["expectedOutputs"][0].update(mip=0, byteOffset=64, byteSize=32)
    observations["10"]["textureInputs"][0].update(byteOffset=0, byteSize=32)
    assert algorithm_review(record, observations, proposal)


@pytest.mark.parametrize("fields", [
    {"firstMip": 2, "numMips": 1, "firstSlice": 0, "numSlices": 2},
    {"resourceType": "buffer", "byteOffset": 0x100000000, "byteSize": 64},
])
def test_output_view_fields_survive_host_observation_validation(fields):
    record, observations, proposal = disputed()
    store = FrameRenderFlowStore()
    record = store.prepare(record["outline"])
    session = record["outline"]["captureSessionId"]
    observations["7"]["expectedOutputs"][0].update(fields)
    store.record_observation(session, 7, observations["7"], record["flowId"])
    output = store.get(session, record["flowId"])["observations"]["7"]["expectedOutputs"][0]
    assert all(output[key] == value for key, value in fields.items())

