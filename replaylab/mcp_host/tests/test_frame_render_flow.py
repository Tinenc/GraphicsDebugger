from __future__ import annotations

import copy
import pytest

from src.frame_render_flow import FrameRenderFlowStore, build_document, validate_outline


SESSION = "a" * 32


def test_empty_native_pipeline_does_not_overwrite_verified_inventory():
    store = FrameRenderFlowStore()
    record = store.prepare(outline(1))
    observation = {"expectedOutputs": [{"resourceId": 100, "kind": "rtv", "index": 0}],
                   "inventoryVersion": 3, "bindingsComplete": True,
                   "details": {"shaders": {"vertex": {"shaderId": 12}}}}
    store.record_observation(SESSION, 1, observation, record["flowId"])
    before = store.get(SESSION)
    with pytest.raises(ValueError, match="bread-pipeline-unavailable"):
        store.record_observation(SESSION, 1, {"expectedOutputs": [], "textureInputs": [],
            "bindingsComplete": True, "details": {"shaders": {}, "inputs": [], "outputs": []}}, record["flowId"])
    after = store.get(SESSION)
    assert before["observations"] == after["observations"]
    assert before["observationRevision"] == after["observationRevision"]
    document = build_document(record["outline"], record["flowId"], record["snapshotId"],
                              [stage(1, 1)], observations=after["observations"])
    assert document["observations"]["1"]["inventoryVersion"] == 3


def test_record_failure_retains_inventory_and_does_not_claim_fresh_inspection():
    store = FrameRenderFlowStore()
    record = store.prepare(outline(1))
    store.record_read_failure(SESSION, 1, "bread-replay-unavailable: GPU wait failed", record["flowId"])
    after = store.get(SESSION)
    assert not after["observations"] and after["observationRevision"] == 0
    assert after["replayUnavailable"]["eventId"] == 1


def outline(count=100):
    return {"schema": "bread-frame-render-flow-outline/v1", "captureSessionId": SESSION,
            "captureId": "", "capturePath": "C:/captures/frame.rdc", "api": "D3D12",
            "actions": [{"eventId": index * 3 + 1, "executionIndex": index, "parentEventId": 0,
                         "name": "Draw %d" % index, "flags": 2, "isAction": True,
                         "outputs": [100 + index // 10]}
                        for index in range(count)],
            "summary": {"complete": True, "totalActionCount": count, "omittedActionCount": 0}}


def stage(start, end, **fields):
    return {"title": "Stage", "startEventId": start, "endEventId": end, **fields}


def test_full_outline_is_paged_and_never_limited_to_40_nodes():
    store = FrameRenderFlowStore()
    record = store.prepare(outline(1234))
    assert len(record["candidates"]) == 124
    pages = [store.page(record, offset=offset) for offset in (0, 50, 100)]
    assert [len(page["items"]) for page in pages] == [50, 50, 24]
    assert pages[0]["hasMore"] and pages[0]["nextOffset"] == 50
    assert not pages[-1]["hasMore"] and pages[-1]["nextOffset"] is None
    assert sum(item["actionCount"] for page in pages for item in page["items"]) == 1234
    assert store.page(record, offset=1200, section="actions")["items"][-1]["eventId"] == 3700


def test_source_truncation_is_not_hidden_by_last_page():
    data = outline(10)
    data["summary"].update(complete=False, totalActionCount=15, omittedActionCount=5)
    record = FrameRenderFlowStore().prepare(data)
    page = FrameRenderFlowStore.page(record)
    assert not page["hasMore"]
    assert page["source"]["complete"] is False
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 28)])
    assert not document["coverage"]["complete"]
    assert document["coverage"]["sourceOmittedActionCount"] == 5


def test_order_is_execution_order_and_gap_coverage_is_exact():
    data = outline(5)
    data["actions"][0]["eventId"] = 100
    data["actions"][4]["eventId"] = 50
    record = FrameRenderFlowStore().prepare(data)
    document = build_document(record["outline"], record["flowId"], record["snapshotId"],
                              [stage(100, 4), stage(10, 50)])
    assert document["stages"][0]["eventIds"] == [100, 4]
    assert document["stages"][1]["eventIds"] == [10, 50]
    assert document["coverage"]["omittedActionCount"] == 1
    assert document["coverage"]["complete"] is False


@pytest.mark.parametrize("stages,match", [
    ([stage(2, 10)], "actual EIDs"),
    ([stage(1, 10), stage(7, 19)], "non-overlapping"),
    ([stage(10, 7)], "chronological"),
    ([stage(1, 10, representativeEventId=19)], "inside its stage"),
    ([stage(1, 10, confidence="proven")], "confidence"),
    ([stage(1, 10, eventIds=[999])], "Unsupported stage fields"),
    ([stage(1, 10, images=[{"path": "C:/secret.png"}])], "manifestPath"),
])
def test_stage_payload_cannot_invent_reorder_or_overlap_evidence(stages, match):
    record = FrameRenderFlowStore().prepare(outline(10))
    with pytest.raises(ValueError, match=match):
        build_document(record["outline"], record["flowId"], record["snapshotId"], stages)


def test_parent_markers_are_bounds_but_not_gpu_members():
    data = outline(4)
    data["actions"][0].update(name="Opaque", flags=0x40, isAction=False)
    for row in data["actions"][1:]:
        row["parentEventId"] = 1
    record = FrameRenderFlowStore().prepare(data)
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 10)])
    assert document["stages"][0]["eventIds"] == [4, 7, 10]
    assert document["coverage"]["complete"]
    assert record["candidates"][0]["title"] == "Opaque"


def _indirect_passes(pass_count=4, draws_per_pass=100):
    data = outline(0)
    rows = data["actions"]
    def append(name, flags, parent, kind, outputs=None):
        event = len(rows) + 1
        rows.append({"eventId": event, "executionIndex": len(rows), "name": name,
                     "flags": flags, "parentEventId": parent, "outputs": outputs or [],
                     "actionKind": kind, "isAction": kind != "marker"})
        return event
    for pass_index in range(pass_count):
        marker = append("Color pass %d" % pass_index, 0x0040, 0, "marker")
        command = append("CommandBuffer", 0x1000000 | 0x0040, marker, "marker")
        for draw in range(draws_per_pass):
            wrapper = append("ExecuteIndirect", 0x0200 | 0x0040, command, "marker")
            nested = append("ExecuteIndirect child", 0x0200, wrapper, "marker")
            append("DrawIndexedInstanced", 2 | (0x010000 if draw % 2 else 0x020000), nested, "draw")
    data["summary"]["totalActionCount"] = len(rows)
    return data


def test_indirect_draw_and_command_wrappers_do_not_explode_stage_count():
    record = FrameRenderFlowStore().prepare(_indirect_passes())
    candidates = record["candidates"]
    assert len(candidates) == 4
    assert [group["actionCount"] for group in candidates] == [100] * 4
    assert [group["title"] for group in candidates] == ["Color pass %d" % index for index in range(4)]
    assert sum(group["actionCount"] for group in candidates) == 400
    # The condensed ranges still resolve to every concrete GPU action.
    document = build_document(record["outline"], record["flowId"], record["snapshotId"],
        [stage(group["startEventId"], group["endEventId"]) for group in candidates])
    assert document["coverage"]["complete"]
    assert document["coverage"]["coveredActionCount"] == 400


def test_indirect_folding_preserves_real_attachment_and_nested_pass_boundaries():
    data = _indirect_passes(1, 6)
    draws = [row for row in data["actions"] if row["isAction"]]
    for index, row in enumerate(draws):
        row["outputs"] = [100 if index < 3 else 200]
    record = FrameRenderFlowStore().prepare(data)
    assert [group["actionCount"] for group in record["candidates"]] == [3, 3]
    assert [group["outputs"] for group in record["candidates"]] == [[100], [200]]
    # A true nested pass remains a boundary even with unchanged attachments.
    nested_marker = next(row for row in data["actions"] if row["eventId"] == draws[2]["parentEventId"])
    nested_marker.update(flags=0x0040, name="Nested material pass")
    record = FrameRenderFlowStore().prepare(data)
    assert record["candidates"][1]["title"] == "Nested material pass"
    assert [group["actionCount"] for group in record["candidates"]] == [2, 1, 3]


def test_unmarked_draws_group_by_operation_not_indexed_instanced_flags():
    data = outline(4)
    for index, row in enumerate(data["actions"]):
        row.update(actionKind="draw", flags=2 | (0x010000 if index % 2 else 0x020000), outputs=[])
    record = FrameRenderFlowStore().prepare(data)
    assert len(record["candidates"]) == 1
    assert record["candidates"][0]["actionCount"] == 4


def test_explicit_native_action_flag_supports_future_apis():
    data = outline(1)
    data["actions"][0].update(flags=0, actionKind="future", isAction=True)
    record = FrameRenderFlowStore().prepare(data)
    assert record["candidates"][0]["actionCount"] == 1


def test_native_64_bit_resource_ids_and_frame_number_remain_exact():
    data = outline(1)
    data["frameNumber"] = 143064
    data["actions"][0].update(outputs=[2**60 + 1], depthOutput=2**58 + 7,
                              copySource=2**56 + 3, copyDestination=2**59 + 5)
    clean = validate_outline(data)
    assert clean["actions"][0]["outputs"] == [2**60 + 1]
    assert clean["actions"][0]["copyDestination"] == 2**59 + 5
    assert clean["frameNumber"] == 143064


def test_cached_publication_membership_is_not_sent_as_huge_prompt():
    store = FrameRenderFlowStore()
    record = store.prepare(outline(1000))
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 2998)])
    store.publish(record["flowId"], document)
    page = store.page(store.get(SESSION))
    assert page["published"]
    assert page["items"][0]["actionCount"] == 1000
    assert "eventIds" not in page["items"][0]
    assert len(store.get(SESSION)["document"]["stages"][0]["eventIds"]) == 1000


def test_capture_cache_identity_lifecycle_and_no_mutable_alias():
    store = FrameRenderFlowStore(capacity=2)
    first = store.prepare(outline(1))
    first["outline"]["actions"].clear()
    assert len(store.get(SESSION)["outline"]["actions"]) == 1
    with pytest.raises(ValueError, match="session changed"):
        store.get("b" * 32, first["flowId"])
    second = outline(2)
    store.prepare(second)
    store.prepare(outline(3))
    with pytest.raises(ValueError, match="No cached"):
        store.get(SESSION, first["flowId"])


@pytest.mark.parametrize("change", [
    lambda value: value["actions"].append(copy.deepcopy(value["actions"][0])),
    lambda value: value["actions"][0].update(parentEventId=4),
    lambda value: value["summary"].update(complete=False),
    lambda value: value["summary"].update(totalActionCount=999),
    lambda value: value["actions"][0].update(isAction=1),
])
def test_malformed_outline_rejected(change):
    data = outline(3)
    change(data)
    with pytest.raises(ValueError):
        validate_outline(data)


def test_evidence_resolver_receives_exact_stage_membership():
    record = FrameRenderFlowStore().prepare(outline(3))
    calls = []
    def resolve(image, members):
        calls.append((image, members))
        return {"path": "C:/managed/evidence.png", "eventId": 4}
    document = build_document(record["outline"], record["flowId"], record["snapshotId"],
        [stage(1, 7, images=[{"manifestPath": "C:/managed/manifest.json", "title": "Base color"}])],
        image_resolver=resolve)
    assert calls[0][1] == {1, 4, 7}
    assert document["stages"][0]["images"][0]["title"] == "Base color"


def _reviewed_stage(start, end, **fields):
    fields.setdefault("title", "Direct lighting")
    fields.setdefault("summary", "Combines the surface normal with light direction to shade the surface.")
    fields.setdefault("algorithm", {
        "inputs": [],
        "steps": [{"eventId": end, "title": "Evaluate surface lighting",
                   "calculation": "Evaluate the normal/light dot product and write lit color.",
                   "evidence": "The captured PS multiplies surface color by max(dot(N,L),0) before SV_Target0."}],
        "outputs": [{"eventId": end, "resourceId": 100, "role": "Lit surface color"}],
        "integration": [{"sourceEventId": end, "resourceId": 100, "eventId": end, "mode": "in-place",
                         "explanation": "Writes the current scene color target.",
                         "evidence": "The inspected output binding is the scene target; the PS writes SV_Target0."}],
    })
    for image in fields.get("images", []):
        image.setdefault("title", "Surface lighting")
        image.setdefault("description", "Surface lighting used by the following color composition stage.")
        image.setdefault("channelMeaning", "RGB contains surface illumination; A is not used by the color composition.")
        image.setdefault("evidence", "The producer PS writes illumination to RGB; the following composition PS samples only xyz.")
    return stage(start, end, category="lighting", confidence="inferred",
        investigation={"status": "investigated", "evidence": "Inspected PS and bound resources at the stage draw."},
        techniques=[{"name": "Direct lighting", "status": "recognized", "evidence": "PS evaluates the light direction and normal dot product."}],
        **fields)


def _preview(image, members):
    event = int(image["manifestPath"].split("-")[0])
    assert event in members
    resource = int(image["manifestPath"].split("-")[1])
    return {"eventId": event, "resourceId": resource, "channel": "rgba", "width": 32, "height": 16,
            "attachmentKind": "rtv", "attachmentIndex": 0,
            "path": "C:/managed/%s.png" % image["manifestPath"], "manifestPath": image["manifestPath"],
            "displayMapping": {"rangeMode": "explicit", "blackPoint": 0.0, "whitePoint": 2.0}}


def test_auxiliary_images_keep_evidence_and_indices_when_collapsed():
    record = FrameRenderFlowStore().prepare(outline(1))
    proposal = stage(1, 1, images=[
        {"manifestPath": "1-100"},
        {"manifestPath": "1-101", "presentation": "detail"},
    ], processSteps=[{"label": "Effect", "imageIndex": 1},
                     {"label": "Composed scene", "imageIndex": 0}])
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"],
                         [proposal], image_resolver=_preview)
    result = doc["stages"][0]
    assert [image["presentation"] for image in result["images"]] == ["primary", "detail"]
    assert [step["imageIndex"] for step in result["processSteps"]] == [1, 0]
    assert result["outputCoverage"]["representedResourceIds"] == [100, 101]
    assert [image["resourceId"] for image in result["images"]] == [100, 101]


@pytest.mark.parametrize("presentation", [None, "hidden", True, {}, []])
def test_unknown_image_presentation_does_not_silently_hide_evidence(presentation):
    record = FrameRenderFlowStore().prepare(outline(1))
    with pytest.raises(ValueError, match="Image presentation"):
        build_document(record["outline"], record["flowId"], record["snapshotId"],
            [stage(1, 1, images=[{"manifestPath": "1-100", "presentation": presentation}])],
            image_resolver=_preview)


def test_full_action_coverage_is_not_completed_technical_investigation():
    record = FrameRenderFlowStore().prepare(outline(1))
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 1)])
    assert doc["coverage"]["complete"]
    assert not doc["quality"]["analysisComplete"]
    assert doc["quality"]["publicationAllowed"]
    codes = {gap["code"] for gap in doc["quality"]["gaps"]}
    assert {"missing-stage-preview", "stage-not-investigated"} <= codes
    assert all(gap["nextAction"] for gap in doc["quality"]["gaps"])


def test_stage_with_supported_semantics_and_full_output_preview_passes_review():
    record = FrameRenderFlowStore().prepare(outline(1))
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"],
        [_reviewed_stage(1, 1, images=[{"manifestPath": "1-100", "semantic": "Lighting", "evidence": "PS color output."}])],
        image_resolver=_preview, observations={"1": {"expectedOutputs": [{"resourceId": 100, "kind": "rtv", "index": 0}]}})
    assert doc["quality"]["analysisComplete"]
    assert doc["stages"][0]["outputCoverage"]["status"] == "complete"


@pytest.mark.parametrize("description,missing", [
    ("R16G16B16A16_FLOAT, 1920×1080", True),
    ("R16G16B16A16_FLOAT stores the indirect light added during composition.", False),
    ("R16G16B16A16_FLOAT, undefined×undefined", True),
])
def test_format_metadata_is_not_an_explanation_but_does_not_hide_a_real_one(description, missing):
    record = FrameRenderFlowStore().prepare(outline(1))
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"],
        [_reviewed_stage(1, 1, images=[{"manifestPath": "1-100", "description": description}])],
        image_resolver=_preview)
    assert ("missing-texture-explanation" in {gap["code"] for gap in doc["quality"]["gaps"]}) == missing


def test_failed_binding_enumeration_cannot_pass_quality_with_remaining_previews():
    store = FrameRenderFlowStore()
    record = store.prepare(outline(1))
    error = {"kind": "uav", "stage": "fragment", "error": "Descriptor enumeration failed"}
    store.record_observation(SESSION, 1, {"expectedOutputs": [{"resourceId": 100, "kind": "rtv", "index": 0}],
                                         "bindingsComplete": False, "bindingErrors": [error]})
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"],
        [_reviewed_stage(1, 1, images=[{"manifestPath": "1-100"}])], image_resolver=_preview,
        observations=store.get(SESSION)["observations"])
    assert doc["coverage"]["complete"] and not doc["quality"]["analysisComplete"]
    assert doc["stages"][0]["outputCoverage"]["bindingErrors"] == [error]
    assert doc["stages"][0]["outputCoverage"]["inventoryComplete"] is False
    assert {gap["code"] for gap in doc["quality"]["gaps"]} == {
        "stage-inventory-incomplete", "unverified-algorithm-binding"}
    assert doc["observations"]["1"]["bindingsComplete"] is False
    assert doc["observations"]["1"]["bindingErrors"] == [error]


def test_recorded_mrt_inventory_finds_missing_attachments_even_if_outline_empty():
    data = outline(1)
    data["actions"][0]["outputs"] = []
    store = FrameRenderFlowStore()
    record = store.prepare(data)
    store.record_observation(SESSION, 1, {"expectedOutputs": [
        {"resourceId": 100, "kind": "rtv", "index": 0},
        {"resourceId": 101, "kind": "rtv", "index": 1},
        {"resourceId": 102, "kind": "dsv", "index": 0}], "evidence": "Captured pipeline inventory"})
    record = store.get(SESSION)
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"],
        [_reviewed_stage(1, 1, images=[{"manifestPath": "1-100"}])], image_resolver=_preview,
        observations=record["observations"])
    coverage = doc["stages"][0]["outputCoverage"]
    assert coverage["source"] == "stage-inspection"
    assert coverage["missingResourceIds"] == [101, 102]
    assert "missing-output-attachments" in {gap["code"] for gap in doc["quality"]["gaps"]}
    assert doc["observations"]["1"]["expectedOutputs"][2]["kind"] == "dsv"


def test_single_channel_preview_does_not_replace_mrt_overview():
    record = FrameRenderFlowStore().prepare(outline(1))
    def channel_preview(image, members):
        return dict(_preview(image, members), channel="r")
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"],
        [_reviewed_stage(1, 1, images=[{"manifestPath": "1-100", "group": "channels"}])],
        image_resolver=channel_preview)
    assert doc["stages"][0]["outputCoverage"]["missingResourceIds"] == [100]


def test_same_resource_and_slot_in_another_shader_stage_does_not_cover_uav():
    data = outline(1)
    data["actions"][0].update(flags=4, actionKind="dispatch", outputs=[], hasWork=True)
    record = FrameRenderFlowStore().prepare(data)
    outputs = [{"resourceId": 100, "kind": "uav", "index": 0, "stage": "compute"}]
    def preview(image, members):
        return dict(_preview(image, members), attachmentKind="uav", attachmentStage="fragment")
    proposal = _reviewed_stage(1, 1, images=[{"manifestPath": "1-100"}])
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal],
                         image_resolver=preview, observations={"1": {"expectedOutputs": outputs}})
    assert doc["stages"][0]["outputCoverage"]["missingAttachments"] == outputs
    def correct_preview(image, members):
        return dict(preview(image, members), attachmentStage="compute")
    corrected = build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal],
                               image_resolver=correct_preview, observations={"1": {"expectedOutputs": outputs}})
    assert corrected["quality"]["analysisComplete"]


def test_compute_buffer_output_requires_explanation_instead_of_impossible_screenshot():
    data = outline(1)
    data["actions"][0].update(flags=4, actionKind="dispatch", outputs=[], hasWork=True)
    store = FrameRenderFlowStore()
    record = store.prepare(data)
    store.record_observation(SESSION, 1, {"expectedOutputs": [{"resourceId": 100,
        "kind": "uav", "index": 2, "stage": "compute", "resourceType": "buffer", "captureSupported": False,
        "captureReason": "structured-buffer-has-no-image"}]})
    record = store.get(SESSION)
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"],
        [_reviewed_stage(1, 1)], observations=record["observations"])
    assert doc["quality"]["analysisComplete"]
    assert doc["stages"][0]["outputCoverage"]["nonImageOutputs"][0]["resourceType"] == "buffer"
    assert not doc["stages"][0]["outputCoverage"]["missingResourceIds"]


def test_navigation_prefers_final_effective_draw_over_dispatch_and_zero_work():
    data = outline(6)
    for row in data["actions"]:
        row.update(actionKind="draw", hasWork=True)
    data["actions"][3].update(flags=4, actionKind="dispatch", hasWork=True)
    data["actions"][4].update(flags=2, actionKind="draw", hasWork=False)
    data["actions"][5].update(flags=4, actionKind="dispatch", hasWork=False)
    record = FrameRenderFlowStore().prepare(data)
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"],
        [stage(1, 16, representativeEventId=1)])
    assert doc["stages"][0]["navigationEventId"] == 7
    assert doc["stages"][0]["representativeEventId"] == 7
    assert doc["stages"][0]["navigationStatus"] == "last-draw"
    assert doc["coverage"]["coveredActionCount"] == 6


def test_compute_only_navigation_excludes_zero_dispatch_and_preserves_unknown():
    data = outline(3)
    for row in data["actions"]:
        row.update(actionKind="dispatch", flags=4, outputs=[], hasWork=True)
    data["actions"][1]["hasWork"] = None
    data["actions"][2]["hasWork"] = False
    record = FrameRenderFlowStore().prepare(data)
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 7)])
    assert doc["stages"][0]["navigationEventId"] == 4
    assert doc["stages"][0]["navigationHasWork"] is None
    data["actions"][0]["hasWork"] = data["actions"][1]["hasWork"] = False
    record = FrameRenderFlowStore().prepare(data)
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 7)])
    assert doc["stages"][0]["navigationStatus"] == "no-effective-work"


def test_cross_resource_visual_comparison_allowed_but_mapping_mismatch_is_reported():
    record = FrameRenderFlowStore().prepare(outline(1))
    proposal = _reviewed_stage(1, 1, images=[{"manifestPath": "1-100"}, {"manifestPath": "1-200", "group": "inputs"}],
                             comparisons=[{"leftImageIndex": 0, "rightImageIndex": 1, "label": "Before / after"}])
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal], image_resolver=_preview)
    comparison = doc["stages"][0]["comparisons"][0]
    assert comparison["visualComparable"] and comparison["differentResource"]
    assert not comparison["numericDifferenceEvidence"]
    def different_mapping(image, members):
        value = _preview(image, members)
        if value["resourceId"] == 200:
            value["displayMapping"]["whitePoint"] = 10.0
        return value
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal], image_resolver=different_mapping)
    assert not doc["stages"][0]["comparisons"][0]["visualComparable"]
    assert doc["stages"][0]["comparisons"][0]["comparisonWarnings"] == ["different-display-mapping"]


def test_input_preview_can_reference_earlier_stage_but_not_future_action():
    record = FrameRenderFlowStore().prepare(outline(3))
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"],
        [stage(4, 4, images=[{"manifestPath": "1-100", "group": "inputs"}])], image_resolver=_preview)
    assert doc["stages"][0]["images"][0]["eventId"] == 1
    with pytest.raises(AssertionError):
        build_document(record["outline"], record["flowId"], record["snapshotId"],
            [stage(4, 4, images=[{"manifestPath": "7-100", "group": "inputs"}])], image_resolver=_preview)


def test_mrt_and_channel_collection_is_not_limited_to_four_images():
    record = FrameRenderFlowStore().prepare(outline(1))
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"],
        [stage(1, 1, images=[{"manifestPath": "1-%d" % (100 + index)} for index in range(40)])], image_resolver=_preview)
    assert len(doc["stages"][0]["images"]) == 40


@pytest.mark.parametrize("fields", [
    {"category": "invented-category"},
    {"techniques": [{"name": "SSR", "status": "guaranteed"}]},
    {"investigation": {"status": "finished"}},
    {"comparisons": [{"leftImageIndex": 0, "rightImageIndex": 1, "path": "C:/secret.png"}]},
])
def test_new_annotation_surfaces_are_bounded_and_typed(fields):
    record = FrameRenderFlowStore().prepare(outline(1))
    with pytest.raises(ValueError):
        build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 1, **fields)])


def _effect_stage(**fields):
    return _reviewed_stage(1, 1,
        images=[{"manifestPath": "1-100"}, {"manifestPath": "1-200", "group": "inputs"}],
        comparisons=[{"leftImageIndex": 1, "rightImageIndex": 0, "label": "Before and after lighting"}],
        animations=[{"imageIndices": [1, 0], "label": "Lighting contribution",
                     "description": "The unlit input followed by the captured lighting result."}],
        **fields)


def test_process_steps_preserve_order_across_images_comparisons_and_animations():
    record = FrameRenderFlowStore().prepare(outline(1))
    steps = [{"label": "Input", "imageIndex": 1},
             {"label": "Light contribution", "animationIndex": 0, "description": "Accumulate the lighting."},
             {"label": "Compare the result", "comparisonIndex": 0}]
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"],
        [_effect_stage(processSteps=steps)], image_resolver=_preview,
        observations={"1": {"expectedOutputs": [{"resourceId": 100, "kind": "rtv", "index": 0}]}})
    assert doc["quality"]["analysisComplete"]
    assert doc["stages"][0]["processSteps"] == [dict(step, description=step.get("description", "")) for step in steps]
    # The process sequence references existing media; it must not duplicate or reorder the receipts.
    assert [image["resourceId"] for image in doc["stages"][0]["images"]] == [100, 200]
    assert len(doc["stages"][0]["animations"]) == len(doc["stages"][0]["comparisons"]) == 1


@pytest.mark.parametrize("steps", [
    [{"label": "Only one", "imageIndex": 0}],
    [{"label": "Input", "imageIndex": 0}] * 13,
    [{"label": "Missing reference"}, {"label": "Output", "imageIndex": 0}],
    [{"label": "Ambiguous", "imageIndex": 0, "comparisonIndex": 0}, {"label": "Output", "imageIndex": 0}],
    [{"label": "Missing image", "imageIndex": 2}, {"label": "Output", "imageIndex": 0}],
    [{"label": "Missing comparison", "comparisonIndex": 1}, {"label": "Output", "imageIndex": 0}],
    [{"label": "Missing animation", "animationIndex": 1}, {"label": "Output", "imageIndex": 0}],
    [{"label": "Boolean is not an index", "imageIndex": True}, {"label": "Output", "imageIndex": 0}],
    [{"label": "", "imageIndex": 1}, {"label": "Output", "imageIndex": 0}],
])
def test_process_steps_reject_dangling_ambiguous_or_unreadable_media(steps):
    record = FrameRenderFlowStore().prepare(outline(1))
    with pytest.raises(ValueError):
        build_document(record["outline"], record["flowId"], record["snapshotId"],
                       [_effect_stage(processSteps=steps)], image_resolver=_preview)


def test_process_step_cannot_reference_a_media_collection_that_does_not_exist():
    record = FrameRenderFlowStore().prepare(outline(1))
    proposal = _reviewed_stage(1, 1, images=[{"manifestPath": "1-100"}],
        processSteps=[{"label": "Input", "imageIndex": 0}, {"label": "Output", "comparisonIndex": 0}])
    with pytest.raises(ValueError, match="missing media"):
        build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal], image_resolver=_preview)


def _auxiliary_omission(**fields):
    return {"resourceId": 101, "kind": "rtv", "index": 1,
            "reason": "The inherited auxiliary target is not written by this lighting pass.",
            "evidence": "The captured blend state has ColorWriteMask=0 for RTV1 and PS writes only SV_Target0.",
            **fields}


def test_investigated_auxiliary_output_can_be_omitted_without_hiding_its_inventory():
    record = FrameRenderFlowStore().prepare(outline(1))
    outputs = [{"resourceId": 100, "kind": "rtv", "index": 0},
               {"resourceId": 101, "kind": "rtv", "index": 1, "stage": "fragment", "mip": 2, "slice": 3, "sample": 0}]
    observations = {"1": {"expectedOutputs": outputs}}
    proposal = _reviewed_stage(1, 1, images=[{"manifestPath": "1-100"}])
    before = build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal],
                            image_resolver=_preview, observations=observations)
    assert before["stages"][0]["outputCoverage"]["missingResourceIds"] == [101]
    assert not before["quality"]["analysisComplete"]
    proposal["outputOmissions"] = [_auxiliary_omission()]
    after = build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal],
                           image_resolver=_preview, observations=observations)
    assert after["quality"]["analysisComplete"]
    coverage = after["stages"][0]["outputCoverage"]
    assert coverage["expectedResourceIds"] == [100, 101]
    assert coverage["representedResourceIds"] == [100]
    assert coverage["missingAttachments"] == coverage["missingResourceIds"] == []
    omission = after["stages"][0]["outputOmissions"][0]
    assert omission == {**outputs[1], "reason": proposal["outputOmissions"][0]["reason"],
                        "evidence": proposal["outputOmissions"][0]["evidence"]}
    assert coverage["omittedAttachments"] == [omission]
    assert after["observations"]["1"]["expectedOutputs"] == outputs


@pytest.mark.parametrize("changes", [
    {"resourceId": 999}, {"kind": "uav"}, {"index": 0}, {"stage": "compute"},
    {"mip": 1}, {"slice": 1}, {"sample": 1}, {"reason": " "}, {"evidence": ""},
])
def test_output_omission_requires_exact_observed_binding_and_reasoned_evidence(changes):
    record = FrameRenderFlowStore().prepare(outline(1))
    outputs = [{"resourceId": 101, "kind": "rtv", "index": 1,
                "stage": "fragment", "mip": 0, "slice": 0, "sample": 0}]
    proposal = _reviewed_stage(1, 1, outputOmissions=[_auxiliary_omission(**changes)])
    with pytest.raises(ValueError):
        build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal],
                       observations={"1": {"expectedOutputs": outputs}})


def test_ambiguous_omission_needs_subresource_identity_and_does_not_hide_siblings():
    record = FrameRenderFlowStore().prepare(outline(1))
    outputs = [{"resourceId": 101, "kind": "rtv", "index": 1, "mip": mip} for mip in (0, 1)]
    proposal = _reviewed_stage(1, 1, outputOmissions=[_auxiliary_omission()])
    with pytest.raises(ValueError, match="one unique Host-observed"):
        build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal],
                       observations={"1": {"expectedOutputs": outputs}})
    proposal["outputOmissions"][0]["mip"] = 0
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal],
                         observations={"1": {"expectedOutputs": outputs}})
    assert doc["stages"][0]["outputCoverage"]["missingAttachments"] == [outputs[1]]
    assert not doc["quality"]["analysisComplete"]
    proposal["outputOmissions"] *= 2
    with pytest.raises(ValueError, match="one unique Host-observed"):
        build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal],
                       observations={"1": {"expectedOutputs": outputs}})


@pytest.mark.parametrize("kind,event,resource,channel,group,missing", [
    ("rtv", 7, 200, "rgba", "outputs", True),
    ("present", 4, 200, "rgba", "outputs", True),
    ("present", 7, 100, "rgba", "outputs", True),
    ("present", 7, 200, "a", "outputs", True),
    ("present", 7, 200, "r", "outputs", True),
    ("present", 7, 200, "rgba", "inputs", True),
    ("present", 7, 200, "rgba", "outputs", False),
])
def test_final_preview_requires_exact_present_event_target_and_evidence_kind(kind, event, resource, channel, group, missing):
    data = outline(3)
    data["actions"][1].update(actionKind="copy", flags=0x0400, outputs=[], copySource=300, copyDestination=200)
    data["actions"][2].update(actionKind="present", flags=0x0100, outputs=[], copyDestination=200)
    record = FrameRenderFlowStore().prepare(data)
    proposal = _reviewed_stage(1, 7, title="UI composition and presentation", images=[
        {"manifestPath": "1-100", "title": "UI layer"},
        {"manifestPath": "%d-%d" % (event, resource), "title": "Composed frame", "group": group}])
    proposal["algorithm"] = {
        "inputs": [], "steps": [{"eventId": 7, "title": "Present the composed frame",
            "calculation": "Present the previously composed swapchain image.",
            "evidence": "The Present action identifies resource 200 as its copyDestination."}],
        "outputs": [{"eventId": 7, "resourceId": 200, "role": "Composed backbuffer"}],
        "integration": [{"sourceEventId": 7, "eventId": 7, "resourceId": 200, "mode": "present",
            "explanation": "Display the scene with UI.", "evidence": "Present copyDestination is 200."}]}
    def resolve(image, members):
        resolved = _preview(image, members)
        if image["title"] == "Composed frame":
            resolved["attachmentKind"] = kind
            resolved["channel"] = channel
        return resolved
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal],
        image_resolver=resolve, observations={"7": {"expectedOutputs": []}})
    codes = {gap["code"] for gap in doc["quality"]["gaps"]}
    assert ("missing-final-present-preview" in codes) == missing
    assert doc["quality"]["analysisComplete"] is (not missing)
    assert doc["stages"][0]["outputCoverage"]["presentEventId"] == 7
    assert doc["stages"][0]["outputCoverage"]["presentResourceId"] == 200


@pytest.mark.parametrize("stage_fields,image_fields,code", [
    ({"title": "EID 61523"}, {}, "nonsemantic-stage-title"),
    ({"title": "绘制大量几何体"}, {}, "nonsemantic-stage-title"),
    ({}, {"title": "EID 61523 RTV0 100"}, "nonsemantic-texture-title"),
    ({}, {"title": "RTV0 · 100"}, "nonsemantic-texture-title"),
    ({}, {"channelMeaning": ""}, "missing-texture-channel-meaning"),
    ({}, {"channelMeaning": "各分量精确业务含义未逐项确认。"}, "unresolved-texture-meaning"),
    ({}, {"description": "physical-attachment preserves RGBA"}, "unresolved-texture-meaning"),
    ({}, {"channelMeaning": "Unknown channel meaning"}, "unresolved-texture-meaning"),
    ({}, {"evidence": ""}, "missing-image-semantic-evidence"),
])
def test_complete_action_coverage_cannot_hide_nonsemantic_titles_or_channel_placeholders(stage_fields, image_fields, code):
    record = FrameRenderFlowStore().prepare(outline(1))
    proposal = _reviewed_stage(1, 1, images=[{"manifestPath": "1-100", **image_fields}], **stage_fields)
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"], [proposal],
        image_resolver=_preview, observations={"1": {"expectedOutputs": [{"resourceId": 100, "kind": "rtv", "index": 0}]}})
    assert doc["coverage"]["complete"]
    assert not doc["quality"]["analysisComplete"]
    assert code in {gap["code"] for gap in doc["quality"]["gaps"]}
