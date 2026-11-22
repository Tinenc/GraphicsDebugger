from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from replaylab_agent.agent_context import child_tool_grant
from replaylab_agent.agent_registry import CatalogView
from replaylab_agent import skills as skill_module
from replaylab_agent.cli import default_skill_root
from replaylab_agent.errors import ConfigurationError
from replaylab_agent.policy import READ_ONLY_TOOLS
from replaylab_agent.skills import SkillCatalog
from replaylab_agent.tool_scope import SUPPORTED_INTENT_KINDS


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / ".agents" / "skills"
MCP_SERVER = REPOSITORY_ROOT / "replaylab" / "mcp_host" / "src" / "server.py"
PACKAGING_SCRIPT = (
    REPOSITORY_ROOT / "replaylab" / "packaging" / "Build-ReplayLabRuntime.ps1"
)


def _write_test_skill(root: Path, name: str, instructions: str = "Use evidence.",
                      directory: str = "") -> Path:
    skill_dir = root / (directory or name)
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: Test procedure for {name}.\n"
        "---\n\n"
        f"{instructions}\n",
        encoding="utf-8",
    )
    return path


def _declared_mcp_tools() -> set[str]:
    tree = ast.parse(MCP_SERVER.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "tool"
                and isinstance(target.value, ast.Name)
                and target.value.id == "mcp"
            ):
                names.add(node.name)
                break
    return names


def test_strict_skill_load_still_rejects_one_invalid_sibling(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_test_skill(root, "valid-skill")
    broken = root / "broken-skill"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text(
        "---\nname: broken-skill\ndescription: [not valid yaml\n---\n"
        "UNTRUSTED-CONTENT-MUST-NOT-LEAK",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="invalid skill frontmatter"):
        SkillCatalog([root]).load()


def test_runtime_skill_load_quarantines_bad_item_and_keeps_valid_sibling(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    _write_test_skill(root, "valid-skill", "Retain the exact receipt.")
    broken = root / "broken-skill"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text(
        "---\nname: broken-skill\ndescription: [not valid yaml\n---\n"
        "UNTRUSTED-CONTENT-MUST-NOT-LEAK",
        encoding="utf-8",
    )

    catalog = SkillCatalog([root]).load_runtime()

    assert set(catalog.skills) == {"valid-skill"}
    assert catalog.get("valid-skill").instructions == "Retain the exact receipt."
    assert catalog.quarantined_skill_ids == {"broken-skill"}
    assert catalog.quarantine_count == 1
    assert [item.code for item in catalog.quarantine_diagnostics] == [
        "skill-definition-invalid"
    ]
    diagnostic_text = repr(
        [item.as_dict() for item in catalog.quarantine_diagnostics]
    )
    assert "UNTRUSTED-CONTENT-MUST-NOT-LEAK" not in diagnostic_text
    assert str(tmp_path) not in diagnostic_text
    with pytest.raises(
        ConfigurationError,
        match="packaged definition was quarantined",
    ):
        catalog.get("broken-skill")


def test_runtime_skill_load_quarantines_unreadable_item_without_exception_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "skills"
    _write_test_skill(root, "valid-skill")
    _write_test_skill(root, "unreadable-skill")
    original_read = SkillCatalog._read

    def controlled_read(path: Path):
        if path.parent.name == "unreadable-skill":
            raise OSError("SECRET-OS-DIAGNOSTIC")
        return original_read(path)

    monkeypatch.setattr(SkillCatalog, "_read", staticmethod(controlled_read))

    catalog = SkillCatalog([root]).load_runtime()

    assert set(catalog.skills) == {"valid-skill"}
    assert catalog.quarantined_skill_ids == {"unreadable-skill"}
    assert catalog.quarantine_diagnostics[0].code == "skill-file-unreadable"
    assert "SECRET-OS-DIAGNOSTIC" not in repr(catalog.quarantine_diagnostics)


def test_one_root_cannot_hold_two_copies_of_a_skill(tmp_path: Path) -> None:
    """A Skill's name must be its folder name, so a root can hold it only once.

    That is what makes "a later root wins" safe: the only way two definitions of one
    name exist is across roots, which is ordered, so nothing has to be guessed.
    """

    root = tmp_path / "skills"
    _write_test_skill(root, "duplicate-skill", "First body.", directory="alpha")

    with pytest.raises(ConfigurationError, match="must match its directory"):
        SkillCatalog([root]).load()

    catalog = SkillCatalog([root]).load_runtime()
    assert "duplicate-skill" not in catalog.skills
    assert catalog.quarantine_diagnostics[0].code == "skill-definition-invalid"


def test_a_later_root_overrides_an_earlier_one(tmp_path: Path) -> None:
    """Editing a Skill in Settings writes a copy into a later root.

    Quarantining both instead made Save delete the Skill, and doing it to
    replaylab-core stopped every task from starting.
    """

    bundled = tmp_path / "bundled"
    edited = tmp_path / "edited"
    _write_test_skill(bundled, "duplicate-skill", "Bundled body.")
    _write_test_skill(edited, "duplicate-skill", "The user's edited body.")

    catalog = SkillCatalog([bundled, edited]).load_runtime()

    assert catalog.skills["duplicate-skill"].instructions == "The user's edited body."
    assert catalog.quarantined_skill_ids == frozenset()
    assert catalog.quarantine_diagnostics == ()
    # The strict loader agrees, so packaging and doctor see the same catalog.
    assert SkillCatalog([bundled, edited]).load().skills["duplicate-skill"].instructions == (
        "The user's edited body.")


def test_runtime_skill_quarantine_diagnostics_are_bounded(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    for index in range(70):
        skill_dir = root / f"bad-skill-{index}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("invalid", encoding="utf-8")

    catalog = SkillCatalog([root]).load_runtime()

    assert catalog.quarantine_count == 70
    assert len(catalog.quarantine_diagnostics) == 64
    assert not catalog.skills

    summary = catalog.quarantine_summary()
    assert summary.as_dict()["schema"] == "replaylab-skill-quarantine-summary/v1"
    assert summary.as_dict()["quarantineCount"] == 70
    assert summary.as_dict()["truncated"] is True
    assert len(summary.as_dict()["diagnostics"]) == 64
    assert str(tmp_path) not in repr(summary.as_dict())


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (
            "  replaylab-category: Shader Workbench\n",
            "invalid replaylab-category",
        ),
        (
            "  replaylab-registry-version: zero\n",
            "invalid replaylab-registry-version",
        ),
        (
            "  replaylab-registry-virtual: true\n",
            "must not define replaylab-registry-virtual",
        ),
    ],
)
def test_strict_and_runtime_load_share_metadata_semantic_validation(
    tmp_path: Path,
    metadata: str,
    message: str,
) -> None:
    root = tmp_path / "skills"
    _write_test_skill(root, "valid-skill")
    broken = root / "semantic-skill"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text(
        "---\n"
        "name: semantic-skill\n"
        "description: Metadata validation test.\n"
        "metadata:\n"
        f"{metadata}"
        "---\n\n"
        "Use exact evidence.\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=message):
        SkillCatalog([root]).load()

    catalog = SkillCatalog([root]).load_runtime()
    assert set(catalog.skills) == {"valid-skill"}
    assert catalog.quarantined_skill_ids == {"semantic-skill"}
    assert catalog.quarantine_summary().as_dict()["diagnostics"] == [
        {
            "code": "skill-definition-invalid",
            "rootIndex": 0,
            "entryIndex": 0,
            "skillId": "semantic-skill",
        }
    ]


def test_skill_file_and_parsed_body_limits_are_strict_or_quarantined(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    _write_test_skill(root, "valid-skill")
    oversized_body = "x" * (128 * 1024 + 1)
    _write_test_skill(root, "oversized-body", oversized_body)
    oversized_file = root / "oversized-file"
    oversized_file.mkdir(parents=True)
    (oversized_file / "SKILL.md").write_bytes(b"x" * (256 * 1024 + 1))

    with pytest.raises(ConfigurationError, match="128 Ki characters"):
        SkillCatalog([root]).load()

    catalog = SkillCatalog([root]).load_runtime()
    assert set(catalog.skills) == {"valid-skill"}
    assert catalog.quarantined_skill_ids == {"oversized-body", "oversized-file"}
    assert catalog.quarantine_count == 2
    assert {
        item.code for item in catalog.quarantine_diagnostics
    } == {"skill-definition-invalid"}


def test_strict_skill_load_rejects_oversized_main_file(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    oversized = root / "oversized-file"
    oversized.mkdir(parents=True)
    (oversized / "SKILL.md").write_bytes(b"x" * (256 * 1024 + 1))

    with pytest.raises(ConfigurationError, match="skill file exceeds 256 KiB"):
        SkillCatalog([root]).load()


def _host_projected_catalog() -> SkillCatalog:
    """Load pure procedure bundles; live tools are queried separately."""

    return SkillCatalog([SKILL_ROOT]).load()


def test_repository_skills_are_pure_procedure_and_evidence_contracts() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()

    assert {
        "renderdoc-explain-event",
        "renderdoc-resource-export",
        "renderdoc-shader-workbench",
        "renderdoc-unpacked-asset-intelligence",
    } <= set(catalog.skills)
    for skill in catalog.skills.values():
        assert skill.path.is_absolute()
        assert skill.instructions
        assert not hasattr(skill, "allowed_tools")
        assert not hasattr(skill, "read_only")
        assert "allowed-tools" not in skill.metadata
        assert "replaylab-read-only" not in skill.metadata
        assert "replaylab-min-successful-tools" not in skill.metadata
    catalog = _host_projected_catalog()
    shader = catalog.skills["renderdoc-shader-workbench"]
    event = catalog.skills["renderdoc-explain-event"]
    exporter = catalog.skills["renderdoc-resource-export"]
    unpacked = catalog.skills["renderdoc-unpacked-asset-intelligence"]
    for skill in (shader, event, exporter, unpacked):
        assert not hasattr(skill, "minimum_successful_tools")
        assert not hasattr(skill, "required_all_tools")
        assert not hasattr(skill, "required_any_tools")
    live_tools = _declared_mcp_tools()
    assert {
        "inspect_shader",
        "compile_shader_workspace",
        "try_shader_workspace_recovery_candidate",
        "validate_shader_workspace_candidate",
        "apply_shader_workspace_shared_resource",
        "rollback_shader_workspace_shared_resource",
        "restore_current_shader",
        "register_active_project_workspace",
        "get_current_shader_representation",
        "prepare_material_session",
        "update_material_parameter",
        "rollback_material_session",
        "clear_all_material_sessions",
        "apply_symbol_alias_table",
        "rollback_symbol_alias_table",
        "get_symbol_match_cases",
        "get_symbol_match_evidence",
        "list_symbol_matches",
        "submit_symbol_match",
    } <= live_tools
    assert "export_symbol_alias_table" in live_tools
    assert {
        "inspect_event",
        "analyze_current_frame_flow",
        "analyze_pixel",
        "capture_event_texture_evidence",
        "render_current_frame_flow_diagram",
        "prepare_frame_render_flow",
        "inspect_frame_render_stage",
        "inspect_frame_resource_usage",
        "get_frame_render_flow",
        "publish_frame_render_flow",
        "classify_current_frame_passes",
        "apply_current_frame_pass_ai_proposals",
    } <= live_tools
    assert {
        "get_resource_export_capabilities",
        "inspect_resource_export",
        "prepare_resource_export",
        "finalize_resource_export",
        "get_resource_export_status",
        "run_resource_export_target_validation",
        "validate_resource_export",
    } <= live_tools
    assert {
        "export_drawcall",
        "export_to_unity",
        "save_texture",
    }.isdisjoint(live_tools)
    assert {
        "get_unpacked_asset_index_capabilities",
        "get_active_unpacked_asset_index_status",
        "index_active_unpacked_assets",
        "query_active_unpacked_assets",
        "resolve_active_unpacked_asset",
        "get_unpacked_asset_provider_capabilities",
        "get_unpacked_asset_provider_decisions",
        "query_unpacked_asset_inventory_assets",
        "start_unpacked_asset_report",
        "start_unpacked_asset_blender_export",
        "continue_unpacked_asset_blender_export",
        "get_unpacked_asset_job_status",
        "prepare_active_capture_project",
        "query_active_shader_asset_evidence",
        "inspect_resource_export",
    } <= live_tools
    assert {
        "export_drawcall",
        "export_to_unity",
        "save_texture",
    }.isdisjoint(live_tools)
    # The maintained operation references stay discoverable without requiring
    # their detailed wording to be duplicated in the entrypoint.
    for reference in ("asset-naming.md", "reconstruction.md", "source-operations.md"):
        assert "references/" + reference in shader.instructions
        assert (shader.path.parent / "references" / reference).is_file()
    for skill in (event, exporter, unpacked):
        assert "replaylab-required-references" not in skill.metadata
        # Delivery bounds are exercised below, including lossless section pages.
        assert "mandatory reference" not in skill.instructions.lower()
    assert "aiprovisional" in event.instructions.lower()
    assert "observed-contributor" in event.instructions.lower()
    assert "max_actions=8000" not in event.instructions.lower()
    assert "40-node/64-edge" not in event.instructions.lower()
    assert "raw device depth" in " ".join(event.instructions.lower().split())
    assert "publish_to_ui=false" in event.instructions.lower()
    assert "panel is retired" in event.instructions.lower()
    assert "gltf" in exporter.instructions.lower()
    assert "inverse-transpose" in exporter.instructions.lower()
    assert "strong inventory" in unpacked.instructions.lower()
    assert "stable observations" in unpacked.instructions.lower()
    assert "heuristic" in unpacked.instructions.lower()
    assert "exact cooked virtual skeletalmesh paths" in " ".join(unpacked.instructions.lower().split())
    assert "analyze_current_frame_flow" in READ_ONLY_TOOLS
    assert {"prepare_frame_render_flow", "get_frame_render_flow",
            "inspect_frame_render_stage", "inspect_frame_resource_usage"} <= READ_ONLY_TOOLS
    assert "publish_frame_render_flow" not in READ_ONLY_TOOLS
    assert "inspect_resource_export" in READ_ONLY_TOOLS


def test_event_visual_reference_uses_live_host_contracts_and_receipt_reuse() -> None:
    from src.event_flow_diagram import MAX_EDGES, MAX_NODES

    catalog = SkillCatalog([SKILL_ROOT]).load()
    skill = catalog.get("renderdoc-explain-event")
    event = " ".join(skill.instructions.lower().split())
    detail = " ".join((skill.path.parent / "references" / "visual-evidence.md")
                      .read_text(encoding="utf-8").lower().split())
    for retired in (
        "event_route.artifactobligations", "logical continuation prefix",
        "canonical request sha-256", "24 unique texture", "three unique rendered",
        "it rejects an exact duplicate", "`flat-range`", "authorized `workspace` phase",
    ):
        assert retired not in event, retired
    assert "range_mode=subresource-minmax" not in event
    for contract in (
        "capture_event_texture_evidence", "range_mode=subresource-minmax",
        "attachment_kind=dsv", "attachment_index=0", "subresource-range-flat",
        "reused=true", "event_id=0", "complete raw resource bytes",
        "equal display pngs only establish an unchanged display",
    ):
        assert contract in detail, contract
    assert "max_actions=8000" in detail
    assert f"{MAX_NODES}-node/{MAX_EDGES}-edge" in detail


def test_frame_algorithm_guidance_is_reachable_and_closes_separate_effects() -> None:
    """Do not regress to an inventory whose light/effect output has no use."""

    skill = SkillCatalog([SKILL_ROOT]).load().get("renderdoc-explain-event")
    reference_name = "algorithm-investigation.md"
    references = skill.path.parent / "references"
    assert "references/" + reference_name in skill.instructions
    for document in ("frame-rendering-flow.md", "visual-evidence.md"):
        assert f"]({reference_name})" in (references / document).read_text(encoding="utf-8")
    detail = " ".join((references / reference_name).read_text(encoding="utf-8").lower().split())
    for contract in (
        "investigate backward, explain forward",
        "input -> calculation -> output -> integration",
        "**inputs:**", "**calculation:**", "**output:**", "**integration:**",
        "the consuming eid", "current draw may already be the integration step",
        "no producer in this captured frame", "do not invent a second composition pass",
        "record these answers and their exact evidence as the module is investigated",
        "review is a consistency check, not the first attempt to understand the code",
    ):
        assert contract in detail, contract


def test_frame_algorithm_guidance_requires_actual_shader_evidence_and_bounded_followup() -> None:
    skill = SkillCatalog([SKILL_ROOT]).load().get("renderdoc-explain-event")
    detail = " ".join((skill.path.parent / "references" / "algorithm-investigation.md")
                      .read_text(encoding="utf-8").lower().split())
    for contract in (
        "resource usage is not channel-level or causal proof",
        "source location or instruction range",
        "the nearest prior writer is not automatically the complete source",
        "do not assume lumen, vsm, ray tracing, reprojection or denoising exists",
        "cache resource usage by capture/session and resourceid",
        "visited resource/eid/subresource/question",
        "expand only that unresolved boundary",
        "retry identical unavailable evidence indefinitely",
        "important input sources, actual calculations and integration destinations",
        "inspect_frame_resource_usage", "candidate", "hasmore/nextoffset",
        "navigation anchors and analysis anchors are separate",
        "stage.algorithm", "producereventid", "sourceeventid", "targetresourceid",
    ):
        assert contract in detail, contract


def test_frame_visual_guidance_requires_viewed_images_and_semantic_verification() -> None:
    """A saved PNG receipt must not satisfy the Agent's visual investigation."""

    skill = SkillCatalog([SKILL_ROOT]).load().get("renderdoc-explain-event")
    references = skill.path.parent / "references"
    entry = " ".join(skill.instructions.lower().split())
    visual = " ".join((references / "visual-evidence.md").read_text(encoding="utf-8").lower().split())
    algorithm = " ".join((references / "algorithm-investigation.md")
                         .read_text(encoding="utf-8").lower().split())
    assert int(skill.metadata["replaylab-version"]) >= 29
    assert "actually view the important rt image content" in entry
    for contract in (
        "json receipt, image dimensions, histogram or hash is not a viewed image",
        "inline=false deliberately requests only the receipt",
        "current connection really delivered an image",
        "modelimage.included", "not that the model received or inspected it",
        "modelimage.sourcesha256", "modelimage.sha256",
        "read_evidence_file(path, inline=true)", "artifact.output.path",
        "printing base64 or reading the png as text does not show the picture",
        "keep visual confirmation incomplete",
        "do not repeatedly request the same unsupported media",
        "these are clues, not final algorithm labels",
        "shader sample/decode/arithmetic/output and producer/consumer resources",
        "do not invent a visual observation merely to fill that field",
        "view the constituent images with the same mapping",
    ):
        assert contract in visual, contract
    assert "visual-evidence.md" in algorithm
    assert "during" in algorithm and "investigation" in algorithm


def test_frame_layout_guidance_keeps_key_results_clear_without_losing_coverage() -> None:
    """Folding evidence and animating outputs must preserve their actual roles."""

    skill = SkillCatalog([SKILL_ROOT]).load().get("renderdoc-explain-event")
    detail = " ".join((skill.path.parent / "references" / "frame-rendering-flow.md")
                      .read_text(encoding="utf-8").lower().split())
    for contract in (
        "vertical spine of chronological functional modules",
        "related key images arranged horizontally",
        "borrow the layout and clarity, not another game's algorithms or pass order",
        "not a fixed quota", "important mrt properties",
        "secondary views and auxiliary evidence available in foldouts",
        "image.presentation=primary", "image.presentation=detail",
        "omitted presentation defaults to primary",
        "do not relabel auxiliary pictures as channels",
        "take precedence over presentation",
        "do not duplicate those images as standalone thumbnails",
        "keep core outputs in group outputs even when referenced by an animation or comparison",
        "group=animation-frames only for extra transitional views",
        "not to remove a required output from coverage",
    ):
        assert contract in detail, contract


def test_frame_guidance_separates_internal_evidence_from_summary_and_read_only_analysis() -> None:
    skill = SkillCatalog([SKILL_ROOT]).load().get("renderdoc-explain-event")
    references = skill.path.parent / "references"
    entry = " ".join(skill.instructions.lower().split())
    frame = " ".join((references / "frame-rendering-flow.md").read_text(encoding="utf-8").lower().split())
    algorithm = " ".join((references / "algorithm-investigation.md")
                         .read_text(encoding="utf-8").lower().split())
    for contract in (
        "short plain-language summary", "without inputs/calculation/outputs",
        "horizontal processgraph", "not ui detail panels",
        "do not switch to shader organization to satisfy a gate",
    ):
        assert contract in entry, contract
    for contract in (
        "not an input/calculation/output form or a calculation-details panel",
        "stage.algorithm as internal verification data",
        "readable hlsl compilation is not a prerequisite",
        "actual transformations and downstream result",
        "not required effects, stage counts, eids or algorithms",
        "actual operation and resource paths", "acyclic directed graph",
    ):
        assert contract in frame, contract
    for contract in (
        "they are not node labels or a ui form",
        "no source reorganization, editable shader workspace, compilation or replay replacement",
        "failed explanation read is not a reason to start the shader workbench workflow",
        "undecoded indirect arguments leave workload unknown",
        "not proof of zero work or of pixel contribution",
        "do not invent dispatch dimensions, delete a possible writer",
        "unavailable count alone does not make a supported algorithm explanation false",
    ):
        assert contract in algorithm, contract


def test_frame_reanalysis_and_semantic_channel_publication_guidance() -> None:
    skill = SkillCatalog([SKILL_ROOT]).load().get("renderdoc-explain-event")
    entry = " ".join(skill.instructions.lower().split())
    frame = " ".join((skill.path.parent / "references" / "frame-rendering-flow.md")
                      .read_text(encoding="utf-8").lower().split())
    for contract in (
        "prepare_frame_render_flow(reanalyze=true)", "re-publishing the baseline is not reanalysis",
        'section="proposals"', 'section="quality"', "publication pre-generates and persists them",
    ):
        assert contract in entry, contract
    for contract in (
        "an outline refresh alone does not reanalyze algorithms",
        "do not submit expanded readback objects", "recoverable reads should switch",
        "channelgroups", "one a record alone is not a complete rgba set",
        "a truly single-channel texture needs no channel button",
        "retry only the missing work", "nodes (2–16)", "edges (1–32)",
        "each displayed node a representative texture", "a required final slider/gif",
    ):
        assert contract in frame, contract


def test_frame_guidance_selects_current_resource_flow_without_capture_specific_eids() -> None:
    """Historical captures must not become prescribed investigation targets."""
    import re

    skill = SkillCatalog([SKILL_ROOT]).load().get("renderdoc-explain-event")
    references = skill.path.parent / "references"
    documents = [skill.instructions] + [path.read_text(encoding="utf-8") for path in references.glob("*.md")]
    # event_id=0 is the documented frame-wide API sentinel. Positive EIDs must
    # come from live capture evidence, rather than executable examples/rules.
    for text in documents:
        assert not re.findall(r"(?:\bEID\b|\bevent_id\b|\beventId\b)\s*(?:[:=]\s*)?[\"']?([1-9]\d*)\b", text, re.I)
    entry = " ".join(skill.instructions.lower().split())
    frame = " ".join((references / "frame-rendering-flow.md").read_text(encoding="utf-8").lower().split())
    assert "this capture's execution order and resource flow" in entry
    assert "historical eids and example techniques are not required targets or a module checklist" in entry
    assert "cross-rt handoffs, branches and merges" in entry
    assert "mrt attachments produced together are parallel results" in frame
    assert "repeated equivalent draws need not become separate presentation nodes" in frame


def test_frame_presenter_guidance_uses_short_channel_names_and_useful_image_graphs() -> None:
    skill = SkillCatalog([SKILL_ROOT]).load().get("renderdoc-explain-event")
    frame = " ".join((skill.path.parent / "references" / "frame-rendering-flow.md").read_text(encoding="utf-8").lower().split())
    assert "keep label a short property name" in frame
    assert "explanation in description, shown below in smaller text" in frame
    assert "do not create a pure-text graph" in frame
    assert "each displayed node a representative texture image, comparison or animation" in frame
    assert "prefer this draggable divider when a composition changes the image only subtly" in frame
    assert "multiple draws alone do not require a subgraph" in frame
    assert "auxiliary clear/copy stage" in frame and "without four algorithm tables" in frame


def test_frame_resource_tracing_uses_versions_and_verified_two_way_dependencies() -> None:
    """Usage candidates and repeated images must not be promoted to causal edges."""
    skill = SkillCatalog([SKILL_ROOT]).load().get("renderdoc-explain-event")
    references = skill.path.parent / "references"
    algorithm = " ".join((references / "algorithm-investigation.md")
                         .read_text(encoding="utf-8").lower().split())
    frame = " ".join((references / "frame-rendering-flow.md")
                     .read_text(encoding="utf-8").lower().split())
    visual = " ".join((references / "visual-evidence.md")
                      .read_text(encoding="utf-8").lower().split())
    for contract in (
        "start from a key output version",
        "mip, layer/slice, sample and relevant channel or region",
        'direction="before", order="nearest"',
        "paging backward in execution order, not by numeric eid distance",
        "nearest candidate is not necessarily the correct producer",
        'order="nearest" is not supported with direction="all"',
        "binding is not sampling",
        "branch conditions and dynamic indexing",
        "effective writes before this consumer",
        "coverage replaces or retains the needed contents",
        "before its relevant contents are overwritten",
        "a downstream blend may consume destination contents without an srv read",
        "no producer in this captured frame",
        "established writer interval or history boundary",
        "reuse shared dependencies once across modules",
        "invalidate affected conclusions when capture, replay mutations",
        "expand only that unresolved boundary",
    ):
        assert contract in algorithm, contract
    for contract in (
        "main stage sequence shows execution order",
        "processgraph shows actual data dependencies across draws/targets",
        "mrt attachments produced together are parallel results",
        "single-draw arithmetic and one snapshot repeated under several labels are not a chain",
        "ordinary stages show independent useful results",
        "actual shader-read, copy or blend evidence, not just a shared binding",
    ):
        assert contract in frame, contract
    assert "not single-draw arithmetic or one snapshot relabelled as successive results" in visual


def test_frame_reanalysis_keeps_verified_evidence_and_avoids_empty_pipeline_retry_loops() -> None:
    """A failed current read must neither erase prior evidence nor certify it anew."""
    skill = SkillCatalog([SKILL_ROOT]).load().get("renderdoc-explain-event")
    references = skill.path.parent / "references"
    entry = " ".join(skill.instructions.lower().split())
    algorithm = " ".join((references / "algorithm-investigation.md")
                         .read_text(encoding="utf-8").lower().split())
    visual = " ".join((references / "visual-evidence.md")
                      .read_text(encoding="utf-8").lower().split())
    for contract in (
        "reuse verified static bindings, source and pngs first",
        "static evidence cannot answer everything",
        "an empty pipeline read is not new evidence",
        "never call reused evidence a new validation",
    ):
        assert contract in entry, contract
    for contract in (
        "check their capture, shader identity, resource version and relevant mutation scope",
        "static evidence cannot establish every current value, executed branch, pixel contribution or post-edit result",
        "do not overwrite them with empty lists",
        "preserve evidence-backed cross-eid relationships when fresh inspection fails",
        "remove a relationship only when evidence shows it is false or obsolete",
        "not to pass a gate or hide a failed reanalysis",
        "keep any required fresh verification explicitly unfinished",
        "recover autonomously within the task",
        "do not repeat them or fan out more reads and channel captures against that unchanged state",
        "a retry needs a changed condition or a distinct evidence route",
        "continue independent static work",
        "get_replay_health", "not as a routine prerequisite",
        "only confirmed native fatal failure permits `recover_replay`",
        "new session with capture-original content",
        "not prior shader replacements or cbuffer edits",
        "old bindings/media are historical, not current validation",
        "rather than claiming the recovered picture is identical",
    ):
        assert contract in algorithm, contract
    assert "prefer an applicable saved png to a new capture" in visual
    assert "a stale image cannot prove their current appearance" in visual
    assert "retain valid existing media and mark only missing views pending" in visual


def test_frame_guidance_rechecks_meaning_and_selects_complementary_evidence() -> None:
    """Reference layouts and successful old cards cannot substitute for evidence."""
    skill = SkillCatalog([SKILL_ROOT]).load().get("renderdoc-explain-event")
    references = skill.path.parent / "references"
    entry = " ".join(skill.instructions.lower().split())
    algorithm = " ".join((references / "algorithm-investigation.md")
                         .read_text(encoding="utf-8").lower().split())
    frame = " ".join((references / "frame-rendering-flow.md")
                     .read_text(encoding="utf-8").lower().split())
    visual = " ".join((references / "visual-evidence.md")
                      .read_text(encoding="utf-8").lower().split())
    assert "recheck important conclusions and dependencies, not just flagged gaps or layout" in entry
    assert "asset names are clues, not proof" in entry
    for contract in (
        "mesh shape and input layout", "vs deformation or projection",
        "culling, depth/stencil tests, blending/write masks",
        "shader mathematics and versioned resource handoffs",
        "a compute filter does not require an irrelevant mesh inspection",
        "compare pre/post-vs geometry when deformation or coverage matters",
        "captured calculations and actual uses decide this permutation's role",
    ):
        assert contract in algorithm, contract
    for contract in (
        "reusing a verified static source with its provenance is valid",
        "merely changing layout, deleting graphs or republishing old conclusions is not reanalysis",
        "investigate deeply and present simply",
        "not required effects, stage counts, eids or algorithms",
        "not a fixed quota or required three-image template",
    ):
        assert contract in frame, contract
    assert "do not identify a function from its image or an unpacked asset name alone" in visual
    assert "selection criteria, not prescribed effect names, stage counts or an image quota" in visual


def test_module_discovery_requires_actual_geometry_and_adaptive_semantic_boundaries() -> None:
    skill = SkillCatalog([SKILL_ROOT]).load().get("renderdoc-explain-event")
    assert "references/module-discovery.md" in skill.instructions
    detail = " ".join((skill.path.parent / "references" / "module-discovery.md")
                      .read_text(encoding="utf-8").lower().split())
    for contract in (
        "including previously accepted ranges, not only the last publication's gap list",
        "select representative draws at meaningful changes",
        "do not inspect every draw with every tool",
        "both eventually write scene color is not a reason to merge their meanings",
        "force the user's example effects to appear",
        "capture_mesh_image(event_id=..., inline=true)",
        "mesh counts, strides and an input-layout list are not a viewed mesh shape",
        "one instance, one view or a truncated prefix",
        "a configured depth test is not its observed result",
        "two eids from one capture are before/after states of that frame",
        "material instance parents, parameter overrides and shader/function paths",
        "matching every texture is not required",
        "material parameter names can clarify a role even when individual cbuffer offsets are not matched",
        "do not start shader organization, edit source, rename resources/cbuffer fields",
        "an optional diagnostic not needed by the explanation is not a new requirement",
        "do not erase a real relationship",
    ):
        assert contract in detail, contract


def test_domain_skills_leave_agent_topology_and_tool_order_to_runtime() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    event = " ".join(catalog.get("renderdoc-explain-event").instructions.lower().split())

    assert "agent chooses the scope" in event
    assert "no required phase prefix" in event
    assert "generic child agent" not in event
    assert "mandatory" not in event
    assert "useful partial result" in event
    for name in ("renderdoc-resource-export", "renderdoc-unpacked-asset-intelligence"):
        instructions = " ".join(catalog.get(name).instructions.lower().split())
        assert "the agent chooses" in instructions or "the agent decides" in instructions
        assert "required phase" not in instructions

    # Domain guidance cannot become a second authority for tool grants,
    # mandatory calls or Agent topology through catalog metadata.
    for name, category in (("renderdoc-shader-workbench", "shader"),
                           ("replaylab-core", "core")):
        skill = catalog.get(name)
        assert skill.category == category
        assert not {"allowed-tools", "replaylab-read-only",
                    "replaylab-min-successful-tools", "replaylab-required-references",
                    "replaylab-required-all-tools", "replaylab-required-any-tools"} & skill.metadata.keys()


@pytest.mark.parametrize("skill_id", [
    "renderdoc-shader-workbench", "replaylab-core", "renderdoc-explain-event",
])
def test_bundled_shader_and_core_deliver_complete_guidance_and_references(skill_id: str) -> None:
    from replaylab_agent.skill_gateway import MAX_SKILL_INLINE_BYTES, SkillGateway, encode

    files = SkillCatalog([SKILL_ROOT]).load()
    catalog = CatalogView.load(skill_roots=[SKILL_ROOT])
    skill = catalog.skill(skill_id)
    assert skill.version == str(files.get(skill_id).metadata["replaylab-version"])
    gateway = SkillGateway(catalog)
    delivered = gateway.load(skill_id)
    assert "error" not in delivered
    assert not delivered.get("truncated")
    if delivered.get("sectioned"):
        from replaylab_agent.skill_gateway import split_sections
        _, sections = split_sections(skill.instructions.strip())
        assert [item["section"] for item in delivered["sections"]] == [heading for heading, _ in sections]
        for heading, body in sections:
            offset, chunks = 0, []
            while True:
                page = gateway.load(skill_id, section=heading, offset=offset)
                assert "error" not in page
                assert len(encode(page).encode("utf-8")) <= MAX_SKILL_INLINE_BYTES
                chunks.append(page["instructions"])
                if not page.get("hasMore"):
                    break
                assert page["nextOffset"] > offset
                offset = page["nextOffset"]
            assert "".join(chunks) == body
    else:
        assert delivered["instructions"] == skill.instructions.strip()
    assert len(encode(delivered).encode("utf-8")) <= MAX_SKILL_INLINE_BYTES

    expected = sorted(path.relative_to(skill.path.parent).as_posix()
                      for path in (skill.path.parent / "references").glob("*.md"))
    assert delivered.get("references", []) == expected
    for call in delivered.get("referenceLoads", []):
        assert call["tool"] == "load_bread_skill"
        path = skill.path.parent / call["arguments"]["section"]
        assert path.resolve().is_relative_to(skill.path.parent.resolve())
        offset, chunks = 0, []
        while True:
            arguments = dict(call["arguments"], offset=offset)
            reference = gateway.load(**arguments)
            assert "error" not in reference
            assert reference["reference"] is True
            assert len(encode(reference).encode("utf-8")) <= MAX_SKILL_INLINE_BYTES
            chunks.append(reference["instructions"])
            if not reference["hasMore"]:
                break
            assert reference["nextOffset"] > offset
            offset = reference["nextOffset"]
        # Skill receipts identify the exact delivered UTF-8 bytes, including
        # Windows newlines; universal-newline reads silently change that body.
        assert "".join(chunks) == path.read_bytes().decode("utf-8")


def test_runtime_packaging_embeds_every_skill_folder_not_a_hand_kept_list() -> None:
    """The frozen runtime used to ship 5 of the 10 repository Skills.

    A list of folder names in the build script was the reason: every Skill added
    later passed the tests here and never reached replaylab-ai.exe. The script now
    enumerates the folders that hold a SKILL.md, and the smoke test counts them
    with an empty user tools config so the build machine's own Skills do not skew it.
    """

    script = PACKAGING_SCRIPT.read_text(encoding="utf-8")

    assert '$skillRoot = Join-Path $repositoryRoot ".agents\\skills"' in script
    assert 'Get-ChildItem -LiteralPath $skillRoot -Directory' in script
    assert '":.agents/skills/" + $_.Name' in script
    assert '$_.Name -eq "replaylab-core"' in script
    assert "--tools-config $smokeToolsPath" in script
    # No Skill folder is named by hand any more.
    for name in ("renderdoc-explain-event", "renderdoc-shader-workbench",
                 "renderdoc-resource-export", "renderdoc-unpacked-asset-intelligence"):
        assert f'(Join-Path $skillRoot "{name}' not in script


def test_required_skill_references_are_contained_and_host_loaded(tmp_path) -> None:
    skill_root = tmp_path / "skills"
    skill_dir = skill_root / "bounded-skill"
    references = skill_dir / "references"
    references.mkdir(parents=True)
    (references / "contract.md").write_text(
        "# Required contract\n\nEvidence must be exact.", encoding="utf-8"
    )
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: bounded-skill\n"
        "description: Test required reference loading.\n"
        "metadata:\n"
        "  replaylab-required-references: references/contract.md\n"
        "---\n\n"
        "# Main contract\n",
        encoding="utf-8",
    )

    skill = SkillCatalog([skill_root]).load().get("bounded-skill")

    assert "# Main contract" in skill.instructions
    assert "## Mandatory reference: references/contract.md" in skill.instructions
    assert "Evidence must be exact." in skill.instructions


def test_required_skill_reference_rejects_parent_escape(tmp_path) -> None:
    skill_root = tmp_path / "skills"
    skill_dir = skill_root / "bounded-skill"
    skill_dir.mkdir(parents=True)
    (skill_root / "outside.md").write_text("outside", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: bounded-skill\n"
        "description: Test required reference containment.\n"
        "metadata:\n"
        "  replaylab-required-references: ../outside.md\n"
        "---\n\n"
        "# Main contract\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="invalid required reference"):
        SkillCatalog([skill_root]).load()


def test_frozen_runtime_loads_skills_from_pyinstaller_bundle(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert default_skill_root() == tmp_path / ".agents" / "skills"


def test_native_ui_intents_do_not_select_or_require_a_skill() -> None:
    assert SUPPORTED_INTENT_KINDS == (
        "explain-current-event",
        "inspect-picked-pixel",
        "reverse-edit-selected-shader",
    )
    assert not hasattr(skill_module, "INTENT_SKILLS")


def test_workflow_skills_do_not_declare_rigid_tool_requirements() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    for skill in catalog.skills.values():
        assert not hasattr(skill, "minimum_successful_tools")
        assert not hasattr(skill, "required_all_tools")
        assert not hasattr(skill, "required_any_tools")


def test_skill_without_category_or_english_instructions_loads(tmp_path: Path) -> None:
    _write_test_skill(tmp_path, "capture-check", "先核对捕获记录，再说明结论依据。")
    skill = SkillCatalog([tmp_path]).load().get("capture-check")
    assert skill.category == "other"
    assert "再说明结论依据" in skill.instructions


def test_retired_skill_tool_metadata_is_non_authoritative_compatibility_text(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "legacy-hints"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: legacy-hints\n"
        "description: Legacy execution metadata must be ignored.\n"
        "metadata:\n"
        "  replaylab-min-successful-tools: nope\n"
        "  replaylab-required-all-tools: inspect_shader BadTool\n"
        "  replaylab-required-any-tools: inspect_event inspect_event\n"
        "---\n\n"
        "# Adaptive guidance\n\nUse evidence and completion outcomes.\n",
        encoding="utf-8",
    )

    skill = SkillCatalog([root]).load().get("legacy-hints")

    assert skill.instructions
    assert not hasattr(skill, "minimum_successful_tools")
    assert not hasattr(skill, "required_all_tools")
    assert not hasattr(skill, "required_any_tools")


def test_removed_legacy_skills_are_not_discoverable() -> None:
    catalog = SkillCatalog([SKILL_ROOT]).load()
    for name in (
        "renderdoc-export-interoperability",
        "renderdoc-inspect-pixel",
        "renderdoc-material-cbuffer",
        "renderdoc-performance",
    ):
        with pytest.raises(ConfigurationError, match="unknown Bread skill"):
            catalog.get(name)


def test_connection_effects_not_skill_text_control_child_tool_grants() -> None:
    read_only = child_tool_grant(())
    workspace = child_tool_grant(("workspace",))
    replay = child_tool_grant(("replay",))

    parameterized = {
        "classify_current_frame_passes",
        "apply_current_frame_pass_ai_proposals",
    }
    assert {"inspect_event", "inspect_shader"} <= read_only
    assert read_only <= READ_ONLY_TOOLS | parameterized
    assert "compile_shader_workspace" not in read_only
    assert "classify_current_frame_passes" in read_only
    assert "apply_current_frame_pass_ai_proposals" in read_only
    assert "classify_current_frame_passes" in child_tool_grant(("ui",))
    assert "compile_shader_workspace" in workspace
    assert "apply_shader_workspace_candidate" not in workspace
    assert "apply_shader_workspace_candidate" in replay
    assert "update_material_parameter" in replay
    assert "resume_shader_workspace_workflow" not in read_only
    assert "decide_shader_organization_review_budget" not in replay


def test_live_host_catalog_never_depends_on_skill_advertised_tools() -> None:
    catalog = _host_projected_catalog()
    external = _declared_mcp_tools()

    for skill in catalog.skills.values():
        assert not hasattr(skill, "allowed_tools")
    assert {"inspect_event", "inspect_shader"} <= external
    assert not {
        "report_task_result",
        "resume_shader_workspace_workflow",
        "decide_shader_organization_review_budget",
        "review_shader_workspace_organization",
        "get_shader_workspace_organization_review_status",
        "verify_shader_organization_segment",
    } & external
