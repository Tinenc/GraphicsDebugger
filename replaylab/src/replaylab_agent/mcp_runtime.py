"""Resolution helpers for the co-packaged Bread MCP Host."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .errors import ConfigurationError

_MCP_EXECUTABLE_NAMES = (
    "BreadMCP.unicode.exe",
    "BreadMCP.exe",
)
_MCP_PATH_NAMES = ("bread-mcp",)


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def find_bread_mcp(explicit: Path | str | None = None) -> Path:
    """Resolve the existing MCP Host without starting it."""

    candidates: list[str] = []
    if explicit:
        candidates.append(str(explicit))
    for env_name in ("BREAD_MCP_HOST", "REPLAYLAB_MCP_HOST"):
        configured = os.environ.get(env_name, "").strip()
        if configured:
            candidates.append(configured)
    development_host = repository_root() / "x64" / "Development" / "replaylab"
    candidates.extend(str(development_host / name) for name in _MCP_EXECUTABLE_NAMES)
    on_path = None
    for name in (*_MCP_EXECUTABLE_NAMES, *_MCP_PATH_NAMES):
        on_path = shutil.which(name)
        if on_path:
            break
    if on_path:
        candidates.append(on_path)
    checked: list[str] = []
    for candidate in candidates:
        path = Path(candidate).expanduser()
        checked.append(str(path))
        if path.is_file() and not path.is_symlink():
            return path.resolve()
    raise ConfigurationError(
        "Bread MCP host (BreadMCP) was not found; set BREAD_MCP_HOST. Checked: "
        + ", ".join(checked)
    )


# Compatibility alias for call sites that still import the historic helper name.
find_replaylab_mcp = find_bread_mcp


__all__ = ["find_bread_mcp", "find_replaylab_mcp", "repository_root"]
