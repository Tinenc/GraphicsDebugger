"""High-level ShaderWorkspace compile, ABI validation, replay and rollback smoke test."""
import argparse
import json
import os
from pathlib import Path
import sys
import shutil
import uuid

parser = argparse.ArgumentParser()
parser.add_argument("--instance", required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--app", type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[3]
os.environ["REPLAYLAB_INSTANCE_ID"] = args.instance
os.environ["BREAD_APP_ROOT"] = str(args.app / "replaylab")
os.environ["BREAD_TOOLS_ROOT"] = str(args.app / "replaylab/tools")
sys.path.insert(0, str(root / "replaylab/mcp_host"))
from src.replaylab_tools import configure_environment
configure_environment(executable_dir=args.app / "replaylab")
from src import server
args.output.mkdir(parents=True, exist_ok=True)
def run(name, **kwargs):
    raw = getattr(server, name)(**kwargs)
    value = json.loads(raw) if isinstance(raw, str) else raw
    (args.output / (name + ".json")).write_text(json.dumps(value, indent=2, ensure_ascii=False),encoding="utf-8")
    print(name, json.dumps(value, ensure_ascii=False)[:800], flush=True)
    if isinstance(value, dict) and value.get("error"):
        raise RuntimeError(value["error"])
    return value
workspace = str(args.output / "workspace")
if not Path(workspace).exists():
    run("prepare_shader_workspace",event_id=11,stage="fragment",workspace_path=workspace,
        catalog_output_dir=str(args.output / "catalog"),attempt_reconstruction=True)
state = run("get_shader_workspace_status", workspace_path=workspace)["result"]
assert state["source"]["present"] and state["source"]["baselineCompileVerified"]
session = server.ipc.call("get_capture_status", {})["captureSessionId"]
def screenshot(name):
    request = uuid.uuid4().hex
    directory = args.output / ("eid-11-" + request)
    directory.mkdir()
    result = server.ipc.call("capture_event_texture_evidence", {"event_id":11,"attachment_kind":"rtv",
        "attachment_index":0,"channel":"rgba","mip":0,"slice":0,"sample":0,"range_mode":"native",
        "expected_capture_session_id":session,"request_id":request,"artifact_dir":str(directory)})
    (args.output / (name + ".json")).write_text(json.dumps(result,indent=2),encoding="utf-8")
    pictures = list(directory.glob("*.png"))
    assert len(pictures) == 1, result
    shutil.copyfile(pictures[0], args.output / (name + ".png"))
screenshot("baseline")
source = Path(state["source"]["path"])
text = source.read_text(encoding="utf-8")
before = "o0.xyzw = Tint.xyzw * r0.xyzw;"
after = "o0.xyzw = Tint.xyzw * r0.xyzw * float4(0.25, 1, 0.3, 1);"
assert before in text or after in text, "unexpected test shader"
source.write_text(text.replace(before, after),encoding="utf-8")
compiled = run("compile_shader_workspace", workspace_path=workspace,prefer_native=False)["result"]
state = run("get_shader_workspace_status", workspace_path=workspace)["result"]
candidate = compiled["rawSha256"]
validation = run("validate_shader_workspace_candidate",workspace_path=workspace,
    expected_revision=state["revision"],expected_candidate_raw_sha256=candidate)
assert validation["validation"]["replayAbiValidated"] is True, validation
applied = run("apply_shader_workspace_candidate", workspace_path=workspace,event_id=11,
    expected_revision=state["revision"],expected_candidate_raw_sha256=candidate)
assert applied["applied"] is True, applied
try:
    screenshot("shader-edited")
finally:
    run("rollback_shader_workspace_override",workspace_path=workspace,override_id=applied["override"]["overrideId"])
screenshot("restored")
assert (args.output / "baseline.png").read_bytes() != (args.output / "shader-edited.png").read_bytes()
assert (args.output / "baseline.png").read_bytes() == (args.output / "restored.png").read_bytes()
print("SHADER_SMOKE_PASS", flush=True)
