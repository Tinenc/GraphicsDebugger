from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from replaylab_agent.agent_context import harden_codex_argv
from replaylab_agent.agent_tools_config import AgentToolsConfig
from replaylab_agent.desktop_capabilities import (
    discover_desktop_capabilities, codex_runtime_config, codex_runtime_overrides,
    native_execution_policy, is_bread_server_config,
)
from replaylab_agent.native_vendor import prepare_workspace, runtime_argv, session_meta
from replaylab_agent.profiles import Profile


def plugin(root: Path, name: str = "browser", manifest: dict | None = None) -> Path:
    directory = root / "plugins/cache/test" / name / "1.0"
    (directory / ".codex-plugin").mkdir(parents=True)
    (directory / ".codex-plugin/plugin.json").write_text(json.dumps(manifest or {"skills": "./skills"}))
    (directory / "skills/demo").mkdir(parents=True)
    (directory / "skills/demo/SKILL.md").write_text("demo")
    return directory


def test_inheritance_is_explicit_and_survives_settings_edits(tmp_path):
    path = tmp_path / "tools.json"
    path.write_text('{"inheritDesktopCapabilities":true,"mcpServers":[]}')
    config = AgentToolsConfig.load(path)
    config.set_skill_enabled("one", False)
    config.save()
    assert AgentToolsConfig.load(path).inherit_desktop_capabilities
    assert not AgentToolsConfig.load(tmp_path / "missing").inherit_desktop_capabilities


def test_desktop_discovery_preserves_enabled_selection_and_hides_secrets(tmp_path):
    installed = plugin(tmp_path)
    (installed / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "disabled": {"command": "should-not-start"},
        "private": {"command": "desktop-service", "enabled": False},
    }}))
    (tmp_path / "skills").mkdir()
    (tmp_path / "config.toml").write_text('''
profile = "work"
[profiles.work]
sandbox_mode = "read-only"
approval_policy = "on-request"
[plugins."browser@test"]
enabled = true
[plugins."absent@test"]
enabled = false
[mcp_servers.disabled]
command = "no"
enabled = false
[mcp_servers.node]
command = "python"
args = ["server.py"]
enabled_tools = ["read"]
disabled_tools = ["write"]
[mcp_servers.node.env]
SECRET = "do-not-print-this"
[mcp_servers.rdoc_alias]
command = "python"
args = ["-m", "replaylab_mcp.server"]
''')
    discovered = discover_desktop_capabilities(tmp_path)
    assert [item["id"] for item in discovered.servers] == ["node"]
    assert discovered.servers[0]["enabled_tools"] == ["read"]
    assert discovered.config["approval_policy"] == "on-request"
    assert discovered.config["sandbox_mode"] == "read-only"
    assert installed / "skills" in discovered.skills
    assert "do-not-print-this" not in json.dumps(discovered.summary())
    assert {item["name"] for item in discovered.server_status if item["status"] == "disabled"} == {"disabled", "private"}


def test_skills_available_without_codex_config_and_explicit_disabled_paths(tmp_path, monkeypatch):
    (tmp_path / "skills/demo").mkdir(parents=True)
    skill = tmp_path / "skills/demo/SKILL.md"
    skill.write_text("demo")
    assert discover_desktop_capabilities(tmp_path).skills == [tmp_path / "skills"]
    (tmp_path / "config.toml").write_text('[[skills.config]]\npath=' + json.dumps(str(skill.parent)) + '\nenabled=false\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config = AgentToolsConfig(inherit_desktop_capabilities=True)
    catalog = SimpleNamespace(skills={"demo": SimpleNamespace(path=skill), "other": SimpleNamespace(path=None)})
    config.filter_catalog(catalog)
    assert list(catalog.skills) == ["other"]
    assert config.desktop_disabled_skill_ids == ("demo",)


def test_native_settings_use_private_protocol_not_secret_process_argv(tmp_path):
    (tmp_path / "config.toml").write_text('''
sandbox_mode="danger-full-access"
[features]
browser_use=true
[mcp_servers.local]
command="python"
[mcp_servers.local.env]
TOKEN="hidden-token"
''')
    discovered = discover_desktop_capabilities(tmp_path)
    settings = codex_runtime_config(discovered, tmp_path / "managed")
    argv = harden_codex_argv(["codex", "app-server"], inherit_desktop_capabilities=True,
                            config_overrides=codex_runtime_overrides(discovered, tmp_path / "managed"))
    assert "hidden-token" not in str(argv)
    assert 'features.browser_use=true' in argv
    assert 'features.browser_use=false' not in argv
    assert settings['mcp_servers.local']["env"]["TOKEN"] == "hidden-token"
    assert settings["sandbox_mode"] == "danger-full-access"
    assert "approval_policy" not in settings


@pytest.mark.parametrize("preset", ["claude-code-acp", "gemini-acp", "qwen-acp", "copilot-acp", "kimi-acp", "codebuddy-acp"])
def test_native_vendors_no_longer_receive_bread_builtin_denials_when_enabled(tmp_path, preset):
    profile = Profile("test", preset, options={"allow_unsandboxed_agent": True})
    prepare_workspace(profile, tmp_path, ["inspect_event"], inherit_desktop_capabilities=True)
    assert list(tmp_path.iterdir()) == []
    command = ["vendor", "acp", "--tools", ""] if preset == "codebuddy-acp" else ["vendor", "acp"]
    assert runtime_argv(profile, command, tmp_path, ["inspect_event"], inherit_desktop_capabilities=True) == ["vendor", "acp"]
    if preset == "claude-code-acp":
        options = session_meta(profile, inherit_desktop_capabilities=True)["claudeCode"]["options"]
        assert "disallowedTools" not in options and "user" in options["settingSources"]


@pytest.mark.asyncio
async def test_codex_inherits_real_policy_and_tools_without_changing_approval(tmp_path, monkeypatch):
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector
    connector = CodexAppServerConnector(["fake"], cwd=tmp_path, inherit_desktop_capabilities=True,
        expected_mcp_servers={"Bread"}, desktop_runtime_config={"approval_policy": "on-request"})
    requests = []
    async def request(method, params, **kwargs):
        requests.append((method, dict(params)))
        if method == "account/read":
            return {"account": {"type": "chatgpt"}}
        if method == "thread/start":
            return {"thread": {"id": "thread", "instructionSources": ["enabled skill"]},
                    "sandbox": {"type": "readOnly"}, "approvalPolicy": "on-request"}
        if method == "mcpServerStatus/list":
            return {"data": [{"name": name, "runtimeStatus": "connected", "tools": {"read": {}}} for name in ("Bread", "browser")]}
        if method == "app/installed":
            return {"apps": [{"id": "app", "callable": True}]}
        if method == "turn/start":
            return {"turn": {"id": "turn"}}
        return {}
    monkeypatch.setattr(connector, "_initialize", AsyncMock())
    monkeypatch.setattr(connector._transport, "request", request)
    monkeypatch.setattr(connector, "_collect_turn", AsyncMock(return_value="ok"))
    result = await connector.run("explain")
    assert result.text == "ok"
    starts = [args for method, args in requests if method in {"thread/start", "turn/start"}]
    assert all("approvalPolicy" not in args and "permissions" not in args for args in starts)
    assert starts[0]["config"]["approval_policy"] == "on-request"
    assert connector.native_capabilities["callableApps"] == ["app"]
    assert connector.native_capabilities["approvalPolicy"] == "on-request"


@pytest.mark.asyncio
async def test_imported_explicit_tool_approval_is_never_fabricated():
    from replaylab_agent.external_mcp import ExternalMCPPool, ExternalTool
    session = SimpleNamespace(call_tool=AsyncMock())
    pool = ExternalMCPPool()
    pool.tools["desktop__change"] = ExternalTool("desktop", "change", SimpleNamespace(), session, "prompt")
    result = await pool.call("desktop__change", {})
    assert result.isError
    assert "external-tool-approval-required" in result.content[0].text
    session.call_tool.assert_not_called()


@pytest.mark.parametrize("command", ["C:/app/BreadMCP.exe", "C:/app/ReplayLabInternal.exe"])
def test_native_import_excludes_packaged_host_aliases(command):
    assert is_bread_server_config({"command": command})


@pytest.mark.parametrize("settings,options,enabled", [
    ({}, {}, True),
    ({"sandbox_mode": "danger-full-access"}, {}, True),
    ({"sandbox_mode": "read-only"}, {}, False),
    ({"approval_policy": "on-request"}, {}, False),
    ({"approval_policy": "never"}, {}, True),
    ({}, {"native_shell": False}, False),
    ({}, {"approval_policy": "untrusted"}, False),
    ({"default_permissions": "limited"}, {}, False),
])
def test_native_execution_respects_explicit_provider_policy(settings, options, enabled):
    assert native_execution_policy(True, settings, options)[0] is enabled


def test_plugin_services_and_disabled_settings_match_shared_and_native(tmp_path):
    installed = plugin(tmp_path)
    (installed / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "cua_repl": {"command": "python", "args": ["${CODEX_PLUGIN_ROOT}/server.py"],
                     "default_tools_approval_mode": "prompt"}}}))
    (tmp_path / "config.toml").write_text('[plugins."browser@test"]\nenabled=true\n[[skills.config]]\npath=' + json.dumps(str(installed / "skills/demo")) + '\nenabled=false\n')
    discovered = discover_desktop_capabilities(tmp_path)
    managed = tmp_path / "managed"
    settings = codex_runtime_config(discovered, managed, declared_mcp_ids=("cua_repl",))
    assert settings["mcp_servers.cua_repl"]["enabled"] is False
    assert settings["mcp_servers.cua_repl"]["default_tools_approval_mode"] == "prompt"
    assert settings["skills"]["config"][0]["enabled"] is False
    assert settings["skills"]["config"][0]["path"].startswith(str(managed))


@pytest.mark.asyncio
async def test_late_unscoped_host_is_inspected_before_model_turn(tmp_path, monkeypatch):
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector, _UnscopedBreadServers
    connector = CodexAppServerConnector(["fake"], cwd=tmp_path, inherit_desktop_capabilities=True)
    inventories = [
        {"alias": {"name": "alias", "runtimeStatus": "starting", "tools": {}}},
        {"alias": {"name": "alias", "runtimeStatus": "connected", "tools": {
            name: {} for name in ("get_application_ui_state", "get_capture_status", "prepare_shader_workspace")}}},
    ]
    monkeypatch.setattr(connector, "_list_thread_mcp_servers", AsyncMock(side_effect=inventories))
    monkeypatch.setattr(connector._transport, "request", AsyncMock(return_value={"apps": []}))
    with pytest.raises(_UnscopedBreadServers):
        await connector._verify_tool_isolation("thread")
    assert connector._list_thread_mcp_servers.await_count == 2


@pytest.mark.asyncio
async def test_acp_terminal_actual_argv_and_release(tmp_path):
    import os
    import sys
    from replaylab_agent.acp_terminal import ACPTerminals
    manager = ACPTerminals(tmp_path, os.environ)
    literal = "value&%NOT_EXPANDED% 'quoted'"
    result = await manager.create(sys.executable, ["-c", "import sys; print(sys.argv[1])", literal])
    identity = result["terminalId"]
    assert (await manager.wait(identity))["exitCode"] == 0
    assert literal in (await manager.output(identity))["output"]
    await manager.release(identity)
    with pytest.raises(ValueError, match="Unknown"):
        await manager.output(identity)
    await manager.close()


@pytest.mark.asyncio
async def test_acp_terminal_requires_matching_session(tmp_path):
    from replaylab_agent.acp_session import _main_agent_client
    from replaylab_agent.connectors.acp import _load_acp_sdk, _ACPClientState
    from replaylab_agent.errors import PolicyError
    state = _ACPClientState(on_delta=None)
    state.session_id = "owned"
    terminal = SimpleNamespace(create=AsyncMock(return_value={"terminalId": "one"}))
    client = _main_agent_client(_load_acp_sdk(), state, frozenset(), terminals=terminal)
    with pytest.raises(PolicyError):
        await client.create_terminal("foreign", "python")
    terminal.create.assert_not_called()
    assert await client.create_terminal("owned", "python") == {"terminalId": "one"}


def test_cursor_migration_preserves_user_denials_and_approval_mode(tmp_path, monkeypatch):
    from replaylab_agent.process_env import ensure_cursor_profile_config, release_cursor_managed_tool_restrictions
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    profile = Profile("cursor-test", "cursor-acp", options={"allow_unsandboxed_agent": True})
    path = ensure_cursor_profile_config(profile)
    payload = json.loads(path.read_text())
    payload["permissions"]["deny"].append("Shell(rm*)")
    path.write_text(json.dumps(payload))
    release_cursor_managed_tool_restrictions(profile)
    updated = json.loads(path.read_text())
    assert updated["permissions"]["deny"] == ["Shell(rm*)"]
    assert updated["approvalMode"] == "allowlist"
    assert updated["permissions"]["allow"] == ["Mcp(Bread:*)"]


def test_native_shell_is_a_validated_per_connection_boolean():
    profile = Profile("api", "openai", model="test-model", options={"native_shell": False})
    profile.validate()
    from replaylab_agent.errors import ConfigurationError
    profile.options["native_shell"] = "yes"
    with pytest.raises(ConfigurationError, match="boolean"):
        profile.validate()
