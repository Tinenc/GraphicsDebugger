"""Request-local reuse of workspace validation, sealed by fresh file contents.

These Python objects never cross the tool/HTTP protocol. They are minted around
the ordinary status validator, and every reuse re-hashes its bounded inputs.
The capture's RDC and unrelated historical job directories are never scanned.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

from .catalog import MAX_CATALOG_BYTES, ShaderKey
from .errors import ManifestValidationError
from .io import is_within, read_json_object

_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_FILE_BYTES = 512 * 1024 * 1024
_MAX_INPUTS = 4096
_BOUND_SNAPSHOTS: ContextVar[tuple["WorkspaceStatusSnapshot", ...]] = ContextVar(
    "shader_workspace_status_snapshots", default=()
)


class WorkspaceStatusSnapshotChanged(ManifestValidationError):
    """A validated input changed; the caller must obtain fresh validation."""


def _key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _configuration(service: Any) -> tuple[str, ...]:
    registry = getattr(service, "registry", None)
    routes = getattr(service, "route_config_path", None)
    return (_key(registry.path) if registry is not None else "stored-artifacts",
            _key(routes) if routes else "",
            str(getattr(service, "_tool_config_sha256", "")),
            str(getattr(service, "_route_config_sha256", "")))


def _seal(path: Path) -> tuple[str, int, str]:
    # Check the lexical path as well as its final target; an equal-content
    # symlink/junction must not silently replace a fixed evidence location.
    if path.is_symlink() or os.path.normcase(str(path.resolve())) != os.path.normcase(str(path.absolute())):
        raise WorkspaceStatusSnapshotChanged("workspace validation input was redirected")
    try:
        info = path.stat()
    except FileNotFoundError:
        return ("missing", 0, "")
    if stat.S_ISDIR(info.st_mode):
        return ("directory", 0, "")
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
        raise WorkspaceStatusSnapshotChanged("workspace validation input is not a bounded regular file")
    from ..file_content_cache import digest_file
    try:
        digest = digest_file(path, persistent=info.st_size >= 8*1024*1024, maximum_bytes=_MAX_FILE_BYTES)
    except (ValueError,OSError) as error:
        raise WorkspaceStatusSnapshotChanged("workspace validation input changed while hashing") from error
    return ("file", info.st_size, digest)


def _inputs(service: Any, root: Path) -> tuple[tuple[Path, ...], dict]:
    discovery_seals = {}

    def discover(path: Path, limit: int, label: str) -> dict:
        before = _seal(path)
        result = read_json_object(path, limit, label)
        if _seal(path) != before:
            raise WorkspaceStatusSnapshotChanged("workspace dependency graph changed while reading")
        discovery_seals[path] = before
        return result

    manifest = discover(root / "manifest.json", _MAX_MANIFEST_BYTES, "workspace manifest")
    paths = {root, root / "manifest.json", root / "ai" / "flow-graph.json",
             root / "ai" / "module-descriptions.json", root / "explanation" / "representation.json"}

    def visit(value: Any, field: str = "") -> None:
        if isinstance(value, dict):
            for name, child in value.items():
                visit(child, name)
        elif isinstance(value, list):
            for child in value:
                visit(child, field)
        elif isinstance(value, str) and value and (
            field.lower().endswith(("path", "paths")) or field == "producedByJobs"
        ):
            if "\x00" in value or len(value) > 32768:
                raise WorkspaceStatusSnapshotChanged("workspace validation input path is invalid")
            unresolved = Path(value)
            candidate = unresolved if unresolved.is_absolute() else root / unresolved
            if is_within(root, candidate.resolve()):
                paths.add(candidate)
                if field in {"jobPath", "producedByJobs"}:
                    paths.add(candidate / "job.json")

    visit(manifest)
    # Auxiliary descriptors can point at immutable files not repeated in the
    # manifest. Their own bytes and referenced in-workspace paths are sealed.
    descriptor = root / "explanation" / "representation.json"
    if descriptor.is_file():
        visit(discover(descriptor, _MAX_MANIFEST_BYTES, "workspace representation"))

    catalog_value = manifest.get("capture", {}).get("catalogPath")
    if catalog_value:
        catalog = Path(catalog_value)
        paths.add(catalog)
        value = discover(catalog, MAX_CATALOG_BYTES, "capture catalog")
        key = ShaderKey.from_value(manifest["shader"])
        for record in value.get("shaders", []):
            if (isinstance(record, dict) and record.get("rawSha256") == key.raw_sha256
                    and ShaderKey.from_value(record) == key):
                raw = Path(record["rawPath"])
                paths.add(raw if raw.is_absolute() else catalog.parent / raw)

    registry = getattr(service, "registry", None)
    if registry is not None:
        paths.add(Path(registry.path))
    routes = getattr(service, "route_config_path", None)
    if routes is not None:
        paths.add(Path(routes))
    # The process-owned registry is immutable. Seal its pinned executables and
    # adapter dependencies once each, without probing or launching any tool.
    for operation in registry.operations() if registry is not None else ():
        paths.add(Path(operation.executable))
        paths.update(Path(path) for path, unused_hash in operation.fixed_file_hashes)
    if len(paths) > _MAX_INPUTS:
        raise WorkspaceStatusSnapshotChanged("workspace validation input count exceeds its limit")
    return tuple(sorted(paths, key=lambda path: str(path))), discovery_seals


@dataclass(frozen=True)
class WorkspaceStatusSnapshot:
    _root: Path
    _configuration: tuple[str, ...]
    _status_json: bytes
    _seals: tuple[tuple[Path, tuple[str, int, str]], ...]
    _artifact_admission: bool = False

    def validated_status(self) -> dict:
        """Copy the admission result for planning; recheck before mutation.

        A watcher may use this to locate its fixed source before the source
        lease. Only checked_status under that lease authorizes starting it.
        """
        return json.loads(self._status_json)

    def checked_status(self, service: Any) -> dict:
        if not self._artifact_admission and _configuration(service) != self._configuration:
            raise WorkspaceStatusSnapshotChanged("workspace validation process configuration changed")
        for path, expected in self._seals:
            if _seal(path) != expected:
                raise WorkspaceStatusSnapshotChanged("workspace validation input contents changed: %s" % path)
        result = self.validated_status()
        from .workspace import _workspace_operation_locked
        result["workspaceLocked"] = _workspace_operation_locked(self._root)
        return result


def capture_status_snapshot(service: Any, workspace_path: str | Path, *,
                            status_loader=None, artifact_admission=False) -> WorkspaceStatusSnapshot:
    """Run full validation once between two equal content-seal collections."""

    root = Path(workspace_path).expanduser().resolve()
    inputs, discovery_seals = _inputs(service, root)
    before = tuple((path, _seal(path)) for path in inputs)
    if any(dict(before).get(path) != seal for path, seal in discovery_seals.items()):
        raise WorkspaceStatusSnapshotChanged("workspace dependency graph changed before validation")
    result = (status_loader or service.status)(root)
    # Changes to the dependency graph itself are covered by the manifest,
    # catalog and registry files; all were sealed before the validator read.
    after = tuple((path, _seal(path)) for path in inputs)
    if before != after or _key(result.get("workspacePath", "")) != _key(root):
        raise WorkspaceStatusSnapshotChanged("workspace validation inputs changed during validation")
    return WorkspaceStatusSnapshot(root, _configuration(service),
                                   json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"), after,
                                   artifact_admission)


@contextmanager
def bind_status_snapshot(snapshot: WorkspaceStatusSnapshot):
    token = _BOUND_SNAPSHOTS.set((*_BOUND_SNAPSHOTS.get(), snapshot))
    try:
        yield
    finally:
        _BOUND_SNAPSHOTS.reset(token)


def bound_status_snapshot(workspace_path: str | Path) -> WorkspaceStatusSnapshot | None:
    expected = _key(workspace_path)
    return next((item for item in reversed(_BOUND_SNAPSHOTS.get()) if _key(item._root) == expected), None)
