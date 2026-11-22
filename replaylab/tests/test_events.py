from __future__ import annotations

import json
import os
import time

import pytest

from replaylab_agent.agent_progress import ProgressReporter
from replaylab_agent.events import TASK_EVENT_SCHEMA, TaskEventWriter


def _payloads(writer: TaskEventWriter) -> list[dict]:
    payloads: list[dict] = []
    for line in writer.path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            payloads.append(value)
    return payloads


def test_task_events_are_append_only_jsonl_with_monotonic_sequence(tmp_path) -> None:
    writer = TaskEventWriter(tmp_path, task_id="task-abc")

    first = writer.emit("running", backend="model", profile="local", summary="解释当前事件")
    second = writer.emit("message-delta", text="证据片段")

    payloads = _payloads(writer)
    assert first.sequence == 1
    assert second.sequence == 2
    assert [item["sequence"] for item in payloads] == [1, 2]
    assert all(item["schema"] == TASK_EVENT_SCHEMA for item in payloads)
    assert all(item["taskId"] == "task-abc" for item in payloads)


def test_progress_reporter_chunks_large_public_streams_and_terminal_answer(tmp_path) -> None:
    writer = TaskEventWriter(tmp_path, task_id="bounded-stream")
    reporter = ProgressReporter(
        writer, profile_id="local", backend="openai", strategy="deepagents/v1"
    )
    text = "推理🧠" * 5000
    reporter.main_message(text)
    reporter.main_reasoning({"text": text, "source": "model"})
    reporter.child_message("child-1", text, skill="renderdoc-explain-event")
    reporter.final_answer(text)
    reporter.completed(
        text="完成",
        terminal_status="completed",
    )

    raw_lines = writer.path.read_bytes().splitlines()
    assert all(len(line) < 64 * 1024 for line in raw_lines)
    payloads = [json.loads(line) for line in raw_lines]
    provider = [p for p in payloads if p["kind"] == "message-delta" and p["data"].get("stream") == "provider"]
    assert "".join(item["text"] for item in provider) == text
    terminal = payloads[-1]
    assert terminal["kind"] == "completed"
    assert terminal["deliveryId"].startswith("agent-")
    assert terminal["data"]["terminalStatus"] == "completed"
    assert terminal["data"]["completionScope"] == "agent-turn"
    assert terminal["data"]["acceptanceAuthority"] == "agent"
    assert "report" not in terminal["data"]
    assert "verification" not in terminal["data"]


def test_task_events_resume_sequence_and_redact_sensitive_data(tmp_path) -> None:
    first = TaskEventWriter(tmp_path, task_id="resume-task")
    first.emit("running", data={"api_key": "must-not-appear", "safe": 1})
    second = TaskEventWriter(tmp_path, task_id="resume-task")
    event = second.emit("completed")

    encoded = second.path.read_text(encoding="utf-8")
    payload = json.loads(encoded.splitlines()[0])
    assert event.sequence == 2
    assert "must-not-appear" not in encoded
    assert payload["data"] == {"api_key": "[REDACTED]", "safe": 1}


def test_terminal_delivery_is_fsynced_and_idempotent_across_writers(
    tmp_path, monkeypatch
) -> None:
    fsync_calls: list[int] = []
    original_fsync = os.fsync

    def recording_fsync(file_descriptor: int) -> None:
        fsync_calls.append(file_descriptor)
        original_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    first = TaskEventWriter(tmp_path, task_id="durable-delivery")
    projected = first.emit("completed", delivery_id="outbox-deadbeef", summary="accepted")
    reopened = TaskEventWriter(tmp_path, task_id="durable-delivery")
    replayed = reopened.emit("completed", delivery_id="outbox-deadbeef", summary="duplicate")

    payloads = _payloads(reopened)
    assert replayed.sequence == projected.sequence == 1
    assert len(payloads) == 1
    assert payloads[0]["summary"] == "accepted"
    assert fsync_calls


def test_task_event_failure_keeps_chinese_and_escapes_malformed_unicode(tmp_path) -> None:
    writer = TaskEventWriter(tmp_path, task_id="unicode-failure")
    writer.emit("failed", summary="无法读取 C:/捕获/测试.rdc " + "\ud800")

    raw = writer.path.read_bytes()
    raw.decode("utf-8", errors="strict")
    assert json.loads(raw)["summary"].endswith("\ud800")


def test_task_events_resume_after_a_large_last_record(tmp_path) -> None:
    first = TaskEventWriter(tmp_path, task_id="large-tail")
    first.emit("message-delta", text="证据" * (512 * 1024))
    second = TaskEventWriter(tmp_path, task_id="large-tail")

    terminal = second.emit("completed", summary="done")

    assert terminal.sequence == 2
    assert [payload["sequence"] for payload in _payloads(second)] == [1, 2]


@pytest.mark.parametrize("damaged_tail", [b'{"schema":', b"{not-json}\n"])
def test_task_events_resume_after_invalid_or_partial_tail(tmp_path, damaged_tail) -> None:
    first = TaskEventWriter(tmp_path, task_id="damaged-tail")
    first.emit("running")
    with first.path.open("ab") as stream:
        stream.write(damaged_tail)

    second = TaskEventWriter(tmp_path, task_id="damaged-tail")
    terminal = second.emit("completed", summary="recovered")

    assert terminal.sequence == 2
    assert [payload["sequence"] for payload in _payloads(second)] == [1, 2]


def test_task_event_writer_prunes_only_stale_safe_task_artifacts(tmp_path) -> None:
    stale = tmp_path / "stale-task.jsonl"
    stale_report = tmp_path / "stale-task.report.json"
    recent = tmp_path / "recent-task.jsonl"
    unrelated = tmp_path / "notes.txt"
    for path, value in ((stale, "old"), (stale_report, "{}"), (recent, "new"), (unrelated, "keep")):
        path.write_text(value, encoding="utf-8")
    old = time.time() - 8 * 24 * 60 * 60
    os.utime(stale, (old, old))
    os.utime(unrelated, (old, old))

    TaskEventWriter(tmp_path, task_id="current-task")

    assert not stale.exists()
    assert not stale_report.exists()
    assert recent.exists()
    assert unrelated.exists()


def test_stream_batches_preserve_text_and_terminal_order(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.events._DELTA_FLUSH_SECONDS", 60)
    writer = TaskEventWriter(tmp_path, "batch")
    for char in "完整的中文流式内容🧠" * 100:
        writer.emit_delta("message-delta", text=char, data={"stream": "provider", "reset": False})
    terminal = writer.emit("completed", delivery_id="batch-final")
    rows = _payloads(writer)
    assert len(rows) == 2
    assert rows[0]["text"] == "完整的中文流式内容🧠" * 100
    assert rows[-1]["sequence"] == terminal.sequence == 2
    assert rows[-1]["deliveryId"] == "batch-final"


def test_stream_boundaries_reset_and_items_are_never_merged(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.events._DELTA_FLUSH_SECONDS", 60)
    writer = TaskEventWriter(tmp_path, "boundaries")
    for item, text in [("one", "A"), ("one", "B"), ("two", "C")]:
        writer.emit_delta("agent-message-delta", text=text, data={"itemId": item})
    writer.emit_delta("agent-message-delta", text="D", data={"itemId": "two", "reset": True})
    writer.emit("tool-started", tool="inspect_shader")
    writer.emit_delta("reasoning-delta", text="E", data={"channel": "summary"})
    writer.emit_delta("reasoning-delta", text="F", data={"channel": "raw"})
    writer.flush()
    rows = _payloads(writer)
    assert [r["text"] for r in rows] == ["AB", "C", "D", "", "E", "F"]
    assert [r["sequence"] for r in rows] == list(range(1, 7))
    assert rows[2]["data"]["reset"] is True


def test_stream_timer_flushes_without_followup_and_owns_metadata(tmp_path, monkeypatch):
    import threading
    writer = TaskEventWriter(tmp_path, "latency")
    ready = threading.Event()
    original = writer.emit
    def emit(*args, **kwargs):
        event = original(*args, **kwargs)
        ready.set()
        return event
    monkeypatch.setattr(writer, "emit", emit)
    data = {"itemId": "first", "password": "never-save", "nested": [1]}
    writer.emit_delta("message-delta", text="last token", data=data)
    data["itemId"] = "mutated"
    data["nested"].append(2)
    assert ready.wait(3)
    row = _payloads(writer)[0]
    assert row["data"] == {"itemId": "first", "password": "[REDACTED]", "nested": [1]}
    assert row["text"] == "last token"


def test_stream_batch_failure_is_retained_for_synchronous_retry(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.events._DELTA_FLUSH_SECONDS", 60)
    writer = TaskEventWriter(tmp_path, "retry")
    writer.emit_delta("message-delta", text="keep me")
    original = writer.emit
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(writer, "emit", fail)
    with pytest.raises(OSError):
        writer.flush()
    monkeypatch.setattr(writer, "emit", original)
    writer.emit("completed")
    assert [r["text"] for r in _payloads(writer)] == ["keep me", ""]


def test_stream_append_resumes_old_v1_file_and_concurrent_writer(tmp_path, monkeypatch):
    monkeypatch.setattr("replaylab_agent.events._DELTA_FLUSH_SECONDS", 60)
    writer = TaskEventWriter(tmp_path, "old-log")
    writer.emit("queued")
    writer.emit_delta("message-delta", text="buffered")
    other = TaskEventWriter(tmp_path, "old-log")
    other.emit("tool-completed")
    writer.emit("completed")
    assert [r["sequence"] for r in _payloads(writer)] == [1, 2, 3, 4]
    assert _payloads(writer)[2]["text"] == "buffered"


def test_queued_event_links_turns_to_their_conversation(tmp_path):
    writer = TaskEventWriter(tmp_path, "turn")
    ProgressReporter(writer, profile_id="test", backend="model").queued(
        prompt_chars=1, preset="test", profile_title="test", model="test", conversation_id="chat")
    assert _payloads(writer)[0]["data"]["conversationId"] == "chat"
