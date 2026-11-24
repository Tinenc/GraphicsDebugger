"""Real current-capture UI save fixture, controlled by bounded receipt commands.

Only the canonical, previously accepted source named by the explicit environment
is eligible. Edits go through the native editor's Ctrl+S and the normal Host CAS
client. There is no filesystem source writer, simulated Host, or gate writer.
The parent verifies compile/apply publicly, requests restoration, then closes GUI.
"""
from __future__ import print_function
from difflib import SequenceMatcher
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from PySide2 import QtCore, QtGui, QtWidgets

EXPECTED_SHA = '593cbe25a817b680426e34f3bb1f5551710b0824d26be6157a7fcfdb6433aab9'
ORIGINAL_SHADER_SHA = '3c4afb434934308919e884efa26523f832807f2adc839d3a3754ceb559255205'
OUT = os.environ['REPLAYLAB_SHADER_REAL_SAVE_OUTPUT']
COMMAND = os.environ['REPLAYLAB_SHADER_REAL_SAVE_COMMAND']
CANCEL = os.environ.get('REPLAYLAB_SHADER_REAL_SAVE_CANCEL', OUT + '.cancel')
TARGET = os.environ['REPLAYLAB_SHADER_REAL_SAVE_TARGET']
WORKSPACE_HINT = os.environ.get('REPLAYLAB_SHADER_REAL_SAVE_WORKSPACE_ID', '')
EXPECTED_EVENT_ID = int(os.environ.get('SHADER_REVIEW_EVENT_ID', '11329'))
assert EXPECTED_EVENT_ID in (11329, 61523), 'Expected an explicitly evidenced test event'

class RestoreResultObserver(object):
    """Fixture-only transparent receipt of the normal public restore call.

    The original call runs outside the observer lock. Recording never invokes a
    client or Qt object and cannot change the original result or exception.
    """
    def __init__(self, client_type, receipt_path):
        self.client_type = client_type
        self.receipt_path = receipt_path
        self.original = client_type.restore
        self._lock = threading.Lock()
        self._installed = False
        self._observed = 0
        self._completed = 0
        self._recording_error = None
        observer, original = self, self.original

        def observed_restore(client, *args, **kwargs):
            with observer._lock:
                observer._observed += 1
                sequence = observer._observed
            try:
                result = original(client, *args, **kwargs)
            except BaseException as error:
                observer._record(sequence, args, kwargs, 'raised', error=error)
                raise
            observer._record(sequence, args, kwargs, 'returned', result=result)
            return result

        self.wrapper = observed_restore

    @staticmethod
    def write_json(path, value):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode='w', encoding='utf-8', dir=os.path.dirname(path),
                    prefix='.' + os.path.basename(path) + '.', suffix='.tmp',
                    delete=False) as stream:
                temporary = stream.name
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _record(self, sequence, args, kwargs, outcome, result=None, error=None):
        # A completed call remains observable if cleanup restored the class while
        # it was in flight. This closure holds plain Python data, never a timer.
        with self._lock:
            self._completed += 1
            try:
                request = {}
                for index, name in enumerate(('project_root', 'capture_path', 'capture_session_id')):
                    value = args[index] if len(args) > index else kwargs.get(name)
                    request[name] = value if isinstance(value, str) else None
                receipt = {'schema': 'replaylab-fixture-auto-restore/v1',
                           'pid': os.getpid(), 'observedCall': sequence,
                           'observedCalls': self._observed, 'completedCalls': self._completed,
                           'completedAt': time.time(), 'request': request,
                           'outcome': outcome}
                if error is None:
                    receipt['result'] = result
                else:
                    receipt['exception'] = {'type': type(error).__name__, 'message': str(error)}
                    for field in ('winerror', 'error_type', 'method', 'path'):
                        value = getattr(error, field, None)
                        if isinstance(value, int) or (isinstance(value, str) and len(value) <= 2048):
                            receipt['exception'][field] = value
                self.write_json(self.receipt_path, receipt)
            except BaseException as recording_error:
                # Diagnostics are explicitly outside the production contract,
                # including when both the original call and recorder fail.
                self._recording_error = type(recording_error).__name__

    def install(self):
        with self._lock:
            if not self._installed:
                if self.client_type.restore is not self.original:
                    raise RuntimeError('Restore method changed before fixture observer installation')
                self.client_type.restore = self.wrapper
                self._installed = True

    def uninstall(self):
        with self._lock:
            if self.client_type.restore is self.wrapper:
                self.client_type.restore = self.original
            self._installed = False

    def snapshot(self):
        with self._lock:
            return {'installed': self._installed and self.client_type.restore is self.wrapper,
                    'observedCalls': self._observed, 'completedCalls': self._completed,
                    'recordingError': self._recording_error}

def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def same_path(left, right):
    return bool(left and right) and os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))

def native_edit_hunks(current, target):
    """Plan small native edits before sending any key, without replacing a buffer.

    A new error directive and a distant alpha change are separate hunks. Editing
    bottom to top keeps each original offset valid while earlier text is intact.
    """
    old_lines, new_lines = current.splitlines(True), target.splitlines(True)
    offsets = [0]
    for line in old_lines:
        offsets.append(offsets[-1] + len(line))
    hunks = []
    for tag, first, last, new_first, new_last in SequenceMatcher(
            None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        start, end = offsets[first], offsets[last]
        old, new = current[start:end], ''.join(new_lines[new_first:new_last])
        prefix = 0
        while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
            prefix += 1
        suffix = 0
        while (suffix < min(len(old), len(new)) - prefix and
               old[len(old) - suffix - 1] == new[len(new) - suffix - 1]):
            suffix += 1
        start += prefix
        end -= suffix
        insertion = new[prefix:len(new) - suffix if suffix else len(new)]
        if start != end or insertion:
            hunks.append((start, end, insertion))
    hunks.reverse()
    # Count all mutation characters before any native key. Include the extra
    # Escape before every Return, final Escape and Ctrl+S in the key bound.
    change_count = sum(end - start + len(text) for start, end, text in hunks)
    key_count = change_count + sum(text.count('\n') for start, end, text in hunks) + 2
    if change_count > 2048 or key_count > 2048:
        raise RuntimeError('Native edit exceeds the bounded 2048-character/key limit')
    simulated = current
    for start, end, insertion in hunks:
        simulated = simulated[:start] + insertion + simulated[end:]
    if simulated != target:
        raise RuntimeError('Native edit hunks do not produce the exact bounded target')
    return hunks, change_count, key_count

def key(widget, code, modifiers=QtCore.Qt.NoModifier, text=''):
    for kind in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease):
        QtWidgets.QApplication.sendEvent(widget, QtGui.QKeyEvent(kind, code, modifiers, text))

class Probe(object):
    def __init__(self):
        self.result = {'schema': 'replaylab-native-real-save/v1', 'pid': os.getpid(),
                       'hostBoundarySimulated': False, 'sourceWriter': 'native-CtrlS-Host-CAS',
                       'ready': False, 'commands': [], 'ok': False,
                       'cleanup': False, 'cleanupRequested': False, 'timerDestroyed': False,
                       'cancelPath': CANCEL, 'targetPath': TARGET,
                       'expectedWorkspaceId': WORKSPACE_HINT}
        self.original = None
        self.target = None
        self.seen = 0
        self.timer = None
        self._cleanup_timer = None
        self.started = time.monotonic()
        self.timeout_seconds = 900.0
        self.restore_observer = None
        self._index_receipts = set()
        self._previous_tick = None
        self.result['uiHeartbeat'] = {
            'intervalSeconds': 0.5, 'samples': 0, 'maxGapSeconds': 0.0,
            'gapsOverTwoSeconds': [],
            'scope': 'Existing UI fixture timer; includes fixture and application work',
        }

    def install_restore_observer(self):
        bridge = sys.modules.get('renderdoc_mcp_bridge')
        protocol = getattr(bridge, '_project_restore_client_protocol', None)
        if protocol is None:
            raise RuntimeError('Canonical public restore client is not loaded before the fixture')
        self.restore_observer = RestoreResultObserver(
            protocol.ReplayLabRestoreHostClient,
            os.path.join(os.path.dirname(os.path.abspath(OUT)), 'auto-restore.json'))
        self.restore_observer.install()
        self.result['autoRestoreObservation'] = {
            'path': self.restore_observer.receipt_path,
            'installedAt': time.time(),
            'restoreClientAlreadyActive': getattr(bridge, '_project_auto_restore_client', None) is not None}

    def write(self):
        if self.restore_observer is not None:
            self.result['autoRestoreObserver'] = self.restore_observer.snapshot()
        with open(OUT, 'w', encoding='utf-8') as stream:
            json.dump(self.result, stream, ensure_ascii=False, indent=2)

    def start(self):
        try:
            self.timeout_seconds = float(os.environ.get('REPLAYLAB_SHADER_REAL_SAVE_TIMEOUT_SECONDS', '900'))
            if not 1.0 <= self.timeout_seconds <= 1800.0:
                raise ValueError('Fixture timeout must be between 1 and 1800 seconds')
            # Python owns this timer. An application child would be destroyed only
            # after qrenderdoc's Py_Finalize, when a Shiboken callback is unsafe.
            self.timer = QtCore.QTimer()
            self.timer.setInterval(500)
            self.timer.timeout.connect(self.tick)
            self.timer.destroyed.connect(self._timer_destroyed)
            self.result['timeoutSeconds'] = self.timeout_seconds
            self.write()
            self._previous_tick = time.monotonic()
            self.timer.start()
        except Exception:
            self.result['error'] = traceback.format_exc()
            self.cleanup('exception')

    def _timer_destroyed(self, *unused):
        self._cleanup_timer = None
        self.result.update(cleanup=True, timerDestroyed=True,
                           finishedAt=time.time(), elapsedSeconds=time.monotonic() - self.started)
        self.write()
        if getattr(sys, '_replaylab_real_save_probe', None) is self:
            del sys._replaylab_real_save_probe

    def cleanup(self, reason):
        if self.result['cleanupRequested']:
            return
        self.result.update(cleanupRequested=True, cleanupReason=reason)
        if self.restore_observer is not None:
            self.restore_observer.uninstall()
        timer, self.timer = self.timer, None
        if timer is None:
            self._timer_destroyed()
            return
        timer.stop()
        timer.timeout.disconnect(self.tick)
        self._cleanup_timer = timer
        self.write()
        # Do not delete the receiver inside its own timeout event. qrenderdoc's
        # main pump sends DeferredDelete after this callback returns. Only the
        # destroyed signal acknowledges cleanup to the external launcher.
        timer.deleteLater()

    def tick(self):
        try:
            now = time.monotonic()
            if self._previous_tick is not None:
                gap = now - self._previous_tick
                heartbeat = self.result['uiHeartbeat']
                heartbeat['samples'] += 1
                heartbeat['maxGapSeconds'] = max(heartbeat['maxGapSeconds'], gap)
                if gap > 2.0:
                    heartbeat['gapsOverTwoSeconds'].append({
                        'elapsedSeconds': now - self.started, 'gapSeconds': gap,
                        'editorReady': bool(self.result['ready']),
                        'completedCommands': len(self.result['commands']),
                    })
                    heartbeat['gapsOverTwoSeconds'] = sorted(
                        heartbeat['gapsOverTwoSeconds'],
                        key=lambda item: item['gapSeconds'], reverse=True)[:8]
            self._previous_tick = now
            if self.result['cleanupRequested']:
                return
            if os.path.exists(CANCEL):
                self.result['cancelled'] = True
                self.cleanup('cancelled')
                return
            if time.monotonic() - self.started >= self.timeout_seconds:
                self.result['error'] = 'Bounded UI save fixture timed out'
                self.cleanup('timeout')
                return
            try:
                with open(TARGET, 'r', encoding='utf-8-sig') as stream:
                    target = json.load(stream)
            except (ValueError, IOError):
                if self.target is not None:
                    raise RuntimeError('Validated target disappeared or became unreadable')
                # Read-only evidence while public discovery waits for ordinary
                # project matching. Keep transient status messages for diagnosis.
                bridge = sys.modules.get('renderdoc_mcp_bridge')
                ctx = getattr(bridge, '_ctx', None)
                waiting = {'captureLoaded': bool(ctx and ctx.IsCaptureLoaded()),
                           'projectExecutorReady': getattr(bridge, '_project_async_executor', None) is not None,
                           'projectRestoreActive': getattr(bridge, '_project_auto_restore_client', None) is not None}
                # The normal production callback retains this bounded plain-data
                # observation even if the fixture installed after restore began.
                restore_summary = getattr(bridge, '_project_auto_restore_result', None)
                if isinstance(restore_summary, dict):
                    waiting['autoRestoreResult'] = dict(restore_summary)
                completed_indexes = []
                guard = getattr(bridge, '_project_shader_index_jobs_guard', None)
                if guard is not None and guard.acquire(False):
                    try:
                        index_jobs = []
                        for job_id, job in list(getattr(bridge, '_project_shader_index_jobs', {}).items())[-8:]:
                            result = job.get('result') or {}
                            row = {'jobId': job_id, 'status': job.get('status'),
                                   'targets': (job.get('request') or {}).get('targetRawSha256s'),
                                   'progress': dict(job.get('progress') or {})}
                            row['result'] = {key: result.get(key) for key in (
                                'scannedResourceCount', 'shaderResourceCount', 'processedShaderResourceCount',
                                'matchedShaderCount', 'indexedShaderResourceCount', 'reasonId',
                                'filteredOutShaderResourceCount', 'targetMatchedShaderResourceIds',
                                'observedTargetRawSha256s', 'unobservedTargetRawSha256s',
                                'rawIdentitySearchComplete', 'targetInventoryComplete', 'diagnostics')}
                            index_jobs.append(row)
                            if (job.get('status') == 'completed' and isinstance(job_id, str) and
                                    len(job_id) == 32 and all(char in '0123456789abcdef' for char in job_id) and
                                    job_id not in self._index_receipts):
                                self._index_receipts.add(job_id)
                                # Select only the normal JSON payload, never job
                                # controller, cancelEvent, or replay/SWIG objects.
                                completed_indexes.append({
                                    'schema': 'replaylab-fixture-shader-index/v1',
                                    'pid': os.getpid(), 'jobId': job_id,
                                    'status': job['status'],
                                    'captureSessionId': job.get('captureSessionId'),
                                    'requestSha256': job.get('requestSha256'),
                                    'request': job.get('request'), 'result': job.get('result')})
                        waiting['indexJobs'] = index_jobs
                    finally:
                        guard.release()
                for receipt in completed_indexes:
                    path = os.path.join(os.path.dirname(os.path.abspath(OUT)),
                                        'index-' + receipt['jobId'] + '.json')
                    try:
                        # Full rawBytecodeBase64 is kept only in the separate
                        # output receipt. No I/O runs under the production guard.
                        RestoreResultObserver.write_json(path, receipt)
                        self.result.setdefault('indexReceipts', []).append({
                            'jobId': receipt['jobId'], 'path': path})
                    except Exception as recording_error:
                        self.result.setdefault('indexReceiptErrors', []).append({
                            'jobId': receipt['jobId'], 'type': type(recording_error).__name__})
                if ctx:
                    try:
                        window = ctx.GetMainWindow().Widget()
                        status_bar = getattr(window, 'statusBar', lambda: None)()
                        if status_bar is None:
                            status_bar = window.findChild(QtWidgets.QStatusBar, 'statusBar')
                        if status_bar is not None:
                            waiting['statusMessage'] = status_bar.currentMessage()
                    except Exception as diagnostic_error:
                        waiting['diagnosticError'] = str(diagnostic_error)
                history = self.result.setdefault('waitingDiagnostics', [])
                if not history or history[-1]['state'] != waiting:
                    history.append({'elapsedSeconds': time.monotonic() - self.started, 'state': waiting})
                    del history[:-32]
                    self.write()
                return  # No target means wait; never select a remembered workspace.
            if (target.get('schema') != 'replaylab-real-save-target/v1' or
                    target.get('expectedSourceSha256') != EXPECTED_SHA or
                    target.get('shaderRawSha256') != ORIGINAL_SHADER_SHA or
                    target.get('eventId') != EXPECTED_EVENT_ID or target.get('stage') != 'fragment' or
                    target.get('entryPoint') != 'MainPS' or
                    not all(isinstance(target.get(name), str) and target[name] for name in (
                        'workspaceId', 'workspacePath', 'sourcePath', 'instanceId',
                        'captureSessionId', 'capturePath', 'resourceId')) or
                    not target['resourceId'].isdigit() or
                    (WORKSPACE_HINT and target['workspaceId'] != WORKSPACE_HINT) or
                    not same_path(target['sourcePath'], os.path.join(target['workspacePath'], 'source', 'shader.hlsl'))):
                raise RuntimeError('Target lacks the exact accepted source and current-capture identity')
            if self.target is not None and target != self.target:
                raise RuntimeError('Validated target changed during the native save fixture')
            bridge = sys.modules.get('renderdoc_mcp_bridge')
            ctx, panel = getattr(bridge, '_ctx', None), getattr(bridge, '_shader_workspace_ui', None)
            if not ctx or not ctx.IsCaptureLoaded() or not panel:
                return
            if int(ctx.CurEvent()) != EXPECTED_EVENT_ID:
                return
            if (bridge._capture_session_id != target['captureSessionId'] or
                    bridge.INSTANCE_ID != target['instanceId']):
                raise RuntimeError('Target is from a different current capture or instance')
            workspace_id = target['workspaceId']
            model = panel._workspaces.get(workspace_id)
            if not model:
                return  # Wait for ordinary current-scope discovery/authorization.
            binding = model.get('captureBinding') or {}
            if (binding.get('instanceId') != target['instanceId'] or
                    binding.get('captureSessionId') != target['captureSessionId'] or
                    not same_path(binding.get('capturePath'), target['capturePath']) or
                    not same_path(model.get('workspacePath'), target['workspacePath']) or
                    str(model.get('resourceId')) != target['resourceId'] or
                    model.get('shaderRawSha256') != ORIGINAL_SHADER_SHA or
                    model.get('stage') != 'fragment' or model.get('entryPoint') != 'MainPS'):
                raise RuntimeError('Target differs from the current authorized workspace model')
            document = panel._documents.get(workspace_id)
            if document is None or not document.get('available'):
                panel._open_workspace(workspace_id, quiet=True)
                return
            if (document.get('captureBinding') != binding or
                    not same_path(document.get('sourcePath'), target['sourcePath'])):
                raise RuntimeError('Available document differs from the validated current target')
            if self.original is None:
                if document['editor'].IsReadOnly() or document.get('dirty'):
                    return
                source = panel._document_source_text(document)
                if sha(source) != EXPECTED_SHA:
                    raise RuntimeError('Expected accepted source before the first UI edit')
                self.original = source
                self.target = target
                self.session = bridge._capture_session_id
                self.instance = bridge.INSTANCE_ID
                panel._open_workspace(workspace_id, quiet=True, select=True)
                panel.show()
                if not panel._flow_visible:
                    panel.flow_button.click()
                panel.flow_graph.fit_button.click()
                graph = panel.flow_graph.view._graph
                items = []
                iterator = QtWidgets.QTreeWidgetItemIterator(panel.file_tree)
                while iterator.value():
                    items.append(iterator.value().text(0))
                    iterator += 1
                available = [d for d in panel._documents.values() if d.get('available')]
                self.result.update(ready=True, instanceId=self.instance, captureSessionId=self.session,
                    workspaceId=workspace_id, sourcePath=document['sourcePath'], sourceSha256=sha(source),
                    target=dict(target), captureBinding=dict(binding), resourceId=target['resourceId'],
                    readOnly=document['editor'].IsReadOnly(), treeItems=items,
                    visibleDocumentIds=[d['workspaceId'] for d in available],
                    allDocumentsCurrentCapture=all(d.get('captureBinding') == document.get('captureBinding') for d in available),
                    noProjectLibrary=not any(x in ('Project Library', '项目库') for x in items),
                    graphNodes=len(graph.get('nodes', [])))
                image = os.path.splitext(OUT)[0] + '-initial.png'
                if not panel.top.grab().save(image):
                    raise RuntimeError('Cannot save the actual workspace image')
                self.result['screenshot'] = image
                self.write()
            if bridge._capture_session_id != self.session or bridge.INSTANCE_ID != self.instance:
                raise RuntimeError('Capture changed during the bounded UI save fixture')
            if document.get('saveInFlight') or document.get('commitUnknown'):
                return
            if self.result['commands']:
                latest = self.result['commands'][-1]
                latest['saved'] = (document.get('rawSha256') == latest['expectedSha256'] and not document.get('dirty'))
                latest['diskReceiptSha256'] = document.get('rawSha256')
                latest['diagnosticSummary'] = str(panel.status_label.text())
                summary = document['editor'].Widget().findChild(QtWidgets.QToolButton, 'shaderCodeDiagnostics')
                latest['compilerErrorsVisible'] = summary is not None and not summary.isHidden()
                latest['compilerDetails'] = summary.toolTip() if summary else ''
                self.write()
            if not os.path.exists(COMMAND):
                return
            try:
                with open(COMMAND, 'r', encoding='utf-8-sig') as stream:
                    command = json.load(stream)
            except (ValueError, IOError):
                return
            sequence = int(command.get('sequence', 0))
            if sequence <= self.seen:
                return
            if (command.get('instanceId') != self.instance or
                    command.get('captureSessionId') != self.session or
                    command.get('workspaceId') != workspace_id or
                    command.get('expectedSourceSha256') != sha(panel._document_source_text(document))):
                raise RuntimeError('UI command no longer matches the current source and capture')
            action = command.get('action')
            if action == 'finish':
                if document.get('rawSha256') != EXPECTED_SHA or document.get('dirty'):
                    raise RuntimeError('The exact accepted source must be restored before completion')
                self.result['ok'] = self.result['noProjectLibrary'] and self.result['allDocumentsCurrentCapture']
                self.result['restoredSourceSha256'] = document.get('rawSha256')
                self.seen = sequence
                self.cleanup('success')
                return
            if document.get('dirty'):
                raise RuntimeError('Do not overwrite a dirty user edit')
            text = self.original
            if action == 'edit-alpha':
                if text.count('SV_Target.w = 0.0f;') != 1:
                    raise RuntimeError('Ambiguous output alpha assignment')
                text = text.replace('SV_Target.w = 0.0f;', 'SV_Target.w = 0.125f;')
            elif action == 'compile-error':
                text = '#error ReplayLab_native_save_fixture\n' + text
            elif action != 'restore':
                raise RuntimeError('Unknown bounded UI command')
            # Apply only the bounded difference with actual keystrokes. SetText is a load
            # API and suppresses the owner's dirty callback, so it cannot prove user saving.
            current = panel._document_source_text(document)
            hunks, changed_characters, planned_keys = native_edit_hunks(current, text)
            self.seen = sequence
            panel._open_workspace(workspace_id, quiet=True, select=True)
            widget = document['editor'].Widget().findChild(QtWidgets.QWidget, 'shaderCodeEditor')
            widget.setFocus()
            for start, end, insertion in hunks:
                before = current[:start]
                document['editor'].GoToLine(before.count('\n') + 1, len(before.rsplit('\n', 1)[-1]) + 1)
                for unused in range(end - start):
                    key(widget, QtCore.Qt.Key_Delete)
                for character in insertion:
                    if character == '\n':
                        key(widget, QtCore.Qt.Key_Escape)
                        key(widget, QtCore.Qt.Key_Return, text='\n')
                    else:
                        key(widget, QtCore.Qt.Key_unknown, text=character)
            key(widget, QtCore.Qt.Key_Escape)
            if panel._document_source_text(document) != text or not document.get('dirty'):
                raise RuntimeError('Native keystrokes did not produce the exact dirty proposal')
            key(widget, QtCore.Qt.Key_S, QtCore.Qt.ControlModifier)
            self.result['commands'].append({'sequence': sequence, 'action': action,
                'expectedSha256': sha(text), 'saved': False, 'submittedAt': time.time(),
                'nativeEditHunks': len(hunks), 'changedCharacters': changed_characters,
                'plannedKeyPairs': planned_keys,
                'nativeBufferSha256': sha(panel._document_source_text(document))})
            self.write()
        except Exception:
            self.result['error'] = traceback.format_exc()
            self.cleanup('exception')

probe = Probe()
sys._replaylab_real_save_probe = probe
try:
    # Install synchronously before dispatching start/timers. Do not submit or
    # repeat any restore request, including if capture loading already began.
    probe.install_restore_observer()
except Exception:
    probe.result['error'] = traceback.format_exc()
    probe.cleanup('observer-installation-failed')
    raise
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(probe.start)
