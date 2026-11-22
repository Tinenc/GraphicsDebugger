from __future__ import annotations

import pytest

from replaylab_agent.agent_report import TaskReportStore
from replaylab_agent.task_review import TaskActivityTracker, TaskReviewDecision, TaskReviewLoop, completion_review_state, work_identity


def review(verdict="complete"):
    return {"verdict": verdict, "criteria": [{"requirement": "Explain the requested concept", "status": "satisfied", "evidence": "The answer explains it."}],
            "findings": [], "nextActions": [], "blocker": ""}


def state(verdict="complete", **kwargs):
    return {"scopeId": "scope", "fresh": True, "workFingerprint": "facts", "review": review(verdict), **kwargs}


def test_explicit_fresh_user_decision_stops_automatic_recovery_and_preserves_question():
    value=review("blocked")
    value.update(blocker="Only the choice of TAA reconstruction strategy remains",decisionRequest=dict(
        question="Continue, approximate, or disable TAA?",options=["Continue","Approximate","Disable"],evidence=["Saved pass comparison"]))
    shader={"targets":[{"workspacePath":"shader","sourceSha256":"a"*64,"changed":True,"review":{}}]}
    result=TaskReviewLoop.for_intent(None).decide(state(review=value),shader)
    assert result.action=="partial" and result.reason=="awaiting-user-decision"
    text=result.incomplete_text("zh_CN","已保存重建场景和对照。")
    assert value["decisionRequest"]["question"] in text and "Approximate" in text
    assert "等待你的选择" in text


def test_stale_user_decision_does_not_stop_new_authorized_work():
    value=review("blocked")
    value.update(blocker="An old choice",decisionRequest=dict(question="Old question?",options=["A","B"],evidence=["Old facts"]))
    result=TaskReviewLoop.for_intent(None).decide(state(review=value,fresh=False),{})
    assert result.reason!="awaiting-user-decision" and result.action=="continue"


def test_plain_answer_review_accepts_existing_evidence_without_artifact_or_tool_work():
    assert TaskReviewLoop.for_intent(None).decide(state(), {}).action == "accept"
    loop = TaskReviewLoop.for_intent(None)
    first = loop.decide(state(review=None), {})
    assert first.action == "continue" and "no new investigation" in first.continuation_prompt()
    assert loop.decide(state(), {}).action == "accept"


def test_followup_inspection_still_requires_task_review_but_no_shader_rewrite():
    shader = {"targets": [{"workspacePath": "shader", "sourceSha256": "a" * 64,
                           "changed": False, "sourceWorkObserved": False, "review": {}}]}
    loop = TaskReviewLoop.for_intent(None)
    missing = loop.decide(state(review=None), shader)
    assert missing.action == "continue"
    assert missing.shader.action == "accept"
    assert "Shader-specific checks remain" not in missing.continuation_prompt()
    assert loop.decide(state(), shader).action == "accept"


def test_joint_progress_does_not_stall_on_unchanged_shader_target():
    loop = TaskReviewLoop.for_intent(None)
    shader = {"targets": [{"workspacePath": "shader", "sourceSha256": "a" * 64, "changed": True, "review": {}}]}
    for index in range(6):
        assert loop.decide(state(review=None, workFingerprint=str(index)), shader).action == "continue"
    assert loop.decide(state(review=None, workFingerprint="5"), shader).reason == "no-progress"


def test_blocked_result_retains_completed_evidence_and_actual_reason():
    value = review("blocked")
    value["blocker"] = "The requested capture file is unavailable"
    loop = TaskReviewLoop.for_intent(None)
    reconsider = loop.decide(state(review=value), {})
    assert reconsider.action == "continue" and reconsider.reason == "blocker-recovery-review"
    assert "do not bypass that boundary" in reconsider.continuation_prompt()
    result = loop.decide(state(review=value), {})
    assert result.action == "partial"
    assert "The answer explains it" in result.incomplete_text("en_US")
    assert "capture file is unavailable" in result.incomplete_text("en_US")
    assert "/output/delivered.txt" in result.incomplete_text("en_US", "Saved /output/delivered.txt")


def test_partial_answer_does_not_expand_internal_review_ledger_or_duplicate_blocker():
    import copy
    value = review("blocked")
    value.update(blocker="The final buffer use could not be verified. Detailed recovery evidence follows.",
        criteria=[{"requirement": "Preserve completed output", "status": "satisfied", "evidence": "receipt hash " * 500}] * 16,
        findings=["internal tool findings " * 100] * 8, nextActions=["internal recovery details " * 100] * 8)
    original = copy.deepcopy(value)
    candidate = "The diagram is saved: [open result](/output/diagram.png).\n\nThe final buffer use could not be verified."
    decision = TaskReviewDecision("partial", "blocked-after-recovery-review", state(review=value))
    text = decision.incomplete_text("en_US", candidate)
    assert text == "The task is incomplete.\n\n" + candidate
    assert value == original  # Compact display never discards audit evidence.
    assert "internal" not in text and text.count("could not be verified") == 1


def test_partial_answer_keeps_actual_blocker_when_candidate_omits_it():
    value = review("blocked")
    value["blocker"] = "The output binding changed during capture inspection. Repeated diagnostics are in the log."
    text = TaskReviewDecision("partial", "blocked", state(review=value)).incomplete_text("en_US", "The draft is saved.")
    assert "The task is incomplete." in text
    assert "The draft is saved." in text
    assert "Reason: The output binding changed during capture inspection." in text
    assert "Repeated diagnostics" not in text


def test_missing_agent_answer_uses_a_small_review_summary_and_keeps_remaining_work():
    value = review("blocked")
    value["criteria"] += [{"requirement": "Verify final composition", "status": "blocked", "evidence": "long diagnostics " * 100}]
    value.update(blocker="The final target cannot be read.", nextActions=["Restore the capture connection."])
    text = TaskReviewDecision("partial", "blocked", state(review=value)).incomplete_text("en_US")
    assert "The answer explains it." in text
    assert "Remaining: Verify final composition" in text
    assert "Reason: The final target cannot be read." in text
    assert "Next step: Restore the capture connection." in text
    assert "long diagnostics" not in text


def test_partial_wrapper_preserves_requested_substantive_answer_instead_of_slicing_it():
    answer = "A requested detailed explanation.\n" * 200 + "[Saved artifact](/output/result.png)"
    text = TaskReviewDecision("partial", "blocked", state(review=review("blocked"))).incomplete_text("en_US", answer)
    assert answer in text
    assert text.startswith("The task is incomplete.")


def test_chinese_partial_message_uses_plain_status_and_short_actual_cause():
    value = review("blocked")
    value["blocker"] = "结果还不能通过最终检查。后面是重复的工具记录。"
    text = TaskReviewDecision("partial", "blocked-after-recovery-review", state(review=value)).incomplete_text("zh_CN", "流程图已保存并打开。")
    assert text == "任务尚未完成。\n\n流程图已保存并打开。\n\n原因：结果还不能通过最终检查。"


def test_blocker_rephrasing_and_next_action_edits_do_not_create_new_work():
    loop = TaskReviewLoop.for_intent(None)
    first = review("blocked")
    first.update(blocker="Evidence is incomplete", nextActions=["Inspect an untried artifact"])
    assert loop.decide(state(review=first), {}).reason == "blocker-recovery-review"
    edited = {**first, "blocker": "Still incomplete evidence", "nextActions": ["Try another wording"]}
    assert loop.decide(state(review=edited), {}).reason == "blocked-after-recovery-review"
    assert loop.decide(state(review=edited, workFingerprint="new-observed-facts"), {}).action == "continue"


def test_shader_blocker_receives_same_generic_recovery_opportunity():
    loop = TaskReviewLoop.for_intent(None)
    shader = {"targets": [{"workspacePath": "shader", "sourceSha256": "a" * 64, "changed": True,
                           "fresh": True, "saved": True,
                           "review": {"verdict": "blocked", "blocker": "Untried evidence route", "nextActions": ["Inspect reference"]}}]}
    first = loop.decide(state(), shader)
    assert first.action == "continue" and first.reason == "blocker-recovery-review"
    assert "Untried evidence route" in first.continuation_prompt()
    assert loop.decide(state(), shader).action == "partial"


def test_status_cycles_without_new_work_are_not_progress():
    loop = TaskReviewLoop.for_intent(None)
    assert loop.decide(state("needs_work"), {}).action == "continue"
    assert loop.decide(state("needs_work", fresh=False), {}).action == "continue"
    assert loop.decide(state("needs_work"), {}).reason == "no-progress"


def test_repeated_native_results_invalidate_freshness_without_manufacturing_progress(tmp_path):
    store = TaskReportStore("activity", tmp_path)
    store.begin_task_review("a" * 64)
    tracker = TaskActivityTracker(store)
    tracker.native_event({"activityId": "first", "activityKind": "commandExecution", "status": "completed"})
    store.record_task_review("complete", review()["criteria"], [], [])
    first = store.task_review_state()
    tracker.native_event({"activityId": "second", "activityKind": "commandExecution", "status": "completed"})
    second = completion_review_state(store, tracker)
    assert second["workRevision"] > first["workRevision"] and second["fresh"] is False
    assert first["workFingerprint"] == second["workFingerprint"]
    tracker.native_event({"activityId": "third", "activityKind": "commandExecution", "status": "completed", "workIdentity": "b" * 64})
    assert store.task_review_state()["workFingerprint"] != second["workFingerprint"]


def test_dynamic_timing_metadata_is_not_progress_and_firstparty_is_not_double_written(tmp_path):
    assert work_identity({"x": 1, "elapsedSeconds": 1, "timestamp": "a"}) == work_identity({"x": 1, "elapsedSeconds": 99, "timestamp": "b"})
    store = TaskReportStore("dedup", tmp_path)
    store.begin_task_review("a" * 64)
    tracker = TaskActivityTracker(store)
    tracker.native_event({"activityId": "host", "activityKind": "mcpToolCall", "activityTarget": "Bread.inspect_event", "status": "completed"})
    tracker.native_event({"activityId": "review", "activityKind": "mcpToolCall", "activityTarget": "Bread.record_task_review", "status": "completed"})
    assert store.task_review_state()["workRevision"] == 0


@pytest.mark.asyncio
async def test_external_transport_failure_revokes_pass_once_and_repeated_failure_is_not_progress(tmp_path):
    from replaylab_agent.mcp_proxy import _reviewed_external_call
    store = TaskReportStore("external-failure", tmp_path)
    store.begin_task_review("a" * 64)
    store.record_task_review("complete", review()["criteria"], [], [])

    class External:
        async def call(self, name, arguments):
            raise RuntimeError("External connection closed after call")

    for index in range(2):
        with pytest.raises(RuntimeError, match="connection closed"):
            await _reviewed_external_call(External(), "external_export", {"asset": "mesh"}, store)
        observed = store.task_review_state()
        assert not observed["fresh"] and observed["workRevision"] == index + 1
        if index == 0:
            fingerprint = observed["workFingerprint"]
        else:
            assert observed["workFingerprint"] == fingerprint


def test_rejected_host_call_invalidates_pass_even_without_host_receipt(tmp_path):
    store = TaskReportStore("host-rejected", tmp_path)
    store.begin_task_review("a" * 64)
    store.record_task_review("complete", review()["criteria"], [], [])
    TaskActivityTracker(store).native_event({"activityId": "denied", "activityKind": "mcpToolCall",
        "activityTarget": "Bread.inspect_event", "status": "failed"})
    assert store.task_review_state()["fresh"] is False


def test_whole_task_complete_cannot_remove_an_observed_shader_display_requirement():
    shader = {"targets": [{"workspacePath": "shader", "sourceSha256": "a" * 64, "changed": True,
        "fresh": True, "saved": True, "review": {"verdict": "complete"},
        "cbufferSemanticDelivery": {"required": True, "sourceSha256": "a" * 64, "status": "pending"}}]}
    loop = TaskReviewLoop.for_intent(None)
    first = loop.decide(state(), shader)
    assert first.action == "continue" and first.shader.targets[0]["reviewStatus"] == "semantic-delivery-pending"
    assert "publish_cbuffer_aliases" in first.continuation_prompt()
    # A new complete claim without actual publication cannot cycle forever.
    assert loop.decide(state(), shader).reason == "no-progress"
    shader["targets"][0]["cbufferSemanticDelivery"]["status"] = "complete"
    assert loop.decide(state(workFingerprint="publication-recorded"), shader).action == "accept"


def test_joint_review_counts_all_binding_metadata_progress_at_unchanged_source():
    delivery = {"required": True, "sourceSha256": "a" * 64, "status": "pending",
                "bindingCoverage": {"bindingCount": 5, "reviewedBindingCount": 0}}
    shader = {"targets": [{"workspacePath": "shader", "sourceSha256": "a" * 64, "changed": True,
        "fresh": True, "saved": True, "review": {"verdict": "complete"}, "cbufferSemanticDelivery": delivery}]}
    loop = TaskReviewLoop.for_intent(None)
    for count in range(5):
        delivery["bindingCoverage"]["reviewedBindingCount"] = count
        assert loop.decide(state(), shader).action == "continue"
    delivery.update(evidence="Different wording", timestamp="Later", receiptHash="new note")
    assert loop.decide(state(), shader).reason == "no-progress"
