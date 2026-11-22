from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from replaylab_agent.connectors import codex_app_server as codex
from replaylab_agent.errors import ConnectorError, ProviderRequestError


FIXTURE = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"


def connector_for(tmp_path, mode, *, timeout=5):
    trace = tmp_path / "requests.jsonl"
    connector = codex.CodexAppServerConnector(
        [sys.executable, str(FIXTURE), mode, str(trace)], cwd=tmp_path,
        workspace_write=True, timeout=timeout, heartbeat_interval=0.05,
    )
    return connector, trace


def requests(trace, method):
    return [row["params"] for row in map(json.loads, trace.read_text(encoding="utf-8").splitlines())
            if row.get("method") == method]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["capacity-success", "capacity-legacy", "capacity-after-tool"])
async def test_capacity_recovers_same_model_thread_without_resubmitting_work(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(codex, "_CAPACITY_RETRY_DELAYS", (0.01, 0.01))
    connector, trace = connector_for(tmp_path, mode)
    skill = tmp_path / "SKILL.md"
    skill.write_text("fixture skill", encoding="utf-8")
    timeline = []
    result = await connector.run(
        "original request sentinel", model="user-selected-model", skill_name="fixture",
        skill_path=skill, on_timeline=timeline.append,
    )
    turns = requests(trace, "turn/start")
    assert result.text == "continued existing work"
    assert result.turn_id == "turn-capacity-2"
    assert len(turns) == 2
    assert len(requests(trace, "thread/start")) == 1
    assert len(requests(trace, "account/read")) == 1
    assert not requests(trace, "account/login/start")
    assert all(turn["model"] == "user-selected-model" for turn in turns)
    assert all(turn["threadId"] == result.thread_id == "thread-test" for turn in turns)
    assert turns[0]["permissions"] == turns[1]["permissions"]
    assert turns[0]["runtimeWorkspaceRoots"] == turns[1]["runtimeWorkspaceRoots"] == [str(tmp_path)]
    assert "original request sentinel" in turns[0]["input"][0]["text"]
    assert len(turns[0]["input"]) == 2  # Original skill attachment is admitted once.
    assert turns[1]["input"] == [{"type": "text", "text": codex._CAPACITY_RECOVERY_PROMPT}]
    retries = [row for row in timeline if row.get("activityKind") == "providerRetry"]
    assert [row["activityTitleId"] for row in retries] == ["task.provider.retry-capacity"]
    assert retries[0]["activityTitleArgs"] == [0.01, 1, 2]
    if mode == "capacity-after-tool":
        assert (tmp_path / "effect-count.txt").read_text() == "1"
        assert sum(row.get("activityKind") == "fileChange" and row.get("lifecycle") == "completed"
                   for row in timeline) == 1
    assert connector._transport.process is None


@pytest.mark.asyncio
async def test_capacity_retry_exhaustion_remains_failed_and_classified(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "_CAPACITY_RETRY_DELAYS", (0.01, 0.01))
    connector, trace = connector_for(tmp_path, "capacity-exhausted")
    with pytest.raises(ProviderRequestError, match="attempts exhausted") as raised:
        await connector.run("finish", model="keep-this-model")
    assert raised.value.error_code == "provider-capacity"
    assert raised.value.retryable is True  # A later user retry may succeed; this run is finished.
    assert len(requests(trace, "turn/start")) == 3
    assert len(requests(trace, "thread/start")) == 1
    assert connector._transport.process is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("tag", "code"), [
    ("unauthorized", "provider-authentication"),
    ("usageLimitExceeded", "provider-quota"),
    ("sessionBudgetExceeded", "provider-quota"),
    ("rateLimitExceeded", "provider-rate-limit"),
    ("contextWindowExceeded", "provider-context"),
    ("badRequest", "provider-configuration"),
    ("sandboxError", "provider-policy"),
    ("cyberPolicy", "provider-policy"),
    ("misalignmentPolicyViolation", "provider-policy"),
    ("tooManyDenials", "provider-policy"),
    ("internalServerError", "provider-turn-failed"),
    ("futureUnknownError", "provider-turn-failed"),
])
async def test_structured_non_capacity_error_overrides_capacity_text(tmp_path, tag, code):
    connector, trace = connector_for(tmp_path, "capacity-tag-" + tag)
    with pytest.raises(ProviderRequestError) as raised:
        await connector.run("finish")
    assert raised.value.error_code == code
    assert raised.value.retryable is False
    assert tag in raised.value.codex_error_info
    assert len(requests(trace, "turn/start")) == 1
    assert not requests(trace, "account/login/start")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["capacity-pending-tool", "capacity-snapshot-pending"])
async def test_capacity_does_not_continue_an_unfinished_effect(tmp_path, mode):
    connector, trace = connector_for(tmp_path, mode)
    with pytest.raises(ProviderRequestError, match="terminal outcome is unconfirmed"):
        await connector.run("finish")
    assert len(requests(trace, "turn/start")) == 1


@pytest.mark.asyncio
async def test_provider_own_retry_notification_does_not_create_client_retry(tmp_path):
    connector, trace = connector_for(tmp_path, "capacity-provider-retry")
    result = await connector.run("finish")
    assert result.text == "continued existing work"
    assert len(requests(trace, "turn/start")) == 1


@pytest.mark.asyncio
async def test_cancellation_during_capacity_backoff_sends_no_followup(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "_CAPACITY_RETRY_DELAYS", (2.0, 2.0))
    connector, trace = connector_for(tmp_path, "capacity-success")
    waiting = asyncio.Event()

    def on_timeline(event):
        if event.get("activityKind") == "providerRetry":
            waiting.set()

    run = asyncio.create_task(connector.run("finish", on_timeline=on_timeline))
    await asyncio.wait_for(waiting.wait(), timeout=3)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=2)
    assert len(requests(trace, "turn/start")) == 1
    assert requests(trace, "turn/interrupt") == [{"threadId": "thread-test", "turnId": "turn-capacity-1"}]
    assert connector._transport.process is None


@pytest.mark.asyncio
async def test_capacity_recovery_cannot_extend_original_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "_CAPACITY_RETRY_DELAYS", (1.0, 1.0))
    connector, trace = connector_for(tmp_path, "capacity-success", timeout=0.5)
    with pytest.raises(ProviderRequestError, match="original turn deadline"):
        await connector.run("finish")
    assert len(requests(trace, "turn/start")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["capacity-interrupted", "capacity-request-rejected"])
async def test_non_failed_terminal_or_unconfirmed_start_never_retried(tmp_path, mode):
    connector, trace = connector_for(tmp_path, mode)
    with pytest.raises(ConnectorError):
        await connector.run("finish")
    assert len(requests(trace, "turn/start")) == 1


@pytest.mark.asyncio
async def test_recovery_preserves_the_native_engine_live_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "_CAPACITY_RETRY_DELAYS", (0.01, 0.01))
    connector, trace = connector_for(tmp_path, "capacity-after-tool")
    try:
        result = await connector.run("finish", model="selected-model", keep_thread=True)
        assert connector._active_thread_id == result.thread_id == "thread-test"
        assert connector._transport.process is not None
        assert connector._transport.process.returncode is None
        assert (tmp_path / "effect-count.txt").read_text() == "1"
    finally:
        await connector.close()
    assert len(requests(trace, "thread/start")) == 1


@pytest.mark.asyncio
async def test_cancellation_interrupts_the_new_active_recovery_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "_CAPACITY_RETRY_DELAYS", (0.01, 0.01))
    connector, trace = connector_for(tmp_path, "capacity-recovery-wait")
    waiting = asyncio.Event()
    run = asyncio.create_task(connector.run("finish", keep_thread=True,
        on_activity=lambda *_: waiting.set()))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=3)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, timeout=2)
    finally:
        await connector.close()
    assert len(requests(trace, "turn/start")) == 2
    assert requests(trace, "turn/interrupt") == [{"threadId": "thread-test", "turnId": "turn-capacity-2"}]


@pytest.mark.parametrize(("status", "code", "retryable"), [
    (401, "provider-authentication", False), (403, "provider-policy", False),
    (429, "provider-rate-limit", False), (400, "provider-configuration", False),
    (503, "provider-capacity", True), (500, "provider-turn-failed", False),
])
def test_documented_http_error_variants_preserve_nonretryable_classification(status, code, retryable):
    failure = codex._CodexTurnFailure("failed", {
        "message": "Selected model is at capacity. token=private-value",
        "codexErrorInfo": {"responseTooManyFailedAttempts": {"httpStatusCode": status}},
    }, unfinished_tools=False)
    assert failure.error_code == code
    assert failure.retryable is retryable
    assert "private-value" not in str(failure)
    assert "[REDACTED]" in str(failure)


@pytest.mark.asyncio
async def test_cancel_unacknowledged_recovery_invalidates_owned_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "_CAPACITY_RETRY_DELAYS", (0.01, 0.01))
    connector, trace = connector_for(tmp_path, "capacity-recovery-ack-wait")
    run = asyncio.create_task(connector.run("finish", keep_thread=True))

    async def wait_until_accepted():
        while not (tmp_path / "accepted-recovery").exists():
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_until_accepted(), timeout=3)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, timeout=2)
        assert connector._transport.process is None
        assert connector._active_thread_id == ""
        assert len(requests(trace, "turn/start")) == 2
        # It cannot claim to interrupt an unknown new turn by sending the old id.
        assert not requests(trace, "turn/interrupt")
    finally:
        await connector.close()
