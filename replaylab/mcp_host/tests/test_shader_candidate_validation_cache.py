"""One native preliminary reflection per exact current-session candidate."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from src import server
from src.shader_candidate_validation_cache import ShaderCandidateValidationCache


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    gate = {"status": "compatible", "requestId": "a" * 64, "reportSha256": "b" * 64,
            "baselineAbiSha256": "c" * 64, "candidateAbiSha256": "c" * 64}
    status = {
        "workspaceId": "1" * 32, "workspacePath": str(tmp_path), "revision": 2,
        "capture": {"captureId": "2" * 64, "catalogSha256": "3" * 64,
                    "capturePath": str(tmp_path / "frame.rdc"), "api": "Vulkan"},
        "shader": {"resourceId": 77, "rawSha256": "4" * 64},
        "baseline": {"rawSha256": "4" * 64, "abiSha256": "c" * 64},
        "stage": "fragment", "entryPoint": "main", "targetProfile": {"profile": "ps_6_0"},
        "representation": {"lane": "executable-editable", "compileEligible": True},
        "source": {"path": str(tmp_path / "shader.hlsl"), "format": "hlsl",
                   "currentSha256": "5" * 64, "dependenciesDigest": "6" * 64,
                   "dependencyDocumentCount": 1},
        "compilerBinding": {"bindingSha256": "7" * 64, "hops": [{"toolId": "dxc"}]},
        "routePlan": {"planSha256": "8" * 64}, "sourceDependenciesCurrent": True,
        "abiValidationStatus": "compatible", "abiCompatible": True, "replayAbiValidated": True,
        "latestCompiled": {"revision": 2, "sourceSha256": "5" * 64, "rawSha256": "9" * 64,
            "kind": "spirv", "abiGate": gate, "replayAbiValidated": True,
            "abiCompatible": True, "requiresReplayAbiValidation": False,
            "semanticEquivalenceVerified": False, "validationScope": "replay-reflection-abi-compatible"}}
    request = {"requestId": "a" * 64,
        "binding": {"workspaceId": "1" * 32, "captureId": "2" * 64, "revision": 2,
                    "candidateRawSha256": "9" * 64, "baselineAbiSha256": "c" * 64,
                    "stage": "fragment", "entryPoint": "main", "api": "vulkan", "encoding": "spirv"},
        "params": {"candidate_path": str(tmp_path / "candidate.spv"), "expected_sha256": "9" * 64}}
    validated = {"captureSessionId": "d" * 32, "compatible": True, "replayAbiValidated": True,
                 "requiresReplayAbiValidation": False, "revision": 2,
                 "requestId": "a" * 64, "abiReportSha256": "b" * 64}
    cache = ShaderCandidateValidationCache()
    monkeypatch.setattr(server, "_SHADER_CANDIDATE_VALIDATIONS", cache)
    monkeypatch.setattr(server, "_SHADER_OVERRIDE_CAPTURE_KEY", "d" * 32)
    service = mock.Mock()
    service.status.side_effect = lambda unused: copy.deepcopy(status)
    service.candidate_reflection_request.side_effect = lambda unused: copy.deepcopy(request)
    service.validate_candidate_reflection.side_effect = lambda *unused: copy.deepcopy(validated)
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    live = {"captureSessionId": "d" * 32}
    catalog = mock.Mock(side_effect=lambda *a, **kw: copy.deepcopy(live))
    monkeypatch.setattr(server, "_ensure_workspace_capture_catalog_identity", catalog)
    ipc = mock.Mock(return_value={"captureId": "2" * 64, "captureSessionId": "d" * 32})
    monkeypatch.setattr(server.ipc, "call", ipc)
    return SimpleNamespace(path=str(tmp_path), status=status, request=request, validated=validated,
                           cache=cache, service=service, live=live, catalog=catalog, ipc=ipc)


@pytest.mark.parametrize("api,encoding", [
    ("d3d11", "dxbc"), ("d3d12", "dxbc"), ("d3d12", "dxil"),
    ("vulkan", "spirv"), ("opengl", "opengl-spirv"), ("opengl", "glsl"),
])
def test_fresh_validation_then_apply_reuses_only_current_success(candidate, api, encoding):
    c = candidate
    c.status["capture"]["api"] = api
    c.status["latestCompiled"]["kind"] = encoding
    c.request["binding"].update(api=api, encoding=encoding)
    first = server._fresh_shader_candidate_validation(c.path, 2, "9" * 64)
    assert first[3]["replayAbiValidated"]
    assert c.cache.contains(c.path)
    result = server._shader_candidate_validation_for_apply(c.path, 2, "9" * 64)
    assert result[3]["reusedCurrentSessionValidation"]
    assert result[3]["freshPreliminaryReplayReflectionPerformed"] is False
    assert c.ipc.call_count == 1
    assert c.ipc.call_args.args[0] == "reflect_shader_candidate"
    assert c.service.validate_candidate_reflection.call_count == 1
    assert c.catalog.call_count == 2


def test_explicit_validate_still_obtains_new_replay_evidence(candidate):
    c = candidate
    for _ in range(2):
        server._fresh_shader_candidate_validation(c.path, 2, "9" * 64)
    assert c.ipc.call_count == 2
    assert c.service.validate_candidate_reflection.call_count == 2


def test_local_file_validation_failure_never_falls_back_to_cached_success(candidate):
    c = candidate
    c.cache.remember(c.path, c.status, c.request, c.validated)
    c.service.status.side_effect = RuntimeError("candidate ABI report hash mismatch")
    with pytest.raises(RuntimeError, match="report hash mismatch"):
        server._shader_candidate_validation_for_apply(c.path, 2, "9" * 64)
    c.ipc.assert_not_called()


@pytest.mark.parametrize("field", [
    "source", "include", "include-count", "compiler", "compiler-config", "route", "entry",
    "stage", "original", "resource", "capture", "capture-path", "catalog", "candidate",
    "revision", "request", "request-params", "report", "baseline-abi", "candidate-abi",
    "profile", "session", "manual", "incompatible", "dependencies-stale",
])
def test_cache_misses_every_changed_bound_input(candidate, field):
    c = candidate
    c.cache.remember(c.path, c.status, c.request, c.validated)
    status, request, session = copy.deepcopy(c.status), copy.deepcopy(c.request), "d" * 32
    if field == "source": status["source"]["currentSha256"] = "0" * 64
    elif field == "include": status["source"]["dependenciesDigest"] = "0" * 64
    elif field == "include-count": status["source"]["dependencyDocumentCount"] = 2
    elif field == "compiler": status["compilerBinding"]["bindingSha256"] = "0" * 64
    elif field == "compiler-config": status["compilerBinding"]["hops"][0]["toolId"] = "changed"
    elif field == "route": status["routePlan"]["planSha256"] = "0" * 64
    elif field == "entry": status["entryPoint"] = "other"
    elif field == "stage": status["stage"] = "vertex"
    elif field == "original": status["baseline"]["rawSha256"] = "0" * 64
    elif field == "resource": status["shader"]["resourceId"] = 78
    elif field == "capture": status["capture"]["captureId"] = "0" * 64
    elif field == "capture-path": status["capture"]["capturePath"] += ".other"
    elif field == "catalog": status["capture"]["catalogSha256"] = "0" * 64
    elif field == "candidate": status["latestCompiled"]["rawSha256"] = "0" * 64
    elif field == "revision": status["revision"] = status["latestCompiled"]["revision"] = 3
    elif field == "request": request["requestId"] = "0" * 64
    elif field == "request-params": request["params"]["candidate_path"] += ".other"
    elif field == "report": status["latestCompiled"]["abiGate"]["reportSha256"] = "0" * 64
    elif field == "baseline-abi": status["baseline"]["abiSha256"] = "0" * 64
    elif field == "candidate-abi": status["latestCompiled"]["abiGate"]["candidateAbiSha256"] = "0" * 64
    elif field == "profile": status["targetProfile"]["profile"] = "ps_6_1"
    elif field == "session": session = "e" * 32
    elif field == "manual": status["latestCompiled"]["validationScope"] = "manual-edit-unvalidated"
    elif field == "incompatible": status["abiValidationStatus"] = "incompatible"
    elif field == "dependencies-stale": status["sourceDependenciesCurrent"] = False
    assert c.cache.lookup(c.path, status, request, session) is None


def test_changed_session_performs_fresh_reflection_and_clears_old_cache(candidate):
    c = candidate
    c.cache.remember(c.path, c.status, c.request, c.validated)
    c.live["captureSessionId"] = "e" * 32
    c.ipc.return_value["captureSessionId"] = "e" * 32
    result = server._shader_candidate_validation_for_apply(c.path, 2, "9" * 64)
    assert result[3]["captureSessionId"] == "e" * 32
    assert "reusedCurrentSessionValidation" not in result[3]
    assert c.ipc.call_count == 1


def test_disk_compatible_gate_without_process_receipt_never_authorizes_reuse(candidate):
    c = candidate
    result = server._shader_candidate_validation_for_apply(c.path, 2, "9" * 64)
    assert "reusedCurrentSessionValidation" not in result[3]
    assert c.ipc.call_count == 1


@pytest.mark.parametrize("field", ["compilerBinding", "captureSessionId", "report", "validation"])
def test_missing_or_failed_evidence_not_cached(candidate, field):
    c = candidate
    if field == "compilerBinding": c.status.pop("compilerBinding")
    elif field == "captureSessionId": c.validated.pop("captureSessionId")
    elif field == "report": c.validated["abiReportSha256"] = "0" * 64
    elif field == "validation": c.validated["replayAbiValidated"] = False
    c.cache.remember(c.path, c.status, c.request, c.validated)
    assert not c.cache.contains(c.path)


@pytest.mark.parametrize("missing", ["compilerBinding", "dependenciesDigest", "catalogSha256"])
def test_legacy_missing_cache_metadata_keeps_the_full_validation_route(candidate, missing):
    c = candidate
    if missing == "compilerBinding": c.status.pop(missing)
    elif missing == "dependenciesDigest": c.status["source"].pop(missing)
    else: c.status["capture"].pop(missing)
    for _ in range(2):
        result = server._shader_candidate_validation_for_apply(c.path, 2, "9" * 64)
        assert result[3]["replayAbiValidated"]
        assert "reusedCurrentSessionValidation" not in result[3]
    assert c.ipc.call_count == 2
    assert not c.cache.contains(c.path)


def test_unicode_local_workspace_identity_does_not_need_ascii_alias(candidate):
    c = candidate
    local = Path(c.path) / "着色器 工作区"
    local.mkdir()
    c.cache.remember(str(local), c.status, c.request, c.validated)
    assert c.cache.lookup(str(local / "."), c.status, c.request, "d" * 32)


@pytest.mark.skipif(os.name != "nt", reason="Windows local filesystem spelling")
def test_windows_local_workspace_case_and_slashes_share_only_one_cache_entry(candidate):
    c = candidate
    c.cache.remember(c.path, c.status, c.request, c.validated)
    assert c.cache.lookup(c.path.upper().replace("\\", "/"), c.status, c.request, "d" * 32)


@pytest.mark.skipif(os.name == "nt", reason="POSIX case-sensitive local filesystem")
def test_linux_workspace_case_is_not_merged(candidate):
    c = candidate
    c.cache.remember(c.path + "/Shader", c.status, c.request, c.validated)
    assert not c.cache.lookup(c.path + "/shader", c.status, c.request, "d" * 32)


@pytest.mark.parametrize("capture_path", [
    "C:\\抓帧\\终末地\\t2.rdc", "\\\\server\\共享\\t2.rdc",
    "/storage/emulated/0/捕获/t2.rdc", "adb://device-123/storage/capture.rdc",
    "remote://linux-host/home/captures/t2.rdc",
])
def test_foreign_capture_and_source_path_evidence_is_opaque_and_exact(candidate, capture_path):
    c = candidate
    c.status["capture"]["capturePath"] = capture_path
    c.status["source"]["path"] = capture_path + "/source/着色器.hlsl"
    before = copy.deepcopy(c.status)
    c.cache.remember(c.path, c.status, c.request, c.validated)
    assert c.cache.lookup(c.path, c.status, c.request, "d" * 32)
    assert c.status == before
    changed = copy.deepcopy(c.status)
    changed["capture"]["capturePath"] += ".another"
    assert c.cache.lookup(c.path, changed, c.request, "d" * 32) is None


def test_cache_owns_copies_and_is_bounded(candidate):
    c = candidate
    cache = ShaderCandidateValidationCache(limit=2)
    for path in (c.path + "/1", c.path + "/2", c.path + "/3"):
        cache.remember(path, c.status, c.request, c.validated)
    assert not cache.contains(c.path + "/1")
    value = cache.lookup(c.path + "/3", c.status, c.request, "d" * 32)
    value["compatible"] = False
    c.validated["compatible"] = False
    assert cache.lookup(c.path + "/3", c.status, c.request, "d" * 32)["compatible"]
    cache.clear()
    assert not cache.contains(c.path + "/3")


def test_current_pair_still_enters_native_checked_apply_and_owner_guard(candidate, monkeypatch):
    c = candidate
    c.cache.remember(c.path, c.status, c.request, c.validated)
    monkeypatch.setattr(server, "_observe_current_shared_shader_candidate", lambda *a: None)
    anchor = {"eventId": 321, "originalResourceId": 77,
              "originalRawSha256": "4" * 64, "physicalAbiSha256": "c" * 64}
    monkeypatch.setattr(server, "_expected_shared_shader_anchor", lambda *a: anchor)
    monkeypatch.setattr(server, "_verified_restore_catalog_binding", lambda *a: {})
    guard = mock.Mock()
    monkeypatch.setattr(server, "_require_workspace_live_edit_quiescent", guard)
    c.ipc.side_effect = RuntimeError("checked native failure preserved")
    result = json.loads(server.apply_shader_workspace_shared_resource(
        c.path, 2, "9" * 64, expected_anchor_event_id=321))
    assert result["error"] == "checked native failure preserved"
    guard.assert_called_once()
    c.ipc.assert_called_once()
    assert c.ipc.call_args.args[0] == "apply_shader_candidate_shared_resource"
    assert c.ipc.call_args.args[1]["expected_capture_session_id"] == "d" * 32
