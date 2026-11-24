"""Deterministic, evidence-neutral SVG rendering for Host-admitted Event graphs.

The renderer deliberately does not accept dictionaries, Mermaid, HTML, file
paths, CSS, URLs, or caller-provided geometry.  A Host must first construct the
strict immutable IR types below.  Rendering is then a pure bytes-in-memory
operation whose layout, palette, shapes, line patterns, legend, and metadata
are fixed by the ``evidence-neutral-v1`` theme.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from html import escape as xml_escape
import json
import re
from typing import Any


GRAPH_IR_SCHEMA = "replaylab-event-flow-graph-ir/v1"
DIAGRAM_METADATA_SCHEMA = "replaylab-event-flow-diagram/v1"
THEME_EVIDENCE_NEUTRAL_V1 = "evidence-neutral-v1"
MAX_NODES = 40
MAX_EDGES = 64
MAX_LANES = 8
MAX_LABEL_CHARACTERS = 160
MAX_REASON_CHARACTERS = 160
MAX_SVG_BYTES = 512 * 1024

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NODE_ID = re.compile(r"^[CDERSPG][A-Za-z0-9_.:-]{0,31}$")
_RESOURCE_KEY = re.compile(r"^[a-z0-9_.:-]{1,96}$")
_STAGE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")
_EVIDENCE_REF = re.compile(r"^evidence-[a-z0-9][a-z0-9-]{0,62}$")
_URL_LIKE = re.compile(
    r"(?:\b(?:https?|javascript|data|file):|\bwww\.)", re.IGNORECASE
)


class EventFlowDiagramError(ValueError):
    """The Host graph IR cannot be rendered safely and deterministically."""


class EventFlowTheme(str, Enum):
    EVIDENCE_NEUTRAL_V1 = THEME_EVIDENCE_NEUTRAL_V1


class EventFlowLayout(str, Enum):
    LR = "LR"


class EventFlowNodeKind(str, Enum):
    CATEGORY_OVERVIEW = "category-overview"
    EVENT = "event"
    RESOURCE_FLOW = "resource-flow"
    PIPELINE_STAGE = "pipeline-stage"
    PASS_OVERVIEW = "pass-overview"
    GBUFFER_ATTACHMENT = "gbuffer-attachment"
    DEPTH_ATTACHMENT = "depth-attachment"


class EventFlowEdgeKind(str, Enum):
    RESOURCE_FLOW = "resource-flow"
    PIPELINE_STAGE = "pipeline-stage"
    PASS_OVERVIEW = "pass-overview"
    GBUFFER_ATTACHMENT = "gbuffer-attachment"
    DEPTH_ATTACHMENT = "depth-attachment"


def _plain_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise EventFlowDiagramError("%s must be text" % field)
    if not value or value != value.strip() or len(value) > maximum:
        raise EventFlowDiagramError("%s has invalid content/length" % field)
    if _URL_LIKE.search(value):
        raise EventFlowDiagramError("%s must not contain a URL" % field)
    for character in value:
        codepoint = ord(character)
        if (
            codepoint < 0x20
            or 0x7F <= codepoint <= 0x9F
            or 0xD800 <= codepoint <= 0xDFFF
            or codepoint in (0x061C, 0x200E, 0x200F, 0xFFFE, 0xFFFF)
            or 0x202A <= codepoint <= 0x202E
            or 0x2066 <= codepoint <= 0x2069
        ):
            raise EventFlowDiagramError(
                "%s contains a forbidden control or bidi character" % field
            )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise EventFlowDiagramError("%s is not valid UTF-8 text" % field) from exc
    return value


def _optional_plain_text(value: Any, field: str, maximum: int) -> str:
    if value == "":
        return ""
    return _plain_text(value, field, maximum)


def _xml_text(value: str) -> str:
    # Attribute-shaped strings are legal character data, but encoding their
    # equals sign prevents a simple downstream scanner from mistaking escaped
    # ``href=``/``style=``/event-handler text for an SVG attribute. It still
    # renders to the exact original character and leaves ordinary ``>=``
    # receipts readable in the serialized SVG.
    escaped = xml_escape(value, quote=True)
    return re.sub(
        r"(?i)\b(href|src|style|on[a-z]+|xmlns|xlink:href)(\s*)=",
        lambda match: "%s%s&#61;" % (match.group(1), match.group(2)),
        escaped,
    )


def _strict_nonnegative_int(value: Any, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EventFlowDiagramError("%s must be an integer" % field)
    if value < 0 or value > maximum:
        raise EventFlowDiagramError("%s is out of range" % field)
    return value


@dataclass(frozen=True, slots=True)
class EventFlowTruncation:
    source_truncated: bool = False
    omitted_node_count_lower_bound: int = 0
    omitted_edge_count_lower_bound: int = 0
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source_truncated, bool):
            raise EventFlowDiagramError("source_truncated must be boolean")
        _strict_nonnegative_int(
            self.omitted_node_count_lower_bound,
            "omitted_node_count_lower_bound",
            0xFFFFFFFF,
        )
        _strict_nonnegative_int(
            self.omitted_edge_count_lower_bound,
            "omitted_edge_count_lower_bound",
            0xFFFFFFFF,
        )
        if not isinstance(self.reasons, tuple) or len(self.reasons) > 16:
            raise EventFlowDiagramError("truncation reasons must be a bounded tuple")
        for index, reason in enumerate(self.reasons):
            _plain_text(
                reason,
                "truncation.reasons[%d]" % index,
                MAX_REASON_CHARACTERS,
            )
        if not self.source_truncated and (
            self.omitted_node_count_lower_bound
            or self.omitted_edge_count_lower_bound
            or self.reasons
        ):
            raise EventFlowDiagramError(
                "a complete graph cannot declare omitted nodes, edges, or reasons"
            )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "sourceTruncated": self.source_truncated,
            "omittedNodeCountLowerBound": self.omitted_node_count_lower_bound,
            "omittedEdgeCountLowerBound": self.omitted_edge_count_lower_bound,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class EventFlowNode:
    node_id: str
    kind: EventFlowNodeKind
    label: str
    lane: int = 0
    event_id: int | None = None
    resource_key: str = ""
    stage: str = ""
    attachment: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or _NODE_ID.fullmatch(self.node_id) is None:
            raise EventFlowDiagramError("node_id is invalid")
        if not isinstance(self.kind, EventFlowNodeKind):
            raise EventFlowDiagramError("node kind must be an EventFlowNodeKind")
        _plain_text(self.label, "node label", MAX_LABEL_CHARACTERS)
        _strict_nonnegative_int(self.lane, "node lane", MAX_LANES - 1)
        if self.event_id is not None and (
            isinstance(self.event_id, bool)
            or not isinstance(self.event_id, int)
            or self.event_id < 1
            or self.event_id > 0xFFFFFFFF
        ):
            raise EventFlowDiagramError("event_id must be a positive native EID")
        if self.resource_key and _RESOURCE_KEY.fullmatch(self.resource_key) is None:
            raise EventFlowDiagramError("node resource_key is invalid")
        if self.stage and _STAGE_TOKEN.fullmatch(self.stage) is None:
            raise EventFlowDiagramError("node stage is invalid")
        if self.attachment:
            _plain_text(self.attachment, "node attachment", 64)

        if self.kind is EventFlowNodeKind.EVENT:
            if self.event_id is None or self.node_id != "E%d" % self.event_id:
                raise EventFlowDiagramError(
                    "event nodes require an exact E<event_id> identity"
                )
            if self.resource_key or self.stage or self.attachment:
                raise EventFlowDiagramError(
                    "event nodes cannot carry resource/stage/attachment identities"
                )
        elif self.event_id is not None:
            raise EventFlowDiagramError("only event nodes may carry event_id")
        elif self.kind is EventFlowNodeKind.CATEGORY_OVERVIEW:
            if not self.node_id.startswith("C"):
                raise EventFlowDiagramError(
                    "category-overview nodes require C identity"
                )
            if self.resource_key or self.stage or self.attachment:
                raise EventFlowDiagramError(
                    "category-overview nodes cannot carry physical identities"
                )
        elif self.kind is EventFlowNodeKind.RESOURCE_FLOW:
            if not self.node_id.startswith("R") or not self.resource_key:
                raise EventFlowDiagramError(
                    "resource-flow nodes require R identity and resource_key"
                )
            if self.stage or self.attachment:
                raise EventFlowDiagramError(
                    "resource-flow nodes cannot carry stage/attachment identities"
                )
        elif self.kind is EventFlowNodeKind.PIPELINE_STAGE:
            if not self.node_id.startswith("S") or not self.stage:
                raise EventFlowDiagramError(
                    "pipeline-stage nodes require S identity and stage"
                )
            if self.resource_key or self.attachment:
                raise EventFlowDiagramError(
                    "pipeline-stage nodes cannot carry resource/attachment identities"
                )
        elif self.kind is EventFlowNodeKind.PASS_OVERVIEW:
            if not self.node_id.startswith("P"):
                raise EventFlowDiagramError("pass-overview nodes require P identity")
            if self.resource_key or self.stage or self.attachment:
                raise EventFlowDiagramError(
                    "pass-overview nodes cannot carry physical identities"
                )
        elif self.kind is EventFlowNodeKind.GBUFFER_ATTACHMENT:
            if (
                not self.node_id.startswith("G")
                or not self.resource_key
                or not self.attachment
            ):
                raise EventFlowDiagramError(
                    "GBuffer nodes require G identity, attachment, and resource_key"
                )
            if self.stage:
                raise EventFlowDiagramError(
                    "GBuffer attachment nodes cannot carry a pipeline stage"
                )
        elif self.kind is EventFlowNodeKind.DEPTH_ATTACHMENT:
            if (
                not self.node_id.startswith("D")
                or not self.resource_key
                or not self.attachment
            ):
                raise EventFlowDiagramError(
                    "depth attachment nodes require D identity, attachment, and resource_key"
                )
            if self.stage:
                raise EventFlowDiagramError(
                    "depth attachment nodes cannot carry a pipeline stage"
                )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "kind": self.kind.value,
            "label": self.label,
            "lane": self.lane,
            "eventId": self.event_id,
            "resourceKey": self.resource_key,
            "stage": self.stage,
            "attachment": self.attachment,
        }


@dataclass(frozen=True, slots=True)
class EventFlowEdge:
    source_id: str
    target_id: str
    kind: EventFlowEdgeKind
    evidence_ref: str
    label: str = ""
    resource_key: str = ""
    stage_access_token: str = ""

    def __post_init__(self) -> None:
        for field, value in (("source_id", self.source_id), ("target_id", self.target_id)):
            if not isinstance(value, str) or _NODE_ID.fullmatch(value) is None:
                raise EventFlowDiagramError("edge %s is invalid" % field)
        if self.source_id == self.target_id:
            raise EventFlowDiagramError("edge endpoints must differ")
        if not isinstance(self.kind, EventFlowEdgeKind):
            raise EventFlowDiagramError("edge kind must be an EventFlowEdgeKind")
        if not isinstance(self.evidence_ref, str) or _EVIDENCE_REF.fullmatch(
            self.evidence_ref
        ) is None:
            raise EventFlowDiagramError("edge evidence_ref is invalid")
        _optional_plain_text(self.label, "edge label", MAX_LABEL_CHARACTERS)
        if self.resource_key and _RESOURCE_KEY.fullmatch(self.resource_key) is None:
            raise EventFlowDiagramError("edge resource_key is invalid")
        if self.stage_access_token and _STAGE_TOKEN.fullmatch(
            self.stage_access_token
        ) is None:
            raise EventFlowDiagramError("edge stage_access_token is invalid")
        if self.kind is EventFlowEdgeKind.RESOURCE_FLOW and (
            not self.resource_key or not self.stage_access_token
        ):
            raise EventFlowDiagramError(
                "resource-flow edges require resource_key and stage_access_token"
            )
        if self.kind in (
            EventFlowEdgeKind.GBUFFER_ATTACHMENT,
            EventFlowEdgeKind.DEPTH_ATTACHMENT,
        ) and not self.resource_key:
            raise EventFlowDiagramError(
                "attachment edges require resource_key"
            )
        if self.kind in (
            EventFlowEdgeKind.PIPELINE_STAGE,
            EventFlowEdgeKind.PASS_OVERVIEW,
        ) and (self.resource_key or self.stage_access_token):
            raise EventFlowDiagramError(
                "pipeline/pass edges cannot relabel a resource identity"
            )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "source": self.source_id,
            "target": self.target_id,
            "kind": self.kind.value,
            "evidenceRef": self.evidence_ref,
            "label": self.label,
            "resourceKey": self.resource_key,
            "stageAccessToken": self.stage_access_token,
        }


@dataclass(frozen=True, slots=True)
class EventFlowGraphIR:
    capture_session_id: str
    nodes: tuple[EventFlowNode, ...]
    edges: tuple[EventFlowEdge, ...]
    truncation: EventFlowTruncation
    capture_id: str | None = None
    snapshot_id: str | None = None
    schema: str = GRAPH_IR_SCHEMA
    schema_version: int = 1
    theme: EventFlowTheme = EventFlowTheme.EVIDENCE_NEUTRAL_V1
    layout: EventFlowLayout = EventFlowLayout.LR

    def __post_init__(self) -> None:
        if self.schema != GRAPH_IR_SCHEMA or self.schema_version != 1:
            raise EventFlowDiagramError("graph IR schema/version is unsupported")
        if not isinstance(self.theme, EventFlowTheme) or self.theme is not (
            EventFlowTheme.EVIDENCE_NEUTRAL_V1
        ):
            raise EventFlowDiagramError("graph theme must be evidence-neutral-v1")
        if not isinstance(self.layout, EventFlowLayout) or self.layout is not (
            EventFlowLayout.LR
        ):
            raise EventFlowDiagramError("graph layout must be LR")
        if not isinstance(self.capture_session_id, str) or _HEX32.fullmatch(
            self.capture_session_id
        ) is None:
            raise EventFlowDiagramError("capture_session_id must be lowercase hex32")
        if self.capture_id is not None and (
            not isinstance(self.capture_id, str)
            or _HEX64.fullmatch(self.capture_id) is None
        ):
            raise EventFlowDiagramError("capture_id must be lowercase hex64 when present")
        if self.snapshot_id is not None and (
            not isinstance(self.snapshot_id, str)
            or _HEX64.fullmatch(self.snapshot_id) is None
        ):
            raise EventFlowDiagramError("snapshot_id must be lowercase hex64 when present")
        if not isinstance(self.nodes, tuple):
            raise EventFlowDiagramError("graph nodes must be an immutable tuple")
        if not 1 <= len(self.nodes) <= MAX_NODES:
            raise EventFlowDiagramError("graph must contain between 1 and 40 nodes")
        if not isinstance(self.edges, tuple):
            raise EventFlowDiagramError("graph edges must be an immutable tuple")
        if len(self.edges) > MAX_EDGES:
            raise EventFlowDiagramError("graph must contain at most 64 edges")
        if not isinstance(self.truncation, EventFlowTruncation):
            raise EventFlowDiagramError("graph truncation receipt is invalid")
        if any(not isinstance(node, EventFlowNode) for node in self.nodes):
            raise EventFlowDiagramError("graph nodes must be validated EventFlowNode values")
        if any(not isinstance(edge, EventFlowEdge) for edge in self.edges):
            raise EventFlowDiagramError("graph edges must be validated EventFlowEdge values")

        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise EventFlowDiagramError("graph node identities must be unique")
        event_ids = [
            node.event_id
            for node in self.nodes
            if node.kind is EventFlowNodeKind.EVENT
        ]
        if not event_ids:
            raise EventFlowDiagramError("graph requires at least one exact EID node")
        if len(event_ids) != len(set(event_ids)):
            raise EventFlowDiagramError("graph native EIDs must be unique")

        node_index = {node_id: index for index, node_id in enumerate(node_ids)}
        edge_keys: set[tuple[str, str, str, str, str]] = set()
        for edge in self.edges:
            if edge.source_id not in node_index or edge.target_id not in node_index:
                raise EventFlowDiagramError("graph edge references an undeclared node")
            if node_index[edge.source_id] >= node_index[edge.target_id]:
                raise EventFlowDiagramError(
                    "LR graph edges must follow the Host node order"
                )
            # Resource-flow edges may legitimately share the same EID pair:
            # deferred lighting commonly reads several MRT resources produced
            # by one geometry EID.  Their physical resource/access identity is
            # part of the typed edge key, while non-resource edge kinds retain
            # their former endpoint/kind uniqueness.
            key = (
                edge.source_id,
                edge.target_id,
                edge.kind.value,
                edge.resource_key,
                edge.stage_access_token,
            )
            if key in edge_keys:
                raise EventFlowDiagramError("graph contains a duplicate typed edge")
            edge_keys.add(key)
            source_kind = self.nodes[node_index[edge.source_id]].kind
            target_kind = self.nodes[node_index[edge.target_id]].kind
            if edge.kind is EventFlowEdgeKind.PIPELINE_STAGE and not (
                source_kind is EventFlowNodeKind.PIPELINE_STAGE
                and target_kind is EventFlowNodeKind.PIPELINE_STAGE
            ):
                raise EventFlowDiagramError(
                    "pipeline-stage edges require two pipeline-stage nodes"
                )
            if edge.kind is EventFlowEdgeKind.PASS_OVERVIEW and not (
                (
                    source_kind is EventFlowNodeKind.CATEGORY_OVERVIEW
                    and target_kind is EventFlowNodeKind.PASS_OVERVIEW
                )
                or (
                    source_kind is EventFlowNodeKind.PASS_OVERVIEW
                    and target_kind is EventFlowNodeKind.EVENT
                )
            ):
                raise EventFlowDiagramError(
                    "pass-overview edges require category-to-pass or pass-to-EID membership"
                )
            if edge.kind is EventFlowEdgeKind.GBUFFER_ATTACHMENT and not (
                source_kind is EventFlowNodeKind.EVENT
                and target_kind is EventFlowNodeKind.GBUFFER_ATTACHMENT
            ):
                raise EventFlowDiagramError(
                    "GBuffer attachment edges require EID-to-attachment binding"
                )
            if edge.kind is EventFlowEdgeKind.DEPTH_ATTACHMENT and not (
                source_kind is EventFlowNodeKind.EVENT
                and target_kind is EventFlowNodeKind.DEPTH_ATTACHMENT
            ):
                raise EventFlowDiagramError(
                    "depth attachment edges require EID-to-DSV binding"
                )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schemaVersion": self.schema_version,
            "theme": self.theme.value,
            "layout": self.layout.value,
            "captureSessionId": self.capture_session_id,
            "captureId": self.capture_id,
            "snapshotId": self.snapshot_id,
            "nodes": [node.canonical_dict() for node in self.nodes],
            "edges": [edge.canonical_dict() for edge in self.edges],
            "truncation": self.truncation.canonical_dict(),
        }


@dataclass(frozen=True, slots=True)
class EventFlowDiagramMetadata:
    graph_ir_sha256: str
    svg_sha256: str
    width: int
    height: int
    node_count: int
    edge_count: int
    capture_session_id: str
    capture_id: str | None
    snapshot_id: str | None
    truncation: EventFlowTruncation
    schema: str = DIAGRAM_METADATA_SCHEMA
    schema_version: int = 1
    mime_type: str = "image/svg+xml"
    theme: str = THEME_EVIDENCE_NEUTRAL_V1
    layout: str = "LR"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schemaVersion": self.schema_version,
            "mimeType": self.mime_type,
            "theme": self.theme,
            "layout": self.layout,
            "graphIrSha256": self.graph_ir_sha256,
            "svgSha256": self.svg_sha256,
            "width": self.width,
            "height": self.height,
            "nodeCount": self.node_count,
            "edgeCount": self.edge_count,
            "captureSessionId": self.capture_session_id,
            "captureId": self.capture_id,
            "snapshotId": self.snapshot_id,
            "truncation": self.truncation.canonical_dict(),
        }


@dataclass(frozen=True, slots=True)
class EventFlowDiagramResult:
    svg_bytes: bytes
    svg_sha256: str
    metadata: EventFlowDiagramMetadata


@dataclass(frozen=True, slots=True)
class _PlacedNode:
    node: EventFlowNode
    x: int
    y: int
    width: int
    height: int


_NODE_WIDTH = 208
_NODE_HEIGHT = 92
_X_GAP = 84
_Y_GAP = 56
_LEFT = 44
_TOP = 96
_RIGHT = 44
_LEGEND_HEIGHT = 220

_NODE_COLORS = {
    EventFlowNodeKind.CATEGORY_OVERVIEW: ("#eef2f6", "#40566d"),
    EventFlowNodeKind.EVENT: ("#e8eef5", "#2f3b48"),
    EventFlowNodeKind.RESOURCE_FLOW: ("#edf3ec", "#395a3b"),
    EventFlowNodeKind.PIPELINE_STAGE: ("#f2eef8", "#5b4775"),
    EventFlowNodeKind.PASS_OVERVIEW: ("#f4f1e8", "#665a37"),
    EventFlowNodeKind.GBUFFER_ATTACHMENT: ("#f8ecec", "#7a3e3e"),
    EventFlowNodeKind.DEPTH_ATTACHMENT: ("#edf2f8", "#345676"),
}

_EDGE_STYLE = {
    EventFlowEdgeKind.RESOURCE_FLOW: ("#34495e", "", 2),
    EventFlowEdgeKind.PIPELINE_STAGE: ("#5b4775", "9 5", 2),
    EventFlowEdgeKind.PASS_OVERVIEW: ("#665a37", "2 5", 2),
    EventFlowEdgeKind.GBUFFER_ATTACHMENT: ("#7a3e3e", "12 4 2 4", 3),
    EventFlowEdgeKind.DEPTH_ATTACHMENT: ("#345676", "5 3", 3),
}


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _node_lines(node: EventFlowNode) -> tuple[str, ...]:
    if node.kind is EventFlowNodeKind.CATEGORY_OVERVIEW:
        return ("[Category]", node.label)
    if node.kind is EventFlowNodeKind.EVENT:
        return ("EID %d" % node.event_id, node.label)
    if node.kind is EventFlowNodeKind.RESOURCE_FLOW:
        return ("[Resource] %s" % node.resource_key, node.label)
    if node.kind is EventFlowNodeKind.PIPELINE_STAGE:
        return ("[Stage] %s" % node.stage, node.label)
    if node.kind is EventFlowNodeKind.PASS_OVERVIEW:
        return ("[Pass]", node.label)
    if node.kind is EventFlowNodeKind.GBUFFER_ATTACHMENT:
        return (
            "[GBuffer candidate] %s" % node.attachment,
            "%s · %s" % (node.resource_key, node.label),
        )
    return (
        "[Depth] %s" % node.attachment,
        "%s · %s" % (node.resource_key, node.label),
    )


def _edge_lines(edge: EventFlowEdge) -> tuple[str, str]:
    if edge.kind is EventFlowEdgeKind.RESOURCE_FLOW:
        main = "[Resource] %s · %s" % (
            edge.resource_key,
            edge.stage_access_token,
        )
    elif edge.kind is EventFlowEdgeKind.PIPELINE_STAGE:
        main = "[Pipeline] %s" % (edge.label or "stage transition")
    elif edge.kind is EventFlowEdgeKind.PASS_OVERVIEW:
        main = "[Pass] %s" % (edge.label or "contains EID")
    elif edge.kind is EventFlowEdgeKind.GBUFFER_ATTACHMENT:
        main = "[RT attachment candidate] %s%s" % (
            edge.resource_key,
            " · " + edge.label if edge.label else "",
        )
    else:
        main = "[Depth] %s%s" % (
            edge.resource_key,
            " · " + edge.label if edge.label else "",
        )
    if edge.label and edge.kind is EventFlowEdgeKind.RESOURCE_FLOW:
        main += " · " + edge.label
    return main, "evidence=" + edge.evidence_ref


def _node_shape(node: _PlacedNode) -> list[str]:
    fill, stroke = _NODE_COLORS[node.node.kind]
    x, y, width, height = node.x, node.y, node.width, node.height
    attributes = 'fill="%s" stroke="%s" stroke-width="2"' % (fill, stroke)
    if node.node.kind is EventFlowNodeKind.CATEGORY_OVERVIEW:
        return [
            '<rect x="%d" y="%d" width="%d" height="%d" rx="5" ry="5" %s />'
            % (x, y + 10, width, height - 10, attributes),
            '<rect x="%d" y="%d" width="%d" height="18" rx="4" ry="4" %s />'
            % (x + 12, y, width // 3, attributes),
        ]
    if node.node.kind is EventFlowNodeKind.EVENT:
        return [
            '<rect x="%d" y="%d" width="%d" height="%d" rx="13" ry="13" %s />'
            % (x, y, width, height, attributes)
        ]
    if node.node.kind is EventFlowNodeKind.RESOURCE_FLOW:
        return [
            '<ellipse cx="%d" cy="%d" rx="%d" ry="%d" %s />'
            % (x + width // 2, y + height // 2, width // 2, height // 2, attributes)
        ]
    if node.node.kind is EventFlowNodeKind.PIPELINE_STAGE:
        inset = 18
        points = (
            "%d,%d %d,%d %d,%d %d,%d %d,%d %d,%d"
            % (
                x + inset,
                y,
                x + width - inset,
                y,
                x + width,
                y + height // 2,
                x + width - inset,
                y + height,
                x + inset,
                y + height,
                x,
                y + height // 2,
            )
        )
        return ['<polygon points="%s" %s />' % (points, attributes)]
    if node.node.kind is EventFlowNodeKind.PASS_OVERVIEW:
        return [
            '<rect x="%d" y="%d" width="%d" height="%d" %s />'
            % (x, y, width, height, attributes),
            '<rect x="%d" y="%d" width="%d" height="%d" fill="none" '
            'stroke="%s" stroke-width="1" />'
            % (x + 6, y + 6, width - 12, height - 12, stroke),
        ]
    if node.node.kind is EventFlowNodeKind.GBUFFER_ATTACHMENT:
        points = "%d,%d %d,%d %d,%d %d,%d" % (
            x + width // 2,
            y,
            x + width,
            y + height // 2,
            x + width // 2,
            y + height,
            x,
            y + height // 2,
        )
    else:
        bevel = 18
        points = "%d,%d %d,%d %d,%d %d,%d %d,%d %d,%d %d,%d %d,%d" % (
            x + bevel,
            y,
            x + width - bevel,
            y,
            x + width,
            y + bevel,
            x + width,
            y + height - bevel,
            x + width - bevel,
            y + height,
            x + bevel,
            y + height,
            x,
            y + height - bevel,
            x,
            y + bevel,
        )
    return ['<polygon points="%s" %s />' % (points, attributes)]


def _text_element(x: int, y: int, text: str, css_class: str = "") -> str:
    class_attribute = ' class="%s"' % css_class if css_class else ""
    return '<text x="%d" y="%d"%s>%s</text>' % (
        x,
        y,
        class_attribute,
        _xml_text(text),
    )


def _render_edge(
    edge: EventFlowEdge,
    placed: dict[str, _PlacedNode],
    *,
    parallel_index: int = 0,
    parallel_count: int = 1,
) -> list[str]:
    source = placed[edge.source_id]
    target = placed[edge.target_id]
    x1 = source.x + source.width
    y1 = source.y + source.height // 2
    x2 = target.x
    y2 = target.y + target.height // 2
    middle = (x1 + x2) // 2
    color, dash, stroke_width = _EDGE_STYLE[edge.kind]
    dash_attribute = ' stroke-dasharray="%s"' % dash if dash else ""
    # Parallel physical resources between one EID pair must remain visibly
    # distinct instead of painting the same curve and label repeatedly.
    offset = (2 * parallel_index - (parallel_count - 1)) * 24
    path = (
        '<path d="M %d %d C %d %d, %d %d, %d %d" fill="none" '
        'stroke="%s" stroke-width="%d"%s />'
        % (
            x1,
            y1,
            middle,
            y1 + offset,
            middle,
            y2 + offset,
            x2,
            y2,
            color,
            stroke_width,
            dash_attribute,
        )
    )
    arrow = (
        '<polygon points="%d,%d %d,%d %d,%d" fill="%s" '
        'stroke="%s" stroke-width="1" />'
        % (x2, y2, x2 - 11, y2 - 6, x2 - 11, y2 + 6, color, color)
    )
    main, evidence = _edge_lines(edge)
    label_x = middle
    label_y = (y1 + y2) // 2 - 11 + offset
    label_width = min(390, max(150, 7 * max(len(main), len(evidence)) + 14))
    background = (
        '<rect x="%d" y="%d" width="%d" height="37" rx="4" ry="4" '
        'fill="#ffffff" stroke="#b4bcc5" stroke-width="1" />'
        % (label_x - label_width // 2, label_y - 16, label_width)
    )
    return [
        '<g id="edge-%s-%s-%s-%s">'
        % (edge.source_id, edge.target_id, edge.kind.value, edge.evidence_ref),
        '<title>%s</title>'
        % _xml_text("%s; %s" % (main, evidence)),
        path,
        arrow,
        background,
        _text_element(label_x, label_y, main, "edge-label"),
        _text_element(label_x, label_y + 14, evidence, "edge-evidence"),
        "</g>",
    ]


def _render_node(node: _PlacedNode) -> list[str]:
    lines = [
        '<g id="node-%s">' % node.node.node_id,
        '<title>%s</title>'
        % _xml_text(" · ".join(_node_lines(node.node))),
    ]
    lines.extend(_node_shape(node))
    labels = _node_lines(node.node)
    center_x = node.x + node.width // 2
    first_y = node.y + node.height // 2 - 5
    lines.append(_text_element(center_x, first_y, labels[0], "node-identity"))
    lines.append(_text_element(center_x, first_y + 22, labels[1], "node-label"))
    lines.append("</g>")
    return lines


def _render_legend(width: int, y: int) -> list[str]:
    lines = [
        '<g id="legend">',
        '<rect x="24" y="%d" width="%d" height="190" rx="8" ry="8" '
        'fill="#f7f8fa" stroke="#8b949e" stroke-width="1" />'
        % (y, width - 48),
        _text_element(42, y + 27, "Evidence-neutral legend", "legend-heading"),
    ]
    node_labels = (
        (EventFlowNodeKind.CATEGORY_OVERVIEW, "folder: category overview"),
        (EventFlowNodeKind.EVENT, "rounded: exact EID"),
        (EventFlowNodeKind.RESOURCE_FLOW, "ellipse: resource"),
        (EventFlowNodeKind.PIPELINE_STAGE, "hexagon: pipeline stage"),
        (EventFlowNodeKind.PASS_OVERVIEW, "double box: pass overview"),
        (EventFlowNodeKind.GBUFFER_ATTACHMENT, "diamond: RT attachment candidate"),
        (EventFlowNodeKind.DEPTH_ATTACHMENT, "octagon: physical DSV attachment"),
    )
    for index, (kind, label) in enumerate(node_labels):
        x = 42 + index * 190
        fill, stroke = _NODE_COLORS[kind]
        lines.append(
            '<rect x="%d" y="%d" width="22" height="18" fill="%s" '
            'stroke="%s" stroke-width="2" />'
            % (x, y + 43, fill, stroke)
        )
        lines.append(_text_element(x + 29, y + 57, label, "legend-label"))
    edge_labels = (
        (EventFlowEdgeKind.RESOURCE_FLOW, "solid: proven resource flow"),
        (EventFlowEdgeKind.PIPELINE_STAGE, "long dash: pipeline transition"),
        (EventFlowEdgeKind.PASS_OVERVIEW, "dot: pass membership"),
        (
            EventFlowEdgeKind.GBUFFER_ATTACHMENT,
            "dash-dot: physical attachment binding",
        ),
        (EventFlowEdgeKind.DEPTH_ATTACHMENT, "short dash: physical DSV binding"),
    )
    for index, (kind, label) in enumerate(edge_labels):
        x = 42 + index * 235
        line_y = y + 101
        color, dash, stroke_width = _EDGE_STYLE[kind]
        dash_attribute = ' stroke-dasharray="%s"' % dash if dash else ""
        lines.append(
            '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" '
            'stroke-width="%d"%s />'
            % (x, line_y, x + 34, line_y, color, stroke_width, dash_attribute)
        )
        lines.append(_text_element(x + 42, line_y + 4, label, "legend-label"))
    lines.append(
        _text_element(
            42,
            y + 151,
            "Every edge carries an explicit type label and evidence reference; color is supplementary.",
            "legend-note",
        )
    )
    lines.append("</g>")
    return lines


def render_event_flow_diagram(graph: EventFlowGraphIR) -> EventFlowDiagramResult:
    """Render one strict Host graph IR to deterministic in-memory SVG bytes."""

    if not isinstance(graph, EventFlowGraphIR):
        raise EventFlowDiagramError(
            "render_event_flow_diagram accepts only validated EventFlowGraphIR"
        )
    graph_bytes = _canonical_bytes(graph.canonical_dict())
    graph_digest = sha256(graph_bytes).hexdigest()

    placed_values = tuple(
        _PlacedNode(
            node=node,
            x=_LEFT + index * (_NODE_WIDTH + _X_GAP),
            y=_TOP + node.lane * (_NODE_HEIGHT + _Y_GAP),
            width=_NODE_WIDTH,
            height=_NODE_HEIGHT,
        )
        for index, node in enumerate(graph.nodes)
    )
    placed = {value.node.node_id: value for value in placed_values}
    max_lane = max(node.lane for node in graph.nodes)
    width = max(
        1460,
        _LEFT
        + len(graph.nodes) * _NODE_WIDTH
        + max(0, len(graph.nodes) - 1) * _X_GAP
        + _RIGHT,
    )
    graph_bottom = _TOP + (max_lane + 1) * _NODE_HEIGHT + max_lane * _Y_GAP
    legend_y = graph_bottom + 56
    footer_height = 54 if graph.truncation.source_truncated else 30
    height = legend_y + _LEGEND_HEIGHT + footer_height

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" role="img" aria-labelledby="title description">'
        % (width, height, width, height),
        '<title id="title">Bread Event Flow Diagram</title>',
        '<desc id="description">Left-to-right Host-validated Event flow. Shapes, labels, '
        'line patterns, and a legend carry meaning independently of color.</desc>',
        '<metadata>schema=%s;theme=%s;layout=LR;graphIrSha256=%s</metadata>'
        % (DIAGRAM_METADATA_SCHEMA, THEME_EVIDENCE_NEUTRAL_V1, graph_digest),
        '<rect x="0" y="0" width="%d" height="%d" fill="#ffffff" />'
        % (width, height),
        '<text x="24" y="36" class="diagram-title">Bread Event Flow</text>',
        '<text x="24" y="61" class="diagram-subtitle">theme=evidence-neutral-v1 · '
        'layout=LR · nodes=%d · edges=%d</text>'
        % (len(graph.nodes), len(graph.edges)),
    ]

    # Fixed attributes are repeated rather than supplied through caller CSS.
    # The only style block is absent by construction; classes are inert labels
    # used for testability/accessibility, while presentation is explicit below.
    parallel_counts: dict[tuple[str, str], int] = {}
    for edge in graph.edges:
        pair = (edge.source_id, edge.target_id)
        parallel_counts[pair] = parallel_counts.get(pair, 0) + 1
    parallel_indices: dict[tuple[str, str], int] = {}
    for edge in graph.edges:
        pair = (edge.source_id, edge.target_id)
        parallel_index = parallel_indices.get(pair, 0)
        parallel_indices[pair] = parallel_index + 1
        lines.append(
            '<g font-family="sans-serif" fill="#20262e">\n%s\n</g>'
            % "\n".join(
                _render_edge(
                    edge,
                    placed,
                    parallel_index=parallel_index,
                    parallel_count=parallel_counts[pair],
                )
            )
        )
    for node in placed_values:
        node_lines = _render_node(node)
        # Apply deterministic typography on the trusted group, never from IR.
        lines.append(
            '<g font-family="sans-serif" fill="#20262e" text-anchor="middle" '
            'font-size="13">\n%s\n</g>' % "\n".join(node_lines)
        )
    lines.extend(_render_legend(width, legend_y))

    footer_y = legend_y + 213
    if graph.truncation.source_truncated:
        reason_text = ", ".join(graph.truncation.reasons) or "unspecified"
        warning = (
            "TRUNCATED · omitted nodes >= %d · omitted edges >= %d · reasons: %s"
            % (
                graph.truncation.omitted_node_count_lower_bound,
                graph.truncation.omitted_edge_count_lower_bound,
                reason_text,
            )
        )
        lines.append(
            '<rect x="24" y="%d" width="%d" height="34" rx="5" ry="5" '
            'fill="#fff4d6" stroke="#7a5d00" stroke-width="2" />'
            % (footer_y - 23, width - 48)
        )
        lines.append(_text_element(42, footer_y, warning, "truncation-warning"))
    else:
        lines.append(
            _text_element(
                24,
                footer_y,
                "COMPLETE · omitted nodes = 0 · omitted edges = 0",
                "truncation-complete",
            )
        )
    lines.append("</svg>")

    # Add fixed typography attributes mechanically without exposing a CSS or
    # style injection surface.  These replacements target renderer-owned tags.
    svg_text = "\n".join(lines) + "\n"
    replacements = {
        'class="diagram-title"': (
            'font-family="sans-serif" font-size="20" font-weight="700" '
            'fill="#20262e"'
        ),
        'class="diagram-subtitle"': (
            'font-family="sans-serif" font-size="12" fill="#4f5965"'
        ),
        'class="edge-label"': (
            'text-anchor="middle" font-family="sans-serif" font-size="11" '
            'font-weight="600" fill="#20262e"'
        ),
        'class="edge-evidence"': (
            'text-anchor="middle" font-family="monospace" font-size="9" '
            'fill="#4f5965"'
        ),
        'class="node-identity"': 'font-size="14" font-weight="700"',
        'class="node-label"': 'font-size="12"',
        'class="legend-heading"': (
            'font-family="sans-serif" font-size="14" font-weight="700" '
            'fill="#20262e"'
        ),
        'class="legend-label"': (
            'font-family="sans-serif" font-size="10" fill="#20262e"'
        ),
        'class="legend-note"': (
            'font-family="sans-serif" font-size="11" fill="#4f5965"'
        ),
        'class="truncation-warning"': (
            'font-family="sans-serif" font-size="12" font-weight="700" '
            'fill="#5c4600"'
        ),
        'class="truncation-complete"': (
            'font-family="sans-serif" font-size="11" fill="#4f5965"'
        ),
    }
    for marker, attributes in replacements.items():
        svg_text = svg_text.replace(marker, attributes)
    svg_bytes = svg_text.encode("utf-8")
    if len(svg_bytes) > MAX_SVG_BYTES:
        raise EventFlowDiagramError("rendered SVG exceeds the 512 KiB bound")
    svg_digest = sha256(svg_bytes).hexdigest()
    metadata = EventFlowDiagramMetadata(
        graph_ir_sha256=graph_digest,
        svg_sha256=svg_digest,
        width=width,
        height=height,
        node_count=len(graph.nodes),
        edge_count=len(graph.edges),
        capture_session_id=graph.capture_session_id,
        capture_id=graph.capture_id,
        snapshot_id=graph.snapshot_id,
        truncation=graph.truncation,
    )
    return EventFlowDiagramResult(
        svg_bytes=svg_bytes,
        svg_sha256=svg_digest,
        metadata=metadata,
    )


__all__ = [
    "DIAGRAM_METADATA_SCHEMA",
    "GRAPH_IR_SCHEMA",
    "MAX_EDGES",
    "MAX_NODES",
    "THEME_EVIDENCE_NEUTRAL_V1",
    "EventFlowDiagramError",
    "EventFlowDiagramMetadata",
    "EventFlowDiagramResult",
    "EventFlowEdge",
    "EventFlowEdgeKind",
    "EventFlowGraphIR",
    "EventFlowLayout",
    "EventFlowNode",
    "EventFlowNodeKind",
    "EventFlowTheme",
    "EventFlowTruncation",
    "render_event_flow_diagram",
]
