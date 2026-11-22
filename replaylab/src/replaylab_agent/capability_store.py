"""App-owned compressed runtimes. Extraction is transactional and provider neutral."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import threading
import time
import zipfile

COMPONENTS = frozenset({"office", "browsers", "python", "native", "node", "provider-runtime", "custom-skills"})
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _native(path: Path) -> Path:
    text = str(path.absolute())
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        text = "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text
    return Path(text)


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or "\\" in value or ":" in value or any(p in (".", "..", "") for p in value.split("/")):
        raise ValueError("Invalid runtime archive path")
    return path


def _remove_owned(path: Path, root: Path) -> None:
    # Every recursive removal is an exact installation-owned staging/backup
    # below this tool store, never a project or a computed filesystem root.
    def plain(value):
        text = str(value.resolve())
        if os.name == "nt" and text.startswith("\\\\?\\UNC\\"):
            text = "\\\\" + text[8:]
        elif os.name == "nt" and text.startswith("\\\\?\\"):
            text = text[4:]
        return Path(text)
    resolved, parent = plain(path), plain(root)
    if resolved == parent or not resolved.is_relative_to(parent) or path.is_symlink():
        raise ValueError("Runtime cleanup escaped its app-owned store")
    shutil.rmtree(_native(resolved))


@contextmanager
def _installation_lock(root: Path, name: str):
    key = os.path.normcase(str(root.resolve())) + ":" + name
    with _guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        state = _native(root / ".runtime-state")
        state.mkdir(parents=True, exist_ok=True)
        with (state / (name + ".lock")).open("a+b") as stream:
            stream.seek(0)
            if not stream.read(1):
                stream.write(b"0"); stream.flush()
            deadline = time.monotonic() + 180
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Runtime preparation is still in progress")
                    time.sleep(.05)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def ensure_component(root: Path, manifest: dict, name: str) -> Path:
    if name not in COMPONENTS:
        raise ValueError("Unknown app-owned runtime component")
    root = root.resolve()
    target = root / name
    record = next((r for r in manifest.get("componentArchives", []) if r.get("id") == name), None)
    if record is None:
        return target  # Full/legacy bundles remain readable without conversion.
    archive = root.joinpath(*_relative(record["path"]).parts)
    expected_hash = record["sha256"]
    marker = target / ".bread-runtime.json"
    if target.is_symlink() or target.is_junction():
        raise ValueError("Runtime destination is a redirected path")
    def ready():
        try:
            return json.loads(_native(marker).read_text(encoding="utf-8")).get("archiveSha256") == expected_hash
        except (OSError, ValueError):
            return False
    if ready():
        return target
    with _installation_lock(root, name):
        if ready():
            return target
        with _native(archive).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected_hash:
                raise ValueError("Runtime archive checksum mismatch: " + name)
        expected = {r["path"]: r for r in manifest["files"] if r["path"].startswith(name + "/")}
        staging_root = root / ".runtime-state"
        staging = Path(tempfile.mkdtemp(prefix=name + "-", dir=str(_native(staging_root))))
        (staging / "unpack-owner.json").write_text(json.dumps(
            {"schema": "bread-runtime-unpack/v1", "component": name}), encoding="utf-8")
        previous = staging / "previous"
        installed = False
        try:
            with zipfile.ZipFile(_native(archive)) as package:
                files = [item for item in package.infolist() if not item.is_dir()]
                if len(files) != len(expected) or {item.filename for item in files} != set(expected):
                    raise ValueError("Runtime archive inventory mismatch: " + name)
                for item in files:
                    relative = _relative(item.filename)
                    if relative.parts[0] != name or stat.S_ISLNK(item.external_attr >> 16):
                        raise ValueError("Invalid runtime archive member")
                    wanted = expected[item.filename]
                    if item.file_size != wanted["size"]:
                        raise ValueError("Runtime member size mismatch")
                    destination = staging.joinpath(*relative.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    with package.open(item) as source, destination.open("xb") as output:
                        while chunk := source.read(1024 * 1024):
                            digest.update(chunk); output.write(chunk)
                    if digest.hexdigest() != wanted["sha256"]:
                        raise ValueError("Runtime member checksum mismatch")
            prepared = staging / name
            (prepared / ".bread-runtime.json").write_text(
                json.dumps({"archiveSha256": expected_hash}), encoding="utf-8")
            if _native(target).exists():
                if target.is_symlink() or target.is_junction():
                    raise ValueError("Runtime destination is a redirected path")
                os.replace(_native(target), previous)
            try:
                os.replace(prepared, _native(target))
                installed = True
            except BaseException:
                if previous.exists() and not _native(target).exists():
                    os.replace(previous, _native(target))
                raise
        finally:
            if installed or not previous.exists():
                _remove_owned(staging, root)
        # The OS lock proves no other extractor of this component is active.
        # Collect only marked staging directories after the new installation
        # succeeded; an interrupted/failed attempt never discards old tools.
        for abandoned in _native(staging_root).glob(name + "-*"):
            try:
                owner = abandoned / "unpack-owner.json"
                if (abandoned.is_dir() and not abandoned.is_symlink() and not abandoned.is_junction()
                        and owner.stat().st_size < 4096
                        and json.loads(owner.read_text(encoding="utf-8")) ==
                            {"schema": "bread-runtime-unpack/v1", "component": name}):
                    _remove_owned(abandoned, root)
            except (OSError, ValueError):
                pass
        return target


def declared_file(root: Path, manifest: dict, relative: str) -> bool:
    if _native(root / relative).is_file():
        return True
    component = relative.split("/", 1)[0]
    record = next((r for r in manifest.get("componentArchives", []) if r.get("id") == component), None)
    return bool(record and _native(root / record["path"]).is_file()
                and any(r["path"] == relative for r in manifest.get("files", [])))
