"""A reader preview augments the exact source asset without full-tree reads."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
from unittest import mock

import pytest

from src import asset_texture_preview, server, tool_runner, uex_profiles
from src.active_projects import ActiveReplayProjectService
from src.asset_catalog import AssetCatalogService
from src.symbols.imaging import fingerprint
from tests.test_texture_identity import gradient_rgba, png_bytes, texture_package


@pytest.fixture
def preview_case(tmp_path, monkeypatch):
    root = tmp_path / "assets"
    mount = root / "unpacked"
    content = mount / "Project" / "Content"
    content.mkdir(parents=True)
    (content.parent / "Project.uproject").write_text("{}")
    owner = content / "T_Surface.uasset"
    owner.write_bytes(texture_package("T_Crow_Body_D"))
    for suffix in (".uexp", ".ubulk", ".m.ubulk", ".uptnl"):
        owner.with_suffix(suffix).write_bytes(b"exact texture payload")
    service = ActiveReplayProjectService(tmp_path / "selection.json", tmp_path / "projects")
    status = service.create_project(name="Fixture", game_id="game", build_id="fixture", asset_root=root)
    database = service.shared_dictionary_directory("assets") / server.ACTIVE_ASSET_CATALOG_FILENAME
    catalog = AssetCatalogService(database)
    catalog.scan(root, build_id="fixture", depth="headers")
    config = tmp_path / "profile.json"
    config.write_text(json.dumps({"mountRoot": str(mount)}))
    profile = {"configPath": str(config), "mountRoot": str(mount), "profile": "fixture"}
    operation = {"name": "uex_preview_texture", "program": "uex.exe", "kind": "cli",
                 "argv": ["--config", "{uexConfig}", "preview-texture", "--profile", "{uexProfile}",
                          "--out", "{outputDir}/preview.png", "{asset}"],
                 "inputSchema": {"type": "object", "properties": {"asset": {"type": "string"}},
                                 "required": ["asset"]}}
    output_root = tmp_path / "reader-output"
    output_root.mkdir()
    state = {"mutate": lambda receipt: None, "original": None, "invocations": 0}

    def run(op, arguments, *, program, output_root, host_slots, **unused):
        state["invocations"] += 1
        output = output_root / ("run-%016x" % state["invocations"])
        output.mkdir()
        image = output / "preview.png"
        image.write_bytes(png_bytes(gradient_rgba(32, 32), 32, 32))
        receipt = {"operation": op["name"], "succeeded": True,
                   "argv": tool_runner.build_argv(op, arguments, program=program,
                                                   output_dir=str(output), host_slots=host_slots),
                   "outputDirectory": str(output), "producedFiles": [{"path": str(image), "byteSize": image.stat().st_size}],
                   "stdout": "decoder output", "stdoutFileTruncated": False, "exitCode": 0}
        state["mutate"](receipt)
        state["original"] = copy.deepcopy(receipt)
        return receipt

    monkeypatch.setattr(server, "_active_project_service", lambda: service)
    monkeypatch.setattr(server, "_declared_asset_operations", lambda: [operation])
    monkeypatch.setattr(server, "_asset_tool_output_root", lambda: output_root)
    monkeypatch.setattr(uex_profiles, "ensure_profile", lambda unused: profile)
    monkeypatch.setattr(tool_runner, "run_operation", run)
    return {"root": root, "mount": mount, "owner": owner, "catalog": catalog, "service": service,
            "status": status, "profile": profile, "output_root": output_root, "state": state,
            "reference": "Project/Content/T_Surface.0"}


def invoke(case):
    return json.loads(asyncio.run(server.run_asset_tool_operation(
        "uex_preview_texture", {"asset": case["reference"]})))


def test_production_hook_indexes_bound_png_and_preserves_reader_receipt(preview_case):
    case = preview_case
    with mock.patch.object(AssetCatalogService, "scan", side_effect=AssertionError("no tree scan")), \
            mock.patch.object(AssetCatalogService, "content_manifest", create=True,
                              side_effect=AssertionError("no whole-tree manifest")):
        result = invoke(case)
    assert result["succeeded"] is True, result
    assert all(result[key] == value for key, value in case["state"]["original"].items())
    indexed = result["texturePreviewIndex"]
    assert indexed["indexed"] is True, indexed
    assert indexed["physicalIdentityVerified"] is False
    assert result["argv"][-1] == "Project/Content/T_Surface.uasset"
    assert len(indexed["sourceFiles"]) == 5
    relative = case["owner"].relative_to(case["root"]).as_posix()
    assert indexed["relativePath"] == relative
    row = case["catalog"].query(relative_paths=[relative])["assets"][0]
    provenance = row["metadata"]["textureFingerprint"]["previewProvenance"]
    expected = hashlib.sha256(json.dumps(case["state"]["original"], sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert provenance["receiptSha256"] == expected
    fp = fingerprint(gradient_rgba(32, 32), 32, 32)
    matches = case["catalog"].search_thumbnails(fp["dhash"])["candidates"]
    assert any(item["relativePath"] == relative for item in matches)
    assert case["catalog"].verify()["verified"] is True


def test_directory_only_texture_is_hydrated_without_a_tree_walk(preview_case):
    case = preview_case
    case["catalog"].database_path.unlink()
    case["catalog"].scan(case["root"], build_id="fixture", depth="names")
    relative = case["owner"].relative_to(case["root"]).as_posix()
    assert not case["catalog"].query(relative_paths=[relative])["assets"][0]["contentSha256"]
    hydrate = AssetCatalogService.hydrate
    hydrated = []
    def exact_hydrate(catalog, root, paths, **kwargs):
        hydrated.extend(paths)
        return hydrate(catalog, root, paths, **kwargs)
    with mock.patch.object(AssetCatalogService, "scan", side_effect=AssertionError("no scan")), \
            mock.patch.object(AssetCatalogService, "_detect_engine", side_effect=AssertionError("no mount discovery")), \
            mock.patch.object(AssetCatalogService, "hydrate", exact_hydrate):
        result = invoke(case)
    assert result["texturePreviewIndex"]["indexed"] is True, result
    assert hydrated == [relative]
    assert case["catalog"].verify()["verified"] is True


def test_preview_does_not_hydrate_a_file_missing_from_directory_index(preview_case):
    case = preview_case
    extra = case["owner"].with_name("T_New.uasset")
    extra.write_bytes(texture_package("T_Crow_Body_D"))
    case["reference"] = "Project/Content/T_New.0"
    with mock.patch.object(AssetCatalogService, "hydrate", side_effect=AssertionError("unindexed path")):
        result = invoke(case)
    assert result["succeeded"] is True
    assert result["texturePreviewIndex"]["reason"] == "texture-reference-not-indexed"


@pytest.mark.parametrize("suffix", [".uasset", ".uexp", ".ubulk", ".m.ubulk", ".uptnl"])
def test_reader_input_changes_do_not_publish_but_preserve_preview(preview_case, suffix):
    case = preview_case
    def mutate(receipt):
        path = case["owner"].with_suffix(suffix)
        stat = path.stat()
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    case["state"]["mutate"] = mutate
    result = invoke(case)
    assert result["succeeded"] is True
    assert result["texturePreviewIndex"]["indexed"] is False
    assert result["texturePreviewIndex"]["reason"] == "reader-inputs-changed"


def test_companion_appearing_after_preflight_invalidates_closure(preview_case):
    case = preview_case
    payload = case["owner"].with_suffix(".uptnl")
    payload.unlink()
    case["state"]["mutate"] = lambda receipt: payload.write_bytes(b"new optional payload")
    result = invoke(case)
    assert result["succeeded"] is True
    assert result["texturePreviewIndex"]["reason"] == "reader-inputs-changed"


@pytest.mark.parametrize("change", ["profile", "project", "root", "build"])
def test_reader_context_changes_cannot_redirect_cache_write(preview_case, tmp_path, change):
    case = preview_case
    def mutate(receipt):
        if change == "profile":
            Path(case["profile"]["configPath"]).write_text("changed profile")
        elif change == "project":
            case["service"].create_project(name="Other", game_id="other", build_id="other", asset_root=case["root"])
        elif change == "root":
            other = tmp_path / "other-root"
            other.mkdir()
            case["service"].configure_asset_root(other)
        else:
            import sqlite3
            with sqlite3.connect(case["catalog"].database_path) as db:
                db.execute("UPDATE metadata SET value='other-build' WHERE key='buildId'")
    case["state"]["mutate"] = mutate
    result = invoke(case)
    assert result["succeeded"] is True, result
    assert result["texturePreviewIndex"]["indexed"] is False
    assert case["catalog"].coverage()["thumbnailRows"] == 0


@pytest.mark.parametrize("change", ["wrong-output", "not-produced", "wrong-asset", "wrong-profile", "wrong-size", "failed"])
def test_only_exact_successful_host_output_is_cached(preview_case, tmp_path, change):
    case = preview_case
    def mutate(receipt):
        if change == "wrong-output":
            receipt["outputDirectory"] = str(tmp_path)
        elif change == "not-produced":
            receipt["producedFiles"] = []
        elif change == "wrong-asset":
            receipt["argv"][-1] = "Project/Content/OtherTexture.0"
        elif change == "wrong-profile":
            receipt["argv"][receipt["argv"].index("--profile") + 1] = "other"
        elif change == "wrong-size":
            receipt["producedFiles"][0]["byteSize"] += 1
        else:
            receipt["succeeded"] = False
    case["state"]["mutate"] = mutate
    result = invoke(case)
    assert result["succeeded"] is (change != "failed")
    assert result["texturePreviewIndex"]["indexed"] is False
    assert case["catalog"].coverage()["thumbnailRows"] == 0


@pytest.mark.parametrize("missing", ["catalog", "exact-reference"])
def test_missing_index_keeps_preview_success_and_reports_coverage_gap(preview_case, missing):
    case = preview_case
    if missing == "catalog":
        case["catalog"].database_path.unlink()
    else:
        case["reference"] = "T_Surface.0"
    result = invoke(case)
    assert result["succeeded"] is True, result
    assert case["state"]["invocations"] == 1
    assert result["texturePreviewIndex"]["indexed"] is False
    assert result["texturePreviewIndex"]["reason"] in {"catalog-not-indexed", "texture-reference-not-indexed"}
    assert result["texturePreviewIndex"]["absenceProven"] is False
