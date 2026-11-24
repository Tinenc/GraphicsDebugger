"""Real Bridge admission/worker/UI functions retain lifecycle and Source guards."""

import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from tests.test_bridge_catalog_rebind import _load_bridge
from tests.test_shader_saved_source_display import saved_source


@pytest.fixture
def dispatch(tmp_path):
    bridge = _load_bridge()
    tree = ast.parse(Path(bridge.__file__).read_text(encoding="utf-8"))
    poller_class = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "JsonPoller")

    class QObject:
        def __init__(self, *unused):
            pass

    bridge.QtCore = SimpleNamespace(QObject=QObject)
    exec(compile(ast.Module(body=[poller_class], type_ignores=[]), bridge.__file__, "exec"), vars(bridge))
    state = {"loaded": True, "path": str(tmp_path / "capture.rdc"), "api": "GraphicsAPI.D3D11"}
    callbacks, getter_calls = [], []

    def invoke(callback):
        callbacks.append(callback.__name__)
        callback()

    helper = SimpleNamespace(InvokeOntoUIThread=invoke)
    replay = SimpleNamespace(GetForegroundInvokeGeneration=lambda: 0)
    bridge._ctx = SimpleNamespace(
        IsCaptureLoaded=lambda: state["loaded"], CurEvent=lambda: 13,
        CurSelectedEvent=lambda: 11, Replay=lambda: replay,
        GetCaptureFilename=lambda: getter_calls.append("path") or state["path"],
        APIProps=lambda: getter_calls.append("api") or SimpleNamespace(pipelineType=state["api"], localRenderer="D3D11"),
        IsCaptureLocal=lambda: True,
        Extensions=lambda: SimpleNamespace(GetMiniQtHelper=lambda: helper))
    bridge._capture_session_id = "a" * 32
    bridge._active_capture_token = ("loaded", os.path.normcase(os.path.abspath(state["path"])))
    bridge._symbol_capture_snapshot = {"captureId": "c" * 64}
    bridge._publish_console_event = mock.Mock()
    bridge._replay = mock.Mock(side_effect=AssertionError("identity/presentation cannot enter replay"))
    bridge.INSTANCE_ID = "saved-source-test"
    poller = bridge.JsonPoller(bridge.handle_request)
    return SimpleNamespace(bridge=bridge, poller=poller, state=state, callbacks=callbacks,
                           getters=getter_calls, helper=helper, root=tmp_path)


def admit(state, method="get_current_renderdoc_context", params=None):
    request = {"id": "d" * 32, "method": method,
               "params": {"identity_only": True} if params is None else params}
    return state.poller._new_item(request["id"], str(state.root / "response.json"), None, request, None)


@pytest.mark.parametrize("method", ["get_current_renderdoc_context", "get_capture_status"])
def test_identity_reuses_real_qt_claim_facts_without_second_ui_hop(dispatch, method):
    item = admit(dispatch, method)
    getters = list(dispatch.getters)
    response = dispatch.poller._execute_item(item)
    assert "error" not in response
    result = response["result"]
    capture = result["capture"] if method == "get_current_renderdoc_context" else result
    assert capture["captureSessionId"] == "a" * 32
    assert capture["api"] == "GraphicsAPI.D3D11"
    assert dispatch.getters == getters
    assert dispatch.callbacks == []
    dispatch.bridge._replay.assert_not_called()


@pytest.mark.parametrize("damage", ["missing", "wrong-shape", "api", "path", "local", "token"])
def test_missing_or_malformed_admission_falls_back_to_fresh_qt_read(dispatch, damage):
    item = admit(dispatch)
    snapshot = item["captureSnapshot"]
    if damage == "missing":
        snapshot.pop("captureIdentity")
    elif damage == "wrong-shape":
        snapshot["captureIdentity"] = []
    elif damage == "token":
        snapshot["captureToken"] = []
    else:
        snapshot["captureIdentity"][damage] = {"api": "", "path": "", "local": "true"}[damage]
    response = dispatch.poller._execute_item(item)
    assert response["result"]["capture"]["path"] == dispatch.state["path"]
    assert dispatch.callbacks == ["_invoke"]


@pytest.mark.parametrize("change", ["close", "reload", "path", "api", "stop", "invalidated"])
def test_capture_lifecycle_change_after_claim_never_serves_admitted_identity(dispatch, change):
    item = admit(dispatch)
    if change == "close":
        dispatch.state["loaded"] = False
        dispatch.bridge._capture_session_id = ""
        dispatch.bridge._active_capture_token = None
    elif change in ("reload", "path", "api"):
        # API belongs to a capture load. Even reopening the same path changes
        # the real session, as CaptureLifecycleViewer.OnCaptureLoaded does.
        dispatch.bridge._capture_session_id = "b" * 32
        if change == "path":
            dispatch.state["path"] = str(dispatch.root / "other.rdc")
            dispatch.bridge._active_capture_token = ("loaded", os.path.normcase(dispatch.state["path"]))
        elif change == "api":
            dispatch.state["api"] = "GraphicsAPI.Vulkan"
    elif change == "stop":
        dispatch.poller._stop_event.set()
    else:
        item["captureInvalidatedEvent"].set()
    response = dispatch.poller._execute_item(item)
    assert "error" in response
    assert dispatch.callbacks == []


def test_selection_preemption_keeps_identity_read_but_capture_invalidation_does_not(dispatch):
    item = admit(dispatch)
    item["cancelEvent"].set()
    item["selectionPreemptedEvent"].set()
    response = dispatch.poller._execute_item(item)
    assert response["result"]["capture"]["captureSessionId"] == "a" * 32
    assert dispatch.callbacks == []


def preview_item(dispatch, saved_source):
    workspace, source, unused_manifest, binding = saved_source
    dispatch.state["path"] = binding["capturePath"]
    dispatch.bridge._capture_session_id = binding["captureSessionId"]
    dispatch.bridge._active_capture_token = ("loaded", os.path.normcase(os.path.abspath(binding["capturePath"])))
    shown = []
    panel = SimpleNamespace(_capture_binding=dispatch.bridge._shader_console_protocol.normalize_capture_binding(binding),
                            show=lambda: None, capture_changed=lambda value: None)

    def show(opened):
        shown.append(opened)
        return {"shown": True, "workspaceId": opened["workspaceId"], "readOnly": True,
                "documentKind": "organization-preview"}

    panel.show_organization_preview = show
    dispatch.bridge._shader_workspace_ui = panel
    return admit(dispatch, "show_shader_organization_preview", {
        "canonical_source": True, "workspace_path": str(workspace),
        "expected_capture_session_id": binding["captureSessionId"],
        "expected_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }), shown


def test_canonical_preview_reuses_initial_binding_but_keeps_final_real_ui_call(dispatch, saved_source):
    item, shown = preview_item(dispatch, saved_source)
    response = dispatch.poller._execute_item(item)
    assert response["result"]["shown"] is True
    assert response["result"]["replayApplied"] is False
    assert len(shown) == 1
    assert dispatch.callbacks == ["_invoke"]
    dispatch.bridge._replay.assert_not_called()


@pytest.mark.parametrize("change", ["close", "path", "session"])
def test_preview_final_ui_guard_still_reads_actual_capture_after_source_load(dispatch, saved_source, change):
    item, shown = preview_item(dispatch, saved_source)

    def changed(callback):
        dispatch.callbacks.append(callback.__name__)
        if change == "close":
            dispatch.state["loaded"] = False
        elif change == "path":
            dispatch.state["path"] = str(dispatch.root / "another.rdc")
        else:
            dispatch.bridge._capture_session_id = "b" * 32
        callback()

    dispatch.helper.InvokeOntoUIThread = changed
    response = dispatch.poller._execute_item(item)
    assert "error" in response
    assert shown == []
    assert dispatch.callbacks == ["_invoke"]


def test_preview_keeps_real_source_read_race_detection_without_initial_ui_hop(dispatch, saved_source):
    item, shown = preview_item(dispatch, saved_source)
    source = saved_source[1]
    console = dispatch.bridge._shader_console_protocol
    read = console._read_file_bytes
    source_reads = 0

    def changed(path, *args):
        nonlocal source_reads
        raw = read(path, *args)
        if os.path.normcase(str(path)) == os.path.normcase(str(source)):
            source_reads += 1
            if source_reads == 1:
                source.write_bytes(b"// changed after first production read\n")
        return raw

    with mock.patch.object(console, "_read_file_bytes", side_effect=changed):
        response = dispatch.poller._execute_item(item)
    assert "changed while it was read" in response["error"]["message"]
    assert shown == []
    assert dispatch.callbacks == []


def test_timing_separates_ui_queue_callback_wall_and_thread_cpu_without_payload(dispatch):
    bridge = dispatch.bridge
    clock = {"wall": 1.0, "cpu": 0.01}
    bridge.time = SimpleNamespace(monotonic=lambda: clock["wall"], thread_time=lambda: clock["cpu"])
    timing = {"uiTransactions": []}
    bridge._request_dispatch_state.current = {"stageTiming": timing, "captureSnapshot": {"captureSessionId": "a" * 32}}

    def queued(callback):
        clock["wall"] = 3.0
        callback()

    dispatch.helper.InvokeOntoUIThread = queued

    def work():
        clock.update(wall=3.5, cpu=0.04)
        return "private Source and credentials remain outside timing"

    try:
        assert bridge._invoke_on_ui_sync(work).startswith("private")
    finally:
        del bridge._request_dispatch_state.current
    assert timing["uiTransactions"] == [{"callback": "work", "outcome": "done",
        "uiQueueWallMs": 2000.0, "callbackWallMs": 500.0, "callbackThreadCpuMs": 30.0}]
    assert "private" not in json.dumps(timing)


def test_worker_queue_publishes_safe_stage_timing_and_keeps_response_schema(dispatch):
    started, release = threading.Event(), threading.Event()
    bridge, poller = dispatch.bridge, dispatch.poller
    bridge.METHODS["hold_for_queue_test"] = lambda params: started.set() or release.wait(1) or {}
    first = admit(dispatch, "hold_for_queue_test", {"providerReasoning": "private", "credential": "secret"})
    first["responsePath"] = str(dispatch.root / "first.json")
    second = admit(dispatch)
    poller._request_queue.put(first)
    worker = threading.Thread(target=poller._worker_main)
    worker.start()
    try:
        assert started.wait(1)
        poller._request_queue.put(second)
        assert not release.wait(0.05)
    finally:
        release.set()
        poller._request_queue.put(poller._WORKER_STOP)
        worker.join(2)
    assert not worker.is_alive()
    response = json.loads(Path(second["responsePath"]).read_text(encoding="utf-8"))
    assert response["id"] == "d" * 32
    assert response["result"]["identityOnly"] is True
    diagnostic = response["bridgeTiming"]
    assert diagnostic["schema"] == "bread-bridge-request-timing/v1"
    assert diagnostic["workerQueueWallMs"] >= 40.0
    assert diagnostic["uiTransactions"] == []
    assert not {"params", "providerReasoning", "credential", "Source"} & diagnostic.keys()
    assert "private" not in json.dumps(diagnostic) and "secret" not in json.dumps(diagnostic)


def test_broken_timing_cannot_change_the_published_tool_result(dispatch):
    item = admit(dispatch)
    item["stageTiming"] = {"broken": True}
    response = {"id": "d" * 32, "result": {"committed": True}}
    dispatch.poller._finish_item(item, response)
    assert json.loads(Path(item["responsePath"]).read_text(encoding="utf-8")) == response


def test_pending_ui_cancellation_timing_cannot_run_late_callback(dispatch):
    bridge = dispatch.bridge
    posted = threading.Event()
    callbacks, writes, outcome = [], [], {}

    def defer(callback):
        callbacks.append(callback)
        posted.set()

    dispatch.helper.InvokeOntoUIThread = defer
    bridge.METHODS["pending_ui_test"] = lambda params: bridge._invoke_on_ui_sync(lambda: writes.append("mutation"))
    item = admit(dispatch, "pending_ui_test", {})
    worker = threading.Thread(target=lambda: outcome.setdefault("response", dispatch.poller._execute_item(item)))
    worker.start()
    assert posted.wait(1)
    item["cancelEvent"].set()
    worker.join(1)
    assert not worker.is_alive()
    assert outcome["response"]["error"]["code"] == -32010
    callbacks[0]()
    assert writes == []
    stage = item["stageTiming"]["uiTransactions"][0]
    assert stage["outcome"] == "cancelled"
    assert stage["callbackWallMs"] == 0
    assert stage["callbackThreadCpuMs"] is None


@pytest.mark.skipif(os.name != "nt", reason="Bundled Python 3.6 uses the native Windows thread clock")
def test_bundled_python36_cpu_clock_uses_actual_read_only_windows_thread_times(dispatch):
    dispatch.bridge.time = SimpleNamespace()
    measured = dispatch.bridge._bridge_thread_cpu_seconds()
    assert isinstance(measured, float) and measured >= 0
