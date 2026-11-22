from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from src.shader_workspace.flow_graph import (
    FLOW_GRAPH_SCHEMA,
    MAX_GRAPH_NODES,
    MODULE_DESCRIPTIONS_SCHEMA,
    ShaderFlowGraphError,
    build_shader_flow_graph,
    load_workspace_flow_graph_status,
    module_description_paging,
    module_description_template,
    validate_module_descriptions,
    validate_shader_flow_graph,
)
from src.shader_workspace.equivalence import mermaid_from_flow_graph


class ShaderFlowGraphTests(unittest.TestCase):
    WORKSPACE_ID = "1" * 32

    @staticmethod
    def _build(source: str, **overrides):
        arguments = {
            "workspace_id": ShaderFlowGraphTests.WORKSPACE_ID,
            "source_path": "source/shader.hlsl",
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "source_revision": 7,
            "stage": "fragment",
            "entry_point": "MainPS",
            "generated_at": "2026-08-31T00:00:00Z",
            "origin": "organized-proposal",
            "descriptions": {"ApplyLighting": "Evaluates the lighting contribution."},
        }
        arguments.update(overrides)
        return build_shader_flow_graph(source, **arguments)

    def test_draft_graph_accepts_existing_compile_history_without_authorizing_replay(self) -> None:
        source = "float4 MainPS() : SV_Target0 { return 1; }"
        for revision in (0, 124):
            with self.subTest(revision=revision):
                graph = self._build(source, source_revision=revision, origin="editable-draft")
                validated = validate_shader_flow_graph(
                    graph, expected_source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                    expected_source_revision=revision,
                )
                self.assertTrue(validated["policy"]["navigationOnly"])
                self.assertFalse(validated["policy"]["compileAuthorization"])
                self.assertFalse(validated["policy"]["applyAuthorization"])
        with self.assertRaisesRegex(ShaderFlowGraphError, "source revision"):
            self._build(source, source_revision=0, origin="organized-proposal")

    def test_extracts_calls_locations_and_bounded_ai_descriptions(self) -> None:
        source = (
            "#define PretendCall(x) Ignored(x)\n"
            "// float4 CommentedOut() { return 0; }\n"
            "Texture2D<float4> Tex : register(t0);\n"
            "float4 SampleBase(float2 uv) { return Tex.Sample(Samp, uv); }\n"
            "float3 ApplyLighting(float3 value) { return value; }\n"
            "[shader(\"pixel\")]\n"
            "float4 MainPS(float2 uv : TEXCOORD0) : SV_Target0\n"
            "{\n"
            "  return float4(ApplyLighting(SampleBase(uv).rgb), 1.0);\n"
            "}\n"
        )

        graph = self._build(source)

        self.assertEqual(graph["schema"], FLOW_GRAPH_SCHEMA)
        self.assertEqual(
            [node["name"] for node in graph["nodes"]],
            ["MainPS", "ApplyLighting", "SampleBase"],
        )
        by_name = {node["name"]: node for node in graph["nodes"]}
        self.assertEqual(by_name["MainPS"]["kind"], "entry-point")
        self.assertEqual(by_name["MainPS"]["source"]["line"], 9)
        self.assertEqual(by_name["MainPS"]["source"]["column"], 3)
        self.assertEqual(
            by_name["ApplyLighting"]["description"],
            "Evaluates the lighting contribution.",
        )
        edges = {
            (edge["source"], edge["target"])
            for edge in graph["edges"]
        }
        self.assertEqual(
            edges,
            {
                (by_name["MainPS"]["id"], by_name["ApplyLighting"]["id"]),
                (by_name["MainPS"]["id"], by_name["SampleBase"]["id"]),
            },
        )
        self.assertTrue(graph["policy"]["navigationOnly"])
        self.assertFalse(graph["policy"]["compileAuthorization"])
        self.assertFalse(graph["policy"]["applyAuthorization"])

    def test_flow_spec_stays_separate_from_function_graph_and_mermaid(self) -> None:
        source = (
            "float3 ApplyLighting(float3 surfaceColor) { return surfaceColor; }\n"
            "float4 MainPS(float2 uv : TEXCOORD0) : SV_Target0\n"
            "{\n"
            "  float3 implementationValue = ApplyLighting(float3(uv, 0));\n"
            "  return float4(implementationValue, 1);\n"
            "}\n"
        )
        flow_spec = {
            "stages": [
                {
                    "id": "implementation",
                    "label": "float3 leakedValue = ApplyLighting(uv);",
                    "function": "MainPS",
                    "sourceRange": {"startLine": 4, "endLine": 4},
                    "reads": ["uv"],
                    "writes": ["implementationValue"],
                    "next": [],
                }
            ]
        }

        graph = self._build(source, flow_spec=flow_spec)

        self.assertNotIn("graphKind", graph)
        self.assertEqual(
            {node["kind"] for node in graph["nodes"]},
            {"entry-point", "function"},
        )
        self.assertTrue(all(edge["kind"] == "calls" for edge in graph["edges"]))
        by_name = {node["name"]: node for node in graph["nodes"]}
        self.assertEqual(by_name["MainPS"]["inputs"], ["uv"])
        self.assertEqual(by_name["MainPS"]["outputs"], ["SV_Target0"])
        self.assertIn("MainPS(in float2 uv)", by_name["MainPS"]["signature"])
        self.assertEqual(by_name["ApplyLighting"]["inputs"], ["surfaceColor"])
        self.assertEqual(by_name["ApplyLighting"]["outputs"], ["return"])

        diagram = mermaid_from_flow_graph(graph)
        self.assertIn("in: uv", diagram)
        self.assertIn("out: SV_Target0", diagram)
        self.assertNotIn("signature:", diagram)
        self.assertNotIn("implementationValue", diagram)
        self.assertNotIn("leakedValue", diagram)

        with self.assertRaisesRegex(ShaderFlowGraphError, "implementation text"):
            self._build(
                source,
                descriptions={"MainPS": "float hiddenValue = uv.x"},
            )

    def test_graph_id_ignores_generation_time_but_binds_source_revision(self) -> None:
        source = "float4 MainPS() : SV_Target { return 1.0; }\n"
        first = self._build(source)
        second = self._build(
            source, generated_at="2026-08-31T01:00:00Z"
        )
        changed_revision = self._build(source, source_revision=8)

        self.assertEqual(first["graphId"], second["graphId"])
        self.assertNotEqual(first["graphId"], changed_revision["graphId"])

    def test_macro_declared_entry_still_has_a_navigation_node(self) -> None:
        source = (
            "#define DECLARE_PIXEL(name) float4 name() : SV_Target { return 1; }\n"
            "DECLARE_PIXEL(MainPS)\n"
        )

        graph = self._build(source)

        self.assertEqual(len(graph["nodes"]), 1)
        self.assertEqual(graph["nodes"][0]["name"], "MainPS")
        self.assertEqual(graph["nodes"][0]["kind"], "entry-point")
        self.assertEqual(graph["nodes"][0]["source"]["line"], 2)

    def test_description_template_and_validation_are_plain_and_bounded(self) -> None:
        template = module_description_template(
            "float Helper() { return 1; }\n"
            "float4 MainPS() : SV_Target { return Helper(); }\n"
        )
        self.assertEqual(template["schema"], MODULE_DESCRIPTIONS_SCHEMA)
        self.assertEqual(
            [item["name"] for item in template["modules"]],
            ["Helper", "MainPS"],
        )
        template["modules"][0]["description"] = "Computes an intermediate value."
        self.assertEqual(
            validate_module_descriptions(template)["Helper"],
            "Computes an intermediate value.",
        )
        invalid = copy.deepcopy(template)
        invalid["modules"][0]["description"] = "two\nlines"
        with self.assertRaisesRegex(ShaderFlowGraphError, "single-line"):
            validate_module_descriptions(invalid)

    def test_function_comments_reject_code_fragments_without_semicolons(self) -> None:
        template = module_description_template(
            "float Evaluate(float value) { return value; }\n"
            "float4 MainPS() : SV_Target { return Evaluate(1); }\n"
        )
        implementation_fragments = (
            "Evaluate(value)",
            "Calls Evaluate(value) for the result",
            "if (mask) return the sampled value",
            "float3 normalValue",
            "values[index] supplies the result",
            "result ? left : right",
        )
        for fragment in implementation_fragments:
            invalid = copy.deepcopy(template)
            invalid["modules"][0]["description"] = fragment
            with self.subTest(fragment=fragment), self.assertRaisesRegex(
                ShaderFlowGraphError, "implementation"
            ):
                validate_module_descriptions(invalid)

        valid = copy.deepcopy(template)
        valid["modules"][0]["description"] = (
            "Evaluates the source-defined contribution from the supplied value."
        )
        self.assertIn("Evaluate", validate_module_descriptions(valid))

    def test_legacy_host_entry_summary_does_not_relax_implementation_filter(self) -> None:
        source = "float4 MainPS() : SV_Target { return 1.0; }\n"
        legacy = "Shader entry point; the stage result is produced here."
        graph = self._build(source, descriptions={"MainPS": legacy})
        self.assertEqual(validate_shader_flow_graph(graph)["nodes"][0]["description"], legacy)
        self.assertTrue(graph["policy"]["navigationOnly"])
        self.assertFalse(graph["policy"]["applyAuthorization"])
        for description in (legacy + " return 1;", "return;", "discard;",
                            "float value = 1;", "Evaluate(value);",
                            "Shader entry point; return result;"):
            with self.subTest(description=description), self.assertRaisesRegex(
                    ShaderFlowGraphError, "implementation"):
                self._build(source, descriptions={"MainPS": description})

    def test_node_limit_is_explicit_and_entry_point_is_retained(self) -> None:
        helpers = "".join(
            "float Helper%d() { return Helper%d(); }\n" % (index, index + 1)
            if index < MAX_GRAPH_NODES + 7 else
            "float Helper%d() { return 1.0; }\n" % index
            for index in range(MAX_GRAPH_NODES + 8)
        )
        source = helpers + "float4 MainPS() : SV_Target { return Helper0(); }\n"

        graph = self._build(source)

        self.assertEqual(len(graph["nodes"]), MAX_GRAPH_NODES)
        self.assertEqual(graph["nodes"][0]["name"], "MainPS")
        self.assertTrue(graph["truncated"]["nodes"])
        self.assertEqual(graph["truncated"]["omittedNodes"], 9)
        self.assertEqual(graph["truncated"]["omittedEdges"], 9)

    def test_unreachable_helper_is_not_a_disconnected_flowchart_root(self) -> None:
        source = (
            "float Orphan() { return 0.0; }\n"
            "float Used() { return 1.0; }\n"
            "float4 MainPS() : SV_Target { return Used(); }\n"
        )
        graph = self._build(source)
        self.assertEqual([node["name"] for node in graph["nodes"]],
                         ["MainPS", "Used"])
        self.assertFalse(graph["truncated"]["nodes"])
        self.assertNotIn("Orphan", mermaid_from_flow_graph(graph))

    def test_final_source_comment_precedes_stale_module_description(self) -> None:
        source = (
            "// 计算归一化法线。\n"
            "float3 NormalizeSurface(float3 normal) { return normalize(normal); }\n"
            "/* Compute the final output. */\n"
            "float4 MainPS(float3 normal) : SV_Target {\n"
            "  return float4(NormalizeSurface(normal), 1.0);\n"
            "}\n"
        )
        graph = self._build(source, descriptions={
            "NormalizeSurface": "An obsolete description.",
            "MainPS": "Another obsolete description.",
        })
        by_name = {node["name"]: node for node in graph["nodes"]}
        self.assertEqual(by_name["NormalizeSurface"]["description"],
                         "计算归一化法线。")
        self.assertEqual(by_name["MainPS"]["description"],
                         "Compute the final output.")
        no_comment = self._build(
            "float4 MainPS() : SV_Target { return 1.0; }\n",
            descriptions={"MainPS": "Fallback summary from a reviewed module."},
        )
        self.assertEqual(no_comment["nodes"][0]["description"],
                         "Fallback summary from a reviewed module.")

    def test_description_coverage_can_be_batched_beyond_visible_graph_limit(self) -> None:
        function_count = MAX_GRAPH_NODES + 44
        source = "".join(
            "float Helper%d(float value) { return value; }\n" % index
            for index in range(function_count)
        )
        template = module_description_template(source)
        paging = module_description_paging(source)
        self.assertEqual(len(template["modules"]), MAX_GRAPH_NODES)
        self.assertEqual(paging["totalFunctions"], function_count)
        self.assertTrue(paging["hasMore"])
        self.assertEqual(paging["nextOutlineOffset"], MAX_GRAPH_NODES)
        self.assertGreater(paging["maximumDocumentBytes"], 0)
        self.assertEqual(paging["mergeKey"], "modules[].name")

        # This models the final result of several modules-by-name draft merges.
        # The graph itself stays visually bounded, while whole-Shader review can
        # still require a description for every function in the source.
        merged = {
            "schema": MODULE_DESCRIPTIONS_SCHEMA,
            "schemaVersion": 1,
            "modules": [
                {
                    "name": "Helper%d" % index,
                    "description": "Returns the source-defined value for this helper.",
                }
                for index in range(function_count)
            ],
        }
        self.assertEqual(len(validate_module_descriptions(merged)), function_count)

    def test_validation_rejects_path_escape_and_content_tamper(self) -> None:
        source = "float4 MainPS() : SV_Target { return 1.0; }\n"
        graph = self._build(source)
        escaped = copy.deepcopy(graph)
        escaped["nodes"][0]["source"]["path"] = "../outside.hlsl"
        with self.assertRaisesRegex(ShaderFlowGraphError, "within"):
            validate_shader_flow_graph(escaped)

        tampered = copy.deepcopy(graph)
        tampered["nodes"][0]["description"] = "Unbound changed text."
        with self.assertRaisesRegex(ShaderFlowGraphError, "graphId"):
            validate_shader_flow_graph(tampered)

    def test_workspace_projection_marks_source_changes_stale(self) -> None:
        source = "float4 MainPS() : SV_Target { return 1.0; }\n"
        graph = self._build(source, descriptions={
            "MainPS": "Shader entry point; the stage result is produced here.",
        })
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ai").mkdir()
            (root / "ai" / "flow-graph.json").write_text(
                json.dumps(graph), encoding="utf-8"
            )

            current = load_workspace_flow_graph_status(
                root,
                workspace_id=self.WORKSPACE_ID,
                source_sha256=graph["sourceSha256"],
                source_revision=7,
                source_path="source/shader.hlsl",
            )
            stale = load_workspace_flow_graph_status(
                root,
                workspace_id=self.WORKSPACE_ID,
                source_sha256="2" * 64,
                source_revision=8,
                source_path="source/shader.hlsl",
            )

        self.assertTrue(current["available"])
        self.assertEqual(current["status"], "current")
        self.assertFalse(current["stale"])
        self.assertEqual(stale["status"], "stale")
        self.assertTrue(stale["stale"])


if __name__ == "__main__":
    unittest.main()
