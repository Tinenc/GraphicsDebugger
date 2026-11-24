"""Project preparation stays readable; only verified commits serialize selection."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import threading
from unittest import mock

import pytest

from src.active_projects import ActiveProjectError, ActiveReplayProjectService


@pytest.fixture
def projects(tmp_path):
    service = ActiveReplayProjectService(tmp_path / "state" / "active.json", tmp_path / "projects")
    first = service.create_project(name="First", game_id="game", build_id="build",
                                   platform="windows", api_family="d3d12")
    second = service.create_project(name="Second", game_id="other", build_id="other")
    service.activate(first["projectRoot"])
    return service, first, second


def test_long_snapshot_does_not_hold_selection_lock_or_block_readers(projects):
    service, first, unused = projects
    started, release = threading.Event(), threading.Event()

    def prepare():
        with service.snapshot_active_project(first["workspaceId"], first["projectRoot"]):
            started.set()
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(prepare)
        try:
            assert started.wait(1)
            assert not service._selection_lock_path.exists()
            status = pool.submit(service.status).result(timeout=1)
            assert status["workspaceId"] == first["workspaceId"]
            assert pool.submit(service.replay_service).result(timeout=1).status()["scope"]["gameId"] == "game"
        finally:
            release.set()
        task.result(timeout=1)


def test_snapshot_reads_stay_on_original_project_and_to_thread_inherits_it(projects):
    service, first, second = projects
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(ActiveProjectError, match="selection or configuration changed"):
            with service.snapshot_active_project(first["workspaceId"], first["projectRoot"]) as snapshot:
                pool.submit(service.activate, second["projectRoot"]).result(timeout=1)
                assert service.status()["workspaceId"] == first["workspaceId"]
                assert service.replay_service().status()["scope"]["gameId"] == "game"
                propagated = asyncio.run(asyncio.to_thread(service.status))
                assert propagated["workspaceId"] == first["workspaceId"]
                # Returned status is not mutable shared snapshot state.
                propagated["scope"]["gameId"] = "tampered"
                assert service.status()["scope"]["gameId"] == "game"
                with pytest.raises(ActiveProjectError, match="changed"):
                    service.assert_active_project_snapshot(snapshot)
                with mock.patch.object(service, "replay_service") as publish:
                    with pytest.raises(ActiveProjectError, match="changed"):
                        service.register_alias_table("unused.json")
                    publish.assert_not_called()
    assert service.status()["workspaceId"] == second["workspaceId"]


def test_commit_blocks_project_switch_only_during_its_body(projects):
    service, first, second = projects
    with service.snapshot_active_project(first["workspaceId"], first["projectRoot"]) as snapshot:
        pass
    started = threading.Event()

    def switch():
        started.set()
        return service.activate(second["projectRoot"])

    with ThreadPoolExecutor(max_workers=1) as pool:
        with service.commit_active_project_snapshot(snapshot):
            switch_task = pool.submit(switch)
            assert started.wait(1)
            assert service._selection_lock_path.is_file()
            with pytest.raises(TimeoutError):
                switch_task.result(timeout=0.05)
            assert service.status()["workspaceId"] == first["workspaceId"]
        assert switch_task.result(timeout=1)["workspaceId"] == second["workspaceId"]
    with pytest.raises(ActiveProjectError, match="changed"):
        with service.commit_active_project_snapshot(snapshot):
            pytest.fail("a stale prepared result must never commit")


def test_selection_away_and_back_invalidates_prepared_snapshot(projects):
    service, first, second = projects
    with service.snapshot_active_project(first["workspaceId"], first["projectRoot"]) as snapshot:
        pass
    service.activate(second["projectRoot"])
    service.activate(first["projectRoot"])
    with pytest.raises(ActiveProjectError, match="changed"):
        service.assert_active_project_snapshot(snapshot)


def test_reactivating_same_project_preserves_selection_seal_and_real_snapshot(projects):
    service, first, unused = projects
    before = service.state_path.read_bytes()
    with service.snapshot_active_project(first["workspaceId"], first["projectRoot"]) as snapshot:
        with mock.patch("src.active_projects._utc_now", return_value="2099-01-01T00:00:00Z"):
            assert service.activate(first["projectRoot"])["workspaceId"] == first["workspaceId"]
        assert service.state_path.read_bytes() == before
        service.assert_active_project_snapshot(snapshot)
        with service.commit_active_project_snapshot(snapshot):
            pass


def test_same_project_activation_does_not_hide_configuration_change(projects, tmp_path):
    service, first, unused = projects
    with service.snapshot_active_project(first["workspaceId"], first["projectRoot"]) as snapshot:
        pass
    asset_root = tmp_path / "changed-assets"
    asset_root.mkdir()
    service.configure_asset_root(asset_root)
    service.activate(first["projectRoot"])
    with pytest.raises(ActiveProjectError, match="configuration changed"):
        service.assert_active_project_snapshot(snapshot)


def test_asset_configuration_change_invalidates_snapshot(projects, tmp_path):
    service, first, unused = projects
    with service.snapshot_active_project(first["workspaceId"], first["projectRoot"]) as snapshot:
        pass
    asset_root = tmp_path / "unpacked"
    asset_root.mkdir()
    service.configure_asset_root(asset_root)
    with pytest.raises(ActiveProjectError, match="changed"):
        with service.commit_active_project_snapshot(snapshot):
            pytest.fail("changed asset context must be rejected before publication")


def test_snapshot_is_bound_to_selection_service(projects, tmp_path):
    service, first, unused = projects
    other = ActiveReplayProjectService(tmp_path / "other-state" / "active.json", tmp_path / "other-projects")
    with service.snapshot_active_project(first["workspaceId"], first["projectRoot"]) as snapshot:
        with pytest.raises(ActiveProjectError, match="another selection service"):
            other.assert_active_project_snapshot(snapshot)


@pytest.mark.parametrize("method,validation", [
    ("status", "_status_from_active"), ("replay_service", "_replay_service_for")])
def test_expensive_project_validation_runs_outside_selection_lock(projects, method, validation):
    service, first, second = projects
    entered, release = threading.Event(), threading.Event()
    original = getattr(service, validation)

    def slow(root, manifest):
        if threading.current_thread().name.startswith("slow-project-read"):
            entered.set()
            assert release.wait(3)
        return original(root, manifest)

    with mock.patch.object(service, validation, side_effect=slow), \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="slow-project-read") as slow_pool, \
            ThreadPoolExecutor(max_workers=1) as writer:
        task = slow_pool.submit(getattr(service, method))
        try:
            assert entered.wait(1)
            assert not service._selection_lock_path.exists()
            assert writer.submit(service.activate, second["projectRoot"]).result(timeout=1)["workspaceId"] == second["workspaceId"]
        finally:
            release.set()
        result = task.result(timeout=1)
    if method == "status":
        assert result["workspaceId"] == first["workspaceId"]
    else:
        assert result.status()["scope"]["gameId"] == "game"


def test_scope_sync_without_changes_does_not_invalidate_snapshot(projects):
    service, first, unused = projects
    with service.snapshot_active_project(first["workspaceId"], first["projectRoot"]) as snapshot:
        with service.commit_active_project_snapshot(snapshot):
            with mock.patch.object(service, "_write_workspace", wraps=service._write_workspace) as write:
                service._sync_scope_from_replay(snapshot.project_root)
                write.assert_not_called()
        service.assert_active_project_snapshot(snapshot)


def test_snapshot_exit_preserves_original_failure_and_unbinds_context(projects):
    service, first, second = projects
    with pytest.raises(ValueError, match="preparation failed"):
        with service.snapshot_active_project(first["workspaceId"], first["projectRoot"]):
            service.activate(second["projectRoot"])
            raise ValueError("preparation failed")
    assert service.status()["workspaceId"] == second["workspaceId"]


@pytest.mark.parametrize("damage", ["changed", "missing", "malformed-index"])
def test_lifecycle_check_does_not_depend_on_explanatory_artifacts(projects, damage):
    service, first, unused = projects
    root = Path(first["projectRoot"])
    raw = b"flowchart TD\n  A --> B\n"
    records = service._register_explanatory_artifact(
        root, first["workspaceId"], "a" * 32,
        (raw, hashlib.sha256(raw).hexdigest()),
        kind="mermaid-flowchart", suffix="mmd", label="flowchart",
    )
    with service.snapshot_active_project(first["workspaceId"], root) as snapshot:
        artifact = root / records[0]["artifactPath"]
        if damage == "changed":
            artifact.write_bytes(b"changed diagram")
        elif damage == "missing":
            artifact.unlink()
        else:
            (root / "shared/dictionaries/index.json").write_text("{}", encoding="utf-8")
        service.assert_active_project_snapshot(snapshot)
        with service.commit_active_project_snapshot(snapshot):
            assert snapshot.workspace_id == first["workspaceId"]
        # Actual artifact reads still fail rather than serving damaged diagrams.
        with pytest.raises(ActiveProjectError):
            service._read_shared_index(root, first["workspaceId"])
    # Even an already damaged explanatory cache does not prevent a new lifecycle
    # snapshot; normal comprehensive project reads continue to validate it.
    with service.snapshot_active_project(first["workspaceId"], root):
        pass
    with pytest.raises(ActiveProjectError):
        service.status()


def test_lifecycle_snapshot_keeps_managed_directory_boundary_checks(projects):
    service, first, unused = projects
    with service.snapshot_active_project(first["workspaceId"], first["projectRoot"]) as snapshot:
        pass
    (Path(first["projectRoot"]) / "shared/dictionaries/explanatory").rmdir()
    with pytest.raises(ActiveProjectError, match="missing or redirected"):
        service.assert_active_project_snapshot(snapshot)
