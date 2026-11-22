"""Validated project reads reuse bytes only while every consumed file is unchanged."""
import json
import os
from unittest import mock

import pytest

from src import project_read_cache, projects
from tests.test_projects import ReplayProjectTests, _dxbc


@pytest.fixture
def case():
    value = ReplayProjectTests()
    value.setUp()
    catalog, shader, capture = value._catalog("cache", _dxbc(), resource_id=91)
    value.service.import_capture(catalog)
    status = value._workspace_status("cache-source", capture, shader, "// original source\n")
    value.record = value.service.register_workspace(status)["knowledge"]
    value.service = projects.ReplayProjectService(value.project, cache_validated_reads=True)
    yield value
    value.tearDown()


def test_warm_read_avoids_file_reads_and_returns_independent_value(case):
    first = case.service._load()
    first["name"] = "caller edit"
    with mock.patch.object(projects, "_file_bytes", wraps=projects._file_bytes) as reads:
        second = case.service._load()
    assert reads.call_count == 0
    assert second["name"] != "caller edit"


def test_same_size_source_change_with_restored_mtime_invalidates_success(case):
    case.service._load()
    path = case.project / case.record["sourcePath"]
    stamp = path.stat()
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b"original", b"modified"))
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises(projects.ReplayProjectError, match="hash mismatch"):
        case.service._load()


def test_removed_artifact_does_not_inherit_previous_validity(case):
    case.service._load()
    (case.project / case.record["analysisPath"]).unlink()
    with pytest.raises(projects.ReplayProjectError, match="regular"):
        case.service._load()


def test_manifest_change_invalidates_and_legacy_read_does_not_skip_persistence(case):
    path = case.service.manifest_path
    value = json.loads(path.read_text(encoding="utf-8"))
    value.pop("activeHeads")
    path.write_text(json.dumps(value), encoding="utf-8")
    case.service._load(persist_migrations=False)
    assert "activeHeads" not in json.loads(path.read_text(encoding="utf-8"))
    case.service._load(persist_migrations=True)
    assert "activeHeads" in json.loads(path.read_text(encoding="utf-8"))


def test_unavailable_change_identity_uses_ordinary_validator(case, monkeypatch):
    monkeypatch.setattr(project_read_cache, "file_stamp", lambda path: None)
    case.service._load()
    with mock.patch.object(projects, "_file_bytes", wraps=projects._file_bytes) as reads:
        case.service._load()
    assert reads.call_count > 0


def test_alias_byte_requests_use_ordinary_reader(case):
    case.service._load()
    with mock.patch.object(projects, "_file_bytes", wraps=projects._file_bytes) as reads:
        case.service._load(alias_artifact_cache={})
    assert reads.call_count > 0
