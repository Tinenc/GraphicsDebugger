"""Historical organization metadata never controls a new workspace draft."""

from __future__ import annotations

from pathlib import Path

import test_shader_organization as harness
from src.shader_workspace.organization import ShaderOrganizationManager


def test_new_workspace_uses_its_current_source_even_with_stale_import(tmp_path):
    target = tmp_path / "new-workspace"
    target.mkdir()
    source = b"float4 MainPS() : SV_Target { return 0.75; }\n"
    service = harness._FakeWorkspaceService(target, source)
    ai_dir = target / "ai"
    ai_dir.mkdir()
    legacy_import = ai_dir / "organization-import.json"
    legacy_import.write_text("{broken old progress", encoding="utf-8")
    (ai_dir / "organization-rounds.json").write_text("{broken old budget", encoding="utf-8")
    manager = ShaderOrganizationManager(lambda: service, lambda *_: {})

    opened = manager.begin(str(target), {"eid": 11384})
    assert opened["baselineCompiledAndAbiValidated"] is False
    assert Path(opened["proposalSourcePath"]).read_bytes() == source
    assert service.source_path.read_bytes() == source
    assert service.revision == 0
    assert legacy_import.read_text(encoding="utf-8") == "{broken old progress"
    assert manager.resume_summary(target)["organizationId"] == opened["organizationId"]
