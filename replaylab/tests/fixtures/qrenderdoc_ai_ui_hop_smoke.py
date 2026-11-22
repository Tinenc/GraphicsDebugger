"""Runtime smoke test for ReplayLab's worker-to-qrenderdoc UI dispatch."""

from __future__ import print_function

import json
import os
import sys
import threading

from PySide2 import QtCore, QtWidgets


application = QtWidgets.QApplication.instance()
output_path = os.environ.get("REPLAYLAB_UI_HOP_SMOKE_OUTPUT", "")
finished = threading.Event()
result = {
    "schema": "replaylab-qrenderdoc-ai-ui-hop-smoke/v1",
    "ok": False,
}


def _publish_and_quit():
    if finished.is_set():
        return
    finished.set()
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    print("REPLAYLAB_UI_HOP_SMOKE=" + encoded)
    if output_path:
        with open(output_path, "w") as output:
            output.write(encoded)
    application.closeAllWindows()
    QtCore.QTimer.singleShot(0, lambda: application.exit(0))


def _timeout():
    if not finished.is_set():
        result["error"] = "worker-to-UI dispatch timed out"
        _publish_and_quit()


def _worker():
    bridge = sys.modules.get("renderdoc_mcp_bridge")
    try:
        if bridge is None:
            raise RuntimeError("built-in ReplayLab bridge is unavailable")

        # _invoke_on_ui_sync only hops when a worker request owns dispatch
        # context. Recreate that exact private boundary without opening a
        # capture or issuing any replay operation.
        bridge._request_dispatch_state.current = {
            "cancelEvent": threading.Event(),
            "captureSnapshot": {},
        }
        try:
            observed = bridge._invoke_on_ui_sync(
                lambda: {
                    "onApplicationThread": (
                        QtCore.QThread.currentThread() == application.thread()
                    ),
                    "miniQtHelperAvailable": (
                        pyrenderdoc.Extensions().GetMiniQtHelper() is not None
                    ),
                }
            )
        finally:
            del bridge._request_dispatch_state.current

        result.update(observed)
        result["ok"] = bool(
            observed.get("onApplicationThread")
            and observed.get("miniQtHelperAvailable")
        )
    except Exception as exc:
        result["error"] = "%s: %s" % (type(exc).__name__, str(exc))
    pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(
        _publish_and_quit
    )


def _start():
    worker = threading.Thread(target=_worker)
    worker.daemon = True
    worker.start()
    bridge = sys.modules.get("renderdoc_mcp_bridge")
    if bridge is not None:
        bridge._runtime_ui_hop_smoke_worker = worker


timeout = QtCore.QTimer()
timeout.setSingleShot(True)
timeout.timeout.connect(_timeout)
timeout.start(10000)

bridge = sys.modules.get("renderdoc_mcp_bridge")
if bridge is not None:
    bridge._runtime_ui_hop_smoke_timeout = timeout
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(_start)
