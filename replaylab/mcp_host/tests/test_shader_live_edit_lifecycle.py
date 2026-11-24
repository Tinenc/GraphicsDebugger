from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest

from src.shader_live_edit import (
    ShaderLiveEditError,
    ShaderLiveEditManager,
    ShaderLiveEditSession,
)
from tests.test_shader_live_edit import (
    _FakeWatchManager,
    _compatible_result,
    _wait_until,
)


def _scope(binding: dict[str, str]) -> tuple[str, str, str]:
    return tuple(binding[key] for key in ("instanceId", "captureSessionId", "capturePath"))


class _PersistentWatchManager(_FakeWatchManager):
    def __init__(self):
        super().__init__()
        self.persistent_start_calls = []

    def start_persistent(self, workspace_path, debounce_ms):
        self.persistent_start_calls.append((str(workspace_path), debounce_ms))
        return self.start(workspace_path, debounce_ms)


class _LifecycleRig:
    """Real lifecycle/registry code with an independently controlled GUI mailbox."""

    def __init__(self, tmp_path: Path):
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.capture = tmp_path / "capture.rdc"
        self.capture.write_bytes(b"offline lifecycle identity only")
        self.identity = {
            "workspaceId": "a" * 32,
            "captureId": "b" * 64,
            "capturePath": str(self.capture),
            "shaderKey": "77:fragment:MainPS:" + "c" * 64,
            "shaderRawSha256": "c" * 64,
            "resourceId": 77,
            "stage": "fragment",
            "entryPoint": "MainPS",
        }
        self.binding = self.gui_binding("old-gui", "1" * 32)
        self.states = {_scope(self.binding): "current"}
        self.bound_scopes = []
        self.apply_calls = []
        self.apply_entered = threading.Event()
        self.release_apply = threading.Event()
        self.block_apply = False
        self.bind_failure = False
        self.watch = _PersistentWatchManager()
        self.manager = ShaderLiveEditManager(
            self.watch,
            self._unbound_apply,
            poll_seconds=0.005,
            join_seconds=0.1,
            session_unavailable_retry_seconds=0.03,
            registry_path=tmp_path / "config" / "live-edit.json",
            workspace_identity=lambda _path: copy.deepcopy(self.identity),
            bind_session=self.bind_session,
        )

    def gui_binding(self, instance: str, session: str) -> dict[str, str]:
        return {
            "instanceId": instance,
            "captureSessionId": session,
            "capturePath": str(self.capture),
        }

    @staticmethod
    def _unbound_apply(*_args):
        raise AssertionError("a scoped live job used the mutable global apply callback")

    def bind_session(self, _workspace):
        if self.bind_failure:
            raise ShaderLiveEditError("test GUI mailbox unavailable")
        binding = copy.deepcopy(self.binding)
        scope = _scope(binding)
        self.bound_scopes.append(scope)

        def apply_candidate(workspace, revision, raw_sha256):
            self.apply_calls.append((scope, workspace, revision, raw_sha256))
            self.apply_entered.set()
            if self.block_apply and not self.release_apply.wait(3.0):
                raise AssertionError("test did not release the in-flight replay mutation")
            return _compatible_result(self.workspace, revision, raw_sha256)

        return ShaderLiveEditSession(
            binding=binding,
            state=lambda: self.states.get(scope, "unavailable"),
            apply_candidate=apply_candidate,
        )

    def start(self):
        started = self.manager.start(self.workspace, 100)
        _wait_until(lambda: self.manager.poll(started["liveEditId"])["state"] == "running")
        return started

    def authorization(self):
        entries = self.manager.list()["authorizations"]
        assert len(entries) == 1
        return entries[0]

    def use_gui(self, binding, *, previous_state="expired"):
        self.states[_scope(self.binding)] = previous_state
        self.binding = copy.deepcopy(binding)
        self.states[_scope(binding)] = "current"

    def close(self):
        self.release_apply.set()
        self.manager.shutdown()
        # Even a coordinator already marked failed must finish before tmp_path
        # disappears; its late audit write is part of the tested lifecycle.
        for job in self.manager._jobs.values():
            job._thread.join(2.0)
            job._apply_thread.join(2.0)
            assert not job._thread.is_alive()
            assert not job._apply_thread.is_alive()


@pytest.fixture
def rig(tmp_path):
    value = _LifecycleRig(tmp_path)
    try:
        yield value
    finally:
        value.close()


@pytest.mark.parametrize("next_instance", ["old-gui", "new-gui"])
def test_same_internal_host_reopens_capture_with_new_live_id_and_preserved_consent(rig, next_instance):
    old = rig.start()
    old_binding = copy.deepcopy(rig.binding)
    authorization = rig.authorization()
    new_binding = rig.gui_binding(next_instance, "2" * 32)
    rig.use_gui(new_binding)

    result = rig.manager.reconcile_capture(new_binding)
    assert len(result["results"]) == 1
    assert result["results"][0]["resumed"] is True
    new_id = result["results"][0]["liveEditId"]
    assert new_id != old["liveEditId"]
    assert len(rig.watch.start_calls) == 2
    assert len(rig.watch.persistent_start_calls) == 1
    persisted = rig.authorization()
    assert persisted["authorizationId"] == authorization["authorizationId"]
    assert persisted["lastLiveEditId"] == new_id
    assert rig.manager.poll(new_id)["captureBinding"] == new_binding
    assert rig.manager.poll(old["liveEditId"])["captureBinding"] == old_binding
    old_job = rig.manager._jobs[old["liveEditId"]]
    assert not old_job._thread.is_alive()
    assert not old_job._apply_thread.is_alive()
    assert old["watchId"] in rig.watch.stop_calls

    with pytest.raises(ShaderLiveEditError):
        rig.manager.save_authorized_source(
            old["liveEditId"], authorization["authorizationId"], "a" * 32,
            "d" * 64, "old editor must not save",
        )
    assert rig.watch.save_calls == []
    with pytest.raises(ShaderLiveEditError):
        rig.manager.require_capture_binding(new_id, old_binding)
    with pytest.raises(ShaderLiveEditError):
        rig.manager.require_capture_binding(old["liveEditId"], new_binding)
    rig.manager.require_capture_binding(new_id, new_binding)

    # An old tab's late stop carries the same durable authorization, but its
    # previous live ID must never revoke the authorization now owned by new_id.
    with pytest.raises(ShaderLiveEditError, match="revocation was not confirmed"):
        rig.manager.stop(old["liveEditId"])
    assert rig.authorization()["lastLiveEditId"] == new_id
    assert rig.manager.poll(new_id)["state"] == "running"
    saved = rig.manager.save_authorized_source(
        new_id, authorization["authorizationId"], "a" * 32,
        "d" * 64, "float4 MainPS() : SV_Target { return 1; }",
    )
    assert saved["liveEditId"] == new_id
    watch_id = rig.manager.poll(new_id)["watchId"]
    rig.watch.publish(watch_id, 6, "6" * 64)
    _wait_until(lambda: rig.manager.poll(new_id)["applySuccesses"] == 1)
    assert [call[0] for call in rig.apply_calls] == [_scope(new_binding)]
    assert rig.manager.reconcile_capture(new_binding)["results"] == []
    assert len(rig.watch.start_calls) == 2
    rig.manager.stop(new_id)
    assert rig.manager.list()["authorizations"] == []


@pytest.mark.parametrize("owner_state", ["current", "unavailable"])
def test_current_foreign_or_unknown_gui_owner_is_not_automatically_taken_over(rig, owner_state):
    old = rig.start()
    old_binding = copy.deepcopy(rig.binding)
    authorization = rig.authorization()
    new_binding = rig.gui_binding("other-gui", "2" * 32)
    rig.use_gui(new_binding, previous_state=owner_state)

    for _ in range(3):
        assert rig.manager.reconcile_capture(new_binding)["results"] == []
    assert len(rig.watch.start_calls) == 1
    assert rig.watch.persistent_start_calls == []
    assert rig.authorization()["authorizationId"] == authorization["authorizationId"]
    assert rig.authorization()["lastLiveEditId"] == old["liveEditId"]
    with pytest.raises(ShaderLiveEditError, match="another GUI"):
        rig.manager.require_capture_binding(old["liveEditId"], new_binding)
    if owner_state == "current":
        assert rig.manager.poll(old["liveEditId"])["state"] == "running"
        assert rig.watch.stop_calls == []
        rig.watch.publish(old["watchId"], 6, "6" * 64)
        _wait_until(lambda: rig.manager.poll(old["liveEditId"])["applySuccesses"] == 1)
        assert rig.apply_calls[0][0] == _scope(old_binding)


def test_concurrent_capture_reconciliations_resume_exactly_one_persistent_watcher(rig):
    old = rig.start()
    binding = rig.gui_binding("new-gui", "2" * 32)
    rig.use_gui(binding)
    barrier = threading.Barrier(5)

    def reconcile():
        barrier.wait(timeout=2.0)
        return rig.manager.reconcile_capture(copy.deepcopy(binding))

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(reconcile) for _ in range(4)]
        barrier.wait(timeout=2.0)
        results = [future.result(timeout=3.0) for future in futures]
    resumed = [item for result in results for item in result["results"]]
    assert len(resumed) == 1
    assert resumed[0]["resumed"] is True
    assert resumed[0]["liveEditId"] != old["liveEditId"]
    assert rig.authorization()["lastLiveEditId"] == resumed[0]["liveEditId"]
    assert len(rig.watch.start_calls) == 2
    assert len(rig.watch.persistent_start_calls) == 1
    assert len([item for item in rig.manager.list()["sessions"]
                if item["state"] not in {"stopped", "failed"}]) == 1


def test_expired_session_rejects_save_and_replacement_reuse_before_reconcile(rig):
    started = rig.start()
    authorization = rig.authorization()
    old_binding = copy.deepcopy(rig.binding)
    rig.watch.publish(started["watchId"], 6, "6" * 64)
    _wait_until(lambda: rig.manager.poll(started["liveEditId"])["applySuccesses"] == 1)
    assert rig.manager.poll(started["liveEditId"])["activeReplacement"] is not None
    rig.use_gui(rig.gui_binding("new-gui", "2" * 32))

    with pytest.raises(ShaderLiveEditError):
        rig.manager.save_authorized_source(
            started["liveEditId"], authorization["authorizationId"], "a" * 32,
            "d" * 64, "expired editor",
        )
    with pytest.raises(ShaderLiveEditError, match="no longer current"):
        rig.manager.require_capture_binding(started["liveEditId"], old_binding)
    # The next compile is byte-identical to the former replacement. The session
    # check still applies before the no-RPC exact-candidate reuse optimization.
    rig.watch.publish(started["watchId"], 7, "6" * 64)
    _wait_until(lambda: rig.manager.poll(started["liveEditId"])["state"] in {"stopped", "failed"})
    assert rig.watch.save_calls == []
    assert len(rig.apply_calls) == 1
    assert rig.manager.poll(started["liveEditId"])["applyReuses"] == 0
    assert rig.manager.poll(started["liveEditId"])["activeReplacement"] is None
    assert rig.authorization()["authorizationId"] == authorization["authorizationId"]


@pytest.mark.parametrize("wait_for_failed_coordinator", [False, True])
def test_resume_waits_for_old_in_flight_apply_to_finish_and_keeps_frozen_binding(rig, wait_for_failed_coordinator):
    rig.block_apply = True
    started = rig.start()
    old_binding = copy.deepcopy(rig.binding)
    authorization = rig.authorization()
    rig.watch.publish(started["watchId"], 6, "6" * 64)
    assert rig.apply_entered.wait(2.0)
    new_binding = rig.gui_binding("new-gui", "2" * 32)
    rig.use_gui(new_binding)
    if wait_for_failed_coordinator:
        _wait_until(lambda: rig.manager.poll(started["liveEditId"])["state"] == "failed")

    # The old coordinator can already have failed its mailbox check while its
    # apply worker remains alive. Both terminal and stopping paths must join it.
    result = rig.manager.reconcile_capture(new_binding)
    assert len(result["results"]) == 1
    assert result["results"][0]["resumed"] is False
    assert result["results"][0]["errorType"] == "ShaderLiveEditError"
    assert len(rig.watch.start_calls) == 1
    assert rig.watch.persistent_start_calls == []
    assert rig.authorization()["authorizationId"] == authorization["authorizationId"]
    assert rig.apply_calls[0][0] == _scope(old_binding)
    assert rig.manager._jobs[started["liveEditId"]]._apply_thread.is_alive()

    rig.release_apply.set()
    old_job = rig.manager._jobs[started["liveEditId"]]
    _wait_until(lambda: not old_job._thread.is_alive() and not old_job._apply_thread.is_alive())
    assert rig.manager.poll(started["liveEditId"])["activeReplacement"] is None
    result = rig.manager.reconcile_capture(new_binding)
    assert result["results"][0]["resumed"] is True
    assert result["results"][0]["liveEditId"] != started["liveEditId"]
    assert len(rig.watch.persistent_start_calls) == 1
    assert rig.authorization()["authorizationId"] == authorization["authorizationId"]


@pytest.mark.parametrize("revoke_before_authorize", [1, 2])
def test_revoked_persistent_authorization_cannot_be_resurrected_during_resume(rig, revoke_before_authorize):
    started = rig.start()
    authorization = rig.authorization()
    # Retire monitoring without revoking its durable consent, as host shutdown
    # and capture-session replacement do. Keep the previously read resume entry.
    rig.manager._jobs[started["liveEditId"]].stop()
    original_authorize = rig.manager._registry.authorize
    authorize_calls = []

    def authorize(*args, **kwargs):
        authorize_calls.append(kwargs.copy())
        if len(authorize_calls) == revoke_before_authorize:
            assert rig.manager._registry.revoke(
                rig.workspace, authorization["authorizationId"],
            )
        return original_authorize(*args, **kwargs)

    with mock.patch.object(rig.manager._registry, "authorize", side_effect=authorize):
        with pytest.raises(ShaderLiveEditError, match="revoked before resume"):
            rig.manager._start(rig.workspace, 100, persistent_entry=authorization)
    assert len(authorize_calls) == revoke_before_authorize
    assert all(call["authorization_id"] == authorization["authorizationId"]
               for call in authorize_calls)
    assert rig.manager.list()["authorizations"] == []
    assert list(rig.manager._jobs) == [started["liveEditId"]]
    assert rig.manager._active_by_workspace == {}
    assert len(rig.watch.start_calls) == revoke_before_authorize
    if revoke_before_authorize == 2:
        assert "%032x" % 2 in rig.watch.stop_calls


def test_failed_resume_is_bounded_per_capture_scope_but_a_later_session_can_retry(rig):
    rig.start()
    authorization = rig.authorization()
    second_binding = rig.gui_binding("new-gui", "2" * 32)
    rig.use_gui(second_binding)
    rig.bind_failure = True
    result = rig.manager.reconcile_capture(second_binding)
    assert result["results"][0]["resumed"] is False
    assert "mailbox unavailable" in result["results"][0]["message"]
    assert len(rig.watch.start_calls) == 1
    rig.bind_failure = False
    assert rig.manager.reconcile_capture(second_binding)["results"] == []
    assert len(rig.watch.start_calls) == 1
    third_binding = rig.gui_binding("new-gui", "3" * 32)
    rig.use_gui(third_binding)
    result = rig.manager.reconcile_capture(third_binding)
    assert result["results"][0]["resumed"] is True
    assert rig.authorization()["authorizationId"] == authorization["authorizationId"]
    assert len(rig.watch.persistent_start_calls) == 1


def test_reconcile_does_not_resume_authorizations_for_a_different_capture_path(rig):
    old = rig.start()
    binding = rig.gui_binding("new-gui", "2" * 32)
    rig.states[_scope(rig.binding)] = "expired"
    binding["capturePath"] = str(rig.capture.with_name("different.rdc"))
    assert rig.manager.reconcile_capture(binding)["results"] == []
    assert len(rig.watch.start_calls) == 1
    assert rig.authorization()["lastLiveEditId"] == old["liveEditId"]


def test_idle_live_worker_exits_when_capture_session_expires(rig):
    started = rig.start()
    authorization = rig.authorization()
    rig.states[_scope(rig.binding)] = "expired"
    old_job = rig.manager._jobs[started["liveEditId"]]
    _wait_until(lambda: not old_job._thread.is_alive() and not old_job._apply_thread.is_alive())
    assert rig.manager.poll(started["liveEditId"])["state"] == "failed"
    assert started["watchId"] in rig.watch.stop_calls
    assert rig.apply_calls == []
    assert rig.authorization()["authorizationId"] == authorization["authorizationId"]


def test_idle_live_worker_retains_temporary_unavailability_and_recovers(rig):
    started = rig.start()
    authorization = rig.authorization()
    old_job = rig.manager._jobs[started["liveEditId"]]
    rig.states[_scope(rig.binding)] = "unavailable"

    _wait_until(
        lambda: "captureSessionUnavailable" in {
            event["type"]
            for event in rig.manager.poll(started["liveEditId"])["events"]
        }
    )
    # Multiple bounded retry windows may elapse. The worker and consent stay
    # alive because an unavailable mailbox is not evidence that the capture
    # was closed or replaced.
    _wait_until(
        lambda: rig.manager.poll(started["liveEditId"])["sessionState"]
        == "unavailable"
    )
    assert old_job._thread.is_alive()
    assert old_job._apply_thread.is_alive()
    assert rig.manager.poll(started["liveEditId"])["state"] == "running"
    assert started["watchId"] not in rig.watch.stop_calls
    assert rig.authorization()["authorizationId"] == authorization["authorizationId"]

    rig.states[_scope(rig.binding)] = "current"
    _wait_until(
        lambda: "captureSessionAvailable" in {
            event["type"]
            for event in rig.manager.poll(started["liveEditId"])["events"]
        }
    )
    rig.watch.publish(started["watchId"], 6, "6" * 64)
    _wait_until(
        lambda: rig.manager.poll(started["liveEditId"])["applySuccesses"] == 1
    )
    assert old_job._thread.is_alive()
    assert old_job._apply_thread.is_alive()
    assert len(rig.apply_calls) == 1


def test_capture_reconcile_recovers_a_startup_resume_attempted_before_gui_was_available(rig):
    started = rig.start()
    authorization = rig.authorization()
    registry_path = rig.manager._registry.path
    rig.close()
    rig.bind_failure = True
    rig.manager = ShaderLiveEditManager(
        rig.watch,
        rig._unbound_apply,
        poll_seconds=0.005,
        join_seconds=0.1,
        session_unavailable_retry_seconds=0.03,
        registry_path=registry_path,
        workspace_identity=lambda _path: copy.deepcopy(rig.identity),
        bind_session=rig.bind_session,
    )
    initial_resume = rig.manager.resume_persistent()
    assert initial_resume["attempted"] is True
    assert initial_resume["results"][0]["resumed"] is False
    assert rig.authorization()["authorizationId"] == authorization["authorizationId"]
    assert rig.manager.resume_persistent() == initial_resume
    assert len(rig.watch.start_calls) == 1

    rig.bind_failure = False
    binding = rig.gui_binding("new-gui", "2" * 32)
    rig.use_gui(binding)
    recovered = rig.manager.reconcile_capture(binding)
    assert recovered["results"][0]["resumed"] is True
    assert recovered["results"][0]["liveEditId"] != started["liveEditId"]
    assert rig.authorization()["authorizationId"] == authorization["authorizationId"]
    assert len(rig.watch.persistent_start_calls) == 1
