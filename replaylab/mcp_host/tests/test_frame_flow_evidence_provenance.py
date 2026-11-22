"""Portable evidence stays useful without becoming a new replay observation."""
from copy import deepcopy
import json

from src import server
from src.frame_render_flow import FrameRenderFlowStore, build_document
from test_frame_render_flow import SESSION, outline, stage
from test_frame_flow_persistence import bundle, storage
from test_server_frame_render_flow import setup, _stage_inspection


def test_rebind_retains_original_observation_source_across_multiple_sessions(bundle):
    capture, document, data, root = bundle
    document["observations"] = {"1": {"captureSessionId": SESSION,
        "expectedOutputs": [{"kind": "rtv", "index": 0, "resourceId": 100}]}}
    storage.save(capture, document, data, root)
    loaded = storage.load(capture, root)
    current = deepcopy(data)
    current["captureSessionId"] = "b" * 32
    first = storage.rebind(loaded, current)
    observation = first["document"]["observations"]["1"]
    assert observation["captureSessionId"] == "b" * 32
    assert observation["sourceCaptureSessionId"] == SESSION
    assert observation["evidenceFreshness"] == "previously-captured"
    assert observation["currentReplayVerified"] is False
    loaded["document"] = first["document"]
    current["captureSessionId"] = "c" * 32
    second = storage.rebind(loaded, current)
    assert second["document"]["observations"]["1"]["sourceCaptureSessionId"] == SESSION


def test_compact_persistence_keeps_provenance_and_old_review_is_not_new_inspection():
    store = FrameRenderFlowStore()
    record = store.prepare(outline(1))
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 1)])
    store.publish(record["flowId"], document)
    store.begin_reanalysis(SESSION, record["flowId"])
    historical = {"captureSessionId": SESSION, "sourceCaptureSessionId": "b" * 32,
        "evidenceFreshness": "previously-captured", "currentReplayVerified": False,
        "expectedOutputs": [{"kind": "rtv", "index": 0, "resourceId": 100}]}
    store.record_observation(SESSION, 1, historical, record["flowId"])
    record = store.get(SESSION)
    assert store.reanalysis_status(record)["newlyInspectedEventIds"] == []
    assert store.reanalysis_status(record)["reviewComplete"] is False
    rebuilt = build_document(record["outline"], record["flowId"], record["snapshotId"],
                             [stage(1, 1)], observations=record["observations"])
    for field in ("sourceCaptureSessionId", "evidenceFreshness", "currentReplayVerified"):
        assert rebuilt["observations"]["1"][field] == historical[field]


def test_historical_cache_is_not_a_new_observation_and_fresh_read_alone_is_not_reanalysis(setup, monkeypatch):
    prepared = json.loads(server.prepare_frame_render_flow())
    flow = prepared["flowId"]
    old = dict(_stage_inspection(1), sourceCaptureSessionId="b" * 32,
               evidenceFreshness="previously-captured", currentReplayVerified=False)
    server._frame_render_flow_store.record_observation(SESSION, 1, old, flow)
    server._frame_render_flow_store.begin_reanalysis(SESSION, flow)
    revision = server._frame_render_flow_store.get(SESSION)["observationRevision"]
    reads = []
    def inspect(method, params, **kwargs):
        reads.append(params)
        assert method == "inspect_frame_render_stage"
        return _stage_inspection(1)
    monkeypatch.setattr(server.ipc, "call", inspect)
    cached = json.loads(server.inspect_frame_render_stage(flow, 1))
    assert cached["cacheHit"] and cached["currentReplayVerified"] is False
    record = server._frame_render_flow_store.get(SESSION)
    assert record["observationRevision"] == revision
    assert server._frame_render_flow_store.reanalysis_status(record)["newlyInspectedEventIds"] == []
    fresh = json.loads(server.inspect_frame_render_stage(flow, 1, refresh=True))
    assert fresh["currentReplayVerified"] is True and fresh["sourceCaptureSessionId"] == SESSION
    assert fresh["evidenceFreshness"] == "current-read" and len(reads) == 1
    record = server._frame_render_flow_store.get(SESSION)
    assert server._frame_render_flow_store.reanalysis_status(record)["newlyInspectedEventIds"] == [1]
    assert server._frame_render_flow_store.reanalysis_status(record)["reviewComplete"] is False
