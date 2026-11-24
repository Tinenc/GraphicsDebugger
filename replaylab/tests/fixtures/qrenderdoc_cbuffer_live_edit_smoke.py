"""Native CBuffer inline-editor acceptance via --ui-python (Python 3.6).

Explicit REPLAYLAB_CBUFFER_EDIT_EVENT, _BIND, _OUTPUT are required. The capture
is loaded by the launcher. Waits for automatic project restore before selecting
the requested event. Changes and resets one scalar, one vector and one colour
through native Qt controls, reads the actual replay buffer, and records UI timer
responsiveness. Does not compile or write shader source. Leaves a normally
docked CBuffer viewer; the launcher owns shutdown on failure.
"""
import json
import hashlib
import os
import struct
import sys
import time
import traceback
from pathlib import Path

from PySide2 import QtCore, QtGui, QtWidgets
import qrenderdoc as qrd
import renderdoc as rd


class Probe(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.event = int(os.environ['REPLAYLAB_CBUFFER_EDIT_EVENT'])
        self.bind = int(os.environ['REPLAYLAB_CBUFFER_EDIT_BIND'])
        self.space = int(os.environ.get('REPLAYLAB_CBUFFER_EDIT_SPACE', '0'))
        self.output = Path(os.environ['REPLAYLAB_CBUFFER_EDIT_OUTPUT'])
        self.targets = json.loads(os.environ.get('REPLAYLAB_CBUFFER_EDIT_FIELDS',
            '["normalIntensity", "normalIntensity", "trailSize", "baseTint"]'))
        self.phase = 'loading'
        self.result = {'ok': False, 'pid': os.getpid(), 'checks': [], 'phase': self.phase}
        self.started = time.monotonic()
        self.busy = False
        self.viewer = None
        self.target = 0
        self.gaps = []
        self.last_tick = None
        self.ready_after = 0
        self.baseline = None
        self.done = False
        self.keep = []  # Retain wrappers returned by the bundled native/PySide bridge.
        self.timings = []
        self.last_timing = None
        self.output_deterministic = True

    def save(self):
        self.result['phase'] = self.phase
        self.output.write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding='utf-8')

    def start(self):
        self.timer = QtCore.QTimer()
        self.timer.setInterval(20)
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.save()

    def replay(self, fn, complete):
        self.busy = True
        def worker(ctrl):
            try:
                result, error = fn(ctrl), None
            except Exception:
                result, error = None, traceback.format_exc()
            def finish():
                self.busy = False
                if error:
                    self.fail(error)
                else:
                    try:
                        complete(result)
                    except Exception:
                        self.fail(traceback.format_exc())
            self.ctx.Extensions().GetMiniQtHelper().InvokeOntoUIThread(finish)
        self.ctx.Replay().AsyncInvoke(worker)

    def read(self, complete, force_replay=False):
        def work(ctrl):
            if force_replay:
                ctrl.SetFrameEvent(self.event, True)
            binding = ctrl.GetPipelineState().GetConstantBlock(rd.ShaderStage.Pixel, self.block, 0)
            raw = bytes(ctrl.GetBufferData(binding.descriptor.resource,
                                          binding.descriptor.byteOffset, self.byte_size))
            outputs = []
            if os.environ.get('REPLAYLAB_CBUFFER_EDIT_CHECK_OUTPUT') == '1':
                for target in ctrl.GetPipelineState().GetOutputTargets():
                    if target.resource == rd.ResourceId.Null():
                        continue
                    sub = rd.Subresource()
                    sub.mip, sub.slice, sub.sample = target.firstMip, target.firstSlice, 0
                    data = bytes(ctrl.GetTextureData(target.resource, sub))
                    outputs.append(hashlib.sha256(data).hexdigest())
            return raw, int(ctrl.GetSharedBufferPatchGeneration()), outputs
        self.replay(work, complete)

    def row(self, name):
        model = self.model
        for i in range(model.rowCount()):
            index = model.index(i, 0)
            if str(model.data(index)) == name:
                return index
        raise RuntimeError('No semantic field in native tree: ' + name)

    def idle(self):
        return (not self.editor.property('busy') and not self.editor.property('pending')
                and time.monotonic() >= self.ready_after)

    def tick(self):
        if self.done:
            return
        now = time.monotonic()
        if self.last_tick is not None and self.phase not in ('loading', 'tree'):
            self.gaps.append((now - self.last_tick) * 1000)
        self.last_tick = now
        try:
            if self.viewer is not None:
                timing = self.editor.property('lastTiming')
                if timing and timing != self.last_timing:
                    self.last_timing = timing
                    self.timings.append(dict(timing))
            if now - self.started > 420:
                raise RuntimeError('Timed out in ' + self.phase)
            if self.busy:
                return
            if self.phase == 'loading':
                if not self.ctx.IsCaptureLoaded():
                    return
                restore_path = os.environ.get('REPLAYLAB_CBUFFER_EDIT_RESTORE_RESULT')
                if restore_path:
                    restored = json.loads(Path(restore_path).read_text(encoding='utf-8'))
                    bridge = sys.modules.get('renderdoc_mcp_bridge')
                    session = getattr(bridge, '_capture_session_id', '')
                    if (not session or restored.get('captureSessionId') != session
                            or not restored.get('completed')
                            or restored.get('editing', {}).get('status') == 'pending'):
                        return
                else:
                    logs = Path(os.environ['LOCALAPPDATA']) / 'ReplayLab/console-events'
                    if not any('projectAutoRestoreFinished' in p.read_text(encoding='utf-8')
                               for p in logs.rglob('events.qrenderdoc.%d.*.jsonl' % os.getpid())):
                        return
                start_gate = os.environ.get('REPLAYLAB_CBUFFER_EDIT_START_GATE')
                if start_gate and not Path(start_gate).is_file():
                    return
                self.ctx.SetEventID([], self.event, self.event)
                reflection = self.ctx.CurPipelineState().GetShaderReflection(rd.ShaderStage.Pixel)
                self.block = next(i for i, b in enumerate(reflection.constantBlocks)
                                  if b.fixedBindNumber == self.bind and b.fixedBindSetOrSpace == self.space)
                self.byte_size = int(reflection.constantBlocks[self.block].byteSize)
                self.viewer = self.ctx.ViewConstantBuffer(rd.ShaderStage.Pixel, self.block, 0)
                self.widget = self.viewer.Widget()
                self.pipeline_viewer = self.ctx.GetPipelineViewer()
                self.pipeline_widget = self.pipeline_viewer.Widget()
                self.ctx.AddDockWindow(self.widget, qrd.DockReference.TransientPopupArea,
                                       self.pipeline_widget, 0.3)
                if os.environ.get('REPLAYLAB_CBUFFER_EDIT_ALL_BLOCKS') == '1':
                    self.other_viewers = []
                    for block_index in range(len(reflection.constantBlocks)):
                        if block_index == self.block:
                            continue
                        other = self.ctx.ViewConstantBuffer(rd.ShaderStage.Pixel, block_index, 0)
                        other_widget = other.Widget()
                        self.other_viewers.append((other, other_widget))
                        self.ctx.AddDockWindow(other_widget, qrd.DockReference.TransientPopupArea,
                                               self.pipeline_widget, 0.3)
                    self.ctx.RaiseDockWindow(self.widget)
                self.tree = self.widget.findChild(QtWidgets.QTreeView, 'fixedVars')
                self.model = self.tree.model()
                self.dock_parent = self.widget.parentWidget()
                self.editor = self.widget.findChild(QtWidgets.QWidget, 'cbufferLiveEditor')
                if self.editor is None:
                    raise RuntimeError('Installed build lacks inline CBuffer editor')
                self.phase = 'tree'
                self.save()
            elif self.phase == 'tree':
                if self.model.rowCount() < 10:
                    return
                self.result['docked'] = self.dock_parent is not None
                self.result['rows'] = self.model.rowCount()
                self.check_search()
                self.phase = 'select'
            elif self.phase == 'select':
                if self.target >= len(self.targets):
                    self.result.update(ok=True, maxUiTickMs=max(self.gaps or [0]),
                        elapsedSeconds=now-self.started, testedFields=self.targets,
                        editTimings=self.timings,
                        boundByteSize=self.widget.property('cbufferBindingBytes'),
                        readByteSize=self.widget.property('cbufferReadBytes'),
                        aliasCacheHit=self.widget.property('cbufferAliasCacheHit'),
                        legacyRefresh=os.environ.get('RENDERDOC_CBUFFER_EDIT_LEGACY_REFRESH') == '1')
                    if os.environ.get('REPLAYLAB_CBUFFER_EDIT_CHECK_OUTPUT') == '1':
                        self.result['outputDeterministic'] = self.output_deterministic
                        self.result['pixelResetVerified'] = self.output_deterministic and all(
                            check.get('outputResetMatches') for check in self.result['checks'])
                    preview = str(self.output.with_suffix('.png'))
                    self.result['preview'] = preview
                    self.window = self.widget.window()
                    self.window.grab().save(preview)
                    self.phase = 'complete'
                    self.done = True
                    self.timer.stop()
                    self.save()
                    return
                self.name = self.targets[self.target]
                index = self.row(self.name)
                self.tree.setCurrentIndex(index)
                self.tree.scrollTo(index)
                self.offsets = [int(x.strip()) for x in str(self.model.data(
                    self.model.index(index.row(), 2))).split(',')]
                self.spins = [self.widget.findChild(QtWidgets.QDoubleSpinBox, 'cbufferLiveValue%d' % i)
                              for i in range(len(self.offsets))]
                self.keep.extend(self.spins)
                if not all(s and s.isVisible() and s.isEnabled() for s in self.spins):
                    raise RuntimeError('Native row did not enable the typed editors for ' + self.name)
                self.read(self.begin_edit)
            elif self.phase == 'edited' and self.idle():
                if self.editor.property('error'):
                    raise RuntimeError(str(self.editor.property('error')))
                self.read(self.check_edit)
            elif self.phase == 'sliding':
                self.drag_index += 1
                self.slider.setValue(self.drag_index * 10)
                if self.drag_index == 20:
                    self.slider.sliderReleased.emit()
                    self.prepare_expected('slider')
            elif self.phase == 'reset' and self.idle():
                self.read(self.check_reset)
        except Exception:
            self.fail(traceback.format_exc())

    def begin_edit(self, result):
        self.baseline, self.generation, self.output_hashes = result
        if self.output_hashes:
            self.result['checks'].append({'field': self.name, 'baselineOutput': self.output_hashes})
            self.read(self.check_repeated_baseline, force_replay=True)
        else:
            self.edit_values()

    def check_search(self):
        search = self.widget.findChild(QtWidgets.QLineEdit, 'cbufferSearch')
        empty = self.widget.findChild(QtWidgets.QLabel, 'cbufferSearchEmpty')
        if search is None or not search.isVisible():
            raise RuntimeError('CBuffer search is unavailable')
        self.keep.extend([search, empty])
        shortcuts = self.widget.findChildren(QtWidgets.QShortcut)
        self.keep.extend(shortcuts)
        assert any(s.key().toString() == 'Ctrl+F5' and
                   s.context() == QtCore.Qt.WidgetWithChildrenShortcut for s in shortcuts)
        index = self.row(self.targets[0])
        name = str(self.model.data(index))
        offset = str(self.model.data(self.model.index(index.row(), 2))).split(',')[0]
        rows = self.model.rowCount()
        search.setText(name.upper() + ' ' + offset)
        assert not self.tree.isRowHidden(index.row(), index.parent())
        search.setText('bread-no-such-parameter-91fa37')
        assert empty.isVisible()
        assert all(self.tree.isRowHidden(i, QtCore.QModelIndex()) for i in range(rows))
        # Escape clears the filter and returns keyboard navigation to the tree.
        QtWidgets.QApplication.sendEvent(search, QtGui.QKeyEvent(
            QtCore.QEvent.KeyPress, QtCore.Qt.Key_Escape, QtCore.Qt.NoModifier))
        assert not search.text() and not empty.isVisible()
        assert all(not self.tree.isRowHidden(i, QtCore.QModelIndex()) for i in range(rows))
        assert self.model.rowCount() == rows
        self.result['search'] = {'semanticName': name, 'offset': offset,
                                 'caseInsensitive': True, 'escapeClears': True,
                                 'modelRowsPreserved': True}

    def check_repeated_baseline(self, result):
        raw, generation, outputs = result
        assert raw == self.baseline
        self.result['checks'].pop()
        self.initial_output_hashes = self.output_hashes
        self.output_hashes = outputs
        self.read(self.check_stable_baseline, force_replay=True)

    def check_stable_baseline(self, result):
        raw, generation, outputs = result
        assert raw == self.baseline
        self.baseline_stable = outputs == self.output_hashes
        self.output_deterministic = self.output_deterministic and self.baseline_stable
        self.repeated_output_hashes = outputs
        self.output_hashes = outputs
        self.edit_values()

    def edit_values(self):
        self.before = [s.value() for s in self.spins]
        color = self.widget.findChild(QtWidgets.QPushButton, 'cbufferLiveColor')
        self.keep.append(color)
        self.color_available = bool(color and color.isVisible())
        if self.target == 0 and len(self.spins) == 1:
            self.numeric_line = self.spins[0].findChild(QtWidgets.QLineEdit)
            self.numeric_line.setText('1e-20')
            self.spins[0].interpretText()
            if abs(self.spins[0].value() - 1e-20) > 1e-27:
                raise RuntimeError('Scientific-notation input was lost or rounded to zero')
            self.prepare_expected('scientific-numeric-input')
            return
        if len(self.spins) == 1:
            self.slider = self.widget.findChild(QtWidgets.QSlider, 'cbufferLiveSlider0')
            self.slider.sliderPressed.emit()
            self.drag_index = 0
            self.phase = 'sliding'
            return
        if self.color_available:
            color.click()
            dialog = self.widget.findChild(QtWidgets.QDialog, 'cbufferColorDialog')
            self.keep.append(dialog)
            if dialog is None:
                raise RuntimeError('Color action did not open the native picker')
            hue = dialog.findChild(QtWidgets.QSlider, 'cbufferColorHue')
            sv = dialog.findChild(QtWidgets.QWidget, 'cbufferColorSV')
            self.keep.extend([hue, sv])
            if hue is None or hue.orientation() != QtCore.Qt.Vertical or sv is None:
                raise RuntimeError('Picker does not expose the SV square and side hue strip')
            hsv = [dialog.findChild(QtWidgets.QDoubleSpinBox, 'cbufferColorHSV%d' % i)
                   for i in range(3)]
            self.keep.extend(hsv)
            hsv[0].setValue(210.0)
            hsv[1].setValue(66.6667)
            hsv[2].setValue(60.0)
            alpha = self.spins[3].value() if len(self.spins) == 4 else None
            dialog.reject()
            assert [s.value() for s in self.spins] == self.before
            # Reopen after WA_DeleteOnClose has been scheduled; exercise numeric editing and
            # keyboard square input before acceptance, retaining HDR and the separate alpha lane.
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
            color.click()
            dialog = self.widget.findChild(QtWidgets.QDialog, 'cbufferColorDialog')
            self.keep.append(dialog)
            hsv = [dialog.findChild(QtWidgets.QDoubleSpinBox, 'cbufferColorHSV%d' % i)
                   for i in range(3)]
            self.keep.extend(hsv)
            hsv[0].setValue(210.0)
            hsv[1].setValue(66.6667)
            hsv[2].setValue(60.0)
            sv = dialog.findChild(QtWidgets.QWidget, 'cbufferColorSV')
            self.keep.append(sv)
            QtWidgets.QApplication.sendEvent(sv, QtGui.QKeyEvent(
                QtCore.QEvent.KeyPress, QtCore.Qt.Key_Right, QtCore.Qt.NoModifier))
            assert abs(hsv[1].value() - 67.67) < 0.01
            QtWidgets.QApplication.sendEvent(sv, QtGui.QKeyEvent(
                QtCore.QEvent.KeyPress, QtCore.Qt.Key_Down, QtCore.Qt.ShiftModifier))
            assert abs(hsv[2].value() - 50.0) < 0.01
            hsv[1].setValue(66.6667)
            hsv[2].setValue(60.0)
            if alpha is not None:
                assert self.spins[3].value() == alpha
            intensity = max(1.0, *self.before[:3])
            assert abs(max(s.value() for s in self.spins[:3]) - 0.6 * intensity) < 0.0001 * intensity
            dialog.accept()
            self.prepare_expected('sv-color-picker-and-cancel')
        else:
            # One GUI burst must coalesce a contiguous vector into one update.
            for i, spin in enumerate(self.spins):
                spin.setValue(self.before[i] + (i + 1) * 0.125)
            self.prepare_expected('numeric-vector')

    def prepare_expected(self, interaction):
        self.wanted = [s.value() for s in self.spins]
        self.expected = bytearray(self.baseline)
        for offset, value in zip(self.offsets, self.wanted):
            self.expected[offset:offset+4] = struct.pack('<f', value)
        self.result['checks'].append({'field': self.name, 'components': len(self.spins),
            'before': self.before, 'wanted': self.wanted,
            'colorAvailable': self.color_available, 'interaction': interaction})
        if self.output_hashes:
            self.result['checks'][-1].update(initialOutput=self.initial_output_hashes,
                repeatedOutput=self.repeated_output_hashes, baselineStable=self.baseline_stable)
        self.phase = 'edited'
        self.ready_after = time.monotonic() + 0.2
        self.save()

    def check_edit(self, result):
        raw, generation, outputs = result
        if raw != bytes(self.expected):
            raise RuntimeError('Native buffer differs from requested field-only update: ' + self.name)
        if generation <= self.generation:
            raise RuntimeError('Real replay mutation was not observed')
        self.result['checks'][-1].update(bytesMatch=True, generation=generation)
        if self.output_hashes:
            self.result['checks'][-1].update(outputBefore=self.output_hashes, outputAfter=outputs,
                                            outputChanged=outputs != self.output_hashes)
        reset = self.widget.findChild(QtWidgets.QPushButton, 'cbufferLiveReset')
        self.keep.append(reset)
        if reset is None or not reset.isEnabled():
            raise RuntimeError('Reset unavailable after a committed edit')
        reset.click()
        self.phase = 'reset'
        self.ready_after = time.monotonic() + 0.2
        self.save()

    def check_reset(self, result):
        raw, generation, outputs = result
        if raw != self.baseline:
            raise RuntimeError('Reset changed bytes outside the selected field or lost original values')
        self.result['checks'][-1].update(resetMatches=True, resetGeneration=generation)
        if self.output_hashes:
            self.result['checks'][-1].update(outputReset=outputs,
                outputResetMatches=outputs == self.output_hashes)
            if self.output_deterministic and outputs != self.output_hashes:
                raise RuntimeError('Rendered output did not return to its stable starting pixels')
        self.target += 1
        self.baseline = None
        self.phase = 'select'
        self.save()

    def fail(self, error):
        self.result['error'] = error
        self.result['needsRollback'] = self.baseline is not None
        # Preserve diagnostics; do not broadly clear other editors' patches.
        self.done = True
        self.timer.stop()
        self.save()


probe = Probe(pyrenderdoc)
sys.modules['__main__']._bread_inline_cb_probe = probe
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(probe.start)
