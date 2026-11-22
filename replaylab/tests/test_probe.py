from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

import httpx
import pytest

from replaylab_agent.errors import AuthenticationError, ConnectorError
from replaylab_agent.probe import (
    _openai_endpoint_candidates,
    _probe_agent_launch,
    discover_models,
    probe_profile,
)
from replaylab_agent.profiles import Profile


@pytest.mark.asyncio
async def test_openai_probe_discovers_models_without_generation(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "Bearer test-only")
    seen: dict[str, object] = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers, params):
            seen.update(url=url, headers=headers, params=params)
            request = httpx.Request("GET", url)
            return httpx.Response(
                200,
                request=request,
                json={"data": [{"id": "gpt-b"}, {"id": "gpt-a"}]},
            )

        @asynccontextmanager
        async def stream(self, method, url, *, headers, params):
            assert method == "GET"
            yield await self.get(url, headers=headers, params=params)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())
    report = await probe_profile(Profile("test", "openai", "gpt-a"))
    assert report.status == "connected"
    assert report.models == ["gpt-a", "gpt-b"]
    assert report.selected_model_available is True
    assert seen["url"] == "https://api.openai.com/v1/models"
    assert seen["headers"] == {"Authorization": "Bearer test-only"}


@pytest.mark.asyncio
async def test_external_agent_probe_launches_without_prompting() -> None:
    report = await probe_profile(
        Profile(
            "agent-test",
            "custom-acp",
            command=[sys.executable],
            options={"allow_unsandboxed_agent": True},
        )
    )
    assert report.status == "launchable"
    assert report.kind == "agent"


@pytest.mark.asyncio
async def test_external_agent_probe_discards_unread_output_and_reaps_process(
    monkeypatch,
) -> None:
    created: dict[str, object] = {}

    class FakeStdin:
        def __init__(self) -> None:
            self.closed = False
            self.waited = False

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            self.waited = True

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdin = FakeStdin()
            self.wait_calls = 0

        async def wait(self) -> int:
            self.wait_calls += 1
            return self.returncode

    process = FakeProcess()

    async def fake_create_subprocess_exec(*argv, **kwargs):
        created["argv"] = argv
        created["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    profile = Profile(
        "agent-stdio",
        "custom-acp",
        command=[sys.executable],
        options={"allow_unsandboxed_agent": True},
    )

    await _probe_agent_launch(profile, [sys.executable, "--version"], timeout=0.001)

    assert created["argv"] == (sys.executable, "--version")
    kwargs = created["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert kwargs["stdout"] == asyncio.subprocess.DEVNULL
    assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
    assert process.stdin.closed is True
    assert process.stdin.waited is True
    assert process.wait_calls == 1


@pytest.mark.asyncio
async def test_external_acp_probe_requires_explicit_process_trust() -> None:
    from replaylab_agent.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="unsandboxed OS process"):
        await probe_profile(
            Profile("agent-test", "custom-acp", command=[sys.executable])
        )


def test_gemini_probe_keeps_key_out_of_url_query() -> None:
    from replaylab_agent.probe import _model_list_requests

    requests = _model_list_requests(
        Profile("gemini-test", "gemini", "gemini-test"), "secret-key"
    )
    assert len(requests) == 1
    _, url, headers, params = requests[0]
    assert url.endswith("/v1beta/models")
    assert headers == {"x-goog-api-key": "secret-key"}
    assert params == {}


@pytest.mark.parametrize(
    "entered, expected",
    [
        (
            "https://api.example.test",
            [
                ("https://api.example.test", "https://api.example.test/models"),
                ("https://api.example.test/v1", "https://api.example.test/v1/models"),
            ],
        ),
        (
            "https://api.example.test/v1",
            [
                ("https://api.example.test/v1", "https://api.example.test/v1/models"),
                ("https://api.example.test", "https://api.example.test/models"),
            ],
        ),
        (
            "https://api.example.test/models",
            [
                ("https://api.example.test", "https://api.example.test/models"),
                ("https://api.example.test/v1", "https://api.example.test/v1/models"),
            ],
        ),
        (
            "https://api.example.test/v1/chat/completions",
            [
                ("https://api.example.test/v1", "https://api.example.test/v1/models"),
                ("https://api.example.test", "https://api.example.test/models"),
            ],
        ),
        (
            "https://api.example.test/responses",
            [
                ("https://api.example.test", "https://api.example.test/models"),
                ("https://api.example.test/v1", "https://api.example.test/v1/models"),
            ],
        ),
        (
            "https://api.example.test/v1/responses",
            [
                ("https://api.example.test/v1", "https://api.example.test/v1/models"),
                ("https://api.example.test", "https://api.example.test/models"),
            ],
        ),
        (
            "https://api.example.test/chat/completions",
            [
                ("https://api.example.test", "https://api.example.test/models"),
                ("https://api.example.test/v1", "https://api.example.test/v1/models"),
            ],
        ),
        (
            "http://127.0.0.1:11434/v1/models",
            [
                ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1/models"),
                ("http://127.0.0.1:11434", "http://127.0.0.1:11434/models"),
            ],
        ),
    ],
)
def test_openai_endpoint_candidates_are_bounded_and_same_origin(entered, expected) -> None:
    candidates = _openai_endpoint_candidates(entered)
    assert [(item.base_url, item.models_url) for item in candidates] == expected
    assert len(candidates) <= 2


@pytest.mark.asyncio
async def test_discovery_falls_back_between_bounded_v1_candidates(monkeypatch) -> None:
    calls: list[str] = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers, params):
            calls.append(url)
            assert headers == {"Authorization": "Bearer session-secret"}
            request = httpx.Request("GET", url)
            if url.endswith("/custom/models"):
                return httpx.Response(404, request=request, json={"error": "missing"})
            return httpx.Response(200, request=request, json={"data": [{"id": "m"}]})

        @asynccontextmanager
        async def stream(self, method, url, *, headers, params):
            assert method == "GET"
            yield await self.get(url, headers=headers, params=params)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())
    profile = Profile(
        "custom",
        "custom-openai",
        "m",
        base_url="https://api.example.test/custom/chat/completions",
    )
    endpoint, models, attempts = await discover_models(
        profile, api_key="Bearer session-secret"
    )
    assert endpoint == "https://api.example.test/custom/v1"
    assert models == ["m"]
    assert calls == [
        "https://api.example.test/custom/models",
        "https://api.example.test/custom/v1/models",
    ]
    assert [item["status"] for item in attempts] == [404, 200]


@pytest.mark.parametrize(
    ("base_url", "expected_trust_env"),
    [
        ("http://127.0.0.1:11434/v1", False),
        ("http://localhost:11434/v1", False),
        ("https://api.example.test/v1", True),
    ],
)
@pytest.mark.asyncio
async def test_discovery_never_uses_environment_proxy_for_loopback_credentials(
    monkeypatch, base_url, expected_trust_env
) -> None:
    client_options: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            client_options.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        @asynccontextmanager
        async def stream(self, method, url, *, headers, params):
            assert method == "GET"
            assert headers == {"Authorization": "Bearer local-secret"}
            yield httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={"data": [{"id": "model"}]},
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    profile = Profile(
        "proxy-boundary",
        "custom-openai",
        "model",
        base_url=base_url,
    )
    await discover_models(profile, api_key="local-secret", trust_env=True)
    assert client_options["trust_env"] is expected_trust_env


@pytest.mark.parametrize("auth_status", [401, 403])
@pytest.mark.asyncio
async def test_discovery_never_follows_redirect_or_retries_auth_failure(
    monkeypatch, auth_status
) -> None:
    calls: list[str] = []

    class RedirectClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers, params):
            calls.append(url)
            request = httpx.Request("GET", url)
            return httpx.Response(
                auth_status,
                request=request,
                headers={"location": "http://attacker.invalid/models"},
                json={},
            )

        @asynccontextmanager
        async def stream(self, method, url, *, headers, params):
            assert method == "GET"
            yield await self.get(url, headers=headers, params=params)

    monkeypatch.setattr(httpx, "AsyncClient", RedirectClient)
    profile = Profile(
        "custom-auth",
        "custom-openai",
        "m",
        base_url="https://api.example.test/chat/completions",
    )
    with pytest.raises(AuthenticationError) as caught:
        await discover_models(profile, api_key="session-secret")
    assert calls == ["https://api.example.test/models"]
    detail = str(caught.value)
    assert f"https://api.example.test -> {auth_status}" in detail
    assert "authentication-rejected" in detail
    assert "session-secret" not in detail


@pytest.mark.asyncio
async def test_failed_discovery_reports_each_bounded_attempt_without_key(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class MissingClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        @asynccontextmanager
        async def stream(self, method, url, *, headers, params):
            assert method == "GET"
            calls.append(url)
            status = 404 if url.endswith("/custom/models") else 405
            yield httpx.Response(
                status,
                request=httpx.Request("GET", url, headers=headers),
                json={"error": "missing"},
            )

    monkeypatch.setattr(httpx, "AsyncClient", MissingClient)
    profile = Profile(
        "custom-missing",
        "custom-openai",
        "model",
        base_url="https://api.example.test/custom/chat/completions",
    )
    with pytest.raises(ConnectorError) as caught:
        await discover_models(profile, api_key="must-not-appear")

    assert calls == [
        "https://api.example.test/custom/models",
        "https://api.example.test/custom/v1/models",
    ]
    detail = str(caught.value)
    assert "1) https://api.example.test/custom -> 404 (HTTP 404)" in detail
    assert "2) https://api.example.test/custom/v1 -> 405 (HTTP 405)" in detail
    assert "must-not-appear" not in detail


@pytest.mark.asyncio
async def test_chunked_model_discovery_stops_at_four_mib_without_full_buffer(
    monkeypatch,
) -> None:
    yielded = 0
    calls = 0

    class ChunkedResponse:
        status_code = 200
        headers: dict[str, str] = {}

        async def aiter_bytes(self):
            nonlocal yielded
            for _ in range(10):
                yielded += 1
                yield b"x" * (1024 * 1024)

    class ChunkedClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        @asynccontextmanager
        async def stream(self, method, url, *, headers, params):
            nonlocal calls
            calls += 1
            yield ChunkedResponse()

    monkeypatch.setattr(httpx, "AsyncClient", ChunkedClient)
    profile = Profile(
        "bounded-models",
        "custom-openai",
        "model",
        base_url="https://api.example.test/v1",
    )
    with pytest.raises(ConnectorError, match="exceeded 4 MiB"):
        await discover_models(profile, api_key="session-secret")
    assert calls == 1
    assert yielded == 5
