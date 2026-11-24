"""Keep the optimized scanner identical to the prior strict token matching."""
import importlib.util
from pathlib import Path
import re
from unittest import mock

import pytest

from src.shader_workspace.adapters import dxil_access_signature as host
from tests.test_dxil_access_signature import REAL_SHAPE_DXIL


path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "dxil_access_signature.py"
spec = importlib.util.spec_from_file_location("dxil_index_bridge", path)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


@pytest.mark.parametrize("module", [host, bridge])
def test_index_preserves_ascii_boundaries_quoted_names_and_overlapping_tokens(module):
    names = ['%1', '%12', '%value', '%value.ext', '%"quoted name"',
             '%"outer %1"', '%"escaped\\\"name"', '%"%12"']
    wrappers = [(' ', ' '), ('a', 'z'), ('$', '.'), ('界', '界'),
                ('"', '"'), ('%', '%'), (';', ''), ('[', ']')]
    lines = [left + name + right for name in names for left, right in wrappers]
    lines += ['%unknown = add i32 %1, %12 ; %value',
              '%"outer %1" = extractvalue %dx.types.CBufRet.i32 %12, 0',
              'call void @unknown(%"quoted name", %1)',
              '%"%12" = add i32 %1, %12']
    codes, users = module._index_legacy_users(lines, dict.fromkeys(names))
    for name in names:
        old = re.compile(r"(?<![-A-Za-z$._0-9])%s(?![-A-Za-z$._0-9])" % re.escape(name))
        expected = [index for index, line in enumerate(lines)
                    if old.search(module._strip_comment(line).strip())]
        assert users.get(name, []) == expected
    assert len(codes) == len(lines)


@pytest.mark.parametrize("module", [host, bridge])
def test_combined_evidence_validates_access_exactly_once_and_keeps_layout_result(module):
    expected = module.extract_dxil_cbuffer_access_signature(REAL_SHAPE_DXIL).to_dict()
    absent = module.extract_dxil_unannotated_cbuffer_bindings(REAL_SHAPE_DXIL)
    with mock.patch.object(module, "extract_dxil_cbuffer_access_signature",
                           wraps=module.extract_dxil_cbuffer_access_signature) as parse:
        actual, annotations = module.extract_dxil_cbuffer_evidence(REAL_SHAPE_DXIL)
    assert actual.to_dict() == expected and annotations == absent
    assert parse.call_count == 1


@pytest.mark.parametrize("module", [host, bridge])
def test_combined_layout_unavailable_never_hides_access_error(module):
    missing_section = REAL_SHAPE_DXIL.replace(b'; Buffer Definitions:', b'; unavailable section:')
    access, absent = module.extract_dxil_cbuffer_evidence(missing_section)
    assert access.blocks and absent is None
    with pytest.raises(module.DxilAccessSignatureError):
        module.extract_dxil_unannotated_cbuffer_bindings(missing_section)
    with pytest.raises(module.DxilAccessSignatureError):
        module.extract_dxil_cbuffer_evidence(REAL_SHAPE_DXIL, expected_sha256='0' * 64)
