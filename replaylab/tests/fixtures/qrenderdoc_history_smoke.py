"""Run in a fresh qrenderdoc process with --ui-python.

RENDERDOC_HISTORY_SMOKE_CAPTURE: explicit .rdc path (or use an already open capture).
RENDERDOC_HISTORY_SMOKE_EID: actual draw EID to anchor; defaults to CurEvent.
RENDERDOC_HISTORY_SMOKE_TEXTURE: optional decimal ResourceId, must be an actual
color target bound at that EID; otherwise the first bound color target is used.
RENDERDOC_HISTORY_SMOKE_OUTPUT: JSON report path.
RENDERDOC_HISTORY_SMOKE_QUIT=1: close the test instance after writing the report.
RENDERDOC_HISTORY_SMOKE_ISOLATE_AUTO_RESTORE=1: require an unloaded owned test process;
temporarily suppress project auto-activation before LoadCapture, verify native mutation
generation is unchanged, close this capture and restore the bridge callback at completion.
RENDERDOC_HISTORY_SMOKE_PYTHON_STACKS=1: during initial/warm/single/scroll, a native
faulthandler watchdog dumps this process's Python threads every 2 seconds to
<report>.python-stacks.log. It does not require the GUI event loop or Python GIL to tick.
RENDERDOC_HISTORY_SMOKE_TIMEOUT: per-phase seconds (default 45).
RENDERDOC_HISTORY_SMOKE_MAX_HEARTBEAT_MS: allowed history-loading GUI gap (default 350).
RENDERDOC_HISTORY_SMOKE_TAB_SWITCH=1: also validate current/history tab isolation using
real screen pixels, rapid mouse tab changes and a controlled replay/GUI completion race.
RENDERDOC_HISTORY_SMOKE_WITNESS=output: read the existing output window's GPU result
through the opt-in timing witness, without Display or resource rebinding. This mode
works with a locked desktop and proves application output, not OS scanout.
The two selected images must differ for this additional pixel comparison to be conclusive.

Uses real Qt mouse events routed through the widget at the image-area position.
If native top-level lookup is unavailable, uses the active test window's actual
childAt hit test and visible region; it does not claim OS mouse-input coverage.
It never invokes a thumbnail callback or private history method. The assertions
cover single preview without global navigation, double-click global navigation,
anchor-first neighborhood requests, bounded widgets/cache and GUI timer gaps.
The heartbeat excludes capture loading and synchronous global EID navigation.
Run this separately for 1.rdc and 6.rdc with suitable bound draw EIDs/resources.
"""
from __future__ import print_function

import hashlib
import json
import os
import sys
import threading
import time
import traceback

import renderdoc as rd
from PySide2 import QtCore, QtGui, QtWidgets


def _pending_surface_evidence(sample, current_reference):
    if sample["displayedEvent"] is not None or sample["displayedResource"] is not None:
        raise RuntimeError("Pending tab prematurely certified an EID/resource")
    if sample["blankRGB"]:
        return "gpu-cleared"
    if (sample["imageSize"] == current_reference["imageSize"] and
            sample["sha256"] == current_reference["sha256"]):
        return "current-before-gui-completion"
    raise RuntimeError("Pending tab exposed neither a cleared output nor the exact current image")


class _HistorySmoke(object):
    def __init__(self):
        self.app = QtWidgets.QApplication.instance()
        self.result = {"schema": "qrenderdoc-history-smoke/v1", "ok": False, "cases": []}
        self.output = os.environ.get("RENDERDOC_HISTORY_SMOKE_OUTPUT", "")
        self.phase = "loading"
        self.finished = False
        self.timer = None
        self.history = None
        self.gaps = []
        self.loading_gaps = []
        self.samples = []
        self.last_tick = None
        self.deadline = time.perf_counter() + 120.0
        self.timeout = float(os.environ.get("RENDERDOC_HISTORY_SMOKE_TIMEOUT", "45"))
        self.max_gap = float(os.environ.get("RENDERDOC_HISTORY_SMOKE_MAX_HEARTBEAT_MS", "350"))
        self.keep = []
        self.kept_ids = set()
        self.mouse_routes = []
        self.mouse_grab = None
        self.test_window = None
        self.saved_window_geometry = None
        self.saved_window_minimum = None
        self.saved_window_constraint = None
        self.window_layout = None
        self.history_splitter = None
        self.saved_splitter_limits = None
        self.saved_splitter_sizes = None
        self.layout_signature = None
        self.tab_checks = os.environ.get("RENDERDOC_HISTORY_SMOKE_TAB_SWITCH") == "1"
        self.output_witness = os.environ.get("RENDERDOC_HISTORY_SMOKE_WITNESS") == "output"
        self.slow_gaps = []
        self.previous_tick_phase = None
        self.previous_tick_wall_ms = 0.0
        self.surface_samples = []
        self.surface_counter = 0
        self.replay_release = None
        self.isolate_auto_restore = os.environ.get("RENDERDOC_HISTORY_SMOKE_ISOLATE_AUTO_RESTORE") == "1"
        self.isolation_bridge = None
        self.isolation_original_schedule = None
        self.isolation_schedule = None
        self.result["autoRestoreIsolation"] = {"requested": self.isolate_auto_restore,
                                                 "installed": False}
        self.stack_sampling_requested = os.environ.get("RENDERDOC_HISTORY_SMOKE_PYTHON_STACKS") == "1"
        self.stack_file = None
        self.stack_module = None
        self.stack_sampling = False

    def start(self):
        try:
            self.publish_progress("starting")
            self.install_auto_restore_isolation()
            # QApplication outlives qrenderdoc's embedded Python interpreter. Keep this Python
            # wrapper Python-owned so QApplication teardown cannot destroy it after Py_Finalize.
            self.timer = QtCore.QTimer()
            self.timer.setTimerType(QtCore.Qt.PreciseTimer)
            self.timer.setInterval(5)
            self.timer.timeout.connect(self.tick)
            capture = os.environ.get("RENDERDOC_HISTORY_SMOKE_CAPTURE", "")
            if not pyrenderdoc.IsCaptureLoaded() and capture:
                pyrenderdoc.LoadCapture(capture, rd.ReplayOptions(), capture, False, True)
            self.timer.start()
            self.publish_progress("loading")
        except Exception as exc:
            self.finish(exc)

    def install_auto_restore_isolation(self):
        if not self.isolate_auto_restore:
            return
        if (pyrenderdoc.IsCaptureLoaded()
                or bool(getattr(pyrenderdoc, "IsCaptureLoading", lambda: False)())):
            raise RuntimeError("Auto-restore isolation must be installed before loading any capture")
        if not os.environ.get("RENDERDOC_HISTORY_SMOKE_CAPTURE", ""):
            raise RuntimeError("Auto-restore isolation requires an explicit fixture capture path")
        bridge = sys.modules.get("renderdoc_mcp_bridge")
        if bridge is None:
            raise RuntimeError("The installed canonical ReplayLab bridge is unavailable for isolation")
        if getattr(bridge, "_project_auto_restore_client", None) is not None:
            raise RuntimeError("A project restore is already active; use a fresh owned test process")
        if getattr(bridge, "_project_auto_restore_result", None) is not None:
            raise RuntimeError("This process already restored project data; isolation cannot undo it")
        schedule = getattr(bridge, "_schedule_project_auto_activation", None)
        cancel = getattr(bridge, "_cancel_project_auto_activation", None)
        if not callable(schedule) or not callable(cancel):
            raise RuntimeError("The bridge does not expose its bounded auto-activation lifecycle")
        cancel()
        self.isolation_bridge = bridge
        self.isolation_original_schedule = schedule
        self.isolation_schedule = lambda: None
        bridge._schedule_project_auto_activation = self.isolation_schedule
        self.result["autoRestoreIsolation"].update({"installed": True,
            "captureWasUnloaded": True, "restoreClientWasInactive": True,
            "bridgePath": str(getattr(bridge, "__file__", "")),
            "scope": "owned process callback only; no project configuration or assets modified"})

    def native_mutation_generation(self):
        values = []
        pyrenderdoc.Replay().BlockInvoke(
            lambda controller: values.append(int(controller.GetShaderReplacementGeneration(0))))
        if len(values) != 1:
            raise RuntimeError("The native mutation-generation query did not complete")
        return values[0]

    def finish_auto_restore_isolation(self):
        bridge = self.isolation_bridge
        if bridge is None:
            return
        observation = self.result["autoRestoreIsolation"]
        errors = []
        try:
            if pyrenderdoc.IsCaptureLoaded():
                try:
                    observation["generationEnd"] = self.native_mutation_generation()
                    observation["generationStable"] = (
                        observation.get("generationBefore") == observation["generationEnd"])
                    if not observation["generationStable"]:
                        errors.append("Native mutation generation changed during isolated history validation")
                except Exception as exc:
                    errors.append(str(exc))
                finally:
                    pyrenderdoc.CloseCapture()
            observation["captureClosed"] = not pyrenderdoc.IsCaptureLoaded()
            if not observation["captureClosed"]:
                errors.append("The owned isolated capture did not close")
        except Exception as exc:
            errors.append(str(exc))
        finally:
            try:
                bridge._cancel_project_auto_activation()
            except Exception as exc:
                errors.append(str(exc))
            # Refuse to overwrite any unrelated callback installed while this fixture was active.
            if bridge._schedule_project_auto_activation is self.isolation_schedule:
                bridge._schedule_project_auto_activation = self.isolation_original_schedule
                observation["callbackRestored"] = True
            else:
                observation["callbackRestored"] = False
                errors.append("The auto-activation callback changed outside this fixture")
            self.isolation_bridge = None
        if errors:
            observation["cleanupErrors"] = errors
            raise RuntimeError("; ".join(errors))

    def publish_progress(self, phase):
        if self.output:
            with open(self.output + ".progress.json", "w", encoding="utf-8") as stream:
                json.dump({"phase": phase, "pid": os.getpid(), "time": time.perf_counter()}, stream)

    def exposure_diagnostics(self, host):
        point = host.mapToGlobal(host.rect().center())
        result = {"activeWindow": self.describe_widget(self.app.activeWindow()),
                  "testWindowActive": self.test_window.isActiveWindow(),
                  "lastMouseRoute": self.mouse_routes[-1],
                  "hostVisibleRegion": self.describe_rect(host.visibleRegion().boundingRect()),
                  "topLevelAt": self.describe_widget(self.app.topLevelAt(point)),
                  "windowChildAt": self.describe_widget(self.test_window.childAt(
                      self.test_window.mapFromGlobal(point))),
                  "devicePixelRatio": self.test_window.devicePixelRatioF()}
        if self.output:
            origin = self.test_window.mapToGlobal(QtCore.QPoint(0, 0))
            pixmap = self.test_screen.grabWindow(0, origin.x(), origin.y(),
                                               self.test_window.width(), self.test_window.height())
            path = os.path.splitext(self.output)[0] + ".window-exposure.png"
            if pixmap.save(path):
                result["windowImage"] = path
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.WindowFromPoint.argtypes = [wintypes.POINT]
            user32.WindowFromPoint.restype = wintypes.HWND
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            native = []
            for scale in sorted(set([1.0, self.test_window.devicePixelRatioF()])):
                hwnd = user32.WindowFromPoint(wintypes.POINT(int(point.x() * scale), int(point.y() * scale)))
                process_id, class_name = wintypes.DWORD(), ctypes.create_unicode_buffer(256)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
                user32.GetClassNameW(hwnd, class_name, len(class_name))
                native.append({"scale": scale, "hwnd": int(hwnd or 0),
                               "pid": process_id.value, "class": class_name.value})
            result["nativePointWindows"] = native
        return result

    @staticmethod
    def describe_rect(rect):
        return [rect.x(), rect.y(), rect.width(), rect.height()]

    def advance(self, phase):
        self.phase = phase
        self.deadline = time.perf_counter() + self.timeout
        self.publish_progress(phase)
        self.update_stack_sampling()

    def update_stack_sampling(self):
        if not self.stack_sampling_requested:
            return
        active = self.phase in ("initial", "warm", "single", "scroll") and not self.finished
        if self.stack_file is None and active:
            if not self.output:
                raise RuntimeError("Python stack sampling requires an explicit report path")
            import faulthandler
            self.stack_module = faulthandler
            path = self.output + ".python-stacks.log"
            self.stack_file = open(path, "w", encoding="utf-8")
            evidence = {"file": path, "intervalSeconds": 2.0, "nativeWatchdog": True,
                        "guiPythonThreadIdent": threading.get_ident(),
                        "guiPythonThreadHex": hex(threading.get_ident()), "periods": []}
            if os.name == "nt":
                import ctypes
                evidence["guiNativeThreadId"] = int(ctypes.windll.kernel32.GetCurrentThreadId())
            self.result["pythonStackSampling"] = evidence
        if self.stack_file is not None:
            marker = {"phase": self.phase, "active": active, "time": time.perf_counter()}
            self.result["pythonStackSampling"]["periods"].append(marker)
            self.stack_file.write("\nPHASE " + json.dumps(marker, sort_keys=True) + "\n")
            self.stack_file.flush()
        if active and not self.stack_sampling:
            # This is a native watchdog, not another Python QTimer/thread that could itself wait
            # behind the blocked GIL. Do not restart it on each heartbeat and distort the test.
            self.stack_module.dump_traceback_later(2.0, repeat=True, file=self.stack_file, exit=False)
            self.stack_sampling = True
        elif not active and self.stack_sampling:
            self.stack_module.cancel_dump_traceback_later()
            self.stack_sampling = False

    def stop_stack_sampling(self):
        if self.stack_sampling:
            self.stack_module.cancel_dump_traceback_later()
            self.stack_sampling = False
        if self.stack_file is not None:
            self.stack_file.flush()
            self.stack_file.close()
            self.stack_file = None
            self.result["pythonStackSampling"]["closed"] = True

    def hold(self, widget):
        if widget is not None and id(widget) not in self.kept_ids:
            self.kept_ids.add(id(widget))
            self.keep.append(widget)
        return widget

    def describe_widget(self, widget):
        if widget is None:
            return None
        self.hold(widget)
        try:
            origin = widget.mapToGlobal(QtCore.QPoint(0, 0))
            return {"class": widget.metaObject().className(), "name": widget.objectName(),
                    "visible": widget.isVisible(),
                    "globalRect": [origin.x(), origin.y(), widget.width(), widget.height()]}
        except Exception as exc:
            return {"error": str(exc)}

    def fit_test_window(self):
        available = self.test_screen.availableGeometry()
        frame, client = self.test_window.frameGeometry(), self.test_window.geometry()
        # Geometry excludes the native frame. Reserve its observed margins and an extra inset
        # instead of assuming showMaximized() can override a persisted layout minimum height.
        left = max(0, client.left() - frame.left())
        top = max(0, client.top() - frame.top())
        right = max(0, frame.right() - client.right())
        bottom = max(0, frame.bottom() - client.bottom())
        target = available.adjusted(12 + left, 12 + top, -12 - right, -12 - bottom)
        if target.width() < 640 or target.height() < 480:
            raise RuntimeError("Available screen area is too small for the history fixture")
        if self.test_window.geometry() != target:
            self.test_window.setGeometry(target)
        self.result["windowLayout"] = {
            "available": [available.x(), available.y(), available.width(), available.height()],
            "targetClient": [target.x(), target.y(), target.width(), target.height()],
            "actualWindow": self.describe_widget(self.test_window),
            "minimumSize": [self.test_window.minimumWidth(), self.test_window.minimumHeight()]}

    def fit_history_area(self):
        self.fit_test_window()
        if self.history_splitter is None:
            candidates = [self.hold(value) for value in self.widget.findChildren(QtWidgets.QSplitter)]
            for value in candidates:
                first = self.hold(value.widget(0)) if value.count() else None
                if (value.orientation() == QtCore.Qt.Vertical and first is not None
                        and first.objectName() == "dockarea"):
                    self.history_splitter = value
                    break
            if self.history_splitter is None:
                raise RuntimeError("Texture Viewer main/history splitter was not found")
            self.saved_splitter_limits = (self.history_splitter.minimumHeight(), self.history_splitter.maximumHeight())
            self.saved_splitter_sizes = list(self.history_splitter.sizes())
        splitter = self.history_splitter
        origin = splitter.mapToGlobal(QtCore.QPoint(0, 0))
        maximum = self.test_screen.availableGeometry().bottom() - origin.y() - 16
        if maximum < 410:
            raise RuntimeError("History splitter has less than 410 on-screen pixels; see windowLayout")
        # Constrain this existing test area, preserving the image card's real fixed size and the
        # real native render widget. No alternate image or invisible mouse receiver is substituted.
        if splitter.maximumHeight() != maximum:
            splitter.setMaximumHeight(maximum)
        if self.history is not None and self.history.isVisible() and splitter.count() == 2:
            signature = (origin.x(), origin.y(), splitter.width(), splitter.height(), maximum)
            if signature != self.layout_signature:
                self.layout_signature = signature
                usable = max(0, splitter.height() - splitter.handleWidth())
                splitter.setSizes([max(180, usable - 230), 230])
        self.result["windowLayout"]["historySplitter"] = self.describe_widget(splitter)
        self.result["windowLayout"]["splitterSizes"] = list(splitter.sizes())

    def screen_contains(self, widget, point):
        position = widget.mapToGlobal(point)
        return self.test_screen.availableGeometry().contains(position)

    def mouse_route(self, host, point=None):
        self.hold(host)
        point = point or QtCore.QPoint(host.width() // 2, min(35, host.height() // 2))
        global_point = host.mapToGlobal(point)
        # Hold every wrapper immediately, before traversing. PySide wrappers returned by
        # parentWidget() can own another wrapper's lifetime even while C++ retains the widget.
        receiver = self.hold(self.app.widgetAt(global_point))
        route_method = "QApplication.widgetAt"
        if (receiver is None and self.test_window is not None
                and (self.output_witness or self.test_window.isActiveWindow())
                and host.visibleRegion().contains(point)):
            # Some native child-window/platform combinations cannot resolve topLevelAt through
            # QApplication. Output-witness mode deliberately verifies native Qt input, not OS
            # focus/input routing. Resolve the real child and ancestors even on a locked desktop;
            # desktop-witness mode still requires the top-level window to be active.
            receiver = self.hold(self.test_window.childAt(self.test_window.mapFromGlobal(global_point)))
            route_method = ("output-witness native QWidget.childAt" if self.output_witness
                            else "active top-level QWidget.childAt")
        route = {"phase": self.phase, "host": self.describe_widget(host),
                 "receiver": self.describe_widget(receiver),
                 "globalPoint": [global_point.x(), global_point.y()], "ancestors": [],
                 "routeMethod": route_method}
        ancestor = receiver
        belongs = receiver is not None and (receiver == host or host.isAncestorOf(receiver))
        while ancestor is not None:
            route["ancestors"].append(self.describe_widget(ancestor))
            if ancestor == host:
                break
            ancestor = self.hold(ancestor.parentWidget())
        route["belongsToHost"] = belongs
        self.mouse_routes.append(route)
        self.mouse_routes = self.mouse_routes[-20:]
        return receiver, global_point, belongs

    def mouse(self, host, kind, point=None):
        releasing = kind == QtCore.QEvent.MouseButtonRelease
        if releasing:
            # Qt implicitly grabs the press receiver until the final button release. Opening
            # History may move the resource link during its press handler; the physical mouse
            # has not moved, and release still belongs to that original receiver.
            if self.mouse_grab is None:
                raise RuntimeError("Mouse release has no matching validated press receiver")
            receiver, global_point, press_host = self.mouse_grab
            self.mouse_grab = None
            if press_host != host:
                raise RuntimeError("Mouse release host differs from the matching press")
            self.mouse_routes.append({"phase": self.phase, "mouseKind": "grabbed-release",
                                      "host": self.describe_widget(host),
                                      "receiver": self.describe_widget(receiver),
                                      "globalPoint": [global_point.x(), global_point.y()],
                                      "usesValidatedPressReceiver": True})
            self.mouse_routes = self.mouse_routes[-20:]
        else:
            # Each press, including the second press represented by MouseButtonDblClick,
            # independently hits the actual widget under the image-area point. A native child
            # or an obscuring widget is never bypassed by the release-only grab emulation.
            receiver, global_point, belongs = self.mouse_route(host, point)
            if receiver is None or not belongs:
                raise RuntimeError("No unobscured Qt mouse receiver belongs to the host; see mouseRoutes")
            self.mouse_grab = (self.hold(receiver), global_point, self.hold(host))
            self.mouse_routes[-1]["mouseKind"] = ("double-click" if kind == QtCore.QEvent.MouseButtonDblClick
                                                 else "press")
        local = receiver.mapFromGlobal(global_point)
        buttons = QtCore.Qt.NoButton if releasing else QtCore.Qt.LeftButton
        event = QtGui.QMouseEvent(kind, QtCore.QPointF(local), QtCore.QPointF(global_point),
                                 QtCore.Qt.LeftButton, buttons, QtCore.Qt.NoModifier)
        QtWidgets.QApplication.sendEvent(receiver, event)
        return receiver

    def click(self, host, double=False, point=None):
        self.mouse(host, QtCore.QEvent.MouseButtonPress, point)
        self.mouse(host, QtCore.QEvent.MouseButtonRelease, point)
        if double:
            self.mouse(host, QtCore.QEvent.MouseButtonDblClick, point)
            self.mouse(host, QtCore.QEvent.MouseButtonRelease, point)

    def surface(self, label, save=False):
        if self.output_witness and self.render.isHidden():
            return {"label": label, "time": time.perf_counter(), "renderHidden": True,
                    "renderVisible": self.render.isVisible(), "sha256": None, "imageSize": None,
                    "pending": self.render.property("eidPresentationPending"),
                    "displayedEvent": self.render.property("eidDisplayedEvent"),
                    "displayedResource": self.render.property("eidDisplayedResource"),
                    "coverage": "hidden output, no image sampled"}
        # QScreen.grabWindow(0, ...) reads the actual desktop surface, including native GPU child
        # windows. QWidget.grab() and resource readbacks would not prove which tab was visible.
        # Cover the complete render interior. A corner-only crop can contain nothing except
        # letterboxing even when the actual current and historical images differ elsewhere.
        interior = self.render.rect().adjusted(2, 2, -2, -2)
        origin = self.render.mapToGlobal(interior.topLeft())
        width, height = interior.width(), interior.height()
        if width < 16 or height < 16:
            raise RuntimeError("Texture surface is too small for a real screen witness")
        screens = [self.hold(screen) for screen in QtGui.QGuiApplication.screens()]
        screen = next((screen for screen in screens if screen.geometry().contains(origin)), None)
        bounds = QtCore.QRect(origin, QtCore.QSize(width, height))
        if screen is None or not screen.availableGeometry().contains(bounds):
            raise RuntimeError("Complete texture surface is outside the available screen area")
        if not self.render.isHidden():
            if not self.render.isVisible():
                raise RuntimeError("Texture surface has a hidden Qt ancestor")
            # The CustomPaintWidget owns an opaque native render child. visibleRegion() on its
            # parent excludes that child, so it is not a witness for the child window's visibility.
            # Check ancestor clipping explicitly; the full desktop image below remains the witness.
            ancestor = self.hold(self.render.parentWidget())
            while ancestor is not None:
                clipped = QtCore.QRect(ancestor.mapFromGlobal(origin), QtCore.QSize(width, height))
                if not ancestor.rect().contains(clipped):
                    self.result["windowExposure"] = self.exposure_diagnostics(self.render)
                    raise RuntimeError("Complete texture surface is clipped by a Qt ancestor")
                ancestor = self.hold(ancestor.parentWidget())
        if self.output_witness:
            if not QtCore.QMetaObject.invokeMethod(self.widget, "CapturePresentationWitness", QtCore.Qt.DirectConnection):
                raise RuntimeError("Opt-in output witness slot is unavailable in this qrenderdoc build")
            witness = self.widget.property("eidPresentationWitness")
            if not isinstance(witness, QtGui.QImage) or witness.isNull():
                raise RuntimeError("Existing output window did not return a valid native image")
            image = witness.convertToFormat(QtGui.QImage.Format_RGBA8888)
            self.widget.setProperty("eidPresentationWitness", None)
        else:
            pixmap = screen.grabWindow(0, origin.x(), origin.y(), width, height)
            image = pixmap.toImage().convertToFormat(QtGui.QImage.Format_RGBA8888)
        if image.isNull():
            raise RuntimeError("Native surface screenshot failed")
        raw = bytes(image.constBits())
        if len(raw) != image.byteCount():
            raise RuntimeError("Native surface screenshot returned an incomplete buffer")
        # This conservative second witness is only used to reject inconclusive references.
        # Equal solid images must not appear different merely because the history panel changed
        # the render dimensions. Completion still requires the original full-resolution hash.
        comparison = image.scaled(128, 128, QtCore.Qt.IgnoreAspectRatio,
                                  QtCore.Qt.FastTransformation)
        comparison_raw = bytes(comparison.constBits())
        # Save only the two acceptance references. Identical texture content in differently sized
        # canvases must not become a supposed different historical image through letterboxing.
        if label == "current-reference":
            self.current_reference_image = image.copy()
        elif label == "history-reference":
            self.history_reference_image = image.copy()
        blank_rgb = not any(raw[0::4]) and not any(raw[1::4]) and not any(raw[2::4])
        sample = {"label": label, "time": time.perf_counter(),
                  "rect": [origin.x(), origin.y(), width, height],
                  "imageSize": [image.width(), image.height()],
                  "sha256": hashlib.sha256(raw).hexdigest(),
                  "fullFrameComparisonSha256": hashlib.sha256(comparison_raw).hexdigest(),
                  "blankRGB": blank_rgb,
                  "coverage": ("complete existing output window GPU pixels; no Display or rebinding, not OS scanout"
                               if self.output_witness else "complete render interior, native desktop pixels"),
                  "renderVisible": self.render.isVisible(),
                  "renderHidden": self.render.isHidden(),
                  "pending": self.render.property("eidPresentationPending"),
                  "serial": self.render.property("eidPresentationSerial"),
                  "displayedEvent": self.render.property("eidDisplayedEvent"),
                  "displayedResource": self.render.property("eidDisplayedResource")}
        if save and self.output:
            self.surface_counter += 1
            path = os.path.splitext(self.output)[0] + ".surface-%02d-%s.png" % (self.surface_counter, label)
            if not image.save(path):
                raise RuntimeError("Could not save native surface evidence")
            sample["image"] = path
        if len(self.surface_samples) < 512:
            self.surface_samples.append(sample)
        return sample

    def prepare_tab_checks(self):
        self.current_tab = self.hold(self.widget.findChild(QtWidgets.QWidget, "renderContainer"))
        ancestor = self.hold(self.render.parentWidget())
        ancestry = []
        while ancestor is not None:
            ancestry.append({"widget": self.describe_widget(ancestor),
                             "pythonType": str(type(ancestor)),
                             "hasIndexOf": hasattr(ancestor, "indexOf"),
                             "inheritsTabWidget": ancestor.inherits("QTabWidget")})
            if isinstance(ancestor, QtWidgets.QStackedWidget) and ancestor.indexOf(self.current_tab) >= 0:
                break
            ancestor = self.hold(ancestor.parentWidget())
        self.result["tabAncestry"] = ancestry
        if ancestor is None:
            self.result["windowExposure"] = self.exposure_diagnostics(self.render)
            raise RuntimeError("Shared texture tab area was not found")
        self.texture_tabs = ancestor
        area = self.hold(ancestor.parentWidget())
        if not area.inherits("QTabWidget"):
            raise RuntimeError("Texture stack does not belong to the actual tab area")
        bars = [self.hold(child) for child in area.findChildren(QtWidgets.QWidget)
                if child.inherits("QTabBar") and child.parentWidget() == area]
        if len(bars) != 1:
            raise RuntimeError("Actual texture tab bar was not found uniquely")
        self.texture_tab_bar = bars[0]
        self.history_tab = self.hold(ancestor.currentWidget())
        if self.history_tab == self.current_tab:
            raise RuntimeError("History was not attached to its own locked tab")
        self.history_surface = self.surface("history-reference", True)
        overlap_size = QtCore.QSize(min(self.current_reference_image.width(),
                                      self.history_reference_image.width()),
                                   min(self.current_reference_image.height(),
                                       self.history_reference_image.height()))
        current_overlap = self.current_reference_image.copy(QtCore.QRect(QtCore.QPoint(), overlap_size))
        history_overlap = self.history_reference_image.copy(QtCore.QRect(QtCore.QPoint(), overlap_size))
        overlap_equal = bytes(current_overlap.constBits()) == bytes(history_overlap.constBits())
        self.result["tabReferenceOverlap"] = {
            "size": [overlap_size.width(), overlap_size.height()],
            "equal": overlap_equal,
            "purpose": "Reject identical visible content differing only in canvas extent"}
        if (self.history_surface["sha256"] == self.current_surface["sha256"] or
                self.history_surface["fullFrameComparisonSha256"] ==
                self.current_surface["fullFrameComparisonSha256"] or overlap_equal):
            self.result["inconclusive"] = True
            raise RuntimeError("Current and history full-frame screen witnesses are indistinguishable; "
                               "tab pixel test is inconclusive")
        self.tab_cases = ["tab-current", "tab-rapid-current", "tab-delayed-completion-current"]
        self.begin_current_tab_case()

    def click_tab(self, tab):
        index = self.texture_tabs.indexOf(tab)
        if index < 0:
            raise RuntimeError("Texture tab disappeared")
        if hasattr(self.texture_tab_bar, "tabRect"):
            self.result["tabInput"] = "Qt mouse events using actual tabRect"
            point = self.texture_tab_bar.tabRect(index).center()
            self.click(self.texture_tab_bar, point=point)
        else:
            # Custom C++ subclasses can be exposed as QWidget by this bundled PySide version.
            # Use their real QTabBar keyboard handler, never a private callback/setCurrentIndex.
            self.result["tabInput"] = "Qt tab-bar key events; custom wrapper has no tabRect"
            for unused in range(self.texture_tabs.count()):
                previous = self.texture_tabs.currentIndex()
                if previous == index:
                    break
                key = QtCore.Qt.Key_Right if index > previous else QtCore.Qt.Key_Left
                for kind in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease):
                    QtWidgets.QApplication.sendEvent(self.texture_tab_bar,
                        QtGui.QKeyEvent(kind, key, QtCore.Qt.NoModifier))
                if self.texture_tabs.currentIndex() == previous:
                    raise RuntimeError("Actual tab-bar key input did not advance selection")
        if self.texture_tabs.currentWidget() != tab:
            raise RuntimeError("Actual tab-bar input did not select its tab")

    def assert_suspended(self):
        if (not self.render.isHidden() or not self.render.property("eidPresentationPending")
                or self.render.property("eidDisplayedEvent") is not None
                or self.render.property("eidDisplayedResource") is not None):
            raise RuntimeError("Tab switch left the old native surface visible or certified as current")

    def begin_current_tab_case(self):
        self.tab_case = self.tab_cases.pop(0)
        self.tab_started = time.perf_counter()
        if self.tab_case == "tab-delayed-completion-current":
            entered, release = threading.Event(), threading.Event()
            self.replay_release = release
            # The GUI remains inside this callback until a worker barrier after the historical
            # display enters. Thus the old display has executed, but its GUI completion cannot
            # have been delivered yet. Switching tabs here must invalidate that completion.
            self.click_tab(self.history_tab)

            def barrier(controller):
                entered.set()
                release.wait(min(self.timeout, 10.0))

            pyrenderdoc.Replay().AsyncInvoke(barrier)
            if not entered.wait(min(self.timeout, 10.0)):
                release.set()
                raise RuntimeError("Controlled replay barrier did not execute after history display")
            self.result["delayedCompletionEvidence"] = {
                "workerBarrierEnteredAfterHistoryDisplay": True,
                "guiEventLoopWasNotPumped": True,
                "waitMs": (time.perf_counter() - self.tab_started) * 1000.0}
            self.tab_started = time.perf_counter()
            try:
                self.click_tab(self.current_tab)
                self.assert_suspended()
            finally:
                release.set()
                self.replay_release = None
        else:
            self.click_tab(self.current_tab)
            self.assert_suspended()
            if self.tab_case == "tab-rapid-current":
                self.click_tab(self.history_tab)
                self.assert_suspended()
                self.click_tab(self.current_tab)
                self.assert_suspended()
        self.tab_immediate = self.surface(self.tab_case + "-pending", True)
        self.tab_stable = 0
        self.tab_last_surface = 0.0
        self.advance("tab_current")

    def observe_current_tab(self, now):
        if int(pyrenderdoc.CurEvent()) != self.before_click:
            raise RuntimeError("Texture tab switching changed the global EID")
        if now - self.tab_last_surface < 0.02:
            return
        self.tab_last_surface = now
        sample = self.surface(self.tab_case)
        if self.render.isHidden():
            return
        if sample["pending"]:
            # The tab may reveal only after a real GPU clear, allowing Qt to deliver native-child
            # resize before final Display. A readback FIFO may also run after final Display but
            # before its GUI completion; accept only the exact known current image in that race.
            try:
                sample["pendingEvidence"] = _pending_surface_evidence(sample, self.current_surface)
            except RuntimeError:
                self.surface(self.tab_case + "-invalid-pending", True)
                raise
            return
        if (int(sample["displayedEvent"] or 0) != self.before_click
                or str(sample["displayedResource"]) != self.current_resource or sample["pending"]):
            raise RuntimeError("Current tab revealed a surface belonging to a previous request")
        if sample["sha256"] == self.history_surface["sha256"]:
            self.surface(self.tab_case + "-wrong-history", True)
            raise RuntimeError("Actual current-tab screen surface still shows the historical image")
        if (sample["imageSize"] == self.current_surface["imageSize"]
                and sample["sha256"] == self.current_surface["sha256"]):
            self.tab_stable += 1
        else:
            self.tab_stable = 0
        if self.tab_stable < 2:
            return
        self.result["cases"].append({"name": self.tab_case, "ok": True,
                                      "elapsedMs": (now - self.tab_started) * 1000.0,
                                      "immediate": self.tab_immediate,
                                      "currentSurface": self.surface(self.tab_case + "-complete", True)})
        if self.tab_cases and self.tab_cases[0] == "tab-delayed-completion-current":
            self.begin_current_tab_case()
            return
        self.click_tab(self.history_tab)
        self.assert_suspended()
        self.advance("tab_history")

    def observe_history_tab(self):
        if not self.displayed(self.click_event) or self.render.isHidden():
            return
        sample = self.surface("history-return")
        if sample["sha256"] != self.history_surface["sha256"]:
            return
        if self.tab_cases:
            self.begin_current_tab_case()
            return
        matches = [card for card in self.cards() if int(card.property("historyEventId")) == self.click_event]
        if len(matches) != 1:
            raise RuntimeError("Selected history card was not restored after tab switching")
        self.card = self.hold(matches[0])
        self.advance("double")
        self.click(self.card, double=True)

    def prepare(self):
        if self.isolation_bridge is not None:
            self.result["autoRestoreIsolation"]["generationBefore"] = self.native_mutation_generation()
        self.original = int(pyrenderdoc.CurEvent())
        self.anchor = int(os.environ.get("RENDERDOC_HISTORY_SMOKE_EID", str(self.original)))
        if self.anchor <= 0:
            raise RuntimeError("Choose a positive draw EID using RENDERDOC_HISTORY_SMOKE_EID")
        pyrenderdoc.SetEventID([], self.anchor, self.anchor, True)
        pyrenderdoc.ShowTextureViewer()
        self.viewer = pyrenderdoc.GetTextureViewer()
        self.widget = self.viewer.Widget()
        # A persisted window rectangle can extend below the current monitor. A card may then
        # pass Qt's child visibility checks while no screen can receive the test mouse event.
        # Fit only this owned test window and restore its saved geometry before closing it.
        self.test_window = self.hold(self.widget.window())
        self.saved_window_geometry = self.test_window.saveGeometry()
        self.saved_window_minimum = QtCore.QSize(self.test_window.minimumSize())
        self.window_layout = self.hold(self.test_window.layout())
        if self.window_layout is not None:
            self.saved_window_constraint = self.window_layout.sizeConstraint()
            self.window_layout.setSizeConstraint(QtWidgets.QLayout.SetNoConstraint)
        screens = [self.hold(screen) for screen in QtGui.QGuiApplication.screens()]
        centre = self.test_window.frameGeometry().center()
        self.test_screen = next((screen for screen in screens if screen.geometry().contains(centre)),
                                screens[0] if screens else None)
        if self.test_screen is None:
            raise RuntimeError("No screen is available for actual history mouse/surface checks")
        self.test_window.showNormal()
        self.test_window.setMinimumSize(0, 0)
        self.fit_test_window()
        # A background launcher must not leave this owned fixture behind another application.
        # Native widgetAt and desktop-pixel witnesses deliberately reject an occluded window.
        self.test_window.raise_()
        self.test_window.activateWindow()
        self.render = self.widget.findChild(QtWidgets.QWidget, "render")
        self.outputs = self.widget.findChild(QtWidgets.QWidget, "outputThumbs")
        self.keep.extend([self.viewer, self.widget, self.render, self.outputs])
        targets = list(pyrenderdoc.CurPipelineState().GetOutputTargets())
        wanted = os.environ.get("RENDERDOC_HISTORY_SMOKE_TEXTURE", "").strip()
        wanted = str(int(wanted)) if wanted else None
        found = [(index, target.resource) for index, target in enumerate(targets)
                 if target.resource != rd.ResourceId.Null()
                 and (wanted is None or str(target.resource).split("::")[-1] == wanted)]
        if not found:
            raise RuntimeError("No requested actual bound color target at the anchor EID")
        self.slot, self.resource = found[0]
        self.mip = max(0, int(targets[self.slot].firstMip))
        self.slot_name = str(pyrenderdoc.CurPipelineState().OutputAbbrev()) + str(self.slot)
        self.result.update({"capture": str(pyrenderdoc.GetCaptureFilename()), "anchorEvent": self.anchor,
                            "resource": str(self.resource), "outputSlot": self.slot,
                            "maxAllowedLoadingHeartbeatMs": self.max_gap,
                            "completionEvidence": "actual Qt image-area events, live global event, and Texture Viewer properties set after Display returns; not OS scanout"})
        self.usage = None
        resource = self.resource

        def query(controller):
            usage = list(controller.GetUsage(resource))
            allowed = {rd.ResourceUsage.ColorTarget, rd.ResourceUsage.DepthStencilTarget,
                       rd.ResourceUsage.Clear, rd.ResourceUsage.Discard, rd.ResourceUsage.Resolve,
                       rd.ResourceUsage.ResolveDst, rd.ResourceUsage.Copy, rd.ResourceUsage.CopyDst}
            if self.mip > 0:
                allowed.add(rd.ResourceUsage.GenMips)
            candidates = sorted(set(int(item.eventId) for item in usage if int(item.eventId) > 0
                                    and (item.usage in allowed
                                         or rd.ResourceUsage.VS_RWResource <= item.usage <= rd.ResourceUsage.All_RWResource)))
            self.usage = candidates

        pyrenderdoc.Replay().AsyncInvoke(query)
        self.advance("open")

    def open_history(self):
        children = [self.hold(child) for child in self.outputs.findChildren(QtWidgets.QWidget)]
        previews = [child for child in children if child.metaObject().className() == "ResourcePreview"]
        matches = []
        for preview in previews:
            slot = self.hold(preview.findChild(QtWidgets.QLabel, "slotLabel"))
            label = self.hold(preview.findChild(QtWidgets.QLabel, "descriptionLabel"))
            if slot is not None and label is not None and slot.text() == self.slot_name:
                matches.append((preview, slot, label))
        if len(matches) != 1:
            raise RuntimeError("Cannot find unique actual output preview " + self.slot_name)
        self.preview, self.slot_label, self.link = matches[0]
        self.keep.extend(matches[0])
        self.output_scroll = self.hold(self.outputs.findChild(QtWidgets.QScrollArea, "scrollArea"))
        if self.output_scroll is None:
            raise RuntimeError("Output thumbnail scroll area is unavailable")
        self.output_scroll_content = self.hold(self.output_scroll.widget())
        self.output_scroll_viewport = self.hold(self.output_scroll.viewport())
        self.output_horizontal_bar = self.hold(self.output_scroll.horizontalScrollBar())
        self.output_vertical_bar = self.hold(self.output_scroll.verticalScrollBar())
        # A bound output can be below the visible strip (e.g. RT slot 5). Move that actual
        # resource link into view before the stable-layout and real mouse-hit checks.
        self.output_scroll.ensureWidgetVisible(self.link, 20, 20)
        if not self.link.isVisible() or not self.link.font().underline():
            raise RuntimeError("The actual output history link is not visible/enabled")
        self.anchor_index = max([index for index, event in enumerate(self.usage) if event <= self.anchor] or [0])
        self.result["candidateEvents"] = self.usage
        self.result["expectedAnchorIndex"] = self.anchor_index
        self.link_layout_signature = None
        self.link_layout_stable_ticks = 0
        self.link_layout_started = time.perf_counter()
        self.advance("link_layout")

    def diagnostics(self):
        if self.history is None:
            return {"phase": self.phase}
        names = ["historyRevisionCount", "historyCachedCount", "historyLiveWidgets",
                 "historySelectedEvent", "historySelectedIndex", "historyPendingEvent",
                 "historyRequestSerial", "historyRequestedEvents",
                 "historyValidationPending", "historyCacheMutationGeneration"]
        state = {name: self.history.property(name) for name in names}
        state.update({"phase": self.phase, "currentEvent": int(pyrenderdoc.CurEvent()),
                      "displayedEvent": self.render.property("eidDisplayedEvent"),
                      "displayedResource": self.render.property("eidDisplayedResource")})
        return state

    def cards(self):
        cards = [self.hold(card) for card in self.history.findChildren(QtWidgets.QWidget, "historyRevision")]
        return [card for card in cards if card.isVisible()]

    def displayed(self, event):
        return (int(self.render.property("eidDisplayedEvent") or 0) == event
                and str(self.render.property("eidDisplayedResource")) == str(self.resource))

    def observe(self, now):
        if self.last_tick is not None:
            gap = (now - self.last_tick) * 1000.0
            self.gaps.append(gap)
            if gap > 50.0 and len(self.slow_gaps) < 128:
                self.slow_gaps.append({"phase": self.phase, "previousPhase": self.previous_tick_phase,
                                       "gapMs": gap, "previousTickWallMs": self.previous_tick_wall_ms,
                                       "pendingEvent": (self.history.property("historyPendingEvent")
                                                        if self.history is not None else None)})
            if self.phase in ("initial", "warm", "scroll", "single"):
                self.loading_gaps.append(gap)
        self.last_tick = now
        if self.history is not None:
            state = self.diagnostics()
            if len(self.samples) < 4096:
                self.samples.append(state)
            if int(state["historyCachedCount"] or 0) > 24 or int(state["historyLiveWidgets"] or 0) > 24:
                raise RuntimeError("History exceeded the 24-image/widget bound")

    def tick(self):
        if self.finished:
            return
        tick_started = time.perf_counter()
        tick_phase = self.phase
        try:
            now = time.perf_counter()
            self.observe(now)
            if now > self.deadline:
                raise RuntimeError("Timed out in history phase " + self.phase)
            if self.phase == "loading":
                if pyrenderdoc.IsCaptureLoaded():
                    self.prepare()
            elif self.phase == "open":
                if self.usage is not None:
                    if len(self.usage) < 2:
                        raise RuntimeError("History test needs at least two write candidates")
                    self.open_history()
            elif self.phase == "link_layout":
                self.fit_history_area()
                # Reapply after any deferred thumbnail-strip layout/range update. This only
                # scrolls the existing resource link; it never bypasses widgetAt validation.
                self.output_scroll.ensureWidgetVisible(self.link, 20, 20)
                receiver, unused_point, belongs = self.mouse_route(self.link)
                signature = (self.describe_widget(self.link), self.describe_widget(receiver))
                if signature == self.link_layout_signature and belongs:
                    self.link_layout_stable_ticks += 1
                else:
                    self.link_layout_signature = signature
                    self.link_layout_stable_ticks = 0
                if (not belongs or self.link_layout_stable_ticks < 3
                        or now - self.link_layout_started < 0.15):
                    if not belongs and now - self.link_layout_started > 5.0:
                        self.result["windowExposure"] = self.exposure_diagnostics(self.link)
                        raise RuntimeError("History link remains occluded or outside native hit testing")
                    return
                if self.tab_checks:
                    self.current_resource = str(self.viewer.GetCurrentResource())
                    if (int(self.render.property("eidDisplayedEvent") or 0) != self.anchor
                            or str(self.render.property("eidDisplayedResource")) != self.current_resource):
                        return
                    candidate = self.surface("current-reference-check")
                    previous = getattr(self, "current_surface_candidate", None)
                    if (previous is None or candidate["sha256"] != previous["sha256"]
                            or candidate["rect"] != previous["rect"]):
                        self.current_surface_candidate = candidate
                        self.current_surface_stable_since = now
                        return
                    if now - self.current_surface_stable_since < 0.05:
                        return
                    self.current_surface = self.surface("current-reference", True)
                self.last_tick = time.perf_counter()
                self.advance("initial")
                self.click(self.link)
            elif self.phase == "initial":
                if self.history is None:
                    self.history = self.widget.findChild(QtWidgets.QWidget, "renderTargetHistoryView")
                    if self.history is None:
                        return
                    self.keep.append(self.history)
                    self.scroll = self.history.findChild(QtWidgets.QScrollArea, "renderTargetHistoryScroll")
                    self.scrollbar = self.scroll.horizontalScrollBar()
                    self.keep.extend([self.scroll, self.scrollbar])
                self.fit_history_area()
                state = self.diagnostics()
                requested = state["historyRequestedEvents"] or []
                if not requested or not int(state["historyCachedCount"] or 0):
                    return
                first = int(requested[0])
                first_index = self.usage.index(first)
                if abs(first_index - self.anchor_index) > 12 or (self.anchor_index > 12 and first_index == 0):
                    raise RuntimeError("First thumbnail request did not start in the selected neighborhood")
                if int(state["historyRevisionCount"]) != len(self.usage):
                    raise RuntimeError("History candidate count differs from actual resource usage")
                self.result["cases"].append({"name": "anchor-neighborhood-first", "ok": True,
                                              "firstRequestEvent": first, "firstRequestIndex": first_index,
                                              "firstImageElapsedMs": sum(self.loading_gaps), "state": state})
                self.advance("warm")
            elif self.phase == "warm":
                self.fit_history_area()
                state = self.diagnostics()
                if int(state["historyPendingEvent"] or 0):
                    return
                ready = [card for card in self.cards() if card.property("historyThumbnailReady")
                         and int(card.property("historyEventId")) != int(state["historySelectedEvent"])
                         and self.screen_contains(card, QtCore.QPoint(card.width() // 2, 35))
                         and self.scroll.viewport().rect().contains(self.scroll.viewport().mapFromGlobal(
                             card.mapToGlobal(QtCore.QPoint(card.width() // 2, 35))))]
                if not ready:
                    return
                hit = [card for card in ready if self.mouse_route(card)[2]]
                if not hit:
                    return
                self.card = hit[0]
                self.keep.append(self.card)
                if self.card.findChildren(QtWidgets.QWidget):
                    raise RuntimeError("History image card still contains native/child widgets")
                self.click_event = int(self.card.property("historyEventId"))
                self.before_click = int(pyrenderdoc.CurEvent())
                self.before_scroll = self.scrollbar.value()
                self.advance("single")
                self.click(self.card)
            elif self.phase == "single":
                if int(self.history.property("historySelectedEvent") or 0) != self.click_event:
                    raise RuntimeError("Image-area mouse click did not select its history revision")
                if int(pyrenderdoc.CurEvent()) != self.before_click:
                    raise RuntimeError("Single history click unexpectedly changed the global EID")
                if self.scrollbar.value() != self.before_scroll:
                    raise RuntimeError("Single mouse click moved its card before double-click")
                if not self.displayed(self.click_event):
                    return
                self.result["cases"].append({"name": "image-single-preview", "ok": True,
                                              "event": self.click_event, "globalEvent": self.before_click,
                                              "state": self.diagnostics()})
                if self.tab_checks:
                    self.tab_prepare_started = now
                    self.advance("tab_prepare")
                    return
                self.advance("double")
                self.click(self.card, double=True)
            elif self.phase == "tab_prepare":
                if now - self.tab_prepare_started >= 0.05:
                    self.prepare_tab_checks()
            elif self.phase == "tab_current":
                self.observe_current_tab(now)
            elif self.phase == "tab_history":
                self.observe_history_tab()
            elif self.phase == "double":
                if int(pyrenderdoc.CurEvent()) != self.click_event or not self.displayed(self.click_event):
                    return
                self.result["cases"].append({"name": "image-double-jump", "ok": True,
                                              "event": self.click_event, "state": self.diagnostics()})
                maximum = self.scrollbar.maximum()
                self.scroll_positions = sorted(set([0, maximum // 3, maximum * 2 // 3, maximum]))
                # Start at the end opposite the original anchor, then visit other neighborhoods.
                if self.scrollbar.value() > maximum // 2:
                    self.scroll_positions.reverse()
                self.scroll_positions = [value for value in self.scroll_positions
                                         if value != self.scrollbar.value()]
                self.before_far_serial = int(self.history.property("historyRequestSerial") or 0)
                self.advance("scroll")
                self.last_tick = time.perf_counter()
                if self.scroll_positions:
                    self.scrollbar.setValue(self.scroll_positions.pop())
                self.scroll_started = time.perf_counter()
            elif self.phase == "scroll":
                state = self.diagnostics()
                # The scrollbar's zero-delay refresh must run before we inspect its new cards.
                if now - self.scroll_started < 0.05:
                    return
                cards = self.cards()
                if not cards or int(state["historyPendingEvent"] or 0):
                    return
                if any(not card.property("historyThumbnailReady")
                       and not card.property("historyThumbnailUnavailable") for card in cards):
                    return
                self.result["cases"].append({"name": "scroll-neighborhood-bounds", "ok": True,
                                              "scrollValue": self.scrollbar.value(), "state": state})
                if self.scroll_positions:
                    self.scrollbar.setValue(self.scroll_positions.pop())
                    self.scroll_started = now
                    self.advance("scroll")
                    return
                requested = int(state["historyRequestSerial"] or 0)
                self.result["lruCapacityExercised"] = (requested > 24
                                                      and int(state["historyCachedCount"] or 0) == 24)
                if self.loading_gaps and max(self.loading_gaps) > self.max_gap:
                    raise RuntimeError("History-loading GUI heartbeat exceeded configured limit")
                self.finish()
        except Exception as exc:
            self.finish(exc)
        finally:
            self.previous_tick_phase = tick_phase
            self.previous_tick_wall_ms = (time.perf_counter() - tick_started) * 1000.0

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.stop_stack_sampling()
        if self.replay_release is not None:
            self.replay_release.set()
        if self.timer is not None:
            self.timer.stop()
            self.timer.timeout.disconnect(self.tick)
            self.timer.deleteLater()
            self.timer = None
        if self.history_splitter is not None and self.saved_splitter_limits is not None:
            self.history_splitter.setMaximumHeight(self.saved_splitter_limits[1])
            self.history_splitter.setMinimumHeight(self.saved_splitter_limits[0])
            if self.saved_splitter_sizes and len(self.saved_splitter_sizes) == self.history_splitter.count():
                self.history_splitter.setSizes(self.saved_splitter_sizes)
        if self.test_window is not None and self.saved_window_geometry is not None:
            if self.window_layout is not None and self.saved_window_constraint is not None:
                self.window_layout.setSizeConstraint(self.saved_window_constraint)
            if self.saved_window_minimum is not None:
                self.test_window.setMinimumSize(self.saved_window_minimum)
            self.test_window.restoreGeometry(self.saved_window_geometry)
        self.result.update({"ok": error is None, "finalPhase": self.phase,
                            "slowGaps": self.slow_gaps,
                            "clock": {"name": "perf_counter", "implementation": time.get_clock_info("perf_counter").implementation,
                                      "resolutionSeconds": time.get_clock_info("perf_counter").resolution},
                            "heartbeatSamples": len(self.gaps),
                            "maximumHeartbeatMs": max(self.gaps or [0]),
                            "maximumLoadingHeartbeatMs": max(self.loading_gaps or [0]),
                            "stateSamples": self.samples, "mouseRoutes": self.mouse_routes,
                            "tabSwitchChecks": self.tab_checks, "surfaceSamples": self.surface_samples})
        if self.history is not None:
            self.result["nativeLoadingHeartbeat"] = {
                "samples": self.history.property("historyNativeHeartbeatSamples"),
                "maximumMs": self.history.property("historyNativeMaximumLoadingHeartbeatMs")}
            self.result["nativeUISpans"] = self.history.property("historyUISpans")
        if error is not None:
            self.result["error"] = str(error)
            self.result["traceback"] = traceback.format_exc()
            try:
                self.result["failureState"] = self.diagnostics()
            except Exception as diagnostic_error:
                self.result["diagnosticError"] = str(diagnostic_error)
        try:
            self.finish_auto_restore_isolation()
        except Exception as isolation_error:
            self.result["ok"] = False
            self.result["isolationError"] = str(isolation_error)
        encoded = json.dumps(self.result, sort_keys=True, separators=(",", ":"))
        print("RENDERDOC_HISTORY_SMOKE=" + encoded)
        if self.output:
            with open(self.output, "w", encoding="utf-8") as stream:
                stream.write(encoded)
        if os.environ.get("RENDERDOC_HISTORY_SMOKE_QUIT") == "1":
            self.app.closeAllWindows()
            QtCore.QTimer.singleShot(0, lambda: self.app.exit(0))


runner = _HistorySmoke()
runner.publish_progress("script-loaded")
runner.app._history_smoke_runner = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
