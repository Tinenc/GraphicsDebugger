"""Cross-capture source refresh against incomplete native module inventories."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from src import projects, project_restore
from src.projects import ReplayProjectError, ReplayProjectService
from src.shader_workspace.project_source import source_receipt, validate_project_source
from src.symbols.matcher import capture_identity
from tests import test_projects as fixtures
from tests.test_recovery_assets import _make_spirv


OLD_SOURCE = "#version 450\nlayout(location=0) out vec4 color;\nvoid main() { color=vec4(1); }\n"
NEW_SOURCE = OLD_SOURCE.replace("vec4(1)", "vec4(0.5)")


@pytest.fixture
def refresh_case():
    fixture = fixtures.ReplayProjectTests()
    fixture.setUp()
    try:
        fixture.project = fixture.root / "vulkan-project"
        ReplayProjectService.create(fixture.project, "Vulkan cross-capture source",
                                    game_id="example-game", build_id="build-1",
                                    platform="windows-x64", api_family="vulkan")
        fixture.service = ReplayProjectService(fixture.project)
        # Use an actual SPIR-V container, not DXBC with a relabelled API.
        catalog, shader, _ = fixture._catalog("t1", fixtures._dxbc(), resource_id=208035)
        blob = _make_spirv()
        Path(shader["rawPath"]).write_bytes(blob)
        document = json.loads(catalog.read_text(encoding="utf-8"))
        document["api"] = "Vulkan"
        document["shaders"][0].update(encoding="SPIR-V", codeSha256="", rawSize=len(blob),
                                     rawSha256=hashlib.sha256(blob).hexdigest())
        document["captureId"] = capture_identity(document, document["shaders"])
        catalog.write_text(json.dumps(document), encoding="utf-8")
        fixture.service.import_capture(catalog)
        shader = document["shaders"][0]

        def workspace(label, text, resource_id):
            root = fixture.root / label
            (root / "source").mkdir(parents=True)
            source = root / "source" / "shader.glsl"
            source.write_text(text, encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            (root / "ai").mkdir()
            (root / "ai" / "analysis.md").write_text("Shader calculations.\n", encoding="utf-8")
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            return {
                "schema": "renderdoc-ultra-shader-workspace-status/v1",
                "workspacePath": str(root), "manifestPath": str(root / "manifest.json"),
                "workspaceId": hashlib.md5(label.encode()).hexdigest(),
                "capture": {"captureId": document["captureId"], "api": "Vulkan"},
                "shader": {"resourceId": resource_id, "rawSha256": shader["rawSha256"]},
                "stage": "fragment", "entryPoint": "main",
                "targetProfile": {"containerFormat": "spirv", "stage": "fragment",
                                  "profile": "spirv", "apiEnvironment": "vulkan"},
                "source": {"present": True, "path": str(source), "format": "glsl",
                           "lane": "executable-editable", "currentSha256": digest,
                           "decompiledSha256": digest, "modifiedSinceDecompile": False},
                "baseline": {"rawSha256": shader["rawSha256"], "codeSha256": shader["rawSha256"],
                             "kind": "SPIRV", "abiSha256": "a" * 64},
            }

        old_status = workspace("origin-v1", OLD_SOURCE, 208035)
        old = fixture.service.register_workspace(old_status)["knowledge"]
        target = workspace("t3-target", OLD_SOURCE, 72473)
        # The target has its own actual reflected ABI. A module-only inventory
        # can have another ABI summary for the exact same original bytecode.
        target["baseline"]["abiSha256"] = "e" * 64
        target["capture"]["captureId"] = "3" * 64
        inventory = fixture._shader_inventory(
            resource_id=72473, raw_sha256=old["rawSha256"], layout_hash=old["layoutHash"],
            interface_hash=old["interfaceHash"], abi_hash="1" * 64,
            api="vulkan", encoding="SPIR-V", protocol_encoding="spirv")
        bridge = copy.deepcopy(inventory)
        bridge["shaders"][0].update(stableKeyComplete=False, interfaceComplete=True,
                                    encoding="spirv")
        inventory = project_restore._project_shader_inventory(bridge)
        yield fixture, workspace, old, target, inventory
    finally:
        fixture.tearDown()


def _install_import(fixture, knowledge, target, *, managed=True):
    root = Path(target["workspacePath"])
    record = {"path": "source/shader.glsl", "sha256": knowledge["sourceSha256"],
              "entryPoint": "main"}
    if managed:
        empty = copy.deepcopy(target)
        empty["source"]["present"] = False
        bundle = fixture.service.prepare_workspace_source(knowledge["knowledgeId"], empty)
        relative = "source-inputs/project-%s.glsl" % knowledge["sourceSha256"]
        (root / relative).parent.mkdir()
        (root / relative).write_bytes(bundle["sourceText"].encode("utf-8"))
        record.update(origin="verified-project-source", projectSource=source_receipt(bundle, relative))
        target["source"].update(origin=record["origin"], projectSource=record["projectSource"])
    manifest = {key: copy.deepcopy(target[key]) for key in
                ("workspaceId", "stage", "entryPoint", "targetProfile", "baseline")}
    manifest.update(source=record, routePlan={"editableFormat": "glsl"})
    projects.atomic_write_json(root / "manifest.json", manifest)
    return manifest


@pytest.mark.parametrize("managed", [True, False])
def test_latest_source_refresh_uses_target_baseline_without_binary_authority(refresh_case, managed):
    fixture, workspace, old, target, inventory = refresh_case
    old_manifest = _install_import(fixture, old, target, managed=managed)
    newer = fixture.service.register_workspace(workspace("origin-v2", NEW_SOURCE, 208035))["knowledge"]
    snapshot = fixture.service.reuse_snapshot_for_shader_inventory(inventory)
    match = snapshot["reusePlan"]["matches"][0]
    candidate = next(item for item in match["workspaceCandidates"] if item["knowledgeId"] == newer["knowledgeId"])
    assert inventory["shaders"][0]["interfaceComplete"] is False
    assert candidate["sourcePreparationOnly"] is True
    assert candidate["binaryCandidateEligible"] is False
    assert candidate["binaryCandidate"] is None
    result = fixture.service.seed_workspace_from_inventory(
        newer["knowledgeId"], target, inventory["shaders"][0], inventory, snapshot["binding"])
    root = Path(target["workspacePath"])
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    receipt = validate_project_source(root, manifest, manifest["source"])
    assert receipt["knowledgeId"] == newer["knowledgeId"]
    assert receipt["baselineAbiSha256"] == target["baseline"]["abiSha256"] != inventory["shaders"][0]["abiHash"]
    assert Path(target["source"]["path"]).read_text(encoding="utf-8") == NEW_SOURCE
    assert (root / receipt["inputPath"]).read_text(encoding="utf-8") == NEW_SOURCE
    if managed:
        previous = old_manifest["source"]["projectSource"]
        assert (root / previous["inputPath"]).read_text(encoding="utf-8") == OLD_SOURCE
        assert previous["receiptSha256"] != receipt["receiptSha256"]
    assert result["requiresCompile"] is True
    assert result["requiresLiveAbiRevalidation"] is True
    assert result["verifiedBinaryCandidateMaySkipCompile"] is False
    assert result["compiled"] is False
    assert result["applied"] is False
    assert "latestCompiled" not in manifest
    restored_status = copy.deepcopy(target)
    restored_status["source"].update(
        origin="verified-project-source", projectSource=receipt,
        currentSha256=newer["sourceSha256"])
    before_validation = copy.deepcopy(restored_status)
    project_restore._validate_prepared_project_source(
        restored_status, newer, project_restore._normal_shader_identity(inventory["shaders"][0]),
        {"projectId": receipt["projectId"], "projectPath": receipt["projectPath"],
         "capture": {"inventoryOnly": True}}, source_preparation_only=True)
    assert restored_status == before_validation


def test_latest_source_refresh_preserves_current_unsaved_edit(refresh_case):
    fixture, workspace, old, target, inventory = refresh_case
    _install_import(fixture, old, target)
    newer = fixture.service.register_workspace(workspace("origin-v2", NEW_SOURCE, 208035))["knowledge"]
    draft = OLD_SOURCE + "// Local unfinished lighting adjustment.\n"
    source_path = Path(target["source"]["path"])
    source_path.write_text(draft, encoding="utf-8")
    target["source"].update(currentSha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
                            modifiedSinceDecompile=True)
    manifest_path = Path(target["manifestPath"])
    original_manifest = manifest_path.read_bytes()
    snapshot = fixture.service.reuse_snapshot_for_shader_inventory(inventory)
    with pytest.raises(ReplayProjectError, match="still unmodified"):
        fixture.service.seed_workspace_from_inventory(
            newer["knowledgeId"], target, inventory["shaders"][0], inventory, snapshot["binding"])
    assert source_path.read_text(encoding="utf-8") == draft
    assert manifest_path.read_bytes() == original_manifest


@pytest.mark.parametrize("change", ["resource", "raw", "baseline", "manifest-baseline"])
def test_incomplete_inventory_does_not_bypass_current_target_identity(refresh_case, change):
    fixture, workspace, old, target, inventory = refresh_case
    _install_import(fixture, old, target)
    newer = fixture.service.register_workspace(workspace("origin-v2", NEW_SOURCE, 208035))["knowledge"]
    source_path = Path(target["source"]["path"])
    if change == "resource":
        target["shader"]["resourceId"] = 208035
    elif change == "raw":
        target["shader"]["rawSha256"] = "8" * 64
    elif change == "baseline":
        target["baseline"]["rawSha256"] = "8" * 64
    else:
        manifest_path = Path(target["manifestPath"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["baseline"]["abiSha256"] = "8" * 64
        projects.atomic_write_json(manifest_path, manifest)
    with pytest.raises(ReplayProjectError, match="ResourceId|identity differs|provenance changed"):
        fixture.service.seed_workspace_from_inventory(
            newer["knowledgeId"], target, inventory["shaders"][0], inventory)
    assert source_path.read_text(encoding="utf-8") == OLD_SOURCE
