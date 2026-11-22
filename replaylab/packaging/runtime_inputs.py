"""Stable source identity for the frozen Bread hosts and their embedded skills/data."""
from __future__ import annotations
import hashlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def fingerprint():
    paths = []
    for relative in ("replaylab/src", "replaylab/mcp_host/src", "replaylab/unity", ".agents/skills", "replaylab/portable_skills"):
        paths.extend(p for p in (REPO / relative).rglob("*") if p.is_file() and
                     "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo"))
    paths.extend((REPO / "replaylab/packaging").glob("*_entry.py"))
    paths.extend(REPO / p for p in ("replaylab/pyproject.toml", "replaylab/mcp_host/pyproject.toml",
                                   "replaylab/packaging/Build-ReplayLabRuntime.ps1",
                                   "replaylab/packaging/host_runtime.py",
                                   "replaylab/packaging/version_policy.py",
                                   "replaylab/packaging/RenderDocBread.package.json",
                                   "renderdoc/api/replay/version.h",
                                   "replaylab/dependencies/build-requirements.lock"))
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(REPO).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


if __name__ == "__main__":
    print(fingerprint())
