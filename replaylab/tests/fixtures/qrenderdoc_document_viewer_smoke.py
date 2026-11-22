"""Native reader smoke: no capture or Unity project is opened or changed."""
import json
import os
import sys
from PySide2 import QtCore, QtWidgets

class Runner(object):
    def run(self):
        from renderdoc_mcp_bridge.project_document_viewer import DocumentWindow
        self.output = os.environ["BREAD_DOCUMENT_SMOKE_OUTPUT"]
        self.window = DocumentWindow(os.environ["BREAD_DOCUMENT_SMOKE_INPUT"])
        self.window.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
        self.window.show()
        self.ticks = 0
        self.timer = QtCore.QTimer(self.window)
        self.timer.timeout.connect(self.check)
        self.timer.start(100)

    def check(self):
        self.ticks += 1
        if self.window.source.toPlainText() or self.ticks > 100:
            self.timer.stop()
            try:
                checks = {"unicode": "渲染还原" in self.window.source.toPlainText(),
                          "table": "<table" in self.window.browser.toHtml(),
                          "flowchart": self.window.diagram_tabs.count() == 1,
                          "noCaptureLoaded": not pyrenderdoc.IsCaptureLoaded()}
                self.window.grab().save(os.path.splitext(self.output)[0]+".png")
                self.window.tabs.setCurrentWidget(self.window.diagram_tabs)
                QtWidgets.QApplication.processEvents()
                view = self.window.diagram_tabs.widget(0)
                checks["nativeGraphNodes"] = len(view._nodes) == 3
                self.window.grab().save(os.path.splitext(self.output)[0]+"-graph.png")
                result = {"ok": all(checks.values()), "checks": checks, "captureMutated": False}
            except Exception as error:
                result = {"ok": False, "error": repr(error)}
            with open(self.output, "w", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
            self.window.close()
            QtCore.QTimer.singleShot(20, QtWidgets.QApplication.quit)

runner = Runner()
sys.modules["renderdoc_mcp_bridge"]._document_smoke_runner = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.run)
