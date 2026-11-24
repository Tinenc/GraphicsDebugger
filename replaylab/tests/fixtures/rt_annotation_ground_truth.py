"""Read the controlled capture using the matching bundled renderdoc Python module."""
import json
import sys
from pathlib import Path
import renderdoc as rd

capture, destination = Path(sys.argv[1]), Path(sys.argv[2])
destination.mkdir(exist_ok=True)
rd.InitialiseReplay(rd.GlobalEnvironment(), [])
cap = rd.OpenCaptureFile()
result = cap.OpenFile(str(capture), '', None)
if result != rd.ResultCode.Succeeded:
    raise RuntimeError(str(result))
result, controller = cap.OpenCapture(rd.ReplayOptions(), None)
if result != rd.ResultCode.Succeeded:
    raise RuntimeError(str(result))
try:
    names = {str(resource.resourceId): resource.name for resource in controller.GetResources()}
    textures = {str(texture.resourceId): texture for texture in controller.GetTextures()}
    draws = []
    def visit(actions):
        for action in actions:
            if action.flags & rd.ActionFlags.Drawcall:
                draws.append(action)
            visit(action.children)
    visit(controller.GetRootActions())
    evidence = {'draws': [], 'resources': [], 'pixels': []}
    for action in draws:
        controller.SetFrameEvent(action.eventId, True)
        pipe = controller.GetPipelineState()
        reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
        row = {'eventId': action.eventId, 'indices': action.numIndices,
               'outputs': [{'resourceId': str(output.resource), 'name': names.get(str(output.resource))}
                           for output in pipe.GetOutputTargets() if output.resource != rd.ResourceId.Null()],
               'pixelShader': str(pipe.GetShader(rd.ShaderStage.Pixel)),
               'entryPoint': reflection.entryPoint if reflection else '',
               'enumeratedEntries': [entry.name for entry in controller.GetShaderEntryPoints(pipe.GetShader(rd.ShaderStage.Pixel))],
               'inputs': [{'resourceId': str(item.descriptor.resource), 'name': names.get(str(item.descriptor.resource))}
                          for item in pipe.GetReadOnlyResources(rd.ShaderStage.Pixel, True)]}
        evidence['draws'].append(row)
        legacy = rd.ShaderEntryPoint()
        legacy.name, legacy.stage = 'main', rd.ShaderStage.Pixel
        legacy_reflection = controller.GetShader(rd.ResourceId.Null(), pipe.GetShader(rd.ShaderStage.Pixel), legacy)
        row['legacyMainAliasPreservesIdentity'] = (legacy_reflection.resourceId == reflection.resourceId and
                                                 bytes(legacy_reflection.rawBytes) == bytes(reflection.rawBytes))
    evidence['finalEventId'] = draws[-1].eventId
    for resource_id, texture in textures.items():
        label = names.get(resource_id, '')
        if label not in ('SceneHDR', 'BloomBlur', 'FinalAnnotatedOutput'):
            continue
        save = rd.TextureSave()
        save.resourceId, save.destType, save.mip = texture.resourceId, rd.FileType.PNG, 0
        save.slice.sliceIndex, save.sample.sampleIndex = 0, 0
        save.alpha = rd.AlphaMapping.Discard
        path = destination / (label + '.png')
        status = controller.SaveTexture(save, str(path))
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError(str(status))
        evidence['resources'].append({'name': label, 'resourceId': resource_id,
                                      'width': texture.width, 'height': texture.height, 'image': str(path)})
        for x, y in ((94, 330), (220, 330), (740, 330), (70, 80)):
            pixel = controller.PickPixel(texture.resourceId, x, y, rd.Subresource(0, 0, 0), rd.CompType.Typeless)
            evidence['pixels'].append({'resource': label, 'x': x, 'y': y, 'rgba': list(pixel.floatValue)})
    (destination / 'ground-truth.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    print(json.dumps(evidence))
finally:
    controller.Shutdown()
    cap.Shutdown()
    rd.ShutdownReplay()
