"""Offline API construction and actual SDK transport boundaries (no network)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

from replaylab_agent.connectors import langchain_models
from replaylab_agent.connectors import openai_compatibility
from replaylab_agent.errors import ConfigurationError
from replaylab_agent.profiles import Profile
from replaylab_agent.provider_diagnostics import provider_failure_metadata


@pytest.mark.parametrize("preset,url,options,constructor", [
    ("openai", "", {}, "ChatOpenAI"),
    ("custom-openai", "https://api.invalid/v1", {}, "ChatOpenAI"),
    ("anthropic", "", {}, "ChatAnthropic"),
    ("gemini", "", {}, "ChatGoogleGenerativeAI"),
    ("vertex", "", {}, "ChatVertexAI"),
    ("azure-openai", "https://resource.openai.azure.com", {"api_version": "2025-01-01-preview"}, "AzureChatOpenAI"),
    ("azure-openai", "https://resource.openai.azure.com/openai/v1", {}, "ChatOpenAI"),
])
def test_every_api_constructor_has_bounded_request_defaults(preset, url, options, constructor):
    profile = Profile(id="test", preset=preset, model="fixture-model", base_url=url, options=options)
    with mock.patch.object(langchain_models, "_load") as load:
        langchain_models.build_chat_model(profile, environ={profile.key_env: "synthetic-key"})
    kwargs = getattr(load.return_value, constructor).call_args.kwargs
    assert kwargs["max_retries"] == 2
    if constructor in {"ChatOpenAI", "AzureChatOpenAI"}:
        assert kwargs["timeout"] == httpx.Timeout(120, read=600)
        assert kwargs["stream_chunk_timeout"] == 600
    else:
        assert kwargs["timeout"] == 120.0
    assert profile.options == options


def test_explicit_timeout_and_retry_override_remain_exact():
    profile = Profile(id="test", preset="ollama", model="fixture-model", options={"timeout_seconds": 42.5})
    with mock.patch.object(langchain_models, "_load") as load:
        langchain_models.build_chat_model(profile, max_retries=0)
    kwargs = load.return_value.ChatOpenAI.call_args.kwargs
    assert kwargs["max_retries"] == 0 and kwargs["timeout"] == 42.5
    assert kwargs["stream_chunk_timeout"] == 42.5


def test_azure_v1_constructs_real_sdk_without_legacy_version_or_deployment_prefix(monkeypatch):
    from langchain_openai import ChatOpenAI
    monkeypatch.delenv("OPENAI_API_VERSION", raising=False)
    profile = Profile(id="test", preset="azure-openai", model="deployment-name",
                      base_url="https://resource.openai.azure.com/openai/v1",
                      options={"max_output_tokens": 2048})
    model = langchain_models.build_chat_model(profile, environ={profile.key_env: "synthetic-key"})
    try:
        assert isinstance(model,ChatOpenAI)
        assert str(model.root_client.base_url) == profile.base_url + "/"
        assert model.model_name == "deployment-name"
        assert model.max_tokens == 2048
        assert model.root_client.max_retries == 0
        assert model._bread_max_retries == 2
        assert model.root_client.timeout == httpx.Timeout(120, read=600)
        assert model.stream_chunk_timeout == 600
    finally:
        model.root_client.close()
        asyncio.run(model.root_async_client.close())


@pytest.mark.parametrize("options,expected", [
    ({"thinking_budget_tokens": 16384}, 16384),
    ({"thinking_budget_tokens": 0}, 0),
    ({"thinking_budget_tokens": 16384, "stream_thinking": False}, 0),
])
def test_gemini_thinking_budget_reaches_real_request_config(options, expected):
    profile = Profile(id="test", preset="gemini", model="gemini-2.5-flash", options=options)
    model = langchain_models.build_chat_model(profile, environ={profile.key_env: "synthetic-key"})
    try:
        config = model._build_thinking_config()
        assert config.thinking_budget == expected
        assert model.max_retries == 2 and model.timeout == 120.0
        if options.get("stream_thinking") is False:
            assert config.include_thoughts is False
    finally:
        model.client.close()
        asyncio.run(model.client.aio.aclose())


@pytest.mark.parametrize("preset", ["gemini", "vertex"])
def test_unsupported_google_override_rejected_before_sdk_or_credentials(preset):
    profile = Profile(id="test", preset=preset, model="fixture-model", base_url="https://gateway.invalid")
    with mock.patch.object(langchain_models, "_load") as load, \
         mock.patch.object(Profile, "resolve_api_key", side_effect=AssertionError("must not resolve credential")):
        with pytest.raises(ConfigurationError, match="base URL"):
            langchain_models.build_chat_model(profile)
    load.assert_not_called()


def _chat_response():
    return {"id": "fixture", "object": "chat.completion", "created": 0, "model": "fixture-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"},
                         "finish_reason": "stop"}]}


def _transport_model(monkeypatch, handler):
    from langchain_openai import ChatOpenAI
    sync = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    retry_delay = openai_compatibility._retry_delay
    monkeypatch.setattr(openai_compatibility, "_retry_delay",
                        lambda *args: None if retry_delay(*args) is None else 0)

    class TestChatOpenAI(ChatOpenAI):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, http_client=sync, http_async_client=async_client)

    monkeypatch.setattr(langchain_models, "_load", lambda *args: SimpleNamespace(
        ChatOpenAI=TestChatOpenAI))
    profile = Profile(id="test", preset="custom-openai", model="fixture-model",
                      base_url="https://api.invalid/v1", options={"api": "chat"})
    model = langchain_models.build_chat_model(profile, environ={profile.key_env: "synthetic-key"})
    return model


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,attempts", [
    (401, "invalid_api_key", 1), (403, "permission_denied", 1),
    (400, "invalid_request_error", 1), (429, "insufficient_quota", 1),
    (429, "credit_balance_exhausted", 1), (429, "project_spend_limit_exceeded", 1),
    (429, "rate_limit_exceeded", 3),
    (503, "server_overloaded", 3),
])
async def test_actual_sdk_failure_retry_is_bounded_per_request(monkeypatch, status, code, attempts):
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "synthetic provider failure", "code": code}})
    model = _transport_model(monkeypatch, handler)
    try:
        with pytest.raises(Exception):
            await model.ainvoke("fixture request")
        assert len(requests) == attempts
    finally:
        model.root_client.close()
        await model.root_async_client.close()


@pytest.mark.asyncio
async def test_transient_request_recovers_without_replaying_an_agent(monkeypatch):
    count = 0
    async def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(503, json={"error": {"message": "overloaded"}}) if count < 3 else httpx.Response(200, json=_chat_response())
    model = _transport_model(monkeypatch, handler)
    try:
        assert (await model.ainvoke("fixture request")).content == "done"
        assert count == 3
    finally:
        model.root_client.close()
        await model.root_async_client.close()


@pytest.mark.asyncio
async def test_timeout_has_bounded_requests_and_preserves_original_failure(monkeypatch):
    count = 0
    async def handler(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("synthetic stalled request", request=request)
    model = _transport_model(monkeypatch, handler)
    try:
        with pytest.raises(Exception) as raised:
            await model.ainvoke("fixture request")
        assert count == 3
        assert "Timeout" in type(raised.value).__name__
    finally:
        model.root_client.close()
        await model.root_async_client.close()


@pytest.mark.asyncio
async def test_cancelled_request_exits_without_retry(monkeypatch):
    started = asyncio.Event()
    count = 0
    async def handler(request):
        nonlocal count
        count += 1
        started.set()
        await asyncio.Event().wait()
    model = _transport_model(monkeypatch, handler)
    try:
        task = asyncio.create_task(model.ainvoke("fixture request"))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert count == 1
    finally:
        model.root_client.close()
        await model.root_async_client.close()


@pytest.mark.asyncio
async def test_openai_stream_survives_gap_longer_than_sdk_default(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S", "0.02")
    monkeypatch.setattr(langchain_models, "DEFAULT_OPENAI_STREAM_IDLE_SECONDS", .5)

    class PausedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index, word in enumerate(("first", " second")):
                if index:
                    yield b": keepalive\n\n"
                    await asyncio.sleep(.06)
                chunk = {"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                         "model": "fixture-model", "choices": [{"index": 0,
                         "delta": {"role": "assistant", "content": word},
                         "finish_reason": "stop" if index else None}]}
                yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            yield b"data: [DONE]\n\n"

    async def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=PausedStream())

    model = _transport_model(monkeypatch, handler)
    try:
        assert "".join([chunk.content async for chunk in model.astream("fixture request")]) == "first second"
    finally:
        model.root_client.close()
        await model.root_async_client.close()


@pytest.mark.asyncio
async def test_stream_failure_after_output_is_not_retried(monkeypatch):
    count = 0
    class PartialStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            chunk = {"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                     "model": "fixture-model", "choices": [{"index": 0,
                     "delta": {"role": "assistant", "content": "partial"}, "finish_reason": None}]}
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            raise httpx.ReadError("synthetic stream disconnect")
    async def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=PartialStream())
    model = _transport_model(monkeypatch, handler)
    output = []
    try:
        with pytest.raises(httpx.ReadError):
            async for chunk in model.astream("fixture request"):
                output.append(chunk.content)
        assert output == ["partial"] and count == 1
    finally:
        model.root_client.close()
        await model.root_async_client.close()


@pytest.mark.parametrize("status,expected", [
    (401, "provider-authentication"), (403, "provider-permission"),
    (429, "provider-rate-limit"), (503, "provider-unavailable"),
])
def test_actual_gemini_error_shape_keeps_terminal_classification(status, expected):
    from google.genai import errors
    cls = errors.ServerError if status >= 500 else errors.ClientError
    error = cls(status, {"error": {"code": status, "message": "synthetic provider failure"}})
    metadata = provider_failure_metadata(error)
    assert metadata["errorCode"] == expected
    assert "synthetic" not in json.dumps(metadata)


@pytest.mark.parametrize("name,expected", [
    ("ResourceExhausted", "provider-rate-limit"),
    ("Unauthenticated", "provider-authentication"),
])
def test_actual_vertex_error_shape_keeps_terminal_classification(name, expected):
    from google.api_core import exceptions
    error = getattr(exceptions, name)("synthetic provider failure")
    assert provider_failure_metadata(error)["errorCode"] == expected


@pytest.mark.parametrize("error_type", [httpx.ReadError, httpx.WriteError])
def test_stream_network_error_is_distinct_from_provider_rejection(error_type):
    metadata = provider_failure_metadata(error_type("synthetic connection failure"))
    assert metadata["errorCode"] == "provider-transport" and metadata["retryable"] is True
