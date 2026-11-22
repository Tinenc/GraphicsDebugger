"""Fixed file-output adapter for 3DMigoto CMD_Decompiler.

CMD_Decompiler derives ``.hlsl`` beside its input instead of accepting an
output path.  The shader tool registry requires every operation to name its
exact job output, so this small adapter bridges that interface without a shell.
It is intended to be SHA-256 pinned together with CMD_Decompiler.exe.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
import subprocess
import sys
from typing import Sequence


def bind_entry_point(source: str, entry: str) -> str:
    """3DMigoto emits main even when the captured shader has a named entry."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", entry):
        raise ValueError("entry point must be an HLSL identifier")
    if entry == "main":
        return source
    pattern = r"(?m)^([ \t]*void[ \t]+)main([ \t]*\()"
    if len(re.findall(pattern, source)) != 1:
        raise ValueError("expected exactly one 3DMigoto main entry declaration")
    if re.search(r"\b" + re.escape(entry) + r"\b", source):
        raise ValueError("captured entry point collides with a reconstructed identifier")
    return re.sub(pattern, lambda match: match[1] + entry + match[2], source)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--entry-point", default="main")
    arguments = parser.parse_args(argv)
    tool = Path(arguments.tool).resolve(strict=True)
    source = Path(arguments.input).resolve(strict=True)
    output = Path(arguments.output).resolve()
    if tool.is_symlink() or not tool.is_file():
        raise RuntimeError("CMD_Decompiler must be a regular non-symlink file")
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("input must be a regular non-symlink file")
    if output.exists() or output.is_symlink():
        raise RuntimeError("refusing to overwrite adapter output")
    derived = source.with_suffix(".hlsl")
    if derived.exists() or derived.is_symlink():
        raise RuntimeError("refusing to overwrite CMD_Decompiler derived output")
    completed = subprocess.run(
        [str(tool), "-D", "-V", "--stop-on-failure", str(source)],
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
    if derived.is_symlink() or not derived.is_file() or derived.stat().st_size <= 0:
        raise RuntimeError("CMD_Decompiler did not produce its expected .hlsl file")
    if arguments.entry_point != "main":
        source_text = derived.read_text(encoding="utf-8-sig")
        derived.write_text(bind_entry_point(source_text, arguments.entry_point), encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    os.replace(derived, output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("adapter error: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        raise SystemExit(1)
