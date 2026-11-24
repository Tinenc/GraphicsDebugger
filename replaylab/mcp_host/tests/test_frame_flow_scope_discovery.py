"""A broad draft must not hide the source timeline or omitted work."""
from __future__ import annotations

import json

from src import server
from src.frame_render_flow import FrameRenderFlowStore, build_document
from test_frame_render_flow import SESSION, outline, stage
from test_server_frame_render_flow import setup  # shared fake bridge, no replay


def test_source_candidates_remain_paged_after_broad_draft(setup):
    calls, _ = setup
    prepared = json.loads(server.prepare_frame_render_flow())
    result = json.loads(server.publish_frame_render_flow(
        prepared["flowId"], [stage(1, 12001)], show_in_window=False, publication_scope='frame'))
    assert result["coverage"]["complete"]
    authored = json.loads(server.get_frame_render_flow(prepared["flowId"]))
    assert authored["total"] == 1 and authored["sourceCandidateCount"] == 401
    reads = len(calls)
    candidates = [json.loads(server.get_frame_render_flow(
        prepared["flowId"], offset=offset, limit=200, section="candidates"))
        for offset in (0, 200, 400)]
    assert [len(page["items"]) for page in candidates] == [200, 200, 1]
    assert sum(row["actionCount"] for page in candidates for row in page["items"]) == 4001
    assert not candidates[-1]["hasMore"]
    assert len(calls) == reads  # inspection of coverage never needs GPU replay


def test_omitted_ranges_use_execution_order_and_readable_offsets():
    data = outline(8)
    for row, eid in zip(data["actions"], (90, 10, 70, 20, 60, 30, 50, 40)):
        row["eventId"] = eid
    store = FrameRenderFlowStore()
    record = store.prepare(data)
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"],
                         [stage(10, 70), stage(30, 50)])
    assert doc["coverage"]["uncoveredRanges"] == [
        {"startEventId": 90, "endEventId": 90, "actionCount": 1, "actionOffset": 0},
        {"startEventId": 20, "endEventId": 60, "actionCount": 2, "actionOffset": 3},
        {"startEventId": 40, "endEventId": 40, "actionCount": 1, "actionOffset": 7},
    ]
    gap = next(row for row in doc["quality"]["gaps"] if row["code"] == "unrepresented-frame-actions")
    assert gap["omittedActionCount"] == 4 and not gap["rangesTruncated"]
    for recovery, omitted in zip(gap["recoveryActions"], gap["uncoveredRanges"]):
        args = dict(recovery["arguments"])
        flow_id = args.pop("flow_id")
        page = store.page(store.get(SESSION, flow_id), **args)
        assert page["items"][0]["eventId"] == omitted["startEventId"]


def test_fragmented_omissions_are_bounded_without_hiding_total():
    store = FrameRenderFlowStore()
    record = store.prepare(outline(160))
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"],
                         [stage(index * 3 + 1, index * 3 + 1) for index in range(1, 160, 2)])
    gap = next(row for row in doc["quality"]["gaps"] if row["code"] == "unrepresented-frame-actions")
    assert gap["omittedActionCount"] == gap["uncoveredRangeCount"] == 80
    assert len(gap["uncoveredRanges"]) == 64 and gap["rangesTruncated"]
    assert len(gap["recoveryActions"]) == 8
    assert doc["coverage"]["uncoveredRangeCount"] == 80


def test_source_truncation_is_a_quality_gap_even_if_retained_ranges_are_covered():
    data = outline(5)
    data["summary"].update(complete=False, totalActionCount=20, omittedActionCount=15)
    record = FrameRenderFlowStore().prepare(data)
    doc = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 13)])
    gaps = {row["code"]: row for row in doc["quality"]["gaps"]}
    assert gaps["incomplete-action-outline"]["sourceOmittedActionCount"] == 15
    assert "unrepresented-frame-actions" not in gaps
    assert not doc["quality"]["analysisComplete"]
