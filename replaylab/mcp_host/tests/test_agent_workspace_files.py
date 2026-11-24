import hashlib
import json
import os
from pathlib import Path

import pytest

from src.agent_workspace_files import (
    AGENT_WORKSPACE_ROOT_ENV, MAX_AGENT_JSON_BYTES, copy_text_to_agent_workspace,
    read_agent_json_file, read_agent_text_file,
)


@pytest.fixture
def agent_directory(tmp_path, monkeypatch):
    root = tmp_path / "agent-workspace"
    root.mkdir()
    monkeypatch.setenv(AGENT_WORKSPACE_ROOT_ENV, str(root))
    return root


def test_independent_full_copy_native_edit_and_import_keep_user_source(agent_directory, tmp_path, monkeypatch):
    from src import server
    from test_shader_organization import ShaderOrganizationTests, _FakeWorkspaceService
    fixture = ShaderOrganizationTests()
    fixture.setUp()
    try:
        fixture.root = tmp_path / "shader"
        original = b"float4 MainPS() : SV_Target { return 1.0f.xxxx; }\n"
        fixture.service = _FakeWorkspaceService(fixture.root, original)
        opened = fixture._begin()
        monkeypatch.setattr(server, "_shader_organization_manager", lambda: fixture.manager)
        monkeypatch.setattr(server, "_workspace_root_for_files", lambda path: fixture.root)
        monkeypatch.setattr(server, "_shader_workspace_service", lambda: fixture.service)
        copied = json.loads(server.read_shader_workspace_file(
            str(fixture.root), "source/shader.hlsl", limit=8, agent_file_name="shader-edit.hlsl"))
        copy = copied["agentFile"]
        path = Path(copy["path"])
        assert copied["truncated"] and path.read_bytes() == original
        assert copy["sha256"] == hashlib.sha256(original).hexdigest()
        edited = original.replace(b"1.0f", b"0.5f")
        path.write_bytes(edited)
        proposal = Path(opened["proposalSourcePath"])
        before_sha = hashlib.sha256(proposal.read_bytes()).hexdigest()
        imported = json.loads(server.write_shader_workspace_organization_file(
            str(fixture.root), opened["organizationId"], "proposal",
            source_file=str(path), expected_proposal_sha256=before_sha))
        assert imported["inputFile"]["sha256"] == hashlib.sha256(edited).hexdigest()
        assert imported["applied"] is False and proposal.read_bytes() == edited
        assert fixture.service.source_path.read_bytes() == original
        stale = json.loads(server.write_shader_workspace_organization_file(
            str(fixture.root), opened["organizationId"], "proposal",
            source_file=str(path), expected_proposal_sha256=before_sha))
        assert "proposal changed" in stale["error"] and proposal.read_bytes() == edited
    finally:
        fixture.tearDown()


def test_recopy_preserves_the_agents_uncommitted_work(agent_directory):
    first = copy_text_to_agent_workspace("draft.hlsl", b"original\n")
    Path(first["path"]).write_bytes(b"unsaved edits\n")
    second = copy_text_to_agent_workspace("draft.hlsl", b"new source\n")
    assert first["path"] != second["path"]
    assert Path(first["path"]).read_bytes() == b"unsaved edits\n"
    assert Path(second["path"]).read_bytes() == b"new source\n"


@pytest.mark.parametrize("name", ["../secret.txt", ".cursor/mcp.json", ".mcp.json",
                                    "AGENTS.md", "settings.json", "config.toml",
                                    "AGENTS.md ", "AGENTS.md.", "settings.json ",
                                    "config.toml."])
def test_launch_configuration_and_cross_root_paths_are_not_artifacts(agent_directory, name):
    with pytest.raises(ValueError):
        read_agent_text_file(name)
    with pytest.raises(ValueError):
        copy_text_to_agent_workspace(name, b"text")


def test_other_run_and_binary_or_oversized_files_are_rejected(agent_directory, tmp_path):
    other = tmp_path / "other-run"
    other.mkdir()
    secret = other / "draft.hlsl"
    secret.write_text("outside", encoding="utf-8")
    with pytest.raises(ValueError):
        read_agent_text_file(str(secret))
    for raw in (b"", b"text\0text", b"x" * (4 * 1024 * 1024 + 1), b"\xff"):
        with pytest.raises((ValueError, UnicodeDecodeError)):
            copy_text_to_agent_workspace("invalid.hlsl", raw)


def test_symlink_or_hardlink_cannot_import_outside_content(agent_directory, tmp_path):
    outside = tmp_path / "private.txt"
    outside.write_bytes(b"not an Agent artifact")
    linked = agent_directory / "linked.hlsl"
    os.link(outside, linked)
    with pytest.raises(ValueError, match="one link"):
        read_agent_text_file(str(linked))
    symbolic = agent_directory / "symlink.hlsl"
    try:
        symbolic.symlink_to(outside)
    except OSError:
        return  # The hardlink boundary above is tested on ordinary Windows.
    with pytest.raises(ValueError, match="symlink|redirected"):
        read_agent_text_file(str(symbolic))


def test_no_model_supplied_root_fallback(monkeypatch):
    monkeypatch.delenv(AGENT_WORKSPACE_ROOT_ENV, raising=False)
    with pytest.raises(ValueError, match="controller-provided"):
        copy_text_to_agent_workspace("draft.hlsl", b"text")


def test_large_owned_json_has_separate_limit_and_exact_byte_hash(agent_directory):
    raw = json.dumps([{"description": "shader meaning" * 330000}]).encode()
    assert 4 * 1024 * 1024 < len(raw) < MAX_AGENT_JSON_BYTES
    path = agent_directory / "semantic-fields.json"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    value, facts = read_agent_json_file(str(path), digest)
    assert len(value) == 1 and facts["sizeBytes"] == len(raw)
    assert facts["sha256"] == digest and facts["replayAuthority"] is False
    with pytest.raises(ValueError, match="4 MiB"):
        read_agent_text_file(str(path))
    with pytest.raises(ValueError, match="does not match"):
        read_agent_json_file(str(path), "0" * 64)
    path.write_bytes(b"[{}]")
    with pytest.raises(ValueError, match="does not match"):
        read_agent_json_file(str(path), digest)


@pytest.mark.parametrize("digest", ["", "ABC", "A" * 64, "a" * 63, None])
def test_json_file_requires_explicit_current_hash(agent_directory, digest):
    with pytest.raises(ValueError, match="SHA256"):
        read_agent_json_file("missing.json", digest)


def test_json_file_uses_owned_root_and_link_boundary(agent_directory, tmp_path):
    raw = b'["not publication authority"]'
    outside = tmp_path / "outside.json"
    outside.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="outside"):
        read_agent_json_file(str(outside), digest)
    linked = agent_directory / "linked.json"
    os.link(outside, linked)
    with pytest.raises(ValueError, match="one link"):
        read_agent_json_file(str(linked), digest)
    with pytest.raises(ValueError, match="configuration"):
        read_agent_json_file("settings.json", digest)


@pytest.mark.parametrize("raw", [b"[broken", b"[]\0", b"\xff", b"x" * (MAX_AGENT_JSON_BYTES + 1)],
                         ids=["syntax", "nul", "encoding", "oversized"])
def test_invalid_or_oversized_json_is_rejected(agent_directory, raw):
    path = agent_directory / "data.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        read_agent_json_file(str(path), hashlib.sha256(raw).hexdigest())


def test_complete_file_transfer_and_range_report_actual_utf8_coverage(agent_directory, tmp_path, monkeypatch):
    from src import server
    source = tmp_path / "shader.hlsl"
    text = "float3 surfaceColor; // 中文😀\r\n" * 9000
    source.write_bytes(text.encode("utf-8"))
    monkeypatch.setattr(server, "_workspace_file_context", lambda _: (tmp_path, {}))

    complete = json.loads(server.read_shader_workspace_file(str(tmp_path), source.name, mode="complete", limit=1))
    assert complete["text"] == text and complete["truncated"] is False
    assert complete["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert complete["coverage"] == {"byteOffset": 0, "returnedBytes": len(text.encode()), "complete": True}
    assert complete["agentFile"] is None and complete["applied"] is False

    page = json.loads(server.read_shader_workspace_file(str(tmp_path), source.name, offset=26, limit=9))
    assert page["text"] == text[26:35]
    assert page["coverage"] == {"byteOffset": len(text[:26].encode()),
                                "returnedBytes": len(text[26:35].encode()), "complete": False}
    file_only = json.loads(server.read_shader_workspace_file(str(tmp_path), source.name, mode="file"))
    assert file_only["text"] == "" and file_only["coverage"]["complete"] is False
    assert file_only["coverage"]["returnedBytes"] == 0
    copy = file_only["agentFile"]
    assert Path(copy["path"]).read_bytes() == source.read_bytes()
    assert copy["sourceSha256"] == complete["sha256"] and copy["sourceRelativePath"] == source.name
    assert copy["workingCopy"] is True and copy["replayAuthority"] is False


def test_complete_mode_is_explicit_and_never_silently_truncates(tmp_path, monkeypatch):
    from src import server
    source = tmp_path / "shader.hlsl"
    monkeypatch.setattr(server, "_workspace_file_context", lambda _: (tmp_path, {}))
    source.write_bytes(b"x" * (4 * 1024 * 1024))
    complete = json.loads(server.read_shader_workspace_file(str(tmp_path), source.name, mode="complete"))
    assert complete["sizeBytes"] == len(complete["text"]) == 4 * 1024 * 1024
    default = json.loads(server.read_shader_workspace_file(str(tmp_path), source.name))
    assert default["chars"] == 60_000 and default["truncated"] is True
    for mode in ("complete", "file"):
        error = json.loads(server.read_shader_workspace_file(str(tmp_path), source.name, mode=mode, offset=1))
        assert "offset=0" in error["error"] and "text" not in error
    source.write_bytes(source.read_bytes() + b"x")
    too_large = json.loads(server.read_shader_workspace_file(str(tmp_path), source.name, mode="complete"))
    assert "size limit" in too_large["error"] and "text" not in too_large
    source.write_bytes(b"invalid utf8 \xff")
    invalid = json.loads(server.read_shader_workspace_file(str(tmp_path), source.name, mode="complete"))
    assert "error" in invalid and "text" not in invalid


def test_begin_copies_current_proposal_and_import_retains_cas(agent_directory, monkeypatch):
    from src import server
    from test_shader_organization import ShaderOrganizationTests
    fixture = ShaderOrganizationTests()
    fixture.setUp()
    try:
        monkeypatch.setattr(server, "_shader_organization_manager", lambda: fixture.manager)
        monkeypatch.setattr(server, "_workspace_root_for_files", lambda path: fixture.root)
        monkeypatch.setattr(server, "_shader_workspace_service", lambda: fixture.service)
        monkeypatch.setattr(server, "_shader_organization_semantic_context", lambda *args, **kwargs:
                            {"stage": "fragment", "evidence": "binding " * 9000})
        first = json.loads(server.begin_shader_workspace_organization(str(fixture.root), agent_file_name="proposal.hlsl"))
        assert "agentFileError" not in first
        original = fixture.service.source_path.read_bytes()
        proposal = Path(first["proposalSourcePath"])
        edited = b"// agent draft\n" + proposal.read_bytes()
        proposal.write_bytes(edited)
        second = json.loads(server.begin_shader_workspace_organization(str(fixture.root), agent_file_name="proposal.hlsl"))
        copied = second["agentFile"]
        assert second["organizationId"] == first["organizationId"]
        assert copied["path"] != first["agentFile"]["path"]
        assert Path(copied["path"]).read_bytes() == edited
        assert copied["expectedProposalSha256"] == hashlib.sha256(edited).hexdigest()
        assert second["proposalEditing"]["proposalSha256"] == hashlib.sha256(edited).hexdigest()
        assert first["proposalEditing"]["proposalSha256"] == hashlib.sha256(original).hexdigest()
        assert second["proposalEditing"]["availableInputs"] == ["edits", "source_file", "content"]
        Path(copied["path"]).write_bytes(edited + b"// next edit\n")
        result = json.loads(server.write_shader_workspace_organization_file(
            str(fixture.root), second["organizationId"], "proposal", source_file=copied["path"],
            expected_proposal_sha256=copied["expectedProposalSha256"]))
        assert result["applied"] is False and proposal.read_bytes() == edited + b"// next edit\n"
        assert result["proposalEditing"]["proposalSha256"] == hashlib.sha256(proposal.read_bytes()).hexdigest()
        assert fixture.service.source_path.read_bytes() == original
    finally:
        fixture.tearDown()


def test_failed_begin_copy_reports_existing_draft_instead_of_losing_it(monkeypatch):
    from src import server
    from test_shader_organization import ShaderOrganizationTests
    fixture = ShaderOrganizationTests()
    fixture.setUp()
    try:
        monkeypatch.delenv(AGENT_WORKSPACE_ROOT_ENV, raising=False)
        monkeypatch.setattr(server, "_shader_organization_manager", lambda: fixture.manager)
        monkeypatch.setattr(server, "_shader_organization_semantic_context", lambda *args, **kwargs: {})
        result = json.loads(server.begin_shader_workspace_organization(str(fixture.root), agent_file_name="proposal.hlsl"))
        assert result["organizationId"] and Path(result["proposalSourcePath"]).is_file()
        assert result["agentFile"] is None and "controller-provided" in result["agentFileError"]
        assert "error" not in result and result["applied"] is False
        assert result["proposalEditing"]["sourceFile"]["scope"] == "current-run-owned-agent-workspace"
    finally:
        fixture.tearDown()


def test_begin_review_facts_do_not_describe_a_later_proposal(monkeypatch):
    from src import server, shader_organization_review, source_review_observations
    from test_shader_organization import ShaderOrganizationTests
    fixture = ShaderOrganizationTests()
    fixture.setUp()
    try:
        monkeypatch.setattr(server, "_shader_organization_manager", lambda: fixture.manager)
        monkeypatch.setattr(server, "_shader_organization_semantic_context", lambda *args, **kwargs: {})
        real_snapshot = shader_organization_review.source_snapshot
        def changed_snapshot(workspace, organization):
            value = real_snapshot(workspace, organization)
            path = Path(value["sourcePath"])
            path.write_bytes(path.read_bytes() + b"\n// concurrent draft edit\n")
            return real_snapshot(workspace, organization)
        monkeypatch.setattr(shader_organization_review, "source_snapshot", changed_snapshot)
        monkeypatch.setattr(source_review_observations, "source_review_observations",
                            lambda snapshot: pytest.fail("Later proposal must not become this receipt's evidence"))
        result = json.loads(server.begin_shader_workspace_organization(str(fixture.root)))
        assert "error" not in result and result["organizationId"]
        assert "sourceReviewScope" not in result and "sourceReviewObservations" not in result
        assert "changed" in result["sourceReviewScopeError"]
        assert "changed" in result["sourceReviewObservationsError"]
        assert result["proposalEditing"]["proposalSha256"] == result["proposalSourceSha256"]
        assert Path(result["proposalSourcePath"]).read_bytes().endswith(b"// concurrent draft edit\n")
    finally:
        fixture.tearDown()
