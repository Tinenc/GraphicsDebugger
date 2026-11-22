"""Settings edits the same three files the AI configures itself.

Skills, MCP servers and asset readers are one catalog with two editors: the
person in Settings > AI, and the Agent when a task is told to set the workspace
up. Both go through these commands, so what one writes the other reads back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import replaylab_agent.cli as cli
from replaylab_agent import agent_asset_readers as readers
from replaylab_agent.agent_tools_config import AgentToolsConfig, user_skill_overlay_root
from replaylab_agent.cli import _parser, _tools_command
from replaylab_agent.errors import ConfigurationError


@pytest.fixture()
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    records: list[object] = []
    monkeypatch.setattr(cli, "_print_machine_json", records.append)
    return records


def _run(config: Path, arguments: list[str]) -> int:
    return _tools_command(
        _parser().parse_args(["tools", "--tools-config", str(config), *arguments, "--json"])
    )


def _skill_root(root: Path, identifier: str, body: str = "") -> Path:
    folder = root / identifier
    folder.mkdir(parents=True, exist_ok=True)
    document = folder / "SKILL.md"
    document.write_text(
        body
        or (
            "---\n"
            f"name: {identifier}\n"
            f"title: {identifier.title()}\n"
            "description: a Skill under test\n"
            "version: 1\n"
            "---\n\n"
            "# Body\n"
        ),
        encoding="utf-8",
    )
    return document


# ------------------------------------------------------------------ Skills


def test_a_user_skill_is_shown_and_edited_where_it_lives(tmp_path, emitted) -> None:
    config_path = tmp_path / "agent-tools.json"
    root = tmp_path / "my-skills"
    document = _skill_root(root, "my-skill")
    AgentToolsConfig(skill_roots=(root,)).save(config_path)

    assert _run(config_path, ["skill-show", "my-skill"]) == 0
    shown = emitted[-1]
    assert shown["source"] == "user"
    assert shown["path"] == str(document)
    assert shown["copyOnWrite"] is False
    assert "# Body" in shown["content"]

    edited = tmp_path / "edited.md"
    edited.write_text(shown["content"].replace("# Body", "# Edited"), encoding="utf-8")
    assert _run(config_path, ["skill-write", "my-skill", "--content-file", str(edited)]) == 0
    assert emitted[-1]["copiedToOverlay"] is False
    assert "# Edited" in document.read_text(encoding="utf-8")


def test_editing_a_bundled_skill_copies_it_into_the_user_overlay(tmp_path, emitted,
                                                                 monkeypatch) -> None:
    """The next install replaces the bundled file, so an edit must not live there."""

    config_path = tmp_path / "agent-tools.json"
    bundled = tmp_path / "bundled"
    _skill_root(bundled, "shipped")
    monkeypatch.setattr(cli, "default_skill_root", lambda: bundled)

    assert _run(config_path, ["skill-show", "shipped"]) == 0
    assert emitted[-1]["source"] == "bundled"
    assert emitted[-1]["copyOnWrite"] is True

    body = tmp_path / "body.md"
    body.write_text(emitted[-1]["content"] + "\nlocal change\n", encoding="utf-8")
    assert _run(config_path, ["skill-write", "shipped", "--content-file", str(body)]) == 0
    written = emitted[-1]
    overlay = user_skill_overlay_root(config_path)
    assert written["copiedToOverlay"] is True
    assert written["path"] == str(overlay / "shipped" / "SKILL.md")
    assert written["registeredOverlayRoot"] is True
    # The bundled copy is untouched and the overlay is now a Skill root.
    assert "local change" not in (bundled / "shipped" / "SKILL.md").read_text(encoding="utf-8")
    assert overlay in AgentToolsConfig.load(config_path).skill_roots


def test_a_new_skill_starts_from_a_template_in_the_overlay(tmp_path, emitted) -> None:
    config_path = tmp_path / "agent-tools.json"
    assert _run(config_path, ["skill-new", "capture-triage", "--title", "Capture Triage"]) == 0
    created = Path(emitted[-1]["path"])
    assert created.is_file()
    text = created.read_text(encoding="utf-8")
    assert 'name: "capture-triage"' in text
    assert "Capture Triage" in text
    assert emitted[-1]["registeredOverlayRoot"] is True
    with pytest.raises(ConfigurationError):
        _run(config_path, ["skill-new", "capture-triage"])
    with pytest.raises(ConfigurationError):
        _run(config_path, ["skill-new", "Not An Id"])


def test_an_empty_or_unknown_skill_is_refused_by_name(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "agent-tools.json"
    monkeypatch.setattr(cli, "default_skill_root", lambda: tmp_path / "bundled")
    (tmp_path / "bundled").mkdir()
    with pytest.raises(ConfigurationError) as unknown:
        _run(config_path, ["skill-show", "absent"])
    assert "absent" in str(unknown.value)
    root = tmp_path / "my-skills"
    _skill_root(root, "present")
    AgentToolsConfig(skill_roots=(root,)).save(config_path)
    blank = tmp_path / "blank.md"
    blank.write_text("   \n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        _run(config_path, ["skill-write", "present", "--content-file", str(blank)])


def test_a_skill_row_names_the_registered_root_not_the_skill_folder(tmp_path, emitted) -> None:
    """Settings removes what the configuration holds; deriving it from the path was a bug."""

    config_path = tmp_path / "agent-tools.json"
    root = tmp_path / "my-skills"
    _skill_root(root, "one")
    AgentToolsConfig(skill_roots=(root,)).save(config_path)
    assert _run(config_path, ["skill-show", "one"]) == 0
    row = _run  # keep the helper referenced for readers of this test
    del row
    from replaylab_agent.agent_registry import CatalogView
    from replaylab_agent.agent_tools_config import describe_skills

    config = AgentToolsConfig.load(config_path)
    rows = describe_skills(CatalogView.load(skill_roots=(root,)), config, tmp_path / "bundled")
    assert rows[0]["root"] == str(root)
    assert rows[0]["root"] != str(root / "one")
    assert rows[0]["removable"] is True


def test_deleting_a_skill_removes_that_skill_and_nothing_else(tmp_path, emitted) -> None:
    config_path = tmp_path / "agent-tools.json"
    assert _run(config_path, ["skill-new", "keep-me"]) == 0
    assert _run(config_path, ["skill-new", "delete-me"]) == 0
    overlay = user_skill_overlay_root(config_path)

    assert _run(config_path, ["skill-remove", "delete-me"]) == 0
    assert emitted[-1]["deleted"] == str(overlay / "delete-me")
    assert not (overlay / "delete-me").exists()
    # The neighbour, and the registered folder, survive.
    assert (overlay / "keep-me" / "SKILL.md").is_file()
    assert overlay in AgentToolsConfig.load(config_path).skill_roots
    assert emitted[-1]["unregisteredRoot"] == ""

    assert _run(config_path, ["skill-remove", "keep-me"]) == 0
    assert emitted[-1]["unregisteredRoot"] == str(overlay)
    assert overlay not in AgentToolsConfig.load(config_path).skill_roots


def test_a_skill_installed_with_renderdoc_cannot_be_deleted(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "agent-tools.json"
    bundled = tmp_path / "bundled"
    _skill_root(bundled, "shipped")
    monkeypatch.setattr(cli, "default_skill_root", lambda: bundled)
    with pytest.raises(ConfigurationError) as refused:
        _run(config_path, ["skill-remove", "shipped"])
    assert "disable" in str(refused.value)
    assert (bundled / "shipped" / "SKILL.md").is_file()


# ------------------------------------------------------------- MCP servers


def test_testing_a_server_reports_initialize_and_the_tool_count(tmp_path, emitted,
                                                                monkeypatch) -> None:
    config_path = tmp_path / "agent-tools.json"
    config = AgentToolsConfig()
    config.upsert_mcp_server({"id": "local", "title": "Local", "command": "server.exe",
                              "args": ["--stdio"]})
    config.save(config_path)

    async def route(command, arguments, environment, *, cwd, timeout):
        assert command == "server.exe"
        assert list(arguments) == ["--stdio"]
        return {"ok": True, "tools": 7, "seconds": 0.42, "error": ""}

    monkeypatch.setattr("replaylab_agent.acp_session._diagnose_local_mcp_route", route)
    assert _run(config_path, ["mcp-test", "local"]) == 0
    assert emitted[-1]["ok"] is True
    assert emitted[-1]["tools"] == 7
    assert emitted[-1]["seconds"] == 0.42


def test_a_server_that_does_not_start_says_so_instead_of_failing_the_command(
    tmp_path, emitted, monkeypatch
) -> None:
    config_path = tmp_path / "agent-tools.json"
    config = AgentToolsConfig()
    config.upsert_mcp_server({"id": "broken", "command": "missing.exe"})
    config.save(config_path)

    async def route(command, arguments, environment, *, cwd, timeout):
        return {"ok": False, "tools": 0, "seconds": 1.0,
                "error": "FileNotFoundError: missing.exe"}

    monkeypatch.setattr("replaylab_agent.acp_session._diagnose_local_mcp_route", route)
    assert _run(config_path, ["mcp-test", "broken"]) == 0
    assert emitted[-1]["ok"] is False
    assert "missing.exe" in emitted[-1]["error"]
    with pytest.raises(ConfigurationError):
        _run(config_path, ["mcp-test", "absent"])


# ------------------------------------------------------------ asset readers


def test_readers_are_registered_listed_and_removed_through_the_same_file(
    tmp_path, emitted, monkeypatch
) -> None:
    registry = tmp_path / "asset-readers.json"
    monkeypatch.setattr(cli, "default_readers_path", lambda: registry)
    monkeypatch.setattr(readers, "default_readers_path", lambda: registry)
    config_path = tmp_path / "agent-tools.json"

    definition = {"id": "uex", "title": "uex", "command": "C:/tools/uex.exe",
                  "args": ["preview", "{path}"], "patterns": ["*.uasset"], "engines": ["unreal"]}
    assert _run(config_path, ["reader-set", "--reader-json", json.dumps(definition)]) == 0
    assert emitted[-1]["reader"]["id"] == "uex"

    assert _run(config_path, ["reader-list"]) == 0
    view = emitted[-1]
    assert [row["id"] for row in view["readers"]] == ["uex"]
    assert view["contract"]["presets"], "Settings offers the community readers as starting points"

    assert _run(config_path, ["reader-disable", "uex"]) == 0
    assert emitted[-1]["enabled"] is False
    assert _run(config_path, ["reader-remove", "uex"]) == 0
    assert emitted[-1]["removed"] is True
    assert _run(config_path, ["reader-list"]) == 0
    assert emitted[-1]["readers"] == []


def test_adding_a_recognised_tool_needs_only_its_path(tmp_path, emitted, monkeypatch) -> None:
    """"Here is my extractor" is the whole interaction for a tool we know."""

    registry = tmp_path / "asset-readers.json"
    monkeypatch.setattr(cli, "default_readers_path", lambda: registry)
    monkeypatch.setattr(readers, "default_readers_path", lambda: registry)
    config_path = tmp_path / "agent-tools.json"

    assert _run(config_path, ["reader-add", "C:/tools/uex/uex.exe"]) == 0
    reader = emitted[-1]["reader"]
    assert reader["id"] == "uex"
    assert reader["engines"] == ["unreal"]
    assert "{path}" in reader["args"]
    assert "*.uasset" in reader["patterns"]
    assert reader["note"], "a recognised tool arrives with its purpose already written down"


def test_an_unknown_tool_is_stored_with_the_plain_shape_for_the_agent_to_prove(
    tmp_path, emitted, monkeypatch
) -> None:
    registry = tmp_path / "asset-readers.json"
    monkeypatch.setattr(cli, "default_readers_path", lambda: registry)
    monkeypatch.setattr(readers, "default_readers_path", lambda: registry)
    config_path = tmp_path / "agent-tools.json"

    assert _run(config_path, ["reader-add", "D:/mine/extract.exe", "--name", "My Extractor",
                              "--description", "reads our own container"]) == 0
    reader = emitted[-1]["reader"]
    assert reader["id"] == "my-extractor"
    assert reader["args"] == ["{path}"]
    assert reader["patterns"] == ["*"]
    assert reader["note"] == "reads our own container"


def test_the_panel_is_told_what_is_built_in_and_what_is_recommended(tmp_path, emitted,
                                                                    monkeypatch) -> None:
    registry = tmp_path / "asset-readers.json"
    monkeypatch.setattr(cli, "default_readers_path", lambda: registry)
    monkeypatch.setattr(readers, "default_readers_path", lambda: registry)
    config_path = tmp_path / "agent-tools.json"

    assert _run(config_path, ["reader-list"]) == 0
    builtins = emitted[-1]["builtins"]
    assert [item["id"] for item in builtins] == [
        "unreal-package", "unity-serialized", "standalone-containers"]
    assert all(item["description"] for item in builtins)

    assert _run(config_path, ["list"]) == 0
    assert [item["id"] for item in emitted[-1]["mcpPresets"]] == ["uex-mcp"]


def test_discovery_lists_installed_tools_and_marks_the_ones_already_added(
    tmp_path, emitted, monkeypatch
) -> None:
    registry = tmp_path / "asset-readers.json"
    monkeypatch.setattr(cli, "default_readers_path", lambda: registry)
    monkeypatch.setattr(readers, "default_readers_path", lambda: registry)
    monkeypatch.setattr(readers, "discovery_locations", lambda: (tmp_path,))
    config_path = tmp_path / "agent-tools.json"
    (tmp_path / "uex.exe").write_bytes(b"MZ")

    assert _run(config_path, ["reader-discover"]) == 0
    candidates = emitted[-1]["candidates"]
    assert [item["id"] for item in candidates] == ["uex"]
    assert candidates[0]["alreadyRegistered"] is False

    assert _run(config_path, ["reader-add", str(tmp_path / "uex.exe")]) == 0
    assert _run(config_path, ["reader-discover"]) == 0
    assert emitted[-1]["candidates"][0]["alreadyRegistered"] is True


def test_a_reader_without_patterns_or_magic_is_refused_with_the_reason(
    tmp_path, emitted, monkeypatch
) -> None:
    registry = tmp_path / "asset-readers.json"
    monkeypatch.setattr(cli, "default_readers_path", lambda: registry)
    monkeypatch.setattr(readers, "default_readers_path", lambda: registry)
    config_path = tmp_path / "agent-tools.json"
    with pytest.raises(ConfigurationError) as refused:
        _run(config_path, ["reader-set", "--reader-json",
                           json.dumps({"id": "vague", "command": "reader.exe"})])
    assert "patterns" in str(refused.value)
    with pytest.raises(ConfigurationError):
        _run(config_path, ["reader-set", "--reader-json", "not json"])
    with pytest.raises(ConfigurationError):
        _run(config_path, ["reader-enable", "absent"])


# ------------------------------------------------------------------- tools

def test_a_tool_is_registered_from_its_connection_alone(tmp_path, emitted, monkeypatch) -> None:
    """Settings asks for a path or a URL; nothing else is a person's job."""

    from replaylab_agent import agent_tool_providers

    # This registration test is independent of unpackers installed beside the
    # developer's checkout; their automatic sync has its own coverage.
    monkeypatch.setattr(cli, "_sync_bundled_unpackers_quietly", lambda: None)
    registry = tmp_path / "tools.json"
    monkeypatch.setattr(cli, "tool_providers_path", lambda: registry)
    monkeypatch.setattr(agent_tool_providers, "default_tools_path", lambda: registry)
    config_path = tmp_path / "agent-tools.json"

    assert _run(config_path, ["tool-add", "--kind", "cli",
                              "--command", "C:/tools/AssetStudioCLI.exe"]) == 0
    tool = emitted[-1]["tool"]
    assert tool["id"] == "assetstudiocli"
    assert tool["roles"] == ["asset-reader"]
    assert tool["interface"]["operations"] == [], "the interface is discovered, not typed in"

    assert _run(config_path, ["tool-add", "--kind", "mcp-stdio", "--name", "uex MCP server",
                              "--command", "C:/tools/uex.exe", "--arg", "mcp"]) == 0
    server = emitted[-1]["tool"]
    assert server["id"] == "uex-mcp-server"
    assert server["roles"] == ["general"]
    assert server["connection"]["args"] == ["mcp"]

    assert _run(config_path, ["tool-add", "--kind", "mcp-http", "--name", "Remote",
                              "--url", "https://mcp.example.com/mcp",
                              "--bearer-env", "MCP_TOKEN",
                              "--header", "X-Tenant=acme"]) == 0
    remote = emitted[-1]["tool"]
    assert remote["connection"]["url"] == "https://mcp.example.com/mcp"
    assert remote["connection"]["bearerEnv"] == "MCP_TOKEN"
    assert remote["connection"]["headers"] == {"X-Tenant": "acme"}

    assert _run(config_path, ["tool-list"]) == 0
    view = emitted[-1]
    assert sorted(item["id"] for item in view["tools"]) == [
        "assetstudiocli", "remote", "uex-mcp-server"]
    assert [item["id"] for item in view["builtinReaders"]][0] == "unreal-package"
    assert set(view["connectionKinds"]) == {"mcp-stdio", "mcp-http", "cli"}


def test_a_tool_is_disabled_and_removed_by_id(tmp_path, emitted, monkeypatch) -> None:
    from replaylab_agent import agent_tool_providers

    registry = tmp_path / "tools.json"
    monkeypatch.setattr(cli, "tool_providers_path", lambda: registry)
    monkeypatch.setattr(agent_tool_providers, "default_tools_path", lambda: registry)
    config_path = tmp_path / "agent-tools.json"

    assert _run(config_path, ["tool-add", "--kind", "cli", "--command", "x.exe"]) == 0
    assert _run(config_path, ["tool-disable", "x"]) == 0
    assert emitted[-1]["enabled"] is False
    assert _run(config_path, ["tool-remove", "x"]) == 0
    assert emitted[-1]["removed"] is True
    with pytest.raises(ConfigurationError):
        _run(config_path, ["tool-enable", "absent"])


def test_a_bad_connection_is_refused_with_the_reason(tmp_path, emitted, monkeypatch) -> None:
    from replaylab_agent import agent_tool_providers

    registry = tmp_path / "tools.json"
    monkeypatch.setattr(cli, "tool_providers_path", lambda: registry)
    monkeypatch.setattr(agent_tool_providers, "default_tools_path", lambda: registry)
    config_path = tmp_path / "agent-tools.json"

    with pytest.raises(ConfigurationError) as refused:
        _run(config_path, ["tool-add", "--kind", "mcp-http", "--name", "Remote",
                           "--url", "http://example.com/mcp"])
    assert "localhost" in str(refused.value)
    with pytest.raises(ConfigurationError):
        _run(config_path, ["tool-add", "--kind", "cli", "--command", ""])
    with pytest.raises(ConfigurationError) as pairs:
        _run(config_path, ["tool-add", "--kind", "cli", "--command", "x.exe",
                           "--env", "NOEQUALS"])
    assert "NAME=value" in str(pairs.value)


def test_testing_a_tool_reports_what_answered(tmp_path, emitted, monkeypatch) -> None:
    """A configured tool is not a working tool; this is the button that says which."""

    from replaylab_agent import agent_tool_providers

    registry = tmp_path / "tools.json"
    monkeypatch.setattr(cli, "tool_providers_path", lambda: registry)
    monkeypatch.setattr(agent_tool_providers, "default_tools_path", lambda: registry)
    monkeypatch.setattr(cli, "load_tool_providers",
                        lambda *a, **k: agent_tool_providers.load_tools(registry))
    config_path = tmp_path / "agent-tools.json"
    agent_tool_providers.upsert_tool(
        {"id": "server", "roles": ["general"],
         "connection": {"kind": "mcp-stdio", "command": "server.exe", "args": ["mcp"]}}, registry)

    async def route(command, arguments, environment, *, cwd, timeout):
        assert command == "server.exe"
        return {"ok": True, "tools": 9, "seconds": 0.31, "error": ""}

    monkeypatch.setattr("replaylab_agent.acp_session._diagnose_local_mcp_route", route)
    assert _run(config_path, ["tool-test", "server"]) == 0
    assert emitted[-1]["ok"] is True
    assert emitted[-1]["operations"] == 9
    assert emitted[-1]["connection"] == "mcp-stdio"


def test_testing_a_command_line_tool_that_is_not_there_says_so(tmp_path, emitted,
                                                               monkeypatch) -> None:
    from replaylab_agent import agent_tool_providers

    registry = tmp_path / "tools.json"
    monkeypatch.setattr(cli, "tool_providers_path", lambda: registry)
    monkeypatch.setattr(agent_tool_providers, "default_tools_path", lambda: registry)
    monkeypatch.setattr(cli, "load_tool_providers",
                        lambda *a, **k: agent_tool_providers.load_tools(registry))
    config_path = tmp_path / "agent-tools.json"
    agent_tool_providers.upsert_tool(
        {"id": "missing", "roles": ["asset-reader"],
         "connection": {"kind": "cli", "command": str(tmp_path / "absent.exe")}}, registry)

    assert _run(config_path, ["tool-test", "missing"]) == 0
    assert emitted[-1]["ok"] is False
    assert "no such program" in emitted[-1]["error"]
    with pytest.raises(ConfigurationError):
        _run(config_path, ["tool-test", "nobody"])
