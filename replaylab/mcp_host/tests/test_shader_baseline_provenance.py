from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.shader_workspace.organization import (
    ShaderOrganizationManager,
    _baseline_source_provenance,
)
from tests.test_shader_organization import _FakeWorkspaceService


class _ImportedService(_FakeWorkspaceService):
    def status(self, workspace_path):
        result = super().status(workspace_path)
        result["source"]["origin"] = "verified-project-source"
        return result


@pytest.mark.parametrize("existing_artifact", [False, True])
def test_project_begin_never_calls_reused_source_decompiler_output(
    tmp_path: Path, monkeypatch, existing_artifact: bool
):
    monkeypatch.setenv("REPLAYLAB_CONFIG_DIR", str(tmp_path / "config"))
    root = tmp_path / "workspace"
    root.mkdir()
    source = b"float4 MainPS() : SV_Target { return 1.0; }\n"
    service = _ImportedService(root, source)
    original = root / "ai" / "decompiler-original.hlsl"
    if existing_artifact:
        original.parent.mkdir()
        original.write_bytes(source)

    manager = ShaderOrganizationManager(lambda: service, lambda *args: {})
    result = manager.begin(str(root), {"schema": "test-context/v1"})
    provenance = result["semanticContext"]["baselineSourceProvenance"]
    session = json.loads(Path(result["sessionPath"]).read_text("utf-8"))

    assert provenance["kind"] == "verified-project-source"
    assert provenance["initialSourceOrigin"] == "verified-project-source"
    assert provenance["initialSourceSha256"] == hashlib.sha256(source).hexdigest()
    assert provenance["declaredDecompilerSourceSha256"] is None
    assert provenance["decompilerOriginalPreserved"] is False
    assert session["decompilerOriginalPath"] is None
    assert session["decompilerOriginalSha256"] is None
    assert original.exists() is existing_artifact
    if existing_artifact:
        assert original.read_bytes() == source


def test_project_abi_repair_uses_same_provenance_rule(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REPLAYLAB_CONFIG_DIR", str(tmp_path / "config"))
    root = tmp_path / "workspace"
    root.mkdir()
    source = b"float4 MainPS() : SV_Target { return 1.0; }\n"
    service = _ImportedService(root, source)
    manager = ShaderOrganizationManager(lambda: service, lambda *args: {})
    result = manager.repair_baseline_source(
        str(root), hashlib.sha256(source).hexdigest(),
        source.decode() + "// compile-error\n",
        acknowledge_semantic_equivalence_unverified=True,
    )
    assert result["phase"] == "compile-check"
    assert result["baselineSourceProvenance"]["kind"] == "verified-project-source"
    assert not (root / "ai" / "decompiler-original.hlsl").exists()
    assert service.source_path.read_bytes() == source


@pytest.mark.parametrize("origin", [
    "toolchain-migration-preserved-source",
    "toolchain-refreshed-source",
    "explicit-explanatory-promotion",
    None,
])
def test_preserved_or_promoted_source_is_not_a_decompiler_result(tmp_path: Path, origin):
    source = b"float4 MainPS() : SV_Target { return 1.0; }\n"
    digest = hashlib.sha256(source).hexdigest()
    result = _baseline_source_provenance(
        {"source": {"format": "hlsl", "origin": origin, "decompiledSha256": digest}},
        tmp_path, source, digest, "a" * 64,
    )
    assert result["kind"] == "preserved-workspace-source"
    assert result["decompilerOriginalPreserved"] is False
    assert result["declaredDecompilerSourceSha256"] is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("origin", ["trusted-decompiler", "trusted-decompile-route"])
def test_true_decompiler_output_is_preserved(tmp_path: Path, origin):
    source = b"float4 MainPS() : SV_Target { return 1.0; }\n"
    digest = hashlib.sha256(source).hexdigest()
    result = _baseline_source_provenance(
        {"source": {"format": "hlsl", "origin": origin, "decompiledSha256": digest}},
        tmp_path, source, digest, "a" * 64,
    )
    assert result["kind"] == "decompiler-output"
    assert result["decompilerOriginalPreserved"] is True
    assert result["declaredDecompilerSourceSha256"] == digest
    assert (tmp_path / "decompiler-original.hlsl").read_bytes() == source
