"""Optional shader explanations never control source publication."""

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


def test_begin_exposes_draft_without_injecting_document_templates(work):
    opened = work.manager.begin(str(work.root), {"eid": 11384})
    assert opened["semanticContext"]["eid"] == 11384
    assert "analysisTemplate" not in opened
    assert "moduleDescriptionsTemplate" not in opened
    assert "flowSpecTemplate" not in opened
    assert "templateContract" not in opened
    assert "policy" not in opened
    assert "requiredChecks" not in opened


def test_invalid_optional_explanation_does_not_block_final_source(work):
    opened = work.manager.begin(str(work.root), {"eid": 11384})
    Path(opened["analysisPath"]).write_text("Unfinished explanatory notes", encoding="utf-8")
    Path(opened["moduleDescriptionsPath"]).write_text("{broken optional JSON", encoding="utf-8")
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_text("// clearer source\n" + work.original.decode("utf-8"), encoding="utf-8")

    finished = work.manager.finish_draft(str(work.root), opened["organizationId"])
    assert finished["ok"] is True
    assert finished["wholeSourceValidated"] is True
    assert finished["semanticOrganizationVerified"] is False
    assert work.service.source_path.read_bytes() == proposal.read_bytes()
