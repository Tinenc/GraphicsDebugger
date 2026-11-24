"""Task-local process context and deterministic tool grants."""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ConfigurationError
from .events import default_task_event_root
from .policy import (
    EFFECTFUL_TOOLS,
    READ_ONLY_TOOLS,
    tool_admissible_at_effect_ceiling,
)
from .process_env import external_agent_environment, resolved_agent_argv
from .profiles import Profile
from .tool_scope import ToolCallScope


@dataclass(slots=True)
class ProviderContext:
    profile: Profile
    model: str
    instance_id: str
    context: dict[str, Any]
    intent: dict[str, Any] | None
    mcp_executable: Path | None = None
    workspace_base: Path | None = None
    log_root: Path | None = None
    main_timeout: float = 86_400.0
    task_id: str = ""
    conversation_id: str = ""
    inherit_desktop_capabilities: bool = False

    @property
    def protocol(self) -> str:
        return str(self.profile.provider.protocol)

    def workspace_root(self) -> Path:
        base = self.workspace_base or (
            default_task_event_root().parent / "external-agent-workspace"
        )
        base.mkdir(parents=True, exist_ok=True)
        if base.is_symlink():
            raise ConfigurationError("the external Agent workspace root cannot be a link")
        return base.resolve()

    def new_turn_workspace(self) -> tuple[Path, Path]:
        root = self.workspace_root()
        turn_root = root / f"native-{uuid.uuid4().hex}"
        turn_root.mkdir(mode=0o700)
        workspace = turn_root / (
            "cursor-workspace" if self.profile.preset == "cursor-acp" else "agent-workspace"
        )
        workspace.mkdir(mode=0o700)
        from .conversation_cleanup import record_workspace_owner

        record_workspace_owner(turn_root, self.task_id, self.conversation_id)
        if self.profile.preset == "cursor-acp" and not self.inherit_desktop_capabilities:
            from .process_env import ensure_cursor_profile_config, ensure_cursor_workspace

            ensure_cursor_profile_config(self.profile)
            ensure_cursor_workspace(workspace)
        return workspace, turn_root

    @staticmethod
    def cleanup_turn_workspace(turn_root: Path | None) -> dict | None:
        if turn_root is None:
            return None
        from .agent_diagnostics import get_logger, public_error
        from .shader_draft_recovery import recover_shader_drafts, retain_owned_work_outputs, validated_turn_root
        recovery = None
        try:
            if not turn_root.exists():
                return None
            resolved = validated_turn_root(turn_root)
            retained = retain_owned_work_outputs(resolved)
            if retained is not None:
                get_logger().info("Agent task outputs retained: source=%s count=%s reason=%s",
                    retained["sourceDirectory"], retained["outputCount"], retained["reason"])
                return retained
            recovery = recover_shader_drafts(resolved)
            if recovery is not None:
                get_logger().warning("Shader draft recovery: status=%s manifest=%s source=%s reason=%s",
                    recovery["status"], recovery["manifestPath"], recovery["sourceDirectory"], recovery["reason"])
                if recovery["sourceRetained"]:
                    return recovery
                # A provider can finish a report while Shader copies are being
                # archived. Do not delete that newly delivered ordinary output.
                retained = retain_owned_work_outputs(resolved)
                if retained is not None:
                    retained["shaderRecovery"] = recovery
                    return retained
            # Validate the exact absolute directory again immediately before
            # recursive deletion; a redirected root must never be removed.
            shutil.rmtree(validated_turn_root(resolved))
            return recovery
        except Exception as error:
            get_logger().warning("Agent turn cleanup retained %s: %s", turn_root, public_error(error))
            if recovery is not None:
                # Archiving may have succeeded before deletion hit a busy
                # provider file. Keep its usable locator in the final receipt.
                return {**recovery, "sourceRetained": True, "reason": public_error(error)}
            return {"schema": "replaylab-shader-draft-recovery/v1", "status": "source-retained",
                    "sourceRetained": True, "sourceDirectory": str(turn_root), "manifestPath": "",
                    "recoveryDirectory": "", "files": [], "reason": public_error(error)}

    def agent_argv(self) -> list[str]:
        return resolved_agent_argv(self.profile)

    def agent_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        # `self.model` is the run's model: the panel's per-request pick when there was
        # one, otherwise the profile's own.
        return external_agent_environment(
            self.profile, dict(extra or {}), model=str(self.model or ""))

    def scope(self) -> ToolCallScope:
        return ToolCallScope.from_request(self.context, self.intent)


def normalized_effects(effects: Sequence[str]) -> frozenset[str]:
    granted = {str(item).strip() for item in effects if str(item).strip()}
    if "replay" in granted:
        granted.add("workspace")
    return frozenset(granted)


def child_tool_grant(effects: Sequence[str]) -> frozenset[str]:
    """Expose Host capabilities permitted by this task's effect ceiling."""

    granted = normalized_effects(effects)
    allowed = set(READ_ONLY_TOOLS)
    for name in EFFECTFUL_TOOLS:
        if tool_admissible_at_effect_ceiling(name, granted):
            allowed.add(name)
    return frozenset(allowed)


_CODEX_FORBIDDEN_ARGS = frozenset(
    {
        "-c",
        "--config",
        "--enable",
        "--disable",
        "--yolo",
        "--dangerously-bypass-approvals-and-sandbox",
    }
)
_CODEX_CONFIG_OVERRIDES = (
    'model_provider="replaylab_openai_subscription"',
    'model_providers.replaylab_openai_subscription.name="Bread OpenAI subscription"',
    "model_providers.replaylab_openai_subscription.requires_openai_auth=true",
    "model_providers.replaylab_openai_subscription.supports_websockets=false",
    'model_providers.replaylab_openai_subscription.wire_api="responses"',
    "model_providers.replaylab_openai_subscription.request_max_retries=2",
    "model_providers.replaylab_openai_subscription.stream_max_retries=2",
    "model_providers.replaylab_openai_subscription.stream_idle_timeout_ms=90000",
    "features.apps=false",
    "features.plugins=false",
    "features.remote_plugin=false",
    "features.recommended_plugins=false",
    "features.browser_use=false",
    "features.browser_use_external=false",
    "features.computer_use=false",
    "features.in_app_browser=false",
    "features.in_app_chat=false",
    "features.in_app_dictation=false",
    "features.in_app_local_automation=false",
    "features.in_app_updates=false",
    "features.image_generation=false",
    "features.shell_snapshot=false",
    "features.skill_search=false",
    "features.tool_suggest=false",
    "features.workspace_dependencies=false",
    "features.unbounded_connection_retries=false",
    "apps._default.enabled=false",
    # The connector verifies :read-only plus the exact per-turn workspace roots
    # before the model starts. Native commands may inspect that workspace, while
    # Shader/project/replay writes continue through the scoped Bread MCP route.
    "features.shell_tool=true",
    "features.unified_exec=true",
    "features.skill_mcp_dependency_install=false",
    "features.hooks=false",
    # `agents.enabled` is parsed as a role table in Codex 0.142.1.
    # Disable collaboration through its feature gate without creating a role.
    "features.multi_agent=false",
    "features.goals=false",
    "features.memories=false",
    "project_doc_max_bytes=0",
    "project_doc_fallback_filenames=[]",
    "tools.view_image=true",
    'web_search="disabled"',
    'history.persistence="none"',
    "allow_login_shell=false",
)


def harden_codex_argv(argv: Sequence[str], *, inherit_desktop_capabilities: bool = False,
                      config_overrides: Sequence[str] = ()) -> list[str]:
    """Scope Codex to a read-only workspace before adding the Bread MCP route."""

    values = list(argv)
    if any(value in _CODEX_FORBIDDEN_ARGS for value in values[1:]):
        raise ConfigurationError(
            "Codex profile command may not override Bread's security configuration"
        )
    try:
        index = values.index("app-server")
    except ValueError as exc:
        raise ConfigurationError(
            "Codex profile command must contain the app-server subcommand"
        ) from exc
    hardened = values[:index]
    overrides = _CODEX_CONFIG_OVERRIDES
    if inherit_desktop_capabilities:
        # Keep transport reliability/history settings. Do not negate the user's
        # installed plugins, web/browser/computer tools, skills or permissions.
        overrides = tuple(value for value in overrides if value.startswith((
            "model_provider", "model_providers.", "history.persistence=",
            "features.unbounded_connection_retries=")))
    for value in (*overrides, *config_overrides):
        hardened.extend(("-c", value))
    hardened.extend(values[index:])
    return hardened


__all__ = [
    "ProviderContext",
    "child_tool_grant",
    "harden_codex_argv",
    "normalized_effects",
]
