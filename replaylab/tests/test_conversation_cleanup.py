"""Conversation cleanup deletes owned telemetry, never captured or authored work."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from replaylab_agent.conversation_cleanup import (
    cleanup_conversation, cleanup_pending_conversations, record_workspace_owner,
)
from replaylab_agent.events import TaskEventWriter


def _task(root, task, conversation):
    root.mkdir(parents=True, exist_ok=True)
    (root / (task + ".report-state.json")).write_text(json.dumps({
        "schema": "replaylab-task-report-state/v1", "taskId": task,
        "conversationId": conversation,
    }), encoding="utf-8")
    for suffix in (".jsonl", ".archive.json", ".report.json"):
        (root / (task + suffix)).write_text("conversation private text", encoding="utf-8")
    logs = root.parent / "logs"
    logs.mkdir(exist_ok=True)
    (logs / ("acp-native-" + task + ".stderr.log")).write_text("provider private text")


def _workspace(root, task, conversation, digit):
    turn = root.parent / "external-agent-workspace" / ("native-" + digit * 32)
    workspace = turn / "agent-workspace"
    workspace.mkdir(parents=True)
    record_workspace_owner(turn, task, conversation)
    (workspace / ".mcp.json").write_text("private launch config")
    return turn, workspace


def test_completed_and_omitted_tasks_are_removed_without_touching_other_chats(tmp_path):
    root = tmp_path / "ai-tasks"
    _task(root, "recent", "conversation-a")
    _task(root, "omitted-from-history", "conversation-a")
    _task(root, "unrelated", "conversation-b")
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"user capture")
    (tmp_path / "logs/agent-core.log").write_text("shared diagnostic")
    report = cleanup_conversation("conversation-a", ["recent"], event_root=root)
    assert report["complete"] and report["taskIds"] == ["omitted-from-history", "recent"]
    assert sorted(path.name for path in root.glob("*.*") if path.is_file()) == [
        "unrelated.archive.json", "unrelated.jsonl", "unrelated.report-state.json", "unrelated.report.json"]
    assert capture.read_bytes() == b"user capture"
    assert (tmp_path / "logs/agent-core.log").read_text() == "shared diagnostic"
    assert (tmp_path / "logs/acp-native-unrelated.stderr.log").exists()
    assert not (tmp_path / "logs/acp-native-recent.stderr.log").exists()


def test_ownership_conflict_is_rejected_before_deleting_any_task(tmp_path):
    root = tmp_path / "ai-tasks"
    _task(root, "owned", "conversation-a")
    _task(root, "unrelated", "conversation-b")
    with pytest.raises(ValueError, match="another conversation"):
        cleanup_conversation("conversation-a", ["owned", "unrelated"], event_root=root)
    assert (root / "owned.jsonl").exists() and (root / "unrelated.jsonl").exists()
    assert not (root / ".deleted-tasks").exists()


def test_workspace_owner_conflict_survives_missing_report_state(tmp_path):
    root = tmp_path / "ai-tasks"
    _task(root, "other-task", "conversation-other")
    (root / "other-task.report-state.json").unlink()
    other, _ = _workspace(root, "other-task", "conversation-other", "e")
    with pytest.raises(ValueError, match="another conversation"):
        cleanup_conversation("conversation-current", ["other-task"], event_root=root)
    assert (root / "other-task.jsonl").exists() and other.exists()
    assert not (root / ".deleted-tasks").exists()


def test_existing_deletion_owner_cannot_be_overwritten(tmp_path):
    root = tmp_path / "ai-tasks"
    _task(root, "old-task", "conversation-a")
    assert cleanup_conversation("conversation-a", event_root=root)["complete"]
    marker = root / ".deleted-tasks/old-task.json"
    before = marker.read_bytes()
    with pytest.raises(ValueError, match="another conversation"):
        cleanup_conversation("conversation-b", ["old-task"], event_root=root)
    assert marker.read_bytes() == before


def test_delayed_event_writer_cannot_recreate_deleted_transcript(tmp_path):
    root = tmp_path / "ai-tasks"
    writer = TaskEventWriter(root, "late-task")
    writer.emit("queued", text="old prompt")
    assert cleanup_conversation("conversation-a", ["late-task"], event_root=root)["complete"]
    for selected in (writer, TaskEventWriter(root, "late-task")):
        with pytest.raises(RuntimeError, match="was deleted"):
            selected.emit("completed", text="late private answer")
    assert not (root / "late-task.jsonl").exists()
    assert not (root / "late-task.jsonl.lock").exists()


def test_cleanup_recovers_owned_scratch_and_preserves_delivered_files(tmp_path):
    root = tmp_path / "ai-tasks"
    scratch, _ = _workspace(root, "scratch", "conversation-a", "a")
    delivered, work = _workspace(root, "delivery", "conversation-a", "b")
    (work / "report.md").write_text("delivered report")
    (work / "shader.hlsl").write_text("delivered shader")
    launch = delivered / ("mcp-servers-" + "1" * 32 + ".json")
    launch.write_text("private launch credentials")
    other, _ = _workspace(root, "other", "conversation-b", "c")
    unowned = root.parent / "external-agent-workspace" / ("native-" + "d" * 32)
    unowned.mkdir()
    (unowned / "private.log").write_text("unproven ownership")
    report = cleanup_conversation("conversation-a", event_root=root)
    assert report["complete"] and report["taskIds"] == ["delivery", "scratch"]
    assert not scratch.exists()
    assert (work / "report.md").read_text() == "delivered report"
    assert (work / "shader.hlsl").exists()
    assert not launch.exists() and not (work / ".mcp.json").exists()
    assert report["preservedWorkspaces"] == [str(delivered)]
    assert other.exists() and unowned.exists()


def test_busy_log_cleanup_can_retry_after_report_ownership_was_removed(tmp_path, monkeypatch):
    root = tmp_path / "ai-tasks"
    _task(root, "busy", "conversation-a")
    log = tmp_path / "logs/acp-native-busy.stderr.log"
    unlink = Path.unlink
    def locked(path, *args, **kwargs):
        if path == log:
            raise PermissionError("provider log handle is still closing")
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", locked)
        patch.setattr("replaylab_agent.conversation_cleanup.time.sleep", lambda _: None)
        report = cleanup_conversation("conversation-a", event_root=root)
    assert not report["complete"] and log.exists()
    assert not (root / "busy.report-state.json").exists()
    retry = cleanup_conversation("conversation-a", event_root=root)
    assert retry["complete"] and retry["taskIds"] == ["busy"] and not log.exists()


def test_pending_deletion_resumes_after_shutdown_before_cleanup_started(tmp_path):
    import hashlib
    root = tmp_path / "ai-tasks"
    # Legacy task has no sidecar: the UI's durable request is its ownership receipt.
    root.mkdir()
    (root / "legacy.jsonl").write_text("private transcript")
    pending = root / ".cleanup-pending"
    pending.mkdir()
    filename = hashlib.sha256(b"conversation-a").hexdigest() + ".json"
    (pending / filename).write_text(json.dumps({
        "schema": "bread-conversation-cleanup/v1", "conversationId": "conversation-a", "taskIds": ["legacy"],
    }))
    report = cleanup_pending_conversations(event_root=root)
    assert report["complete"] and report["taskIds"] == ["legacy"]
    assert not (root / "legacy.jsonl").exists() and not (pending / filename).exists()
    assert list((root / ".deleted-conversations").glob("*.json"))
    assert cleanup_pending_conversations(event_root=root)["taskIds"] == []


@pytest.mark.parametrize("task", ["../escape", "..", "a/b", "a\\b"])
def test_invalid_task_identifiers_do_not_remove_anything(tmp_path, task):
    root = tmp_path / "ai-tasks"
    _task(root, "owned", "conversation-a")
    with pytest.raises(ValueError):
        cleanup_conversation("conversation-a", [task], event_root=root)
    assert (root / "owned.jsonl").exists()


def test_redirected_task_file_is_not_followed(tmp_path):
    root = tmp_path / "ai-tasks"
    root.mkdir()
    outside = tmp_path / "user.txt"
    outside.write_text("keep")
    try:
        (root / "task.jsonl").symlink_to(outside)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    with pytest.raises(ValueError, match="link or reparse"):
        cleanup_conversation("conversation-a", ["task"], event_root=root)
    assert outside.read_text() == "keep"


def test_cleanup_cli_uses_local_storage_without_loading_a_model(tmp_path, monkeypatch, capsys):
    from replaylab_agent import cli
    root = tmp_path / "ReplayLab/ai-tasks"
    _task(root, "completed", "conversation-a")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(cli, "_load_store", lambda *args: pytest.fail("No model/settings needed"))
    assert cli.main(["conversation-cleanup", "--conversation-id", "conversation-a", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["taskIds"] == ["completed"] and result["complete"]


def test_gui_persists_intent_before_mutation_and_does_not_restore_deleted_chat():
    root = Path(__file__).resolve().parents[2]
    source = (root / "qrenderdoc/Windows/AI/AIAssistant.cpp").read_text(encoding="utf-8")
    deletion = source.split("void AIAssistant::deleteConversation(", 1)[1].split(
        "void AIAssistant::forgetDeletedTask(", 1)[0]
    assert deletion.index("saveConversationCleanupRequest(") < deletion.index("m_ChatQueue.remove(i)")
    assert deletion.index("saveConversationCleanupRequest(") < deletion.index("cancelChat(")
    assert deletion.index("saveConversations();") < deletion.index("runCLIAsync(")
    load = source.split("void AIAssistant::loadConversations()", 1)[1].split(
        "void AIAssistant::saveConversations()", 1)[0]
    assert "conversationDeletionRequested(taskEventRoot(), thread.id)" in load
    assert "removedDeletedConversations) && !m_ConversationStoreReadOnly" in load
