"""Capability failures must try concrete alternatives without an infinite loop."""
import copy

from replaylab_agent.task_review import TaskReviewLoop


def state():
    return {"scopeId": "task", "fresh": True, "workFingerprint": "same-files-and-render",
        "review": {"verdict": "blocked", "blocker": "Compute is unavailable"},
        "unityRestorations": [{"restorationId": "a" * 32, "complete": False, "revision": 1,
            "stateFingerprint": "same-scene", "gapCodes": ["compute-unsupported"],
            "recovery": {"contextFingerprint": "current-target", "suggestedRouteId": "raster-equivalent-adapter",
                "availableRouteIds": ["raster-equivalent-adapter", "cpu-staged-adapter"], "attemptFingerprint": "no-attempts"}}]}


def test_blocked_claim_continues_each_untried_concrete_route_once():
    loop, value = TaskReviewLoop.for_intent(None), state()
    assert loop.decide(value, {}).reason == "blocker-recovery-review"
    first = loop.decide(value, {})
    assert first.action == "continue" and first.reason == "unity-recovery-route"
    assert first.recovery_route["routeId"] == "raster-equivalent-adapter"
    assert "apply_unity_recovery_assets" in first.continuation_prompt()
    assert "record_unity_reconstruction" in first.continuation_prompt()
    assert "core_algorithm" in first.continuation_prompt() and "visible_effect" in first.continuation_prompt()
    second = loop.decide(value, {})
    assert second.recovery_route["routeId"] == "cpu-staged-adapter"
    third = loop.decide(value, {})
    assert third.action == "partial" and third.reason == "blocked-after-recovery-review"


def test_attempt_evidence_resumes_normal_review_but_route_narratives_do_not_repeat_forever():
    loop, value = TaskReviewLoop.for_intent(None), state()
    for _ in range(4):
        decision = loop.decide(value, {})
    assert decision.action == "partial"
    changed = copy.deepcopy(value)
    changed["unityRestorations"][0]["recovery"]["description"] = "same plan in new words"
    assert loop.decide(changed, {}).action == "partial"
    changed["unityRestorations"][0]["recovery"]["attemptFingerprint"] = "different-derived-files-and-real-observation"
    assert loop.decide(changed, {}).action == "continue"


def test_route_availability_does_not_accept_failed_render_or_wrong_identity():
    value = state()
    value["review"]["verdict"] = "complete"
    value["unityRestorations"][0]["gapCodes"] = ["different-capture"]
    loop = TaskReviewLoop.for_intent(None)
    assert loop.decide(value, {}).action == "continue"
    assert loop.decide(value, {}).action == "continue"
    value["unityRestorations"][0].update(complete=True, gapCodes=[])
    assert loop.decide(value, {}).action == "accept"


def test_failure_revision_alone_does_not_reset_continuation_progress():
    loop, value = TaskReviewLoop.for_intent(None), state()
    for revision in range(1, 10):
        value["unityRestorations"][0]["revision"] = revision
        decision = loop.decide(value, {})
        if revision >= 4:
            assert decision.action == "partial"
