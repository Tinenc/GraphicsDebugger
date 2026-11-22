"""AI-owned dependency tracing, asset layout and Unicode document operations."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def write_document(path, content):
    target = Path(path).expanduser()
    if not target.is_absolute() or target.suffix.lower() not in {".md", ".markdown", ".html", ".htm", ".txt"}:
        raise ValueError("Use an absolute Markdown, HTML or text document path")
    if not isinstance(content, str) or len(content.encode("utf-8")) > 8 * 1024 * 1024:
        raise ValueError("Document content must be bounded Unicode text")
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = content.replace("\r\r\n", "\n").replace("\r\n", "\n").encode("utf-8-sig")
    descriptor, pending = tempfile.mkstemp(prefix=".bread-document-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw); output.flush(); os.fsync(output.fileno())
        os.replace(pending, target)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)
    assert target.read_bytes() == raw
    return {"path": str(target), "sha256": hashlib.sha256(raw).hexdigest(), "encoding": "utf-8-bom",
            "unicodeRoundTrip": target.read_text(encoding="utf-8-sig") == raw.decode("utf-8-sig"),
            "questionMarkRuns": len(re.findall(r"\?{3,}", content)), "replacementCharacters": content.count("\ufffd"),
            "diagnosticsOnly": True, "nextAction": "Read the saved text. Existing question marks require rewriting from original Unicode source, not encoding conversion."}


def asset_layout(assets, root):
    root = str(root).replace("\\", "/").rstrip("/")
    if not root.startswith("Assets/") or ".." in root.split("/"):
        raise ValueError("Use a project-local Assets root")
    folders = {"model": "Models", "material": "Materials", "prefab": "Prefabs", "texture": "Textures",
               "scene": "Scenes", "shader": "Shaders", "runtime": "Runtime", "editor": "Editor"}
    groups = {}
    for asset in assets:
        kind = asset.get("kind")
        if kind not in folders:
            raise ValueError("Asset kind must be model/material/prefab/texture/scene/shader/runtime/editor")
        source = str(asset["path"]).replace("\\", "/")
        if not source.startswith("Assets/") or ".." in source.split("/"):
            raise ValueError("Asset paths must stay within Assets")
        # Equal bytes alone do not identify equal texture sampling/import use.
        can_share = kind == "texture" and asset.get("contentSha256") and asset.get("samplingIdentity")
        identity = (kind, asset.get("contentSha256") if can_share else source,
                    json.dumps(asset.get("samplingIdentity") or {}, sort_keys=True))
        groups.setdefault(identity, []).append(asset)
    moves, aliases, collisions, targets = [], [], [], {}
    for group in groups.values():
        asset = min(group, key=lambda item: (not bool(item.get("nameOverride")), item["path"]))
        eids = sorted({int(eid) for row in group for eid in row.get("eventIds", [])})
        role = str(asset.get("nameOverride") or asset.get("role") or Path(asset["path"]).stem)
        role = re.sub(r"(?:[-_][a-fA-F0-9]{8,64})+$", "", role)
        role = re.sub(r"^EID\d+[_-]", "", role, flags=re.I)
        role = re.sub(r"[^\w.-]+", "_", role).strip("._") or asset["kind"].title()
        prefix = "EID%d_" % eids[0] if len(eids) == 1 else ""
        target = root + "/" + folders[asset["kind"]] + "/" + prefix + role + Path(asset["path"]).suffix
        if target in targets:
            collisions.append({"target": target, "sources": [targets[target], asset["path"]],
                "nextAction": "Supply a meaningful nameOverride for different assets sharing one semantic role; no hash suffix or overwrite was generated."})
            continue
        targets[target] = asset["path"]
        moves.append({"source": asset["path"], "target": target, "eventIds": eids})
        aliases.extend({"source": row["path"], "canonical": target} for row in group if row is not asset)
    return {"schema": "bread-unity-asset-layout/v1", "assetRoot": root, "moves": moves, "textureAliases": aliases,
            "collisions": collisions, "diagnosticsOnly": True,
            "guidPolicy": "Move assets through AssetDatabase; retain .meta GUIDs, rebind duplicate texture references and preserve archived originals."}


def trace_dependencies(event_ids, inspect, usage, texture_info, max_depth=8, max_nodes=128):
    """Trace actual prior write candidates; do not infer staticness from dimension."""
    pending = [(int(eid), 0) for eid in event_ids]
    events, edges, usage_cache, texture_cache, missing = {}, [], {}, {}, []
    writers = re.compile(r"RWResource|ColorTarget|DepthStencilTarget|StreamOut|CopyDst|ResolveDst|(?:^|\.)Copy$|(?:^|\.)Resolve$|Clear|GenMips|CPUWrite|Discard", re.I)
    while pending and len(events) < max_nodes:
        eid, level = pending.pop(0)
        if eid in events:
            continue
        try:
            snapshot = inspect(eid)
        except Exception as error:
            events[eid] = {"eventId": eid, "inspectionError": str(error)}
            missing.append({"eventId": eid, "reason": "inspection-unavailable", "diagnostic": str(error)})
            continue
        events[eid] = snapshot
        bindings = list(snapshot.get("textureInputs", []))
        bindings.extend(snapshot.get("inputs", []))
        # Utility copies/resolves and depth/blend reads live in the detailed
        # action facts, rather than the Shader descriptor inventory.
        bindings.extend((snapshot.get("details") or {}).get("inputs", []))
        bindings.extend(row for row in snapshot.get("expectedOutputs", []) if row.get("kind") == "uav")
        seen_bindings = set()
        for binding in bindings:
            rid = int(binding.get("resourceId") or 0)
            identity = (rid, binding.get("kind", binding.get("role", "")), binding.get("stage"), binding.get("index"))
            if not rid or identity in seen_bindings:
                continue
            seen_bindings.add(identity)
            if rid not in usage_cache:
                try:
                    usage_cache[rid] = usage(rid)
                except Exception as error:
                    usage_cache[rid] = {"candidates": [], "queryError": str(error)}
                try:
                    texture_cache[rid] = texture_info(rid)
                except Exception as error:
                    texture_cache[rid] = {"metadataError": str(error)}
            candidates = [row for row in usage_cache[rid].get("candidates", []) if
                0 < int(row.get("eventId") or 0) < eid and writers.search(str(row.get("usage", "")))]
            candidates.sort(key=lambda row: int(row["eventId"]), reverse=True)
            # Keep multiple writers: atlas tiles and accumulation are not full overwrites.
            chosen = candidates[:16]
            history = [row for row in usage_cache[rid].get("candidates", []) if
                int(row.get("eventId") or 0) >= eid and writers.search(str(row.get("usage", "")))][:16]
            edge = {"consumerEventId": eid, "resourceId": str(rid), "binding": binding,
                    "producerCandidates": chosen, "producerCandidatesTruncated": len(candidates) > len(chosen),
                    "queryError": usage_cache[rid].get("queryError"),
                    "texture": texture_cache[rid], "resourceLevelOnly": True,
                    "crossFrameOrImmutableCandidate": not candidates,
                    "historyUpdateCandidates": history if not candidates else [],
                    "staticness": "AI must inspect coordinates, writers and history; 3D does not imply static"}
            edges.append(edge)
            if level < max_depth:
                pending.extend((int(row["eventId"]), level + 1) for row in chosen)
                if not candidates:
                    pending.extend((int(row["eventId"]), level + 1) for row in history)
            elif chosen:
                missing.append({"eventId": eid, "resourceId": str(rid), "reason": "depth-budget"})
    return {"schema": "bread-effect-dependencies/v1", "events": events, "resources": edges,
            "remainingEvents": sorted({eid for eid, level in pending if eid not in events}), "limits": missing,
            "diagnosticsOnly": True, "acceptanceBy": "agent",
            "nextAction": "Inspect candidate producer shaders and subresources, port the actual pass/compute chain and manage live history. Try a justified approximation only after investigating a concrete portability limit."}
