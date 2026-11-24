"""Task-scoped MCP declarations for vendor-native subscription agents."""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .mcp_runtime import find_replaylab_mcp
from .agent_context import ProviderContext, child_tool_grant, harden_codex_argv
from .agent_report import TASK_EVENT_ROOT_ENV, TASK_ID_ENV
from .policy import AGENT_LOCAL_TOOLS, CORE_DIRECT_TOOLS, ordered_tool_names
from .skill_gateway import load_validated_skill_catalog
from .tool_output import OWNED_OUTPUT_READER
from .owned_workspace import AGENT_WORKSPACE_ROOT_ENV, OwnedWorkspace
from .process_env import MCP_HOST_PROVIDER_ENV


_CODEX_TASK_MCP_STARTUP_TIMEOUT_SECONDS = 60
_CODEX_TASK_MCP_TOOL_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class NativeMCPRoute:
    command: str
    args: tuple[str, ...]
    env: dict[str, str]
    allowed_tools: frozenset[str]
    effects: frozenset[str]
    registry_path: Path
    # Direct definitions inserted into the vendor context.  ``allowed_tools`` is
    # the larger server-side Host dispatch grant and is never sent as schemas.
    visible_tools: frozenset[str] = frozenset()

    def claude_json(self) -> dict[str, object]:
        return {
            "type": "stdio",
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
        }

    def codex_config_override(self) -> str:
        command = _toml_string(self.command)
        args = ", ".join(_toml_string(value) for value in self.args)
        environment = ", ".join(
            f"{name} = {_toml_string(value)}" for name, value in sorted(self.env.items())
        )
        # Codex treats unannotated or effectful MCP tools as approval-requiring.
        # This server is not the unrestricted ReplayLab Host: it is a fresh
        # per-task filter with an exact allow-list, effect ceiling, frozen UI
        # scope. The initiating ReplayLab intent is therefore
        # the approval boundary, while the filter remains the enforcement
        # boundary.  Keep this trust override scoped to this one inline server.
        return (
            "mcp_servers.Bread={ command = "
            + command
            + ", args = ["
            + args
            + "], env = {"
            + environment
            + "}, required = true, startup_timeout_sec = "
            + str(_CODEX_TASK_MCP_STARTUP_TIMEOUT_SECONDS)
            + ", tool_timeout_sec = "
            + str(_CODEX_TASK_MCP_TOOL_TIMEOUT_SECONDS)
            + ', default_tools_approval_mode = "approve" }'
        )

    def compact_codex_config_override(self) -> str:
        """Keep task configuration and credentials outside Windows shell command lines."""
        if self.args[:3] != ("-I", "-m", "replaylab_agent.mcp_proxy"):
            raise ValueError("A compact Codex route requires the first-party MCP proxy")
        path = self.registry_path.parent / ("codex-mcp-route-" + uuid.uuid4().hex + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"schema": "bread-mcp-route/v1", "arguments": list(self.args[3:]),
                       "environment": self.env}, stream, ensure_ascii=False)
        # The private turn root owns the file; ordinary task teardown removes it.
        compact = NativeMCPRoute(self.command,
            ("-I", "-m", "replaylab_agent.mcp_proxy", "--route-file", str(path)),
            {}, self.allowed_tools, self.effects, self.registry_path, self.visible_tools)
        return compact.codex_config_override()


def build_native_mcp_route(
    context: ProviderContext,
    *,
    workspace: Path,
    event_root: Path,
    allowed_effects: Sequence[str],
    skill_roots: Sequence[Path | str] = (),
    disabled_skills: Sequence[str] = (),
    mcp_servers: Sequence[Mapping[str, object]] = (),
    full_host_catalog: bool = False,
    editor_python_enabled: bool = False,
) -> NativeMCPRoute | None:
    """Build the shared Skill/Host/external route without requiring a capture."""

    roots = tuple(skill_roots)
    servers = [dict(server) for server in mcp_servers if isinstance(server, Mapping)]
    # Every task can record its Agent completion review, including ordinary
    # questions without a capture, Skill root, or external integration.
    effects = frozenset(str(value).strip() for value in allowed_effects if str(value).strip())
    if roots:
        # Use the same available-Skill view as the proxy. Missing optional
        # roots remain diagnostics rather than preventing an AI conversation.
        load_validated_skill_catalog(roots, disabled_skills=disabled_skills)
    if "replay" in effects:
        effects = effects | {"workspace"}
    # The Agent sees common typed Host tools directly and may discover less
    # common operations without a mandatory schema-loading round trip.
    allowed_tools = (child_tool_grant(tuple(effects)) if context.instance_id else frozenset()) | AGENT_LOCAL_TOOLS
    allowed_tools = allowed_tools | {OWNED_OUTPUT_READER}
    if not context.instance_id:
        effects = frozenset()
    visible_tools = (
        allowed_tools if full_host_catalog else
        (AGENT_LOCAL_TOOLS | {OWNED_OUTPUT_READER} | (set(CORE_DIRECT_TOOLS) & allowed_tools))
    )
    owned_root = OwnedWorkspace(workspace.resolve()).root
    registry = workspace.parent / f"live-edit-registry-{uuid.uuid4().hex}.json"
    arguments: list[str] = [
        "-I",
        "-m",
        "replaylab_agent.mcp_proxy",
        "--scope-json",
        context.scope().to_json(),
    ]
    if context.instance_id:
        host = find_replaylab_mcp(context.mcp_executable)
        arguments.extend(("--host", str(host), "--host-arg", "stdio",
                          "--instance-id", context.instance_id, "--registry-path", str(registry)))
    if full_host_catalog:
        # API mode builds the compact model-facing surface in its own process.
        # Its private proxy connection therefore needs every Host definition.
        arguments.append("--full-host-catalog")
    for name in ordered_tool_names(allowed_tools):
        arguments.extend(("--allow", name))
    for effect in sorted(effects):
        arguments.extend(("--effect", effect))
    for skill_id in disabled_skills:
        text = str(skill_id or "").strip()
        if text:
            arguments.extend(("--disabled-skill", text))
    for root in roots:
        text = str(root or "").strip()
        if text:
            arguments.extend(("--skill-root", text))
    if servers:
        # Keep runtime credentials out of argv, directory metadata and the
        # vendor's readable workspace. This sibling file belongs to the fresh
        # turn root and is removed by the ordinary turn cleanup.
        server_file = workspace.parent / f"mcp-servers-{uuid.uuid4().hex}.json"
        descriptor = os.open(str(server_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(servers[:32], stream, ensure_ascii=False)
        arguments.extend(("--mcp-server-file", str(server_file)))
    # Vendor processes intentionally drop Host-only configuration. Carry it in
    # this task's MCP declaration instead: otherwise the nested Host talks to
    # the right capture but falls back to another window's global project.
    environment = ({key: value for key, value in os.environ.items()
                    if key.upper() in MCP_HOST_PROVIDER_ENV} if context.instance_id else {})
    if context.instance_id:
        environment["REPLAYLAB_INSTANCE_ID"] = context.instance_id
        environment["BREAD_UNREAL_EDITOR_PYTHON"] = "1" if editor_python_enabled and "project" in effects else "0"
    environment.update({
        TASK_ID_ENV: context.task_id,
        TASK_EVENT_ROOT_ENV: str(event_root),
        AGENT_WORKSPACE_ROOT_ENV: str(owned_root),
    })
    if context.conversation_id:
        environment["REPLAYLAB_CONVERSATION_ID"] = context.conversation_id
    return NativeMCPRoute(
        command=sys.executable,
        args=tuple(arguments),
        env=environment,
        allowed_tools=frozenset(allowed_tools),
        effects=effects,
        registry_path=registry,
        visible_tools=frozenset(visible_tools),
    )


def native_codex_argv(argv: Sequence[str], route: NativeMCPRoute | None, *,
                      inherit_desktop_capabilities: bool = False,
                      config_overrides: Sequence[str] = ()) -> list[str]:
    hardened = harden_codex_argv(argv, inherit_desktop_capabilities=inherit_desktop_capabilities,
                                 config_overrides=config_overrides)
    if route is None:
        return hardened
    try:
        index = hardened.index("app-server")
    except ValueError:  # harden_codex_argv already reports this; defensive only.
        return hardened
    return [
        *hardened[:index],
        "-c",
        route.compact_codex_config_override(),
        *hardened[index:],
    ]


def _toml_string(value: str) -> str:
    # JSON strings are valid TOML basic strings for the characters used in an
    # argv/environment declaration and correctly escape Windows backslashes.
    return json.dumps(str(value), ensure_ascii=False)


__all__ = ["NativeMCPRoute", "build_native_mcp_route", "native_codex_argv"]
