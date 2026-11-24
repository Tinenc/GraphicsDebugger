import asyncio
import json
from types import SimpleNamespace

import pytest
from replaylab_agent import cli
from replaylab_agent.doctor import DoctorCheck
from replaylab_agent.profiles import Profile


def test_doctor_streams_pending_and_finished_rows_before_each_slow_probe(monkeypatch, capsys):
    from replaylab_agent import connection_readiness
    from replaylab_agent import portable_capabilities
    monkeypatch.setattr(portable_capabilities, "manifest", lambda: {"installed": True})
    events = []
    def captured():
        events.extend(json.loads(line) for line in capsys.readouterr().out.splitlines())
        return events
    def doctor(profile, catalog, **kwargs):
        rows = [DoctorCheck("model-context", "error", "unknown model window")]
        for row in rows:
            kwargs["progress_callback"](row)
        return rows
    async def local_probe():
        assert captured()[-1]["check"]["status"] == "running"
        return DoctorCheck("portable-tool-execution", "ok", "created and read documents")
    async def host_probe(_):
        assert captured()[-1]["check"]["name"] == "bread-host-execution"
        return DoctorCheck("bread-host-execution", "ok", "round trip")
    async def model_probe(*args, **kwargs):
        assert captured()[-1]["check"]["name"] == "model-tool-execution"
        return [DoctorCheck("model-tool-execution", "ok", "model called MCP")]
    monkeypatch.setattr(cli, "_selected_profile", lambda *a: Profile("x", "codex"))
    monkeypatch.setattr(cli, "_catalog", lambda *a: None)
    monkeypatch.setattr(cli, "_tools_config", lambda *a: None)
    monkeypatch.setattr(cli, "run_doctor", doctor)
    monkeypatch.setattr(connection_readiness, "check_portable_tools", local_probe)
    monkeypatch.setattr(connection_readiness, "check_host_tools", host_probe)
    monkeypatch.setattr(connection_readiness, "run_execution_check", model_probe)
    args = SimpleNamespace(jsonl=True, profile="x", mcp_host=None, execution_test=True, timeout=3)
    assert cli._doctor_command(args, None) == 1  # Unknown budget remains a separate error.
    captured()
    assert events[-1]["type"] == "complete"
    rows = [e for e in events if e["type"] == "check"]
    assert [e["index"] for e in rows] == [0, 1, 1, 2, 2, 3, 3]
    assert len(events[-1]["checks"]) == 4


def test_core_connection_check_needs_no_optional_document_server(monkeypatch, capsys):
    from replaylab_agent import connection_readiness, portable_capabilities
    monkeypatch.setattr(portable_capabilities, "manifest", lambda: {})
    monkeypatch.setattr(cli, "_selected_profile", lambda *a: Profile("x", "codex"))
    monkeypatch.setattr(cli, "_catalog", lambda *a: None)
    monkeypatch.setattr(cli, "_tools_config", lambda *a: None)
    monkeypatch.setattr(cli, "run_doctor", lambda *a, **k: [])
    async def unavailable():
        raise AssertionError("Core cannot require Office/document extensions")
    async def host(_):
        return DoctorCheck("bread-host-execution", "ok", "round trip")
    async def model(*a, **k):
        return [DoctorCheck("model-tool-execution", "ok", "real tool receipt")]
    monkeypatch.setattr(connection_readiness, "check_portable_tools", unavailable)
    monkeypatch.setattr(connection_readiness, "check_host_tools", host)
    monkeypatch.setattr(connection_readiness, "run_execution_check", model)
    assert cli._doctor_command(SimpleNamespace(jsonl=True, profile="x", mcp_host=None,
        execution_test=True, timeout=3), None) == 0
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert [r["name"] for r in result["checks"]] == ["bread-host-execution", "model-tool-execution"]


@pytest.mark.asyncio
async def test_codex_browser_rejection_retains_the_actual_provider_reason(monkeypatch, tmp_path):
    from replaylab_agent.connectors import subscription
    class Rejected:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def account_read(self): return {"account": None}
        async def login_start(self, **kwargs):
            return {"loginId": "login-x", "authUrl": "https://auth.example.test/login"}
        async def wait_login(self, *args, **kwargs):
            return {"success": False, "error": {"message": "403: access denied"}}
    monkeypatch.setattr(subscription, "resolve_agent_executable", lambda *a, **k: "C:/trusted/codex.exe")
    events = []
    backend = subscription.SubscriptionBackend(Profile("test", "codex"), timeout=3,
        emit=events.append, environ={"LOCALAPPDATA": str(tmp_path)},
        codex_factory=lambda *a: Rejected())
    result = await backend.connect("browser", asyncio.Event())
    assert not result.authenticated
    assert result.message_id == "subscription.codex-login-failed-detail"
    assert result.message_args == ("403: access denied",)
    assert events[1]["messageArgs"] == ["403: access denied"]
