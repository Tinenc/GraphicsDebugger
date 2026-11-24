"""Bounded capture/flow-scoped resource usage candidates, never causal edges."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import OrderedDict
from copy import deepcopy
import threading
from typing import Any, Mapping


USAGE_SCHEMA = "bread-frame-resource-usage/v1"
MAX_USAGE_ROWS = 100_000
MAX_PAGE_SIZE = 200


def _uint(value: Any, name: str, minimum: int = 0, maximum: int = 0xFFFFFFFF) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("%s must be an integer between %d and %d" % (name, minimum, maximum))
    return value


def usage_query(record: Mapping[str, Any], resource_id: int, anchor_event_id: int,
                direction: str, offset: int, limit: int, order: str = "execution") -> dict[str, Any]:
    resource_id = _uint(resource_id, "resource_id", 1, 0xFFFFFFFFFFFFFFFF)
    anchor_event_id = _uint(anchor_event_id, "anchor_event_id")
    offset = _uint(offset, "offset", maximum=MAX_USAGE_ROWS)
    limit = _uint(limit, "limit", 1, MAX_PAGE_SIZE)
    if direction not in ("all", "before", "after"):
        raise ValueError("direction must be all, before or after")
    if direction != "all" and not anchor_event_id:
        raise ValueError("before/after requires a positive anchor_event_id")
    if order not in ("execution", "nearest"):
        raise ValueError("order must be execution or nearest")
    if order == "nearest" and direction == "all":
        raise ValueError("nearest order requires before/after and an anchor_event_id")
    anchor_index = None
    if anchor_event_id:
        anchor = next((row for row in record["outline"]["actions"]
                       if row["eventId"] == anchor_event_id), None)
        if anchor is None:
            raise ValueError("anchor_event_id is not part of this frame outline")
        anchor_index = anchor["executionIndex"]
    return {"resourceId": resource_id, "anchorEventId": anchor_event_id,
            "anchorExecutionIndex": anchor_index, "direction": direction,
            "offset": offset, "limit": limit, "requestedOrder": order}


def usage_cache_key(record: Mapping[str, Any], resource_id: int) -> tuple[str, str, str, int]:
    return (record["outline"]["captureSessionId"], record["flowId"], record["snapshotId"], resource_id)


def normalize_usage(raw: Any, record: Mapping[str, Any], resource_id: int) -> dict[str, Any]:
    """Retain GetUsage types as observations; no inferred read/write/algorithm labels."""
    outline = record["outline"]
    if (not isinstance(raw, Mapping) or raw.get("schema") != "renderdoc-resource-usage/v1"
            or _uint(raw.get("resourceId"), "usage resourceId", 1, 0xFFFFFFFFFFFFFFFF) != resource_id):
        raise ValueError("Resource usage returned mismatched resource evidence")
    # Older Find receipts have no capture identity. Their admission relies on
    # the session-pinned IPC request plus the Host's post-call capture check.
    # If a newer receipt supplies identity, it must agree as well.
    for source in (raw, raw.get("capture", {})):
        if not isinstance(source, Mapping):
            raise ValueError("Resource usage capture identity is invalid")
        for key in ("captureSessionId", "captureId"):
            if key in source and source[key] != outline[key]:
                raise ValueError("Resource usage returned mismatched capture evidence")
    candidates = raw.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("Resource usage candidates must be a list")
    declared_count = _uint(raw.get("count"), "usage count")
    flags = {}
    for key in ("complete", "truncated", "hasMore"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ValueError("Resource usage %s must be boolean" % key)
            flags[key] = raw[key]
    by_eid = {row["eventId"]: row for row in outline["actions"]}
    items, unmapped = [], 0
    for row in candidates[:MAX_USAGE_ROWS]:
        if not isinstance(row, Mapping):
            raise ValueError("Resource usage candidate must be an object")
        event = _uint(row.get("eventId"), "usage eventId")
        usage = row.get("usage")
        if not isinstance(usage, str) or len(usage) > 256 or any(ord(c) < 32 for c in usage):
            raise ValueError("Resource usage type must be bounded plain text")
        numeric = _uint(row.get("usageValue"), "usageValue")
        action = by_eid.get(event)
        if action is None:
            # GetUsage can include initial-state events not present in the
            # outline. Do not turn them into invented timeline positions.
            unmapped += 1
            continue
        items.append({"eventId": event, "executionIndex": action["executionIndex"],
                      "usage": usage, "usageValue": numeric,
                      "actionName": action["name"], "actionFlags": action["flags"]})
    items.sort(key=lambda row: row["executionIndex"])
    count_mismatch = declared_count != len(candidates)
    source_complete = (not count_mismatch and flags.get("complete", True)
                       and not flags.get("truncated", False) and not flags.get("hasMore", False)
                       and raw.get("nextOffset") is None)
    host_omitted = max(0, len(candidates) - MAX_USAGE_ROWS)
    source = {"tool": "find_draws_by_resource", "schema": raw["schema"],
              "reportedCount": declared_count, "receivedCount": len(candidates),
              "retainedCount": len(items), "unmappedEventCount": unmapped,
              "hostOmittedCount": host_omitted, "countMismatch": count_mismatch,
              "complete": bool(source_complete),
              "outlineComplete": bool(outline["summary"]["complete"])}
    for key in ("truncated", "hasMore", "nextOffset"):
        if key in raw:
            source[key] = raw[key] if key != "nextOffset" else (
                None if raw[key] is None else _uint(raw[key], "source nextOffset"))
    return {"items": items, "executionIndices": [row["executionIndex"] for row in items],
            "source": source,
            "complete": bool(source_complete and outline["summary"]["complete"]
                             and not unmapped and not host_omitted)}


def usage_page(entry: Mapping[str, Any], record: Mapping[str, Any], query: Mapping[str, Any],
               *, cache_hit: bool) -> dict[str, Any]:
    order = entry["executionIndices"]
    first, last = 0, len(order)
    if query["direction"] == "before":
        last = bisect_left(order, query["anchorExecutionIndex"])
    elif query["direction"] == "after":
        first = bisect_right(order, query["anchorExecutionIndex"])
    total = last - first
    start = min(total, query["offset"])
    end = min(total, start + query["limit"])
    reverse = query.get("requestedOrder") == "nearest" and query["direction"] == "before"
    # Slice only this page from the end. A backward trace can start beside its
    # consumer without repeatedly transferring thousands of old uses or copying
    # the full usage history. These are still candidates, not causal edges.
    rows = (entry["items"][last - end:last - start][::-1] if reverse
            else entry["items"][first + start:first + end])
    return {"schema": USAGE_SCHEMA, "flowId": record["flowId"], "snapshotId": record["snapshotId"],
            "captureSessionId": record["outline"]["captureSessionId"],
            "captureId": record["outline"]["captureId"], **query,
            "order": "execution-descending" if reverse else "execution-ascending",
            "anchorExcluded": query["direction"] != "all",
            "candidateOnly": True, "cacheHit": cache_hit,
            "complete": entry["complete"], "source": deepcopy(entry["source"]),
            "total": total, "hasMore": end < total, "nextOffset": end if end < total else None,
            "items": deepcopy(rows),
            "limitations": ["Usage types identify resource-level candidates, not channel reads or causal dependencies.",
                            "Repeated writes may retain earlier contributions; a nearby writer is not necessarily the complete producer.",
                            "Inspect exact bindings, subresources and Shader calculations before assigning a role or composition path."]}


class FrameResourceUsageCache:
    """LRU cache bounded by entries and rows; stale concurrent refreshes cannot win."""
    def __init__(self, capacity: int = 32, row_budget: int = 200_000):
        self.capacity = _uint(capacity, "cache capacity", 1, 1024)
        self.row_budget = _uint(row_budget, "cache row budget", 1)
        self._entries: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
        self._pending: OrderedDict[tuple, int] = OrderedDict()
        self._serial = 0
        self._rows = 0
        self._lock = threading.RLock()

    def acquire(self, key: tuple, *, refresh: bool = False) -> tuple[dict[str, Any] | None, int | None]:
        with self._lock:
            if not refresh and key in self._entries:
                self._entries.move_to_end(key)
                return self._entries[key], None
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._rows -= len(previous["items"])
            self._serial += 1
            self._pending[key] = self._serial
            self._pending.move_to_end(key)
            while len(self._pending) > self.capacity:
                self._pending.popitem(last=False)
            return None, self._serial

    def store(self, key: tuple, token: int, entry: dict[str, Any]) -> bool:
        with self._lock:
            if self._pending.get(key) != token:
                return False
            self._pending.pop(key)
            if len(entry["items"]) > self.row_budget:
                return False
            self._entries[key] = entry
            self._rows += len(entry["items"])
            while len(self._entries) > self.capacity or self._rows > self.row_budget:
                _, evicted = self._entries.popitem(last=False)
                self._rows -= len(evicted["items"])
            return True
