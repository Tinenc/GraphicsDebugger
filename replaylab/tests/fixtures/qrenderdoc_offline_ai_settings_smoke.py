"""Exercise real settings with a missing or failing AgentHost on a private desktop."""
import json
import os
from pathlib import Path
import time
from PySide2 import QtCore, QtWidgets


class Smoke:
    def __init__(self):
        self.app = QtWidgets.QApplication.instance()
        self.started = time.monotonic()
        self.phase = "open"
        self.report = {"ok": False, "checks": []}
        self.profile_path = Path(os.environ["APPDATA"]) / "ReplayLab/ai/profiles.json"
        self.timer = QtCore.QTimer(self.app)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self.tick)

    def check(self, name, value):
        if not value:
            raise RuntimeError(name)
        self.report["checks"].append(name)

    def button(self, parent, text):
        return next(w for w in parent.findChildren(QtWidgets.QPushButton) if w.text() == text and w.isVisible())

    def field(self, parent, placeholder):
        return next(w for w in parent.findChildren(QtWidgets.QLineEdit) if w.placeholderText() == placeholder)

    def finish(self, error=None):
        self.timer.stop()
        if error:
            self.report["error"] = str(error)
            self.report["dialogs"] = [w.windowTitle() for w in self.app.topLevelWidgets() if isinstance(w, QtWidgets.QDialog) and w.isVisible()]
        else:
            self.report["ok"] = True
        self.report["phase"] = self.phase
        self.report["seconds"] = time.monotonic() - self.started
        Path(os.environ["BREAD_DIAG_UI_REPORT"]).write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
        QtCore.QTimer.singleShot(100, lambda: os._exit(0))

    def tick(self):
        try:
            if time.monotonic() - self.started > 25:
                raise RuntimeError("Offline settings UI smoke timed out")
            widgets = self.app.allWidgets()
            if self.phase == "open":
                buttons = [w for w in widgets if isinstance(w, QtWidgets.QToolButton) and w.accessibleName() == "AI Settings"]
                if not buttons:
                    return
                self.opened = time.monotonic()
                self.phase = "settings"
                QtCore.QTimer.singleShot(0, buttons[0].click)
            elif self.phase == "settings":
                lists = [w for w in widgets if w.objectName() == "AIConnectionList"]
                if not lists:
                    return
                self.connections = lists[0]
                self.page = self.connections.parentWidget()
                add = self.button(self.page, "Add...")
                self.check("Add is immediately available without an AI Host catalog", add.isEnabled())
                self.report["settingsOpenMilliseconds"] = (time.monotonic() - self.opened) * 1000
                self.phase = "add"
                QtCore.QTimer.singleShot(0, add.click)
            elif self.phase == "add":
                dialogs = [w for w in widgets if isinstance(w, QtWidgets.QDialog) and w.windowTitle() == "Add AI Connection" and w.isVisible()]
                if not dialogs:
                    return
                dialog = dialogs[0]
                providers = [w for w in dialog.findChildren(QtWidgets.QComboBox) if w.findData("ollama") >= 0]
                self.check("Built-in API provider list is available immediately", bool(providers) and providers[0].count() >= 10)
                providers[0].setCurrentIndex(providers[0].findData("ollama"))
                self.field(dialog, "For example: Work OpenAI or Personal Codex").setText("Offline UI test")
                self.field(dialog, "Exact model or deployment name").setText("offline-model")
                self.button(dialog, "Add").click()
                self.check("Save is enabled with no network or AI Host", self.button(dialog, "Save").isEnabled())
                dialog.grab().save(os.environ["BREAD_DIAG_UI_IMAGE"])
                self.phase = "saved"
                self.button(dialog, "Save").click()
            elif self.phase == "saved":
                if not self.profile_path.exists() or self.connections.count() != 1:
                    return
                payload = json.loads(self.profile_path.read_text(encoding="utf-8"))
                self.check("New connection is saved in the original profile format", payload["profiles"][0]["model"] == "offline-model")
                self.connections.setCurrentRow(0)
                edit = self.button(self.page, "Edit...")
                self.check("Edit remains enabled after unrelated host errors", edit.isEnabled())
                self.phase = "edit"
                QtCore.QTimer.singleShot(0, edit.click)
            elif self.phase == "edit":
                dialogs = [w for w in widgets if isinstance(w, QtWidgets.QDialog) and w.windowTitle() == "Edit AI Connection" and w.isVisible()]
                if not dialogs:
                    return
                dialog = dialogs[0]
                name = self.field(dialog, "For example: Work OpenAI or Personal Codex")
                self.check("Saved connection can be reopened without starting the AI Host", name.text() == "Offline UI test")
                name.setText("Offline edited")
                self.phase = "edited"
                self.button(dialog, "Save").click()
            elif self.phase == "edited":
                payload = json.loads(self.profile_path.read_text(encoding="utf-8"))
                if payload["profiles"][0]["title"] != "Offline edited":
                    return
                self.check("Edited settings persist without an AI Host", True)
                self.finish()
        except Exception as error:
            self.finish(error)


def start():
    runner = Smoke()
    runner.app._bread_offline_settings_smoke = runner
    runner.timer.start()


pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(start)
