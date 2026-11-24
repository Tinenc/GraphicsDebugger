"""Public docs are current Source-bound artifacts, without replay side effects."""

import hashlib
import json
from pathlib import Path

import pytest

from src import server
from src.active_projects import ActiveReplayProjectService, ActiveProjectError
from src.shader_workspace import explanations, workspace_mutation_transaction as transaction
from test_shader_workflow_publication_scope import closed_explanation, ORGANIZATION_ID


def _write(root, kind="analysis", content="Current saved Source explanation"):
    return json.loads(server.write_shader_workspace_organization_file(
        str(root), ORGANIZATION_ID, kind, content))


@pytest.mark.parametrize("kind,name", [("analysis", "analysis.md"), ("flowchart", "flowchart.mmd")])
def test_public_reader_observes_published_source_bound_document(closed_explanation, kind, name):
    root, source, directory, status, session, session_path, authority = closed_explanation
    canonical = root / "ai" / name
    canonical.write_bytes(b"legacy r92 explanation")
    result = _write(root, kind)
    assert "error" not in result
    assert result["canonicalPublication"]["status"] == "published"
    read = json.loads(server.read_shader_workspace_file(str(root), "ai/" + name))
    assert read["text"] == "Current saved Source explanation"
    assert read["explanationProvenance"]["status"] == "current"
    assert read["explanationProvenance"]["currentSourceMatches"] is True
    assert read["explanationProvenance"]["acceptanceAuthorization"] is False
    assert (directory / name).read_bytes() == canonical.read_bytes()
    for path, original in authority.items():
        assert path.read_bytes() == original


def test_explanation_read_reuses_one_status_and_rechecks_next_revision(monkeypatch, closed_explanation):
    root, source, directory, status, *_ = closed_explanation
    assert "error" not in _write(root)
    service = server._shader_workspace_service()
    previous = service.status
    calls = []

    def counted(path):
        current = previous(path)
        calls.append(current["revision"])
        return current

    monkeypatch.setattr(service, "status", counted)
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    first = json.loads(server.read_shader_workspace_file(str(root), "ai/analysis.md"))
    assert first["explanationProvenance"]["currentSourceMatches"] is True
    assert first["explanationProvenance"]["acceptanceAuthorization"] is False
    assert calls == [8]

    source.write_bytes(b"// changed source at revision 9\n")
    status["revision"] = 9
    status["source"]["currentSha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    second = json.loads(server.read_shader_workspace_file(str(root), "ai/analysis.md"))
    assert second["text"] == first["text"]
    assert second["explanationProvenance"]["status"] == "stale"
    assert second["explanationProvenance"]["currentSourceMatches"] is False
    assert second["explanationProvenance"]["acceptanceAuthorization"] is False
    assert calls == [8, 9]


@pytest.mark.parametrize("which", ["artifact", "binding"])
def test_canonical_compare_and_swap_preserves_edit_after_snapshot(monkeypatch, closed_explanation, which):
    root, source, directory, status, *_ = closed_explanation
    canonical = root / "ai" / "analysis.md"
    canonical.write_bytes(b"legacy explanation")
    changed = canonical if which == "artifact" else explanations.binding_path(canonical)
    original = explanations.apply_transaction

    def race(**kwargs):
        changed.write_bytes(b"user changed canonical " + which.encode())
        return original(**kwargs)

    monkeypatch.setattr(explanations, "apply_transaction", race)
    result = _write(root)
    assert "target changed before its transaction" in result["error"]
    assert changed.read_bytes() == b"user changed canonical " + which.encode()
    assert (directory / "analysis.md").read_bytes() == b"old explanation"
    assert source.read_bytes() == b"// current saved shader\n"


def test_canonical_user_edit_after_bound_publication_is_not_overwritten(closed_explanation):
    root, source, directory, status, *_ = closed_explanation
    assert "error" not in _write(root)
    canonical = root / "ai" / "analysis.md"
    canonical.write_bytes(b"user annotation since publication")
    result = _write(root, content="would discard user annotation")
    assert "canonical explanation changed outside" in result["error"]
    assert canonical.read_bytes() == b"user annotation since publication"
    read = json.loads(server.read_shader_workspace_file(str(root), "ai/analysis.md"))
    assert read["explanationProvenance"]["currentSourceMatches"] is False
    assert read["text"] == "user annotation since publication"


def test_observed_canonical_user_annotation_can_be_merged_without_a_sidecar_gate(closed_explanation):
    root, source, directory, status, *_ = closed_explanation
    assert "error" not in _write(root)
    canonical = root / "ai" / "analysis.md"
    canonical.write_bytes(b"user annotation since publication")
    observed = json.loads(server.read_shader_workspace_file(str(root), "ai/analysis.md"))
    assert observed["sha256"] == hashlib.sha256(canonical.read_bytes()).hexdigest()
    result = json.loads(server.write_shader_workspace_organization_file(
        str(root), ORGANIZATION_ID, "analysis", observed["text"] + "\nMerged current explanation",
        expected_canonical_sha256=observed["sha256"],
    ))
    assert "error" not in result
    assert canonical.read_text() == "user annotation since publication\nMerged current explanation"
    assert (directory / "analysis.md").read_bytes() == canonical.read_bytes()
    reread = json.loads(server.read_shader_workspace_file(str(root), "ai/analysis.md"))
    assert reread["explanationProvenance"]["currentSourceMatches"] is True
    assert source.read_bytes() == b"// current saved shader\n"


def test_observed_canonical_content_cas_rejects_later_user_edit(closed_explanation):
    root, source, directory, status, *_ = closed_explanation
    assert "error" not in _write(root)
    canonical = root / "ai" / "analysis.md"
    canonical.write_bytes(b"observed user annotation")
    observed = json.loads(server.read_shader_workspace_file(str(root), "ai/analysis.md"))
    canonical.write_bytes(b"later user annotation")
    result = json.loads(server.write_shader_workspace_organization_file(
        str(root), ORGANIZATION_ID, "analysis", "merged observed annotation",
        expected_canonical_sha256=observed["sha256"],
    ))
    assert "changed since it was read" in result["error"]
    assert canonical.read_bytes() == b"later user annotation"


def test_source_and_manifest_race_rolls_back_only_documents(monkeypatch, closed_explanation):
    root, source, directory, status, *_ = closed_explanation
    canonical = root / "ai" / "analysis.md"
    canonical.write_bytes(b"legacy explanation")
    manifest = Path(status["manifestPath"])
    original = transaction.atomic_write_bytes

    def race(path, data):
        original(path, data)
        if path == canonical:
            manifest.write_bytes(b"user changed Source path/revision in manifest")

    monkeypatch.setattr(transaction, "atomic_write_bytes", race)
    result = _write(root)
    assert "manifest changed during" in result["error"]
    assert manifest.read_bytes() == b"user changed Source path/revision in manifest"
    assert canonical.read_bytes() == b"legacy explanation"
    assert (directory / "analysis.md").read_bytes() == b"old explanation"
    assert source.read_bytes() == b"// current saved shader\n"


@pytest.mark.parametrize("changed", ["source", "artifact", "binding"])
def test_public_read_marks_racing_pair_stale(monkeypatch, closed_explanation, changed):
    root, source, directory, status, *_ = closed_explanation
    assert "error" not in _write(root)
    canonical = root / "ai" / "analysis.md"
    sidecar = explanations.binding_path(canonical)
    original = explanations._regular_snapshot
    reads = 0

    def race(base, path, limit):
        nonlocal reads
        if path == sidecar:
            reads += 1
            if reads == 2:
                target = {"source": source, "artifact": canonical, "binding": sidecar}[changed]
                target.write_bytes(b"user read-race edit")
        return original(base, path, limit)

    monkeypatch.setattr(explanations, "_regular_snapshot", race)
    result = json.loads(server.read_shader_workspace_file(str(root), "ai/analysis.md"))
    assert result["explanationProvenance"]["currentSourceMatches"] is False


def test_concurrent_canonical_edit_during_commit_is_preserved(monkeypatch, closed_explanation):
    root, source, directory, status, *_ = closed_explanation
    canonical = root / "ai" / "analysis.md"
    canonical.write_bytes(b"legacy explanation")
    original = transaction.atomic_write_bytes

    def race(path, data):
        original(path, data)
        if path == canonical:
            path.write_bytes(b"user concurrent canonical edit")

    monkeypatch.setattr(transaction, "atomic_write_bytes", race)
    result = _write(root)
    assert "error" in result
    assert canonical.read_bytes() == b"user concurrent canonical edit"
    assert source.read_bytes() == b"// current saved shader\n"
    # Unknown bytes cannot be rolled back. A partially published pair is
    # visibly stale instead of becoming accepted Shader or current analysis.
    read = json.loads(server.read_shader_workspace_file(str(root), "ai/analysis.md"))
    assert read["explanationProvenance"]["currentSourceMatches"] is False


def _create_pending_document_conflict(monkeypatch, root):
    canonical = root / "ai" / "analysis.md"
    canonical.write_bytes(b"legacy explanation")
    original = transaction.atomic_write_bytes

    def race(path, data):
        original(path, data)
        if path == canonical:
            path.write_bytes(b"user annotation during commit")

    with monkeypatch.context() as patch:
        patch.setattr(transaction, "atomic_write_bytes", race)
        assert "error" in _write(root)
    return canonical


@pytest.mark.parametrize("edit_after_read", [False, True])
def test_mid_commit_conflict_can_be_observed_and_merged_without_old_journal_gate(
    monkeypatch, closed_explanation, edit_after_read
):
    root, source, directory, status, session, session_path, authority = closed_explanation
    canonical = _create_pending_document_conflict(monkeypatch, root)
    observed = json.loads(server.read_shader_workspace_file(str(root), "ai/analysis.md"))
    store = server._shader_organization_manager()._mutation_store / "explanation-publications"
    ledger = transaction.transaction_directory(store, root, ORGANIZATION_ID) / "ledger.json"
    assert ledger.exists()
    if edit_after_read:
        canonical.write_bytes(b"user second annotation after read")
    # A later closed session for the same program can describe current Source;
    # an old organization ID is neither recovery permission nor a new gate.
    new_id = "d" * 32
    new_session = root / "ai" / "organization-sessions" / new_id
    new_session.mkdir()
    updated = dict(session, organizationId=new_id, analysisPath=str(new_session / "analysis.md"))
    (new_session / "session.json").write_text(json.dumps(updated))
    result = json.loads(server.write_shader_workspace_organization_file(
        str(root), new_id, "analysis", observed["text"] + "\nMerged current explanation",
        expected_canonical_sha256=observed["sha256"],
    ))
    if edit_after_read:
        assert "changed since it was read" in result["error"]
        assert canonical.read_bytes() == b"user second annotation after read"
        assert ledger.exists()
    else:
        assert "error" not in result
        assert canonical.read_text() == "user annotation during commit\nMerged current explanation"
        assert not ledger.exists()
        audits = [json.loads(p.read_text()) for p in (store / "retired-intents").glob("*.json")]
        assert len(audits) == 1 and audits[0]["status"] == "retired"
        assert audits[0]["targetsWritten"] is False
        assert audits[0]["observedCanonicalSha256"] == observed["sha256"]
        assert json.loads(server.read_shader_workspace_file(str(root), "ai/analysis.md"))["explanationProvenance"]["currentSourceMatches"] is True
    for path, original in authority.items():
        assert path.read_bytes() == original
    assert source.read_bytes() == b"// current saved shader\n"


def test_pending_intent_retirement_preserves_an_edit_during_retirement(monkeypatch, closed_explanation):
    root, source, *_ = closed_explanation
    canonical = _create_pending_document_conflict(monkeypatch, root)
    observed = json.loads(server.read_shader_workspace_file(str(root), "ai/analysis.md"))
    original = explanations.atomic_write_json

    def race(path, value):
        original(path, value)
        if value.get("status") == "retirement-prepared":
            canonical.write_bytes(b"user annotation during retirement")

    monkeypatch.setattr(explanations, "atomic_write_json", race)
    result = json.loads(server.write_shader_workspace_organization_file(
        str(root), ORGANIZATION_ID, "analysis", "merged observed annotation",
        expected_canonical_sha256=observed["sha256"],
    ))
    assert "changed while its old intent was retired" in result["error"]
    assert canonical.read_bytes() == b"user annotation during retirement"
    assert source.read_bytes() == b"// current saved shader\n"


@pytest.mark.parametrize("tamper", ["target", "payload", "missing_payload", "tool", "phase"])
def test_unknown_pending_intent_cannot_be_retired(monkeypatch, closed_explanation, tamper):
    root, source, *_ = closed_explanation
    canonical = _create_pending_document_conflict(monkeypatch, root)
    observed = json.loads(server.read_shader_workspace_file(str(root), "ai/analysis.md"))
    store = server._shader_organization_manager()._mutation_store / "explanation-publications"
    directory = transaction.transaction_directory(store, root, ORGANIZATION_ID)
    ledger_path = directory / "ledger.json"
    ledger = json.loads(ledger_path.read_text())
    if tamper == "target":
        ledger["targets"][0]["path"] = str(source)
    elif tamper == "payload":
        (directory / ledger["targets"][0]["afterFile"]).write_bytes(b"tampered payload")
    elif tamper == "missing_payload":
        (directory / ledger["targets"][0]["afterFile"]).unlink()
    elif tamper == "tool":
        ledger["tool"] = "restructure-source"
    else:
        ledger["phase"] = "unknown"
    ledger_path.write_text(json.dumps(ledger))
    preserved = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = json.loads(server.write_shader_workspace_organization_file(
        str(root), ORGANIZATION_ID, "analysis", "must not apply",
        expected_canonical_sha256=observed["sha256"],
    ))
    assert "error" in result
    assert ledger_path.exists()
    for path, raw in preserved.items():
        assert path.read_bytes() == raw


def test_validated_retirement_cleanup_interruption_is_retryable(monkeypatch, closed_explanation):
    root, source, directory, status, session, session_path, authority = closed_explanation
    canonical = _create_pending_document_conflict(monkeypatch, root)
    observed = json.loads(server.read_shader_workspace_file(str(root), "ai/analysis.md"))
    original = transaction._cleanup

    def interrupted(directory, rows):
        (directory / rows[0]["afterFile"]).unlink()
        raise OSError("simulated cleanup interruption")

    with monkeypatch.context() as patch:
        patch.setattr(transaction, "_cleanup", interrupted)
        result = json.loads(server.write_shader_workspace_organization_file(
            str(root), ORGANIZATION_ID, "analysis", "merged current annotation",
            expected_canonical_sha256=observed["sha256"],
        ))
        assert "simulated cleanup interruption" in result["error"]
    assert canonical.read_bytes() == b"user annotation during commit"
    assert source.read_bytes() == b"// current saved shader\n"
    result = json.loads(server.write_shader_workspace_organization_file(
        str(root), ORGANIZATION_ID, "analysis", "merged current annotation",
        expected_canonical_sha256=observed["sha256"],
    ))
    assert "error" not in result
    assert canonical.read_bytes() == b"merged current annotation"
    store = server._shader_organization_manager()._mutation_store / "explanation-publications"
    audits = [json.loads(p.read_text()) for p in (store / "retired-intents").glob("*.json")]
    assert sorted(a["status"] for a in audits) == ["retired", "retirement-refused"]
    assert all(a["targetsWritten"] is False for a in audits)
    for path, raw in authority.items():
        assert path.read_bytes() == raw


@pytest.mark.parametrize("which", ["artifact", "binding"])
def test_redirected_canonical_target_is_rejected(monkeypatch, closed_explanation, which):
    root, source, directory, status, *_ = closed_explanation
    canonical = root / "ai" / "analysis.md"
    target = canonical if which == "artifact" else explanations.binding_path(canonical)
    outside = root.parent / (root.name + "-outside-document")
    outside.write_bytes(b"external user text")
    try:
        try:
            target.symlink_to(outside)
        except OSError:
            pytest.skip("symlink permission unavailable")
        result = _write(root)
        assert "error" in result
        assert outside.read_bytes() == b"external user text"
        assert (directory / "analysis.md").read_bytes() == b"old explanation"
        assert source.read_bytes() == b"// current saved shader\n"
    finally:
        outside.unlink()


def test_public_reader_marks_source_edit_stale_and_flow_spec_historical(closed_explanation):
    root, source, directory, status, *_ = closed_explanation
    assert "error" not in _write(root)
    source.write_bytes(b"// later user Source edit\n")
    status["source"]["currentSha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    result = json.loads(server.read_shader_workspace_file(str(root), "ai/analysis.md"))
    assert result["explanationProvenance"]["currentSourceMatches"] is False
    historical = json.loads(server.read_shader_workspace_file(str(root), "ai/flow-spec.json"))
    assert historical["explanationProvenance"] == {
        "role": "historical-flow-spec", "currentSourceMatches": None,
        "currentUiGraphSource": "ai/flow-graph.json", "acceptanceAuthorization": False,
        "sourceBindingAvailable": False, "declaredSourceSha256": None,
    }
    assert historical["text"] == "authority ai/flow-spec.json"


def test_public_flow_spec_read_reports_declared_binding_without_promoting_ranges(closed_explanation):
    root, source, directory, status, *_ = closed_explanation
    spec = {"sourceSha256": "f" * 64, "stages": [{"sourceRange": [4, 9]}]}
    path = root / "ai" / "flow-spec.json"
    raw = json.dumps(spec).encode()
    path.write_bytes(raw)
    result = json.loads(server.read_shader_workspace_file(str(root), "ai/flow-spec.json"))
    assert result["explanationProvenance"]["declaredSourceSha256"] == "f" * 64
    assert result["explanationProvenance"]["sourceBindingAvailable"] is True
    assert result["explanationProvenance"]["currentSourceMatches"] is None
    assert result["explanationProvenance"]["role"] == "historical-flow-spec"
    assert path.read_bytes() == raw


def _publish_fixture_docs(root, status, store):
    session_dir = root / "ai" / "organization-sessions" / ORGANIZATION_ID
    session_dir.mkdir(parents=True)
    binding = {
        "schema": explanations.BINDING_SCHEMA,
        "sourcePath": str(Path(status["source"]["path"]).resolve()),
        "sourceSha256": status["source"]["currentSha256"],
        "sourceRevision": status.get("revision"),
        "originalRawSha256": status["baseline"]["rawSha256"],
        "stage": status["stage"], "entryPoint": status["entryPoint"],
        "acceptanceAuthorization": False,
    }
    for kind, name in (("analysis", "analysis.md"), ("flowchart", "flowchart.mmd")):
        explanations.publish_closed_explanation(
            store_root=store, root=root, organization_id=ORGANIZATION_ID,
            session_artifact=session_dir / name, kind=kind,
            data=("Current " + kind + " explanation\n").encode(),
            source_binding=binding, status=status,
        )


def test_real_project_analysis_registration_uses_current_canonical_and_skips_stale():
    import test_projects as harness
    case = harness.ReplayProjectTests(methodName="runTest")
    case.setUp()
    try:
        catalog, shader, capture_id = case._catalog("bound-docs", harness._dxbc(), resource_id=91)
        case.service.import_capture(catalog)
        status = case._workspace_status("bound-docs-workspace", capture_id, shader,
                                        "float4 main() : SV_Target { return 1; }\n")
        root = Path(status["workspacePath"])
        _publish_fixture_docs(root, status, case.root / "host-mutations")
        result = case.service.register_workspace(status, include_compiled_candidate=False)
        record = result["knowledge"]
        assert (case.project / record["analysisPath"]).read_bytes() == b"Current analysis explanation\n"
        assert ActiveReplayProjectService._read_flowchart(status)[0] == b"Current flowchart explanation\n"
        source = Path(status["source"]["path"])
        source.write_bytes(b"float4 main() : SV_Target { return 0.5; }\n")
        status["source"]["currentSha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        later = case.service.register_workspace(status, include_compiled_candidate=False)
        assert "analysisPath" not in later["knowledge"]
        assert later["explanatoryDiagnostics"][0]["artifact"] == "ai/analysis.md"
        with pytest.raises(ActiveProjectError, match="current workspace Source"):
            ActiveReplayProjectService._read_flowchart(status)
        assert (root / "ai" / "analysis.md").read_bytes() == b"Current analysis explanation\n"
    finally:
        case.tearDown()


def test_real_inventory_registration_preserves_candidate_and_skips_changed_bound_analysis():
    import test_projects as harness
    case = harness.ReplayProjectTests(methodName="runTest")
    case.setUp()
    try:
        unused_catalog, shader, unused_capture = case._catalog(
            "inventory-explanations", harness._dxbc(b"physical baseline"), resource_id=1701)
        inventory = case._shader_inventory(resource_id=9915, raw_sha256=shader["rawSha256"],
                                            layout_hash="1" * 64, interface_hash="2" * 64,
                                            anchor_event_id=23)
        target = dict(shader, shaderResourceId=9915)
        status = case._workspace_status("inventory-explanations-workspace",
                                        inventory["capture"]["captureId"], target,
                                        "float4 main() : SV_Target { return 1; }\n")
        case._with_fully_validated_candidate(status, harness._dxbc(b"physical candidate"), revision=3)
        root = Path(status["workspacePath"])
        _publish_fixture_docs(root, status, case.root / "host-mutations")
        # Binary/malformed external edits to a formerly bound optional document
        # are skipped before UTF-8 validation, preserving physical publication.
        (root / "ai" / "analysis.md").write_bytes(b"\xff user document edit")
        preserved = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
        result = case.service.register_workspace_from_inventory(
            status, inventory["shaders"][0], inventory,
            case.service.reuse_snapshot_for_shader_inventory(inventory)["binding"])
        assert "compiledCandidate" in result["knowledge"]
        assert "analysisPath" not in result["knowledge"]
        assert result["explanatoryDiagnostics"][0]["artifact"] == "ai/analysis.md"
        for path, original in preserved.items():
            assert path.read_bytes() == original
    finally:
        case.tearDown()


def test_actual_console_reader_keeps_current_flow_graph_and_source_unchanged():
    import tempfile
    import test_shader_console as harness
    from src.shader_workspace.flow_graph import build_shader_flow_graph
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        # This test exercises the platform-independent file reader. The wider
        # console fixture uses a Windows capture path; give this isolated case
        # a real OS-local absolute identity rather than relaxing production.
        class PortableFixture(harness.ShaderConsoleFixture):
            @property
            def capture_binding(self):
                return harness.console.normalize_capture_binding({
                    "instanceId": "1234-fixture", "captureSessionId": "8" * 32,
                    "capturePath": str(self.root / "test.rdc")})

            def _write(self, path, value):
                if path == self.workspace / "manifest.json":
                    value["capture"]["capturePath"] = self.capture_binding["capturePath"]
                elif path == self.registry:
                    for entry in value["entries"]:
                        entry["workspaceIdentity"]["capturePath"] = self.capture_binding["capturePath"]
                        entry["workspaceIdentitySha256"] = harness.console._canonical_sha256(entry["workspaceIdentity"])
                super()._write(path, value)
        fixture = PortableFixture(base)
        source = fixture.source.read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        graph = build_shader_flow_graph(source.decode(), workspace_id=fixture.WORKSPACE_ID,
                                       source_path="source/shader.hlsl", source_sha256=digest,
                                       source_revision=7, stage="fragment", entry_point="MainPS",
                                       generated_at="2026-08-31T00:00:00Z", origin="organized-proposal")
        path = fixture.workspace / "ai" / "flow-graph.json"
        fixture._write(path, graph)
        before = harness.console.load_console_snapshot(str(fixture.registry), capture_binding=fixture.capture_binding)
        preserved = {p: p.read_bytes() for p in fixture.workspace.rglob("*") if p.is_file()
                     and p.name not in {"analysis.md", "flowchart.mmd"}}
        _publish_fixture_docs(fixture.workspace, {
            "source": {"path": str(fixture.source), "currentSha256": digest},
            "baseline": {"rawSha256": "a" * 64}, "stage": "fragment", "entryPoint": "MainPS",
        }, base / "host-mutations")
        after = harness.console.load_console_snapshot(str(fixture.registry), capture_binding=fixture.capture_binding)
        assert after["errors"] == before["errors"] == []
        assert after["workspaces"][0]["flowGraph"] == before["workspaces"][0]["flowGraph"]
        assert after["workspaces"][0]["flowGraphStatus"]["status"] == "current"
        for artifact, original in preserved.items():
            assert artifact.read_bytes() == original
