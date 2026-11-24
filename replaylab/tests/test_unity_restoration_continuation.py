"""The Agent continues specific Unity repairs without accepting or looping on old images."""
from __future__ import annotations

import copy

import pytest

from replaylab_agent.agent_report import TaskReportStore
from replaylab_agent.task_review import TaskReviewLoop
from src.task_completion_review import activity_identity


def task_state(*, fingerprint="work", complete=False, revision=3, state_fingerprint="scene"):
    return {"scopeId": "scope", "fresh": True, "workFingerprint": fingerprint,
        "review": {"verdict": "complete", "criteria": [], "findings": [], "nextActions": [], "blocker": ""},
        "unityRestorations": [{"restorationId": "a" * 32, "revision": revision,
            "stateFingerprint": state_fingerprint, "complete": complete,
            "gapCodes": [] if complete else ["render-mismatch"], "nextTools": ["render_unity_restoration"]}]}


def test_complete_text_does_not_bypass_a_pending_unity_render_and_recovery_is_specific():
    loop = TaskReviewLoop.for_intent(None)
    first = loop.decide(task_state(), {})
    assert first.action == "continue"
    prompt = first.continuation_prompt()
    assert "SAME restoration ID" in prompt
    assert "check_unity_restoration(repair_configuration=true)" in prompt
    second = loop.decide(task_state(), {})
    assert second.action == "partial" and second.reason == "no-progress"


def test_changed_scene_evidence_resumes_repair_and_actual_completion_can_finish():
    loop = TaskReviewLoop.for_intent(None)
    assert loop.decide(task_state(), {}).action == "continue"
    assert loop.decide(task_state(), {}).action == "partial"
    assert loop.decide(task_state(fingerprint="material-edited", revision=4, state_fingerprint="fixed-scene"), {}).action == "continue"
    assert loop.decide(task_state(fingerprint="verified", revision=4, state_fingerprint="fixed-scene", complete=True), {}).action == "accept"


def test_new_image_filenames_and_duplicate_iterations_do_not_reset_no_progress():
    first = {"schema": "bread-unity-restoration/v1", "restorationId": "a" * 32, "revision": 3,
        "complete": False, "gaps": [{"code": "render-mismatch"}],
        "inspection": {"stateFingerprint": "scene", "image": {"path": "/tmp/first.png"}},
        "comparison": {"reference": {"sha256": "b" * 64}, "rendered": {"sha256": "c" * 64, "path": "/tmp/first.png"}, "withinTolerance": False},
        "iterations": [{"progress": "first-observation"}]}
    second = copy.deepcopy(first)
    second["inspection"]["image"]["path"] = "/tmp/different-uuid.png"
    second["comparison"]["rendered"]["path"] = "/tmp/different-uuid.png"
    second["iterations"].append({"progress": "unchanged"})
    one = activity_identity("render_unity_restoration", first, {"restoration_id": "a" * 32})
    two = activity_identity("render_unity_restoration", second, {"restoration_id": "a" * 32})
    assert one == two
    loop = TaskReviewLoop.for_intent(None)
    assert loop.decide(task_state(fingerprint=one), {}).action == "continue"
    assert loop.decide(task_state(fingerprint=two), {}).reason == "no-progress"


def test_managed_import_diagnostic_does_not_override_agent_acceptance_of_direct_results(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAD_UNITY_RESTORATION_ROOT", str(tmp_path / "restorations"))
    store = TaskReportStore("old-import-obligation", tmp_path / "events")
    store.begin_task_review("a" * 64)
    store.record_result("import_resource_export_to_unity", {"status": "completed", "operationId": "b" * 32,
        "projectPath": str(tmp_path / "Unity"), "functionalRestorationRequired": True})
    state = TaskReportStore(store.task_id, store.event_root).task_review_state()
    assert state["unityRestorations"][0]["gapCodes"] == ["functional-import-needs-restoration-task"]
    state.update(fresh=True, review={"verdict": "complete"})
    decision = TaskReviewLoop.for_intent(None).decide(state, {})
    assert state["unityRestorations"][0]["diagnosticsOnly"]
    assert decision.action == "accept"
    store.record_task_review("complete", [{"requirement": "Restore the draw", "status": "satisfied",
        "evidence": "Direct project compilation, execution and rendered-image inspection; managed binding was not used"}], [], [])
    assert store.task_review_state()["review"]["verdict"] == "complete"
