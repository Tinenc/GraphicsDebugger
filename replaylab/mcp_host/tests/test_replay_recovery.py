"""Fatal replay recovery keeps evidence/drafts without reapplying failed edits."""

from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import pytest


SOURCE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/replay_recovery.py"
spec = spec_from_file_location("replay_recovery", SOURCE)
recovery = module_from_spec(spec)
spec.loader.exec_module(recovery)


class Hooks:
    def __init__(self, tmp_path):
        self.path = tmp_path / "capture.rdc"
        self.path.write_bytes(b"unchanged captured frame")
        self.current = dict(loaded=True, local=True, temporary=False, capturePath=str(self.path),
                            captureSessionId="old-session", eventId=42, selectedEventId=41)
        self.dirty = {"source": "unsaved shader edit", "dirty": True}
        self.saved = {"diagram": {"channelImages": ["immutable-r.png", "immutable-a.png"]},
                      "project": str(tmp_path / "project"), "draft": self.dirty}
        self.live = {"replacement": "bad-replacement", "cbuffer": "edited", "currentCache": "old-session"}
        self.healthy = False
        self.calls = []
        self.guard = False
        self.explicit_cancelled = False
        self.health_hook = None
        self.begin_hook = None
        self.reopen_hook = None
        self.finish_hook = None
        self.restore_hook = None

    def snapshot(self):
        return dict(self.current)

    def health(self):
        self.calls.append("health")
        if self.health_hook:
            return self.health_hook()
        return {"captureSessionId": self.current["captureSessionId"], "healthy": self.healthy,
                "code": "Succeeded" if self.healthy else "GPUTimeout", "diagnostic": "native status"}

    def cancelled(self):
        return self.explicit_cancelled

    def preserve(self, snapshot):
        self.calls.append("preserve")
        return {"preserved": True, "state": self.saved}

    def begin(self, snapshot, preserved):
        self.calls.append("begin")
        self.guard = True
        if self.begin_hook:
            self.begin_hook()
        return "guard-token"

    def invalidate(self, snapshot, preserved):
        self.calls.append("invalidate")
        self.live.clear()

    def reopen(self, snapshot):
        self.calls.append("reopen")
        self.current.update(captureSessionId="new-session", eventId=100, selectedEventId=100)
        self.healthy = True
        if self.reopen_hook:
            self.reopen_hook()

    def restore_event(self, snapshot):
        self.calls.append("restore-event")
        self.current.update(eventId=snapshot["eventId"], selectedEventId=snapshot["selectedEventId"])
        if self.restore_hook:
            self.restore_hook()

    def finish(self, snapshot, preserved, result, token):
        self.calls.append("finish")
        try:
            assert preserved["state"] is self.saved
            if self.finish_hook:
                self.finish_hook()
        finally:
            self.guard = False


def test_fatal_controller_is_rebuilt_once_as_original_capture_and_restores_selection(tmp_path):
    hooks = Hooks(tmp_path)
    saved = deepcopy(hooks.saved)
    coordinator = recovery.ReplayRecoveryCoordinator()
    result = coordinator.recover(hooks, expected_session_id="old-session")
    assert result["recovered"] and result["attempted"]
    assert result["replayContent"] == "capture-original"
    assert result["previousCaptureSessionId"] == "old-session"
    assert result["captureSessionId"] == "new-session"
    assert (result["selectedEventId"], result["eventId"]) == (41, 42)
    assert result["preserved"] and hooks.saved == saved
    assert hooks.dirty["dirty"] and hooks.dirty["source"] == "unsaved shader edit"
    assert hooks.live == {}
    assert not result["replacementsRestored"] and not result["liveEditsRestored"]
    assert not result["currentEvidenceVerified"]
    assert hooks.calls == ["health", "preserve", "begin", "invalidate", "reopen",
                           "health", "restore-event", "health", "finish"]
    assert not hooks.guard and not coordinator.active


def test_healthy_or_empty_but_nonfatal_replay_is_never_reopened(tmp_path):
    hooks = Hooks(tmp_path)
    hooks.healthy = True
    hooks.current.update(shaders={}, inputs=[], outputs=[])
    result = recovery.ReplayRecoveryCoordinator().recover(hooks)
    assert result["status"] == "not-needed" and not result["attempted"]
    assert hooks.calls == ["health"] and hooks.live["replacement"] == "bad-replacement"


@pytest.mark.parametrize("health", [None, {}, {"healthy": False},
    {"healthy": False, "captureSessionId": "different", "code": "GPUTimeout"},
    {"healthy": "false", "captureSessionId": "old-session", "code": "GPUTimeout"}])
def test_unavailable_or_stale_health_is_not_fatal_authorization(tmp_path, health):
    hooks = Hooks(tmp_path)
    hooks.health_hook = lambda: health
    result = recovery.ReplayRecoveryCoordinator().recover(hooks)
    assert not result["attempted"] and result["failedPhase"] == "preflight"
    assert hooks.calls == ["health"]


@pytest.mark.parametrize("field,value", [("loaded", False), ("local", False), ("temporary", True)])
def test_unavailable_remote_or_temporary_capture_cannot_be_automatically_reopened(tmp_path, field, value):
    hooks = Hooks(tmp_path)
    hooks.current[field] = value
    result = recovery.ReplayRecoveryCoordinator().recover(hooks)
    assert not result["attempted"] and not hooks.calls


def test_stale_requested_session_is_rejected_before_any_replay_or_preservation(tmp_path):
    hooks = Hooks(tmp_path)
    result = recovery.ReplayRecoveryCoordinator().recover(hooks, expected_session_id="different")
    assert result["errorId"] == "replay-recovery-capture-changed"
    assert not hooks.calls


@pytest.mark.parametrize("event", [None, -1, True, "42"])
def test_unavailable_original_selection_cannot_be_silently_replaced_by_default_event(tmp_path, event):
    hooks = Hooks(tmp_path)
    hooks.current["eventId"] = event
    result = recovery.ReplayRecoveryCoordinator().recover(hooks)
    assert not result["attempted"] and not hooks.calls


def test_draft_preservation_must_succeed_before_close_or_invalidation(tmp_path):
    hooks = Hooks(tmp_path)
    hooks.preserve = lambda snapshot: {"preserved": False}
    result = recovery.ReplayRecoveryCoordinator().recover(hooks)
    assert result["failedPhase"] == "preserve" and not result["attempted"]
    assert not hooks.guard and hooks.live["replacement"] == "bad-replacement"


@pytest.mark.parametrize("mutation", ["file", "capture", "session", "cancel"])
def test_change_while_preserving_never_reopens_or_invalidates_user_state(tmp_path, mutation):
    hooks = Hooks(tmp_path)
    original = hooks.preserve

    def preserve(snapshot):
        if mutation == "file":
            hooks.path.write_bytes(b"replaced capture with other content")
        elif mutation == "capture":
            hooks.current["capturePath"] = str(tmp_path / "different.rdc")
        elif mutation == "session":
            hooks.current["captureSessionId"] = "user-session"
        else:
            hooks.explicit_cancelled = True
        return original(snapshot)

    hooks.preserve = preserve
    result = recovery.ReplayRecoveryCoordinator().recover(hooks)
    assert not result["attempted"] and not result["recovered"]
    assert hooks.calls == ["health", "preserve"]
    assert hooks.live["replacement"] == "bad-replacement"


def test_real_user_capture_switch_during_begin_does_not_invalidate_the_new_capture(tmp_path):
    hooks = Hooks(tmp_path)
    hooks.begin_hook = lambda: hooks.current.update(captureSessionId="user-session")
    result = recovery.ReplayRecoveryCoordinator().recover(hooks)
    assert not result["attempted"] and result["errorId"] == "replay-recovery-capture-changed"
    assert hooks.calls[-1] == "finish" and "invalidate" not in hooks.calls
    assert not hooks.guard


def test_recursive_recovery_is_rejected_without_disturbing_outer_transaction(tmp_path):
    hooks = Hooks(tmp_path)
    coordinator = recovery.ReplayRecoveryCoordinator()
    nested = []
    hooks.begin_hook = lambda: nested.append(coordinator.recover(hooks))
    assert coordinator.recover(hooks)["recovered"]
    assert nested[0]["errorId"] == "replay-recovery-busy"
    assert hooks.calls.count("reopen") == 1 and not coordinator.active


@pytest.mark.parametrize("where", ["begin", "reopen", "restore", "finish"])
def test_exception_always_releases_lifecycle_guard_without_dropping_drafts(tmp_path, where):
    hooks = Hooks(tmp_path)
    saved = deepcopy(hooks.saved)
    coordinator = recovery.ReplayRecoveryCoordinator()

    def broken():
        raise RuntimeError("simulated " + where + " failure")

    setattr(hooks, where + "_hook", broken)
    result = coordinator.recover(hooks)
    assert not result["recovered"] and result["status"] == "failed"
    assert hooks.calls.count("finish") == 1 and not hooks.guard and not coordinator.active
    assert hooks.saved == saved


@pytest.mark.parametrize("where", ["reopen", "restore"])
def test_new_controller_must_be_healthy_before_and_after_event_restore(tmp_path, where):
    hooks = Hooks(tmp_path)
    setattr(hooks, where + "_hook", lambda: setattr(hooks, "healthy", False))
    result = recovery.ReplayRecoveryCoordinator().recover(hooks)
    assert not result["recovered"] and result["attempted"]
    assert not hooks.guard and result["preserved"]
    if where == "reopen":
        assert "restore-event" not in hooks.calls


@pytest.mark.parametrize("mutation", ["file", "path", "same-session", "wrong-event"])
def test_wrong_reopened_identity_or_selection_is_not_reported_as_recovered(tmp_path, mutation):
    hooks = Hooks(tmp_path)
    if mutation == "file":
        # Change size as well: Windows can coalesce same-tick mtime updates.
        hooks.reopen_hook = lambda: hooks.path.write_bytes(b"different captured bytes and size")
    elif mutation == "path":
        hooks.reopen_hook = lambda: hooks.current.update(capturePath=str(tmp_path / "other.rdc"))
    elif mutation == "same-session":
        hooks.reopen_hook = lambda: hooks.current.update(captureSessionId="old-session")
    else:
        hooks.restore_hook = lambda: hooks.current.update(eventId=200)
    result = recovery.ReplayRecoveryCoordinator().recover(hooks)
    assert not result["recovered"] and result["attempted"] and not hooks.guard


def test_second_fatal_in_rebuilt_session_cannot_loop_by_acquiring_a_new_session_id(tmp_path):
    hooks = Hooks(tmp_path)
    coordinator = recovery.ReplayRecoveryCoordinator()
    assert coordinator.recover(hooks)["recovered"]
    hooks.healthy = False
    hooks.calls.clear()
    result = coordinator.recover(hooks)
    assert result["errorId"] == "replay-recovery-retry-limit"
    assert not result["attempted"] and hooks.calls == ["health"]


def test_reopen_failure_cannot_repeat_against_the_same_failed_controller(tmp_path):
    hooks = Hooks(tmp_path)
    coordinator = recovery.ReplayRecoveryCoordinator()
    hooks.reopen = lambda snapshot: (_ for _ in ()).throw(RuntimeError("native reopen failed"))
    assert not coordinator.recover(hooks)["recovered"]
    hooks.calls.clear()
    assert coordinator.recover(hooks)["errorId"] == "replay-recovery-retry-limit"
    assert hooks.calls == ["health"]
