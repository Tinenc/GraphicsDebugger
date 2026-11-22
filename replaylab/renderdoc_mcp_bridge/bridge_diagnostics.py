"""Small crash breadcrumbs for the embedded Python 3.6 bridge.

Never persist request arguments, source text, provider configuration or tokens.
Failure to write diagnostics must never prevent opening a capture/project.
"""
import atexit
import json
import os
from pathlib import Path
import threading
import time

_guard = threading.Lock()
_stream = None
_fatal = None
_owns_fault_handler = False
_registered = False
_UI_METHODS = frozenset(("show_frame_render_flow", "save_frame_render_flow",
                         "show_shader_organization_preview", "open_native_document"))


def record(kind, method="", callback="", outcome=""):
    try:
        with _guard:
            if _stream is not None:
                _stream.write(json.dumps({"schema": "bread-bridge-diagnostic/v1",
                    "pid": os.getpid(), "unixMs": int(time.time() * 1000),
                    "kind": kind, "method": method, "callback": callback,
                    "outcome": outcome}, sort_keys=True) + "\n")
                _stream.flush()
    except Exception:
        pass


def ui_event(kind, method, callback, outcome=""):
    if method in _UI_METHODS:
        record(kind, method, callback, outcome)


def start(version):
    global _stream, _fatal, _owns_fault_handler, _registered
    if _stream is not None:
        return
    try:
        folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ReplayLab/logs"
        folder.mkdir(parents=True, exist_ok=True)
        _stream = (folder / ("bridge-%s.jsonl" % os.getpid())).open("a", encoding="utf-8")
        record("started", outcome=version)
        import faulthandler
        if not faulthandler.is_enabled():
            _fatal = (folder / ("bridge-%s-fatal.log" % os.getpid())).open("a", encoding="utf-8")
            faulthandler.enable(file=_fatal, all_threads=True)
            _owns_fault_handler = True
        if not _registered:
            atexit.register(stop)
            _registered = True
    except Exception:
        # Retain startup breadcrumbs even on builds without faulthandler.
        pass


def stop():
    global _stream, _fatal, _owns_fault_handler
    record("stopped")
    try:
        if _owns_fault_handler:
            import faulthandler
            faulthandler.disable()
    except Exception:
        pass
    _owns_fault_handler = False
    with _guard:
        for stream in (_fatal, _stream):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        _fatal = _stream = None
