"""Every tool a Skill tells the AI to call has to exist, and be reachable.

A Skill is a procedure the AI follows literally. A tool name in it that was
renamed, or never existed, is not a typo the AI can route around: it reaches for
that name at the exact step the procedure says to, gets tool-not-found, and has
no substitute -- and these appear most often in the *recovery* branches, so the
failure lands when something has already gone wrong.

This checks the Skills against the Host's real registrations and against the
effect manifest, mechanically, so a rename cannot quietly break a procedure.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from replaylab_agent.cli import default_skill_root
from replaylab_agent.policy import AGENT_LOCAL_TOOLS, READ_ONLY_TOOLS, TOOL_EFFECTS

_REPO = Path(__file__).resolve().parents[1]
_SERVER = _REPO / "mcp_host" / "src" / "server.py"

# `name(` or `name(...)` in backticks, which is how a Skill names a call.
_CALL_RE = re.compile(r"`([a-z][a-z0-9_]{3,63})\s*\(")
# A backticked snake_case identifier with no parentheses. Half the dead names were
# written this way ("`read_recovery_regular_file` on a candidate"), so checking only
# call syntax misses exactly the mentions that read as prose.
_TOKEN_RE = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")

# Names a Skill may mention while telling the AI *not* to use them.
_PROHIBITED_LEGACY = {"export_drawcall", "export_to_unity", "save_texture",
                      "export_mesh", "export_texture"}
# Identifiers that are not tools: result keys and operation names a Skill quotes.
_NOT_TOOLS = {"ask_user", "delegation_requests", "workflow_progress", "rename_function",
              "remaining_work", "retained_intermediates", "observation_dispositions", "source_anchor",
              "evidence_paths", "parameter_localization", "source_functions",
              "material_inspector_not_connected"}  # Review/import gap identifiers, not tools.


def _registered_tools() -> tuple[set[str], set[str]]:
    """Tool names and their parameter names, from the AST: no MCP runtime needed."""

    tree = ast.parse(_SERVER.read_text(encoding="utf-8"))
    trees = [(tree, False)]
    # Split first-party registrations are connected explicitly from server.py.
    # Follow those imports rather than exempting their tool names from the gate.
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and any(isinstance(arg, ast.Name) and arg.id == "mcp" for arg in node.args)}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            for item in node.names:
                if item.name == "register_tools" and (item.asname or item.name) in calls:
                    source = _SERVER.parent / (node.module.replace(".", "/") + ".py")
                    trees.append((ast.parse(source.read_text(encoding="utf-8")), True))
    names: set[str] = set()
    parameters: set[str] = set()
    for source_tree, split_registration in trees:
        for node in ast.walk(source_tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                call = decorator.func if isinstance(decorator, ast.Call) else decorator
                attribute = getattr(call, "attr", "")
                owner = getattr(getattr(call, "value", None), "id", "")
                if (attribute == "tool" and owner == "mcp") or (split_registration and isinstance(call, ast.Name) and call.id == "tool"):
                    names.add(node.name)
                    for argument in list(node.args.args) + list(node.args.kwonlyargs):
                        parameters.add(argument.arg)
    # These are named in call_unity_project_tool arguments, and are registered
    # by the bundled Editor assembly instead of the Python Host.
    for source in (_REPO / "unity").rglob("*.cs"):
        text = source.read_text(encoding="utf-8")
        names.update(re.findall(r'\[McpForUnityTool\("([^"\r\n]+)"', text))
        parameters.update(re.findall(r'\[ToolParameter\("([^"\r\n]+)"', text))
        # SDK tools also consume JObject p directly instead of attributes.
        # Derive those actual keys rather than exempting prose identifiers.
        parameters.update(re.findall(r'\bp(?:\[\s*"|\.Value<[^>\r\n]+>\(\s*")([^"\r\n]+)"',text))
    return names, parameters


def _unpacker_operation_names() -> set[str]:
    """Recipe operations are callable too: ``run_asset_tool_operation(name=...)``.

    A Skill names them the same way it names a tool, and a renamed recipe breaks a
    procedure exactly like a renamed tool would -- so they are collected from the
    built-in unpackers rather than exempted by hand.
    """

    from replaylab_agent import builtin_unpackers

    names: set[str] = set()
    for spec in builtin_unpackers.BUILTIN_UNPACKERS:
        kind = spec["kind"]
        options = {"embedded": True} if kind == "unitypy" else {}
        definition = builtin_unpackers.builtin_unpacker_definition(
            kind, "program.exe", options)
        for operation in definition["interface"]["operations"]:
            names.add(str(operation["name"]))
    return names


def _skill_files() -> list[Path]:
    root = default_skill_root()
    if not root.is_dir():
        pytest.skip("the bundled Skill root is not present in this checkout")
    return sorted(root.glob("*/SKILL.md")) + sorted(root.glob("*/references/*.md"))


def test_every_tool_a_skill_names_is_a_real_tool() -> None:
    from src.shader_organization_review import CHECKS
    from src.task_completion_review import TASK_REVIEW_TOOL_SCHEMA
    from src.unity_restoration import REVIEW_AREAS

    tools, parameters = _registered_tools()
    assert len(tools) > 100, "the registration sweep found suspiciously few tools"
    # Review evidence keys and verdicts are schema values, not callable names.
    review_values = set(CHECKS) | set(REVIEW_AREAS) | set(TASK_REVIEW_TOOL_SCHEMA["properties"]["verdict"]["enum"])
    known = (tools | set(AGENT_LOCAL_TOOLS) | _PROHIBITED_LEGACY | _NOT_TOOLS | review_values
             | _unpacker_operation_names())

    unknown: dict[str, set[str]] = {}
    checked = 0
    for document in _skill_files():
        text = document.read_text(encoding="utf-8")
        for match in _CALL_RE.finditer(text):
            name = match.group(1)
            # Only names that look like Host tools; prose like `note(` is not one.
            if "_" not in name:
                continue
            checked += 1
            if name not in known:
                unknown.setdefault(name, set()).add(document.parent.name)
        for match in _TOKEN_RE.finditer(text):
            name = match.group(1)
            # A parameter name is snake_case too, and Skills quote plenty of them.
            if name in known or name in parameters:
                continue
            checked += 1
            unknown.setdefault(name, set()).add(document.parent.name)

    assert checked > 0, "the reference sweep found no tool calls"
    assert not unknown, "Skills name tools that do not exist: " + ", ".join(
        f"{name} (in {', '.join(sorted(where))})" for name, where in sorted(unknown.items()))


def test_no_skill_calls_an_effectful_tool_read_only() -> None:
    """A Skill that labels an effectful tool "read-only" sends the AI to a tool
    its connection does not have, at the ceiling where it says to use it."""

    effectful = set(TOOL_EFFECTS) - set(READ_ONLY_TOOLS)
    offenders: list[str] = []
    for document in _skill_files():
        for line in document.read_text(encoding="utf-8").splitlines():
            if "read-only" not in line.lower():
                continue
            for match in _CALL_RE.finditer(line):
                if match.group(1) in effectful:
                    offenders.append(f"{document.parent.name}: {line.strip()[:90]}")
    assert not offenders, "effectful tools described as read-only: " + "; ".join(offenders)
