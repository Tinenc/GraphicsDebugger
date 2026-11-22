"""Retired pages are collected without losing live or recoverable names."""

import json

import pytest

from src.shader_workspace import aliases, alias_field_gc as gc, alias_field_storage as storage


def _view(root, name):
    return storage.store_view({"fields": [{"name": "%s_%d" % (name, index)}
                                           for index in range(40)]}, root)


def _manifest(root, key, views):
    path = root / (key * 64 + ".json")
    path.write_bytes(storage.json_bytes({"schema": aliases.SCHEMA, "layoutSha256": key * 64,
                                        "semanticViews": views}))
    return path


def _page(root, view):
    return root / storage.DIRECTORY / (view["fieldChunks"][0]["sha256"] + ".json")


def _owner(transaction):
    return {"transaction_dir": transaction, "operation_id": "publish-reorganized",
            "request_sha256": "a" * 64}


def test_epoch_keeps_only_fixed_pre_upgrade_pages_not_new_history(tmp_path):
    legacy = _view(tmp_path, "legacy-rollback")
    marker = gc.ensure_epoch(tmp_path)
    fresh = _view(tmp_path, "retired-new-publication")
    assert gc.ensure_epoch(tmp_path) == marker
    result = gc.collect_unreferenced_pages(tmp_path)
    assert result["deletedPages"] == 1
    assert result["legacyProtectedPages"] == 1
    assert _page(tmp_path, legacy).is_file()
    assert not _page(tmp_path, fresh).exists()
    assert gc.collect_unreferenced_pages(tmp_path)["deletedPages"] == 0


def test_shared_live_user_or_other_shader_page_is_kept_until_last_reference_removed(tmp_path):
    gc.ensure_epoch(tmp_path)
    shared = _view(tmp_path, "shared-user-name")
    _manifest(tmp_path, "a", [dict(shared, source="user")])
    _manifest(tmp_path, "b", [dict(shared, source="ai")])
    _manifest(tmp_path, "b", [])
    assert gc.collect_unreferenced_pages(tmp_path)["deletedPages"] == 0
    assert storage.read_fields(shared, tmp_path)
    _manifest(tmp_path, "a", [])
    assert gc.collect_unreferenced_pages(tmp_path)["deletedPages"] == 1


def test_pending_pin_preserves_original_and_staged_pages_across_other_publication(tmp_path):
    root, transaction = tmp_path / "store", tmp_path / "workspace" / "operation"
    transaction.mkdir(parents=True)
    gc.ensure_epoch(root)
    original, staged, orphan = [_view(root, name) for name in ("original", "staged", "orphan")]
    backup = transaction / "backup.json"
    backup.write_bytes(storage.json_bytes({"semanticViews": [original]}))
    proposal = transaction / "staged.json"
    proposal.write_bytes(storage.json_bytes({"semanticViews": [staged]}))
    key = gc.pin_transaction(root, **_owner(transaction), manifest_paths=[backup, proposal])
    _manifest(root, "c", [staged])
    # This models a crash after only some live manifests were replaced. Another
    # operation can collect its pages without breaking the first one's rollback.
    assert gc.collect_unreferenced_pages(root)["deletedPages"] == 1
    assert not _page(root, orphan).exists()
    assert storage.read_fields(original, root)
    assert storage.read_fields(staged, root)
    assert (root / gc.PIN_DIRECTORY / (key + ".json")).is_file()
    # Rollback restores its original manifest before releasing its durable pin.
    _manifest(root, "c", [original])
    backup.unlink()
    proposal.unlink()
    gc.release_transaction_pin(root, **_owner(transaction))
    gc.release_transaction_pin(root, **_owner(transaction))
    assert gc.collect_unreferenced_pages(root)["deletedPages"] == 1
    assert not _page(root, staged).exists()
    assert storage.read_fields(original, root)


@pytest.mark.parametrize("bad_reference", ["manifest", "pin", "marker"])
def test_invalid_reference_metadata_defers_entire_sweep(tmp_path, bad_reference):
    gc.ensure_epoch(tmp_path)
    orphan = _view(tmp_path, "orphan")
    if bad_reference == "manifest":
        (tmp_path / ("b" * 64 + ".json")).write_text('{"semanticViews": []}')
    elif bad_reference == "pin":
        directory = tmp_path / gc.PIN_DIRECTORY
        directory.mkdir()
        (directory / ("c" * 64 + ".json")).write_text('{"schema": "bad"}')
    else:
        (tmp_path / gc.EPOCH_FILE).write_text('{"schema": "bad"}')
    with pytest.raises(ValueError):
        gc.collect_unreferenced_pages(tmp_path)
    assert _page(tmp_path, orphan).is_file()


def test_corrupt_orphan_prevents_even_valid_orphans_being_deleted(tmp_path):
    gc.ensure_epoch(tmp_path)
    valid, corrupt = _view(tmp_path, "valid"), _view(tmp_path, "corrupt")
    _page(tmp_path, corrupt).write_bytes(b"[]")
    with pytest.raises(ValueError):
        gc.collect_unreferenced_pages(tmp_path)
    assert _page(tmp_path, valid).is_file()
    assert _page(tmp_path, corrupt).is_file()


def test_pin_rejects_foreign_manifest_and_release_checks_owner(tmp_path):
    root, transaction = tmp_path / "store", tmp_path / "transaction"
    transaction.mkdir()
    gc.ensure_epoch(root)
    view = _view(root, "original")
    foreign = _manifest(root, "d", [view])
    with pytest.raises(ValueError, match="outside"):
        gc.pin_transaction(root, **_owner(transaction), manifest_paths=[foreign])
    backup = transaction / "backup.json"
    backup.write_bytes(foreign.read_bytes())
    key = gc.pin_transaction(root, **_owner(transaction), manifest_paths=[backup])
    path = root / gc.PIN_DIRECTORY / (key + ".json")
    payload = json.loads(path.read_bytes())
    payload["operationId"] = "another-operation"
    path.write_bytes(storage.json_bytes(payload))
    with pytest.raises(ValueError, match="belong"):
        gc.release_transaction_pin(root, **_owner(transaction))
    assert path.exists()


def test_directory_bounds_and_unknown_pin_entries_defer_cleanup(tmp_path, monkeypatch):
    gc.ensure_epoch(tmp_path)
    orphan = _view(tmp_path, "orphan")
    directory = tmp_path / gc.PIN_DIRECTORY
    directory.mkdir()
    (directory / "unknown-future-format.json").write_text("{}")
    with pytest.raises(ValueError, match="unknown"):
        gc.collect_unreferenced_pages(tmp_path)
    (directory / "unknown-future-format.json").unlink()
    for index in range(2):
        (directory / (str(index) * 64 + ".json")).write_text("{}")
    monkeypatch.setattr(gc, "MAX_PINS", 1)
    with pytest.raises(ValueError, match="bound"):
        gc.collect_unreferenced_pages(tmp_path)
    assert _page(tmp_path, orphan).exists()


def test_interrupted_atomic_pin_write_does_not_prevent_future_cleanup(tmp_path):
    gc.ensure_epoch(tmp_path)
    orphan = _view(tmp_path, "orphan")
    directory = tmp_path / gc.PIN_DIRECTORY
    directory.mkdir()
    temporary = directory / ("." + "a" * 64 + ".json.tmp-" + "b" * 32)
    temporary.write_text("partial")
    assert gc.collect_unreferenced_pages(tmp_path)["deletedPages"] == 1
    assert temporary.read_text() == "partial"
    assert not _page(tmp_path, orphan).exists()


def test_missing_referenced_page_prevents_collecting_unrelated_orphan(tmp_path):
    gc.ensure_epoch(tmp_path)
    live, orphan = _view(tmp_path, "live"), _view(tmp_path, "orphan")
    _manifest(tmp_path, "a", [live])
    _page(tmp_path, live).unlink()
    with pytest.raises(ValueError, match="missing"):
        gc.collect_unreferenced_pages(tmp_path)
    assert _page(tmp_path, orphan).exists()


def test_redirect_detection_is_enforced_even_without_os_symlink_privilege(tmp_path, monkeypatch):
    gc.ensure_epoch(tmp_path)
    orphan = _view(tmp_path, "orphan")
    original = storage._redirected
    monkeypatch.setattr(storage, "_redirected", lambda path: path == tmp_path / storage.DIRECTORY
                        or original(path))
    with pytest.raises(ValueError, match="redirected"):
        gc.collect_unreferenced_pages(tmp_path)
    assert _page(tmp_path, orphan).exists()


@pytest.mark.parametrize("redirect", [storage.DIRECTORY, gc.PIN_DIRECTORY, gc.EPOCH_FILE])
def test_redirected_cleanup_locations_never_follow_outside_root(tmp_path, redirect):
    root, outside = tmp_path / "store", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "target" if redirect == gc.EPOCH_FILE else outside
    if redirect == gc.EPOCH_FILE:
        target.write_text("{}")
    try:
        (root / redirect).symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("creating symlinks is unavailable on this Windows installation")
    sentinel = outside / "keep.json"
    sentinel.write_text("keep")
    with pytest.raises(ValueError, match="redirected"):
        gc.collect_unreferenced_pages(root)
    assert sentinel.read_text() == "keep"
