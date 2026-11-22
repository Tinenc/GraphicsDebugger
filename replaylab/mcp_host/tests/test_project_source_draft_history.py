"""A restored project source cannot inherit an unrelated historical draft lock."""
import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

from src.shader_workspace import project_source
from src.shader_workspace.organization import active_draft_id
from tests.test_project_source_program_identity import source_case
from tests.test_shader_workspace_mcp import load_server_module


@pytest.fixture
def imported(source_case):
    project, workspaces, knowledge, create = source_case
    root, status = create("draft-history-target")
    bundle = project.service.prepare_workspace_source(knowledge["knowledgeId"], status)
    workspaces.import_project_source(root, bundle)
    status = workspaces.status(root)
    organization = "1" * 32
    session_dir = root / "ai" / "organization-sessions" / organization
    session_dir.mkdir(parents=True)
    old_source = b"// previously organized source\n"
    old_sha = hashlib.sha256(old_source).hexdigest()
    session = dict(schema="renderdoc-ai-shader-organization-session/v1", schemaVersion=1,
                   organizationId=organization, workspaceId=status["workspaceId"],
                   originalRawSha256=status["baseline"]["rawSha256"], state="drafting",
                   baselineSourceSha256=old_sha, proposalSourceSha256=old_sha,
                   syncedDraftSourceSha256=old_sha, sessionIntegrityHmacSha256="f" * 64)
    gate = dict(schema="renderdoc-ai-shader-organization-gate/v1", schemaVersion=1,
                organizationId=organization, workspaceId=status["workspaceId"],
                decision="editable-draft", baselineSourceSha256=old_sha,
                proposalSourceSha256=old_sha, finalBinding={"sourceSha256": old_sha, "complete": False})
    session_path = session_dir / "session.json"
    session_path.write_text(json.dumps(session))
    (session_dir / "organized-proposal.hlsl").write_bytes(old_source)
    gate_path = root / "ai" / "organize-code-gate.json"
    gate_path.write_text(json.dumps(gate))
    return root, workspaces, status, gate_path, session_path, gate, session


def test_already_imported_source_archives_old_gate_then_compiles_without_old_review(imported):
    root, workspaces, status, gate_path, session_path, gate, session = imported
    gate_bytes, session_bytes = gate_path.read_bytes(), session_path.read_bytes()
    assert active_draft_id(root) == gate["organizationId"]
    server = load_server_module()
    assert server._require_organization_source_review(root) == ""
    assert not gate_path.exists()
    assert active_draft_id(root) == ""
    history = list((root / "ai" / "organization-source-history").glob("*.json"))
    assert len(history) == 2
    assert any(path.read_bytes() == gate_bytes for path in history)
    assert session_path.read_bytes() == session_bytes
    assert (session_path.parent / "organized-proposal.hlsl").read_bytes() == b"// previously organized source\n"
    # Real workspace compile consumes the imported bytes, without inheriting
    # the archived review or claiming the target's live ABI has passed.
    compiled = workspaces.compile(root)
    assert compiled["sourceSha256"] == status["source"]["currentSha256"]
    assert compiled["requiresReplayAbiValidation"] is True
    assert compiled["replayAbiValidated"] is False


@pytest.mark.parametrize("state", ["canonical-edit", "new-draft", "new-proposal", "receipt", "foreign-workspace"])
def test_unpublished_source_and_current_draft_keep_their_pointer(imported, state):
    root, workspaces, status, gate_path, session_path, gate, session = imported
    if state == "canonical-edit":
        Path(status["source"]["path"]).write_text("// unfinished user edit\n")
    elif state == "new-draft":
        gate["baselineSourceSha256"] = status["source"]["currentSha256"]
    elif state == "new-proposal":
        session["syncedDraftSourceSha256"] = status["source"]["currentSha256"]
    elif state == "receipt":
        path = root / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["source"]["projectSource"]["receiptSha256"] = "0" * 64
        path.write_text(json.dumps(manifest))
    else:
        session["workspaceId"] = "0" * 32
    gate_path.write_text(json.dumps(gate))
    session_path.write_text(json.dumps(session))
    before = gate_path.read_bytes()
    assert project_source.archive_superseded_project_source_gate(root) is None
    assert gate_path.read_bytes() == before
    assert active_draft_id(root) == gate["organizationId"]


def test_concurrently_replaced_gate_is_not_detached(imported):
    root, workspaces, status, gate_path, session_path, gate, session = imported
    from src.shader_workspace import io
    original = io.atomic_write_bytes
    updated = {**gate, "baselineSourceSha256": status["source"]["currentSha256"]}
    def concurrent(path, payload, **options):
        original(path, payload, **options)
        gate_path.write_text(json.dumps(updated))
    with mock.patch.object(io, "atomic_write_bytes", side_effect=concurrent):
        assert project_source.archive_superseded_project_source_gate(root) is None
    assert json.loads(gate_path.read_text()) == updated


@pytest.fixture
def restored_manual_edit(imported):
    root, workspaces, status, gate_path, session_path, gate, session = imported
    source_path = Path(status["source"]["path"])
    source_path.write_bytes(source_path.read_bytes() + b"\n// saved native editor change\n")
    workspaces.compile(root)
    status = workspaces.status(root)
    current = status["source"]["currentSha256"]
    proposal_path = session_path.parent / "organized-proposal.hlsl"
    proposal_path.write_bytes(source_path.read_bytes())
    session["proposalSourcePath"] = proposal_path.relative_to(root).as_posix()
    session["syncedDraftSourceSha256"] = current
    # Native save updates the real proposal and synchronized draft hash. The
    # organization summary can still describe the older AI-authored proposal.
    assert session["proposalSourceSha256"] != current
    session_path.write_text(json.dumps(session))
    gate["proposalSourceSha256"] = current
    gate["finalBinding"]["sourceSha256"] = current
    gate_path.write_text(json.dumps(gate))
    latest = status["latestCompiled"]
    record = dict(knowledgeId="2" * 32, managedWorkspace={"workspaceId": status["workspaceId"]},
                  identity={"rawSha256": status["baseline"]["rawSha256"],
                            "stage": status["stage"], "entryPoint": status["entryPoint"]},
                  sourceSha256=current, candidateRevision=latest["revision"],
                  candidateRawSha256=latest["rawSha256"])
    applied = dict(status="applied", replacementId="3" * 32, workspaceId=status["workspaceId"],
                   workspacePath=str(root))
    return imported, status, record, applied, proposal_path


def test_applied_published_manual_edit_releases_its_obsolete_draft_pointer(restored_manual_edit):
    imported, status, record, applied, proposal_path = restored_manual_edit
    root, workspaces, _, gate_path, session_path, gate, session = imported
    before = {path: path.read_bytes() for path in (gate_path, session_path, proposal_path)}
    archived = project_source.archive_superseded_project_source_gate(
        root, restored_record=record, applied_item=applied)
    assert archived is not None
    assert Path(archived).read_bytes() == before[gate_path]
    assert not gate_path.exists()
    assert active_draft_id(root) == ""
    assert session_path.read_bytes() == before[session_path]
    assert proposal_path.read_bytes() == before[proposal_path]
    receipt_path = Path(archived).with_name(Path(archived).stem + "-replacement.json")
    receipt = json.loads(receipt_path.read_text())
    assert receipt["currentSourceAlreadyApplied"] is True
    assert receipt["sourceSha256"] == status["source"]["currentSha256"]
    assert receipt["candidateRawSha256"] == record["candidateRawSha256"]
    assert receipt["reviewTransferred"] is False
    assert receipt["requiresCompile"] is True
    assert receipt["requiresLiveAbiRevalidation"] is True


@pytest.mark.parametrize("difference", [
    "proposal", "canonical", "candidate-bytes", "candidate-record", "revision",
    "project-source", "project-workspace", "not-applied", "applied-workspace",
    "applied-path", "active-review", "synced-draft", "original-shader",
])
def test_unproven_or_newer_drafts_are_not_retired(restored_manual_edit, difference):
    imported, status, record, applied, proposal_path = restored_manual_edit
    root, workspaces, _, gate_path, session_path, gate, session = imported
    if difference == "proposal":
        proposal_path.write_bytes(proposal_path.read_bytes() + b"// unfinished new proposal\n")
    elif difference == "canonical":
        Path(status["source"]["path"]).write_text("// unsaved next editor revision\n")
    elif difference == "candidate-bytes":
        manifest = json.loads((root / "manifest.json").read_text())
        (root / manifest["latestCompiled"]["blobPath"]).write_bytes(b"altered candidate")
    elif difference == "candidate-record":
        record["candidateRawSha256"] = "0" * 64
    elif difference == "revision":
        record["candidateRevision"] += 1
    elif difference == "project-source":
        record["sourceSha256"] = "0" * 64
    elif difference == "project-workspace":
        record["managedWorkspace"]["workspaceId"] = "0" * 32
    elif difference == "not-applied":
        applied["status"] = "failed"
    elif difference == "applied-workspace":
        applied["workspaceId"] = "0" * 32
    elif difference == "applied-path":
        applied["workspacePath"] = str(root.parent / "other-workspace")
    elif difference == "active-review":
        gate["decision"] = "organization-beginning"
        gate_path.write_text(json.dumps(gate))
    elif difference == "synced-draft":
        session["syncedDraftSourceSha256"] = "0" * 64
        session_path.write_text(json.dumps(session))
    else:
        record["identity"]["rawSha256"] = "0" * 64
    before = gate_path.read_bytes()
    assert project_source.archive_superseded_project_source_gate(
        root, restored_record=record, applied_item=applied) is None
    assert gate_path.read_bytes() == before
    assert active_draft_id(root) == gate["organizationId"]


def test_new_proposal_written_during_retirement_keeps_its_gate(restored_manual_edit):
    imported, status, record, applied, proposal_path = restored_manual_edit
    root, workspaces, _, gate_path, session_path, gate, session = imported
    from src.shader_workspace import io
    original = io.atomic_write_bytes
    before = gate_path.read_bytes()
    newer = proposal_path.read_bytes() + b"// concurrent new work\n"
    def concurrent(path, payload, **options):
        original(path, payload, **options)
        proposal_path.write_bytes(newer)
    with mock.patch.object(io, "atomic_write_bytes", side_effect=concurrent):
        assert project_source.archive_superseded_project_source_gate(
            root, restored_record=record, applied_item=applied) is None
    assert gate_path.read_bytes() == before
    assert proposal_path.read_bytes() == newer
