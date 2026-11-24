"""Operation failure stays visible without aborting or replaying an Agent turn."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from replaylab_agent.connectors.acp import _ACPClientState, _handle_public_timeline_update
from replaylab_agent.connectors.codex_app_server import _TimelineState, _item_timeline_event
from replaylab_agent.deep_engine import _mcp_result_payload
from replaylab_agent.tool_output import (
    MAX_INLINE_TOOL_OUTPUT_BYTES, OwnedOutputStore, guard_mcp_call_result, mcp_result_failed,
)


def finish_failure():
    # The actual failed compile receipt has a reason rather than an error key.
    return {"schema": "replaylab-shader-organization-finish/v1", "ok": False,
            "applied": False, "promoted": False, "draftPreserved": True,
            "previousSourceRestored": True, "reason": "undeclared identifier '_9530'",
            "state": "finish-failed-draft-preserved",
            "blockingConditions": ["finish-validation-failed"]}


def mcp_result(payload):
    text = json.dumps(payload)
    return CallToolResult(content=[TextContent(type="text", text=text)],
                          structuredContent={"result": text}, isError=False)


@pytest.mark.parametrize("payload,failed", [
    (finish_failure(), True),
    ({"ok": False, "state": "source-review-pending", "recoverable": True}, True),
    ({"ok": True, "history": [finish_failure()], "result": {"ok": False}}, False),
    ({"ok": True, "state": "completed", "diagnostics": "old error: compile failed"}, False),
    ({"pending": True, "state": "running"}, False),
    ({"ok": 0}, False),
])
def test_current_receipt_only_controls_mcp_and_api_outcome(payload, failed):
    raw = mcp_result(payload)
    guarded = guard_mcp_call_result(raw, "finish_shader_workspace_organization")
    assert guarded.isError is failed
    assert guarded.content == raw.content
    assert guarded.structuredContent == raw.structuredContent
    assert raw.isError is False
    api_text = _mcp_result_payload(raw, "openai-responses", "finish_shader_workspace_organization")
    assert api_text.startswith("ERROR: ") is failed
    assert json.loads(api_text.removeprefix("ERROR: ")) == payload


def test_external_server_business_fields_are_not_reinterpreted():
    raw = mcp_result(finish_failure())
    assert guard_mcp_call_result(raw, "other_tool", authority="user-configured-mcp-server") is raw
    api_text = _mcp_result_payload(raw, "openai-chat", "other_tool", authority="user-configured-mcp-server")
    assert not api_text.startswith("ERROR: ")


def test_mcp_multiblock_history_does_not_become_current_failure():
    raw = CallToolResult(content=[TextContent(type="text", text=json.dumps(finish_failure())),
                                 TextContent(type="text", text="Historical receipt example")])
    assert not mcp_result_failed(raw)


def test_oversized_failed_receipt_keeps_error_and_exact_diagnostic(tmp_path, monkeypatch):
    payload = {**finish_failure(), "diagnostics": "error detail " * MAX_INLINE_TOOL_OUTPUT_BYTES}
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path)
    store = OwnedOutputStore()
    guarded = guard_mcp_call_result(mcp_result(payload), "finish_shader_workspace_organization", store=store)
    assert guarded.isError is True
    pointer = json.loads(guarded.content[0].text)
    assert pointer["isError"] is True
    assert pointer["doNotRetry"] is True
    assert json.loads(Path(pointer["path"]).read_text(encoding="utf-8")) == payload


def test_oversized_protocol_error_keeps_failure_when_content_is_plain_text(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.tool_output._tool_output_root", lambda: tmp_path)
    text = "diagnostic " * MAX_INLINE_TOOL_OUTPUT_BYTES
    raw = CallToolResult(content=[TextContent(type="text", text=text)], isError=True)
    guarded = guard_mcp_call_result(raw, "other_tool", store=OwnedOutputStore(),
                                    authority="user-configured-mcp-server")
    pointer = json.loads(guarded.content[0].text)
    assert guarded.isError is True and pointer["isError"] is True
    assert Path(pointer["path"]).read_text(encoding="utf-8") == text


@pytest.mark.parametrize("payload,expected", [(finish_failure(), "failed"),
    ({"ok": True, "history": [finish_failure()]}, "completed")])
def test_codex_completed_transport_reports_actual_tool_outcome(payload, expected):
    raw = mcp_result(payload).model_dump(by_alias=True)
    event = _item_timeline_event({"id": "call-1", "type": "mcpToolCall", "server": "Bread",
        "tool": "finish_shader_workspace_organization", "status": "completed", "result": raw},
        lifecycle="completed", elapsed=1, state=_TimelineState())
    assert event["status"] == expected
    assert event["lifecycle"] == "completed"
    assert event["activityId"] == "call-1"


@pytest.mark.asyncio
async def test_acp_failed_tool_does_not_abort_session_and_next_operation_can_succeed():
    events = []
    state = _ACPClientState(on_delta=None, on_timeline=events.append,
                           allowed_tools=frozenset({"finish_shader_workspace_organization"}))
    for ident, payload in [("failed", finish_failure()), ("retry-after-repair", {"ok": True})]:
        await _handle_public_timeline_update(state, {"toolCallId": ident,
            "title": "Bread.finish_shader_workspace_organization", "status": "completed",
            "rawOutput": payload}, "tool_call_update")
    assert [event["status"] for event in events] == ["failed", "completed"]
    assert not state.abort_event.is_set()
    assert state.callback_error is None
    assert state.policy_error is None

