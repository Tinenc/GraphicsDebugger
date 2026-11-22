"""Bounded, exact-event volume slices prepared before the diagram is published."""
from __future__ import annotations

from copy import deepcopy
import json
import math


MAX_SLICES = 32
MAX_IMAGES = 1024
MAX_ESTIMATED_READBACK_BYTES = 2 * 1024 * 1024 * 1024


def _field(preview, name, default=None):
    return preview.get(name, (preview.get("displayMapping") or {}).get(name, default))


def _integer(value, name, minimum=0):
    if type(value) is not int or not minimum <= value <= 0xffffffff:
        raise ValueError("Volume preview requires valid " + name)
    return value


def slice_plan(preview, maximum=MAX_SLICES):
    """Sample only the bound mip's W interval, retaining the selected slice."""
    depth = _integer(_field(preview, "mipDepth"), "mip depth", 1)
    first = _integer(_field(preview, "viewSliceFirst", 0), "first W slice")
    count = _integer(_field(preview, "viewSliceCount", depth), "W slice count", 1)
    selected = _integer((preview.get("displayMapping") or {}).get("slice", first), "selected W slice")
    if first + count > depth or not first <= selected < first + count:
        raise ValueError("Volume preview W interval does not contain the selected slice")
    if count <= maximum:
        return list(range(first, first + count))
    if maximum == 1:
        return [selected]
    if maximum == 2:
        furthest = first if selected - first >= first + count - 1 - selected else first + count - 1
        return sorted({selected, furthest})
    # Reserve one slot for the exact selected W slice; endpoints are always kept.
    sampled = {first + (index * (count - 1)) // (maximum - 2)
               for index in range(maximum - 1)}
    sampled.add(selected)
    return sorted(sampled)


def display_range(preview):
    """One fixed scale across Z, so scrubbing does not change apparent density."""
    mapping = preview.get("displayMapping") or {}
    low, high = mapping.get("blackPoint", 0.0), mapping.get("whitePoint", 1.0)
    if (type(low) not in (int, float) or type(high) not in (int, float)
            or not math.isfinite(low) or not math.isfinite(high) or low >= high):
        raise ValueError("Volume preview requires the parent image's finite display range")
    if low == 0.0 and high == 1.0:
        return {"rangeMode": "native", "blackPoint": None, "whitePoint": None}
    return {"rangeMode": "explicit", "blackPoint": low, "whitePoint": high}


def precache_volumes(document, allocate, capture, accept):
    """One replay transaction per distinct volume; scrubbing never invokes replay."""
    result = {"attempted": 0, "prepared": 0, "failed": 0, "errors": []}
    remaining, verified, revision, original_event = MAX_IMAGES, {}, None, None
    fatal = False
    parents = [image for stage in document["stages"] for image in stage["images"]
               if image.get("group") not in ("channels", "animation-frames")
               and _field(image, "textureDimension") == 3]
    for preview in parents:
        if fatal:
            result["deferred"] = result.get("deferred", 0) + 1
            continue
        result["attempted"] += 1
        preview.pop("volumePreview", None)
        try:
            if preview.get("attachmentKind") not in ("rtv", "uav", "srv"):
                raise ValueError("Volume preview requires an exact bound texture view")
            count = _integer(preview.get("componentCount"), "format component count", 1)
            if count > 4:
                raise ValueError("Volume preview format has too many components")
            channels = (["rgb"] if count >= 3 else []) + list("rgba"[:count])
            # RenderDoc may read the entire volume for each SaveTexture call.
            # Bound estimated readback before allocating the per-slice files;
            # reducing the displayed PNG size alone does not reduce this work.
            source_bytes = _field(preview, "textureByteSize", 0)
            maximum = MAX_SLICES
            if type(source_bytes) is int and source_bytes > 0:
                maximum = min(maximum, max(1, MAX_ESTIMATED_READBACK_BYTES // (source_bytes * len(channels))))
            z_values = slice_plan(preview, maximum)
            mapping = preview.get("displayMapping") or {}
            transform = display_range(preview)
            key = json.dumps([preview["eventId"], preview["resourceId"], preview["attachmentKind"],
                              preview.get("attachmentIndex", mapping.get("attachmentIndex")),
                              preview.get("attachmentStage", mapping.get("attachmentStage")),
                              mapping.get("mip", 0), mapping.get("sample", 0),
                              mapping.get("valueDecode", "native"), mapping.get("flipY", False),
                              z_values, count, transform], sort_keys=True)
            if key in verified:
                preview["volumePreview"] = deepcopy(verified[key])
                result["prepared"] += 1
                continue
            budget = len(z_values) * len(channels)
            if budget > remaining:
                raise ValueError("Volume previews reached the diagram's bounded slice image budget")
            remaining -= budget
            requests, allocated = [], {}
            for z in z_values:
                for channel in channels:
                    request = dict(allocate(preview, channel), slice=z, channel=channel, **transform)
                    requests.append(request)
                    allocated[(z, channel)] = request
            reply = capture(preview, requests)
            if reply.get("captureSessionId") != document["captureSessionId"]:
                raise ValueError("Volume precache returned a different capture session")
            if (preview.get("channelCacheRevision") is not None
                    and reply.get("replayRevision") != preview["channelCacheRevision"]):
                raise ValueError("Volume precache changed the parent channel replay revision")
            if revision is not None and (reply.get("replayRevision") != revision
                                          or reply.get("originalEventId") != original_event):
                raise ValueError("Volume precache changed replay revision or the selected event")
            revision, original_event = deepcopy(reply.get("replayRevision")), reply.get("originalEventId")
            rows = reply.get("receipts", [])
            if not isinstance(rows, list) or len(rows) > budget:
                raise ValueError("Volume precache returned an invalid slice receipt list")
            if (reply.get("replayRevision") is None or reply.get("componentCount") != count
                    or reply.get("mipDepth") != _field(preview, "mipDepth")
                    or rows and (reply.get("viewSliceFirst") != _field(preview, "viewSliceFirst", 0)
                                 or reply.get("viewSliceCount") != _field(preview, "viewSliceCount", _field(preview, "mipDepth")))):
                raise ValueError("Volume precache returned different physical texture dimensions")
            accepted, seen, errors = [], set(), list(reply.get("errors", []))
            for item in rows:
                identity = (item.get("slice"), item.get("channel"))
                if identity not in allocated or identity in seen:
                    raise ValueError("Volume precache returned an unrelated or duplicated slice")
                seen.add(identity)
                if any(item.get(name) != value for name, value in transform.items()):
                    raise ValueError("Volume precache changed the shared display range")
                z, channel = identity
                parent = dict(preview, displayMapping=dict(mapping, slice=z))
                try:
                    image = accept(parent, item, allocated[identity], reply)
                    image.update(slice=z, channel=channel)
                    accepted.append(image)
                except Exception as error:
                    errors.append({"slice": z, "channel": channel, "error": str(error)[:1200]})
            accepted.sort(key=lambda image: (image["slice"], channels.index(image["channel"])))
            complete = len(accepted) == budget and not errors
            volume = {"depth": _field(preview, "mipDepth"), "mip": mapping.get("mip", 0),
                      "viewSliceFirst": _field(preview, "viewSliceFirst", 0),
                      "viewSliceCount": _field(preview, "viewSliceCount", _field(preview, "mipDepth")),
                      "selectedSlice": mapping.get("slice", 0), "sampled": len(z_values) < _field(preview, "viewSliceCount", _field(preview, "mipDepth")),
                      "slices": accepted, "complete": complete,
                      "captureSessionId": document["captureSessionId"], "replayRevision": revision}
            if accepted:
                preview["volumePreview"] = volume
            if complete:
                verified[key] = deepcopy(volume)
                result["prepared"] += 1
            else:
                result["failed"] += 1
                result["errors"].append({"eventId": preview["eventId"], "resourceId": preview["resourceId"],
                                         "missingSliceCount": budget - len(accepted), "errors": errors})
                fatal = "bread-replay-unavailable" in json.dumps(errors)
        except Exception as error:
            result["failed"] += 1
            result["errors"].append({"eventId": preview.get("eventId"), "resourceId": preview.get("resourceId"),
                                     "error": str(error)[:1200]})
            fatal = "bread-replay-unavailable" in str(error)
    result["complete"] = not result["failed"] and not fatal
    return result
