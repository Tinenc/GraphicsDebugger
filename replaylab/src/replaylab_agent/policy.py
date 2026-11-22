"""Deterministic effect classes for the first-party ReplayLab tool catalog.

The legacy workflow state machine intentionally does not live here. This
module answers only whether a tool is read-only and which connection-level
effect classes an admitted call may exercise.
"""

from __future__ import annotations

from typing import Any, Iterable

from .errors import PolicyError


# Served locally by the task runtime: capability discovery, Skill loading, lazy
# Host dispatch and completion self-review. Review also has a compatible Host
# entry point; local handling keeps it available without a capture connection.
# The dispatcher checks the selected Host tool's effect before forwarding.
AGENT_LOCAL_TOOLS = frozenset(
    {"list_bread_capabilities", "load_bread_skill", "invoke_bread_tool", "record_task_review"}
)

# These are the small, typed first-party surface shown directly to an Agent.
# The Host may retain other operations for existing UI/project integrations;
# they remain discoverable on demand instead of filling every model turn.
# This list is a capability list, not an order in which work must be done.
CORE_DIRECT_TOOLS: tuple[str, ...] = (
    "publish_visual_reply",
    "get_cbuffer_parameter_link",
    "get_mesh_uv_link",
    "capture_mesh_uv_evidence",
    "run_shader_effect_experiment",
    "get_current_renderdoc_context",
    "get_application_ui_state",
    "control_application_ui",
    "prepare_frame_render_flow",
    "get_frame_render_flow",
    "inspect_frame_render_stage",
    "inspect_event_api_parameters",
    "inspect_frame_resource_usage",
    "validate_frame_render_flow",
    "publish_frame_render_flow",
    "get_host_runtime_status",
    "inspect_event",
    "inspect_pixel_object",
    "inspect_shader",
    "get_bound_textures",
    "search_unpacked_assets",
    "read_unpacked_asset",
    "get_active_shader_workspace_path",
    "get_shader_workspace_status",
    "get_shader_workspace_ui_state",
    "show_shader_workspace_source",
    "prepare_shader_workspace",
    "decompile_shader_workspace",
    "read_shader_workspace_file",
    "read_shader_workspace_source",
    "get_shader_workspace_outline",
    "begin_shader_workspace_organization",
    "write_shader_workspace_organization_file",
    "rename_shader_workspace_symbols",
    "restructure_shader_workspace_source",
    "get_shader_workspace_mutation_status",
    "finish_shader_workspace_organization",
    "diagnose_shader_organization_candidate",
    "record_shader_organization_review",
    "get_shader_semantic_overview_context",
    "update_shader_semantic_overview",
    "record_shader_resource_naming_review",
    "publish_shader_resource_names",
    "publish_cbuffer_aliases",
    "compile_shader_workspace",
    "convert_shader_workspace_to_hlsl",
    "validate_shader_workspace_candidate",
    "validate_shader_workspace_delivery",
    "register_active_project_workspace",
    "apply_shader_workspace_candidate",
    "apply_shader_workspace_shared_resource",
    "list_shader_workspace_live_edits",
    "start_shader_workspace_live_edit",
    "stop_shader_workspace_live_edit",
    "rollback_shader_workspace_shared_resource",
)

# Only a stable display order. The model decides which tool is useful.
CORE_TOOL_PRIORITY: tuple[str, ...] = (
    *CORE_DIRECT_TOOLS,
    "list_bread_capabilities",
    "load_bread_skill",
    "invoke_bread_tool",
    "ask_user",
    "record_task_review",
)


def ordered_tool_names(tool_names: Iterable[str]) -> list[str]:
    """Stable grant order: core tools first, then the remaining names sorted."""

    remaining = {
        str(name or "").strip()
        for name in tool_names
        if str(name or "").strip()
    }
    ordered: list[str] = []
    for name in CORE_TOOL_PRIORITY:
        if name in remaining:
            ordered.append(name)
            remaining.remove(name)
    ordered.extend(sorted(remaining))
    return ordered


READ_ONLY_TOOLS = frozenset(
    {
        "review_unity_view_evidence",
        "localize_unity_effect_difference",
        "review_unity_temporal_stability",
        "review_unity_restoration_handoff",
        "get_shader_semantic_overview_context",
        "get_replay_health",
        "ping",
        "get_capture_status",
        # Polls long-tool liveness and Host/qrenderdoc memory samples; never mutates.
        "get_host_runtime_status",
        "get_current_renderdoc_context",
        "get_application_ui_state",
        "prepare_frame_render_flow",
        "get_frame_render_flow",
        "validate_frame_render_flow",
        "inspect_frame_render_stage",
        "inspect_event_api_parameters",
        "inspect_frame_resource_usage",
        "read_shader_workspace_file",
        "read_shader_workspace_source",
        # Reads back a file the Host itself produced -- text as text, an image as an
        # image -- so a saved path is evidence on routes that only carry tool text.
        # Restricted to the Host's own evidence roots; it is not a file browser.
        "read_evidence_file",
        # Looks a Shader up in the project's shared library by an identity that does
        # not depend on which capture it was seen in. Reads; decides nothing.
        "find_reusable_shader_work",
        "get_shared_shader_library",
        # Sorts a capture's Shaders into already-done / reusable / new. Reads only.
        "plan_capture_shader_reuse",
        # Optional operation advice and managed Unity inspection never install,
        # connect, import, or turn an absent reader into a Shader prerequisite.
        "get_draw_workflow_plan",
        "validate_unity_render_plan",
        "compare_unity_render_images",
        "compare_unity_effect_layers",
        "trace_unity_effect_dependencies",
        "plan_unity_asset_layout",
        "inspect_unity_project",
        "get_unity_integration_job",
        "inspect_unreal_project",
        "get_unreal_integration_job",
        "get_unreal_restoration_recipe_schema",
        "compare_unreal_restoration",
        "review_unreal_restoration_layers",
        "get_unreal_validation_schema",
        "review_unreal_hdr_sequence",
        "get_unity_shader_transfer_sources",
        "get_unity_restoration_status",
        "get_unity_restoration_step",
        "get_resource_export_payload",
        # The MCP-shaped catalog of everything that can read or unpack an asset.
        "list_asset_tool_operations",
        "get_shader_workspace_outline",
        "get_resource_export_spec_template",
        "analyze_vertex_input_roles",
        # Resource relationships around one event: who wrote what it reads, who reads
        # what it writes. Relationships only; every judgement is the model's.
        "get_event_resource_graph",
        "get_event_buffer_data",
        "get_draw_intent",
        # Search the configured asset root directly; no catalog, no scan, read-only.
        "search_unpacked_assets",
        "list_unpacked_asset_directory",
        "read_unpacked_asset",
        "find_unpacked_asset_relations",
        "describe_unpacked_asset",
        # Folder counts from the names index: what is where, before anything is parsed.
        "profile_unpacked_asset_directories",
        # Reads one constant buffer across several draws and reports which lanes
        # change; no asset involved, nothing written.
        "profile_constant_buffer_variance",
        # Reads one resolved material and proposes names for this draw's constants
        # and texture slots. Proposes only; applying them is a separate, effectful call.
        "propose_shader_names_from_asset",
        "decode_material_shader_outputs",
        # One list of external tools, their roles and the interfaces discovered for them.
        "list_bread_tools",
        # Looks for installed extractors; reports, never registers.
        "discover_asset_readers",
        # Starts one registered tool and reports whether it answers. Changes nothing.
        "test_tool_connection",
        "get_bread_configuration",
        "inspect_event",
        "analyze_current_frame_flow",
        "analyze_pixel",
        "inspect_shader",
        "debug_vulkan_bindings",
        "analyze_lighting",
        "get_draw_calls",
        "get_frame_summary",
        "get_draw_call_details",
        "get_pipeline_state",
        "get_bound_textures",
        "find_draws_by_shader",
        "find_draws_by_texture",
        "find_draws_by_resource",
        "get_textures",
        "get_buffers",
        "get_resources",
        "get_texture_info",
        "get_texture_data",
        "get_buffer_contents",
        "pick_pixel",
        "get_texture_minmax",
        "pixel_history",
        "inspect_pixel_object",
        "debug_pixel",
        "debug_vertex",
        "enumerate_counters",
        "fetch_counters",
        "get_debug_messages",
        "get_action_timings",
        "get_symbol_match_cases",
        "get_symbol_match_evidence",
        "list_symbol_matches",
        "get_shader_workspace_status",
        "get_shader_workspace_ui_state",
        # Polls the retained receipt for an already-started mutation; never starts
        # or repeats the rename/restructure/publication operation.
        "get_shader_workspace_mutation_status",
        "poll_shader_workspace_live_edit",
        "list_shader_workspace_live_edits",
        "inspect_shader_resource_occupancy",
        "list_shader_workspace_family_uses",
        "get_shader_toolchain_capabilities",
        "get_shader_repair_context",
        "plan_shader_toolchain_route",
        "plan_shader_recovery",
        "inspect_asset_shader_recovery",
        "list_material_sessions",
        "get_material_session_status",
        "poll_shader_workspace_watch",
        "get_replay_project_status",
        "get_replay_project_reuse_plan",
        "get_active_replay_project_status",
        "get_active_capture_catalog_binding_status",
        "get_active_project_future_frame_plan",
        "get_resource_export_capabilities",
        "get_resource_export_status",
        "inspect_resource_export",
        "validate_resource_export",
        "query_active_shader_asset_evidence",
        "get_unpacked_asset_index_capabilities",
        "get_active_unpacked_asset_index_status",
        "get_unpacked_asset_provider_capabilities",
        "get_unpacked_asset_provider_decisions",
        "get_unpacked_asset_job_status",
        "query_active_unpacked_assets",
        "query_unpacked_asset_inventory_assets",
        "resolve_active_unpacked_asset",
        "fingerprint_event_outputs",
        "get_post_vs_data",
        "inspect_vertex_effect_scope",
    }
)


EFFECTFUL_TOOLS = frozenset(
    {
        "publish_visual_reply",
        "get_cbuffer_parameter_link",
        "get_mesh_uv_link",
        "capture_mesh_uv_evidence",
        "run_shader_effect_experiment",
        "recover_replay",
        "open_capture",
        "launch_renderdoc",
        "list_captures",
        "identify_drawcalls",
        "capture_event_texture_evidence",
        "capture_draw_image",
        "capture_mesh_image",
        "capture_texture_image",
        # Writes organized work into the project's shared Shader library.
        "publish_shader_work",
        "record_shader_reuse_applied",
        # Runs a registered extractor; writes only into a Host-owned output directory.
        "run_asset_tool_operation",
        # Typed facade over the same registered extractor/output transaction.
        "uex_material_shader_maps",
        "record_asset_tool_operation",
        "render_current_frame_flow_diagram",
        "publish_frame_render_flow",
        "classify_current_frame_passes",
        "export_capture_symbol_catalog",
        "scan_asset_symbols",
        "match_capture_assets",
        "resolve_capture_symbols",
        "submit_symbol_match",
        "export_symbol_alias_table",
        "apply_symbol_alias_table",
        "rollback_symbol_alias_table",
        # Publishes resolved texture/CBuffer display names into the current RDC
        # and project naming ledger before source-level Shader organization.
        "publish_shader_resource_names",
        "create_shader_workspace",
        "prepare_shader_workspace",
        "try_shader_workspace_recovery_candidate",
        "get_current_shader_representation",
        "decompile_shader_workspace",
        "get_shader_workspace_representation",
        "promote_shader_workspace_explanation",
        "compile_shader_workspace",
        "convert_shader_workspace_to_hlsl",
        "migrate_shader_workspace_toolchain",
        "begin_shader_workspace_organization",
        "write_shader_workspace_organization_file",
        "rename_shader_workspace_symbols",
        # Names rows of a constant buffer for the RenderDoc panel. Wider than a
        # rename (it reaches rows this shader never reads) and narrower in effect
        # (nothing about the shader or its reflection changes).
        "publish_cbuffer_aliases",
        "restructure_shader_workspace_source",
        "finish_shader_workspace_organization",
        "diagnose_shader_organization_candidate",
        "record_shader_organization_review",
        "update_shader_semantic_overview",
        "record_shader_resource_naming_review",
        "show_shader_workspace_source",
        "control_application_ui",
        "write_restoration_document",
        "open_restoration_document",
        "export_unity_volume_texture",
        "export_unity_texture_data",
        "validate_shader_workspace_candidate",
        "validate_shader_workspace_delivery",
        "apply_shader_workspace_candidate",
        "apply_shader_workspace_candidate_family",
        "apply_shader_workspace_shared_resource",
        "rollback_shader_workspace_override",
        "rollback_shader_workspace_override_family",
        "rollback_shader_workspace_shared_resource",
        "prepare_material_session",
        "update_material_parameter",
        "update_material_parameters",
        "rollback_material_session",
        "clear_all_material_sessions",
        "start_shader_workspace_watch",
        "stop_shader_workspace_watch",
        "start_shader_workspace_live_edit",
        "migrate_shader_workspace_live_edit_authorization",
        "stop_shader_workspace_live_edit",
        "release_task_shader_occupancy",
        "run_shader_toolchain_round_trip_smoke",
        "create_replay_project",
        "ensure_active_replay_project",
        "activate_active_replay_project",
        "configure_active_replay_project",
        "configure_active_project_asset_root",
        "import_replay_project_capture",
        "register_replay_project_workspace",
        "register_replay_project_alias_table",
        "save_replay_project_material_knowledge",
        "seed_shader_workspace_from_replay_project",
        "seed_active_shader_workspace",
        "prepare_active_capture_project",
        "get_active_shader_workspace_path",
        "register_active_project_workspace",
        "register_active_project_shader_replacement",
        "register_active_project_material_session",
        "restore_current_shader",
        "apply_current_frame_pass_ai_proposals",
        "prepare_resource_export",
        "finalize_resource_export",
        "run_resource_export_target_validation",
        "start_unity_project_connection",
        "start_unreal_project_connection",
        "probe_unreal_rendering",
        "call_unreal_project_tool",
        "import_resource_export_to_unreal",
        "render_unreal_restoration",
        "preflight_unreal_materials",
        "cancel_unity_integration_job",
        "call_unity_project_tool",
        "import_resource_export_to_unity",
        "connect_unity_project_mcp",
        "begin_unity_restoration",
        "plan_unity_restoration_sequence",
        "annotate_unity_restoration_step",
        "fork_resource_export",
        "plan_unity_restoration_recovery",
        "apply_unity_recovery_assets",
        "record_unity_reconstruction",
        "adopt_unity_restoration_render_plan",
        "use_unity_visual_fidelity_goal",
        "bind_unity_restoration",
        "check_unity_restoration",
        "render_unity_restoration",
        "review_unity_restoration",
        "index_active_unpacked_assets",
        # Parses the folders the AI chose into the project's asset index (scoped, never bulk).
        "hydrate_unpacked_asset_scope",
        # Records which engine version this project's unpacked tree was cooked with.
        "set_unpacked_asset_engine_version",
        "start_unpacked_asset_report",
        "start_unpacked_asset_blender_export",
        "continue_unpacked_asset_blender_export",
        # AI-authored ABI repair of the workspace source (compile + replay
        # reflection, restored on failure) and the asset-matching memory.
        "repair_shader_workspace_source_abi",
        "retrieve_shader_asset_candidates",
        "submit_shader_asset_resolution",
        # One call: capture the draw, map the channels the vertex shader identified,
        # write the package and the readable texture copies.
        "export_draw_resources",
        "forget_shader_asset_resolution",
        # Joins/starts the project's background asset scan and waits for it.
        "wait_for_unpacked_asset_index",
        # Probes readers against the tree and remembers the winner in the project.
        "verify_unpacked_asset_readers",
        # Asks a tool what it can do, proves it against this project's assets, and
        # writes the interface back into the configuration.
        "discover_tool_interface",
        # Write what a draw is taken to be, and check it back. Project knowledge.
        "record_draw_intent",
        "verify_draw_intent",
        # Edits the Settings > AI configuration on the user's behalf.
        "update_bread_configuration",
    }
)


WORKFLOW_EFFECTS = frozenset({"workspace", "project", "ui", "replay"})


TOOL_EFFECTS: dict[str, frozenset[str]] = {
    "publish_visual_reply": frozenset({"workspace", "ui"}),
    "get_cbuffer_parameter_link": frozenset({"workspace"}),
    "get_mesh_uv_link": frozenset({"workspace"}),
    "capture_mesh_uv_evidence": frozenset({"workspace", "ui"}),
    "run_shader_effect_experiment": frozenset({"workspace", "replay"}),
    "recover_replay": frozenset({"ui", "replay"}),
    **{
        name: frozenset({"workspace"})
        for name in (
            "create_shader_workspace",
            "prepare_shader_workspace",
            "try_shader_workspace_recovery_candidate",
            "migrate_shader_workspace_toolchain",
            "apply_shader_workspace_candidate",
            "get_current_shader_representation",
            "decompile_shader_workspace",
            "get_shader_workspace_representation",
            "promote_shader_workspace_explanation",
            "compile_shader_workspace",
            "begin_shader_workspace_organization",
            "write_shader_workspace_organization_file",
            "record_shader_organization_review",
            "update_shader_semantic_overview",
            "record_shader_resource_naming_review",
            "rename_shader_workspace_symbols",
            "publish_cbuffer_aliases",
            "restructure_shader_workspace_source",
            "validate_shader_workspace_candidate",
            "start_shader_workspace_watch",
            "stop_shader_workspace_watch",
            "run_shader_toolchain_round_trip_smoke",
            "open_capture",
            "launch_renderdoc",
            "list_captures",
            "identify_drawcalls",
            "capture_event_texture_evidence",
            "capture_draw_image",
            "capture_mesh_image",
            "capture_texture_image",
            "publish_shader_work",
            "record_shader_reuse_applied",
            "run_asset_tool_operation",
            "uex_material_shader_maps",
            "record_asset_tool_operation",
            "render_current_frame_flow_diagram",
            "prepare_material_session",
            "update_material_parameter",
            "rollback_material_session",
            "clear_all_material_sessions",
            "prepare_resource_export",
            "finalize_resource_export",
            "run_resource_export_target_validation",
            "start_unpacked_asset_report",
            "start_unpacked_asset_blender_export",
            "continue_unpacked_asset_blender_export",
        )
    },
    **{
        name: frozenset({"project"})
        for name in (
            "create_replay_project",
            "ensure_active_replay_project",
            "activate_active_replay_project",
            "configure_active_replay_project",
            "configure_active_project_asset_root",
            "import_replay_project_capture",
            "register_replay_project_workspace",
            "register_replay_project_alias_table",
            "save_replay_project_material_knowledge",
            "seed_shader_workspace_from_replay_project",
            "register_active_project_shader_replacement",
            "register_active_project_material_session",
            "export_capture_symbol_catalog",
            "scan_asset_symbols",
            "match_capture_assets",
            "resolve_capture_symbols",
            "submit_symbol_match",
            "export_symbol_alias_table",
            "apply_symbol_alias_table",
            "index_active_unpacked_assets",
            "hydrate_unpacked_asset_scope",
            "set_unpacked_asset_engine_version",
            "verify_unpacked_asset_readers",
            "discover_tool_interface",
            "record_draw_intent",
            "verify_draw_intent",
            "repair_shader_workspace_source_abi",
            # first use may build/hydrate the project's asset catalog
            "retrieve_shader_asset_candidates",
            "export_draw_resources",
            "wait_for_unpacked_asset_index",
        )
    },
    "update_bread_configuration": frozenset({"ui"}),
    "show_shader_workspace_source": frozenset({"ui"}),
    # Normal application controls can save documents, change replay or project settings.
    "control_application_ui": frozenset({"ui", "replay", "workspace", "project"}),
    "publish_frame_render_flow": frozenset({"ui", "workspace"}),
    **{
        name: frozenset({"replay"})
        for name in (
            "apply_shader_workspace_candidate",
            "apply_shader_workspace_candidate_family",
            "apply_shader_workspace_shared_resource",
            "rollback_shader_workspace_override",
            "rollback_shader_workspace_override_family",
            "rollback_shader_workspace_shared_resource",
            "restore_current_shader",
        )
    },
    "classify_current_frame_passes": frozenset({"project", "ui"}),
    # Discovery describes all supported forms. Dispatch derives the exact
    # effects from finish flags so a source save needs no replay grant.
    "finish_shader_workspace_organization": frozenset({"workspace", "ui", "replay", "project"}),
    "decompile_shader_workspace": frozenset({"workspace", "replay"}),
    "convert_shader_workspace_to_hlsl": frozenset({"workspace", "replay"}),
    "validate_shader_workspace_delivery": frozenset({"workspace", "replay"}),
    "diagnose_shader_organization_candidate": frozenset({"workspace", "replay"}),
    "register_active_project_workspace": frozenset({"project"}),
    "apply_current_frame_pass_ai_proposals": frozenset({"project", "ui"}),
    "prepare_active_capture_project": frozenset({"project", "ui"}),
    "get_active_shader_workspace_path": frozenset({"project", "replay"}),
    "apply_symbol_alias_table": frozenset({"project", "ui"}),
    # Persists the publication under workspace/ai, updates project-owned names,
    # changes the current RDC UI, and invalidates stale review/delivery receipts.
    "publish_shader_resource_names": frozenset({"workspace", "project", "ui"}),
    "update_shader_semantic_overview": frozenset({"workspace", "project"}),
    # Both operations can publish the active project's shared CBuffer alias
    # dictionary in addition to changing workspace receipts/source. They do not
    # call the bridge or notify an already-open panel, so ``ui`` is not claimed.
    "publish_cbuffer_aliases": frozenset({"workspace", "project"}),
    "rename_shader_workspace_symbols": frozenset({"workspace", "project"}),
    "rollback_symbol_alias_table": frozenset({"ui"}),
    "update_material_parameter": frozenset({"workspace", "replay"}),
    "update_material_parameters": frozenset({"workspace", "replay"}),
    "rollback_material_session": frozenset({"workspace", "replay"}),
    "clear_all_material_sessions": frozenset({"workspace", "replay"}),
    "seed_active_shader_workspace": frozenset({"workspace"}),
    "submit_shader_asset_resolution": frozenset({"project"}),
    "forget_shader_asset_resolution": frozenset({"project"}),
    "start_shader_workspace_live_edit": frozenset({"workspace", "replay"}),
    "migrate_shader_workspace_live_edit_authorization": frozenset(
        {"workspace", "replay"}
    ),
    "stop_shader_workspace_live_edit": frozenset({"workspace", "replay"}),
    "release_task_shader_occupancy": frozenset({"workspace", "replay"}),
    # External Unity project writes are not permissions to alter RenderDoc replay.
    # Connection/import also persist managed jobs and operation receipts.
    "start_unity_project_connection": frozenset({"project", "workspace"}),
    "start_unreal_project_connection": frozenset({"project", "workspace"}),
    "probe_unreal_rendering": frozenset({"project"}),
    "call_unreal_project_tool": frozenset({"project"}),
    "import_resource_export_to_unreal": frozenset({"project", "workspace"}),
    "render_unreal_restoration": frozenset({"project", "workspace"}),
    "preflight_unreal_materials": frozenset({"project"}),
    "import_resource_export_to_unity": frozenset({"project", "workspace"}),
    "cancel_unity_integration_job": frozenset({"workspace"}),
    "call_unity_project_tool": frozenset({"project"}),
    "connect_unity_project_mcp": frozenset({"project", "workspace"}),
    "begin_unity_restoration": frozenset({"project", "workspace"}),
    "plan_unity_restoration_sequence": frozenset({"workspace"}),
    "annotate_unity_restoration_step": frozenset({"workspace"}),
    "fork_resource_export": frozenset({"workspace"}),
    "plan_unity_restoration_recovery": frozenset({"workspace"}),
    "apply_unity_recovery_assets": frozenset({"project", "workspace"}),
    "record_unity_reconstruction": frozenset({"workspace"}),
    "adopt_unity_restoration_render_plan": frozenset({"workspace"}),
    "use_unity_visual_fidelity_goal": frozenset({"workspace"}),
    "bind_unity_restoration": frozenset({"project", "workspace"}),
    "check_unity_restoration": frozenset({"project", "workspace"}),
    "render_unity_restoration": frozenset({"project", "workspace"}),
    "review_unity_restoration": frozenset({"workspace"}),
    "write_restoration_document": frozenset({"workspace"}),
    "open_restoration_document": frozenset({"ui"}),
    "export_unity_volume_texture": frozenset({"workspace"}),
    "export_unity_texture_data": frozenset({"workspace"}),
}


_PARAMETERIZED_EFFECT_TOOLS = frozenset(
    {"classify_current_frame_passes", "apply_current_frame_pass_ai_proposals",
     "finish_shader_workspace_organization", "decompile_shader_workspace"}
)


def tool_effects(name: str) -> frozenset[str]:
    """Return a conservative effect class for one discovered tool."""

    tool = str(name or "").strip()
    if not tool or tool in READ_ONLY_TOOLS or tool in AGENT_LOCAL_TOOLS:
        return frozenset()
    return TOOL_EFFECTS.get(tool, frozenset({"workspace"}))


def validate_registered_tool_effect_manifest(names: Iterable[str]) -> frozenset[str]:
    """Fail closed when a first-party tool lacks an explicit declaration."""

    registered = frozenset(str(name or "").strip() for name in names) - AGENT_LOCAL_TOOLS
    if "" in registered:
        raise PolicyError("registered first-party tool manifest contains an empty id")
    explicit_effects = frozenset(TOOL_EFFECTS)
    declared = READ_ONLY_TOOLS | EFFECTFUL_TOOLS | explicit_effects
    conflicts = READ_ONLY_TOOLS & (EFFECTFUL_TOOLS | explicit_effects)
    missing_effects = registered - READ_ONLY_TOOLS - explicit_effects
    stale_declarations = declared - registered
    missing_markers = explicit_effects - EFFECTFUL_TOOLS
    missing_mappings = EFFECTFUL_TOOLS - explicit_effects
    if conflicts:
        raise PolicyError(
            "registered first-party tools have conflicting read-only/effectful declarations: "
            + ", ".join(sorted(conflicts))
        )
    if missing_effects:
        raise PolicyError(
            "registered first-party tools lack explicit effect declarations: "
            + ", ".join(sorted(missing_effects))
        )
    if stale_declarations:
        raise PolicyError(
            "effect manifest declares tools not registered by the first-party Host: "
            + ", ".join(sorted(stale_declarations))
        )
    if missing_markers:
        raise PolicyError(
            "registered first-party tools have effect mappings but no effectful declaration: "
            + ", ".join(sorted(missing_markers))
        )
    if missing_mappings:
        raise PolicyError(
            "registered first-party tools are marked effectful without effect mappings: "
            + ", ".join(sorted(missing_mappings))
        )
    return registered


def tool_effects_are_host_known(name: str) -> bool:
    tool = str(name or "").strip()
    return bool(tool_effects(tool)) and (
        tool in EFFECTFUL_TOOLS or tool in TOOL_EFFECTS
    )


def tool_has_parameterized_effects(name: str) -> bool:
    return str(name or "").strip() in _PARAMETERIZED_EFFECT_TOOLS


def tool_effect_variants(name: str) -> tuple[frozenset[str], ...]:
    """Return every effect set that a valid call of ``name`` can request.

    Discovery/grant construction must not hide an operation merely because its
    most permissive form exceeds the connection ceiling.  Parameterized tools
    can be read-only, UI-only, project-only, or both; the proxy still checks the
    exact arguments with :func:`tool_call_effects` immediately before dispatch.
    """

    tool = str(name or "").strip()
    if tool == "finish_shader_workspace_organization":
        return tuple(frozenset({"workspace", *extras}) for extras in
                     ((), ("ui",), ("replay", "project"), ("ui", "replay", "project")))
    if tool == "decompile_shader_workspace":
        return (frozenset({"workspace"}), frozenset({"workspace", "replay"}))
    if tool in _PARAMETERIZED_EFFECT_TOOLS:
        return (
            frozenset(),
            frozenset({"ui"}),
            frozenset({"project"}),
            frozenset({"project", "ui"}),
        )
    return (tool_effects(tool),)


def tool_admissible_at_effect_ceiling(
    name: str, effects: Iterable[str]
) -> bool:
    """Whether at least one valid call fits the connection effect ceiling."""

    granted = frozenset(str(item).strip() for item in effects if str(item).strip())
    return any(variant.issubset(granted) for variant in tool_effect_variants(name))


def _effect_flag(
    arguments: dict[str, Any], snake_name: str, camel_name: str, default: bool
) -> bool:
    if snake_name in arguments:
        value = arguments[snake_name]
        supplied_name = snake_name
    elif camel_name in arguments:
        value = arguments[camel_name]
        supplied_name = camel_name
    else:
        return default
    # The downstream Pydantic call may coerce 1 or "true" into bool. Treating
    # such an input as the policy default would authorize fewer effects than the
    # Host actually performs, so reject it before either route dispatches.
    if type(value) is not bool:
        raise PolicyError(
            f"effect selector '{supplied_name}' must be a JSON boolean"
        )
    return value


def tool_call_effects(name: str, arguments: dict[str, Any]) -> frozenset[str]:
    tool = str(name or "").strip()
    if tool == "finish_shader_workspace_organization":
        effects = {"workspace"}
        apply = _effect_flag(arguments, "apply_shared_resource", "applySharedResource", False)
        # Validate every selector even if another already implies the effect;
        # accepting coerced flags can understate what the Host will execute.
        live = _effect_flag(arguments, "start_live_edit", "startLiveEdit", False)
        mode = arguments.get("publication_mode", arguments.get("publicationMode", "final"))
        if mode not in ("final", "checkpoint"):
            raise PolicyError("publication_mode must be final or checkpoint")
        selectors = dict(arguments)
        for key in ("validate_for_apply", "validateForApply"):
            if selectors.get(key, False) is None:
                selectors.pop(key)
        validate = _effect_flag(selectors, "validate_for_apply", "validateForApply", mode == "final")
        show = _effect_flag(arguments, "show_in_workspace", "showInWorkspace", False)
        if apply or validate or live:
            effects.add("replay")
            if mode == "final":
                effects.add("project")
        if show:
            effects.add("ui")
        return frozenset(effects)
    if tool == "decompile_shader_workspace":
        reference = _effect_flag(arguments, "reference_only", "referenceOnly", False)
        validate = _effect_flag(arguments, "validate_for_apply", "validateForApply", True)
        apply = _effect_flag(arguments, "apply_shared_resource", "applySharedResource", False)
        return frozenset({"workspace", "replay"} if apply or (validate and not reference)
                         else {"workspace"})
    if tool in _PARAMETERIZED_EFFECT_TOOLS:
        effects: set[str] = set()
        if _effect_flag(arguments, "publish_to_ui", "publishToUi", True):
            effects.add("ui")
        if _effect_flag(arguments, "persist_project", "persistProject", False):
            effects.add("project")
        return frozenset(effects)
    return tool_effects(tool)


__all__ = [
    "AGENT_LOCAL_TOOLS",
    "CORE_DIRECT_TOOLS",
    "CORE_TOOL_PRIORITY",
    "EFFECTFUL_TOOLS",
    "READ_ONLY_TOOLS",
    "TOOL_EFFECTS",
    "WORKFLOW_EFFECTS",
    "ordered_tool_names",
    "tool_call_effects",
    "tool_admissible_at_effect_ceiling",
    "tool_effects",
    "tool_effect_variants",
    "tool_effects_are_host_known",
    "tool_has_parameterized_effects",
    "validate_registered_tool_effect_manifest",
]
