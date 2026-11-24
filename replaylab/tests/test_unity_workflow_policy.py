"""Managed Unity tools require target-project effects, never replay permission."""

import pytest

from replaylab_agent.policy import (
    READ_ONLY_TOOLS, EFFECTFUL_TOOLS, tool_call_effects, tool_admissible_at_effect_ceiling,
    tool_effects_are_host_known,
)


@pytest.mark.parametrize("tool", ["get_draw_workflow_plan", "inspect_unity_project", "get_unity_integration_job",
                                  "compare_unity_render_images", "validate_unity_render_plan",
                                  "get_unity_shader_transfer_sources", "get_unity_restoration_status",
                                  "get_unity_restoration_step", "get_resource_export_payload",
                                  "compare_unity_effect_layers", "trace_unity_effect_dependencies", "plan_unity_asset_layout", "review_unity_view_evidence",
                                  "review_unity_temporal_stability", "review_unity_restoration_handoff", "localize_unity_effect_difference"])
def test_unity_observations_are_available_to_read_only_connections(tool):
    assert tool in READ_ONLY_TOOLS
    assert tool_call_effects(tool, {}) == frozenset()
    assert tool_admissible_at_effect_ceiling(tool, set())


@pytest.mark.parametrize("tool,effects", [
    ("start_unity_project_connection", {"project", "workspace"}),
    ("import_resource_export_to_unity", {"project", "workspace"}),
    ("cancel_unity_integration_job", {"workspace"}),
    ("call_unity_project_tool", {"project"}),
    ("connect_unity_project_mcp", {"project", "workspace"}),
    ("begin_unity_restoration", {"project", "workspace"}),
    ("plan_unity_restoration_sequence", {"workspace"}),
    ("annotate_unity_restoration_step", {"workspace"}),
    ("bind_unity_restoration", {"project", "workspace"}),
    ("check_unity_restoration", {"project", "workspace"}),
    ("render_unity_restoration", {"project", "workspace"}),
    ("review_unity_restoration", {"workspace"}),
    ("plan_unity_restoration_recovery", {"workspace"}),
    ("apply_unity_recovery_assets", {"project", "workspace"}),
    ("record_unity_reconstruction", {"workspace"}),
    ("adopt_unity_restoration_render_plan", {"workspace"}),
    ("use_unity_visual_fidelity_goal", {"workspace"}),
    ("fork_resource_export", {"workspace"}),
    ("write_restoration_document", {"workspace"}),
    ("open_restoration_document", {"ui"}),
    ("export_unity_volume_texture", {"workspace"}),
    ("export_unity_texture_data", {"workspace"}),
])
def test_unity_mutations_cannot_bypass_effect_grants_or_mutate_renderdoc_replay(tool, effects):
    assert tool in EFFECTFUL_TOOLS
    assert tool_effects_are_host_known(tool)
    assert tool_call_effects(tool, {}) == frozenset(effects)
    assert not tool_admissible_at_effect_ceiling(tool, set())
    assert tool_admissible_at_effect_ceiling(tool, effects)
    assert "replay" not in tool_call_effects(tool, {})


def test_fidelity_tools_survive_runtime_capability_negotiation():
    from replaylab_agent.mcp_proxy import _compatible_host_grant
    names=frozenset({"trace_unity_effect_dependencies","plan_unity_asset_layout",
        "compare_unity_effect_layers","write_restoration_document","open_restoration_document",
        "export_unity_volume_texture","export_unity_texture_data"})
    granted,diagnostics=_compatible_host_grant(sorted(names),names)
    assert granted==names and not diagnostics
