"""Exercise real qrenderdoc navigation while its replay queue is deliberately occupied.

Run with qrenderdoc's --python option and an opened capture. Alternatively set
RENDERDOC_EID_SCRUB_SMOKE_CAPTURE to a capture path for this script to load.
RENDERDOC_EID_SCRUB_SMOKE_OUTPUT selects the JSON report path; setting
RENDERDOC_EID_SCRUB_SMOKE_QUIT=1 closes the test instance after the report.

RENDERDOC_EID_TIMING=1 is required. New preview/scrub records and the Texture
Viewer's post-Display properties prove completion; an early FIFO sentinel alone
cannot prove that a deferred GUI publication finished. The held-arrow case uses
real Qt key events and five consecutive leaf rows: A -> D (three Down presses),
then D -> E without releasing the key. Auto-repeat and unrelated releases must
not end the gesture; the final Down release must end it without another replay.
"""

from __future__ import print_function

import json
import os
import re
import threading
import time
import traceback

import renderdoc as rd
from PySide2 import QtCore, QtGui, QtWidgets


_COMPLETION = re.compile(r"^(preview|scrub) EID (\d+): .*?, generation (\d+)(?:,.*)?$")


class _ScrubSmokeRunner(object):
    def __init__(self):
        self.application = QtWidgets.QApplication.instance()
        self.output_path = os.environ.get("RENDERDOC_EID_SCRUB_SMOKE_OUTPUT", "")
        self.result = {"schema": "qrenderdoc-eid-scrub-smoke/v2", "ok": False, "cases": []}
        self.original = None
        self.finished = False
        self.release_gate = threading.Event()
        self.deadline = time.monotonic() + 120.0
        self.timer = None
        self.phase = "loading"
        self.log_lines = []

    def start(self):
        try:
            if os.environ.get("RENDERDOC_EID_TIMING") != "1":
                raise RuntimeError("Start this qrenderdoc process with RENDERDOC_EID_TIMING=1")
            self.log_path = os.path.join(self.application.applicationDirPath(), "eid_timing.log")
            # Startup scripts can run on the Python worker. Construct Qt objects only after
            # InvokeOntoUIThread has dispatched this method to the application thread.
            # QApplication survives embedded Python shutdown; keep Python timers Python-owned.
            self.timer = QtCore.QTimer()
            self.timer.setInterval(20)
            self.timer.timeout.connect(self.tick)
            capture_path = os.environ.get("RENDERDOC_EID_SCRUB_SMOKE_CAPTURE", "")
            if not pyrenderdoc.IsCaptureLoaded() and capture_path:
                pyrenderdoc.LoadCapture(capture_path, rd.ReplayOptions(), capture_path, False, True)
            self.timer.start()
        except Exception as exc:
            self.finish(exc)

    def event_pair(self, index):
        return (int(index.data(QtCore.Qt.UserRole)), int(index.data(QtCore.Qt.UserRole + 1)))

    def find_events(self):
        # Keep the entire PySide wrapper chain alive. A temporary Widget() wrapper can release
        # its child wrappers when the expression ends, leaving findChild's result invalid even
        # though the C++ Event Browser still owns and displays that tree.
        self.event_browser = pyrenderdoc.GetEventBrowser()
        self.event_browser_widget = self.event_browser.Widget()
        self.tree = self.event_browser_widget.findChild(QtWidgets.QTreeView, "events")
        if self.tree is None:
            raise RuntimeError("Event Browser tree is unavailable")
        self.model = self.tree.model()
        self.selection_model = self.tree.selectionModel()
        model = self.model
        candidates = []
        arrow_rows = []

        def visit(parent):
            consecutive = []
            for row in range(model.rowCount(parent)):
                index = model.index(row, 0, parent)
                if model.rowCount(index):
                    consecutive = []
                    visit(index)
                else:
                    selected, effective = self.event_pair(index)
                    if selected > 0 and effective > 0:
                        if all(self.event_pair(candidate)[1] != effective for candidate in candidates):
                            candidates.append(index)
                        if all(self.event_pair(candidate)[1] != effective for candidate in consecutive):
                            consecutive.append(index)
                        else:
                            consecutive = [index]
                        if len(consecutive) == 5 and not arrow_rows:
                            arrow_rows.extend(consecutive)
                    else:
                        consecutive = []
                if arrow_rows:
                    return

        visit(QtCore.QModelIndex())
        if len(candidates) < 2:
            raise RuntimeError("The capture needs two visible leaf events")
        self.a_index, self.b_index = candidates[:2]
        self.a = self.event_pair(self.a_index)
        self.b = self.event_pair(self.b_index)
        if self.a == self.b:
            raise RuntimeError("The two test events must differ")
        self.result["events"] = {"a": self.a, "b": self.b}
        if arrow_rows:
            self.arrow_indices = arrow_rows
            self.arrow_events = [self.event_pair(index) for index in arrow_rows]
            self.result["events"]["arrowAtoE"] = self.arrow_events
        else:
            self.arrow_indices = None
            self.result["heldArrowUnavailable"] = "No five consecutive distinct leaf events in the visible model"

    def reset_log(self):
        self.log_offset = os.path.getsize(self.log_path) if os.path.exists(self.log_path) else 0
        self.log_partial = b""
        self.log_lines = []

    def poll_log(self):
        if not os.path.exists(self.log_path):
            return
        try:
            with open(self.log_path, "rb") as stream:
                stream.seek(self.log_offset)
                data = stream.read()
                self.log_offset = stream.tell()
        except PermissionError:
            return
        lines = (self.log_partial + data).split(b"\n")
        self.log_partial = lines.pop()
        self.log_lines.extend(line.decode("utf-8", "replace").rstrip("\r") for line in lines)

    def state(self):
        return {"selected": int(pyrenderdoc.CurSelectedEvent()), "current": int(pyrenderdoc.CurEvent()),
                "treeSelection": list(self.event_pair(self.tree.currentIndex())),
                "isScrubbing": bool(pyrenderdoc.IsScrubbing()),
                "displayedEvent": self.render.property("eidDisplayedEvent"),
                "displayedResource": self.render.property("eidDisplayedResource"),
                "textureResource": str(self.viewer.GetCurrentResource()),
                "pipeline": self.pipeline_state(pyrenderdoc.CurPipelineState())}

    @staticmethod
    def pipeline_state(pipe):
        return {"outputs": [str(binding.resource) for binding in pipe.GetOutputTargets()],
                "shaders": [str(pipe.GetShader(stage))
                            for stage in (rd.ShaderStage.Vertex, rd.ShaderStage.Pixel, rd.ShaderStage.Compute)]}

    def send_key(self, event_type, key=QtCore.Qt.Key_Down, auto_repeat=False):
        event = QtGui.QKeyEvent(event_type, key, QtCore.Qt.NoModifier, "", auto_repeat, 1)
        self.application.sendEvent(self.tree, event)

    def begin_case(self):
        name = self.case_names[len(self.result["cases"])]
        self.reverse_arrow = name == "held_arrow_release_pending_up"
        first_arrow = 4 if self.reverse_arrow else 0
        self.base = self.arrow_events[first_arrow] if name.startswith("held_arrow_") else self.a
        # This initial synchronous selection runs before the queue gate exists.
        pyrenderdoc.SetEventID([], self.base[0], self.base[1], False)
        if name.startswith("held_arrow_"):
            parent = self.arrow_indices[0].parent()
            while parent.isValid():
                self.tree.expand(parent)
                parent = parent.parent()
            flags = QtCore.QItemSelectionModel.ClearAndSelect | QtCore.QItemSelectionModel.Rows
            self.selection_model.setCurrentIndex(self.arrow_indices[first_arrow], flags)
        self.reset_log()
        self.release_gate = threading.Event()
        self.gate_entered = threading.Event()
        self.gate_exited = threading.Event()
        self.drained = threading.Event()
        self.queue_result = {}
        self.case_result = {"name": name, "checkpoints": []}
        self.released_selection_changes = []
        self.deadline = time.monotonic() + 30.0

        def occupy_replay(controller):
            self.gate_entered.set()
            if not self.release_gate.wait(10.0):
                self.queue_result["gateTimedOut"] = True
            self.gate_exited.set()

        pyrenderdoc.Replay().AsyncInvoke(occupy_replay, "eid-scrub-smoke-gate")
        self.phase = "gate"

    def issue_inputs(self):
        name = self.case_names[len(self.result["cases"])]
        flags = QtCore.QItemSelectionModel.ClearAndSelect | QtCore.QItemSelectionModel.Rows
        started = time.monotonic()

        if name == "event_browser_return_to_committed":
            self.selection_model.setCurrentIndex(self.b_index, flags)
            self.selection_model.setCurrentIndex(self.a_index, flags)
        elif name.startswith("held_arrow_"):
            key = QtCore.Qt.Key_Up if self.reverse_arrow else QtCore.Qt.Key_Down
            for press in range(3):
                self.send_key(QtCore.QEvent.KeyPress, key, auto_repeat=press != 0)
            if self.event_pair(self.tree.currentIndex()) != self.arrow_events[1 if self.reverse_arrow else 3]:
                raise RuntimeError("Three real direction-key presses did not move to the expected row")
            if not pyrenderdoc.IsScrubbing():
                raise RuntimeError("Direction-key presses did not start a held gesture")
            if name.startswith("held_arrow_release_pending"):
                self.send_key(QtCore.QEvent.KeyRelease, key)
                if pyrenderdoc.IsScrubbing():
                    raise RuntimeError("Final key release did not end the pending gesture")
                self.selection_model.currentChanged.connect(self.record_released_selection)
        else:
            for selected, effective in (self.b, self.a):
                pyrenderdoc.ScrubToSelectedEvent(selected, effective)
                if name != "held_scrub_return_to_committed":
                    pyrenderdoc.EndScrub()
            pyrenderdoc.EndScrub()

        self.case_result.update({
            "inputMilliseconds": (time.monotonic() - started) * 1000.0,
            "committedBeforeRelease": [int(pyrenderdoc.CurSelectedEvent()),
                                       int(pyrenderdoc.CurEvent())],
            "gateStillOccupied": not self.gate_exited.is_set(),
        })
        self.target = self.arrow_events[1 if self.reverse_arrow else 3] if name.startswith("held_arrow_") else self.a
        self.checkpoint = "D" if name.startswith("held_arrow_") else "A"
        self.release_gate.set()
        self.phase = "publication"

    def record_released_selection(self, current, previous):
        self.released_selection_changes.append(list(self.event_pair(current)))

    def publication_ready(self):
        self.poll_log()
        matching = [match for line in self.log_lines for match in [_COMPLETION.match(line)]
                    if match and int(match.group(2)) == self.target[1]]
        complete = [int(match.group(3)) for match in matching if match.group(1) == "scrub"]
        previews = [int(match.group(3)) for match in matching if match.group(1) == "preview"]
        common = set(complete).intersection(previews)
        state = self.state()
        if not common or state["selected"] != self.target[0] or state["current"] != self.target[1]:
            return False
        if state["displayedEvent"] != self.target[1] or state["displayedResource"] != state["textureResource"]:
            return False
        self.generation = max(common)
        return True

    def queue_drain(self, phase="verify"):
        self.drained = threading.Event()

        def after_publication(controller):
            try:
                fatal = controller.GetFatalErrorStatus()
                self.queue_result.update({"replayEvent": int(controller.GetCurrentEventID()),
                                          "pipeline": self.pipeline_state(controller.GetPipelineState()),
                                          "fatal": str(fatal), "fatalSucceeded": fatal.code == rd.ResultCode.Succeeded})
            except Exception as exc:
                self.queue_result["error"] = str(exc)
            finally:
                self.drained.set()

        pyrenderdoc.Replay().AsyncInvoke(after_publication, "eid-scrub-smoke-sentinel")
        self.phase = phase

    def verify_checkpoint(self):
        state = self.state()
        if (state["selected"] != self.target[0] or state["current"] != self.target[1]
                or state["treeSelection"] != list(self.target)
                or state["displayedEvent"] != self.target[1]
                or state["displayedResource"] != state["textureResource"]
                or self.queue_result.get("replayEvent") != self.target[1]
                or self.queue_result.get("pipeline") != state["pipeline"]
                or not self.queue_result.get("fatalSucceeded")):
            raise RuntimeError("A completed navigation did not agree across display, selection and live pipeline")
        if not self.case_result["gateStillOccupied"] or self.case_result["committedBeforeRelease"] != list(self.base):
            raise RuntimeError("The deterministic input gate was not held while issuing navigation")
        if self.queue_result.get("gateTimedOut"):
            raise RuntimeError("The deterministic replay gate timed out")
        self.case_result["checkpoints"].append({"name": self.checkpoint, "generation": self.generation,
                                                "state": state, "replay": dict(self.queue_result),
                                                "uiLogTail": self.log_lines[-8:]})
        if self.case_result["name"] == "held_arrow_A_D_E":
            if not state["isScrubbing"] or state["treeSelection"] != list(self.target):
                raise RuntimeError("Event publication released the held key or moved Qt's navigation cursor")
            if self.checkpoint == "D":
                self.reset_log()
                self.send_key(QtCore.QEvent.KeyRelease, auto_repeat=True)
                if not pyrenderdoc.IsScrubbing():
                    raise RuntimeError("An auto-repeat key release ended the held gesture")
                self.send_key(QtCore.QEvent.KeyPress, auto_repeat=True)
                self.target = self.arrow_events[4]
                self.checkpoint = "E"
                if self.event_pair(self.tree.currentIndex()) != self.target:
                    raise RuntimeError("A held repeat after publishing D did not advance to E")
                self.phase = "publication"
                return
            self.send_key(QtCore.QEvent.KeyRelease, QtCore.Qt.Key_Shift)
            if not pyrenderdoc.IsScrubbing():
                raise RuntimeError("An unrelated key release ended the held gesture")
            self.reset_log()
            self.send_key(QtCore.QEvent.KeyRelease)
            if pyrenderdoc.IsScrubbing():
                raise RuntimeError("The final direction-key release did not end the held gesture")
            self.queue_drain("verify_release")
            return
        self.complete_case()

    def complete_case(self):
        if self.case_result["name"].startswith("held_arrow_release_pending"):
            self.selection_model.currentChanged.disconnect(self.record_released_selection)
            self.case_result["selectionChangesAfterRelease"] = self.released_selection_changes
            if any(pair != list(self.target) for pair in self.released_selection_changes):
                raise RuntimeError("An obsolete replay moved the selected row after key release")
        self.case_result["ok"] = True
        self.result["cases"].append(self.case_result)
        if len(self.result["cases"]) == len(self.case_names):
            self.result["ok"] = all(case["ok"] for case in self.result["cases"])
            self.finish()
        else:
            self.begin_case()

    def tick(self):
        try:
            if time.monotonic() > self.deadline:
                raise RuntimeError("EID scrub runtime regression timed out")
            if self.phase == "loading":
                if not pyrenderdoc.IsCaptureLoaded():
                    return
                self.original = (pyrenderdoc.CurSelectedEvent(), pyrenderdoc.CurEvent())
                pyrenderdoc.ShowTextureViewer()
                self.viewer = pyrenderdoc.GetTextureViewer()
                self.viewer_widget = self.viewer.Widget()
                self.render = self.viewer_widget.findChild(QtWidgets.QWidget, "render")
                if self.render is None:
                    raise RuntimeError("Texture Viewer render widget is unavailable")
                self.find_events()
                self.case_names = ["context_return_to_committed",
                                   "event_browser_return_to_committed",
                                   "held_scrub_return_to_committed"]
                if self.arrow_indices:
                    self.case_names.append("held_arrow_A_D_E")
                    self.case_names.append("held_arrow_release_pending")
                    self.case_names.append("held_arrow_release_pending_up")
                self.begin_case()
            elif self.phase == "gate" and self.gate_entered.is_set():
                self.issue_inputs()
            elif self.phase == "publication" and self.publication_ready():
                self.queue_drain()
            elif self.phase == "verify" and self.drained.is_set():
                self.verify_checkpoint()
            elif self.phase == "verify_release" and self.drained.is_set():
                state = self.state()
                self.poll_log()
                if (state["isScrubbing"] or state["current"] != self.target[1]
                        or state["selected"] != self.target[0]
                        or state["treeSelection"] != list(self.target)
                        or state["displayedEvent"] != self.target[1]
                        or state["displayedResource"] != state["textureResource"]
                        or self.queue_result.get("pipeline") != state["pipeline"]
                        or not self.queue_result.get("fatalSucceeded")
                        or self.queue_result.get("replayEvent") != self.target[1]
                        or any(_COMPLETION.match(line) for line in self.log_lines)):
                    raise RuntimeError("Releasing a fully published arrow step replayed or changed the event")
                self.case_result["releasedState"] = state
                self.complete_case()
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
        self.release_gate.set()
        if error is not None:
            self.result["error"] = "%s: %s" % (type(error).__name__, str(error))
            self.result["failurePhase"] = self.phase
            self.result["traceback"] = traceback.format_exc()
            self.result["failedCase"] = getattr(self, "case_result", None)
            self.result["lastReplayState"] = getattr(self, "queue_result", None)
            try:
                self.poll_log()
                self.result["failureState"] = self.state()
                self.result["uiLogTail"] = self.log_lines[-20:]
            except Exception as exc:
                self.result["diagnosticError"] = str(exc)
        if getattr(self, "tree", None) is not None:
            # Ensure test failures do not leave the Event Browser's held-key tracking active.
            try:
                self.send_key(QtCore.QEvent.KeyRelease)
                self.send_key(QtCore.QEvent.KeyRelease, QtCore.Qt.Key_Up)
            except Exception as exc:
                self.result["keyCleanupError"] = str(exc)
        if self.original is not None and pyrenderdoc.IsCaptureLoaded():
            try:
                pyrenderdoc.SetEventID([], self.original[0], self.original[1], False)
            except Exception as exc:
                self.result["restoreError"] = str(exc)
                self.result["ok"] = False
        encoded = json.dumps(self.result, sort_keys=True, separators=(",", ":"))
        print("RENDERDOC_EID_SCRUB_SMOKE=" + encoded)
        if self.output_path:
            with open(self.output_path, "w") as output:
                output.write(encoded)
        if os.environ.get("RENDERDOC_EID_SCRUB_SMOKE_QUIT") == "1":
            self.application.closeAllWindows()
            QtCore.QTimer.singleShot(0, lambda: self.application.exit(0))


runner = _ScrubSmokeRunner()
runner.application._eid_scrub_smoke_runner = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
