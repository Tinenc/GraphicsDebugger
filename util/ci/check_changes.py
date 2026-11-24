"""Check introduced commits and changed C/C++ lines without rewriting history."""
from __future__ import annotations

import argparse
import difflib
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
EXTENSIONS = {".h", ".c", ".cpp", ".m", ".mm", ".inl", ".geom", ".frag", ".vert", ".comp", ".hlsl"}


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8")


def changed_ranges(before: str, after: str) -> list[tuple[int, int]]:
    return [(start + 1, end) for kind, _, _, start, end in
            difflib.SequenceMatcher(a=before.splitlines(), b=after.splitlines(), autojunk=False).get_opcodes()
            if kind != "equal" and end > start]


def relevant_format_changes(source: str, formatted: str, ranges: list[tuple[int, int]]):
    # clang-format expands ranges to enclosing statements. Gate only differences touching
    # introduced lines so an existing surrounding style debt does not invalidate a patch.
    return [(first, last, start, end) for kind, first, last, start, end in
            difflib.SequenceMatcher(a=source.splitlines(), b=formatted.splitlines(), autojunk=False).get_opcodes()
            if kind != "equal" and any(first < high and max(last, first + 1) >= low for low, high in ranges)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["commits", "format"])
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args()
    base = git("merge-base", args.base, args.head).strip()
    if args.mode == "commits":
        failures = [line for line in git("log", "--no-merges", "--format=%h %s", base + ".." + args.head).splitlines()
                    if len(line.split(" ", 1)[1]) > 72]
        if failures:
            raise SystemExit("Introduced commit subjects exceed 72 characters:\n" + "\n".join(failures))
        print("Introduced commit messages passed")
        return
    formatter = ROOT / "util/clangformat" / ("clang-format-15.0.exe" if os.name == "nt" else "clang-format-15.0")
    paths = git("diff", "--name-only", "--diff-filter=ACMR", "-z", base, args.head).split("\0")
    failures = []
    for name in paths:
        path = ROOT / name
        if not name or path.suffix not in EXTENSIONS or path.name == "resource.h" or "3rdparty" in path.parts or "official" in path.parts:
            continue
        after = git("show", args.head + ":" + name)
        try:
            before = git("show", base + ":" + name)
        except subprocess.CalledProcessError:
            before = ""
        ranges = changed_ranges(before, after)
        if not ranges:
            continue
        command = [str(formatter), "-style=file", "-assume-filename=" + str(path)]
        command += [f"-lines={first}:{last}" for first, last in ranges]
        formatted = subprocess.check_output(command, input=after.encode("utf-8"), cwd=ROOT).decode("utf-8")
        if relevant_format_changes(after, formatted, ranges):
            failures.extend(difflib.unified_diff(after.splitlines(), formatted.splitlines(), fromfile=name, tofile=name + " (formatted)", lineterm=""))
    if failures:
        raise SystemExit("Changed-line formatting failed:\n" + "\n".join(failures))
    print("Changed C/C++ lines passed clang-format 15.0.7")


if __name__ == "__main__":
    main()
