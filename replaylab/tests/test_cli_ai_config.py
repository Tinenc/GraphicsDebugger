from __future__ import annotations

import pytest

from replaylab_agent.agent_contracts import RunResult
from replaylab_agent.cli import _parser, _profile_command
from replaylab_agent.conversation import SUPPORTED_CONVERSATION_SCHEMAS
from replaylab_agent.errors import ConfigurationError
from replaylab_agent.process_env import (
    codebuddy_profile_home,
    codex_profile_home,
    cursor_profile_home,
)
from replaylab_agent.profiles import Profile, ProfileStore


def test_profile_machine_commands_manage_picker_models(tmp_path, monkeypatch) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    emitted: list[object] = []
    monkeypatch.setattr(cli, "_print_machine_json", emitted.append)
    parser = _parser()

    add = parser.parse_args(
        [
            "profile",
            "add",
            "local-main",
            "--preset",
            "ollama",
            "--title",
            "My Local AI",
            "--allowed-model",
            "model-a",
            "--allowed-model",
            "model-b",
            "--default-model",
            "model-a",
            "--model-context-window",
            "model-a=65536",
            "--model-context-window",
            "model-b=262144",
            "--select",
            "--json",
        ]
    )
    assert _profile_command(add, store) == 0
    assert emitted[-1]["id"] == "local-main"
    assert emitted[-1]["title"] == "My Local AI"
    assert emitted[-1]["models"] == ["model-a", "model-b"]
    assert emitted[-1]["defaultModel"] == "model-a"
    assert emitted[-1]["modelContextWindows"] == {
        "model-a": 65_536,
        "model-b": 262_144,
    }
    assert emitted[-1]["runInputSchemas"] == list(SUPPORTED_CONVERSATION_SCHEMAS)
    assert emitted[-1]["runInputSchemas"] == [
        "replaylab-conversation/v5",
        "replaylab-conversation/v4",
        "replaylab-conversation/v3",
        "replaylab-conversation/v2",
    ]

    update = parser.parse_args(
        [
            "profile",
            "update",
            "local-main",
            "--default-model",
            "model-b",
            "--json",
        ]
    )
    assert _profile_command(update, store) == 0
    assert emitted[-1]["model"] == "model-b"
    assert emitted[-1]["defaultModel"] == "model-b"
    reloaded = ProfileStore(store.path).load().active()
    assert reloaded.default_model == "model-b"
    assert reloaded.model == "model-b"
    assert reloaded.resolve_model() == "model-b"
    assert reloaded.model_context_windows == {
        "model-a": 65_536,
        "model-b": 262_144,
    }

    remove = parser.parse_args(
        ["profile", "remove", "local-main", "--json"]
    )
    assert _profile_command(remove, store) == 0
    assert emitted[-1] == {"removed": "local-main", "activeProfile": ""}


def test_profile_cli_parses_decorated_model_context_window_from_last_equals(
    tmp_path,
) -> None:
    parser = _parser()
    store = ProfileStore(tmp_path / "profiles.json")
    args = parser.parse_args(
        [
            "profile",
            "add",
            "cursor-window",
            "--preset",
            "cursor-acp",
            "--allowed-model",
            "composer-2.5[fast=true]",
            "--default-model",
            "composer-2.5[fast=true]",
            "--model-context-window",
            "composer-2.5[fast=true]=200000",
            "--option",
            "allow_unsandboxed_agent=true",
        ]
    )

    assert _profile_command(args, store) == 0
    assert store.active().model_context_windows == {
        "composer-2.5[fast=true]": 200_000
    }


def test_parser_exposes_secret_safe_model_discovery_and_auth_commands() -> None:
    parser = _parser()
    models = parser.parse_args(
        [
            "profile",
            "discover-models",
            "--preset",
            "custom-openai",
            "--base-url",
            "https://example.test/v1/chat/completions",
            "--api-key-stdin",
            "--json",
        ]
    )
    assert models.command == "profile"
    assert models.profile_command == "discover-models"
    assert models.api_key_stdin is True
    assert not hasattr(models, "api_key")

    login = parser.parse_args(
        [
            "profile",
            "subscription",
            "connect",
            "--profile",
            "codex",
            "--flow",
            "device",
            "--start-gated",
            "--json",
        ]
    )
    assert login.profile_command == "subscription"
    assert login.subscription_command == "connect"
    assert login.flow == "device"
    assert login.start_gated is True


def test_profile_machine_commands_round_trip_subscription_auth_flow(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    emitted: list[dict] = []
    monkeypatch.setattr(cli, "_print_machine_json", emitted.append)
    parser = _parser()

    add = parser.parse_args(
        [
            "profile",
            "add",
            "codex-device",
            "--preset",
            "codex",
            "--option",
            "subscription_auth_flow=device",
            "--json",
        ]
    )
    assert _profile_command(add, store) == 0
    assert emitted[-1]["options"]["subscription_auth_flow"] == "device"

    reloaded = ProfileStore(store.path).load().profiles["codex-device"]
    assert reloaded.options["subscription_auth_flow"] == "device"

    update = parser.parse_args(
        [
            "profile",
            "update",
            "codex-device",
            "--option",
            "subscription_auth_flow=browser",
            "--json",
        ]
    )
    assert _profile_command(update, store) == 0
    assert emitted[-1]["options"]["subscription_auth_flow"] == "browser"
    assert ProfileStore(store.path).load().profiles["codex-device"].options[
        "subscription_auth_flow"
    ] == "browser"


def test_provider_replacement_clears_custom_acp_state_but_preserves_identity(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "stable-id",
            "custom-acp",
            title="My Subscription",
            command=["old-agent", "--acp"],
            agent_env=["OLD_AGENT_TOKEN"],
            options={"allow_unsandboxed_agent": True},
            probed_capabilities=["chat"],
        )
    )
    emitted: list[dict] = []
    monkeypatch.setattr(cli, "_print_machine_json", emitted.append)
    args = _parser().parse_args(
        [
            "profile",
            "update",
            "stable-id",
            "--replace-provider",
            "--preset",
            "codex",
            "--json",
        ]
    )

    assert _profile_command(args, store) == 0
    updated = store.active()
    assert updated.id == "stable-id"
    assert updated.title == "My Subscription"
    assert updated.preset == "codex"
    assert updated.command == []
    assert updated.argv == ["codex", "app-server"]
    assert updated.agent_env == []
    assert updated.options == {}
    assert updated.probed_capabilities is None
    assert updated.models == []
    assert updated.default_model == ""
    assert emitted[-1]["active"] is True


def test_codex_profile_remove_clears_only_profile_scoped_home(
    tmp_path, monkeypatch
) -> None:
    local = (tmp_path / "local").resolve()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    store = ProfileStore(tmp_path / "profiles.json")
    profile = Profile("codex-remove", "codex")
    store.add(profile)
    store.save()
    home = codex_profile_home(profile)
    home.mkdir(parents=True)
    (home / "auth.json").write_text("test-only", encoding="utf-8")

    args = _parser().parse_args(["profile", "remove", profile.id, "--json"])
    assert _profile_command(args, store) == 0
    assert not home.exists()
    assert local.is_dir()


def test_replacing_codex_provider_clears_profile_scoped_home(
    tmp_path, monkeypatch
) -> None:
    local = (tmp_path / "local").resolve()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    store = ProfileStore(tmp_path / "profiles.json")
    profile = Profile("codex-replace", "codex", title="Keep title")
    store.add(profile)
    store.save()
    home = codex_profile_home(profile)
    home.mkdir(parents=True)
    (home / "auth.json").write_text("test-only", encoding="utf-8")

    args = _parser().parse_args(
        [
            "profile",
            "update",
            profile.id,
            "--replace-provider",
            "--preset",
            "ollama",
            "--allowed-model",
            "test-model",
            "--default-model",
            "test-model",
            "--json",
        ]
    )
    assert _profile_command(args, store) == 0
    assert store.profiles[profile.id].preset == "ollama"
    assert store.profiles[profile.id].title == "Keep title"
    assert not home.exists()


def test_cursor_profile_remove_clears_only_profile_scoped_home(
    tmp_path, monkeypatch
) -> None:
    local = (tmp_path / "local").resolve()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    store = ProfileStore(tmp_path / "profiles.json")
    profile = Profile(
        "cursor-remove",
        "cursor-acp",
        options={"allow_unsandboxed_agent": True},
    )
    store.add(profile)
    store.save()
    home = cursor_profile_home(profile)
    home.mkdir(parents=True)
    (home / "auth.json").write_text("test-only", encoding="utf-8")

    args = _parser().parse_args(["profile", "remove", profile.id, "--json"])
    assert _profile_command(args, store) == 0
    assert not home.exists()
    assert local.is_dir()


def test_replacing_cursor_provider_clears_profile_scoped_home(
    tmp_path, monkeypatch
) -> None:
    local = (tmp_path / "local").resolve()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    store = ProfileStore(tmp_path / "profiles.json")
    profile = Profile(
        "cursor-replace",
        "cursor-acp",
        title="Keep title",
        options={"allow_unsandboxed_agent": True},
    )
    store.add(profile)
    store.save()
    home = cursor_profile_home(profile)
    home.mkdir(parents=True)
    (home / "auth.json").write_text("test-only", encoding="utf-8")

    args = _parser().parse_args(
        [
            "profile",
            "update",
            profile.id,
            "--replace-provider",
            "--preset",
            "ollama",
            "--allowed-model",
            "test-model",
            "--default-model",
            "test-model",
            "--json",
        ]
    )
    assert _profile_command(args, store) == 0
    assert store.profiles[profile.id].preset == "ollama"
    assert store.profiles[profile.id].title == "Keep title"
    assert not home.exists()


def test_codebuddy_profile_remove_clears_only_connection_scoped_settings(
    tmp_path, monkeypatch
) -> None:
    local = (tmp_path / "local").resolve()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    store = ProfileStore(tmp_path / "profiles.json")
    profile = Profile(
        "codebuddy-remove",
        "codebuddy-acp",
        options={"allow_unsandboxed_agent": True},
    )
    store.add(profile)
    store.save()
    home = codebuddy_profile_home(profile)
    home.mkdir(parents=True)
    (home / "settings.json").write_text("test-only", encoding="utf-8")

    args = _parser().parse_args(["profile", "remove", profile.id, "--json"])
    assert _profile_command(args, store) == 0
    assert profile.id not in store.profiles
    assert not home.exists()
    assert local.is_dir()


def test_replacing_codebuddy_provider_clears_connection_scoped_settings(
    tmp_path, monkeypatch
) -> None:
    local = (tmp_path / "local").resolve()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    store = ProfileStore(tmp_path / "profiles.json")
    profile = Profile(
        "codebuddy-replace",
        "codebuddy-acp",
        title="Keep title",
        options={"allow_unsandboxed_agent": True},
    )
    store.add(profile)
    store.save()
    home = codebuddy_profile_home(profile)
    home.mkdir(parents=True)
    (home / "settings.json").write_text("test-only", encoding="utf-8")

    args = _parser().parse_args(
        [
            "profile",
            "update",
            profile.id,
            "--replace-provider",
            "--preset",
            "ollama",
            "--allowed-model",
            "test-model",
            "--default-model",
            "test-model",
            "--json",
        ]
    )
    assert _profile_command(args, store) == 0
    assert store.profiles[profile.id].preset == "ollama"
    assert store.profiles[profile.id].title == "Keep title"
    assert not home.exists()


def test_codebuddy_remove_failure_retains_login_but_removes_connection(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    profile = Profile(
        "codebuddy-remove-failure",
        "codebuddy-acp",
        options={"allow_unsandboxed_agent": True},
    )
    store.add(profile)
    store.save()

    def fail_remove(_profile):
        raise ConfigurationError("contained state is busy")

    monkeypatch.setattr(cli, "remove_codebuddy_profile_home", fail_remove)
    args = _parser().parse_args(["profile", "remove", profile.id, "--json"])

    assert _profile_command(args, store) == 0
    assert profile.id not in store.profiles
    assert profile.id not in ProfileStore(store.path).load().profiles


def test_api_provider_replacement_clears_old_endpoint_key_options_and_models(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "stable-api",
            "openai",
            model="old-model",
            models=["old-model", "other-old-model"],
            default_model="old-model",
            title="Production API",
            base_url="https://old.example.test/v1",
            api_key_env="OLD_VENDOR_API_KEY",
            options={"temperature": 0.5},
            probed_capabilities=["tools"],
        )
    )
    monkeypatch.setattr(cli, "_print_machine_json", lambda value: None)
    args = _parser().parse_args(
        [
            "profile",
            "update",
            "stable-api",
            "--replace-provider",
            "--preset",
            "anthropic",
            "--allowed-model",
            "claude-new",
            "--default-model",
            "claude-new",
            "--json",
        ]
    )

    assert _profile_command(args, store) == 0
    updated = store.active()
    assert updated.id == "stable-api"
    assert updated.title == "Production API"
    assert updated.preset == "anthropic"
    assert updated.endpoint == "https://api.anthropic.com"
    assert updated.key_env == "ANTHROPIC_API_KEY"
    assert updated.models == ["claude-new"]
    assert updated.default_model == "claude-new"
    assert updated.options == {}
    assert updated.probed_capabilities is None
    encoded = store.path.read_text(encoding="utf-8")
    assert "old.example.test" not in encoded
    assert "OLD_VENDOR_API_KEY" not in encoded
    assert "old-model" not in encoded


def test_cross_preset_update_requires_explicit_replacement(tmp_path) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(Profile("stable", "ollama", "old-model"))
    args = _parser().parse_args(
        [
            "profile",
            "update",
            "stable",
            "--preset",
            "openai",
            "--allowed-model",
            "new-model",
            "--default-model",
            "new-model",
        ]
    )
    with pytest.raises(ValueError, match="requires --replace-provider"):
        _profile_command(args, store)
    assert store.active().preset == "ollama"


def test_parser_exposes_per_run_model_override() -> None:
    parser = _parser()
    run = parser.parse_args(
        ["run", "--profile", "p", "--model", "m", "--prompt", "hello"]
    )
    assert run.model == "m"


@pytest.mark.asyncio
async def test_run_forwards_model_override_only_for_this_run(tmp_path, monkeypatch) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "local",
            "ollama",
            model="model-a",
            models=["model-a", "model-b"],
            default_model="model-a",
        )
    )

    observed = {}

    class FakeEngine:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def run(self, request):
            observed["request"] = request
            return RunResult(
                task_id="task",
                profile_id="local",
                executor="Ollama",
                text="ok",
                terminal_status="completed",
            )

    monkeypatch.setattr(cli, "DeepAgentsEngine", FakeEngine)
    args = _parser().parse_args(
        ["run", "--profile", "local", "--model", "model-b", "--prompt", "hello"]
    )
    assert await cli._run_once(args, store) == 0
    assert observed["request"].prompt == "hello"
    assert observed["request"].model == "model-b"
    assert store.active().model == "model-a"
