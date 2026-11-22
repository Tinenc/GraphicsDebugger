"""Saved-source fallback editors retain independently authored navigation."""
import copy
import hashlib
import json
import threading
from unittest import mock

import pytest

from src.shader_workspace.flow_graph import build_shader_flow_graph
from src.shader_workspace.semantic_overview import prepare_overview
from test_shader_console import ShaderConsoleFixture, console
from test_shader_stage_outline import Hierarchy, outline, workspace as panel_module
from test_shader_workspace_panel import _canonical_display_fixture


# Like a SPIRV-Cross entry, the reciprocal prevents conservative wrapper
# unwrapping. Missing sidecars must reproduce the actual one-main regression.
FUNCTIONS = ["Surface", "Probe", "Wet", "Lut", "Face", "Diffuse", "Highlight",
             "Rim", "Cluster", "Shadow", "Grade", "Air", "Fog", "Motion"]
SOURCE = ("\n".join("float %s(float x) { return x * %d; }" % (name, i + 1)
                    for i, name in enumerate(FUNCTIONS)) +
          "\nfloat Shade(float x) {\n" +
          "\n".join("  x = %s(x);" % name for name in FUNCTIONS) +
          "\n  return x;\n}\nfloat4 MainPS(float x) : SV_Target {\n"
          "  x = 1.0 / x;\n  return Shade(x);\n}\n")


class NavigationFixture(ShaderConsoleFixture):
    @property
    def capture_binding(self):
        return console.normalize_capture_binding({
            "instanceId": "preview-navigation", "captureSessionId": "8" * 32,
            "capturePath": str(self.root / "capture.rdc")})


def _fixture(tmp_path):
    fixture = NavigationFixture(tmp_path)
    fixture.baseline_artifact.write_bytes(SOURCE.encode("utf8"))
    gate = fixture.write_editable_draft()
    gate["baselineSourceSha256"] = hashlib.sha256(fixture.baseline_artifact.read_bytes()).hexdigest()
    fixture._write(fixture.workspace / "ai/organize-code-gate.json", gate)
    session_path = fixture.session_dir / "session.json"
    session = json.loads(session_path.read_text("utf8"))
    session["baselineSourceSha256"] = gate["baselineSourceSha256"]
    fixture._write(session_path, session)
    manifest_path = fixture.workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf8"))
    manifest.update(stage="fragment", entryPoint="MainPS")
    manifest["capture"]["capturePath"] = fixture.capture_binding["capturePath"]
    fixture._write(manifest_path, manifest)
    source = fixture.source.read_text("utf8")
    digest = hashlib.sha256(fixture.source.read_bytes()).hexdigest()
    overview = prepare_overview(source, [
        {"id": name.lower(), "title": name, "anchors": [{"function": name}]}
        for name in FUNCTIONS], "MainPS")
    overview["originalRawSha256"] = fixture.RAW_SHA256
    graph = build_shader_flow_graph(
        source, workspace_id=fixture.WORKSPACE_ID,
        source_path="source/shader.hlsl", source_sha256=digest,
        source_revision=2, stage="fragment", entry_point="MainPS",
        generated_at="2026-10-29T00:00:00Z", origin="organized-proposal")
    fixture._write(fixture.workspace / "ai/semantic-overview.json", overview)
    fixture._write(fixture.workspace / "ai/flow-graph.json", graph)
    # Registration can fail independently. Neither loader uses an edit grant.
    fixture.registry.unlink()
    return fixture, gate, overview, graph


def _projection(opened):
    return panel_module._build_source_flow_projection({
        "stop": threading.Event(), "sourceText": opened["text"],
        "sourceIdentity": {"workspaceId": opened["workspaceId"]},
        "entryPoint": opened["entryPoint"], "graph": opened.get("flowGraph"),
        "semanticOverview": opened.get("semanticOverview")})


@pytest.mark.parametrize("verified", [False, True])
def test_both_preview_loaders_deliver_same_fourteen_stages_as_canonical(tmp_path, verified):
    fixture, gate, overview, graph = _fixture(tmp_path)
    before = fixture.source.read_bytes()
    if verified:
        opened = console.load_verified_organization_preview(
            str(fixture.workspace), gate["organizationId"], gate["proposalSourceSha256"],
            fixture.capture_binding)
    else:
        opened = console.load_saved_shader_source_preview(
            str(fixture.workspace), gate["proposalSourceSha256"], fixture.capture_binding)
    projected = _projection(opened)
    assert [row["title"] for row in projected["stageOutline"]] == FUNCTIONS
    canonical = console._load_semantic_overview(str(fixture.workspace))
    canonical_rows = outline.build_stage_outline(Hierarchy(opened["text"]), "MainPS", canonical)
    assert [row["semanticId"] for row in projected["stageOutline"]] == [
        row["semanticId"] for row in canonical_rows]
    assert len(_projection(dict(opened, semanticOverview=None))["stageOutline"]) == 1
    assert opened["semanticOverview"] == overview
    assert len(opened["flowGraph"]["nodes"]) == len(graph["nodes"])
    assert all(node["source"]["documentId"] == opened["workspaceId"]
               for node in opened["flowGraph"]["nodes"])
    assert "semantic-overview.json" in " ".join(opened["watchedPaths"])
    assert fixture.source.read_bytes() == before
    assert opened["replayApplied"] is False
    assert not fixture.registry.exists()


def test_saved_source_reload_reanchors_edited_code_without_promoting_old_review(tmp_path):
    fixture, gate, overview, _graph = _fixture(tmp_path)
    opened = console.load_saved_shader_source_preview(
        str(fixture.workspace), gate["proposalSourceSha256"], fixture.capture_binding)
    fixture.source.write_bytes(b"// new earlier line\n" + fixture.source.read_bytes().replace(
        b"return x * 3;", b"return x * 3 + 1;"))
    refreshed = console.reload_saved_shader_source_preview(opened, fixture.capture_binding)
    projected = _projection(refreshed)
    assert len(projected["stageOutline"]) == 14
    assert projected["stageOutline"][0]["line"] > _projection(opened)["stageOutline"][0]["line"]
    assert refreshed["semanticOverview"]["sourceSha256"] == overview["sourceSha256"]
    assert refreshed["rawSha256"] != overview["sourceSha256"]
    assert not projected["stageOutlineState"].get("reviewed")
    assert refreshed["flowGraph"] is None  # exact-byte AI graph descriptions are stale


def test_preview_snapshot_watches_and_reloads_metadata_only_publication(tmp_path):
    fixture, gate, overview, _graph = _fixture(tmp_path)
    opened = console.load_saved_shader_source_preview(
        str(fixture.workspace), gate["proposalSourceSha256"], fixture.capture_binding)
    request = {"stop": threading.Event(), "force": True, "previousSignature": None,
               "watchedPaths": [], "registryPath": str(fixture.registry),
               "projectSelectionPath": str(tmp_path / "active.json"),
               "documentIds": [], "captureBinding": fixture.capture_binding,
               "sourcePreviews": {opened["workspaceId"]: opened}}
    with mock.patch.object(panel_module, "_shader_console_protocol", console), mock.patch.object(
            console, "load_console_snapshot", return_value={"workspaces": []}):
        first = panel_module._build_workspace_snapshot_files(request)
        metadata_path = str(fixture.workspace / "ai/semantic-overview.json")
        assert metadata_path in first["watchedPaths"]
        assert metadata_path in first["existingPaths"]
        overview["stages"][0]["title"] = "Surface edited metadata"
        fixture._write(fixture.workspace / "ai/semantic-overview.json", overview)
        request.update(force=False, watchedPaths=first["watchedPaths"], previousSignature=first["signature"])
        refreshed = panel_module._build_workspace_snapshot_files(request)
    source = refreshed["sources"][opened["workspaceId"]]
    assert source["rawSha256"] == opened["rawSha256"]
    assert source["semanticOverview"]["stages"][0]["title"] == "Surface edited metadata"


@pytest.mark.parametrize("field,value", [("originalRawSha256", "0" * 64), ("entryPoint", "Other")])
def test_preview_does_not_attach_another_shader_overview(tmp_path, field, value):
    fixture, gate, overview, _graph = _fixture(tmp_path)
    overview[field] = value
    fixture._write(fixture.workspace / "ai/semantic-overview.json", overview)
    opened = console.load_saved_shader_source_preview(
        str(fixture.workspace), gate["proposalSourceSha256"], fixture.capture_binding)
    assert opened["semanticOverview"] is None
    assert opened["text"]


@pytest.mark.parametrize("field,value", [("workspaceId", "0" * 32), ("sourceSha256", "0" * 64),
                                         ("stage", "vertex"), ("entryPoint", "Other")])
def test_preview_rejects_wrong_graph_binding_without_hiding_source_or_directory(tmp_path, field, value):
    fixture, gate, overview, graph = _fixture(tmp_path)
    graph[field] = value
    fixture._write(fixture.workspace / "ai/flow-graph.json", graph)
    opened = console.load_saved_shader_source_preview(
        str(fixture.workspace), gate["proposalSourceSha256"], fixture.capture_binding)
    assert opened["flowGraph"] is None and opened["flowGraphStatus"]["status"] == "invalid"
    assert opened["semanticOverview"] == overview and len(_projection(opened)["stageOutline"]) == 14


def test_preview_panel_keeps_and_refreshes_navigation_metadata_without_replacing_buffer(tmp_path):
    panel, _provider, _type, _canonical, _source, opened = _canonical_display_fixture(tmp_path)
    overview = prepare_overview(SOURCE, [
        {"id": name.lower(), "title": name, "anchors": [{"function": name}]}
        for name in FUNCTIONS], "MainPS")
    opened.update(text=SOURCE, semanticOverview=overview,
                  flowGraph={"graphId": "1" * 32}, flowGraphStatus={"available": True},
                  watchedPaths=[str(tmp_path / "ai/semantic-overview.json")])
    panel._workspaces.clear()
    panel._source_documents.clear()
    panel._documents.clear()
    panel.tab_bar.ids = []
    assert panel.show_organization_preview(opened)
    key = opened["workspaceId"]
    assert panel._workspaces[key]["semanticOverview"] == overview
    assert panel._workspaces[key]["flowGraph"] == opened["flowGraph"]
    updated = copy.deepcopy(opened)
    updated["semanticOverview"]["stages"][0]["title"] = "Renamed surface"
    result = {"snapshot": {"workspaces": []}, "eventIds": {}, "sources": {key: updated},
              "watchedPaths": opened["watchedPaths"], "existingPaths": [], "signature": ()}
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    assert panel._workspaces[key]["semanticOverview"]["stages"][0]["title"] == "Renamed surface"
    assert panel._documents[key]["editor"].GetText() == SOURCE
    assert not panel._documents[key]["dirty"]
    assert panel._workspaces[key]["autoApplyReady"] is False
