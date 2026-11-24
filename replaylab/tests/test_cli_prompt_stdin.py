from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from replaylab_agent.agent_contracts import RunResult
from replaylab_agent.profiles import Profile, ProfileStore


def _store(tmp_path: Path) -> ProfileStore:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(Profile("local", "ollama", "test-model"))
    return store


def _args(**overrides):
    values = {
        "mcp_host": None,
        "context_file": None,
        "live_context": False,
        "instance_id": "",
        "prompt": "wait",
        "prompt_stdin": False,
        "profile": "local",
        "model": "",
        "skill": "",
        "intent_kind": "",
        "task_id": "task",
        "conversation_id": "",
        "cancel_file": None,
        "engine_config": None,
        "json": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _result() -> RunResult:
    return RunResult(
        task_id="task",
        profile_id="local",
        executor="Ollama",
        text="ok",
        terminal_status="completed",
    )


def _install_engine(monkeypatch, implementation):
    import replaylab_agent.cli as cli

    class FakeEngine:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def run(self, request):
            return await implementation(request)

    monkeypatch.setattr(cli, "DeepAgentsEngine", FakeEngine)


def test_parser_rejects_malicious_conversation_id_without_echo(capsys) -> None:
    import replaylab_agent.cli as cli

    hostile = "../capture/conversation"
    with pytest.raises(SystemExit) as failure:
        cli._parser().parse_args(
            [
                "run",
                "--profile",
                "local",
                "--prompt",
                "inspect",
                "--conversation-id",
                hostile,
            ]
        )

    assert failure.value.code == 2
    diagnostic = capsys.readouterr().err
    assert hostile not in diagnostic
    assert "invalid-command-line" in diagnostic


@pytest.mark.asyncio
async def test_run_cancel_file_cancels_engine_before_native_force_kill(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def run(_request):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    _install_engine(monkeypatch, run)
    cancel_file = tmp_path / "cancel.signal"
    cancel_file.write_bytes(b"")
    operation = asyncio.create_task(
        cli._run_once(_args(cancel_file=cancel_file), _store(tmp_path))
    )
    await started.wait()
    cancel_file.write_text("cancel\n", encoding="utf-8")

    with pytest.raises(asyncio.CancelledError):
        await operation
    assert cancelled.is_set()
    assert not cancel_file.exists()


@pytest.mark.asyncio
async def test_missing_cancel_file_does_not_cancel_new_engine(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def run(_request):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    _install_engine(monkeypatch, run)
    cancel_file = tmp_path / "not-visible-yet.signal"
    operation = asyncio.create_task(
        cli._run_once(_args(cancel_file=cancel_file), _store(tmp_path))
    )
    await started.wait()
    await asyncio.sleep(0.15)
    assert not operation.done()
    assert not cancelled.is_set()

    cancel_file.write_text("cancel\n", encoding="utf-8")
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert cancelled.is_set()
    assert not cancel_file.exists()


@pytest.mark.asyncio
async def test_cancel_watcher_ignores_transient_error_and_oversized_payload(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    cancel_file = tmp_path / "cancel.signal"
    cancel_file.write_bytes(b"")
    real_open = Path.open
    failed_once = False

    def flaky_open(path, *args, **kwargs):
        nonlocal failed_once
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == cancel_file and mode == "rb" and not failed_once:
            failed_once = True
            raise PermissionError("temporary sharing violation")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky_open)
    waiter = asyncio.create_task(cli._wait_for_run_cancellation(cancel_file))
    await asyncio.sleep(0.15)
    assert failed_once
    assert not waiter.done()

    with real_open(cancel_file, "wb") as signal:
        signal.write(b"not-a-cancel-token" * 8)
    await asyncio.sleep(0.15)
    assert not waiter.done()

    with real_open(cancel_file, "wb") as signal:
        signal.write(b"cancel\n")
    await asyncio.wait_for(waiter, timeout=1.0)


@pytest.mark.asyncio
async def test_cancel_watcher_failure_does_not_cancel_engine(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    completed = asyncio.Event()

    async def run(_request):
        await asyncio.sleep(0.05)
        completed.set()
        return _result()

    async def broken_cancel_watcher(_path):
        raise RuntimeError("watcher bug")

    _install_engine(monkeypatch, run)
    monkeypatch.setattr(cli, "_wait_for_run_cancellation", broken_cancel_watcher)
    assert (
        await cli._run_once(
            _args(cancel_file=tmp_path / "cancel.signal"), _store(tmp_path)
        )
        == 0
    )
    assert completed.is_set()


@pytest.mark.asyncio
async def test_run_reads_prompt_from_stdin(monkeypatch, tmp_path) -> None:
    import replaylab_agent.cli as cli

    observed = {}

    async def run(request):
        observed["request"] = request
        return _result()

    _install_engine(monkeypatch, run)
    prompt = "解释当前中文着色器"
    monkeypatch.setattr(
        cli.sys, "stdin", SimpleNamespace(buffer=BytesIO(prompt.encode("utf-8")))
    )
    assert (
        await cli._run_once(
            _args(prompt=None, prompt_stdin=True), _store(tmp_path)
        )
        == 0
    )
    assert observed["request"].prompt == prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "profile_id", "engine_name"),
    [
        (Profile("local", "ollama", "test-model"), "local", "DeepAgentsEngine"),
        (Profile("subscription", "codex"), "subscription", "NativeAgentEngine"),
    ],
)
async def test_explicit_conversation_id_reaches_every_engine_request(
    monkeypatch, tmp_path, profile, profile_id, engine_name
) -> None:
    import replaylab_agent.cli as cli

    observed = {}

    class FakeEngine:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def run(self, request):
            observed["request"] = request
            return RunResult(
                task_id=request.task_id,
                profile_id=request.profile.id,
                executor="fixture",
                text="ok",
                terminal_status="completed",
            )

    class ForbiddenEngine:
        def __init__(self, *args, **kwargs) -> None:  # pragma: no cover
            del args, kwargs
            raise AssertionError("the wrong engine route was selected")

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(profile)
    monkeypatch.setattr(cli, engine_name, FakeEngine)
    other = "NativeAgentEngine" if engine_name == "DeepAgentsEngine" else "DeepAgentsEngine"
    monkeypatch.setattr(cli, other, ForbiddenEngine)

    assert (
        await cli._run_once(
            _args(
                profile=profile_id,
                prompt="organize this shader",
                conversation_id="conversation-4a8f",
            ),
            store,
        )
        == 0
    )
    assert observed["request"].conversation_id == "conversation-4a8f"


@pytest.mark.asyncio
async def test_explicit_conversation_id_must_match_typed_envelope(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    called = False

    async def run(_request):
        nonlocal called
        called = True
        return _result()

    _install_engine(monkeypatch, run)
    prompt = (
        '{"schema":"replaylab-conversation/v5",'
        '"conversationId":"conversation-envelope",'
        '"messages":[],"request":{"role":"user","content":"continue"}}'
    )
    with pytest.raises(ValueError, match="conflicts with the typed conversation envelope"):
        await cli._run_once(
            _args(prompt=prompt, conversation_id="conversation-explicit"),
            _store(tmp_path),
        )
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conversation_id",
    ["../escape", "bad/id", " leading-space", "line\nbreak", "x" * 161],
)
async def test_run_rejects_noncanonical_conversation_ids(
    monkeypatch, tmp_path, conversation_id
) -> None:
    import replaylab_agent.cli as cli

    called = False

    async def run(_request):
        nonlocal called
        called = True
        return _result()

    _install_engine(monkeypatch, run)
    with pytest.raises(ValueError, match="conversation identity is invalid") as failure:
        await cli._run_once(
            _args(conversation_id=conversation_id),
            _store(tmp_path),
        )
    assert conversation_id not in str(failure.value)
    assert called is False


@pytest.mark.asyncio
async def test_matching_explicit_and_envelope_conversation_id_is_accepted(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    observed = {}

    async def run(request):
        observed["request"] = request
        return _result()

    _install_engine(monkeypatch, run)
    prompt = (
        '{"schema":"replaylab-conversation/v5",'
        '"conversationId":"conversation-same",'
        '"messages":[],"request":{"role":"user","content":"continue"}}'
    )
    assert (
        await cli._run_once(
            _args(prompt=prompt, conversation_id="conversation-same"),
            _store(tmp_path),
        )
        == 0
    )
    assert observed["request"].conversation_id == "conversation-same"


@pytest.mark.asyncio
async def test_direct_action_becomes_scoped_intent(monkeypatch, tmp_path) -> None:
    import replaylab_agent.cli as cli

    observed = {}

    async def run(request):
        observed["request"] = request
        return _result()

    _install_engine(monkeypatch, run)
    monkeypatch.setattr(
        cli,
        "_read_context",
        lambda _args: (
            {"capture": {"loaded": True, "captureSessionId": "a" * 32}},
            "qrenderdoc-test",
            "C:/capture.rdc",
        ),
    )
    args = _args(
        prompt="inspect",
        skill="renderdoc-explain-event",
        intent_kind="inspect-picked-pixel",
    )
    assert await cli._run_once(args, _store(tmp_path)) == 0
    request = observed["request"]
    assert request.intent == {"kind": "inspect-picked-pixel", "arguments": {}}
    assert request.skill_name == "renderdoc-explain-event"


@pytest.mark.asyncio
async def test_explain_event_intent_keeps_agent_skill_choice_adaptive(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    observed = {}

    async def run(request):
        observed["request"] = request
        return _result()

    _install_engine(monkeypatch, run)
    monkeypatch.setattr(
        cli,
        "_read_context",
        lambda _args: (
            {"capture": {"loaded": True, "captureSessionId": "a" * 32}},
            "qrenderdoc-test",
            "C:/capture.rdc",
        ),
    )
    assert (
        await cli._run_once(
            _args(prompt="explain", intent_kind="explain-current-event"),
            _store(tmp_path),
        )
        == 0
    )
    request = observed["request"]
    assert request.skill_name == ""
    assert request.intent == {"kind": "explain-current-event", "arguments": {}}


@pytest.mark.asyncio
async def test_direct_action_does_not_restrict_an_explicit_alternate_skill(
    monkeypatch, tmp_path
) -> None:
    import replaylab_agent.cli as cli

    observed = {}

    async def run(request):
        observed["request"] = request
        return _result()

    _install_engine(monkeypatch, run)
    monkeypatch.setattr(
        cli,
        "_read_context",
        lambda _args: (
            {"capture": {"loaded": True, "captureSessionId": "a" * 32}},
            "qrenderdoc-test",
            "C:/capture.rdc",
        ),
    )
    args = _args(
        prompt="explain this shader target",
        skill="renderdoc-shader-workbench",
        intent_kind="explain-current-event",
    )
    assert await cli._run_once(args, _store(tmp_path)) == 0
    assert observed["request"].skill_name == "renderdoc-shader-workbench"
    assert observed["request"].intent == {
        "kind": "explain-current-event", "arguments": {},
    }
