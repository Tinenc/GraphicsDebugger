from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


REPLAYLAB_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = REPLAYLAB_ROOT.parent

INTERFACE_HEADER = REPOSITORY_ROOT / "qrenderdoc" / "Code" / "Interface" / "QRDInterface.h"
CAPTURE_CONTEXT_HEADER = REPOSITORY_ROOT / "qrenderdoc" / "Code" / "CaptureContext.h"
CAPTURE_CONTEXT_SOURCE = REPOSITORY_ROOT / "qrenderdoc" / "Code" / "CaptureContext.cpp"
NATIVE_HEADER = REPOSITORY_ROOT / "qrenderdoc" / "Widgets" / "ShaderCodeEditor.h"
NATIVE_SOURCE = REPOSITORY_ROOT / "qrenderdoc" / "Widgets" / "ShaderCodeEditor.cpp"
PANEL_SOURCE = REPLAYLAB_ROOT / "renderdoc_mcp_bridge" / "project_shader_workspace.py"
SWIG_INTERFACE = REPOSITORY_ROOT / "qrenderdoc" / "Code" / "pyrenderdoc" / "qrenderdoc.i"
PYTHON_CONTEXT_SOURCE = SWIG_INTERFACE.with_name("PythonContext.cpp")
QMAKE_PROJECT = REPOSITORY_ROOT / "qrenderdoc" / "qrenderdoc.pro"
MSBUILD_PROJECT = REPOSITORY_ROOT / "qrenderdoc" / "qrenderdoc_local.vcxproj"
MSBUILD_FILTERS = REPOSITORY_ROOT / "qrenderdoc" / "qrenderdoc_local.vcxproj.filters"
CMAKE_PROJECT = REPOSITORY_ROOT / "qrenderdoc" / "CMakeLists.txt"
SHADER_VIEWER_SOURCE = REPOSITORY_ROOT / "qrenderdoc" / "Windows" / "ShaderViewer.cpp"


def _read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError("required native editor bridge file is missing: %s" % path)
    return path.read_text(encoding="utf-8")


def _cpp_type_body(source: str, type_name: str) -> str:
    declaration = re.search(
        r"\b(?:class|struct)\s+(?:\w+\s+)*" + re.escape(type_name) + r"\b[^;{]*\{",
        source,
    )
    if declaration is None:
        raise AssertionError("could not find C++ type %s" % type_name)

    opening = source.find("{", declaration.start())
    depth = 0
    for offset in range(opening, len(source)):
        character = source[offset]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1:offset]
    raise AssertionError("C++ type %s has no matching closing brace" % type_name)


def _cpp_function_body(source: str, declaration_pattern: str) -> str:
    declaration = re.search(declaration_pattern, source)
    if declaration is None:
        raise AssertionError("could not find C++ function matching %s" % declaration_pattern)

    opening = source.find("{", declaration.end())
    if opening < 0:
        raise AssertionError("C++ function has no body: %s" % declaration_pattern)
    depth = 0
    for offset in range(opening, len(source)):
        character = source[offset]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1:offset]
    raise AssertionError("C++ function has no matching closing brace: %s" % declaration_pattern)


def _parse_python(path: Path) -> ast.Module:
    return ast.parse(
        _read(path), filename=str(path), feature_version=(3, 6)
    )


def _panel_class(tree: ast.Module) -> ast.ClassDef:
    candidates = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and node.name == "ReplayLabShaderWorkspacePanel"
    ]
    if not candidates:
        raise AssertionError("ReplayLabShaderWorkspacePanel is missing")
    # The no-Qt fallback intentionally has the same class name but only a tiny
    # defensive constructor. The real panel has by far the largest body.
    return max(candidates, key=lambda node: len(node.body))


def _panel_methods(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    panel = _panel_class(tree)
    methods = {
        node.name: node
        for node in panel.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return methods


def _source_segment(path: Path, node: ast.AST) -> str:
    source = _read(path)
    segment = ast.get_source_segment(source, node)
    if segment is None:
        raise AssertionError("could not recover source for AST node")
    return segment


def _attribute_calls(node: ast.AST) -> set[str]:
    return {
        item.func.attr
        for item in ast.walk(node)
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
    }


class ShaderNativeEditorInterfaceContractTests(unittest.TestCase):
    def test_swig_interface_is_narrow_and_owns_no_python_qt_subclass(self):
        interface = _read(INTERFACE_HEADER)
        editor = _cpp_type_body(interface, "IShaderCodeEditor")

        required_operations = (
            r"virtual\s+QWidget\s*\*\s*Widget\s*\(",
            r"virtual\s+rdcstr\s+GetText\s*\(",
            r"virtual\s+void\s+SetText\s*\(",
            r"virtual\s+void\s+SetReadOnly\s*\(",
            r"virtual\s+bool\s+IsReadOnly\s*\(",
            r"virtual\s+void\s+SetFileName\s*\(",
            r"virtual\s+void\s+SetLanguage\s*\(",
            r"virtual\s+void\s+GoToLine\s*\(",
            r"virtual\s+bool\s+IsModified\s*\(",
            r"virtual\s+void\s+SetModified\s*\(",
            r"virtual\s+void\s+SetSavePoint\s*\(",
            r"virtual\s+void\s+SetDiagnostics\s*\(",
        )
        for operation in required_operations:
            with self.subTest(operation=operation):
                self.assertIsNotNone(
                    re.search(operation, editor),
                    "IShaderCodeEditor is missing operation matching %s" % operation,
                )

        # Text changes keep the dirty/save model live, while save and external
        # definition navigation must return to the Python workspace controller.
        for callback_role in ("text", "save", "chat", "definition"):
            with self.subTest(callback_role=callback_role):
                callback_pattern = (
                    r"(?is)(?:%s).*?(?:callback|std::function)|"
                    r"(?:callback|std::function).*?(?:%s)"
                    % (callback_role, callback_role)
                )
                self.assertIsNotNone(
                    re.search(callback_pattern, editor),
                    "IShaderCodeEditor is missing the %s callback role"
                    % callback_role,
                )

        self.assertIsNotNone(
            re.search(
                r"DECLARE_REFLECTION_STRUCT\s*\(\s*IShaderCodeEditor\s*\)",
                interface,
            ),
            "IShaderCodeEditor must be exposed through the QRDInterface reflection/SWIG path",
        )

        context = _cpp_type_body(interface, "ICaptureContext")
        self.assertIsNotNone(
            re.search(
                r"IShaderCodeEditor\s*\*\s*CreateShaderCodeEditor\s*\(",
                context,
            ),
            "ICaptureContext must create native shader editors",
        )
        self.assertIsNotNone(
            re.search(
                r"void\s+DestroyShaderCodeEditor\s*\(\s*IShaderCodeEditor\s*\*",
                context,
            ),
            "ICaptureContext must destroy native shader editors",
        )

    def test_widget_implements_the_interface_with_shared_editor_services(self):
        header = _read(NATIVE_HEADER)
        source = _read(NATIVE_SOURCE)

        self.assertIsNotNone(
            re.search(
                r"class\s+ShaderCodeEditor\s*:\s*public\s+QWidget\s*,\s*"
                r"public\s+IShaderCodeEditor",
                header,
            ),
            "ShaderCodeEditor must implement QWidget and IShaderCodeEditor",
        )
        editor = _cpp_type_body(header, "ShaderCodeEditor")
        self.assertIn("Q_OBJECT", editor)
        for operation in (
            "Widget", "GetText", "SetText", "SetReadOnly", "IsReadOnly",
            "SetFileName", "SetLanguage", "GoToLine", "IsModified",
            "SetModified", "SetSavePoint", "SetDiagnostics",
        ):
            with self.subTest(operation=operation):
                self.assertIsNotNone(
                    re.search(r"\b%s\s*\(" % operation, editor),
                    "ShaderCodeEditor is missing %s" % operation,
                )

        self.assertIn("CodeEditorService", source)
        self.assertIn("ScintillaEdit", source)
        self.assertIn("registerEditor", source)
        self.assertNotIn("QPlainTextEdit", header + source)

    def test_capture_context_is_the_single_native_owner(self):
        header = _read(CAPTURE_CONTEXT_HEADER)
        source = _read(CAPTURE_CONTEXT_SOURCE)
        context = _cpp_type_body(header, "CaptureContext")

        self.assertIn("CreateShaderCodeEditor", context)
        self.assertIn("DestroyShaderCodeEditor", context)

        create = _cpp_function_body(
            source,
            r"IShaderCodeEditor\s*\*\s*CaptureContext::CreateShaderCodeEditor\s*\([^)]*\)",
        )
        destroy = _cpp_function_body(
            source,
            r"void\s+CaptureContext::DestroyShaderCodeEditor\s*\([^)]*\)",
        )
        self.assertIn("new ShaderCodeEditor", create)
        self.assertIn("ShaderEditor_LanguageServer", create)
        self.assertIn("ClearCallbacks", destroy)
        self.assertRegex(destroy, r"\bdelete\b")

    def test_external_definition_tabs_never_enter_the_compile_payload(self):
        source = _read(SHADER_VIEWER_SOURCE)
        self.assertIn('setProperty("shaderNavigationOnly", true)', source)

        current_files = _cpp_function_body(
            source,
            r"rdcstrpairs\s+ShaderViewer::GetCurrentFileContents\s*\(\s*\)",
        )
        refresh = _cpp_function_body(
            source,
            r"void\s+ShaderViewer::on_refresh_clicked\s*\(\s*\)",
        )
        reset = _cpp_function_body(
            source,
            r"void\s+ShaderViewer::on_resetEdits_clicked\s*\(\s*\)",
        )
        for body in (current_files, refresh, reset):
            self.assertIn('property("shaderNavigationOnly").toBool()', body)

    def test_native_editor_deletion_invalidates_borrowed_qwidget_on_both_paths(self):
        source = _read(CAPTURE_CONTEXT_SOURCE)
        destroy = _cpp_function_body(
            source, r"void\s+CaptureContext::DestroyShaderCodeEditor\s*\([^)]*\)"
        )
        shutdown = _cpp_function_body(
            source, r"CaptureContext::~CaptureContext\s*\(\s*\)"
        )
        for body, editor in ((destroy, "nativeEditor"), (shutdown, "editor")):
            with self.subTest(editor=editor):
                clear = body.index(editor + "->ClearCallbacks()")
                invalidate = body.index(
                    "PythonContext::InvalidateNativeQtObject(" + editor + ")"
                )
                deletion = body.index("delete " + editor + ";")
                self.assertLess(clear, invalidate)
                self.assertLess(invalidate, deletion)
        # The embedded installation links libshiboken but need not provide its
        # separate Python module. Cleanup must not depend on that import.
        self.assertNotRegex(_read(PANEL_SOURCE), r"(?:from|import)\s+shiboken2\b")

    def test_native_wrapper_invalidation_pins_borrowed_reference_under_gil(self):
        body = _cpp_function_body(
            _read(PYTHON_CONTEXT_SOURCE),
            r"void\s+PythonContext::InvalidateNativeQtObject\s*\([^)]*\)",
        )
        self.assertIn("#if PYSIDE2_ENABLED", body)
        self.assertIn("!initialised()", body)
        self.assertIn("!Py_IsInitialized()", body)
        steps = (
            "PyGILState_Ensure()",
            "retrieveWrapper(object)",
            "Py_INCREF(",
            "Shiboken::Object::releaseOwnership(wrapper)",
            "Shiboken::Object::invalidate(wrapper)",
            "Shiboken::Object::removeParent(wrapper, false)",
            "Py_DECREF(",
            "PyGILState_Release(gil)",
        )
        positions = [body.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("Shiboken::Object::destroy(", body)
        self.assertNotRegex(body, r"\bdelete\s+object\b")
        self.assertNotIn("object->parent()", body)

    def test_qwidget_typemap_and_every_supported_project_include_the_bridge(self):
        swig = _read(SWIG_INTERFACE)
        self.assertIn('%include "Code/Interface/QRDInterface.h"', swig)
        self.assertRegex(swig, r"%typemap\s*\(out\)\s*QWidget\s*\*")
        self.assertRegex(
            swig,
            r"TEMPLATE_ARRAY_INSTANTIATE\s*\(\s*rdcarray\s*,\s*"
            r"ShaderEditorDiagnostic\s*\)",
        )

        qmake = _read(QMAKE_PROJECT).replace("\\", "/")
        msbuild = _read(MSBUILD_PROJECT).replace("\\", "/")
        filters = _read(MSBUILD_FILTERS).replace("\\", "/")
        for manifest_name, project in (
                ("qmake", qmake), ("msbuild", msbuild), ("filters", filters)):
            with self.subTest(project=manifest_name):
                self.assertTrue(
                    "Widgets/ShaderCodeEditor.cpp" in project,
                    "build manifest is missing Widgets/ShaderCodeEditor.cpp",
                )
                self.assertTrue(
                    "Widgets/ShaderCodeEditor.h" in project,
                    "build manifest is missing Widgets/ShaderCodeEditor.h",
                )

        # CMake delegates the actual qrenderdoc source list to qmake, but its
        # Xcode dependency/source presentation must still discover the widget.
        cmake = _read(CMAKE_PROJECT).replace("\\", "/")
        self.assertIsNotNone(
            re.search(
                r"GLOB_RECURSE\s+WIDGETS_FILES\s+Widgets/\*\.h\s+Widgets/\*\.cpp",
                cmake,
            ),
            "CMake's Xcode source discovery must include Widgets/*.h and Widgets/*.cpp",
        )


class ShaderWorkspaceNativeAdapterContractTests(unittest.TestCase):
    def test_panel_uses_the_native_interface_instead_of_qplaintextedit(self):
        tree = _parse_python(PANEL_SOURCE)
        methods = _panel_methods(tree)
        opener = methods.get("_open_workspace")
        self.assertIsNotNone(opener)
        opener_source = _source_segment(PANEL_SOURCE, opener)

        self.assertIn("CreateShaderCodeEditor", opener_source)
        self.assertIn(".Widget()", opener_source)
        self.assertNotIn("_HLSLEditor(", opener_source)
        self.assertNotIn("QPlainTextEdit", opener_source)

        # The panel controller must use the SWIG adapter rather than reaching
        # through QWidget() to QPlainTextEdit-only APIs. This keeps all IDE
        # behavior in the native Scintilla-backed widget.
        forbidden_qplaintextedit_calls = {
            "toPlainText", "setPlainText", "document", "textCursor",
            "setTextCursor", "setExtraSelections",
        }
        panel_calls = set()
        for method in methods.values():
            panel_calls.update(_attribute_calls(method))
        self.assertEqual(set(), panel_calls & forbidden_qplaintextedit_calls)

        panel_source = _source_segment(PANEL_SOURCE, _panel_class(tree))
        for operation in (
            "GetText", "SetText", "SetReadOnly", "GoToLine", "SetModified",
            "SetSavePoint", "SetDiagnostics",
        ):
            with self.subTest(operation=operation):
                self.assertIn(operation, panel_source)

    def test_close_removes_the_widget_before_destroying_native_ownership(self):
        tree = _parse_python(PANEL_SOURCE)
        methods = _panel_methods(tree)
        close = methods.get("_close_document")
        self.assertIsNotNone(close)
        close_source = _source_segment(PANEL_SOURCE, close)

        destroy_methods = {
            name: _source_segment(PANEL_SOURCE, method)
            for name, method in methods.items()
            if "DestroyShaderCodeEditor" in _source_segment(PANEL_SOURCE, method)
        }
        self.assertTrue(
            destroy_methods,
            "the panel must release native editor ownership through CaptureContext",
        )

        verified_order = False
        if "DestroyShaderCodeEditor" in close_source:
            verified_order = (
                "removeWidget" in close_source
                and close_source.index("removeWidget")
                < close_source.index("DestroyShaderCodeEditor")
            )
            self.assertNotIn("deleteLater", close_source)
        else:
            for helper_name, helper_source in destroy_methods.items():
                helper_call = "self.%s(" % helper_name
                if helper_call not in close_source:
                    continue
                if "removeWidget" in helper_source:
                    verified_order = (
                        helper_source.index("removeWidget")
                        < helper_source.index("DestroyShaderCodeEditor")
                    )
                elif "removeWidget" in close_source:
                    verified_order = (
                        close_source.index("removeWidget")
                        < close_source.index(helper_call)
                    )
                self.assertNotIn("deleteLater", helper_source)
                if verified_order:
                    break

        self.assertTrue(
            verified_order,
            "removeWidget must happen before DestroyShaderCodeEditor",
        )


if __name__ == "__main__":
    unittest.main()
