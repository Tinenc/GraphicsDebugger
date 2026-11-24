"""Actual Workspace persistent-debug acceptance in a private --ui-script process.

BREAD_SHADER_VISUAL_DEBUG_WORKSPACE_CONFIG names capture/sourceFile/output JSON.
Use only copied fixture captures and sources in an isolated profile. The source
is never saved or edited. Optional eventId/earlierEventId/nonDrawEventId/laterEventId
select events; otherwise use the Shared_Buffer fixture's last four draws.
Observers delegate every real compile, submit and native editor marker call.
They do not fake operations, native state, Qt callbacks, or Manager responses.
Embedded Python 3.6 compatible. A separate launcher owns the process deadline.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback


class MarkerObserver(object):
    """Record calls to the real editor; no public native marker getter exists."""
    def __init__(self, editor):
        self.editor = editor
        self.calls = []
        self.active_range = None

    def __getattr__(self, name):
        return getattr(self.editor, name)

    def SetVisualDebugRange(self, start, end, active):
        result = self.editor.SetVisualDebugRange(start, end, active)
        self.calls.append([int(start), int(end), bool(active)])
        self.active_range = [int(start), int(end)] if active else None
        return result


def run(context):
    import renderdoc as rd
    from PySide2 import QtCore, QtWidgets

    config = json.loads(Path(os.environ[
        "BREAD_SHADER_VISUAL_DEBUG_WORKSPACE_CONFIG"]).read_text(encoding="utf-8-sig"))
    output = Path(config["output"]).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    screenshots = output.parent / (output.stem + "-screenshots")
    screenshots.mkdir(exist_ok=True)
    report = {"schema": "bread-shader-persistent-workspace-smoke/v1", "ok": False,
              "pid": os.getpid(), "checks": [], "screenshots": [], "operations": []}
    app = QtWidgets.QApplication.instance()
    started = time.monotonic()
    panel = controller = document = editor = observer = manager = None
    original_compile = original_submit = None
    owned_capture = False
    counts = {"compile": 0}

    def save():
        report["elapsedSeconds"] = round(time.monotonic() - started, 3)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def check(name, condition, details=None):
        report["checks"].append(dict(name=name, passed=bool(condition), details=details))
        save()
        if not condition:
            raise AssertionError(name + ": " + str(details))

    def state():
        if controller is None:
            return {}
        return dict(active=controller.active, busy=controller.busy,
                    sessionId=controller.session_id, label=controller.label.text(),
                    selectedEventId=int(context.CurEvent()))

    def pump(seconds=0.5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents(QtCore.QEventLoop.AllEvents, 20)
            time.sleep(0.005)

    def wait_for(name, predicate):
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            app.processEvents(QtCore.QEventLoop.AllEvents, 20)
            if predicate():
                check(name, True, state())
                return
            if (name == "debugActiveFromNondraw" and controller is not None and
                    controller.request and not controller.busy and not controller.active):
                check(name, False, state())
            time.sleep(0.005)
        check(name, False, state())

    def replay_call(callback):
        result = {}
        def invoke(replay):
            try:
                result["value"] = callback(replay)
            except BaseException:
                result["error"] = traceback.format_exc()
        context.Replay().BlockInvoke(invoke)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result.get("value")

    def screenshot(name):
        path = screenshots / (name + ".png")
        check("screenshot:" + name, panel.top.grab().save(str(path), "PNG"))
        report["screenshots"].append(str(path))

    def unchanged(name):
        check(name, source.read_bytes() == source_bytes and capture.read_bytes() == capture_bytes and
              str(editor.GetText()) == original_text and bool(editor.IsModified()) == modified and
              bool(document.get("dirty")) == dirty)

    try:
        check("freshOwnedContext", not context.IsCaptureLoaded())
        bridges = []
        def registered():
            bridges[:] = [module for module in list(sys.modules.values()) if module is not None and
                          str(getattr(module, "__file__", "")).replace("\\", "/").endswith(
                              "/renderdoc_mcp_bridge/__init__.py") and
                          getattr(module, "_shader_workspace_ui", None) is not None]
            return len(bridges) == 1
        wait_for("registeredBridge", registered)
        bridge = bridges[0]
        panel = bridge._shader_workspace_ui
        controller = panel.visual_debug
        check("registeredProductController", controller.operation is bridge._shader_visual_debug_operation,
              dict(bridgeFile=bridge.__file__, controllerModule=type(controller).__module__))
        capture, source = Path(config["capture"]).resolve(), Path(config["sourceFile"]).resolve()
        capture_bytes, source_bytes = capture.read_bytes(), source.read_bytes()
        context.LoadCapture(str(capture), rd.ReplayOptions(), str(capture), False, True)
        owned_capture = bool(context.IsCaptureLoaded())
        check("fixtureCaptureLoaded", owned_capture)
        pending, actions = list(context.CurRootActions()), []
        while pending:
            action = pending.pop(0)
            pending.extend(action.children)
            actions.append(action)
        draws = sorted(int(a.eventId) for a in actions if not a.children and
                       a.flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.MeshDispatch))
        check("multipleRealDraws", len(draws) >= 3, draws)
        anchor = int(config.get("eventId", draws[-2]))
        earlier = int(config.get("earlierEventId", draws[0]))
        nondraw = int(config.get("nonDrawEventId", anchor - 1))
        later = int(config.get("laterEventId", draws[-1]))
        check("navigationEventsValid", 0 < earlier < anchor < later and nondraw > 0 and nondraw not in draws,
              dict(anchor=anchor, earlier=earlier, nondraw=nondraw, later=later))
        report["events"] = dict(anchor=anchor, earlier=earlier, nondraw=nondraw, later=later)
        context.SetEventID([], anchor, anchor, True)
        original_shader = replay_call(lambda replay: replay.GetPipelineState().GetShader(rd.ShaderStage.Pixel))
        prior = replay_call(lambda replay: replay.GetTargetShaderReplacement(original_shader))
        panel.show()
        controller.capture_current(source_file=str(source))
        wait_for("ownedSourceOpened", lambda: panel._current_document() is not None and not controller.busy)
        document = panel._current_document()
        editor = document["editor"]
        original_text, modified, dirty = str(editor.GetText()), bool(editor.IsModified()), bool(document.get("dirty"))
        check("ownedSourceMatchesDisk", original_text.replace("\r\n", "\n") ==
              source_bytes.decode("utf-8-sig").replace("\r\n", "\n"))
        raw = original_text.encode("utf-8")
        expression = str(config.get("expression", "probe"))
        start = int(config.get("byteStart", raw.rfind(expression.encode("utf-8"))))
        end = start + len(expression.encode("utf-8"))
        check("expressionExists", raw[start:end] == expression.encode("utf-8") and start >= 0)
        line = raw[:start].count(b"\n") + 1
        column = start - raw.rfind(b"\n", 0, start)
        observer = MarkerObserver(editor)
        document["editor"] = observer
        manager = bridge._shader_visual_debug_manager
        check("realManagerAvailable", manager is not None and manager._session is None)
        original_compile = manager.driver.compile
        def observed_compile(*args, **kwargs):
            counts["compile"] += 1
            return original_compile(*args, **kwargs)
        manager.driver.compile = observed_compile
        original_submit = controller.submit
        def observed_submit(operation, request):
            report["operations"].append(dict(operation=operation, eventId=request.get("eventId"),
                                              entryPoint=request.get("entryPoint")))
            save()
            return original_submit(operation, request)
        controller.submit = observed_submit

        # First debugging begins while a non-draw EID is selected. The document
        # retains its original Shader binding and no UI navigation is permitted.
        context.SetEventID([], nondraw, nondraw, True)
        pump()
        editor.GoToLine(line, column)
        screenshot("before-debug-at-nondraw")
        controller.start_button.click()
        wait_for("debugActiveFromNondraw", lambda: controller.active and not controller.busy)
        selected_replay = replay_call(lambda replay: int(replay.GetCurrentEventID()))
        check("beginPreservesSelectedEvent", int(context.CurEvent()) == nondraw and selected_replay == nondraw)
        session = manager._session
        session_id = controller.session_id
        selection = dict(controller.selection)
        receipt = manager._receipt(session)
        report["initialReceipt"] = receipt
        check("receiptDeclaresActualPersistentScope", receipt.get("scope") == "shader-resource" and
              receipt.get("resourceScoped") is True and receipt.get("persistentAcrossEvents") is True and
              receipt.get("eventScoped") is False and not receipt.get("independentFloatOutput"))
        target = replay_call(lambda replay: replay.GetTargetShaderReplacement(original_shader))
        check("nativeGlobalReplacementMatchesManager", target == session["cached"]["target"] and target != prior,
              dict(original=str(original_shader), target=str(target), prior=str(prior)))
        check("oneCompileAndOriginalDocumentMarked", counts["compile"] == 1 and
              observer.active_range == [selection["byteStart"], selection["byteEnd"]] and
              controller.request["workspaceId"] == document["workspaceId"])
        check("actualRTOptionEnforced", not controller.float_preview.isEnabled() and
              not controller.float_preview.isChecked())
        screenshot("active-at-nondraw")
        compile_count, operation_count, marker_count = counts["compile"], len(report["operations"]), len(observer.calls)
        generation = controller.state.generation
        for name, event in (("earlier", earlier), ("nondraw", nondraw), ("later", later)):
            context.SetEventID([], event, event, True)
            # More than one UI polling interval: a legacy follow worker would
            # be observable even if a fast cache made its result look unchanged.
            pump(0.8)
            native = replay_call(lambda replay: (int(replay.GetCurrentEventID()),
                                                  replay.GetTargetShaderReplacement(original_shader)))
            check(name + ":persistentSessionAndTarget", controller.active and not controller.busy and
                  controller.session_id == session_id and manager._session is session and
                  native[0] == event and native[1] == target and int(context.CurEvent()) == event)
            check(name + ":noCompileFollowOrMarkerRebind", counts["compile"] == compile_count and
                  len(report["operations"]) == operation_count and len(observer.calls) == marker_count and
                  controller.state.generation == generation and controller.selection == selection and
                  observer.active_range == [selection["byteStart"], selection["byteEnd"]] and
                  controller.request["workspaceId"] == document["workspaceId"])
            unchanged(name + ":sourceAndCaptureUnchanged")
            screenshot("active-" + name)
        controller.stop_button.click()
        wait_for("cancelCompletes", lambda: not controller.active and not controller.busy and
                 not controller.session_id and not controller._stopping)
        check("cancelRestoresPriorNativeStateAtSelectedEvent", manager._session is None and
              replay_call(lambda replay: replay.GetTargetShaderReplacement(original_shader)) == prior and
              int(context.CurEvent()) == later and observer.active_range is None)
        unchanged("finalSourceCaptureAndDirtyStateUnchanged")
        screenshot("restored-at-later")
        report.update(ok=True, compileCount=counts["compile"], markerCalls=observer.calls,
                      markerEvidence="delegated-native-marker-calls-and-editor-screenshots",
                      sourceSha256=hashlib.sha256(source_bytes).hexdigest())
    except BaseException:
        report.update(error=traceback.format_exc(), lastControllerState=state())
    finally:
        cleanup_errors = []
        try:
            if controller is not None and (controller.request or controller.session_id or controller.busy):
                if not controller._stopping:
                    controller.stop()
                wait_for("cleanupDebugStopped", lambda: not controller.active and not controller.busy and
                         not controller.session_id and not controller._stopping)
            if original_submit is not None:
                controller.submit = original_submit
            if original_compile is not None:
                manager.driver.compile = original_compile
            if document is not None and editor is not None:
                document["editor"] = editor
            if owned_capture and context.IsCaptureLoaded():
                context.CloseCapture()
        except BaseException:
            cleanup_errors.append(traceback.format_exc())
        if cleanup_errors:
            report.update(ok=False, cleanupErrors=cleanup_errors)
        save()
        context.GetMainWindow().Widget().close()
        QtCore.QCoreApplication.exit(0 if report["ok"] else 1)


if "pyrenderdoc" in globals():
    pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(lambda: run(pyrenderdoc))
