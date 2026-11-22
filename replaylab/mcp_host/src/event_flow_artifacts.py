"""Host-owned graph construction and managed publication for Event diagrams."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from typing import Any, Mapping, Sequence

from .event_flow_diagram import (
    MAX_EDGES,
    MAX_NODES,
    THEME_EVIDENCE_NEUTRAL_V1,
    EventFlowDiagramError,
    EventFlowEdge,
    EventFlowEdgeKind,
    EventFlowGraphIR,
    EventFlowNode,
    EventFlowNodeKind,
    EventFlowTruncation,
    render_event_flow_diagram,
)


EVENT_FLOW_ARTIFACT_SCHEMA = "replaylab-event-flow-artifact/v1"
EVENT_FLOW_MANIFEST_SCHEMA = "replaylab-event-flow-artifact-manifest/v1"
EVENT_ARTIFACTS_HOME_ENV = "RENDERDOC_ULTRA_EVENT_ARTIFACTS_HOME"
DIAGRAM_KINDS = frozenset(("resource-flow", "pipeline-stage", "pass-overview"))
_API_PIPELINES = {
    "d3d": {
        "order": ("IA", "VS", "HS", "DS", "GS", "AS", "MS", "RS", "PS", "OM", "CS"),
        "front": "IA",
        "output": "OM",
        "stages": {
            "vertex": "VS",
            "hull": "HS",
            "domain": "DS",
            "geometry": "GS",
            # RenderDoc exposes the D3D12 amplification stage through the
            # cross-API task-stage slot.
            "task": "AS",
            "amplification": "AS",
            "mesh": "MS",
            "fragment": "PS",
            "pixel": "PS",
            "compute": "CS",
        },
    },
    "vulkan": {
        "order": ("VTX", "VS", "TCS", "TES", "GS", "TS", "MS", "RS", "FS", "FB", "CS"),
        "front": "VTX",
        "output": "FB",
        "stages": {
            "vertex": "VS",
            "hull": "TCS",
            "domain": "TES",
            "geometry": "GS",
            "task": "TS",
            "amplification": "TS",
            "mesh": "MS",
            "fragment": "FS",
            "pixel": "FS",
            "compute": "CS",
        },
    },
    "opengl": {
        "order": ("VTX", "VS", "TCS", "TES", "GS", "TS", "MS", "RS", "FS", "FB", "CS"),
        "front": "VTX",
        "output": "FB",
        "stages": {
            "vertex": "VS",
            "hull": "TCS",
            "domain": "TES",
            "geometry": "GS",
            "task": "TS",
            "amplification": "TS",
            "mesh": "MS",
            "fragment": "FS",
            "pixel": "FS",
            "compute": "CS",
        },
    },
}
_URL_LIKE = re.compile(r"(?:\b(?:https?|javascript|data|file):|\bwww\.)", re.IGNORECASE)


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_capture_label(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if not text or _URL_LIKE.search(text):
        return fallback
    cleaned = []
    for character in text:
        codepoint = ord(character)
        if (
            codepoint < 0x20
            or 0x7F <= codepoint <= 0x9F
            or codepoint in (0x061C, 0x200E, 0x200F)
            or 0x202A <= codepoint <= 0x202E
            or 0x2066 <= codepoint <= 0x2069
        ):
            cleaned.append(" ")
        else:
            cleaned.append(character)
    result = " ".join("".join(cleaned).split())
    return result[:120] or fallback


def _resource_key(value: Any) -> str:
    resource_id = _integer(value)
    return "resource.%d" % resource_id if resource_id > 0 else ""


def _source_flow_sha256(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, OverflowError, UnicodeEncodeError) as exc:
        raise EventFlowDiagramError(
            "current-frame flow receipt is not strict canonical JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _evidence_ref(kind: str, snapshot_id: str, index: int) -> str:
    return "evidence-%s-%s-%d" % (kind, snapshot_id[:24], index)


def _api_family(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EventFlowDiagramError("pipeline-stage diagram requires the frozen capture API")
    compact = "".join(
        character
        for character in value.strip().casefold()
        if character in "abcdefghijklmnopqrstuvwxyz0123456789"
    )
    if compact.startswith("graphicsapi"):
        compact = compact[len("graphicsapi") :]
    if compact in {
        "d3d",
        "d3d11",
        "d3d12",
        "direct3d",
        "direct3d11",
        "direct3d12",
        "directx11",
        "directx12",
        "dx11",
        "dx12",
    }:
        return "d3d"
    if compact == "vulkan":
        return "vulkan"
    if compact in {"gl", "gles", "opengl", "opengles"}:
        return "opengl"
    raise EventFlowDiagramError("pipeline-stage diagram capture API is unsupported")


def _flow_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    flow = value.get("flow") if value.get("schema") == "replaylab-current-frame-flow-mcp/v1" else value
    if not isinstance(flow, Mapping) or not isinstance(flow.get("items"), list):
        raise EventFlowDiagramError("current-frame flow payload is invalid")
    if not isinstance(flow.get("summary"), Mapping):
        raise EventFlowDiagramError("current-frame flow summary is invalid")
    return flow


def _ordered_items(flow: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    seen_events: set[int] = set()
    for index, item in enumerate(flow.get("items", [])):
        if not isinstance(item, Mapping):
            raise EventFlowDiagramError(
                "current-frame flow item %d is not structured" % index
            )
        event_id = item.get("eventId")
        execution_index = item.get("executionIndex")
        if (
            isinstance(event_id, bool)
            or not isinstance(event_id, int)
            or event_id < 1
            or event_id > 0xFFFFFFFF
            or event_id in seen_events
        ):
            raise EventFlowDiagramError(
                "current-frame flow items require unique positive native EIDs"
            )
        if (
            isinstance(execution_index, bool)
            or not isinstance(execution_index, int)
            or execution_index < 0
            or execution_index > 0xFFFFFFFF
        ):
            raise EventFlowDiagramError(
                "current-frame flow items require bounded executionIndex values"
            )
        seen_events.add(event_id)
        result.append(item)
    result.sort(key=lambda item: (item["executionIndex"], item["eventId"]))
    return result


def _truncation(
    summary: Mapping[str, Any],
    *,
    omitted_nodes: int = 0,
    omitted_edges: int = 0,
    reasons: Sequence[str] = (),
) -> EventFlowTruncation:
    source_omitted = max(
        0,
        _integer(
            summary.get(
                "sourceOmittedEventCountLowerBound",
                summary.get("omittedActionCountLowerBound", 0),
            )
        ),
    )
    source_edge_omitted = max(
        0,
        _integer(summary.get("omittedResourceFlowEdgeCountLowerBound", 0)),
    )
    all_reasons = [str(value)[:120] for value in reasons if str(value).strip()]
    raw_reasons = summary.get("truncationReasons", [])
    if isinstance(raw_reasons, (list, tuple)):
        all_reasons.extend(str(value)[:120] for value in raw_reasons if str(value).strip())
    source_action_truncated = bool(
        summary.get("sourceTruncated") is True
        or summary.get("truncated") is True
    )
    source_resource_truncated = summary.get("resourceFlowTruncated") is True
    # A true truncation flag is itself proof that at least one source item was
    # discarded, even when an older producer omitted its numeric lower bound.
    if source_action_truncated and source_omitted == 0:
        source_omitted = 1
        all_reasons.append("source-action-truncated")
    if source_resource_truncated and source_edge_omitted == 0:
        source_edge_omitted = 1
        all_reasons.append("source-resource-flow-truncated")
    source_truncated = bool(
        source_action_truncated
        or source_resource_truncated
        or source_omitted
        or source_edge_omitted
        or omitted_nodes
        or omitted_edges
    )
    if not source_truncated:
        return EventFlowTruncation()
    unique_reasons = []
    for reason in all_reasons or ["host-bounded-diagram"]:
        if reason not in unique_reasons:
            unique_reasons.append(reason)
    return EventFlowTruncation(
        source_truncated=True,
        omitted_node_count_lower_bound=min(
            0xFFFFFFFF, source_omitted + max(0, omitted_nodes)
        ),
        omitted_edge_count_lower_bound=min(
            0xFFFFFFFF, source_edge_omitted + max(0, omitted_edges)
        ),
        reasons=tuple(unique_reasons[:16]),
    )


def _resource_flow_graph(
    flow: Mapping[str, Any],
    capture_session_id: str,
    capture_id: str | None,
    snapshot_id: str,
) -> EventFlowGraphIR:
    items = _ordered_items(flow)
    item_by_event = {_integer(item.get("eventId")): item for item in items}
    raw_edges = flow["summary"].get("resourceFlowEdges", [])
    candidates: list[tuple[int, Mapping[str, Any]]] = []
    if isinstance(raw_edges, list):
        for source_index, edge in enumerate(raw_edges):
            if not isinstance(edge, Mapping):
                continue
            producer = _integer(edge.get("producerEid"))
            consumer = _integer(edge.get("consumerEid"))
            if (
                producer in item_by_event
                and consumer in item_by_event
                and producer != consumer
                and _resource_key(edge.get("resourceId"))
            ):
                candidates.append((source_index, edge))
    definite = [
        (source_index, edge)
        for source_index, edge in candidates
        if (
            edge.get("authority") == "FixedProgram"
            and edge.get("producerWriteCertainty") == "definite"
            and edge.get("subresourceOverlap") == "overlap"
            and edge.get("interveningWriteUncertain") is not True
        )
    ]
    definite.sort(
        key=lambda pair: (
            _integer(pair[1].get("producerEid")),
            _integer(pair[1].get("consumerEid")),
            _integer(pair[1].get("resourceId")),
            str(pair[1].get("consumerStage") or ""),
            str(pair[1].get("consumerAccess") or ""),
            pair[0],
        )
    )
    involved = {
        _integer(edge.get(name))
        for _source_index, edge in definite
        for name in ("producerEid", "consumerEid")
    }
    selected = [item for item in items if _integer(item.get("eventId")) in involved]
    for item in items:
        if len(selected) >= MAX_NODES:
            break
        if item not in selected:
            selected.append(item)
    selected.sort(key=lambda item: (_integer(item.get("executionIndex")), _integer(item.get("eventId"))))
    selected = selected[:MAX_NODES]
    selected_ids = {_integer(item.get("eventId")) for item in selected}
    nodes = tuple(
        EventFlowNode(
            node_id="E%d" % _integer(item.get("eventId")),
            kind=EventFlowNodeKind.EVENT,
            event_id=_integer(item.get("eventId")),
            label=_safe_capture_label(item.get("name"), "GPU action"),
        )
        for item in selected
    )
    order = {node.event_id: index for index, node in enumerate(nodes)}
    admitted_edges = []
    admitted_edge_keys: set[tuple[int, int, str, str]] = set()
    for index, (_source_index, edge) in enumerate(definite):
        producer = _integer(edge.get("producerEid"))
        consumer = _integer(edge.get("consumerEid"))
        if producer not in selected_ids or consumer not in selected_ids:
            continue
        if order[producer] >= order[consumer] or len(admitted_edges) >= MAX_EDGES:
            continue
        key = _resource_key(edge.get("resourceId"))
        if not key:
            continue
        stage = str(edge.get("consumerStage") or edge.get("consumerAccess") or "read").strip()
        stage = re.sub(r"[^A-Za-z0-9_.:-]", "-", stage)[:32] or "read"
        typed_key = (producer, consumer, key, stage)
        if typed_key in admitted_edge_keys:
            continue
        admitted_edge_keys.add(typed_key)
        admitted_edges.append(
            EventFlowEdge(
                source_id="E%d" % producer,
                target_id="E%d" % consumer,
                kind=EventFlowEdgeKind.RESOURCE_FLOW,
                evidence_ref=_evidence_ref("flow", snapshot_id, index + 1),
                label="%s %s" % (key, stage),
                resource_key=key,
                stage_access_token=stage,
            )
        )
    return EventFlowGraphIR(
        capture_session_id=capture_session_id,
        capture_id=capture_id,
        snapshot_id=snapshot_id,
        nodes=nodes,
        edges=tuple(admitted_edges),
        truncation=_truncation(
            flow["summary"],
            omitted_nodes=max(0, len(items) - len(nodes)),
            omitted_edges=max(0, len(candidates) - len(admitted_edges)),
            reasons=(
                "diagram-node-bound" if len(items) > len(nodes) else "",
                (
                    "uncertain-resource-flow-omitted"
                    if len(candidates) > len(definite)
                    else ""
                ),
                (
                    "diagram-edge-bound"
                    if len(admitted_edges) >= MAX_EDGES
                    and len(definite) > len(admitted_edges)
                    else ""
                ),
                (
                    "resource-flow-projection-omitted"
                    if len(definite) > len(admitted_edges)
                    else ""
                ),
            ),
        ),
    )


def _pipeline_stage_graph(
    flow: Mapping[str, Any],
    capture_session_id: str,
    capture_id: str | None,
    event_id: int,
    capture_api: str,
    snapshot_id: str,
) -> EventFlowGraphIR:
    items = _ordered_items(flow)
    item = next((value for value in items if _integer(value.get("eventId")) == event_id), None)
    if item is None:
        raise EventFlowDiagramError("pipeline-stage diagram EID is outside the admitted flow")
    family = _api_family(capture_api)
    pipeline = _API_PIPELINES[family]
    action_kind = str(item.get("actionKind", "")).casefold()
    stages = []
    raw_shaders = item.get("shaders", [])
    shader_stages = []
    if isinstance(raw_shaders, list):
        for shader in raw_shaders:
            if not isinstance(shader, Mapping):
                continue
            stage = pipeline["stages"].get(
                str(shader.get("stage", "")).casefold()
            )
            if stage and stage not in shader_stages:
                shader_stages.append(stage)
    if action_kind == "graphics" and not any(
        stage in {"AS", "TS", "MS"} for stage in shader_stages
    ):
        stages.append(pipeline["front"])
    stages.extend(shader_stages)
    if action_kind == "graphics":
        stages.append("RS")
        targets = item.get("targets", {})
        if isinstance(targets, Mapping) and (
            targets.get("renderTargets") or targets.get("depthTargets")
        ):
            stages.append(pipeline["output"])
    stages = sorted(set(stages), key=pipeline["order"].index)
    if not stages:
        raise EventFlowDiagramError("the selected EID exposes no participating pipeline stage")
    nodes = [
        EventFlowNode(
            node_id="E%d" % event_id,
            kind=EventFlowNodeKind.EVENT,
            event_id=event_id,
            label=_safe_capture_label(item.get("name"), "GPU action"),
        )
    ]
    nodes.extend(
        EventFlowNode(
            node_id="S" + stage.lower(),
            kind=EventFlowNodeKind.PIPELINE_STAGE,
            stage=stage,
            label="bound/involved stage",
            lane=1,
        )
        for stage in stages
    )
    edges = tuple(
        EventFlowEdge(
            source_id="S" + source.lower(),
            target_id="S" + target.lower(),
            kind=EventFlowEdgeKind.PIPELINE_STAGE,
            evidence_ref=_evidence_ref("stage", snapshot_id, index + 1),
            label="%s to %s" % (source, target),
        )
        for index, (source, target) in enumerate(zip(stages, stages[1:]))
    )
    return EventFlowGraphIR(
        capture_session_id=capture_session_id,
        capture_id=capture_id,
        snapshot_id=snapshot_id,
        nodes=tuple(nodes),
        edges=edges,
        truncation=_truncation(flow["summary"]),
    )


def _physical_attachment_candidates(
    item: Mapping[str, Any], category: str
) -> list[dict[str, Any]]:
    """Return capture-bound attachment identities without semantic inference."""

    if category != "GBuffer":
        return []
    event_id = _integer(item.get("eventId"))
    targets = item.get("targets", {})
    targets = targets if isinstance(targets, Mapping) else {}
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    collections = (
        (
            "renderTargets",
            "RT",
            "G",
            EventFlowNodeKind.GBUFFER_ATTACHMENT,
            EventFlowEdgeKind.GBUFFER_ATTACHMENT,
        ),
        (
            "depthTargets",
            "DSV",
            "D",
            EventFlowNodeKind.DEPTH_ATTACHMENT,
            EventFlowEdgeKind.DEPTH_ATTACHMENT,
        ),
    )
    for field, attachment_prefix, node_prefix, node_kind, edge_kind in collections:
        values = targets.get(field, [])
        if not isinstance(values, list):
            continue
        for fallback_slot, target in enumerate(values):
            if not isinstance(target, Mapping):
                continue
            resource_id = _integer(target.get("resourceId"))
            resource_key = _resource_key(resource_id)
            raw_slot = target.get("slot", fallback_slot)
            slot = (
                raw_slot
                if isinstance(raw_slot, int) and not isinstance(raw_slot, bool) and raw_slot >= 0
                else fallback_slot
            )
            identity = (node_prefix, slot, resource_id)
            if not resource_key or identity in seen:
                continue
            seen.add(identity)
            attachment = (
                "RT%d" % slot
                if attachment_prefix == "RT"
                else ("DSV" if slot == 0 else "DSV%d" % slot)
            )
            node_id = "%s%d_%s" % (
                node_prefix,
                slot,
                hashlib.sha256(
                    ("%s:%d:%d:%d" % (node_prefix, event_id, slot, resource_id)).encode(
                        "ascii"
                    )
                ).hexdigest()[:12],
            )
            result.append(
                {
                    "eventId": event_id,
                    "nodeId": node_id,
                    "nodeKind": node_kind,
                    "edgeKind": edge_kind,
                    "resourceKey": resource_key,
                    "attachment": attachment,
                    "format": _safe_capture_label(
                        target.get("format"), "format unknown"
                    ),
                }
            )
    return result


def _pass_overview_graph(
    flow: Mapping[str, Any],
    capture_session_id: str,
    capture_id: str | None,
    snapshot_id: str,
) -> EventFlowGraphIR:
    items = _ordered_items(flow)
    item_records: list[
        tuple[Mapping[str, Any], str, tuple[str, str, int]]
    ] = []
    categories: list[str] = []
    passes: list[tuple[str, str, int]] = []
    occurrence_counts: dict[tuple[str, str], int] = {}
    previous_base_pass: tuple[str, str] | None = None
    for item in items:
        category = _safe_capture_label(item.get("category"), "Unknown")
        pass_label = _safe_capture_label(
            item.get("subcategory"), "%s pass" % category
        )
        if category == "GBuffer" and "candidate" not in pass_label.casefold():
            pass_label = "%s (candidate)" % pass_label
        base_pass = (category, pass_label)
        if base_pass != previous_base_pass:
            occurrence_counts[base_pass] = occurrence_counts.get(base_pass, 0) + 1
        occurrence = occurrence_counts[base_pass]
        pass_key = (category, pass_label, occurrence)
        previous_base_pass = base_pass
        if category not in categories:
            categories.append(category)
        if pass_key not in passes:
            passes.append(pass_key)
        item_records.append((item, category, pass_key))

    category_ids = {
        category: "C%d" % (index + 1) for index, category in enumerate(categories)
    }
    pass_ids = {
        pass_key: "P%d" % (index + 1) for index, pass_key in enumerate(passes)
    }
    all_attachments = {
        _integer(item.get("eventId")): _physical_attachment_candidates(item, category)
        for item, category, _pass_key in item_records
    }
    full_node_count = (
        len(categories)
        + len(passes)
        + len(item_records)
        + sum(len(values) for values in all_attachments.values())
    )
    full_edge_count = (
        len(passes)
        + len(item_records)
        + sum(len(values) for values in all_attachments.values())
    )

    nodes: list[EventFlowNode] = []
    present_categories: set[str] = set()
    present_passes: set[tuple[str, str, int]] = set()
    admitted_passes: list[tuple[str, str, int]] = []
    selected: list[tuple[Mapping[str, Any], str, tuple[str, str, int]]] = []
    representative_counts: dict[tuple[str, str, int], int] = {}
    base_edge_count = 0
    for item, category, pass_key in item_records:
        if representative_counts.get(pass_key, 0) >= 2:
            continue
        missing_category = category not in present_categories
        missing_pass = pass_key not in present_passes
        required_nodes = 1 + int(missing_category) + int(missing_pass)
        required_edges = 1 + int(missing_pass)
        if (
            len(nodes) + required_nodes > MAX_NODES
            or base_edge_count + required_edges > MAX_EDGES
        ):
            continue
        if missing_category:
            nodes.append(
                EventFlowNode(
                    node_id=category_ids[category],
                    kind=EventFlowNodeKind.CATEGORY_OVERVIEW,
                    label=(
                        "GBuffer candidate" if category == "GBuffer" else category
                    ),
                    lane=0,
                )
            )
            present_categories.add(category)
        if missing_pass:
            nodes.append(
                EventFlowNode(
                    node_id=pass_ids[pass_key],
                    kind=EventFlowNodeKind.PASS_OVERVIEW,
                    label=(
                        pass_key[1]
                        if pass_key[2] == 1
                        else "%s #%d" % (pass_key[1], pass_key[2])
                    ),
                    lane=1,
                )
            )
            present_passes.add(pass_key)
            admitted_passes.append(pass_key)
        event_id = _integer(item.get("eventId"))
        nodes.append(
            EventFlowNode(
                node_id="E%d" % event_id,
                kind=EventFlowNodeKind.EVENT,
                event_id=event_id,
                label=_safe_capture_label(item.get("name"), "GPU action"),
                lane=2,
            )
        )
        selected.append((item, category, pass_key))
        representative_counts[pass_key] = representative_counts.get(pass_key, 0) + 1
        base_edge_count += required_edges

    edges: list[EventFlowEdge] = []
    for index, pass_key in enumerate(admitted_passes):
        edges.append(
            EventFlowEdge(
                source_id=category_ids[pass_key[0]],
                target_id=pass_ids[pass_key],
                kind=EventFlowEdgeKind.PASS_OVERVIEW,
                evidence_ref=_evidence_ref("category", snapshot_id, index + 1),
                label="contains pass",
            )
        )
    for index, (item, _category, pass_key) in enumerate(selected):
        confidence = _safe_capture_label(item.get("confidence"), "candidate")
        edges.append(
            EventFlowEdge(
                source_id=pass_ids[pass_key],
                target_id="E%d" % _integer(item.get("eventId")),
                kind=EventFlowEdgeKind.PASS_OVERVIEW,
                evidence_ref=_evidence_ref("pass", snapshot_id, index + 1),
                label=confidence,
            )
        )

    attachment_index = 0
    for item, _category, _pass_key in selected:
        for attachment_value in all_attachments.get(
            _integer(item.get("eventId")), []
        ):
            if len(nodes) >= MAX_NODES or len(edges) >= MAX_EDGES:
                continue
            nodes.append(
                EventFlowNode(
                    node_id=attachment_value["nodeId"],
                    kind=attachment_value["nodeKind"],
                    resource_key=attachment_value["resourceKey"],
                    attachment=attachment_value["attachment"],
                    label=attachment_value["format"],
                    lane=3,
                )
            )
            attachment_index += 1
            evidence_kind = (
                "gbuffer"
                if attachment_value["edgeKind"]
                is EventFlowEdgeKind.GBUFFER_ATTACHMENT
                else "depth"
            )
            edges.append(
                EventFlowEdge(
                    source_id="E%d" % attachment_value["eventId"],
                    target_id=attachment_value["nodeId"],
                    kind=attachment_value["edgeKind"],
                    evidence_ref=_evidence_ref(
                        evidence_kind, snapshot_id, attachment_index
                    ),
                    label="%s physical attachment binding"
                    % attachment_value["attachment"],
                    resource_key=attachment_value["resourceKey"],
                )
            )

    omitted_nodes = max(0, full_node_count - len(nodes))
    omitted_edges = max(0, full_edge_count - len(edges))
    return EventFlowGraphIR(
        capture_session_id=capture_session_id,
        capture_id=capture_id,
        snapshot_id=snapshot_id,
        nodes=tuple(nodes),
        edges=tuple(edges),
        truncation=_truncation(
            flow["summary"],
            omitted_nodes=omitted_nodes,
            omitted_edges=omitted_edges,
            reasons=(
                (
                    "representative-pass-overview"
                    if len(selected) < len(item_records)
                    else ""
                ),
                "diagram-node-bound" if omitted_nodes and len(nodes) >= MAX_NODES else "",
                "diagram-edge-bound" if omitted_edges and len(edges) >= MAX_EDGES else "",
            ),
        ),
    )


def build_event_flow_graph(
    value: Mapping[str, Any],
    *,
    diagram_kind: str,
    capture_session_id: str,
    capture_id: str | None = None,
    event_id: int = 0,
    capture_api: str | None = None,
) -> EventFlowGraphIR:
    """Create strict graph IR only from the Host's current-frame flow receipt."""

    if diagram_kind not in DIAGRAM_KINDS:
        raise EventFlowDiagramError("diagram_kind is unsupported")
    snapshot_id = _source_flow_sha256(value)
    flow = _flow_payload(value)
    if diagram_kind == "resource-flow":
        return _resource_flow_graph(
            flow, capture_session_id, capture_id, snapshot_id
        )
    if diagram_kind == "pipeline-stage":
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 1:
            raise EventFlowDiagramError("pipeline-stage diagram requires a positive event_id")
        return _pipeline_stage_graph(
            flow,
            capture_session_id,
            capture_id,
            event_id,
            capture_api or "",
            snapshot_id,
        )
    return _pass_overview_graph(
        flow, capture_session_id, capture_id, snapshot_id
    )


def default_event_artifact_root(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    configured = str(env.get(EVENT_ARTIFACTS_HOME_ENV, "") or "").strip()
    if configured:
        raw = Path(configured).expanduser()
        if not raw.is_absolute():
            raise EventFlowDiagramError("configured Event artifact root must be absolute")
        return Path(os.path.abspath(str(raw)))
    local = str(env.get("LOCALAPPDATA", "") or "").strip()
    if local:
        return Path(
            os.path.abspath(
                str(Path(local).expanduser() / "ReplayLab" / "event-artifacts")
            )
        )
    xdg = str(env.get("XDG_DATA_HOME", "") or "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return Path(os.path.abspath(str(base / "ReplayLab" / "event-artifacts")))


def _is_redirected(path: Path) -> bool:
    return path.is_symlink() or bool(
        getattr(os.path, "isjunction", lambda unused: False)(path)
    )


def _secure_directory(path: Path, boundary: Path) -> None:
    boundary = Path(os.path.abspath(str(boundary)))
    path = Path(os.path.abspath(str(path)))
    try:
        path.relative_to(boundary)
    except ValueError as exc:
        raise EventFlowDiagramError("managed artifact path escaped its root") from exc
    current = boundary
    current.mkdir(parents=True, exist_ok=True)
    if (
        _is_redirected(current)
        or not current.is_dir()
        or os.path.normcase(str(current.resolve())) != os.path.normcase(str(current))
    ):
        raise EventFlowDiagramError("managed artifact root is redirected")
    for part in path.relative_to(boundary).parts:
        current = current / part
        if current.exists() and (_is_redirected(current) or not current.is_dir()):
            raise EventFlowDiagramError("managed artifact directory is redirected")
        current.mkdir(exist_ok=True)
        if (
            _is_redirected(current)
            or not current.is_dir()
            or os.path.normcase(str(current.resolve())) != os.path.normcase(str(current))
        ):
            raise EventFlowDiagramError("managed artifact directory is redirected")
    try:
        path.chmod(stat.S_IRWXU)
    except OSError:
        pass


def _publish_no_overwrite(path: Path, payload: bytes) -> bool:
    if path.exists():
        if _is_redirected(path) or not path.is_file() or path.read_bytes() != payload:
            raise EventFlowDiagramError("managed artifact hash path is occupied")
        return True
    temporary = path.with_name(".%s.tmp.%s" % (path.name, uuid.uuid4().hex))
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if _is_redirected(temporary) or temporary.read_bytes() != payload:
            raise EventFlowDiagramError("managed temporary artifact changed before publish")
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _is_redirected(path) or not path.is_file() or path.read_bytes() != payload:
                raise EventFlowDiagramError("managed artifact publication raced conflicting bytes")
            return True
        return False
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def publish_event_flow_artifact(
    graph: EventFlowGraphIR, *, artifact_root: Path | None = None
) -> dict[str, Any]:
    """Render and content-address one SVG plus manifest under the Host root."""

    if graph.snapshot_id is None:
        raise EventFlowDiagramError(
            "managed Event diagrams require source-flow snapshot provenance"
        )
    rendered = render_event_flow_diagram(graph)
    root = Path(
        os.path.abspath(str(artifact_root or default_event_artifact_root()))
    )
    directory = root / graph.capture_session_id / "diagrams"
    _secure_directory(directory, root)
    artifact_id = rendered.svg_sha256
    svg_path = directory / (artifact_id + ".svg")
    reused = _publish_no_overwrite(svg_path, rendered.svg_bytes)
    if hashlib.sha256(svg_path.read_bytes()).hexdigest() != artifact_id:
        raise EventFlowDiagramError("published SVG hash verification failed")
    manifest = {
        "schema": EVENT_FLOW_MANIFEST_SCHEMA,
        "schemaVersion": 1,
        "artifactId": artifact_id,
        "artifactType": "event-flow-diagram",
        "path": str(svg_path.resolve()),
        "mimeType": "image/svg+xml",
        "byteSize": len(rendered.svg_bytes),
        "sha256": artifact_id,
        "captureSessionId": graph.capture_session_id,
        "captureId": graph.capture_id,
        "theme": THEME_EVIDENCE_NEUTRAL_V1,
        "layout": "LR",
        "graph": rendered.metadata.to_dict(),
        "provenance": {
            "schema": "replaylab-event-flow-provenance/v1",
            "schemaVersion": 1,
            "sourceTool": "analyze_current_frame_flow",
            "sourceFlowResultSha256": graph.snapshot_id,
            "graphIrSha256": rendered.metadata.graph_ir_sha256,
            "captureSessionId": graph.capture_session_id,
            "captureId": graph.capture_id,
        },
        "policy": {
            "hostManagedPath": True,
            "noOverwrite": True,
            "displayDerived": True,
            "physicalEvidence": False,
            "replayMutation": False,
        },
    }
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_path = directory / (artifact_id + ".manifest.json")
    manifest_reused = _publish_no_overwrite(manifest_path, manifest_bytes)
    return {
        "schema": EVENT_FLOW_ARTIFACT_SCHEMA,
        "schemaVersion": 1,
        "operation": "render-event-flow-artifact",
        "captureSessionId": graph.capture_session_id,
        "captureId": graph.capture_id,
        "artifact": manifest,
        "manifestPath": str(manifest_path.resolve()),
        "manifestSha256": manifest_sha256,
        "created": not reused,
        "reused": reused,
        "manifestCreated": not manifest_reused,
        "applied": False,
        "policy": {
            "hostManagedPath": True,
            "noOverwrite": True,
            "projectMutation": False,
            "uiMutation": False,
            "replayMutation": False,
        },
    }


__all__ = [
    "DIAGRAM_KINDS",
    "EVENT_ARTIFACTS_HOME_ENV",
    "EVENT_FLOW_ARTIFACT_SCHEMA",
    "EVENT_FLOW_MANIFEST_SCHEMA",
    "build_event_flow_graph",
    "default_event_artifact_root",
    "publish_event_flow_artifact",
]
