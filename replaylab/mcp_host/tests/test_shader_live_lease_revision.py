"""Re-entering a delivered revision is not a later-save lease operation."""
from unittest import mock

import pytest

from src import server


@pytest.mark.parametrize("expected", [None, {"revision": 4, "candidateRawSha256": "a" * 64}])
def test_include_change_with_unchanged_main_source_rejects_old_apply(expected):
    status = {"sourceDependenciesCurrent": False,
              "representation": {"lane": "executable-editable", "compileEligible": True},
              "source": {"currentSha256": "b" * 64},
              "latestCompiled": {"sourceSha256": "b" * 64, "revision": 4, "rawSha256": "a" * 64},
              "revision": 4}
    with pytest.raises(RuntimeError, match="include files changed"):
        server._require_shader_candidate_binding(status, expected)


@pytest.mark.parametrize("revision", [4, 5, None, True, "3"])
def test_nonpreceding_lease_returns_to_normal_exact_pair_path(revision):
    with mock.patch.object(server, "_shared_live_abi_lease", return_value={"abiLeaseRevision": revision}), \
            mock.patch.object(server, "_shader_workspace_service") as service, \
            mock.patch.object(server.ipc, "call") as ipc:
        result = server._apply_shader_workspace_shared_resource_with_live_abi_lease(
            "workspace", expected_revision=4, expected_candidate_raw_sha256="a" * 64)
    assert result is None
    service.assert_not_called()
    ipc.assert_not_called()


def test_preceding_lease_keeps_later_save_path():
    with mock.patch.object(server, "_shared_live_abi_lease", return_value={"abiLeaseRevision": 3}), \
            mock.patch.object(server, "_shader_workspace_service", side_effect=RuntimeError("later-save")):
        with pytest.raises(RuntimeError, match="later-save"):
            server._apply_shader_workspace_shared_resource_with_live_abi_lease(
                "workspace", expected_revision=4, expected_candidate_raw_sha256="a" * 64)
