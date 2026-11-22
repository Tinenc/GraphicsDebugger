"""Small deterministic limits shared by both Agent engines."""

from __future__ import annotations

import json
import os
from pathlib import Path
from .agent_contracts import OrchestratorConfig
from .agent_protocol import CHILD_EFFECTS


def default_engine_config_path() -> Path:
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    # Never the shared temp directory: on Linux/macOS that put the task event
    # stream, the log and the tool settings under a world-readable /tmp that any
    # local user could pre-plant and that vanished on reboot. XDG data home is the
    # per-user equivalent of LOCALAPPDATA, which the rest of the product uses.
    root = Path(local).expanduser() if local else Path.home() / ".local" / "share"
    return root / "ReplayLab" / "config" / "agent-engine.json"


def load_engine_config(path: Path | str | None = None) -> OrchestratorConfig:
    """Load bounded operational limits; unknown/legacy switches are ignored."""

    config = OrchestratorConfig()
    source = Path(path) if path is not None else default_engine_config_path()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except (OSError, ValueError):
        return config
    if not isinstance(payload, dict):
        return config
    float_limits: dict[str, tuple[float, float]] = {
        "main_timeout": (1.0, 86_400.0),
        "heartbeat_seconds": (0.05, 300.0),
    }
    for name, (minimum, maximum) in float_limits.items():
        value = payload.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            setattr(config, name, min(maximum, max(minimum, float(value))))
    # Historical max_tool_calls values remain readable in existing JSON files,
    # but no longer constrain either Agent route or the MCP filter.
    effects = payload.get("allowed_effects")
    if isinstance(effects, list):
        selected = frozenset(str(item) for item in effects)
        if selected.issubset(CHILD_EFFECTS):
            config.allowed_effects = selected
    return config


__all__ = ["default_engine_config_path", "load_engine_config"]
