"""Regression coverage for actual guidance and configured-tool availability.

No model, capture, real vendor process or user configuration is used here.
The MCP subprocess fixture exercises the real protocol/dispatch boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from replaylab_agent.agent_context import ProviderContext
from replaylab_agent.agent_contracts import OrchestratorConfig, RunRequest
from replaylab_agent.agent_registry import CatalogView
from replaylab_agent.deep_engine import BreadMCPTools
from replaylab_agent.native_mcp import build_native_mcp_route
from replaylab_agent.policy import AGENT_LOCAL_TOOLS
from replaylab_agent.profiles import Profile
from replaylab_agent.skill_gateway import SkillGateway


def test_repair_metadata_tool_is_exposed_with_readonly_effects():
    from replaylab_agent.policy import READ_ONLY_TOOLS, tool_call_effects
    from replaylab_agent.mcp_proxy import _compatible_host_grant
    assert 'get_shader_repair_context' in READ_ONLY_TOOLS
    granted, diagnostics = _compatible_host_grant(['get_shader_repair_context'], frozenset({'get_shader_repair_context'}))
    assert granted == frozenset({'get_shader_repair_context'}) and not diagnostics
    assert tool_call_effects('get_shader_repair_context', {'workspace_path':'example'}) == frozenset()


def _skill_root(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    folder = root / "demo"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Example task guidance.\n"
        "metadata:\n  replaylab-version: '7'\n---\n## Acceptance\nInspect the saved result.\n",
        encoding="utf-8",
    )
    return root


def _servers(tmp_path: Path) -> tuple[dict, ...]:
    fixture = Path(__file__).parent / "fixtures" / "fake_partial_mcp_host.py"
    return (
        {"id": "enabled-user", "transport": "stdio", "command": sys.executable,
         "args": [str(fixture)], "env": {"API_KEY": "private-test-credential"}},
        {"id": "unavailable-user", "transport": "stdio", "command": str(tmp_path / "missing.exe")},
    )


@pytest.mark.parametrize("section,offset", [
    ("Missing section", 0), ("references/missing.md", 0), ("", True), ("Acceptance", -1),
])
def test_failed_skill_read_is_not_marked_loaded(tmp_path: Path, section: str, offset: int) -> None:
    emitted = []
    gateway = SkillGateway(CatalogView.load(skill_roots=[_skill_root(tmp_path)]), on_skill_read=emitted.append)
    failed = gateway.load("demo", section, offset)
    assert "error" in failed
    assert gateway.loaded_skill_ids == ()
    assert gateway.directory()["skillReadFacts"] == []
    assert emitted == []
    accepted = gateway.load("demo")
    assert gateway.loaded_skill_ids == ("demo",)
    receipt = accepted["readReceipt"]
    assert receipt["entryComplete"] is True
    assert receipt["instructionsSha256"] == hashlib.sha256(accepted["instructions"].encode()).hexdigest()
    assert emitted == [receipt]


def test_read_receipt_distinguishes_reference_page_from_entire_entry(tmp_path: Path) -> None:
    root = _skill_root(tmp_path)
    reference = root / "demo" / "references" / "large.md"
    reference.parent.mkdir()
    reference.write_text("Useful reference detail.\n" * 2500, encoding="utf-8")
    gateway = SkillGateway(CatalogView.load(skill_roots=[root]))
    page = gateway.load("demo", "references/large.md")
    assert page["hasMore"] is True
    assert page["readReceipt"]["section"] == "references/large.md"
    assert page["readReceipt"]["entryComplete"] is False
    assert page["readReceipt"]["deliveredChars"] == len(page["instructions"])
    assert gateway.directory()["skillReadFacts"] == [page["readReceipt"]]


@pytest.mark.parametrize("with_capture", [True, False])
def test_native_route_keeps_runtime_credentials_out_of_argv_and_vendor_roots(
    tmp_path: Path, with_capture: bool,
) -> None:
    workspace = tmp_path / "vendor-workspace"
    workspace.mkdir()
    host = tmp_path / "BreadMCP.exe"
    host.write_bytes(b"host")
    context = ProviderContext(
        profile=Profile(id="native", preset="codex", model="gpt-x"), model="gpt-x",
        instance_id="test-instance" if with_capture else "", context={}, intent=None,
        mcp_executable=host, task_id="native-external-route",
    )
    route = build_native_mcp_route(context, workspace=workspace, event_root=tmp_path / "events",
                                   allowed_effects=("replay",), mcp_servers=_servers(tmp_path))
    assert route is not None
    assert "private-test-credential" not in " ".join(route.args)
    assert "private-test-credential" not in route.codex_config_override()
    index = route.args.index("--mcp-server-file")
    config = Path(route.args[index + 1])
    assert config.parent == workspace.parent
    assert config not in workspace.rglob("*")
    assert json.loads(config.read_text(encoding="utf-8")) == list(_servers(tmp_path))
    if not with_capture:
        assert "--host" not in route.args
        assert route.allowed_tools == AGENT_LOCAL_TOOLS | {"read_evidence_file"}
        assert route.effects == frozenset()


@pytest.mark.asyncio
async def test_api_configured_external_mcp_remains_callable_without_capture(tmp_path: Path) -> None:
    config = OrchestratorConfig(workspace_base=tmp_path / "turns", external_mcp_servers=_servers(tmp_path))
    request = RunRequest(prompt="use the configured asset server", profile=Profile(
        id="api", preset="openai", model="gpt-x"), task_id="capture-free-api")
    session = BreadMCPTools(request, config, tmp_path / "events")
    async with session as tools:
        assert set(tools) == {"enabled-user__inspect_event", "enabled-user__new_host_tool", "read_evidence_file"}
        assert "unavailable-user" in session.failed_mcp_servers
        output = await tools["enabled-user__new_host_tool"].ainvoke({})
        assert '"authority":"user-configured-mcp-server"' in output
        assert '"tool": "new_host_tool"' in output
        assert "private-test-credential" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("with_capture", [True, False])
async def test_native_proxy_actually_dispatches_external_tools_without_host_effect_claims(
    tmp_path: Path, with_capture: bool,
) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    fixture = Path(__file__).parent / "fixtures" / "fake_partial_mcp_host.py"
    runtime_config = tmp_path / "private-runtime.json"
    runtime_config.write_text(json.dumps(_servers(tmp_path)), encoding="utf-8")
    args = ["-m", "replaylab_agent.mcp_proxy", "--mcp-server-file", str(runtime_config),
            "--skill-root", str(_skill_root(tmp_path))]
    for tool in AGENT_LOCAL_TOOLS:
        args.extend(("--allow", tool))
    if with_capture:
        args.extend(("--host", sys.executable, "--host-arg", str(fixture),
                     "--allow", "inspect_event", "--instance-id", "test-instance",
                     "--registry-path", str((tmp_path / "registry.json").resolve())))
    environment = dict(os.environ, REPLAYLAB_TASK_ID="external-proxy", REPLAYLAB_TASK_EVENT_ROOT=str(tmp_path / "events"))
    with (tmp_path / "proxy.stderr.log").open("w", encoding="utf-8") as errlog:
        async with stdio_client(StdioServerParameters(command=sys.executable, args=args, env=environment), errlog=errlog) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                names = {tool.name for tool in (await client.list_tools()).tools}
                assert names == AGENT_LOCAL_TOOLS | {"read_evidence_file"} | ({"inspect_event"} if with_capture else set())
                directory = json.loads((await client.call_tool("list_bread_capabilities", {})).content[0].text)
                assert "enabled-user__new_host_tool" in directory["userMcpTools"]
                assert next(server for server in directory["mcpServers"] if server["id"] == "unavailable-user")["enabled"] is False
                contract = json.loads((await client.call_tool("list_bread_capabilities", {"tool": "enabled-user__new_host_tool"})).content[0].text)["tool"]
                assert contract["authority"] == "user-configured-mcp-server"
                assert "effects" not in contract and "hostEffectGrant" not in contract
                result = await client.call_tool("invoke_bread_tool", {"name": contract["name"], "arguments": {}})
                assert not result.isError, result.content
                assert json.loads(result.content[0].text)["externalMcp"]["server"] == "enabled-user"
                assert json.loads(result.content[1].text)["tool"] == "new_host_tool"
                denied = await client.call_tool("invoke_bread_tool", {"name": "not-configured__new_host_tool", "arguments": {}})
                assert denied.isError
                loaded = json.loads((await client.call_tool("load_bread_skill", {"skill_id": "demo"})).content[0].text)
                assert loaded["readReceipt"]["entryComplete"] is True
    events = [json.loads(line) for line in (tmp_path / "events" / "external-proxy.jsonl").read_text(encoding="utf-8").splitlines()]
    receipt = next(event["data"]["skillReadReceipt"] for event in events if event["data"].get("phase") == "skill-read")
    assert receipt["skillId"] == "demo"
    assert "private-test-credential" not in json.dumps(events)


@pytest.mark.asyncio
async def test_native_cleanup_does_not_restart_configured_external_servers(tmp_path: Path, monkeypatch) -> None:
    from replaylab_agent.native_engine import NativeAgentEngine

    seen = []
    releases = []

    class CleanupSession:
        def __init__(self, request, config, event_root):
            seen.append(config.external_mcp_servers)

        async def __aenter__(self):
            async def release(**arguments):
                releases.append(arguments)
            return {"release_task_shader_occupancy": release}

        async def __aexit__(self, *args):
            pass

    class Connector:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            return SimpleNamespace(text="Result saved.")

        async def close(self):
            pass

    monkeypatch.setattr("replaylab_agent.native_engine.build_native_mcp_route", lambda *args, **kwargs: None)
    monkeypatch.setattr("replaylab_agent.deep_engine.BreadMCPTools", CleanupSession)
    config = OrchestratorConfig(event_root=tmp_path / "events", log_root=tmp_path / "logs",
                                workspace_base=tmp_path / "turns", external_mcp_servers=_servers(tmp_path))
    engine = NativeAgentEngine(catalog=CatalogView.load(skill_roots=[]), config=config,
                               codex_connector_factory=Connector)
    result = await engine.run(RunRequest(prompt="organize the authorized shader", instance_id="test-instance",
                                         task_id="native-cleanup", profile=Profile(id="native", preset="codex")))
    # This fake returns prose without doing source work or recording a task
    # review. Cleanup still runs, but cannot turn it into completed work.
    assert result.terminal_status == "partial"
    assert seen == [()]
    assert len(config.external_mcp_servers) == 2
    assert releases == [{"rollback_incomplete_replacement": False}]
