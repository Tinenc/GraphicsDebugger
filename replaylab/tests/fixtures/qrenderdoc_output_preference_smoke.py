"""Run in a fresh qrenderdoc process with --python; no capture edits are made.

RENDERDOC_OUTPUT_PREFERENCE_CAPTURE: explicit capture path, or use an opened capture.
RENDERDOC_OUTPUT_PREFERENCE_ANCHOR: group A draw EID, default 29385 (the reported 1.rdc case).
RENDERDOC_OUTPUT_PREFERENCE_SLOT: manually selected color output slot, default 5.
RENDERDOC_OUTPUT_PREFERENCE_OTHER: optional draw EID from another group with RT0 and the
selected slot both bound. Otherwise infer a nearby candidate from action output metadata.
RENDERDOC_OUTPUT_PREFERENCE_FALLBACK: optional draw EID with RT0 bound and selected slot
unbound; otherwise infer a candidate. Actual pipeline bindings are checked after publication.
RENDERDOC_OUTPUT_PREFERENCE_OUTPUT: JSON report path.
RENDERDOC_OUTPUT_PREFERENCE_TIMEOUT: per-phase seconds, default 45.
RENDERDOC_OUTPUT_PREFERENCE_QUIT=1: close this test instance after the report.

Sequence: A defaults to RT0; real thumbnail-image click selects RT5; B defaults to
RT0 even though RT5 exists; return A restores RT5; sparse group C falls back to RT0;
return A still restores RT5. While held, scrub A -> B -> A and require RT0 -> RT5
with IsScrubbing still true on both completed presentations, then release.
GetCurrentResource and the post-Display EID/resource properties must both match. No ViewFollowedResource/ViewTexture call primes selection.
Mouse input retains all PySide wrappers, scrolls to the actual output image, waits
for stable layout, hit-tests its press, and delivers release to the grabbed receiver.
"""
from __future__ import print_function

import json
import os
import time
import traceback

import renderdoc as rd
from PySide2 import QtCore, QtGui, QtWidgets


def _rid(value):
    return str(value)


def _binding(value):
    return {"resource": _rid(value.resource), "mip": int(value.firstMip),
            "slice": int(value.firstSlice)}


class _OutputPreferenceSmoke(object):
    def __init__(self):
        self.app = QtWidgets.QApplication.instance()
        self.result = {"schema": "qrenderdoc-output-preference-smoke/v1", "ok": False, "cases": []}
        self.output = os.environ.get("RENDERDOC_OUTPUT_PREFERENCE_OUTPUT", "")
        self.anchor = int(os.environ.get("RENDERDOC_OUTPUT_PREFERENCE_ANCHOR", "29385"))
        self.slot = int(os.environ.get("RENDERDOC_OUTPUT_PREFERENCE_SLOT", "5"))
        self.timeout = float(os.environ.get("RENDERDOC_OUTPUT_PREFERENCE_TIMEOUT", "45"))
        self.phase = "loading"
        self.finished = False
        self.timer = None
        self.deadline = time.monotonic() + 120.0
        self.keep, self.kept_ids, self.mouse_routes = [], set(), []
        self.mouse_grab = None
        self.state_samples = []
        self.expected_resource = None

    def start(self):
        try:
            # QApplication survives embedded Python shutdown; keep Python timers Python-owned.
            self.timer = QtCore.QTimer()
            self.timer.setTimerType(QtCore.Qt.PreciseTimer)
            self.timer.setInterval(10)
            self.timer.timeout.connect(self.tick)
            capture = os.environ.get("RENDERDOC_OUTPUT_PREFERENCE_CAPTURE", "")
            if not pyrenderdoc.IsCaptureLoaded() and capture:
                pyrenderdoc.LoadCapture(capture, rd.ReplayOptions(), capture, False, True)
            self.timer.start()
        except Exception as exc:
            self.finish(exc)

    def advance(self, phase):
        self.phase = phase
        self.deadline = time.monotonic() + self.timeout

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

    def mouse_route(self, host, point=None):
        self.hold(host)
        point = point or QtCore.QPoint(host.width() // 2, min(35, host.height() // 2))
        global_point = host.mapToGlobal(point)
        # Hold every wrapper immediately, before traversing. PySide wrappers returned by
        # parentWidget() can own another wrapper's lifetime even while C++ retains the widget.
        receiver = self.hold(self.app.widgetAt(global_point))
        route = {"phase": self.phase, "host": self.describe_widget(host),
                 "receiver": self.describe_widget(receiver),
                 "globalPoint": [global_point.x(), global_point.y()], "ancestors": []}
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

    def click(self, host, double=False):
        self.mouse(host, QtCore.QEvent.MouseButtonPress)
        self.mouse(host, QtCore.QEvent.MouseButtonRelease)
        if double:
            self.mouse(host, QtCore.QEvent.MouseButtonDblClick)
            self.mouse(host, QtCore.QEvent.MouseButtonRelease)

    def prepare(self):
        if self.anchor <= 0 or self.slot < 1:
            raise ValueError("Anchor must be positive and manual color slot must be above RT0")
        self.actions = {}

        def visit(actions):
            for action in actions:
                self.actions[int(action.eventId)] = action
                visit(action.children)

        visit(pyrenderdoc.CurRootActions())
        if self.anchor not in self.actions:
            raise RuntimeError("Anchor EID is not an action in this capture")
        self.texture_ids = set(_rid(texture.resourceId) for texture in pyrenderdoc.GetTextures())
        self.null_id = _rid(rd.ResourceId.Null())
        anchor_action = self.actions[self.anchor]
        anchor_outputs = tuple(_rid(resource) for resource in anchor_action.outputs)
        if (len(anchor_outputs) <= self.slot or anchor_outputs[0] not in self.texture_ids
                or anchor_outputs[self.slot] not in self.texture_ids
                or anchor_outputs[0] == anchor_outputs[self.slot]):
            raise RuntimeError("Anchor needs distinct actual texture bindings at RT0 and the manual slot")
        draws = [action for action in self.actions.values()
                 if not action.children and (action.flags & rd.ActionFlags.Drawcall)
                 and not (action.flags & rd.ActionFlags.MultiAction)]
        draws.sort(key=lambda action: abs(int(action.eventId) - self.anchor))

        def choose(env_name, sparse):
            explicit = os.environ.get(env_name, "").strip()
            if explicit:
                event = int(explicit)
                if event not in self.actions:
                    raise RuntimeError(env_name + " must name an actual action")
                return event
            for action in draws:
                outputs = tuple(_rid(resource) for resource in action.outputs)
                if (not outputs or outputs[0] not in self.texture_ids
                        or outputs == anchor_outputs or outputs[0] == anchor_outputs[self.slot]):
                    continue
                manual_valid = len(outputs) > self.slot and outputs[self.slot] in self.texture_ids
                if sparse:
                    if not manual_valid:
                        return int(action.eventId)
                elif manual_valid and outputs[0] != outputs[self.slot]:
                    return int(action.eventId)
            raise RuntimeError("No suitable output group; explicitly set " + env_name)

        self.other = choose("RENDERDOC_OUTPUT_PREFERENCE_OTHER", False)
        self.fallback = choose("RENDERDOC_OUTPUT_PREFERENCE_FALLBACK", True)
        self.result.update({"capture": str(pyrenderdoc.GetCaptureFilename()), "anchor": self.anchor,
                            "manualSlot": self.slot, "otherGroupEvent": self.other,
                            "fallbackGroupEvent": self.fallback,
                            "completionEvidence": "GetCurrentResource + post-Display EID/resource after actual pipeline group publication; no selection priming API"})
        pyrenderdoc.ShowTextureViewer()
        self.viewer = self.hold(pyrenderdoc.GetTextureViewer())
        self.widget = self.hold(self.viewer.Widget())
        self.render = self.hold(self.widget.findChild(QtWidgets.QWidget, "render"))
        self.outputs = self.hold(self.widget.findChild(QtWidgets.QWidget, "outputThumbs"))
        self.output_scroll = self.hold(self.outputs.findChild(QtWidgets.QScrollArea, "scrollArea"))
        if self.render is None or self.output_scroll is None:
            raise RuntimeError("Texture Viewer output widgets are unavailable")
        self.hold(self.output_scroll.widget())
        self.hold(self.output_scroll.viewport())
        self.hold(self.output_scroll.horizontalScrollBar())
        self.hold(self.output_scroll.verticalScrollBar())
        self.groups = {}
        self.navigate(self.anchor, "anchor-default", 0)

    def group(self):
        pipe = pyrenderdoc.CurPipelineState()
        colors = [_binding(value) for value in pipe.GetOutputTargets()]
        while colors and colors[-1]["resource"] == self.null_id:
            colors.pop()
        depth = _binding(pipe.GetDepthTarget())
        depth_resolve = _binding(pipe.GetDepthResolveTarget())
        return {"colors": colors, "depth": depth, "depthResolve": depth_resolve}

    def navigate(self, event, phase, slot):
        self.advance(phase)
        self.expected_event = event
        pyrenderdoc.SetEventID([], event, event, True)
        self.active_group = self.group()
        colors = self.active_group["colors"]
        if not colors or colors[0]["resource"] not in self.texture_ids:
            raise RuntimeError("Actual pipeline needs a displayable RT0 for " + phase)
        if slot >= len(colors) or colors[slot]["resource"] not in self.texture_ids:
            raise RuntimeError("Expected color slot is not actually bound for " + phase)
        self.expected_resource = colors[slot]["resource"]
        if phase == "anchor-default":
            if len(colors) <= self.slot or colors[self.slot]["resource"] not in self.texture_ids:
                raise RuntimeError("Actual anchor pipeline does not bind the manual slot")
            if colors[0]["resource"] == colors[self.slot]["resource"]:
                raise RuntimeError("Distinct anchor resources are needed to prove slot restoration")
            self.groups["anchor"] = self.active_group
        elif phase == "other-default":
            if self.active_group == self.groups["anchor"]:
                raise RuntimeError("Other event has the same actual output group as the anchor")
            if len(colors) <= self.slot or colors[self.slot]["resource"] not in self.texture_ids:
                raise RuntimeError("Other group must keep the old manual slot bound to expose slot carry-over")
            if colors[0]["resource"] == colors[self.slot]["resource"]:
                raise RuntimeError("Other group RT0/manual resources must be distinct")
            self.groups["other"] = self.active_group
        elif phase == "fallback-default":
            if self.active_group in self.groups.values():
                raise RuntimeError("Fallback event must be a previously unvisited output group")
            if len(colors) > self.slot and colors[self.slot]["resource"] in self.texture_ids:
                raise RuntimeError("Fallback group unexpectedly still binds the manual slot")
            self.groups["fallback"] = self.active_group
        else:
            if self.active_group != self.groups["anchor"]:
                raise RuntimeError("Returned anchor output group changed unexpectedly")

    def begin_scrub(self, event, phase, group_name, slot):
        # ScrubToEvent publishes asynchronously. Expected resources come from a previously
        # verified group, never from the still-old pipeline immediately after requesting.
        self.advance(phase)
        self.expected_event = event
        self.expected_group = self.groups[group_name]
        self.expected_resource = self.expected_group["colors"][slot]["resource"]
        self.active_group = None
        pyrenderdoc.ScrubToEvent(event)

    def state(self):
        return {"phase": self.phase, "currentEvent": int(pyrenderdoc.CurEvent()),
                "isScrubbing": bool(pyrenderdoc.IsScrubbing()),
                "currentResource": _rid(self.viewer.GetCurrentResource()),
                "displayedEvent": self.render.property("eidDisplayedEvent"),
                "displayedResource": self.render.property("eidDisplayedResource"),
                "expectedEvent": self.expected_event, "expectedResource": self.expected_resource}

    def presented(self):
        state = self.state()
        if len(self.state_samples) < 4096:
            self.state_samples.append(state)
        return (state["currentEvent"] == self.expected_event
                and state["currentResource"] == self.expected_resource
                and int(state["displayedEvent"] or 0) == self.expected_event
                and str(state["displayedResource"]) == self.expected_resource)

    def prepare_click(self):
        children = [self.hold(child) for child in self.outputs.findChildren(QtWidgets.QWidget)]
        wanted = str(pyrenderdoc.CurPipelineState().OutputAbbrev()) + str(self.slot)
        matches = []
        for preview in children:
            if preview.metaObject().className() != "ResourcePreview":
                continue
            slot_label = self.hold(preview.findChild(QtWidgets.QLabel, "slotLabel"))
            if slot_label is not None and slot_label.text() == wanted:
                matches.append(preview)
        if len(matches) != 1:
            raise RuntimeError("Cannot find the real color output preview " + wanted)
        self.preview = matches[0]
        self.preview_layout = self.hold(self.preview.layout())
        self.image_item = self.hold(self.preview_layout.itemAtPosition(0, 0))
        self.image = self.hold(self.image_item.widget())
        if self.image is None:
            raise RuntimeError("Output preview image is unavailable")
        self.output_scroll.ensureWidgetVisible(self.image, 20, 20)
        self.layout_signature = None
        self.layout_stable_ticks = 0
        self.layout_started = time.monotonic()
        self.advance("manual-layout")

    def record_case(self):
        self.result["cases"].append({"name": self.phase, "ok": True, "state": self.state(),
                                     "group": self.active_group})

    def tick(self):
        if self.finished:
            return
        try:
            now = time.monotonic()
            if now > self.deadline:
                raise RuntimeError("Timed out in output-preference phase " + self.phase)
            if self.phase == "loading":
                if pyrenderdoc.IsCaptureLoaded():
                    self.prepare()
                return
            if self.phase == "manual-layout":
                self.output_scroll.ensureWidgetVisible(self.image, 20, 20)
                receiver, unused_point, belongs = self.mouse_route(self.image)
                signature = (self.describe_widget(self.image), self.describe_widget(receiver))
                if signature == self.layout_signature and belongs:
                    self.layout_stable_ticks += 1
                else:
                    self.layout_signature = signature
                    self.layout_stable_ticks = 0
                if not belongs or self.layout_stable_ticks < 3 or now - self.layout_started < 0.15:
                    return
                self.expected_resource = self.groups["anchor"]["colors"][self.slot]["resource"]
                self.advance("manual-selection")
                self.click(self.image)
                return
            if not self.presented():
                return
            if self.phase in ("held-other-default", "held-anchor-restored"):
                if not pyrenderdoc.IsScrubbing():
                    raise RuntimeError("The held output preference completed only after scrubbing ended")
                # Only inspect actual bindings once the requested event and Display result
                # have both published. The old group's pipeline is valid during the wait.
                self.active_group = self.group()
                if self.active_group != self.expected_group:
                    raise RuntimeError("Completed held presentation has the wrong output group")
            elif self.phase == "held-release":
                if pyrenderdoc.IsScrubbing():
                    return
                if self.group() != self.groups["anchor"]:
                    raise RuntimeError("Ending scrub changed the restored anchor output group")
                self.result["heldRelease"] = self.state()
                self.finish()
                return
            self.record_case()
            if self.phase == "anchor-default":
                self.prepare_click()
            elif self.phase == "manual-selection":
                self.navigate(self.other, "other-default", 0)
            elif self.phase == "other-default":
                self.navigate(self.anchor, "anchor-restored", self.slot)
            elif self.phase == "anchor-restored":
                self.navigate(self.fallback, "fallback-default", 0)
            elif self.phase == "fallback-default":
                self.navigate(self.anchor, "anchor-after-fallback", self.slot)
            elif self.phase == "anchor-after-fallback":
                self.begin_scrub(self.other, "held-other-default", "other", 0)
            elif self.phase == "held-other-default":
                self.begin_scrub(self.anchor, "held-anchor-restored", "anchor", self.slot)
            elif self.phase == "held-anchor-restored":
                self.advance("held-release")
                pyrenderdoc.EndScrub()
        except Exception as exc:
            self.finish(exc)

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        if self.timer is not None:
            self.timer.stop()
            self.timer.timeout.disconnect(self.tick)
            self.timer.deleteLater()
            self.timer = None
        self.result.update({"ok": error is None, "finalPhase": self.phase,
                            "stateSamples": self.state_samples, "mouseRoutes": self.mouse_routes})
        if error is not None:
            self.result["error"] = str(error)
            self.result["traceback"] = traceback.format_exc()
            try:
                self.result["failureState"] = self.state()
                self.result["failureGroup"] = self.group()
            except Exception as diagnostic_error:
                self.result["diagnosticError"] = str(diagnostic_error)
        encoded = json.dumps(self.result, sort_keys=True, separators=(",", ":"))
        print("RENDERDOC_OUTPUT_PREFERENCE_SMOKE=" + encoded)
        if self.output:
            with open(self.output, "w", encoding="utf-8") as stream:
                stream.write(encoded)
        if os.environ.get("RENDERDOC_OUTPUT_PREFERENCE_QUIT") == "1":
            self.app.closeAllWindows()
            QtCore.QTimer.singleShot(0, lambda: self.app.exit(0))


runner = _OutputPreferenceSmoke()
runner.app._output_preference_smoke_runner = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
