"""Paths of the tools that travel with the program, and the tokens that name them.

A deployed qrenderdoc carries third-party tools under ``<app>/replaylab/tools/``;
a source checkout keeps the same layout under ``replaylab/third_party/``.
Registrations refer to that root as ``%BREAD_TOOLS%`` (and to the executable
directory as ``%BREAD_APP%``) so a copy of the application on another machine
resolves them where *it* lives.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

TOOLS_ROOT_TOKEN = "%BREAD_TOOLS%"
APP_ROOT_TOKEN = "%BREAD_APP%"


def app_root() -> Path:
    """Directory of the running Bread executables (frozen) or the ``replaylab`` tree."""

    override = os.environ.get("BREAD_APP_ROOT")
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def bundled_tools_root() -> Path:
    """Where bundled third-party tools live for this installation."""

    override = os.environ.get("BREAD_TOOLS_ROOT")
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "tools"
    return Path(__file__).resolve().parents[2] / "third_party"


def expand_tool_path(text: str) -> str:
    """Resolve the tokens for this machine."""

    value = str(text or "")
    if TOOLS_ROOT_TOKEN in value:
        value = value.replace(TOOLS_ROOT_TOKEN, str(bundled_tools_root()))
    if APP_ROOT_TOKEN in value:
        value = value.replace(APP_ROOT_TOKEN, str(app_root()))
    return value


def fold_tool_path(text: str) -> str:
    """The inverse of ``expand_tool_path`` for paths inside the bundle."""

    value = str(text or "")
    for token, root in ((TOOLS_ROOT_TOKEN, bundled_tools_root()), (APP_ROOT_TOKEN, app_root())):
        prefix = str(root)
        if value.startswith(prefix) and (len(value) == len(prefix) or value[len(prefix)] in "\\/"):
            return token + value[len(prefix):].replace("\\", "/")
    return value


__all__ = ["APP_ROOT_TOKEN", "TOOLS_ROOT_TOKEN", "app_root", "bundled_tools_root",
           "expand_tool_path", "fold_tool_path"]
