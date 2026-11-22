"""Capability-based routing for optional asset evidence.

Readers enrich capture evidence; a missing reader is never a prerequisite for
Shader analysis. Registrations describe candidate capabilities, while returned
data proves what a particular read actually provided. No subprocesses, project
writes or export operations are performed by the routing policy itself.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


READ_CAPABILITIES = frozenset(("classify", "properties", "references"))
MAX_READER_ATTEMPTS = 2
_BUILTIN_ENGINES = {"unitypy": "unity", "assetstudio": "unity", "assetripper": "unity",
                    "uex": "unreal", "umodel": "unreal"}


def normalize_engine(value: Any) -> str:
    value = str(value or "").strip().lower()
    return {"unknown": "", "ue4": "unreal", "ue5": "unreal", "unrealengine": "unreal"}.get(value, value)


def normalize_capability(value: str = "", *, deep: bool = False) -> str:
    value = str(value or ("properties" if deep else "classify")).strip().lower()
    if value not in READ_CAPABILITIES:
        raise ValueError("reader capability must be classify, properties or references")
    return value


def selection_for_root(selection: Mapping[str, Any] | None, root: Path) -> Mapping[str, Any]:
    """Do not reuse a project's choice after its game directory has changed."""
    if not isinstance(selection, Mapping) or not selection.get("assetRoot"):
        return {}
    try:
        matches = Path(str(selection["assetRoot"])).resolve() == root.resolve()
    except (OSError, ValueError):
        return {}
    return selection if matches else {}


def selected_reader(selection: Mapping[str, Any], engine: str, capability: str) -> str:
    selections = selection.get("selections") or {}
    if isinstance(selections, Mapping):
        group = selections.get(engine or "unknown") or {}
        if isinstance(group, Mapping):
            item = group.get(capability) or {}
            if isinstance(item, Mapping) and item.get("reader"):
                return str(item["reader"])
    # Legacy v1 selections only proved classification. A header winner is not a
    # choice of reader for material fields or the complete dependency graph.
    if (capability == "classify" and selection.get("capability", "classify") == "classify"
            and normalize_engine(selection.get("engine")) in {"", engine}):
        return str(selection.get("reader") or "")
    return ""


def operation_identity(tool: Mapping[str, Any], operation: Mapping[str, Any]) -> str:
    connection = tool.get("connection") or {}
    invocation = {"connection": connection, "kind": operation.get("kind"),
                  "argv": operation.get("argv"), "name": operation.get("name")
                  if operation.get("kind") == "mcp" else None,
                  "pathArgument": operation.get("pathArgument")}
    return hashlib.sha256(json.dumps(invocation, sort_keys=True, default=str).encode()).hexdigest()


def reader_candidates(tools: Sequence[Mapping[str, Any]], relative: str, *,
                      engine: str = "", capability: str = "classify",
                      preferred: str = "") -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Order eligible read-only operations, with a project choice before defaults.

    Old registrations lacking capability metadata remain eligible, but their
    result must prove the requested capability. General/export operations never
    run implicitly as part of a read.
    """
    engine = normalize_engine(engine)
    ranked = []
    for tool in tools:
        if not tool.get("enabled", True) or "asset-reader" not in (tool.get("roles") or []):
            continue
        for operation in (tool.get("interface") or {}).get("operations") or []:
            if operation.get("role") != "asset-reader" or not operation.get("proved"):
                continue
            if operation.get("kind") not in {"mcp", "cli"}:
                continue
            declared = operation.get("capabilities") or []
            if declared and capability not in declared:
                continue
            engines = operation.get("engines") or tool.get("engines") or []
            if not engines and tool.get("builtinKind") in _BUILTIN_ENGINES:
                engines = [_BUILTIN_ENGINES[str(tool["builtinKind"])]]
            known_engines = {normalize_engine(item) for item in engines} - {""}
            if engine and known_engines and engine not in known_engines:
                continue
            patterns = operation.get("patterns") or []
            name = PurePosixPath(relative).name.lower()
            if patterns and not any(fnmatch.fnmatchcase(relative.lower(), str(pattern).lower())
                                    or fnmatch.fnmatchcase(name, str(pattern).lower())
                                    for pattern in patterns):
                continue
            identifier = str(tool.get("id") or "")
            rank = (identifier != preferred, capability not in (operation.get("observedCapabilities") or []),
                    not bool(declared),
                    not bool(engine and engine in known_engines),
                    operation.get("kind") != "mcp", identifier, str(operation.get("name") or ""))
            ranked.append((rank, tool, operation))
    ranked.sort(key=lambda entry: entry[0])
    result = []
    seen = set()
    for unused_rank, tool, operation in ranked:
        identity = operation_identity(tool, operation)
        if identity not in seen:
            seen.add(identity)
            result.append((tool, operation))
    return result


def result_capabilities(payload: Mapping[str, Any] | None) -> set[str]:
    """Evidence from the result, never a tool's name or a hasMaterials flag."""
    if not isinstance(payload, Mapping):
        return set()
    if (payload.get("readerStatus") in {"failed", "invalid-output", "unavailable"}
            or payload.get("parseStatus") == "failed" or payload.get("isError")
            or payload.get("succeeded") is False or payload.get("success") is False
            or payload.get("error")):
        return set()
    found = set()
    if payload.get("kind") not in {None, "", "unknown", "unreadable"} or payload.get("className"):
        found.add("classify")
    records = [payload]
    for key in ("exports", "materials"):
        items = payload.get(key) or []
        if isinstance(items, (list, tuple)):
            records.extend(item for item in items[:64] if isinstance(item, Mapping))
    for record in records:
        if any(isinstance(record.get(key), Mapping) and bool(record[key])
               for key in ("properties", "Properties", "m_SavedProperties", "scalars", "floats",
                           "vectors", "colors", "textures")):
            found.add("properties")
        references = record.get("references") or []
        if isinstance(references, (list, tuple)) and any(isinstance(item, Mapping) and any(item.get(key) for key in
                                                ("objectPath", "targetPath", "pathId", "guid"))
               for item in references):
            found.add("references")
        textures = record.get("textures") or {}
        if isinstance(textures, Mapping) and any(
                isinstance(item, Mapping) and isinstance(item.get("objectReference"), Mapping)
                and item["objectReference"].get("pathId")
                for item in textures.values()):
            found.add("references")
    return found


def capture_fallback(reason: str) -> dict[str, Any]:
    return {"nonblocking": True, "fallback": {"mode": "capture-only", "reasonCode": reason,
            "assetNamesAvailable": False, "inferredNamesMustBeMarked": True,
            "canContinue": ["shader-analysis", "shader-organization", "capture-resource-export"]}}


class ReaderResultCache:
    """Short-lived, bounded reuse and cross-event-loop coalescing of identical reads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[float, dict[str, Any], int]] = OrderedDict()
        self._pending: dict[str, Future] = {}

    @staticmethod
    def key(root: Path, path: Path, tool: Mapping[str, Any], operation: Mapping[str, Any], *,
            context: Mapping[str, Any] | None = None) -> str:
        stat = path.stat()
        # Configuration/proof changes and in-place file rewrites invalidate reuse.
        facts = [str(root.resolve()), str(path.resolve()), stat.st_size, stat.st_mtime_ns,
                 stat.st_ctime_ns, operation_identity(tool, operation), operation.get("proved"), context]
        connection = tool.get("connection") or {}
        inputs = ([connection.get("command")] + list(connection.get("args") or [])
                  + list(operation.get("argv") or []) + list((context or {}).values()))
        for value in inputs:
            text = str(value or "")
            if text and os.path.isfile(text):
                program = Path(text).stat()
                facts.extend((str(Path(text).resolve()), program.st_size, program.st_mtime_ns, program.st_ctime_ns))
        return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()

    def acquire(self, key: str) -> tuple[dict[str, Any] | None, Future, bool]:
        with self._lock:
            item = self._entries.pop(key, None)
            if item and item[0] > time.monotonic():
                self._entries[key] = item
                return copy.deepcopy(item[1]), Future(), False
            pending = self._pending.get(key)
            if pending is not None:
                return None, pending, False
            pending = Future()
            self._pending[key] = pending
            return None, pending, True

    def finish(self, key: str, result: Mapping[str, Any]) -> None:
        payload = copy.deepcopy(dict(result))
        size = len(json.dumps(payload, default=str).encode("utf-8"))
        with self._lock:
            pending = self._pending.pop(key, None)
            if size <= 2 * 1024 * 1024:
                ttl = 120 if result_capabilities(payload) else 15
                self._entries[key] = (time.monotonic() + ttl, payload, size)
                while len(self._entries) > 128 or sum(item[2] for item in self._entries.values()) > 16 * 1024 * 1024:
                    self._entries.popitem(last=False)
        if pending is not None and not pending.done():
            pending.set_result(copy.deepcopy(payload))
