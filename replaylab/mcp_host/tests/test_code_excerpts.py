import pytest
from src.code_excerpts import complete_source_excerpt,validate_fragment
from src.code_excerpts import presentation_excerpt
from src.visual_replies import CodeRange, CodeMapping


def test_source_range_includes_closing_brace_and_preserves_lines():
    source='float f()\n{\n  return 1;\n}\nfloat g;'
    text,end=complete_source_excerpt(source,1,3)
    assert text=='float f()\n{\n  return 1;\n}' and end==4


def test_nested_blocks_comments_and_strings_are_checked():
    assert validate_fragment('void F(){ /* } */ const char *s="[}"; if(x){ y(); } // {\n}')
    for code in ['void F(){','float x = f(1;','void F(){ /*','void F(){ x[0); }']:
        with pytest.raises(ValueError):validate_fragment(code)


def test_large_function_requires_a_smaller_complete_calculation():
    with pytest.raises(ValueError,match='line budget'):
        complete_source_excerpt('void F(){\n'+('x++;\n'*150)+'}',1,4)


def test_sparse_code_keeps_source_lines_and_omission_is_unlinked():
    source='float F(){\n float slope = input;\n float unused = 4;\n return slope;\n}'
    excerpt=presentation_excerpt(source,1,5,ranges=[CodeRange(startLine=1,endLine=2),CodeRange(startLine=4,endLine=5)])
    assert excerpt['text']=='float F(){\n float slope = input;\n// ****\n return slope;\n}'
    assert excerpt['lineNumbers']==[1,2,0,4,5]
    with pytest.raises(ValueError,match='incomplete'):
        presentation_excerpt(source,1,2,ranges=[CodeRange(startLine=1,endLine=2)])


def test_example_rows_and_renamed_tokens_map_to_exact_saved_coordinates():
    excerpt=presentation_excerpt('float F(){\n float actual = input;\n return actual;\n}',2,2,
        example='float weight = input;',mappings=[CodeMapping(displayLine=1,sourceLine=2),
        CodeMapping(displayLine=1,sourceLine=3,symbol='weight',sourceSymbol='actual')])
    assert excerpt['lineNumbers']==[2]
    assert excerpt['mappings'][1]==dict(displayLine=1,sourceLine=3,column=9,symbol='weight',sourceSymbol='actual')
    with pytest.raises(ValueError,match='source symbol'):
        presentation_excerpt('float other;',1,1,example='float weight;',mappings=[
            CodeMapping(displayLine=1,sourceLine=1,symbol='weight')])


def test_exact_example_rows_keep_line_and_token_locations_without_guessing():
    source='float unrelated;\nfloat F(){\n float slope = input;\n return slope;\n}\n'
    excerpt=presentation_excerpt(source,2,5,example='float F(){\n  float slope = input;\n  return slope;\n}')
    assert excerpt['lineNumbers']==[2,3,4,5]
    assert excerpt['exactSourceLines']==[True]*4
    ambiguous=presentation_excerpt('float value;\nfloat value;',1,2,example='float value;')
    assert ambiguous['lineNumbers']==[0] and ambiguous['exactSourceLines']==[False]


def test_renamed_example_row_uses_its_unambiguous_explicit_mapping():
    excerpt=presentation_excerpt('float actual = input;',1,1,example='float weight = input;',mappings=[
        CodeMapping(displayLine=1,sourceLine=1,symbol='weight',sourceSymbol='actual')])
    assert excerpt['lineNumbers']==[1] and excerpt['exactSourceLines']==[False]
