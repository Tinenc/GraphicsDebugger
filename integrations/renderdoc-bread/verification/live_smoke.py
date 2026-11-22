"""Run against a specifically selected Bread bridge; never chooses a user's capture."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import shutil
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "replaylab/mcp_host"))
from src.ipc_client import IPCClient

parser = argparse.ArgumentParser()
parser.add_argument("--instance", required=True)
parser.add_argument("--capture", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
client = IPCClient(timeout=90, instance_id=args.instance)
receipts = {}

def call(method, params=None, label=None):
    result = client.call(method, params or {})
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(f"{method}: {result['error']}")
    receipts[label or method] = result
    (args.output / "receipts.json").write_text(json.dumps(receipts, indent=2, ensure_ascii=False), encoding="utf-8")
    print(method, json.dumps(result, ensure_ascii=False)[:160], flush=True)
    return result

call("ping")
call("open_capture", {"capture_path": str(args.capture.resolve())})
status = call("get_capture_status")
assert Path(status["capturePath"]).resolve() == args.capture.resolve(), "wrong capture"
actions = call("get_draw_calls")
event = next(a["eventId"] for a in actions["actions"] if a["numIndices"] == 3)
pipe = call("get_pipeline_state", {"event_id": event})
resource = next(a["outputs"][0] for a in actions["actions"] if a["eventId"] == event)
def screenshot(name):
    request = uuid.uuid4().hex
    directory = args.output / ("eid-%d-%s" % (event, request))
    directory.mkdir()
    result = call("capture_event_texture_evidence", {"event_id":event,"attachment_kind":"rtv",
        "attachment_index":0,"channel":"rgba","mip":0,"slice":0,"sample":0,"range_mode":"native",
        "expected_capture_session_id":status["captureSessionId"],"request_id":request,
        "artifact_dir":str(directory)}, name)
    pictures = list(directory.glob("*.png"))
    assert len(pictures) == 1, result
    shutil.copyfile(pictures[0], args.output / (name + ".png"))
screenshot("baseline")
catalog = call("export_shader_catalog", {"output_dir": str(args.output / "catalog"), "event_ids": [event]})
call("get_capture_status", label="catalogStatus")
capture_id = receipts["catalogStatus"]["captureId"]
snap = call("snapshot_event_cbuffer", {"event_id": event, "stage": "fragment", "block_index": 0, "expected_capture_id": capture_id})
field = next(f for f in snap["fields"] if f["byteSize"] == 16)
packed = struct.pack("<4f", 0.1, 0.3, 1, 1)
raw = bytearray(base64.b64decode(snap["rawBase64"]))
raw[field["byteOffset"]:field["byteOffset"] + 16] = packed
binding = {"snapshot_id": snap["snapshotId"], "expected_capture_id": capture_id,
           "expected_capture_session_id": snap["captureSessionId"]}
applied = None
try:
    applied = call("apply_event_cbuffer_override", dict(binding, expected_raw_sha256=snap["rawSha256"],
        expected_layout_sha256=snap["layoutSha256"], expected_revision=0, new_revision=1,
        expected_override_id="", data_base64=base64.b64encode(raw).decode(),
        data_sha256=hashlib.sha256(raw).hexdigest(),
        patches=[{"leafId":field["leafId"],"packedBase64":base64.b64encode(packed).decode()}]))
    call("get_pipeline_state", {"event_id": event}, "afterCBufferPipeline")
    screenshot("cbuffer-edited")
finally:
    call("rollback_event_cbuffer_override", dict(binding, override_id=(applied or {}).get("overrideId", "")))
call("get_pipeline_state", {"event_id":event}, "restoredPipeline")
screenshot("restored")
baseline=(args.output / "baseline.png").read_bytes()
assert baseline != (args.output / "cbuffer-edited.png").read_bytes(), "CBuffer did not change the output"
assert baseline == (args.output / "restored.png").read_bytes(), "CBuffer rollback did not restore exact image"
call("get_current_frame_flow_inputs", {"max_actions":8000,"expected_capture_session_id":status["captureSessionId"]})
call("export_to_unity", {"event_id":event,"output_dir":str(args.output / "unity-export"),"mesh_name":"BreadTriangle"})
print("LIVE_SMOKE_PASS", flush=True)
