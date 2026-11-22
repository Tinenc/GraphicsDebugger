from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import textwrap
import types

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPOSITORY_ROOT / "replaylab" / "renderdoc_mcp_bridge" / "__init__.py"
REPLAY_MANAGER_CPP = REPOSITORY_ROOT / "qrenderdoc" / "Code" / "ReplayManager.cpp"
REPLAY_MANAGER_HEADER = REPOSITORY_ROOT / "qrenderdoc" / "Code" / "ReplayManager.h"
REPLAY_INTERFACE = (
    REPOSITORY_ROOT / "qrenderdoc" / "Code" / "Interface" / "QRDInterface.h"
)
PYRENDERDOC_INTERFACE = (
    REPOSITORY_ROOT / "qrenderdoc" / "Code" / "pyrenderdoc" / "qrenderdoc.i"
)
AI_ASSISTANT_CPP = (
    REPOSITORY_ROOT / "qrenderdoc" / "Windows" / "AI" / "AIAssistant.cpp"
)
AI_ASSISTANT_HEADER = (
    REPOSITORY_ROOT / "qrenderdoc" / "Windows" / "AI" / "AIAssistant.h"
)


def _load_bridge():
    fake_renderdoc = types.ModuleType("renderdoc")
    previous = sys.modules.get("renderdoc")
    sys.modules["renderdoc"] = fake_renderdoc
    try:
        spec = importlib.util.spec_from_file_location(
            "renderdoc_mcp_background_bridge_contract", BRIDGE_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = previous


def _class_method_source(class_name: str, method_name: str) -> str:
    source = BRIDGE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    class_node = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    method = next(
        node for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    )
    return ast.get_source_segment(source, method) or ""


def _function_source(function_name: str) -> str:
    source = BRIDGE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    )
    return ast.get_source_segment(source, function) or ""


def _standalone_class_method(class_name: str, method_name: str, **globals_):
    namespace = dict(globals_)
    namespace.setdefault("_replay_recovery_path", "")
    exec(textwrap.dedent(_class_method_source(class_name, method_name)), namespace)
    return namespace[method_name]


def _assigned_literal(name: str):
    source = BRIDGE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    assignment = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name
                for target in node.targets)
    )
    value = assignment.value
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"):
        value = value.args[0]
    return ast.literal_eval(value)


def _assigned_string_set(name: str) -> set[str]:
    return set(_assigned_literal(name))


def _text_section(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index + len(start))
    return source[start_index:end_index]


def test_qt_mailbox_callbacks_only_claim_and_submit_background_work() -> None:
    timer = _class_method_source("JsonPoller", "timerEvent")
    queued = _class_method_source("JsonPoller", "_process_queued_request")
    submit = _class_method_source("JsonPoller", "_enqueue_item")
    worker = _class_method_source("JsonPoller", "_worker_main")

    assert "self._handler(" not in timer
    assert "self._handler(" not in queued
    assert "self._submit_claimed_request(" in timer
    assert "self._submit_claimed_request(" in queued
    assert "put_nowait" in submit
    assert "queue.Full" in submit
    assert "self._execute_item(item)" in worker
    assert "except BaseException" in worker

    ui_dispatch = _class_method_source("JsonPoller", "_invoke_handler_on_ui")
    assert "except BaseException" in ui_dispatch
    assert "GetMiniQtHelper().InvokeOntoUIThread" in ui_dispatch

    selected = _class_method_source("CaptureLifecycleViewer", "OnSelectedEventChanged")
    changed = _class_method_source("CaptureLifecycleViewer", "OnEventChanged")
    assert "cancel_active_for_user_activity" in selected
    assert "cancel_active_for_user_activity" in changed


def test_same_eid_viewer_refresh_does_not_cancel_background_work() -> None:
    cancellations = []
    poller = types.SimpleNamespace(
        cancel_active_for_user_activity=lambda: cancellations.append("cancel"))
    globals_ = {
        "_poller": poller,
        "_safe_int": lambda value, default=0: int(value),
        "_publish_instance_manifest": lambda **unused: None,
    }
    selected_changed = _standalone_class_method(
        "CaptureLifecycleViewer", "OnSelectedEventChanged", **globals_)
    visible_changed = _standalone_class_method(
        "CaptureLifecycleViewer", "OnEventChanged", **globals_)
    viewer = types.SimpleNamespace(
        _selected_event_id=11329, _visible_event_id=11329)

    # CaptureContext::RefreshStatus emits both callbacks with force=true even
    # though the selected/current EID did not change. That maintenance refresh
    # must not invalidate queued or active Agent evidence.
    selected_changed(viewer, 11329)
    visible_changed(viewer, 11329)
    assert cancellations == []

    # A genuine selected or effective EID transition is still immediate user
    # activity and independently invalidates the old request snapshot.
    selected_changed(viewer, 11330)
    visible_changed(viewer, 11331)
    assert cancellations == ["cancel", "cancel"]


def test_user_activity_generation_cancels_active_background_work() -> None:
    cancel = _standalone_class_method(
        "JsonPoller", "cancel_active_for_user_activity")
    cancel_event = threading.Event()
    poller = types.SimpleNamespace(
        _active_guard=threading.RLock(),
        _user_activity_generation=7,
        _active_item={"cancelEvent": cancel_event, "selectionPreemptedEvent": threading.Event(),
                      "captureInvalidatedEvent": threading.Event()},
    )

    cancel(poller)

    assert poller._user_activity_generation == 8
    assert cancel_event.is_set()
    assert poller._active_item["selectionPreemptedEvent"].is_set()
    assert not poller._active_item["captureInvalidatedEvent"].is_set()


def test_background_replay_uses_tagged_blocking_lane_off_the_gui_thread() -> None:
    bridge = _load_bridge()
    calls = []

    class Replay:
        def HasPendingForegroundInvoke(self):
            return False

        def BlockInvokeTagged(self, tag, callback):
            calls.append(tag)
            callback(object())

        def BlockInvoke(self, callback):
            raise AssertionError("background requests must use the tagged lane")

    bridge._ctx = types.SimpleNamespace(Replay=lambda: Replay())
    bridge._request_event_state.current = {
        "backgroundReplayTag": "ReplayLab.background.ipc.test",
        "cancelEvent": threading.Event(),
    }
    try:
        assert bridge._replay(lambda unused: 37) == 37
    finally:
        del bridge._request_event_state.current

    assert calls == ["ReplayLab.background.ipc.test"]


def test_persistent_replay_commit_finishes_before_user_preemption() -> None:
    bridge = _load_bridge()
    generation = [11]
    calls = []

    class Replay:
        def HasPendingForegroundInvoke(self):
            return False

        def GetForegroundInvokeGeneration(self):
            return generation[0]

        def BlockInvokeTagged(self, unused_tag, callback):
            callback(object())

    class MiniQt:
        def InvokeOntoUIThread(self, callback):
            callback()

    class Extensions:
        def GetMiniQtHelper(self):
            return MiniQt()

    replay = Replay()
    bridge._ctx = types.SimpleNamespace(
        Replay=lambda: replay, Extensions=lambda: Extensions())

    cancel_event = threading.Event()
    tracker = {
        "backgroundReplayTag": "ReplayLab.background.ipc.atomic",
        "cancelEvent": cancel_event,
        "foregroundReplayGeneration": 11,
        "eventDirty": False,
        "replayAtomicTransaction": False,
        "replayAtomicCommitStarted": False,
    }
    bridge._request_event_state.current = tracker
    bridge._request_dispatch_state.current = {"cancelEvent": cancel_event}
    try:
        def commit_then_rollback(unused_ctrl):
            bridge._begin_replay_atomic_transaction()
            calls.append("persistent-setter")
            cancel_event.set()
            generation[0] = 12
            bridge._background_replay_checkpoint()
            calls.append("rollback-or-finish")

        bridge._replay(commit_then_rollback)
        bridge._invoke_on_ui_sync(lambda: calls.append("ui-commit"))
    finally:
        del bridge._request_dispatch_state.current
        del bridge._request_event_state.current

    assert calls == ["persistent-setter", "rollback-or-finish", "ui-commit"]
    assert tracker["replayAtomicTransaction"] is False
    assert tracker["replayAtomicCommitStarted"] is True

    # A read-only callback retains the normal cancellation boundary.
    generation[0] = 20
    read_tracker = {
        "backgroundReplayTag": "ReplayLab.background.ipc.read",
        "cancelEvent": threading.Event(),
        "foregroundReplayGeneration": 20,
        "eventDirty": False,
    }
    bridge._request_event_state.current = read_tracker
    try:
        def stale_read(unused_ctrl):
            generation[0] = 21
            bridge._background_replay_checkpoint()

        with pytest.raises(
                bridge._ReplayLabBackgroundPreempted,
                match="user replay operation"):
            bridge._replay(stale_read)
    finally:
        del bridge._request_event_state.current

    mutation_functions = (
        "_release_event_shader_overrides",
        "_release_event_cbuffer_snapshots",
        "_replace_shared_shader_resource",
        "_remove_shared_shader_resource",
        "_apply_shader_candidate_overrides",
        "_rollback_shader_candidate_overrides",
        "_apply_event_cbuffer_override",
        "_rollback_event_cbuffer_override",
        "_clear_all_event_cbuffer_snapshots",
    )
    for function_name in mutation_functions:
        assert "_begin_replay_atomic_transaction" in _function_source(function_name)


def test_worker_handlers_snapshot_capture_context_on_the_ui_thread() -> None:
    gui_methods = _assigned_string_set("IPC_GUI_THREAD_METHODS")
    assert {
        "load_pass_classification",
        "close_pass_panel",
        "open_capture",
        "begin_export_shader_catalog",
        "consume_export_shader_catalog",
        "begin_rdc_project_shader_resource_index",
        "poll_rdc_project_shader_resource_index",
        "commit_rdc_project_shader_resource_index",
    }.issubset(gui_methods)

    for function_name in (
            "_build_current_renderdoc_context",
            "_ui_resource_name",
            "_read_rdc_project_index",
            "_write_rdc_project_index",
            "_export_shader_catalog",
            "_load_symbol_aliases",
            "_rollback_symbol_aliases"):
        assert "_invoke_on_ui_sync" in _function_source(function_name)

    replay = _function_source("_replay")
    assert "_foreground_replay_manager()" in replay
    assert "_ctx.Replay().BlockInvoke" not in replay

    # Continuation slices run on RenderDoc's replay thread, so they must retain
    # the UI-captured manager instead of touching ICaptureContext there.
    project_queue = _function_source("_queue_project_shader_resource_index_slice")
    assert 'job.get("replayManager")' in project_queue
    assert "_ctx." not in project_queue


def test_background_replay_and_shader_catalog_have_no_foreground_lane_fallback() -> None:
    bridge = _load_bridge()
    foreground_calls = []

    class LegacyReplay:
        def BlockInvoke(self, callback):
            foreground_calls.append(callback)

    bridge._ctx = types.SimpleNamespace(Replay=lambda: LegacyReplay())
    bridge._request_event_state.current = {
        "backgroundReplayTag": "ReplayLab.background.ipc.no-fallback",
        "cancelEvent": threading.Event(),
    }
    try:
        with pytest.raises(RuntimeError, match="does not support background"):
            bridge._replay(lambda unused: 1)
    finally:
        del bridge._request_event_state.current

    assert foreground_calls == []

    shader_catalog = _function_source("_begin_export_shader_catalog")
    assert "callback, SHADER_CATALOG_REPLAY_TAG_PREFIX + job_id" in shader_catalog
    assert "except TypeError" not in shader_catalog


def test_pending_user_replay_work_preempts_background_analysis() -> None:
    bridge = _load_bridge()
    executed = []

    class Replay:
        def HasPendingForegroundInvoke(self):
            return True

        def BlockInvokeTagged(self, unused_tag, callback):
            callback(object())

    tracker = {
        "backgroundReplayTag": "ReplayLab.background.ipc.test",
        "cancelEvent": threading.Event(),
        "userPreempted": False,
        "backgroundAbandoned": False,
    }
    bridge._ctx = types.SimpleNamespace(Replay=lambda: Replay())
    bridge._request_event_state.current = tracker
    try:
        with pytest.raises(bridge._ReplayLabBackgroundPreempted, match="user replay"):
            bridge._replay(lambda unused: executed.append(True))
    finally:
        del bridge._request_event_state.current

    assert executed == []
    assert tracker["userPreempted"] is True
    assert tracker["backgroundAbandoned"] is True


def test_completed_foreground_enqueue_generation_preempts_stale_background_work() -> None:
    bridge = _load_bridge()
    executed = []

    class Replay:
        def HasPendingForegroundInvoke(self):
            # The foreground callback already left the queue. Queue inspection
            # alone cannot detect it, so the enqueue generation must remain
            # authoritative for the lifetime of this background request.
            return False

        def GetForegroundInvokeGeneration(self):
            return 12

        def BlockInvokeTagged(self, unused_tag, callback):
            callback(object())

    tracker = {
        "backgroundReplayTag": "ReplayLab.background.ipc.test",
        "cancelEvent": threading.Event(),
        "foregroundReplayGeneration": 11,
        "userPreempted": False,
        "backgroundAbandoned": False,
    }
    bridge._ctx = types.SimpleNamespace(Replay=lambda: Replay())
    bridge._request_event_state.current = tracker
    try:
        with pytest.raises(bridge._ReplayLabBackgroundPreempted, match="user replay"):
            bridge._replay(lambda unused: executed.append(True))
    finally:
        del bridge._request_event_state.current

    assert executed == []
    assert tracker["userPreempted"] is True
    assert tracker["backgroundAbandoned"] is True


def test_foreground_enqueue_during_atomic_replay_does_not_cancel_committed_result() -> None:
    bridge = _load_bridge()
    generation = [21]
    execution_order = []

    class Replay:
        def HasPendingForegroundInvoke(self):
            return False

        def GetForegroundInvokeGeneration(self):
            return generation[0]

        def BlockInvokeTagged(self, unused_tag, callback):
            callback(object())
            # Model ReplayManager selecting the newly queued foreground callback
            # only after the atomic background callback has returned.
            execution_order.append("foreground")

    bridge._ctx = types.SimpleNamespace(Replay=lambda: Replay())
    bridge._capture_session_id = "atomic-session"
    bridge._publish_console_event = lambda *unused_args, **unused_kwargs: None

    def atomic_mutation(unused_params):
        def commit(unused_ctrl):
            bridge._begin_replay_atomic_transaction()
            execution_order.append("commit")
            generation[0] += 1
            return {"committed": True}

        return bridge._replay(commit)

    bridge.METHODS["atomic_generation_contract"] = atomic_mutation
    bridge._request_dispatch_state.current = {
        "backgroundReplayTag": "ReplayLab.background.ipc.atomic-generation",
        "cancelEvent": threading.Event(),
        "foregroundReplayGeneration": 21,
        "captureSnapshot": {
            "captureSessionId": "atomic-session",
            "visibleEventId": 41,
        },
    }
    try:
        response = bridge.handle_request({
            "id": "atomic-generation",
            "method": "atomic_generation_contract",
            "params": {},
        })
    finally:
        del bridge._request_dispatch_state.current

    assert response == {
        "id": "atomic-generation", "result": {"committed": True}}
    assert execution_order == ["commit", "foreground"]


def test_foreground_generation_after_unsliceable_read_discards_stale_result() -> None:
    bridge = _load_bridge()
    generation = [31]
    executed = []

    class Replay:
        def HasPendingForegroundInvoke(self):
            return False

        def GetForegroundInvokeGeneration(self):
            return generation[0]

        def BlockInvokeTagged(self, unused_tag, callback):
            callback(object())

    tracker = {
        "backgroundReplayTag": "ReplayLab.background.ipc.unsliceable-read",
        "cancelEvent": threading.Event(),
        "foregroundReplayGeneration": 31,
        "userPreempted": False,
        "backgroundAbandoned": False,
        "eventDirty": False,
    }
    bridge._ctx = types.SimpleNamespace(Replay=lambda: Replay())
    bridge._request_event_state.current = tracker
    try:
        def unsliceable_read(unused_ctrl):
            executed.append("native-read-finished")
            generation[0] += 1
            return {"stale": True}

        with pytest.raises(
                bridge._ReplayLabBackgroundPreempted,
                match="user replay operation"):
            bridge._replay(unsliceable_read)
    finally:
        del bridge._request_event_state.current

    assert executed == ["native-read-finished"]
    assert tracker["userPreempted"] is True
    assert tracker["backgroundAbandoned"] is True


def test_resource_export_preemption_cleans_provisional_bundle_and_restores_event(
    monkeypatch,
) -> None:
    bridge = _load_bridge()
    request = {
        "eventId": 42,
        "captureSessionId": "b" * 32,
    }
    state = {"bundle": {"boundShaderStages": []}}
    cleaned = []
    replay_calls = []

    protocol = types.SimpleNamespace(
        validate_capture_request=lambda params, capture_id, session_id: request,
        capture_controller=lambda ctrl, rd, value, stages: state,
        attach_raw_evidence=lambda *args: None,
        cleanup_capture=lambda value: cleaned.append(value),
        finalize_capture=lambda *args: pytest.fail(
            "a preempted export must not be finalized"
        ),
    )
    monkeypatch.setattr(bridge, "_resource_export_protocol", protocol)
    monkeypatch.setattr(
        bridge,
        "_resource_export_capture_binding",
        lambda params: ("a" * 64, "b" * 32),
    )
    monkeypatch.setattr(bridge, "_available_shader_stages", lambda: [])
    monkeypatch.setattr(bridge, "_request_visible_event_id", lambda default: 7)
    monkeypatch.setattr(bridge, "_resource_export_shader_evidence", lambda *args: {})
    monkeypatch.setattr(bridge, "_capture_session_id", "b" * 32)
    monkeypatch.setattr(bridge, "_set_frame_event", lambda ctrl, eid, force: None)

    def replay(callback):
        replay_calls.append(callback)
        if len(replay_calls) == 1:
            return callback(object())
        if len(replay_calls) == 2:
            raise bridge._ReplayLabBackgroundPreempted(
                "user replay superseded resource evidence capture"
            )
        return callback(object())

    monkeypatch.setattr(bridge, "_replay", replay)

    with pytest.raises(bridge._ReplayLabBackgroundPreempted, match="superseded"):
        bridge._capture_resource_export_bundle({})

    assert cleaned == [state]
    # Capture, raw-evidence attempt, then the finally-path event restoration.
    assert len(replay_calls) == 3


def test_cancelled_shader_refresh_hint_does_not_reverse_committed_mutation() -> None:
    bridge = _load_bridge()
    committed = []

    class Replay:
        def HasPendingForegroundInvoke(self):
            return False

        def GetForegroundInvokeGeneration(self):
            return 4

        def BlockInvokeTagged(self, unused_tag, callback):
            callback(object())

    tracker = {
        "backgroundReplayTag": "ReplayLab.background.ipc.shader-commit",
        "cancelEvent": threading.Event(),
        "foregroundReplayGeneration": 4,
        "visibleEventId": 1,
        "eventDirty": False,
    }
    bridge._ctx = types.SimpleNamespace(Replay=lambda: Replay())
    bridge._request_event_state.current = tracker
    try:
        result = bridge._replay(
            lambda unused_ctrl: committed.append("shader") or {"committed": True})

        def cancelled_ui_hint(unused_callback):
            raise bridge._ReplayLabBackgroundPreempted(
                "user activity superseded the refresh hint")

        bridge._invoke_on_ui_sync = cancelled_ui_hint
        assert bridge._notify_shader_replay_changed() is None
    finally:
        del bridge._request_event_state.current

    assert result == {"committed": True}
    assert committed == ["shader"]


def test_background_replay_restores_temporary_eid_inside_the_same_callback() -> None:
    bridge = _load_bridge()
    replay_callbacks = []
    set_events = []

    class Controller:
        def SetFrameEvent(self, event_id, force):
            set_events.append((event_id, force))

    controller = Controller()

    class Replay:
        def HasPendingForegroundInvoke(self):
            return False

        def GetForegroundInvokeGeneration(self):
            return 7

        def BlockInvokeTagged(self, tag, callback):
            replay_callbacks.append(tag)
            callback(controller)

        def BlockInvoke(self, callback):
            raise AssertionError("background requests must use one tagged callback")

    tracker = {
        "backgroundReplayTag": "ReplayLab.background.ipc.restore-test",
        "cancelEvent": threading.Event(),
        "foregroundReplayGeneration": 7,
        "visibleEventId": 41,
        "lastEventId": 41,
        "lastSetSucceeded": True,
        "mutated": False,
        "eventDirty": False,
        "userPreempted": False,
        "backgroundAbandoned": False,
    }
    bridge._ctx = types.SimpleNamespace(Replay=lambda: Replay())
    bridge._request_event_state.current = tracker
    try:
        result = bridge._replay(
            lambda ctrl: (
                bridge._set_frame_event(ctrl, 99, True), "complete")[1])
    finally:
        del bridge._request_event_state.current

    assert result == "complete"
    assert replay_callbacks == ["ReplayLab.background.ipc.restore-test"]
    assert set_events == [(99, True), (41, True)]
    assert tracker["lastEventId"] == 41
    assert tracker["lastSetSucceeded"] is True
    assert tracker["eventDirty"] is False


def test_native_fatal_controller_cannot_report_successful_event_selection() -> None:
    bridge = _load_bridge()
    bridge.rd.ResultCode = types.SimpleNamespace(Succeeded=0)
    bridge._background_replay_checkpoint = lambda: None
    for already_failed in (True, False):
        calls = []
        class Controller:
            def GetFatalErrorStatus(self):
                return types.SimpleNamespace(code=1 if already_failed or calls else 0)
            def SetFrameEvent(self, event, force):
                calls.append(event)
        tracker = {"visibleEventId": 41, "lastSetSucceeded": False}
        bridge._request_event_state.current = tracker
        try:
            with pytest.raises(RuntimeError, match="bread-replay-unavailable"):
                bridge._set_frame_event(Controller(), 99)
            assert tracker["lastSetSucceeded"] is False
            assert calls == ([] if already_failed else [99])
        finally:
            del bridge._request_event_state.current


def test_current_context_freeze_barrier_owns_fixed_eid_and_restores_it() -> None:
    bridge = _load_bridge()
    set_events = []
    bridge._capture_session_id = "session-a"
    bridge._active_capture_token = "capture-a"
    bridge._foreground_replay_generation = lambda: (_ for _ in ()).throw(
        AssertionError("the fixed-EID queue barrier must not inspect user generation")
    )
    bridge._set_frame_event = (
        lambda unused_ctrl, event_id, force:
        set_events.append((event_id, force)))
    bridge._write_current_context_freeze_terminal = lambda unused_job: None
    bridge._finish_current_renderdoc_context = lambda facts: {"facts": facts}

    def job(capture_token="capture-a"):
        return {
            "status": "queued",
            "facts": {"warnings": []},
            "compatibleStages": (),
            "aliases": {},
            "captureSessionId": "session-a",
            "captureToken": capture_token,
            "currentEventId": 77,
            "restoreEventId": 41,
        }

    # ReplayManager serialises this foreground barrier ahead of later user
    # navigation. The callback therefore owns its temporary fixed-EID mutation
    # and restores the submit-time visible EID before the next replay callback.
    current = job()
    bridge._current_context_freeze_jobs = {"current": current}
    bridge._run_current_context_freeze_job("current", object())
    assert current["status"] == "completed"
    assert set_events == [(77, False), (41, True)]

    # Capture identity remains the hard stale boundary. A job from another
    # capture must fail before touching replay even though generation is no
    # longer (incorrectly) used as a queue-barrier freshness test.
    set_events.clear()
    stale_capture = job(capture_token="capture-b")
    bridge._current_context_freeze_jobs = {"stale-capture": stale_capture}
    bridge._run_current_context_freeze_job("stale-capture", object())
    assert stale_capture["status"] == "failed"
    assert "capture changed" in stale_capture["error"]
    assert set_events == []


@pytest.mark.parametrize("rollback_confirmed", [True, False])
def test_chat_context_waits_for_its_project_restore_rollback(tmp_path, monkeypatch, rollback_confirmed):
    bridge = _load_bridge()
    session = "a" * 32
    timers, queued, cancelled = [], [], []
    restore = types.SimpleNamespace(cancel_for_inspection=lambda: cancelled.append(True),
                                    replay_inspection_safe=False)
    bridge._project_auto_restore_client = restore
    bridge.QtCore = types.SimpleNamespace(QTimer=types.SimpleNamespace(
        singleShot=lambda _, callback: timers.append(callback)))
    bridge._ctx = types.SimpleNamespace(Replay=lambda: types.SimpleNamespace(
        AsyncInvoke=lambda method, tag: queued.append((method, tag))))
    bridge._capture_current_renderdoc_context_facts = lambda **_: (
        {"capture": {"loaded": True, "captureSessionId": session}, "events": {"currentEventId": 77}},
        (), ("loaded", "test"), 77)
    bridge._current_context_freeze_result_path = lambda job_id: str(tmp_path / (job_id + ".json"))
    bridge._write_current_context_freeze_terminal = lambda _: None
    monkeypatch.setattr(bridge.threading, "Timer", lambda *args: types.SimpleNamespace(start=lambda: None))

    # Project Host context queries use this same primitive without the chat flag.
    bridge._begin_current_context_freeze(expected_capture_session_id=session)
    assert len(queued) == 1 and not cancelled
    assert callable(queued[0][0])
    assert queued[0][1].startswith(bridge.CURRENT_CONTEXT_FREEZE_REPLAY_TAG_PREFIX)
    queued.clear()
    job = json.loads(bridge.begin_native_context_freeze_json(expected_capture_session_id=session))
    assert job["status"] == "queued" and len(cancelled) == 1 and not queued
    timers.pop()()
    assert not queued
    restore.replay_inspection_safe = rollback_confirmed
    bridge._project_auto_restore_client = None
    timers.pop()()
    if rollback_confirmed:
        assert len(queued) == 1
    else:
        assert not queued
        assert "rollback could not be verified" in bridge._poll_current_context_freeze(job["jobId"])["error"]


def test_ipc_current_context_reuses_native_fixed_eid_barrier() -> None:
    bridge = _load_bridge()
    capture_session_id = "a" * 32
    expected_context = {
        "schema": "renderdoc-ultra-current-context/v1",
        "snapshotId": "b" * 64,
    }
    begin_arguments = {}

    def begin(**kwargs):
        begin_arguments.update(kwargs)
        return {"jobId": "c" * 32, "status": "queued"}

    bridge._begin_current_context_freeze = begin
    bridge._poll_current_context_freeze = lambda unused_job_id: {
        "jobId": "c" * 32,
        "status": "completed",
        "context": expected_context,
    }
    bridge._invoke_on_ui_sync = lambda callback: callback()
    # This fixture tests fixed-EID ordering; cancellation has dedicated tests.
    bridge._request_dispatch_cancelled = lambda dispatch: False
    bridge._build_current_renderdoc_context = lambda: (_ for _ in ()).throw(
        AssertionError("IPC context must use the native fixed-EID barrier"))
    bridge._current_bound_shader_context = lambda *unused: (_ for _ in ()).throw(
        AssertionError("IPC context must not use the background replay lane"))

    bridge._request_dispatch_state.current = {
        "cancelEvent": threading.Event(),
        "captureSnapshot": {
            "captureSessionId": capture_session_id,
            "visibleEventId": 11329,
            "selectedEventId": 11328,
        },
    }
    try:
        result = bridge._get_current_renderdoc_context({})
    finally:
        del bridge._request_dispatch_state.current

    assert result is expected_context
    assert begin_arguments == {
        "expected_current_event_id": "11329",
        "expected_selected_event_id": "11328",
        "expected_capture_session_id": capture_session_id,
    }


def test_cancelled_pending_ui_callback_cannot_run_after_worker_returns() -> None:
    scheduled = threading.Event()
    callbacks = []
    handler_calls = []

    class MiniQt:
        def InvokeOntoUIThread(self, callback):
            callbacks.append(callback)
            scheduled.set()

    class Extensions:
        def GetMiniQtHelper(self):
            return MiniQt()

    invoke_handler = _standalone_class_method(
        "JsonPoller", "_invoke_handler_on_ui", threading=threading,
        _request_preemption_message=lambda dispatch, fallback, **kwargs: fallback,
        _ctx=types.SimpleNamespace(Extensions=lambda: Extensions()))

    poller = types.SimpleNamespace(
        _handler=lambda request: handler_calls.append(request),
        _cancelled_response=lambda request_id, message: {
            "id": request_id,
            "error": {"code": -32010, "message": message},
        },
    )
    item = {
        "requestId": "ui-pending",
        "request": {"id": "ui-pending", "method": "open_capture"},
        "cancelEvent": threading.Event(),
    }
    outcome = {}
    worker = threading.Thread(
        target=lambda: outcome.setdefault(
            "response", invoke_handler(poller, item)))
    worker.start()
    assert scheduled.wait(1.0)

    item["cancelEvent"].set()
    worker.join(1.0)
    assert not worker.is_alive()
    assert outcome["response"]["error"]["code"] == -32010

    # Simulate Qt eventually delivering the already-posted closure. Its phase
    # is cancelled, so it must never mutate UI state after response/claim finalisation.
    callbacks[0]()
    assert handler_calls == []


def test_running_ui_callback_finishes_before_cancellation_is_published() -> None:
    handler_started = threading.Event()
    release_handler = threading.Event()
    worker_done = threading.Event()
    ui_threads = []

    class MiniQt:
        def InvokeOntoUIThread(self, callback):
            ui_thread = threading.Thread(target=callback)
            ui_threads.append(ui_thread)
            ui_thread.start()

    class Extensions:
        def GetMiniQtHelper(self):
            return MiniQt()

    def handler(request):
        handler_started.set()
        assert release_handler.wait(1.0)
        return {"id": request["id"], "result": "committed"}

    invoke_handler = _standalone_class_method(
        "JsonPoller", "_invoke_handler_on_ui", threading=threading,
        _ctx=types.SimpleNamespace(Extensions=lambda: Extensions()))
    poller = types.SimpleNamespace(
        _handler=handler,
        _cancelled_response=lambda request_id, message: {
            "id": request_id,
            "error": {"code": -32010, "message": message},
        },
    )
    item = {
        "requestId": "ui-running",
        "request": {"id": "ui-running", "method": "open_capture"},
        "cancelEvent": threading.Event(),
    }
    outcome = {}

    def run_worker():
        outcome["response"] = invoke_handler(poller, item)
        worker_done.set()

    worker = threading.Thread(target=run_worker)
    worker.start()
    assert handler_started.wait(1.0)
    item["cancelEvent"].set()
    assert not worker_done.wait(0.1)

    release_handler.set()
    worker.join(1.0)
    ui_threads[0].join(1.0)
    assert not worker.is_alive()
    assert outcome["response"] == {"id": "ui-running", "result": "committed"}


def test_symbol_alias_operations_are_not_whole_gui_thread_handlers() -> None:
    gui_methods = _assigned_string_set("IPC_GUI_THREAD_METHODS")
    assert "load_symbol_aliases" not in gui_methods
    assert "rollback_symbol_aliases" not in gui_methods

    load_aliases = _function_source("_load_symbol_aliases")
    assert "_invoke_on_ui_sync" in load_aliases


def test_failed_response_publication_retains_the_durable_claim(tmp_path: Path) -> None:
    writes = []

    def fail_write(path, response):
        writes.append((path, response))
        raise OSError("response volume unavailable")

    finish_item = _standalone_class_method(
        "JsonPoller", "_finish_item", os=os,
        _atomic_write_json=fail_write,
        traceback=types.SimpleNamespace(print_exc=lambda: None))
    claim = tmp_path / "request.claim"
    claim.write_text("claimed", encoding="utf-8")
    response_path = tmp_path / "response.json"

    finish_item(object(), {
        "requestId": "publish-failure",
        "responsePath": str(response_path),
        "claimPath": str(claim),
    }, {"id": "publish-failure", "result": {"committed": True}})

    assert len(writes) == 2
    assert claim.exists()
    assert not response_path.exists()


def test_replay_manager_prioritises_foreground_but_only_user_eid_changes_preempt() -> None:
    cpp = REPLAY_MANAGER_CPP.read_text(encoding="utf-8")
    header = REPLAY_MANAGER_HEADER.read_text(encoding="utf-8")
    interface = REPLAY_INTERFACE.read_text(encoding="utf-8")
    swig = PYRENDERDOC_INTERFACE.read_text(encoding="utf-8")
    capture_context = (
        REPOSITORY_ROOT / "qrenderdoc" / "Code" / "CaptureContext.cpp"
    ).read_text(encoding="utf-8")

    current_context_tag = _assigned_literal(
        "CURRENT_CONTEXT_FREEZE_REPLAY_TAG_PREFIX")
    # Current-context snapshots are foreground replay work.  Keep their tag
    # outside the background prefix so ReplayManager does not demote them
    # behind long-running AI/background operations.
    assert current_context_tag == "ReplayLab.current-context-freeze."
    assert not current_context_tag.startswith("ReplayLab.background.")
    assert "IsReplayLabBackgroundInvoke" in cpp
    assert 'const QString userReplayContextTag = lit("replaySetEvent");' in cpp
    assert "IsUserReplayContextInvoke" in cpp
    assert "m_RenderQueue.insert(insertionIndex, cmd)" in cpp
    push_invoke = cpp.split("void ReplayManager::PushInvoke", 1)[1].split(
        "void ReplayManager::run", 1)[0]
    priority_branch = push_invoke[push_invoke.index(
        "if(IsReplayLabBackgroundInvoke(cmd->tag))"):]
    background_branch, foreground_branch = priority_branch.split("else", 1)
    assert "m_ForegroundInvokeGeneration++" not in background_branch
    assert "m_ForegroundInvokeGeneration++" in foreground_branch
    assert "if(IsUserReplayContextInvoke(cmd->tag))" in foreground_branch
    pending = cpp.split(
        "bool ReplayManager::HasPendingForegroundInvoke()", 1)[1].split(
        "uint64_t ReplayManager::GetForegroundInvokeGeneration()", 1)[0]
    assert "IsUserReplayContextInvoke(cmd->tag)" in pending
    assert "!IsReplayLabBackgroundInvoke(cmd->tag)" not in pending
    assert '? lit("replaySetEvent")' in capture_context
    assert ': lit("replayRefreshEvent")' in capture_context
    assert "bool ReplayManager::HasPendingForegroundInvoke()" in cpp
    assert "uint64_t ReplayManager::GetForegroundInvokeGeneration()" in cpp
    assert "void ReplayManager::BlockInvokeTagged" in cpp
    assert "bool HasPendingForegroundInvoke();" in header
    assert "uint64_t GetForegroundInvokeGeneration();" in header
    assert "uint64_t m_ForegroundInvokeGeneration = 0;" in header
    assert "void BlockInvokeTagged" in header
    assert "virtual bool HasPendingForegroundInvoke() = 0;" in interface
    assert "virtual uint64_t GetForegroundInvokeGeneration() = 0;" in interface
    assert "virtual void BlockInvokeTagged" in interface
    assert "%ignore IReplayManager::BlockInvokeTagged;" in swig
    assert "$self->BlockInvokeTagged(tag, m);" in swig
    assert "Py_BEGIN_ALLOW_THREADS" in swig


def test_current_context_barrier_does_not_invalidate_its_own_enqueue() -> None:
    bridge_source = BRIDGE_PATH.read_text(encoding="utf-8")
    replay_manager = REPLAY_MANAGER_CPP.read_text(encoding="utf-8")
    current_context_tag = _assigned_literal(
        "CURRENT_CONTEXT_FREEZE_REPLAY_TAG_PREFIX")
    run_job = _function_source("_run_current_context_freeze_job")
    begin_job = _function_source("_begin_current_context_freeze")

    # The real ExtensionManager SWIG object exposes the UI hop through its
    # IMiniQtHelper child. Keep both worker-to-UI call sites on that shape.
    assert ".Extensions().InvokeOntoUIThread" not in bridge_source
    assert bridge_source.count(
        ".Extensions().GetMiniQtHelper().InvokeOntoUIThread") == 2

    # The fixed-EID callback is a FIFO ordering barrier, not a user replay
    # action. Its own PushInvoke must not increment the user generation, and
    # Python must not re-check that generation after the callback was queued.
    push_invoke = replay_manager.split(
        "void ReplayManager::PushInvoke", 1)[1].split(
            "void ReplayManager::run", 1)[0]
    assert '"ReplayLab.current-context-freeze."' in bridge_source
    assert "if(IsUserReplayContextInvoke(cmd->tag))" in push_invoke
    assert current_context_tag != "replaySetEvent"
    assert "foregroundReplayGeneration" not in run_job
    assert "foregroundReplayGeneration" not in begin_job


def test_current_context_freeze_terminal_event_is_stable_bounded_and_idempotent() -> None:
    bridge = _load_bridge()
    published = []
    learned = []
    bridge._current_context_freeze_console_message = (
        lambda status: "freeze-%s" % status)
    bridge._publish_console_event = (
        lambda module, severity, message, phase="", code="", context=None:
        published.append({
            "module": module,
            "severity": severity,
            "message": message,
            "phase": phase,
            "code": code,
            "context": context,
        }))
    bridge._learning_observation_writer = lambda **kwargs: learned.append(kwargs)
    failed = {
        "jobId": "a" * 32,
        "status": "failed",
        "captureSessionId": "b" * 32,
        "currentEventId": 77,
        "failureCode": "replay-callback-failed",
        "error": "driver failure " + "x" * 4096,
        "consoleEventPublished": False,
    }

    bridge._publish_current_context_freeze_console_event(failed)
    bridge._publish_current_context_freeze_console_event(failed)

    assert len(published) == 1
    event = published[0]
    assert event["code"] == "currentContextFreezeFailed"
    assert event["context"]["operation"] == "currentContextFreeze"
    assert event["context"]["jobId"] == "a" * 32
    assert event["context"]["failureCode"] == "replay-callback-failed"
    assert len(event["context"]["diagnostics"]) == 1024
    assert "driver failure" not in event["message"]
    assert len(learned) == 1
    assert learned[0]["phase"] == "context-freeze"
    assert learned[0]["exception_type"] == "replay-callback-failed"
    assert learned[0]["task_id"] == "a" * 32
    assert learned[0]["terminal_status"] == "failed"
    assert len(learned[0]["message"]) == 1024

    # A failure before Begin can create a durable job envelope is still
    # correlated by a generated jobId. The raw exception remains diagnostic
    # text and the public call preserves the original failure.
    preflight = []
    bridge._begin_current_context_freeze = (
        lambda *unused_args, **unused_kwargs:
        (_ for _ in ()).throw(RuntimeError("preflight exploded")))
    bridge._publish_current_context_freeze_console_event = preflight.append
    with pytest.raises(RuntimeError, match="preflight exploded"):
        bridge.begin_native_context_freeze_json(
            expected_current_event_id="77",
            expected_capture_session_id="b" * 32)
    assert len(preflight) == 1
    assert len(preflight[0]["jobId"]) == 32
    assert preflight[0]["failureCode"] == "begin-failed"
    assert "preflight exploded" in preflight[0]["error"]


def test_abandon_current_context_freeze_does_not_rewrite_a_terminal_job() -> None:
    bridge = _load_bridge()
    completed = {
        "jobId": "c" * 32,
        "status": "completed",
        "captureSessionId": "d" * 32,
        "currentEventId": 77,
        "selectedEventId": 77,
        "resultPath": "C:/fixed/result.json",
        "context": {"capture": {"loaded": True}},
        "error": "",
        "updatedMonotonic": bridge.time.monotonic(),
        "finishedMonotonic": bridge.time.monotonic(),
    }
    bridge._current_context_freeze_jobs = {completed["jobId"]: completed}

    snapshot = bridge._abandon_current_context_freeze(completed["jobId"])

    assert snapshot["status"] == "completed"
    assert snapshot["context"] == completed["context"]
    assert completed["status"] == "completed"


def test_native_ai_background_capacity_and_gui_polling_contract() -> None:
    cpp = AI_ASSISTANT_CPP.read_text(encoding="utf-8")
    header = AI_ASSISTANT_HEADER.read_text(encoding="utf-8")

    # Configuration/probe work must not reintroduce a nested GUI-thread process
    # wait under a helper name.
    assert "runCLISync" not in cpp
    assert "runCLIInputSync" not in cpp
    assert "runCLISync" not in header
    assert "runCLIInputSync" not in header

    configure = _text_section(
        cpp, "void AIAssistant::configureProcess(",
        "bool AIAssistant::attachKillOnCloseJob(")
    assert "setCreateProcessArgumentsModifier" in configure
    assert "BELOW_NORMAL_PRIORITY_CLASS" in configure
    assert "0x00004000UL" in configure
    assert "CREATE_SUSPENDED" in configure
    assert "0x00000004UL" in configure

    decode_pool = _text_section(
        cpp, "QThreadPool *aiBackgroundThreadPool()", "bool mimeDataHasAttachmentCandidate(")
    assert "new QThreadPool" in decode_pool
    assert "setMaxThreadCount(1)" in decode_pool
    assert "QThread::LowPriority" in cpp
    assert "QThreadPool::globalInstance()->start" not in cpp
    assert cpp.count("aiBackgroundThreadPool()->start") >= 3

    concurrency = _text_section(
        cpp, "int maxConcurrentAIChats()", "QString clarificationOriginSha256(")
    assert "QThread::idealThreadCount()" in concurrency
    assert "logicalProcessors >= 8 ? 2 : 1" in concurrency
    launch = _text_section(
        cpp, "void AIAssistant::startNextChat()", "void AIAssistant::launchChat(")
    assert "maxConcurrentAIChats() - m_ChatExecutions.size()" in launch
    assert "availableSlots > 0" in launch

    polling = _text_section(
        cpp, "void AIAssistant::pollTaskEvents()", "qint64 AIAssistant::pollTaskEvent(")
    assert "std::sort(tasks.begin(), tasks.end())" in polling
    assert "m_TaskEventPollCursor" in polling
    assert "remainingBytes = MaxEventReadBytes" in polling
    assert "pollTaskEvent(taskId, remainingBytes)" in polling


def test_native_cli_starts_only_after_job_containment_and_releases_in_order() -> None:
    cpp = AI_ASSISTANT_CPP.read_text(encoding="utf-8")
    header = AI_ASSISTANT_HEADER.read_text(encoding="utf-8")

    containment = _text_section(
        cpp, "bool AIAssistant::attachKillOnCloseJob(",
        "bool AIAssistant::resumeSuspendedProcess(")
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in containment
    assert "AssignProcessToJobObject" in containment

    resume = _text_section(
        cpp, "bool AIAssistant::resumeSuspendedProcess(",
        "void AIAssistant::closeKillOnCloseJob(")
    assert "PROCESS_INFORMATION" in resume
    assert "ResumeThread(information->hThread)" in resume

    async_cli = _text_section(
        cpp, "void AIAssistant::runCLIAsync(",
        "void AIAssistant::refreshProfiles(")
    assert 'gatedEnvironment.insert(lit("REPLAYLAB_START_GATED"), lit("1"))' in async_cli
    assert "m_ConfigurationProcesses.insert(process, state)" in async_cli
    assert "if(assistant && owner && !assistant->m_ShuttingDown && completion)" in async_cli
    assert "connectAIConfigurationSignal" in async_cli

    attach = async_cli.index("attachKillOnCloseJob")
    start_gate = async_cli.index("write(startGate)")
    protected_input = async_cli.index("write(state->input)")
    close_input = async_cli.index("closeWriteChannel()")
    resume_process = async_cli.index("resumeSuspendedProcess")
    assert attach < start_gate < protected_input < close_input < resume_process

    assert (
        "QHash<QProcess *, std::shared_ptr<AIConfigurationProcessState>> "
        "m_ConfigurationProcesses;"
    ) in header
    shutdown = _text_section(
        cpp, "void AIAssistant::Shutdown()", "bool AIAssistant::AddTextAttachment(")
    assert shutdown.index("cancelConfigurationProcesses();") < shutdown.index(
        "forceStopProcessesForDestruction();")
    cancellation = _text_section(
        cpp, "void AIAssistant::cancelConfigurationProcesses()",
        "void AIAssistant::forceStopProcessesForDestruction()")
    assert "m_ConfigurationProcesses.clear()" in cancellation
    assert "state->completion = std::function" in cancellation
    assert "state->closeJob()" in cancellation
    assert "process->kill()" in cancellation
    assert "waitForFinished" not in cancellation


def test_connection_dialog_probes_and_subscription_cancel_remain_bounded() -> None:
    cpp = AI_ASSISTANT_CPP.read_text(encoding="utf-8")
    settings = _text_section(
        cpp, "QWidget *AIAssistant::CreateSettingsPage(",
        "void AIAssistant::manageConnections()")

    fetch_models = _text_section(
        settings, "connect(fetchModels, &QPushButton::clicked",
        "auto updateAuthStatus =")
    assert "beginDialogCommand(false, false)" in fetch_models

    cancellation = _text_section(
        settings, "QTimer *cancelTimer = new QTimer(processObject);",
        "connect(processObject, &QProcess::started")
    assert "cancelTimer->setSingleShot(true)" in cancellation
    assert "cancelTimer->setInterval(2500)" in cancellation
    assert "process->write(cancelRecord) != cancelRecord.size()" in cancellation
    assert "state->cancelTimer->start()" in cancellation
    close_job = cancellation.index("closeKillOnCloseJob(state->job)")
    kill = cancellation.index("process->kill()")
    complete = cancellation.index("completeOperation(-1, QProcess::CrashExit, detail)")
    assert close_job < kill < complete

def test_fatal_replay_keeps_context_available_for_recovery_without_gpu_work():
    bridge = _load_bridge()
    bridge.rd.ResultCode = types.SimpleNamespace(Succeeded=0)
    bridge._capture_session_id = "session-a"
    bridge._active_capture_token = "capture-a"
    bridge._write_current_context_freeze_terminal = lambda job: None
    bridge._finish_current_renderdoc_context = lambda facts: dict(facts)
    job = {"status": "queued", "facts": {"warnings": []}, "compatibleStages": (),
           "aliases": {}, "captureSessionId": "session-a", "captureToken": "capture-a",
           "currentEventId": 77, "restoreEventId": 41}
    class Controller:
        def GetFatalErrorStatus(self):
            return types.SimpleNamespace(code=1)
        def SetFrameEvent(self, *args):
            raise AssertionError("A fatal controller cannot perform GPU work")
    bridge._current_context_freeze_jobs = {"fatal": job}
    bridge._run_current_context_freeze_job("fatal", Controller())
    assert job["status"] == "completed"
    assert job["context"]["boundShaders"] == []
    assert "bread-replay-unavailable" in job["context"]["warnings"][0]

