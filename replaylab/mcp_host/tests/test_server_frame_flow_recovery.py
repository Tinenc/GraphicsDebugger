"""Host recovery paths with real capture guards and texture evidence admission."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import struct
import zlib

import pytest

from src import server
from src.frame_render_flow import FrameRenderFlowStore
from test_frame_render_flow import outline, _reviewed_stage
from test_server_event_texture_evidence import EvidenceIPC, SESSION, _context


def _png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\0" + bytes((30, 80, 150, 255)) * 64 for _ in range(32))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 32, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class RecoveryIPC(EvidenceIPC):
    def __init__(self):
        super().__init__()
        self.session, self.current = SESSION, 7
        self.api_override = {}
        self.api_switch_session = False
        self.batch_override = {}
        self.corrupt_channel = None
        self.alpha_failures = 0
        self.revision = [2, 3, []]
        self.repair_revision = None
        self.batch_calls = []
        self.saved = None
        self.data = outline(3)
        for action in self.data["actions"]:
            action["outputs"] = [101]

    def call(self, method, params=None, timeout=None):
        payload = dict(params or {})
        if method == "get_current_renderdoc_context":
            self.calls.append((method, payload, timeout))
            result = _context(self.session)
            result["events"] = {"currentEventId": self.current, "selectedEventId": self.current}
            if payload.get("identity_only"):
                result.update(identityOnly=True, events={"currentEventId": 0, "selectedEventId": 0})
            return result
        if method == "capture_event_texture_evidence":
            receipt = super().call(method, payload, timeout)
            output = receipt["artifact"]["output"]
            data = _png()
            Path(output["path"]).write_bytes(data)
            output.update(byteSize=len(data), sha256=hashlib.sha256(data).hexdigest())
            receipt["artifact"]["attachment"]["componentCount"] = 4
            return receipt
        self.calls.append((method, payload, timeout))
        if method == "inspect_event_api_parameters":
            result = {"schema": "bread-event-api-parameters/v1", "eventId": payload["event_id"],
                      "captureSessionId": self.session, "chunks": [], "indirectArgumentsResolved": False,
                      **self.api_override}
            if self.api_switch_session:
                self.session = "b" * 32
            return result
        if method == "get_frame_render_flow_outline":
            return dict(self.data, offset=0, hasMore=False, nextOffset=None)
        if method == "inspect_frame_render_stage":
            return {"schema": "bread-frame-stage-evidence/v1", "inventoryVersion": 3, "eventId": payload["event_id"],
                    "captureSessionId": self.session, "bindingsComplete": True,
                    "expectedOutputs": [{"resourceId": 101, "kind": "rtv", "index": 0,
                                         "resourceType": "texture", "captureSupported": True}],
                    "textureInputs": [], "inputs": [], "shaders": [{"resourceId": 8, "stage": "pixel"}],
                    "details": {"pipelineType": "graphics"}}
        if method == "save_frame_render_flow":
            self.saved = copy.deepcopy(payload)
            return {"saved": True, "portable": True}
        if method == "capture_frame_flow_channels":
            self.batch_calls.append(copy.deepcopy(payload))
            rows = []
            for item in payload["items"]:
                parent = item["preview"]
                plan = parent.get("channelGroups", [{"channel": "rgb"}, {"channel": "a"}])
                count = parent.get("componentCount", 4)
                row = {"key": item["key"], "componentCount": count, "channelGroups": plan,
                       "receipts": [], "errors": []}
                for request in item["requests"]:
                    channel = request["channel"]
                    if channel not in {part["channel"] for part in plan}:
                        continue
                    if channel == "a" and self.alpha_failures:
                        self.alpha_failures -= 1
                        row["errors"].append({"channel": "a", "error": "Transient readback failure"})
                        continue
                    mapping = parent["displayMapping"]
                    receipt = self.call("capture_event_texture_evidence", {
                        **request, "event_id": parent["eventId"], "attachment_kind": parent["attachmentKind"],
                        "attachment_index": parent["attachmentIndex"], "stage": parent["attachmentStage"],
                        "channel": "rgba" if channel == "rgb" else channel,
                        "mip": mapping["mip"], "slice": mapping["slice"], "sample": mapping["sample"],
                        "range_mode": "native", "expected_capture_session_id": self.session})
                    if self.corrupt_channel and channel == "a":
                        self.corrupt_channel(receipt)
                    row["receipts"].append({"channel": channel, "rangeMode": "native", "receipt": receipt})
                rows.append(row)
            revision = self.repair_revision if len(self.batch_calls) > 1 and self.repair_revision else self.revision
            return {"schema": "bread-frame-flow-channel-capture/v1", "captureSessionId": self.session,
                    "originalEventId": 7, "replayRevision": revision, "items": rows, **self.batch_override}
        raise AssertionError("Unexpected operation: " + method)


@pytest.fixture
def machine(monkeypatch, tmp_path):
    fake = RecoveryIPC()
    monkeypatch.setattr(server, "ipc", fake)
    monkeypatch.setattr(server, "_frame_render_flow_store", FrameRenderFlowStore())
    monkeypatch.setattr(server, "_EVENT_TEXTURE_EVIDENCE_ROOT", tmp_path / "evidence")
    monkeypatch.setenv("RENDERDOC_ULTRA_EVENT_ARTIFACTS_HOME", str(tmp_path / "diagrams"))
    return fake


def _json(value):
    result = json.loads(value)
    assert "error" not in result, result
    return result


@pytest.mark.parametrize("arguments", [
    {"event_id": True}, {"event_id": 0}, {"event_id": 2**32}, {"event_id": "7"},
    {"max_depth": True}, {"max_depth": 0}, {"max_depth": 13},
    {"max_nodes": True}, {"max_nodes": 15}, {"max_nodes": 4097},
])
def test_api_parameter_validation_rejects_before_any_ipc(machine, arguments):
    response = json.loads(server.inspect_event_api_parameters(**{"event_id": 7, **arguments}))
    assert "error" in response
    assert not machine.calls


def test_api_parameters_are_bounded_exact_session_and_read_only(machine):
    result = _json(server.inspect_event_api_parameters(7, max_depth=12, max_nodes=4096))
    assert result["eventId"] == 7 and result["indirectArgumentsResolved"] is False
    assert [call[0] for call in machine.calls] == ["get_current_renderdoc_context",
             "inspect_event_api_parameters", "get_current_renderdoc_context"]
    params = machine.calls[1][1]
    assert params == {"event_id": 7, "max_depth": 12, "max_nodes": 4096,
                      "expected_capture_session_id": SESSION, "__replaylab_expected_capture_session_id": SESSION}


@pytest.mark.parametrize("override", [{"eventId": 4}, {"captureSessionId": "b" * 32}, {"schema": "other"}])
def test_api_parameters_reject_foreign_receipt(machine, override):
    machine.api_override = override
    result = json.loads(server.inspect_event_api_parameters(7))
    assert "mismatched capture/event evidence" in result["error"]


def test_api_parameters_reject_capture_change_after_successful_read(machine):
    machine.api_switch_session = True
    result = json.loads(server.inspect_event_api_parameters(7))
    assert "capture/session changed" in result["error"]


def test_api_parameters_reject_boolean_eid_in_receipt(machine):
    machine.api_override = {"eventId": True}
    result = json.loads(server.inspect_event_api_parameters(1))
    assert "error" in result


def _main_manifest():
    response = server.capture_event_texture_evidence(7, "rtv", 0, channel="rgba", range_mode="native", inline=False)
    return _json(response[0])["manifest"]["path"]


def test_reanalysis_proposals_roundtrip_and_quality_require_this_review(machine):
    flow = _json(server.prepare_frame_render_flow())
    flow_id = flow["flowId"]
    _json(server.inspect_frame_render_stage(flow_id, 7))
    proposal = _reviewed_stage(1, 7, images=[{"manifestPath": _main_manifest(), "channelGroups": []}],
                              processGraph={"nodes": [{"id": "shade", "label": "Shade surface", "eventId": 7},
                                                      {"id": "result", "label": "Scene color", "imageIndex": 0}],
                                            "edges": [{"from": "shade", "to": "result"}]})
    proposal["algorithm"]["outputs"][0]["resourceId"] = 101
    proposal["algorithm"]["integration"][0]["resourceId"] = 101
    initial = _json(server.publish_frame_render_flow(flow_id, [proposal], show_in_window=False, publication_scope='frame'))
    assert initial["quality"]["analysisComplete"], initial["quality"]
    raw = _json(server.get_frame_render_flow(flow_id, section="stages"))["items"][0]
    assert "navigationEventId" in raw and "path" in raw["images"][0]

    started = _json(server.prepare_frame_render_flow(reanalyze=True))
    assert started["cacheHit"] and not started["reanalysis"]["reviewComplete"]
    # A previous publication remains a comparison baseline, never an implicit
    # editable draft or proof of the newly requested analysis.
    assert _json(server.get_frame_render_flow(flow_id,section="proposals"))["items"] == []
    baseline = _json(server.get_frame_render_flow(flow_id,section="baseline"))["items"]
    assert baseline[0]["processGraph"] == raw["processGraph"]
    inspected = _json(server.inspect_frame_render_stage(flow_id,7))
    assert inspected
    proposal["investigation"]["evidence"] = (
        "Rechecked current Shader calculations and bindings: the surface result is blended in place; retain the module boundary.")
    final = _json(server.publish_frame_render_flow(flow_id,[proposal],show_in_window=False, publication_scope='frame'))
    assert final["savedWithCapture"] and final["quality"]["analysisComplete"], final
    assert final["reanalysis"]["semanticCorrectnessProven"] is False
    assert not _json(server.get_frame_render_flow(flow_id,section="quality"))["items"]


def _document(count=1):
    parent = {"eventId": 7, "resourceId": 101, "group": "outputs", "channel": "rgba",
              "attachmentKind": "rtv", "attachmentIndex": 0, "attachmentStage": "fragment",
              "title": "Surface color", "description": "Color before lighting composition.",
              "channelMeaning": "RGB: surface color; A: coverage", "evidence": "PS output components.",
              "componentCount": 4, "channelGroups": [{"channel": "rgb", "label": "Surface color"},
                                                     {"channel": "a", "label": "Coverage"}],
              "displayMapping": {"mip": 1, "slice": 2, "sample": 1}}
    return {"captureSessionId": SESSION, "capturePath": "C:/captures/frame.rdc",
            "stages": [{"id": "stage-%d" % i, "images": [copy.deepcopy(parent)]} for i in range(count)]}


def _assert_managed_images(document):
    for stage in document["stages"]:
        for image in stage["images"][1:]:
            manifest = json.loads(Path(image["manifestPath"]).read_text(encoding="utf-8"))
            data = Path(image["path"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == image["sha256"]
            assert manifest["capture"]["captureSessionId"] == SESSION
            assert manifest["restoration"] == {"originalEventId": 7, "restored": True}
            assert (image["eventId"], image["resourceId"], image["width"], image["height"]) == (7, 101, 64, 32)
            assert [image["displayMapping"][k] for k in ("mip", "slice", "sample")] == [1, 2, 1]


def test_precache_admits_real_manifests_and_preserves_semantics(machine):
    document = _document()
    result = server._precache_frame_flow_channels(document, _context())
    assert result == {"attempted": 1, "prepared": 1, "failed": 0, "errors": [], "complete": True,
        "volumes":{"attempted":0,"prepared":0,"failed":0,"errors":[],"complete":True}}
    images = document["stages"][0]["images"]
    assert [image["channel"] for image in images] == ["rgba", "rgba", "a"]
    assert [image["channelMeaning"] for image in images[1:]] == ["Surface color", "Coverage"]
    assert all(image["parentImageIndex"] == 0 and image["group"] == "channels" for image in images[1:])
    assert images[0]["channelCacheSession"] == SESSION and images[0]["channelCacheRevision"] == machine.revision
    _assert_managed_images(document)


def test_precache_evidence_event_is_distinct_from_restored_user_selection(machine):
    document = _document()
    document["stages"][0]["images"][0]["eventId"] = 4
    result = server._precache_frame_flow_channels(document, _context())
    assert result["complete"], result
    for image in document["stages"][0]["images"][1:]:
        manifest = json.loads(Path(image["manifestPath"]).read_text(encoding="utf-8"))
        assert manifest["eventId"] == image["eventId"] == 4
        assert manifest["restoration"] == {"originalEventId": 7, "restored": True}


def test_publish_persists_precached_channels_and_proposals_republish_without_duplicates(machine):
    flow_id = _json(server.prepare_frame_render_flow())["flowId"]
    _json(server.inspect_frame_render_stage(flow_id, 7))
    proposal = _reviewed_stage(1, 7, images=[{"manifestPath": _main_manifest(),
        "channelGroups": [{"channel": "rgb", "description": "Lit surface color"},
                          {"channel": "a", "description": "Surface coverage"}]}])
    proposal["algorithm"]["outputs"][0]["resourceId"] = 101
    proposal["algorithm"]["integration"][0]["resourceId"] = 101
    published = _json(server.publish_frame_render_flow(flow_id, [proposal], show_in_window=False, publication_scope='frame'))
    assert published["savedWithCapture"] and published["channelCache"]["complete"]
    saved = machine.saved["document"]["stages"][0]["images"]
    assert len(saved) == 3 and saved[0]["channelCacheComplete"]
    assert all(Path(image["manifestPath"]).is_file() for image in saved)
    proposals = _json(server.get_frame_render_flow(flow_id, section="proposals"))["items"]
    assert len(proposals[0]["images"]) == 3
    assert all("channelCacheComplete" not in image and "displayMapping" not in image
               for image in proposals[0]["images"])
    repeated = _json(server.publish_frame_render_flow(flow_id, proposals, show_in_window=False, publication_scope='frame'))
    assert repeated["channelCache"]["complete"] and repeated["imageCount"] == 3
    assert len(machine.saved["document"]["stages"][0]["images"]) == 3


def test_precache_batches_parents_without_unbounded_replay(machine):
    document = _document(9)
    result = server._precache_frame_flow_channels(document, _context())
    assert result["complete"] and result["prepared"] == 9
    assert [len(call["items"]) for call in machine.batch_calls] == [8, 1]
    _assert_managed_images(document)


@pytest.mark.parametrize("corruption", ["hash", "session", "resource", "stage", "mip", "restoration"])
def test_precache_rejects_corrupt_channel_without_losing_valid_rgb(machine, corruption):
    def corrupt(receipt):
        if corruption == "hash":
            receipt["artifact"]["output"]["sha256"] = "0" * 64
        elif corruption == "session":
            receipt["captureSessionId"] = "b" * 32
        elif corruption == "restoration":
            receipt["restoration"]["restored"] = False
        else:
            receipt["artifact"]["attachment"][{"resource": "resourceId", "stage": "stage", "mip": "mip"}[corruption]] = {
                "resource": 102, "stage": "compute", "mip": 2}[corruption]
    machine.corrupt_channel = corrupt
    document = _document()
    result = server._precache_frame_flow_channels(document, _context())
    assert not result["complete"] and result["failed"] == 1
    assert result["errors"][0]["missingChannels"] == ["a"]
    assert len(document["stages"][0]["images"]) == 2
    assert not document["stages"][0]["images"][0]["channelCacheComplete"]
    _assert_managed_images(document)


@pytest.mark.parametrize("override", [{"captureSessionId": "b" * 32}, {"originalEventId": 4}])
def test_precache_rejects_whole_batch_with_wrong_capture_or_original_event(machine, override):
    machine.batch_override = override
    document = _document()
    result = server._precache_frame_flow_channels(document, _context())
    assert not result["complete"] and result["failed"] == 1
    assert len(document["stages"][0]["images"]) == 1


def test_precache_retries_only_failed_alpha_and_keeps_successful_rgb(machine):
    machine.alpha_failures = 1
    document = _document()
    result = server._precache_frame_flow_channels(document, _context())
    assert result["complete"], result
    assert len(machine.batch_calls) == 2
    assert [item["channel"] for item in machine.batch_calls[1]["items"][0]["requests"]] == ["a"]
    assert len(document["stages"][0]["images"]) == 3
    _assert_managed_images(document)


def test_precache_refuses_to_merge_repair_from_new_replay_revision(machine):
    machine.alpha_failures = 1
    machine.repair_revision = [3, 3, []]
    document = _document()
    result = server._precache_frame_flow_channels(document, _context())
    assert not result["complete"]
    assert "different capture, revision" in str(result["errors"])
    assert len(document["stages"][0]["images"]) == 2


def test_precache_exhausted_retry_retains_existing_channel_but_marks_incomplete(machine):
    machine.alpha_failures = 2
    document = _document()
    old_alpha = {"group": "channels", "parentImageIndex": 0, "channel": "a",
                 "title": "Coverage", "description": "Previously inspected surface coverage",
                 "manifestPath": "existing-alpha-manifest"}
    document["stages"][0]["images"].append(copy.deepcopy(old_alpha))
    result = server._precache_frame_flow_channels(document, _context())
    assert not result["complete"] and len(machine.batch_calls) == 2
    assert result["errors"][0]["missingChannels"] == ["a"]
    assert document["stages"][0]["images"][1] == old_alpha
    assert not document["stages"][0]["images"][0]["channelCacheComplete"]


@pytest.mark.parametrize("parent", [{"componentCount": 1}, {"channelGroups": []}, {"attachmentKind": "dsv"}])
def test_no_split_previews_never_request_channels(machine, parent):
    document = _document()
    document["stages"][0]["images"][0].update(parent)
    result = server._precache_frame_flow_channels(document, _context())
    assert result["complete"] and result["attempted"] == 0
    assert not machine.batch_calls
