import os
from pathlib import Path
import sys
import json
import subprocess

import pytest

from replaylab_agent.connectors.codex_app_server import _JsonlProcess
from replaylab_agent.errors import ConnectorError


def test_windows_chinese_error_bytes_are_decoded_without_replacement(monkeypatch):
    from replaylab_agent import process_diagnostics as diagnostics
    monkeypatch.setattr(diagnostics, "_native_encodings", lambda: ["cp936"])
    text, encoding = diagnostics.decode_process_output(bytes.fromhex("c3fcc1eed0d0ccabb3a4a1a30d0a"))
    assert text == "命令行太长。\r\n"
    assert encoding == "cp936"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be"])
def test_native_stderr_encodings_preserve_unicode(encoding):
    from replaylab_agent.process_diagnostics import decode_process_output
    text = "Error: 中文目录 missing\r\n"
    assert decode_process_output(text.encode(encoding))[0] == text


def test_environment_report_records_lengths_without_secret_values(tmp_path):
    from replaylab_agent.process_diagnostics import startup_snapshot, write_startup_report
    environment = {"PATH": "C:\\safe;" * 1500, "OPENAI_API_KEY": "CANARY_API_SECRET", "OTHER_PRIVATE_DATA": "CANARY_PRIVATE_VALUE", "CODEX_HOME": str(tmp_path / "中文 Ω")}
    snapshot = startup_snapshot(["codex.exe", "app-server", "-c", "sensitive=CANARY_PRIVATE_VALUE"], tmp_path, environment)
    path = Path(write_startup_report(snapshot, environment=environment, exit_code=1,
        stderr=b"Authorization: Bearer CANARY_API_SECRET\n"))
    rendered = path.read_text(encoding="utf-8")
    assert "CANARY" not in rendered
    report = json.loads(rendered)
    assert report["pathEntryCount"] == 1501
    assert report["commandLineUtf16Units"] > 0
    assert report["environmentCount"] == 4
    assert report["executableKind"] == ".exe"
    assert report["exitCode"] == 1


@pytest.mark.asyncio
async def test_failed_process_has_a_durable_redacted_startup_report(tmp_path):
    process = _JsonlProcess((sys.executable, "-c", "import sys;sys.stderr.buffer.write('配置有误'.encode('utf-8'));sys.exit(1)"), cwd=tmp_path, env=dict(os.environ, OPENAI_API_KEY="test-only-canary"))
    try:
        with pytest.raises(ConnectorError, match="配置有误") as failure:
            await process.request("initialize", {}, timeout=5)
        assert "startup report:" in str(failure.value)
        reports = list((tmp_path / "startup-logs").glob("provider-startup-*.json"))
        assert len(reports) == 1
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        assert report["stderrEncoding"] == "utf-8"
        assert report["exitCode"] == 1
        assert "test-only-canary" not in reports[0].read_text(encoding="utf-8")
    finally:
        await process.close()


@pytest.mark.asyncio
async def test_requested_close_does_not_log_a_false_startup_failure(tmp_path):
    fixture = Path(__file__).parent / "fixtures/fake_codex_app_server.py"
    process = _JsonlProcess((sys.executable, str(fixture)), cwd=tmp_path, env=dict(os.environ))
    await process.request("fixture/echo", {"ok": True}, timeout=5)
    await process.close()
    assert not list((tmp_path / "startup-logs").glob("provider-startup-*.json"))


@pytest.mark.asyncio
async def test_npm_codex_bypasses_batch_limits_and_preserves_long_environment(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows process launcher")
    install = tmp_path / "install 中文 & 100%"
    package = install / "node_modules/@openai/codex"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"name":"@openai/codex"}')
    shim = install / "codex.cmd"
    shim.write_text('@echo off\r\nnode "%~dp0\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n', encoding="utf-8")
    native = package / "vendor/x86_64-pc-windows-msvc/codex/codex.exe"
    native.parent.mkdir(parents=True)
    source = tmp_path / "fake.cs"
    source.write_text('using System; class Fake { static void Main() { while(Console.ReadLine()!=null) { Console.WriteLine("{\\"id\\":1,\\"result\\":{\\"ok\\":true}}"); Console.Out.Flush(); } } }')
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    subprocess.run([str(compiler), "/nologo", "/target:exe", "/out:" + str(native), str(source)], check=True, capture_output=True)
    environment = dict(os.environ, PATH="C:\\somewhere;" * 1000, PROCESSOR_ARCHITECTURE="AMD64")
    process = _JsonlProcess((str(shim), "app-server", "-c", "test=" + "a" * 12000), cwd=tmp_path, env=environment)
    try:
        assert await process.request("initialize", {}, timeout=10) == {"ok": True}
        assert process._startup_snapshot["executable"] == str(native)
        assert process._effective_environment["PATH"] == environment["PATH"]
    finally:
        await process.close()


def test_long_mcp_configuration_uses_private_file_and_preserves_scope(tmp_path, monkeypatch):
    import tomllib
    from replaylab_agent.native_mcp import NativeMCPRoute, native_codex_argv
    from replaylab_agent.mcp_proxy import _expand_route_file
    scope = '{"description":"' + "unicode-中文-" * 5000 + '"}'
    arguments = ("-I", "-m", "replaylab_agent.mcp_proxy", "--scope-json", scope, "--allow", "read_evidence_file")
    monkeypatch.setenv("REPLAYLAB_TASK_ID", "old")
    route = NativeMCPRoute(sys.executable, arguments, {"REPLAYLAB_TASK_ID": "task-file"}, frozenset({"read_evidence_file"}), frozenset(), tmp_path / "registry.json")
    argv = native_codex_argv(["codex", "app-server"], route)
    assert len(subprocess.list2cmdline(argv)) < 6000
    table = tomllib.loads(argv[argv.index("app-server") - 1])["mcp_servers"]["Bread"]
    assert table["env"] == {}
    assert _expand_route_file(table["args"][3:]) == list(arguments[3:])
    assert os.environ["REPLAYLAB_TASK_ID"] == "task-file"


@pytest.mark.asyncio
async def test_actual_early_cli_exit_retains_reason_without_credentials(tmp_path):
    secret = "test-secret-value-only"
    command = [sys.executable, "-c",
        "import sys;sys.stderr.write('Error loading configuration: unsupported field; credential test-secret-value-only\\n');sys.stderr.flush();sys.exit(1)"]
    process = _JsonlProcess(tuple(command), cwd=tmp_path, env=dict(os.environ, OPENAI_API_KEY=secret))
    try:
        with pytest.raises(ConnectorError) as error:
            await process.request("initialize", {}, timeout=5)
        assert "unsupported field" in str(error.value)
        assert "code 1" in str(error.value)
        assert secret not in str(error.value)
    finally:
        await process.close()
