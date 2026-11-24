"""Draft writing is a plain file operation, without a task-workflow gate."""

from __future__ import annotations

import hashlib
import json
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest

from src import server
from src.shader_workspace.organization import ShaderOrganizationManager


ORGANIZATION_ID = "a" * 32


def test_proposal_write_updates_only_the_requested_draft(monkeypatch, tmp_path) -> None:
    session_dir = tmp_path / "ai" / "organization-sessions" / ORGANIZATION_ID
    session_dir.mkdir(parents=True)
    proposal = session_dir / "proposal.hlsl"
    proposal.write_text("old source", encoding="utf-8")
    analysis = session_dir / "analysis.txt"
    analysis.write_text("existing analysis", encoding="utf-8")
    (session_dir / "session.json").write_text(json.dumps({
        "organizationId": ORGANIZATION_ID,
        "state": "drafting",
        "proposalSourcePath": str(proposal),
        "analysisPath": str(analysis),
    }), encoding="utf-8")
    calls: list[str] = []

    class Manager:
        _guard = threading.RLock()

        def authorize_edit(self, _root, _organization_id, *, status=None):
            calls.append("authorize")

        def checkpoint(self, _root, _organization_id, *, status=None):
            calls.append("checkpoint")

    monkeypatch.setattr(server, "_shader_workspace_service", lambda: SimpleNamespace(
        source_edit_status=lambda _path: {"workspacePath": str(tmp_path)}))
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: Manager())
    expected = hashlib.sha256(b"old source").hexdigest()

    result = json.loads(server.write_shader_workspace_organization_file(
        str(tmp_path), ORGANIZATION_ID, "proposal", "new source", expected
    ))

    assert result["sha256"] == hashlib.sha256(b"new source").hexdigest()
    assert result["applied"] is False
    assert proposal.read_text(encoding="utf-8") == "new source"
    assert analysis.read_text(encoding="utf-8") == "existing analysis"
    assert calls == ["authorize", "checkpoint"]
    assert "requiredAction" not in result


def test_stale_proposal_hash_does_not_overwrite(monkeypatch, tmp_path) -> None:
    session_dir = tmp_path / "ai" / "organization-sessions" / ORGANIZATION_ID
    session_dir.mkdir(parents=True)
    proposal = session_dir / "proposal.hlsl"
    proposal.write_text("current", encoding="utf-8")
    (session_dir / "session.json").write_text(json.dumps({
        "organizationId": ORGANIZATION_ID,
        "state": "drafting",
        "proposalSourcePath": str(proposal),
    }), encoding="utf-8")

    class Manager:
        _guard = threading.RLock()

        def authorize_edit(self, _root, _organization_id, *, status=None):
            pass

    monkeypatch.setattr(server, "_shader_workspace_service", lambda: SimpleNamespace(
        source_edit_status=lambda _path: {"workspacePath": str(tmp_path)}))
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: Manager())

    result = json.loads(server.write_shader_workspace_organization_file(
        str(tmp_path), ORGANIZATION_ID, "proposal", "new", "f" * 64
    ))

    assert result["error"]
    assert proposal.read_text(encoding="utf-8") == "current"


def test_absolute_session_path_must_resolve_inside_ai_tree(monkeypatch, tmp_path) -> None:
    session_dir = tmp_path / "ai" / "organization-sessions" / ORGANIZATION_ID
    session_dir.mkdir(parents=True)
    outside = tmp_path / "source" / "shader.hlsl"
    outside.parent.mkdir()
    outside.write_text("original", encoding="utf-8")
    (session_dir / "session.json").write_text(json.dumps({
        "organizationId": ORGANIZATION_ID,
        "state": "drafting",
        "proposalSourcePath": str(outside.resolve()),
    }), encoding="utf-8")

    class Manager:
        _guard = threading.RLock()

        def authorize_edit(self, _root, _organization_id, *, status=None):
            pass

    monkeypatch.setattr(server, "_shader_workspace_service", lambda: SimpleNamespace(
        source_edit_status=lambda _path: {"workspacePath": str(tmp_path)}))
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: Manager())

    result = json.loads(server.write_shader_workspace_organization_file(
        str(tmp_path), ORGANIZATION_ID, "proposal", "replacement"
    ))

    assert result["error"]
    assert outside.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize("outside_ai_tree", [False, True])
def test_flowchart_write_uses_session_path_without_touching_shader(
    monkeypatch, tmp_path, outside_ai_tree
) -> None:
    session_dir = tmp_path / "ai" / "organization-sessions" / ORGANIZATION_ID
    session_dir.mkdir(parents=True)
    source = tmp_path / "source" / "shader.hlsl"
    source.parent.mkdir()
    source.write_bytes(b"canonical shader")
    proposal = session_dir / "proposal.hlsl"
    proposal.write_bytes(b"organization draft")
    flowchart = (source if outside_ai_tree else session_dir / "flowchart.mmd")
    session = {
        "organizationId": ORGANIZATION_ID,
        "state": "drafting",
        "proposalSourcePath": str(proposal),
        "flowchartPath": str(flowchart),
    }
    (session_dir / "session.json").write_text(json.dumps(session), encoding="utf-8")
    calls = []

    class Manager:
        _guard = threading.RLock()

        def authorize_edit(self, _root, _organization_id, *, status=None):
            calls.append("authorize")

        def checkpoint(self, _root, _organization_id, *, status=None):
            calls.append("checkpoint")

    monkeypatch.setattr(server, "_shader_workspace_service", lambda: SimpleNamespace(
        source_edit_status=lambda _path: {"workspacePath": str(tmp_path)}))
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: Manager())
    content = "flowchart TD\n  Input --> Calculation --> Output\n"
    result = json.loads(server.write_shader_workspace_organization_file(
        str(tmp_path), ORGANIZATION_ID, "flowchart", content
    ))
    assert source.read_bytes() == b"canonical shader"
    assert proposal.read_bytes() == b"organization draft"
    if outside_ai_tree:
        assert result["error"]
        assert calls == ["authorize"]
    else:
        assert result["kind"] == "flowchart"
        assert result["path"] == str(flowchart)
        assert result["sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert result["applied"] is False
        assert flowchart.read_text(encoding="utf-8") == content
        assert calls == ["authorize", "checkpoint"]


@pytest.fixture
def closed_explanation(monkeypatch, tmp_path):
    directory = tmp_path / "ai" / "organization-sessions" / ORGANIZATION_ID
    directory.mkdir(parents=True)
    source = tmp_path / "source" / "shader.hlsl"
    source.parent.mkdir()
    source.write_bytes(b"// current saved shader\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    analysis = directory / "analysis.md"
    analysis.write_bytes(b"old explanation")
    flowchart = directory / "flowchart.mmd"
    flowchart.write_bytes(b"old diagram")
    status = {
        "workspacePath": str(tmp_path), "workspaceId": "b" * 32,
        "manifestPath": str(tmp_path / "manifest.json"),
        "stage": "fragment", "entryPoint": "main", "revision": 8,
        "shader": {"rawSha256": "c" * 64},
        "baseline": {"rawSha256": "c" * 64},
        "source": {"path": str(source), "currentSha256": digest, "present": True,
                   "compileEligible": False},
    }
    session = {
        "organizationId": ORGANIZATION_ID, "state": "completed",
        "originalRawSha256": "c" * 64, "stage": "fragment", "entryPoint": "main",
        "analysisPath": str(analysis), "flowchartPath": str(flowchart),
        "finalBinding": {"sourceSha256": digest},
    }
    session_path = directory / "session.json"
    session_path.write_text(json.dumps(session), encoding="utf-8")
    authority = {}
    for relative in ("manifest.json", "compiled/candidate.bin", "ai/source-gate.json", "ai/flow-graph.json", "ai/flow-spec.json"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("authority " + relative).encode())
        authority[path] = path.read_bytes()

    class Manager:
        _guard = threading.RLock()
        _mutation_store = tmp_path.parent / (tmp_path.name + "-host-mutations")

        def _same_shader_program(self, stored, current):
            return ShaderOrganizationManager._same_shader_program(stored, current)

        def authorize_edit(self, *_args):
            pytest.fail("closed explanation reopened a source draft")

        def checkpoint(self, *_args):
            pytest.fail("closed explanation rewrote acceptance/session authority")

    class Service:
        def status(self, *_args):
            return status

    monkeypatch.setattr(server, "_shader_workspace_service", lambda: SimpleNamespace(
        source_edit_status=lambda _path: {"workspacePath": str(tmp_path)}))
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: Service())
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: Manager())
    return tmp_path, source, directory, status, session, session_path, authority


@pytest.mark.parametrize("kind", ["analysis", "flowchart"])
@pytest.mark.parametrize("state", ["completed", "abandoned"])
def test_closed_explanation_is_current_source_bound_without_changing_authority(closed_explanation, kind, state):
    root, source, directory, status, session, session_path, authority = closed_explanation
    session["state"] = state
    # Volatile workspace/capture/session/revision differences do not confer or
    # revoke permission to describe the same captured program's current source.
    session["workspaceId"] = "old-workspace"
    session["captureId"] = "old-capture"
    session["finalBinding"]["sourceSha256"] = "f" * 64
    session_path.write_text(json.dumps(session), encoding="utf-8")
    authority[session_path] = session_path.read_bytes()
    authority[source] = source.read_bytes()
    result = json.loads(server.write_shader_workspace_organization_file(
        str(root), ORGANIZATION_ID, kind, "current explanation"
    ))
    assert "error" not in result
    assert result["artifactOnly"] is True
    assert result["applied"] is False
    assert result["sourceBinding"]["sourceSha256"] == status["source"]["currentSha256"]
    assert result["sourceBinding"]["matchesOrganizationFinalSource"] is False
    assert result["sourceBinding"]["acceptanceAuthorization"] is False
    assert result["sourceBinding"]["sourceChangedDuringWrite"] is False
    artifact = Path(result["path"])
    assert artifact.read_text() == "current explanation"
    assert json.loads(artifact.with_name(artifact.name + ".source-binding.json").read_text()) == result["sourceBinding"]
    canonical = Path(result["canonicalPublication"]["path"])
    assert canonical == root / "ai" / ("analysis.md" if kind == "analysis" else "flowchart.mmd")
    assert canonical.read_bytes() == artifact.read_bytes()
    assert json.loads(canonical.with_name(canonical.name + ".source-binding.json").read_text()) == result["sourceBinding"]
    for path, original in authority.items():
        assert path.read_bytes() == original


@pytest.mark.parametrize("field,value", [
    ("originalRawSha256", "d" * 64), ("stage", "vertex"), ("entryPoint", "otherMain"),
])
def test_closed_explanation_rejects_a_different_program(closed_explanation, field, value):
    root, source, directory, status, session, session_path, authority = closed_explanation
    session[field] = value
    session_path.write_text(json.dumps(session), encoding="utf-8")
    result = json.loads(server.write_shader_workspace_organization_file(
        str(root), ORGANIZATION_ID, "analysis", "wrong explanation"
    ))
    assert "different captured Shader program" in result["error"]
    assert (directory / "analysis.md").read_bytes() == b"old explanation"
    assert not (directory / "analysis.md.source-binding.json").exists()


def test_closed_session_never_allows_proposal_source_write(closed_explanation):
    root, source, directory, status, session, session_path, authority = closed_explanation
    result = json.loads(server.write_shader_workspace_organization_file(
        str(root), ORGANIZATION_ID, "proposal", "new shader"
    ))
    assert "no longer editable" in result["error"]
    assert source.read_bytes() == b"// current saved shader\n"


def test_closed_explanation_cannot_write_a_gate_even_if_session_path_claims_it(closed_explanation):
    root, source, directory, status, session, session_path, authority = closed_explanation
    session["analysisPath"] = str(root / "ai" / "source-gate.json")
    session_path.write_text(json.dumps(session), encoding="utf-8")
    result = json.loads(server.write_shader_workspace_organization_file(
        str(root), ORGANIZATION_ID, "analysis", "must not overwrite gate"
    ))
    assert "explanation path" in result["error"]
    assert (root / "ai" / "source-gate.json").read_bytes() == authority[root / "ai" / "source-gate.json"]


def test_closed_explanation_checks_supplied_source_hash(closed_explanation):
    root, source, directory, status, session, session_path, authority = closed_explanation
    result = json.loads(server.write_shader_workspace_organization_file(
        str(root), ORGANIZATION_ID, "analysis", "stale", expected_source_sha256="f" * 64
    ))
    assert "supplied explanation binding" in result["error"]
    assert (directory / "analysis.md").read_bytes() == b"old explanation"


def test_source_edit_before_explanation_write_is_preserved_and_rejected(monkeypatch, closed_explanation):
    root, source, directory, status, session, session_path, authority = closed_explanation
    original = server._bounded_file_sha256
    calls = 0

    def concurrent_source(path, *args):
        nonlocal calls
        if path == source:
            calls += 1
            if calls == 2:
                source.write_bytes(b"user concurrent edit")
        return original(path, *args)

    monkeypatch.setattr(server, "_bounded_file_sha256", concurrent_source)
    result = json.loads(server.write_shader_workspace_organization_file(
        str(root), ORGANIZATION_ID, "analysis", "stale"
    ))
    assert "Source changed before" in result["error"]
    assert source.read_bytes() == b"user concurrent edit"
    assert (directory / "analysis.md").read_bytes() == b"old explanation"


def test_source_edit_during_explanation_write_is_preserved_and_rolls_back_documents(monkeypatch, closed_explanation):
    from src.shader_workspace import workspace_mutation_transaction as transaction
    root, source, directory, status, session, session_path, authority = closed_explanation
    original = transaction.atomic_write_bytes

    def concurrent_source(path, data):
        original(path, data)
        if path == directory / "analysis.md":
            source.write_bytes(b"user concurrent edit")

    monkeypatch.setattr(transaction, "atomic_write_bytes", concurrent_source)
    result = json.loads(server.write_shader_workspace_organization_file(
        str(root), ORGANIZATION_ID, "analysis", "observed older source"
    ))
    assert "Source changed during explanation publication" in result["error"]
    assert source.read_bytes() == b"user concurrent edit"
    assert (directory / "analysis.md").read_bytes() == b"old explanation"
    assert not (root / "ai" / "analysis.md").exists()
    assert not (root / "ai" / "analysis.md.source-binding.json").exists()


def test_real_completed_source_and_abi_gate_remain_valid_after_explanation_updates(monkeypatch):
    import test_shader_organization as harness
    from unittest import mock

    case = harness.ShaderOrganizationTests(methodName="runTest")
    case.setUp()
    try:
        opened = case._begin()
        finished = case.manager.finish_draft(str(case.root), opened["organizationId"])
        assert finished["wholeSourceValidated"] is True
        gate_before = case.manager.validate_publication_gate(str(case.root))
        status_before = case.service.status(str(case.root))
        explanation_paths = {Path(opened["analysisPath"]), Path(opened["flowchartPath"]),
                             case.root / "ai" / "analysis.md", case.root / "ai" / "flowchart.mmd"}
        preserved = {path: path.read_bytes() for path in case.root.rglob("*")
                     if path.is_file() and path not in explanation_paths}
        monkeypatch.setattr(server, "_workspace_root_for_files", lambda _path: case.root)
        monkeypatch.setattr(server, "_shader_workspace_service", lambda: case.service)
        monkeypatch.setattr(server, "_shader_organization_manager", lambda: case.manager)
        with mock.patch.object(case.service, "compile", side_effect=AssertionError("explanation must not compile")), \
             mock.patch.object(case.manager, "_validate_candidate", side_effect=AssertionError("explanation must not redo ABI")):
            for kind in ("analysis", "flowchart"):
                result = json.loads(server.write_shader_workspace_organization_file(
                    str(case.root), opened["organizationId"], kind, "updated explanation"
                ))
                assert "error" not in result
            assert case.manager.validate_publication_gate(str(case.root)) == gate_before
            assert case.service.status(str(case.root)) == status_before
        for path, original in preserved.items():
            assert path.read_bytes() == original
    finally:
        case.tearDown()
        case.doCleanups()
