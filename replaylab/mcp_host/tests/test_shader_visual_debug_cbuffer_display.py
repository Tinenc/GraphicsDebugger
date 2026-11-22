"""Ephemeral CBuffer labels share the debug lifetime, never the project store."""
import hashlib
from types import SimpleNamespace

import pytest

from test_shader_visual_debug_driver import ResourceId, adapter
from test_shader_visual_debug_persistent import persistent


@pytest.fixture
def display(persistent):
    manager, driver, bridge, ctrl, params = persistent
    ctrl.reflection.constantBlocks = ["prior-block"]
    calls, entries = [], {}
    ui = SimpleNamespace(accept=True, error=False)
    def register(token, original, target, source_sha, active_sha, stage, generation, source_blocks, active_blocks):
        assert ctrl.global_target == target  # Publication follows checked apply.
        assert generation == ctrl.GetShaderReplacementGeneration(0)
        calls.append(("register", token, original, target, source_sha, active_sha,
                      stage, generation, source_blocks, active_blocks))
        if ui.error:
            raise RuntimeError("display registry failed")
        if ui.accept:
            entries[token] = target
        return ui.accept
    def unregister(token):
        calls.append(("unregister", token))
        entries.pop(token, None)
    ui.RegisterCBufferDebugDisplaySource = register
    ui.UnregisterCBufferDebugDisplaySource = unregister
    bridge._ctx = ui
    bridge._invoke_on_ui_sync = lambda callback: callback()
    compile_target = driver.compile
    def compile_with_reflection(*args):
        result = compile_target(*args)
        result["reflection"] = SimpleNamespace(raw=("debug-%d" % driver.compiles).encode(),
                                               constantBlocks=["debug-block"])
        return result
    driver.compile = compile_with_reflection
    return manager, driver, bridge, ctrl, params, ui, calls, entries


def test_debug_registers_exact_lifetime_only_source_after_native_apply(display):
    manager, _, _, ctrl, params, _, calls, entries = display
    result = manager.operation("start", params)
    assert result["status"] == "active" and result["cbufferDisplayNamesAvailable"] is True
    row = calls[0]
    assert row[2:4] == (ResourceId(101), ResourceId(401))
    assert row[4] == hashlib.sha256(ctrl.reflection.raw).hexdigest()
    assert row[5] == hashlib.sha256(b"debug-1").hexdigest()
    assert row[8:] == (["prior-block"], ["debug-block"])
    assert entries == {result["sessionId"]: ResourceId(401)}


def test_captured_original_identity_precedes_manual_candidate_alias_source(display):
    manager, _, bridge, _, params, _, calls, _ = display
    original = SimpleNamespace(raw=b"captured-original", constantBlocks=["original-block"])
    bridge._shared_shader_replacements[101] = {
        "captureSessionId": "capture-a", "originalResource": ResourceId(101),
        "expectedOriginalSha256": hashlib.sha256(original.raw).hexdigest(),
        "capturedOriginalReflection": original}
    assert manager.operation("start", params)["status"] == "active"
    assert calls[0][4] == hashlib.sha256(original.raw).hexdigest()
    assert calls[0][8] == ["original-block"]


def test_compare_stop_clear_and_resume_reuses_cached_target_and_labels(display):
    manager, driver, _, ctrl, params, _, calls, entries = display
    active = manager.operation("start", params)
    request = dict(params, sessionId=active["sessionId"])
    ctrl.event = 99
    compared = manager.operation("compare", dict(request, requestGeneration=2, original=True))
    assert compared["status"] == "comparing" and not compared["cbufferDisplayNamesAvailable"]
    assert not entries and calls[-1] == ("unregister", active["sessionId"])
    resumed = manager.operation("compare", dict(request, requestGeneration=3, original=False))
    assert resumed["status"] == "active" and resumed["cbufferDisplayNamesAvailable"]
    assert entries and driver.compiles == 1
    assert manager.operation("stop", dict(request, requestGeneration=4))["status"] == "stopped"
    assert not entries and ctrl.global_target == ResourceId(301) and ctrl.event == 99


@pytest.mark.parametrize("operation", ["follow", "stop"])
def test_foreign_native_owner_clears_only_own_display_registration(display, operation):
    manager, _, _, ctrl, params, _, calls, entries = display
    active = manager.operation("start", params)
    entries["different-preview"] = ResourceId(999)
    ctrl.global_target = ResourceId(999)
    result = manager.operation(operation, dict(params, requestGeneration=2))
    assert result["reason"] == "replacement-changed"
    assert entries == {"different-preview": ResourceId(999)}
    assert calls[-1] == ("unregister", active["sessionId"])
    assert ctrl.global_target == ResourceId(999)


def test_capture_close_clears_transient_names_without_touching_new_native_ids(display):
    manager, _, bridge, ctrl, params, _, _, entries = display
    manager.operation("start", params)
    native_calls = list(ctrl.calls)
    bridge._capture_session_id = "capture-b"
    ctrl.global_target = ResourceId(999)
    manager.shutdown()
    assert not entries and ctrl.calls == native_calls


@pytest.mark.parametrize("error", [False, True])
def test_display_failure_does_not_turn_successful_shader_apply_into_failure(display, error):
    manager, _, _, ctrl, params, ui, _, entries = display
    ui.accept, ui.error = False, error
    active = manager.operation("start", params)
    assert active["status"] == "active" and not active["cbufferDisplayNamesAvailable"]
    assert ctrl.global_target == ResourceId(401) and not entries
    assert manager.operation("stop", dict(params, requestGeneration=2))["status"] == "stopped"
    assert ctrl.global_target == ResourceId(301)


def test_inspected_baseline_is_an_owned_copy_independent_of_backend_lifetime(display):
    manager, _, _, ctrl, params, _, _, _ = display
    assert manager.operation("start", params)["status"] == "active"
    baseline = manager._session["facts"]["baselineReflection"]
    assert baseline is not ctrl.reflection
    original_raw = baseline.rawBytes
    ctrl.reflection.raw = b"backend-released-and-reused"
    ctrl.reflection.constantBlocks[:] = []
    assert baseline.rawBytes == original_raw and baseline.constantBlocks == ["prior-block"]


def test_original_owner_is_copied_before_replay_callback_releases_native_lifetime(display):
    manager, _, bridge, _, params, _, calls, _ = display
    lifetime = SimpleNamespace(alive=True)
    class BorrowedOriginal:
        @property
        def raw(self):
            assert lifetime.alive, "Native original reflection no longer belongs to a live controller"
            return b"captured-original"
        @property
        def constantBlocks(self):
            assert lifetime.alive
            return ["original-owned-block"]
        def __deepcopy__(self, memo):
            return SimpleNamespace(raw=self.raw, constantBlocks=list(self.constantBlocks))
    borrowed = BorrowedOriginal()
    bridge._shared_shader_replacements[101] = {
        "captureSessionId": "capture-a", "originalResource": ResourceId(101),
        "expectedOriginalSha256": hashlib.sha256(borrowed.raw).hexdigest(),
        "capturedOriginalReflection": borrowed}
    replay = bridge._replay
    def expire_after_copy(callback):
        result = replay(callback)
        if isinstance(result, tuple):
            lifetime.alive = False
        return result
    bridge._replay = expire_after_copy
    result = manager.operation("start", params)
    assert result["status"] == "active" and result["cbufferDisplayNamesAvailable"]
    assert not lifetime.alive and calls[0][8] == ["original-owned-block"]
