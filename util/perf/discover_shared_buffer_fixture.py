"""Find the actual shared range and draw EIDs in a Shared_Buffer regression capture."""
from __future__ import print_function

import argparse
import json
from pathlib import Path
import sys
import traceback

from discover_d3d12_checkpoint_fixture import capture_hash, collect_actions
from eid_history_profile import resource_number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--allow-unbound-prefix", action="store_true",
                        help="Record fixture prefix draws without tint separately")
    args = parser.parse_args()
    capture_path, output = Path(args.capture).resolve(), Path(args.output).resolve()
    if not capture_path.is_file() or output.exists():
        parser.error("Capture must exist and output must be a new file")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema": "renderdoc-shared-buffer-fixture/v1", "ok": False,
              "capture": str(capture_path), "captureSha256": capture_hash(capture_path),
              "bindings": [], "issues": []}
    controller, capture_file, initialized = None, None, False
    try:
        import renderdoc as rd
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        initialized = True
        rd.SetDebugLogFile(str(output.with_suffix(".rdoc.log")))
        capture_file = rd.OpenCaptureFile()
        status = capture_file.OpenFile(str(capture_path), "", None)
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError("OpenFile failed: " + str(status))
        report["api"] = capture_file.DriverName()
        status, controller = capture_file.OpenCapture(rd.ReplayOptions(), None)
        if status != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(status))
        controller.ConfigureReplayCheckpoints(False, 0, 0)
        actions, _ = collect_actions(controller, rd)
        draws = [row for row in actions if row["draw"]]
        if not 2 <= len(draws) <= 8:
            raise ValueError("Fixture requires 2-8 actual draws")
        report["draws"] = draws
        resources = {resource.resourceId: resource for resource in controller.GetResources()}
        shared = None
        consumers = []
        report["unboundPrefixDraws"] = []
        for draw in draws:
            eid = draw["eventId"]
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()
            reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
            if reflection is None:
                raise ValueError("Draw has no fragment/pixel shader reflection")
            matches = []
            for index, block in enumerate(reflection.constantBlocks):
                for variable in block.variables:
                    if str(variable.name) == "tint":
                        binding = pipe.GetConstantBlock(rd.ShaderStage.Pixel, index, 0)
                        descriptor = binding.descriptor
                        if descriptor.resource == rd.ResourceId.Null():
                            raise ValueError("tint is not backed by a captured buffer")
                        if int(variable.type.rows) != 1 or int(variable.type.columns) != 4:
                            raise ValueError("Fixture tint must reflect a float4")
                        absolute = int(descriptor.byteOffset) + int(variable.byteOffset)
                        matches.append((descriptor.resource, absolute, index,
                                        int(descriptor.byteOffset), int(variable.byteOffset)))
            if not matches and args.allow_unbound_prefix and not consumers:
                report["unboundPrefixDraws"].append(draw)
                continue
            if len(matches) != 1:
                raise ValueError("Draw must have exactly one reflected tint")
            buffer, absolute, block_index, base, member = matches[0]
            identity = (resource_number(buffer), absolute)
            if shared is not None and shared != identity:
                raise ValueError("Draws do not share the same actual byte range")
            shared = identity
            consumers.append(draw)
            report["bindings"].append({"eventId": eid, "buffer": identity[0],
                                       "name": str(resources[buffer].name) if buffer in resources else "",
                                       "bindingOffset": base, "memberOffset": member,
                                       "absoluteOffset": absolute, "blockIndex": block_index,
                                       "tintBytes": bytes(controller.GetBufferData(buffer, absolute, 16)).hex()})
        if len(consumers) < 2:
            raise ValueError("Fixture requires at least two actual tint consumers")
        report["profileArguments"] = ["--capture", str(capture_path), "--events",
                                      ",".join(str(row["eventId"]) for row in consumers),
                                      "--buffer", str(shared[0]), "--offset", str(shared[1]),
                                      "--data-hex", "0000403f", "--require-neighbors-unchanged",
                                      "--require-identity-unchanged"]
        report["ok"] = True
    except Exception as error:
        report["issues"].append(str(error))
        report["traceback"] = traceback.format_exc()
    finally:
        if controller:
            controller.Shutdown()
        if capture_file:
            capture_file.Shutdown()
        if initialized:
            rd.ShutdownReplay()
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "output": str(output), "issues": report["issues"]}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
