"""Native --ui-python probe. Run only in a dedicated verification GUI instance."""
from __future__ import print_function
import json
import os
import traceback
from PySide2 import QtCore, QtWidgets

output = os.environ["BREAD_UI_VERIFY_OUTPUT"]
capture = os.environ["BREAD_UI_VERIFY_CAPTURE"]
os.makedirs(output, exist_ok=True)
report = {}

def finish():
    try:
        window = pyrenderdoc.GetMainWindow().Widget()
        assert window.grab().save(os.path.join(output, "bread-ai-ui.png"))
        report["aiActionTriggered"] = True
        report["status"] = "passed"
    except Exception:
        report["status"] = "failed"
        report["error"] = traceback.format_exc()
    with open(os.path.join(output, "native-ui.json"), "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)

def probe():
    try:
        if not pyrenderdoc.IsCaptureLoaded():
            pyrenderdoc.LoadCapture(capture, pyrenderdoc.Config().DefaultReplayOptions, capture, False, True)
        assert pyrenderdoc.IsCaptureLoaded()
        pyrenderdoc.SetEventID([], 11, 11)
        panel = pyrenderdoc.GetPipelineViewer().Widget()
        pyrenderdoc.RaiseDockWindow(panel)
        window = pyrenderdoc.GetMainWindow().Widget()
        report["captureLoaded"] = True
        report["capturePath"] = pyrenderdoc.GetCaptureFilename()
        report["pipelineWidgets"] = sorted(set(str(w.objectName()) for w in panel.findChildren(QtWidgets.QWidget)
            if "camera" in str(w.objectName()).lower()))
        assert report["pipelineWidgets"], "DF camera diagnostics missing from integrated pipeline panel"
        assert window.grab().save(os.path.join(output, "pipeline-ui.png"))
        action = window.findChild(QtWidgets.QAction, "action_AI_Assistant")
        assert action is not None, "Bread AI native menu is missing"
        report["aiActionText"] = action.text()
        action.trigger()
        QtCore.QTimer.singleShot(2000, finish)
    except Exception:
        report["status"] = "failed"
        report["error"] = traceback.format_exc()
        with open(os.path.join(output, "native-ui.json"), "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)

pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(probe)
