"""Incidental presentation changes do not invalidate native Shader proofs."""

from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest

from tests.test_bridge_dxil_access_gate import load_bridge


@pytest.fixture(scope="module")
def bridge():
    return load_bridge()


def binding(bridge, *, replace_role=None, replace_path=None, native=(), version=1):
    sources = [tuple([role, str(replace_path) if role == replace_role else path, roots, terminals])
               for role, path, roots, terminals in bridge._shader_restore_algorithm_sources]
    return bridge._shader_restore_protocol.physical_validation_binding(sources, native, version)


def test_real_bridge_version_ui_text_comments_and_formatting_are_provenance_only(bridge, tmp_path):
    path = Path(bridge.__file__)
    source = path.read_text(encoding="utf-8")
    changed = re.sub(r'BRIDGE_INTEGRATION_VERSION = "[^"]+"',
                     'BRIDGE_INTEGRATION_VERSION = "99.8.7"', source)
    changed = changed.replace("Open one Host-verified organization draft as an isolated read-only tab.",
                              "A new presentation description that is not Shader ABI policy.")
    changed += "\n# A packaging/version/presentation-only update.\n"
    modified = tmp_path / "bridge.py"
    modified.write_text(changed, encoding="utf-8")
    assert bridge._shader_restore_protocol.validation_policy_sha256([str(path)]) != \
           bridge._shader_restore_protocol.validation_policy_sha256([str(modified)])
    assert binding(bridge, replace_role="bridge-physical-shader-validation", replace_path=modified) == binding(bridge)


def test_catalog_identity_read_route_is_not_physical_shader_validation_policy(bridge, tmp_path):
    source = Path(bridge.__file__).read_text(encoding="utf-8")
    # Replay admission still uses the same physical rules even when this
    # unrelated catalog/identity read chooses another scheduling path.
    start = source.index("def _get_capture_status(params):")
    end = source.index("\ndef ", start + 1)
    changed = source[:start] + "def _get_capture_status(params):\n    return {'loaded': False}\n\n" + source[end:]
    modified = tmp_path / "catalog-read-change.py"
    modified.write_text(changed, encoding="utf-8")
    assert binding(bridge, replace_role="bridge-physical-shader-validation", replace_path=modified) == binding(bridge)


@pytest.mark.parametrize("before,after", [
    ('if cap != cand:', 'if False:'),
    ('"stage", "entryPoint", "threadGroupSize",', '"stage", "entryPoint", "threadGroupSize", "newPhysicalRequirement",'),
    ('hashlib.sha256(target_raw).hexdigest() != candidate_sha256',
     'hashlib.sha256(target_raw).hexdigest() == candidate_sha256'),
    ('physical_binding != _shader_restore_physical_validation_binding',
     'False'),
])
def test_actual_cbuffer_layout_structure_and_built_native_checks_invalidate(bridge, tmp_path, before, after):
    source = Path(bridge.__file__).read_text(encoding="utf-8")
    assert before in source
    modified = tmp_path / "physical-change.py"
    modified.write_text(source.replace(before, after), encoding="utf-8")
    assert binding(bridge, replace_role="bridge-physical-shader-validation", replace_path=modified) != binding(bridge)


def test_dxil_access_scalar_layout_parser_is_part_of_physical_policy(bridge, tmp_path):
    path = next(Path(path) for role, path, roots, terminals in bridge._shader_restore_algorithm_sources
                if role == "captured-dxil-layout-access")
    source = path.read_text(encoding="utf-8")
    assert '"i32": 4,' in source
    modified = tmp_path / "dxil-change.py"
    modified.write_text(source.replace('"i32": 4,', '"i32": 8,'), encoding="utf-8")
    assert binding(bridge, replace_role="captured-dxil-layout-access", replace_path=modified) != binding(bridge)


def test_actual_native_binary_bytes_and_abi_rule_version_invalidate(bridge, tmp_path):
    native = tmp_path / "renderdoc.dll"
    native.write_bytes(b"first real native ABI implementation")
    first = binding(bridge, native=[str(native)])
    native.write_bytes(b"different native ABI implementation")
    assert binding(bridge, native=[str(native)]) != first
    assert binding(bridge, version=2) != binding(bridge, version=1)


def test_missing_physical_root_is_not_silently_omitted(bridge, tmp_path):
    source = tmp_path / "wrong.py"
    source.write_text("def unrelated(): return True\n", encoding="utf-8")
    with pytest.raises(ValueError, match="code root is missing"):
        bridge._shader_restore_protocol.physical_validation_binding([
            ("actual-validation", str(source), ("required_physical_check",), ()),
        ], [])


def test_docstrings_locations_and_runtime_ast_metadata_do_not_change_code_facts(bridge, tmp_path):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text('def validate(value):\n    """Old docs."""\n    return value[0] == 4\n', encoding="utf-8")
    second.write_text('\n\n# reformatted\ndef validate(value):\n    """New docs."""\n    return (value[0] == 4)\n', encoding="utf-8")
    protocol = bridge._shader_restore_protocol
    a = protocol.physical_validation_binding([("rule", str(first), ("validate",), ())], [])
    b = protocol.physical_validation_binding([("rule", str(second), ("validate",), ())], [])
    assert a == b
    ast.parse(Path(protocol.__file__).read_text(encoding="utf-8"), feature_version=(3, 6))
    ast.parse(Path(bridge.__file__).read_text(encoding="utf-8"), feature_version=(3, 6))


def test_local_name_matching_global_function_does_not_pull_administration_into_policy(bridge, tmp_path):
    source = tmp_path / "local.py"
    source.write_text("def administration(): return 'UI version 1'\ndef validate(administration): return administration()\n", encoding="utf-8")
    protocol = bridge._shader_restore_protocol
    first = protocol.physical_validation_binding([("rule", str(source), ("validate",), ())], [])
    source.write_text(source.read_text().replace("UI version 1", "UI version 2"), encoding="utf-8")
    assert protocol.physical_validation_binding([("rule", str(source), ("validate",), ())], []) == first


def test_legacy_36_literal_and_index_nodes_have_same_facts_as_modern_ast(bridge):
    protocol = bridge._shader_restore_protocol
    for name, field, literal in (("Str", "s", "profile"), ("Bytes", "s", b"DXIL\x00"), ("Num", "n", 4),
                                 ("NameConstant", "value", True), ("NameConstant", "value", None)):
        legacy = type(name, (ast.AST,), {"_fields": (field,)})()
        setattr(legacy, field, literal)
        assert protocol._semantic_ast(legacy) == protocol._semantic_ast(ast.Constant(value=literal))
    legacy_index = type("Index", (ast.AST,), {"_fields": ("value",)})()
    legacy_index.value = ast.Constant(value=4)
    assert protocol._semantic_ast(legacy_index) == protocol._semantic_ast(ast.Constant(value=4))


def test_complete_selected_policy_serializes_with_legacy_36_literals(bridge, monkeypatch):
    protocol = bridge._shader_restore_protocol
    expected = binding(bridge)
    real_parse = ast.parse

    class LegacyLiterals(ast.NodeTransformer):
        def visit_Constant(self, node):
            value = node.value
            if isinstance(value, bytes):
                name, field = "Bytes", "s"
            elif isinstance(value, str):
                name, field = "Str", "s"
            elif value is None or isinstance(value, bool):
                name, field = "NameConstant", "value"
            elif isinstance(value, (int, float, complex)):
                name, field = "Num", "n"
            else:
                return node
            legacy = type(name, (ast.AST,), {"_fields": (field,)})()
            setattr(legacy, field, value)
            return legacy

    def parse_legacy(*args, **kwargs):
        return LegacyLiterals().visit(real_parse(*args, **kwargs))

    monkeypatch.setattr(protocol.ast, "parse", parse_legacy)
    actual = binding(bridge)
    assert actual == expected
    assert protocol.physical_validation_binding_sha256(actual) == protocol.physical_validation_binding_sha256(expected)
