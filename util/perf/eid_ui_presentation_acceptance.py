"""Run only in an owned qrenderdoc process with --ui-script.

RENDERDOC_EID_UI_TEST_CONFIG is private JSON or its absolute file path: manifest (the successful small D3D12
checkpoint discovery JSON), output, isolationRoot, worktree; optional timeoutSeconds
(30), loadTimeoutSeconds (120), tabRounds (2). The script loads the manifest's capture
itself and exits this application with 0/1. RENDERDOC_EID_TIMING=1 enables the existing
read-only CapturePresentationWitness slot. No ViewTexture/Display or product API is added.

Coverage: held ScrubToEvent publication, real Qt output/history mouse events, real
tab-bar input, full existing output-window pixels. This is not physical OS mouse,
slider hit-testing, OS scanout, or large-capture performance acceptance. The launcher
must enforce its own process deadline if a native driver call stops the Qt event loop.
Importing this module for its pure acceptance tests never loads Qt or RenderDoc.
"""
import hashlib
import json
import os
from pathlib import Path
import time
import traceback


def load_configuration(value):
    if value.lstrip().startswith("{"):
        return json.loads(value)
    return json.loads(Path(value).read_text(encoding="utf-8-sig"))


def loading_complete(load_call_returned, capture_loaded, capture_loading):
    # LoadCapture pumps a nested Qt event loop. Its replay worker sets IsCaptureLoaded before
    # the caller selects the final event and delivers OnCaptureLoaded to every viewer.
    return bool(load_call_returned and capture_loaded and not capture_loading)


def fixture_inputs(config, manifest):
    if manifest.get("schema") != "renderdoc-d3d12-checkpoint-fixture/v1" or manifest.get("ok") is not True:
        raise ValueError("A successful checkpoint discovery manifest is required")
    events = manifest.get("selectedEvents", [])
    if len(events) != 4 or len(set(events)) != 4 or events != sorted(events) or min(events) <= 0:
        raise ValueError("UI fixture requires four evidenced draw EIDs")
    bindings = {row["eventId"]: row for row in manifest.get("bindings", [])}
    resources = []
    for event in events:
        colors = bindings.get(event, {}).get("colorTargets", [])
        if len(colors) != 1 or colors[0].get("slot") != 0:
            raise ValueError("Each evidenced main draw must bind exactly one RT0")
        resources.append(colors[0]["resource"])
    if len(set(resources)) != 1 or resources[0].endswith("::0"):
        raise ValueError("The four fixture draws must share a real RT0")
    if manifest.get("captureDriver") != "D3D12":
        raise ValueError("This bounded UI fixture requires a discovered D3D12 capture")
    timeout = float(config.get("timeoutSeconds", 30))
    load_timeout = float(config.get("loadTimeoutSeconds", 120))
    rounds = int(config.get("tabRounds", 2))
    if not 1 <= timeout <= 120 or not 1 <= load_timeout <= 300 or not 1 <= rounds <= 5:
        raise ValueError("UI fixture deadlines and tab rounds are outside their bounded ranges")
    output, isolation = Path(config["output"]).resolve(), Path(config["isolationRoot"]).resolve()
    if os.path.commonpath([str(output), str(isolation)]) != str(isolation):
        raise ValueError("UI report must remain inside the owned isolation directory")
    if not output.parent.is_dir() or output.exists():
        raise ValueError("UI output must be a new file in an existing isolated run directory")
    return {"events": events, "resource": resources[0], "capture": str(Path(manifest["capture"]).resolve()),
            "output": str(output), "timeout": timeout, "loadTimeout": load_timeout, "rounds": rounds}


def presentation_ready(state, event, resource, global_event, held=None, strict=False):
    if state["pending"]:
        if state["displayedEvent"] is not None or state["displayedResource"] is not None:
            raise ValueError("A pending surface prematurely certified an EID/resource")
        return False
    if state["hidden"]:
        return False
    identity = state["displayedEvent"] == event and state["displayedResource"] == resource
    if strict and not identity:
        raise ValueError("Completed tab publication belongs to the previous display source")
    ready = (identity and state["currentEvent"] == global_event and state["selectedEvent"] == global_event
             and state["resource"] == resource)
    if ready and held is not None and state["scrubbing"] != held:
        raise ValueError("Presentation completed only after the held scrub state changed")
    return ready


def check_surface(surface, reference):
    if surface["size"] != reference["size"] or surface["sha256"] != reference["sha256"]:
        raise ValueError("Existing output pixels differ from the selected tab's complete reference")


def check_pending_surface(state, surface, reference):
    if not state["pending"] or state["displayedEvent"] is not None or state["displayedResource"] is not None:
        raise ValueError("Invalid pending presentation identity")
    if surface["blankRGB"]:
        return "cleared"
    check_surface(surface, reference)
    return "new-source-before-ui-completion"


def run_ui(context, config):
    import renderdoc as rd
    import qrenderdoc as qrd
    from PySide2 import QtCore, QtGui, QtWidgets

    class Runner:
        def __init__(self):
            self.app = QtWidgets.QApplication.instance()
            self.finished = False
            self.keep = []
            self.kept = set()
            self.timer = None
            self.publish_allowed = False
            self.load_call_returned = False
            self.render = self.widget = self.history = None
            self.phase = "startup"
            self.serial_floor = 0
            self.started = time.monotonic()
            self.report = {"schema": "qrenderdoc-eid-ui-presentation/v1", "ok": False,
                           "configuration": config, "cases": [], "states": [], "surfaces": [],
                           "mouseRoutes": [], "coverage": __doc__}
            self.output = Path(config["output"])

        def hold(self, widget):
            if widget is not None and id(widget) not in self.kept:
                self.kept.add(id(widget))
                self.keep.append(widget)
            return widget

        def publish(self):
            if not self.publish_allowed:
                raise ValueError("Report destination has not passed isolation validation")
            self.report.update(phase=self.phase, elapsedMs=round((time.monotonic() - self.started) * 1000, 3))
            temporary = self.output.with_suffix(self.output.suffix + ".tmp")
            temporary.write_text(json.dumps(self.report, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.output)

        def advance(self, phase):
            self.phase = phase
            self.phase_start = time.monotonic()
            self.deadline = self.phase_start + self.inputs["timeout"]
            self.stable_key, self.stable_since = None, None
            self.publish()

        def start(self):
            try:
                manifest = json.loads(Path(config["manifest"]).read_text(encoding="utf-8"))
                self.inputs = fixture_inputs(config, manifest)
                self.publish_allowed = True
                self.anchor, self.preview_event = self.inputs["events"][-1], self.inputs["events"][1]
                self.expected_resource = self.inputs["resource"]
                self.report["fixture"] = self.inputs
                self.runtime_identity()
                if os.environ.get("RENDERDOC_EID_TIMING") != "1":
                    raise ValueError("RENDERDOC_EID_TIMING=1 is required for the read-only output witness")
                if context.IsCaptureLoaded() or getattr(context, "IsCaptureLoading", lambda: False)():
                    raise ValueError("Use a fresh owned UI process with no capture already loading")
                self.timer = QtCore.QTimer()
                self.timer.setTimerType(QtCore.Qt.PreciseTimer)
                self.timer.setInterval(20)
                self.timer.timeout.connect(self.tick)
                self.advance("loading")
                self.deadline = time.monotonic() + self.inputs["loadTimeout"]
                self.timer.start()
                context.LoadCapture(self.inputs["capture"], rd.ReplayOptions(), self.inputs["capture"], False, True)
                self.load_call_returned = True
                self.report["loadCaptureReturned"] = {
                    "elapsedMs": round((time.monotonic() - self.started) * 1000, 3),
                    "loaded": bool(context.IsCaptureLoaded()), "currentEvent": int(context.CurEvent()),
                    "selectedEvent": int(context.CurSelectedEvent()),
                    "meaning": "LoadCapture returned after initial SetEventID and OnCaptureLoaded callbacks"}
                if not context.IsCaptureLoaded():
                    raise ValueError("LoadCapture returned without a loaded capture")
                self.publish()
            except Exception as error:
                self.finish(error)

        def runtime_identity(self):
            import ctypes
            import site
            import sys
            from ctypes import wintypes
            actual_config = Path(str(qrd.ConfigFilePath("UI.config"))).resolve()
            expected_config = Path(config["isolationRoot"]).resolve() / "config" / "UI.config"
            modules = (wintypes.HMODULE * 1024)()
            required = wintypes.DWORD()
            kernel, psapi = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("psapi", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.EnumProcessModules.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE),
                                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
            kernel.GetModuleFileNameW.restype = wintypes.DWORD
            if not psapi.EnumProcessModules(kernel.GetCurrentProcess(), modules, ctypes.sizeof(modules), ctypes.byref(required)):
                raise ctypes.WinError(ctypes.get_last_error())
            if required.value > ctypes.sizeof(modules):
                raise ValueError("Loaded native module inventory exceeds the explicit 1024-module bound")
            paths = []
            for index in range(required.value // ctypes.sizeof(wintypes.HMODULE)):
                buffer = ctypes.create_unicode_buffer(32768)
                length = kernel.GetModuleFileNameW(modules[index], buffer, len(buffer))
                if not length or length >= len(buffer):
                    raise ValueError("Native module path is missing or truncated")
                paths.append(str(Path(buffer.value).resolve()))
            bridge = sys.modules.get("renderdoc_mcp_bridge")
            self.report["runtime"] = {"executable": self.app.applicationFilePath(), "pid": os.getpid(),
                "nativeModules": paths, "pythonExecutable": sys.executable, "pythonVersion": sys.version,
                "pythonModulePaths": {name: str(getattr(sys.modules.get(name), "__file__", "")) for name in
                    ("renderdoc", "qrenderdoc", "PySide2", "PySide2.QtCore", "PySide2.QtGui", "PySide2.QtWidgets", "shiboken2")},
                "bridgePath": str(getattr(bridge, "__file__", "")), "bridgeImported": bridge is not None,
                "enableUserSite": site.ENABLE_USER_SITE, "uiConfig": str(actual_config),
                "requestedConfigRoot": os.environ.get("RENDERDOC_CONFIG_DIR")}
            if actual_config != expected_config:
                raise ValueError("The application is not using the isolated UI.config path")
            if site.ENABLE_USER_SITE is not False:
                raise ValueError("Python user-site isolation is not active")
            run_bin = Path(config["worktree"]).resolve() / "x64" / "Development"
            if Path(self.app.applicationFilePath()).resolve() != run_bin / "q_render_soco_doc.exe":
                raise ValueError("The UI executable is outside the intended worktree runtime")
            cores = [Path(path) for path in paths if Path(path).name.lower() == "renderdoc.dll"]
            if cores != [run_bin / "renderdoc.dll"]:
                raise ValueError("The actual loaded RenderDoc core is not the isolated worktree DLL")

        def state(self):
            return {"phase": self.phase, "elapsedMs": round((time.monotonic() - self.started) * 1000, 3),
                    "currentEvent": int(context.CurEvent()), "selectedEvent": int(context.CurSelectedEvent()),
                    "scrubbing": bool(context.IsScrubbing()), "resource": str(self.viewer.GetCurrentResource()),
                    "pending": bool(self.render.property("eidPresentationPending")),
                    "displayedEvent": self.render.property("eidDisplayedEvent"),
                    "displayedResource": self.render.property("eidDisplayedResource"),
                    "serial": int(self.render.property("eidPresentationSerial") or 0),
                    "hidden": self.render.isHidden(), "visible": self.render.isVisible(),
                    "historySelected": self.history.property("historySelectedEvent") if self.history else None,
                    "historyPending": self.history.property("historyPendingEvent") if self.history else None,
                    "historyRequestSerial": self.history.property("historyRequestSerial") if self.history else None}

        def surface(self):
            self.publish()  # Preserve which phase entered a possibly blocking native readback.
            if not QtCore.QMetaObject.invokeMethod(self.widget, "CapturePresentationWitness", QtCore.Qt.DirectConnection):
                raise ValueError("Existing CapturePresentationWitness slot is unavailable")
            image = self.widget.property("eidPresentationWitness")
            if not isinstance(image, QtGui.QImage) or image.isNull():
                raise ValueError("Existing output returned no native presentation image")
            image = image.convertToFormat(QtGui.QImage.Format_RGBA8888)
            raw = bytes(image.constBits())
            self.widget.setProperty("eidPresentationWitness", None)
            if len(raw) != image.byteCount() or len(raw) < 16 * 16 * 4:
                raise ValueError("Presentation image readback is incomplete or too small")
            small = image.scaled(128, 128, QtCore.Qt.IgnoreAspectRatio, QtCore.Qt.FastTransformation)
            result = {"phase": self.phase, "size": [image.width(), image.height()], "bytes": len(raw),
                      "sha256": hashlib.sha256(raw).hexdigest(),
                      "scaledSha256": hashlib.sha256(bytes(small.constBits())).hexdigest(),
                      "blankRGB": not any(raw[0::4]) and not any(raw[1::4]) and not any(raw[2::4]),
                      "state": self.state()}
            if len(self.report["surfaces"]) < 1024:
                self.report["surfaces"].append(result)
            return result

        def ready(self, event, global_event=None, held=None, strict=False, reference=None):
            state = self.state()
            if len(self.report["states"]) < 4096:
                self.report["states"].append(state)
            if strict and state["pending"] and not state["hidden"]:
                check_pending_surface(state, self.surface(), reference)
            if not presentation_ready(state, event, self.expected_resource,
                                      event if global_event is None else global_event, held, strict):
                return False
            if state["serial"] < self.serial_floor:
                raise ValueError("An obsolete display completion replaced the newer presentation")
            colors = list(context.CurPipelineState().GetOutputTargets())
            if not colors or str(colors[0].resource) != self.expected_resource:
                raise ValueError("Published pipeline RT0 disagrees with discovered fixture resource")
            if time.monotonic() - self.phase_start < 0.12:
                return False
            sample = self.surface()
            if reference is not None:
                check_surface(sample, reference)
            key = (tuple(sample["size"]), sample["sha256"], state["serial"])
            now = time.monotonic()
            if key != self.stable_key:
                self.stable_key, self.stable_since = key, now
                return False
            if now - self.stable_since < 0.06:
                return False
            self.last_surface = sample
            return True

        def click(self, host, double=False, point=None):
            self.hold(host)
            if not host.isVisible() or not host.isEnabled():
                raise ValueError("Requested Qt mouse target is not visible/enabled")
            point = point or QtCore.QPoint(host.width() // 2, min(35, host.height() // 2))
            global_point = host.mapToGlobal(point)
            receiver = self.hold(self.app.widgetAt(global_point))
            route = "QApplication.widgetAt"
            if receiver is None:
                receiver = self.hold(self.window.childAt(self.window.mapFromGlobal(global_point)))
                route = "owned-window.childAt"
            if receiver is None or not (receiver == host or host.isAncestorOf(receiver)):
                raise ValueError("Qt hit test does not reach the requested visible widget")
            if not host.visibleRegion().contains(point):
                raise ValueError("Requested mouse point is clipped out of the widget")
            self.report["mouseRoutes"].append({"phase": self.phase, "route": route,
                "host": host.objectName(), "class": host.metaObject().className(),
                "receiver": receiver.objectName(), "globalPoint": [global_point.x(), global_point.y()], "double": double})
            kinds = [QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonRelease]
            if double:
                kinds += [QtCore.QEvent.MouseButtonDblClick, QtCore.QEvent.MouseButtonRelease]
            for kind in kinds:
                # Qt keeps the press receiver for release even if opening history moves the link.
                local = receiver.mapFromGlobal(global_point)
                buttons = QtCore.Qt.NoButton if kind == QtCore.QEvent.MouseButtonRelease else QtCore.Qt.LeftButton
                QtWidgets.QApplication.sendEvent(receiver, QtGui.QMouseEvent(kind, QtCore.QPointF(local),
                    QtCore.QPointF(global_point), QtCore.Qt.LeftButton, buttons, QtCore.Qt.NoModifier))

        def find_preview(self):
            wanted = str(context.CurPipelineState().OutputAbbrev()) + "0"
            matches = []
            for preview in self.outputs.findChildren(QtWidgets.QWidget):
                self.hold(preview)
                if preview.metaObject().className() == "ResourcePreview":
                    slot = self.hold(preview.findChild(QtWidgets.QLabel, "slotLabel"))
                    link = self.hold(preview.findChild(QtWidgets.QLabel, "descriptionLabel"))
                    if slot is not None and slot.text() == wanted and link is not None:
                        matches.append((slot, link))
            if len(matches) != 1:
                raise ValueError("Cannot find unique actual RT0 ResourcePreview labels")
            self.slot, self.link = matches[0]
            self.output_scroll = self.hold(self.outputs.findChild(QtWidgets.QScrollArea, "scrollArea"))
            if self.output_scroll is None:
                raise ValueError("Output thumbnail scroll area is unavailable")
            self.output_scroll.ensureWidgetVisible(self.slot, 20, 20)

        def find_tabs(self):
            stack = self.hold(self.current_tab.parentWidget())
            if not isinstance(stack, QtWidgets.QStackedWidget):
                raise ValueError("Current texture tab is not inside the actual tab stack")
            area = self.hold(stack.parentWidget())
            if not area.inherits("QTabWidget"):
                raise ValueError("Texture stack does not belong to a QTabWidget")
            bars = [self.hold(child) for child in area.findChildren(QtWidgets.QWidget)
                    if child.inherits("QTabBar") and child.parentWidget() == area]
            if len(bars) != 1:
                raise ValueError("The actual texture QTabWidget has no unique tab bar")
            self.tabs, self.tab_bar = stack, bars[0]
            self.history_tab = self.hold(stack.currentWidget())
            if self.history_tab == self.current_tab:
                raise ValueError("History did not open a separate locked texture tab")

        def tab(self, widget):
            index = self.tabs.indexOf(widget)
            if index < 0:
                raise ValueError("Requested texture tab disappeared")
            self.serial_floor = self.state()["serial"] + 1
            if hasattr(self.tab_bar, "tabRect"):
                self.click(self.tab_bar, point=self.tab_bar.tabRect(index).center())
            else:
                # This bundled PySide may expose the custom C++ QTabBar as QWidget. Its real
                # keyboard handler still routes QTabWidget.currentChanged; no private callback.
                self.report["tabKeyboardFallback"] = True
                for unused in range(self.tabs.count()):
                    before = self.tabs.currentIndex()
                    if before == index:
                        break
                    key = QtCore.Qt.Key_Right if index > before else QtCore.Qt.Key_Left
                    for kind in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease):
                        QtWidgets.QApplication.sendEvent(self.tab_bar, QtGui.QKeyEvent(kind, key, QtCore.Qt.NoModifier))
                    if self.tabs.currentIndex() == before:
                        raise ValueError("Native Qt tab-bar input did not change the selected tab")
            if self.tabs.currentWidget() != widget:
                raise ValueError("Tab-bar input selected a different tab")
            state = self.state()
            if not state["pending"] or state["displayedEvent"] is not None or state["displayedResource"] is not None:
                raise ValueError("Tab switch left the previous display identity certified")
            self.report["states"].append(state)

        def card(self):
            cards = [self.hold(card) for card in self.history.findChildren(QtWidgets.QWidget, "historyRevision")
                     if int(card.property("historyEventId") or 0) == self.preview_event]
            if len(cards) != 1 or not cards[0].property("historyThumbnailReady"):
                return None
            scroll = self.hold(self.history.findChild(QtWidgets.QScrollArea, "renderTargetHistoryScroll"))
            if scroll is None:
                raise ValueError("Actual history scroll area is missing")
            scroll.ensureWidgetVisible(cards[0], 20, 20)
            return cards[0]

        def record(self, name):
            self.report["cases"].append({"name": name, "ok": True, "state": self.state(),
                                          "phaseMs": round((time.monotonic() - self.phase_start) * 1000, 3)})

        def tick(self):
            if self.finished:
                return
            try:
                if time.monotonic() > self.deadline:
                    raise ValueError("UI acceptance timed out in phase " + self.phase)
                if self.phase == "loading":
                    if not loading_complete(self.load_call_returned, context.IsCaptureLoaded(),
                                            getattr(context, "IsCaptureLoading", lambda: False)()):
                        return
                    if Path(str(context.GetCaptureFilename())).resolve() != Path(self.inputs["capture"]):
                        raise ValueError("A different capture was loaded")
                    context.ShowTextureViewer()
                    self.viewer = self.hold(context.GetTextureViewer())
                    self.widget = self.hold(self.viewer.Widget())
                    self.render = self.hold(self.widget.findChild(QtWidgets.QWidget, "render"))
                    self.outputs = self.hold(self.widget.findChild(QtWidgets.QWidget, "outputThumbs"))
                    self.current_tab = self.hold(self.widget.findChild(QtWidgets.QWidget, "renderContainer"))
                    if any(value is None for value in (self.render, self.outputs, self.current_tab)):
                        raise ValueError("Texture viewer widgets are unavailable")
                    self.window = self.hold(self.widget.window())
                    screen = self.hold(self.app.primaryScreen())
                    if screen is None:
                        raise ValueError("No Qt screen exists for this UI test")
                    layout = self.hold(self.window.layout())
                    if layout is not None:
                        layout.setSizeConstraint(QtWidgets.QLayout.SetNoConstraint)
                    self.window.setMinimumSize(0, 0)
                    self.window.showNormal()
                    self.window.setGeometry(screen.availableGeometry().adjusted(24, 24, -24, -24))
                    self.advance("default-rt0-held")
                    context.ScrubToEvent(self.anchor)
                elif self.phase == "default-rt0-held":
                    if self.ready(self.anchor, held=True):
                        self.current_reference = self.last_surface
                        self.record("default RT0 published while scrub remains held")
                        context.EndScrub()
                        self.find_preview()
                        self.advance("slot-click")
                elif self.phase == "slot-click":
                    if time.monotonic() - self.phase_start >= 0.15 and not context.IsScrubbing():
                        self.click(self.slot)
                        self.advance("slot-selected")
                elif self.phase == "slot-selected":
                    if self.ready(self.anchor, held=False, reference=self.current_reference):
                        self.record("real ResourcePreview RT0 click")
                        self.output_scroll.ensureWidgetVisible(self.link, 20, 20)
                        self.advance("history-link")
                elif self.phase == "history-link":
                    if time.monotonic() - self.phase_start >= 0.15:
                        if not self.link.font().underline():
                            raise ValueError("Actual output description history link is not enabled")
                        self.click(self.link)
                        self.advance("history-open")
                elif self.phase == "history-open":
                    if self.history is None:
                        self.history = self.hold(self.widget.findChild(QtWidgets.QWidget, "renderTargetHistoryView"))
                    if self.history is not None and self.history.isVisible() and self.card() is not None:
                        self.find_tabs()
                        self.advance("history-card-layout")
                elif self.phase == "history-card-layout":
                    if time.monotonic() - self.phase_start >= 0.15:
                        card = self.card()
                        if card is not None:
                            self.click(card)
                            self.advance("history-single")
                elif self.phase == "history-single":
                    if int(context.CurEvent()) != self.anchor:
                        raise ValueError("Single history mouse click changed the global EID")
                    if self.ready(self.preview_event, self.anchor):
                        if int(self.history.property("historySelectedEvent") or 0) != self.preview_event:
                            raise ValueError("History selected revision disagrees with the image")
                        self.history_reference = self.last_surface
                        if self.history_reference["scaledSha256"] == self.current_reference["scaledSha256"]:
                            raise ValueError("Current/history fixture images are indistinguishable; cannot certify routing")
                        self.record("history image single click previews without global EID change")
                        self.round = 0
                        self.advance("tab-current")
                        self.tab(self.current_tab)
                elif self.phase == "tab-current":
                    if self.ready(self.anchor, self.anchor, strict=True, reference=self.current_reference):
                        self.record("current tab pixels match current EID")
                        self.advance("tab-history")
                        self.tab(self.history_tab)
                elif self.phase == "tab-history":
                    if self.ready(self.preview_event, self.anchor, strict=True, reference=self.history_reference):
                        self.record("history tab pixels retain selected historical EID")
                        self.round += 1
                        if self.round < self.inputs["rounds"]:
                            self.advance("tab-current")
                            self.tab(self.current_tab)
                            self.tab(self.history_tab)
                            self.tab(self.current_tab)
                        else:
                            self.advance("history-double")
                            self.click(self.card(), double=True)
                elif self.phase == "history-double":
                    if self.ready(self.preview_event, self.preview_event):
                        self.record("history image double click changes global EID")
                        self.advance("return-current")
                        self.tab(self.current_tab)
                elif self.phase == "return-current":
                    if self.ready(self.preview_event):
                        self.advance("held-return-anchor")
                        context.ScrubToEvent(self.anchor)
                elif self.phase == "held-return-anchor":
                    if self.ready(self.anchor, held=True, reference=self.current_reference):
                        self.record("held EID return refreshes current output after history navigation")
                        context.EndScrub()
                        self.advance("released")
                elif self.phase == "released":
                    if self.ready(self.anchor, held=False, reference=self.current_reference):
                        self.record("release preserves completed current output")
                        self.finish()
            except Exception as error:
                self.finish(error)

        def finish(self, error=None):
            if self.finished:
                return
            self.finished = True
            if self.timer is not None:
                self.timer.stop()
                self.timer.timeout.disconnect(self.tick)
                self.timer.deleteLater()
                self.timer = None
            self.report["ok"] = error is None
            if error is not None:
                self.report.update(error=str(error), traceback=traceback.format_exc())
            try:
                import sys
                bridge = sys.modules.get("renderdoc_mcp_bridge")
                if "runtime" in self.report:
                    self.report["runtime"]["bridgePathAtEnd"] = str(getattr(bridge, "__file__", ""))
                    self.report["runtime"]["bridgeImportedAtEnd"] = bridge is not None
                if self.render is not None:
                    self.report["finalState"] = self.state()
                if self.publish_allowed:
                    self.publish()
            except Exception as publish_error:
                self.report["ok"] = False
                print("UI acceptance report publication failed: " + str(publish_error))
            print("RENDERDOC_EID_UI_ACCEPTANCE=" + json.dumps(self.report, separators=(",", ":")))
            # qrenderdoc drives processEvents while CaptureContext::isRunning(), rather than
            # QApplication::exec(). Close this context's own window to end that loop. No other
            # application/window/process is discovered or closed by this acceptance script.
            try:
                if not context.GetMainWindow().Widget().close():
                    raise ValueError("The owned main window rejected the test close")
            except Exception as close_error:
                self.report["ok"] = False
                self.report["closeError"] = str(close_error)
                if self.publish_allowed:
                    self.publish()
            QtCore.QCoreApplication.exit(0 if self.report["ok"] else 1)

    runner = Runner()
    runner.app._eid_ui_acceptance_runner = runner
    context.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)


if "pyrenderdoc" in globals():
    try:
        run_ui(pyrenderdoc, load_configuration(os.environ["RENDERDOC_EID_UI_TEST_CONFIG"]))
    except Exception:
        print("RENDERDOC_EID_UI_ACCEPTANCE_STARTUP_ERROR=" + traceback.format_exc())
        from PySide2 import QtCore
        def close_failed_start():
            pyrenderdoc.GetMainWindow().Widget().close()
            QtCore.QCoreApplication.exit(1)
        pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(close_failed_start)
