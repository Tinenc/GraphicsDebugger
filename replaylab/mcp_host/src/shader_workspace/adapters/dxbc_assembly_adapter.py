"""Fail-closed 3DMigoto DXBC assembly round-trip adapter.

The 3DMigoto 1.3.16 ``cmd_Decompiler`` command writes files beside its
input: ``-d`` writes ``.asm`` and ``-a`` writes ``.shdr``.  ReplayLab jobs,
on the other hand, require an exact, new output path.  This adapter bridges
those interfaces without a command shell and binds assembly to the immutable
captured DXBC used for reflection/signature copying.

This is an executable assembly lane, not HLSL reconstruction.  Successful
assembly only creates a candidate; RenderDoc replay reflection and the normal
ABI gate remain mandatory before replacement.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import BinaryIO, Sequence


_COPY_CHUNK = 1024 * 1024


def _sha256_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(_COPY_CHUNK)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return _sha256_stream(stream)


def _expected_sha256(value: str, label: str) -> str:
    digest = str(value).strip().casefold()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise RuntimeError("%s must be a lowercase SHA-256 digest" % label)
    return digest


def _regular_file(raw: str, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeError("%s must be absolute" % label)
    if path.is_symlink():
        raise RuntimeError("%s must be a regular non-symlink file" % label)
    path = path.resolve(strict=True)
    if not path.is_file():
        raise RuntimeError("%s must be a regular non-symlink file" % label)
    return path


def _new_output(raw: str, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeError("%s must be absolute" % label)
    if path.exists() or path.is_symlink():
        raise RuntimeError("refusing to overwrite %s" % label)
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise RuntimeError("%s parent must be a regular directory" % label)
    resolved_parent = parent.resolve(strict=True)
    if resolved_parent != parent:
        raise RuntimeError("%s parent must not be redirected" % label)
    return resolved_parent / path.name


def _copy_new(source: Path, destination: Path) -> None:
    """Copy to an exclusively-created file; never replace a raced target."""

    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, length=_COPY_CHUNK)
        output_stream.flush()
        os.fsync(output_stream.fileno())


def _snapshot_baseline(
    baseline: Path, destination: Path, expected_sha256: str
) -> Path:
    """Create one job-local, hash-bound reflection baseline snapshot."""

    before = baseline.stat(follow_symlinks=False)
    _copy_new(baseline, destination)
    after = baseline.stat(follow_symlinks=False)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise RuntimeError("baseline changed while its reflection snapshot was copied")
    if _sha256_file(destination) != expected_sha256:
        raise RuntimeError("baseline SHA-256 differs from the captured shader binding")
    return destination


def _publish_derived(derived: Path, output: Path) -> None:
    if derived.is_symlink() or not derived.is_file() or derived.stat().st_size <= 0:
        raise RuntimeError("CMD_Decompiler did not produce its expected derived output")
    _copy_new(derived, output)
    derived.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("disassemble", "assemble"), required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--tool-sha256", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--baseline-sha256", required=True)
    arguments = parser.parse_args(argv)

    tool = _regular_file(arguments.tool, "CMD_Decompiler")
    source = _regular_file(arguments.input, "input")
    output = _new_output(arguments.output, "adapter output")
    expected_tool_hash = _expected_sha256(arguments.tool_sha256, "tool-sha256")
    expected_baseline_hash = _expected_sha256(
        arguments.baseline_sha256, "baseline-sha256"
    )
    if _sha256_file(tool) != expected_tool_hash:
        raise RuntimeError("CMD_Decompiler SHA-256 differs from the pinned tool binding")
    if source.parent != output.parent:
        raise RuntimeError("input and output must belong to the same isolated job directory")

    if arguments.mode == "disassemble":
        if arguments.baseline is not None:
            raise RuntimeError("disassemble must not accept a separate baseline")
        if _sha256_file(source) != expected_baseline_hash:
            raise RuntimeError("disassembly input differs from the captured baseline")
        derived = source.with_suffix(".asm")
        command = [
            str(tool),
            "-d",
            "-V",
            "--stop-on-failure",
            str(source),
        ]
    else:
        if not arguments.baseline:
            raise RuntimeError("assemble requires the captured reflection baseline")
        baseline = _regular_file(arguments.baseline, "baseline")
        if baseline == source or baseline == output:
            raise RuntimeError("baseline must be distinct from assembly input/output")
        snapshot = _snapshot_baseline(
            baseline,
            source.parent / "reflection-baseline.dxbc",
            expected_baseline_hash,
        )
        derived = source.with_suffix(".shdr")
        command = [
            str(tool),
            "-a",
            "--copy-reflection",
            str(snapshot),
            "--stop-on-failure",
            str(source),
        ]

    if derived == output or derived.exists() or derived.is_symlink():
        raise RuntimeError("refusing to overwrite CMD_Decompiler derived output")
    completed = subprocess.run(
        command,
        cwd=str(source.parent),
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
        shell=False,
        env=dict(os.environ),
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        return int(completed.returncode or 1)
    if _sha256_file(tool) != expected_tool_hash:
        raise RuntimeError("CMD_Decompiler changed while the adapter was running")
    _publish_derived(derived, output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("adapter error: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        raise SystemExit(1)
