"""Create a Windows CPython 3.12 build environment from sealed local wheels."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import venv

REPO = Path(__file__).resolve().parents[2]


def run(*command):
    subprocess.run([str(p) for p in command], check=True)


def bootstrap(output: Path, wheels: Path) -> Path:
    if sys.version_info[:2] != (3, 12) or sys.maxsize <= 2**32 or sys.platform != "win32":
        raise ValueError("The Windows release builder needs 64-bit CPython 3.12")
    marker = output / ".bread-build-environment.json"
    inputs = [REPO / p for p in ("replaylab/dependencies/build-requirements.lock",
        "replaylab/dependencies/inputs.lock.json", "replaylab/pyproject.toml", "replaylab/mcp_host/pyproject.toml")]
    identity = hashlib.sha256(b"".join(p.read_bytes() for p in inputs)).hexdigest()
    if output.exists() and any(output.iterdir()) and not marker.is_file():
        raise ValueError("Use an empty build environment directory; existing user environments are preserved")
    output.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"schema": "bread-build-environment/v1", "state": "preparing"}) + "\n", encoding="utf-8")
    python = output / "Scripts/python.exe"
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(output)
    run(python, "-m", "pip", "install", "--disable-pip-version-check", "--no-index",
        "--only-binary=:all:", "--find-links", wheels, "--require-hashes", "-r", inputs[0])
    run(python, "-m", "pip", "install", "--disable-pip-version-check", "--no-index",
        "--no-deps", "--no-build-isolation", "-e", REPO / "replaylab", "-e", REPO / "replaylab/mcp_host")
    run(python, "-m", "pip", "check")
    marker.write_text(json.dumps({"schema": "bread-build-environment/v1", "state": "ready",
        "inputsSha256": identity, "pythonVersion": sys.version.split()[0]}, indent=2) + "\n", encoding="utf-8")
    return python


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wheels", type=Path, required=True)
    args = parser.parse_args()
    print(bootstrap(args.output.resolve(), args.wheels.resolve()))
