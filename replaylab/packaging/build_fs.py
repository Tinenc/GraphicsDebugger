"""Windows long-path filesystem operations for sealed build and package data."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time


def long_path(path: Path | str) -> Path:
    absolute = os.path.abspath(os.fspath(path))
    if os.name == "nt" and not absolute.startswith("\\\\?\\"):
        absolute = "\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute
    return Path(absolute)


def logical_path(path: Path | str) -> Path:
    value = os.fspath(path)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def walk_files(root: Path):
    root = long_path(root)
    def fail(error):
        raise OSError(f"Cannot enumerate build input {logical_path(root)}; existing data was preserved") from error
    for current, dirs, files in os.walk(root, onerror=fail, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            details = path.lstat()
            if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"Build inputs cannot contain links or reparse points: {logical_path(path)}")
        for name in files:
            yield Path(current) / name


def copy_directory(source: Path, target: Path) -> None:
    source, target = long_path(source), long_path(target)
    if not source.is_dir() or source.is_symlink() or source.is_junction():
        raise ValueError("The build source must be an ordinary directory")
    for path in walk_files(source):
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or destination.is_junction():
            raise ValueError("The build destination must not redirect a file")
        shutil.copy2(path, destination)


@contextmanager
def temporary_directory(*, prefix: str, dir: Path | str | None = None):
    # Retain a lexical identity before any delete; only this freshly created directory is owned.
    work = Path(tempfile.mkdtemp(prefix=prefix, dir=long_path(dir) if dir else None))
    owned = long_path(work)
    try:
        yield str(owned)
    finally:
        def writable(function, path, error):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            function(path)
        for attempt in range(4):
            try:
                shutil.rmtree(owned, onexc=writable)
                break
            except FileNotFoundError:
                break
            except OSError as error:
                if attempt == 3:
                    print(f"Warning: temporary build files retained at {logical_path(owned)}: {error}", file=sys.stderr)
                else:
                    time.sleep(.1 * (attempt + 1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["copy"])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    copy_directory(args.source, args.destination)
