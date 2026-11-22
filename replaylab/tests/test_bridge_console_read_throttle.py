"""Exercise the real bridge policy without importing Qt or opening a capture."""
import ast
from pathlib import Path
import threading
from types import SimpleNamespace


def policy(monkeypatch):
    source = Path(__file__).resolve().parents[1] / 'renderdoc_mcp_bridge/__init__.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    names = {'CONSOLE_QUIET_REQUEST_METHODS', 'CONSOLE_THROTTLED_READ_METHODS',
             '_console_read_log_times', '_console_read_log_lock'}
    nodes = [node for node in tree.body if (
        isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets)
    ) or (isinstance(node, ast.FunctionDef) and node.name == '_console_request_lifecycle_visible')]
    clock = [100.0]
    environment = {}
    namespace = {'threading': threading, 'os': SimpleNamespace(environ=environment),
                 'time': SimpleNamespace(monotonic=lambda: clock[0]), '_capture_session_id': 'capture-a'}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace, clock, environment


def test_read_progress_is_bounded_and_new_capture_is_reported(monkeypatch):
    ns, clock, _ = policy(monkeypatch)
    visible = ns['_console_request_lifecycle_visible']
    for method in ('get_pipeline_state', 'get_shader_info'):
        assert visible(method)
        assert not any(visible(method) for _ in range(1000))
    clock[0] += 10
    assert visible('get_shader_info')
    ns['_capture_session_id'] = 'capture-b'
    assert visible('get_shader_info')
    assert len(ns['_console_read_log_times']) == 2


def test_mutations_keep_every_lifecycle_and_status_stays_quiet(monkeypatch):
    ns, _, _ = policy(monkeypatch)
    visible = ns['_console_request_lifecycle_visible']
    assert all(visible('apply_shared_shader_replacement') for _ in range(100))
    assert not visible('get_current_renderdoc_context')
    assert not ns['_console_read_log_times']


def test_diagnostic_control_restores_unthrottled_reads(monkeypatch):
    ns, _, environment = policy(monkeypatch)
    environment['RENDERDOC_UI_DISABLE_READ_LOG_THROTTLE'] = '1'
    assert all(ns['_console_request_lifecycle_visible']('get_shader_info') for _ in range(100))
    assert not ns['_console_read_log_times']
