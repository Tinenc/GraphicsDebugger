"""Current-source draft continuation without the retired review-round workflow."""

from __future__ import annotations

from pathlib import Path

import pytest

import test_shader_organization as harness


@pytest.fixture
def work():
    case = harness.ShaderOrganizationTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()
        case.doCleanups()


def test_matching_draft_resumes_without_restarting_or_compiling(work):
    opened = work.manager.begin(str(work.root), {"eid": 11384})
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_text("// current draft\n" + work.original.decode("utf-8"), encoding="utf-8")
    work.manager.checkpoint(str(work.root), opened["organizationId"])

    resumed = work.manager.begin(str(work.root), {"eid": 11384})
    assert resumed["organizationId"] == opened["organizationId"]
    assert resumed["proposalSourcePath"] == opened["proposalSourcePath"]
    assert proposal.read_text(encoding="utf-8").startswith("// current draft")
    assert work.service.revision == 0


def test_old_budget_file_does_not_control_new_draft(work):
    ai_dir = work.root / "ai"
    ai_dir.mkdir(exist_ok=True)
    (ai_dir / "organization-rounds.json").write_text("{broken legacy budget", encoding="utf-8")
    opened = work.manager.begin(str(work.root), {"eid": 11384})
    assert opened["organizationId"]
    assert "review" not in opened
    assert not hasattr(work.manager, "review")
    assert (ai_dir / "organization-rounds.json").read_text(encoding="utf-8") == "{broken legacy budget"


def test_finalized_source_can_start_a_new_independent_draft(work):
    opened = work.manager.begin(str(work.root), {"eid": 11384})
    Path(opened["proposalSourcePath"]).write_text(
        "// first pass\n" + work.original.decode("utf-8"), encoding="utf-8"
    )
    finished = work.manager.finish_draft(str(work.root), opened["organizationId"])
    assert finished["ok"] is True
    assert finished["wholeSourceValidated"] is True

    next_draft = work.manager.begin(str(work.root), {"eid": 11384})
    assert next_draft["organizationId"] != opened["organizationId"]
    assert next_draft["baselineSourceSha256"] == finished["finalBinding"]["sourceSha256"]
    assert Path(next_draft["proposalSourcePath"]).read_bytes() == work.service.source_path.read_bytes()
    assert next_draft["baselineCompiledAndAbiValidated"] is False
