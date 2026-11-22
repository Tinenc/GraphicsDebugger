"""Regression cases for reusable observations and non-blocking transfer tools."""
from array import array
import base64
import hashlib
import os
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

import pytest

from src import file_content_cache, project_restore
from src.shader_workspace.canonical_source import select_source
from src.unity_layered_validation import compare_layers


def test_unchanged_capture_hash_is_reused_but_same_size_restored_mtime_is_not(tmp_path, monkeypatch):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"first")
    original = project_restore.hashlib.sha256
    monkeypatch.setenv("BREAD_FILE_DIGEST_CACHE_ROOT",str(tmp_path/"digest-cache"))
    before_reads = file_content_cache.statistics()["hashReads"]
    first = project_restore._verified_rdc_content_sha256(capture)
    assert project_restore._verified_rdc_content_sha256(capture) == first
    assert file_content_cache.statistics()["hashReads"]-before_reads == (1 if file_content_cache.file_stamp(capture) is not None else 2)
    before = capture.stat()
    capture.write_bytes(b"other")
    os.utime(capture, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert project_restore._verified_rdc_content_sha256(capture) == original(b"other").hexdigest()
    assert first != original(b"other").hexdigest()


def test_no_reliable_change_identity_disables_cache(tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"source")
    file_content_cache.put(capture, None, "unsafe")
    assert file_content_cache.get(capture, None) is None


def source_row(name, sha, active=False, when=""):
    return dict(workspaceId=name, workspacePath="/sources/" + name,
                sourceSha256=sha, sourceFormat="hlsl", localChanges=True,
                activeHead=active, registeredAt=when)


def test_current_project_source_retains_other_drafts_and_latest_wins_over_old_tab():
    older, current = source_row("draft", "a"), source_row("head", "b", True, "2026-10-31")
    result = select_source([older, current])
    assert result["workspaceId"] == "head"
    assert result["retainedSourceConflicts"][0]["workspaceId"] == "draft"
    assert select_source([older, current], ui={"selectedWorkspaceId": "draft"})["workspaceId"] == "head"
    assert select_source([older, source_row("another", "c")])["workspaceId"] == "another"


def floats(tmp_path, name, values):
    path = tmp_path / name
    path.write_bytes(struct.pack("<" + "f" * len(values), *values))
    return dict(path=str(path), encoding="float32le", width=len(values), height=1, channels=1)


def test_layer_comparison_preserves_hdr_errors_and_shared_pass_input(tmp_path):
    reference = floats(tmp_path, "reference.bin", [2, 4, 6])
    target = floats(tmp_path, "target.bin", [1, 3, 5])
    fixture = tmp_path / "input.rt"
    fixture.write_bytes(b"same input before this pass")
    result = compare_layers([dict(name="bloom", category="post-process", reference=reference, rendered=target,
                                 inputFixture=dict(referencePath=str(fixture), renderedPath=str(fixture)))])
    layer = result["layers"][0]
    assert layer["metrics"]["meanAbsoluteError"] == 1
    assert layer["identicalInputFixture"] is True
    assert result["diagnosticsOnly"] is True and result["acceptanceBy"] == "agent"
    assert result["automaticImageAdjustment"] is False


def test_engine_comparison_exposure_is_advice_and_invalid_values_remain_visible(tmp_path):
    result = compare_layers([dict(category="native-engine", reference=floats(tmp_path, "a", [2, float("nan")]),
                                 rendered=floats(tmp_path, "b", [1, 0]))])
    assert result["layers"][0]["metrics"]["suggestedExposureDeltaEV"] == 1
    assert result["layers"][0]["metrics"]["nonFiniteComponents"] == 1
    with pytest.raises(ValueError, match="same explicit RT"):
        compare_layers([dict(reference=floats(tmp_path, "c", [1]), rendered=floats(tmp_path, "d", [1, 2]))])


@pytest.mark.parametrize("fails", [False, True])
def test_event_buffer_read_restores_visible_replay_even_on_gpu_failure(monkeypatch, fails):
    from test_bridge_composite_readonly import load_bridge
    bridge = load_bridge()
    changes = []
    bridge._capture_session_id = "capture"
    bridge._request_visible_event_id = lambda default: 2111
    bridge._resolve_resource_id = lambda ctrl, resource, kind: resource
    bridge._set_frame_event = lambda ctrl, event, force: changes.append(event)
    bridge._find_action_by_event = lambda actions, event: SimpleNamespace(eventId=event)
    def read(*args):
        if fails:
            raise RuntimeError("GPU read failed")
        return struct.pack("<IIIiI", 1536, 0, 0, -4, 0)
    ctrl = SimpleNamespace(GetRootActions=lambda: [], GetBufferData=read)
    bridge._replay = lambda callback: callback(ctrl)
    params = dict(resource_id=50610, event_id=1064, before_action=True,
                  expected_capture_session_id="capture", offset=0, length=20)
    if fails:
        with pytest.raises(RuntimeError, match="GPU read failed"):
            bridge._get_buffer_data(params)
    else:
        result = bridge._get_buffer_data(params)
        assert result["eventId"] == 1064 and len(base64.b64decode(result["base64"])) == 20
    assert changes == [1063, 2111]


def test_vulkan_indirect_api_parameters_decode_exact_event_bound_bytes(monkeypatch):
    from test_bridge_composite_readonly import load_bridge
    import test_frame_stage_evidence as stage
    bridge = load_bridge()
    monkeypatch.setitem(sys.modules, bridge.__name__, bridge)
    monkeypatch.setitem(sys.modules, bridge.__name__ + ".frame_stage_evidence", stage.evidence)
    chunk = stage.Structured("vkCmdDrawIndexedIndirect", "Chunk", children=[
        stage.Structured("Buffer", "Resource", 50610), stage.Structured("Offset", "UnsignedInteger", 0),
        stage.Structured("DrawCount", "UnsignedInteger", 1)])
    action = SimpleNamespace(eventId=1064, flags=0x080002, parent=None, children=[],
                             events=[SimpleNamespace(eventId=1064, chunkIndex=0)])
    bridge._ctx = SimpleNamespace(GetAction=lambda eid: action,
                                 GetStructuredFile=lambda: SimpleNamespace(chunks=[chunk]))
    bridge._capture_session_id = "capture"
    bridge._request_capture_loaded = lambda: True
    bridge._invoke_on_ui_sync = lambda callback: callback()
    calls = []
    def buffer(params):
        calls.append(params)
        return dict(base64=base64.b64encode(struct.pack("<IIIiI", 1536, 0, 0, -4, 0)).decode())
    bridge._get_buffer_data = buffer
    result = bridge._inspect_event_api_parameters(dict(event_id=1064, expected_capture_session_id="capture"))
    decoded = result["indirect"]["decoded"]
    assert decoded["source"] == "event-bound-indirect-arguments"
    assert decoded["arguments"]["vertexOffset"] == -4
    assert decoded["nonZeroWorkload"] is False
    assert calls[0]["event_id"] == 1064 and calls[0]["before_action"] is True


def test_frame_algorithm_accepts_observed_zero_work_without_calling_it_a_contribution():
    from test_frame_flow_algorithm import case, build
    record, observations, proposal = case()
    action = next(row for row in record["outline"]["actions"] if row["eventId"] == 7)
    action.update(hasWork=None, flags=action["flags"] | 0x080000, outputs=[200])
    observations["7"] = {"indirectWorkload": dict(status="resolved", source="event-bound-indirect-arguments",
        eventId=7, nonZeroWorkload=False, workload=dict(indexCount=1536, instanceCount=0),
        bufferEvidence=dict(resourceId=50610, capturePhase="before-action", replayEventId=6, restoredEventId=2111))}
    doc = build(record, observations, proposal)
    assert not doc["stages"][0]["qualityGaps"]
    assert doc["stages"][0]["navigationEventId"] == 4
    assert doc["observations"]["7"]["indirectWorkload"]["bufferEvidence"]["capturePhase"] == "before-action"
