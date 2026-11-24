import hashlib
import json

import pytest

from src import source_review_observations as module


def snapshot(tmp_path, source, baseline=None, **extra):
    current = source.encode("utf-8")
    initial = (source if baseline is None else baseline).encode("utf-8")
    (tmp_path / "source.hlsl").write_bytes(current)
    (tmp_path / "baseline.hlsl").write_bytes(initial)
    return {"workspacePath": str(tmp_path), "sourcePath": str(tmp_path / "source.hlsl"),
            "baselineSourcePath": str(tmp_path / "baseline.hlsl"),
            "sourceSha256": hashlib.sha256(current).hexdigest(),
            "baselineSourceSha256": hashlib.sha256(initial).hexdigest(), **extra}


PROBE = """float3 BlendProbe(float3 indirectLight, float3 baseLight, float scale, float weight)
{
    float deltaX = scale * indirectLight.x - baseLight.x;
    float deltaY = scale * indirectLight.y - baseLight.y;
    float deltaZ = scale * indirectLight.z - baseLight.z;
    float mixedX = weight * deltaX + baseLight.x;
    float mixedY = weight * deltaY + baseLight.y;
    float mixedZ = weight * deltaZ + baseLight.z;
    return float3(mixedX, mixedY, mixedZ);
}
"""


def test_remaining_channel_chains_have_exact_locations_and_no_quality_verdict(tmp_path):
    state = snapshot(tmp_path, PROBE)
    result = module.source_review_observations(state)
    channels = result["channelExpressions"]
    assert channels["candidateCount"] == 2
    first, second = channels["examples"]
    assert first["function"] == "BlendProbe"
    assert (first["startLine"], first["endLine"], first["channels"]) == (3, 5, "XYZ")
    assert first["variables"] == ["deltaX", "deltaY", "deltaZ"]
    assert second["variables"] == ["mixedX", "mixedY", "mixedZ"]
    assert result["semanticAssessment"] is False
    assert not ({"verdict", "score", "passed", "safeToVectorize", "blocked"} & result.keys())
    assert result["functionBodies"]["counts"]["unchanged"] == 1
    assert result["sourceSha256"] == state["sourceSha256"]
    assert (tmp_path / "source.hlsl").read_text() == PROBE


def test_vectors_remove_candidates_without_requiring_any_specific_refactor(tmp_path):
    edited = """float3 BlendProbe(float3 indirectLight, float3 baseLight, float scale, float weight)
{
    float3 delta = scale * indirectLight - baseLight;
    return weight * delta + baseLight;
}
"""
    result = module.source_review_observations(snapshot(tmp_path, edited, PROBE))
    assert result["channelExpressions"]["candidateCount"] == 0
    assert result["functionBodies"]["counts"] == {"changed": 1, "unchanged": 0, "added": 0, "removed": 0}


@pytest.mark.parametrize("body", [
    "float valueX = v.x; if (flag) { float valueY = v.y; float valueZ = v.z; }",
    "if (flag) { float valueX = v.x; } else { float valueY = v.y; float valueZ = v.z; }",
    "float valueX = v.x;\n#if MODE\nfloat valueY = v.y;\n#endif\nfloat valueZ = v.z;",
    "float valueX = v.x; precise float valueY = v.y; float valueZ = v.z;",
    "float valueX = v.x + 1; float valueY = v.y - 1; float valueZ = v.z + 1;",
    "float valueX = v.x * weight1; float valueY = v.y * weight2; float valueZ = v.z * weight1;",
    "float valueX = CalcX(v.x); float valueY = CalcY(v.y); float valueZ = CalcZ(v.z);",
    "// float valueX = v.x; float valueY = v.y; float valueZ = v.z;\nreturn;",
    'const char *s = "float valueX = v.x; float valueY = v.y; float valueZ = v.z;";',
    "float valueX = v.x, other = 2; float valueY = v.y; float valueZ = v.z;",
])
def test_candidates_do_not_join_different_paths_or_operators(tmp_path, body):
    result = module.source_review_observations(snapshot(tmp_path, "void MainPS() {\n" + body + "\n}"))
    assert result["channelExpressions"]["candidateCount"] == 0


def test_rgb_components_and_common_channel_named_scalar(tmp_path):
    source = """float3 MainPS(float3 light, float commonX) {
        float colorR = commonX * light.x;
        float colorG = commonX * light.y;
        float colorB = commonX * light.z;
        return float3(colorR, colorG, colorB);
    }"""
    result = module.source_review_observations(snapshot(tmp_path, source))
    assert result["channelExpressions"]["candidateCount"] == 1
    assert result["channelExpressions"]["examples"][0]["channels"] == "RGB"


def test_body_comparison_ignores_only_comments_whitespace_and_tracks_overloads(tmp_path):
    baseline = """float Keep(float x) { return x + 1; }
float Work(float x) { return x * 2; }
float Work(float2 x) { return x.x; }
float Old() { return 3; }
"""
    edited = """// New documentation, same calculation.
float Keep(float renamed) { return renamed + 1; }
float Work(float x) { // Explain existing multiplication.
    return x * 2;
}
float Work(float2 x) { return x.y; }
float Added() { return 3; }
"""
    result = module.source_review_observations(snapshot(tmp_path, edited, baseline))
    assert result["functionBodies"]["counts"] == {"changed": 2, "unchanged": 1, "added": 1, "removed": 1}
    assert result["functionBodies"]["examples"]["unchanged"][0]["function"] == "Work"
    # A rename is a token change; the Host does not claim it improved an algorithm.
    assert result["functionBodies"]["examples"]["changed"][0]["function"] == "Keep"


def test_actual_entry_and_nested_calls_are_visible(tmp_path):
    source = """float Leaf(float x) { return x * 2; }
float Unused() { return 1; }
float MainPS_inner(float x) { return Leaf(x); }
float MainPS(float x) { return MainPS_inner(x); }
"""
    result = module.source_review_observations(snapshot(tmp_path, source, entryPoint="MainPS"))
    assert [(row["function"], row["calls"]) for row in result["callGraph"]["nodes"]] == [
        ("MainPS", ["MainPS_inner"]), ("MainPS_inner", ["Leaf"]), ("Leaf", [])]
    assert result["callGraph"]["edgeCount"] == 2
    assert result["callGraph"]["omittedFunctions"] == 1


def test_payload_is_bounded_and_all_omitted_examples_are_counted(tmp_path):
    source = "\n".join(PROBE.replace("BlendProbe", "BlendProbe" + str(index)) for index in range(40))
    result = module.source_review_observations(snapshot(tmp_path, source))
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= module.MAX_OBSERVATION_BYTES
    channels = result["channelExpressions"]
    assert channels["candidateCount"] == 80
    assert channels["omittedCount"] + len(channels["examples"]) == 80
    assert 0 < len(channels["examples"]) <= 8
    assert len({row["function"] for row in channels["examples"]}) == len(channels["examples"])
    functions = result["functionBodies"]
    assert functions["omittedCounts"]["unchanged"] + len(functions["examples"]["unchanged"]) == 40
    assert result["callGraph"]["omittedFunctions"] + len(result["callGraph"]["nodes"]) == 40


def test_exact_source_hash_cache_avoids_reparsing_and_return_values_are_independent(tmp_path, monkeypatch):
    with module._CACHE_LOCK:
        module._CACHE.clear()
    parse = module._function_records
    parsed = []
    def counted(source):
        parsed.append(source)
        return parse(source)
    monkeypatch.setattr(module, "_function_records", counted)
    state = snapshot(tmp_path, PROBE)
    first = module.source_review_observations(state)
    first["channelExpressions"]["examples"].clear()
    second = module.source_review_observations(state)
    assert len(parsed) == 1
    assert len(second["channelExpressions"]["examples"]) == 2
    changed = snapshot(tmp_path, PROBE + "\n// another source identity\n", PROBE)
    module.source_review_observations(changed)
    assert len(parsed) == 2


def test_stale_source_identity_is_a_diagnostic(tmp_path):
    state = snapshot(tmp_path, PROBE)
    (tmp_path / "source.hlsl").write_text(PROBE + "\n// concurrent edit", encoding="utf-8")
    with pytest.raises(ValueError, match="Current source changed"):
        module.source_review_observations(state)


def test_same_source_without_a_baseline_file_is_supported(tmp_path):
    state = snapshot(tmp_path, PROBE)
    state.pop("baselineSourcePath")
    assert module.source_review_observations(state)["functionBodies"]["counts"]["unchanged"] == 1
    state["baselineSourceSha256"] = "0" * 64
    with pytest.raises(ValueError, match="Baseline source changed"):
        module.source_review_observations(state)


def test_reuse_candidates_normalize_only_parameter_names_and_preserve_locations(tmp_path):
    source = """float3 EvaluateFirst(float3 color, float weight) {
    float3 tinted = color * weight;
    return saturate(tinted);
}
float3 EvaluateSecond(float3 baseColor, float blend) {
    float3 tinted = baseColor * blend;
    return saturate(tinted);
}
"""
    result = module.source_review_observations(snapshot(tmp_path, source))
    duplicates = result["duplicateBodies"]
    assert duplicates["candidateGroupCount"] == 1
    assert duplicates["candidateFunctionCount"] == 2
    assert [(row["function"], row["startLine"], row["endLine"])
            for row in duplicates["examples"][0]["functions"]] == [
        ("EvaluateFirst", 1, 4), ("EvaluateSecond", 5, 8)]
    assert result["semanticAssessment"] is False


@pytest.mark.parametrize("first,second", [
    ("return x + 1;", "return y + 2;"),
    ("return x.red;", "return y.green;"),
    ("return material.x + x;", "return material.y + y;"),
    ("return x * globalA;", "return y * globalB;"),
    ("return First(x);", "return Second(y);"),
    ("float local = x; return local;", "float other = y; return other;"),
    ('const char *label = "one"; return x;', 'const char *label = "two"; return y;'),
    ("\n#if A\nreturn x;\n#endif\n", "\n#if B\nreturn y;\n#endif\n"),
])
def test_reuse_candidates_do_not_erase_constants_members_globals_calls_or_hidden_literals(tmp_path, first, second):
    source = "float A(float x) { " + first + " }\nfloat B(float y) { " + second + " }"
    result = module.source_review_observations(snapshot(tmp_path, source))
    assert result["duplicateBodies"]["candidateGroupCount"] == 0


def test_reuse_candidates_keep_parameter_modes_and_precision_qualifiers(tmp_path):
    source = """precise float A(precise float x) { return x; }
float B(float y) { return y; }
float C(out float z) { return z; }
"""
    result = module.source_review_observations(snapshot(tmp_path, source))
    assert result["duplicateBodies"]["candidateGroupCount"] == 0


def test_assignment_observations_count_rhs_tokens_lines_and_depth_without_comment_noise(tmp_path):
    source = """float MainPS(float2 input, float gain, float bias) {
    // float fake = (((((a + b)))));
    float value =
        ((gain * input.x) +
         (bias * input.y));
    float shortValue = gain;
    return value + shortValue;
}
"""
    assignments = module.source_review_observations(snapshot(tmp_path, source))["assignments"]
    assert assignments["assignmentCount"] == 2
    assert assignments["maximumTokenCount"] == 17
    assert assignments["maximumLineSpan"] == 2
    assert assignments["maximumParenthesisDepth"] == 2
    assert {key: value for key, value in assignments["examples"][0].items() if key not in {"candidateId", "startOffset", "endOffset", "target"}} == {
        "function": "MainPS", "assignmentLine": 3, "startLine": 4, "endLine": 5,
        "tokenCount": 17, "lineSpan": 2, "parenthesisDepth": 2}
    assert source[assignments["examples"][0]["startOffset"]] == "="
    assert assignments["examples"][0]["target"] == "value"


def test_assignment_depth_accounts_for_nested_arguments_chains_and_statement_boundaries(tmp_path):
    source = """void MainPS() {
    first = F((one = 1), ((second = 2)), 3);
    chained = middle = (inner = (4));
    after = 5;
}
"""
    assignments = module.source_review_observations(snapshot(tmp_path, source))["assignments"]
    assert assignments["assignmentCount"] == 7
    assert assignments["maximumParenthesisDepth"] == 3
    at_line = lambda line: [row for row in assignments["examples"] if row["assignmentLine"] == line]
    assert [row["parenthesisDepth"] for row in at_line(2)] == [3, 0, 0]
    assert [row["parenthesisDepth"] for row in at_line(3)] == [2, 2, 1]
    assert at_line(4)[0]["tokenCount"] == 1
    assert at_line(4)[0]["parenthesisDepth"] == 0


def test_long_assignment_chain_uses_linear_token_visits():
    # Count token reads rather than elapsed time: slow CI must not fail a
    # performance regression check, and overlapping RHS scans must be visible.
    visits = []
    class Token:
        def __init__(self, text):
            self.value = text
            self.line = 1
        @property
        def text(self):
            visits.append(None)
            return self.value
    length = 5000
    tokens = [Token("{")]
    for index in range(length):
        tokens.extend((Token("value%d" % index), Token("=")))
    tokens.extend((Token("1"), Token(";"), Token("}")))
    result = module._assignment_observations(tokens,
        [{"name": "Chain", "bodyStart": 0, "bodyEnd": len(tokens) - 1}])
    assert result["assignmentCount"] == length
    assert result["maximumTokenCount"] == 2 * length - 1
    assert len(visits) <= 3 * len(tokens)


def test_missing_comment_samples_include_later_larger_functions_not_only_front_adapters(tmp_path):
    adapters = "\n".join("float Adapter%d(float x) { return x; }" % index for index in range(13))
    source = adapters + """
float ShadeVariant1(float base, float light) {
    float result = base * light;
    result += saturate(light - base);
    return result;
}
"""
    comments = module.source_review_observations(snapshot(tmp_path, source))["functionComments"]
    assert comments["missingLeadingCommentCount"] == 14
    assert comments["examples"][0]["function"] == "ShadeVariant1"
    assert comments["exampleOrder"] == "descending-body-token-count-then-source-line-not-quality"
    assert comments["omittedCount"] + len(comments["examples"]) == 14


def test_masked_strings_comments_and_directives_do_not_invent_code_or_leading_comments(tmp_path):
    source = '''const char *name = "// comment\\\" float Fake() { float x = (((1))); }";
// Explain the first function.
float Explained(float x) { return x; }
/* Explain the second function.
 * It uses the input unchanged.
 */
[branch]
float AlsoExplained(float x) { return x; }
//
float EmptyComment(float x) { return x; } // This describes the preceding result.
float Missing(float x) { return x; }
const char *marker = "/* not a description */";
float StringIsNotComment(float x) { return x; }
#define PHANTOM float Phantom() { float hidden = ((1)); }
'''
    result = module.source_review_observations(snapshot(tmp_path, source))
    assert result["functionBodies"]["currentCount"] == 5
    assert result["assignments"]["assignmentCount"] == 0
    comments = result["functionComments"]
    assert comments["missingLeadingCommentCount"] == 3
    assert [row["function"] for row in comments["examples"]] == [
        "EmptyComment", "Missing", "StringIsNotComment"]


def test_nonconsecutive_infix_and_suffix_channel_names_are_navigation_facts(tmp_path):
    source = """float3 Reconstruct(float4 position) {
    precise float projectedXFromX = position.x * matrixRow0.x;
    precise float projectedXFromY = position.y * matrixRow1.x;
    precise float projectedXFromZ = position.z * matrixRow2.x;
    precise float projectedYFromX = position.x * matrixRow0.y;
    precise float projectedYFromY = position.y * matrixRow1.y;
    precise float projectedYFromZ = position.z * matrixRow2.y;
    precise float projectedZFromX = position.x * matrixRow0.z;
    precise float projectedZFromY = position.y * matrixRow1.z;
    precise float projectedZFromZ = position.z * matrixRow2.z;
    float primitiveTileOffsetX = floor(position.x);
    float primitiveLocalX = position.x - primitiveTileOffsetX;
    float primitiveTileOffsetY = floor(position.y);
    float primitiveLocalY = position.y - primitiveTileOffsetY;
    float primitiveTileOffsetZ = floor(position.z);
    float primitiveLocalZ = position.z - primitiveTileOffsetZ;
    return position.xyz;
}
"""
    result = module.source_review_observations(snapshot(tmp_path, source))
    assert result["channelExpressions"]["candidateCount"] == 0
    named = result["namedChannelFamilies"]
    assert named["candidateFamilyCount"] == 8
    by_pattern = {row["pattern"]: row for row in named["examples"]}
    assert [{key: row[key] for key in ("variable", "line")}
            for row in by_pattern["projected<component>FromX"]["variables"]] == [
        {"variable": "projectedXFromX", "line": 2},
        {"variable": "projectedYFromX", "line": 5},
        {"variable": "projectedZFromX", "line": 8},
    ]
    raw_patterns = {row["pattern"] for row in module._source_facts(
        source.encode(), result["sourceSha256"])["namedChannelFamilies"]["examples"]}
    assert "primitiveTileOffset<component>" in raw_patterns
    assert "primitiveLocal<component>" in raw_patterns
    assert "Naming facts only" in named["limitations"]


def test_named_families_allow_different_expressions_but_ignore_nonlocal_declarations(tmp_path):
    source = """float globalX, globalY, globalZ;
float3 MainPS(float argX, float argY, float argZ) {
    float colorR = DifferentRed();
    float another = 1;
    float colorG = DifferentGreen();
    float colorB = DifferentBlue();
    return float3(colorR, colorG, colorB);
}
"""
    result = module.source_review_observations(snapshot(tmp_path, source))
    assert result["channelExpressions"]["candidateCount"] == 0
    named = result["namedChannelFamilies"]
    assert named["candidateFamilyCount"] == 1
    assert named["examples"][0]["channels"] == "RGB"


def test_payload_keeps_small_samples_of_each_fact_class_and_counts_nested_omissions(tmp_path):
    source = "\n".join(PROBE.replace("BlendProbe", "BlendProbe" + str(index)) for index in range(60))
    result = module.source_review_observations(snapshot(tmp_path, source))
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= module.MAX_OBSERVATION_BYTES
    for key, count in [("channelExpressions", "candidateCount"), ("namedChannelFamilies", "candidateFamilyCount"),
                       ("duplicateBodies", "candidateGroupCount"), ("assignments", "assignmentCount"),
                       ("functionComments", "missingLeadingCommentCount")]:
        fact = result[key]
        assert fact["examples"], key
        assert len(fact["examples"]) + fact["omittedCount"] == fact[count]
    group = result["duplicateBodies"]["examples"][0]
    assert len(group["functions"]) >= 2
    assert len(group["functions"]) + group["omittedFunctions"] == 60
    assert result["callGraph"]["nodes"]
    assert result["functionBodies"]["examples"]["unchanged"]


def test_new_observations_are_cached_by_exact_source_and_not_mutated_by_payload_or_callers(tmp_path, monkeypatch):
    with module._CACHE_LOCK:
        module._CACHE.clear()
    original = module._named_channel_families
    count = []
    def counted(*args):
        count.append(True)
        return original(*args)
    monkeypatch.setattr(module, "_named_channel_families", counted)
    state = snapshot(tmp_path, PROBE)
    result = module.source_review_observations(state)
    result["assignments"]["examples"].clear()
    result["namedChannelFamilies"]["examples"].clear()
    result["functionComments"]["examples"].clear()
    again = module.source_review_observations(state)
    assert len(count) == 1
    assert again["assignments"]["examples"]
    assert again["namedChannelFamilies"]["examples"]
    assert again["functionComments"]["examples"]
    for index in range(module._CACHE_ENTRIES + 1):
        module.source_review_observations(snapshot(tmp_path, PROBE + "\n// identity %d" % index))
    assert len(module._CACHE) == module._CACHE_ENTRIES
    assert state["sourceSha256"] not in module._CACHE


def test_two_channel_light_detail_families_are_observed_before_a_third_channel_exists(tmp_path):
    source = """float2 MainPS(float3 adjusted, float scaleX, float scaleY) {
    float lightDetailWeightX = clamp(abs(adjusted.x) * 3.33f, 0.0f, 1.0f);
    float lightDetailWeightY = clamp(abs(adjusted.y) * 3.33f, 0.0f, 1.0f);
    float lightDetailShiftX = adjusted.x - (((adjusted.x < 0.0f) ? -1.0f : 1.0f) * scaleX);
    float lightDetailShiftY = adjusted.y - (((adjusted.y < 0.0f) ? -1.0f : 1.0f) * scaleY);
    return float2(lightDetailWeightX + lightDetailShiftX, lightDetailWeightY + lightDetailShiftY);
}
"""
    result = module.source_review_observations(snapshot(tmp_path, source))
    assert result["channelExpressions"]["candidateCount"] == 2
    assert [row["channels"] for row in result["channelExpressions"]["examples"]] == ["XY", "XY"]
    named = result["namedChannelFamilies"]
    assert named["candidateFamilyCount"] == 2
    assert {row["pattern"] for row in named["examples"]} == {
        "lightDetailWeight<component>", "lightDetailShift<component>"}


@pytest.mark.parametrize("expression,constructor,common,vector,order,channels", [
    ("float2(scale * normal.x, scale * normal.y)", "float2", "scale", "normal", "common-first", "xy"),
    ("float3(scale * normal.x, scale * normal.y, scale * normal.z)", "float3", "scale", "normal", "common-first", "xyz"),
    ("float3(normal.x * scale, normal.y * scale, normal.z * scale)", "float3", "scale", "normal", "vector-first", "xyz"),
    ("float3((scale) * (material.normal.x), ((scale) * (material.normal.y)), scale * material.normal.z)",
     "float3", "scale", "material.normal", "common-first", "xyz"),
    ("half4(weights.fade * color.r, weights.fade * color.g, weights.fade * color.b, weights.fade * color.a)",
     "half4", "weights.fade", "color", "common-first", "rgba"),
    ("float3(0.5f * color.x, 0.5f * color.y, 0.5f * color.z)", "float3", "0.5f", "color", "common-first", "xyz"),
])
def test_expanded_component_products_have_exact_navigation_facts(tmp_path, expression, constructor, common, vector, order, channels):
    source = "float3 Shade(float3 normal, float scale) {\n    return " + expression + ";\n}"
    result = module.source_review_observations(snapshot(tmp_path, source))
    products = result["expandedVectorProducts"]
    assert products["candidateCount"] == 1
    row = products["examples"][0]
    assert {key: value for key, value in row.items() if key not in {"candidateId", "startOffset", "endOffset"}} == {
        "function": "Shade", "startLine": 2, "endLine": 2, "constructor": constructor,
        "commonValue": common, "vectorValue": vector, "operandOrder": order, "channels": channels}
    assert source[row["startOffset"]:row["endOffset"]] == expression
    assert "no type or safe-rewrite proof" in products["limitations"]


@pytest.mark.parametrize("expression", [
    "float3(scale * normal.x, other * normal.y, scale * normal.z)",
    "float3(scale * normal.x, scale * normal.z, scale * normal.y)",
    "float3(scale * normal.x, normal.y * scale, scale * normal.z)",
    "float3(scale * normal.x, scale * other.y, scale * normal.z)",
    "float3(Read() * normal.x, Read() * normal.y, Read() * normal.z)",
    "float3(values[i] * normal.x, values[i] * normal.y, values[i] * normal.z)",
    "float3(scale * vectors[i].x, scale * vectors[i].y, scale * vectors[i].z)",
    "float3(scale++ * normal.x, scale++ * normal.y, scale++ * normal.z)",
    "float3((scale + bias) * normal.x, (scale + bias) * normal.y, (scale + bias) * normal.z)",
    "float3(normal.x, normal.y, normal.z)",
    "float3(scale * normal)",
    "float3(scale * normal.x,\n#if B\nscale * normal.y,\n#endif\nscale * normal.z)",
    '"float3(scale * normal.x, scale * normal.y, scale * normal.z)"',
    "/* float3(scale * normal.x, scale * normal.y, scale * normal.z) */ 1",
])
def test_expanded_products_do_not_claim_arbitrary_algebra_calls_or_changed_evaluation(tmp_path, expression):
    source = "float3 MainPS() { return " + expression + "; }"
    result = module.source_review_observations(snapshot(tmp_path, source))
    assert result["expandedVectorProducts"]["candidateCount"] == 0


def test_nested_constructor_inspections_are_bounded():
    visits = []
    class Token:
        def __init__(self, word, offset):
            self.word, self.offset, self.line = word, offset, 1
        @property
        def text(self):
            visits.append(None)
            return self.word
    words = ["{"] + [word for _ in range(2000) for word in ("float3", "(")] + ["1"] + [")"] * 2000 + ["}"]
    tokens = [Token(word, index) for index, word in enumerate(words)]
    result = module._expanded_vector_products(" " * len(tokens), tokens,
        [{"name": "Nested", "bodyStart": 0, "bodyEnd": len(tokens) - 1}])
    assert result["candidateCount"] == 0
    assert len(visits) < 20 * len(tokens)


def test_source_candidate_ids_are_unique_source_bound_and_only_reference_visible_facts(tmp_path):
    source = PROBE + "\nfloat3 Expand(float3 value, float scale) { float a = 1; float b = 2; return float3(scale * value.x, scale * value.y, scale * value.z); }"
    state = snapshot(tmp_path, source)
    first = module.source_review_observations(state)
    candidates = module.review_candidates(first)
    ids = [candidate["id"] for candidate in candidates]
    assert len(ids) == len(set(ids))
    assert all(candidate["functions"] for candidate in candidates)
    assert {candidate["category"] for candidate in candidates} >= {
        "channelExpressions", "namedChannelFamilies", "expandedVectorProducts", "assignments", "functionComments"}
    assert not {"callGraph", "functionBodies"} & {candidate["category"] for candidate in candidates}
    assert module.review_candidates(module.source_review_observations(state)) == candidates
    changed = module.source_review_observations(snapshot(tmp_path, source + "\n// source changed"))
    assert not set(ids) & {candidate["id"] for candidate in module.review_candidates(changed)}
    # Same-line, same-size assignments are separate facts even when their
    # bounded display locations happen to be identical.
    repeated = module.source_review_observations(snapshot(tmp_path, "void MainPS() { float a = 1; float b = 2; }"))
    assignments = repeated["assignments"]["examples"]
    assert len(assignments) == 2
    assert assignments[0]["candidateId"] != assignments[1]["candidateId"]


def test_candidate_function_names_are_not_truncated_for_scope_review_binding(tmp_path):
    name = "Compute" + "DetailedLighting" * 12
    result = module.source_review_observations(snapshot(tmp_path, PROBE.replace("BlendProbe", name)))
    candidates = module.review_candidates(result)
    assert candidates
    assert all(candidate["functions"] == [name] for candidate in candidates)


def test_constructor_and_candidate_id_samples_share_existing_payload_budget(tmp_path):
    source = "\n".join(PROBE.replace("BlendProbe", "BlendProbe%d" % index) for index in range(30))
    source += "\n" + "\n".join(
        "float3 Expand%d(float3 value, float scale) { return float3(scale * value.x, scale * value.y, scale * value.z); }" % index
        for index in range(30))
    result = module.source_review_observations(snapshot(tmp_path, source))
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= module.MAX_OBSERVATION_BYTES
    for category in module._REVIEW_CANDIDATE_CATEGORIES:
        rows = result[category]["examples"]
        assert rows, category
        assert all(row["candidateId"] for row in rows)
    candidates = module.review_candidates(result)
    assert len(candidates) == sum(len(result[category]["examples"]) for category in module._REVIEW_CANDIDATE_CATEGORIES)
    assert result["expandedVectorProducts"]["candidateCount"] == 30
    assert len(result["expandedVectorProducts"]["examples"]) + result["expandedVectorProducts"]["omittedCount"] == 30


def test_catalog_retains_every_match_beyond_first_eight_functions_and_within_one_body(tmp_path):
    source = "\n".join(PROBE.replace("BlendProbe", "BlendProbe%d" % index) for index in range(12))
    source += "\nfloat3 LongBody(float3 value, float scale) {\n"
    for index in range(20):
        source += "float weight%dX = scale * value.x; float weight%dY = scale * value.y;\n" % (index, index)
        source += "float3 product%d = float3(scale * value.x, scale * value.y, scale * value.z);\n" % index
    source += "return value; }\n"
    state = snapshot(tmp_path, source)
    catalog = module.source_review_catalog(state)
    preview = module.source_review_observations(state)
    assert catalog["schema"] == module.CATALOG_SCHEMA
    assert catalog["complete"] is True
    assert preview["complete"] is False
    assert preview["catalog"]["complete"] is True
    assert "not-semantic" in catalog["completenessBasis"]
    for category, count, expected in [
        ("channelExpressions", "candidateCount", 44),
        ("namedChannelFamilies", "candidateFamilyCount", 44),
        ("expandedVectorProducts", "candidateCount", 20),
        ("functionComments", "missingLeadingCommentCount", 13),
    ]:
        facts = catalog[category]
        assert facts[count] == expected
        assert len(facts["examples"]) == expected
        assert facts["omittedCount"] == 0
    assert len(catalog["duplicateBodies"]["examples"][0]["functions"]) == 12
    assert catalog["duplicateBodies"]["examples"][0]["omittedFunctions"] == 0
    assert len(catalog["functionBodies"]["examples"]["unchanged"]) == 13
    assert len(catalog["callGraph"]["nodes"]) == 13
    assert len(catalog["assignments"]["examples"]) == 8
    assert "top-8" in catalog["assignments"]["selection"]
    assert catalog["assignments"]["omittedCount"] == catalog["assignments"]["assignmentCount"] - 8
    assert len(json.dumps(preview, ensure_ascii=False, separators=(",", ":")).encode()) <= 6144
    all_candidates = module.review_candidates(catalog)
    assert preview["catalog"]["candidateCount"] == len(all_candidates)
    encoded = json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert preview["catalog"]["sha256"] == hashlib.sha256(encoded).hexdigest()
    ids = {row["id"] for row in all_candidates}
    assert len(ids) == len(all_candidates)
    assert {row["id"] for row in module.review_candidates(preview)} < ids
    assert any(row["variables"] == ["weight19X", "weight19Y"]
               for row in catalog["channelExpressions"]["examples"])


def test_catalog_keeps_all_duplicate_groups_members_and_disconnected_call_nodes(tmp_path):
    source = "\n".join("float Helper%d_%d(float value) { return value + %d; }" % (group, member, group)
                       for group in range(12) for member in range(10))
    source += "\nfloat MainPS(float value) { return " + " + ".join(
        "Helper%d_0(value)" % group for group in range(12)) + "; }"
    state = snapshot(tmp_path, source, entryPoint="MainPS")
    catalog = module.source_review_catalog(state)
    assert catalog["duplicateBodies"]["candidateGroupCount"] == 12
    assert len(catalog["duplicateBodies"]["examples"]) == 12
    assert all(len(row["functions"]) == 10 for row in catalog["duplicateBodies"]["examples"])
    assert len(catalog["callGraph"]["nodes"]) == 121
    assert catalog["callGraph"]["entryReachableCount"] == 13
    assert len(catalog["callGraph"]["nodes"][0]["calls"]) == 12
    assert catalog["callGraph"]["omittedFunctions"] == 0
    assert all(row["omittedCallees"] == 0 for row in catalog["callGraph"]["nodes"])


def test_catalog_ids_bind_original_occurrences_not_sampling_order(tmp_path, monkeypatch):
    source = PROBE + "\nfloat3 Products(float3 v, float s) { return float3(s*v.x,s*v.y,s*v.z) + float3(s*v.x,s*v.y,s*v.z); }"
    state = snapshot(tmp_path, source)
    first = module.source_review_catalog(state)
    products = first["expandedVectorProducts"]["examples"]
    assert len({row["candidateId"] for row in products}) == 2
    assert len({row["startOffset"] for row in products}) == 2
    with module._CACHE_LOCK:
        module._CACHE[state["sourceSha256"]]["channelExamples"].reverse()
    second = module.source_review_catalog(state)
    assert {row["id"] for row in module.review_candidates(first)} == {
        row["id"] for row in module.review_candidates(second)}
    monkeypatch.setattr(module, "MAX_OBSERVATION_BYTES", 5400)
    preview = module.source_review_observations(state)
    assert {row["id"] for row in module.review_candidates(preview)} <= {
        row["id"] for row in module.review_candidates(first)}
    assert (tmp_path / "source.hlsl").read_text() == source


def test_catalog_and_preview_share_parse_cache_without_mutating_each_other(tmp_path, monkeypatch):
    with module._CACHE_LOCK:
        module._CACHE.clear()
    parse = module._function_records
    parsed = []
    def counted(source):
        parsed.append(source)
        return parse(source)
    monkeypatch.setattr(module, "_function_records", counted)
    state = snapshot(tmp_path, PROBE)
    first = module.source_review_catalog(state)
    first["namedChannelFamilies"]["examples"].clear()
    preview = module.source_review_observations(state)
    again = module.source_review_catalog(state)
    assert len(parsed) == 1
    assert again["namedChannelFamilies"]["candidateFamilyCount"] == 2
    assert len(again["namedChannelFamilies"]["examples"]) == 2
    assert preview["catalog"]["sha256"] == module._digest(again)
    (tmp_path / "source.hlsl").write_text(PROBE + "// changed", encoding="utf-8")
    with pytest.raises(ValueError, match="Current source changed"):
        module.source_review_catalog(state)


def test_catalog_keeps_same_named_family_occurrences_in_distinct_scopes(tmp_path):
    source = """float MainPS(float2 v) {
    { float valueX = v.x; float valueY = v.y; }
    { float valueX = Different(v.x); float valueY = v.y + 1; }
    return 0;
}"""
    catalog = module.source_review_catalog(snapshot(tmp_path, source))
    family = catalog["namedChannelFamilies"]["examples"][0]
    assert len(family["variables"]) == 4
    assert len({row["offset"] for row in family["variables"]}) == 4
    assert (family["startLine"], family["endLine"]) == (2, 3)
    assert "scope" in catalog["namedChannelFamilies"]["limitations"]
    candidate = next(row for row in module.review_candidates(catalog)
                     if row["category"] == "namedChannelFamilies")
    assert candidate["symbols"] == ["valueX", "valueY"]
    assert [(row["startLine"], row["endLine"]) for row in candidate["locations"]] == [(2, 2), (3, 3), (2, 2), (3, 3)]
    assert {source[row["startOffset"]:row["endOffset"]] for row in candidate["locations"]} == {"valueX", "valueY"}


def test_catalog_candidate_symbols_are_full_names_and_identify_each_fact(tmp_path):
    long_name = "normalized" + "CapturedLightDirection" * 8
    source = "float3 MainPS(float3 %s, float scale) {" % long_name
    source += "float weightX = 1; float weightY = 2;"
    source += "float3 output = float3(scale * %s.x, scale * %s.y, scale * %s.z);" % ((long_name,) * 3)
    source += "return output; }"
    candidates = module.review_candidates(module.source_review_catalog(snapshot(tmp_path, source)))
    expanded = next(row for row in candidates if row["category"] == "expandedVectorProducts")
    assert expanded["symbols"] == ["scale", long_name]
    assignments = [row for row in candidates if row["category"] == "assignments"]
    assert {symbol for row in assignments for symbol in row["symbols"]} == {"weightX", "weightY", "output"}
