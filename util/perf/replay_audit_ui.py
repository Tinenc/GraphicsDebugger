"""Private qrenderdoc audit controlled by files under its isolated run directory.

Run only with the audit launcher. Capture/project inputs are read-only. All mutations,
replacement shaders, buffer patches and checkpoint configuration belong to this process.
"""
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
import time
import traceback

from PySide2 import QtCore, QtGui, QtWidgets
import qrenderdoc as qrd
import renderdoc as rd


def atomic_json(path, value):
    temporary = path.with_suffix('.pending')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    deadline = time.monotonic() + 2
    while True:
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.002)


def thread_cpu_ms():
    values = [ctypes.c_uint64() for unused in range(4)]
    kernel = ctypes.windll.kernel32
    function = kernel.GetThreadTimes
    function.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint64)] * 4
    function.restype = ctypes.c_int
    if not function(ctypes.c_void_p(-2), *[ctypes.byref(value) for value in values]):
        return None
    return (values[2].value + values[3].value) / 10000.0


class ApplicationAudit(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.directory = Path(os.environ['BREAD_AUDIT_RUN'])
        self.runtime = Path(QtWidgets.QApplication.applicationDirPath())
        self.command = None
        self.last_command = ''
        self.busy = False
        self.loading_started = time.perf_counter()
        self.ready = False
        self.replacement_targets = []
        self.keep = []
        self.pending_navigation = None
        self.pending_inline = None
        self.original_event = 1291
        self.state = {'pid': os.getpid(), 'ready': False}
        self.generic_capture = os.environ.get('BREAD_AUDIT_GENERIC') == '1'
        self.generic_pending = False

    def save_state(self):
        atomic_json(self.directory / 'state.json', self.state)

    def start(self):
        self.timer = QtCore.QTimer()
        self.timer.setInterval(10)
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.save_state()

    def dispatch_replay(self, function, completed):
        def worker(ctrl):
            try:
                value, error = function(ctrl), None
            except Exception:
                value, error = None, traceback.format_exc()

            def deliver():
                try:
                    if error:
                        raise RuntimeError(error)
                    completed(value)
                except Exception:
                    self.fail(traceback.format_exc())
            self.ctx.Extensions().GetMiniQtHelper().InvokeOntoUIThread(deliver)
        self.ctx.Replay().AsyncInvoke(worker)

    def log_offset(self):
        path = self.runtime / 'eid_timing_driver.log'
        return path.stat().st_size if path.exists() else 0

    def driver_lines(self, offset):
        path = self.runtime / 'eid_timing_driver.log'
        try:
            with path.open('rb') as stream:
                stream.seek(offset)
                return stream.read().decode('utf-8', 'replace').splitlines()
        except OSError:
            return []

    def finish(self, value):
        value.update(commandId=self.command['id'], operation=self.command['operation'], ok=True)
        atomic_json(self.directory / ('result-' + self.command['id'] + '.json'), value)
        self.busy = False
        self.pending_navigation = None
        self.pending_inline = None
        self.state.update(lastCompleted=self.command['id'], lastError=None)
        self.save_state()

    def fail(self, error):
        if self.command:
            atomic_json(self.directory / ('result-' + self.command['id'] + '.json'),
                        {'commandId': self.command['id'], 'ok': False, 'error': error})
        self.state['lastError'] = error
        self.busy = False
        self.pending_navigation = None
        self.pending_inline = None
        self.save_state()

    def tick(self):
        try:
            if not self.ready:
                if not self.ctx.IsCaptureLoaded():
                    if time.perf_counter() - self.loading_started > 180:
                        raise RuntimeError('Capture did not load within 180 seconds')
                    return
                if self.generic_capture:
                    if not self.generic_pending:
                        self.generic_pending = True
                        self.dispatch_replay(self.generic_select, self.generic_open)
                    return
                self.ctx.SetEventID([], self.original_event, self.original_event, False)
                self.ctx.ShowTextureViewer()
                self.texture_viewer = self.ctx.GetTextureViewer()
                target = next(texture.resourceId for texture in self.ctx.GetTextures()
                              if int(texture.resourceId) == 662263)
                self.texture_viewer.ViewTexture(target, rd.CompType.Typeless, True)
                self.ctx.ShowPipelineViewer()
                # Keep PySide wrappers alive across C++ ownership conversions and dock moves.
                self.texture_widget = self.texture_viewer.Widget()
                self.pipeline_widget = self.ctx.GetPipelineViewer().Widget()
                self.keep.extend([self.texture_widget, self.pipeline_widget])
                self.render_widget = self.texture_widget.findChild(QtWidgets.QWidget, 'render')
                self.keep.append(self.render_widget)
                self.ready = True
                self.state.update(ready=True, capture=str(self.ctx.GetCaptureFilename()),
                                  loadAndInitialViewMs=(time.perf_counter() - self.loading_started) * 1000)
                self.save_state()
                return
            if self.pending_navigation:
                self.poll_navigation()
                return
            if self.pending_inline:
                self.poll_inline()
                return
            if self.busy:
                return
            path = self.directory / 'command.json'
            if not path.is_file():
                return
            try:
                command = json.loads(path.read_text(encoding='utf-8'))
            except PermissionError:
                # The launcher/virus scanner may hold the atomic replacement briefly.
                # This is not an application failure and must not overwrite a completed result.
                return
            if command['id'] == self.last_command:
                return
            self.command = command
            self.last_command = command['id']
            self.busy = True
            self.state['running'] = command['id']
            self.save_state()
            self.perform(command)
        except Exception:
            self.fail(traceback.format_exc())

    def perform(self, command):
        operation = command['operation']
        if operation == 'status':
            self.dispatch_replay(self.status, self.finish)
        elif operation == 'activate_texture':
            self.ctx.ShowTextureViewer()
            QtCore.QTimer.singleShot(250, lambda: self.finish({'textureVisible': self.texture_widget.isVisible()}))
        elif operation == 'ui_snapshot':
            rows = []
            for child in self.texture_widget.findChildren(QtWidgets.QWidget):
                if str(child.metaObject().className()) not in ('ResourcePreview', 'ThumbnailStrip'):
                    continue
                rect = child.geometry()
                rows.append({'class': str(child.metaObject().className()), 'name': child.objectName(),
                             'visible': child.isVisible(), 'hidden': child.isHidden(),
                             'geometry': [rect.x(), rect.y(), rect.width(), rect.height()],
                             'labels': [label.text() for label in child.findChildren(QtWidgets.QLabel)]})
            path = str(self.directory / (self.command['id'] + '.png'))
            self.texture_widget.grab().save(path)
            self.finish({'event': int(self.ctx.CurEvent()), 'previews': rows, 'image': path,
                         'selection': self.navigation_input_ack(int(self.ctx.CurSelectedEvent()))
                            if hasattr(self, 'event_tree') else None})
        elif operation == 'thumbnail_expose':
            if command.get('inputs'):
                found = False
                for tabs in self.texture_widget.findChildren(QtWidgets.QTabBar):
                    for i in range(tabs.count()):
                        if tabs.tabText(i).strip() in ('Inputs', '\u8f93\u5165'):
                            tabs.setCurrentIndex(i)
                            found = True
                if not found:
                    raise RuntimeError('Input preview tab was not found')
            self.expose_probe = {'steps': [], 'started': time.perf_counter()}
            strips = [w for w in self.texture_widget.findChildren(QtWidgets.QWidget)
                      if str(w.metaObject().className()) == 'ThumbnailStrip' and w.isVisible()]
            for strip in strips:
                for bar in strip.findChildren(QtWidgets.QScrollBar):
                    if bar.maximum() > bar.minimum():
                        self.expose_probe['steps'].append((bar, bar.maximum()))
                        self.expose_probe['steps'].append((bar, bar.minimum()))
            self.keep.append(strips)
            def step():
                if self.expose_probe['steps']:
                    bar, value = self.expose_probe['steps'].pop(0)
                    bar.setValue(value)
                    QtCore.QTimer.singleShot(180, step)
                    return
                overlaps = []
                for strip in strips:
                    previews = [w for w in strip.findChildren(QtWidgets.QWidget)
                                if str(w.metaObject().className()) == 'ResourcePreview' and w.isVisible()]
                    for i, a in enumerate(previews):
                        for b in previews[i+1:]:
                            if a.geometry().intersects(b.geometry()):
                                overlaps.append([a.objectName(), b.objectName()])
                if overlaps:
                    raise RuntimeError('Active thumbnail widget rectangles overlap: ' + repr(overlaps))
                self.thumbnail_witness()
            QtCore.QTimer.singleShot(180, step)
        elif operation == 'history_ui_probe':
            self.ctx.SetEventID([], int(command['event']), int(command['event']), True)
            self.history_probe = {'started': time.perf_counter(), 'samples': [], 'lastCount': -1}
            def poll_history():
                try:
                    widget = self.texture_widget.findChild(QtWidgets.QWidget, 'renderTargetHistoryView')
                    elapsed = (time.perf_counter()-self.history_probe['started'])*1000
                    if widget:
                        row = {key: widget.property(key) for key in (
                            'historyRevisionCount', 'historyCachedCount', 'historyLiveWidgets',
                            'historyRequestSerial', 'historyPendingEvent', 'historyCacheMutationGeneration',
                            'historyUnavailableCount', 'historyWorkerActive', 'historyScrollPending',
                            'historyRefreshScheduled', 'historyApplyingBatch', 'historyVisible', 'historyScrubbing')}
                        row['elapsedMs'] = elapsed
                        if row['historyCachedCount'] != self.history_probe['lastCount']:
                            self.history_probe['samples'].append(row)
                            self.history_probe['lastCount'] = row['historyCachedCount']
                        atomic_json(self.directory / 'history-progress.json', row)
                        if row['historyRevisionCount'] and row['historyCachedCount'] + int(row['historyUnavailableCount'] or 0) == row['historyRevisionCount']:
                            result = dict(self.history_probe)
                            result.pop('started')
                            result['completeMs'] = elapsed
                            result['uiSpans'] = widget.property('historyUISpans')
                            result['heartbeat'] = {k: widget.property(k) for k in (
                                'historyNativeHeartbeatSamples', 'historyNativeMaximumLoadingHeartbeatMs',
                                'historyImageBytes', 'historyImageBudgetBytes')}
                            self.finish(result)
                            return
                    if elapsed > float(command.get('deadlineMs', 120000)):
                        raise RuntimeError('History did not cache all revisions: ' + repr(self.history_probe['samples'][-3:]))
                    QtCore.QTimer.singleShot(50, poll_history)
                except Exception:
                    self.fail(traceback.format_exc())
            def open_history():
                try:
                    target = next(t.resourceId for t in self.ctx.GetTextures()
                                  if int(t.resourceId) == int(command['resource']))
                    target_name = str(self.ctx.GetResourceName(target))
                    previews = [w for w in self.texture_widget.findChildren(QtWidgets.QWidget)
                                if str(w.metaObject().className()) == 'ResourcePreview'
                                and any(target_name in label.text() for label in w.findChildren(QtWidgets.QLabel))]
                    if not previews:
                        raise RuntimeError('History target has no bound preview')
                    QtCore.QMetaObject.invokeMethod(previews[0], 'resourceLinkActivated', QtCore.Qt.DirectConnection)
                    QtCore.QTimer.singleShot(50, poll_history)
                except Exception:
                    self.fail(traceback.format_exc())
            QtCore.QTimer.singleShot(500, open_history)
        elif operation == 'root_snapshot':
            leaves = []
            def walk(actions):
                for action in actions:
                    if action.children:
                        walk(action.children)
                    else:
                        leaves.append(int(action.eventId))
            roots = self.ctx.CurRootActions()
            walk(roots)
            self.finish({'rootEvents': [int(a.eventId) for a in roots], 'firstLeaves': leaves[:35],
                         'lastLeaves': leaves[-10:], 'ordered': leaves == sorted(leaves),
                         'fakeMarkers': [{'selected': int(a.eventId), 'first': int(a.children[0].eventId),
                                          'last': int(a.children[-1].eventId)}
                                         for a in roots if a.children and a.IsFakeMarker()]})
        elif operation == 'key_probe':
            key = {'up': QtCore.Qt.Key_Up, 'down': QtCore.Qt.Key_Down}[command['key']]
            before = self.navigation_input_ack(int(self.ctx.CurSelectedEvent()))
            QtWidgets.QApplication.sendEvent(self.event_tree, QtGui.QKeyEvent(QtCore.QEvent.KeyPress, key, QtCore.Qt.NoModifier))
            after = self.navigation_input_ack(int(self.ctx.CurSelectedEvent()))
            QtWidgets.QApplication.sendEvent(self.event_tree, QtGui.QKeyEvent(QtCore.QEvent.KeyRelease, key, QtCore.Qt.NoModifier))
            QtCore.QTimer.singleShot(1200, lambda: self.finish({'before': before, 'immediate': after,
                'after': self.navigation_input_ack(int(self.ctx.CurSelectedEvent()))}))
        elif operation == 'api_inspector_probe':
            widget = self.ctx.GetAPIInspector().Widget()
            if command.get('hide'):
                bookmarks = self.ctx.GetMainWindow().Widget().findChild(QtWidgets.QWidget, 'bookmarksView')
                if bookmarks is None:
                    raise RuntimeError('Saved bookmarks tab was not restored')
                self.ctx.RaiseDockWindow(bookmarks)
            if command.get('show'):
                self.ctx.ShowAPIInspector()
            if command.get('refresh'):
                self.ctx.GetAPIInspector().Refresh()
            def api_snapshot():
                tree = widget.findChild(QtWidgets.QTreeView, 'apiEvents')
                selection = tree.selectionModel().selectedRows(0)
                index = selection[0] if selection else QtCore.QModelIndex()
                self.finish({'visible': widget.isVisible(), 'event': int(self.ctx.CurSelectedEvent()),
                    'displayedEvent': widget.property('apiDisplayedEvent'),
                    'populateCount': widget.property('apiPopulateCount'),
                    'rootRows': tree.model().rowCount(),
                    'selectedEID': str(index.sibling(index.row(), 0).data()) if index.isValid() else None})
            QtCore.QTimer.singleShot(100, api_snapshot)
        elif operation == 'timeline_probe':
            viewport = self.timeline_widget.findChild(QtWidgets.QWidget, 'qt_scrollarea_viewport')
            rect = viewport.rect()
            # The bottom band selects resource-use history, not frame actions. Click the EID
            # axis above markers so this test exercises ordinary timeline scrubbing.
            point = QtCore.QPointF(rect.width() * float(command['fraction']), 12)
            for kind, buttons in [(QtCore.QEvent.MouseButtonPress, QtCore.Qt.LeftButton),
                                  (QtCore.QEvent.MouseMove, QtCore.Qt.LeftButton),
                                  (QtCore.QEvent.MouseButtonRelease, QtCore.Qt.NoButton)]:
                QtWidgets.QApplication.sendEvent(viewport, QtGui.QMouseEvent(kind, point, QtCore.Qt.LeftButton,
                                                                           buttons, QtCore.Qt.NoModifier))
            QtCore.QTimer.singleShot(1200, lambda: self.finish({'fraction': command['fraction'],
                'after': self.navigation_input_ack(int(self.ctx.CurSelectedEvent()))}))
        elif operation == 'audit_configuration':
            allowed = ('RENDERDOC_D3D12_', 'RENDERDOC_UI_', 'RENDERDOC_CBUFFER_')
            for key, value in command.get('environment', {}).items():
                if not key.startswith(allowed):
                    raise RuntimeError('Unsupported owned-process diagnostic control')
                os.environ[key] = str(value)
            self.finish({'environment': {k: os.environ.get(k) for k in command.get('environment', {})}})
        elif operation == 'cancel_navigation':
            before=int(self.ctx.CurEvent())
            self.ctx.ScrubToEvent(int(command['target']))
            self.ctx.EndScrub()
            requested=self.navigation_input_ack(int(command['target']))
            self.ctx.SetEventID([],before,before,False)
            cancelled=self.navigation_input_ack(before)
            if not requested['matches'] or not cancelled['matches']:
                raise RuntimeError('Same-event navigation cancellation left an obsolete cursor')
            def verify_cancel(ctrl):
                if int(ctrl.GetCurrentEventID())!=before:
                    raise RuntimeError('Cancelled input changed the physical replay event')
                return {'controllerEvent':int(ctrl.GetCurrentEventID())}
            def finish_cancel(value):
                settled=self.navigation_input_ack(before)
                if not settled['matches'] or int(self.ctx.CurEvent())!=before:
                    raise RuntimeError('Cancelled input later moved the navigation controls')
                self.finish({'requested':requested,'cancelled':cancelled,'settled':settled,**value})
            QtCore.QTimer.singleShot(120,lambda:self.dispatch_replay(verify_cancel,finish_cancel))
        elif operation == 'marker_navigation':
            model=self.event_tree.model()
            maximum=int(command['maximumEvent'])
            def find(parent=QtCore.QModelIndex()):
                for row in range(model.rowCount(parent)):
                    index=model.index(row,0,parent)
                    selected=index.data(QtCore.Qt.UserRole)
                    effective=index.data(QtCore.Qt.UserRole+1)
                    if isinstance(selected,int) and isinstance(effective,int) and 0<selected<effective<=maximum:
                        return selected,effective
                    child=find(index)
                    if child:return child
                return None
            marker=find()
            if marker is None:
                raise RuntimeError('No real marker with distinct selected/effective EIDs')
            self.command.update(selectedEvents=[marker[0]],recordInputAck=True,requireInputAck=True)
            self.navigation_events=[marker[1]]
            self.navigation_samples=[]
            self.next_navigation()
        elif operation == 'pipeline_probe':
            def probe(ctrl):
                rows = []
                for event in command['events']:
                    ctrl.SetSelectedEventID(event)
                    ctrl.SetFrameEvent(event, True)
                    pipe = ctrl.GetPipelineState()
                    row = {'event': event, 'outputs': [str(v.resource) for v in pipe.GetOutputTargets()], 'stages': []}
                    for stage in (rd.ShaderStage.Vertex, rd.ShaderStage.Pixel):
                        reflection = pipe.GetShaderReflection(stage)
                        if not reflection:
                            continue
                        blocks = []
                        for index, block in enumerate(reflection.constantBlocks):
                            binding = pipe.GetConstantBlock(stage, index, 0).descriptor
                            def fields(members, base=0):
                                found = []
                                for member in members:
                                    offset = base + int(member.byteOffset)
                                    if member.type.members:
                                        found.extend(fields(member.type.members, offset))
                                    elif member.type.baseType == rd.VarType.Float:
                                        found.append({'name': str(member.name), 'offset': offset,
                                                      'rows': int(member.type.rows), 'columns': int(member.type.columns)})
                                return found
                            blocks.append({'index': index, 'name': str(block.name), 'size': int(block.byteSize),
                                           'buffer': str(binding.resource), 'offset': int(binding.byteOffset),
                                           'boundBytes': int(binding.byteSize), 'floats': fields(block.variables)[:20],
                                           'variables': [{'name': str(v.name), 'offset': int(v.byteOffset),
                                                          'type': str(v.type.baseType), 'rows': int(v.type.rows),
                                                          'columns': int(v.type.columns), 'members': len(v.type.members)}
                                                         for v in block.variables[:8]]})
                        row['stages'].append({'stage': str(stage), 'shader': str(pipe.GetShader(stage)),
                                              'entry': str(pipe.GetShaderEntryPoint(stage)),
                                              'rawSha256': hashlib.sha256(bytes(reflection.rawBytes)).hexdigest(),
                                              'blocks': blocks})
                    rows.append(row)
                return {'samples': rows}
            self.dispatch_replay(probe, self.finish)
        elif operation == 'capture_inventory':
            def inventory(ctrl):
                actions = []
                def visit(items):
                    for action in items:
                        if action.flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.Dispatch):
                            actions.append({'event': int(action.eventId), 'flags': str(action.flags),
                                            'name': str(action.customName),
                                            'indices': int(action.numIndices),
                                            'instances': int(action.numInstances)})
                        visit(action.children)
                visit(ctrl.GetRootActions())
                return {'api': str(ctrl.GetAPIProperties().pipelineType), 'actions': actions,
                        'current': int(ctrl.GetCurrentEventID()), 'fatal': str(ctrl.GetFatalErrorStatus())}
            self.dispatch_replay(inventory, self.finish)
        elif operation == 'frame_pass_inventory':
            def pass_inventory(ctrl):
                rows = []
                def visit(actions, parents):
                    for action in actions:
                        name = str(action.customName)
                        row = {'event': int(action.eventId), 'name': name, 'flags': str(action.flags),
                               'parents': parents, 'outputs': [int(r) for r in action.outputs],
                               'depth': int(action.depthOut)}
                        rows.append(row)
                        visit(action.children, parents + ([name] if name else []))
                visit(ctrl.GetRootActions(), [])
                return {'actions': rows, 'resources': [{'id': int(r.resourceId), 'name': str(r.name)}
                                                      for r in ctrl.GetResources()]}
            self.dispatch_replay(pass_inventory, self.finish)
        elif operation == 'resource_repeatability':
            def repeat_resources(ctrl):
                descriptions = {int(t.resourceId): t for t in ctrl.GetTextures()}
                buffers = {int(b.resourceId): b for b in ctrl.GetBuffers()}
                samples = []
                for event in command['events']:
                    for repeat in range(int(command.get('repeats', 2))):
                        ctrl.SetSelectedEventID(event)
                        ctrl.SetFrameEvent(event, True)
                        targets = command.get('resources')
                        if targets is None:
                            targets = sorted({int(t.resource) for t in ctrl.GetPipelineState().GetOutputTargets()
                                              if t.resource != rd.ResourceId.Null()})
                        resources = []
                        for rid in targets:
                            if rid in descriptions:
                                desc = descriptions[rid]
                                raw = bytes(ctrl.GetTextureData(desc.resourceId, rd.Subresource()))
                                info = {'resource': rid, 'width': int(desc.width), 'height': int(desc.height),
                                        'format': str(desc.format.Name())}
                            elif rid in buffers:
                                desc = buffers[rid]
                                raw = bytes(ctrl.GetBufferData(desc.resourceId, 0, desc.length))
                                info = {'resource': rid, 'kind': 'buffer'}
                            else:
                                raise RuntimeError('Resource is absent from current capture: ' + str(rid))
                            info.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                            if command.get('storePixels'):
                                filename = 'resource-%s-%s-%s-%s.bin' % (command['id'], event, repeat, rid)
                                (self.directory / filename).write_bytes(raw)
                                info['rawFile'] = filename
                            resources.append(info)
                        samples.append({'event': event, 'repeat': repeat, 'resources': resources})
                return {'samples': samples, 'fatal': str(ctrl.GetFatalErrorStatus())}
            self.dispatch_replay(repeat_resources, self.finish)
        elif operation == 'pixel_dependency_witness':
            def dependency_pixels(ctrl):
                textures = {int(t.resourceId):t for t in ctrl.GetTextures()}
                rows = []
                event = int(command['event'])
                for repeat in range(int(command.get('repeats',12))):
                    ctrl.SetSelectedEventID(event)
                    ctrl.SetFrameEvent(event,True)
                    resources = []
                    for rid in command['resources']:
                        desc = textures[rid]
                        if str(desc.format.Name()) not in ('R32G32_UINT','R10G10B10A2_UNORM'):
                            raise RuntimeError('Unexpected packed diagnostic texture format')
                        words = 2 if str(desc.format.Name()) == 'R32G32_UINT' else 1
                        raw = bytes(ctrl.GetTextureData(desc.resourceId,rd.Subresource()))
                        pixels = [{'xy':xy,'words':list(struct.unpack_from('<'+'I'*words,raw,
                                  (xy[1]*int(desc.width)+xy[0])*words*4))} for xy in command['pixels']]
                        resources.append({'resource':rid,'format':str(desc.format.Name()),
                                          'sha256':hashlib.sha256(raw).hexdigest(),'pixels':pixels})
                    rows.append({'repeat':repeat,'resources':resources})
                return {'event':event,'samples':rows,'scope':'Original GPU data at the same replayed event; no edits'}
            self.dispatch_replay(dependency_pixels,self.finish)
        elif operation == 'restore_resource_audit':
            def restore_audit(ctrl):
                resources={int(r.resourceId):r for r in ctrl.GetResources()}
                textures={int(t.resourceId):t for t in ctrl.GetTextures()}
                buffers={int(b.resourceId):b for b in ctrl.GetBuffers()}
                chunks=ctrl.GetStructuredFile().chunks
                rows=[]
                for rid in command['resources']:
                    r=resources[rid]
                    uses=[{'event':int(u.eventId),'usage':str(u.usage)} for u in ctrl.GetUsage(r.resourceId)]
                    creates=[{'index':int(i),'name':str(chunks[i].name)} for i in r.initialisationChunks]
                    row={'resource':rid,'name':str(r.name),'type':str(r.type),
                         'parents':[{'id':int(p),'name':str(resources[int(p)].name)} for p in r.parentResources],
                         'initialisation':creates,'firstUses':uses[:10],'usageCount':len(uses),
                         'firstClear':next((u['event'] for u in uses if 'Clear' in u['usage']),None)}
                    if rid in textures:
                        t=textures[rid];row.update(width=int(t.width),height=int(t.height),depth=int(t.depth),
                            mips=int(t.mips),slices=int(t.arraysize),format=str(t.format.Name()),logicalBytes=int(t.byteSize))
                    if rid in buffers:row['logicalBytes']=int(buffers[rid].length)
                    rows.append(row)
                return {'resources':rows,'scope':'Captured usage is a conservative upper bound, not a full-overwrite proof'}
            self.dispatch_replay(restore_audit,self.finish)
        elif operation == 'resource_usage':
            def resource_usage(ctrl):
                resources = {int(r.resourceId): r.resourceId for r in ctrl.GetResources()}
                return {'resources': [{'id': rid, 'usage': [
                    {'event': int(u.eventId), 'usage': str(u.usage)} for u in ctrl.GetUsage(resources[rid])]}
                    for rid in command['resources']]}
            self.dispatch_replay(resource_usage, self.finish)
        elif operation == 'query_cache_witness':
            def query_witness(ctrl):
                resource = next(r.resourceId for r in ctrl.GetResources() if str(r.name) == 'fixture-query-readback')
                resolves = []
                draws = []
                def visit(actions):
                    for a in actions:
                        if a.copyDestination == resource:
                            resolves.append(int(a.eventId))
                        if a.flags & rd.ActionFlags.Drawcall:
                            draws.append(int(a.eventId))
                        visit(a.children)
                visit(ctrl.GetRootActions())
                if not resolves:
                    raise RuntimeError('Owned timestamp resolve action missing')
                event = max(resolves)
                samples = []
                for i in range(2):
                    ctrl.SetFrameEvent(max(draws), False)
                    offset = self.log_offset()
                    ctrl.SetSelectedEventID(event)
                    ctrl.SetFrameEvent(event, False)
                    blob = bytes(ctrl.GetBufferData(resource, 0, 48))
                    ticks = struct.unpack('<6Q', blob)
                    if not all(ticks) or any(ticks[n] > ticks[n+1] for n in [0,2,4]):
                        raise RuntimeError('Native timestamp writes were not resolved in order')
                    samples.append({'ticks':list(ticks), 'driverLines':self.driver_lines(offset)})
                if samples[0]['ticks'] == samples[1]['ticks']:
                    raise RuntimeError('Timestamp results were cached instead of executing native queries')
                return {'event':event,'resource':str(resource),'freshQueryResults':True,'samples':samples}
            self.dispatch_replay(query_witness, self.finish)
        elif operation == 'shader_input_evidence':
            def shader_inputs(ctrl):
                event = int(command['event'])
                ctrl.SetSelectedEventID(event)
                ctrl.SetFrameEvent(event, True)
                pipe = ctrl.GetPipelineState()
                stage = rd.ShaderStage.Compute if command.get('compute') else rd.ShaderStage.Pixel
                reflection = pipe.GetShaderReflection(stage)
                if not reflection:
                    raise RuntimeError('No shader reflection at selected event/stage')
                obj = pipe.GetComputePipelineObject() if command.get('compute') else pipe.GetGraphicsPipelineObject()
                assembly = str(ctrl.DisassembleShader(obj, reflection, 'DXBC/DXIL'))
                name = 'original-shader-%s-%s.txt' % (event, str(stage).replace('.', '-'))
                (self.directory / name).write_text(assembly, encoding='utf-8')
                reads = [{'resource': int(d.descriptor.resource), 'offset': int(d.descriptor.byteOffset),
                          'size': int(d.descriptor.byteSize)} for d in pipe.GetReadOnlyResources(stage, True)]
                writes = [{'resource': int(d.descriptor.resource)} for d in pipe.GetReadWriteResources(stage, True)]
                return {'event':event, 'stage':str(stage), 'shader':str(pipe.GetShader(stage)),
                        'rawSha256':hashlib.sha256(bytes(reflection.rawBytes)).hexdigest(),
                        'assembly': name, 'reads':reads, 'writes':writes,
                        'sourceFiles':[str(f.filename) for f in reflection.debugInfo.files]}
            self.dispatch_replay(shader_inputs, self.finish)
        elif operation == 'repeat_shader_inputs':
            def repeat_inputs(ctrl):
                textures = {int(t.resourceId):t for t in ctrl.GetTextures()}
                buffers = {int(b.resourceId):b for b in ctrl.GetBuffers()}
                stage = rd.ShaderStage.Compute if command.get('compute') else rd.ShaderStage.Pixel
                event = int(command['event'])
                rows = []
                for repeat in range(int(command.get('repeats', 4))):
                    ctrl.SetSelectedEventID(event)
                    ctrl.SetFrameEvent(event, True)
                    pipe = ctrl.GetPipelineState()
                    reflection = pipe.GetShaderReflection(stage)
                    refs = sorted({int(d.descriptor.resource) for d in pipe.GetReadOnlyResources(stage, True)
                                   if d.descriptor.resource != rd.ResourceId.Null()})
                    inputs = []
                    for rid in refs:
                        info = {'resource':rid}
                        digest = hashlib.sha256()
                        size = 0
                        if rid in textures:
                            desc = textures[rid]
                            info.update(format=str(desc.format.Name()), mips=int(desc.mips),
                                        slices=int(desc.arraysize), width=int(desc.width), height=int(desc.height))
                            if int(desc.byteSize) > int(command.get('maxReadBytes',128*1024*1024)):
                                info['skipped'] = 'Texture exceeds diagnostic readback cap'
                            else:
                                for mip in range(int(desc.mips)):
                                    for layer in range(int(desc.arraysize)):
                                        sub = rd.Subresource()
                                        sub.mip, sub.slice, sub.sample = mip, layer, 0
                                        blob = bytes(ctrl.GetTextureData(desc.resourceId, sub))
                                        digest.update(blob)
                                        size += len(blob)
                        elif rid in buffers:
                            desc = buffers[rid]
                            if int(desc.length) > int(command.get('maxReadBytes',128*1024*1024)):
                                info['skipped'] = 'Buffer exceeds diagnostic readback cap'
                            else:
                                blob = bytes(ctrl.GetBufferData(desc.resourceId, 0, desc.length))
                                digest.update(blob)
                                size = len(blob)
                        else:
                            info['skipped'] = 'No current resource description'
                        if 'skipped' not in info:
                            info.update(bytes=size, sha256=digest.hexdigest())
                        inputs.append(info)
                    constants = []
                    for i, block in enumerate(reflection.constantBlocks):
                        binding = pipe.GetConstantBlock(stage, i, 0).descriptor
                        if binding.resource == rd.ResourceId.Null() or not block.byteSize:
                            continue
                        blob = bytes(ctrl.GetBufferData(binding.resource, binding.byteOffset, block.byteSize))
                        constants.append({'block':i,'resource':int(binding.resource), 'offset':int(binding.byteOffset),
                                          'bytes':len(blob), 'sha256':hashlib.sha256(blob).hexdigest()})
                    rows.append({'repeat':repeat, 'inputs':inputs, 'constants':constants,
                                 'output':self.fingerprint(ctrl)})
                return {'event':event,'samples':rows,'scope':'Readback diagnosis only; excluded from performance'}
            self.dispatch_replay(repeat_inputs, self.finish)
        elif operation == 'comparison_layout':
            texture = self.texture_widget
            pipeline = self.pipeline_widget
            self.ctx.AddDockWindow(pipeline, qrd.DockReference.RightOf, texture, 0.4)
            self.ctx.ShowTextureViewer()
            self.ctx.ShowPipelineViewer()
            self.ctx.GetMainWindow().Widget().resize(1600, 1000)
            if command.get('navigationControls'):
                self.ctx.ShowTimelineBar()
                self.ctx.ShowEventBrowser()
                self.timeline_widget = self.ctx.GetTimelineBar().Widget()
                self.event_widget = self.ctx.GetEventBrowser().Widget()
                self.event_tree = self.event_widget.findChild(QtWidgets.QTreeView,'events')
                if self.event_tree is None:
                    raise RuntimeError('Native event tree is missing')
                self.keep.extend([self.timeline_widget,self.event_widget,self.event_tree])
                self.ctx.AddDockWindow(self.timeline_widget,qrd.DockReference.TopOf,texture,0.15)
                self.ctx.AddDockWindow(self.event_widget,qrd.DockReference.LeftOf,texture,0.25)
            QtCore.QTimer.singleShot(200, lambda: self.finish({'layout': 'texture-and-pipeline-visible'}))
        elif operation == 'collect_garbage':
            import gc
            self.finish({'collectedObjects': gc.collect()})
        elif operation == 'python_heap_start':
            import gc
            import tracemalloc
            gc.collect()
            tracemalloc.start(6)
            self.python_heap_baseline = tracemalloc.take_snapshot()
            self.finish({'tracing': True})
        elif operation == 'python_heap_finish':
            import gc
            import tracemalloc
            gc.collect()
            differences = tracemalloc.take_snapshot().compare_to(self.python_heap_baseline, 'traceback')
            rows = [{'bytes': item.size_diff, 'count': item.count_diff,
                     'trace': [{'file': frame.filename, 'line': frame.lineno} for frame in item.traceback]}
                    for item in differences[:40]]
            retained = sum(item.size_diff for item in differences)
            tracemalloc.stop()
            self.python_heap_baseline = None
            self.finish({'retainedBytes': retained, 'differences': rows})
        elif operation == 'background_status':
            bridge = sys.modules.get('renderdoc_mcp_bridge')
            flow = getattr(bridge, '_frame_render_flow_ui', None)
            self.finish({'frameFlowRestored': bool(getattr(bridge, '_frame_render_flow_saved', None)),
                         'frameFlowPanelExists': flow is not None})
        elif operation == 'manifest_probe':
            bridge = sys.modules.get('renderdoc_mcp_bridge')
            if bridge is None:
                raise RuntimeError('Canonical bridge module is missing')
            if not hasattr(self, 'manifest_original'):
                self.manifest_original = bridge._publish_instance_manifest
                self.manifest_observations = []
                def observed_manifest(activity=False, stopped=False):
                    started = time.perf_counter()
                    bypass = self.manifest_probe_mode == 'disabled' and activity and not stopped
                    try:
                        if bypass:
                            bridge._instance_activity_unix_ms = int(time.time() * 1000)
                            return
                        return self.manifest_original(activity=activity, stopped=stopped)
                    finally:
                        self.manifest_observations.append({'activity': activity, 'stopped': stopped,
                            'bypass': bypass, 'wallMs': (time.perf_counter()-started)*1000})
                bridge._publish_instance_manifest = observed_manifest
            self.manifest_probe_mode = command.get('mode', 'observe')
            rows = self.manifest_observations
            self.manifest_observations = []
            self.finish({'observations': rows, 'mode': self.manifest_probe_mode})
        elif operation == 'panel_visibility':
            if command.get('pipeline', True):
                self.ctx.ShowPipelineViewer()
            else:
                self.pipeline_widget.hide()
            QtCore.QTimer.singleShot(150, lambda: self.finish({
                'pipelineVisible': self.pipeline_widget.isVisible(),
                'textureVisible': self.texture_widget.isVisible()}))
        elif operation == 'viewer_status':
            texture = self.texture_widget
            pipeline = self.pipeline_widget
            widgets = []
            for widget in texture.findChildren(QtWidgets.QWidget):
                name = widget.objectName()
                if name in ('render', 'pixelContext', 'outputThumbs', 'inputThumbs'):
                    widgets.append({'name': name, 'visible': widget.isVisible(),
                                    'size': [widget.width(), widget.height()]})
            self.finish({'textureVisible': texture.isVisible(),
                         'pipelineVisible': pipeline.isVisible(), 'widgets': widgets,
                         'meshExists': self.ctx.HasMeshPreview(),
                         'meshVisible': self.ctx.GetMeshPreview().Widget().isVisible()
                             if self.ctx.HasMeshPreview() else False})
        elif operation == 'mesh_visibility':
            if command['visible']:
                self.ctx.ShowMeshPreview()
            elif self.ctx.HasMeshPreview():
                self.ctx.GetMeshPreview().Widget().hide()
            QtCore.QTimer.singleShot(250, lambda: self.finish({
                'meshExists': self.ctx.HasMeshPreview(),
                'visible': self.ctx.GetMeshPreview().Widget().isVisible()
                    if self.ctx.HasMeshPreview() else False}))
        elif operation == 'gil_navigation_probe':
            # Deliberately queue a Python replay callback immediately before a blocking
            # native selection. It must enter Python while SetEventID waits for the FIFO.
            observed = []
            event = int(command['event'])
            atomic_json(self.directory / 'gil-probe-entered.json', {'event': event})
            self.ctx.Replay().AsyncInvoke(lambda ctrl: observed.append(int(ctrl.GetAPIProperties().pipelineType)))
            self.ctx.SetEventID([], event, event, False)
            self.finish({'callbackRan': len(observed) == 1, 'event': int(self.ctx.CurEvent())})
        elif operation == 'navigate':
            self.navigation_events = command['events'] * int(command.get('repeats', 1))
            self.navigation_samples = []
            self.next_navigation()
        elif operation == 'headless':
            self.dispatch_replay(lambda ctrl: self.headless(ctrl, command), self.finish)
        elif operation == 'mesh_postvs':
            def mesh_postvs(ctrl):
                data = ctrl.GetPostVSData(0, 0, rd.MeshDataStage.VSOut)
                if data.vertexResourceId == rd.ResourceId.Null() or not data.numIndices:
                    raise RuntimeError('Visible mesh view did not prepare post-VS data')
                blob = bytes(ctrl.GetBufferData(data.vertexResourceId, data.vertexByteOffset, 256))
                if not blob:
                    raise RuntimeError('Visible mesh post-VS bytes are empty')
                return {'vertexBuffer': str(data.vertexResourceId), 'numIndices': int(data.numIndices),
                        'vertexStride': int(data.vertexByteStride), 'bytes': len(blob),
                        'sha256': hashlib.sha256(blob).hexdigest()}
            self.dispatch_replay(mesh_postvs, self.finish)
        elif operation == 'fixed_write_boundary':
            def fixed_write_boundary(ctrl):
                resource = next(r.resourceId for r in ctrl.GetResources() if int(r.resourceId) == 921)
                rows = []
                for event in (1, 1296):
                    start = self.log_offset()
                    ctrl.SetSelectedEventID(event)
                    ctrl.SetFrameEvent(event, True)
                    blob = bytes(ctrl.GetBufferData(resource, 1702336, 320))
                    rows.append({'event': event, 'bytes': len(blob),
                                 'sha256': hashlib.sha256(blob).hexdigest(),
                                 'driverLines': self.driver_lines(start)})
                return {'samples': rows}
            self.dispatch_replay(fixed_write_boundary, self.finish)
        elif operation == 'configure':
            def configure(ctrl):
                ctrl.ConfigureReplayCheckpoints(bool(command.get('checkpoints', False)),
                                                int(command.get('budget', 128 * 1024 * 1024)),
                                                int(command.get('maxCheckpoints', 1)))
                return {'checkpoint': json.loads(str(ctrl.GetReplayCheckpointStatus()))}
            self.dispatch_replay(configure, self.finish)
        elif operation == 'replacements':
            self.dispatch_replay(self.apply_replacements, self.finish)
        elif operation == 'gpu_counters':
            self.dispatch_replay(self.gpu_counters, self.finish)
        elif operation == 'gpu_counter_routes':
            def counter_routes(ctrl):
                event = int(ctrl.GetCurrentEventID())
                before = self.fingerprint(ctrl)
                cases = []
                for counters in [[rd.GPUCounter.EventGPUDuration],
                                 [rd.GPUCounter.EventGPUDuration, rd.GPUCounter.InputVerticesRead,
                                  rd.GPUCounter.PSInvocations, rd.GPUCounter.SamplesPassed]]:
                    values = ctrl.FetchCounters(counters)
                    if not values:
                        raise RuntimeError('Native counter route failed: ' + str(ctrl.GetFatalErrorStatus()))
                    rows = [{'event':int(r.eventId), 'counter':str(r.counter), 'counterId':int(r.counter),
                             'value':float(r.value.d) if int(r.counter) == int(rd.GPUCounter.EventGPUDuration)
                                     else int(r.value.u64)} for r in values]
                    atomic_json(self.directory / ('counter-witness-' + str(len(cases)) + '.json'), rows)
                    for counter in counters:
                        if not any(v['counterId'] == int(counter) and v['value'] > 0 for v in rows):
                            raise RuntimeError('Requested native counter has no positive witness: ' + str(counter))
                    cases.append({'requested':[str(c) for c in counters], 'samples':rows})
                ctrl.SetFrameEvent(event, True)
                after = self.fingerprint(ctrl)
                if before['textures'] != after['textures']:
                    raise RuntimeError('Counter collection failed to preserve owned fixture pixels')
                return {'cases':cases, 'pixelsRestored':True, 'stablePowerRequested':False,
                        'fatal':str(ctrl.GetFatalErrorStatus())}
            self.dispatch_replay(counter_routes, self.finish)
        elif operation == 'fingerprint':
            self.ctx.SetEventID([], int(command.get('event', 1296)), int(command.get('event', 1296)),
                                bool(command.get('force', True)))
            self.dispatch_replay(self.fingerprint, self.finish)
        elif operation == 'cbuffer':
            event = int(command.get('event', 1296))
            self.ctx.SetEventID([], event, event, False)
            self.dispatch_replay(self.prepare_cbuffer, lambda value: self.edit_cbuffer(value))
        elif operation in ('hold_cbuffer', 'release_held_cbuffer'):
            event = int(command['event'])
            self.ctx.SetEventID([], event, event, False)
            started = time.perf_counter()
            def hold(ctrl):
                identity = self.prepare_cbuffer(ctrl) if operation == 'hold_cbuffer' else None
                if operation == 'hold_cbuffer':
                    error = ctrl.SetSharedBufferPatch(self.patch_resource, self.patch_base+self.patch_offset,
                                                      struct.pack(self.patch_format, self.patch_after))
                else:
                    error = ctrl.ClearSharedBufferPatch(self.patch_resource, self.patch_base+self.patch_offset, 4)
                if error:
                    raise RuntimeError(str(error))
                restored = None if operation == 'hold_cbuffer' else bytes(ctrl.GetBufferData(
                    self.patch_resource,self.patch_base,self.patch_size)) == self.patch_baseline
                return {'identity': identity, 'generation': int(ctrl.GetSharedBufferPatchGeneration()),
                        'restoredBytes': restored}
            def held(value):
                def published(accepted):
                    value.update(published=bool(accepted),totalMs=(time.perf_counter()-started)*1000)
                    self.finish(value)
                self.ctx.RefreshReplayStatus(event,value['generation'],published)
            self.dispatch_replay(hold,held)
        elif operation == 'inline_cbuffer':
            event = int(command.get('event', 1296))
            self.ctx.SetEventID([], event, event, False)
            self.dispatch_replay(self.prepare_cbuffer, self.open_inline)
        elif operation == 'cbuffer_fingerprint':
            self.ctx.SetEventID([], 1296, 1296, False)
            self.dispatch_replay(self.cbuffer_fingerprint, self.finish)
        elif operation == 'parameter_probe':
            self.ctx.SetEventID([], 1296, 1296, False)
            self.dispatch_replay(self.parameter_probe, self.finish)
        elif operation == 'shader_routes':
            self.ctx.SetEventID([], 1296, 1296, False)
            self.dispatch_replay(self.shader_routes, self.finish)
        elif operation == 'compile_debug_routes':
            self.dispatch_replay(self.compile_debug_routes, self.finish)
        elif operation == 'descriptor_view_scan':
            def scan(ctrl):
                rows = []
                started = time.perf_counter()
                for store in ctrl.GetDescriptorStores():
                    if int(store.descriptorCount) < 1000000:
                        continue
                    ranges = []
                    for page in range(1, 193):
                        item = rd.DescriptorRange()
                        item.offset = int(store.firstDescriptorOffset) + page * 256 * int(store.descriptorByteSize)
                        item.descriptorSize = int(store.descriptorByteSize)
                        item.count = 1
                        ranges.append(item)
                    expected = None
                    for repeat in range(int(command.get('repeats', 3))):
                        views = ctrl.GetDescriptors(store.resourceId, ranges)
                        values = [(str(v.type), str(v.resource), int(v.byteOffset), int(v.byteSize)) for v in views]
                        if len(values) != 192 or any(v[0] != str(rd.DescriptorType.Buffer) or v[3] != 768 for v in values):
                            raise RuntimeError('Large-heap fixture descriptor contents differ')
                        if expected is not None and values != expected:
                            raise RuntimeError('Page eviction changed decoded descriptor contents')
                        expected = values
                    rows.append({'store': int(store.resourceId), 'descriptorCount': int(store.descriptorCount),
                                 'views': expected})
                if len(rows) != 2:
                    raise RuntimeError('Expected the two owned million-slot descriptor heaps')
                return {'stores': rows, 'scanMs': (time.perf_counter()-started)*1000}
            self.dispatch_replay(scan, self.finish)
        elif operation == 'history_batch_probe':
            def history_batch(ctrl):
                event = int(ctrl.GetCurrentEventID())
                pipe = ctrl.GetPipelineState()
                resources = [d.resource for d in pipe.GetOutputTargets() if d.resource != rd.ResourceId.Null()]
                resource = resources[0] if not command.get('resource') else next(
                    t.resourceId for t in ctrl.GetTextures() if int(t.resourceId) == int(command['resource']))
                if resource == rd.ResourceId.Null():
                    raise RuntimeError('History target is unavailable')
                events = sorted(int(e) for e in command['events'])
                display = rd.TextureDisplay()
                display.resourceId = resource
                display.scale = -1.0
                output = ctrl.CreateOutput(rd.CreateHeadlessWindowingData(256, 144), rd.ReplayOutputType.Texture)
                if not output:
                    raise RuntimeError('No headless history output')
                try:
                    def reference():
                        start = time.perf_counter()
                        hashes = []
                        distinct = []
                        for eid in events:
                            output.DisplayTextureAtEvent(eid, display)
                            pixels = bytes(output.ReadbackOutputTexture())
                            hashes.append(hashlib.sha256(pixels).hexdigest())
                            distinct.append(len(set(pixels)))
                        return {'hashes': hashes, 'distinctByteValues': distinct,
                                'milliseconds': (time.perf_counter()-start)*1000}
                    a, b = reference(), reference()
                    offset = self.log_offset()
                    start = time.perf_counter()
                    def collect(ordered):
                        images, batches = [], []
                        while len(images) < len(ordered):
                            before = time.perf_counter()
                            batch = output.ReadbackTextureAtEvents(ordered[len(images):], display)
                            if not batch or len(batch) > len(ordered)-len(images):
                                raise RuntimeError('History batch made no valid prefix progress')
                            batches.append({'completed': len(batch), 'milliseconds': (time.perf_counter()-before)*1000})
                            images.extend(batch)
                        return images, batches
                    images, batches = collect(events)
                    milliseconds = (time.perf_counter()-start)*1000
                    hashes = [hashlib.sha256(bytes(blob)).hexdigest() for blob in images]
                    stable = a['hashes'] == b['hashes']
                    if command.get('requirePixels', True) and (not stable or hashes != b['hashes']):
                        raise RuntimeError('History batch pixels differ from the stable per-event reference')
                    reverse, reverse_batches = collect(list(reversed(events)))
                    if stable and [hashlib.sha256(bytes(x)).hexdigest() for x in reverse] != list(reversed(b['hashes'])):
                        raise RuntimeError('Descending history fallback pixels differ')
                    if int(ctrl.GetCurrentEventID()) != event:
                        raise RuntimeError('History batch changed the controller selection')
                    if output.ReadbackTextureAtEvents(events, display, lambda: True):
                        raise RuntimeError('Cancelled history batch returned images')
                    if int(ctrl.GetCurrentEventID()) != event:
                        raise RuntimeError('Cancelled history batch changed the selection')
                    return {'event': event, 'events': events, 'controlA': a,
                        'controlB': b, 'batchMs': milliseconds, 'batchHashes': hashes,
                        'controlStable': stable, 'pixelsMatch': hashes == b['hashes'],
                        'batches': batches, 'reverseBatches': reverse_batches,
                        'driverLines': self.driver_lines(offset)}
                finally:
                    output.Shutdown()
            self.dispatch_replay(history_batch, self.finish)
        elif operation == 'native_reuse_mutations':
            def mutate(ctrl):
                pipe = ctrl.GetPipelineState()
                event = int(ctrl.GetCurrentEventID())
                history_checks = []
                def verify_history(phase):
                    if not command.get('historyChecks'):
                        return
                    target = next(d.resource for d in ctrl.GetPipelineState().GetOutputTargets()
                                  if d.resource != rd.ResourceId.Null())
                    display = rd.TextureDisplay()
                    display.resourceId, display.scale = target, -1.0
                    output = ctrl.CreateOutput(rd.CreateHeadlessWindowingData(256, 144), rd.ReplayOutputType.Texture)
                    try:
                        output.DisplayTextureAtEvent(event, display)
                        expected = bytes(output.ReadbackOutputTexture())
                        actual = output.ReadbackTextureAtEvents([event], display)
                        if len(actual) != 1 or bytes(actual[0]) != expected or int(ctrl.GetCurrentEventID()) != event:
                            raise RuntimeError('Historical preview stale after ' + phase)
                        history_checks.append({'phase': phase, 'sha256': hashlib.sha256(expected).hexdigest()})
                    finally:
                        output.Shutdown()
                shader = pipe.GetShader(rd.ShaderStage.Pixel)
                block = pipe.GetShaderReflection(rd.ShaderStage.Pixel).constantBlocks[0]
                binding = pipe.GetConstantBlock(rd.ShaderStage.Pixel, 0, 0).descriptor
                if int(block.byteSize) != 16 or block.variables[0].type.baseType != rd.VarType.Float:
                    raise RuntimeError('Owned fixture CBuffer layout mismatch')
                before_bytes = bytes(ctrl.GetBufferData(binding.resource, binding.byteOffset, 16))
                before = self.fingerprint(ctrl)
                checkpoint_before = json.loads(str(ctrl.GetReplayCheckpointStatus()))
                error = ctrl.SetSharedBufferPatch(binding.resource, binding.byteOffset, struct.pack('<f', 0.5))
                if error:
                    raise RuntimeError(str(error))
                checkpoint_first = json.loads(str(ctrl.GetReplayCheckpointStatus()))
                if command.get('expectUpstreamCheckpoint'):
                    if (not checkpoint_before['events'] or not checkpoint_first['events'] or
                            checkpoint_first['hits'] <= checkpoint_before['hits']):
                        raise RuntimeError('First CBuffer edit did not reuse its proved upstream checkpoint')
                edited = self.fingerprint(ctrl)
                verify_history('cbuffer-first')
                error = ctrl.SetSharedBufferPatch(binding.resource, binding.byteOffset, struct.pack('<f', 0.25))
                if error:
                    raise RuntimeError(str(error))
                edited_again = self.fingerprint(ctrl)
                verify_history('cbuffer-next')
                if edited_again['textures'] == edited['textures']:
                    raise RuntimeError('A consecutive CBuffer value did not update the rendered pixels')
                error = ctrl.ResetSharedBufferRange(binding.resource, binding.byteOffset, 4)
                if error:
                    raise RuntimeError(str(error))
                restored = self.fingerprint(ctrl)
                verify_history('cbuffer-reset')
                if before['textures'] == edited['textures'] or before['textures'] != restored['textures']:
                    raise RuntimeError('Owned fixture CBuffer pixels did not change and restore')
                if bytes(ctrl.GetBufferData(binding.resource, binding.byteOffset, 16)) != before_bytes:
                    raise RuntimeError('Owned fixture CBuffer bytes were not restored')
                source = b'cbuffer Params:register(b0){float4 Tint;} float4 PSMain(float4 p:SV_Position):SV_Target0{return Tint.bgra;}'
                if 'variant' in command:
                    variant = int(command['variant'])
                    scale = (0.5 + (variant % 4096) / 8192.0 if command.get('uniqueShaderVariants')
                             else 0.5 + (variant % 17) / 64.0)
                    source = ('cbuffer Params:register(b0){float4 Tint;} float4 PSMain(float4 p:SV_Position):SV_Target0{return Tint.bgra * %.6f;}' % scale).encode('ascii')
                if command.get('invalidCompile'):
                    invalid, error = ctrl.BuildTargetShader('PSMain', rd.ShaderEncoding.HLSL,
                        b'invalid HLSL source', rd.ShaderCompileFlags(), rd.ShaderStage.Pixel)
                    if invalid != rd.ResourceId.Null() or not str(error):
                        if invalid != rd.ResourceId.Null():
                            ctrl.FreeTargetResource(invalid)
                        raise RuntimeError('Invalid fixture source did not fail compilation')
                target, error = ctrl.BuildTargetShader('PSMain', rd.ShaderEncoding.HLSL, source,
                                                       rd.ShaderCompileFlags(), rd.ShaderStage.Pixel)
                if target == rd.ResourceId.Null():
                    raise RuntimeError(str(error))
                trace = None
                try:
                    error = ctrl.ReplaceTargetShaderChecked(shader, target)
                    if error:
                        raise RuntimeError(str(error))
                    changed = self.fingerprint(ctrl)
                    verify_history('shader-replacement')
                    if changed['textures'] == before['textures']:
                        raise RuntimeError('Owned fixture HLSL replacement did not change pixels')
                    canonical_shader_check = None
                    if command.get('canonicalShaderReplay'):
                        # A second compatible replacement consumes the first replacement's
                        # complete write set. Read pixels before forcing a separate reference.
                        other, other_error = ctrl.BuildTargetShader('PSMain', rd.ShaderEncoding.HLSL,
                            b'cbuffer Params:register(b0){float4 Tint;} float4 PSMain(float4 p:SV_Position):SV_Target0{return Tint.rgba * 0.375;}',
                            rd.ShaderCompileFlags(), rd.ShaderStage.Pixel)
                        if other == rd.ResourceId.Null():
                            raise RuntimeError(str(other_error))
                        try:
                            other_error = ctrl.ReplaceTargetShaderChecked(shader, other)
                            if other_error:
                                raise RuntimeError(str(other_error))
                            actual = self.fingerprint(ctrl)
                            ctrl.SetFrameEvent(event, True)
                            canonical = self.fingerprint(ctrl)
                            if actual['textures'] != canonical['textures']:
                                raise RuntimeError('Retained Shader write proof differs from canonical replay')
                            canonical_shader_check = {'actual': actual, 'reference': canonical,
                                                      'exactPixelsMatch': True}
                        finally:
                            restore_error = ctrl.ReplaceTargetShaderChecked(shader, target)
                            ctrl.FreeTargetResource(other)
                            if restore_error:
                                raise RuntimeError(str(restore_error))
                        if self.fingerprint(ctrl)['textures'] != changed['textures']:
                            raise RuntimeError('Compatible Shader switch-back changed its output')
                    trace = ctrl.DebugPixel(106, 64, rd.DebugPixelInputs())
                    if not trace.debugger:
                        raise RuntimeError('Owned fixture pixel debugger unavailable')
                    states = 0
                    for i in range(32):
                        batch = ctrl.ContinueDebug(trace.debugger)
                        states += len(batch)
                        if not batch:
                            break
                    ctrl.FreeTrace(trace)
                    trace = None
                    verify_history('after-debug')
                    if self.fingerprint(ctrl)['textures'] != changed['textures']:
                        raise RuntimeError('Debugging changed the current replacement result')
                finally:
                    if trace:
                        ctrl.FreeTrace(trace)
                    ctrl.RemoveTargetShaderReplacementChecked(shader)
                    ctrl.FreeTargetResource(target)
                if self.fingerprint(ctrl)['textures'] != before['textures']:
                    raise RuntimeError('Shader rollback did not restore exact fixture pixels')
                verify_history('shader-rollback')
                if command.get('borrowedBuild'):
                    raw = bytes(ctrl.GetPipelineState().GetShaderReflection(rd.ShaderStage.Pixel).rawBytes)
                    borrowed = []
                    try:
                        for unused in range(2):
                            target, error = ctrl.BuildTargetShader('PSMain', rd.ShaderEncoding.DXBC, raw,
                                rd.ShaderCompileFlags(), rd.ShaderStage.Pixel)
                            if target == rd.ResourceId.Null():
                                raise RuntimeError(str(error))
                            borrowed.append(target)
                            if target != shader:
                                raise RuntimeError('Exact captured bytecode did not borrow the captured Shader')
                    finally:
                        for target in borrowed:
                            ctrl.FreeTargetResource(target)
                    if not ctrl.GetShaderEntryPoints(shader) or self.fingerprint(ctrl)['textures'] != before['textures']:
                        raise RuntimeError('Releasing borrowed builds damaged the captured Shader')
                return {'event': event, 'checkpointBefore': checkpoint_before,
                        'checkpointAfterFirstEdit': checkpoint_first,
                        'cbufferBytesRestored': True, 'cbufferPixelsChanged': True,
                        'shaderPixelsChanged': True, 'debugStates': states, 'debugRestored': True,
                        'before': before, 'edited': edited, 'shaderEdited': changed,
                        'historyChecks': history_checks, 'canonicalShaderCheck': canonical_shader_check}
            self.dispatch_replay(mutate, self.finish)
        elif operation == 'editor_save_routes':
            self.dispatch_replay(lambda ctrl: {'shader': ctrl.GetPipelineState().GetShader(rd.ShaderStage.Pixel),
                                              'before': self.fingerprint(ctrl)}, self.start_editor_routes)
        elif operation == 'thumbnail_witness':
            self.ctx.SetEventID([], int(command.get('event', 1296)), int(command.get('event', 1296)), False)
            QtCore.QTimer.singleShot(120, self.thumbnail_witness)
        elif operation == 'burst':
            self.burst_events = command['events'] * int(command.get('repeats', 1))
            self.burst_index = 0
            self.burst_started = time.perf_counter()
            self.burst_deadline = self.burst_started + 30
            self.burst_inputs = []
            self.burst_next()
        elif operation == 'select_followed_texture':
            from renderdoc_mcp_bridge.frame_stage_evidence import followed_binding
            wanted = int(command['texture'])
            follow = followed_binding(self.ctx, rd, qrd,
                {'eventId': int(self.ctx.CurEvent()), 'resourceId': wanted}, lambda rid: int(rid))
            self.texture_viewer.ViewFollowedResource(*follow)
            if int(self.texture_viewer.GetCurrentResource()) != wanted:
                raise RuntimeError('Could not restore the followed resource')
            self.ctx.Replay().AsyncInvoke(lambda ctrl: self.ctx.Extensions().GetMiniQtHelper().InvokeOntoUIThread(
                lambda: QtCore.QTimer.singleShot(200, lambda: self.finish({'texture': wanted, 'followed': True}))))
        elif operation == 'select_texture':
            target = next(texture.resourceId for texture in self.ctx.GetTextures()
                          if int(texture.resourceId) == int(command['texture']))
            self.texture_viewer.ViewTexture(target, rd.CompType.Typeless, True)
            # Test-only selection handoff: run after queued texture display work,
            # then allow the GUI publication callback to complete before release.
            self.ctx.Replay().AsyncInvoke(lambda ctrl: self.ctx.Extensions().GetMiniQtHelper().InvokeOntoUIThread(
                lambda: QtCore.QTimer.singleShot(200, lambda: self.finish({'texture': int(target)}))))
        elif operation == 'release_manual':
            if not self.ctx.IsCaptureLoaded():
                raise RuntimeError('Manual handoff requires a loaded capture')
            event = int(self.ctx.CurEvent())
            render = self.render_widget
            if (render.property('eidDisplayedEvent') != event or
                    render.property('eidPresentationPending') or
                    render.property('eidPresentationSerial') != render.property('eidRequestedSerial')):
                raise RuntimeError('Manual handoff still has a pending texture presentation')
            details = self.pipeline_widget.findChild(QtWidgets.QWidget, 'D3D12PipelineStateViewer')
            if details is not None and details.isVisible() and (
                    details.property('replayDisplayedEvent') != event or
                    details.property('replayDisplayedSerial') != details.property('replayRequestedSerial')):
                raise RuntimeError('Manual handoff still has pending pipeline data')
            self.finish({'pid':os.getpid(), 'capture':str(self.ctx.GetCaptureFilename()),
                         'event':event, 'textureVisible':self.texture_widget.isVisible(),
                         'pipelineVisible':self.pipeline_widget.isVisible(), 'auditPollingStopped':True})
            self.timer.stop()
        elif operation == 'close':
            if command.get('pendingReplay'):
                self.ctx.ScrubToEvent(1291)
                self.ctx.EndScrub()
            modifications = self.ctx.GetCaptureModifications()
            # Save test edits only to this disposable run directory. Never answer
            # a modal save prompt or overwrite an input capture during cleanup.
            saved = None
            if modifications != qrd.CaptureModifications.NoModifications:
                saved = str(self.directory / 'closing-results.rdc')
                if not self.ctx.SaveCaptureTo(saved):
                    raise RuntimeError('Could not save disposable close-test capture')
            self.finish({'closed': True, 'modificationsBeforeClose': str(modifications),
                         'disposableSave': saved})
            self.ctx.GetMainWindow().Widget().close()
        else:
            raise ValueError('Unsupported audit operation ' + operation)

    def generic_select(self, ctrl):
        draws = []
        def visit(actions):
            for action in actions:
                if action.flags & rd.ActionFlags.Drawcall:
                    draws.append(int(action.eventId))
                visit(action.children)
        visit(ctrl.GetRootActions())
        if not draws:
            raise RuntimeError('The API regression capture has no draw')
        event = draws[-1] if os.environ.get('BREAD_AUDIT_SELECT_LAST_DRAW') == '1' else draws[0]
        ctrl.SetSelectedEventID(event)
        ctrl.SetFrameEvent(event, True)
        targets = [target.resource for target in ctrl.GetPipelineState().GetOutputTargets()
                   if target.resource != rd.ResourceId.Null()]
        if not targets:
            raise RuntimeError('The API regression draw has no color output')
        return {'event': event, 'draws': draws, 'texture': int(targets[0]),
                'api': str(ctrl.GetAPIProperties().pipelineType)}

    def generic_open(self, identity):
        self.original_event = identity['event']
        self.ctx.SetEventID([], self.original_event, self.original_event, True)
        preserve_layout = os.environ.get('BREAD_AUDIT_PRESERVE_LAYOUT') == '1'
        if not preserve_layout:
            self.ctx.ShowTextureViewer()
        else:
            # Activate the existing texture tab without changing dock positions/sizes.
            self.ctx.ShowTextureViewer()
        self.texture_viewer = self.ctx.GetTextureViewer()
        target = next(texture.resourceId for texture in self.ctx.GetTextures()
                      if int(texture.resourceId) == identity['texture'])
        if not preserve_layout:
            self.texture_viewer.ViewTexture(target, rd.CompType.Typeless, True)
        if not preserve_layout:
            self.ctx.ShowPipelineViewer()
        self.texture_widget = self.texture_viewer.Widget()
        self.pipeline_widget = self.ctx.GetPipelineViewer().Widget()
        self.render_widget = self.texture_widget.findChild(QtWidgets.QWidget, 'render')
        self.keep.extend([self.texture_widget, self.pipeline_widget, self.render_widget])
        if not preserve_layout:
            self.ctx.AddDockWindow(self.pipeline_widget, qrd.DockReference.RightOf, self.texture_widget, 0.4)
            self.ctx.ShowTextureViewer()
            self.ctx.ShowPipelineViewer()
            self.ctx.GetMainWindow().Widget().resize(1600, 1000)
        else:
            self.timeline_widget = self.ctx.GetTimelineBar().Widget()
            self.event_widget = self.ctx.GetEventBrowser().Widget()
            self.event_tree = self.event_widget.findChild(QtWidgets.QTreeView, 'events')
            self.keep.extend([self.timeline_widget, self.event_widget, self.event_tree])
        self.state.update(capture=str(self.ctx.GetCaptureFilename()), generic=identity)
        def ready():
            self.ready = True
            self.state['ready'] = True
            self.save_state()
        QtCore.QTimer.singleShot(200, ready)

    def status(self, ctrl):
        actions = []
        def visit(nodes):
            for action in nodes:
                actions.append({'eventId': int(action.eventId), 'flags': str(action.flags),
                                'name': str(action.customName), 'numIndices': int(action.numIndices)})
                visit(action.children)
        visit(ctrl.GetRootActions())
        textures = ctrl.GetTextures()
        buffers = ctrl.GetBuffers()
        metadata_headers = []
        for chunk in ctrl.GetStructuredFile().chunks:
            if 'Capture' in str(chunk.name) or 'Frame' in str(chunk.name):
                count = chunk.FindChild('NumImages')
                if count is not None:
                    metadata_headers.append({'chunk': str(chunk.name), 'numImages': int(count.data.basic.u),
                                             'byteLength': int(chunk.metadata.length)})
        return {'event': int(ctrl.GetCurrentEventID()), 'imageStateHeaders': metadata_headers,
                'checkpoint': json.loads(str(ctrl.GetReplayCheckpointStatus())),
                'bufferGeneration': int(ctrl.GetSharedBufferPatchGeneration()),
                'textures': len(textures), 'textureLogicalBytes': sum(int(t.byteSize) for t in textures),
                'buffers': len(buffers), 'bufferLogicalBytes': sum(int(b.length) for b in buffers),
                'actions': actions, 'fatal': str(ctrl.GetFatalErrorStatus()),
                'replacementsApplied': len(self.replacement_targets)}

    def next_navigation(self):
        if len(self.navigation_samples) == len(self.navigation_events):
            self.finish({'samples': self.navigation_samples})
            return
        event = self.navigation_events[len(self.navigation_samples)]
        selected = self.command.get('selectedEvents',[event]*len(self.navigation_events))[len(self.navigation_samples)]
        ui_log = self.runtime / 'eid_timing.log'
        self.pending_navigation = {
            'event': event, 'driverOffset': self.log_offset(),
            'uiOffset': ui_log.stat().st_size if ui_log.exists() else 0,
            'started': time.perf_counter(), 'startedPythonClockMs': time.perf_counter() * 1000,
            'fromEvent': int(self.ctx.CurEvent()),'selectedTarget':selected}
        if self.command.get('mode') == 'set_event':
            self.ctx.SetEventID([], event, event, False)
            self.pending_navigation['setEventReturnedMs'] = (
                time.perf_counter() - self.pending_navigation['started']) * 1000
            self.ctx.Replay().AsyncInvoke(lambda ctrl: self.ctx.Extensions().GetMiniQtHelper().InvokeOntoUIThread(
                lambda: self.complete_navigation(None)))
        else:
            self.ctx.ScrubToSelectedEvent(selected,event)
            self.ctx.EndScrub()
            self.pending_navigation['inputHandlerMs'] = (
                time.perf_counter() - self.pending_navigation['started']) * 1000
            if self.command.get('recordInputAck'):
                ack = self.navigation_input_ack(event,selected)
                self.pending_navigation['inputAcknowledgement'] = ack
                if self.command.get('requireInputAck') and not ack['matches']:
                    raise RuntimeError('Navigation controls did not acknowledge the requested EID immediately')
            if self.command.get('resizeDuring'):
                window = self.ctx.GetMainWindow().Widget()
                for delay, width in [(0, 1616), (8, 1584), (16, 1600)]:
                    QtCore.QTimer.singleShot(delay, lambda width=width: window.resize(width, 1000))

    def navigation_input_ack(self,event,selected_request=None):
        if selected_request is None:selected_request=event
        current = self.event_tree.currentIndex()
        selected = current.data(QtCore.Qt.UserRole) if current.isValid() else None
        cursor = self.timeline_widget.property('navigationCursorEvent')
        return {'requested':event,'selectedRequest':selected_request,'treeSelected':selected,'timelineCursor':cursor,
                'publishedEvent':int(self.ctx.CurEvent()),
                'displayedEvent':self.render_widget.property('eidDisplayedEvent'),
                'matches':selected==selected_request and cursor==event}

    def poll_navigation(self):
        pending = self.pending_navigation
        if self.command.get('mode') == 'set_event':
            return
        if time.perf_counter() - pending['started'] > 30:
            raise RuntimeError('Navigation timed out at EID ' + str(pending['event']))
        if os.environ.get('RENDERDOC_EID_TIMING') != '1':
            render = self.render_widget
            if (int(self.ctx.CurEvent()) == pending['event'] and
                    render.property('eidDisplayedEvent') == pending['event'] and
                    not render.property('eidPresentationPending') and
                    render.property('eidPresentationSerial') == render.property('eidRequestedSerial') and
                    (not self.pipeline_widget.isVisible() or
                     not self.pipeline_widget.property('replayDetailsPending'))):
                pending['completionEvidence'] = 'Native EID presentation serial; 10 ms observer'
                self.complete_navigation(None)
            return
        path = self.runtime / 'eid_timing.log'
        if not path.exists():
            return
        try:
            with path.open('rb') as stream:
                stream.seek(pending['uiOffset'])
                lines = stream.read().decode('utf-8', 'replace').splitlines()
        except OSError:
            return
        record = next((line for line in lines if line.startswith('commit EID %d:' % pending['event'])), None)
        if record:
            self.complete_navigation(record)

    def complete_navigation(self, ui_record):
        pending = self.pending_navigation
        if pending is None:
            return
        sample = {key: value for key, value in pending.items() if key not in ('started', 'uiOffset', 'driverOffset')}
        sample.update(totalMs=(time.perf_counter() - pending['started']) * 1000,
                      uiRecord=ui_record, driverLines=self.driver_lines(pending['driverOffset']),
                      publishedEvent=int(self.ctx.CurEvent()), selectedEvent=int(self.ctx.CurSelectedEvent()))
        render = self.render_widget
        if render is not None:
            sample['presentation'] = {'event': render.property('eidDisplayedEvent'),
                                      'resource': render.property('eidDisplayedResource'),
                                      'serial': render.property('eidPresentationSerial'),
                                      'requestedSerial': render.property('eidRequestedSerial'),
                                      'pending': render.property('eidPresentationPending'),
                                      'visible': render.isVisible()}
            if ui_record is not None and render.isVisible():
                shown = sample['presentation']
                if shown['event'] != sample['event'] or shown['pending']:
                    raise RuntimeError('Native completion preceded the current visible presentation')
                if shown['requestedSerial'] is not None and shown['serial'] != shown['requestedSerial']:
                    raise RuntimeError('Native completion preceded the latest display request')
        if sample['publishedEvent'] != sample['event'] or sample['selectedEvent'] != sample['selectedTarget']:
            raise RuntimeError('Published event does not match requested event')
        details = self.pipeline_widget.findChild(QtWidgets.QWidget, 'D3D12PipelineStateViewer')
        if details is not None and details.isVisible() and details.property('replayDisplayedEvent') is not None:
            sample['pipelinePresentation'] = {
                'event': details.property('replayDisplayedEvent'),
                'serial': details.property('replayDisplayedSerial'),
                'requestedSerial': details.property('replayRequestedSerial')}
            if ui_record is not None and (sample['pipelinePresentation']['event'] != sample['event'] or
                    sample['pipelinePresentation']['serial'] != sample['pipelinePresentation']['requestedSerial']):
                raise RuntimeError('Native completion preceded current D3D12 pipeline details')
        self.navigation_samples.append(sample)
        self.pending_navigation = None
        QtCore.QTimer.singleShot(40, self.next_navigation)

    def headless(self, ctrl, command):
        samples = []
        for event in command['events'] * int(command.get('repeats', 1)):
            offset = self.log_offset()
            cpu = thread_cpu_ms()
            started = time.perf_counter()
            ctrl.SetFrameEvent(event, bool(command.get('force', True)))
            total = (time.perf_counter() - started) * 1000
            ended_cpu = thread_cpu_ms()
            samples.append({'event': event, 'totalMs': total,
                            'threadCpuMs': None if cpu is None else ended_cpu - cpu,
                            'driverLines': self.driver_lines(offset) if command.get('recordDriver', True) else []})
        return {'samples': samples, 'checkpoint': json.loads(str(ctrl.GetReplayCheckpointStatus()))}

    def apply_replacements(self, ctrl):
        inputs = json.loads(Path(os.environ['BREAD_AUDIT_REPLACEMENTS']).read_text(encoding='utf-8'))
        resources = {int(resource.resourceId): resource.resourceId for resource in ctrl.GetResources()}
        applied = []
        for record in inputs:
            original = resources[int(record['originalResourceId'])]
            stage = rd.ShaderStage.Pixel if record['stage'] == 'fragment' else rd.ShaderStage.Vertex
            entry = rd.ShaderEntryPoint(record['entryPoint'], stage)
            reflection = ctrl.GetShader(rd.ResourceId.Null(), original, entry)
            if hashlib.sha256(bytes(reflection.rawBytes)).hexdigest() != record['originalRawSha256']:
                raise RuntimeError('Captured shader identity differs for ' + str(original))
            blob = Path(record['candidatePath']).read_bytes()
            if hashlib.sha256(blob).hexdigest() != record['candidateRawSha256']:
                raise RuntimeError('Candidate shader bytes changed')
            started = time.perf_counter()
            target, error = ctrl.BuildTargetShader(record['entryPoint'], rd.ShaderEncoding.SPIRV,
                                                    blob, rd.ShaderCompileFlags(), stage)
            if target == rd.ResourceId.Null():
                raise RuntimeError('Target shader creation failed ' + str(error))
            ctrl.ReplaceResource(original, target)
            self.replacement_targets.append((original, target))
            applied.append({'original': str(original), 'target': str(target),
                            'candidateRawSha256': record['candidateRawSha256'],
                            'totalMs': (time.perf_counter() - started) * 1000})
        ctrl.SetFrameEvent(self.original_event, True)
        return {'applied': applied, 'count': len(applied)}

    def gpu_counters(self, ctrl):
        if rd.GPUCounter.EventGPUDuration not in ctrl.EnumerateCounters():
            raise RuntimeError('Native GPU duration counters unavailable')
        started = time.perf_counter()
        rows = ctrl.FetchCounters([rd.GPUCounter.EventGPUDuration])
        if not rows:
            raise RuntimeError('No native GPU counter samples: ' + str(ctrl.GetFatalErrorStatus()))
        samples = [{'event': int(row.eventId), 'durationMs': float(row.value.d) * 1000}
                   for row in rows]
        total = (time.perf_counter() - started) * 1000
        ctrl.SetFrameEvent(int(self.ctx.CurEvent()), True)
        return {'counterFetchWallMs': total, 'samples': samples,
                'summedActionGpuMs': sum(sample['durationMs'] for sample in samples),
                'timingScope': 'Native per-action GPU timestamps; action durations do not include CPU replay setup'}

    def fingerprint(self, ctrl):
        pipeline = ctrl.GetPipelineState()
        textures = []
        for target in pipeline.GetOutputTargets():
            if target.resource == rd.ResourceId.Null():
                continue
            subresource = rd.Subresource()
            subresource.mip, subresource.slice, subresource.sample = target.firstMip, target.firstSlice, 0
            raw = bytes(ctrl.GetTextureData(target.resource, subresource))
            if self.command.get('storePixels'):
                (self.directory / ('pixels-' + self.command['id'] + '-' + str(int(target.resource)) + '.bin')).write_bytes(raw)
            desc = next(t for t in ctrl.GetTextures() if t.resourceId == target.resource)
            textures.append({'resource': str(target.resource), 'bytes': len(raw),
                             'width': int(desc.width), 'height': int(desc.height), 'format': str(desc.format.Name()),
                             'sha256': hashlib.sha256(raw).hexdigest()})
        shaders = []
        for stage in (rd.ShaderStage.Vertex, rd.ShaderStage.Pixel):
            reflection = pipeline.GetShaderReflection(stage)
            shaders.append({'stage': str(stage), 'resource': str(pipeline.GetShader(stage)),
                            'rawSha256': hashlib.sha256(bytes(reflection.rawBytes)).hexdigest() if reflection else None})
        return {'event': int(ctrl.GetCurrentEventID()), 'textures': textures, 'shaders': shaders,
                'fatal': str(ctrl.GetFatalErrorStatus())}

    def cbuffer_fingerprint(self, ctrl):
        identity = self.prepare_cbuffer(ctrl)
        original = self.fingerprint(ctrl)
        error = ctrl.SetSharedBufferPatch(self.patch_resource, self.patch_base + self.patch_offset,
                                          struct.pack('<f', self.patch_before + 0.125))
        if error:
            raise RuntimeError(str(error))
        edited = self.fingerprint(ctrl)
        error = ctrl.ClearSharedBufferPatch(self.patch_resource, self.patch_base + self.patch_offset, 4)
        if error:
            raise RuntimeError(str(error))
        restored = self.fingerprint(ctrl)
        if original['textures'] == edited['textures']:
            raise RuntimeError('The selected CBuffer field did not change real output pixels')
        if original['textures'] != restored['textures']:
            raise RuntimeError('Clearing the CBuffer patch did not restore output pixels')
        return {'identity': identity, 'original': original, 'edited': edited, 'restored': restored,
                'imageChanged': True,
                'restoredBytes': bytes(ctrl.GetBufferData(self.patch_resource, self.patch_base, 320)) == self.patch_baseline}

    def parameter_probe(self, ctrl):
        identity = self.prepare_cbuffer(ctrl)
        block = ctrl.GetPipelineState().GetShaderReflection(rd.ShaderStage.Pixel).constantBlocks[identity['block']]
        baseline = self.fingerprint(ctrl)
        tested = []
        try:
            for member in block.variables:
                offset = int(member.byteOffset)
                if member.type.baseType != rd.VarType.Float or offset + 4 > len(self.patch_baseline):
                    continue
                before = struct.unpack_from('<f', self.patch_baseline, offset)[0]
                if not math.isfinite(before) or abs(before) > 10000:
                    continue
                error = ctrl.SetSharedBufferPatch(self.patch_resource, self.patch_base + offset,
                                                  struct.pack('<f', before + 0.25))
                if error:
                    raise RuntimeError(str(error))
                changed = self.fingerprint(ctrl)['textures'] != baseline['textures']
                tested.append({'offset': offset, 'field': str(member.name), 'before': before, 'changed': changed})
                error = ctrl.ClearSharedBufferPatch(self.patch_resource, self.patch_base + offset, 4)
                if error:
                    raise RuntimeError(str(error))
                if changed:
                    break
        finally:
            ctrl.ClearSharedBufferPatches()
        restored = self.fingerprint(ctrl)
        return {'identity': identity, 'tested': tested, 'restored': restored['textures'] == baseline['textures']}

    def shader_routes(self, ctrl):
        record = next(row for row in json.loads(Path(os.environ['BREAD_AUDIT_REPLACEMENTS']).read_text(encoding='utf-8'))
                      if int(row['originalResourceId']) == 65564)
        original = next(row.resourceId for row in ctrl.GetResources() if int(row.resourceId) == 65564)
        previous = ctrl.GetTargetShaderReplacement(original)
        if previous == rd.ResourceId.Null():
            raise RuntimeError('Expected the current project replacement')
        before = self.fingerprint(ctrl)
        samples, targets = [], []
        def measure(name, operation):
            offset = self.log_offset()
            started = time.perf_counter()
            value = operation()
            samples.append({'name': name, 'nativeMs': (time.perf_counter() - started) * 1000,
                            'driverLines': self.driver_lines(offset)})
            return value
        def replace(checked, target):
            if checked:
                error = ctrl.ReplaceTargetShaderChecked(original, target)
                if error:
                    raise RuntimeError(str(error))
            else:
                ctrl.ReplaceResource(original, target)
        try:
            for lane in ('baseline', 'candidate'):
                blob = Path(record[lane + 'Path']).read_bytes()
                expected = record['originalRawSha256' if lane == 'baseline' else 'candidateRawSha256']
                if hashlib.sha256(blob).hexdigest() != expected:
                    raise RuntimeError('Shader bytecode identity changed')
                target, error = measure('module-create-' + lane, lambda: ctrl.BuildTargetShader(
                    record['entryPoint'], rd.ShaderEncoding.SPIRV, blob, rd.ShaderCompileFlags(), rd.ShaderStage.Pixel))
                if target == rd.ResourceId.Null():
                    raise RuntimeError(str(error))
                targets.append(target)
            for checked in (True, False):
                lane = 'checked' if checked else 'legacy'
                measure(lane + '-replace', lambda: replace(checked, targets[0]))
                image = self.fingerprint(ctrl)
                measure(lane + '-restore-project', lambda: replace(checked, previous))
                restored = self.fingerprint(ctrl)
                if restored['textures'] != before['textures']:
                    raise RuntimeError('Shader replacement did not restore project output')
                measure(lane + '-same-target', lambda: replace(checked, previous))
            return {'samples': samples, 'projectRestored': True,
                    'replacementChangedPixels': image['textures'] != before['textures'],
                    'before': before, 'changed': image, 'restored': restored,
                    'note': 'SPIR-V module creation is measured separately; this is not external source compiler time.'}
        finally:
            if ctrl.GetTargetShaderReplacement(original) != previous:
                replace(True, previous)
            for target in targets:
                ctrl.FreeTargetResource(target)

    def compile_debug_routes(self, ctrl):
        event = int(ctrl.GetCurrentEventID())
        pipeline = ctrl.GetPipelineState()
        shader = pipeline.GetShader(rd.ShaderStage.Pixel)
        reflection = pipeline.GetShaderReflection(rd.ShaderStage.Pixel)
        outputs = list(reflection.outputSignature)
        if len(outputs) != 1 or int(outputs[0].regIndex) != 0 or int(outputs[0].compCount) != 4:
            raise RuntimeError('This shader fixture requires exactly one vec4 color output at location zero')
        previous = ctrl.GetTargetShaderReplacement(shader)
        original = self.fingerprint(ctrl)
        targets, measurements = [], []
        def measure(name, operation):
            offset, started = self.log_offset(), time.perf_counter()
            value = operation()
            measurements.append({'name': name, 'nativeMs': (time.perf_counter() - started) * 1000,
                                 'driverLines': self.driver_lines(offset)})
            return value
        def checked(target):
            error = ctrl.ReplaceTargetShaderChecked(shader, target)
            if error:
                raise RuntimeError(str(error))
        try:
            for name, color in [('red', '1.0,0.0,0.0,1.0'), ('green', '0.0,1.0,0.0,1.0')]:
                source = '#version 450\nlayout(location=0) out vec4 colour;\nvoid main(){colour=vec4(' + color + ');}\n'
                (self.directory / ('probe-' + name + '.glsl')).write_text(source, encoding='utf-8')
                target, error = measure('source-compile-' + name, lambda: ctrl.BuildTargetShader(
                    'main', rd.ShaderEncoding.GLSL, source.encode('utf-8'), rd.ShaderCompileFlags(), rd.ShaderStage.Pixel))
                if target == rd.ResourceId.Null():
                    raise RuntimeError(str(error))
                targets.append(target)
            measure('source-save-native-apply', lambda: ctrl.ReplaceResource(shader, targets[0]))
            red = self.fingerprint(ctrl)
            measure('visual-debug-checked-apply', lambda: checked(targets[1]))
            green = self.fingerprint(ctrl)
            if red['textures'] == green['textures']:
                raise RuntimeError('Compiled shader changes did not alter real output pixels')
            before_bad = int(ctrl.GetShaderReplacementGeneration(event))
            rejected, diagnostics = measure('compile-error', lambda: ctrl.BuildTargetShader(
                'main', rd.ShaderEncoding.GLSL, b'#version 450\ninvalid shader source',
                rd.ShaderCompileFlags(), rd.ShaderStage.Pixel))
            if rejected != rd.ResourceId.Null() or not diagnostics:
                raise RuntimeError('Invalid shader unexpectedly compiled')
            if before_bad != int(ctrl.GetShaderReplacementGeneration(event)):
                raise RuntimeError('A failed compile changed replay generation')
            texture_id = ctrl.GetPipelineState().GetOutputTargets()[0].resource
            texture = next(item for item in ctrl.GetTextures() if item.resourceId == texture_id)
            trace = measure('pixel-debug-enter', lambda: ctrl.DebugPixel(
                int(texture.width) // 2, int(texture.height) // 2, rd.DebugPixelInputs()))
            state_count, batches = 0, 0
            try:
                if not trace.debugger:
                    raise RuntimeError('The controlled shader did not create a pixel debugger')
                while batches < 100:
                    states = measure('pixel-debug-continue-' + str(batches), lambda: ctrl.ContinueDebug(trace.debugger))
                    batches += 1
                    state_count += len(states)
                    if not states:
                        break
                if state_count == 0:
                    raise RuntimeError('Pixel debugger produced no execution states')
            finally:
                measure('pixel-debug-free', lambda: ctrl.FreeTrace(trace))
            after_debug = self.fingerprint(ctrl)
            if after_debug['textures'] != green['textures']:
                raise RuntimeError('Pixel debugging did not restore the current shader result')
            return {'event': event, 'measurements': measurements, 'shaderPixelsChanged': True,
                    'debugStates': state_count, 'debugBatches': batches, 'debugRestored': True,
                    'compileErrorPreservedGeneration': True, 'original': original, 'red': red, 'green': green}
        finally:
            if previous == rd.ResourceId.Null():
                error = ctrl.RemoveTargetShaderReplacementChecked(shader)
                if error:
                    raise RuntimeError(str(error))
            else:
                checked(previous)
            for target in targets:
                ctrl.FreeTargetResource(target)

    def start_editor_routes(self, initial):
        self.editor_probe = {'initial': initial, 'results': [], 'lane': 0}
        self.next_editor_route()

    def next_editor_route(self):
        state = self.editor_probe
        if state['lane'] >= 3:
            for widget in state.get('widgets', []):
                widget.close()
            QtCore.QTimer.singleShot(100, self.finish_editor_routes)
            return
        valid = state['lane'] < 2
        native_fixture = os.environ.get('BREAD_AUDIT_NATIVE_FIXTURE') == '1'
        source = (('cbuffer Params:register(b0){float4 Tint;} float4 PSMain(float4 p:SV_Position):SV_Target0{return Tint.bgra;}'
                   if valid else 'invalid HLSL source') if native_fixture else
                  ('#version 450\nlayout(location=0) out vec4 colour;\nvoid main(){colour=vec4(1,0,0,1);}\n'
                   if valid else '#version 450\ninvalid shader source\n'))
        if state['lane'] == 1:
            widget = state['widgets'][0]
        else:
            viewer = self.ctx.EditShader(state['initial']['shader'], rd.ShaderStage.Pixel,
                                         'PSMain' if native_fixture else 'main',
                                         [('private-perf.hlsl' if native_fixture else 'private-perf.frag', source)], rd.KnownShaderTool.Unknown,
                                         rd.ShaderEncoding.HLSL if native_fixture else rd.ShaderEncoding.GLSL,
                                         rd.ShaderCompileFlags(), None, None)
            widget = viewer.Widget()
            self.keep.extend([viewer, widget])
            state.setdefault('widgets', []).append(widget)
        tool = widget.findChild(QtWidgets.QComboBox, 'compileTool')
        if tool is not None and tool.count():
            tool.setCurrentIndex(0 if native_fixture else tool.count() - 1)
        button = widget.findChild(QtWidgets.QToolButton, 'refresh')
        if button is None:
            raise RuntimeError('Native shader editor refresh control unavailable')
        self.keep.append(button)
        state['offset'] = self.log_offset()
        state['uiOffset'] = (self.runtime / 'eid_timing.log').stat().st_size if (self.runtime / 'eid_timing.log').exists() else 0
        state['started'] = time.perf_counter()
        button.click()
        state['inputHandlerMs'] = (time.perf_counter() - state['started']) * 1000
        QtCore.QTimer.singleShot(100, self.verify_editor_route)

    def verify_editor_route(self):
        state = self.editor_probe
        timing_lines = (self.runtime / 'eid_timing.log').read_bytes()[state['uiOffset']:].decode('utf-8', 'replace').splitlines()
        notices = [line for line in timing_lines if line.startswith('shader save:')]
        completions = [line for line in timing_lines if line.startswith('status refresh:') and 'published=1' in line]
        if not notices or (state['lane'] != 1 and not completions):
            if time.perf_counter() - state['started'] > 30:
                raise RuntimeError('Native shader display completion was not published')
            QtCore.QTimer.singleShot(10, self.verify_editor_route)
            return
        fields = {key: float(value) for key, value in re.findall(r'([\w]+)=([0-9.]+)', notices[-1])}
        complete_ms = fields['input_to_notice_ms']
        if state['lane'] != 1:
            finished = float(re.search(r'finish_clock_ms=([0-9.]+)', completions[-1])[1])
            complete_ms = finished - fields['input_clock_ms']
        def inspect(ctrl):
            target = ctrl.GetTargetShaderReplacement(state['initial']['shader'])
            return {'replacementActive': target != rd.ResourceId.Null(), 'image': self.fingerprint(ctrl),
                    'generation': int(ctrl.GetShaderReplacementGeneration(int(ctrl.GetCurrentEventID())))}
        def completed(value):
            valid = state['lane'] < 2
            if value['replacementActive'] != valid:
                raise RuntimeError('Native editor save/compile-error route has the wrong replacement state')
            changed = value['image']['textures'] != state['initial']['before']['textures']
            if changed != valid:
                raise RuntimeError('Native editor result did not match the expected real pixels')
            lines = self.driver_lines(state['offset'])
            if state['lane'] == 1:
                if value['generation'] != state['results'][0]['generation'] or any(
                        line.startswith('api=Vulkan ') and 'type=WithoutDraw' in line for line in lines):
                    raise RuntimeError('An unchanged native editor save replayed or invalidated the frame')
            state['results'].append({'validSource': valid, 'repeatUnchangedSource': state['lane'] == 1,
                                      'generation': value['generation'], 'inputHandlerMs': state['inputHandlerMs'],
                                      'diagnosticObservedMs': (time.perf_counter() - state['started']) * 1000,
                                      'driverLines': lines,
                                      'nativeSaveTimings': timing_lines,
                                      'nativeCompleteMs': complete_ms,
                                      'completionClockPrecisionMs': 1.0,
                                      'replacementActive': value['replacementActive'], 'pixelsChanged': changed})
            state['lane'] += 1
            self.next_editor_route()
        self.dispatch_replay(inspect, completed)

    def finish_editor_routes(self):
        if not self.ctx.SaveCaptureTo(str(self.directory / 'editor-results.rdc')):
            raise RuntimeError('Could not save disposable editor-test capture changes')
        self.finish({'samples': self.editor_probe['results'], 'nativeEditorFailureRestoresOriginal': True})

    def thumbnail_witness(self):
        try:
            # This explicit readback is a functional check, never a timing
            # sample. Enable only the native witness slot for its duration.
            previous_timing = os.environ.get('RENDERDOC_EID_TIMING')
            os.environ['RENDERDOC_EID_TIMING'] = '1'
            try:
                invoked = QtCore.QMetaObject.invokeMethod(self.texture_widget, 'CaptureThumbnailWitnesses',
                                                          QtCore.Qt.DirectConnection)
            finally:
                if previous_timing is None:
                    os.environ.pop('RENDERDOC_EID_TIMING', None)
                else:
                    os.environ['RENDERDOC_EID_TIMING'] = previous_timing
            if not invoked:
                raise RuntimeError('Native thumbnail witness slot unavailable')
            values = self.texture_widget.property('replayThumbnailWitnesses')
            collected = []
            for value in values:
                image = value['image']
                if image.isNull():
                    raise RuntimeError('Visible thumbnail has no native output image')
                width, height = image.width(), image.height()
                data = bytes(image.constBits())
                stride = image.bytesPerLine()
                rgb = b''.join(data[row * stride:row * stride + width * 3] for row in range(height))
                collected.append({key: value[key] for key in ('resource', 'event', 'mip', 'slice', 'cast')})
                collected[-1].update(width=width, height=height, nativeSha256=hashlib.sha256(rgb).hexdigest())
                collected[-1]['_nativeRgb'] = rgb
            self.keep.append(values)
            def compare(ctrl):
                resources = {str(texture.resourceId): texture.resourceId for texture in ctrl.GetTextures()}
                output = ctrl.CreateOutput(rd.CreateHeadlessWindowingData(32, 32), rd.ReplayOutputType.Texture)
                try:
                    for item in collected:
                        resource = resources.get(item['resource'])
                        if resource is None:
                            item['unbound'] = True
                            item.pop('_nativeRgb', None)
                            continue
                        sub = rd.Subresource()
                        sub.mip, sub.slice, sub.sample = int(item['mip']), int(item['slice']), 0
                        reference = bytes(output.DrawThumbnail(item['width'], item['height'], resource,
                                                               sub, rd.CompType(int(item['cast']))))
                        item['referenceSha256'] = hashlib.sha256(reference).hexdigest()
                        item['matches'] = item['referenceSha256'] == item['nativeSha256']
                        native = item.pop('_nativeRgb')
                        if len(native) == len(reference):
                            differences = [abs(a-b) for a, b in zip(native, reference)]
                            item['differentChannels'] = sum(value != 0 for value in differences)
                            item['maximumChannelDelta'] = max(differences, default=0)
                            item['differentPixels'] = sum(native[i:i+3] != reference[i:i+3]
                                                          for i in range(0, len(native), 3))
                finally:
                    output.Shutdown()
                return {'thumbnails': collected, 'allMatch': bool(collected) and all(
                    value.get('matches', value.get('unbound', False)) for value in collected)}
            self.dispatch_replay(compare, self.finish)
        except Exception:
            self.fail(traceback.format_exc())

    def burst_next(self):
        if self.burst_index < len(self.burst_events):
            event = self.burst_events[self.burst_index]
            started = time.perf_counter()
            self.ctx.ScrubToEvent(event)
            if self.command.get('releaseEach'):
                self.ctx.EndScrub()
            if self.command.get('resizeDuring') and self.burst_index % 3 == 0:
                self.ctx.GetMainWindow().Widget().resize(1600 + (16 if self.burst_index % 2 else 0), 1000)
            sample={'event': event, 'handlerMs': (time.perf_counter() - started) * 1000,
                    'detailsEnabled': self.pipeline_widget.isEnabled()}
            if self.command.get('requireInputAck'):
                sample['inputAcknowledgement']=self.navigation_input_ack(event)
                if not sample['inputAcknowledgement']['matches']:
                    self.fail('Burst input cursor moved behind the requested EID')
                    return
            self.burst_inputs.append(sample)
            self.burst_index += 1
            QtCore.QTimer.singleShot(5, self.burst_next)
            return
        if not hasattr(self, 'burst_released') or self.burst_released != self.command['id']:
            self.burst_released = self.command['id']
            self.ctx.EndScrub()
        target = self.burst_events[-1]
        if (int(self.ctx.CurEvent()) == target and self.render_widget.property('eidDisplayedEvent') == target
                and not self.render_widget.property('eidPresentationPending')
                and not self.pipeline_widget.property('replayDetailsPending')):
            self.finish({'inputs': self.burst_inputs, 'finalEvent': int(self.ctx.CurEvent()),
                         'displayedEvent': self.render_widget.property('eidDisplayedEvent'),
                         'detailsEnabled': self.pipeline_widget.isEnabled(),
                         'detailsPending': self.pipeline_widget.property('replayDetailsPending'),
                         'elapsedMs': (time.perf_counter() - self.burst_started) * 1000})
        elif time.perf_counter() > self.burst_deadline:
            self.fail('Burst did not settle on its final event and pipeline details')
        else:
            QtCore.QTimer.singleShot(10, self.burst_next)

    def prepare_cbuffer(self, ctrl):
        if os.environ.get('BREAD_AUDIT_SILVER') == '1' or os.environ.get('BREAD_AUDIT_NATIVE_FIXTURE') == '1':
            kind = rd.VarType.UInt if os.environ.get('BREAD_AUDIT_SILVER') == '1' else rd.VarType.Float
            pipe = ctrl.GetPipelineState()
            reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
            index, block = next((i, b) for i, b in enumerate(reflection.constantBlocks)
                                if int(b.byteSize) == 16 and b.variables and
                                b.variables[0].type.baseType == kind and
                                int(b.variables[0].type.columns) == 4)
            binding = pipe.GetConstantBlock(rd.ShaderStage.Pixel, index, 0).descriptor
            if binding.resource == rd.ResourceId.Null():
                raise RuntimeError('Silver probe requires a bound buffer, not root constants')
            self.patch_resource, self.patch_base = binding.resource, int(binding.byteOffset)
            self.patch_size, self.patch_format = 16, '<I' if kind == rd.VarType.UInt else '<f'
            self.patch_offset, self.patch_name = int(block.variables[0].byteOffset), str(block.variables[0].name)
            self.patch_baseline = bytes(ctrl.GetBufferData(binding.resource, self.patch_base, self.patch_size))
            self.patch_before = struct.unpack_from(self.patch_format, self.patch_baseline, self.patch_offset)[0]
            self.patch_after = self.patch_before ^ 0x00800000 if kind == rd.VarType.UInt else self.patch_before + 0.125
            return {'block': index, 'buffer': str(binding.resource), 'bindingOffset': self.patch_base,
                    'fieldOffset': self.patch_offset, 'field': self.patch_name, 'before': self.patch_before,
                    'type': str(kind), 'interpretation': 'Exact captured reflected lane; no reconstructed material semantics'}
        self.patch_size, self.patch_format = 320, '<f'
        if int(ctrl.GetSharedBufferPatchGeneration()) != 0 and not hasattr(self, 'patch_resource'):
            raise RuntimeError('Unexpected pre-existing buffer edit in isolated process')
        reflection = ctrl.GetPipelineState().GetShaderReflection(rd.ShaderStage.Pixel)
        candidates = [(index, block) for index, block in enumerate(reflection.constantBlocks)
                      if int(block.byteSize) == 320]
        if len(candidates) != 1:
            raise RuntimeError('Expected one material CBuffer with 320 reflected bytes')
        index, block = candidates[0]
        binding = ctrl.GetPipelineState().GetConstantBlock(rd.ShaderStage.Pixel, index, 0)
        self.patch_resource = binding.descriptor.resource
        self.patch_base = int(binding.descriptor.byteOffset)
        self.patch_baseline = bytes(ctrl.GetBufferData(self.patch_resource, self.patch_base, 320))
        leaves = []
        def visit(members, base=0):
            for member in members:
                offset = base + int(member.byteOffset)
                if len(member.type.members):
                    visit(member.type.members, offset)
                elif member.type.baseType == rd.VarType.Float and int(member.type.rows) == 1 and int(member.type.columns) == 1:
                    leaves.append((offset, str(member.name)))
        visit(block.variables)
        values = [(offset, name, struct.unpack_from('<f', self.patch_baseline, offset)[0])
                  for offset, name in leaves if offset + 4 <= 320]
        values = [value for value in values if math.isfinite(value[2]) and abs(value[2]) < 1000]
        if not values:
            raise RuntimeError('No bounded reflected float available')
        # The first reflected floats in this capture are unused by the current replacement.
        # The explicit parameter probe proved normalScale changes the actual bound RT pixels.
        values.sort(key=lambda value: (value[1] != 'normalScale', value[0]))
        self.patch_offset, self.patch_name, self.patch_before = values[0]
        self.patch_after = self.patch_before + 0.125
        return {'block': index, 'buffer': str(self.patch_resource), 'bindingOffset': self.patch_base,
                'fieldOffset': self.patch_offset, 'field': self.patch_name, 'before': self.patch_before}

    def edit_cbuffer(self, identity):
        self.cbuffer_identity = identity
        self.cbuffer_samples = []
        self.submit_cbuffer(False)

    def open_inline(self, identity):
        self.inline_identity = identity
        self.inline_samples = []
        self.inline_viewer = self.ctx.ViewConstantBuffer(rd.ShaderStage.Pixel, identity['block'], 0)
        self.inline_widget = self.inline_viewer.Widget()
        if 'debounceMs' in self.command:
            timer = self.inline_widget.findChild(QtCore.QTimer, 'cbufferLiveSubmitTimer')
            if timer is None:
                raise RuntimeError('Native CBuffer submit timer was not found')
            timer.setInterval(int(self.command['debounceMs']))
        self.keep.extend([self.inline_viewer, self.inline_widget])
        self.ctx.AddDockWindow(self.inline_widget, qrd.DockReference.TransientPopupArea,
                               self.ctx.GetPipelineViewer().Widget(), 0.3)
        self.pending_inline = {'phase': 'select', 'started': time.perf_counter()}

    def poll_inline(self):
        pending = self.pending_inline
        if time.perf_counter() - pending['started'] > 30:
            editor = self.inline_widget.findChild(QtWidgets.QWidget, 'cbufferLiveEditor')
            spin = self.inline_widget.findChild(QtWidgets.QDoubleSpinBox, 'cbufferLiveValue0')
            evidence = {'phase': pending['phase'], 'identity': self.inline_identity,
                        'patchBefore': self.patch_before, 'patchAfter': self.patch_after,
                        'spin': spin.value() if spin else None,
                        'properties': {name: editor.property(name) for name in (
                            'busy', 'pending', 'error', 'lastTiming', 'generation')} if editor else {}}
            atomic_json(self.directory / 'inline-timeout.json', evidence)
            raise RuntimeError('Inline CBuffer audit timed out at ' + pending['phase'] + ': ' + repr(evidence))
        tree = self.inline_widget.findChild(QtWidgets.QTreeView, 'fixedVars')
        editor = self.inline_widget.findChild(QtWidgets.QWidget, 'cbufferLiveEditor')
        if tree is None or editor is None:
            return
        if pending['phase'] == 'select':
            model = tree.model()
            matches = []
            def visit(parent):
                for row in range(model.rowCount(parent)):
                    index = model.index(row, 0, parent)
                    offset = str(model.data(model.index(row, 2, parent)))
                    kind = str(model.data(model.index(row, 3, parent))).strip().lower()
                    if offset == str(self.patch_offset) and (kind in ('float', 'float32') or
                            (os.environ.get('BREAD_AUDIT_NATIVE_FIXTURE') == '1' and kind.startswith('float')) or
                            (os.environ.get('BREAD_AUDIT_SILVER') == '1' and kind.startswith('uint'))):
                        matches.append(index)
                    visit(index)
            visit(QtCore.QModelIndex())
            if not matches:
                return
            tree.setCurrentIndex(matches[0])
            tree.clicked.emit(matches[0])
            spin = self.inline_widget.findChild(QtWidgets.QDoubleSpinBox, 'cbufferLiveValue0')
            if spin is None or not spin.isEnabled():
                return
            self.keep.extend([tree, editor, spin])
            self.inline_editor = editor
            self.inline_spin = spin
            self.inline_previous_timing = editor.property('lastTiming')
            self.inline_before = float(spin.value())
            if abs(self.inline_before - self.patch_before) > max(1e-5, abs(self.patch_before) * 1e-5):
                raise RuntimeError('CBuffer raw bytes after texture output disagree with the native editor: ' +
                                   repr({'raw': self.patch_before, 'editor': self.inline_before,
                                         'identity': self.inline_identity}))
            if int(self.command.get('settleMs', 0)):
                pending['phase'] = 'settling'
                pending['settleUntil'] = time.perf_counter() + int(self.command['settleMs']) / 1000.0
                return
            self.inline_triggered = time.perf_counter()
            spin.setValue(self.patch_after)
            pending['phase'] = 'edited'
            return
        if pending['phase'] == 'settling':
            if time.perf_counter() < pending['settleUntil']:
                return
            self.inline_previous_timing = self.inline_editor.property('lastTiming')
            self.inline_triggered = time.perf_counter()
            self.inline_spin.setValue(self.patch_after)
            pending['dragValues'] = int(self.command.get('dragValues', 1))
            pending['dragInputsSent'] = 1
            def drag(value):
                self.inline_spin.setValue(value)
                pending['dragInputsSent'] += 1
            for index in range(1, pending['dragValues']):
                delta = index if self.patch_format == '<I' else index / 64.0
                value = self.patch_after + delta
                QtCore.QTimer.singleShot(index * 8, lambda value=value: drag(value))
            pending['phase'] = 'edited'
            return
        timing = editor.property('lastTiming')
        if pending['phase'] == 'edited' and pending.get('dragInputsSent', 1) < pending.get('dragValues', 1):
            return
        if editor.property('busy') or editor.property('pending') or not timing or timing == self.inline_previous_timing:
            return
        if editor.property('error') or not timing.get('published'):
            raise RuntimeError('Inline CBuffer publication failed ' + str(editor.property('error')))
        sample = dict(timing)
        sample['operation'] = pending['phase']
        sample['observedTotalMs'] = (time.perf_counter() - self.inline_triggered) * 1000
        self.inline_samples.append(sample)
        self.inline_previous_timing = timing
        if pending['phase'] == 'edited':
            reset = self.inline_widget.findChild(QtWidgets.QPushButton, 'cbufferLiveReset')
            if reset is None:
                raise RuntimeError('Inline reset control unavailable')
            self.keep.append(reset)
            self.inline_triggered = time.perf_counter()
            reset.click()
            pending['phase'] = 'restored'
            return
        self.pending_inline = None
        def verify(ctrl):
            raw = bytes(ctrl.GetBufferData(self.patch_resource, self.patch_base, self.patch_size))
            return {'restoredBytes': raw == self.patch_baseline, 'identity': self.inline_identity,
                    'timerIntervalMs': self.inline_widget.findChild(QtCore.QTimer, 'cbufferLiveSubmitTimer').interval(),
                    'samples': self.inline_samples}
        def completed(value):
            self.inline_widget.close()
            self.finish(value)
        self.dispatch_replay(verify, completed)

    def submit_cbuffer(self, restore):
        started = time.perf_counter()
        offset = self.log_offset()
        def work(ctrl):
            queue_ms = (time.perf_counter() - started) * 1000
            cpu = thread_cpu_ms()
            begin = time.perf_counter()
            if restore:
                error = ctrl.ClearSharedBufferPatch(self.patch_resource,
                                                    self.patch_base + self.patch_offset, 4)
            else:
                error = ctrl.SetSharedBufferPatch(self.patch_resource,
                                                  self.patch_base + self.patch_offset,
                                                  struct.pack(self.patch_format, self.patch_after))
            native_ms = (time.perf_counter() - begin) * 1000
            ended_cpu = thread_cpu_ms()
            if error:
                raise RuntimeError(str(error))
            restored = None
            if restore:
                restored = bytes(ctrl.GetBufferData(self.patch_resource, self.patch_base, self.patch_size)) == self.patch_baseline
            return {'queueMs': queue_ms, 'nativeMs': native_ms,
                    'nativeThreadCpuMs': None if cpu is None else ended_cpu - cpu,
                    'generation': int(ctrl.GetSharedBufferPatchGeneration()), 'restoredBytes': restored}

        def edited(value):
            published_at = time.perf_counter()
            def published(accepted):
                value.update(operation='restore' if restore else 'edit', published=bool(accepted),
                             publicationMs=(time.perf_counter() - published_at) * 1000,
                             totalMs=(time.perf_counter() - started) * 1000,
                             driverLines=self.driver_lines(offset))
                self.cbuffer_samples.append(value)
                if restore:
                    self.finish({'identity': self.cbuffer_identity, 'samples': self.cbuffer_samples})
                else:
                    QtCore.QTimer.singleShot(50, lambda: self.submit_cbuffer(True))
            self.ctx.RefreshReplayStatus(int(self.command.get('event', 1296)), value['generation'], published)
        self.dispatch_replay(work, edited)


runner = ApplicationAudit(pyrenderdoc)
sys.modules['__main__']._bread_private_replay_audit = runner
if os.environ.get('BREAD_AUDIT_LIVE_EXTENSION') == '1':
    extension_path = Path(os.environ['BREAD_AUDIT_RUN']).parents[2] / 'util/perf/replay_audit_live_extension.py'
    exec(compile(extension_path.read_bytes(), str(extension_path), 'exec'))
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
