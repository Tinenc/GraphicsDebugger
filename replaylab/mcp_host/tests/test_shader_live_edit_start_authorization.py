from __future__ import annotations

from contextlib import ExitStack
from unittest import mock

import pytest

from src.shader_live_edit import ShaderLiveEditError
from tests.test_shader_live_edit_lifecycle import _LifecycleRig


@pytest.fixture
def rig(tmp_path):
    value = _LifecycleRig(tmp_path)
    try:
        yield value
    finally:
        value.close()


@pytest.mark.parametrize("authorization_kind", ["new", "reused", "persistent-resume"])
@pytest.mark.parametrize("failure_phase", ["bind", "watch", "job"])
def test_failed_start_revokes_only_its_new_authorization(rig, authorization_kind, failure_phase):
    previous = None
    if authorization_kind != "new":
        started = rig.start()
        previous = rig.authorization()
        # Retire the workers as a Host shutdown does, retaining durable consent.
        rig.manager._jobs[started["liveEditId"]].stop()

    failure = ShaderLiveEditError("offline start failure: " + failure_phase)
    with ExitStack() as stack:
        revoke = stack.enter_context(mock.patch.object(
            rig.manager._registry, "revoke", wraps=rig.manager._registry.revoke,
        ))
        if failure_phase == "bind":
            stack.enter_context(mock.patch.object(rig.manager, "_bind_session", side_effect=failure))
        elif failure_phase == "watch":
            stack.enter_context(mock.patch.object(rig.watch, "start", side_effect=failure))
        else:
            stack.enter_context(mock.patch("src.shader_live_edit._LiveEditJob.start", side_effect=failure))

        with pytest.raises(ShaderLiveEditError, match="offline start failure"):
            if authorization_kind == "persistent-resume":
                rig.manager._start(rig.workspace, 100, persistent_entry=previous)
            else:
                rig.manager.start(rig.workspace, 100)

        entries = rig.manager.list()["authorizations"]
        if authorization_kind == "new":
            assert entries == []
            revoke.assert_called_once()
            assert revoke.call_args.args[0] == rig.workspace
            assert len(revoke.call_args.args[1]) == 32
        else:
            revoke.assert_not_called()
            assert len(entries) == 1
            assert entries[0]["authorizationId"] == previous["authorizationId"]
            assert entries[0]["workspaceIdentity"] == previous["workspaceIdentity"]

    assert rig.manager._active_by_workspace == {}
    assert rig.manager._starting_workspaces == set()
    assert not any(session["state"] == "running" for session in rig.manager.list()["sessions"])
    assert rig.apply_calls == []
    if failure_phase == "job":
        assert rig.watch.stop_calls[-1] == "%032x" % len(rig.watch.start_calls)

    # Removing the transient fault can reuse the same durable grant successfully.
    resumed = rig.start()
    if previous is not None:
        assert rig.authorization()["authorizationId"] == previous["authorizationId"]
    assert rig.authorization()["lastLiveEditId"] == resumed["liveEditId"]
    assert rig.manager.poll(resumed["liveEditId"])["captureBinding"] == rig.binding


def test_explicit_start_rejects_changed_workspace_identity_without_revoking_consent(rig):
    started = rig.start()
    previous = rig.authorization()
    rig.manager._jobs[started["liveEditId"]].stop()
    rig.identity["resourceId"] = 78
    rig.identity["shaderKey"] = "78:fragment:MainPS:" + "c" * 64

    with pytest.raises(ShaderLiveEditError, match="workspace identity changed"):
        rig.manager.start(rig.workspace, 100)

    assert rig.authorization() == previous
    assert len(rig.watch.start_calls) == 1
    assert rig.apply_calls == []


def test_failed_new_start_cannot_revoke_a_later_distinct_authorization(rig):
    replacement = None

    def supersede_and_fail(*_args):
        nonlocal replacement
        own = rig.authorization()
        assert rig.manager._registry.revoke(rig.workspace, own["authorizationId"])
        replacement = rig.manager._registry.authorize(rig.workspace, rig.identity, 100)
        assert replacement["authorizationId"] != own["authorizationId"]
        raise ShaderLiveEditError("offline authorization changed during start")

    with mock.patch.object(rig.watch, "start", side_effect=supersede_and_fail):
        with pytest.raises(ShaderLiveEditError, match="authorization changed during start"):
            rig.manager.start(rig.workspace, 100)

    assert rig.authorization() == replacement
    assert rig.manager._active_by_workspace == {}
    assert rig.apply_calls == []
