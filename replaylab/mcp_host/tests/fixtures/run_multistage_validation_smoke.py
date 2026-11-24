"""Complete the installed native smoke through real capture-bound IPC calls.

Argument: directory containing native-smoke.json, *.original.bin and catalog
receipts produced by qrenderdoc_multistage_validation_smoke.py and a fresh
export_shader_catalog call for its exact stage/EIDs. Never builds/replaces source.
"""
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.ipc_client import IPCClient

root = Path(sys.argv[1]).resolve()
receipt = json.loads((root / "catalog-receipt.json").read_text(encoding="utf-8"))
catalog = json.loads(Path(receipt["catalogPath"]).read_text(encoding="utf-8"))
result = json.loads((root / "native-smoke.json").read_text(encoding="utf-8"))
client = IPCClient(timeout=300)
for stage, event_id in [("vertex", 1267), ("compute", result["compute"]["eventId"])]:
    blob = root / (stage + ".original.bin")
    sha = hashlib.sha256(blob.read_bytes()).hexdigest()
    uses = [use for use in catalog["uses"] if use["stage"] == stage and use["eventId"] == event_id and use["rawSha256"] == sha]
    if len(uses) != 1:
        raise RuntimeError("Original stage bytes no longer match the exact catalog use")
    use = uses[0]
    params = {"stage": stage, "event_id": event_id, "expected_capture_id": receipt["captureId"],
        "expected_capture_session_id": receipt["captureSessionId"],
        "expected_original_resource_id": use["resourceId"], "expected_original_sha256": sha,
        "entry_point": use["entryPoint"], "encoding": "spirv",
        "baseline": {"path": str(blob), "rawSha256": sha}, "candidate": {"path": str(blob), "rawSha256": sha}}
    observed = client.call("compare_shader_organization_candidates", params, timeout=300)
    result[stage + "Comparison"] = observed
    result["checks"][stage + "ABA"] = observed.get("accepted") is True and observed.get("replayRestored") is True
    result["diagnostics"] = [row for row in result["diagnostics"] if row.get("stage") != stage]
    result["ok"] = all(result["checks"].get(name) for name in
        ("vertexNativeChannels", "computeNativeStorage", "vertexABA", "computeABA", "eventRestored"))
    (root / "native-smoke.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"stage": stage, "accepted": observed.get("accepted"), "sampleCount": observed.get("sampleCount"),
        "reason": observed.get("reason"), "replayRestored": observed.get("replayRestored")}), flush=True)
