"""Selection movement cannot cancel capture-identity/source-only UI reads."""

import ast
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "__init__.py"


def _ui_bridge(request):
    names = {
        "_ReplayLabBackgroundPreempted", "_selection_independent_request",
        "_request_dispatch_cancelled", "_invoke_on_ui_sync",
        "_request_preemption_message",
        "_background_replay_checkpoint",
    }
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
             and node.name in names]
    dispatch = {
        "cancelEvent": threading.Event(),
        "captureInvalidatedEvent": threading.Event(),
        "selectionPreemptedEvent": threading.Event(),
        "stopEvent": threading.Event(),
        "captureSnapshot": {"captureSessionId": "a" * 32},
    }
    calls = []
    helper = SimpleNamespace(InvokeOntoUIThread=lambda callback: callback())
    namespace = {
        "threading": threading, "_capture_session_id": "a" * 32,
        "_safe_int": lambda value, default=0: int(value or default),
        "_request_dispatch_context": lambda: dispatch,
        "_request_event_tracker": lambda: None,
        "_bridge_diagnostics": SimpleNamespace(ui_event=lambda *a: None),
        "_ctx": SimpleNamespace(Extensions=lambda:
                                SimpleNamespace(GetMiniQtHelper=lambda: helper)),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    dispatch["selectionIndependent"] = namespace["_selection_independent_request"](request)
    return namespace, dispatch, calls


@pytest.mark.parametrize("rpc_request", (
    {"method": "get_current_renderdoc_context", "params": {"identity_only": True}},
    {"method": "get_current_renderdoc_context", "params": {}},
    {"method": "inspect_event", "params": {"event_id": 1064}},
    {"method": "get_shader_workspace_ui_state", "params": {}},
    {"method": "read_shader_workspace_source", "params": {"capture_session_id": "b" * 32}},
    {"method": "show_shader_organization_preview", "params": {}},
))
def test_eid_selection_change_keeps_capture_identity_or_source_display(rpc_request):
    namespace, dispatch, calls = _ui_bridge(rpc_request)
    dispatch["cancelEvent"].set()
    dispatch["selectionPreemptedEvent"].set()
    result = namespace["_invoke_on_ui_sync"](lambda: calls.append("ui-read") or "result")
    assert result == "result"
    assert calls == ["ui-read"]


@pytest.mark.parametrize("change", ("capture-invalidated", "session-changed", "shutdown"))
def test_source_display_still_cancels_on_capture_change_or_shutdown(change):
    namespace, dispatch, calls = _ui_bridge({"method": "show_shader_organization_preview"})
    dispatch["cancelEvent"].set()
    if change == "capture-invalidated":
        dispatch["captureInvalidatedEvent"].set()
    elif change == "session-changed":
        namespace["_capture_session_id"] = "b" * 32
    else:
        dispatch["stopEvent"].set()
    with pytest.raises(namespace["_ReplayLabBackgroundPreempted"]):
        namespace["_invoke_on_ui_sync"](lambda: calls.append("ui-read"))
    assert calls == []


def test_explicit_cancellation_still_stops_a_fixed_eid_read():
    namespace, dispatch, calls = _ui_bridge({"method": "inspect_event", "params": {"event_id": 1064}})
    dispatch["cancelEvent"].set()
    with pytest.raises(namespace["_ReplayLabBackgroundPreempted"]):
        namespace["_invoke_on_ui_sync"](lambda: calls.append("unexpected"))
    assert not calls


@pytest.mark.parametrize("rpc_request", (
    {"method": "apply_shader_candidate_shared_resource", "params": {}},
    {"method": "export_shader_catalog", "params": {}},
))
def test_selection_dependent_or_replay_operations_keep_preemption(rpc_request):
    namespace, dispatch, calls = _ui_bridge(rpc_request)
    dispatch["cancelEvent"].set()
    with pytest.raises(namespace["_ReplayLabBackgroundPreempted"]):
        namespace["_invoke_on_ui_sync"](lambda: calls.append("operation"))
    assert calls == []


def test_eid_selection_preemption_is_resumable_but_capture_close_is_not():
    namespace, dispatch, calls = _ui_bridge({"method": "export_shader_catalog"})
    dispatch["cancelEvent"].set()
    dispatch["selectionPreemptedEvent"].set()
    with pytest.raises(namespace["_ReplayLabBackgroundPreempted"], match="yielded after"):
        namespace["_invoke_on_ui_sync"](lambda: calls.append("operation"))
    dispatch["captureInvalidatedEvent"].set()
    with pytest.raises(namespace["_ReplayLabBackgroundPreempted"], match="was cancelled"):
        namespace["_invoke_on_ui_sync"](lambda: calls.append("operation"))
    assert calls == []


def test_replay_thread_preemption_uses_carried_dispatch_context():
    namespace, dispatch, unused = _ui_bridge({"method": "export_shader_catalog"})
    dispatch["cancelEvent"].set()
    dispatch["selectionPreemptedEvent"].set()
    tracker = {"backgroundReplayTag": "test", "cancelEvent": dispatch["cancelEvent"],
               "dispatchContext": dispatch}
    # Replay callbacks carry the request tracker but not the worker's thread local.
    namespace["_request_dispatch_context"] = lambda: None
    namespace["_request_event_tracker"] = lambda: tracker
    with pytest.raises(namespace["_ReplayLabBackgroundPreempted"], match="yielded after"):
        namespace["_background_replay_checkpoint"]()

