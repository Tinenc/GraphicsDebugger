"""Theme-aware, capture-bound frame rendering overview (Python 3.6 / PySide2).

The Host supplies a reviewed data document, never HTML or executable UI.  The
view owns only drawing and user navigation.  Thumbnail reads run off the GUI
thread and are bounded, scaled, and scheduled for the visible region only.
"""
from __future__ import print_function

import os
import re
import threading
import math
import time
import copy
import uuid
import hashlib
import json
from collections import OrderedDict
from .frame_flow_channels import channel_plan
from .project_frame_volume import normalize_volume_preview
from .project_frame_flow_layout import find_frame_flow_placeholder, retain_dock_ancestors

try:
    from PySide2 import QtCore, QtGui, QtWidgets
    HAS_QT = True
except ImportError:
    HAS_QT = False


FLOW_SCHEMA = "bread-frame-render-flow/v1"
MAX_STAGES = 256
MAX_IMAGES = 512
MAX_STAGE_IMAGES = 64
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 64 * 1024 * 1024
EXPORT_PART_HEIGHT = 32768
MAX_EXPORT_PART_PIXELS = 32 * 1024 * 1024
MAX_CHANNEL_CACHE_GROUPS = 16
CHANNEL_CACHE_SECONDS = 30.0
EXPORT_MESSAGE_MILLISECONDS = 6000
FLOW_ATTACHMENT_ROLE = 0x4252
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _text(value, maximum):
    return _CONTROL.sub("", "" if value is None else str(value))[:maximum]


def _event(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("Frame flow event ID must be a positive integer")
    return value


def channel_revision_key(value, depth=0):
    """The internal mutation receipt is JSON on disk and tuples in the bridge."""
    if depth > 8:
        raise ValueError("Channel cache revision is too deeply nested")
    if isinstance(value, (list, tuple)):
        if len(value) > 512:
            raise ValueError("Channel cache revision is too large")
        return tuple(channel_revision_key(item, depth + 1) for item in value)
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, str) and len(value) <= 256:
        return value
    raise ValueError("Channel cache revision is invalid")


def normalize_algorithm(value, members):
    """Keep Host-reviewed calculation data bounded and plain at the UI boundary."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Frame flow algorithm must be an object")
    result = {}
    for key in ("inputs", "steps", "outputs", "integration"):
        rows = value.get(key, [])
        if not isinstance(rows, list) or len(rows) > 32:
            raise ValueError("Frame flow algorithm requires bounded lists of at most 32 entries")
        result[key] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Frame flow algorithm entry must be an object")
            item = {"eventId": _event(row.get("eventId"))}
            if key != "integration" and item["eventId"] not in members:
                raise ValueError("Frame flow algorithm event does not belong to its stage")
            fields = (("title", 160), ("calculation", 1200), ("evidence", 2400)) if key == "steps" else (
                (("role", 240), ("evidence", 2400)) if key == "inputs" else (("role", 240),)
                if key == "outputs" else (("explanation", 1200), ("evidence", 2400)))
            for field, limit in fields:
                item[field] = _text(row.get(field), limit)
                if field != "evidence" and not item[field].strip():
                    raise ValueError("Frame flow algorithm explanation is missing")
            if key != "steps":
                item["resourceId"] = _event(row.get("resourceId"))
            if key == "inputs" and row.get("producerEventId") is not None:
                item["producerEventId"] = _event(row["producerEventId"])
            if key == "integration":
                item["sourceEventId"] = _event(row.get("sourceEventId"))
                if item["sourceEventId"] not in members:
                    raise ValueError("Frame flow algorithm producer does not belong to its stage")
                item["mode"] = row.get("mode")
                if item["mode"] not in ("downstream", "in-place", "present", "non-image"):
                    raise ValueError("Frame flow algorithm integration mode is unsupported")
                if row.get("targetResourceId") is not None:
                    item["targetResourceId"] = _event(row["targetResourceId"])
            result[key].append(item)
    return result


def _brief(value, maximum=240):
    value = " ".join(str(value).split())
    if len(value) <= maximum:
        return value
    prefix = value[:maximum]
    boundary = max(prefix.rfind("。"), prefix.rfind(";"), prefix.rfind("；"))
    return prefix[:boundary + 1] if boundary > maximum // 2 else prefix.rstrip() + "…"


def normalize_document(document):
    """Defensive UI boundary; authoritative capture/evidence checks are in Host."""
    if not isinstance(document, dict) or document.get("schema") != FLOW_SCHEMA:
        raise ValueError("Unsupported frame rendering flow schema")
    stages = document.get("stages")
    if not isinstance(stages, list) or not stages or len(stages) > MAX_STAGES:
        raise ValueError("Frame rendering flow requires 1 to 256 stages")
    result = {"schema": FLOW_SCHEMA, "schemaVersion": document.get("schemaVersion", 1)}
    for key, limit in (("flowId", 128), ("captureSessionId", 128), ("captureId", 128),
                       ("capturePath", 32768), ("title", 160), ("summary", 2400)):
        result[key] = _text(document.get(key), limit)
    if not result["flowId"] or not result["captureSessionId"]:
        raise ValueError("Frame rendering flow identity is missing")
    coverage = document.get("coverage") or {}
    result["coverage"] = {
        key: coverage.get(key) for key in (
            "totalActionCount", "coveredActionCount", "omittedActionCount",
            "complete", "sourceComplete", "totalSourceActionCount", "notes")}
    quality = document.get("quality") or {}
    result["quality"] = {"analysisComplete": bool(quality.get("analysisComplete")),
                         "gaps": [{key: _text(gap.get(key), 1200) for key in (
                             "code", "stageId", "message", "nextAction")}
                                  for gap in (quality.get("gaps") or [])[:1024]
                                  if isinstance(gap, dict)]}
    result["stages"] = []
    seen, seen_events, total_images = set(), set(), 0
    for raw in stages:
        if not isinstance(raw, dict):
            raise ValueError("Frame flow stage must be an object")
        stage = {key: _text(raw.get(key), limit) for key, limit in (
            ("id", 128), ("title", 160), ("summary", 1200), ("confidence", 64),
            ("category", 64), ("navigationActionKind", 64), ("navigationStatus", 64))}
        if not stage["id"] or stage["id"] in seen or not stage["title"]:
            raise ValueError("Frame flow stage IDs and titles must be present and unique")
        seen.add(stage["id"])
        start, end = _event(raw.get("startEventId")), _event(raw.get("endEventId"))
        events = raw.get("eventIds")
        if not isinstance(events, list) or len(events) > 100000:
            raise ValueError("Frame flow stage event IDs must be a bounded list")
        # Host has verified executionIndex order. EIDs need not be numerically
        # monotonic; preserve the capture's execution sequence exactly.
        events = [_event(event) for event in events]
        if not events or events[0] != start or events[-1] != end:
            raise ValueError("Frame flow stage endpoints do not match its execution sequence")
        if len(set(events)) != len(events) or seen_events.intersection(events):
            raise ValueError("Frame flow stages must not overlap in execution order")
        seen_events.update(events)
        representative = _event(raw.get("representativeEventId"))
        if representative not in events:
            raise ValueError("Frame flow representative must belong to its stage")
        stage.update(startEventId=start, endEventId=end, eventIds=events,
                     representativeEventId=representative)
        stage["algorithm"] = normalize_algorithm(raw.get("algorithm"), set(events))
        navigation = raw.get("navigationEventId", representative)
        if isinstance(navigation, bool):
            raise ValueError("Frame flow navigation must be an integer")
        if navigation != 0:
            navigation = _event(navigation)
            if navigation not in events:
                raise ValueError("Frame flow navigation must belong to its stage")
        stage["navigationEventId"] = navigation
        stage["navigationHasWork"] = raw.get("navigationHasWork")
        investigation = raw.get("investigation") or {}
        stage["investigation"] = {key: _text(investigation.get(key), 1200) for key in (
            "status", "evidence", "unresolvedReason", "nextAction")}
        stage["techniques"] = [{key: _text(technique.get(key), 1200) for key in (
            "name", "status", "evidence", "summary")}
            for technique in (raw.get("techniques") or [])[:32] if isinstance(technique, dict)]
        output_coverage = raw.get("outputCoverage") or {}
        stage["outputCoverage"] = {"status": _text(output_coverage.get("status"), 64),
            "inventoryComplete": output_coverage.get("inventoryComplete"),
            "missingResourceIds": [_text(rid, 128) for rid in
                                   (output_coverage.get("missingResourceIds") or [])[:64]]}
        stage["outputCoverage"]["bindingErrors"] = [
            {name: _text(item.get(name), 1200) for name in ("kind", "stage", "error")}
            for item in (output_coverage.get("bindingErrors") or [])[:64] if isinstance(item, dict)]
        for key in ("nonImageOutputs", "missingAttachments"):
            stage["outputCoverage"][key] = [{name: _text(item.get(name), 160) for name in (
                "resourceId", "kind", "index", "stage", "resourceType", "captureReason", "name")}
                for item in (output_coverage.get(key) or [])[:128] if isinstance(item, dict)]
        images = raw.get("images") or []
        if not isinstance(images, list) or len(images) > MAX_STAGE_IMAGES:
            raise ValueError("Frame flow stage has too many previews")
        stage["images"] = []
        for raw_image in images:
            if not isinstance(raw_image, dict):
                raise ValueError("Frame flow preview must be an object")
            preview = {key: _text(raw_image.get(key), limit) for key, limit in (
                ("title", 160), ("description", 1200), ("path", 32768),
                ("channel", 128), ("format", 128), ("resourceId", 128),
                ("sha256", 128),
                ("label", 160), ("semantic", 1200), ("channelMeaning", 1200), ("evidence", 1200),
                ("attachmentStage", 64), ("attachmentKind", 32))}
            component_count = raw_image.get("componentCount", 0)
            preview["componentCount"] = component_count if type(component_count) is int and 1 <= component_count <= 4 else 0
            if "channelCacheComplete" in raw_image:
                preview["channelCacheComplete"] = raw_image.get("channelCacheComplete") is True
            preview["channelCacheSession"] = _text(raw_image.get("channelCacheSession"), 256)
            try:
                preview["channelCacheRevision"] = channel_revision_key(raw_image.get("channelCacheRevision"))
            except ValueError:
                preview["channelCacheRevision"] = None
                preview["channelCacheComplete"] = False
            if "channelGroups" in raw_image:
                groups = raw_image["channelGroups"]
                if not isinstance(groups, list) or len(groups) > 4:
                    raise ValueError("Frame flow channel groups require at most four entries")
                preview["channelGroups"] = []
                for group in groups:
                    if not isinstance(group, dict) or group.get("channel") not in ("rgb", "r", "g", "b", "a", "depth"):
                        raise ValueError("Frame flow channel group is unsupported")
                    preview["channelGroups"].append({"channel": group["channel"],
                        "label": _text(group.get("label"), 160), "description": _text(group.get("description"), 1200)})
                channel_plan(preview)
            preview["title"] = preview["title"] or preview["label"]
            preview["group"] = raw_image.get("group", "outputs")
            if preview["group"] not in ("outputs", "channels", "inputs", "animation-frames"):
                raise ValueError("Frame flow preview group is unsupported")
            preview["presentation"] = raw_image.get("presentation", "primary")
            if preview["presentation"] not in ("primary", "detail"):
                raise ValueError("Frame flow preview presentation must be primary or detail")
            if (not os.path.isabs(preview["path"]) or
                    os.path.splitext(preview["path"])[1].lower() != ".png"):
                raise ValueError("Frame flow preview must be a Host-verified absolute PNG path")
            preview["eventId"] = _event(raw_image.get("eventId"))
            if preview["group"] != "inputs" and preview["eventId"] not in events:
                raise ValueError("Frame flow preview event does not belong to its stage")
            mapping = raw_image.get("displayMapping") or {}
            preview["displayMapping"] = {
                key: _text(mapping.get(key), 160) if mapping.get(key) is not None else ""
                for key in ("rangeMode", "blackPoint", "whitePoint", "depthInterpretation", "valueDecode",
                            "mip", "slice", "sample", "attachmentKind", "attachmentIndex", "attachmentStage")}
            if "flipY" in mapping:
                preview["displayMapping"]["flipY"] = mapping["flipY"] is True
            for key in ("attachmentKind", "attachmentIndex", "attachmentStage"):
                if preview["displayMapping"][key] == "" and raw_image.get(key) not in (None, ""):
                    preview["displayMapping"][key] = _text(raw_image[key], 160)
            for key in ("width", "height"):
                value = raw_image.get(key)
                preview[key] = value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
            if raw_image.get("volumePreview") is not None:
                preview["volumePreview"] = normalize_volume_preview(raw_image["volumePreview"], preview)
            parent = raw_image.get("parentImageIndex")
            if parent is not None:
                if (isinstance(parent, bool) or not isinstance(parent, int) or parent < 0 or
                        parent >= len(images) or parent == len(stage["images"])):
                    raise ValueError("Frame flow channel parent must reference another preview")
                preview["parentImageIndex"] = parent
            stage["images"].append(preview)
        comparisons = raw.get("comparisons") or []
        if not isinstance(comparisons, list) or len(comparisons) > 8:
            raise ValueError("Frame flow stage has too many comparisons")
        stage["comparisons"] = []
        for comparison in comparisons:
            if not isinstance(comparison, dict):
                raise ValueError("Frame flow comparison must be an object")
            left, right = comparison.get("leftImageIndex"), comparison.get("rightImageIndex")
            if (isinstance(left, bool) or isinstance(right, bool) or
                    not isinstance(left, int) or not isinstance(right, int) or left == right or
                    min(left, right) < 0 or max(left, right) >= len(images)):
                raise ValueError("Frame flow comparison must reference two existing previews")
            item = {key: _text(comparison.get(key), 1200) for key in (
                "label", "description", "relationship")}
            item.update(leftImageIndex=left, rightImageIndex=right,
                        visualComparable=bool(comparison.get("visualComparable", True)),
                        differentResource=bool(comparison.get("differentResource", False)))
            stage["comparisons"].append(item)
        animations = raw.get("animations") or []
        if not isinstance(animations, list) or len(animations) > 8:
            raise ValueError("Frame flow stage has too many animations")
        stage["animations"] = []
        for animation in animations:
            if not isinstance(animation, dict):
                raise ValueError("Frame flow animation must be an object")
            indices = animation.get("imageIndices")
            duration = animation.get("frameDurationMs", 400)
            if (not isinstance(indices, list) or not 2 <= len(indices) <= 16 or
                    any(isinstance(index, bool) or not isinstance(index, int) or
                        index < 0 or index >= len(images) for index in indices)):
                raise ValueError("Frame flow animation requires 2 to 16 existing previews")
            if isinstance(duration, bool) or not isinstance(duration, int) or not 100 <= duration <= 5000:
                raise ValueError("Frame flow animation interval must be 100 to 5000 milliseconds")
            stage["animations"].append({"label": _text(animation.get("label"), 160),
                "description": _text(animation.get("description"), 1200),
                "imageIndices": list(indices), "frameDurationMs": duration})
        steps = raw.get("processSteps") or []
        if not isinstance(steps, list) or len(steps) > 12 or len(steps) == 1:
            raise ValueError("Frame flow process requires 2 to 12 steps")
        stage["processSteps"] = []
        for step in steps:
            if not isinstance(step, dict) or not _text(step.get("label"), 160):
                raise ValueError("Frame flow process step requires a label")
            fields = [(key, size) for key, size in (("imageIndex", len(images)),
                ("comparisonIndex", len(stage["comparisons"])),
                ("animationIndex", len(stage["animations"]))) if key in step]
            if len(fields) != 1:
                raise ValueError("Frame flow process step must reference one image, comparison or animation")
            key, size = fields[0]
            index = step[key]
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < size:
                raise ValueError("Frame flow process step references missing media")
            stage["processSteps"].append({"label": _text(step["label"], 160),
                "description": _text(step.get("description"), 1200), key: index})
        stage["processGraph"] = normalize_process_graph(raw.get("processGraph"), stage)
        visual_review = raw.get("visualReview")
        stage["visualReview"] = ({key: visual_review[key] for key in
            ("graphPresentation", "processStepsPresentation") if visual_review.get(key) in ("omit", "show")}
            if isinstance(visual_review, dict) else {})
        total_images += len(stage["images"])
        if total_images > MAX_IMAGES:
            raise ValueError("Frame rendering flow has too many previews")
        result["stages"].append(stage)
    return result


def normalize_process_graph(value, stage):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Frame flow process graph must be an object")
    nodes, edges = value.get("nodes"), value.get("edges")
    if not isinstance(nodes, list) or not 2 <= len(nodes) <= 16:
        raise ValueError("Frame flow process graph requires 2 to 16 nodes")
    if not isinstance(edges, list) or not 1 <= len(edges) <= 32:
        raise ValueError("Frame flow process graph requires 1 to 32 edges")
    allowed_events = set(stage["eventIds"])
    for rows in (stage.get("algorithm") or {}).values():
        for row in rows:
            allowed_events.update(row[key] for key in ("eventId", "sourceEventId", "producerEventId") if key in row)
    normalized, ids = [], set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("Frame flow process graph node must be an object")
        item = {key: _text(node.get(key), limit) for key, limit in
                (("id", 128), ("label", 160), ("description", 1200))}
        if not item["id"] or item["id"] in ids or not item["label"]:
            raise ValueError("Frame flow process graph node IDs and labels must be unique and present")
        ids.add(item["id"])
        refs = [(key, collection) for key, collection in (("imageIndex", "images"),
            ("comparisonIndex", "comparisons"), ("animationIndex", "animations")) if key in node]
        if len(refs) > 1:
            raise ValueError("Frame flow process graph node references multiple media")
        for key, collection in refs:
            index = node[key]
            if type(index) is not int or not 0 <= index < len(stage[collection]):
                raise ValueError("Frame flow process graph node references missing media")
            item[key] = index
        if "eventId" in node:
            event = _event(node["eventId"])
            if event not in allowed_events:
                raise ValueError("Frame flow process graph node event is not a stage member or an evidenced related event")
            if "imageIndex" in item and event != stage["images"][item["imageIndex"]]["eventId"]:
                raise ValueError("Frame flow process graph image event does not match")
            item["eventId"] = event
        normalized.append(item)
    links, pairs = [], set()
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError("Frame flow process graph edge must be an object")
        source, target = _text(edge.get("from"), 128), _text(edge.get("to"), 128)
        if source not in ids or target not in ids or source == target or (source, target) in pairs:
            raise ValueError("Frame flow process graph edge has a missing, repeated or identical endpoint")
        pairs.add((source, target))
        links.append({"from": source, "to": target, "label": _text(edge.get("label"), 160)})
    graph = {"nodes": normalized, "edges": links}
    process_graph_layers(graph)
    return graph


def process_graph_layers(graph):
    """Stable topological ranks; branches keep their declared order."""
    pending = [node["id"] for node in graph["nodes"]]
    predecessors = {key: set() for key in pending}
    for edge in graph["edges"]:
        predecessors[edge["to"]].add(edge["from"])
    levels = {}
    while pending:
        ready = [key for key in pending if predecessors[key].issubset(levels)]
        if not ready:
            raise ValueError("Frame flow process graph must not contain a cycle")
        for key in ready:
            levels[key] = max((levels[parent] + 1 for parent in predecessors[key]), default=0)
            pending.remove(key)
    return [[node["id"] for node in graph["nodes"] if levels[node["id"]] == level]
            for level in range(max(levels.values()) + 1)]


def process_graph_layout(graph, heights, width=1040.0):
    layers = process_graph_layers(graph)
    columns = min(3, len(layers))
    node_width = (width - 60 * (columns - 1)) / columns
    positions, bands, y = {}, [], 0.0
    for start in range(0, len(layers), columns):
        group = layers[start:start + columns]
        layer_heights = [sum(heights[key] for key in layer) + 24 * (len(layer) - 1) for layer in group]
        height = max(layer_heights)
        bands.append((y, y + height + 48))
        for column, layer in enumerate(group):
            row_y = y + 48 + (height - layer_heights[column]) * 0.5
            for key in layer:
                positions[key] = {"x": column * (node_width + 60), "y": row_y,
                                  "width": node_width, "height": heights[key], "layer": start + column,
                                  "band": len(bands) - 1}
                row_y += heights[key] + 24
        y += height + 100
    return positions, bands, y


def binding_matches(document, binding):
    if not document or not isinstance(binding, dict):
        return False
    if document.get("captureSessionId") != binding.get("captureSessionId"):
        return False
    left, right = document.get("capturePath"), binding.get("capturePath")
    if not left or not right:
        return False
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def frame_flow_attachment(document, selection, selected_event=0):
    """Reference a published stage/media item without image reads or replay work."""
    stage = next((item for item in document.get('stages', [])
                  if item['id'] == selection.get('stageId')), None)
    if stage is None:
        raise ValueError('Frame flow stage is unavailable')
    chosen, images = dict(selection), stage.get('images', [])
    image_index = chosen.get('imageIndex')
    node = None
    graph = stage.get('processGraph') or {}
    step = None
    if 'processStepIndex' in chosen:
        index, steps = chosen['processStepIndex'], stage.get('processSteps') or []
        if type(index) is not int or not 0 <= index < len(steps):
            raise ValueError('Frame flow step is unavailable')
        step = steps[index]
        image_index = step.get('imageIndex', image_index)
        for key in ('comparisonIndex', 'animationIndex'):
            if key in step:
                chosen[key] = step[key]
    if chosen.get('nodeId'):
        node = next((item for item in graph.get('nodes', []) if item['id'] == chosen['nodeId']), None)
        if node is None:
            raise ValueError('Frame flow calculation is unavailable')
        image_index = node.get('imageIndex', image_index)
        for key in ('comparisonIndex', 'animationIndex'):
            if key in node:
                chosen[key] = node[key]
    if image_index is not None and (type(image_index) is not int or not 0 <= image_index < len(images)):
        raise ValueError('Frame flow texture is unavailable')
    preview = images[image_index] if image_index is not None else None
    selected_media, referenced = None, []
    for key, collection in (('comparisonIndex', 'comparisons'), ('animationIndex', 'animations')):
        if key not in chosen:
            continue
        index, entries = chosen[key], stage.get(collection) or []
        if type(index) is not int or not 0 <= index < len(entries):
            raise ValueError('Frame flow media is unavailable')
        selected_media = entries[index]
        referenced.extend(selected_media.get('imageIndices') or
                          [selected_media['leftImageIndex'], selected_media['rightImageIndex']])
    event = (preview or {}).get('eventId') or (node or {}).get('eventId') or stage['representativeEventId']
    title = ((preview or {}).get('title') if 'imageIndex' in selection else None)
    title = title or (node or {}).get('label') or (step or {}).get('label') or (preview or {}).get('title') or (selected_media or {}).get('label') or stage['title']
    identity = hashlib.sha256(('%s\n%s\n%s' % (document['flowId'], stage['id'],
        json.dumps(chosen, sort_keys=True))).encode('utf8')).hexdigest()
    context = {'schema': 'replaylab-attachment-context/v1',
        'origin': {'kind': 'other', 'panel': 'frame-render-flow', 'sourceId': identity},
        'capture': {'eventId': event, 'selectedEventId': int(selected_event),
                    'captureSessionId': document['captureSessionId']}}
    if document.get('captureId'):
        context['capture']['captureId'] = document['captureId']
    if preview and preview.get('resourceId'):
        resource = {'kind': 'texture', 'resourceId': str(preview['resourceId']),
                    'name': ' '.join(_text(preview.get('title'), 512).split())}
        for key in ('mip', 'slice', 'sample'):
            value = (preview.get('displayMapping') or {}).get(key)
            if str(value).isdigit() and 0 <= int(value) <= 0xffffffff:
                resource[key] = int(value)
        context['resource'] = resource
    indices = []
    for index in ([image_index] if image_index is not None else []) + referenced + list(range(len(images))):
        if index not in indices:
            indices.append(index)
        if len(indices) >= 16:
            break
    media = []
    for index in indices:
        image = images[index]
        record = {key: _text(image.get(key), 480) for key in
                  ('title', 'description', 'resourceId', 'channel', 'semantic', 'channelMeaning', 'sha256')}
        record.update(imageIndex=index, eventId=image['eventId'],
                      navigation=preview_navigation(image))
        media.append(record)
    nodes = [{key: value for key, value in item.items() if key in
              ('id', 'label', 'eventId', 'imageIndex', 'comparisonIndex', 'animationIndex')}
             for item in graph.get('nodes', [])[:32]]
    facts = {'schema': 'bread-flow-selection/v1', 'kind': 'frame-render-flow',
        'flowId': document['flowId'], 'flowTitle': document.get('title', ''),
        'capturePath': _text(document.get('capturePath'), 4096),
        'captureId': document.get('captureId', ''), 'captureSessionId': document['captureSessionId'],
        'stageSnapshotSha256': hashlib.sha256(json.dumps(stage, sort_keys=True, ensure_ascii=False).encode('utf8')).hexdigest(),
        'selection': chosen, 'title': title,
        'stage': {key: stage.get(key) for key in ('id', 'title', 'summary', 'confidence',
            'startEventId', 'endEventId', 'representativeEventId', 'navigationEventId')},
        'eventIds': list(stage['eventIds'][:256]), 'eventCount': len(stage['eventIds']),
        'images': media, 'imageCount': len(images),
        'processGraph': {'nodes': nodes, 'edges': list(graph.get('edges', [])[:64])},
        'processSteps': [{key: item.get(key) for key in
                          ('label', 'imageIndex', 'comparisonIndex', 'animationIndex') if key in item}
                         for item in stage.get('processSteps', [])[:12]],
        'comparisons': [{key: item.get(key) for key in ('label', 'leftImageIndex', 'rightImageIndex', 'relationship')}
                        for item in stage.get('comparisons', [])[:8]],
        'animations': [{key: item.get(key) for key in ('label', 'imageIndices')}
                       for item in stage.get('animations', [])[:8]],
        'evidenceTruncated': len(stage['eventIds']) > 256 or len(images) > 16}
    return context, facts


def matching_stage_ids(document, query):
    query = str(query or "").strip().casefold()
    numeric = query[3:].strip() if query.startswith("eid") else query
    event = int(numeric) if numeric.isdigit() else None
    matches = []
    for stage in (document or {}).get("stages", []):
        text = " ".join([stage["title"], stage["summary"]] + [
            " ".join(image.get(key, "") for key in ("title", "description", "semantic", "channelMeaning"))
            for image in stage["images"]] + [technique["name"] for technique in stage.get("techniques", [])] + [
            " ".join(str(row.get(key, "")) for key in ("title", "calculation", "role", "explanation"))
            for rows in (stage.get("algorithm") or {}).values() for row in rows])
        text += " " + " ".join(" ".join(node.get(key, "") for key in ("label", "description"))
                               for node in (stage.get("processGraph") or {}).get("nodes", []))
        if not query or query in text.casefold() or event in stage["eventIds"]:
            matches.append(stage["id"])
    return matches


def preview_navigation(preview):
    """Exact output identity supplied to the bridge; no resource is selected by label."""
    mapping = preview.get("displayMapping") or {}
    result = {"eventId": _event(preview["eventId"]), "resourceId": str(preview.get("resourceId") or ""),
              "channel": str(preview.get("channel") or ""),
              "attachmentKind": str(preview.get("attachmentKind") or mapping.get("attachmentKind") or ""),
              "attachmentStage": str(preview.get("attachmentStage") or mapping.get("attachmentStage") or "")}
    result["displayMapping"] = dict(mapping)
    for key in ("blackPoint", "whitePoint"):
        if mapping.get(key) not in (None, ""):
            value = float(mapping[key])
            if not math.isfinite(value):
                raise ValueError("Frame flow display range must be finite")
            result["displayMapping"][key] = value
    for key in ("mip", "slice", "sample", "attachmentIndex"):
        value = preview.get(key, mapping.get(key))
        if value not in (None, ""):
            try:
                result[key] = max(0, int(value))
            except (TypeError, ValueError):
                raise ValueError("Frame flow preview subresource must be an integer")
    return result


def texture_channel_groups(images):
    """Attach channel evidence to its exact texture snapshot, never by title."""
    groups, unassigned = {}, []
    def identity(image):
        mapping = image.get("displayMapping") or {}
        return (str(image.get("resourceId") or "").replace("ResourceId::", ""), image.get("eventId"),
                tuple(str(mapping.get(key) or 0) for key in ("mip", "slice", "sample")))
    for index, image in enumerate(images):
        if image.get("group") != "channels":
            continue
        parent = image.get("parentImageIndex")
        candidates = ([parent] if parent is not None else [candidate for candidate, base in enumerate(images)
            if base.get("group", "outputs") in ("outputs", "inputs") and
            image.get("resourceId") and identity(image) == identity(base)])
        candidates = [candidate for candidate in candidates if 0 <= candidate < len(images) and
                      images[candidate].get("group", "outputs") in ("outputs", "inputs") and
                      image.get("resourceId") and identity(image) == identity(images[candidate])]
        if candidates:
            groups.setdefault(candidates[0], []).append(image)
        else:
            unassigned.append(image)
    for channels in groups.values():
        channels.sort(key=lambda image: {"rgb": -1, "rgba": -1, "r": 0, "g": 1, "b": 2, "a": 3}.get(
            str(image.get("channel") or "").lower(), 4))
    return groups, unassigned


def channel_previews_complete(parent, images):
    channels = {str(image.get("channel", "")).lower().replace("rgba", "rgb") for image in images}
    expected = {group["channel"] for group in channel_plan(parent or {})}
    if expected == {"r"} and "depth" in channels:
        return True
    return expected.issubset(channels)


def channel_details_available(parent):
    """A single semantic group already has its useful view in the main card."""
    return len(channel_plan(parent or {})) > 1


def planned_channel_previews(parent, images, allow_parent_rgb=True):
    """Arrange real receipts by semantic plan; the opaque overview can supply RGB only."""
    available = {str(image.get("channel", "")).lower().replace("rgba", "rgb"): image for image in images}
    result = []
    for group in channel_plan(parent):
        channel = group["channel"]
        image = available.get(channel)
        if (image is None and allow_parent_rgb and channel == "rgb" and
                str(parent.get("channel", "")).lower() in ("rgb", "rgba")):
            image = dict(parent, channel="rgb", group="channels")
        if image is None:
            continue
        image = dict(image)
        image["channel"] = channel
        if group.get("label"):
            image["title"] = group["label"]
        if group.get("description"):
            image["channelMeaning"] = group["description"]
        result.append(image)
    return result


def saved_channels_current(parent, images, session, revision, document_revision):
    if not parent or not parent.get("channelCacheComplete") or not channel_previews_complete(parent, images):
        return False
    recorded = parent.get("channelCacheSession")
    if not recorded:
        return False
    if recorded == session:
        return channel_revision_key(parent.get("channelCacheRevision")) == channel_revision_key(revision)
    # A restored capture's saved receipts remain useful until this viewing
    # session changes Shader or CBuffer values. Never compare unrelated epochs.
    return channel_revision_key(revision) == channel_revision_key(document_revision)


def prune_channel_cache(cache, generation, revision, now):
    """Drop old replay values before reusing any decoded channel receipts."""
    for key, value in list(cache.items()):
        if key[:2] != (generation, revision) or now - value[0] >= CHANNEL_CACHE_SECONDS:
            cache.pop(key, None)
    while len(cache) > MAX_CHANNEL_CACHE_GROUPS:
        cache.popitem(last=False)


def normalize_channel_previews(parent, images):
    """Validate asynchronous Host previews without deriving any image channels."""
    if not isinstance(images, list) or not images or len(images) > 16:
        raise ValueError("Channel response requires 1 to 16 verified previews")
    if any(not isinstance(image, dict) for image in images):
        raise ValueError("Channel response preview must be an object")
    event = _event(parent["eventId"])
    prepared = []
    for image in images:
        image = dict(image, group="channels")
        image.pop("parentImageIndex", None)
        if not image.get("description") and image.get("caption"):
            image["description"] = image["caption"]
        prepared.append(image)
    # Reuse the same untrusted-display boundary as a published document. The
    # synthetic container is not published, persisted or used for navigation.
    normalized = normalize_document({"schema": FLOW_SCHEMA, "flowId": "channel-response",
        "captureSessionId": "channel-response", "stages": [{"id": "channels", "title": "channels",
        "startEventId": event, "endEventId": event, "eventIds": [event],
        "representativeEventId": event, "images": prepared}]})["stages"][0]["images"]
    def identity(preview):
        mapping = preview.get("displayMapping") or {}
        resource = str(preview.get("resourceId") or "").replace("ResourceId::", "")
        return resource, tuple(int(mapping.get(key) or 0) for key in ("mip", "slice", "sample"))
    if not parent.get("resourceId"):
        raise ValueError("Channel parent resource identity is missing")
    seen = set()
    for image in normalized:
        channel = image["channel"].lower()
        if channel == "rgba" and "rgb" in {group["channel"] for group in channel_plan(parent)}:
            channel = "rgb"
        if channel not in ("rgb", "r", "g", "b", "a", "depth") or channel in seen:
            raise ValueError("Channel response contains unsupported or repeated channels")
        if identity(image) != identity(parent):
            raise ValueError("Channel response does not match its texture subresource")
        image["channel"] = channel
        seen.add(channel)
    normalized.sort(key=lambda image: {"rgb": -1, "r": 0, "g": 1, "b": 2, "a": 3, "depth": 4}[image["channel"]])
    return normalized


def preview_card_identity(preview):
    """Only exact evidence plus identical explanatory text is interchangeable.

    A reused RID is not an image identity: time, subresource, channel and exposure
    all change the meaning of the preview. Presentation/group only place a card.
    """
    path = str(preview.get("path") or "")
    return (os.path.normcase(os.path.abspath(path)) if path else "",
        tuple(str(preview.get(key) or "") for key in (
            "sha256", "eventId", "resourceId", "channel", "format", "width", "height", "attachmentStage",
            "title", "description", "label", "semantic", "channelMeaning", "evidence")),
        tuple(sorted((str(key), str(value)) for key, value in (preview.get("displayMapping") or {}).items())),
        tuple(tuple(str(group.get(key) or "") for key in ("channel", "label", "description"))
              for group in channel_plan(preview)),
        tuple(tuple(row.get(key) for key in ("slice", "channel", "path", "sha256"))
              for row in (preview.get("volumePreview") or {}).get("slices", [])))


def standalone_preview_groups(images, referenced_indices):
    """Explicit narrative media wins; fold detail cards and remove exact repeats."""
    seen = {preview_card_identity(images[index]) for index in referenced_indices}
    result = {"outputs": [], "inputs": [], "details": []}
    for group in ("outputs", "inputs", "details"):
        for image in images:
            image_group = image.get("group", "outputs")
            if image_group not in ("outputs", "inputs"):
                continue
            destination = "details" if image.get("presentation", "primary") == "detail" else image_group
            if destination != group:
                continue
            identity = preview_card_identity(image)
            if identity not in seen:
                seen.add(identity)
                result[group].append(image)
    return result


def channel_detail_meaning(preview):
    """Pick only this channel's supplied meaning, without inventing its role."""
    channel = str(preview.get("channel") or "").upper().replace("RGBA", "RGB")
    meaning = str(preview.get("channelMeaning") or "").strip()
    # Older diagrams sometimes repeat all four meanings on every channel image.
    # Select only an explicitly labelled meaning; never transfer another channel's role.
    labelled = dict((match.group(1).upper().replace("RGBA", "RGB"), match.group(2).strip())
        for match in re.finditer(r"(?:^|[;；\n。])\s*(RGB|RGBA|R|G|B|A)\s*[:：]\s*(.*?)"
            r"(?=(?:[;；\n。]\s*(?:RGB|RGBA|R|G|B|A)\s*[:：])|$)", meaning, re.I | re.S))
    label = labelled.get(channel, "")
    if not label:
        label = (meaning if meaning and not labelled else
                 str(preview.get("title") or displayed_semantic(preview.get("semantic")) or "").strip())
    label = re.sub(r"^" + re.escape(channel) + r"\s*[:：]\s*", "", label, flags=re.I) if channel else label
    return label


def channel_detail_title(preview):
    """Use a short supplied role; the complete explanation stays below the image."""
    channel = str(preview.get("channel") or "").upper().replace("RGBA", "RGB")
    label = channel_detail_meaning(preview)
    label = re.split(r"[。；;\n，,]|[.!?]\s", label, maxsplit=1)[0].strip()
    # No model inference at display time: shorten prose rather than fabricate a
    # technical name. Full text remains in both the caption and the tooltip.
    limit = 16 if re.search(r"[\u3400-\u9fff]", label) else 28
    if len(label) > limit:
        shortened = label[:limit - 1]
        if " " in shortened and not re.search(r"[\u3400-\u9fff]", shortened):
            shortened = shortened.rsplit(" ", 1)[0]
        label = shortened.rstrip() + "…"
    return (channel + ": " + label) if channel and label else channel or label


def channel_detail_caption(preview):
    return concise_caption(dict(preview, channelMeaning=channel_detail_meaning(preview)))


def _process_media_indices(stage, node):
    if "imageIndex" in node:
        return [node["imageIndex"]]
    if "comparisonIndex" in node:
        comparison = stage["comparisons"][node["comparisonIndex"]]
        return [comparison["leftImageIndex"], comparison["rightImageIndex"]]
    if "animationIndex" in node:
        return stage["animations"][node["animationIndex"]]["imageIndices"]
    return []


def _output_snapshot_identity(preview):
    """Only captured version identity; channel/exposure are alternate views."""
    if preview.get("group", "outputs") not in ("outputs", "animation-frames"):
        return None
    resource = str(preview.get("resourceId") or "").replace("ResourceId::", "")
    try:
        resource = int(resource)
        if resource <= 0:
            return None
        mapping = preview.get("displayMapping") or {}
        subresource = tuple(int(mapping.get(key) or 0) for key in ("mip", "slice", "sample"))
    except (TypeError, ValueError):
        return None
    return preview.get("eventId"), resource, subresource


def _process_describes_one_result(stage, graph):
    """Avoid presenting one draw's arithmetic or repeated output as pass flow.

    This is a display-only check of saved identities. It neither guesses Shader
    calculations nor claims that two different resources have a dependency.
    """
    events, snapshots, unbound = set(), {}, False
    for node in graph["nodes"]:
        indices = _process_media_indices(stage, node)
        images = [stage["images"][index] for index in indices]
        events.update(image["eventId"] for image in images)
        if node.get("eventId"):
            events.add(node["eventId"])
        elif not images:
            unbound = True
        snapshots[node["id"]] = set(identity for identity in
            (_output_snapshot_identity(image) for image in images) if identity is not None)
    algorithm_events = {step["eventId"] for step in (stage.get("algorithm") or {}).get("steps", [])}
    if len(events) == 1 and (not unbound or set(stage.get("eventIds", [])) == events or algorithm_events == events):
        return True
    successors = {node["id"]: [] for node in graph["nodes"]}
    for edge in graph["edges"]:
        successors[edge["from"]].append(edge["to"])
    for source, source_snapshots in snapshots.items():
        if not source_snapshots:
            continue
        pending, visited = list(successors[source]), set()
        while pending:
            target = pending.pop()
            if target in visited:
                continue
            visited.add(target)
            # A comparison/animation may intentionally contain the preceding
            # image alongside a later result. Only repeating the complete set
            # invents another stage from the same captured output snapshots.
            if source_snapshots == snapshots[target]:
                return True
            pending.extend(successors[target])
    return False


def presented_process_graph(stage):
    # Preserve the reviewed relationship evidence in the document while omitting
    # a redundant all-text mini graph. The module and its preview gallery remain.
    graph = stage.get("processGraph")
    hint = (stage.get("visualReview") or {}).get("graphPresentation")
    if hint == "omit":
        return None
    if graph and _process_describes_one_result(stage, graph):
        return None
    if not hint and graph and not any(any(key in node for key in
            ("imageIndex", "comparisonIndex", "animationIndex")) for node in graph.get("nodes", [])):
        # Older saved diagrams predate visualReview. Apply the same display-only
        # rule on restoration without changing or discarding their graph data.
        return None
    return graph


def presented_process_steps(stage):
    # An omitted explicit graph must not silently reappear as an unchecked
    # legacy linear chain beneath it. Its previews still reach the gallery.
    if stage.get("processGraph"):
        return []
    steps = stage.get("processSteps", [])
    if (stage.get("visualReview") or {}).get("processStepsPresentation") == "omit" or not steps:
        return []
    nodes = [dict(step, id="step-%d" % index) for index, step in enumerate(steps)]
    graph = {"nodes": nodes, "edges": [{"from": nodes[index]["id"], "to": nodes[index + 1]["id"]}
                                       for index in range(len(nodes) - 1)]}
    return [] if _process_describes_one_result(stage, graph) else steps


def reading_stage_id(bounds, viewport_top, viewport_height, at_bottom=False):
    """Select the module at the reading edge, independent of replay selection."""
    if not bounds:
        return ""
    if at_bottom and bounds[-1][1] < viewport_top + viewport_height:
        return bounds[-1][0]
    anchor = viewport_top + min(32.0, max(0.0, viewport_height) * 0.08)
    for stage_id, top, bottom in bounds:
        if bottom > anchor:
            return stage_id
    return bounds[-1][0]


def displayed_semantic(value):
    """Protocol tags are evidence metadata, not human-readable captions."""
    value = str(value or "").strip()
    return "" if re.fullmatch(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+", value) else value


def concise_caption(preview):
    """Hide legacy presentation boilerplate, never invent a resource meaning.

    Full source text remains in the tooltip and saved document. New Host
    publications require semantic descriptions; this only cleans old diagrams.
    """
    parts = []
    noise = re.compile(r"physical-attachment|无二维纹理预览|保留物理RGBA|业务含义未|"
        r"不是字节差异|不等于.*贡献|按子资源范围显示|not a (?:byte|numerical)|"
        r"no (?:2d|two-dimensional) (?:texture )?preview|physical RGBA", re.I)
    format_name = str(preview.get("format") or "")
    for key in ("description", "semantic", "channelMeaning"):
        value = displayed_semantic(preview.get(key)) if key == "semantic" else str(preview.get(key) or "")
        for line in re.split(r"[\n；;。]+", value):
            line = line.strip()
            if not line or noise.search(line):
                continue
            # Strip metadata-only sentences; leave actual channel explanations.
            metadata = line.replace(format_name, "") if format_name else line
            if re.fullmatch(r"[\s,，:：·×x\d]+", metadata):
                continue
            if line not in parts:
                parts.append(line)
    return "\n".join(parts)


def export_part_paths(path, height, part_height=EXPORT_PART_HEIGHT):
    path = os.path.abspath(path)
    if os.path.splitext(path)[1].lower() != ".png":
        path += ".png"
    count = max(1, int(math.ceil(float(height) / part_height)))
    if count == 1:
        return [path]
    stem = os.path.splitext(path)[0]
    return ["%s-part%03d.png" % (stem, index + 1) for index in range(count)]


def export_segments(height, stage_ends, width=1120):
    """Prefer a single image; split unusually long diagrams at stage boundaries."""
    limit = min(EXPORT_PART_HEIGHT, MAX_EXPORT_PART_PIXELS // width)
    segments, start = [], 0
    while start < height:
        end = min(height, start + limit)
        if end < height:
            candidates = [int(math.ceil(value)) for value in stage_ends if start < value <= end]
            if candidates:
                end = max(candidates)
        segments.append((start, end))
        start = end
    return segments


if HAS_QT:
    class _PreviewFuture(object):
        def __init__(self):
            self.complete = threading.Event()
            self.cancelled, self.value = False, None

        def done(self):
            return self.complete.is_set()

        def result(self):
            return self.value

        def cancel(self):
            self.cancelled = True


    class _PreviewTask(QtCore.QRunnable):
        def __init__(self, operation, path, future):
            super(_PreviewTask, self).__init__()
            self.operation, self.path, self.future = operation, path, future
            self.setAutoDelete(True)

        def run(self):
            try:
                if not self.future.cancelled:
                    self.future.value = self.operation(self.path)
            except Exception:
                self.future.value = None
            finally:
                # Python-owned result only, no cross-thread PySide signal marshal.
                self.future.complete.set()


    class _PreviewExecutor(object):
        def __init__(self):
            self.pool = QtCore.QThreadPool()
            self.pool.setMaxThreadCount(2)

        def submit(self, operation, path):
            future = _PreviewFuture()
            self.pool.start(_PreviewTask(operation, path, future))
            return future

        def shutdown(self, wait=True):
            self.pool.clear()
            if wait and (self.pool.waitForDone(5000) is False or self.pool.activeThreadCount()):
                raise RuntimeError("Frame preview workers did not stop within 5 seconds")


    def _format(source, *values):
        for index, value in enumerate(values, 1):
            source = source.replace("%%%d" % index, str(value))
        return source


    def _mix(base, accent, weight):
        return QtGui.QColor(*[int(a + (b - a) * weight) for a, b in zip(
            (base.red(), base.green(), base.blue()),
            (accent.red(), accent.green(), accent.blue()))])


    def _decode_preview(path):
        # Do not decode original-size textures or block the GUI on disk I/O.
        if not os.path.isfile(path) or os.path.getsize(path) > MAX_IMAGE_BYTES:
            return None
        reader = QtGui.QImageReader(path, b"png")
        size = reader.size()
        if (not size.isValid() or size.width() * size.height() > MAX_IMAGE_PIXELS):
            return None
        image = reader.read()
        # Render-target alpha often stores masks or unrelated data. Treat the
        # evidence as opaque RGB BEFORE any scaling. Decoders may premultiply
        # translucent pixels while scaling, irreversibly removing their RGB.
        if image.isNull():
            return None
        image = image.convertToFormat(QtGui.QImage.Format_RGB32)
        return image.scaled(640, 360, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)


    def _save_export_image(job):
        path, image = job
        temporary = path + "." + uuid.uuid4().hex + ".tmp"
        try:
            if not image.save(temporary, "PNG"):
                raise IOError("PNG encoding failed")
            os.replace(temporary, path)
            return {"path": path}
        except Exception as error:
            return {"error": str(error)}
        finally:
            if os.path.isfile(temporary):
                os.remove(temporary)


    def _mapping_description(preview):
        mapping = preview.get("displayMapping") or {}
        lines = []
        if mapping.get("rangeMode"):
            mode = mapping["rangeMode"]
            if mode in ("raw", "identity", "none", "native"):
                mode = QtCore.QCoreApplication.translate("ReplayLabProjects", "Original values")
            elif mode in ("auto", "auto-range", "min-max", "minmax", "subresource-minmax"):
                mode = QtCore.QCoreApplication.translate("ReplayLabProjects", "Measured range")
            elif mode in ("explicit", "manual", "fixed"):
                mode = QtCore.QCoreApplication.translate("ReplayLabProjects", "Specified range")
            elif mode == "visible-distribution":
                mode = QtCore.QCoreApplication.translate("ReplayLabProjects", "Visible distribution")
            lines.append(_format(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Display range: %1–%2 (%3)"),
                mapping.get("blackPoint", ""), mapping.get("whitePoint", ""), mode))
        if mapping.get("depthInterpretation"):
            interpretation = mapping["depthInterpretation"]
            if interpretation == "raw-resource-component-0":
                interpretation = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Raw depth component (not converted to distance)")
            lines.append(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Depth interpretation: %1").replace("%1", interpretation))
        if any(mapping.get(key) not in (None, "") for key in ("mip", "slice", "sample")):
            lines.append(_format(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "MIP %1 · Slice %2 · Sample %3"),
                mapping.get("mip", ""), mapping.get("slice", ""), mapping.get("sample", "")))
        if mapping.get("attachmentKind"):
            kind = mapping["attachmentKind"]
            if kind in ("rtv", "dsv"):
                kind = kind.upper()  # API identifiers, not translatable labels.
            lines.append(_format(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Attachment: %1 %2"),
                kind, mapping.get("attachmentIndex", "")))
        if mapping.get("attachmentStage"):
            lines.append(QtCore.QCoreApplication.translate("ReplayLabProjects", "Shader stage: %1").replace(
                "%1", mapping["attachmentStage"]))
        return "\n".join(lines)


    def _category_label(category):
        if category == "depth":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Depth")
        if category == "shadow":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Shadows")
        if category == "geometry":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Geometry")
        if category == "lighting":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Lighting")
        if category == "transparency":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Transparency")
        if category == "postprocess":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Post-processing")
        if category == "upscale":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Upscaling")
        if category == "ui":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "UI composition")
        if category == "compute":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Compute")
        if category == "copy":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Copy")
        if category == "present":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Present")
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "Other")


    def _category_color(category, palette):
        # Four related accents: geometry/depth, lighting, post effects, utility.
        # Keep text on the normal palette, tint only borders and small headers.
        base = palette.color(QtGui.QPalette.Highlight)
        hue = base.hue() if base.hue() >= 0 else 205
        shift = 0
        if category in ("geometry", "transparency"):
            shift = -35
        elif category == "lighting":
            shift = -170
        elif category in ("postprocess", "upscale"):
            shift = 65
        color = QtGui.QColor.fromHsv((hue + shift) % 360, min(150, max(95, base.saturation())),
            210 if palette.color(QtGui.QPalette.Base).lightness() < 128 else 145)
        if category in ("copy", "present", "compute", "other", ""):
            color = _mix(palette.color(QtGui.QPalette.Text), base, 0.25)
        return color


    def _technique_status(status):
        if status == "recognized":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Recognized")
        if status == "inferred":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Inferred")
        if status == "not-observed":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Not observed")
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "Unresolved")


    def _group_label(group):
        if group == "channels":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Channel details")
        if group == "inputs":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Inputs and previous results")
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "Render outputs")


    def _navigation_label(stage):
        status = stage.get("navigationStatus")
        if status == "last-draw":
            return (QtCore.QCoreApplication.translate("ReplayLabProjects", "Last effective draw")
                    if stage.get("navigationHasWork") is True else QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Last draw; workload not confirmed"))
        if status == "last-dispatch":
            return (QtCore.QCoreApplication.translate("ReplayLabProjects", "Last effective dispatch")
                    if stage.get("navigationHasWork") is True else QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Last dispatch; workload not confirmed"))
        if status == "last-utility":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "Last copy, clear or presentation action")
        if status == "no-effective-work":
            return QtCore.QCoreApplication.translate("ReplayLabProjects", "No effective draw or dispatch in this stage")
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "Jump target")


    def _quality_gap_text(gap):
        """Stable Host codes are localized here; unknown diagnostics stay intact."""
        code = gap.get("code", "")
        if code == "unrepresented-frame-actions":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Some frame actions are not represented in the diagram.")
        elif code == "incomplete-action-outline":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "The frame action outline is incomplete.")
        elif code == "missing-stage-explanation":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Explain what this stage reads, calculates and contributes to the frame.")
        elif code in ("unresolved-stage-explanation", "unresolved-stage-purpose"):
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Inspect the Shader and its inputs and outputs to explain this stage's purpose.")
        elif code == "missing-texture-explanation":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Explain what each texture stores and how later stages use it.")
        elif code == "missing-stage-inspection":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Inspect this stage's pipeline and resource bindings before completing the analysis.")
        elif code == "stage-inventory-incomplete":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Retry the failed resource binding checks and explain any remaining limitation.")
        elif code == "missing-stage-preview":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Add an output preview from the final draw or dispatch, or explain why no image is available.")
        elif code == "missing-output-attachments":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Add the missing color and depth outputs, including every relevant MRT attachment.")
        elif code == "stage-not-investigated":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Inspect the pipeline, Shader and resources, then record this stage's findings.")
        elif code == "stage-investigation-partial":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Continue investigating this stage's remaining question using relevant evidence.")
        elif code == "missing-investigation-evidence":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Add the EIDs, Shader observations or resource comparisons supporting this explanation.")
        elif code == "missing-technique-review":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Identify the rendering techniques used here and record the supporting observations.")
        elif code == "unresolved-technique":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Inspect the relevant Shader calculation or image comparison to clarify the rendering technique.")
        elif code == "missing-technique-evidence":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Support the technique finding with concrete observations, not only names or colors.")
        elif code == "missing-image-semantic-evidence":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Explain which Shader calculations or resource uses support the texture and channel meanings.")
        elif code == "nonsemantic-stage-title":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Name the rendering function of this stage, such as shadows, lighting or Bloom.")
        elif code == "nonsemantic-texture-title":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Name what this texture stores instead of using its EID or resource ID as the title.")
        elif code == "unresolved-texture-meaning":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Follow this texture's producers and consumers to explain its role in the calculation.")
        elif code == "missing-texture-channel-meaning":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Explain the meaningful channel groups using the Shader calculations that read or write them.")
        elif code == "missing-final-present-preview":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Show the final composed frame after the UI is blended, not only the UI layer.")
        elif code == "missing-module-data-flow":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Missing module data flow")
        elif code == "invalid-process-data-flow":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "A data-flow connection contradicts the captured execution or parallel outputs")
        elif code == "unverified-process-data-flow":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "A data-flow connection needs calculation and resource-binding evidence")
        elif code == "stage-review-not-refreshed":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Stage review needs to be refreshed")
        elif code == "missing-stage-algorithm":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Missing calculation steps")
        elif code == "incomplete-stage-algorithm":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Incomplete calculation explanation")
        elif code == "unverified-algorithm-binding":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Unverified calculation inputs or outputs")
        elif code == "missing-algorithm-integration":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Missing output composition")
        elif code == "missing-algorithm-step-evidence":
            message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Missing Shader calculation evidence")
        else:
            message = _format(QtCore.QCoreApplication.translate("ReplayLabProjects", "Diagnostic %1: %2 %3"),
                code, gap.get("message", ""), gap.get("nextAction", ""))
        if not gap.get("stageId"):
            return message
        return _format(QtCore.QCoreApplication.translate("ReplayLabProjects", "Stage %1: %2"),
                       gap.get("stageId", ""), message)


    def _draw_contained(painter, image_rect, pixmap, palette, failed=False, deferred=False):
        painter.fillRect(image_rect, palette.color(QtGui.QPalette.Window))
        if pixmap is not None:
            size = QtCore.QSizeF(pixmap.size())
            size.scale(image_rect.size(), QtCore.Qt.KeepAspectRatio)
            rect = QtCore.QRectF(QtCore.QPointF(0, 0), size)
            rect.moveCenter(image_rect.center())
            painter.drawPixmap(rect, pixmap, QtCore.QRectF(pixmap.rect()))
        else:
            painter.setPen(palette.color(QtGui.QPalette.Text))
            message = (QtCore.QCoreApplication.translate("ReplayLabProjects", "Zoom in to load this preview.") if deferred else
                       QtCore.QCoreApplication.translate("ReplayLabProjects", "Preview unavailable")
                       if failed else QtCore.QCoreApplication.translate("ReplayLabProjects", "Loading preview…"))
            painter.drawText(image_rect, QtCore.Qt.AlignCenter, message)


    def _preview_explanation(preview):
        """Keep source texture meaning reachable after a media card replaces it."""
        return "\n".join(value for value in (_mapping_description(preview), preview.get("description", ""),
            displayed_semantic(preview.get("semantic")), preview.get("channelMeaning", ""), preview.get("evidence", "")) if value)


    class _PlainText(QtWidgets.QGraphicsTextItem):
        def __init__(self, text, width, palette, parent, bold=False, small=False, muted=False):
            super(_PlainText, self).__init__(parent)
            self.setPlainText(str(text))
            self.setTextWidth(width)
            # Neutral typography separates hierarchy from the restrained category
            # accents. Palette-relative tones remain readable in both themes.
            foreground, background = palette.color(QtGui.QPalette.Text), palette.color(QtGui.QPalette.Base)
            dark = background.lightnessF() < 0.5
            title_color = _mix(foreground, QtGui.QColor("#ffffff" if dark else "#000000"), 0.75)
            self.setDefaultTextColor(title_color if bold else
                _mix(foreground, background, 0.24 if muted else 0.10))
            font = QtGui.QFont(QtWidgets.QApplication.instance().font())
            font.setBold(bold)
            # Cards are wider than many dock areas and are fitted to width.
            # Start with readable diagram typography rather than Qt's tiny
            # default QGraphicsTextItem font, especially for Chinese text.
            font.setPointSizeF(max(11.0, font.pointSizeF()) +
                              (1.0 if bold and small else 4.0 if bold else -1.0 if small else 0.0))
            self.setFont(font)
            self.document().setDocumentMargin(0)
            self.setAcceptedMouseButtons(QtCore.Qt.NoButton)


    class _LinkText(_PlainText):
        """Only explicit underlined text owns EID navigation hit targets."""
        def __init__(self, text, width, palette, parent, activate, bold=False, small=False, marker=True):
            super(_LinkText, self).__init__(text, width, palette, parent, bold, small)
            self.activate, self._press_position = activate, None
            font = self.font()
            font.setUnderline(True)
            self.setFont(font)
            self.setCursor(QtCore.Qt.PointingHandCursor)
            self.setAcceptHoverEvents(True)
            self.setFlag(QtWidgets.QGraphicsItem.ItemIsFocusable, True)
            self.setAcceptedMouseButtons(QtCore.Qt.LeftButton)

        def _contains_text(self, point):
            return self.document().documentLayout().hitTest(point, QtCore.Qt.ExactHit) >= 0

        def hoverMoveEvent(self, event):
            self.setCursor(QtCore.Qt.PointingHandCursor if self._contains_text(event.pos()) else QtCore.Qt.ArrowCursor)
            super(_LinkText, self).hoverMoveEvent(event)

        def mousePressEvent(self, event):
            if not self._contains_text(event.pos()):
                self._press_position = None
                event.ignore()
                return
            self._press_position = event.screenPos()
            self.setFocus()
            event.accept()

        def mouseReleaseEvent(self, event):
            origin, self._press_position = self._press_position, None
            if (origin is not None and self._contains_text(event.pos()) and
                    (event.screenPos() - origin).manhattanLength() < QtWidgets.QApplication.startDragDistance()):
                self.activate()
            event.accept()

        def keyPressEvent(self, event):
            if event.key() in (QtCore.Qt.Key_Enter, QtCore.Qt.Key_Return, QtCore.Qt.Key_Space):
                self.activate()
                event.accept()
                return
            super(_LinkText, self).keyPressEvent(event)


    class _PreviewItem(QtWidgets.QGraphicsObject):
        def __init__(self, preview, width, palette, activate, parent,
                     channels=None, expanded=False, toggle=None, channel_detail=False, request_channels=False,
                     volume_toggle=None):
            super(_PreviewItem, self).__init__(parent)
            self.preview, self.width = preview, width
            self.palette, self.activate = palette, activate
            self.pixmap, self.failed, self.deferred = None, False, False
            self.setAcceptedMouseButtons(QtCore.Qt.NoButton)
            self.title = _LinkText(channel_detail_title(preview) if channel_detail else preview["title"],
                width - 16, palette, self,
                lambda: activate(preview_navigation(preview)), bold=True)
            self.title.setPos(8, 8)
            self.title.setToolTip(QtCore.QCoreApplication.translate("ReplayLabProjects", "Go to EID %1").replace(
                "%1", str(preview["eventId"])))
            y = 12 + self.title.boundingRect().height()
            dimensions = ("%d × %d" % (preview["width"], preview["height"]) if
                          preview.get("width") and preview.get("height") else "")
            self.metadata = _PlainText(" · ".join(value for value in (
                "EID %d" % preview["eventId"], preview["channel"].upper().replace("RGBA", "RGB"),
                dimensions, preview["format"]) if value), width - 16, palette, self, small=True, muted=True)
            self.metadata.setPos(8, y)
            y += self.metadata.boundingRect().height() + 6
            self.image_rect = QtCore.QRectF(8, y, width - 16, 180)
            y += 188
            self.caption = _PlainText(channel_detail_caption(preview) if channel_detail else concise_caption(preview),
                width - 16, palette, self, small=channel_detail)
            self.caption.setPos(8, y)
            y += self.caption.boundingRect().height() + 10
            self.channel_previews, self.channel_toggle = [], None
            if volume_toggle or channels or request_channels:
                if volume_toggle:
                    label = QtCore.QCoreApplication.translate("ReplayLabProjects", "3D texture slices")
                else:
                    label = (_format(QtCore.QCoreApplication.translate("ReplayLabProjects", "Channel details (%1)"), len(channels))
                             if channels else QtCore.QCoreApplication.translate("ReplayLabProjects", "Channel details"))
                self.channel_toggle = _ChannelButton(label, width - 16,
                    False, palette, volume_toggle or toggle, self)
                self.channel_toggle.setPos(8, y)
                y += self.channel_toggle.height + 8
                # Channels live in a separate popup; they never enlarge the stage.
            self.height = y
            self.setToolTip(_preview_explanation(preview))

        def boundingRect(self):
            return QtCore.QRectF(0, 0, self.width, self.height)

        def paint(self, painter, option, widget=None):
            painter.setPen(QtGui.QPen(self.palette.color(QtGui.QPalette.Mid), 0.8))
            painter.setBrush(self.palette.color(QtGui.QPalette.Base))
            painter.drawRoundedRect(self.boundingRect(), 7, 7)
            _draw_contained(painter, self.image_rect, self.pixmap, self.palette, self.failed, self.deferred)

    class _GroupToggle(QtWidgets.QGraphicsObject):
        def __init__(self, label, width, expanded, palette, toggle, parent):
            super(_GroupToggle, self).__init__(parent)
            self.width, self.expanded, self.palette, self.toggle = width, expanded, palette, toggle
            self.setCursor(QtCore.Qt.PointingHandCursor)
            self.setFlag(QtWidgets.QGraphicsItem.ItemIsFocusable, True)
            self._press_position = None
            title = _PlainText(label, width - 36, palette, self, bold=True, small=True)
            title.setPos(28, 8)
            self.height = max(36, title.boundingRect().height() + 16)

        def boundingRect(self):
            return QtCore.QRectF(0, 0, self.width, self.height)

        def paint(self, painter, option, widget=None):
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(_mix(self.palette.color(QtGui.QPalette.Base),
                                 self.palette.color(QtGui.QPalette.Highlight), 0.12))
            painter.drawRoundedRect(self.boundingRect(), 5, 5)
            painter.setBrush(self.palette.color(QtGui.QPalette.Text))
            y = self.height * 0.5
            points = ([QtCore.QPointF(10, y - 3), QtCore.QPointF(20, y - 3), QtCore.QPointF(15, y + 4)]
                      if self.expanded else [QtCore.QPointF(12, y - 5), QtCore.QPointF(12, y + 5),
                                             QtCore.QPointF(19, y)])
            painter.drawPolygon(QtGui.QPolygonF(points))

        def mousePressEvent(self, event):
            self._press_position = event.screenPos() if event.button() == QtCore.Qt.LeftButton else None
            self.setFocus()
            event.accept()

        def mouseReleaseEvent(self, event):
            origin, self._press_position = self._press_position, None
            if (event.button() == QtCore.Qt.LeftButton and origin is not None and
                    (event.screenPos() - origin).manhattanLength() < QtWidgets.QApplication.startDragDistance()):
                self.toggle()
            event.accept()

        def keyPressEvent(self, event):
            if event.key() in (QtCore.Qt.Key_Enter, QtCore.Qt.Key_Return, QtCore.Qt.Key_Space):
                self.toggle()
                event.accept()
                return
            super(_GroupToggle, self).keyPressEvent(event)


    class _ChannelButton(_GroupToggle):
        """A popup action, with a channel-grid icon rather than an expand arrow."""
        def paint(self, painter, option, widget=None):
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(_mix(self.palette.color(QtGui.QPalette.Base),
                                 self.palette.color(QtGui.QPalette.Highlight), 0.12))
            painter.drawRoundedRect(self.boundingRect(), 5, 5)
            painter.setBrush(self.palette.color(QtGui.QPalette.Text))
            for x in (10, 17):
                for y in (self.height * 0.5 - 6, self.height * 0.5 + 1):
                    painter.drawRect(QtCore.QRectF(x, y, 5, 5))


    class _ComparisonImage(object):
        """A shared lazy-thumbnail receiver; no independent visible Qt item."""
        def __init__(self, owner, preview):
            self.owner, self.preview = owner, preview
            self.pixmap, self.failed, self.deferred = None, False, False

        def sceneBoundingRect(self):
            return self.owner.mapRectToScene(self.owner.image_rect)

        def update(self):
            self.owner.update()


    class _ComparisonItem(QtWidgets.QGraphicsObject):
        def __init__(self, comparison, images, width, palette, activate, parent,
                     ratio=0.5, ratio_changed=None):
            super(_ComparisonItem, self).__init__(parent)
            self.width, self.palette, self.activate = width, palette, activate
            self.ratio, self.ratio_changed = min(0.95, max(0.05, ratio)), ratio_changed
            self._press_position, self._dragging = None, False
            self.setFlag(QtWidgets.QGraphicsItem.ItemIsFocusable, True)
            self.setAcceptHoverEvents(True)
            left, right = images[comparison["leftImageIndex"]], images[comparison["rightImageIndex"]]
            self.previews = [_ComparisonImage(self, left), _ComparisonImage(self, right)]
            title = _PlainText(comparison["label"] or QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Before / after comparison"), width - 20, palette, self, bold=True)
            title.setPos(10, 8)
            y = title.boundingRect().height() + 14
            relation = "\n".join(value for value in (comparison.get("relationship"),
                                                      comparison.get("description")) if value)
            if relation:
                description = _PlainText(relation, width - 20, palette, self)
                description.setPos(10, y)
                y += description.boundingRect().height() + 8
            self.image_links = []
            for side, preview in enumerate((left, right)):
                label = _LinkText(_format(QtCore.QCoreApplication.translate("ReplayLabProjects", "%1 (EID %2)"),
                    preview["title"], preview["eventId"]), (width - 32) / 2, palette, self,
                    lambda preview=preview: activate(preview_navigation(preview)), small=True)
                label.setPos(10 + side * (width - 12) / 2, y)
                label.setToolTip(_preview_explanation(preview))
                self.image_links.append(label)
            y += max(label.boundingRect().height() for label in self.image_links) + 8
            self.image_rect = QtCore.QRectF(10, y, width - 20, 320)
            y += 328
            # Their standalone cards are omitted, so each side retains its own
            # texture explanation here instead of losing that information.
            self.image_captions = []
            for side, preview in enumerate((left, right)):
                caption = _PlainText(concise_caption(preview), (width - 32) / 2, palette, self)
                caption.setPos(10 + side * (width - 12) / 2, y)
                caption.setToolTip(_preview_explanation(preview))
                self.image_captions.append(caption)
            y += max(caption.boundingRect().height() for caption in self.image_captions) + 8
            note = QtCore.QCoreApplication.translate("ReplayLabProjects", "Drag the divider to compare.")
            if not comparison.get("visualComparable", True):
                self.setToolTip(QtCore.QCoreApplication.translate("ReplayLabProjects",
                    "The previews use different sizes, channels or display ranges; interpret the visible difference with care."))
            foot = _PlainText(note, width - 20, palette, self, small=True, muted=True)
            foot.setPos(10, y)
            self.height = y + foot.boundingRect().height() + 12

        def boundingRect(self):
            return QtCore.QRectF(0, 0, self.width, self.height)

        def _divider_x(self):
            return self.image_rect.left() + self.image_rect.width() * self.ratio

        def paint(self, painter, option, widget=None):
            painter.setPen(QtGui.QPen(self.palette.color(QtGui.QPalette.Mid), 0.8))
            painter.setBrush(self.palette.color(QtGui.QPalette.Base))
            painter.drawRoundedRect(self.boundingRect(), 7, 7)
            x = self._divider_x()
            for index, preview in enumerate(self.previews):
                clip = QtCore.QRectF(self.image_rect)
                if index == 0:
                    clip.setRight(x)
                else:
                    clip.setLeft(x)
                painter.save()
                painter.setClipRect(clip, QtCore.Qt.IntersectClip)
                _draw_contained(painter, self.image_rect, preview.pixmap, self.palette, preview.failed, preview.deferred)
                painter.restore()
            color = _mix(self.palette.color(QtGui.QPalette.Text), self.palette.color(QtGui.QPalette.Highlight), 0.15)
            painter.setPen(QtGui.QPen(color, 3))
            painter.drawLine(QtCore.QPointF(x, self.image_rect.top()), QtCore.QPointF(x, self.image_rect.bottom()))
            painter.setBrush(self.palette.color(QtGui.QPalette.Highlight))
            painter.drawEllipse(QtCore.QPointF(x, self.image_rect.center().y()), 11, 17)

        def hoverMoveEvent(self, event):
            inside = self.image_rect.contains(event.pos())
            self.setCursor(QtCore.Qt.SplitHCursor if inside and abs(event.pos().x() - self._divider_x()) < 15
                           else QtCore.Qt.ArrowCursor)
            super(_ComparisonItem, self).hoverMoveEvent(event)

        def mousePressEvent(self, event):
            self._press_position = event.screenPos() if event.button() == QtCore.Qt.LeftButton else None
            self._dragging = bool(self._press_position is not None and self.image_rect.contains(event.pos()) and
                                  abs(event.pos().x() - self._divider_x()) < 15)
            self.setFocus()
            event.accept()

        def mouseMoveEvent(self, event):
            if self._dragging:
                self.ratio = min(0.95, max(0.05,
                    (event.pos().x() - self.image_rect.left()) / self.image_rect.width()))
                if callable(self.ratio_changed):
                    self.ratio_changed(self.ratio)
                self.update()
            event.accept()

        def mouseReleaseEvent(self, event):
            self._press_position, self._dragging = None, False
            event.accept()

        def keyPressEvent(self, event):
            if event.key() in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Right):
                self.ratio = min(0.95, max(0.05, self.ratio + (-0.05 if event.key() == QtCore.Qt.Key_Left else 0.05)))
                if callable(self.ratio_changed):
                    self.ratio_changed(self.ratio)
                self.update()
                event.accept()
                return
            super(_ComparisonItem, self).keyPressEvent(event)


    class _AnimationImage(_ComparisonImage):
        def __init__(self, owner, preview, index):
            super(_AnimationImage, self).__init__(owner, preview)
            self.index = index

        @property
        def active(self):
            return self.index in (self.owner.frame, (self.owner.frame + 1) % len(self.owner.previews))


    class _AnimationItem(QtWidgets.QGraphicsObject):
        """Bounded PNG sequence: background decode, visible-only playback."""
        def __init__(self, animation, images, width, palette, activate, parent, state=None):
            super(_AnimationItem, self).__init__(parent)
            self.width, self.palette, self.activate = width, palette, activate
            self.state = state if state is not None else {}
            self.frame = self.state.get("frame", 0) % len(animation["imageIndices"])
            self.playing, self.last_advance = self.state.get("playing", True), time.monotonic()
            self.interval = animation["frameDurationMs"] / 1000.0
            self.previews = [_AnimationImage(self, images[index], position)
                             for position, index in enumerate(animation["imageIndices"])]
            self.setAcceptedMouseButtons(QtCore.Qt.NoButton)
            title = _PlainText(animation["label"] or QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Rendering sequence"), width - 20, palette, self, bold=True)
            title.setPos(10, 8)
            y = title.boundingRect().height() + 16
            if animation["description"]:
                description = _PlainText(animation["description"], width - 20, palette, self)
                description.setPos(10, y)
                y += description.boundingRect().height() + 8
            self.image_link = _LinkText("", width - 180, palette, self,
                lambda: activate(preview_navigation(self.previews[self.frame].preview)), small=True)
            self.image_link.setPos(10, y)
            self.control = _LinkText("", 130, palette, self, self._toggle, small=True, marker=False)
            self.control.setPos(width - 140, y)
            self._refresh_labels()
            y += max(self.image_link.boundingRect().height(), self.control.boundingRect().height()) + 8
            self.image_rect = QtCore.QRectF(10, y, width - 20, 320)
            self.caption = _PlainText("", width - 20, palette, self)
            self.caption.setPos(10, y + 328)
            # Reserve the tallest frame caption once. Playback must not move
            # the image, following stages, or the user's comparison controls.
            caption_height = 0
            for receiver in self.previews:
                self.caption.setPlainText(concise_caption(receiver.preview))
                caption_height = max(caption_height, self.caption.boundingRect().height())
            self.height = y + 340 + caption_height
            self._refresh_labels()

        def _refresh_labels(self):
            preview = self.previews[self.frame].preview
            self.image_link.setPlainText(_format(QtCore.QCoreApplication.translate("ReplayLabProjects",
                "%1 · EID %2 · %3/%4"), preview["title"], preview["eventId"], self.frame + 1, len(self.previews)))
            explanation = _preview_explanation(preview)
            self.image_link.setToolTip(explanation)
            if hasattr(self, "caption"):
                self.caption.setPlainText(concise_caption(preview))
                self.caption.setToolTip(explanation)
            self.control.setPlainText(QtCore.QCoreApplication.translate("ReplayLabProjects", "Pause animation")
                if self.playing else QtCore.QCoreApplication.translate("ReplayLabProjects", "Play animation"))

        def _toggle(self):
            self.playing = not self.playing
            self.state["playing"] = self.playing
            self.last_advance = time.monotonic()
            self._refresh_labels()

        def advance(self, now, visible):
            if not visible or not self.playing:
                self.last_advance = now
                return False
            if now - self.last_advance < self.interval:
                return False
            next_frame = (self.frame + 1) % len(self.previews)
            next_image = self.previews[next_frame]
            if next_image.pixmap is None and not next_image.failed:
                return False
            self.frame, self.last_advance = next_frame, now
            self.state["frame"] = self.frame
            self._refresh_labels()
            self.update()
            return True

        def boundingRect(self):
            return QtCore.QRectF(0, 0, self.width, self.height)

        def paint(self, painter, option, widget=None):
            painter.setPen(QtGui.QPen(self.palette.color(QtGui.QPalette.Mid), 0.8))
            painter.setBrush(self.palette.color(QtGui.QPalette.Base))
            painter.drawRoundedRect(self.boundingRect(), 7, 7)
            preview = self.previews[self.frame]
            _draw_contained(painter, self.image_rect, preview.pixmap, self.palette, preview.failed, preview.deferred)


    class _StageItem(QtWidgets.QGraphicsObject):
        WIDTH = 1080.0

        def __init__(self, stage, number, palette, activate, expanded_groups=None,
                     toggle_group=None, comparison_ratios=None, animation_states=None, request_channels=False):
            super(_StageItem, self).__init__()
            self.stage, self.number, self.palette, self.activate = stage, number, palette, activate
            self.setData(FLOW_ATTACHMENT_ROLE, {'stageId': stage['id']})
            self.previews, self.comparisons, self.animations, self.group_toggles = [], [], [], {}
            self._expanded_groups, self._toggle_group = expanded_groups or set(), toggle_group
            self._can_request_channels = request_channels
            self._channel_groups, self._unassigned_channels = texture_channel_groups(stage["images"])
            self._image_indices = {id(image): index for index, image in enumerate(stage["images"])}
            self._channel_parent_indices, canonical = {}, {}
            for index, image in enumerate(stage["images"]):
                if image.get("group", "outputs") in ("outputs", "inputs"):
                    self._channel_parent_indices[index] = canonical.setdefault(preview_card_identity(image), index)
            channel_groups = {}
            for index, channels in self._channel_groups.items():
                grouped = channel_groups.setdefault(self._channel_parent_indices[index], [])
                seen = {preview_card_identity(image) for image in grouped}
                for image in channels:
                    if preview_card_identity(image) not in seen:
                        grouped.append(image)
                        seen.add(preview_card_identity(image))
                grouped.sort(key=lambda image: {"rgb": -1, "rgba": -1, "r": 0, "g": 1, "b": 2, "a": 3}.get(
                    str(image.get("channel") or "").lower(), 4))
            self._channel_groups = channel_groups
            self._accent = _category_color(stage.get("category"), palette)
            self.setAcceptedMouseButtons(QtCore.Qt.NoButton)
            self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
            self.title = (_LinkText(stage["title"], self.WIDTH - 90, palette, self,
                lambda: activate({"eventId": stage["navigationEventId"]}), bold=True)
                if stage["navigationEventId"] else _PlainText(stage["title"], self.WIDTH - 90, palette, self, bold=True))
            self.title.setPos(56, 16)
            y = max(46, self.title.boundingRect().height() + 22)
            confidence = stage["confidence"]
            if confidence in ("captured", "confirmed"):
                confidence = QtCore.QCoreApplication.translate("ReplayLabProjects", "Confirmed")
            elif confidence in ("inferred", "probable"):
                confidence = QtCore.QCoreApplication.translate("ReplayLabProjects", "Inferred")
            elif confidence in ("unknown", "candidate"):
                confidence = QtCore.QCoreApplication.translate("ReplayLabProjects", "Uncertain")
            meta_text = _format(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "EID %1–%2 · %3 actions · %4"), stage["startEventId"],
                stage["endEventId"], len(stage["eventIds"]), confidence)
            self.metadata = _PlainText(meta_text, self.WIDTH - 40, palette, self, small=True, muted=True)
            self.metadata.setPos(20, y)
            y += self.metadata.boundingRect().height() + 8
            self.summary = _PlainText(concise_caption({"description": stage["summary"]}), self.WIDTH - 40, palette, self)
            self.summary.setPos(20, y)
            self.summary.setToolTip(stage["summary"])
            y += self.summary.boundingRect().height() + 12
            techniques = [technique for technique in stage.get("techniques", [])
                          if technique["name"].strip().casefold() != stage["title"].strip().casefold()]
            if techniques:
                text = QtCore.QCoreApplication.translate("ReplayLabProjects", "Techniques: %1").replace(
                    "%1", " · ".join("%s (%s)" % (technique["name"], _technique_status(technique["status"]))
                                     for technique in techniques))
                row = _PlainText(text, self.WIDTH - 40, palette, self, small=True, muted=True)
                row.setPos(20, y)
                row.setToolTip("\n".join("%s: %s\n%s" % (technique["name"], technique["summary"],
                                                          technique["evidence"]) for technique in techniques))
                y += row.boundingRect().height() + 10
            investigation = stage.get("investigation") or {}
            warnings = []
            if investigation.get("status") in ("partial", "not-investigated"):
                warnings.append(QtCore.QCoreApplication.translate("ReplayLabProjects", "Analysis is incomplete for this stage."))
                warnings.extend(value for value in (investigation.get("unresolvedReason"),
                                                      investigation.get("nextAction")) if value)
            coverage = stage.get("outputCoverage") or {}
            missing_count = max(len(coverage.get("missingResourceIds", [])), len(coverage.get("missingAttachments", [])))
            if missing_count:
                warnings.append(QtCore.QCoreApplication.translate("ReplayLabProjects",
                    "%1 render outputs are still missing from the diagram.").replace("%1", str(missing_count)))
            non_images = coverage.get("nonImageOutputs", [])
            if warnings:
                self.setToolTip("\n".join(warnings))
            if non_images:
                self.setToolTip(self.toolTip() + "\n" + "\n".join(_format(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Shader stage: %1 · %2 %3 · Resource: %4"), item.get("stage", ""),
                    item.get("kind", "").upper(), item.get("index", ""), item.get("resourceId", ""))
                    for item in non_images))
            steps = presented_process_steps(stage)
            used_images, used_comparisons, used_animations = set(), set(), set()
            ratios = comparison_ratios if comparison_ratios is not None else {}
            states = animation_states if animation_states is not None else {}
            if presented_process_graph(stage):
                y = self._add_process_graph(presented_process_graph(stage), y, ratios, states,
                                            used_images, used_comparisons, used_animations)
            elif steps:
                y = self._add_process(steps, y, ratios, states, used_images, used_comparisons, used_animations)
            y = self._add_featured_media(y, ratios, states, used_images, used_comparisons, used_animations)
            galleries = standalone_preview_groups(stage["images"], used_images)
            for group in ("outputs", "inputs", "details", "channels:unassigned"):
                images = self._unassigned_channels if group == "channels:unassigned" else galleries[group]
                if not images:
                    continue
                label = (_format(QtCore.QCoreApplication.translate("ReplayLabProjects", "More textures (%1)"), len(images))
                         if group == "details" else _format(QtCore.QCoreApplication.translate("ReplayLabProjects", "%1 (%2)"),
                            _group_label("channels" if group == "channels:unassigned" else group), len(images)))
                if group == "outputs":
                    y = self._add_grid(images, y)
                else:
                    expanded = group != "channels:unassigned" and (stage["id"], group) in (expanded_groups or set())
                    button_type = _ChannelButton if group == "channels:unassigned" else _GroupToggle
                    toggle = button_type(label, self.WIDTH - 40, expanded, palette,
                        lambda group=group: toggle_group(stage["id"], group), self)
                    toggle.setPos(20, y)
                    self.group_toggles[group] = toggle
                    y += toggle.height + 10
                    if expanded:
                        y = self._add_grid(images, y)
            self.height = y
            if stage["navigationEventId"]:
                self.title.setToolTip(_format(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Click to select EID %1 · %2"),
                    stage["navigationEventId"], _navigation_label(stage)))

        def _image_card(self, index, width, channel_detail=False):
            channel_parent = self._channel_parent_indices.get(index, index)
            key = "channels:%d" % channel_parent
            parent = self.stage["images"][index]
            volume_key = "volume:%d" % index if parent.get("volumePreview") else None
            can_split = channel_details_available(parent)
            item = _PreviewItem(self.stage["images"][index], width, self.palette, self.activate, self,
                self._channel_groups.get(channel_parent) if can_split else [], False,
                lambda key=key: self._toggle_group(self.stage["id"], key), channel_detail=channel_detail,
                request_channels=can_split and self._can_request_channels and bool(parent.get("resourceId")),
                volume_toggle=(lambda key=volume_key: self._toggle_group(self.stage["id"], key)) if volume_key else None)
            if item.channel_toggle is not None:
                self.group_toggles[volume_key or key] = item.channel_toggle
            item.setData(FLOW_ATTACHMENT_ROLE, {'stageId': self.stage['id'], 'imageIndex': index})
            return item

        def _add_featured_media(self, y, ratios, states, used_images, used_comparisons, used_animations):
            media = [("comparison", index) for index in range(len(self.stage.get("comparisons", [])))
                     if index not in used_comparisons]
            media += [("animation", index) for index in range(len(self.stage.get("animations", [])))
                      if index not in used_animations]
            columns = min(2, len(media))
            if not columns:
                return y
            width = (self.WIDTH - 40 - 12 * (columns - 1)) / columns
            for start in range(0, len(media), columns):
                row, referenced = [], []
                for column, (kind, index) in enumerate(media[start:start + columns]):
                    key = (self.stage["id"], index)
                    if kind == "comparison":
                        data = self.stage["comparisons"][index]
                        item = _ComparisonItem(data, self.stage["images"], width, self.palette,
                            self.activate, self, ratios.get(key, 0.5),
                            lambda ratio, key=key: ratios.__setitem__(key, ratio))
                        self.comparisons.append(item)
                        referenced.extend((data["leftImageIndex"], data["rightImageIndex"]))
                    else:
                        data = self.stage["animations"][index]
                        item = _AnimationItem(data, self.stage["images"], width, self.palette, self.activate,
                                              self, states.setdefault(key, {}))
                        self.animations.append(item)
                        referenced.extend(data["imageIndices"])
                    item.setPos(20 + column * (width + 12), y)
                    item.setData(FLOW_ATTACHMENT_ROLE, {'stageId': self.stage['id'], kind + 'Index': index})
                    self.previews.extend(item.previews)
                    row.append(item)
                used_images.update(referenced)
                y += max(item.height for item in row) + 12
                y = self._add_media_channels(referenced, y)
            return y

        def _add_media_channels(self, indices, y):
            # Comparison/animation sources no longer get duplicate standalone
            # cards. Keep their channel access beside the explicit media instead.
            for index in indices:
                index = self._channel_parent_indices.get(index, index)
                if self.stage["images"][index].get("volumePreview"):
                    key = "volume:%d" % index
                    if key not in self.group_toggles:
                        toggle = _ChannelButton(_format(QtCore.QCoreApplication.translate(
                            "ReplayLabProjects", "%1 · 3D texture slices"), self.stage["images"][index]["title"]),
                            self.WIDTH - 40, False, self.palette,
                            lambda key=key: self._toggle_group(self.stage["id"], key), self)
                        toggle.setPos(20, y)
                        self.group_toggles[key] = toggle
                        y += toggle.height + 10
                    continue
                key = "channels:%d" % index
                channels = self._channel_groups.get(index)
                if (not channel_details_available(self.stage["images"][index]) or
                        (not channels and not (self._can_request_channels and self.stage["images"][index].get("resourceId"))) or
                        key in self.group_toggles):
                    continue
                toggle = _ChannelButton(_format(QtCore.QCoreApplication.translate("ReplayLabProjects",
                    "%1 · Channel details"), self.stage["images"][index]["title"]),
                    self.WIDTH - 40, False, self.palette,
                    lambda key=key: self._toggle_group(self.stage["id"], key), self)
                toggle.setPos(20, y)
                self.group_toggles[key] = toggle
                y += toggle.height + 10
            return y

        def _add_process(self, steps, y, ratios, states, used_images, used_comparisons, used_animations):
            columns = min(3, len(steps))
            gap, width = 30, (self.WIDTH - 40 - 30 * (columns - 1)) / columns
            color = _mix(self.palette.color(QtGui.QPalette.Text), self.palette.color(QtGui.QPalette.Highlight), 0.15)
            self.process_items = []
            for start in range(0, len(steps), columns):
                row, referenced = [], []
                for column, step in enumerate(steps[start:start + columns]):
                    selection = {'stageId': self.stage['id'], 'processStepIndex': start + column}
                    x = 20 + column * (width + gap)
                    label = _PlainText(step["label"], width, self.palette, self, bold=True)
                    label.setData(FLOW_ATTACHMENT_ROLE, selection)
                    label.setPos(x, y)
                    media_y = y + label.boundingRect().height() + 8
                    if step["description"]:
                        description = _PlainText(step["description"], width, self.palette, self)
                        description.setData(FLOW_ATTACHMENT_ROLE, selection)
                        description.setPos(x, media_y)
                        media_y += description.boundingRect().height() + 8
                    if "imageIndex" in step:
                        index = step["imageIndex"]
                        item = self._image_card(index, width)
                        self.previews.append(item)
                        used_images.add(index)
                        referenced.append(index)
                    elif "comparisonIndex" in step:
                        index = step["comparisonIndex"]
                        key = (self.stage["id"], index)
                        comparison = self.stage["comparisons"][index]
                        item = _ComparisonItem(comparison, self.stage["images"], width, self.palette, self.activate,
                            self, ratios.get(key, 0.5), lambda ratio, key=key: ratios.__setitem__(key, ratio))
                        self.comparisons.append(item)
                        self.previews.extend(item.previews)
                        used_comparisons.add(index)
                        used_images.update((comparison["leftImageIndex"], comparison["rightImageIndex"]))
                        referenced.extend((comparison["leftImageIndex"], comparison["rightImageIndex"]))
                    else:
                        index = step["animationIndex"]
                        animation = self.stage["animations"][index]
                        item = _AnimationItem(animation, self.stage["images"], width, self.palette, self.activate,
                            self, states.setdefault((self.stage["id"], index), {}))
                        self.animations.append(item)
                        self.previews.extend(item.previews)
                        used_animations.add(index)
                        used_images.update(animation["imageIndices"])
                        referenced.extend(animation["imageIndices"])
                    item.setPos(x, media_y)
                    item.setData(FLOW_ATTACHMENT_ROLE, dict(selection, **{key: step[key] for key in
                        ('imageIndex', 'comparisonIndex', 'animationIndex') if key in step}))
                    self.process_items.append(item)
                    row.append(item)
                    if column:
                        mid = media_y + item.image_rect.center().y()
                        line = QtWidgets.QGraphicsLineItem(x - gap + 4, mid, x - 7, mid, self)
                        line.setPen(QtGui.QPen(color, 2.5))
                        arrow = QtWidgets.QGraphicsPolygonItem(QtGui.QPolygonF([
                            QtCore.QPointF(x - 11, mid - 5), QtCore.QPointF(x - 11, mid + 5),
                            QtCore.QPointF(x - 3, mid)]), self)
                        arrow.setPen(QtGui.QPen(color))
                        arrow.setBrush(QtGui.QBrush(color))
                y = max(item.y() + item.height for item in row) + 24
                y = self._add_media_channels(referenced, y)
            return y

        def _add_process_graph(self, graph, y, ratios, states, used_images, used_comparisons, used_animations):
            columns = min(3, len(process_graph_layers(graph)))
            width = (self.WIDTH - 40 - 60 * (columns - 1)) / columns
            self.graph_nodes, self.graph_edges = {}, []
            heights, referenced = {}, []
            for node in graph["nodes"]:
                group = QtWidgets.QGraphicsRectItem(self)
                group.setData(FLOW_ATTACHMENT_ROLE, {'stageId': self.stage['id'], 'nodeId': node['id']})
                group.setAcceptedMouseButtons(QtCore.Qt.NoButton)
                group.setPen(QtGui.QPen(QtCore.Qt.NoPen))
                group.setBrush(QtGui.QBrush(QtCore.Qt.NoBrush))
                event = node.get("eventId")
                if "imageIndex" in node:
                    preview = self.stage["images"][node["imageIndex"]]
                    event = preview["eventId"]
                    activate = lambda preview=preview: self.activate(preview_navigation(preview))
                else:
                    activate = lambda event=event: self.activate({"eventId": event})
                title = (_LinkText(node["label"], width - 16, self.palette, group, activate, bold=True)
                         if event else _PlainText(node["label"], width - 16, self.palette, group, bold=True))
                title.setPos(8, 8)
                height = title.boundingRect().height() + 16
                if node["description"]:
                    description = _PlainText(node["description"], width - 16, self.palette, group)
                    description.setPos(8, height)
                    height += description.boundingRect().height() + 8
                media = None
                if "imageIndex" in node:
                    index = node["imageIndex"]
                    media = self._image_card(index, width)
                    self.previews.append(media)
                    used_images.add(index)
                    referenced.append(index)
                elif "comparisonIndex" in node:
                    index = node["comparisonIndex"]
                    data = self.stage["comparisons"][index]
                    key = (self.stage["id"], index)
                    media = _ComparisonItem(data, self.stage["images"], width, self.palette, self.activate,
                        group, ratios.get(key, 0.5), lambda ratio, key=key: ratios.__setitem__(key, ratio))
                    self.comparisons.append(media)
                    self.previews.extend(media.previews)
                    used_comparisons.add(index)
                    referenced.extend((data["leftImageIndex"], data["rightImageIndex"]))
                    used_images.update((data["leftImageIndex"], data["rightImageIndex"]))
                elif "animationIndex" in node:
                    index = node["animationIndex"]
                    data = self.stage["animations"][index]
                    media = _AnimationItem(data, self.stage["images"], width, self.palette, self.activate,
                        group, states.setdefault((self.stage["id"], index), {}))
                    self.animations.append(media)
                    self.previews.extend(media.previews)
                    used_animations.add(index)
                    referenced.extend(data["imageIndices"])
                    used_images.update(data["imageIndices"])
                if media is not None:
                    media.setParentItem(group)
                    media.setPos(0, height)
                    height += media.height
                else:
                    group.setPen(QtGui.QPen(self.palette.color(QtGui.QPalette.Mid), 1))
                    group.setBrush(QtGui.QBrush(self.palette.color(QtGui.QPalette.Base)))
                heights[node["id"]] = max(72, height)
                group.setRect(0, 0, width, heights[node["id"]])
                self.graph_nodes[node["id"]] = group
            positions, bands, height = process_graph_layout(graph, heights, self.WIDTH - 40)
            for key, position in positions.items():
                self.graph_nodes[key].setPos(20 + position["x"], y + position["y"])
            color = _mix(self.palette.color(QtGui.QPalette.Text), self.palette.color(QtGui.QPalette.Highlight), 0.15)
            for edge in graph["edges"]:
                source, target = positions[edge["from"]], positions[edge["to"]]
                left = QtCore.QPointF(20 + source["x"] + source["width"], y + source["y"] + source["height"] * 0.5)
                right = QtCore.QPointF(20 + target["x"], y + target["y"] + target["height"] * 0.5)
                path = QtGui.QPainterPath(left)
                if target["band"] != source["band"]:
                    rail = y + bands[source["band"]][1] + 24
                    for point in (QtCore.QPointF(self.WIDTH - 10, left.y()), QtCore.QPointF(self.WIDTH - 10, rail),
                                  QtCore.QPointF(10, rail), QtCore.QPointF(10, right.y()), right):
                        path.lineTo(point)
                    label_x, label_y, label_width = 24, rail + 4, self.WIDTH - 48
                elif target["layer"] > source["layer"] + 1:
                    rail = y + bands[source["band"]][0] + 12
                    for point in (QtCore.QPointF(left.x() + 12, left.y()), QtCore.QPointF(left.x() + 12, rail),
                                  QtCore.QPointF(right.x() - 12, rail), QtCore.QPointF(right.x() - 12, right.y()), right):
                        path.lineTo(point)
                    label_x, label_y, label_width = left.x() + 16, rail + 3, right.x() - left.x() - 32
                else:
                    middle = (left.x() + right.x()) * 0.5
                    path.cubicTo(QtCore.QPointF(middle, left.y()), QtCore.QPointF(middle, right.y()), right)
                    label_x, label_y, label_width = left.x() + 3, min(left.y(), right.y()) - 36, 54
                line = QtWidgets.QGraphicsPathItem(path, self)
                line.setPen(QtGui.QPen(color, 2.5))
                line.setAcceptedMouseButtons(QtCore.Qt.NoButton)
                line.setToolTip(edge["label"])
                arrow = QtWidgets.QGraphicsPolygonItem(QtGui.QPolygonF([
                    right, right + QtCore.QPointF(-8, -5), right + QtCore.QPointF(-8, 5)]), self)
                arrow.setPen(QtGui.QPen(color))
                arrow.setBrush(QtGui.QBrush(color))
                arrow.setAcceptedMouseButtons(QtCore.Qt.NoButton)
                if edge["label"]:
                    label = _PlainText(edge["label"], max(40, label_width), self.palette, self, small=True)
                    # Connections are compact annotations, not another prose
                    # panel. Keep long labels out of neighboring cards while
                    # preserving their full supplied text on hover.
                    metrics = QtGui.QFontMetricsF(label.font())
                    label.setPlainText(metrics.elidedText(edge["label"].replace("\n", " "),
                                                         QtCore.Qt.ElideRight, max(40, label_width)))
                    label.setToolTip(edge["label"])
                    label.setPos(label_x, label_y)
                self.graph_edges.append(line)
            return self._add_media_channels(referenced, y + height)

        def _add_grid(self, images, y):
            columns = min(3, len(images))
            width = (self.WIDTH - 40 - 12 * (columns - 1)) / columns
            for start in range(0, len(images), columns):
                row = []
                for column, preview in enumerate(images[start:start + columns]):
                    image_index = self._image_indices[id(preview)]
                    item = self._image_card(image_index, width)
                    item.setPos(20 + column * (width + 12), y)
                    row.append(item)
                    self.previews.append(item)
                y += max(item.height for item in row) + 12
            return y + 4

        def boundingRect(self):
            return QtCore.QRectF(0, 0, self.WIDTH, self.height)

        def paint(self, painter, option, widget=None):
            palette = self.palette
            accent = self._accent
            border = accent if self.isSelected() else palette.color(QtGui.QPalette.Mid)
            painter.setPen(QtGui.QPen(border, 2 if self.isSelected() else 1.2))
            painter.setBrush(_mix(palette.color(QtGui.QPalette.Base), accent, 0.035))
            painter.drawRoundedRect(self.boundingRect(), 12, 12)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(accent)
            painter.drawEllipse(QtCore.QRectF(17, 16, 27, 27))
            painter.setPen(palette.color(QtGui.QPalette.HighlightedText))
            painter.drawText(QtCore.QRectF(17, 16, 27, 27), QtCore.Qt.AlignCenter, str(self.number))


    class _ChannelPopup(QtWidgets.QFrame):
        """Display only verified channel images, outside the diagram's geometry.

        RGB overview PNGs are opaque presentation images. In particular their
        alpha cannot recover the captured texture's A component.
        """
        def __init__(self, title, channels, owner):
            super(_ChannelPopup, self).__init__(owner, QtCore.Qt.Popup)
            self._alive, self._executor = True, owner._executor
            self._pending, self._cache = {}, OrderedDict()
            self._updating_channels = False
            self._saved_previews = False
            self.previews = []
            self.setObjectName("breadFrameFlowChannels")
            self.setFrameShape(QtWidgets.QFrame.StyledPanel)
            self.setAutoFillBackground(True)
            self.setPalette(owner.palette())
            background = owner.palette().color(QtGui.QPalette.Window).name()
            foreground = owner.palette().color(QtGui.QPalette.Text).name()
            border = owner.palette().color(QtGui.QPalette.Mid).name()
            # i18n-ignore: stylesheet selectors and palette-derived CSS colors are implementation text.
            self.setStyleSheet("#breadFrameFlowChannels {background-color: %s; border: 1px solid %s;} "
                "#breadFrameFlowChannels QLabel {color: %s; background: transparent;} "
                "#breadFrameFlowChannels QToolButton {background: transparent; border: none; padding: 5px;}" %
                (background, border, foreground))
            self.setWindowTitle(_format(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "%1 · Channel details"), title))
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(12, 10, 12, 12)
            header = QtWidgets.QHBoxLayout()
            self.title = QtWidgets.QLabel(self.windowTitle(), self)
            self.title.setTextFormat(QtCore.Qt.PlainText)
            self.title.setWordWrap(True)
            font = self.title.font()
            font.setBold(True)
            font.setPointSizeF(max(12.0, font.pointSizeF()))
            self.title.setFont(font)
            header.addWidget(self.title, 1)
            self.close_button = QtWidgets.QToolButton(self)
            # Theme styles do not consistently supply SP_TitleBarCloseButton.
            # Draw a neutral close glyph in the current text color instead.
            icon = QtGui.QPixmap(18, 18)
            icon.fill(QtCore.Qt.transparent)
            painter = QtGui.QPainter(icon)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setPen(QtGui.QPen(owner.palette().color(QtGui.QPalette.Text), 2))
            painter.drawLine(4, 4, 14, 14)
            painter.drawLine(14, 4, 4, 14)
            painter.end()
            self.close_button.setIcon(QtGui.QIcon(icon))
            self.close_button.setToolTip(QtCore.QCoreApplication.translate("ReplayLabProjects", "Close"))
            self.close_button.setAccessibleName(QtCore.QCoreApplication.translate("ReplayLabProjects", "Close"))
            self.close_button.clicked.connect(self.close)
            header.addWidget(self.close_button, 0, QtCore.Qt.AlignTop)
            layout.addLayout(header)
            self.status = QtWidgets.QLabel(self)
            self.status.setTextFormat(QtCore.Qt.PlainText)
            self.status.setWordWrap(True)
            layout.addWidget(self.status)
            self.view = QtWidgets.QGraphicsView(self)
            self.scene = QtWidgets.QGraphicsScene(self.view)
            self.view.setScene(self.scene)
            self.view.setRenderHints(owner.renderHints())
            self.view.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
            self.view.setBackgroundBrush(owner.palette().color(QtGui.QPalette.Window))
            layout.addWidget(self.view, 1)
            self._owner = owner
            self.set_channels(channels)
            available = QtWidgets.QApplication.desktop().availableGeometry(owner)
            origin = owner.mapToGlobal(owner.rect().center()) - QtCore.QPoint(self.width() // 2, self.height() // 2)
            self.move(max(available.left() + 8, min(origin.x(), available.right() - self.width() - 8)),
                      max(available.top() + 8, min(origin.y(), available.bottom() - self.height() - 8)))
            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(40)
            self._timer.timeout.connect(self._load_visible)
            self.view.horizontalScrollBar().valueChanged.connect(self._load_visible)
            self.view.verticalScrollBar().valueChanged.connect(self._load_visible)
            QtWidgets.QApplication.instance().installEventFilter(self)
            self.show()
            self._timer.start()

        def set_channels(self, channels):
            if not self._alive:
                return
            # Scroll-range signals are synchronous. Build off-screen first so
            # they never observe deleted cards or an incomplete replacement row.
            scene = QtWidgets.QGraphicsScene(self.view)
            previews = []
            columns, y = max(1, min(4, len(channels))), 8
            width = 260 if columns >= 3 else 440
            for start in range(0, len(channels), columns):
                row = []
                for column, preview in enumerate(channels[start:start + columns]):
                    card = _PreviewItem(preview, width, self._owner.palette(), self._owner.activate, None, channel_detail=True)
                    card.setPos(8 + column * (width + 12), y)
                    scene.addItem(card)
                    previews.append(card)
                    row.append(card)
                y += max(card.height for card in row) + 12
            scene.setSceneRect(0, 0, columns * (width + 12) + 4, y)
            old_scene = self.scene
            self._updating_channels = True
            try:
                self.scene, self.previews = scene, previews
                self._saved_previews = False
                self.view.setScene(scene)
                self.status.setVisible(not channels)
                if not channels:
                    self.status.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "Loading channels…"))
                available = QtWidgets.QApplication.desktop().availableGeometry(self._owner)
                self.resize(min(int(scene.sceneRect().width()) + 34, available.width() - 32),
                            min(max(180, int(y) + 85), available.height() - 48, 680))
                self.view.horizontalScrollBar().setValue(0)
                self.view.verticalScrollBar().setValue(0)
                if self.isVisible():
                    self.move(max(available.left() + 8, min(self.x(), available.right() - self.width() - 8)),
                              max(available.top() + 8, min(self.y(), available.bottom() - self.height() - 8)))
            finally:
                self._updating_channels = False
                old_scene.deleteLater()
            # A shown popup needs one layout turn before all four cards have a
            # valid visible rectangle; the same popup then receives every image.
            QtCore.QTimer.singleShot(0, self._load_visible)
            self.view.viewport().update()

        def set_error(self, error):
            if self._alive:
                source = (QtCore.QCoreApplication.translate("ReplayLabProjects",
                    "Showing saved channel previews. Current replay could not be checked: %1")
                    if self._saved_previews and self.previews else QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Could not load channel previews: %1"))
                self.status.setText(_format(source, _text(error, 1200)))
                self.status.show()

        def set_saved(self):
            if self._alive:
                self._saved_previews = True
                self.status.setText(QtCore.QCoreApplication.translate("ReplayLabProjects",
                    "Showing saved channel previews. Current replay has not been verified."))
                self.status.show()

        def set_loading(self):
            if self._alive:
                self.status.setText(QtCore.QCoreApplication.translate("ReplayLabProjects",
                    "Checking current replay; showing saved channel previews.") if self._saved_previews and self.previews else
                    QtCore.QCoreApplication.translate("ReplayLabProjects", "Loading channels…"))
                self.status.show()

        def set_incomplete(self):
            if self._alive:
                self.status.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Some channels could not be loaded. Reopen to retry."))
                self.status.show()

        def _load_visible(self, unused=None):
            if not self._alive or self._updating_channels:
                return
            for path, future in list(self._pending.items()):
                if not future.done():
                    continue
                del self._pending[path]
                try:
                    image = future.result()
                    self._cache[path] = QtGui.QPixmap.fromImage(image) if image is not None else None
                except Exception:
                    self._cache[path] = None
                while len(self._cache) > 24:
                    self._cache.popitem(last=False)
            visible = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
            for card in self.previews:
                if not card.sceneBoundingRect().intersects(visible):
                    card.pixmap = None
                    continue
                path = card.preview["path"]
                if path in self._cache:
                    card.pixmap, card.failed = self._cache[path], self._cache[path] is None
                    self._cache.move_to_end(path)
                    card.update()
                elif path not in self._pending and len(self._pending) < 4:
                    self._pending[path] = self._executor.submit(_decode_preview, path)

        def eventFilter(self, watched, event):
            if (self._alive and event.type() == QtCore.QEvent.MouseButtonPress and
                    not self.frameGeometry().contains(event.globalPos())):
                self.close()
            return False

        def keyPressEvent(self, event):
            if event.key() == QtCore.Qt.Key_Escape:
                self.close()
                event.accept()
                return
            super(_ChannelPopup, self).keyPressEvent(event)

        def closeEvent(self, event):
            self._alive = False
            self._timer.stop()
            QtWidgets.QApplication.instance().removeEventFilter(self)
            for future in self._pending.values():
                future.cancel()
            self._pending.clear()
            self._cache.clear()
            super(_ChannelPopup, self).closeEvent(event)


    class _DiagramExport(QtCore.QObject):
        """Render every stage, including off-screen images, without a giant bitmap.

        Qt scene work runs in short GUI ticks. PNG reads/encoding use workers;
        only a 512-pixel strip's thumbnails and one bounded output image remain
        resident. The live canvas and its scroll/collapse state are untouched.
        """
        def __init__(self, view, path, progress, finished, parent):
            super(_DiagramExport, self).__init__(parent)
            self.document, self.palette = copy.deepcopy(view.document), QtGui.QPalette(view.palette())
            self.expanded, self.ratios = set(view._expanded_groups), dict(view._comparison_ratios)
            self.animation_states = copy.deepcopy(view._animation_states)
            self.executor, self.path = view._executor, path
            self.progress, self.finished = progress, finished
            self.scene = QtWidgets.QGraphicsScene(self)
            self.previews, self.stage_ends, self.files, self.pending = [], [], [], {}
            self.phase, self.stage_index, self.part_index = "layout", 0, 0
            self.alive, self.image, self.writer, self.cancel_requested = True, None, None, False
            self.timer = QtCore.QTimer(self)
            self.timer.setInterval(10)
            self.timer.timeout.connect(self._tick)
            self.y = 20.0
            for key, bold in (("title", True), ("summary", False)):
                if self.document.get(key):
                    item = _PlainText(self.document[key], 1080, self.palette, None, bold=bold)
                    self.scene.addItem(item)
                    item.setPos(0, self.y)
                    self.y += item.boundingRect().height() + 14
            self.timer.start()

        def cancel(self):
            self.cancel_requested = True

        def _finish(self, error=""):
            if not self.alive:
                return
            self.alive = False
            self.timer.stop()
            for future in self.pending.values():
                future.cancel()
            if self.writer is not None:
                self.writer.cancel()
            self.image = None
            self.pending.clear()
            self.scene.clear()
            self.finished({"ok": not error, "error": error, "paths": list(self.files),
                "stageCount": self.stage_index, "width": 1120, "height": getattr(self, "height", 0),
                "segments": getattr(self, "segments", [])})
            self.deleteLater()

        def _tick(self):
            if not self.alive:
                return
            try:
                self._advance()
            except Exception as error:
                self._finish(str(error))

        def _advance(self):
            if self.cancel_requested and self.phase != "encode":
                self._finish("cancelled")
                return
            if self.phase == "layout":
                stages = self.document["stages"]
                if self.stage_index < len(stages):
                    if self.stage_index:
                        color = _mix(self.palette.color(QtGui.QPalette.Text), self.palette.color(QtGui.QPalette.Highlight), 0.15)
                        self.scene.addLine(540, self.y + 2, 540, self.y + 29, QtGui.QPen(color, 3.2))
                        self.scene.addPolygon(QtGui.QPolygonF([QtCore.QPointF(532, self.y + 25),
                            QtCore.QPointF(548, self.y + 25), QtCore.QPointF(540, self.y + 39)]),
                            QtGui.QPen(color), QtGui.QBrush(color))
                        self.y += 50
                    item = _StageItem(stages[self.stage_index], self.stage_index + 1, self.palette,
                        lambda unused: None, self.expanded, lambda *unused: None, self.ratios, self.animation_states)
                    self.scene.addItem(item)
                    item.setPos(0, self.y)
                    self.y += item.height
                    self.previews.extend(item.previews)
                    self.stage_ends.append(self.y + 20)
                    self.stage_index += 1
                    self.progress(0, len(stages))
                    return
                self.height = int(math.ceil(self.y + 20))
                self.segments = export_segments(self.height, self.stage_ends)
                self.paths = export_part_paths(self.path, len(self.segments), 1)
                self.phase = "new-part"
                return
            if self.phase == "new-part":
                self.part_start, self.part_end = self.segments[self.part_index]
                self.image = QtGui.QImage(1120, self.part_end - self.part_start, QtGui.QImage.Format_RGB32)
                if self.image.isNull():
                    raise MemoryError("Could not allocate diagram image")
                self.image.fill(self.palette.color(QtGui.QPalette.Window))
                self.strip_start, self.phase = self.part_start, "prepare-strip"
                return
            if self.phase == "prepare-strip":
                self.strip_end = min(self.part_end, self.strip_start + 512)
                rect = QtCore.QRectF(-20, self.strip_start, 1120, self.strip_end - self.strip_start)
                self.receivers = [item for item in self.previews if getattr(item, "active", True)
                                  and item.sceneBoundingRect().intersects(rect)]
                self.paths_to_load = list(dict.fromkeys(item.preview["path"] for item in self.receivers))
                self.loaded, self.pending, self.phase = {}, {}, "load-strip"
                return
            if self.phase == "load-strip":
                for path, future in list(self.pending.items()):
                    if future.done():
                        image = future.result()
                        if image is None or image.isNull():
                            raise IOError("Could not read preview: " + path)
                        self.loaded[path] = QtGui.QPixmap.fromImage(image)
                        del self.pending[path]
                while self.paths_to_load and len(self.pending) < 4:
                    path = self.paths_to_load.pop(0)
                    self.pending[path] = self.executor.submit(_decode_preview, path)
                if self.paths_to_load or self.pending:
                    return
                for item in self.receivers:
                    item.pixmap = self.loaded[item.preview["path"]]
                painter = QtGui.QPainter(self.image)
                try:
                    painter.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.TextAntialiasing |
                                           QtGui.QPainter.SmoothPixmapTransform)
                    size = self.strip_end - self.strip_start
                    target = QtCore.QRectF(0, self.strip_start - self.part_start, 1120, size)
                    painter.setClipRect(target, QtCore.Qt.IntersectClip)
                    self.scene.render(painter, target,
                                      QtCore.QRectF(-20, self.strip_start, 1120, size))
                finally:
                    painter.end()
                for item in self.receivers:
                    item.pixmap = None
                self.loaded.clear()
                self.strip_start = self.strip_end
                self.progress(self.strip_start, self.height)
                if self.strip_start < self.part_end:
                    self.phase = "prepare-strip"
                else:
                    self.writer = self.executor.submit(_save_export_image, (self.paths[self.part_index], self.image))
                    self.image, self.phase = None, "encode"
                return
            if self.phase == "encode" and self.writer.done():
                result = self.writer.result() or {"error": "PNG writer failed"}
                self.writer = None
                if result.get("error"):
                    raise IOError(result["error"])
                self.files.append(result["path"])
                if self.cancel_requested:
                    self._finish("cancelled")
                    return
                self.part_index += 1
                if self.part_index == len(self.segments):
                    self._finish()
                else:
                    self.phase = "new-part"


    class _StageDirectoryDelegate(QtWidgets.QStyledItemDelegate):
        """Compact two-line navigation rows, painted from the current theme."""
        def sizeHint(self, option, index):
            metrics = QtGui.QFontMetrics(option.font)
            return QtCore.QSize(160, max(48, metrics.lineSpacing() * 2 + 16))

        def paint(self, painter, option, index):
            painter.save()
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            palette = option.palette
            selected = bool(option.state & QtWidgets.QStyle.State_Selected)
            hovered = bool(option.state & QtWidgets.QStyle.State_MouseOver)
            rect = QtCore.QRectF(option.rect).adjusted(2, 2, -2, -2)
            background = palette.color(QtGui.QPalette.Window)
            foreground = palette.color(QtGui.QPalette.Text)
            accent = palette.color(QtGui.QPalette.Highlight)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(_mix(background, accent, 0.16 if selected else 0.07 if hovered else 0.0))
            painter.drawRoundedRect(rect, 6, 6)
            if selected:
                painter.setBrush(accent)
                painter.drawRoundedRect(QtCore.QRectF(rect.left(), rect.top() + 9, 3, rect.height() - 18), 1.5, 1.5)
            font = QtGui.QFont(option.font)
            font.setBold(selected)
            painter.setFont(font)
            painter.setPen(_mix(foreground, background, 0.10 if selected else 0.36))
            painter.drawText(rect.adjusted(9, 9, -rect.width() + 34, -8), QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft,
                             str(index.row() + 1).zfill(2))
            text_rect = rect.adjusted(39, 8, -9, -8)
            text = str(index.data(QtCore.Qt.DisplayRole) or "")
            text_layout = QtGui.QTextLayout(text, font)
            text_option = QtGui.QTextOption()
            text_option.setWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
            text_layout.setTextOption(text_option)
            text_layout.beginLayout()
            lines = []
            for unused in range(2):
                line = text_layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(max(1.0, text_rect.width()))
                lines.append(line)
            text_layout.endLayout()
            painter.setPen(foreground if selected else _mix(foreground, background, 0.12))
            metrics = QtGui.QFontMetricsF(font)
            for number, line in enumerate(lines):
                position = text_rect.topLeft() + QtCore.QPointF(0, number * metrics.lineSpacing())
                if number == 1 and line.textStart() + line.textLength() < len(text):
                    painter.drawText(position + QtCore.QPointF(0, metrics.ascent()),
                        metrics.elidedText(text[line.textStart():], QtCore.Qt.ElideRight, text_rect.width()))
                else:
                    line.draw(painter, position)
            if option.state & QtWidgets.QStyle.State_HasFocus:
                painter.setBrush(QtCore.Qt.NoBrush)
                painter.setPen(QtGui.QPen(accent, 1, QtCore.Qt.DotLine))
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 5, 5)
            painter.restore()


    class FrameRenderFlowView(QtWidgets.QGraphicsView):
        readingPositionChanged = QtCore.Signal()
        contextRequested = QtCore.Signal(object)

        def __init__(self, activate, parent=None, channel_request_callback=None, channel_revision_provider=None):
            super(FrameRenderFlowView, self).__init__(parent)
            self.activate, self.document = activate, None
            self.channel_request_callback = channel_request_callback
            self.channel_revision_provider = channel_revision_provider
            self._scene = QtWidgets.QGraphicsScene(self)
            self.setScene(self._scene)
            self.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.TextAntialiasing |
                                QtGui.QPainter.SmoothPixmapTransform)
            self.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
            self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
            self.setViewportUpdateMode(QtWidgets.QGraphicsView.BoundingRectViewportUpdate)
            self._stages, self._previews, self._animations = {}, [], []
            self._expanded_groups, self._comparison_ratios = set(), {}
            self._animation_states = {}
            self._channel_popup = None
            self._channel_popup_key = None
            self._channel_cache, self._channel_requests = OrderedDict(), set()
            self._pending_group_toggles = []
            # concurrent.futures imports multiprocessing/socket on Python 3.6;
            # the embedded host intentionally has no _socket. Use Qt's pool.
            self._executor = _PreviewExecutor()
            self._pending, self._cache = {}, OrderedDict()
            self._generation, self._alive, self._fit_width = 0, True, True
            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(40)
            self._timer.timeout.connect(self._drain_images)
            self._schedule = QtCore.QTimer(self)
            self._schedule.setSingleShot(True)
            self._schedule.timeout.connect(self._request_visible_images)
            self._layout_timer = QtCore.QTimer(self)
            self._layout_timer.setSingleShot(True)
            self._layout_timer.timeout.connect(self._apply_group_toggles)
            self._animation_timer = QtCore.QTimer(self)
            self._animation_timer.setInterval(100)
            self._animation_timer.timeout.connect(self._advance_animations)
            self.horizontalScrollBar().valueChanged.connect(self._queue_visible_images)
            self.verticalScrollBar().valueChanged.connect(self._queue_visible_images)

        def contextMenuEvent(self, event):
            item, selection = self.itemAt(event.pos()), {}
            while item is not None:
                data = item.data(FLOW_ATTACHMENT_ROLE)
                if isinstance(data, dict):
                    for key, value in data.items():
                        selection.setdefault(key, value)
                item = item.parentItem()
            if not selection.get('stageId') or not self.document:
                event.ignore()
                return
            document = self.document
            menu = QtWidgets.QMenu(self)
            action = menu.addAction(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Add to Bread AI chat"))
            chosen = menu.exec_(event.globalPos())
            if chosen == action and self._alive and self.document is document:
                self.contextRequested.emit({'selection': selection, 'document': document})
            event.accept()

        def set_document(self, document):
            self._close_channel_popup()
            identity = lambda value: tuple((value or {}).get(key) for key in
                                           ("flowId", "captureSessionId", "capturePath"))
            new_document = self.document is None or identity(self.document) != identity(document)
            self._generation += 1
            self._channel_cache.clear()
            self._channel_requests.clear()
            self.document = document
            if new_document:
                self._document_channel_revision = self._channel_revision()
            self._cache.clear()
            if new_document:
                self._expanded_groups.clear()
                self._comparison_ratios.clear()
                self._animation_states.clear()
            self._pending_group_toggles = []
            self._layout_timer.stop()
            self._rebuild()
            if new_document:
                self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())

        def _rebuild(self):
            if not self._alive:
                return
            self._close_channel_popup()
            # Scene replacement otherwise lets QGraphicsView center a new tall
            # scene before fit_width reads the viewport, losing the user's place.
            center = self.mapToScene(self.viewport().rect().center())
            at_top = self.verticalScrollBar().value() == self.verticalScrollBar().minimum()
            self._scene.clear()
            self._stages, self._previews, self._animations = {}, [], []
            palette = self.palette()
            self.setBackgroundBrush(palette.color(QtGui.QPalette.Window))
            y = 20.0
            for index, stage in enumerate((self.document or {}).get("stages", []), 1):
                if index > 1:
                    color = _mix(palette.color(QtGui.QPalette.Text), palette.color(QtGui.QPalette.Highlight), 0.15)
                    self._scene.addLine(540, y + 2, 540, y + 29, QtGui.QPen(color, 3.2))
                    self._scene.addPolygon(QtGui.QPolygonF([
                        QtCore.QPointF(532, y + 25), QtCore.QPointF(548, y + 25),
                        QtCore.QPointF(540, y + 39)]), QtGui.QPen(color), QtGui.QBrush(color))
                    y += 50
                item = _StageItem(stage, index, palette, self.activate, self._expanded_groups,
                                  self._toggle_group, self._comparison_ratios, self._animation_states,
                                  callable(self.channel_request_callback))
                item.setPos(0, y)
                self._scene.addItem(item)
                self._stages[stage["id"]] = item
                self._previews.extend(item.previews)
                self._animations.extend(item.animations)
                y += item.height
            self._scene.setSceneRect(-20, 0, _StageItem.WIDTH + 40, y + 20)
            if self._fit_width:
                self.fit_width()
            self.centerOn(_StageItem.WIDTH * 0.5 if self._fit_width else center.x(), center.y())
            if at_top:
                self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())
            self._queue_visible_images()
            if self._animations and self.isVisible():
                self._animation_timer.start()
            else:
                self._animation_timer.stop()

        def _advance_animations(self):
            if not self._alive:
                return
            visible = self.mapToScene(self.viewport().rect()).boundingRect()
            now, advanced = time.monotonic(), False
            for item in self._animations:
                advanced = item.advance(now, self.isVisible() and item.sceneBoundingRect().intersects(visible)) or advanced
            if advanced:
                self._queue_visible_images()

        def _toggle_group(self, stage_id, group):
            # Never delete the clicked QGraphicsItem inside its mouse handler.
            self._pending_group_toggles.append((stage_id, group))
            self._layout_timer.start(0)

        def _apply_group_toggles(self):
            if not self._alive or not self._pending_group_toggles:
                return
            pending, self._pending_group_toggles = self._pending_group_toggles, []
            channel_keys = [key for key in pending if key[1].startswith("channels:")]
            volume_keys = [key for key in pending if key[1].startswith("volume:")]
            pending = [key for key in pending if not key[1].startswith(("channels:", "volume:")) and key[1] != "algorithm"]
            if channel_keys:
                self._open_channels(*channel_keys[-1])
            if volume_keys:
                self._open_volume(*volume_keys[-1])
            if not pending:
                return
            stage_id = pending[-1][0]
            item = self._stages.get(stage_id)
            previous_y = item.y() if item is not None else 0
            center = self.mapToScene(self.viewport().rect().center())
            for key in pending:
                if key in self._expanded_groups:
                    self._expanded_groups.remove(key)
                else:
                    self._expanded_groups.add(key)
            self._pending_group_toggles = []
            self._rebuild()
            item = self._stages.get(stage_id)
            self.centerOn(center.x(), center.y() + (item.y() - previous_y if item is not None else 0))

        def _close_channel_popup(self):
            popup, self._channel_popup = getattr(self, "_channel_popup", None), None
            self._channel_popup_key = None
            if popup is not None:
                popup.close()
                popup.deleteLater()

        def _open_volume(self, stage_id, key):
            item = self._stages.get(stage_id)
            if item is None:
                return
            try:
                index = int(key.split(":", 1)[1])
            except (ValueError, IndexError):
                return
            if not 0 <= index < len(item.stage["images"]):
                return
            preview = item.stage["images"][index]
            if not preview.get("volumePreview"):
                return
            self._close_channel_popup()
            from .project_frame_volume import VolumePreviewPopup
            self._channel_popup = VolumePreviewPopup(preview, self, _decode_preview,
                lambda selected: self.activate(preview_navigation(selected)))

        def _channel_revision(self):
            self._channel_revision_error = ""
            try:
                revision = self.channel_revision_provider() if callable(self.channel_revision_provider) else None
                return channel_revision_key(revision)
            except Exception as error:
                # Saved PNG evidence is viewable even while live replay state
                # is unavailable. Never use this sentinel as a verified epoch.
                self._channel_revision_error = str(error)
                return ("revision-unavailable",)

        def _open_channels(self, stage_id, key):
            item = self._stages.get(stage_id)
            if item is None:
                return
            if key == "channels:unassigned":
                channels, title = item._unassigned_channels, item.stage["title"]
                parent = None
            else:
                try:
                    index = int(key.split(":", 1)[1])
                except (ValueError, IndexError):
                    return
                channels = item._channel_groups.get(index, [])
                if index < 0 or index >= len(item.stage["images"]):
                    return
                parent = item.stage["images"][index]
                title = parent["title"]
                if not channel_details_available(parent):
                    return
                channels = planned_channel_previews(parent, channels)
            request = parent is not None and parent.get("resourceId") and callable(self.channel_request_callback)
            revision = self._channel_revision()
            revision_error = self._channel_revision_error
            request = request and not revision_error
            prune_channel_cache(self._channel_cache, self._generation, revision, time.monotonic())
            identity = (self._generation, revision, preview_card_identity(parent)) if parent is not None else None
            cached = self._channel_cache.get(identity)
            if cached is not None:
                channels = cached[1]
                self._channel_cache.move_to_end(identity)
            saved_current = saved_channels_current(parent, channels,
                (getattr(self, "document", None) or {}).get("captureSessionId"), revision,
                getattr(self, "_document_channel_revision", None))
            refresh = ((parent is not None and parent.get("channelCacheComplete") is False) or
                       (callable(self.channel_revision_provider) and not saved_current) or
                       not channel_previews_complete(parent, channels))
            if channels or request or revision_error:
                self._close_channel_popup()
                self._channel_popup_key = identity
                # These are immutable saved receipts, not a claim that live
                # replay still matches. Show them before any optional refresh.
                self._channel_popup = _ChannelPopup(title, channels, self)
                if channels and cached is None and (refresh or revision_error):
                    self._channel_popup.set_saved()
                if revision_error:
                    self._channel_popup.set_error(revision_error)
            if request and cached is None and refresh:
                self._channel_popup.set_loading()
                if identity in self._channel_requests:
                    return
                self._channel_requests.add(identity)
                def complete(result):
                    self._channel_requests.discard(identity)
                    if not self._alive or identity[0] != self._generation:
                        return
                    if identity[1] != self._channel_revision():
                        if self._channel_popup_key == identity:
                            popup = self._channel_popup
                            if popup is not None and popup._alive:
                                if channels:
                                    popup.set_saved()
                                else:
                                    popup.set_incomplete()
                        return
                    popup = self._channel_popup if self._channel_popup_key == identity else None
                    try:
                        if not isinstance(result, dict):
                            raise ValueError("Invalid channel response")
                        if not result.get("images"):
                            raise ValueError(result.get("error") or "Channel response contains no previews")
                        actual_parent = dict(parent)
                        if type(result.get("componentCount")) is int and result["componentCount"] in (1, 2, 3, 4):
                            actual_parent["componentCount"] = result["componentCount"]
                        if "channelGroups" in result:
                            actual_parent["channelGroups"] = result["channelGroups"]
                        channel_plan(actual_parent)  # Reject overlapping or unavailable components.
                        images = normalize_channel_previews(actual_parent, result.get("images"))
                        images = planned_channel_previews(actual_parent, images, allow_parent_rgb=False)
                        complete_set = (not result.get("error") and result.get("complete", True) and
                                        channel_previews_complete(actual_parent, images))
                        # Do not replace an existing saved set with an incomplete
                        # refresh or mix different replay versions into one set.
                        if popup is not None and popup._alive and (complete_set or not channels):
                            popup.set_channels(images)
                        if complete_set:
                            # Cache only after the currently open popup accepted
                            # the complete row; failed/partial rebuilds retry.
                            self._channel_cache[identity] = (time.monotonic(), images)
                            prune_channel_cache(self._channel_cache, self._generation, identity[1], time.monotonic())
                        elif popup is not None and popup._alive:
                            if result.get("error"):
                                popup.set_error(result["error"])
                            elif channels:
                                popup.set_saved()
                            else:
                                popup.set_incomplete()
                    except Exception as error:
                        if popup is not None and popup._alive:
                            popup.set_error(str(error))
                try:
                    self.channel_request_callback(copy.deepcopy(parent), complete)
                except Exception as error:
                    complete({"error": str(error)})

        def fit_width(self):
            self._fit_width = True
            self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOn)
            center_y = self.mapToScene(self.viewport().rect().center()).y()
            scale = max(0.15, min(2.5, self.viewport().width() / (_StageItem.WIDTH + 40.0)))
            self.resetTransform()
            self.scale(scale, scale)
            self.centerOn(_StageItem.WIDTH * 0.5, center_y)
            self._queue_visible_images()

        def zoom(self, factor=None):
            self._fit_width = False
            self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
            old = self.transform().m11()
            desired = max(0.15, min(3.0, old * factor if factor else 1.0))
            self.scale(desired / old, desired / old)
            self._queue_visible_images()

        def wheelEvent(self, event):
            if event.modifiers() & QtCore.Qt.ControlModifier:
                self.zoom(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)
                event.accept()
            else:
                super(FrameRenderFlowView, self).wheelEvent(event)

        def resizeEvent(self, event):
            at_top = self.verticalScrollBar().value() == self.verticalScrollBar().minimum()
            super(FrameRenderFlowView, self).resizeEvent(event)
            if getattr(self, "_fit_width", False):
                self.fit_width()
            if at_top:
                self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())
            self._queue_visible_images()

        def showEvent(self, event):
            super(FrameRenderFlowView, self).showEvent(event)
            self._queue_visible_images()
            if getattr(self, "_animations", []):
                self._animation_timer.start()

        def hideEvent(self, event):
            super(FrameRenderFlowView, self).hideEvent(event)
            self._close_channel_popup()
            self._animation_timer.stop()

        def changeEvent(self, event):
            super(FrameRenderFlowView, self).changeEvent(event)
            if event.type() in (QtCore.QEvent.PaletteChange, QtCore.QEvent.ApplicationPaletteChange):
                if hasattr(self, "_scene"):
                    self._rebuild()

        def focus_stage(self, stage_id):
            item = self._stages.get(stage_id)
            if item is not None:
                self._scene.clearSelection()
                item.setSelected(True)
                visible = self.mapToScene(self.viewport().rect()).boundingRect()
                # A long module must open at its title, not halfway through its
                # previews. QGraphicsView clamps the last module to the page end.
                top = item.sceneBoundingRect().top() - 16.0 / max(0.01, self.transform().m11())
                self.centerOn(visible.center().x(), top + visible.height() * 0.5)
                self._queue_visible_images()

        def _queue_visible_images(self, unused=None):
            if getattr(self, "_alive", False):
                self._schedule.start(0)
                self.readingPositionChanged.emit()

        def _request_visible_images(self):
            if not self._alive:
                return
            visible = self.mapToScene(self.viewport().rect()).boundingRect()
            visible.adjust(0, -240, 0, 240)
            candidates = []
            for item in self._previews:
                if not getattr(item, "active", True) or not item.sceneBoundingRect().intersects(visible):
                    item.pixmap = None
                    continue
                candidates.append(item)
            # A zoomed-out overview can contain hundreds of tiny previews.
            # Prioritize nearby unique images within the cache budget, avoiding
            # an endless decode/evict loop or unbounded retained QPixmaps.
            candidates.sort(key=lambda item: abs(item.sceneBoundingRect().center().y() - visible.center().y()))
            selected_paths = set()
            for item in candidates:
                path = item.preview["path"]
                if path not in selected_paths and len(selected_paths) >= 24:
                    item.pixmap, item.deferred = None, True
                    item.update()
                    continue
                selected_paths.add(path)
                item.deferred = False
                if path in self._cache:
                    item.pixmap = self._cache[path]
                    item.failed = item.pixmap is None
                    self._cache.move_to_end(path)
                    item.update()
                elif ((self._generation, path) not in self._pending and len(self._pending) < 4):
                    self._pending[(self._generation, path)] = self._executor.submit(_decode_preview, path)
            if self._pending:
                self._timer.start()

        def _drain_images(self):
            for key, future in list(self._pending.items()):
                if not future.done():
                    continue
                del self._pending[key]
                if key[0] != self._generation or not self._alive:
                    continue
                try:
                    image = future.result()
                    pixmap = QtGui.QPixmap.fromImage(image) if image is not None else None
                except Exception:
                    pixmap = None
                self._cache[key[1]] = pixmap
                while len(self._cache) > 24:
                    self._cache.popitem(last=False)
            if not self._pending:
                self._timer.stop()
            self._request_visible_images()

        def shutdown(self):
            self._close_channel_popup()
            self._alive = False
            self._generation += 1
            self._timer.stop()
            self._schedule.stop()
            self._layout_timer.stop()
            self._animation_timer.stop()
            for future in self._pending.values():
                future.cancel()
            self._pending.clear()
            # qrenderdoc embeds Python: no Python worker may outlive integration
            # shutdown and touch Qt during interpreter teardown. At most two
            # bounded local PNG decodes are running; queued futures are cancelled.
            self._executor.shutdown(wait=True)
            self._cache.clear()


    class FrameRenderFlowPanel(object):
        def __init__(self, ctx, qrd, capture_binding_provider, closed_callback=None,
                     navigation_callback=None, channel_request_callback=None, channel_revision_provider=None):
            self.ctx, self.qrd = ctx, qrd
            self.capture_binding_provider, self.closed_callback = capture_binding_provider, closed_callback
            self.navigation_callback = navigation_callback
            self.document, self._alive, self._docked = None, True, False
            self._matches, self._match_index = [], -1
            self._outline_rows = {}
            self._export_job, self.export_state = None, {}
            self._export_generation, self._export_job_generation = 0, None
            self._dock_ancestors = []
            self.top = find_frame_flow_placeholder(ctx.GetMainWindow().Widget())
            if self.top is None:
                self.top = QtWidgets.QWidget()
            else:
                # Keep the original dock, tab order and active tab selected by the native
                # default/saved layout. Filling it must not dock or activate another window.
                self._dock_ancestors = retain_dock_ancestors(self.top)
                self._docked = bool(self._dock_ancestors)
            self.top.setObjectName("breadFrameRenderFlow")
            self.top.setProperty("breadPreserveOnLayoutRestore", True)
            self.top.setWindowTitle(QtCore.QCoreApplication.translate("ReplayLabProjects", "Frame Rendering Flow"))
            self.top.setWindowIcon(ctx.GetMainWindow().Widget().windowIcon())
            self.top.destroyed.connect(self._destroyed)
            layout = QtWidgets.QVBoxLayout(self.top)
            layout.setContentsMargins(8, 8, 8, 8)
            toolbar = QtWidgets.QHBoxLayout()
            self.search = QtWidgets.QLineEdit(self.top)
            self.search.setObjectName("breadFrameFlowSearch")
            self.search.setPlaceholderText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Find stage or EID (Ctrl+F)"))
            self.search.textChanged.connect(self._search_changed)
            self.search.returnPressed.connect(self._next_match)
            toolbar.addWidget(self.search, 1)
            self.search_status = QtWidgets.QLabel(self.top)
            toolbar.addWidget(self.search_status)
            self.save_image = QtWidgets.QPushButton(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Save image…"), self.top)
            self.save_image.setObjectName("breadFrameFlowSaveImage")
            self.save_image.clicked.connect(self._choose_export)
            toolbar.addWidget(self.save_image)
            self.cancel_export = QtWidgets.QPushButton(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Cancel export"), self.top)
            self.cancel_export.clicked.connect(lambda: self._export_job.cancel() if self._export_job else None)
            self.cancel_export.hide()
            toolbar.addWidget(self.cancel_export)
            self.fit = QtWidgets.QPushButton(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Fit width"), self.top)
            toolbar.addWidget(self.fit)
            self.actual = QtWidgets.QPushButton(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "100%"), self.top)
            toolbar.addWidget(self.actual)
            layout.addLayout(toolbar)
            self.title = QtWidgets.QLabel(self.top)
            self.title.setWordWrap(True)
            self.title.setTextFormat(QtCore.Qt.PlainText)
            font = self.title.font()
            font.setBold(True)
            self.title.setFont(font)
            layout.addWidget(self.title)
            self.summary = QtWidgets.QLabel(self.top)
            self.summary.setWordWrap(True)
            self.summary.setTextFormat(QtCore.Qt.PlainText)
            layout.addWidget(self.summary)
            self.status = QtWidgets.QLabel(self.top)
            self.status.setTextFormat(QtCore.Qt.PlainText)
            self.status.setWordWrap(True)
            layout.addWidget(self.status)
            self.export_status = QtWidgets.QLabel(self.top)
            self.export_status.setTextFormat(QtCore.Qt.PlainText)
            self.export_status.setWordWrap(True)
            self.export_status.hide()
            layout.addWidget(self.export_status)
            self._export_message_timer = QtCore.QTimer(self.top)
            self._export_message_timer.setSingleShot(True)
            self._export_message_timer.timeout.connect(self._clear_export_message)
            self.view = FrameRenderFlowView(self._navigate, self.top, channel_request_callback, channel_revision_provider)
            self.view.setObjectName("breadFrameFlowCanvas")
            self.view.contextRequested.connect(self._add_to_chat)
            self.body = QtWidgets.QSplitter(QtCore.Qt.Horizontal, self.top)
            self.body.setObjectName("breadFrameFlowBody")
            self.body.setHandleWidth(4)
            self.body.setChildrenCollapsible(False)
            self.directory = QtWidgets.QWidget(self.body)
            self.directory.setObjectName("breadFrameFlowDirectory")
            self.directory.setMinimumWidth(140)
            self.directory.setMaximumWidth(320)
            directory_layout = QtWidgets.QVBoxLayout(self.directory)
            directory_layout.setContentsMargins(0, 2, 8, 0)
            directory_layout.setSpacing(8)
            directory_title = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Rendering stages"), self.directory)
            directory_title.setContentsMargins(10, 6, 0, 4)
            directory_font = directory_title.font()
            directory_font.setPointSizeF(max(10.0, directory_font.pointSizeF()))
            directory_font.setBold(True)
            directory_title.setFont(directory_font)
            directory_layout.addWidget(directory_title)
            self.outline = QtWidgets.QListWidget(self.directory)
            self.outline.setObjectName("breadFrameFlowOutline")
            self.outline.setAccessibleName(QtCore.QCoreApplication.translate("ReplayLabProjects", "Rendering stages"))
            self.outline.setFrameShape(QtWidgets.QFrame.NoFrame)
            self.outline.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.outline.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
            self.outline.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            self.outline.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            self.outline.setMouseTracking(True)
            self.outline.setSpacing(3)
            directory_font.setBold(False)
            self.outline.setFont(directory_font)
            self.outline.setItemDelegate(_StageDirectoryDelegate(self.outline))
            self.outline.itemClicked.connect(self._outline_activated)
            self.outline.itemActivated.connect(self._outline_activated)
            self.outline.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            self.outline.customContextMenuRequested.connect(self._outline_context_menu)
            directory_layout.addWidget(self.outline, 1)
            self.body.addWidget(self.directory)
            self.body.addWidget(self.view)
            self.body.setStretchFactor(0, 0)
            self.body.setStretchFactor(1, 1)
            self.body.setSizes([218, 900])
            layout.addWidget(self.body, 1)
            self._outline_sync = QtCore.QTimer(self.top)
            self._outline_sync.setSingleShot(True)
            self._outline_sync.timeout.connect(self._sync_outline)
            self.view.readingPositionChanged.connect(self._queue_outline_sync)
            self.fit.clicked.connect(self.view.fit_width)
            self.actual.clicked.connect(lambda: self.view.zoom())
            self.shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+F"), self.top)
            self.shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
            self.shortcut.activated.connect(self.search.setFocus)
            self.legend = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Vertical arrows between stages show execution order. Connections within a module show how textures and calculation results flow. Underlined titles link to events and textures. Open channel details in a popup or drag a comparison divider. Ctrl+wheel zooms; drag empty space to pan."), self.top)
            self.legend.setWordWrap(True)
            layout.addWidget(self.legend)
            self.capture_changed()
            self.top.setProperty("breadFrameFlowPlaceholder", False)

        def _outline_context_menu(self, position):
            item = self.outline.itemAt(position)
            if item is None or not self._is_current():
                return
            payload = {'selection': {'stageId': str(item.data(QtCore.Qt.UserRole))},
                       'document': self.document}
            menu = QtWidgets.QMenu(self.outline)
            action = menu.addAction(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Add to Bread AI chat"))
            action.triggered.connect(lambda unused=False: self._add_to_chat(payload))
            menu.exec_(self.outline.viewport().mapToGlobal(position))

        def _add_to_chat(self, payload):
            if (not isinstance(payload, dict) or payload.get('document') is not self.document
                    or not self._is_current()):
                return
            context, facts = frame_flow_attachment(self.document, payload.get('selection') or {},
                                                   int(self.ctx.CurSelectedEvent()))
            source = _format(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Frame Rendering Flow — %1"), facts['title'])
            self.ctx.AddTextToAIAssistant(facts['title'], json.dumps(facts, ensure_ascii=False), source,
                json.dumps(context, ensure_ascii=False, separators=(',', ':')))

        def _populate_outline(self):
            self.outline.clear()
            self._outline_rows = {}
            for row, stage in enumerate((self.document or {}).get("stages", [])):
                item = QtWidgets.QListWidgetItem(stage["title"], self.outline)
                item.setData(QtCore.Qt.UserRole, stage["id"])
                item.setToolTip("\n".join(value for value in (stage["title"], stage.get("summary", "")) if value))
                self._outline_rows[stage["id"]] = row
            self.directory.setVisible(bool(self._outline_rows))
            self._queue_outline_sync()

        def _outline_activated(self, item):
            if self._alive and item is not None:
                # Browsing the diagram is intentionally separate from the
                # underlined event/texture links: never change replay state.
                self.view.focus_stage(str(item.data(QtCore.Qt.UserRole)))
                self._sync_outline()

        def _queue_outline_sync(self):
            if self._alive:
                self._outline_sync.start(0)

        def _sync_outline(self):
            if not self._alive or not self.document:
                return
            visible = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
            bar = self.view.verticalScrollBar()
            at_bottom = bar.maximum() > bar.minimum() and bar.value() >= bar.maximum() - 1
            bounds = [(stage["id"], self.view._stages[stage["id"]].sceneBoundingRect().top(),
                       self.view._stages[stage["id"]].sceneBoundingRect().bottom())
                      for stage in self.document["stages"] if stage["id"] in self.view._stages]
            stage_id = reading_stage_id(bounds, visible.top(), visible.height(), at_bottom)
            row = self._outline_rows.get(stage_id, -1)
            if row < 0 or row == self.outline.currentRow():
                return
            blocked = self.outline.blockSignals(True)
            try:
                self.outline.setCurrentRow(row)
                self.outline.scrollToItem(self.outline.item(row), QtWidgets.QAbstractItemView.EnsureVisible)
            finally:
                self.outline.blockSignals(blocked)

        def _destroyed(self, unused=None):
            if self._alive:
                self._alive = False
                # Child C++ objects may already be destroyed: release only the worker here.
                self.view._alive = False
                if self._export_job is not None:
                    self._export_job.finished = lambda unused: None
                    self._export_job.alive = False
                    for future in self._export_job.pending.values():
                        future.cancel()
                for future in self.view._pending.values():
                    future.cancel()
                self.view._executor.shutdown(wait=True)
                if callable(self.closed_callback):
                    self.closed_callback(self)

        def _is_current(self):
            return self._alive and binding_matches(self.document, self.capture_binding_provider())

        def _refresh_dock_ancestors(self):
            # Retain the new chain before releasing old wrappers; the native layout may have
            # moved this same dock to another area or deliberately detached it.
            self._dock_ancestors = retain_dock_ancestors(self.top)

        def show(self):
            if not self._alive:
                raise RuntimeError("Frame rendering flow panel was closed")
            self._refresh_dock_ancestors()
            if not self._docked or not self._dock_ancestors:
                self.ctx.AddDockWindow(self.top, self.qrd.DockReference.MainToolArea, None, 0.72)
                self._docked = True
                self._refresh_dock_ancestors()
            self.ctx.RaiseDockWindow(self.top)

        def set_document(self, document):
            normalized = normalize_document(document)
            if not binding_matches(normalized, self.capture_binding_provider()):
                raise ValueError("Frame rendering flow belongs to a different capture session")
            self._reset_export_context()
            self.document = normalized
            self.save_image.setEnabled(self._export_job is None)
            self.title.setText(normalized["title"])
            self.summary.setText(normalized["summary"])
            self._populate_outline()
            self.view.setEnabled(True)
            self.view.set_document(normalized)
            coverage = normalized["coverage"]
            source = (QtCore.QCoreApplication.translate("ReplayLabProjects", "Complete source coverage")
                      if coverage.get("sourceComplete") else QtCore.QCoreApplication.translate(
                          "ReplayLabProjects", "Source coverage is incomplete"))
            self.status.setText(_format(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "%1 stages · %2/%3 actions covered · %4"), len(normalized["stages"]),
                coverage.get("coveredActionCount", 0), coverage.get("totalActionCount", 0), source))
            gaps = normalized.get("quality", {}).get("gaps", [])
            if gaps:
                self.status.setText(self.status.text() + "\n" + _format(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "%1 analysis gaps remain; see the affected stages."), len(gaps)))
                self.status.setToolTip("\n".join(_quality_gap_text(gap) for gap in gaps))
            else:
                self.status.setToolTip("")
            self._search_changed(self.search.text())
            return self.get_state()

        def get_state(self):
            return {"visible": self._alive and self.top.isVisible(),
                    "flowId": (self.document or {}).get("flowId", ""),
                    "docFlowId": (self.document or {}).get("flowId", ""),
                    "captureId": (self.document or {}).get("captureId", ""),
                    "capturePath": (self.document or {}).get("capturePath", ""),
                    "captureSessionId": (self.document or {}).get("captureSessionId", ""),
                    "currentCapture": bool(self._is_current()),
                    "stageCount": len((self.document or {}).get("stages", [])),
                    "outlineStageId": (str(self.outline.currentItem().data(QtCore.Qt.UserRole))
                                       if self.outline.currentItem() is not None else ""),
                    "previewCount": sum(len(stage["images"]) for stage in (self.document or {}).get("stages", [])),
                    "visiblePreviewCount": len(self.view._previews),
                    "loadedPreviewCount": len(set(item.preview["path"] for item in self.view._previews if item.pixmap is not None)),
                    "pendingPreviewCount": len(self.view._pending),
                    "failedPreviewCount": sum(item.failed for item in self.view._previews),
                    "animationCount": len(self.view._animations),
                    "playingAnimationCount": sum(item.playing for item in self.view._animations),
                    "export": dict(self.export_state),
                    "channelGroupCount": sum(len(texture_channel_groups(stage["images"])[0])
                                             for stage in (self.document or {}).get("stages", []))}

        def capture_changed(self, unused=None):
            if not self._alive:
                return
            if not self._is_current():
                self._reset_export_context()
                self.document = None
                self.save_image.setEnabled(False)
                self.view.set_document(None)
                self.view.setEnabled(False)
                self._populate_outline()
                self.title.clear()
                self.summary.clear()
                self._matches, self._match_index = [], -1
                self.search_status.clear()
                self.status.setToolTip("")
                self.status.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Ask Bread AI to draw this frame's rendering flow."))

        def _clear_export_message(self):
            if self._alive:
                self._export_message_timer.stop()
                self.export_status.clear()
                self.export_status.hide()

        def _reset_export_context(self):
            # A refreshed diagram is a new export context even when its flow ID
            # is unchanged. A queued PNG writer may finish after an RDC switch;
            # its result must never become the new capture's UI status/receipt.
            self._export_generation += 1
            self._clear_export_message()
            self.export_state = {}
            self.cancel_export.hide()
            if self._export_job is not None:
                # Let an already-running encoder finish its file safely, then
                # release the job through its generation-bound completion.
                self._export_job.cancel()

        def _choose_export(self):
            if not self._is_current() or self._export_job is not None:
                return
            generation = self._export_generation
            capture = os.path.splitext(self.document["capturePath"])[0]
            selected = QtWidgets.QFileDialog.getSaveFileName(self.top,
                QtCore.QCoreApplication.translate("ReplayLabProjects", "Save frame rendering flow"),
                # i18n-ignore: stable export filename suffix, not a UI label.
                capture + "-frame-flow.png",
                QtCore.QCoreApplication.translate("ReplayLabProjects", "PNG image (*.png)"))
            path = selected[0] if isinstance(selected, (list, tuple)) else selected
            if path and generation == self._export_generation and self._is_current():
                self.export_image(path)

        def export_image(self, path):
            """Non-modal API shared by the toolbar and native acceptance tests."""
            if not self._is_current() or self._export_job is not None:
                raise ValueError("A current diagram is required and only one export can run")
            path = os.path.abspath(str(path))
            if not os.path.isdir(os.path.dirname(path)):
                raise ValueError("The export directory does not exist")
            self._clear_export_message()
            self._export_generation += 1
            generation = self._export_generation
            self._export_job_generation = generation
            self.export_state = {"running": True, "ok": False, "paths": []}
            self.save_image.setEnabled(False)
            self.cancel_export.show()
            self.export_status.show()
            self._export_progress(0, 1, generation)
            try:
                self._export_job = _DiagramExport(self.view, path,
                    lambda value, maximum: self._export_progress(value, maximum, generation),
                    lambda result: self._export_finished(result, generation), self.top)
            except Exception as error:
                self._export_finished({"ok": False, "paths": [], "error": str(error)}, generation)
                raise
            return dict(self.export_state)

        def _export_progress(self, value, maximum, generation):
            if generation != self._export_generation or not self._is_current():
                return
            self.export_status.setText(QtCore.QCoreApplication.translate("ReplayLabProjects",
                "Saving diagram… %1%").replace("%1", str(int(100 * value / max(1, maximum)))))

        def _export_finished(self, result, generation):
            if not self._alive or generation != self._export_job_generation:
                return
            self._export_job = None
            self._export_job_generation = None
            self.cancel_export.hide()
            self.save_image.setEnabled(self._is_current())
            if not self._is_current():
                self._reset_export_context()
                return
            if generation != self._export_generation:
                return
            self.export_state = dict(result, running=False)
            paths = result.get("paths", [])
            if result.get("ok"):
                message = (QtCore.QCoreApplication.translate("ReplayLabProjects", "Saved image: %1").replace(
                    "%1", paths[0]) if len(paths) == 1 else _format(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Saved the complete diagram as %1 images:\n%2"), len(paths), "\n".join(paths)))
            elif result.get("error") == "cancelled":
                message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Image export cancelled.")
                if paths:
                    message += "\n" + "\n".join(paths)
            else:
                message = QtCore.QCoreApplication.translate("ReplayLabProjects", "Image export failed: %1").replace(
                    "%1", str(result.get("error", "")))
                if paths:
                    message += "\n" + "\n".join(paths)
            self.export_status.setText(message)
            self.export_status.show()
            self._export_message_timer.start(EXPORT_MESSAGE_MILLISECONDS)

        def _navigate(self, target):
            if not self._is_current():
                self.capture_changed()
                return False
            target = dict(target) if isinstance(target, dict) else {"eventId": int(target)}
            event_id = target.get("eventId", 0)
            if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
                return False
            action = self.ctx.GetAction(int(event_id))
            if action is None or int(action.eventId) != int(event_id):
                self.status.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "This EID is no longer available in the current capture."))
                return False
            if callable(self.navigation_callback):
                return self.navigation_callback(target) is not False
            self.ctx.SetEventID([], int(event_id), int(event_id))
            return True

        def _search_changed(self, text):
            self._matches = matching_stage_ids(self.document, text) if str(text).strip() else []
            self._match_index = -1
            self.search_status.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "%1 matches").replace("%1", str(len(self._matches)))
                if str(text).strip() else "")
            if self._matches:
                self._next_match()

        def _next_match(self):
            if self._matches:
                self._match_index = (self._match_index + 1) % len(self._matches)
                self.view.focus_stage(self._matches[self._match_index])

        def shutdown(self):
            if self._alive:
                self._outline_sync.stop()
                self._reset_export_context()
                if self._export_job is not None:
                    self._export_job._finish("cancelled")
                self.view.shutdown()
                self._alive = False
                self.top.close()
                self.top.deleteLater()
else:
    class FrameRenderFlowPanel(object):
        def __init__(self, *args, **kwargs):
            raise RuntimeError("PySide2 is required for the frame rendering flow panel")
