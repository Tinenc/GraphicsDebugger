from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import xml.etree.ElementTree as ET

import pytest

from src.event_flow_diagram import (
    DIAGRAM_METADATA_SCHEMA,
    MAX_EDGES,
    MAX_NODES,
    EventFlowDiagramError,
    EventFlowEdge,
    EventFlowEdgeKind,
    EventFlowGraphIR,
    EventFlowLayout,
    EventFlowNode,
    EventFlowNodeKind,
    EventFlowTheme,
    EventFlowTruncation,
    render_event_flow_diagram,
)


SESSION = "a" * 32
CAPTURE = "b" * 64
SNAPSHOT = "c" * 64


def _evidence(index: int) -> str:
    return "evidence-host-%d" % index


def _representative_graph(
    *, truncation: EventFlowTruncation | None = None
) -> EventFlowGraphIR:
    nodes = (
        EventFlowNode(
            node_id="C1",
            kind=EventFlowNodeKind.CATEGORY_OVERVIEW,
            label="GBuffer candidate",
        ),
        EventFlowNode(
            node_id="P1",
            kind=EventFlowNodeKind.PASS_OVERVIEW,
            label="GBuffer pass",
        ),
        EventFlowNode(
            node_id="E101",
            kind=EventFlowNodeKind.EVENT,
            event_id=101,
            label="Opaque draw",
        ),
        EventFlowNode(
            node_id="Svs",
            kind=EventFlowNodeKind.PIPELINE_STAGE,
            stage="VS",
            label="Vertex processing",
            lane=1,
        ),
        EventFlowNode(
            node_id="Sps",
            kind=EventFlowNodeKind.PIPELINE_STAGE,
            stage="PS",
            label="Fragment shading",
            lane=1,
        ),
        EventFlowNode(
            node_id="Rscene",
            kind=EventFlowNodeKind.RESOURCE_FLOW,
            resource_key="resource.42",
            label="SceneColor",
            lane=2,
        ),
        EventFlowNode(
            node_id="G0",
            kind=EventFlowNodeKind.GBUFFER_ATTACHMENT,
            resource_key="resource.77",
            attachment="RT0",
            label="RGBA8",
            lane=3,
        ),
        EventFlowNode(
            node_id="D0",
            kind=EventFlowNodeKind.DEPTH_ATTACHMENT,
            resource_key="resource.88",
            attachment="DSV",
            label="D32",
            lane=3,
        ),
    )
    edges = (
        EventFlowEdge(
            source_id="C1",
            target_id="P1",
            kind=EventFlowEdgeKind.PASS_OVERVIEW,
            evidence_ref=_evidence(1),
        ),
        EventFlowEdge(
            source_id="P1",
            target_id="E101",
            kind=EventFlowEdgeKind.PASS_OVERVIEW,
            evidence_ref=_evidence(2),
        ),
        EventFlowEdge(
            source_id="Svs",
            target_id="Sps",
            kind=EventFlowEdgeKind.PIPELINE_STAGE,
            evidence_ref=_evidence(3),
            label="VS to PS",
        ),
        EventFlowEdge(
            source_id="E101",
            target_id="Rscene",
            kind=EventFlowEdgeKind.RESOURCE_FLOW,
            evidence_ref=_evidence(4),
            resource_key="resource.42",
            stage_access_token="color-write",
            label="RTV output",
        ),
        EventFlowEdge(
            source_id="E101",
            target_id="G0",
            kind=EventFlowEdgeKind.GBUFFER_ATTACHMENT,
            evidence_ref=_evidence(5),
            resource_key="resource.77",
            label="RT0 physical attachment binding",
        ),
        EventFlowEdge(
            source_id="E101",
            target_id="D0",
            kind=EventFlowEdgeKind.DEPTH_ATTACHMENT,
            evidence_ref=_evidence(6),
            resource_key="resource.88",
            label="DSV physical attachment binding",
        ),
    )
    return EventFlowGraphIR(
        capture_session_id=SESSION,
        capture_id=CAPTURE,
        snapshot_id=SNAPSHOT,
        nodes=nodes,
        edges=edges,
        truncation=truncation or EventFlowTruncation(),
    )


def test_render_is_deterministic_parseable_and_self_hashed() -> None:
    graph = _representative_graph()

    first = render_event_flow_diagram(graph)
    second = render_event_flow_diagram(graph)

    assert first == second
    assert first.svg_sha256 == sha256(first.svg_bytes).hexdigest()
    assert first.metadata.svg_sha256 == first.svg_sha256
    assert first.metadata.schema == DIAGRAM_METADATA_SCHEMA
    assert first.metadata.theme == "evidence-neutral-v1"
    assert first.metadata.layout == "LR"
    assert first.metadata.node_count == 8
    assert first.metadata.edge_count == 6
    assert first.metadata.capture_session_id == SESSION
    assert first.metadata.capture_id == CAPTURE
    assert first.metadata.snapshot_id == SNAPSHOT
    ET.fromstring(first.svg_bytes)

    expected_graph_hash = sha256(
        json.dumps(
            graph.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert first.metadata.graph_ir_sha256 == expected_graph_hash


def test_fixed_theme_renders_all_shapes_line_patterns_labels_and_legend() -> None:
    svg = render_event_flow_diagram(_representative_graph()).svg_bytes.decode("utf-8")

    assert "Bread Event Flow Diagram" in svg
    assert "Bread Event Flow" in svg
    assert "ReplayLab Event Flow" not in svg
    assert "EID 101" in svg
    assert "[Resource] resource.42" in svg
    assert "[Stage] VS" in svg
    assert "[Category]" in svg
    assert "[Pass]" in svg
    assert "[GBuffer candidate] RT0" in svg
    assert "[Depth] DSV" in svg
    assert "folder: category overview" in svg
    assert "rounded: exact EID" in svg
    assert "ellipse: resource" in svg
    assert "hexagon: pipeline stage" in svg
    assert "double box: pass overview" in svg
    assert "diamond: RT attachment candidate" in svg
    assert "octagon: physical DSV attachment" in svg
    assert "solid: proven resource flow" in svg
    assert "long dash: pipeline transition" in svg
    assert "dot: pass membership" in svg
    assert "dash-dot: physical attachment binding" in svg
    assert "short dash: physical DSV binding" in svg
    assert "attachment write" not in svg
    assert 'stroke-dasharray="9 5"' in svg
    assert 'stroke-dasharray="2 5"' in svg
    assert 'stroke-dasharray="12 4 2 4"' in svg
    assert "color is supplementary" in svg


def test_renderer_accepts_only_the_strict_host_ir_type() -> None:
    with pytest.raises(EventFlowDiagramError, match="only validated"):
        render_event_flow_diagram(_representative_graph().canonical_dict())  # type: ignore[arg-type]


def test_theme_layout_and_collection_containers_are_closed_enums() -> None:
    graph = _representative_graph()
    with pytest.raises(EventFlowDiagramError, match="theme"):
        replace(graph, theme="dark")  # type: ignore[arg-type]
    with pytest.raises(EventFlowDiagramError, match="layout"):
        replace(graph, layout="TD")  # type: ignore[arg-type]
    with pytest.raises(EventFlowDiagramError, match="validated EventFlowNode"):
        replace(graph, nodes=(graph.nodes[0], {"id": "E1"}))  # type: ignore[arg-type]
    with pytest.raises(EventFlowDiagramError, match="tuple"):
        replace(graph, edges=list(graph.edges))  # type: ignore[arg-type]
    assert graph.theme is EventFlowTheme.EVIDENCE_NEUTRAL_V1
    assert graph.layout is EventFlowLayout.LR


def test_event_node_identity_cannot_hide_or_relabel_native_eid() -> None:
    with pytest.raises(EventFlowDiagramError, match="exact E"):
        EventFlowNode(
            node_id="E102",
            kind=EventFlowNodeKind.EVENT,
            event_id=101,
            label="Draw",
        )
    with pytest.raises(EventFlowDiagramError, match="positive native EID"):
        EventFlowNode(
            node_id="E0",
            kind=EventFlowNodeKind.EVENT,
            event_id=0,
            label="Draw",
        )


@pytest.mark.parametrize(
    "value,message",
    (
        ("line one\nline two", "control or bidi"),
        ("reversed \u202e text", "control or bidi"),
        ("https://example.invalid", "URL"),
        ("javascript:alert(1)", "URL"),
    ),
)
def test_dynamic_text_rejects_controls_bidi_and_urls(value: str, message: str) -> None:
    with pytest.raises(EventFlowDiagramError, match=message):
        EventFlowNode(
            node_id="E1",
            kind=EventFlowNodeKind.EVENT,
            event_id=1,
            label=value,
        )


def test_plain_text_is_xml_escaped_without_active_svg_content() -> None:
    graph = _representative_graph()
    nodes = list(graph.nodes)
    nodes[1] = replace(
        nodes[1],
        label='Draw </text><script>alert("x")</script> & selected',
    )
    edges = list(graph.edges)
    edges[2] = replace(edges[2], label='write </text><a href="x">link</a>')
    rendered = render_event_flow_diagram(
        replace(graph, nodes=tuple(nodes), edges=tuple(edges))
    )
    svg = rendered.svg_bytes.decode("utf-8")

    assert "&lt;script&gt;" in svg
    assert "&lt;a href&#61;&quot;x&quot;&gt;" in svg
    assert "<script" not in svg
    assert "<a " not in svg
    assert "href=" not in svg
    assert "onclick=" not in svg
    assert "<style" not in svg
    assert " style=" not in svg
    assert "<foreignObject" not in svg
    assert "url(" not in svg
    ET.fromstring(rendered.svg_bytes)


def test_truncation_receipt_is_visible_and_preserved_in_metadata() -> None:
    truncation = EventFlowTruncation(
        source_truncated=True,
        omitted_node_count_lower_bound=17,
        omitted_edge_count_lower_bound=23,
        reasons=("action-limit", "host-byte-budget"),
    )
    result = render_event_flow_diagram(
        _representative_graph(truncation=truncation)
    )
    svg = result.svg_bytes.decode("utf-8")

    assert "TRUNCATED" in svg
    assert "omitted nodes &gt;= 17" in svg
    assert "omitted edges &gt;= 23" in svg
    assert "action-limit, host-byte-budget" in svg
    assert result.metadata.truncation is truncation
    assert result.metadata.to_dict()["truncation"] == {
        "sourceTruncated": True,
        "omittedNodeCountLowerBound": 17,
        "omittedEdgeCountLowerBound": 23,
        "reasons": ["action-limit", "host-byte-budget"],
    }


def test_complete_receipt_cannot_smuggle_omission_counts_or_reasons() -> None:
    with pytest.raises(EventFlowDiagramError, match="complete graph"):
        EventFlowTruncation(
            source_truncated=False,
            omitted_node_count_lower_bound=1,
        )
    with pytest.raises(EventFlowDiagramError, match="complete graph"):
        EventFlowTruncation(
            source_truncated=False,
            reasons=("unknown",),
        )


def test_edges_require_declared_forward_nodes_and_unique_typed_identity() -> None:
    graph = _representative_graph()
    with pytest.raises(EventFlowDiagramError, match="undeclared"):
        replace(
            graph,
            edges=(
                EventFlowEdge(
                    source_id="E101",
                    target_id="Rmissing",
                    kind=EventFlowEdgeKind.RESOURCE_FLOW,
                    evidence_ref=_evidence(1),
                    resource_key="resource.1",
                    stage_access_token="read",
                ),
            ),
        )
    with pytest.raises(EventFlowDiagramError, match="Host node order"):
        replace(
            graph,
            edges=(
                EventFlowEdge(
                    source_id="Rscene",
                    target_id="E101",
                    kind=EventFlowEdgeKind.RESOURCE_FLOW,
                    evidence_ref=_evidence(1),
                    resource_key="resource.42",
                    stage_access_token="read",
                ),
            ),
        )
    with pytest.raises(EventFlowDiagramError, match="duplicate typed edge"):
        replace(graph, edges=(graph.edges[0], graph.edges[0]))


def test_distinct_physical_resources_may_share_one_eid_pair() -> None:
    graph = _representative_graph()
    first = graph.edges[3]
    second = replace(
        first,
        evidence_ref=_evidence(99),
        resource_key="resource.43",
        label="second resource",
    )

    result = render_event_flow_diagram(replace(graph, edges=(first, second)))
    svg = result.svg_bytes.decode("utf-8")

    assert result.metadata.edge_count == 2
    assert "[Resource] resource.42" in svg
    assert "[Resource] resource.43" in svg
    assert svg.count("edge-E101-Rscene-resource-flow-") == 2


def test_edge_kind_specific_physical_fields_and_endpoints_are_strict() -> None:
    with pytest.raises(EventFlowDiagramError, match="require resource_key"):
        EventFlowEdge(
            source_id="E1",
            target_id="E2",
            kind=EventFlowEdgeKind.RESOURCE_FLOW,
            evidence_ref=_evidence(1),
        )
    with pytest.raises(EventFlowDiagramError, match="cannot relabel"):
        EventFlowEdge(
            source_id="S1",
            target_id="S2",
            kind=EventFlowEdgeKind.PIPELINE_STAGE,
            evidence_ref=_evidence(1),
            resource_key="resource.1",
        )

    graph = _representative_graph()
    invalid_pipeline = replace(
        graph.edges[2], source_id="E101", target_id="Sps"
    )
    with pytest.raises(EventFlowDiagramError, match="two pipeline-stage"):
        replace(graph, edges=(invalid_pipeline,))
    invalid_pass = replace(
        graph.edges[1], source_id="E101", target_id="Rscene"
    )
    with pytest.raises(EventFlowDiagramError, match="category-to-pass or pass-to-EID"):
        replace(graph, edges=(invalid_pass,))
    invalid_attachment = replace(
        graph.edges[4], target_id="Rscene"
    )
    with pytest.raises(EventFlowDiagramError, match="EID-to-attachment"):
        replace(graph, edges=(invalid_attachment,))


def test_maximum_valid_graph_is_bounded_and_one_over_limit_is_rejected() -> None:
    nodes = tuple(
        EventFlowNode(
            node_id="E%d" % event_id,
            kind=EventFlowNodeKind.EVENT,
            event_id=event_id,
            label="Draw %d" % event_id,
            lane=(event_id - 1) % 2,
        )
        for event_id in range(1, MAX_NODES + 1)
    )
    pairs = [
        (source, target)
        for source in range(1, MAX_NODES)
        for target in range(source + 1, MAX_NODES + 1)
    ][:MAX_EDGES]
    edges = tuple(
        EventFlowEdge(
            source_id="E%d" % source,
            target_id="E%d" % target,
            kind=EventFlowEdgeKind.RESOURCE_FLOW,
            evidence_ref=_evidence(index + 1),
            resource_key="resource.%d" % (index + 1),
            stage_access_token="read",
        )
        for index, (source, target) in enumerate(pairs)
    )
    graph = EventFlowGraphIR(
        capture_session_id=SESSION,
        nodes=nodes,
        edges=edges,
        truncation=EventFlowTruncation(),
    )

    result = render_event_flow_diagram(graph)
    assert result.metadata.node_count == MAX_NODES
    assert result.metadata.edge_count == MAX_EDGES
    assert len(result.svg_bytes) <= 512 * 1024

    with pytest.raises(EventFlowDiagramError, match="at most 64"):
        replace(graph, edges=edges + (edges[-1],))
    with pytest.raises(EventFlowDiagramError, match="between 1 and 40"):
        replace(
            graph,
            nodes=nodes
            + (
                EventFlowNode(
                    node_id="E41",
                    kind=EventFlowNodeKind.EVENT,
                    event_id=41,
                    label="Draw 41",
                ),
            ),
        )


def test_renderer_has_no_path_or_file_output_parameter() -> None:
    names = render_event_flow_diagram.__annotations__
    assert set(names) == {"graph", "return"}
    result = render_event_flow_diagram(_representative_graph())
    assert isinstance(result.svg_bytes, bytes)
    assert result.metadata.mime_type == "image/svg+xml"
