from __future__ import annotations

import sys
from pathlib import Path

import pytest

from replaylab_agent.acp_session import ACPMainAgentSession, _main_agent_client
from replaylab_agent.connectors.acp import ACPConnector, _ACPClientState, _load_acp_sdk, _make_client
from replaylab_agent.errors import ConnectorError, PolicyError


NOTIFICATIONS = (
    "codebuddy.ai/command",
    "codebuddy.ai/artifact",
    "codebuddy.ai/checkpoint",
    "codebuddy.ai/system_init",
    "cursor/update_todos",
    "com.example/future_notification",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codebuddy-acp", "cursor-acp", "gemini-acp", "custom-acp"])
@pytest.mark.parametrize("client_kind", ["probe", "main"])
async def test_unhandled_notifications_have_no_effect(provider, client_kind, caplog):
    pytest.importorskip("acp")
    events = []
    state = _ACPClientState(on_delta=events.append, on_timeline=events.append,
                            provider_id=provider, session_id="session")
    sdk = _load_acp_sdk()
    client = (_make_client(sdk, state) if client_kind == "probe"
              else _main_agent_client(sdk, state, frozenset()))
    # Even a command-shaped payload is opaque data, never a tool execution,
    # assistant response, completion receipt, or permission grant.
    payload = {"sessionId": "session", "command": "private-command-secret",
               "tool": "report_task_result", "status": "completed",
               "stopReason": "end_turn", "text": "private-text-secret"}
    for method in NOTIFICATIONS:
        await client.ext_notification(method, payload)
    assert not state.abort_event.is_set()
    assert state.policy_error is state.callback_error is None
    assert state.permission_denials == 0
    assert not state.permission_ids
    assert not state.text_chunks
    assert not state.tool_statuses
    assert not events
    assert "private-command-secret" not in caplog.text
    assert "private-text-secret" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,method", [
    ("codebuddy-acp", "codebuddy.ai/delegateTool"),
    ("codebuddy-acp", "codebuddy.ai/uiControl"),
    ("codebuddy-acp", "cursor/create_plan"),
    ("cursor-acp", "codebuddy.ai/delegateTool"),
    ("custom-acp", "com.example/future_request"),
])
@pytest.mark.parametrize("client_kind", ["probe", "main"])
async def test_unknown_requests_return_method_not_found_without_cancelling(provider, method, client_kind):
    RequestError = pytest.importorskip("acp.exceptions").RequestError
    state = _ACPClientState(on_delta=None, provider_id=provider, session_id="session")
    sdk = _load_acp_sdk()
    client = (_make_client(sdk, state) if client_kind == "probe"
              else _main_agent_client(sdk, state, frozenset()))
    with pytest.raises(RequestError) as raised:
        await client.ext_method(method, {"command": "unavailable-command"})
    assert raised.value.code == -32601
    assert not state.abort_event.is_set()
    assert state.policy_error is None
    assert state.permission_denials == 0
    assert not state.permission_ids


@pytest.mark.asyncio
async def test_notifications_do_not_change_native_or_mcp_authorization():
    pytest.importorskip("acp")
    sdk = _load_acp_sdk()
    allowed = frozenset({"inspect_event"})
    state = _ACPClientState(on_delta=None, provider_id="codebuddy-acp",
                            session_id="session", allowed_tools=allowed)
    client = _main_agent_client(sdk, state, allowed)
    await client.ext_notification("codebuddy.ai/command", {
        "sessionId": "session", "command": "unavailable-command",
        "allow": True, "terminal": True, "fs": {"writeTextFile": True},
    })
    with pytest.raises(PolicyError):
        await client.create_terminal("session", "unavailable-command")
    with pytest.raises(PolicyError):
        await client.read_text_file("session", "unavailable-file")
    with pytest.raises(PolicyError):
        await client.write_text_file("session", "unavailable-file", "content")
    options = [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}]
    denied = await client.request_permission("session", {
        "toolCallId": "foreign", "title": "Bash", "kind": "execute",
        "rawInput": {"command": "unavailable-command"},
    }, options)
    assert denied.outcome.outcome == "cancelled"
    granted = await client.request_permission("session", {
        "toolCallId": "granted", "title": "inspect_event", "kind": "other",
        "rawInput": {"server": "Bread", "tool": "inspect_event"},
    }, options)
    assert granted.outcome.outcome == "selected"
    assert not state.abort_event.is_set()
    # A malformed permission request still cancels the turn.
    await client.request_permission("wrong-session", {
        "toolCallId": "wrong-session", "title": "inspect_event", "kind": "other",
        "rawInput": {"server": "Bread", "tool": "inspect_event"},
    }, options)
    assert state.abort_event.is_set()
    assert isinstance(state.policy_error, PolicyError)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["model-test", "main-session"])
@pytest.mark.parametrize("mode", ["extension-notifications", "extension-requests"])
async def test_extensions_over_real_sdk_and_stdio_allow_generation(tmp_path, route, mode):
    pytest.importorskip("acp")
    fixture = Path(__file__).parent / "fixtures" / "fake_acp_agent.py"
    command = [sys.executable, str(fixture), "no-mcp", mode]
    if route == "model-test":
        result = await ACPConnector(command, cwd=tmp_path, provider_id="codebuddy-acp", timeout=10).test_model()
        assert result == {"ok": True, "stopReason": "end_turn"}
    else:
        async with ACPMainAgentSession(command=command, cwd=tmp_path, env=None,
                allowed_tools=frozenset(), provider_id="codebuddy-acp", timeout=10) as session:
            for prompt in ("first", "second"):
                result = await session.prompt(prompt)
                assert result.text == "ACP: " + prompt
                assert result.stop_reason == "end_turn"
                assert result.tool_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["model-test", "main-session"])
async def test_notifications_do_not_turn_provider_refusal_into_success(tmp_path, route):
    pytest.importorskip("acp")
    fixture = Path(__file__).parent / "fixtures" / "fake_acp_agent.py"
    command = [sys.executable, str(fixture), "no-mcp", "extension-notifications", "refusal"]
    with pytest.raises(ConnectorError, match="refusal"):
        if route == "model-test":
            await ACPConnector(command, cwd=tmp_path, provider_id="codebuddy-acp", timeout=10).test_model()
        else:
            async with ACPMainAgentSession(command=command, cwd=tmp_path, env=None,
                    allowed_tools=frozenset(), provider_id="codebuddy-acp", timeout=10) as session:
                await session.prompt("hello")
