"""Bounded diagnostics for an already owned ApplicationAudit; no project/layout changes.

Load from the independent application's Python shell only after checking its PID,
creation time and executable. Keeps the existing capture and Shader replacements.
The original audit timer must be released again before manual delivery.
"""
import json
import math
import os
from pathlib import Path
import struct
import sys
import time

import renderdoc as rd
import qrenderdoc as qrd
from PySide2 import QtCore

audit = sys.modules['__main__']._bread_private_replay_audit
if not hasattr(audit, '_live_originals'):
    audit._live_originals = {name: getattr(type(audit), name) for name in
                             ('perform', 'prepare_cbuffer', 'poll_navigation')}


def poll_navigation(self):
    if os.environ.get('RENDERDOC_EID_TIMING') == '1':
        return self._live_originals['poll_navigation'](self)
    pending = self.pending_navigation
    if self.command.get('mode') == 'set_event':
        return
    if time.perf_counter() - pending['started'] > 30:
        raise RuntimeError('Live navigation presentation did not complete')
    if (int(self.ctx.CurEvent()) == pending['event'] and
            self.render_widget.property('eidDisplayedEvent') == pending['event'] and
            not self.render_widget.property('eidPresentationPending') and
            (not self.pipeline_widget.isVisible() or
             not self.pipeline_widget.property('replayDetailsPending'))):
        pending['completionEvidence'] = 'native current-EID presentation properties; 10ms observer'
        self.complete_navigation(None)


def prepare_cbuffer(self, ctrl):
    if not self.command.get('materialField'):
        return self._live_originals['prepare_cbuffer'](self, ctrl)
    pipe = ctrl.GetPipelineState()
    reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
    matches = []
    def visit(variables, base, index, block):
        for var in variables:
            offset = base + int(var.byteOffset)
            if var.type.members:
                visit(var.type.members, offset, index, block)
            elif str(var.name) == self.command['materialField']:
                if var.type.baseType != rd.VarType.Float or int(var.type.rows) != 1 or int(var.type.columns) != 1:
                    raise RuntimeError('The requested material field is not a reflected scalar float')
                matches.append((index, block, offset, str(var.name)))
    for index, block in enumerate(reflection.constantBlocks):
        visit(block.variables, 0, index, block)
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one physical reflected material field: ' + str(matches))
    index, block, offset, name = matches[0]
    binding = pipe.GetConstantBlock(rd.ShaderStage.Pixel, index, 0).descriptor
    if binding.resource == rd.ResourceId.Null():
        raise RuntimeError('A real bound material buffer is required')
    self.patch_resource, self.patch_base = binding.resource, int(binding.byteOffset)
    self.patch_size, self.patch_format = int(block.byteSize), '<f'
    self.patch_offset, self.patch_name = offset, name
    self.patch_baseline = bytes(ctrl.GetBufferData(binding.resource, self.patch_base, self.patch_size))
    self.patch_before = struct.unpack_from('<f', self.patch_baseline, offset)[0]
    if not math.isfinite(self.patch_before):
        raise RuntimeError('Nonfinite material baseline')
    self.patch_after = self.patch_before + 0.125
    return {'block': index, 'blockName': str(block.name), 'buffer': str(binding.resource),
            'bindingOffset': self.patch_base, 'fieldOffset': offset, 'field': name,
            'before': self.patch_before, 'byteSize': self.patch_size}


def perform(self, command):
    operation = command['operation']
    if operation == 'live_environment':
        old = os.environ.get('RENDERDOC_EID_TIMING', '0')
        os.environ['RENDERDOC_EID_TIMING'] = str(command.get('timing', '0'))
        self.finish({'previous': old, 'timing': os.environ['RENDERDOC_EID_TIMING']})
    elif operation == 'live_inventory':
        def inventory(ctrl):
            pipe = ctrl.GetPipelineState()
            refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
            def fields(variables):
                return [{'name': str(v.name), 'offset': int(v.byteOffset),
                         'type': str(v.type.baseType), 'columns': int(v.type.columns),
                         'members': fields(v.type.members)} for v in variables]
            nearby = []
            def visit(actions):
                for action in actions:
                    if action.flags & rd.ActionFlags.Drawcall and abs(int(action.eventId) - int(ctrl.GetCurrentEventID())) < 100:
                        nearby.append(int(action.eventId))
                    visit(action.children)
            visit(ctrl.GetRootActions())
            return {'event': int(ctrl.GetCurrentEventID()), 'nearbyDraws': nearby,
                    'bufferGeneration': int(ctrl.GetSharedBufferPatchGeneration()),
                    'blocks': [{'index': i, 'name': str(b.name), 'bytes': int(b.byteSize),
                                'fields': fields(b.variables)} for i, b in enumerate(refl.constantBlocks)],
                    'checkpoint': json.loads(str(ctrl.GetReplayCheckpointStatus()))}
        self.dispatch_replay(inventory, self.finish)
    elif operation == 'live_debug':
        def debug(ctrl):
            trace = None
            phases = {}
            inputs = rd.DebugPixelInputs()
            inputs.sample, inputs.primitive = 0, 0xffffffff
            try:
                start = time.perf_counter()
                trace = ctrl.DebugPixel(int(command['x']), int(command['y']), inputs)
                phases['enterMs'] = (time.perf_counter() - start) * 1000
                phases['debuggerAvailable'] = bool(trace and trace.debugger)
                if trace and trace.debugger:
                    start = time.perf_counter()
                    states = ctrl.ContinueDebug(trace.debugger)
                    phases['firstBatchMs'] = (time.perf_counter() - start) * 1000
                    phases['firstBatchStates'] = len(states)
            finally:
                if trace:
                    start = time.perf_counter()
                    ctrl.FreeTrace(trace)
                    phases['freeMs'] = (time.perf_counter() - start) * 1000
            start = time.perf_counter()
            ctrl.SetFrameEvent(int(ctrl.GetCurrentEventID()), True)
            phases['restoreMs'] = (time.perf_counter() - start) * 1000
            return phases
        self.dispatch_replay(debug, self.finish)
    elif operation == 'live_debug_ui':
        started = time.perf_counter()
        def begin(ctrl):
            pipe = ctrl.GetPipelineState()
            inputs = rd.DebugPixelInputs()
            inputs.sample, inputs.primitive = 0, 0xffffffff
            t = time.perf_counter()
            trace = ctrl.DebugPixel(int(command['x']), int(command['y']), inputs)
            if not trace or not trace.debugger:
                if trace:
                    ctrl.FreeTrace(trace)
                raise RuntimeError('The selected pixel did not create a debugger')
            return {'trace': trace, 'reflection': pipe.GetShaderReflection(rd.ShaderStage.Pixel),
                    'pipeline': pipe.GetGraphicsPipelineObject(),
                    'enterMs': (time.perf_counter() - t) * 1000}
        def show(value):
            trace = value['trace']
            if hasattr(trace, 'thisown'):
                trace.thisown = False  # DebugShader's native viewer takes trace ownership.
            t = time.perf_counter()
            viewer = self.ctx.DebugShader(value['reflection'], value['pipeline'], trace,
                                          '%s / %s,%s' % (self.ctx.CurEvent(), command['x'], command['y']))
            widget = viewer.Widget()
            self.ctx.AddDockWindow(widget, qrd.DockReference.AddTo, self.texture_widget)
            # SetCurrentStep is a no-op until native trace publication. Reaching step 1 below
            # proves a usable UI state, not merely a returned DebugPixel call.
            creation_ms = (time.perf_counter() - t) * 1000
            def poll():
                if time.perf_counter() - started > 180:
                    widget.close()
                    self.fail('Native Shader Viewer did not publish step 1')
                    return
                viewer.SetCurrentStep(1)
                if int(viewer.CurrentStep()) != 1:
                    QtCore.QTimer.singleShot(10, poll)
                    return
                result = {'enterMs': value['enterMs'], 'viewerCreationMs': creation_ms,
                          'completeUiMs': (time.perf_counter() - started) * 1000,
                          'completionEvidence': 'native SetCurrentStep(1) became usable after trace publication',
                          'visible': widget.isVisible(), 'step': int(viewer.CurrentStep())}
                widget.close()
                self.ctx.ShowTextureViewer()
                self.dispatch_replay(lambda ctrl: {'fatal': str(ctrl.GetFatalErrorStatus())},
                                     lambda state: self.finish(dict(result, **state)))
            QtCore.QTimer.singleShot(10, poll)
        self.dispatch_replay(begin, show)
    elif operation == 'live_pixel_probe':
        def pixel_probe(ctrl):
            target = ctrl.GetPipelineState().GetOutputTargets()[0].resource
            desc = next(t for t in ctrl.GetTextures() if t.resourceId == target)
            if str(desc.format.Name()) != 'R16G16B16A16_FLOAT':
                raise RuntimeError('This bounded probe expects an RGBA16F render target')
            raw = bytes(ctrl.GetTextureData(target, rd.Subresource()))
            candidates = []
            for y in range(0, int(desc.height), 4):
                for x in range(0, int(desc.width), 4):
                    pixel = struct.unpack_from('<4e', raw, (y * int(desc.width) + x) * 8)
                    brightness = sum(abs(v) for v in pixel[:3])
                    if math.isfinite(brightness) and .05 < brightness < 3:
                        candidates.append((brightness, x, y))
            candidates.sort(reverse=True)
            return {'target': str(target), 'candidates': candidates[:16]}
        self.dispatch_replay(pixel_probe, self.finish)
    elif operation == 'live_gc':
        import gc
        self.finish({'collected': gc.collect(), 'keepEntries': len(self.keep)})
    else:
        self._live_originals['perform'](self, command)


for name in ('perform', 'prepare_cbuffer', 'poll_navigation'):
    setattr(type(audit), name, globals()[name])
if hasattr(audit, 'timer'):
    audit.timer.start()
