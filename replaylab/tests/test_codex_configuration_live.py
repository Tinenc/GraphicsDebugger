"""Opt-in real Codex config parsing, without credentials or model requests."""
from __future__ import annotations

import os
import shutil

import pytest

from replaylab_agent.agent_context import harden_codex_argv
from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector


CODEX = os.environ.get("REPLAYLAB_TEST_CODEX_EXECUTABLE") or shutil.which("codex")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("REPLAYLAB_TEST_REAL_CODEX") != "1" or not CODEX,
        reason="opt-in real Codex configuration check; no remote model calls",
    ),
]


@pytest.mark.parametrize("inherit", [False, True], ids=["isolated", "inherited"])
async def test_real_codex_hardening_loads_config_and_preserves_agent_roles(tmp_path, inherit):
    home = tmp_path / "codex-home"
    home.mkdir()
    config = home / "config.toml"
    source = ('[features]\nmulti_agent=true\n'
              '[agents.reviewer]\ndescription="Existing review role"\n')
    config.write_text(source, encoding="utf-8")
    argv = harden_codex_argv([CODEX, "app-server"], inherit_desktop_capabilities=inherit)
    connector = CodexAppServerConnector(
        argv, cwd=tmp_path, timeout=15,
        env={**os.environ, "CODEX_HOME": str(home), "OPENAI_API_KEY": "", "CODEX_API_KEY": ""},
    )
    try:
        account = await connector.account_read()
        assert account["account"] is None
        result = await connector._transport.request("config/read", {"includeLayers": False}, timeout=10)
        assert result["config"]["features"]["multi_agent"] is inherit
        assert result["config"]["agents"]["reviewer"]["description"] == "Existing review role"
    finally:
        await connector.close()
    assert config.read_text(encoding="utf-8") == source
    assert connector._transport.process is None
