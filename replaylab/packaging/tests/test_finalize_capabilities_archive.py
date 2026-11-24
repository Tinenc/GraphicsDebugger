from pathlib import Path
import hashlib
import importlib.util
import json
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import prepare_capabilities_bundle as bundle
from finalize_capabilities_archive import finalize, digest


def base_release(tmp_path, extra_files=None):
    source = tmp_path / "caps"
    required = ["python/python.exe", "node/bin/node.exe", "python/Lib/site-packages/docx/__init__.py",
                "python/Lib/site-packages/openpyxl/__init__.py", "python/Lib/site-packages/pptx/__init__.py",
                "python/Lib/site-packages/pypdf/__init__.py"]
    for name in required:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test dependency")
    metadata = {"schema": "bread-portable-capabilities/v1", "plugins": [], "skillRoots": []}
    bundle.seal(source, metadata)
    files = {"replaylab/tools/capabilities/" + p.relative_to(source).as_posix(): p.read_bytes()
             for p in source.rglob("*") if p.is_file()}
    files["replaylab/tools/capabilities/office/__pycache__/runtime.pyc"] = b"generated cache"
    files.update(extra_files or {})
    records = [{"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for path, data in files.items()]
    base = tmp_path / "base.zip"
    with zipfile.ZipFile(base, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items():
            archive.writestr("RenderDocBread/" + path, data)
        archive.writestr("RenderDocBread/RenderDocBread.manifest.json", json.dumps({"files": records}))
    base.with_suffix(".zip.sha256").write_text(digest(base))
    return base, source, metadata


def test_finalization_preserves_validated_dependencies_and_base(tmp_path):
    base, source, metadata = base_release(tmp_path)
    before = digest(base)
    adapter = source / "bread-adapters/example/SKILL.md"
    adapter.parent.mkdir(parents=True)
    adapter.write_text("application adapter", encoding="utf-8")
    bundle.seal(source, metadata)
    final = tmp_path / "final.zip"
    receipt = finalize(base, source, final)
    assert digest(base) == before
    assert receipt["omittedRuntimeCaches"] == 1
    with zipfile.ZipFile(final) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert not any("__pycache__" in name for name in names)
        manifest = json.loads(archive.read("RenderDocBread/RenderDocBread.manifest.json"))
        for record in manifest["files"]:
            assert hashlib.sha256(archive.read("RenderDocBread/" + record["path"])).hexdigest() == record["sha256"]


def test_finalization_rejects_private_skill_overlay(tmp_path):
    base, source, metadata = base_release(tmp_path)
    snapshot = source / "user-skill-snapshots/example/SKILL.md"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("personal procedure", encoding="utf-8")
    bundle.seal(source, metadata)
    output = tmp_path / "final.zip"
    with pytest.raises(ValueError, match="Unapproved skill"):
        finalize(base, source, output)
    assert not output.exists()


def test_finalization_rejects_a_changed_runtime_dependency(tmp_path):
    base, source, metadata = base_release(tmp_path)
    (source / "python/python.exe").write_bytes(b"changed runtime")
    bundle.seal(source, metadata)
    with pytest.raises(ValueError, match="cannot change"):
        finalize(base, source, tmp_path / "must-not-exist.zip")


def test_matching_runtime_and_first_party_update_preserve_dependencies_and_filter_private_files(tmp_path, monkeypatch):
    import runtime_inputs
    monkeypatch.setattr(runtime_inputs, "fingerprint", lambda: "current-source")
    base, caps, _ = base_release(tmp_path, {"integrations/replaylab/python/renderdoc_mcp_bridge/removed.py": b"old"})
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    records = []
    for name in ("replaylab-ai.exe", "BreadMCP.exe", "ReplayLabInternal.exe", "ReplayLabShaderTools.exe"):
        path = runtime / name
        path.write_bytes(name.encode())
        records.append({"path": name, "size": path.stat().st_size, "sha256": digest(path)})
    (runtime / "replaylab-runtime-manifest.json").write_text(json.dumps(
        {"version": "test", "sourceInputsSha256": "current-source", "files": records}))
    repo = tmp_path / "repo"
    for relative in ("replaylab/renderdoc_mcp_bridge", ".agents/skills", "replaylab/portable_skills", "replaylab/unity"):
        path = repo / relative
        path.mkdir(parents=True)
        (path / "current.txt").write_text(relative)
        (path / "profiles.json").write_text('"private"')
    output = tmp_path / "final.zip"
    # The base checksum proves its unchanged dependencies, independently of
    # any now-modified local tool cache.
    (caps / "python/python.exe").write_bytes(b"changed local cache")
    receipt = finalize(base, None, output, runtime, repo)
    assert receipt["omittedRuntimeCaches"] == 1
    assert receipt["removedFirstPartyFiles"] == 1
    with zipfile.ZipFile(output) as archive:
        assert not any(name.endswith(("removed.py", "profiles.json")) for name in archive.namelist())
        assert archive.read("RenderDocBread/replaylab/unity/current.txt") == b"replaylab/unity"
        manifest = json.loads(archive.read("RenderDocBread/RenderDocBread.manifest.json"))
        for record in manifest["files"]:
            assert hashlib.sha256(archive.read("RenderDocBread/" + record["path"])).hexdigest() == record["sha256"]
    with pytest.raises(ValueError, match="matching frozen runtime"):
        finalize(base, None, tmp_path / "invalid.zip", first_party_source=repo)
