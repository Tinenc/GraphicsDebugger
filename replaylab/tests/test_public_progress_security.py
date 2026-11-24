from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from replaylab_agent.agent_diagnostics import public_error, redact
from replaylab_agent.connectors import acp, codex_app_server
from replaylab_agent.connectors.acp import ACPConnector
from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector
from replaylab_agent.errors import ConnectorError


def test_public_error_is_bounded_single_line_and_redacts_credentials() -> None:
    secret = "sk-secret-value-that-must-not-appear"
    error = RuntimeError("provider failed\nwith api_key=" + secret + " " + "x" * 2000)

    summary = public_error(error)

    assert "\n" not in summary
    assert len(summary) <= 600
    assert secret not in summary
    assert "[REDACTED]" in summary


def test_redact_handles_common_secret_assignments() -> None:
    rendered = redact(
        "token=one-secret authorization:two-secret password=three-secret "
        "api_key=four-secret"
    )

    for secret in ("one-secret", "two-secret", "three-secret", "four-secret"):
        assert secret not in rendered
    assert rendered.count("[REDACTED]") == 4


@pytest.mark.asyncio
async def test_codex_spawn_failure_keeps_argv_out_of_connector_error(
    tmp_path, monkeypatch
) -> None:
    secret_argument = "--api-key=sk-command-secret-value"
    private_executable = r"C:\private\bin\codex-secret.exe"

    async def fail_spawn(*_args, **_kwargs):
        raise FileNotFoundError(f"missing {private_executable} {secret_argument}")

    monkeypatch.setattr(codex_app_server.asyncio, "create_subprocess_exec", fail_spawn)
    connector = CodexAppServerConnector(
        [private_executable, "app-server", secret_argument], cwd=tmp_path
    )

    with pytest.raises(ConnectorError) as caught:
        await connector._transport.start()

    prefix = (
        "cannot start Codex app-server; verify the configured executable and "
        "connection settings"
    )
    message = str(caught.value)
    assert message.startswith(prefix + "; startup report: ")
    report = Path(message.removeprefix(prefix + "; startup report: "))
    assert report.is_file()
    assert "sk-command-secret-value" not in report.read_text(encoding="utf-8")
    assert private_executable not in str(caught.value)
    assert secret_argument not in str(caught.value)
    assert isinstance(caught.value.__cause__, FileNotFoundError)


@pytest.mark.asyncio
async def test_acp_spawn_failure_keeps_argv_out_of_connector_error(
    tmp_path, monkeypatch
) -> None:
    secret_argument = "--token=cursor-command-secret-value"
    private_executable = r"C:\private\bin\cursor-agent.exe"

    def fail_spawn(*_args, **_kwargs):
        raise FileNotFoundError(f"missing {private_executable} {secret_argument}")

    sdk = SimpleNamespace(
        client_type=object,
        protocol_version=1,
        spawn_agent_process=fail_spawn,
    )
    monkeypatch.setattr(acp, "_load_acp_sdk", lambda: sdk)
    connector = ACPConnector(
        [private_executable, secret_argument], cwd=tmp_path, timeout=1
    )

    with pytest.raises(ConnectorError) as caught:
        await connector.test_connection()

    assert str(caught.value) == (
        "cannot start ACP agent; verify the configured executable and connection settings"
    )
    assert private_executable not in str(caught.value)
    assert secret_argument not in str(caught.value)
    assert isinstance(caught.value.__cause__, FileNotFoundError)
