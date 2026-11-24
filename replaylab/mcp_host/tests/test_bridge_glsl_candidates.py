"""GLSL is an exact source artifact through reflection, ownership and restore."""
import copy
import hashlib
import os
from types import SimpleNamespace as NS

import pytest

from tests import test_shader_restore_receipts as receipts
from tests.test_shader_numeric_validation import FakeBridge, packed, validation


ORIGINAL = b"#version 450\nlayout(location=0) out vec4 c; void main(){ c=vec4(1); }\n"
CANDIDATE = ORIGINAL.replace(b"vec4(1)", b"vec4(1.0)")


@pytest.fixture
def case():
    fixture = receipts.ShaderRestoreReceiptTests()
    fixture.setUp()
    fixture.original = ORIGINAL
    fixture.candidate.write_bytes(CANDIDATE)
    yield fixture
    fixture.doCleanups()


def make_gl(case, session="7" * 32):
    bridge, ctrl = case.bridge(session=session)
    bridge.rd.ShaderEncoding.GLSL = "GLSL"
    ctrl.api = "GraphicsAPI.OpenGL"
    ctrl.GetTargetShaderEncodings = lambda: ["GLSL"]
    original_get = ctrl.GetShader
    def get_shader(*args):
        value = original_get(*args)
        value.encoding = "GLSL"
        value.constantBlocks = []
        return value
    ctrl.GetShader = get_shader
    bridge._known_catalog_capture_ids = {"c" * 64: os.path.normcase(str(case.capture))}
    params = dict(case.params(), encoding="glsl", expected_profile="glsl")
    exact, stable = bridge._shader_restore_catalog_pairs(params)
    bridge._known_catalog_identity_pairs.add(exact)
    bridge._known_catalog_stable_pairs.add(stable)
    return bridge, ctrl, params


def test_reflection_preserves_source_identity_and_always_frees_target(case):
    bridge, ctrl, params = make_gl(case)
    result = bridge._reflect_shader_candidate(params)
    assert result["reflectionSucceeded"] and result["freeTargetResourceSucceeded"]
    assert result["encoding"] == "glsl"
    assert result["rawSha256"] == result["reflectedRawSha256"] == hashlib.sha256(CANDIDATE).hexdigest()
    assert ("build", CANDIDATE) in ctrl.calls
    assert ("free", 100) in ctrl.calls
    assert not ctrl.replacements


@pytest.mark.parametrize("change", ["encoding", "bytes"])
def test_glsl_reflection_mismatch_is_not_a_success_or_a_replacement(case, change):
    bridge, ctrl, params = make_gl(case)
    original_get = ctrl.GetShader
    def changed(*args):
        value = original_get(*args)
        if int(args[1]) != 77:
            if change == "encoding": value.encoding = "OpenGLSPIRV"
            else: value.rawBytes += b"\n"
        return value
    ctrl.GetShader = changed
    result = bridge._reflect_shader_candidate(params)
    assert not result["reflectionSucceeded"] and result["freeTargetResourceSucceeded"]
    with pytest.raises(RuntimeError, match="does not match candidate bytes"):
        bridge._apply_shader_candidate_shared_resource(params)
    assert not ctrl.replacements and not bridge._shared_shader_replacements
    assert ("free", 101) in ctrl.calls


@pytest.mark.parametrize("source", [b"void main(){}\x00", b"\xff", b" \n", b"x" * (4 * 1024 * 1024 + 1)], ids=["nul", "utf8", "empty", "size"])
def test_invalid_glsl_source_never_builds(case, source):
    bridge, ctrl, params = make_gl(case)
    case.candidate.write_bytes(source)
    params["expected_sha256"] = hashlib.sha256(source).hexdigest()
    with pytest.raises(ValueError):
        bridge._reflect_shader_candidate(params)
    assert not ctrl.calls


@pytest.mark.parametrize("api,entry", [("Vulkan", "main"), ("OpenGL", "Other")])
def test_glsl_cannot_cross_api_or_change_entry(case, api, entry):
    bridge, ctrl, params = make_gl(case)
    ctrl.api = api
    params["entry_point"] = entry
    with pytest.raises(RuntimeError):
        bridge._apply_shader_candidate_shared_resource(params)
    assert not any(call[0] == "build" for call in ctrl.calls)


def test_apply_idempotence_signed_restore_and_exact_rollback_keep_glsl_identity(case):
    bridge, ctrl, params = make_gl(case)
    applied = bridge._apply_shader_candidate_shared_resource(params)
    assert applied["configured"] and applied["encoding"] == "glsl"
    # Checked replacement already refreshed the selected anchor. Its post-apply
    # binding is still inspected, without another forced replay of that EID.
    assert applied["postApplyAnchorVerified"] is True
    assert [call for call in ctrl.calls if call[0] == "event"] == [("event", 321), ("event", 321)]
    owner = bridge._shared_shader_replacements[77]
    assert owner["capturedOriginalReflection"].rawBytes == ORIGINAL
    assert owner["candidateRawSha256"] == hashlib.sha256(CANDIDATE).hexdigest()
    again = bridge._apply_shader_candidate_shared_resource(params)
    assert again["replacementId"] == applied["replacementId"]
    assert len([call for call in ctrl.calls if call[0] == "build"]) == 1
    receipt = applied["restoreValidationReceipt"]
    assert receipt["payload"]["encoding"] == "glsl"
    assert receipt["payload"]["candidateRawSha256"] == hashlib.sha256(CANDIDATE).hexdigest()
    exact = dict(expected_capture_id="c" * 64, expected_capture_session_id="7" * 32,
                 workspace_id="b" * 32, expected_event_id=321, stage="fragment", entry_point="main")
    for request, field in (("original_resource_id", "originalResourceId"), ("original_sha256", "originalRawSha256"),
            ("replacement_id", "replacementId"), ("operation_id", "operationId"), ("revision", "revision"),
            ("anchor_event_id", "anchorEventId"), ("api", "api"), ("encoding", "encoding"),
            ("candidate_sha256", "candidateRawSha256"), ("physical_abi_sha256", "physicalAbiSha256"),
            ("shader_family_id", "shaderFamilyId"), ("target_resource_id", "targetResourceId")):
        exact["expected_" + request] = applied[field]
    restored = bridge._restore_shader_candidate_shared_resource_exact(exact)
    assert restored["state"] == "rolled-back" and restored["rollback"]["targetFreed"]
    assert not ctrl.replacements and not bridge._shared_shader_replacements
    assert ("free", 100) in ctrl.calls and ("free", 77) not in ctrl.calls

    # Same RDC, new replay session: signed source/ABI proof remains exact.
    warm, warm_ctrl, unused = make_gl(case, session="8" * 32)
    warm_params = dict(case.warm_params(receipt), encoding="glsl", expected_profile="glsl")
    result = warm._apply_shader_candidate_shared_resource(warm_params)
    assert result["configured"] and result["encoding"] == "glsl"
    assert result["freshPhysicalAbiValidationPerformed"]
    assert ("reflect", 100) in warm_ctrl.calls
    assert warm_ctrl.replacements == {77: 100}
    corrupt = copy.deepcopy(warm_params)
    corrupt["encoding"] = "opengl-spirv"
    with pytest.raises(RuntimeError, match="no longer matches"):
        warm._verified_shader_restore_request(corrupt)

    # A signed source receipt cannot authorize a different newly built ABI.
    stale, stale_ctrl, unused = make_gl(case, session="8" * 32)
    get_shader = stale_ctrl.GetShader
    def changed_abi(*args):
        value = get_shader(*args)
        # Fragment thread-group placeholders are not physical ABI. Add an
        # actual binding so this still exercises strict GLSL interface checks.
        if int(args[1]) != 77:
            value.samplers = [NS(name="extraSampler", fixedBindNumber=1,
                                 fixedBindSetOrSpace=0, bindArraySize=1)]
        return value
    stale_ctrl.GetShader = changed_abi
    with pytest.raises(RuntimeError, match="physical ABI differs"):
        stale._apply_shader_candidate_shared_resource(warm_params)
    assert not stale_ctrl.replacements and ("free", 100) in stale_ctrl.calls


def gl_numeric(existing=False):
    bridge = FakeBridge(existing=existing)
    bridge.rd.ShaderEncoding.GLSL = "GLSL"
    bridge.params.update(encoding="glsl", entry_point="main")
    bridge._shader_api_token = lambda ctrl: "opengl"
    blobs = {"baseline": ORIGINAL, "candidate": CANDIDATE}
    bridge._read_shader_candidate = lambda params: (None, blobs[params["candidate_path"]], hashlib.sha256(blobs[params["candidate_path"]]).hexdigest())
    bridge._validate_live_shader_event = lambda *args, **kw: {"reflection": NS(raw=ORIGINAL, encoding="GLSL", readWriteResources=[]), "pipelineResource": 3}
    bridge.ctrl.GetShader = lambda unused, target, entry: NS(raw=bridge.ctrl.targets[target], encoding="GLSL", readWriteResources=[])
    return bridge


def test_glsl_post_apply_failure_rolls_back_to_exact_previous_owner(case):
    bridge, ctrl, params = make_gl(case)
    applied = bridge._apply_shader_candidate_shared_resource(params)
    owner = bridge._shared_shader_replacements[77]
    revised = CANDIDATE + b"// revision two\n"
    case.candidate.write_bytes(revised)
    params.update(revision=2, expected_sha256=hashlib.sha256(revised).hexdigest())
    validate = bridge._validate_live_shader_event
    def fail_post_apply(*args, **kwargs):
        result = validate(*args, **kwargs)
        if ctrl.replacements.get(77) == 101:
            raise RuntimeError("injected post-apply binding failure")
        return result
    bridge._validate_live_shader_event = fail_post_apply
    with pytest.raises(RuntimeError, match="post-apply binding"):
        bridge._apply_shader_candidate_shared_resource(params)
    assert ctrl.replacements == {77: 100}
    assert int(bridge._ctx.GetResourceReplacement(receipts.ResourceId(77))) == 100
    assert bridge._shared_shader_replacements[77] is owner
    assert owner["replacementId"] == applied["replacementId"]
    assert ("free", 101) in ctrl.calls and ("free", 100) not in ctrl.calls


@pytest.mark.parametrize("failure", ["none", "abi", "output", "readback"])
def test_glsl_numeric_comparison_keeps_checks_and_restores_prior_owner(failure):
    bridge = gl_numeric(existing=True)
    if failure == "abi": bridge.compare_compatible = False
    if failure == "output": bridge.ctrl.output_values[202] = packed("f", 2)
    if failure == "readback": bridge.ctrl.observe_failure = 1
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] is (failure == "none")
    assert result["replayRestored"]
    if failure == "abi": assert "physical ABI mismatch" in result["reason"]
    if failure == "readback": assert "unsupported output" in result["reason"]
    assert bridge.ctrl.mapping == 999 and bridge.ctrl.event_id == 17
    assert not bridge.ctrl.targets
