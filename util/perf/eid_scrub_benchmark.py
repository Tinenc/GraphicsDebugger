"""Run inside qrenderdoc with --python; see README.md for environment variables.

Held steps wait for matching, newly appended preview + GUI-completion timing records.
Every completed step must publish CurEvent, selection, live pipeline and texture binding.
"""

from __future__ import print_function

import json
import os
import re
import threading
import time
import traceback

import renderdoc as rd
from PySide2 import QtCore, QtWidgets


_UI_RECORD = re.compile(
    r"^(preview|scrub|commit) EID (\d+): (.*), generation (\d+)(?:, state (\w+))?"
    r"(?:, displayAndReads ([\d.]+)ms)?$")
_DRIVER_RECORD = re.compile(r"\bapi=(ScrubReplay|ScrubSnapshotHit)\s+eid=(\d+)\b")


class _LogTail(object):
    def __init__(self, path):
        self.path = path
        self.reset()

    def reset(self):
        self.offset = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        self.partial = b""
        self.lines = []

    def poll(self):
        if not os.path.exists(self.path):
            return
        if os.path.getsize(self.path) < self.offset:
            raise RuntimeError("Timing log was truncated during the benchmark: " + self.path)
        try:
            with open(self.path, "rb") as source:
                source.seek(self.offset)
                data = source.read()
                self.offset = source.tell()
        except PermissionError:
            # The Windows timing writer briefly holds an exclusive file handle while appending.
            # Leave the cursor untouched and retry on the next bounded timer tick.
            return
        pieces = (self.partial + data).split(b"\n")
        self.partial = pieces.pop()
        self.lines.extend(line.decode("utf-8", "replace").rstrip("\r") for line in pieces)


class _Benchmark(object):
    def __init__(self):
        self.application = QtWidgets.QApplication.instance()
        self.result = {"schema": "renderdoc-eid-scrub-benchmark/v3", "ok": False,
                       "held": {"forward": [], "reverse": []}, "sequential": [],
                       "burst": {}, "presentation": []}
        self.timer = None
        self.finished = False
        self.finishing = False
        self.phase = "startup"
        self.pending = None
        self.last_request = None
        self.original = None
        self.output_path = os.environ.get("RENDERDOC_EID_BENCHMARK_OUTPUT", "")

    def start(self):
        try:
            if os.environ.get("RENDERDOC_EID_TIMING") != "1":
                raise RuntimeError("Start this qrenderdoc process with RENDERDOC_EID_TIMING=1")
            tokens = re.split(r"[\s,;]+", os.environ.get("RENDERDOC_EID_BENCHMARK_EVENTS", "").strip())
            self.events = [int(token) for token in tokens if token]
            self.held_only = os.environ.get("RENDERDOC_EID_BENCHMARK_HELD_ONLY") == "1"
            self.require_output = os.environ.get("RENDERDOC_EID_BENCHMARK_REQUIRE_OUTPUT") == "1"
            if len(self.events) < 2 or min(self.events) <= 0 or len(set(self.events)) != len(self.events):
                raise ValueError("RENDERDOC_EID_BENCHMARK_EVENTS needs at least two distinct positive action EIDs")
            self.timeout = float(os.environ.get("RENDERDOC_EID_BENCHMARK_TIMEOUT", "30"))
            self.poll_ms = int(os.environ.get("RENDERDOC_EID_BENCHMARK_POLL_MS", "5"))
            self.continuous_ms = int(os.environ.get("RENDERDOC_EID_BENCHMARK_CONTINUOUS_MS", "600"))
            if self.timeout <= 0 or self.poll_ms < 1:
                raise ValueError("Timeout and polling interval must be positive")
            if self.continuous_ms <= 0:
                raise ValueError("RENDERDOC_EID_BENCHMARK_CONTINUOUS_MS must be positive")
            directory = self.application.applicationDirPath()
            self.ui_log = _LogTail(os.environ.get("RENDERDOC_EID_BENCHMARK_UI_LOG",
                                                  os.path.join(directory, "eid_timing.log")))
            self.driver_log = _LogTail(os.environ.get("RENDERDOC_EID_BENCHMARK_DRIVER_LOG",
                                                      os.path.join(directory, "eid_timing_driver.log")))
            self.result.update({"events": self.events, "pollIntervalMs": self.poll_ms,
                                "buildLabel": os.environ.get("RENDERDOC_EID_BENCHMARK_BUILD_LABEL", "unspecified"),
                                "executable": self.application.applicationFilePath(),
                                "conservativeReplaySync": os.environ.get("RENDERDOC_D3D12_CONSERVATIVE_REPLAY_SYNC", "0"),
                                "uiTimingLog": self.ui_log.path, "driverTimingLog": self.driver_log.path,
                                "completionEvidence": "matching new preview and scrub generation after live pipeline publication and queued panel/display work; not monitor scanout",
                                "phaseOrder": (["held.forward", "held.reverse", "held.continuous", "held.release"]
                                               + ([] if self.held_only else ["sequential", "burst", "presentation"]))})
            self.timer = QtCore.QTimer(self.application)
            self.timer.setTimerType(QtCore.Qt.PreciseTimer)
            self.timer.setInterval(self.poll_ms)
            self.timer.timeout.connect(self.tick)
            self.deadline = time.monotonic() + 120.0
            self.phase = "loading"
            capture = os.environ.get("RENDERDOC_EID_BENCHMARK_CAPTURE", "")
            if not pyrenderdoc.IsCaptureLoaded() and capture:
                pyrenderdoc.LoadCapture(capture, rd.ReplayOptions(), capture, False, True)
            self.timer.start()
        except Exception as exc:
            self.finish(exc)

    def prepare(self):
        self.original = (int(pyrenderdoc.CurSelectedEvent()), int(pyrenderdoc.CurEvent()))
        self.main_window = pyrenderdoc.GetMainWindow()
        self.main_window_widget = self.main_window.Widget()
        known = {}

        def visit(actions):
            for action in actions:
                known[int(action.eventId)] = action
                visit(action.children)

        visit(pyrenderdoc.CurRootActions())
        self.known_actions = known
        self.texture_ids = set(str(texture.resourceId) for texture in pyrenderdoc.GetTextures())
        for event in self.events:
            if event not in known or known[event].children or (known[event].flags & rd.ActionFlags.MultiAction):
                raise ValueError("EID %d must be an actual leaf action, not a remapped parent" % event)
        pyrenderdoc.ShowTextureViewer()
        # Keep the entire PySide wrapper ownership chain alive.
        self.viewer = pyrenderdoc.GetTextureViewer()
        self.viewer_widget = self.viewer.Widget()
        self.render_widget = self.viewer_widget.findChild(QtWidgets.QWidget, "render")
        self.channels_widget = self.viewer_widget.findChild(QtWidgets.QComboBox, "channels")
        self.saved_display = (self.viewer.GetTextureOverlay(), self.viewer.IsZoomAutoFit(),
                              self.viewer.GetZoomLevel(), self.viewer.GetHistogramRange(),
                              self.viewer.GetChannelVisibilityBits())
        wanted_texture = os.environ.get("RENDERDOC_EID_BENCHMARK_TEXTURE", "").strip()
        if wanted_texture:
            wanted_texture = str(int(wanted_texture))
            matches = [texture for texture in pyrenderdoc.GetTextures()
                       if str(texture.resourceId).split("::")[-1] == wanted_texture]
            if len(matches) != 1:
                raise ValueError("RENDERDOC_EID_BENCHMARK_TEXTURE must match one captured texture ResourceId")
            texture = matches[0]
            self.viewer.ViewTexture(texture.resourceId, rd.CompType.Typeless, True)
            self.result.update({"textureBindingMode": "explicit locked texture across events",
                                "textureResource": str(texture.resourceId),
                                "textureWidth": int(texture.width), "textureHeight": int(texture.height)})
        else:
            self.result["textureBindingMode"] = "existing Texture Viewer binding"
        self.viewer.SetTextureOverlay(rd.DebugOverlay.NoOverlay)
        # Starting on an already committed target is intentionally a no-op in CaptureContext.
        # Move to frame start only in this case; do not warm the requested sweep beforehand.
        if int(pyrenderdoc.CurEvent()) == self.events[0]:
            pyrenderdoc.SetEventID([], 0, 0, True)
        self.result["initialCommittedEventId"] = int(pyrenderdoc.CurEvent())
        self.result["startupSelectedEventId"] = self.original[0]
        self.result["startupCurrentEventId"] = self.original[1]
        self.phase = "ready"
        self.deadline = time.monotonic() + self.timeout
        self.ready_after = time.monotonic() + 1.0

    def assert_visible_simple_texture(self):
        if self.render_widget is None or not self.render_widget.isVisible() or self.render_widget.width() <= 0 or self.render_widget.height() <= 0:
            raise RuntimeError("The main Texture Viewer output must remain visible")
        if self.channels_widget is None or self.channels_widget.currentIndex() != 0:
            raise RuntimeError("Use the ordinary RGBA Texture Viewer mode, without custom shader or YUV decode")
        if self.viewer.GetTextureOverlay() != rd.DebugOverlay.NoOverlay:
            raise RuntimeError("An overlay was enabled during the benchmark")

    def begin(self, kind, target):
        self.assert_visible_simple_texture()
        self.ui_log.reset()
        self.driver_log.reset()
        self.pending = {"kind": kind, "target": target, "start": time.perf_counter(),
                        "deadline": time.monotonic() + self.timeout}
        self.last_request = self.pending

    def held_step(self):
        if self.held_index == len(self.held_targets):
            self.begin("continuous", self.events[1])
            self.pending["observations"] = []
            self.pending["stateSamples"] = []
            self.pending["pipelineWindow"] = None
            self.pending["calls"] = 0
            self.pending["seenGenerations"] = set()
            self.phase = "continuous"
            return
        direction, target = self.held_targets[self.held_index]
        self.begin("held", target)
        self.pending["direction"] = direction
        pyrenderdoc.ScrubToEvent(target)
        self.pending["callMs"] = (time.perf_counter() - self.pending["start"]) * 1000.0
        if not pyrenderdoc.IsScrubbing():
            raise RuntimeError("Held step failed to start a real scrub gesture")

    def sequential_step(self):
        if self.sequential_index == len(self.events):
            self.begin("burst", self.events[0])
            for event in reversed(self.events):
                pyrenderdoc.ScrubToEvent(event)
            self.pending["callMs"] = (time.perf_counter() - self.pending["start"]) * 1000.0
            pyrenderdoc.EndScrub()
            return
        target = self.events[self.sequential_index]
        self.begin("sequential", target)
        unchanged = (int(pyrenderdoc.CurSelectedEvent()) == target and int(pyrenderdoc.CurEvent()) == target)
        pyrenderdoc.ScrubToEvent(target)
        self.pending["callMs"] = (time.perf_counter() - self.pending["start"]) * 1000.0
        pyrenderdoc.EndScrub()
        if unchanged:
            self.pending["noOpAlreadyCommitted"] = True
            self.queue_drain()

    def queue_drain(self):
        self.drained = threading.Event()
        self.drain_error = None
        self.replay_state = None

        def sentinel(controller):
            try:
                status = controller.GetFatalErrorStatus()
                if status.code != rd.ResultCode.Succeeded:
                    self.drain_error = str(status)
                self.replay_state = {"eventId": int(controller.GetCurrentEventID()),
                                     "pipeline": self.pipeline_state(controller.GetPipelineState()),
                                     "fatal": {"code": str(status.code), "message": str(status)}}
            except Exception as exc:
                self.drain_error = str(exc)
            finally:
                self.drained.set()

        pyrenderdoc.Replay().AsyncInvoke(sentinel, "eid-benchmark-drain")
        self.pending["draining"] = True

    @staticmethod
    def pipeline_state(pipe):
        return {"shaders": {str(stage): str(pipe.GetShader(stage))
                             for stage in (rd.ShaderStage.Vertex, rd.ShaderStage.Hull,
                                           rd.ShaderStage.Domain, rd.ShaderStage.Geometry,
                                           rd.ShaderStage.Pixel, rd.ShaderStage.Compute,
                                           rd.ShaderStage.Task, rd.ShaderStage.Mesh)},
                "outputs": [str(binding.resource) for binding in pipe.GetOutputTargets()]}

    def available_output_textures(self):
        action = self.known_actions.get(int(pyrenderdoc.CurEvent()))
        if action is None:
            return []
        pipe = pyrenderdoc.CurPipelineState()
        candidates = []
        if action.flags & (rd.ActionFlags.Copy | rd.ActionFlags.Resolve |
                           rd.ActionFlags.Present | rd.ActionFlags.Clear):
            candidates.append(str(action.copyDestination))
        elif (action.flags & rd.ActionFlags.Dispatch and
              pipe.GetShader(rd.ShaderStage.Compute) != rd.ResourceId.Null()):
            candidates.extend(str(binding.descriptor.resource)
                              for binding in pipe.GetReadWriteResources(rd.ShaderStage.Compute, True)
                              if not binding.access.staticallyUnused)
        else:
            candidates.extend(str(binding.resource) for binding in pipe.GetOutputTargets())
            candidates.append(str(pipe.GetDepthTarget().resource))
            candidates.append(str(pipe.GetDepthResolveTarget().resource))
            for stage in (rd.ShaderStage.Vertex, rd.ShaderStage.Hull, rd.ShaderStage.Domain,
                          rd.ShaderStage.Geometry, rd.ShaderStage.Pixel,
                          rd.ShaderStage.Task, rd.ShaderStage.Mesh):
                candidates.extend(str(binding.descriptor.resource)
                                  for binding in pipe.GetReadWriteResources(stage, True)
                                  if not binding.access.staticallyUnused)
        return sorted(set(candidates).intersection(self.texture_ids))

    def published_state(self, validate=True):
        resource = str(self.viewer.GetCurrentResource())
        if validate and "textureResource" in self.result and resource != self.result["textureResource"]:
            raise RuntimeError("An explicitly locked texture changed during navigation")
        outputs = self.available_output_textures()
        if validate and self.require_output and "textureResource" not in self.result and outputs and resource not in outputs:
            raise RuntimeError("Texture Viewer did not follow an available output at EID %d: %s, available %s" %
                               (int(pyrenderdoc.CurEvent()), resource, outputs))
        return {"currentEventId": int(pyrenderdoc.CurEvent()),
                "selectedEventId": int(pyrenderdoc.CurSelectedEvent()),
                "textureResource": resource,
                "textureBound": self.viewer.GetCurrentResource() != rd.ResourceId.Null(),
                "availableOutputTextures": outputs,
                "displayedEventId": self.render_widget.property("eidDisplayedEvent"),
                "displayedResource": self.render_widget.property("eidDisplayedResource"),
                "pipelinePublicationSerial": self.main_window_widget.property("pipelinePublicationSerial"),
                "pipeline": self.pipeline_state(pyrenderdoc.CurPipelineState())}

    def continuous_tick(self):
        pending = self.pending
        if time.monotonic() > pending["deadline"]:
            raise RuntimeError("Continuous drag completion timed out")
        self.ui_log.poll()
        self.driver_log.poll()
        if any("api=ScrubSnapshotHit " in line for line in self.driver_log.lines):
            raise RuntimeError("Continuous dragging used an interactive image snapshot")
        state = self.published_state()
        observed_ms = round((time.perf_counter() - pending["start"]) * 1000.0, 3)
        sample = {"observedMs": observed_ms, "state": state}
        if state["pipelinePublicationSerial"] is None:
            raise RuntimeError("This build does not expose the pipeline-publication completion serial")
        previous = pending["pipelineWindow"]
        pending["stateSamples"].append(sample)
        if len(pending["stateSamples"]) > 4096:
            raise RuntimeError("Continuous probe exceeded its 4096-sample report limit")
        if previous is not None and previous["state"]["pipelinePublicationSerial"] == state["pipelinePublicationSerial"]:
            if previous["state"]["currentEventId"] != state["currentEventId"] or previous["state"]["pipeline"] != state["pipeline"]:
                self.result["held"]["continuous"] = {"calls": pending["calls"],
                                                       "stateSamples": pending["stateSamples"],
                                                       "publishedWhileInputActive": pending["observations"],
                                                       "pipelineInstability": {"before": previous, "after": sample}}
                raise RuntimeError("Published pipeline metadata changed without a new GUI publication")
        else:
            pending["pipelineWindow"] = sample
        for line in self.ui_log.lines:
            match = _UI_RECORD.match(line)
            if not match or match.group(1) != "scrub":
                continue
            event, generation = int(match.group(2)), int(match.group(4))
            if generation in pending["seenGenerations"]:
                continue
            preview = [candidate for candidate in self.ui_log.lines
                       if _UI_RECORD.match(candidate)
                       and _UI_RECORD.match(candidate).group(1) == "preview"
                       and int(_UI_RECORD.match(candidate).group(2)) == event
                       and int(_UI_RECORD.match(candidate).group(4)) == generation]
            driver = [candidate for candidate in self.driver_log.lines
                      if _DRIVER_RECORD.search(candidate)
                      and int(_DRIVER_RECORD.search(candidate).group(2)) == event]
            if (not preview or not driver or state["currentEventId"] != event
                    or state["selectedEventId"] != event or state["displayedEventId"] != event
                    or state["displayedResource"] != state["textureResource"]):
                continue
            pending["seenGenerations"].add(generation)
            pending["observations"].append({"eventId": event, "generation": generation,
                                           "observedMs": round((time.perf_counter() - pending["start"]) * 1000.0, 3),
                                           "state": state, "previewLog": preview[-1],
                                           "completionLog": line, "driverLog": driver[-1]})
        elapsed = (time.perf_counter() - pending["start"]) * 1000.0
        if elapsed >= self.continuous_ms:
            report = {"durationMs": round(elapsed, 3), "requestedDurationMs": self.continuous_ms,
                      "calls": pending["calls"], "publishedWhileInputActive": pending["observations"],
                      "stateSamples": pending["stateSamples"], "pipelineStableWithinPublication": True}
            self.result["held"]["continuous"] = report
            if len(pending["observations"]) < 2:
                raise RuntimeError("Continuous input did not publish at least two live frames before release")
            # Force a distinct final request after the input stream. Its fresh log records are
            # required; an older occurrence of the same EID cannot satisfy final completion.
            target = self.events[pending["calls"] % len(self.events)]
            self.phase = "benchmark"
            self.begin("continuous_final", target)
            pyrenderdoc.ScrubToEvent(target)
            return
        target = self.events[pending["calls"] % len(self.events)]
        pending["calls"] += 1
        pyrenderdoc.ScrubToEvent(target)

    def poll_pending(self):
        pending = self.pending
        if time.monotonic() > pending["deadline"]:
            raise RuntimeError("%s EID %d completion timed out" % (pending["kind"], pending["target"]))
        if pending.get("draining"):
            if self.drained.is_set():
                if self.drain_error:
                    raise RuntimeError(self.drain_error)
                self.complete_pending()
            return
        self.ui_log.poll()
        self.driver_log.poll()
        if any("api=ScrubSnapshotHit " in line for line in self.driver_log.lines):
            raise RuntimeError("Interactive dragging used an event image snapshot")
        records = []
        for line in self.ui_log.lines:
            match = _UI_RECORD.match(line)
            if match and int(match.group(2)) == pending["target"]:
                records.append({"kind": match.group(1), "generation": int(match.group(4)),
                                "state": match.group(5), "line": line})
        is_held = pending["kind"] in ("held", "continuous_final")
        completion_kind = "scrub"
        for completion in reversed(records):
            if completion["kind"] != completion_kind:
                continue
            generation = completion["generation"]
            if pending["kind"] != "held_release":
                previews = [record for record in records if record["kind"] == "preview"
                            and record["generation"] == generation
                            and record["state"] in (("dragging",) if is_held else ("dragging", "released"))]
                if not previews:
                    continue
                driver = [line for line in self.driver_log.lines if _DRIVER_RECORD.search(line)
                          and int(_DRIVER_RECORD.search(line).group(2)) == pending["target"]]
                if not driver:
                    continue
                pending["previewLog"] = previews[-1]["line"]
                pending["driverLog"] = driver[-1]
            pending["completionLog"] = completion["line"]
            pending["generation"] = generation
            pending["completionObservedMs"] = (time.perf_counter() - pending["start"]) * 1000.0
            self.queue_drain()
            return

    def complete_pending(self):
        pending = self.pending
        self.pending = None
        entry = {key: value for key, value in pending.items()
                 if key not in ("kind", "target", "start", "deadline", "draining", "direction")}
        entry["eventId"] = pending["target"]
        entry["settledMs"] = (time.perf_counter() - pending["start"]) * 1000.0
        for key in ("callMs", "settledMs", "completionObservedMs"):
            if key in entry:
                entry[key] = round(entry[key], 3)
        state = self.published_state()
        entry["publishedState"] = state
        if state["currentEventId"] != pending["target"] or state["selectedEventId"] != pending["target"]:
            raise RuntimeError("The completed scrub did not publish the requested event")
        if state["displayedEventId"] != pending["target"] or state["displayedResource"] != state["textureResource"]:
            raise RuntimeError("The displayed event/resource does not match the published Texture Viewer binding")
        if self.replay_state is None or self.replay_state["eventId"] != pending["target"] or self.replay_state["pipeline"] != state["pipeline"]:
            raise RuntimeError("Published pipeline state does not match the live replay controller")
        if pending["kind"] in ("held", "continuous_final"):
            self.assert_visible_simple_texture()
            entry["isScrubbing"] = bool(pyrenderdoc.IsScrubbing())
            if not entry["isScrubbing"]:
                raise RuntimeError("Held step unexpectedly released the gesture")
            if self.last_generation is not None and entry["generation"] <= self.last_generation:
                raise RuntimeError("Held preview generations did not advance")
            self.last_generation = entry["generation"]
            if pending["kind"] == "continuous_final":
                self.result["held"]["continuous"]["final"] = entry
                self.begin("held_release", pending["target"])
                pyrenderdoc.EndScrub()
                self.queue_drain()
                return
            self.result["held"][pending["direction"]].append(entry)
            self.held_index += 1
            self.held_step()
            return
        if pending["kind"] == "held_release":
            if pyrenderdoc.IsScrubbing():
                raise RuntimeError("EndScrub did not end the input gesture")
            self.result["held"]["release"] = entry
            if self.held_only:
                self.result["ok"] = True
                self.finish()
                return
            self.sequential_index = 0
            self.sequential_step()
        elif pending["kind"] == "sequential":
            self.result["sequential"].append(entry)
            self.sequential_index += 1
            self.sequential_step()
        elif pending["kind"] == "burst":
            entry.update({"calls": len(self.events), "target": self.events[0],
                          "queueCallsMs": entry.pop("callMs"), "holdBeforeCommitMs": 0})
            self.result["burst"] = entry
            self.presentation_index = 0
            self.presentation_step()
        elif pending["kind"] == "presentation":
            self.result["presentation"].append(entry)
            self.presentation_index += 1
            self.presentation_step()

    def presentation_step(self):
        operations = [
            ("zoom-125", lambda: self.viewer.SetZoomLevel(False, 1.25)),
            ("zoom-175", lambda: self.viewer.SetZoomLevel(False, 1.75)),
            ("range-10-90", lambda: self.viewer.SetHistogramRange(0.1, 0.9)),
            ("range-20-80", lambda: self.viewer.SetHistogramRange(0.2, 0.8)),
            ("channels-r", lambda: self.viewer.SetChannelVisibility(True, False, False, False)),
            ("channels-rgb", lambda: self.viewer.SetChannelVisibility(True, True, True, False)),
        ]
        if self.presentation_index == len(operations):
            self.result["ok"] = True
            self.finish()
            return
        name, operation = operations[self.presentation_index]
        self.begin("presentation", int(pyrenderdoc.CurEvent()))
        self.pending["name"] = name
        operation()
        self.pending["callMs"] = (time.perf_counter() - self.pending["start"]) * 1000.0
        self.queue_drain()

    def tick(self):
        try:
            if self.phase == "loading":
                if pyrenderdoc.IsCaptureLoaded():
                    self.prepare()
                elif time.monotonic() > self.deadline:
                    raise RuntimeError("Capture load timed out")
            elif self.phase == "ready" and time.monotonic() >= self.ready_after:
                self.assert_visible_simple_texture()
                self.phase = "benchmark"
                self.held_targets = ([("forward", event) for event in self.events]
                                     + [("reverse", event) for event in reversed(self.events[:-1])])
                self.held_index = 0
                self.last_generation = None
                self.held_step()
            elif self.phase == "benchmark" and self.pending is not None:
                self.poll_pending()
            elif self.phase == "continuous":
                self.continuous_tick()
        except Exception as exc:
            self.finish(exc)

    def finish(self, error=None):
        if self.finished or self.finishing:
            return
        self.finishing = True
        if self.timer is not None:
            self.timer.stop()
        if error is not None:
            self.result.update({"ok": False, "error": "%s: %s" % (type(error).__name__, error),
                                "failurePhase": self.phase, "traceback": traceback.format_exc()})
            failed = self.pending or self.last_request
            if failed:
                self.result["failedRequest"] = {key: failed.get(key)
                                                for key in ("kind", "target", "direction", "generation", "draining")}
            diagnostics = {"lastReplayState": getattr(self, "replay_state", None),
                           "lastDrainError": getattr(self, "drain_error", None)}
            self.result["failureDiagnostics"] = diagnostics
            try:
                diagnostics["publishedState"] = self.published_state(validate=False)
                diagnostics["isScrubbing"] = bool(pyrenderdoc.IsScrubbing())
            except Exception as exc:
                diagnostics["publishedStateError"] = str(exc)
            for name, tail in (("ui", getattr(self, "ui_log", None)),
                               ("driver", getattr(self, "driver_log", None))):
                if tail is None:
                    continue
                try:
                    tail.poll()
                    diagnostics[name + "LogTail"] = {"path": tail.path, "offset": tail.offset,
                                                      "lineCount": len(tail.lines), "lines": tail.lines[-20:],
                                                      "partialLine": tail.partial.decode("utf-8", "replace")}
                except Exception as exc:
                    diagnostics[name + "LogTailError"] = str(exc)
            if failed:
                target = failed["target"]
                records = [match for line in getattr(self.ui_log, "lines", [])
                           for match in [_UI_RECORD.match(line)]
                           if match and int(match.group(2)) == target]
                state = diagnostics.get("publishedState", {})
                diagnostics["completionChecks"] = {
                    "currentEventMatches": state.get("currentEventId") == target,
                    "selectedEventMatches": state.get("selectedEventId") == target,
                    "displayedEventMatches": state.get("displayedEventId") == target,
                    "displayedResourceMatches": bool(state) and state.get("displayedResource") == state.get("textureResource"),
                    "previewGenerations": [int(record.group(4)) for record in records if record.group(1) == "preview"],
                    "scrubGenerations": [int(record.group(4)) for record in records if record.group(1) == "scrub"],
                    "driverReplayCount": sum(1 for line in getattr(self.driver_log, "lines", [])
                                             for match in [_DRIVER_RECORD.search(line)]
                                             if match and match.group(1) == "ScrubReplay" and int(match.group(2)) == target),
                    "drainFinished": getattr(self, "drained", threading.Event()).is_set(),
                }
            # A failure can itself be a stalled replay queue. Probe fatal state asynchronously,
            # with a GUI deadline; never BlockInvoke solely to produce a timeout diagnostic.
            self.failure_probe_done = threading.Event()
            self.failure_probe_deadline = time.monotonic() + 1.5

            def failure_probe(controller):
                try:
                    status = controller.GetFatalErrorStatus()
                    diagnostics["fatal"] = {"code": str(status.code), "message": str(status),
                                            "eventId": int(controller.GetCurrentEventID())}
                except Exception as exc:
                    diagnostics["fatalProbeError"] = str(exc)
                finally:
                    self.failure_probe_done.set()

            try:
                pyrenderdoc.Replay().AsyncInvoke(failure_probe, "eid-benchmark-failure-probe")
                self.failure_timer = QtCore.QTimer(self.application)
                self.failure_timer.setInterval(25)
                self.failure_timer.timeout.connect(self.poll_failure_probe)
                self.failure_timer.start()
                return
            except Exception as exc:
                diagnostics["fatalProbeError"] = str(exc)
        self.publish_result()

    def poll_failure_probe(self):
        if not self.failure_probe_done.is_set() and time.monotonic() < self.failure_probe_deadline:
            return
        self.failure_timer.stop()
        if not self.failure_probe_done.is_set():
            self.result["failureDiagnostics"]["fatalProbeTimedOut"] = True
            self.result["restoreSkipped"] = "replay queue did not answer the bounded fatal-status probe"
        self.publish_result()

    def publish_result(self):
        if self.finished:
            return
        self.finished = True
        try:
            self.result["selectedEventId"] = int(pyrenderdoc.CurSelectedEvent())
            self.result["currentEventId"] = int(pyrenderdoc.CurEvent())
            self.result["wasStillScrubbing"] = bool(pyrenderdoc.IsScrubbing())
        except Exception:
            pass
        # A normal selection cancels an outstanding generation. Do not call EndScrub then block
        # the GUI: the asynchronous release commit needs that same GUI thread to acknowledge it.
        if (self.original is not None and pyrenderdoc.IsCaptureLoaded()
                and not self.result.get("failureDiagnostics", {}).get("fatalProbeTimedOut")):
            try:
                pyrenderdoc.SetEventID([], self.original[0], self.original[1], False)
                overlay, autofit, zoom, value_range, channels = self.saved_display
                self.viewer.SetTextureOverlay(overlay)
                self.viewer.SetZoomLevel(autofit, zoom)
                self.viewer.SetHistogramRange(value_range[0], value_range[1])
                self.viewer.SetChannelVisibility(*[bool(channels & (1 << bit)) for bit in range(4)])
            except Exception as exc:
                self.result["restoreError"] = str(exc)
                self.result["ok"] = False
        encoded = json.dumps(self.result, sort_keys=True, separators=(",", ":"))
        print("RENDERDOC_EID_BENCHMARK=" + encoded)
        if self.output_path:
            with open(self.output_path, "w", encoding="utf-8") as output:
                output.write(encoded)
        if os.environ.get("RENDERDOC_EID_BENCHMARK_QUIT") == "1":
            self.application.closeAllWindows()
            QtCore.QTimer.singleShot(0, lambda: self.application.exit(0))


runner = _Benchmark()
runner.application._eid_scrub_benchmark_runner = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
