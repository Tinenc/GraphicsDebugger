"""Run with qrenderdoc --ui-script to validate a named capture's EID replay.

Set RENDERDOC_EID_VALIDATE_OUTPUT to an absolute JSON path and
RENDERDOC_EID_VALIDATE_EVENTS to comma-separated, capture-specific event IDs.
Readbacks are deliberately separate from the interactive timing benchmark.
The script closes only its own qrenderdoc process when it finishes.
"""

import hashlib
import json
import os
import time
import traceback

import renderdoc as rd
from PySide2 import QtCore, QtWidgets


application = QtWidgets.QApplication.instance()
output_path = os.environ["RENDERDOC_EID_VALIDATE_OUTPUT"]
events = [int(value) for value in os.environ["RENDERDOC_EID_VALIDATE_EVENTS"].split(",")]
result = {"schema": "renderdoc-eid-replay-validation/v1", "events": [], "ok": False}
deadline = time.monotonic() + 180.0
event_outputs = {}
display_resource = None


def publish():
    with open(output_path, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
    print("RENDERDOC_EID_VALIDATION=" + json.dumps(result, sort_keys=True))
    application.closeAllWindows()
    QtCore.QTimer.singleShot(0, lambda: application.exit(0))


def read_targets(controller, event):
    pipe = controller.GetPipelineState()
    targets = list(pipe.GetOutputTargets()) + [pipe.GetDepthTarget()]
    textures = {texture.resourceId: texture for texture in controller.GetTextures()}
    specifications = [(target.resource, max(0, int(target.firstMip)),
                       max(0, int(target.firstSlice))) for target in targets]
    specifications += [(resource, 0, 0) for resource in event_outputs.get(event, [])]
    if display_resource is not None:
        specifications.append((display_resource, 0, 0))
    records = []
    seen = set()
    for resource, mip, slice_index in specifications:
        if resource == rd.ResourceId.Null() or resource not in textures:
            continue
        key = (str(resource), mip, slice_index)
        if key in seen:
            continue
        seen.add(key)
        texture = textures[resource]
        # Sample zero is explicit for MSAA. Hash native bytes, preserving HDR precision.
        data = bytes(controller.GetTextureData(resource, rd.Subresource(mip, slice_index, 0)))
        if not data:
            raise RuntimeError("Empty texture readback for %s" % resource)
        records.append({
            "resource": str(resource), "mip": mip, "slice": slice_index, "sample": 0,
            "format": texture.format.Name(), "width": int(texture.width),
            "height": int(texture.height), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    if not records:
        raise RuntimeError("Selected EID has no readable render/depth target")
    return records


def validate(controller):
    try:
        result["api"] = str(controller.GetAPIProperties().pipelineType)
        result["conservativeSync"] = os.environ.get(
            "RENDERDOC_D3D12_CONSERVATIVE_REPLAY_SYNC", "0") == "1"
        for event in events:
            # Every visit uses the canonical real scrub path. Read back the result independently
            # of UI presentation and compare it with ordinary forced event selection.
            controller.ScrubFrameEvent(event)
            controller.CommitScrubFrameEvent(event)
            scrub = read_targets(controller, event)
            controller.SetFrameEvent(event, True)
            canonical = read_targets(controller, event)
            entry = {"eventId": event, "scrub": scrub, "canonical": canonical,
                     "identical": scrub == canonical}
            result["events"].append(entry)
        if os.environ.get("RENDERDOC_EID_VALIDATE_REPEAT") == "1":
            event = events[-1]
            result["repeat"] = []
            for lane in ("scrub", "scrub", "canonical", "canonical", "scrub"):
                if lane == "scrub":
                    controller.ScrubFrameEvent(event)
                    controller.CommitScrubFrameEvent(event)
                else:
                    controller.SetFrameEvent(event, True)
                result["repeat"].append({"lane": lane, "eventId": event,
                                         "targets": read_targets(controller, event)})
        result["debugMessages"] = [
            {"severity": str(message.severity), "description": message.description}
            for message in controller.GetDebugMessages()
        ]
        result["ok"] = all(entry["identical"] for entry in result["events"])
    except Exception:
        result["error"] = traceback.format_exc()
    pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(publish)


def start():
    global display_resource
    display_resource = pyrenderdoc.GetTextureViewer().GetCurrentResource()
    result["actions"] = []
    for event in events:
        action = pyrenderdoc.GetAction(event)
        if action is not None:
            event_outputs[event] = list(action.outputs) + [action.depthOut, action.copyDestination,
                                                           action.copySource]
            result["actions"].append({"eventId": event, "flags": str(action.flags),
                                      "customName": str(action.customName),
                                      "copySource": str(action.copySource),
                                      "copyDestination": str(action.copyDestination)})
    pyrenderdoc.Replay().AsyncInvoke(validate)


def ready():
    if pyrenderdoc.IsCaptureLoaded():
        result["capture"] = pyrenderdoc.GetCaptureFilename()
        pyrenderdoc.ShowTextureViewer()
        QtCore.QTimer.singleShot(1000, start)
    elif time.monotonic() < deadline:
        QtCore.QTimer.singleShot(250, ready)
    else:
        result["error"] = "Capture load timed out"
        publish()


pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(ready)
