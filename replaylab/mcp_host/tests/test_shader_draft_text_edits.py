import asyncio
import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

from src.shader_workspace.io import apply_exact_text_edits


@pytest.fixture
def draft(monkeypatch):
    from src import server
    from test_shader_organization import ShaderOrganizationTests

    fixture = ShaderOrganizationTests()
    fixture.setUp()
    try:
        opened = fixture._begin()
        monkeypatch.setattr(server, "_shader_organization_manager", lambda: fixture.manager)
        monkeypatch.setattr(server, "_shader_workspace_service", lambda: fixture.service)
        proposal = Path(opened["proposalSourcePath"])
        # An intentionally incomplete draft is valid for text editing. Only the
        # later final publication may compile and validate the candidate.
        original = b"// material\r\nfloat channelX = source.x;\r\nfloat channelY = source.y;\r\n"
        proposal.write_bytes(original)
        fixture.manager.checkpoint(str(fixture.root), opened["organizationId"])
        monkeypatch.setattr(fixture.service, "compile", lambda *a, **k: pytest.fail("draft edit compiled"))
        monkeypatch.setattr(fixture.service, "compile_check", lambda *a, **k: pytest.fail("draft edit validated"))
        yield server, fixture, opened, proposal, original
    finally:
        fixture.tearDown()


def write(draft, text_edits, **kwargs):
    server, fixture, opened, proposal, original = draft
    args = {"expected_proposal_sha256": hashlib.sha256(original).hexdigest(), "edits": text_edits}
    args.update(kwargs)
    return json.loads(server.write_shader_workspace_organization_file(
        str(fixture.root), opened["organizationId"], "proposal", **args))


def test_ordered_edits_save_once_without_touching_saved_source(draft):
    server, fixture, opened, proposal, original = draft
    saved = fixture.service.source_path.read_bytes()
    result = write(draft, [
        {"old_text": "float channelX = source.x;", "new_text": "float3 color = source; // 颜色"},
        {"old_text": "float3 color = source; // 颜色", "new_text": "float3 lightColor = source; // 光照"},
        {"old_text": "float channelY = source.y;\r\n", "new_text": ""},
    ])
    assert "error" not in result
    expected = "// material\r\nfloat3 lightColor = source; // 光照\r\n".encode()
    assert proposal.read_bytes() == expected
    assert result["previousProposalSha256"] == hashlib.sha256(original).hexdigest()
    assert result["sha256"] == hashlib.sha256(expected).hexdigest()
    assert result["textEditsApplied"] == 3 and result["inputFile"] is None
    editing = result["proposalEditing"]
    assert editing["proposalSha256"] == hashlib.sha256(proposal.read_bytes()).hexdigest()
    assert editing["tool"] == "write_shader_workspace_organization_file"
    assert editing["availableInputs"] == ["edits", "source_file", "content"]
    assert editing["atomicEdits"]["fields"] == ["old_text", "new_text"]
    assert editing["atomicEdits"]["requiresCurrentProposalHash"] is True
    assert editing["atomicEdits"]["requiresRestructureAnalysis"] is False
    assert editing["sourceFile"] == {
        "scope": "current-run-owned-agent-workspace", "acceptsArbitraryPaths": False}
    assert editing["draftOnly"] is True
    assert result["applied"] is False and fixture.service.source_path.read_bytes() == saved
    # Checkpoint still authorizes continuing this exact draft.
    fixture.manager.authorize_edit(str(fixture.root), opened["organizationId"])


@pytest.mark.parametrize("bad_edit", [
    {"old_text": "missing", "new_text": "changed"},
    {"old_text": "float", "new_text": "half"},
    {"old_text": "", "new_text": "added"},
    {"old_text": "source.x", "new_text": "bad\0text"},
    {"old_text": "source.x", "new_text": 1},
    {"old_text": "source.x", "new_text": "x", "replace_all": True},
])
def test_any_later_failure_leaves_entire_proposal_and_checkpoint_unchanged(draft, bad_edit):
    _, _, _, proposal, original = draft
    session = proposal.parent / "session.json"
    before_sha = json.loads(session.read_text())["proposalSourceSha256"]
    result = write(draft, [{"old_text": "// material", "new_text": "// changed"}, bad_edit])
    assert "error" in result
    assert proposal.read_bytes() == original
    assert json.loads(session.read_text())["proposalSourceSha256"] == before_sha


@pytest.mark.parametrize("options, expected", [
    ({"expected_proposal_sha256": ""}, "require expected_proposal_sha256"),
    ({"expected_proposal_sha256": "a" * 64}, "changed since it was read"),
    ({"content": "new content"}, "exactly one"),
    ({"source_file": "other.hlsl"}, "exactly one"),
    ({"edits": []}, "non-empty list"),
])
def test_edit_inputs_and_stale_hash_are_rejected(draft, options, expected):
    result = write(draft, [{"old_text": "source.x", "new_text": "source.z"}], **options)
    assert expected in result["error"] and draft[3].read_bytes() == draft[4]


def test_edit_mode_does_not_write_explanatory_artifacts(draft):
    server, fixture, opened, proposal, original = draft
    for kind in ("analysis", "flowchart"):
        result = json.loads(server.write_shader_workspace_organization_file(
            str(fixture.root), opened["organizationId"], kind,
            expected_proposal_sha256=hashlib.sha256(original).hexdigest(),
            edits=[{"old_text": "source.x", "new_text": "source.z"}]))
        assert "only to proposal" in result["error"]
    assert proposal.read_bytes() == original


@pytest.mark.parametrize("kind, content", [("analysis", "# Material algorithm"),
                                         ("flowchart", "flowchart TD\nA --> B")])
def test_explanatory_write_does_not_advertise_proposal_editing(draft, kind, content):
    server, fixture, opened, proposal, original = draft
    result = json.loads(server.write_shader_workspace_organization_file(
        str(fixture.root), opened["organizationId"], kind, content=content))
    assert "error" not in result and result["artifactOnly"] is True
    assert "proposalEditing" not in result
    assert proposal.read_bytes() == original


def test_external_change_during_edit_is_not_overwritten(draft, monkeypatch):
    from src.shader_workspace import io
    _, _, _, proposal, _ = draft
    before_apply = io.apply_exact_text_edits
    concurrent = b"// concurrent user draft\n"

    def racing_apply(*args, **kwargs):
        result = before_apply(*args, **kwargs)
        proposal.write_bytes(concurrent)
        return result

    monkeypatch.setattr(io, "apply_exact_text_edits", racing_apply)
    result = write(draft, [{"old_text": "source.x", "new_text": "source.z"}])
    assert "changed before draft write" in result["error"]
    assert proposal.read_bytes() == concurrent


def test_observation_failure_does_not_change_successful_write(draft, monkeypatch):
    module = types.ModuleType("src.source_review_observations")

    def unavailable(snapshot):
        raise RuntimeError("observation unavailable")

    module.source_review_observations = unavailable
    module.source_review_catalog = unavailable
    monkeypatch.setitem(sys.modules, "src.source_review_observations", module)
    result = write(draft, [{"old_text": "source.x", "new_text": "source.z"}])
    assert "error" not in result
    assert result["sourceReviewObservationsError"] == "observation unavailable"
    assert draft[3].read_bytes() == draft[4].replace(b"source.x", b"source.z")


def test_observations_never_describe_a_later_concurrent_draft_as_written_bytes(draft, monkeypatch):
    from src import shader_organization_review
    _, _, _, proposal, original = draft
    real_snapshot = shader_organization_review.source_snapshot
    concurrent = b"// user edited after the tool saved its proposal\n"

    def snapshot_after_external_edit(*args, **kwargs):
        proposal.write_bytes(concurrent)
        return real_snapshot(*args, **kwargs)

    module = types.ModuleType("src.source_review_observations")
    module.source_review_observations = lambda snapshot: pytest.fail("observations used different bytes")
    monkeypatch.setitem(sys.modules, "src.source_review_observations", module)
    monkeypatch.setattr(shader_organization_review, "source_snapshot", snapshot_after_external_edit)
    result = write(draft, [{"old_text": "source.x", "new_text": "source.z"}])
    written = original.replace(b"source.x", b"source.z")
    assert "error" not in result and result["sha256"] == hashlib.sha256(written).hexdigest()
    assert result["textEditsApplied"] == 1 and result["applied"] is False
    assert "sourceReviewObservations" not in result
    assert "changed before observing" in result["sourceReviewObservationsError"]
    assert proposal.read_bytes() == concurrent


def test_exact_match_detects_overlapping_ambiguity_and_bounds_utf8_size():
    with pytest.raises(ValueError, match="more than once"):
        apply_exact_text_edits("aaa", [{"old_text": "aa", "new_text": "b"}], maximum_bytes=50)
    with pytest.raises(ValueError, match="size limit"):
        apply_exact_text_edits("a", [{"old_text": "a", "new_text": "中文"}], maximum_bytes=5)


def test_deleting_entire_draft_is_rejected_atomically(draft):
    result = write(draft, [{"old_text": draft[4].decode(), "new_text": ""}])
    assert "non-empty" in result["error"] and draft[3].read_bytes() == draft[4]


def test_external_mcp_exposes_and_accepts_draft_edits(draft):
    server, fixture, opened, proposal, original = draft
    proxy = server._DeferredFastMCP()
    proxy.tool()(server.write_shader_workspace_organization_file)
    application = proxy._build()
    tools = asyncio.run(application.list_tools())
    schema = tools[0].inputSchema["properties"]["edits"]
    array = next(branch for branch in schema["anyOf"] if branch["type"] == "array")
    assert array["items"]["type"] == "object"
    asyncio.run(application.call_tool("write_shader_workspace_organization_file", {
        "workspace_path": str(fixture.root), "organization_id": opened["organizationId"],
        "kind": "proposal", "expected_proposal_sha256": hashlib.sha256(original).hexdigest(),
        "edits": [{"old_text": "source.x", "new_text": "source.z"}],
    }))
    assert proposal.read_bytes() == original.replace(b"source.x", b"source.z")
