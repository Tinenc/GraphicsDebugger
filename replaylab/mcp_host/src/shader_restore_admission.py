"""Process-local reuse for one restore and its ordered editor startup.

Nothing here is a protocol field or a durable execution authorization. Changed
files fall back to the ordinary validator; compile/apply workers do not inherit
this context. A dispatcher belongs to the Internal service running this batch.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import threading

_CURRENT = ContextVar("bread_shader_restore_admission", default=None)


@contextmanager
def restore_admission(dispatcher=None):
    token = _CURRENT.set({"dispatch": dispatcher, "snapshots": {}, "snapshotBytes": 0, "guard": threading.RLock(), "stats": {
        "artifactValidations": 0, "snapshotReuses": 0, "changedItemFallbacks": 0,
        "localDispatches": 0}})
    try:
        yield
    finally:
        _CURRENT.reset(token)


def local_dispatcher():
    state = _CURRENT.get()
    if state and state["dispatch"] is not None:
        state["stats"]["localDispatches"] += 1
    return state["dispatch"] if state else None


def statistics():
    state = _CURRENT.get()
    return dict(state["stats"], retainedSnapshots=len(state["snapshots"])) if state else {}


def restored_snapshot(service, path):
    from .shader_workspace.status_snapshot import _key, WorkspaceStatusSnapshotChanged
    state = _CURRENT.get()
    if state is None:
        return None
    key = _key(path)
    with state["guard"]:
        snapshot = state["snapshots"].get(key)
    if snapshot is not None:
        try:
            snapshot.checked_status(service)
        except (WorkspaceStatusSnapshotChanged, OSError):
            with state["guard"]:
                if state["snapshots"].get(key) is snapshot:
                    state["snapshots"].pop(key, None)
                    state["snapshotBytes"] -= len(snapshot._status_json)
                state["stats"]["changedItemFallbacks"] += 1
            return None
        with state["guard"]:
            state["stats"]["snapshotReuses"] += 1
    return snapshot


def artifact_status(service, path, validate):
    """Mint reuse only around the real artifact validator, with input seals."""
    state = _CURRENT.get()
    if state is None:
        return validate(path)
    from .shader_workspace.status_snapshot import _key, capture_status_snapshot
    from .shader_workspace.errors import ManifestValidationError
    snapshot = restored_snapshot(service, path)
    if snapshot is None:
        with state["guard"]:
            state["stats"]["artifactValidations"] += 1
        try:
            snapshot = capture_status_snapshot(service, path, status_loader=validate,
                                               artifact_admission=True)
        except (ManifestValidationError, OSError, KeyError):
            # Older/exceptional layouts remain readable through their normal
            # validator. Failure to cache is never a failure to restore.
            return validate(path)
        size = len(snapshot._status_json)
        with state["guard"]:
            previous = state["snapshots"].pop(_key(path), None)
            if previous is not None:
                state["snapshotBytes"] -= len(previous._status_json)
            while state["snapshots"] and (len(state["snapshots"]) >= 512 or state["snapshotBytes"] + size > 64 * 1024 * 1024):
                oldest = next(iter(state["snapshots"]))
                state["snapshotBytes"] -= len(state["snapshots"].pop(oldest)._status_json)
            if size <= 64 * 1024 * 1024:
                state["snapshots"][_key(path)] = snapshot
                state["snapshotBytes"] += size
    # restored_snapshot already checked the complete seals; a newly captured
    # snapshot was sealed around validation. Do not repeat that same scan.
    result = snapshot.validated_status()
    from .shader_workspace.workspace import _workspace_operation_locked
    result["workspaceLocked"] = _workspace_operation_locked(snapshot._root)
    return result
