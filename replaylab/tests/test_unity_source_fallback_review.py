"""Transferred drafts do not restart source replay work during Unity restoration."""
from replaylab_agent.shader_review import ShaderReviewLoop
from replaylab_agent.task_review import TaskReviewLoop


def target():
    return {"workspacePath": "workspace", "organizationId": "organization", "sourceSha256": "a" * 64,
        "fresh": True, "changed": True, "saved": False,
        "unityTransfer": {"restorationId": "restoration", "referenceOnly": True,
            "sourceSha256": "a" * 64, "originalReplayComplete": False}}


def test_explicit_reverse_edit_keeps_original_obligations_but_port_defers_exact_reference():
    row = target()
    assert ShaderReviewLoop().decide({"targets": [row]}).action == "accept"
    loop = ShaderReviewLoop.for_intent({"kind": "reverse-edit-selected-shader"})
    assert loop.decide({"targets": [row]}).action == "continue"
    row["sourceSha256"] = "b" * 64
    assert ShaderReviewLoop().decide({"targets": [row]}).action == "continue"


def test_source_deferral_cannot_complete_an_unrendered_unity_task():
    state = {"scopeId": "scope", "fresh": True, "workFingerprint": "work", "review": {"verdict": "complete"},
        "unityRestorations": [{"restorationId": "restoration", "complete": False,
            "gapCodes": ["current-render-comparison-required"]}]}
    loop = TaskReviewLoop.for_intent(None)
    decision = loop.decide(state, {"targets": [target()]})
    assert decision.action == "continue"
    assert "Continue this same Shader organization task" not in decision.continuation_prompt()
    state["unityRestorations"][0]["complete"] = True
    assert loop.decide(state, {"targets": [target()]}).action == "accept"


def test_advisory_port_source_conflict_is_reported_without_locking_unity_work():
    row = target()
    row["sourceConflict"] = True
    row["unityTransfer"]["diagnosticsOnly"] = True
    assert ShaderReviewLoop().decide({"targets": [row]}).action == "accept"
    explicit = ShaderReviewLoop.for_intent({"kind": "reverse-edit-selected-shader"})
    assert explicit.decide({"targets": [row]}).action != "accept"
