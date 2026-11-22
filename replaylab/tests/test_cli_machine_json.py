from __future__ import annotations

import json
from io import BytesIO, StringIO
from types import SimpleNamespace

import pytest

from replaylab_agent.agent_contracts import RunResult
from replaylab_agent.profiles import Profile, ProfileStore


class _BinaryStdout:
    def __init__(self) -> None:
        self.buffer = BytesIO()


class _BinaryStderr:
    def __init__(self) -> None:
        self.buffer = BytesIO()


def _run_args(**changes):
    values = {
        "mcp_host": None,
        "context_file": None,
        "live_context": False,
        "instance_id": "",
        "prompt": "解释事件",
        "prompt_stdin": False,
        "profile": "local",
        "model": "",
        "skill": "",
        "intent_kind": "",
        "task_id": "task-json",
        "cancel_file": None,
        "engine_config": None,
        "json": True,
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_run_json_is_compact_utf8_and_model_uses_deepagents(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(Profile("local", "ollama", "test-model"))
    seen = []

    class FakeDeepEngine:
        def __init__(self, *args, **kwargs) -> None:
            seen.append((args, kwargs))

        async def run(self, request):
            assert request.prompt == "解释事件"
            return RunResult(
                task_id=request.task_id,
                profile_id=request.profile.id,
                executor="deepagents",
                text="中文回答 ✓",
                terminal_status="completed",
            )

    class ForbiddenNativeEngine:
        def __init__(self, *args, **kwargs) -> None:  # pragma: no cover
            raise AssertionError("model profiles must not use a vendor-native loop")

    output = _BinaryStdout()
    monkeypatch.setattr(cli, "DeepAgentsEngine", FakeDeepEngine)
    monkeypatch.setattr(cli, "NativeAgentEngine", ForbiddenNativeEngine)
    monkeypatch.setattr(cli.sys, "stdout", output)

    assert await cli._run_once(_run_args(), store) == 0
    encoded = output.buffer.getvalue()
    assert len(seen) == 1
    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    assert json.loads(encoded.decode("utf-8"))["text"] == "中文回答 ✓"


@pytest.mark.asyncio
async def test_subscription_profile_uses_only_vendor_native_engine(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(Profile("subscription", "codex"))

    class ForbiddenDeepEngine:
        def __init__(self, *args, **kwargs) -> None:  # pragma: no cover
            raise AssertionError("subscription profiles must not use deepagents")

    class FakeNativeEngine:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, request):
            return RunResult(
                task_id=request.task_id,
                profile_id=request.profile.id,
                executor="Codex",
                text="done",
                terminal_status="completed",
                core="vendor-native-agent/v1",
            )

    output = _BinaryStdout()
    monkeypatch.setattr(cli, "DeepAgentsEngine", ForbiddenDeepEngine)
    monkeypatch.setattr(cli, "NativeAgentEngine", FakeNativeEngine)
    monkeypatch.setattr(cli.sys, "stdout", output)

    result = await cli._run_once(
        _run_args(profile="subscription", prompt="inspect"), store
    )
    payload = json.loads(output.buffer.getvalue())
    assert result == 0
    assert payload["engine"] == "vendor-native-agent/v1"
    assert payload["children"] == []


def test_terminal_error_is_exact_bounded_utf8_even_under_chinese_locale(
    monkeypatch,
) -> None:
    import replaylab_agent.cli as cli

    output = _BinaryStderr()
    monkeypatch.setattr(cli.sys, "stderr", output)
    detail = "replaylab-ai: 无法读取 D:\\抓帧\\白银之城\\4.rdc"

    cli._print_utf8_error(detail)

    assert output.buffer.getvalue() == (detail + "\n").encode("utf-8")


def test_terminal_error_text_only_fallback_preserves_chinese(monkeypatch) -> None:
    import replaylab_agent.cli as cli

    output = StringIO()
    monkeypatch.setattr(cli.sys, "stderr", output)
    cli._print_utf8_error("连接失败：中文路径")

    assert output.getvalue() == "连接失败：中文路径\n"


def test_cli_main_converts_unexpected_run_failure_to_safe_terminal_category(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    output = _BinaryStderr()
    secret = "sk-cli-secret-value"

    async def fail_run(*_args, **_kwargs):
        raise NameError(f"private failure token={secret}")

    monkeypatch.setattr(cli.sys, "stderr", output)
    monkeypatch.setattr(cli, "_load_store", lambda _path: object())
    monkeypatch.setattr(cli, "_run_once", fail_run)

    result = cli.main(
        ["--config", str(tmp_path / "profiles.json"), "run", "--prompt", "private"]
    )

    terminal = output.buffer.getvalue().decode("utf-8")
    assert result == 2
    assert "error[internal-host-failure]" in terminal
    assert secret not in terminal
    assert "NameError" not in terminal


def test_cli_rejected_argument_does_not_echo_secret_argv(monkeypatch) -> None:
    import replaylab_agent.cli as cli

    output = StringIO()
    secret_argument = "--api-key=sk-argv-secret-value"
    monkeypatch.setattr(cli.sys, "stderr", output)

    result = cli.main(["run", "--prompt", "hello", secret_argument])

    assert result == 2
    terminal = output.getvalue()
    assert "error[invalid-command-line]" in terminal
    assert secret_argument not in terminal


def test_machine_json_preserves_chinese_and_malformed_provider_unicode() -> None:
    import replaylab_agent.cli as cli

    encoded = cli._machine_json(
        {"status": "failed", "detail": "中文路径 D:/捕获/测试.rdc " + "\ud800"}
    )

    encoded.decode("utf-8", errors="strict")
    payload = json.loads(encoded)
    assert "D:/捕获/测试.rdc" in payload["detail"]
    assert payload["detail"].endswith("\ud800")
