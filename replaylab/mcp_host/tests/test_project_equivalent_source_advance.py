"""A published edit returns across captures whose debug containers differ."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_projects as fixtures
from src.projects import ReplayProjectError
from src.shader_workspace.compile_source_unit import capture_compilation_unit


@pytest.fixture
def make_rig():
    fixture = fixtures.ReplayProjectTests("test_unchanged_previous_publication_advances_head_with_backup")
    fixture.setUp()

    def build(include=False, high_generation=False):
        f = fixture
        ca, sa, ia = f._catalog("equivalent-a", fixtures._dxbc(b"program", b"debug-a"), resource_id=101)
        cb, sb, ib = f._catalog("equivalent-b", fixtures._dxbc(b"program", b"debug-b"), resource_id=909)
        f.service.import_capture(ca)
        f.service.import_capture(cb)
        original = ('#include "material.hlsl"\n' if include else "") + "float4 main():SV_Target { return 1; }\n"
        a = f._workspace_status("source-a", ia, sa, original)
        b = f._workspace_status("source-b", ib, sb, "// untouched original source\n")
        for status in (a, b):
            status["capture"]["api"] = "D3D11"
            (Path(status["source"]["path"]).parent / "material.hlsl").write_text("float material=1;\n")

        def compile_publish(status, text, revision):
            source = Path(status["source"]["path"])
            source.write_text(text, encoding="utf-8")
            status["source"]["currentSha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
            status["source"]["modifiedSinceDecompile"] = True
            f._with_fully_validated_candidate(status, fixtures._dxbc(("compiled-"+str(revision)).encode()), revision=revision)
            status["latestCompiled"]["sourceDependencies"] = {
                "digest": capture_compilation_unit(source, source.read_bytes())["digest"]}
            manifest = Path(status["manifestPath"])
            saved = json.loads(manifest.read_text())
            saved.update(workspaceId=status["workspaceId"], baseline=status["baseline"],
                         targetProfile=status["targetProfile"], stage=status["stage"],
                         entryPoint=status["entryPoint"], source={
                             "path": "source/shader.hlsl", "sha256": status["source"]["currentSha256"],
                             "entryPoint": status["entryPoint"]})
            saved["latestCompiled"]["sourceDependencies"] = status["latestCompiled"]["sourceDependencies"]
            manifest.write_text(json.dumps(saved))
            return f.service.register_workspace(status)["knowledge"]

        first = compile_publish(a, original, 21)
        if high_generation:
            for revision in range(22, 26):
                first = compile_publish(a, original, revision)
        seeded = f.service.seed_workspace(first["knowledgeId"], b)
        assert seeded["matchMode"] == "code-interface-layout-exact"
        compile_publish(b, original, 3)
        newest = compile_publish(b, "float4 main():SV_Target { return 0.25; }\n", 4)
        receipt = f.service.published_workspace_source_advance(newest["knowledgeId"], a)
        assert receipt is not None
        assert receipt["previousKnowledgeId"] == first["knowledgeId"]
        return SimpleNamespace(f=f, a=a, b=b, first=first, newest=newest, receipt=receipt,
                               publish=compile_publish)

    yield build
    fixture.tearDown()


def test_latest_b_edit_advances_unchanged_a_across_debug_containers(make_rig):
    r = make_rig(high_generation=True)
    assert r.receipt["previousHeadGeneration"] > r.receipt["generation"]
    before = Path(r.a["source"]["path"]).read_bytes()
    seeded = r.f.service.seed_workspace(r.newest["knowledgeId"], r.a,
        published_source_advance=r.receipt)
    assert seeded["matchMode"] == "code-interface-layout-exact"
    assert seeded["requiresCompile"] and seeded["requiresLiveAbiRevalidation"]
    assert not seeded["applied"] and not seeded["compiled"]
    assert Path(r.a["source"]["path"]).read_bytes() == Path(r.b["source"]["path"]).read_bytes()
    assert Path(seeded["supersededLocalEdit"]["backupPath"]).read_bytes() == before
    from src.shader_workspace.project_source import validate_project_source
    manifest = json.loads(Path(r.a["manifestPath"]).read_text())
    receipt = validate_project_source(Path(r.a["workspacePath"]), manifest, manifest["source"])
    assert receipt["knowledgeId"] == r.newest["knowledgeId"]
    assert receipt["originalRawSha256"] == r.a["baseline"]["rawSha256"]
    assert receipt["requiresCompile"] and receipt["requiresLiveAbiRevalidation"]
    assert not receipt["applied"]


@pytest.mark.parametrize("change", ["source", "binary", "include", "revision", "head", "profile", "abi", "code"])
def test_equivalent_advance_preserves_unpublished_or_changed_state(make_rig, change):
    r = make_rig(include=change == "include")
    source = Path(r.a["source"]["path"])
    if change == "source":
        source.write_text("manual edit still in progress")
    elif change == "binary":
        Path(r.a["latestCompiled"]["candidateBlobPath"]).write_bytes(b"modified candidate")
    elif change == "include":
        (source.parent / "material.hlsl").write_text("float material=0.9;\n")
    elif change == "revision":
        path = Path(r.a["manifestPath"])
        value = json.loads(path.read_text())
        value["revision"] += 1
        path.write_text(json.dumps(value))
    elif change == "head":
        r.publish(r.b, "float4 main():SV_Target { return 0.75; }\n", 5)
    elif change == "profile":
        r.a["targetProfile"]["profile"] = "ps_4_0"
    elif change == "abi":
        r.a["baseline"]["abiSha256"] = "e" * 64
    else:
        r.a["baseline"]["codeSha256"] = "e" * 64
    before = source.read_bytes()
    with pytest.raises(ReplayProjectError):
        r.f.service.seed_workspace(r.newest["knowledgeId"], r.a, published_source_advance=r.receipt)
    assert source.read_bytes() == before


def test_cross_container_receipt_cannot_overwrite_a_newer_a_publication(make_rig):
    r = make_rig()
    newer = r.publish(r.a, "float4 main():SV_Target { return 0.8; }\n", 22)
    assert r.f.service.published_workspace_source_advance(r.newest["knowledgeId"], r.a) is None
    assert r.f.service.current_workspace_source_head(newer["knowledgeId"])["knowledge"]["sourceSha256"] == r.a["source"]["currentSha256"]
