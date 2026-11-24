from __future__ import annotations

import copy
import json

from replaylab_agent.shader_review import ShaderReviewLoop, review_state


def target(verdict="complete", **kwargs):
    return {"workspacePath": "shader", "organizationId": "org", "sourceKind": "proposal",
            "sourceSha256": "a" * 64, "changed": True, "saved": True, "fresh": True,
            "review": {"verdict": verdict}, **kwargs}


def state(*targets):
    return {"targets": list(targets)}


def test_questions_are_not_forced_to_organize_and_explicit_intent_cannot_skip_all_work():
    assert ShaderReviewLoop().decide({}).action == "accept"
    loop = ShaderReviewLoop.for_intent({"kind": "reverse-edit-selected-shader"})
    assert loop.decide({}).action == "continue"
    stalled = loop.decide({})
    assert stalled.action == "partial" and stalled.reason == "no-progress"
    assert "not created" in stalled.incomplete_text("en_US")


def test_unchanged_inspection_draft_does_not_expand_a_followup_into_editing():
    row = target(changed=False, sourceWorkObserved=False, fresh=False, review={})
    assert ShaderReviewLoop().decide(state(row)).action == "accept"
    explicit = ShaderReviewLoop.for_intent({"kind": "reverse-edit-selected-shader"})
    assert explicit.decide(state(row)).action == "continue"
    # Native edits, successful source operations and a claimed source review
    # all retain the normal source-quality obligation.
    for options in ({"changed": True}, {"sourceWorkObserved": True},
                    {"review": {"verdict": "needs_work"}},
                    {"diagnostic": "Source could not be read"},
                    {"sourceConflict": True}):
        assert ShaderReviewLoop().decide(state({**row, **options})).action == "continue"
    legacy = {key: value for key, value in row.items() if key != "sourceWorkObserved"}
    assert ShaderReviewLoop().decide(state(legacy)).action == "continue"


def test_source_bound_agent_review_requires_real_changes_and_saved_delivery():
    for options, expected in [({"fresh": False}, "stale"), ({"changed": False}, "unchanged"),
                              ({"saved": False}, "unsaved"), ({"review": {}}, "missing")]:
        decision = ShaderReviewLoop().decide(state(target(**options)))
        assert decision.action == "continue"
        assert decision.targets[0]["reviewStatus"] == expected
    assert ShaderReviewLoop().decide(state(target())).action == "accept"


def test_legacy_complete_cannot_skip_structured_whole_source_coverage():
    row = target(scopeCoverageComplete=False)
    decision = ShaderReviewLoop().decide(state(row))
    assert decision.action == "continue"
    assert decision.targets[0]["reviewStatus"] == "scope-incomplete"
    assert "structured coverage" in decision.incomplete_text("en_US")
    row["scopeCoverageComplete"] = True
    assert ShaderReviewLoop().decide(state(row)).action == "accept"


def test_missing_independent_directory_has_metadata_repair_guidance_without_new_compile():
    row = target(scopeCoverageComplete=True, semanticOverviewComplete=False)
    decision = ShaderReviewLoop().decide(state(row))
    assert decision.action == "continue"
    assert decision.targets[0]["reviewStatus"] == "overview-pending"
    assert "update_shader_semantic_overview" in decision.continuation_prompt()
    assert "without recompiling" in decision.incomplete_text("en_US")
    row["semanticOverviewComplete"] = True
    assert ShaderReviewLoop().decide(state(row)).action == "accept"


def test_reviewed_directory_still_needs_requested_ui_delivery_without_recompilation():
    row = target(scopeCoverageComplete=True, semanticOverviewComplete=True,
                 semanticOverviewDisplayRequired=True, semanticOverviewDisplayComplete=False,
                 semanticOverviewDisplay={"complete": False, "reason": "UI shows provisional calls"})
    decision = ShaderReviewLoop().decide(state(row))
    assert decision.action == "continue"
    assert decision.targets[0]["reviewStatus"] == "overview-display-pending"
    assert "show_shader_workspace_source" in decision.continuation_prompt()
    assert "get_shader_workspace_ui_state" in decision.continuation_prompt()
    assert "repeat finish" in decision.continuation_prompt()
    row["semanticOverviewDisplayComplete"] = True
    assert ShaderReviewLoop().decide(state(row)).action == "accept"


def test_continuation_carries_exact_draft_and_unmet_scope_without_mutating_evidence():
    row = target("needs_work", saved=False, review={
        "verdict": "needs_work", "findings": ["Fallback path still needs organization"],
        "nextActions": ["Repair the fallback and its caller in the working copy"],
        "scopeReview": {"results": [{"id": "fallback", "status": "unmet",
                                      "remaining_work": ["Group its material calculation"]}]},
    })
    before = copy.deepcopy(row)
    decision = ShaderReviewLoop().decide(state(row))
    assert decision.action == "continue"
    supplied = json.loads(decision.continuation_prompt().split("\n\n", 1)[1].split("\n", 1)[1])
    assert supplied[0]["organizationId"] == row["organizationId"]
    assert supplied[0]["sourceSha256"] == row["sourceSha256"]
    assert supplied[0]["saved"] is False
    assert supplied[0]["review"]["scopeGaps"] == [
        {"id": "fallback", "status": "unmet", "remainingWork": ["Group its material calculation"]}]
    assert row == before


def test_actual_changes_allow_many_rounds_but_repeated_notes_or_reads_do_not():
    loop = ShaderReviewLoop()
    for revision in range(8):
        row = target("needs_work", sourceSha256=str(revision) * 64)
        assert loop.decide(state(row)).action == "continue"
    row["organizationId"] = "restarted-session"
    row["review"]["nextActions"] = ["Read again"]
    decision = loop.decide(state(row))
    assert decision.action == "partial" and decision.reason == "no-progress"


def test_all_workspaces_need_current_review_and_blocker_remains_visible():
    blocked = target(review={"verdict": "blocked", "blocker": "Missing captured function body"})
    pending = target("needs_work", workspacePath="second")
    loop = ShaderReviewLoop()
    assert loop.decide(state(blocked, pending)).action == "continue"
    pending["review"] = {"verdict": "complete"}
    decision = loop.decide(state(blocked, pending))
    assert decision.action == "partial" and decision.reason == "blocked"
    assert "Missing captured function body" in decision.incomplete_text("en_US")


def test_save_or_external_source_conflict_resolution_counts_as_progress():
    loop = ShaderReviewLoop()
    row = target(saved=False, canonicalSourceSha256="b" * 64)
    assert loop.decide(state(row)).action == "continue"
    row["canonicalSourceSha256"] = "c" * 64
    assert loop.decide(state(row)).action == "continue"
    row["saved"] = True
    assert loop.decide(state(row)).action == "accept"


def test_all_targets_are_checked_and_io_failure_is_partial_without_agent_retry():
    rows = [target(workspacePath=f"shader-{index}") for index in range(40)]
    rows[-1]["fresh"] = False
    assert ShaderReviewLoop().decide(state(*rows)).targets[0]["workspacePath"] == "shader-39"

    class BrokenStore:
        def shader_organization_review_state(self):
            raise OSError("review sidecar is locked")

    decision = ShaderReviewLoop().decide(review_state(BrokenStore()))
    assert decision.action == "partial" and decision.reason == "review-state-unavailable"
    assert "locked" in decision.incomplete_text("en_US")


def test_no_change_complete_review_is_accepted_with_explicit_factual_note():
    row = target(changed=False, review={"verdict": "complete", "noChangeReason": "All four requested checks already hold."})
    decision = ShaderReviewLoop().decide(state(row))
    assert decision.action == "accept"
    assert "unchanged this turn" in decision.unchanged_note("en_US")
    row["saved"] = False
    assert ShaderReviewLoop().decide(state(row)).action == "continue"


def test_complete_source_review_cannot_omit_required_cbuffer_display_effect():
    row = target(cbufferSemanticDelivery={"required": True, "sourceSha256": "a" * 64,
                                         "status": "pending", "reason": "publication-source-stale"})
    loop = ShaderReviewLoop()
    first = loop.decide(state(row))
    assert first.action == "continue"
    assert first.targets[0]["reviewStatus"] == "semantic-delivery-pending"
    prompt = first.continuation_prompt()
    assert "publish_cbuffer_aliases" in prompt and "semantic_source_sha256" in prompt
    assert "do not rewrite source, repeat source review, compile" in prompt
    assert "record_shader_organization_review" not in prompt
    assert "publication-source-stale" in prompt
    assert loop.decide(state(row)).reason == "no-progress"
    row["cbufferSemanticDelivery"]["status"] = "complete"
    assert loop.decide(state(row)).action == "accept"
    row["cbufferSemanticDelivery"]["sourceSha256"] = "b" * 64
    assert ShaderReviewLoop().decide(state(row)).action == "continue"


def test_shader_inspection_does_not_inherit_panel_effect_but_real_source_work_does():
    semantics = {"required": True, "sourceSha256": "a" * 64, "status": "pending"}
    row = target(changed=False, sourceWorkObserved=False, fresh=False, review={}, cbufferSemanticDelivery=semantics)
    assert ShaderReviewLoop().decide(state(row)).action == "accept"
    row.update(changed=True, fresh=True, review={"verdict": "complete"})
    assert ShaderReviewLoop().decide(state(row)).targets[0]["reviewStatus"] == "semantic-delivery-pending"
    row["cbufferSemanticDelivery"] = {"required": False, "status": "not-applicable"}
    assert ShaderReviewLoop().decide(state(row)).action == "accept"


def test_complete_material_block_cannot_cover_sibling_bindings_or_resource_review():
    delivery = {"required": True, "status": "complete", "sourceSha256": "a" * 64,
                "bindingCoverage": {"complete": False, "bindingCount": 5, "reviewedBindingCount": 1}}
    row = target(cbufferSemanticDelivery=delivery)
    assert ShaderReviewLoop().decide(state(row)).action == "continue"
    delivery["bindingCoverage"]["complete"] = True
    delivery["resourceNamingDelivery"] = {"required": True, "status": "pending", "sourceSha256": "a" * 64}
    decision = ShaderReviewLoop().decide(state(row))
    assert decision.action == "continue"
    assert "record_shader_resource_naming_review" in decision.continuation_prompt()
    assert "Do not call finish_shader_workspace_organization again" in decision.continuation_prompt()
    delivery["resourceNamingDelivery"]["status"] = "complete"
    assert ShaderReviewLoop().decide(state(row)).action == "accept"
    delivery["resourceNamingDelivery"]["sourceSha256"] = "b" * 64
    assert ShaderReviewLoop().decide(state(row)).action == "continue"


def test_same_source_all_binding_progress_continues_but_new_review_prose_does_not():
    loop = ShaderReviewLoop()
    delivery = {"required": True, "status": "pending", "sourceSha256": "a" * 64,
                "bindingCoverage": {"complete": False, "bindingCount": 5, "reviewedBindingCount": 0},
                "resourceNamingDelivery": {"required": True, "status": "pending", "sourceSha256": "a" * 64,
                    "textureCoverage": {"bindingCount": 3, "reviewedBindingCount": 0, "missingCount": 3}}}
    row = target(cbufferSemanticDelivery=delivery)
    for count in range(5):
        delivery["bindingCoverage"]["reviewedBindingCount"] = count
        assert loop.decide(state(row)).action == "continue"
    for count in range(1, 4):
        delivery["resourceNamingDelivery"]["textureCoverage"].update(reviewedBindingCount=count, missingCount=3-count)
        assert loop.decide(state(row)).action == "continue"
    delivery["resourceNamingDelivery"].update(evidence="New wording", receiptHash="new-review-hash", timestamp="new-time")
    assert loop.decide(state(row)).reason == "no-progress"
