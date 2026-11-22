"""Delete only task telemetry and controller-owned scratch for one removed chat.

Project files, capture files and delivered work are never cleanup targets. Small
identifier-only tombstones remain so a delayed writer cannot recreate deleted
conversation text. A retry reads the ownership ledger even after sidecars vanish.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
from typing import Iterable

from .events import _exclusive_path_lock, default_task_event_root
from .shader_draft_recovery import _LAUNCH_NAMES, _safe_path, validated_turn_root

_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CONVERSATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")
OWNER_FILE = ".bread-task-owner.json"
OWNER_SCHEMA = "bread-task-workspace-owner/v1"
_LEDGER_SCHEMA = "bread-conversation-cleanup/v1"
_SUFFIXES = (".jsonl", ".report.json", ".report-state.json", ".archive.json")


def _request_path(root: Path, conversation_id: str) -> Path:
    return root / ".cleanup-pending" / (hashlib.sha256(conversation_id.encode()).hexdigest() + ".json")


def _read(path: Path) -> dict:
    _safe_path(path)
    if not path.is_file():
        return {}
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Task ownership file exceeds the safety limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _write(path: Path, value: dict) -> None:
    _safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # These contain identifiers only, never prompts or file contents. A failed
    # write stops deletion rather than losing the list needed for a retry.
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".pending")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, separators=(",", ":")))
            stream.flush()
            os.fsync(stream.fileno())
        _safe_path(path)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def record_workspace_owner(root: Path, task_id: str, conversation_id: str) -> None:
    if not _TASK_ID.fullmatch(task_id) or not _CONVERSATION_ID.fullmatch(conversation_id):
        return
    _write(validated_turn_root(root) / OWNER_FILE, {
        "schema": OWNER_SCHEMA, "taskId": task_id, "conversationId": conversation_id,
    })


def _trim_retained_launch_files(turn: Path) -> None:
    """Remove known controller launch data while leaving delivered files in place."""
    turn = validated_turn_root(turn)
    targets = [path for path in turn.iterdir()
               if re.fullmatch(r"(?:mcp-servers|live-edit-registry)-[0-9a-f]{32}\.json", path.name)]
    for name in ("agent-workspace", "cursor-workspace"):
        workspace = turn / name
        _safe_path(workspace)
        if not workspace.is_dir():
            continue
        targets.extend(path for path in workspace.iterdir()
                       if path.name.casefold() in _LAUNCH_NAMES or path.name == ".mcp.json")
        targets.extend(workspace / ".cursor" / name for name in ("cli.json", "mcp.json"))
    for path in targets:
        _safe_path(path)
        if path.is_file():
            path.unlink()


def cleanup_conversation(
    conversation_id: str, task_ids: Iterable[str] = (), *, event_root: Path | None = None,
) -> dict:
    """Called after the GUI stops this conversation's contained task processes.

    Explicit task IDs come from the GUI's stored messages, not model-generated
    paths. Historical report ownership finds tasks omitted from bounded history.
    """
    if not _CONVERSATION_ID.fullmatch(conversation_id):
        raise ValueError("Invalid conversation identifier")
    explicit = set(task_ids)
    if any(not isinstance(task, str) or not _TASK_ID.fullmatch(task) for task in explicit):
        raise ValueError("Invalid task identifier")
    root = Path(event_root) if event_root is not None else default_task_event_root()
    if not root.is_absolute():
        raise ValueError("Task cleanup requires an absolute storage root")
    _safe_path(root)
    root = root.resolve()
    ledger = root / ".deleted-conversations" / (hashlib.sha256(conversation_id.encode()).hexdigest() + ".json")
    previous = _read(ledger)
    tasks = set(explicit)
    if previous.get("schema") == _LEDGER_SCHEMA and previous.get("conversationId") == conversation_id:
        tasks.update(task for task in previous.get("taskIds", [])
                     if isinstance(task, str) and _TASK_ID.fullmatch(task))
    request_path = _request_path(root, conversation_id)
    pending = _read(request_path)
    if pending.get("schema") == _LEDGER_SCHEMA and pending.get("conversationId") == conversation_id:
        tasks.update(task for task in pending.get("taskIds", [])
                     if isinstance(task, str) and _TASK_ID.fullmatch(task))

    for state_path in root.glob("*.report-state.json"):
        task = state_path.name.removesuffix(".report-state.json")
        if not _TASK_ID.fullmatch(task):
            continue
        try:
            state = _read(state_path)
        except (OSError, ValueError):
            # A corrupt unrelated chat cannot block deletion of this one. An
            # explicit task is checked again below before anything is removed.
            continue
        if (state.get("schema") == "replaylab-task-report-state/v1"
                and state.get("taskId") == task and state.get("conversationId") == conversation_id):
            tasks.add(task)

    workspace_root = root.parent / "external-agent-workspace"
    _safe_path(workspace_root)
    owned_workspaces = []
    workspace_owners: dict[str, set[str]] = {}
    for turn in workspace_root.glob("native-*"):
        try:
            owner = _read(turn / OWNER_FILE)
            if (owner.get("schema") == OWNER_SCHEMA
                    and isinstance(owner.get("conversationId"), str)
                    and _CONVERSATION_ID.fullmatch(owner["conversationId"])
                    and isinstance(owner.get("taskId"), str) and _TASK_ID.fullmatch(owner["taskId"])):
                validated = validated_turn_root(turn)
                workspace_owners.setdefault(owner["taskId"], set()).add(owner["conversationId"])
                if owner["conversationId"] == conversation_id:
                    owned_workspaces.append(validated)
                    tasks.add(owner["taskId"])
        except (OSError, ValueError):
            continue  # Unproven ownership is never permission to remove a directory.

    # Validate the entire plan first. An old/corrupt UI reference cannot delete a
    # task whose durable owner is a different conversation.
    for task in tasks:
        if workspace_owners.get(task, set()) - {conversation_id}:
            raise ValueError("Task workspace belongs to another conversation")
        marker = _read(root / ".deleted-tasks" / (task + ".json"))
        if marker and (marker.get("taskId") != task or marker.get("conversationId") != conversation_id):
            raise ValueError("Deleted task belongs to another conversation")
        state = _read(root / (task + ".report-state.json"))
        if state and (state.get("taskId") != task or
                      state.get("conversationId") not in (None, "", conversation_id)):
            raise ValueError("Task belongs to another conversation")
        for suffix in (*_SUFFIXES, ".jsonl.lock", ".report-state.lock"):
            _safe_path(root / (task + suffix))
        _safe_path(root.parent / "logs" / ("acp-native-" + task + ".stderr.log"))

    request = {"schema": _LEDGER_SCHEMA, "conversationId": conversation_id, "taskIds": sorted(tasks)}
    _write(request_path, request)
    _write(ledger, request)
    for task in tasks:
        _write(root / ".deleted-tasks" / (task + ".json"), {
            "schema": "bread-deleted-task/v1", "taskId": task, "conversationId": conversation_id,
        })

    failures = []
    removed = 0
    for task in sorted(tasks):
        # Synchronize with writes already in flight before the deletion marker.
        # New writers check it before locking and again before writing data.
        locks = [root / (task + suffix) for suffix in (".jsonl.lock", ".report-state.lock")]
        try:
            with ExitStack() as stack:
                for path in locks:
                    stack.enter_context(_exclusive_path_lock(path))
                for suffix in _SUFFIXES:
                    path = root / (task + suffix)
                    _safe_path(path)
                    if path.exists():
                        path.unlink()
                        removed += 1
            paths = [*locks, root.parent / "logs" / ("acp-native-" + task + ".stderr.log")]
            for path in paths:
                _safe_path(path)
                # Windows process termination may still be closing log handles.
                for attempt in range(5):
                    try:
                        path.unlink(missing_ok=True)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.05 * (attempt + 1))
        except (OSError, ValueError) as error:
            failures.append({"taskId": task, "reason": str(error)})

    preserved = []
    from .agent_context import ProviderContext
    for turn in owned_workspaces:
        receipt = ProviderContext.cleanup_turn_workspace(turn)
        if receipt and receipt.get("sourceRetained"):
            # Authored source/reports/images stay at their delivered locations.
            # Retention failures are reported, never resolved by forced deletion.
            preserved.append(str(turn))
            if not (receipt.get("schema") == "replaylab-owned-work-output-retention/v1"
                    and receipt.get("outputCount", 0) > 0):
                failures.append({"workspace": str(turn), "reason": receipt.get("reason", "")})
            else:
                try:
                    _trim_retained_launch_files(turn)
                except (OSError, ValueError) as error:
                    failures.append({"workspace": str(turn), "reason": str(error)})
    if not failures:
        _safe_path(request_path)
        request_path.unlink(missing_ok=True)
    return {"schema": _LEDGER_SCHEMA, "conversationId": conversation_id,
            "taskIds": sorted(tasks), "removedFiles": removed,
            "preservedWorkspaces": preserved, "failures": failures, "complete": not failures}


def cleanup_pending_conversations(*, event_root: Path | None = None) -> dict:
    """Resume confirmed deletions interrupted by application shutdown or a busy file."""
    root = Path(event_root) if event_root is not None else default_task_event_root()
    if not root.is_absolute():
        raise ValueError("Task cleanup requires an absolute storage root")
    _safe_path(root)
    task_ids = set()
    failures = []
    for path in (root / ".cleanup-pending").glob("*.json"):
        try:
            request = _read(path)
            conversation = request.get("conversationId", "")
            if (request.get("schema") != _LEDGER_SCHEMA or not isinstance(conversation, str)
                    or not _CONVERSATION_ID.fullmatch(conversation)
                    or path != _request_path(root, conversation)):
                raise ValueError("Invalid cleanup request identity")
            result = cleanup_conversation(conversation, event_root=root)
            task_ids.update(result["taskIds"])
            failures.extend(result["failures"])
        except (OSError, ValueError) as error:
            failures.append({"request": path.name, "reason": str(error)})
    return {"schema": _LEDGER_SCHEMA, "taskIds": sorted(task_ids),
            "failures": failures, "complete": not failures}
