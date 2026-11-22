"""Source-bound explanatory documents; never Shader acceptance authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping
import uuid

from .io import atomic_write_json, is_within, read_json_object
from . import workspace_mutation_transaction as mutations
from .workspace_mutation_transaction import apply_transaction


BINDING_SCHEMA = "renderdoc-ai-shader-explanation-source-binding/v1"
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_BINDING_BYTES = 64 * 1024


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _regular_snapshot(root: Path, path: Path, limit: int) -> bytes | None:
    """Keep the lexical path until every workspace ancestor is checked."""
    if not is_within(root, path.resolve()):
        raise RuntimeError("explanation path escapes its workspace")
    current = path
    while True:
        junction = getattr(current, "is_junction", None)
        if current.is_symlink() or (junction is not None and junction()):
            raise RuntimeError("explanation path is redirected")
        if current == root:
            break
        if current.parent == current:
            raise RuntimeError("explanation path escapes its workspace")
        current = current.parent
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > limit:
        raise RuntimeError("explanation file is not regular or exceeds its size limit")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise RuntimeError("explanation file exceeds its size limit")
    return raw


def binding_path(artifact: Path) -> Path:
    return artifact.with_name(artifact.name + ".source-binding.json")


def _source_snapshot(root: Path, status: Mapping[str, Any]) -> tuple[Path, str]:
    source = status.get("source") or {}
    path = Path(str(source.get("path") or ""))
    if not path.is_absolute():
        raise RuntimeError("workspace Source path is invalid")
    raw = _regular_snapshot(root, path, MAX_SOURCE_BYTES)
    if raw is None:
        raise RuntimeError("workspace Source is unavailable")
    return path, _sha(raw)


def source_binding_metadata(
    root: Path, artifact: Path, raw: bytes, status: Mapping[str, Any]
) -> dict[str, Any]:
    """Describe optional provenance without rejecting Source/ABI publication.

    Older documents without a sidecar remain readable, explicitly unbound.
    Readers must not promote a bound but stale document against current Source.
    """
    result: dict[str, Any] = {
        "role": "explanatory-document", "acceptanceAuthorization": False,
        "status": "unbound", "currentSourceMatches": None,
        "artifactSha256": _sha(raw),
    }
    try:
        sidecar = binding_path(artifact)
        stored = _regular_snapshot(root, sidecar, MAX_BINDING_BYTES)
        if stored is None:
            return result
        binding = json.loads(stored.decode("utf-8"))
        if not isinstance(binding, dict) or binding.get("schema") != BINDING_SCHEMA:
            raise RuntimeError("explanation source binding schema is invalid")
        result["sourceBinding"] = binding
        if binding.get("artifactSha256") != _sha(raw):
            raise RuntimeError("explanation changed after its Source binding")
        path, digest = _source_snapshot(root, status)
        source = status.get("source") or {}
        baseline = status.get("baseline") or {}
        if (str(path.resolve()) != str(binding.get("sourcePath") or "")
                or digest != binding.get("sourceSha256")
                or digest != source.get("currentSha256")
                or binding.get("sourceChangedDuringWrite") is True
                or binding.get("stage") != status.get("stage")
                or binding.get("entryPoint", "") != status.get("entryPoint", "")
                or binding.get("originalRawSha256") != baseline.get("rawSha256")):
            raise RuntimeError("explanation does not describe current workspace Source")
        # A reader can race any publisher or external editor between the pair.
        if (_regular_snapshot(root, sidecar, MAX_BINDING_BYTES) != stored
                or _regular_snapshot(root, artifact, MAX_DOCUMENT_BYTES) != raw
                or _source_snapshot(root, status) != (path, digest)):
            raise RuntimeError("explanation changed while it was read")
        result.update(status="current", currentSourceMatches=True)
    except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
        result.update(status="stale", currentSourceMatches=False, reason=str(exc)[:800])
    return result


def _retire_observed_intent(
    store: Path, root: Path, canonical: Path, observed_sha256: str,
    check_source: Callable[[], None],
) -> None:
    """Retire only a verified derived-doc intent, never restore its targets.

    The content CAS is the authorization to merge the current public document.
    Old organization IDs do not select a new acceptance or recovery workflow.
    This narrow path uses the journal's existing lock/validator/cleanup; it is
    deliberately unavailable to Source, ABI or general organization mutations.
    """
    with mutations._transaction_lock(store, root, ""):
        directory = mutations.transaction_directory(store, root, "")
        ledger_path = directory / "ledger.json"
        ledger = mutations._read_ledger(ledger_path)
        if ledger is None:
            return
        owner = str(ledger.get("organizationId") or "")
        if (ledger.get("tool") != "publish-source-bound-explanation"
                or not re.fullmatch(r"[0-9a-f]{32}", owner)
                or ledger.get("externalRoots") != []
                or ledger.get("phase") not in {"preparing", "prepared", "applying", "checkpointing", "committed", "rolling-back", "recovery-required"}):
            raise RuntimeError("pending intent is not a derived explanation publication")
        unused_root, unused_roots, rows = mutations._validate_ledger_binding(ledger, root, owner)
        name = canonical.name
        session = root / "ai" / "organization-sessions" / owner / name
        allowed = {session, binding_path(session), canonical, binding_path(canonical)}
        paths = {Path(str(row["path"])) for row in rows}
        # Canonicalized spellings are compared by the journal's physical path
        # identity, including Windows case and extended path compatibility.
        if (len(rows) != 4 or {mutations._path_identity(p) for p in paths}
                != {mutations._path_identity(p) for p in allowed}):
            raise RuntimeError("pending explanation intent has unexpected document targets")
        intent_sha256 = _sha(json.dumps(ledger, sort_keys=True).encode("utf-8"))
        audit_directory = store / "retired-intents"
        if (mutations._is_redirected(audit_directory)
                or (audit_directory.exists() and not audit_directory.is_dir())):
            raise RuntimeError("explanation retirement audit directory is redirected")
        authorized_cleanup = False
        authorized_audit_name = None
        if audit_directory.exists():
            # A cleanup may stop after deleting some owned payloads. Only an
            # already durable, validated retirement intent for these exact
            # ledger bytes authorizes continuing that metadata-only cleanup.
            for audit in mutations._bounded_directory_entries(audit_directory):
                if not re.fullmatch(r"[0-9a-f]{32}\.json", audit.name):
                    continue
                prior = read_json_object(audit, MAX_BINDING_BYTES, "explanation retirement audit")
                if (prior.get("schema") == "renderdoc-ai-shader-explanation-retired-intent/v1"
                        and prior.get("intentSha256") == intent_sha256
                        and prior.get("payloadsValidated") is True
                        and prior.get("cleanupStarted") is True
                        and prior.get("targetsWritten") is False
                        and prior.get("status") in {"retirement-prepared", "retirement-refused"}):
                    authorized_cleanup = True
                    authorized_audit_name = audit.name
        after_images: dict[str, bytes] = {}
        incomplete_cleanup = False
        for row in rows:
            for kind in ("before", "after"):
                if (authorized_cleanup
                        and not mutations._payload_path(directory, row, kind + "File").exists()):
                    incomplete_cleanup = True
                    continue
                payload = mutations._read_payload(directory, row, kind=kind, required=bool(row[kind + "Existed"]))
                if kind == "after":
                    after_images[mutations._path_identity(Path(str(row["path"])))] = payload or b""
        if not incomplete_cleanup:
            after_doc = after_images[mutations._path_identity(canonical)]
            after_binding = after_images[mutations._path_identity(binding_path(canonical))]
            if (after_images[mutations._path_identity(session)] != after_doc
                    or after_images[mutations._path_identity(binding_path(session))] != after_binding
                    or ledger.get("requestSha256") != _sha(after_binding + after_doc)):
                raise RuntimeError("pending explanation intent payload identity is invalid")
            parsed_binding = json.loads(after_binding.decode("utf-8"))
            if (not isinstance(parsed_binding, dict) or parsed_binding.get("schema") != BINDING_SCHEMA
                    or parsed_binding.get("artifactSha256") != _sha(after_doc)):
                raise RuntimeError("pending explanation intent Source binding is invalid")
        snapshots = {
            path: _regular_snapshot(root, path, MAX_BINDING_BYTES if path.name.endswith(".source-binding.json") else MAX_DOCUMENT_BYTES)
            for path in allowed
        }
        current = snapshots[canonical]
        if current is None or _sha(current) != observed_sha256:
            raise RuntimeError("canonical explanation changed since it was read")
        check_source()
        audit_directory.mkdir(exist_ok=True)
        if mutations._is_redirected(audit_directory):
            raise RuntimeError("explanation retirement audit directory is redirected")
        audit = audit_directory / (uuid.uuid4().hex + ".json")
        record = {
            "schema": "renderdoc-ai-shader-explanation-retired-intent/v1",
            "status": "retirement-prepared", "workspacePath": str(root),
            "intentSha256": intent_sha256, "payloadsValidated": True,
            "cleanupStarted": False,
            "continuedValidatedCleanup": incomplete_cleanup,
            "previousValidatedRetirementAudit": authorized_audit_name,
            "previousOrganizationId": owner, "previousPhase": ledger.get("phase"),
            "observedCanonicalSha256": observed_sha256,
            "targets": [{"path": str(p), "present": raw is not None,
                         "currentSha256": _sha(raw) if raw is not None else None}
                        for p, raw in snapshots.items()],
            "targetsWritten": False, "acceptanceAuthorization": False,
        }
        atomic_write_json(audit, record)
        try:
            for path, raw in snapshots.items():
                limit = MAX_BINDING_BYTES if path.name.endswith(".source-binding.json") else MAX_DOCUMENT_BYTES
                if _regular_snapshot(root, path, limit) != raw:
                    raise RuntimeError("explanation changed while its old intent was retired")
            check_source()
            record["cleanupStarted"] = True
            atomic_write_json(audit, record)
            mutations._cleanup(directory, rows)
        except BaseException:
            record["status"] = "retirement-refused"
            atomic_write_json(audit, record)
            raise
        record["status"] = "retired"
        atomic_write_json(audit, record)


def publish_closed_explanation(
    *, store_root: Path, root: Path, organization_id: str,
    session_artifact: Path, kind: str, data: bytes,
    source_binding: Mapping[str, Any], status: Mapping[str, Any],
    expected_canonical_sha256: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """CAS the session/public document pair; Source is never a write target."""
    canonical = root / "ai" / ("analysis.md" if kind == "analysis" else "flowchart.mmd")
    binding = dict(source_binding)
    binding.update(artifactSha256=_sha(data), sourceChangedDuringWrite=False,
                   currentSourceSha256AfterWrite=binding["sourceSha256"])
    encoded = (json.dumps(binding, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    # Sidecars go first so interrupted first publication cannot expose new
    # canonical bytes as an unbound legacy document. Readers reject the pair
    # until its matching artifact has also been replaced.
    targets = {binding_path(session_artifact): encoded, session_artifact: data,
               binding_path(canonical): encoded, canonical: data}
    before = {
        path: _regular_snapshot(root, path, MAX_BINDING_BYTES if path.name.endswith(".source-binding.json") else MAX_DOCUMENT_BYTES)
        for path in targets
    }
    # A previously published document edited outside its sidecar is user data.
    # Preserve it. Legacy canonical documents can be replaced from their exact
    # observed bytes, allowing migration from the old session-only writer.
    previous = before[binding_path(canonical)]
    current = before[canonical]
    if expected_canonical_sha256:
        if current is None or _sha(current) != expected_canonical_sha256:
            raise RuntimeError("canonical explanation changed since it was read")
    elif previous is not None:
        try:
            old_binding = json.loads(previous.decode("utf-8"))
        except (ValueError, UnicodeError):
            old_binding = None
        if (not isinstance(old_binding, dict) or old_binding.get("schema") != BINDING_SCHEMA
                or old_binding.get("artifactSha256") != _sha(before[canonical] or b"")):
            raise RuntimeError("canonical explanation changed outside its Source binding; "
                               "read its current contents and supply expected_canonical_sha256")

    manifest_text = str(status.get("manifestPath") or "")
    manifest = Path(manifest_text) if manifest_text else None
    manifest_before = (_regular_snapshot(root, manifest, MAX_SOURCE_BYTES)
                       if manifest is not None else None)

    def check_source() -> None:
        path, digest = _source_snapshot(root, status)
        if (str(path.resolve()) != binding["sourcePath"]
                or digest != binding["sourceSha256"]):
            raise RuntimeError("workspace Source changed during explanation publication")
        if (manifest is not None
                and _regular_snapshot(root, manifest, MAX_SOURCE_BYTES) != manifest_before):
            raise RuntimeError("workspace manifest changed during explanation publication")

    check_source()
    if expected_canonical_sha256:
        _retire_observed_intent(store_root / "explanation-publications", root,
                                canonical, expected_canonical_sha256, check_source)
    apply_transaction(
        # Separate derived-document recovery from organization checkpoints.
        # Neither a closed acceptance receipt nor Source is ever resealed.
        store_root=store_root / "explanation-publications", workspace_root=root,
        organization_id=organization_id, tool="publish-source-bound-explanation",
        operation_id="", request_sha256=_sha(encoded + data), targets=targets,
        expected_before=before, checkpoint_after_apply=check_source,
    )
    return binding, {"status": "published", "path": str(canonical),
                     "sourceBindingPath": str(binding_path(canonical)),
                     "sha256": _sha(data), "artifactOnly": True,
                     "acceptanceAuthorization": False}
