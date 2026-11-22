"""Old failed inventories must not destroy an otherwise reusable saved diagram."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from src import server
from src.frame_render_flow import FrameRenderFlowStore, build_document
from test_frame_render_flow import SESSION, outline, stage
from test_server_frame_render_flow import setup, _evidence, _stage_inspection


@pytest.fixture
def saved(monkeypatch):
    store = FrameRenderFlowStore()
    monkeypatch.setattr(server, "_frame_render_flow_store", store)
    record = store.prepare(outline(2))
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 4)])
    return store, record, document


def empty_receipt(**changes):
    return {"captureSessionId": SESSION, "bindingsComplete": True, "bindingErrors": [],
            "expectedOutputs": [], "textureInputs": [], **changes}


@pytest.mark.parametrize("changes", [{}, {"details": {"shaders": {}, "inputs": [], "outputs": []}},
                                     {"pipelineUnavailable": True}])
def test_restore_quarantines_bad_inventory_without_discarding_diagram(saved, changes):
    store, record, document = saved
    document["observations"] = {"1": empty_receipt(**changes), "4": {
        "captureSessionId": SESSION, "expectedOutputs": [{"kind": "rtv", "index": 0, "resourceId": 100}]}}
    before = deepcopy(document)
    restored = server._adopt_frame_render_flow(document, record["outline"])
    assert restored["document"]["stages"][0]["title"] == document["stages"][0]["title"]
    assert restored["document"]["coverage"] == document["coverage"]
    assert "1" not in restored["observations"] and "4" in restored["observations"]
    assert restored["observationReadFailures"]["1"].startswith("bread-pipeline-unavailable:")
    assert "replayUnavailable" not in restored
    assert document == before


def test_failed_restored_inventory_preserves_existing_good_receipt(saved):
    store, record, document = saved
    original = {"captureSessionId": SESSION, "expectedOutputs": [{"kind": "rtv", "index": 0, "resourceId": 100}]}
    store.record_observation(SESSION, 1, original, record["flowId"])
    revision = store.get(SESSION)["observationRevision"]
    document["observations"] = {"1": empty_receipt()}
    restored = server._adopt_frame_render_flow(document, record["outline"])
    assert restored["observations"]["1"] == original
    assert restored["observationRevision"] == revision


@pytest.mark.parametrize("changes,match", [
    ({"captureSessionId": "b" * 32}, "another capture session"),
    ({"expectedOutputs": [{"kind": "invented", "resourceId": 7}]}, "RTV/DSV/UAV"),
    ({"bindingErrors": "invalid"}, "bounded list"),
])
def test_restore_does_not_hide_identity_or_schema_errors(saved, changes, match):
    _, record, document = saved
    document["observations"] = {"1": empty_receipt(pipelineUnavailable=True, **changes)}
    with pytest.raises(ValueError, match=match):
        server._adopt_frame_render_flow(document, record["outline"])


@pytest.mark.parametrize("changes", [{"outputs": []}, {"flags": 4}, {"hasWork": False}])
def test_empty_compact_receipt_without_proven_contradiction_is_not_discarded(monkeypatch, changes):
    store = FrameRenderFlowStore()
    monkeypatch.setattr(server, "_frame_render_flow_store", store)
    data = outline(1)
    data["actions"][0].update(changes)
    record = store.prepare(data)
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 1)])
    document["observations"] = {"1": empty_receipt()}
    restored = server._adopt_frame_render_flow(document, record["outline"])
    assert "1" in restored["observations"]
    assert not restored.get("observationReadFailures")


def test_quarantined_old_receipt_allows_first_healthy_read_without_refresh(setup, monkeypatch):
    prepared = json.loads(server.prepare_frame_render_flow())
    store = server._frame_render_flow_store
    record = store.get(SESSION)
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 12001)])
    document["observations"] = {"1": empty_receipt()}
    server._adopt_frame_render_flow(document, record["outline"])
    before = store.get(SESSION)
    assert "1" not in before["observations"] and "1" in before["observationReadFailures"]
    calls = []
    def inspect(method, params, **kwargs):
        assert method == "inspect_frame_render_stage"
        calls.append(params["event_id"])
        return _stage_inspection(1)
    monkeypatch.setattr(server.ipc, "call", inspect)
    result = json.loads(server.inspect_frame_render_stage(prepared["flowId"], 1))
    assert "error" not in result, result
    assert calls == [1] and not result["cacheHit"]
    assert result["currentReplayVerified"] is True
    assert result["evidenceFreshness"] == "current-read"
    after = store.get(SESSION)
    assert "1" not in after["observationReadFailures"]
    assert after["observationRevision"] == before["observationRevision"] + 1


@pytest.mark.parametrize("fatal", [True, False])
def test_publication_remembers_only_native_fatal_and_retains_saved_media(setup, monkeypatch, tmp_path, fatal):
    prepared = json.loads(server.prepare_frame_render_flow())
    manifest, png = _evidence(tmp_path / "evidence", event=12001)
    original_png = png.read_bytes()
    proposal = stage(1, 12001, images=[{"manifestPath": str(manifest), "title": "Scene color"}])
    calls = []
    message = ("bread-replay-unavailable: GPU synchronization failed" if fatal
               else "Channel display mapping could not be read")
    def precache(document, context):
        calls.append(document["flowId"])
        return {"attempted": 1, "prepared": 0, "failed": 1, "complete": False,
                "errors": [{"parent": "stage-1:0", "errors": [{"error": message}]}]}
    monkeypatch.setattr(server, "_precache_frame_flow_channels", precache)
    first = json.loads(server.publish_frame_render_flow(prepared["flowId"], [proposal], show_in_window=False, publication_scope='frame'))
    assert first["saved"] and not first["channelCache"]["complete"]
    record = server._frame_render_flow_store.get(SESSION)
    assert bool(record.get("replayUnavailable")) is fatal
    second = json.loads(server.publish_frame_render_flow(prepared["flowId"], [proposal], show_in_window=False, publication_scope='frame'))
    assert second["saved"] and second["savedWithCapture"]
    assert len(calls) == (1 if fatal else 2)
    if fatal:
        assert second["channelCache"]["attempted"] == 0
        assert second["channelCache"]["status"] == "saved-evidence-retained"
        assert second["channelCache"]["currentReplayVerified"] is False
        # No per-event receipt existed for this batch failure. Later inspection
        # still must not call the failed native controller through another tool.
        inspected = json.loads(server.inspect_frame_render_stage(prepared["flowId"], 4))
        assert "bread-replay-unavailable" in inspected["error"]
    saved_document = json.loads(Path(second["documentPath"]).read_text(encoding="utf-8"))
    assert saved_document["stages"][0]["images"][0]["path"] == str(png)
    assert png.read_bytes() == original_png


def test_batch_fatal_cannot_poison_reopened_capture_session(setup, monkeypatch):
    prepared = json.loads(server.prepare_frame_render_flow())
    store = server._frame_render_flow_store
    store.mark_replay_unavailable(SESSION, "bread-replay-unavailable: old controller failed", prepared["flowId"])
    next_session = "b" * 32
    with pytest.raises(ValueError, match="Capture session changed"):
        store.mark_replay_unavailable(next_session, "unrelated failure", prepared["flowId"])
    data = outline(2)
    data["captureSessionId"] = next_session
    reopened = store.prepare(data)
    monkeypatch.setattr(server, "_active_live_context", lambda **kwargs: {
        "capture": {"captureSessionId": next_session, "captureId": "", "path": data["capturePath"], "api": "D3D12"}})
    calls = []
    def inspect(method, params, **kwargs):
        assert method == "inspect_frame_render_stage"
        assert params["expected_capture_session_id"] == next_session
        calls.append(params["event_id"])
        return _stage_inspection(1, session=next_session)
    monkeypatch.setattr(server.ipc, "call", inspect)
    result = json.loads(server.inspect_frame_render_stage(reopened["flowId"], 1))
    assert calls == [1] and not result["cacheHit"]
    assert result["currentReplayVerified"] is True and result["sourceCaptureSessionId"] == next_session
    assert not store.get(next_session).get("replayUnavailable")
    assert "old controller failed" in store.get(SESSION)["replayUnavailable"]["error"]
