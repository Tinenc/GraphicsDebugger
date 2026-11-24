"""Run with run_connection_ui_smoke.py --ui-script and RD_ANNOTATION_CAPTURE.

Uses an owned private desktop/profile, actual replay readback and Qt events. It never
sends an AI request, writes the capture, or changes another application's session.
"""
import json
import os
from pathlib import Path
import sys
import time
import traceback

import renderdoc as rd
from PySide2 import QtCore, QtGui, QtWidgets


class AnnotationSmoke(object):
    def __init__(self):
        self.app = QtWidgets.QApplication.instance()
        self.output = Path(os.environ["BREAD_DIAG_UI_REPORT"])
        self.capture = Path(os.environ["RD_ANNOTATION_CAPTURE"]).resolve()
        self.before = self.capture.stat()
        self.phase = "open"
        self.started = time.monotonic()
        self.result = {"ok": False, "checks": [], "capture": str(self.capture)}
        self.keep = []

    def check(self, name, value):
        if not value:
            raise RuntimeError(name)
        self.result["checks"].append(name)

    def button(self, source):
        text = QtCore.QCoreApplication.translate("AITextureAnnotation", source)
        buttons = self.panel.findChildren(QtWidgets.QAbstractButton)
        self.keep.extend(buttons)
        return next(item for item in buttons if item.text() == text)

    def drag(self, start, end):
        self.canvas.grab()
        left, top, width, height = self.canvas.property('annotationImageRect')
        for kind, uv in ((QtCore.QEvent.MouseButtonPress, start),
                         (QtCore.QEvent.MouseMove, end),
                         (QtCore.QEvent.MouseButtonRelease, end)):
            point = QtCore.QPointF(left + uv[0] * width, top + uv[1] * height)
            event = QtGui.QMouseEvent(kind, point, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton,
                                     QtCore.Qt.NoModifier)
            self.app.sendEvent(self.canvas, event)

    def tick(self):
        try:
            if time.monotonic() - self.started > 60:
                raise RuntimeError("Annotation UI timed out at " + self.phase)
            if self.phase == "open":
                bridge = sys.modules.get("renderdoc_mcp_bridge")
                if bridge is None:
                    return
                self.check("fresh owned capture context", not pyrenderdoc.IsCaptureLoaded())
                bridge._cancel_project_auto_activation()
                bridge._schedule_project_auto_activation = lambda: None
                pyrenderdoc.LoadCapture(str(self.capture), rd.ReplayOptions(), str(self.capture), False, True)
                self.phase = "loaded"
                return
            if self.phase == "loaded":
                if not pyrenderdoc.IsCaptureLoaded():
                    return
                draws = []
                def visit(actions):
                    for action in actions:
                        if action.flags & rd.ActionFlags.Drawcall:
                            draws.append(int(action.eventId))
                        visit(action.children)
                visit(pyrenderdoc.CurRootActions())
                self.check("fixture has two draw events", len(draws) >= 2)
                self.anchor, self.other = int(os.getenv("RD_ANNOTATION_EVENT", draws[-1])), draws[0]
                self.check("annotation observation is a draw", self.anchor in draws)
                pyrenderdoc.SetEventID([], self.anchor, self.anchor, True)
                pyrenderdoc.ShowTextureViewer()
                self.viewer = pyrenderdoc.GetTextureViewer()
                resource = os.getenv("RD_ANNOTATION_RESOURCE")
                if resource:
                    texture = next(t for t in pyrenderdoc.GetTextures()
                                   if str(t.resourceId).split("::")[-1] == resource)
                    self.viewer.ViewTexture(texture.resourceId, rd.CompType.Typeless, True)
                self.window = pyrenderdoc.GetMainWindow().Widget()
                self.keep.extend([self.viewer, self.window])
                self.window.findChild(QtWidgets.QAction, "action_AI_Assistant").trigger()
                self.phase = "binding"
                self.ready_after = time.monotonic() + 1.0
                return
            if self.phase == "binding":
                if time.monotonic() < self.ready_after:
                    return
                instances = Path(os.environ["LOCALAPPDATA"]) / "ReplayLab/ipc-v2/instances"
                ready = False
                for path in instances.glob("*/instance.json"):
                    value = json.loads(path.read_text(encoding="utf-8"))
                    ready |= value.get("pid") == os.getpid() and value.get("captureLoaded", False)
                if not ready:
                    return
                self.brush = self.window.findChild(QtWidgets.QPushButton, "aiAnnotateRenderTarget")
                self.keep.append(self.brush)
                self.check("brush enabled with capture", self.brush is not None and self.brush.isEnabled())
                self.brush.click()
                self.phase = "canvas"
                return
            if self.phase == "canvas":
                self.panel = self.window.findChild(QtWidgets.QWidget, "aiTextureAnnotation")
                if self.panel is None:
                    return
                self.canvas = self.panel.findChild(QtWidgets.QWidget, "aiAnnotationCanvas")
                self.keep.extend([self.panel, self.canvas])
                self.check("canvas appears in texture viewer", self.panel.isVisible() and self.canvas.width() > 100)
                self.result["canvasSize"] = [self.canvas.width(), self.canvas.height()]
                self.panel.grab().save(os.environ["BREAD_DIAG_UI_IMAGE"])
                self.panel.grab().save(os.environ["BREAD_DIAG_UI_IMAGE"])
                controls = {name: self.button(name) for name in
                            ("Fit", "Undo", "Redo", "Clear", "Cancel", "Add to conversation")}
                canvas_top = self.canvas.mapToGlobal(QtCore.QPoint()).y()
                self.check("actions above the drawing canvas", all(
                    widget.mapToGlobal(QtCore.QPoint()).y() + widget.height() <= canvas_top
                    for widget in controls.values()))
                def position(name):
                    point = controls[name].mapToGlobal(QtCore.QPoint())
                    return point.y(), point.x()

                fit_y, fit_x = position("Fit")
                undo_y, undo_x = position("Undo")
                self.check("history follows Fit across toolbar rows",
                           (undo_y > fit_y + 5 or (abs(undo_y - fit_y) <= 5 and undo_x > fit_x)) and
                           position("Undo")[1] < position("Redo")[1] < position("Clear")[1])
                self.check("actions are grouped on the upper right",
                           position("Cancel")[0] == position("Add to conversation")[0] and
                           position("Cancel")[0] < position("Fit")[0] and
                           position("Cancel")[1] < position("Add to conversation")[1])
                self.check("toolbar controls fit within the canvas width", all(
                    widget.mapTo(self.panel, QtCore.QPoint()).x() + widget.width() <= self.panel.width()
                    for widget in controls.values()))
                self.check("add waits for marks", not controls["Add to conversation"].isEnabled())

                def background(button):
                    image = button.grab().toImage()
                    return image.pixelColor(8, image.height() // 2)

                cancel_color = background(controls["Cancel"])
                self.check("Cancel has a red background", cancel_color.red() > cancel_color.green() + 25)
                self.button("Circle").click()
                self.drag((.3, .35), (.6, .65))
                self.button("Arrow").click()
                self.drag((.75, .7), (.45, .5))
                self.check("marks enable attachment", self.button("Add to conversation").isEnabled())
                add_color = background(controls["Add to conversation"])
                self.check("Add to conversation has a green background", add_color.green() > add_color.red() + 25)
                self.panel.grab().save(os.environ["BREAD_DIAG_UI_IMAGE"])
                pyrenderdoc.SetEventID([], self.other, self.other, True)
                self.phase = "attach"
                return
            if self.phase == "attach":
                self.check("navigation leaves frozen canvas visible", self.panel.isVisible())
                self.check("current EID changed after drawing", int(pyrenderdoc.CurEvent()) == self.other)
                self.button("Add to conversation").click()
                self.phase = "chips"
                return
            if self.phase == "chips":
                chips = self.window.findChildren(QtWidgets.QFrame, "aiAttachmentChip")
                self.keep.extend(chips)
                self.check("one visible marked image with hidden original", len(chips) == 1)
                names = [button.accessibleName() for chip in chips
                         for button in chip.findChildren(QtWidgets.QAbstractButton)]
                self.check("images retain drawing EID after navigation", all("EID " + str(self.anchor) in name for name in names))
                self.result.update(annotationEID=self.anchor, currentEID=self.other, attachmentNames=names)
                # Return to the exact observation and use the brush again: restore the draft.
                pyrenderdoc.SetEventID([], self.anchor, self.anchor, True)
                self.brush.click()
                self.phase = "reopen"
                return
            if self.phase == "reopen":
                self.panel = self.window.findChild(QtWidgets.QWidget, "aiTextureAnnotation")
                if self.panel is None: return
                self.canvas = self.panel.findChild(QtWidgets.QWidget, "aiAnnotationCanvas")
                self.keep.extend([self.panel,self.canvas])
                self.check("brush resumes existing marks", self.button("Update conversation marks").isEnabled())
                self.panel.grab().save(str(self.output.with_name('resumed.png')))
                self.button("Eraser").click()
                width = self.panel.findChild(QtWidgets.QSpinBox, "aiAnnotationEraserWidth")
                self.keep.append(width)
                width.setValue(48)
                self.drag((.3,.5),(.3,.53))
                self.button("Update conversation marks").click()
                self.phase = "updated"
                return
            if self.phase == "updated":
                chips = self.window.findChildren(QtWidgets.QFrame, "aiAttachmentChip")
                self.keep.extend(chips)
                self.check("update replaces pair instead of duplicating", len(chips)==1)
                # A chip click edits the saved snapshot even after navigating to another EID.
                pyrenderdoc.SetEventID([], self.other, self.other, True)
                prefix = QtCore.QCoreApplication.translate("AIAssistant", "Preview attachment: %1").split("%1")[0]
                preview = next(b for b in chips[0].findChildren(QtWidgets.QAbstractButton)
                               if b.accessibleName().startswith(prefix))
                self.keep.append(preview)
                preview.click()
                self.phase = "reopen-click"
                return
            if self.phase == "reopen-click":
                self.panel = self.window.findChild(QtWidgets.QWidget, "aiTextureAnnotation")
                if self.panel is None:return
                self.keep.append(self.panel)
                self.check("chip reopens pinned snapshot after navigation", self.button("Update conversation marks").isEnabled())
                self.button("Cancel").click()
                self.phase = "remove"
                return
            if self.phase == "remove":
                chips = self.window.findChildren(QtWidgets.QFrame, "aiAttachmentChip")
                self.keep.extend(chips)
                prefix = QtCore.QCoreApplication.translate("AIAssistant", "Remove attachment: %1").split("%1")[0]
                remove = next(button for button in chips[0].findChildren(QtWidgets.QAbstractButton)
                              if button.accessibleName().startswith(prefix))
                self.keep.append(remove)
                remove.click()
                self.phase = "removed"
                return
            if self.phase == "removed":
                self.check("remove keeps annotation pair atomic", not self.window.findChildren(QtWidgets.QFrame, "aiAttachmentChip"))
                self.finish()
        except Exception:
            self.finish(traceback.format_exc())

    def finish(self, error=None):
        self.timer.stop()
        after = self.capture.stat()
        self.result.update(ok=not error, error=error, phase=self.phase,
                           captureUnchanged=(after.st_size, after.st_mtime_ns) ==
                                            (self.before.st_size, self.before.st_mtime_ns))
        self.output.write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding="utf-8")
        pyrenderdoc.CloseCapture()
        QtCore.QTimer.singleShot(50, self.app.closeAllWindows)
        QtCore.QTimer.singleShot(150, self.app.quit)

    def start(self):
        self.timer = QtCore.QTimer()
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.tick)
        self.timer.start()


runner = AnnotationSmoke()
sys.modules["__main__"]._bread_rt_annotation_smoke = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
