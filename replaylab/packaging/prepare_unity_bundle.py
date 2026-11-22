"""Build a pinned Unity MCP provider with an isolated, relocatable Python runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPLAYLAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPLAYLAB / "mcp_host"))
from src.unity_integration import ARCHIVE_SHA256, COMMIT, VERSION, UnityIntegrationService, _Job


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(root):
    manifest = json.loads((root / "bread-unity-provider.json").read_text(encoding="utf-8"))
    if manifest["commit"] != COMMIT or manifest["archiveSha256"] != ARCHIVE_SHA256:
        raise ValueError("Unity MCP bundle is from a different pinned provider")
    for record in manifest["files"]:
        path = (root / record["path"]).resolve()
        if root.resolve() not in path.parents or sha256(path) != record["sha256"]:
            raise ValueError(f"Unity MCP bundle hash mismatch: {record['path']}")
    return root


def prepare(output, cache):
    if output.exists():
        return verify(output)
    service = UnityIntegrationService(cache_root=cache)
    job = _Job("portable-package", "")
    # The user cache is only an optional verified archive seed. Never reuse a venv with
    # absolute launchers or mutate a running Unity installation.
    previous = Path(os.environ.get("LOCALAPPDATA", "")) / "ReplayLab/unity/downloads" / f"unity-mcp-{COMMIT}.zip"
    archive = cache / "downloads" / previous.name
    if previous.is_file() and sha256(previous) == ARCHIVE_SHA256 and not archive.exists():
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(previous, archive)
    source = service._source(job)
    executable = service._runtime(source, job)
    environment = executable.parent.parent
    python = environment / "Scripts/python.exe"
    information = json.loads(subprocess.check_output([str(python), "-I", "-c",
        "import sys,json;print(json.dumps({'base':sys.base_prefix,'major':sys.version_info.major,'minor':sys.version_info.minor}))"], text=True))
    if (information["major"], information["minor"]) != (3, 12):
        raise ValueError("The pinned Unity MCP portable runtime requires CPython 3.12")
    base = Path(information["base"])
    runtime = output / "runtime"
    output.mkdir(parents=True)
    try:
        ignore = shutil.ignore_patterns("__pycache__", "benchmarks", "tests", "test", "*.pyc", "*.pdb", "*.lib", "*.exp", "*.log")
        shutil.copytree(source / "MCPForUnity", output / "MCPForUnity", ignore=ignore)
        shutil.copyfile(source / "LICENSE", output / "LICENSE")
        runtime.mkdir()
        for name in ("python.exe", "python312.dll", "python3.dll", "LICENSE.txt"):
            shutil.copyfile(base / name, runtime / name)
        for path in base.glob("vcruntime*.dll"):
            shutil.copyfile(path, runtime / path.name)
        shutil.copytree(base / "DLLs", runtime / "DLLs", ignore=ignore)
        shutil.copytree(base / "Lib", runtime / "Lib", ignore=shutil.ignore_patterns("site-packages", "test", "tests", "idlelib", "tkinter", "ensurepip", "venv", "__pycache__", "*.pyc"))
        shutil.copytree(environment / "Lib/site-packages", runtime / "Lib/site-packages", ignore=ignore)
        # _pth disables registry/environment Python discovery, site.py and absolute .pth files.
        (runtime / "python312._pth").write_text(
            ".\nDLLs\nLib\nLib/site-packages\nLib/site-packages/win32\nLib/site-packages/win32/lib\nLib/site-packages/pywin32_system32\n", encoding="ascii")
        (runtime / "bread_unity_entry.py").write_text("import sys\nsys.dontwritebytecode = True\nfrom main import main\nif __name__ == '__main__':\n    main()\n", encoding="ascii")
        # Wheel install metadata may record the local build source. It is not runtime data.
        for path in runtime.rglob("direct_url.json"):
            path.unlink()
        for path in runtime.rglob("*.pth"):
            path.unlink()
        records = [{"path": p.relative_to(output).as_posix(), "sha256": sha256(p)}
                   for p in sorted(output.rglob("*")) if p.is_file()]
        (output / "bread-unity-provider.json").write_text(json.dumps({
            "schema": "bread-unity-provider-bundle/v1", "commit": COMMIT,
            "archiveSha256": ARCHIVE_SHA256, "version": VERSION, "python": "3.12", "files": records}, indent=2) + "\n", encoding="utf-8")
    except BaseException:
        # Keep failed output for diagnosis. An incomplete directory cannot pass verification.
        raise
    return verify(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.output.resolve(), args.cache.resolve()))
