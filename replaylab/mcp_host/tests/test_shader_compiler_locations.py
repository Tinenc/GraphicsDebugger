"""Compiler locations must survive native/fallback compilation and panel reload."""
import hashlib
from pathlib import Path

import pytest

from src.shader_workspace.diagnostics import parse_compiler_diagnostics
from src.shader_workspace.watcher import _compiler_diagnostics
from tests.test_shader_console import console


@pytest.mark.parametrize("message,file,line,column", [
    ("ERROR: source0.glsl:1782: 'float4' : no matching overloaded function found", "input.glsl", 1782, 1),
    ("ERROR: 0:24: 'roughness' : undeclared identifier", "input.glsl", 24, 1),
    ("0:31(9): error: syntax error, unexpected identifier", "input.glsl", 31, 9),
    ("ERROR: C:\\project\\input.glsl:12:7: 'float4' : syntax error", "C:\\project\\input.glsl", 12, 7),
    ("input.hlsl:8:3: error: use of undeclared identifier 'value'", "input.hlsl", 8, 3),
    ("input.hlsl(22,4): error X3000: syntax error", "input.hlsl", 22, 4),
    ("ERROR: 1:12: undeclared identifier", "1", 12, 1),
])
def test_target_compiler_locations(message, file, line, column):
    result = parse_compiler_diagnostics(message, "input.glsl")
    assert len(result) == 1
    assert (result[0]["file"], result[0]["line"], result[0]["column"]) == (file, line, column)
    assert result[0]["severity"] == "error"


def test_glslang_stdout_survives_save_watcher_and_console(tmp_path):
    source = tmp_path / "source" / "shader.glsl"
    source.parent.mkdir()
    source.write_text("void main() { float4(1); }", encoding="utf8")
    job = tmp_path / "jobs" / "compile"
    job.mkdir(parents=True)
    # glslang uses stdout, while the exit-status exception only names the job.
    output = f"ERROR: {job / 'input.glsl'}:1782: 'float4' : no matching overloaded function found\n"
    (job / "stderr.log").write_text("", encoding="utf8")
    (job / "stdout.log").write_text(output, encoding="utf8")
    diagnostics = _compiler_diagnostics(tmp_path, source, RuntimeError(f"external tool exited with code 2; see {job}"))
    assert len(diagnostics) == 1
    assert diagnostics[0]["line"] == 1782
    assert Path(diagnostics[0]["file"]) == source
    assert "float4" in diagnostics[0]["message"]
    rows = console.console_entries_from_audits(str(tmp_path), str(source), watch={"events": [
        {"sequence": 1, "type": "compileFailed", "message": "compiler exited", "diagnostics": diagnostics}]})
    assert rows[0]["line"] == 1782
    assert Path(rows[0]["sourceFile"]) == source
    assert "float4" in rows[0]["text"]


def test_saved_manifest_reads_stdout_and_deduplicates_streams(tmp_path):
    source = tmp_path / "source" / "shader.glsl"
    source.parent.mkdir()
    source.write_text("bad", encoding="utf8")
    job = tmp_path / "jobs" / "compile"
    job.mkdir(parents=True)
    output = "ERROR: source0.glsl:18: 'float4' : no matching overloaded function found\n"
    for name in ("stderr.log", "stdout.log"):
        (job / name).write_text(output, encoding="utf8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    history = {"history": [{"operation": "compile-route-chain", "status": "failed", "hops": [
        {"operation": "compile", "status": "failed", "inputSha256": digest,
         "stderrPath": "jobs/compile/stderr.log", "stdoutPath": "jobs/compile/stdout.log"}]}]}
    result = console._manifest_compile_diagnostics(history, str(tmp_path), str(source))
    assert len(result[digest]) == 1
    assert result[digest][0]["line"] == 18
    assert Path(result[digest][0]["sourceFile"]) == source


@pytest.mark.parametrize("message", [
    "ERROR: source0.glsl:1782: 'float4' : no matching overloaded function found",
    "ERROR: 0:1782: 'float4' : no matching overloaded function found",
    "0:1782(5): error: unknown type 'float4'",
])
def test_console_native_glsl_locations(message, tmp_path):
    source = tmp_path / "source" / "shader.glsl"
    result = console.parse_compiler_diagnostics(message, str(source), str(tmp_path))
    assert len(result) == 1
    assert result[0]["line"] == 1782
    assert "float4" in result[0]["message"]
    assert Path(result[0]["sourceFile"]) == source


@pytest.mark.parametrize("reported", ["source-input/01.hlsli", "input.glsl"])
def test_job_include_or_later_language_error_is_not_main_source(tmp_path, reported):
    source = tmp_path / "source" / "shader.hlsl"
    job = tmp_path / "jobs" / "compile"
    job.mkdir(parents=True)
    diagnostic_file = job / reported
    (job / "stdout.log").write_text(f"{diagnostic_file}:91:4: error: invalid value", encoding="utf8")
    records = _compiler_diagnostics(tmp_path, source, RuntimeError(f"external tool exited; see {job}"))
    assert records[0]["file"] == str(diagnostic_file)
    rows = console.console_entries_from_audits(str(tmp_path), str(source), watch={"events": [
        {"sequence": 1, "type": "compileFailed", "diagnostics": records}]})
    assert rows[0]["sourceFile"] == ""
    assert rows[0]["line"] == 91


def test_manifest_diagnostics_cannot_load_external_log(tmp_path):
    source = tmp_path / "source" / "shader.glsl"
    digest = "a" * 64
    log = tmp_path / "outside.log"
    log.write_text("ERROR: 0:99: untrusted error", encoding="utf8")
    result = console._manifest_compile_diagnostics({"history": [{"operation": "compile", "status": "failed",
        "inputSha256": digest, "stdoutPath": "outside.log"}]}, str(tmp_path), str(source))
    assert result == {}


def test_link_failure_keeps_compiler_reason_without_fake_location(tmp_path):
    source = tmp_path / "source" / "shader.glsl"
    job = tmp_path / "jobs" / "compile"
    job.mkdir(parents=True)
    (job / "stdout.log").write_text("ERROR: Linking fragment stage: Missing entry point\n", encoding="utf8")
    result = _compiler_diagnostics(tmp_path, source, RuntimeError(f"external tool exited with code 2; see {job}"))
    assert result[0]["line"] is None
    assert "Missing entry point" in result[0]["message"]
    parsed = console.parse_compiler_diagnostics("ERROR: Linking fragment stage: Missing entry point", str(source))
    assert parsed[0]["line"] == 0
    assert not parsed[0]["sourceFile"]
