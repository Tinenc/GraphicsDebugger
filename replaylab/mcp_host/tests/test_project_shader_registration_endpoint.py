from __future__ import annotations

import json
from unittest import mock

import pytest

from src import project_restore, server


@pytest.mark.parametrize("embed_capture_index", [False, True])
def test_explicit_shader_registration_forwards_capture_index_choice(
    embed_capture_index: bool,
) -> None:
    receipt = {"projectCommitted": True}
    with mock.patch.object(
        project_restore, "sync_active_shader_workspace_after_apply", return_value=receipt
    ) as sync:
        result = json.loads(
            server.register_active_project_shader_replacement(
                "managed-workspace", 109, "a" * 64, "b" * 32, embed_capture_index
            )
        )

    assert result == receipt
    sync.assert_called_once_with(
        "managed-workspace",
        expected_revision=109,
        expected_candidate_raw_sha256="a" * 64,
        replacement_id="b" * 32,
        embed_capture_index=embed_capture_index,
    )


@pytest.mark.parametrize("embed_capture_index", [1, "false", None])
def test_shader_registration_rejects_non_boolean_capture_index_choice(
    embed_capture_index: object,
) -> None:
    with mock.patch.object(
        project_restore, "sync_active_shader_workspace_after_apply"
    ) as sync:
        result = json.loads(
            server.register_active_project_shader_replacement(
                "managed-workspace", 109, "a" * 64, "b" * 32, embed_capture_index
            )
        )

    assert result["error"] == "embed_capture_index must be a boolean"
    sync.assert_not_called()
