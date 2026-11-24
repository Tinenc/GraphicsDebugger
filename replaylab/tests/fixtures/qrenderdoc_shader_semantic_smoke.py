"""Run in an isolated --ui-python instance with the bundled semantic provider.

REPLAYLAB_SHADER_SEMANTIC_OUTPUT is an absolute receipt path. No RDC is opened,
and only operating-system temporary HLSL fixtures are created.
"""
from __future__ import print_function
import json
import os
import sys
import tempfile
import time
from PySide2 import QtCore, QtGui, QtWidgets


SOURCE = '''#include "shared.hlsli"
float Choose(float value) { return value; }
int Choose(int value) { return value + 1; }
struct First { float amount; };
struct Second { float amount; };
float4 MainPS(float4 input : TEXCOORD0) : SV_Target0
{
    float shared = input.x;
    First first;
    Second second;
    first.amount = 2.0;
    second.amount = 3.0;
    float total = Choose(shared);
    int integerTotal = Choose(3);
    {
        float shared = input.y;
        total += Included(shared);
    }
    return total + shared + first.amount + second.amount + integerTotal;
}
'''


def settle(milliseconds=100):
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(milliseconds, loop.quit)
    loop.exec_()


def run():
    receipt = dict(checks={}, captureMutated=False)
    directory = tempfile.TemporaryDirectory(prefix="bread-native-semantic-")
    host = None
    editors = []
    context = getattr(sys.modules.get("renderdoc_mcp_bridge"), "_ctx", None)
    try:
        root = directory.name
        paths = [os.path.join(root, name) for name in ("main.hlsl", "shared.hlsli", "unrelated.hlsl")]
        sources = [SOURCE, "float Included(float value) { return value * 2.0; }\n",
                   "float shared; float Included(float x) { return -x; }\n"]
        preview_directory = os.path.join(root, "ai", "previews")
        os.makedirs(preview_directory)
        paths.append(os.path.join(preview_directory, "frozen.hlsl"))
        sources.append(SOURCE)
        for path, source in zip(paths, sources):
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(source)
        host = QtWidgets.QWidget()
        host.setWindowTitle("Bread semantic navigation verification")
        host.resize(1100, 700)
        stack = QtWidgets.QStackedWidget(host)
        QtWidgets.QVBoxLayout(host).addWidget(stack)
        for path, source in zip(paths, sources):
            editor = context.CreateShaderCodeEditor(stack)
            editors.append(editor)
            stack.addWidget(editor.Widget())
            editor.SetFileName(path)
            if path == paths[3]:
                editor.SetIncludeBaseDirectory(root)
            editor.SetLanguage("hlsl")
            editor.SetReadOnly(False)
            editor.SetText(source)
        callbacks = []
        for index, editor in enumerate(editors):
            def navigate(path, line, column, selected=index):
                callbacks.append(dict(file=str(path), line=int(line), column=int(column)))
                stack.setCurrentIndex(selected)
            editor.SetDefinitionCallback(navigate)
        host.show()
        settle(2000)
        widget = editors[0].Widget().findChild(QtWidgets.QWidget, "shaderCodeEditor")

        def f12(name, needle, expected_line, offset=0, expected_editor=0, source=None,
                source_editor=0):
            code = editors[source_editor].GetText() if source is None else source
            start = code.index(needle) + offset
            prefix = code[:start]
            stack.setCurrentIndex(source_editor)
            editors[source_editor].GoToLine(prefix.count("\n") + 1, len(prefix.rsplit("\n", 1)[-1]) + 2)
            source_widget = editors[source_editor].Widget().findChild(QtWidgets.QWidget, "shaderCodeEditor")
            QtWidgets.QApplication.sendEvent(source_widget, QtGui.QKeyEvent(
                QtCore.QEvent.KeyPress, QtCore.Qt.Key_F12, QtCore.Qt.NoModifier))
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                settle(50)
                if stack.currentIndex() == expected_editor and editors[expected_editor].GetCursorLine() == expected_line:
                    break
            receipt["checks"][name] = (stack.currentIndex() == expected_editor and
                                       editors[expected_editor].GetCursorLine() == expected_line)
            receipt.setdefault("details", {})[name] = dict(
                editor=stack.currentIndex(), line=editors[stack.currentIndex()].GetCursorLine(), status=source_widget.toolTip())

        f12("floatOverload", "Choose(shared)", 2)
        f12("intOverload", "Choose(3)", 3)
        f12("includeIsolation", "Included(shared)", 1, expected_editor=1)
        f12("shadowedLocal", "Included(shared)", 16, len("Included("))
        f12("outerLocal", "total + shared", 8, len("total + "))
        f12("memberOwner", "first.amount +", 4, len("first."))
        editors[0].SetText(SOURCE.replace("float shared = input.x;", "int shared = (int)input.x;"))
        f12("unsavedOverload", "Choose(shared)", 3)
        # A local declaration has exactly one semantic use in its inner scope. The
        # bounded compiler-confirmed reference route must exclude the outer name.
        f12("declarationFindsOnlyInnerReference", "shared = input.y", 17)
        # The frozen preview lives under ai/previews, while includes belong to the
        # original source directory. Its float overload and the canonical buffer's
        # unsaved int overload must coexist without replacing either LSP overlay.
        f12("relocatedPreviewIncludesOriginalHeader", "Included(shared)", 1,
            expected_editor=1, source_editor=3)
        f12("previewKeepsIndependentOverlay", "Choose(shared)", 2,
            expected_editor=3, source_editor=3)
        f12("canonicalOverlaySurvivesPreview", "Choose(shared)", 3)
        preview_callbacks = [item for item in callbacks if item["file"] == paths[3]]
        receipt["checks"]["previewCallbackKeepsRealIdentity"] = bool(preview_callbacks)
        receipt["checks"]["includeProjectionPreservesBuffer"] = editors[3].GetText() == SOURCE
        receipt["checks"]["includeProjectionDoesNotCreateFiles"] = not any(
            name.startswith(".bread-editor-") for name in os.listdir(root))
        receipt["checks"]["includeProjectionPreservesDiskSources"] = all(
            open(path, encoding="utf-8").read() == source for path, source in zip(paths, sources))
        macro_source = '#define HEADER "shared.hlsli"\n' + SOURCE.replace(
            '#include "shared.hlsli"', '#include HEADER')
        editors[3].SetText(macro_source)
        start = macro_source.index("Included(shared)")
        prefix = macro_source[:start]
        stack.setCurrentIndex(3)
        editors[3].GoToLine(prefix.count("\n") + 1, len(prefix.rsplit("\n", 1)[-1]) + 2)
        preview_widget = editors[3].Widget().findChild(QtWidgets.QWidget, "shaderCodeEditor")
        previous_callbacks = len(callbacks)
        QtWidgets.QApplication.sendEvent(preview_widget, QtGui.QKeyEvent(
            QtCore.QEvent.KeyPress, QtCore.Qt.Key_F12, QtCore.Qt.NoModifier))
        settle(300)
        status = preview_widget.toolTip()
        receipt["checks"]["relocatedMacroIncludeReportsBoundary"] = (
            len(callbacks) == previous_callbacks and
            ("macro includes" in status or "宏形式 include" in status))
        receipt["callbacks"] = callbacks
        receipt["ok"] = all(receipt["checks"].values())
    except Exception as exc:
        receipt.update(ok=False, error=str(exc))
    finally:
        for editor in editors:
            try:
                editor.ClearCallbacks()
                context.DestroyShaderCodeEditor(editor)
            except Exception:
                pass
        if host is not None:
            host.close()
            host.deleteLater()
        directory.cleanup()
        output = os.environ.get("REPLAYLAB_SHADER_SEMANTIC_OUTPUT", "")
        if output:
            with open(output, "w", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False, indent=2)


pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(run)
