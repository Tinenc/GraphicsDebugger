"""Interactive, read-only call-flow view for ReplayLab Shader workspaces.

The view consumes a bounded data model and emits navigation metadata only.  It
never reads a source path, writes Shader text, compiles, or changes replay
state.  The owning workspace must resolve every navigation request against its
already-authorized document catalog before moving an editor cursor.

This module remains Python 3.6/PySide2 compatible.  Its ``project_*.py`` name
also makes every direct Qt translation call visible to ReplayLab's catalog
extractor.
"""

from __future__ import print_function

import hashlib
import math
import re
import copy


try:
    from PySide2 import QtCore, QtGui, QtWidgets
    HAS_QT = True
except ImportError:  # pragma: no cover - model helpers remain unit-testable
    HAS_QT = False
    QtGui = None
    QtWidgets = None

    class _TranslationFallback(object):
        @staticmethod
        def translate(unused_context, source):
            del unused_context
            return str(source)

    class _QtCoreFallback(object):
        QCoreApplication = _TranslationFallback

    QtCore = _QtCoreFallback()


FLOW_GRAPH_SCHEMA = "replaylab-shader-flow-graph/v1"
FLOW_GRAPH_VERSION = 1
MAX_GRAPH_NODES = 256
MAX_GRAPH_EDGES = 1024
MAX_NODE_ID_LENGTH = 128
MAX_LABEL_LENGTH = 160
MAX_DESCRIPTION_LENGTH = 640
MAX_LOCATION_TEXT_LENGTH = 1024
MAX_GRAPH_KEY_LENGTH = 512
MAX_SOURCE_POSITION = 10000000

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _qt_format(translated, *arguments):
    values = tuple(str(value) for value in arguments)

    def _replace(match):
        index = int(match.group(1))
        return values[index - 1] if 0 < index <= len(values) else match.group(0)

    return re.sub(r"%([1-9][0-9]*)", _replace, str(translated))


def _bounded_text(value, limit, one_line=False):
    if value is None:
        return ""
    text = _CONTROL_RE.sub("", str(value)).strip()
    if one_line:
        text = " ".join(text.split())
    if len(text) > int(limit):
        text = text[:max(0, int(limit) - 1)].rstrip() + "\u2026"
    return text


def _bounded_position(value, fallback):
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return int(fallback)
    return max(1, min(MAX_SOURCE_POSITION, parsed))


def _normalized_io(value):
    """Bounded {reads, writes, resources, locals} lists of short strings."""
    result = {"reads": [], "writes": [], "resources": [], "locals": []}
    if not isinstance(value, dict):
        return result
    for key in result:
        items = value.get(key)
        if isinstance(items, (list, tuple)):
            for item in items[:24]:
                text = _bounded_text(item, 96, True)
                if text and text not in result[key]:
                    result[key].append(text)
    return result


# Background tints per node kind, mixed into the palette base so they read
# correctly on both light and dark themes.
_NODE_TINTS = {
    "input": (86, 156, 214),      # blue: where data enters
    "output": (226, 160, 72),     # amber: where results leave
    "function-call": (72, 190, 150),  # green: a stage / helper call
    "condition": (170, 130, 220),  # violet: branching
    "loop": (170, 130, 220),
}


def _tinted(base, kind, strength=0.16):
    tint = _NODE_TINTS.get(kind)
    if not tint:
        return QtGui.QColor(base)
    r, g, b = tint
    return QtGui.QColor(
        int(base.red() + (r - base.red()) * strength),
        int(base.green() + (g - base.green()) * strength),
        int(base.blue() + (b - base.blue()) * strength))


def _localized_known_description(value):
    """Localize fixed Host presets while preserving AI-authored descriptions."""

    text = str(value or "")
    if text == "Not yet described.":
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "Unknown")
    if text == "Shader entry point that coordinates the final stage result.":
        return QtCore.QCoreApplication.translate(
            "ReplayLabProjects",
            "Shader entry point that coordinates the final stage result.")
    if text == "Samples or loads texture data for downstream calculations.":
        return QtCore.QCoreApplication.translate(
            "ReplayLabProjects",
            "Samples or loads texture data for downstream calculations.")
    if text == "Evaluates a lighting or material-response contribution.":
        return QtCore.QCoreApplication.translate(
            "ReplayLabProjects",
            "Evaluates a lighting or material-response contribution.")
    if text == "Prepares normal or tangent-space data.":
        return QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Prepares normal or tangent-space data.")
    if text == "Transforms position or coordinate data.":
        return QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Transforms position or coordinate data.")
    if text == "Adjusts or combines color data.":
        return QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Adjusts or combines color data.")
    if text == "Computes a shadowing or visibility contribution.":
        return QtCore.QCoreApplication.translate(
            "ReplayLabProjects",
            "Computes a shadowing or visibility contribution.")
    if text == "Decodes packed Shader data into a usable form.":
        return QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Decodes packed Shader data into a usable form.")
    if text == "Packs calculated Shader data for later use.":
        return QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Packs calculated Shader data for later use.")
    if text == "Combines intermediate Shader values.":
        return QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Combines intermediate Shader values.")
    if text == "Helper function used by the organized Shader program.":
        return QtCore.QCoreApplication.translate(
            "ReplayLabProjects",
            "Helper function used by the organized Shader program.")
    return text


def _navigation_from_node(node):
    source = node.get("source")
    if not isinstance(source, dict):
        source = node.get("location")
    if not isinstance(source, dict):
        source = {}

    navigation = {}
    text_fields = (
        "workspaceId", "documentId", "fileId", "path", "relativePath",
        "file", "sourceRevision", "sourceSha256", "anchorId", "symbol",
        "functionSignature", "statementId", "snippet",
    )
    for field in text_fields:
        value = source.get(field, node.get(field))
        if field == "functionSignature" and value in (None, ""):
            value = node.get("signature")
        clean = _bounded_text(value, MAX_LOCATION_TEXT_LENGTH, field != "snippet")
        if clean:
            navigation[field] = clean

    line_value = source.get("line", node.get("line"))
    column_value = source.get("column", node.get("column"))
    navigation["line"] = _bounded_position(line_value, 1)
    navigation["column"] = _bounded_position(column_value, 1)
    return navigation


def _edge_endpoint(edge, primary, alternatives):
    value = edge.get(primary)
    if isinstance(value, dict):
        value = value.get("id")
    if value in (None, ""):
        for field in alternatives:
            value = edge.get(field)
            if isinstance(value, dict):
                value = value.get("id")
            if value not in (None, ""):
                break
    return _bounded_text(value, MAX_NODE_ID_LENGTH, True)


def _anonymous_graph_key(nodes, edges):
    digest = hashlib.sha256()
    for node in nodes:
        digest.update(node["id"].encode("utf-8", "replace"))
        digest.update(b"\0")
    for edge in edges:
        digest.update(edge["source"].encode("utf-8", "replace"))
        digest.update(b">")
        digest.update(edge["target"].encode("utf-8", "replace"))
        digest.update(b"\0")
    return "anonymous:" + digest.hexdigest()


def normalize_flow_graph(value, graph_key=None, source_sha256=None, stale=False):
    """Return a small, inert graph model suitable for the Qt scene.

    Unknown keys are intentionally discarded.  In particular, command-like
    payloads cannot flow through a node click.  Paths remain opaque lookup hints
    and are never opened by this component.
    """

    outer = value if isinstance(value, dict) else {}
    raw = outer.get("flowGraph")
    if not isinstance(raw, dict):
        raw = outer

    raw_nodes = raw.get("nodes", [])
    raw_edges = raw.get("edges", [])
    if not isinstance(raw_nodes, list):
        raw_nodes = []
    if not isinstance(raw_edges, list):
        raw_edges = []

    nodes = []
    node_ids = set()
    considered_nodes = raw_nodes[:MAX_GRAPH_NODES]
    for index, candidate in enumerate(considered_nodes):
        if not isinstance(candidate, dict):
            continue
        kind = _bounded_text(candidate.get("kind"), 64, True).lower()
        if not kind:
            kind = "entry-point" if candidate.get("name") == raw.get("entryPoint") else "function"
        execution_node = (raw.get("graphKind") == "function-execution" and
                          kind in ("flow-control", "function-group"))
        if not execution_node and kind not in ("entry-point", "function", "function-call", "function-summary", "input", "output"):
            # Statement/control/computation boxes are internal evidence. They
            # never enter the Qt scene, including from legacy algorithm graphs.
            continue
        node_id = _bounded_text(
            candidate.get("id", candidate.get("nodeId")),
            MAX_NODE_ID_LENGTH, True)
        if not node_id:
            node_id = "module-%d" % (index + 1)
        if node_id in node_ids:
            continue
        node_ids.add(node_id)
        label = _bounded_text(
            candidate.get("label", candidate.get("name")),
            MAX_LABEL_LENGTH, True)
        if not label:
            label = QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Unnamed module")
        description = _localized_known_description(_bounded_text(
            candidate.get("description", candidate.get("summary")),
            MAX_DESCRIPTION_LENGTH, False))
        # i18n-ignore: exact legacy Host-generated prose compatibility value.
        legacy_entry_description = "Shader entry point; the stage result is produced here."
        if description != legacy_entry_description and (
                any(marker in description for marker in (";", "{", "}", "```")) or
                re.search(r"(?<![=!<>])=(?!=)|\b(?:return|discard)\s+", description)):
            description = ""
        if not description and kind in (
                "entry-point", "function", "function-call", "function-summary"):
            description = QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Unknown")
        raw_io = candidate.get("io")
        interface_known = (isinstance(raw_io, dict) or
                           isinstance(candidate.get("inputs"), list) and
                           isinstance(candidate.get("outputs"), list))
        if not isinstance(raw_io, dict):
            raw_io = {"reads": candidate.get("inputs", []),
                      "writes": candidate.get("outputs", []),
                      "resources": [], "locals": []}
        node = {
            "id": node_id,
            "label": label,
            "symbol": _bounded_text(candidate.get("name", label), MAX_LABEL_LENGTH, True),
            "description": description,
            "kind": kind,
            "navigation": _navigation_from_node(candidate),
            "functionId": _bounded_text(candidate.get("functionId"), MAX_NODE_ID_LENGTH, True),
            "helperGroupId": _bounded_text(candidate.get("helperGroupId"), MAX_NODE_ID_LENGTH, True),
            "signature": _bounded_text(candidate.get("signature"),
                                       MAX_LOCATION_TEXT_LENGTH, True),
            "interfaceKnown": interface_known,
            "io": _normalized_io(raw_io),
        }
        node["navigation"].pop("snippet", None)
        # Boundary nodes are generated with English labels by the pure-Python
        # hierarchy; localise them here where Qt translation is available.
        if node["kind"] == "input":
            node["label"] = QtCore.QCoreApplication.translate("ReplayLabProjects", "Input") + (
                " \u00b7 " + node["symbol"] if node.get("symbol") else "")
        elif node["kind"] == "output":
            node["label"] = QtCore.QCoreApplication.translate("ReplayLabProjects", "Output")
        elif node["kind"] == "flow-control":
            control = candidate.get("controlKind")
            if control in ("for", "while", "do"):
                node["label"] = QtCore.QCoreApplication.translate("ReplayLabProjects", "Repeat these steps")
            elif control == "switch":
                node["label"] = QtCore.QCoreApplication.translate("ReplayLabProjects", "Choose a path")
            elif control in ("return", "discard"):
                node["label"] = QtCore.QCoreApplication.translate("ReplayLabProjects", "Finish this path")
            elif control == "break":
                node["label"] = QtCore.QCoreApplication.translate("ReplayLabProjects", "Exit the loop")
            elif control == "continue":
                node["label"] = QtCore.QCoreApplication.translate("ReplayLabProjects", "Continue with the next iteration")
            else:
                node["label"] = QtCore.QCoreApplication.translate("ReplayLabProjects", "Check a condition")
            if not node["description"]:
                node["description"] = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Follow the source condition; open this step to inspect its code.")
        elif node["kind"] == "function-group" and candidate.get("summaryId") == "implementation-helpers":
            node["label"] = QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Implementation helpers")
            node["description"] = QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Open to inspect the supporting calculations.")
        elif node["kind"] == "function-group" and not node["description"]:
            node["description"] = QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "These calls share an expression; their evaluation order is not expanded here.")
        nodes.append(node)

    edges = []
    edge_ids = set()
    considered_edges = raw_edges[:MAX_GRAPH_EDGES]
    for candidate in considered_edges:
        if not isinstance(candidate, dict):
            continue
        source_id = _edge_endpoint(
            candidate, "source", ("from", "caller", "callerId"))
        target_id = _edge_endpoint(
            candidate, "target", ("to", "callee", "calleeId"))
        if source_id not in node_ids or target_id not in node_ids:
            continue
        kind = _bounded_text(candidate.get("kind"), 64, True).lower()
        edge_key = (source_id, target_id, kind)
        if edge_key in edge_ids:
            continue
        edge_ids.add(edge_key)
        edges.append({"source": source_id, "target": target_id, "kind": kind})

    explicit_key = _bounded_text(graph_key, MAX_GRAPH_KEY_LENGTH, True)
    if not explicit_key:
        explicit_key = _bounded_text(
            raw.get("graphId", outer.get("graphId")),
            MAX_GRAPH_KEY_LENGTH, True)
    binding = raw.get("binding")
    if not isinstance(binding, dict):
        binding = outer.get("binding")
    if not isinstance(binding, dict):
        binding = {}
    graph_source_sha256 = _bounded_text(
        raw.get("sourceSha256", binding.get("sourceSha256")),
        MAX_LOCATION_TEXT_LENGTH, True).lower()
    graph_workspace_id = _bounded_text(
        raw.get("workspaceId", binding.get("workspaceId")),
        MAX_LOCATION_TEXT_LENGTH, True)
    graph_source_revision = _bounded_text(
        raw.get("sourceRevision", binding.get("sourceRevision")),
        MAX_LOCATION_TEXT_LENGTH, True)
    for node in nodes:
        navigation = node["navigation"]
        if graph_workspace_id and not navigation.get("workspaceId"):
            navigation["workspaceId"] = graph_workspace_id
        if graph_source_revision and not navigation.get("sourceRevision"):
            navigation["sourceRevision"] = graph_source_revision
        if graph_source_sha256 and not navigation.get("sourceSha256"):
            navigation["sourceSha256"] = graph_source_sha256
    current_source_sha256 = _bounded_text(
        source_sha256, MAX_LOCATION_TEXT_LENGTH, True).lower()
    if not explicit_key:
        revision = _bounded_text(
            binding.get("sourceRevision",
                        binding.get("sourceSha256",
                                    raw.get("sourceRevision", graph_source_sha256))),
            MAX_LOCATION_TEXT_LENGTH, True)
        if graph_workspace_id:
            explicit_key = graph_workspace_id + (
                ":" + revision if revision else "")
    if not explicit_key:
        explicit_key = _anonymous_graph_key(nodes, edges)

    raw_state = _bounded_text(
        raw.get("state", raw.get("status")), 64, True).lower()
    is_stale = (bool(stale) or bool(raw.get("stale", False)) or raw_state in (
        "stale", "outdated", "out-of-date") or
        bool(current_source_sha256 and graph_source_sha256 and
             current_source_sha256 != graph_source_sha256))
    generated = bool(raw.get("generated", True))
    declared_truncation = raw.get("truncated")
    if not isinstance(declared_truncation, dict):
        declared_truncation = {}
    omitted_nodes = declared_truncation.get("omittedNodes", 0)
    omitted_edges = declared_truncation.get("omittedEdges", 0)
    if isinstance(omitted_nodes, bool) or not isinstance(omitted_nodes, int):
        omitted_nodes = 0
    if isinstance(omitted_edges, bool) or not isinstance(omitted_edges, int):
        omitted_edges = 0
    omitted_nodes = max(
        0, omitted_nodes, len(raw_nodes) - len(considered_nodes))
    omitted_edges = max(
        0, omitted_edges, len(raw_edges) - len(considered_edges))
    nodes_truncated = bool(
        declared_truncation.get("nodes", False) or omitted_nodes)
    edges_truncated = bool(
        declared_truncation.get("edges", False) or omitted_edges)

    return {
        "schema": FLOW_GRAPH_SCHEMA,
        "schemaVersion": FLOW_GRAPH_VERSION,
        "graphKey": explicit_key,
        "graphKind": _bounded_text(raw.get("graphKind"), 64, True),
        "functionId": _bounded_text(raw.get("functionId"), MAX_NODE_ID_LENGTH, True),
        "functionName": _bounded_text(raw.get("functionName"), MAX_LABEL_LENGTH, True),
        "helperGroupId": _bounded_text(raw.get("helperGroupId"), MAX_NODE_ID_LENGTH, True),
        "sourceSha256": graph_source_sha256,
        "nodes": nodes,
        "edges": edges,
        "stale": is_stale,
        "generated": generated,
        "sourceDerived": raw.get("sourceDerived") is True,
        "emptyState": (raw.get("emptyState") if raw.get("emptyState") in (
            "building", "invalid-source", "entry-unavailable") else "unavailable"),
        "errorDetail": _bounded_text(raw.get("errorDetail"), 1024, False),
        "nodesTruncated": nodes_truncated,
        "edgesTruncated": edges_truncated,
        "omittedNodeCount": omitted_nodes,
        "omittedEdgeCount": omitted_edges,
    }


def _node_title(node):
    # Old algorithm artifacts can label a function with explanatory prose.
    # Keep the actual function name prominent; control-flow labels remain prose.
    if node.get("kind") in ("entry-point", "function", "function-call", "function-summary"):
        return node.get("symbol") or node.get("label", "")
    return node.get("label", "")


def _node_description(node, algorithm):
    description = node.get("description", "")
    # The algorithm model may use the stage's explanation as both its title
    # and summary. Display it once without removing either field from the
    # source-bound graph or discarding a genuinely different explanation.
    if (algorithm and " ".join(description.split()) ==
            " ".join(_node_title(node).split())):
        return ""
    return description


class GraphNavigationHistory(object):
    """Small graph-only history, independent from the editor cursor history."""

    def __init__(self):
        self._entries = []
        self._cursor = -1

    def reset(self, graph, options=None):
        self._entries = [(copy.deepcopy(graph), dict(options or {}))]
        self._cursor = 0
        return self.current()

    def current(self):
        return self._entries[self._cursor] if self._cursor >= 0 else (None, {})

    def state(self):
        return {"canBack": self._cursor > 0,
                "canForward": 0 <= self._cursor < len(self._entries) - 1,
                "index": self._cursor, "length": len(self._entries)}

    def open(self, graph, options=None, ancestors=None):
        if ancestors is not None:
            # Explicit source ancestry replaces unrelated visited graphs.
            self._entries = [(copy.deepcopy(parent), dict(settings or {}))
                             for parent, settings in ancestors]
            self._cursor = len(self._entries) - 1
        if self._cursor < 0:
            return self.reset(graph, options)
        target = graph or {}
        if target.get("graphId"):
            for index, (existing, unused_options) in enumerate(self._entries[:self._cursor + 1]):
                existing = existing or {}
                same_function = (target.get("functionId") and
                                 existing.get("functionId") == target["functionId"] and
                                 existing.get("helperGroupId") == target.get("helperGroupId"))
                if existing.get("graphId") == target["graphId"] or same_function:
                    # A recursive call returns to that ancestor. It must not
                    # grow a synthetic visitation chain or discard a parent.
                    self._entries = self._entries[:index + 1]
                    self._entries[index] = (copy.deepcopy(graph), dict(options or {}))
                    self._cursor = index
                    return self.current()
        self._entries = self._entries[:self._cursor + 1]
        self._entries.append((copy.deepcopy(graph), dict(options or {})))
        self._cursor = len(self._entries) - 1
        return self.current()

    def back(self):
        if self._cursor > 0:
            self._cursor -= 1
        return self.current()

    def forward(self):
        if self._cursor < len(self._entries) - 1:
            self._cursor += 1
        return self.current()


if HAS_QT:
    _NODE_WIDTH = 280.0
    _NODE_MIN_HEIGHT = 88.0
    # Width of the jump-to-code arrow strip kept free at every node's right edge.
    _NODE_ARROW_STRIP = 26.0
    _LAYER_GAP = 76.0
    _COLUMN_GAP = 48.0


    class _FlowEdgeItem(QtWidgets.QGraphicsPathItem):
        def __init__(self, source, target, kind=""):
            super(_FlowEdgeItem, self).__init__()
            self.source = source
            self.target = target
            self.kind = kind
            self.setZValue(-2.0)
            self.setAcceptedMouseButtons(QtCore.Qt.NoButton)
            color = QtGui.QColor(118, 151, 183, 205)
            if kind in ("conditional", "branch", "true", "false", "loop"):
                color = QtGui.QColor(210, 156, 83, 215)
            self._color = color
            self.setPen(QtGui.QPen(color, 1.6, QtCore.Qt.SolidLine,
                                   QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
            self._arrow = QtWidgets.QGraphicsPolygonItem(self)
            self._arrow.setPen(QtGui.QPen(color, 1.0))
            self._arrow.setBrush(QtGui.QBrush(color))
            self._label = None
            if kind == "true":
                self._label = QtWidgets.QGraphicsSimpleTextItem(
                    QtCore.QCoreApplication.translate("ReplayLabProjects", "True"), self)
            elif kind == "false":
                self._label = QtWidgets.QGraphicsSimpleTextItem(
                    QtCore.QCoreApplication.translate("ReplayLabProjects", "False"), self)
            if self._label is not None:
                self._label.setBrush(QtGui.QBrush(color))
                self._label.setAcceptedMouseButtons(QtCore.Qt.NoButton)
            self.source.add_edge(self)
            self.target.add_edge(self)
            self.adjust()

        def adjust(self):
            source_rect = self.source.sceneBoundingRect()
            target_rect = self.target.sceneBoundingRect()
            source_center = source_rect.center()
            target_center = target_rect.center()
            delta_x = target_center.x() - source_center.x()
            delta_y = target_center.y() - source_center.y()

            if (self.kind == "calls" and
                    target_rect.top() - source_rect.bottom() > _LAYER_GAP + 20.0):
                # A caller can invoke several children. Route long arrows to
                # the side of the single-column stack so they do not cross
                # intervening function boxes.
                side_x = min(source_rect.left(), target_rect.left()) - 64.0
                start = QtCore.QPointF(source_rect.left(), source_center.y())
                end = QtCore.QPointF(target_rect.left(), target_center.y())
                first_control = QtCore.QPointF(side_x, start.y())
                second_control = QtCore.QPointF(side_x, end.y())
            elif abs(delta_y) >= 30.0:
                if delta_y >= 0.0:
                    start = QtCore.QPointF(source_center.x(), source_rect.bottom())
                    end = QtCore.QPointF(target_center.x(), target_rect.top())
                    control = max(42.0, abs(delta_y) * 0.45)
                    if self.source.algorithm and self.target.algorithm:
                        # Compact layers need controls inside the clear gap;
                        # the old 42px minimum folds a 22px connector backwards.
                        control = max(0.0, end.y() - start.y()) * 0.45
                    first_control = start + QtCore.QPointF(0.0, control)
                    second_control = end - QtCore.QPointF(0.0, control)
                else:
                    side_x = max(source_rect.right(), target_rect.right()) + 42.0
                    start = QtCore.QPointF(source_rect.right(), source_center.y())
                    end = QtCore.QPointF(target_rect.right(), target_center.y())
                    first_control = QtCore.QPointF(side_x, start.y())
                    second_control = QtCore.QPointF(side_x, end.y())
            elif delta_x >= 0.0:
                start = QtCore.QPointF(source_rect.right(), source_center.y())
                end = QtCore.QPointF(target_rect.left(), target_center.y())
                control = max(36.0, abs(delta_x) * 0.45)
                first_control = start + QtCore.QPointF(control, 0.0)
                second_control = end - QtCore.QPointF(control, 0.0)
            else:
                start = QtCore.QPointF(source_rect.left(), source_center.y())
                end = QtCore.QPointF(target_rect.right(), target_center.y())
                control = max(36.0, abs(delta_x) * 0.45)
                first_control = start - QtCore.QPointF(control, 0.0)
                second_control = end + QtCore.QPointF(control, 0.0)

            path = QtGui.QPainterPath(start)
            path.cubicTo(first_control, second_control, end)
            self.setPath(path)
            if self._label is not None:
                self._label.setPos(path.pointAtPercent(0.4))

            direction = end - second_control
            length = math.hypot(direction.x(), direction.y())
            if length < 0.001:
                direction = end - start
                length = max(0.001, math.hypot(direction.x(), direction.y()))
            unit = QtCore.QPointF(direction.x() / length, direction.y() / length)
            normal = QtCore.QPointF(-unit.y(), unit.x())
            arrow_length = 10.0
            arrow_width = 4.8
            base = end - unit * arrow_length
            polygon = QtGui.QPolygonF((
                end,
                base + normal * arrow_width,
                base - normal * arrow_width,
            ))
            self._arrow.setPolygon(polygon)


    class _FlowNodeItem(QtWidgets.QGraphicsObject):
        def __init__(self, node, activate_callback, expand_callback, palette,
                     algorithm=False, context_callback=None):
            super(_FlowNodeItem, self).__init__()
            self.node = node
            self._activate_callback = activate_callback
            self._expand_callback = expand_callback
            self._context_callback = context_callback
            self._edges = []
            self._press_position = None
            self._laying_out = False
            self.algorithm = algorithm
            self._width = 420.0 if algorithm else _NODE_WIDTH
            minimum_height = 64.0 if algorithm else _NODE_MIN_HEIGHT

            self.setFlags(
                QtWidgets.QGraphicsItem.ItemIsSelectable |
                QtWidgets.QGraphicsItem.ItemIsFocusable |
                QtWidgets.QGraphicsItem.ItemSendsGeometryChanges)
            # The node body only selects/expands; only the arrow at the right
            # edge jumps to code, so the hand cursor is shown over the arrow alone.
            self.setCursor(QtCore.Qt.ArrowCursor)
            self.setAcceptHoverEvents(True)
            self._arrow_hover = False
            self.setCacheMode(QtWidgets.QGraphicsItem.DeviceCoordinateCache)

            self._text_color = palette.color(QtGui.QPalette.Text)
            self._base_color = palette.color(QtGui.QPalette.Base)
            self._border_color = palette.color(QtGui.QPalette.Mid)
            self._accent_color = palette.color(QtGui.QPalette.Highlight)

            self._title = QtWidgets.QGraphicsTextItem(self)
            self._title.setPlainText(_node_title(node))
            self._title.setDefaultTextColor(self._text_color)
            self._title.setTextWidth(self._width - 30.0 - _NODE_ARROW_STRIP)
            self._title.setAcceptedMouseButtons(QtCore.Qt.NoButton)

            description = _node_description(node, algorithm)
            self._description = QtWidgets.QGraphicsTextItem(self)
            self._description.setPlainText(description)
            description_color = QtGui.QColor(self._text_color)
            description_color.setAlpha(205)
            self._description.setDefaultTextColor(description_color)
            description_font = self._description.font()
            description_font.setPointSizeF(max(9.5, description_font.pointSizeF()))
            self._description.setFont(description_font)
            title_font = self._title.font()
            title_font.setBold(True)
            title_font.setPointSizeF(description_font.pointSizeF() + 1.5)
            self._title.setFont(title_font)
            self._description.setTextWidth(self._width - 30.0 - _NODE_ARROW_STRIP)
            self._description.setVisible(bool(description))
            self._description.setAcceptedMouseButtons(QtCore.Qt.NoButton)
            # Read the purpose first, then locate the corresponding function.
            # Interface metadata remains available for navigation and analysis;
            # it is deliberately not another text block inside the card.
            top = 10.0
            self._description.setPos(18.0, top)
            description_height = (
                self._description.boundingRect().height() if description else 0.0)
            self._title.setPos(18.0, top + description_height + (3.0 if description else 0.0))
            self._height = max(
                minimum_height,
                self._title.pos().y() + self._title.boundingRect().height() + 12.0)
            self._kind = str(node.get("kind", "") or "")
            self._fill_color = _tinted(self._base_color, self._kind)

            navigation = node.get("navigation", {})
            location = (navigation.get("relativePath") or
                        navigation.get("file") or
                        navigation.get("path") or "")
            if location:
                self.setToolTip(_qt_format(
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Go to %1, line %2"),
                    location, navigation.get("line", 1)))

        def boundingRect(self):
            return QtCore.QRectF(0.0, 0.0, self._width, self._height)

        def paint(self, painter, option, unused_widget=None):
            del unused_widget
            painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
            rect = self.boundingRect().adjusted(0.75, 0.75, -0.75, -0.75)
            fill = QtGui.QColor(self._fill_color)
            if option.state & QtWidgets.QStyle.State_Selected:
                fill = fill.lighter(112)
                border = QtGui.QColor(self._accent_color)
                width = 2.2
            else:
                border = QtGui.QColor(self._border_color)
                width = 1.2
            painter.setPen(QtGui.QPen(border, width))
            painter.setBrush(QtGui.QBrush(fill))
            painter.drawRoundedRect(rect, 8.0, 8.0)
            painter.setPen(QtCore.Qt.NoPen)
            stripe = _NODE_TINTS.get(self._kind)
            painter.setBrush(QtGui.QBrush(QtGui.QColor(*stripe) if stripe else self._accent_color))
            painter.drawRoundedRect(
                QtCore.QRectF(1.5, 9.0, 5.0, max(18.0, self._height - 18.0)),
                2.5, 2.5)
            self._paint_jump_arrow(painter)

        def _arrow_rect(self):
            """Hit/paint rectangle of the jump-to-code arrow inside the right edge."""
            size = 20.0
            return QtCore.QRectF(
                self._width - _NODE_ARROW_STRIP + (_NODE_ARROW_STRIP - size) / 2.0 - 2.0,
                (self._height - size) / 2.0, size, size)

        def _paint_jump_arrow(self, painter):
            rect = self._arrow_rect()
            if self._arrow_hover:
                halo = QtGui.QColor(self._accent_color)
                halo.setAlpha(60)
                painter.setPen(QtCore.Qt.NoPen)
                painter.setBrush(QtGui.QBrush(halo))
                painter.drawRoundedRect(rect.adjusted(-3.0, -3.0, 3.0, 3.0), 5.0, 5.0)
            color = QtGui.QColor(self._accent_color if self._arrow_hover else self._text_color)
            if not self._arrow_hover:
                color.setAlpha(170)
            pen = QtGui.QPen(color, 2.0)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            pen.setJoinStyle(QtCore.Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.NoBrush)
            # A right-pointing arrow: shaft plus chevron head.
            cy = rect.center().y()
            x0 = rect.left() + 4.0
            x1 = rect.right() - 4.0
            painter.drawLine(QtCore.QPointF(x0, cy), QtCore.QPointF(x1, cy))
            head = 5.5
            painter.drawLine(QtCore.QPointF(x1, cy), QtCore.QPointF(x1 - head, cy - head))
            painter.drawLine(QtCore.QPointF(x1, cy), QtCore.QPointF(x1 - head, cy + head))

        def hoverMoveEvent(self, event):
            over = self._arrow_rect().contains(event.pos())
            if over != self._arrow_hover:
                self._arrow_hover = over
                self.setCursor(QtCore.Qt.PointingHandCursor if over else QtCore.Qt.ArrowCursor)
                self.update()
            super(_FlowNodeItem, self).hoverMoveEvent(event)

        def hoverLeaveEvent(self, event):
            if self._arrow_hover:
                self._arrow_hover = False
                self.setCursor(QtCore.Qt.ArrowCursor)
                self.update()
            super(_FlowNodeItem, self).hoverLeaveEvent(event)

        def add_edge(self, edge):
            self._edges.append(edge)

        def itemChange(self, change, value):
            if change == QtWidgets.QGraphicsItem.ItemPositionHasChanged:
                for edge in self._edges:
                    edge.adjust()
            return super(_FlowNodeItem, self).itemChange(change, value)

        def set_layout_position(self, point):
            self._laying_out = True
            try:
                self.setPos(point)
            finally:
                self._laying_out = False

        def mousePressEvent(self, event):
            if event.button() == QtCore.Qt.LeftButton:
                self._press_position = event.scenePos()
            super(_FlowNodeItem, self).mousePressEvent(event)

        def mouseReleaseEvent(self, event):
            was_click = False
            if event.button() == QtCore.Qt.LeftButton and self._press_position is not None:
                delta = event.scenePos() - self._press_position
                was_click = math.hypot(delta.x(), delta.y()) <= 5.0
            on_arrow = self._arrow_rect().contains(event.pos())
            super(_FlowNodeItem, self).mouseReleaseEvent(event)
            self._press_position = None
            # Clicking the node body only selects it. Only the arrow at the
            # right edge jumps to code, so a click never yanks the editor
            # around by accident and a double-click (expand) never navigates.
            if was_click and on_arrow:
                self._activate_callback(self.node)

        def mouseDoubleClickEvent(self, event):
            if event.button() == QtCore.Qt.LeftButton:
                self._press_position = None
                if self._arrow_rect().contains(event.pos()):
                    # A rapid second click on the arrow is still "jump", not "expand".
                    self._activate_callback(self.node)
                    event.accept()
                    return
                if self.node.get("functionId"):
                    self._expand_callback(self.node)
                    event.accept()
                    return
            super(_FlowNodeItem, self).mouseDoubleClickEvent(event)

        def keyPressEvent(self, event):
            if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                self._activate_callback(self.node)
                event.accept()
                return
            super(_FlowNodeItem, self).keyPressEvent(event)

        def contextMenuEvent(self, event):
            if not callable(self._context_callback):
                return super(_FlowNodeItem, self).contextMenuEvent(event)
            menu = QtWidgets.QMenu()
            action = menu.addAction(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Add to Bread AI chat"))
            if menu.exec_(event.screenPos()) == action:
                self._context_callback(self.node)
            event.accept()


    class ShaderFlowGraphView(QtWidgets.QGraphicsView):
        """Fixed graph layout with per-graph pan and zoom retention."""

        nodeActivated = QtCore.Signal(object)
        subgraphRequested = QtCore.Signal(object)
        contextRequested = QtCore.Signal(object)
        graphStateChanged = QtCore.Signal(object)

        def __init__(self, parent=None):
            super(ShaderFlowGraphView, self).__init__(parent)
            self._scene = QtWidgets.QGraphicsScene(self)
            self.setScene(self._scene)
            self.setRenderHints(
                QtGui.QPainter.Antialiasing |
                QtGui.QPainter.TextAntialiasing |
                QtGui.QPainter.SmoothPixmapTransform)
            self.setTransformationAnchor(QtWidgets.QGraphicsView.NoAnchor)
            self.setResizeAnchor(QtWidgets.QGraphicsView.AnchorViewCenter)
            self.setViewportUpdateMode(QtWidgets.QGraphicsView.BoundingRectViewportUpdate)
            self.setDragMode(QtWidgets.QGraphicsView.NoDrag)
            self.setFocusPolicy(QtCore.Qt.StrongFocus)
            self.setMinimumWidth(280)
            self.setBackgroundBrush(QtGui.QBrush(
                self.palette().color(QtGui.QPalette.Window)))
            self.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects",
                "Click a node to go to code, double-click a function to explore it, drag empty space to pan, and use the mouse wheel to zoom."))

            self._graph = normalize_flow_graph(None)
            self._graph_key = ""
            self._nodes = {}
            self._edges = []
            self._view_by_graph = {}
            self._auto_fit = True
            self._fit_pending = False
            self._updating_scene_rect = False
            self._pending_subgraph_request = None
            self._subgraph_timer = QtCore.QTimer(self)
            self._subgraph_timer.setSingleShot(True)
            self._subgraph_timer.timeout.connect(self._emit_subgraph_request)
            self._pending_click_node = None
            self._click_timer = QtCore.QTimer(self)
            self._click_timer.setSingleShot(True)
            self._click_timer.timeout.connect(self._emit_pending_click)
            self._panning = False
            self._pan_origin = QtCore.QPoint()
            self._zoom = 1.0

        def graph_info(self):
            return dict(self._graph)

        def graph_key(self):
            return self._graph_key

        def _request_context(self, node):
            self.contextRequested.emit({"node": copy.deepcopy(node),
                                        "graph": copy.deepcopy(self.graph_info()),
                                        "graphKey": self.graph_key()})

        def _remember_positions(self):
            if self._graph_key:
                center = self.mapToScene(self.viewport().rect().center())
                self._view_by_graph[self._graph_key] = (
                    self._auto_fit, self._zoom, center.x(), center.y())

        def _activate_node(self, node):
            # A single click navigates the *editor* to the node's call site and
            # must feel instant, like selecting a desktop icon. It is emitted on
            # the next event-loop tick (0 ms) rather than synchronously so the
            # emit never re-enters the graph item's own mouse handler, but it
            # is not held back to wait for a possible double-click.
            #
            # No disambiguation delay is needed: a double-click's expand only
            # replaces the flow-graph scene (see _expand_node) and never moves
            # the editor, so the editor landing on the call site first is the
            # correct, coherent outcome ("where it is called" in the editor,
            # "what it does" in the graph) rather than a stray jump. The timer
            # lives on the persistent view, never on a node item that expanding
            # would delete.
            self._pending_click_node = node
            self._click_timer.start(0)

        def _emit_pending_click(self):
            node = self._pending_click_node
            self._pending_click_node = None
            if not node:
                return
            navigation = dict(node.get("navigation", {}))
            navigation["nodeId"] = node["id"]
            navigation.setdefault("symbol", node["symbol"])
            self.nodeActivated.emit(navigation)

        def _expand_node(self, node):
            # Drop any not-yet-emitted single-click (the 0 ms tick normally has
            # already fired by the time a second press arrives, which is fine:
            # expanding only swaps the graph scene and leaves the editor at the
            # call site the click selected).
            self._click_timer.stop()
            self._pending_click_node = None
            if not node.get("functionId"):
                return
            navigation = dict(node.get("navigation", {}))
            navigation.update({"nodeId": node["id"], "functionId": node["functionId"]})
            if node.get("helperGroupId"):
                navigation["helperGroupId"] = node["helperGroupId"]
            navigation.setdefault("symbol", node["symbol"])
            # Expanding clears the scene. Defer until the native double-click
            # handler has returned, so it cannot delete its own active item.
            self._pending_subgraph_request = navigation
            self._subgraph_timer.start(0)

        def _emit_subgraph_request(self):
            navigation = self._pending_subgraph_request
            self._pending_subgraph_request = None
            if navigation is not None:
                self.subgraphRequested.emit(navigation)

        @staticmethod
        def _depths(nodes, edges):
            node_ids = [node["id"] for node in nodes]
            incoming = {node_id: 0 for node_id in node_ids}
            outgoing = {node_id: [] for node_id in node_ids}
            for edge in edges:
                if edge.get("kind") == "loop":
                    # Keep the back edge visible, but never let it force a
                    # cyclic algorithm into arbitrary layout layers.
                    continue
                outgoing[edge["source"]].append(edge["target"])
                incoming[edge["target"]] += 1
            queue = [node_id for node_id in node_ids if incoming[node_id] == 0]
            depths = {node_id: 0 for node_id in queue}
            cursor = 0
            while cursor < len(queue):
                source_id = queue[cursor]
                cursor += 1
                for target_id in outgoing[source_id]:
                    depths[target_id] = max(
                        depths.get(target_id, 0), depths[source_id] + 1)
                    incoming[target_id] -= 1
                    if incoming[target_id] == 0:
                        queue.append(target_id)
            unresolved = [node_id for node_id in node_ids if node_id not in depths]
            cycle_depth = max(depths.values()) + 1 if depths else 0
            for index, node_id in enumerate(unresolved):
                depths[node_id] = cycle_depth + (index // 6)
            return depths

        def _automatic_positions(self):
            if self._graph["graphKind"] == "function-call":
                # The call hierarchy is browsed one function at a time. A
                # single column preserves invocation order and keeps the
                # main flow top-to-bottom regardless of fan-out.
                positions = {}
                y_position = 0.0
                for node in self._graph["nodes"]:
                    node_id = node["id"]
                    item = self._nodes[node_id]
                    positions[node_id] = QtCore.QPointF(
                        -item.boundingRect().width() * 0.5, y_position)
                    y_position += item.boundingRect().height() + _LAYER_GAP
                return positions
            depths = self._depths(self._graph["nodes"], self._graph["edges"])
            layers = {}
            for node in self._graph["nodes"]:
                layers.setdefault(depths.get(node["id"], 0), []).append(node["id"])

            positions = {}
            y_position = 0.0
            algorithm = self._graph["graphKind"] == "algorithm-dataflow"
            layer_gap = 22.0 if algorithm else _LAYER_GAP
            for depth in sorted(layers):
                node_ids = layers[depth]
                for row_start in range(0, len(node_ids), 8):
                    row_ids = node_ids[row_start:row_start + 8]
                    total_width = (
                        sum(self._nodes[node_id].boundingRect().width()
                            for node_id in row_ids) +
                        max(0, len(row_ids) - 1) * _COLUMN_GAP)
                    x_position = -total_width * 0.5
                    row_height = 0.0
                    for node_id in row_ids:
                        item = self._nodes[node_id]
                        positions[node_id] = QtCore.QPointF(
                            x_position, y_position)
                        x_position += item.boundingRect().width() + _COLUMN_GAP
                        row_height = max(
                            row_height, item.boundingRect().height())
                    y_position += row_height + (layer_gap if algorithm else 28.0)
                if not algorithm:
                    y_position += _LAYER_GAP - 28.0
            return positions

        def _update_scene_rect(self):
            if self._updating_scene_rect:
                return
            self._updating_scene_rect = True
            try:
                if not self._nodes:
                    bounds = QtCore.QRectF(-220.0, -100.0, 440.0, 200.0)
                else:
                    bounds = self._scene.itemsBoundingRect().adjusted(-90.0, -90.0, 90.0, 90.0)
                if self._scene.sceneRect() != bounds:
                    self._scene.setSceneRect(bounds)
            finally:
                self._updating_scene_rect = False

        def _show_empty_state(self):
            state = self._graph.get("emptyState")
            if state == "building":
                message = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Building call flow from the current source...")
            elif state == "invalid-source":
                message = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "The current Shader source cannot be parsed for call flow.")
            elif state == "entry-unavailable":
                message = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "The Shader entry point is missing or ambiguous in the current source.")
            else:
                message = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "No call flow is available for this Shader.")
            empty = self._scene.addText(message)
            empty.setToolTip(self._graph.get("errorDetail", ""))
            empty.setDefaultTextColor(
                self.palette().color(QtGui.QPalette.Text))
            empty.setTextWidth(340.0)
            rect = empty.boundingRect()
            empty.setPos(-rect.width() * 0.5, -rect.height() * 0.5)
            self._update_scene_rect()

        def set_graph(self, graph, graph_key=None, source_sha256=None, stale=False):
            # Cancel any pending single-click before the scene (and its node
            # items) are replaced, so the timer never fires on a deleted node.
            self._click_timer.stop()
            self._pending_click_node = None
            self._remember_positions()
            normalized = normalize_flow_graph(
                graph, graph_key, source_sha256, stale)
            self._graph = normalized
            self._graph_key = normalized["graphKey"]
            self._scene.clear()
            self._nodes = {}
            self._edges = []
            self.resetTransform()
            self._zoom = 1.0
            self._auto_fit = True

            palette = self.palette()
            # i18n-ignore: Serialized graph-kind discriminator produces a Boolean layout option, never UI text.
            algorithm = normalized["graphKind"] == "algorithm-dataflow"
            for node in normalized["nodes"]:
                item = _FlowNodeItem(
                    node, self._activate_node, self._expand_node, palette,
                    algorithm, self._request_context)
                self._scene.addItem(item)
                self._nodes[node["id"]] = item

            if not self._nodes:
                self._show_empty_state()
                self.graphStateChanged.emit(self.graph_info())
                return self.graph_info()

            positions = self._automatic_positions()
            for node_id, item in self._nodes.items():
                point = positions[node_id]
                item.set_layout_position(point)

            for edge in normalized["edges"]:
                item = _FlowEdgeItem(
                    self._nodes[edge["source"]],
                    self._nodes[edge["target"]], edge.get("kind", ""))
                self._scene.addItem(item)
                self._edges.append(item)
            self._update_scene_rect()
            saved_view = self._view_by_graph.get(self._graph_key)
            if saved_view and not saved_view[0]:
                self._manual_view()
                self._zoom = saved_view[1]
                self.scale(self._zoom, self._zoom)
                self.centerOn(saved_view[2], saved_view[3])
            else:
                self.fit_graph()
            self.graphStateChanged.emit(self.graph_info())
            return self.graph_info()

        def clear_graph(self, graph_key=None):
            return self.set_graph(None, graph_key)

        def fit_graph(self):
            self._auto_fit = True
            self._apply_fit()
            self._schedule_fit()

        def _apply_fit(self):
            if not self._auto_fit:
                return
            # Automatic scrollbar toggles can recursively resize fitInView.
            # The entire graph is visible in fit mode; manual mode restores bars.
            self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            if not self._nodes:
                self.resetTransform()
                self._zoom = 1.0
                return
            margin = 24.0 if self._graph["graphKind"] == "algorithm-dataflow" else 36.0
            bounds = self._scene.itemsBoundingRect().adjusted(-margin, -margin, margin, margin)
            if not bounds.isEmpty():
                self.fitInView(bounds, QtCore.Qt.KeepAspectRatio)
                self._zoom = abs(self.transform().m11())

        def _schedule_fit(self):
            if self._fit_pending or not self._auto_fit:
                return
            self._fit_pending = True
            QtCore.QTimer.singleShot(0, self._finish_fit)

        def _manual_view(self):
            self._auto_fit = False
            self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
            self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)

        def actual_size(self):
            self._manual_view()
            self.resetTransform()
            self._zoom = 1.0
            self.centerOn(self._scene.sceneRect().center())

        def _finish_fit(self):
            self._fit_pending = False
            self._apply_fit()

        def resizeEvent(self, event):
            super(ShaderFlowGraphView, self).resizeEvent(event)
            if getattr(self, "_auto_fit", False):
                self._schedule_fit()

        def showEvent(self, event):
            super(ShaderFlowGraphView, self).showEvent(event)
            if self._auto_fit:
                self._schedule_fit()

        def reset_layout(self):
            if not self._nodes:
                return
            positions = self._automatic_positions()
            for node_id, item in self._nodes.items():
                item.set_layout_position(positions[node_id])
            self._update_scene_rect()
            self.fit_graph()

        def wheelEvent(self, event):
            delta = event.angleDelta().y()
            if not delta:
                event.ignore()
                return
            factor = 1.15 if delta > 0 else 1.0 / 1.15
            next_zoom = self._zoom * factor
            if ((factor < 1.0 and next_zoom < 0.18) or
                    (factor > 1.0 and next_zoom > 4.5)):
                event.accept()
                return
            self._manual_view()
            old_position = self.mapToScene(event.pos())
            self.scale(factor, factor)
            self._zoom = next_zoom
            new_position = self.mapToScene(event.pos())
            offset = new_position - old_position
            self.translate(offset.x(), offset.y())
            event.accept()

        def _node_at(self, view_position):
            item = self.itemAt(view_position)
            while item is not None:
                if isinstance(item, _FlowNodeItem):
                    return item
                item = item.parentItem()
            return None

        def mousePressEvent(self, event):
            blank_left = (
                event.button() == QtCore.Qt.LeftButton and
                self._node_at(event.pos()) is None)
            if event.button() == QtCore.Qt.MiddleButton or blank_left:
                self._panning = True
                self._pan_origin = event.pos()
                self.viewport().setCursor(QtCore.Qt.ClosedHandCursor)
                event.accept()
                return
            super(ShaderFlowGraphView, self).mousePressEvent(event)

        def mouseMoveEvent(self, event):
            if self._panning:
                delta = event.pos() - self._pan_origin
                if not delta.isNull():
                    self._manual_view()
                self._pan_origin = event.pos()
                self.horizontalScrollBar().setValue(
                    self.horizontalScrollBar().value() - delta.x())
                self.verticalScrollBar().setValue(
                    self.verticalScrollBar().value() - delta.y())
                event.accept()
                return
            super(ShaderFlowGraphView, self).mouseMoveEvent(event)

        def mouseReleaseEvent(self, event):
            if self._panning and event.button() in (
                    QtCore.Qt.LeftButton, QtCore.Qt.MiddleButton):
                self._panning = False
                self.viewport().unsetCursor()
                event.accept()
                return
            super(ShaderFlowGraphView, self).mouseReleaseEvent(event)

        def keyPressEvent(self, event):
            if event.key() == QtCore.Qt.Key_Home:
                self.fit_graph()
                event.accept()
                return
            if (event.modifiers() & QtCore.Qt.ControlModifier and
                    event.key() == QtCore.Qt.Key_0):
                self.actual_size()
                event.accept()
                return
            super(ShaderFlowGraphView, self).keyPressEvent(event)


    class ShaderFlowGraphPanel(QtWidgets.QWidget):
        """Header, provenance state and canvas used as the workspace right pane."""

        nodeActivated = QtCore.Signal(object)
        subgraphRequested = QtCore.Signal(object)
        contextRequested = QtCore.Signal(object)

        def __init__(self, parent=None, navigate_callback=None, navigation_resolver=None):
            super(ShaderFlowGraphPanel, self).__init__(parent)
            self._navigate_callback = navigate_callback
            self._navigation_resolver = navigation_resolver
            self._history = GraphNavigationHistory()
            self.setObjectName("replaylabShaderFlowGraph")
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(4, 0, 0, 0)
            layout.setSpacing(4)

            header = QtWidgets.QHBoxLayout()
            self.back_button = QtWidgets.QToolButton(self)
            self.back_button.setArrowType(QtCore.Qt.LeftArrow)
            self.back_button.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Back to the parent function"))
            header.addWidget(self.back_button)
            self.forward_button = QtWidgets.QToolButton(self)
            self.forward_button.setArrowType(QtCore.Qt.RightArrow)
            self.forward_button.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Forward to the next graph"))
            header.addWidget(self.forward_button)
            self.title_label = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader call flow"), self)
            title_font = self.title_label.font()
            title_font.setBold(True)
            self.title_label.setFont(title_font)
            self.title_label.setSizePolicy(
                QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            header.addWidget(self.title_label, 1)
            self.state_label = QtWidgets.QLabel(self)
            self.state_label.setObjectName("replaylabShaderFlowGraphState")
            header.addWidget(self.state_label)
            self.limit_label = QtWidgets.QLabel(self)
            self.limit_label.setObjectName("replaylabShaderFlowGraphLimit")
            header.addWidget(self.limit_label)

            self.fit_button = QtWidgets.QToolButton(self)
            self.fit_button.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Fit"))
            self.fit_button.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Fit the call flow to the available space"))
            header.addWidget(self.fit_button)
            self.size_button = QtWidgets.QToolButton(self)
            self.size_button.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "100%"))
            self.size_button.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Show the flow at actual size"))
            header.addWidget(self.size_button)
            layout.addLayout(header)

            self.view = ShaderFlowGraphView(self)
            self.view.nodeActivated.connect(self._node_activated)
            self.view.subgraphRequested.connect(self.subgraphRequested.emit)
            self.view.contextRequested.connect(self.contextRequested.emit)
            self.back_button.clicked.connect(self.go_back)
            self.forward_button.clicked.connect(self.go_forward)
            self.fit_button.clicked.connect(self.view.fit_graph)
            self.size_button.clicked.connect(self.view.actual_size)
            layout.addWidget(self.view, 1)
            self.set_graph(None)

        def _node_activated(self, navigation):
            self.nodeActivated.emit(navigation)
            if callable(self._navigate_callback):
                self._navigate_callback(dict(navigation))

        def _update_state(self, info):
            self.title_label.setToolTip(info.get("functionName") or "")
            if info.get("functionName"):
                self.title_label.setText(_qt_format(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "%1: calculation flow"), info["functionName"]))
            elif info.get("graphKind") == "algorithm-dataflow":
                self.title_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Shader algorithm flow"))
            elif info.get("graphKind") == "function-execution":
                self.title_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Shader execution flow"))
            else:
                self.title_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Shader call flow"))
            count = len(info.get("nodes", []))
            if count == 0:
                if info.get("emptyState") == "building":
                    self.state_label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Building"))
                    self.state_label.setToolTip(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Building call flow from the current source..."))
                else:
                    self.state_label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Unavailable"))
                    self.state_label.setToolTip(info.get("errorDetail") or
                        QtCore.QCoreApplication.translate(
                            "ReplayLabProjects",
                            "A call flow will appear when organized HLSL includes module relationships."))
            elif info.get("stale"):
                self.state_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Out of date"))
                self.state_label.setToolTip(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "This call flow was generated from an earlier Shader revision."))
            elif info.get("generated"):
                self.state_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Generated reconstruction"))
                self.state_label.setToolTip(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Module names and descriptions are generated reconstruction, not original source."))
            elif info.get("sourceDerived"):
                self.state_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Derived from source"))
                if info.get("graphKind") == "function-execution":
                    self.state_label.setToolTip(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Main steps follow the current source execution paths; open a function for more detail. This is not a validation result."))
                else:
                    self.state_label.setToolTip(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Call relationships are derived from the current editor source; this is not a validation result."))
            else:
                self.state_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Available"))
                self.state_label.setToolTip("")

            if info.get("nodesTruncated") or info.get("edgesTruncated"):
                self.limit_label.setText(_qt_format(
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "%1 modules · %2 calls (limited)"),
                    len(info.get("nodes", [])), len(info.get("edges", []))))
                self.limit_label.setToolTip(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "The flow was limited to keep navigation responsive."))
            else:
                self.limit_label.clear()
                self.limit_label.setToolTip("")

        def set_graph(self, graph, graph_key=None, source_sha256=None,
                      stale=False, navigate_callback=None):
            if navigate_callback is not None:
                self._navigate_callback = navigate_callback
            self._history.reset(graph, {"graph_key": graph_key,
                                       "source_sha256": source_sha256, "stale": stale})
            return self._show_history()

        def _show_history(self):
            graph, options = self._history.current()
            info = self.view.set_graph(graph, **options)
            self._update_state(info)
            state = self._history.state()
            self.back_button.setEnabled(state["canBack"])
            self.forward_button.setEnabled(state["canForward"])
            return info

        def open_subgraph(self, graph, graph_key=None, source_sha256=None, stale=False,
                          ancestors=None):
            self._history.open(graph, {"graph_key": graph_key,
                                      "source_sha256": source_sha256, "stale": stale}, ancestors)
            return self._show_history()

        def go_back(self):
            return self._navigate_history(-1)

        def go_forward(self):
            return self._navigate_history(1)

        def _navigate_history(self, direction):
            target = self._history._cursor + direction
            if not 0 <= target < len(self._history._entries):
                return self.view.graph_info()
            graph, options = self._history._entries[target]
            if callable(self._navigation_resolver):
                resolved = self._navigation_resolver(copy.deepcopy(graph), dict(options))
                if resolved is None:
                    return self.view.graph_info()
                self._history._entries[target] = resolved
            self._history._cursor = target
            return self._show_history()

        def navigation_state(self):
            return self._history.state()

        def mark_source_changed(self):
            # Keep native anchors and the user's exploration intact while
            # making the older source binding explicit without scene rebuilds.
            if self.view._graph.get("stale"):
                return
            for unused_graph, options in self._history._entries:
                options["stale"] = True
            self.view._graph["stale"] = True
            self._update_state(self.view.graph_info())

        def clear_graph(self, graph_key=None, state="unavailable", detail=""):
            return self.set_graph({"emptyState": state, "errorDetail": detail}, graph_key)

        def fit_graph(self):
            self.view.fit_graph()

        def reset_layout(self):
            self.view.reset_layout()


else:
    class ShaderFlowGraphView(object):
        def __init__(self, *unused_args, **unused_kwargs):
            del unused_args, unused_kwargs
            raise RuntimeError("PySide2 Qt is required for the Shader flow graph")


    class ShaderFlowGraphPanel(object):
        def __init__(self, *unused_args, **unused_kwargs):
            del unused_args, unused_kwargs
            raise RuntimeError("PySide2 Qt is required for the Shader flow graph")


__all__ = [
    "FLOW_GRAPH_SCHEMA",
    "FLOW_GRAPH_VERSION",
    "HAS_QT",
    "MAX_GRAPH_EDGES",
    "MAX_GRAPH_NODES",
    "ShaderFlowGraphPanel",
    "ShaderFlowGraphView",
    "GraphNavigationHistory",
    "normalize_flow_graph",
]
