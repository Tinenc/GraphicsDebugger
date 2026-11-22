"""Verify the packaged Agent framework against the project's exact pin."""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
import re
import sys
import tomllib

from .errors import ConfigurationError


def _runtime_pyproject() -> Path:
    packaged = Path(__file__).with_name("pyproject.toml")
    if packaged.is_file() or getattr(sys, "frozen", False):
        return packaged
    return Path(__file__).resolve().parents[2] / "pyproject.toml"


def verify_deepagents_version(pyproject: Path | None = None) -> str:
    """Require installed distribution metadata to match the one pyproject pin."""

    path = pyproject if pyproject is not None else _runtime_pyproject()
    try:
        with path.open("rb") as stream:
            project = tomllib.load(stream).get("project", {})
    except (OSError, ValueError) as exc:
        raise ConfigurationError("cannot read the packaged runtime dependency manifest") from exc
    dependencies = project.get("dependencies", []) if isinstance(project, dict) else []
    declarations = [
        item for item in dependencies
        if isinstance(item, str) and re.match(r"^deepagents\b", item.strip(), re.IGNORECASE)
    ] if isinstance(dependencies, list) else []
    pin = (
        re.fullmatch(r"deepagents\s*==\s*([A-Za-z0-9_.+!-]+)", declarations[0].strip(), re.IGNORECASE)
        if len(declarations) == 1 else None
    )
    if pin is None:
        raise ConfigurationError("runtime dependency manifest must declare one exact deepagents==version pin")
    expected = pin.group(1)
    try:
        installed = importlib.metadata.version("deepagents")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ConfigurationError("required deepagents distribution metadata is missing") from exc
    if installed != expected:
        raise ConfigurationError(f"runtime requires deepagents=={expected}; found {installed}")
    return installed
