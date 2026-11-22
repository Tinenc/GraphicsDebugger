"""An immutable published export can be corrected without replaying its capture."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src import resource_export
from tests import test_unity_restore as restore_fixture


def _snapshot(directory: Path):
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


@pytest.fixture
def original(tmp_path, monkeypatch):
    root = tmp_path / "Managed"
    package = restore_fixture.finalized_package(root)
    manager = resource_export.ResourceExportManager(root)
    operation_id = package.parent.name
    state, capture, bundle = manager._validated_capture(operation_id)

    def replay_forbidden(*args, **kwargs):
        raise AssertionError("forking must not access the replay bridge or its GPU driver")

    monkeypatch.setattr(restore_fixture, "load_resource_export", replay_forbidden)
    monkeypatch.setattr(restore_fixture._MeshController, "__init__", replay_forbidden)
    return manager, operation_id, state["capture"]["manifestSha256"], package, capture, bundle


def test_fork_reuses_physical_bytes_and_allows_corrected_finalize(original):
    manager, source_id, source_sha, package, capture, original_bundle = original
    untouched = _snapshot(package.parent)
    result = manager.fork_capture(source_id, source_sha, "fix-material")
    child_id = result["operationId"]
    state, copied, bundle = manager._validated_capture(child_id)
    assert child_id != source_id
    assert state["status"] == "captured"
    assert result["schema"] == "bread-resource-export-fork/v1"
    assert result["needsCapture"] is False and result["replayPerformed"] is False
    assert result["idempotent"] is False
    assert result["bundleSha256"] != source_sha
    assert bundle["ticketId"] == child_id
    assert bundle["requestSha256"] == state["requestSha256"] != original_bundle["requestSha256"]
    for field in ("captureId", "captureSessionId", "eventId", "instance", "view", "mesh", "files", "completion"):
        assert bundle.get(field) == original_bundle.get(field)
    assert bundle["captureReuse"]["sourceOperationId"] == source_id
    assert bundle["captureOrigin"]["bundleSha256"] == source_sha
    for record in bundle["files"]:
        assert (copied / record["path"]).read_bytes() == (capture / record["path"]).read_bytes()
        assert not os.path.samefile(copied / record["path"], capture / record["path"])

    spec = json.loads((package / "transfer-spec.json").read_text(encoding="utf-8"))
    spec["assetName"] = "BreadCorrectedMaterial"
    with pytest.raises(resource_export.ResourceExportError, match="different transfer specification"):
        manager.finalize(source_id, source_sha, spec)
    finalized = manager.finalize(child_id, result["bundleSha256"], spec)
    assert Path(finalized["packagePath"]).name == "target"
    assert json.loads((Path(finalized["packagePath"]) / "transfer-spec.json").read_text())["assetName"] == spec["assetName"]
    assert _snapshot(package.parent) == untouched


def test_stable_revision_key_recovers_response_and_finalized_revision(original):
    manager, source_id, source_sha, package, _, _ = original
    first = manager.fork_capture(source_id, source_sha, "network-retry")
    second = manager.fork_capture(source_id, source_sha, "network-retry")
    assert second["operationId"] == first["operationId"]
    assert second["bundleSha256"] == first["bundleSha256"]
    assert second["idempotent"] is True
    spec = json.loads((package / "transfer-spec.json").read_text())
    manager.finalize(first["operationId"], first["bundleSha256"], spec)
    third = manager.fork_capture(source_id, source_sha, "network-retry")
    assert third["operationId"] == first["operationId"]
    assert third["operation"]["status"] == "finalized"
    assert third["idempotent"] is True


def test_distinct_revisions_and_nested_fork_preserve_capture_origin(original):
    manager, source_id, source_sha, _, _, _ = original
    first = manager.fork_capture(source_id, source_sha, "first-fix")
    other = manager.fork_capture(source_id, source_sha, "second-fix")
    anonymous = [manager.fork_capture(source_id, source_sha) for _ in range(2)]
    assert len({r["operationId"] for r in [first, other, *anonymous]}) == 4
    nested = manager.fork_capture(first["operationId"], first["bundleSha256"], "next-fix")
    _, _, bundle = manager._validated_capture(nested["operationId"])
    assert bundle["captureReuse"]["sourceOperationId"] == first["operationId"]
    assert bundle["captureOrigin"]["operationId"] == source_id
    assert bundle["captureOrigin"]["bundleSha256"] == source_sha


@pytest.mark.parametrize("tamper", ["source-bytes", "source-manifest", "source-request", "wrong-sha"])
def test_fork_refuses_changed_source_before_creating_revision(original, tamper):
    manager, source_id, source_sha, package, capture, bundle = original
    operations_before = {path.name for path in package.parent.parent.iterdir()}
    if tamper == "source-bytes":
        path = capture / bundle["files"][0]["path"]
        data = path.read_bytes()
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    elif tamper == "source-manifest":
        path = capture / "manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif tamper == "source-request":
        path = package.parent / "operation.json"
        state = json.loads(path.read_text())
        state["request"]["eventId"] += 1
        path.write_text(json.dumps(state), encoding="utf-8")
    else:
        source_sha = "0" * 64
    with pytest.raises(resource_export.ResourceExportError):
        manager.fork_capture(source_id, source_sha, "bad-source")
    assert {path.name for path in package.parent.parent.iterdir()} == operations_before


def test_retry_refuses_changed_child_bytes(original):
    manager, source_id, source_sha, package, _, _ = original
    untouched = _snapshot(package.parent)
    child = manager.fork_capture(source_id, source_sha, "child-corruption")
    _, capture, bundle = manager._validated_capture(child["operationId"])
    path = capture / bundle["files"][0]["path"]
    path.write_bytes(b"corrupted")
    with pytest.raises(resource_export.ResourceExportError, match="changed after acceptance"):
        manager.fork_capture(source_id, source_sha, "child-corruption")
    assert _snapshot(package.parent) == untouched


def test_interrupted_copy_resumes_without_overwriting_existing_payload(original, monkeypatch):
    manager, source_id, source_sha, package, _, _ = original
    untouched = _snapshot(package.parent)
    original_copy = resource_export.shutil.copyfile
    calls = []

    def fail_second_copy(source, destination, *args, **kwargs):
        calls.append(Path(source).name)
        if len(calls) == 2:
            raise OSError("simulated copy failure")
        return original_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(resource_export.shutil, "copyfile", fail_second_copy)
    with pytest.raises(OSError, match="simulated copy failure"):
        manager.fork_capture(source_id, source_sha, "resume-copy")
    assert _snapshot(package.parent) == untouched
    first_source = calls[0]
    calls.clear()

    def copy_remaining(source, destination, *args, **kwargs):
        calls.append(Path(source).name)
        return original_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(resource_export.shutil, "copyfile", copy_remaining)
    child = manager.fork_capture(source_id, source_sha, "resume-copy")
    assert child["idempotent"] is True
    assert first_source not in calls
    manager._validated_capture(child["operationId"])
    assert _snapshot(package.parent) == untouched
