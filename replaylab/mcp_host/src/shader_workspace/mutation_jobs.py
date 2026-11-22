"""Pollable, idempotent jobs for Shader workspace mutations.

MCP clients can abandon a request when their transport deadline expires while
the worker thread continues changing the workspace.  A mutation therefore has
an operation identity and a retained terminal receipt.  Repeating the same
``operation_id`` with the same request joins the existing job; reusing it for a
different request is rejected before either request is run.

This delivery ledger is intentionally bounded and process-local.  Committed
rename/restructure/undo identities and request digests are also written into
the organization session before its atomic commit returns; after a Host restart
the reconstructed manager replays that durable receipt instead of repeating the
workspace effect.  This in-memory view only bridges live client timeouts.
"""

from __future__ import annotations

import re
import hashlib
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .io import canonical_workspace_identity


STATUS_SCHEMA = "replaylab-shader-workspace-mutation/v1"
_MAX_TERMINAL_JOBS = 96
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")

_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_ORDER: list[str] = []
_ACTIVE_BY_TARGET: dict[str, str] = {}


def normalize_operation_id(value: str = "", *, required: bool = False) -> str:
    """Validate a caller operation id, optionally creating one internally."""

    operation_id = str(value or "").strip()
    if not operation_id:
        if required:
            raise ValueError(
                "operation_id is required before starting a mutation so a lost "
                "first response can be recovered idempotently"
            )
        return uuid.uuid4().hex
    if not _OPERATION_ID_RE.fullmatch(operation_id):
        raise ValueError(
            "operation_id must be 8-128 ASCII letters, digits, '.', '_', ':', or '-'"
        )
    return operation_id


def bridge_operation_id(value: str = "") -> str:
    """Keep the native 128-bit protocol while accepting stable caller labels.

    UUID spellings normalize to the same key. Other workspace mutation labels
    get a deterministic namespaced digest, so retries never create a new write.
    """
    operation = str(value or "").strip()
    if not operation:
        return uuid.uuid4().hex
    if len(operation) > 128 or any(ord(char) < 32 for char in operation):
        raise ValueError("operation_id must be a short stable label without control characters")
    try:
        return uuid.UUID(operation).hex
    except ValueError:
        return hashlib.sha256(("bread-shared-shader-operation/v1\0" + operation).encode("utf-8")).hexdigest()[:32]


def _target_key(workspace_path: str, organization_id: str) -> str:
    return "%s::%s" % (
        canonical_workspace_identity(workspace_path),
        str(organization_id or "").strip().casefold(),
    )


def _request_hashes(
    request_sha256: str, compatible_request_sha256: Sequence[str]
) -> tuple[str, frozenset[str]]:
    primary = str(request_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", primary):
        raise ValueError("request_sha256 must be a lowercase SHA-256 digest")
    compatible = tuple(compatible_request_sha256 or ())
    if len(compatible) > 8:
        raise ValueError("compatible_request_sha256 has too many entries")
    accepted = {primary}
    for value in compatible:
        candidate = str(value or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", candidate):
            raise ValueError(
                "compatible_request_sha256 entries must be SHA-256 digests"
            )
        accepted.add(candidate)
    return primary, frozenset(accepted)


def _public_job(
    record: Mapping[str, Any], *, idempotent_replay: bool = False
) -> dict[str, Any]:
    started = float(record.get("startedAt") or time.time())
    updated = float(record.get("updatedAt") or started)
    status = str(record.get("status") or "queued")
    payload: dict[str, Any] = {
        "schema": STATUS_SCHEMA,
        "schemaVersion": 1,
        "operationId": record.get("operationId"),
        "requestSha256": record.get("requestSha256"),
        "tool": record.get("tool"),
        "workspacePath": record.get("workspacePath"),
        "organizationId": record.get("organizationId"),
        "status": status,
        "phase": record.get("phase"),
        "startedAt": started,
        "updatedAt": updated,
        "elapsedSeconds": round(
            max(0.0, (time.time() if status in {"queued", "running"} else updated) - started),
            3,
        ),
        "terminal": status in {"done", "failed"},
        "idempotentReplay": bool(idempotent_replay),
        "sameOperationRetryAllowed": (
            record.get("retryFailedSameOperation") is True
        ),
        "error": record.get("error") or "",
    }
    result = record.get("result")
    if isinstance(result, Mapping):
        payload["result"] = dict(result)
        if type(result.get("accepted")) is bool:
            payload["resultAccepted"] = result["accepted"]
            if result["accepted"] is False:
                payload["rejectionReason"] = str(
                    result.get("reason") or result.get("error") or ""
                )[:400]
    return payload


def _prune_locked() -> None:
    terminal = [
        operation_id
        for operation_id in _ORDER
        if (_JOBS.get(operation_id) or {}).get("status") in {"done", "failed"}
    ]
    excess = max(0, len(terminal) - _MAX_TERMINAL_JOBS)
    for operation_id in terminal[:excess]:
        _JOBS.pop(operation_id, None)
        try:
            _ORDER.remove(operation_id)
        except ValueError:
            pass


def get_shader_mutation_job(operation_id: str) -> dict[str, Any] | None:
    wanted = str(operation_id or "").strip()
    with _LOCK:
        record = _JOBS.get(wanted)
        return _public_job(record) if record is not None else None


def list_active_shader_mutation_jobs() -> list[dict[str, Any]]:
    with _LOCK:
        return [
            _public_job(record)
            for operation_id in _ORDER
            if (record := _JOBS.get(operation_id)) is not None
            and record.get("status") in {"queued", "running"}
        ]


def start_shader_mutation_job(
    *,
    tool: str,
    workspace_path: str,
    organization_id: str,
    request_sha256: str,
    compatible_request_sha256: Sequence[str] = (),
    runner: Callable[[], Mapping[str, Any]],
    operation_id: str = "",
    retry_failed: bool = False,
) -> dict[str, Any]:
    """Start a mutation or return its prior receipt.

    One workspace/organization may have one active mutation.  A repeated
    operation id is joined only when its request digest is identical.
    """

    operation = normalize_operation_id(operation_id)
    request_sha, accepted_request_hashes = _request_hashes(
        request_sha256, compatible_request_sha256
    )
    target = _target_key(workspace_path, organization_id)
    with _LOCK:
        existing = _JOBS.get(operation)
        if existing is not None:
            if str(existing.get("requestSha256") or "") not in accepted_request_hashes:
                raise ValueError(
                    "operation_id is already bound to a different mutation request"
                )
            if existing.get("status") != "failed" or not retry_failed:
                return _public_job(existing, idempotent_replay=True)
            # A caller with its own durable external-operation ledger may safely
            # recover an uncertain bridge response under the same idempotency
            # key. Replace only a terminal failed worker with the identical
            # request digest; generic workspace mutations leave this disabled.
            _JOBS.pop(operation, None)
            try:
                _ORDER.remove(operation)
            except ValueError:
                pass

        active_id = _ACTIVE_BY_TARGET.get(target)
        active = _JOBS.get(active_id or "")
        if active is not None and active.get("status") in {"queued", "running"}:
            busy = {
                "operationId": operation,
                "requestSha256": request_sha,
                "tool": str(tool or ""),
                "workspacePath": str(workspace_path or ""),
                "organizationId": str(organization_id or ""),
                "status": "busy",
                "phase": "not-started",
                "startedAt": time.time(),
                "updatedAt": time.time(),
                "error": "another mutation for this organization is still active",
                "retryFailedSameOperation": False,
            }
            payload = _public_job(busy)
            payload["blockedByOperationId"] = active.get("operationId")
            payload["blockedByTool"] = active.get("tool")
            return payload

        now = time.time()
        record: dict[str, Any] = {
            "operationId": operation,
            "requestSha256": request_sha,
            "tool": str(tool or ""),
            "workspacePath": str(workspace_path or ""),
            "organizationId": str(organization_id or ""),
            "targetKey": target,
            "status": "queued",
            "phase": "queued",
            "startedAt": now,
            "updatedAt": now,
            "error": "",
            "result": None,
            "retryFailedSameOperation": bool(retry_failed),
        }
        _JOBS[operation] = record
        _ORDER.append(operation)
        _ACTIVE_BY_TARGET[target] = operation
        _prune_locked()

    def _run() -> None:
        from ..host_runtime import record_memory_checkpoint

        with _LOCK:
            current = _JOBS.get(operation)
            if current is None:
                return
            current["status"] = "running"
            current["phase"] = "mutating"
            current["updatedAt"] = time.time()
        record_memory_checkpoint(
            "shader-mutation-start",
            tool=str(tool or ""),
            phase="mutating",
            extra={"operationId": operation, "requestSha256": request_sha},
        )
        try:
            result = dict(runner() or {})
            with _LOCK:
                current = _JOBS.get(operation)
                if current is None:
                    return
                current["result"] = result
                current["status"] = "done"
                current["phase"] = "done"
                current["updatedAt"] = time.time()
                current["error"] = ""
        except BaseException as exc:  # keep a receipt even when a worker is cancelled
            with _LOCK:
                current = _JOBS.get(operation)
                if current is None:
                    return
                current["status"] = "failed"
                current["phase"] = "failed"
                current["updatedAt"] = time.time()
                current["error"] = str(exc)[:800]
                current["result"] = {
                    "ok": False,
                    "applied": False,
                    "error": str(exc)[:800],
                    "errorType": type(exc).__name__,
                    "traceback": traceback.format_exc()[-2000:],
                }
        finally:
            with _LOCK:
                current = _JOBS.get(operation)
                if current is not None:
                    current["updatedAt"] = time.time()
                    if _ACTIVE_BY_TARGET.get(target) == operation:
                        _ACTIVE_BY_TARGET.pop(target, None)
                    terminal_status = current.get("status")
                    terminal_error = current.get("error") or ""
                    terminal_result = current.get("result")
                else:
                    terminal_status = "missing"
                    terminal_error = ""
                    terminal_result = None
                _prune_locked()
            result_accepted = (
                terminal_result.get("accepted")
                if isinstance(terminal_result, Mapping)
                and type(terminal_result.get("accepted")) is bool
                else None
            )
            record_memory_checkpoint(
                "shader-mutation-end",
                tool=str(tool or ""),
                phase=str(terminal_status),
                extra={
                    "operationId": operation,
                    "ok": terminal_status == "done",
                    "resultAccepted": result_accepted,
                    "rejectionReason": (
                        str(terminal_result.get("reason") or terminal_result.get("error") or "")[:400]
                        if result_accepted is False else ""
                    ),
                    "error": str(terminal_error)[:400],
                },
            )

    threading.Thread(
        target=_run,
        name="shader-mutation-%s" % operation[:12],
        daemon=True,
    ).start()
    return get_shader_mutation_job(operation) or _public_job(record)


def wait_for_shader_mutation_job(
    operation_id: str, wait_seconds: float
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.0, float(wait_seconds or 0.0))
    while True:
        snapshot = get_shader_mutation_job(operation_id)
        if snapshot is None or snapshot.get("terminal"):
            return snapshot
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return snapshot
        time.sleep(min(0.02, remaining))
