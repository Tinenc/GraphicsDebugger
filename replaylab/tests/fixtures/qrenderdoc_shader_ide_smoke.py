"""Run with --ui-python in an isolated qrenderdoc instance; no capture mutation.

REPLAYLAB_SHADER_IDE_SMOKE_OUTPUT selects the JSON receipt. The adjacent PNG
shows the actual native editor and fitted graph. No HLSL file is written,
compiled or replaced; the compiler diagnostic test exercises UI wiring only.
"""
from __future__ import print_function

import json
import os
import sys
import tempfile

try:
    from PySide2 import QtCore, QtGui, QtWidgets
    import qrenderdoc as qrd
    from renderdoc_mcp_bridge.project_shader_flow_graph import ShaderFlowGraphPanel
except Exception as bootstrap_error:
    bootstrap_output = os.environ.get("REPLAYLAB_SHADER_IDE_SMOKE_OUTPUT", "")
    if bootstrap_output:
        with open(bootstrap_output, "w", encoding="utf-8") as stream:
            json.dump({"ok": False, "stage": "imports", "error": str(bootstrap_error)}, stream)
    raise


SOURCE = (
    "float Evaluate(float value) {\n"
    "  float doubled = value * 2.0;\n"
    "  return doubled;\n"
    "}\n"
    "float4 MainPS(float4 pos : SV_Position) : SV_Target {\n"
    "  return float4(Evaluate(pos.x) + ExternalValue(pos.y), 0, 0, 1);\n"
    "}\n")


def _settle(milliseconds=80):
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(milliseconds, loop.quit)
    loop.exec_()


def _checkpoint(stage):
    path = os.environ.get("REPLAYLAB_SHADER_IDE_SMOKE_OUTPUT", "")
    if path:
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"ok": False, "stage": stage}, stream)


def _key(widget, code, modifiers=QtCore.Qt.NoModifier, text=""):
    QtWidgets.QApplication.sendEvent(widget, QtGui.QKeyEvent(
        QtCore.QEvent.KeyPress, code, modifiers, text))
    QtWidgets.QApplication.sendEvent(widget, QtGui.QKeyEvent(
        QtCore.QEvent.KeyRelease, code, modifiers, text))


def _fits(view):
    projected = view.mapFromScene(view.scene().itemsBoundingRect()).boundingRect()
    visible = view.viewport().rect().adjusted(-2, -2, 2, 2)
    return visible.contains(projected)


class _Runner(object):
    def run(self):
        output = os.environ.get("REPLAYLAB_SHADER_IDE_SMOKE_OUTPUT", "")
        result = {"schema": "replaylab-shader-ide-smoke/v1", "checks": {},
                  "captureMutated": False, "compilerDiagnostic": "injected-ui-only"}
        editors = []
        host = None
        context = None
        try:
            _checkpoint("creating-editor")
            # PythonShell's proxy does not expose the optional editor factory.
            # Use the same native UI context as the first-party workspace panel.
            context = getattr(sys.modules.get("renderdoc_mcp_bridge"), "_ctx", None)
            if context is None:
                raise RuntimeError("The built-in UI context is unavailable")
            host = QtWidgets.QWidget()
            host.setWindowTitle("ReplayLab scratch editor verification")
            host.resize(1150, 780)
            layout = QtWidgets.QHBoxLayout(host)
            stack = QtWidgets.QStackedWidget(host)
            layout.addWidget(stack, 3)
            editors = [context.CreateShaderCodeEditor(stack) for _ in range(2)]
            if any(editor is None for editor in editors):
                raise RuntimeError("The UI context returned no native editor")
            for editor in editors:
                stack.addWidget(editor.Widget())
                editor.SetLanguage("hlsl")
                editor.SetReadOnly(False)
            paths = [os.path.join(tempfile.gettempdir(), "replaylab-ide-scratch-%d-%d.hlsl" %
                                  (os.getpid(), index)) for index in range(2)]
            for editor, path in zip(editors, paths):
                editor.SetFileName(path)
            editors[0].SetText(SOURCE)
            editors[1].SetText("float ExternalValue(float v) { return v; }\n")
            _checkpoint("creating-graph")
            opened = []
            for index, editor in enumerate(editors):
                def navigate(file_name, line, column, selected=index):
                    opened.append({"file": str(file_name), "line": int(line), "column": int(column)})
                    stack.setCurrentIndex(selected)
                editor.SetDefinitionCallback(navigate)
            stack.setCurrentIndex(0)
            graph = {
                "schema": "replaylab-shader-flow-graph/v1", "schemaVersion": 1,
                "graphKind": "function-execution", "graphId": "scratch-stage-graph",
                "nodes": [
                    {"id": "double", "name": "Evaluate", "label": "Multiply the input by two",
                     "kind": "function-call", "source": {"line": 2, "column": 3}},
                    {"id": "return", "name": "Evaluate", "label": "Return the intermediate value",
                     "kind": "function-call", "source": {"line": 3, "column": 3}},
                    {"id": "output", "name": "MainPS", "label": "Write the color output",
                     "kind": "function-call", "source": {"line": 6, "column": 3}}],
                "edges": [{"source": "double", "target": "return", "kind": "sequence"},
                          {"source": "return", "target": "output", "kind": "sequence"}]}
            activations = []
            def activate(location):
                activations.append(location)
                stack.setCurrentIndex(0)
                editors[0].GoToLine(location["line"], location["column"])
            panel = ShaderFlowGraphPanel(host, navigate_callback=activate)
            layout.addWidget(panel, 2)
            panel.set_graph(graph, "scratch-stage-graph")
            host.show()
            _checkpoint("first-event-loop")
            _settle(350)
            _checkpoint("fit-checks")
            checks = result["checks"]
            view = panel.view
            checks["firstShowFits"] = _fits(view)
            checks["aspectRatioPreserved"] = abs(view.transform().m11() - view.transform().m22()) < 1e-8
            first_zoom = view._zoom
            host.resize(910, 510)
            _settle()
            checks["resizeFits"] = _fits(view) and abs(first_zoom - view._zoom) > 1e-6
            panel.size_button.click()
            _settle()
            checks["actualSize"] = abs(view._zoom - 1.0) < 1e-8 and not view._auto_fit
            host.resize(1080, 710)
            _settle()
            checks["manualZoomSurvivesResize"] = abs(view._zoom - 1.0) < 1e-8
            panel.set_graph(graph, "scratch-stage-graph", stale=True)
            _settle()
            checks["manualZoomSurvivesRefresh"] = abs(view._zoom - 1.0) < 1e-8 and not view._auto_fit
            panel.fit_button.click()
            _settle()
            checks["fitButtonRestoresAutomaticFit"] = view._auto_fit and _fits(view)
            ys = [view._nodes[node["id"]].pos().y() for node in graph["nodes"]]
            checks["topDown"] = ys[0] < ys[1] < ys[2]
            view._activate_node(view._graph["nodes"][0])
            # Graph navigation is delivered on the next UI turn. Finish it before
            # testing F12 so its callback cannot overwrite the definition cursor.
            _settle()
            checks["algorithmNavigation"] = bool(activations) and editors[0].GetCursorLine() == 2

            widget = editors[0].Widget().findChild(QtWidgets.QWidget, "shaderCodeEditor")
            editors[0].GoToLine(6, SOURCE.splitlines()[5].index("Evaluate") + 2)
            _key(widget, QtCore.Qt.Key_F12)
            _settle()
            checks["f12Function"] = editors[0].GetCursorLine() == 1
            result["functionDefinitionDebug"] = {"cursorLine": editors[0].GetCursorLine(),
                "cursorColumn": editors[0].GetCursorColumn(), "status": widget.toolTip(),
                "navigation": list(opened), "source": editors[0].GetText()}
            editors[0].GoToLine(2, SOURCE.splitlines()[1].index("value") + 2)
            _key(widget, QtCore.Qt.Key_F12)
            _settle()
            checks["f12LastParameter"] = editors[0].GetCursorLine() == 1
            editors[0].GoToLine(6, SOURCE.splitlines()[5].index("ExternalValue") + 2)
            _key(widget, QtCore.Qt.Key_F12)
            _settle()
            checks["unrelatedShaderExcludedFromDefinitions"] = stack.currentIndex() == 0 and editors[0].GetCursorLine() == 6
            included_source = SOURCE + '\n#include "' + os.path.basename(paths[1]) + '"\n'
            editors[0].SetText(included_source)
            editors[0].GoToLine(6, SOURCE.splitlines()[5].index("ExternalValue") + 2)
            _key(widget, QtCore.Qt.Key_F12)
            _settle()
            checks["f12RevealsIncludedDocument"] = stack.currentIndex() == 1 and bool(opened)

            # A suspended capture draft is retained by its owner but is not a navigation
            # candidate for the current capture (ordinary hidden tabs still participate).
            editors[1].SetNavigationEnabled(False)
            stack.setCurrentIndex(0)
            editors[0].GoToLine(6, SOURCE.splitlines()[5].index("ExternalValue") + 2)
            _key(widget, QtCore.Qt.Key_F12)
            _settle()
            checks["suspendedCaptureExcludedFromDefinitions"] = stack.currentIndex() == 0 and editors[0].GetCursorLine() == 6
            editors[1].SetNavigationEnabled(True)

            scoped_source = (
                "// 中文导航与局部作用域\n"
                "float Evaluate(float value) {\n"
                "  float result = value * 2.0;\n"
                "  { float value = 7.0; result += value; }\n"
                "  return result + value;\n"
                "}\n"
                "float Other(float value) {\n"
                "  return value;\n"
                "}\n"
                "float MainPS() { return Evaluate(2.0); }\n")
            editors[0].SetText(scoped_source)
            _settle(300)
            editors[0].GoToLine(2, scoped_source.splitlines()[1].index("value") + 2)
            _key(widget, QtCore.Qt.Key_F12)
            _settle()
            menus = [item for item in QtWidgets.QApplication.allWidgets()
                     if item.objectName() == "shaderReferencesPopup" and item.isVisible()]
            menu = menus[0] if menus else None
            references = ([action for action in menu.actions() if action.isEnabled() and action.data()]
                          if menu is not None else [])
            checks["declarationF12ShowsScopedReferences"] = sorted(int(action.data()) for action in references) == [3, 5]
            result["referenceLabels"] = [action.text() for action in references]
            result["parameterReferenceDebug"] = {"cursorLine": editors[0].GetCursorLine(),
                "cursorColumn": editors[0].GetCursorColumn(),
                "menuCount": len(menus), "allLabels": [action.text() for action in menu.actions()] if menu else [],
                "source": editors[0].GetText()}
            if references:
                references[-1].trigger()
                menu.close()
                _settle()
            checks["referenceChoiceNavigates"] = editors[0].GetCursorLine() in (3, 5)
            back = editors[0].Widget().findChild(QtWidgets.QToolButton, "shaderCodeBack")
            forward = editors[0].Widget().findChild(QtWidgets.QToolButton, "shaderCodeForward")
            back.click()
            checks["codeBackReturnsToDeclaration"] = editors[0].GetCursorLine() == 2
            _settle(220)
            forward.click()
            checks["codeForwardReturnsToReference"] = editors[0].GetCursorLine() in (3, 5)

            editors[0].GoToLine(2, 8)
            _key(widget, QtCore.Qt.Key_F12)
            _settle()
            menus = [item for item in QtWidgets.QApplication.allWidgets()
                     if item.objectName() == "shaderReferencesPopup" and item.isVisible()]
            menu = menus[0] if menus else None
            refs = [action for action in menu.actions() if action.isEnabled() and action.data()] if menu else []
            checks["functionDeclarationShowsCalls"] = editors[0].GetCursorLine() == 10 and not refs
            if menu: menu.close()

            # History stores editor positions that move with intervening text edits.
            editors[0].GoToLine(2, scoped_source.splitlines()[1].index("value") + 2)
            _key(widget, QtCore.Qt.Key_F12)
            _settle(220)
            menus = [item for item in QtWidgets.QApplication.allWidgets()
                     if item.objectName() == "shaderReferencesPopup" and item.isVisible()]
            menu = menus[0] if menus else None
            refs = [action for action in menu.actions() if action.isEnabled() and action.data()] if menu else []
            if refs:
                refs[-1].trigger()
                menu.close()
            editors[0].GoToLine(1, 1)
            _key(widget, QtCore.Qt.Key_Return, text="\n")
            _key(widget, QtCore.Qt.Key_Left, QtCore.Qt.AltModifier)
            checks["historyFollowsInsertedLineAtReference"] = editors[0].GetCursorLine() == 6
            _key(widget, QtCore.Qt.Key_Left, QtCore.Qt.AltModifier)
            checks["historyFollowsInsertedLineAtDeclaration"] = editors[0].GetCursorLine() == 3
            _key(widget, QtCore.Qt.Key_Right, QtCore.Qt.AltModifier)
            checks["forwardHistoryUsesUpdatedPosition"] = editors[0].GetCursorLine() == 6

            comma_source = ("float value;\nfloat G() {\n"
                            "  float first = dot(float2(1, 2), float2(3, 4)), value = 1;\n"
                            "  return value;\n}\n")
            editors[0].SetText(comma_source)
            _settle(300)
            editors[0].GoToLine(4, 11)
            _key(widget, QtCore.Qt.Key_F12)
            checks["commaDeclarationShadowsGlobal"] = editors[0].GetCursorLine() == 3
            _key(widget, QtCore.Qt.Key_F12)
            _settle()
            menus = [item for item in QtWidgets.QApplication.allWidgets()
                     if item.objectName() == "shaderReferencesPopup" and item.isVisible()]
            menu = menus[0] if menus else None
            refs = [action for action in menu.actions() if action.isEnabled() and action.data()] if menu else []
            checks["commaDeclarationReferencesStayLocal"] = editors[0].GetCursorLine() == 4 and not refs
            if menu: menu.close()

            struct_source = ("struct First { float value; };\n"
                             "struct Second { float value; };\n"
                             "First first;\nSecond second;\nfloat Probe() {\n"
                             "  float a = first.value;\n  return a + second.value;\n}\n")
            editors[0].SetText(struct_source)
            _settle(300)
            editors[0].GoToLine(6, struct_source.splitlines()[5].index("value") + 2)
            _key(widget, QtCore.Qt.Key_F12)
            checks["structFieldUsesOwnerType"] = editors[0].GetCursorLine() == 1
            _key(widget, QtCore.Qt.Key_F12)
            _settle()
            menus = [item for item in QtWidgets.QApplication.allWidgets()
                     if item.objectName() == "shaderReferencesPopup" and item.isVisible()]
            menu = menus[0] if menus else None
            refs = [action for action in menu.actions() if action.isEnabled() and action.data()] if menu else []
            checks["structReferencesExcludeOtherOwner"] = editors[0].GetCursorLine() == 6 and not refs
            if menu: menu.close()

            indexed_source = ("struct Light { float3 color; };\n"
                              "Light lights[4];\n"
                              "float3 Probe() { return lights[1 + int(0)].color.xyz; }\n")
            editors[0].SetText(indexed_source)
            for field in ("color", "xyz"):
                editors[0].GoToLine(3, indexed_source.splitlines()[2].index(field) + 2)
                _key(widget, QtCore.Qt.Key_F12)
                checks["indexedStructMember_" + field] = editors[0].GetCursorLine() == 1

            editors[0].SetText(scoped_source)
            marker = editors[0].CreateLineAnchor(3)
            editors[0].GoToLine(1, 1)
            _key(widget, QtCore.Qt.Key_Return, text="\n")
            checks["returnPreservesLfDocument"] = editors[0].GetText() == "\n" + scoped_source
            checks["bookmarkFollowsInsertedLines"] = editors[0].GetLineAnchorLine(marker) == 4
            _key(widget, QtCore.Qt.Key_Backspace)
            checks["bookmarkFollowsDeletedPrecedingLine"] = editors[0].GetLineAnchorLine(marker) == 3
            editors[0].GoToLine(3, 8)
            _key(widget, QtCore.Qt.Key_Space, text=" ")
            checks["bookmarkSurvivesTargetTextEdit"] = editors[0].GetLineAnchorLine(marker) == 3
            editors[0].GoToLine(3, 1)
            # This Scintilla Qt version reads global keyboard modifiers, so a synthetic
            # Shift+Down cannot establish a selection. Ordinary Delete key events remove
            # the exact line including its terminator without faking a modifier state.
            removed_line = editors[0].GetText().splitlines(True)[2]
            for unused in removed_line:
                _key(widget, QtCore.Qt.Key_Delete)
            checks["targetLineActuallyDeleted"] = removed_line.strip() not in editors[0].GetText()
            checks["deletedTargetInvalidatesBookmark"] = editors[0].GetLineAnchorLine(marker) == 0
            result["deleteTargetDebug"] = {"anchorLine": editors[0].GetLineAnchorLine(marker), "source": editors[0].GetText()}
            marker = editors[0].CreateLineAnchor(2)
            editors[0].SetText(scoped_source)
            checks["fullReloadInvalidatesOldBookmarks"] = editors[0].GetLineAnchorLine(marker) == 0
            editors[0].SetText("first\nlast")
            marker = editors[0].CreateLineAnchor(2)
            editors[0].GoToLine(2, 5)
            for unused in "last\n":
                _key(widget, QtCore.Qt.Key_Backspace)
            checks["eofLineActuallyDeleted"] = editors[0].GetText() == "first"
            checks["deletedEofLineInvalidatesBookmark"] = editors[0].GetLineAnchorLine(marker) == 0
            result["deleteEofDebug"] = {"anchorLine": editors[0].GetLineAnchorLine(marker), "source": editors[0].GetText()}
            editors[0].SetText("first\r\nlast\r\n")
            editors[0].GoToLine(1, 1)
            _key(widget, QtCore.Qt.Key_Return, text="\n")
            checks["returnPreservesCrLfDocument"] = editors[0].GetText() == "\r\nfirst\r\nlast\r\n"
            declarations = ("float value;\nfloat F(float value);\n"
                            "float G() {\n  for(int value = 0; value < 2; ++value) {}\n"
                            "  return value;\n}\n")
            editors[0].SetText(declarations)
            editors[0].GoToLine(5, 11)
            _key(widget, QtCore.Qt.Key_F12)
            _settle()
            checks["prototypeAndForParameterDoNotLeak"] = editors[0].GetCursorLine() == 1
            editors[0].SetText(scoped_source)
            save_requests = []
            editors[0].SetSaveCallback(lambda: save_requests.append(str(editors[0].GetText())))
            editors[0].GoToLine(1, 1)
            _key(widget, QtCore.Qt.Key_Space, text=" ")
            _key(widget, QtCore.Qt.Key_S, QtCore.Qt.ControlModifier)
            checks["nativeEditingAndSaveCallback"] = bool(save_requests) and save_requests[-1].startswith(" //")
            result["nativeSaveScope"] = "scratch callback only; real Host save tested separately"

            stack.setCurrentIndex(0)
            editors[0].SetText("float customValue;\nfloat customVector;\nfloat4 MainPS() : SV_Target { return cus; }\n")
            _settle(300)
            editors[0].GoToLine(3, len("float4 MainPS() : SV_Target { return cus") + 1)
            QtWidgets.QApplication.setActiveWindow(host)
            widget.setFocus()
            _key(widget, QtCore.Qt.Key_Space, QtCore.Qt.ControlModifier)
            _settle()
            completion_debug = []
            result["completionBeforeAccept"] = editors[0].GetText()
            for candidate in QtWidgets.QApplication.allWidgets():
                # Scintilla's C++ subclass is wrapped as QWidget by embedded
                # PySide2. Qt metaobject properties still expose the real list.
                if candidate.inherits("QListWidget"):
                    completion_debug.append({"visible": candidate.isVisible(),
                                             "count": int(candidate.property("count") or 0)})
            result["completionDebug"] = completion_debug
            checks["completionPopupShowsChoices"] = any(
                value["visible"] and value["count"] >= 2 for value in completion_debug)
            _key(widget, QtCore.Qt.Key_Tab)
            _settle()
            checks["completionInsertsDeclaredVariable"] = "return customValue;" in editors[0].GetText()
            result["completionSource"] = editors[0].GetText()
            _key(widget, QtCore.Qt.Key_Escape)

            editors[0].SetText("float4 MainPS() : SV_Target {\n  return float4(1, 0, 0, 1);\n")
            _settle(350)
            summary = editors[0].Widget().findChild(QtWidgets.QToolButton, "shaderCodeDiagnostics")
            checks["editingErrorsVisible"] = summary is not None and not summary.isHidden()
            result["localDiagnosticSummary"] = summary.text() if summary else ""
            editors[0].SetText(SOURCE)
            _settle(300)
            problem = qrd.ShaderEditorDiagnostic()
            problem.line, problem.column, problem.length = 6, 10, 6
            problem.severity = qrd.ShaderEditorDiagnosticSeverity.Error
            problem.message = "Scratch compiler diagnostic at the output statement"
            problem.source = "scratch compiler UI fixture"
            editors[0].SetDiagnostics([problem])
            _settle()
            checks["compilerDiagnosticVisible"] = not summary.isHidden()
            summary.click()
            checks["compilerDiagnosticNavigates"] = editors[0].GetCursorLine() == 6
            # A real keystroke invalidates old source-bound compiler diagnostics.
            editors[0].GoToLine(7, 2)
            _key(widget, QtCore.Qt.Key_Space, text=" ")
            _settle(350)
            checks["oldCompilerDiagnosticClearsOnEdit"] = summary.isHidden()
            result["clearedDiagnosticDebug"] = {"source": editors[0].GetText(), "summary": summary.text(), "details": summary.toolTip()}
            editors[0].SetText(SOURCE)
            _settle(300)
            editors[0].GoToLine(2, SOURCE.splitlines()[1].index("doubled") + 2)
            panel.fit_button.click()
            _settle()
            if output:
                image_path = os.path.splitext(output)[0] + ".png"
                host.grab().save(image_path)
                result["screenshot"] = image_path
            result["ok"] = all(checks.values())
        except Exception as error:
            result.update(ok=False, error="%s: %s" % (type(error).__name__, error))
        finally:
            for editor in editors:
                if editor is not None:
                    editor.ClearCallbacks()
                    context.DestroyShaderCodeEditor(editor)
            if host is not None:
                host.close()
                host.deleteLater()
        if output:
            with open(output, "w", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
        print("REPLAYLAB_SHADER_IDE_SMOKE=" + json.dumps(result, ensure_ascii=False))


runner = _Runner()
bootstrap_output = os.environ.get("REPLAYLAB_SHADER_IDE_SMOKE_OUTPUT", "")
if bootstrap_output:
    with open(bootstrap_output, "w", encoding="utf-8") as stream:
        json.dump({"ok": False, "stage": "waiting-for-ui-callback"}, stream)
bridge = sys.modules.get("renderdoc_mcp_bridge")
if bridge is not None:
    bridge._runtime_shader_ide_smoke = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.run)
