"""Small run contracts shared by the API and vendor-native engines."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_protocol import (
    CHILD_EFFECTS,
    ChildResult,
    ClarificationRequest,
)
from .profiles import Profile


@dataclass(slots=True)
class OrchestratorConfig:
    """Host-owned limits; no planning or review state machine lives here."""

    main_timeout: float = 86_400.0
    allowed_effects: frozenset[str] = frozenset(CHILD_EFFECTS)
    event_root: Path | None = None
    log_root: Path | None = None
    workspace_base: Path | None = None
    heartbeat_seconds: float = 10.0
    # Skill roots this run may load Skills from (bundled root plus any the user added).
    skill_roots: tuple[Path, ...] = ()
    # Skills the user switched off in Settings. The roots are folders, so a task-scoped
    # process rebuilding the catalog from them would otherwise offer a Skill the user
    # disabled -- and load it when asked.
    disabled_skills: tuple[str, ...] = ()
    # External MCP servers the user enabled in Settings > AI. They are attached to the
    # vendor session and listed in the capability directory; the Host tool grant above
    # is unaffected by them.
    external_mcp_servers: tuple[dict, ...] = ()
    inherit_desktop_capabilities: bool = False
    desktop_capability_summary: dict = field(default_factory=dict)
    native_shell_enabled: bool = False
    declared_mcp_ids: tuple[str, ...] = ()
    disabled_skill_paths: tuple[str, ...] = ()


@dataclass(slots=True)
class RunRequest:
    prompt: str
    profile: Profile
    task_id: str
    model: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    instance_id: str = ""
    capture_path: str = ""
    # Stable qrenderdoc conversation lineage for related user turns and receipts.
    conversation_id: str = ""
    intent: dict[str, Any] | None = None
    skill_name: str = ""
    mcp_executable: Path | None = None


@dataclass(slots=True)
class RunResult:
    task_id: str
    profile_id: str
    executor: str
    text: str
    terminal_status: str
    clarification: ClarificationRequest | None = None
    children: list[ChildResult] = field(default_factory=list)
    error: str = ""
    core: str = "deepagents/v1"

    def payload(self) -> dict[str, Any]:
        return {
            "taskId": self.task_id,
            "profile": self.profile_id,
            "executor": self.executor,
            "engine": self.core,
            "text": self.text,
            "terminalStatus": self.terminal_status,
            "completionScope": "agent-turn",
            "acceptanceAuthority": "agent",
            "clarification": (
                self.clarification.event_payload() if self.clarification else None
            ),
            "children": [child.model_dump() for child in self.children],
            "error": self.error,
        }


__all__ = ["OrchestratorConfig", "RunRequest", "RunResult"]
