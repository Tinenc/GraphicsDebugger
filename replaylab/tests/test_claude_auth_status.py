"""Managed Claude account status must precede the no-generation ACP handshake."""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from replaylab_agent import native_vendor
from replaylab_agent.connectors import claude_auth, subscription
from replaylab_agent.errors import ConnectorError
from replaylab_agent.profiles import Profile


@pytest.fixture
def profile():
    return Profile("claude-test", "claude-code-acp", options={"allow_unsandboxed_agent": True})


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", exit_code=0, hang=False):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stderr.feed_data(stderr)
        self.returncode = None if hang else exit_code
        self.exited = asyncio.Event()
        if not hang:
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.exited.set()

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def stop(self):
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.exited.set()


@pytest.fixture
def fake_cli(monkeypatch):
    launches = []
    stopped = []
    started = asyncio.Event()
    process = [None]

    async def spawn(*argv, **kwargs):
        launches.append((argv, kwargs))
        started.set()
        return process[0]

    async def stop(child):
        stopped.append(child)
        child.stop()

    monkeypatch.setattr(claude_auth, "resolve_known_vendor_executable",
                        lambda preset, command, *, environ=None: "C:/trusted/claude.exe")
    monkeypatch.setattr(claude_auth.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(subscription, "_terminate_async_process_tree", stop)
    return process, launches, stopped, started


@pytest.mark.asyncio
@pytest.mark.parametrize("stdout,exit_code,state", [
    (b'{"loggedIn":true,"email":"PRIVATE_EMAIL","token":"PRIVATE_TOKEN"}', 0, "authenticated"),
    (b'{"loggedIn":false}', 1, "unauthenticated"),
    (b'{"loggedIn":false}', 0, "unauthenticated"),
    (b'{"loggedIn":true}', 1, "process-failed"),
    (b'{"loggedIn":false}', 2, "process-failed"),
    (b'PRIVATE_TOKEN invalid JSON', 1, "invalid-response"),
    (b'{"loggedIn":"true"}', 0, "invalid-response"),
    (b'{"account": {"token":"PRIVATE_TOKEN"}}', 0, "invalid-response"),
    (b'{"loggedIn":false,"loggedIn":true}', 0, "invalid-response"),
])
async def test_auth_status_requires_strict_boolean_and_exit_code(
        stdout, exit_code, state, profile, tmp_path, fake_cli):
    process, launches, stopped, unused_started = fake_cli
    process[0] = FakeProcess(stdout, b"PRIVATE_STDERR", exit_code)
    result = await claude_auth.check_claude_auth_status(
        profile, environ={"LOCALAPPDATA": str(tmp_path)}, cwd=tmp_path)
    assert result.state == state
    assert result.logged_in is (state == "authenticated")
    rendered = repr(asdict(result))
    assert all(secret not in rendered for secret in ("PRIVATE_EMAIL", "PRIVATE_TOKEN", "PRIVATE_STDERR"))
    assert not stopped


@pytest.mark.asyncio
async def test_auth_status_shares_managed_login_environment_and_no_window(profile, tmp_path, fake_cli):
    process, launches, unused_stopped, unused_started = fake_cli
    process[0] = FakeProcess(b'{"loggedIn":true}')
    result = await claude_auth.check_claude_auth_status(
        profile, environ={"LOCALAPPDATA": str(tmp_path), "CLAUDE_CONFIG_DIR": "C:/global",
                          "ANTHROPIC_API_KEY": "PRIVATE_TOKEN"}, cwd=tmp_path)
    assert result.logged_in
    argv, kwargs = launches[0]
    assert argv == ("C:/trusted/claude.exe", "--setting-sources", "project",
                    "auth", "status", "--json")
    assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(
        (tmp_path / "ReplayLab" / "claude" / profile.id).resolve())
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert kwargs["cwd"] == str(tmp_path)
    if os.name == "nt":
        assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
    else:
        assert kwargs["start_new_session"] is True


@pytest.mark.asyncio
async def test_auth_status_without_task_workspace_excludes_all_settings(profile, tmp_path, fake_cli):
    process, launches, unused_stopped, unused_started = fake_cli
    process[0] = FakeProcess(b'{"loggedIn":false}', exit_code=1)
    result = await claude_auth.check_claude_auth_status(
        profile, environ={"LOCALAPPDATA": str(tmp_path)})
    assert not result.logged_in
    assert launches[0][0] == ("C:/trusted/claude.exe", "--setting-sources", "",
                              "auth", "status", "--json")
    assert "cwd" not in launches[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["stdout", "stderr"])
async def test_auth_status_bounds_both_streams_and_stops_process(channel, profile, tmp_path, fake_cli):
    process, unused_launches, stopped, unused_started = fake_cli
    process[0] = FakeProcess(**{channel: b"X" * (claude_auth.MAX_AUTH_STATUS_BYTES + 1)}, hang=True)
    result = await claude_auth.check_claude_auth_status(profile, environ={"LOCALAPPDATA": str(tmp_path)})
    assert result.state == "output-limit" and not result.logged_in
    assert result.message_args == ("output-limit",)
    assert stopped == [process[0]]


@pytest.mark.asyncio
async def test_auth_status_timeout_stops_process(profile, tmp_path, fake_cli):
    process, unused_launches, stopped, unused_started = fake_cli
    process[0] = FakeProcess(hang=True)
    result = await claude_auth.check_claude_auth_status(
        profile, environ={"LOCALAPPDATA": str(tmp_path)}, timeout=0.01)
    assert result.state == "timed-out" and not result.logged_in
    assert stopped == [process[0]]


@pytest.mark.asyncio
async def test_auth_status_cancel_stops_process_and_propagates(profile, tmp_path, fake_cli):
    process, unused_launches, stopped, started = fake_cli
    process[0] = FakeProcess(hang=True)
    pending = asyncio.create_task(claude_auth.check_claude_auth_status(
        profile, environ={"LOCALAPPDATA": str(tmp_path)}))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert stopped == [process[0]]


@pytest.mark.asyncio
async def test_missing_cli_does_not_launch(profile, monkeypatch, fake_cli):
    unused_process, launches, unused_stopped, unused_started = fake_cli
    monkeypatch.setattr(claude_auth, "resolve_known_vendor_executable", lambda *a, **kw: "")
    result = await claude_auth.check_claude_auth_status(profile)
    assert result.state == "cli-unavailable" and not result.logged_in
    assert launches == []


@pytest.mark.asyncio
@pytest.mark.parametrize("logged_in", [False, True])
async def test_subscription_checks_claude_account_before_handshake(
        logged_in, profile, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subscription, "resolve_agent_executable", lambda *a, **kw: "C:/trusted/adapter.cmd")

    async def account(checked_profile, **kwargs):
        calls.append("account")
        assert checked_profile.id == profile.id
        assert kwargs["environ"]["LOCALAPPDATA"] == str(tmp_path)
        return claude_auth._status("authenticated" if logged_in else "unauthenticated")

    class Adapter:
        async def test_connection(self):
            calls.append("handshake")
            return {"sessionId": "session-test", "models": []}

    monkeypatch.setattr(subscription, "check_claude_auth_status", account)
    backend = subscription.SubscriptionBackend(
        profile, timeout=5, emit=lambda event: None,
        environ={"LOCALAPPDATA": str(tmp_path)},
        acp_factory=lambda *args: Adapter())
    result = await backend.test(asyncio.Event())
    assert result.authenticated is logged_in
    assert calls == (["account", "handshake"] if logged_in else ["account"])
    assert result.message_id == ("subscription.claude-sign-in-test-succeeded" if logged_in
                                 else "subscription.claude-account-unauthenticated")


@pytest.mark.asyncio
async def test_valid_claude_account_does_not_hide_failed_handshake(profile, tmp_path, monkeypatch):
    monkeypatch.setattr(subscription, "resolve_agent_executable", lambda *a, **kw: "C:/trusted/adapter.cmd")

    async def account(*args, **kwargs):
        return claude_auth._status("authenticated")

    class Adapter:
        async def test_connection(self):
            raise ConnectorError("test handshake rejected")

    monkeypatch.setattr(subscription, "check_claude_auth_status", account)
    backend = subscription.SubscriptionBackend(
        profile, timeout=5, emit=lambda event: None,
        environ={"LOCALAPPDATA": str(tmp_path)}, acp_factory=lambda *args: Adapter())
    with pytest.raises(ConnectorError, match="handshake rejected"):
        await backend.test(asyncio.Event())


@pytest.mark.asyncio
async def test_subscription_claude_meta_reaches_actual_test_session(profile, tmp_path, monkeypatch):
    import replaylab_agent.connectors.acp as acp
    from test_agent_acp_session import _fake_sdk

    async def unused_prompt(connection):
        raise AssertionError("a connection check must not generate")

    holder = {}
    monkeypatch.setattr(acp, "_load_acp_sdk", lambda: _fake_sdk(unused_prompt, holder))
    monkeypatch.setattr(subscription, "resolve_agent_executable", lambda *a, **kw: "C:/trusted/adapter.cmd")
    backend = subscription.SubscriptionBackend(
        profile, timeout=5, emit=lambda event: None, environ={"LOCALAPPDATA": str(tmp_path)})
    try:
        connector = backend._make_acp_connector(profile, 5, backend._environment(profile))
        details = await connector.test_connection()
        assert details["sessionId"] == "sess-1"
        assert holder["connection"].meta == native_vendor.session_meta(profile)
        options = holder["connection"].meta["claudeCode"]["options"]
        assert options["settingSources"] == ["project"]
        assert options["strictMcpConfig"] is True
        assert [name for name, unused in holder["connection"].calls] == ["initialize", "new_session"]
    finally:
        backend.close()
