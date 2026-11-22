"""Per-vendor containment for native subscription Agents.

Vendor Agents ship ambient shell, file, web, and delegation tools. ReplayLab
disables those surfaces and exposes only the task-scoped ``ReplayLab`` MCP plus
the vendor's Skill/elicitation primitives. The ACP client also rejects foreign
permission requests and terminal/file callbacks as a fail-closed boundary.

* **Claude Code** (``claude-agent-acp``): ``session/new`` ``_meta.claudeCode``
  options (``tools: []``, ``disallowedTools``, ``allowedTools``,
  ``settingSources``, ``strictMcpConfig``) plus a ``.claude/settings.json`` in
  the fresh workspace as a second layer; thinking via ``MAX_THINKING_TOKENS``.
* **Gemini CLI family** (``gemini-acp``, ``qwen-acp``): workspace
  ``settings.json`` with a built-in-tool gate, exact MCP tool approvals and an
  MCP server allow-list.
* **Cursor**: handled by ``process_env.ensure_cursor_workspace`` (permissions).
* **CodeBuddy**: handled on the preset command line (``--tools ""``).
* **Copilot**: exact ``--available-tools`` / ``--allow-tool`` launch options.
* **Kimi**: a per-turn explicit Agent file containing only the exact task MCP
  tools, selected with ``--agent-file``.
* **custom**: the client-side denial layer alone applies.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from .catalog import BREAD_MCP_SERVER_NAME
from .policy import ordered_tool_names
from .profiles import Profile

# Claude Code's own tools (removed via ``tools: []`` and additionally denied).
CLAUDE_BUILTIN_TOOLS: tuple[str, ...] = (
    "Agent", "Task", "Bash", "BashOutput", "KillShell", "PowerShell", "Read", "Write", "Edit",
    "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS", "WebSearch", "WebFetch", "TodoWrite",
    "Skill", "AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "SandboxNetworkAccess",
)
CLAUDE_NATIVE_TOOLS: tuple[str, ...] = ("Skill", "AskUserQuestion")

# Gemini CLI / Qwen Code built-in tool names (``tools.exclude``).
GEMINI_BUILTIN_TOOLS: tuple[str, ...] = (
    "run_shell_command", "write_file", "replace", "edit", "read_file", "read_many_files",
    "list_directory", "glob", "grep_search", "search_file_content", "google_web_search",
    "web_search", "web_fetch", "write_todos", "save_memory", "ask_user", "invoke_agent",
    "activate_skill", "enter_plan_mode", "exit_plan_mode", "complete_task",
    "get_internal_docs", "list_mcp_resources", "read_mcp_resource", "update_topic",
    "tracker_create_task", "tracker_update_task", "tracker_get_task", "tracker_list_tasks",
    "tracker_add_dependency", "tracker_visualize", "list_background_processes",
    "read_background_output",
)

DEFAULT_THINKING_BUDGET = 8000
_CLAUDE_PRESET = "claude-code-acp"
_GEMINI_FAMILY = {"gemini-acp": ".gemini", "qwen-acp": ".qwen"}


def thinking_budget(profile: Profile) -> int:
    """``thinking_budget_tokens`` profile option; ``0`` switches thinking off."""

    try:
        value = int(profile.options.get("thinking_budget_tokens", DEFAULT_THINKING_BUDGET))
    except (TypeError, ValueError):
        return DEFAULT_THINKING_BUDGET
    return max(0, min(value, 128_000))


def claude_session_meta(setting_sources: Sequence[str] = ("project",)) -> dict[str, Any]:
    """``session/new`` ``_meta`` for ``claude-agent-acp``.

    ``tools: [\"Skill\"]`` leaves only project Skills available;
    ``AskUserQuestion`` is separately bridged by claude-agent-acp when the ACP
    client advertises form elicitation. ``allowedTools`` auto-approves
    the Host MCP server (no permission round-trip), ``settingSources`` keeps the
    user's global ``~/.claude/settings.json`` (hooks, plugins, allow rules) out
    of the session, ``strictMcpConfig`` ignores any other configured MCP server.
    """

    return {
        "claudeCode": {
            "options": {
                "tools": ["Skill"],
                "disallowedTools": [
                    item for item in CLAUDE_BUILTIN_TOOLS if item not in CLAUDE_NATIVE_TOOLS
                ],
                "allowedTools": [f"mcp__{BREAD_MCP_SERVER_NAME}"],
                "settingSources": list(setting_sources),
                "strictMcpConfig": True,
            }
        },
    }


def session_meta(profile: Profile, *, inherit_desktop_capabilities: bool = False) -> dict[str, Any] | None:
    """Vendor ``_meta`` for ``session/new`` or ``None`` when unsupported."""

    if profile.preset == _CLAUDE_PRESET:
        if inherit_desktop_capabilities:
            return {"claudeCode": {"options": {"settingSources": ["user", "project", "local"]}}}
        return claude_session_meta()
    return None


def write_claude_settings(workspace: Path) -> None:
    """Deny every built-in Claude Code tool in this workspace (project settings)."""

    settings_dir = workspace / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "permissions": {
            "allow": [
                "Skill",
                "AskUserQuestion",
                f"mcp__{BREAD_MCP_SERVER_NAME}",
            ],
            "deny": [
                item for item in CLAUDE_BUILTIN_TOOLS if item not in CLAUDE_NATIVE_TOOLS
            ],
            "defaultMode": "default",
        },
        "includeCoAuthoredBy": False,
    }
    (settings_dir / "settings.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def write_claude_mcp_config(
    workspace: Path,
    *,
    command: str,
    args: Sequence[str],
    env: Mapping[str, str] | None = None,
    extra_servers: Sequence[Mapping[str, Any]] = (),
) -> None:
    """Pin Claude Code to the filtered ReplayLab route plus the user's own servers."""

    servers: dict[str, Any] = {
        BREAD_MCP_SERVER_NAME: {
            "type": "stdio",
            "command": str(command),
            "args": [str(value) for value in args],
            "env": {str(key): str(value) for key, value in (env or {}).items()},
        }
    }
    for server in extra_servers:
        identifier = str(server.get("id", "") or "").strip()
        if not identifier or identifier == BREAD_MCP_SERVER_NAME:
            continue
        if str(server.get("transport", "stdio")) == "stdio":
            servers[identifier] = {
                "type": "stdio",
                "command": str(server.get("command", "")),
                "args": [str(value) for value in server.get("args", ()) or ()],
                "env": {str(key): str(value) for key, value in (server.get("env") or {}).items()},
            }
        else:
            servers[identifier] = {"type": "http", "url": str(server.get("url", ""))}
    payload = {"mcpServers": servers}
    (workspace / ".mcp.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def copy_claude_skills(
    workspace: Path,
    skills: Sequence[tuple[str, Path]],
) -> None:
    """Copy admitted filesystem Skills into Claude's project Skill directory."""

    from .capability_store import _native

    excluded = {"node_modules", ".git", "__pycache__", ".venv", "venv", ".pytest_cache"}
    root = _native(workspace / ".claude" / "skills")
    root.mkdir(parents=True, exist_ok=True)
    for skill_id, skill_file in skills:
        declared = _native(skill_file)
        if not declared.exists():
            continue
        if declared.is_symlink() or getattr(declared.lstat(), "st_file_attributes", 0) & 0x400:
            continue
        declared_root = declared.parent
        if declared_root.is_symlink() or getattr(declared_root.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Skill '{skill_id}' contains a link or reparse point")
        source = declared.resolve()
        if source.name != "SKILL.md" or not source.is_file():
            continue
        source_root = source.parent
        if source_root.is_symlink() or getattr(source_root.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Skill '{skill_id}' contains a link or reparse point")
        def fail(error):
            raise error
        for current, dirs, files in os.walk(source_root, followlinks=False, onerror=fail):
            dirs[:] = [name for name in dirs if name not in excluded]
            for name in dirs + files:
                entry = Path(current) / name
                if entry.is_symlink() or getattr(entry.lstat(), "st_file_attributes", 0) & 0x400:
                    raise ValueError(f"Skill '{skill_id}' contains a link or reparse point")
        if not skill_id or Path(str(skill_id)).name != str(skill_id) or str(skill_id) in {".", ".."} or any(c in str(skill_id) for c in "\\/:"):
            raise ValueError("Invalid Claude skill directory name")
        destination = root / str(skill_id)
        shutil.copytree(source_root, destination, dirs_exist_ok=False, symlinks=False,
                        ignore=shutil.ignore_patterns(*excluded))


def _gemini_mcp_tool_name(tool_name: str) -> str:
    return f"mcp_{BREAD_MCP_SERVER_NAME}_{tool_name}"


def _kimi_mcp_tool_name(tool_name: str) -> str:
    return f"mcp__{BREAD_MCP_SERVER_NAME}__{tool_name}"


def write_gemini_settings(
    workspace: Path,
    config_dir: str,
    allowed_tools: Sequence[str] = (),
) -> None:
    """Exclude the built-in tools and trust the ReplayLab MCP server (workspace scope).

    ``mcp.allowed`` contains server names; ``tools.allowed`` contains exact
    fully-qualified tool names. Mixing those namespaces makes the server
    connect but leaves every tool behind a confirmation gate. ``tools.core``
    is empty to suppress built-ins; the explicit MCP entries remain visible on
    Gemini releases whose core allow-list currently also filters MCP tools.

    Both the nested and flat legacy keys are written for the Gemini-derived
    CLIs which still read different generations of the settings schema.
    """

    settings_dir = workspace / config_dir
    settings_dir.mkdir(parents=True, exist_ok=True)
    exclude = list(GEMINI_BUILTIN_TOOLS)
    allowed = [_gemini_mcp_tool_name(name) for name in ordered_tool_names(allowed_tools)]
    payload = {
        "tools": {"core": [], "exclude": exclude, "allowed": allowed},
        "mcp": {"allowed": [BREAD_MCP_SERVER_NAME], "excluded": []},
        "excludeTools": exclude,
        "allowedTools": allowed,
        "telemetry": {"enabled": False, "logPrompts": False},
        "usageStatisticsEnabled": False,
    }
    (settings_dir / "settings.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def write_kimi_agent(workspace: Path, allowed_tools: Sequence[str]) -> Path:
    """Create a launch-scoped Kimi Agent whose visible tools are task exact."""

    settings_dir = workspace / ".kimi-code"
    settings_dir.mkdir(parents=True, exist_ok=True)
    target = settings_dir / "replaylab-agent.md"
    tool_lines = [
        f"  - {json.dumps(_kimi_mcp_tool_name(name), ensure_ascii=True)}"
        for name in ordered_tool_names(allowed_tools)
    ]
    tools = "tools: []" if not tool_lines else "tools:\n" + "\n".join(tool_lines)
    target.write_text(
        "---\n"
        "name: replaylab\n"
        "description: Bread AI task-scoped RenderDoc analysis agent\n"
        + tools
        + "\nsubagents: []\n"
        "---\n\n"
        "${base_prompt}\n",
        encoding="utf-8",
    )
    return target


def prepare_workspace(
    profile: Profile,
    workspace: Path,
    allowed_tools: Sequence[str] = (),
    *, inherit_desktop_capabilities: bool = False,
) -> None:
    """Write the vendor's workspace-scoped configuration into a fresh workspace."""

    if inherit_desktop_capabilities:
        return
    if profile.preset == _CLAUDE_PRESET:
        write_claude_settings(workspace)
    elif profile.preset in _GEMINI_FAMILY:
        write_gemini_settings(
            workspace,
            _GEMINI_FAMILY[profile.preset],
            allowed_tools,
        )
    elif profile.preset == "kimi-acp":
        write_kimi_agent(workspace, allowed_tools)


def runtime_argv(
    profile: Profile,
    argv: Sequence[str],
    workspace: Path,
    allowed_tools: Sequence[str] = (),
    *, inherit_desktop_capabilities: bool = False,
) -> list[str]:
    """Apply launch-only vendor controls which ACP ``session/new`` cannot carry."""

    result = list(argv)
    if inherit_desktop_capabilities:
        # The shipped CodeBuddy preset contains a Bread-imposed empty tool
        # allow-list. Remove that exact restriction, not user-provided options.
        if profile.preset == "codebuddy-acp":
            for index in range(len(result) - 2, -1, -1):
                if result[index:index + 2] == ["--tools", ""]:
                    del result[index:index + 2]
        return result
    exact_tools = ordered_tool_names(allowed_tools)
    if profile.preset == "copilot-acp":
        result.append("--disable-builtin-mcps")
        if exact_tools:
            identifiers = ",".join(
                f"{BREAD_MCP_SERVER_NAME}({name})" for name in exact_tools
            )
            result.extend(
                (
                    f"--available-tools={identifiers}",
                    f"--allow-tool={identifiers}",
                )
            )
    elif profile.preset == "gemini-acp" and exact_tools:
        result.extend(
            (
                f"--allowed-mcp-server-names={BREAD_MCP_SERVER_NAME}",
                "--allowed-tools="
                + ",".join(_gemini_mcp_tool_name(name) for name in exact_tools),
            )
        )
    elif profile.preset == "kimi-acp":
        agent_file = workspace / ".kimi-code" / "replaylab-agent.md"
        try:
            acp_index = result.index("acp")
        except ValueError:
            acp_index = len(result)
        result[acp_index:acp_index] = ["--agent-file", str(agent_file)]
    return result


def agent_env(profile: Profile) -> dict[str, str]:
    """Extra environment for the agent process (thinking on, no telemetry noise)."""

    env: dict[str, str] = {}
    if profile.preset == _CLAUDE_PRESET:
        budget = thinking_budget(profile)
        if budget > 0:
            env["MAX_THINKING_TOKENS"] = str(budget)
        env["DISABLE_AUTOUPDATER"] = "1"
        env["DISABLE_TELEMETRY"] = "1"
        # Claude Code spills MCP results above this many tokens into a file and
        # then tries its built-in Read tool — which Bread must keep denied. Keep
        # the ceiling high enough that Host-capped results stay inline, and rely
        # on read_evidence_file when a spill still happens.
        env["MAX_MCP_OUTPUT_TOKENS"] = "64000"
    elif profile.preset in _GEMINI_FAMILY:
        env["NO_COLOR"] = "1"
    elif profile.preset == "kimi-acp":
        env["KIMI_DISABLE_TELEMETRY"] = "1"
    return env


def main_agent_env(profile: Profile, main_timeout: float) -> dict[str, str]:
    """Environment for the long-lived main Agent process.

    Claude Code honours ``MCP_TIMEOUT`` (server start-up) and
    ``MCP_TOOL_TIMEOUT`` (one tool call). Capture export and validation tools can
    legitimately run for many minutes, so the tool timeout follows the main-turn
    budget.
    """

    env = agent_env(profile)
    env["MCP_TIMEOUT"] = "60000"
    env["MCP_TOOL_TIMEOUT"] = str(int(max(main_timeout, 60.0) * 1000))
    return env


def describe(profile: Profile) -> str:
    """One line for the log: which vendor switches apply to this preset."""

    preset = profile.preset
    if preset == _CLAUDE_PRESET:
        return "claude: project Skills + AskUserQuestion elicitation, one strict Bread MCP route, thinking via MAX_THINKING_TOKENS"
    if preset in _GEMINI_FAMILY:
        return f"gemini-family: {_GEMINI_FAMILY[preset]}/settings.json built-in gate + exact Bread MCP allow-list"
    if preset == "copilot-acp":
        return "copilot: exact Bread MCP tools via launch-only available/allow lists"
    if preset == "kimi-acp":
        return "kimi: launch-scoped Agent file exposes exact Bread MCP tools only"
    if preset == "cursor-acp":
        return "cursor: .cursor/cli.json permissions"
    if preset == "codebuddy-acp":
        return "codebuddy: --tools \"\" with the Bread compatibility tool id mcp__Bread on the command line"
    return "no vendor switch known; client-side denial layer only"


__all__ = [
    "CLAUDE_BUILTIN_TOOLS",
    "CLAUDE_NATIVE_TOOLS",
    "GEMINI_BUILTIN_TOOLS",
    "agent_env",
    "claude_session_meta",
    "describe",
    "copy_claude_skills",
    "main_agent_env",
    "prepare_workspace",
    "runtime_argv",
    "session_meta",
    "thinking_budget",
    "write_claude_settings",
    "write_claude_mcp_config",
    "write_gemini_settings",
    "write_kimi_agent",
]
