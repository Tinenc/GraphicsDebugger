"""Automatic source choice always progresses without dropping old source/editors."""
import itertools
import os
from unittest import mock

import pytest

from src.shader_workspace.canonical_source import select_source
from test_shader_canonical_source import owner, scan


def row(name, language="hlsl", modified=0, registered="", active=False):
    return dict(workspacePath="/sources/" + name, workspaceId=name,
                sourceFormat=language, sourceSha256=name, localChanges=True,
                sourceModifiedNs=modified, registeredAt=registered, activeHead=active)


@pytest.mark.parametrize("old_language,new_language", [("hlsl", "glsl"), ("glsl", "hlsl"), ("hlsl", "hlsl")])
def test_latest_saved_source_wins_even_when_older_source_is_active_and_selected(old_language, new_language):
    old = row("old", old_language, 100, active=True)
    latest = row("latest", new_language, 200)
    ui = dict(selectedWorkspaceId="old", documents=[dict(workspaceId="old", dirty=True)])
    result = select_source([old, latest], ui=ui)
    assert result["workspaceId"] == "latest"
    assert result["retainedSourceConflicts"][0]["workspaceId"] == "old"
    assert ui["documents"][0]["dirty"] is True


@pytest.mark.parametrize("registered", ["", "2026-10-31T23:00:00Z"])
def test_same_age_or_missing_times_prefer_hlsl_to_active_glsl(registered):
    rows = [row("glsl", "glsl", registered=registered, active=True),
            row("hlsl", "hlsl", registered=registered)]
    for permutation in itertools.permutations(rows):
        assert select_source(permutation)["workspaceId"] == "hlsl"


def test_registration_clock_normalizes_offsets_and_can_choose_newer_glsl():
    rows = [row("hlsl", registered="2026-11-01T08:00:00+08:00"),
            row("glsl", "glsl", registered="2026-11-01T00:00:01Z")]
    assert select_source(rows)["workspaceId"] == "glsl"
    rows[1]["registeredAt"] = "2026-11-01T00:00:00Z"
    assert select_source(rows)["workspaceId"] == "hlsl"


def test_same_language_and_unknown_dates_have_a_stable_non_refusing_tiebreak():
    rows = [row("b", registered="unavailable"), row("a", registered=None), row("c")]
    for permutation in itertools.permutations(rows):
        assert select_source(permutation)["workspaceId"] == "a"
    assert select_source([]) is None


def test_real_imported_hlsl_glsl_owners_choose_latest_without_rewrite_or_compile(tmp_path):
    root = tmp_path / "shaders"
    glsl, model = owner(root, "old-glsl", language="glsl", text=b"old GLSL")
    hlsl, unused = owner(root, "new-hlsl", text=b"new HLSL")
    os.utime(glsl / "shader.glsl", ns=(1_700_000_000_000_000_000,) * 2)
    os.utime(hlsl / "shader.hlsl", ns=(1_800_000_000_000_000_000,) * 2)
    # Later compilation/manifest bookkeeping cannot make old source the latest.
    os.utime(glsl / "manifest.json", ns=(1_900_000_000_000_000_000,) * 2)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    rows, reader = scan(root, model)
    assert select_source(rows)["workspacePath"] == str(hlsl)
    assert len(reader.reads) == 2
    assert {p: p.read_bytes() for p in before} == before


def test_automatic_resolver_does_not_wait_for_ui_state(tmp_path, monkeypatch):
    from src import server
    import src.shader_workspace.canonical_source as sources
    rows = [row("old", "glsl", 100), row("new", "hlsl", 200)]
    monkeypatch.setattr(sources, "existing_sources", lambda *a, **k: (rows, []))
    monkeypatch.setattr(server, "_active_project_workspace_root", lambda status: tmp_path)
    with mock.patch.object(server.ipc, "call", side_effect=AssertionError("No UI query for automatic choice")):
        chosen, diagnostics = server._canonical_managed_shader_source({}, "frame.rdc", "Vulkan", {}, "session")
    assert chosen["workspaceId"] == "new" and diagnostics == []
