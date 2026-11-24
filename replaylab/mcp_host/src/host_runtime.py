"""Host-side long-tool progress and process-memory snapshots.

Long unpacked/organization tools share one slot so the Agent cannot start a
second heavy call while the first is still on the IPC/event loop. Status
polling never takes that slot. Memory samples are best-effort Windows
WorkingSet/Private bytes for the Host and the bound qrenderdoc process.

Every registered MCP tool also emits a begin/end memory sample so growth can
be attributed beyond the long-tool subset. Tools which actually touched replay
drop rebuildable qrenderdoc/bridge transients after their terminal state. Status,
reporting, outline and filesystem tools never issue cleanup IPC. Agents never
call a cleanup tool for this — release is Host-owned and invisible to the list.
"""

from __future__ import annotations

import asyncio
import functools
import gc
import inspect
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, TypeVar, cast

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {}
_TRACE_LOCK = threading.Lock()
_LAST_TRACE_WRITE = 0.0
_LAST_POLL_TRACE = 0.0
_TRACE_SESSION_ID = uuid.uuid4().hex

LONG_TOOLS = frozenset({
    "describe_unpacked_asset",
    "find_unpacked_asset_relations",
    "search_unpacked_assets",
    "restructure_shader_workspace_source",
    "hydrate_unpacked_asset_scope",
    "index_active_unpacked_assets",
    "retrieve_shader_asset_candidates",
    "begin_shader_workspace_organization",
    "rename_shader_workspace_symbols",
    "publish_shader_resource_names",
    "finish_shader_workspace_organization",
    "decompile_shader_workspace",
    "validate_shader_workspace_delivery",
    "diagnose_shader_organization_candidate",
    "run_shader_effect_experiment",
    "publish_visual_reply",
    "get_cbuffer_parameter_link",
    "get_mesh_uv_link",
    "capture_mesh_uv_evidence",
    "inspect_vertex_effect_scope",
})

# Tools that actually force replay / BuildTarget / texture reads. Read-only Host
# status, outline, reporting and filesystem tools must not issue replay IPC merely
# because they returned: that used to block the event loop for up to three seconds
# and could clear replay state while a detached mutation/review job was still live.
REPLAY_TOUCH_TOOLS = frozenset({
    "begin_shader_workspace_organization",
    "finish_shader_workspace_organization",
    "decompile_shader_workspace",
    "validate_shader_workspace_delivery",
    "diagnose_shader_organization_candidate",
    "run_shader_effect_experiment",
    "publish_visual_reply",
    "get_cbuffer_parameter_link",
    "get_mesh_uv_link",
    "capture_mesh_uv_evidence",
    "inspect_vertex_effect_scope",
    "get_texture_fingerprints",
    "get_bound_textures",
    "get_constant_buffer_values",
    "get_draw_geometry",
    "apply_shader_workspace_shared_resource",
    "start_shader_workspace_live_edit",
})

# These return a job receipt before their replay worker is terminal. Their
# LongToolSlot owns cleanup; the MCP wrapper must not release on the start receipt.
BACKGROUND_REPLAY_JOB_TOOLS = frozenset()

STATUS_SCHEMA = "replaylab-host-runtime-status/v1"
TRACE_SCHEMA = "replaylab-host-memory-trace/v1"

_WINDOWS_MEMORY_API_LOCK = threading.Lock()
_WINDOWS_MEMORY_API: tuple[Any, Any] | None = None
if os.name == "nt":
    import ctypes as _ctypes
    from ctypes import wintypes as _wintypes

    class _ProcessMemoryCountersEx(_ctypes.Structure):
        _fields_ = [
            ("cb", _wintypes.DWORD),
            ("PageFaultCount", _wintypes.DWORD),
            ("PeakWorkingSetSize", _ctypes.c_size_t),
            ("WorkingSetSize", _ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", _ctypes.c_size_t),
            ("QuotaPagedPoolUsage", _ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", _ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", _ctypes.c_size_t),
            ("PagefileUsage", _ctypes.c_size_t),
            ("PeakPagefileUsage", _ctypes.c_size_t),
            ("PrivateUsage", _ctypes.c_size_t),
        ]

    _PROCESS_MEMORY_COUNTERS_PTR = _ctypes.POINTER(_ProcessMemoryCountersEx)
else:  # pragma: no cover - exercised by non-Windows CI imports
    _ProcessMemoryCountersEx = None  # type: ignore[assignment,misc]
    _PROCESS_MEMORY_COUNTERS_PTR = None


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ".")


def _trace_path() -> Path:
    return _local_appdata() / "ReplayLab" / "logs" / "host-memory-trace.jsonl"


def _windows_process_memory_api() -> tuple[Any, Any] | None:
    """Return one typed WinAPI binding; ctypes pointer classes stay bounded."""

    global _WINDOWS_MEMORY_API
    if os.name != "nt":
        return None
    if _WINDOWS_MEMORY_API is not None:
        return _WINDOWS_MEMORY_API
    with _WINDOWS_MEMORY_API_LOCK:
        if _WINDOWS_MEMORY_API is not None:
            return _WINDOWS_MEMORY_API
        kernel32 = _ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = _ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            _wintypes.DWORD, _wintypes.BOOL, _wintypes.DWORD
        ]
        kernel32.OpenProcess.restype = _wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [_wintypes.HANDLE]
        kernel32.CloseHandle.restype = _wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [
            _wintypes.HANDLE,
            _PROCESS_MEMORY_COUNTERS_PTR,
            _wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = _wintypes.BOOL
        _WINDOWS_MEMORY_API = (kernel32, psapi)
        return _WINDOWS_MEMORY_API


def _windows_process_memory(pid: int) -> dict[str, Any] | None:
    if os.name != "nt" or pid <= 0:
        return None
    try:
        api = _windows_process_memory_api()
        if api is None:
            return None
        kernel32, psapi = api
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            counters = _ProcessMemoryCountersEx()
            counters.cb = _ctypes.sizeof(_ProcessMemoryCountersEx)
            if not psapi.GetProcessMemoryInfo(
                handle, _ctypes.byref(counters), counters.cb
            ):
                return None
            working = int(counters.WorkingSetSize)
            private = int(counters.PrivateUsage)
            return {
                "pid": int(pid),
                "workingSetBytes": working,
                "privateBytes": private,
                "peakWorkingSetBytes": int(counters.PeakWorkingSetSize),
                "workingSetGiB": round(working / (1024 ** 3), 3),
                "privateGiB": round(private / (1024 ** 3), 3),
            }
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _qrenderdoc_pid() -> int:
    from .ipc_client import INSTANCE_ROOT, IPCClient

    try:
        entries = os.listdir(INSTANCE_ROOT)
    except OSError:
        return 0
    newest = 0
    pid = 0
    for name in entries:
        directory = os.path.join(INSTANCE_ROOT, name)
        manifest = IPCClient._manifest(directory, allow_stale_heartbeat=True)
        if not isinstance(manifest, dict):
            continue
        try:
            heartbeat = int(manifest.get("heartbeatUnixMs") or 0)
            candidate = int(manifest.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if candidate > 0 and heartbeat >= newest:
            newest = heartbeat
            pid = candidate
    return pid


def memory_snapshot() -> dict[str, Any]:
    host = _windows_process_memory(os.getpid())
    replay = _windows_process_memory(_qrenderdoc_pid())
    snapshot = {
        "sampledAtUnixNs": time.time_ns(),
        "host": host,
        "qrenderdoc": replay,
    }
    if isinstance(host, Mapping) and isinstance(replay, Mapping):
        snapshot["combinedWorkingSetGiB"] = round(
            (int(host.get("workingSetBytes") or 0) + int(replay.get("workingSetBytes") or 0))
            / (1024 ** 3),
            3,
        )
    return snapshot


def _process_deltas(start: Mapping[str, Any] | None, end: Mapping[str, Any] | None) -> dict[str, int]:
    start = start if isinstance(start, Mapping) else {}
    end = end if isinstance(end, Mapping) else {}
    deltas: dict[str, int] = {}
    for role in ("host", "qrenderdoc"):
        before = start.get(role) if isinstance(start.get(role), Mapping) else {}
        after = end.get(role) if isinstance(end.get(role), Mapping) else {}
        for field in ("workingSetBytes", "privateBytes"):
            try:
                deltas["%s%sDeltaBytes" % (
                    role,
                    "WorkingSet" if field == "workingSetBytes" else "Private",
                )] = int(after.get(field) or 0) - int(before.get(field) or 0)
            except (TypeError, ValueError):
                deltas["%s%sDeltaBytes" % (
                    role,
                    "WorkingSet" if field == "workingSetBytes" else "Private",
                )] = 0
    return deltas


def _append_trace(event: Mapping[str, Any]) -> None:
    global _LAST_TRACE_WRITE
    now = time.monotonic()
    kind = str(event.get("kind") or "")
    # Heartbeats can fire every 2s; keep the file from dominating the disk.
    if kind == "heartbeat" and now - _LAST_TRACE_WRITE < 4.0:
        return
    path = _trace_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "schema": TRACE_SCHEMA,
            "sessionId": _TRACE_SESSION_ID,
            "unixNs": time.time_ns(),
            **dict(event),
        }
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        with _TRACE_LOCK:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        _LAST_TRACE_WRITE = now
    except Exception:
        return


def ensure_memory_trace_session(*, reason: str = "host-start") -> dict[str, Any]:
    """Emit one session marker so a new detection run is easy to slice in the log."""

    marker = {
        "kind": "session",
        "reason": str(reason or "host-start")[:120],
        "tracePath": str(_trace_path()),
        "memory": memory_snapshot(),
        "longTools": sorted(LONG_TOOLS),
        "replayTouchTools": sorted(REPLAY_TOUCH_TOOLS),
        "autoRelease": "replay-touch-terminal-only",
        "note": (
            "Host releases rebuildable qrenderdoc transients after terminal "
            "replay-touch tools; status/report/outline calls do not issue cleanup IPC."
        ),
    }
    _append_trace(marker)
    return marker


def record_memory_checkpoint(
    kind: str,
    *,
    tool: str = "",
    phase: str = "",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one explicit memory sample (used by jobs / status polls)."""

    payload: dict[str, Any] = {
        "kind": str(kind or "checkpoint")[:64],
        "tool": str(tool or "")[:120],
        "phase": str(phase or "")[:120],
        "memory": memory_snapshot(),
    }
    if extra:
        payload.update({key: value for key, value in dict(extra).items()
                        if key not in payload})
    _append_trace(payload)
    return payload


def record_operation_timing(
    *, tool: str, operation: str, outcome: str,
    timing: Mapping[str, Any], error_type: str = "",
) -> None:
    """Append one compact phase breakdown to the existing Host trace.

    The caller supplies only IDs, aggregate counts and timings. No source,
    alias, path, request body or compiler diagnostics belong in this row.
    Trace writes remain best-effort and cannot change a mutation result.
    """

    try:
        raw_dimensions = timing.get("dimensions")
        raw_dimensions = raw_dimensions if isinstance(raw_dimensions, Mapping) else {}
        count_keys = {
            "sourceByteSize", "sourceLineCount", "ledgerOperationCount",
            "renameCount", "incomingRenameCount", "affectedLineCount",
            "editCount", "structureReportCacheHit",
        }
        dimensions = {
            key: value for key, value in raw_dimensions.items()
            if key in count_keys and type(value) is int and 0 <= value <= 10_000_000
        }
        for key in ("organizationId", "operationId"):
            value = raw_dimensions.get(key)
            if isinstance(value, str) and 8 <= len(value) <= 128 and all(
                char.isascii() and (char.isalnum() or char in "_.:-") for char in value
            ):
                dimensions[key] = value
        role = raw_dimensions.get("candidateRole")
        if isinstance(role, str) and role in {"baseline", "proposal", "recovery", "other"}:
            dimensions["candidateRole"] = role
        raw_phases = timing.get("phasesMs")
        raw_phases = raw_phases if isinstance(raw_phases, Mapping) else {}
        phases = {
            key: round(float(value), 3)
            for key, value in raw_phases.items()
            if isinstance(key, str) and 1 <= len(key) <= 64 and key.isascii()
            and key.replace("_", "").isalnum()
            and type(value) in {int, float} and 0 <= value <= 1_000_000_000
        }
        total = timing.get("totalMs")
        total_ms = round(float(total), 3) if type(total) in {int, float} and 0 <= total <= 1_000_000_000 else None
        phase = timing.get("activePhase")
        failure = timing.get("failureCategory")
        failure_category = failure if isinstance(failure, str) and failure in {
            "proposal-drift", "range-or-rewrite", "structural-replay", "ledger-limit"
        } else None
        _append_trace({
            "kind": "operation-timing",
            "tool": str(tool)[:120],
            "operation": str(operation)[:40],
            "outcome": str(outcome)[:40],
            "errorType": str(error_type)[:80] or None,
            "timing": {
                "schema": "replaylab-shader-organization-operation-timing/v1",
                "totalMs": total_ms,
                "activePhase": phase if isinstance(phase, str) and phase in phases else None,
                "failureCategory": failure_category,
                "dimensions": dimensions,
                "phasesMs": phases,
            },
        })
    except Exception:
        return


def _release_qrenderdoc_transient_caches() -> dict[str, Any]:
    """Best-effort drop of rebuildable qrenderdoc/bridge caches after a tool call.

    Host Python ``gc.collect`` cannot shrink ``q_render_soco_doc.exe``. The
    capture's loaded RDC resources stay in one session; tool calls that force
    replay / BuildTargetShader / GetTextureData can still stack scratch on top.
    Prefer ``release_replay_transient_caches`` (fingerprint + ClearReplayCache +
    orphan FreeTarget) and fall back to the older fingerprint-only IPC when the
    bridge build is older.

    This is Host-internal only — never register it as an Agent MCP tool.
    Keep the IPC timeout short: auto-release runs after every tool and must not
    stall the Agent when qrenderdoc is down.
    """

    result: dict[str, Any] = {"applied": False}
    try:
        from .ipc_client import IPCClient

        ipc = IPCClient()
        if not ipc.is_bridge_alive():
            result["reason"] = "bridge-not-alive"
            return result
        try:
            released = ipc.call(
                "release_replay_transient_caches",
                {"clear_replay_cache": True, "release_orphaned_targets": True},
                timeout=3.0,
            )
        except Exception:
            released = ipc.call("release_texture_fingerprint_cache", {}, timeout=3.0)
        if isinstance(released, Mapping):
            result.update(dict(released))
            result.setdefault("applied", True)
        else:
            result["raw"] = released
            result["applied"] = True
    except Exception as exc:
        result["reason"] = str(exc)[:400]
    return result


def _auto_release_after_tool(
    tool_name: str,
    *,
    scope: str,
    before_memory: Mapping[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Host-owned cleanup after a tool returns. Never an Agent-facing tool.

    Only explicit replay-touch tools clear rebuildable scratch unless a trusted
    internal caller opts into ``force``. Agents never invoke this path by name.
    """

    name = str(tool_name or "").strip() or "unknown"
    should = bool(force or name in REPLAY_TOUCH_TOOLS)
    if not should:
        return {"applied": False, "skipped": True, "tool": name}
    gc.collect()
    pre = before_memory if isinstance(before_memory, Mapping) else memory_snapshot()
    release = _release_qrenderdoc_transient_caches()
    post = memory_snapshot()
    deltas = _process_deltas(pre, post)
    _append_trace({
        "kind": "cache-release",
        "tool": name,
        "scope": scope,
        "automatic": True,
        "release": release,
        "memoryBeforeRelease": pre,
        "memory": post,
        "qrenderdocPrivateDeltaBytes": deltas.get("qrenderdocPrivateDeltaBytes", 0),
        "qrenderdocWorkingSetDeltaBytes": deltas.get("qrenderdocWorkingSetDeltaBytes", 0),
        "qrenderdocPrivateDeltaGiB": round(
            deltas.get("qrenderdocPrivateDeltaBytes", 0) / (1024 ** 3), 3
        ),
        "qrenderdocWorkingSetDeltaGiB": round(
            deltas.get("qrenderdocWorkingSetDeltaBytes", 0) / (1024 ** 3), 3
        ),
    })
    _append_trace({
        "kind": "post-release",
        "tool": name,
        "scope": scope,
        "memory": post,
        "releaseApplied": bool(release.get("applied")),
        "replayCacheCleared": bool(release.get("replayCacheCleared")),
        "releasedEntries": int(release.get("releasedEntries") or 0),
    })
    return {
        "applied": bool(release.get("applied")),
        "release": release,
        "memoryBeforeRelease": pre,
        "memoryAfterRelease": post,
        "deltas": deltas,
    }


_Function = TypeVar("_Function", bound=Callable[..., Any])


@contextmanager
def sample_tool_memory(tool_name: str, *, long_tool: bool = False) -> Iterator[None]:
    """Record begin/end memory for one MCP tool invocation.

    Long-tool slots already emit their own begin/heartbeat/end rows; callers
    pass ``long_tool=True`` (or the tool is in ``LONG_TOOLS``) so this wrapper
    only adds entry/exit marks. Replay cleanup is scheduled by the instrumentation
    wrapper for direct calls or by ``finish_long_tool`` after a background job is
    terminal; never both.
    """

    name = str(tool_name or "").strip() or "unknown"
    covered = name in LONG_TOOLS or long_tool
    start_memory = memory_snapshot()
    started = time.monotonic()
    _append_trace({
        "kind": "mark" if covered else "begin",
        "tool": name,
        "scope": "long-tool-covered" if covered else "all-tools",
        "memory": start_memory,
    })
    ok = False
    try:
        yield
        ok = True
    finally:
        end_memory = memory_snapshot()
        deltas = _process_deltas(start_memory, end_memory)
        _append_trace({
            "kind": "end",
            "tool": name,
            "scope": "long-tool-covered" if covered else "all-tools",
            "ok": ok,
            "elapsedSeconds": round(max(0.0, time.monotonic() - started), 3),
            "startMemory": start_memory,
            "endMemory": end_memory,
            "qrenderdocWorkingSetDeltaBytes": deltas.get("qrenderdocWorkingSetDeltaBytes", 0),
            "qrenderdocPrivateDeltaBytes": deltas.get("qrenderdocPrivateDeltaBytes", 0),
            "hostWorkingSetDeltaBytes": deltas.get("hostWorkingSetDeltaBytes", 0),
            "hostPrivateDeltaBytes": deltas.get("hostPrivateDeltaBytes", 0),
            "qrenderdocWorkingSetDeltaGiB": round(
                deltas.get("qrenderdocWorkingSetDeltaBytes", 0) / (1024 ** 3), 3
            ),
            "qrenderdocPrivateDeltaGiB": round(
                deltas.get("qrenderdocPrivateDeltaBytes", 0) / (1024 ** 3), 3
            ),
        })


def instrument_tool_memory(function: _Function) -> _Function:
    """Wrap every registered MCP tool with begin/end memory samples.

    Preserve async tools as coroutines. A sync wrapper around an async tool
    would return an un-awaited coroutine object to FastMCP, which then fails
    Pydantic validation and surfaces to the Agent as a \"coroutine bug\".
    """

    def touches_replay(name, args, kwargs):
        if name not in REPLAY_TOUCH_TOOLS or name in BACKGROUND_REPLAY_JOB_TOOLS:
            return False
        if name in {"decompile_shader_workspace", "finish_shader_workspace_organization"}:
            try:
                values = inspect.signature(function).bind_partial(*args, **kwargs)
                values.apply_defaults()
                if name == "finish_shader_workspace_organization":
                    arguments = values.arguments
                    validation = arguments.get("validate_for_apply")
                    if validation is None:
                        validation = arguments.get("publication_mode", "final") == "final"
                    return (validation is True or arguments.get("apply_shared_resource") is True
                            or arguments.get("start_live_edit") is True)
                return (values.arguments.get("reference_only") is not True and
                        (values.arguments.get("validate_for_apply", True) is not False
                         or values.arguments.get("apply_shared_resource") is True))
            except TypeError:
                return False
        return True

    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
            name = getattr(function, "__name__", "unknown")
            try:
                with sample_tool_memory(name):
                    return await function(*args, **kwargs)
            finally:
                if touches_replay(name, args, kwargs):
                    # The IPC may wait up to three seconds. Keep status/poll calls
                    # responsive on the asyncio loop while cleanup runs.
                    await asyncio.to_thread(
                        _auto_release_after_tool,
                        name,
                        scope="direct-async-replay-tool",
                        before_memory=memory_snapshot(),
                    )

        return cast(_Function, async_wrapped)

    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        name = getattr(function, "__name__", "unknown")
        try:
            with sample_tool_memory(name):
                return function(*args, **kwargs)
        finally:
            # FastMCP executes synchronous tools on its worker pool. Status,
            # outline and report tools never enter this branch.
            if touches_replay(name, args, kwargs):
                _auto_release_after_tool(
                    name,
                    scope="direct-sync-replay-tool",
                    before_memory=memory_snapshot(),
                )

    return cast(_Function, wrapped)


def reset_long_tool_state() -> None:
    with _LOCK:
        _STATE.clear()


def _progress_view(progress: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize done/total counters for get_host_runtime_status polling."""

    raw = dict(progress or {})
    done = raw.get("done")
    total = raw.get("total")
    if done is None:
        for key in ("walkedFiles", "scannedFiles", "processed", "bytesRead"):
            if key in raw:
                done = raw.get(key)
                break
    if total is None:
        for key in ("maxFiles", "maxWalkFiles", "wanted", "fileSizeBytes", "total"):
            if key in raw:
                total = raw.get(key)
                break
    try:
        done_n = int(done) if done is not None else None
    except (TypeError, ValueError):
        done_n = None
    try:
        total_n = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_n = None
    percent = None
    if done_n is not None and total_n is not None and total_n > 0:
        percent = round(min(100.0, 100.0 * max(0, done_n) / total_n), 1)
    unit = str(raw.get("unit") or "")
    if not unit:
        if "walkedFiles" in raw or "scannedFiles" in raw or "maxWalkFiles" in raw:
            unit = "files"
        elif "bytesRead" in raw or "fileSizeBytes" in raw:
            unit = "bytes"
        elif done_n is not None:
            unit = "items"
    summary = str(raw.get("summary") or "")
    if not summary:
        if done_n is not None and total_n is not None:
            summary = "%s/%s %s" % (done_n, total_n, unit or "items")
        elif done_n is not None:
            summary = "%s %s (total unknown)" % (done_n, unit or "items")
        elif raw.get("detail"):
            summary = str(raw.get("detail"))[:200]
    view = dict(raw)
    if done_n is not None:
        view["done"] = done_n
    if total_n is not None:
        view["total"] = total_n
    if percent is not None:
        view["percent"] = percent
    if unit:
        view["unit"] = unit
    if summary:
        view["summary"] = summary[:240]
    return view


def current_status() -> dict[str, Any]:
    global _LAST_POLL_TRACE
    memory = memory_snapshot()
    with _LOCK:
        running = dict(_STATE) if _STATE else None
    elapsed = None
    if running:
        started = float(running.get("startedMonotonic") or 0.0)
        elapsed = round(max(0.0, time.monotonic() - started), 1)
        progress = _progress_view(running.get("progress") if isinstance(running.get("progress"), Mapping) else {})
        running = {
            key: value
            for key, value in running.items()
            if key not in {"startedMonotonic", "startMemory", "ownerToken"}
        }
        running["elapsedSeconds"] = elapsed
        running["alive"] = True
        if progress:
            running["progress"] = progress
            running["progressSummary"] = progress.get("summary") or ""
    mutation_jobs: list[dict[str, Any]] = []
    try:
        from .shader_workspace.mutation_jobs import (
            list_active_shader_mutation_jobs,
        )

        mutation_jobs = list_active_shader_mutation_jobs()
    except Exception:
        mutation_jobs = []
    busy = bool(running) or bool(mutation_jobs)
    # Throttled poll samples so repeated AI status checks still leave a trail
    # without flooding the jsonl (heartbeats already cover long-tool stretches).
    now = time.monotonic()
    if now - _LAST_POLL_TRACE >= 5.0:
        _LAST_POLL_TRACE = now
        _append_trace({
            "kind": "poll",
            "tool": "get_host_runtime_status",
            "toolStatus": "running" if busy else "idle",
            "currentTool": (running or {}).get("name") if running else "",
            "shaderMutationJobCount": len(mutation_jobs),
            "memory": memory,
        })
    return {
        "schema": STATUS_SCHEMA,
        "schemaVersion": 1,
        "toolStatus": "running" if busy else "idle",
        "currentTool": running,
        "shaderMutationJobs": mutation_jobs,
        "memory": memory,
        "memoryTracePath": str(_trace_path()),
        "memoryTraceSessionId": _TRACE_SESSION_ID,
        "autoReleaseAfterTools": False,
        "autoReleasePolicy": {
            "mode": "replay-touch-terminal-only",
            "tools": sorted(REPLAY_TOUCH_TOOLS),
            "statusAndReportingExcluded": True,
        },
        "longTools": sorted(LONG_TOOLS),
        "facts": {
            "longToolSlotAvailable": not bool(running),
            "activeShaderMutations": len(mutation_jobs),
        },
        "availableCapabilities": (
            ["get_host_runtime_status"]
            if busy else ["get_host_runtime_status", *sorted(LONG_TOOLS)]
        ),
        "applied": False,
    }


def try_begin_long_tool(
    name: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    owner_token: str = "",
) -> dict[str, Any] | None:
    """Return None when this caller owns the slot, or a busy payload otherwise."""

    now = time.monotonic()
    memory = memory_snapshot()
    with _LOCK:
        if _STATE:
            started = float(_STATE.get("startedMonotonic") or now)
            running = {
                key: value
                for key, value in _STATE.items()
                if key not in {"startedMonotonic", "ownerToken"}
            }
            running["elapsedSeconds"] = round(max(0.0, now - started), 1)
            running["alive"] = True
            blocked_by = str(_STATE.get("name") or "")
        else:
            running = None
            blocked_by = ""
            _STATE.update({
                "name": name,
                "ownerToken": str(owner_token or uuid.uuid4().hex),
                "arguments": {
                    key: value
                    for key, value in dict(arguments or {}).items()
                    if isinstance(key, str) and len(key) <= 64
                },
                "phase": "started",
                "startedUnixNs": time.time_ns(),
                "startedMonotonic": now,
                "updatedUnixNs": time.time_ns(),
                "startMemory": memory,
            })
    if running is not None:
        busy = {
            "schema": STATUS_SCHEMA,
            "schemaVersion": 1,
            "toolStatus": "busy",
            "requestedTool": name,
            "currentTool": running,
            "memory": memory,
            "longTools": sorted(LONG_TOOLS),
            "facts": {
                "longToolSlotAvailable": False,
                "blockedBy": blocked_by,
            },
            "applied": False,
        }
        _append_trace({
            "kind": "busy",
            "tool": name,
            "blockedBy": blocked_by,
            "elapsedSeconds": running.get("elapsedSeconds"),
            "memory": memory,
        })
        return busy
    _append_trace({"kind": "begin", "tool": name, "memory": memory})
    return None


def update_long_tool(
    phase: str,
    *,
    owner_token: str,
    **progress: Any,
) -> bool:
    with _LOCK:
        if not owner_token or not _STATE:
            return False
        if _STATE.get("ownerToken") != owner_token:
            return False
        _STATE["phase"] = str(phase or "")[:120]
        _STATE["updatedUnixNs"] = time.time_ns()
        if progress:
            current = dict(_STATE.get("progress") or {})
            current.update(progress)
            _STATE["progress"] = current
        tool_name = _STATE.get("name")
    _append_trace({
        "kind": "heartbeat",
        "tool": tool_name,
        "phase": phase,
        "progress": progress,
        "memory": memory_snapshot(),
    })
    return True


def finish_long_tool(
    *,
    ok: bool,
    extra: Mapping[str, Any] | None = None,
    owner_token: str = "",
) -> dict[str, Any]:
    with _LOCK:
        if _STATE and _STATE.get("ownerToken") != owner_token:
            return {
                "ok": ok,
                "slotReleased": False,
                "reason": "long-tool-owner-mismatch",
                "elapsedSeconds": 0.0,
            }
        started = dict(_STATE)
        _STATE.clear()
    end_memory = memory_snapshot()
    if not started:
        return {
            "ok": ok,
            "slotReleased": False,
            "elapsedSeconds": 0.0,
            "endMemory": end_memory,
            "qrenderdocWorkingSetDeltaBytes": 0,
            "qrenderdocWorkingSetDeltaGiB": 0.0,
        }
    start_memory = started.get("startMemory") if isinstance(started.get("startMemory"), Mapping) else {}
    q0 = ((start_memory.get("qrenderdoc") or {}) if isinstance(start_memory, Mapping) else {}).get("workingSetBytes") or 0
    q1 = ((end_memory.get("qrenderdoc") or {}).get("workingSetBytes") or 0)
    try:
        delta = int(q1) - int(q0)
    except (TypeError, ValueError):
        delta = 0
    deltas = _process_deltas(
        start_memory if isinstance(start_memory, Mapping) else {},
        end_memory if isinstance(end_memory, Mapping) else {},
    )
    payload = {
        "tool": started.get("name"),
        "ok": ok,
        "slotReleased": True,
        "scope": "long-tool",
        "elapsedSeconds": round(
            max(0.0, time.monotonic() - float(started.get("startedMonotonic") or time.monotonic())),
            1,
        ),
        "startMemory": start_memory,
        "endMemory": end_memory,
        "qrenderdocWorkingSetDeltaBytes": deltas.get("qrenderdocWorkingSetDeltaBytes", delta),
        "qrenderdocPrivateDeltaBytes": deltas.get("qrenderdocPrivateDeltaBytes", 0),
        "hostWorkingSetDeltaBytes": deltas.get("hostWorkingSetDeltaBytes", 0),
        "hostPrivateDeltaBytes": deltas.get("hostPrivateDeltaBytes", 0),
        "qrenderdocWorkingSetDeltaGiB": round(
            deltas.get("qrenderdocWorkingSetDeltaBytes", delta) / (1024 ** 3), 3
        ),
        "qrenderdocPrivateDeltaGiB": round(
            deltas.get("qrenderdocPrivateDeltaBytes", 0) / (1024 ** 3), 3
        ),
    }
    if extra:
        payload.update(dict(extra))
    _append_trace({"kind": "end", "ok": ok, **payload})
    # A replay-touching long tool releases exactly once, after the worker has
    # reached its terminal state. The outer wrapper deliberately skips all long
    # tools, so a start-job receipt cannot clear replay under the active worker.
    release_info = _auto_release_after_tool(
        str(started.get("name") or ""),
        scope="long-tool",
        before_memory=end_memory,
    )
    if release_info:
        payload["qrenderdocCacheRelease"] = release_info.get("release") or release_info
    return payload


class LongToolSlot:
    """Exclusive slot plus a 2s memory/progress heartbeat for one long tool."""

    def __init__(self, name: str, arguments: Mapping[str, Any] | None = None):
        self._owner_token = uuid.uuid4().hex
        self._close_lock = threading.Lock()
        self._closed = False
        self._close_result: dict[str, Any] | None = None
        self.busy = try_begin_long_tool(
            name, arguments, owner_token=self._owner_token
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if self.busy is None:
            self._thread = threading.Thread(
                target=self._beat, daemon=True, name="host-runtime-%s" % name
            )
            self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(2.0):
            update_long_tool("working", owner_token=self._owner_token)

    def update(self, phase: str, **progress: Any) -> bool:
        """Publish progress only while this exact slot still owns the state."""

        if self.busy is not None or self._closed:
            return False
        return update_long_tool(
            phase, owner_token=self._owner_token, **progress
        )

    def close(self, *, ok: bool) -> dict[str, Any] | None:
        if self.busy is not None:
            return None
        with self._close_lock:
            if self._closed:
                return self._close_result
            self._closed = True
            self._stop.set()
            if (
                self._thread is not None
                and self._thread is not threading.current_thread()
            ):
                self._thread.join(timeout=0.5)
            self._close_result = finish_long_tool(
                ok=ok, owner_token=self._owner_token
            )
            return self._close_result


def attach_runtime(result: dict[str, Any], runtime: Mapping[str, Any]) -> dict[str, Any]:
    attached = dict(result)
    attached.setdefault("toolStatus", "ok" if runtime.get("ok") else "error")
    attached["hostRuntime"] = {
        "elapsedSeconds": runtime.get("elapsedSeconds"),
        "qrenderdocWorkingSetDeltaGiB": runtime.get("qrenderdocWorkingSetDeltaGiB"),
        "qrenderdocPrivateDeltaGiB": runtime.get("qrenderdocPrivateDeltaGiB"),
        "hostWorkingSetDeltaBytes": runtime.get("hostWorkingSetDeltaBytes"),
        "hostPrivateDeltaBytes": runtime.get("hostPrivateDeltaBytes"),
        "qrenderdocWorkingSetDeltaBytes": runtime.get("qrenderdocWorkingSetDeltaBytes"),
        "qrenderdocPrivateDeltaBytes": runtime.get("qrenderdocPrivateDeltaBytes"),
        "memory": runtime.get("endMemory"),
    }
    return attached
