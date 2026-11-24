"""UI selection is a prompt hint; the model may follow a newer user target."""

import pytest

from replaylab_agent.agent_prompts import main_agent_user_prompt, summarize_context
from replaylab_agent.native_engine import _tool_state_revision
from replaylab_agent.tool_scope import ToolCallScope, selected_shader_target_for_intent


CONTEXT = {
    "capture": {"captureSessionId": "a" * 32, "captureId": "b" * 64},
    "events": {"currentEventId": 11402},
    "shaders": {"focused": {
        "resourceId": "1000000000000000723", "stage": "fragment",
        "entryPoint": "MainPS", "rawSha256": "c" * 64,
    }},
}


@pytest.mark.parametrize("intent", [
    None, {}, {"kind": "explain-current-event"}, {"kind": "analyze-shader"},
])
def test_only_workbench_action_adds_selected_shader_hint(intent):
    assert selected_shader_target_for_intent(intent) == {}


def test_workbench_target_is_context_and_does_not_set_a_completion_contract():
    target = selected_shader_target_for_intent(
        {"kind": "reverse-edit-selected-shader", "arguments": {"eventId": 11402}},
        CONTEXT,
    )
    assert target == {
        "eventId": 11402, "stage": "PS", "resourceId": "1000000000000000723",
        "entryPoint": "MainPS", "rawSha256": "c" * 64,
        "captureSessionId": "a" * 32, "captureId": "b" * 64,
    }
    prompt = main_agent_user_prompt(
        request="请处理 EID 12000 的 PS", history=(), selected_shader_target=target,
    )
    assert '"eventId": 11402' in prompt
    assert "latest user request if it names another target" in prompt
    assert "report_task_result" not in prompt


def test_reverse_edit_action_does_not_freeze_effects_to_the_ui_eid():
    scope = ToolCallScope.from_request(
        CONTEXT,
        {"kind": "reverse-edit-selected-shader", "arguments": {"eventId": 11402}},
    )
    scope.validate("begin_shader_workspace_organization", {"event_id": 12000})


def test_other_ui_actions_are_context_when_user_redirects_effects():
    scope = ToolCallScope.from_request(
        CONTEXT, {"kind": "explain-current-event", "arguments": {"eventId": 11402}}
    )
    assert scope.event_id == 11402
    scope.validate("write_shader_workspace_organization_file", {"event_id": 12000})


def test_pixel_button_hints_do_not_lock_later_resource_or_pixel_choice():
    scope = ToolCallScope.from_request(
        {
            **CONTEXT,
            "texture": {
                "resourceId": "77", "pickedPixel": {"x": 10, "y": 20},
                "subresource": {"mip": 0, "slice": 0, "sample": 0},
            },
        },
        {"kind": "inspect-picked-pixel", "arguments": {}},
    )
    assert (scope.event_id, scope.resource_id, scope.x, scope.y) == (11402, 77, 10, 20)
    scope.validate("capture_event_texture_evidence", {
        "event_id": 12000, "resource_id": 88, "x": 30, "y": 40,
    })


def test_incomplete_ui_hints_are_observable_without_rejecting_chat():
    scope = ToolCallScope.from_request(
        {}, {"kind": "inspect-picked-pixel", "arguments": {}}
    )
    assert scope.event_id is None
    assert ToolCallScope.from_json(scope.to_json()) == scope
    scope.validate("inspect_event", {"event_id": 12000})


def test_transport_recovery_detects_new_tool_receipts_without_a_report_gate():
    first = {"updatedUnixNs": 1, "toolFacts": [{"tool": "inspect_event", "outcome": "returned"}]}
    second = {"updatedUnixNs": 2, "toolFacts": first["toolFacts"] + [
        {"tool": "write_shader_workspace_organization_file", "outcome": "returned"}
    ]}
    assert _tool_state_revision(first) != _tool_state_revision(second)


def test_capture_routing_summary_distinguishes_reopen_and_replaced_same_path():
    base = {"capture": {"loaded": True, "path": "D:/captures/current.rdc",
                        "api": "D3D12", "captureSessionId": "a" * 32,
                        "captureId": "b" * 64}}
    first = summarize_context(base)
    reopened = summarize_context({"capture": {**base["capture"],
                                               "captureSessionId": "c" * 32}})
    replaced = summarize_context({"capture": {**base["capture"],
                                               "captureSessionId": "d" * 32,
                                               "captureId": "e" * 64}})
    assert first != reopened != replaced
    assert '"captureSessionId": "' + "a" * 32 + '"' in first
    assert '"captureId": "' + "b" * 64 + '"' in first
    assert "a" * 32 not in reopened and "b" * 64 in reopened
    assert "b" * 64 not in replaced and "e" * 64 in replaced


def test_capture_routing_summary_does_not_invent_catalog_before_export():
    text = summarize_context({"capture": {"loaded": True, "path": "D:/capture.rdc",
                                          "api": "Vulkan", "captureSessionId": "a" * 32,
                                          "captureId": ""}})
    assert "captureSessionId" in text and "captureId" not in text
    unloaded = summarize_context({"capture": {"loaded": False,
                                              "captureSessionId": "a" * 32}})
    assert "capture: none loaded" in unloaded and "a" * 32 not in unloaded
