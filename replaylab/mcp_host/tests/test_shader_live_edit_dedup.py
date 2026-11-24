from __future__ import annotations

import copy
from pathlib import Path

import pytest

from src.shader_live_edit import MAX_ACTIVE_LIVE_EDITS, ShaderLiveEditError, ShaderLiveEditManager
from tests.test_shader_live_edit import _wait_until
from tests.test_shader_live_edit_lifecycle import (
    _LifecycleRig,
    _PersistentWatchManager,
)


class _DedupRig(_LifecycleRig):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.identities = {str(self.workspace.resolve()): copy.deepcopy(self.identity)}
        self.registry_path = tmp_path / "config" / "live-edit.json"
        self.manager.shutdown()
        self.manager = self.new_manager(self.watch)

    def new_manager(self, watch):
        return ShaderLiveEditManager(
            watch,
            self._unbound_apply,
            poll_seconds=0.005,
            join_seconds=0.1,
            registry_path=self.registry_path,
            workspace_identity=lambda path: copy.deepcopy(self.identities[str(Path(path).resolve())]),
            bind_session=self.bind_session,
        )

    def another_workspace(self, name="newer-workspace", *, resource_id=77):
        workspace = self.workspace.parent / name
        workspace.mkdir()
        identity = copy.deepcopy(self.identity)
        identity["workspaceId"] = "%032x" % (len(self.identities) + 1)
        identity["resourceId"] = resource_id
        identity["shaderKey"] = "%d:fragment:MainPS:%s" % (resource_id, identity["shaderRawSha256"])
        self.identities[str(workspace.resolve())] = identity
        return workspace

    def seed_history(self, *workspaces):
        """Create real durable consent, then finish the seeding Host's workers."""
        seeder = self.new_manager(_PersistentWatchManager())
        try:
            for workspace in workspaces:
                started = seeder.start(workspace, 100)
                _wait_until(lambda: seeder.poll(started["liveEditId"])["state"] == "running")
            return copy.deepcopy(seeder.list()["authorizations"])
        finally:
            seeder.shutdown()
            for job in seeder._jobs.values():
                job._thread.join(2.0)
                job._apply_thread.join(2.0)
                assert not job._thread.is_alive()
                assert not job._apply_thread.is_alive()

    def entries(self):
        return {entry["workspacePath"]: entry for entry in self.manager.list()["authorizations"]}

    def active(self):
        return [job for job in self.manager.list()["sessions"]
                if job["state"] not in {"stopped", "failed"}]


@pytest.fixture
def rig(tmp_path):
    value = _DedupRig(tmp_path)
    try:
        yield value
    finally:
        value.close()


def test_same_physical_shader_restores_only_the_latest_explicitly_authorized_workspace(rig):
    newer = rig.another_workspace()
    rig.seed_history(rig.workspace, newer)
    entries_before = rig.entries()
    assert entries_before[str(newer)]["authorizedAt"] > entries_before[str(rig.workspace)]["authorizedAt"]

    result = rig.manager.reconcile_capture(copy.deepcopy(rig.binding))
    resumed = [item for item in result["results"] if item.get("resumed") is True]
    assert len(resumed) == 1
    assert resumed[0]["workspacePath"] == str(newer)
    assert rig.watch.persistent_start_calls == [(str(newer), 100)]
    assert len(rig.active()) == 1
    assert set(rig.entries()) == set(entries_before)


def test_full_and_bounded_catalog_authorizations_restore_one_physical_shader(rig):
    bounded = rig.another_workspace("bounded-catalog-workspace")
    # A full-frame and one-event catalog have distinct captureIds, even when
    # they bind the same original Shader resource in the same loaded RDC.
    rig.identities[str(bounded.resolve())]["captureId"] = "d" * 64
    rig.seed_history(rig.workspace, bounded)
    entries_before = rig.entries()
    assert entries_before[str(rig.workspace)]["workspaceIdentity"]["captureId"] != (
        entries_before[str(bounded)]["workspaceIdentity"]["captureId"])

    result = rig.manager.reconcile_capture(copy.deepcopy(rig.binding))
    assert len(result["results"]) == 1
    assert result["results"][0]["resumed"] is True
    assert result["results"][0]["workspacePath"] == str(bounded)
    assert rig.watch.persistent_start_calls == [(str(bounded), 100)]
    assert len(rig.active()) == 1
    for path, entry in rig.entries().items():
        assert entry["authorizationId"] == entries_before[path]["authorizationId"]
        assert entry["workspaceIdentity"] == entries_before[path]["workspaceIdentity"]
    assert {path: entry["authorizationId"] for path, entry in rig.entries().items()} == {
        path: entry["authorizationId"] for path, entry in entries_before.items()
    }
    for _ in range(3):
        rig.manager.reconcile_capture(copy.deepcopy(rig.binding))
    assert len(rig.watch.persistent_start_calls) == 1


def test_identical_shader_bytes_with_different_resource_ids_restore_independent_workspaces(rig):
    distinct = rig.another_workspace("other-resource", resource_id=88)
    rig.seed_history(rig.workspace, distinct)
    assert rig.identities[str(distinct)]["shaderRawSha256"] == rig.identity["shaderRawSha256"]

    result = rig.manager.reconcile_capture(copy.deepcopy(rig.binding))
    resumed = [item for item in result["results"] if item.get("resumed") is True]
    assert {item["workspacePath"] for item in resumed} == {str(rig.workspace), str(distinct)}
    assert len(rig.watch.persistent_start_calls) == 2
    assert len(rig.active()) == 2
    assert len(rig.entries()) == 2


def test_running_current_workspace_wins_over_a_newer_historical_authorization(rig):
    current = rig.start()
    newer = rig.another_workspace()
    rig.seed_history(newer)
    before = rig.entries()
    assert before[str(newer)]["authorizedAt"] > before[str(rig.workspace)]["authorizedAt"]

    rig.manager.reconcile_capture(copy.deepcopy(rig.binding))
    assert rig.watch.persistent_start_calls == []
    assert len(rig.watch.start_calls) == 1
    assert rig.manager.poll(current["liveEditId"])["state"] == "running"
    assert [job["liveEditId"] for job in rig.active()] == [current["liveEditId"]]
    assert rig.watch.stop_calls == []
    assert rig.entries() == before


def test_explicit_reselection_survives_restart_without_automatic_resume_reauthorizing(rig):
    newer = rig.another_workspace()
    rig.seed_history(rig.workspace, newer)
    initial = rig.entries()
    assert initial[str(newer)]["authorizedAt"] > initial[str(rig.workspace)]["authorizedAt"]

    # A real explicit start selects the older source again. It retains durable
    # consent identity while recording this newer user choice.
    rig.seed_history(rig.workspace)
    selected = rig.entries()
    assert selected[str(rig.workspace)]["authorizationId"] == initial[str(rig.workspace)]["authorizationId"]
    assert selected[str(rig.workspace)]["authorizedAt"] > selected[str(newer)]["authorizedAt"]
    selected_dates = {path: entry["authorizedAt"] for path, entry in selected.items()}

    for restart in range(2):
        if restart:
            rig.close()
            rig.watch = _PersistentWatchManager()
            rig.manager = rig.new_manager(rig.watch)
        result = rig.manager.reconcile_capture(copy.deepcopy(rig.binding))
        resumed = [item for item in result["results"] if item.get("resumed") is True]
        assert len(resumed) == 1
        assert resumed[0]["workspacePath"] == str(rig.workspace)
        assert rig.watch.persistent_start_calls == [(str(rig.workspace), 100)]
        assert {path: entry["authorizedAt"] for path, entry in rig.entries().items()} == selected_dates
        assert rig.entries()[str(rig.workspace)]["updatedAt"] > selected_dates[str(rig.workspace)]


@pytest.mark.parametrize("owner_state", ["current", "unavailable"])
def test_foreign_or_unavailable_owner_blocks_other_workspaces_for_the_same_shader(rig, owner_state):
    old = rig.start()
    newer = rig.another_workspace()
    rig.seed_history(newer)
    before = rig.entries()
    new_binding = rig.gui_binding("new-gui", "2" * 32)
    rig.use_gui(new_binding, previous_state=owner_state)
    if owner_state == "unavailable":
        _wait_until(lambda: rig.manager.poll(old["liveEditId"])["sessionState"] == "unavailable")

    rig.manager.reconcile_capture(new_binding)
    assert rig.watch.persistent_start_calls == []
    assert len(rig.watch.start_calls) == 1
    assert rig.entries() == before
    if owner_state == "current":
        assert rig.manager.poll(old["liveEditId"])["state"] == "running"
        assert rig.watch.stop_calls == []
    else:
        # A temporarily unresponsive GUI retains the owner so historical
        # consent cannot silently transfer its captured Shader elsewhere.
        owner = rig.manager.poll(old["liveEditId"])
        assert owner["state"] == "running"
        assert owner["sessionState"] == "unavailable"
        assert rig.watch.stop_calls == []


def test_new_capture_session_retires_old_group_owner_before_resuming_newer_workspace(rig):
    old = rig.start()
    newer = rig.another_workspace()
    rig.seed_history(newer)
    authorization_ids = {entry["authorizationId"] for entry in rig.entries().values()}
    new_binding = rig.gui_binding("new-gui", "2" * 32)
    rig.use_gui(new_binding)

    result = rig.manager.reconcile_capture(new_binding)
    resumed = [item for item in result["results"] if item.get("resumed") is True]
    assert len(resumed) == 1
    assert resumed[0]["workspacePath"] == str(newer)
    assert resumed[0]["liveEditId"] != old["liveEditId"]
    assert rig.manager.poll(old["liveEditId"])["state"] in {"stopped", "failed"}
    assert old["watchId"] in rig.watch.stop_calls
    assert rig.manager.poll(resumed[0]["liveEditId"])["captureBinding"] == new_binding
    assert rig.watch.persistent_start_calls == [(str(newer), 100)]
    assert len(rig.active()) == 1
    assert {entry["authorizationId"] for entry in rig.entries().values()} == authorization_ids


def test_two_current_duplicates_keep_latest_explicit_owner_without_revoking_history(rig):
    older = rig.start()
    newer = rig.another_workspace()
    latest = rig.manager.start(newer, 100)
    _wait_until(lambda: rig.manager.poll(latest["liveEditId"])["state"] == "running")
    before = rig.entries()

    rig.manager.reconcile_capture(copy.deepcopy(rig.binding))
    assert rig.watch.persistent_start_calls == []
    assert len(rig.watch.start_calls) == 2
    assert rig.manager.poll(older["liveEditId"])["state"] in {"stopped", "failed"}
    assert older["watchId"] in rig.watch.stop_calls
    assert rig.manager.poll(latest["liveEditId"])["state"] == "running"
    assert [job["liveEditId"] for job in rig.active()] == [latest["liveEditId"]]
    assert rig.entries() == before


def test_group_does_not_start_another_workspace_while_old_apply_worker_cannot_stop(rig):
    rig.block_apply = True
    old = rig.start()
    newer = rig.another_workspace()
    rig.seed_history(newer)
    before = rig.entries()
    rig.watch.publish(old["watchId"], 6, "6" * 64)
    assert rig.apply_entered.wait(2.0)
    new_binding = rig.gui_binding("new-gui", "2" * 32)
    rig.use_gui(new_binding)

    result = rig.manager.reconcile_capture(new_binding)
    assert not any(item.get("resumed") is True for item in result["results"])
    assert rig.watch.persistent_start_calls == []
    assert len(rig.watch.start_calls) == 1
    assert rig.entries() == before


def test_project_restore_can_authorize_all_five_shaders_after_capture_local_id_changes(rig):
    historical = [rig.another_workspace("historical-%d" % index, resource_id=700 + index) for index in range(5)]
    for index, workspace in enumerate(historical):
        identity = rig.identities[str(workspace)]
        identity["shaderRawSha256"] = "%064x" % (index + 1)
        identity["shaderKey"] = "%d:fragment:MainPS:%s" % (identity["resourceId"], identity["shaderRawSha256"])
    rig.seed_history(*historical)
    before = copy.deepcopy(rig.entries())
    restored = [rig.another_workspace("restored-%d" % index, resource_id=900 + index) for index in range(5)]
    for index, workspace in enumerate(restored):
        identity = rig.identities[str(workspace)]
        identity["shaderRawSha256"] = rig.identities[str(historical[index])]["shaderRawSha256"]
        identity["shaderKey"] = "%d:fragment:MainPS:%s" % (identity["resourceId"], identity["shaderRawSha256"])
        identity["captureId"] = "%064x" % (index + 100)
        rig.manager.start(workspace, 100)
    _wait_until(lambda: len(rig.active()) == 5)
    entries = rig.entries()
    assert len(entries) == 10
    assert len(rig.watch.start_calls) == 5
    assert {job["workspacePath"] for job in rig.active()} == {str(path) for path in restored}
    # Historic consent is neither revoked nor retargeted by matching raw bytes.
    assert {path: entries[path] for path in before} == before
    for workspace in restored:
        assert entries[str(workspace)]["workspaceIdentity"] == rig.identities[str(workspace)]
    assert not rig.apply_calls  # Starting watchers must not apply a baseline again.
    # A new Host can read the registry even though it exceeds active concurrency.
    restarted = rig.new_manager(_PersistentWatchManager())
    try:
        assert restarted.list()["authorizations"] == rig.manager.list()["authorizations"]
        assert restarted.list()["sessions"] == []
    finally:
        restarted.shutdown()


def test_historical_authorizations_do_not_raise_the_actual_concurrent_editor_limit(rig):
    historical = [rig.another_workspace("previous-%d" % index, resource_id=700 + index)
                  for index in range(MAX_ACTIVE_LIVE_EDITS)]
    rig.seed_history(*historical)
    previous_entries = copy.deepcopy(rig.entries())
    current = [rig.another_workspace("current-%d" % index, resource_id=900 + index)
               for index in range(MAX_ACTIVE_LIVE_EDITS)]
    for workspace in current:
        rig.manager.start(workspace, 100)
    _wait_until(lambda: len(rig.active()) == MAX_ACTIVE_LIVE_EDITS)
    overflow = rig.another_workspace("current-overflow", resource_id=999)
    entries_before_rejection = copy.deepcopy(rig.entries())
    with pytest.raises(ShaderLiveEditError, match="active live-edit limit reached"):
        rig.manager.start(overflow, 100)
    assert rig.entries() == entries_before_rejection
    assert {path: rig.entries()[path] for path in previous_entries} == previous_entries
    assert len(rig.watch.start_calls) == MAX_ACTIVE_LIVE_EDITS
    assert len(rig.entries()) == MAX_ACTIVE_LIVE_EDITS * 2
