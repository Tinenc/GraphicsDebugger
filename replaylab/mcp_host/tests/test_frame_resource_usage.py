from __future__ import annotations

import copy

import pytest

from src.frame_render_flow import FrameRenderFlowStore
from src.frame_resource_usage import (
    FrameResourceUsageCache, normalize_usage, usage_cache_key, usage_page, usage_query,
)


SESSION = "a" * 32
RID = 2**60 + 5


def flow_record(session=SESSION):
    return FrameRenderFlowStore().prepare({
        "schema": "bread-frame-render-flow-outline/v1", "captureSessionId": session,
        "captureId": "", "capturePath": "C:/captures/frame.rdc", "api": "D3D12",
        "actions": [{"eventId": event, "executionIndex": index, "parentEventId": 0,
                     "name": "Step %d" % index, "flags": 2, "outputs": [RID], "isAction": True}
                    for index, event in enumerate((100, 20, 70, 10, 50))],
        "summary": {"complete": True, "totalActionCount": 5, "omittedActionCount": 0}})


def usage_receipt(events=(50, 100, 70, 20, 10)):
    return {"schema": "renderdoc-resource-usage/v1", "resourceId": RID,
            "count": len(events), "coarseCandidates": True,
            "candidates": [{"eventId": event, "usage": "ResourceUsage.PS_Resource",
                            "usageValue": 12, "actionName": "not authoritative"}
                           for event in events]}


def test_query_filters_in_execution_order_not_numeric_eid_and_excludes_anchor():
    record = flow_record()
    raw = usage_receipt((50, 100, 70, 70, 20, 10))
    raw["candidates"][2].update(usage="ResourceUsage.ColorTarget", usageValue=21)
    entry = normalize_usage(raw, record, RID)
    before = usage_page(entry, record, usage_query(record, RID, 70, "before", 0, 50), cache_hit=False)
    assert [row["eventId"] for row in before["items"]] == [100, 20]
    after = usage_page(entry, record, usage_query(record, RID, 70, "after", 0, 50), cache_hit=True)
    assert [row["eventId"] for row in after["items"]] == [10, 50]
    assert before["candidateOnly"] and before["anchorExcluded"]
    assert before["complete"] and before["resourceId"] == RID
    assert before["order"] == "execution-ascending"
    all_rows = usage_page(entry, record, usage_query(record, RID, 0, "all", 0, 50), cache_hit=True)
    assert [row["eventId"] for row in all_rows["items"]] == [100, 20, 70, 70, 10, 50]
    assert all_rows["items"][2]["usage"] == "ResourceUsage.ColorTarget"
    assert all_rows["items"][3]["usage"] == "ResourceUsage.PS_Resource"
    assert all_rows["items"][0]["actionName"] == "Step 0"
    assert not {"producer", "consumer", "algorithm", "read", "write"}.intersection(all_rows["items"][0])


def test_pages_are_bounded_and_complete_is_independent_of_has_more():
    record = flow_record()
    entry = normalize_usage(usage_receipt(), record, RID)
    query = usage_query(record, RID, 0, "all", 0, 2)
    first = usage_page(entry, record, query, cache_hit=False)
    assert first["complete"] and first["hasMore"] and first["nextOffset"] == 2
    assert len(first["items"]) == 2 and first["total"] == 5
    last = usage_page(entry, record, {**query, "offset": 4}, cache_hit=True)
    assert last["complete"] and not last["hasMore"] and last["nextOffset"] is None
    assert [item["eventId"] for item in last["items"]] == [50]
    last["items"][0]["usage"] = "mutated response"
    assert entry["items"][-1]["usage"] == "ResourceUsage.PS_Resource"
    empty = usage_page(entry, record, {**query, "offset": 100}, cache_hit=True)
    assert empty["items"] == [] and not empty["hasMore"]


def test_nearest_before_pages_backwards_in_actual_execution_order_without_losing_uses():
    record = flow_record()
    entry = normalize_usage(usage_receipt((100, 20, 20, 70, 10, 50)), record, RID)
    query = usage_query(record, RID, 10, "before", 0, 2, "nearest")
    first = usage_page(entry, record, query, cache_hit=True)
    second = usage_page(entry, record, {**query, "offset": 2}, cache_hit=True)
    assert [item["eventId"] for item in first["items"]] == [70, 20]
    assert [item["eventId"] for item in second["items"]] == [20, 100]
    assert first["nextOffset"] == 2 and first["hasMore"] and not second["hasMore"]
    assert first["order"] == "execution-descending" and first["candidateOnly"]
    assert first["complete"] and first["total"] == 4
    assert not usage_page(entry, record, {**query, "offset": 20}, cache_hit=True)["items"]
    # Reverse paging must not mutate the cached canonical history.
    assert [row["eventId"] for row in entry["items"]] == [100, 20, 20, 70, 10, 50]


def test_nearest_after_is_forward_and_retains_incomplete_source_boundary():
    record = flow_record()
    entry = normalize_usage({**usage_receipt(), "complete": False}, record, RID)
    query = usage_query(record, RID, 70, "after", 0, 2, "nearest")
    result = usage_page(entry, record, query, cache_hit=False)
    assert [row["eventId"] for row in result["items"]] == [10, 50]
    assert not result["complete"] and not result["hasMore"]
    assert result["order"] == "execution-ascending" and result["candidateOnly"]


@pytest.mark.parametrize("order,direction,anchor", [("nearest", "all", 70), ("nearest", "before", 0),
                                                   ("bad", "after", 70), (True, "after", 70)])
def test_nearest_query_requires_valid_order_and_directional_anchor(order, direction, anchor):
    with pytest.raises(ValueError):
        usage_query(flow_record(), RID, anchor, direction, 0, 50, order)


@pytest.mark.parametrize("patch", [
    {"complete": False}, {"truncated": True}, {"hasMore": True},
    {"nextOffset": 5}, {"count": 20}, {"count": 4},
])
def test_upstream_truncation_or_inconsistent_count_cannot_claim_complete(patch):
    record = flow_record()
    entry = normalize_usage({**usage_receipt(), **patch}, record, RID)
    result = usage_page(entry, record, usage_query(record, RID, 0, "all", 0, 50), cache_hit=False)
    assert not result["complete"] and not result["source"]["complete"]
    assert not result["hasMore"]


def test_unmapped_initial_or_missing_events_and_partial_outline_remain_incomplete():
    record = flow_record()
    record["outline"]["summary"].update(complete=False, omittedActionCount=2, totalActionCount=7)
    entry = normalize_usage(usage_receipt((0, 100, 999)), record, RID)
    assert not entry["complete"]
    assert entry["source"]["unmappedEventCount"] == 2
    assert entry["source"]["complete"] and not entry["source"]["outlineComplete"]
    assert [row["eventId"] for row in entry["items"]] == [100]


def test_host_row_bound_is_explicit_and_does_not_create_false_completeness(monkeypatch):
    from src import frame_resource_usage as module
    monkeypatch.setattr(module, "MAX_USAGE_ROWS", 2)
    entry = normalize_usage(usage_receipt(), flow_record(), RID)
    assert not entry["complete"] and entry["source"]["hostOmittedCount"] == 3
    assert len(entry["items"]) == 2


@pytest.mark.parametrize("patch", [
    {"resourceId": RID + 1}, {"resourceId": True}, {"schema": "other"},
    {"captureSessionId": "b" * 32}, {"capture": {"captureSessionId": "b" * 32}},
    {"captureId": "b" * 64}, {"complete": "yes"}, {"count": True},
    {"candidates": "truncated text"},
])
def test_bad_identity_or_shape_is_rejected(patch):
    with pytest.raises(ValueError):
        normalize_usage({**usage_receipt(), **patch}, flow_record(), RID)


@pytest.mark.parametrize("patch", [
    {"resource_id": True}, {"resource_id": 2**64}, {"anchor_event_id": 999},
    {"direction": "before"}, {"direction": "bad"}, {"limit": 0},
    {"limit": 201}, {"offset": -1}, {"offset": True},
])
def test_invalid_query_rejected_before_collection(patch):
    arguments = dict(resource_id=RID, anchor_event_id=0, direction="all", offset=0, limit=50)
    with pytest.raises(ValueError):
        usage_query(flow_record(), **{**arguments, **patch})


def test_cache_isolates_session_flow_snapshot_and_resource_and_discards_stale_refresh():
    record = flow_record()
    entry = normalize_usage(usage_receipt(), record, RID)
    cache = FrameResourceUsageCache()
    key = usage_cache_key(record, RID)
    _, old_token = cache.acquire(key)
    _, current_token = cache.acquire(key, refresh=True)
    assert not cache.store(key, old_token, entry)
    assert cache.store(key, current_token, entry)
    assert cache.acquire(key) == (entry, None)
    for position, replacement in enumerate(("b" * 32, "flow-other", "other-snapshot", RID + 1)):
        different = list(key)
        different[position] = replacement
        assert cache.acquire(tuple(different))[0] is None
    assert cache.acquire(key)[0] == entry
    assert cache.acquire(key, refresh=True)[0] is None
    # A failed refresh leaves no older entry that could be called fresh later.
    assert cache.acquire(key)[0] is None


def test_cache_row_and_entry_budgets_evict_oldest_without_losing_fresh_result():
    cache = FrameResourceUsageCache(capacity=2, row_budget=6)
    record = flow_record()
    entry = normalize_usage(usage_receipt(), record, RID)
    key = usage_cache_key(record, RID)
    _, token = cache.acquire(key)
    assert cache.store(key, token, entry)
    second = (*key[:-1], RID + 1)
    _, token = cache.acquire(second)
    assert cache.store(second, token, copy.deepcopy(entry))
    assert cache.acquire(key)[0] is None
    assert cache.acquire(second)[0] == entry
