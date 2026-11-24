from __future__ import annotations

import importlib.util
import json
import hashlib
import os
from unittest import mock
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("bread_package", Path(__file__).parents[1] / "package_portable.py")
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


@pytest.fixture
def native_test_input(tmp_path, monkeypatch):
    # Unit tests exercise the sealed-file exception without requiring large LFS
    # payloads or executing platform-specific DLLs. Real codecs run in release smoke.
    import zipfile
    archive = tmp_path / "native-fixture.zip"
    records = []
    with zipfile.ZipFile(archive, "w") as stream:
        for name in sorted(package.UEX_NATIVE_FILES):
            data = ("fixture " + name).encode()
            stream.writestr(name, data)
            records.append({"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    record = {"id": "uex-native", "path": "native-fixture.zip", "files": records}
    monkeypatch.setattr(package, "uex_native_input", lambda: (record, archive))


def test_copy_excludes_private_state_but_preserves_runtime_and_skills(tmp_path):
    source, target = tmp_path / "source", tmp_path / "relocated with 中文"
    files = ["tool.exe", "licenses/LICENSE.txt", "refs/SKILL.md", "profiles.json", "keys.json",
             ".uex-cache/db.sqlite", "__pycache__/module.pyc", "obj/tool.obj", "tool.pdb", "logs/private.log"]
    for name in files:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("contents", encoding="utf-8")
    package.copy_tree(source, target)
    assert sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()) == [
        "licenses/LICENSE.txt", "refs/SKILL.md", "tool.exe"]


def test_path_audit_distinguishes_download_urls_and_host_paths():
    assert not package.ABSOLUTE.search('https://example.com/tools/file.zip')
    assert not package.ABSOLUTE.search('%BREAD_TOOLS%/unpackers/uex/uex.exe')
    for value in [r'C:\Users\someone\tool.exe', 'D:/Study/tool.exe', r'\\machine\share\tool.exe']:
        assert package.ABSOLUTE.search(value)


def test_smoke_generated_office_caches_are_not_release_files(tmp_path):
    for name in ["office/code.py", "office/__pycache__/code.pyc", "office/code.pyo"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content")
    assert [p.relative_to(tmp_path).as_posix() for p in package.release_files(tmp_path)] == ["office/code.py"]


def test_release_paths_cannot_escape_or_name_root(tmp_path):
    for value in ["..", "../private.json", str(tmp_path)]:
        with pytest.raises(ValueError):
            package.child(tmp_path, value)


def test_receipt_rewrite_preserves_runtime_hashes_and_uses_relative_tools(tmp_path):
    runtime = tmp_path / "replaylab"
    runtime.mkdir()
    manifest = {"shaderTools": {"path": "D:/build/result.exe", "inputSha256": "a" * 64},
                "files": [{"path": "BreadMCP.exe", "sha256": "b" * 64}]}
    package.write_json(runtime / "replaylab-runtime-manifest.json", manifest)
    tools = runtime / "tools/unpackers"
    tools.mkdir(parents=True)
    package.write_json(tools / "manifest.json", {"tools": {"uex": {"program": "D:/build/uex.exe"}}})
    package.normalize_receipts(tmp_path)
    updated = package.read_json(runtime / "replaylab-runtime-manifest.json")
    assert updated["files"] == manifest["files"]
    assert updated["shaderTools"] == {"inputSha256": "a" * 64}
    assert package.read_json(tools / "manifest.json")["tools"]["uex"]["program"] == "%BREAD_TOOLS%/unpackers/uex/uex.exe"


def test_smoke_checks_do_not_access_the_live_user_profile(tmp_path):
    application = tmp_path / "application"
    application.mkdir()
    calls = []
    def run(command, **kwargs):
        calls.append(kwargs)
        return mock.Mock(returncode=0)
    with mock.patch.object(package.subprocess, "run", side_effect=run), \
         mock.patch('verify_portable_profile.verify', return_value=[]) as profiles:
        package.smoke(application)
    profiles.assert_called_once_with(application)
    assert len(calls) == 5  # Core does not execute optional Office/document tools.
    for call in calls:
        assert call["env"]["HOME"] == call["env"]["USERPROFILE"]
        assert "CODEX_HOME" not in call["env"]
        for name in ("LOCALAPPDATA", "APPDATA", "USERPROFILE", "RENDERDOC_CONFIG_DIR",
                     "REPLAYLAB_RUNTIME_DIR", "REPLAYLAB_INTERNAL_RUNTIME_DIR", "REPLAYLAB_CONFIG_DIR"):
            assert Path(call["env"][name]).is_relative_to(tmp_path / "validation-state")
            assert call["env"][name] != os.environ.get(name)


def test_full_assembler_core_default_ignores_installed_optional_and_unknown_tools(tmp_path, monkeypatch, native_test_input):
    import zipfile
    application = tmp_path / "installed"; application.mkdir()
    runtime = tmp_path / "runtime"; runtime.mkdir()
    gui = tmp_path / "gui.exe"; gui.write_bytes(b"gui")
    crt = tmp_path / "crt"; crt.mkdir()
    (crt / "vcruntime140.dll").write_bytes(b"fixture CRT")
    uex = tmp_path / "uex"; uex.mkdir()
    for name in ("LICENSE.uex.txt", "LICENSE.CUE4Parse.txt", "NOTICE.CUE4Parse.txt"):
        (uex / name).write_text("license")
    for name in ("replaylab-ai.exe", "BreadMCP.exe", "ReplayLabInternal.exe", "ReplayLabShaderTools.exe"):
        (runtime / name).write_bytes(b"runtime")
    package.write_json(runtime / "replaylab-runtime-manifest.json", {"version": "test", "files": [
        {"path": path.name, "size": path.stat().st_size, "sha256": package.digest(path)}
        for path in runtime.glob("*.exe")
    ]})
    for name in ("capabilities/bread-capabilities.json", "unrequested-plugin/tool.exe"):
        path = application / "replaylab/tools" / name
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text("unrequested")
    monkeypatch.setattr(package, "audit", lambda *a: None)  # Real audits run against the actual release.
    monkeypatch.setattr(package, "verify_compilers", lambda *a: None)
    monkeypatch.setattr(package, "smoke", lambda *a: None)
    config = {"rootFiles": [], "directories": [], "toolFiles": [], "toolDirectories": [],
              "product": "RenderDocBread", "platform": "windows-x64"}
    result = package.assemble(application, gui, runtime, uex, crt, tmp_path / "dist", config)
    receipts = list((tmp_path / "dist").glob("RenderDocBread-audit-*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["passed"] is True and receipt["breadVersion"] == "test"
    assert receipt["mode"] == "package" and receipt["profile"] == "core"
    with zipfile.ZipFile(result) as archive:
        assert archive.read("RenderDocBread/vcruntime140.dll") == b"fixture CRT"
        assert archive.read("RenderDocBread/replaylab/tools/unpackers/uex/vcruntime140.dll") == b"fixture CRT"
        assert not any("/capabilities/" in n or "/portable-skills/" in n or "/unrequested-plugin/" in n for n in archive.namelist())
        assert json.loads(archive.read("RenderDocBread/replaylab/bread-distribution.json"))["profile"] == "core"
        assert any(n.endswith("skills/renderdoc-resource-export/SKILL.md") for n in archive.namelist())


@pytest.mark.parametrize("minimal", [False, True])
def test_command_line_requires_standard_functions_unless_minimal_is_explicit(tmp_path, monkeypatch, minimal):
    captured = []
    config = tmp_path / "package.json"
    package.write_json(config, {"standardCapabilitiesDirectory": "replaylab/output/verified-standard"})
    argv = ["package_portable.py", "--config", str(config)]
    for name in ("application", "gui", "runtime", "crt", "destination", "unity-mcp"):
        argv.extend(["--" + name, str(tmp_path)])
    if minimal:
        argv.append("--minimal")
    monkeypatch.setattr(package.sys, "argv", argv)
    monkeypatch.setattr(package, "assemble", lambda *a: captured.append(a))
    package.main()
    assert captured[0][-1] == (None if minimal else package.REPO / "replaylab/output/verified-standard")


def native_audit_fixture(root):
    import runtime_inputs
    package.stage_uex_natives(root)
    from host_runtime import HOSTS
    files = []
    for name in HOSTS:
        path = root / "replaylab" / name
        path.write_bytes(b"runtime")
        files.append({"path": name, "size": path.stat().st_size, "sha256": package.digest(path)})
    package.write_json(root / "replaylab/replaylab-runtime-manifest.json", {
        "files": files, "sourceInputsSha256": runtime_inputs.fingerprint()})
    package.write_json(root / "replaylab/tools/unpackers/uex/uex.runtimeconfig.json", {"runtimeOptions": {}})
    package.write_json(root / "replaylab/bread-distribution.json", {"profile": "core"})
    for source in (package.REPO / ".agents/skills").glob("*/SKILL.md"):
        target = root / "replaylab/skills" / source.parent.name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture")
    return {"requiredFiles": [package.UEX_NATIVE_PREFIX + n for n in package.UEX_NATIVE_FILES]}


def test_pinned_native_dlls_survive_cache_filter_but_runtime_state_is_rejected(tmp_path, native_test_input):
    config = native_audit_fixture(tmp_path)
    package.audit(tmp_path, config)
    (tmp_path / package.UEX_NATIVE_PREFIX / "session.sqlite").write_bytes(b"runtime state")
    with pytest.raises(ValueError, match="Private/cache file"):
        package.audit(tmp_path, config)


def test_changed_native_dll_is_rejected_even_with_the_approved_filename(tmp_path, native_test_input):
    config = native_audit_fixture(tmp_path)
    (tmp_path / package.UEX_NATIVE_PREFIX / "Detex.dll").write_bytes(b"changed")
    with pytest.raises(ValueError, match="UEX native dependency differs"):
        package.audit(tmp_path, config)


def test_frozen_vendor_metadata_audit_still_rejects_local_paths():
    sbom = "cryptography-50.0.1.dist-info/sboms/cryptography-rust.cyclonedx.json"
    upstream = '{"bom-ref": "path+file:///D:/a/cryptography/cryptography/wheelhouse/a/package"}'
    assert not package._machine_path_in_metadata(upstream, sbom)
    assert package._machine_path_in_metadata(upstream, "")
    assert package._machine_path_in_metadata(upstream + ' "C:/Users/test/private"', sbom)
    benchmark = "jsonschema/benchmarks/issue232/issue.json"
    assert not package._machine_path_in_metadata('"^[^{}/ :\\\\\\\\]+(?::\\\\d+)?$"', benchmark)
    assert package._machine_path_in_metadata(r'"C:/Users/test/private"', benchmark)
    assert package._vendor_support_source("deepagents/profiles/provider/_openai.py")
    assert package._vendor_support_source("numpy-2.5.2.dist-info/licenses/numpy/src/LICENSE")
    assert not package._vendor_support_source("profiles/session.json")



@pytest.mark.parametrize("version", [None, "1.13", "01.13.129", "1.13.129-rc.1", "2147483648.0.0"])
def test_standard_update_rejects_unsupported_versions(version):
    config = package.read_json(package.REPO / "replaylab/packaging/RenderDocBread.package.json")
    with pytest.raises(ValueError, match="standard release"):
        package.standard_update_archive_name(config, version)


def test_standard_update_archive_uses_release_version():
    config = package.read_json(package.REPO / "replaylab/packaging/RenderDocBread.package.json")
    assert package.standard_update_archive_name(config, "1.13.129") == "RenderDocBread-1.13.129-win-x64.zip"
