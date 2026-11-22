from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from src.pass_classification import (
    KNOWLEDGE_SCHEMA,
    PassClassificationError,
    PassKnowledgeStore,
    apply_ai_semantic_proposals,
    classify_passes,
)

CAPTURE_A = "a" * 64
CAPTURE_B = "b" * 64
PS_A = "1" * 64
VS_A = "2" * 64


def _pipeline(
    *,
    render_targets=None,
    depth_target=None,
    blend=None,
    depth_test=None,
    depth_write=None,
    reads=None,
    fullscreen=None,
    shader_name="",
):
    value = {
        "renderTargets": [] if render_targets is None else render_targets,
        "depthTarget": depth_target,
        "blendEnabled": blend,
        "depthTestEnabled": depth_test,
        "depthWriteEnabled": depth_write,
        "readResources": [] if reads is None else reads,
        "shaders": {
            "vertex": {"shaderId": 10, "rawSha256": VS_A},
            "fragment": {
                "shaderId": 11,
                "rawSha256": PS_A,
                "displayName": shader_name,
            },
        },
    }
    if fullscreen is not None:
        value["fullscreen"] = fullscreen
    return value


class PassClassificationTests(unittest.TestCase):
    def test_depth_only_is_confirmed_from_pipeline_shape(self) -> None:
        result = classify_passes(
            [{"eventId": 10, "name": "DrawIndexed", "flags": 0x0002}],
            pipeline_states={10: _pipeline(depth_target=900)},
            capture_id=CAPTURE_A,
        )
        item = result["items"][0]
        self.assertEqual(item["category"], "Depth")
        self.assertEqual(item["subcategory"], "DepthOnly")
        self.assertEqual(item["confidence"], "confirmed")
        self.assertEqual(item["authority"], "FixedProgram")
        self.assertFalse(item["semanticVerified"])
        self.assertEqual(item["evidence"][0]["code"], "depth-only-target")
        self.assertRegex(item["passFingerprint"], r"^[0-9a-f]{64}$")

    def test_shadow_marker_and_depth_shape_select_shadow(self) -> None:
        result = classify_passes(
            [
                {
                    "eventId": 20,
                    "name": "DrawIndexed",
                    "flags": 0x0002,
                    "markerPath": ["Frame", "CSM ShadowDepth"],
                }
            ],
            pipeline_states={20: _pipeline(depth_target=901)},
        )
        item = result["items"][0]
        self.assertEqual(item["category"], "Shadow")
        self.assertEqual(item["subcategory"], "CascadedShadow")
        self.assertEqual(item["confidence"], "probable")
        self.assertEqual(
            {entry["source"] for entry in item["evidence"]},
            {"markerPath", "pipelineState"},
        )

    def test_graphics_semantic_categories_use_independent_fixed_evidence(self) -> None:
        actions = [
            {"eventId": 1, "name": "BasePass Opaque", "flags": 2},
            {"eventId": 2, "name": "Draw", "flags": 2, "markerPath": ["GBuffer"]},
            {"eventId": 3, "name": "Water Translucency", "flags": 2},
            {"eventId": 4, "name": "ToneMap", "flags": 2},
            {"eventId": 5, "name": "HUD Canvas", "flags": 2},
            {"eventId": 6, "name": "Deferred Lighting", "flags": 2},
        ]
        states = {
            1: _pipeline(
                render_targets=[101], depth_target=201, blend=False, depth_write=True
            ),
            2: _pipeline(
                render_targets=[101, 102, 103],
                depth_target=201,
                blend=False,
                depth_write=True,
            ),
            3: _pipeline(
                render_targets=[101], depth_target=201, blend=True, depth_write=False
            ),
            4: _pipeline(
                render_targets=[101], blend=False, depth_write=False, fullscreen=True
            ),
            5: _pipeline(
                render_targets=[101], blend=True, depth_test=False, depth_write=False
            ),
            6: _pipeline(
                render_targets=[101],
                reads=[1, 2, 3, 4],
                fullscreen=True,
                depth_write=False,
            ),
        }
        result = classify_passes(actions, pipeline_states=states)
        categories = {item["eventId"]: item["category"] for item in result["items"]}
        self.assertEqual(
            categories,
            {
                1: "BasePass",
                2: "GBuffer",
                3: "Transparent",
                4: "Post",
                5: "UI",
                6: "Lighting",
            },
        )
        self.assertTrue(all(item["evidence"] for item in result["items"]))
        self.assertEqual(
            [node["label"] for node in result["tree"]],
            ["BasePass", "GBuffer", "Lighting", "Transparent", "Post", "UI"],
        )

    def test_dispatch_stays_compute_and_reports_semantic_role(self) -> None:
        result = classify_passes(
            [
                {
                    "eventId": 30,
                    "name": "TAA Upscale",
                    "flags": 0x0004,
                    "markerPath": ["PostProcess"],
                }
            ],
            pipeline_states={30: _pipeline(render_targets=[100])},
        )
        item = result["items"][0]
        self.assertEqual(item["category"], "Compute")
        self.assertEqual(item["confidence"], "confirmed")
        self.assertEqual(item["semanticRole"], "Post")

    def test_utility_actions_are_exact_action_categories(self) -> None:
        result = classify_passes(
            [
                {"eventId": 40, "name": "ClearRenderTarget", "flags": 0x0001},
                {"eventId": 41, "name": "Resolve", "flags": 0x0800},
                {"eventId": 42, "name": "Copy", "flags": 0x0400},
            ]
        )
        self.assertEqual(
            [(item["category"], item["subcategory"]) for item in result["items"]],
            [("Utility", "Clear"), ("Utility", "Resolve"), ("Utility", "Copy")],
        )
        self.assertTrue(
            all(item["confidence"] == "confirmed" for item in result["items"])
        )

    def test_execute_indirect_parent_is_container_and_children_are_not_duplicated(
        self,
    ) -> None:
        nested = [
            {
                "eventId": 50,
                "name": "ExecuteIndirect(2)",
                "flags": 0x0200,
                "children": [
                    {
                        "eventId": 51,
                        "name": "DrawIndexedIndirect[0]",
                        "flags": 0x080002,
                    },
                    {
                        "eventId": 52,
                        "name": "DrawIndexedIndirect[1]",
                        "flags": 0x080002,
                    },
                ],
            },
            # A duplicate flattened copy from an adapter must not become a
            # second leaf.
            {
                "eventId": 51,
                "name": "DrawIndexedIndirect[0]",
                "flags": 0x080002,
                "markerPath": ["ExecuteIndirect(2)"],
            },
        ]
        states = {
            51: _pipeline(
                render_targets=[101], depth_target=201, blend=False, depth_write=True
            ),
            52: _pipeline(
                render_targets=[101], depth_target=201, blend=False, depth_write=True
            ),
        }
        result = classify_passes(nested, pipeline_states=states)
        self.assertEqual(result["summary"]["containerCount"], 1)
        self.assertEqual(result["summary"]["actionCount"], 2)
        self.assertEqual(result["summary"]["duplicateCount"], 1)
        self.assertEqual([item["eventId"] for item in result["items"]], [51, 52])
        self.assertEqual(result["summary"]["executionTimeline"], [50, 51, 52])
        self.assertEqual(
            result["summary"]["actionExecutionTimeline"], [51, 52]
        )
        self.assertTrue(all(item["indirect"] for item in result["items"]))
        groups = result["tree"][0]["children"][0]["children"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["nodeType"], "container")
        self.assertEqual(groups[0]["count"], 2)
        self.assertTrue(groups[0]["foldByDefault"])

    def test_markers_become_context_not_empty_action_leaves(self) -> None:
        result = classify_passes(
            [
                {"eventId": 60, "name": "BasePass", "flags": 0x0040},
                {
                    "eventId": 61,
                    "name": "Draw",
                    "flags": 2,
                    "markerPath": ["BasePass"],
                },
            ],
            pipeline_states={61: _pipeline(render_targets=[1], depth_target=2)},
        )
        self.assertEqual([item["eventId"] for item in result["items"]], [61])
        self.assertEqual(result["summary"]["executionTimeline"], [60, 61])
        self.assertEqual(result["summary"]["actionExecutionTimeline"], [61])
        self.assertEqual(
            result["omittedEvents"][0]["reason"], "marker-used-as-tree-context"
        )

    def test_shader_catalog_names_and_hashes_are_joined_by_event(self) -> None:
        catalog = {
            "shaders": [
                {
                    "shaderKey": "11:fragment:main:x",
                    "shaderResourceId": 11,
                    "stage": "fragment",
                    "rawSha256": PS_A,
                    "displayName": "DeferredLightingPS",
                }
            ],
            "uses": [
                {
                    "eventId": 70,
                    "shaderKey": "11:fragment:main:x",
                    "resourceId": 11,
                    "stage": "fragment",
                }
            ],
        }
        state = _pipeline(render_targets=[100], reads=[1, 2, 3], fullscreen=True)
        state["shaders"]["fragment"].pop("rawSha256")
        result = classify_passes(
            [{"eventId": 70, "name": "Draw", "flags": 2}],
            pipeline_states={70: state},
            shader_catalog=catalog,
        )
        item = result["items"][0]
        self.assertEqual(item["category"], "Lighting")
        fragment = next(
            shader for shader in item["shaders"] if shader["stage"] == "fragment"
        )
        self.assertEqual(fragment["rawSha256"], PS_A)
        self.assertEqual(fragment["name"], "DeferredLightingPS")

    def test_draw_state_projects_valid_target_raster_stencil_and_samplers(
        self,
    ) -> None:
        state = _pipeline(render_targets=[101, 102])
        state.update(
            {
                "cullMode": "Back",
                "fillMode": "Solid",
                "frontCCW": True,
                "depthFunction": "LessEqual",
                "stencilTestEnabled": True,
                "stencilReadOnly": False,
                "stencilWritePossible": True,
                "stencilFront": {
                    "function": "Equal",
                    "failOperation": "Keep",
                    "depthFailOperation": "IncrementClamp",
                    "passOperation": "Replace",
                    "reference": 7,
                    "compareMask": 0x3F,
                    "writeMask": 0x7F,
                    "ignored": "not projected",
                },
                "stencilBack": {
                    "function": "AlwaysTrue",
                    "failOperation": "Keep",
                    "depthFailOperation": "Keep",
                    "passOperation": "Keep",
                    "reference": 0,
                    "compareMask": 0xFF,
                    "writeMask": 0,
                },
                "colorTargetStates": [
                    {
                        "slot": 1,
                        "blendEnabled": True,
                        "logicOperationEnabled": False,
                        "writeMask": 5,
                        "writeChannels": "RB",
                        "ignored": "not projected",
                    },
                    {
                        "slot": 0,
                        "blendEnabled": False,
                        "logicOperationEnabled": True,
                        "writeMask": 15,
                        "writeChannels": "RGBA",
                    },
                    {
                        "slot": 2,
                        "blendEnabled": True,
                        "logicOperationEnabled": False,
                        "writeMask": -1,
                        "writeChannels": "",
                    },
                    {
                        "slot": 3,
                        "blendEnabled": True,
                        "logicOperationEnabled": False,
                        "writeMask": 3,
                        "writeChannels": "R",
                    },
                    {
                        "slot": 4,
                        "blendEnabled": "unknown",
                        "logicOperationEnabled": False,
                        "writeMask": 1,
                        "writeChannels": "R",
                    },
                ],
                "samplers": {
                    "fragment": {
                        "declaredCount": 3,
                        "boundCount": 3,
                        "bindingsResolved": True,
                        "bindingOmittedCount": 2,
                        "bindings": [
                            {
                                "bindingIndex": 2,
                                "directAccess": False,
                                "arrayElement": 1,
                                "descriptorStoreId": 300,
                                "descriptorByteOffset": 64,
                                "descriptorByteSize": 32,
                                "samplerResourceId": 400,
                                "creationTimeConstant": False,
                                "type": "Sampler",
                                "addressU": "Wrap",
                                "addressV": "ClampEdge",
                                "addressW": "Mirror",
                                "compareFunction": "LessEqual",
                                "filter": "Anisotropic",
                                "maxAnisotropy": 8.0,
                                "minLOD": 0.0,
                                "maxLOD": 12.0,
                                "mipBias": -0.5,
                                "staticallyUnused": False,
                                "accessEvidence": (
                                    "not-statically-unused-candidate"
                                ),
                                "runtimeAccessProven": False,
                                "ignored": "not projected",
                            },
                            {
                                "directAccess": True,
                                "arrayElement": 0,
                                "creationTimeConstant": True,
                                "staticallyUnused": False,
                                "accessEvidence": "direct-descriptor-candidate",
                                "runtimeAccessProven": False,
                            },
                            {
                                "bindingIndex": 9,
                                "directAccess": False,
                                "runtimeAccessProven": True,
                            },
                        ],
                    },
                    "invalid-stage": [],
                },
            }
        )

        draw_state = classify_passes(
            [{"eventId": 70, "name": "Draw", "flags": 2}],
            pipeline_states={70: state},
        )["items"][0]["drawState"]

        self.assertEqual(
            [
                {
                    "slot": 0,
                    "blendEnabled": False,
                    "logicOperationEnabled": True,
                    "writeMask": 15,
                    "writeChannels": "RGBA",
                },
                {
                    "slot": 1,
                    "blendEnabled": True,
                    "logicOperationEnabled": False,
                    "writeMask": 5,
                    "writeChannels": "RB",
                },
                {
                    "slot": 2,
                    "blendEnabled": True,
                    "logicOperationEnabled": False,
                    "writeMask": -1,
                    "writeChannels": "",
                },
            ],
            draw_state["colorTargetStates"],
        )
        self.assertEqual(
            {
                "cullMode": "Back",
                "fillMode": "Solid",
                "frontCCW": True,
                "depthFunction": "LessEqual",
                "stencilTestEnabled": True,
                "stencilReadOnly": False,
                "stencilWritePossible": True,
            },
            {
                field: draw_state[field]
                for field in (
                    "cullMode",
                    "fillMode",
                    "frontCCW",
                    "depthFunction",
                    "stencilTestEnabled",
                    "stencilReadOnly",
                    "stencilWritePossible",
                )
            },
        )
        self.assertEqual(
            {
                "function": "Equal",
                "failOperation": "Keep",
                "depthFailOperation": "IncrementClamp",
                "passOperation": "Replace",
                "reference": 7,
                "compareMask": 0x3F,
                "writeMask": 0x7F,
            },
            draw_state["stencilFront"],
        )
        sampler = draw_state["samplers"]["fragment"]
        self.assertEqual(
            {
                "declaredCount": 3,
                "boundCount": 3,
                "bindingsResolved": True,
                "bindingOmittedCount": 2,
            },
            {key: value for key, value in sampler.items() if key != "bindings"},
        )
        self.assertEqual(2, len(sampler["bindings"]))
        self.assertEqual(2, sampler["bindings"][0]["bindingIndex"])
        self.assertEqual("RB", draw_state["colorTargetStates"][1]["writeChannels"])
        self.assertTrue(sampler["bindings"][1]["directAccess"])
        self.assertNotIn("bindingIndex", sampler["bindings"][1])
        self.assertFalse(sampler["bindings"][0]["runtimeAccessProven"])
        self.assertNotIn("ignored", sampler["bindings"][0])
        self.assertNotIn("invalid-stage", draw_state["samplers"])

    def test_draw_state_bounds_color_target_states(self) -> None:
        state = _pipeline(render_targets=[])
        state["colorTargetStates"] = [
            {
                "slot": slot,
                "blendEnabled": False,
                "logicOperationEnabled": False,
                "writeMask": 15,
                "writeChannels": "RGBA",
            }
            for slot in range(300)
        ]

        draw_state = classify_passes(
            [{"eventId": 71, "name": "Draw", "flags": 2}],
            pipeline_states={71: state},
        )["items"][0]["drawState"]

        self.assertEqual(256, len(draw_state["colorTargetStates"]))
        self.assertEqual(0, draw_state["colorTargetStates"][0]["slot"])
        self.assertEqual(255, draw_state["colorTargetStates"][-1]["slot"])

    def test_ai_proposal_is_isolated_and_never_changes_fixed_tree(self) -> None:
        fixed = classify_passes(
            [{"eventId": 80, "name": "BasePass", "flags": 2}],
            pipeline_states={80: _pipeline(render_targets=[1], depth_target=2)},
        )
        fixed_tree = deepcopy(fixed["tree"])
        overlaid = apply_ai_semantic_proposals(
            fixed,
            [
                {
                    "eventId": 80,
                    "category": "UI",
                    "subcategory": "Inventory",
                    "displayLabel": "Inventory UI candidate",
                    "rationale": "The asset name resembles a menu material.",
                }
            ],
        )
        self.assertEqual(overlaid["tree"], fixed_tree)
        self.assertEqual(overlaid["items"][0]["category"], "BasePass")
        proposal = overlaid["items"][0]["semanticProposal"]
        self.assertEqual(proposal["authority"], "AIProvisional")
        self.assertFalse(proposal["verified"])
        self.assertFalse(proposal["applied"])
        self.assertFalse(proposal["physicalEvidence"])
        self.assertFalse(overlaid["policy"]["aiCanChangeFixedTree"])

    def test_ai_proposal_rejects_unknown_or_duplicate_event(self) -> None:
        fixed = classify_passes([{"eventId": 90, "name": "Dispatch", "flags": 4}])
        with self.assertRaises(PassClassificationError):
            apply_ai_semantic_proposals(
                fixed, [{"eventId": 999, "category": "Compute"}]
            )
        with self.assertRaises(PassClassificationError):
            apply_ai_semantic_proposals(
                fixed,
                [
                    {"eventId": 90, "category": "Compute"},
                    {"eventId": 90, "category": "Post"},
                ],
            )

    def test_timeline_resource_flow_and_boundary_receipts_are_explicit(self) -> None:
        result = classify_passes(
            {
                "actions": [
                    {"eventId": 10, "name": "Producer", "flags": 2},
                    {"eventId": 20, "name": "Consumer", "flags": 2},
                ],
                "summary": {
                    "truncated": True,
                    "omittedActionCountLowerBound": 3,
                    "truncationReasons": ["action-limit"],
                    "requestedMaxActions": 2,
                },
            },
            pipeline_states={
                10: {
                    "writeResources": [
                        {"resourceId": 7, "access": "shader-write"}
                    ]
                },
                20: {
                    "readResources": [
                        {"resourceId": 7, "access": "shader-read", "stage": "fragment"},
                        {"resourceId": 8, "access": "constant-buffer", "stage": "fragment"},
                    ]
                },
            },
        )
        summary = result["summary"]
        self.assertEqual([10, 20], summary["executionTimeline"])
        self.assertEqual([0, 1], [item["executionIndex"] for item in result["items"]])
        edge = summary["resourceFlowEdges"][0]
        self.assertEqual((10, 20, 7), (
            edge["producerEid"], edge["consumerEid"], edge["resourceId"]
        ))
        self.assertEqual("shader-read", edge["consumerAccess"])
        self.assertEqual("unknown", edge["subresourceOverlap"])
        self.assertEqual("FixedProgramCandidate", edge["authority"])
        self.assertEqual(8, summary["externalInputs"][0]["resourceId"])
        self.assertEqual(7, summary["terminalOutputs"][0]["resourceId"])
        self.assertTrue(summary["truncated"])
        self.assertEqual(3, summary["omittedActionCountLowerBound"])

    def test_depth_blend_and_zero_workload_do_not_overclaim_contribution(self) -> None:
        result = classify_passes(
            [
                {
                    "eventId": 101,
                    "name": "Zero draw",
                    "flags": 2,
                    "numIndices": 0,
                    "numVertices": 0,
                    "numInstances": 1,
                }
            ],
            pipeline_states={
                101: {
                    "renderTargets": [{"resourceId": 1}],
                    "colorWriteTargets": [{"resourceId": 1}],
                    "blendEnabled": True,
                    "blendReadTargets": [{"resourceId": 1}],
                    "depthTarget": {"resourceId": 2},
                    "depthTestEnabled": True,
                    "depthWriteEnabled": False,
                }
            },
        )
        item = result["items"][0]
        self.assertIn("blend-destination", {value["access"] for value in item["inputs"]})
        self.assertIn("depth-test", {value["access"] for value in item["inputs"]})
        self.assertNotIn(2, {value["resourceId"] for value in item["outputs"]})
        self.assertFalse(item["participation"]["nonZeroWorkload"])
        self.assertFalse(item["participation"]["outputAffectingCandidate"])
        self.assertEqual("not-established", item["actualWorkState"])
        self.assertIn("not-tested", item["participation"]["contributorObservation"])

    def test_unknown_depth_write_is_only_a_candidate_resource_writer(self) -> None:
        result = classify_passes(
            [
                {"eventId": 1, "name": "Depth maybe", "flags": 2},
                {"eventId": 2, "name": "Depth reader", "flags": 2},
            ],
            pipeline_states={
                1: {
                    "depthTarget": {"resourceId": 9},
                    "depthTestEnabled": True,
                },
                2: {
                    "readResources": [
                        {"resourceId": 9, "access": "depth-test"}
                    ]
                },
            },
        )
        first = result["items"][0]
        self.assertEqual([], first["outputs"])
        self.assertEqual(9, first["possibleOutputs"][0]["resourceId"])
        edge = result["summary"]["resourceFlowEdges"][0]
        self.assertEqual((1, 2), (edge["producerEid"], edge["consumerEid"]))
        self.assertEqual("possible", edge["producerWriteCertainty"])
        self.assertEqual("FixedProgramCandidate", edge["authority"])
        external = next(
            value
            for value in result["summary"]["externalInputs"]
            if value["consumerEid"] == 2
        )
        self.assertEqual(9, external["resourceId"])
        self.assertEqual("FixedProgramCandidate", external["authority"])
        terminal = result["summary"]["terminalOutputs"][0]
        self.assertEqual("possible", terminal["writeCertainty"])
        self.assertEqual("FixedProgramCandidate", terminal["authority"])

    def test_full_marker_identity_and_breadcrumb_keep_cameras_distinct(self) -> None:
        actions = [
            {
                "eventId": 1,
                "name": "BasePass Camera A",
                "flags": 2,
                "markerPath": ["Camera A", "Opaque"],
                "markerEventPath": [
                    {"eventId": 100, "name": "Camera A"},
                    {"eventId": 101, "name": "Opaque"},
                ],
            },
            {
                "eventId": 2,
                "name": "BasePass Camera B",
                "flags": 2,
                "markerPath": ["Camera B", "Opaque"],
                "markerEventPath": [
                    {"eventId": 200, "name": "Camera B"},
                    {"eventId": 201, "name": "Opaque"},
                ],
            },
        ]
        result = classify_passes(actions)
        groups = result["tree"][0]["children"][0]["children"]
        self.assertEqual(
            ["Camera A / Opaque", "Camera B / Opaque"],
            [group["label"] for group in groups],
        )

    def test_standalone_indirect_draws_do_not_form_a_synthetic_container(self) -> None:
        result = classify_passes(
            [
                {"eventId": 71, "name": "DrawIndirect", "flags": 0x080002},
                {"eventId": 72, "name": "DrawIndirect", "flags": 0x080002},
            ]
        )
        groups = result["tree"][0]["children"][0]["children"]
        self.assertEqual(2, len(groups))
        self.assertTrue(all(group["count"] == 1 for group in groups))
        self.assertTrue(all(
            item["participation"]["indirectArgumentsResolved"] is False
            for item in result["items"]
        ))

    def test_utility_without_declared_destination_is_not_a_candidate(self) -> None:
        item = classify_passes(
            [{"eventId": 90, "name": "Copy", "flags": 0x0400}]
        )["items"][0]
        self.assertTrue(item["participation"]["issuedAction"])
        self.assertFalse(item["participation"]["outputAffectingCandidate"])
        self.assertEqual("not-established", item["actualWorkState"])

    def test_present_consumes_the_frame_output_without_becoming_a_writer(self) -> None:
        result = classify_passes(
            [
                {"eventId": 1, "name": "Final composite", "flags": 2},
                {
                    "eventId": 2,
                    "name": "Present",
                    "flags": 0x0100,
                    "outputs": [{"resourceId": 55, "name": "Backbuffer"}],
                },
            ],
            pipeline_states={
                1: {
                    "writeResources": [
                        {"resourceId": 55, "access": "color-target"}
                    ]
                }
            },
        )
        present = result["items"][1]
        self.assertEqual([], present["outputs"])
        self.assertEqual("present-source", present["inputs"][0]["access"])
        self.assertEqual(1, result["summary"]["terminalOutputs"][0]["producerEid"])
        self.assertEqual(
            (1, 2),
            (
                result["summary"]["resourceFlowEdges"][0]["producerEid"],
                result["summary"]["resourceFlowEdges"][0]["consumerEid"],
            ),
        )

    def test_resolve_declared_output_is_a_definite_utility_destination(self) -> None:
        result = classify_passes(
            [
                {
                    "eventId": 1,
                    "name": "Resolve",
                    "flags": 0x0800,
                    "outputs": [{"resourceId": 55, "name": "Resolved color"}],
                }
            ]
        )

        item = result["items"][0]
        self.assertEqual([55], [value["resourceId"] for value in item["outputs"]])
        self.assertEqual([], item["possibleOutputs"])
        self.assertEqual("output-affecting-candidate", item["actualWorkState"])

    def test_partial_subresource_identity_stays_a_candidate_edge(self) -> None:
        result = classify_passes(
            [
                {"eventId": 1, "name": "Producer", "flags": 2},
                {"eventId": 2, "name": "Consumer", "flags": 2},
            ],
            pipeline_states={
                1: {
                    "writeResources": [
                        {"resourceId": 7, "subresource": {"mip": 1}}
                    ]
                },
                2: {
                    "readResources": [
                        {
                            "resourceId": 7,
                            "subresource": {"mip": 1, "slice": 2},
                        }
                    ]
                },
            },
        )
        edge = result["summary"]["resourceFlowEdges"][0]
        self.assertEqual("unknown", edge["subresourceOverlap"])
        self.assertEqual("FixedProgramCandidate", edge["authority"])

    def test_equal_partial_subresource_identity_stays_a_candidate_edge(self) -> None:
        result = classify_passes(
            [
                {"eventId": 1, "name": "Producer", "flags": 2},
                {"eventId": 2, "name": "Consumer", "flags": 2},
            ],
            pipeline_states={
                1: {
                    "writeResources": [
                        {"resourceId": 7, "subresource": {"mip": 1}}
                    ]
                },
                2: {
                    "readResources": [
                        {"resourceId": 7, "subresource": {"mip": 1}}
                    ]
                },
            },
        )

        edge = result["summary"]["resourceFlowEdges"][0]
        self.assertEqual("unknown", edge["subresourceOverlap"])
        self.assertEqual("FixedProgramCandidate", edge["authority"])

    def test_possible_write_keeps_both_plausible_producers_and_terminals(
        self,
    ) -> None:
        subresource = {"mip": 0, "slice": 0, "sample": 0, "plane": 0}
        result = classify_passes(
            [
                {"eventId": 1, "name": "Definite producer", "flags": 2},
                {"eventId": 2, "name": "Possible producer", "flags": 2},
                {"eventId": 3, "name": "Consumer", "flags": 2},
            ],
            pipeline_states={
                1: {
                    "writeResources": [
                        {"resourceId": 7, "subresource": subresource}
                    ]
                },
                2: {
                    "possibleOutputs": [
                        {
                            "resourceId": 7,
                            "access": "color-target-possible",
                            "subresource": subresource,
                        }
                    ]
                },
                3: {
                    "readResources": [
                        {"resourceId": 7, "subresource": subresource}
                    ]
                },
            },
        )

        edges = result["summary"]["resourceFlowEdges"]
        self.assertEqual([1, 2], [edge["producerEid"] for edge in edges])
        self.assertTrue(
            all(edge["authority"] == "FixedProgramCandidate" for edge in edges)
        )
        self.assertTrue(edges[0]["interveningWriteUncertain"])
        self.assertEqual("definite", edges[0]["producerWriteCertainty"])
        self.assertEqual("possible", edges[1]["producerWriteCertainty"])
        self.assertEqual([], result["summary"]["externalInputs"])

        terminals = result["summary"]["terminalOutputs"]
        self.assertEqual([1, 2], [value["producerEid"] for value in terminals])
        self.assertTrue(
            all(
                value["authority"] == "FixedProgramCandidate"
                for value in terminals
            )
        )
        self.assertTrue(terminals[0]["possibleOverwrite"])
        self.assertEqual("definite", terminals[0]["writeCertainty"])
        self.assertEqual("possible", terminals[1]["writeCertainty"])

    def test_resource_flow_keeps_distinct_ranges_and_external_ranges(self) -> None:
        def subresource(mip: int) -> dict[str, int]:
            return {"mip": mip, "slice": 0, "sample": 0, "plane": 0}

        result = classify_passes(
            [
                {"eventId": 1, "name": "Producer", "flags": 2},
                {"eventId": 2, "name": "Consumer", "flags": 2},
            ],
            pipeline_states={
                1: {
                    "writeResources": [
                        {
                            "resourceId": 7,
                            "access": "shader-write",
                            "stage": "compute",
                            "subresource": subresource(0),
                        },
                        {
                            "resourceId": 7,
                            "access": "shader-write",
                            "stage": "compute",
                            "subresource": subresource(1),
                        },
                    ]
                },
                2: {
                    "readResources": [
                        {
                            "resourceId": 7,
                            "access": "shader-read",
                            "stage": "fragment",
                            "subresource": subresource(0),
                        },
                        {
                            "resourceId": 7,
                            "access": "shader-read",
                            "stage": "fragment",
                            "subresource": subresource(1),
                        },
                        {
                            "resourceId": 8,
                            "access": "shader-read",
                            "stage": "fragment",
                            "subresource": subresource(0),
                        },
                        {
                            "resourceId": 8,
                            "access": "shader-read",
                            "stage": "fragment",
                            "subresource": subresource(1),
                        },
                    ]
                },
            },
        )

        edges = result["summary"]["resourceFlowEdges"]
        self.assertEqual(2, len(edges))
        self.assertEqual(
            {0, 1},
            {edge["consumerRange"]["subresource"]["mip"] for edge in edges},
        )
        external = result["summary"]["externalInputs"]
        self.assertEqual(2, len(external))
        self.assertEqual(
            {0, 1},
            {
                value["consumerRange"]["subresource"]["mip"]
                for value in external
            },
        )

    def test_copy_and_resolve_destination_aliases_are_one_writer(self) -> None:
        for flag in (0x0400, 0x0800):
            with self.subTest(flag=flag):
                result = classify_passes(
                    [
                        {
                            "eventId": 1,
                            "name": "Transfer",
                            "flags": flag,
                            "copyDestination": {
                                "resourceId": 55,
                                "name": "Destination",
                            },
                            "outputs": [
                                {"resourceId": 55, "name": "Destination"}
                            ],
                        }
                    ]
                )

                outputs = result["items"][0]["outputs"]
                self.assertEqual(1, len(outputs))
                self.assertEqual("copy-destination", outputs[0]["access"])
                self.assertEqual(
                    ["copy-destination", "action-output"], outputs[0]["roles"]
                )
                terminal = result["summary"]["terminalOutputs"]
                self.assertEqual(1, len(terminal))
                self.assertEqual("FixedProgram", terminal[0]["authority"])

    def test_utility_destination_does_not_write_previous_draw_bindings(self) -> None:
        for flag in (0x0001, 0x100001, 0x0400, 0x0800):
            with self.subTest(flag=flag):
                result = classify_passes([{
                    "eventId": 1, "name": "Utility", "flags": flag,
                    "copyDestination": {"resourceId": 55},
                    "outputs": [{"resourceId": 66}],
                    "depthOutput": {"resourceId": 77},
                }])
                outputs = result["items"][0]["outputs"]
                self.assertEqual([55], [value["resourceId"] for value in outputs])
                self.assertEqual("copy-destination", outputs[0]["access"])

    def test_action_timeline_excludes_unknown_events_but_full_timeline_keeps_them(
        self,
    ) -> None:
        result = classify_passes(
            [
                {"eventId": 4, "name": "State-only API event", "flags": 0},
                {"eventId": 5, "name": "Draw", "flags": 2},
                {"eventId": 6, "name": "Dispatch", "flags": 4},
                {"eventId": 7, "name": "Clear", "flags": 1},
            ]
        )

        self.assertEqual([4, 5, 6, 7], result["summary"]["executionTimeline"])
        self.assertEqual(
            [5, 6, 7], result["summary"]["actionExecutionTimeline"]
        )
        self.assertEqual(
            "unknown",
            next(item for item in result["items"] if item["eventId"] == 4)[
                "actionKind"
            ],
        )

    def test_resource_flow_uses_latest_compatible_subresource_writer(self) -> None:
        result = classify_passes(
            [
                {"eventId": 1, "name": "Write mip 0", "flags": 2},
                {"eventId": 2, "name": "Write mip 1", "flags": 2},
                {"eventId": 3, "name": "Read mip 0", "flags": 2},
            ],
            pipeline_states={
                1: {
                    "writeResources": [
                        {
                            "resourceId": 7,
                            "subresource": {
                                "mip": 0,
                                "slice": 0,
                                "sample": 0,
                                "plane": 0,
                            },
                        }
                    ]
                },
                2: {
                    "writeResources": [
                        {
                            "resourceId": 7,
                            "subresource": {
                                "mip": 1,
                                "slice": 0,
                                "sample": 0,
                                "plane": 0,
                            },
                        }
                    ]
                },
                3: {
                    "readResources": [
                        {
                            "resourceId": 7,
                            "subresource": {
                                "mip": 0,
                                "slice": 0,
                                "sample": 0,
                                "plane": 0,
                            },
                        }
                    ]
                },
            },
        )

        edge = result["summary"]["resourceFlowEdges"][0]
        self.assertEqual((1, 3), (edge["producerEid"], edge["consumerEid"]))
        self.assertEqual("overlap", edge["subresourceOverlap"])
        self.assertEqual("FixedProgram", edge["authority"])

    def test_resource_flow_same_subresource_write_hides_older_writer(self) -> None:
        subresource = {"mip": 0, "slice": 0, "sample": 0, "plane": 0}
        result = classify_passes(
            [
                {"eventId": 1, "name": "Old write", "flags": 2},
                {"eventId": 2, "name": "Latest write", "flags": 2},
                {"eventId": 3, "name": "Read", "flags": 2},
            ],
            pipeline_states={
                1: {"writeResources": [{"resourceId": 7, "subresource": subresource}]},
                2: {"writeResources": [{"resourceId": 7, "subresource": subresource}]},
                3: {"readResources": [{"resourceId": 7, "subresource": subresource}]},
            },
        )

        edges = result["summary"]["resourceFlowEdges"]
        self.assertEqual(
            [(2, 3)],
            [(edge["producerEid"], edge["consumerEid"]) for edge in edges],
        )

    def test_terminal_outputs_keep_independent_final_subresource_writers(
        self,
    ) -> None:
        def subresource(mip: int) -> dict[str, int]:
            return {"mip": mip, "slice": 0, "sample": 0, "plane": 0}

        result = classify_passes(
            [
                {"eventId": 1, "name": "Write mip 0 old", "flags": 2},
                {"eventId": 2, "name": "Write mip 1", "flags": 2},
                {"eventId": 3, "name": "Write mip 0 final", "flags": 2},
            ],
            pipeline_states={
                1: {
                    "writeResources": [
                        {"resourceId": 7, "subresource": subresource(0)}
                    ]
                },
                2: {
                    "writeResources": [
                        {"resourceId": 7, "subresource": subresource(1)}
                    ]
                },
                3: {
                    "writeResources": [
                        {"resourceId": 7, "subresource": subresource(0)}
                    ]
                },
            },
        )

        terminal = result["summary"]["terminalOutputs"]
        self.assertEqual(
            {(2, 1), (3, 0)},
            {
                (value["producerEid"], value["range"]["subresource"]["mip"])
                for value in terminal
            },
        )
        self.assertTrue(
            all(value["authority"] == "FixedProgram" for value in terminal)
        )

    def test_terminal_outputs_subtract_later_overlapping_byte_range(self) -> None:
        result = classify_passes(
            [
                {"eventId": 1, "name": "Write whole range", "flags": 2},
                {"eventId": 2, "name": "Overwrite middle", "flags": 2},
            ],
            pipeline_states={
                1: {
                    "writeResources": [
                        {"resourceId": 8, "byteOffset": 0, "byteSize": 100}
                    ]
                },
                2: {
                    "writeResources": [
                        {"resourceId": 8, "byteOffset": 40, "byteSize": 20}
                    ]
                },
            },
        )

        terminal = result["summary"]["terminalOutputs"]
        self.assertEqual(
            {(1, 0, 40), (1, 60, 40), (2, 40, 20)},
            {
                (
                    value["producerEid"],
                    value["range"]["byteOffset"],
                    value["range"]["byteSize"],
                )
                for value in terminal
            },
        )
        self.assertTrue(
            all(value["authority"] == "FixedProgram" for value in terminal)
        )

    def test_terminal_outputs_keep_unknown_range_relationship_conservative(
        self,
    ) -> None:
        result = classify_passes(
            [
                {"eventId": 1, "name": "Write unknown range", "flags": 2},
                {"eventId": 2, "name": "Write known mip", "flags": 2},
            ],
            pipeline_states={
                1: {"writeResources": [{"resourceId": 9}]},
                2: {
                    "writeResources": [
                        {
                            "resourceId": 9,
                            "subresource": {
                                "mip": 0,
                                "slice": 0,
                                "sample": 0,
                                "plane": 0,
                            },
                        }
                    ]
                },
            },
        )

        terminal = result["summary"]["terminalOutputs"]
        self.assertEqual([1, 2], [value["producerEid"] for value in terminal])
        self.assertTrue(
            all(
                value["authority"] == "FixedProgramCandidate"
                and value["rangeRelationship"] == "unknown"
                for value in terminal
            )
        )

    def test_definite_depth_writer_does_not_swallow_possible_stencil_writer(
        self,
    ) -> None:
        subresource = {"mip": 0, "slice": 0, "sample": 0, "plane": 0}
        result = classify_passes(
            [
                {"eventId": 1, "name": "Depth/stencil producer", "flags": 2},
                {"eventId": 2, "name": "Stencil consumer", "flags": 2},
            ],
            pipeline_states={
                1: {
                    "writeResources": [
                        {
                            "resourceId": 20,
                            "access": "depth-target",
                            "stage": "output-merger",
                            "subresource": subresource,
                        }
                    ],
                    "possibleOutputs": [
                        {
                            "resourceId": 20,
                            "access": "stencil-target-possible",
                            "stage": "output-merger",
                            "subresource": subresource,
                        }
                    ],
                },
                2: {
                    "readResources": [
                        {
                            "resourceId": 20,
                            "access": "stencil-test",
                            "stage": "output-merger",
                            "subresource": subresource,
                        }
                    ]
                },
            },
        )

        edges = result["summary"]["resourceFlowEdges"]
        self.assertEqual(
            {"depth-target", "stencil-target-possible"},
            {edge["producerAccess"] for edge in edges},
        )
        stencil_edge = next(
            edge for edge in edges
            if edge["producerAccess"] == "stencil-target-possible"
        )
        self.assertEqual("possible", stencil_edge["producerWriteCertainty"])
        self.assertEqual("FixedProgramCandidate", stencil_edge["authority"])
        terminals = result["summary"]["terminalOutputs"]
        self.assertEqual(
            {"depth-target", "stencil-target-possible"},
            {value["access"] for value in terminals},
        )

    def test_source_resource_metadata_truncation_receipt_is_bounded_and_ored(
        self,
    ) -> None:
        result = classify_passes(
            {
                "actions": [{"eventId": 1, "name": "Draw", "flags": 2}],
                "summary": {
                    "resourceFactsTruncated": False,
                    "resourceMetadata": {
                        "sourceRecordCount": 5000,
                        "retainedEntryCount": 4096,
                        "omittedSourceRecordCount": 904,
                        "recordLimit": 4096,
                        "byteBudget": 2 * 1024 * 1024,
                        "budgetedBytes": 2 * 1024 * 1024 - 64,
                        "truncated": True,
                        "untrustedPayload": "must not cross the boundary",
                    },
                },
            }
        )

        summary = result["summary"]
        self.assertTrue(summary["resourceFactsTruncated"])
        self.assertEqual(
            {
                "sourceRecordCount": 5000,
                "retainedEntryCount": 4096,
                "omittedSourceRecordCount": 904,
                "recordLimit": 4096,
                "byteBudget": 2 * 1024 * 1024,
                "budgetedBytes": 2 * 1024 * 1024 - 64,
                "truncated": True,
            },
            summary["resourceMetadata"],
        )

        source_flag_only = classify_passes(
            {
                "actions": [{"eventId": 2, "name": "Draw", "flags": 2}],
                "summary": {
                    "resourceFactsTruncated": True,
                    "resourceMetadata": {"truncated": False},
                },
            }
        )
        self.assertTrue(source_flag_only["summary"]["resourceFactsTruncated"])

    def test_bridge_p2_draw_facts_survive_bounded_nullable_projection(self) -> None:
        vertex_layout = [
            {
                "index": index,
                "name": "POSITION" if index == 0 else None,
                "vertexBuffer": 0,
                "byteOffset": index * 4,
                "format": "R32_FLOAT" if index == 0 else None,
                "perInstance": False,
                "instanceRate": 0,
                "genericEnabled": False,
                "used": True,
            }
            for index in range(300)
        ]
        result = classify_passes(
            [
                {
                    "eventId": 1,
                    "name": "Draw",
                    "flags": 2,
                    "workload": {
                        "indexCount": 3,
                        "vertexCount": 3,
                        "instanceCount": 1,
                        "drawIndex": 7,
                        "dispatchBase": [1, 2, 3],
                    },
                }
            ],
            pipeline_states={
                1: {
                    "readResources": [
                        {
                            "resourceId": 50,
                            "access": "vertex-buffer",
                            "byteStride": 32,
                        },
                        {
                            "resourceId": 60,
                            "access": "index-buffer",
                            "byteStride": None,
                        },
                    ],
                    "colorWriteTargets": [{"resourceId": 10}],
                    "depthTarget": {"resourceId": 20},
                    "depthTestEnabled": True,
                    "depthWriteEnabled": True,
                    "sampleCount": None,
                    "sampleCountResolved": False,
                    "indexStride": None,
                    "vertexLayout": vertex_layout,
                    "vertexLayoutOmittedCount": 3,
                    "rasterizerDiscardEnabled": None,
                    "rasterOutputApplicable": None,
                    "rasterOutputSuppressionReason": (
                        "rasterizer-discard-unknown"
                    ),
                    "pipelineStateApplicable": True,
                    "colorTargetStates": [
                        {
                            "slot": 0,
                            "blendEnabled": None,
                            "logicOperationEnabled": False,
                            "colorBlend": {
                                "sourceFactor": "SrcAlpha",
                                "destinationFactor": "InvSrcAlpha",
                                "operation": "Add",
                            },
                            "alphaBlend": {
                                "sourceFactor": None,
                                "destinationFactor": "Zero",
                                "operation": "Max",
                            },
                            "logicOperation": None,
                            "writeMask": None,
                            "writeChannels": None,
                        }
                    ],
                }
            },
        )

        item = result["items"][0]
        self.assertEqual(32, item["inputs"][0]["byteStride"])
        self.assertIsNone(item["inputs"][1]["byteStride"])
        self.assertEqual(7, item["workload"]["drawIndex"])
        self.assertEqual([1, 2, 3], item["workload"]["dispatchBase"])
        draw_state = item["drawState"]
        self.assertIsNone(draw_state["sampleCount"])
        self.assertFalse(draw_state["sampleCountResolved"])
        self.assertIsNone(draw_state["indexStride"])
        self.assertIsNone(draw_state["rasterizerDiscardEnabled"])
        self.assertIsNone(draw_state["rasterOutputApplicable"])
        self.assertEqual(
            "rasterizer-discard-unknown",
            draw_state["rasterOutputSuppressionReason"],
        )
        self.assertTrue(draw_state["pipelineStateApplicable"])
        self.assertEqual(256, len(draw_state["vertexLayout"]))
        self.assertEqual(47, draw_state["vertexLayoutOmittedCount"])
        blend = draw_state["colorTargetStates"][0]
        self.assertIsNone(blend["blendEnabled"])
        self.assertEqual("Add", blend["colorBlend"]["operation"])
        self.assertIsNone(blend["alphaBlend"]["sourceFactor"])
        self.assertIsNone(blend["logicOperation"])
        self.assertIsNone(blend["writeMask"])
        self.assertIsNone(blend["writeChannels"])
        self.assertEqual([], item["outputs"])
        self.assertEqual(
            {"color-target-possible", "depth-target-possible"},
            {value["access"] for value in item["possibleOutputs"]},
        )
        nullable_workload = classify_passes(
            [
                {
                    "eventId": 2,
                    "name": "Draw",
                    "flags": 2,
                    "workload": {
                        "drawIndex": None,
                        "dispatchBase": None,
                    },
                }
            ]
        )["items"][0]["workload"]
        self.assertIsNone(nullable_workload["drawIndex"])
        self.assertIsNone(nullable_workload["dispatchBase"])


class PassKnowledgeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "project"
        self.project.mkdir()
        (self.project / "knowledge").mkdir()
        self.manifest = {
            "schema": "renderdoc-ultra-replay-project/v1",
            "schemaVersion": 1,
            "projectId": "1" * 32,
            "scope": {
                "gameId": "example-game",
                "buildId": "build-1",
                "platform": "windows-x64",
                "apiFamily": "d3d12",
            },
            "captures": [{"captureId": CAPTURE_A}, {"captureId": CAPTURE_B}],
            "knowledge": {"workspaces": [], "aliases": [], "materials": []},
        }
        (self.project / "project.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )
        self.store = PassKnowledgeStore(self.project)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _classification(self, capture_id: str, name: str) -> dict:
        return classify_passes(
            [{"eventId": 100, "name": name, "flags": 2}],
            pipeline_states={
                100: _pipeline(
                    render_targets=[{"resourceId": 1, "format": "R8G8B8A8"}],
                    depth_target={"resourceId": 2, "format": "D32"},
                    blend=False,
                    depth_test=True,
                    depth_write=True,
                )
            },
            capture_id=capture_id,
        )

    def test_save_load_and_exact_fingerprint_reuse(self) -> None:
        first = self._classification(CAPTURE_A, "BasePass Opaque")
        saved = self.store.save(first)
        self.assertEqual(saved["schema"], KNOWLEDGE_SCHEMA)
        self.assertEqual(saved["recordCount"], 1)
        self.assertEqual(saved["reusableRecordCount"], 1)
        knowledge = self.store.load()
        self.assertEqual(knowledge["records"][0]["category"], "BasePass")
        self.assertEqual(knowledge["records"][0]["authority"], "FixedProgram")
        self.assertFalse(knowledge["records"][0]["semanticVerified"])

        future = classify_passes(
            [{"eventId": 200, "name": "DrawIndexed", "flags": 2}],
            pipeline_states={
                200: _pipeline(
                    render_targets=[{"resourceId": 999, "format": "R8G8B8A8"}],
                    depth_target={"resourceId": 888, "format": "D32"},
                    blend=False,
                    depth_test=True,
                    depth_write=True,
                )
            },
            project_knowledge=knowledge,
            capture_id=CAPTURE_B,
        )
        item = future["items"][0]
        self.assertEqual(item["category"], "BasePass")
        self.assertTrue(
            any(entry["source"] == "projectKnowledge" for entry in item["evidence"])
        )

        # Saving the same capture replaces its observation instead of counting
        # it twice.
        self.store.save(first)
        loaded_again = self.store.load()
        self.assertEqual(len(loaded_again["captureObservations"]), 1)
        self.assertEqual(loaded_again["records"][0]["captureCount"], 1)

    def test_conflicting_fixed_observations_disable_cross_frame_reuse(self) -> None:
        first = self._classification(CAPTURE_A, "BasePass")
        second = self._classification(CAPTURE_B, "UI HUD")
        # Force the second capture to describe the same exact portable pass
        # fingerprint; only semantic evidence differs.
        self.assertEqual(
            first["items"][0]["passFingerprint"],
            second["items"][0]["passFingerprint"],
        )
        self.store.save(first)
        saved = self.store.save(second)
        self.assertEqual(saved["reusableRecordCount"], 0)
        record = self.store.load()["records"][0]
        self.assertTrue(record["conflict"])
        self.assertFalse(record["reusable"])
        self.assertEqual(record["category"], "Unknown")

    def test_ai_overlays_are_persisted_separately_and_never_become_records(
        self,
    ) -> None:
        fixed = self._classification(CAPTURE_A, "BasePass")
        overlaid = apply_ai_semantic_proposals(
            fixed,
            [{"eventId": 100, "category": "UI", "rationale": "candidate only"}],
        )
        saved = self.store.save(overlaid)
        self.assertEqual(saved["provisionalOverlayCount"], 1)
        knowledge = self.store.load()
        self.assertEqual(knowledge["records"][0]["category"], "BasePass")
        proposal = knowledge["provisionalOverlays"][0]
        self.assertEqual(proposal["authority"], "AIProvisional")
        self.assertFalse(proposal["verified"])
        self.assertFalse(proposal["applied"])
        self.assertFalse(knowledge["policy"]["aiProposalsReusableAuthority"])

    def test_store_rejects_truncated_classification(self) -> None:
        truncated = self._classification(CAPTURE_A, "BasePass")
        truncated["summary"]["sourceTruncated"] = True
        truncated["summary"]["truncated"] = True

        with self.assertRaisesRegex(
            PassClassificationError, "truncated pass classification"
        ):
            self.store.save(truncated)

    def test_store_rejects_unimported_capture_and_project_scope_change(self) -> None:
        unknown = self._classification("c" * 64, "BasePass")
        with self.assertRaises(PassClassificationError):
            self.store.save(unknown)

        self.store.save(self._classification(CAPTURE_A, "BasePass"))
        changed = dict(self.manifest)
        changed["scope"] = dict(changed["scope"])
        changed["scope"]["buildId"] = "other-build"
        (self.project / "project.json").write_text(
            json.dumps(changed), encoding="utf-8"
        )
        with self.assertRaisesRegex(PassClassificationError, "exact project scope"):
            self.store.load()

    def test_load_rejects_ai_record_as_reusable_authority(self) -> None:
        self.store.save(self._classification(CAPTURE_A, "BasePass"))
        value = json.loads(self.store.path.read_text(encoding="utf-8"))
        value["records"][0]["authority"] = "AIProvisional"
        self.store.path.write_text(json.dumps(value), encoding="utf-8")
        loaded = self.store.load()
        result = classify_passes(
            [{"eventId": 200, "name": "Draw", "flags": 2}],
            pipeline_states={200: _pipeline(render_targets=[1], depth_target=2)},
            project_knowledge=loaded,
        )
        self.assertFalse(
            any(
                evidence["source"] == "projectKnowledge"
                for evidence in result["items"][0]["evidence"]
            )
        )


if __name__ == "__main__":
    unittest.main()
