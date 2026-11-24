"""Captured bytes remain usable independently of optional workflow receipts."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import struct

import pytest

from src.capture_source import capture_source
from src.resource_export import ResourceExportManager, ResourceExportError
from src.unity_restore import _buffer_bindings
from src.unity_sequence import UnitySequenceStore
from src.unity_restoration_requirements import track
from test_unity_restoration import prepared  # noqa: F401
from tests.test_unity_restore import finalized_package


def test_same_rdc_reopens_and_file_metadata_changes_are_diagnostics(prepared, tmp_path):
    store, task, *_ = prepared
    path = tmp_path / "capture.rdc"
    path.write_bytes(b"capture-one")
    capture = {"path": str(path), "api": "Vulkan", "captureSessionId": "a" * 32}
    with store.transaction(task) as state:
        state["capture"] = {**capture, "sourceIdentity": capture_source(capture)}
    before = store.load(task)
    result = store.observe_capture(task, {**capture, "captureSessionId": "b" * 32})
    assert result["binding"] == before["binding"]
    assert result["reference"] is None  # no claim that a new session was observed
    assert result["capture"]["captureSessionId"] == "b" * 32
    assert result["captureHistory"][-1]["reference"] == before["reference"]
    path.write_bytes(b"capture-two")
    changed = store.observe_capture(task, {**capture, "captureSessionId": "c" * 32})
    assert changed["captureHistory"][-1]["reason"] == "capture-file-refreshed"
    assert changed["diagnostics"][-1]["code"] == "capture-file-identity-changed"
    unrelated = tmp_path / "other.rdc"
    unrelated.write_bytes(b"unrelated capture")
    with pytest.raises(ValueError, match="different RDC"):
        store.observe_capture(task, {**capture, "path": str(unrelated), "captureSessionId": "d" * 32})
    assert store.load(task)["capture"]["captureSessionId"] == "c" * 32


def test_legacy_path_intent_adopts_reopened_capture_without_new_task(prepared, tmp_path):
    store, task, *_ = prepared
    path = tmp_path / "capture.rdc"
    path.write_bytes(b"legacy")
    with store.transaction(task) as state:
        state["capture"] = {"path": str(path), "captureSessionId": "a" * 32}
    result = store.observe_capture(task, {"path": str(path), "captureSessionId": "b" * 32})
    assert result["restorationId"] == task
    assert result["captureHistory"][-1]["reason"] == "legacy-path-intent-adopted"
    assert result["capture"]["sourceIdentity"]["contentSha256"]


def test_old_package_import_needs_no_live_session_after_all_original_sessions_close(prepared):
    store, task, _, sources, bundle, _ = prepared
    state = store.load(task)
    bundle["captureSessionId"] = "f" * 32
    result = store.bind(task, {"status": "finalized", "operationId": "1" * 32,
        "packagePath": state["binding"]["packagePath"]},
        {"requestPath": state["binding"]["requestPath"]}, sources)
    assert result["binding"]["operationId"] == "1" * 32
    assert result["diagnostics"][-1]["code"] == "legacy-export-source-context-unrecorded"


def test_failed_observation_does_not_erase_prior_binding_and_render(prepared):
    store, task, observed, *_ = prepared
    store.compare(task, observed)
    before = store.load(task)
    ledger = {"unityRestorations": {task: {"restorationId": task}}}
    track(ledger, "check_unity_restoration", {"error": "Editor temporarily unavailable"},
          {"restoration_id": task}, True)
    assert store.load(task) == before
    assert ledger["unityRestorations"][task]["diagnosticsOnly"]


def test_large_shader_proof_moves_to_manifested_sidecar_without_losing_evidence(tmp_path):
    root = tmp_path / "managed"
    package = finalized_package(root)
    manager = ResourceExportManager(root)
    state, _, _ = manager._validated_capture(package.parent.name)
    child = manager.fork_capture(package.parent.name, state["capture"]["manifestSha256"], "large-proof")
    source = tmp_path / "source.hlsl"
    source.write_text("float4 Main():SV_Target{return 1;}", encoding="utf-8")
    proof = {"workspacePath": str(tmp_path), "stage": "fragment", "ready": True,
        "sourcePath": str(source), "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "validation": {"mode": "validated-source", "replayValidated": True,
            "passedChecks": ["compilation", "abi"], "review": {"expanded": "e" * (1100 * 1024)}}}
    spec = json.loads((package / "transfer-spec.json").read_text())
    spec["shader"]["organizedSources"] = [proof]
    result = manager.finalize(child["operationId"], child["operation"]["capture"]["manifestSha256"], spec)
    target = Path(result["packagePath"])
    row = json.loads((target / "transfer-spec.json").read_text())["shader"]["organizedSources"][0]
    assert (target / "transfer-spec.json").stat().st_size < 1024 * 1024
    raw = (target / row["evidence"]["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == row["evidence"]["sha256"]
    assert json.loads(raw) == proof
    manifest = json.loads((target / "package-manifest.json").read_text())
    assert any(f["path"] == row["evidence"]["path"] for f in manifest["files"])
    assert manager.finalize(child["operationId"], child["operation"]["capture"]["manifestSha256"], spec)["idempotent"]


def test_nonfinite_packed_channel_is_preserved_for_bit_exact_unity_buffer_adapter(tmp_path):
    root = tmp_path / "managed"
    package = finalized_package(root)
    manager = ResourceExportManager(root)
    state, capture, original = manager._validated_capture(package.parent.name)
    bundle = copy.deepcopy(original)
    raw = struct.pack("<3I", 0x7FC01234, 0xFFFFFFFF, 0x3F800000)
    relative = "mesh/packed.bin"
    for directory in (capture, package / "source"):
        (directory / relative).write_bytes(raw)
    evidence = {"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
        "status": "captured", "count": 3, "components": 1, "encoding": "float32le"}
    bundle["mesh"]["attributes"].append({"index": 1, "name": "Packed", "semantic": "TEXCOORD0",
        "canonical": evidence, "raw": evidence})
    output = tmp_path / "gltf"
    output.mkdir()
    spec = json.loads((package / "transfer-spec.json").read_text())
    result = manager._write_gltf(output, capture, bundle, state["capture"]["manifestSha256"], spec)
    assert result["vertexCount"] == 3
    assert result["omittedAttributes"][0]["shaderInputPolicy"] == "raw-bits"
    bindings = _buffer_bindings([{"name": "_Packed", "bufferType": "raw", "stride": 4,
        "bufferSource": {"vertexAttributeIndex": 1}}], bundle, package, {"source/" + relative: evidence["sha256"]})
    assert base64.b64decode(bindings[0]["bufferBase64"]) == raw
    bundle["mesh"]["attributes"][1]["semantic"] = "POSITION"
    with pytest.raises(ResourceExportError, match="POSITION contains"):
        manager._write_gltf(output, capture, bundle, state["capture"]["manifestSha256"], spec)


def test_sequence_order_is_advisory_and_annotation_does_not_claim_validation(prepared):
    store, task, _, _, _, project = prepared
    sequences = UnitySequenceStore(store)
    plan = sequences.create(store.load(task)["capture"], str(project), {"actions": [
        {"eventId": 24, "flags": 4}, {"eventId": 22, "flags": 0}, {"eventId": 23, "flags": 2}]}, [], [[22, 24]])
    assert [row["eventId"] for row in plan["actions"]] == [23, 24]
    assert plan["orderIsAdvisory"]
    result = sequences.annotate(plan["sequenceId"], 23, "ready-for-next", "Direct Unity render collected; managed review omitted", ["image.png"])
    assert result["current"]["eventId"] == 24
    assert not result["complete"]
    assert not result["actions"][0]["annotation"]["automatedValidation"]


def test_raw_payload_remains_readable_when_a_derived_channel_file_is_missing(tmp_path, monkeypatch):
    root = tmp_path / "managed"
    package = finalized_package(root)
    manager = ResourceExportManager(root)
    state, directory, bundle = manager._validated_capture(package.parent.name)
    (directory / bundle["mesh"]["attributes"][0]["canonical"]["path"]).unlink()
    monkeypatch.setattr(manager, "_validated_capture", lambda *_: pytest.fail("Data reads must not repeat full export validation"))
    result = manager.capture_payload(package.parent.name)
    assert result["eventId"] == 23 and result["dataOnly"]
    assert result["bundlePath"] == str(directory / state["capture"]["manifestRelativePath"])
    assert result["requiresFinalization"] is False


@pytest.mark.asyncio
async def test_registered_sequence_can_inspect_later_eid_without_prior_acceptance(prepared):
    from types import SimpleNamespace
    from mcp.server.fastmcp import FastMCP
    from src.unity_restoration_tools import register_tools
    store, task, _, _, _, project = prepared
    capture = store.load(task)["capture"]
    host = SimpleNamespace(_resource_export_live_identity=lambda: ({"capture": capture}, "", capture["captureSessionId"], "Vulkan"),
        ipc=SimpleNamespace(call=lambda *_: {"actions": [{"eventId": 23, "flags": 2}, {"eventId": 24, "flags": 4}]}),
        inspect_resource_export=lambda eid: json.dumps({"eventId": eid, "mesh": {}}),
        get_pipeline_state=lambda eid: json.dumps({"eventId": eid, "depthState": {"compare": "Equal"}}),
        get_event_resource_graph=lambda eid, *_: json.dumps({"eventId": eid, "edges": []}),
        _ok_compact=json.dumps, _err=lambda error: json.dumps({"error": str(error)}))
    mcp = FastMCP("sequence test")
    register_tools(mcp, host)

    async def invoke(name, **arguments):
        result = await mcp.call_tool(name, arguments)
        content = result[0] if isinstance(result, tuple) else result
        return json.loads("".join(block.text for block in content if block.type == "text"))

    plan = await invoke("plan_unity_restoration_sequence", project_path=str(project), event_ranges=[[23, 24]])
    result = await invoke("get_unity_restoration_step", sequence_id=plan["sequenceId"], event_id=24, include_dependencies=True)
    assert result["step"]["eventId"] == 24
    assert result["pipelineState"]["depthState"]["compare"] == "Equal"
    assert result["resourceGraph"]["eventId"] == 24
    assert result["orderIsAdvisory"] and not plan["complete"]
