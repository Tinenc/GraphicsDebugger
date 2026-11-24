"""Restore sealed build inputs without a developer installation or account state.

Only archives listed in dependencies/inputs.lock.json are accepted. Expanded
files are disposable build data; verified archives remain read-only. Missing
archives are assembled offline from the ordinary Git files listed in the lock.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import time
import zipfile
from build_fs import long_path, logical_path, temporary_directory, walk_files
from file_hashes import digest as sha, warm_digests, identity, settled

REPO = Path(__file__).resolve().parents[2]
DEPENDENCIES = REPO / "replaylab/dependencies"


def child(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if (not relative or "\\" in relative or ":" in relative or parts.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise ValueError(f"Unsafe dependency path: {relative!r}")
    target = root.joinpath(*parts.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Dependency path escapes its root: {relative}")
    return target


def verify_archive(root: Path, record: dict) -> Path:
    root = long_path(root)
    archive = child(root, record["path"])
    if not archive.is_file():
        if record.get("parts"):
            return assemble_archive(root, record)
        raise ValueError(f"Missing dependency input {record['path']}; obtain the complete source checkout including replaylab/dependencies/archives.")
    if archive.stat().st_size != record["size"] or sha(archive) != record["sha256"]:
        with archive.open("rb") as stream:
            pointer = stream.read(128).startswith(b"version https://git-lfs.github.com/spec/v1")
        if pointer and record.get("parts"):
            return assemble_archive(root, record)
        hint = "; legacy Git LFS pointer found: obtain the complete source checkout including replaylab/dependencies/archives" if pointer else ""
        raise ValueError(f"Dependency input changed: {record['path']}{hint}")
    expected = {r["path"] for r in record["files"]}
    if len(expected) != len(record["files"]):
        raise ValueError("Duplicate dependency inventory paths")
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        names = [r.filename for r in members]
        if len(names) != len(expected) or set(names) != expected:
            raise ValueError(f"Dependency archive inventory differs: {record['id']}")
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("Dependency archive has case-colliding paths")
        for member in members:
            child(root, member.filename)
            if member.is_dir() or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("Dependency archives must contain ordinary files only")
        sizes = {r["path"]: r["size"] for r in record["files"]}
        if any(member.file_size != sizes[member.filename] for member in members):
            raise ValueError("Dependency archive member size differs")
    return archive


def assemble_archive(root: Path, record: dict) -> Path:
    """Restore an ignored ZIP from checked-in parts without any network access."""
    root = long_path(root)
    archive = child(root, record["path"])
    root.mkdir(parents=True, exist_ok=True)
    parts = record.get("parts", [])
    if (not parts or len({r["path"] for r in parts}) != len(parts)
            or sum(r["size"] for r in parts) != record["size"]):
        raise ValueError("Invalid dependency archive parts inventory")
    with input_lock(root / (".assemble-" + archive.name + ".lock")):
        if archive.is_symlink() or archive.is_junction():
            raise ValueError("Dependency archive target must be an ordinary file")
        if archive.exists():
            with archive.open("rb") as stream:
                pointer = stream.read(128).startswith(b"version https://git-lfs.github.com/spec/v1")
            if not pointer:
                return verify_archive(root, record)
        pending = None
        try:
            with tempfile.NamedTemporaryFile(dir=root, prefix=".bread-archive-", suffix=".partial", delete=False) as stream:
                pending = Path(stream.name)
                for part in parts:
                    source = child(root, part["path"])
                    if (source == archive or source.is_symlink() or source.is_junction()
                            or not source.is_file() or source.stat().st_size != part["size"]):
                        raise ValueError(f"Missing or changed dependency part: {part['path']}; obtain a complete source checkout.")
                    digest = hashlib.sha256()
                    with source.open("rb") as data:
                        while chunk := data.read(1024 * 1024):
                            digest.update(chunk)
                            stream.write(chunk)
                    if digest.hexdigest() != part["sha256"]:
                        raise ValueError(f"Dependency part hash differs: {part['path']}")
            staged = dict(record, path=pending.name, parts=[])
            verify_archive(root, staged)
            os.replace(pending, archive)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
    return archive


def verify_tree(root: Path, records: list[dict]) -> bool:
    root = long_path(root)
    if {p.relative_to(root).as_posix() for p in walk_files(root)} != {r["path"] for r in records}:
        return False
    warm_digests((child(root, r["path"]) for r in records), fresh=True)
    for record in records:
        path = child(root, record["path"])
        if (not path.is_file() or path.is_symlink() or path.is_junction()
                or path.stat().st_size != record["size"] or sha(path) != record["sha256"]):
            return False
    return True


@contextmanager
def input_lock(path: Path):
    """An OS-owned lock is released after exceptions and process termination."""
    path = long_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + 120
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Another dependency restore is still active: {path}")
                time.sleep(0.1)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def tree_receipt(target, record):
    # Per-file identity detects replacement and same-size edits with restored
    # mtimes. Aggregate totals alone cannot safely authorize cache reuse.
    files = {p.relative_to(target).as_posix(): list(identity(p)) for p in walk_files(target)}
    return {"schema": "bread-restored-tree/v1", "archiveSha256": record["sha256"],
            "inventorySha256": hashlib.sha256(json.dumps(record["files"], sort_keys=True).encode()).hexdigest(),
            "settled": all(settled(value) for value in files.values()), "files": files}


def restore(root: Path, output: Path, record: dict, *, full_verify=False) -> Path:
    root, output = long_path(root), long_path(output)
    # Content-addressed directories allow different revisions to build concurrently.
    parent = output / record["id"]
    legacy = parent / record["sha256"]
    target = legacy if legacy.exists() else parent / record["sha256"][:20]
    receipt = target.with_suffix(".ready.json")
    if output.resolve().is_relative_to(root.resolve()):
        raise ValueError("Expanded dependencies must not be written inside sealed inputs")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.is_junction():
        raise ValueError("Dependency output must not be a link")
    with input_lock(target.with_suffix(".lock")):
        archive = verify_archive(root, record)
        if target != legacy and receipt.is_file():
            try:
                previous = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}
            if isinstance(previous, dict) and previous.get("archiveSha256") and previous["archiveSha256"] != record["sha256"]:
                raise ValueError("Short dependency directory hash collision; use a different output root")
        if target.exists():
            current = tree_receipt(target, record)
            expected = {r["path"] for r in record["files"]}
            reusable = False
            if not full_verify and current["settled"] and set(current["files"]) == expected:
                try:
                    reusable = json.loads(receipt.read_text(encoding="utf-8")) == current
                except (OSError, ValueError):
                    pass
            if reusable or verify_tree(target, record["files"]):
                if not reusable:
                    write_tree_receipt(receipt, current)
                return logical_path(target)
        with temporary_directory(prefix=".restoring-", dir=target.parent) as work:
            staging = Path(work) / "payload"
            staging.mkdir()
            with zipfile.ZipFile(archive) as source:
                for member in source.infolist():
                    destination = child(staging, member.filename)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as src, destination.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
            if not verify_tree(staging, record["files"]):
                raise ValueError(f"Extracted dependency hash differs: {record['id']}")
            if target.exists():
                # Preserve an incomplete/modified generation for diagnosis. Never
                # recursively delete a caller-selected output or follow links.
                damaged = Path(work) / "previous"
                target.rename(damaged)
                try:
                    staging.rename(target)
                except BaseException:
                    damaged.rename(target)
                    raise
                damaged.rename(target.with_name(target.name + f".damaged-{time.time_ns()}"))
            else:
                staging.rename(target)
        write_tree_receipt(receipt, tree_receipt(target, record))
    return logical_path(target)


def write_tree_receipt(path, value):
    # Called under the generation lock; the receipt is outside the exact tree.
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(pending, path)


def load_lock(root: Path = DEPENDENCIES) -> dict:
    value = json.loads((root / "inputs.lock.json").read_text(encoding="utf-8"))
    if value.get("schema") != "bread-build-inputs/v1":
        raise ValueError("Unknown build-input lock format")
    ids = [r["id"] for r in value["archives"]]
    if len(ids) != len(set(ids)) or any(not i or not all(c.isalnum() or c == "-" for c in i) for i in ids):
        raise ValueError("Invalid dependency input identifiers")
    return value


def standard_capabilities(config: dict, output: Path | None = None) -> Path:
    # Explicit legacy package configurations remain usable. New configurations
    # resolve a sealed input, never a developer's ignored release snapshot.
    if "standardCapabilitiesInput" not in config:
        return REPO / config["standardCapabilitiesDirectory"]
    identifier = config["standardCapabilitiesInput"]
    record = next(r for r in load_lock()["archives"] if r["id"] == identifier)
    return restore(DEPENDENCIES, output or REPO / "replaylab/output/build-inputs/expanded", record, full_verify=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, default=DEPENDENCIES)
    parser.add_argument("--output", type=Path, default=REPO / "replaylab/output/dependencies")
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--full-verify", action="store_true", help="Bypass expanded-tree receipts and hash every file")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    lock = load_lock(args.inputs)
    selected = set(args.only) or {r["id"] for r in lock["archives"]}
    if selected - {r["id"] for r in lock["archives"]}:
        parser.error("Unknown --only dependency input")
    paths = {}
    for record in lock["archives"]:
        if record["id"] not in selected:
            continue
        path = (verify_archive(args.inputs, record) if args.verify_only
                else restore(args.inputs, args.output, record, full_verify=args.full_verify))
        if args.verify_only:
            parent = long_path(args.output) / record["id"]
            for target in {parent / record["sha256"], parent / record["sha256"][:20]}:
                if target.exists() and not verify_tree(target, record["files"]):
                    raise ValueError(f"Expanded dependency differs: {record['id']}")
        paths[record["id"]] = str(path.resolve())
        print(f"Verified {record['id']}" if args.verify_only else f"Restored {record['id']}", flush=True)
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(paths, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
