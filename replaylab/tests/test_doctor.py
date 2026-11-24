from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import json
import pytest

from replaylab_agent import doctor
from replaylab_agent.doctor import _is_packaged_windows_executable, run_doctor
from replaylab_agent.profiles import Profile
from replaylab_agent.skills import SkillCatalog


@pytest.fixture(autouse=True)
def no_real_claude_auth_process(monkeypatch):
    unexpected = AsyncMock(side_effect=AssertionError("unexpected real Claude auth check"))
    monkeypatch.setattr(doctor, "check_claude_auth_status", unexpected)
    yield
    unexpected.assert_not_awaited()


@pytest.fixture
def claude_doctor(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "resolve_agent_executable",
                        lambda *args, **kwargs: "C:/test/claude-code-acp.exe")
    monkeypatch.setattr(doctor, "_is_packaged_windows_executable", lambda unused: False)
    profile = Profile("claude-isolated", "claude-code-acp",
                      options={"allow_unsandboxed_agent": True})
    environ = {"PATH": "", "LOCALAPPDATA": str(tmp_path / "local"),
               "CLAUDE_CONFIG_DIR": str(tmp_path / "ambient-claude")}

    def run():
        return run_doctor(profile, SimpleNamespace(skills={"one": object()}),
                          bridge=SimpleNamespace(instances=lambda: []),
                          include_external_mcp=False, environ=environ)

    return profile, environ, run


@pytest.mark.parametrize("state", ["authenticated", "unauthenticated", "cli-unavailable",
                                  "invalid-response", "process-failed", "output-limit", "timed-out"])
def test_doctor_checks_claude_sign_in_in_the_selected_profile(monkeypatch, claude_doctor, state):
    profile, environ, run = claude_doctor
    result = SimpleNamespace(
        logged_in=state == "authenticated", state=state, detail="bounded status: " + state,
        message_id=("subscription.claude-account-signed-in" if state == "authenticated" else
                    "subscription.claude-account-unauthenticated" if state == "unauthenticated" else
                    "subscription.claude-auth-status-failed"),
        message_args=() if state in {"authenticated", "unauthenticated"} else (state,))
    check_auth = AsyncMock(return_value=result)
    monkeypatch.setattr(doctor, "check_claude_auth_status", check_auth)
    check = next(item for item in run() if item.name == "claude-authentication")

    check_auth.assert_awaited_once_with(profile, environ=environ, timeout=5.0)
    assert check.status == ("ok" if state == "authenticated" else "error")
    assert check.passed is (state == "authenticated")
    assert check.detail == result.detail
    assert check.message_id == result.message_id
    assert check.message_args == result.message_args


@pytest.mark.parametrize("prerequisite", ["trust", "profile", "command", "packaged-command"])
def test_doctor_never_launches_claude_when_a_prerequisite_fails(monkeypatch, claude_doctor, prerequisite):
    profile, _, run = claude_doctor
    if prerequisite == "trust":
        profile.options["allow_unsandboxed_agent"] = False
    elif prerequisite == "profile":
        profile.id = "invalid/profile"
    elif prerequisite == "command":
        monkeypatch.setattr(doctor, "resolve_agent_executable", lambda *args, **kwargs: "")
    else:
        monkeypatch.setattr(doctor, "_is_packaged_windows_executable", lambda unused: True)
    checks = run()
    check = next(item for item in checks if item.name == "claude-authentication")
    assert check.status == "warning"
    assert check.message_id == "claude-auth.skipped"
    assert not check.passed
    assert any(item.status == "error" for item in checks)


@pytest.mark.parametrize("preset", ["codex", "cursor-acp", "gemini-acp"])
def test_doctor_does_not_run_claude_auth_for_other_vendors(claude_doctor, preset):
    profile, _, run = claude_doctor
    profile.preset = preset
    if preset == "codex":
        profile.options.clear()
    assert all(item.name != "claude-authentication" for item in run())


def test_doctor_auth_checker_failure_is_an_error_without_raw_output(monkeypatch, claude_doctor):
    _, _, run = claude_doctor
    secret = "raw-cli-output-must-not-escape"
    monkeypatch.setattr(doctor, "check_claude_auth_status",
                        AsyncMock(side_effect=RuntimeError(secret)))
    checks = run()
    check = next(item for item in checks if item.name == "claude-authentication")
    assert check.status == "error"
    assert check.message_id == "subscription.claude-auth-status-failed"
    assert check.message_args == ("process-failed",)
    assert secret not in repr(checks)


def test_windowsapps_executable_is_not_reported_as_launchable() -> None:
    path = r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0_x64\codex.exe"
    assert _is_packaged_windows_executable(path) is (os.name == "nt")


def test_doctor_checks_current_default_and_every_enabled_model() -> None:
    profile = Profile(
        "multi-context",
        "codex",
        model="current-model",
        models=["default-model", "current-model", "third-model"],
        default_model="default-model",
        model_context_windows={
            "current-model": 65_536,
            "default-model": 131_072,
            "third-model": 262_144,
        },
    )

    checks = run_doctor(
        profile,
        SimpleNamespace(skills={"one": object()}),
        bridge=SimpleNamespace(instances=lambda: []),
        mcp_executable=Path(sys.executable),
        environ={"PATH": ""},
    )
    contexts = [check for check in checks if check.name.startswith("model-context")]

    assert [(check.status, check.detail) for check in contexts] == [
        ("ok", "65536 tokens (profile)"),
        ("ok", "default model: 131072 tokens (profile)"),
        ("ok", "enabled model 3: 262144 tokens (profile)"),
    ]
    rendered = "\n".join(check.detail for check in contexts)
    assert "current-model" not in rendered
    assert "default-model" not in rendered
    assert "third-model" not in rendered


def test_doctor_accepts_agent_provider_default_context() -> None:
    profile = Profile("provider-default", "codex")

    checks = run_doctor(
        profile,
        SimpleNamespace(skills={}),
        bridge=SimpleNamespace(instances=lambda: []),
        mcp_executable=Path(sys.executable),
        environ={"PATH": ""},
    )
    context = next(check for check in checks if check.name == "model-context")

    assert context.status == "ok"
    assert context.detail == "200000 tokens (provider-managed-fallback)"


def test_custom_model_alias_context_metadata_is_optional_and_never_invented(monkeypatch):
    monkeypatch.setattr('replaylab_agent.conversation._catalog_context_window', lambda *args: 0)
    profile = Profile('gateway-aliases', 'openai', 'k3',
        models=['k3', 'claude-fable-5', 'glm-5.3'], base_url='https://gateway.example/v1')
    checks = run_doctor(profile, SimpleNamespace(skills={}),
        bridge=SimpleNamespace(instances=lambda: []), mcp_executable=Path(sys.executable), environ={'PATH': ''})
    context = [check for check in checks if check.name.startswith('model-context')]
    assert len(context) == 3 and all(check.status == 'warning' for check in context)
    assert all(check.message_id == 'doctor.context-window-unknown' for check in context)
    assert {check.message_args[0] for check in context} == {'k3', 'claude-fable-5', 'glm-5.3'}


def test_doctor_does_not_echo_available_api_key(monkeypatch) -> None:
    secret = "sk-doctor-must-not-echo-this-value"
    monkeypatch.setenv("REPLAYLAB_TEST_DOCTOR_KEY", secret)
    profile = Profile(
        "secret-safe",
        "openai",
        "private-model",
        api_key_env="REPLAYLAB_TEST_DOCTOR_KEY",
        model_context_windows={"private-model": 65_536},
    )

    checks = run_doctor(
        profile,
        SimpleNamespace(skills={}),
        bridge=SimpleNamespace(instances=lambda: []),
        mcp_executable=Path(sys.executable),
    )

    assert secret not in "\n".join(check.detail for check in checks)


def test_doctor_reports_safe_bounded_skill_quarantine_summary(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    valid = root / "valid-skill"
    valid.mkdir(parents=True)
    (valid / "SKILL.md").write_text(
        "---\nname: valid-skill\ndescription: Valid procedure.\n---\n\n"
        "Use verified evidence.\n",
        encoding="utf-8",
    )
    broken = root / "broken-skill"
    broken.mkdir(parents=True)
    secret = "UNTRUSTED-DOCTOR-CONTENT"
    (broken / "SKILL.md").write_text(secret, encoding="utf-8")
    catalog = SkillCatalog([root]).load_runtime()

    checks = run_doctor(
        Profile("provider-default", "codex"),
        catalog,
        bridge=SimpleNamespace(instances=lambda: []),
        include_external_mcp=False,
        environ={"PATH": ""},
    )

    check = next(item for item in checks if item.name == "skills-quarantine")
    assert check.status == "warning"
    # The folder is named (it is what the user opens); the path and content are not.
    assert check.detail == (
        "1 skill definition(s) quarantined; "
        "bounded codes: skill-definition-invalid:1; folders: broken-skill (bundled)"
    )
    rendered = "\n".join(item.detail for item in checks)
    assert secret not in rendered
    assert str(tmp_path) not in rendered


def test_doctor_can_be_pointed_at_an_empty_tool_configuration(tmp_path, monkeypatch) -> None:
    """The packaging smoke must measure the build, not the developer's machine.

    `doctor` counts the Skills the catalog loaded, and the catalog is bundled Skills
    plus whatever Skill folders this machine has registered. The build gate asserts an
    exact bundled count, so one extra Skill on a developer's box failed a build that
    was perfectly correct. `--tools-config` points the configuration somewhere that
    does not exist, which reads as "no user configuration".
    """

    from replaylab_agent import cli

    configured = tmp_path / "real" / "agent-tools.json"
    configured.parent.mkdir(parents=True)
    extra_root = tmp_path / "my-skills"
    (extra_root / "my-own-skill").mkdir(parents=True)
    (extra_root / "my-own-skill" / "SKILL.md").write_text(
        "---\nname: my-own-skill\ndescription: A Skill this machine happens to have.\n"
        "metadata:\n  replaylab-category: local\n---\n\n# Mine\n\nSteps.\n",
        encoding="utf-8",
    )
    configured.write_text(
        json.dumps({"skillRoots": [str(extra_root)]}), encoding="utf-8")
    from replaylab_agent import agent_tools_config

    monkeypatch.setattr(agent_tools_config, "default_tools_config_path",
                        lambda: configured)

    with_user = cli._catalog(cli._tools_config(None))
    isolated = cli._catalog(cli._tools_config(tmp_path / "absent" / "agent-tools.json"))

    assert "my-own-skill" in with_user.skills
    assert "my-own-skill" not in isolated.skills
    assert len(isolated.skills) == len(with_user.skills) - 1
    # The bundled standard is still there when the user configuration is ignored.
    assert "replaylab-core" in isolated.skills
