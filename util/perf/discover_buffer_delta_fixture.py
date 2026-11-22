"""List actual scalar-fixture bindings; run with the isolated offline replay host."""
from __future__ import print_function
import argparse
import json
from pathlib import Path
import renderdoc as rd

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    cap, controller = None, None
    try:
        cap = rd.OpenCaptureFile()
        result = cap.OpenFile(args.capture, '', None)
        if result != rd.ResultCode.Succeeded:
            raise RuntimeError(str(result))
        result, controller = cap.OpenCapture(rd.ReplayOptions(), None)
        if result != rd.ResultCode.Succeeded:
            raise RuntimeError(str(result))
        controller.ConfigureReplayCheckpoints(False, 0, 0)
        names = {str(r.resourceId): r.name for r in controller.GetResources()}
        pending, draws = list(controller.GetRootActions()), []
        while pending:
            action = pending.pop(0)
            pending[0:0] = list(action.children)
            if action.flags & rd.ActionFlags.Drawcall:
                controller.SetFrameEvent(action.eventId, True)
                pipe = controller.GetPipelineState()
                reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                bindings = []
                for slot, block in enumerate(reflection.constantBlocks if reflection else []):
                    bound = pipe.GetConstantBlock(rd.ShaderStage.Pixel, slot, 0).descriptor
                    bindings.append({'slot': slot, 'block': block.name, 'buffer': str(bound.resource),
                                     'name': names.get(str(bound.resource), ''),
                                     'byteOffset': int(bound.byteOffset), 'byteSize': int(bound.byteSize)})
                draws.append({'eventId': int(action.eventId), 'bindings': bindings})
        report = {'capture': str(Path(args.capture).resolve()), 'draws': draws}
        Path(args.output).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report))
    finally:
        if controller is not None:
            controller.Shutdown()
        if cap is not None:
            cap.Shutdown()
        rd.ShutdownReplay()

if __name__ == '__main__':
    main()
