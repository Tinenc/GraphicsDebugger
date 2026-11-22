"""Capture-bound, asynchronous channel previews for the frame diagram popup.

No Qt objects or filesystem work are touched by the UI request path. The caller
supplies UI-thread snapshots and an executor which delivers completions on UI.
"""
import collections
import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid


def mutation_identity(bridge):
    records = bridge._event_cbuffer_snapshots.copy()
    return (bridge._shared_shader_mutation_epoch, bridge._event_cbuffer_release_serial,
            tuple(sorted((key, value.get("revision"), value.get("currentRawSha256"))
                         for key, value in records.items())))


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not re.match(r"^[0-9]+$", str(value)):
        raise ValueError("Invalid channel preview " + name)
    value = int(value)
    if value < minimum or value > (0xFFFFFFFFFFFFFFFF if name == "resourceId" else 0xFFFFFFFF):
        raise ValueError("Invalid channel preview " + name)
    return value


def request_identity(preview):
    mapping = preview.get("displayMapping") or {}
    value_decode = mapping.get("valueDecode") or "native"
    if value_decode not in ("native", "float32-bits"):
        raise ValueError("Unsupported channel preview value decoding")
    resource = str(preview.get("resourceId", "")).replace("ResourceId::", "")
    kind = preview.get("attachmentKind") or mapping.get("attachmentKind")
    if kind not in ("rtv", "dsv", "uav", "srv", "present"):
        raise ValueError("Channel preview attachment identity is missing")
    identity = {
        "event_id": _integer(preview.get("eventId"), "eventId", 1),
        "resource_id": _integer(resource, "resourceId", 1),
        "attachment_kind": kind,
        "attachment_index": _integer(mapping.get("attachmentIndex", preview.get("attachmentIndex")), "attachmentIndex"),
        "stage": preview.get("attachmentStage") or mapping.get("attachmentStage") or "fragment",
        "mip": _integer(mapping.get("mip"), "mip"),
        "slice": _integer(mapping.get("slice"), "slice"),
        "sample": _integer(mapping.get("sample"), "sample"),
    }
    if value_decode != "native":
        identity["value_decode"] = value_decode
    flip_y = mapping.get("flipY", False)
    if type(flip_y) is not bool:
        raise ValueError("Invalid channel preview orientation")
    if flip_y:
        identity["flip_y"] = True
    return identity


def channel_plan(preview, component_count=None):
    """Semantic grouping plus complete real-component coverage, without guessing.

    A single recorded Alpha image is not a plan for the entire texture. Missing
    groups stay physical channels until the Agent supplies their actual roles.
    """
    count = component_count if component_count is not None else preview.get("componentCount", 0)
    count = count if type(count) is int and count in (1, 2, 3, 4) else 4
    mapping = preview.get("displayMapping") or {}
    if (preview.get("attachmentKind") or mapping.get("attachmentKind")) == "dsv":
        return [{"channel": "depth"}]
    available = set("rgba"[:count])
    declared = preview.get("channelGroups")
    if declared == []:
        return []
    if declared is None:
        # Only explicit channel labels from prior semantic analysis are usable;
        # a title containing "color" is not evidence of its packed layout.
        meaning = str(preview.get("channelMeaning") or "")
        declared = [{"channel": match.group(1).lower(), "description": match.group(2).strip()}
                    for match in re.finditer(r"(?:^|[;；\n])\s*(RGB|R|G|B|A)\s*[:：]\s*([^;；\n]+)", meaning, re.I)]
    if not isinstance(declared, list) or len(declared) > 4:
        raise ValueError("Channel groups must contain at most four semantic groups")
    groups, covered = [], set()
    for group in declared:
        if not isinstance(group, dict) or group.get("channel") not in ("rgb", "r", "g", "b", "a", "depth"):
            raise ValueError("Channel group uses an unsupported component selection")
        channel = group["channel"]
        components = set("r" if channel == "depth" else channel)
        if not components.issubset(available):
            if len(components) == 1:
                continue  # A stale Alpha label cannot create an absent channel.
            raise ValueError("Channel group is absent from the actual texture format")
        if components.intersection(covered):
            raise ValueError("Channel groups overlap physical components")
        covered.update(components)
        groups.append({key: str(group[key])[:1200] for key in ("channel", "label", "description") if key in group})
    for channel in "rgba"[:count]:
        if channel not in covered:
            groups.append({"channel": channel})
    groups.sort(key=lambda group: {"rgb": 0, "r": 0, "g": 1, "b": 2, "a": 3, "depth": 0}[group["channel"]])
    return groups


def existing_channel_semantics(document, parent):
    """Copy only channels associated with this exact parent, never another RT."""
    identity = request_identity(parent)
    result = {}
    for stage in (document or {}).get("stages", []):
        images = stage.get("images", [])
        parents = [index for index, image in enumerate(images)
                   if image.get("group", "outputs") != "channels" and
                   all(image.get(key) == parent.get(key) for key in
                       ("eventId", "resourceId", "sha256", "title", "description"))]
        if not parents:
            continue
        for image in images:
            if image.get("group") != "channels":
                continue
            try:
                if request_identity(image) != identity:
                    continue
            except ValueError:
                continue
            if image.get("parentImageIndex") is not None and image["parentImageIndex"] not in parents:
                continue
            result[str(image.get("channel", "")).lower()] = copy.deepcopy(image)
    return result


def _verify_png(image):
    path = image.get("path", "")
    if (not os.path.isabs(path) or os.path.islink(path) or
            os.path.normcase(os.path.realpath(path)) != os.path.normcase(os.path.abspath(path))):
        raise ValueError("Channel preview PNG is unavailable or redirected")
    size = os.path.getsize(path)
    if size < 8 or size > 32 * 1024 * 1024:
        raise ValueError("Channel preview PNG exceeds the bounded read size")
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("Channel preview is not a PNG")
        stream.seek(0)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != image.get("sha256"):
        raise ValueError("Channel preview PNG hash changed")


def _preview(receipt, identity, parent, semantics, channel):
    artifact = receipt.get("artifact") or {}
    attachment, output = artifact.get("attachment") or {}, artifact.get("output") or {}
    if (receipt.get("eventId") != identity["event_id"] or
            receipt.get("exactEventBinding") is not True or
            artifact.get("status") != "saved" or
            (artifact.get("display") or {}).get("valueDecode", "native") != identity.get("value_decode", "native") or
            (artifact.get("display") or {}).get("flipY", False) is not identity.get("flip_y", False) or
            attachment.get("resourceId") != identity["resource_id"] or
            any(attachment.get(key) != identity[key] for key in ("mip", "slice", "sample")) or
            attachment.get("kind") != identity["attachment_kind"] or
            attachment.get("index") != identity["attachment_index"] or
            attachment.get("stage") != identity["stage"] or
            (artifact.get("display") or {}).get("channel") != ("rgba" if channel == "rgb" else channel)):
        raise ValueError("Channel preview receipt does not match its requested binding")
    # Preserve verified business names from a pre-existing channel. For a newly
    # inspected component, only use an explicitly labelled component meaning.
    prior = semantics.get(channel, {})
    meaning = str(prior.get("channelMeaning") or "")
    if not meaning:
        meanings = str(parent.get("channelMeaning") or "")
        match = re.search(r"(?:^|[;；\n])\s*" + channel + r"\s*[:：]\s*([^;；\n]+)", meanings, re.I)
        meaning = match.group(1).strip() if match else ""
    display = artifact.get("display") or {}
    dimensions = (output.get("reduction") or {}).get("scaledTo") or {}
    image = {key: copy.deepcopy(prior.get(key, "")) for key in
             ("title", "description", "semantic", "evidence", "label")}
    image.update({
        "title": image["title"] or parent.get("title", ""),
        "channelMeaning": meaning, "group": "channels", "channel": channel,
        "eventId": identity["event_id"], "resourceId": str(identity["resource_id"]),
        "path": output.get("path"), "sha256": output.get("sha256"),
        "format": attachment.get("format", ""), "componentCount": attachment.get("componentCount", 0),
        "width": dimensions.get("width", attachment.get("viewWidth", 0)),
        "height": dimensions.get("height", attachment.get("viewHeight", 0)),
        "attachmentKind": identity["attachment_kind"], "attachmentStage": identity["stage"],
        "displayMapping": dict(display, mip=identity["mip"], slice=identity["slice"],
                               sample=identity["sample"], attachmentKind=identity["attachment_kind"],
                               attachmentIndex=identity["attachment_index"], attachmentStage=identity["stage"]),
    })
    _verify_png(image)
    return image


def capture_channel_batch(bridge, parent, snapshot, semantics, cancelled, requests=None):
    """One replay transaction for all physical channels, restoring on any failure."""
    identity = request_identity(parent)
    replay, session = snapshot["replay"], snapshot["session"]

    def checkpoint():
        if (cancelled() or session != bridge._capture_session_id or
                snapshot["flowGeneration"] != bridge._frame_render_flow_generation or
                snapshot["mutationEpoch"] != mutation_identity(bridge) or
                bridge._replay_foreground_generation(replay) != snapshot["foregroundGeneration"]):
            raise RuntimeError("Channel preview request was superseded")

    checkpoint()
    folder = None
    if requests is None:
        folder = bridge._frame_flow_artifact_root() / "frame-flow-channels" / session
        folder.mkdir(parents=True, exist_ok=True)
    elif not isinstance(requests, dict) or len(requests) > 6:
        raise ValueError("Channel batch requires bounded Host-owned request directories")
    result, receipts, failures, errors = [], [], [], []
    metadata = {"componentCount": 0, "channelGroups": []}

    def collect(ctrl):
        selected = False
        try:
            checkpoint()
            texture = next((item for item in ctrl.GetTextures() or []
                            if bridge._resource_id_int(item.resourceId) == identity["resource_id"]), None)
            count = int(getattr(getattr(texture, "format", None), "compCount", 0))
            if count not in (1, 2, 3, 4):
                raise ValueError("Channel preview texture component count is unavailable")
            plan = channel_plan(parent, count)
            metadata.update(componentCount=count, channelGroups=plan)
            channels = [group["channel"] for group in plan]
            reused = {}
            if requests is None:
                for image in snapshot.get("cachedChannelImages", []):
                    if (image.get("channel") in channels and request_identity(image) == identity):
                        try:
                            _verify_png(image)
                            reused[image["channel"]] = copy.deepcopy(image)
                        except (OSError, ValueError):
                            pass
            result.extend(reused.values())
            if not channels or requests is not None and (len(channels) <= 1 or not set(channels).intersection(requests)):
                return
            selected = True
            bridge._set_frame_event(ctrl, identity["event_id"], True)
            for channel in channels:
                checkpoint()
                if channel in reused:
                    continue
                if requests is not None and channel not in requests:
                    # A publish retry may request only the missing components.
                    continue
                # A constant channel is meaningful in the popup (e.g. opaque A).
                # Retry its flat automatic range once at native 0..1, never infer
                # a channel from another image or fabricate a missing Alpha.
                for range_mode in ("visible-distribution", "native"):
                    if requests is None:
                        request_id = uuid.uuid4().hex
                        request_dir = folder / ("eid-%d-%s" % (identity["event_id"], request_id))
                        request_dir.mkdir()
                    else:
                        request_id = requests[channel].get("request_id")
                        request_dir = requests[channel].get("artifact_dir")
                    params = dict(identity, channel="rgba" if channel == "rgb" else channel, range_mode=range_mode,
                                  max_dimension=1024, request_id=request_id,
                                  artifact_dir=str(request_dir), expected_capture_session_id=session)
                    try:
                        receipt = bridge._capture_event_texture_evidence(
                            params, _controller=ctrl, _original_event=snapshot["originalEvent"],
                            _expected_resource_id=identity["resource_id"])
                        artifact = receipt.get("artifact") or {}
                        if (range_mode != "native" and artifact.get("status") == "skipped" and
                                artifact.get("skipReason") == "subresource-range-flat"):
                            continue
                        if receipt.get("captureSessionId") != session:
                            raise ValueError("Channel preview receipt capture session changed")
                        group = next(group for group in plan if group["channel"] == channel)
                        known = copy.deepcopy(semantics)
                        if group.get("label") or group.get("description"):
                            prior = known.setdefault(channel, {})
                            if group.get("label"):
                                if parent.get("channelGroups") is not None or not prior.get("title"):
                                    prior["title"] = group["label"]
                            if group.get("description"):
                                for name in ("description", "channelMeaning"):
                                    if parent.get("channelGroups") is not None or not prior.get(name):
                                        prior[name] = group["description"]
                        result.append(_preview(receipt, identity, parent, known, channel))
                        receipts.append({"channel": channel, "rangeMode": range_mode, "receipt": receipt})
                    except Exception as error:
                        checkpoint()  # Cancellation/session drift is not a recoverable channel error.
                        failures.append({"channel": channel, "error": str(error)})
                    break
            checkpoint()
        except Exception as error:
            errors.append(error)
        finally:
            if selected:
                try:
                    bridge._set_frame_event(ctrl, snapshot["originalEvent"], True)
                except Exception as error:
                    errors.append(error)

    tagged = getattr(replay, "BlockInvokeTagged", None)
    if tagged is not None:
        tagged("ReplayLab.background.frame-flow-channels", collect)
    else:
        replay.BlockInvoke(collect)
    if errors:
        raise errors[-1]
    checkpoint()
    if not metadata["componentCount"]:
        raise RuntimeError("Channel preview replay request did not complete")
    for item in receipts:
        item["receipt"]["restoration"]["restored"] = True
    order = [group["channel"] for group in metadata["channelGroups"]]
    result.sort(key=lambda image: order.index(image["channel"]))
    return dict(metadata, images=result, receipts=receipts, errors=failures,
                complete=not failures and (requests is not None and len(metadata["channelGroups"]) <= 1 or
                         set(channels_from_images(result)) == set(group["channel"] for group in metadata["channelGroups"])))


def channels_from_images(images):
    return [str(image.get("channel", "")).lower() for image in images]


def capture_channels(bridge, parent, snapshot, semantics, cancelled):
    value = capture_channel_batch(bridge, parent, snapshot, semantics, cancelled)
    if not value["images"] and value["errors"]:
        raise RuntimeError("Channel preview replay request did not complete: " + str(value["errors"])[:1024])
    return value


class ChannelPreviewLoader(object):
    """Per-panel bounded LRU with worker-only PNG verification and owner cancel."""
    def __init__(self, executor, capture, current_session, maximum=16, current_mutation=None):
        self.executor, self.capture, self.current_session = executor, capture, current_session
        self.owner = "frame-flow-channels-" + uuid.uuid4().hex
        self.maximum = max(1, min(int(maximum), 64))
        self.cache = collections.OrderedDict()
        self.pending = {}
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.current_mutation = current_mutation

    def request(self, parent, snapshot, semantics, completion):
        if self.cancelled.is_set():
            return
        identity = request_identity(parent)
        parent_semantics = {name: parent.get(name) for name in
                            ("title", "description", "semantic", "channelMeaning", "evidence", "label", "channelGroups")}
        key = json.dumps([snapshot["session"], snapshot["flowGeneration"],
                          snapshot["foregroundGeneration"], snapshot.get("mutationEpoch"),
                          parent.get("sha256"), identity, parent_semantics, semantics], sort_keys=True)
        if key in self.pending:
            self.pending[key].append(completion)
            return
        if len(self.pending) >= 8:
            completion({"images": [], "error": "Channel preview request queue is full"})
            return
        self.pending[key] = [completion]

        def stale():
            return (self.cancelled.is_set() or self.current_session() != snapshot["session"] or
                    (self.current_mutation is not None and
                     self.current_mutation() != snapshot["mutationEpoch"]))

        def worker():
            if stale():
                raise RuntimeError("Channel preview request was superseded")
            with self.lock:
                cached = self.cache.pop(key, None)
            retry_snapshot = snapshot
            if cached and time.monotonic() - cached[0] < 30:
                try:
                    for image in cached[1]["images"]:
                        _verify_png(image)
                    with self.lock:
                        self.cache[key] = cached
                    if cached[1].get("complete"):
                        return copy.deepcopy(cached[1])
                    retry_snapshot = dict(snapshot, cachedChannelImages=copy.deepcopy(cached[1]["images"]))
                except (OSError, ValueError):
                    pass
            value = self.capture(parent, retry_snapshot, semantics, self.cancelled.is_set)
            if isinstance(value, list):
                value = {"images": value, "errors": [], "complete": True}
            if not stale():
                with self.lock:
                    # Preserve partial success, but retry its missing channels
                    # instead of treating the set as a complete cache hit.
                    if value.get("images") or value.get("complete"):
                        self.cache[key] = (time.monotonic(), copy.deepcopy(value))
                    while len(self.cache) > self.maximum:
                        self.cache.popitem(last=False)
            return value

        def completed(value, error):
            callbacks = self.pending.pop(key, [])
            if self.cancelled.is_set() or self.current_session() != snapshot["session"]:
                return
            if stale():
                value, error = {}, "Channel preview replay content changed"
            value = value or {}
            for callback in callbacks:
                callback({"images": copy.deepcopy(value.get("images", [])) if not error else [],
                          "error": str(error or ""), "complete": not error and value.get("complete", False),
                          "componentCount": value.get("componentCount", 0),
                          "channelGroups": copy.deepcopy(value.get("channelGroups")),
                          "errors": copy.deepcopy(value.get("errors", []))})

        try:
            self.executor.submit(self.owner, worker, completed)
        except Exception:
            self.pending.pop(key, None)
            raise

    def close(self):
        self.cancelled.set()
        self.executor.cancel_owner(self.owner)
        self.pending.clear()
        with self.lock:
            self.cache.clear()
