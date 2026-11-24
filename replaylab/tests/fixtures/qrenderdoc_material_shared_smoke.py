"""Isolated --ui-python native Material/CBuffer acceptance (Python 3.6).

Required REPLAYLAB_MATERIAL_SMOKE_CAPTURE, _OUTPUT, _EVENT, _AWAY_EVENT.
Optional _STAGE=fragment, _BLOCK=0, _ARRAY=0, _FIELD=<exact reflected path>,
_VALUE=<JSON scalar/vector>, _SECONDS=1.0, _QUIT=0, _MAX_HEARTBEAT_MS=250,
_TIMEOUT_SECONDS=300. Progress is published to OUTPUT.progress.json and an append-only
OUTPUT.receipts.jsonl. The parent may write OUTPUT.close-request.json with the exact
{"pid": <owned process PID>, "reason": "..."} to request rollback and normal window close.
This is cooperative shutdown; a blocked native/UI thread still requires parent supervision.
The suffixes above all share the REPLAYLAB_MATERIAL_SMOKE prefix.

Choose two actual draws consuming the same original buffer range in a tiny
D3D11/OpenGL/Vulkan fixture. The probe exports a real one-event bridge catalog,
opens the native CBuffer tree and triggers its real context-menu QAction. Native
C++ constructs the selection; no reflection/binding/selection dictionary is forged.
Subsequent native input commits use MiniQt textEdited and the asynchronous Material queue.
This proves native callback/byte/pixel behavior, not OS mouse input or scanout.
The parent owns process startup and GPU exclusivity. No capture/asset is rewritten.
"""
from __future__ import print_function

import hashlib
import json
import math
import os
import struct
import sys
import threading
import time
import traceback

import renderdoc as rd
import qrenderdoc as qrd
from PySide2 import QtCore, QtGui, QtWidgets


PREFIX = "REPLAYLAB_MATERIAL_SMOKE"


def setting(name, default=""):
    return os.environ.get(PREFIX + "_" + name, default)


def output_pixels(ctrl):
    """Every bound color/depth mip and slice, with a 512 MiB total read limit."""
    pipe = ctrl.GetPipelineState()
    textures = {str(texture.resourceId): texture for texture in ctrl.GetTextures()}
    bindings = list(pipe.GetOutputTargets()) + [pipe.GetDepthTarget()]
    result, seen, total = [], set(), 0
    for binding in bindings:
        if binding.resource == rd.ResourceId.Null():
            continue
        texture = textures.get(str(binding.resource))
        if texture is None:
            raise RuntimeError("Bound output lacks texture metadata")
        if int(texture.depth) != 1:
            raise RuntimeError("This bounded Material fixture requires 2D outputs")
        first_mip, mip_count = int(binding.firstMip), int(binding.numMips)
        first_slice, slice_count = int(binding.firstSlice), int(binding.numSlices)
        if (first_mip < 0 or first_mip >= int(texture.mips) or mip_count <= 0 or
                first_slice < 0 or first_slice >= int(texture.arraysize) or slice_count <= 0):
            raise RuntimeError("Invalid bound output view metadata")
        for mip in range(first_mip, min(first_mip + mip_count, int(texture.mips))):
            for array_slice in range(first_slice, min(first_slice + slice_count, int(texture.arraysize))):
                for sample in range(max(1, int(texture.msSamp))):
                    key = (str(binding.resource), mip, array_slice, sample)
                    if key in seen:
                        continue
                    seen.add(key)
                    estimate = max(1, int(texture.width) >> mip) * max(1, int(texture.height) >> mip) * 16
                    if total + estimate > 512 * 1024 * 1024:
                        raise RuntimeError("All-bound-output readback exceeds the 512 MiB limit")
                    raw = bytes(ctrl.GetTextureData(texture.resourceId, rd.Subresource(mip, array_slice, sample)))
                    if not raw:
                        raise RuntimeError("Native output readback is empty")
                    total += len(raw)
                    if total > 512 * 1024 * 1024:
                        raise RuntimeError("Native output bytes exceed the 512 MiB limit")
                    result.append({"resource": key[0], "mip": mip, "slice": array_slice,
                                   "sample": sample, "bytes": len(raw),
                                   "sha256": hashlib.sha256(raw).hexdigest()})
    if not result:
        raise RuntimeError("No bound output pixels were available")
    fatal = ctrl.GetFatalErrorStatus()
    if fatal.code != rd.ResultCode.Succeeded:
        raise RuntimeError("Native replay failed: " + str(fatal))
    return result


class MaterialSharedSmoke(object):
    def __init__(self):
        self.output = os.path.abspath(setting("OUTPUT"))
        self.event = int(setting("EVENT"))
        self.away = int(setting("AWAY_EVENT"))
        if not setting("OUTPUT") or self.event <= 0 or self.away <= 0 or self.event == self.away:
            raise ValueError("Explicit output and two distinct draw EIDs are required")
        self.stage_name = setting("STAGE", "fragment")
        self.block = int(setting("BLOCK", "0"))
        self.array = int(setting("ARRAY", "0"))
        self.duration = float(setting("SECONDS", "1.0"))
        self.started = time.monotonic()
        self.deadline = self.started + float(setting("TIMEOUT_SECONDS", "300"))
        self.phase = "loading"
        self.result = {"schema": "qrenderdoc-shared-material-smoke/v1", "ok": False,
                       "pid": os.getpid(), "event": self.event, "awayEvent": self.away,
                       "samples": {}, "publications": [], "inputCount": 0}
        self.keep = []
        self.busy = False
        self.done = False
        self.cleaning = False
        self.last_tick = None
        self.gaps = []
        self.original_event = None
        self.bridge = None
        self.panel = None
        self.viewer_widget = None
        self.timer = None
        self.progress_lock = threading.Lock()
        self.receipt_sequence = 0
        self.last_progress_time = self.started
        self.last_phase_receipt = None
        self.menu_deadline = None
        self.native_menus = []
        self.close_requested = False
        self.cleanup_deadline = None

    def receipt(self, step, **details):
        now = time.monotonic()
        with self.progress_lock:
            self.receipt_sequence += 1
            row = {"schema": "qrenderdoc-material-smoke-progress/v1", "pid": os.getpid(),
                   "sequence": self.receipt_sequence, "phase": self.phase, "step": step,
                   "elapsedMs": (now - self.started) * 1000,
                   "remainingMs": max(0, (self.deadline - now) * 1000), "busy": self.busy,
                   "cleaning": self.cleaning, "done": self.done, "threadId": threading.get_ident(),
                   "closeRequested": self.close_requested, "details": details}
            os.makedirs(os.path.dirname(self.output), exist_ok=True)
            with open(self.output + ".receipts.jsonl", "a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            temporary = self.output + ".progress.json.tmp"
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump(row, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, self.output + ".progress.json")
            self.last_progress_time = now

    def check_close_request(self):
        path = self.output + ".close-request.json"
        if self.close_requested or not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8-sig") as stream:
                request = json.load(stream)
        except (ValueError, OSError):
            return False  # The parent's atomic replacement may not have completed yet.
        if request.get("pid") != os.getpid():
            return False
        self.close_requested = True
        self.result["parentCloseRequest"] = {"pid": os.getpid(), "reason": str(request.get("reason", ""))}
        self.receipt("parent-close-request-accepted")
        self.close_native_menus()
        self.fail(RuntimeError("The parent requested graceful fixture shutdown"))
        return True

    def close_native_menus(self):
        for menu in self.native_menus:
            try:
                if menu.isVisible():
                    QtCore.QMetaObject.invokeMethod(menu, "close", QtCore.Qt.QueuedConnection)
            except RuntimeError:
                pass  # The C++ stack-owned menu has already been destroyed after aboutToHide.

    def start(self):
        try:
            self.receipt("start-enter")
            os.makedirs(os.path.dirname(self.output), exist_ok=True)
            self.timer = QtCore.QTimer()
            self.timer.setInterval(10)
            self.timer.setTimerType(QtCore.Qt.PreciseTimer)
            self.timer.timeout.connect(self.tick)
            self.keep.append(self.timer)
            QtWidgets.QApplication.instance().aboutToQuit.connect(
                lambda: self.receipt("application-about-to-quit"))
            self.timer.start()
            if not pyrenderdoc.IsCaptureLoaded():
                capture = setting("CAPTURE")
                if not capture:
                    raise ValueError("An explicit capture path is required")
                self.receipt("capture-load-enter", capture=capture)
                self.busy = True
                try:
                    pyrenderdoc.LoadCapture(capture, rd.ReplayOptions(), capture, False, True)
                finally:
                    self.busy = False
                self.receipt("capture-load-return")
        except Exception as exc:
            self.fail(exc)

    def replay(self, work, complete):
        self.busy = True
        self.receipt("replay-queued")
        miniqt = pyrenderdoc.Extensions().GetMiniQtHelper()

        def worker(ctrl):
            self.receipt("replay-worker-enter")
            value, error = None, None
            try:
                value = work(ctrl)
            except Exception as exc:
                error = exc
            self.receipt("replay-worker-return", error=str(error) if error else None)

            def publish():
                self.busy = False
                self.receipt("replay-ui-publication", error=str(error) if error else None)
                if error is not None:
                    self.fail(error)
                elif not self.done:
                    try:
                        complete(value)
                    except Exception as exc:
                        self.fail(exc)
            miniqt.InvokeOntoUIThread(publish)
        pyrenderdoc.Replay().AsyncInvoke(worker)

    def sample(self, name, event, next_phase):
        self.receipt("sample-select-enter", sample=name, event=event)
        pyrenderdoc.SetEventID([], event, event)
        self.receipt("sample-select-return", sample=name, event=event)

        def read(ctrl):
            buffer = next(item.resourceId for item in ctrl.GetBuffers()
                          if int(item.resourceId) == self.info["bufferResourceId"])
            raw = bytes(ctrl.GetBufferData(buffer, self.absolute, self.leaf["byteSize"]))
            return {"event": event, "fieldHex": raw.hex(), "pixels": output_pixels(ctrl),
                    "generation": int(ctrl.GetSharedBufferPatchGeneration())}

        def complete(value):
            self.result["samples"][name] = value
            self.phase = next_phase
        self.replay(read, complete)

    def tick(self):
        try:
            if self.done:
                return
            now = time.monotonic()
            if self.phase != self.last_phase_receipt:
                self.last_phase_receipt = self.phase
                self.receipt("phase-enter")
            elif now - self.last_progress_time >= 1.0:
                self.receipt("ui-timer-heartbeat")
            if self.check_close_request():
                return
            if self.cleaning and self.cleanup_deadline is not None and now > self.cleanup_deadline:
                self.result.setdefault("cleanupErrors", []).append("Rollback callback exceeded its 15 second grace period")
                self.finish()
                return
            if self.phase in ("continuous", "drain"):
                if self.last_tick is not None:
                    self.gaps.append((now - self.last_tick) * 1000)
                self.last_tick = now
            if now > self.deadline:
                raise RuntimeError("Material native fixture timed out in " + self.phase)
            if self.cleaning or self.busy:
                return
            if self.phase == "loading":
                if not pyrenderdoc.IsCaptureLoaded():
                    return
                self.bridge = sys.modules.get("renderdoc_mcp_bridge")
                if self.bridge is None:
                    raise RuntimeError("The installed canonical ReplayLab bridge is unavailable")
                if self.bridge._material_panel_controller.list_status():
                    raise RuntimeError("Use an isolated process with no existing material sessions")
                self.original_event = int(pyrenderdoc.CurEvent())
                self.receipt("catalog-select-enter")
                pyrenderdoc.SetEventID([], self.event, self.event)
                self.receipt("catalog-select-return")
                self.stage = dict(self.bridge._available_shader_stages([self.stage_name]))[self.stage_name]
                self.receipt("catalog-export-enter")
                catalog = self.bridge._export_shader_catalog({
                    "event_ids": [self.event], "stages": [self.stage_name],
                    "output_dir": os.path.splitext(self.output)[0] + "-catalog"})
                self.result["catalog"] = catalog
                self.receipt("catalog-export-return")
                self.replay(lambda ctrl: self.bridge._event_cbuffer_live_state(
                    ctrl, self.event, self.stage_name, self.stage, self.block,
                    shared=True, array_index=self.array), self.metadata)
            elif self.phase == "baseline-a":
                self.sample("baselineA", self.event, "baseline-b")
            elif self.phase == "baseline-b":
                self.sample("baselineB", self.away, "open-native-view")
            elif self.phase == "open-native-view":
                pyrenderdoc.SetEventID([], self.event, self.event)
                self.viewer = pyrenderdoc.ViewConstantBuffer(self.stage, self.block, self.array)
                self.keep.append(self.viewer)
                self.viewer_widget = self.viewer.Widget()
                self.keep.append(self.viewer_widget)
                pyrenderdoc.AddDockWindow(self.viewer_widget, qrd.DockReference.TransientPopupArea,
                                         pyrenderdoc.GetPipelineViewer().Widget(), 0.3)
                self.phase = "native-tree"
            elif self.phase == "native-tree":
                self.open_native_menu()
            elif self.phase == "native-menu":
                self.trigger_native_action()
            elif self.phase == "native-action":
                dialogs = [widget for widget in QtWidgets.QApplication.topLevelWidgets()
                           if isinstance(widget, QtWidgets.QMessageBox) and widget.isVisible()]
                if dialogs:
                    self.result["nativeActionDialogs"] = [{"title": dialog.windowTitle(), "text": dialog.text()}
                                                           for dialog in dialogs]
                    for dialog in dialogs:
                        QtCore.QMetaObject.invokeMethod(dialog, "reject", QtCore.Qt.QueuedConnection)
                    raise RuntimeError("The native Material QAction opened an error dialog")
                if self.bridge._material_panel_controller.list_status():
                    self.receipt("native-action-publication-observed")
                    self.close_native_menus()
                    self.phase = "exposed"
                elif now > self.menu_deadline:
                    raise RuntimeError("The native Material QAction did not publish a session within 15 seconds")
            elif self.phase == "exposed":
                statuses = self.bridge._material_panel_controller.list_status()
                if not statuses:
                    return
                if len(statuses) != 1 or len(statuses[0]["parameters"]) != 1:
                    raise RuntimeError("Native menu exposed an unexpected material field")
                self.panel = self.bridge._material_panel_ui
                self.status = statuses[0]
                self.sid = self.status["sessionId"]
                self.pid = self.status["parameters"][0]["parameterId"]
                self.editor = self.panel._editor_widgets[(self.sid, self.pid)]
                self.keep.append(self.editor)
                param = self.status["parameters"][0]
                if (param["absoluteByteOffset"] != self.absolute or
                        param["byteSize"] != self.leaf["byteSize"]):
                    raise RuntimeError("Native selection exposed a different physical range")
                self.result["nativeSelection"] = param
                pyrenderdoc.SetEventID([], self.away, self.away)
                self.input_text(self.new_text)
                self.edit_deadline = now + 15
                self.phase = "first-edit"
            elif self.phase == "first-edit":
                if self.panel.controller.edit_queue.busy:
                    return
                if self.panel.controller.status(self.sid)["revision"] != 1:
                    if now < self.edit_deadline:
                        return
                    raise RuntimeError("The native textEdited callback did not commit its first edit")
                if not self.panel.controller.status(self.sid)["previewPublished"]:
                    raise RuntimeError("The first Material preview publication was not confirmed")
                self.sample("editedB", self.away, "edited-a")
            elif self.phase == "edited-a":
                self.sample("editedA", self.event, "start-continuous")
            elif self.phase == "start-continuous":
                pyrenderdoc.SetEventID([], self.away, self.away)
                self.continuous_start = now
                self.start_revision = self.panel.controller.status(self.sid)["revision"]
                self.last_revision = self.start_revision
                self.phase = "continuous"
                self.last_tick = now
            elif self.phase == "continuous":
                status = self.panel.controller.status(self.sid)
                if status["revision"] != self.last_revision:
                    self.result["publications"].append({"elapsedMs": (now - self.continuous_start) * 1000,
                        "revision": status["revision"], "generation": status["generation"],
                        "previewPublished": status["previewPublished"],
                        "replayedEventId": status["replayedEventId"],
                        "currentEvent": int(pyrenderdoc.CurEvent())})
                    self.last_revision = status["revision"]
                if now - self.continuous_start >= self.duration:
                    self.input_text(self.new_text)
                    self.phase = "drain"
                else:
                    text = self.old_text if self.result["inputCount"] % 2 == 0 else self.new_text
                    self.input_text(text)
                    self.result["inputCount"] += 1
            elif self.phase == "drain":
                if not self.panel.controller.edit_queue.busy:
                    idle_seconds = float(setting("IDLE_SECONDS") or "0")
                    if idle_seconds:
                        if not 1.0 <= idle_seconds <= 5.0:
                            raise RuntimeError("Idle acceptance delay must be between one and five seconds")
                        self.idle_seconds = idle_seconds
                        self.phase = "idle-before"
                    else:
                        self.sample("finalB", self.away, "clear")
            elif self.phase == "idle-before":
                def before_idle(value):
                    self.result["idleBefore"] = value
                    self.idle_started = time.perf_counter()
                    self.phase = "idle-wait"
                self.replay(self.idle_witness, before_idle)
            elif self.phase == "idle-wait":
                if time.perf_counter() - self.idle_started >= self.idle_seconds:
                    def after_idle(value):
                        before = self.result["idleBefore"]
                        self.result["idleAfter"] = value
                        self.result["idleWaitSeconds"] = time.perf_counter() - self.idle_started
                        for key in ("event", "generation", "fieldHex", "pixels"):
                            if before[key] != value[key]:
                                raise RuntimeError("Background maintenance changed the current " + key)
                        if value["checkpoints"]["builds"] <= before["checkpoints"]["builds"]:
                            raise RuntimeError("The native UI idle scheduler did not build a missing checkpoint")
                        self.phase = "idle-verified"
                    self.replay(self.idle_witness, after_idle)
            elif self.phase == "idle-verified":
                self.sample("finalB", self.away, "clear")
            elif self.phase == "clear":
                self.panel.controller.submit_rollback(self.sid, self.cleared, self)
                self.phase = "clearing"
            elif self.phase == "restored-a":
                self.sample("restoredA", self.event, "restored-b")
            elif self.phase == "restored-b":
                self.sample("restoredB", self.away, "verify")
            elif self.phase == "verify":
                self.verify()
                self.finish()
        except Exception as exc:
            self.fail(exc)

    def idle_witness(self, ctrl):
        # This read-only callback does not select an event or invoke maintenance. Only the real
        # native UI scheduler may build a checkpoint while the user input is stopped.
        if int(ctrl.GetCurrentEventID()) != self.away:
            raise RuntimeError("Idle witness observed a different current event")
        buffer = next(item.resourceId for item in ctrl.GetBuffers()
                      if int(item.resourceId) == self.info["bufferResourceId"])
        return {"event": int(ctrl.GetCurrentEventID()),
                "generation": int(ctrl.GetSharedBufferPatchGeneration()),
                "fieldHex": bytes(ctrl.GetBufferData(buffer, self.absolute, self.leaf["byteSize"])).hex(),
                "pixels": output_pixels(ctrl),
                "checkpoints": json.loads(str(ctrl.GetReplayCheckpointStatus()))}

    def input_text(self, text):
        # Single-line MiniQt editors connect QLineEdit::textEdited, which intentionally
        # excludes programmatic setText. A native input-method commit models one user
        # value edit, reaches that exact signal and avoids invoking Python callbacks by hand.
        if not QtCore.QMetaObject.invokeMethod(self.editor, "selectAll", QtCore.Qt.DirectConnection):
            raise RuntimeError("The native Material editor has no selectAll slot")
        event = QtGui.QInputMethodEvent()
        event.setCommitString(text)
        QtWidgets.QApplication.postEvent(self.editor, event)

    def metadata(self, info):
        wanted = setting("FIELD")
        fields = [leaf for leaf in info["fields"] if leaf["baseType"] == "Float"
                  and (not wanted or leaf["path"] == wanted)]
        if not fields:
            raise RuntimeError("No evidenced float scalar/vector field matches the requested selection")
        self.info, self.leaf = info, fields[0]
        self.absolute = int(info["byteOffset"]) + self.leaf["byteOffset"]
        offset, count = self.leaf["byteOffset"], self.leaf["components"]
        self.old_values = list(struct.unpack("<%df" % count,
            info["rawBytes"][offset:offset + self.leaf["byteSize"]]))
        if not all(math.isfinite(value) for value in self.old_values):
            raise RuntimeError("The selected baseline field is not finite")
        values = json.loads(setting("VALUE")) if setting("VALUE") else list(self.old_values)
        if not isinstance(values, list):
            values = [values]
        if not setting("VALUE"):
            values[0] = self.old_values[0] * 0.5 if self.old_values[0] else 0.5
        self.new_bytes = self.bridge._material_panel_protocol._pack_values(self.leaf, values)
        self.new_text = ", ".join("%.9g" % value for value in values)
        self.old_text = ", ".join("%.9g" % value for value in self.old_values)
        self.result["metadata"] = {key: info[key] for key in
            ("api", "bufferResourceId", "byteOffset", "byteSize", "entryPoint", "slot", "registerSpace")}
        self.result["metadata"].update({"field": self.leaf, "absoluteOffset": self.absolute})
        self.phase = "baseline-a"

    def open_native_menu(self):
        children = self.viewer_widget.findChildren(QtWidgets.QTreeView)
        self.keep.extend(children)
        trees = [item for item in children
                 if item.objectName() == "fixedVars"]
        self.keep.extend(trees)
        if not trees:
            return
        tree = trees[0]
        model = tree.model()
        self.keep.append(model)
        if model is None or model.rowCount() == 0:
            return
        wanted = self.leaf["path"]
        seen = []

        def visit(parent, prefix=""):
            for row in range(model.rowCount(parent)):
                index = model.index(row, 0, parent)
                name = str(model.data(index, QtCore.Qt.DisplayRole) or "")
                path = prefix + "." + name if prefix else name
                seen.append(path)
                if path == wanted or name == wanted:
                    return index
                child = visit(index, path)
                if child is not None:
                    return child
            return None

        selected = visit(QtCore.QModelIndex())
        if selected is None:
            self.result["nativeTreeRows"] = seen[:100]
            return
        tree.expandAll()
        tree.scrollTo(selected)
        tree.setCurrentIndex(selected)
        point = tree.visualRect(selected).center()
        self.result["nativeMenuRoute"] = {"path": wanted, "row": selected.row(),
            "rect": [tree.visualRect(selected).x(), tree.visualRect(selected).y(),
                     tree.visualRect(selected).width(), tree.visualRect(selected).height()],
            "mechanism": "posted native QContextMenuEvent -> C++ reflected selection -> queued real QAction -> registered callback"}
        self.phase = "native-menu"
        self.menu_deadline = time.monotonic() + 15
        self.receipt("native-context-event-post", route=self.result["nativeMenuRoute"])
        # BufferViewer's native slot enters RDDialog::show's nested blocking event loop. A direct
        # Python signal.emit would keep this timer callback/PySide call on that stack, preventing
        # this same timer from supervising the modal route. Posting returns before native dispatch.
        event = QtGui.QContextMenuEvent(QtGui.QContextMenuEvent.Mouse, point,
                                        tree.viewport().mapToGlobal(point))
        QtCore.QCoreApplication.postEvent(tree.viewport(), event)

    def trigger_native_action(self):
        expected = QtCore.QCoreApplication.translate("ReplayLabProjects", "Expose as Material Parameter")
        menus = [widget for widget in QtWidgets.QApplication.topLevelWidgets()
                 if isinstance(widget, QtWidgets.QMenu) and widget.isVisible()]
        if not menus:
            if time.monotonic() > self.menu_deadline:
                raise RuntimeError("The posted native context event did not open a menu within 15 seconds")
            return
        self.native_menus = menus
        self.keep.extend(menus)
        actions_seen = []

        def find(menu):
            self.keep.append(menu)
            for action in menu.actions():
                self.keep.append(action)
                actions_seen.append({"text": action.text(), "enabled": action.isEnabled()})
                if action.text().replace("&", "") == expected and action.isEnabled():
                    return action
                if action.menu() is not None:
                    result = find(action.menu())
                    if result is not None:
                        return result
            return None
        action = next((found for found in (find(menu) for menu in menus) if found is not None), None)
        self.result["nativeMenuActions"] = actions_seen[:100]
        if action is None:
            self.close_native_menus()
            raise RuntimeError("The native reflected field menu did not expose an enabled Material action")
        self.receipt("native-action-queue", text=action.text(), enabled=action.isEnabled())
        # Queue the actual QAction slot so C++ -> Python extension -> replay BlockInvoke executes
        # without an outer PySide action.trigger call holding the originating Python stack/GIL.
        if not QtCore.QMetaObject.invokeMethod(action, "trigger", QtCore.Qt.QueuedConnection):
            raise RuntimeError("Qt refused to queue the actual native Material QAction")
        self.phase = "native-action"
        self.menu_deadline = time.monotonic() + 15

    def cleared(self, result, error):
        if error is not None:
            self.fail(error)
        elif result is None or not result.get("snapshotReleased"):
            self.fail(RuntimeError("The shared field clear did not release its snapshot"))
        else:
            self.phase = "restored-a"

    def verify(self):
        samples = self.result["samples"]
        for suffix in ("A", "B"):
            baseline, edited, restored = [samples[prefix + suffix] for prefix in ("baseline", "edited", "restored")]
            if edited["fieldHex"] != self.new_bytes.hex():
                raise RuntimeError("Shared bytes were not persistent at event " + suffix)
            if restored["fieldHex"] != baseline["fieldHex"] or restored["pixels"] != baseline["pixels"]:
                raise RuntimeError("Clearing the field did not restore all bound outputs at event " + suffix)
            if edited["pixels"] == baseline["pixels"]:
                raise RuntimeError("Inconclusive: the chosen field did not visibly affect event " + suffix)
        if samples["finalB"]["fieldHex"] != self.new_bytes.hex():
            raise RuntimeError("Continuous input did not apply its final value")
        generations = sorted(set(item["generation"] for item in self.result["publications"]))
        self.result["distinctPublishedGenerations"] = generations
        if len(generations) < 2:
            raise RuntimeError("Continuous input did not publish multiple changed native versions before stopping")
        if any(not item["previewPublished"] or item["replayedEventId"] != self.away
               for item in self.result["publications"]):
            raise RuntimeError("A continuous edit did not finish its matching pipeline/viewer publication")
        if max(self.gaps or [0]) > float(setting("MAX_HEARTBEAT_MS", "250")):
            raise RuntimeError("The Material GUI heartbeat stalled during continuous input")

    def fail(self, error):
        if self.done or self.cleaning:
            return
        self.result["error"] = str(error)
        self.result["traceback"] = traceback.format_exc()
        self.cleaning = True
        self.cleanup_deadline = time.monotonic() + 15
        self.receipt("failure", error=str(error))
        self.close_native_menus()
        if self.panel is not None:
            self.panel.controller.edit_queue.cancel_owner(self.panel)
        model = self.bridge._material_panel_controller if self.bridge else None
        statuses = model.list_status() if model else []
        if statuses:
            remaining = [len(statuses)]
            def cleared(result, clear_error):
                if clear_error is not None:
                    self.result.setdefault("cleanupErrors", []).append(str(clear_error))
                remaining[0] -= 1
                if not remaining[0]:
                    self.finish()
            for status in statuses:
                model.submit_rollback(status["sessionId"], cleared, self)
        else:
            self.finish()

    def finish(self):
        if self.done:
            return
        self.done = True
        self.receipt("finish-enter")
        self.close_native_menus()
        # Persist the outcome before any normal native close/drain could block. The progress
        # receipt then identifies precisely which cleanup call has not returned.
        self.result.update({"ok": "error" not in self.result and not self.result.get("cleanupErrors"),
                            "finalPhase": self.phase, "cleanupComplete": False})
        with open(self.output, "w", encoding="utf-8") as stream:
            json.dump(self.result, stream, ensure_ascii=False, indent=2)
        try:
            if self.timer is not None:
                self.timer.stop()
                self.timer.timeout.disconnect(self.tick)
                self.timer.deleteLater()
            if self.viewer_widget is not None:
                self.receipt("viewer-close-queued")
                QtCore.QMetaObject.invokeMethod(self.viewer_widget, "close", QtCore.Qt.QueuedConnection)
            if self.original_event is not None and pyrenderdoc.IsCaptureLoaded():
                self.receipt("restore-original-event-enter", event=self.original_event)
                pyrenderdoc.SetEventID([], self.original_event, self.original_event)
                self.receipt("restore-original-event-return")
        except Exception as exc:
            self.result.setdefault("cleanupErrors", []).append(str(exc))
        self.result.update({"ok": "error" not in self.result and not self.result.get("cleanupErrors"), "finalPhase": self.phase,
            "cleanupComplete": not bool(self.result.get("cleanupErrors")),
            "maxHeartbeatMs": max(self.gaps or [0]), "heartbeatSamples": len(self.gaps),
            "presentationEvidence": "SetSharedBufferPatch completed, GUI pipeline published and viewer replay queue drained; not scanout"})
        with open(self.output, "w", encoding="utf-8") as stream:
            json.dump(self.result, stream, ensure_ascii=False, indent=2)
        print("REPLAYLAB_MATERIAL_SMOKE=" + json.dumps(self.result, sort_keys=True))
        self.receipt("result-published", ok=self.result["ok"])
        if setting("QUIT") == "1" or self.close_requested:
            self.receipt("application-close-queued")
            # Let MainWindow's normal close path drain replay and release its capture. An immediate
            # QApplication.exit after closeAllWindows could abandon that shutdown sequence.
            QtCore.QMetaObject.invokeMethod(QtWidgets.QApplication.instance(), "closeAllWindows",
                                            QtCore.Qt.QueuedConnection)


probe = MaterialSharedSmoke()
probe.receipt("script-loaded")
QtWidgets.QApplication.instance()._material_shared_smoke = probe
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(probe.start)
