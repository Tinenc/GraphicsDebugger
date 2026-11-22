from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.event_flow_artifacts import (
    EVENT_FLOW_ARTIFACT_SCHEMA,
    EVENT_FLOW_MANIFEST_SCHEMA,
    build_event_flow_graph,
    default_event_artifact_root,
    publish_event_flow_artifact,
)
from src.event_flow_diagram import EventFlowDiagramError, EventFlowNodeKind


SESSION = "a" * 32
CAPTURE = "b" * 64


def _flow() -> dict:
    return {
        "schema": "replaylab-current-frame-flow-mcp/v1",
        "flow": {
            "items": [
                {
                    "eventId": 10,
                    "executionIndex": 0,
                    "name": "Opaque draw",
                    "category": "GBuffer",
                    "subcategory": "Opaque MRT pass",
                    "confidence": "candidate",
                    "actionKind": "graphics",
                    "shaders": [
                        {"stage": "vertex", "shaderId": 1},
                        {"stage": "fragment", "shaderId": 2},
                    ],
                    "targets": {
                        "renderTargets": [
                            {"slot": 0, "resourceId": 70, "format": "RGBA8"},
                            {"slot": 1, "resourceId": 71, "format": "RGBA16F"},
                        ],
                        "depthTargets": [{"resourceId": 80, "format": "D32"}],
                    },
                },
                {
                    "eventId": 20,
                    "executionIndex": 1,
                    "name": "Deferred lighting",
                    "category": "Lighting",
                    "subcategory": "Deferred light pass",
                    "confidence": "probable",
                    "actionKind": "graphics",
                    "shaders": [
                        {"stage": "vertex", "shaderId": 3},
                        {"stage": "fragment", "shaderId": 4},
                    ],
                    "targets": {
                        "renderTargets": [
                            {"slot": 0, "resourceId": 90, "format": "RGBA16F"}
                        ],
                        "depthTargets": [],
                    },
                },
            ],
            "summary": {
                "sourceTruncated": False,
                "truncated": False,
                "resourceFlowTruncated": False,
                "resourceFlowEdges": [
                    {
                        "producerEid": 10,
                        "consumerEid": 20,
                        "resourceId": 70,
                        "consumerStage": "fragment",
                        "producerWriteCertainty": "definite",
                        "subresourceOverlap": "overlap",
                        "authority": "FixedProgram",
                    },
                    {
                        "producerEid": 10,
                        "consumerEid": 20,
                        "resourceId": 71,
                        "consumerStage": "fragment",
                        "producerWriteCertainty": "definite",
                        "subresourceOverlap": "overlap",
                        "authority": "FixedProgram",
                    },
                    {
                        "producerEid": 10,
                        "consumerEid": 20,
                        "resourceId": 72,
                        "consumerStage": "fragment",
                        "producerWriteCertainty": "possible",
                        "subresourceOverlap": "unknown",
                        "authority": "FixedProgramCandidate",
                    },
                ],
            },
        },
    }


def test_resource_flow_keeps_multiple_definite_resources_for_one_eid_pair() -> None:
    graph = build_event_flow_graph(
        _flow(),
        diagram_kind="resource-flow",
        capture_session_id=SESSION,
        capture_id=CAPTURE,
    )

    assert [node.event_id for node in graph.nodes] == [10, 20]
    assert len(graph.edges) == 2
    assert [edge.resource_key for edge in graph.edges] == [
        "resource.70",
        "resource.71",
    ]
    assert all(edge.stage_access_token == "fragment" for edge in graph.edges)
    assert graph.truncation.source_truncated is True
    assert graph.truncation.omitted_edge_count_lower_bound >= 1
    assert "uncertain-resource-flow-omitted" in graph.truncation.reasons


def test_pipeline_stage_keeps_compact_api_abbreviations_and_exact_eid() -> None:
    graph = build_event_flow_graph(
        _flow(),
        diagram_kind="pipeline-stage",
        capture_session_id=SESSION,
        capture_id=CAPTURE,
        event_id=10,
        capture_api="D3D12",
    )

    assert graph.nodes[0].event_id == 10
    assert [node.stage for node in graph.nodes[1:]] == ["IA", "VS", "RS", "PS", "OM"]
    assert [(edge.source_id, edge.target_id) for edge in graph.edges] == [
        ("Sia", "Svs"),
        ("Svs", "Srs"),
        ("Srs", "Sps"),
        ("Sps", "Som"),
    ]


def test_pipeline_stage_requires_an_admitted_event() -> None:
    with pytest.raises(EventFlowDiagramError, match="outside the admitted flow"):
        build_event_flow_graph(
            _flow(),
            diagram_kind="pipeline-stage",
            capture_session_id=SESSION,
            event_id=999,
            capture_api="D3D12",
        )


def test_mesh_pipeline_does_not_invent_input_assembler_participation() -> None:
    flow = _flow()
    flow["flow"]["items"][0]["shaders"] = [
        {"stage": "task", "shaderId": 1},
        {"stage": "mesh", "shaderId": 2},
        {"stage": "fragment", "shaderId": 3},
    ]

    graph = build_event_flow_graph(
        flow,
        diagram_kind="pipeline-stage",
        capture_session_id=SESSION,
        event_id=10,
        capture_api="D3D12",
    )

    assert [node.stage for node in graph.nodes[1:]] == ["AS", "MS", "RS", "PS", "OM"]
    assert all(node.stage != "IA" for node in graph.nodes)


@pytest.mark.parametrize(
    "capture_api,expected",
    (
        ("D3D11", ["IA", "VS", "HS", "DS", "GS", "RS", "PS", "OM"]),
        ("Vulkan", ["VTX", "VS", "TCS", "TES", "GS", "RS", "FS", "FB"]),
        ("OpenGL", ["VTX", "VS", "TCS", "TES", "GS", "RS", "FS", "FB"]),
    ),
)
def test_pipeline_stage_uses_the_frozen_api_family_abbreviations(
    capture_api: str, expected: list[str]
) -> None:
    flow = _flow()
    flow["flow"]["items"][0]["shaders"] = [
        {"stage": "vertex", "shaderId": 1},
        {"stage": "hull", "shaderId": 2},
        {"stage": "domain", "shaderId": 3},
        {"stage": "geometry", "shaderId": 4},
        {"stage": "fragment", "shaderId": 5},
    ]

    graph = build_event_flow_graph(
        flow,
        diagram_kind="pipeline-stage",
        capture_session_id=SESSION,
        event_id=10,
        capture_api=capture_api,
    )

    assert [node.stage for node in graph.nodes[1:]] == expected


def test_pipeline_stage_rejects_a_missing_or_unknown_frozen_api() -> None:
    for capture_api in (None, "Metal"):
        with pytest.raises(EventFlowDiagramError, match="capture API"):
            build_event_flow_graph(
                _flow(),
                diagram_kind="pipeline-stage",
                capture_session_id=SESSION,
                event_id=10,
                capture_api=capture_api,
            )


def test_pass_overview_expands_physical_gbuffer_attachments_without_semantics() -> None:
    graph = build_event_flow_graph(
        _flow(),
        diagram_kind="pass-overview",
        capture_session_id=SESSION,
        capture_id=CAPTURE,
    )

    attachments = [
        node for node in graph.nodes if node.kind is EventFlowNodeKind.GBUFFER_ATTACHMENT
    ]
    assert [(node.attachment, node.resource_key) for node in attachments] == [
        ("RT0", "resource.70"),
        ("RT1", "resource.71"),
    ]
    assert all("Albedo" not in node.label and "Normal" not in node.label for node in attachments)
    depth = [
        node for node in graph.nodes if node.kind is EventFlowNodeKind.DEPTH_ATTACHMENT
    ]
    assert [(node.attachment, node.resource_key, node.label) for node in depth] == [
        ("DSV", "resource.80", "D32")
    ]
    assert all("write" not in edge.label.casefold() for edge in graph.edges)
    category = next(
        node for node in graph.nodes if node.kind is EventFlowNodeKind.CATEGORY_OVERVIEW
    )
    assert category.label == "GBuffer candidate"
    gbuffer_pass = next(
        node
        for node in graph.nodes
        if node.kind is EventFlowNodeKind.PASS_OVERVIEW
        and "Opaque MRT pass" in node.label
    )
    assert "candidate" in gbuffer_pass.label.casefold()


def test_repeated_gbuffer_resource_bindings_keep_unique_attachment_nodes() -> None:
    flow = _flow()
    repeated = dict(flow["flow"]["items"][0])
    repeated["eventId"] = 11
    repeated["executionIndex"] = 1
    flow["flow"]["items"].insert(1, repeated)

    graph = build_event_flow_graph(
        flow,
        diagram_kind="pass-overview",
        capture_session_id=SESSION,
    )
    attachments = [
        node for node in graph.nodes if node.kind is EventFlowNodeKind.GBUFFER_ATTACHMENT
    ]

    assert len(attachments) == 4
    assert len({node.node_id for node in attachments}) == 4
    depth = [
        node for node in graph.nodes if node.kind is EventFlowNodeKind.DEPTH_ATTACHMENT
    ]
    assert len(depth) == 2
    assert len({node.node_id for node in depth}) == 2


def test_pass_overview_preserves_first_appearance_and_has_category_pass_layers() -> None:
    flow = _flow()
    first, second = flow["flow"]["items"]
    first["category"] = "Post"
    first["subcategory"] = "Tone map"
    first["targets"] = {"renderTargets": [], "depthTargets": []}
    second["category"] = "Depth"
    second["subcategory"] = "Depth prepass"

    graph = build_event_flow_graph(
        flow,
        diagram_kind="pass-overview",
        capture_session_id=SESSION,
    )

    assert [
        node.label
        for node in graph.nodes
        if node.kind is EventFlowNodeKind.CATEGORY_OVERVIEW
    ] == ["Post", "Depth"]
    assert [
        node.label
        for node in graph.nodes
        if node.kind is EventFlowNodeKind.PASS_OVERVIEW
    ] == ["Tone map", "Depth prepass"]
    assert [
        node.event_id
        for node in graph.nodes
        if node.kind is EventFlowNodeKind.EVENT
    ] == [10, 20]


def test_pass_overview_splits_noncontiguous_occurrences_of_the_same_label() -> None:
    flow = _flow()
    first = flow["flow"]["items"][0]
    first["category"] = "Post"
    first["subcategory"] = "Tone map"
    first["targets"] = {"renderTargets": [], "depthTargets": []}
    adjacent = json.loads(json.dumps(first))
    adjacent["eventId"] = 11
    adjacent["executionIndex"] = 1
    middle = flow["flow"]["items"][1]
    middle["eventId"] = 20
    middle["executionIndex"] = 2
    middle["subcategory"] = "Deferred light pass"
    repeated = json.loads(json.dumps(first))
    repeated["eventId"] = 30
    repeated["executionIndex"] = 3
    flow["flow"]["items"] = [first, adjacent, middle, repeated]
    flow["flow"]["summary"]["resourceFlowEdges"] = []

    graph = build_event_flow_graph(
        flow,
        diagram_kind="pass-overview",
        capture_session_id=SESSION,
    )

    assert [
        node.label
        for node in graph.nodes
        if node.kind is EventFlowNodeKind.PASS_OVERVIEW
    ] == ["Tone map", "Deferred light pass", "Tone map #2"]
    assert [
        node.event_id
        for node in graph.nodes
        if node.kind is EventFlowNodeKind.EVENT
    ] == [10, 11, 20, 30]


def test_pass_overview_reports_every_known_local_node_and_edge_drop() -> None:
    flow = _flow()
    flow["flow"]["items"] = []
    categories = [
        "GBuffer",
        "Depth",
        "Shadow",
        "Lighting",
        "Transparent",
        "Post",
        "UI",
    ]
    event_id = 1
    for category in categories:
        for representative in range(2):
            render_targets = []
            depth_targets = []
            if category == "GBuffer":
                render_targets = [
                    {
                        "slot": slot,
                        "resourceId": 1000 + representative * 10 + slot,
                        "format": "RGBA8",
                    }
                    for slot in range(8)
                ]
                depth_targets = [
                    {
                        "resourceId": 2000 + representative,
                        "format": "D32",
                    }
                ]
            flow["flow"]["items"].append(
                {
                    "eventId": event_id,
                    "executionIndex": event_id,
                    "name": "%s draw %d" % (category, representative),
                    "category": category,
                    "subcategory": "%s pass" % category,
                    "confidence": "candidate",
                    "actionKind": "graphics",
                    "shaders": [],
                    "targets": {
                        "renderTargets": render_targets,
                        "depthTargets": depth_targets,
                    },
                }
            )
            event_id += 1
    flow["flow"]["summary"]["resourceFlowEdges"] = []

    graph = build_event_flow_graph(
        flow,
        diagram_kind="pass-overview",
        capture_session_id=SESSION,
    )

    assert len(graph.nodes) == 40
    assert graph.truncation.source_truncated is True
    assert graph.truncation.omitted_node_count_lower_bound >= 6
    assert graph.truncation.omitted_edge_count_lower_bound >= 6
    assert "diagram-node-bound" in graph.truncation.reasons


def test_source_truncation_flags_supply_nonzero_lower_bounds() -> None:
    flow = _flow()
    summary = flow["flow"]["summary"]
    summary["resourceFlowEdges"] = []
    summary["sourceTruncated"] = True
    summary["resourceFlowTruncated"] = True

    graph = build_event_flow_graph(
        flow,
        diagram_kind="resource-flow",
        capture_session_id=SESSION,
    )

    assert graph.truncation.omitted_node_count_lower_bound >= 1
    assert graph.truncation.omitted_edge_count_lower_bound >= 1
    assert "source-action-truncated" in graph.truncation.reasons
    assert "source-resource-flow-truncated" in graph.truncation.reasons


def test_capture_controlled_url_label_is_replaced_not_rendered() -> None:
    flow = _flow()
    flow["flow"]["items"][0]["name"] = "https://capture.invalid/script"

    graph = build_event_flow_graph(
        flow,
        diagram_kind="resource-flow",
        capture_session_id=SESSION,
    )

    assert graph.nodes[0].label == "GPU action"


def test_managed_publication_is_content_addressed_hash_verified_and_reusable(tmp_path) -> None:
    source = _flow()
    graph = build_event_flow_graph(
        source,
        diagram_kind="pass-overview",
        capture_session_id=SESSION,
        capture_id=CAPTURE,
    )

    first = publish_event_flow_artifact(graph, artifact_root=tmp_path)
    second = publish_event_flow_artifact(graph, artifact_root=tmp_path)

    assert first["schema"] == EVENT_FLOW_ARTIFACT_SCHEMA
    assert first["created"] is True and first["reused"] is False
    assert second["created"] is False and second["reused"] is True
    artifact = first["artifact"]
    assert artifact["schema"] == EVENT_FLOW_MANIFEST_SCHEMA
    svg_path = Path(artifact["path"])
    manifest_path = Path(first["manifestPath"])
    assert svg_path.is_relative_to(tmp_path)
    assert manifest_path.is_relative_to(tmp_path)
    assert hashlib.sha256(svg_path.read_bytes()).hexdigest() == artifact["sha256"]
    manifest_bytes = manifest_path.read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == first["manifestSha256"]
    assert json.loads(manifest_bytes) == artifact
    expected_source_digest = hashlib.sha256(
        json.dumps(
            source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert graph.snapshot_id == expected_source_digest
    assert artifact["graph"]["snapshotId"] == expected_source_digest
    assert artifact["provenance"] == {
        "schema": "replaylab-event-flow-provenance/v1",
        "schemaVersion": 1,
        "sourceTool": "analyze_current_frame_flow",
        "sourceFlowResultSha256": expected_source_digest,
        "graphIrSha256": artifact["graph"]["graphIrSha256"],
        "captureSessionId": SESSION,
        "captureId": CAPTURE,
    }
    assert artifact["policy"] == {
        "hostManagedPath": True,
        "noOverwrite": True,
        "displayDerived": True,
        "physicalEvidence": False,
        "replayMutation": False,
    }


def test_managed_publication_rejects_redirected_session_directory(tmp_path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    session = tmp_path / SESSION
    try:
        session.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    graph = build_event_flow_graph(
        _flow(), diagram_kind="resource-flow", capture_session_id=SESSION
    )

    with pytest.raises(EventFlowDiagramError, match="redirected"):
        publish_event_flow_artifact(graph, artifact_root=tmp_path)


def test_managed_publication_rejects_redirected_root(tmp_path) -> None:
    target = tmp_path / "outside-root"
    target.mkdir()
    redirected = tmp_path / "redirected-root"
    try:
        redirected.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    graph = build_event_flow_graph(
        _flow(), diagram_kind="resource-flow", capture_session_id=SESSION
    )

    with pytest.raises(EventFlowDiagramError, match="root is redirected"):
        publish_event_flow_artifact(graph, artifact_root=redirected)


def test_default_artifact_root_uses_only_host_environment() -> None:
    root = default_event_artifact_root(
        {"LOCALAPPDATA": "D:/managed-local", "XDG_DATA_HOME": "D:/ignored"}
    )
    assert root == Path("D:/managed-local/ReplayLab/event-artifacts").resolve()


def test_diagram_kind_is_closed() -> None:
    with pytest.raises(EventFlowDiagramError, match="unsupported"):
        build_event_flow_graph(
            _flow(),
            diagram_kind="model-css",
            capture_session_id=SESSION,
        )
