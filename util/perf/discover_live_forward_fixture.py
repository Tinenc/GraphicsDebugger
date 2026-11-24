"""Read exact draw/texture identities from the bounded D3D12_RenderTarget_Binds fixture.

Opens the explicitly named capture and uses the GPU. Does not modify capture/project files.
Same-submission eligibility and changed intermediate pixels are proved by the separate replay
probe, never inferred from this metadata discovery.
"""
from __future__ import print_function

import argparse
import json
from pathlib import Path
import sys
import traceback

from eid_checkpoint_profile import selected_event_evidence
from eid_history_profile import Recorder, resource_number
from eid_shared_buffer_profile import binary_identity, build_identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    capture, output = Path(args.capture).resolve(), Path(args.output).resolve()
    if not capture.is_file() or output.exists():
        parser.error("Capture must exist and output must be new")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema": "renderdoc-live-forward-fixture/v1", "ok": False,
              "capture": str(capture), "samples": [],
              "limits": ["Metadata only; native continuation and output changes unverified"]}
    recorder = Recorder(report, output, output.with_suffix(".rdoc.log"), 120)
    rd, cap, controller, initialized = None, None, None, False
    try:
        import renderdoc as rd
        report["buildIdentity"] = build_identity(rd, None)
        report["captureIdentity"] = binary_identity(capture)
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        initialized = True
        rd.SetDebugLogFile(str(recorder.log_path))
        cap = rd.OpenCaptureFile()
        if cap.OpenFile(str(capture), "", None) != rd.ResultCode.Succeeded:
            raise RuntimeError("Capture OpenFile failed")
        if str(cap.DriverName()) != "D3D12":
            raise RuntimeError("Requires the D3D12 fixture capture")
        opened, _ = recorder.measure("capture.openReplay", lambda: cap.OpenCapture(rd.ReplayOptions(), None))
        result, controller = opened
        if result != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("Capture OpenCapture failed: " + str(result))
        draws = []
        def walk(actions):
            for action in actions:
                if action.flags & rd.ActionFlags.Drawcall:
                    if action.flags & rd.ActionFlags.Indirect:
                        raise RuntimeError("Fixture contains an indirect draw")
                    draws.append(action)
                walk(action.children)
        walk(controller.GetRootActions())
        if len(draws) != 4:
            raise RuntimeError("Fixture must contain exactly four actual draws")
        events = [int(draw.eventId) for draw in draws]
        if events != sorted(set(events)):
            raise RuntimeError("Draw event identities are not distinct and ascending")
        ids = sorted({resource_number(resource) for draw in draws for resource in draw.outputs
                      if resource_number(resource) not in (None, 0)})
        if len(ids) != 8:
            raise RuntimeError("Fixture must have eight distinct color targets")
        metadata = controller.GetTextures()
        report["textures"] = [{"resourceId": resource_number(texture.resourceId),
                               "width": int(texture.width), "height": int(texture.height),
                               "depth": int(texture.depth), "mips": int(texture.mips),
                               "arraysize": int(texture.arraysize), "format": texture.format.Name()}
                              for texture in metadata if resource_number(texture.resourceId) in ids]
        if len(report["textures"]) != len(ids):
            raise RuntimeError("Not every bound output has native texture metadata")
        report["events"], report["resources"] = events, ids
        report["selectedEventEvidence"] = selected_event_evidence(controller, events)
        if any(row["status"] != "exactAPIEvent" for row in report["selectedEventEvidence"]):
            raise RuntimeError("Missing exact draw API-event evidence")
        report["profileArguments"] = ["--capture", str(capture), "--events",
                                      ",".join(map(str, events)), "--resources", ",".join(map(str, ids))]
        report["ok"] = True
    except Exception as error:
        report.update(ok=False, error=str(error), traceback=traceback.format_exc())
    finally:
        for label, callback in (("cleanup.replay", controller.Shutdown if controller else None),
                                ("cleanup.capture", cap.Shutdown if cap else None),
                                ("cleanup.runtime", rd.ShutdownReplay if initialized else None)):
            if callback is not None:
                try:
                    recorder.measure(label, callback, cleanup=True)
                except Exception as error:
                    report.setdefault("cleanupErrors", []).append({"phase": label, "error": str(error)})
                    report["ok"] = False
        recorder.publish()
    print(json.dumps({"ok": report["ok"], "output": str(output),
                      "events": report.get("events"), "resources": report.get("resources")}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
