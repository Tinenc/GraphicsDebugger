from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import zipfile

import pytest
from replaylab_agent.capability_store import ensure_component, declared_file, _native


def component(root, files, *, bad_member=False):
    root.mkdir(parents=True, exist_ok=True)
    archive = root / "python.zip"
    records = [{"path": name, "size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
               for name, value in files.items()]
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_LZMA) as z:
        for name, value in files.items():
            z.writestr(name, value)
    metadata = {"files": records, "componentArchives": [{"id": "python", "path": archive.name,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}]}
    if bad_member:
        records[0]["sha256"] = "0"*64
    return metadata


def test_compressed_runtime_is_available_without_expanding_at_startup(tmp_path):
    meta = component(tmp_path, {"python/python.exe": b"runtime", "python/pkg/data.txt": b"data"})
    assert declared_file(tmp_path, meta, "python/python.exe")
    assert not (tmp_path / "python").exists()
    assert not declared_file(tmp_path, meta, "python/missing")
    with ThreadPoolExecutor(max_workers=3) as pool:
        paths = list(pool.map(lambda _: ensure_component(tmp_path, meta, "python"), range(3)))
    assert paths == [tmp_path / "python"]*3
    assert (paths[0] / "pkg/data.txt").read_bytes() == b"data"
    assert list((tmp_path / ".runtime-state").iterdir()) == [tmp_path / ".runtime-state/python.lock"]
    assert (paths[0] / ".bread-runtime.json").is_file()


def test_archive_or_member_corruption_keeps_existing_tools_intact(tmp_path):
    meta = component(tmp_path, {"python/python.exe": b"runtime"}, bad_member=True)
    old = tmp_path / "python"
    old.mkdir(); (old / "user.txt").write_text("retained")
    with pytest.raises(ValueError, match="member checksum"):
        ensure_component(tmp_path, meta, "python")
    assert (old / "user.txt").read_text() == "retained"
    assert not (old / "python.exe").exists()
    assert not (old / ".bread-runtime.json").exists()
    meta["componentArchives"][0]["sha256"] = "0"*64
    with pytest.raises(ValueError, match="archive checksum"):
        ensure_component(tmp_path, meta, "python")


def test_archive_traversal_never_creates_an_outside_file(tmp_path):
    meta = component(tmp_path, {"python/../escape.txt": b"bad"})
    with pytest.raises(ValueError, match="archive path"):
        ensure_component(tmp_path, meta, "python")
    assert not (tmp_path / "escape.txt").exists()


def test_long_member_names_expand_through_the_owned_long_path_adapter(tmp_path):
    name = "python/" + "/".join(["long-directory-name"] * 12) + "/data.txt"
    meta = component(tmp_path, {name: b"long-path-data"})
    ensure_component(tmp_path, meta, "python")
    assert _native(tmp_path / name).read_bytes() == b"long-path-data"
