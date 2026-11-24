"""Exercise the pinned official DXC archive and actual code-generation gate."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "fetch_shader_dxc.py"
SPEC = importlib.util.spec_from_file_location("bread_dxc_package_test", SCRIPT)
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)
GLSLANG_SPEC = importlib.util.spec_from_file_location("bread_glslang_package_test", SCRIPT.with_name("fetch_shader_glslang.py"))
glslang = importlib.util.module_from_spec(GLSLANG_SPEC)
GLSLANG_SPEC.loader.exec_module(glslang)


def _archive():
    archive = os.environ.get("BREAD_VISUAL_DXC_ARCHIVE")
    if not archive:
        pytest.skip("Set BREAD_VISUAL_DXC_ARCHIVE to the pinned official release archive")
    return archive


def _glslang_archive():
    archive = os.environ.get("BREAD_VISUAL_GLSLANG_ARCHIVE")
    if not archive:
        pytest.skip("Set BREAD_VISUAL_GLSLANG_ARCHIVE to the pinned official Ubuntu deb")
    return archive


def test_offline_official_archive_is_pinned_licensed_and_codegen_verified(tmp_path):
    output = tmp_path / "compiler"
    platform_name = "windows-x64" if os.name == "nt" else "linux-x64"
    receipt = package.acquire(output, platform_name, _archive())
    assert receipt["capabilities"]["dxilValidatedAndSigned"]
    assert receipt["capabilities"]["spirv"]
    assert receipt["capabilities"]["exportedEntryPoint"]
    assert receipt["capabilities"]["bindingShift"]
    assert any("LICENSE" in name.upper() for name in receipt["files"])
    assert json.loads((output / "bread-dxc-release.json").read_text(encoding="utf-8")) == receipt
    for name, expected in receipt["files"].items():
        assert package.digest(output / name) == expected
    package.verify_bundle(output, platform_name)
    unexpected = output / "unexpected-loader-library.so"
    unexpected.write_bytes(b"not an owned release file")
    with pytest.raises(RuntimeError, match="unexpected files"):
        package.verify_bundle(output, platform_name)
    unexpected.unlink()
    with pytest.raises(RuntimeError, match="new or empty"):
        package.acquire(output, platform_name, _archive())


def test_corrupt_archive_is_rejected_before_compiler_execution(tmp_path, monkeypatch):
    archive = tmp_path / "modified.zip"
    archive.write_bytes(b"not-the-official-DXC-archive")
    monkeypatch.setattr(package, "verify_compiler", lambda path: pytest.fail("Unverified compiler was executed"))
    with pytest.raises(RuntimeError, match="SHA-256"):
        package.acquire(tmp_path / "compiler", "windows-x64", archive)
    assert not (tmp_path / "compiler").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows private DXC deployment")
def test_windows_private_deployment_preserves_workspace_pinned_compiler(tmp_path):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is required to validate Windows deployment")
    cache = tmp_path / "cache"
    receipt = package.acquire(cache, "windows-x64", _archive())
    application = tmp_path / "application"
    tools = application / "replaylab" / "tools"
    tools.mkdir(parents=True)
    legacy = {name: ("workspace-pinned-" + name).encode("ascii")
              for name in ("dxc.exe", "dxcompiler.dll", "dxil.dll")}
    for name, payload in legacy.items():
        (tools / name).write_bytes(payload)
    pinned = {name: package.digest(tools / name) for name in legacy}
    verification = tools / "shader-toolchain-verification.json"
    verification.write_text(json.dumps(pinned), encoding="utf-8")
    original_verification = verification.read_bytes()
    command = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(SCRIPT.with_name("Deploy-ShaderCompiler.ps1")), "-ApplicationDirectory",
               str(application), "-PythonExecutable", sys.executable, "-CacheDirectory", str(cache)]
    environment = dict(os.environ)
    # Python may have inherited PowerShell 7's module paths. Let a Windows
    # PowerShell 5 subprocess construct its own compatible standard-module path.
    environment = {key: value for key, value in environment.items() if key.lower() != "psmodulepath"}
    def run(arguments, success=True):
        result = subprocess.run(command + arguments, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, timeout=90, env=environment)
        if success:
            assert result.returncode == 0, result.stdout
        return result
    run([])
    run(["-VerifyOnly"])
    bridge_file = SCRIPT.parents[1] / "renderdoc_mcp_bridge/shader_visual_compile.py"
    spec = importlib.util.spec_from_file_location("private_windows_dxc_test", bridge_file)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    compiler = bridge.Compiler(str(application))
    identity = compiler.identity("hlsl", "vulkan", "ps_6_0")
    assert identity["origin"] == "packaged"
    assert Path(identity["files"][0][0]) == tools / "dxc" / "bin" / "dxc.exe"
    dependencies = {Path(path): digest for path, digest in identity["files"]}
    for name in legacy:
        assert dependencies[tools / "dxc" / "bin" / name] == receipt["files"][name]
    source = "float4 main():SV_Target0{return float4(-7,2.5,0,1);}"
    assert compiler.compile(source, "hlsl", "d3d12", "main", "ps_6_0")["encoding"] == "DXIL"
    assert compiler.compile(source, "hlsl", "vulkan", "main", "ps_6_0")["encoding"] == "SPIRV"
    for name, payload in legacy.items():
        assert (tools / name).read_bytes() == payload
        assert package.digest(tools / name) == pinned[name]
        assert package.digest(tools / "dxc" / "bin" / name) == receipt["files"][name]
    assert verification.read_bytes() == original_verification
    licenses = tools / "dxc" / "licenses" / receipt["release"]
    assert json.loads((licenses / "bread-dxc-release.json").read_text(encoding="utf-8")) == receipt
    for name, digest in receipt["files"].items():
        if "LICENSE" in name.upper():
            assert package.digest(licenses / name) == digest
    # Verification must inspect the private group, never quietly accept the flat fallback.
    (tools / "dxc" / "bin" / "dxil.dll").write_bytes(b"tampered-private-validator")
    failure = run(["-VerifyOnly"], success=False)
    assert failure.returncode != 0 and "differs from the pinned official release" in failure.stdout
    assert {name: package.digest(tools / name) for name in legacy} == pinned


def test_corrupt_glslang_archive_is_rejected_before_extraction_or_execution(tmp_path, monkeypatch):
    archive = tmp_path / "modified.deb"
    archive.write_bytes(b"not-the-official-glslang-archive")
    monkeypatch.setattr(glslang, "verify_compiler", lambda path: pytest.fail("Unverified compiler was executed"))
    with pytest.raises(RuntimeError, match="SHA-256"):
        glslang.acquire(tmp_path / "compiler", archive)
    assert not (tmp_path / "compiler").exists()


@pytest.mark.skipif(os.name == "nt", reason="Linux glslang executable")
def test_offline_glslang_archive_has_licenses_and_self_contained_codegen(tmp_path):
    receipt = glslang.acquire(tmp_path / "compiler", _glslang_archive())
    assert receipt["capabilities"]["macroPreprocess"]
    assert receipt["capabilities"]["includePreprocess"]
    assert receipt["capabilities"]["vulkanGLSL"]
    assert receipt["capabilities"]["openGLSPIRV"]
    assert receipt["capabilities"]["optimizedWithoutExternalTools"]
    glslang.verify_bundle(tmp_path / "compiler")


@pytest.mark.skipif(os.name == "nt", reason="Linux CMake compiler install layout")
def test_linux_cmake_target_installs_pinned_rpath_bundle_without_native_build(tmp_path):
    archive = _archive()
    glslang_archive = _glslang_archive()
    cmake = shutil.which("cmake")
    if not cmake:
        pytest.skip("CMake is required to validate the Linux packaging integration")
    source, build, installed, cache = [tmp_path / name for name in ("source", "build", "installed", "cache")]
    source.mkdir()
    module = SCRIPT.with_name("ShaderCompiler.cmake")
    (source / "CMakeLists.txt").write_text(
        'cmake_minimum_required(VERSION 3.5)\nproject(BreadCompilerInstall NONE)\ninclude("' +
        module.as_posix() + '")\n', encoding="utf-8")
    def run(arguments, success=True):
        result = subprocess.run([cmake] + [str(value) for value in arguments],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if success:
            assert result.returncode == 0, result.stdout
        return result
    run(["-S", source, "-B", build, "-DBREAD_BUNDLE_SHADER_COMPILERS=ON",
         "-DBREAD_DXC_ARCHIVE=" + archive, "-DBREAD_DXC_CACHE_DIRECTORY=" + str(cache),
         "-DBREAD_GLSLANG_ARCHIVE=" + glslang_archive,
         "-DBREAD_GLSLANG_CACHE_DIRECTORY=" + str(tmp_path / "glslang-cache"),
         "-DCMAKE_INSTALL_PREFIX=" + str(installed)])
    run(["--build", build, "--target", "bundle-bread-shader-compilers"])
    run(["--install", build, "--component", "bread-shader-compilers"])
    bundle = installed / "share/renderdoc/replaylab/tools/dxc"
    package.verify_bundle(bundle, "linux-x64")
    # The bridge's installed lookup must win over PATH and execute the binary
    # in its original bin/../lib location, including for a relocated prefix.
    bridge_file = SCRIPT.parents[1] / "renderdoc_mcp_bridge/shader_visual_compile.py"
    spec = importlib.util.spec_from_file_location("installed_bread_compiler_test", bridge_file)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    compiler = bridge.Compiler(str(installed / "bin"))
    identity = compiler.identity("hlsl", "vulkan", "ps_6_0")
    assert identity["origin"] == "packaged"
    assert Path(identity["files"][0][0]) == bundle / "bin/dxc"
    result = compiler.compile("float4 main():SV_Target0{return 0.25;}", "hlsl", "vulkan", "main", "ps_6_0")
    assert result["encoding"] == "SPIRV"
    glslang_bundle = installed / "share/renderdoc/replaylab/tools/glslang"
    glslang.verify_bundle(glslang_bundle)
    legacy_tool = glslang_bundle.parent / "glslangValidator"
    legacy_tool.write_text("#!/bin/sh\nexit 77\n", encoding="utf-8")
    legacy_tool.chmod(0o755)
    glsl_identity = compiler.identity("glsl", "opengl", "")
    assert glsl_identity["origin"] == "packaged"
    assert Path(glsl_identity["files"][0][0]) == glslang_bundle / "bin/glslangValidator"
    source_glsl = "#version 450\n#define VALUE 0.25\nlayout(location=0) out vec4 c;void main(){c=vec4(VALUE,0,0,1);}"
    expanded = compiler.preprocess(source_glsl, "glsl", "main", api="vulkan")
    assert "vec4(0.25" in expanded.replace(" ", "")
    binary = compiler.compile(source_glsl, "glsl", "opengl", "main", target_encoding="OpenGLSPIRV")
    assert binary["encoding"] == "OpenGLSPIRV"
    (cache / "lib/libdxil.so").write_bytes(b"changed compiler dependency")
    failure = run(["--build", build, "--target", "bundle-bread-dxc"], success=False)
    assert failure.returncode != 0 and "Pinned DXC bundle file changed" in failure.stdout
    # A tampered cache must not overwrite the already verified installation.
    package.verify_bundle(bundle, "linux-x64")
