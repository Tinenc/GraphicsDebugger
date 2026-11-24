"""Naming history, crash recovery and page retention share one transaction."""

import json
from contextlib import contextmanager

import pytest

from src.shader_workspace import aliases, alias_field_gc as gc, alias_field_storage as storage
from src.shader_workspace import cbuffer_alias_publication as publication

ORIGINAL = "a" * 64


def _setup(tmp_path, monkeypatch):
    root = tmp_path / "aliases"
    root.mkdir()
    monkeypatch.setattr(aliases, "project_store_root", lambda: root)
    monkeypatch.setattr(aliases, "user_store_root", lambda: tmp_path / "user")
    return root, tmp_path / "workspace"


def _batch(version, *, block="Material", bind=3):
    members = [{"name": "values", "offset": 0}]
    candidate = "%064x" % version
    return {"block": block, "members": members,
            "entries": [{"path": "values", "offset": 0, "alias": "MaterialValues%d" % version}],
            "shaderScope": {"shaderRawSha256": candidate, "stage": "fragment", "bind": bind,
                            "space": 0, "layoutSha256": aliases.layout_sha256(block, members)},
            "semanticFields": [{"name": "parameter%d_%d" % (version, index), "type": "float",
                                "components": [{"offset": index * 4}], "status": "inferred",
                                "provenance": "shader", "evidence": "Shader uses this scalar."}
                               for index in range(40)],
            "semanticSourceSha256": "%064x" % (version + 1000),
            "semanticCapturedShaderSha256": ORIGINAL,
            "semanticRetention": {"protectedShaderRawSha256": [ORIGINAL, candidate], "keepPrevious": 0}}


def _args(workspace, version, batches=None):
    return {"workspace_root": workspace, "operation_id": "publication-%d" % version,
            "request_sha256": "%064x" % (version + 100),
            "caller_request_sha256": "%064x" % (version + 200),
            "publications": batches if batches is not None else [_batch(version)],
            "build_result": lambda rows: {"published": rows}}


def _manifests(root):
    return {path.name: path.read_bytes() for path in root.glob("*.json") if len(path.stem) == 64}


def _pages(root):
    return {path.name: path.read_bytes() for path in (root / storage.DIRECTORY).glob("*.json")}


def test_repeated_publications_bound_active_views_and_new_page_storage(tmp_path, monkeypatch):
    root, workspace = _setup(tmp_path, monkeypatch)
    for version in range(1, 9):
        result = publication.publish_transaction(**_args(workspace, version))
        manifest = json.loads(next(iter(_manifests(root).values())))
        assert len(manifest["semanticViews"]) == 1
        assert manifest["semanticViews"][0]["shaderScope"]["shaderRawSha256"] == "%064x" % version
        assert len(_pages(root)) == 1
        assert result["semanticFieldCacheCleanup"]["complete"]
        assert result["semanticHistoryCleanup"]["removedViews"] == int(version > 1)
    assert not list((root / gc.PIN_DIRECTORY).glob("*.json"))


def test_changed_layout_history_is_pruned_and_old_receipts_never_resurrect_it(tmp_path, monkeypatch):
    root, workspace = _setup(tmp_path, monkeypatch)
    first = _args(workspace, 1, [_batch(1, block="OldReflectedName")])
    first_result = publication.publish_transaction(**first)
    second = _args(workspace, 2, [_batch(2, block="NewReflectedName")])
    result = publication.publish_transaction(**second)
    assert result["semanticHistoryCleanup"]["historicalLayoutsUpdated"] == 1
    assert result["semanticHistoryCleanup"]["removedViews"] == 1
    assert len(_pages(root)) == 1
    manifests, pages = _manifests(root), _pages(root)
    replay = publication.publish_transaction(**first)
    assert replay["idempotentReplay"]
    assert replay["published"] == first_result["published"]
    fast = publication.replay_committed_receipt(workspace_root=workspace,
        operation_id=first["operation_id"], caller_request_sha256=first["caller_request_sha256"])
    assert fast["idempotentReplay"]
    assert _manifests(root) == manifests and _pages(root) == pages
    # Dynamic historical targets are distinct from the original request scope.
    unused, ledger_path = publication.publication_paths(workspace, second["operation_id"])
    ledger = publication.read_ledger(ledger_path)
    assert len(ledger["targets"]) == 2 and len(ledger["requestedLayoutSha256"]) == 1
    assert publication.publish_transaction(**second)["idempotentReplay"]


def test_failure_after_first_live_replace_restores_every_layout_and_retries(tmp_path, monkeypatch):
    root, workspace = _setup(tmp_path, monkeypatch)
    publication.publish_transaction(**_args(workspace, 1, [_batch(1, block="OldName")]))
    before = _manifests(root)
    newer = _args(workspace, 2, [_batch(2, block="NewName")])
    write = publication.atomic_write_bytes
    replacements = []
    failed = []
    def interrupted(path, raw):
        if path.parent == root and len(path.stem) == 64:
            replacements.append(path)
            if len(replacements) == 2 and not failed:
                failed.append(True)
                raise OSError("second manifest replacement interrupted")
        return write(path, raw)
    monkeypatch.setattr(publication, "atomic_write_bytes", interrupted)
    with pytest.raises(OSError, match="second manifest"):
        publication.publish_transaction(**newer)
    assert _manifests(root) == before
    unused, ledger_path = publication.publication_paths(workspace, newer["operation_id"])
    assert publication.read_ledger(ledger_path)["phase"] == "failed-rolled-back"
    assert not list((root / gc.PIN_DIRECTORY).glob("*.json"))
    assert publication.publish_transaction(**newer)["semanticHistoryCleanup"]["removedViews"] == 1
    assert len(_pages(root)) == 1


def test_restored_transaction_is_not_marked_recovery_required_for_cleanup_error(tmp_path, monkeypatch):
    root, workspace = _setup(tmp_path, monkeypatch)
    publication.publish_transaction(**_args(workspace, 1))
    before = _manifests(root)
    write = publication.atomic_write_bytes
    failed = []
    def interrupted(path, raw):
        if path.parent == root and len(path.stem) == 64 and not failed:
            failed.append(True)
            raise OSError("manifest replace interrupted")
        return write(path, raw)
    monkeypatch.setattr(publication, "atomic_write_bytes", interrupted)
    cleanup = publication._cleanup_committed_staging
    def busy(directory, ledger):
        if ledger.get("phase") == "failed-rolled-back":
            raise OSError("backup temporarily busy")
        return cleanup(directory, ledger)
    monkeypatch.setattr(publication, "_cleanup_committed_staging", busy)
    newer = _args(workspace, 2)
    with pytest.raises(OSError, match="manifest replace"):
        publication.publish_transaction(**newer)
    assert _manifests(root) == before
    unused, ledger_path = publication.publication_paths(workspace, newer["operation_id"])
    ledger = publication.read_ledger(ledger_path)
    assert ledger["phase"] == "failed-rolled-back"
    assert "busy" in ledger["cleanupError"]
    assert list((root / gc.PIN_DIRECTORY).glob("*.json"))
    monkeypatch.setattr(publication, "_cleanup_committed_staging", cleanup)
    assert publication.publish_transaction(**newer)["semanticFieldCacheCleanup"]["complete"]


def test_other_workspace_pending_rollback_pages_survive_successful_publication(tmp_path, monkeypatch):
    root, workspace = _setup(tmp_path, monkeypatch)
    publication.publish_transaction(**_args(workspace, 1))
    old_path = root / next(iter(_manifests(root)))
    old_manifest = json.loads(old_path.read_bytes())
    other = tmp_path / "other-workspace" / "operation"
    other.mkdir(parents=True)
    backup = other / "backup.json"
    backup.write_bytes(old_path.read_bytes())
    owner = {"transaction_dir": other, "operation_id": "pending-elsewhere", "request_sha256": "d" * 64}
    with aliases.alias_store_transactions([root]):
        gc.pin_transaction(root, **owner, manifest_paths=[backup])
    result = publication.publish_transaction(**_args(workspace, 2))
    assert result["semanticFieldCacheCleanup"]["pendingTransactions"] == 1
    assert len(_pages(root)) == 2
    assert storage.read_fields(old_manifest["semanticViews"][0], root)
    with aliases.alias_store_transactions([root]):
        gc.release_transaction_pin(root, **owner)
        assert gc.collect_unreferenced_pages(root)["deletedPages"] == 1
    assert len(_pages(root)) == 1


def test_maintenance_error_does_not_invalidate_committed_names(tmp_path, monkeypatch):
    root, workspace = _setup(tmp_path, monkeypatch)
    def busy(unused):
        raise OSError("cache busy")
    monkeypatch.setattr(gc, "collect_unreferenced_pages", busy)
    result = publication.publish_transaction(**_args(workspace, 1))
    assert result["published"][0]["applied"]
    assert result["semanticFieldCacheCleanup"]["deferred"]
    assert publication.publish_transaction(**_args(workspace, 1))["idempotentReplay"]


def test_invalid_old_layouts_are_reported_bounded_without_blocking_current_publish(tmp_path, monkeypatch):
    root, workspace = _setup(tmp_path, monkeypatch)
    for index in range(10):
        (root / ("%064x.json" % (index + 5000))).write_text("not-json")
    result = publication.publish_transaction(**_args(workspace, 1))
    cleanup = result["semanticHistoryCleanup"]
    assert result["published"][0]["applied"]
    assert cleanup["deferred"] and cleanup["deferredLayoutCount"] == 10
    assert len(cleanup["diagnostics"]) == 8
    assert all(len(row["reason"]) <= 400 for row in cleanup["diagnostics"])


def test_interrupted_applying_survives_other_workspace_gc_and_recovers(tmp_path, monkeypatch):
    root, workspace = _setup(tmp_path, monkeypatch)
    publication.publish_transaction(**_args(workspace, 1))
    old_view = json.loads(next(iter(_manifests(root).values())))["semanticViews"][0]
    write = publication.atomic_write_bytes
    interrupted = []
    def terminate_after_replace(path, raw):
        write(path, raw)
        if path.parent == root and len(path.stem) == 64 and not interrupted:
            interrupted.append(True)
            raise SystemExit("process stopped after replacing live manifest")
    monkeypatch.setattr(publication, "atomic_write_bytes", terminate_after_replace)
    newer = _args(workspace, 2)
    with pytest.raises(SystemExit):
        publication.publish_transaction(**newer)
    unused, ledger_path = publication.publication_paths(workspace, newer["operation_id"])
    assert publication.read_ledger(ledger_path)["phase"] == "applying"
    monkeypatch.setattr(publication, "atomic_write_bytes", write)
    other = _args(tmp_path / "other-workspace", 3, [_batch(3, block="OtherBinding", bind=4)])
    result = publication.publish_transaction(**other)
    assert result["semanticFieldCacheCleanup"]["pendingTransactions"] == 1
    assert storage.read_fields(old_view, root)
    assert len(_pages(root)) == 3
    recovered = publication.publish_transaction(**newer)
    assert recovered["semanticFieldCacheCleanup"]["complete"]
    assert len(_pages(root)) == 2
    old_page = root / storage.DIRECTORY / (old_view["fieldChunks"][0]["sha256"] + ".json")
    assert not old_page.exists()
    assert not list((root / gc.PIN_DIRECTORY).glob("*.json"))


def test_fast_committed_replay_holds_root_lock_when_releasing_pin(tmp_path, monkeypatch):
    root, workspace = _setup(tmp_path, monkeypatch)
    args = _args(workspace, 1)
    publication.publish_transaction(**args)
    locked = []
    store_lock = aliases.alias_store_transactions
    release = gc.release_transaction_pin
    @contextmanager
    def observe(roots):
        with store_lock(roots):
            locked.append(list(roots))
            try:
                yield
            finally:
                locked.pop()
    def checked_release(alias_root, **kwargs):
        assert locked and root in locked[-1]
        return release(alias_root, **kwargs)
    monkeypatch.setattr(aliases, "alias_store_transactions", observe)
    monkeypatch.setattr(gc, "release_transaction_pin", checked_release)
    assert publication.replay_committed_receipt(workspace_root=workspace,
        operation_id=args["operation_id"], caller_request_sha256=args["caller_request_sha256"])["idempotentReplay"]
