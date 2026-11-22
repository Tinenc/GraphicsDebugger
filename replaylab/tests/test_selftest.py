from __future__ import annotations

import importlib.util
import importlib.metadata
from pathlib import Path

import pytest

from replaylab_agent import selftest
from replaylab_agent.selftest import run_protocol_self_test


EXPECTED_NAMES = [
    "api.openai-compatible",
    "api.anthropic-compatible",
    "api.google-genai",
    "subscription.codex-app-server",
    "subscription.acp",
]


@pytest.mark.asyncio
async def test_protocol_self_test_has_fixed_schema_and_exercises_fake_peers() -> None:
    items = await run_protocol_self_test(timeout=5.0)
    payload = [item.to_wire() for item in items]

    assert [item["name"] for item in payload] == EXPECTED_NAMES
    assert all(set(item) == {"name", "status", "detail"} for item in payload)
    assert [item["status"] for item in payload[:4]] == ["ok"] * 4
    assert payload[4]["status"] in {"ok", "warning"}
    assert all(item["detail"] for item in payload)

    # Returning OK here means the production connectors parsed both Codex login
    # flows/model list and, when installed, ACP chat through the real SDK.
    assert "browser/device login URLs" in payload[3]["detail"]
    if importlib.util.find_spec("acp") is None:
        assert "skipped" in payload[4]["detail"]
    else:
        assert "session/new handshake" in payload[4]["detail"]
        assert "two-turn streaming chat and extension compatibility passed" in payload[4]["detail"]


@pytest.mark.asyncio
async def test_missing_optional_acp_runtime_is_an_explicit_warning(monkeypatch) -> None:
    monkeypatch.setattr(selftest.importlib.util, "find_spec", lambda name: None)
    item = await selftest._run_item(  # noqa: SLF001 - protocol failure contract
        "subscription.acp", lambda: selftest._check_acp(1.0)
    )
    assert item.to_wire() == {
        "name": "subscription.acp",
        "status": "warning",
        "detail": "optional agent-client-protocol runtime is not installed; ACP handshake skipped",
    }


@pytest.mark.asyncio
async def test_self_test_failure_redacts_its_synthetic_credential() -> None:
    async def fail() -> str:
        raise RuntimeError(f"request exposed {_self_test_token()}")

    item = await selftest._run_item(  # noqa: SLF001 - redaction contract
        "api.openai-compatible", fail
    )
    assert item.status == "error"
    assert _self_test_token() not in item.detail
    assert "<self-test-token>" in item.detail


def _self_test_token() -> str:
    return selftest._SELF_TEST_TOKEN  # noqa: SLF001 - verify exact secret redaction


def test_self_test_child_environment_excludes_accounts_and_credentials(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "forbidden-home")
    monkeypatch.setenv("CODEX_HOME", "forbidden-codex-home")
    monkeypatch.setenv("OPENAI_API_KEY", "forbidden-openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "forbidden-anthropic-key")
    monkeypatch.setenv("PYTHONSTARTUP", "forbidden-startup")

    environment = selftest._child_environment()  # noqa: SLF001 - isolation contract

    assert "HOME" not in environment
    assert "CODEX_HOME" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment
    assert "PYTHONSTARTUP" not in environment
    assert environment["PYTHONPATH"] == str(
        Path(selftest.__file__).resolve().parents[1]
    )


def test_packaging_smoke_verifies_actual_framework_version(monkeypatch, capsys) -> None:
    original = importlib.metadata.version

    def wrong_deepagents(name: str) -> str:
        return "999.0.0" if name == "deepagents" else original(name)

    monkeypatch.setattr(importlib.metadata, "version", wrong_deepagents)
    assert selftest._external_mcp_import_smoke() == 40
    assert "deepagents-version:runtime requires deepagents==" in capsys.readouterr().err


def test_packaging_smoke_accepts_installed_pinned_framework() -> None:
    assert selftest._external_mcp_import_smoke() == 0


def test_acp_chat_packaging_gate_fails_on_protocol_error(monkeypatch, capsys) -> None:
    async def fail(timeout):
        raise RuntimeError("synthetic ACP protocol failure")

    monkeypatch.setattr(selftest, "_check_acp", fail)
    assert selftest._child_entry(["--acp-chat-smoke"]) == 1
    import json
    assert json.loads(capsys.readouterr().out)["status"] == "error"
