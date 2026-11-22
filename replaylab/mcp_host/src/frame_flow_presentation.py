"""Bounded, data-only presentation graphs and semantic channel grouping."""
from __future__ import annotations

from typing import Any, Mapping


def _text(value: Any, limit: int, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or any(
            ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError("Process graph text must be bounded plain text")
    value = value.strip()
    if required and not value:
        raise ValueError("Process graph labels and IDs cannot be empty")
    return value


def channel_groups(value: Any, component_count: int = 0) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 4:
        raise ValueError("channelGroups must contain at most four meaningful groups")
    result, used = [], set()
    for row in value:
        if not isinstance(row, Mapping) or set(row) - {"channel", "label", "description"}:
            raise ValueError("Channel groups accept channel, label and description")
        channel = row.get("channel")
        if channel not in ("rgb", "r", "g", "b", "a"):
            raise ValueError("Channel group must be rgb, r, g, b or a")
        components = set(channel)
        if used & components:
            raise ValueError("Channel groups cannot overlap")
        if component_count and not components <= set("rgba"[:component_count]):
            raise ValueError("Channel group references a component absent from the texture")
        used.update(components)
        result.append({"channel": channel, "label": _text(row.get("label", ""), 160),
                       "description": _text(row.get("description", ""), 1200)})
    return result


def process_graph(value: Any, images: list[dict], comparisons: list[dict],
                  animations: list[dict], members: set[int], *,
                  allowed_events: set[int] | None = None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"nodes", "edges"}:
        raise ValueError("processGraph requires nodes and edges")
    nodes, edges = value["nodes"], value["edges"]
    if not isinstance(nodes, list) or not 2 <= len(nodes) <= 16:
        raise ValueError("Process graph requires 2 to 16 nodes")
    if not isinstance(edges, list) or not 1 <= len(edges) <= 32:
        raise ValueError("Process graph requires 1 to 32 edges")
    media = {"imageIndex": images, "comparisonIndex": comparisons, "animationIndex": animations}
    allowed_events = members if allowed_events is None else members | allowed_events
    clean_nodes, ids = [], set()
    for node in nodes:
        if not isinstance(node, Mapping) or set(node) - ({"id", "label", "description", "eventId"} | set(media)):
            raise ValueError("Unsupported process graph node fields")
        identity = _text(node.get("id"), 80, True)
        if identity in ids or any(not (char.isascii() and (char.isalnum() or char in "_.-")) for char in identity):
            raise ValueError("Process graph node IDs must be unique plain identifiers")
        ids.add(identity)
        clean = {"id": identity, "label": _text(node.get("label"), 160, True),
                 "description": _text(node.get("description", ""), 600)}
        references = set(node) & set(media)
        if len(references) > 1:
            raise ValueError("A process node accepts at most one media reference")
        for key in references:
            index = node[key]
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(media[key]):
                raise ValueError("Process graph references missing media")
            clean[key] = index
        if "eventId" in node:
            event = node["eventId"]
            if isinstance(event, bool) or not isinstance(event, int) or event not in allowed_events:
                raise ValueError("Process graph event must be a stage member or an evidenced related event")
            if "imageIndex" in node and images[node["imageIndex"]].get("eventId") != event:
                raise ValueError("Process graph event does not match its image")
            clean["eventId"] = event
        clean_nodes.append(clean)
    adjacency = {identity: [] for identity in ids}
    indegree = {identity: 0 for identity in ids}
    clean_edges, seen, connected = [], set(), set()
    for edge in edges:
        if not isinstance(edge, Mapping) or set(edge) - {"from", "to", "label"}:
            raise ValueError("Process graph edges accept from, to and label")
        source, target = edge.get("from"), edge.get("to")
        if not isinstance(source, str) or not isinstance(target, str) or source not in ids or target not in ids or source == target:
            raise ValueError("Process graph edge must connect two existing distinct nodes")
        if (source, target) in seen:
            raise ValueError("Duplicate process graph edge")
        seen.add((source, target))
        connected.update((source, target))
        adjacency[source].append(target)
        indegree[target] += 1
        clean_edges.append({"from": source, "to": target, "label": _text(edge.get("label", ""), 160)})
    if connected != ids:
        raise ValueError("Process graph contains disconnected nodes")
    pending = [identity for identity in ids if not indegree[identity]]
    visited = 0
    while pending:
        source = pending.pop()
        visited += 1
        for target in adjacency[source]:
            indegree[target] -= 1
            if not indegree[target]:
                pending.append(target)
    if visited != len(ids):
        raise ValueError("Process graph must be acyclic; unfold repeated work into explicit steps")
    return {"nodes": clean_nodes, "edges": clean_edges}


def _presentation_graph(stage: Mapping[str, Any]) -> dict | None:
    """Use the same graph, including legacy arrows, for checks and presentation."""
    graph = stage.get("processGraph")
    if not graph and len(stage.get("processSteps", [])) > 1:
        steps = stage["processSteps"]
        graph = {"nodes": [{"id": "step-%d" % (index + 1), "label": step.get("label", ""),
                            **{key: step[key] for key in ("imageIndex", "comparisonIndex", "animationIndex") if key in step}}
                           for index, step in enumerate(steps)],
                 "edges": [{"from": "step-%d" % (index + 1), "to": "step-%d" % (index + 2)}
                           for index in range(len(steps) - 1)]}
    return graph


def _process_graph_review(stage: Mapping[str, Any], actions: Mapping[int, Mapping[str, Any]],
                          observations: Mapping[str, Any]) -> dict[str, Any]:
    """Check declared resource paths without equating bindings with causation.

    The graph remains editable. Temporal contradictions and parallel MRT outputs
    are distinct from missing evidence. A supported path needs both the Agent's
    recorded calculation/provenance and the corresponding observed bindings.
    """
    graph = _presentation_graph(stage)
    review = {"gaps": [], "verifiedTransferCount": 0, "repeatedSnapshotPathCount": 0,
              "unverifiedTransferPathCount": 0, "unverifiedSubresourcePathCount": 0,
              "verifiedMediaTransferCount": 0, "hasMedia": False, "hasGraph": bool(graph)}
    if not graph:
        return review
    review["hasMedia"] = any(any(key in node for key in ("imageIndex", "comparisonIndex", "animationIndex"))
                             for node in graph["nodes"])
    gaps, seen_gaps = review["gaps"], set()

    def gap(code, source, target, message, next_action):
        identity = (code, source, target)
        if identity in seen_gaps:
            return
        seen_gaps.add(identity)
        gaps.append({"code": code, "stageId": stage["id"],
                     "message": "Connection %s -> %s: %s" % (source, target, message),
                     "nextAction": next_action})

    def observation(event):
        row = observations.get(str(event), {})
        return row if isinstance(row, Mapping) else {}

    def resource(value):
        if isinstance(value, bool):
            return None
        try:
            number = int(str(value).removeprefix("ResourceId::"))
            return number if number > 0 else None
        except (TypeError, ValueError):
            return None

    def effective(event):
        action = actions.get(event)
        if not action or action.get("isAction") is False or action.get("hasWork") is False:
            return False
        if action.get("hasWork") is None and action.get("flags", 0) & 0x080000:
            obs = observation(event)
            decoded = obs.get("indirectWorkload", (obs.get("details") or {}).get("indirectWorkload", {}))
            return (decoded.get("status") == "resolved" and decoded.get("eventId") == event and
                    decoded.get("source") in {"native-structured-indirect-arguments", "event-bound-indirect-arguments"} and
                    decoded.get("nonZeroWorkload") is True)
        return True

    def entries(event, output=False):
        obs = observation(event)
        keys = ("expectedOutputs",) if output else ("textureInputs", "inputs")
        result = [item for key in keys for item in (obs.get(key) or []) if isinstance(item, Mapping)]
        if not output:
            result += [item for item in (obs.get("expectedOutputs") or [])
                       if isinstance(item, Mapping) and item.get("kind") == "uav"]
        return result

    def bound(event, rid, output=False):
        if rid is None or not effective(event):
            return False
        obs = observation(event)
        if obs.get("bindingErrors") or obs.get("bindingsComplete") is False:
            return False
        if any(resource(item.get("resourceId")) == rid for item in entries(event, output)):
            return True
        action = actions[event]
        if action.get("flags", 0) & (0x0400 | 0x0800 | 0x0100):
            return resource(action.get("copyDestination" if output else "copySource")) == rid
        return False

    images = stage.get("images", [])
    algorithm = stage.get("algorithm") or {}
    inputs, integration = algorithm.get("inputs", []), algorithm.get("integration", [])
    steps = {row["eventId"]: row for row in algorithm.get("steps", [])}

    def input_view(image):
        parent = image.get("parentImageIndex")
        if image.get("group") == "channels" and type(parent) is int and 0 <= parent < len(images):
            image = images[parent]
        event, rid = image.get("eventId"), resource(image.get("resourceId"))
        kind = image.get("attachmentKind") or (image.get("displayMapping") or {}).get("attachmentKind")
        if kind == "srv":
            return True
        if image.get("group") != "inputs":
            return False
        # An input card may be an earlier producer's RTV, or a consumer's SRV
        # snapshot. Preserve that distinction instead of treating every input
        # gallery card as an output made at the displayed event.
        return (bound(event, rid) or any(row.get("eventId") == event and row.get("resourceId") == rid
                                       for row in inputs) or not bound(event, rid, True))

    def image_anchor(image):
        event = image.get("eventId")
        if event not in actions:
            return None
        return {"event": event, "rid": resource(image.get("resourceId")),
                "role": "input" if input_view(image) else "output", "image": image}

    nodes, anchors = {node["id"]: node for node in graph["nodes"]}, {}
    for identity, node in nodes.items():
        if "imageIndex" in node:
            values = [image_anchor(images[node["imageIndex"]])]
        else:
            indices = []
            if "comparisonIndex" in node:
                comparison = stage["comparisons"][node["comparisonIndex"]]
                indices = [comparison["leftImageIndex"], comparison["rightImageIndex"]]
            elif "animationIndex" in node:
                indices = stage["animations"][node["animationIndex"]]["imageIndices"]
            values = [image_anchor(images[index]) for index in indices]
            if not values and node.get("eventId") in actions:
                values = [{"event": node["eventId"], "rid": None, "role": "operation", "image": None}]
        anchors[identity] = [value for value in values if value is not None]

    # A resource version includes its event and whether the snapshot is an
    # input or output. Reusing one RID does not collapse later image versions.
    key = lambda event, rid, role: (event, rid, role)
    links, view_links, view_cache = {}, {}, {}

    def nonnegative(value):
        return value if type(value) is int and value >= 0 else None

    def view(item):
        mip = nonnegative(item.get("mip", item.get("firstMip")))
        layer = nonnegative(item.get("slice", item.get("firstSlice")))
        # Older inventories recorded the first subresource, but not the size
        # of its bound view. The first mip/layer is still known to be covered.
        # Keep that proven subset instead of discarding the entire binding;
        # this does not infer coverage of any mip/layer beyond the base point.
        # An explicit empty/invalid range is not a legacy missing range.
        mip_count = 1 if item.get("numMips") is None else nonnegative(item["numMips"])
        layer_count = 1 if item.get("numSlices") is None else nonnegative(item["numSlices"])
        return (mip, mip_count,
                layer, layer_count,
                nonnegative(item.get("sample")))

    def complete_view(value):
        return all(item is not None for item in value) and value[1] > 0 and value[3] > 0

    def resource_views(endpoint):
        if endpoint in view_cache:
            return view_cache[endpoint]
        event, rid, role = endpoint
        output = role == "output"
        rows = [item for item in entries(event, output) if resource(item.get("resourceId")) == rid]
        if not output and (event, rid) in inplace:
            rows += [item for item in entries(event, True) if resource(item.get("resourceId")) == rid]
        action = actions.get(event, {})
        operand = "copyDestination" if output else "copySource"
        if not rows and resource(action.get(operand)) == rid:
            subresource = action.get(operand + "Subresource")
            if isinstance(subresource, Mapping):
                rows.append(dict(subresource, numMips=1, numSlices=1))
        # Unknown base coordinates/sample are not exact subresource evidence.
        # Missing extents retain only their known base point (see view above).
        view_cache[endpoint] = {value for item in rows if complete_view(value := view(item))}
        return view_cache[endpoint]

    def overlaps(a, b):
        return (max(a[0], b[0]) < min(a[0] + a[1], b[0] + b[1]) and
                max(a[2], b[2]) < min(a[2] + a[3], b[2] + b[3]) and a[4] == b[4])

    def intersection(a, b):
        if not overlaps(a, b):
            return None
        mip, layer = max(a[0], b[0]), max(a[2], b[2])
        return (mip, min(a[0] + a[1], b[0] + b[1]) - mip,
                layer, min(a[2] + a[3], b[2] + b[3]) - layer, a[4])

    def connect(source, target):
        links.setdefault(source, set()).add(target)
        if source[0] != target[0] and actions[source[0]]["executionIndex"] >= actions[target[0]]["executionIndex"]:
            return
        for a in resource_views(source):
            for b in resource_views(target):
                # Between events the same captured subresource must transfer.
                # Within an evidenced Shader/copy operation, its input may be
                # remapped into a different output mip, slice or target.
                remap = source[0] == target[0] and source[2] == "input" and target[2] == "output"
                if remap or overlaps(a, b):
                    view_links.setdefault(source + (a,), set()).add(target + (b,))

    output_keys = {(row.get("eventId"), resource(row.get("resourceId"))) for row in algorithm.get("outputs", [])}
    output_keys.update((anchor["event"], anchor["rid"]) for values in anchors.values() for anchor in values
                       if anchor["role"] == "output" and anchor["rid"] is not None)
    output_keys.update((row.get("eventId"), resource(row.get("targetResourceId"))) for row in integration
                       if row.get("targetResourceId") is not None)
    inplace = {(row.get("eventId"), row.get("resourceId")) for row in integration
               if row.get("mode") == "in-place" and row.get("evidence") and
               row.get("sourceEventId") == row.get("eventId")}
    for row in inputs:
        event, rid, producer = row.get("eventId"), resource(row.get("resourceId")), row.get("producerEventId")
        evidence = row.get("evidence") or steps.get(event, {}).get("evidence")
        read = bound(event, rid) or ((event, rid) in inplace and bound(event, rid, True))
        if not evidence or not read:
            continue
        if producer in actions and bound(producer, rid, True):
            connect(key(producer, rid, "output"), key(event, rid, "input"))
        if steps.get(event, {}).get("evidence"):
            for output_event, output_rid in output_keys:
                if output_event == event and bound(event, output_rid, True):
                    connect(key(event, rid, "input"), key(event, output_rid, "output"))
    for row in integration:
        producer, event = row.get("sourceEventId"), row.get("eventId")
        rid, target_rid = resource(row.get("resourceId")), resource(row.get("targetResourceId"))
        if not row.get("evidence") or not bound(producer, rid, True):
            continue
        if row.get("mode") == "present" and actions.get(event, {}).get("flags", 0) & 0x0100 and bound(event, rid, True):
            connect(key(producer, rid, "output"), key(event, rid, "output"))
        elif row.get("mode") in ("downstream", "non-image") and bound(event, rid):
            connect(key(producer, rid, "output"), key(event, rid, "input"))
            if target_rid is not None and bound(event, target_rid, True):
                connect(key(event, rid, "input"), key(event, target_rid, "output"))

    reach_cache, precise_successors = {}, {}
    for source, targets in view_links.items():
        for target in targets:
            precise_successors.setdefault(source[:3], set()).add((source[3], target))

    def reaches(source, target, precise=False):
        pair = (source, target, precise)
        if pair in reach_cache:
            return reach_cache[pair]
        pending, visited = [source], set()
        while pending:
            current = pending.pop()
            if (current == target if not precise else
                    current[:3] == target[:3] and overlaps(current[3], target[3])):
                reach_cache[pair] = True
                return True
            if current not in visited:
                visited.add(current)
                if not precise:
                    pending.extend(links.get(current, ()))
                    continue
                for source_view, successor in precise_successors.get(current[:3], ()):
                    carried = intersection(current[3], source_view)
                    if carried is None:
                        continue
                    if current[0] == successor[0] and current[2] == "input" and successor[2] == "output":
                        # Only this evidenced calculation/copy edge can change
                        # subresources. A broad SRV must not teleport an image
                        # from mip 1 to mip 2 on a cross-event transfer.
                        carried = successor[3]
                    else:
                        carried = intersection(carried, successor[3])
                    if carried is not None:
                        pending.append(successor[:3] + (carried,))
        reach_cache[pair] = False
        return False

    def snapshot(anchor):
        """Display channels/exposure cannot turn one resource into new work."""
        if anchor["rid"] is None or anchor["image"] is None:
            return None
        image = anchor["image"]
        mapping = image.get("displayMapping") or {}
        return (anchor["event"], anchor["rid"], anchor["role"],
                *(mapping.get(field, image.get(field, 0)) for field in ("mip", "slice", "sample")))

    operation_endpoints = {}
    for precise, graph_links in ((False, links), (True, view_links)):
        endpoints = set(graph_links)
        endpoints.update(value for values in graph_links.values() for value in values)
        for value in endpoints:
            operation_endpoints.setdefault((value[0], False, precise), set()).add(value)
            if value[2] == "output":
                operation_endpoints.setdefault((value[0], True, precise), set()).add(value)

    def endpoint_keys(anchor, source, precise):
        if anchor["role"] == "operation":
            # An operation may remain a text connector; its endpoint still
            # needs an actual, evidenced resource path, not just a later EID.
            return operation_endpoints.get((anchor["event"], source, precise), set())
        if anchor["rid"] is None:
            return set()
        endpoint = key(anchor["event"], anchor["rid"], anchor["role"])
        if not precise:
            return {endpoint}
        point = snapshot(anchor)[3:]
        if any(type(value) is not int or value < 0 for value in point):
            return set()
        selected = (point[0], 1, point[1], 1, point[2])
        return {endpoint + (selected,)} if any(overlaps(value, selected)
                                               for value in resource_views(endpoint)) else set()

    def crosses_events(left, right, precise=True, require_media=False):
        return any(a["event"] != b["event"] and
                   actions[a["event"]]["executionIndex"] < actions[b["event"]]["executionIndex"] and
                   (not require_media or a["image"] is not None or b["image"] is not None) and
                   any(reaches(source, target, precise) for source in endpoint_keys(a, True, precise)
                       for target in endpoint_keys(b, False, precise))
                   for a in left for b in right)

    adjacency = {identity: [] for identity in nodes}
    for edge in graph["edges"]:
        adjacency[edge["from"]].append(edge["to"])
    for source in nodes:
        pending, visited = list(adjacency[source]), set()
        while pending:
            target = pending.pop()
            if target in visited:
                continue
            visited.add(target)
            pending.extend(adjacency[target])
            left, right = anchors[source], anchors[target]
            if not left or not right:
                continue
            left_snapshots, right_snapshots = {snapshot(a) for a in left}, {snapshot(a) for a in right}
            if None not in left_snapshots | right_snapshots and left_snapshots == right_snapshots:
                # Identical snapshots connected through differently named
                # arithmetic nodes are still one result, not new render work.
                # Keep the authored data for editing but omit this mini graph.
                review["repeatedSnapshotPathCount"] += 1
            elif crosses_events(left, right):
                review["verifiedTransferCount"] += 1
                if crosses_events(left, right, require_media=True):
                    review["verifiedMediaTransferCount"] += 1
            elif crosses_events(left, right, precise=False):
                review["unverifiedSubresourcePathCount"] += 1
            elif any(a["event"] != b["event"] for a in left for b in right):
                # Text operation anchors do not undergo image-to-image gap
                # checks below. They nevertheless cannot make an unsupported
                # cross-draw arrow visible beside a different supported path.
                review["unverifiedTransferPathCount"] += 1
            if min(actions[a["event"]]["executionIndex"] for a in left) > max(
                    actions[a["event"]]["executionIndex"] for a in right):
                gap("invalid-process-data-flow", source, target, "The path runs from a later event to an earlier event.",
                    "Use the actual producer-to-consumer order. A text-only transformation cannot reverse captured execution.")
                continue
            # A slider or animation represents several snapshots, not one
            # fabricated resource endpoint. Only judge exact endpoints below.
            if len(left) != 1 or len(right) != 1:
                continue
            a, b = left[0], right[0]
            ae, be, ar, br = a["event"], b["event"], a["rid"], b["rid"]
            if a["role"] == b["role"] == "output" and ae == be and ar != br:
                rtvs = {resource(item.get("resourceId")) for item in entries(ae, True) if item.get("kind") == "rtv"}
                if ar in rtvs and br in rtvs and bound(ae, ar, True) and bound(be, br, True):
                    gap("invalid-process-data-flow", source, target, "These textures are parallel MRT outputs of the same draw.",
                        "Show the calculation branching to both outputs, or show the outputs side by side without a dependency arrow.")
                    continue
            if a["role"] == "operation" or b["role"] == "operation":
                continue
            if ar is None or br is None:
                # Legacy/partial images without an exact RID only support the
                # ordering check above; do not invent a resource relationship.
                continue
            if not reaches(key(ae, ar, a["role"]), key(be, br, b["role"])):
                gap("unverified-process-data-flow", source, target, "The resource transfer shown by this path is not supported by the recorded calculation and bindings.",
                    "Record the exact producer/input or composition relation and inspect its resource bindings. Explain the Shader calculation; bindings alone do not prove mathematical dependence.")
    return review


def process_graph_gaps(stage: Mapping[str, Any], actions: Mapping[int, Mapping[str, Any]],
                       observations: Mapping[str, Any]) -> list[dict[str, str]]:
    return _process_graph_review(stage, actions, observations)["gaps"]


def process_presentation_review(stage: Mapping[str, Any], actions: Mapping[int, Mapping[str, Any]],
                                observations: Mapping[str, Any]) -> dict[str, Any]:
    """Only display subgraphs of evidenced, distinct render-resource versions.

    Single-draw arithmetic, parallel MRTs and repeated views remain available
    as standalone result images. This display choice never weakens validation
    of an authored data-flow claim or edits the original graph/legacy steps.
    """
    review = _process_graph_review(stage, actions, observations)
    if not review["hasGraph"]:
        reason = "no-subgraph"
    elif review["repeatedSnapshotPathCount"]:
        reason = "repeated-resource-snapshot"
    elif review["gaps"]:
        reason = "unsupported-resource-path"
    elif not review["hasMedia"]:
        reason = "no-representative-media"
    elif review["unverifiedSubresourcePathCount"]:
        reason = "unverified-subresource-mapping"
    elif review["unverifiedTransferPathCount"]:
        reason = "unverified-resource-transfer"
    elif not review["verifiedTransferCount"]:
        reason = "no-cross-event-resource-transfer"
    elif not review["verifiedMediaTransferCount"]:
        reason = "no-representative-media"
    else:
        reason = "verified-resource-flow"
    review["presentation"] = "show" if reason == "verified-resource-flow" else "omit"
    review["reason"] = reason
    return review
