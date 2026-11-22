"""Read-only native replay of the user's supplied regression capture.

Run through qrenderdoc --ui-script on a private desktop/profile. It never opens
the file in CaptureContext, applies project restoration or changes the live app.
"""
import hashlib
import json
import os
import traceback
import importlib.util
import renderdoc as rd
from PySide2 import QtCore

output = r'D:\Study\GitHub\renderdocUltra\output\bread-shader-rdc-106'
os.makedirs(output, exist_ok=True)
report = {'ok': False, 'checks': [], 'events': []}
bridge_source = r'D:\Study\GitHub\renderdocUltra\replaylab\renderdoc_mcp_bridge'
spec = importlib.util.spec_from_file_location('bread_probe_bridge', os.path.join(bridge_source, '__init__.py'), submodule_search_locations=[bridge_source])
bridge = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)
capture = controller = None
try:
    capture = rd.OpenCaptureFile()
    status = capture.OpenFile(r'C:\Users\YC\Downloads\上衣_剔除1.rdc', '', None)
    report['openFile'] = str(status)
    if status != rd.ResultCode.Succeeded:
        raise RuntimeError('Supplied RDC did not open: ' + str(status))
    status, controller = capture.OpenCapture(rd.ReplayOptions(), None)
    report['openReplay'] = str(status)
    if status != rd.ResultCode.Succeeded:
        raise RuntimeError('Native replay unavailable: ' + str(status))
    report['api'] = str(controller.GetAPIProperties().pipelineType)
    def field(variable):
        t = variable.type
        result = {'name': variable.name, 'byteOffset': int(variable.byteOffset)}
        result['type'] = {name: str(getattr(t, name, 'missing')) for name in (
            'name', 'baseType', 'rows', 'columns', 'elements', 'arrayByteStride', 'matrixByteStride')}
        result['members'] = [field(member) for member in getattr(t, 'members', [])]
        return result
    for eid in (7195, 8747):
        controller.SetFrameEvent(eid, True)
        pipeline = controller.GetPipelineState()
        shader = pipeline.GetShader(rd.ShaderStage.Fragment)
        entries = list(controller.GetShaderEntryPoints(shader))
        entry = next((e for e in entries if e.stage == rd.ShaderStage.Fragment), entries[0])
        pso = pipeline.GetGraphicsPipelineObject()
        reflection = controller.GetShader(pso, shader, entry)
        raw = bytes(reflection.rawBytes)
        prefix = 'EID%d-fragment' % eid
        with open(os.path.join(output, prefix + '.bin'), 'wb') as stream: stream.write(raw)
        row = {'eventId': eid, 'shaderResourceId': str(int(shader)), 'entry': entry.name,
            'encoding': str(reflection.encoding), 'rawSha256': hashlib.sha256(raw).hexdigest(),
            'blocks': [{'name': block.name, 'byteSize': int(block.byteSize),
                'bind': int(block.fixedBindNumber), 'space': int(block.fixedBindSetOrSpace),
                'bufferBacked': bool(block.bufferBacked), 'fields': [field(v) for v in block.variables]}
                for block in reflection.constantBlocks]}
        row['compiledInterface'] = bridge._compiled_shader_interface(reflection)
        report['events'].append(row)
        if not row['compiledInterface']['interfaceComplete']:
            raise RuntimeError('Original interface is still incomplete')
        if eid == 7195:
            rt = pipeline.GetOutputTargets()[0].resource
            original_output = bytes(controller.GetTextureData(rt, rd.Subresource()))
            with open(os.path.join(output, 'roundtrip-before.rtdata'), 'wb') as stream: stream.write(original_output)
            with open(os.path.join(output, 'roundtrip.dxbc'), 'rb') as stream: candidate_bytes = stream.read()
            target, messages = controller.BuildTargetShader('main', rd.ShaderEncoding.DXBC,
                candidate_bytes, rd.ShaderCompileFlags(), rd.ShaderStage.Fragment)
            if not target: raise RuntimeError('Native target build failed: ' + messages)
            applied = False
            try:
                candidate_entry = next(e for e in controller.GetShaderEntryPoints(target) if e.name == 'main')
                candidate = controller.GetShader(rd.ResourceId(), target, candidate_entry)
                row['candidateInterface'] = bridge._compiled_shader_interface(candidate)
                row['nativeInterfaceCompatible'] = bridge._physical_shader_interfaces_compatible(reflection, candidate)
                if not row['nativeInterfaceCompatible']:
                    row['incompatibilities'] = bridge._interface_incompatibilities(
                        bridge._physical_shader_interface(row['compiledInterface']),
                        bridge._physical_shader_interface(row['candidateInterface']))
                    raise RuntimeError('Roundtrip ABI mismatch')
                diagnostic = controller.ReplaceTargetShaderChecked(shader, target)
                if diagnostic: raise RuntimeError(diagnostic)
                applied = True
                controller.SetFrameEvent(eid, True)
                changed = bytes(controller.GetTextureData(rt, rd.Subresource()))
                with open(os.path.join(output, 'roundtrip-after.rtdata'), 'wb') as stream: stream.write(changed)
                row['privateReplayRoundtrip'] = {'applied': True, 'outputBytes': len(changed),
                    'exactBytes': changed == original_output, 'projectFilesModified': False}
            finally:
                if applied:
                    diagnostic = controller.RemoveTargetShaderReplacementChecked(shader)
                    if diagnostic: raise RuntimeError(diagnostic)
                controller.FreeTargetResource(target)
        try:
            disassembly = controller.DisassembleShader(pso, reflection, '')
            with open(os.path.join(output, prefix + '.asm'), 'w', encoding='utf-8') as stream: stream.write(disassembly)
        except Exception as error: row['disassemblyError'] = str(error)
    report['ok'] = True
    report['checks'] = ['supplied-file-native-replay', 'original-shader-bytes', 'original-cbuffer-reflection', 'no-project-restore-or-override']
except Exception as error:
    report['error'] = str(error)
    report['traceback'] = traceback.format_exc()
finally:
    if controller is not None: controller.Shutdown()
    if capture is not None: capture.Shutdown()
    with open(os.path.join(output, 'native-reflection.json'), 'w', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    with open(os.environ['BREAD_DIAG_UI_REPORT'], 'w', encoding='utf-8') as stream:
        json.dump({'ok': report['ok'], 'checks': report['checks'], 'error': report.get('error')}, stream)
    def finish():
        pyrenderdoc.GetMainWindow().Widget().close()
        QtCore.QTimer.singleShot(500, lambda: os._exit(0))
    pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(finish)
