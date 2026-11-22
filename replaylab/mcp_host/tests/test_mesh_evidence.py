from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace as NS

from PIL import Image
import pytest

from src import server
from src.mesh_evidence_image import footprint, wireframe
from test_bridge_composite_readonly import load_bridge


BRIDGE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge"
spec = importlib.util.spec_from_file_location("mesh_read_contract", BRIDGE / "mesh_evidence.py")
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


def mesh(**changes):
    value = dict(numIndices=3, topology="Topology.TriangleList", vertexByteStride=16,
                 indexByteStride=0, vertexResourceId=1, indexResourceId=0,
                 vertexByteOffset=0, indexByteOffset=0, baseVertex=0, unproject=True,
                 flipY=False, allowRestart=True, restartIndex=0xffff, status="",
                 format=NS(compCount=4, compByteWidth=4, compType="Float", type="Regular"))
    value.update(changes)
    return NS(**value)


class Controller:
    def __init__(self, positions=None, indices=b"", geometry=None):
        positions = positions or [[-0.5, -0.5, 0, 1], [0.5, -0.5, 0, 1], [0, 0.5, 0, 1]]
        self.buffers = {1: b"".join(struct.pack("<4f", *p) for p in positions), 2: indices}
        self.reads = []
        self.geometry = geometry or mesh()
        self.event = 7
        self.post_calls = []
        self.action = NS(eventId=30, flags=2, numInstances=1, children=[])

    def GetBufferData(self, resource, offset, count):
        self.reads.append((resource, offset, count))
        assert count <= 8 * 1024 * 1024
        return self.buffers[resource][offset:offset + count]

    def GetRootActions(self):
        return [self.action]

    def GetPostVSData(self, instance, view, stage):
        self.post_calls.append((self.event, instance, view, stage))
        return self.geometry

    def GetPipelineState(self):
        return NS(MultiviewBroadcastCount=lambda: 1,
                  GetShader=lambda stage: 0,
                  GetViewport=lambda index: NS(x=0, y=0, width=1000, height=1000))


def test_indexed_offsets_base_vertex_and_restart_are_not_contiguous_vertex_guesses():
    ctrl = Controller(positions=[[0, 0, 0, 1], [1, 0, 0, 1], [0, 1, 0, 1], [9, 9, 0, 1]],
                      indices=b"padding!" + struct.pack("<4H", 6, 5, 7, 0xffff))
    value = reader.read_mesh(ctrl, mesh(numIndices=4, indexResourceId=2, indexByteStride=2,
                                       indexByteOffset=8, baseVertex=-5))
    assert value["complete"]
    assert value["indices"] == [1, 0, 2, None]
    assert value["positions"] == [[0, 0, 0, 1], [1, 0, 0, 1], [0, 1, 0, 1]]
    assert ctrl.reads == [(2, 8, 8), (1, 0, 48)]


def test_sparse_indices_and_truncated_reads_break_connectivity_and_remain_partial():
    ctrl = Controller(indices=struct.pack("<3I", 0, 9999999, 2))
    value = reader.read_mesh(ctrl, mesh(indexResourceId=2, indexByteStride=4), max_vertices=2)
    assert value["indices"] == [0, None, None]
    assert "vertex-window-only" in value["limitations"]
    assert not value["complete"]
    assert ctrl.reads[-1] == (1, 0, 32)
    assert footprint(dict(value, allInstances=True)) is None


def test_unknown_format_is_not_reinterpreted_as_float_and_short_reads_are_explicit():
    ctrl = Controller()
    value = reader.read_mesh(ctrl, mesh(format=NS(compCount=4, compByteWidth=4,
                                                  compType="UInt", type="Regular")))
    assert not value["positions"] and not ctrl.reads
    assert "unsupported-position-format-or-stride" in value["limitations"]
    value = reader.read_mesh(ctrl, mesh(numIndices=6))
    assert len(value["positions"]) == 3 and value["indices"][-1] is None
    assert "short-vertex-read" in value["limitations"]


def test_non_finite_positions_never_escape_as_invalid_json():
    ctrl = Controller(positions=[[float("nan"), 0, 0, 1], [0, 0, 0, 1]])
    value = reader.read_mesh(ctrl, mesh(numIndices=2))
    assert value["positions"][0] is None
    json.dumps(value, allow_nan=False)
    assert not value["complete"]


def test_native_padding_and_vertex_offset_are_respected():
    ctrl = Controller()
    ctrl.buffers[1] = b"HEAD" + struct.pack("<4f", 1, 2, 3, 4) + b"pad!" + struct.pack("<4f", 5, 6, 7, 8)
    value = reader.read_mesh(ctrl, mesh(numIndices=2, vertexByteOffset=4, vertexByteStride=20))
    assert value["complete"]
    assert value["positions"] == [[1, 2, 3, 4], [5, 6, 7, 8]]


def test_wireframe_returns_actual_opaque_picture_and_never_claims_visibility():
    value = reader.read_mesh(Controller(), mesh())
    png, illustration = wireframe(value, 128)
    image = Image.open(io.BytesIO(png))
    assert image.mode == "RGB" and image.size == (128, 128)
    assert len(image.getcolors()) >= 3
    assert illustration["drawnSegments"] == 3
    assert not illustration["depthTested"] and not illustration["culled"]


def test_strip_restart_is_a_break_and_unprojected_data_is_not_a_screen_shape():
    value = {"coordinateSpace": "clip", "topology": "TriangleStrip",
             "positions": [[-0.5, -0.5, 0, 1], [0.5, -0.5, 0, 1], [0, 0.5, 0, 1]],
             "indices": [0, 1, 2, None, 0, 1]}
    _, illustration = wireframe(value, 128)
    assert illustration["primitiveCount"] == 1
    value["coordinateSpace"] = "shader-output"
    with pytest.raises(ValueError, match="clip-space"):
        wireframe(value)


def test_wireframe_preserves_the_captured_viewport_aspect_ratio():
    value = dict(reader.read_mesh(Controller(), mesh()), viewport={"width": 1600, "height": 900})
    png, illustration = wireframe(value, 640)
    assert Image.open(io.BytesIO(png)).size == (640, 360)
    assert illustration["width"] == 640 and illustration["height"] == 360


def test_projection_handles_negative_w_huge_coordinates_and_does_not_crop_instances():
    value = {"coordinateSpace": "clip", "topology": "LineList", "complete": True,
             "allInstances": True, "finalPositionStage": True,
             "positions": [[-1e9, 0, 0, 1], [1e9, 0, 0, 1],
                                                  [0, 0, 0, -1], [1, 0, 0, 1]],
             "indices": [0, 1, 2, 3]}
    png, illustration = wireframe(value, 128)
    assert png and illustration["drawnSegments"] == 1 and illustration["skippedPrimitives"] == 1
    assert footprint(value) is None
    value["indices"] = [0, 1]
    value["allInstances"] = False
    assert footprint(value) is None


@pytest.fixture
def integrated(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setitem(sys.modules, bridge.__name__, bridge)
    monkeypatch.setitem(sys.modules, bridge.__name__ + ".mesh_evidence", reader)
    ctrl = Controller()
    bridge.rd.MeshDataStage = NS(VSOut="vs", GSOut="gs")
    bridge.rd.ShaderStage = NS(Hull="hs", Domain="ds", Geometry="gs", Mesh="mesh")
    bridge._request_visible_event_id = lambda: 7
    bridge._request_capture_session_id = lambda: "capture-session"
    bridge._require_healthy_replay = lambda unused: None
    bridge._set_frame_event = lambda ctrl, eid, force, **kw: setattr(ctrl, "event", eid)
    bridge._replay = lambda fn: fn(ctrl)
    return bridge, ctrl


def test_exact_eid_transaction_returns_positions_and_restores_visible_eid(integrated):
    bridge, ctrl = integrated
    result = bridge._get_post_vs_data({"event_id": 30})
    assert ctrl.post_calls == [(30, 0, 0, "vs")]
    assert ctrl.event == 7
    assert result["eventId"] == 30 and result["captureSessionId"] == "capture-session"
    assert result["positions"] and result["allInstances"]
    assert result["finalPositionStage"]
    assert result["viewport"]["width"] == 1000


def test_eid_restored_on_buffer_failure_and_invalid_instance_or_view(integrated):
    bridge, ctrl = integrated
    ctrl.buffers = {}
    with pytest.raises(KeyError):
        bridge._get_post_vs_data({"event_id": 30})
    assert ctrl.event == 7
    for params in ({"instance": 2}, {"view": 5}):
        with pytest.raises(ValueError):
            bridge._get_post_vs_data(dict(params, event_id=30))
        assert ctrl.event == 7


def test_legacy_current_eid_and_selected_instance_are_explicit(integrated):
    bridge, ctrl = integrated
    ctrl.action.eventId = 7
    ctrl.action.numInstances = 4
    ctrl.action.flags |= 0x020000  # Native Instanced flag for the four-instance draw.
    result = bridge._get_post_vs_data({"instance": 2, "stage": "gs-out"})
    assert ctrl.post_calls == [(7, 2, 0, "gs")]
    assert result["complete"] and not result["allInstances"]
    assert "selected-instance-only" in result["limitations"]


@pytest.mark.parametrize("instanced", [False, True])
def test_zero_default_instances_only_blocks_actual_instanced_mesh_observations(integrated, instanced):
    bridge, ctrl = integrated
    ctrl.action.flags = 2 | (0x020000 if instanced else 0)
    ctrl.action.numInstances = 0
    if instanced:
        with pytest.raises(ValueError, match="instance is unavailable"):
            bridge._get_post_vs_data({"event_id": 30})
        assert not ctrl.post_calls
    else:
        result = bridge._get_post_vs_data({"event_id": 30})
        assert ctrl.post_calls == [(30, 0, 0, "vs")]
        assert result["instanceCount"] == 1 and result["allInstances"]
    assert ctrl.event == 7


def test_mesh_image_is_managed_readable_and_sent_as_image_content(tmp_path, monkeypatch):
    value = dict(reader.read_mesh(Controller(), mesh()), eventId=30, captureSessionId="session",
                 allInstances=True, instanceCount=1)
    seen = []
    monkeypatch.setattr(server, "get_post_vs_data", lambda *args: seen.append(args) or json.dumps(value))
    monkeypatch.setattr(server, "_event_texture_evidence_root", lambda: tmp_path)
    result = server.capture_mesh_image(30, max_dimension=128)
    receipt = json.loads(result[0])
    assert receipt["saved"] and receipt["inlineImage"] == "included in this result"
    assert len(result) == 2
    assert Path(receipt["path"]).is_relative_to(tmp_path)
    assert json.loads(Path(receipt["sourcePath"]).read_text())["eventId"] == 30
    assert seen[0][0] == 30
    assert not receipt["illustration"]["depthTested"]


def test_mesh_image_rejects_wrong_eid_without_creating_an_artifact(tmp_path, monkeypatch):
    value = dict(reader.read_mesh(Controller(), mesh()), eventId=9, captureSessionId="session")
    monkeypatch.setattr(server, "get_post_vs_data", lambda *args: json.dumps(value))
    monkeypatch.setattr(server, "_event_texture_evidence_root", lambda: tmp_path)
    receipt = json.loads(server.capture_mesh_image(30)[0])
    assert "requested capture/EID" in receipt["error"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("changes", [{"complete": False}, {"allInstances": False},
                                   {"finalPositionStage": False}, {"viewCount": 2},
                                   {"eventId": 9}, {"viewport": {"x": 10, "y": 0, "width": 100, "height": 100}}])
def test_draw_capture_uses_exact_eid_and_does_not_crop_incomplete_geometry(monkeypatch, changes):
    value = dict(reader.read_mesh(Controller(), mesh()), eventId=30, allInstances=True,
                 finalPositionStage=True,
                 viewport={"x": 0, "y": 0, "width": 100, "height": 100})
    value.update(changes)
    queries, exports = [], []
    def call(name, arguments=None, timeout=None):
        queries.append((name, arguments))
        return json.dumps(value)
    monkeypatch.setattr(server, "_call", call)
    monkeypatch.setattr(server, "_draw_target_size", lambda *args: {"width": 100, "height": 100})
    monkeypatch.setattr(server, "_capture_event_texture_evidence_receipt", lambda **args:
                        exports.append(args) or json.dumps({"artifact": {"status": "skipped"}}))
    server.capture_draw_image(30, inline=False)
    assert queries[0][0] == "get_post_vs_data" and queries[0][1]["event_id"] == 30
    assert exports[0]["crop"] is None
