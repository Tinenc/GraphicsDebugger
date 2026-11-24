"""Publish-time channel capture orchestration; no UI or free-form filesystem paths."""
from __future__ import annotations

from copy import deepcopy


def precache(document, allocate, capture, accept, volume_capture=None):
    """Materialize physical channel groups, preserving valid results on partial failure.

    Capture receives at most eight parents and Host-allocated destinations. accept
    owns the exact receipt/hash/session checks and managed manifest publication.
    """
    pending, result = [], {"attempted": 0, "prepared": 0, "failed": 0, "errors": []}
    total = sum(len(stage["images"]) for stage in document["stages"])
    for stage in document["stages"]:
        for index, preview in enumerate(list(stage["images"])):
            if preview.get("group") in ("channels", "animation-frames"):
                continue
            if preview.get("channelGroups") == [] or preview.get("componentCount") == 1 or preview.get("attachmentKind") == "dsv":
                preview["channelGroups"] = preview.get("channelGroups", [])
                preview["channelCacheComplete"] = True
                continue
            if preview.get("attachmentKind") not in ("rtv", "uav", "srv", "present"):
                continue
            pending.append((stage, index, preview))

    replay_failure = ""
    for offset in range(0, len(pending), 8):
        if replay_failure:
            result["deferred"] = len(pending) - offset
            result["errors"].append({"error": replay_failure, "savedEvidenceRetained": True})
            break
        batch, allocations = [], {}
        entries = pending[offset:offset + 8]
        for stage, index, preview in entries:
            key = "%s:%d" % (stage["id"], index)
            requests = []
            for channel in ("rgb", "r", "g", "b", "a"):
                request = allocate(preview, channel)
                allocations[(key, channel)] = request
                requests.append(request)
            batch.append({"key": key, "preview": preview, "requests": requests})
            result["attempted"] += 1
            preview["channelCacheComplete"] = False
        try:
            reply = capture(batch)
            if reply.get("captureSessionId") != document["captureSessionId"]:
                raise ValueError("Channel precache returned a different capture session")
            rows = reply.get("items", [])
            if not isinstance(rows, list) or len(rows) != len(batch) or len({row.get("key") for row in rows}) != len(rows):
                raise ValueError("Channel precache returned an incomplete or duplicated parent list")
            by_key = {row["key"]: row for row in rows}
            if set(by_key) != {row["key"] for row in batch}:
                raise ValueError("Channel precache returned unrelated parents")
        except Exception as error:
            result["failed"] += len(entries)
            result["errors"].append({"parents": [row["key"] for row in batch], "error": str(error)[:1200]})
            if "bread-replay-unavailable" in str(error):
                replay_failure = str(error)[:1200]
            continue
        for stage, index, preview in entries:
            key = "%s:%d" % (stage["id"], index)
            row = by_key[key]
            try:
                from .frame_flow_presentation import channel_groups
                count = row.get("componentCount")
                if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 4:
                    raise ValueError("Channel precache omitted the actual texture format component count")
                plan = channel_groups(row.get("channelGroups"), count)
                preview.update(componentCount=count, channelGroups=plan)
                expected = {item["channel"] for item in plan} if len(plan) > 1 else set()
                # Repair a transient per-channel read once, keeping the successful
                # captures. Never retry a malformed batch or mix replay revisions.
                available = {item.get("channel") for item in row.get("receipts", [])}
                missing = expected - available
                fatal = next((str(error.get("error", "")) for error in row.get("errors", [])
                              if "bread-replay-unavailable" in str(error.get("error", ""))), "")
                if fatal:
                    replay_failure = fatal[:1200]
                if missing and row.get("errors") and not replay_failure:
                    requests = []
                    for channel in sorted(missing):
                        request = allocate(preview, channel)
                        allocations[(key, channel)] = request
                        requests.append(request)
                    try:
                        repaired = capture([{"key": key, "preview": preview, "requests": requests}])
                        repaired_rows = repaired.get("items", [])
                        if (repaired.get("captureSessionId") != document["captureSessionId"]
                                or repaired.get("replayRevision") != reply.get("replayRevision")
                                or repaired.get("originalEventId") != reply.get("originalEventId")
                                or len(repaired_rows) != 1 or repaired_rows[0].get("key") != key
                                or repaired_rows[0].get("componentCount") != count
                                or channel_groups(repaired_rows[0].get("channelGroups"), count) != plan):
                            raise ValueError("Channel repair returned different capture, revision or texture groups")
                        repaired_row = repaired_rows[0]
                        new_receipts = repaired_row.get("receipts", [])
                        repaired_channels = {item.get("channel") for item in new_receipts}
                        if not repaired_channels <= missing or len(repaired_channels) != len(new_receipts):
                            raise ValueError("Channel repair returned unrelated or duplicated groups")
                        row = dict(row, receipts=list(row.get("receipts", [])) + new_receipts,
                                   errors=[error for error in row.get("errors", [])
                                           if error.get("channel") not in repaired_channels])
                    except Exception as error:
                        row = dict(row, errors=list(row.get("errors", [])) + [{"error": str(error)[:1200]}])
                captured, errors = set(), list(row.get("errors", []))
                groups = {item["channel"]: item for item in plan}
                receipts = row.get("receipts", [])
                if not isinstance(receipts, list) or len(receipts) > 4:
                    raise ValueError("Channel precache returned too many channel receipts")
                for item in receipts:
                    channel = item.get("channel")
                    if channel not in expected or channel in captured:
                        raise ValueError("Channel precache returned an unrelated or duplicated group")
                    try:
                        image = accept(preview, item, allocations[(key, channel)], reply)
                        group = groups[channel]
                        previous = next((existing for existing in stage["images"]
                                         if existing.get("group") == "channels" and existing.get("parentImageIndex") == index
                                         and ("rgb" if existing.get("channel") == "rgba" else existing.get("channel")) == channel), {})
                        image.update(group="channels", parentImageIndex=index, presentation="detail",
                                     title=previous.get("title") or preview.get("title", ""),
                                     channelMeaning=group.get("label") or previous.get("channelMeaning") or preview.get("channelMeaning", ""),
                                     semantic=previous.get("semantic") or preview.get("semantic", ""),
                                     description=group.get("description") or previous.get("description") or preview.get("description", ""),
                                     evidence=previous.get("evidence") or preview.get("evidence", ""))
                        old = next((i for i, existing in enumerate(stage["images"])
                                    if existing.get("group") == "channels" and existing.get("parentImageIndex") == index
                                    and ("rgb" if existing.get("channel") == "rgba" else existing.get("channel")) == channel), None)
                        if old is not None:
                            stage["images"][old] = image
                        elif len(stage["images"]) < 64 and total < 512:
                            stage["images"].append(image)
                            total += 1
                        else:
                            raise ValueError("Channel precache reached the diagram's bounded image budget")
                        captured.add(channel)
                    except Exception as error:
                        errors.append({"channel": channel, "error": str(error)[:1200]})
                complete = captured == expected and not errors
                preview.update(channelCacheComplete=complete, channelCacheRevision=deepcopy(reply.get("replayRevision")),
                               channelCacheSession=document["captureSessionId"])
                if not complete:
                    result["failed"] += 1
                    result["errors"].append({"parent": key, "missingChannels": sorted(expected - captured), "errors": errors})
                else:
                    result["prepared"] += 1
            except Exception as error:
                result["failed"] += 1
                result["errors"].append({"parent": key, "error": str(error)[:1200]})
    result["complete"] = result["failed"] == 0 and not replay_failure
    if volume_capture is not None and not replay_failure:
        from .frame_flow_volume_precache import precache_volumes
        volumes = precache_volumes(document, allocate, volume_capture, accept)
        result["volumes"] = volumes
        result["complete"] = result["complete"] and volumes["complete"]
        # Surface a controller failure to the publication recovery policy, while
        # retaining every individually admitted preview in the document.
        result["errors"].extend(volumes["errors"])
    return result
