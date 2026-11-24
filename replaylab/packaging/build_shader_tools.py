"""Build/cache the independent shader runner from verified content inputs.

The local import graph is staged under a namespace package, so importing an
adapter never executes src.shader_workspace.__init__ or collects the Host.
No build flag or dependency version is used as a substitute for file hashes.
"""

from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import sysconfig
import time
import uuid


SCHEMA = "replaylab-shader-tools-cache/v1"
EXECUTABLE = "ReplayLabShaderTools.exe"
FLAGS = ("--noconfirm", "--onefile", "--noupx", "--log-level", "WARN",
         "--name", "ReplayLabShaderTools")


def checked_path(path: Path, *, file: bool = False) -> Path:
    path = Path(os.path.abspath(path))
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("shader tool build path is redirected: %s" % component)
    if file and not path.is_file():
        raise ValueError("shader tool build input is not a regular file: %s" % path)
    return path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with checked_path(path, file=True).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def local_sources(source_root: Path, entry: Path) -> dict[str, Path]:
    """Resolve static adapter imports recursively and reject opaque additions."""
    adapters = checked_path(source_root / "src" / "shader_workspace" / "adapters")
    found = {"shader_tools_entry.py": checked_path(entry, file=True)}
    pending = [("shader_tools_entry", entry)]

    def add(module: str, *, required: bool = True) -> None:
        if module == "adapters":
            relative = Path("__init__.py")
        else:
            relative = Path(*module.split(".")[1:]).with_suffix(".py")
        path = adapters / relative
        if not path.is_file():
            package = path.with_suffix("") / "__init__.py"
            if package.is_file():
                path = package
            elif module == "adapters" or not required:
                return  # The canonical adapters directory is a namespace.
            else:
                raise ValueError("missing local shader adapter import: " + module)
        name = "adapters/" + path.relative_to(adapters).as_posix()
        if name not in found:
            found[name] = checked_path(path, file=True)
            pending.append((module, path))
        pieces = module.split(".")
        for length in range(1, len(pieces)):
            add(".".join(pieces[:length]), required=False)

    while pending:
        module, path = pending.pop()
        tree = ast.parse(checked_path(path, file=True).read_text(encoding="utf-8"))
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id == "__import__"
                or isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
            ):
                raise ValueError("dynamic imports require an explicit shader build contract: %s" % path)
            if isinstance(node, ast.Import):
                imports = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    parts = package.split(".")
                    if node.level > len(parts):
                        raise ValueError("shader adapter import escapes its package")
                    prefix = ".".join(parts[:len(parts) - node.level + 1] + ([prefix] if prefix else []))
                imports = [prefix]
                if prefix == "adapters" or prefix.startswith("adapters."):
                    for alias in node.names:
                        add(prefix + "." + alias.name, required=False)
            else:
                continue
            for imported in imports:
                if imported == "adapters" or imported.startswith("adapters."):
                    add(imported)
                elif imported.split(".")[0] not in sys.stdlib_module_names | {"__future__"}:
                    raise ValueError("shader runner import is outside its audited local/stdlib closure: " + imported)
    return found


def _tree_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    result = []
    for directory, dirs, files in os.walk(checked_path(root), followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in (
            "__pycache__", "site-packages"))
        for name in dirs:
            checked_path(Path(directory) / name)
        result.extend(checked_path(Path(directory) / name, file=True)
                      for name in sorted(files) if not name.endswith((".pyc", ".pyo")))
    return result


def build_environment_files() -> dict[str, Path]:
    """Hash the interpreter, stdlib, freezer/bootloader and installed hook inputs."""
    from packaging.requirements import Requirement

    result = {"python/executable": checked_path(Path(sys.executable), file=True)}
    base = checked_path(Path(sys.base_prefix))
    for path in sorted(base.iterdir()):
        if path.is_file() and path.suffix.lower() in (".exe", ".dll", ".zip", ".pyd"):
            result["python/base/" + path.name] = path
    for root, label in ((Path(sysconfig.get_path("stdlib")), "stdlib"),
                        (base / "DLLs", "DLLs")):
        for path in _tree_files(root):
            result["python/" + label + "/" + path.relative_to(root).as_posix()] = path

    # -I still runs the interpreter's site initialization. Bind installed
    # startup scripts (including editable finders referenced by .pth files).
    for directory in map(Path, sys.path):
        if not directory.is_dir() or directory.name not in ("site-packages", "dist-packages"):
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file() and (path.suffix == ".pth" or
                                  path.name.startswith("__editable__") or
                                  path.name in ("sitecustomize.py", "usercustomize.py")):
                result["python/site-startup/" + str(path)] = checked_path(path, file=True)

    # Hook discovery can be extended by installed providers. Bind their code,
    # metadata and dependencies too, even when a hook ultimately is not used.
    requested = ["pyinstaller"]
    requested.extend(point.dist.metadata["Name"] for point in
                     importlib.metadata.entry_points(group="pyinstaller40") if point.dist)
    seen = set()
    while requested:
        name = requested.pop()
        distribution = importlib.metadata.distribution(name)
        identity = distribution.metadata["Name"].lower().replace("_", "-")
        if identity in seen:
            continue
        seen.add(identity)
        files = distribution.files
        if not files:
            raise ValueError("shader build dependency has no file inventory: " + identity)
        for relative in files:
            if str(relative).endswith((".pyc", ".pyo")):
                continue
            path = checked_path(Path(distribution.locate_file(relative)), file=True)
            result["distribution/" + identity + "/" + str(relative).replace("\\", "/")] = path
        for requirement_text in distribution.requires or ():
            requirement = Requirement(requirement_text)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                requested.append(requirement.name)
    return result


def input_manifest(source_root: Path, entry: Path, *, environment_files=None) -> tuple[dict, dict[str, Path]]:
    sources = local_sources(source_root, entry)
    paths = {"source/" + name: path for name, path in sources.items()}
    paths["build/implementation.py"] = Path(__file__)
    paths.update(build_environment_files() if environment_files is None else environment_files)
    material = {
        "schema": SCHEMA,
        "python": {"version": sys.version, "cacheTag": sys.implementation.cache_tag,
                   "platform": sys.platform, "machine": platform.machine()},
        "flags": list(FLAGS),
        "files": {name: sha256(path) for name, path in sorted(paths.items())},
    }
    return {**material, "inputSha256": hashlib.sha256(canonical(material)).hexdigest()}, sources


@contextmanager
def cache_lock(root: Path, timeout: float = 180.0):
    checked_path(root).mkdir(parents=True, exist_ok=True)
    lock_path = checked_path(root / ".build.lock")
    with lock_path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + timeout
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
                    raise TimeoutError("shader tool cache build lock is busy")
                time.sleep(0.1)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def verified_cache(entry: Path, inputs: dict) -> Path:
    record_path = checked_path(entry / "manifest.json", file=True)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    executable = checked_path(entry / EXECUTABLE, file=True)
    if (set(record) != {"schema", "inputs", "executableSha256", "size"}
            or record["schema"] != SCHEMA or record["inputs"] != inputs
            or record["size"] != executable.stat().st_size
            or record["executableSha256"] != sha256(executable)):
        raise ValueError("shader tool cache content differs from its verified inputs/artifact")
    return executable


def _run_build(staging: Path, work: Path, sources: dict[str, Path]) -> Path:
    for relative, source in sources.items():
        destination = checked_path(staging / relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checked_path(source, file=True), destination)
    dist = checked_path(work / "dist")
    spec = checked_path(work / "spec")
    spec.mkdir(parents=True, exist_ok=True)
    # PyInstaller's Windows DLL exclusion policy resolves Path.home(). Keep
    # the caller's actual Windows home identity without inheriting arbitrary
    # Python paths, provider credentials, or user-supplied build options.
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC",
                                      "USERPROFILE", "HOMEDRIVE", "HOMEPATH"}}
    environment.update(PYTHONHASHSEED="0", PYTHONIOENCODING="utf-8",
                       PYINSTALLER_CONFIG_DIR=str(work / "pyinstaller-config"))
    environment["PATH"] = os.pathsep.join((str(Path(sys.executable).parent),
                                          str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")))
    subprocess.run([sys.executable, "-I", "-m", "PyInstaller", *FLAGS,
                    "--paths", str(staging), "--distpath", str(dist),
                    "--workpath", str(work / "build"), "--specpath", str(spec),
                    str(staging / "shader_tools_entry.py")],
                   cwd=staging, env=environment, stdout=sys.stderr, check=True)
    return checked_path(dist / EXECUTABLE, file=True)


def build_or_reuse(source_root: Path, entry: Path, cache_root: Path, work_root: Path,
                   output: Path, *, environment_files=None, builder=_run_build) -> dict:
    for path in (source_root, entry, cache_root, work_root, output):
        checked_path(path)
    with cache_lock(cache_root):
        inputs, sources = input_manifest(source_root, entry, environment_files=environment_files)
        cached = checked_path(cache_root / inputs["inputSha256"])
        hit = cached.exists()
        if hit:
            executable = verified_cache(cached, inputs)
        else:
            staging = checked_path(work_root / "source")
            staging.mkdir(parents=True, exist_ok=False)
            executable = builder(staging, work_root, sources)
            pending = checked_path(cache_root / (".pending-" + uuid.uuid4().hex))
            pending.mkdir(exist_ok=False)
            shutil.copyfile(checked_path(executable, file=True), pending / EXECUTABLE)
            record = {"schema": SCHEMA, "inputs": inputs,
                      "executableSha256": sha256(pending / EXECUTABLE),
                      "size": (pending / EXECUTABLE).stat().st_size}
            if record["size"] <= 0:
                raise ValueError("shader tool build produced an empty executable")
            (pending / "manifest.json").write_bytes(canonical(record))
            executable = verified_cache(pending, inputs)
        after, unused_sources = input_manifest(source_root, entry, environment_files=environment_files)
        if after != inputs:
            raise ValueError("shader tool build inputs changed during generation")
        if not hit:
            pending.rename(cached)
            executable = verified_cache(cached, inputs)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = checked_path(output.with_name("." + output.name + "." + uuid.uuid4().hex))
        shutil.copyfile(executable, temporary)
        digest = sha256(executable)
        if sha256(temporary) != digest:
            raise ValueError("shader tool output changed while copying")
        os.replace(temporary, checked_path(output))
        return {"schema": SCHEMA, "inputSha256": inputs["inputSha256"],
                "executableSha256": digest, "cacheHit": hit, "path": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-root", "entry", "cache-root", "work-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    options = parser.parse_args()
    result = build_or_reuse(options.source_root, options.entry, options.cache_root,
                           options.work_root, options.output)
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
