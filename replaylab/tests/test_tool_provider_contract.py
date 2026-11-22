"""One tool list, two readers of it: Settings writes it, the Host calls what it holds.

An MCP server and a command-line extractor are the same thing wearing different
connections, so they live in one file with one validator. Both packages carry that
validator — they are packaged separately and cannot import each other — so this holds
them to the same answers. A tool the panel accepts and the Host drops would be a tool
the user believes in and the AI never calls.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from replaylab_agent import agent_tool_providers as agent


def _host_module():
    source = Path(__file__).resolve().parents[1] / "mcp_host" / "src" / "tool_providers.py"
    if not source.is_file():
        pytest.skip("the Host source tree is not checked out beside the Agent")
    spec = importlib.util.spec_from_file_location("_host_tool_providers", source)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


STDIO = {"id": "uex", "title": "uex", "roles": ["general", "asset-reader"],
         "connection": {"kind": "mcp-stdio", "command": "C:/tools/uex.exe", "args": ["mcp"]}}
HTTP = {"id": "remote", "connection": {"kind": "mcp-http", "url": "https://mcp.example.com/mcp",
                                       "bearerEnv": "MCP_TOKEN",
                                       "headers": {"X-Tenant": "acme"}}}
CLI = {"id": "studio", "roles": ["asset-reader"],
       "connection": {"kind": "cli", "command": "C:/tools/AssetStudioCLI.exe"}}

REJECTED = (
    {"connection": {"kind": "cli", "command": "x"}},                    # no id
    {"id": "Bad Id", "connection": {"kind": "cli", "command": "x"}},    # id shape
    {"id": "a", "connection": {"kind": "cli"}},                         # no command
    {"id": "a", "connection": {"kind": "carrier-pigeon", "command": "x"}},
    {"id": "a", "connection": {"kind": "mcp-http"}},                    # no url
    {"id": "a", "connection": {"kind": "mcp-http", "url": "ftp://example.com"}},
    # Plain HTTP off-box would put a bearer token on the wire in clear text.
    {"id": "a", "connection": {"kind": "mcp-http", "url": "http://example.com/mcp"}},
    {"id": "a", "roles": ["admin"], "connection": {"kind": "cli", "command": "x"}},
    {"id": "replaylab", "connection": {"kind": "cli", "command": "x"}},
)


def test_both_validators_accept_the_same_registrations() -> None:
    host = _host_module()
    for raw in (STDIO, HTTP, CLI):
        assert agent.validate_tool(raw) == host.validate_tool(raw), raw


def test_both_validators_reject_the_same_registrations() -> None:
    host = _host_module()
    for raw in REJECTED:
        with pytest.raises(agent.ToolConfigError):
            agent.validate_tool(raw)
        with pytest.raises(host.ToolConfigError):
            host.validate_tool(raw)


def test_loopback_http_is_allowed_because_it_cannot_leave_the_machine() -> None:
    host = _host_module()
    local = {"id": "local", "connection": {"kind": "mcp-http", "url": "http://localhost:7331/mcp"}}
    assert agent.validate_tool(local) == host.validate_tool(local)


def test_the_file_settings_writes_is_the_file_the_host_reads(tmp_path: Path) -> None:
    host = _host_module()
    registry = tmp_path / "tools.json"
    agent.upsert_tool(dict(STDIO), registry)
    agent.upsert_tool(dict(CLI), registry)
    written = json.loads(registry.read_text(encoding="utf-8"))
    assert written["schema"] == host.SCHEMA == agent.SCHEMA
    loaded, diagnostics = host.load_tools(registry)
    assert [tool["id"] for tool in loaded] == ["uex", "studio"]
    assert diagnostics == []


RICH_OPERATION = {
    "role": "general", "kind": "cli", "name": "extract_pak",
    "description": "Extract one .pak into a directory.",
    "argv": ["{program}", "--in", "{archive}", "--out", "{outputDir}"],
    "inputSchema": {"type": "object",
                    "properties": {"archive": {"type": "string", "description": "the .pak"}},
                    "required": ["archive"]},
    "result": {"kind": "files-in-output-dir", "successWhen": {"exitCode": 0, "minFiles": 1}},
    "knownFailures": [{"case": "SkeletalMesh", "symptom": "this build crashes"}],
    "proved": {"sample": "E:/game/Content.pak", "producedFiles": 12,
               "programSha256": "a" * 64, "sampleSha256": "b" * 64},
}


def test_both_validators_keep_the_same_operation_fields() -> None:
    """The Agent writes this file; the Host reads it. A field only one of them
    understands is a field the other silently deletes on the next edit."""

    host = _host_module()
    mine = agent.validate_operation(dict(RICH_OPERATION))
    theirs = host.validate_operation(dict(RICH_OPERATION))
    mine.pop("proved", None).pop("at", None)
    theirs.pop("proved", None).pop("at", None)
    assert mine == theirs


def test_engine_capability_metadata_survives_both_registry_writers(tmp_path: Path) -> None:
    host = _host_module()
    operation = {**RICH_OPERATION, "role": "asset-reader", "engines": ["unity"],
                 "capabilities": ["properties", "references"],
                 "observedCapabilities": ["properties"]}
    raw = {**CLI, "engines": ["unity"], "interface": {"operations": [operation]}}
    registry = tmp_path / "tools.json"
    agent.save_tools([raw], registry)
    loaded, diagnostics = host.load_tools(registry)
    assert not diagnostics
    assert loaded[0]["engines"] == ["unity"]
    assert loaded[0]["interface"]["operations"][0]["capabilities"] == ["properties", "references"]
    host.save_tools(loaded, registry)
    loaded_again, diagnostics = agent.load_tools(registry)
    assert not diagnostics
    assert loaded_again[0]["interface"]["operations"][0]["observedCapabilities"] == ["properties"]


def test_a_discovered_recipe_survives_settings_rewriting_the_file(tmp_path: Path) -> None:
    """Settings re-saving a tool must not throw away what discovery cost.

    The Host records a named operation with a typed schema and its known
    failures. If the Agent-side validator does not understand those fields, the
    next edit in Settings drops them and the AI works the same program out again.
    """

    registry = tmp_path / "tools.json"
    agent.upsert_tool(dict(CLI), registry)
    agent.record_interface("studio", {"source": "cli/probe",
                                      "operations": [dict(RICH_OPERATION)]}, registry)
    edited = dict(CLI)
    edited["description"] = "Unity"
    agent.upsert_tool(edited, registry)

    host = _host_module()
    tools, diagnostics = host.load_tools(registry)
    assert diagnostics == []
    operation = tools[0]["interface"]["operations"][0]
    assert operation["name"] == "extract_pak"
    assert operation["inputSchema"]["required"] == ["archive"]
    assert operation["result"]["kind"] == "files-in-output-dir"
    assert operation["knownFailures"][0]["case"] == "SkeletalMesh"
    assert operation["proved"]["producedFiles"] == 12


def test_an_operation_nobody_watched_work_is_not_offered(tmp_path: Path) -> None:
    """The whole point of the interface record: proof, not a guess."""

    host = _host_module()
    unproved = dict(CLI)
    unproved["interface"] = {"operations": [
        {"role": "asset-reader", "kind": "cli", "argv": ["{path}"], "patterns": ["*.bin"]}]}
    tool = agent.validate_tool(unproved)
    assert tool["interface"]["operations"][0]["proved"] is None
    assert agent.asset_reader_operation(tool) is None
    assert host.asset_reader_operation(tool) is None

    proved = dict(unproved)
    proved["interface"] = {"operations": [
        {"role": "asset-reader", "kind": "cli", "argv": ["{path}"], "patterns": ["*.bin"],
         "proved": {"sample": "Content/a.bin"}}]}
    assert agent.asset_reader_operation(agent.validate_tool(proved)) is not None


def test_a_discovered_interface_survives_re_registering_the_same_connection(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "tools.json"
    agent.upsert_tool(dict(CLI), registry)
    agent.record_interface("studio", {
        "source": "cli/probe",
        "operations": [{"role": "asset-reader", "kind": "cli", "argv": ["{path}"],
                        "patterns": ["*.assets"], "proved": {"sample": "a.assets"}}],
    }, registry)

    # Editing the description must not throw away what discovery cost.
    same = dict(CLI)
    same["description"] = "Unity"
    agent.upsert_tool(same, registry)
    tools, _ = agent.load_tools(registry)
    assert agent.asset_reader_operation(tools[0]) is not None

    # Pointing it at a different program must, because the interface was that one's.
    moved = dict(CLI)
    moved["connection"] = {"kind": "cli", "command": "D:/other/AssetStudioCLI.exe"}
    agent.upsert_tool(moved, registry)
    tools, _ = agent.load_tools(registry)
    assert tools[0]["interface"]["operations"] == []


def test_proving_an_asset_reader_operation_grants_the_role(tmp_path: Path) -> None:
    registry = tmp_path / "tools.json"
    agent.upsert_tool({"id": "uex", "roles": ["general"],
                       "connection": {"kind": "mcp-stdio", "command": "uex.exe", "args": ["mcp"]}},
                      registry)
    updated = agent.record_interface("uex", {
        "source": "mcp/tools-list",
        "operations": [{"role": "asset-reader", "kind": "mcp", "name": "preview_asset",
                        "pathArgument": "path", "proved": {"sample": "Content/a.uasset"}}],
    }, registry)
    assert "asset-reader" in updated["roles"]


def test_the_runtime_gets_both_transports_in_the_shape_the_engines_expect(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "tools.json"
    executable = tmp_path / "uex.exe"
    executable.write_bytes(b"present")
    agent.upsert_tool({
        **STDIO,
        "connection": {**STDIO["connection"], "command": str(executable)},
    }, registry)
    agent.upsert_tool({**HTTP, "roles": ["general"]}, registry)
    agent.upsert_tool(dict(CLI), registry)
    tools, _ = agent.load_tools(registry)
    servers = {server["id"]: server for server in agent.runtime_servers(tools)}
    # A command-line tool is not an MCP server and is never attached as one.
    assert set(servers) == {"uex", "remote"}
    assert servers["uex"]["transport"] == "stdio"
    assert servers["uex"]["args"] == ["mcp"]
    assert servers["remote"]["transport"] == "http"
    assert servers["remote"]["url"] == "https://mcp.example.com/mcp"
    assert servers["remote"]["bearerEnv"] == "MCP_TOKEN"
    assert servers["remote"]["headers"] == {"X-Tenant": "acme"}


def test_missing_optional_program_is_hidden_from_runtime_catalog(tmp_path: Path) -> None:
    missing = agent.validate_tool({
        "id": "missing",
        "roles": ["general", "asset-reader"],
        "connection": {
            "kind": "mcp-stdio", "command": str(tmp_path / "no.exe")
        },
        "interface": {"operations": [{
            "role": "asset-reader",
            "kind": "mcp",
            "name": "read_asset",
            "proved": {"sample": "a.uasset"},
        }]},
    })
    assert agent.tool_runtime_status(missing)[0] is False
    assert agent.runtime_servers([missing]) == ()


def test_the_old_two_registries_are_folded_into_one_list(tmp_path: Path) -> None:
    from replaylab_agent import agent_asset_readers
    from replaylab_agent.agent_tools_config import AgentToolsConfig

    readers = tmp_path / "asset-readers.json"
    agent_asset_readers.upsert_reader(
        {"id": "legacy", "command": "C:/tools/legacy.exe", "args": ["{path}"],
         "patterns": ["*.pak"], "note": "old"}, readers)
    agent_tools = tmp_path / "agent-tools.json"
    config = AgentToolsConfig()
    config.upsert_mcp_server({"id": "old-server", "command": "server.exe", "args": ["--stdio"]})
    config.save(agent_tools)

    registry = tmp_path / "tools.json"
    report = agent.migrate_legacy(readers, agent_tools, registry)
    assert sorted(report["imported"]) == ["legacy", "old-server"]
    tools = {tool["id"]: tool for tool in agent.load_tools(registry)[0]}
    assert tools["legacy"]["connection"]["kind"] == "cli"
    assert tools["old-server"]["connection"]["kind"] == "mcp-stdio"
    # Carried over unproved: the old registry never proved anything.
    assert agent.asset_reader_operation(tools["legacy"]) is None
    # Running it twice must not duplicate anything.
    assert agent.migrate_legacy(readers, agent_tools, registry)["imported"] == []


def test_re_saving_a_tool_from_settings_keeps_its_discovered_recipes(tmp_path: Path) -> None:
    """Settings rewrites the whole connection, including fields its dialog never shows.

    Comparing whole connection objects made that look like a different program, so
    the interface -- everything the AI worked out by running it -- was deleted.
    """

    registry = tmp_path / "tools.json"
    remote = {"id": "docs", "roles": ["general"],
              "connection": {"kind": "mcp-http", "url": "https://mcp.example.com/mcp",
                             "bearerEnv": "MCP_TOKEN",
                             "headerEnv": {"X-Api-Key": "DOCS_KEY"}}}
    agent.upsert_tool(dict(remote), registry)
    agent.record_interface("docs", {"source": "mcp/tools-list", "operations": [
        {"role": "asset-reader", "kind": "mcp", "name": "read_asset",
         "patterns": ["*.bin"], "proved": {"sample": "a.bin"}}]}, registry)

    # The dialog has no headerEnv field, so what it saves back has lost it.
    resaved = {"id": "docs", "roles": ["general"],
               "connection": {"kind": "mcp-http", "url": "https://mcp.example.com/mcp",
                              "bearerEnv": "MCP_TOKEN", "headers": {}}}
    agent.upsert_tool(resaved, registry)
    tools, _ = agent.load_tools(registry)
    assert tools[0]["interface"]["operations"][0]["name"] == "read_asset"

    # Pointing it at a different endpoint still invalidates what was discovered.
    moved = dict(resaved)
    moved["connection"] = {"kind": "mcp-http", "url": "https://other.example.com/mcp"}
    agent.upsert_tool(moved, registry)
    tools, _ = agent.load_tools(registry)
    assert tools[0]["interface"]["operations"] == []
