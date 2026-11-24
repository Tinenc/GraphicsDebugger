"""External integrations retain their own authority without leaking credentials."""
from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
import asyncio
import json
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from replaylab_agent import external_mcp


def test_connection_diagnostic_scrubs_unlabelled_credentials_before_truncation(monkeypatch):
    monkeypatch.setenv("TEST_HEADER_CREDENTIAL", "header-environment-secret")
    monkeypatch.setenv("TEST_BEARER_CREDENTIAL", "bearer-environment-secret")
    escaped_secret = 'credential"with\'\\escapes'
    server = {
        "url": "https://user-secret:password-secret@example.invalid/path-secret?sig=query-secret",
        "headers": {"X-Unusual-Header": "literal-header-secret"},
        "headerEnv": {"X-Second-Header": "TEST_HEADER_CREDENTIAL"},
        "bearerEnv": "TEST_BEARER_CREDENTIAL",
        "env": {"CUSTOM_VALUE": escaped_secret},
    }
    message = ("401: " + server["url"] + " redirected to https://elsewhere.invalid/capability-token?key=other-secret "
               "literal-header-secret header-environment-secret bearer-environment-secret "
               + json.dumps(escaped_secret) + " " + repr(escaped_secret) + " " + "x" * 580 + "query-secret")
    result = external_mcp._safe_external_error(server, RuntimeError(message))
    for secret in ("user-secret", "password-secret", "path-secret", "query-secret", "other-secret",
                   "capability-token", "literal-header-secret", "header-environment-secret",
                   "bearer-environment-secret", "credential", "escapes"):
        assert secret not in result
    assert "401" in result and "example.invalid" in result and "elsewhere.invalid" in result
    assert len(result) <= 600


def test_connection_diagnostic_scrubs_exception_chain_and_handles_invalid_settings():
    error = RuntimeError("connection failed")
    error.__cause__ = ValueError("https://host.invalid/private-token?key=query-token")
    result = external_mcp._safe_external_error({"headers": 1, "headerEnv": 2}, error)
    assert "RuntimeError: connection failed" in result
    assert "ValueError" in result and "host.invalid" in result
    assert "private-token" not in result and "query-token" not in result


def _fake_connection(monkeypatch, *, names=(), initialization_error=None,
                     call_error=None, server_name="external", instructions=""):
    mcp = pytest.importorskip("mcp")
    import mcp.client.stdio
    from mcp.types import CallToolResult, TextContent, Tool

    calls = []
    sinks = []

    @asynccontextmanager
    async def stdio(_params, *, errlog):
        sinks.append(errlog)
        yield None, None

    class Session:
        def __init__(self, *_streams):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def initialize(self):
            if initialization_error is not None:
                raise initialization_error
            return SimpleNamespace(serverInfo=SimpleNamespace(name=server_name), instructions=instructions)

        async def list_tools(self):
            return SimpleNamespace(tools=[Tool(name=name, inputSchema={"type": "object"}) for name in names])

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            if call_error is not None:
                raise call_error
            return CallToolResult(content=[TextContent(type="text", text="external result")])

    monkeypatch.setattr(mcp, "ClientSession", Session)
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", stdio)
    return calls, sinks


@pytest.mark.asyncio
async def test_attach_failure_exposes_only_sanitized_diagnostics(monkeypatch, caplog):
    _calls, sinks = _fake_connection(monkeypatch, initialization_error=RuntimeError(
        "403 https://example.invalid/private?sig=secret-query header-secret env-secret"))
    monkeypatch.setattr(external_mcp, "get_logger", lambda: logging.getLogger("test.external-safety"))
    server = {"id": "optional", "command": "fake", "headers": {"X-Auth": "header-secret"},
              "env": {"CUSTOM": "env-secret"}}
    async with AsyncExitStack() as stack:
        pool = external_mcp.ExternalMCPPool()
        await pool.attach([server], stack)
        assert not pool.tools
        assert "403" in pool.failed_servers["optional"]
        for secret in ("secret-query", "header-secret", "env-secret", "/private"):
            assert secret not in pool.failed_servers["optional"]
            assert secret not in caplog.text
        assert sinks and sinks[0].name == external_mcp.os.devnull
        assert sinks[0].closed  # Release failures immediately, not at task end.
    assert sinks[0].closed


@pytest.mark.asyncio
async def test_external_call_exceptions_do_not_leak_configured_values(monkeypatch):
    _fake_connection(monkeypatch, names=["lookup"], call_error=RuntimeError("custom auth value-secret"))
    async with AsyncExitStack() as stack:
        pool = external_mcp.ExternalMCPPool()
        await pool.attach([{"id": "optional", "command": "fake", "headers": {"X-Auth": "value-secret"}}], stack)
        with pytest.raises(RuntimeError) as failure:
            await pool.call("optional__lookup", {})
        assert "value-secret" not in str(failure.value)
        assert "[REDACTED]" in str(failure.value)


@pytest.mark.asyncio
async def test_bread_host_alias_inventory_cannot_bypass_task_capture_route(monkeypatch):
    calls, _ = _fake_connection(monkeypatch, names=[
        "get_capture_status", "get_application_ui_state", "prepare_frame_render_flow", "select_event"])
    async with AsyncExitStack() as stack:
        pool = external_mcp.ExternalMCPPool()
        await pool.attach([{"id": "innocent-alias", "command": "fake", "enabled_tools": ["select_event"]}], stack)
        assert not pool.tools and not calls
        assert "capture-scoped Bread tools" in pool.failed_servers["innocent-alias"]


@pytest.mark.asyncio
async def test_bread_initialization_identity_rejects_partial_inventory(monkeypatch):
    _fake_connection(monkeypatch, names=["select_event"], server_name="Bread",
                     instructions="Capture-bound GPU replay analysis with Shader tools")
    async with AsyncExitStack() as stack:
        pool = external_mcp.ExternalMCPPool()
        await pool.attach([{"id": "another-alias", "command": "fake"}], stack)
        assert not pool.tools and "another-alias" in pool.failed_servers


@pytest.mark.asyncio
async def test_unrelated_capture_server_remains_callable(monkeypatch):
    calls, _ = _fake_connection(monkeypatch, names=["get_capture_status", "lookup"])
    async with AsyncExitStack() as stack:
        pool = external_mcp.ExternalMCPPool()
        await pool.attach([{"id": "third-party", "command": "fake"}], stack)
        result = await pool.call("third-party__lookup", {"query": "example"})
        assert calls == [("lookup", {"query": "example"})]
        assert not pool.failed_servers
        assert result.content[-1].text == "external result"


@pytest.mark.parametrize("configured, expected", [(None, 10.0), (True, 10.0),
                         (float("nan"), 10.0), (-1, 0.1), (4.5, 4.5), (999, 30.0)])
def test_optional_startup_timeout_respects_configured_finite_bound(configured, expected):
    assert external_mcp._startup_timeout({"startup_timeout_sec": configured}) == expected


def _delayed_connections(monkeypatch, *, delays):
    mcp = pytest.importorskip("mcp")
    import mcp.client.stdio
    from mcp.types import Tool

    events = []

    @asynccontextmanager
    async def stdio(params, *, errlog):
        events.append((params.command, "transport-open"))
        try:
            yield params.command, None
        finally:
            events.append((params.command, "transport-close"))

    class Session:
        def __init__(self, name, _write):
            self.name = name

        async def __aenter__(self):
            events.append((self.name, "session-open"))
            return self

        async def __aexit__(self, *_args):
            events.append((self.name, "session-close"))

        async def initialize(self):
            await asyncio.sleep(delays.get(self.name, 0))
            return SimpleNamespace(serverInfo=SimpleNamespace(name="external"))

        async def list_tools(self):
            return SimpleNamespace(tools=[Tool(name="lookup", inputSchema={})])

    monkeypatch.setattr(mcp, "ClientSession", Session)
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", stdio)
    return events


@pytest.mark.asyncio
async def test_stalled_optional_server_closes_without_delaying_healthy_connection(monkeypatch):
    events = _delayed_connections(monkeypatch, delays={"stalled": 60})
    async with AsyncExitStack() as stack:
        pool = external_mcp.ExternalMCPPool()
        await pool.attach([
            {"id": "stalled", "command": "stalled", "startup_timeout_sec": 0.1},
            {"id": "working", "command": "working"},
        ], stack)
        assert "TimeoutError" in pool.failed_servers["stalled"]
        assert "working__lookup" in pool.tools
        assert ("stalled", "session-close") in events
        assert ("stalled", "transport-close") in events
        assert events.index(("working", "transport-open")) < events.index(("stalled", "transport-close"))
        assert ("working", "transport-close") not in events
    assert events[-2:] == [("working", "session-close"), ("working", "transport-close")]


@pytest.mark.asyncio
async def test_optional_startup_has_total_budget_and_preserves_working_connections(monkeypatch):
    events = _delayed_connections(monkeypatch, delays={"stalled": 60})
    monkeypatch.setattr(external_mcp, "_STARTUP_TOTAL_SECONDS", 0.1)
    async with AsyncExitStack() as stack:
        pool = external_mcp.ExternalMCPPool()
        await pool.attach([
            {"id": "working", "command": "working"},
            {"id": "stalled", "command": "stalled"},
            {"id": "also-working", "command": "also-working"},
        ], stack)
        assert "working__lookup" in pool.tools
        assert "stalled" in pool.failed_servers
        assert "also-working__lookup" in pool.tools
        assert ("stalled", "transport-close") in events


@pytest.mark.asyncio
async def test_cancelled_startup_closes_connection_and_propagates_cancellation(monkeypatch):
    events = _delayed_connections(monkeypatch, delays={"stalled": 60})
    async with AsyncExitStack() as stack:
        pool = external_mcp.ExternalMCPPool()
        task = asyncio.create_task(pool.attach([{"id": "stalled", "command": "stalled"}], stack))
        for _ in range(10):
            if ("stalled", "session-open") in events:
                break
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events[-2:] == [("stalled", "session-close"), ("stalled", "transport-close")]
        assert not pool.tools


@pytest.mark.asyncio
async def test_real_stdio_initialization_timeout_reaps_process_and_keeps_next_server_usable(monkeypatch):
    pytest.importorskip("mcp")
    import mcp.client.stdio

    processes = []
    create_process = mcp.client.stdio._create_platform_compatible_process

    async def track_process(*args, **kwargs):
        process = await create_process(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(mcp.client.stdio, "_create_platform_compatible_process", track_process)
    fixture = Path(__file__).parent / "fixtures" / "fake_large_external_mcp.py"
    async with AsyncExitStack() as stack:
        pool = external_mcp.ExternalMCPPool()
        await pool.attach([
            {"id": "stalled", "command": sys.executable,
             "args": ["-c", "import time; time.sleep(60)"], "startup_timeout_sec": 1.0},
            {"id": "working", "command": sys.executable, "args": [str(fixture)]},
        ], stack)
        assert "TimeoutError" in pool.failed_servers["stalled"]
        assert len(processes) == 2 and processes[0].returncode is not None
        assert processes[1].returncode is None
        assert any(name.startswith("working__") for name in pool.tools)
        result = await pool.call("working__large", {})
        assert json.loads(result.content[-1].text)["calls"] == 1
    assert processes[1].returncode is not None
