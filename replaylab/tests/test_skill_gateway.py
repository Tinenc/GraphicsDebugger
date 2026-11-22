"""The capability directory and Skill loader every AI connection is given."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from replaylab_agent.agent_registry import CatalogView
from replaylab_agent.agent_tools_config import (
    AgentToolsConfig,
    mcp_server_runtime_status,
    validate_mcp_server,
)
from replaylab_agent.errors import ConfigurationError
from replaylab_agent.policy import AGENT_LOCAL_TOOLS, tool_effects
from replaylab_agent.skill_gateway import (
    DIRECTORY_TOOL,
    INVOKE_TOOL,
    LOAD_SKILL_TOOL,
    MAX_SKILL_INLINE_BYTES,
    MAX_TOOLS_PER_FAMILY,
    SkillGateway,
    encode,
    invoke_tool_schema,
    load_validated_skill_catalog,
    split_sections,
    tool_families,
)


SKILL = """---
name: {name}
description: {description}
metadata:
  replaylab-category: {category}
  replaylab-version: "3"
  replaylab-when: {when}
---
{body}
"""

CORE = """---
name: replaylab-core
description: Standing Host evidence rules.
metadata:
  replaylab-category: core
  replaylab-always: "true"
---
Copy identifiers exactly.
"""


def _catalog(tmp_path: Path) -> CatalogView:
    root = tmp_path / "skills"
    (root / "replaylab-core").mkdir(parents=True)
    (root / "replaylab-core" / "SKILL.md").write_text(CORE, encoding="utf-8")
    for name, category, when, body in (
        ("renderdoc-shader-workbench", "shader", "The user asks about one shader.", "Step 1. Prepare."),
        ("renderdoc-explain-event", "event-pipeline", "The user asks about one event.", "Step 1. Inspect."),
    ):
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            SKILL.format(name=name, description=f"{category} work", category=category,
                         when=when, body=body),
            encoding="utf-8",
        )
    return CatalogView.load(skill_roots=[root])


def test_directory_lists_skills_tool_families_and_never_decides_intent(tmp_path: Path) -> None:
    gateway = SkillGateway(
        _catalog(tmp_path),
        tool_names=["inspect_event", "get_texture_info", "prepare_shader_workspace",
                    "compile_shader_workspace", "retrieve_shader_asset_candidates"],
        effects=["workspace"],
    )
    directory = gateway.directory()
    assert directory["schema"] == "replaylab-capability-directory/v1"
    assert {item["id"] for item in directory["skills"]} == {
        "renderdoc-shader-workbench", "renderdoc-explain-event", "replaylab-core"}
    assert "alwaysLoadedSkills" not in directory
    assert directory["loadedSkills"] == []
    assert directory["loadWith"] == LOAD_SKILL_TOOL
    assert all("whenToUse" in item for item in directory["skills"]
               if item["id"] != "replaylab-core")
    families = {item["family"] for item in directory["toolFamilies"]}
    assert {"shader-workspace", "capture-analysis", "resources", "unpacked-assets"} <= families
    assert directory["grantedToolCount"] == 5
    # No ranking, no scoring, no guess: the payload is the same for every request.
    assert directory == gateway.directory()
    assert "query=<text>" in directory["expandWith"]


def test_shared_loaded_flag_does_not_advise_another_actor_to_skip_instructions(tmp_path: Path) -> None:
    gateway = SkillGateway(_catalog(tmp_path))
    first = gateway.load("renderdoc-shader-workbench")
    directory = gateway.directory()

    assert directory["loadedSkills"] == ["renderdoc-shader-workbench"]
    assert "complete instructions are already in your own current context" in directory["skillUse"]
    assert "not what this actor has received" in directory["skillUse"]
    assert "reload absent or incomplete instructions" in directory["skillUse"]
    repeated = gateway.load("renderdoc-shader-workbench")
    assert repeated["instructions"] == first["instructions"]
    assert repeated["readReceipt"]["entryComplete"] is True


def test_finish_metadata_describes_source_save_and_actual_optional_effects(tmp_path: Path) -> None:
    from replaylab_agent.policy import tool_call_effects

    name = "finish_shader_workspace_organization"
    gateway = SkillGateway(_catalog(tmp_path), tool_names=[name], effects=["workspace"])
    contract = gateway.tool(name)["tool"]
    arguments = contract["effectArguments"]
    assert contract["baseEffects"] == ["workspace"]
    assert tool_call_effects(name, {}) == frozenset({"workspace", "replay", "project"})
    assert tool_call_effects(name, {"validate_for_apply": False}) == frozenset(contract["baseEffects"])
    for flag, effect in (("apply_shared_resource", "replay"),
                         ("show_in_workspace", "ui")):
        detail = next(value for key, value in arguments.items() if key.startswith(flag + " /"))
        assert detail == {"effect": effect, "default": False}
        assert tool_call_effects(name, {flag: True, "validate_for_apply": False}) == frozenset({"workspace", effect} | ({"project"} if effect == "replay" else set()))
    validate = arguments["validate_for_apply / validateForApply"]
    assert validate["default"] is None
    assert "final" in validate["condition"] and "checkpoint" in validate["condition"]
    live = arguments["start_live_edit / startLiveEdit"]
    assert live["default"] is False
    assert live["independentEffect"] is True
    assert "after apply_shared_resource applied" in live["condition"]
    assert tool_call_effects(name, {"start_live_edit": True}) == frozenset({"workspace", "replay", "project"})
    assert "publish_to_ui / publishToUi" not in arguments


def test_decompile_metadata_distinguishes_reference_from_validated_delivery(tmp_path: Path) -> None:
    gateway = SkillGateway(_catalog(tmp_path), tool_names=["decompile_shader_workspace"])
    contract = gateway.tool("decompile_shader_workspace")["tool"]
    assert contract["baseEffects"] == ["workspace"]
    assert contract["effectArguments"]["validate_for_apply / validateForApply"]["default"] is True
    assert ["workspace"] in contract["effectVariants"]
    assert ["replay", "workspace"] in contract["effectVariants"]


@pytest.mark.parametrize("name", ("classify_current_frame_passes", "apply_current_frame_pass_ai_proposals"))
def test_pass_metadata_retains_its_own_effect_selectors(tmp_path: Path, name: str) -> None:
    gateway = SkillGateway(_catalog(tmp_path), tool_names=[name])
    selectors = gateway.tool(name)["tool"]["effectArguments"]
    assert selectors["publish_to_ui / publishToUi"] == {"effect": "ui", "default": True}
    assert selectors["persist_project / persistProject"] == {"effect": "project", "default": False}
    assert "apply_shared_resource / applySharedResource" not in selectors


def test_capability_search_finds_granted_tools_by_name_or_description(tmp_path: Path) -> None:
    gateway = SkillGateway(
        _catalog(tmp_path),
        tool_names=[
            "write_shader_workspace_organization_file",
            "rename_shader_workspace_symbols",
            "inspect_event",
        ],
        tool_descriptions={
            "write_shader_workspace_organization_file": "Write organized shader draft text.",
            "rename_shader_workspace_symbols": "Rename identifiers in the workspace.",
            "inspect_event": "Inspect one capture event.",
        },
        effects=["workspace"],
    )
    hits = gateway.search("organization write")
    assert hits["matchCount"] >= 1
    names = {item["name"] for item in hits["tools"]}
    assert "write_shader_workspace_organization_file" in names
    empty = gateway.search("")
    assert "error" in empty


def test_loading_a_skill_returns_its_procedure_and_records_it(tmp_path: Path) -> None:
    gateway = SkillGateway(_catalog(tmp_path), tool_names=["inspect_event"])
    loaded = gateway.load("renderdoc-explain-event")
    assert loaded["id"] == "renderdoc-explain-event"
    assert "Step 1. Inspect." in loaded["instructions"]
    assert loaded["alreadyLoaded"] is False
    assert gateway.loaded_skill_ids == ("renderdoc-explain-event",)
    again = gateway.load("renderdoc-explain-event")
    assert again["alreadyLoaded"] is True
    assert gateway.directory()["loadedSkills"] == ["renderdoc-explain-event"]


def test_unknown_skill_is_refused_with_the_directory(tmp_path: Path) -> None:
    gateway = SkillGateway(_catalog(tmp_path))
    for identifier in ("no-such-skill", ""):
        result = gateway.load(identifier)
        assert "error" in result
        assert "renderdoc-shader-workbench" in result["availableSkills"]
    assert gateway.loaded_skill_ids == ()


def test_core_skill_is_an_ordinary_optional_skill(tmp_path: Path) -> None:
    """Core guidance is available when the Agent decides it is useful."""

    gateway = SkillGateway(_catalog(tmp_path))
    assert "replaylab-core" in [item["id"] for item in gateway.directory()["skills"]]
    result = gateway.load("replaylab-core")
    assert "error" not in result
    assert "alwaysLoaded" not in result
    assert result["instructions"]
    assert "replaylab-core" in gateway.directory()["loadedSkills"]


def test_local_tools_carry_no_effect_and_stay_out_of_the_host_manifest() -> None:
    assert AGENT_LOCAL_TOOLS == {DIRECTORY_TOOL, LOAD_SKILL_TOOL, INVOKE_TOOL, "record_task_review"}
    for name in AGENT_LOCAL_TOOLS:
        assert tool_effects(name) == frozenset()


def test_tool_families_group_by_purpose_not_by_name_length() -> None:
    families = {item["family"]: item for item in tool_families(
        ["inspect_event", "get_material_session", "export_resource_bundle"])}
    assert set(families) == {"capture-analysis", "materials", "resources"}
    assert families["materials"]["toolCount"] == 1


def test_resource_naming_review_is_direct_workspace_effect_not_replay():
    from replaylab_agent.policy import CORE_DIRECT_TOOLS, EFFECTFUL_TOOLS
    name = "record_shader_resource_naming_review"
    assert name in CORE_DIRECT_TOOLS and name in EFFECTFUL_TOOLS
    assert tool_effects(name) == {"workspace"}
    assert tool_families([name])[0]["family"] == "shader-workspace"


def test_directory_context_and_metadata_repair_are_available_to_all_connection_gateways():
    from replaylab_agent.policy import CORE_DIRECT_TOOLS, EFFECTFUL_TOOLS
    read = "get_shader_semantic_overview_context"
    update = "update_shader_semantic_overview"
    assert {read, update} <= set(CORE_DIRECT_TOOLS)
    assert read not in EFFECTFUL_TOOLS and update in EFFECTFUL_TOOLS
    assert tool_effects(read) == frozenset()
    assert tool_effects(update) == {"workspace", "project"}
    assert all(row["family"] == "shader-workspace" for row in tool_families([read, update]))


def test_general_review_discovery_is_separate_from_shader_review_and_available_without_capture(tmp_path: Path) -> None:
    names = ["record_task_review", "record_shader_organization_review"]
    catalog = _catalog(tmp_path)
    gateway = SkillGateway(catalog, tool_names=names, effects=[])
    directory = gateway.directory()
    families = {item["family"]: item for item in directory["toolFamilies"]}
    assert families["task-review"]["toolNames"] == ["record_task_review"]
    assert families["shader-workspace"]["toolNames"] == ["record_shader_organization_review"]
    assert gateway.search("task review")["tools"][0]["name"] == "record_task_review"
    assert gateway.tool("record_task_review")["tool"]["family"] == "task-review"
    # Discovering general review neither loads a capture Skill nor adds tools
    # missing from this connection's grant.
    assert gateway.loaded_skill_ids == ()
    assert directory["grantedToolCount"] == len(names)
    absent = SkillGateway(catalog, tool_names=[])
    assert "error" in absent.tool("record_task_review")


def test_disabled_skills_leave_the_catalog_even_for_bundled_core(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    config = AgentToolsConfig()
    config.set_skill_enabled("renderdoc-explain-event", False)
    config.set_skill_enabled("replaylab-core", False)
    filtered = config.filter_catalog(catalog)
    assert "renderdoc-explain-event" not in filtered.skills
    assert "replaylab-core" not in filtered.skills
    assert [item["id"] for item in SkillGateway(filtered).directory()["skills"]] == [
        "renderdoc-shader-workbench"]


def test_missing_skill_roots_do_not_block_host_tools(tmp_path: Path) -> None:
    empty = load_validated_skill_catalog([])
    assert empty.skills == {}
    missing = load_validated_skill_catalog([tmp_path / "missing"])
    assert missing.skills == {}
    assert any("Skill root unavailable" in item for item in missing.diagnostics)


def test_mcp_server_definitions_are_validated_before_they_reach_a_session() -> None:
    server = validate_mcp_server(
        {"id": "Docs", "title": "Docs", "command": "npx", "args": ["-y", "docs-mcp"],
         "env": {"TOKEN": "x"}})
    assert server.id == "docs" and server.transport == "stdio"
    for invalid in (
        {"id": "replaylab", "command": "x"},
        {"id": "bad id", "command": "x"},
        {"id": "ok"},
        {"id": "ok", "transport": "http", "url": "http://insecure"},
        {"id": "ok", "command": "x", "env": {"1bad": "v"}},
    ):
        with pytest.raises(ConfigurationError):
            validate_mcp_server(invalid)


def test_tools_config_round_trips_and_survives_a_broken_file(tmp_path: Path) -> None:
    path = tmp_path / "agent-tools.json"
    config = AgentToolsConfig()
    (tmp_path / "extra").mkdir()
    config.add_skill_root(tmp_path / "extra")
    config.upsert_mcp_server({"id": "docs", "command": "npx", "args": ["docs"]})
    config.set_skill_enabled("renderdoc-explain-event", False)
    config.save(path)
    reloaded = AgentToolsConfig.load(path)
    assert reloaded.skill_roots == (tmp_path / "extra",)
    assert [server.id for server in reloaded.enabled_mcp_servers()] == ["docs"]
    assert reloaded.disabled_skills == ("renderdoc-explain-event",)
    assert reloaded.skill_root_list(tmp_path / "bundled")[0] == tmp_path / "bundled"

    path.write_text("{ not json", encoding="utf-8")
    broken = AgentToolsConfig.load(path)
    assert broken.skill_roots == () and broken.diagnostics

    path.write_text(json.dumps({"mcpServers": [{"id": "bad id", "command": "x"},
                                               {"id": "good", "command": "y"}]}), encoding="utf-8")
    partial = AgentToolsConfig.load(path)
    assert [server.id for server in partial.mcp_servers] == ["good"]
    assert partial.diagnostics


def test_missing_optional_stdio_server_is_not_runtime_available(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.exe"
    missing = validate_mcp_server({
        "id": "optional", "command": str(missing_path)
    })
    assert mcp_server_runtime_status(missing) == (
        False,
        "stdio executable is unavailable: %s" % missing_path,
    )

    executable = tmp_path / "present.exe"
    executable.write_bytes(b"present")
    present = validate_mcp_server({"id": "present", "command": str(executable)})
    assert mcp_server_runtime_status(present) == (True, "")
    present.enabled = False
    assert mcp_server_runtime_status(present) == (False, "disabled")
    assert mcp_server_runtime_status(present, respect_enabled=False) == (True, "")


def test_a_family_expands_into_every_tool_it_holds(tmp_path: Path) -> None:
    """The directory groups tools to stay readable; this is the way back out.

    Without it an Agent seeing "capture-analysis, 31 tools" and eight examples has
    to guess whether the one it wants is among the other 23.
    """

    names = ["inspect_event", "get_draw_calls", "get_pass_flow", "inspect_shader"]
    gateway = SkillGateway(
        _catalog(tmp_path), tool_names=names,
        tool_descriptions={name: f"what {name} does" for name in names})
    directory = gateway.directory()
    assert "toolNames" in directory["expandWith"] or "family=<name>" in directory["expandWith"]
    capture = next(item for item in directory["toolFamilies"] if item["family"] == "capture-analysis")
    assert set(capture["toolNames"]) >= {"inspect_event", "get_draw_calls", "get_pass_flow"}

    expanded = gateway.family("capture-analysis")
    assert expanded["toolCount"] == 3
    assert [tool["name"] for tool in expanded["tools"]] == [
        "get_draw_calls", "get_pass_flow", "inspect_event"]
    assert expanded["tools"][0]["description"] == "what get_draw_calls does"

    # Every family the directory advertises can be expanded, and nothing else can.
    for entry in directory["toolFamilies"]:
        assert "error" not in gateway.family(entry["family"])
    refused = gateway.family("no-such-family")
    assert "error" in refused
    assert refused["families"] == [entry["family"] for entry in directory["toolFamilies"]]


def test_large_tool_families_are_split_into_bounded_namespaces(tmp_path: Path) -> None:
    names = ["inspect_event_%02d" % index for index in range(21)]
    gateway = SkillGateway(
        _catalog(tmp_path),
        tool_names=names,
        tool_descriptions={name: "Inspect one event." for name in names},
    )

    families = gateway.directory()["toolFamilies"]
    assert [entry["family"] for entry in families] == [
        "capture-analysis-1", "capture-analysis-2", "capture-analysis-3"
    ]
    assert sum(entry["toolCount"] for entry in families) == len(names)
    assert all(entry["toolCount"] <= MAX_TOOLS_PER_FAMILY for entry in families)
    assert all(
        gateway.family(entry["family"])["toolCount"] <= MAX_TOOLS_PER_FAMILY
        for entry in families
    )


def test_descriptions_can_arrive_after_the_tool_names(tmp_path: Path) -> None:
    """The task proxy learns the names before it has spoken to the Host."""

    gateway = SkillGateway(_catalog(tmp_path), tool_names=["inspect_event"])
    assert gateway.family("capture-analysis")["tools"][0]["description"] == ""
    gateway.describe_tools({"inspect_event": "Inspect one exact event."})
    assert gateway.family("capture-analysis")["tools"][0]["description"] == (
        "Inspect one exact event.")


def test_exact_tool_contract_is_loaded_only_after_catalog_selection(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "properties": {"event_id": {"type": "integer", "minimum": 1}},
        "required": ["event_id"],
        "additionalProperties": False,
    }
    gateway = SkillGateway(
        _catalog(tmp_path),
        tool_names=["inspect_event", "compile_shader_workspace"],
        tool_descriptions={
            "inspect_event": "Inspect one exact event and return pipeline facts.",
            "compile_shader_workspace": "Compile the current managed shader workspace.",
        },
        tool_schemas={"inspect_event": schema},
        effects=["workspace"],
    )

    directory = gateway.directory()
    assert all("inputSchema" not in family for family in directory["toolFamilies"])
    assert directory["invokeWith"] == INVOKE_TOOL
    assert directory["hostEffectGrant"]["allowed"] == ["workspace"]
    assert directory["vendorGenericToolPolicy"]["grantedByHostEffects"] is False
    assert directory["vendorGenericToolPolicy"]["availability"] == "provider-dependent"

    matches = gateway.search("event")
    assert matches["tools"][0]["name"] == "inspect_event"
    assert "inputSchema" not in matches["tools"][0]

    assert gateway.tool_contract_loaded("inspect_event") is False
    contract = gateway.tool("inspect_event")
    assert gateway.tool_contract_loaded("inspect_event") is True
    assert contract["alreadyLoaded"] is False
    assert contract["tool"]["inputSchema"] == schema
    assert contract["tool"]["effects"] == ["read-only"]
    assert contract["invokeWith"] == INVOKE_TOOL
    assert gateway.tool("inspect_event")["alreadyLoaded"] is True
    assert gateway.family("capture-analysis")["tools"][0]["contractLoaded"] is True
    assert gateway.tool_argument_errors("inspect_event", {}) == (
        "missing required parameter 'event_id'",
    )
    assert gateway.tool_argument_errors(
        "inspect_event", {"event_id": "11384", "event": 11384}
    ) == (
        "unexpected parameter 'event'",
        "arguments.event_id must be integer",
    )
    assert gateway.tool_argument_errors("inspect_event", {"event_id": 11384}) == ()
    assert "error" in gateway.tool("invented_tool")

    dispatch_schema = invoke_tool_schema()
    assert dispatch_schema["required"] == ["name", "arguments"]


def test_typed_asset_reader_facade_has_a_lazy_family_and_workspace_effect(
    tmp_path: Path,
) -> None:
    gateway = SkillGateway(
        _catalog(tmp_path),
        tool_names=["uex_material_shader_maps"],
        tool_schemas={
            "uex_material_shader_maps": {
                "type": "object",
                "properties": {"asset": {"type": "string"}},
                "required": ["asset"],
            }
        },
        effects=["workspace"],
    )

    family = gateway.family("asset-readers")
    assert [item["name"] for item in family["tools"]] == [
        "uex_material_shader_maps"
    ]
    assert family["tools"][0]["effects"] == ["workspace"]
    assert gateway.tool("uex_material_shader_maps")["tool"]["inputSchema"][
        "required"
    ] == ["asset"]


def test_discovery_reports_actor_direct_routes_without_reloading_or_widening_schema(tmp_path: Path) -> None:
    names = ["read_shader_workspace_file", "compile_shader_workspace", "ping"]
    schema = {"type": "object", "properties": {"workspace_path": {"type": "string"}},
              "required": ["workspace_path"], "additionalProperties": False}
    gateway = SkillGateway(_catalog(tmp_path), tool_names=names,
                           tool_schemas={name: schema for name in names})
    main_direct = names[:2]
    child_direct = names[:1]
    main = gateway.tool("compile_shader_workspace", direct_tools=main_direct)
    child = gateway.tool("compile_shader_workspace", direct_tools=child_direct)
    assert main["invokeWith"] == "compile_shader_workspace"
    assert main["directlyAvailable"] is True
    assert child["invokeWith"] == INVOKE_TOOL
    assert child["directlyAvailable"] is False
    # A shared delivery history does not pretend the child's own tool list
    # includes the parent's schema, nor remove the still discoverable contract.
    assert child["alreadyLoaded"] is True
    assert main["tool"]["inputSchema"] == child["tool"]["inputSchema"] == schema
    assert gateway.tool_argument_errors("compile_shader_workspace", {}) == (
        "missing required parameter 'workspace_path'",)
    assert gateway.tool("ping", direct_tools=main_direct)["invokeWith"] == INVOKE_TOOL
    for payload in (gateway.family("shader-workspace", direct_tools=child_direct),
                    gateway.search("shader", direct_tools=child_direct)):
        rows = {row["name"]: row for row in payload["tools"]}
        assert rows["read_shader_workspace_file"]["directlyAvailable"] is True
        assert rows["compile_shader_workspace"]["directlyAvailable"] is False
        assert all("inputSchema" not in row for row in rows.values())
    assert gateway.directory(direct_tools=main_direct)["directToolCount"] == 2
    assert gateway.directory(direct_tools=child_direct)["directToolCount"] == 1
    assert "error" in gateway.tool("not_granted", direct_tools=["not_granted"])


def test_resource_name_publication_declares_every_real_side_effect() -> None:
    assert tool_effects("publish_shader_resource_names") == {
        "workspace",
        "project",
        "ui",
    }


def test_parameterized_effects_are_visible_without_overstating_each_call(
    tmp_path: Path,
) -> None:
    name = "classify_current_frame_passes"
    gateway = SkillGateway(
        _catalog(tmp_path),
        tool_names=[name],
        tool_schemas={name: {"type": "object"}},
        effects=["ui"],
    )

    compact = gateway.family("capture-analysis")["tools"][0]
    assert compact["effectsDependOnArguments"] is True
    assert compact["effectVariants"] == [
        ["read-only"],
        ["ui"],
        ["project"],
        ["project", "ui"],
    ]
    exact = gateway.tool(name)["tool"]
    assert exact["effectsDependOnArguments"] is True
    assert exact["effectArguments"]["publish_to_ui / publishToUi"] == {
        "effect": "ui",
        "default": True,
    }
    assert exact["effectArguments"]["persist_project / persistProject"] == {
        "effect": "project",
        "default": False,
    }


def test_registered_extractors_are_named_in_the_directory(tmp_path: Path) -> None:
    """An Agent cannot decide to use an unpacker it has no way of knowing exists."""

    gateway = SkillGateway(
        _catalog(tmp_path),
        asset_tools=[
            {"id": "uex", "title": "UE Extractor",
             "description": "Unreal 4.27 pak and uasset",
             "interface": {"operations": [{"name": "extract", "proved": {"sample": "a.pak"}},
                                          {"name": "guessed"}]}},
        ],
    )
    entry = gateway.directory()["assetTools"][0]
    assert entry["id"] == "uex"
    assert entry["purpose"] == "Unreal 4.27 pak and uasset"
    # Only what was watched working counts, which is what the count has to say.
    assert entry["provedOperations"] == 1


def test_a_host_with_no_extractors_says_nothing_about_them(tmp_path: Path) -> None:
    assert "assetTools" not in SkillGateway(_catalog(tmp_path)).directory()


def _gateway_with(tmp_path: Path, bodies: dict[str, str]) -> SkillGateway:
    root = tmp_path / "skills"
    for name, body in bodies.items():
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            SKILL.format(name=name, description="test skill", category="shader",
                         when="The user asks.", body=body),
            encoding="utf-8")
    return SkillGateway(CatalogView.load(skill_roots=[root]), tool_names=["inspect_event"],
                        effects=["workspace"])


def test_a_long_skill_arrives_in_sections_the_client_will_not_spill(tmp_path: Path) -> None:
    """A 60 KB procedure in one tool result is a procedure the AI never reads.

    A vendor client that writes a large tool result to a file and then cannot read
    that file back from its own sandbox turns a loaded Skill into "permission
    denied" -- observed, three runs in a row. So a long Skill is delivered head
    first, with its section list, and each remaining section on request.
    """

    from replaylab_agent import skill_gateway

    long_skill = "Intro paragraph.\n\n" + "".join(
        "## %d. Step\n%s\n\n" % (index, "x" * 9000) for index in range(6))
    gateway = _gateway_with(tmp_path, {"demo": long_skill})

    first = gateway.load("demo")
    assert first["sectioned"] is True
    assert len(encode(first).encode("utf-8")) <= skill_gateway.MAX_SKILL_INLINE_BYTES
    assert first["instructions"].startswith("Intro paragraph.")
    assert [item["section"] for item in first["sections"]] == ["%d. Step" % i for i in range(6)]
    assert first["deliveredSections"] == [item["section"] for item in first["sections"]
                                          if item["delivered"]]
    assert first["deliveredSections"], "the head of the procedure is always delivered"
    assert len(first["deliveredSections"]) < 6, "the tail waits to be asked for"

    # Every section that was not delivered is reachable by name, exactly.
    pending = [item["section"] for item in first["sections"] if not item["delivered"]]
    for heading in pending:
        piece = gateway.load("demo", heading)
        assert piece["section"] == heading
        assert piece["instructions"].startswith("## %s" % heading)
    assert "error" in gateway.load("demo", "no such section")

    # A short Skill is still delivered whole, in one piece.
    short = _gateway_with(tmp_path / "b", {"tiny": "## Only\ndo the thing\n"}).load("tiny")
    assert "sectioned" not in short
    assert short["instructions"].strip().endswith("do the thing")


def test_large_skill_section_is_losslessly_cursor_paged_with_bounded_results(
    tmp_path: Path,
) -> None:
    body = "## Oversized procedure\n" + "".join(
        "step-%05d: %s\n" % (index, "数据" * 23) for index in range(1400)
    )
    gateway = _gateway_with(tmp_path, {"demo": body})
    _preamble, sections = split_sections(
        gateway._catalog.skill("demo").instructions.strip()
    )
    expected = dict(sections)["Oversized procedure"]

    offset = 0
    pieces: list[str] = []
    while True:
        page = gateway.load("demo", "Oversized procedure", offset)
        assert "error" not in page
        assert len(encode(page).encode("utf-8")) <= MAX_SKILL_INLINE_BYTES
        assert page["offset"] == offset
        assert page["nextOffset"] > offset
        pieces.append(page["instructions"])
        if not page["hasMore"]:
            break
        offset = page["nextOffset"]

    assert "".join(pieces) == expected
    assert len(pieces) > 2


def test_bundled_shader_workbench_large_sections_are_all_reachable_without_spill() -> None:
    root = Path(__file__).resolve().parents[2] / ".agents" / "skills"
    catalog = CatalogView.load(skill_roots=[root])
    gateway = SkillGateway(catalog)
    skill = catalog.skill("renderdoc-shader-workbench")
    _preamble, sections = split_sections(skill.instructions.strip())

    initial = gateway.load(skill.id)
    assert len(encode(initial).encode("utf-8")) <= MAX_SKILL_INLINE_BYTES
    for heading, expected in sections:
        pieces: list[str] = []
        offset = 0
        while True:
            page = gateway.load(skill.id, heading, offset)
            assert "error" not in page
            assert len(encode(page).encode("utf-8")) <= MAX_SKILL_INLINE_BYTES
            pieces.append(page["instructions"])
            if not page["hasMore"]:
                break
            assert page["nextOffset"] > offset
            offset = page["nextOffset"]
        assert "".join(pieces) == expected


def test_skill_reference_directory_returns_calls_that_load_exact_local_text(tmp_path: Path) -> None:
    gateway = _gateway_with(tmp_path, {"demo": "Read the reference when useful."})
    references = tmp_path / "skills" / "demo" / "references"
    references.mkdir()
    body = "# Naming evidence\n\nKeep existing names.\n"
    (references / "naming.md").write_bytes(body.encode("utf-8"))
    (references / "unrelated.txt").write_text("not a reference", encoding="utf-8")
    initial = gateway.load("demo")
    assert initial["references"] == ["references/naming.md"]
    assert initial["referenceLoads"] == [{
        "tool": LOAD_SKILL_TOOL,
        "arguments": {"skill_id": "demo", "section": "references/naming.md"},
    }]
    page = gateway.load(**initial["referenceLoads"][0]["arguments"])
    assert page["instructions"] == body
    assert page["reference"] is True
    assert page["hasMore"] is False
    assert page["offset"] == 0
    assert page["nextOffset"] == len(body)


def test_skill_reference_is_losslessly_paged_with_executable_continuations(tmp_path: Path) -> None:
    gateway = _gateway_with(tmp_path, {"demo": "Small main Skill."})
    reference = tmp_path / "skills" / "demo" / "references" / "large.md"
    reference.parent.mkdir()
    body = "# Details\n" + "值和上下文\n" * 3300
    reference.write_bytes(body.encode("utf-8"))
    parts = []
    offset = 0
    while True:
        page = gateway.load("demo", "references/large.md", offset)
        assert "error" not in page
        assert len(encode(page).encode("utf-8")) <= MAX_SKILL_INLINE_BYTES
        parts.append(page["instructions"])
        if not page["hasMore"]:
            break
        assert page["nextOffset"] > offset
        offset = page["nextOffset"]
        assert page["next"] == (
            "load_bread_skill(skill_id='demo', section='references/large.md', offset=%d)" % offset
        )
    assert len(parts) > 1
    assert "".join(parts) == body
    assert "error" in gateway.load("demo", "references/large.md", len(body) + 1)
    assert "error" in gateway.load("demo", "references/large.md", True)


@pytest.mark.parametrize("section", [
    "references/../../outside.md", "references/../outside.md",
    "references/sub/../../outside.md", "references//outside.md",
    "references/..\\outside.md", "references/C:outside.md",
    "references/outside.md:stream.md", "references/outside.txt",
])
def test_reference_cannot_read_paths_outside_its_skill(tmp_path: Path, section: str) -> None:
    gateway = _gateway_with(tmp_path, {"demo": "Small main Skill."})
    (tmp_path / "skills" / "outside.md").write_text("PRIVATE OUTSIDE TEXT", encoding="utf-8")
    result = gateway.load("demo", section)
    assert "error" in result
    assert "instructions" not in result
    assert "PRIVATE OUTSIDE TEXT" not in encode(result)


def test_reference_rejects_oversize_non_utf8_and_missing_files(tmp_path: Path) -> None:
    gateway = _gateway_with(tmp_path, {"demo": "Small main Skill."})
    reference = tmp_path / "skills" / "demo" / "references" / "test.md"
    reference.parent.mkdir()
    reference.write_bytes(b"x" * (64 * 1024))
    assert "error" not in gateway.load("demo", "references/test.md")
    reference.write_bytes(b"x" * (64 * 1024 + 1))
    assert "64 KiB" in gateway.load("demo", "references/test.md")["error"]
    assert "references" not in gateway.load("demo")
    reference.write_bytes(b"\xffinvalid")
    assert "error" in gateway.load("demo", "references/test.md")
    assert "error" in gateway.load("demo", "references/missing.md")


@pytest.mark.parametrize("directory_link", [False, True])
def test_reference_rejects_file_and_directory_symlinks(tmp_path: Path, directory_link: bool) -> None:
    gateway = _gateway_with(tmp_path, {"demo": "Small main Skill."})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "linked.md").write_text("PRIVATE LINKED TEXT", encoding="utf-8")
    references = tmp_path / "skills" / "demo" / "references"
    try:
        if directory_link:
            references.symlink_to(outside, target_is_directory=True)
        else:
            references.mkdir()
            (references / "linked.md").symlink_to(outside / "linked.md")
    except OSError as error:
        pytest.skip("symbolic links unavailable: %s" % error)
    result = gateway.load("demo", "references/linked.md")
    assert "error" in result
    assert "instructions" not in result
    assert "symlinks or junctions" in result["error"]
    assert "references" not in gateway.load("demo")


@pytest.mark.parametrize("attribute", ["is_symlink", "is_junction"])
def test_reference_redirection_guard_is_enforced_without_link_creation_privileges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attribute: str,
) -> None:
    gateway = _gateway_with(tmp_path, {"demo": "Small main Skill."})
    reference = tmp_path / "skills" / "demo" / "references" / "linked.md"
    reference.parent.mkdir()
    reference.write_text("Must not be read", encoding="utf-8")
    original = getattr(Path, attribute)
    monkeypatch.setattr(Path, attribute, lambda path: path == reference or original(path))
    result = gateway.load("demo", "references/linked.md")
    assert "symlinks or junctions" in result["error"]
    assert "instructions" not in result
    assert "references" not in gateway.load("demo")


def test_reference_metadata_counts_toward_inline_skill_limit(tmp_path: Path) -> None:
    body = "x" * (MAX_SKILL_INLINE_BYTES - 200)
    gateway = _gateway_with(tmp_path, {"demo": body})
    references = tmp_path / "skills" / "demo" / "references"
    references.mkdir()
    (references / "naming-and-recovery-procedure.md").write_text("Details.", encoding="utf-8")
    page = gateway.load("demo")
    assert len(encode(page).encode("utf-8")) <= MAX_SKILL_INLINE_BYTES
    assert page["hasMore"] is True
    rest = gateway.load("demo", offset=page["nextOffset"])
    assert page["instructions"] + rest["instructions"] == body


def test_bundled_shader_references_are_loadable_without_host_file_access() -> None:
    root = Path(__file__).resolve().parents[2] / ".agents" / "skills"
    gateway = SkillGateway(CatalogView.load(skill_roots=[root]))
    initial = gateway.load("renderdoc-shader-workbench")
    assert initial["referenceLoads"]
    for load in initial["referenceLoads"]:
        arguments = load["arguments"]
        parts = []
        while True:
            page = gateway.load(**arguments)
            assert "error" not in page
            assert len(encode(page).encode("utf-8")) <= MAX_SKILL_INLINE_BYTES
            parts.append(page["instructions"])
            if not page["hasMore"]:
                break
            arguments = {**arguments, "offset": page["nextOffset"]}
        expected = (root / "renderdoc-shader-workbench" / arguments["section"]).read_bytes().decode("utf-8")
        assert "".join(parts) == expected
