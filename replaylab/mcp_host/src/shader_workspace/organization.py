"""Editable shader drafts with a final compile, ABI, and publication boundary.

The Agent chooses how to organize the proposal source. The Host checks source
identity and publication safety when the Agent finishes; captured-output
comparisons cover the tested capture only, not every possible input.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import copy
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
from typing import Any
import uuid

from ..host_runtime import record_operation_timing
from . import aliases as cbuffer_aliases
from .equivalence import (
    EquivalenceError,
    apply_renames,
    build_verdict,
    insert_function_comments,
    rename_only_equivalent,
    split_bound_renames,
    MAX_RENAMES_PER_REQUEST,
    MAX_RENAMES_TOTAL,
    validate_renames,
)
from .io import atomic_write_bytes, atomic_write_json, is_within, read_json_object
from .publication_receipts import CompletedShaderReviews, OrganizationCheckpoints
from .workspace_mutation_transaction import (
    apply_transaction as apply_workspace_mutation,
    pending_transaction_owner as pending_workspace_mutation_owner,
    recover_transaction as recover_workspace_mutation,
)
from .source_language import SOURCE_LANGUAGES, organization_capabilities, validate_language_renames
from .source_observation import source_observation_transaction
from .flow_graph import _function_records as _function_records_for_template
from .flow_graph import (
    MAX_FLOW_GRAPH_BYTES,
    ShaderFlowGraphError,
    build_shader_flow_graph,
    module_description_template,
    validate_shader_flow_graph,
)
from .leases import (
    SOURCE_TRANSACTION_BACKUP_FILE,
    SOURCE_TRANSACTION_JOURNAL_FILE,
    WorkspaceSourceLeaseError,
    WorkspaceSourceTransactionLease,
)


ORGANIZATION_SESSION_SCHEMA = "renderdoc-ai-shader-organization-session/v1"
ORGANIZATION_GATE_SCHEMA = "renderdoc-ai-shader-organization-gate/v1"
ORGANIZATION_POLICY_VERSION = "renderdoc-ai-organize-workspace/v2"
LEGACY_ORGANIZATION_POLICY_VERSIONS = {"renderdoc-ai-organize-workspace/v1"}
ORGANIZATION_OBSERVATION_SCHEMA = "renderdoc-ai-shader-organization-observation/v1"
OPEN_DRAFT_STATES = frozenset({"drafting", "awaiting-proposal-review"})
_ORGANIZATION_LOGGER = logging.getLogger("renderdoc-mcp.shader-organization")


def _draft_status(service, workspace_path):
    """Source editing has no dependency on compiler or replay ABI availability."""
    reader = (getattr(service, "source_edit_status", None)
              if hasattr(type(service), "source_edit_status") or "source_edit_status" in vars(service)
              else None)
    return reader(workspace_path) if callable(reader) else service.status(workspace_path)


def active_draft_id(workspace_path: str | Path) -> str:
    """Read the current local draft pointer; historical sessions do not own files.

    This is a deferral hint, never compile/apply authority. A new begin writes
    this pointer before exposing its proposal. Final physical checks continue
    to validate their own exact source, manifest and capture bindings.
    """
    root = Path(workspace_path).resolve()
    gate_path = root / "ai" / "organize-code-gate.json"
    if not gate_path.exists():
        return ""
    gate = read_json_object(gate_path, MAX_CONTEXT_BYTES, "organization draft pointer")
    if gate.get("schema") != ORGANIZATION_GATE_SCHEMA:
        return ""
    organization_id = _hex(gate.get("organizationId"), "organization_id", 32)
    session_dir = root / "ai" / "organization-sessions" / organization_id
    if not (session_dir / "session.json").exists():
        # A begin may be publishing its files. It already deferred observation.
        return organization_id if gate.get("decision") == "organization-beginning" else ""
    _safe_dir(session_dir, root, "organization session directory", create=False)
    session = _read_session_file(session_dir, "organization draft pointer")
    if (session.get("schema") != ORGANIZATION_SESSION_SCHEMA
            or session.get("organizationId") != organization_id):
        raise ShaderOrganizationError("organization draft pointer identity is invalid")
    return organization_id if session.get("state") in OPEN_DRAFT_STATES else ""


class _OperationTiming:
    """Time a draft operation without recording its Shader source or user text.

    Phases are exclusive wall-clock intervals. The result includes the same
    timings that the Host logs on failures, so a slow or rejected operation can
    be diagnosed without replaying the mutation. Waiting for the manager lock
    is counted separately from the actual workspace work.
    """

    def __init__(self, tool: str, operation: str) -> None:
        self.tool = tool
        self.operation = operation if operation in {
            "extract", "rewrite", "rename_function", "undo", "remove", "note", "list", "rename", "verify"
        } else "invalid"
        self._started_ns = 0
        self._phase_started_ns = 0
        self._phase = "lockWait"
        self._phases_ns: dict[str, int] = {}
        self._outcome = "error"
        self._dimensions: dict[str, int | str] = {}
        self._failure_category = ""

    def __enter__(self) -> "_OperationTiming":
        self._started_ns = self._phase_started_ns = time.perf_counter_ns()
        return self

    def _close_phase(self, now_ns: int) -> None:
        self._phases_ns[self._phase] = (
            self._phases_ns.get(self._phase, 0) + now_ns - self._phase_started_ns
        )

    def mark(self, phase: str) -> None:
        now_ns = time.perf_counter_ns()
        self._close_phase(now_ns)
        self._phase = phase
        self._phase_started_ns = now_ns

    def dimensions(self, **values: int | str) -> None:
        # Only bounded IDs and aggregate counts enter diagnostics. Never add
        # source snippets, aliases, paths, comments or model-provided text.
        allowed = {
            "organizationId", "operationId", "sourceByteSize", "sourceLineCount",
            "ledgerOperationCount", "renameCount", "incomingRenameCount",
            "affectedLineCount", "editCount", "candidateRole", "structureReportCacheHit",
        }
        for key, value in values.items():
            if key not in allowed:
                continue
            if key == "organizationId":
                if isinstance(value, str) and _ID.fullmatch(value):
                    self._dimensions[key] = value
            elif key == "operationId":
                if isinstance(value, str) and _MUTATION_OPERATION_ID.fullmatch(value):
                    self._dimensions[key] = value
            elif key == "candidateRole":
                if value in {"baseline", "proposal", "recovery", "other"}:
                    self._dimensions[key] = str(value)
            elif type(value) is int and value >= 0:
                self._dimensions[key] = value

    def outcome(self, value: str) -> None:
        if value in {"accepted", "rejected", "reused", "validated"}:
            self._outcome = value

    def failure_category(self, value: str) -> None:
        if value in {
            "proposal-drift", "range-or-rewrite", "structural-replay", "ledger-limit"
        }:
            self._failure_category = value

    def snapshot(self) -> dict[str, Any]:
        now_ns = time.perf_counter_ns()
        phases = dict(self._phases_ns)
        phases[self._phase] = phases.get(self._phase, 0) + now_ns - self._phase_started_ns
        return {
            "schema": "replaylab-shader-organization-operation-timing/v1",
            "totalMs": round((now_ns - self._started_ns) / 1_000_000, 3),
            "dimensions": dict(self._dimensions),
            "activePhase": self._phase,
            "failureCategory": self._failure_category or None,
            "phasesMs": {
                key: round(value / 1_000_000, 3)
                for key, value in phases.items()
            },
        }

    def attach(self, result: dict[str, Any]) -> dict[str, Any]:
        # Timings are transport diagnostics, not durable mutation authority.
        # The sealed receipt remains independent of machine speed.
        self._outcome = (
            "accepted" if result.get("accepted") is True
            else "rejected" if result.get("accepted") is False
            else "returned"
        )
        return {**result, "timing": self.snapshot()}

    def __exit__(self, error_type: Any, error: Any, traceback: Any) -> None:
        timing = self.snapshot()
        outcome = "error" if error_type is not None else self._outcome
        error_name = error_type.__name__ if error_type is not None else ""
        try:
            _ORGANIZATION_LOGGER.info(
                "organization-operation-timing %s",
                json.dumps({
                    "tool": self.tool,
                    "operation": self.operation,
                    "outcome": outcome,
                    "errorType": error_name or None,
                    **timing,
                }, separators=(",", ":"), sort_keys=True),
            )
        except Exception:
            pass
        record_operation_timing(
            tool=self.tool, operation=self.operation, outcome=outcome,
            timing=timing, error_type=error_name,
        )

MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_CONTEXT_BYTES = 2 * 1024 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[0-9a-f]{32}$")
_MUTATION_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_MAX_DURABLE_MUTATION_RECEIPTS = 512
_UNSAFE_HLSL_DIRECTIVE = re.compile(
    r"^[ \t]*#[ \t]*(?:include|line)\b", re.IGNORECASE | re.MULTILINE
)


class ShaderOrganizationError(RuntimeError):
    """A proposal could not pass the bounded organization transaction."""


def _organization_continuation_error(
    message: str,
    *,
    code: str,
    root: Path,
    organization_id: str,
    baseline_source_sha256: str = "",
    current_source_sha256: str = "",
    detail: str = "",
) -> ShaderOrganizationError:
    """Describe incompatible prior evidence for automatic session isolation.

    An accepted historical stage is useful evidence, not authority over a new
    explicit organization request.  The caller preserves that evidence and
    starts a current-source session automatically; no Host/user decision gate
    is attached to the error.
    """

    organization = str(organization_id or "")
    diagnostic = {
        "schema": "replaylab-shader-organization-continuation-diagnostic/v1",
        "schemaVersion": 1,
        "state": "auto-isolated",
        "code": str(code),
        "workspacePath": str(root),
        "organizationId": organization,
        "resumeSameOrganizationId": False,
        "baselineSourceSha256": str(baseline_source_sha256 or ""),
        "currentSourceSha256": str(current_source_sha256 or ""),
        "detail": str(detail or message)[:2000],
        "userActionRequired": False,
        "hostBlocksBegin": False,
        "priorEvidenceReused": False,
        "currentSourceRemainsAuthoritative": True,
        "automaticAction": "isolate-prior-artifacts-and-start-current-source-session",
    }
    error = ShaderOrganizationError(message)
    error.continuation_diagnostic = diagnostic  # type: ignore[attr-defined]
    return error


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_TARGET_PROFILE_IDENTITY_FIELDS = (
    "profile",
    "stage",
    "containerFormat",
    "executableChunk",
    "programVersionToken",
    "shaderModelMajor",
    "shaderModelMinor",
    "targetEnvironment",
    "environment",
)


def _shader_program_identity(status: Mapping[str, Any]) -> dict[str, Any]:
    """Return the capture-independent physical identity of one Shader program.

    Capture/session IDs, EIDs, resource handles, workspace IDs and filesystem
    paths are intentionally absent.  They describe where the program was seen,
    not which program it is. Exact captured bytes are the sole durable
    compatibility identity. Stage/profile/ABI are retained as useful
    diagnostics and are revalidated when a compiled candidate is published,
    but mutable metadata can never deny organization of identical bytecode.
    """

    baseline = status.get("baseline")
    baseline = baseline if isinstance(baseline, Mapping) else {}
    shader = status.get("shader")
    shader = shader if isinstance(shader, Mapping) else {}
    baseline_raw = str(baseline.get("rawSha256") or "").strip().lower()
    shader_raw = str(shader.get("rawSha256") or "").strip().lower()
    raw_values = [value for value in (baseline_raw, shader_raw) if value]
    if not raw_values or any(_SHA256.fullmatch(value) is None for value in raw_values):
        raise ShaderOrganizationError(
            "workspace does not expose the captured Shader bytecode identity"
        )
    if any(value != raw_values[0] for value in raw_values[1:]):
        raise ShaderOrganizationError(
            "workspace captured Shader bytecode identities disagree"
        )

    stage = str(status.get("stage") or shader.get("stage") or "").strip().lower()

    target = status.get("targetProfile")
    target = target if isinstance(target, Mapping) else {}
    profile = {
        key: target[key]
        for key in _TARGET_PROFILE_IDENTITY_FIELDS
        if key in target and target[key] not in (None, "")
    }
    profile_sha = _sha256(_canonical_json(profile)) if profile else ""

    physical_abi = str(baseline.get("abiSha256") or "").strip().lower()
    if physical_abi and _SHA256.fullmatch(physical_abi) is None:
        raise ShaderOrganizationError("workspace captured Shader ABI identity is invalid")
    if not physical_abi:
        compiled_interface = baseline.get("compiledInterface")
        if isinstance(compiled_interface, Mapping):
            physical_abi = _sha256(_canonical_json(compiled_interface))

    return {
        "schema": "replaylab-shader-program-identity/v1",
        "originalRawSha256": raw_values[0],
        "stage": stage,
        "targetProfile": profile,
        "targetProfileSha256": profile_sha,
        "physicalAbiSha256": physical_abi,
    }


def _abi_repair_program_identity(status: Mapping[str, Any]) -> dict[str, Any]:
    """Return the strict physical target identity for an in-place ABI repair.

    Organization drafts may follow identical captured bytecode across volatile
    capture/workspace locations. Source repair is narrower: it temporarily
    swaps the canonical source and compiles it, so the raw program, stage,
    entry point and captured interface layout must stay fixed for the entire
    transaction. A preserved decompiler file is intentionally absent from
    this identity; it is provenance evidence, not authority over the current
    readable source.
    """

    program = _shader_program_identity(status)
    shader = status.get("shader")
    shader = shader if isinstance(shader, Mapping) else {}

    status_stage = str(status.get("stage") or "").strip().lower()
    shader_stage = str(shader.get("stage") or "").strip().lower()
    stages = [value for value in (status_stage, shader_stage) if value]
    if not stages or any(value != stages[0] for value in stages[1:]):
        raise ShaderOrganizationError(
            "workspace captured Shader stage identity is missing or inconsistent"
        )

    status_entry = str(status.get("entryPoint") or "").strip()
    shader_entry = str(shader.get("entryPoint") or "").strip()
    entries = [value for value in (status_entry, shader_entry) if value]
    if not entries or any(value != entries[0] for value in entries[1:]):
        raise ShaderOrganizationError(
            "workspace captured Shader entry-point identity is missing or inconsistent"
        )

    layout_sha = str(program.get("physicalAbiSha256") or "").strip().lower()
    if _SHA256.fullmatch(layout_sha) is None:
        raise ShaderOrganizationError(
            "workspace does not expose the captured Shader interface layout identity"
        )
    baseline = status.get("baseline")
    baseline = baseline if isinstance(baseline, Mapping) else {}
    compiled_interface = baseline.get("compiledInterface")
    if not isinstance(compiled_interface, Mapping):
        raise ShaderOrganizationError(
            "workspace does not expose the captured Shader compiled interface"
        )
    compiled_interface_sha = _sha256(_canonical_json(compiled_interface))

    return {
        "schema": "replaylab-shader-abi-repair-program-identity/v1",
        "originalRawSha256": program["originalRawSha256"],
        "stage": stages[0],
        "entryPoint": entries[0],
        "targetProfileSha256": str(program.get("targetProfileSha256") or ""),
        "physicalAbiSha256": layout_sha,
        "compiledInterfaceSha256": compiled_interface_sha,
    }


def _source_bound_user_aliases(evidence: Mapping[str, Any] | None) -> dict[str, dict[str, str]]:
    """Extract identifier-safe user choices from Host-read CBuffer evidence.

    The returned map is sealed into the organization session at begin.  The
    editable semantic-context copy is never consulted as authority.
    """

    result: dict[str, dict[str, str]] = {}
    blocks = evidence.get("blocks") if isinstance(evidence, Mapping) else None
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, Mapping):
            continue
        owner = str(block.get("block") or "")
        if re.fullmatch(r"[A-Za-z_]\w*", owner) is None:
            continue
        layout_sha = str(block.get("layoutSha256") or "")
        candidates: list[tuple[str, str]] = []
        block_alias = block.get("blockAlias")
        if (
            isinstance(block_alias, Mapping)
            and block_alias.get("source") == "user"
        ):
            candidates.append(("cbuffer:" + owner, str(block_alias.get("alias") or "")))
        for entry in block.get("user", []) if isinstance(block.get("user"), list) else []:
            if not isinstance(entry, Mapping):
                continue
            member = str(entry.get("path") or "")
            if re.fullmatch(r"[A-Za-z_]\w*", member) is None:
                continue
            candidates.append((owner + "." + member, str(entry.get("alias") or "")))
        for selector, target in candidates:
            if (
                re.fullmatch(r"[A-Za-z_]\w*", target) is None
                or not cbuffer_aliases.is_valid_alias(target)
            ):
                continue
            receipt = {"targetName": target, "layoutSha256": layout_sha}
            previous = result.get(selector)
            if previous is None:
                result[selector] = receipt
            elif previous != receipt:
                # Duplicate source selectors with contradictory user aliases
                # are not uniquely source-bound and therefore grant no rename.
                result.pop(selector, None)
    return result


def _json_size(value: Any) -> int:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ShaderOrganizationError("organization evidence is not JSON serializable") from exc


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ShaderOrganizationError(
            "organization session is not JSON serializable"
        ) from exc


def _regular_bytes(path: Path, maximum: int, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ShaderOrganizationError("%s is missing or redirected" % label)
    before = path.stat()
    if before.st_size <= 0 or before.st_size > maximum:
        raise ShaderOrganizationError("%s has an invalid byte size" % label)
    value = path.read_bytes()
    after = path.stat()
    if (
        len(value) != before.st_size
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ShaderOrganizationError("%s changed while it was read" % label)
    return value


def _utf8(path: Path, maximum: int, label: str) -> tuple[str, bytes]:
    value = _regular_bytes(path, maximum, label)
    if b"\x00" in value:
        raise ShaderOrganizationError("%s contains NUL" % label)
    try:
        return value.decode("utf-8", errors="strict"), value
    except UnicodeError as exc:
        raise ShaderOrganizationError("%s is not strict UTF-8" % label) from exc


def _safe_dir(path: Path, parent: Path, label: str, *, create: bool) -> Path:
    if not is_within(parent, path):
        raise ShaderOrganizationError("%s escapes its workspace" % label)
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise ShaderOrganizationError("%s is redirected" % label)
    elif create:
        path.mkdir(parents=False, exist_ok=False)
    else:
        raise ShaderOrganizationError("%s does not exist" % label)
    if path.resolve() != path:
        raise ShaderOrganizationError("%s is redirected" % label)
    return path


def _hex(value: Any, label: str, length: int = 64) -> str:
    text = str(value or "").strip().lower()
    matcher = _SHA256 if length == 64 else _ID
    if matcher.fullmatch(text) is None:
        raise ShaderOrganizationError("%s is invalid" % label)
    return text


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ShaderOrganizationError("%s is invalid" % label)
    return value


def _baseline_source_provenance(
    status: Mapping[str, Any],
    ai_dir: Path,
    source_bytes: bytes,
    source_sha: str,
    original_raw_sha: str,
) -> dict[str, Any]:
    """Preserve genuine reconstruction bytes without relabelling imported edits.

    ``decompiledSha256`` is a legacy status field for the initial source hash;
    project imports and toolchain migrations use it too. Hash equality alone
    therefore does not establish that the source came from a decompiler.
    Existing, incorrectly named artifacts remain intact but are not advertised
    as a numerical recovery baseline.
    """
    source = status["source"]
    initial_sha = _hex(
        source.get("decompiledSha256"), "workspace initial source SHA-256"
    )
    origin = str(source.get("origin") or "unknown")
    decompiler_origin = origin in {"trusted-decompiler", "trusted-decompile-route"}
    raw_original = ai_dir / ("decompiler-original." + str(source["format"]))
    preserved = False
    observed_sha = ""
    fallback_reason = ""
    if raw_original.exists() or raw_original.is_symlink():
        try:
            observed_sha = _sha256(_regular_bytes(
                raw_original, MAX_SOURCE_BYTES, "preserved decompiler source"
            ))
            preserved = decompiler_origin and observed_sha == initial_sha
            if not decompiler_origin:
                fallback_reason = "initial-source-is-not-decompiler-output"
            elif not preserved:
                fallback_reason = "preserved-decompiler-source-hash-mismatch"
            elif source_sha != initial_sha:
                fallback_reason = "current-source-differs-from-decompiler-output"
        except Exception as error:
            fallback_reason = "preserved-decompiler-source-unusable: " + str(error)[:800]
    elif not decompiler_origin:
        fallback_reason = "initial-source-is-not-decompiler-output"
    elif source_sha == initial_sha:
        atomic_write_bytes(raw_original, source_bytes)
        preserved = True
        observed_sha = source_sha
    else:
        fallback_reason = "decompiler-original-missing-for-edited-source"

    if source_sha != initial_sha:
        kind = "current-workspace-source-fallback"
    elif decompiler_origin:
        kind = "decompiler-output"
    elif origin == "verified-project-source":
        kind = "verified-project-source"
    else:
        kind = "preserved-workspace-source"
    return {
        "schema": "replaylab-shader-organization-baseline-provenance/v1",
        "schemaVersion": 1,
        "kind": kind,
        "baselineSourceSha256": source_sha,
        "initialSourceOrigin": origin,
        "initialSourceSha256": initial_sha,
        "declaredDecompilerSourceSha256": initial_sha if decompiler_origin else None,
        "decompilerOriginalPreserved": preserved,
        "decompilerOriginalObservedSha256": observed_sha,
        "fallbackReason": fallback_reason,
        "originalShaderRawSha256": original_raw_sha,
        "semanticEquivalenceVerified": False,
    }


def _exclusive_durable_write(path: Path, value: bytes, label: str) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ShaderOrganizationError("%s already exists or is unavailable" % label) from exc
    try:
        remaining = memoryview(value)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise ShaderOrganizationError("%s write stalled" % label)
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_locked_source(handle: Any, value: bytes, label: str) -> None:
    handle.seek(0)
    remaining = memoryview(value)
    while remaining:
        written = handle.write(remaining)
        if not isinstance(written, int) or written <= 0:
            raise ShaderOrganizationError("%s write stalled" % label)
        remaining = remaining[written:]
    handle.truncate(len(value))
    handle.flush()
    os.fsync(handle.fileno())


def _remove_source_transaction_evidence(journal: Path, backup: Path) -> None:
    # Journal first: a crash after this unlink still leaves the backup, which
    # independently prevents the shared lease from being released.
    for artifact in (journal, backup):
        if not artifact.exists() and not artifact.is_symlink():
            continue
        if artifact.is_symlink() or not artifact.is_file():
            raise ShaderOrganizationError(
                "source transaction recovery evidence is missing or redirected"
            )
        artifact.unlink()


def _source_compare_and_swap(
    path: Path,
    expected: bytes,
    replacement: bytes,
    label: str,
) -> bool:
    """Conditionally replace source while an OS lock protects compare/write.

    The workspace lease excludes every managed writer.  The source-file lock
    closes the remaining compare/replace window for ordinary Windows editor
    writes (and for cooperating POSIX writers).  Keeping the original handle
    open also prevents a Windows rename-over save; on POSIX an unmanaged
    rename is detected by the post-write inode check without touching its new
    pathname.
    """

    if path.is_symlink() or not path.is_file():
        raise ShaderOrganizationError("%s is missing or redirected" % label)
    try:
        if os.name == "nt":
            # Python's normal CRT open shares write access. Open the pathname
            # with Win32 share-read only so a competing editor cannot truncate,
            # write, delete, or rename it between the comparison and write.
            import ctypes
            from ctypes import wintypes
            import msvcrt

            create_file = ctypes.windll.kernel32.CreateFileW
            create_file.argtypes = (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            )
            create_file.restype = wintypes.HANDLE
            win_handle = create_file(
                str(path),
                0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
                0x00000001,  # FILE_SHARE_READ
                None,
                3,  # OPEN_EXISTING
                0x00000080 | 0x00200000,  # NORMAL | OPEN_REPARSE_POINT
                None,
            )
            invalid_handle = ctypes.c_void_p(-1).value
            if win_handle == invalid_handle:
                raise ctypes.WinError()
            try:
                descriptor = msvcrt.open_osfhandle(
                    int(win_handle), os.O_RDWR | os.O_BINARY
                )
            except Exception:
                ctypes.windll.kernel32.CloseHandle(win_handle)
                raise
            handle = os.fdopen(descriptor, "r+b", buffering=0)
        else:  # pragma: no cover - Windows is the supported ReplayLab host.
            handle = path.open("r+b", buffering=0)
    except OSError as exc:
        raise ShaderOrganizationError("%s could not be opened exclusively" % label) from exc
    locked = False
    windows_lock = os.name == "nt"
    try:
        handle.seek(0)
        if windows_lock:
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, MAX_SOURCE_BYTES)
            except OSError as exc:
                raise ShaderOrganizationError(
                    "%s is being edited by another process" % label
                ) from exc
        else:  # pragma: no cover - Windows is the supported ReplayLab host.
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ShaderOrganizationError(
                    "%s is being edited by another process" % label
                ) from exc
        locked = True

        opened = os.fstat(handle.fileno())
        named = path.stat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or path.is_symlink()
            or opened.st_dev != named.st_dev
            or opened.st_ino != named.st_ino
        ):
            raise ShaderOrganizationError("%s identity changed before CAS" % label)
        handle.seek(0)
        observed = handle.read(MAX_SOURCE_BYTES + 1)
        if len(observed) > MAX_SOURCE_BYTES:
            raise ShaderOrganizationError("%s exceeds its fixed size limit" % label)
        if observed != expected:
            return False

        root = path.parent.parent
        journal = root / SOURCE_TRANSACTION_JOURNAL_FILE
        backup = root / SOURCE_TRANSACTION_BACKUP_FILE
        if any(
            artifact.exists() or artifact.is_symlink()
            for artifact in (journal, backup)
        ):
            raise ShaderOrganizationError(
                "workspace has unresolved source transaction recovery evidence"
            )
        _exclusive_durable_write(backup, expected, "source transaction backup")
        journal_value = _canonical_json(
            {
                "schema": "renderdoc-shader-source-transaction/v1",
                "sourcePath": str(path.relative_to(root)).replace("\\", "/"),
                "expectedSourceSha256": _sha256(expected),
                "replacementSourceSha256": _sha256(replacement),
                "backupFile": SOURCE_TRANSACTION_BACKUP_FILE,
            }
        ) + b"\n"
        try:
            _exclusive_durable_write(
                journal, journal_value, "source transaction journal"
            )
        except Exception:
            try:
                backup.unlink()
            except OSError:
                pass
            raise

        try:
            _write_locked_source(handle, replacement, "%s replacement" % label)
            after_opened = os.fstat(handle.fileno())
            after_named = path.stat()
            handle.seek(0)
            after = handle.read(MAX_SOURCE_BYTES + 1)
            if (
                path.is_symlink()
                or after_opened.st_dev != after_named.st_dev
                or after_opened.st_ino != after_named.st_ino
                or after != replacement
            ):
                raise ShaderOrganizationError(
                    "%s changed concurrently during compare-and-swap" % label
                )
            _remove_source_transaction_evidence(journal, backup)
        except Exception as write_error:
            try:
                _write_locked_source(handle, expected, "%s recovery" % label)
                handle.seek(0)
                restored = handle.read(MAX_SOURCE_BYTES + 1)
                if restored != expected:
                    raise ShaderOrganizationError(
                        "%s could not be restored from its in-memory backup" % label
                    )
                _remove_source_transaction_evidence(journal, backup)
            except Exception as recovery_error:
                # Recovery evidence remains, so WorkspaceSourceTransactionLease
                # deliberately preserves crash residue and every managed start
                # fails closed until an operator inspects/restores the backup.
                raise ShaderOrganizationError(
                    "%s CAS failed (%s); recovery evidence was retained (%s)"
                    % (label, write_error, recovery_error)
                ) from write_error
            raise
        return True
    finally:
        if locked:
            try:
                handle.seek(0)
                if windows_lock:
                    import msvcrt

                    msvcrt.locking(
                        handle.fileno(), msvcrt.LK_UNLCK, MAX_SOURCE_BYTES
                    )
                else:  # pragma: no cover - see acquisition branch above.
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _read_session_file(session_dir: Path, description: str) -> dict:
    """Load session.json, naming a begin that died at its baseline gate."""

    session_path = session_dir / "session.json"
    failure_path = session_dir / "failure.json"
    if not session_path.exists() and failure_path.is_file():
        try:
            failure = read_json_object(failure_path, MAX_CONTEXT_BYTES, "organization failure")
        except ValueError:
            failure = {}
        raise ShaderOrganizationError(
            "organization session %s never opened: begin failed at %s; repair the "
            "workspace source (repair_shader_workspace_source_abi) and call "
            "begin_shader_workspace_organization again for a new organization_id"
            % (session_dir.name, failure.get("phase") or "its baseline gate")
        )
    return read_json_object(session_path, MAX_CONTEXT_BYTES, description)


def _validate_source_directives(text: str, label: str, source_path: Path | None = None) -> None:
    if re.search(r"(?im)^[ \t]*#[ \t]*line\b", text):
        raise ShaderOrganizationError(
            "%s must not contain #line" % label
        )
    if re.search(r"(?im)^[ \t]*#[ \t]*include\b", text):
        if source_path is None:
            raise ShaderOrganizationError("%s include graph needs its working source path" % label)
        from .source_documents import capture_source_unit
        try:
            capture_source_unit(source_path, source_override=text.encode("utf-8"))
        except (OSError, ValueError) as error:
            raise ShaderOrganizationError("%s include graph is unavailable: %s" % (label, error)) from error


class ShaderOrganizationManager:
    """Own bounded proposal files and the compile/ABI/code identity gate."""

    def __init__(
        self,
        service_factory: Callable[[], Any],
        validate_candidate: Callable[[str, int, str], Mapping[str, Any]],
        compare_candidates: Callable[
            [str, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]
        ] | None = None,
        cbuffer_alias_identity: Callable[[str], Mapping[str, Any]] | None = None,
        *,
        completed_reviews: CompletedShaderReviews | None = None,
    ) -> None:
        if not callable(service_factory) or not callable(validate_candidate):
            raise TypeError("organization callbacks must be callable")
        if compare_candidates is not None and not callable(compare_candidates):
            raise TypeError("organization comparison callback must be callable")
        if cbuffer_alias_identity is not None and not callable(cbuffer_alias_identity):
            raise TypeError("organization CBuffer identity callback must be callable")
        self._service_factory = service_factory
        self._validate_candidate = validate_candidate
        self._compare_candidates = compare_candidates
        self._cbuffer_alias_identity = cbuffer_alias_identity
        self._guard = threading.RLock()
        # Session files live below an AI-editable tree. Seal every fixed field
        # with a process-owned key so proposal edits cannot rewrite baseline
        # code/hash/identity authority. A restart admits an unfinished session
        # only through its separate exact Host-owned draft checkpoint.
        self._session_hmac_key = secrets.token_bytes(32)
        # Completed reviews are source-asset provenance, not process sessions.
        # Their exact accepted snapshot survives a Host restart in a local
        # store that is outside the managed proposal tools' write surface.
        self._completed_reviews = completed_reviews or CompletedShaderReviews()
        self._checkpoints = OrganizationCheckpoints(
            self._completed_reviews.path.with_name("organization-checkpoints.sqlite3"))
        # Mutation intent/backups must not live in the AI-editable workspace.
        # Keeping them beside the Host-owned checkpoint database lets a new
        # process authenticate and finish an interrupted multi-file commit.
        self._mutation_store = self._completed_reviews.path.with_name(
            "organization-mutation-transactions"
        )
        # Exact-source reports are reused by list/note and repeated reads.
        # A changed proposal has a different hash and is always analyzed anew.
        self._structure_report_cache: OrderedDict[
            tuple[str, str], dict[str, Any]
        ] = OrderedDict()

    def _cached_structure_report(
        self, source: str, entry_point: str
    ) -> tuple[dict[str, Any], bool]:
        from .restructure import structure_report

        key = (_sha256(source.encode("utf-8")), entry_point)
        cached = self._structure_report_cache.get(key)
        if cached is not None:
            self._structure_report_cache.move_to_end(key)
            return copy.deepcopy(cached), True
        report = structure_report(source, entry_point=entry_point)
        self._structure_report_cache[key] = copy.deepcopy(report)
        self._structure_report_cache.move_to_end(key)
        while len(self._structure_report_cache) > 4:
            self._structure_report_cache.popitem(last=False)
        return report, False

    def _remember_mutation_session(self, session_dir: Path) -> None:
        session = _read_session_file(session_dir, "organization mutation checkpoint")
        self._checkpoints.remember(session)

    def _recover_pending_mutation(
        self, root: Path, organization_id: str, session_dir: Path
    ) -> None:
        recover_workspace_mutation(
            store_root=self._mutation_store,
            workspace_root=root,
            organization_id=organization_id,
            checkpoint_after_apply=lambda: self._remember_mutation_session(
                session_dir
            ),
            checkpoint_after_rollback=lambda: self._remember_mutation_session(
                session_dir
            ),
        )

    def _session_hmac(self, session: Mapping[str, Any]) -> str:
        unsigned = dict(session)
        unsigned.pop("sessionIntegrityHmacSha256", None)
        return hmac.new(
            self._session_hmac_key,
            _canonical_json(unsigned),
            hashlib.sha256,
        ).hexdigest()

    def _seal_session(self, session: Mapping[str, Any]) -> dict[str, Any]:
        sealed = dict(session)
        sealed["sessionIntegrityHmacSha256"] = self._session_hmac(sealed)
        return sealed

    @staticmethod
    def _mutation_identity(
        operation_id: str = "", request_sha256: str = ""
    ) -> tuple[str, str] | None:
        operation = str(operation_id or "").strip()
        request_sha = str(request_sha256 or "").strip().lower()
        if not operation and not request_sha:
            return None
        if (
            _MUTATION_OPERATION_ID.fullmatch(operation) is None
            or _SHA256.fullmatch(request_sha) is None
        ):
            raise ShaderOrganizationError(
                "durable mutation requires its valid operation ID and request SHA-256"
            )
        return operation, request_sha

    @staticmethod
    def _mutation_request_hashes(
        request_sha256: str, compatible_request_sha256: Sequence[str]
    ) -> frozenset[str]:
        request_sha = str(request_sha256 or "").strip().lower()
        compatible = tuple(compatible_request_sha256 or ())
        if len(compatible) > 8:
            raise ShaderOrganizationError(
                "durable mutation has too many compatible request digests"
            )
        accepted = {request_sha}
        for value in compatible:
            candidate = str(value or "").strip().lower()
            if _SHA256.fullmatch(candidate) is None:
                raise ShaderOrganizationError(
                    "compatible durable mutation digest is not a SHA-256"
                )
            accepted.add(candidate)
        return frozenset(accepted)

    def _durable_mutation_replay(
        self,
        session: Mapping[str, Any],
        *,
        tool: str,
        operation_id: str = "",
        request_sha256: str = "",
        compatible_request_sha256: Sequence[str] = (),
    ) -> dict[str, Any] | None:
        identity = self._mutation_identity(operation_id, request_sha256)
        if identity is None:
            return None
        operation, request_sha = identity
        accepted_request_hashes = self._mutation_request_hashes(
            request_sha, compatible_request_sha256
        )
        receipts = session.get("mutationReceipts")
        receipts = receipts if isinstance(receipts, Mapping) else {}
        receipt = receipts.get(operation)
        if receipt is None:
            return None
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("tool") != tool
            or receipt.get("requestSha256") not in accepted_request_hashes
            or receipt.get("committed") is not True
        ):
            raise ShaderOrganizationError(
                "operation_id is already bound to a different durable mutation"
            )
        last = session.get("lastMutationResult")
        result = None
        if (
            isinstance(last, Mapping)
            and last.get("operationId") == operation
            and last.get("requestSha256") in accepted_request_hashes
            and isinstance(last.get("result"), Mapping)
        ):
            result = copy.deepcopy(dict(last["result"]))
        if result is None:
            summary = receipt.get("resultSummary")
            summary = dict(summary) if isinstance(summary, Mapping) else {}
            result = {
                "schema": "replaylab-durable-shader-mutation-replay/v1",
                "schemaVersion": 1,
                "originalResultSchema": summary.get("schema"),
                "workspacePath": summary.get("workspacePath"),
                "organizationId": summary.get("organizationId"),
                "operation": summary.get("operation"),
                "accepted": summary.get("accepted", True),
                "applied": summary.get("applied", False),
                "proposalSourcePath": summary.get("proposalSourcePath"),
                "proposalSourceSha256": summary.get("proposalSourceSha256"),
                "committed": True,
            }
        result["idempotentReplay"] = True
        result["durableMutation"] = {
            "operationId": operation,
            "requestSha256": receipt.get("requestSha256"),
            "tool": tool,
            "committed": True,
            "committedAt": receipt.get("committedAt"),
        }
        return result

    def _record_durable_mutation(
        self,
        session: Mapping[str, Any],
        *,
        tool: str,
        operation_id: str = "",
        request_sha256: str = "",
        compatible_request_sha256: Sequence[str] = (),
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        identity = self._mutation_identity(operation_id, request_sha256)
        if identity is None:
            return dict(session)
        operation, request_sha = identity
        accepted_request_hashes = self._mutation_request_hashes(
            request_sha, compatible_request_sha256
        )
        updated = dict(session)
        receipts = updated.get("mutationReceipts")
        receipts = copy.deepcopy(dict(receipts)) if isinstance(receipts, Mapping) else {}
        existing = receipts.get(operation)
        if existing is not None:
            if (
                not isinstance(existing, Mapping)
                or existing.get("tool") != tool
                or existing.get("requestSha256") not in accepted_request_hashes
            ):
                raise ShaderOrganizationError(
                    "operation_id is already bound to a different durable mutation"
                )
            return updated
        if len(receipts) >= _MAX_DURABLE_MUTATION_RECEIPTS:
            raise ShaderOrganizationError(
                "organization durable mutation ledger is full; start a new organization session"
            )
        result_value = copy.deepcopy(dict(result))
        summary = {
            key: result_value.get(key)
            for key in (
                "schema", "workspacePath", "organizationId", "operation",
                "accepted", "applied", "proposalSourcePath",
                "proposalSourceSha256",
            )
            if key in result_value
        }
        receipt = {
            "schema": "replaylab-durable-shader-mutation-receipt/v1",
            "schemaVersion": 1,
            "operationId": operation,
            "requestSha256": request_sha,
            "tool": tool,
            "committed": True,
            "committedAt": _utc_now(),
            "resultSha256": _sha256(_canonical_json(result_value)),
            "resultSummary": summary,
        }
        receipts[operation] = receipt
        updated["mutationReceipts"] = receipts
        updated["lastMutationResult"] = {
            "operationId": operation,
            "requestSha256": request_sha,
            "result": result_value,
        }
        # Keep exact replay for the latest operation when it fits. The compact
        # committed receipt always remains, so idempotency never depends on
        # retaining a large duplicate result.
        if len(_canonical_json(updated)) > MAX_CONTEXT_BYTES - 4096:
            updated["lastMutationResult"] = {
                "operationId": operation,
                "requestSha256": request_sha,
                "result": summary,
            }
        return updated


    @staticmethod
    def _canonical_cbuffer_alias_rows(
        identity: Mapping[str, Any],
        rows: Sequence[Mapping[str, Any]],
        block_alias: str = "",
        *,
        allow_placeholder_layout: bool = False,
        require_row_alignment: bool = True,
        canonicalize_to_reflected_leaf: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
            raise ShaderOrganizationError("CBuffer alias authorization requires rows")
        if len(rows) > 512:
            raise ShaderOrganizationError("CBuffer alias rows exceed the 512-row bound")
        blocks = [
            item for item in (identity.get("blocks") or [])
            if isinstance(item, Mapping)
        ]
        normalized: list[dict[str, Any]] = []
        selectors: set[str] = set()
        for raw in rows:
            if not isinstance(raw, Mapping):
                raise ShaderOrganizationError("each CBuffer alias row must be an object")
            bind, space, offset = raw.get("bind"), raw.get("space") or 0, raw.get("byteOffset")
            alias = str(raw.get("alias") or "").strip()
            if (
                type(bind) is not int or bind < 0
                or type(space) is not int or space < 0
                or type(offset) is not int or offset < 0
                or (require_row_alignment and offset % 16)
                or not cbuffer_aliases.is_valid_alias(alias)
            ):
                raise ShaderOrganizationError(
                    "CBuffer alias rows require non-negative bind/space, %s and a valid alias"
                    % (
                        "a 16-byte row offset"
                        if require_row_alignment
                        else "a non-negative byte offset"
                    )
                )
            matches = [
                block for block in blocks
                if block.get("bind") == bind and int(block.get("space") or 0) == space
            ]
            if len(matches) != 1:
                raise ShaderOrganizationError(
                    "CBuffer alias row has no unique current reflected block"
                )
            block = matches[0]
            members = [
                {"name": member.get("name"), "offset": member.get("offset")}
                for member in (block.get("members") or [])
                if isinstance(member, Mapping)
            ]
            if (
                not allow_placeholder_layout
                and cbuffer_aliases.has_placeholder_members(members)
            ):
                raise ShaderOrganizationError(
                    "AI CBuffer aliases require a named reflected layout"
                )
            layout_sha = cbuffer_aliases.layout_sha256(
                str(block.get("block") or ""), members
            )
            if _SHA256.fullmatch(layout_sha) is None:
                raise ShaderOrganizationError("CBuffer reflected layout has no stable layout hash")
            leaf = next(
                (
                    item for item in (block.get("leaves") or [])
                    if isinstance(item, Mapping)
                    and type(item.get("offset")) is int
                    and type(item.get("byteSize")) is int
                    and item["offset"] <= offset < item["offset"] + max(1, item["byteSize"])
                    and str(item.get("path") or "")
                ),
                None,
            )
            if leaf is None:
                raise ShaderOrganizationError(
                    "CBuffer alias row has no current reflected leaf covering its offset"
                )
            reflected_offset = int(leaf["offset"])
            published_offset = (
                reflected_offset if canonicalize_to_reflected_leaf else offset
            )
            selector = "cbuffer-row:s%d:b%d:o%d" % (
                space,
                bind,
                published_offset,
            )
            if selector in selectors:
                raise ShaderOrganizationError("duplicate CBuffer alias selector: " + selector)
            selectors.add(selector)
            normalized.append({
                "selector": selector,
                "bind": bind,
                "space": space,
                "byteOffset": published_offset,
                **(
                    {"requestedByteOffset": offset}
                    if published_offset != offset
                    else {}
                ),
                "alias": alias,
                "block": str(block.get("block") or ""),
                "reflectedPath": str(leaf["path"]),
                "reflectedLeafOffset": int(leaf["offset"]),
                "layoutSha256": layout_sha,
            })
        block_record: dict[str, Any] | None = None
        alias_text = str(block_alias or "").strip()
        if alias_text:
            if not cbuffer_aliases.is_valid_alias(alias_text):
                raise ShaderOrganizationError("CBuffer block alias is invalid")
            layouts = {
                (item["bind"], item["space"], item["block"], item["layoutSha256"])
                for item in normalized
            }
            if len(layouts) != 1:
                raise ShaderOrganizationError(
                    "one block_alias cannot authorize rows from several CBuffer layouts"
                )
            bind, space, block, layout_sha = next(iter(layouts))
            block_record = {
                "selector": "cbuffer-block:s%d:b%d" % (space, bind),
                "bind": bind,
                "space": space,
                "block": block,
                "layoutSha256": layout_sha,
                "alias": alias_text,
            }
        return normalized, block_record

    @staticmethod
    def normalize_cbuffer_alias_rows(
        identity: Mapping[str, Any],
        rows: Sequence[Mapping[str, Any]],
        block_alias: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Bind Agent-chosen labels directly to the current physical CBuffer.

        This is deliberately not an authorization operation.  The Agent owns the
        semantic choice; the Host only resolves each requested byte into the one
        current reflected leaf and rejects ambiguous or out-of-range targets.
        Placeholder ``unknown[N]`` layouts are valid physical layouts here.  Their
        AI aliases are scoped to the original Shader identity by the publication
        store/panel instead of being rejected as a workflow policy.
        """

        return ShaderOrganizationManager._canonical_cbuffer_alias_rows(
            identity,
            rows,
            block_alias,
            allow_placeholder_layout=True,
            require_row_alignment=False,
            canonicalize_to_reflected_leaf=True,
        )

    @staticmethod
    def _canonical_resource_alias_rows(
        rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Normalize exact capture resource-name targets before publication.

        ``assetPath`` and ``hostEvidenceKind`` describe how the Agent chose a
        label. They are useful provenance, but they are not Host-verified and
        therefore cannot authorize (or veto) a display-name write. The public
        endpoint separately proves the current EID/resource/register tuple.
        """

        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
            raise ShaderOrganizationError("resource alias publication requires rows")
        if len(rows) > 256:
            raise ShaderOrganizationError("resource alias rows exceed the 256-row bound")
        normalized: list[dict[str, Any]] = []
        targets: set[tuple[int, int, int]] = set()
        for raw in rows:
            if not isinstance(raw, Mapping):
                raise ShaderOrganizationError("each resource alias row must be an object")
            resource_id = raw.get("resourceId")
            bind = raw.get("bindNumber", raw.get("bind"))
            space = raw.get("registerSpace", raw.get("space", 0))
            alias = str(raw.get("alias") or "").strip()
            selector = str(raw.get("selector") or "").strip()
            asset_path = str(raw.get("assetPath") or raw.get("asset") or "").strip()
            evidence_kind = str(raw.get("hostEvidenceKind") or "").strip().lower()
            if (
                type(resource_id) is not int or resource_id <= 0
                or type(bind) is not int or bind < 0
                or type(space) is not int or space < 0
                or re.fullmatch(r"[A-Za-z_]\w*", alias) is None
                or len(alias) > 128
                or len(selector) > 4096 or "\x00" in selector
                or len(asset_path) > 4096 or "\x00" in asset_path
                or len(evidence_kind) > 128 or "\x00" in evidence_kind
            ):
                raise ShaderOrganizationError(
                    "resource alias rows require a positive resourceId, non-negative "
                    "bind/space, a safe identifier alias, and bounded optional provenance"
                )
            target = (resource_id, bind, space)
            if target in targets:
                raise ShaderOrganizationError("duplicate resource alias physical target")
            targets.add(target)
            normalized.append({
                "selector": selector,
                "resourceId": resource_id,
                "bindNumber": bind,
                "registerSpace": space,
                "alias": alias,
                "assetPath": asset_path,
                "hostEvidenceKind": evidence_kind,
            })
        return normalized

    def normalize_resource_alias_rows(
        self, rows: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Validate AI-selected texture-name rows without granting semantics.

        The Agent owns the content-match interpretation.  This fixed-program
        helper checks only the bounded row shape, safe display identifier and
        bounded optional provenance;
        the public publication endpoint separately proves that every physical
        resource/register tuple exists on the requested current Shader.
        """

        return self._canonical_resource_alias_rows(rows)

    @staticmethod
    def _binding_target(
        binding: Mapping[str, Any], renames: Mapping[str, str]
    ) -> tuple[str, str]:
        """Return (target, selector-used) for one baseline declaration."""

        selector = str(binding.get("renameSelector") or "")
        if selector in renames:
            return str(renames[selector]), selector
        # Legacy/plain keys are detected so generated declarations cannot evade
        # source-bound provenance merely because a spelling happens to be unique.
        name = str(binding.get("name") or "")
        owner = str(binding.get("owner") or "")
        legacy = owner + "." + name if owner else name
        if legacy in renames:
            return str(renames[legacy]), legacy
        if name in renames:
            return str(renames[name]), name
        return "", ""

    @staticmethod
    def _rename_provenance_binding(receipt: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: receipt.get(key)
            for key in (
                "basis", "selector", "originalName", "targetName", "kind", "owner",
                "register", "byteOffset", "neutralName", "authorizationId",
                "workspaceId", "captureId", "captureSessionId", "eventId", "stage",
                "entryPoint", "shaderResourceId", "shaderRawSha256", "sourceSha256",
                "assetResolutionSha256", "assetEvidenceSha256",
                "layoutSha256",
            )
        }

    def _build_rename_provenance(
        self,
        baseline_text: str,
        renames: Mapping[str, str],
        existing: Mapping[str, Any],
        _user_aliases: Mapping[str, Any],
    ) -> dict[str, Any]:
        from .quality import declared_binding_inventory, is_deterministic_neutral_name

        provenance: dict[str, Any] = {}
        for key, value in existing.items():
            if not isinstance(value, Mapping):
                continue
            receipt = copy.deepcopy(dict(value))
            receipt.pop("hostVerifiedForReview", None)
            provenance[str(key)] = receipt
        for binding in declared_binding_inventory(baseline_text):
            if binding.get("generatedPhysicalName") is not True:
                continue
            selector = str(binding.get("renameSelector") or "")
            target, used_selector = self._binding_target(binding, renames)
            if not target:
                continue
            if used_selector != selector:
                raise ShaderOrganizationError(
                    "generated declaration %s must be renamed through its exact source-bound selector"
                    % selector
                )
            base_receipt = {
                "selector": selector,
                "originalName": str(binding.get("name") or ""),
                "targetName": target,
                "kind": str(binding.get("kind") or ""),
                "owner": str(binding.get("owner") or ""),
                "register": str(binding.get("register") or ""),
                "byteOffset": binding.get("byteOffset"),
                "neutralName": str(binding.get("neutralName") or ""),
            }
            if is_deterministic_neutral_name(binding, target):
                neutral_receipt = {**base_receipt, "basis": "deterministic-neutral"}
                neutral_receipt["provenanceBindingSha256"] = _sha256(
                    _canonical_json(self._rename_provenance_binding(neutral_receipt))
                )
                provenance[selector] = neutral_receipt
                continue
            # Semantic naming is an Agent decision. The Host's responsibility is
            # limited to proving that this exact selector exists in the current
            # baseline and that the token-level rename is safe. Asset/session
            # and panel-alias receipts are evidence the Agent may inspect, never
            # source-review permissions.
            semantic_receipt = {
                **base_receipt,
                "basis": "agent-directed-current-source",
            }
            semantic_receipt["provenanceBindingSha256"] = _sha256(
                _canonical_json(self._rename_provenance_binding(semantic_receipt))
            )
            provenance[selector] = semantic_receipt
        return provenance

    @classmethod
    def _downgrade_legacy_user_alias_rename_receipt(
        cls,
        selector: str,
        raw: Mapping[str, Any],
        session: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Keep an old source rename while removing panel-alias authority.

        Early organization sessions used an existing CBuffer panel alias as
        the source rename's review credential.  A panel alias is display state,
        not source authority.  Preserve its digest as historical evidence and
        rebind the exact sealed selector/target to the current-source receipt
        used by all new sessions.
        """

        receipt = copy.deepcopy(dict(raw))
        if receipt.get("basis") != "host-user-alias-receipt":
            return receipt
        expected = _sha256(_canonical_json(cls._rename_provenance_binding(receipt)))
        rename_map = session.get("renameMap")
        if (
            receipt.get("provenanceBindingSha256") != expected
            or receipt.get("selector") != selector
            or not isinstance(rename_map, Mapping)
            or rename_map.get(selector) != receipt.get("targetName")
        ):
            return receipt
        rebound = {
            key: copy.deepcopy(receipt.get(key))
            for key in (
                "selector", "originalName", "targetName", "kind", "owner",
                "register", "byteOffset", "neutralName", "layoutSha256",
            )
        }
        rebound.update({
            "basis": "agent-directed-current-source",
            "legacyEvidence": {
                "basis": "host-user-alias-receipt",
                "status": "preserved-nonauthoritative",
                "provenanceBindingSha256": receipt.get("provenanceBindingSha256"),
                "layoutSha256": receipt.get("layoutSha256"),
            },
        })
        rebound["provenanceBindingSha256"] = _sha256(
            _canonical_json(cls._rename_provenance_binding(rebound))
        )
        return rebound

    @classmethod
    def _downgrade_legacy_asset_rename_receipt(
        cls,
        selector: str,
        raw: Mapping[str, Any],
        session: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Turn a valid v2 asset ticket into non-authoritative rename provenance.

        Old drafts embedded workspace/capture/session/ResourceId observations in
        ``host-asset-receipt`` and later treated those volatile values as review
        permission. A session is already bound to the immutable original Shader
        bytes, and the rename map binds the selector to its chosen target. Preserve
        a digest of the old evidence for diagnostics, but authorize review exactly
        like a current Agent-directed rename.
        """

        receipt = copy.deepcopy(dict(raw))
        if receipt.get("basis") != "host-asset-receipt":
            return receipt
        expected = _sha256(_canonical_json(cls._rename_provenance_binding(receipt)))
        rename_map = session.get("renameMap")
        program = cls._session_program_identity(session, {})
        original_raw = str(program.get("originalRawSha256") or "").strip().lower()
        receipt_raw = str(receipt.get("shaderRawSha256") or "").strip().lower()
        receipt_source = str(receipt.get("sourceSha256") or "").strip().lower()
        baseline_source = str(session.get("baselineSourceSha256") or "").strip().lower()
        if (
            receipt.get("provenanceBindingSha256") != expected
            or receipt.get("selector") != selector
            or not isinstance(rename_map, Mapping)
            or rename_map.get(selector) != receipt.get("targetName")
            or _SHA256.fullmatch(original_raw) is None
            or receipt_raw != original_raw
            or (receipt_source and receipt_source != baseline_source)
        ):
            return receipt
        rebound = {
            key: copy.deepcopy(receipt.get(key))
            for key in (
                "selector", "originalName", "targetName", "kind", "owner",
                "register", "byteOffset", "neutralName", "layoutSha256",
            )
        }
        rebound.update({
            "basis": "agent-directed-current-source",
            "legacyEvidence": {
                "basis": "host-asset-receipt",
                "status": "preserved-nonauthoritative",
                "provenanceBindingSha256": receipt.get("provenanceBindingSha256"),
                "shaderRawSha256": receipt_raw,
                "assetEvidenceSha256": receipt.get("assetEvidenceSha256"),
                "assetResolutionSha256": receipt.get("assetResolutionSha256"),
            },
        })
        rebound["provenanceBindingSha256"] = _sha256(
            _canonical_json(cls._rename_provenance_binding(rebound))
        )
        return rebound

    def _verified_rename_provenance(
        self,
        root: Path,
        status: Mapping[str, Any],
        session: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate stored rename receipts against the sealed current session."""

        stored = session.get("renameProvenance")
        if not isinstance(stored, Mapping):
            return {}
        verified: dict[str, Any] = {}
        for selector, raw in stored.items():
            if not isinstance(raw, Mapping):
                continue
            receipt = self._downgrade_legacy_asset_rename_receipt(
                str(selector), raw, session
            )
            receipt = self._downgrade_legacy_user_alias_rename_receipt(
                str(selector), receipt, session
            )
            expected_provenance_sha = _sha256(
                _canonical_json(self._rename_provenance_binding(receipt))
            )
            if receipt.get("provenanceBindingSha256") != expected_provenance_sha:
                continue
            if receipt.get("basis") == "deterministic-neutral":
                receipt["hostVerifiedForReview"] = True
                verified[str(selector)] = receipt
                continue
            if (
                receipt.get("basis") == "agent-directed-current-source"
                and receipt.get("selector") == selector
                and isinstance(session.get("renameMap"), Mapping)
                and session["renameMap"].get(selector) == receipt.get("targetName")
            ):
                # The session seal, current raw-Shader identity and later
                # compile/ABI checks are the physical proof. No asset authority
                # is required for the Agent's semantic naming decision.
                receipt["hostVerifiedForReview"] = True
                verified[str(selector)] = receipt
                continue
        return verified

    @staticmethod
    def _session_program_identity(
        session: Mapping[str, Any], current: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Read a draft's physical program identity, including v2 compatibility.

        Early v2 sessions predate ``shaderProgramIdentity`` but captured the
        exact raw program in their CBuffer identity receipt.  Exact raw bytes
        imply the same physical ABI for a fixed stage/profile, so missing
        legacy profile/ABI fields may be completed from the current workspace;
        a legacy session without a raw-byte receipt is never adopted.
        """

        stored = session.get("shaderProgramIdentity")
        if isinstance(stored, Mapping):
            identity = copy.deepcopy(dict(stored))
        else:
            alias_identity = session.get("cbufferAliasIdentity")
            alias_identity = (
                alias_identity if isinstance(alias_identity, Mapping) else {}
            )
            raw = str(
                session.get("originalRawSha256")
                or alias_identity.get("rawSha256")
                or ""
            ).strip().lower()
            identity = {
                "schema": "replaylab-shader-program-identity/v1",
                "originalRawSha256": raw,
                "stage": str(session.get("stage") or "").strip().lower(),
                "targetProfile": copy.deepcopy(
                    session.get("targetProfile")
                    if isinstance(session.get("targetProfile"), Mapping)
                    else current.get("targetProfile", {})
                ),
                "targetProfileSha256": str(
                    session.get("targetProfileSha256")
                    or current.get("targetProfileSha256")
                    or ""
                ),
                "physicalAbiSha256": str(
                    session.get("physicalAbiSha256")
                    or current.get("physicalAbiSha256")
                    or ""
                ),
            }
        return identity

    @classmethod
    def _same_shader_program(
        cls, session: Mapping[str, Any], status: Mapping[str, Any]
    ) -> bool:
        current = _shader_program_identity(status)
        stored = cls._session_program_identity(session, current)
        raw = str(stored.get("originalRawSha256") or "").strip().lower()
        return bool(
            _SHA256.fullmatch(raw) is not None
            and raw == current["originalRawSha256"]
        )

    def _rebind_session_context(
        self,
        session: Mapping[str, Any],
        root: Path,
        status: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Attach a compatible draft to the current workspace observation.

        This is a routing refresh, not an authorization transfer.  The only
        durable compatibility decision was made by ``_same_shader_program``;
        volatile capture/EID/resource/workspace identities are updated so later
        receipts describe the current run instead of rejecting it.
        """

        if not self._same_shader_program(session, status):
            raise ShaderOrganizationError(
                "saved organization belongs to a different captured Shader program"
            )
        rebound = copy.deepcopy(dict(session))
        rebound["workspacePath"] = str(root)
        rebound["workspaceId"] = str(status.get("workspaceId") or "")
        rebound["captureId"] = str(
            (status.get("capture") or {}).get("captureId") or ""
        )
        rebound["stage"] = str(status.get("stage") or rebound.get("stage") or "")
        rebound["entryPoint"] = str(
            status.get("entryPoint") or rebound.get("entryPoint") or ""
        )
        rebound["shaderProgramIdentity"] = _shader_program_identity(status)
        rebound["originalRawSha256"] = rebound["shaderProgramIdentity"][
            "originalRawSha256"
        ]
        rebound["targetProfile"] = copy.deepcopy(
            rebound["shaderProgramIdentity"]["targetProfile"]
        )
        rebound["targetProfileSha256"] = rebound["shaderProgramIdentity"][
            "targetProfileSha256"
        ]
        rebound["physicalAbiSha256"] = rebound["shaderProgramIdentity"][
            "physicalAbiSha256"
        ]
        stored_provenance = rebound.get("renameProvenance")
        if isinstance(stored_provenance, Mapping):
            rebound["renameProvenance"] = {
                str(selector): self._downgrade_legacy_asset_rename_receipt(
                    str(selector), receipt, rebound
                )
                for selector, receipt in stored_provenance.items()
                if isinstance(receipt, Mapping)
            }
        rebound["reboundAt"] = _utc_now()
        return self._seal_session(rebound)

    def checkpoint(self, workspace_path: str, organization_id: str, *, status=None) -> None:
        """Persist a durable session ledger; never freeze draft files.

        Proposal/source documents, notes and the shared review counter remain
        ordinary working state.  Their current bytes are validated by the
        operation that consumes them instead of by an unrelated conversation,
        process or capture-session authority snapshot.
        """
        with self._guard:
            root = Path(workspace_path).resolve()
            session_dir = root / "ai" / "organization-sessions" / _hex(organization_id, "organization_id", 32)
            session = _read_session_file(session_dir, "organization checkpoint")
            status = status if status is not None else _draft_status(self._service_factory(), str(root))
            self._validate_session(session, root, organization_id, status)
            if session.get("state") == "completed":
                # Completed acceptance is content-bound. Do not rewrite it just
                # to record volatile routing context after its external review
                # receipt has been committed.
                self._checkpoints.remember(session)
                return
            rebound = self._rebind_session_context(session, root, status)
            atomic_write_json(session_dir / "session.json", rebound)
            self._checkpoints.remember(rebound)

    def save_proposal(self, workspace_path: str, organization_id: str) -> dict[str, Any]:
        """Persist the working proposal without touching canonical source or replay."""
        with self._guard:
            status = _draft_status(self._service_factory(), workspace_path)
            self.authorize_edit(workspace_path, organization_id, status=status)
            root = Path(workspace_path).resolve()
            session_dir = root / "ai" / "organization-sessions" / organization_id
            session = _read_session_file(session_dir, "organization proposal checkpoint")
            result = self._begin_result(root, session_dir, session, {})
            return {
                "schema": "replaylab-shader-organization-save/v1", "schemaVersion": 1,
                "ok": True, "state": "proposal-checkpoint", "workspacePath": str(root),
                "organizationId": organization_id, "sourceSha256": result["proposalSourceSha256"],
                "proposalSourceSha256": result["proposalSourceSha256"],
                "proposalSourcePath": result["proposalSourcePath"],
                "promoted": False, "published": False, "applied": False,
                "canonicalSourceUpdated": False, "draftPreserved": True,
                "wholeSourceValidated": False, "safeToStartLiveEdit": False,
                "validationDeferred": True,
            }

    def save_editor_source(self, workspace_path: str, source_workspace_id: str,
                           expected_raw_sha256: str, text: str, *,
                           commit_guard: Callable | None = None) -> dict[str, Any]:
        """Save an IDE edit through a resumable draft, independently of replay ownership.

        The existing draft gate keeps background watchers from compiling this save. Both
        an AI's newer proposal and an external canonical edit retain their own CAS guards.
        Frozen validation snapshots are never overwritten.
        """
        from .errors import WorkspaceWatchConflictError

        expected = _hex(expected_raw_sha256, "editor source SHA-256")
        if not isinstance(text, str) or not text.strip() or "\x00" in text:
            raise ShaderOrganizationError("editor source must be non-empty NUL-free text")
        replacement = text.encode("utf-8", errors="strict")
        if len(replacement) > MAX_SOURCE_BYTES:
            raise ShaderOrganizationError("editor source exceeds its fixed size limit")
        replacement_sha = _sha256(replacement)
        with self._guard, source_observation_transaction(workspace_path):
            status = _draft_status(self._service_factory(), workspace_path)
            if status.get("workspaceId") != source_workspace_id:
                raise ShaderOrganizationError("editor source workspace identity changed")
            root, source_path, unused_bytes, current_sha = self._workspace_source(status)
            # begin() resumes sessions and invalidates the publication gate. Check the
            # editor's baseline without those side effects, under the same writer locks.
            # A stale editor must not turn a successfully validated source back into a draft.
            existing = self.open_session(root, status, recover=False)
            observed_sha = current_sha
            if existing is not None:
                session_dir = root / "ai" / "organization-sessions" / existing["organizationId"]
                proposal_path = self._session_artifact(
                    root, existing, "proposalSourcePath", session_dir, "proposal source")
                observed_sha = _sha256(_regular_bytes(
                    proposal_path, MAX_SOURCE_BYTES, "proposal source"))
            if observed_sha not in (expected, replacement_sha):
                raise WorkspaceWatchConflictError(
                    "The draft changed since this editor read it; reload before saving.")
            # Wait for organization/source locks before briefly pinning project selection.
            # Switching projects while queued therefore rejects the save before begin().
            with commit_guard() if commit_guard is not None else nullcontext():
                if current_sha == replacement_sha and observed_sha == replacement_sha:
                    # A timed-out request may already be committed and subsequently
                    # validated. Confirm its bytes without reopening/invalidation.
                    return self._editor_source_save_result(
                        source_path, source_workspace_id, expected, replacement,
                        replacement_sha, (existing or {}).get("organizationId", ""))
                return self._save_editor_source_owned(
                    workspace_path, source_workspace_id, expected, replacement, replacement_sha)

    def _save_editor_source_owned(self, workspace_path, source_workspace_id,
                                  expected, replacement, replacement_sha):
        from .errors import WorkspaceWatchConflictError

        with self._guard:
            begun = self.begin(workspace_path, editable_source_only=True)
            organization_id = begun["organizationId"]
            proposal = Path(begun["proposalSourcePath"])
            proposal_sha = begun["proposalSourceSha256"]
            if proposal_sha not in (expected, replacement_sha):
                raise WorkspaceWatchConflictError(
                    "The draft changed since this editor read it; reload before saving.")
            if proposal_sha != replacement_sha:
                # The singleton manager lock is shared with every AI proposal writer.
                # Canonical publication below has the journaled OS-file compare-and-swap.
                atomic_write_bytes(proposal, replacement)
                self.checkpoint(workspace_path, organization_id)
            result = self.save_draft(
                workspace_path, organization_id,
                expected_reviewed_source_sha256=replacement_sha)
            source_path = Path(result["sourcePath"])
            return self._editor_source_save_result(
                source_path, source_workspace_id, expected, replacement, replacement_sha, organization_id)

    @staticmethod
    def _editor_source_save_result(source_path, source_workspace_id, expected,
                                   replacement, replacement_sha, organization_id):
        info = source_path.stat()
        return {"rawSha256": replacement_sha, "size": len(replacement),
                "modifiedNs": info.st_mtime_ns, "changed": replacement_sha != expected,
                "compileScheduled": False, "watchId": "", "sourceOnly": True,
                "sourcePath": str(source_path), "organizationId": organization_id,
                "sourceWorkspaceId": source_workspace_id}

    def save_draft(
        self,
        workspace_path: str,
        organization_id: str,
        *,
        generate_flow_graph: bool = False,
        expected_reviewed_source_sha256: str = "",
    ) -> dict[str, Any]:
        """Make the current proposal the editable workspace source without publishing it.

        A locked compare-and-swap writes the canonical source, followed by a
        recoverable transaction for the session and inactive gate. The captured
        baseline stays in the session for later inspection. This is an editor
        save: it neither compiles nor grants a replay replacement receipt.
        """

        with self._guard, source_observation_transaction(workspace_path):
            organization_id = _hex(organization_id, "organization_id", 32)
            service = self._service_factory()
            status = _draft_status(service, workspace_path)
            root, unused_path, unused_bytes, unused_sha = self._workspace_source(status)
            session_dir = root / "ai" / "organization-sessions" / organization_id
            _safe_dir(session_dir, root, "organization session directory", create=False)
            self._recover_pending_mutation(root, organization_id, session_dir)
            status = _draft_status(service, str(root))
            observed_root, source_path, current_bytes, current_sha = self._workspace_source(status)
            if observed_root != root:
                raise ShaderOrganizationError("organization workspace changed during draft save")
            session_path = session_dir / "session.json"
            original_session_bytes = _regular_bytes(
                session_path, MAX_CONTEXT_BYTES, "organization draft session"
            )
            session = _read_session_file(session_dir, "organization draft session")
            self._validate_session(session, root, organization_id, status)
            if session.get("state") not in OPEN_DRAFT_STATES:
                raise ShaderOrganizationError("organization source draft is not open")
            supplied_hmac = str(session.get("sessionIntegrityHmacSha256") or "")
            if not (
                _SHA256.fullmatch(supplied_hmac)
                and hmac.compare_digest(supplied_hmac, self._session_hmac(session))
            ) and not self._checkpoints.contains(session):
                raise ShaderOrganizationError("organization draft session is not authenticated")
            if str(source_path.relative_to(root)).replace("\\", "/") != session.get("sourcePath"):
                raise ShaderOrganizationError("organization draft source path changed")
            baseline_path = self._session_artifact(
                root, session, "baselineSourcePath", session_dir, "baseline source"
            )
            baseline_bytes = _regular_bytes(
                baseline_path, MAX_SOURCE_BYTES, "organization baseline source"
            )
            baseline_sha = _hex(session.get("baselineSourceSha256"), "baseline source SHA-256")
            if _sha256(baseline_bytes) != baseline_sha:
                raise ShaderOrganizationError("organization baseline source changed")
            proposal_path = self._session_artifact(
                root, session, "proposalSourcePath", session_dir, "proposal source"
            )
            proposal_text, proposal_bytes = _utf8(
                proposal_path, MAX_SOURCE_BYTES, "proposal source"
            )
            proposal_sha = _sha256(proposal_bytes)
            if expected_reviewed_source_sha256 and proposal_sha != expected_reviewed_source_sha256:
                raise ShaderOrganizationError("proposal changed after source self-review; review the current draft")
            expected_current_sha = _hex(
                session.get("syncedDraftSourceSha256") or baseline_sha,
                "last saved draft source SHA-256",
            )
            if current_sha != expected_current_sha and current_sha != proposal_sha:
                raise ShaderOrganizationError(
                    "workspace source changed since this draft was last saved; read the current source before overwriting it"
                )
            saved_at = _utc_now()
            updated = dict(session)
            updated["syncedDraftSourceSha256"] = proposal_sha
            updated["savedDraftAt"] = saved_at
            updated = self._seal_session(updated)
            updated_bytes = (
                json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            if len(updated_bytes) > MAX_CONTEXT_BYTES:
                raise ShaderOrganizationError("organization draft session exceeds its size limit")
            gate = self._inactive_gate(
                organization_id=organization_id,
                status=status,
                decision="editable-draft",
                reason="current editable source has not been compiled or ABI-validated",
                source_sha256=proposal_sha,
            )
            gate.update({
                "baselineSourceSha256": baseline_sha,
                "proposalSourceSha256": proposal_sha,
                "editableDraftSourcePath": str(source_path),
                "savedDraftAt": saved_at,
            })
            gate_path = root / "ai" / "organize-code-gate.json"
            prior_gate_bytes = (
                _regular_bytes(gate_path, MAX_CONTEXT_BYTES, "organization draft gate")
                if gate_path.exists() else None
            )
            gate_bytes = (
                json.dumps(gate, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            targets = {session_path: updated_bytes, gate_path: gate_bytes}
            expected_before: dict[Path, bytes | None] = {
                session_path: original_session_bytes,
                gate_path: prior_gate_bytes,
            }
            source_swapped = False
            if proposal_bytes != current_bytes:
                # The Host-owned mutation ledger serializes managed writers,
                # but its per-target atomic replacement cannot compare and
                # write an editor-owned source under one OS file lock. Keep the
                # canonical source out of that transaction and use its own
                # journaled, locked CAS first. The metadata transaction below
                # remains inactive until the exact proposal is on disk.
                if not _source_compare_and_swap(
                    source_path, current_bytes, proposal_bytes, "workspace shader source"
                ):
                    raise ShaderOrganizationError(
                        "workspace source changed during draft save; read the current source before overwriting it"
                    )
                source_swapped = True
            try:
                apply_workspace_mutation(
                    store_root=self._mutation_store,
                    workspace_root=root,
                    organization_id=organization_id,
                    tool="save_shader_workspace_organization_draft",
                    operation_id=uuid.uuid4().hex,
                    request_sha256=proposal_sha,
                    targets=targets,
                    expected_before=expected_before,
                    checkpoint_after_apply=lambda: self._remember_mutation_session(session_dir),
                    checkpoint_after_rollback=lambda: self._remember_mutation_session(session_dir),
                )
            except Exception as mutation_error:
                # A cleanup error can be raised after the metadata commit. A
                # partially written ledger must be resolved before deciding
                # whether reversing the source CAS is safe.
                try:
                    self._recover_pending_mutation(root, organization_id, session_dir)
                    committed = (
                        _regular_bytes(session_path, MAX_CONTEXT_BYTES, "organization draft session")
                        == updated_bytes
                        and _regular_bytes(gate_path, MAX_CONTEXT_BYTES, "organization draft gate")
                        == gate_bytes
                    )
                    unchanged = (
                        _regular_bytes(session_path, MAX_CONTEXT_BYTES, "organization draft session")
                        == original_session_bytes
                        and (
                            _regular_bytes(gate_path, MAX_CONTEXT_BYTES, "organization draft gate")
                            if gate_path.exists() else None
                        ) == prior_gate_bytes
                    )
                except Exception as recovery_error:
                    raise ShaderOrganizationError(
                        "draft source was saved, but its metadata needs recovery; current source was preserved: %s"
                        % recovery_error
                    ) from mutation_error
                if not committed:
                    if not unchanged:
                        raise ShaderOrganizationError(
                            "draft metadata changed during save; current source was preserved"
                        ) from mutation_error
                    if source_swapped and not _source_compare_and_swap(
                        source_path, proposal_bytes, current_bytes,
                        "workspace shader source rollback",
                    ):
                        raise ShaderOrganizationError(
                            "draft metadata save failed after an external source edit; current source was preserved"
                        ) from mutation_error
                    raise
            if _regular_bytes(source_path, MAX_SOURCE_BYTES, "workspace shader source") != proposal_bytes:
                raise ShaderOrganizationError(
                    "workspace source changed after draft save; current source was preserved"
                )
            flow_graph_result: dict[str, Any] = {"generated": False}
            if generate_flow_graph:
                try:
                    graph_path = root / "ai" / "flow-graph.json"
                    graph = build_shader_flow_graph(
                        proposal_text,
                        workspace_id=str(updated["workspaceId"]),
                        source_path=str(updated["sourcePath"]),
                        source_sha256=proposal_sha,
                        source_revision=status["revision"],
                        stage=str(updated["stage"]),
                        entry_point=str(updated["entryPoint"]),
                        generated_at=_utc_now(),
                        origin="editable-draft",
                    )
                    atomic_write_json(graph_path, graph)
                    flow_graph_result = {
                        "generated": True,
                        "graphId": graph["graphId"],
                        "artifactPath": str(graph_path),
                        "nodeCount": len(graph["nodes"]),
                        "edgeCount": len(graph["edges"]),
                    }
                except Exception as graph_error:
                    flow_graph_result = {
                        "generated": False,
                        "diagnostic": str(graph_error)[:800],
                    }
            return {
                "schema": "replaylab-shader-organization-save/v1",
                "schemaVersion": 1,
                "organizationId": organization_id,
                "ok": True,
                "promoted": True,
                "published": False,
                "applied": False,
                "draftPreserved": True,
                "workspacePath": str(root),
                "sourcePath": str(source_path),
                "sourceFormat": str(updated.get("sourceFormat") or "hlsl"),
                "baselineSourceSha256": baseline_sha,
                "sourceSha256": proposal_sha,
                "proposalSourceSha256": proposal_sha,
                "wholeSourceValidated": False,
                "safeToStartLiveEdit": False,
                "validationDeferred": True,
                "state": "editable-draft",
                "gatePath": str(gate_path),
                "flowGraph": flow_graph_result,
            }

    def authorize_edit(self, workspace_path: str, organization_id: str, *, status=None) -> None:
        """Authenticate the on-disk organization session before a draft write.

        The Agent owns workflow decisions. Host verifies only that this is the
        same captured Shader program and that the canonical source did not
        change concurrently. Draft documents are mutable working state and are
        deliberately not tied to a conversation, process, EID or capture path.
        """
        with self._guard:
            status = status if status is not None else _draft_status(self._service_factory(), workspace_path)
            root, unused_path, unused_bytes, current_sha = self._workspace_source(status)
            session_dir = root / "ai" / "organization-sessions" / _hex(organization_id, "organization_id", 32)
            session = _read_session_file(session_dir, "organization edit authorization")
            self._validate_session(session, root, organization_id, status)
            if session.get("state") not in OPEN_DRAFT_STATES:
                raise ShaderOrganizationError(
                    "organization session is not awaiting proposal edits"
                )
            expected_source_sha = (
                session.get("syncedDraftSourceSha256")
                or session.get("baselineSourceSha256")
            )
            if expected_source_sha != current_sha:
                raise ShaderOrganizationError(
                    "workspace source changed concurrently; current source must be inspected before overwriting it"
                )
            # Rebind the compatible program to the current observation. The
            # operation's own proposal/session/source CAS remains the race gate.
            rebound = self._rebind_session_context(session, root, status)
            atomic_write_json(session_dir / "session.json", rebound)
            self._checkpoints.remember(rebound)

    @staticmethod
    def _candidate_binding(
        source_sha256: str,
        compile_result: Mapping[str, Any] | None,
        validation: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if compile_result is None or validation is None:
            return {
                "complete": False,
                "sourceSha256": source_sha256,
                "revision": None,
                "candidateRawSha256": "",
                "codeSha256": "",
                "abiRequestId": "",
                "abiReportSha256": "",
            }
        return {
            "complete": True,
            "sourceSha256": _hex(source_sha256, "gate source SHA-256"),
            **({"sourceDependenciesDigest": _hex(
                compile_result["sourceDependencies"]["digest"], "gate include dependency digest")}
               if isinstance(compile_result.get("sourceDependencies"), Mapping)
               and compile_result["sourceDependencies"].get("digest") else {}),
            "revision": _positive_int(
                compile_result.get("revision"), "gate candidate revision"
            ),
            "candidateRawSha256": _hex(
                compile_result.get("rawSha256"), "gate candidate raw SHA-256"
            ),
            "codeSha256": _hex(
                compile_result.get("codeSha256"), "gate code SHA-256"
            ),
            "abiRequestId": _hex(
                validation.get("requestId"), "gate ABI request ID"
            ),
            "abiReportSha256": _hex(
                validation.get("abiReportSha256"), "gate ABI report SHA-256"
            ),
        }

    @staticmethod
    def _inactive_gate(
        *,
        organization_id: str,
        status: Mapping[str, Any],
        decision: str,
        reason: str,
        source_sha256: str,
        compile_result: Mapping[str, Any] | None = None,
        validation: Mapping[str, Any] | None = None,
        source_restored: bool = False,
    ) -> dict[str, Any]:
        binding = ShaderOrganizationManager._candidate_binding(
            source_sha256, compile_result, validation
        )
        return {
            "schema": ORGANIZATION_GATE_SCHEMA,
            "schemaVersion": 1,
            "policyVersion": ORGANIZATION_POLICY_VERSION,
            "organizationId": organization_id,
            "reviewedAt": _utc_now(),
            "requestedTransformation": "organize-only",
            "workspaceId": str(status.get("workspaceId", "")),
            "captureId": str(status.get("capture", {}).get("captureId", "")),
            "stage": str(status.get("stage", "")),
            "entryPoint": str(status.get("entryPoint", "")),
            "decision": decision,
            "reason": str(reason),
            "codeSha256Equal": False,
            "organizedSourcePromoted": False,
            "sourceRestored": bool(source_restored),
            "semanticEquivalenceVerified": False,
            "replayApplied": False,
            "safeToStartLiveEdit": False,
            "finalBinding": binding,
        }

    @staticmethod
    def _publish_root_gate(root: Path, gate: Mapping[str, Any]) -> None:
        atomic_write_json(root / "ai" / "organize-code-gate.json", dict(gate))

    def _require_final_binding(
        self,
        service: Any,
        root: Path,
        session: Mapping[str, Any],
        binding: Mapping[str, Any],
    ) -> None:
        status = service.status(str(root))
        observed_root, unused_path, unused_bytes, source_sha = self._workspace_source(
            status
        )
        latest = status.get("latestCompiled") if isinstance(status, Mapping) else None
        abi_gate = latest.get("abiGate") if isinstance(latest, Mapping) else None
        if (
            observed_root != root
            or not self._same_shader_program(session, status)
            or source_sha != binding.get("sourceSha256")
            or status.get("sourceDependenciesCurrent") is False
            or status.get("revision") != binding.get("revision")
            or not isinstance(latest, Mapping)
            or latest.get("sourceSha256") != binding.get("sourceSha256")
            or latest.get("sourceDependencies", {}).get("digest", "") != binding.get("sourceDependenciesDigest", "")
            or latest.get("rawSha256") != binding.get("candidateRawSha256")
            or latest.get("codeSha256") != binding.get("codeSha256")
            or latest.get("abiCompatible") is not True
            or not isinstance(abi_gate, Mapping)
            or abi_gate.get("status") != "compatible"
            or abi_gate.get("requestId") != binding.get("abiRequestId")
            or abi_gate.get("reportSha256") != binding.get("abiReportSha256")
        ):
            raise ShaderOrganizationError(
                "organization final source/candidate/ABI binding changed"
            )

    def open_session(self, root: Path, status: Mapping[str, Any] | None = None,
                     *, recover: bool = True) -> dict[str, Any] | None:
        """The newest organization session still awaiting review, or None.

        Sessions carry the rename map, structural operations and function notes,
        so a task that was interrupted (process closed,
        connection lost, new conversation) resumes from them instead of
        starting over. Capture/session/workspace routing changes are rebound
        automatically when the original captured Shader bytes still match.
        Stage/profile/ABI remain diagnostics and final-publication checks; they
        are not continuation authority. Invalid, completed, idle or foreign
        sessions are ignored and can never veto a new organization request.
        """

        sessions_dir = root / "ai" / "organization-sessions"
        pending_owner = pending_workspace_mutation_owner(
            store_root=self._mutation_store,
            workspace_root=root,
        )
        if not recover and pending_owner is not None:
            raise ShaderOrganizationError("saved organization mutation requires recovery before editing")
        if not sessions_dir.is_dir() or sessions_dir.is_symlink():
            if pending_owner is not None:
                raise ShaderOrganizationError(
                    "saved organization mutation has no recoverable session"
                )
            return None
        candidates = []
        entries = sorted(
            sessions_dir.iterdir(),
            key=lambda item: (item.name != pending_owner, item.name),
        )
        recovered_owner = pending_owner is None
        for entry in entries:
            if not entry.is_dir() or entry.is_symlink() or not (entry / "session.json").is_file():
                continue
            session: Mapping[str, Any] | None = None
            try:
                if entry.name == pending_owner and _ID.fullmatch(entry.name):
                    # The workspace-wide Host slot identifies its owner without
                    # trusting a possibly half-replaced workspace session file.
                    self._recover_pending_mutation(root, entry.name, entry)
                    recovered_owner = True
                session = _read_session_file(entry, "organization session")
                # Idle/completed history is explanatory only. In particular,
                # an idle release intentionally edits session.json outside the
                # organization manager and used to leave a stale checkpoint
                # that blocked every later begin with an authority error.
                if session.get("state") not in OPEN_DRAFT_STATES:
                    continue
                candidate_id = str(session.get("organizationId") or "")
                if (
                    _ID.fullmatch(candidate_id) is not None
                    and candidate_id == entry.name
                ):
                    # No recovery call is needed here: the one workspace-wide
                    # owner was recovered before reading its session above.
                    pass
                else:
                    continue
                if status is not None:
                    self._validate_session(session, root, str(session.get("organizationId")), status)
            except Exception as error:  # unknown/foreign sessions remain irrelevant
                if (
                    recover and status is not None
                    and isinstance(session, Mapping)
                    and self._migrate_authenticated_legacy_session(
                        root, entry, session, status
                    )
                ):
                    continue
                # A draft for another program, or malformed historical state,
                # cannot be applied to the current Shader. Preserve it on disk
                # for inspection and let begin create a clean current-program
                # session instead of turning history into a workflow blocker.
                continue
            organization_id = str(session.get("organizationId") or entry.name)
            if status is not None:
                unused_root, unused_path, unused_bytes, current_sha = (
                    self._workspace_source(status)
                )
                expected_source_sha = str(
                    session.get("syncedDraftSourceSha256")
                    or session.get("baselineSourceSha256") or ""
                )
                if expected_source_sha != current_sha:
                    # A process may have stopped after the locked source CAS
                    # and before the session/gate metadata transaction. Resume
                    # only when the current source is the exact proposal of an
                    # authenticated draft; every other edit is a new starting
                    # point and must remain untouched.
                    supplied_hmac = str(session.get("sessionIntegrityHmacSha256") or "")
                    authenticated = bool(
                        _SHA256.fullmatch(supplied_hmac)
                        and hmac.compare_digest(supplied_hmac, self._session_hmac(session))
                    ) or self._checkpoints.contains(session)
                    if not authenticated:
                        continue
                    try:
                        proposal_path = self._session_artifact(
                            root, session, "proposalSourcePath", entry, "proposal source"
                        )
                        proposal = _regular_bytes(
                            proposal_path, MAX_SOURCE_BYTES, "proposal source"
                        )
                    except Exception:
                        continue
                    if _sha256(proposal) != current_sha:
                        continue
            candidates.append((str(session.get("createdAt") or session.get("startedAt") or ""), entry.name, session))
        if not recovered_owner:
            raise ShaderOrganizationError(
                "saved organization mutation owner session is unavailable"
            )
        if not candidates:
            return None
        candidates.sort()
        return dict(candidates[-1][2])

    def _migrate_authenticated_legacy_session(
        self,
        root: Path,
        session_dir: Path,
        session: Mapping[str, Any],
        status: Mapping[str, Any],
    ) -> bool:
        """Retire one exact v1 draft so begin can create a fresh v2 review.

        Source/proposal evidence remains in place and a bounded migration receipt
        records its hashes.  It has no publication authority.  Any mismatch with
        the Host checkpoint falls through to the ordinary tamper failure.
        """

        organization_id = str(session.get("organizationId") or "")
        supplied = str(session.get("sessionIntegrityHmacSha256") or "")
        process_sealed = (
            _SHA256.fullmatch(supplied) is not None
            and hmac.compare_digest(supplied, self._session_hmac(session))
        )
        identity_valid = bool(
            session.get("schema") == ORGANIZATION_SESSION_SCHEMA
            and session.get("schemaVersion") == 1
            and session.get("policyVersion") in LEGACY_ORGANIZATION_POLICY_VERSIONS
            and session.get("state") in OPEN_DRAFT_STATES
            and organization_id == session_dir.name
            and _ID.fullmatch(organization_id)
            and Path(str(session.get("workspacePath") or "")).resolve() == root
            and session.get("workspaceId") == status.get("workspaceId")
            and session.get("captureId") == status.get("capture", {}).get("captureId")
            and session.get("stage") == status.get("stage")
            and session.get("entryPoint") == status.get("entryPoint")
            and session.get("sourceFormat", "hlsl") == status.get("source", {}).get("format")
            and session.get("baselineCompiledAndAbiValidated") is True
            and session.get("semanticEquivalenceVerified") is False
            and session.get("replayApplied") is False
        )
        if not identity_valid:
            return False
        checkpointed = self._checkpoints.contains(session)
        if not process_sealed and not checkpointed:
            return False
        if not checkpointed:
            self._checkpoints.remember(session)
        proposal_path = self._session_artifact(
            root, session, "proposalSourcePath", session_dir, "legacy proposal source"
        )
        baseline_path = self._session_artifact(
            root, session, "baselineSourcePath", session_dir, "legacy baseline source"
        )
        migration = {
            "schema": "renderdoc-ai-shader-organization-policy-migration/v1",
            "schemaVersion": 1,
            "organizationId": organization_id,
            "workspaceId": session.get("workspaceId"),
            "fromPolicyVersion": session.get("policyVersion"),
            "toPolicyVersion": ORGANIZATION_POLICY_VERSION,
            "legacySessionPath": str(session_dir),
            "legacySessionSha256": _sha256(_canonical_json(dict(session))),
            "baselineSourceSha256": _sha256(_regular_bytes(
                baseline_path, MAX_SOURCE_BYTES, "legacy baseline source"
            )),
            "proposalSourceSha256": _sha256(_regular_bytes(
                proposal_path, MAX_SOURCE_BYTES, "legacy proposal source"
            )),
            "explanationOnly": True,
            "publicationAt": _utc_now(),
            "reason": (
                "The v1 review predates complete declaration/provenance quality gates; "
                "its artifacts are preserved for reference and cannot authorize publication."
            ),
        }
        migration_dir = root / "ai" / "organization-policy-migrations"
        _safe_dir(migration_dir, root / "ai", "organization policy migrations", create=True)
        atomic_write_json(migration_dir / (organization_id + ".json"), migration)
        self._checkpoints.retire_as_explanation_only(session)
        return True

    def resume_summary(self, root: Path, status: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        """Compact progress facts for an open session (for status/resume)."""

        session = self.open_session(root, status)
        if session is None:
            return None
        return {
            "organizationId": session.get("organizationId"),
            "state": session.get("state"),
            "proposalSourcePath": str(root / str(session.get("proposalSourcePath", ""))),
            "renames": len(session.get("renameMap") or {}),
            "renameProvenance": len(session.get("renameProvenance") or {}),
            "comments": len(session.get("commentMap") or {}),
            "structureOps": len(session.get("structureOps") or []),
            "functionNotes": len(session.get("functionNotes") or {}),
        }

    def begin(
        self,
        workspace_path: str,
        semantic_context: Mapping[str, Any] | None = None,
        *,
        restart: bool = False,
        editable_source_only: bool = False,
    ) -> dict[str, Any]:
        """Open or resume a draft from current source without compiling it.

        A compatible open session retains its edits and source identity. A
        stale or corrupt historical draft cannot veto a new session from the
        current source; ``restart`` requests a fresh session explicitly.
        """

        with self._guard:
            service = self._service_factory()
            initial_status = _draft_status(service, workspace_path)
            initial_root, unused_path, unused_bytes, unused_sha = (
                self._workspace_source(initial_status)
            )
            if not restart:
                existing = self.open_session(initial_root, initial_status)
                if existing is not None:
                    session_dir = initial_root / "ai" / "organization-sessions" / str(existing["organizationId"])
                    context_path = session_dir / "semantic-context.json"
                    try:
                        stored_context = read_json_object(context_path, MAX_CONTEXT_BYTES, "semantic context")
                    except Exception:  # noqa: BLE001
                        stored_context = dict(semantic_context or {})
                    try:
                        rebound = self._rebind_session_context(
                            existing, initial_root, initial_status
                        )
                        atomic_write_json(session_dir / "session.json", rebound)
                        result = self._begin_result(
                            initial_root, session_dir, rebound, stored_context
                        )
                    except (ShaderOrganizationError, OSError, ValueError):
                        # Preserve the unusable draft as evidence, then continue
                        # below from the current canonical source. A historical
                        # artifact is never workflow authority.
                        result = None
                    if result is not None:
                        self._publish_root_gate(initial_root, self._inactive_gate(
                            organization_id=str(existing["organizationId"]), status=initial_status,
                            decision="organization-beginning", reason="shader source draft is open",
                            source_sha256=unused_sha,
                        ))
                        result["resumed"] = True
                        result["progress"] = self.resume_summary(initial_root, initial_status)
                        self.checkpoint(str(initial_root), str(existing["organizationId"]), status=initial_status)
                        return result
            with source_observation_transaction(initial_root):
                # A short source snapshot transaction is unrelated to replay
                # ownership. Later canonical saves retain their own source CAS.
                status = _draft_status(service, workspace_path)
                root, source_path, source_bytes, source_sha = (
                    self._workspace_source(status)
                )
                if root != initial_root:
                    raise ShaderOrganizationError(
                        "workspace identity changed during organization source snapshot"
                    )
                result = self._begin_owned(
                    service,
                    status,
                    root,
                    source_path,
                    source_bytes,
                    source_sha,
                    semantic_context,
                    restart=restart,
                    editable_source_only=editable_source_only,
                )
                self.checkpoint(str(root), str(result["organizationId"]), status=status)
                return result

    def repair_baseline_source(
        self,
        workspace_path: str,
        expected_source_sha256: str,
        source_text: Any,
        *,
        acknowledge_semantic_equivalence_unverified: bool,
    ) -> dict[str, Any]:
        """Replace the workspace HLSL with an AI-authored captured-ABI repair.

        The decompiled text is compiled and replay-ABI validated first; only a
        candidate whose fresh reflection matches the captured physical ABI is
        kept as the new organization baseline.  Anything else leaves the source
        bytes exactly as they were and returns the compiler/ABI evidence, so an
        Agent can iterate without ever leaving a broken source behind.  When the
        byte-for-byte decompiler output is unavailable, the readable current
        source is recorded as fallback provenance instead of blocking repair.
        """

        if acknowledge_semantic_equivalence_unverified is not True:
            raise ShaderOrganizationError(
                "ABI source repair requires acknowledging that semantic "
                "equivalence stays unverified"
            )
        expected = _hex(expected_source_sha256, "expected source SHA-256")
        if not isinstance(source_text, str) or not source_text.strip():
            raise ShaderOrganizationError("ABI repair source must be non-empty text")
        if "\x00" in source_text:
            raise ShaderOrganizationError("ABI repair source contains NUL characters")
        try:
            replacement = source_text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ShaderOrganizationError("ABI repair source is not valid UTF-8") from exc
        if len(replacement) > MAX_SOURCE_BYTES:
            raise ShaderOrganizationError("ABI repair source exceeds its fixed size limit")
        replacement_sha = _sha256(replacement)

        with self._guard:
            service = self._service_factory()
            initial_status = service.status(workspace_path)
            if initial_status.get("source", {}).get("format") != "hlsl":
                raise ShaderOrganizationError("this captured-ABI source repair channel supports HLSL only")
            initial_program_identity = _abi_repair_program_identity(initial_status)
            initial_root, unused_path, unused_bytes, unused_sha = (
                self._workspace_source(initial_status)
            )
            lease = WorkspaceSourceTransactionLease(initial_root)
            try:
                lease.acquire()
            except WorkspaceSourceLeaseError as exc:
                raise ShaderOrganizationError(
                    "ABI source repair requires the exclusive workspace source "
                    "transaction lease; stop the active watcher/live edit first"
                ) from exc
            try:
                status = service.status(workspace_path)
                root, source_path, source_bytes, source_sha = (
                    self._workspace_source(status)
                )
                if root != initial_root:
                    raise ShaderOrganizationError(
                        "workspace identity changed while ABI repair acquired ownership"
                    )
                _validate_source_directives(source_text, "ABI repair source", source_path)
                program_identity = _abi_repair_program_identity(status)
                if program_identity != initial_program_identity:
                    raise ShaderOrganizationError(
                        "captured Shader raw/stage/entry/layout identity changed while "
                        "ABI repair acquired ownership"
                    )
                if source_sha != expected:
                    raise ShaderOrganizationError(
                        "workspace source changed before the ABI repair; read the "
                        "current source again and resubmit"
                    )
                if replacement == source_bytes:
                    raise ShaderOrganizationError(
                        "ABI repair source is identical to the current workspace source"
                    )
                return self._repair_baseline_owned(
                    service, status, root, source_path, source_bytes, source_sha,
                    replacement, replacement_sha, program_identity,
                )
            finally:
                lease.release()

    def _repair_baseline_owned(
        self,
        service: Any,
        status: Mapping[str, Any],
        root: Path,
        source_path: Path,
        source_bytes: bytes,
        source_sha: str,
        replacement: bytes,
        replacement_sha: str,
        program_identity: Mapping[str, Any],
    ) -> dict[str, Any]:
        from .diagnostics import classify_shader_failure

        ai_dir = root / "ai"
        _safe_dir(ai_dir, root, "workspace AI directory", create=True)
        attempts_dir = _safe_dir(
            ai_dir / "abi-repair-attempts", ai_dir, "ABI repair attempts directory",
            create=True,
        )
        baseline_source_provenance = _baseline_source_provenance(
            status, ai_dir, source_bytes, source_sha,
            program_identity["originalRawSha256"],
        )
        decompiled_sha = baseline_source_provenance["declaredDecompilerSourceSha256"]

        attempt_id = uuid.uuid4().hex
        attempt_source = attempts_dir / (attempt_id + ".hlsl")
        receipt_path = attempts_dir / (attempt_id + ".json")
        atomic_write_bytes(attempt_source, replacement)
        receipt: dict[str, Any] = {
            "schema": "renderdoc-ai-shader-abi-source-repair/v1",
            "schemaVersion": 1,
            "attemptId": attempt_id,
            "at": _utc_now(),
            "workspacePath": str(root),
            "workspaceId": status.get("workspaceId"),
            "captureId": status.get("capture", {}).get("captureId"),
            "stage": status.get("stage"),
            "entryPoint": status.get("entryPoint"),
            "previousSourceSha256": source_sha,
            "decompilerOriginalSha256": decompiled_sha,
            "baselineSourceProvenance": baseline_source_provenance,
            "capturedProgramIdentity": dict(program_identity),
            "repairedSourceSha256": replacement_sha,
            "attemptSourcePath": str(attempt_source.relative_to(root)).replace("\\", "/"),
            "semanticEquivalenceVerified": False,
            "replayApplied": False,
        }

        # A compile-only check first: a source that does not even compile must
        # never be swapped in, and the Agent gets line-bound diagnostics.
        check = None
        compile_check = getattr(service, "compile_check", None)
        if callable(compile_check):
            try:
                check = compile_check(str(root), replacement.decode("utf-8"))
            except Exception as exc:  # evidence only; the swap below still gates
                check = {"ok": None, "reason": str(exc)[:2000]}
        if isinstance(check, Mapping) and check.get("ok") is False:
            receipt.update({
                "accepted": False,
                "phase": "compile-check",
                "sourceRestored": True,
                "diagnostics": list(check.get("diagnostics") or [])[:128],
            })
            atomic_write_json(receipt_path, receipt)
            return self._repair_result(root, receipt, status, None)

        try:
            swapped = _source_compare_and_swap(
                source_path, source_bytes, replacement, "workspace HLSL"
            )
        except ShaderOrganizationError:
            raise
        if not swapped:
            raise ShaderOrganizationError(
                "workspace shader source changed on disk during the ABI repair"
            )
        failure: dict[str, Any] | None = None
        compile_result: dict[str, Any] | None = None
        validation: dict[str, Any] | None = None
        try:
            compile_result, validation = self._compile_and_validate(
                service, root, replacement_sha, "ABI-repaired source"
            )
        except Exception as exc:
            failure = {
                "type": type(exc).__name__,
                "message": str(exc)[:4000],
                "diagnostic": classify_shader_failure(exc, phase="abi-source-repair"),
            }
        after = None
        try:
            after = service.status(str(root))
        except Exception as exc:
            if failure is None:
                failure = {"type": type(exc).__name__, "message": str(exc)[:4000]}
        if failure is None and isinstance(after, Mapping):
            try:
                observed_program_identity = _abi_repair_program_identity(after)
                if observed_program_identity != dict(program_identity):
                    receipt["observedProgramIdentity"] = observed_program_identity
                    raise ShaderOrganizationError(
                        "captured Shader raw/stage/entry/layout identity changed during "
                        "ABI repair"
                    )
            except Exception as exc:
                failure = {
                    "type": type(exc).__name__,
                    "message": str(exc)[:4000],
                    "diagnostic": classify_shader_failure(
                        exc, phase="abi-source-repair"
                    ),
                }
        latest = after.get("latestCompiled") if isinstance(after, Mapping) else None
        if failure is None and isinstance(latest, Mapping):
            receipt.update({
                "accepted": True,
                "phase": "compiled-and-replay-abi-validated",
                "sourceRestored": False,
                "revision": compile_result.get("revision") if compile_result else None,
                "candidateRawSha256": compile_result.get("rawSha256") if compile_result else None,
                "codeSha256": compile_result.get("codeSha256") if compile_result else None,
                "abiRequestId": validation.get("requestId") if validation else None,
                "abiReportSha256": validation.get("abiReportSha256") if validation else None,
                "abiGate": dict(latest.get("abiGate") or {}),
            })
            atomic_write_json(receipt_path, receipt)
            return self._repair_result(root, receipt, after, None)

        # Fail closed: put the previous bytes back before reporting anything.
        restored = _source_compare_and_swap(
            source_path, replacement, source_bytes, "workspace HLSL"
        )
        if not restored:
            raise ShaderOrganizationError(
                "ABI repair failed and the previous workspace source could not be "
                "restored automatically; inspect %s" % source_path
            )
        evidence: dict[str, Any] = {}
        if isinstance(after, Mapping):
            attempt = after.get("lastAbiAttempt")
            if isinstance(attempt, Mapping):
                evidence["abiComparison"] = attempt.get("comparison")
                # Identity-keyed findings with a concrete HLSL hint each; this
                # is what the next repair attempt should be written against.
                evidence["abiExplanation"] = attempt.get("explanation")
            if isinstance(latest, Mapping):
                evidence["latestCompiled"] = {
                    key: latest.get(key)
                    for key in ("revision", "sourceSha256", "rawSha256", "abiGate")
                }
            evidence["abiValidationStatus"] = after.get("abiValidationStatus")
        receipt.update({
            "accepted": False,
            "phase": "compile-or-replay-abi",
            "sourceRestored": True,
            "failure": failure,
            "evidence": evidence,
        })
        atomic_write_json(receipt_path, receipt)
        return self._repair_result(root, receipt, after or status, failure)

    def _repair_result(
        self,
        root: Path,
        receipt: Mapping[str, Any],
        status: Mapping[str, Any] | None,
        failure: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        accepted = receipt.get("accepted") is True
        result: dict[str, Any] = {
            "schema": "renderdoc-ai-shader-abi-source-repair-result/v1",
            "schemaVersion": 1,
            "workspacePath": str(root),
            "attemptId": receipt.get("attemptId"),
            "accepted": accepted,
            "phase": receipt.get("phase"),
            "sourceRestored": receipt.get("sourceRestored"),
            "currentSourceSha256": (
                receipt.get("repairedSourceSha256") if accepted
                else receipt.get("previousSourceSha256")
            ),
            "semanticEquivalenceVerified": False,
            "replayApplied": False,
            "baselineSourceProvenance": copy.deepcopy(
                dict(receipt.get("baselineSourceProvenance") or {})
            ),
            "capturedProgramIdentity": copy.deepcopy(
                dict(receipt.get("capturedProgramIdentity") or {})
            ),
            "receiptPath": str(root / "ai" / "abi-repair-attempts" / (str(receipt.get("attemptId")) + ".json")),
        }
        if accepted:
            result.update({
                "revision": receipt.get("revision"),
                "candidateRawSha256": receipt.get("candidateRawSha256"),
                "abiGate": receipt.get("abiGate"),
                "state": "abi-repaired",
            })
        else:
            evidence = receipt.get("evidence") or {}
            result.update({
                "diagnostics": receipt.get("diagnostics"),
                "failure": dict(failure) if failure else None,
                "evidence": evidence,
                "baselineInterface": (
                    (status or {}).get("baseline", {}).get("compiledInterface")
                    if isinstance(status, Mapping) else None
                ),
                "state": "abi-repair-rejected",
            })
        return result

    def _begin_owned(
        self,
        service: Any,
        status: Mapping[str, Any],
        root: Path,
        source_path: Path,
        source_bytes: bytes,
        source_sha: str,
        semantic_context: Mapping[str, Any] | None,
        *,
        restart: bool = False,
        editable_source_only: bool = False,
    ) -> dict[str, Any]:
        """Create begin artifacts from the caller's short source snapshot."""

        context = dict(semantic_context or {})
        ai_dir = root / "ai"
        if ai_dir.exists():
            _safe_dir(ai_dir, root, "workspace AI directory", create=False)
        else:
            ai_dir.mkdir(parents=False, exist_ok=False)
        # Each explicit request starts from the current editable source. Old
        # organization receipts remain historical evidence, never draft input
        # or a reason to delay this request.
        source_text = source_bytes.decode("utf-8", errors="strict")
        try:
            _validate_source_directives(source_text, "organization baseline HLSL", source_path)
        except ShaderOrganizationError as error:
            if not editable_source_only:
                raise
            # IDE saves can retain incomplete include directives. They only
            # open an inactive draft; compile and finish still require the
            # complete, frozen dependency graph before any publication.
            context["sourceDependencyDiagnostic"] = {
                "reason": str(error)[:1024], "validationDeferred": True,
                "sourceEditable": True, "replayApplied": False,
            }
        # Display names the user (or an earlier explicit AI publication) gave
        # this shader's CBuffer members are optional naming evidence only.
        # Panel lookup/availability must never gate source organization.
        alias_identity = None
        if context.get("includeLiveBindingEvidence") is True and self._cbuffer_alias_identity is not None:
            try:
                candidate = self._cbuffer_alias_identity(str(root))
                if (
                    not isinstance(candidate, Mapping)
                    or candidate.get("schema") != "replaylab-cbuffer-alias-identity/v1"
                    or candidate.get("stage") != status.get("stage")
                    or candidate.get("entryPoint") != status.get("entryPoint")
                    or candidate.get("rawSha256") != status.get("baseline", {}).get("rawSha256")
                    or not isinstance(candidate.get("blocks"), list)
                    or _json_size(candidate) > MAX_CONTEXT_BYTES // 2
                ):
                    raise ValueError("captured CBuffer alias identity is invalid")
                alias_identity = dict(candidate)
                context["cbufferAliasIdentity"] = alias_identity
            except (OSError, RuntimeError, ValueError) as alias_error:
                context["cbufferAliasEvidenceUnavailable"] = {
                    "reason": str(alias_error)[:800],
                    "sourceOrganizationBlocked": False,
                    "requiredToolForPanelNames": "publish_cbuffer_aliases",
                }
        try:
            context["cbufferAliases"] = cbuffer_aliases.evidence_for_source(
                source_text, alias_identity
            )
        except (OSError, ValueError) as alias_error:
            context["cbufferAliases"] = {
                "available": False,
                "blocks": [],
                "reason": str(alias_error)[:800],
            }
            context["cbufferAliasEvidenceUnavailable"] = {
                "reason": str(alias_error)[:800],
                "sourceOrganizationBlocked": False,
                "requiredToolForPanelNames": "publish_cbuffer_aliases",
            }
        cbuffer_user_aliases = _source_bound_user_aliases(context["cbufferAliases"])
        if _json_size(context) > MAX_CONTEXT_BYTES:
            raise ShaderOrganizationError("semantic context exceeds its fixed size limit")

        # The public caller already owns this re-entrant guard. Keeping the
        # artifact transaction scoped here also makes direct internal use safe.
        with self._guard:
            sessions_dir = ai_dir / "organization-sessions"
            if sessions_dir.exists():
                _safe_dir(sessions_dir, ai_dir, "organization sessions directory", create=False)
            else:
                sessions_dir.mkdir(parents=False, exist_ok=False)

            organization_id = uuid.uuid4().hex
            # Invalidate any older successful root gate before this new review
            # session exposes editable proposal files.
            self._publish_root_gate(
                root,
                self._inactive_gate(
                    organization_id=organization_id,
                    status=status,
                    decision="organization-beginning",
                    reason="shader source draft is open",
                    source_sha256=source_sha,
                ),
            )

            source_format = str(status["source"]["format"])
            raw_original = ai_dir / ("decompiler-original." + source_format)
            baseline_source_provenance = _baseline_source_provenance(
                status, ai_dir, source_bytes, source_sha,
                _hex(
                    (status.get("baseline") or {}).get("rawSha256"),
                    "workspace original Shader raw SHA-256",
                ),
            )
            decompiled_sha = baseline_source_provenance["declaredDecompilerSourceSha256"]
            preserved_decompiler_source = baseline_source_provenance["decompilerOriginalPreserved"]
            context["baselineSourceProvenance"] = copy.deepcopy(
                baseline_source_provenance
            )
            if _json_size(context) > MAX_CONTEXT_BYTES:
                raise ShaderOrganizationError(
                    "semantic context exceeds its fixed size limit"
                )

            session_dir = sessions_dir / organization_id
            session_dir.mkdir(parents=False, exist_ok=False)
            baseline_path = session_dir / ("baseline-source." + source_format)
            proposal_path = session_dir / ("organized-proposal." + source_format)
            analysis_path = session_dir / "analysis.md"
            flowchart_path = session_dir / "flowchart.mmd"
            module_descriptions_path = session_dir / "module-descriptions.json"
            structured_flow_graph_path = session_dir / "flow-graph.json"
            context_path = session_dir / "semantic-context.json"
            atomic_write_bytes(baseline_path, source_bytes)
            atomic_write_bytes(proposal_path, source_bytes)
            # Optional explanations start empty. Parsing every function to
            # prefill a document is unnecessary for a source editing draft.
            atomic_write_bytes(analysis_path, b"")
            atomic_write_bytes(flowchart_path, b"")
            atomic_write_json(
                module_descriptions_path,
                module_description_template(""),
            )
            atomic_write_json(context_path, context)

            # Draft creation only snapshots source and capture facts. Finalization
            # obtains fresh compiler and ABI evidence for both versions once.
            baseline_compile: dict[str, Any] | None = None
            baseline_validation: dict[str, Any] | None = None
            baseline_validation_error = ""

            shader_program_identity = _shader_program_identity(status)
            session = self._seal_session({
                "schema": ORGANIZATION_SESSION_SCHEMA,
                "schemaVersion": 1,
                "policyVersion": ORGANIZATION_POLICY_VERSION,
                "organizationId": organization_id,
                "state": "drafting",
                "createdAt": _utc_now(),
                "workspacePath": str(root),
                "workspaceId": _hex(status.get("workspaceId"), "workspaceId", 32),
                "captureId": _hex(
                    status.get("capture", {}).get("captureId"), "captureId"
                ),
                "shaderProgramIdentity": shader_program_identity,
                "originalRawSha256": shader_program_identity[
                    "originalRawSha256"
                ],
                "targetProfile": copy.deepcopy(
                    shader_program_identity["targetProfile"]
                ),
                "targetProfileSha256": shader_program_identity[
                    "targetProfileSha256"
                ],
                "physicalAbiSha256": shader_program_identity[
                    "physicalAbiSha256"
                ],
                "stage": str(status.get("stage", "")),
                "entryPoint": str(status.get("entryPoint", "")),
                "sourcePath": str(source_path.relative_to(root)).replace("\\", "/"),
                "sourceFormat": source_format,
                "baselineSourcePath": str(baseline_path.relative_to(root)).replace("\\", "/"),
                "proposalSourcePath": str(proposal_path.relative_to(root)).replace("\\", "/"),
                "analysisPath": str(analysis_path.relative_to(root)).replace("\\", "/"),
                "flowchartPath": str(flowchart_path.relative_to(root)).replace("\\", "/"),
                "moduleDescriptionsPath": str(
                    module_descriptions_path.relative_to(root)
                ).replace("\\", "/"),
                "structuredFlowGraphPath": str(
                    structured_flow_graph_path.relative_to(root)
                ).replace("\\", "/"),
                "semanticContextPath": str(context_path.relative_to(root)).replace("\\", "/"),
                "decompilerOriginalPath": (
                    str(raw_original.relative_to(root)).replace("\\", "/")
                    if preserved_decompiler_source else None
                ),
                "baselineSourceSha256": source_sha,
                "proposalSourceSha256": source_sha,
                "decompilerOriginalSha256": (
                    decompiled_sha if preserved_decompiler_source else None
                ),
                "declaredDecompilerSourceSha256": decompiled_sha,
                "baselineSourceProvenance": baseline_source_provenance,
                "baselineRevision": baseline_compile["revision"] if baseline_compile else None,
                "baselineCandidateRawSha256": baseline_compile["rawSha256"] if baseline_compile else "",
                "baselineCodeSha256": baseline_compile["codeSha256"] if baseline_compile else "",
                "baselineAbiRequestId": (
                    _hex(baseline_validation.get("requestId"), "baseline ABI request ID")
                    if baseline_validation else ""
                ),
                "baselineAbiReportSha256": (
                    _hex(baseline_validation.get("abiReportSha256"), "baseline ABI report SHA-256")
                    if baseline_validation else ""
                ),
                "baselineCompiledAndAbiValidated": bool(baseline_compile and baseline_validation),
                "baselineValidationError": baseline_validation_error,
                "semanticEquivalenceVerified": False,
                "replayApplied": False,
                # Host-applied edits (rename_shader_workspace_symbols); a proposal
                # that equals baseline + these renames is accepted without a
                # byte-identical executable (DXIL keeps names in metadata).
                "renameMap": {},
                "renameProvenance": {},
                "commentMap": {},
                "structureOps": [],
                "functionNotes": {},
                "mutationReceipts": {},
                "lastMutationResult": None,
                "cbufferAliasIdentity": alias_identity,
                "cbufferUserAliasRenames": cbuffer_user_aliases,
            })
            if baseline_compile is not None and baseline_validation is not None:
                atomic_write_json(session_dir / "baseline-compile.json", baseline_compile)
                atomic_write_json(session_dir / "baseline-abi.json", baseline_validation)
            atomic_write_json(session_dir / "session.json", session)
            self._publish_root_gate(
                root,
                self._inactive_gate(
                    organization_id=organization_id,
                    status=status,
                    decision="drafting",
                    reason="shader source draft is open",
                    source_sha256=source_sha,
                    compile_result=baseline_compile,
                    validation=baseline_validation,
                    source_restored=True,
                ),
            )
            return self._begin_result(root, session_dir, session, context)

    def rename(
        self,
        workspace_path: str,
        organization_id: str,
        renames: Mapping[str, Any],
        comments: Mapping[str, Any] | None = None,
        asset_name_authorization: Mapping[str, Any] | None = None,
        *,
        operation_id: str = "",
        request_sha256: str = "",
        compatible_request_sha256: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Apply declaration-bound names to the current draft.

        The source bytes read below are the transaction's compare-and-swap
        precondition. Historical structural operations remain provenance, not
        a requirement to rebuild the entire draft before a local rename.
        """

        with _OperationTiming("rename_shader_workspace_symbols", "rename") as timing, self._guard:
            timing.mark("workspaceStatus")
            organization_id = _hex(organization_id, "organization_id", 32)
            timing.dimensions(organizationId=organization_id, operationId=operation_id)
            service = self._service_factory()
            status = _draft_status(service, workspace_path)
            root, _source_path, _current_bytes, _current_sha = self._workspace_source(status)
            timing.mark("sessionRead")
            session_dir = root / "ai" / "organization-sessions" / organization_id
            _safe_dir(session_dir, root, "organization session directory", create=False)
            self._recover_pending_mutation(root, organization_id, session_dir)
            session_path = session_dir / "session.json"
            session = _read_session_file(session_dir, "organization session")
            original_session_bytes = _regular_bytes(
                session_path, MAX_CONTEXT_BYTES, "organization session"
            )
            self._validate_session(session, root, organization_id, status)
            if session.get("state") not in OPEN_DRAFT_STATES:
                raise ShaderOrganizationError("organization source draft is not open")
            replay = self._durable_mutation_replay(
                session,
                tool="rename_shader_workspace_symbols",
                operation_id=operation_id,
                request_sha256=request_sha256,
                compatible_request_sha256=compatible_request_sha256,
            )
            if replay is not None:
                return timing.attach(replay)
            timing.mark("baselineRead")
            baseline_path = self._session_artifact(
                root, session, "baselineSourcePath", session_dir, "baseline source"
            )
            baseline_text, _baseline_bytes = _utf8(
                baseline_path, MAX_SOURCE_BYTES, "organization baseline source"
            )
            # ``asset_name_authorization`` is a deprecated compatibility input.
            # It is deliberately ignored: Host asset/session records may inform
            # the Agent, but they never decide whether a source rename may run.
            incoming = {str(k): str(v) for k, v in dict(renames or {}).items()}
            existing_comments = dict(session.get("commentMap") or {})
            existing_renames = dict(session.get("renameMap") or {})
            timing.dimensions(
                organizationId=organization_id,
                operationId=operation_id,
                sourceByteSize=len(_baseline_bytes),
                sourceLineCount=baseline_text.count("\n") + int(not baseline_text.endswith("\n")),
                ledgerOperationCount=len(session.get("structureOps") or []),
                renameCount=len(existing_renames),
                incomingRenameCount=len(incoming),
            )
            actual_path = self._session_artifact(root, session, "proposalSourcePath", session_dir, "proposal source")
            actual_proposal, unused_bytes = _utf8(actual_path, MAX_SOURCE_BYTES, "proposal source")
            structure_ops = list(session.get("structureOps") or [])
            saved_structure_receipts = list(session.get("structureReceipts") or [])
            if (len(saved_structure_receipts) == len(structure_ops)
                    and any(item.get("operation") == "extract"
                            and "generatedNames" not in item for item in structure_ops)):
                from .restructure import retain_extraction_names
                structure_ops = retain_extraction_names(
                    structure_ops, saved_structure_receipts, existing_renames
                )
            prior_draft_sha = session.get("proposalSourceSha256")
            draft_was_edited = bool(
                prior_draft_sha and prior_draft_sha != _sha256(unused_bytes)
            )
            staged_replay = bool(
                structure_ops and prior_draft_sha == _sha256(unused_bytes)
                and session.get("draftRequiresCurrentSource") is not True
            )
            if len(incoming) > MAX_RENAMES_PER_REQUEST:
                raise ShaderOrganizationError(
                    "rename request rejected: at most %d renames per request (send the rest in "
                    "the next round; the session keeps the ones already accepted)"
                    % MAX_RENAMES_PER_REQUEST)
            timing.mark("renameValidation")
            try:
                if staged_replay:
                    # A structural extraction may split one original value
                    # between caller and helper. Its canonical selector must
                    # still rename both generated declarations. Replaying once
                    # is needed for that case; the current draft SHA avoids
                    # the former preflight and canonical replays.
                    from .restructure import validate_staged_renames
                    staged_renames = dict(existing_renames)
                    staged_renames.update(incoming)
                    additions = {
                        selector: target for selector, target in incoming.items()
                        if selector not in existing_renames
                    }
                    if additions:
                        current_additions, unused_deferred = split_bound_renames(
                            actual_proposal, additions
                        )
                        if current_additions:
                            validate_renames(
                                actual_proposal, current_additions,
                                limit=MAX_RENAMES_PER_REQUEST,
                            )
                    validate_language_renames(
                        baseline_text, staged_renames,
                        str(session.get("sourceFormat", "hlsl")),
                        str(session.get("entryPoint", "")),
                    )
                    clean = validate_staged_renames(
                        baseline_text, structure_ops, staged_renames
                    )
                    current_renames: dict[str, str] = {}
                else:
                    clean = {}
                # A repeated request may use the original selector even after
                # the previous target has become its current spelling. Rebase
                # that selector only; every actual edit is still validated
                # against the current source's lexical ownership.
                if not staged_replay:
                    current_requests: dict[str, str] = {}
                    original_selectors: dict[str, str] = {}
                    for selector, target in incoming.items():
                        if existing_renames.get(selector) == target:
                            continue
                        current_selector = selector
                        prior_target = existing_renames.get(selector)
                        if prior_target:
                            if "::" in selector:
                                owner, unused_leaf = selector.split("::", 1)
                                current_selector = (
                                    existing_renames.get(owner, owner) + "::" + prior_target
                                )
                            elif "." in selector:
                                owner, unused_leaf = selector.split(".", 1)
                                current_selector = (
                                    existing_renames.get(owner, owner) + "." + prior_target
                                )
                            else:
                                current_selector = prior_target
                        elif "::" in selector:
                            owner, leaf = selector.split("::", 1)
                            current_selector = existing_renames.get(owner, owner) + "::" + leaf
                        elif "." in selector:
                            owner, leaf = selector.split(".", 1)
                            current_selector = existing_renames.get(owner, owner) + "." + leaf
                        if (current_selector in current_requests
                                and current_requests[current_selector] != target):
                            raise EquivalenceError(
                                "two selectors request different names for %s" % current_selector
                            )
                        current_requests[current_selector] = target
                        original_selectors[current_selector] = selector
                        if "::" in current_selector:
                            owner, leaf = current_selector.split("::", 1)
                            for receipt in session.get("structureReceipts") or []:
                                if (not isinstance(receipt, Mapping)
                                        or receipt.get("operation") != "extract"
                                        or receipt.get("enclosingFunction") != owner
                                        or leaf not in (
                                            [receipt.get("returns")]
                                            + list(receipt.get("inputs") or [])
                                            + list(receipt.get("inouts") or [])
                                        )):
                                    continue
                                helper_selector = str(receipt.get("name") or "") + "::" + leaf
                                if helper_selector != current_selector:
                                    if (helper_selector in current_requests
                                            and current_requests[helper_selector] != target):
                                        raise EquivalenceError(
                                            "two selectors request different names for %s" % helper_selector
                                        )
                                    current_requests[helper_selector] = target
                                    original_selectors[helper_selector] = helper_selector
                    validate_language_renames(
                        actual_proposal, current_requests,
                        str(session.get("sourceFormat", "hlsl")),
                        str(session.get("entryPoint", "")),
                    )
                    current_renames = validate_renames(
                        actual_proposal, current_requests,
                        limit=MAX_RENAMES_PER_REQUEST,
                    ) if current_requests else {}
                    proposal = apply_renames(actual_proposal, current_renames)
                    clean = dict(existing_renames)
                    for current_selector, target in current_renames.items():
                        clean[original_selectors.get(current_selector, current_selector)] = target
            except (EquivalenceError, ValueError) as exc:
                raise ShaderOrganizationError("rename request rejected: %s" % exc) from exc
            merged_comments = dict(session.get("commentMap") or {})
            merged_comments.update(
                {str(k): str(v) for k, v in dict(comments or {}).items() if str(v or "").strip()}
            )
            timing.mark("proposalReplay")
            if staged_replay:
                renamed_comments = {clean.get(k, k): v for k, v in merged_comments.items()}
                proposal, missing, structure_receipts = self._regenerate_proposal(
                    baseline_text, clean, structure_ops, renamed_comments
                )
                from .restructure import retain_extraction_names
                structure_ops = retain_extraction_names(
                    structure_ops, structure_receipts, clean
                )
            else:
                renamed_comments = {
                    current_renames.get(existing_renames.get(k, k), existing_renames.get(k, k)): v
                    for k, v in merged_comments.items()
                }
                try:
                    proposal, missing = insert_function_comments(proposal, renamed_comments)
                except EquivalenceError as exc:
                    raise ShaderOrganizationError("comment request rejected: %s" % exc) from exc
                structure_receipts = list(session.get("structureReceipts") or [])
                for selector, target in current_renames.items():
                    if "::" in selector:
                        owner, leaf = selector.split("::", 1)
                        for receipt in structure_receipts:
                            if (receipt.get("operation") == "extract"
                                    and receipt.get("name") == owner
                                    and receipt.get("returns") == leaf):
                                receipt["returns"] = target
                    else:
                        for receipt in structure_receipts:
                            if receipt.get("name") == selector:
                                receipt["name"] = target
                            if receipt.get("enclosingFunction") == selector:
                                receipt["enclosingFunction"] = target
                        for item in structure_ops:
                            if item.get("name") == selector:
                                item["name"] = target
                            anchor = item.get("anchor")
                            if isinstance(anchor, dict) and anchor.get("scope") == selector:
                                anchor["scope"] = target
            # These are source-bound identifier substitutions over a checked
            # structural ledger. Compiling the entire large Shader after every
            # naming batch makes a draft progressively slower as rewrites grow.
            # Keep compilation and replay ABI tied to the exact final proposal
            # at finalization.
            timing.mark("metadataAndStaticProof")
            proposal_path = self._session_artifact(
                root, session, "proposalSourcePath", session_dir, "proposal source"
            )
            updated = dict(session)
            updated["renameMap"] = clean
            updated["renameProvenance"] = self._build_rename_provenance(
                baseline_text,
                clean,
                self._verified_rename_provenance(root, status, session),
                (
                    session.get("cbufferUserAliasRenames")
                    if isinstance(session.get("cbufferUserAliasRenames"), Mapping)
                    else {}
                ),
            )
            updated["commentMap"] = merged_comments
            updated["structureOps"] = structure_ops
            updated["structureReceipts"] = structure_receipts
            updated["proposalSourceSha256"] = _sha256(proposal.encode("utf-8"))
            updated["draftRequiresCurrentSource"] = bool(
                session.get("draftRequiresCurrentSource") or draft_was_edited
            )
            # Source-token renames and CBuffer panel display aliases are two
            # independent effects.  Never infer or publish panel aliases from
            # this source mutation; the explicit publish_cbuffer_aliases route
            # validates current reflected physical targets, any supplied
            # compiled layout evidence, and its own durable receipt.
            alias_publication = {
                "attempted": False,
                "applied": False,
                "published": 0,
                "keptUser": [],
                "blocks": [],
                "memberRenames": [],
                "memberRenameCount": 0,
                "memberRenamesTruncated": False,
                "reasonId": "explicit-publication-required",
                "requiredTool": "publish_cbuffer_aliases",
                "sourceOrganizationBlocked": False,
            }
            # A legacy session may contain an implicit publication snapshot.
            # Keep it in historical artifacts/checkpoints, but do not carry it
            # forward as current review evidence.
            updated.pop("cbufferAliasPublication", None)
            check = (
                rename_only_equivalent(baseline_text, proposal, clean)
                if not structure_ops and not draft_was_edited
                and session.get("draftRequiresCurrentSource") is not True
                else {"equal": False}
            )
            result = {
                "schema": "renderdoc-ai-shader-organization-rename/v1",
                "schemaVersion": 1,
                "workspacePath": str(root),
                "organizationId": organization_id,
                "accepted": True,
                "proposalSourcePath": str(proposal_path),
                "proposalSourceSha256": _sha256(proposal.encode("utf-8")),
                "renamesApplied": clean,
                "renameProvenance": updated["renameProvenance"],
                "commentsApplied": sorted(set(renamed_comments) - set(missing)),
                "commentsNotFound": missing,
                "renameOnlyEquivalent": bool(check.get("equal")),
                "structureOps": len(structure_ops),
                "aliasesPath": str(root / "ai" / "aliases.json"),
                "viewPath": str(
                    root / "ai" / (
                        "organized." + str(session.get("sourceFormat", "hlsl"))
                    )
                ),
                "cbufferAliases": alias_publication,
                "state": "proposal-updated",
                "validationTier": "proposal-draft",
                "completedChecks": (
                    ["rename-only-static-proof"]
                    if bool(check.get("equal")) else []
                ),
                "draftViewPath": str(
                    root / "ai" / (
                        "organized." + str(session.get("sourceFormat", "hlsl"))
                    )
                ),
                "canonicalSourceUpdated": False,
                "replayAbiChecked": False,
                "applied": False,
            }
            updated = self._record_durable_mutation(
                updated,
                tool="rename_shader_workspace_symbols",
                operation_id=operation_id,
                request_sha256=request_sha256,
                compatible_request_sha256=compatible_request_sha256,
                result=result,
            )
            updated = self._seal_session(updated)
            session_bytes = (
                json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            if (
                len(_canonical_json(updated)) > MAX_CONTEXT_BYTES
                or len(session_bytes) > MAX_CONTEXT_BYTES
            ):
                raise ShaderOrganizationError("structural ledger exceeds its 2 MiB bound")
            if _regular_bytes(
                _source_path, MAX_SOURCE_BYTES, "workspace source"
            ) != _current_bytes:
                raise ShaderOrganizationError(
                    "organization source changed before rename transaction"
                )
            language = str(session.get("sourceFormat", "hlsl"))
            targets = self._alias_view_payloads(
                root,
                baseline_text,
                clean,
                merged_comments,
                proposal,
                language,
            )
            targets[proposal_path] = proposal.encode("utf-8")
            targets[session_path] = session_bytes
            timing.mark("transaction")
            apply_workspace_mutation(
                store_root=self._mutation_store,
                workspace_root=root,
                organization_id=organization_id,
                tool="rename_shader_workspace_symbols",
                operation_id=operation_id,
                request_sha256=request_sha256,
                targets=targets,
                expected_before={
                    proposal_path: unused_bytes,
                    session_path: original_session_bytes,
                },
                checkpoint_after_apply=lambda: self._remember_mutation_session(
                    session_dir
                ),
                checkpoint_after_rollback=lambda: self._remember_mutation_session(
                    session_dir
                ),
            )
            return timing.attach(result)

    @staticmethod
    def _regenerate_proposal(
        baseline_text: str,
        renames: Mapping[str, str],
        structure_ops: Sequence[Mapping[str, Any]],
        comments: Mapping[str, str],
    ) -> tuple[str, list[str], list[dict[str, Any]]]:
        """baseline -> renames -> structural operations -> comments, in that fixed order."""

        from .restructure import (RestructureError, anchor_extractions,
                                  apply_staged_operations)

        try:
            replay_ops = list(structure_ops)
            if any(item.get("operation") == "extract" and "generatedNames" not in item
                   for item in replay_ops):
                initial_renames, unused_deferred = split_bound_renames(
                    baseline_text, renames
                )
                replay_ops = anchor_extractions(
                    apply_renames(baseline_text, initial_renames),
                    replay_ops, initial_renames,
                )
            proposal, receipts = apply_staged_operations(
                baseline_text, replay_ops, dict(renames)
            )
        except (EquivalenceError, RestructureError) as exc:
            raise ShaderOrganizationError("structural operations no longer apply: %s" % exc) from exc
        try:
            proposal, missing = insert_function_comments(proposal, dict(comments))
        except EquivalenceError as exc:
            raise ShaderOrganizationError("comment request rejected: %s" % exc) from exc
        return proposal, missing, receipts

    def restructure(
        self,
        workspace_path: str,
        organization_id: str,
        operation: str,
        *,
        start_line: int = 0,
        end_line: int = 0,
        name: str = "",
        new_name: str = "",
        comment: str = "",
        expected_proposal_sha256: str = "",
        edits: list[dict[str, Any]] | None = None,
        operation_id: str = "",
        request_sha256: str = "",
        compatible_request_sha256: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Checked proposal edits: extract/rewrite/undo/remove/rename_function/note/list.

        HLSL rewrite is an expected-SHA-bound transaction of at most 16 complete
        statement/helper edits, 64 KiB before+after and 400 affected lines. Exact
        canonical code anchors compose with net renames and later structural
        operations; dependent removal is rejected instead of using stale lines.
        Existing global ABI/entry declarations remain protected; new pure
        computation structs are allowed. Draft edits use bounded source/static
        checks. Whole-program compile, replay ABI and equivalence are deferred
        to the exact final proposal at review; explicit segment verification
        remains available. GLSL supports list/note here, not structural rewriting.
        """

        from .restructure import (RestructureError, prepare_rewrite,
                                  source_bound_anchor, anchor_extractions, apply_operations,
                                  retain_extraction_names, _canonical_source)

        with _OperationTiming("restructure_shader_workspace_source", operation) as timing, self._guard:
            timing.mark("workspaceStatus")
            organization_id = _hex(organization_id, "organization_id", 32)
            timing.dimensions(organizationId=organization_id, operationId=operation_id)
            service = self._service_factory()
            status = _draft_status(service, workspace_path)
            root, _source_path, _current_bytes, _current_sha = self._workspace_source(status)
            timing.mark("sessionRead")
            session_dir = root / "ai" / "organization-sessions" / organization_id
            _safe_dir(session_dir, root, "organization session directory", create=False)
            self._recover_pending_mutation(root, organization_id, session_dir)
            session_path = session_dir / "session.json"
            session = _read_session_file(session_dir, "organization session")
            self._validate_session(session, root, organization_id, status)
            if session.get("state") not in OPEN_DRAFT_STATES:
                raise ShaderOrganizationError("organization source draft is not open")
            replay = self._durable_mutation_replay(
                session,
                tool="restructure_shader_workspace_source",
                operation_id=operation_id,
                request_sha256=request_sha256,
                compatible_request_sha256=compatible_request_sha256,
            )
            if replay is not None:
                return timing.attach(replay)
            timing.mark("baselineAndProposalRead")
            baseline_path = self._session_artifact(
                root, session, "baselineSourcePath", session_dir, "baseline source"
            )
            baseline_text, _baseline_bytes = _utf8(
                baseline_path, MAX_SOURCE_BYTES, "organization baseline source"
            )
            operation = str(operation or "").strip().lower()
            language = str(session.get("sourceFormat", "hlsl"))
            if language == "glsl" and operation not in {"list", "note"}:
                raise ShaderOrganizationError(
                    "GLSL organization supports safe local/function renames, comments and notes; "
                    "structural extraction is not supported by this source analyzer")
            ops = copy.deepcopy(list(session.get("structureOps") or []))
            # A rewrite label identifies a ledger entry, not Shader semantics.
            # Return the generated label for undo/remove without requiring the
            # Agent to invent or reconcile administrative identifiers.
            if operation == "rewrite" and (
                    not isinstance(name, str)
                    or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
                    or any(item.get("name") == name for item in ops)):
                name = "Rewrite_" + uuid.uuid4().hex
            comments = dict(session.get("commentMap") or {})
            renames = dict(session.get("renameMap") or {})
            proposal_path = self._session_artifact(root, session, "proposalSourcePath", session_dir, "proposal source")
            actual_proposal, actual_bytes = _utf8(proposal_path, MAX_SOURCE_BYTES, "proposal source")
            original_session_bytes = _regular_bytes(session_path, MAX_CONTEXT_BYTES, "organization session")
            timing.dimensions(
                organizationId=organization_id,
                operationId=operation_id,
                sourceByteSize=len(actual_bytes),
                sourceLineCount=actual_proposal.count("\n") + int(not actual_proposal.endswith("\n")),
                ledgerOperationCount=len(ops),
                renameCount=len(renames),
                affectedLineCount=(end_line - start_line + 1)
                if operation == "extract" and end_line >= start_line > 0 else 0,
                editCount=len(edits or []) if operation == "rewrite" else 0,
            )
            if operation == "list":
                timing.mark("staticStructureReport")
                # Listing describes the current draft. Replaying every historical
                # extraction three times adds no authority to this read-only view.
                report, report_cache_hit = self._cached_structure_report(
                    actual_proposal, str(session.get("entryPoint") or "")
                )
                timing.dimensions(structureReportCacheHit=int(report_cache_hit))
                proposal_sha = _sha256(actual_bytes)
                receipts = list(session.get("structureReceipts") or [])
                for mutation in (session.get("mutationReceipts") or {}).values():
                    if receipts:
                        break
                    saved = mutation.get("result") if isinstance(mutation, Mapping) else None
                    if isinstance(saved, Mapping) and saved.get("proposalSourceSha256") == proposal_sha:
                        saved_receipts = saved.get("receipts")
                        if isinstance(saved_receipts, list):
                            receipts = saved_receipts
                if (_regular_bytes(proposal_path, MAX_SOURCE_BYTES, "proposal source") != actual_bytes
                        or _regular_bytes(session_path, MAX_CONTEXT_BYTES, "organization session") != original_session_bytes
                        or _regular_bytes(_source_path, MAX_SOURCE_BYTES, "workspace source") != _current_bytes):
                    raise ShaderOrganizationError("organization source/session changed during listing; read again")
                return timing.attach({
                    "schema": "renderdoc-ai-shader-organization-restructure/v1",
                    "schemaVersion": 1,
                    "workspacePath": str(root), "organizationId": organization_id,
                    "operation": "list", "accepted": True,
                    "operations": len(ops), "receipts": len(receipts),
                    "lastReceipt": None, "compile": None,
                    "ledger": self._function_ledger(session, ops, receipts, report),
                    "structure": {key: report[key] for key in ("totals", "anonymity", "flagged")},
                    "proposalSourcePath": str(proposal_path),
                    "proposalSourceSha256": proposal_sha,
                    "commentsNotFound": [], "state": "proposal-unchanged",
                    "validationTier": "proposal-read-only", "completedChecks": [],
                    "draftViewPath": str(root / "ai" / ("organized." + language)),
                    "canonicalSourceUpdated": False, "replayAbiChecked": False,
                    "applied": False,
                })
            def reject(reason: str, category: str) -> dict[str, Any]:
                timing.failure_category(category)
                return timing.attach(self._restructure_rejection(
                    root, organization_id, operation, ops, reason, None
                ))
            if operation in {"extract", "rewrite", "note"}:
                # These operations are local to the current draft. Its bytes
                # and the sealed session are checked again by the atomic write;
                # replaying every earlier edit from the baseline would discard
                # a user's valid direct edits and make cost grow with history.
                proposal = actual_proposal
                receipts = list(session.get("structureReceipts") or [])
                changed = operation != "note"
                timing.mark("rangeAndAnchorAnalysis")
                if operation == "note":
                    if not str(name or "").strip() or not str(comment or "").strip():
                        raise ShaderOrganizationError("note needs name (a function) and comment (its purpose, one or two sentences)")
                    if str(name).strip() not in {
                        record["name"] for record in _function_records_for_template(actual_proposal)
                    }:
                        raise ShaderOrganizationError("note must name a function in the current proposal")
                    notes = dict(session.get("functionNotes") or {})
                    notes[str(name).strip()] = str(comment).strip()[:600]
                    session = dict(session)
                    session["functionNotes"] = notes
                else:
                    if language != "hlsl":
                        raise ShaderOrganizationError("bounded structural editing is supported only for executable HLSL organization")
                    if len(ops) >= 200:
                        raise ShaderOrganizationError("this session already holds 200 structural operations")
                    replayable_draft = bool(
                        session.get("draftRequiresCurrentSource") is not True
                        and session.get("proposalSourceSha256") == _sha256(actual_bytes)
                    )
                    operation_renames = renames if replayable_draft else {}
                    if not replayable_draft or not renames:
                        canonical_draft = actual_proposal
                    elif not ops:
                        try:
                            canonical_draft = _canonical_source(
                                actual_proposal, renames
                            )
                        except RestructureError as exc:
                            return reject(str(exc), "canonical-binding")
                    else:
                        try:
                            canonical_draft, unused_missing, unused_receipts = (
                                self._regenerate_proposal(
                                    baseline_text, {}, ops, comments
                                )
                            )
                        except ShaderOrganizationError as exc:
                            return reject(str(exc), "canonical-binding")
                    if operation == "extract":
                        if not (isinstance(start_line, int) and isinstance(end_line, int)) or start_line <= 0 or end_line < start_line:
                            raise ShaderOrganizationError("extract needs start_line <= end_line (1-based lines of the current proposal)")
                        if not str(name or "").strip():
                            raise ShaderOrganizationError("extract needs a function name")
                        edit = {
                            "operation": "extract", "startLine": int(start_line),
                            "endLine": int(end_line), "name": str(name).strip(),
                            "comment": str(comment or "").strip()[:600],
                        }
                        try:
                            edit["anchor"] = source_bound_anchor(
                                actual_proposal, start_line, end_line,
                                renames=operation_renames,
                                canonical_source=canonical_draft,
                            )
                        except RestructureError as exc:
                            return reject(str(exc), "range-or-rewrite")
                    else:
                        if _hex(expected_proposal_sha256, "expected_proposal_sha256") != _sha256(actual_bytes):
                            raise ShaderOrganizationError("proposal SHA256 is stale; re-read the current bounded source")
                        if any(item.get("name") == name for item in ops):
                            raise ShaderOrganizationError("rewrite needs a unique ledger name")
                        try:
                            edit = prepare_rewrite(
                                actual_proposal, edits, renames=operation_renames,
                                entry_point=str(session.get("entryPoint")),
                                name=name, canonical_source=canonical_draft,
                            )
                        except RestructureError as exc:
                            return reject(str(exc), "range-or-rewrite")
                    try:
                        proposal, emitted = apply_operations(
                            actual_proposal, [edit],
                            renames=operation_renames,
                            _canonical_source_override=canonical_draft,
                        )
                        edit = retain_extraction_names(
                            [edit], emitted, operation_renames
                        )[0]
                    except RestructureError as exc:
                        return reject(str(exc), "range-or-rewrite")
                    emitted[0]["position"] = len(ops)
                    ops.append(edit)
                    receipts.extend(emitted)
                timing.mark("proposalReplay")
                timing.mark("metadataSerialization")
                proposal_sha = _sha256(proposal.encode("utf-8"))
                result = {
                    "schema": "renderdoc-ai-shader-organization-restructure/v1",
                    "schemaVersion": 1, "workspacePath": str(root),
                    "organizationId": organization_id, "operation": operation,
                    "accepted": True, "operations": self._public_structure_ops(ops),
                    "receipts": receipts,
                    "lastReceipt": receipts[-1] if changed and receipts else None,
                    "compile": None,
                    "ledger": None,
                    "structure": {"available": False, "sourceSha256": proposal_sha},
                    "proposalSourcePath": str(proposal_path),
                    "proposalSourceSha256": proposal_sha,
                    "commentsNotFound": [],
                    "state": "proposal-updated" if changed else "proposal-unchanged",
                    "validationTier": "proposal-draft" if changed else "proposal-read-only",
                    "completedChecks": ["bounded-source-static-check"] if changed else [],
                    "draftViewPath": str(root / "ai" / ("organized." + language)),
                    "canonicalSourceUpdated": False, "replayAbiChecked": False,
                    "applied": False,
                }
                updated = dict(session)
                if changed:
                    updated["structureOps"] = ops
                    updated["structureReceipts"] = receipts
                    updated["proposalSourceSha256"] = proposal_sha
                    updated["draftRequiresCurrentSource"] = bool(
                        session.get("draftRequiresCurrentSource")
                        or not replayable_draft
                    )
                updated = self._record_durable_mutation(
                    updated, tool="restructure_shader_workspace_source",
                    operation_id=operation_id, request_sha256=request_sha256,
                    compatible_request_sha256=compatible_request_sha256,
                    result=result,
                )
                updated = self._seal_session(updated)
                session_bytes = (
                    json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n"
                ).encode("utf-8")
                if (len(_canonical_json(updated)) > MAX_CONTEXT_BYTES
                        or len(session_bytes) > MAX_CONTEXT_BYTES):
                    return reject("structural ledger exceeds its 2 MiB bound", "ledger-limit")
                if (_regular_bytes(proposal_path, MAX_SOURCE_BYTES, "proposal source") != actual_bytes
                        or _regular_bytes(session_path, MAX_CONTEXT_BYTES, "organization session") != original_session_bytes
                        or _regular_bytes(_source_path, MAX_SOURCE_BYTES, "workspace source") != _current_bytes):
                    raise ShaderOrganizationError("organization source/session changed during draft preparation; no rewrite committed")
                targets = {session_path: session_bytes}
                expected_before = {session_path: original_session_bytes}
                if changed:
                    targets.update(self._alias_view_payloads(
                        root, baseline_text, renames, comments, proposal, language
                    ))
                    targets[proposal_path] = proposal.encode("utf-8")
                    expected_before[proposal_path] = actual_bytes
                timing.mark("transaction")
                apply_workspace_mutation(
                    store_root=self._mutation_store,
                    workspace_root=root, organization_id=organization_id,
                    tool="restructure_shader_workspace_source",
                    operation_id=operation_id, request_sha256=request_sha256,
                    targets=targets, expected_before=expected_before,
                    checkpoint_after_apply=lambda: self._remember_mutation_session(session_dir),
                    checkpoint_after_rollback=lambda: self._remember_mutation_session(session_dir),
                )
                return timing.attach(result)
            timing.mark("existingReplay")
            old_comments = {renames.get(k, k): v for k, v in comments.items()}
            original_proposal, original_missing, original_receipts = self._regenerate_proposal(
                baseline_text, renames, ops, old_comments)
            if operation not in {"list", "note"} and actual_proposal != original_proposal:
                return reject("proposal contains edits outside the structural ledger; preserve and review it", "proposal-drift")
            if operation == "rewrite":
                if language != "hlsl":
                    raise ShaderOrganizationError("bounded rewrite is supported only for executable HLSL organization")
                if _hex(expected_proposal_sha256, "expected_proposal_sha256") != _sha256(actual_bytes):
                    raise ShaderOrganizationError("proposal SHA256 is stale; re-read the current bounded source")
            timing.mark("rangeAndAnchorAnalysis")
            if operation not in {"list", "note"}:
                try:
                    baseline_renames, unused_deferred = split_bound_renames(
                        baseline_text, renames
                    )
                    ops = anchor_extractions(
                        apply_renames(baseline_text, baseline_renames),
                        ops, baseline_renames,
                    )
                except RestructureError as exc:
                    raise ShaderOrganizationError("existing structural ledger cannot be anchored: %s" % exc) from exc
            if operation in {"extract", "rewrite"}:
                canonical_proposal, unused, unused_receipts = self._regenerate_proposal(
                    baseline_text, {}, ops, comments
                )
            changed = True
            if operation == "extract":
                if not (isinstance(start_line, int) and isinstance(end_line, int)) or start_line <= 0 or end_line < start_line:
                    raise ShaderOrganizationError("extract needs start_line <= end_line (1-based lines of the current proposal)")
                if not str(name or "").strip():
                    raise ShaderOrganizationError("extract needs a function name")
                if len(ops) >= 200:
                    raise ShaderOrganizationError("this session already holds 200 structural operations")
                ops.append({"operation": "extract", "startLine": int(start_line), "endLine": int(end_line),
                            "name": str(name).strip(), "comment": str(comment or "").strip()[:600]})
                try:
                    ops[-1]["anchor"] = source_bound_anchor(
                        actual_proposal, start_line, end_line,
                        renames=renames, canonical_source=canonical_proposal,
                    )
                except RestructureError as exc:
                    return reject(str(exc), "range-or-rewrite")
            elif operation == "rewrite":
                if len(ops) >= 200 or any(item.get("name") == name for item in ops):
                    raise ShaderOrganizationError("rewrite needs a unique ledger name and fewer than 200 operations")
                try:
                    ops.append(prepare_rewrite(actual_proposal, edits, renames=renames,
                                               entry_point=str(session.get("entryPoint")), name=name,
                                               canonical_source=canonical_proposal))
                except RestructureError as exc:
                    return reject(str(exc), "range-or-rewrite")
            elif operation == "undo":
                if not ops:
                    raise ShaderOrganizationError("nothing to undo")
                removed = ops.pop()
                comments.pop(str(removed.get("name", "")), None)
            elif operation == "remove":
                index = next((i for i, item in enumerate(ops) if item.get("name") == name), None)
                if index is None:
                    raise ShaderOrganizationError("no structural operation created %r" % name)
                ops.pop(index)
                comments.pop(str(name), None)
            elif operation == "rename_function":
                index = next((i for i, item in enumerate(ops) if item.get("operation") == "extract" and item.get("name") == name), None)
                if index is None:
                    raise ShaderOrganizationError("no extracted function named %r" % name)
                if not str(new_name or "").strip():
                    raise ShaderOrganizationError("rename_function needs new_name")
                ops[index]["name"] = str(new_name).strip()
                # Later canonical anchors may name/call this helper. Update
                # those exact tokens together, never locate by stale lines.
                for later in ops[index + 1:]:
                    anchors = ([later["anchor"]] if "anchor" in later else []) + list(later.get("edits") or [])
                    for anchor in anchors:
                        if anchor.get("scope") == name:
                            anchor["scope"] = str(new_name).strip()
                        for field in ("before", "after"):
                            if field in anchor:
                                anchor[field] = apply_renames(anchor[field], {name: str(new_name).strip()})
                if name in comments:
                    comments[str(new_name).strip()] = comments.pop(name)
                if str(comment or "").strip():
                    ops[index]["comment"] = str(comment).strip()[:600]
            elif operation == "note":
                if not str(name or "").strip() or not str(comment or "").strip():
                    raise ShaderOrganizationError("note needs name (a function) and comment (its purpose, one or two sentences)")
                notes = dict(session.get("functionNotes") or {})
                notes[str(name).strip()] = str(comment).strip()[:600]
                session = dict(session)
                session["functionNotes"] = notes
                changed = False
            elif operation == "list":
                changed = False
            else:
                raise ShaderOrganizationError("operation must be one of: extract, rewrite, undo, remove, rename_function, note, list")

            if operation in {"undo", "remove"}:
                try:
                    canonical, unused, unused_receipts = self._regenerate_proposal(
                        baseline_text, {}, ops, {}
                    )
                except ShaderOrganizationError as exc:
                    return reject(str(exc), "structural-replay")
                from .flow_graph import _tokens
                retained = {token.text for token in _tokens(baseline_text + "\n" + canonical)}
                def selector_retained(selector: str) -> bool:
                    selector = str(selector)
                    if "::" in selector:
                        owner, leaf = selector.split("::", 1)
                        return owner in retained and leaf in retained
                    if "." in selector:
                        owner, leaf = selector.split(".", 1)
                        return owner in retained and leaf in retained
                    return selector in retained
                renames = {key: value for key, value in renames.items()
                           if selector_retained(key)}
            renamed_comments = {renames.get(k, k): v for k, v in comments.items()}
            proposal_path = self._session_artifact(
                root, session, "proposalSourcePath", session_dir, "proposal source")
            actual_proposal, unused_proposal_bytes = _utf8(proposal_path, MAX_SOURCE_BYTES, "proposal source")
            timing.mark("proposalReplay")
            if operation == "note":
                # A note changes only the sealed function ledger. The complete
                # proposal was already regenerated and checked above; replaying
                # it twice more cannot strengthen this metadata-only edit.
                proposal, missing, receipts = (
                    original_proposal, original_missing, original_receipts
                )
            else:
                try:
                    proposal, missing, receipts = self._regenerate_proposal(baseline_text, renames, ops, renamed_comments)
                    if changed:
                        ops = retain_extraction_names(ops, receipts, renames)
                except ShaderOrganizationError as exc:
                    return reject(str(exc), "structural-replay")
            if not changed:
                proposal = actual_proposal
                if operation == "note" and str(name).strip() not in {
                        record["name"] for record in _function_records_for_template(proposal)}:
                    raise ShaderOrganizationError("note must name a function in the current proposal")
            # Structural source checks above reject invalid ranges, anchors,
            # ownership and protected interfaces. A draft may still have an
            # unresolved call/type error; compiling every multi-megabyte draft
            # after each local edit is quadratic in a long organization task.
            # Finalization compiles this exact SHA and checks replay ABI and
            # equivalence before canonical publication.
            compile_result: dict[str, Any] | None = None
            timing.mark("staticStructureReport")
            report, report_cache_hit = self._cached_structure_report(
                proposal, str(session.get("entryPoint") or "")
            )
            timing.dimensions(structureReportCacheHit=int(report_cache_hit))
            ledger = self._function_ledger(session, ops, receipts, report)
            timing.mark("metadataSerialization")
            result = {
                "schema": "renderdoc-ai-shader-organization-restructure/v1",
                "schemaVersion": 1,
                "workspacePath": str(root),
                "organizationId": organization_id,
                "operation": operation,
                "accepted": True,
                "operations": (
                    self._public_structure_ops(ops)
                    if operation != "list" else len(ops)
                ),
                "receipts": receipts if operation != "list" else len(receipts),
                "lastReceipt": (
                    receipts[-1]
                    if receipts and operation in {"extract", "rewrite"} else None
                ),
                "compile": compile_result,
                "ledger": ledger,
                "structure": (
                    report if operation != "list" else {
                        "totals": report["totals"],
                        "anonymity": report["anonymity"],
                        "flagged": report["flagged"],
                    }
                ),
                "proposalSourcePath": str(root / str(session["proposalSourcePath"])),
                "proposalSourceSha256": _sha256(proposal.encode("utf-8")),
                "commentsNotFound": missing,
                "state": "proposal-updated" if changed else "proposal-unchanged",
                "validationTier": (
                    "proposal-draft" if changed else "proposal-read-only"
                ),
                "completedChecks": (
                    ["bounded-source-static-check"] if changed else []
                ),
                "draftViewPath": str(
                    root / "ai" / ("organized." + language)
                ),
                "canonicalSourceUpdated": False,
                "replayAbiChecked": False,
                "applied": False,
            }
            if changed:
                proposal_path = self._session_artifact(
                    root, session, "proposalSourcePath", session_dir, "proposal source"
                )
                updated = dict(session)
                updated["structureOps"] = ops
                updated["renameMap"] = renames
                updated["commentMap"] = comments
                updated["structureReceipts"] = receipts
                updated["proposalSourceSha256"] = _sha256(proposal.encode("utf-8"))
                updated = self._record_durable_mutation(
                    updated,
                    tool="restructure_shader_workspace_source",
                    operation_id=operation_id,
                    request_sha256=request_sha256,
                    compatible_request_sha256=compatible_request_sha256,
                    result=result,
                )
                updated = self._seal_session(updated)
                session_bytes = (
                    json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n"
                ).encode("utf-8")
                if (
                    len(_canonical_json(updated)) > MAX_CONTEXT_BYTES
                    or len(session_bytes) > MAX_CONTEXT_BYTES
                ):
                    return reject("structural ledger exceeds its 2 MiB bound", "ledger-limit")
                if (_regular_bytes(proposal_path, MAX_SOURCE_BYTES, "proposal source") != actual_bytes
                        or _regular_bytes(session_path, MAX_CONTEXT_BYTES, "organization session") != original_session_bytes
                        or _regular_bytes(_source_path, MAX_SOURCE_BYTES, "workspace source") != _current_bytes):
                    raise ShaderOrganizationError("organization source/session changed during draft preparation; no rewrite committed")
                targets = self._alias_view_payloads(
                    root, baseline_text, renames, comments, proposal, language
                )
                targets[proposal_path] = proposal.encode("utf-8")
                targets[session_path] = session_bytes
                timing.mark("transaction")
                apply_workspace_mutation(
                    store_root=self._mutation_store,
                    workspace_root=root,
                    organization_id=organization_id,
                    tool="restructure_shader_workspace_source",
                    operation_id=operation_id,
                    request_sha256=request_sha256,
                    targets=targets,
                    expected_before={
                        proposal_path: actual_bytes,
                        session_path: original_session_bytes,
                    },
                    checkpoint_after_apply=lambda: self._remember_mutation_session(
                        session_dir
                    ),
                    checkpoint_after_rollback=lambda: self._remember_mutation_session(
                        session_dir
                    ),
                )
            if operation == "note":
                updated = self._record_durable_mutation(
                    session,
                    tool="restructure_shader_workspace_source",
                    operation_id=operation_id,
                    request_sha256=request_sha256,
                    compatible_request_sha256=compatible_request_sha256,
                    result=result,
                )
                updated = self._seal_session(updated)
                session_bytes = (
                    json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n"
                ).encode("utf-8")
                if (
                    len(_canonical_json(updated)) > MAX_CONTEXT_BYTES
                    or len(session_bytes) > MAX_CONTEXT_BYTES
                ):
                    raise ShaderOrganizationError(
                        "structural ledger exceeds its 2 MiB bound"
                    )
                if _regular_bytes(
                    _source_path, MAX_SOURCE_BYTES, "workspace source"
                ) != _current_bytes:
                    raise ShaderOrganizationError(
                        "organization source changed before note transaction"
                    )
                timing.mark("transaction")
                apply_workspace_mutation(
                    store_root=self._mutation_store,
                    workspace_root=root,
                    organization_id=organization_id,
                    tool="restructure_shader_workspace_source",
                    operation_id=operation_id,
                    request_sha256=request_sha256,
                    targets={session_path: session_bytes},
                    expected_before={session_path: original_session_bytes},
                    checkpoint_after_apply=lambda: self._remember_mutation_session(
                        session_dir
                    ),
                    checkpoint_after_rollback=lambda: self._remember_mutation_session(
                        session_dir
                    ),
                )
            return timing.attach(result)

    @staticmethod
    def _public_structure_ops(ops: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for item in ops:
            public = {k: v for k, v in item.items() if k not in {"anchor", "edits", "generatedNames"}}
            if item.get("operation") == "rewrite":
                public["editCount"] = len(item.get("edits") or [])
                public["sourceRanges"] = [edit.get("sourceLines") for edit in item.get("edits") or []]
            result.append(public)
        return result

    @staticmethod
    def _function_ledger(
        session: Mapping[str, Any],
        ops: Sequence[Mapping[str, Any]],
        receipts: Sequence[Mapping[str, Any]],
        report: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """The compact working memory for the consolidation pass.

        One row per function: Host facts (lines, interface, globals, flags,
        where it came from) plus the AI's own purpose note.  Deciding merges
        and splits from this table costs a few hundred tokens instead of
        re-reading code.
        """

        notes = session.get("functionNotes") if isinstance(session.get("functionNotes"), Mapping) else {}
        comments = session.get("commentMap") if isinstance(session.get("commentMap"), Mapping) else {}
        accepted_origins = (
            session.get("acceptedFunctionOrigins")
            if isinstance(session.get("acceptedFunctionOrigins"), Mapping)
            else {}
        )
        by_name = {str(item.get("name")): item for item in receipts if item.get("operation") == "extract"}
        rename_map = dict(session.get("renameMap") or {})
        rewritten = {rename_map.get(str(edit.get("scope")), str(edit.get("scope")))
                     for operation in ops if operation.get("operation") == "rewrite"
                     for edit in operation.get("edits") or []}
        op_comments = {str(item.get("name")): str(item.get("comment") or "") for item in ops}
        rows = []
        for item in report.get("functions", []):
            name = item["name"]
            receipt = by_name.get(name)
            accepted_origin = accepted_origins.get(name)
            accepted_origin = (
                accepted_origin if isinstance(accepted_origin, Mapping) else {}
            )
            if name in rewritten:
                # An internal signature can change atomically. The old extract
                # receipt is historical evidence, not the current interface.
                receipt = None
            rows.append({
                "name": name,
                "purpose": str(notes.get(name) or comments.get(name) or op_comments.get(name) or ""),
                "lines": item["lines"],
                "isEntry": item.get("isEntry", False),
                "origin": ("extracted from %s lines %s" % (receipt["enclosingFunction"], receipt["sourceLines"])
                           if receipt else "rewritten" if name in rewritten
                           else str(accepted_origin.get("origin") or "original")),
                "inputs": (receipt["inputs"] if receipt else
                           copy.deepcopy(accepted_origin.get("inputs"))
                           if isinstance(accepted_origin.get("inputs"), list)
                           else item.get("parameters", [])),
                "inouts": (receipt["inouts"] if receipt else
                            copy.deepcopy(accepted_origin.get("inouts"))
                            if isinstance(accepted_origin.get("inouts"), list)
                            else []),
                "outputs": (
                    ([receipt["returns"]] if receipt and receipt.get("returns") else [])
                    + (receipt["outParams"] if receipt else [])
                    if receipt
                    else copy.deepcopy(accepted_origin.get("outputs"))
                    if isinstance(accepted_origin.get("outputs"), list)
                    else []
                ),
                "calls": item.get("calls", []),
                "callers": item.get("callers", []),
                "globalsUsed": item.get("globalsUsed", []),
                "anonymousLocals": item.get("anonymousLocals", 0),
                "flags": item.get("flags", []),
                "status": "reviewed" if (name in notes and not item.get("flags")) else ("noted" if name in notes else "provisional"),
            })
        return rows

    @staticmethod
    @staticmethod
    def _restructure_rejection(root: Path, organization_id: str, operation: str, ops: list[dict[str, Any]],
                               reason: str, compile_result: Mapping[str, Any] | None) -> dict[str, Any]:
        return {
            "schema": "renderdoc-ai-shader-organization-restructure/v1",
            "schemaVersion": 1,
            "workspacePath": str(root),
            "organizationId": organization_id,
            "operation": operation,
            "accepted": False,
            "reason": reason,
            "compile": dict(compile_result) if compile_result else None,
            "attemptedOperations": ShaderOrganizationManager._public_structure_ops(ops),
            "counted": False,
            "state": "rejected-no-mutation",
            "applied": False,
        }

    @classmethod
    def _stage_cbuffer_aliases(
        cls,
        baseline_text: str,
        renames: Mapping[str, str],
        captured_identity: Mapping[str, Any] | None,
        alias_root: Path,
        alias_store_kind: str,
        alias_user_root: Path,
    ) -> tuple[dict[str, Any], dict[Path, bytes], dict[Path, bytes | None]]:
        raise ShaderOrganizationError(
            "source rename cannot publish CBuffer panel aliases; use the explicit "
            "publish_cbuffer_aliases tool with current physical binding"
        )

    @classmethod
    def _stage_cbuffer_aliases_locked(
        cls,
        baseline_text: str,
        renames: Mapping[str, str],
        captured_identity: Mapping[str, Any] | None,
        alias_root: Path,
        alias_store_kind: str,
        alias_user_root: Path,
    ) -> tuple[dict[str, Any], dict[Path, bytes], dict[Path, bytes | None]]:
        """Build shared alias-store after-images without changing the live store.

        The live-store snapshot is taken under its store lock.  The later
        workspace transaction reacquires that lock and compares the exact
        expected-before bytes before publishing any staged after-image.
        """

        raise ShaderOrganizationError(
            "source rename cannot stage CBuffer panel aliases; use the explicit "
            "publish_cbuffer_aliases tool with current physical binding"
        )

        publications = cbuffer_aliases.aliases_from_renames(
            baseline_text, dict(renames), captured_identity
        )
        with tempfile.TemporaryDirectory(prefix="bread-organization-alias-stage-") as temporary:
            stage_root = Path(temporary)
            keys: list[str] = []
            before_targets: dict[Path, bytes | None] = {}
            lock_roots = (
                [alias_root, alias_user_root]
                if alias_store_kind == "project"
                else [alias_root]
            )
            with cbuffer_aliases.alias_store_transactions(lock_roots):
                for publication in publications:
                    key = str(publication.get("layoutSha256") or "")
                    if not key or key in keys:
                        continue
                    # Preserve project-store adoption semantics before taking
                    # the exact transaction snapshot.
                    cbuffer_aliases._adopt_user_file_at(key, alias_root)
                    live_path = cbuffer_aliases.layout_alias_path(key, alias_root)
                    if live_path.exists():
                        content = _regular_bytes(
                            live_path,
                            cbuffer_aliases.MAX_FILE_BYTES,
                            "CBuffer alias transaction source",
                        )
                        atomic_write_bytes(
                            cbuffer_aliases.layout_alias_path(key, stage_root), content
                        )
                        before_targets[live_path] = content
                    else:
                        before_targets[live_path] = None
                    keys.append(key)

            report = cls._publish_cbuffer_aliases(
                baseline_text,
                renames,
                captured_identity,
                publication_root=stage_root,
                reported_root=alias_root,
                reported_store_kind=alias_store_kind,
            )
            targets: dict[Path, bytes] = {}
            for key in keys:
                staged_path = cbuffer_aliases.layout_alias_path(key, stage_root)
                if staged_path.exists():
                    targets[cbuffer_aliases.layout_alias_path(key, alias_root)] = (
                        _regular_bytes(
                            staged_path,
                            cbuffer_aliases.MAX_FILE_BYTES,
                            "staged CBuffer alias transaction target",
                        )
                    )
            return report, targets, {
                path: before_targets[path] for path in targets
            }

    @staticmethod
    def _publish_cbuffer_aliases(
        baseline_text: str, renames: Mapping[str, str],
        captured_identity: Mapping[str, Any] | None = None,
        *,
        publication_root: Path | None = None,
        reported_root: Path | None = None,
        reported_store_kind: str | None = None,
    ) -> dict[str, Any]:
        """Reject the removed implicit source-rename publication path.

        Panel aliases are an independent explicit mutation with stronger
        physical binding checks. Keeping this private method as
        a fail-closed compatibility shim prevents old extensions/tests from
        silently recreating the removed side effect.
        """

        raise ShaderOrganizationError(
            "source rename cannot publish CBuffer panel aliases; use the explicit "
            "publish_cbuffer_aliases tool with current physical binding"
        )

        blocks: list[dict[str, Any]] = []
        published = 0
        kept_user: list[str] = []
        member_renames: list[dict[str, Any]] = []
        for publication in cbuffer_aliases.aliases_from_renames(baseline_text, dict(renames), captured_identity):
            record: dict[str, Any] = {
                "block": publication["block"], "layoutSha256": publication["layoutSha256"] or None,
                "sourceBlock": publication.get("sourceBlock"),
                "bind": publication.get("bind"), "space": publication.get("space"),
                "capturedIdentityMatched": publication.get("capturedIdentityMatched", False),
                "unmapped": publication.get("unmapped", []),
                "memberMappingKind": publication.get("memberMappingKind", ""),
            }
            if not publication["layoutSha256"]:
                record["error"] = "layout incomplete: member offsets unknown, aliases not published"
                blocks.append(record)
                continue
            try:
                if publication_root is None:
                    report = cbuffer_aliases.publish_aliases(
                        publication["block"],
                        publication["members"],
                        publication["entries"],
                        source="ai",
                        block_alias=publication.get("blockAlias"),
                    )
                else:
                    report = cbuffer_aliases._publish_aliases_unlocked(
                        publication["block"],
                        publication["members"],
                        publication["entries"],
                        source="ai",
                        block_alias=publication.get("blockAlias"),
                        root=publication_root,
                        store_kind=str(reported_store_kind or "user"),
                    )
            except (OSError, ValueError) as exc:
                record["error"] = str(exc)
                blocks.append(record)
                continue
            published += len(report["written"]) + int(report["blockAliasWritten"])
            kept_user.extend(report["keptUser"])
            if report["blockAliasKeptUser"]:
                kept_user.append(publication["block"])
            report_path = report["path"]
            if reported_root is not None:
                report_path = str(
                    cbuffer_aliases.layout_alias_path(
                        str(report.get("layoutSha256") or ""), reported_root
                    )
                )
            record.update({
                "written": report["written"],
                "keptUser": report["keptUser"],
                "blockAlias": report["blockAlias"],
                "blockAliasWritten": report["blockAliasWritten"],
                "blockAliasKeptUser": report["blockAliasKeptUser"],
                "rejected": report["rejected"],
                "path": report_path,
                "store": report.get("store", "user"),
                "applied": report["applied"],
                **({"reasonId": report["reasonId"]} if "reasonId" in report else {}),
            })
            mappings: list[dict[str, Any]] = []
            for entry in publication.get("entries", []):
                qualified_path = "%s.%s" % (publication["block"], entry.get("path"))
                state = ("written" if qualified_path in report["written"] else
                         "kept-user" if qualified_path in report["keptUser"] else
                         "rejected" if any(item.get("path") == qualified_path
                                           for item in report["rejected"]) else "not-written")
                mapping = {
                    "space": publication.get("space"), "bind": publication.get("bind"),
                    "byteOffset": int(entry.get("offset", -1)),
                    "byteSize": int(entry.get("byteSize", 0)),
                    "name": str(entry.get("alias") or ""),
                    "original": str(entry.get("original") or entry.get("path")),
                    "reflectedPath": str(entry.get("path") or ""),
                    "block": publication["block"], "aliasState": state,
                }
                mappings.append(mapping)
                # Only an AI alias actually written by this call can seed
                # automatic material-family rules. A retained user name is
                # authoritative display state, not confirmation of the AI name.
                if (state == "written" and mapping["byteOffset"] >= 0
                        and mapping["byteSize"] > 0):
                    member_renames.append(mapping)
            record["memberMappings"] = mappings
            record["memberMappingCount"] = len(mappings)
            record["memberMappingsTruncated"] = False
            blocks.append(record)
        return {"published": published, "keptUser": kept_user, "blocks": blocks,
                # This is an internal Host ledger consumed immediately by
                # _remember_member_rules. Truncating it made every cumulative
                # rename call return the same first 512 entries, so later
                # batches could never seed the tail of a material family.
                "memberRenames": member_renames,
                "memberRenameCount": len(member_renames),
                "memberRenamesTruncated": False,
                "storeRoot": str(reported_root or publication_root or cbuffer_aliases.store_root())}

    def validate_publication_gate(
        self,
        workspace_path: str,
        status: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return one trusted organize-only publication gate.

        The root and per-session gate files live below the AI-editable tree and
        are therefore consistency records, not authority by themselves.  The
        completed session must match this process's seal or an exact completed
        review retained by the Host outside the workspace. Callers which use this
        result as a publication precondition must hold the workspace source
        transaction lease across this validation and their publication commit.
        This method deliberately does not acquire ``self._guard``: begin/review
        take that guard before the same source lease, so doing the reverse here
        would create a lock-order cycle.  The HMAC key is immutable after init.
        """

        service = self._service_factory()
        if status is None:
            observed_status = service.status(workspace_path)
        elif isinstance(status, Mapping):
            observed_status = status
        else:
            raise ShaderOrganizationError(
                "organization publication status is invalid"
            )

        root, source_path, source_bytes, source_sha = self._workspace_source(
            observed_status
        )
        if Path(workspace_path).expanduser().resolve() != root:
            raise ShaderOrganizationError(
                "organization publication workspace identity changed"
            )

        ai_dir = _safe_dir(root / "ai", root, "organization AI directory", create=False)
        sessions_dir = _safe_dir(
            ai_dir / "organization-sessions",
            ai_dir,
            "organization sessions directory",
            create=False,
        )
        root_gate = read_json_object(
            ai_dir / "organize-code-gate.json",
            MAX_CONTEXT_BYTES,
            "organization publication root gate",
        )
        if (
            root_gate.get("schema") != ORGANIZATION_GATE_SCHEMA
            or root_gate.get("schemaVersion") != 1
            or root_gate.get("policyVersion") != ORGANIZATION_POLICY_VERSION
        ):
            raise ShaderOrganizationError(
                "organization publication gate schema is invalid"
            )

        organization_id = _hex(
            root_gate.get("organizationId"),
            "organization publication ID",
            32,
        )
        session_dir = _safe_dir(
            sessions_dir / organization_id,
            sessions_dir,
            "organization publication session directory",
            create=False,
        )
        session = _read_session_file(session_dir, "organization publication session")
        self._validate_session(
            session, root, organization_id, observed_status
        )
        if (
            session.get("state") != "completed"
            or session.get("decision") != "accepted-executable-editable"
            or session.get("organizedSourcePromoted") is not True
            or session.get("finishedCurrentDraft") is not True
        ):
            raise ShaderOrganizationError(
                "organization publication session lacks a finalized source"
            )

        session_gate = read_json_object(
            session_dir / "gate.json",
            MAX_CONTEXT_BYTES,
            "organization publication session gate",
        )
        if root_gate != session_gate:
            raise ShaderOrganizationError(
                "organization root and session publication gates differ"
            )

        binding = root_gate.get("finalBinding")
        sealed_binding = session.get("finalBinding")
        binding_fields = {
            "complete",
            "sourceSha256",
            "revision",
            "candidateRawSha256",
            "codeSha256",
            "abiRequestId",
            "abiReportSha256",
        }
        if isinstance(binding, Mapping) and "sourceDependenciesDigest" in binding:
            _hex(binding["sourceDependenciesDigest"], "publication include dependency digest")
            binding_fields.add("sourceDependenciesDigest")
        if (
            not isinstance(binding, Mapping)
            or not isinstance(sealed_binding, Mapping)
            or set(binding) != binding_fields
            or set(sealed_binding) != binding_fields
            or dict(binding) != dict(sealed_binding)
            or binding.get("complete") is not True
        ):
            raise ShaderOrganizationError(
                "organization publication final binding is invalid"
            )

        if (
            root_gate.get("requestedTransformation") != "organize-only"
            or root_gate.get("decision") != session.get("decision")
            or root_gate.get("organizedSourcePromoted") is not True
            or root_gate.get("baselineCompiledAndAbiValidated") is not True
            or root_gate.get("organizedCompiledAndAbiValidated") is not True
            or root_gate.get("rawRecoveryCompiledAndAbiValidated") is not False
            or root_gate.get("semanticEquivalenceVerified") is not False
            or root_gate.get("replayApplied") is not False
            or root_gate.get("safeToStartLiveEdit") is not True
            or root_gate.get("publicationContract") != "validated-source"
            or root_gate.get("finishedCurrentDraft") is not True
        ):
            raise ShaderOrganizationError(
                "organization publication gate lacks validated source evidence"
            )

        sealed_pairs = (
            ("organizationId", "organizationId"),
            ("workspaceId", "workspaceId"),
            ("captureId", "captureId"),
            ("stage", "stage"),
            ("entryPoint", "entryPoint"),
            ("baselineSourceSha256", "baselineSourceSha256"),
            ("proposalSourceSha256", "proposalSourceSha256"),
            ("baselineCodeSha256", "baselineCodeSha256"),
            ("organizedCodeSha256", "organizedCodeSha256"),
            ("codeSha256Equal", "codeSha256Equal"),
            ("equivalenceLevel", "equivalenceLevel"),
            ("observation", "observation"),
            ("verdict", "verdict"),
            ("renameMap", "renameMap"),
            ("renameProvenance", "renameProvenance"),
            ("decision", "decision"),
            ("organizedSourcePromoted", "organizedSourcePromoted"),
            ("reviewedAt", "completedAt"),
            ("flowGraphId", "flowGraphId"),
            ("flowGraphSha256", "flowGraphSha256"),
            ("finishedCurrentDraft", "finishedCurrentDraft"),
            ("publicationContract", "publicationContract"),
        )
        if any(
            root_gate.get(gate_key) != session.get(session_key)
            for gate_key, session_key in sealed_pairs
        ):
            raise ShaderOrganizationError(
                "organization publication gate differs from its sealed session"
            )
        baseline_source_sha = _hex(
            session.get("baselineSourceSha256"),
            "organization baseline source SHA-256",
        )
        proposal_source_sha = _hex(
            session.get("proposalSourceSha256"),
            "organization proposal source SHA-256",
        )
        baseline_code_sha = _hex(
            session.get("baselineCodeSha256"),
            "organization baseline code SHA-256",
        )
        organized_code_sha = _hex(
            session.get("organizedCodeSha256"),
            "organization organized code SHA-256",
        )
        final_source_sha = _hex(
            binding.get("sourceSha256"),
            "organization final source SHA-256",
        )
        final_candidate_sha = _hex(
            binding.get("candidateRawSha256"),
            "organization final candidate SHA-256",
        )
        final_code_sha = _hex(
            binding.get("codeSha256"),
            "organization final code SHA-256",
        )
        final_request_id = _hex(
            binding.get("abiRequestId"),
            "organization final ABI request ID",
        )
        final_report_sha = _hex(
            binding.get("abiReportSha256"),
            "organization final ABI report SHA-256",
        )
        final_revision = _positive_int(
            binding.get("revision"), "organization final revision"
        )
        graph_bound_to_receipt = bool(root_gate.get("flowGraphId") or root_gate.get("flowGraphSha256"))
        if graph_bound_to_receipt:
            flow_graph_id = _hex(
                root_gate.get("flowGraphId"),
                "organization Shader flow graph ID",
                32,
            )
            flow_graph_sha256 = _hex(
                root_gate.get("flowGraphSha256"),
                "organization Shader flow graph SHA-256",
            )

        baseline_path = self._session_artifact(
            root,
            session,
            "baselineSourcePath",
            session_dir,
            "organization publication baseline source",
        )
        proposal_path = self._session_artifact(
            root,
            session,
            "proposalSourcePath",
            session_dir,
            "organization publication proposal source",
        )
        baseline_bytes = _regular_bytes(
            baseline_path,
            MAX_SOURCE_BYTES,
            "organization publication baseline source",
        )
        proposal_bytes = _regular_bytes(
            proposal_path,
            MAX_SOURCE_BYTES,
            "organization publication proposal source",
        )
        declared_source_path = Path(
            str(root_gate.get("executableEditableSourcePath", ""))
        )
        declared_proposal_path = Path(
            str(root_gate.get("proposalSourcePath", ""))
        )
        if not declared_source_path.is_absolute():
            declared_source_path = root / declared_source_path
        if not declared_proposal_path.is_absolute():
            declared_proposal_path = root / declared_proposal_path
        if (
            declared_source_path.is_symlink()
            or declared_proposal_path.is_symlink()
            or declared_source_path.resolve() != source_path
            or declared_proposal_path.resolve() != proposal_path
            or _sha256(baseline_bytes) != baseline_source_sha
            or _sha256(proposal_bytes) != proposal_source_sha
            or proposal_bytes != source_bytes
        ):
            raise ShaderOrganizationError(
                "organization publication source evidence changed"
            )

        # Code hashes identify evidence. Only the fast exact-code path needs
        # equality; rename proof and Host measurements authorize other paths.
        code_equal = baseline_code_sha == organized_code_sha
        equivalence_level = root_gate.get("equivalenceLevel")
        if (
            root_gate.get("codeSha256Equal") is not code_equal
            or equivalence_level not in {"exact-code", "rename-only", "observational-pass"}
            or (equivalence_level == "exact-code") is not code_equal
        ):
            raise ShaderOrganizationError(
                "organization publication equivalence level contradicts its code evidence"
            )
        if equivalence_level == "rename-only":
            try:
                baseline_text = baseline_bytes.decode("utf-8", errors="strict")
                proposal_text = proposal_bytes.decode("utf-8", errors="strict")
                renames = validate_renames(baseline_text, session.get("renameMap"),
                                           limit=MAX_RENAMES_TOTAL)
                equivalent = rename_only_equivalent(baseline_text, proposal_text, renames)
            except (EquivalenceError, UnicodeDecodeError) as exc:
                raise ShaderOrganizationError(
                    "organization publication rename-only evidence is invalid"
                ) from exc
            if equivalent.get("equal") is not True:
                raise ShaderOrganizationError(
                    "organization publication rename-only source proof failed"
                )
        if equivalence_level == "observational-pass":
            observation = self._validate_observation(
                session.get("observation"),
                baseline_source_sha=baseline_source_sha,
                candidate_source_sha=proposal_source_sha,
                baseline_candidate_sha=root_gate.get("baselineReviewCandidateRawSha256"),
                candidate_sha=final_candidate_sha,
            )
            if observation.get("accepted") is not True:
                raise ShaderOrganizationError(
                    "organization publication lacks accepted Host output comparison"
                )
        verdict = session.get("verdict")
        if not isinstance(verdict, Mapping) or verdict.get("accepted") is not True:
            raise ShaderOrganizationError("organization publication verdict is inconsistent")

        if graph_bound_to_receipt:
            declared_flow_graph_path = Path(
                str(root_gate.get("flowGraphArtifactPath", ""))
            )
            if not declared_flow_graph_path.is_absolute():
                declared_flow_graph_path = root / declared_flow_graph_path
            root_flow_graph_path = root / "ai" / "flow-graph.json"
            session_flow_graph_path = self._session_artifact(
                root,
                session,
                "structuredFlowGraphPath",
                session_dir,
                "organization publication Shader flow graph",
            )
            if (
                declared_flow_graph_path.is_symlink()
                or declared_flow_graph_path.resolve() != root_flow_graph_path.resolve()
                or root_flow_graph_path.is_symlink()
                or not is_within(root, root_flow_graph_path.resolve())
            ):
                raise ShaderOrganizationError(
                    "organization publication Shader flow graph path is invalid"
                )
            root_flow_graph_bytes = _regular_bytes(
                root_flow_graph_path,
                MAX_FLOW_GRAPH_BYTES,
                "organization publication Shader flow graph",
            )
            session_flow_graph_bytes = _regular_bytes(
                session_flow_graph_path,
                MAX_FLOW_GRAPH_BYTES,
                "organization session Shader flow graph",
            )
            if (
                root_flow_graph_bytes != session_flow_graph_bytes
                or _sha256(root_flow_graph_bytes) != flow_graph_sha256
            ):
                raise ShaderOrganizationError(
                    "organization publication Shader flow graph content changed"
                )
            try:
                flow_graph_value = read_json_object(
                    root_flow_graph_path,
                    MAX_FLOW_GRAPH_BYTES,
                    "organization publication Shader flow graph",
                )
                validated_flow_graph = validate_shader_flow_graph(
                    flow_graph_value,
                    expected_workspace_id=str(session["workspaceId"]),
                    expected_source_sha256=final_source_sha,
                    expected_source_revision=final_revision,
                    expected_source_path=str(session["sourcePath"]),
                )
            except (ValueError, ShaderFlowGraphError) as exc:
                raise ShaderOrganizationError(
                    "organization publication Shader flow graph is invalid: %s" % exc
                ) from exc
            if validated_flow_graph["graphId"] != flow_graph_id:
                raise ShaderOrganizationError(
                    "organization publication Shader flow graph ID changed"
                )

        latest = observed_status.get("latestCompiled")
        abi_gate = latest.get("abiGate") if isinstance(latest, Mapping) else None
        if (
            not isinstance(latest, Mapping)
            or not isinstance(abi_gate, Mapping)
            or latest.get("replayAbiValidated") is not True
            or latest.get("abiCompatible") is not True
            or latest.get("requiresReplayAbiValidation") is not False
            or latest.get("validationScope")
            != "replay-reflection-abi-compatible"
            or latest.get("semanticEquivalenceVerified") is not False
            or observed_status.get("replayAbiValidated") is not True
            or observed_status.get("abiCompatible") is not True
            or abi_gate.get("status") != "compatible"
        ):
            raise ShaderOrganizationError(
                "organization publication candidate lacks current replay ABI validation"
            )

        latest_source_sha = _hex(
            latest.get("sourceSha256"),
            "organization publication candidate source SHA-256",
        )
        latest_candidate_sha = _hex(
            latest.get("rawSha256"),
            "organization publication candidate SHA-256",
        )
        latest_code_sha = _hex(
            latest.get("codeSha256"),
            "organization publication candidate code SHA-256",
        )
        latest_request_id = _hex(
            abi_gate.get("requestId"),
            "organization publication ABI request ID",
        )
        latest_report_sha = _hex(
            abi_gate.get("reportSha256"),
            "organization publication ABI report SHA-256",
        )
        _hex(
            abi_gate.get("baselineAbiSha256"),
            "organization publication baseline ABI SHA-256",
        )
        _hex(
            abi_gate.get("candidateAbiSha256"),
            "organization publication candidate ABI SHA-256",
        )
        _positive_int(
            root_gate.get("baselineReviewRevision"),
            "organization baseline review revision",
        )
        for field, label in (
            (
                "baselineReviewCandidateRawSha256",
                "organization baseline review candidate SHA-256",
            ),
            (
                "baselineReviewAbiRequestId",
                "organization baseline review ABI request ID",
            ),
            (
                "baselineReviewAbiReportSha256",
                "organization baseline review ABI report SHA-256",
            ),
        ):
            _hex(root_gate.get(field), label)

        gate_binding_projection = {
            "complete": True,
            "sourceSha256": root_gate.get("finalSourceSha256"),
            "revision": root_gate.get("finalRevision"),
            "candidateRawSha256": root_gate.get("finalCandidateRawSha256"),
            "codeSha256": root_gate.get("finalCodeSha256"),
            "abiRequestId": root_gate.get("finalAbiRequestId"),
            "abiReportSha256": root_gate.get("finalAbiReportSha256"),
        }
        if (
            gate_binding_projection != dict(binding)
            or proposal_source_sha != final_source_sha
            or final_source_sha != source_sha
            or latest_source_sha != source_sha
            or organized_code_sha != final_code_sha
            or latest_code_sha != final_code_sha
            or observed_status.get("revision") != final_revision
            or latest.get("revision") != final_revision
            or latest_candidate_sha != final_candidate_sha
            or latest_request_id != final_request_id
            or latest_report_sha != final_report_sha
        ):
            raise ShaderOrganizationError(
                "organization publication gate is stale or internally disconnected"
            )

        # Re-resolve status through the fixed workspace service immediately
        # before returning.  With the caller-owned source lease this is the
        # final coherent current-source/candidate/ABI publication witness.
        self._require_final_binding(service, root, session, binding)
        return dict(root_gate)

    @staticmethod
    def _validate_observation(
        value: Any,
        *,
        baseline_source_sha: str | None,
        candidate_source_sha: str,
        baseline_candidate_sha: str,
        candidate_sha: str,
    ) -> dict[str, Any]:
        """Validate Host-only bounded measurements; never a universal proof."""
        if not isinstance(value, Mapping) or _json_size(value) > MAX_CONTEXT_BYTES // 2:
            raise ShaderOrganizationError("Host output comparison is invalid or too large")
        result = dict(value)
        if (
            result.get("schema") != ORGANIZATION_OBSERVATION_SCHEMA
            or result.get("scope") != "captured-event-output"
            or not isinstance(result.get("accepted"), bool)
            or result.get("semanticEquivalenceVerified") is not False
            or result.get("replayRestored") is not True
            or not isinstance(result.get("metrics"), Mapping)
            or not isinstance(result.get("tolerances"), Mapping)
            or isinstance(result.get("sampleCount"), bool)
            or not isinstance(result.get("sampleCount"), int)
            or result["sampleCount"] < (1 if result["accepted"] else 0)
        ):
            raise ShaderOrganizationError("Host output comparison contract is invalid")
        for key, expected in (
            ("baselineSourceSha256", baseline_source_sha),
            ("candidateSourceSha256", candidate_source_sha),
            ("baselineCandidateRawSha256", baseline_candidate_sha),
            ("candidateRawSha256", candidate_sha),
        ):
            if key == "baselineSourceSha256" and expected is None:
                # A captured executable has no source bytes. Its raw binary
                # hash below binds the comparison; do not invent a source hash.
                if result.get(key) is not None:
                    raise ShaderOrganizationError("captured baseline unexpectedly claims source bytes")
                continue
            if _hex(result.get(key), "comparison " + key) != _hex(expected, "expected " + key):
                raise ShaderOrganizationError("Host output comparison candidate binding changed")
        return result

    def _current_compile_and_validation(
        self, service: Any, root: Path, expected_source_sha: str
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Reuse only current Host-verified source, compiler and ABI evidence."""
        status = service.status(str(root))
        latest = status.get("latestCompiled") if isinstance(status, Mapping) else None
        abi = latest.get("abiGate") if isinstance(latest, Mapping) else None
        if (
            not isinstance(latest, Mapping)
            or not isinstance(abi, Mapping)
            or status.get("source", {}).get("currentSha256") != expected_source_sha
            or latest.get("sourceSha256") != expected_source_sha
            or status.get("sourceDependenciesCurrent") is False
            or status.get("revision") != latest.get("revision")
            or status.get("abiValidationStatus") in ("incompatible", "source-changed")
            or status.get("replayAbiValidated") is not True
            or status.get("abiCompatible") is not True
            or latest.get("abiCompatible") is not True
            or latest.get("replayAbiValidated") is not True
            or latest.get("requiresReplayAbiValidation") is not False
            or latest.get("semanticEquivalenceVerified") is not False
            or latest.get("validationScope") != "replay-reflection-abi-compatible"
            or abi.get("status") != "compatible"
        ):
            return None
        compile_result = dict(latest)
        compile_result["workspacePath"] = str(root)
        compile_result["reusedCurrentCandidate"] = True
        validation = {
            "workspacePath": str(root),
            "revision": latest["revision"],
            "requestId": abi.get("requestId"),
            "abiReportSha256": abi.get("reportSha256"),
            "compatible": True,
            "replayAbiValidated": True,
            "requiresReplayAbiValidation": False,
            "reusedCurrentEvidence": True,
        }
        binding = self._candidate_binding(expected_source_sha, compile_result, validation)
        self._require_final_binding(service, root, {
            "workspaceId": status.get("workspaceId"),
            "captureId": status.get("capture", {}).get("captureId"),
            "stage": status.get("stage"),
            "entryPoint": status.get("entryPoint"),
            "shaderProgramIdentity": _shader_program_identity(status),
        }, binding)
        return compile_result, validation

    def _compile_and_validate(
        self,
        service: Any,
        root: Path,
        expected_source_sha: str,
        label: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        role = (
            "baseline" if "baseline" in label else
            "proposal" if "proposal" in label or "organized" in label else
            "recovery" if "recovery" in label else "other"
        )
        with _OperationTiming("shader_organization_candidate", "verify") as timing:
            timing.dimensions(candidateRole=role)
            timing.mark("reuseCheck")
            current = self._current_compile_and_validation(service, root, expected_source_sha)
            if current is not None:
                timing.outcome("reused")
                return current
            native_recovery = None
            for compiler_attempt in range(2):
                timing.mark("fullCompile")
                compile_result = (service.compile(str(root)) if compiler_attempt == 0 else
                                  service.compile(str(root), prefer_native=False))
                if not isinstance(compile_result, dict):
                    raise ShaderOrganizationError("%s compile returned a non-object" % label)
                revision = _positive_int(compile_result.get("revision"), "%s revision" % label)
                raw_sha = _hex(compile_result.get("rawSha256"), "%s raw SHA-256" % label)
                code_sha = _hex(compile_result.get("codeSha256"), "%s code SHA-256" % label)
                if _hex(
                    compile_result.get("sourceSha256"), "%s source SHA-256" % label
                ) != expected_source_sha:
                    raise ShaderOrganizationError("%s compile was not bound to source bytes" % label)
                timing.mark("replayAbiValidation")
                validation_value = self._validate_candidate(str(root), revision, raw_sha)
                if not isinstance(validation_value, Mapping):
                    raise ShaderOrganizationError("%s ABI validation returned a non-object" % label)
                validation = dict(validation_value)
                request_id = _hex(
                    validation.get("requestId"), "%s ABI request ID" % label
                )
                report_sha = _hex(
                    validation.get("abiReportSha256"), "%s ABI report SHA-256" % label
                )
                if (
                    validation.get("compatible") is not True
                    or validation.get("replayAbiValidated") is not True
                    or validation.get("requiresReplayAbiValidation") is not False
                    or validation.get("revision") != revision
                ):
                    if (compiler_attempt == 0 and compile_result.get("nativeCompile")
                            and validation.get("compatible") is False
                            and validation.get("revision") == revision):
                        failed = service.status(str(root))
                        failure = failed.get("lastAbiAttempt") or {}
                        if (failure.get("status") == "incompatible"
                                and failure.get("revision") == revision
                                and failure.get("candidateRawSha256") == raw_sha
                                and failure.get("sourceSha256") == expected_source_sha
                                and failed.get("source", {}).get("currentSha256") == expected_source_sha
                                and failed.get("sourceDependenciesCurrent") is not False
                                and (failed.get("routePlan") is None
                                     or failed.get("routePlan", {}).get("compile"))):
                            native_recovery = {"reason": "native-interface-incompatible",
                                "failedRevision": revision, "failedAbiReportSha256": report_sha,
                                "sourceSha256": expected_source_sha,
                                "nextCompiler": "registered-capture-compatible-compiler"}
                            continue
                    raise self._abi_failure_error(service, root, label)
                break
            if native_recovery is not None:
                compile_result["nativeCompileRecovery"] = native_recovery
            timing.mark("candidateStatusBinding")
            after = service.status(str(root))
            latest = after.get("latestCompiled") if isinstance(after, dict) else None
            abi_gate = latest.get("abiGate") if isinstance(latest, dict) else None
            if (
                not isinstance(latest, dict)
                or Path(str(after.get("workspacePath", ""))).resolve() != root
                or after.get("revision") != revision
                or after.get("source", {}).get("currentSha256") != expected_source_sha
                or latest.get("sourceSha256") != expected_source_sha
                or after.get("sourceDependenciesCurrent") is False
                or latest.get("rawSha256") != raw_sha
                or latest.get("codeSha256") != code_sha
                or latest.get("abiCompatible") is not True
                or not isinstance(abi_gate, Mapping)
                or abi_gate.get("status") != "compatible"
                or abi_gate.get("requestId") != request_id
                or abi_gate.get("reportSha256") != report_sha
            ):
                raise ShaderOrganizationError("%s workspace binding changed" % label)
            timing.outcome("validated")
            return compile_result, validation

    @staticmethod
    def _abi_failure_error(service: Any, root: Path, label: str) -> ShaderOrganizationError:
        """Name the failed ABI facts in the error instead of only the verdict."""

        message = "%s did not pass fresh replay ABI validation" % label
        explanation = None
        try:
            status = service.status(str(root))
            attempt = status.get("lastAbiAttempt") if isinstance(status, Mapping) else None
            explanation = attempt.get("explanation") if isinstance(attempt, Mapping) else None
        except Exception:
            explanation = None
        if isinstance(explanation, Mapping) and explanation.get("findings"):
            findings = list(explanation.get("findings") or [])
            message += ": %s; e.g. %s" % (
                explanation.get("summary"),
                str(findings[0].get("message", ""))[:300],
            )
        error = ShaderOrganizationError(message)
        error.abi_explanation = explanation  # type: ignore[attr-defined]
        return error

    def _workspace_source(
        self, status: Mapping[str, Any]
    ) -> tuple[Path, Path, bytes, str]:
        source_edit_only = status.get("schema") == "renderdoc-ultra-shader-source-edit-status/v1"
        if not source_edit_only and status.get("schema") != "renderdoc-ultra-shader-workspace-status/v1":
            raise ShaderOrganizationError("workspace status schema is invalid")
        source = status.get("source")
        representation = status.get("representation")
        if (
            not isinstance(source, Mapping)
            or not isinstance(representation, Mapping)
            or source.get("present") is not True
            or source.get("format") not in SOURCE_LANGUAGES
            or source.get("lane") != "executable-editable"
            or (not source_edit_only and source.get("compileEligible") is not True)
            or representation.get("lane") != "executable-editable"
            or (not source_edit_only and representation.get("compileEligible") is not True)
        ):
            raise ShaderOrganizationError(
                "AI organization requires an executable-editable HLSL or GLSL workspace"
            )
        root = Path(str(status.get("workspacePath", ""))).resolve()
        source_path = Path(str(source.get("path", ""))).resolve()
        if (
            not root.is_dir()
            or root.is_symlink()
            or not is_within(root, source_path)
            or source_path.is_symlink()
            or source_path.name != "shader." + str(source.get("format"))
        ):
            raise ShaderOrganizationError("workspace source path is invalid or redirected")
        source_bytes = _regular_bytes(source_path, MAX_SOURCE_BYTES, "workspace HLSL")
        source_sha = _sha256(source_bytes)
        if _hex(source.get("currentSha256"), "current source SHA-256") != source_sha:
            raise ShaderOrganizationError("workspace source hash changed")
        source_bytes.decode("utf-8", errors="strict")
        return root, source_path, source_bytes, source_sha

    def _validate_session(
        self,
        session: Mapping[str, Any],
        root: Path,
        organization_id: str,
        status: Mapping[str, Any],
    ) -> None:
        if (
            session.get("schema") != ORGANIZATION_SESSION_SCHEMA
            or session.get("schemaVersion") != 1
            or session.get("policyVersion") != ORGANIZATION_POLICY_VERSION
            or session.get("organizationId") != organization_id
            or session.get("sourceFormat", "hlsl") != status.get("source", {}).get("format")
            or type(session.get("baselineCompiledAndAbiValidated")) is not bool
            or session.get("semanticEquivalenceVerified") is not False
            or session.get("replayApplied") is not False
        ):
            raise ShaderOrganizationError("organization session structure is invalid")
        if not self._same_shader_program(session, status):
            raise ShaderOrganizationError(
                "organization session belongs to a different captured Shader program"
            )
        if (session.get("state") == "completed"
                and session.get("baselineCompiledAndAbiValidated") is not True):
            raise ShaderOrganizationError(
                "completed organization session lacks baseline compile/ABI proof"
            )
        if (
            session.get("state") == "completed"
            and not self._completed_reviews.contains(session)
        ):
            raise ShaderOrganizationError(
                "finalized source receipt is unavailable or changed"
            )

    def _session_artifact(
        self,
        root: Path,
        session: Mapping[str, Any],
        key: str,
        session_dir: Path,
        label: str,
    ) -> Path:
        value = session.get(key)
        if not isinstance(value, str) or not value or "\\" in value:
            raise ShaderOrganizationError("%s path is invalid" % label)
        path = (root / value).resolve()
        if not is_within(session_dir, path) or path.parent != session_dir:
            raise ShaderOrganizationError("%s path escapes its session" % label)
        return path

    @staticmethod
    def _alias_view_payloads(
        root: Path,
        baseline_text: str,
        renames: Mapping[str, str],
        comments: Mapping[str, str],
        proposal: str,
        source_format: str = "hlsl",
    ) -> dict[Path, bytes]:
        """``ai/aliases.json`` (display name -> original) and the rendered view ``ai/organized.hlsl``.

        These are review artifacts. Acceptance promotes the actual proposal
        into the canonical editor/compile source; there is no alias-only main
        editor buffer or implicit reverse rewrite.
        """

        by_target: dict[str, str] = {}
        ambiguous_targets: set[str] = set()
        for selector, target in renames.items():
            if target in by_target and by_target[target] != selector:
                ambiguous_targets.add(target)
            else:
                by_target[target] = selector
        for target in ambiguous_targets:
            by_target.pop(target, None)
        aliases = {
            "schema": "replaylab-shader-aliases/v1",
            "schemaVersion": 1,
            # Preserve the legacy inverse map only for targets with one owner.
            # Distinct functions may safely reuse a local name, so an inverse
            # map cannot represent every scope without silently losing rows.
            "aliases": by_target,
            "renamesBySelector": dict(renames),
            "ambiguousTargets": sorted(ambiguous_targets),
            "comments": dict(comments),
            "baselineSourceSha256": _sha256(baseline_text.encode("utf-8")),
            "viewPath": "ai/organized." + source_format,
        }
        return {
            root / "ai" / "aliases.json": (
                json.dumps(aliases, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8"),
            root / "ai" / ("organized." + source_format): proposal.encode("utf-8"),
        }

    @classmethod
    def _write_alias_view(cls, root: Path, baseline_text: str, renames: Mapping[str, str], comments: Mapping[str, str], proposal: str, source_format: str = "hlsl") -> None:
        for path, content in cls._alias_view_payloads(
            root, baseline_text, renames, comments, proposal, source_format
        ).items():
            atomic_write_bytes(path, content)

    def _begin_result(
        self,
        root: Path,
        session_dir: Path,
        session: Mapping[str, Any],
        semantic_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Expose the current draft and capture facts without prescribing work."""

        _safe_dir(session_dir, root, "organization session directory", create=False)
        proposal_path = self._session_artifact(
            root, session, "proposalSourcePath", session_dir, "proposal source"
        )
        proposal_bytes = _regular_bytes(
            proposal_path, MAX_SOURCE_BYTES, "proposal source"
        )
        source_format = str(session.get("sourceFormat", "hlsl"))
        return {
            "schema": "renderdoc-ai-shader-organization-begin/v1",
            "schemaVersion": 1,
            "workspacePath": str(root),
            "organizationId": session["organizationId"],
            "sourcePath": str(root / str(session["sourcePath"])),
            "sourceFormat": source_format,
            "proposalSourcePath": str(proposal_path),
            "proposalSourceSha256": _sha256(proposal_bytes),
            "sessionPath": str(session_dir / "session.json"),
            "semanticContext": dict(semantic_context),
            "semanticContextPath": str(root / str(session["semanticContextPath"])),
            "baselineSourceSha256": session["baselineSourceSha256"],
            "baselineCompiledAndAbiValidated": session["baselineCompiledAndAbiValidated"],
            "baselineValidationError": str(session.get("baselineValidationError") or ""),
            "organizationCapabilities": organization_capabilities(source_format),
            "analysisPath": str(root / str(session["analysisPath"])),
            "flowchartPath": str(root / str(session["flowchartPath"])),
            "moduleDescriptionsPath": str(root / str(session["moduleDescriptionsPath"])),
            "structuredFlowGraphPath": str(root / str(session["structuredFlowGraphPath"])),
            "applied": False,
        }

    def validate_source_delivery(self, workspace_path: str, *, expected_source_sha256: str = "") -> dict[str, Any]:
        """Validate reconstructed source against the captured original, without applying.

        Source stays editable on every failure. This is one final grouped check,
        reusing the current compile/ABI proof when it still covers all includes.
        A reconstruction is never compared with another copy of itself.
        """
        from .diagnostics import classify_shader_failure

        delivery: dict[str, Any] = {
            "schema": "bread-shader-delivery/v1", "complete": False,
            "sourceReady": False, "compileValidated": False, "abiValidated": False,
            "outputValidated": False, "readyForApply": False, "applied": False,
            "repairRequired": True, "validationScope": "captured-event-output",
            "semanticEquivalenceVerified": False,
        }
        phase = "source"
        with self._guard, source_observation_transaction(workspace_path):
            service = self._service_factory()
            try:
                status = service.status(workspace_path)
                root, source_path, source_bytes, source_sha = self._workspace_source(status)
                if expected_source_sha256 and source_sha != expected_source_sha256:
                    raise ShaderOrganizationError("source changed before delivery validation")
                delivery.update(sourceReady=True, sourceSha256=source_sha,
                                workspaceId=status.get("workspaceId"), sourcePath=str(source_path))
                phase = "compile-and-abi"
                compiled, validated = self._compile_and_validate(service, root, source_sha, "source delivery")
                binding = self._candidate_binding(source_sha, compiled, validated)
                delivery.update(compileValidated=True, abiValidated=True, finalBinding=binding,
                                expectedRevision=binding["revision"],
                                expectedCandidateRawSha256=binding["candidateRawSha256"],
                                compileReused=compiled.get("reusedCurrentCandidate") is True,
                                candidate={**compiled, "requiresReplayAbiValidation": False,
                                           "replayAbiValidated": True})
                phase = "captured-output"
                baseline = status.get("baseline") or {}
                original_sha = _hex(baseline.get("rawSha256"), "captured baseline binary SHA-256")
                exact_code = (compiled.get("codeSha256") == baseline.get("codeSha256")
                              and bool(baseline.get("codeSha256")))
                if exact_code:
                    delivery["equivalenceLevel"] = "exact-captured-code"
                else:
                    if self._compare_candidates is None:
                        raise ShaderOrganizationError("captured-output comparison is unavailable")
                    original_path = (root / str(baseline.get("path") or "")).resolve()
                    if not is_within(root, original_path) or original_path.is_symlink() or not original_path.is_file():
                        raise ShaderOrganizationError("captured baseline path is unavailable")
                    observed = self._validate_observation(self._compare_candidates(str(root), {
                        "candidateBlobPath": str(original_path), "rawSha256": original_sha,
                        "sourceSha256": None,
                    }, compiled), baseline_source_sha=None, candidate_source_sha=source_sha,
                        baseline_candidate_sha=original_sha, candidate_sha=compiled["rawSha256"])
                    delivery["observation"] = observed
                    if observed.get("accepted") is not True:
                        raise ShaderOrganizationError(str(observed.get("reason") or "captured output differs"))
                    delivery["equivalenceLevel"] = "observational-pass"
                self._require_final_binding(service, root, {
                    "workspaceId": status.get("workspaceId"), "captureId": status["capture"]["captureId"],
                    "stage": status.get("stage"), "entryPoint": status.get("entryPoint"),
                    "shaderProgramIdentity": _shader_program_identity(status),
                }, binding)
                delivery.update(complete=True, outputValidated=True, readyForApply=True,
                                repairRequired=False, nextAction="apply-if-requested")
            except Exception as error:
                delivery.update(phase=phase, reason=str(error)[:2000], sourcePreserved=True,
                                diagnostic=classify_shader_failure(error, phase="source-delivery"),
                                abiExplanation=getattr(error, "abi_explanation", None),
                                nextAction=("repair-source-and-revalidate" if phase != "captured-output"
                                            else "localize-numeric-difference-and-repair"),
                                repairTools=["read_shader_workspace_file", "get_shader_workspace_status",
                                             "repair_shader_workspace_source_abi",
                                             "validate_shader_workspace_delivery"])
                if phase == "captured-output" and isinstance(delivery.get("observation"), dict):
                    from .diagnostic_candidate import numeric_recovery_contract
                    delivery["numericRecovery"] = numeric_recovery_contract(
                        root, source_sha, delivery["observation"])
                    delivery["repairTools"].insert(0, "diagnose_shader_organization_candidate")
        return delivery

    def finish_draft(
        self,
        workspace_path: str,
        organization_id: str,
        *,
        generate_flow_graph: bool = False,
        preserve_draft: bool = False,
        expected_reviewed_source_sha256: str = "",
    ) -> dict[str, Any]:
        """Compile and promote the unfinished proposal draft for apply.

        Ending organization may apply *this* draft only after compile, replay
        ABI and final equivalence succeed; it never silently falls back to an
        older revision or treats ABI compatibility as behavioral equivalence.
        Failure restores the baseline source and leaves the proposal session
        intact. An optional navigation graph is generated only after source
        publication; graph errors cannot invalidate the source receipt.
        """

        with self._guard, source_observation_transaction(workspace_path):
            organization_id = _hex(organization_id, "organization_id", 32)
            service = self._service_factory()
            status = service.status(workspace_path)
            root, source_path, initial_source_bytes, current_source_sha = self._workspace_source(status)
            session_dir = root / "ai" / "organization-sessions" / organization_id
            session_path = session_dir / "session.json"
            _safe_dir(session_dir, root, "organization session directory", create=False)
            original_session_bytes = _regular_bytes(
                session_path, MAX_CONTEXT_BYTES, "organization finish session"
            )
            session_gate_path = session_dir / "gate.json"
            root_gate_path = root / "ai" / "organize-code-gate.json"

            def optional_publication_bytes(path: Path, label: str) -> bytes | None:
                if not path.exists():
                    return None
                return _regular_bytes(path, MAX_CONTEXT_BYTES, label)

            original_session_gate_bytes = optional_publication_bytes(
                session_gate_path, "organization finish session gate"
            )
            original_root_gate_bytes = optional_publication_bytes(
                root_gate_path, "organization finish root gate"
            )
            session = _read_session_file(session_dir, "organization finish draft")
            self._validate_session(session, root, organization_id, status)
            if session.get("state") not in OPEN_DRAFT_STATES:
                raise ShaderOrganizationError(
                    "organization source draft is not open; nothing to finish"
                )
            baseline_sha = _hex(
                session.get("baselineSourceSha256"), "organization baseline source SHA-256"
            )
            baseline_path = self._session_artifact(
                root, session, "baselineSourcePath", session_dir, "baseline source"
            )
            baseline_bytes = _regular_bytes(
                baseline_path, MAX_SOURCE_BYTES, "organization baseline source"
            )
            if _sha256(baseline_bytes) != baseline_sha:
                raise ShaderOrganizationError("organization baseline source changed")
            expected_current_sha = _hex(
                session.get("syncedDraftSourceSha256") or baseline_sha,
                "last saved draft source SHA-256",
            )
            if current_source_sha != expected_current_sha:
                raise ShaderOrganizationError(
                    "editable source changed since the organization draft was saved; refuse finish"
                )
            proposal_path = self._session_artifact(
                root, session, "proposalSourcePath", session_dir, "proposal source"
            )
            proposal_text, proposal_bytes = _utf8(
                proposal_path, MAX_SOURCE_BYTES, "proposal source"
            )
            proposal_sha = _sha256(proposal_bytes)
            if expected_reviewed_source_sha256 and proposal_sha != expected_reviewed_source_sha256:
                raise ShaderOrganizationError("proposal changed after source self-review; review the current draft")
            language = str(session.get("sourceFormat", "hlsl"))
            sealed_baseline_code = (
                _hex(session.get("baselineCodeSha256"), "baseline code SHA-256")
                if session.get("baselineCompiledAndAbiValidated") is True else None
            )

            # Finish is a publication boundary. Bind its baseline proof now,
            # before replacing the source, so the sealed gate can verify the
            # same compile and ABI evidence as an ordinary final review.
            baseline_swapped = initial_source_bytes != baseline_bytes
            if baseline_swapped and not _source_compare_and_swap(
                source_path, initial_source_bytes, baseline_bytes, "workspace HLSL"
            ):
                raise ShaderOrganizationError(
                    "editable source changed before validating the saved draft"
                )
            try:
                baseline_compile_for_observation, baseline_validation_for_gate = (
                    self._compile_and_validate(
                        service, root, baseline_sha, "finish organization baseline"
                    )
                )
                if (sealed_baseline_code is not None and
                        baseline_compile_for_observation["codeSha256"] != sealed_baseline_code):
                    raise ShaderOrganizationError(
                        "organization baseline executable code changed before finish"
                    )
            except Exception as error:
                if baseline_swapped and not _source_compare_and_swap(
                    source_path, baseline_bytes, initial_source_bytes, "workspace HLSL"
                ):
                    raise ShaderOrganizationError(
                        "baseline validation failed and the saved editable draft could not be restored: %s"
                        % error
                    ) from error
                raise

            if not _source_compare_and_swap(
                source_path, baseline_bytes, proposal_bytes, "workspace HLSL"
            ):
                if baseline_swapped:
                    observed = _regular_bytes(source_path, MAX_SOURCE_BYTES, "workspace HLSL")
                    if observed == baseline_bytes and not _source_compare_and_swap(
                        source_path, baseline_bytes, initial_source_bytes, "workspace HLSL"
                    ):
                        raise ShaderOrganizationError(
                            "editable source could not be restored after finish conflict"
                        )
                raise ShaderOrganizationError(
                    "editable source changed before finishing the current draft"
                )
            source_replaced = True
            equivalence_level = "not-run"
            observation: dict[str, Any] | None = None
            try:
                organized_compile, organized_validation = self._compile_and_validate(
                    service, root, proposal_sha, "finish current organization draft"
                )
                code_equal = (
                    organized_compile["codeSha256"]
                    == baseline_compile_for_observation["codeSha256"]
                )
                equivalence = rename_only_equivalent(
                    baseline_bytes.decode("utf-8", errors="replace"),
                    proposal_text,
                    session.get("renameMap") if isinstance(session.get("renameMap"), Mapping) else {},
                )
                rename_only_equal = bool(equivalence.get("equal"))
                equivalence_level = (
                    "exact-code" if code_equal
                    else ("rename-only" if rename_only_equal else "rejected")
                )
                accepted = code_equal or rename_only_equal
                if (
                    not accepted
                    and baseline_compile_for_observation is not None
                    and self._compare_candidates is not None
                ):
                    observation = self._validate_observation(
                        self._compare_candidates(
                            str(root),
                            baseline_compile_for_observation,
                            organized_compile,
                        ),
                        baseline_source_sha=baseline_sha,
                        candidate_source_sha=proposal_sha,
                        baseline_candidate_sha=baseline_compile_for_observation["rawSha256"],
                        candidate_sha=organized_compile["rawSha256"],
                    )
                    accepted = observation.get("accepted") is True
                    if accepted:
                        equivalence_level = "observational-pass"
                if not accepted:
                    reason = (
                        str(observation.get("reason") or "captured-output fingerprint differed")
                        if isinstance(observation, Mapping)
                        else (
                            "final equivalence is unverified: compiled code differs, "
                            "the edit is not rename-only, and no accepted captured-output "
                            "comparison is available"
                        )
                    )
                    raise ShaderOrganizationError(reason)
            except Exception as error:
                if source_replaced:
                    restored = _source_compare_and_swap(
                        source_path, proposal_bytes, initial_source_bytes, "workspace HLSL"
                    )
                    if not restored:
                        raise ShaderOrganizationError(
                            "finish failed and the previous editable source could not be restored: %s"
                            % error
                        ) from error
                failed_result = {
                    "schema": "replaylab-shader-organization-finish/v1",
                    "schemaVersion": 1,
                    "organizationId": organization_id,
                    "applied": False,
                    "promoted": False,
                    "ok": False,
                    "draftPreserved": True,
                    "baselineRestored": initial_source_bytes == baseline_bytes,
                    "previousSourceRestored": True,
                    "reason": str(error)[:800],
                    "equivalenceLevel": equivalence_level,
                    "observation": observation,
                    "abiExplanation": getattr(error, "abi_explanation", None),
                    "state": "finish-failed-draft-preserved",
                    "blockingConditions": ["finish-validation-failed"],
                    "recoverable": True,
                    "repair": {
                        "proposalSourcePath": str(proposal_path),
                        "baselineSourcePath": str(baseline_path),
                        "nextAction": ("localize-numeric-difference-and-repair" if observation is not None
                                       else "repair-compiler-or-abi-findings"),
                        "strategy": ("Use the failed output samples to isolate changed functional modules; "
                                     "bisect only the changed region when the cause is unclear, preserve the "
                                     "baseline and current draft, then validate the repaired complete source."),
                        "retryUnchangedSource": False,
                        "preserveDefaultTolerance": True,
                    },
                }
                if isinstance(observation, dict):
                    from .diagnostic_candidate import numeric_recovery_contract
                    failed_result["numericRecovery"] = numeric_recovery_contract(
                        root, _sha256(initial_source_bytes), observation, proposal_sha)
                return failed_result

            def finish_publication_step(action: Callable[[], Any], label: str) -> Any:
                """Run one post-validation publication step with baseline recovery.

                Compile/ABI/equivalence success leaves the proposal temporarily in
                the canonical source path.  No artifact or receipt failure after
                that point may strand those bytes there or turn an unfinished
                draft into a completed session.  Keep the original exception as
                the cause and report recovery failures separately.
                """

                try:
                    return action()
                except Exception as error:
                    recovery_errors: list[str] = []
                    try:
                        observed = _regular_bytes(
                            source_path, MAX_SOURCE_BYTES,
                            "workspace HLSL finish publication recovery",
                        )
                        if observed != initial_source_bytes:
                            if observed != proposal_bytes or not _source_compare_and_swap(
                                source_path,
                                proposal_bytes,
                                initial_source_bytes,
                                "workspace HLSL finish publication recovery",
                            ):
                                raise ShaderOrganizationError(
                                    "canonical source changed concurrently; refusing to "
                                    "overwrite newer bytes"
                                )
                    except Exception as recovery_error:
                        recovery_errors.append(
                            "canonical source: %s" % str(recovery_error)
                        )
                    try:
                        atomic_write_bytes(session_path, original_session_bytes)
                    except Exception as recovery_error:
                        recovery_errors.append(
                            "organization session: %s" % str(recovery_error)
                        )
                    for gate_path, original_bytes, gate_label in (
                        (
                            session_gate_path,
                            original_session_gate_bytes,
                            "organization session gate",
                        ),
                        (
                            root_gate_path,
                            original_root_gate_bytes,
                            "organization root gate",
                        ),
                    ):
                        try:
                            if original_bytes is None:
                                if gate_path.exists():
                                    if gate_path.is_symlink() or not gate_path.is_file():
                                        raise ShaderOrganizationError(
                                            "%s recovery target is not a regular file"
                                            % gate_label
                                        )
                                    gate_path.unlink()
                            else:
                                atomic_write_bytes(gate_path, original_bytes)
                        except Exception as recovery_error:
                            recovery_errors.append(
                                "%s: %s" % (gate_label, recovery_error)
                            )
                    try:
                        # A later failure may occur after the completed checkpoint
                        # replaced the pending row. Restore the exact original
                        # open draft authority for a retry.
                        self._checkpoints.remember(session)
                    except Exception as recovery_error:
                        recovery_errors.append(
                            "organization checkpoint: %s" % str(recovery_error)
                        )
                    message = "finish publication failed during %s: %s" % (
                        label, error
                    )
                    if recovery_errors:
                        message += "; recovery failed (%s)" % "; ".join(
                            recovery_errors
                        )
                    else:
                        message += "; previous source and retryable draft session restored"
                    raise ShaderOrganizationError(message) from error

            final_binding = finish_publication_step(
                lambda: self._candidate_binding(
                    proposal_sha, organized_compile, organized_validation
                ),
                "final binding",
            )
            if preserve_draft:
                # A diagnostic checkpoint is useful evidence, not a completed
                # organization. Save through the existing recoverable draft
                # transaction so subsequent edits retain this session/baseline.
                saved = finish_publication_step(
                    lambda: self.save_draft(
                        str(root), organization_id, generate_flow_graph=generate_flow_graph,
                        expected_reviewed_source_sha256=proposal_sha,
                    ),
                    "validated checkpoint save",
                )
                return {
                    **saved, "schema": "replaylab-shader-organization-finish/v1",
                    "state": "validated-checkpoint", "publicationMode": "checkpoint",
                    "wholeSourceValidated": True, "semanticOrganizationVerified": False,
                    "validationDeferred": False, "draftPreserved": True,
                    "equivalenceLevel": equivalence_level, "observation": observation,
                    "finalBinding": final_binding,
                    "expectedRevision": final_binding["revision"],
                    "expectedCandidateRawSha256": final_binding["candidateRawSha256"],
                }
            reviewed_at = _utc_now()
            # Finalization validates and publishes the whole source. Whether it
            # meets the user's readability goal is the Agent's assessment.
            # Diagrams are independent artifacts and are never fabricated here.
            flow_graph = {"graphId": ""}
            root_flow_graph_path = root / "ai" / "flow-graph.json"
            flow_graph_sha256 = ""

            decision = "accepted-executable-editable"
            verdict = build_verdict(
                accepted=True, level=equivalence_level, reasons=[]
            )
            gate = {
                "schema": ORGANIZATION_GATE_SCHEMA,
                "schemaVersion": 1,
                "policyVersion": ORGANIZATION_POLICY_VERSION,
                "organizationId": organization_id,
                "reviewedAt": reviewed_at,
                "requestedTransformation": "organize-only",
                "workspaceId": session["workspaceId"],
                "captureId": session["captureId"],
                "stage": session["stage"],
                "entryPoint": session["entryPoint"],
                "baselineSourceSha256": baseline_sha,
                "proposalSourceSha256": proposal_sha,
                "baselineCodeSha256": baseline_compile_for_observation["codeSha256"],
                "organizedCodeSha256": organized_compile["codeSha256"],
                "baselineReviewRevision": baseline_compile_for_observation["revision"],
                "baselineReviewCandidateRawSha256": baseline_compile_for_observation["rawSha256"],
                "baselineReviewAbiRequestId": baseline_validation_for_gate["requestId"],
                "baselineReviewAbiReportSha256": baseline_validation_for_gate["abiReportSha256"],
                "codeSha256Equal": bool(code_equal),
                "equivalenceLevel": equivalence_level,
                "observation": observation,
                "renameMap": dict(session.get("renameMap") or {}),
                "renameProvenance": copy.deepcopy(dict(session.get("renameProvenance") or {})),
                "verdict": verdict,
                "decision": decision,
                "organizedSourcePromoted": True,
                "executableEditableSourcePath": str(source_path),
                "proposalSourcePath": str(proposal_path),
                "baselineCompiledAndAbiValidated": True,
                "organizedCompiledAndAbiValidated": True,
                "rawRecoveryCompiledAndAbiValidated": False,
                "semanticEquivalenceVerified": False,
                "replayApplied": False,
                "safeToStartLiveEdit": True,
                "finalBinding": final_binding,
                "finalSourceSha256": final_binding["sourceSha256"],
                "finalRevision": final_binding["revision"],
                "finalCandidateRawSha256": final_binding["candidateRawSha256"],
                "finalCodeSha256": final_binding["codeSha256"],
                "finalAbiRequestId": final_binding["abiRequestId"],
                "finalAbiReportSha256": final_binding["abiReportSha256"],
                "flowGraphId": flow_graph.get("graphId") if isinstance(flow_graph, Mapping) else "",
                "flowGraphSha256": flow_graph_sha256,
                "flowGraphArtifactPath": str(root_flow_graph_path),
                "publicationContract": "validated-source",
                "finishedCurrentDraft": True,
                "wholeSourceValidated": True,
                "note": "Current source compiled, passed ABI and equivalence validation.",
            }
            finish_publication_step(
                lambda: atomic_write_bytes(
                    session_dir / ("organized." + source_path.suffix.lstrip(".")),
                    proposal_bytes,
                ),
                "session organized source artifact",
            )
            finish_publication_step(
                lambda: atomic_write_bytes(
                    root / "ai" / ("organized." + source_path.suffix.lstrip(".")),
                    proposal_bytes,
                ),
                "workspace organized source artifact",
            )
            finish_publication_step(
                lambda: atomic_write_json(
                    session_dir / "organized-compile.json", organized_compile
                ),
                "organized compile artifact",
            )
            finish_publication_step(
                lambda: atomic_write_json(
                    session_dir / "baseline-compile.json", baseline_compile_for_observation
                ),
                "baseline compile artifact",
            )
            finish_publication_step(
                lambda: atomic_write_json(
                    session_dir / "baseline-abi.json", baseline_validation_for_gate
                ),
                "baseline ABI artifact",
            )
            finish_publication_step(
                lambda: atomic_write_json(
                    session_dir / "organized-abi.json", organized_validation
                ),
                "organized ABI artifact",
            )
            completed = dict(session)
            completed["state"] = "completed"
            completed["completedAt"] = reviewed_at
            completed["decision"] = decision
            completed["proposalSourceSha256"] = proposal_sha
            completed["organizedCodeSha256"] = organized_compile["codeSha256"]
            completed["baselineRevision"] = baseline_compile_for_observation["revision"]
            completed["baselineCandidateRawSha256"] = baseline_compile_for_observation["rawSha256"]
            completed["baselineCodeSha256"] = baseline_compile_for_observation["codeSha256"]
            completed["baselineAbiRequestId"] = baseline_validation_for_gate["requestId"]
            completed["baselineAbiReportSha256"] = baseline_validation_for_gate["abiReportSha256"]
            completed["baselineCompiledAndAbiValidated"] = True
            completed["baselineValidationError"] = ""
            completed["codeSha256Equal"] = bool(code_equal)
            completed["equivalenceLevel"] = equivalence_level
            completed["observation"] = observation
            completed["verdict"] = verdict
            completed["organizedSourcePromoted"] = True
            completed["finalBinding"] = final_binding
            completed["finishedCurrentDraft"] = True
            completed["publicationContract"] = "validated-source"
            completed["flowGraphId"] = gate["flowGraphId"]
            completed["flowGraphSha256"] = gate["flowGraphSha256"]
            completed = finish_publication_step(
                lambda: self._seal_session(completed), "completed session seal"
            )
            finish_publication_step(
                lambda: atomic_write_json(session_path, completed),
                "completed session publication",
            )
            finish_publication_step(
                lambda: atomic_write_json(session_gate_path, gate),
                "session gate publication",
            )
            finish_publication_step(
                lambda: self._require_final_binding(
                    service, root, session, final_binding
                ),
                "final binding revalidation",
            )
            if is_within(root, self._completed_reviews.path.resolve()):
                raise ShaderOrganizationError(
                    "finalized source receipt store must be outside the workspace"
                )
            finish_publication_step(
                lambda: self._completed_reviews.remember(completed),
                "finalized source receipt store",
            )
            finish_publication_step(
                lambda: self._checkpoints.remember(completed),
                "completed checkpoint store",
            )
            finish_publication_step(
                lambda: self._publish_root_gate(root, gate),
                "workspace root gate publication",
            )
            flow_graph_result: dict[str, Any] = {"generated": False}
            if generate_flow_graph:
                try:
                    graph = build_shader_flow_graph(
                        proposal_text,
                        workspace_id=str(session["workspaceId"]),
                        source_path=str(session["sourcePath"]),
                        source_sha256=proposal_sha,
                        source_revision=final_binding["revision"],
                        stage=str(session["stage"]),
                        entry_point=str(session["entryPoint"]),
                        generated_at=_utc_now(),
                        origin="organized-proposal",
                    )
                    atomic_write_json(root_flow_graph_path, graph)
                    flow_graph_result = {
                        "generated": True,
                        "graphId": graph["graphId"],
                        "artifactPath": str(root_flow_graph_path),
                        "nodeCount": len(graph["nodes"]),
                        "edgeCount": len(graph["edges"]),
                    }
                except Exception as graph_error:
                    flow_graph_result = {
                        "generated": False,
                        "diagnostic": str(graph_error)[:800],
                    }
            return {
                "schema": "replaylab-shader-organization-finish/v1",
                "schemaVersion": 1,
                "organizationId": organization_id,
                "ok": True,
                "promoted": True,
                "applied": False,
                "draftPreserved": False,
                "decision": decision,
                "equivalenceLevel": equivalence_level,
                "observation": observation,
                "finalBinding": final_binding,
                "expectedRevision": final_binding["revision"],
                "expectedCandidateRawSha256": final_binding["candidateRawSha256"],
                "workspacePath": str(root),
                "sourceFormat": language,
                "gatePath": str(root / "ai" / "organize-code-gate.json"),
                "wholeSourceValidated": True,
                "semanticOrganizationVerified": False,
                "state": "validated-source",
                "flowGraph": flow_graph_result,
            }


__all__ = [
    "ORGANIZATION_GATE_SCHEMA",
    "ORGANIZATION_POLICY_VERSION",
    "ORGANIZATION_SESSION_SCHEMA",
    "ShaderOrganizationError",
    "ShaderOrganizationManager",
]
