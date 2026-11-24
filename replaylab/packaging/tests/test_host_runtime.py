from __future__ import annotations

import json
import zipfile

import pytest

from host_runtime import HOSTS, MANIFEST, SUPPORT, digest, merge_support, publish, runtime_paths, support_records


def _library(path, timestamp, payload):
    with zipfile.ZipFile(path, "w") as library:
        library.writestr(zipfile.ZipInfo("encodings/__init__.pyc", timestamp), payload)


def test_shared_support_only_accepts_equivalent_duplicate_files(tmp_path):
    left, right = tmp_path / "agent", tmp_path / "mcp"
    for host in (left, right):
        support = host / SUPPORT
        support.mkdir(parents=True)
        (support / "python312.dll").write_bytes(b"same DLL")
    _library(left / SUPPORT / "base_library.zip", (2025, 1, 1, 0, 0, 0), b"module")
    _library(right / SUPPORT / "base_library.zip", (2026, 1, 1, 0, 0, 0), b"module")
    combined = tmp_path / "combined"
    merge_support(combined, [left, right])
    assert {item.name for item in combined.iterdir()} == {"base_library.zip", "python312.dll"}

    _library(right / SUPPORT / "base_library.zip", (2026, 1, 1, 0, 0, 0), b"different")
    with pytest.raises(ValueError, match="disagree on support file: base_library.zip"):
        merge_support(tmp_path / "rejected", [left, right])


def _stage(root, payload):
    (root / SUPPORT).mkdir(parents=True)
    files = []
    for name in HOSTS:
        file = root / name
        file.write_bytes(payload)
        files.append({"path": name, "size": file.stat().st_size, "sha256": digest(file)})
    (root / SUPPORT / "python312.dll").write_bytes(payload)
    manifest = {"schema": "replaylab-runtime-output/v1", "layout": "onedir",
                "files": files + support_records(root)}
    (root / MANIFEST).write_text(json.dumps(manifest))
    return manifest


def test_publish_replaces_complete_runtime_and_preserves_unowned_files(tmp_path):
    stage = tmp_path / "first"
    first = _stage(stage, b"generation one")
    destination = tmp_path / "output"
    publish(stage, destination)
    assert len(runtime_paths(destination, first)) == len(HOSTS) + 1

    next_stage = tmp_path / "second"
    second = _stage(next_stage, b"generation two")
    publish(next_stage, destination)
    assert len(runtime_paths(destination, second)) == len(HOSTS) + 1

    unowned = destination / SUPPORT / "foreign.dll"
    unowned.write_bytes(b"unowned")
    with pytest.raises(ValueError, match="exact manifest"):
        publish(stage, destination)
    assert unowned.read_bytes() == b"unowned"
    assert (destination / HOSTS[0]).read_bytes() == b"generation two"
