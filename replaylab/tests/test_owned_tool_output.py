from __future__ import annotations
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from replaylab_agent.agent_contracts import OrchestratorConfig, RunRequest
from replaylab_agent.deep_engine import BreadMCPTools
from replaylab_agent.profiles import Profile
from replaylab_agent.tool_output import (OwnedOutputStore, guard_tool_output_text,
    guard_mcp_call_result, is_complete_shader_file_output, MAX_INLINE_TOOL_OUTPUT_BYTES)


def _retain(store, text):
    return json.loads(guard_tool_output_text("external__large", text, store=store,
                                           authority="user-configured-mcp-server"))


def test_owned_json_search_returns_exact_utf8_match_offsets_without_whole_file_paging(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    store = OwnedOutputStore()
    raw = ("{\"shader\":\"" + "雨" * 70000 + " sample(texture0) 雨滴" * 35 + "\"}").encode("utf-8")
    path = store.write(raw, hashlib.sha256(raw).hexdigest(), is_json=True,
                       tool="inspect_shader", authority="first-party-host")
    matches, offset = [], 0
    try:
        while True:
            page = store.read({"path": str(path), "text_query": "sample", "offset": offset})
            assert len(page["matches"]) <= 16
            assert len(json.dumps(page).encode("utf-8")) < MAX_INLINE_TOOL_OUTPUT_BYTES
            matches.extend(page["matches"])
            offset = page["nextOffset"]
            if not page["truncated"]:
                break
        assert len(matches) == 35
        for match in matches:
            assert match["offset"] > 200000
            assert raw[match["matchOffset"]:].startswith(b"sample")
            assert "sample" in store.read({"path": str(path), "offset": match["offset"], "max_bytes": 512})["content"]
        path.write_bytes(b"tampered")
        with pytest.raises(ValueError, match="SHA-256"):
            store.read({"path": str(path), "text_query": "sample"})
    finally:
        store.close()


def _complete_shader_payload(source):
    raw = source.encode("utf-8")
    return {"schema": "renderdoc-ai-shader-workspace-file/v1", "mode": "complete",
            "workspacePath": "D:/workspace", "relativePath": "source/shader.hlsl",
            "sizeBytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "totalChars": len(source), "offset": 0, "chars": len(source),
            "truncated": False, "text": source, "agentFile": None,
            "coverage": {"byteOffset": 0, "returnedBytes": len(raw), "complete": True},
            "applied": False}


@pytest.mark.parametrize("transport", ["native-codex", "native-acp", "openai-responses",
                                       "openai-chat", "anthropic-messages"])
def test_large_operation_error_keeps_current_recovery_facts_across_transports(
        tmp_path, monkeypatch, transport):
    from mcp.types import CallToolResult, TextContent
    from replaylab_agent.deep_engine import _mcp_result_payload
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    payload = {
        "ok": False, "applied": False, "error": "CBuffer alias store exceeds its bounded size",
        "errorType": "ValueError", "errorCode": "alias-store-capacity",
        "operationId": "current-publication", "requestSha256": "a" * 64,
        "mutation": {"operationId": "current-publication", "requestSha256": "a" * 64,
                     "tool": "publish_cbuffer_aliases", "status": "failed", "phase": "failed",
                     "terminal": True, "sameOperationRetryAllowed": True, "idempotentReplay": False},
        "diagnostic": {"code": "alias-store-capacity", "recoverable": True,
                       "actualBytes": 40000, "limitBytes": 32000},
        "history": [{"error": "unrelated historical compile failure", "status": "applied"}],
        "traceback": "trace detail 雨" * 10000,
    }
    original = json.dumps(payload, ensure_ascii=False)
    result = CallToolResult(content=[TextContent(type="text", text=original)],
                            structuredContent={"result": original}, isError=False)
    store = OwnedOutputStore()
    try:
        if transport.startswith("native-"):
            guarded = guard_mcp_call_result(result, "publish_cbuffer_aliases", store=store)
            assert guarded.isError is True
            pointer = json.loads(guarded.content[0].text)
        else:
            pointer = json.loads(_mcp_result_payload(result, transport,
                "publish_cbuffer_aliases", store=store))
        assert len(json.dumps(pointer, ensure_ascii=False).encode("utf8")) < MAX_INLINE_TOOL_OUTPUT_BYTES
        assert pointer["isError"] is True
        failure = pointer["receipt"]["operationFailure"]
        assert failure["error"] == payload["error"]
        assert failure["applied"] is False
        assert failure["mutation"] == payload["mutation"]
        assert failure["diagnostic"] == payload["diagnostic"]
        assert pointer["status"] == "failed"
        assert "history" not in failure and "traceback" not in failure
        assert pointer["doNotRetry"] is True
        assert pointer["doNotRetryScope"] == "result-retrieval-only"
        assert "Supported recovery remains available" in pointer["operationRecovery"]
        retained = Path(pointer["path"]).read_text(encoding="utf8")
        expected = original if transport.startswith("native-") else "ERROR: " + original
        assert hashlib.sha256(retained.encode()).digest() == hashlib.sha256(expected.encode()).digest()
    finally:
        store.close()


@pytest.mark.parametrize("authority,ok", [("first-party-host", True),
                                         ("user-configured-mcp-server", False)])
def test_operation_failure_summary_does_not_promote_history_or_external_claims(
        tmp_path, monkeypatch, authority, ok):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    payload = {"ok": ok, "error": "" if ok else "external failure", "applied": False,
               "history": [{"ok": False, "error": "old failure", "recoverable": True}],
               "evidence": "e" * 60000}
    store = OwnedOutputStore()
    try:
        pointer = json.loads(guard_tool_output_text("publish_cbuffer_aliases",
            json.dumps(payload), store=store, authority=authority))
        assert "operationFailure" not in pointer.get("receipt", {})
        assert "status" not in pointer
    finally:
        store.close()


def test_current_mutation_outcome_wins_over_earlier_nested_history(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    store = OwnedOutputStore()
    payload = {"ok": False, "history": [{"status": "applied", "operationId": "old", "applied": True}],
               "mutation": {"status": "failed", "operationId": "current", "terminal": True},
               "error": "current publication failed", "traceback": "x" * 60000}
    try:
        pointer = json.loads(guard_tool_output_text("publish_cbuffer_aliases", json.dumps(payload), store=store))
        assert pointer["status"] == "failed"
        assert pointer["operationId"] == "current"
        assert "applied" not in pointer
    finally:
        store.close()


@pytest.mark.parametrize("tool,envelope", [("get_shader_workspace_status", True),
    ("compile_shader_workspace", True), ("apply_shader_workspace_shared_resource", False)])
def test_status_compile_apply_spill_retains_cbuffer_pending_effect(tmp_path, monkeypatch, tool, envelope):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    semantic = {"required": True, "status": "pending", "candidateRawSha256": "b" * 64,
                "reason": "publication-display-target-stale", "recompileRequired": False}
    payload = {"workspacePath": "D:/workspace", "cbufferSemanticDelivery": semantic,
               "longReflection": "x" * 60000}
    payload = {"result": payload} if envelope else payload
    result = json.loads(guard_tool_output_text(tool, json.dumps(payload), store=OwnedOutputStore(),
                                              authority="first-party-host"))
    assert result["receipt"]["cbufferSemanticDelivery"] == semantic


@pytest.mark.parametrize("written,preserved,mirror_skipped", [(0, 0, 1), (2, 0, 0), (0, 2, 0)])
def test_spilled_cbuffer_publication_preserves_semantic_outcome_and_exact_evidence(
        tmp_path, monkeypatch, written, preserved, mirror_skipped):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    store = OwnedOutputStore()
    payload = {"schema": "replaylab-cbuffer-alias-publication/v1", "applied": True,
               "semanticSourceSha256": "c" * 64, "activeDisplayShaderRawSha256": "d" * 64,
               "partial": written < 2 or bool(mirror_skipped), "operationId": "naming-1",
               "bindingCoverage": {"schema": "bread-cbuffer-binding-coverage/v1", "complete": False,
                                   "bindingCount": 6, "reviewedBindingCount": 1, "unresolvedLaneCount": 1,
                                   "pendingBindings": [{"bind": 1, "space": 0, "missingLaneCount": 211}]},
               "physicalBinding": {"eventId": 61408, "stage": "fragment", "shaderRawSha256": "a" * 64},
               "counts": {"requestedRows": 200, "writtenRows": 200, "requestedSemanticFields": 2,
                          "writtenSemanticFields": written, "keptUserSemanticFields": preserved,
                          "requestedDisplaySemanticFields": 2, "writtenDisplaySemanticFields": written,
                          "keptUserDisplaySemanticFields": preserved},
               "acceptance": {"requestedSemanticFieldsAccountedFor": written + preserved == 2,
                              "currentDisplaySemanticFieldsAccountedFor": not mirror_skipped and written + preserved == 2},
               "published": [{"bind": 5, "space": 0, "semanticViewWritten": bool(written),
                              "semanticFieldsWritten": written, "semanticFieldsKeptUser": preserved,
                              "semanticBindingBasis": "verified-active-reflection",
                              "written": [{"alias": "original row " + str(i), "evidence": "x" * 200}
                                          for i in range(200)]}],
               "semanticMirrors": [],
               "semanticOriginalUnavailable": [{"bind": 5, "space": 0,
                                                "reason": "original reflection has overlapping storage placeholders",
                                                "basis": "verified-active-reflection"}],
               "semanticMirrorSkipped": [{"bind": 5, "space": 0, "name": "baseColor",
                                          "reason": "lane absent in the active replacement"}] * mirror_skipped}
    encoded = json.dumps(payload, ensure_ascii=False)
    pointer = json.loads(guard_tool_output_text("publish_cbuffer_aliases", encoded, store=store))
    assert len(json.dumps(pointer, ensure_ascii=False).encode()) < MAX_INLINE_TOOL_OUTPUT_BYTES
    assert pointer["doNotRetry"] is True and pointer["receiptIsPartial"] is True
    assert pointer["doNotRetryScope"] == "result-retrieval-only"
    receipt = pointer["receipt"]["cbufferPublication"]
    assert receipt["counts"] == payload["counts"]
    assert receipt["acceptance"] == payload["acceptance"]
    assert receipt["partial"] == payload["partial"]
    assert receipt["semanticSourceSha256"] == payload["semanticSourceSha256"]
    assert receipt["activeDisplayShaderRawSha256"] == payload["activeDisplayShaderRawSha256"]
    assert receipt["physicalBinding"] == payload["physicalBinding"]
    assert receipt["bindingCoverage"] == payload["bindingCoverage"]
    assert receipt["published"]["blocks"][0]["semanticFieldsWritten"] == written
    assert receipt["published"]["blocks"][0]["semanticFieldsKeptUser"] == preserved
    assert receipt["published"]["blocks"][0]["semanticBindingBasis"] == "verified-active-reflection"
    assert receipt["semanticOriginalUnavailable"]["count"] == 1
    assert receipt["semanticOriginalUnavailable"]["examples"][0]["basis"] == "verified-active-reflection"
    assert receipt["semanticMirrorSkipped"]["count"] == mirror_skipped
    # The compact result distinguishes ordinary-name success, field failure,
    # and preserved user fields. Detailed receipts remain recoverable once.
    assert Path(pointer["path"]).read_text(encoding="utf-8") == encoded
    store.close()


@pytest.mark.parametrize("tool", ["begin_shader_workspace_organization", "finish_shader_workspace_organization"])
def test_large_source_receipt_keeps_required_semantic_delivery_visible(tmp_path, monkeypatch, tool):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    contract = {"required": True, "status": "pending", "sourceSha256": "a" * 64,
                "candidateRawSha256": "b" * 64, "recompileRequired": False}
    payload = {"workspacePath": "D:/shader", "sourceSha256": "a" * 64,
               "cbufferSemanticDelivery": contract, "longEvidence": "e" * (2 * MAX_INLINE_TOOL_OUTPUT_BYTES)}
    envelope = json.loads(guard_tool_output_text(tool, json.dumps(payload)))
    assert envelope["receipt"]["cbufferSemanticDelivery"] == contract
    assert envelope["doNotRetry"] is True


@pytest.mark.parametrize("tool", ["begin_shader_workspace_organization", "finish_shader_workspace_organization",
    "get_shader_workspace_status", "record_shader_resource_naming_review", "record_shader_organization_review"])
def test_large_all_binding_evidence_never_hides_remaining_naming_contract(tmp_path, monkeypatch, tool):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    coverage = {"complete": False, "bindingCount": 5, "reviewedBindingCount": 1,
                "pendingBindings": [{"bind": i, "space": 0, "missingLaneCount": 100,
                                     "evidence": "x" * 6000} for i in range(4)]}
    resource = {"required": True, "status": "pending", "sourceSha256": "a" * 64,
                "tool": "record_shader_resource_naming_review", "receiptRelativePath": "ai/resource-naming-review.json",
                "reason": "resource-naming-review-missing",
                "textureCoverage": {"bindingCount": 9, "reviewedBindingCount": 3, "missingCount": 6},
                "assetUsage": {"status": "partial", "evidence": "matched material " * 1000}}
    semantic = {"required": True, "status": "pending", "sourceSha256": "a" * 64,
                "cbufferStatus": "pending", "bindingCoverage": coverage, "resourceNamingDelivery": resource,
                "nextActions": [{"tool": "get_shader_workspace_outline", "arguments": {"section": "resource-naming"}},
                                {"tool": "record_shader_resource_naming_review", "purpose": "Review current bindings"}]}
    preparation = {"configured": True, "status": "partial", "sourceNamesApplied": False,
                   "panelAliasesApplied": False, "evidence": "asset details " * 6000}
    payload = {"cbufferSemanticDelivery": semantic, "assetNamingPreparation": preparation,
               "sourceSha256": "a" * 64}
    pointer = json.loads(guard_tool_output_text(tool, json.dumps(payload)))
    compact = pointer["receipt"]["cbufferSemanticDelivery"]
    assert len(json.dumps(compact).encode()) <= 4096
    assert compact["required"] and compact["status"] == "pending"
    assert compact["sourceSha256"] == "a" * 64
    assert compact["bindingCoverage"]["reviewedBindingCount"] == 1
    assert compact["resourceNamingDelivery"]["required"]
    assert compact["resourceNamingDelivery"]["textureCoverage"]["missingCount"] == 6
    assert compact["resourceNamingDelivery"]["tool"] == "record_shader_resource_naming_review"
    assert compact["resourceNamingDelivery"]["receiptRelativePath"] == "ai/resource-naming-review.json"
    assert compact["nextActions"][0]["arguments"] == {"section": "resource-naming"}
    if tool == "begin_shader_workspace_organization":
        assert pointer["receipt"]["assetNamingPreparation"]["sourceNamesApplied"] is False
    assert pointer["doNotRetry"]
    assert json.loads(Path(pointer["path"]).read_text(encoding="utf-8")) == payload


def test_large_cbuffer_projection_bounds_diagnostics_and_does_not_trust_external_claims(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    payload = {"schema": "replaylab-cbuffer-alias-publication/v1", "applied": True, "partial": True,
               "activeDisplayShaderRawSha256": "a" * 64, "activeReplacementShaderRawSha256": "b" * 64,
               "counts": {"requestedSemanticFields": 100, "writtenSemanticFields": 0},
               "published": [{"bind": i, "space": 0, "semanticFieldsWritten": 0} for i in range(100)],
               "semanticMirrorSkipped": [{"name": "颜色" * 5000, "reason": "详细诊断" * 5000}] * 20}
    original = json.dumps(payload, ensure_ascii=False)
    bounded = guard_tool_output_text("publish_cbuffer_aliases", original)
    assert len(bounded.encode()) < MAX_INLINE_TOOL_OUTPUT_BYTES
    receipt = json.loads(bounded)["receipt"]["cbufferPublication"]
    assert receipt["activeDisplayShaderRawSha256"] == "a" * 64
    assert receipt["activeReplacementShaderRawSha256"] == "b" * 64
    assert receipt["published"]["detailsOmitted"] is True
    assert receipt["published"]["blockCount"] == 100
    assert receipt["semanticMirrorSkipped"]["count"] == 20
    assert len(receipt["semanticMirrorSkipped"]["examples"]) == 4
    external = json.loads(guard_tool_output_text("publish_cbuffer_aliases", original,
                                                 authority="user-configured-mcp-server"))
    assert "cbufferPublication" not in external.get("receipt", {})


def test_explicit_whole_shader_uses_lossless_pages_on_api_transport(tmp_path, monkeypatch):
    from mcp.types import CallToolResult, TextContent
    from replaylab_agent.deep_engine import _mcp_result_payload
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    source = "float3 surfaceColor; // 中文😀\r\n" * 12000
    # Host emits ASCII JSON for legacy Windows transport; decoding must still
    # verify the actual UTF-8 source bytes and retain the entire result.
    encoded = json.dumps(_complete_shader_payload(source), ensure_ascii=True)
    result = CallToolResult(content=[TextContent(type="text", text=encoded)],
                            structuredContent={"result": encoded}, isError=False)
    guarded = guard_mcp_call_result(result, "read_shader_workspace_file")
    assert guarded is result
    store = OwnedOutputStore()
    try:
        content = _mcp_result_payload(guarded, "openai-responses", "read_shader_workspace_file", store=store)
        pointer = json.loads(content)
        assert len(content.encode("utf-8")) < MAX_INLINE_TOOL_OUTPUT_BYTES
        recovered = ""
        offset = 0
        while True:
            page = store.read({"path": pointer["path"], "offset": offset, "max_bytes": 20480})
            recovered += page["content"]
            offset += page["returnedBytes"]
            if not page["truncated"]:
                break
        assert json.loads(recovered)["text"] == source
        assert offset == pointer["byteSize"]
    finally:
        store.close()
    assert not list((tmp_path / "outputs").rglob("*.json"))


def test_large_embedded_text_is_paged_without_leaving_a_duplicate_resource_block(tmp_path, monkeypatch):
    from mcp.types import CallToolResult, EmbeddedResource, TextResourceContents, ImageContent
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    source = "float4 color;\n" * 30000
    result = CallToolResult(content=[
        EmbeddedResource(type="resource", resource=TextResourceContents(
            uri="file:///source.hlsl", text=source, mimeType="text/x-hlsl", _meta={"revision": 3})),
        ImageContent(type="image", data="AAAA", mimeType="image/png"),
    ])
    store = OwnedOutputStore()
    try:
        guarded = guard_mcp_call_result(result, "read_resource", store=store)
        assert [item.type for item in guarded.content] == ["text", "image"]
        pointer = json.loads(guarded.content[0].text)
        saved = json.loads(Path(pointer["path"]).read_text(encoding="utf-8"))["resource"]
        assert saved["text"] == source and saved["mimeType"] == "text/x-hlsl"
        assert saved["_meta"] == {"revision": 3}
        assert len(guarded.model_dump_json().encode("utf-8")) < MAX_INLINE_TOOL_OUTPUT_BYTES
    finally:
        store.close()


@pytest.mark.parametrize("change", ["range", "wrong-hash", "partial", "extra-data", "oversize"])
def test_full_source_exception_rejects_incomplete_or_unbounded_shapes(tmp_path, monkeypatch, change):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    payload = _complete_shader_payload("x" * (4 * 1024 * 1024 + 1 if change == "oversize" else 100000))
    if change == "range": payload["mode"] = "range"
    if change == "wrong-hash": payload["sha256"] = "0" * 64
    if change == "partial": payload["coverage"]["complete"] = False
    if change == "extra-data": payload["otherText"] = "large" * 10000
    text = json.dumps(payload)
    assert not is_complete_shader_file_output("read_shader_workspace_file", text)
    assert json.loads(guard_tool_output_text("read_shader_workspace_file", text))["schema"] == "replaylab-tool-output-pointer/v1"


@pytest.mark.parametrize("tool,authority", [
    ("external__read_shader_workspace_file", "first-party-host"),
    ("read_shader_workspace_file", "user-configured-mcp-server"),
    ("invoke_bread_tool", "first-party-host"),
])
def test_whole_source_claim_does_not_grant_arbitrary_tools_an_exception(tmp_path, monkeypatch, tool, authority):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    text = json.dumps({**_complete_shader_payload("x" * 100000), "trusted": True})
    result = json.loads(guard_tool_output_text(tool, text, authority=authority))
    assert result["schema"] == "replaylab-tool-output-pointer/v1"


@pytest.mark.parametrize("tool", ["read_shader_workspace_file", "begin_shader_workspace_organization"])
def test_spilled_shader_receipt_keeps_full_owned_copy_and_import_cas(tmp_path, monkeypatch, tool):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    agent_file = {"path": "D:/" + "folder/" * 300 + "proposal.hlsl",
                  "relativePath": "proposal.hlsl", "virtualPath": "/proposal.hlsl",
                  "sizeBytes": 300000, "sha256": "a" * 64, "sourceSha256": "a" * 64,
                  "sourceWorkspacePath": "D:/shader", "sourceRelativePath": "ai/proposal.hlsl",
                  "organizationId": "draft-1", "expectedProposalSha256": "a" * 64,
                  "workingCopy": True, "replayAuthority": False}
    text = json.dumps({"workspacePath": "D:/shader", "organizationId": "draft-1",
                       "agentFile": agent_file, "text": "source" * 20000, "applied": False})
    guarded = guard_tool_output_text(tool, text)
    pointer = json.loads(guarded)
    assert len(guarded.encode("utf-8")) < MAX_INLINE_TOOL_OUTPUT_BYTES
    assert pointer["receipt"]["agentFile"] == agent_file
    assert pointer["applied"] is False


@pytest.mark.parametrize("tool", ["begin_shader_workspace_organization", "write_shader_workspace_organization_file"])
def test_spilled_draft_receipt_keeps_current_review_observations(tmp_path, monkeypatch, tool):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    observations = {"sourceSha256": "b" * 64, "candidateCount": 17,
                    "candidates": [{"function": "EvaluateLight", "line": 32, "example": "float lightX = weight * color.x;"}],
                    "diagnostic": "facts only", "details": "x" * 2500}
    editing = {"tool": "write_shader_workspace_organization_file", "proposalSha256": "b" * 64,
               "availableInputs": ["edits", "source_file", "content"], "draftOnly": True}
    payload = {"workspacePath": "D:/shader", "organizationId": "draft-1",
               "sha256": "b" * 64, "previousProposalSha256": "a" * 64, "textEditsApplied": 3,
               "proposalEditing": editing,
               "sourceReviewObservations": observations,
               "sourceReviewScope": {"initialFunctions": ["Main"], "currentFunctions": ["Main"]},
               "unrelatedLargeEvidence": "x" * 40000, "applied": False}
    guarded = guard_tool_output_text(tool, json.dumps(payload))
    pointer = json.loads(guarded)
    assert pointer["receipt"]["sourceReviewObservations"] == observations
    assert pointer["receipt"]["proposalEditing"] == editing
    assert pointer["receipt"]["sourceReviewScope"] == payload["sourceReviewScope"]
    assert pointer["receipt"]["sha256"] == "b" * 64
    assert pointer["receipt"]["previousProposalSha256"] == "a" * 64
    assert pointer["receipt"]["textEditsApplied"] == 3
    assert pointer["applied"] is False
    assert len(guarded.encode("utf-8")) < MAX_INLINE_TOOL_OUTPUT_BYTES


def test_external_payload_cannot_claim_host_review_observation_projection(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    pointer = json.loads(guard_tool_output_text("write_shader_workspace_organization_file",
        json.dumps({"sourceReviewObservations": {"text": "not Host evidence"},
                    "proposalEditing": {"draftOnly": True}, "large": "x" * 40000}),
        authority="user-configured-mcp-server"))
    assert "sourceReviewObservations" not in pointer.get("receipt", {})
    assert "proposalEditing" not in pointer.get("receipt", {})


@pytest.mark.parametrize("tool", [
    "finish_shader_workspace_organization", "validate_shader_workspace_delivery",
    "diagnose_shader_organization_candidate",
])
def test_failed_comparison_keeps_location_and_recovery_contract_beside_spill(tmp_path, monkeypatch, tool):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    mismatch = {"x": 843, "y": 514, "component": "R", "componentIndex": 0,
                "baselineValue": 0.5, "candidateValue": 0.50244140625,
                "absoluteError": 0.00244140625, "allowedAbsoluteError": 0.001}
    payload = {"ok": False, "applied": False, "sourceSha256": "a" * 64,
               "numericRecovery": {"tool": "diagnose_shader_organization_candidate",
                                   "organizationId": "draft", "publicationAllowed": False},
               "observation": {"accepted": False, "eventId": 61408,
                   "metrics": {"outputs": [{"accepted": False, "failedComponents": 27,
                       "worstMismatch": mismatch, "mismatchSamples": [mismatch] * 1000}]},
                   "baselineStability": {"checked": True, "byteExact": False, "withinTolerance": False,
                       "outputs": [{"accepted": False, "failedComponents": 3,
                                    "worstMismatch": mismatch}]}},
               "largeEvidence": "x" * 40000}
    guarded = guard_tool_output_text(tool, json.dumps(payload))
    pointer = json.loads(guarded)
    assert pointer["isError"] is True and pointer["applied"] is False
    assert pointer["receiptIsPartial"] is True and pointer["doNotRetry"] is True
    assert pointer["receipt"]["numericRecovery"] == payload["numericRecovery"]
    observation = pointer["receipt"]["observation"]
    assert observation["metrics"]["outputs"][0]["worstMismatch"] == mismatch
    assert observation["metrics"]["outputs"][0]["failedComponents"] == 27
    assert observation["metrics"]["partial"] is True
    assert observation["baselineStability"]["withinTolerance"] is False
    assert observation["baselineStability"]["outputs"][0]["failedComponents"] == 3
    assert len(guarded.encode("utf-8")) < MAX_INLINE_TOOL_OUTPUT_BYTES
    retained = json.loads(Path(pointer["path"]).read_text(encoding="utf-8"))
    assert len(retained["observation"]["metrics"]["outputs"][0]["mismatchSamples"]) == 1000


def test_external_server_cannot_claim_shader_validation_projection(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    payload = {"observation": {"accepted": True}, "numericRecovery": {"authorized": True},
               "large": "x" * 40000}
    pointer = json.loads(guard_tool_output_text("finish_shader_workspace_organization",
        json.dumps(payload), authority="user-configured-mcp-server"))
    assert "observation" not in pointer.get("receipt", {})
    assert "numericRecovery" not in pointer.get("receipt", {})


def test_spilled_debug_preview_retains_complete_trace_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    artifact = {"path": "D:/debug/trace.jsonl", "complete": True, "executionComplete": True,
                "truncationReasons": [], "steps": 3483, "byteSize": 700000,
                "sha256": "a" * 64, "summaryOffset": 690000,
                "index": [{"step": 2000, "offset": 400000}]}
    pointer = json.loads(guard_tool_output_text("debug_pixel",
        json.dumps({"traceArtifact": artifact, "trace": "x" * 40000, "traceTruncated": True})))
    assert pointer["receipt"]["traceArtifact"] == artifact
    assert pointer["receiptIsPartial"] is True
    assert pointer["doNotRetry"] is True


def test_nested_delivery_failure_keeps_recovery_and_observation_inline(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    recovery = {"tool": "diagnose_shader_organization_candidate",
                "arguments": {"workspace_path": "D:/workspace", "expected_source_sha256": "a" * 64}}
    payload = {"ok": False, "delivery": {"complete": False, "numericRecovery": recovery,
        "observation": {"accepted": False, "diagnosticCode": "baseline-replay-unstable",
                        "candidateDifferenceConclusive": False,
                        "replayMethod": "sequential-replay",
                        "pairedReplayUnavailable": "unsupported attachment: " + "details " * 32,
                        "metrics": {"outputs": [{"failedComponents": 3}]}},
        "largeEvidence": "x" * 40000}}
    pointer = json.loads(guard_tool_output_text("validate_shader_workspace_delivery", json.dumps(payload)))
    assert pointer["receipt"]["numericRecovery"] == recovery
    assert pointer["receipt"]["observation"]["candidateDifferenceConclusive"] is False
    assert pointer["receipt"]["observation"]["diagnosticCode"] == "baseline-replay-unstable"
    assert pointer["receipt"]["observation"]["pairedReplayUnavailable"] == payload["delivery"]["observation"]["pairedReplayUnavailable"]
    assert pointer["receipt"]["observation"]["replayMethod"] == "sequential-replay"


@pytest.mark.parametrize("authority", ["first-party-host", "user-configured-mcp-server"])
def test_spilled_begin_keeps_asset_preparation_without_promoting_unverified_mapping(tmp_path, monkeypatch, authority):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    preparation = {"schema": "bread-shader-asset-naming-preparation/v1", "state": "recorded-evidence",
                   "assetRootConfigured": True, "assetRootAvailable": True, "assetRoot": "D:/解包资源",
                   "target": {"eventId": 11402, "stage": "fragment", "rawSha256": "a" * 64},
                   "retainedMapping": {"recordPath": "D:/project/ai/asset-resolution.json",
                                       "contentFreshness": "not-checked", "sourceNamesApplied": "not-checked",
                                       "liveAliasesApplied": "not-checked"}}
    observations = {"sourceSha256": "b" * 64, "details": "x" * 5900,
                    "complete": False, "catalog": {
                        "relativePath": "ai/organization-sessions/draft/source-review-catalog.json",
                        "sourceSha256": "b" * 64, "sha256": "c" * 64,
                        "complete": True, "candidateCount": 49, "bytes": 31675}}
    agent_file = {"path": "D:/owned/organized.hlsl", "relativePath": "organized.hlsl",
                  "virtualPath": "/organized.hlsl", "sha256": "b" * 64, "sizeBytes": 300000,
                  "workingCopy": True, "replayAuthority": False}
    guarded = guard_tool_output_text("begin_shader_workspace_organization",
        json.dumps({"assetNamingPreparation": preparation, "sourceReviewObservations": observations,
                    "agentFile": agent_file, "unrelated": "x" * 40000}), authority=authority)
    pointer = json.loads(guarded)
    if authority == "first-party-host":
        assert pointer["receipt"]["assetNamingPreparation"] == preparation
        assert pointer["receipt"]["sourceReviewObservations"] == observations
        assert pointer["receipt"]["agentFile"]["path"] == agent_file["path"]
    else:
        assert "assetNamingPreparation" not in pointer.get("receipt", {})
    assert len(guarded.encode("utf-8")) < MAX_INLINE_TOOL_OUTPUT_BYTES


def test_large_copy_metadata_cannot_evict_source_observations(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    agent_file = {"path": "D:/" + "x" * 3970, "relativePath": "y" * 3970,
                  "virtualPath": "/" + "z" * 3970, "sourceWorkspacePath": "D:/" + "w" * 3970,
                  "sha256": "a" * 64, "expectedProposalSha256": "a" * 64,
                  "sizeBytes": 300000, "workingCopy": True, "replayAuthority": False}
    observations = {"sourceSha256": "a" * 64, "details": "x" * 5900}
    editing = {"tool": "write_shader_workspace_organization_file", "proposalSha256": "a" * 64,
               "availableInputs": ["edits", "source_file", "content"], "draftOnly": True,
               "atomicEdits": {"fields": ["old_text", "new_text"], "requiresCurrentProposalHash": True,
                               "hashArgument": "expected_proposal_sha256", "uniqueSequentialMatches": True,
                               "requiresRestructureAnalysis": False},
               "sourceFile": {"scope": "current-run-owned-agent-workspace", "acceptsArbitraryPaths": False}}
    payload = {"workspacePath": "D:/shader", "organizationId": "draft-1", "agentFile": agent_file,
               "sourceReviewObservations": observations, "sourceSha256": "a" * 64,
               "proposalEditing": editing,
               "large": "x" * 40000}
    guarded = guard_tool_output_text("begin_shader_workspace_organization", json.dumps(payload))
    pointer = json.loads(guarded)
    assert pointer["receipt"]["sourceReviewObservations"] == observations
    assert pointer["receipt"]["proposalEditing"] == editing
    for field in ("path", "relativePath", "virtualPath", "sha256", "expectedProposalSha256"):
        assert pointer["receipt"]["agentFile"][field] == agent_file[field]
    assert len(guarded.encode("utf-8")) < MAX_INLINE_TOOL_OUTPUT_BYTES


@pytest.mark.parametrize("tool", ["propose_shader_names_from_asset",
    "submit_shader_asset_resolution", "retrieve_shader_asset_candidates",
    "begin_shader_workspace_organization"])
@pytest.mark.parametrize("large_dictionary", [False, True])
def test_material_context_survives_large_candidate_catalog_without_promoting_aliases(
        tmp_path, monkeypatch, tool, large_dictionary):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    context_path = "D:/project/workspaces/shader/ai/asset-resolution.json"
    parameters = [{"name": "WetRoughness", "value": 0.9573, "type": "float"}]
    if large_dictionary:
        parameters *= 600
    authority = {"semanticOnly": True, "allowsPhysicalAlias": False}
    reuse = {"scope": "shader-context-only", "reason": "different-draw"}
    summary = {"schema": "bread-shader-material-semantic-context-summary/v1",
               "contextPath": context_path, "parameterCount": len(parameters),
               "candidateCount": 0, "unlocatedCount": len(parameters),
               "binding": {"eventId": 11402, "rawSha256": "a" * 64},
               "reuse": reuse, "authority": authority}
    context = {"schema": "bread-shader-material-semantic-context/v1",
               "parameterDictionary": parameters, "unlocatedParameterNames": ["WetRoughness"],
               "candidateCorrespondences": [], "agentHypotheses": [],
               "reuse": reuse, "authority": authority}
    payload = {"materialSemanticContext": context, "materialSemanticContextSummary": summary,
               "materialSemanticContextPath": context_path, "candidateEvidence": "x" * 40000}
    guarded = guard_tool_output_text(tool, json.dumps(payload))
    pointer = json.loads(guarded)
    receipt = pointer["receipt"]
    assert receipt["materialSemanticContextSummary"] == summary
    assert receipt["materialSemanticContextPath"] == context_path
    assert pointer["doNotRetry"] is True and pointer["receiptIsPartial"] is True
    if large_dictionary:
        assert "materialSemanticContext" not in receipt
    else:
        assert receipt["materialSemanticContext"] == context
    # The paged result retains every name/value and its uncertainty unchanged.
    assert json.loads(Path(pointer["path"]).read_text(encoding="utf-8")) == payload
    assert len(guarded.encode("utf-8")) < MAX_INLINE_TOOL_OUTPUT_BYTES


@pytest.mark.parametrize("tool,authority", [
    ("external__propose_shader_names_from_asset", "first-party-host"),
    ("propose_shader_names_from_asset", "user-configured-mcp-server"),
])
def test_external_result_cannot_claim_material_context_projection(tmp_path, monkeypatch, tool, authority):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    payload = {"materialSemanticContextSummary": {"authority": {"semanticOnly": False}},
               "materialSemanticContextPath": "D:/foreign/context.json", "large": "x" * 40000}
    pointer = json.loads(guard_tool_output_text(tool, json.dumps(payload), authority=authority))
    assert "materialSemanticContextSummary" not in pointer.get("receipt", {})
    assert "materialSemanticContextPath" not in pointer.get("receipt", {})


def test_begin_keeps_material_context_route_when_preparation_is_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    context_path = "D:/project/workspaces/shader/ai/asset-resolution.json"
    summary = {"schema": "bread-shader-material-semantic-context-summary/v1",
               "contextPath": context_path, "parameterCount": 80, "unlocatedCount": 20,
               "reuse": {"scope": "shader-context-only"}, "authority": {"semanticOnly": True}}
    preparation = {"materialSemanticContextSummary": summary,
                   "materialSemanticContextPath": context_path, "evidence": "x" * 5000}
    payload = {"assetNamingPreparation": preparation, "materialSemanticContextSummary": summary,
               "materialSemanticContextPath": context_path, "source": "x" * 40000}
    pointer = json.loads(guard_tool_output_text("begin_shader_workspace_organization", json.dumps(payload)))
    assert pointer["receipt"]["assetNamingPreparation"]["detailsOmitted"] is True
    assert pointer["receipt"]["materialSemanticContextSummary"] == summary
    assert pointer["receipt"]["materialSemanticContextPath"] == context_path


def test_owned_pager_reconstructs_exact_utf8_and_checks_digest(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    store = OwnedOutputStore()
    original = json.dumps({"source": '参数😀"\n' * 15000, "nextStep": "external data"}, ensure_ascii=False)
    pointer = _retain(store, original)
    content = bytearray()
    offset = 0
    while True:
        page = store.read({"path": pointer["path"], "offset": offset, "max_bytes": 20480})
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= MAX_INLINE_TOOL_OUTPUT_BYTES
        assert page["authority"] == "user-configured-mcp-server" and page["hostVerified"] is False
        assert page["sha256"] == pointer["sha256"]
        assert page["returnedBytes"] == len(page["content"].encode())
        content.extend(page["content"].encode())
        offset = page["nextOffset"]
        if not page["truncated"]:
            break
    assert bytes(content) == original.encode()
    assert hashlib.sha256(content).hexdigest() == pointer["sha256"]
    path = Path(pointer["path"])
    path.write_bytes(b"X" + path.read_bytes()[1:])
    with pytest.raises(ValueError, match="SHA-256"):
        store.read({"path": str(path)})
    store.close()
    assert not path.exists()


def test_pager_authority_is_exact_run_production_not_temp_root(tmp_path, monkeypatch):
    root = tmp_path / "outputs"
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: root)
    first, second = OwnedOutputStore(), OwnedOutputStore()
    pointer = _retain(first, "参数" * 12000)
    with pytest.raises(ValueError, match="not produced by this run"):
        second.read(pointer["readArguments"])
    forged = root / "forged.txt"
    forged.write_text("not produced")
    with pytest.raises(ValueError, match="not produced by this run"):
        first.read({"path": str(forged)})
    outside = tmp_path / "user.txt"
    outside.write_text("user file")
    assert first.read({"path": str(outside)}) is None
    with pytest.raises(ValueError, match="UTF-8"):
        first.read({"path": pointer["path"], "offset": 1})
    with pytest.raises(ValueError, match="20480"):
        first.read({"path": pointer["path"], "max_bytes": 20481})
    first.close()


def test_pager_refuses_symlink_alias_and_replaced_file(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path / "outputs")
    store = OwnedOutputStore()
    pointer = _retain(store, "X" * 30000)
    path = Path(pointer["path"])
    copy = tmp_path / "same-content.txt"
    copy.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(copy)
    except OSError:
        pytest.skip("OS does not grant test symlink creation")
    with pytest.raises(ValueError, match="symlink"):
        store.read({"path": str(path)})
    assert store.read({"path": str(copy)}) is None
    store.close()
    assert copy.is_file()


def _server():
    return {"id": "configured", "transport": "stdio", "command": sys.executable,
            "args": [str(Path(__file__).parent / "fixtures" / "fake_large_external_mcp.py")]}


async def _pages(read, pointer):
    offset, chunks = 0, []
    while True:
        value = await read({"path": pointer["path"], "offset": offset, "max_bytes": 4097})
        page = json.loads(value)
        assert page["hostVerified"] is False
        chunks.append(page["content"])
        offset = page["nextOffset"]
        if not page["truncated"]:
            return "".join(chunks)


@pytest.mark.asyncio
async def test_api_without_capture_reads_complete_external_result_once(tmp_path):
    request = RunRequest(prompt="read configured asset evidence", task_id="external-output-api",
                         profile=Profile(id="api", preset="openai", model="test"))
    config = OrchestratorConfig(workspace_base=tmp_path / "turns", external_mcp_servers=(_server(),))
    session = BreadMCPTools(request, config, tmp_path / "events")
    async with session as tools:
        pointer = json.loads(await tools["configured__large"].ainvoke({}))
        assert pointer["authority"] == "user-configured-mcp-server"
        complete = await _pages(tools["read_evidence_file"].ainvoke, pointer)
        provenance, payload = complete.split("\n", 1)
        assert json.loads(provenance)["externalMcp"]["server"] == "configured"
        decoded = json.loads(payload)
        assert decoded["calls"] == 1 and decoded["nextStep"] == "external data stays exact"
        assert "strategy" in decoded
        assert (await tools["read_evidence_file"].ainvoke({"path": str(tmp_path / "user.txt")})).startswith("ERROR:")
    assert not Path(pointer["path"]).exists()


@pytest.mark.asyncio
async def test_native_proxy_without_capture_reads_complete_external_result_once(tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    config = tmp_path / "servers.json"
    config.write_text(json.dumps([_server()]), encoding="utf-8")
    args = ["-m", "replaylab_agent.mcp_proxy", "--mcp-server-file", str(config)]
    for name in ("list_bread_capabilities", "load_bread_skill", "invoke_bread_tool", "read_evidence_file"):
        args.extend(("--allow", name))
    env = dict(os.environ, REPLAYLAB_TASK_ID="external-output-native", REPLAYLAB_TASK_EVENT_ROOT=str(tmp_path / "events"))
    with (tmp_path / "stderr.log").open("w", encoding="utf-8") as errlog:
        async with stdio_client(StdioServerParameters(command=sys.executable, args=args, env=env), errlog=errlog) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                assert "read_evidence_file" in {tool.name for tool in (await client.list_tools()).tools}
                result = await client.call_tool("invoke_bread_tool", {"name": "configured__large", "arguments": {}})
                assert not result.isError, result.content
                pointer = json.loads(result.content[0].text)
                async def read(arguments):
                    result = await client.call_tool("read_evidence_file", arguments)
                    assert not result.isError, result.content
                    return result.content[0].text
                complete = await _pages(read, pointer)
                provenance, payload = complete.split("\n", 1)
                assert json.loads(provenance)["externalMcp"]["server"] == "configured"
                decoded = json.loads(payload)
                assert decoded["calls"] == 1 and decoded["nextStep"] == "external data stays exact"
                denied = await client.call_tool("read_evidence_file", {"path": str(config)})
                assert denied.isError


@pytest.mark.asyncio
async def test_api_reads_its_actual_proxy_owned_host_result_without_cross_run_grant(tmp_path, monkeypatch):
    from replaylab_agent.native_mcp import NativeMCPRoute
    fixture = Path(__file__).parent / "fixtures" / "fake_concurrent_mcp_host.py"
    registry = tmp_path / "registry.json"
    names = {"compile_shader_workspace", "get_host_runtime_status", "read_evidence_file"}
    args = ["-m", "replaylab_agent.mcp_proxy", "--host", sys.executable,
            "--host-arg", str(fixture), "--instance-id", "fixture-instance",
            "--registry-path", str(registry), "--full-host-catalog", "--effect", "workspace"]
    for name in names:
        args.extend(("--allow", name))
    route = NativeMCPRoute(command=sys.executable, args=tuple(args),
        env={"REPLAYLAB_TASK_ID": "api-owned-host-output", "REPLAYLAB_TASK_EVENT_ROOT": str(tmp_path / "events")},
        allowed_tools=frozenset(names), effects=frozenset({"workspace"}), registry_path=registry)
    monkeypatch.setattr("replaylab_agent.deep_engine.build_native_mcp_route", lambda *args, **kwargs: route)
    request = RunRequest(prompt="compile fixture", task_id="api-owned-host-output", instance_id="fixture-instance",
                         profile=Profile(id="api", preset="openai", model="test"))
    session = BreadMCPTools(request, OrchestratorConfig(workspace_base=tmp_path / "turns"), tmp_path / "events")
    async with session as tools:
        pointer = json.loads(await tools["compile_shader_workspace"].ainvoke({"large_receipt": True}))
        located = json.loads(await tools["read_evidence_file"].ainvoke({
            "path": pointer["path"], "text_query": "receipt"}))
        assert located["textQuery"] == "receipt" and located["matches"]
        decoded = json.loads(await _pages(tools["read_evidence_file"].ainvoke, pointer))
        assert decoded["invocationCount"] == 1 and len(decoded["receipt"].encode()) == 300000
        status = json.loads(await tools["get_host_runtime_status"].ainvoke({}))
        assert status["effectCalls"] == 1
