"""Presentation choices from verified previews and declared resource relationships.

No replay, new captures or numerical-equivalence claims. Image similarity only
selects a useful visual comparison after a resource relationship is established.
"""
from __future__ import annotations

from collections import OrderedDict
from hashlib import sha256
from io import BytesIO
import math
from pathlib import Path
import threading

from PIL import Image

from .frame_flow_presentation import process_presentation_review


_MEDIA = ("imageIndex", "comparisonIndex", "animationIndex")
_CACHE = OrderedDict()
_LOCK = threading.RLock()
_MAX_CACHE = 256
_MAX_BYTES = 32 * 1024 * 1024


def comparison_compatibility(left, right):
    """Compare displayed snapshots using the same subresource and mapping."""
    reasons = []
    if (left.get("width"), left.get("height")) != (right.get("width"), right.get("height")):
        reasons.append("different-image-size")
    channel = lambda image: "rgba" if image.get("channel") == "rgb" else image.get("channel")
    if channel(left) != channel(right):
        reasons.append("different-channel")
    lm, rm = left.get("displayMapping", {}), right.get("displayMapping", {})
    if any(lm.get(key) != rm.get(key) for key in ("rangeMode", "blackPoint", "whitePoint", "depthInterpretation")):
        reasons.append("different-display-mapping")
    if (lm.get("valueDecode") or "native") != (rm.get("valueDecode") or "native"):
        reasons.append("different-value-decoding")
    if any(lm.get(key, 0) != rm.get(key, 0) for key in ("mip", "slice", "sample")):
        reasons.append("different-subresource")
    return reasons


def _small_rgb(preview):
    path = Path(preview["path"])
    if not path.is_absolute() or path.suffix.lower() != ".png":
        raise ValueError("Similarity requires a verified PNG preview")
    stat = path.stat()
    if not 0 < stat.st_size <= _MAX_BYTES:
        raise ValueError("Preview exceeds the bounded similarity byte budget")
    key = (str(path), preview["sha256"], stat.st_size, stat.st_mtime_ns,
           preview["width"], preview["height"])
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key], True
    with path.open("rb") as stream:
        raw = stream.read(_MAX_BYTES + 1)
    if len(raw) != stat.st_size or sha256(raw).hexdigest() != preview["sha256"]:
        raise ValueError("Preview changed after evidence verification")
    with Image.open(BytesIO(raw)) as image:
        if (image.format != "PNG" or image.size != (preview["width"], preview["height"]) or
                image.width * image.height > 64 * 1024 * 1024):
            raise ValueError("Preview dimensions do not match bounded evidence")
        # GPU alpha can carry data. Keep its RGB, including where alpha is zero,
        # before resampling; transparent pixels must not erase scene content.
        rgb = image.convert("RGB")
        try:
            rgb.thumbnail((96, 96), Image.Resampling.BOX)
            value = (rgb.size, rgb.tobytes())
        finally:
            rgb.close()
    with _LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > _MAX_CACHE:
            _CACHE.popitem(last=False)
    return value, False


def preview_similarity(left, right):
    reasons = comparison_compatibility(left, right)
    result = {"method": "display-rgb-small-preview/v1", "numericDifferenceEvidence": False,
              "comparisonWarnings": reasons, "similar": False}
    if reasons:
        return dict(result, status="incompatible")
    # Unknown mapping cannot support an automatic near-identical choice.
    for image in (left, right):
        mapping = image.get("displayMapping", {})
        points = [mapping.get(key) for key in ("blackPoint", "whitePoint")]
        if (any(type(value) not in (int, float) or not math.isfinite(value) for value in points) or
                points[0] >= points[1] or image.get("channel") not in ("rgb", "rgba", "r", "g", "b", "a", "depth")):
            return dict(result, status="mapping-unavailable")
    try:
        (size, a), cached_a = _small_rgb(left)
        (other_size, b), cached_b = _small_rgb(right)
    except (OSError, KeyError, ValueError, Image.DecompressionBombError):
        return dict(result, status="preview-unavailable")
    if size != other_size or not a or len(a) != len(b):
        return dict(result, status="preview-unavailable")
    differences = [abs(x - y) for x, y in zip(a, b)]
    mean = sum(differences) / (len(differences) * 255.0)
    changed = sum(max(differences[i:i + 3]) > 10 for i in range(0, len(differences), 3)) / (len(differences) / 3)
    return dict(result, status="compared", meanAbsoluteDifference=round(mean, 6),
                changedPixelFraction=round(changed, 6), sampleWidth=size[0], sampleHeight=size[1],
                cacheHit=cached_a and cached_b, similar=mean <= 0.04 and changed <= 0.18)


def _attach_graph_media(stage, actions, observations):
    graph, images = stage.get("processGraph"), stage.get("images", [])
    if not graph:
        return 0
    # Presentation enrichment must not rewrite an unresolved relationship into
    # a different assertion. Leave existing graph findings for the Agent.
    baseline = process_presentation_review(stage, actions, observations)
    if baseline["gaps"]:
        return 0
    algorithm = stage.get("algorithm") or {}
    outputs = algorithm.get("outputs", [])
    linked = 0
    for node in graph["nodes"]:
        if any(key in node for key in _MEDIA) or not node.get("eventId"):
            continue
        candidates = [i for i, image in enumerate(images) if image.get("eventId") == node["eventId"]
                      and image.get("group", "outputs") == "outputs"
                      and image.get("channel") in ("rgb", "rgba", "depth")]
        declared = {row["resourceId"] for row in outputs if row["eventId"] == node["eventId"]}
        declared.update(row["resourceId"] for row in algorithm.get("inputs", [])
                        if row.get("producerEventId") == node["eventId"])
        declared.update(row["resourceId"] for row in algorithm.get("integration", [])
                        if row.get("sourceEventId") == node["eventId"])
        if declared:
            candidates = [i for i in candidates if images[i].get("resourceId") in declared]
        primary = [i for i in candidates if images[i].get("presentation", "primary") == "primary"]
        candidates = primary or candidates
        # Never pick a random MRT or a component preview for an operation.
        if len(candidates) == 1:
            node["imageIndex"] = candidates[0]
            # An operation -> output edge is not output-A -> output-B. Trial
            # one attachment at a time; preserve the operation if its preview
            # would introduce an unsupported or contradictory resource path.
            candidate = process_presentation_review(stage, actions, observations)
            if (candidate["gaps"] or any(candidate[key] > baseline[key] for key in (
                    "repeatedSnapshotPathCount", "unverifiedTransferPathCount", "unverifiedSubresourcePathCount")) or
                    (baseline["presentation"] == "show" and candidate["presentation"] != "show")):
                # A unique RID still may be the wrong mip/layer or a repeated
                # result. Keep the evidenced operation instead of letting an
                # optional preview suppress the otherwise valid render chain.
                node.pop("imageIndex")
                continue
            baseline = candidate
            linked += 1
    return linked


def _declared_pairs(stage, actions, observations):
    """Only successive views of the same resource on a declared calculation path."""
    images, algorithm = stage.get("images", []), stage.get("algorithm") or {}
    possible = set()
    for row in algorithm.get("inputs", []):
        producer, event, rid = row.get("producerEventId"), row.get("eventId"), row.get("resourceId")
        if producer is None or producer == event:
            continue
        before = [i for i, image in enumerate(images) if image.get("eventId") == producer
                  and image.get("resourceId") == rid and image.get("group") in ("inputs", "outputs")]
        after = [i for i, image in enumerate(images) if image.get("eventId") == event
                 and image.get("resourceId") == rid and image.get("group") == "outputs"]
        possible.update((a, b) for a in before for b in after)
    graph = stage.get("processGraph")
    if graph:
        nodes = {node["id"]: node for node in graph["nodes"]}
        adjacency = {key: [] for key in nodes}
        for edge in graph["edges"]:
            adjacency[edge["from"]].append(edge["to"])
        for identity, node in nodes.items():
            if "imageIndex" not in node:
                continue
            pending, visited = list(adjacency[identity]), set()
            while pending:
                target = pending.pop()
                if target in visited:
                    continue
                visited.add(target)
                pending.extend(adjacency[target])
                if "imageIndex" in nodes[target]:
                    possible.add((node["imageIndex"], nodes[target]["imageIndex"]))
    result = []
    for left, right in sorted(possible):
        a, b = images[left], images[right]
        ae, be = a.get("eventId"), b.get("eventId")
        if (a.get("resourceId") != b.get("resourceId") or not a.get("resourceId") or
                ae not in actions or be not in actions or actions[ae]["executionIndex"] >= actions[be]["executionIndex"] or
                a.get("group") in ("channels", "animation-frames") or b.get("group") in ("channels", "animation-frames")):
            continue
        check = dict(stage, processSteps=[], processGraph={
            "nodes": [{"id": "before", "imageIndex": left}, {"id": "after", "imageIndex": right}],
            "edges": [{"from": "before", "to": "after"}]})
        if process_presentation_review(check, actions, observations)["presentation"] == "show":
            result.append((left, right))
    return result


def prepare_visuals(stage, actions, observations):
    """Attach representative existing media, then choose useful declared comparisons."""
    review = {"linkedGraphImages": _attach_graph_media(stage, actions, observations), "autoComparisons": 0,
              "similarityChecks": [], "numericDifferenceEvidence": False}
    graph = stage.get("processGraph")
    images, comparisons = stage.get("images", []), stage.get("comparisons", [])
    existing = {(row["leftImageIndex"], row["rightImageIndex"]) for row in comparisons}
    candidates = _declared_pairs(stage, actions, observations)
    for left, right in sorted(existing | set(candidates))[:32]:
        metric = preview_similarity(images[left], images[right])
        review["similarityChecks"].append({"leftImageIndex": left, "rightImageIndex": right, **metric})
        if ((left, right) in existing or (left, right) not in candidates or
                not metric["similar"] or len(comparisons) >= 8):
            continue
        image = images[right]
        comparisons.append({"leftImageIndex": left, "rightImageIndex": right,
                            "label": image.get("title", ""), "description": image.get("description", ""),
                            "relationship": "before-after", "visualComparable": True,
                            "comparisonWarnings": [], "differentResource": False,
                            "numericDifferenceEvidence": False})
        index = len(comparisons) - 1
        if graph:
            for node in graph["nodes"]:
                if node.get("imageIndex") == right:
                    node.pop("imageIndex")
                    node["comparisonIndex"] = index
        existing.add((left, right))
        review["autoComparisons"] += 1
    # A gallery image does not by itself establish another render stage.
    # Preserve the authored graphs as evidence, but only show arrows supported
    # by a real cross-event resource path. Check legacy steps independently so
    # an omitted graph cannot silently fall back to another invented sequence.
    graph_review = process_presentation_review(dict(stage, processSteps=[]), actions, observations)
    steps_review = process_presentation_review(dict(stage, processGraph=None), actions, observations)
    review.update(graphPresentation=graph_review["presentation"],
                  processStepsPresentation=steps_review["presentation"],
                  graphPresentationReason=graph_review["reason"],
                  processStepsPresentationReason=steps_review["reason"],
                  verifiedGraphTransferCount=graph_review["verifiedTransferCount"],
                  verifiedGraphMediaTransferCount=graph_review["verifiedMediaTransferCount"],
                  repeatedGraphSnapshotPathCount=graph_review["repeatedSnapshotPathCount"],
                  unverifiedGraphTransferPathCount=graph_review["unverifiedTransferPathCount"],
                  unverifiedGraphSubresourcePathCount=graph_review["unverifiedSubresourcePathCount"],
                  verifiedStepTransferCount=steps_review["verifiedTransferCount"],
                  verifiedStepMediaTransferCount=steps_review["verifiedMediaTransferCount"],
                  repeatedStepSnapshotPathCount=steps_review["repeatedSnapshotPathCount"],
                  unverifiedStepTransferPathCount=steps_review["unverifiedTransferPathCount"],
                  unverifiedStepSubresourcePathCount=steps_review["unverifiedSubresourcePathCount"])
    return review
