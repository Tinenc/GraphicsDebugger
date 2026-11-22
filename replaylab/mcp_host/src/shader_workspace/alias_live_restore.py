"""Restore display names after saves without delaying shader application.

One daemon per workspace consumes only its newest queued revision. Native owner
checks and project pinning remain inside the worker. Display failures never
revoke an already successful replacement, and project reopen retries them.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
import threading

from .alias_display_mirror import restore_display_alias_mirrors
from .io import atomic_write_json, canonical_workspace_identity

_LOCK = threading.Lock()
_JOBS = {}
_MAX_JOBS = 16


def _run(item):
    from ..ipc_client import bind_direct_tool_call

    host, root, revision, candidate, binding, project = item
    result = {"schema": "bread-cbuffer-display-restore/v1", "revision": revision,
              "candidateRawSha256": candidate, "status": "failed"}
    try:
        host._require_same_active_project(project)
        with bind_direct_tool_call(binding["instanceId"], binding["captureSessionId"]):
            status = host._shader_workspace_service().status(str(root))
            latest = status.get("latestCompiled") or {}
            if latest.get("revision") != revision or latest.get("rawSha256") != candidate:
                return  # A newer save owns the next queued attempt.
            result.update(restore_display_alias_mirrors(
                host, root, status, binding["captureSessionId"], project["projectRoot"]))
    except Exception as error:
        result["diagnostic"] = str(error)[:400]
    result["finishedAt"] = datetime.now(timezone.utc).isoformat()
    # The root was admitted before scheduling. Only a still-current project
    # may write this compatibility diagnostic; names use the transactional store.
    try:
        host._require_same_active_project(project)
        atomic_write_json(root / "ai" / "cbuffer-display-restore.json", result)
    except Exception:
        pass


def _consume(key):
    while True:
        with _LOCK:
            item = _JOBS.get(key)
            if item is None:
                _JOBS.pop(key, None)
                return
            _JOBS[key] = None
        try:
            _run(item)
        except Exception:
            # Keep the lane drainable even if a diagnostic cannot be written.
            pass


def schedule(host, workspace, revision, candidate, binding):
    """Queue successful apply's names, never a pre-apply compiled candidate."""
    project = host._active_project_service().status()
    if project.get("scopeConfigured") is not True:
        return {"status": "not-required", "reason": "no-active-project"}
    root = Path(host._require_managed_active_workspace(project, str(workspace))).resolve()
    item = (host, root, revision, candidate, dict(binding), copy.deepcopy(project))
    key = canonical_workspace_identity(root)
    with _LOCK:
        exists = key in _JOBS
        if not exists and len(_JOBS) >= _MAX_JOBS:
            raise RuntimeError("CBuffer display restore queue is full; project reopen will retry")
        _JOBS[key] = item
        if not exists:
            worker = threading.Thread(target=_consume, args=(key,), name="BreadCBufferDisplayRestore", daemon=True)
            try:
                worker.start()
            except Exception:
                _JOBS.pop(key, None)
                raise
    return {"status": "pending", "reason": "background-display-restore"}
