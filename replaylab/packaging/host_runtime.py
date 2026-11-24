"""Assemble and verify the shared frozen Bread Host support tree."""
from __future__ import annotations

import argparse
import hashlib
import json
import marshal
from pathlib import Path, PurePosixPath
import shutil
from types import CodeType
import zipfile

from build_fs import copy_directory, long_path, temporary_directory, walk_files
from restore_dependencies import input_lock


SUPPORT = "bread-host-support"
HOSTS = ("replaylab-ai.exe", "BreadMCP.exe", "ReplayLabInternal.exe", "ReplayLabShaderTools.exe")
MANIFEST = "replaylab-runtime-manifest.json"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(root):
    files = sorted(walk_files(root))
    return {path.relative_to(long_path(root)).as_posix(): path for path in files}


def zip_contents(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate members in the frozen Python standard library")
        return {name: hashlib.sha256(archive.read(name)).digest() for name in names}


def merge_support(destination, directories):
    destination = long_path(destination)
    destination.mkdir()
    names = {}
    for directory in directories:
        source = directory / SUPPORT
        if not source.is_dir() or source.is_symlink() or source.is_junction():
            raise ValueError("Missing ordinary PyInstaller support directory: " + str(source))
        for name, path in inventory(source).items():
            old_name = names.setdefault(name.casefold(), name)
            if old_name != name:
                raise ValueError("Case collision in the Host support files: " + name)
            target = destination / name
            if target.exists():
                same = path.stat().st_size == target.stat().st_size and digest(path) == digest(target)
                if not same and name == "base_library.zip":
                    same = zip_contents(path) == zip_contents(target)
                if not same:
                    raise ValueError("Frozen Hosts disagree on support file: " + name)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)


def support_records(root):
    support = root / SUPPORT
    if not support.is_dir() or support.is_symlink() or support.is_junction():
        raise ValueError("Missing ordinary shared Host support directory")
    return [{"path": SUPPORT + "/" + name, "size": path.stat().st_size, "sha256": digest(path)}
            for name, path in inventory(support).items()]


def runtime_paths(root, manifest, *, require_hosts=True):
    """Yield only a verified, exact frozen payload. Accept existing onefile receipts."""
    seen, files = set(), {}
    for record in manifest["files"]:
        name = record["path"]
        relative = PurePosixPath(name)
        if (name != relative.as_posix() or "\\" in name or ":" in name or relative.is_absolute()
                or ".." in relative.parts or name.casefold() in seen
                or (name not in HOSTS and not name.startswith(SUPPORT + "/"))):
            raise ValueError("Invalid or duplicate runtime manifest path: " + name)
        path = long_path(root / name)
        if (not path.is_file() or path.is_symlink() or path.is_junction()
                or path.stat().st_size != record["size"] or digest(path) != record["sha256"]):
            raise ValueError("Frozen Host file differs from its manifest: " + name)
        seen.add(name.casefold())
        files[name] = path
    if require_hosts and not set(HOSTS) <= files.keys():
        raise ValueError("Frozen runtime manifest does not include all four Hosts")
    support = root / SUPPORT
    expected = {name.removeprefix(SUPPORT + "/") for name in files if name.startswith(SUPPORT + "/")}
    if manifest.get("layout") == "onedir":
        if not support.is_dir() or support.is_symlink() or support.is_junction():
            raise ValueError("Missing ordinary shared Host support directory")
        if not expected or expected != inventory(support).keys():
            raise ValueError("Shared Host support directory differs from its exact manifest")
    elif expected or support.exists() or support.is_symlink() or support.is_junction():
        raise ValueError("Unexpected support files in a onefile runtime")
    return files


def publish(source, destination):
    destination = long_path(destination)
    with input_lock(destination.with_name("." + destination.name + ".lock")):
        _publish_locked(source, destination)


def _publish_locked(source, destination):
    manifest = json.loads((source / MANIFEST).read_text(encoding="utf-8-sig"))
    files = runtime_paths(source, manifest)
    allowed = {MANIFEST, SUPPORT, *HOSTS}
    destination = long_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_dir() or destination.is_symlink() or destination.is_junction():
            raise ValueError("Portable runtime output is not an ordinary directory")
        if any(destination.iterdir()):
            if {child.name for child in destination.iterdir()} != allowed:
                raise ValueError("Portable runtime output contains files owned by another operation")
            receipt = destination / MANIFEST
            if receipt.is_symlink() or receipt.is_junction() or not receipt.is_file():
                raise ValueError("Portable runtime output has no ordinary manifest")
            previous = json.loads(receipt.read_text(encoding="utf-8-sig"))
            if previous.get("schema") != manifest["schema"] or previous.get("layout") != "onedir":
                raise ValueError("Portable runtime output has an unknown owner")
            runtime_paths(destination, previous)
    # Prepare and verify the entire replacement on the destination volume before
    # touching an existing generation. A failed rename restores its original path.
    with temporary_directory(prefix=".bread-hosts-", dir=destination.parent) as temporary:
        temporary = Path(temporary)
        candidate = temporary / "candidate"
        previous = destination.with_name(destination.name + "-" + temporary.name + "-backup")
        candidate.mkdir()
        for name in HOSTS:
            shutil.copy2(files[name], candidate / name)
        copy_directory(source / SUPPORT, candidate / SUPPORT)
        shutil.copy2(source / MANIFEST, candidate / MANIFEST)
        runtime_paths(candidate, manifest)
        existed = destination.exists()
        if existed:
            destination.rename(previous)
        try:
            candidate.rename(destination)
        except OSError:
            if existed:
                try:
                    previous.rename(destination)
                except OSError:
                    raise RuntimeError(f"Runtime swap failed; previous generation retained at {previous}")
            raise
        if existed:
            # Cleanup is scoped to the owned temporary directory. Preserve any
            # still-open previous generation if Windows denies the move.
            try:
                previous.rename(temporary / "previous")
            except OSError:
                print(f"Warning: previous frozen Host generation retained at {previous}")


def code_identity(code):
    return (
        code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount, code.co_nlocals,
        code.co_stacksize, code.co_flags, code.co_code, code.co_names, code.co_varnames,
        code.co_freevars, code.co_cellvars, code.co_exceptiontable,
        tuple(code_identity(value) if isinstance(value, CodeType) else (type(value), value)
              for value in code.co_consts),
    )


def verify_sources(executable, entry, roots):
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(executable))
    pyz = archive.open_embedded_archive(next(name for name in archive.toc if name.endswith(".pyz")))
    verified = 0
    differences = []

    def compare(name, source, frozen):
        nonlocal verified
        compiled = compile(source.read_bytes(), str(source), "exec", dont_inherit=True, optimize=0)
        if not isinstance(frozen, CodeType) or code_identity(compiled) != code_identity(frozen):
            differences.append(name)
        verified += 1

    compare(entry.name, entry, marshal.loads(archive.extract(entry.stem)))
    for root in roots:
        for source in root.rglob("*.py"):
            relative = source.relative_to(root.parent).with_suffix("")
            name = ".".join(relative.parts[:-1] if source.name == "__init__.py" else relative.parts)
            if name in pyz.toc:
                compare(name, source, pyz.extract(name))
            else:
                differences.append(name + " (missing)")
    if differences:
        raise ValueError(f"{executable.name} does not match current frozen source: {', '.join(differences)}")
    print(f"{executable.name}: verified {verified} first-party modules against frozen bytecode")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command, parameters in (("merge", ("destination", "directories")),
                                ("records", ("root",)),
                                ("publish", ("source", "destination")),
                                ("verify-sources", ("executable", "entry", "roots"))):
        sub = commands.add_parser(command)
        for name in parameters:
            sub.add_argument(name, type=Path, **({"nargs": "+"} if name in {"roots", "directories"} else {}))
    args = parser.parse_args()
    if args.command == "merge":
        merge_support(args.destination, args.directories)
    elif args.command == "records":
        print(json.dumps(support_records(args.root), ensure_ascii=True))
    elif args.command == "publish":
        publish(args.source, args.destination)
    else:
        verify_sources(args.executable, args.entry, args.roots)
