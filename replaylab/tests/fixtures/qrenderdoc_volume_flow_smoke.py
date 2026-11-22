"""No-capture Qt acceptance for cached Texture3D flow previews via --ui-python.

Set BREAD_VOLUME_SMOKE_ROOT and BREAD_VOLUME_SMOKE_SOURCE (bridge directory),
with LOCALAPPDATA and RENDERDOC_CONFIG_DIR below the system-temp smoke root.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
import types

from PySide2 import QtCore, QtGui, QtWidgets


class VolumeSmoke(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.root = Path(os.environ["BREAD_VOLUME_SMOKE_ROOT"]).resolve()
        if os.path.commonpath([str(self.root), tempfile.gettempdir()]) != tempfile.gettempdir():
            raise RuntimeError("Volume smoke must stay in system temporary directory")
        for name in ("LOCALAPPDATA", "RENDERDOC_CONFIG_DIR"):
            value = Path(os.environ[name]).resolve()
            if value == self.root or os.path.commonpath([str(self.root), str(value)]) != str(self.root):
                raise RuntimeError("Volume smoke requires isolated settings")
        self.result = {"ok": False, "checks": [], "pid": os.getpid()}
        self.phase, self.started = "start", time.monotonic()
        self.navigation, self.live_requests = [], []

    def save(self):
        self.result["phase"] = self.phase
        (self.root / "result.json").write_text(json.dumps(self.result, indent=2), encoding="utf-8")

    def check(self, name, value):
        if not value:
            raise AssertionError(name)
        self.result["checks"].append(name)

    def start(self):
        QtWidgets.QApplication.instance().setQuitOnLastWindowClosed(False)
        self.timer = QtCore.QTimer()
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.save()

    def finish(self):
        self.save()
        self.timer.stop()
        if hasattr(self, "view"):
            self.view.shutdown()
            self.view.close()
        QtCore.QTimer.singleShot(50, self.ctx.GetMainWindow().Widget().close)
        QtCore.QTimer.singleShot(100, QtWidgets.QApplication.instance().quit)

    def tick(self):
        try:
            if time.monotonic() - self.started > 20:
                if hasattr(self, "popup"):
                    self.result["popupState"] = {"alive": self.popup._alive, "visible": self.popup.isVisible(),
                        "timer": self.popup._timer.isActive(), "pending": len(self.popup._pending),
                        "cache": len(self.popup._cache), "status": self.popup.status.text()}
                raise RuntimeError("Volume UI smoke timed out")
            if self.phase == "start":
                source = Path(os.environ["BREAD_VOLUME_SMOKE_SOURCE"])
                package = types.ModuleType("bread_volume_ui_smoke")
                package.__path__ = [str(source)]
                sys.modules[package.__name__] = package
                spec = importlib.util.spec_from_file_location(package.__name__ + ".project_frame_flow", str(source / "project_frame_flow.py"))
                self.module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(self.module)
                rows = []
                for channel in ("rgb", "r", "g", "b", "a"):
                    for z in (0, 2, 7):
                        path = self.root / (channel + str(z) + ".png")
                        image = QtGui.QImage(160, 90, QtGui.QImage.Format_RGB32)
                        shade = 20 + z * 20 + (20 if channel == "a" else 0)
                        image.fill(QtGui.QColor(shade, shade, shade))
                        image.save(str(path))
                        rows.append({"slice": z, "channel": channel, "path": str(path),
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "width": 160, "height": 90})
                preview = {"title": "Volume", "description": "Captured slices", "eventId": 1,
                    "resourceId": "7", "channel": "rgb", "componentCount": 4, "format": "R16G16B16A16_FLOAT",
                    "path": rows[1]["path"], "width": 160, "height": 90,
                    "displayMapping": {"mip": 0, "slice": 2, "flipY": True},
                    "volumePreview": {"depth": 8, "mip": 0, "selectedSlice": 2, "slices": rows}}
                self.document = self.module.normalize_document({"schema": self.module.FLOW_SCHEMA,
                    "flowId": "volume", "captureSessionId": "a", "capturePath": str(self.root / "fake.rdc"),
                    "stages": [{"id": "stage", "title": "Volume pass", "startEventId": 1, "endEventId": 1,
                                "eventIds": [1], "representativeEventId": 1, "images": [preview]}]})
                self.view = self.module.FrameRenderFlowView(self.navigation.append,
                    channel_request_callback=lambda *args: self.live_requests.append(args))
                self.view.resize(960, 700)
                self.view.set_document(self.document)
                self.check("Volume texture card has a slice entry", "volume:0" in self.view._stages["stage"].group_toggles)
                self.view._open_volume("stage", "volume:0")
                self.popup = self.view._channel_popup
                self.check("All captured channels available", self.popup.channel.count() == 5)
                self.check("Default slider index preserves actual Z", self.popup.slider.value() == 1)
                self.phase = "default"
            elif self.phase == "default" and self.popup._pixmap is not None:
                self.check("Default cached slice rendered", self.popup._pixmap.toImage().pixelColor(0, 0).red() == 60)
                self.popup.slider.setValue(2)
                self.popup.channel.setCurrentIndex(4)
                self.phase = "changed"
            elif self.phase == "changed" and self.popup._pixmap is not None:
                self.check("Slider and channel render actual cached alpha slice", self.popup._pixmap.toImage().pixelColor(0, 0).red() == 180)
                self.check("Channel change retains depth position", self.popup._rows[self.popup.slider.value()]["slice"] == 7)
                self.popup._open_slice()
                target = self.navigation[-1]
                self.check("Texture Viewer navigation uses actual Z/channel and parent orientation", target["slice"] == 7 and target["channel"] == "a" and target["displayMapping"]["flipY"] is True)
                self.popup.grab().save(str(self.root / "volume-popup.png"))
                self.popup.close()
                self.phase = "reopening"
            elif self.phase == "reopening":
                self.view._open_volume("stage", "volume:0")
                self.popup = self.view._channel_popup
                self.phase = "reopened"
            elif self.phase == "reopened" and self.popup._pixmap is not None:
                self.check("Reopening displays cached slices", self.popup._pixmap.toImage().pixelColor(0, 0).red() == 60)
                changed = dict(self.document, captureSessionId="b")
                self.view.set_document(changed)
                self.check("Capture switch closes old volume popup", not self.popup._alive and self.view._channel_popup is None)
                self.check("Browsing never requests replay", not self.live_requests)
                self.result["ok"] = True
                self.phase = "complete"
                self.finish()
                return
            self.save()
        except Exception:
            self.result["error"] = traceback.format_exc()
            self.finish()


runner = VolumeSmoke(pyrenderdoc)
sys.modules["__main__"]._bread_volume_flow_smoke = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
