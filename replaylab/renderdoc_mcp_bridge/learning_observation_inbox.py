"""Python 3.6-compatible fact writer for pre-AgentHost failures.

This module intentionally has no dependency on ReplayLab AgentHost or Qt.  A
qrenderdoc/bridge failure can therefore be recorded before the AgentHost is
started.  The writer only emits a versioned observation envelope; it never
loads, edits, or promotes a Skill, tool, MCP server, or registry entry.
"""

from __future__ import absolute_import, print_function

import hashlib
import json
import os
import re
import tempfile
import time
import uuid


OBSERVATION_SCHEMA = "replaylab-learning-observation/v1"
MAX_OBSERVATION_BYTES = 256 * 1024
MAX_MESSAGE_CHARS = 16 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def default_learning_observation_root():
    """Return the cross-process inbox shared with AgentHost startup ingest."""

    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "ReplayLab", "learning-observation-inbox")


def _text(value, label, maximum, required=True):
    value = str(value or "").strip()
    if required and not value:
        raise ValueError("%s is required" % label)
    if len(value) > maximum or "\0" in value:
        raise ValueError("%s is invalid or too long" % label)
    return value


def _safe_id(value, label):
    value = str(value or "").strip()
    if value and not _SAFE_ID_RE.match(value):
        raise ValueError("%s is not a safe stable identity" % label)
    return value


def _digest(value, label):
    value = str(value or "").strip().lower()
    if value and not _SHA256_RE.match(value):
        raise ValueError("%s must be a lowercase SHA-256 digest" % label)
    return value


def _canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def write_learning_observation(
    source_version,
    component,
    phase,
    exception_type,
    message,
    root=None,
    source="bridge",
    task_id="",
    run_id="",
    terminal_status="failed",
    tool_or_route="",
    capture_session_id="",
    snapshot_id="",
    event_id=0,
    stage="",
    resource_id="",
    raw_sha256="",
    provider_or_toolchain="",
    registry_sha256="",
    evidence_digests=(),
    created_unix_ms=None,
):
    """Atomically write one strict observation and return its id/path.

    All values are bounded scalars or digests.  File paths, arbitrary JSON and
    executable candidate content are intentionally absent from this contract.
    """

    if source not in ("qrenderdoc", "bridge", "agenthost"):
        raise ValueError("source is invalid")
    if isinstance(event_id, bool):
        raise ValueError("event id is invalid")
    event_id = int(event_id or 0)
    if event_id < 0 or event_id > 0xFFFFFFFF:
        raise ValueError("event id is invalid")
    created = int(time.time() * 1000) if created_unix_ms is None else int(
        created_unix_ms
    )
    if created < 0:
        raise ValueError("created time is invalid")
    evidence = tuple(_digest(item, "evidence digest") for item in evidence_digests)
    if len(evidence) > 64 or len(evidence) != len(set(evidence)):
        raise ValueError("evidence digests are invalid")
    material = {
        "schema": OBSERVATION_SCHEMA,
        "schemaVersion": 1,
        "createdUnixMs": created,
        "source": source,
        "sourceVersion": _text(source_version, "source version", 128),
        "taskId": _safe_id(task_id, "task id"),
        "runId": _safe_id(run_id, "run id"),
        "terminalStatus": _text(terminal_status, "terminal status", 64),
        "component": _text(component, "component", 256),
        "phase": _text(phase, "phase", 256),
        "exceptionType": _text(exception_type, "exception type", 256),
        "message": _text(message, "message", MAX_MESSAGE_CHARS),
        "toolOrRoute": _text(tool_or_route, "tool or route", 256, False),
        "capture": {
            "captureSessionId": _text(
                capture_session_id, "capture session id", 1024, False
            ),
            "snapshotId": _digest(snapshot_id, "snapshot id"),
            "eventId": event_id,
            "stage": _text(stage, "stage", 64, False),
            "resourceId": _text(resource_id, "resource id", 256, False),
            "rawSha256": _digest(raw_sha256, "raw shader digest"),
        },
        "providerOrToolchain": _text(
            provider_or_toolchain, "provider or toolchain", 256, False
        ),
        "registrySha256": _digest(registry_sha256, "registry digest"),
        "evidenceDigests": list(evidence),
    }
    observation_id = "observation-" + hashlib.sha256(
        _canonical_bytes(material)
    ).hexdigest()
    envelope = dict(material)
    envelope["observationId"] = observation_id
    encoded = _canonical_bytes(envelope)
    if len(encoded) > MAX_OBSERVATION_BYTES:
        raise ValueError("learning observation exceeds the 256 KiB safety bound")

    root = os.path.abspath(root or default_learning_observation_root())
    incoming = os.path.join(root, "incoming")
    if not os.path.isdir(incoming):
        try:
            os.makedirs(incoming)
        except OSError:
            if not os.path.isdir(incoming):
                raise
    destination = os.path.join(incoming, observation_id + ".json")
    temporary = os.path.join(
        incoming,
        ".%s.%s.%s.tmp" % (observation_id, os.getpid(), uuid.uuid4().hex),
    )
    try:
        with open(temporary, "xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass
    return {"observationId": observation_id, "path": destination}


__all__ = [
    "MAX_OBSERVATION_BYTES",
    "OBSERVATION_SCHEMA",
    "default_learning_observation_root",
    "write_learning_observation",
]
