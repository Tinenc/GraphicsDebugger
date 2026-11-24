from src.vertex_input_roles import analyze_inputs
import json


def test_live_nested_inspection_preserves_identity_and_does_not_confuse_channel_indices():
    result = analyze_inputs({"shader": {"rawSha256": "a" * 64, "entryPoint": "VSMain",
        "inputs": [{"name": "TEXCOORD", "semantic": "TEXCOORD", "idx": 1, "count": 4}],
        "sourceFiles": [{"filename": "mesh.hlsl", "contents":
            "// normalize(TEXCOORD1)\nfloat4 a = TEXCOORD10;\nfloat4 b = TEXCOORD1;\n"}]}}, 41, 12)
    row = result["inputs"][0]
    assert row["components"] == 4 and row["attributeIndex"] is None
    assert row["signatureIndex"] == 0 and row["semanticIndex"] == 1
    assert row["sourceUses"] == [{"document": "mesh.hlsl", "line": 3,
        "text": "float4 b = TEXCOORD1;", "lineTruncated": False}]
    assert not row["signals"]
    assert result["shaderIdentity"]["rawSha256"] == "a" * 64


def test_disassembly_map_and_use_bounds_are_explicit():
    result = analyze_inputs({"shader": {"inputSignature": [{"regIndex": 2, "compCount": 3}],
        "disassembly": {"DXBC": "dp3 r0.x, v2.xyz, v2.xyz\nmov r1, v20\nmov r2, v2"}}}, 8, 1)
    row = result["inputs"][0]
    assert result["sourceAvailable"]
    assert row["truncated"] and row["matchedLineCount"] == 2
    assert row["sourceUses"][0]["line"] == 1
    assert row["role"] == "requires-dataflow-analysis"


def test_normalize_is_a_clue_not_a_proven_normal():
    row = analyze_inputs({"inputs": [{"name": "direction"}],
                          "source": "float3 ray = normalize(direction);"}, 1, 2)["inputs"][0]
    assert row["signals"] == ["normalize call on a matching source line; inspect dataflow"]
    assert row["role"] == "requires-dataflow-analysis"


def test_host_tool_uses_the_actual_nested_reflection_contract(monkeypatch):
    from src import server
    calls = []
    def inspect(**arguments):
        calls.append(arguments)
        return json.dumps({"shader": {"inputs": [{"name": "positionOS", "count": 3}],
                                      "source": "output = positionOS;"}})
    monkeypatch.setattr(server, "inspect_shader", inspect)
    result = json.loads(server.analyze_vertex_input_roles(91))
    assert calls == [{"event_id": 91, "stage": "vertex"}]
    assert result["sourceAvailable"] and result["inputs"][0]["components"] == 3
