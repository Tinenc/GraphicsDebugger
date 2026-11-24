"""Current-goal self-reviews are Agent assessments, not Host quality verdicts."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from src import server
from src.task_completion_review import activity_identity
from src.task_report import (TaskReportStore, TASK_ID_ENV, TASK_EVENT_ROOT_ENV,
    task_report_state_path, read_task_report_sidecars)


def _criteria(status="satisfied"):
    return [{"requirement": "Explain the user's requested concept", "status": status,
             "evidence": "The prepared answer explains the concept and gives the requested example."}]


def _store(tmp_path, name="task-1"):
    store = TaskReportStore(name, tmp_path)
    state = store.begin_task_review(hashlib.sha256(b"current user request").hexdigest())
    assert state["workRevision"] == 0 and state["fresh"] is False
    return store


def test_pending_user_decision_roundtrips_without_rewriting_legacy_reviews(tmp_path):
    store=_store(tmp_path)
    question=dict(question="Continue rebuilding TAA, approximate it, or disable it?",
                  options=["Continue with new budget","Approximate","Disable TAA"],evidence=["Saved original/reconstructed pass comparisons"])
    receipt=store.record_task_review("blocked",_criteria("blocked"),["TAA remains unresolved"],
        ["Await the user's choice"],"Only a user tradeoff decision remains",decision_request=question)
    assert receipt["decisionRequest"]==question
    restored=TaskReportStore("task-1",tmp_path).task_review_state()
    assert restored["fresh"] and restored["review"]["decisionRequest"]==question
    store.record_task_activity("new-evidence","changed",occurrence_id="one")
    assert not store.task_review_state()["fresh"]
    store.record_task_review("complete",_criteria(),[],[])
    assert "decisionRequest" not in store.task_review_state()["review"]


@pytest.mark.parametrize("question",[
    {},dict(question="Which?",options=["One"],evidence=["Saved evidence"]),
    dict(question="Which?",options=["A","B"],evidence=[]),
])
def test_pending_decision_needs_a_reviewable_question_and_saved_evidence(tmp_path,question):
    with pytest.raises(ValueError):
        _store(tmp_path).record_task_review("blocked",_criteria("blocked"),[],[],"User decision needed",decision_request=question)


def test_complete_cannot_also_request_an_unresolved_user_decision(tmp_path):
    with pytest.raises(ValueError):
        _store(tmp_path).record_task_review("complete",_criteria(),[],[],decision_request=dict(
            question="Which?",options=["A","B"],evidence=["Saved evidence"]))


def test_plain_answer_can_review_existing_reasoning_without_any_probe(tmp_path):
    store = _store(tmp_path)
    receipt = store.record_task_review("complete", _criteria(), [], [])
    state = store.task_review_state()
    assert state["fresh"] is True and state["review"] == receipt
    assert receipt["author"] == "agent" and receipt["hostSemanticApproval"] is False
    assert store.tools_used() == ()
    assert receipt["workRevision"] == 0
    assert TaskReportStore("task-1", tmp_path).task_review_state()["fresh"] is True


def test_new_visual_publication_needs_its_presentation_assessment(tmp_path):
    store=_store(tmp_path)
    inventory=dict(cards=[dict(type='image',title='Requested image',nodes=[])])
    store.register_reply_visual('a'*64,presentation=inventory)
    with pytest.raises(ValueError,match='visual_review'):
        store.record_task_review('complete',_criteria(),[],[])
    evidence='The user asked for one image. It answers that question; a graph and code are not useful here.'
    review=store.record_task_review('complete',_criteria(),[],[],visual_review=evidence)
    assert review['visualReview']==dict(visualId='a'*64,evidence=evidence)
    assert store.task_review_state()['fresh']
    store.register_reply_visual('b'*64,presentation=inventory)
    state=store.task_review_state()
    assert not state['fresh'] and state['visualPublication']['id']=='b'*64
    store.record_task_review('complete',_criteria(),[],[],visual_review='Checked the corrected image and its caption.')
    assert store.task_review_state()['fresh']


def test_legacy_visual_registration_does_not_rewrite_old_review_contract(tmp_path):
    store=_store(tmp_path)
    store.register_reply_visual('a'*64)
    store.record_task_review('complete',_criteria(),[],[])
    assert store.task_review_state()['fresh']


def test_frame_diagram_quality_cannot_be_overruled_by_self_review(tmp_path):
    store = _store(tmp_path)
    receipt = {"schema": "bread-frame-render-flow-publication/v1", "flowId": "flow-test",
               "quality": {"analysisComplete": False, "gaps": [{"code": "missing-output-attachments"}]}}
    store.record_result("publish_frame_render_flow", receipt)
    # The proxy's separate store must see the Host publication after hydration.
    proxy = TaskReportStore("task-1", tmp_path)
    with pytest.raises(ValueError, match="missing-output-attachments"):
        proxy.record_task_review("complete", _criteria(), [], [])
    proxy.record_task_review("needs_work", _criteria("unmet"), ["Missing an output"], ["Capture it"])
    receipt["quality"] = {"analysisComplete": True, "gaps": []}
    store.record_result("publish_frame_render_flow", receipt)
    proxy.record_task_review("complete", _criteria(), [], [])
    assert proxy.task_review_state()["fresh"]


def test_frame_gap_does_not_leak_to_unrelated_new_task_scope(tmp_path):
    store = _store(tmp_path)
    store.record_result("publish_frame_render_flow", {"schema": "bread-frame-render-flow-publication/v1",
        "flowId": "flow-test", "quality": {"analysisComplete": False, "gaps": []}})
    store.begin_task_review(hashlib.sha256(b"explain a separate question").hexdigest())
    store.record_task_review("complete", _criteria(), [], [])


def test_deleted_task_cannot_reopen_its_in_memory_reanalysis(tmp_path):
    store = _store(tmp_path)
    store.record_result("prepare_frame_render_flow", {
        "flowId": "flow-test", "reanalysisRequested": True}, arguments={"reanalyze": True})
    assert store.frame_reanalysis_pending("flow-test")
    marker = tmp_path / ".deleted-tasks" / "task-1.json"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"schema": "bread-deleted-task/v1", "taskId": "task-1"}))
    assert not store.enabled
    assert not store.frame_reanalysis_pending("flow-test")


def test_frame_reanalysis_requires_a_new_publication_and_failed_correction_invalidates_old_success(tmp_path):
    store = _store(tmp_path)
    receipt = {"schema": "bread-frame-render-flow-publication/v1", "flowId": "flow-test",
               "quality": {"analysisComplete": True, "gaps": []}}
    store.record_result("publish_frame_render_flow", receipt)
    store.record_task_review("complete", _criteria(), [], [])
    store.record_result("prepare_frame_render_flow", {
        "flowId": "flow-test", "reanalysisRequested": True}, arguments={"reanalyze": True})
    assert TaskReportStore("task-1", tmp_path).frame_reanalysis_pending("flow-test")
    with pytest.raises(ValueError, match="fresh-analysis-not-published"):
        store.record_task_review("complete", _criteria(), [], [])
    store.record_result("publish_frame_render_flow", receipt)
    assert not TaskReportStore("task-1", tmp_path).frame_reanalysis_pending("flow-test")
    store.record_result("publish_frame_render_flow", "Invalid attachment", failed=True)
    # Reading an earlier accepted quality report cannot certify a failed edit.
    store.record_result("get_frame_render_flow", {"quality": receipt["quality"]})
    with pytest.raises(ValueError, match="latest-publication-failed"):
        store.record_task_review("complete", _criteria(), [], [])
    store.record_result("publish_frame_render_flow", receipt)
    store.record_task_review("complete", _criteria(), [], [])
    store.record_result("prepare_frame_render_flow", {
        "flowId": "flow-test", "reanalysisRequested": True}, arguments={"reanalyze": False})
    store.record_task_review("complete", _criteria(), [], [])


@pytest.mark.parametrize("arguments", [{}, {"flow_id": "flow-typo"}])
def test_failed_first_publication_can_be_recovered_with_a_resolved_flow_id(tmp_path, arguments):
    store = _store(tmp_path)
    store.record_result("publish_frame_render_flow", "Capture unavailable", failed=True, arguments=arguments)
    with pytest.raises(ValueError, match="latest-publication-failed"):
        store.record_task_review("complete", _criteria(), [], [])
    store.record_result("publish_frame_render_flow", {
        "schema": "bread-frame-render-flow-publication/v1", "flowId": "flow-recovered",
        "quality": {"analysisComplete": True, "gaps": []}})
    store.record_task_review("complete", _criteria(), [], [])


def test_later_read_invalidates_review_but_duplicate_facts_are_not_new_progress(tmp_path):
    store = _store(tmp_path)
    result = {"eventId": 77, "revision": 3, "sourceSha256": "a" * 64, "elapsedMs": 1, "timestampUnixNs": 10}
    store.record_result("inspect_event", result)
    first = store.task_review_state()
    store.record_task_review("complete", _criteria(), [], [])
    store.record_result("inspect_event", {**result, "elapsedMs": 200, "timestampUnixNs": 900})
    repeated = store.task_review_state()
    assert repeated["fresh"] is False and repeated["workRevision"] > first["workRevision"]
    assert repeated["workFingerprint"] == first["workFingerprint"]
    store.record_result("inspect_event", {**result, "revision": 4})
    changed = store.task_review_state()
    assert changed["workFingerprint"] != first["workFingerprint"]
    store.record_task_review("complete", _criteria(), [], [])
    assert store.task_review_state()["fresh"] is True


def test_runtime_occurrence_dedup_and_stable_progress_are_separate(tmp_path):
    store = _store(tmp_path)
    store.record_task_activity("fs/write_text_file", "source-sha-a", occurrence_id="write-1")
    first = store.task_review_state()
    store.record_task_review("complete", _criteria(), [], [])
    store.record_task_activity("fs/write_text_file", "source-sha-a", occurrence_id="write-1")
    assert store.task_review_state()["fresh"] is True
    store.record_task_activity("fs/write_text_file", "source-sha-a", occurrence_id="write-2")
    repeated = store.task_review_state()
    assert repeated["workRevision"] == first["workRevision"] + 1 and repeated["fresh"] is False
    assert repeated["workFingerprint"] == first["workFingerprint"]
    store.record_task_activity("fs/write_text_file", "source-sha-b", occurrence_id="write-3")
    assert store.task_review_state()["workFingerprint"] != first["workFingerprint"]


def test_review_notes_and_repeated_review_tools_never_count_as_work(tmp_path):
    store = _store(tmp_path)
    before = store.task_review_state()
    store.record_task_review("needs_work", _criteria("unmet"), ["Example is missing"], ["Add the example"])
    store.record_result("record_task_review", {"verdict": "needs_work", "findings": ["changed note"]})
    store.record_result("record_shader_organization_review", {"verdict": "complete", "checks": {"readability": "source facts"}})
    store.record_task_review("needs_work", _criteria("unmet"), ["Another explanation of same missing example"], ["Add it"])
    after = store.task_review_state()
    assert after["workRevision"] == before["workRevision"]
    assert after["workFingerprint"] == before["workFingerprint"]


def test_reused_task_id_starts_new_scope_and_rejects_old_store_writes(tmp_path):
    old = _store(tmp_path)
    first = old.record_task_review("complete", _criteria(), [], [])
    current = TaskReportStore("task-1", tmp_path)
    state = current.begin_task_review(hashlib.sha256(b"current user request").hexdigest())
    assert state["scopeId"] != first["scopeId"] and state["review"] is None
    with pytest.raises(ValueError, match="different execution"):
        old.record_task_review("complete", _criteria(), [], [])
    with pytest.raises(ValueError, match="different execution"):
        old.record_task_activity("native-edit", "sha", occurrence_id="late-old-task")
    assert current.task_review_state()["review"] is None


def test_cross_task_or_stale_persisted_review_never_becomes_fresh(tmp_path):
    first = _store(tmp_path, "first")
    review = first.record_task_review("complete", _criteria(), [], [])
    second = _store(tmp_path, "second")
    path = task_report_state_path(tmp_path, "second")
    stored = json.loads(path.read_text())
    stored["taskCompletionReview"]["review"] = review
    path.write_text(json.dumps(stored))
    assert second.task_review_state()["review"] is None
    stored["taskCompletionReview"]["review"] = {**review, "taskId": "second",
        "scopeId": stored["taskCompletionReview"]["scopeId"], "criteria": []}
    path.write_text(json.dumps(stored))
    assert second.task_review_state()["fresh"] is False


@pytest.mark.parametrize("verdict,criteria,findings,actions,blocker", [
    ("complete", [], [], [], ""),
    ("complete", _criteria("unmet"), [], [], ""),
    ("complete", _criteria(), ["unfixed"], [], ""),
    ("complete", [{"requirement": "task", "status": "satisfied", "evidence": ""}], [], [], ""),
    ("needs_work", _criteria("unmet"), ["missing"], [], ""),
    ("blocked", _criteria("blocked"), [], [], ""),
    ("complete", [{**_criteria()[0], "status": "met"}], [], [], ""),
])
def test_malformed_or_contradictory_assessments_are_rejected(tmp_path, verdict, criteria, findings, actions, blocker):
    with pytest.raises(ValueError):
        _store(tmp_path).record_task_review(verdict, criteria, findings, actions, blocker)


def test_blocked_review_preserves_agent_reason_without_host_quality_judgment(tmp_path):
    store = _store(tmp_path)
    receipt = store.record_task_review("blocked", _criteria("blocked"), ["Requested evidence unavailable"],
        ["User must provide the missing capture"], "No capture was supplied")
    assert store.task_review_state()["fresh"] is True and receipt["blocker"] == "No capture was supplied"
    assert receipt["hostSemanticApproval"] is False


def test_failed_reassessment_cannot_reuse_an_older_complete_verdict(tmp_path):
    store = _store(tmp_path)
    store.record_task_review("complete", _criteria(), [], [])
    before = store.task_review_state()
    with pytest.raises(ValueError, match="next_actions"):
        store.record_task_review("needs_work", _criteria("unmet"), ["Important gap found"], [])
    state = TaskReportStore("task-1", tmp_path).task_review_state()
    assert state["review"] is None and state["fresh"] is False
    assert "next_actions" in state["reviewError"]
    assert state["workRevision"] == before["workRevision"]
    assert state["workFingerprint"] == before["workFingerprint"]
    store.record_task_review("complete", _criteria(), [], [])
    assert store.task_review_state()["fresh"] is True and "reviewError" not in store.task_review_state()


def test_actual_tool_failure_invalidates_review_without_repeated_error_progress(tmp_path):
    store = _store(tmp_path)
    store.record_result("compile_shader_workspace", {"error": "compiler unavailable"}, failed=True)
    before = store.task_review_state()
    store.record_task_review("blocked", _criteria("blocked"), [], [], "compiler unavailable")
    store.record_result("compile_shader_workspace", {"error": "compiler unavailable"}, failed=True)
    after = store.task_review_state()
    assert after["fresh"] is False and after["workFingerprint"] == before["workFingerprint"]


def test_pre_call_review_failure_invalidates_once_and_preserves_work_identity(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.record_task_review("complete", _criteria(), [], [])
    before = store.task_review_state()
    writes = []
    write_state = store._write_state
    def count_write():
        writes.append(True)
        write_state()
    monkeypatch.setattr(store, "_write_state", count_write)
    with pytest.raises(TypeError) as failure:
        store.record_task_review(verdict="complete")
    store.invalidate_task_review(str(failure.value))
    store.invalidate_task_review(str(failure.value))
    state = TaskReportStore("task-1", tmp_path).task_review_state()
    assert state["review"] is None and state["fresh"] is False
    assert state["reviewError"] == str(failure.value)[:600]
    assert state["workRevision"] == before["workRevision"]
    assert state["workFingerprint"] == before["workFingerprint"]
    assert len(writes) == 1
    store.record_task_review("complete", _criteria(), [], [])
    current = TaskReportStore("task-1", tmp_path)
    current.begin_task_review(hashlib.sha256(b"next request").hexdigest())
    current.record_task_review("complete", _criteria(), [], [])
    with pytest.raises(ValueError, match="different execution"):
        store.invalidate_task_review("old execution's delayed failure")
    assert current.task_review_state()["fresh"] is True


def test_concurrent_local_and_host_receipts_merge_without_losing_review_scope(tmp_path):
    store = _store(tmp_path)
    def add(index):
        other = TaskReportStore("task-1", tmp_path)
        other.record_task_activity("external-tool", f"result-{index}", occurrence_id=f"call-{index}")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(add, range(12)))
    assert store.task_review_state()["workRevision"] == 12
    store.record_task_review("complete", _criteria(), [], [])
    assert store.task_review_state()["fresh"] is True


def test_server_tool_reuses_shared_ledger_without_preparatory_tool(tmp_path, monkeypatch):
    monkeypatch.setenv(TASK_ID_ENV, "server-task")
    monkeypatch.setenv(TASK_EVENT_ROOT_ENV, str(tmp_path))
    from src.task_report import current_task_report_store
    store = current_task_report_store()
    store.begin_task_review(hashlib.sha256(b"ordinary question").hexdigest())
    reply = json.loads(server.record_task_review("complete", _criteria(), [], []))
    assert reply["taskId"] == "server-task" and store.task_review_state()["fresh"] is True
    assert store.task_review_state()["workRevision"] == 0
    assert read_task_report_sidecars(tmp_path, "server-task")[1]["toolsUsed"] == ["record_task_review"]


def test_activity_digest_ignores_timing_but_keeps_real_artifact_revision():
    first = {"value": "same", "revision": 3, "elapsed": 1, "duration": 2, "elapsedMs": 3, "requestId": "one"}
    second = {**first, "elapsed": 44, "duration": 55, "elapsedMs": 66, "requestId": "two"}
    assert activity_identity("inspect", first) == activity_identity("inspect", second)
    assert activity_identity("inspect", first) != activity_identity("inspect", {**second, "revision": 4})


def test_instrumented_non_shader_arguments_distinguish_work_without_storing_them(tmp_path, monkeypatch):
    from src.task_report import current_task_report_store, instrument_task_tool
    monkeypatch.setenv(TASK_ID_ENV, "generic-work")
    monkeypatch.setenv(TASK_EVENT_ROOT_ENV, str(tmp_path))
    store = current_task_report_store()
    store.begin_task_review(hashlib.sha256(b"edit requested setting").hexdigest())
    @instrument_task_tool
    def edit_setting(value: str, label: str = "default"):
        return {"success": True}
    edit_setting("private-setting-one")
    before = store.task_review_state()
    edit_setting("private-setting-one", label="default")
    assert store.task_review_state()["workFingerprint"] == before["workFingerprint"]
    edit_setting("private-setting-two")
    assert store.task_review_state()["workFingerprint"] != before["workFingerprint"]
    assert "private-setting" not in task_report_state_path(tmp_path, "generic-work").read_text()


def test_observation_storage_is_bounded_without_false_new_progress(tmp_path, monkeypatch):
    monkeypatch.setattr("src.task_completion_review.MAX_IDENTITIES", 2)
    store = _store(tmp_path)
    for number in range(3):
        store.record_task_activity("read", f"value-{number}", occurrence_id=str(number))
    before = store.task_review_state()
    assert before["progressTrackingSaturated"] is True and before["workRevision"] == 3
    store.record_task_activity("read", "value-0", occurrence_id="new-call")
    after = store.task_review_state()
    assert after["workRevision"] == 4 and after["workFingerprint"] == before["workFingerprint"]
