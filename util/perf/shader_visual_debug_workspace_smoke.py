"""Exercise the registered Shader Workspace in an owned --ui-script process.

BREAD_SHADER_VISUAL_DEBUG_WORKSPACE_CONFIG names a JSON file with capture,
sourceFile and output. Optional fields: eventId, expression (default probe),
byteStart, expectedExpression, member and screenshotDirectory. Function/return
tokens use the real native caret callback. An explicit byteEnd uses the product
controller's range entry point (recorded separately, not claimed as native drag
selection), since IShaderCodeEditor exposes no selection setter.
Launch a fresh, isolated, analytics-opted-out
profile with an external deadline. This fixture uses only its own Qt widgets
and capture context; the real bridge/controller/Manager perform every operation.
Compatible with qrenderdoc's embedded Python 3.6.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback


def run(context):
    import renderdoc as rd
    from PySide2 import QtCore, QtGui, QtWidgets

    config = json.loads(Path(os.environ[
        "BREAD_SHADER_VISUAL_DEBUG_WORKSPACE_CONFIG"]).read_text(encoding="utf-8-sig"))
    output = Path(config["output"]).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    screenshots = Path(config.get("screenshotDirectory") or
                       str(output.parent / (output.stem + "-screenshots")))
    screenshots.mkdir(parents=True, exist_ok=True)
    report = {"schema": "bread-shader-visual-debug-workspace-smoke/v1",
              "ok": False, "pid": os.getpid(), "checks": [], "screenshots": []}
    app = QtWidgets.QApplication.instance()
    panel = controller = editor = document = None
    owned_capture = False
    menu = None
    picker_timer = None
    started = time.monotonic()

    def save_report():
        report["elapsedSeconds"] = round(time.monotonic() - started, 3)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def check(name, condition, details=None):
        report["checks"].append({"name": name, "passed": bool(condition), "details": details})
        save_report()
        if not condition:
            raise AssertionError(name + ": " + str(details))

    def state():
        if controller is None:
            return {}
        return {"active": controller.active, "busy": controller.busy,
                "stopping": controller._stopping, "sessionId": controller.session_id,
                "label": controller.label.text()}

    def wait_for(name, predicate):
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            app.processEvents(QtCore.QEventLoop.AllEvents, 20)
            if report.get("memberPickerError"):
                check(name, False, report["memberPickerError"])
            if predicate():
                check(name, True, state())
                return
            # Yield the GIL to the actual controller/replay workers. No fake
            # operation, timer drain replacement or direct Manager call is used.
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
        filename = screenshots / (name + ".png")
        check("screenshot:" + name, panel.top.grab().save(str(filename), "PNG"))
        report["screenshots"].append(str(filename))
        save_report()

    def source_unchanged(name):
        check(name, str(editor.GetText()) == original_text and
              bool(editor.IsModified()) == original_modified and
              bool(document.get("dirty")) == original_dirty and
              source_path.read_bytes() == original_disk,
              {"editorModified": bool(editor.IsModified()),
               "documentDirty": bool(document.get("dirty")),
               "sourceSha256": hashlib.sha256(str(editor.GetText()).encode("utf-8")).hexdigest()})

    def key_command(shift):
        editor.GoToLine(line, column)
        scintilla.setFocus(QtCore.Qt.OtherFocusReason)
        modifiers = QtCore.Qt.ControlModifier
        if shift:
            modifiers |= QtCore.Qt.ShiftModifier
        event = QtGui.QKeyEvent(QtCore.QEvent.ShortcutOverride, QtCore.Qt.Key_E, modifiers)
        event.ignore()
        app.sendEvent(scintilla, event)
        check("shortcutOverride:" + ("Ctrl+Shift+E" if shift else "Ctrl+E"), event.isAccepted())
        app.sendEvent(scintilla, QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_E, modifiers))
        app.sendEvent(scintilla, QtGui.QKeyEvent(QtCore.QEvent.KeyRelease, QtCore.Qt.Key_E, modifiers))

    try:
        check("freshOwnedContext", not context.IsCaptureLoaded())
        bridges = []
        def find_registered_bridge():
            bridges[:] = [module for module in list(sys.modules.values())
                          if module is not None and
                          str(getattr(module, "__file__", "")).replace("\\", "/").endswith(
                              "/renderdoc_mcp_bridge/__init__.py") and
                          getattr(module, "_shader_workspace_ui", None) is not None]
            return len(bridges) == 1
        wait_for("registeredBridgeAvailable", find_registered_bridge)
        bridge = bridges[0]
        panel = bridge._shader_workspace_ui
        controller = panel.visual_debug
        check("usesRegisteredProductController", controller.panel is panel and
              controller.operation is bridge._shader_visual_debug_operation,
              {"bridgeFile": str(bridge.__file__), "controllerModule": type(controller).__module__})

        capture = str(Path(config["capture"]).resolve())
        source_path = Path(config["sourceFile"]).resolve()
        original_disk = source_path.read_bytes()
        context.LoadCapture(capture, rd.ReplayOptions(), capture, False, True)
        owned_capture = bool(context.IsCaptureLoaded())
        check("captureLoaded", owned_capture, capture)
        event = int(config.get("eventId", 0))
        if not event:
            pending, draws = list(context.CurRootActions()), []
            while pending:
                action = pending.pop(0)
                pending.extend(action.children)
                if action.flags & rd.ActionFlags.Drawcall:
                    draws.append(int(action.eventId))
            check("captureHasDraw", bool(draws))
            event = max(draws)
        context.SetEventID([], event, event, True)
        report["eventId"] = event
        panel.show()
        check("freshWorkspaceHasNoDocument", panel._current_document() is None)
        controller.capture_current(source_file=str(source_path))
        wait_for("sourceOpenedAsynchronously", lambda: panel._current_document() is not None and
                 not controller.busy)
        document = panel._current_document()
        editor = document["editor"]
        editor_widget = document["editorWidget"]
        scintilla = editor_widget.findChild(QtWidgets.QWidget, "shaderCodeEditor")
        check("nativeEditorAvailable", scintilla is not None)
        original_text = str(editor.GetText())
        original_modified = bool(editor.IsModified())
        original_dirty = bool(document.get("dirty"))
        check("sourceFileLoaded", original_text.replace("\r\n", "\n") ==
              original_disk.decode("utf-8-sig").replace("\r\n", "\n"),
              {"sourcePath": document.get("sourcePath"), "entryPoint": document.get("entryPoint")})
        raw = original_text.encode("utf-8")
        expression = str(config.get("expression") or "probe")
        encoded_expression = expression.encode("utf-8")
        byte_start = int(config.get("byteStart", raw.rfind(encoded_expression)))
        byte_end = int(config.get("byteEnd", byte_start + len(encoded_expression)))
        expected_expression = str(config.get("expectedExpression") or expression)
        expected_member = str(config.get("member") or "")
        explicit_range = "byteEnd" in config
        report["selectionPath"] = "controller-explicit-range" if explicit_range else "native-caret"
        check("selectedExpressionExists", 0 <= byte_start < byte_end <= len(raw) and
              raw[byte_start:byte_start + len(encoded_expression)] == encoded_expression,
              {"expression": expression, "byteStart": byte_start, "byteEnd": byte_end})
        line = raw[:byte_start].count(b"\n") + 1
        column = byte_start - raw.rfind(b"\n", 0, byte_start)
        editor.GoToLine(line, column)
        panel.show()
        app.processEvents()
        controls = (controller.start_button, controller.stop_button)
        check("startAndRestoreVisibleAboveSource", all(button.isVisible() and
              button.mapTo(panel.top, QtCore.QPoint()).y() <
              editor_widget.mapTo(panel.top, QtCore.QPoint()).y() for button in controls),
              [button.text() for button in controls])
        check("shortcutHintsVisible", "Ctrl+E" in controller.start_button.text() and
              "Ctrl+E" in controller.start_button.toolTip() and
              "Ctrl+Shift+E" in controller.stop_button.text())
        check("defaultUsesCapturedRT", not controller.float_preview.isChecked())
        check("customDisplayIsDefault", controller.mode.currentData() == "custom")
        check("sourceImportMenuRemoved", not hasattr(controller, "capture_source_button"))

        # Interact with the real modal Qt picker from inside this owned process.
        # Do not replace QInputDialog.getItem: inspect the options actually shown.
        if "member" in config:
            report["memberPickers"] = []
            def choose_member():
                dialog = app.activeModalWidget()
                if not isinstance(dialog, QtWidgets.QInputDialog):
                    return
                combos = dialog.findChildren(QtWidgets.QComboBox)
                if not combos:
                    return  # A snapshot rename dialog is not a member picker.
                labels = [combos[0].itemText(i) for i in range(combos[0].count())] if combos else []
                expected_label = expected_expression + expected_member + " ("
                matches = [i for i, text in enumerate(labels) if text.startswith(expected_label)]
                if len(matches) != 1:
                    report["memberPickerError"] = {"expected": expected_label, "shown": labels}
                    dialog.reject()
                    return
                combos[0].setCurrentIndex(matches[0])
                report["memberPickers"].append({"title": dialog.windowTitle(),
                    "prompt": dialog.labelText(), "shown": labels, "selected": labels[matches[0]]})
                save_report()
                dialog.accept()
            picker_timer = QtCore.QTimer(panel.top)
            picker_timer.setInterval(10)
            picker_timer.timeout.connect(choose_member)
            picker_timer.start()

        # Emit the actual native context-menu signal, then inspect its QActions.
        # This invokes ShaderCodeEditor::ShowContextMenu without desktop input.
        # A long selected expression may have scrolled horizontally beyond the
        # first line. Put a real source line under the menu's test point first.
        editor.GoToLine(1, 1)
        app.processEvents()
        scintilla.customContextMenuRequested.emit(QtCore.QPoint(180, 12))
        # QMenu::popup is synchronous. Inspect before pumping unrelated native
        # focus events, which can dismiss a popup in a hidden fixture window.
        check("nativeContextMenuOpened", isinstance(app.activePopupWidget(), QtWidgets.QMenu))
        menu = app.activePopupWidget()
        action_text = lambda text: QtCore.QCoreApplication.translate("ShaderCodeEditor", text)
        actions = {action.text(): action for action in menu.actions()}
        start_action = actions.get(action_text("Output to RT"))
        stop_action = actions.get(action_text("Stop visual debug"))
        check("contextMenuCommandsAndShortcuts", start_action is not None and stop_action is not None and
              start_action.isEnabled() and stop_action.isEnabled() and
              start_action.shortcut().toString(QtGui.QKeySequence.PortableText) == "Ctrl+E" and
              stop_action.shortcut().toString(QtGui.QKeySequence.PortableText) == "Ctrl+Shift+E",
              [{"text": action.text(), "shortcut": action.shortcut().toString(QtGui.QKeySequence.PortableText),
                "enabled": action.isEnabled()} for action in menu.actions() if not action.isSeparator()])
        menu.close()
        menu = None
        editor.GoToLine(line, column)
        screenshot("source-ready")

        targets = [(index, target) for index, target in
                   enumerate(context.CurPipelineState().GetOutputTargets()) if int(target.resource) != 0]
        check("captureHasColorTargets", bool(targets))
        def texture_hashes():
            def read(replay):
                hashes = {}
                for index, target in targets:
                    subresource = rd.Subresource()
                    subresource.mip = max(0, int(getattr(target, "firstMip", 0)))
                    subresource.slice = max(0, int(getattr(target, "firstSlice", 0)))
                    data = bytes(replay.GetTextureData(target.resource, subresource))
                    if not data:
                        raise RuntimeError("Empty render-target data at slot %d" % index)
                    hashes[str(index)] = hashlib.sha256(data).hexdigest()
                return hashes
            return replay_call(read)

        def shader_state():
            def read(replay):
                pipeline = replay.GetPipelineState()
                reflection = pipeline.GetShaderReflection(rd.ShaderStage.Pixel)
                override = replay.GetEventShaderOverride(event, rd.ShaderStage.Pixel)
                return {"shaderId": int(pipeline.GetShader(rd.ShaderStage.Pixel)),
                        "rawSha256": hashlib.sha256(bridge._shader_raw_bytes(reflection)).hexdigest(),
                        "override": {"active": bool(override.active),
                                     "replacement": int(override.replacement),
                                     "expectedOriginal": int(override.expectedOriginal),
                                     "outputTarget": int(override.outputTarget),
                                     "independentFloatOutput": bool(override.independentFloatOutput)}}
            return replay_call(read)

        baseline = texture_hashes()
        baseline_shader = shader_state()
        baseline_repeats = [baseline]
        for _unused in range(max(1, int(config.get("originalReplaySamples", 1))) - 1):
            replay_call(lambda replay: replay.SetFrameEvent(event, True))
            baseline_repeats.append(texture_hashes())
        unstable = [slot for slot in baseline if len(set(sample[slot] for sample in baseline_repeats)) > 1]
        baseline = baseline_repeats[-1]
        report["originalReplayTargetHashes"] = baseline_repeats
        report["originalReplayUnstableTargets"] = unstable
        report["baselineShaderState"] = baseline_shader
        report["baselineTargetHashes"] = baseline

        def check_restored(name, hashes):
            current_shader = shader_state()
            details = {"before": baseline, "after": hashes, "byteExact": hashes == baseline,
                       "originalReplayUnstableTargets": unstable,
                       "beforeShader": baseline_shader, "afterShader": current_shader}
            # Byte equality remains mandatory for every deterministic target.
            # Explicit repeated-original controls can demonstrate a capture's
            # own nondeterminism. Record the limitation rather than claiming
            # that those targets were byte-exact after restoration.
            check(name if not unstable else name.replace("ExactGPUOutput", "StableOutputsAndShaderState"),
                  current_shader == baseline_shader and all(
                      hashes[slot] == baseline[slot] for slot in baseline if slot not in unstable), details)
        if explicit_range:
            controller.begin(document["workspaceId"], byte_start, byte_end)
        else:
            controller.start_button.click()
        wait_for("explicitRangeStartsRealDebugSession" if explicit_range else "buttonStartsRealDebugSession", lambda: controller.active and
                 bool(controller.session_id) and not controller.busy)
        check("explicitRangeReachedManager" if explicit_range else "caretSelectionReachedManager", controller.request.get("byteStart") == byte_start and
              controller.request.get("byteEnd") == byte_end, controller.request.get("byteStart"))
        selection = bridge._shader_visual_debug_manager._session["selection"]
        check("semanticExpressionReachedManager", selection.get("expression") == expected_expression,
              selection)
        check("chosenMemberReachedManager", controller.request.get("member", "") == expected_member,
              controller.request.get("member", ""))
        if "member" in config:
            check("realMemberPickerWasShown", len(report["memberPickers"]) == 1, report["memberPickers"])
        debug_hashes = texture_hashes()
        check("explicitRangeChangesActualGPUOutput" if explicit_range else "buttonChangesActualGPUOutput", debug_hashes != baseline,
              {"before": baseline, "debug": debug_hashes})
        check("activeStateClearlyMarked", controller.stop_button.isEnabled() and
              controller.label.font().bold() and expected_expression + expected_member in controller.label.text(), state())
        source_unchanged("explicitRangePreservesSourceAndDirtyState" if explicit_range else "buttonDebugPreservesSourceAndDirtyState")
        screenshot("explicit-range-active" if explicit_range else "button-debug-active")

        key_command(True)
        wait_for("shortcutStopsRealDebugSession", lambda: not controller.active and
                 not controller.session_id and not controller.busy and not controller._stopping)
        restored_hashes = texture_hashes()
        report["shortcutRestoredTargetHashes"] = restored_hashes
        check_restored("shortcutRestoresExactGPUOutput", restored_hashes)
        source_unchanged("shortcutRestorePreservesSourceAndDirtyState")

        if explicit_range:
            controller.begin(document["workspaceId"], byte_start, byte_end)
        else:
            key_command(False)
        wait_for("explicitRangeRestartsRealDebugSession" if explicit_range else "shortcutRestartsRealDebugSession", lambda: controller.active and
                 bool(controller.session_id) and not controller.busy)
        if "member" in config:
            check("memberChoiceIsRequestedAgain", len(report["memberPickers"]) == 2, report["memberPickers"])
        repeated_debug_hashes = texture_hashes()
        check(("explicitRangeProducesSameGPUOutput" if explicit_range else "shortcutProducesSameGPUOutput")
              if not unstable else "repeatedPreviewPreservesStableOutputs",
              all(repeated_debug_hashes[slot] == debug_hashes[slot] for slot in debug_hashes if slot not in unstable),
              {"before": debug_hashes, "after": repeated_debug_hashes,
               "byteExact": repeated_debug_hashes == debug_hashes,
               "originalReplayUnstableTargets": unstable})
        source_unchanged("rangeRestartPreservesSourceAndDirtyState" if explicit_range else "shortcutDebugPreservesSourceAndDirtyState")
        screenshot("range-restarted" if explicit_range else "shortcut-debug-active")
        controller.stop_button.click()
        wait_for("buttonStopsRealDebugSession", lambda: not controller.active and
                 not controller.session_id and not controller.busy and not controller._stopping)
        restored_hashes = texture_hashes()
        report["buttonRestoredTargetHashes"] = restored_hashes
        check_restored("restoreButtonRestoresExactGPUOutput", restored_hashes)
        check("restoreButtonReturnsToIdle", not controller.stop_button.isEnabled() and
              controller.start_button.isEnabled() and not controller.label.font().bold())
        source_unchanged("completeRoundTripPreservesSourceAndDirtyState")
        screenshot("restored")
        if config.get("snapshotChecks"):
            snapshots = panel.debug_snapshots
            controller.begin(document["workspaceId"], byte_start, byte_end)
            wait_for("snapshotSetupDebugActive", lambda: controller.active and not controller.busy)
            panel.show()
            app.processEvents()
            controller.snapshot_button.click()
            wait_for("snapshotButtonCreatesEntry", lambda: snapshots.list.count() == 1)
            screenshot("snapshot-created")
            check("snapshotSidebarIsLeftOfSource", snapshots.isVisible() and
                  snapshots.mapTo(panel.top, QtCore.QPoint()).x() <
                  editor_widget.mapTo(panel.top, QtCore.QPoint()).x(),
                  {"visible": snapshots.isVisible(), "sizes": panel.main_splitter.sizes(),
                   "snapshotX": snapshots.mapTo(panel.top, QtCore.QPoint()).x(),
                   "editorX": editor_widget.mapTo(panel.top, QtCore.QPoint()).x()})
            panel.snapshots_button.click()
            check("snapshotSidebarHideButton", not snapshots.isVisible())
            panel.snapshots_button.click()
            check("snapshotSidebarShowButton", snapshots.isVisible())
            first_id = snapshots.list.item(0).data(QtCore.Qt.UserRole)
            snapshots.list.setCurrentRow(0)
            rename_timer = QtCore.QTimer(panel.top)
            def rename_dialog():
                dialog = app.activeModalWidget()
                if isinstance(dialog, QtWidgets.QInputDialog) and dialog.inputMode() == QtWidgets.QInputDialog.TextInput:
                    dialog.setTextValue("边缘光测试")
                    dialog.accept()
            rename_timer.timeout.connect(rename_dialog)
            rename_timer.start(10)
            snapshots.rename_button.click()
            rename_timer.stop()
            check("snapshotChineseRenamePersists", snapshots._store.list()[0]["name"] == "边缘光测试" and
                  "边缘光测试" in snapshots.list.item(0).text())
            # Trigger the registered Qt shortcut itself. Native event routing is
            # separately exercised for Ctrl+E above; no desktop input is used.
            shortcut = controller.snapshot_shortcut
            check("snapshotShortcutRegistered", shortcut.key().toString() == "Ctrl+Shift+B" and shortcut.isEnabled())
            app.sendEvent(shortcut, QtGui.QShortcutEvent(shortcut.key(), shortcut.id()))
            wait_for("snapshotShortcutCreatesSecondEntry", lambda: snapshots.list.count() == 2)
            check("snapshotDragModeEnabled", snapshots.list.dragDropMode() == QtWidgets.QAbstractItemView.InternalMove)
            # Exercise the same persisted-order callback as the view's rowsMoved.
            item = snapshots.list.takeItem(0)
            snapshots.list.insertItem(1, item)
            snapshots.reordered()
            check("snapshotReorderPersists", snapshots._store.list()[1]["id"] == first_id)
            snapshots.list.setCurrentRow(0)
            snapshots.delete_button.click()
            check("snapshotDeletePersists", snapshots.list.count() == 1 and snapshots._store.list()[0]["id"] == first_id)
            controller.stop_button.click()
            wait_for("snapshotSetupStopped", lambda: not controller.request and not controller.busy)
            snapshots.activate(snapshots.list.item(0))
            wait_for("snapshotClickReappliesDebug", lambda: controller.active and not controller.busy)
            check("snapshotReusesCompiledShader", bridge._shader_visual_debug_manager._session["cacheHit"])
            check("snapshotRestoresMember", controller.request.get("member", "") == expected_member)
            snapshot_hashes = texture_hashes()
            controller.stop_button.click()
            wait_for("snapshotBeforeEditingStopped", lambda: not controller.request and not controller.busy)
            edit_find = str(config.get("snapshotEditFind") or "rim *= 0.5")
            edit_replace = str(config.get("snapshotEditReplace") or "rim *= 0.8")
            prefix = "// 快照前插入的行\n\n"
            edited = prefix + original_text.replace(edit_find, edit_replace)
            check("fixtureChangesEarlierCalculation", edited != "// 快照前插入的行\n\n" + original_text)
            try:
                # Use the real native edit notifications. SetText is an explicit
                # document reload and deliberately destroys old range handles.
                current_bytes = str(editor.GetText()).encode("utf-8")
                edit_start = current_bytes.index(edit_find.encode("utf-8"))
                editor.SelectRange(edit_start, edit_start + len(edit_find.encode("utf-8")))
                app.sendEvent(scintilla, QtGui.QKeyEvent(
                    QtCore.QEvent.KeyPress, QtCore.Qt.Key_unknown, QtCore.Qt.NoModifier, edit_replace))
                editor.SelectRange(0, 0)
                app.sendEvent(scintilla, QtGui.QKeyEvent(
                    QtCore.QEvent.KeyPress, QtCore.Qt.Key_unknown, QtCore.Qt.NoModifier, prefix))
                check("snapshotFixtureUsesNativeEdits", str(editor.GetText()) == edited)
                editor.SetModified(True)
                panel._document_text_changed(document["workspaceId"])
                snapshots.refresh(True)
                snapshots.activate(snapshots.list.item(0))
                wait_for("snapshotResolvesEditedSource", lambda: controller.active and not controller.busy)
                selection = bridge._shader_visual_debug_manager._session["selection"]
                check("snapshotTracksInsertedLines", selection["line"] == line + 2 and
                      selection["expression"] == expected_expression, selection)
                check("snapshotRendersLatestCalculation", texture_hashes() != snapshot_hashes)
                check("snapshotEditsNeverWriteSource", source_path.read_bytes() == original_disk)
                screenshot("snapshot-edited-source")
                controller.stop_button.click()
                wait_for("snapshotEditedPreviewStopped", lambda: not controller.request and not controller.busy)
            finally:
                snapshots.release_document(document, original_text)
                editor.SetText(original_text)
                editor.SetModified(original_modified)
                document["dirty"] = original_dirty
                snapshots.bind_document(document, panel._document_model(document["workspaceId"]) or
                                        document.get("workspace") or {})
            snapshots.refresh(True)
            check_restored("snapshotRestoresExactGPUOutput", texture_hashes())
            source_unchanged("snapshotsPreserveDiskAndOriginalBuffer")
            screenshot("snapshots-left-sidebar")
        if config.get("followChecks"):
            # Discover another compatible Draw from this small owned fixture.
            def candidates(replay):
                pending, draws, nondraws = list(replay.GetRootActions()), [], []
                while pending:
                    action = pending.pop()
                    pending.extend(action.children)
                    if not action.children:
                        (draws if action.flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.MeshDispatch)
                         else nondraws).append(int(action.eventId))
                original_shader = int(replay.GetPipelineState().GetShader(rd.ShaderStage.Pixel))
                matched = None
                try:
                    for candidate in sorted(draws):
                        if candidate == event:
                            continue
                        replay.SetFrameEvent(candidate, True)
                        if int(replay.GetPipelineState().GetShader(rd.ShaderStage.Pixel)) == original_shader:
                            matched = candidate
                            break
                finally:
                    replay.SetFrameEvent(event, True)
                return matched, next((value for value in nondraws if value > 0), 0)
            compatible, incompatible = replay_call(candidates)
            check("followFixtureHasEvents", compatible is not None and incompatible > 0,
                  {"compatible": compatible, "incompatible": incompatible})
            controller.begin(document["workspaceId"], byte_start, byte_end)
            wait_for("followSetupActive", lambda: controller.active and not controller.busy)
            context.SetEventID([], compatible, compatible, True)
            wait_for("followCompatibleDrawActive", lambda: controller.active and not controller.busy and
                     controller.request.get("eventId") == compatible)
            check("followOnlyCurrentDrawOverridden", replay_call(lambda replay:
                  not replay.GetEventShaderOverride(event, rd.ShaderStage.Pixel).active and
                  replay.GetEventShaderOverride(compatible, rd.ShaderStage.Pixel).active))
            check("followUsesCompiledCache", bridge._shader_visual_debug_manager._session["cacheHit"])
            context.SetEventID([], incompatible, incompatible, True)
            wait_for("followIncompatibleEventPauses", lambda: controller.paused and not controller.busy and
                     controller.request.get("eventId") == incompatible)
            check("followPauseRemovesPreviousOverride", replay_call(lambda replay:
                  not replay.GetEventShaderOverride(compatible, rd.ShaderStage.Pixel).active))
            context.SetEventID([], event, event, True)
            wait_for("followOriginalDrawResumes", lambda: controller.active and not controller.busy and
                     controller.request.get("eventId") == event)
            check("followReturnUsesCompiledCache", bridge._shader_visual_debug_manager._session["cacheHit"])
            controller.stop_button.click()
            wait_for("followStopClearsIntent", lambda: not controller.request and not controller.busy)
            check_restored("followRestoresExactGPUOutput", texture_hashes())
            screenshot("follow-restored")
        report["ok"] = True
    except BaseException:
        report["error"] = traceback.format_exc()
        report["lastControllerState"] = state()
    finally:
        cleanup_errors = []
        if picker_timer is not None:
            picker_timer.stop()
        try:
            if menu is not None:
                menu.close()
            if controller is not None and (controller.request or controller.session_id or
                                           controller.busy or controller._stopping):
                if not controller._stopping:
                    controller.stop()
                wait_for("cleanupRestoresDebug", lambda: not controller.active and
                         not controller.session_id and not controller.busy and not controller._stopping)
        except BaseException:
            cleanup_errors.append(traceback.format_exc())
        try:
            if owned_capture and context.IsCaptureLoaded():
                context.CloseCapture()
        except BaseException:
            cleanup_errors.append(traceback.format_exc())
        if cleanup_errors:
            report["ok"] = False
            report["cleanupErrors"] = cleanup_errors
        save_report()
        # Close only the fixture process's context. A launcher supplies the
        # outer deadline if a native replay or shutdown unexpectedly blocks.
        context.GetMainWindow().Widget().close()
        QtCore.QCoreApplication.exit(0 if report["ok"] else 1)


if "pyrenderdoc" in globals():
    pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(lambda: run(pyrenderdoc))
