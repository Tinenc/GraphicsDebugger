from __future__ import annotations

import copy
from unittest import mock

import pytest

from src.shader_live_edit import ShaderLiveEditError
from tests.test_shader_live_edit import _wait_until
from tests.test_shader_live_edit_lifecycle import _LifecycleRig, _scope


@pytest.fixture
def rig(tmp_path):
    value = _LifecycleRig(tmp_path)
    # Model an old owner that has not reached its next mailbox poll yet.
    # A user can save immediately after a capture is reopened, before the
    # background refresh/list operation has retired the old live job.
    value.manager.poll_seconds = 10.0
    try:
        yield value
    finally:
        value.close()


def request_compile(rig):
    source_sha = "e" * 64

    def schedule(watch_id, expected_source_sha256):
        assert expected_source_sha256 == source_sha
        rig.watch.publish(watch_id, 6, "6" * 64, source_sha)
        return {"watchId": watch_id, "compileScheduled": True,
                "sourceSha256": expected_source_sha256}

    rig.watch.request_saved_source_compile = mock.Mock(side_effect=schedule)
    rig.manager.poll_seconds = 0.005
    return lambda: rig.manager.compile_saved_source(
        str(rig.workspace), rig.identity["workspaceId"], source_sha,
        capture_binding=copy.deepcopy(rig.binding))


@pytest.mark.parametrize("instance", ["old-gui", "new-gui"])
def test_first_explicit_save_after_reopen_retires_old_owner_without_background_refresh(rig, instance):
    old = rig.start()
    authorization = rig.authorization()
    old_binding = copy.deepcopy(rig.binding)
    rig.use_gui(rig.gui_binding(instance, "2" * 32))
    compile_saved = request_compile(rig)

    queued = compile_saved()

    assert queued["liveEditId"] != old["liveEditId"]
    assert queued["watchId"] != old["watchId"]
    assert rig.authorization()["authorizationId"] == authorization["authorizationId"]
    assert rig.authorization()["lastLiveEditId"] == queued["liveEditId"]
    assert rig.manager.poll(queued["liveEditId"])["captureBinding"] == rig.binding
    assert rig.manager.poll(old["liveEditId"])["captureBinding"] == old_binding
    assert old["watchId"] in rig.watch.stop_calls
    assert not rig.manager._jobs[old["liveEditId"]]._thread.is_alive()
    _wait_until(lambda: rig.manager.poll(queued["liveEditId"])["applySuccesses"] == 1)
    assert [(call[0], call[2:]) for call in rig.apply_calls] == [
        (_scope(rig.binding), (6, "6" * 64))]
    rig.watch.request_saved_source_compile.assert_called_once_with(queued["watchId"], "e" * 64)


@pytest.mark.parametrize("state", ["current", "unavailable"])
def test_explicit_save_cannot_take_over_current_or_unknown_foreign_owner(rig, state):
    old = rig.start()
    authorization = rig.authorization()
    rig.use_gui(rig.gui_binding("other-gui", "2" * 32), previous_state=state)
    compile_saved = request_compile(rig)

    with pytest.raises(ShaderLiveEditError, match="another GUI"):
        compile_saved()

    assert rig.watch.stop_calls == []
    assert len(rig.watch.start_calls) == 1
    assert rig.authorization() == authorization
    rig.watch.request_saved_source_compile.assert_not_called()
    assert rig.apply_calls == []
    assert rig.manager.poll(old["liveEditId"])["state"] == "running"


def test_expired_owner_must_finish_inflight_apply_before_explicit_save_starts_new_job(rig):
    # This old worker must run its apply queue before the capture switch.
    rig.manager.poll_seconds = 0.005
    old = rig.start()
    rig.block_apply = True
    rig.watch.publish(old["watchId"], 6, "6" * 64)
    assert rig.apply_entered.wait(2.0)
    rig.use_gui(rig.gui_binding("old-gui", "2" * 32))
    compile_saved = request_compile(rig)

    with pytest.raises(ShaderLiveEditError, match="worker.*(stop|finished)"):
        compile_saved()

    assert len(rig.watch.start_calls) == 1
    rig.watch.request_saved_source_compile.assert_not_called()
    rig.release_apply.set()
    old_job = rig.manager._jobs[old["liveEditId"]]
    _wait_until(lambda: not old_job._thread.is_alive() and not old_job._apply_thread.is_alive())
    rig.block_apply = False
    queued = compile_saved()
    assert queued["liveEditId"] != old["liveEditId"]
    _wait_until(lambda: rig.manager.poll(queued["liveEditId"])["applySuccesses"] == 1)
    assert rig.apply_calls[-1][0] == _scope(rig.binding)
