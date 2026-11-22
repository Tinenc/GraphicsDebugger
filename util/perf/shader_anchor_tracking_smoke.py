"""Owned-process editor anchor checks; no capture, GPU or desktop input."""
import json
import importlib.util
import os
from pathlib import Path
import traceback
import types


def run(context):
    from PySide2 import QtCore, QtGui, QtWidgets
    app = QtWidgets.QApplication.instance()
    clipboard = app.clipboard()
    previous_clipboard = QtCore.QMimeData()
    previous_data = clipboard.mimeData() if clipboard is not None else None
    if previous_data is not None:
        for mime_type in previous_data.formats():
            previous_clipboard.setData(mime_type, previous_data.data(mime_type))
    report = {"ok": False, "pid": os.getpid(), "checks": []}
    output = Path(os.environ["BREAD_SHADER_ANCHOR_SMOKE_OUTPUT"])
    output.write_text(json.dumps(report), encoding="utf-8")
    host = QtWidgets.QWidget()
    host.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    editor = context.CreateShaderCodeEditor(host)
    widget = editor.Widget()
    layout = QtWidgets.QVBoxLayout(host)
    layout.addWidget(widget)
    host.show()
    target = widget.findChild(QtWidgets.QWidget, "shaderCodeEditor")
    control = QtCore.Qt.ControlModifier

    def check(name, value, detail=None):
        report["checks"].append({"name": name, "passed": bool(value), "detail": detail})
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if not value:
            raise AssertionError(name + ": " + str(detail))

    def key(code, modifiers=QtCore.Qt.NoModifier, text=""):
        app.sendEvent(target, QtGui.QKeyEvent(QtCore.QEvent.KeyPress, code, modifiers, text))

    def select(start, end):
        editor.SelectRange(start, end)

    def action(index, enabled=True):
        # Scintilla queries QApplication.keyboardModifiers(), not the flags on
        # synthetic key events. Trigger its real context-menu actions instead.
        if os.name == "nt":
            app.processEvents()
        report["stage"] = "context-menu-event"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        point = QtCore.QPoint(0, 0)
        app.sendEvent(target, QtGui.QContextMenuEvent(QtGui.QContextMenuEvent.Keyboard, point))
        report["stage"] = "context-menu-actions"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        menu = widget.findChildren(QtWidgets.QMenu)[-1]
        command = menu.actions()[index]
        if command.isEnabled() != enabled:
            raise AssertionError("unexpected editor action state: " + str(command.text()))
        if enabled:
            command.trigger()
        report["stage"] = "context-menu-triggered"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if os.name == "nt":
            app.processEvents()
        menu.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)

    def history(redo=False):
        action(1 if redo else 0)

    def replace(start, end, text):
        select(start, end)
        if text:
            key(QtCore.Qt.Key_unknown, text=text)
        elif end > start:
            key(QtCore.Qt.Key_Delete)

    def setup(source, expression, occurrence=0):
        editor.SetText(source)
        data = source.encode("utf-8")
        start = data.index(expression.encode("utf-8")) if not occurrence else data.rindex(expression.encode("utf-8"))
        return editor.CreateRangeAnchor(start, start + len(expression.encode("utf-8")))

    def selected(handle):
        positions = list(editor.GetRangeAnchorRange(handle))
        return str(editor.GetText()).encode("utf-8")[positions[0]:positions[1]].decode("utf-8") if len(positions) == 2 else None

    try:
        editor.SetText("emptyClipboard;")
        clipboard.clear()
        key(QtCore.Qt.Key_V, control)
        check("emptyClipboardPasteIsIgnored", str(editor.GetText()) == "emptyClipboard;")
        action(5, False)
        check("emptyClipboardMenuSafelyDisablesPaste", True)
        source = "// 法线\nfloat main() { result.albedo = 1; return 0; }\n"
        anchor = setup(source, "result.albedo")
        start = source.encode("utf-8").index(b"result.albedo")
        insertion = "result.albedo = 0.1; "
        replace(start, start, insertion)
        check("sameNameInsertedBeforeKeepsOriginalRange", list(editor.GetRangeAnchorRange(anchor))[0] == start + len(insertion), str(editor.GetText()))
        operator = str(editor.GetText()).encode("utf-8").index(b" = 1") + 1
        replace(operator, operator + 1, "+=")
        check("compoundAssignmentKeepsExpression", selected(anchor) == "result.albedo")
        member = list(editor.GetRangeAnchorRange(anchor))[0] + len("result.")
        replace(member, member + len("albedo"), "color")
        check("partialSuffixRename", selected(anchor) == "result.color", selected(anchor))
        prefix = list(editor.GetRangeAnchorRange(anchor))[0]
        replace(prefix, prefix + len("result"), "surface")
        check("partialPrefixRename", selected(anchor) == "surface.color", selected(anchor))
        history()
        check("undoRenameRestoresRange", selected(anchor) == "result.color", selected(anchor))
        history(True)
        check("redoRenameRestoresRange", selected(anchor) == "surface.color", selected(anchor))

        anchor = setup("a;", "a")
        replace(1, 1, "New")
        replace(0, 1, "")
        check("appendRenameThenDeleteOriginalPrefix", selected(anchor) == "New", selected(anchor))

        anchor = setup("left; right;", "left")
        select(0, 5)
        key(QtCore.Qt.Key_X, control)
        select(len(str(editor.GetText()).encode()), len(str(editor.GetText()).encode()))
        key(QtCore.Qt.Key_V, control)
        check("explicitCutPasteMovesRange", selected(anchor) == "left" and list(editor.GetRangeAnchorRange(anchor))[0] > 0, str(editor.GetText()))
        history()
        history()
        check("undoMoveRestoresOriginalRange", selected(anchor) == "left" and list(editor.GetRangeAnchorRange(anchor))[0] == 0, str(editor.GetText()))
        select(len(str(editor.GetText()).encode()), len(str(editor.GetText()).encode()))
        key(QtCore.Qt.Key_V, control)
        check("undoCutThenPasteDoesNotStealOriginal", list(editor.GetRangeAnchorRange(anchor))[0] == 0)

        anchor = setup("value;", "value")
        select(0, 6)
        action(4)
        check("copySelectedSource", str(app.clipboard().text()) == "value;", str(app.clipboard().text()))
        key(QtCore.Qt.Key_Delete)
        key(QtCore.Qt.Key_V, control)
        check("copyDeletePasteSameTextStaysDeleted", str(editor.GetText()) == "value;" and not list(editor.GetRangeAnchorRange(anchor)),
              {"source": str(editor.GetText()), "range": list(editor.GetRangeAnchorRange(anchor)),
               "clipboard": str(app.clipboard().text())})
        history()
        history()
        check("undoDeletionRestoresOriginalObject", selected(anchor) == "value")
        history(True)
        check("redoDeletionInvalidatesAgain", not list(editor.GetRangeAnchorRange(anchor)))

        editor.SetText("float3 Evaluate() { return 0; }\n")
        line = editor.CreateLineAnchor(1)
        replace(0, 6, "")
        check("functionLineSurvivesReturnTypeDeletion", editor.GetLineAnchorLine(line) == 1)
        old = setup("tracked;", "tracked")
        editor.SetText("tracked;")
        check("setTextInvalidatesOldRangeIds", not list(editor.GetRangeAnchorRange(old)))
        editor.SelectRange(100000, 100001)
        check("selectionClampsToDocument", editor.GetCursorColumn() == len("tracked;") + 1)

        # Exercise the Python persistence lifecycle against these same real
        # Scintilla edits, including native identity changes with identical text.
        bridge = Path(os.environ["BREAD_SHADER_ANCHOR_SOURCE_ROOT"]) / "replaylab" / "renderdoc_mcp_bridge"
        def load(name, filename):
            spec = importlib.util.spec_from_file_location(name, str(bridge / filename))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        backend = load("native_snapshot_backend", "shader_visual_snapshots.py")
        ui = load("native_snapshot_ui", "project_shader_debug_snapshots.py")
        source = "float helper(float x) { float rim = x; return rim; }\n"
        editor.SetText(source)
        workspace = {"workspacePath": "owned-smoke", "stage": "Pixel", "shaderRawSha256": "a" * 64}
        document = {"editor": editor, "baselineEditorText": source, "available": True,
                    "workspaceId": "owned-smoke", "workspace": workspace,
                    "sourcePath": str(Path(os.environ["TEMP"]) / "owned-smoke.hlsl")}
        identity = ui.snapshot_identity(document, workspace)
        start = source.rindex("rim")
        record = backend.make_snapshot(source, {"byteStart": start, "byteEnd": start + 3}, identity)
        store = backend.SnapshotStore(str(Path(os.environ["TEMP"]) / "snapshot-store"), identity)
        store.add(record)
        live = ui.LiveSnapshotAnchors(backend, store, document, identity)
        replace(start, start, "rim + ")
        live.flush()
        located = live.resolve(store.list()[0], str(editor.GetText()))
        check("liveStoreTracksOriginalAfterDuplicateInsertion",
              located.get("selection", {}).get("byteStart") == start + 6, located)
        live.reset(source)
        editor.SetText(source)
        live = ui.LiveSnapshotAnchors(backend, store, document, identity)
        select(start, start + 3)
        action(4)
        key(QtCore.Qt.Key_Delete)
        key(QtCore.Qt.Key_V, control)
        live.flush()
        check("liveStoreSameBytesIdentityDeleted", str(editor.GetText()) == source and
              store.list()[0]["anchor"].get("invalidated") == "snapshot-expression-deleted")
        document["pendingSave"] = {"saveToken": 1}
        live.submitted()
        live.saved(source)
        document["pendingSave"] = None
        live.reset(source)
        check("savedDeletionSurvivesReset", store.list()[0]["anchor"].get("invalidated") == "snapshot-expression-deleted")

        store.remove(record["id"])
        store.add(record)
        incomplete = source.replace("}", "")
        editor.SetText(incomplete)
        live = ui.LiveSnapshotAnchors(backend, store, document, identity)
        check("incompleteReloadIsPendingNotDeleted", live.handles[record["id"]] is None and
              live.resolve(record, incomplete).get("reason") != "snapshot-expression-deleted")
        replace(len(incomplete) - 1, len(incomplete) - 1, "}")
        live.flush()
        check("repairIncompleteReloadRecoversWithinSameEditor",
              live.resolve(store.list()[0], str(editor.GetText())).get("status") == "resolved")
        live.close()

        requests = []
        panel = types.SimpleNamespace(_current_document=lambda: document,
            _document_model=lambda unused: workspace,
            visual_debug=types.SimpleNamespace(begin=lambda *args, **kwargs: requests.append((args, kwargs))))
        view = ui.ShaderDebugSnapshots(panel, host)
        view.timer.stop()
        view._module, view._store, view._identity = backend, store, identity
        view.refresh(True)
        check("snapshotSidebarListsLiveLocation", view.list.count() == 1 and
              view._rows[record["id"]][1].get("status") == "resolved")
        view.activate(view.list.item(0))
        check("snapshotClickSelectsExactExpressionAndStartsCurrentSourceDebug",
              len(requests) == 1 and requests[0][0][1:3] == (start, start + 3) and
              editor.GetCursorColumn() == start + 4, requests)
        view.release_document(document)
        view.close()
        report["ok"] = True
    except BaseException:
        report["error"] = traceback.format_exc()
    finally:
        if clipboard is not None:
            if app.platformName() == "offscreen":
                # This platform's clipboard is process-local. Do not leave a
                # Python-owned QMimeData in Qt's global clipboard past Python
                # finalization (PySide2 5.15 would destroy it after the runtime).
                clipboard.clear()
            else:
                clipboard.setMimeData(previous_clipboard)
        editor.ClearCallbacks()
        layout.removeWidget(widget)
        widget.setParent(None)
        context.DestroyShaderCodeEditor(editor)
        host.close()
        Path(os.environ["BREAD_SHADER_ANCHOR_SMOKE_OUTPUT"]).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        context.GetMainWindow().Widget().close()
        QtCore.QCoreApplication.exit(0 if report["ok"] else 1)


if "pyrenderdoc" in globals():
    pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(lambda: run(pyrenderdoc))
