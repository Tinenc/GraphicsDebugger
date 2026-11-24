"""Portable compiler identity, safe argv and actual preprocessing/codegen."""
import importlib.util
import os
from pathlib import Path
import shutil
import struct

import pytest


BRIDGE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge"


def _load(filename):
    spec = importlib.util.spec_from_file_location("test_" + filename, BRIDGE / (filename + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compile_module = _load("shader_visual_compile")
debug = _load("shader_visual_debug")
instrument = _load("shader_visual_instrument")


def _runner(tmp_path, monkeypatch, names=("dxc",)):
    directories = []
    for name in names:
        environment = "BREAD_VISUAL_DXC" if name == "dxc" else "BREAD_VISUAL_GLSLANG"
        executable = os.environ.get(environment) or shutil.which(name) or (shutil.which("glslangValidator") if name == "glslang" else None)
        if not executable:
            pytest.skip("Set " + environment + " for actual compiler validation")
        directories.append(str(Path(executable).parent))
    monkeypatch.setenv("PATH", os.pathsep.join(directories + [os.environ.get("PATH", "")]))
    return compile_module.Compiler(str(tmp_path / "application"))


def _snapshot(tmp_path, source, extra=None, edited=None):
    root = tmp_path / "unit"
    root.mkdir(exist_ok=True)
    (root / "main.hlsl").write_text(source, encoding="utf-8")
    for name, contents in (extra or {}).items():
        filename = root / name
        filename.parent.mkdir(parents=True, exist_ok=True)
        filename.write_text(contents, encoding="utf-8")
    path = root / (edited or "main.hlsl")
    text = path.read_text(encoding="utf-8")
    return debug.source_snapshot({"source": text, "sourcePath": str(path),
        "rootSourcePath": str(root / "main.hlsl"), "byteStart": text.rindex("value"), "includeRoot": str(root)})


def _preprocess(runner, snapshot, flags=None):
    prepared = instrument.prepare_preprocess(snapshot["source"], snapshot["byteStart"], snapshot["byteEnd"])
    text = runner.preprocess(prepared["source"], "hlsl", "main", "ps_5_0", "d3d11", snapshot, flags=flags)
    return instrument.finish_preprocess(text, prepared)


def _spirv_entries(raw):
    words = struct.unpack("<%dI" % (len(raw) // 4), raw)
    entries, index = [], 5
    while index < len(words):
        length, opcode = words[index] >> 16, words[index] & 0xffff
        assert length > 0
        if opcode == 15:
            encoded = struct.pack("<%dI" % (length - 3), *words[index + 3:index + length])
            entries.append(encoded.split(b"\0", 1)[0].decode("utf-8"))
        index += length
    return entries


def test_packaged_tool_precedes_path_and_dependency_identity_changes(tmp_path, monkeypatch):
    suffix = ".exe" if os.name == "nt" else ""
    package = tmp_path / "app" / "replaylab" / "tools"
    package.mkdir(parents=True)
    executable, dependency = package / ("dxc" + suffix), package / "compiler-library.bin"
    executable.write_bytes(b"packaged-compiler")
    dependency.write_bytes(b"version-one")
    executable.chmod(0o755)
    runner = compile_module.Compiler(str(tmp_path / "app"))
    monkeypatch.setattr(runner, "_dependencies", lambda path, name: [path, str(dependency)])
    monkeypatch.setattr(compile_module.shutil, "which", lambda name: pytest.fail("PATH used before the package"))
    first = runner.identity("hlsl", "vulkan", "ps_6_0")
    dependency.write_bytes(b"version-two-and-new-size")
    second = runner.identity("hlsl", "vulkan", "ps_6_0")
    assert first["origin"] == "packaged"
    assert first != second
    assert first["files"][0] == second["files"][0]


def test_installed_path_is_resolved_in_place(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    identity = runner.identity("hlsl", "d3d12", "ps_6_0")
    assert identity["origin"] == "system-path"
    assert Path(identity["files"][0][0]).is_absolute()
    assert str(tmp_path) not in identity["files"][0][0]
    assert all(len(digest) == 64 for _, digest in identity["files"])
    assert not (tmp_path / "application").exists()


@pytest.mark.parametrize("installed", [False, True])
def test_private_dxc_precedes_pinned_workspace_tool_without_modifying_it(tmp_path, monkeypatch, installed):
    suffix = ".exe" if os.name == "nt" else ""
    application = tmp_path / "prefix" / "bin"
    tools = (tmp_path / "prefix" / "share" / "renderdoc" / "replaylab" / "tools"
             if installed else application / "replaylab" / "tools")
    private = tools / "dxc" / "bin" / ("dxc" + suffix)
    private.parent.mkdir(parents=True)
    private.write_bytes(b"private-visual-compiler")
    private.chmod(0o755)
    # Unix cannot place a loose executable and a private directory at the same
    # name. The preceding parent-directory fallback is the competing old tool.
    legacy = (tools if os.name == "nt" else application) / ("dxc" + suffix)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"workspace-save-route-pinned-compiler")
    legacy.chmod(0o755)
    runner = compile_module.Compiler(str(application))
    monkeypatch.setattr(compile_module.shutil, "which", lambda name: pytest.fail("PATH used before private DXC"))
    assert Path(runner._tool("dxc")) == private.resolve()
    assert runner._origins[str(private.resolve())] == "packaged"
    assert legacy.read_bytes() == b"workspace-save-route-pinned-compiler"


def test_safe_options_preserve_semantics_and_drop_file_destinations():
    options = compile_module.compile_options([("@cmdline",
        '/DVALUE=0.25 -D "LABEL=hello world" /Zpr -HV 2018 -fvk-use-dx-layout '
        '-fvk-b-shift 4 0 /T ps_6_0 /E main /I "C:\\source includes" '
        '/Fo "C:\\private\\out.dxil" -Fddebug.pdb -Qstrip_reflect /O3')])
    assert options == ["-DVALUE=0.25", "-DLABEL=hello world", "-Zpr", "-HV", "2018",
                       "-fvk-use-dx-layout", "-fvk-b-shift", "4", "0", "-O3"]
    assert compile_module.has_preprocessor_options([("@cmdline", "-DFLAG=1")])
    assert not compile_module.has_preprocessor_options([("@cmdline", "-Zpc -O3")])
    assert compile_module.compile_options([("@spirver", "spirv1.5")]) == []


@pytest.mark.parametrize("version", ["2016", "2018", "2021"])
def test_explicit_hlsl_version_wins_over_vulkan_compatibility_default(version):
    flags = [("@cmdline", "-HV" + version + " -Zpr")]
    assert compile_module.effective_options(flags, "hlsl", "vulkan") == ["-HV", version, "-Zpr"]


def test_vulkan_default_is_scoped_to_hlsl():
    assert compile_module.effective_options([], "hlsl", "vulkan") == ["-HV", "2018"]
    for language, api in (("glsl", "vulkan"), ("glsl", "opengl"),
                          ("hlsl", "d3d11"), ("hlsl", "d3d12")):
        assert compile_module.effective_options([], language, api) == []


def test_real_vulkan_legacy_vector_operators_compile_and_preprocess_consistently(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    source = '''
float4 main(float4 position : SV_Position) : SV_Target0 {
    bool3 below = position.xyz < 0;
    bool3 above = position.xyz > 1;
    bool outside = any(below || above);
    bool3 both = below && above;
    float3 value = below ? position.xyz : 1.0.xxx;
    return float4(value + float3(both) + float(outside), 1);
}'''
    preprocessed = runner.preprocess(source, "hlsl", "main", "ps_6_0", "vulkan")
    assert runner.compile(source, "hlsl", "vulkan", "main")["encoding"] == "SPIRV"
    assert runner.compile(preprocessed, "hlsl", "vulkan", "main")["encoding"] == "SPIRV"
    # An explicitly requested modern dialect must still diagnose legacy source.
    with pytest.raises(compile_module.CompilerError, match="must be scalar"):
        runner.compile(source, "hlsl", "vulkan", "main", flags=[("@cmdline", "-HV 2021")])


def test_dependency_resolution_cache_tracks_library_replacement_and_environment(tmp_path, monkeypatch):
    executable, dependency = tmp_path / "dxc", tmp_path / "libcompiler.so"
    executable.write_bytes(b"compiler")
    dependency.write_bytes(b"one")
    runner = compile_module.Compiler(str(tmp_path))
    calls = []
    def resolve(path, name):
        calls.append(path)
        return [str(executable), str(dependency)]
    monkeypatch.setattr(runner, "_dependencies", resolve)
    runner._cached_dependencies(str(executable), "dxc")
    runner._cached_dependencies(str(executable), "dxc")
    assert len(calls) == 1
    dependency.write_bytes(b"replacement")
    runner._cached_dependencies(str(executable), "dxc")
    assert len(calls) == 2
    monkeypatch.setenv("LD_LIBRARY_PATH", str(tmp_path / "other"))
    runner._cached_dependencies(str(executable), "dxc")
    assert len(calls) == 3


@pytest.mark.skipif(os.name == "nt", reason="Unix symbolic link loader resolution")
def test_dependency_resolution_cache_tracks_symlink_retarget_and_new_search_path(tmp_path, monkeypatch):
    first, second = tmp_path / "lib-first.so", tmp_path / "lib-second.so"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    alias = tmp_path / "libcompiler.so"
    alias.symlink_to(first)
    executable = tmp_path / "dxc"
    executable.write_bytes(b"compiler")
    search = tmp_path / "future" / "hwcaps"
    runner = compile_module.Compiler(str(tmp_path))
    calls = []
    def resolve(path, name):
        calls.append(path)
        runner._dependency_search_paths[(path, name)] = {str(search)}
        return [str(executable), str(alias)]
    monkeypatch.setattr(runner, "_dependencies", resolve)
    runner._cached_dependencies(str(executable), "dxc")
    alias.unlink()
    alias.symlink_to(second)
    runner._cached_dependencies(str(executable), "dxc")
    assert len(calls) == 2
    search.mkdir(parents=True)
    runner._cached_dependencies(str(executable), "dxc")
    assert len(calls) == 3


@pytest.mark.parametrize("options", ["-some-new-layout=1", "@args.txt", "-D", '-D "VALUE=1', "-fvk-b-shift 4", "--output /tmp/unsafe"])
def test_unknown_or_malformed_options_fail_explicitly(options):
    with pytest.raises(compile_module.CompilerError):
        compile_module.compile_options([("@cmdline", options)])


def test_option_split_preserves_backslashes_and_escaped_macro_quotes():
    assert compile_module._arguments('-DPATH=C:\\textures\\a -DSTR=\\"hello\\"') == [
        '-DPATH=C:\\textures\\a', '-DSTR="hello"']


def test_real_preprocessor_keeps_pragma_once_and_repeated_include_semantics(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    source = '#include "shared.hlsli"\n#include "shared.hlsli"\nfloat4 main():SV_Target0 {float value=selectValue();return value;}'
    snapshot = _snapshot(tmp_path, source, {"shared.hlsli": "#pragma once\nfloat selectValue(){return 0.25;}"})
    mapped = _preprocess(runner, snapshot)
    assert mapped["source"].count("float selectValue()") == 1
    plan = instrument.inspect(mapped["source"], mapped["byteStart"], mapped["byteEnd"])
    assert plan["valueType"] == "float"


def test_real_preprocessor_tracks_selected_helper_inside_once_include(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    source = '#include "shared.hlsli"\n#include "shared.hlsli"\nfloat4 main():SV_Target0{return helper(0.25);}'
    header = "#pragma once\nfloat helper(float value){return value*2;}"
    snapshot = _snapshot(tmp_path, source, {"shared.hlsli": header}, edited="shared.hlsli")
    mapped = _preprocess(runner, snapshot)
    plan = instrument.inspect(mapped["source"], mapped["byteStart"], mapped["byteEnd"])
    assert plan["owner"]["name"] == "helper"


@pytest.mark.parametrize("include", ["missing.hlsli", "../outside.hlsli"])
def test_real_inactive_missing_or_forbidden_include_does_not_block(tmp_path, monkeypatch, include):
    runner = _runner(tmp_path, monkeypatch)
    (tmp_path / "outside.hlsli").write_text("#error EXTERNAL_FILE_WAS_READ", encoding="utf-8")
    source = '#if 0\n#include "' + include + '"\n#endif\nfloat4 main():SV_Target0 {float value=0.25;return value;}'
    snapshot = _snapshot(tmp_path, source)
    mapped = _preprocess(runner, snapshot)
    assert "EXTERNAL_FILE_WAS_READ" not in mapped["source"]
    assert instrument.inspect(mapped["source"], mapped["byteStart"], mapped["byteEnd"])["valueType"] == "float"


def test_real_active_forbidden_include_fails_inside_mirror(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    (tmp_path / "outside.hlsli").write_text("#error EXTERNAL_FILE_WAS_READ", encoding="utf-8")
    source = '#include "../outside.hlsli"\nfloat4 main():SV_Target0 {float value=0.25;return value;}'
    snapshot = _snapshot(tmp_path, source)
    with pytest.raises(compile_module.CompilerError) as caught:
        _preprocess(runner, snapshot)
    assert "__bread_unavailable_includes__" in str(caught.value)
    assert "EXTERNAL_FILE_WAS_READ" not in str(caught.value)


def test_commented_include_is_not_opened_or_rewritten(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    source = '/*\n#include "../outside.hlsli"\n*/\nfloat4 main():SV_Target0 {float value=0.25;return value;}'
    snapshot = _snapshot(tmp_path, source)
    assert snapshot["blockedIncludes"] == {}
    assert compile_module._mirror_source(source, {"../outside.hlsli"}) == source
    _preprocess(runner, snapshot)


@pytest.mark.parametrize("active", [False, True])
def test_real_computed_include_cannot_read_untracked_live_file(tmp_path, monkeypatch, active):
    runner = _runner(tmp_path, monkeypatch)
    outside = tmp_path / "outside.hlsli"
    outside.write_text("#error EXTERNAL_FILE_WAS_READ", encoding="utf-8")
    source = '#define HEADER "' + outside.as_posix() + '"\n#if ' + str(int(active)) + '\n#include HEADER\n#endif\nfloat4 main():SV_Target0{float value=0.25;return value;}'
    snapshot = _snapshot(tmp_path, source)
    if active:
        with pytest.raises(compile_module.CompilerError) as caught:
            _preprocess(runner, snapshot)
        assert "computed-" in str(caught.value)
        assert "EXTERNAL_FILE_WAS_READ" not in str(caught.value)
    else:
        assert "EXTERNAL_FILE_WAS_READ" not in _preprocess(runner, snapshot)["source"]


def test_real_spliced_include_cannot_escape_and_keeps_physical_line_numbers(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    source = '#if 0\n#inc\\\nlude "../outside.hlsli"\n#endif\nfloat4 main():SV_Target0{float value=__LINE__;return value;}'
    mapped = _preprocess(runner, _snapshot(tmp_path, source))
    assert "value=5" in mapped["source"].replace(" ", "")
    outside = tmp_path / "outside.hlsli"
    outside.write_text("#error EXTERNAL_FILE_WAS_READ", encoding="utf-8")
    active = '#inc\\\nlude "' + outside.as_posix() + '"\nfloat4 main():SV_Target0{return 1;}'
    with pytest.raises(compile_module.CompilerError) as caught:
        runner.preprocess(active, "hlsl", "main", "ps_5_0", "d3d11")
    assert "__bread_unavailable_includes__" in str(caught.value)
    assert "EXTERNAL_FILE_WAS_READ" not in str(caught.value)


def test_real_profile_and_macro_defines_are_used_during_preprocessing(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    source = "float4 main():SV_Target0 {float value=VALUE+__SHADER_TARGET_MAJOR;return value;}"
    snapshot = _snapshot(tmp_path, source)
    mapped = _preprocess(runner, snapshot, flags=[("@cmdline", "-DVALUE=0.25 -HV 2018")])
    assert "0.25+5" in mapped["source"].replace(" ", "")


def test_real_vulkan_glsl_preprocessing_uses_native_vulkan_macro(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch, ("glslang",))
    source = "#version 450\n#if VULKAN != 100\n#error wrong environment\n#endif\nlayout(location=0) out vec4 color;void main(){color=vec4(1);}"
    flags = [("@cmdline", " --entry-point main --client vulkan100 --target-env vulkan1.0 --entry-point main"),
             ("@spirver", "spirv1.0")]
    assert "wrong environment" not in runner.preprocess(source, "glsl", "main", api="vulkan", flags=flags)


def test_real_dxc_spirv_preserves_exported_entry_and_binding_shift(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    source = "cbuffer C:register(b0){float4 tint;};float4 SourcePS():SV_Target0{return tint;}"
    result = runner.compile(source, "hlsl", "vulkan", "SourcePS", "ps_6_0", exported_entry="CapturedPS",
                            flags=[("@cmdline", "-fvk-use-dx-layout -fvk-b-shift 4 0 -Zpr")])
    assert result["encoding"] == "SPIRV"
    assert _spirv_entries(result["bytes"]) == ["CapturedPS"]
    words = struct.unpack("<%dI" % (len(result["bytes"]) // 4), result["bytes"])
    assert any(words[i] == (4 << 16 | 71) and words[i + 2:i + 4] == (33, 4) for i in range(5, len(words) - 3))


def test_real_dxc_dxil_container_is_validated_and_signed(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    result = runner.compile("float4 main():SV_Target0{return 0.25;}", "hlsl", "d3d12", "main", "ps_6_0",
                            flags=[("@cmdline", "/enable_unbounded_descriptor_tables")])
    assert result["encoding"] == "DXIL"
    assert result["bytes"][:4] == b"DXBC"
    assert result["bytes"][4:20] != bytes(16)
    assert "validation failed" not in result["diagnostics"].lower()


def test_real_glslang_opengl_spirv_retains_opengl_entry_environment(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch, ("glslang",))
    source = "#version 450\nlayout(location=0) out vec4 color;void main(){color=vec4(0.25,0,0,1);}"
    result = runner.compile(source, "glsl", "opengl", "main", target_encoding="OpenGLSPIRV", exported_entry="CapturedPS")
    assert result["encoding"] == "OpenGLSPIRV"
    assert _spirv_entries(result["bytes"]) == ["CapturedPS"]
