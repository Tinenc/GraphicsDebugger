"""Isolated native diagnostic streaming/cancellation and Black-theme smoke.

Run by util/ai/run_connection_ui_smoke.py; no captures, real accounts or Unity.
"""
import json
import os
from pathlib import Path
import time
from PySide2 import QtCore, QtGui, QtWidgets


class Smoke:
    def __init__(self):
        self.app = QtWidgets.QApplication.instance()
        self.start = time.monotonic()
        self.phase = "open"
        self.report = {"ok": False, "checks": []}
        self.timer = QtCore.QTimer(self.app)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)
        self.ticks = 0

    def check(self, name, value):
        if not value: raise RuntimeError(name)
        self.report["checks"].append(name)
        Path(os.environ["BREAD_DIAG_UI_REPORT"]).write_text(json.dumps(dict(self.report, phase=self.phase, ticks=self.ticks)), encoding="utf-8")

    def finish(self, error=None):
        self.timer.stop()
        if error:
            self.report["error"] = str(error)
            self.report["labels"] = [w.text()[:240] for w in self.app.allWidgets() if isinstance(w, QtWidgets.QLabel) and w.isVisible()][-30:]
            self.report["lists"] = [(w.objectName(),w.count()) for w in self.app.allWidgets() if isinstance(w, QtWidgets.QListWidget)]
        else: self.report["ok"] = True
        self.report["phase"] = self.phase
        self.report["ticks"] = self.ticks
        Path(os.environ["BREAD_DIAG_UI_REPORT"]).write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
        QtCore.QTimer.singleShot(0, pyrenderdoc.GetMainWindow().Widget().close)
        # The intentionally incomplete fake Agent cannot implement shutdown's
        # service RPCs. The launcher owns this isolated fixture's final exit;
        # cancellation itself is checked before this point and by its child PID.
        QtCore.QTimer.singleShot(200, lambda: os._exit(0))

    def tick(self):
        self.ticks += 1
        try:
            Path(os.environ["BREAD_DIAG_UI_REPORT"]).write_text(json.dumps(dict(self.report, phase=self.phase, ticks=self.ticks)), encoding="utf-8")
            if time.monotonic() - self.start > 35: raise RuntimeError("UI smoke timed out")
            widgets = self.app.allWidgets()
            if self.phase == "open":
                title = pyrenderdoc.GetMainWindow().Widget().windowTitle()
                self.check("Normal product title without build placeholders", "Unstable" not in title and "NO_GIT_COMMIT" not in title and "Development" not in title)
                settings = [w for w in widgets if isinstance(w, QtWidgets.QToolButton) and w.accessibleName() == "AI Settings"]
                if not settings: return
                self.check("Black palette is the default", QtWidgets.QApplication.palette("QWidget").color(QtGui.QPalette.Window).lightness() < 100)
                views = [w for w in widgets if isinstance(w, QtWidgets.QGraphicsView)]
                self.check("Graph views follow the Black palette", all(w.palette().color(QtGui.QPalette.Window).lightness() < 100 for w in views))
                self.phase = "settings"
                QtCore.QTimer.singleShot(0, settings[0].click)
            elif self.phase == "settings":
                rows = [w for w in widgets if w.objectName() == "AIConnectionList"]
                if not rows or rows[0].count() != 1: return
                rows[0].setCurrentRow(0)
                check = [w for w in widgets if isinstance(w, QtWidgets.QPushButton) and w.text() == "Check Connection"]
                if not check or not check[0].isEnabled(): return
                self.button = check[0]
                started = time.monotonic()
                self.button.click()
                self.check("Diagnostic dialog opens synchronously before checks", any(w.objectName() == "AIConnectionCheckDialog" and w.isVisible() for w in self.app.allWidgets()))
                self.report["openMilliseconds"] = (time.monotonic()-started)*1000
                self.phase = "stream"
            elif self.phase == "stream":
                rows = [w for w in widgets if w.objectName() == "AIConnectionCheckRows"]
                if not rows or rows[0].topLevelItemCount() < 2: return
                self.check("Rows update before command completion", rows[0].topLevelItem(1).text(1) == "second-probe")
                dialog = [w for w in widgets if w.objectName() == "AIConnectionCheckDialog"][0]
                self.check("Diagnostic background follows Black palette", dialog.palette().color(QtGui.QPalette.Window).lightness() < 100)
                main = pyrenderdoc.GetMainWindow().Widget()
                main.grab().save(os.environ["BREAD_DIAG_UI_IMAGE"])
                self.check("Captured native dark UI", True)
                self.phase = "cancel"
                self.check("About to cancel diagnostic", True)
                if os.environ.get("BREAD_DIAG_CLOSE_PARENT"):
                    dialog.parentWidget().window().close()
                else:
                    dialog.close()
                self.closed = time.monotonic()
            elif self.phase == "cancel":
                if time.monotonic() - self.closed < .5: return
                if not os.environ.get("BREAD_DIAG_CLOSE_PARENT"):
                    self.check("Closing restores settings controls", self.button.isEnabled())
                self.check("Diagnostic dialog is destroyed", not any(w.objectName() == "AIConnectionCheckDialog" for w in widgets))
                for w in self.app.topLevelWidgets():
                    if isinstance(w, QtWidgets.QDialog): w.close()
                self.finish()
        except Exception as error:
            self.finish(error)


def start():
    runner = Smoke()
    runner.app._bread_diagnostic_smoke = runner
    runner.timer.start()

pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(start)
