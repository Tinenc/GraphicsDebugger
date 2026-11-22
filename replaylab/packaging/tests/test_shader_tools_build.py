from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest import mock

import pytest


PACKAGING = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("shader_tools_builder", PACKAGING / "build_shader_tools.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.fixture
def fixture(tmp_path):
    source = tmp_path / "mcp_host"
    adapters = source / "src" / "shader_workspace" / "adapters"
    adapters.mkdir(parents=True)
    (adapters / "example.py").write_text("import argparse\nfrom .support import VALUE\n", encoding="utf-8")
    (adapters / "support.py").write_text("VALUE = 1\n", encoding="utf-8")
    entry = tmp_path / "shader_tools_entry.py"
    entry.write_text("from adapters.example import main\n", encoding="utf-8")
    host = source / "src" / "replaylab_host.py"
    host.write_text("# Host v1\n", encoding="utf-8")
    interpreter = tmp_path / "python.exe"
    interpreter.write_bytes(b"python-runtime-v1")
    freezer = tmp_path / "bootloader.exe"
    freezer.write_bytes(b"pyinstaller-bootloader-v1")
    environment = {"python/executable": interpreter, "distribution/freezer/bootloader": freezer}
    calls = []

    def build(staging, work, sources):
        calls.append(dict(sources))
        artifact = work / "built.exe"
        artifact.write_bytes(b"fixture-runner-" + str(len(calls)).encode("ascii"))
        return artifact

    def invoke(**overrides):
        index = len(list(tmp_path.glob("work-*")))
        return builder.build_or_reuse(source, entry, tmp_path / "cache", tmp_path / ("work-%d" % index),
                                     tmp_path / "out" / builder.EXECUTABLE,
                                     environment_files=environment, builder=overrides.get("build", build))

    return source, entry, adapters, host, environment, calls, invoke


def test_cache_reuses_identical_bytes_after_host_only_change(fixture):
    source, entry, adapters, host, environment, calls, invoke = fixture
    first = invoke()
    original = Path(first["path"]).read_bytes()
    host.write_text("# Report and Host UI changed; adapter closure is unchanged\n", encoding="utf-8")
    second = invoke()
    assert len(calls) == 1
    assert second["cacheHit"] is True
    assert first["inputSha256"] == second["inputSha256"]
    assert Path(second["path"]).read_bytes() == original
    assert set(calls[0]) == {"shader_tools_entry.py", "adapters/example.py", "adapters/support.py"}


@pytest.mark.parametrize("changed", ["entry", "adapter", "transitive", "python", "bootloader"])
def test_actual_build_input_changes_invalidate_runner_cache(fixture, changed):
    source, entry, adapters, host, environment, calls, invoke = fixture
    first = invoke()
    path = {"entry": entry, "adapter": adapters / "example.py", "transitive": adapters / "support.py",
            "python": environment["python/executable"],
            "bootloader": environment["distribution/freezer/bootloader"]}[changed]
    path.write_bytes(path.read_bytes() + b"\n# changed\n")
    second = invoke()
    assert len(calls) == 2
    assert second["cacheHit"] is False
    assert second["inputSha256"] != first["inputSha256"]
    assert second["executableSha256"] != first["executableSha256"]


@pytest.mark.parametrize("changed", ["artifact", "manifest", "missing-manifest"])
def test_corrupt_cache_never_reuses_unverified_runner(fixture, changed):
    source, entry, adapters, host, environment, calls, invoke = fixture
    first = invoke()
    cached = source.parent / "cache" / first["inputSha256"]
    if changed == "artifact":
        (cached / builder.EXECUTABLE).write_bytes(b"tampered runner")
    elif changed == "manifest":
        record = json.loads((cached / "manifest.json").read_text())
        record["inputs"]["files"]["source/adapters/support.py"] = "f" * 64
        (cached / "manifest.json").write_text(json.dumps(record), encoding="utf-8")
    else:
        (cached / "manifest.json").unlink()
    with pytest.raises(ValueError, match="cache|regular file"):
        invoke()
    assert len(calls) == 1


def test_inputs_changed_during_build_are_not_committed(fixture):
    source, entry, adapters, host, environment, calls, invoke = fixture

    def changing_build(staging, work, sources):
        artifact = work / "built.exe"
        artifact.write_bytes(b"runner")
        (adapters / "support.py").write_text("VALUE = 2\n", encoding="utf-8")
        return artifact

    with pytest.raises(ValueError, match="inputs changed"):
        invoke(build=changing_build)
    assert not (source.parent / "out" / builder.EXECUTABLE).exists()
    assert not any(path.name[0] != "." for path in (source.parent / "cache").iterdir())


@pytest.mark.parametrize("bad_import", ["import src.server", "import httpx", "__import__('src.server')",
                                        "import importlib\nimportlib.import_module('src.server')"])
def test_opaque_or_non_adapter_imports_fail_before_build(fixture, bad_import):
    source, entry, adapters, host, environment, calls, invoke = fixture
    (adapters / "support.py").write_text(bad_import, encoding="utf-8")
    with pytest.raises(ValueError, match="import"):
        invoke()
    assert calls == []


def test_actual_dispatcher_closure_and_all_help_commands_exclude_host(tmp_path):
    source = PACKAGING.parent / "mcp_host"
    sources = builder.local_sources(source, PACKAGING / "shader_tools_entry.py")
    assert len(sources) == 7
    assert all(name.startswith("adapters/") or name == "shader_tools_entry.py" for name in sources)
    for relative, origin in sources.items():
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, destination)
    # Run the staged namespace, not the broad src package used by Host tests.
    for command in ([], ["adapter-hlsl"], ["adapter-dxbc-assembly"], ["adapter-d3dcompile"],
                    ["adapter-dxil-assemble"], ["adapter-spirv-cross"], ["adapter-renodx"]):
        code = (
            "import sys; sys.path.insert(0, %r); import shader_tools_entry; "
            "\ntry: shader_tools_entry.main(%r)\nexcept SystemExit as exc: assert exc.code == 0\n"
            "assert not any(name == 'src' or name.startswith('src.') for name in sys.modules)\n"
        ) % (str(tmp_path), command + ["--help"])
        result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr


def test_cache_lock_has_bounded_contention(tmp_path):
    with builder.cache_lock(tmp_path):
        with pytest.raises(TimeoutError, match="lock is busy"):
            with builder.cache_lock(tmp_path, timeout=0.05):
                raise AssertionError("second lock was granted")


def test_runner_build_uses_isolated_staging_without_collecting_host(tmp_path):
    staging, work = tmp_path / "source", tmp_path / "work"
    staging.mkdir()
    entry = tmp_path / "entry.py"
    entry.write_text("print('fixture')\n", encoding="utf-8")

    def fake_pyinstaller(command, **options):
        assert command[:4] == [sys.executable, "-I", "-m", "PyInstaller"]
        assert not any("collect" in item for item in command)
        assert options["cwd"] == staging
        assert "PYTHONPATH" not in options["env"]
        assert "PYTHONHOME" not in options["env"]
        assert options["env"]["PYINSTALLER_CONFIG_DIR"] == str(work / "pyinstaller-config")
        assert (staging / "shader_tools_entry.py").read_bytes() == entry.read_bytes()
        (work / "dist").mkdir()
        (work / "dist" / builder.EXECUTABLE).write_bytes(b"fake runner")

    with mock.patch.object(builder.subprocess, "run", side_effect=fake_pyinstaller):
        result = builder._run_build(staging, work, {"shader_tools_entry.py": entry})
    assert result.read_bytes() == b"fake runner"


@pytest.mark.skipif(os.name != "nt", reason="Windows home lookup uses these environment variables")
@pytest.mark.parametrize("without_userprofile", [False, True])
def test_runner_build_child_resolves_real_windows_home_with_minimal_environment(tmp_path, without_userprofile):
    staging, work = tmp_path / "source", tmp_path / "work"
    staging.mkdir()
    entry = tmp_path / "entry.py"
    entry.write_text("pass\n", encoding="utf-8")
    original_run = subprocess.run
    parent_environment = dict(os.environ)
    if without_userprofile:
        if not all(parent_environment.get(name) for name in ("HOMEDRIVE", "HOMEPATH")):
            pytest.skip("The caller has no real HOMEDRIVE/HOMEPATH fallback to preserve")
        parent_environment.pop("USERPROFILE", None)
    expected_home = str(Path.home().resolve())
    parent_environment["REPLAYLAB_TEST_UNRELATED_VARIABLE"] = "must-not-be-forwarded"

    def probe_child(command, **options):
        environment = options["env"]
        for name in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
            assert environment.get(name) == parent_environment.get(name)
        assert "PYTHONPATH" not in environment
        assert "PYTHONHOME" not in environment
        assert "REPLAYLAB_TEST_UNRELATED_VARIABLE" not in environment
        # This executes the same isolated child/home lookup that PyInstaller
        # performs, rather than asserting only the environment dictionary.
        result = original_run(
            [sys.executable, "-I", "-c", "from pathlib import Path; print(Path.home().resolve())"],
            cwd=staging, env=environment, capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected_home
        (work / "dist").mkdir()
        (work / "dist" / builder.EXECUTABLE).write_bytes(b"home-probe-only")

    with mock.patch.dict(builder.os.environ, parent_environment, clear=True), \
            mock.patch.object(builder.subprocess, "run", side_effect=probe_child):
        assert builder._run_build(staging, work, {"shader_tools_entry.py": entry}).read_bytes() == b"home-probe-only"
