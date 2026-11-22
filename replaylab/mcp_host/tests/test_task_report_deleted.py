"""Deleted chat telemetry cannot be resurrected by late Host responses."""
import json

from src.task_report import TaskReportStore, task_report_state_path


def test_deleted_task_stops_existing_and_new_report_writers(tmp_path):
    store = TaskReportStore("task", tmp_path, conversation_id="conversation-a")
    store.record_tool("inspect_event")
    state = task_report_state_path(tmp_path, "task")
    assert state.exists()
    markers = tmp_path / ".deleted-tasks"
    markers.mkdir()
    (markers / "task.json").write_text("{}")
    state.unlink()
    (tmp_path / "task.report-state.lock").unlink()
    for writer in (store, TaskReportStore("task", tmp_path, conversation_id="conversation-a")):
        assert not writer.enabled
        writer.record_tool("get_shader_info")
        writer.record_result("inspect_event", {"eventId": 7})
    assert not state.exists()
    assert not (tmp_path / "task.report-state.lock").exists()


def test_host_without_conversation_environment_preserves_durable_owner(tmp_path):
    first = TaskReportStore("task", tmp_path, conversation_id="conversation-a")
    first.record_tool("inspect_event")
    TaskReportStore("task", tmp_path, environ={}).record_tool("get_shader_info")
    payload = json.loads(task_report_state_path(tmp_path, "task").read_text())
    assert payload["conversationId"] == "conversation-a"
