"""External native channel-precache acceptance; never launches an app or an AI.

Run with the Host venv against an already-open, explicitly selected isolated
qrenderdoc instance. All new evidence/reports stay below --output-root in OS temp.
The existing saved diagram supplies exact texture identities; its source RDC,
project, sidecar and publication are never changed. Both channel plans below
exercise physical extraction, without publishing invented semantic meanings.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--local-app-data", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--event", type=int)
    parser.add_argument("--resource")
    args = parser.parse_args()
    temporary = Path(tempfile.gettempdir()).resolve()
    root = Path(args.output_root).resolve()
    local = Path(args.local_app_data).resolve()
    for path in (root, local):
        if path == temporary or not path.is_relative_to(temporary):
            raise ValueError("Native acceptance paths must be isolated below OS temp")
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / "result.json"
    if report_path.exists():
        raise ValueError("Use a fresh acceptance output directory")
    os.environ["LOCALAPPDATA"] = str(local)
    os.environ["REPLAYLAB_INSTANCE_ID"] = args.instance_id
    # An outer development shell may point Host stores at the user's profile;
    # this acceptance runner uses only the explicitly isolated instance root.
    for key in ("RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE", "RENDERDOC_ULTRA_PROJECTS_HOME",
                "RENDERDOC_ULTRA_SHADER_TOOLS_CONFIG", "RENDERDOC_ULTRA_SHADER_TOOLCHAINS_CONFIG",
                "RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY", "RENDERDOC_ULTRA_SHADER_REPRESENTATIONS_ROOT",
                "RENDERDOC_ULTRA_RESOURCE_EXPORT_ROOT", "RENDERDOC_ULTRA_UNPACKED_ASSET_JOB_ROOT",
                "REPLAYLAB_CONFIG_DIR", "REPLAYLAB_RUNTIME_DIR", "REPLAYLAB_HOST_INSTANCE_ID"):
        os.environ.pop(key, None)
    os.environ["RENDERDOC_ULTRA_EVENT_TEXTURE_EVIDENCE_ROOT"] = str(root / "evidence")
    os.environ["RENDERDOC_ULTRA_EVENT_ARTIFACTS_HOME"] = str(root / "artifacts")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcp_host"))
    from src import server
    from src.ipc_client import IPCClient
    server.ipc = IPCClient(instance_id=args.instance_id, timeout=300)
    result = {"ok": False, "phase": "connecting", "instanceId": args.instance_id, "checks": []}
    started = time.monotonic()

    def save():
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def check(name, passed):
        if not passed:
            raise AssertionError(name)
        result["checks"].append(name)

    save()
    try:
        context = server._active_live_context(allow_missing_capture_id=True, require_event_state=True)
        session = context["capture"]["captureSessionId"]
        capture_path = Path(context["capture"]["path"])
        before = capture_path.stat()
        source_identity = (before.st_size, before.st_mtime_ns)
        result.update(captureSessionId=session, originalEventId=context["events"]["currentEventId"])
        with server.ipc.bind_capture_session(session):
            saved = server.ipc.call("get_saved_frame_render_flow", {"expected_capture_session_id": session}, timeout=120)
            candidates = [image for stage in saved.get("document", {}).get("stages", [])
                          for image in stage.get("images", [])
                          if image.get("group") not in ("channels", "animation-frames")
                          and image.get("attachmentKind") in ("rtv", "srv", "uav", "present")
                          and image.get("componentCount") != 1
                          and (args.event is None or image.get("eventId") == args.event)
                          and (args.resource is None or str(image.get("resourceId")) == args.resource)]
            check("saved diagram contains an exact selectable texture", bool(candidates))
            # Prefer an observed four-component format to exercise genuine A.
            candidates.sort(key=lambda image: image.get("componentCount") != 4)
            selected = copy.deepcopy(candidates[0])
            result["selected"] = {key: selected.get(key) for key in
                ("eventId", "resourceId", "attachmentKind", "attachmentIndex", "attachmentStage", "componentCount")}
            mapping = selected.get("displayMapping", {})
            for key in ("mip", "slice", "sample"):
                check("saved parent includes exact " + key, isinstance(mapping.get(key), int))

            document = {"captureSessionId": session, "capturePath": str(capture_path), "stages": []}
            for name, channels in (("rgb-alpha", ("rgb", "a")), ("packed-components", tuple("rgba"))):
                parent = copy.deepcopy(selected)
                parent.update(group="outputs", channelGroups=[{"channel": channel} for channel in channels])
                document["stages"].append({"id": name, "images": [parent]})
            result["phase"] = "native channel generation"
            save()
            cache = server._precache_frame_flow_channels(document, context)
            result["channelCache"] = cache
            check("all requested native channel groups passed Host evidence validation", cache["complete"])
            after_context = server._require_same_active_capture(context, allow_missing_capture_id=True,
                                                               require_event_state=True)
            check("selected EID restored after all channel batches",
                  after_context["events"]["currentEventId"] == context["events"]["currentEventId"])
            all_channels = []
            for stage in document["stages"]:
                parent = stage["images"][0]
                plan = parent["channelGroups"]
                expected = {row["channel"] for row in plan} if len(plan) > 1 else set()
                actual = {"rgb" if image["channel"] == "rgba" else image["channel"] for image in stage["images"][1:]}
                check(stage["id"] + " contains exactly its real channel groups", expected == actual)
                check(stage["id"] + " is complete before opening any popup", parent["channelCacheComplete"])
                check(stage["id"] + " does not invent alpha", parent["componentCount"] == 4 or "a" not in actual)
                for image in stage["images"][1:]:
                    manifest_path = Path(image["manifestPath"])
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    output = manifest["artifact"]["output"]
                    data = Path(image["path"]).read_bytes()
                    check(stage["id"] + " " + image["channel"] + " has matching persisted PNG hash",
                          hashlib.sha256(data).hexdigest() == image["sha256"] == output["sha256"])
                    check(stage["id"] + " " + image["channel"] + " has exact capture identity",
                          manifest["capture"]["captureSessionId"] == session and image["eventId"] == selected["eventId"]
                          and str(image["resourceId"]) == str(selected["resourceId"]))
                    check(stage["id"] + " " + image["channel"] + " preserves opaque RGB before scaling",
                          output.get("reduction", {}).get("opaqueRgbProcessed") is True)
                    all_channels.append({"stage": stage["id"], "channel": image["channel"], "path": image["path"],
                                         "manifestPath": image["manifestPath"], "sha256": image["sha256"]})
            result["channels"] = all_channels
            # Persist and read back the accepted preview data without touching
            # the user's portable flow sidecar or invoking save_frame_render_flow.
            data_path = root / "accepted-channel-document.json"
            data_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
            restored = json.loads(data_path.read_text(encoding="utf-8"))
            check("saved preview identities and plans survive JSON restoration", restored == document)
            source_after = capture_path.stat()
            check("source RDC was not modified", source_identity == (source_after.st_size, source_after.st_mtime_ns))
            result.update(ok=True, documentPath=str(data_path), phase="complete")
    except Exception:
        result.update(phase="failed", error=traceback.format_exc())
    result["elapsedSeconds"] = time.monotonic() - started
    save()
    print(json.dumps({"ok": result["ok"], "report": str(report_path), "checks": len(result["checks"])}))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
