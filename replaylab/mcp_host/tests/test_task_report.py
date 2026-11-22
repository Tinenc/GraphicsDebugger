"""Task telemetry is factual and never authorizes or blocks Host operations."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from src import task_report
from src.task_report import (
    TASK_EVENT_ROOT_ENV, TASK_ID_ENV, TaskReportStore,
    clear_task_report_sidecars, instrument_task_tool, non_reporting_task_tool,
    read_task_report_sidecars, task_report_path, task_report_state_path,
)


def test_telemetry_persists_facts_without_a_report_or_gate(tmp_path) -> None:
    store = TaskReportStore("task-1", tmp_path, conversation_id="conversation-1")
    store.record_result("inspect_event", {"eventId": 11384, "path": str(tmp_path / "capture.rdc")})
    restarted = TaskReportStore("task-1", tmp_path)
    report, state = read_task_report_sidecars(tmp_path, "task-1")
    assert report is None
    assert not task_report_path(tmp_path, "task-1").exists()
    assert restarted.tools_used() == ("inspect_event",)
    assert state["toolsUsed"] == ["inspect_event"]
    assert state["toolFacts"][0]["tool"] == "inspect_event"
    assert state["artifactPaths"] == [str(tmp_path / "capture.rdc")]
    assert isinstance(state["updatedUnixNs"], int)
    assert "completionContract" not in state
    assert "maxRounds" not in state
    assert "report" not in state


def test_corrupt_legacy_sidecar_does_not_block_new_work(tmp_path) -> None:
    task_report_state_path(tmp_path, "task-2").write_text("corrupt old review state", encoding="utf-8")
    store = TaskReportStore("task-2", tmp_path)
    store.record_result("get_current_renderdoc_context", {"captureLoaded": True})
    state = read_task_report_sidecars(tmp_path, "task-2")[1]
    assert state["toolsUsed"] == ["get_current_renderdoc_context"]
    assert "reviewBudget" not in state
    state["toolStats"] = {"old-tool": {"calls": "broken", "totalMs": "NaN"}}
    task_report_state_path(tmp_path, "task-2").write_text(json.dumps(state), encoding="utf-8")
    TaskReportStore("task-2", tmp_path).record_result("inspect_event", {"eventId": 2})
    assert read_task_report_sidecars(tmp_path, "task-2")[1]["toolStats"]["old-tool"]["calls"] == 0


def test_projection_redacts_secrets_and_bounds_shader_text(tmp_path) -> None:
    store = TaskReportStore("task-3", tmp_path)
    store.record_result("inspect_shader", {
        "apiToken": "NEVER-EXPOSE-ME", "sourceText": "shader" * 10000,
        "error": "Bearer abcdef1234567890", "workspacePath": str(tmp_path / "shader"),
    })
    raw = task_report_state_path(tmp_path, "task-3").read_text(encoding="utf-8")
    assert "NEVER-EXPOSE-ME" not in raw
    assert "abcdef1234567890" not in raw
    assert "shader" * 100 not in raw
    assert read_task_report_sidecars(tmp_path, "task-3")[1]["toolFacts"][0]["outcome"] == "error"


def test_instrumentation_preserves_sync_async_and_failed_results(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(TASK_ID_ENV, "task-4")
    monkeypatch.setenv(TASK_EVENT_ROOT_ENV, str(tmp_path))

    @instrument_task_tool
    def inspect_event(event_id: int) -> dict:
        return {"eventId": event_id}

    @instrument_task_tool
    async def inspect_shader(event_id: int) -> dict:
        return {"eventId": event_id}

    @instrument_task_tool
    def failed_tool() -> None:
        raise RuntimeError("capture temporarily unavailable")

    assert inspect_event(12) == {"eventId": 12}
    assert asyncio.run(inspect_shader(12)) == {"eventId": 12}
    with pytest.raises(RuntimeError, match="capture temporarily unavailable"):
        failed_tool()
    state = read_task_report_sidecars(tmp_path, "task-4")[1]
    assert state["toolsUsed"] == ["failed_tool", "inspect_event", "inspect_shader"]
    assert [item["outcome"] for item in state["toolFacts"]] == ["returned", "returned", "error"]
    assert all(item["elapsedMs"] >= 0 for item in state["toolFacts"])
    assert state["toolStats"]["inspect_event"]["calls"] == 1
    assert state["toolStats"]["failed_tool"]["errors"] == 1
    assert state["toolStats"]["inspect_shader"]["totalMs"] >= 0


def test_telemetry_failure_cannot_cancel_a_physical_tool(monkeypatch) -> None:
    class UnavailableStore:
        def record_result(self, *args, **kwargs):
            raise OSError("telemetry volume is unavailable")

    monkeypatch.setattr(task_report, "current_task_report_store", lambda: UnavailableStore())

    @instrument_task_tool
    def apply_shader_workspace_shared_resource() -> str:
        return "applied"

    assert apply_shader_workspace_shared_resource() == "applied"


def test_failed_finish_receipt_is_observed_without_changing_result_or_stopping_work(tmp_path, monkeypatch):
    monkeypatch.setenv(TASK_ID_ENV, "task-finish-outcomes")
    monkeypatch.setenv(TASK_EVENT_ROOT_ENV, str(tmp_path))
    receipt = {"schema": "replaylab-shader-organization-finish/v1", "ok": False,
               "reason": "undeclared identifier '_9530'", "draftPreserved": True,
               "previousSourceRestored": True, "state": "finish-failed-draft-preserved"}
    calls = []

    @instrument_task_tool
    def finish_shader_workspace_organization():
        calls.append(1)
        return json.dumps(receipt)

    assert json.loads(finish_shader_workspace_organization()) == receipt
    receipt = {"ok": True, "history": [receipt]}
    assert json.loads(finish_shader_workspace_organization()) == receipt
    assert calls == [1, 1]
    state = read_task_report_sidecars(tmp_path, "task-finish-outcomes")[1]
    assert state["toolStats"]["finish_shader_workspace_organization"]["errors"] == 1
    assert [fact["outcome"] for fact in state["toolFacts"]] == ["error", "returned"]
    assert json.loads(state["toolFacts"][0]["summary"])["draftPreserved"] is True


def test_housekeeping_is_not_an_agent_observation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(TASK_ID_ENV, "task-5")
    monkeypatch.setenv(TASK_EVENT_ROOT_ENV, str(tmp_path))

    @instrument_task_tool
    @non_reporting_task_tool
    def release_task_shader_occupancy() -> str:
        return "released"

    assert release_task_shader_occupancy() == "released"
    assert read_task_report_sidecars(tmp_path, "task-5") == (None, None)


def test_physical_shader_receipts_support_cleanup_without_quality_state(tmp_path) -> None:
    store = TaskReportStore("task-6", tmp_path)
    workspace = str(tmp_path / "workspace")
    store.record_result("apply_shader_workspace_shared_resource", {
        "applied": True, "replacement": {"replacementId": "a" * 32},
    }, arguments={"workspace_path": workspace})
    store.record_result("start_shader_workspace_live_edit", {
        "result": {"workspacePath": workspace, "liveEditId": "b" * 32},
    }, arguments={"workspace_path": workspace})
    state = read_task_report_sidecars(tmp_path, "task-6")[1]
    assert state["pendingShaderWork"][workspace]["delivery"] == {
        "replayApplied": True, "replacementId": "a" * 32,
        "liveEditStarted": True, "liveEditId": "b" * 32,
    }
    store.record_result("stop_shader_workspace_live_edit", {"result": {"state": "stopped"}},
                        arguments={"live_edit_id": "b" * 32})
    store.record_result("rollback_shader_workspace_shared_resource", {
        "applied": False, "replacement": {"cleared": True},
    },
                        arguments={"workspace_path": workspace, "replacement_id": "a" * 32})
    assert read_task_report_sidecars(tmp_path, "task-6")[1]["pendingShaderWork"] == {}


def test_two_store_instances_merge_facts_under_task_lock(tmp_path) -> None:
    def record(index: int) -> None:
        TaskReportStore("task-7", tmp_path).record_result("tool-%d" % index, {"index": index})

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(record, range(8)))
    state = read_task_report_sidecars(tmp_path, "task-7")[1]
    assert set(state["toolsUsed"]) == {"tool-%d" % index for index in range(8)}
    assert len(state["toolFacts"]) == 8


def test_terminal_event_and_sidecar_cleanup_are_bounded(tmp_path) -> None:
    events = tmp_path / "task-8.jsonl"
    events.write_text(
        json.dumps({"taskId": "task-8", "kind": "started"}) + "\n"
        + json.dumps({"taskId": "task-8", "kind": "completed"}) + "\n",
        encoding="utf-8",
    )
    assert task_report._task_event_is_terminal(tmp_path, "task-8")
    TaskReportStore("task-8", tmp_path).record_tool("inspect_event")
    clear_task_report_sidecars(tmp_path, "task-8")
    assert read_task_report_sidecars(tmp_path, "task-8") == (None, None)
    with pytest.raises(ValueError, match="unsafe path"):
        task_report_path(tmp_path, "../escape")
