"""Launch official npm Codex installations without cmd.exe's expansion/length limits."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Mapping, Sequence

from .errors import ConfigurationError
from .process_diagnostics import utf16_units
from .process_env import resolve_executable, resolve_known_vendor_executable


def _ordinary(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        current = path
        while current != root.parent:
            details = current.lstat()
            if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400:
                return False
            current = current.parent
        return path.is_file()
    except (OSError, ValueError):
        return False


def _npm_codex(shim: Path, environment: Mapping[str, str]) -> list[str]:
    root = shim.parent
    package = root / "node_modules/@openai/codex"
    metadata = package / "package.json"
    try:
        if not _ordinary(metadata, root) or metadata.stat().st_size > 128 * 1024:
            return []
        info = json.loads(metadata.read_text(encoding="utf-8"))
        if info.get("name") != "@openai/codex":
            return []
        # Only bypass the generated npm wrapper which points to this exact package.
        if shim.stat().st_size > 64 * 1024:
            return []
        wrapper = shim.read_bytes().decode("utf-8-sig", errors="replace").replace("\\", "/")
        if "node_modules/@openai/codex/bin/codex.js" not in wrapper:
            return []
        machine = environment.get("PROCESSOR_ARCHITECTURE", "").lower()
        arm = machine == "arm64"
        triple = "aarch64-pc-windows-msvc" if arm else "x86_64-pc-windows-msvc"
        target = "codex-win32-arm64" if arm else "codex-win32-x64"
        candidates = [package / "vendor" / triple / "codex/codex.exe",
                      package / "node_modules/@openai" / target / "vendor" / triple / "codex/codex.exe",
                      root / "node_modules/@openai" / target / "vendor" / triple / "codex/codex.exe"]
        for candidate in candidates:
            if _ordinary(candidate, root) and 2 <= candidate.stat().st_size <= 512 * 1024 * 1024:
                with candidate.open("rb") as stream:
                    if stream.read(2) == b"MZ":
                        return [str(candidate)]
        entry = package / "bin/codex.js"
        node = root / "node.exe"
        if not _ordinary(node, root):
            resolved = resolve_executable("node.exe", environ=environment)
            node = Path(resolved) if resolved else node
        if _ordinary(entry, root) and node.is_absolute() and _ordinary(node, node.parent) and node.suffix.lower() == ".exe":
            return [str(node), str(entry)]
    except (OSError, ValueError, TypeError):
        pass
    return []


def prepare_codex_launch(command: Sequence[str], environment: Mapping[str, str]) -> tuple[tuple[str, ...], dict[str, str]]:
    values = tuple(command)
    env = dict(environment)
    if os.name != "nt":
        return values, env
    # Windows names are case-insensitive; avoid duplicate Path/PATH entries.
    env = {key.upper(): value for key, value in env.items()}
    name = Path(values[0]).name.lower()
    if name in {"codex", "codex.exe", "codex.cmd", "codex.bat"}:
        executable = values[0]
        if not Path(executable).is_absolute():
            executable = resolve_known_vendor_executable("codex", executable, environ=env)
            if not executable:
                raise ConfigurationError("The Codex executable could not be resolved from the approved installation or PATH")
        values = (executable, *values[1:])
        if Path(executable).suffix.lower() in {".cmd", ".bat"}:
            direct = _npm_codex(Path(executable), env)
            if not direct:
                raise ConfigurationError("The Codex batch launcher could not be resolved to the official native executable or Node entry point. Select an official codex.exe installation")
            values = (*direct, *values[1:])
    if utf16_units(subprocess.list2cmdline(values)) + 1 > 32767:
        raise ConfigurationError("The Codex command exceeds the Windows native 32767-character limit; keep task configuration in the private route file")
    for name, value in env.items():
        if "\0" in name or "=" in name or "\0" in value:
            raise ConfigurationError("The Codex process environment contains an invalid name or NUL")
        if utf16_units(name + "=" + value) > 32767:
            raise ConfigurationError(f"The Codex environment entry {name[:128]} exceeds the Windows per-variable limit")
    return values, env
