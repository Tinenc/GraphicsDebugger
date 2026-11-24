import hashlib
import importlib.util
import json
import os
import time
from pathlib import Path
import zipfile

import pytest

SPEC = importlib.util.spec_from_file_location("restore_dependencies", Path(__file__).parents[1] / "restore_dependencies.py")
restore = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore)


def sealed(tmp_path, entries=None):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    entries = entries or {"tool/license.txt": b"original notice", "tool/code.py": b"print('tool')\n"}
    archive = inputs / "tools.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        for name, data in entries.items():
            stream.writestr(name, data)
    record = {"id": "tools", "path": "tools.zip", "size": archive.stat().st_size,
        "sha256": restore.sha(archive), "files": [{"path": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()}
            for n, d in entries.items()]}
    return inputs, record


def test_restore_is_reusable_and_preserves_sealed_inputs(tmp_path):
    inputs, record = sealed(tmp_path)
    output = tmp_path / "build"
    result = restore.restore(inputs, output, record)
    original_mtime = (result / "tool/code.py").stat().st_mtime_ns
    assert restore.restore(inputs, output, record) == result
    assert (result / "tool/code.py").stat().st_mtime_ns == original_mtime
    assert restore.sha(inputs / record["path"]) == record["sha256"]
    assert set(p.name for p in inputs.iterdir()) == {"tools.zip"}


def test_modified_output_is_repaired_with_previous_generation_preserved(tmp_path):
    inputs, record = sealed(tmp_path)
    result = restore.restore(inputs, tmp_path / "build", record)
    (result / "tool/code.py").write_text("damaged")
    assert restore.restore(inputs, tmp_path / "build", record) == result
    assert (result / "tool/code.py").read_bytes() == b"print('tool')\n"
    previous = list(result.parent.glob("*.damaged-*"))
    assert len(previous) == 1 and (previous[0] / "tool/code.py").read_text() == "damaged"


def test_changed_input_cannot_overwrite_working_output(tmp_path):
    inputs, record = sealed(tmp_path)
    result = restore.restore(inputs, tmp_path / "build", record)
    (inputs / record["path"]).write_bytes(b"wrong input")
    with pytest.raises(ValueError, match="input changed"):
        restore.restore(inputs, tmp_path / "build", record)
    assert (result / "tool/code.py").read_bytes() == b"print('tool')\n"


def test_unlisted_output_files_cannot_enter_a_package(tmp_path):
    inputs, record = sealed(tmp_path)
    result = restore.restore(inputs, tmp_path / "build", record)
    (result / "unexpected-client.exe").write_bytes(b"unlisted")
    restore.restore(inputs, tmp_path / "build", record)
    assert not (result / "unexpected-client.exe").exists()


@pytest.mark.parametrize("entries", [
    {"../outside": b"x"}, {"/absolute": b"x"}, {"C:/absolute": b"x"},
    {"dir\\file": b"x"}, {"tool/A": b"x", "tool/a": b"y"},
])
def test_unsafe_archive_paths_are_rejected_before_extraction(tmp_path, entries):
    inputs, record = sealed(tmp_path, entries)
    with pytest.raises(ValueError):
        restore.restore(inputs, tmp_path / "build", record)
    assert not (tmp_path / "outside").exists()


def test_lfs_pointer_gives_actionable_diagnostic(tmp_path):
    inputs, record = sealed(tmp_path)
    (inputs / record["path"]).write_text("version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 10000\n")
    with pytest.raises(ValueError, match="complete source checkout"):
        restore.verify_archive(inputs, record)


def split_input(inputs, record):
    data = (inputs / record["path"]).read_bytes()
    parts = []
    for index, chunk in enumerate((data[:len(data)//2], data[len(data)//2:])):
        name = f"archives/tools/{index}.part"
        part = inputs / name
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(chunk)
        parts.append({"path": name, "size": len(chunk), "sha256": hashlib.sha256(chunk).hexdigest()})
    (inputs / record["path"]).unlink()
    return dict(record, parts=parts), data


@pytest.mark.parametrize("legacy_pointer", [False, True])
def test_missing_archive_is_assembled_from_checkout_without_network(tmp_path, legacy_pointer):
    inputs, record = sealed(tmp_path)
    record, data = split_input(inputs, record)
    if legacy_pointer:
        (inputs / record["path"]).write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 10000\n")
    assert restore.verify_archive(inputs, record).read_bytes() == data
    assert not list(inputs.glob(".bread-archive-*"))
    assert restore.restore(inputs, tmp_path / "build", record).joinpath("tool/code.py").read_bytes() == b"print('tool')\n"


@pytest.mark.parametrize("damage", ["missing", "truncated", "same_size"])
def test_invalid_part_cannot_publish_archive_or_overwrite_pointer(tmp_path, damage):
    inputs, record = sealed(tmp_path)
    record, data = split_input(inputs, record)
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 10000\n"
    (inputs / record["path"]).write_bytes(pointer)
    part = inputs / record["parts"][0]["path"]
    if damage == "missing":
        part.unlink()
    else:
        part.write_bytes(b"x" * (part.stat().st_size - (damage == "truncated")))
    with pytest.raises(ValueError, match="dependency part|part hash"):
        restore.verify_archive(inputs, record)
    assert (inputs / record["path"]).read_bytes() == pointer
    assert not list(inputs.glob(".bread-archive-*"))


@pytest.mark.parametrize("damage", ["duplicate", "escape", "archive_hash"])
def test_invalid_parts_inventory_is_rejected(tmp_path, damage):
    inputs, record = sealed(tmp_path)
    record, data = split_input(inputs, record)
    if damage == "duplicate":
        record["parts"][1]["path"] = record["parts"][0]["path"]
    elif damage == "escape":
        record["parts"][0]["path"] = "../outside"
    else:
        record["sha256"] = "f" * 64
    with pytest.raises(ValueError):
        restore.verify_archive(inputs, record)
    assert not (inputs / record["path"]).exists()
    assert not list(inputs.glob(".bread-archive-*"))


def test_sealing_roundtrip_keeps_locked_archive_and_file_inventory(tmp_path):
    from seal_dependency_parts import seal
    inputs, record = sealed(tmp_path)
    (inputs / "inputs.lock.json").write_text(json.dumps({"schema": "bread-build-inputs/v1", "archives": [record]}))
    sealed_record = seal(inputs, 64)["archives"][0]
    assert sealed_record["sha256"] == record["sha256"]
    assert sealed_record["files"] == record["files"]
    original = (inputs / record["path"]).read_bytes()
    (inputs / record["path"]).unlink()
    assert restore.verify_archive(inputs, sealed_record).read_bytes() == original


def test_extract_cannot_write_into_dependency_inputs(tmp_path):
    inputs, record = sealed(tmp_path)
    with pytest.raises(ValueError, match="inside sealed inputs"):
        restore.restore(inputs, inputs / "build", record)


def test_legacy_explicit_config_remains_usable():
    assert restore.standard_capabilities({"standardCapabilitiesDirectory": "old/bundle"}) == restore.REPO / "old/bundle"


def test_receipt_skips_hashes_but_full_verification_bypasses_it(tmp_path, monkeypatch):
    inputs, record = sealed(tmp_path)
    output = tmp_path / "build"
    result = restore.restore(inputs, output, record)
    assert len(result.name) == 20
    time.sleep(2.05)
    restore.restore(inputs, output, record)  # Upgrade the recent-file receipt after settling.
    original = restore.verify_tree
    calls = []
    def measured(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(restore, "verify_tree", measured)
    restore.restore(inputs, output, record)
    assert not calls
    restore.restore(inputs, output, record, full_verify=True)
    assert calls == [1]
    path = result / "tool/code.py"
    info = path.stat()
    path.write_bytes(b"x" * info.st_size)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    restore.restore(inputs, output, record)
    assert path.read_bytes() == b"print('tool')\n"


def test_old_full_hash_directory_is_preserved(tmp_path):
    inputs, record = sealed(tmp_path)
    output = tmp_path / "build"
    short = restore.restore(inputs, output, record)
    legacy = short.with_name(record["sha256"])
    short.rename(legacy)
    assert restore.restore(inputs, output, record) == legacy


@pytest.mark.parametrize("value", ["{broken", "[]", "null"])
def test_damaged_receipt_falls_back_to_full_hash_verification(tmp_path, value):
    inputs, record = sealed(tmp_path)
    output = tmp_path / "build"
    result = restore.restore(inputs, output, record)
    result.with_suffix(".ready.json").write_text(value)
    assert restore.restore(inputs, output, record) == result
    assert (result / "tool/code.py").read_bytes() == b"print('tool')\n"
