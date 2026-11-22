"""Catalog CPU finalization with real DXIL parsing and thread-confined native fakes."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from tests.test_bridge_catalog_rebind import _load_bridge
from tests.test_bridge_dxil_access_gate import reflection as dxil_reflection
from tests.test_dxil_access_signature import REAL_SHAPE_DXIL


def _plain(value, seen=None):
    """Bytes are allowed, but native wrappers, controllers and callbacks are not."""
    if type(value) in (str, bytes, int, float, bool, type(None)):
        return
    assert type(value) in (dict, list, tuple), "non-plain captured value: %s" % type(value)
    seen = set() if seen is None else seen
    if id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, dict):
        for key, item in value.items():
            _plain(key, seen)
            _plain(item, seen)
    else:
        for item in value:
            _plain(item, seen)


class _ReplayOwned:
    def __init__(self, rig, fields):
        object.__setattr__(self, "_rig", rig)
        object.__setattr__(self, "_fields", fields)

    def __getattr__(self, name):
        rig = object.__getattribute__(self, "_rig")
        rig.require_replay()
        fields = object.__getattribute__(self, "_fields")
        if name not in fields:
            raise AttributeError(name)
        return fields[name]


def _native(rig, value):
    if isinstance(value, SimpleNamespace):
        return _ReplayOwned(rig, {key: _native(rig, item) for key, item in vars(value).items()})
    if isinstance(value, list):
        return [_native(rig, item) for item in value]
    return value


class _CatalogRig:
    def __init__(self, tmp_path, monkeypatch):
        self.module = _load_bridge()
        self.output = tmp_path / "catalog"
        self.capture_path = str(tmp_path / "capture.rdc")
        self.ui_thread = threading.get_ident()
        self.replay_thread = None
        self.in_replay = False
        self.replay_returned = threading.Event()
        self.parse_entered = threading.Event()
        self.release_parse = threading.Event()
        self.block_parse = False
        self.foreground_generation = 0
        self.parse_calls = []
        self.callbacks = []
        self.event_id = 9
        self.reflection_calls = 0
        self.disassembly_calls = 0
        self.binding_calls = []
        self.blob = b"offline captured DXIL shader identity"
        self.reflection = _native(self, dxil_reflection(self.blob))
        self.entry = _native(self, SimpleNamespace(name="main"))
        rig = self

        class Pipeline:
            def GetShader(self, _stage): rig.require_replay(); return 77
            def GetShaderEntryPoint(self, _stage): rig.require_replay(); return "main"
            def GetShaderPipelineObject(self): rig.require_replay(); return 901
            def GetConstantBlock(self, stage, index, array_index):
                rig.require_replay()
                rig.binding_calls.append((rig.event_id, index))
                return _native(rig, SimpleNamespace(descriptor=SimpleNamespace(
                    resource=1000 + rig.event_id, byteOffset=rig.event_id * 16, byteSize=64)))

        class Controller:
            def GetAPIProperties(self):
                rig.require_replay()
                return _native(rig, SimpleNamespace(pipelineType="GraphicsAPI.D3D12", localRenderer="D3D12"))
            def GetFrameInfo(self): rig.require_replay(); return _native(rig, SimpleNamespace(frameNumber=1))
            def SetFrameEvent(self, event, _force): rig.require_replay(); rig.event_id = event
            def GetPipelineState(self): rig.require_replay(); return Pipeline()
            def GetShaderEntryPoints(self, resource):
                rig.require_replay(); assert resource == 77; return [rig.entry]
            def GetShader(self, pipeline, resource, entry):
                rig.require_replay(); assert (pipeline, resource, entry) == (901, 77, rig.entry)
                rig.reflection_calls += 1
                return rig.reflection
            def GetDisassemblyTargets(self, with_pipeline):
                rig.require_replay(); assert with_pipeline is True; return ["DXC DXIL"]
            def DisassembleShader(self, pipeline, reflection, target):
                rig.require_replay(); assert (pipeline, reflection, target) == (901, rig.reflection, "DXC DXIL")
                rig.disassembly_calls += 1
                return REAL_SHAPE_DXIL.decode("utf-8")
            def GetRootActions(self): raise AssertionError("explicit EIDs must not scan all actions")

        class ReplayManager:
            def AsyncInvoke(self, method, tag=""):
                assert callable(method) and isinstance(tag, str)
                rig.require_ui()
                rig.callbacks.append(method)
            def GetForegroundInvokeGeneration(self): rig.require_ui_or_replay(); return rig.foreground_generation
            def HasPendingForegroundInvoke(self): rig.require_ui_or_replay(); return False

        self.controller = Controller()
        self.replay_manager = ReplayManager()
        m = self.module
        m._ctx = SimpleNamespace(
            IsCaptureLoaded=lambda: self.ui_value(True), CurEvent=lambda: self.ui_value(9),
            GetCaptureFilename=lambda: self.ui_value(self.capture_path),
            Replay=lambda: self.ui_value(self.replay_manager))
        m._capture_session_id = "a" * 32
        m._active_capture_token = ("loaded", os.path.normcase(self.capture_path))
        m._replay = self.replay
        m._known_catalog_capture_ids = {}
        m._known_catalog_identity_pairs = set()
        m._symbol_capture_snapshot = {"captureId":"", "capturePath":"", "shaderKeys":set(), "usesByKey":{}}
        m.IPC_DIR = str(tmp_path / "ipc")
        Path(m.IPC_DIR).mkdir()
        self.snapshot_parser = m._dxil_evidence_from_snapshot

        def parse(source, blocks):
            assert not self.is_replay()
            _plain(blocks)
            assert type(source) is bytes
            self.parse_calls.append((threading.get_ident(), source, copy.deepcopy(blocks)))
            self.parse_entered.set()
            if self.block_parse and not self.release_parse.wait(3.0):
                raise AssertionError("test did not release the CPU parser")
            return self.snapshot_parser(source, blocks)

        monkeypatch.setattr(m, "_dxil_evidence_from_snapshot", parse)

    def is_replay(self):
        return self.in_replay and threading.get_ident() == self.replay_thread

    def require_replay(self): assert self.is_replay(), "native replay object escaped its callback"
    def require_ui(self): assert threading.get_ident() == self.ui_thread, "CPU worker touched CaptureContext"
    def require_ui_or_replay(self):
        assert threading.get_ident() == self.ui_thread or self.is_replay(), "CPU worker touched ReplayManager"
    def ui_value(self, value): self.require_ui_or_replay(); return value

    def replay(self, callback):
        self.in_replay = True
        self.replay_thread = threading.get_ident()
        try:
            return callback(self.controller)
        finally:
            self.in_replay = False
            self.replay_returned.set()

    def params(self):
        return {"output_dir":str(self.output), "event_ids":[101, 202], "stages":["fragment"]}

    def collect(self):
        result = self.module._export_shader_catalog(self.params(), defer_finalization=True)
        _plain(result)
        return result

    def begin(self):
        started = self.module._begin_export_shader_catalog(self.params())
        callback = self.callbacks.pop(0)
        self.replay(callback)
        return started["jobId"]

    def poll(self, job_id):
        return self.module._poll_export_shader_catalog({"job_id":job_id})

    def wait_finished(self, job_id):
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            with self.module._shader_catalog_export_jobs_guard:
                job = self.module._shader_catalog_export_jobs[job_id]
                if job["status"] not in ("queued", "running"):
                    return copy.copy(job)
            time.sleep(0.005)
        raise AssertionError("catalog worker did not finish")

    def assert_unpublished(self):
        assert self.module._known_catalog_capture_ids == {}
        assert self.module._known_catalog_identity_pairs == set()
        assert self.module._symbol_capture_snapshot["captureId"] == ""

    def close(self):
        self.release_parse.set()
        self.module._stop_shader_catalog_finalize_workers()
        assert not self.module._shader_catalog_finalize_workers


@pytest.fixture
def rig(tmp_path, monkeypatch):
    value = _CatalogRig(tmp_path, monkeypatch)
    try:
        yield value
    finally:
        value.close()


def test_collection_is_plain_and_returns_before_any_cpu_parser_or_file_write(rig):
    collected = rig.collect()
    assert rig.parse_calls == [] and rig.replay_returned.is_set()
    assert rig.reflection_calls == rig.disassembly_calls == 1
    assert len(collected["catalog"]["shaders"]) == 1
    assert [item["eventId"] for item in collected["catalog"]["uses"]] == [101, 202]
    assert rig.binding_calls == [(event, block) for event in (101, 202) for block in range(4)]
    assert rig.event_id == 9
    assert not rig.output.exists()
    rig.assert_unpublished()


def test_finalizer_preserves_real_dxil_layout_and_per_event_use_bindings(rig):
    collected = rig.collect()
    blocks = collected["catalog"]["shaders"][0]["constantBlocks"]
    expected_access, evidence = rig.snapshot_parser(REAL_SHAPE_DXIL, copy.deepcopy(blocks))
    expected_blocks = copy.deepcopy(blocks)
    for block in expected_blocks:
        marker = evidence.get((block["space"], block["bind"]))
        if marker:
            block.update(layoutCompleteness="ambiguous", layoutEvidence=marker)
    result = rig.module._finalize_shader_catalog_export(collected, defer_publication=True)
    catalog = json.loads(Path(result["catalogPath"]).read_text(encoding="utf-8"))
    assert catalog["shaders"][0]["constantBlocks"] == expected_blocks
    assert catalog["shaders"][0]["interfaceComplete"] is False
    for use in catalog["uses"]:
        for block, expected in zip(use["constantBlocks"], expected_blocks):
            binding = block.pop("binding")
            assert binding["resourceId"] == 1000 + use["eventId"]
            assert block == expected
    assert expected_access.source_sha256 == hashlib.sha256(REAL_SHAPE_DXIL).hexdigest()
    assert len(rig.parse_calls) == 1
    assert "_dxilEvidenceRequests" not in catalog
    assert "_deferredPublication" in result
    rig.assert_unpublished()


def test_synchronous_export_also_runs_parser_after_replay_callback_returns(rig):
    result = rig.module._export_shader_catalog(rig.params())
    assert rig.replay_returned.is_set() and len(rig.parse_calls) == 1
    assert result["captureId"] in rig.module._known_catalog_capture_ids
    assert Path(result["catalogPath"]).is_file()


@pytest.mark.parametrize("retained", [True, False])
def test_replaced_catalog_preserves_original_identity_without_candidate_dxil(rig, monkeypatch, retained):
    module = rig.module
    before = module._export_shader_catalog(rig.params())
    baseline = json.loads(Path(before["catalogPath"]).read_text(encoding="utf-8"))
    unused_access, original_layout = rig.snapshot_parser(
        REAL_SHAPE_DXIL, baseline["shaders"][0]["constantBlocks"])
    candidate = b"different active replacement DXIL"
    active_reflection = _native(rig, dxil_reflection(candidate))
    owner = {
        "captureSessionId": module._capture_session_id,
        "captureToken": module._active_capture_token, "api": "d3d12",
        "originalResource": 77, "expectedOriginalResourceId": 77,
        "expectedOriginalSha256": hashlib.sha256(rig.blob).hexdigest(),
        "stageName": "fragment", "entryPoint": "main",
        "candidateRawSha256": hashlib.sha256(candidate).hexdigest(),
        "capturedOriginalReflection": rig.reflection,
        "capturedDxilLayoutEvidence": original_layout if retained else None,
    }
    module._shared_shader_replacements[77] = owner
    module._shared_shader_replacement_capture_token = module._active_capture_token

    def active_shader(*arguments):
        rig.require_replay()
        return active_reflection

    def reject_disassembly(*arguments):
        raise AssertionError("replacement disassembly must never identify the original")

    monkeypatch.setattr(rig.controller, "GetShader", active_shader)
    monkeypatch.setattr(rig.controller, "DisassembleShader", reject_disassembly)
    parameters = dict(rig.params(), output_dir=str(rig.output.parent / "replaced"),
                      expected_stable_capture_id=before["stableCaptureId"],
                      expected_stable_identity_evidence_sha256=before["stableIdentityEvidenceSha256"])
    after = module._export_shader_catalog(parameters)
    catalog = json.loads(Path(after["catalogPath"]).read_text(encoding="utf-8"))
    for field in ("captureId", "stableCaptureId", "identityEvidenceSha256", "stableIdentityEvidenceSha256"):
        assert after[field] == before[field]
    assert catalog["shaders"][0]["rawSha256"] == hashlib.sha256(rig.blob).hexdigest()
    assert len(rig.parse_calls) == 1
    if retained:
        assert catalog["errors"] == baseline["errors"]
        assert catalog["shaders"][0]["constantBlocks"] == baseline["shaders"][0]["constantBlocks"]
    else:
        assert catalog["shaders"][0]["interfaceComplete"] is False
        assert all(block["layoutEvidence"]["typeAnnotation"] == "unavailable"
                   for block in catalog["shaders"][0]["constantBlocks"])
        assert "Captured-original DXIL layout evidence is unavailable" in catalog["errors"][0]["message"]
    assert module._shared_shader_replacements[77] is owner


def test_synchronous_dispatch_cancel_stops_cpu_phase_before_files(rig):
    collected = rig.collect()
    cancel = threading.Event()
    cancel.set()
    rig.module._request_dispatch_state.current = {"cancelEvent": cancel}
    try:
        with pytest.raises(rig.module._ShaderCatalogExportCancelled):
            rig.module._finalize_shader_catalog_export(collected)
    finally:
        del rig.module._request_dispatch_state.current
    assert rig.parse_calls == [] and not rig.output.exists()
    rig.assert_unpublished()


def test_async_parser_can_block_after_replay_returns_while_poll_stays_running_until_ui_consume(rig):
    rig.block_parse = True
    job_id = rig.begin()
    assert rig.parse_entered.wait(2.0) and rig.replay_returned.is_set()
    assert rig.parse_calls[0][0] != rig.ui_thread
    assert rig.poll(job_id)["status"] == "running"
    assert not rig.output.exists()
    rig.assert_unpublished()
    rig.release_parse.set()
    assert rig.wait_finished(job_id)["status"] == "completed"
    assert rig.poll(job_id)["status"] == "completed"
    rig.assert_unpublished()
    result = rig.module._consume_export_shader_catalog({"job_id":job_id})["result"]
    assert result["captureId"] in rig.module._known_catalog_capture_ids
    assert (result["captureId"], result["identityEvidenceSha256"]) in rig.module._known_catalog_identity_pairs
    assert job_id not in rig.module._shader_catalog_export_jobs


@pytest.mark.parametrize("change", ["cancel", "session", "capture-token"])
def test_cancellation_or_capture_change_during_parse_stops_all_files_and_publication(rig, change):
    rig.block_parse = True
    job_id = rig.begin()
    assert rig.parse_entered.wait(2.0)
    if change == "cancel":
        result = rig.module._cancel_export_shader_catalog({"job_id":job_id})
        assert result["cancellationRequested"] is True
    elif change == "session":
        rig.module._capture_session_id = "b" * 32
    else:
        rig.module._active_capture_token = ("loaded", os.path.normcase(rig.capture_path + ".new"))
    rig.release_parse.set()
    job = rig.wait_finished(job_id)
    assert job["status"] in ("cancelled", "failed")
    assert job["result"] is None and job["publication"] is None
    assert not rig.output.exists()
    rig.assert_unpublished()
    with pytest.raises((ValueError, RuntimeError)):
        rig.module._consume_export_shader_catalog({"job_id":job_id})


def test_cancel_after_first_blob_write_does_not_write_catalog_or_publish(rig, monkeypatch):
    collected = rig.collect()
    cancel = threading.Event()
    writes = []
    original = rig.module._atomic_write_bytes

    def write(path, value):
        writes.append(path)
        original(path, value)
        cancel.set()

    monkeypatch.setattr(rig.module, "_atomic_write_bytes", write)
    with pytest.raises(rig.module._ShaderCatalogExportCancelled):
        rig.module._finalize_shader_catalog_export(collected, cancel_event=cancel)
    assert len(writes) == 1 and writes[0].endswith(".bin")
    assert not (rig.output / "shader_catalog.v1.json").exists()
    rig.assert_unpublished()


def test_tampered_captured_blob_is_rejected_before_parsing_or_writes(rig):
    collected = rig.collect()
    raw_sha = next(iter(collected["blobs"]))
    collected["blobs"][raw_sha] = b"different captured bytes"
    with pytest.raises(RuntimeError, match="bytecode identity changed"):
        rig.module._finalize_shader_catalog_export(collected)
    assert rig.parse_calls == [] and not rig.output.exists()
    rig.assert_unpublished()


def test_tampered_captured_disassembly_is_rejected_before_parsing_or_writes(rig):
    collected = rig.collect()
    collected["catalog"]["_dxilEvidenceRequests"][0]["sourceBytes"] += b"\n; different snapshot"
    with pytest.raises(RuntimeError, match="disassembly identity changed"):
        rig.module._finalize_shader_catalog_export(collected)
    assert rig.parse_calls == [] and not rig.output.exists()
    rig.assert_unpublished()


def test_foreground_replay_during_cpu_phase_is_rechecked_on_ui_before_publication(rig):
    rig.block_parse = True
    job_id = rig.begin()
    assert rig.parse_entered.wait(2.0)
    rig.foreground_generation += 1
    rig.release_parse.set()
    assert rig.wait_finished(job_id)["status"] == "completed"
    with pytest.raises(rig.module._ShaderCatalogExportCancelled, match="user replay operation"):
        rig.module._consume_export_shader_catalog({"job_id": job_id})
    rig.assert_unpublished()


def test_invalid_access_evidence_keeps_catalog_layout_lower_only(rig, monkeypatch):
    collected = rig.collect()

    def unavailable(source, blocks):
        raise RuntimeError("DXC DXIL CBuffer access evidence is incomplete: unsupported load")

    monkeypatch.setattr(rig.module, "_dxil_evidence_from_snapshot", unavailable)
    result = rig.module._finalize_shader_catalog_export(collected, defer_publication=True)
    catalog = json.loads(Path(result["catalogPath"]).read_text(encoding="utf-8"))
    assert catalog["shaders"][0]["interfaceComplete"] is False
    assert catalog["errors"][0]["scope"] == "dxil-cbuffer-layout-evidence"
    assert catalog["counts"]["errors"] == 1
    for blocks in ([catalog["shaders"][0]["constantBlocks"]] +
                   [use["constantBlocks"] for use in catalog["uses"]]):
        assert all(block["layoutCompleteness"] == "ambiguous" and
                   block["layoutEvidence"]["typeAnnotation"] == "unavailable" for block in blocks)
    rig.assert_unpublished()


def test_dxil_snapshot_budget_discards_excess_text_and_never_claims_complete_layout(rig, monkeypatch):
    monkeypatch.setattr(rig.module, "MAX_SHADER_CATALOG_DXIL_SNAPSHOT_BYTES", len(REAL_SHAPE_DXIL) - 1)
    collected = rig.collect()
    request = collected["catalog"]["_dxilEvidenceRequests"][0]
    assert request["sourceBytes"] is None
    assert "byte budget" in request["sourceError"]
    result = rig.module._finalize_shader_catalog_export(collected, defer_publication=True)
    catalog = json.loads(Path(result["catalogPath"]).read_text(encoding="utf-8"))
    assert rig.parse_calls == []
    assert catalog["shaders"][0]["interfaceComplete"] is False
    assert "byte budget" in catalog["errors"][0]["message"]
    assert all(block["layoutEvidence"]["typeAnnotation"] == "unavailable"
               for block in catalog["shaders"][0]["constantBlocks"])
    rig.assert_unpublished()


def test_shutdown_waits_for_inflight_cpu_worker_and_cancels_its_publication(rig):
    rig.block_parse = True
    job_id = rig.begin()
    assert rig.parse_entered.wait(2.0)
    finished = threading.Event()
    errors = []

    def stop():
        try:
            rig.module._stop_shader_catalog_finalize_workers()
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    stopper = threading.Thread(target=stop)
    stopper.start()
    try:
        assert not finished.wait(0.05)
        assert rig.module._shader_catalog_finalize_stopping is True
        assert rig.module._shader_catalog_finalize_workers
    finally:
        rig.release_parse.set()
        stopper.join(3.0)
    assert finished.is_set() and not stopper.is_alive() and errors == []
    assert not rig.module._shader_catalog_finalize_workers
    assert rig.wait_finished(job_id)["status"] == "cancelled"
    assert not rig.output.exists()
    rig.assert_unpublished()


def test_shutdown_timeout_keeps_worker_tracked_and_refuses_new_jobs(rig, monkeypatch):
    rig.block_parse = True
    job_id = rig.begin()
    assert rig.parse_entered.wait(2.0)
    monkeypatch.setattr(rig.module, "IPC_WORKER_STOP_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(RuntimeError, match="CPU worker is still stopping"):
        rig.module._stop_shader_catalog_finalize_workers()
    assert rig.module._shader_catalog_finalize_workers
    with pytest.raises(RuntimeError, match="export is stopping"):
        rig.module._begin_export_shader_catalog(rig.params())
    rig.release_parse.set()
    assert rig.wait_finished(job_id)["status"] == "cancelled"
    assert not rig.output.exists()
    rig.assert_unpublished()
