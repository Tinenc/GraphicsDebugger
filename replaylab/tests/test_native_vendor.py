"""Vendor-native workspace, MCP, and session hardening."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
import pytest

from replaylab_agent.native_vendor import (
    CLAUDE_BUILTIN_TOOLS,
    agent_env,
    copy_claude_skills,
    main_agent_env,
    prepare_workspace,
    runtime_argv,
    session_meta,
    write_claude_mcp_config,
)
from replaylab_agent.profiles import Profile


def test_claude_skill_copy_omits_dependency_trees_and_keeps_deep_references(tmp_path):
    from replaylab_agent.capability_store import _native
    source = _native(tmp_path / "source")
    source.mkdir()
    (source / "SKILL.md").write_text("instructions")
    for name in ("node_modules", ".git", "__pycache__", ".venv", ".pytest_cache"):
        path = source / "scripts" / name / "nested" / "ignored.txt"
        path.parent.mkdir(parents=True)
        path.write_text("ignored")
    reference = Path(*(["references"] + ["long-reference-directory"] * 14 + ["evidence.md"]))
    (source / reference).parent.mkdir(parents=True)
    (source / reference).write_text("retained")
    workspace = tmp_path / "workspace"
    copy_claude_skills(workspace, [("demo", source / "SKILL.md")])
    target = _native(workspace / ".claude/skills/demo")
    assert (target / reference).read_text() == "retained"
    assert not list(target.rglob("ignored.txt"))


def test_claude_skill_copy_rejects_links_in_retained_references(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("instructions")
    (source / "real.txt").write_text("retained")
    try:
        (source / "link.txt").symlink_to(source / "real.txt")
    except OSError:
        pytest.skip("Creating symlinks requires Windows Developer Mode")
    with pytest.raises(ValueError, match="link or reparse"):
        copy_claude_skills(tmp_path / "workspace", [("demo", source / "SKILL.md")])


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions")
def test_claude_skill_copy_prunes_dependency_junction_but_rejects_reference_junction(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("instructions")
    external = tmp_path / "external"
    external.mkdir()
    (external / "private.txt").write_text("do not follow")
    for name in ("node_modules", "references"):
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(source / name), str(external)],
                                capture_output=True)
        assert result.returncode == 0
    with pytest.raises(ValueError, match="link or reparse"):
        copy_claude_skills(tmp_path / "rejected", [("demo", source / "SKILL.md")])
    (source / "references").rmdir()
    copy_claude_skills(tmp_path / "allowed", [("demo", source / "SKILL.md")])
    assert not (tmp_path / "allowed/.claude/skills/demo/node_modules").exists()
    assert (external / "private.txt").read_text() == "do not follow"


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions")
def test_claude_skill_root_cannot_be_a_junction(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "SKILL.md").write_text("external instructions")
    linked = tmp_path / "linked"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(external)], capture_output=True)
    assert result.returncode == 0
    with pytest.raises(ValueError, match="link or reparse"):
        copy_claude_skills(tmp_path / "workspace", [("demo", linked / "SKILL.md")])


def test_claude_session_meta_keeps_only_native_skill_and_replaylab_mcp() -> None:
    profile = Profile(
        "claude",
        "claude-code-acp",
        options={"allow_unsandboxed_agent": True},
    )

    options = session_meta(profile)["claudeCode"]["options"]

    assert options["tools"] == ["Skill"]
    assert options["allowedTools"] == ["mcp__Bread"]
    assert options["strictMcpConfig"] is True
    assert options["settingSources"] == ["project"]
    assert "Bash" in options["disallowedTools"]
    assert "Agent" in options["disallowedTools"]
    assert "AskUserQuestion" not in options["disallowedTools"]


def test_claude_workspace_has_one_strict_mcp_and_copied_skills(tmp_path: Path) -> None:
    profile = Profile(
        "claude",
        "claude-code-acp",
        options={"allow_unsandboxed_agent": True},
    )
    source = tmp_path / "source" / "demo"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo.\n---\nUse evidence.\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    prepare_workspace(profile, workspace)
    write_claude_mcp_config(
        workspace,
        command="replaylab-ai",
        args=("-I", "-m", "replaylab_agent.mcp_proxy"),
        env={"REPLAYLAB_TASK_ID": "task-1"},
    )
    copy_claude_skills(workspace, [("demo", source / "SKILL.md")])

    settings = json.loads((workspace / ".claude" / "settings.json").read_text(encoding="utf-8"))
    mcp = json.loads((workspace / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["Bread"]
    assert set(settings["permissions"]["deny"]) == set(CLAUDE_BUILTIN_TOOLS) - {
        "Skill",
        "AskUserQuestion",
    }
    assert mcp["type"] == "stdio"
    assert mcp["args"] == ["-I", "-m", "replaylab_agent.mcp_proxy"]
    assert (workspace / ".claude" / "skills" / "demo" / "SKILL.md").is_file()


def test_gemini_and_qwen_workspaces_exclude_ambient_tools(tmp_path: Path) -> None:
    for preset, directory in (("gemini-acp", ".gemini"), ("qwen-acp", ".qwen")):
        workspace = tmp_path / preset
        workspace.mkdir()
        prepare_workspace(
            Profile(preset, preset, options={"allow_unsandboxed_agent": True}),
            workspace,
            ("inspect_event", "report_task_result"),
        )
        payload = json.loads((workspace / directory / "settings.json").read_text(encoding="utf-8"))
        assert payload["tools"]["core"] == []
        assert payload["tools"]["allowed"] == [
            "mcp_Bread_inspect_event",
            "mcp_Bread_report_task_result",
        ]
        assert payload["mcp"] == {"allowed": ["Bread"], "excluded": []}
        assert "run_shell_command" in payload["tools"]["exclude"]
        assert "invoke_agent" in payload["tools"]["exclude"]


def test_vendor_runtime_argv_applies_launch_scoped_tool_filters(tmp_path: Path) -> None:
    copilot = Profile(
        "copilot", "copilot-acp", options={"allow_unsandboxed_agent": True}
    )
    assert runtime_argv(
        copilot,
        ["copilot", "--acp"],
        tmp_path,
        ("report_task_result", "inspect_event"),
    ) == [
        "copilot",
        "--acp",
        "--disable-builtin-mcps",
        "--available-tools=Bread(inspect_event),Bread(report_task_result)",
        "--allow-tool=Bread(inspect_event),Bread(report_task_result)",
    ]

    gemini = Profile(
        "gemini", "gemini-acp", options={"allow_unsandboxed_agent": True}
    )
    assert runtime_argv(
        gemini,
        ["gemini", "--acp"],
        tmp_path,
        ("inspect_event",),
    )[-2:] == [
        "--allowed-mcp-server-names=Bread",
        "--allowed-tools=mcp_Bread_inspect_event",
    ]


def test_kimi_uses_a_per_turn_exact_tool_agent(tmp_path: Path) -> None:
    profile = Profile(
        "kimi", "kimi-acp", options={"allow_unsandboxed_agent": True}
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prepare_workspace(profile, workspace, ("inspect_event", "report_task_result"))

    agent = workspace / ".kimi-code" / "replaylab-agent.md"
    text = agent.read_text(encoding="utf-8")
    assert "description: Bread AI task-scoped RenderDoc analysis agent" in text
    assert "description: ReplayLab task-scoped" not in text
    assert 'mcp__Bread__inspect_event' in text
    assert 'mcp__Bread__report_task_result' in text
    assert "subagents: []" in text
    assert "Read" not in text
    assert runtime_argv(profile, ["kimi", "acp"], workspace) == [
        "kimi",
        "--agent-file",
        str(agent),
        "acp",
    ]


def test_claude_environment_bounds_thinking_and_mcp_timeout() -> None:
    profile = Profile(
        "claude",
        "claude-code-acp",
        options={"allow_unsandboxed_agent": True, "thinking_budget_tokens": 12345},
    )

    environment = main_agent_env(profile, 90.0)

    assert environment["MAX_THINKING_TOKENS"] == "12345"
    assert environment["MAX_MCP_OUTPUT_TOKENS"] == "64000"
    assert environment["MCP_TIMEOUT"] == "60000"
    assert environment["MCP_TOOL_TIMEOUT"] == "90000"
    assert agent_env(Profile("codex", "codex")) == {}


def test_vendors_without_session_meta_get_none() -> None:
    for preset in ("codex", "cursor-acp", "gemini-acp", "qwen-acp"):
        assert session_meta(Profile(preset, preset)) is None
