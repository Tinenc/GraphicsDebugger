"""Replay adapter contracts using typed native-like IDs, without a live capture."""
import hashlib
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


PATH = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "shader_visual_debug.py"
SPEC = importlib.util.spec_from_file_location("visual_debug_driver_tests", PATH)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class ResourceId:
    def __init__(self, number=0):
        self.number = number

    def __eq__(self, other):
        return isinstance(other, ResourceId) and self.number == other.number

    def __repr__(self):
        return "ResourceId(%d)" % self.number


def override(active=False, original=101, replacement=0, target=0xffffffff, independent=False):
    return SimpleNamespace(active=active, eventId=13, stage="Pixel",
                           expectedOriginal=ResourceId(original),
                           replacement=ResourceId(replacement), outputTarget=target,
                           independentFloatOutput=independent)


class Controller:
    def __init__(self):
        self.event = 13
        self.state = override()
        self.calls = []
        self.error = ""
        self.generation = 9
        self.actions = [SimpleNamespace(eventId=13, flags=2, children=[])]
        self.reflection = SimpleNamespace(
            entryPoint="exported_main", encoding="SPIRV", raw=b"captured-shader",
            outputSignature=[], constantBlocks=[], debugInfo=SimpleNamespace(
                encoding="HLSL", entrySourceName="PSMain", editBaseFile=1,
                entryLocation=SimpleNamespace(fileIndex=0), compileFlags=SimpleNamespace(flags=[]),
                files=[SimpleNamespace(filename="include/common.hlsli", contents="float value;\n"),
                       SimpleNamespace(filename="shaders/pixel.hlsl", contents='#include "common.hlsli"\nfloat4 PSMain() : SV_Target { return value; }\n')]))
        fmt = SimpleNamespace(compCount=4, compType="Float", Name=lambda: "RGBA32_FLOAT")
        self.texture = SimpleNamespace(resourceId=ResourceId(201), format=fmt, msSamp=1)
        self.pipeline = SimpleNamespace(
            GetShader=lambda stage: ResourceId(101),
            GetShaderReflection=lambda stage: self.reflection,
            GetOutputTargets=lambda: [SimpleNamespace(resource=ResourceId(201), format=fmt)])

    def GetCurrentEventID(self):
        return self.event

    def GetRootActions(self):
        return self.actions

    def GetPipelineState(self):
        return self.pipeline

    def GetTextures(self):
        return [self.texture]

    def GetVulkanPipelineState(self):
        return SimpleNamespace(currentPass=SimpleNamespace(renderpass=SimpleNamespace(colorAttachments=[0])))

    def GetShaderReplacementGeneration(self, event):
        return self.generation

    def GetEventShaderOverride(self, event, stage):
        return self.state

    def SetFrameEvent(self, event, force):
        self.calls.append(("event", event, force))
        self.event = event

    def SetEventShaderOverride(self, event, stage, original, replacement, target, independent=False):
        assert isinstance(original, ResourceId)
        assert isinstance(replacement, ResourceId)
        assert self.event == event
        self.calls.append(("set", event, original, replacement, target))
        if not self.error:
            self.state = override(True, original.number, replacement.number, target, independent)
            self.generation += 1
        return self.error

    def ClearEventShaderOverride(self, event, stage):
        self.calls.append(("clear", event, stage))
        self.state = override()
        self.generation += 1


@pytest.fixture
def adapter(tmp_path):
    ctrl = Controller()
    bridge = SimpleNamespace(
        rd=SimpleNamespace(ShaderStage=SimpleNamespace(Pixel="Pixel"),
                           ActionFlags=SimpleNamespace(Drawcall=2, MeshDispatch=8),
                           ShaderReflection=copy.deepcopy, ConstantBlock=copy.deepcopy,
                           ShaderCompileFlags=lambda: SimpleNamespace(flags=[])),
        _capture_session_id="capture-a", _active_capture_token=(None, str(tmp_path / "frame.rdc")),
        _shared_shader_replacements={}, _replay=lambda callback: callback(ctrl),
        _resource_id_int=lambda value: value.number if isinstance(value, ResourceId) else 0,
        _shader_api_token=lambda controller: "vulkan",
        _shader_raw_bytes=lambda reflection: getattr(reflection, "raw", getattr(reflection, "rawBytes", b"")),
        _normalise_stage_name=lambda value: {"PS": "fragment"}.get(value, value),
        _compiled_shader_interface=lambda reflection: {"entryPoint": reflection.entryPoint},
        _physical_shader_interface=lambda interface: interface,
        _require_healthy_replay=lambda controller: None,
        _notify_shader_replay_changed=lambda: None)
    driver = mod.ReplayDriver.__new__(mod.ReplayDriver)
    driver.bridge = bridge
    driver.compiler = SimpleNamespace(identity=lambda *args: "fixed-compiler")
    driver._has_preprocessor_options = lambda values: False
    params = {"captureBinding": {"captureSessionId": "capture-a", "capturePath": bridge._active_capture_token[1]},
              "eventId": 13, "stage": "fragment", "language": "hlsl", "shaderId": "101", "scope": "event"}
    return driver, bridge, ctrl, params


def test_capture_source_uses_source_entry_and_preferred_edit_file(adapter):
    driver, _, _, params = adapter
    source = driver.capture_source(params)
    assert source["entryPoint"] == "PSMain"
    assert source["rootName"] == "1_pixel.hlsl"
    assert source["language"] == "hlsl"
    assert '#include "0_common.hlsli"' in source["text"]
    assert source["shaderRawSha256"] == hashlib.sha256(b"captured-shader").hexdigest()


def test_capture_source_resolves_relative_include_before_ambiguous_leaf(adapter):
    driver, _, ctrl, params = adapter
    ctrl.reflection.debugInfo.files = [
        SimpleNamespace(filename="a/common.hlsli", contents="float localValue;"),
        SimpleNamespace(filename="b/common.hlsli", contents="float otherValue;"),
        SimpleNamespace(filename="a/sub/pixel.hlsl", contents='#include "../common.hlsli"\nfloat4 PSMain() : SV_Target {return localValue;}')]
    ctrl.reflection.debugInfo.editBaseFile = 2
    source = driver.capture_source(params)
    assert '#include "0_common.hlsli"' in source["text"]


def test_capture_source_unknown_file_extension_preserves_captured_language(adapter, tmp_path):
    driver, _, _, params = adapter
    source_file = tmp_path / "pixel.txt"
    source_file.write_text("float4 PSMain() : SV_Target {return 1;}")
    source = driver.capture_source(dict(params, sourceFile=str(source_file)))
    assert source["language"] == "hlsl"


def test_vulkan_resolve_attachment_is_not_a_fragment_output_slot(adapter):
    driver, _, ctrl, params = adapter
    descriptor = ctrl.pipeline.GetOutputTargets()[0]
    ctrl.pipeline.GetOutputTargets = lambda: [descriptor, descriptor]
    facts = driver.inspect(params)
    assert [target["index"] for target in facts["targets"]] == [0]


@pytest.mark.parametrize("flags,children,accepted", [(2, [], True), (8, [], True),
    (4, [], False), (0x40, [], False), (2, [SimpleNamespace(eventId=14, flags=2, children=[])], False)])
def test_preview_requires_leaf_graphics_action(adapter, flags, children, accepted):
    driver, _, ctrl, params = adapter
    ctrl.actions = [SimpleNamespace(eventId=13, flags=flags, children=children)]
    if accepted:
        assert driver.inspect(params)["eventId"] == 13
    else:
        with pytest.raises(mod.DebugError) as error:
            driver.inspect(params)
        assert error.value.reason == "not-draw"


def test_compile_identity_ignores_attachment_resource_but_preserves_format(adapter):
    driver, _, ctrl, params = adapter
    first = driver.inspect(params)
    ctrl.texture.resourceId = ResourceId(202)
    ctrl.pipeline.GetOutputTargets = lambda: [SimpleNamespace(resource=ctrl.texture.resourceId,
                                                             format=ctrl.texture.format)]
    second = driver.inspect(params)
    assert first["compileIdentity"] == second["compileIdentity"]
    assert first["targets"][0]["resourceId"] != second["targets"][0]["resourceId"]
    ctrl.texture.format.compCount = 2
    assert driver.inspect(params)["compileIdentity"] != second["compileIdentity"]


def test_capture_source_falls_back_to_entry_location_then_first_file(adapter):
    driver, _, ctrl, params = adapter
    ctrl.reflection.debugInfo.editBaseFile = -1
    assert driver.capture_source(params)["rootName"] == "0_common.hlsli"
    ctrl.reflection.debugInfo.entryLocation.fileIndex = 999
    assert driver.capture_source(params)["rootName"] == "0_common.hlsli"


def test_capture_source_unknown_encoding_uses_selected_extension(adapter):
    driver, _, ctrl, params = adapter
    ctrl.reflection.debugInfo.encoding = "Unknown"
    assert driver.capture_source(params)["language"] == "hlsl"
    ctrl.reflection.debugInfo.encoding = "GLSL"
    assert driver.capture_source(params)["language"] == "glsl"


def test_explicit_source_file_remains_local_and_can_override_entry(adapter, tmp_path):
    driver, _, _, params = adapter
    path = tmp_path / "chosen.frag"
    path.write_text("void main() {}", encoding="utf-8")
    source = driver.capture_source(dict(params, sourceFile=str(path), entryPoint="main"))
    assert source["sourcePath"] == mod._path(str(path))
    assert source["includeRoot"] == mod._path(str(tmp_path))
    assert source["sourceOrigin"] == "selected-file"
    assert source["entryPoint"] == "main" and source["language"] == "glsl"
    assert "files" not in source


def test_capture_source_rejects_event_or_capture_drift(adapter):
    driver, bridge, ctrl, params = adapter
    ctrl.event = 14
    with pytest.raises(mod.DebugError, match="current Draw") as error:
        driver.capture_source(params)
    assert error.value.reason == "event-changed"
    bridge._capture_session_id = "capture-b"
    with pytest.raises(mod.DebugError) as error:
        driver.capture_source(params)
    assert error.value.reason == "capture-changed"


def test_inspect_accepts_matching_hash_and_preserves_profile_object_value(adapter):
    driver, _, _, params = adapter
    facts = driver.inspect(dict(params, targetProfile={"profile": "ps_6_0"},
                                expectedOriginalSha256=hashlib.sha256(b"captured-shader").hexdigest()))
    assert facts["profile"] == "ps_6_0"
    assert facts["targets"][0]["components"] == 4
    assert isinstance(facts["restoreState"]["replacement"], ResourceId)
    assert facts["mutationGeneration"] == 9


def test_inspect_hash_lineage_requires_matching_capture_entry_and_bytes(adapter):
    driver, bridge, ctrl, params = adapter
    params["expectedOriginalSha256"] = "original-hash"
    owner = {"expectedOriginalSha256": "original-hash", "candidateRawSha256": hashlib.sha256(ctrl.reflection.raw).hexdigest(),
             "captureSessionId": "capture-a", "entryPoint": ctrl.reflection.entryPoint}
    bridge._shared_shader_replacements[101] = owner
    assert driver.inspect(params)["shaderId"] == "101"
    for field in ("captureSessionId", "entryPoint", "candidateRawSha256", "expectedOriginalSha256"):
        saved = owner[field]
        owner[field] = "different"
        with pytest.raises(mod.DebugError) as error:
            driver.inspect(params)
        assert error.value.reason == "shader-changed"
        owner[field] = saved


def session_for(driver, params, active_prior=False):
    facts = driver.inspect(params)
    prior = mod.ReplayDriver._state(override(active_prior, replacement=301 if active_prior else 0, target=2 if active_prior else 0xffffffff))
    return {"facts": facts, "restoreState": prior, "status": "active",
            "cached": {"target": ResourceId(401)}, "target": {"index": 0}}


def test_restore_prior_override_keeps_typed_ids_and_current_user_event(adapter):
    driver, _, ctrl, params = adapter
    session = session_for(driver, params, True)
    ctrl.state = override(True, replacement=401, target=0)
    ctrl.event = 27
    driver.restore(session)
    assert ctrl.event == 27
    assert ctrl.calls == [("event", 13, True), ("set", 13, ResourceId(101), ResourceId(301), 2), ("event", 27, True)]
    assert ctrl.state.replacement == ResourceId(301)


def test_failed_restore_preserves_preview_and_restores_user_event(adapter):
    driver, _, ctrl, params = adapter
    session = session_for(driver, params, True)
    ctrl.state = override(True, replacement=401, target=0)
    ctrl.event, ctrl.error = 27, "native restore failed"
    with pytest.raises(mod.DebugError) as error:
        driver.restore(session)
    assert error.value.reason == "restore-failed"
    assert ctrl.event == 27 and ctrl.state.replacement == ResourceId(401)


def test_foreign_override_is_not_cleared_or_replaced(adapter):
    driver, _, ctrl, params = adapter
    session = session_for(driver, params)
    ctrl.state = override(True, replacement=999, target=0)
    with pytest.raises(mod.DebugError) as error:
        driver.restore(session)
    assert error.value.reason == "replacement-changed"
    assert ctrl.calls == [] and ctrl.state.replacement == ResourceId(999)


def test_capture_changed_while_comparing_does_not_touch_new_controller(adapter):
    driver, bridge, ctrl, params = adapter
    session = session_for(driver, params)
    session["status"] = "comparing"
    bridge._capture_session_id = "capture-b"
    driver.restore(session)
    assert ctrl.calls == []
    with pytest.raises(mod.DebugError) as error:
        driver.apply(session, session, lambda: True)
    assert error.value.reason == "capture-changed" and ctrl.calls == []


def test_apply_rejects_mutation_during_compile_and_cancel_before_commit(adapter):
    driver, _, ctrl, params = adapter
    session = session_for(driver, params)
    ctrl.generation += 1
    with pytest.raises(mod.DebugError) as error:
        driver.apply(session, None, lambda: True)
    assert error.value.reason == "replacement-changed"
    with pytest.raises(mod.DebugError) as error:
        driver.apply(session, None, lambda: False)
    assert error.value.reason == "superseded" and ctrl.calls == []


def test_viewer_refresh_error_does_not_reclassify_committed_apply(adapter):
    driver, bridge, ctrl, params = adapter
    session = session_for(driver, params)
    def destroyed_viewer():
        raise RuntimeError("viewer destroyed")
    bridge._notify_shader_replay_changed = destroyed_viewer
    driver.apply(session, None, lambda: True)
    assert ctrl.state.replacement == ResourceId(401)


def test_float_preview_receipt_and_exact_prior_float_override_restore(adapter):
    driver, _, ctrl, params = adapter
    ctrl.state = override(True, replacement=301, target=2, independent=True)
    session = session_for(driver, params)
    session["restoreState"] = driver._state(ctrl.state)
    session["independentFloatOutput"] = True
    preview = SimpleNamespace(resourceId=ResourceId(999), width=32, height=32,
                              format=SimpleNamespace(compCount=4, compByteWidth=4, compType="Float"))
    ctrl.GetEventShaderPreviewTexture = lambda event, stage: preview
    driver.apply(session, None, lambda: True)
    assert session["previewTexture"] is preview
    assert ctrl.state.independentFloatOutput and driver._owned(ctrl, session)
    driver.restore(session)
    assert ctrl.state.replacement == ResourceId(301)
    assert ctrl.state.independentFloatOutput and ctrl.state.outputTarget == 2


@pytest.mark.parametrize("getter_raises", [False, True])
def test_float_preview_missing_texture_rolls_back_committed_mutation(adapter, getter_raises):
    driver, _, ctrl, params = adapter
    ctrl.state = override(True, replacement=301, target=2, independent=True)
    session = session_for(driver, params)
    session["restoreState"] = driver._state(ctrl.state)
    session["independentFloatOutput"] = True
    def get_preview(event, stage):
        if getter_raises:
            raise RuntimeError("lost remote response")
        return SimpleNamespace(resourceId=ResourceId())
    ctrl.GetEventShaderPreviewTexture = get_preview
    with pytest.raises((RuntimeError, mod.DebugError)):
        driver.apply(session, None, lambda: True)
    assert ctrl.state.replacement == ResourceId(301)
    assert ctrl.state.independentFloatOutput and ctrl.state.outputTarget == 2


@pytest.mark.parametrize("prior_active", [False, True])
@pytest.mark.parametrize("getter_raises", [False, True])
def test_float_preview_failed_rollback_reports_committed_ownership_for_recovery(adapter, prior_active, getter_raises):
    driver, _, ctrl, params = adapter
    ctrl.state = override(prior_active, replacement=301 if prior_active else 0,
                          target=2 if prior_active else 0xffffffff, independent=prior_active)
    session = session_for(driver, params)
    session["restoreState"] = driver._state(ctrl.state)
    session["independentFloatOutput"] = True
    real_clear = ctrl.ClearEventShaderOverride
    def failed_clear(event, stage):
        raise RuntimeError("clear response unavailable")
    def get_preview(event, stage):
        ctrl.error = "prior override restore failed"
        ctrl.ClearEventShaderOverride = failed_clear
        if getter_raises:
            raise RuntimeError("texture descriptor reply lost")
        return SimpleNamespace(resourceId=ResourceId())
    ctrl.GetEventShaderPreviewTexture = get_preview
    with pytest.raises(mod.DebugError) as error:
        driver.apply(session, None, lambda: True)
    assert error.value.reason == "restore-failed" and error.value.preview_committed
    assert ctrl.state.replacement == ResourceId(401) and ctrl.state.independentFloatOutput
    ctrl.error = ""
    ctrl.ClearEventShaderOverride = real_clear
    driver.restore(session)
    assert ctrl.state.active == prior_active
    assert ctrl.state.replacement == ResourceId(301 if prior_active else 0)


def test_float_preview_getter_foreign_mutation_is_not_rolled_back(adapter):
    driver, _, ctrl, params = adapter
    session = session_for(driver, params)
    session["independentFloatOutput"] = True
    def get_preview(event, stage):
        ctrl.state = override(True, replacement=999, target=2)
        raise RuntimeError("descriptor unavailable after foreign edit")
    ctrl.GetEventShaderPreviewTexture = get_preview
    with pytest.raises(mod.DebugError) as error:
        driver.apply(session, None, lambda: True)
    assert error.value.reason == "replacement-changed"
    assert not getattr(error.value, "preview_committed", False)
    assert ctrl.state.replacement == ResourceId(999)
    assert [call[0] for call in ctrl.calls] == ["set"]


@pytest.mark.parametrize("intermediate_owned", [False, True])
def test_recovery_reconciles_rollback_that_committed_before_response_was_lost(adapter, intermediate_owned):
    driver, _, ctrl, params = adapter
    session = session_for(driver, params)
    session["independentFloatOutput"] = True
    session["recoveryRequired"] = True
    if intermediate_owned:
        # Switching from an already-owned preview may have rolled back to that
        # previous preview rather than to the user's original shader.
        ctrl.state = override(True, replacement=351, target=2, independent=True)
        session["recoveryRollbackState"] = driver._state(ctrl.state)
    else:
        ctrl.state = override()
        session["recoveryRollbackState"] = driver._state(ctrl.state)
    driver.restore(session)
    assert not ctrl.state.active
    assert [call[0] for call in ctrl.calls] == (["clear"] if intermediate_owned else [])


@pytest.mark.parametrize("independent", [False, True])
def test_post_apply_remote_health_failure_keeps_committed_candidate_recoverable(adapter, independent):
    driver, bridge, ctrl, params = adapter
    session = session_for(driver, params)
    session["independentFloatOutput"] = independent
    healthy = [True]
    def health(controller):
        if not healthy[0]:
            raise RuntimeError("remote connection lost")
    bridge._require_healthy_replay = health
    native_set = ctrl.SetEventShaderOverride
    def commit_then_lose_transport(*args):
        result = native_set(*args)
        healthy[0] = False
        return result
    ctrl.SetEventShaderOverride = commit_then_lose_transport
    with pytest.raises(mod.DebugError) as error:
        driver.apply(session, None, lambda: True)
    assert error.value.preview_committed and error.value.reason == "restore-failed"
    assert ctrl.state.replacement == ResourceId(401)
    assert [call[0] for call in ctrl.calls] == ["set"]


@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("locally_committed", [False, True])
def test_native_error_with_unhealthy_replay_retains_possible_commit(adapter, independent, locally_committed):
    driver, bridge, ctrl, params = adapter
    ctrl.state = override(True, replacement=301, target=2, independent=True)
    session = session_for(driver, params)
    session["restoreState"] = driver._state(ctrl.state)
    session["independentFloatOutput"] = independent
    before = driver._state(ctrl.state)
    healthy = [True]
    def health(controller):
        if not healthy[0]:
            raise RuntimeError("remote response lost after registration")
    bridge._require_healthy_replay = health
    native_set = ctrl.SetEventShaderOverride
    def failed_reply(*args):
        # The native controller can either have registered its local state or
        # lost the remote reply before doing so. Neither proves remote state.
        ctrl.error = "" if locally_committed else "lost acknowledgement"
        native_set(*args)
        healthy[0] = False
        return "RemoteServerConnectionLost"
    ctrl.SetEventShaderOverride = failed_reply
    with pytest.raises(mod.DebugError) as error:
        driver.apply(session, None, lambda: True)
    assert error.value.reason == "restore-failed" and error.value.preview_committed
    assert error.value.rollback_state == before
    assert "RemoteServerConnectionLost" in str(error.value)
    assert ctrl.state.replacement == ResourceId(401 if locally_committed else 301)
    # No unconfirmed rollback or descriptor access follows the fatal reply.
    assert [call[0] for call in ctrl.calls] == ["set"]


@pytest.mark.parametrize("independent", [False, True])
def test_healthy_native_semantic_error_does_not_claim_preview_ownership(adapter, independent):
    driver, _, ctrl, params = adapter
    session = session_for(driver, params)
    session["independentFloatOutput"] = independent
    before = driver._state(ctrl.state)
    ctrl.error = "The selected output has no color attachment"
    with pytest.raises(mod.DebugError) as error:
        driver.apply(session, None, lambda: True)
    assert error.value.reason == "apply-failed"
    assert not getattr(error.value, "preview_committed", False)
    assert driver._state(ctrl.state) == before
    assert [call[0] for call in ctrl.calls] == ["set"]


def test_unhealthy_replay_rejects_apply_before_any_mutation(adapter):
    driver, bridge, ctrl, params = adapter
    session = session_for(driver, params)
    def health(controller):
        raise RuntimeError("remote connection lost")
    bridge._require_healthy_replay = health
    with pytest.raises(RuntimeError, match="remote connection lost"):
        driver.apply(session, None, lambda: True)
    assert ctrl.calls == []


def test_void_remote_clear_failure_cannot_certify_local_state_as_restored(adapter):
    driver, bridge, ctrl, params = adapter
    session = session_for(driver, params)
    ctrl.state = override(True, replacement=401, target=0)
    healthy = [True]
    def health(controller):
        if not healthy[0]:
            raise RuntimeError("remote clear response lost")
    bridge._require_healthy_replay = health
    native_clear = ctrl.ClearEventShaderOverride
    def local_clear_then_remote_failure(*args):
        native_clear(*args)
        healthy[0] = False
    ctrl.ClearEventShaderOverride = local_clear_then_remote_failure
    with pytest.raises(RuntimeError, match="remote clear response lost"):
        driver.restore(session)
    assert session["recoveryRequired"] and not ctrl.state.active
    calls = list(ctrl.calls)
    # Even though local bookkeeping now equals the original state, the retry
    # cannot claim restoration while remote replay health remains unavailable.
    with pytest.raises(RuntimeError, match="remote clear response lost"):
        driver.restore(session)
    assert ctrl.calls == calls


def test_descriptor_rollback_checks_void_clear_remote_confirmation(adapter):
    driver, bridge, ctrl, params = adapter
    session = session_for(driver, params)
    session["independentFloatOutput"] = True
    ctrl.GetEventShaderPreviewTexture = lambda event, stage: SimpleNamespace(resourceId=ResourceId())
    healthy = [True]
    def health(controller):
        if not healthy[0]:
            raise RuntimeError("remote rollback reply lost")
    bridge._require_healthy_replay = health
    native_clear = ctrl.ClearEventShaderOverride
    def local_clear_then_remote_failure(*args):
        native_clear(*args)
        healthy[0] = False
    ctrl.ClearEventShaderOverride = local_clear_then_remote_failure
    with pytest.raises(mod.DebugError) as error:
        driver.apply(session, None, lambda: True)
    assert error.value.preview_committed and error.value.reason == "restore-failed"
    assert not ctrl.state.active


def test_foreign_change_to_preview_mode_is_an_ownership_change(adapter):
    driver, _, ctrl, params = adapter
    session = session_for(driver, params)
    ctrl.state = override(True, replacement=401, target=0, independent=True)
    with pytest.raises(mod.DebugError) as error:
        driver.restore(session)
    assert error.value.reason == "replacement-changed"
    assert not ctrl.calls


def test_inspect_active_owned_preview_keeps_original_baseline_and_cache_identity(adapter):
    driver, _, ctrl, params = adapter
    params["expectedOriginalSha256"] = hashlib.sha256(ctrl.reflection.raw).hexdigest()
    session = session_for(driver, params)
    initial = session["facts"]
    ctrl.state = override(True, replacement=401, target=0)
    ctrl.reflection.raw = b"instrumented-preview"
    ctrl.reflection.entryPoint = "debug_wrapper"
    ctrl.reflection.debugInfo.compileFlags.flags = [SimpleNamespace(name="unexpected", value="replacement")]
    facts = driver.inspect(params, previous=session)
    assert facts["compileIdentity"] == initial["compileIdentity"]
    assert facts["baselineInterface"] == initial["baselineInterface"]
    assert facts["baselineEntryPoint"] == "exported_main"


def test_inspect_previous_preview_requires_ownership_and_reenumerates_targets(adapter):
    driver, _, ctrl, params = adapter
    session = session_for(driver, params)
    ctrl.state = override(True, replacement=401, target=0)
    ctrl.texture.format.compCount = 2
    assert driver.inspect(params, previous=session)["targets"][0]["components"] == 2
    ctrl.state = override(True, replacement=999, target=0)
    with pytest.raises(mod.DebugError) as error:
        driver.inspect(params, previous=session)
    assert error.value.reason == "replacement-changed"


def test_inspect_different_event_does_not_reuse_previous_baseline(adapter):
    driver, _, ctrl, params = adapter
    ctrl.actions.append(SimpleNamespace(eventId=29, flags=2, children=[]))
    session = session_for(driver, params)
    ctrl.event = 29
    ctrl.reflection.raw = b"next-draw-shader"
    facts = driver.inspect(dict(params, eventId=29), previous=session)
    assert facts["compileIdentity"]["baselineSha256"] == hashlib.sha256(b"next-draw-shader").hexdigest()
