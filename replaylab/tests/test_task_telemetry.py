"""Host tool receipts survive process restarts without grading the AI answer."""

from replaylab_agent.agent_report import TaskReportStore, read_task_report_sidecars


def test_tool_receipts_survive_restart_and_include_elapsed_time(tmp_path):
    store = TaskReportStore("receipt-task", tmp_path, conversation_id="conversation-1")
    store.record_result(
        "inspect_event", {"eventId": 42, "path": "D:/capture.rdc"},
        elapsed_ms=12.5,
    )

    restored = TaskReportStore("receipt-task", tmp_path)
    assert restored.tools_used() == ("inspect_event",)
    old_report, state = read_task_report_sidecars(tmp_path, "receipt-task")
    assert old_report is None
    assert state["toolFacts"][-1]["tool"] == "inspect_event"
    assert state["toolFacts"][-1]["elapsedMs"] == 12.5
    assert state["artifactPaths"] == ["D:/capture.rdc"]
    assert "verification" not in state
    assert "completionContract" not in state


def test_corrupt_old_sidecar_does_not_block_new_tool_receipt(tmp_path):
    stale = tmp_path / "recovery.report-state.json"
    stale.write_text("{broken", encoding="utf-8")
    store = TaskReportStore("recovery", tmp_path)
    store.record_result("inspect_shader", {"eventId": 11384})
    assert store.tools_used() == ("inspect_shader",)
    assert read_task_report_sidecars(tmp_path, "recovery")[1]["toolFacts"][-1]["tool"] == "inspect_shader"
