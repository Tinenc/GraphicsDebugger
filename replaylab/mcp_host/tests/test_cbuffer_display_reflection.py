"""Display names follow exact immutable resources, never the ABI apply gate."""
import hashlib
from types import SimpleNamespace

import pytest

from test_cbuffer_alias_identity import _wrapper


@pytest.mark.parametrize("api", ["D3D11", "D3D12", "Vulkan", "OpenGL", "OpenGLES"])
def test_manual_unvalidated_owner_works_away_from_anchor_without_replay(monkeypatch, api):
    bridge, params, unused, checks = _wrapper(monkeypatch, owned=True)
    bridge["_record"].update(abiValidationMode="manual-fast", physicalAbiSha256="", api=api)
    bridge["_pipeline"].selected = 999
    params["expected_candidate_sha256"] = bridge["_record"]["candidateRawSha256"]
    result = bridge["_get_shader_cbuffer_alias_identity"](params)
    assert result["activeDisplayShaderRawSha256"] == params["expected_candidate_sha256"]
    assert result["activeDisplayBlocks"] == []
    assert result["replayMutation"] is result["physicalAbiValidated"] is False
    assert result["eventIdentityVerified"] is False
    assert bridge["_reads"] == [27]
    assert bridge["_pipeline"].selected == 999 and len(checks) == 2


def test_uncatalogued_capture_is_bound_to_real_original_bytes_without_claiming_capture_hash(monkeypatch):
    bridge, params, _, _ = _wrapper(monkeypatch)
    bridge["_pipeline"].selected = 999
    params.pop("event_id")
    result = bridge["_get_shader_cbuffer_alias_identity"](params)
    assert result["captureId"] == ""
    assert result["captureIdentityBasis"] == "session-original-bytes"
    assert result["rawSha256"] == params["expected_original_sha256"]
    assert result["eventId"] == 0 and bridge["_reads"] == [17]


@pytest.mark.parametrize("field,value", [
    ("expected_original_sha256", "d" * 64), ("expected_capture_session_id", "d" * 32),
    ("expected_candidate_sha256", "d" * 64), ("stage", "vertex"),
    ("entry_point", "other"), ("expected_capture_id", "d" * 64)])
def test_manual_owner_mismatches_fail_before_any_native_read(monkeypatch, field, value):
    bridge, params, _, _ = _wrapper(monkeypatch, owned=True)
    params[field] = value
    with pytest.raises((RuntimeError, ValueError)):
        bridge["_get_shader_cbuffer_alias_identity"](params)
    assert bridge["_reads"] == []


def test_native_owner_must_match_even_when_ui_owner_and_cached_source_still_match(monkeypatch):
    bridge, params, _, _ = _wrapper(monkeypatch, owned=True)
    bridge["_controller"].target = 999
    with pytest.raises(RuntimeError, match="owner changed outside Bread"):
        bridge["_get_shader_cbuffer_alias_identity"](params)


@pytest.mark.parametrize("api,accepted", [("d3d11", True), ("d3d12", True), ("vulkan", False), ("opengl", False)])
def test_synthetic_dxbc_entry_can_read_exact_candidate_without_replaying_anchor(monkeypatch, api, accepted):
    bridge, params, _, _ = _wrapper(monkeypatch, owned=True)
    bridge["_pipeline"].selected = 999
    bridge["_shader_api_token"] = lambda ctrl: api
    bridge["_controller"].GetShaderEntryPoints = lambda resource: [SimpleNamespace(name="main", stage="fragment")]
    if accepted:
        assert bridge["_get_shader_cbuffer_alias_identity"](params)["activeDisplayShaderRawSha256"] == bridge["_record"]["candidateRawSha256"]
    else:
        with pytest.raises(RuntimeError, match="entry point/stage changed"):
            bridge["_get_shader_cbuffer_alias_identity"](params)


def test_native_owner_change_during_read_is_rejected(monkeypatch):
    bridge, params, _, _ = _wrapper(monkeypatch, owned=True)
    bridge["_pipeline"].selected = 999
    ctrl = bridge["_controller"]
    original = ctrl.GetShader
    def changed(*args):
        reflection = original(*args)
        ctrl.target = 999
        return reflection
    ctrl.GetShader = changed
    with pytest.raises(RuntimeError, match="owner changed outside Bread"):
        bridge["_get_shader_cbuffer_alias_identity"](params)


def debug_session(bridge, *, owned):
    reflection = SimpleNamespace(rawBytes=b"ephemeral debug", entryPoint="MainPS", constantBlocks=[])
    session = {"status": "active", "sessionId": "preview-one",
               "facts": {"scope": "shader-resource", "capture": bridge["_capture_session_id"],
                         "shaderId": "17", "baselineReflection": bridge["_original"]},
               "cached": {"target": 37, "reflection": reflection,
                          "rawSha256": hashlib.sha256(reflection.rawBytes).hexdigest()}}
    bridge["_shader_visual_debug_manager"] = SimpleNamespace(_session=session)
    bridge["_pipeline"].reflection = reflection
    bridge["_controller"].target = 37
    return session


@pytest.mark.parametrize("owned", [False, True])
def test_debug_identity_is_ephemeral_and_compare_stop_restore_persistent_identity(monkeypatch, owned):
    bridge, params, _, _ = _wrapper(monkeypatch, owned=owned)
    before = bridge["_get_shader_cbuffer_display_identity"](params)
    session = debug_session(bridge, owned=owned)
    result = bridge["_get_shader_cbuffer_alias_identity"](params)
    assert result["activeDisplayEphemeral"] is True
    assert result["activeDisplayShaderRawSha256"] == hashlib.sha256(b"ephemeral debug").hexdigest()
    assert result["activeReplacementShaderRawSha256"] == (bridge["_record"]["candidateRawSha256"] if owned else "")
    fresh = bridge["_get_shader_cbuffer_display_identity"](params)
    assert fresh["status"] == "current" and fresh["activeDisplayEphemeral"] is True
    # Selecting another Shader cannot reuse the underlying persistent entry as
    # if it proved which ephemeral target that other panel currently displays.
    bridge["_pipeline"].selected = 999
    assert bridge["_get_shader_cbuffer_display_identity"](params)["status"] == "refresh-required"
    assert bridge["_get_shader_cbuffer_alias_identity"](params)["activeDisplayEphemeral"] is True
    session["status"] = "comparing"
    bridge["_controller"].target = 27 if owned else 0
    bridge["_pipeline"].reflection = bridge["_original"]
    bridge["_pipeline"].selected = 17
    compared = bridge["_get_shader_cbuffer_alias_identity"](params)
    assert compared["activeDisplayEphemeral"] is False
    assert compared["activeDisplayShaderRawSha256"] == params["expected_original_sha256"]
    bridge["_shader_visual_debug_manager"]._session = None
    after = bridge["_get_shader_cbuffer_display_identity"](params)
    assert after["activeDisplayShaderRawSha256"] == before["activeDisplayShaderRawSha256"]
    assert after["activeDisplayEphemeral"] is False


def test_debug_native_ownership_and_retained_original_cannot_be_forged(monkeypatch):
    bridge, params, _, _ = _wrapper(monkeypatch)
    session = debug_session(bridge, owned=False)
    bridge["_controller"].target = 999
    with pytest.raises(RuntimeError, match="owner changed outside Bread"):
        bridge["_get_shader_cbuffer_alias_identity"](params)
    bridge["_controller"].target = 37
    session["facts"]["baselineReflection"] = SimpleNamespace(rawBytes=b"foreign")
    with pytest.raises(RuntimeError, match="captured-original Shader bytes"):
        bridge["_get_shader_cbuffer_alias_identity"](params)


def test_debug_session_change_during_read_does_not_return_stale_names(monkeypatch):
    bridge, params, _, _ = _wrapper(monkeypatch)
    debug_session(bridge, owned=False)
    original = bridge["_shader_constant_fields"]
    def changed(variables):
        bridge["_shader_visual_debug_manager"]._session = None
        return original(variables)
    bridge["_shader_constant_fields"] = changed
    with pytest.raises(RuntimeError, match="owner changed during"):
        bridge["_get_shader_cbuffer_alias_identity"](params)


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("endpoint", ["alias", "display"])
def test_published_ui_snapshot_can_expire_before_queued_replay_without_native_pointer_access(
        monkeypatch, owned, endpoint):
    bridge, params, _, _ = _wrapper(monkeypatch, owned=owned)
    lifetime = SimpleNamespace(alive=True, snapshots=0, queued=0)
    raw = b"verified replacement" if owned else b"captured original"
    block = SimpleNamespace(name="published-block", fixedBindNumber=0, fixedBindSetOrSpace=0,
                            byteSize=44, variables=[SimpleNamespace(name="published-field", byteOffset=0)])
    class BorrowedReflection:
        def __getattribute__(self, name):
            if name in ("rawBytes", "entryPoint", "constantBlocks"):
                assert lifetime.alive, "PublishedPipelineState was destroyed by an EID change"
                return {"rawBytes": raw, "entryPoint": "MainPS", "constantBlocks": [block]}[name]
            return object.__getattribute__(self, name)
    bridge["_pipeline"].reflection = BorrowedReflection()
    def primitives(value):
        if isinstance(value, dict):
            return all(isinstance(key, str) and primitives(item) for key, item in value.items())
        if isinstance(value, list):
            return all(primitives(item) for item in value)
        return value is None or type(value) in (str, bool, int, float)
    def ui(callback):
        result = callback()
        if isinstance(result, dict) and "rawSha256" in result:
            assert primitives(result)
            lifetime.snapshots += 1
            # Releasing this UI invocation lets EID publication destroy the old
            # cloned reflection before the queued replay callback can execute.
            lifetime.alive = False
            bridge["_pipeline"].selected = 999
            bridge["_pipeline"].reflection = SimpleNamespace(rawBytes=b"another-eid")
        return result
    bridge["_invoke_on_ui_sync"] = ui
    def queued(callback):
        assert not lifetime.alive
        lifetime.queued += 1
        return callback(bridge["_controller"])
    bridge["_replay"] = queued
    result = bridge["_get_shader_cbuffer_%s_identity" % endpoint](params)
    assert lifetime.snapshots == 1 and lifetime.queued == (1 if endpoint == "alias" else 0)
    assert result["activeDisplayShaderRawSha256"] == hashlib.sha256(raw).hexdigest()
    if endpoint == "alias":
        blocks = result["activeDisplayBlocks"] if owned else result["blocks"]
        assert blocks[0]["block"] == "published-block"
        assert blocks[0]["members"] == [{"name": "published-field", "offset": 0}]


def test_display_freshness_does_not_dereference_replay_owned_debug_reflection(monkeypatch):
    bridge, params, _, _ = _wrapper(monkeypatch)
    session = debug_session(bridge, owned=False)
    class ReplayOnlyReflection:
        def __getattribute__(self, name):
            raise AssertionError("Freshness must only consume the compiled target's copied SHA")
    session["cached"]["reflection"] = ReplayOnlyReflection()
    result = bridge["_get_shader_cbuffer_display_identity"](params)
    assert result["status"] == "current" and result["activeDisplayEphemeral"] is True
