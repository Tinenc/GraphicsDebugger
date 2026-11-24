"""Bounded compilation-unit dependencies and editable include working copies.

Paths are identities. We follow explicit include edges, never search other Shader
workspaces or directories for a matching basename.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from contextlib import nullcontext
import base64

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_UNIT_BYTES = 16 * 1024 * 1024
MAX_UNIT_DOCUMENTS = 64
MAX_INCLUDE_DEPTH = 16
_SOURCE_SUFFIXES = {".hlsl", ".hlsli", ".fx", ".fxh", ".glsl", ".vert", ".frag",
                    ".comp", ".geom", ".tesc", ".tese", ".h", ".inc"}
_DIRECTIVE = re.compile(r"(?m)^[ \t]*#[ \t]*(include|define|undef|if|ifdef|ifndef|elif|else|endif)\b([^\r\n]*)")
_LITERAL = re.compile(r'^[ \t]*(["<])([^"<>\r\n]+)[">][ \t]*$')
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*\Z")


def _key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path) -> bytes:
    if path.suffix.lower() not in _SOURCE_SUFFIXES:
        raise ValueError("Shader dependency is not a supported source document")
    if path.is_symlink() or not path.is_file():
        raise ValueError("Shader dependency is missing or redirected: %s" % path)
    before = path.stat()
    if not 0 <= before.st_size <= MAX_DOCUMENT_BYTES:
        raise ValueError("Shader dependency exceeds its source size limit")
    raw = path.read_bytes()
    after = path.stat()
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ValueError("Shader dependency changed while it was read")
    if b"\0" in raw:
        raise ValueError("Shader dependency contains NUL")
    raw.decode("utf-8", errors="strict")
    return raw


def _without_comments(text: str) -> str:
    # Preserve offsets/newlines and quoted include tokens. Comments do not
    # authorize filesystem access and a commented-out include is not an edge.
    pattern = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/')
    return pattern.sub(lambda m: m.group() if m.group().startswith('"') else
                       ''.join('\n' if c == '\n' else '\r' if c == '\r' else ' '
                               for c in m.group()), text)


def capture_source_unit(source_path: str | Path, source_override: bytes | None = None, *,
                        document_reader: Callable[[Path], bytes] | None = None,
                        allow_incomplete: bool = False) -> dict[str, Any]:
    """Read only this unit's explicit include closure, bounded by files/bytes/depth.

    The returned raw bytes are immutable observations for a compile/save operation.
    A source override represents the caller's main working buffer, not arbitrary
    replacement include files. Unsupported dynamic include expressions fail clearly.
    Editing may request a partial closure to reach already explicit managed files;
    its empty digest is never a compilation proof or an external relocation plan.
    """
    root = Path(source_path).absolute()
    records: dict[str, dict[str, Any]] = {}
    macros: dict[str, str] = {}
    total = 0
    reader = document_reader or _read
    errors = []

    def visit(path: Path, depth: int, override: bytes | None = None, ambiguous=False):
        nonlocal total
        key = _key(path)
        if key in records:
            pending, seen = [key], set()
            while pending:
                target_key = pending.pop()
                if target_key in seen or target_key not in records:
                    continue
                seen.add(target_key)
                previous = records[target_key]
                if previous.get("macroInclude"):
                    raise ValueError("Repeated Shader include has a context-dependent macro include")
                pending.extend(_key(edge["target"]) for edge in previous["includes"])
            # Repeated inclusion can have macro side effects. A lightweight
            # closure reader cannot assume an include guard was active.
            for target_key in seen:
                for directive in _DIRECTIVE.finditer(_without_comments(records[target_key]["raw"].decode("utf-8"))):
                    if directive.group(1) in ("define", "undef"):
                        parts = directive.group(2).strip().split(None, 1)
                        if parts:
                            macros.pop(parts[0], None)
            return
        if depth > MAX_INCLUDE_DEPTH or len(records) >= MAX_UNIT_DOCUMENTS:
            raise ValueError("Shader include closure exceeds its fixed depth/file limit")
        raw = override if override is not None else reader(path)
        if (not isinstance(raw, bytes) or (depth == 0 and not raw) or
                len(raw) > MAX_DOCUMENT_BYTES or b"\0" in raw):
            raise ValueError("Shader source document is invalid")
        text = raw.decode("utf-8", errors="strict")
        total += len(raw)
        if total > MAX_UNIT_BYTES:
            raise ValueError("Shader include closure exceeds its total byte limit")
        record = {"path": str(path), "raw": raw, "sha256": _sha(raw), "includes": []}
        records[key] = record
        clean = _without_comments(text)
        conditions = []

        def constant_condition(expression):
            reduced = re.sub(r"[() \t]", "", expression)
            return {"0": False, "1": True, "!0": True, "!1": False}.get(reduced)

        for directive in _DIRECTIVE.finditer(clean):
            value = directive.group(2).strip()
            kind = directive.group(1)
            active = all(item["active"] for item in conditions)
            if kind in ("if", "ifdef", "ifndef"):
                condition = constant_condition(value) if kind == "if" else None
                conditions.append({"parent": active, "taken": condition,
                                   "active": active and condition is not False,
                                   "ambiguous": condition is None})
                continue
            if kind == "endif":
                if conditions:
                    conditions.pop()
                continue
            if kind in ("elif", "else"):
                if conditions:
                    state = conditions[-1]
                    condition = constant_condition(value) if kind == "elif" else True
                    selected = (False if state["taken"] is True else
                                None if state["taken"] is None else condition)
                    state["active"] = state["parent"] and selected is not False
                    state["ambiguous"] = selected is None
                    state["taken"] = (True if state["taken"] is True or selected is True else
                                      None if state["taken"] is None or selected is None else False)
                continue
            if not active:
                continue
            if kind == "undef":
                macros.pop(value, None)
                continue
            if kind == "define":
                parts = value.split(None, 1)
                if parts:
                    macros.pop(parts[0], None)
                if (not ambiguous and not any(item["ambiguous"] for item in conditions) and len(parts) == 2 and
                        _IDENTIFIER.fullmatch(parts[0]) and _LITERAL.fullmatch(parts[1])):
                    macros[parts[0]] = parts[1]
                continue
            expression = value
            if _IDENTIFIER.fullmatch(expression):
                record["macroInclude"] = True
                expression = macros.get(expression, expression)
            literal = _LITERAL.fullmatch(expression)
            if literal is None or expression.rstrip()[-1] != ('"' if literal.group(1) == '"' else '>'):
                if allow_incomplete:
                    errors.append("Shader include expression cannot be resolved explicitly: %s" % value[:160])
                    continue
                raise ValueError("Shader include expression cannot be resolved explicitly: %s" % value[:160])
            name = literal.group(2)
            target = Path(name)
            if not target.is_absolute():
                target = path.parent / target
                if literal.group(1) == '<':
                    try:
                        reader(target)
                    except (ValueError, OSError, KeyError):
                        target = root.parent / name
            target = Path(os.path.abspath(str(target)))
            # Rewrite the entire directive argument, including macro spellings,
            # so snapshot input is self-contained and uses an exact path.
            start, end = directive.span(2)
            record["includes"].append({"start": start, "end": end, "target": str(target),
                                       "line": text.count('\n', 0, start) + 1})
            try:
                visit(target, depth + 1, ambiguous=ambiguous or any(item["ambiguous"] for item in conditions))
            except (ValueError, OSError, KeyError) as ex:
                if not allow_incomplete:
                    raise
                errors.append(str(ex))

    visit(root, 0, source_override)
    documents = list(records.values())
    edges = [(item["path"], edge["target"], edge["line"])
             for item in documents for edge in item["includes"]]
    # Main bytes already have their own source SHA. Dependence identity includes
    # exact path resolution and every header's bytes, even if the main did not change.
    projection = {"edges": edges, "files": sorted(
        (_key(item["path"]), item["sha256"]) for item in documents if _key(item["path"]) != _key(root))}
    digest = _sha(json.dumps(projection, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")) if edges else ""
    result = {"rootPath": str(root), "documents": documents, "digest": digest}
    if errors:
        result.update(complete=False, errors=errors, digest="")
    return result


def rewrite_includes(document: Mapping[str, Any], mapping: Mapping[str, str | Path]) -> bytes:
    """Redirect exact include edges; preserve all remaining bytes and line positions."""
    paths = {_key(path): str(target).replace('\\', '/') for path, target in mapping.items()}
    text = document["raw"].decode("utf-8")
    for edge in reversed(document["includes"]):
        target = paths.get(_key(edge["target"]))
        if target is not None:
            if '"' in target or '\n' in target or '\r' in target:
                raise ValueError("Shader include target path is invalid")
            text = text[:edge["start"]] + ' "' + target + '"' + text[edge["end"]:]
    return text.encode("utf-8")


def _inside(parent: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _managed_path(root: Path, relative: str) -> Path:
    path = root / relative
    if not _inside(root / "source", path) or path.is_symlink():
        raise ValueError("Shader source document escapes its working source tree")
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("Shader source document uses a redirected directory")
    return path


def _receipt_path(root: Path, document_id: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{64}", document_id) is None:
        raise ValueError("Shader source document identity is invalid")
    folder = root / "ai" / "source-documents"
    if not _inside(root, folder) or folder.is_symlink() or folder.parent.is_symlink():
        raise ValueError("Shader source document registry is redirected")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / (document_id + ".json")


def _external_mapping(root: Path, unit) -> dict[str, Path]:
    if unit.get("complete") is False:
        raise ValueError("Incomplete Shader includes cannot be relocated into a working copy")
    # An unopened external revision must not inherit an older edited copy. Once
    # attached, the managed path itself is stable and subsequent edits use CAS.
    return {item["path"]: root / "source" / "includes" /
            _sha((_key(item["path"]) + "\0" + unit["digest"]).encode())[:24] /
            Path(item["path"]).name for item in unit["documents"]
            if not _inside(root / "source", Path(item["path"]))}


def _result(root, status, source_path, document_id, receipt, *, changed=False):
    raw = _read(source_path)
    info = source_path.stat()
    shader = status["shader"]
    return {"schema": "bread-shader-source-document/v1", "sourceOnly": True,
            "compileScheduled": False, "readOnly": False, "sourceEditable": True,
            "workspacePath": str(root), "sourceWorkspaceId": status["workspaceId"],
            "sourceDocumentId": document_id, "documentKind": "source-include",
            "sourcePath": str(source_path), "sourceRelativePath": str(source_path.relative_to(root)),
            "originPath": receipt["originPath"], "rootSourcePath": status["source"]["path"],
            "rootSourceSha256": status["source"]["currentSha256"],
            "fileName": source_path.name,
            "rawSha256": _sha(raw), "size": len(raw), "modifiedNs": info.st_mtime_ns,
            "captureId": status["capture"]["captureId"],
            "capturePath": status["capture"].get("capturePath", ""),
            "stage": shader["stage"], "entryPoint": shader["entryPoint"],
            "resourceId": shader["resourceId"], "changed": changed,
            "mutationPaths": receipt.get("mutationPaths", []),
            "receiptPath": str(_receipt_path(root, document_id))}


def _recover_document_save(root: Path):
    from .organization import _source_compare_and_swap
    journal = root / "ai" / "source-document-save.json"
    if not journal.exists():
        return
    if (journal.is_symlink() or journal.parent.is_symlink() or
            not _inside(root, journal) or journal.stat().st_size > 64 * 1024 * 1024):
        raise ValueError("Shader include save journal is invalid")
    value = json.loads(journal.read_text(encoding="utf-8"))
    if value.get("schema") != "bread-shader-source-document-save/v1" or len(value.get("files", [])) > MAX_UNIT_DOCUMENTS:
        raise ValueError("Shader include save journal identity is invalid")
    for entry in reversed(value["files"]):
        target = _managed_path(root, entry["path"])
        before = base64.b64decode(entry["before"], validate=True)
        after = base64.b64decode(entry["after"], validate=True)
        if len(before) > MAX_DOCUMENT_BYTES or len(after) > MAX_DOCUMENT_BYTES:
            raise ValueError("Shader include save journal source is too large")
        current = _read(target)
        if current == before:
            continue
        if current != after or not _source_compare_and_swap(target, after, before, "Shader include save recovery"):
            raise ValueError("Shader include save recovery found a newer edit; its working buffer was preserved")
    journal.unlink()


def _recover_guarded_document_save(root: Path, commit_guard):
    # Recovery mutates source files too. A queued request from a previous project
    # must not roll them back before its project/capture identity is checked.
    if (root / "ai" / "source-document-save.json").exists():
        with commit_guard() if commit_guard is not None else nullcontext():
            _recover_document_save(root)


def open_source_document(manager, workspace_path: str, source_workspace_id: str,
                         requested_path: str, *, commit_guard=None) -> dict[str, Any]:
    from .organization import _draft_status
    from .source_observation import source_observation_transaction
    from .io import atomic_write_bytes, atomic_write_json
    with manager._guard, source_observation_transaction(workspace_path):
        status = _draft_status(manager._service_factory(), workspace_path)
        root, source, unused, unused_sha = manager._workspace_source(status)
        if status["workspaceId"] != source_workspace_id:
            raise ValueError("Shader source workspace identity changed")
        _recover_guarded_document_save(root, commit_guard)
        status = _draft_status(manager._service_factory(), workspace_path)
        root, source, unused, unused_sha = manager._workspace_source(status)
        unit = capture_source_unit(source, allow_incomplete=True)
        mapping = _external_mapping(root, unit) if unit.get("complete") is not False else {}
        target = next((item for item in unit["documents"] if
                       _key(item["path"]) == _key(requested_path) or
                       (item["path"] in mapping and _key(mapping[item["path"]]) == _key(requested_path))), None)
        if target is None or _key(target["path"]) == _key(source):
            raise ValueError("Definition is not an explicit include of this Shader compilation unit")
        if unit.get("complete") is False and not _inside(root / "source", Path(target["path"])):
            raise ValueError("Incomplete Shader includes cannot be relocated into a working copy")
        source_root = root / "source"
        working = mapping.get(target["path"], Path(target["path"]))
        document_id = _sha((source_workspace_id + "\0" + _key(working)).encode())
        mutation_paths = [item["path"] for item in unit["documents"]
                          if _inside(source_root, Path(item["path"])) and
                          any(edge["target"] in mapping for edge in item["includes"])]
        receipt = {"schema": "bread-shader-source-document/v1", "sourceWorkspaceId": source_workspace_id,
                   "sourceDocumentId": document_id, "originPath": target["path"],
                   "sourceRelativePath": str(working.relative_to(root)),
                   "rootSourceSha256": unit["documents"][0]["sha256"],
                   "dependencySha256": unit["digest"], "mutationPaths": mutation_paths,
                   "externalCopies": {key: str(path.relative_to(root)) for key, path in mapping.items()}}
        with commit_guard() if commit_guard is not None else nullcontext():
            for item in unit["documents"]:
                destination = mapping.get(item["path"])
                if destination is None:
                    continue
                destination = _managed_path(root, str(destination.relative_to(root)))
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    relative = {key: os.path.relpath(value, destination.parent) for key, value in mapping.items()}
                    # Internal dependencies are not relocated; retain their exact
                    # origin by rewriting them relative to the new working file.
                    relative.update({entry["path"]: os.path.relpath(entry["path"], destination.parent)
                                     for entry in unit["documents"] if entry["path"] not in mapping})
                    atomic_write_bytes(destination, rewrite_includes(item, relative))
            atomic_write_json(_receipt_path(root, document_id), receipt)
        return _result(root, status, working, document_id, receipt)


def save_source_document(manager, workspace_path: str, source_workspace_id: str,
                         document_id: str, expected_raw_sha256: str,
                         expected_source_sha256: str, text: str, *, commit_guard=None) -> dict[str, Any]:
    from .organization import _draft_status, _source_compare_and_swap
    from .source_observation import source_observation_transaction
    from .io import atomic_write_json
    from .errors import WorkspaceWatchConflictError
    replacement = text.encode("utf-8", errors="strict")
    if b"\0" in replacement or len(replacement) > MAX_DOCUMENT_BYTES:
        raise ValueError("Shader include source is invalid")
    with manager._guard, source_observation_transaction(workspace_path):
        status = _draft_status(manager._service_factory(), workspace_path)
        root, source, root_bytes, root_sha = manager._workspace_source(status)
        if status["workspaceId"] != source_workspace_id:
            raise WorkspaceWatchConflictError("Shader source workspace identity changed")
        _recover_guarded_document_save(root, commit_guard)
        status = _draft_status(manager._service_factory(), workspace_path)
        root, source, root_bytes, root_sha = manager._workspace_source(status)
        path = _receipt_path(root, document_id)
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError("Shader include document receipt is invalid")
        receipt = json.loads(path.read_text(encoding="utf-8"))
        working = _managed_path(root, receipt["sourceRelativePath"])
        if (receipt.get("sourceWorkspaceId") != source_workspace_id or
                receipt.get("sourceDocumentId") != document_id or
                _sha((source_workspace_id + "\0" + _key(working)).encode()) != document_id):
            raise ValueError("Shader include document identity changed")
        current = _read(working)
        if current == replacement and root_sha == expected_source_sha256:
            return _result(root, status, working, document_id, receipt)
        if root_sha != expected_source_sha256 or _sha(current) != expected_raw_sha256:
            raise WorkspaceWatchConflictError("The source changed on disk; reload before saving the include.")
        unit = capture_source_unit(source, allow_incomplete=True)
        known = {_key(item["path"]): item for item in unit["documents"]}
        external = _key(working) not in known
        if external:
            if (unit.get("complete") is False or _key(receipt["originPath"]) not in known or
                    receipt.get("rootSourceSha256") != root_sha or receipt.get("dependencySha256") != unit["digest"]):
                raise WorkspaceWatchConflictError("The include origin or its parent changed; reload its working copy.")
        existing = manager.open_session(root, status, recover=False)
        if existing is not None:
            proposal = root / existing["proposalSourcePath"]
            if _sha(proposal.read_bytes()) != root_sha:
                raise WorkspaceWatchConflictError("A newer AI proposal exists; save or merge it before editing includes.")
        changes = {working: (current, replacement)}
        if external:
            mapping = _external_mapping(root, unit)
            if (receipt.get("externalCopies") != {
                    key: str(value.relative_to(root)) for key, value in mapping.items()} or
                    mapping.get(receipt["originPath"]) != working):
                raise ValueError("Shader include working-copy mapping changed")
            for item in unit["documents"]:
                parent = Path(item["path"])
                if not _inside(root / "source", parent):
                    continue
                redirected = rewrite_includes(item, {
                    key: os.path.relpath(value, parent.parent) for key, value in mapping.items()})
                if redirected != item["raw"]:
                    if parent == working:
                        raise WorkspaceWatchConflictError("Include relocation conflicts with this edit")
                    changes[parent] = (item["raw"], redirected)
        with commit_guard() if commit_guard is not None else nullcontext():
            # Invalidate publication before any header changes; watchers cannot
            # publish under the same source-observation lease. Newer AI proposals
            # were checked above and are never discarded here.
            manager.begin(workspace_path, editable_source_only=True)
            journal = root / "ai" / "source-document-save.json"
            atomic_write_json(journal, {"schema": "bread-shader-source-document-save/v1", "files": [
                {"path": str(item.relative_to(root)), "before": base64.b64encode(before).decode(),
                 "after": base64.b64encode(after).decode()}
                for item, (before, after) in changes.items()]})
            try:
                for item, (before, after) in changes.items():
                    if before != after and not _source_compare_and_swap(item, before, after, "Shader include source"):
                        raise WorkspaceWatchConflictError("An include or its parent changed during save.")
                manager.begin(workspace_path, restart=True, editable_source_only=True)
                journal.unlink()
            except Exception:
                _recover_document_save(root)
                raise
            status = _draft_status(manager._service_factory(), workspace_path)
            receipt.update(rootSourceSha256=status["source"]["currentSha256"], mutationPaths=[])
            atomic_write_json(path, receipt)
        return _result(root, status, working, document_id, receipt, changed=True)
