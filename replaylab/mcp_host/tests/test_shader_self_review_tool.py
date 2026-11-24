import hashlib
import json
from pathlib import Path

from src.shader_organization_review import CHECKS, source_scope_catalog, source_snapshot
from src.task_report import TaskReportStore
from test_shader_semantic_overview_review import semantic_assessment


def test_real_organization_draft_review_promote_and_later_edit(tmp_path, monkeypatch):
    from src import server
    from src import task_report
    from test_shader_organization import ShaderOrganizationTests, _FakeWorkspaceService
    fixture = ShaderOrganizationTests()
    fixture.setUp()
    try:
        fixture.root = tmp_path / "shader"
        original = b"float4 MainPS() : SV_Target { return 1.0f.xxxx; }\n"
        fixture.service = _FakeWorkspaceService(fixture.root, original)
        (fixture.root / "manifest.json").write_text(json.dumps({
            "entryPoint": "MainPS", "source": {"path": str(fixture.service.source_path.relative_to(fixture.root))}}))
        opened = fixture._begin()
        monkeypatch.setattr(server, "_shader_organization_manager", lambda: fixture.manager)
        monkeypatch.setattr(server, "_workspace_root_for_files", lambda path: fixture.root)
        monkeypatch.setattr(server, "_shader_workspace_service", lambda: fixture.service)
        monkeypatch.setattr(server, "_shader_cbuffer_display_observation", lambda status: {})
        store = TaskReportStore("self-review", tmp_path / "events")
        monkeypatch.setattr(server, "current_task_report_store", lambda: store)
        monkeypatch.setattr(task_report, "current_task_report_store", lambda: store)
        store.record_result("begin_shader_workspace_organization", opened)
        proposal = Path(opened["proposalSourcePath"])
        from test_shader_source_reads import deliver
        deliver(store, source_snapshot(str(fixture.root), opened["organizationId"]),
                proposal.read_text(encoding="utf-8"), mode="complete")
        edited = b"float4 EvaluateBaseColor() { return 1.0f.xxxx; }\nfloat4 MainPS() : SV_Target { return EvaluateBaseColor(); }\n"
        imported = json.loads(server.write_shader_workspace_organization_file(
            str(fixture.root), opened["organizationId"], "proposal", edited.decode("utf-8"),
            expected_proposal_sha256=hashlib.sha256(proposal.read_bytes()).hexdigest()))
        assert not imported.get("error")
        assert store.shader_organization_review_state()["targets"][0]["changed"]
        incomplete = json.loads(server.record_shader_organization_review(
            str(fixture.root), imported["sha256"], "complete",
            {key: "Current complete source scope inspected" for key in CHECKS}, [], [],
            scope_review=scope_assessment(source_snapshot(str(fixture.root), opened["organizationId"]))))
        assert "semantic_overview" in incomplete["error"]
        receipt = json.loads(server.record_shader_organization_review(
            str(fixture.root), imported["sha256"], "complete",
            {key: "MainPS delegates base color calculation to EvaluateBaseColor" for key in CHECKS}, [], [],
            scope_review=scope_assessment(source_snapshot(str(fixture.root), opened["organizationId"])),
            semantic_overview=semantic_assessment(source_snapshot(str(fixture.root), opened["organizationId"]),
                scope_assessment(source_snapshot(str(fixture.root), opened["organizationId"]))),
        ))
        assert not receipt.get("error") and receipt["hostSemanticApproval"] is False
        assert json.loads(Path(receipt["reviewPath"]).read_text())["sourceSha256"] == imported["sha256"]
        overview_path = Path(receipt["semanticOverview"]["path"])
        overview = json.loads(overview_path.read_text(encoding="utf-8"))
        assert overview["sourceSha256"] == imported["sha256"]
        assert overview["stages"][0]["title"] == "Constant output"
        assert b"@overview" not in proposal.read_bytes()
        assert store.shader_organization_review_state()["targets"][0]["fresh"]
        assert store.shader_organization_review_state()["targets"][0]["semanticOverviewComplete"]
        result = json.loads(server.finish_shader_workspace_organization(str(fixture.root), opened["organizationId"], validate_for_apply=False))
        assert result["ok"] and result["sourceSha256"] == imported["sha256"]
        row = store.shader_organization_review_state()["targets"][0]
        assert row["fresh"] and row["saved"]
        # Directory-only repair on saved source keeps the valid source review;
        # no draft write, compile or replay is needed just to refine navigation.
        metadata = semantic_assessment(source_snapshot(str(fixture.root), opened["organizationId"]),
                                       scope_assessment(source_snapshot(str(fixture.root), opened["organizationId"])))
        metadata["stages"][0]["title"] = "Constant fragment color"
        monkeypatch.setattr(server, "_active_project_service", lambda: type("NoProject", (), {"status": lambda self: {}})())
        monkeypatch.setattr(fixture.service, "compile", lambda *args: (_ for _ in ()).throw(AssertionError("no metadata compile")))
        repaired = json.loads(server.update_shader_semantic_overview(
            str(fixture.root), imported["sha256"], metadata, organization_id=opened["organizationId"]))
        assert not repaired.get("error"), repaired
        assert repaired["verdict"] == "complete" and repaired["overviewPublication"]["updatedLocal"]
        assert store.shader_organization_review_state()["targets"][0]["semanticOverviewComplete"]
        assert proposal.read_bytes() == edited
        overview = json.loads(overview_path.read_text(encoding="utf-8"))
        proposal.write_bytes(edited + b"\n// later change\n")
        stale = json.loads(server.record_shader_organization_review(
            str(fixture.root), imported["sha256"], "complete",
            {key: "old source" for key in CHECKS}, [], [], organization_id=opened["organizationId"],
        ))
        assert "changed since" in stale["error"] or "full source read" in stale["error"]
        assert json.loads(overview_path.read_text(encoding="utf-8")) == overview
    finally:
        fixture.tearDown()


def scope_assessment(snapshot):
    from test_shader_scope_review import retained_dispositions
    catalog = source_scope_catalog(snapshot)
    scopes = {"baseline_source_sha256": snapshot["baselineSourceSha256"],
            "plan": [{"id": "output", "requirement": "Organize the complete captured output calculation",
                      "functions": catalog["initialFunctions"]}],
            "results": [{"id": "output", "status": "satisfied", "source_functions": catalog["currentFunctions"],
                         "evidence": "MainPS calls the named base-color helper and keeps the captured output interface",
                         "branches": [{"path": "straight-line output", "status": "satisfied", "evidence": "The sole return uses the unchanged constant color"}],
                         "retained_intermediates": "No redundant copies remain in this small calculation",
                         "remaining_work": []}]}
    scopes["observation_dispositions"] = retained_dispositions(snapshot, scopes["results"])
    return scopes
