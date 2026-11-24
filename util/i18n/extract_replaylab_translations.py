#!/usr/bin/env python3
"""Extract ReplayLab Python UI messages into an lupdate-readable C++ registry."""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import sys


PROJECT_CONTEXT = "ReplayLabProjects"
INIT_SCOPE_BEGIN = "replaylab-project-i18n-begin"
INIT_SCOPE_END = "replaylab-project-i18n-end"
VISIBLE_CALLS = {
    "QAction", "QLabel", "QLineEdit", "QListWidgetItem", "QMenu",
    "QPushButton", "QCheckBox", "QRadioButton", "QGroupBox",
    "QTableWidgetItem", "QTreeWidgetItem", "QToolButton",
    "setWindowTitle", "setText", "setToolTip", "setStatusTip",
    "setWhatsThis", "setPlaceholderText", "setTitle", "setTabText",
    "setItemText", "setLabelText", "setAccessibleName",
    "setAccessibleDescription", "setHeaderLabel", "setHeaderLabels",
    "setHorizontalHeaderLabels", "setVerticalHeaderLabels", "addAction",
    "addItem", "addItems", "insertItem", "insertItems", "addRow", "addTab",
    "getExistingDirectory", "getOpenFileNames", "getOpenFileName",
    "getSaveFileName", "warning", "critical", "question", "information",
    "ErrorDialog", "MessageDialog", "RegisterWindowMenu", "showMessage",
    "_show_project_background_status", "drawText",
}
FORMAT_TOKEN_RE = re.compile(r"%(?:[1-9][0-9]*|[sdf])")
FORMAT_PUNCTUATION_RE = re.compile(r"^[\s\\n:/.()'\"+*|%\-–—·×]*$")
I18N_IGNORE_RE = re.compile(r"#\s*i18n-ignore:\s*\S.{3,}")
CJK_SOURCE_RE = re.compile(
    "["
    "\u1100-\u11ff"  # Hangul Jamo
    "\u2e80-\u303f"  # CJK radicals, description characters, and punctuation
    "\u3040-\u30ff"  # Hiragana and Katakana
    "\u3100-\u318f"  # Bopomofo and Hangul compatibility Jamo
    "\u31a0-\u31bf"  # Bopomofo extended
    "\u31c0-\u31ef"  # CJK strokes
    "\u31f0-\u31ff"  # Katakana phonetic extensions
    "\u3200-\u33ff"  # Enclosed CJK letters and compatibility characters
    "\u3400-\u4dbf"  # CJK unified ideographs extension A
    "\u4e00-\u9fff"  # CJK unified ideographs
    "\ua960-\ua97f"  # Hangul Jamo extended A
    "\uac00-\ud7af"  # Hangul syllables and Jamo extended B
    "\uf900-\ufaff"  # CJK compatibility ideographs
    "\ufe30-\ufe4f"  # CJK compatibility forms
    "\uff00-\uffef"  # Full-width and half-width forms
    "\U00020000-\U0002fa1f"  # CJK extensions B-F and compatibility supplement
    "\U00030000-\U000323af"  # CJK extensions G-H
    "]"
)
REGISTRY_ENTRY_RE = re.compile(
    r'^\s*QT_TRANSLATE_NOOP\('
    r'(?P<context>"(?:\\.|[^"\\])*"),\s*'
    r'(?P<source>"(?:\\.|[^"\\])*")\),\s*$'
)
TECHNICAL_UI_LITERALS = {
    "RDC", "d3d11", "d3d12", "vulkan", "opengl", "windows-x64", "● ", "⚠ ",
}
LEXICAL_SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def call_name(node: ast.Call) -> str:
    function = node.func
    if isinstance(function, ast.Attribute):
        return function.attr
    if isinstance(function, ast.Name):
        return function.id
    return ""


def is_translate_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "translate"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "QCoreApplication"
    )


def constant_text(node: ast.AST) -> str | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, str) else None


def is_format_only(text: str) -> bool:
    return bool(FORMAT_PUNCTUATION_RE.fullmatch(FORMAT_TOKEN_RE.sub("", text)))


def scoped_lines(path: pathlib.Path, source_lines: list[str]) -> tuple[tuple[int, int], list[str]]:
    if path.name != "__init__.py":
        return (1, max(1, len(source_lines))), []
    starts = [index for index, line in enumerate(source_lines, 1) if INIT_SCOPE_BEGIN in line]
    ends = [index for index, line in enumerate(source_lines, 1) if INIT_SCOPE_END in line]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        return (1, 0), [
            f"{path}: ReplayLab project UI scope markers are missing or ambiguous"
        ]
    return (starts[0] + 1, ends[0] - 1), []


def in_scope(node: ast.AST, line_range: tuple[int, int]) -> bool:
    return line_range[0] <= getattr(node, "lineno", 0) <= line_range[1]


def lexical_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, LEXICAL_SCOPES):
            return current
    return None


def binding_key(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return "self." + node.attr
    return None


def build_bindings(
    tree: ast.AST, parents: dict[ast.AST, ast.AST]
) -> dict[ast.AST, dict[str, list[ast.AST]]]:
    bindings: dict[ast.AST, dict[str, list[ast.AST]]] = {}
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        if value is None:
            continue
        owner = lexical_scope(node, parents)
        if owner is None:
            owner = tree
        for target in targets:
            key = binding_key(target)
            if key:
                bindings.setdefault(owner, {}).setdefault(key, []).append(value)
    return bindings


def binding_values(
    key: str,
    owner: ast.AST | None,
    parents: dict[ast.AST, ast.AST],
    bindings: dict[ast.AST, dict[str, list[ast.AST]]],
) -> list[ast.AST]:
    current = owner
    while current is not None:
        values = bindings.get(current, {}).get(key)
        if values:
            return values
        parent = parents.get(current)
        current = parent if isinstance(parent, LEXICAL_SCOPES) else lexical_scope(current, parents)
    return []


def visible_string_nodes(
    node: ast.AST,
    owner: ast.AST | None,
    parents: dict[ast.AST, ast.AST],
    bindings: dict[ast.AST, dict[str, list[ast.AST]]],
    resolving: set[tuple[int, str]] | None = None,
):
    if is_translate_call(node):
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node
        return
    if isinstance(node, ast.Subscript):
        # The subscript result is runtime data; its key is never displayed.
        return
    if isinstance(node, ast.IfExp):
        # Only the selected value becomes UI text, never the branch condition.
        yield from visible_string_nodes(node.body, owner, parents, bindings, resolving)
        yield from visible_string_nodes(node.orelse, owner, parents, bindings, resolving)
        return
    if isinstance(node, ast.Call):
        name = call_name(node)
        if name == "get":
            # Mapping key is data. A literal default can still become UI text.
            for argument in node.args[1:]:
                yield from visible_string_nodes(
                    argument, owner, parents, bindings, resolving
                )
            return
        if name == "getattr":
            for argument in node.args[2:]:
                yield from visible_string_nodes(
                    argument, owner, parents, bindings, resolving
                )
            return
    key = binding_key(node)
    if key:
        resolving = set() if resolving is None else resolving
        marker = (id(owner), key)
        if marker in resolving:
            return
        resolving.add(marker)
        for value in binding_values(key, owner, parents, bindings):
            yield from visible_string_nodes(value, owner, parents, bindings, resolving)
        resolving.remove(marker)
        return
    for child in ast.iter_child_nodes(node):
        yield from visible_string_nodes(child, owner, parents, bindings, resolving)


def ignored_literal(node: ast.AST, source_lines: list[str]) -> bool:
    line = getattr(node, "lineno", 0)
    for line_number in (line - 1, line):
        if 1 <= line_number <= len(source_lines) and I18N_IGNORE_RE.search(
            source_lines[line_number - 1]
        ):
            return True
    return False


def non_display_literal(
    node: ast.AST, root: ast.AST, parents: dict[ast.AST, ast.AST]
) -> bool:
    """Return true for mapping keys and branch conditions that do not become text."""
    current = node
    while current is not root and current in parents:
        parent = parents[current]
        if isinstance(parent, ast.Subscript) and current is parent.slice:
            return True
        if isinstance(parent, ast.Dict):
            for key in parent.keys:
                if current is key:
                    return True
        if isinstance(parent, ast.IfExp) and current is parent.test:
            return True
        if isinstance(parent, ast.Call):
            name = call_name(parent)
            if name == "get" and parent.args and current is parent.args[0]:
                return True
            if name == "getattr" and len(parent.args) > 1 and current is parent.args[1]:
                return True
        current = parent
    return False


def extract(path: pathlib.Path) -> tuple[set[tuple[str, str]], list[str]]:
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=str(path))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    line_range, errors = scoped_lines(path, source_lines)
    bindings = build_bindings(tree, parents)
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = constant_text(node.value) if node.value is not None else None
            if value is not None:
                for target in targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = value

    messages: set[tuple[str, str]] = set()
    reported_literals: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not in_scope(node, line_range):
            continue
        if is_translate_call(node):
            if len(node.args) < 2:
                errors.append(f"{path}:{node.lineno}: translate() needs context and source")
                continue
            context = constant_text(node.args[0])
            if context is None and isinstance(node.args[0], ast.Name):
                context = constants.get(node.args[0].id)
            text = constant_text(node.args[1])
            if not context or text is None:
                errors.append(
                    f"{path}:{node.lineno}: translation context/source must be stable literals"
                )
                continue
            if context != PROJECT_CONTEXT:
                errors.append(
                    f"{path}:{node.lineno}: ReplayLab project UI translation context must be "
                    f"{PROJECT_CONTEXT!r}, got {context!r}"
                )
                continue
            if "%n" in text:
                errors.append(
                    f"{path}:{node.lineno}: ReplayLab Python extraction does not support %n; "
                    "use a complete source sentence with positional placeholders"
                )
                continue
            if CJK_SOURCE_RE.search(text):
                errors.append(
                    f"{path}:{node.lineno}: translation source is canonical English and must "
                    f"not contain CJK text: {text!r}"
                )
                continue
            messages.add((context, text))

        if isinstance(node, ast.Call) and call_name(node) in VISIBLE_CALLS:
            owner = lexical_scope(node, parents)
            for value in list(node.args) + [keyword.value for keyword in node.keywords]:
                for candidate in visible_string_nodes(
                    value, owner, parents, bindings
                ):
                    text = candidate.value
                    if (
                        non_display_literal(candidate, value, parents)
                        or
                        not text.strip()
                        or is_format_only(text)
                        or text in TECHNICAL_UI_LITERALS
                        or ignored_literal(candidate, source_lines)
                    ):
                        continue
                    finding = (candidate.lineno, text)
                    if finding in reported_literals:
                        continue
                    reported_literals.add(finding)
                    errors.append(
                        f"{path}:{candidate.lineno}: raw user-visible string bypasses "
                        f"QCoreApplication.translate(): {text!r}"
                    )
    return messages, errors


def cpp_registry(messages: set[tuple[str, str]], sources: list[pathlib.Path]) -> str:
    # Keep the registry independent of PowerShell 5/7 culture sorting.
    origins = ", ".join(sorted(path.name for path in sources))
    lines = [
        "// This file is generated by util/i18n/extract_replaylab_translations.py.",
        f"// Sources: {origins}",
        "// It is scanned by lupdate and is not compiled into qrenderdoc.",
        "#include <QCoreApplication>",
        "",
        "static const char *const replaylabPythonUiMessages[] =",
        "{",
    ]
    for context, source in sorted(messages):
        lines.append(
            f"  QT_TRANSLATE_NOOP({json.dumps(context, ensure_ascii=False)}, "
            f"{json.dumps(source, ensure_ascii=False)}),"
        )
    lines.extend(["};", ""])
    return "\n".join(lines)


def parse_cpp_registry_messages(text: str) -> set[tuple[str, str]]:
    """Read generated registry entries without accepting malformed or duplicate rows."""
    messages: set[tuple[str, str]] = set()
    entry_count = 0
    for line_number, line in enumerate(text.replace("\r\n", "\n").splitlines(), 1):
        if "QT_TRANSLATE_NOOP" not in line:
            continue
        entry_count += 1
        match = REGISTRY_ENTRY_RE.match(line)
        if match is None:
            raise ValueError(f"malformed QT_TRANSLATE_NOOP entry at line {line_number}")
        try:
            message = (
                json.loads(match.group("context")),
                json.loads(match.group("source")),
            )
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid JSON string in registry entry at line {line_number}: {exc}"
            ) from exc
        context, source = message
        if context != PROJECT_CONTEXT:
            raise ValueError(
                f"unexpected translation context at line {line_number}: {context!r}"
            )
        if "%n" in source:
            raise ValueError(f"unsupported %n source at line {line_number}: {source!r}")
        if CJK_SOURCE_RE.search(source):
            raise ValueError(f"CJK translation source at line {line_number}: {source!r}")
        if message in messages:
            raise ValueError(f"duplicate registry entry at line {line_number}: {message!r}")
        messages.add(message)
    if entry_count == 0:
        raise ValueError("registry contains no QT_TRANSLATE_NOOP entries")
    return messages


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="append", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--allow-output-superset",
        action="store_true",
        help=(
            "with --check, allow the checked-in registry to contain additional messages "
            "from a newer ReplayLab source revision"
        ),
    )
    args = parser.parse_args()
    if args.allow_output_superset and not args.check:
        parser.error("--allow-output-superset is only valid together with --check")

    messages: set[tuple[str, str]] = set()
    errors: list[str] = []
    for source in args.source:
        extracted, source_errors = extract(source.resolve())
        messages.update(extracted)
        errors.extend(source_errors)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    if not messages:
        print("No ReplayLab Python UI translations were found", file=sys.stderr)
        return 1

    expected = cpp_registry(messages, [path.resolve() for path in args.source])
    output = args.output.resolve()
    if args.check:
        try:
            actual = output.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Cannot read ReplayLab translation registry {output}: {exc}", file=sys.stderr)
            return 1
        if args.allow_output_superset:
            try:
                actual_messages = parse_cpp_registry_messages(actual)
            except ValueError as exc:
                print(f"Invalid ReplayLab translation registry {output}: {exc}", file=sys.stderr)
                return 1
            missing = sorted(messages - actual_messages)
            if missing:
                print(
                    "ReplayLab translation registry is missing messages from the checked-out "
                    "built-in integration source:",
                    file=sys.stderr,
                )
                for context, source in missing:
                    print(f"  {context}: {source}", file=sys.stderr)
                return 1
        elif actual.replace("\r\n", "\n") != expected:
            print(
                "ReplayLab translation registry is stale; run the extractor without --check",
                file=sys.stderr,
            )
            return 1
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        # Windows editors/indexers can map the generated source. Replacing a
        # sibling file avoids truncating that mapped file in place (EINVAL).
        import os
        import tempfile
        descriptor, temporary = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp", dir=str(output.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(expected)
            os.replace(temporary, output)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(f"ReplayLab translations: {len(messages)} messages; registry current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
