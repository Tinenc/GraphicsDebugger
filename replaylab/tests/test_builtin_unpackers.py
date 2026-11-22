"""Built-in unpackers ship configured: one path in, a complete tool out."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import replaylab_agent.cli as cli
from replaylab_agent import agent_tool_providers, builtin_unpackers
from replaylab_agent.cli import _parser, _tools_command
from replaylab_agent.unitypy_reader_script import SCRIPT


def _host_validator():
    source = Path(__file__).resolve().parents[1] / "mcp_host" / "src" / "tool_providers.py"
    spec = importlib.util.spec_from_file_location("_host_tool_providers_builtin", source)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    records: list[object] = []
    monkeypatch.setattr(cli, "_print_machine_json", records.append)
    return records


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    registry = tmp_path / "tools.json"
    monkeypatch.setattr(cli, "tool_providers_path", lambda: registry)
    monkeypatch.setattr(agent_tool_providers, "default_tools_path", lambda: registry)
    monkeypatch.setattr(builtin_unpackers, "unitypy_script_path",
                        lambda: tmp_path / "tools" / "unitypy_reader.py")
    monkeypatch.setattr(builtin_unpackers, "bundled_unpacker_programs", lambda: {})
    return tmp_path / "agent-tools.json"


def _run(config: Path, arguments: list[str]) -> int:
    return _tools_command(
        _parser().parse_args(["tools", "--tools-config", str(config), *arguments, "--json"]))


def test_every_builtin_recipe_validates_identically_on_both_sides(tmp_path, monkeypatch):
    monkeypatch.setattr(builtin_unpackers, "unitypy_script_path",
                        lambda: tmp_path / "unitypy_reader.py")
    host = _host_validator()
    samples = {
        "uex": ("C:/tools/uex.exe", {"configPath": "C:/tools/profiles.json", "profile": "game"}),
        "assetstudio": ("C:/tools/AssetStudioModCLI.exe", {"unityVersion": "2021.3.16f1"}),
        "unitypy": ("python", {}),
        "umodel": ("C:/tools/umodel_64.exe", {"gameTag": "ue4.27", "aesKey": "ab" * 32}),
    }
    for kind, (program, options) in samples.items():
        definition = builtin_unpackers.builtin_unpacker_definition(kind, program, options)
        agent_view = agent_tool_providers.validate_tool(definition)
        assert agent_view == host.validate_tool(definition), kind
        assert agent_view["builtinKind"] == kind
        assert agent_view["connection"]["command"] == program
        operations = agent_view["interface"]["operations"]
        assert operations, kind
        # Every operation is offered without a per-machine probe.
        assert all(op["proved"] for op in operations), kind
        assert all(op["kind"] == "cli" for op in operations)
        # Every command-line slot is declared, so the Agent can fill it by name.
        for op in operations:
            declared = set(op["inputSchema"]["properties"]) | {"program", "outputDir", "path"}
            for slot in agent_tool_providers.operation_placeholders(op["argv"]):
                assert slot in declared, (kind, op["name"], slot)
    # The Host reads Unity files through the UnityPy script and Unreal ones through
    # uex preview; the other two only offer operations to the Agent.
    uex = agent_tool_providers.validate_tool(
        builtin_unpackers.builtin_unpacker_definition("uex", "uex.exe", samples["uex"][1]))
    assert host.asset_reader_operation(uex)["name"] == "uex_preview"
    # uex takes --config globally but declares --profile on each subcommand, so the
    # profile has to follow the subcommand name; before that it is an unknown option.
    assert host.asset_reader_operation(uex)["argv"][:5] == [
        "--config", "C:/tools/profiles.json", "preview", "--profile", "game"]
    unity = agent_tool_providers.validate_tool(
        builtin_unpackers.builtin_unpacker_definition("unitypy", "python", {}))
    assert host.asset_reader_operation(unity)["name"] == "unitypy_describe"
    assert (tmp_path / "unitypy_reader.py").read_text(encoding="utf-8") == SCRIPT
    compile(SCRIPT, "unitypy_reader.py", "exec")


def test_required_options_and_key_shapes_are_checked(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="profiles.json is required"):
        builtin_unpackers.builtin_unpacker_definition("uex", "uex.exe", {"profile": "g"})
    with pytest.raises(ValueError, match="AES key"):
        builtin_unpackers.builtin_unpacker_definition("umodel", "umodel.exe",
                                                      {"aesKey": "not-a-key"})
    with pytest.raises(ValueError, match="needs the path"):
        builtin_unpackers.builtin_unpacker_definition("umodel", "", {})
    with pytest.raises(ValueError, match="unknown built-in"):
        builtin_unpackers.builtin_unpacker_definition("nope", "x.exe", {})


def test_settings_registers_a_builtin_by_kind_and_lists_every_kind(isolated, emitted):
    assert _run(isolated, ["tool-list"]) == 0
    rows = emitted[-1]["builtinUnpackers"]
    assert [row["kind"] for row in rows] == ["uex", "assetstudio", "unitypy", "umodel"]
    assert all(row["configured"] is False for row in rows)
    assert "*.utoc" in rows[0]["patterns"] and "*.bundle" in rows[1]["patterns"]

    payload = {"builtinKind": "uex", "program": "C:/tools/uex.exe",
               "builtinOptions": {"configPath": "C:/tools/profiles.json",
                                  "profile": "silver", "registerMcp": True}}
    assert _run(isolated, ["tool-set", "--tool-json", json.dumps(payload)]) == 0
    result = emitted[-1]
    assert result["tool"]["id"] == "builtin-uex"
    assert result["tool"]["builtinOptions"]["profile"] == "silver"
    # uex also arrives as an MCP server, started with the same profile file.
    assert result["mcpServer"]["id"] == "uex-mcp"
    assert result["mcpServer"]["args"] == ["--config", "C:/tools/profiles.json", "mcp"]

    assert _run(isolated, ["tool-list"]) == 0
    view = emitted[-1]
    rows = {row["kind"]: row for row in view["builtinUnpackers"]}
    assert rows["uex"]["configured"] is True
    assert rows["uex"]["program"] == "C:/tools/uex.exe"
    assert rows["uex"]["values"]["profile"] == "silver"
    assert rows["assetstudio"]["configured"] is False
    assert [server["id"] for server in view["mcpServers"]] == ["uex-mcp"]

    # Re-registering with a changed option rewrites the recipe, same id.
    payload["builtinOptions"]["profile"] = "gold"
    payload["builtinOptions"]["registerMcp"] = False
    assert _run(isolated, ["tool-set", "--tool-json", json.dumps(payload)]) == 0
    assert _run(isolated, ["tool-list"]) == 0
    tools = [tool for tool in emitted[-1]["tools"] if tool["id"] == "builtin-uex"]
    assert len(tools) == 1
    assert tools[0]["interface"]["operations"][0]["argv"][:4] == [
        "--config", "C:/tools/profiles.json", "preview", "--profile"]
    assert tools[0]["interface"]["operations"][0]["argv"][4] == "gold"


def test_a_bad_builtin_registration_is_refused_with_the_reason(isolated):
    from replaylab_agent.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="Profile name is required"):
        _run(isolated, ["tool-set", "--tool-json", json.dumps(
            {"builtinKind": "uex", "program": "uex.exe",
             "builtinOptions": {"configPath": "p.json"}})])


def test_bundled_unpackers_register_by_token_and_follow_the_bundle(tmp_path, monkeypatch):
    """Tools shipped under the app's tools/ folder register themselves, portably."""

    from replaylab_agent import bundled_paths

    tools_root = tmp_path / "app" / "tools"
    (tools_root / "unpackers" / "assetstudio").mkdir(parents=True)
    (tools_root / "unpackers" / "assetstudio" / "AssetStudioModCLI.exe").write_bytes(b"MZ")
    (tools_root / "unpackers" / "uex").mkdir(parents=True)
    (tools_root / "unpackers" / "uex" / "uex.exe").write_bytes(b"MZ")
    (tools_root / "unpackers" / "uex" / "profiles.json").write_text(
        json.dumps({"profiles": {"silverpalace": {"game": "GAME_SilverPalace", "paksDir": "D:/x"}}}),
        encoding="utf-8")
    monkeypatch.setenv("BREAD_TOOLS_ROOT", str(tools_root))
    monkeypatch.setenv("BREAD_APP_ROOT", str(tmp_path / "app"))
    registry = tmp_path / "tools.json"
    monkeypatch.setattr(agent_tool_providers, "default_tools_path", lambda: registry)
    monkeypatch.setattr(cli, "tool_providers_path", lambda: registry)
    monkeypatch.setattr(builtin_unpackers, "unitypy_script_path",
                        lambda: tmp_path / "tools" / "unitypy_reader.py")

    present = builtin_unpackers.bundled_unpacker_programs()
    assert present["assetstudio"]["program"] == "%BREAD_TOOLS%/unpackers/assetstudio/AssetStudioModCLI.exe"
    assert present["uex"]["options"]["profile"] == "silverpalace"

    receipt = builtin_unpackers.sync_bundled_unpackers(registry)
    assert set(receipt["added"]) >= {"assetstudio", "uex"}, receipt
    # On disk: tokens, so the file is valid on any machine the app is copied to.
    stored = json.loads(registry.read_text(encoding="utf-8"))
    commands = {tool["id"]: tool["connection"]["command"] for tool in stored["tools"]}
    assert commands["builtin-assetstudio"] == "%BREAD_TOOLS%/unpackers/assetstudio/AssetStudioModCLI.exe"
    assert all(tool["source"] == "bundled" for tool in stored["tools"] if tool["id"] in commands)
    uex_stored = next(tool for tool in stored["tools"] if tool["id"] == "builtin-uex")
    assert uex_stored["builtinOptions"]["configPath"] == "%BREAD_TOOLS%/unpackers/uex/profiles.json"
    assert uex_stored["interface"]["operations"][0]["argv"][1] == "%BREAD_TOOLS%/unpackers/uex/profiles.json"
    # In memory, on this machine: real paths, for the agent and for the Host alike.
    loaded, unused = agent_tool_providers.load_tools(registry)
    by_id = {tool["id"]: tool for tool in loaded}
    assert Path(by_id["builtin-assetstudio"]["connection"]["command"]) == (
        tools_root / "unpackers" / "assetstudio" / "AssetStudioModCLI.exe")
    host = _host_validator()
    host_loaded, unused = host.load_tools(registry)
    host_uex = next(tool for tool in host_loaded if tool["id"] == "builtin-uex")
    assert Path(host_uex["interface"]["operations"][0]["argv"][1]) == tools_root / "unpackers" / "uex" / "profiles.json"
    # A second sync changes nothing; a user-registered program is never displaced.
    again = builtin_unpackers.sync_bundled_unpackers(registry)
    assert again["added"] == [] and again["updated"] == []
    agent_tool_providers.upsert_tool(
        builtin_unpackers.builtin_unpacker_definition("assetstudio", "C:/mine/AssetStudioModCLI.exe", {}), registry)
    builtin_unpackers.sync_bundled_unpackers(registry)
    loaded, unused = agent_tool_providers.load_tools(registry)
    mine = next(tool for tool in loaded if tool["id"] == "builtin-assetstudio")
    assert mine["connection"]["command"] == "C:/mine/AssetStudioModCLI.exe" and mine["source"] == "registered"
    # A bundled tool that disappears is dropped from the registry.
    (tools_root / "unpackers" / "uex" / "uex.exe").unlink()
    gone = builtin_unpackers.sync_bundled_unpackers(registry)
    assert gone["removed"] == ["uex"]
    assert "builtin-uex" not in {tool["id"] for tool in agent_tool_providers.load_tools(registry)[0]}


def test_uex_reads_the_unpacked_tree_without_a_profile_or_keys(isolated):
    """No profiles.json, no AES key: the Host fills the profile it generated itself."""

    host = _host_validator()
    definition = builtin_unpackers.builtin_unpacker_definition("uex", "C:/tools/uex.exe", {})
    assert definition["builtinOptions"]["mode"] == "unpacked"
    tool = agent_tool_providers.validate_tool(definition)  # host slots are not parameters
    reader = host.asset_reader_operation(tool)
    assert reader["argv"] == ["--config", "{uexConfig}", "preview",
                              "--profile", "{uexProfile}", "{uexPath}"]
    # the reader only claims packages here: a .pak in an unpacked tree is not what it mounts
    assert reader["patterns"] == ["*.uasset", "*.umap"]
    operations = {item["name"]: item for item in definition["interface"]["operations"]}
    assert operations["uex_search"]["argv"][:5] == ["--config", "{uexConfig}", "search",
                                                   "--profile", "{uexProfile}"]
    # ... and registering it does not bring up an MCP server with a config that has no profiles
    assert _run(isolated, ["tool-set", "--tool-json", json.dumps(
        {"builtinKind": "uex", "program": "C:/tools/uex.exe", "builtinOptions": {}})]) == 0
    assert _run(isolated, ["tool-list"]) == 0


def test_a_half_written_uex_pak_profile_is_refused(isolated):
    from replaylab_agent.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="profiles.json is required"):
        _run(isolated, ["tool-set", "--tool-json", json.dumps(
            {"builtinKind": "uex", "program": "uex.exe",
             "builtinOptions": {"profile": "silver"}})])


@pytest.mark.parametrize("options,config,profile", [
    ({}, "C:/host/profiles.json", "unpacked-proved"),
    ({"configPath": "C:/tools/profiles.json", "profile": "proved-game"},
     "C:/tools/profiles.json", "proved-game"),
])
def test_material_shader_maps_is_an_explicit_bounded_operation(
        options, config, profile, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "mcp_host"))
    from src import tool_runner

    definition = builtin_unpackers.builtin_unpacker_definition("uex", "uex.exe", options)
    host = _host_validator()
    tool = agent_tool_providers.validate_tool(definition)
    assert tool == host.validate_tool(definition)
    operation = next(op for op in tool["interface"]["operations"]
                     if op["name"] == "uex_material_shader_maps")
    assert operation["role"] == "general"
    assert host.asset_reader_operation(tool)["name"] == "uex_preview"
    assert set(operation["inputSchema"]["properties"]) == {"asset"}
    assert operation["inputSchema"]["required"] == ["asset"]
    assert operation["result"]["successWhen"]["exitCode"] == 0
    assert "materialOverrideChain" in operation["description"]
    assert "only inside the configured complete asset root" in operation["description"]
    # A path remains one argument, including spaces, shell punctuation and slot-like text.
    asset = "Game/Content/Materials/MI Hat & {outputDir}.uasset"
    argv = tool_runner.build_argv(
        operation, {"asset": asset}, program="C:/tools/uex.exe",
        output_dir=str(tmp_path),
        host_slots={"uexConfig": config, "uexProfile": profile})
    assert argv == ["C:/tools/uex.exe", "--config", config, "material-shader-maps",
                    "--profile", profile, "--max-bytes", "8388608", asset]
    assert int(argv[-2]) == tool_runner.MAX_OUTPUT_ARTIFACT_BYTES
    with pytest.raises(tool_runner.ToolRunError, match="missing required.*asset"):
        tool_runner.resolve_arguments(operation, {})
    for extra in ("maxBytes", "uexConfig", "uexProfile", "directory"):
        with pytest.raises(tool_runner.ToolRunError, match="declares no parameter"):
            tool_runner.resolve_arguments(operation, {"asset": asset, extra: "override"})


@pytest.mark.parametrize("exit_code,status", [(0, "parsed"), (1, "budget_exceeded")])
def test_material_shader_maps_keeps_complete_json_in_receipt(
        exit_code, status, tmp_path, monkeypatch):
    """The discovered recipe uses Host stdout retention, including failed diagnostics."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "mcp_host"))
    from src import tool_runner

    program = tmp_path / "uex.exe"
    program.write_bytes(b"fake executable; no process is launched")
    definition = builtin_unpackers.builtin_unpacker_definition(
        "uex", str(program), {"configPath": "C:/profiles.json", "profile": "proved-game"})
    operation = next(op for op in definition["interface"]["operations"]
                     if op["name"] == "uex_material_shader_maps")
    envelope = {"schema": "bread-material-shader-evidence/v1", "status": status,
                "coverage": {"complete": exit_code == 0},
                "diagnostics": [{"message": "evidence " * 9000}]}
    payload = json.dumps(envelope).encode("utf-8")

    def completed(argv, **kwargs):
        assert kwargs["shell"] is False
        assert argv[-1] == "Game/Content/MI_Hat.uasset"
        return tool_runner.subprocess.CompletedProcess(argv, exit_code, payload, b"")

    monkeypatch.setattr(tool_runner.subprocess, "run", completed)
    receipt = tool_runner.run_operation(
        operation, {"asset": "Game/Content/MI_Hat.uasset"},
        program=str(program), output_root=tmp_path / "runs")
    assert receipt["succeeded"] is (exit_code == 0)
    assert receipt["stdoutTruncated"] is True
    assert receipt["stdoutFileTruncated"] is False
    assert receipt["stdoutByteSize"] == receipt["stdoutFileByteSize"] == len(payload)
    assert receipt["stdoutSha256"] == receipt["stdoutFileSha256"]
    assert json.loads(Path(receipt["stdoutPath"]).read_text(encoding="utf-8")) == envelope


def test_recipe_refresh_adds_material_shader_maps_without_changing_user_choices(tmp_path, monkeypatch):
    registry = tmp_path / "tools.json"
    monkeypatch.setattr(builtin_unpackers, "bundled_unpacker_programs", lambda: {})
    tool = builtin_unpackers.builtin_unpacker_definition(
        "uex", "C:/mine/uex.exe", {}, enabled=False, offer_to_agent=False)
    tool["title"] = "My UEX"
    tool["interface"]["operations"] = [
        op for op in tool["interface"]["operations"]
        if op["name"] != "uex_material_shader_maps"]
    agent_tool_providers.save_tools([tool], registry)
    assert builtin_unpackers.sync_bundled_unpackers(registry)["updated"] == ["uex"]
    refreshed = agent_tool_providers.load_tools(registry)[0][0]
    assert refreshed["connection"] == tool["connection"]
    assert refreshed["builtinOptions"] == tool["builtinOptions"]
    assert refreshed["title"] == "My UEX"
    assert refreshed["enabled"] is False and refreshed["roles"] == ["asset-reader"]
    assert "uex_material_shader_maps" in {
        op["name"] for op in refreshed["interface"]["operations"]}
    assert builtin_unpackers.sync_bundled_unpackers(registry)["updated"] == []


@pytest.mark.parametrize("protocol", ["api-main", "claude-acp", "codex", "other-acp"])
def test_loaded_shader_skill_keeps_asset_matching_optional(protocol):
    from replaylab_agent.agent_prompts import deep_main_system_prompt, native_agent_system_prompt
    from replaylab_agent.agent_registry import CatalogView

    catalog = CatalogView.load(skill_roots=[Path(__file__).resolve().parents[2] / ".agents" / "skills"])
    skill = catalog.skill("renderdoc-shader-workbench")
    if protocol == "api-main":
        prompt = deep_main_system_prompt(
            catalog=catalog, request="Organize the matched shader", context=None,
            response_language="zh_CN", subagents=(), loaded_skill=skill,
        )
    else:
        prompt = native_agent_system_prompt(
            catalog=catalog, request="Organize the matched shader", context=None,
            response_language="zh_CN", tool_names=("run_asset_tool_operation",),
            protocol=protocol, effects=(), loaded_skill=skill)
    text = " ".join(prompt.split())
    assert skill.instructions.strip() in prompt
    assert "Asset matching is optional" in text
    assert "uex_material_shader_maps" not in text
    assert any(op["name"] == "uex_material_shader_maps"
               for op in builtin_unpackers._uex_operations())


def test_shipped_recipe_changes_refresh_existing_registrations(tmp_path, monkeypatch):
    registry = tmp_path / "tools.json"
    monkeypatch.setattr(builtin_unpackers, "bundled_unpacker_programs", lambda: {})
    tool = builtin_unpackers.builtin_unpacker_definition(
        "uex", "C:/custom/uex.exe", {"configPath": "C:/custom/profiles.json", "profile": "game"},
        enabled=False, offer_to_agent=False)
    tool["title"] = "My extractor"
    tool["source"] = "registered"
    # The recipe persisted before the subcommand/profile ordering was corrected.
    for operation in tool["interface"]["operations"]:
        argv = operation["argv"]
        operation["argv"] = argv[:2] + argv[3:5] + [argv[2]] + argv[5:]
    agent_tool_providers.save_tools([tool], registry)

    result = builtin_unpackers.sync_bundled_unpackers(registry)
    assert result["updated"] == ["uex"]
    refreshed = agent_tool_providers.load_tools(registry)[0][0]
    assert refreshed["connection"] == tool["connection"]
    assert refreshed["builtinOptions"] == tool["builtinOptions"]
    assert refreshed["title"] == "My extractor"
    assert refreshed["enabled"] is False and refreshed["roles"] == ["asset-reader"]
    assert refreshed["interface"]["operations"][0]["argv"] == [
        "--config", "C:/custom/profiles.json", "preview", "--profile", "game", "{path}"]
    before = registry.read_bytes()
    assert builtin_unpackers.sync_bundled_unpackers(registry)["updated"] == []
    assert registry.read_bytes() == before

    # A user-proved/custom interface is not a shipped recipe to replace.
    refreshed["interface"]["source"] = "cli/probe"
    refreshed["interface"]["operations"][0]["argv"] = ["custom-preview", "{path}"]
    agent_tool_providers.save_tools([refreshed], registry)
    before = registry.read_bytes()
    assert builtin_unpackers.sync_bundled_unpackers(registry)["updated"] == []
    assert registry.read_bytes() == before


def test_failed_probe_does_not_permanently_erase_bundled_batch_recipes(tmp_path, monkeypatch):
    registry = tmp_path / "tools.json"
    program = str(tmp_path / "umodel_64.exe")
    entry = {"program": program, "options": {}}
    monkeypatch.setattr(builtin_unpackers, "bundled_unpacker_programs", lambda: {"umodel": entry})
    tool = builtin_unpackers.builtin_unpacker_definition("umodel", program, {}, enabled=False)
    tool["source"] = "bundled"
    tool["interface"] = {"source": "cli/probe", "operations": []}
    agent_tool_providers.save_tools([tool], registry)
    assert builtin_unpackers.sync_bundled_unpackers(registry)["updated"] == ["umodel"]
    refreshed = agent_tool_providers.load_tools(registry)[0][0]
    assert refreshed["enabled"] is False
    assert {item["name"] for item in refreshed["interface"]["operations"]} == {
        "umodel_list", "umodel_export", "umodel_export_object"}
    assert all(item["role"] == "general" for item in refreshed["interface"]["operations"])
