from __future__ import annotations

import json

import pytest

from src import server
from src.frame_render_flow import FrameRenderFlowStore
from src.frame_resource_usage import FrameResourceUsageCache
from test_frame_resource_usage import RID, SESSION, flow_record, usage_receipt


@pytest.fixture
def rig(monkeypatch):
    record = flow_record()
    store = FrameRenderFlowStore()
    store.prepare(record["outline"])
    monkeypatch.setattr(server, "_frame_render_flow_store", store)
    monkeypatch.setattr(server, "_frame_resource_usage_cache", FrameResourceUsageCache())
    monkeypatch.setattr(server, "_active_live_context", lambda **kwargs: {
        "capture": {"captureSessionId": SESSION, "captureId": "", "path": "C:/captures/frame.rdc"}})
    checks = []
    monkeypatch.setattr(server, "_require_same_active_capture", lambda *args, **kwargs: checks.append(args))
    calls = []
    def bridge(method, parameters, **kwargs):
        assert method == "find_by_resource"
        assert parameters["resource_id"] == RID
        assert parameters["expected_capture_session_id"] == SESSION
        assert parameters["__replaylab_expected_capture_session_id"] == SESSION
        calls.append(parameters)
        return usage_receipt()
    monkeypatch.setattr(server.ipc, "call", bridge)
    return record, calls, checks


def test_registered_tool_reuses_one_collection_for_directions_pages_and_refresh(rig):
    record, calls, checks = rig
    first = json.loads(server.inspect_frame_resource_usage(record["flowId"], RID, 70, "before", limit=1))
    assert "error" not in first, first
    assert first["captureSessionId"] == SESSION and first["snapshotId"] == record["snapshotId"]
    assert first["candidateOnly"] and not first["cacheHit"] and first["complete"]
    assert first["hasMore"] and first["nextOffset"] == 1
    second = json.loads(server.inspect_frame_resource_usage(record["flowId"], RID, 70, "before", offset=1, limit=1))
    after = json.loads(server.inspect_frame_resource_usage(record["flowId"], RID, 70, "after"))
    assert second["cacheHit"] and after["cacheHit"] and len(calls) == 1
    assert [item["eventId"] for item in second["items"]] == [20]
    assert [item["eventId"] for item in after["items"]] == [10, 50]
    refreshed = json.loads(server.inspect_frame_resource_usage(record["flowId"], RID, refresh=True))
    assert not refreshed["cacheHit"] and len(calls) == 2
    assert len(checks) == 4  # Cache hits must check the same active capture too.


def test_nearest_tracing_uses_existing_resource_collection_in_both_directions(rig):
    record, calls, _ = rig
    first = json.loads(server.inspect_frame_resource_usage(record["flowId"], RID, 70, "before", order="nearest", limit=1))
    second = json.loads(server.inspect_frame_resource_usage(record["flowId"], RID, 70, "before", order="nearest", offset=1, limit=1))
    after = json.loads(server.inspect_frame_resource_usage(record["flowId"], RID, 70, "after", order="nearest"))
    assert [row["eventId"] for row in first["items"]] == [20]
    assert [row["eventId"] for row in second["items"]] == [100]
    assert [row["eventId"] for row in after["items"]] == [10, 50]
    assert first["candidateOnly"] and second["cacheHit"] and after["cacheHit"]
    assert len(calls) == 1


@pytest.mark.parametrize("arguments", [
    {"anchor_event_id": 2}, {"anchor_event_id": 0, "direction": "after"},
    {"resource_id": True}, {"limit": 201}, {"refresh": "true"},
])
def test_invalid_request_never_reaches_replay(rig, arguments):
    record, calls, _ = rig
    result = json.loads(server.inspect_frame_resource_usage(**{
        "flow_id": record["flowId"], "resource_id": RID, **arguments}))
    assert "error" in result and calls == []


@pytest.mark.parametrize("patch", [
    {"resourceId": RID + 1}, {"captureSessionId": "b" * 32},
    {"capture": {"captureSessionId": "b" * 32}},
])
def test_wrong_bridge_identity_never_enters_cache(rig, monkeypatch, patch):
    record, _, _ = rig
    monkeypatch.setattr(server.ipc, "call", lambda *args, **kwargs: {**usage_receipt(), **patch})
    result = json.loads(server.inspect_frame_resource_usage(record["flowId"], RID))
    assert "error" in result
    calls = []
    monkeypatch.setattr(server.ipc, "call", lambda *args, **kwargs: calls.append(args) or usage_receipt())
    valid = json.loads(server.inspect_frame_resource_usage(record["flowId"], RID))
    assert not valid["cacheHit"] and len(calls) == 1


def test_capture_change_during_collection_cannot_publish_or_cache_receipt(rig, monkeypatch):
    record, calls, _ = rig
    def changed(*args, **kwargs):
        raise RuntimeError("capture changed during resource query")
    monkeypatch.setattr(server, "_require_same_active_capture", changed)
    result = json.loads(server.inspect_frame_resource_usage(record["flowId"], RID))
    assert "capture changed" in result["error"] and "items" not in result
    monkeypatch.setattr(server, "_require_same_active_capture", lambda *args, **kwargs: None)
    valid = json.loads(server.inspect_frame_resource_usage(record["flowId"], RID))
    assert not valid["cacheHit"] and len(calls) == 2


def test_cached_result_is_not_released_to_another_capture(rig, monkeypatch):
    record, calls, _ = rig
    assert "error" not in json.loads(server.inspect_frame_resource_usage(record["flowId"], RID))
    monkeypatch.setattr(server, "_active_live_context", lambda **kwargs: {
        "capture": {"captureSessionId": "b" * 32, "captureId": "", "path": "C:/captures/frame.rdc"}})
    result = json.loads(server.inspect_frame_resource_usage(record["flowId"], RID))
    assert "Capture session changed" in result["error"] and len(calls) == 1


def test_failed_explicit_refresh_does_not_silently_reuse_old_entry(rig, monkeypatch):
    record, _, _ = rig
    assert "error" not in json.loads(server.inspect_frame_resource_usage(record["flowId"], RID))
    responses = iter(({"error": "bridge failed"}, usage_receipt((20, 70))))
    monkeypatch.setattr(server.ipc, "call", lambda *args, **kwargs: next(responses))
    assert "error" in json.loads(server.inspect_frame_resource_usage(record["flowId"], RID, refresh=True))
    result = json.loads(server.inspect_frame_resource_usage(record["flowId"], RID))
    assert not result["cacheHit"] and [row["eventId"] for row in result["items"]] == [20, 70]
