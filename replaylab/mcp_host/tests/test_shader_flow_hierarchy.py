from __future__ import annotations

import ast
import copy
import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_shader_flow_hierarchy.py"
SPEC = importlib.util.spec_from_file_location("shader_hierarchy_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _internal_nodes(graph):
    return [node for node in graph["nodes"] if node["kind"] not in ("input", "output")]


def _by_snippet(graph):
    return {node["source"]["snippet"]: node["id"] for node in _internal_nodes(graph)}


def test_workspace_function_only_projection_has_no_statement_boxes_or_legacy_code():
    source = ("// Evaluate the surface response.\nfloat Evaluate(float x) { return x*x; }\n"
              "float Main(float x) {\n" + "x += 0;\n" * 1100 +
              "return Evaluate(x);\n}")
    index = MODULE.SourceFlowHierarchy(source)
    accepted = {"entryPoint": "Main", "graphKind": "algorithm-dataflow", "nodes": [
        {"id": "legacy", "name": "Main", "description": "float _8737 = x * x;", "source": {"line": 3}}],
        "edges": []}
    before = copy.deepcopy(accepted)
    root = index.project_root(accepted, functions_only=True)
    assert accepted == before
    body = _internal_nodes(root)
    assert [node["label"] for node in body] == ["Main", "Evaluate"]
    assert body[1]["description"] == "Evaluate the surface response."
    assert body[1]["source"]["line"] == 1104
    assert body[1]["functionId"] == index.function_identity("Evaluate")
    assert body[1]["signature"] == "Evaluate(float x)"
    assert "return" in body[1]["io"]["writes"]
    assert root["edges"] == [{"source": body[0]["id"],
                               "target": body[1]["id"], "kind": "sequence"}]
    assert not root["truncated"]["nodes"]
    assert not any("_8737" in node["description"] for node in root["nodes"])


def test_function_only_leaf_is_one_interface_and_shared_expression_is_one_step():
    source = ("// Compute the response.\nfloat H(float x) {\n" + "x *= 0.5;\n" * 300 +
              "return x;\n}\nfloat Main(float x) { return H(x) + H(x); }")
    index = MODULE.SourceFlowHierarchy(source)
    leaf = index.function_graph(index.function_identity("H"), functions_only=True)
    assert len(_internal_nodes(leaf)) == 1
    assert _internal_nodes(leaf)[0]["label"] == "H"
    assert _internal_nodes(leaf)[0]["description"] == "Compute the response."
    assert _internal_nodes(leaf)[0]["signature"] == "H(float x)"
    main = index.function_graph(index.function_identity("Main"), functions_only=True)
    assert [node["label"] for node in main["nodes"]] == ["Main", "H"]
    assert len(main["edges"]) == 1  # one source expression, not an invented evaluation order
    assert {node["kind"] for node in leaf["nodes"]} == {"function-summary"}


def test_function_call_view_preserves_source_order_and_comment_evidence():
    source = (
        "// Evaluate the specular term.\n"
        "float Specular(float value) { return value; }\n"
        "// Evaluate the diffuse term.\n"
        "float Diffuse(float value) { return value; }\n"
        "// Combine lighting terms.\n"
        "float Main(float value) {\n"
        "  float diffuse = Diffuse(value);\n"
        "  float specular = Specular(value);\n"
        "  return diffuse + specular + Diffuse(value);\n"
        "}\n"
    )
    index = MODULE.SourceFlowHierarchy(source)
    accepted = {"entryPoint": "Main", "nodes": [
        {"name": "Main", "description": "Old module summary."},
        {"name": "Diffuse", "description": "Old diffuse summary."},
    ]}
    graph = index.project_root(accepted, functions_only=True)
    assert [node["name"] for node in graph["nodes"]] == [
        "Main", "Diffuse", "Specular", "Diffuse"]
    assert [node["description"] for node in graph["nodes"]] == [
        "Combine lighting terms.", "Evaluate the diffuse term.",
        "Evaluate the specular term.", "Evaluate the diffuse term."]
    assert {edge["kind"] for edge in graph["edges"]} == {"sequence"}
    assert [(edge["source"], edge["target"]) for edge in graph["edges"]] == [
        (graph["nodes"][0]["id"], graph["nodes"][1]["id"]),
        (graph["nodes"][1]["id"], graph["nodes"][2]["id"]),
        (graph["nodes"][2]["id"], graph["nodes"][3]["id"]),
    ]
    assert graph["nodes"][1]["io"]["reads"] == ["value"]
    assert graph["nodes"][1]["io"]["writes"] == ["return"]


def test_recursive_call_is_visible_without_an_infinite_drilldown():
    source = "float Recurse(float value) { return value > 0 ? Recurse(value - 1) : value; }"
    index = MODULE.SourceFlowHierarchy(source)
    graph = index.function_graph(index.function_identity("Recurse"), functions_only=True)
    assert [node["kind"] for node in graph["nodes"]] == [
        "function-summary", "flow-control"]
    assert graph["edges"] == [{
        "source": graph["nodes"][0]["id"],
        "target": graph["nodes"][1]["id"], "kind": "sequence"}]
    assert "functionId" not in graph["nodes"][1]


def test_workspace_selects_function_only_root_and_expansion_views():
    source = (MODULE_PATH.parent / "project_shader_workspace.py").read_text(encoding="utf-8")
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr in {"project_root", "function_graph"}]
    assert calls  # Root, active function, ancestry and current-buffer navigation.
    assert all(any(keyword.arg == "functions_only" and isinstance(keyword.value, ast.Constant)
                   and keyword.value.value is True for keyword in call.keywords) for call in calls)


def test_compact_execution_keeps_branch_alternatives_and_loop_back_edges():
    source = ("void A(){} void B(){} void C(){} void Main(int x){"
              "A(); if(x){B();}else{C();} A(); while(x){B(); x--;} C();}")
    index = MODULE.SourceFlowHierarchy(source)
    graph = index.function_graph(index.function_identity("Main"), functions_only=True)
    nodes = graph["nodes"]
    assert [n["kind"] for n in nodes] == [
        "function-summary", "function-call", "flow-control", "function-call",
        "function-call", "function-call", "flow-control", "function-call", "function-call"]
    edges = {(a["source"], a["target"], a["kind"]) for a in graph["edges"]}
    assert (nodes[1]["id"], nodes[2]["id"], "sequence") in edges
    assert (nodes[2]["id"], nodes[3]["id"], "true") in edges
    assert (nodes[2]["id"], nodes[4]["id"], "false") in edges
    assert not any(a == nodes[3]["id"] and b == nodes[4]["id"] for a, b, _ in edges)
    assert (nodes[7]["id"], nodes[6]["id"], "loop") in edges
    assert all("snippet" not in n["source"] for n in nodes)


def test_compact_execution_does_not_claim_argument_evaluation_order():
    source = "float A(float x){return x;} float B(){return 1;} float Main(){return A(B());}"
    index = MODULE.SourceFlowHierarchy(source)
    graph = index.function_graph(index.function_identity("Main"), functions_only=True)
    assert len(graph["nodes"]) == 2
    assert graph["nodes"][1]["kind"] == "function-group"
    assert graph["nodes"][1]["label"] == "A / B"
    assert len(graph["edges"]) == 1


def test_reviewed_helper_groups_preserve_paths_and_expand_exact_calls():
    source = ("float H(float x){return x*x;} float J(float x){return x+x;} "
              "void Effect(){} void Main(float x){x=H(x); x=J(x); Effect(); "
              "if(x){x=H(x);}else{x=J(x);} }")
    index = MODULE.SourceFlowHierarchy(source)
    overview = {"sourceSha256": index.source_sha256, "stages": [{
        "title": "Evaluate surface response", "description": "Prepare and shade the surface.",
        "anchors": [{"kind": "function", "functionName": "Main",
                     "functionSignature": "Main(float x)"}],
        "helperSignatures": ["H(float x)", "J(float x)"]}]}
    index.set_function_annotations(overview)
    graph = index.function_graph(index.function_identity("Main"), functions_only=True)
    groups = [node for node in graph["nodes"] if node.get("helperGroupId")]
    assert len(groups) == 1  # Never fold across branches or a substantive effect.
    assert graph["nodes"][0]["description"] == "Prepare and shade the surface."
    assert [n["kind"] for n in graph["nodes"]] == [
        "function-summary", "function-group", "function-call", "flow-control",
        "function-call", "function-call"]
    detail = index.function_graph(index.function_identity("Main"), functions_only=True,
                                  helper_group_id=groups[0]["helperGroupId"])
    assert [node["name"] for node in detail["nodes"]] == ["Main", "H", "J"]
    assert len(detail["edges"]) == 2
    assert {edge["kind"] for edge in detail["edges"]} == {"sequence"}
    overview["sourceSha256"] = "old"
    index.set_function_annotations(overview)
    fresh = index.function_graph(index.function_identity("Main"), functions_only=True)
    assert not any(node.get("helperGroupId") for node in fresh["nodes"])
    with pytest.raises(ValueError, match="changed or removed"):
        index.function_graph(index.function_identity("Main"), functions_only=True,
                             helper_group_id=groups[0]["helperGroupId"])


def test_function_summary_precedes_call_comments_and_navigation_uses_real_ancestry():
    source = ("// Square the value.\n// @stage internal-id\nfloat Helper(float x){return x*x;}\n"
              "void A(){\n// This temporary is just a scalar.\nfloat x=Helper(2);}\n"
              "void B(){A();} void Main(){B();} void Unrelated(){}")
    index = MODULE.SourceFlowHierarchy(source)
    graph = index.function_graph(index.function_identity("A"), functions_only=True)
    assert graph["nodes"][1]["description"] == "Square the value."
    ids = [index.function_identity(name) for name in ("Main", "B", "A", "Helper")]
    assert index.function_path(ids[0], ids[-1]) == ids
    assert index.function_path(ids[0], index.function_identity("Unrelated")) == []


def test_host_prepared_annotation_signature_matches_source_hierarchy():
    from src.shader_workspace.semantic_overview import prepare_overview
    source = "float H(float x){return x*x;} float Main(float x){x=H(x);return H(x);}"
    overview = prepare_overview(source, [{"id": "surface", "title": "Surface response",
        "anchors": [{"function": "Main"}], "helperSignatures": ["H"]}], "Main")
    assert overview["stages"][0]["helperSignatures"] == ["H ( float x )"]
    index = MODULE.SourceFlowHierarchy(source)
    index.set_function_annotations(overview)
    graph = index.function_graph(index.function_identity("Main"), functions_only=True)
    assert graph["nodes"][0]["description"] == "Surface response"
    assert len(graph["nodes"]) == 2 and graph["nodes"][1]["helperGroupId"]


@pytest.mark.parametrize("body,expected", [
    ("return; A(); B();", []),
    ("if(x){return; A();} B();", ["B"]),
    ("if(x){return;}else{return;} A(); B();", []),
])
def test_compact_execution_omits_unreachable_calls(body, expected):
    index = MODULE.SourceFlowHierarchy("void A(){} void B(){} void Main(int x){" + body + "}")
    graph = index.function_graph(index.function_identity("Main"), functions_only=True)
    assert [n["name"] for n in graph["nodes"] if n["kind"] == "function-call"] == expected
    assert all(any(e["target"] == n["id"] for e in graph["edges"]) for n in graph["nodes"][1:])


def test_index_is_python36_and_current_source_only():
    ast.parse(MODULE_PATH.read_text(encoding="utf-8"), feature_version=(3, 6))
    source = "float Square(float x) { return x*x; }\nfloat Main(float a) { float b=Square(a); return b; }"
    index = MODULE.SourceFlowHierarchy(source, {"documentId": "doc", "path": "source/shader.hlsl"})
    graph = index.function_graph(index.function_identity("Main"))
    call, result = _internal_nodes(graph)
    assert call["functionId"] == index.function_identity("Square")
    assert call["source"]["symbol"] == "Main"
    assert call["source"]["snippet"] == "float b=Square(a);"
    assert call["source"]["documentId"] == "doc"
    assert call["source"]["statementId"]
    assert {"source": call["id"], "target": result["id"], "kind": "sequence"} in graph["edges"]
    assert call["label"] == "Square" and call["description"] == ""
    assert "return" in call["io"]["writes"]
    body = _internal_nodes(index.function_graph(call["functionId"]))[0]
    assert body["label"] == "Square" and body["description"] == ""
    assert body["source"]["snippet"] == "return x*x;"


def test_root_preserves_accepted_stages_and_only_expands_real_unique_functions():
    index = MODULE.SourceFlowHierarchy("float Actual(float x) { return x*x; }")
    accepted = {"graphId": "accepted", "nodes": [
        {"id": "real", "name": "Actual", "label": "Lighting", "source": {"line": 1}},
        {"id": "fake", "name": "Missing", "label": "Actual", "functionId": "forged"}],
        "edges": []}
    before = copy.deepcopy(accepted)
    root = index.project_root(accepted)
    assert accepted == before
    assert root["graphId"] == "accepted"
    assert root["nodes"][0]["label"] == "Actual"
    assert root["nodes"][0]["description"] == "Lighting"
    assert root["nodes"][0]["functionId"] == index.function_identity("Actual")
    assert root["nodes"][0]["source"]["symbol"] == "Actual"
    assert "functionId" not in root["nodes"][1]


def test_legacy_definition_graph_projects_entry_calculation_without_changing_artifact():
    index = MODULE.SourceFlowHierarchy("float H(float x){return x*x;} float Main(float x){return H(x);}")
    artifact = {"entryPoint": "Main", "graphId": "accepted-legacy", "nodes": [
        {"id": "definition-main", "name": "Main"}, {"id": "definition-h", "name": "H"}], "edges": []}
    before = copy.deepcopy(artifact)
    root = index.project_root(artifact)
    assert artifact == before
    body = _internal_nodes(root)
    assert len(body) == 1
    assert body[0]["label"] == "H"
    assert body[0]["source"]["snippet"] == "return H(x);"
    assert body[0]["functionId"] == index.function_identity("H")
    assert root["graphKind"] == "algorithm-dataflow"


def test_function_id_survives_body_edits_but_missing_signature_does_not_relocate():
    original = MODULE.SourceFlowHierarchy("float Work(float x) { return x; }")
    function_id = original.function_identity("Work")
    changed = MODULE.SourceFlowHierarchy("\n// moved\nfloat Work(float x) { float y=floor(x); return y*y; }")
    assert changed.function_identity("Work") == function_id
    assert changed.function_graph(function_id)["nodes"][0]["source"]["line"] == 3
    with pytest.raises(ValueError, match="missing or ambiguous"):
        MODULE.SourceFlowHierarchy("float Other(float x) { return x; }").function_graph(function_id)


def test_comments_strings_prototypes_and_member_calls_do_not_create_children():
    source = '''
#define FAKE(x) Fake(x)
float Forward(float x);
float Real(float x) { return x; }
float Main(float x) {
  // Real(x); Fake(x);
  float y = object.Real(x);
  return y;
}
'''
    index = MODULE.SourceFlowHierarchy(source)
    assert index.function_identity("Forward") is None
    assert index.function_identity("Fake") is None
    graph = index.function_graph(index.function_identity("Main"))
    assert not any(node.get("functionId") for node in graph["nodes"])


def test_overloads_are_not_guessed_from_a_call_name():
    source = "float F(float x){return x;} float2 F(float2 x){return x;} float Main(float x){return F(x);}"
    index = MODULE.SourceFlowHierarchy(source)
    assert index.function_identity("F") is None
    assert "functionId" not in index.function_graph(index.function_identity("Main"))["nodes"][0]


def test_duplicate_preprocessor_signatures_fail_closed():
    source = "#if A\nfloat F(float x){return x;}\n#else\nfloat F(float x){return -x;}\n#endif\n"
    index = MODULE.SourceFlowHierarchy(source)
    assert index.function_identity("F") is None
    with pytest.raises(ValueError):
        index.function_graph(next(iter(index.functions)))


def test_conditions_keep_mutually_exclusive_calculations_separate_and_join():
    index = MODULE.SourceFlowHierarchy("float F(float x) { float y=0; if(x>0){y=x*x;}else{y=-x;} return y; }")
    graph = index.function_graph(index.function_identity("F"))
    by_label = _by_snippet(graph)
    condition, true, false, output = [by_label[label] for label in ("if(x>0)", "y=x*x;", "y=-x;", "return y;")]
    edges = {(edge["source"], edge["target"], edge["kind"]) for edge in graph["edges"]}
    assert (condition, true, "true") in edges
    assert (condition, false, "false") in edges
    assert (true, output, "sequence") in edges
    assert (false, output, "sequence") in edges
    assert not any(source == true and target == false for source, target, kind in edges)


def test_unbraced_nested_if_and_return_preserve_real_branches():
    index = MODULE.SourceFlowHierarchy("float F(float x){if(x>0) if(x>1) return 1; else return 2; return 0;}")
    graph = index.function_graph(index.function_identity("F"))
    returns = {node["id"] for node in graph["nodes"] if node["kind"] == "return"}
    assert len(returns) == 3
    boundary_output = next(node["id"] for node in graph["nodes"] if node["kind"] == "output")
    assert all(edge["target"] == boundary_output for edge in graph["edges"] if edge["source"] in returns)


def test_straight_calculations_are_batched_and_math_is_navigation_only():
    source = "float F(float x){" + "".join("x=x+%s;" % i for i in range(16)) + "return smoothstep(0,1,x);}"
    index = MODULE.SourceFlowHierarchy(source)
    graph = index.function_graph(index.function_identity("F"))
    nodes = _internal_nodes(graph)
    assert len(nodes) == 3
    assert "smoothstep" in nodes[-1]["source"]["snippet"]
    assert all(node["label"] == "F" and node["description"] == "" for node in nodes)
    assert all(node["sourceRange"]["startLine"] == 1 for node in graph["nodes"])


def test_loop_break_does_not_link_back_to_predicate():
    index = MODULE.SourceFlowHierarchy("float F(float x){while(x>0){if(x>9) break; x=x-1;} return x;}")
    graph = index.function_graph(index.function_identity("F"))
    lookup = _by_snippet(graph)
    edges = {(edge["source"], edge["target"], edge["kind"]) for edge in graph["edges"]}
    assert (lookup["x=x-1;"], lookup["while(x>0)"], "loop") in edges
    assert (lookup["break;"], lookup["return x;"], "sequence") in edges
    assert not any(a == lookup["break;"] and b == lookup["while(x>0)"] for a, b, k in edges)


def test_target_graph_is_bounded_and_declares_incomplete_projection():
    source = "void H() {} void Main(){" + "H();" * 150 + "}"
    index = MODULE.SourceFlowHierarchy(source)
    graph = index.function_graph(index.function_identity("Main"))
    assert len(_internal_nodes(graph)) == MODULE.MAX_LOCAL_NODES
    assert graph["truncated"]["nodes"] is True
    assert len({node["id"] for node in graph["nodes"]}) == len(graph["nodes"])


def test_invalid_current_source_cannot_return_an_old_function_graph():
    with pytest.raises(ValueError, match="unmatched"):
        MODULE.SourceFlowHierarchy("float F(float x){ return x;")


def test_local_declarations_are_not_algorithm_nodes_and_hlsl_attributes_keep_branches():
    source = "struct Data {float value;}; float F(float x){ Data d; float t; [branch] if(x>0){t=x;}else{t=-x;} return t;}"
    index = MODULE.SourceFlowHierarchy(source)
    graph = index.function_graph(index.function_identity("F"))
    labels = [node["source"]["snippet"] for node in _internal_nodes(graph)]
    assert labels == ["if(x>0)", "t=x;", "t=-x;", "return t;"]
    assert {edge["kind"] for edge in graph["edges"]} >= {"true", "false"}


def test_inout_array_signature_and_attributed_loop_have_stable_function_identity():
    source = "struct Data{float value;}; void Update(inout Data d, float weights[4]) { [unroll] for(int i=0;i<4;i++){ if(weights[i]<0) continue; d.value += weights[i]; } }"
    index = MODULE.SourceFlowHierarchy(source)
    graph = index.function_graph(index.function_identity("Update"))
    labels = _by_snippet(graph)
    loop = labels["for(int i=0;i<4;i++)"]
    assert any(edge["source"] == labels["continue;"] and edge["target"] == loop
               and edge["kind"] == "loop" for edge in graph["edges"])
    assert "inout Data d" in graph["nodes"][0]["source"]["functionSignature"]


def test_switch_cases_are_one_honest_selection_not_false_linear_edges():
    source = "float H(float x){return x*x;} float F(float x){switch((int)x){case 1:return H(x); default:return 0;}}"
    index = MODULE.SourceFlowHierarchy(source)
    graph = index.function_graph(index.function_identity("F"))
    nodes = _internal_nodes(graph)
    assert len(nodes) == 1
    assert nodes[0]["kind"] == "selection"
    assert "functionId" not in nodes[0]
    assert len(graph["edges"]) == 2  # Entry and exit boundaries only.


def test_function_boxes_keep_comment_and_interface_without_call_or_implementation():
    source = '''
// Normalize the supplied lighting direction.
float3 NormalizeLight(inout float3 direction, out float magnitude) {
  // Preserve the incoming vector length.
  magnitude = length(direction);
  direction /= magnitude;
  return direction;
}
float3 Main(float3 light) {
  float magnitude;
  // Prepare a unit direction for lighting.
  float3 unitLight = NormalizeLight(light, magnitude);
  return unitLight;
}
'''
    index = MODULE.SourceFlowHierarchy(source)
    graph = index.function_graph(index.function_identity("Main"))
    call = next(node for node in graph["nodes"] if node["kind"] == "function-call")
    assert call["label"] == "NormalizeLight"
    assert call["description"] == "Prepare a unit direction for lighting."
    assert call["source"]["snippet"] == "float3 unitLight = NormalizeLight(light, magnitude);"
    assert set(call["io"]["writes"]) >= {"direction", "magnitude", "return"}
    assert "direction" in call["io"]["reads"]
    assert "magnitude" not in call["io"]["reads"]
    result = next(node for node in graph["nodes"] if node["kind"] == "return")
    assert result["io"]["reads"] == ["unitLight"]
    assert result["io"]["writes"] == ["return"]
    child = index.function_graph(call["functionId"])
    assert _internal_nodes(child)[0]["description"] == "Preserve the incoming vector length."
    for node in _internal_nodes(child):
        assert node["label"] == "NormalizeLight"
        assert "length(direction)" not in node["description"]
        assert "direction /=" not in node["description"]
    accepted = {"entryPoint": "Main", "graphKind": "algorithm-dataflow", "nodes": [
        {"id": "stage", "name": "NormalizeLight", "label": "Prepare lighting",
         "description": "Prepare lighting", "source": {"line": 13}}], "edges": []}
    stage = next(node for node in index.project_root(accepted)["nodes"] if node["id"] == "stage")
    assert stage["label"] == "NormalizeLight"
    assert stage["description"] == "Prepare a unit direction for lighting."
