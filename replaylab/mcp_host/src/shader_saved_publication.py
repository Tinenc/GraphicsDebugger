"""Persist successful editor saves independently of capture/project switching.

Preparation runs once per candidate, never on watcher polls. Only a successful
physical apply makes an intent publishable. Retrying that intent reads immutable
files and writes its original project; it never touches a replay controller.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Any, Callable, Mapping

from .projects import ReplayProjectError, ReplayProjectService
from .shader_workspace.io import atomic_write_bytes, atomic_write_json, is_within, read_json_object

SCHEMA = "bread-saved-shader-publication/v1"
_GUARD = threading.RLock()
_INFLIGHT: dict[str, threading.Event] = {}
_MAX_ITEMS = 128


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8")).hexdigest()


def _write(path: Path, item: dict[str, Any]) -> None:
    item = copy.deepcopy(item)
    item.pop("sealSha256", None)
    item["sealSha256"] = _digest(item)
    atomic_write_json(path, item)


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise RuntimeError("saved Shader publication path is redirected")
    try:
        item = read_json_object(path, 16 * 1024 * 1024, "saved Shader publication")
    except ValueError:
        if not path.exists():
            raise FileNotFoundError(path) from None
        raise
    seal = item.pop("sealSha256", None)
    if seal != _digest(item) or item.get("schema") != SCHEMA:
        raise RuntimeError("saved Shader publication changed")
    return item


def _cleanup(path: Path, item: Mapping[str, Any]) -> None:
    """Remove only this completed intent's bounded, owned snapshot files."""
    root = Path(item["status"]["workspacePath"]).resolve()
    directory = Path(item["artifactDirectory"]).resolve()
    if directory != root / "watch" / "project-publications" / item["token"]:
        raise RuntimeError("saved Shader snapshot cleanup scope changed")
    for relative in item["files"]:
        target = root / relative
        if target.is_symlink() or not is_within(directory, target.resolve()):
            raise RuntimeError("saved Shader snapshot cleanup path changed")
        target.unlink(missing_ok=True)
    if directory.exists():
        directory.rmdir()
    path.unlink(missing_ok=True)


def _head(replay: ReplayProjectService, status: Mapping[str, Any], *,
          manifest: Mapping[str, Any] | None = None,
          catalog_sha256: str | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    reused_manifest = manifest is not None
    if manifest is None:
        manifest = replay._load()
    capture = replay._capture_for_provenance(manifest, status["capture"]["captureId"],
        catalog_sha256 if catalog_sha256 is not None else replay._capture_status_catalog_sha256(status["capture"]))
    shader = replay._capture_shader(capture, resource_id=status["shader"]["resourceId"],
        stage=status["stage"], entry_point=status.get("entryPoint", ""), raw_sha256=status["baseline"]["rawSha256"])
    material = {**shader, "captureId": capture["captureId"], "originCatalogSha256": capture["catalogSha256"],
        "originResourceId": status["shader"]["resourceId"], "stage": status["stage"],
        "entryPoint": status.get("entryPoint", ""), "rawSha256": status["baseline"]["rawSha256"],
        "baselineAbiSha256": status["baseline"].get("abiSha256", ""),
        "sourceFormat": status["source"]["format"], "targetProfile": status["targetProfile"]}
    key = replay._workspace_head_key(material, manifest)
    head = replay._active_heads_state(manifest)["workspaces"].get(key)
    if head is None:
        return {"headKey": key, "generation": 0, "knowledgeId": ""}, None
    return ({"headKey": key, "generation": head["generation"], "knowledgeId": head["knowledgeId"]},
            replay._workspace_knowledge_from_manifest(manifest, head["knowledgeId"]) if reused_manifest
            else replay.get_workspace_knowledge(head["knowledgeId"]))


def _manual_project_snapshot(active_service: Any, status: Mapping[str, Any],
                             capture_binding: Mapping[str, str]):
    """Bind a prepared save locally; the ensuing native apply decides its session.

    A frozen watcher binding is not ABI authority. Its apply response must still
    match this exact source/candidate/original/session before complete() can
    publish. Re-probing the live capture several times here adds IPC/replay
    latency without strengthening that final transaction check.
    """
    path_key = lambda value: os.path.normcase(os.path.abspath(str(value)))
    capture = status["capture"]
    if path_key(capture.get("capturePath", "")) != path_key(capture_binding.get("capturePath", "")):
        raise RuntimeError("saved Shader publication capture path changed")
    replay = active_service.replay_service()
    manifest = replay._load()
    catalog_sha256 = replay._capture_status_catalog_sha256(capture)
    try:
        replay._capture_for_provenance(manifest, capture["captureId"], catalog_sha256)
    except ReplayProjectError as error:
        if str(error) not in {"capture_id is not imported in this project",
                              "catalog revision is not imported in this project"}:
            raise
        active_service.import_capture(capture["catalogPath"],
            expected_capture_id=capture["captureId"], expected_capture_path=capture["capturePath"],
            expected_api=capture["api"])
        manifest = replay._load()
        replay._capture_for_provenance(manifest, capture["captureId"], catalog_sha256)
    return replay, manifest, catalog_sha256


def prepare(host: Any, workspace_path: str, revision: int, candidate: str,
            capture_binding: Mapping[str, str], *, manual_edit: bool = False) -> Path | None:
    """Freeze portable publication evidence before the owning capture can close."""
    active_service = host._active_project_service()
    active = active_service.status()
    if active.get("scopeConfigured") is not True:
        return None
    root = Path(host._require_managed_active_workspace(active, workspace_path)).resolve()
    service = host._shader_workspace_service()
    expected = {"revision": revision, "candidateRawSha256": candidate}
    status = service.status(str(root))
    source_sha = host._require_shader_candidate_binding(status, expected)
    latest = status["latestCompiled"]
    if not manual_edit and (latest.get("replayAbiValidated") is not True
            or latest.get("validationScope") != "replay-reflection-abi-compatible"):
        unused_service, status, unused_request, validation = host._fresh_shader_candidate_validation(
            str(root), revision, candidate)
        if (validation.get("replayAbiValidated") is not True
                or validation.get("captureSessionId") != capture_binding["captureSessionId"]):
            raise RuntimeError("saved Shader publication has no current capture ABI proof")
    if host._require_shader_candidate_binding(status, expected) != source_sha:
        raise RuntimeError("saved Shader changed before publication preparation")
    with active_service.pin_active_project(active["workspaceId"], active["projectRoot"]):
        if manual_edit:
            replay, manifest, catalog_sha256 = _manual_project_snapshot(active_service, status, capture_binding)
            head, unused_knowledge = _head(replay, status, manifest=manifest, catalog_sha256=catalog_sha256)
        else:
            context = host._workspace_active_capture_context(str(root), status=status)
            if (context.get("capture", {}).get("captureSessionId") != capture_binding["captureSessionId"]):
                raise RuntimeError("saved Shader publication capture changed")
            host._ensure_active_workspace_project_capture(active_service, status, context)
            replay = active_service.replay_service()
            manifest = replay._load()
            head, unused_knowledge = _head(replay, status)
        queue = replay.root / "derived" / "pending-shader-publications"
        queue.mkdir(parents=True, exist_ok=True)
        if queue.resolve() != queue or len(list(queue.glob("*.json"))) >= _MAX_ITEMS:
            raise RuntimeError("saved Shader publication queue is unavailable or full")
        token = uuid.uuid4().hex
        directory = root / "watch" / "project-publications" / token
        directory.mkdir(parents=True, exist_ok=False)
        if directory.resolve() != directory:
            raise RuntimeError("saved Shader publication snapshot is redirected")
        frozen = copy.deepcopy(status)
        files: dict[str, str] = {}

        def freeze(path: Path, name: str, maximum: int) -> Path:
            if path.is_symlink() or not is_within(root, path.resolve()) or path.stat().st_size > maximum:
                raise RuntimeError("saved Shader publication artifact is redirected or oversized")
            raw = path.read_bytes()
            if len(raw) > maximum:
                raise RuntimeError("saved Shader publication artifact is oversized")
            destination = directory / name
            atomic_write_bytes(destination, raw)
            files[destination.relative_to(root).as_posix()] = hashlib.sha256(raw).hexdigest()
            return destination

        try:
            frozen["source"]["path"] = str(freeze(Path(status["source"]["path"]), "source." + status["source"]["format"], 64 * 1024 * 1024))
            latest = frozen["latestCompiled"]
            blob = freeze(Path(latest["candidateBlobPath"]), "candidate.bin", 512 * 1024 * 1024)
            latest["candidateBlobPath"], latest["blobPath"] = str(blob), blob.relative_to(root).as_posix()
            compiled_source = freeze(root / latest["sourcePath"], "compiled-source.txt", 64 * 1024 * 1024)
            latest["sourcePath"] = compiled_source.relative_to(root).as_posix()
            if not manual_edit:
                report = freeze(root / latest["abiGate"]["reportPath"], "abi.json", 16 * 1024 * 1024)
                latest["abiGate"]["reportPath"] = report.relative_to(root).as_posix()
            for index, row in enumerate(frozen.get("assemblyDisassemblyArtifacts") or []):
                row["path"] = str(freeze(Path(row["path"]), "assembly-%d.txt" % index, 64 * 1024 * 1024))
            original_manifest = read_json_object(Path(status["manifestPath"]), 16 * 1024 * 1024, "Shader manifest")
            persisted = dict(latest)
            persisted.pop("candidateBlobPath", None)
            persisted.pop("applied", None)
            original_manifest["latestCompiled"] = persisted
            frozen_manifest = directory / "manifest.json"
            atomic_write_json(frozen_manifest, original_manifest)
            files[frozen_manifest.relative_to(root).as_posix()] = hashlib.sha256(frozen_manifest.read_bytes()).hexdigest()
            frozen["manifestPath"] = str(frozen_manifest)
            if host._require_shader_candidate_binding(service.status(str(root)), expected) != source_sha:
                raise RuntimeError("saved Shader changed while publication artifacts were frozen")
            item = {"schema": SCHEMA, "token": token, "state": "prepared", "createdAt": time.time(),
                "projectRoot": str(Path(active["projectRoot"]).resolve()), "projectId": manifest["projectId"],
                "scopeSha256": _digest(manifest["scope"]), "replayProject": str(replay.root),
                "captureBinding": dict(capture_binding), "expectedHead": head, "status": frozen,
                "artifactDirectory": str(directory), "files": files, "ownerPid": os.getpid()}
            if manual_edit:
                item["manualEdit"] = True
            path = queue / (token + ".json")
            with _GUARD:
                _write(path, item)
                _INFLIGHT[str(path)] = threading.Event()
            return path
        except Exception:
            # A source/CAS failure before native apply has no durable intent.
            # Remove only the snapshot files created by this preparation.
            try:
                _cleanup(queue / (token + ".json"), {
                    "status": {"workspacePath": str(root)}, "token": token,
                    "artifactDirectory": str(directory), "files": files})
            except (OSError, RuntimeError):
                pass
            raise



def cancel(path: Path | None) -> None:
    if path is None:
        return
    with _GUARD:
        item = _read(path)
        item["state"] = "cancelled"
        _write(path, item)
        event = _INFLIGHT.pop(str(path), None)
        if event is not None:
            event.set()
        _cleanup(path, item)


def complete(path: Path, result: Mapping[str, Any]) -> dict[str, Any]:
    """Make an exact successful native transaction durable before retryable IO."""
    try:
        with _GUARD:
            item = _read(path)
            status = item["status"]
            replacement, binding = result.get("replacement") or {}, result.get("binding") or {}
            manual_receipt = None
            if item.get("manualEdit") is True:
                from .project_manual_shader import from_apply
                try:
                    manual_receipt = from_apply(result, status)
                except (ValueError, KeyError, TypeError):
                    item["state"] = "cancelled"
                    _write(path, item)
                    raise
            if (result.get("schema") != "renderdoc-ultra-shader-shared-resource-apply-mcp/v1"
                    or result.get("applied") is not True or result.get("scope") != "captured-resource"
                    or (manual_receipt is None and result.get("validation", {}).get("replayAbiValidated") is not True)
                    or binding.get("revision") != status["revision"]
                    or binding.get("sourceSha256") != status["source"]["currentSha256"]
                    or binding.get("candidateRawSha256") != status["latestCompiled"]["rawSha256"]
                    or replacement.get("candidateRawSha256") != binding["candidateRawSha256"]
                    or replacement.get("originalRawSha256") != status["baseline"]["rawSha256"]
                    or replacement.get("originalResourceId") != status["shader"]["resourceId"]
                    or replacement.get("originalShaderPreserved") is not True
                    or replacement.get("resourceScoped") is not True or replacement.get("eventScoped") is not False
                    or replacement.get("allOriginalResourceUsesAffected") is not True
                    or replacement.get("captureSessionId") != item["captureBinding"]["captureSessionId"]):
                item["state"] = "cancelled"
                _write(path, item)
                raise RuntimeError("saved Shader publication does not match a successful native apply")
            if manual_receipt is not None:
                item["status"]["manualApplyReceipt"] = manual_receipt
            item["state"] = "ready"
            _write(path, item)
        return _publish(path)
    finally:
        with _GUARD:
            event = _INFLIGHT.pop(str(path), None)
            if event is not None:
                event.set()


def _publish(path: Path) -> dict[str, Any]:
    with _GUARD:
        item = _read(path)
        if item["state"] != "ready":
            return {"status": item["state"]}
        replay = ReplayProjectService(item["replayProject"])
        if path.parent != replay.root / "derived" / "pending-shader-publications":
            raise RuntimeError("saved Shader publication belongs to another project")
        manifest = replay._load()
        if manifest["projectId"] != item["projectId"] or _digest(manifest["scope"]) != item["scopeSha256"]:
            raise RuntimeError("saved Shader publication project scope changed")
        status = item["status"]
        root = Path(status["workspacePath"]).resolve()
        directory = Path(item["artifactDirectory"]).resolve()
        if (not is_within(Path(item["projectRoot"]).resolve(), root)
                or directory != root / "watch" / "project-publications" / item["token"]):
            raise RuntimeError("saved Shader publication artifact scope changed")
        for relative, digest in item["files"].items():
            artifact = root / relative
            if (artifact.is_symlink() or not is_within(directory, artifact.resolve())
                    or artifact.stat().st_size > 512 * 1024 * 1024
                    or hashlib.sha256(artifact.read_bytes()).hexdigest() != digest):
                raise RuntimeError("saved Shader publication artifact changed")
        head, knowledge = _head(replay, status)
        source_sha, candidate = status["source"]["currentSha256"], status["latestCompiled"]["rawSha256"]
        already_current = bool(knowledge and knowledge.get("sourceSha256") == source_sha
                               and (knowledge.get("compiledCandidate") or {}).get("rawSha256") == candidate)
        superseded = False
        if knowledge and head != item["expectedHead"] and not already_current:
            same_workspace = knowledge.get("workspaceId") == status["workspaceId"]
            current_revision = (knowledge.get("compiledCandidate") or {}).get("sourceRevision", 0)
            superseded = not same_workspace or current_revision >= status["revision"]
        if not already_current and not superseded:
            receipt = replay.register_workspace(status, expected_head=head, include_analysis=False, require_current_candidate=True)
            knowledge = replay.get_workspace_knowledge(receipt["knowledge"]["knowledgeId"])
            if knowledge["sourceSha256"] != source_sha or knowledge["compiledCandidate"]["rawSha256"] != candidate:
                raise RuntimeError("saved Shader publication readback changed")
        item["state"] = "superseded" if superseded else "committed"
        item["knowledgeId"] = knowledge["knowledgeId"]
        _write(path, item)
        receipt = {"status": item["state"], "knowledgeId": item["knowledgeId"]}
        try:
            _cleanup(path, item)
        except OSError as exc:
            receipt["message"] = "saved source committed; snapshot cleanup pending: %s" % str(exc)[:320]
        return receipt


def drain(project_root: Path, replay_path: Path, checkpoint: Callable[[str], None] | None = None) -> dict[str, int]:
    """Catch up successful saves before restore chooses any project source head."""
    queue = Path(replay_path).resolve() / "derived" / "pending-shader-publications"
    result = {"committed": 0, "superseded": 0, "cancelled": 0}
    if not queue.exists():
        return result
    if queue.is_symlink():
        raise RuntimeError("saved Shader publication queue is redirected")
    paths = list(queue.glob("*.json"))
    if len(paths) > _MAX_ITEMS:
        raise RuntimeError("saved Shader publication queue exceeds its bound")
    ordered = []
    for path in paths:
        try:
            ordered.append((_read(path)["createdAt"], path))
        except FileNotFoundError:
            continue
    for unused_created_at, path in sorted(ordered):
        with _GUARD:
            event = _INFLIGHT.get(str(path))
        if event is not None:
            deadline = time.monotonic() + 30.0
            while not event.wait(0.05):
                if checkpoint:
                    checkpoint("waiting for the preceding saved Shader publication")
                if time.monotonic() >= deadline:
                    raise RuntimeError("preceding saved Shader apply is still finishing; retry project restore")
        try:
            item = _read(path)
        except FileNotFoundError:
            continue
        if Path(item["projectRoot"]).resolve() != Path(project_root).resolve():
            raise RuntimeError("saved Shader publication belongs to another project root")
        if item["state"] == "prepared":
            # Another Host process can still be finishing a valid transaction.
            # Wait for its durable state instead of treating its draft as an
            # abandoned save or choosing an older project head.
            from .ipc_client import IPCClient
            owner = item.get("ownerPid")
            deadline = time.monotonic() + 30.0
            while (item["state"] == "prepared" and isinstance(owner, int)
                    and owner != os.getpid() and IPCClient._process_alive(owner)):
                if checkpoint:
                    checkpoint("waiting for the preceding saved Shader publication")
                if time.monotonic() >= deadline:
                    raise RuntimeError("preceding saved Shader Host is still finishing; retry project restore")
                time.sleep(0.05)
                try:
                    item = _read(path)
                except FileNotFoundError:
                    item = None
                    break
            if item is None:
                continue
        if item["state"] == "prepared":
            # Recover an interrupted Host only from its exact persisted Native
            # pair receipt; an un-applied compiled draft remains unpublishable.
            pair = None
            if item.get("manualEdit") is not True:
                from .project_restore import _load_workspace_shader_pair_validation
                pair = _load_workspace_shader_pair_validation(item["status"])
            if pair is not None:
                item["state"] = "ready"
                _write(path, item)
            else:
                item["state"] = "cancelled"
                _write(path, item)
        if item["state"] == "ready":
            receipt = _publish(path)
            result[receipt["status"]] += 1
        elif item["state"] in {"cancelled", "committed", "superseded"}:
            _cleanup(path, item)
            result[item["state"]] += 1
    return result
