from __future__ import annotations

import hashlib
import enum
import importlib.util
from pathlib import Path
import struct
from types import SimpleNamespace as NS
from unittest import mock

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "shader_validation.py"
spec = importlib.util.spec_from_file_location("replaylab_shader_numeric_validation", MODULE_PATH)
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)
storage_spec = importlib.util.spec_from_file_location("bread_storage_validation", MODULE_PATH.with_name("shader_storage_validation.py"))
storage_validation = importlib.util.module_from_spec(storage_spec)
storage_spec.loader.exec_module(storage_validation)
validation._storage_module = lambda: storage_validation


def descriptor(kind="Float", width=4, count=1, pixels=1, output_kind="color", format_type="Regular"):
    return {"componentType": kind, "componentByteWidth": width,
            "componentCount": count, "width": pixels, "height": 1,
            "kind": output_kind, "formatType": format_type}


def packed(fmt, *values):
    return struct.pack("<" + fmt * len(values), *values)


def native_blob(label):
    payload = label.encode("ascii")
    return (b"DXBC" + b"\0" * 16 + struct.pack("<III", 1, 44 + len(payload), 1)
            + struct.pack("<I", 36) + b"SHDR" + struct.pack("<I", len(payload)) + payload)


def test_half_target_allows_one_unit_scale_storage_quantum_but_not_larger_changes():
    desc = descriptor("Float", width=2)
    assert validation.compare_bytes(packed("e", 0), packed("e", 0.0009), desc)["accepted"]
    assert not validation.compare_bytes(packed("e", 0), packed("e", 0.002), desc)["accepted"]
    assert not validation.compare_bytes(packed("f", 0), packed("f", 0.0009), descriptor())["accepted"]


@pytest.mark.parametrize("kind,format_type", [
    ("Float", "Regular"), ("CompType.Float", "ResourceFormatType.Regular"),
])
def test_float_tolerance_accepts_rounding_but_rejects_any_bad_component(kind, format_type):
    desc = descriptor(kind, count=4, format_type=format_type)
    before = packed("f", 1, 10, 0, -100)
    rounded = packed("f", 1.0005, 10.005, 0.00005, -100.05)
    metrics = validation.compare_bytes(before, rounded, desc)
    assert metrics["accepted"] and metrics["comparedComponents"] == 4
    assert not metrics["byteExact"]
    wrong = packed("f", 1.0005, 10.005, 0.01, -100.05)
    metrics = validation.compare_bytes(before, wrong, desc)
    assert not metrics["accepted"] and metrics["failedComponents"] == 1


@pytest.mark.parametrize("kind,fmt,width", [
    ("UNorm", "B", 1), ("CompType.UNormSRGB", "B", 1),
    ("UNorm", "H", 2), ("SNorm", "h", 2),
])
def test_normalized_tolerance_is_exactly_one_stored_quantization_step(kind, fmt, width):
    desc = descriptor(kind, width=width)
    assert validation.compare_bytes(packed(fmt, 40), packed(fmt, 41), desc)["accepted"]
    assert not validation.compare_bytes(packed(fmt, 40), packed(fmt, 42), desc)["accepted"]


def test_snorm_negative_endpoint_aliases_are_equivalent():
    assert validation.compare_bytes(packed("b", -128), packed("b", -127), descriptor("SNorm", width=1))["accepted"]


@pytest.mark.parametrize("kind,fmt,maximum", [("UInt", "I", 0xFFFFFFFF), ("SInt", "i", 0x7FFFFFFF)])
def test_integer_outputs_require_exact_values_even_near_largest_integer(kind, fmt, maximum):
    metrics = validation.compare_bytes(packed(fmt, maximum), packed(fmt, maximum - 1), descriptor(kind))
    assert not metrics["accepted"] and metrics["maximumAbsoluteError"] == 1


def test_depth_uses_absolute_tolerance_without_relative_slack():
    desc = descriptor("Depth", output_kind="depth")
    assert validation.compare_bytes(packed("f", 0.5), packed("f", 0.5000005), desc)["accepted"]
    assert not validation.compare_bytes(packed("f", 0.5), packed("f", 0.50001), desc)["accepted"]


@pytest.mark.parametrize("value", [0, 32767, 65534])
def test_d16_unorm_depth_keeps_strict_depth_tolerance(value):
    desc = descriptor("UNorm", width=2, output_kind="depth")
    raw = packed("H", value)
    assert validation.compare_bytes(raw, raw, desc)["accepted"]
    metrics = validation.compare_bytes(raw, packed("H", value + 1), desc)
    assert not metrics["accepted"]
    assert metrics["failedComponents"] == 1
    assert metrics["maximumAbsoluteError"] == pytest.approx(1.0 / 65535)
    assert metrics["maximumAbsoluteError"] > validation.TOLERANCES["depthAbsolute"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_float_outputs_are_rejected_even_when_bytes_are_identical(value):
    raw = packed("f", value)
    metrics = validation.compare_bytes(raw, raw, descriptor())
    assert metrics["byteExact"] and not metrics["accepted"]
    assert metrics["failedComponents"] == 1


@pytest.mark.parametrize("kind,width,fmt", [("Float", 4, "f"), ("UInt", 4, "I"), ("Float", 2, "e")])
def test_exact_regular_output_counts_components_instead_of_pixels(kind, width, fmt):
    raw = packed(fmt, *([1] * 8))
    metrics = validation.compare_bytes(raw, raw, descriptor(kind, width=width, count=4, pixels=2))
    assert metrics["accepted"] and metrics["comparedComponents"] == 8


def test_unknown_packed_output_rejects_but_exact_storage_is_recorded_as_bytes():
    desc = descriptor("UNorm", width=0, count=4, format_type="ResourceFormatType.BC7")
    raw = b"abcd"
    metrics = validation.compare_bytes(raw, raw, desc)
    assert metrics["accepted"] and metrics["comparisonKind"] == "byte-exact"
    with pytest.raises(ValueError, match="packed/compressed"):
        validation.compare_bytes(raw, b"abce", desc)


@pytest.mark.parametrize("bits,mantissa,value", [
    (0x000, 6, 0.0), (0x001, 6, 2.0 ** -20), (0x03f, 6, 63 * 2.0 ** -20),
    (0x040, 6, 2.0 ** -14), (0x3c0, 6, 1.0), (0x400, 6, 2.0),
    (0x7bf, 6, 65024.0), (0x001, 5, 2.0 ** -19), (0x1e0, 5, 1.0),
    (0x3df, 5, 64512.0),
])
def test_packed_unsigned_float_independent_bit_patterns(bits, mantissa, value):
    assert validation._unsigned_float(bits, mantissa) == value


@pytest.mark.parametrize("bits,mantissa,infinite", [(0x7c0, 6, True), (0x7c1, 6, False),
                                                    (0x3e0, 5, True), (0x3ff, 5, False)])
def test_packed_unsigned_float_infinity_and_nan(bits, mantissa, infinite):
    import math
    value = validation._unsigned_float(bits, mantissa)
    assert (math.isinf(value) if infinite else math.isnan(value))


def test_r11g11b10_checks_rgb_independently_without_relaxing_float_tolerance():
    desc = descriptor("Float", width=0, count=3, pixels=2, format_type="R11G11B10")
    one = 0x3c0 | (0x3c0 << 11) | (0x1e0 << 22)
    before = packed("I", one, one)
    after = packed("I", one + (1 << 11), one + (1 << 22))
    result = validation.compare_bytes(before, after, desc)
    assert result["comparedComponents"] == 6 and result["failedComponents"] == 2
    assert [(s["x"], s["component"]) for s in result["mismatchSamples"]] == [(0, 1), (1, 2)]
    assert result["mismatchSamples"][0]["baselineValue"] == 1.0
    assert result["mismatchSamples"][0]["candidateValue"] == 1.015625
    assert result["worstMismatch"]["absoluteError"] == 0.03125
    assert result["mismatchSamples"][0]["allowedAbsoluteError"] == pytest.approx(0.0001 + 0.001 * 1.015625)


@pytest.mark.parametrize("code", [0x7c0, 0x7c1])
def test_identical_packed_nonfinite_values_are_not_accepted(code):
    raw = packed("I", code)
    result = validation.compare_bytes(raw, raw, descriptor("Float", width=0, count=3, format_type="R11G11B10"))
    assert result["byteExact"] and not result["accepted"]
    assert result["failedComponents"] == result["baselineNonFiniteComponents"] == 1


def test_r10g10b10a2_uses_rgb_and_alpha_quantization_separately():
    desc = descriptor("UNorm", width=0, count=4, format_type="R10G10B10A2")
    assert validation.compare_bytes(packed("I", 0), packed("I", 1 | (1 << 30)), desc)["accepted"]
    result = validation.compare_bytes(packed("I", 0), packed("I", 2 | (2 << 30)), desc)
    assert result["failedComponents"] == 2
    assert [s["component"] for s in result["mismatchSamples"]] == [0, 3]
    assert result["mismatchSamples"][0]["allowedAbsoluteError"] == pytest.approx(1.0 / 1023)
    assert result["mismatchSamples"][1]["allowedAbsoluteError"] == pytest.approx(1.0 / 3)
    desc["componentType"] = "UInt"
    assert not validation.compare_bytes(packed("I", 0), packed("I", 1), desc)["accepted"]


@pytest.mark.parametrize("before,after", [(b"", b""), (b"1234", b"12")])
def test_empty_or_size_changed_outputs_are_not_accepted(before, after):
    with pytest.raises(ValueError, match="empty or changed size"):
        validation.compare_bytes(before, after, descriptor())


def test_same_bytes_with_invalid_regular_row_layout_do_not_inflate_coverage():
    with pytest.raises(ValueError, match="row layout"):
        validation.compare_bytes(b"1234", b"1234", descriptor(count=4))


def test_mismatch_diagnostics_locate_sparse_channels_without_another_replay():
    desc = descriptor(count=4, pixels=3)
    desc["height"] = 2
    before, after = [0.] * 24, [0.] * 24
    after[7], after[21] = 0.25, 0.5
    result = validation.compare_bytes(packed("f", *before), packed("f", *after), desc)
    assert result["failedComponents"] == 2
    assert [(s["x"], s["y"], s["component"]) for s in result["mismatchSamples"]] == [(1, 0, 3), (2, 1, 1)]
    assert result["mismatchBounds"] == {"minX": 1, "minY": 0, "maxX": 2, "maxY": 1}
    assert result["worstMismatch"]["candidateValue"] == 0.5
    assert result["worstMismatch"]["allowedAbsoluteError"] == pytest.approx(0.0006)
    assert result["mismatchSamplesTruncated"] is False


def test_mismatch_diagnostics_are_bounded_and_preserve_worst_outside_sample():
    count = validation.MAX_MISMATCH_SAMPLES + 5
    result = validation.compare_bytes(packed("f", *([0.] * count)),
        packed("f", *([1.] * (count - 1) + [2.])), descriptor(pixels=count))
    assert len(result["mismatchSamples"]) == validation.MAX_MISMATCH_SAMPLES
    assert result["failedComponents"] == count
    assert result["mismatchSamplesTruncated"] is True
    assert result["worstMismatch"]["x"] == count - 1
    assert result["worstMismatch"]["absoluteError"] == 2


def test_nonfinite_mismatch_diagnostics_are_strict_json_values():
    import json
    result = validation.compare_bytes(packed("f", float("nan")), packed("f", float("inf")), descriptor())
    json.dumps(result, allow_nan=False)
    assert result["mismatchSamples"][0]["reason"] == "non-finite"


def texture_format(kind="CompType.Float", width=4):
    return NS(type="ResourceFormatType.Regular", compType=kind, compByteWidth=width, compCount=1)


class FakeController:
    def __init__(self):
        self.calls = []
        self.targets = {}
        self.mapping = None
        self.event_id = 17
        self.build_count = 0
        self.build_failure = None
        self.observe_failure = None
        self.observe_count = 0
        self.texture = NS(resourceId=41, msSamp=1, depth=1, byteSize=4,
                          width=1, height=1, mips=1, arraysize=1, format=texture_format())
        self.output = NS(resource=41, firstMip=0, firstSlice=0, numSlices=1, format=texture_format())
        self.outputs = [self.output]
        self.depth = NS(resource=0)
        self.output_values = {201: packed("f", 1), 202: packed("f", 1.0005)}

    def BuildTargetShader(self, entry, encoding, blob, flags, stage):
        self.build_count += 1
        self.calls.append(("build", self.build_count))
        if self.build_failure == self.build_count:
            return 0, "injected compiler failure"
        target = 200 + self.build_count
        self.targets[target] = blob
        return target, ""

    def GetShaderEntryPoints(self, target):
        return [NS(name="MainPS", stage="fragment")]

    def GetShader(self, unused_pipeline, target, entry):
        return NS(raw=self.targets[target], readWriteResources=[])

    def GetPipelineState(self):
        return NS(GetOutputTargets=lambda: self.outputs, GetDepthTarget=lambda: self.depth)

    def GetTextures(self):
        return [self.texture]

    def GetTextureData(self, resource, subresource):
        self.observe_count += 1
        self.calls.append(("observe", self.mapping, subresource))
        if self.observe_failure == self.observe_count:
            raise ValueError("injected unsupported output")
        return self.output_values.get(self.mapping, packed("f", 1))

    def ClearReplayCache(self):
        self.calls.append(("clear",))

    def FreeTargetResource(self, target):
        assert self.mapping != target
        self.calls.append(("free", target))
        del self.targets[target]


class FakeBridge:
    def __init__(self, existing=False):
        self.ctrl = FakeController()
        self.rd = NS(ShaderEncoding=NS(DXIL="DXIL", DXBC="DXBC"),
                     ShaderCompileFlags=lambda: None, ResourceId=lambda: 0,
                     Subresource=lambda mip, slice_id, sample: (mip, slice_id, sample))
        self._capture_session_id = "b" * 32
        self._event_shader_overrides = {}
        self._shared_shader_replacements = ({7: {"originalResource": 7, "targetResource": 999}} if existing else {})
        self.orphan_targets = []
        self.orphan_target_limit = 64
        self.ctrl.mapping = 999 if existing else None
        self.replace_failure = None
        self.restore_failure = False
        self.visible_restore_failure = False
        self.compare_compatible = True
        self.event = {"eventId": 50, "expectedOriginalResourceId": 7}
        self.params = {"expected_capture_id": "a" * 64, "expected_capture_session_id": self._capture_session_id,
                       "stage": "fragment", "encoding": "dxbc", "entry_point": "MainPS"}
        for key, blob in (("baseline", native_blob("baseline")), ("candidate", native_blob("candidate"))):
            self.params[key] = {"path": key, "rawSha256": hashlib.sha256(blob).hexdigest()}

    def _require_capture_identity(self, value):
        return value

    def _normalise_stage_name(self, value):
        return value

    def _shader_override_events(self, params):
        return [self.event]

    def _invoke_on_ui_sync(self, fn):
        return fn()

    def _context_resource_ids(self):
        return {7: 7}

    def _require_shared_shader_ui_owner(self, original, existing):
        return None

    def _available_shader_stages(self, stages):
        return [("fragment", "fragment")]

    def _read_shader_candidate(self, params):
        blob = native_blob("baseline" if params["candidate_path"] == "baseline" else "candidate")
        return None, blob, hashlib.sha256(blob).hexdigest()

    def _request_visible_event_id(self, fallback):
        return 17

    def _shader_api_token(self, ctrl):
        return "d3d12"

    def _validate_live_shader_event(self, ctrl, *args, **kwargs):
        ctrl.event_id = 50
        return {"reflection": NS(raw=native_blob("captured"), readWriteResources=[]), "pipelineResource": 3}

    def _resource_id_int(self, value):
        return int(value or 0)

    def _exact_shader_entry(self, entries, entry, stage):
        return entries[0]

    def _shader_raw_bytes(self, reflection):
        return reflection.raw

    def _physical_shader_interfaces_compatible(self, original, candidate):
        return self.compare_compatible

    def _dxil_ambiguous_interface_compatible(self, *args, **kwargs):
        return self.compare_compatible

    def _replace_shared_shader_resource(self, ctrl, api, original, target):
        ctrl.calls.append(("replace", target))
        ctrl.mapping = target
        if target == self.replace_failure or (target == 999 and self.restore_failure):
            raise RuntimeError("injected replacement failure")

    def _remove_shared_shader_resource(self, ctrl, api, original):
        ctrl.calls.append(("remove", original))
        if self.restore_failure:
            raise RuntimeError("injected rollback failure")
        ctrl.mapping = None

    def _set_frame_event(self, ctrl, event, force, allow_background_preemption=True):
        ctrl.calls.append(("event", event))
        if event == 17 and self.visible_restore_failure:
            raise RuntimeError("injected visible restore failure")
        ctrl.event_id = event

    def _fmt_str(self, fmt):
        return str(fmt.compType)

    def _replay(self, fn):
        return fn(self.ctrl)

    def _register_orphan_shader_target(self, target):
        if target in self.orphan_targets:
            return False
        self.orphan_targets.append(target)
        return True

    def _require_shader_target_build_capacity(self, required_targets=1):
        if len(self.orphan_targets) + int(required_targets or 0) > self.orphan_target_limit:
            raise RuntimeError("orphan Shader target limit")

    def _build_target_shader(self, ctrl, entry, encoding, blob, flags, stage):
        self._require_shader_target_build_capacity(1)
        return ctrl.BuildTargetShader(entry, encoding, blob, flags, stage)


def test_exact_captured_baseline_is_observed_without_building_or_freeing_original():
    bridge = FakeBridge(existing=True)
    bridge.event["expectedOriginalSha256"] = bridge.params["baseline"]["rawSha256"]
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] is True
    assert result["replayRestored"] is True
    assert bridge.ctrl.mapping == 999
    assert sum(1 for call in bridge.ctrl.calls if call[0] == "free") == 1
    assert ("remove", 7) in bridge.ctrl.calls


@pytest.mark.parametrize("existing", [False, True])
def test_ab_observation_restores_prior_replacement_and_visible_event(existing):
    bridge = FakeBridge(existing)
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] and result["replayRestored"]
    assert result["sampleCount"] == 1
    assert not result["semanticEquivalenceVerified"]
    assert bridge.ctrl.mapping == (999 if existing else None)
    assert bridge.ctrl.event_id == 17 and bridge.ctrl.targets == {}
    assert bridge.ctrl.observe_count == 2
    assert "baselineStability" not in result


@pytest.mark.parametrize("failed", [False, True])
def test_native_analysis_avoids_viewer_refresh_for_each_observation(failed):
    bridge = FakeBridge(existing=True)
    bridge.ctrl.ReplayFrameEventForAnalysis = lambda event: bridge.ctrl.calls.append(("analysis", event))
    if failed:
        bridge.ctrl.output_values[202] = packed("f", 1.5)
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] is (not failed)
    assert result["replayMethod"] == "analysis-full"
    assert bridge.ctrl.calls.count(("analysis", 50)) == (3 if failed else 2)
    assert [call for call in bridge.ctrl.calls if call[0] == "event"] == [("event", 17)]
    assert bridge.ctrl.mapping == 999 and bridge.ctrl.targets == {}


def test_native_analysis_failure_still_restores_mapping_and_event():
    bridge = FakeBridge(existing=True)
    def fail(event):
        raise RuntimeError("analysis replay failed")
    bridge.ctrl.ReplayFrameEventForAnalysis = fail
    with pytest.raises(RuntimeError, match="analysis replay failed"):
        validation.compare_candidates(bridge, bridge.params)
    assert bridge.ctrl.mapping == 999 and bridge.ctrl.event_id == 17
    assert bridge.ctrl.targets == {}


def test_original_self_observation_does_not_remove_absent_mapping_per_leg():
    bridge = FakeBridge()
    bridge.params["candidate"] = dict(bridge.params["baseline"])
    bridge.event["expectedOriginalSha256"] = bridge.params["baseline"]["rawSha256"]
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] is True
    assert bridge.ctrl.observe_count == 2
    # Only the unconditional final restoration touches the mapping; neither
    # observation starts a redundant checked removal/presentation replay.
    assert bridge.ctrl.calls.count(("remove", 7)) == 1
    assert bridge.ctrl.build_count == 0


def install_pair_result(bridge, candidate=1.0005, repeat=1.0):
    bridge.event["expectedOriginalSha256"] = bridge.params["baseline"]["rawSha256"]
    output = NS(resourceId=41, index=0, depth=False,
                subresource=NS(mip=0, slice=0, sample=0),
                format=texture_format(), width=1, height=1,
                baseline=packed("f", 1), candidate=packed("f", candidate),
                baselineRepeat=packed("f", repeat))
    pair = NS(eventId=50, supported=True, completed=True, reason="", outputs=[output])
    def compare(event, original, baseline, target):
        assert bridge.ctrl.mapping is None
        bridge.ctrl.calls.append(("paired", event, original, baseline, target))
        return pair
    bridge.ctrl.ComparePixelShaderPair = compare
    return pair


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("api", ["d3d11", "d3d12", "vulkan", "opengl"])
def test_same_input_pair_uses_native_snapshots_without_installing_candidate(existing, api):
    bridge = FakeBridge(existing)
    bridge._shader_api_token = lambda ctrl: api
    install_pair_result(bridge)
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] and result["replayRestored"]
    assert result["replayMethod"] == "same-input-paired-draw"
    assert result["sampleCount"] == 1
    assert result["baselineStability"]["byteExact"]
    assert bridge.ctrl.observe_count == 0
    assert ("paired", 50, 7, 7, 201) in bridge.ctrl.calls
    assert ("replace", 201) not in bridge.ctrl.calls
    assert bridge.ctrl.mapping == (999 if existing else None)
    assert bridge.ctrl.event_id == 17 and bridge.ctrl.targets == {}


def test_same_input_self_comparison_passes_original_resource_without_building():
    bridge = FakeBridge()
    install_pair_result(bridge, candidate=1)
    bridge.params["candidate"] = dict(bridge.params["baseline"])
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] and bridge.ctrl.build_count == 0
    assert ("paired", 50, 7, 7, 7) in bridge.ctrl.calls


def test_same_input_pair_also_compares_two_nonoriginal_drafts():
    bridge = FakeBridge(existing=True)
    install_pair_result(bridge)
    bridge.event.pop("expectedOriginalSha256")
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] and result["replayMethod"] == "same-input-paired-draw"
    assert ("paired", 50, 7, 201, 202) in bridge.ctrl.calls
    assert bridge.ctrl.build_count == 2 and bridge.ctrl.targets == {}
    assert bridge.ctrl.observe_count == 0 and bridge.ctrl.mapping == 999


def test_same_input_matching_candidate_cannot_hide_unstable_baseline():
    bridge = FakeBridge()
    install_pair_result(bridge, candidate=1, repeat=1.5)
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"]
    assert result["metrics"]["outputs"][0]["accepted"]
    assert not result["baselineStability"]["withinTolerance"]
    assert result["diagnosticCode"] == "baseline-replay-unstable"
    assert result["candidateDifferenceConclusive"] is False
    assert bridge.ctrl.observe_count == 0


def test_unsupported_native_pair_falls_back_with_reason_and_restores():
    bridge = FakeBridge(existing=True)
    pair = install_pair_result(bridge)
    pair.supported, pair.completed, pair.reason = False, False, "render pass unsupported"
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"]
    assert result["pairedReplayUnavailable"] == "render pass unsupported"
    assert result["replayMethod"] == "frame-event"
    assert bridge.ctrl.observe_count == 2
    assert bridge.ctrl.mapping == 999 and bridge.ctrl.event_id == 17


def test_fallback_failure_preserves_why_same_input_pair_was_unavailable():
    bridge = FakeBridge()
    pair = install_pair_result(bridge)
    pair.supported, pair.completed, pair.reason = False, False, "render pass unsupported"
    bridge.ctrl.observe_failure = 1
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and result["replayRestored"]
    assert result["pairedReplayUnavailable"] == "render pass unsupported"
    assert "unsupported output" in result["reason"]


@pytest.mark.parametrize("failure", ["incomplete", "wrong-event", "native-error"])
def test_started_pair_failure_is_not_retried_or_accepted(failure):
    bridge = FakeBridge(existing=True)
    pair = install_pair_result(bridge)
    if failure == "incomplete":
        pair.completed, pair.reason = False, "GPU readback unavailable"
    elif failure == "wrong-event":
        pair.eventId = 49
    else:
        def fail(*unused):
            raise RuntimeError("native paired failure")
        bridge.ctrl.ComparePixelShaderPair = fail
    with pytest.raises(RuntimeError):
        validation.compare_candidates(bridge, bridge.params)
    assert bridge.ctrl.observe_count == 0
    assert bridge.ctrl.mapping == 999 and bridge.ctrl.event_id == 17
    assert bridge.ctrl.targets == {}


@pytest.mark.parametrize("invalid", ["empty", "duplicate", "wrong-slice", "short", "budget"])
def test_malformed_pair_snapshots_fail_after_restoring_replay(invalid):
    bridge = FakeBridge()
    pair = install_pair_result(bridge)
    output = pair.outputs[0]
    if invalid == "empty":
        pair.outputs = []
    elif invalid == "duplicate":
        pair.outputs.append(output)
    elif invalid == "wrong-slice":
        output.subresource.slice = 1
    elif invalid == "short":
        output.candidate = b"\0"
    else:
        output.width = 2
    with mock.patch.object(validation, "OUTPUT_LIMIT", 3 if invalid == "budget" else 32 * 1024 * 1024):
        result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and result["sampleCount"] == 0
    assert result["replayRestored"] and bridge.ctrl.event_id == 17
    assert bridge.ctrl.targets == {}


@pytest.mark.parametrize("unstable", [False, True])
def test_failed_comparison_repeats_baseline_once_without_relaxing_acceptance(unstable):
    bridge = FakeBridge(existing=True)
    bridge.ctrl.output_values[202] = packed("f", 1.5)
    original = bridge.ctrl.GetTextureData
    def read(resource, subresource):
        if unstable and bridge.ctrl.observe_count == 2:
            bridge.ctrl.output_values[201] = packed("f", 1.5)
        return original(resource, subresource)
    bridge.ctrl.GetTextureData = read
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] is False
    assert result["baselineStability"]["checked"] is True
    assert result["baselineStability"]["byteExact"] is (not unstable)
    if unstable:
        assert result["diagnosticCode"] == "baseline-replay-unstable"
        assert result["candidateDifferenceConclusive"] is False
    else:
        assert "candidateDifferenceConclusive" not in result
    assert bridge.ctrl.observe_count == 3
    assert bridge.ctrl.mapping == 999 and bridge.ctrl.event_id == 17
    assert bridge.ctrl.targets == {}


@pytest.mark.parametrize("cancelled", [False, True])
def test_optional_baseline_repeat_honors_cancel_and_diagnostic_budget(cancelled):
    import threading
    bridge = FakeBridge()
    bridge.ctrl.output_values[202] = packed("f", 1.5)
    cancel = threading.Event()
    if cancelled:
        cancel.set()
    bridge._request_dispatch_context = lambda: {"cancelEvent": cancel}
    with mock.patch.object(validation, "BASELINE_REPEAT_START_LIMIT_SECONDS", 45 if cancelled else 0):
        result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] is False
    assert result["baselineStability"]["checked"] is False
    assert bridge.ctrl.observe_count == 2 and bridge.ctrl.targets == {}


def test_optional_repeat_checks_cancellation_carried_to_native_replay_thread():
    import threading
    bridge = FakeBridge()
    bridge.ctrl.output_values[202] = packed("f", 1.5)
    cancelled = threading.Event()
    cancelled.set()
    bridge._request_dispatch_context = lambda: None
    bridge._request_event_tracker = lambda: {"dispatchContext": {"cancelEvent": cancelled}}
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["baselineStability"]["reason"] == "request-cancelled-or-preempted"
    assert bridge.ctrl.observe_count == 2


def test_exact_repeated_nonfinite_baseline_is_stable_but_not_within_numeric_policy():
    bridge = FakeBridge()
    bridge.ctrl.output_values[201] = packed("f", float("nan"))
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] is False
    assert result["baselineStability"]["byteExact"] is True
    assert result["baselineStability"]["withinTolerance"] is False


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_optional_repeat_failure_preserves_primary_mismatch_evidence(error_type):
    bridge = FakeBridge(existing=True)
    bridge.ctrl.output_values[202] = packed("f", 1.5)
    read = bridge.ctrl.GetTextureData
    def fail_third(resource, subresource):
        if bridge.ctrl.observe_count == 2:
            raise error_type("third read unavailable")
        return read(resource, subresource)
    bridge.ctrl.GetTextureData = fail_third
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] is False and result["sampleCount"] == 1
    metric = result["metrics"]["outputs"][0]
    assert metric["failedComponents"] == 1
    assert metric["mismatchSamples"][0]["candidateValue"] == 1.5
    assert result["baselineStability"]["checked"] is False
    assert result["baselineStability"]["errorType"] == error_type.__name__
    assert result["replayRestored"] is True
    assert bridge.ctrl.mapping == 999 and bridge.ctrl.targets == {}


def test_optional_repeat_failure_cannot_hide_failed_restoration():
    bridge = FakeBridge(existing=True)
    bridge.ctrl.output_values[202] = packed("f", 1.5)
    bridge.ctrl.observe_failure = 3
    bridge.restore_failure = True
    with pytest.raises(RuntimeError, match="replacement failure"):
        validation.compare_candidates(bridge, bridge.params)
    assert sorted(bridge.orphan_targets) == [201, 202]


@pytest.mark.parametrize("pending,current", [(True, 7), (False, 8)])
def test_optional_repeat_yields_to_foreground_work_without_selection_event(pending, current):
    bridge = FakeBridge()
    bridge.ctrl.output_values[202] = packed("f", 1.5)
    bridge._request_event_tracker = lambda: {"foregroundReplayGeneration": 7}
    bridge._foreground_replay_pending = lambda: pending
    bridge._foreground_replay_generation = lambda: current
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["baselineStability"]["checked"] is False
    assert result["baselineStability"]["reason"].startswith("foreground-replay-")
    assert result["sampleCount"] == 1 and bridge.ctrl.observe_count == 2


@pytest.mark.parametrize("failure", [1, 2])
def test_build_failure_restores_visible_event_and_releases_earlier_target(failure):
    bridge = FakeBridge()
    bridge.ctrl.build_failure = failure
    with pytest.raises(RuntimeError, match="BuildTargetShader failed"):
        validation.compare_candidates(bridge, bridge.params)
    assert bridge.ctrl.mapping is None and bridge.ctrl.event_id == 17
    assert bridge.ctrl.targets == {}


def test_two_target_validation_reserves_capacity_before_first_build():
    bridge = FakeBridge()
    bridge.orphan_targets = [object()] * (bridge.orphan_target_limit - 1)

    with pytest.raises(RuntimeError, match="orphan Shader target limit"):
        validation.compare_candidates(bridge, bridge.params)

    assert bridge.ctrl.build_count == 0
    assert len(bridge.orphan_targets) == bridge.orphan_target_limit - 1


def test_successful_restore_retains_every_target_when_free_permanently_fails():
    bridge = FakeBridge()

    def fail_free(target):
        raise RuntimeError("permanent free failure")

    bridge.ctrl.FreeTargetResource = fail_free
    result = validation.compare_candidates(bridge, bridge.params)

    assert result["accepted"] is True
    assert sorted(bridge.orphan_targets) == [201, 202]
    assert sorted(bridge.ctrl.targets) == [201, 202]


@pytest.mark.parametrize("failure", [1, 2])
@pytest.mark.parametrize("existing", [False, True])
def test_unsupported_output_returns_rejected_evidence_only_after_restore(failure, existing):
    bridge = FakeBridge(existing)
    bridge.ctrl.observe_failure = failure
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] is False and result["sampleCount"] == 0
    assert result["replayRestored"] is True
    assert "unsupported output" in result["reason"]
    assert bridge.ctrl.mapping == (999 if existing else None)
    assert bridge.ctrl.event_id == 17 and bridge.ctrl.targets == {}


def test_comparator_exception_restores_replay_before_propagating_error():
    bridge = FakeBridge()
    with mock.patch.object(validation, "compare_bytes", side_effect=RuntimeError("injected comparison failure")):
        with pytest.raises(RuntimeError, match="comparison failure"):
            validation.compare_candidates(bridge, bridge.params)
    assert bridge.ctrl.mapping is None and bridge.ctrl.event_id == 17
    assert bridge.ctrl.targets == {}


@pytest.mark.parametrize("existing", [False, True])
def test_restore_failure_is_rpc_error_and_does_not_free_possibly_referenced_targets(existing):
    bridge = FakeBridge(existing)
    bridge.restore_failure = True
    bridge.ctrl.observe_failure = 1
    with pytest.raises(RuntimeError, match="failure"):
        validation.compare_candidates(bridge, bridge.params)
    assert len(bridge.ctrl.targets) == 2
    assert not any(call[0] == "free" for call in bridge.ctrl.calls)
    assert sorted(bridge.orphan_targets) == [201, 202]


def test_visible_event_restore_failure_cannot_claim_replay_restored():
    bridge = FakeBridge()
    bridge.visible_restore_failure = True
    with pytest.raises(RuntimeError, match="visible restore failure"):
        validation.compare_candidates(bridge, bridge.params)
    assert len(bridge.ctrl.targets) == 2
    assert sorted(bridge.orphan_targets) == [201, 202]


@pytest.mark.parametrize("target", [201, 202])
def test_partial_setter_failure_still_restores_original_mapping(target):
    bridge = FakeBridge()
    bridge.replace_failure = target
    with pytest.raises(RuntimeError, match="replacement failure"):
        validation.compare_candidates(bridge, bridge.params)
    assert bridge.ctrl.mapping is None and bridge.ctrl.event_id == 17
    assert bridge.ctrl.targets == {}


def test_physical_abi_mismatch_returns_rejection_without_applying_candidates():
    bridge = FakeBridge()
    bridge.compare_compatible = False
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and result["sampleCount"] == 0
    assert "physical ABI mismatch" in result["reason"] and result["replayRestored"]
    assert not any(call[0] == "replace" for call in bridge.ctrl.calls)


def test_capture_session_change_during_observation_is_error_after_restore():
    bridge = FakeBridge()
    original = bridge.ctrl.GetTextureData
    def read(resource, subresource):
        bridge._capture_session_id = "c" * 32
        return original(resource, subresource)
    bridge.ctrl.GetTextureData = read
    with pytest.raises(RuntimeError, match="capture session changed"):
        validation.compare_candidates(bridge, bridge.params)
    assert bridge.ctrl.mapping is None and bridge.ctrl.targets == {}


def test_typeless_resource_uses_bound_view_format_for_numeric_comparison():
    bridge = FakeBridge()
    bridge.ctrl.texture.format = texture_format("CompType.Typeless")
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"]
    assert result["metrics"]["outputs"][0]["output"]["componentType"] == "float"


def test_native_integer_enums_preserve_zero_regular_member_and_named_component():
    class ResourceFormatType(enum.IntEnum):
        Regular = 0

    class CompType(enum.IntEnum):
        Float = 1

    bridge = FakeBridge()
    bridge.ctrl.output.format = NS(type=ResourceFormatType.Regular, compType=CompType.Float,
                                  compByteWidth=4, compCount=1)
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"]
    output = result["metrics"]["outputs"][0]["output"]
    assert output["formatType"] == "regular" and output["componentType"] == "float"


@pytest.mark.parametrize("attribute,value", [("numSlices", 2), ("firstMip", 1), ("firstSlice", 1)])
def test_layered_or_out_of_range_output_is_unsupported(attribute, value):
    bridge = FakeBridge()
    setattr(bridge.ctrl.output, attribute, value)
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and result["sampleCount"] == 0 and result["replayRestored"]


def test_legacy_target_without_num_slices_cannot_silently_sample_only_one_array_layer():
    bridge = FakeBridge()
    del bridge.ctrl.output.numSlices
    bridge.ctrl.texture.arraysize = 2
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and "layered" in result["reason"]


def test_per_output_and_combined_materialization_budgets_are_enforced():
    bridge = FakeBridge()
    with mock.patch.object(validation, "OUTPUT_LIMIT", 3):
        with pytest.raises(ValueError, match="budget"):
            validation._outputs(bridge, bridge.ctrl)
    bridge.ctrl.outputs = [bridge.ctrl.output, bridge.ctrl.output]
    bridge.ctrl.texture.byteSize = 1  # Actual readback must be independently bounded.
    with mock.patch.object(validation, "TOTAL_LIMIT", 7):
        with pytest.raises(ValueError, match="materialized output"):
            validation._outputs(bridge, bridge.ctrl)
