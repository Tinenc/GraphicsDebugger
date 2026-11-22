from __future__ import annotations

import copy
import hashlib
import inspect
import json
import pytest
from pathlib import Path
from unittest import mock

from src import server


SESSION = "a" * 32


def _context(session: str = SESSION) -> dict:
    return {
        "schema": "renderdoc-ultra-current-context/v1",
        "schemaVersion": 1,
        "capture": {
            "loaded": True,
            "captureId": "",
            "captureSessionId": session,
            "path": "C:/captures/frame.rdc",
            "api": "D3D12",
        },
        "events": {"currentEventId": 7, "selectedEventId": 7},
    }


class EvidenceIPC:
    def __init__(
        self,
        *,
        skip_reason: str | None = None,
        outside_path: Path | None = None,
        wrong_hash: bool = False,
        race_session: str | None = None,
    ) -> None:
        self.skip_reason = skip_reason
        self.outside_path = outside_path
        self.wrong_hash = wrong_hash
        self.race_session = race_session
        self.context_calls = 0
        self.calls: list[tuple[str, dict, float | None]] = []

    def call(self, method, params=None, timeout=None):
        payload = dict(params or {})
        self.calls.append((method, payload, timeout))
        if method == "get_current_renderdoc_context":
            self.context_calls += 1
            session = (
                self.race_session
                if self.race_session and self.context_calls > 1
                else SESSION
            )
            result = copy.deepcopy(_context(session))
            if payload.get("identity_only"):
                result["identityOnly"] = True
                result["events"] = {"currentEventId": 0, "selectedEventId": 0}
            return result
        if method != "capture_event_texture_evidence":
            raise AssertionError("unexpected IPC method: %s" % method)

        request_dir = Path(payload["artifact_dir"])
        attachment = {
            "kind": payload["attachment_kind"],
            "stage": payload["stage"],
            "index": payload["attachment_index"],
            "resourceId": 101,
            "mip": payload["mip"],
            "slice": payload["slice"],
            "sample": payload["sample"],
            "viewWidth": 64,
            "viewHeight": 32,
            "format": "RGBA16_FLOAT",
            "textureByteSize": (
                256 * 1024 * 1024 + 1
                if self.skip_reason == "source-size-budget-exceeded"
                else 1024 * 1024
            ),
            "sampleCount": 2,
            "presentedBackbuffer": payload["attachment_kind"] == "present",
            "descriptor": {
                "firstMip": payload["mip"],
                "numMips": 1,
                "firstSlice": payload["slice"],
                "numSlices": 1,
            },
        }
        is_minmax = payload["range_mode"] == "subresource-minmax"
        is_flat = self.skip_reason == "subresource-range-flat"
        display = {
            "channel": payload["channel"],
            "channelExtract": {
                "rgba": -1,
                "r": 0,
                "g": 1,
                "b": 2,
                "a": 3,
                "depth": 0,
            }[payload["channel"]],
            "rangeMode": payload["range_mode"],
            "blackPoint": (
                payload["black_point"]
                if payload["range_mode"] == "explicit"
                else (0.5 if is_flat else (0.2 if is_minmax else 0.0))
            ),
            "whitePoint": (
                payload["white_point"]
                if payload["range_mode"] == "explicit"
                else (0.5 if is_flat else (0.8 if is_minmax else 1.0))
            ),
            "subresourceMin": (
                [0.5, 0.5, 0.5, 0.5]
                if is_flat
                else ([0.2, 0.3, 0.4, 0.5] if is_minmax else None)
            ),
            "subresourceMax": (
                [0.5, 0.5, 0.5, 0.5]
                if is_flat
                else ([0.8, 0.7, 0.6, 0.5] if is_minmax else None)
            ),
            "depthInterpretation": (
                "raw-resource-component-0"
                if payload["channel"] == "depth"
                else None
            ),
        }
        if payload["range_mode"] == "visible-distribution":
            if self.skip_reason:
                display.update(blackPoint=None, whitePoint=None)
            else:
                display.update(
                    blackPoint=0.0, whitePoint=0.02,
                    subresourceMin=[0.0, 0.0, 0.0, 0.0],
                    subresourceMax=[65504.0, 65504.0, 65504.0, None],
                    distribution={
                        "method": "whole-subresource-histogram-percentile-v1",
                        "percentiles": [0.005, 0.995],
                        "channels": [index in (range(3) if payload["channel"] == "rgba" else [display["channelExtract"]]) for index in range(4)],
                        "excludedZeroComponents": True, "excludedNonFinite": True,
                        "histogramCalls": 3, "sampleCount": 1000,
                        "segments": [{"minimum": 1e-38, "maximum": 65504.0, "sampleCount": 1000}],
                    })
        if self.skip_reason:
            artifact = {
                "status": "skipped",
                "skipReason": self.skip_reason,
                "attachment": attachment,
                "display": display,
                "output": None,
            }
        else:
            filename = server._event_texture_evidence_filename(
                payload["event_id"],
                payload["attachment_kind"],
                payload["attachment_index"],
                payload["mip"],
                payload["slice"],
                payload["sample"],
                payload["channel"],
            )
            output_path = self.outside_path or (request_dir / filename)
            data = b"\x89PNG\r\n\x1a\nmanaged-evidence"
            output_path.write_bytes(data)
            artifact = {
                "status": "saved",
                "skipReason": None,
                "attachment": attachment,
                "display": display,
                "output": {
                    "path": str(output_path),
                    "mimeType": "image/png",
                    "byteSize": len(data),
                    "sha256": (
                        "0" * 64
                        if self.wrong_hash
                        else hashlib.sha256(data).hexdigest()
                    ),
                    "noOverwrite": True,
                },
            }
        return {
            "schema": "replaylab-event-texture-evidence/v1",
            "schemaVersion": 1,
            "requestId": payload["request_id"],
            "captureSessionId": payload["expected_capture_session_id"],
            "eventId": payload["event_id"],
            "restoration": {"originalEventId": 7, "restored": True},
            "exactEventBinding": True,
            "replayMutation": False,
            "budgets": {
                "maxTextureByteSize": 256 * 1024 * 1024,
                "maxSubresourcePixels": 16 * 1024 * 1024,
                "maxPngByteSize": 128 * 1024 * 1024,
            },
            "artifact": artifact,
        }


def _capture(root: Path, ipc: EvidenceIPC, **overrides) -> dict:
    arguments = {
        "event_id": 41,
        "attachment_kind": "rtv",
        "attachment_index": 0,
        "channel": "r",
        "mip": 1,
        "slice": 2,
        "sample": 1,
        "range_mode": "native",
    }
    arguments.update(overrides)
    with (
        mock.patch.object(server, "ipc", ipc),
        mock.patch.object(server, "_EVENT_TEXTURE_EVIDENCE_ROOT", root),
    ):
        return json.loads(server.capture_event_texture_evidence(**arguments)[0])


class PresentationEvidenceIPC(EvidenceIPC):
    def __init__(self, mutate):
        super().__init__()
        self.mutate = mutate

    def call(self, method, params=None, timeout=None):
        result = super().call(method, params, timeout)
        if method == "capture_event_texture_evidence":
            self.mutate(result)
        return result


def test_orientation_defaults_to_native_and_is_verified_in_saved_receipt(tmp_path):
    legacy = _capture(tmp_path / "legacy", EvidenceIPC())
    assert "error" not in legacy
    ipc = PresentationEvidenceIPC(lambda result: result["artifact"]["display"].update(flipY=True))
    result = _capture(tmp_path / "flipped", ipc, flip_y=True)
    assert "error" not in result
    assert result["artifact"]["display"]["flipY"] is True
    assert next(row[1] for row in ipc.calls if row[0] == "capture_event_texture_evidence")["flip_y"] is True


@pytest.mark.parametrize("actual,requested", [(None, True), (False, True), (True, False), (0, False), (1, True)])
def test_orientation_receipt_cannot_silently_drop_flip_or_coerce_boolean(tmp_path, actual, requested):
    def change(result):
        if actual is not None:
            result["artifact"]["display"]["flipY"] = actual
    result = _capture(tmp_path / "managed", PresentationEvidenceIPC(change), flip_y=requested)
    assert "error" in result


@pytest.mark.parametrize("invalid", [0, 1, "true", None])
def test_invalid_orientation_is_rejected_before_ipc(tmp_path, invalid):
    ipc = EvidenceIPC()
    result = _capture(tmp_path / "managed", ipc, flip_y=invalid)
    assert "error" in result and not ipc.calls


def volume_receipt(result):
    result["artifact"]["attachment"].update(
        textureDimension=3, resourceDepth=16, mipDepth=8, depthSlice=2,
        sliceKind="depth", viewSliceFirst=0, viewSliceCount=8,
        descriptor={"firstMip": 1, "numMips": 1, "firstSlice": 0, "numSlices": 8},
        nativeDescriptor={"firstMip": 1, "numMips": 1, "firstSlice": 0, "numSlices": 0})


def test_volume_receipt_retains_effective_and_native_slice_ranges(tmp_path):
    result = _capture(tmp_path / "managed", PresentationEvidenceIPC(volume_receipt))
    assert "error" not in result
    attachment = result["artifact"]["attachment"]
    assert attachment["nativeDescriptor"]["numSlices"] == 0
    assert attachment["descriptor"]["numSlices"] == attachment["mipDepth"] == 8


@pytest.mark.parametrize("field,value", [("mipDepth", 16), ("resourceDepth", 0), ("depthSlice", 3),
    ("sliceKind", "array"), ("viewSliceCount", 9), ("viewSliceFirst", 1)])
def test_volume_receipt_rejects_inconsistent_depth_metadata(tmp_path, field, value):
    def change(result):
        volume_receipt(result)
        result["artifact"]["attachment"][field] = value
    result = _capture(tmp_path / "managed", PresentationEvidenceIPC(change))
    assert "error" in result


class FloatBitEvidenceIPC(EvidenceIPC):
    def __init__(self, mutate=None):
        super().__init__()
        self.mutate = mutate

    def call(self, method, params=None, timeout=None):
        result = super().call(method, params, timeout)
        if method == "capture_event_texture_evidence":
            artifact = result["artifact"]
            artifact["attachment"].update(format="R32_UINT", componentCount=1, sampleCount=1)
            display = artifact["display"]
            display["valueDecode"] = "float32-bits"
            display["decoding"] = {
                "schema": "bread-texture-value-decode/v1", "sourceFormat": "R32_UINT",
                "interpretation": "ieee754-float32-little-endian", "pixelCount": 2048,
                "sourceBytes": 8192, "fullSubresource": True, "sourceBytesModified": False,
                "sourceSha256": "c" * 64, "finiteCount": 2040, "nonFiniteCount": 8,
                "zeroCount": 1040, "nonZeroFiniteCount": 1000,
            }
            if params["range_mode"] == "visible-distribution":
                display.update(subresourceMin=[0.0], subresourceMax=[100.0])
                display["distribution"].update(method="whole-subresource-float32-bits-histogram-v1",
                                               channels=[True, False, False, False], histogramCalls=0)
            if self.mutate:
                self.mutate(result)
        return result


@pytest.mark.parametrize("range_mode", ["native", "visible-distribution"])
def test_float_bit_preview_has_complete_exact_binding_receipt(tmp_path, range_mode):
    ipc = FloatBitEvidenceIPC()
    result = _capture(tmp_path / "managed", ipc, value_decode="float32-bits", sample=0, range_mode=range_mode)
    assert "error" not in result, result
    assert result["artifact"]["display"]["decoding"]["fullSubresource"] is True
    assert next(row[1] for row in ipc.calls if row[0] == "capture_event_texture_evidence")["value_decode"] == "float32-bits"


@pytest.mark.parametrize("key,value", [
    ("fullSubresource", False), ("sourceBytes", 7500), ("sourceSha256", ""),
    ("finiteCount", 2000), ("zeroCount", 1000), ("sourceBytesModified", True),
    ("interpretation", "guess-depth"),
])
def test_float_bit_preview_rejects_truncated_or_unproven_decode(tmp_path, key, value):
    ipc = FloatBitEvidenceIPC(lambda result: result["artifact"]["display"]["decoding"].update({key: value}))
    result = _capture(tmp_path / "managed", ipc, value_decode="float32-bits", sample=0)
    assert "error" in result
    assert not list((tmp_path / "managed").glob("*/manifest.json"))


def test_decoding_requires_explicit_request_and_never_guesses_from_format(tmp_path):
    result = _capture(tmp_path / "native", FloatBitEvidenceIPC(), sample=0)
    assert "error" in result
    ipc = EvidenceIPC()
    result = _capture(tmp_path / "bad-channel", ipc, value_decode="float32-bits", channel="g")
    assert "error" in result and not ipc.calls


def test_decoded_finite_float32_extrema_are_not_rejected_as_nonfinite(tmp_path):
    def extremes(result):
        display = result["artifact"]["display"]
        display["subresourceMax"] = [3.4028234663852886e38]
    result = _capture(tmp_path / "managed", FloatBitEvidenceIPC(extremes), value_decode="float32-bits",
                      sample=0, range_mode="visible-distribution")
    assert "error" not in result, result


def test_public_tool_has_no_output_path_and_returns_verified_manifest(tmp_path: Path) -> None:
    signature = inspect.signature(server.capture_event_texture_evidence)
    assert "output_path" not in signature.parameters
    assert "artifact_dir" not in signature.parameters

    ipc = EvidenceIPC()
    result = _capture(tmp_path / "managed", ipc)

    assert result["schema"] == "replaylab-event-texture-evidence-host/v1"
    assert result["status"] == "saved"
    assert result["artifact"]["output"]["mimeType"] == "image/png"
    output_path = Path(result["artifact"]["output"]["path"])
    manifest_path = Path(result["manifest"]["path"])
    assert output_path.is_file()
    assert manifest_path.is_file()
    assert output_path.parent == manifest_path.parent
    assert result["manifest"]["sha256"] == hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifact"]["output"]["path"] == str(output_path)
    assert manifest["policy"]["callerSuppliedPath"] is False

    bridge_call = next(call for call in ipc.calls if call[0] == "capture_event_texture_evidence")
    assert bridge_call[1]["expected_capture_session_id"] == SESSION
    assert bridge_call[1]["event_id"] == 41
    assert Path(bridge_call[1]["artifact_dir"]).parent == (tmp_path / "managed")
    assert bridge_call[2] == 300.0
    context_calls = [call for call in ipc.calls if call[0] == "get_current_renderdoc_context"]
    assert context_calls and all(not call[1].get("identity_only") for call in context_calls)


def test_compute_uav_and_fragment_input_receipts_preserve_binding_stage(tmp_path: Path) -> None:
    for kind, stage in (("uav", "compute"), ("srv", "fragment"), ("uav", "raygen")):
        ipc = EvidenceIPC()
        result = _capture(tmp_path / (kind + stage), ipc, attachment_kind=kind, stage=stage, attachment_index=12)
        assert "error" not in result, result
        assert result["artifact"]["attachment"]["stage"] == stage
        assert result["artifact"]["attachment"]["index"] == 12
        assert result["policy"]["rtvDsvOnly"] is False


def test_unavailable_stage_is_rejected_before_any_ipc(tmp_path: Path) -> None:
    ipc = EvidenceIPC()
    result = _capture(tmp_path, ipc, attachment_kind="uav", stage="invalid")
    assert "stage" in result["error"] and not ipc.calls


def test_budget_skip_still_gets_a_hashed_manifest(tmp_path: Path) -> None:
    ipc = EvidenceIPC(skip_reason="source-size-budget-exceeded")
    result = _capture(tmp_path / "managed", ipc)

    assert result["status"] == "skipped"
    assert result["artifact"]["output"] is None
    assert result["artifact"]["skipReason"] == "source-size-budget-exceeded"
    request_dir = Path(result["manifest"]["path"]).parent
    assert sorted(path.name for path in request_dir.iterdir()) == ["manifest.json"]


def test_flat_minmax_skip_is_preserved_in_manifest(tmp_path: Path) -> None:
    ipc = EvidenceIPC(skip_reason="subresource-range-flat")
    result = _capture(
        tmp_path / "managed", ipc, range_mode="subresource-minmax"
    )

    assert result["status"] == "skipped"
    assert result["artifact"]["skipReason"] == "subresource-range-flat"
    assert result["artifact"]["display"]["blackPoint"] == 0.5
    assert Path(result["manifest"]["path"]).is_file()


def test_outside_bridge_path_is_rejected_and_never_exposed(tmp_path: Path) -> None:
    outside = tmp_path / "outside.png"
    ipc = EvidenceIPC(outside_path=outside)
    result = _capture(tmp_path / "managed", ipc)

    assert result["type"] == "RuntimeError"
    assert "unsafe texture evidence path" in result["error"]
    assert outside.is_file()
    assert list((tmp_path / "managed").iterdir()) == []


def test_hash_mismatch_fails_closed_and_cleans_owned_request(tmp_path: Path) -> None:
    ipc = EvidenceIPC(wrong_hash=True)
    result = _capture(tmp_path / "managed", ipc)

    assert result["type"] == "RuntimeError"
    assert "integrity data" in result["error"]
    assert list((tmp_path / "managed").iterdir()) == []


def test_capture_session_race_fails_closed(tmp_path: Path) -> None:
    ipc = EvidenceIPC(race_session="c" * 32)
    result = _capture(tmp_path / "managed", ipc)

    assert result["type"] == "RuntimeError"
    assert "capture/session changed" in result["error"]
    assert list((tmp_path / "managed").iterdir()) == []


def test_scoped_capture_session_mismatch_fails_before_export(tmp_path: Path) -> None:
    ipc = EvidenceIPC()
    with mock.patch.object(server, "_SCOPED_CAPTURE_SESSION_ID", "c" * 32):
        result = _capture(tmp_path / "managed", ipc)

    assert result["type"] == "RuntimeError"
    assert "scoped capture session changed" in result["error"]
    assert all(method != "capture_event_texture_evidence" for method, _, _ in ipc.calls)


def test_non_directory_managed_root_fails_before_bridge_export(tmp_path: Path) -> None:
    root = tmp_path / "managed-root"
    root.write_text("not a directory", encoding="utf-8")
    ipc = EvidenceIPC()
    result = _capture(root, ipc)

    assert result["type"] == "RuntimeError"
    assert "root" in result["error"]
    assert all(method != "capture_event_texture_evidence" for method, _, _ in ipc.calls)


def test_each_request_uses_a_unique_no_overwrite_directory(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    first = _capture(root, EvidenceIPC())
    second = _capture(root, EvidenceIPC())

    first_dir = Path(first["manifest"]["path"]).parent
    second_dir = Path(second["manifest"]["path"]).parent
    assert first_dir != second_dir
    assert first_dir.is_dir()
    assert second_dir.is_dir()


def test_depth_contract_rejects_colour_and_out_of_range_values_before_io(
    tmp_path: Path,
) -> None:
    ipc = EvidenceIPC()
    colour_result = _capture(
        tmp_path / "managed-colour",
        ipc,
        attachment_kind="dsv",
        channel="r",
        mip=0,
        slice=0,
        sample=0,
    )
    assert colour_result["type"] == "ValueError"

    range_result = _capture(
        tmp_path / "managed-range",
        ipc,
        attachment_kind="dsv",
        channel="depth",
        mip=0,
        slice=0,
        sample=0,
        range_mode="explicit",
        black_point=-0.1,
        white_point=1.0,
    )
    assert range_result["type"] == "ValueError"
    assert ipc.calls == []


def test_distribution_is_the_default_and_receipt_is_verified(tmp_path):
    assert inspect.signature(server.capture_event_texture_evidence).parameters["range_mode"].default == "visible-distribution"
    result = _capture(tmp_path, EvidenceIPC(), range_mode="visible-distribution", channel="rgba")
    assert result["status"] == "saved", result
    assert result["artifact"]["display"]["whitePoint"] == 0.02
    assert result["artifact"]["display"]["distribution"]["channels"] == [True, True, True, False]


def test_distribution_budget_skip_does_not_claim_statistics(tmp_path):
    result = _capture(tmp_path, EvidenceIPC(skip_reason="source-size-budget-exceeded"), range_mode="visible-distribution")
    assert result["status"] == "skipped", result
    assert result["artifact"]["display"]["subresourceMin"] is None


def test_distribution_rejects_unmeasured_or_wrong_channel_evidence(tmp_path):
    class WrongChannels(EvidenceIPC):
        def call(self, method, params=None, timeout=None):
            result = super().call(method, params, timeout)
            if method == "capture_event_texture_evidence":
                result["artifact"]["display"]["distribution"]["channels"] = [True, True, True, True]
            return result
    result = _capture(tmp_path, WrongChannels(), range_mode="visible-distribution", channel="rgba")
    assert result["type"] == "RuntimeError", result
    assert "whole-texture exposure" in result["error"]


def test_rgb_minmax_does_not_reintroduce_discarded_alpha_in_host_validation(tmp_path):
    class ExtremeAlpha(EvidenceIPC):
        def call(self, method, params=None, timeout=None):
            result = super().call(method, params, timeout)
            if method == "capture_event_texture_evidence":
                result["artifact"]["display"]["subresourceMin"][3] = -1e20
                result["artifact"]["display"]["subresourceMax"][3] = 1e20
            return result
    result = _capture(tmp_path, ExtremeAlpha(), channel="rgba", range_mode="subresource-minmax")
    assert result["status"] == "saved", result
    assert result["artifact"]["display"]["whitePoint"] == 0.8


def test_large_atlas_requires_small_presentation_preview_on_host_too(tmp_path):
    class Atlas(EvidenceIPC):
        def call(self, method, params=None, timeout=None):
            result = super().call(method, params, timeout)
            if method == "capture_event_texture_evidence":
                result["artifact"]["attachment"].update(viewWidth=16384, viewHeight=2048,
                                                        textureByteSize=128*1024*1024)
            return result
    accepted = _capture(tmp_path / "small", Atlas(), max_dimension=2048)
    assert accepted["status"] == "saved", accepted
    rejected = _capture(tmp_path / "unbounded", Atlas())
    assert "beyond its budget" in rejected["error"]


def test_present_export_requires_actual_present_backbuffer_receipt(tmp_path):
    result = _capture(tmp_path / "valid", EvidenceIPC(), attachment_kind="present",
                      mip=0, slice=0, sample=0)
    assert result["status"] == "saved", result
    class NoProof(EvidenceIPC):
        def call(self, method, params=None, timeout=None):
            result = super().call(method, params, timeout)
            if method == "capture_event_texture_evidence":
                result["artifact"]["attachment"].pop("presentedBackbuffer")
            return result
    result = _capture(tmp_path / "missing", NoProof(), attachment_kind="present",
                      mip=0, slice=0, sample=0)
    assert "Present backbuffer" in result["error"]
