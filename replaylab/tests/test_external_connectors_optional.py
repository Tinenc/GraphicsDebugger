from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from replaylab_agent.agent_context import harden_codex_argv
from replaylab_agent.connectors.acp import ACPConnector
from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector
from replaylab_agent.errors import (
    AuthenticationError,
    ConfigurationError,
    ConnectorError,
    LifecycleCleanupError,
    PolicyError,
    ProviderCapabilityError,
)


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_codex_app_server_lifecycle_and_skill_input(tmp_path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("test", encoding="utf-8")
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py")],
        cwd=tmp_path,
        timeout=5,
    )
    try:
        result = await connector.run(
            "explain",
            skill_name="renderdoc-explain-event",
            skill_path=skill,
        )
    finally:
        await connector.close()

    assert result.text == "evidence answer"
    assert result.thread_id == "thread-test"
    assert result.turn_id == "turn-test"


@pytest.mark.asyncio
async def test_codex_app_server_keeps_one_thread_for_bounded_follow_up_turns(
    tmp_path,
) -> None:
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py")],
        cwd=tmp_path,
        timeout=5,
    )
    try:
        first = await connector.run("first", keep_thread=True)
        process = connector._transport.process
        assert process is not None
        first_pid = process.pid

        second = await connector.run("continue", keep_thread=True)

        assert connector._transport.process is process
        assert connector._transport.process.pid == first_pid
        assert first.thread_id == second.thread_id == "thread-test"
        assert connector._active_thread_id == "thread-test"
    finally:
        await connector.close()

    assert connector._transport.process is None
    assert connector._active_thread_id == ""


@pytest.mark.asyncio
async def test_codex_accepts_exact_task_scoped_replaylab_mcp_inventory(tmp_path) -> None:
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), "native-mcp"],
        cwd=tmp_path,
        expected_mcp_servers={"Bread"},
        timeout=5,
    )
    try:
        result = await connector.run("explain")
    finally:
        await connector.close()

    assert result.text == "evidence answer"


@pytest.mark.asyncio
async def test_codex_waits_for_task_scoped_replaylab_mcp_startup(tmp_path) -> None:
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), "starting-mcp"],
        cwd=tmp_path,
        expected_mcp_servers={"Bread"},
        timeout=5,
    )
    try:
        result = await connector.run("explain")
    finally:
        await connector.close()

    assert result.text == "evidence answer"


@pytest.mark.asyncio
async def test_codex_bounds_task_scoped_replaylab_mcp_startup_wait(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.connectors.codex_app_server as codex_connector

    monkeypatch.setattr(codex_connector, "_MCP_STARTUP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(codex_connector, "_MCP_STARTUP_POLL_SECONDS", 0.01)
    connector = CodexAppServerConnector(
        [
            sys.executable,
            str(FIXTURES / "fake_codex_app_server.py"),
            "stuck-starting-mcp",
        ],
        cwd=tmp_path,
        expected_mcp_servers={"Bread"},
        timeout=5,
    )
    try:
        with pytest.raises(ConnectorError, match="timed out waiting"):
            await connector.run("explain")
    finally:
        await connector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["failed-mcp", "empty-mcp-tools"])
async def test_codex_rejects_unready_task_scoped_replaylab_mcp(
    tmp_path, mode: str
) -> None:
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), mode],
        cwd=tmp_path,
        expected_mcp_servers={"Bread"},
        timeout=5,
    )
    try:
        with pytest.raises(ConnectorError, match="did not initialize"):
            await connector.run("explain")
    finally:
        await connector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("legacy-initialize-protocol", "codex-app-server.permission-profile-contract-unsupported"),
        ("read-only-profile-unavailable", "codex-app-server.read-only-profile-unavailable"),
        ("unverified-read-scope", "codex-app-server.read-scope-unverified"),
    ],
)
async def test_codex_permission_incompatibility_is_typed_and_nonretryable(
    tmp_path, mode: str, expected_code: str
) -> None:
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), mode],
        cwd=tmp_path,
        timeout=5,
    )
    try:
        with pytest.raises(ProviderCapabilityError) as raised:
            await connector.run("explain")
    finally:
        await connector.close()

    assert isinstance(raised.value, ConfigurationError)
    assert raised.value.error_code == expected_code
    assert raised.value.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "ambient-instructions",
        "extra-mcp",
        "wrong-tools",
        "callable-app",
        "unexpected-host-tool",
    ],
)
async def test_codex_ambient_capabilities_fail_closed(tmp_path, mode: str) -> None:
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), mode],
        cwd=tmp_path,
        timeout=5,
    )
    try:
        with pytest.raises(PolicyError):
            await connector.run("explain")
    finally:
        await connector.close()


def test_codex_process_hardening_keeps_read_only_workspace_tools() -> None:
    rendered = " ".join(harden_codex_argv(["codex", "app-server"]))

    for override in (
        'model_provider="replaylab_openai_subscription"',
        "features.apps=false",
        "features.plugins=false",
        "features.shell_tool=true",
        "features.unified_exec=true",
        "tools.view_image=true",
        "features.hooks=false",
        "features.multi_agent=false",
        'web_search="disabled"',
        'history.persistence="none"',
    ):
        assert override in rendered
    assert "mcp_servers.Bread" not in rendered
    assert "agents.enabled=" not in rendered


@pytest.mark.asyncio
async def test_codex_account_models_and_managed_login_surface(tmp_path) -> None:
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py")],
        cwd=tmp_path,
        timeout=5,
    )
    try:
        assert (await connector.account_read())["account"]["planType"] == "plus"
        assert [item["id"] for item in await connector.list_models()] == ["codex-test"]
        browser = await connector.login_start(flow="browser")
        assert (await connector.wait_login(browser["loginId"], timeout=5))["success"] is True
        await connector.logout()
    finally:
        await connector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["signed-out", "signed-out-false", "signed-out-no-flag"])
async def test_codex_signed_out_fails_before_turn_start(tmp_path, mode: str) -> None:
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), mode],
        cwd=tmp_path,
        timeout=5,
    )
    try:
        with pytest.raises(AuthenticationError, match="Settings > AI"):
            await connector.run("do not send")
    finally:
        await connector.close()


@pytest.mark.asyncio
async def test_codex_jsonl_accepts_bounded_record_over_64_kib(tmp_path) -> None:
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), "large-line"],
        cwd=tmp_path,
        timeout=5,
    )
    try:
        result = await connector.run("explain")
    finally:
        await connector.close()
    assert result.text == "x" * 100_000


@pytest.mark.asyncio
async def test_codex_hung_turn_times_out_and_closes_owned_process(tmp_path) -> None:
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py"), "hang-turn"],
        cwd=tmp_path,
        timeout=0.2,
        heartbeat_interval=0.05,
    )
    with pytest.raises(ConnectorError, match="turn timed out"):
        await asyncio.wait_for(connector.run("explain"), timeout=4)
    assert connector._transport.process is None


@pytest.mark.asyncio
async def test_acp_connection_diagnostic_stops_before_model_prompt(tmp_path) -> None:
    pytest.importorskip("acp")
    connector = ACPConnector(
        [sys.executable, str(FIXTURES / "fake_acp_agent.py"), "no-mcp"],
        cwd=tmp_path,
        timeout=5,
    )

    result = await connector.test_connection()

    assert result["sessionId"] == "session-test"


@pytest.mark.asyncio
async def test_acp_provider_exit_is_reported_without_hanging(tmp_path) -> None:
    pytest.importorskip("acp")
    connector = ACPConnector(
        [sys.executable, str(FIXTURES / "fake_acp_agent.py"), "exit", "no-mcp"],
        cwd=tmp_path,
        timeout=2,
    )
    with pytest.raises(ConnectorError):
        await asyncio.wait_for(connector.test_model(), timeout=4)


@pytest.mark.asyncio
async def test_acp_process_cleanup_timeout_is_fatal(monkeypatch) -> None:
    import replaylab_agent.connectors.acp as acp_connector

    cleanup_cancelled = asyncio.Event()

    class HangingManager:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, *args):
            del args
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_cancelled.set()

    monkeypatch.setattr(acp_connector, "_ACP_CLEANUP_TIMEOUT", 0.02)
    with pytest.raises(LifecycleCleanupError, match="cleanup was not confirmed"):
        async with acp_connector._managed_agent_process(HangingManager(), "test ACP"):
            pass
    await asyncio.wait_for(cleanup_cancelled.wait(), timeout=1)


def test_codex_connector_creates_configured_home_only_after_start(tmp_path) -> None:
    home = (tmp_path / "managed-codex-home").resolve()
    connector = CodexAppServerConnector(
        [sys.executable, str(FIXTURES / "fake_codex_app_server.py")],
        cwd=tmp_path,
        env={
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
        }
        | {"CODEX_HOME": str(home)},
    )
    assert not home.exists()
    # Construction is side-effect free; lifecycle behavior is covered above.
    assert connector._transport.process is None
