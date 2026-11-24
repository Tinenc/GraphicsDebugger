"""API and ACP share Skill selection and Agent-owned outcome acceptance."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from replaylab_agent.agent_prompts import (
    deep_child_system_prompt,
    deep_main_system_prompt,
    native_agent_system_prompt,
)
from replaylab_agent.agent_registry import CatalogView


SKILL_ROOT = Path(__file__).resolve().parents[2] / ".agents" / "skills"
ROLES = ("api-main", "api-child", "api-general", "claude-acp", "codex", "other-acp")


def _prompt(catalog: CatalogView, role: str) -> str:
    skill = catalog.skill("renderdoc-shader-workbench")
    if role == "api-main":
        return deep_main_system_prompt(
            catalog=catalog, request="Explain this shader", context=None,
            response_language="zh_CN", subagents=[("general-purpose", "Independent subtask")],
        )
    if role.startswith("api-"):
        return deep_child_system_prompt(
            context=None, effects=(),
        )
    return native_agent_system_prompt(
        catalog=catalog, request="Explain this shader", context=None,
        response_language="zh_CN", tool_names=("inspect_shader",),
        protocol=role, effects=(), loaded_skill=skill,
    )


@pytest.mark.parametrize("role", ROLES)
def test_every_role_leaves_installed_core_optional(role: str) -> None:
    catalog = CatalogView.load(skill_roots=[SKILL_ROOT])
    core = catalog.skill("replaylab-core")
    prompt = _prompt(catalog, role)
    assert core.instructions.strip() not in prompt
    if role == "api-main" or not role.startswith("api-"):
        assert "- replaylab-core:" in prompt


def test_core_keeps_only_shared_boundaries() -> None:
    from replaylab_agent.skill_gateway import SkillGateway

    catalog = CatalogView.load(skill_roots=[SKILL_ROOT])
    loaded = SkillGateway(catalog).load("replaylab-core")
    assert "error" not in loaded
    assert loaded["readReceipt"]["entryComplete"] is True
    assert loaded["instructions"] == catalog.skill("replaylab-core").instructions.strip()
    core = " ".join(loaded["instructions"].split())
    assert "The user defines the outcome; the Agent chooses the method" in core
    assert "not a semantic completion decision or required tool sequence" in core
    assert "extra operations solely for progress markers or arbitrary review counts" in core
    assert "flowSpecTemplate" not in core
    assert "unpacked-asset status early" not in core
    assert len(loaded["instructions"]) < 5_000


@pytest.mark.parametrize("role", ROLES)
def test_every_role_uses_same_conversation_target_boundary_without_shader_script(role: str) -> None:
    from replaylab_agent.agent_prompts import FRAME_REANALYSIS_GUIDANCE, TASK_TARGET_SELECTION

    catalog = CatalogView.load(skill_roots=[SKILL_ROOT])
    prompt = _prompt(catalog, role)
    assert prompt.count(TASK_TARGET_SELECTION) == 1
    assert "latest user request" in prompt
    assert "UI selection is a fallback" in prompt
    assert "SHADER_EVIDENCE_WORKFLOW" not in prompt
    assert "200–320" not in prompt
    assert "final_no_supported_match=true" not in prompt
    # A preloaded domain Skill is intentionally delivered in full. Bound the
    # shared prompt overhead independently, and reject accidental duplication.
    skill_text = catalog.skill("renderdoc-shader-workbench").instructions.strip()
    assert prompt.count(skill_text) <= 1
    assert len(prompt.replace(skill_text, "").replace(FRAME_REANALYSIS_GUIDANCE, "")) < 16_000
    assert len(FRAME_REANALYSIS_GUIDANCE) < 1_400


@pytest.mark.parametrize("role", ROLES)
def test_every_role_loads_relevant_guidance_without_host_workflow_gate(role: str) -> None:
    from replaylab_agent.agent_prompts import (
        SKILL_TASK_GUIDANCE,
        TASK_ACCEPTANCE_GUIDANCE,
        TOOL_EXECUTION_GUIDANCE,
    )

    prompt = _prompt(CatalogView.load(skill_roots=[SKILL_ROOT]), role)
    assert "tools directly" in prompt
    assert prompt.count(SKILL_TASK_GUIDANCE) == 1
    assert prompt.count(TASK_ACCEPTANCE_GUIDANCE) == 1
    assert prompt.count(TOOL_EXECUTION_GUIDANCE) == 1
    assert "load it before substantive work" in prompt
    assert "complete instructions are already present in your own current context" in SKILL_TASK_GUIDANCE
    assert "delivery to an actor, not that you received the complete Skill" in SKILL_TASK_GUIDANCE
    assert "absent or incomplete in your context, load it even when that flag is true" in SKILL_TASK_GUIDANCE
    assert "rather than replacing it with Host semantic scoring" in prompt
    # Whole shared contracts are injected exactly once into every role; a
    # changed sentence must not require duplicating the former wording.
    assert "repair actionable gaps within the authorized scope" in TASK_ACCEPTANCE_GUIDANCE
    assert "continue that work now in this run" in TASK_ACCEPTANCE_GUIDANCE
    assert "a full task does not become a selection of examples" in TASK_ACCEPTANCE_GUIDANCE
    assert "future-work suggestion" in TASK_ACCEPTANCE_GUIDANCE
    assert "self-review the actual result against the user's criteria" in TASK_ACCEPTANCE_GUIDANCE
    assert "review the changed result again before ending" in TASK_ACCEPTANCE_GUIDANCE
    assert "report_task_result" not in prompt


def test_native_missing_capture_connection_does_not_imply_execution_from_snapshot() -> None:
    prompt = native_agent_system_prompt(
        catalog=CatalogView.load(skill_roots=[SKILL_ROOT]),
        request="Organize and apply this Shader", context={"capture": {"loaded": True}},
        response_language="zh_CN", tool_names=(), protocol="claude-acp", effects=(),
    )
    assert "do not substitute the UI snapshot for executing" in prompt
    assert "what remains undone" in prompt
    assert "AskUserQuestion only for a material choice with no safe default" in prompt


def test_shared_execution_guidance_reuses_work_and_preserves_real_permission_boundary() -> None:
    from replaylab_agent.agent_prompts import TOOL_EXECUTION_GUIDANCE

    # This is instructional policy, shared by API/native/child roles above;
    # it must neither create another Host workflow gate nor invent authority.
    assert "do not repeat full-file reads, outline listings or finalization" in TOOL_EXECUTION_GUIDANCE
    assert "search the attached capability directory" in TOOL_EXECUTION_GUIDANCE
    assert "within existing authorization" in TOOL_EXECUTION_GUIDANCE
    assert "never invent a grant or bypass a denial" in TOOL_EXECUTION_GUIDANCE
    assert "inspect its returned job/operation status" in TOOL_EXECUTION_GUIDANCE
    assert "without busy polling" in TOOL_EXECUTION_GUIDANCE


@pytest.mark.parametrize("role", ROLES)
def test_explicit_redraw_requires_fresh_judgment_with_provenance_aware_reuse(role: str) -> None:
    from replaylab_agent.agent_prompts import FRAME_REANALYSIS_GUIDANCE

    prompt = _prompt(CatalogView.load(skill_roots=[SKILL_ROOT]), role)
    assert prompt.count(FRAME_REANALYSIS_GUIDANCE) == 1
    assert "unless the user limits it to presentation" in FRAME_REANALYSIS_GUIDANCE
    assert "prepare_frame_render_flow(reanalyze=True)" in FRAME_REANALYSIS_GUIDANCE
    assert "not an accepted partition" in FRAME_REANALYSIS_GUIDANCE
    assert "checking only old gaps does not satisfy" in FRAME_REANALYSIS_GUIDANCE
    assert "Reuse unchanged raw bytes, source, bindings and images with valid provenance" in FRAME_REANALYSIS_GUIDANCE
    assert "do not flush caches or force fresh GPU reads" in FRAME_REANALYSIS_GUIDANCE
    assert "a historical blocker is not a current refusal" in FRAME_REANALYSIS_GUIDANCE
    assert "within authorization" in FRAME_REANALYSIS_GUIDANCE
    assert "without required technologies, EIDs or module counts" in FRAME_REANALYSIS_GUIDANCE
    assert "may confirm a prior conclusion" in FRAME_REANALYSIS_GUIDANCE


@pytest.mark.parametrize("latest_request", [
    "重新绘制这帧渲染流程图", "请重新生成并完整分析这一帧", "重新分析，不要只发布之前的图",
    "只调整目录字体，不要重新分析", "重新生成这个Shader的注释",
])
def test_followup_intent_remains_the_latest_user_request_not_keyword_rewriting(latest_request: str) -> None:
    from replaylab_agent.agent_prompts import main_agent_user_prompt

    prompt = main_agent_user_prompt(request=latest_request, history=[
        ("user", "请分析这一帧"),
        ("assistant", "已发布旧图；任务blocked，之前的工具无法取得所需数据。"),
    ])
    assert prompt.endswith("## Current request\n" + latest_request)
    assert prompt.count(latest_request) == 1
    assert prompt.index("## Conversation so far") < prompt.index("## Current request")
    # Intent stays model-selected and scoped; Chinese words are guidance examples, not a new
    # classifier that forcibly turns a layout-only or Shader request into frame reanalysis.
    assert "reanalyze=True" not in prompt


@pytest.mark.parametrize("role", ROLES)
def test_asset_preparation_and_first_edit_quality_reach_api_and_native_roles(role: str) -> None:
    from replaylab_agent.agent_prompts import ON_DEMAND_DRAW_GUIDANCE, TASK_ACCEPTANCE_GUIDANCE

    prompt = _prompt(CatalogView.load(skill_roots=[SKILL_ROOT]), role)
    assert prompt.count(ON_DEMAND_DRAW_GUIDANCE) == 1
    assert "configured unpacked asset root" in ON_DEMAND_DRAW_GUIDANCE
    assert "before restructuring" in ON_DEMAND_DRAW_GUIDANCE
    assert "A remembered match alone does not prove its names were applied" in ON_DEMAND_DRAW_GUIDANCE
    assert "Missing readers or unresolved original names do not block" in ON_DEMAND_DRAW_GUIDANCE
    assert "from the first edit, not only during self-review" in TASK_ACCEPTANCE_GUIDANCE
    assert "plain language" in TASK_ACCEPTANCE_GUIDANCE


@pytest.mark.parametrize("role", ROLES)
def test_only_main_roles_receive_whole_task_review_submission_contract(role: str) -> None:
    from replaylab_agent.agent_prompts import CHILD_REPORT_FORMAT, TASK_REVIEW_GUIDANCE

    prompt = _prompt(CatalogView.load(skill_roots=[SKILL_ROOT]), role)
    if role in ("api-child", "api-general"):
        assert TASK_REVIEW_GUIDANCE not in prompt
        assert CHILD_REPORT_FORMAT in prompt
    else:
        assert prompt.count(TASK_REVIEW_GUIDANCE) == 1
        assert CHILD_REPORT_FORMAT not in prompt


@pytest.mark.parametrize("role", ("api-main", "claude-acp", "codex", "other-acp"))
def test_main_connections_share_concise_user_answer_without_review_dump(role: str) -> None:
    from replaylab_agent.agent_prompts import FINAL_ANSWER_GUIDANCE

    prompt = _prompt(CatalogView.load(skill_roots=[SKILL_ROOT]), role)
    assert prompt.count(FINAL_ANSWER_GUIDANCE) == 1
    assert "Detailed evidence belongs in the task log" in FINAL_ANSWER_GUIDANCE
    assert "Retain useful artifact links and requested substantive explanations" in FINAL_ANSWER_GUIDANCE
    assert "keep that status explicit" in FINAL_ANSWER_GUIDANCE
    assert "actual cause and practical next step" in FINAL_ANSWER_GUIDANCE
    assert "Skip gate jargon" in FINAL_ANSWER_GUIDANCE


def test_general_review_reference_is_losslessly_available_without_loading_domain_skills() -> None:
    import hashlib
    from replaylab_agent.skill_gateway import MAX_SKILL_INLINE_BYTES, SkillGateway, encode

    catalog = CatalogView.load(skill_roots=[SKILL_ROOT])
    gateway = SkillGateway(catalog)
    core = gateway.load("replaylab-core")
    name = "references/task-review.md"
    assert name in core["references"]
    detail = gateway.load("replaylab-core", name)
    expected = (SKILL_ROOT / "replaylab-core" / name).read_bytes().decode("utf-8")
    assert detail["instructions"] == expected
    assert detail["hasMore"] is False
    assert len(encode(detail).encode("utf-8")) <= MAX_SKILL_INLINE_BYTES
    assert detail["readReceipt"]["instructionsSha256"] == hashlib.sha256(expected.encode()).hexdigest()
    assert gateway.loaded_skill_ids == ("replaylab-core",)


@pytest.mark.parametrize("role", ("api-main", "other-acp"))
def test_explicit_skill_can_be_reused_when_full_entry_is_in_actor_context(role: str) -> None:
    """The clarified read exception also preserves real preloaded guidance."""

    from replaylab_agent.agent_prompts import SKILL_TASK_GUIDANCE

    catalog = CatalogView.load(skill_roots=[SKILL_ROOT])
    skill = catalog.skill("renderdoc-shader-workbench")
    if role == "api-main":
        prompt = deep_main_system_prompt(
            catalog=catalog, request="Organize this Shader", context=None,
            response_language="zh_CN", subagents=[], loaded_skill=skill,
        )
    else:
        prompt = native_agent_system_prompt(
            catalog=catalog, request="Organize this Shader", context=None,
            response_language="zh_CN", tool_names=("inspect_shader",),
            protocol=role, effects=(), loaded_skill=skill,
        )
    assert skill.instructions.strip() in prompt
    assert prompt.count(SKILL_TASK_GUIDANCE) == 1


def test_shader_skill_and_references_load_completely_with_content_receipts() -> None:
    import hashlib
    from replaylab_agent.skill_gateway import SkillGateway

    catalog = CatalogView.load(skill_roots=[SKILL_ROOT])
    gateway = SkillGateway(catalog)
    loaded = gateway.load("renderdoc-shader-workbench")
    assert "error" not in loaded
    assert loaded["readReceipt"]["entryComplete"] is True
    assert loaded["category"] == "shader"
    assert loaded["instructions"] == catalog.skill(loaded["id"]).instructions.strip()
    assert loaded["readReceipt"]["instructionsSha256"] == hashlib.sha256(loaded["instructions"].encode()).hexdigest()
    # Exercise the actual delivery contract, not a former wording of the
    # instructions. Semantic workflow decisions need independent review.
    assert {"references/asset-naming.md", "references/reconstruction.md", "references/source-operations.md",
            "references/self-review.md"}.issubset(loaded["references"])
    for reference in loaded["references"]:
        body = gateway.load(loaded["id"], reference)
        assert "error" not in body, body
        assert body["reference"] is True and body["hasMore"] is False
        assert body["instructions"] == (SKILL_ROOT / loaded["id"] / reference).read_bytes().decode("utf-8")
        assert body["readReceipt"]["instructionsSha256"] == hashlib.sha256(body["instructions"].encode()).hexdigest()


def test_shader_review_contract_fields_are_delivered_with_the_complete_reference() -> None:
    import hashlib
    from replaylab_agent.skill_gateway import SkillGateway

    gateway = SkillGateway(CatalogView.load(skill_roots=[SKILL_ROOT]))
    loaded = gateway.load("renderdoc-shader-workbench")
    assert loaded["readReceipt"]["entryComplete"] is True
    instructions = " ".join(loaded["instructions"].split())
    for field in ("readability", "unity_style_structure", "function_calls", "redundant_temporaries"):
        assert "`" + field + "`" in instructions
    assert "record_shader_organization_review" in instructions
    assert "references/self-review.md" in loaded["references"]
    detail = gateway.load(loaded["id"], "references/self-review.md")
    assert detail["hasMore"] is False
    assert detail["instructions"] == (SKILL_ROOT / loaded["id"] / "references/self-review.md").read_bytes().decode("utf-8")
    assert detail["readReceipt"]["instructionsSha256"] == hashlib.sha256(detail["instructions"].encode()).hexdigest()


def test_shader_reference_supports_whole_file_work_without_equating_delivery_to_review() -> None:
    from replaylab_agent.skill_gateway import SkillGateway

    gateway = SkillGateway(CatalogView.load(skill_roots=[SKILL_ROOT]))
    detail = gateway.load("renderdoc-shader-workbench", "references/source-operations.md")
    instructions = " ".join(detail["instructions"].split())
    assert 'begin_shader_workspace_organization(agent_file_name="organized.hlsl")' in instructions
    assert 'mode="file"' in instructions
    assert 'mode="complete"' in instructions
    assert 'mode="range"' in instructions
    assert "expected_proposal_sha256" in instructions
    assert "Copying a file does not mean the Agent has read or reviewed it" in instructions


@pytest.mark.parametrize("role", tuple(role for role in ROLES if role != "api-main"))
def test_lazy_dispatch_example_keeps_literal_argument_object(role: str) -> None:
    prompt = _prompt(CatalogView.load(skill_roots=[SKILL_ROOT]), role)
    if role.startswith("api-"):
        assert "arguments={...}" in prompt
    assert "arguments=Ellipsis" not in prompt


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("fault", ("missing", "empty"))
def test_missing_or_empty_core_does_not_block_prompt(role: str, fault: str) -> None:
    catalog = CatalogView.load(skill_roots=[SKILL_ROOT])
    core = catalog.skill("replaylab-core")
    original_core = core.instructions.strip()
    if fault == "missing":
        del catalog.skills[core.id]
    else:
        from dataclasses import replace
        catalog.skills[core.id] = replace(core, instructions=" ")
    prompt = _prompt(catalog, role)
    assert "You are Bread AI" in prompt or "You are a Bread AI sub-agent" in prompt
    assert original_core not in prompt


def test_quarantined_core_is_diagnostic_not_a_runtime_blocker(tmp_path: Path) -> None:
    core_dir = tmp_path / "replaylab-core"
    core_dir.mkdir()
    (core_dir / "SKILL.md").write_text("---\nname: [broken YAML\n---\n", encoding="utf-8")
    catalog = CatalogView.load(skill_roots=[tmp_path])
    assert catalog.diagnostics
    assert catalog.skills == {}


def test_agent_report_imports_sibling_mcp_src_before_source_namespace() -> None:
    """Editable collection must not cache ``replaylab/src`` as package ``src``."""

    package_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(package_root / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import replaylab_agent.agent_report; "
                "import src.task_report as task_report; "
                "print(task_report.__file__)"
            ),
        ],
        cwd=package_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert str(package_root / "mcp_host" / "src" / "task_report.py") in result.stdout
