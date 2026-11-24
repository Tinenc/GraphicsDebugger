from __future__ import annotations

import errno
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from replaylab_agent.connectors.subscription import (
    SubscriptionBackend,
    detect_subscription,
)
from replaylab_agent.doctor import run_doctor
from replaylab_agent.errors import ConfigurationError
from replaylab_agent.process_env import (
    agent_command_argv,
    codebuddy_profile_home,
    codex_profile_home,
    cursor_profile_home,
    ensure_cursor_profile_config,
    external_agent_environment,
    host_process_environment,
    managed_vendor_profile_home,
    remove_codebuddy_profile_home,
    remove_codex_profile_home,
    remove_cursor_profile_home,
    remove_managed_vendor_profile_home,
    resolve_agent_executable,
    resolve_known_vendor_executable,
    resolved_agent_argv,
)
from replaylab_agent.profiles import Profile


def _write_file(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o755)
    return path


def _write_bundled_codex(local_app_data: Path, version: str = "version-a") -> Path:
    return _write_file(
        local_app_data / "OpenAI" / "Codex" / "bin" / version / "codex.exe",
        b"MZ" + (b"\0" * (64 * 1024 - 2)),
    )


def _windows_fallbacks(monkeypatch) -> None:
    import replaylab_agent.process_env as process_env

    monkeypatch.setattr(process_env, "_is_windows_platform", lambda: True)


def test_known_vendor_path_has_priority_over_bundled_codex(tmp_path, monkeypatch) -> None:
    _windows_fallbacks(monkeypatch)
    path_executable = _write_file(tmp_path / "path-bin" / "codex", b"path-codex")
    local = tmp_path / "local"
    local.mkdir()
    _write_bundled_codex(local)

    resolved = resolve_known_vendor_executable(
        "codex",
        "codex",
        environ={"PATH": str(path_executable.parent), "LOCALAPPDATA": str(local)},
    )

    assert resolved == str(path_executable.resolve())


def test_known_vendor_skips_windowsapps_alias_then_uses_bundled_codex(
    tmp_path, monkeypatch
) -> None:
    _windows_fallbacks(monkeypatch)
    alias = _write_file(
        tmp_path
        / "AppData"
        / "Local"
        / "Microsoft"
        / "WindowsApps"
        / "codex",
        b"packaged-alias",
    )
    local = tmp_path / "local"
    local.mkdir()
    bundled = _write_bundled_codex(local)
    environment = {
        "PATH": str(alias.parent),
        "LOCALAPPDATA": str(local),
    }

    assert resolve_known_vendor_executable(
        "codex", "codex", environ=environment
    ) == str(bundled.resolve())
    # Custom ACP remains an explicit-command contract and does not receive
    # catalog fallback behavior, even when its PATH entry is a packaged alias.
    assert resolve_agent_executable(
        Profile(
            "custom",
            "custom-acp",
            command=["codex"],
            options={"allow_unsandboxed_agent": True},
        ),
        environ=environment,
    ) == str(alias.resolve())


def test_codex_bundled_one_level_version_layout_is_resolved(tmp_path, monkeypatch) -> None:
    _windows_fallbacks(monkeypatch)
    local = tmp_path / "local"
    local.mkdir()
    executable = _write_bundled_codex(local, "d0097be4feba73d0")

    resolved = resolve_agent_executable(
        Profile("codex-main", "codex", command=["ignored-user-command"]),
        environ={"PATH": "", "LOCALAPPDATA": str(local)},
    )

    assert resolved == str(executable.resolve())
    assert agent_command_argv(Profile("codex-main", "codex")) == [
        "codex",
        "app-server",
    ]


def test_cursor_fixed_windows_install_is_resolved_and_catalog_command_is_immutable(
    tmp_path, monkeypatch
) -> None:
    _windows_fallbacks(monkeypatch)
    local = tmp_path / "local"
    local.mkdir()
    executable = _write_file(
        local / "cursor-agent" / "cursor-agent.exe",
        b"MZ" + (b"\0" * (64 * 1024 - 2)),
    )
    profile = Profile(
        "cursor-main",
        "cursor-acp",
        command=["ignored-user-command"],
        options={"allow_unsandboxed_agent": True},
    )

    assert resolve_agent_executable(
        profile, environ={"PATH": "", "LOCALAPPDATA": str(local)}
    ) == str(executable.resolve())
    assert agent_command_argv(profile) == ["cursor-agent", "acp"]


def test_cursor_fixed_windows_cmd_fallback_does_not_trust_npm_agent_alias(
    tmp_path, monkeypatch
) -> None:
    _windows_fallbacks(monkeypatch)
    local = tmp_path / "local"
    local.mkdir()
    appdata = tmp_path / "roaming"
    appdata.mkdir()
    official = _write_file(
        local / "cursor-agent" / "cursor-agent.cmd", b"@echo off\r\n"
    )
    _write_file(appdata / "npm" / "agent.cmd", b"@echo untrusted\r\n")
    environment = {
        "PATH": "",
        "LOCALAPPDATA": str(local),
        "APPDATA": str(appdata),
    }

    assert resolve_known_vendor_executable(
        "cursor-acp", "cursor-agent", environ=environment
    ) == str(official.resolve())

    official.unlink()
    assert (
        resolve_known_vendor_executable(
            "cursor-acp", "cursor-agent", environ=environment
        )
        == ""
    )


def test_codex_fallback_rejects_nested_small_non_pe_and_reparse_files(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.process_env as process_env

    _windows_fallbacks(monkeypatch)
    local = tmp_path / "local"
    local.mkdir()
    nested = local / "OpenAI" / "Codex" / "bin" / "v1" / "nested" / "codex.exe"
    _write_file(nested, b"MZ" + (b"\0" * (64 * 1024 - 2)))
    environment = {"PATH": "", "LOCALAPPDATA": str(local)}
    assert resolve_known_vendor_executable("codex", "codex", environ=environment) == ""

    direct = nested.parent.parent / "codex.exe"
    _write_file(direct, b"MZ-too-small")
    assert resolve_known_vendor_executable("codex", "codex", environ=environment) == ""

    _write_file(direct, b"ZZ" + (b"\0" * (64 * 1024 - 2)))
    assert resolve_known_vendor_executable("codex", "codex", environ=environment) == ""

    _write_file(direct, b"MZ" + (b"\0" * (64 * 1024 - 2)))
    original_is_reparse = process_env._is_reparse
    monkeypatch.setattr(
        process_env,
        "_is_reparse",
        lambda info: stat.S_ISREG(info.st_mode) or original_is_reparse(info),
    )
    assert resolve_known_vendor_executable("codex", "codex", environ=environment) == ""


def test_codex_version_enumeration_is_bounded(tmp_path, monkeypatch) -> None:
    _windows_fallbacks(monkeypatch)
    local = tmp_path / "local"
    bin_root = local / "OpenAI" / "Codex" / "bin"
    bin_root.mkdir(parents=True)
    for index in range(65):
        (bin_root / f"v{index:02d}").mkdir()
    _write_file(
        bin_root / "v00" / "codex.exe",
        b"MZ" + (b"\0" * (64 * 1024 - 2)),
    )

    assert (
        resolve_known_vendor_executable(
            "codex", "codex", environ={"PATH": "", "LOCALAPPDATA": str(local)}
        )
        == ""
    )


def test_known_npm_shim_is_literal_and_custom_acp_does_not_use_fallback(
    tmp_path, monkeypatch
) -> None:
    _windows_fallbacks(monkeypatch)
    appdata = tmp_path / "roaming"
    appdata.mkdir()
    shim = _write_file(appdata / "npm" / "gemini.cmd", b"@echo off\r\n")
    codebuddy_shim = _write_file(
        appdata / "npm" / "codebuddy.cmd", b"@echo off\r\n"
    )
    environment = {"PATH": "", "APPDATA": str(appdata)}

    assert resolve_known_vendor_executable(
        "gemini-acp", "gemini", environ=environment
    ) == str(shim.resolve())
    assert resolve_known_vendor_executable(
        "codebuddy-acp", "codebuddy", environ=environment
    ) == str(codebuddy_shim.resolve())
    assert (
        resolve_agent_executable(
            Profile(
                "custom",
                "custom-acp",
                command=["gemini"],
                options={"allow_unsandboxed_agent": True},
            ),
            environ=environment,
        )
        == ""
    )
    assert resolve_agent_executable(
        Profile(
            "custom-absolute",
            "custom-acp",
            command=[sys.executable],
            options={"allow_unsandboxed_agent": True},
        ),
        environ=environment,
    ) == str(Path(sys.executable).resolve())


def test_detect_spawn_resolution_and_doctor_share_absolute_codex_path(
    tmp_path, monkeypatch
) -> None:
    _windows_fallbacks(monkeypatch)
    local = tmp_path / "local"
    local.mkdir()
    executable = _write_bundled_codex(local)
    environment = {"PATH": "", "LOCALAPPDATA": str(local)}
    profile = Profile(
        "codex-main",
        "codex",
        "gpt-5.6-sol",
        command=["ignored-user-command"],
    )

    status = detect_subscription(profile, environ=environment)
    assert status["installed"] is True
    assert status["messageId"] == "subscription.cli-detected"
    assert resolved_agent_argv(profile, environ=environment) == [
        str(executable.resolve()),
        "app-server",
    ]

    backend = SubscriptionBackend(
        profile,
        timeout=5,
        emit=lambda event: None,
        environ=environment,
    )
    try:
        assert backend._resolved_profile().argv == [
            str(executable.resolve()),
            "app-server",
        ]
    finally:
        backend.close()

    checks = run_doctor(
        profile,
        SimpleNamespace(skills={}),
        bridge=SimpleNamespace(instances=lambda: []),
        mcp_executable=Path(sys.executable),
        environ=environment,
    )
    command_check = next(check for check in checks if check.name == "agent-command")
    assert command_check.status == "ok"
    assert command_check.detail == str(executable.resolve())
    registration = next(
        check for check in checks if check.name == "codex-internal-tools"
    )
    assert registration.status == "ok"
    assert "task-scoped Bread MCP" in registration.detail
    assert "no persistent MCP registration" in registration.detail
    context = next(check for check in checks if check.name == "model-context")
    assert context.status == "ok"
    assert context.detail == "1050000 tokens (model-catalog)"


def test_codex_environment_uses_profile_scoped_home_instead_of_global_home(
    tmp_path,
) -> None:
    profile = Profile("codex-main", "codex")
    source = {
        "LOCALAPPDATA": str(tmp_path / "local"),
        "CODEX_HOME": str(tmp_path / "global-codex-home"),
        "PATH": str(tmp_path / "bin"),
    }

    environment = external_agent_environment(profile, environ=source)

    expected = (
        tmp_path / "local" / "ReplayLab" / "codex" / profile.id
    ).resolve()
    assert environment["CODEX_HOME"] == str(expected)
    assert codex_profile_home(profile, environ=source) == expected
    # Environment construction is side-effect free; the official Codex process
    # creates the profile home when login or app-server first needs it.
    assert not expected.exists()


def test_cursor_environment_and_permission_config_are_profile_scoped(
    tmp_path,
) -> None:
    profile = Profile(
        "cursor-main",
        "cursor-acp",
        options={"allow_unsandboxed_agent": True},
    )
    global_config = tmp_path / "global-cursor"
    source = {
        "LOCALAPPDATA": str(tmp_path / "local"),
        "CURSOR_CONFIG_DIR": str(global_config),
        "PATH": str(tmp_path / "bin"),
    }

    environment = external_agent_environment(profile, environ=source)
    expected = (
        tmp_path / "local" / "ReplayLab" / "cursor" / profile.id
    ).resolve()
    assert environment["CURSOR_CONFIG_DIR"] == str(expected)
    assert environment["HOME"] == str(expected)
    assert environment["USERPROFILE"] == str(expected)
    assert cursor_profile_home(profile, environ=source) == expected
    assert not expected.exists()

    config_path = ensure_cursor_profile_config(profile, environ=source)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["approvalMode"] == "allowlist"
    assert payload["permissions"]["allow"] == ["Mcp(Bread:*)"]
    assert payload["permissions"]["deny"] == [
        "Shell(*)",
        "Read(**)",
        "Write(**)",
        "WebFetch(*)",
        "Agent(*)",
        "Task(*)",
        "Subagent(*)",
    ]
    assert "Mcp(*:*)" not in payload["permissions"]["allow"]
    assert json.loads(
        (expected / ".cursor" / "mcp.json").read_text(encoding="utf-8")
    ) == {"mcpServers": {}}
    assert not global_config.exists()


def test_codebuddy_environment_is_profile_scoped_and_ignores_global_config(
    tmp_path,
) -> None:
    profile = Profile(
        "codebuddy-main",
        "codebuddy-acp",
        options={"allow_unsandboxed_agent": True},
    )
    global_config = tmp_path / "global-codebuddy"
    source = {
        "LOCALAPPDATA": str(tmp_path / "local"),
        "CODEBUDDY_CONFIG_DIR": str(global_config),
        "PATH": str(tmp_path / "bin"),
    }

    environment = external_agent_environment(profile, environ=source)
    expected = (
        tmp_path / "local" / "ReplayLab" / "codebuddy" / profile.id
    ).resolve()

    assert environment["CODEBUDDY_CONFIG_DIR"] == str(expected)
    assert codebuddy_profile_home(profile, environ=source) == expected
    assert environment.get("HOME") != str(expected)
    assert not expected.exists()


@pytest.mark.parametrize(
    ("preset", "directory", "environment_key"),
    [
        ("copilot-acp", "copilot", "COPILOT_HOME"),
        ("gemini-acp", "gemini", "GEMINI_CLI_HOME"),
        ("kimi-acp", "kimi", "KIMI_CODE_HOME"),
        ("claude-code-acp", "claude", "CLAUDE_CONFIG_DIR"),
    ],
)
def test_other_subscription_vendor_state_is_profile_scoped(
    tmp_path, preset, directory, environment_key
) -> None:
    profile = Profile(
        preset + "-main",
        preset,
        options={"allow_unsandboxed_agent": True},
    )
    source = {
        "LOCALAPPDATA": str(tmp_path / "local"),
        environment_key: str(tmp_path / "global-vendor-state"),
        "PATH": str(tmp_path / "bin"),
    }

    environment = external_agent_environment(profile, environ=source)
    expected = (
        tmp_path / "local" / "ReplayLab" / directory / profile.id
    ).resolve()
    assert environment[environment_key] == str(expected)
    assert managed_vendor_profile_home(profile, environ=source) == expected
    assert not expected.exists()
    if preset in {"gemini-acp", "kimi-acp"}:
        assert environment["HOME"] == str(expected)
        assert environment["USERPROFILE"] == str(expected)


def test_qwen_uses_managed_os_home_and_encrypted_token_storage(tmp_path) -> None:
    profile = Profile(
        "qwen-main",
        "qwen-acp",
        options={"allow_unsandboxed_agent": True},
    )
    source = {
        "LOCALAPPDATA": str(tmp_path / "local"),
        "HOME": str(tmp_path / "global-home"),
        "USERPROFILE": str(tmp_path / "global-profile"),
    }
    environment = external_agent_environment(profile, environ=source)
    expected = managed_vendor_profile_home(profile, environ=source)
    assert environment["HOME"] == environment["USERPROFILE"] == str(expected)
    assert environment["QWEN_CODE_FORCE_ENCRYPTED_FILE_STORAGE"] == "true"


def test_managed_vendor_home_removal_leaves_global_state_untouched(tmp_path) -> None:
    profile = Profile(
        "kimi-delete",
        "kimi-acp",
        options={"allow_unsandboxed_agent": True},
    )
    local = (tmp_path / "local").resolve()
    global_home = tmp_path / "global-kimi"
    global_sentinel = global_home / "credentials" / "account.json"
    global_sentinel.parent.mkdir(parents=True)
    global_sentinel.write_text("global-must-survive", encoding="utf-8")
    target = managed_vendor_profile_home(
        profile, environ={"LOCALAPPDATA": str(local)}
    )
    (target / "credentials").mkdir(parents=True)
    (target / "credentials" / "account.json").write_text(
        "profile-only", encoding="utf-8"
    )

    remove_managed_vendor_profile_home(
        profile,
        environ={
            "LOCALAPPDATA": str(local),
            "KIMI_CODE_HOME": str(global_home),
        },
    )

    assert not target.exists()
    assert global_sentinel.read_text(encoding="utf-8") == "global-must-survive"


def test_codebuddy_home_removal_leaves_global_state_untouched(tmp_path) -> None:
    profile = Profile(
        "codebuddy-delete",
        "codebuddy-acp",
        options={"allow_unsandboxed_agent": True},
    )
    local = (tmp_path / "local").resolve()
    global_config = tmp_path / "global-codebuddy"
    global_sentinel = global_config / "settings.json"
    global_sentinel.parent.mkdir(parents=True)
    global_sentinel.write_text("global-must-survive", encoding="utf-8")
    target = codebuddy_profile_home(profile, environ={"LOCALAPPDATA": str(local)})
    (target / "settings.json").parent.mkdir(parents=True)
    (target / "settings.json").write_text("profile-only", encoding="utf-8")

    remove_codebuddy_profile_home(
        profile,
        environ={
            "LOCALAPPDATA": str(local),
            "CODEBUDDY_CONFIG_DIR": str(global_config),
        },
    )

    assert not target.exists()
    assert global_sentinel.read_text(encoding="utf-8") == "global-must-survive"


def test_cursor_home_removal_leaves_global_cursor_state_untouched(tmp_path) -> None:
    profile = Profile(
        "cursor-delete",
        "cursor-acp",
        options={"allow_unsandboxed_agent": True},
    )
    local = (tmp_path / "local").resolve()
    global_config = tmp_path / "global-cursor"
    global_sentinel = global_config / "auth.json"
    global_sentinel.parent.mkdir(parents=True)
    global_sentinel.write_text("global-must-survive", encoding="utf-8")
    target = cursor_profile_home(profile, environ={"LOCALAPPDATA": str(local)})
    (target / "auth.json").parent.mkdir(parents=True)
    (target / "auth.json").write_text("profile-only", encoding="utf-8")

    remove_cursor_profile_home(
        profile,
        environ={
            "LOCALAPPDATA": str(local),
            "CURSOR_CONFIG_DIR": str(global_config),
        },
    )

    assert not target.exists()
    assert global_sentinel.read_text(encoding="utf-8") == "global-must-survive"


def test_cursor_config_preparation_refuses_profile_directory_link(tmp_path) -> None:
    profile = Profile(
        "cursor-linked",
        "cursor-acp",
        options={"allow_unsandboxed_agent": True},
    )
    local = (tmp_path / "local").resolve()
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    target = cursor_profile_home(profile, environ={"LOCALAPPDATA": str(local)})
    target.parent.mkdir(parents=True)
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available on this Windows host")

    with pytest.raises(ConfigurationError, match="link/reparse point"):
        ensure_cursor_profile_config(profile, environ={"LOCALAPPDATA": str(local)})
    assert not (outside / "cli-config.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not Windows ACLs")
def test_cursor_profile_state_uses_private_posix_modes(tmp_path) -> None:
    profile = Profile(
        "cursor-private",
        "cursor-acp",
        options={"allow_unsandboxed_agent": True},
    )
    config = ensure_cursor_profile_config(
        profile, environ={"XDG_DATA_HOME": str(tmp_path.resolve())}
    )
    assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_child_environments_force_utf8_and_preserve_chinese_paths(tmp_path) -> None:
    chinese_local = tmp_path / "用户数据" / "配置"
    source = {
        "LOCALAPPDATA": str(chinese_local),
        "PATH": str(tmp_path / "工具"),
        # Locale-dependent inherited values must never override the protocol.
        "PYTHONIOENCODING": "cp936:replace",
        "PYTHONUTF8": "0",
    }

    agent = external_agent_environment(Profile("unicode-profile", "codex"), environ=source)
    host = host_process_environment(
        {"REPLAYLAB_INSTANCE_ID": "实例-一"}, environ=source
    )

    assert agent["PYTHONIOENCODING"] == "utf-8:strict"
    assert agent["PYTHONUTF8"] == "1"
    assert "用户数据" in agent["CODEX_HOME"]
    assert host["PYTHONIOENCODING"] == "utf-8:strict"
    assert host["PYTHONUTF8"] == "1"
    assert host["REPLAYLAB_INSTANCE_ID"] == "实例-一"


def test_managed_provider_paths_are_visible_only_to_mcp_host(tmp_path) -> None:
    provider_values = {
        "RENDERDOC_ULTRA_UNPACKED_ASSET_JOB_ROOT": str(tmp_path / "jobs"),
        "RENDERDOC_ULTRA_RESOURCE_EXPORT_ROOT": str(tmp_path / "resource-exports"),
        "RENDERDOC_ULTRA_UEX": str(tmp_path / "uex.exe"),
        "RENDERDOC_ULTRA_UEX_PROFILES": str(tmp_path / "profiles.json"),
        "RENDERDOC_ULTRA_BLENDER": str(tmp_path / "blender.exe"),
        "RENDERDOC_ULTRA_GLTF_VALIDATOR": str(tmp_path / "gltf_validator.exe"),
        "RENDERDOC_ULTRA_UNITY_VALIDATOR": str(tmp_path / "unity-validator.exe"),
        "RENDERDOC_ULTRA_UEX_BLENDER_WRAPPER": str(tmp_path / "wrapper.py"),
        "RENDERDOC_ULTRA_UEX_BLENDER_CONTINUATION_DRIVER": str(
            tmp_path / "continuation-driver.py"
        ),
        "RENDERDOC_ULTRA_UEFORMAT_ADDON": str(tmp_path / "io_scene_ueformat"),
        "RENDERDOC_ULTRA_UEX_PYTHON": str(tmp_path / "python.exe"),
    }
    source = {"PATH": str(tmp_path / "bin"), **provider_values}
    profile = Profile(
        "custom-provider-isolation",
        "custom-acp",
        command=[sys.executable],
        options={"allow_unsandboxed_agent": True},
    )

    agent = external_agent_environment(profile, environ=source)
    host = host_process_environment(environ=source)

    assert provider_values.items() <= host.items()
    assert set(provider_values).isdisjoint(agent)


def test_non_codex_agent_keeps_explicit_inherited_codex_home(tmp_path) -> None:
    source = {
        "CODEX_HOME": str(tmp_path / "vendor-owned-home"),
        "PATH": str(tmp_path / "bin"),
    }
    profile = Profile(
        "custom",
        "custom-acp",
        command=[sys.executable],
        options={"allow_unsandboxed_agent": True},
    )
    assert external_agent_environment(profile, environ=source)["CODEX_HOME"] == source[
        "CODEX_HOME"
    ]


def test_codex_home_removal_handles_read_only_git_pack_and_leaves_global_home(
    tmp_path,
) -> None:
    profile = Profile("codex-delete", "codex")
    local = (tmp_path / "local").resolve()
    global_home = tmp_path / "global-codex"
    global_sentinel = global_home / "auth.json"
    global_sentinel.parent.mkdir(parents=True)
    global_sentinel.write_text("global-must-survive", encoding="utf-8")
    target = codex_profile_home(profile, environ={"LOCALAPPDATA": str(local)})
    locked_pack = target / "tmp" / "plugins-clone" / ".git" / "objects" / "pack" / "tmp_pack_test"
    locked_pack.parent.mkdir(parents=True)
    locked_pack.write_bytes(b"pack")
    locked_pack.chmod(stat.S_IREAD)

    remove_codex_profile_home(
        profile,
        environ={"LOCALAPPDATA": str(local), "CODEX_HOME": str(global_home)},
    )

    assert not target.exists()
    assert not list(target.parent.glob(f".delete-{profile.id}-*"))
    assert global_sentinel.read_text(encoding="utf-8") == "global-must-survive"


def test_codex_home_removal_retries_transient_pack_lock(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.process_env as process_env

    profile = Profile("codex-retry", "codex")
    local = (tmp_path / "local").resolve()
    target = codex_profile_home(profile, environ={"LOCALAPPDATA": str(local)})
    (target / "tmp" / "plugins-clone").mkdir(parents=True)
    real_rmtree = process_env.shutil.rmtree
    attempts = 0
    sleeps: list[float] = []

    def transient_rmtree(path, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise PermissionError(errno.EACCES, "simulated git/AV pack lock", str(path))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(process_env.shutil, "rmtree", transient_rmtree)
    monkeypatch.setattr(process_env.time, "sleep", sleeps.append)

    remove_codex_profile_home(profile, environ={"LOCALAPPDATA": str(local)})

    assert attempts == 3
    assert sleeps == [0.05, 0.10]
    assert not target.exists()


def test_codex_home_removal_restores_profile_after_persistent_lock(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.process_env as process_env

    profile = Profile("codex-busy", "codex")
    local = (tmp_path / "local").resolve()
    target = codex_profile_home(profile, environ={"LOCALAPPDATA": str(local)})
    sentinel = target / "auth.json"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("still-attached", encoding="utf-8")

    def always_busy(path, *args, **kwargs):
        raise PermissionError(errno.EACCES, "simulated persistent handle", str(path))

    monkeypatch.setattr(process_env.shutil, "rmtree", always_busy)
    monkeypatch.setattr(process_env.time, "sleep", lambda _delay: None)

    with pytest.raises(ConfigurationError, match="cannot remove managed Codex state"):
        remove_codex_profile_home(profile, environ={"LOCALAPPDATA": str(local)})

    assert sentinel.read_text(encoding="utf-8") == "still-attached"
    assert not list(target.parent.glob(f".delete-{profile.id}-*"))


def test_codex_home_removal_does_not_follow_nested_directory_link(tmp_path) -> None:
    profile = Profile("codex-nested-link", "codex")
    local = (tmp_path / "local").resolve()
    outside = (tmp_path / "outside").resolve()
    outside_sentinel = outside / "must-survive.txt"
    outside_sentinel.parent.mkdir()
    outside_sentinel.write_text("outside", encoding="utf-8")
    target = codex_profile_home(profile, environ={"LOCALAPPDATA": str(local)})
    target.mkdir(parents=True)
    nested_link = target / "vendor-cache-link"
    try:
        nested_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available on this Windows host")

    remove_codex_profile_home(profile, environ={"LOCALAPPDATA": str(local)})

    assert not target.exists()
    assert outside_sentinel.read_text(encoding="utf-8") == "outside"


def test_codex_home_removal_refuses_link_target(tmp_path) -> None:
    profile = Profile("codex-linked", "codex")
    local = (tmp_path / "local").resolve()
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    target = codex_profile_home(profile, environ={"LOCALAPPDATA": str(local)})
    target.parent.mkdir(parents=True)
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available on this Windows host")
    with pytest.raises(ConfigurationError, match="link/reparse point"):
        remove_codex_profile_home(profile, environ={"LOCALAPPDATA": str(local)})
    assert outside.is_dir()


def test_generic_resolver_ignores_relative_path_entries(tmp_path) -> None:
    from replaylab_agent.process_env import resolve_executable

    _write_file(tmp_path / "codex", b"not-trusted-from-cwd")
    assert resolve_executable("codex", environ={"PATH": "."}) == ""
