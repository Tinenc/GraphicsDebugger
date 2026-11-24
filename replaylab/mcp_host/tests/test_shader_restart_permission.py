from __future__ import annotations

import json
from unittest import mock

from src import server


def test_explicit_restart_replaces_only_managed_draft(tmp_path) -> None:
    manager = mock.Mock()
    manager.begin.return_value = {"organizationId": "replacement", "proposalSourcePath": "ai/draft.hlsl",
        "workspacePath":str(tmp_path/'workspace'), "proposalSourceSha256":"b"*64}
    context = {"stage": "fragment", "shader": {"rawSha256": "a" * 64}}
    with (
        mock.patch.object(server, "_shader_organization_semantic_context", return_value=context),
        mock.patch.object(server, "_shader_organization_manager", return_value=manager),
    ):
        result = json.loads(server.begin_shader_workspace_organization(
            "D:/managed/workspace", event_id=11834, stage="fragment", restart=True,
        ))

    assert result["organizationId"] == "replacement"
    assert "completionScope" not in result
    assert "assetGate" not in result
    manager.begin.assert_called_once_with("D:/managed/workspace", context, restart=True)


def test_begin_rejects_mismatched_physical_stage() -> None:
    manager = mock.Mock()
    with (
        mock.patch.object(server, "_shader_organization_semantic_context", return_value={"stage": "fragment"}),
        mock.patch.object(server, "_shader_organization_manager", return_value=manager),
    ):
        result = json.loads(server.begin_shader_workspace_organization(
            "D:/managed/workspace", event_id=11834, stage="vertex",
        ))
    assert "error" in result
    manager.begin.assert_not_called()
