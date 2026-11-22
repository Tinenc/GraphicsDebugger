import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest
import version_policy as version


@pytest.fixture
def repository(tmp_path):
    for relative in [version.CONFIG, "renderdoc/api/replay/version.h", *version.SOURCES]:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(version.REPO / relative, target)
    (tmp_path / version.HEADER).parent.mkdir(parents=True, exist_ok=True)
    version.synchronize(tmp_path, write=True)
    return tmp_path


def test_checked_in_versions_match_integrated_core_and_release_revision():
    identity = version.synchronize()
    assert identity["version"] == identity["upstreamVersion"] + "." + str(identity["extensionRevision"])


def test_upstream_merge_updates_all_generated_versions_without_incrementing_revision(repository):
    before = version.release_identity(repository)
    header = repository / "renderdoc/api/replay/version.h"
    content = header.read_text(encoding="utf-8")
    minor = int(before["upstreamVersion"].split(".")[1])
    header.write_text(content.replace(f"RENDERDOC_VERSION_MINOR {minor}", f"RENDERDOC_VERSION_MINOR {minor + 1}"), encoding="utf-8")
    with pytest.raises(ValueError, match="stale"):
        version.synchronize(repository)
    after = version.synchronize(repository, write=True)
    assert after["extensionRevision"] == before["extensionRevision"]
    assert after["version"] == f'1.{minor + 1}.{before["extensionRevision"]}'
    assert version.synchronize(repository) == after
    assert version.synchronize(repository, write=True) == after  # Rebuilding cannot consume revisions.
    for relative in version.SOURCES:
        assert '"' + after["version"] + '"' in (repository / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("revision", [True, -1, 65536, "137", None])
def test_invalid_revision_fails_before_source_mutation(repository, revision):
    config = json.loads((repository / version.CONFIG).read_text())
    config["versioning"]["extensionRevision"] = revision
    with pytest.raises(ValueError, match="extensionRevision"):
        version.release_identity(repository, config)


def test_stale_or_wrong_native_and_runtime_versions_are_rejected(repository):
    identity = version.release_identity(repository)
    assert version.validate_runtime_version(identity["version"], repo=repository) == identity
    with pytest.raises(ValueError, match="Frozen runtime version"):
        version.validate_runtime_version("1.13.136", repo=repository)
    for prefix in ("RenderDocBread", "q_render_soco_doc"):
        version.validate_gui_output(SimpleNamespace(returncode=0, stdout=f'{prefix} v{identity["version"]}\n', stderr=""), identity["version"])
    for output in ("RenderDocBread v1.44", "RenderDocBread v1.13.136", "random 1.46.137", ""):
        with pytest.raises(ValueError, match="GUI --version"):
            version.validate_gui_output(SimpleNamespace(returncode=0, stdout=output, stderr=""), identity["version"])


def test_native_probe_uses_fresh_settings_instead_of_running_application_profile(repository, monkeypatch):
    calls = []
    identity = version.release_identity(repository)
    def run(command, **kwargs):
        calls.append((command, kwargs))
        for name in ("LOCALAPPDATA", "APPDATA", "USERPROFILE", "RENDERDOC_CONFIG_DIR"):
            assert "bread-version-check-" in kwargs["env"][name]
            assert Path(kwargs["env"][name]).is_dir()
        assert "CODEX_HOME" not in kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="RenderDocBread " + identity["displayVersion"], stderr="")
    monkeypatch.setattr(version.subprocess, "run", run)
    version.validate_gui_version(repository / "RenderDocBread.exe", identity["version"])
    assert len(calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows version resource compiler")
def test_generated_native_header_and_real_gui_resource_report_same_release(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes
    from setuptools._distutils.ccompiler import new_compiler
    identity = version.release_identity()
    source = tmp_path / "probe.cpp"
    native = (version.REPO / "qrenderdoc/Code/qrenderdoc.cpp").read_text(encoding="utf-8")
    printer = 'static int PrintBreadVersion()' + native.split(
        'static int PrintBreadVersion()', 1)[1].split('\nint main(', 1)[0]
    source.write_text('#define RENDERDOC_PLATFORM_WIN32 1\n#include <windows.h>\n'
                      '#include <stdio.h>\n#include <string>\n#include "BreadVersion.h"\n'
                      'static const char *RENDERDOC_GetCommitHash(){return "fixture";}\n' + printer +
                      'int WINAPI WinMain(HINSTANCE,HINSTANCE,LPSTR,int){return PrintBreadVersion();}\n',
                      encoding="ascii")
    compiler = new_compiler(compiler="msvc")
    compiler.initialize()
    with monkeypatch.context() as context:
        context.chdir(tmp_path)
        objects = compiler.compile(["probe.cpp"], output_dir=str(tmp_path),
            include_dirs=[str(version.REPO / "qrenderdoc/Code")], extra_postargs=["/EHsc"])
        compiler.call([compiler.rc, "/nologo", *["/I" + item for item in type(compiler).include_dirs],
                        "/I" + str(version.REPO / "renderdoc/api/replay"),
                        "/I" + str(version.REPO / "qrenderdoc/Resources"),
                        "/fo" + str(tmp_path / "gui.res"), str(version.REPO / "qrenderdoc/Resources/qrenderdoc.rc")])
        compiler.link_executable([*objects, str(tmp_path / "gui.res")], str(tmp_path / "probe"),
                                 extra_postargs=["/SUBSYSTEM:WINDOWS"])
    exe = tmp_path / "probe.exe"
    result = subprocess.run([str(exe)], capture_output=True, text=True, check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    version.validate_gui_output(result, identity["version"])
    dll = ctypes.WinDLL("version", use_last_error=True)
    dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    dll.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
    dll.VerQueryValueW.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    size = dll.GetFileVersionInfoSizeW(str(exe), None)
    assert size
    buffer = ctypes.create_string_buffer(size)
    assert dll.GetFileVersionInfoW(str(exe), 0, size, buffer)
    value, length = ctypes.c_void_p(), wintypes.UINT()
    assert dll.VerQueryValueW(buffer, "\\StringFileInfo\\080904b0\\ProductVersion", ctypes.byref(value), ctypes.byref(length))
    assert ctypes.wstring_at(value) == identity["displayVersion"]
    assert dll.VerQueryValueW(buffer, "\\StringFileInfo\\080904b0\\FileVersion", ctypes.byref(value), ctypes.byref(length))
    assert ctypes.wstring_at(value) == identity["version"] + ".0"


@pytest.mark.skipif(os.name == "nt", reason="POSIX native stdout; GUI subsystem is checked on Windows")
def test_posix_native_version_printer_uses_the_release_identity(tmp_path):
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("Native C++ compiler is unavailable")
    native = (version.REPO / "qrenderdoc/Code/qrenderdoc.cpp").read_text(encoding="utf-8")
    printer = 'static int PrintBreadVersion()' + native.split(
        'static int PrintBreadVersion()', 1)[1].split('\nint main(', 1)[0]
    source = tmp_path / "version.cpp"
    source.write_text('#include <stdio.h>\n#include <string>\n#include "BreadVersion.h"\n'
                      'static const char *RENDERDOC_GetCommitHash(){return "fixture";}\n' + printer +
                      'int main(){return PrintBreadVersion();}\n', encoding="ascii")
    executable = tmp_path / "version"
    subprocess.run([compiler, str(source), "-I", str(version.REPO / "qrenderdoc/Code"),
                    "-o", str(executable)], check=True, capture_output=True, text=True)
    result = subprocess.run([str(executable)], check=True, capture_output=True, text=True)
    version.validate_gui_output(result, version.release_identity()["version"])
