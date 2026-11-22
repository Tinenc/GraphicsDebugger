import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('source_link_selection',Path(__file__).parents[1]/'renderdoc_mcp_bridge/source_link_selection.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def test_symbol_selection_uses_utf8_bytes_and_exact_token():
    text='// 中文说明\r\n  float slope = slopeScale + slope;\r\n'
    start,end=module.selection_range(text,2,30,'slope')
    assert text.encode('utf-8')[start:end]==b'slope'
    assert start>text.encode('utf-8').find(b'slopeScale')
    assert module.selection_range(text,2,1,'missing')is None


def test_line_selection_and_invalid_positions():
    text='// 中文\n  float color = 1;\n'
    start,end=module.selection_range(text,2,select_line=True)
    assert text.encode('utf-8')[start:end]==b'float color = 1;'
    assert module.selection_range(text,3,select_line=True)is None
