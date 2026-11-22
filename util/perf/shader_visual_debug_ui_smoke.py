"""Native editor and texture-follow smoke test in an owned --ui-script process.

Set BREAD_SHADER_VISUAL_DEBUG_UI_CONFIG to JSON with capture, output and optional
eventId. Use an isolated analytics-opted-out profile and a launcher deadline.
This fixture sends in-process Qt events only; it never controls another window.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import traceback
from types import SimpleNamespace


def run(context):
    import renderdoc as rd
    import qrenderdoc as qrd
    from PySide2 import QtCore, QtGui, QtWidgets
    config = json.loads(Path(os.environ["BREAD_SHADER_VISUAL_DEBUG_UI_CONFIG"]).read_text(encoding="utf-8-sig"))
    report = {"schema": "bread-shader-visual-debug-ui-smoke/v1", "ok": False,
              "pid": os.getpid(), "checks": []}
    app = QtWidgets.QApplication.instance()
    editor = host = None
    viewer, preview_target, preview_token = None, None, 0
    event = 0

    def check(name, value, details=None):
        report["checks"].append({"name": name, "passed": bool(value), "details": details})
        if not value:
            raise AssertionError(name + ": " + str(details))

    def image_hash(widget):
        image = widget.grab().toImage().convertToFormat(QtGui.QImage.Format_RGBA8888)
        raw = image.constBits()
        if hasattr(raw, "setsize"):
            raw.setsize(image.byteCount())
        return hashlib.sha256(bytes(raw)).hexdigest()

    def replay_call(callback):
        result = {}
        def execute(controller):
            try:
                result["value"] = callback(controller)
            except BaseException:
                result["error"] = traceback.format_exc()
        context.Replay().BlockInvoke(execute)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result.get("value")

    try:
        check("freshOwnedContext", not context.IsCaptureLoaded())
        if not config.get("editorOnly", False):
            capture = str(Path(config["capture"]).resolve())
            context.LoadCapture(capture, rd.ReplayOptions(), capture, False, True)
            check("captureLoaded", context.IsCaptureLoaded())
            event = int(config.get("eventId", 0))
            if not event:
                pending, draws = list(context.CurRootActions()), []
                while pending:
                    action = pending.pop(0)
                    pending.extend(action.children)
                    if action.flags & rd.ActionFlags.Drawcall:
                        draws.append(int(action.eventId))
                event = draws[-1]
            context.SetEventID([], event, event, True)
            viewer = context.GetTextureViewer()
            viewer_widget = viewer.Widget()
            targets = context.CurPipelineState().GetOutputTargets()
            slots = [index for index, target in enumerate(targets) if int(target.resource) != 0]
            check("colorTargetAvailable", bool(slots))
            slot = slots[0]
            resource = targets[slot].resource
            viewer.ViewTexture(resource, rd.CompType.Float, False)
            app.processEvents()
            container = viewer_widget.findChild(QtWidgets.QWidget, "renderContainer")
            tab_widgets = viewer_widget.findChildren(QtWidgets.QTabWidget)
            tabs = next(tabs for tabs in tab_widgets
                        if tabs.indexOf(container) >= 0)
            prior_tab = tabs.currentWidget()
            prior_sub = viewer.GetSelectedSubresource()
            check("lockedTabIsBaseline", prior_tab is not container)
            token = viewer.BeginTemporaryOutputFollow(slot)
            check("previewSelectsFollowingTab", token != 0 and tabs.currentWidget() is container)
            check("previewShowsChosenRT", viewer.GetCurrentResource() == resource)
            check("ownedRTUpdate", viewer.UpdateTemporaryOutputFollow(token, slots[-1]))
            check("ownedViewRestore", viewer.EndTemporaryOutputFollow(token))
            restored = viewer.GetSelectedSubresource()
            check("priorTabRestored", tabs.currentWidget() is prior_tab)
            check("priorResourceRestored", viewer.GetCurrentResource() == resource)
            check("priorSubresourceRestored", (restored.mip, restored.slice, restored.sample) ==
                  (prior_sub.mip, prior_sub.slice, prior_sub.sample))
            check("oldTokenCannotRestoreTwice", not viewer.EndTemporaryOutputFollow(token))
            token = viewer.BeginTemporaryOutputFollow(slot)
            viewer.ViewTexture(resource, rd.CompType.Float, False)
            manual_tab = tabs.currentWidget()
            check("manualNavigationInvalidatesUpdate", not viewer.UpdateTemporaryOutputFollow(token, slot))
            check("manualNavigationInvalidatesRestore", not viewer.EndTemporaryOutputFollow(token))
            check("manualNavigationPreserved", tabs.currentWidget() is manual_tab and manual_tab is prior_tab)

            # Build a real temporary FP32 target through the same native API
            # consumed by ReplayDriver. Keep it alive until its view detaches.
            flags = rd.ShaderCompileFlags()
            flag = rd.ShaderCompileFlag()
            flag.name, flag.value = "@cmdline", "/T ps_5_0"
            flags.flags = [flag]
            preview_source = ("float4 main() : SV_Target%d { return float4(-0.5, 2.0, 0.25, 1.0); }" % slot).encode("utf-8")
            preview_target, diagnostics = replay_call(lambda ctrl: ctrl.BuildTargetShader(
                "main", rd.ShaderEncoding.HLSL, preview_source, flags, rd.ShaderStage.Pixel))
            check("floatPreviewShaderCompiled", int(preview_target) != 0, str(diagnostics))
            original_shader = context.CurPipelineState().GetShader(rd.ShaderStage.Pixel)
            def install_float_preview(ctrl):
                error = ctrl.SetEventShaderOverride(event, rd.ShaderStage.Pixel,
                    original_shader, preview_target, slot, True)
                if error:
                    raise RuntimeError(str(error))
                return ctrl.GetEventShaderPreviewTexture(event, rd.ShaderStage.Pixel)
            preview = replay_call(install_float_preview)
            check("floatPreviewDescriptorIsRGBA32F", int(preview.resourceId) != 0 and
                preview.format.compCount == 4 and preview.format.compByteWidth == 4 and
                preview.format.compType == rd.CompType.Float,
                {"resource": str(preview.resourceId), "format": preview.format.Name(),
                 "width": int(preview.width), "height": int(preview.height)})
            pixels = replay_call(lambda ctrl: bytes(ctrl.GetTextureData(preview.resourceId, rd.Subresource())))
            check("floatPreviewPreservesNegativeAndHDR", struct.pack("<4f", -0.5, 2.0, 0.25, 1.0) in pixels)
            check("temporaryDescriptorIsNotCaptureTexture", context.GetTexture(preview.resourceId) is None)
            preview_token = viewer.BeginTemporaryOutputFollow(slot)
            check("temporaryDescriptorAccepted", viewer.ShowTemporaryTexturePreview(preview_token, preview))
            check("floatPreviewResourceVisible", viewer.GetCurrentResource() == preview.resourceId)
            selected = viewer.GetSelectedSubresource()
            check("floatPreviewUsesNormalizedSubresource", (selected.mip, selected.slice, selected.sample) == (0, 0, 0))
            # Drop the Python wrapper; TextureViewer must retain its own native
            # descriptor value across a normal event/view refresh.
            preview_id = str(preview.resourceId)
            del preview
            context.RefreshStatus()
            check("descriptorCopiedAcrossRefresh", str(viewer.GetCurrentResource()) == preview_id)
            replay_call(lambda ctrl: ctrl.ClearEventShaderOverride(event, rd.ShaderStage.Pixel))
            check("compareCanSelectCapturedOutput", viewer.UpdateTemporaryOutputFollow(preview_token, slot))
            check("compareShowsCapturedOutput", viewer.GetCurrentResource() == resource)
            preview = replay_call(install_float_preview)
            check("compareReleaseCanShowFloatPreview", viewer.ShowTemporaryTexturePreview(preview_token, preview))
            check("compareReleaseShowsFloatResource", viewer.GetCurrentResource() == preview.resourceId)
            check("floatPreviewRestoresOwnedView", viewer.EndTemporaryOutputFollow(preview_token))
            preview_token = 0
            check("floatPreviewPriorTabAndResourceRestored", tabs.currentWidget() is prior_tab and viewer.GetCurrentResource() == resource)
            preview_token = viewer.BeginTemporaryOutputFollow(slot)
            check("floatPreviewCanReopen", viewer.ShowTemporaryTexturePreview(preview_token, preview))
            viewer.ViewTexture(resource, rd.CompType.Float, False)
            check("manualNavigationDetachesFloatDescriptor", viewer.GetCurrentResource() == resource and
                  not viewer.EndTemporaryOutputFollow(preview_token))
            preview_token = viewer.BeginTemporaryOutputFollow(slot)
            check("floatPreviewCanReopenAfterManualNavigation", viewer.ShowTemporaryTexturePreview(preview_token, preview))
            # Removing the inactive saved tab models a user closing it while
            # inspecting the preview. No previous tab can be restored, but the
            # active view must still detach before the native texture is freed.
            tabs.removeTab(tabs.indexOf(prior_tab))
            check("closedPriorTabCannotBeRestored", not viewer.EndTemporaryOutputFollow(preview_token))
            preview_token = 0
            check("closedPriorTabStillDetachesFloatDescriptor", viewer.GetCurrentResource() == resource)
            replay_call(lambda ctrl: ctrl.ClearEventShaderOverride(event, rd.ShaderStage.Pixel))
            replay_call(lambda ctrl: ctrl.FreeTargetResource(preview_target))
            preview_target = None
            context.RefreshStatus()
            check("freedPreviewIsDetachedFromViewer", viewer.GetCurrentResource() == resource)

        host = QtWidgets.QWidget()
        host.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
        layout = QtWidgets.QVBoxLayout(host)
        editor = context.CreateShaderCodeEditor(host)
        editor_widget = editor.Widget()
        layout.addWidget(editor_widget)
        host.resize(720, 300)
        source = "// UTF-8 法线\nfloat4 main() : SV_Target {\n    float value = 0.25;\n    return float4(value, 0, 0, 1);\n}\n"
        editor.SetText(source)
        editor.SetSavePoint()
        hits, stops = [], []
        editor.SetVisualDebugCallback(lambda start, end: hits.append((int(start), int(end))))
        editor.SetStopVisualDebugCallback(lambda: stops.append(True))
        host.show()
        editor.GoToLine(4, 21)
        app.processEvents()
        editor.RequestVisualDebug()
        start = source.encode("utf-8").rindex(b"value")
        expected = (start, start + 5)
        check("caretRequestsExactUTF8Expression", hits[-1] == expected, hits)
        scintilla = editor_widget.findChild(QtWidgets.QWidget, "shaderCodeEditor")
        modifiers = QtCore.Qt.ControlModifier
        shortcut = QtGui.QKeyEvent(QtCore.QEvent.ShortcutOverride, QtCore.Qt.Key_E, modifiers)
        shortcut.ignore()
        app.sendEvent(scintilla, shortcut)
        check("debugShortcutClaimsOverrideWithoutStarting", shortcut.isAccepted() and len(hits) == 1)
        app.sendEvent(scintilla, QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_E, modifiers))
        check("startShortcutUsesSameExpression", len(hits) == 2 and hits[-1] == expected, hits)
        app.sendEvent(scintilla, QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_E,
                                               modifiers | QtCore.Qt.ShiftModifier))
        check("stopShortcutDelivered", len(stops) == 1)
        # Render directly through Qt, without OS screenshots or desktop focus.
        # Hide the caret's focus during the three synchronous visual witnesses.
        scintilla.clearFocus()
        app.processEvents()
        baseline = image_hash(editor_widget)
        editor.SetVisualDebugRange(start, start + 5, True)
        marked = image_hash(editor_widget)
        editor.SetVisualDebugRange(0, 0, False)
        cleared = image_hash(editor_widget)
        check("debugMarksChangeRenderedEditor", marked != baseline)
        check("debugMarksClearExactly", cleared == baseline,
              {"baseline": baseline, "marked": marked, "cleared": cleared})
        check("marksPreserveTextAndDirtyState", editor.GetText() == source and not editor.IsModified())
        editor.SetVisualDebugRange(start, start + 5, True)
        editor.SetText(source + "// edited\n")
        editor.SetVisualDebugRange(0, 0, False)
        check("editedSourceRemainsEditable", editor.GetText().endswith("// edited\n"))
        repository = config.get("repository")
        if not repository:
            repository = Path(__file__).resolve().parents[2]
        module_path = Path(repository) / "replaylab/renderdoc_mcp_bridge/project_shader_visual_debug.py"
        spec = importlib.util.spec_from_file_location("bread_visual_debug_ui_smoke_module", str(module_path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        panel = SimpleNamespace(top=host, ctx=context, _documents={})
        controls = module.ShaderVisualDebugController(panel, lambda name, params: {}, layout)
        try:
            check("realQtControlsDefaultToCapturedRT", not controls._options({})["independentFloatOutput"])
            controls.float_preview.setChecked(True)
            check("realQtControlsEnableFloatPreview", controls._options({})["independentFloatOutput"])
        finally:
            controls.timer.stop()
            controls.closed = True
        report["ok"] = True
    except BaseException:
        report["error"] = traceback.format_exc()
    finally:
        if viewer is not None and preview_token:
            viewer.EndTemporaryOutputFollow(preview_token)
        if preview_target is not None:
            replay_call(lambda ctrl: ctrl.ClearEventShaderOverride(event, rd.ShaderStage.Pixel))
            replay_call(lambda ctrl: ctrl.FreeTargetResource(preview_target))
        if editor is not None:
            editor.ClearCallbacks()
            layout.removeWidget(editor_widget)
            editor_widget.setParent(None)
            context.DestroyShaderCodeEditor(editor)
        if host is not None:
            host.close()
        output = Path(config["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        # Close only this fixture's own context. QApplication.quit alone does
        # not terminate qrenderdoc's manually pumped main-window loop.
        context.GetMainWindow().Widget().close()
        QtCore.QCoreApplication.exit(0 if report["ok"] else 1)


if "pyrenderdoc" in globals():
    pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(lambda: run(pyrenderdoc))
