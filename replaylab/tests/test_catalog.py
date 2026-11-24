from __future__ import annotations

import pytest

from replaylab_agent.catalog import (
    CODEBUDDY_BREAD_MCP_PERMISSION,
    PRESETS,
    PRESET_BY_ID,
    BREAD_MCP_SERVER_NAME,
    get_preset,
)
from replaylab_agent.errors import ConfigurationError
from replaylab_agent.process_env import agent_command_argv
from replaylab_agent.profiles import Profile


def test_catalog_has_unique_stable_ids_and_both_connector_semantics() -> None:
    ids = [preset.id for preset in PRESETS]

    assert len(ids) == len(set(ids)) == len(PRESET_BY_ID)
    assert {preset.kind for preset in PRESETS} == {"model", "agent"}
    assert {
        "openai",
        "anthropic",
        "gemini",
        "azure-openai",
        "deepseek",
        "qwen",
        "qwen-coding-plan",
        "kimi",
        "minimax",
        "glm",
        "doubao",
        "hunyuan",
        "qianfan",
        "siliconflow",
        "ollama",
        "codex",
        "copilot-acp",
        "cursor-acp",
        "gemini-acp",
        "qwen-acp",
        "kimi-acp",
        "codebuddy-acp",
        "claude-code-acp",
        "custom-acp",
    } <= set(ids)


def test_catalog_separates_api_key_references_from_managed_agent_commands() -> None:
    for preset in PRESETS:
        assert get_preset(preset.id) is preset
        if preset.kind == "model" and preset.auth == "api-key":
            assert preset.api_key_env
            assert preset.api_key_env.upper() == preset.api_key_env
        if preset.kind == "agent" and preset.id != "custom-acp":
            assert preset.command
            assert preset.auth == "managed"

    assert get_preset("openai").protocol == "openai-responses"
    assert get_preset("codex").protocol == "codex-app-server"
    assert get_preset("copilot-acp").protocol == "acp"
    assert get_preset("cursor-acp").acp_auth_method == "cursor_login"
    coding_plan = get_preset("qwen-coding-plan")
    assert coding_plan.base_url == "https://coding.dashscope.aliyuncs.com/v1"
    assert coding_plan.api_key_env == "BAILIAN_CODING_PLAN_API_KEY"


def test_catalog_exposes_configuration_and_authentication_ui_semantics() -> None:
    assert get_preset("openai").configuration_mode == "api"
    assert get_preset("openai").authentication_ui == "api-key"
    assert get_preset("openai").supports_model_discovery is True
    assert get_preset("codex").configuration_mode == "subscription"
    assert get_preset("codex").authentication_ui == "codex-managed"
    assert get_preset("codex").supports_model_discovery is True
    assert get_preset("copilot-acp").authentication_ui == "vendor-cli"
    assert get_preset("copilot-acp").supports_model_discovery is False


@pytest.mark.parametrize(
    ("preset_id", "agent_command", "login_command", "login_mode"),
    [
        ("copilot-acp", ("copilot", "--acp"), ("copilot", "login"), "captured"),
        (
            "cursor-acp",
            ("cursor-agent", "acp"),
            ("cursor-agent", "login"),
            "captured",
        ),
        ("gemini-acp", ("gemini", "--acp"), ("gemini",), "terminal"),
        ("qwen-acp", ("qwen", "--acp"), ("qwen",), "terminal"),
        ("kimi-acp", ("kimi", "acp"), ("kimi", "login"), "captured"),
        (
            "codebuddy-acp",
            (
                "codebuddy",
                "--acp",
                "--agent",
                "cli",
                "--strict-mcp-config",
                "--no-session-persistence",
                "--setting-sources",
                "user",
                "--tools",
                "",
                "--allowedTools",
                CODEBUDDY_BREAD_MCP_PERMISSION,
            ),
            (
                "codebuddy",
                "--agent",
                "cli",
                "--strict-mcp-config",
                "--no-session-persistence",
                "--setting-sources",
                "user",
                "--tools",
                "",
            ),
            "terminal",
        ),
        (
            "claude-code-acp",
            ("claude-agent-acp",),
            ("claude", "auth", "login"),
            "terminal",
        ),
    ],
)
def test_official_acp_presets_match_vendor_entrypoints(
    preset_id, agent_command, login_command, login_mode
) -> None:
    preset = get_preset(preset_id)

    assert preset.command == agent_command
    assert preset.subscription_login == login_command
    assert preset.subscription_login_mode == login_mode
    assert preset.install_hint
    assert "http" in preset.install_hint


def test_claude_api_and_subscription_reference_adapter_are_separate_presets() -> None:
    assert "claude-acp" not in PRESET_BY_ID
    api = get_preset("anthropic")
    subscription = get_preset("claude-code-acp")

    assert api.kind == "model"
    assert api.auth == "api-key"
    assert "Anthropic API key" in api.note
    assert subscription.kind == "agent"
    assert subscription.protocol == "acp"
    assert "reference adapter" in subscription.note
    assert "official `@agentclientprotocol" not in subscription.note


def test_codebuddy_exact_catalog_argv_admits_documented_empty_tool_allowlist() -> None:
    profile = Profile(
        "codebuddy-main",
        "codebuddy-acp",
        options={"allow_unsandboxed_agent": True},
    )

    profile.validate()
    assert agent_command_argv(profile) == list(get_preset("codebuddy-acp").command)
    assert CODEBUDDY_BREAD_MCP_PERMISSION == f"mcp__{BREAD_MCP_SERVER_NAME}"
    assert "mcp__*" not in profile.argv
    assert "-y" not in profile.argv
    assert "bypassPermissions" not in profile.argv

    with pytest.raises(ConfigurationError, match="invalid argument"):
        Profile(
            "custom-empty-argument",
            "custom-acp",
            command=["custom-agent", ""],
            options={"allow_unsandboxed_agent": True},
        ).validate()
