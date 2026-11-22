"""Manual apply receipts remain explicit through live-edit result handling."""
import copy
from types import SimpleNamespace

import pytest

from src.shader_live_edit import _LiveEditJob, ShaderLiveEditError
from tests.test_shader_live_edit import _compatible_result


def manual_result(root):
    result = _compatible_result(root, 6, "6" * 64)
    result["validation"] = {"replayAbiValidated": False, "manualEdit": True,
        "abiValidationMode": "manual-fast", "validationScope": "manual-edit-unvalidated",
        "freshPhysicalValidationPerformed": False, "freshLiveCaptureValidationPerformed": False,
        "preliminaryReplayReflectionSkipped": True}
    return result


def test_manual_success_does_not_forge_abi_compatibility(tmp_path):
    job = object.__new__(_LiveEditJob)
    job.workspace = tmp_path
    result = job._validated_apply_result(manual_result(tmp_path), 6, "6" * 64)
    assert result["applied"] is True
    assert result["validation"]["replayAbiValidated"] is False
    assert result["validation"]["manualEdit"] is True
    assert result["validation"]["abiValidationMode"] == "manual-fast"


@pytest.mark.parametrize("manual", [False, True])
def test_apply_timings_survive_with_only_bounded_finite_counters(tmp_path, manual):
    job = object.__new__(_LiveEditJob)
    job.workspace = tmp_path
    result = manual_result(tmp_path) if manual else _compatible_result(tmp_path, 6, "6" * 64)
    result["timingsMs"] = {"prepare": 81.5, "total": 250, "negative": -1,
        "boolean": True, "infinite": float("inf"), "nan": float("nan"),
        "object": {}, "string": "17", "huge": 10 ** 400, "bad/key": 10}
    result["replacement"]["timingsMs"] = {"replaceResource": 31, "total": 50.25,
        "negative": -2, "false": False}
    receipt = job._validated_apply_result(result, 6, "6" * 64)
    assert receipt["timingsMs"] == {"prepare": 81.5, "total": 250.0}
    assert receipt["replacement"]["timingsMs"] == {"replaceResource": 31.0, "total": 50.25}
    assert receipt["validation"]["replayAbiValidated"] is not manual


def test_timing_counter_diagnostics_are_bounded():
    from src.shader_live_edit import _timing_counters
    assert _timing_counters(None) == {}
    counters = _timing_counters({"phase%d" % i: i for i in range(100)})
    assert len(counters) == 32
    assert _timing_counters({"a" * 65: 1, "total": 86400001}) == {}


@pytest.mark.parametrize("field,value", [("manualEdit", False), ("abiValidationMode", "live-lease"),
    ("validationScope", "full-replay-reflection"), ("freshPhysicalValidationPerformed", True)])
def test_unvalidated_success_requires_complete_manual_policy(tmp_path, field, value):
    job = object.__new__(_LiveEditJob); job.workspace = tmp_path
    result = manual_result(tmp_path)
    result["validation"][field] = value
    with pytest.raises(ShaderLiveEditError, match="without replay ABI"):
        job._validated_apply_result(result, 6, "6" * 64)


@pytest.mark.parametrize("field,value", [("originalShaderPreserved", False), ("eventScoped", True),
                                        ("candidateRawSha256", "7" * 64)])
def test_manual_policy_still_preserves_original_scope_and_exact_candidate(tmp_path, field, value):
    job = object.__new__(_LiveEditJob); job.workspace = tmp_path
    result = manual_result(tmp_path)
    result["replacement"][field] = value
    with pytest.raises(ShaderLiveEditError):
        job._validated_apply_result(result, 6, "6" * 64)


def test_identical_manual_save_reuses_active_target_without_changing_validation_claim(tmp_path):
    job = object.__new__(_LiveEditJob); job.workspace = tmp_path
    job._active_apply = manual_result(tmp_path)
    identity = {"sourceSha256": "a" * 64, "sourceDependenciesDigest": ""}
    job._active_apply["sourceIdentity"] = identity
    job._apply_reuses = 0
    events = []
    job._record_locked = lambda name, **data: events.append((name, data))
    assert job._reuse_active_replacement_locked(7, "6" * 64, identity)
    assert job._apply_reuses == 1 and job._last_apply["mutationPerformed"] is False
    assert job._last_apply["validation"]["replayAbiValidated"] is False
    assert job._last_apply["validation"]["manualEdit"] is True
    assert events[0][0] == "applyReused"
    changed_source = dict(identity, sourceSha256="b" * 64)
    assert not job._reuse_active_replacement_locked(8, "6" * 64, changed_source)
