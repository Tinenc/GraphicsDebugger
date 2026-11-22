"""Persistent debug uses native resource ownership, independent of event selection."""
import hashlib

import pytest

from test_shader_visual_debug_driver import ResourceId, adapter, mod
from test_shader_visual_debug_session import Instrumenter


@pytest.fixture
def persistent(adapter, tmp_path):
    driver, bridge, ctrl, params = adapter
    params.pop("scope")  # Resource scope is the new public default.
    ctrl.global_target = ResourceId(301)
    ctrl.freed = []
    ctrl.after_replace = None
    ctrl.GetTargetShaderReplacement = lambda original: ctrl.global_target
    def replace(original, target):
        assert original == ResourceId(101) and isinstance(target, ResourceId)
        ctrl.calls.append(("replace", original, target))
        if ctrl.error:
            return ctrl.error
        ctrl.global_target = target
        if ctrl.after_replace:
            ctrl.after_replace()
        return ""
    def remove(original):
        ctrl.calls.append(("remove", original))
        if ctrl.error:
            return ctrl.error
        ctrl.global_target = ResourceId()
        return ""
    ctrl.ReplaceTargetShaderChecked = replace
    ctrl.RemoveTargetShaderReplacementChecked = remove
    ctrl.FreeTargetResource = lambda target: ctrl.freed.append(target)
    # The prior edit belongs to another native owner, without bridge registry.
    ctrl.reflection.raw = b"native-prior-edited-shader"
    params["expectedOriginalSha256"] = hashlib.sha256(ctrl.reflection.raw).hexdigest()
    source = "float x = 0.25;\n"
    params.update(ownerId="editor", requestGeneration=1, source=source,
                  sourcePath=str(tmp_path / "shader.hlsl"),
                  sourceSha256=hashlib.sha256(source.encode()).hexdigest(),
                  byteStart=source.index("x"), byteEnd=source.index("x") + 1)
    driver.compiles = 0
    driver.during_compile = None
    def compile_target(generated, facts, request):
        driver.compiles += 1
        if driver.during_compile:
            driver.during_compile()
        return {"target": ResourceId(400 + driver.compiles), "byteSize": 128}
    driver.compile = compile_target
    manager = mod.Manager(driver, Instrumenter())
    return manager, driver, bridge, ctrl, params


def test_default_preview_persists_through_earlier_later_unrelated_and_non_draw_events(persistent):
    manager, driver, unused_bridge, ctrl, params = persistent
    first = manager.operation("start", params)
    assert first["status"] == "active"
    assert first["scope"] == "shader-resource" and first["resourceScoped"] is True
    assert first["persistentAcrossEvents"] is True and first["eventScoped"] is False
    assert first["shaderId"] == "101" and first["anchorEventId"] == 13
    installed = ctrl.global_target
    mutations = list(ctrl.calls)
    ctrl.pipeline.GetShader = lambda stage: ResourceId(999)  # Current Draw is unrelated.
    for generation, event in enumerate((0, 9, 27, 99, 13), 2):
        ctrl.event = event
        result = manager.operation("follow", dict(params, eventId=event, requestGeneration=generation))
        assert result["status"] == "active" and result["sessionId"] == first["sessionId"]
        assert result["anchorEventId"] == 13
        assert ctrl.global_target == installed and ctrl.event == event
    assert driver.compiles == 1 and ctrl.calls == mutations


def test_inspect_member_choice_can_finish_after_user_navigates_to_another_draw(persistent):
    manager, driver, unused_bridge, ctrl, params = persistent
    assert manager.operation("inspect", params)["status"] == "ready"
    ctrl.event = 99
    ctrl.pipeline.GetShaderReflection = lambda stage: pytest.fail("must reuse the inspected anchor")
    ctrl.pipeline.GetShader = lambda stage: pytest.fail("unrelated EID must not become the selected shader")
    result = manager.operation("start", dict(params, requestGeneration=2))
    assert result["status"] == "active" and result["anchorEventId"] == 13
    assert driver.compiles == 1 and ctrl.event == 99


@pytest.mark.parametrize("selected_draw", [False, True])
def test_first_debug_can_start_from_unrelated_draw_or_non_draw_without_moving_selection(
        persistent, selected_draw):
    manager, driver, bridge, ctrl, params = persistent
    current_event = 27 if selected_draw else 99
    if selected_draw:
        ctrl.actions.append(type(ctrl.actions[0])(eventId=current_event, flags=2, children=[]))
    ctrl.event = current_event
    ctrl.pipeline.GetShader = lambda stage: ResourceId(101 if ctrl.event == 13 else 999)
    bridge._resolve_resource_id = lambda controller, value: ResourceId(value)
    ctrl.GetUsage = lambda shader: [type(ctrl.actions[0])(eventId=13)]
    driver.during_compile = lambda: pytest.fail("anchor cursor was not restored before compilation") if ctrl.event != current_event else None
    result = manager.operation("start", dict(params, eventId=current_event))
    assert result["status"] == "active" and result["anchorEventId"] == 13
    assert ctrl.event == current_event and ctrl.global_target == ResourceId(401)
    assert ctrl.calls[:2] == [("event", 13, True), ("event", current_event, True)]
    assert len([call for call in ctrl.calls if call[0] == "replace"]) == 1
    assert driver.compiles == 1


def test_failed_anchor_inspection_restores_original_non_draw_cursor(persistent):
    manager, driver, bridge, ctrl, params = persistent
    ctrl.event = 99
    ctrl.pipeline.GetOutputTargets = lambda: []
    bridge._resolve_resource_id = lambda controller, value: ResourceId(value)
    ctrl.GetUsage = lambda shader: [type(ctrl.actions[0])(eventId=13)]
    result = manager.operation("start", dict(params, eventId=99))
    assert result["reason"] == "no-color-target" and ctrl.event == 99
    assert ctrl.global_target == ResourceId(301) and driver.compiles == 0
    assert ctrl.calls == [("event", 13, True), ("event", 99, True)]


def test_event_change_during_compile_does_not_cancel_or_move_selected_event(persistent):
    manager, driver, unused_bridge, ctrl, params = persistent
    driver.during_compile = lambda: setattr(ctrl, "event", 27)
    assert manager.operation("start", params)["status"] == "active"
    assert ctrl.event == 27 and ctrl.global_target == ResourceId(401)
    assert not any(call[0] == "event" for call in ctrl.calls)


def test_option_updates_cache_and_cancel_restore_borrowed_prior_at_any_eid(persistent):
    manager, driver, unused_bridge, ctrl, params = persistent
    first = manager.operation("start", params)
    ctrl.event = 99
    ctrl.pipeline.GetShaderReflection = lambda stage: pytest.fail("debug reflection cannot become baseline")
    changed = manager.operation("start", dict(params, requestGeneration=2, mode="grayscale"))
    assert changed["status"] == "active" and changed["sessionId"] == first["sessionId"]
    reverted = manager.operation("start", dict(params, requestGeneration=3))
    assert reverted["cacheHit"] is True and driver.compiles == 2
    assert ctrl.global_target == ResourceId(401)
    assert manager.operation("stop", dict(params, requestGeneration=4))["status"] == "stopped"
    assert ctrl.global_target == ResourceId(301) and ctrl.event == 99
    manager.shutdown()
    assert sorted(target.number for target in ctrl.freed) == [401, 402]
    assert ResourceId(301) not in ctrl.freed


@pytest.mark.parametrize("prior", [0, 301])
def test_compare_and_stop_restore_exact_prior_without_selecting_anchor(persistent, prior):
    manager, driver, unused_bridge, ctrl, params = persistent
    ctrl.global_target = ResourceId(prior)
    active = manager.operation("start", params)
    ctrl.event = 99
    request = dict(params, sessionId=active["sessionId"])
    comparing = manager.operation("compare", dict(request, requestGeneration=2, original=True))
    assert comparing["status"] == "comparing" and comparing["resourceScoped"] is True
    assert ctrl.global_target == ResourceId(prior) and ctrl.event == 99
    ctrl.event = 0
    resumed = manager.operation("compare", dict(request, requestGeneration=3, original=False))
    assert resumed["status"] == "active" and ctrl.global_target == ResourceId(401)
    assert manager.operation("stop", dict(request, requestGeneration=4))["status"] == "stopped"
    assert ctrl.global_target == ResourceId(prior) and ctrl.event == 0 and driver.compiles == 1
    assert not any(call[0] == "event" for call in ctrl.calls)


def test_foreign_owner_change_during_compile_is_never_overwritten(persistent):
    manager, driver, unused_bridge, ctrl, params = persistent
    driver.during_compile = lambda: setattr(ctrl, "global_target", ResourceId(999))
    result = manager.operation("start", params)
    assert result["reason"] == "replacement-changed" and not ctrl.calls
    assert ctrl.global_target == ResourceId(999) and manager._session is None


def test_reused_anchor_still_rejects_another_original_shader_identity(persistent):
    manager, driver, unused_bridge, ctrl, params = persistent
    manager.operation("start", params)
    ctrl.event = 99
    calls = list(ctrl.calls)
    result = manager.operation("start", dict(params, requestGeneration=2,
                                             expectedOriginalSha256="0" * 64))
    assert result["reason"] == "shader-changed"
    assert ctrl.calls == calls and driver.compiles == 1


def test_stop_does_not_clobber_new_native_owner(persistent):
    manager, unused_driver, unused_bridge, ctrl, params = persistent
    manager.operation("start", params)
    ctrl.global_target = ResourceId(999)
    calls = list(ctrl.calls)
    result = manager.operation("stop", dict(params, requestGeneration=2))
    assert result["reason"] == "replacement-changed"
    assert ctrl.calls == calls and ctrl.global_target == ResourceId(999)
    assert not ctrl.freed


def test_superseded_native_apply_restores_prior_and_does_not_resurrect_debug(persistent):
    manager, unused_driver, unused_bridge, ctrl, params = persistent
    ctrl.after_replace = lambda: manager.invalidate(dict(params, requestGeneration=2))
    result = manager.operation("start", params)
    assert result["reason"] == "superseded" and manager._session is None
    assert ctrl.global_target == ResourceId(301)


def test_capture_close_never_restores_or_frees_ids_on_a_new_controller(persistent):
    manager, unused_driver, bridge, ctrl, params = persistent
    manager.operation("start", params)
    calls = list(ctrl.calls)
    bridge._capture_session_id = "new-capture"
    ctrl.global_target = ResourceId(999)
    manager.shutdown()
    assert ctrl.calls == calls and not ctrl.freed and ctrl.global_target == ResourceId(999)


@pytest.mark.parametrize("operation", ["inspect", "start"])
def test_independent_float_output_rejected_without_mutation(persistent, operation):
    manager, driver, unused_bridge, ctrl, params = persistent
    result = manager.operation(operation, dict(params, independentFloatOutput=True))
    assert result["reason"] == "independent-preview-unsupported"
    assert not ctrl.calls and driver.compiles == 0 and ctrl.global_target == ResourceId(301)


def test_failed_native_apply_keeps_prior_and_reports_failure(persistent):
    manager, unused_driver, unused_bridge, ctrl, params = persistent
    ctrl.error = "driver pipeline rejected"
    result = manager.operation("start", params)
    assert result["reason"] == "apply-failed" and manager._session is None
    assert ctrl.global_target == ResourceId(301)


def test_lost_remote_restore_reply_retains_session_then_stop_reconciles(persistent):
    manager, unused_driver, bridge, ctrl, params = persistent
    manager.operation("start", params)
    healthy = bridge._require_healthy_replay
    def lose_restore_reply():
        if ctrl.global_target == ResourceId(301):
            bridge._require_healthy_replay = lambda unused: (_ for _ in ()).throw(RuntimeError("remote reply lost"))
    ctrl.after_replace = lose_restore_reply
    failed = manager.operation("stop", dict(params, requestGeneration=2))
    assert failed["activePreview"] and manager._session is not None
    assert ctrl.global_target == ResourceId(301)
    bridge._require_healthy_replay = healthy
    ctrl.after_replace = None
    calls = list(ctrl.calls)
    assert manager.operation("stop", dict(params, requestGeneration=3))["status"] == "stopped"
    assert ctrl.calls == calls and ctrl.global_target == ResourceId(301)
