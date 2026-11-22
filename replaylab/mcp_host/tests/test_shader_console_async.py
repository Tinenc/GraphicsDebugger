"""Exercise production refresh methods without constructing Qt widgets."""

import ast
from collections import deque
import importlib.util
from pathlib import Path
import threading
import time
from unittest import mock

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "shader_console.py"
spec = importlib.util.spec_from_file_location("shader_console_async_test", SOURCE)
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


def _panel_methods():
    # Execute the actual methods. Only Qt construction/rendering is replaced;
    # the scheduling/cancellation logic is not duplicated in the test harness.
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    panel = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.ClassDef) and node.name == "ReplayLabConsolePanel"
                 and any(isinstance(child, ast.FunctionDef) and child.name == "refresh" for child in node.body))
    names = {"refresh", "capture_changed", "_closed", "_current_capture_binding", "shutdown",
             "_publish_snapshot", "_clear_messages", "_reset_watched_paths"}
    methods = [node for node in panel.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(SOURCE), "exec"), console.__dict__, namespace)
    return type("ProductionPanelMethods", (), namespace)


Panel = _panel_methods()
Panel.FALLBACK_SCAN_SECONDS = 2.0


class QueuedReader:
    def __init__(self):
        self.submitted = []
        self.completed = deque()
        self.closed = False

    def submit(self, request):
        assert not self.closed
        self.submitted.append(request)

    def take(self):
        return self.completed.popleft() if self.completed else None

    def shutdown(self):
        self.closed = True


def make_panel(tmp_path, reader=None):
    panel = Panel()
    panel.binding = {"instanceId": "test-app", "captureSessionId": "a" * 32,
                     "capturePath": str(tmp_path / "first.rdc")}
    panel.capture_binding_provider = lambda: dict(panel.binding) if panel.binding else None
    panel._alive = True
    panel.registry_path = str(tmp_path / "registry.json")
    panel._snapshot_reader = reader or QueuedReader()
    panel._snapshot_request = None
    panel._refresh_pending = False
    panel._watched_paths = [panel.registry_path]
    panel._last_disk_signature = None
    panel._snapshot = None
    panel._source_dialogs = []
    panel.published = []
    def publish(snapshot, signature):
        panel.published.append(snapshot)
        panel._snapshot = snapshot
        panel._last_disk_signature = signature
    panel._publish_snapshot = publish
    panel.detail = mock.Mock()
    panel.timer = mock.Mock()
    panel.change_timer = mock.Mock()
    panel.file_watcher = mock.Mock(files=lambda: [], directories=lambda: [])
    panel.closed_callback = mock.Mock()
    panel.top = object()
    return panel


def test_refresh_only_queues_one_read_and_coalesces_forced_requests(tmp_path, monkeypatch):
    panel = make_panel(tmp_path)
    reader = panel._snapshot_reader
    monkeypatch.setattr(console, "load_console_snapshot", mock.Mock(side_effect=AssertionError("GUI must not read")))
    monkeypatch.setattr(console, "_console_paths_signature", mock.Mock(side_effect=AssertionError("GUI must not stat")))
    panel.refresh()
    for index in range(30):
        panel.refresh(force=index % 2 == 0)
    assert len(reader.submitted) == 1
    first = reader.submitted[0]
    reader.completed.append((first, {"snapshot": {"captureBinding": first["captureBinding"]}, "signature": ("old",)}))
    panel.refresh()
    assert len(reader.submitted) == 2
    assert reader.submitted[-1]["force"] is True
    assert len(panel.published) == 1
    panel.refresh()
    assert len(reader.submitted) == 2


def test_idle_ui_ticks_consume_results_without_rescanning_project(tmp_path, monkeypatch):
    panel = make_panel(tmp_path)
    reader = panel._snapshot_reader
    clock = mock.Mock(return_value=100.0)
    monkeypatch.setattr(console.time, "monotonic", clock)
    panel.refresh()
    request = reader.submitted[0]
    reader.completed.append((request, {"snapshot": {"captureBinding": request["captureBinding"]}, "signature": ()}))
    for index in range(1, 20):
        clock.return_value = 100.0 + index * 0.1
        panel.refresh()
    assert len(panel.published) == 1
    assert len(reader.submitted) == 1
    clock.return_value = 102.01
    panel.refresh()
    assert len(reader.submitted) == 2
    reader.completed.append((reader.submitted[-1], {"unchanged": True}))
    panel._filesystem_dirty = True
    panel.refresh()
    assert len(reader.submitted) == 3
    assert reader.submitted[-1]["force"] is False


def test_console_watches_shared_directory_once_and_keeps_existing_handles(tmp_path):
    panel = make_panel(tmp_path)
    one, two = tmp_path / "shader.hlsl", tmp_path / "manifest.json"
    one.write_text("code", encoding="utf-8")
    two.write_text("{}", encoding="utf-8")
    panel._reset_watched_paths([str(one), str(two), str(tmp_path / "future.json")])
    panel.file_watcher.addPaths.assert_called_once_with([str(tmp_path)])
    panel.file_watcher.directories = lambda: [str(tmp_path)]
    panel.file_watcher.reset_mock()
    panel._reset_watched_paths([str(one), str(two)])
    panel.file_watcher.addPaths.assert_not_called()
    panel.file_watcher.removePaths.assert_not_called()


def test_unrelated_directory_events_do_not_reparse_or_republish_catalog(tmp_path, monkeypatch):
    source = tmp_path / 'shader.hlsl'
    source.write_text('source', encoding='utf-8')
    catalog = {'captureBinding': None, 'watchedPaths': [str(source), str(tmp_path)],
               'entries': [], 'errors': [], 'workspaces': []}
    request = dict(stop=threading.Event(), registryPath='registry', captureBinding=None,
                   watchedPaths=[], previousSignature=None, force=False)
    load = mock.Mock(return_value=catalog)
    monkeypatch.setattr(console, 'load_console_snapshot', load)
    monkeypatch.setattr(console, '_load_generic_console_entries', lambda: {
        'watchedPaths': [], 'errors': [], 'entries': []})
    for _ in range(2):
        result = console._build_console_panel_snapshot(request)
        request.update(watchedPaths=result['snapshot']['watchedPaths'],
                       previousSignature=result['signature'], cachedCatalog=result['cachedCatalog'])
    for _ in range(100):
        request['changedPaths'] = [str(tmp_path)]
        assert console._build_console_panel_snapshot(request) == {'unchanged': True}
    assert load.call_count == 2
    # An exact source notification still observes a coarse-timestamp write.
    request['changedPaths'] = [str(source)]
    assert 'snapshot' in console._build_console_panel_snapshot(request)
    assert load.call_count == 3


@pytest.mark.parametrize("via_callback", [False, True])
def test_capture_switch_discards_late_result_and_reissues_current_scope(tmp_path, via_callback):
    panel = make_panel(tmp_path)
    reader = panel._snapshot_reader
    panel.refresh()
    original = reader.submitted[0]
    panel.binding["captureSessionId"] = "b" * 32
    if via_callback:
        panel.capture_changed()
        assert original["stop"].is_set()
    reader.completed.append((original, {"snapshot": {"oldScope": True}, "signature": ("old",)}))
    panel.refresh()
    assert panel.published == []
    assert len(reader.submitted) == 2
    assert reader.submitted[-1]["captureBinding"]["captureSessionId"] == "b" * 32
    assert reader.submitted[-1]["force"] is True


def test_capture_closed_to_none_discards_old_success_or_error(tmp_path):
    panel = make_panel(tmp_path)
    panel.refresh()
    reader = panel._snapshot_reader
    original = reader.submitted[0]
    panel.binding = None
    panel.capture_changed()
    reader.completed.append((original, {"error": "old capture no longer exists"}))
    panel.refresh()
    panel.detail.setPlainText.assert_not_called()
    assert not panel.published
    assert reader.submitted[-1]["captureBinding"] is None


def test_panel_close_cancels_owner_and_never_consumes_late_snapshot(tmp_path):
    panel = make_panel(tmp_path)
    reader = panel._snapshot_reader
    callback = panel.closed_callback
    panel.refresh()
    request = reader.submitted[0]
    panel._closed()
    reader.completed.append((request, {"snapshot": {"late": True}, "signature": ()}))
    panel.refresh(True)
    panel._closed()
    assert request["stop"].is_set() and reader.closed
    assert len(reader.submitted) == 1 and not panel.published
    callback.assert_called_once_with()
    panel.timer.stop.assert_called_once_with()
    panel.change_timer.stop.assert_called_once_with()
    assert panel.top is None


def test_late_initial_snapshot_clears_only_messages_predating_panel_creation(tmp_path, monkeypatch):
    panel = make_panel(tmp_path)
    panel._opened_unix_ms = 1000
    panel._clear_initial_snapshot = True
    panel._clear_cursors = {}
    panel._selected_workspace_key = "__all__"
    panel.workspace_combo = mock.Mock()
    panel.workspace_combo.itemData.return_value = "__all__"
    panel._reset_watched_paths = mock.Mock()
    panel._render_selected = mock.Mock()
    # The first read completes much later than the moment the panel opened.
    monkeypatch.setattr(console.time, "time", lambda: 20.0)
    entries = [
        {"streamKey": "journal", "cursorSequence": 1, "unixMs": 900},
        {"streamKey": "journal", "cursorSequence": 2, "unixMs": 1100, "severity": "error"},
        {"streamKey": "journal", "cursorSequence": 3, "severity": "error"},
        {"streamKey": "audit", "cursorSequence": 1, "at": "1970-01-01T00:00:00.900Z"},
        {"streamKey": "audit", "cursorSequence": 2, "at": "1970-01-01T00:00:01.100Z", "severity": "error"},
    ]
    snapshot = {"entries": entries, "workspaces": [], "watchedPaths": []}
    # Use the production publisher and Clear implementation, bypassing only Qt rendering.
    Panel._publish_snapshot(panel, snapshot, ("first",))
    assert panel._clear_cursors == {"journal": 1, "audit": 1}
    assert console.console_entries_after_clear(entries, panel._clear_cursors) == [entries[1], entries[2], entries[4]]
    assert panel._clear_initial_snapshot is False
    Panel._publish_snapshot(panel, snapshot, ("later",))
    assert panel._clear_cursors == {"journal": 1, "audit": 1}
    # An explicit user Clear still clears all current messages.
    panel._clear_messages()
    assert console.console_entries_after_clear(entries, panel._clear_cursors) == []


def test_failed_reader_join_stops_ui_and_shutdown_retries_the_same_owner(tmp_path):
    panel = make_panel(tmp_path)
    panel.refresh()
    reader = panel._snapshot_reader
    reader.shutdown = mock.Mock(side_effect=[console.ShaderConsoleError("reader still stopping"), None])
    request = panel._snapshot_request
    with pytest.raises(console.ShaderConsoleError, match="still stopping"):
        panel._closed()
    assert not panel._alive and request["stop"].is_set()
    panel.timer.stop.assert_called_once_with()
    panel.change_timer.stop.assert_called_once_with()
    panel.shutdown()
    assert reader.shutdown.call_count == 2
    assert panel._snapshot_reader is reader


def test_bridge_keeps_failed_shutdown_owner_until_a_successful_retry():
    bridge = SOURCE.with_name("__init__.py")
    tree = ast.parse(bridge.read_text(encoding="utf-8"))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_unregister_shader_console_ui")
    owner = mock.Mock()
    owner.shutdown.side_effect = [RuntimeError("reader did not stop"), None]
    callbacks = [object()]
    namespace = {"_shader_console_ui": owner, "_shader_console_callbacks": callbacks, "print": mock.Mock()}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(bridge), "exec"), namespace)
    unregister = namespace["_unregister_shader_console_ui"]
    with pytest.raises(RuntimeError, match="did not stop"):
        unregister()
    assert namespace["_shader_console_ui"] is owner
    assert namespace["_shader_console_callbacks"] is callbacks
    namespace["print"].assert_called_once()
    unregister()
    assert owner.shutdown.call_count == 2
    assert namespace["_shader_console_ui"] is None
    assert namespace["_shader_console_callbacks"] == []


def _wait_result(reader):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = reader.take()
        if result is not None:
            return result
        time.sleep(0.005)
    pytest.fail("background reader did not complete")


def test_real_worker_keeps_refresh_responsive_and_returns_plain_result(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    observed_threads = []
    def read(registry, capture_binding=None, include_console_events=True):
        assert include_console_events is False
        observed_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return {"captureBinding": capture_binding, "watchedPaths": [registry], "entries": [], "errors": []}
    monkeypatch.setattr(console, "load_console_snapshot", read)
    reader = console._ConsoleSnapshotReader()
    panel = make_panel(tmp_path, reader)
    try:
        started = time.monotonic()
        panel.refresh(True)
        assert time.monotonic() - started < 0.5
        assert entered.wait(5)
        for _ in range(20):
            panel.refresh(True)
        assert not panel.published and observed_threads != [threading.get_ident()]
        request = panel._snapshot_request
        release.set()
        received, result = _wait_result(reader)
        assert received is request and result["snapshot"]["captureBinding"] == request["captureBinding"]
    finally:
        release.set()
        reader.shutdown()
    assert not reader.thread.is_alive()


def test_real_worker_cancellation_is_not_published_as_a_data_error(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def read(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        console._check_console_read_cancelled()
        pytest.fail("cancelled read continued")
    monkeypatch.setattr(console, "load_console_snapshot", read)
    reader = console._ConsoleSnapshotReader()
    panel = make_panel(tmp_path, reader)
    try:
        panel.refresh(True)
        assert entered.wait(5)
        request = panel._snapshot_request
        request["stop"].set()
        release.set()
        received, result = _wait_result(reader)
        assert received is request and result == {"cancelled": True}
    finally:
        release.set()
        reader.shutdown()
