"""Bounded task telemetry for Bread Host tools.

This module records what a tool returned and where it wrote files. It does not
decide the Agent's next action, accept a report, or authorize a shader effect.
Shader tools and the RenderDoc bridge enforce their own physical preconditions.
The legacy sidecar names remain readable so a running capture can release a
live-edit or replacement left by an earlier task.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Callable, Mapping


REPORT_SCHEMA = "replaylab-task-report/v1"  # Old read-only mirror.
REPORT_STATE_SCHEMA = "replaylab-task-report-state/v1"
TASK_ID_ENV = "REPLAYLAB_TASK_ID"
TASK_EVENT_ROOT_ENV = "REPLAYLAB_TASK_EVENT_ROOT"
CONVERSATION_ID_ENV = "REPLAYLAB_CONVERSATION_ID"

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SECRET_KEY_RE = re.compile(r"token|secret|password|authorization|credential|cookie|api.?key", re.I)
_SECRET_TEXT_RE = re.compile(
    r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|token|api[_-]?key|password|secret|authorization)\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s&;,]+)"
)
_MAX_SIDECAR_BYTES = 2 * 1024 * 1024
_MAX_FACTS = 128
_MAX_FACT_CHARS = 1600
_MAX_EVENT_SCAN_BYTES = 16 * 1024 * 1024
_EVENT_READ_CHUNK_BYTES = 64 * 1024
_NON_REPORTING_TASK_TOOLS = frozenset({"release_task_shader_occupancy"})
_ARTIFACT_KEYS = frozenset({
    "path", "workspacePath", "artifactPath", "analysisPath", "flowchartPath",
    "sourcePath", "exportPath", "outputPath", "projectPath",
})


def _sidecar_path(root: Path | str, task_id: str, suffix: str) -> Path:
    task = str(task_id or "")
    if not _TASK_ID_RE.fullmatch(task):
        raise ValueError("task_id contains unsafe path characters")
    return Path(root) / (task + suffix)


def task_report_path(root: Path | str, task_id: str) -> Path:
    return _sidecar_path(root, task_id, ".report.json")


def task_report_state_path(root: Path | str, task_id: str) -> Path:
    return _sidecar_path(root, task_id, ".report-state.json")


def clear_task_report_sidecars(root: Path | str, task_id: str) -> None:
    """Explicit maintenance cleanup; normal task restarts keep their facts."""

    for path in (task_report_path(root, task_id), task_report_state_path(root, task_id)):
        with suppress(FileNotFoundError):
            if path.is_file() and not path.is_symlink():
                path.unlink()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_SIDECAR_BYTES:
        raise ValueError("task telemetry sidecar exceeds the safety bound")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _read_sidecar(path: Path, schema: str, task_id: str) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_SIDECAR_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema") != schema or value.get("taskId") != task_id:
        return None
    return value


def read_task_report_sidecars(
    root: Path | str, task_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Read current telemetry and, only when present, an old report mirror."""

    state = _read_sidecar(task_report_state_path(root, task_id), REPORT_STATE_SCHEMA, task_id)
    if state is not None:
        old_report = state.get("report")
        return (dict(old_report) if isinstance(old_report, Mapping) else None), state
    return _read_sidecar(task_report_path(root, task_id), REPORT_SCHEMA, task_id), None


def _last_task_event(root: Path, task_id: str) -> dict[str, Any] | None:
    """Read the last complete event without loading a long task log."""

    path = _sidecar_path(root, task_id, ".jsonl")
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            suffix = b""
            scanned = 0
            while position > 0 and scanned < _MAX_EVENT_SCAN_BYTES:
                count = min(_EVENT_READ_CHUNK_BYTES, position, _MAX_EVENT_SCAN_BYTES - scanned)
                position -= count
                stream.seek(position)
                suffix = stream.read(count) + suffix
                scanned += count
                lines = suffix.split(b"\n")
                suffix = lines.pop(0)
                for raw in reversed(lines):
                    try:
                        value = json.loads(raw.rstrip(b"\r"))
                    except (UnicodeError, ValueError):
                        continue
                    if isinstance(value, dict) and value.get("taskId") == task_id:
                        return value
            if position > 0 or not suffix.strip():
                return None
            value = json.loads(suffix.rstrip(b"\r"))
            return value if isinstance(value, dict) and value.get("taskId") == task_id else None
    except (OSError, UnicodeError, ValueError):
        return None


def _task_event_is_terminal(root: Path, task_id: str) -> bool:
    event = _last_task_event(root, task_id)
    return bool(event and event.get("kind") in {"completed", "failed", "cancelled"})


@contextmanager
def _task_state_lock(root: Path, task_id: str):
    root.mkdir(parents=True, exist_ok=True)
    path = _sidecar_path(root, task_id, ".report-state.lock")
    if path.is_symlink():
        raise ValueError("task telemetry lock must not be redirected")
    with path.open("a+b") as stream:
        # Windows permits locking a byte beyond EOF. Do not initialize that
        # byte before acquiring the lock: another newly opened store may have
        # locked it already, making the buffered flush fail with EACCES.
        deadline = time.monotonic() + 5.0
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
                    raise RuntimeError("task telemetry state is busy")
                time.sleep(0.02)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _parsed_result(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _redact_text(value: str) -> str:
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+", "Bearer <redacted>", value)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "<redacted>", text)
    return _SECRET_TEXT_RE.sub(r"\1<redacted>", text)


def _fact_projection(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "<nested value omitted>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in list(value.items())[:24]:
            name = str(key)
            if _SECRET_KEY_RE.search(name):
                result[name] = "<redacted>"
            elif name.lower() in {"bytecode", "base64", "sourcetext", "sourcecode", "hlsl", "blob", "bytes"}:
                result[name] = "<large source/binary omitted>"
            else:
                result[name] = _fact_projection(child, depth + 1)
        if len(value) > 24:
            result["_omittedKeys"] = len(value) - 24
        return result
    if isinstance(value, (list, tuple)):
        rows = [_fact_projection(item, depth + 1) for item in value[:8]]
        return rows + (["<further items omitted>"] if len(value) > 8 else [])
    if isinstance(value, str):
        text = _redact_text(value)
        return text[:1024] + ("<truncated>" if len(text) > 1024 else "")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "<non-text result>"


def tool_fact(
    name: str, result: Any, *, failed: bool = False, elapsed_ms: float | None = None,
) -> dict[str, Any]:
    parsed = _parsed_result(result)
    if isinstance(parsed, Mapping) and (
        parsed.get("error") or parsed.get("isError") is True or parsed.get("ok") is False
    ):
        failed = True
    text = json.dumps(_fact_projection(parsed), ensure_ascii=False, separators=(",", ":"), default=str)
    fact: dict[str, Any] = {
        "tool": str(name)[:96], "outcome": "error" if failed else "returned",
        "summary": text[:_MAX_FACT_CHARS], "truncated": len(text) > _MAX_FACT_CHARS,
    }
    if elapsed_ms is not None:
        fact["elapsedMs"] = round(max(0.0, float(elapsed_ms)), 3)
    return fact


def _returned_artifact_paths(value: Any, depth: int = 0) -> set[str]:
    value = _parsed_result(value) if depth == 0 else value
    if depth > 8:
        return set()
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in list(value.items())[:128]:
            name = str(key)
            if _SECRET_KEY_RE.search(name):
                continue
            if name in _ARTIFACT_KEYS and isinstance(child, str) and len(child) <= 1024:
                found.add(child)
            elif isinstance(child, (Mapping, list, tuple)):
                found.update(_returned_artifact_paths(child, depth + 1))
    elif isinstance(value, (list, tuple)):
        for child in value[:128]:
            found.update(_returned_artifact_paths(child, depth + 1))
    return found


def _workspace_key(value: Any) -> str:
    return str(value or "").replace("\\", "/").casefold()


def _nonnegative_number(value: Any, *, integer: bool = False) -> int | float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = 0.0
    if not math.isfinite(number) or number < 0:
        number = 0.0
    return int(number) if integer else number


def _physical_occupancy_only(value: Any) -> dict[str, dict[str, Any]]:
    """Read only physical effect IDs from an old workflow sidecar."""

    if not isinstance(value, Mapping):
        return {}
    retained: dict[str, dict[str, Any]] = {}
    for workspace, progress in list(value.items())[-32:]:
        if not isinstance(workspace, str) or not isinstance(progress, Mapping):
            continue
        delivery = progress.get("delivery")
        if not isinstance(delivery, Mapping):
            continue
        physical = {
            "liveEditStarted": delivery.get("liveEditStarted") is True,
            "liveEditId": str(delivery.get("liveEditId") or "")[:128],
            "replayApplied": delivery.get("replayApplied") is True,
            "replacementId": str(delivery.get("replacementId") or "")[:128],
        }
        if not physical["liveEditStarted"] and not physical["replayApplied"]:
            continue
        retained[workspace[:1024]] = {"delivery": physical}
    return retained


class TaskReportStore:
    """Read and write facts only; no report gate or workflow state machine."""

    def __init__(
        self, task_id: str, event_root: Path | str | None, *,
        environ: Mapping[str, str] | None = None, conversation_id: str = "",
    ) -> None:
        self.task_id = str(task_id or "").strip()
        self.event_root = Path(event_root) if event_root else None
        self._enabled = bool(self.event_root and _TASK_ID_RE.fullmatch(self.task_id))
        values = environ if environ is not None else os.environ
        candidate = str(conversation_id or values.get(CONVERSATION_ID_ENV, "")).strip()
        self.conversation_id = candidate if _CONVERSATION_ID_RE.fullmatch(candidate) else ""
        self._lock = threading.RLock()
        self._tools_used: set[str] = set()
        self._facts: list[dict[str, Any]] = []
        self._tool_stats: dict[str, dict[str, int | float]] = {}
        self._artifact_paths: set[str] = set()
        self._reply_visuals: list[dict[str, str]] = []
        self._pending_shader_work: dict[str, dict[str, Any]] = {}
        self._shader_review_targets: dict[str, dict[str, Any]] = {}
        self._task_completion: dict[str, Any] = {}
        self._task_review_scope_pin = ""
        if self.enabled:
            with self._lock, _task_state_lock(self.event_root, self.task_id):
                self._hydrate()
                if self.conversation_id:
                    self._write_state()

    @property
    def enabled(self) -> bool:
        # Deleting a chat permanently retires its task IDs. Already-running tool
        # completions must not recreate their sidecars after cleanup.
        return bool(self._enabled and not (
            self.event_root / ".deleted-tasks" / (self.task_id + ".json")
        ).exists())

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "TaskReportStore":
        values = environ if environ is not None else os.environ
        return cls(
            values.get(TASK_ID_ENV, ""), values.get(TASK_EVENT_ROOT_ENV, ""),
            environ=values, conversation_id=values.get(CONVERSATION_ID_ENV, ""),
        )

    def _hydrate(self) -> None:
        if not self.enabled:
            return
        _report, state = read_task_report_sidecars(self.event_root, self.task_id)
        if state is None:
            # A corrupt or old alias file is stale telemetry. Physical shader
            # operations still check live capture and workspace identities.
            self._task_completion = {}
            return
        if not self.conversation_id:
            owner = str(state.get("conversationId", ""))
            if _CONVERSATION_ID_RE.fullmatch(owner):
                self.conversation_id = owner
        raw_tools = state.get("toolsUsed")
        self._tools_used.update(
            str(item) for item in (raw_tools if isinstance(raw_tools, list) else [])
            if isinstance(item, str) and item and item not in _NON_REPORTING_TASK_TOOLS
        )
        self._reply_visuals = [dict(id=row["id"]) for row in state.get("replyVisuals", [])
                              if isinstance(row, dict) and isinstance(row.get("id"), str)
                              and re.fullmatch(r"[0-9a-f]{64}", row["id"])][-8:]
        raw_paths = state.get("artifactPaths")
        self._artifact_paths.update(
            item for item in (raw_paths if isinstance(raw_paths, list) else [])
            if isinstance(item, str) and len(item) <= 1024
        )
        raw_facts = state.get("toolFacts")
        if isinstance(raw_facts, list):
            self._facts = [
                dict(item) for item in raw_facts[-_MAX_FACTS:]
                if isinstance(item, Mapping) and isinstance(item.get("tool"), str)
                and isinstance(item.get("summary"), str)
            ]
        raw_stats = state.get("toolStats")
        if isinstance(raw_stats, Mapping):
            self._tool_stats = {
                str(name)[:96]: {
                    "calls": _nonnegative_number(row.get("calls"), integer=True),
                    "errors": _nonnegative_number(row.get("errors"), integer=True),
                    "totalMs": _nonnegative_number(row.get("totalMs")),
                    "maxMs": _nonnegative_number(row.get("maxMs")),
                }
                for name, row in list(raw_stats.items())[:128]
                if isinstance(name, str) and isinstance(row, Mapping)
            }
        self._pending_shader_work = _physical_occupancy_only(state.get("pendingShaderWork"))
        review_targets = state.get("shaderOrganizationReviews")
        if isinstance(review_targets, Mapping):
            self._shader_review_targets = {
                str(key): dict(value) for key, value in review_targets.items()
                if isinstance(key, str) and isinstance(value, Mapping)
            }
        completion = state.get("taskCompletionReview")
        self._task_completion = {}
        if (isinstance(completion, Mapping) and completion.get("taskId") == self.task_id
                and re.fullmatch(r"[0-9a-f]{32}", str(completion.get("scopeId") or ""))
                and re.fullmatch(r"[0-9a-f]{64}", str(completion.get("requestFingerprint") or ""))
                and type(completion.get("workRevision")) is int and completion["workRevision"] >= 0):
            from .task_completion_review import MAX_IDENTITIES
            self._task_completion = dict(completion)
            for key in ("identities", "occurrences"):
                values = completion.get(key)
                self._task_completion[key] = [value for value in values[:MAX_IDENTITIES]
                    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)] if isinstance(values, list) else []
            if not self._task_review_scope_pin:
                self._task_review_scope_pin = completion["scopeId"]

    def sync(self) -> None:
        if self.enabled:
            with self._lock, _task_state_lock(self.event_root, self.task_id):
                self._hydrate()

    def tools_used(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._tools_used))

    def frame_reanalysis_pending(self, flow_id: str) -> bool:
        """A restarted Host must not restore the baseline as this task's draft."""
        if not self.enabled:
            return False
        self.sync()
        with self._lock:
            publications = (self._task_completion or {}).get("frameFlowPublications", {})
            publication = publications.get(flow_id, {}) if isinstance(publications, Mapping) else {}
            gaps = publication.get("gapCodes", []) if isinstance(publication, Mapping) else []
            return self.enabled and isinstance(gaps, list) and "fresh-analysis-not-published" in gaps

    def shader_organization_review_state(self) -> dict[str, Any]:
        """Return Agent review receipts with fresh, inexpensive source identities."""
        from .shader_organization_review import review_state
        self.sync()
        with self._lock:
            from .unity_restoration_requirements import annotate_transferred_shader_reviews
            return annotate_transferred_shader_reviews(review_state(self._shader_review_targets), self._task_completion or {})

    def begin_task_review(self, request_fingerprint: str, *, marked_render_target: bool = False) -> dict[str, Any]:
        """Start a fresh execution scope internally, never as an Agent tool."""
        if not self.enabled:
            raise ValueError("Task self-review requires an active task ledger")
        if not isinstance(request_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", request_fingerprint):
            raise ValueError("request_fingerprint must be a SHA-256 digest of the current user request")
        from .task_completion_review import state_view
        with self._lock, _task_state_lock(self.event_root, self.task_id):
            self._hydrate()
            self._task_review_scope_pin = uuid.uuid4().hex
            self._task_completion = {"taskId": self.task_id, "scopeId": self._task_review_scope_pin,
                "requestFingerprint": request_fingerprint, "workRevision": 0,
                "identities": [], "occurrences": [], "review": None}
            if marked_render_target:
                self._task_completion["markedRenderTarget"] = True
            # Only a new begin in this execution opts into the full-source read
            # gate. Historical restoration/registration remains unaffected.
            for target in self._shader_review_targets.values():
                target.pop("sourceReadCoverage", None)
            self._write_state()
            return state_view(self._task_completion)

    def _require_task_review_scope(self) -> dict[str, Any]:
        state = self._task_completion
        if not self.enabled or not state:
            raise ValueError("Task self-review execution has not been initialized")
        if state["scopeId"] != self._task_review_scope_pin:
            raise ValueError("Task self-review belongs to a different execution; the old task cannot write this scope")
        return state

    def record_task_review(self, verdict: str, criteria: list[dict[str, str]], findings: list[str],
                           next_actions: list[str], blocker: str = "", decision_request: dict | None = None,
                           visual_review: str = '', visual_checks: dict[str, str] | None = None,
                           reviewed_answer: dict | None = None) -> dict[str, Any]:
        """Record the main Agent's assessment; do not judge its semantic quality."""
        from .task_completion_review import make_review
        if not self.enabled:
            raise ValueError("Task self-review requires an active task ledger")
        with self._lock, _task_state_lock(self.event_root, self.task_id):
            self._hydrate()
            state = self._require_task_review_scope()
            try:
                if verdict == "complete":
                    from .unity_restoration_requirements import refresh as refresh_unity_restorations
                    refresh_unity_restorations(state)
                    # Managed restoration receipts are diagnostics, not authority
                    # over the Agent's workflow or independently observed Unity
                    # results. make_review still records the Agent's criteria,
                    # evidence and honest unmet requirements.
                    frame_publications = state.get("frameFlowPublications", {})
                    if any(not row.get("complete") for row in frame_publications.values()):
                        gaps = [code for row in frame_publications.values() for code in row.get("gapCodes", [])]
                        raise ValueError("Frame diagram is incomplete: repair the latest publication quality.gaps and republish before claiming complete. Pending: " + ", ".join(gaps[:12]))
                    from .shader_organization_review import review_state
                    from .unity_restoration_requirements import annotate_transferred_shader_reviews
                    shader_rows = [row for row in annotate_transferred_shader_reviews(
                        review_state(self._shader_review_targets), state)["targets"] if not row.get("unityTransfer")]
                    overview_pending = [row for row in shader_rows
                        if row.get("sourceWorkObserved") is not False
                        and row.get("semanticOverviewComplete") is not True]
                    if overview_pending:
                        raise ValueError("Shader calculation directory is incomplete: inspect get_shader_semantic_overview_context and publish an independent current-source review with update_shader_semantic_overview; do not recompile or repeat finish for metadata-only repair")
                    display_pending = [row for row in shader_rows
                        if row.get("semanticOverviewDisplayRequired") is True
                        and row.get("semanticOverviewDisplayComplete") is not True]
                    if display_pending:
                        raise ValueError("Shader calculation-directory UI delivery is incomplete: show_shader_workspace_source for the reviewed workspace, then get_shader_workspace_ui_state must show the same source and reviewed overview; do not recompile, revalidate ABI or repeat finish")
                    pending = [row for row in shader_rows
                               if row.get("sourceWorkObserved") is not False
                               and (row.get("cbufferSemanticDelivery") or {}).get("required")
                               and (row.get("cbufferSemanticDelivery") or {}).get("status") != "complete"]
                    if pending:
                        raise ValueError("Shader resource naming is incomplete: use the pending cbufferSemanticDelivery.nextActions to finish asset/texture review and all active CBuffer publications without recompiling")
                review = make_review(state, verdict, criteria, findings, next_actions, blocker,
                                     decision_request, visual_review, visual_checks, reviewed_answer)
            except (ValueError, TypeError) as error:
                # A failed reassessment replaces the prior verdict as well.
                # Keeping an older complete receipt would hide this failure.
                state["review"] = None
                state["reviewError"] = str(error)[:600]
                self._write_state()
                raise
            state["review"] = review
            state.pop("reviewError", None)
            self._write_state()
            return json.loads(json.dumps(review))

    def invalidate_task_review(self, reason: str) -> None:
        """Invalidate a failed runtime reassessment, including pre-call validation.

        Argument binding and framework schema checks may reject a call before
        record_task_review runs. They must not leave an older completion valid.
        """
        diagnostic = str(reason)[:600] or "Task self-review validation failed"
        with self._lock, _task_state_lock(self.event_root, self.task_id):
            self._hydrate()
            state = self._require_task_review_scope()
            if state.get("review") is None and state.get("reviewError") == diagnostic:
                return
            state["review"] = None
            state["reviewError"] = diagnostic
            self._write_state()

    def task_review_state(self) -> dict[str, Any]:
        """One cheap ledger read; no compiler/replay or artifact validation."""
        from .task_completion_review import STATE_SCHEMA, state_view, work_fingerprint
        self.sync()
        with self._lock:
            if not self._task_completion:
                return {"schema": STATE_SCHEMA, "taskId": self.task_id, "scopeId": "",
                        "requestFingerprint": "", "workRevision": 0,
                        "workFingerprint": work_fingerprint([]), "review": None, "fresh": False}
            state = self._require_task_review_scope()
            # Upgrade old task summaries in the read projection as well. Their
            # historical diagnostics must not revive the former execution gate.
            for name in ("unityRestorations", "unityFunctionalImports"):
                for row in state.get(name, {}).values():
                    row.update(diagnosticsOnly=True, workflowPolicy="agent-directed")
            return state_view(state)

    def inspected_effect_draws(self) -> set[int]:
        self.sync()
        with self._lock:
            return set(self._task_completion.get('inspectedEffectDraws', []))

    def _record_task_activity(self, tool: str, identity: str, occurrence_id: str) -> None:
        from .task_completion_review import MAX_IDENTITIES, REVIEW_TOOLS
        if tool in REVIEW_TOOLS or not self._task_completion:
            return
        state = self._require_task_review_scope()
        occurrence = hashlib.sha256((tool + "\0" + (occurrence_id or uuid.uuid4().hex)).encode("utf-8")).hexdigest()
        if occurrence in state["occurrences"]:
            return
        state["occurrences"] = [*state["occurrences"], occurrence][-MAX_IDENTITIES:]
        state["workRevision"] += 1
        fingerprint = hashlib.sha256((tool + "\0" + identity).encode("utf-8")).hexdigest()
        if fingerprint not in state["identities"]:
            if len(state["identities"]) < MAX_IDENTITIES:
                state["identities"].append(fingerprint)
            else:
                # Freshness continues to advance. Do not manufacture progress
                # from evicted identities once bounded observation storage fills.
                state["progressTrackingSaturated"] = True

    def record_task_activity(self, tool: str, identity: str, *, occurrence_id: str = "", changed: bool = True) -> None:
        """Runtime-only observation hook for native/externally executed work.

        Occurrences invalidate old reviews; stable identities alone count as new
        progress. Neither raw source nor command/argument text is persisted.
        """
        if not changed or not self.enabled:
            return
        if not isinstance(tool, str) or not tool or len(tool) > 256:
            raise ValueError("Activity tool must be a bounded identifier")
        if not isinstance(identity, str) or not identity or len(identity) > 4096:
            raise ValueError("Activity identity must be a bounded stable digest or identifier")
        if not isinstance(occurrence_id, str) or len(occurrence_id) > 1024:
            raise ValueError("Activity occurrence must be a bounded identifier")
        with self._lock, _task_state_lock(self.event_root, self.task_id):
            self._hydrate()
            self._record_task_activity(tool, identity, occurrence_id)
            self._write_state()

    def record_shader_source_read(self, observation: Mapping[str, Any]) -> None:
        """Runtime-only owned-file read evidence; no Agent-authored attestation."""
        if (not self.enabled or not isinstance(observation, Mapping)
                or observation.get("schema") != "replaylab-agent-working-file-observation/v1"
                or observation.get("action") != "read" or observation.get("scope") != "owned-workspace"
                or observation.get("workingCopy") is not True or observation.get("replayAuthority") is not False):
            return
        from .shader_source_reads import observe
        with self._lock, _task_state_lock(self.event_root, self.task_id):
            self._hydrate()
            state = self._require_task_review_scope() if self._task_completion else {}
            observe(self._shader_review_targets, observation, epoch=str(state.get("scopeId") or self.task_id))
            self._write_state()

    def shader_source_read_epoch(self) -> str:
        self.sync()
        with self._lock:
            state = self._require_task_review_scope() if self._task_completion else {}
            return str(state.get("scopeId") or self.task_id)

    def record_shader_source_edit(self, workspace: str, organization: str,
                                  previous_sha: str, current_sha: str) -> None:
        """Advance a gated read baseline after a completed Host source mutation."""
        if not self.enabled or not current_sha:
            return
        from .shader_source_reads import advance
        from .shader_organization_review import source_snapshot
        with self._lock, _task_state_lock(self.event_root, self.task_id):
            self._hydrate()
            scope = self._require_task_review_scope() if self._task_completion else {}
            epoch = str(scope.get("scopeId") or self.task_id)
            for target in self._shader_review_targets.values():
                gate = target.get("sourceReadCoverage") or {}
                if (target.get("organizationId") != organization or gate.get("epoch") != epoch
                        or _workspace_key(target.get("workspacePath", "")) != _workspace_key(workspace)):
                    continue
                snapshot = source_snapshot(workspace, organization)
                if snapshot["sourceSha256"] == current_sha:
                    advance(target, previous_sha, snapshot, Path(snapshot["sourcePath"]).stat().st_size)
            self._write_state()

    def shader_source_read_required(self, workspace: str) -> bool:
        """Cheap routing for bounded source delivery, without replay or naming checks."""
        if not self.enabled:
            return False
        self.sync()
        with self._lock:
            return any(_workspace_key(row.get("workspacePath", "")) == _workspace_key(workspace)
                       and isinstance(row.get("sourceReadCoverage"), Mapping)
                       for row in self._shader_review_targets.values())

    def _write_state(self) -> None:
        if not self.enabled:
            return
        _atomic_write_json(task_report_state_path(self.event_root, self.task_id), {
            "schema": REPORT_STATE_SCHEMA,
            "taskId": self.task_id,
            "conversationId": self.conversation_id,
            "updatedUnixNs": time.time_ns(),
            "toolsUsed": sorted(self._tools_used),
            "toolFacts": self._facts[-_MAX_FACTS:],
            "toolStats": self._tool_stats,
            "artifactPaths": sorted(self._artifact_paths)[:128],
            "replyVisuals": self._reply_visuals,
            "pendingShaderWork": self._pending_shader_work,
            "shaderOrganizationReviews": self._shader_review_targets,
            "taskCompletionReview": self._task_completion,
        })

    def register_reply_visual(self, identifier: str, presentation: dict | None = None) -> bool:
        if not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise ValueError("Invalid visual manifest identity")
        if not self.enabled:
            return False
        with self._lock, _task_state_lock(self.event_root, self.task_id):
            self._hydrate()
            if not any(row["id"] == identifier for row in self._reply_visuals):
                if len(self._reply_visuals) >= 8:
                    raise ValueError("This answer already contains eight visual bundles")
                self._reply_visuals.append({"id": identifier})
            if presentation is not None and self._task_completion:
                self._task_completion['visualPublication']={**presentation,'id':identifier}
            self._write_state()
        return True

    def record_tool(self, name: str) -> None:
        tool = str(name or "").strip()
        if not self.enabled or not tool or tool in _NON_REPORTING_TASK_TOOLS:
            return
        with self._lock, _task_state_lock(self.event_root, self.task_id):
            self._hydrate()
            self._tools_used.add(tool)
            self._write_state()

    def _occupancy_from_result(
        self, name: str, result: Any, arguments: Mapping[str, Any], failed: bool,
    ) -> None:
        """Keep physical live-edit/replacement IDs for abandoned-task cleanup."""

        payload = _parsed_result(result)
        if failed or not isinstance(payload, Mapping) or payload.get("error"):
            return
        nested = payload.get("result") if isinstance(payload.get("result"), Mapping) else {}
        workspace = str(
            arguments.get("workspace_path") or payload.get("workspacePath")
            or nested.get("workspacePath") or ""
        )
        delivery: dict[str, Any] | None = None
        matched = next((item for item in self._pending_shader_work if _workspace_key(item) == _workspace_key(workspace)), "") if workspace else ""
        if name == "finish_shader_workspace_organization":
            applied = payload.get("apply") if isinstance(payload.get("apply"), Mapping) else {}
            live = payload.get("liveEdit") if isinstance(payload.get("liveEdit"), Mapping) else {}
            live_result = live.get("result") if isinstance(live.get("result"), Mapping) else {}
            replacement = applied.get("replacement") if isinstance(applied.get("replacement"), Mapping) else {}
            if applied.get("applied") is True and replacement.get("replacementId"):
                delivery = dict(self._pending_shader_work.get(matched, {}).get("delivery") or {})
                delivery.update(replayApplied=True, replacementId=str(replacement["replacementId"])[:128])
            if live_result.get("liveEditId"):
                delivery = delivery or dict(self._pending_shader_work.get(matched, {}).get("delivery") or {})
                delivery.update(liveEditStarted=True, liveEditId=str(live_result["liveEditId"])[:128])
        elif name in {"decompile_shader_workspace", "validate_shader_workspace_delivery"}:
            report = payload.get("delivery") if isinstance(payload.get("delivery"), Mapping) else {}
            applied = report.get("apply") if isinstance(report.get("apply"), Mapping) else {}
            replacement = applied.get("replacement") if isinstance(applied.get("replacement"), Mapping) else {}
            if applied.get("applied") is True and replacement.get("replacementId"):
                delivery = dict(self._pending_shader_work.get(matched, {}).get("delivery") or {})
                delivery.update(replayApplied=True, replacementId=str(replacement["replacementId"])[:128])
        elif name == "apply_shader_workspace_shared_resource":
            replacement = payload.get("replacement") if isinstance(payload.get("replacement"), Mapping) else {}
            if payload.get("applied") is True and replacement.get("replacementId"):
                delivery = dict(self._pending_shader_work.get(matched, {}).get("delivery") or {})
                delivery.update(replayApplied=True, replacementId=str(replacement["replacementId"])[:128])
        elif name == "start_shader_workspace_live_edit":
            if nested.get("liveEditId") and nested.get("ok") is not False:
                delivery = dict(self._pending_shader_work.get(matched, {}).get("delivery") or {})
                delivery.update(liveEditStarted=True, liveEditId=str(nested["liveEditId"])[:128])
        elif name in {"stop_shader_workspace_live_edit", "rollback_shader_workspace_shared_resource"}:
            token_key = "liveEditId" if name.startswith("stop_") else "replacementId"
            token = str(arguments.get("live_edit_id" if token_key == "liveEditId" else "replacement_id") or "")
            if not matched:
                matched = next((item for item, progress in self._pending_shader_work.items()
                                if str((progress.get("delivery") or {}).get(token_key) or "") == token), "")
            replacement = payload.get("replacement") if isinstance(payload.get("replacement"), Mapping) else {}
            physically_reversed = (
                nested.get("state") == "stopped" or nested.get("stopped") is True
                if token_key == "liveEditId"
                else replacement.get("cleared") is True and payload.get("applied") is False
            )
            if matched and token and physically_reversed:
                delivery = dict(self._pending_shader_work[matched].get("delivery") or {})
                if token_key == "liveEditId":
                    delivery["liveEditStarted"] = False
                else:
                    delivery["replayApplied"] = False
                delivery.pop(token_key, None)
        if delivery is None:
            return
        target = matched or workspace
        if not target or len(target) > 1024:
            return
        if delivery.get("liveEditStarted") or delivery.get("replayApplied"):
            self._pending_shader_work[target] = {"delivery": delivery}
            self._pending_shader_work = dict(list(self._pending_shader_work.items())[-32:])
        else:
            self._pending_shader_work.pop(target, None)

    def record_result(
        self, name: str, result: Any, *, failed: bool = False,
        arguments: Mapping[str, Any] | None = None, elapsed_ms: float | None = None,
    ) -> None:
        tool = str(name or "").strip()
        if not self.enabled or not tool or tool in _NON_REPORTING_TASK_TOOLS:
            return
        with self._lock, _task_state_lock(self.event_root, self.task_id):
            self._hydrate()
            self._tools_used.add(tool)
            self._artifact_paths.update(_returned_artifact_paths(result))
            fact = tool_fact(tool, result, failed=failed, elapsed_ms=elapsed_ms)
            self._facts.append(fact)
            self._facts = self._facts[-_MAX_FACTS:]
            stat = dict(self._tool_stats.get(tool) or {})
            duration = max(0.0, float(elapsed_ms or 0.0))
            stat["calls"] = int(stat.get("calls", 0)) + 1
            stat["errors"] = int(stat.get("errors", 0)) + int(fact["outcome"] == "error")
            stat["totalMs"] = round(float(stat.get("totalMs", 0.0)) + duration, 3)
            stat["maxMs"] = round(max(float(stat.get("maxMs", 0.0)), duration), 3)
            self._tool_stats[tool] = stat
            if len(self._tool_stats) > 128:
                self._tool_stats.pop(next(iter(self._tool_stats)))
            self._occupancy_from_result(tool, result, arguments or {}, failed)
            if tool == 'inspect_event' and fact['outcome'] != 'error' and self._task_completion:
                inspection = _parsed_result(result)
                if isinstance(inspection, Mapping) and isinstance(inspection.get('drawCall'), Mapping):
                    event_id = inspection.get('eventId')
                    if type(event_id) is int and event_id > 0:
                        seen = self._task_completion.get('inspectedEffectDraws', [])
                        self._task_completion['inspectedEffectDraws'] = list(dict.fromkeys([*seen, event_id]))[-256:]
            from .shader_organization_review import track_result
            track_result(self._shader_review_targets, tool, _parsed_result(result),
                         arguments or {}, fact["outcome"] == "error",
                         read_epoch=str(self._task_completion.get("scopeId") or self.task_id))
            from .task_completion_review import activity_identity
            self._record_task_activity(tool, activity_identity(tool, _parsed_result(result), arguments), "")
            from .unity_restoration_requirements import track as track_unity_restorations
            track_unity_restorations(self._task_completion, tool, _parsed_result(result), arguments or {}, fact["outcome"] == "error")
            if tool in {"prepare_frame_render_flow", "publish_frame_render_flow"} and self._task_completion:
                payload = _parsed_result(result)
                publications = self._task_completion.setdefault("frameFlowPublications", {})
                if (tool == "prepare_frame_render_flow" and isinstance(payload, Mapping)
                        and (arguments or {}).get("reanalyze") is True
                        and payload.get("reanalysisRequested") is True and fact["outcome"] != "error"):
                    publications[str(payload["flowId"])] = {
                        "complete": False, "gapCodes": ["fresh-analysis-not-published"]}
                elif tool == "publish_frame_render_flow" and fact["outcome"] == "error":
                    # A failed later correction cannot inherit an earlier
                    # publication's completion receipt. A successful retry (or
                    # explicitly republishing the chosen valid draft) resolves it.
                    requested_flow = str((arguments or {}).get("flow_id") or "")
                    # A typo or unavailable identity is a failed attempt, not
                    # an imaginary diagram that must later be published.
                    flow = requested_flow if requested_flow in publications else "latest"
                    gaps = list(publications.get(flow, {}).get("gapCodes", []))
                    if "latest-publication-failed" not in gaps:
                        gaps.append("latest-publication-failed")
                    publications[flow] = {"complete": False, "gapCodes": gaps}
                elif (tool == "publish_frame_render_flow" and isinstance(payload, Mapping)
                      and payload.get("schema") == "bread-frame-render-flow-publication/v1"):
                    flow = str(payload.get("flowId", ""))
                    quality = payload.get("quality", {})
                    gaps = [str(gap.get("code", ""))[:120] for gap in quality.get("gaps", [])[:32]]
                    if payload.get("savedWithCapture") is False:
                        gaps.append("capture-persistence-failed")
                    window = payload.get("window", {})
                    if window.get("requested") is True and window.get("published") is not True:
                        gaps.append("diagram-window-not-opened")
                    publications.pop("latest", None)
                    publications[flow] = {"complete": quality.get("analysisComplete") is True and not gaps,
                                          "gapCodes": gaps}
                self._task_completion["frameFlowPublications"] = dict(list(publications.items())[-32:])
            self._write_state()


_STORE_LOCK = threading.Lock()
_STORE_KEY: tuple[str, str, str] | None = None
_STORE: TaskReportStore | None = None


def current_task_report_store(environ: Mapping[str, str] | None = None) -> TaskReportStore:
    global _STORE_KEY, _STORE
    values = environ if environ is not None else os.environ
    key = (
        str(values.get(TASK_ID_ENV, "")), str(values.get(TASK_EVENT_ROOT_ENV, "")),
        str(values.get(CONVERSATION_ID_ENV, "")),
    )
    with _STORE_LOCK:
        if _STORE is None or _STORE_KEY != key:
            _STORE_KEY = key
            _STORE = TaskReportStore.from_environment(values)
        return _STORE


def non_reporting_task_tool(function: Callable[..., Any]) -> Callable[..., Any]:
    """Exclude Host housekeeping from task observations."""

    setattr(function, "__replaylab_non_reporting_task_tool__", True)
    return function


def instrument_task_tool(function: Callable[..., Any]) -> Callable[..., Any]:
    """Record Host tool outcome and elapsed time without steering or blocking it."""

    signature = inspect.signature(function)
    non_reporting = bool(getattr(function, "__replaylab_non_reporting_task_tool__", False))

    def arguments_for(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
        try:
            bound = signature.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            # Arguments are only inspected for domain identities or hashed for
            # work progress; raw request/source/credential text is not stored.
            # Keeping only Shader fields hid changes to other tools' inputs.
            return dict(bound.arguments)
        except TypeError:
            return {}

    def observe(name: str, result: Any, *, failed: bool, started: float,
                args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> None:
        if non_reporting:
            return
        # Telemetry is never a precondition for a physical Host operation.
        with suppress(Exception):
            current_task_report_store().record_result(
                name, result, failed=failed, arguments=arguments_for(args, kwargs),
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            try:
                result = await function(*args, **kwargs)
            except Exception as error:
                observe(function.__name__, str(error), failed=True, started=started, args=args, kwargs=kwargs)
                raise
            observe(function.__name__, result, failed=False, started=started, args=args, kwargs=kwargs)
            return result
        return async_wrapper

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            result = function(*args, **kwargs)
        except Exception as error:
            observe(function.__name__, str(error), failed=True, started=started, args=args, kwargs=kwargs)
            raise
        observe(function.__name__, result, failed=False, started=started, args=args, kwargs=kwargs)
        return result
    return wrapper


__all__ = [
    "CONVERSATION_ID_ENV", "REPORT_SCHEMA", "REPORT_STATE_SCHEMA",
    "TASK_EVENT_ROOT_ENV", "TASK_ID_ENV", "TaskReportStore",
    "clear_task_report_sidecars", "current_task_report_store",
    "instrument_task_tool", "non_reporting_task_tool", "read_task_report_sidecars",
    "task_report_path", "task_report_state_path", "tool_fact",
]
