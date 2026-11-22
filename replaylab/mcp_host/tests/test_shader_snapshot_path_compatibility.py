"""Read old v1 snapshot keys without confusing foreign document identities."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from tests.test_shader_visual_snapshots import SOURCE, make, snapshots


def identity(workspace="//server/共享/角色", path=None):
    return {"workspace": workspace, "path": path or workspace + "/rim.hlsl",
            "stage": "Pixel", "originalShader": "a" * 64}


def legacy_file(root, stored_identity, records=None):
    if records is None:
        records = [make(identity=stored_identity, name="轮廓光"),
                   make(identity=stored_identity, name="高光", needle="before + 0.25")]
    records = copy.deepcopy(records)
    for record in records:
        record["identity"] = copy.deepcopy(stored_identity)
        record["sourcePath"] = stored_identity["path"]
    value = {"schema": snapshots.SCHEMA, "identity": stored_identity, "snapshots": records}
    digest = hashlib.sha256(json.dumps(stored_identity, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode("utf-8")).hexdigest()
    path = root / "shader-visual-snapshots" / (digest + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path, records


@pytest.mark.parametrize("workspace", [
    "//SERVER/共享/角色", "\\\\SERVER\\共享\\角色", "\\\\?\\UNC\\SERVER\\共享\\角色",
    "//?/UNC/SERVER/共享/角色", "//server/共享/角色/.",
])
def test_unc_spellings_have_one_platform_independent_identity(workspace):
    assert snapshots.normalize_identity(identity(workspace)) == identity()


@pytest.mark.parametrize("workspace", ["D:/抓帧/角色", "d:\\抓帧\\角色", "\\\\?\\D:\\抓帧\\角色"])
def test_drive_extended_spelling_retains_existing_normal_identity(workspace):
    assert snapshots.normalize_identity(identity(workspace)) == identity("d:/抓帧/角色")


@pytest.mark.parametrize("workspace", ["remote://linux-host/home/角色", "adb://serial-01/storage/角色"])
def test_remote_locator_is_opaque_and_is_not_a_local_path(workspace):
    actual = snapshots.normalize_identity(identity(workspace))
    assert actual == identity(workspace)
    assert actual != snapshots.normalize_identity(identity(workspace.replace("://", ":/")))
    assert actual != snapshots.normalize_identity(identity(workspace + "/../角色"))


def test_linux_case_sensitive_paths_remain_separate():
    assert snapshots.normalize_identity(identity("/home/Shader")) != snapshots.normalize_identity(identity("/home/shader"))


def test_ordinary_new_local_store_does_not_scan_unrelated_snapshot_history(tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail("ordinary local snapshot lookup scanned unrelated history")
    monkeypatch.setattr(snapshots.os, "listdir", forbidden)
    assert snapshots.SnapshotStore(tmp_path, identity("D:/角色")).list() == []
    assert snapshots.SnapshotStore(tmp_path, identity("/home/角色")).list() == []


def test_old_extended_drive_key_is_found_without_directory_scan(tmp_path, monkeypatch):
    old, rows = legacy_file(tmp_path, identity("//?/d:/角色"))
    monkeypatch.setattr(snapshots.os, "listdir", lambda *a: pytest.fail("unnecessary directory scan"))
    store = snapshots.SnapshotStore(tmp_path, identity("D:/角色"))
    assert [r["id"] for r in store.list()] == [r["id"] for r in rows]
    store.rename(rows[0]["id"], "新版名称")
    assert not old.exists() and Path(store.path).exists()


@pytest.mark.parametrize("stored", [
    "//SERVER/共享/角色", "//?/unc/server/共享/角色", "//?/UNC/SERVER/共享/角色",
])
def test_old_unc_key_is_found_and_upgraded_on_write_without_losing_user_data(tmp_path, stored):
    old, before = legacy_file(tmp_path, identity(stored))
    old_bytes = old.read_bytes()
    store = snapshots.SnapshotStore(tmp_path, identity())
    assert Path(store.path) != old
    loaded = store.list()
    assert not Path(store.path).exists() and old.read_bytes() == old_bytes
    assert [r["name"] for r in loaded] == ["轮廓光", "高光"]
    assert [r["anchor"] for r in loaded] == [r["anchor"] for r in before]
    assert [r["options"] for r in loaded] == [r["options"] for r in before]
    for record in loaded:
        assert snapshots.resolve_snapshot(record, SOURCE, identity())["status"] == "resolved"
    store.rename(loaded[0]["id"], "新的轮廓光")
    assert Path(store.path).exists() and not old.exists()
    backups = list(old.parent.glob(old.name + ".legacy-*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == old_bytes
    reopened = snapshots.SnapshotStore(tmp_path, identity("\\\\?\\UNC\\SERVER\\共享\\角色"))
    rows = reopened.list()
    assert [r["name"] for r in rows] == ["新的轮廓光", "高光"]
    assert [r["anchor"] for r in rows] == [r["anchor"] for r in before]
    reopened.reorder([rows[1]["id"], rows[0]["id"]])
    reopened.delete(rows[0]["id"])
    assert [r["id"] for r in store.list()] == [rows[1]["id"]]


@pytest.mark.parametrize("workspace", ["remote://LinuxHost/home/角色", "adb://serial-01/storage/角色"])
def test_old_collapsed_remote_key_is_rebound_only_to_exact_request_and_original(tmp_path, workspace):
    current = identity(workspace)
    collapsed = {k: (v.replace("://", ":/") if k in {"workspace", "path"} else v)
                 for k, v in current.items()}
    old, before = legacy_file(tmp_path, collapsed)
    store = snapshots.SnapshotStore(tmp_path, current)
    loaded = store.list()
    assert len(loaded) == 2
    assert loaded[0]["sourcePath"] == current["path"]
    assert snapshots.resolve_snapshot(loaded[0], SOURCE, current)["status"] == "resolved"
    store.rename(before[0]["id"], "中文远端快照")
    assert not old.exists()
    assert snapshots.SnapshotStore(tmp_path, current).list()[0]["name"] == "中文远端快照"
    assert snapshots.SnapshotStore(tmp_path, dict(current, originalShader="b" * 64)).list() == []


@pytest.mark.parametrize("field,value", [
    ("workspace", "//other/共享/角色"), ("path", "//server/共享/角色/other.hlsl"),
    ("stage", "Vertex"), ("originalShader", "b" * 64),
])
def test_legacy_search_never_adopts_foreign_workspace_document_stage_or_shader(tmp_path, field, value):
    old, unused = legacy_file(tmp_path, identity("//SERVER/共享/角色"))
    before = old.read_bytes()
    target = dict(identity(), **{field: value})
    store = snapshots.SnapshotStore(tmp_path, target)
    assert store.list() == []
    assert old.read_bytes() == before and not Path(store.path).exists()


def test_old_posix_case_is_not_migrated_to_another_document(tmp_path):
    old, unused = legacy_file(tmp_path, identity("/home/Shader"))
    before = old.read_bytes()
    assert snapshots.SnapshotStore(tmp_path, identity("/home/shader")).list() == []
    assert old.read_bytes() == before


def test_conflicting_legacy_alias_files_are_not_merged_or_discarded(tmp_path):
    first, rows = legacy_file(tmp_path, identity("//SERVER/共享/角色"))
    rows[0]["name"] = "另一份编辑"
    second, unused = legacy_file(tmp_path, identity("//?/unc/server/共享/角色"), rows)
    first_bytes, second_bytes = first.read_bytes(), second.read_bytes()
    store = snapshots.SnapshotStore(tmp_path, identity())
    with pytest.raises(snapshots.SnapshotError, match="conflicting legacy identities"):
        store.list()
    assert first.read_bytes() == first_bytes and second.read_bytes() == second_bytes
    assert not Path(store.path).exists()


def test_identical_legacy_alias_copies_preserve_one_ordered_set_and_both_backups(tmp_path):
    first, rows = legacy_file(tmp_path, identity("//SERVER/共享/角色"))
    second, unused = legacy_file(tmp_path, identity("//?/unc/server/共享/角色"), rows)
    store = snapshots.SnapshotStore(tmp_path, identity())
    assert len(store.list()) == 2
    store.rename(rows[0]["id"], "合并同内容索引")
    assert not first.exists() and not second.exists()
    assert len(list(first.parent.glob("*.bak"))) == 2
    assert [r["id"] for r in store.list()] == [r["id"] for r in rows]


def test_failed_canonical_publication_preserves_legacy_bytes(tmp_path, monkeypatch):
    old, rows = legacy_file(tmp_path, identity("//SERVER/共享/角色"))
    before = old.read_bytes()
    store = snapshots.SnapshotStore(tmp_path, identity())
    def fail(*args):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(snapshots.os, "replace", fail)
    with pytest.raises(snapshots.SnapshotError, match="snapshot-store-write-failed"):
        store.rename(rows[0]["id"], "未提交修改")
    assert old.read_bytes() == before and not Path(store.path).exists()
    assert not list(old.parent.glob(".snapshot-*"))


def test_migration_obeys_the_legacy_writer_lock(tmp_path, monkeypatch):
    old, rows = legacy_file(tmp_path, identity("//SERVER/共享/角色"))
    before = old.read_bytes()
    store = snapshots.SnapshotStore(tmp_path, identity())
    monkeypatch.setattr(snapshots, "LOCK_TIMEOUT_SECONDS", 0.02)
    with store._path_lock(str(old)):
        with pytest.raises(snapshots.SnapshotError, match="snapshot-store-busy"):
            store.rename(rows[0]["id"], "未提交修改")
    assert old.read_bytes() == before and not Path(store.path).exists()


def test_bound_corrupt_legacy_record_is_not_overwritten(tmp_path):
    old, unused = legacy_file(tmp_path, identity("//SERVER/共享/角色"))
    value = json.loads(old.read_text(encoding="utf-8"))
    value["snapshots"][0]["anchor"]["startToken"] = -1
    old.write_text(json.dumps(value), encoding="utf-8")
    before = old.read_bytes()
    with pytest.raises(snapshots.SnapshotError, match="snapshot-invalid-record"):
        snapshots.SnapshotStore(tmp_path, identity()).list()
    assert old.read_bytes() == before


@pytest.mark.parametrize("contents", ["null", "[]", "{}", '"old invalid value"'])
def test_existing_invalid_canonical_json_is_not_treated_as_an_empty_store(tmp_path, contents):
    store = snapshots.SnapshotStore(tmp_path, identity())
    path = Path(store.path)
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(snapshots.SnapshotError, match="snapshot-store-invalid"):
        store.list()
    with pytest.raises(snapshots.SnapshotError, match="snapshot-store-invalid"):
        store.add(make(identity=identity()))
    assert path.read_text(encoding="utf-8") == contents


@pytest.mark.parametrize("contents", ["null", "[]", "{}"])
def test_known_legacy_uri_key_with_invalid_json_remains_an_explicit_error(tmp_path, contents):
    old, unused = legacy_file(tmp_path, identity("remote:/host/角色"))
    old.write_text(contents, encoding="utf-8")
    store = snapshots.SnapshotStore(tmp_path, identity("remote://host/角色"))
    with pytest.raises(snapshots.SnapshotError, match="snapshot-store-invalid"):
        store.list()
    assert old.read_text(encoding="utf-8") == contents and not Path(store.path).exists()
