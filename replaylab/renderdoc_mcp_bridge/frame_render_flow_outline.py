"""Cheap immutable action-tree projection for the frame story board (Python 3.6).

No pipeline reads or SetFrameEvent calls: output bindings are recorded actions,
not a claim that any pixel was written. The caller owns capture/session guards.
"""
from __future__ import print_function


MAX_ACTIONS = 100000
PAGE_SIZE = 4000


def action_has_work(action, kind, masks):
    """Use decoded workload fields, never a formatted action name."""
    if (int(action.flags) & masks.get("indirect", 0x080000) and
            getattr(action, "indirectArgumentsResolved", None) is not True):
        # Native ActionDescription initializes counts to zero even when GPU
        # indirect arguments were not decoded. Those zeros do not prove that
        # this action did no work; agree with the detailed participation view.
        return None
    is_mesh = bool(int(action.flags) & masks.get("mesh", 0))
    if kind == "dispatch" or (kind == "draw" and is_mesh):
        dimensions = getattr(action, "dispatchDimension", None)
        if dimensions is not None and len(dimensions) == 3:
            return all(int(value) > 0 for value in dimensions)
    elif kind == "draw":
        count = getattr(action, "numIndices", None)
        # D3D11 Draw/DrawIndexed leave numInstances at its native default (0).
        # Only Instanced actions have an instance-count operand. Do not infer
        # this from a formatted/localized API name, or turn real zero-instance
        # instanced draws into work by clamping their count to one.
        if count is not None:
            if int(count) <= 0:
                return False
            if not int(action.flags) & masks.get("instanced", 0x020000):
                return True
            instances = getattr(action, "numInstances", None)
            if instances is not None:
                return int(instances) > 0
    elif kind in ("copy", "resolve", "clear", "present"):
        return True
    return None


def collect_actions(roots, name_of, resource_id, masks, checkpoint=lambda: None,
                    maximum=MAX_ACTIONS, indirect_workload=None):
    rows = []
    total = 0
    stack = [(iter(roots), 0)]
    while stack:
        actions, parent = stack[-1]
        try:
            action = next(actions)
        except StopIteration:
            stack.pop()
            continue
        if total % 256 == 0:
            checkpoint()
        event_id = int(action.eventId)
        flags = int(action.flags)
        children = getattr(action, "children", None) or []
        if len(rows) < maximum:
            kind = "other"
            # Multi-action containers are structural, not a second GPU draw.
            if children or flags & masks.get("marker", 0):
                kind = "marker"
            else:
                for candidate in ("draw", "dispatch", "copy", "resolve", "clear", "present"):
                    if flags & masks.get(candidate, 0):
                        kind = candidate
                        break
            decoded = (indirect_workload(action) if indirect_workload is not None and
                       flags & masks.get("indirect", 0x080000) else None)
            has_work = action_has_work(action, kind, masks)
            if decoded and decoded.get("status") == "resolved":
                has_work = decoded.get("nonZeroWorkload")
            rows.append({
                "eventId": event_id, "parentEventId": parent,
                "executionIndex": total, "name": str(name_of(action))[:512],
                "flags": flags, "actionKind": kind,
                "hasWork": has_work,
                "isAction": kind not in ("marker", "other"),
                "outputs": [resource_id(value) for value in
                            (getattr(action, "outputs", None) or [])
                            if resource_id(value)] if kind in ("draw", "clear", "resolve", "present") else [],
                "depthOutput": resource_id(getattr(action, "depthOut", None)) if kind in ("draw", "clear") else 0,
                "copySource": resource_id(getattr(action, "copySource", None)),
                "copyDestination": resource_id(getattr(action, "copyDestination", None)),
            })
            for operand in ("copySource", "copyDestination"):
                subresource = getattr(action, operand + "Subresource", None)
                if rows[-1][operand] and subresource is not None:
                    rows[-1][operand + "Subresource"] = {
                        field: int(getattr(subresource, field)) for field in ("mip", "slice", "sample")}
            if decoded:
                rows[-1]["indirectWorkload"] = decoded
        total += 1
        if children:
            stack.append((iter(children), event_id))
    return rows, total


def outline_page(snapshot, offset=0, limit=PAGE_SIZE, cache_hit=False):
    if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or
            isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000):
        raise ValueError("Frame flow offset/limit must be non-negative integers; limit is 1..5000")
    actions = snapshot["actions"]
    result = {key: value for key, value in snapshot.items() if key not in ("actions", "summary")}
    stop = min(len(actions), offset + limit)
    result.update(actions=actions[offset:stop], offset=offset,
                  nextOffset=stop, hasMore=stop < len(actions))
    result["summary"] = dict(snapshot["summary"], cacheHit=bool(cache_hit))
    return result
