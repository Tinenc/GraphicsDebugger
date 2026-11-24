from __future__ import annotations

from contextlib import AsyncExitStack
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from replaylab_agent import portable_capabilities as caps
from replaylab_agent import connection_readiness as readiness
from replaylab_agent.portable_tools import document, execute


@pytest.mark.parametrize("generation_length", [20, 64])
def test_source_checkout_discovers_restored_capability_skills(tmp_path, monkeypatch, generation_length):
    monkeypatch.delenv("BREAD_CAPABILITIES_ROOT", raising=False)
    monkeypatch.setenv("BREAD_APP_ROOT", str(tmp_path))
    monkeypatch.setattr(caps, "__file__", str(tmp_path / "src/replaylab_agent/portable_capabilities.py"))
    digest = "0123456789abcdef" * 4
    lock = tmp_path / "dependencies/inputs.lock.json"
    lock.parent.mkdir()
    lock.write_text(json.dumps({"archives": [{"id": "capabilities", "sha256": digest}]}), encoding="utf-8")
    restored = tmp_path / "output/build-inputs/expanded/capabilities" / digest[:generation_length]
    skills = restored / "custom-skills"
    skills.mkdir(parents=True)
    manifest = {"schema": "bread-portable-capabilities/v1", "skillRoots": ["custom-skills"]}
    (restored / "bread-capabilities.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert caps.bundle_root() == restored
    assert caps.skill_roots() == (skills,)


def test_docs_runtime_is_ready_without_office_or_bundled_browser(tmp_path, monkeypatch):
    monkeypatch.setenv('BREAD_CAPABILITIES_ROOT', str(tmp_path))
    paths = ['python/python.exe', 'node/bin/node.exe'] + [
        'python/Lib/site-packages/' + name + '/__init__.py' for name in ('docx','openpyxl','pptx','pypdf','playwright')]
    for name in paths:
        path = tmp_path/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'fixture')
    (tmp_path/'bread-capabilities.json').write_text(json.dumps({
        'schema':'bread-portable-capabilities/v1','distributionProfile':'standard',
        'features':{'office':False,'browser':False},'files':[]}))
    assert caps.public_status()['ready']
    (tmp_path / paths[2]).unlink()
    assert not caps.public_status()['ready']


def test_system_browser_discovery_does_not_depend_on_path_or_working_directory(tmp_path):
    from replaylab_agent.portable_desktop import system_browser
    exe = tmp_path/'Microsoft/Edge/Application/msedge.exe'
    exe.parent.mkdir(parents=True); exe.write_bytes(b'fixture')
    assert system_browser({'ProgramFiles':str(tmp_path)}) == str(exe.resolve())
    assert system_browser({'PATH': str(exe.parent), 'BREAD_BROWSER_EXECUTABLE':'msedge.exe'}) == ''


def test_capability_roots_cannot_escape_and_manifest_refreshes(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAD_CAPABILITIES_ROOT", str(tmp_path))
    (tmp_path / "skills").mkdir()
    value = {"schema": "bread-portable-capabilities/v1", "skillRoots": ["skills", "../outside"], "plugins": []}
    path = tmp_path / "bread-capabilities.json"
    path.write_text(json.dumps(value))
    assert tmp_path / "skills" in caps.skill_roots()
    assert all(p != tmp_path.parent / "outside" for p in caps.skill_roots())
    assert caps.manifest()["plugins"] == []
    value["plugins"] = [{"id": "new", "path": "plugins/new"}]
    path.write_text(json.dumps(value))
    assert caps.manifest()["plugins"] == value["plugins"]


def test_native_plugin_presence_is_not_service_readiness(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAD_CAPABILITIES_ROOT", str(tmp_path))
    monkeypatch.delenv("CODEX_APP_TOOLS_PIPE_PATH", raising=False)
    monkeypatch.delenv("SKY_CUA_NATIVE_PIPE_DIRECTORY", raising=False)
    assert caps.provider_servers() == ()


def test_native_clients_retain_action_permissions_and_explicit_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAD_CAPABILITIES_ROOT", str(tmp_path))
    monkeypatch.setenv("CODEX_APP_TOOLS_PIPE_PATH", "live-test-service")
    code = tmp_path / "client/server.mjs"
    code.parent.mkdir();code.write_text("public client code")
    (tmp_path / "bread-capabilities.json").write_text(json.dumps({"schema": "bread-portable-capabilities/v1",
        "plugins": [{"id": "codex-app-tools@test", "path": "client"}]}))
    server = caps.provider_servers()[0]
    assert server["approvalRules"]["tools"]["send_message_to_thread"]["approval_mode"] == "prompt"
    assert caps.provider_servers(excluded_ids={"codex_app"}) == ()
    policy = {"default": "deny", "tools": {}}
    assert caps.provider_servers(policies={"codex_app": policy})[0]["approvalRules"] == policy


@pytest.mark.asyncio
async def test_model_claim_without_tool_receipt_fails(monkeypatch):
    async def claims_success(*args):
        return "OK, everything worked"
    monkeypatch.setattr(readiness, "_model_probe", claims_success)
    result = await readiness.run_execution_check(object())
    assert result[0].status == "error"
    assert "without-mcp-execution-receipt" in result[0].detail


@pytest.mark.asyncio
@pytest.mark.parametrize("busy_attempts,model_success", [(2, True), (4, True), (4, False)])
async def test_probe_cleanup_preserves_the_model_result(monkeypatch, tmp_path, busy_attempts, model_success):
    workspace = tmp_path / "probe"
    workspace.mkdir()
    monkeypatch.setattr(readiness.tempfile, "mkdtemp", lambda **_: str(workspace))
    attempts, warnings = [], []
    real_remove = readiness.shutil.rmtree

    def remove(path):
        attempts.append(path)
        if len(attempts) <= busy_attempts:
            error = PermissionError("directory still in use")
            error.winerror = 32
            raise error
        real_remove(path)

    async def probe(profile, server, challenge, workspace, timeout):
        if not model_success:
            raise RuntimeError("actual-probe-failure")
        import hashlib
        (workspace / "probe-receipt.json").write_text(json.dumps({
            "challengeSha256": hashlib.sha256(challenge.encode()).hexdigest(), "tool": "bread_connection_probe"}))

    monkeypatch.setattr(readiness.shutil, "rmtree", remove)
    monkeypatch.setattr(readiness, "_model_probe", probe)
    monkeypatch.setattr(readiness, "get_logger", lambda: SimpleNamespace(warning=lambda *args: warnings.append(args)))
    checks = await readiness.run_execution_check(object())
    assert checks[0].status == ("ok" if model_success else "error")
    if not model_success:
        assert "actual-probe-failure" in checks[0].detail
    assert len(attempts) == min(4, busy_attempts + 1)
    assert workspace.exists() == (busy_attempts == 4)
    assert len(warnings) == (1 if busy_attempts == 4 else 0)


@pytest.mark.asyncio
async def test_actual_probe_mcp_writes_verifiable_receipt(tmp_path):
    from replaylab_agent.external_mcp import ExternalMCPPool
    receipt = tmp_path / "proof.json"
    async with AsyncExitStack() as stack:
        pool = ExternalMCPPool()
        await pool.attach([readiness._route(receipt)], stack)
        result = await pool.call("bread-check__bread_connection_probe", {"challenge": "a" * 64})
        assert not result.isError
        assert readiness._proved(receipt, "a" * 64)
        assert not readiness._proved(receipt, "b" * 64)


@pytest.mark.asyncio
async def test_api_route_completes_actual_mcp_roundtrip(monkeypatch):
    import re
    from replaylab_agent.connectors import langchain_models
    class Model:
        calls = 0
        def bind_tools(self, specs):
            self.name = specs[0]["name"]
            return self
        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                challenge = re.search(r"challenge=([a-f0-9]{64})", messages[0].content).group(1)
                import hashlib
                self.expected_proof = hashlib.sha256(challenge.encode()).hexdigest()
                return SimpleNamespace(tool_calls=[{"name": self.name, "args": {"challenge": challenge}, "id": "check"}], content="")
            assert messages[-1].tool_call_id == "check"
            assert self.expected_proof in messages[-1].content
            return SimpleNamespace(content="completed")
    model = Model()
    monkeypatch.setattr(langchain_models, "build_chat_model", lambda *args, **kwargs: model)
    checks = await readiness.run_execution_check(SimpleNamespace(provider=SimpleNamespace(kind="model")))
    assert checks[0].status == "ok", checks[0].detail
    assert model.calls == 2


@pytest.mark.asyncio
async def test_portable_document_mcp_roundtrip_has_no_stdin_deadlock():
    if not (caps.bundle_root() / "python/python.exe").exists():
        pytest.skip("Portable dependency bundle is exercised by the release smoke")
    import asyncio
    result = await asyncio.wait_for(readiness.check_portable_tools(), timeout=30)
    assert result.status == "ok", result.detail


def test_utf8_documents_and_bounded_script_execution(tmp_path):
    if not (caps.bundle_root() / "python/python.exe").exists():
        pytest.skip("Portable dependency bundle is exercised by the release smoke")
    path = str(tmp_path / "中文.docx")
    assert document("create", path, {"title": "测试"})["exitCode"] == 0
    inspected = document("inspect", path)
    assert "测试" in inspected["stdout"]
    assert "\ufffd" not in inspected["stdout"]
    assert execute("python", "import time;time.sleep(10)", str(tmp_path), 1)["timedOut"]
