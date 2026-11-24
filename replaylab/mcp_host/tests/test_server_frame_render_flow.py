from __future__ import annotations

import hashlib
import json
import copy
from pathlib import Path
import struct

import pytest

from src import server
from src.frame_render_flow import FrameRenderFlowStore, build_document
from test_frame_render_flow import SESSION, outline, stage


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_frame_render_flow_store", FrameRenderFlowStore())
    monkeypatch.setenv("RENDERDOC_ULTRA_EVENT_ARTIFACTS_HOME", str(tmp_path / "diagrams"))
    monkeypatch.setattr(server, "_EVENT_TEXTURE_EVIDENCE_ROOT", tmp_path / "evidence")
    monkeypatch.setattr(server, "_active_live_context", lambda **kwargs: {
        "capture": {"captureSessionId": SESSION, "captureId": "", "path": "C:/captures/frame.rdc", "api": "D3D12"}})
    monkeypatch.setattr(server, "_require_same_active_capture", lambda *args, **kwargs: None)
    calls = []
    data = outline(4001)
    def bridge(method, params, **kwargs):
        calls.append((method, params))
        if method == "get_frame_render_flow_outline":
            offset = params["offset"]
            result = dict(data)
            result.update(offset=offset, actions=data["actions"][offset:offset+4000],
                          hasMore=offset+4000 < len(data["actions"]),
                          nextOffset=offset+4000 if offset+4000 < len(data["actions"]) else None)
            return result
        if method == "show_frame_render_flow":
            return {"published": True, "flowId": params["document"]["flowId"]}
        if method == "save_frame_render_flow":
            assert params["document"]["captureSessionId"] == SESSION
            assert params["outline"]["captureSessionId"] == SESSION
            return {"saved": True, "portable": True, "directory": str(tmp_path / "capture.rdc.bread")}
        raise AssertionError("Unexpected heavy replay tool: " + method)
    monkeypatch.setattr(server.ipc, "call", bridge)
    return calls, data


def test_preflight_returns_all_stage_errors_and_publishes_a_patched_draft(setup, monkeypatch):
    calls, _ = setup
    flow = json.loads(server.prepare_frame_render_flow())["flowId"]
    calls.clear()
    store = server._frame_render_flow_store
    store.record_observation(SESSION, 4, {"expectedOutputs": [
        {"resourceId": 100, "kind": "rtv", "index": 0, "mip": 0},
        {"resourceId": 100, "kind": "rtv", "index": 0, "mip": 1},
    ]}, flow)
    omission = {"resourceId": 100, "kind": "rtv", "index": 0, "reason": "Auxiliary view", "evidence": "Observed RTV"}
    invalid = [stage(2, 2, category="invalid", techniques=[{"name": "Test", "status": []}],
                     investigation={"status": "done"}),
               stage(4, 4, outputOmissions=[omission, {**omission, "resourceId": 999},
                                            {**omission, "mip": 0}, {**omission, "mip": 0}])]
    direct = json.loads(server.publish_frame_render_flow(flow, invalid, publication_scope="frame"))
    first = json.loads(server.validate_frame_render_flow(flow, invalid, title="Example"))
    assert first["valid"] is False and first["issueCount"] == 7
    assert first["issues"] == direct["issues"]
    errors = {issue["path"]: issue["message"] for issue in first["issues"]}
    assert "actual EIDs" in errors["stages[0].bounds"]
    assert "recognized, inferred" in errors["stages[0].techniques"]
    assert "investigated, partial" in errors["stages[0].investigation"]
    assert "2 observed matches" in errors["stages[1].outputOmissions[0]"]
    assert "no observed match" in errors["stages[1].outputOmissions[1]"]
    assert "duplicates outputOmissions[2]" in errors["stages[1].outputOmissions[3]"]
    fixed = json.loads(server.validate_frame_render_flow(flow, draft_id=first["draftId"], stage_updates={
        "0": {"startEventId": 1, "endEventId": 1, "category": "geometry", "techniques": [], "investigation": {}},
        "1": {"outputOmissions": [{**omission, "mip": 1}]},
    }))
    assert fixed["valid"] is True and not fixed["saved"] and not fixed["replayed"]
    assert not calls and store.get(SESSION)["document"] is None
    assert store.get_draft(flow, first["draftId"])["stages"] == invalid
    monkeypatch.setattr(server, "_precache_frame_flow_channels", lambda *args: {})
    published = json.loads(server.publish_frame_render_flow(flow, draft_id=fixed["draftId"], show_in_window=False, publication_scope="frame"))
    assert published["savedWithCapture"] is True and published["stageCount"] == 2, published
    assert [method for method, _ in calls] == ["save_frame_render_flow"]
    assert store.get(SESSION)["document"]["title"] == "Example"


def test_preflight_requires_a_cached_current_capture_and_drafts_expire_on_reanalysis(setup):
    calls, _ = setup
    assert "prepare_frame_render_flow" in json.loads(server.validate_frame_render_flow("missing", [stage(1, 1)]))["error"]
    assert not calls
    flow = json.loads(server.prepare_frame_render_flow())["flowId"]
    first = json.loads(server.validate_frame_render_flow(flow, [stage(1, 1)]))
    server._frame_render_flow_store.begin_reanalysis(SESSION, flow)
    result = json.loads(server.validate_frame_render_flow(flow, draft_id=first["draftId"]))
    assert "No draft revision" in result["error"]


def test_prepare_collects_complete_paged_tree_then_reads_cache(setup):
    calls, data = setup
    first = json.loads(server.prepare_frame_render_flow(refresh=True))
    assert "error" not in first, first
    assert len(calls) == 2
    assert [value[1]["refresh"] for value in calls] == [True, False]
    assert first["source"]["totalActionCount"] == 4001
    assert first["total"] == 401 and first["hasMore"]
    second = json.loads(server.prepare_frame_render_flow())
    assert second["cacheHit"] is True and len(calls) == 2
    last = json.loads(server.get_frame_render_flow(first["flowId"], offset=4000, section="actions"))
    assert last["items"] == server._frame_render_flow_store.get(SESSION)["outline"]["actions"][-1:]
    assert not last["hasMore"] and len(calls) == 2


def test_saved_diagram_receipt_from_previous_same_path_session_is_rejected(setup, monkeypatch):
    _calls, data = setup
    previous = copy.deepcopy(data)
    previous["captureSessionId"] = "b" * 32
    old = FrameRenderFlowStore().prepare(previous)
    document = build_document(old["outline"], old["flowId"], old["snapshotId"], [stage(1, 12001)])
    def stale_bridge(method, params, **kwargs):
        assert method == "get_saved_frame_render_flow"
        assert params["expected_capture_session_id"] == SESSION
        return {"document": document, "outline": old["outline"]}
    monkeypatch.setattr(server.ipc, "call", stale_bridge)
    response = json.loads(server.get_frame_render_flow())
    assert "No matching saved frame diagram" in response["error"]
    # A consistent historical receipt is not enough to populate current state.
    assert not server._frame_render_flow_store._records


def test_capture_switch_while_loading_saved_diagram_does_not_populate_store(setup, monkeypatch):
    _calls, data = setup
    record = FrameRenderFlowStore().prepare(data)
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 12001)])
    monkeypatch.setattr(server.ipc, "call", lambda *args, **kwargs: {
        "document": document, "outline": record["outline"]})
    def changed(*args, **kwargs):
        raise RuntimeError("RenderDoc capture/session changed during the active-project operation")
    monkeypatch.setattr(server, "_require_same_active_capture", changed)
    response = json.loads(server.get_frame_render_flow())
    assert "capture/session changed" in response["error"]
    assert not server._frame_render_flow_store._records


@pytest.mark.parametrize("has_new_draft", [False, True])
def test_outline_refresh_does_not_restore_old_semantic_nodes_into_reanalysis(setup, has_new_draft):
    _calls, data = setup
    prepared = json.loads(server.prepare_frame_render_flow())
    store = server._frame_render_flow_store
    record = store.get(SESSION)
    old = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 12001)])
    store.publish(record["flowId"], old)
    data["restoredDocument"] = old
    fresh = json.loads(server.prepare_frame_render_flow(reanalyze=True))
    assert fresh["published"] is False
    assert not json.loads(server.get_frame_render_flow(section="proposals"))["items"]
    if has_new_draft:
        new = copy.deepcopy(old)
        new["stages"][0]["summary"] = "Independently reconstructed module."
        store.publish(record["flowId"], new)
    refreshed = json.loads(server.prepare_frame_render_flow(refresh=True))
    assert "error" not in refreshed, refreshed
    latest = store.get(SESSION)
    assert latest["baselineDocument"] == old
    assert latest["document"] == (new if has_new_draft else None)
    assert refreshed["flowId"] == prepared["flowId"]


def test_failed_refresh_preserves_old_bindings_without_replay_fanout(setup, monkeypatch):
    prepared = json.loads(server.prepare_frame_render_flow())
    flow = prepared["flowId"]
    old = {"schema": "bread-frame-stage-evidence/v1", "captureSessionId": SESSION,
           "eventId": 1, "inventoryVersion": 3, "bindingsComplete": True,
           "details": {"shaders": {"vertex": {"shaderId": 7}}}, "textureInputs": [],
           "expectedOutputs": [{"kind": "rtv", "resourceId": 100, "index": 0}]}
    server._frame_render_flow_store.record_observation(SESSION, 1, old, flow)
    revision = server._frame_render_flow_store.get(SESSION)["observationRevision"]
    calls = []
    def failed(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("bread-replay-unavailable: native GPU synchronization failed")
    monkeypatch.setattr(server.ipc, "call", failed)
    result = json.loads(server.inspect_frame_render_stage(flow, 1, refresh=True))
    assert result["cacheHit"] and result["refreshSucceeded"] is False
    assert result["expectedOutputs"] == old["expectedOutputs"]
    assert result["evidenceFreshness"] == "previously-captured"
    assert server._frame_render_flow_store.get(SESSION)["observationRevision"] == revision
    cached = json.loads(server.inspect_frame_render_stage(flow, 1))
    assert cached["refreshSucceeded"] is False and cached["currentReplayVerified"] is False
    assert server._frame_render_flow_store.get(SESSION)["observationRevision"] == revision
    missing = json.loads(server.inspect_frame_render_stage(flow, 4, refresh=True))
    assert "bread-replay-unavailable" in str(missing["error"])
    assert len(calls) == 1


def test_legacy_utility_inventory_refreshes_once_without_user_request(setup, monkeypatch):
    calls, data = setup
    data["actions"][-1].update(flags=0x0800, actionKind="utility", outputs=[], copyDestination=505)
    prepared = json.loads(server.prepare_frame_render_flow())
    old = {"schema": "bread-frame-stage-evidence/v1", "eventId": 12001,
           "captureSessionId": SESSION, "details": {"actionKind": "utility"},
           "expectedOutputs": [], "textureInputs": [], "bindingsComplete": True}
    server._frame_render_flow_store.record_observation(SESSION, 12001, old, prepared["flowId"])
    refreshed = dict(old, inventoryVersion=2, expectedOutputs=[{
        "kind": "copy-destination", "index": 0, "resourceId": 505,
        "resourceType": "buffer", "captureSupported": False}])
    reads = []
    def inspect(method, params, **kwargs):
        assert method == "inspect_frame_render_stage"
        reads.append(params["event_id"])
        return refreshed
    monkeypatch.setattr(server.ipc, "call", inspect)
    first = json.loads(server.inspect_frame_render_stage(prepared["flowId"], 12001))
    assert first["cacheHit"] is False and first["inventoryVersion"] == 2
    second = json.loads(server.inspect_frame_render_stage(prepared["flowId"], 12001))
    assert second["cacheHit"] is True and second["expectedOutputs"][0]["resourceId"] == 505
    assert reads == [12001]


def test_publish_window_receipt_and_exact_coverage(setup):
    calls, _ = setup
    prepared = json.loads(server.prepare_frame_render_flow())
    result = json.loads(server.publish_frame_render_flow(prepared["flowId"], [stage(1, 12001)],
                                                       title="整帧渲染流程", summary="阶段解释", publication_scope='frame'))
    assert "error" not in result, result
    assert result["saved"] and result["window"]["published"]
    assert result["savedWithCapture"] and result["persistence"]["portable"]
    assert result["coverage"]["complete"] and result["coverage"]["coveredActionCount"] == 4001
    document = json.loads(Path(result["documentPath"]).read_text(encoding="utf-8"))
    assert len(document["stages"][0]["eventIds"]) == 4001
    assert document["stages"][0]["representativeEventId"] == 12001
    assert calls[-1][0] == "show_frame_render_flow"


def test_window_failure_is_not_claimed_as_displayed(setup, monkeypatch):
    prepared = json.loads(server.prepare_frame_render_flow())
    monkeypatch.setattr(server.ipc, "call", lambda *args, **kwargs: {"error": "capture closed"})
    result = json.loads(server.publish_frame_render_flow(prepared["flowId"], [stage(1, 12001)], publication_scope='frame'))
    assert result["saved"] and result["window"]["requested"]
    assert not result["window"]["published"]
    assert "capture closed" in result["window"]["error"]


def test_publication_explains_omitted_repeated_snapshot_graph_without_replay(setup, tmp_path):
    calls, _ = setup
    prepared = json.loads(server.prepare_frame_render_flow())
    manifest, _ = _evidence(tmp_path / "evidence", event=12001)
    graph = {"nodes": [{"id": "surface", "label": "Surface", "imageIndex": 0, "description": ""},
                       {"id": "lighting", "label": "Lighting", "imageIndex": 0, "description": ""}],
             "edges": [{"from": "surface", "to": "lighting", "label": ""}]}
    proposal = stage(1, 12001, images=[{"manifestPath": str(manifest),
        "title": "Scene color", "description": "Accumulated scene illumination."}], processGraph=graph)
    result = json.loads(server.publish_frame_render_flow(prepared["flowId"], [proposal], show_in_window=False, publication_scope='frame'))
    assert "error" not in result, result
    assert result["saved"] and result["savedWithCapture"]
    assert result["presentation"] == [{"stageId": "stage-1", "kind": "processGraph",
        "decision": "omit", "reason": "repeated-resource-snapshot", "verifiedTransferCount": 0}]
    document = json.loads(Path(result["documentPath"]).read_text(encoding="utf-8"))
    assert document["stages"][0]["processGraph"] == graph
    assert len(document["stages"][0]["images"]) == 1
    assert all(method in ("get_frame_render_flow_outline", "save_frame_render_flow") for method, _ in calls)


def test_published_diagram_restores_after_host_restart_without_bridge_replay(setup, monkeypatch):
    prepared = json.loads(server.prepare_frame_render_flow())
    published = json.loads(server.publish_frame_render_flow(prepared["flowId"], [stage(1, 12001)], show_in_window=False, publication_scope='frame'))
    assert published["saved"]
    monkeypatch.setattr(server, "_frame_render_flow_store", FrameRenderFlowStore())
    monkeypatch.setattr(server.ipc, "call", lambda *args, **kwargs: pytest.fail("Replay was invoked for a persisted diagram"))
    restored = json.loads(server.get_frame_render_flow(prepared["flowId"]))
    assert "error" not in restored, restored
    assert restored["published"] and restored["coverage"]["complete"]
    assert restored["items"][0]["actionCount"] == 4001
    all_actions = json.loads(server.get_frame_render_flow(prepared["flowId"], offset=4000, section="actions"))
    assert all_actions["items"][0]["eventId"] == 12001


def test_cross_capture_flow_cannot_publish(setup, monkeypatch):
    prepared = json.loads(server.prepare_frame_render_flow())
    monkeypatch.setattr(server, "_active_live_context", lambda **kwargs: {
        "capture": {"captureSessionId": "b" * 32, "path": "C:/captures/frame.rdc"}})
    result = json.loads(server.publish_frame_render_flow(prepared["flowId"], [stage(1, 12001)], publication_scope='frame'))
    assert "error" in result and not result.get("saved")


@pytest.mark.parametrize("failed_first_publication", [False, True])
def test_host_restart_keeps_requested_reanalysis_separate_from_saved_baseline(setup, monkeypatch, tmp_path, failed_first_publication):
    from src import task_report
    ledger = task_report.TaskReportStore("fresh-analysis", tmp_path / "tasks")
    ledger.begin_task_review(hashlib.sha256(b"regenerate this frame").hexdigest())
    monkeypatch.setattr(task_report, "current_task_report_store", lambda: ledger)
    prepared = json.loads(server.prepare_frame_render_flow())
    old = json.loads(server.publish_frame_render_flow(prepared["flowId"], [stage(1, 12001)], show_in_window=False, publication_scope='frame'))
    assert old["saved"]
    json.loads(server.prepare_frame_render_flow(reanalyze=True))
    if failed_first_publication:
        ledger.record_result("publish_frame_render_flow", "Invalid attachment", failed=True,
                             arguments={"flow_id": prepared["flowId"]})
    assert ledger.frame_reanalysis_pending(prepared["flowId"])
    monkeypatch.setattr(server, "_frame_render_flow_store", FrameRenderFlowStore())
    proposals = json.loads(server.get_frame_render_flow(prepared["flowId"], section="proposals"))
    assert "error" not in proposals, proposals
    assert proposals["items"] == [] and not proposals["published"]
    assert proposals["baselineAvailable"] and proposals["reanalysisRequested"]
    assert not proposals["reanalysis"]["reviewComplete"]


def _stage_inspection(event=12001, session=SESSION):
    return {
        "schema": "bread-frame-stage-evidence/v1", "inventoryVersion": 3, "eventId": event,
        "captureSessionId": session,
        "expectedOutputs": [{"resourceId": 2**48, "kind": "rtv", "index": 0,
                             "resourceType": "texture", "captureSupported": True}],
        "inputs": [{"resourceId": 7, "stage": "pixel", "index": 0}],
        "details": {"pipelineType": "graphics"},
        "textureInputs": [{"resourceId": 7, "stage": "fragment", "index": 0}],
        "shaders": [{"resourceId": 8, "stage": "pixel"}],
    }


def test_old_texture_inventory_refreshes_once_to_get_descriptor_ranges(setup, monkeypatch):
    prepared = json.loads(server.prepare_frame_render_flow())
    old = _stage_inspection()
    old["inventoryVersion"] = 2
    server._frame_render_flow_store.record_observation(SESSION, 12001, old)
    calls = []
    updated = _stage_inspection()
    updated["textureInputs"][0].update(mip=0, numMips=5, slice=2, numSlices=3)
    updated["expectedOutputs"][0].update(mip=1, numMips=1, slice=2, numSlices=3)
    def inspect(method, params, **kwargs):
        assert method == "inspect_frame_render_stage"
        calls.append(params["event_id"])
        return updated
    monkeypatch.setattr(server.ipc, "call", inspect)
    first = json.loads(server.inspect_frame_render_stage(prepared["flowId"], 12001))
    second = json.loads(server.inspect_frame_render_stage(prepared["flowId"], 12001))
    assert not first["cacheHit"] and second["cacheHit"]
    assert second["textureInputs"][0]["numMips"] == 5 and calls == [12001]
    assert second["expectedOutputs"][0]["numSlices"] == 3
    published = server.build_frame_render_flow_document(
        server._frame_render_flow_store.get(SESSION)["outline"], prepared["flowId"], prepared["snapshotId"],
        [stage(1, 12001)], observations={"12001": second})
    assert published["observations"]["12001"]["expectedOutputs"][0]["numSlices"] == 3


def test_stage_inspection_caches_complete_inventory_and_refreshes_on_request(setup, monkeypatch):
    prepared = json.loads(server.prepare_frame_render_flow())
    inspections = []
    def inspect(method, params, **kwargs):
        assert method == "inspect_frame_render_stage"
        assert params["event_id"] == 12001
        assert params["expected_capture_session_id"] == SESSION
        assert params["__replaylab_expected_capture_session_id"] == SESSION
        inspections.append(params)
        return _stage_inspection()
    monkeypatch.setattr(server.ipc, "call", inspect)
    first = json.loads(server.inspect_frame_render_stage(prepared["flowId"], 12001))
    assert "error" not in first, first
    assert not first["cacheHit"] and first["flowId"] == prepared["flowId"]
    assert first["expectedOutputs"][0]["resourceId"] == 2**48
    assert first["action"]["eventId"] == 12001 and first["shaders"]
    cached = json.loads(server.inspect_frame_render_stage(prepared["flowId"], 12001))
    assert cached["cacheHit"] and cached["inputs"] == first["inputs"]
    assert len(inspections) == 1
    refreshed = json.loads(server.inspect_frame_render_stage(prepared["flowId"], 12001, refresh=True))
    assert not refreshed["cacheHit"] and len(inspections) == 2
    observation = server._frame_render_flow_store.get(SESSION)["observations"]["12001"]
    assert observation["schema"] == "bread-frame-stage-evidence/v1"


@pytest.mark.parametrize("wrong_identity", [{"captureSessionId": "b" * 32}, {"eventId": 4}])
def test_stage_inspection_rejects_mismatched_bridge_identity_without_caching(setup, monkeypatch, wrong_identity):
    prepared = json.loads(server.prepare_frame_render_flow())
    response = {**_stage_inspection(), **wrong_identity}
    monkeypatch.setattr(server.ipc, "call", lambda *args, **kwargs: response)
    result = json.loads(server.inspect_frame_render_stage(prepared["flowId"], 12001))
    assert "mismatched capture/event evidence" in result["error"]
    assert not server._frame_render_flow_store.get(SESSION).get("observations")


def test_stage_inspection_rejects_nonexistent_eid_before_replay(setup, monkeypatch):
    prepared = json.loads(server.prepare_frame_render_flow())
    monkeypatch.setattr(server.ipc, "call", lambda *args, **kwargs: pytest.fail("Invalid EID reached replay"))
    result = json.loads(server.inspect_frame_render_stage(prepared["flowId"], 2))
    assert "not part of this frame outline" in result["error"]


def test_stage_inspection_rejects_flow_from_another_active_capture(setup, monkeypatch):
    prepared = json.loads(server.prepare_frame_render_flow())
    monkeypatch.setattr(server, "_active_live_context", lambda **kwargs: {
        "capture": {"captureSessionId": "b" * 32, "path": "C:/captures/frame.rdc"}})
    monkeypatch.setattr(server.ipc, "call", lambda *args, **kwargs: pytest.fail("Different capture reached replay"))
    result = json.loads(server.inspect_frame_render_stage(prepared["flowId"], 12001))
    assert "error" in result


def test_stage_inspection_refreshes_compact_inventory_restored_after_restart(setup, monkeypatch):
    prepared = json.loads(server.prepare_frame_render_flow())
    server._frame_render_flow_store.record_observation(SESSION, 12001, _stage_inspection())
    saved = json.loads(server.publish_frame_render_flow(prepared["flowId"], [stage(1, 12001)], show_in_window=False, publication_scope='frame'))
    assert saved["saved"]
    monkeypatch.setattr(server, "_frame_render_flow_store", FrameRenderFlowStore())
    calls = []
    def inspect(method, params, **kwargs):
        assert method == "inspect_frame_render_stage"
        calls.append(params)
        return _stage_inspection()
    monkeypatch.setattr(server.ipc, "call", inspect)
    result = json.loads(server.inspect_frame_render_stage(prepared["flowId"], 12001))
    assert "error" not in result, result
    assert not result["cacheHit"] and len(calls) == 1
    assert result["schema"] == "bread-frame-stage-evidence/v1" and result["shaders"]


def _evidence(root: Path, session=SESSION, event=1):
    folder = root / ("eid-%d-%s" % (event, "c" * 32))
    folder.mkdir(parents=True)
    png = folder / "evidence.png"
    # Decoder bytes are irrelevant here: the loader verifies header dimensions
    # and content integrity. Native acceptance exercises a real exported PNG.
    content = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 32, 16)
    png.write_bytes(content)
    manifest = {"schema": server._EVENT_TEXTURE_EVIDENCE_HOST_SCHEMA, "eventId": event, "status": "saved",
                "capture": {"captureSessionId": session, "path": "C:/captures/frame.rdc"},
                "artifact": {"output": {"path": str(png), "mimeType": "image/png", "byteSize": len(content),
                    "sha256": hashlib.sha256(content).hexdigest()},
                    "attachment": {"resourceId": 2**48, "format": "RGBA16F", "kind": "rtv", "index": 0},
                    "display": {"channel": "rgba"}}}
    path = folder / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, png


def test_only_bound_managed_untampered_images_are_admitted(setup, tmp_path):
    prepared = json.loads(server.prepare_frame_render_flow())
    manifest, png = _evidence(tmp_path / "evidence")
    result = json.loads(server.publish_frame_render_flow(prepared["flowId"],
        [stage(1, 12001, images=[{"manifestPath": str(manifest), "title": "Color output"}])], show_in_window=False, publication_scope='frame'))
    assert "error" not in result, result
    document = json.loads(Path(result["documentPath"]).read_text(encoding="utf-8"))
    assert document["stages"][0]["images"][0]["resourceId"] == 2**48
    png.write_bytes(png.read_bytes() + b"tampered")
    changed = json.loads(server.publish_frame_render_flow(prepared["flowId"],
        [stage(1, 12001, images=[{"manifestPath": str(manifest)}])], publication_scope='frame'))
    assert "error" in changed and not changed.get("saved")


def test_wrong_session_image_is_rejected(setup, tmp_path):
    prepared = json.loads(server.prepare_frame_render_flow())
    manifest, _ = _evidence(tmp_path / "evidence", session="d" * 32)
    result = json.loads(server.publish_frame_render_flow(prepared["flowId"],
        [stage(1, 12001, images=[{"manifestPath": str(manifest)}])], publication_scope='frame'))
    assert "different capture" in result["error"]


def test_verified_uav_image_preserves_shader_stage(setup, tmp_path):
    prepared = json.loads(server.prepare_frame_render_flow())
    manifest, _ = _evidence(tmp_path / "evidence")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["artifact"]["attachment"].update(kind="uav", stage="compute")
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    result = json.loads(server.publish_frame_render_flow(prepared["flowId"],
        [stage(1, 12001, images=[{"manifestPath": str(manifest)}])], show_in_window=False, publication_scope='frame'))
    assert "error" not in result, result
    document = json.loads(Path(result["documentPath"]).read_text(encoding="utf-8"))
    image = document["stages"][0]["images"][0]
    assert image["attachmentStage"] == "compute"
    assert image["displayMapping"]["attachmentStage"] == "compute"


def test_publication_reports_corrected_navigation_and_quality_not_just_coverage(setup):
    prepared = json.loads(server.prepare_frame_render_flow())
    result = json.loads(server.publish_frame_render_flow(prepared["flowId"],
        [stage(1, 12001, representativeEventId=1)], show_in_window=False, publication_scope='frame'))
    assert result["coverage"]["complete"]
    assert not result["quality"]["analysisComplete"]
    assert result["navigationCorrections"] == [{"stageId": "stage-1", "requestedEventId": 1,
        "navigationEventId": 12001, "reason": "last-effective-draw-then-dispatch-or-utility"}]


def test_v2_annotations_inventory_and_comparisons_survive_host_restart(setup, monkeypatch, tmp_path):
    prepared = json.loads(server.prepare_frame_render_flow())
    manifest, _ = _evidence(tmp_path / "evidence", event=12001)
    auxiliary = {"resourceId": 2**48 + 1, "kind": "rtv", "index": 1,
                 "stage": "fragment", "mip": 1, "slice": 0, "sample": 0,
                 "resourceType": "texture", "captureSupported": True}
    server._frame_render_flow_store.record_observation(SESSION, 12001, {"expectedOutputs": [
        {"resourceId": 2**48, "kind": "rtv", "index": 0, "resourceType": "texture", "captureSupported": True}, auxiliary]})
    omission = {key: value for key, value in auxiliary.items() if key not in ("resourceType", "captureSupported")}
    omission.update(reason="The inherited auxiliary attachment is not written during lighting.",
                    evidence="RTV1 has ColorWriteMask=0 and the pixel shader writes only SV_Target0.")
    process = [{"label": "Lighting reference", "description": "Inspect the captured illumination.", "imageIndex": 1},
               {"label": "Compare lighting", "description": "Inspect both copies with the same mapping.", "comparisonIndex": 0}]
    algorithm = {
        "inputs": [],
        "steps": [{"eventId": 12001, "title": "Evaluate direct lighting",
                   "calculation": "Multiply the surface color by the clamped normal-light dot product.",
                   "evidence": "PS instructions 12-18 compute saturate(dot(N, L)) and write SV_Target0."}],
        "outputs": [{"eventId": 12001, "resourceId": 2**48, "role": "Directly shaded scene color"}],
        "integration": [{"sourceEventId": 12001, "resourceId": 2**48, "mode": "in-place", "eventId": 12001,
                         "explanation": "The draw writes the shaded visible surface directly into scene color.",
                         "evidence": "RTV0 is scene color; the pixel shader writes SV_Target0 with color writes enabled."}],
    }
    proposal = stage(1, 12001, title="Direct lighting", category="lighting", summary="Combine the surface normal and light direction to shade the visible surfaces.", investigation={"status": "investigated", "evidence": "Inspected shader calculation."},
        techniques=[{"name": "Direct light", "status": "recognized", "evidence": "Light vector and normal calculation."}],
        images=[{"manifestPath": str(manifest), "title": "Surface lighting", "label": "Lighting", "semantic": "Direct light",
                 "channelMeaning": "RGB contains direct surface illumination; A is not used by composition.",
                 "evidence": "The pixel shader writes illumination to xyz; composition samples only xyz.",
                 "group": "outputs", "description": "The shaded scene used by the next composition pass."},
                {"manifestPath": str(manifest), "title": "Lighting reference", "group": "inputs", "label": "Reference",
                 "channelMeaning": "RGB contains the same direct illumination used as the reference view.",
                 "evidence": "The reference reuses the exact captured illumination receipt and display mapping.",
                 "description": "A reference view for comparing the visible lighting."}],
        comparisons=[{"leftImageIndex": 0, "rightImageIndex": 1, "label": "Reference comparison", "relationship": "reference"}],
        processSteps=process, outputOmissions=[omission], algorithm=algorithm)
    saved = json.loads(server.publish_frame_render_flow(prepared["flowId"], [proposal], show_in_window=False, publication_scope='frame'))
    assert "error" not in saved, saved
    assert saved["quality"]["analysisComplete"]
    monkeypatch.setattr(server, "_frame_render_flow_store", FrameRenderFlowStore())
    restored = json.loads(server.get_frame_render_flow(prepared["flowId"]))
    assert "error" not in restored, restored
    assert restored["quality"]["analysisComplete"]
    assert restored["items"][0]["techniques"][0]["status"] == "recognized"
    assert restored["items"][0]["comparisons"][0]["visualComparable"]
    assert restored["items"][0]["processSteps"] == process
    assert restored["items"][0]["outputOmissions"] == [omission]
    assert restored["items"][0]["algorithm"] == algorithm
    coverage = restored["items"][0]["outputCoverage"]
    assert coverage["expectedResourceIds"] == [2**48, 2**48 + 1]
    assert coverage["representedResourceIds"] == [2**48]
    assert coverage["omittedAttachments"] == [omission]
    assert coverage["missingAttachments"] == coverage["missingResourceIds"] == []
    persisted = server._frame_render_flow_store.get(SESSION)
    assert persisted["document"]["observations"]["12001"]["expectedOutputs"][1] == auxiliary


def test_legacy_v1_diagram_is_upgraded_with_quality_findings(setup, monkeypatch):
    prepared = json.loads(server.prepare_frame_render_flow())
    saved = json.loads(server.publish_frame_render_flow(prepared["flowId"], [stage(1, 12001)], show_in_window=False, publication_scope='frame'))
    path = Path(saved["documentPath"])
    document = json.loads(path.read_text(encoding="utf-8"))
    document["schemaVersion"] = 1
    document.pop("quality")
    document.pop("observations")
    legacy_fields = {"id", "title", "summary", "confidence", "startEventId", "endEventId", "eventIds", "representativeEventId", "images"}
    document["stages"] = [{key: value for key, value in stage.items() if key in legacy_fields} for stage in document["stages"]]
    data = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    legacy = path.with_name(hashlib.sha256(data).hexdigest() + ".json")
    legacy.write_bytes(data)
    path.unlink()
    monkeypatch.setattr(server, "_frame_render_flow_store", FrameRenderFlowStore())
    restored = json.loads(server.get_frame_render_flow(prepared["flowId"]))
    assert "error" not in restored, restored
    assert restored["published"] and restored["coverage"]["complete"]
    assert not restored["quality"]["analysisComplete"]
    assert restored["items"][0]["category"] == "other"


def test_incomplete_binding_inventory_stays_incomplete_after_restart(setup, monkeypatch, tmp_path):
    prepared = json.loads(server.prepare_frame_render_flow())
    manifest, _ = _evidence(tmp_path / "evidence", event=12001)
    observation = _stage_inspection()
    observation.update(bindingsComplete=False, bindingErrors=[{
        "kind": "uav", "stage": "fragment", "error": "Descriptor enumeration failed"}])
    server._frame_render_flow_store.record_observation(SESSION, 12001, observation)
    proposal = stage(1, 12001, title="Direct lighting", summary="Shade visible surfaces using their normal and the light direction.", investigation={"status": "investigated", "evidence": "Inspected PS."},
        techniques=[{"name": "Direct light", "status": "recognized", "evidence": "PS diffuse lighting."}],
        images=[{"manifestPath": str(manifest), "title": "Surface lighting",
                 "channelMeaning": "RGB stores direct light; A is not read by the next composition shader.",
                 "evidence": "The producing PS writes diffuse illumination to xyz and composition samples xyz.",
                 "description": "Direct lighting color before final composition."}])
    saved = json.loads(server.publish_frame_render_flow(prepared["flowId"], [proposal], show_in_window=False, publication_scope='frame'))
    assert saved["saved"] and not saved["quality"]["analysisComplete"]
    monkeypatch.setattr(server, "_frame_render_flow_store", FrameRenderFlowStore())
    restored = json.loads(server.get_frame_render_flow(prepared["flowId"]))
    assert "error" not in restored, restored
    assert not restored["quality"]["analysisComplete"]
    assert restored["items"][0]["outputCoverage"]["inventoryComplete"] is False
    assert restored["items"][0]["qualityGaps"][0]["code"] == "stage-inventory-incomplete"


def test_portable_restore_is_adopted_without_reexporting_images(setup, monkeypatch, tmp_path):
    prepared = json.loads(server.prepare_frame_render_flow())
    manifest, _ = _evidence(tmp_path / "evidence", event=12001)
    saved = json.loads(server.publish_frame_render_flow(prepared["flowId"],
        [stage(1, 12001, images=[{"manifestPath": str(manifest), "description": "Scene color before tone mapping.",
                                "presentation": "detail"}])],
        show_in_window=False, publication_scope='frame'))
    document = json.loads(Path(saved["documentPath"]).read_text(encoding="utf-8"))
    data = server._frame_render_flow_store.get(SESSION)["outline"]
    new_session = "b" * 32
    data["captureSessionId"] = new_session
    record = FrameRenderFlowStore().prepare(data)
    document.update(captureSessionId=new_session, flowId=record["flowId"], snapshotId=record["snapshotId"])
    # Real bridge restoration returns absolute bundle media paths with their
    # verified original manifest identifiers; the old request may be gone.
    old_preview = Path(document["stages"][0]["images"][0]["path"])
    portable_preview = tmp_path / "portable-preview.png"
    portable_preview.write_bytes(old_preview.read_bytes())
    document["stages"][0]["images"][0]["path"] = str(portable_preview)
    old_preview.unlink()
    manifest.unlink()
    monkeypatch.setattr(server, "_frame_render_flow_store", FrameRenderFlowStore())
    monkeypatch.setattr(server, "_active_live_context", lambda **kwargs: {
        "capture": {"captureSessionId": new_session, "captureId": "", "path": data["capturePath"], "api": "D3D12"}})
    calls = []
    def bridge(method, params, **kwargs):
        calls.append(method)
        if method == "get_saved_frame_render_flow":
            return {"document": document, "outline": data}
        if method == "save_frame_render_flow":
            return {"saved": True, "portable": True}
        raise AssertionError("Unexpected capture analysis during restoration: " + method)
    monkeypatch.setattr(server.ipc, "call", bridge)
    restored = json.loads(server.get_frame_render_flow())
    assert restored["published"] and restored["flowId"] == record["flowId"]
    assert restored["items"][0]["images"][0]["presentation"] == "detail"
    republished = json.loads(server.publish_frame_render_flow(record["flowId"],
        [stage(1, 12001, images=[{"manifestPath": str(manifest), "description": "Reused captured scene color."}])],
        show_in_window=False, publication_scope='frame'))
    assert republished["savedWithCapture"] and republished["imageCount"] == 1
    assert calls == ["get_saved_frame_render_flow", "save_frame_render_flow"]


def test_corrupt_same_session_media_cannot_restore_from_a_valid_document_hash(setup, monkeypatch, tmp_path):
    prepared = json.loads(server.prepare_frame_render_flow())
    manifest, _ = _evidence(tmp_path / "evidence", event=12001)
    saved = json.loads(server.publish_frame_render_flow(prepared["flowId"],
        [stage(1, 12001, images=[{"manifestPath": str(manifest)}])], show_in_window=False, publication_scope='frame'))
    document = json.loads(Path(saved["documentPath"]).read_text(encoding="utf-8"))
    Path(document["stages"][0]["images"][0]["path"]).write_bytes(b"changed preview")
    monkeypatch.setattr(server, "_frame_render_flow_store", FrameRenderFlowStore())
    monkeypatch.setattr(server.ipc, "call", lambda *args, **kwargs: {"restored": False})
    result = json.loads(server.get_frame_render_flow(prepared["flowId"]))
    assert "error" in result and not result.get("published")
