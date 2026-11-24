from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from replaylab_agent.connectors.acp import (
    ACPConnector, _acp_provider_request_failure, _new_session, _read_capture_tail, _select_acp_model, _wait_for,
)
from replaylab_agent.errors import AuthenticationError, ConnectorError, ProviderCapabilityError, ProviderRequestError


@pytest.mark.asyncio
async def test_current_sdk_places_vendor_metadata_at_wire_root() -> None:
    pytest.importorskip("acp")
    from acp.client.connection import ClientSideConnection

    sent = AsyncMock(return_value={"sessionId": "new-session"})
    connection = object.__new__(ClientSideConnection)
    connection._conn = SimpleNamespace(send_request=sent)
    metadata = {"claudeCode": {"options": {"tools": ["Skill"], "strictMcpConfig": True}}}
    await _new_session(connection, cwd="/owned", mcp_servers=[], meta=metadata, timeout=1)
    sent.assert_awaited_once()
    method, payload = sent.call_args.args
    assert method == "session/new"
    assert payload["_meta"] == metadata
    assert "field_meta" not in payload["_meta"]


@pytest.mark.asyncio
async def test_explicit_legacy_metadata_parameter_is_selected_before_request() -> None:
    received = []

    class Connection:
        async def new_session(self, cwd, mcp_servers, meta=None):
            received.append(meta)
            return {"sessionId": "new-session"}

    await _new_session(Connection(), cwd="/owned", mcp_servers=[], meta={"vendor": {}}, timeout=1)
    assert received == [{"vendor": {}}]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("unexpected validation failure"), TypeError("invalid extra result")])
async def test_provider_session_failure_is_never_resent(error) -> None:
    sent = []

    class Connection:
        async def new_session(self, cwd, mcp_servers, **metadata):
            sent.append(metadata)
            raise error

    with pytest.raises(type(error), match=str(error)):
        await _new_session(Connection(), cwd="/owned", mcp_servers=[], meta={"vendor": {}}, timeout=1)
    assert sent == [{"vendor": {}}]


@pytest.mark.asyncio
async def test_unknown_model_interface_cannot_silently_use_vendor_default() -> None:
    with pytest.raises(ProviderCapabilityError) as raised:
        await _select_acp_model(object(), {}, "session", "requested", timeout=1)
    assert raised.value.error_code == "model-selection-unsupported"
    assert raised.value.retryable is False


@pytest.mark.asyncio
async def test_exact_legacy_current_model_needs_no_mutation() -> None:
    assert await _select_acp_model(
        object(), {"models": {"currentModelId": "chosen"}}, "session", "chosen", timeout=1
    ) == "chosen"


@pytest.mark.asyncio
async def test_claude_environment_requires_actual_requested_binding() -> None:
    with pytest.raises(ProviderCapabilityError):
        await _select_acp_model(object(), {}, "session", "chosen", timeout=1,
                                provider_id="claude-code-acp", env={"ANTHROPIC_MODEL": "other"})
    assert await _select_acp_model(object(), {}, "session", "Chosen", timeout=1,
                                   provider_id="claude-code-acp", env={"ANTHROPIC_MODEL": "chosen"}) == "Chosen"


@pytest.mark.asyncio
async def test_model_setter_cannot_report_a_different_model_as_success() -> None:
    session = {"configOptions": [{"id": "model", "currentValue": "other"}]}
    connection = SimpleNamespace(set_config_option=AsyncMock(return_value=session))
    with pytest.raises(ConnectorError, match="did not select"):
        await _select_acp_model(connection, session, "session", "chosen", timeout=1)
    connection.set_config_option.assert_awaited_once()


def _claude_model_options(current="sonnet"):
    # Public config shape from claude-agent-acp 0.76.0 buildModelConfigOption.
    # The adapter keeps resolvedModel private; it returns the picker value even
    # after accepting a full ID through setSessionConfigOption.
    return [{"id": "model", "name": "Model", "category": "model", "type": "select",
             "currentValue": current,
             "options": [{"value": value, "name": value} for value in
                         ["default", "opus[1m]", "claude-fable-5-1[1m]", "sonnet", "haiku"]]}]


@pytest.mark.asyncio
async def test_claude_full_id_accepts_adapter_confirmed_public_alias() -> None:
    schema = pytest.importorskip("acp.schema")
    session = schema.NewSessionResponse(sessionId="session", configOptions=_claude_model_options())
    response = schema.SetSessionConfigOptionResponse(configOptions=_claude_model_options())
    connection = SimpleNamespace(set_config_option=AsyncMock(return_value=response))
    selected = await _select_acp_model(
        connection, session, "session", "claude-sonnet-5", timeout=1,
        provider_id="claude-code-acp", env={"ANTHROPIC_MODEL": "claude-sonnet-5"},
    )
    assert selected == "sonnet"
    connection.set_config_option.assert_awaited_once_with(
        config_id="model", session_id="session", value="claude-sonnet-5")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,environment,reported", [
    ("custom-acp", "claude-sonnet-5", "sonnet"),
    ("cursor-acp", "claude-sonnet-5", "sonnet"),
    ("claude-code-acp", "other-model", "sonnet"),
    ("claude-code-acp", "claude-sonnet-5", "unadvertised-model"),
])
async def test_claude_alias_contract_does_not_relax_other_selections(provider, environment, reported):
    session = {"configOptions": _claude_model_options()}
    response = {"configOptions": _claude_model_options(reported)}
    connection = SimpleNamespace(set_config_option=AsyncMock(return_value=response))
    with pytest.raises(ConnectorError, match="did not select"):
        await _select_acp_model(connection, session, "session", "claude-sonnet-5", timeout=1,
                                provider_id=provider, env={"ANTHROPIC_MODEL": environment})


@pytest.mark.asyncio
async def test_claude_environment_does_not_hide_a_model_switch_hook_rejection():
    connection = SimpleNamespace(set_config_option=AsyncMock(
        side_effect=RuntimeError("Model switch blocked by a PreModelSwitch hook")))
    with pytest.raises(ConnectorError, match="PreModelSwitch"):
        await _select_acp_model(connection, {"configOptions": _claude_model_options()},
                                "session", "claude-sonnet-5", timeout=1,
                                provider_id="claude-code-acp", env={"ANTHROPIC_MODEL": "claude-sonnet-5"})


@pytest.mark.asyncio
@pytest.mark.parametrize("message,is_auth", [("Authentication required", True), ("Server capacity unavailable", False)])
async def test_auth_required_is_distinct_from_generic_server_code(message, is_auth) -> None:
    class RPCError(Exception):
        code = -32000

    async def reject():
        raise RPCError(message)

    expected = AuthenticationError if is_auth else RPCError
    with pytest.raises(expected):
        await _wait_for(reject(), 1, "ACP session/new")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,text", [("refusal", "refused"), ("cancelled", "partial"), ("end_turn", ""), ("max_tokens", "partial")])
async def test_model_probe_never_claims_incomplete_generation_succeeded(tmp_path, monkeypatch, reason, text):
    import replaylab_agent.connectors.acp as module
    from test_agent_acp_session import _fake_sdk

    holder = {}

    async def script(connection):
        if text:
            await connection.say(text)
        return {"stopReason": reason}

    monkeypatch.setattr(module, "_load_acp_sdk", lambda: _fake_sdk(script, holder))
    with pytest.raises(ConnectorError, match="completed text response"):
        await ACPConnector(["fake"], cwd=tmp_path).test_model()
    assert holder["manager"].closed


@pytest.mark.asyncio
async def test_model_probe_selects_requested_model_before_prompt(tmp_path, monkeypatch):
    import replaylab_agent.connectors.acp as module
    from test_agent_acp_session import _Connection, _fake_sdk

    holder = {}
    selected = []

    async def setter(self, **arguments):
        selected.append(arguments)

    async def script(connection):
        assert selected == [{"config_id": "models", "session_id": "sess-1", "value": "chosen"}]
        await connection.say("OK")
        return {"stopReason": "end_turn"}

    monkeypatch.setattr(module, "_load_acp_sdk", lambda: _fake_sdk(script, holder))
    monkeypatch.setattr(_Connection, "config_options", ({"id": "models", "currentValue": "other"},))
    monkeypatch.setattr(_Connection, "set_config_option", setter, raising=False)
    result = await ACPConnector(["fake"], cwd=tmp_path).test_model(model="chosen")
    assert result == {"ok": True, "stopReason": "end_turn", "model": "chosen"}
    assert holder["manager"].closed


@pytest.mark.asyncio
async def test_claude_model_probe_preserves_requested_and_reported_id(tmp_path, monkeypatch):
    import replaylab_agent.connectors.acp as module
    from test_agent_acp_session import _Connection, _fake_sdk

    holder = {}

    async def setter(self, **arguments):
        assert arguments["value"] == "claude-sonnet-5"
        return {"configOptions": _claude_model_options()}

    async def script(connection):
        await connection.say("OK")
        return {"stopReason": "end_turn"}

    monkeypatch.setattr(module, "_load_acp_sdk", lambda: _fake_sdk(script, holder))
    monkeypatch.setattr(_Connection, "config_options", tuple(_claude_model_options()))
    monkeypatch.setattr(_Connection, "set_config_option", setter, raising=False)
    connector = ACPConnector(["fake"], cwd=tmp_path, provider_id="claude-code-acp",
                             env={"ANTHROPIC_MODEL": "claude-sonnet-5"})
    result = await connector.test_model(model="claude-sonnet-5")
    assert result == {"ok": True, "stopReason": "end_turn", "model": "sonnet",
                      "requestedModel": "claude-sonnet-5"}


def test_stderr_tail_reads_only_the_bounded_tail() -> None:
    class BoundedFile(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= 64
            return super().read(size)

    source = BoundedFile(b"x" * 100_000 + b"\nstartup failed\n")
    assert _read_capture_tail(source, limit=64).endswith("startup failed")


@pytest.mark.parametrize("kind,code,retryable", [
    ("authentication_failed", "provider-authentication", False),
    ("oauth_org_not_allowed", "provider-permission", False),
    ("account_on_hold", "provider-quota", False),
    ("billing_error", "provider-quota", False),
    ("rate_limit", "provider-rate-limit", True),
    ("overloaded", "provider-capacity", True),
    ("invalid_request", "provider-configuration", False),
    ("model_not_found", "provider-model-unavailable", False),
    ("max_output_tokens", "provider-context", False),
    ("server_error", "provider-unavailable", True),
    ("transport_lost", "provider-transport", True),
])
def test_typed_claude_rpc_category_survives_generic_sdk_error(kind, code, retryable):
    RequestError = pytest.importorskip("acp.exceptions").RequestError
    failure = _acp_provider_request_failure(
        RequestError(-32603, "Internal error", {"errorKind": kind}), provider_id="claude-code-acp")
    assert failure.error_code == code
    assert failure.retryable is retryable


@pytest.mark.parametrize("status,kind,code", [
    (401, None, "provider-authentication"),
    (403, "authentication_failed", "provider-permission"),
    (402, None, "provider-quota"),
    (429, "billing_error", "provider-quota"),
    (429, None, "provider-rate-limit"),
    (503, None, "provider-unavailable"),
    (504, None, "provider-timeout"),
    (404, "model_not_found", "provider-model-unavailable"),
])
def test_known_claude_rpc_api_wrapper_preserves_http_failure(status, kind, code):
    RequestError = pytest.importorskip("acp.exceptions").RequestError
    inner = RequestError(-32603, f"Internal error: Failed to authenticate. API Error: {status} Request not allowed",
                         {"errorKind": kind})
    wrapper = ConnectorError("model selection failed")
    wrapper.__cause__ = inner
    failure = _acp_provider_request_failure(wrapper, provider_id="claude-code-acp")
    assert failure.error_code == code


@pytest.mark.parametrize("provider,rpc_code,message,real_rpc", [
    ("custom-acp", -32603, "Internal error: API Error: 403 Request not allowed", True),
    ("claude-code-acp", -32602, "Internal error: API Error: 403 Request not allowed", True),
    ("claude-code-acp", -32603, "Tool output: API Error: 403 Request not allowed", True),
    ("claude-code-acp", -32603, "Internal error: API Error: 403 Request not allowed", False),
])
def test_untyped_or_unrelated_text_does_not_become_provider_failure(provider, rpc_code, message, real_rpc):
    RequestError = pytest.importorskip("acp.exceptions").RequestError
    error = RequestError(rpc_code, message) if real_rpc else RuntimeError(message)
    assert _acp_provider_request_failure(error, provider_id=provider) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["model-test", "main-session"])
async def test_actual_claude_403_rpc_shape_reports_generation_permission_not_success(tmp_path, monkeypatch, route):
    import replaylab_agent.acp_session as main
    import replaylab_agent.connectors.acp as module
    from replaylab_agent.provider_diagnostics import provider_failure_metadata
    from test_agent_acp_session import _fake_sdk

    RequestError = pytest.importorskip("acp.exceptions").RequestError
    original = RequestError(-32603, "Internal error: Failed to authenticate. API Error: 403 Request not allowed")
    holder = {}

    async def script(connection):
        raise original

    sdk = lambda: _fake_sdk(script, holder)
    monkeypatch.setattr(module, "_load_acp_sdk", sdk)
    monkeypatch.setattr(main, "_load_acp_sdk", sdk)
    with pytest.raises(ProviderRequestError) as raised:
        if route == "model-test":
            await ACPConnector(["fake"], cwd=tmp_path, provider_id="claude-code-acp").test_model()
        else:
            async with main.ACPMainAgentSession(command=["fake"], cwd=tmp_path, env={},
                    allowed_tools=frozenset(), provider_id="claude-code-acp") as session:
                await session.prompt("hello")
    assert raised.value.error_code == "provider-permission"
    assert raised.value.__cause__ is original
    assert provider_failure_metadata(raised.value)["errorMessageId"] == "task.error.provider-permission"
    assert holder["manager"].closed
    assert sum(name == "prompt" for name, _ in holder["connection"].calls) == 1
