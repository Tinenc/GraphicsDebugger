"""Session-bound strict ABI reuse must not cross a capture reopen."""
import os
from unittest import mock

import pytest

from tests import test_shader_restore_receipts as receipts


@pytest.fixture
def case():
    fixture = receipts.ShaderRestoreReceiptTests()
    fixture.setUp()
    yield fixture
    fixture.doCleanups()


@pytest.mark.parametrize("include_session", [False, True])
def test_legacy_strict_client_without_session_still_runs_the_original_validation(case, include_session):
    bridge, ctrl = case.bridge()
    bridge._known_catalog_capture_ids = {"c" * 64: os.path.normcase(str(case.capture))}
    params = case.params()
    if include_session:
        params["expected_capture_session_id"] = "7" * 32
    with mock.patch.object(bridge, "_dxil_evidence_from_live_disassembly",
                           return_value=case.captured_dxil_evidence(bridge)), \
            mock.patch.object(bridge, "_dxil_ambiguous_interface_compatible", return_value=True) as abi:
        result = bridge._apply_shader_candidate_shared_resource(params)
    assert result["configured"] and result["captureSessionId"] == "7" * 32
    assert ("replace", 77, 100) in ctrl.calls
    assert ("reflect", 100) in ctrl.calls
    abi.assert_called_once()


@pytest.mark.parametrize("session", ["8" * 32, "", "bad", "7" * 64])
def test_strict_apply_rejects_wrong_or_malformed_optional_session_before_build(case, session):
    bridge, ctrl = case.bridge()
    params = dict(case.params(), expected_capture_session_id=session)
    with pytest.raises((ValueError, RuntimeError), match="session"):
        bridge._apply_shader_candidate_shared_resource(params)
    assert not ctrl.calls
    assert not ctrl.replacements


@pytest.mark.parametrize("change", ["session", "capture-token"])
def test_strict_apply_rechecks_session_on_replay_thread_before_any_target_build(case, change):
    bridge, ctrl = case.bridge()
    bridge._known_catalog_capture_ids = {"c" * 64: os.path.normcase(str(case.capture))}
    params = dict(case.params(), expected_capture_session_id="7" * 32)
    def replay(callback):
        if change == "session":
            bridge._capture_session_id = "8" * 32
        else:
            bridge._current_capture_token = lambda: "another-capture"
        return callback(ctrl)
    bridge._replay = replay
    with pytest.raises(RuntimeError, match="session changed before replay mutation"):
        bridge._apply_shader_candidate_shared_resource(params)
    assert not any(row[0] in ("build", "reflect", "replace") for row in ctrl.calls)
    assert not ctrl.replacements
