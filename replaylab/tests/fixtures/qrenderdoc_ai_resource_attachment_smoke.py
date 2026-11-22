"""After the inline CBuffer probe, inspect native menu attachments without sending a chat.

Run in the same --ui-python launcher after qrenderdoc_cbuffer_live_edit_smoke.py.
The launcher owns the fresh application and closes it after collecting evidence.
"""
import json
import os
import sys
import time
import traceback
from pathlib import Path
from PySide2 import QtCore, QtWidgets


class AttachmentProbe(object):
    def __init__(self):
        self.output = Path(os.environ['REPLAYLAB_CBUFFER_EDIT_OUTPUT']).with_name('bread-146-resource-attachments.json')
        self.result = {'ok': False, 'checks': [], 'pid': os.getpid()}
        self.started = time.monotonic()
        self.phase = 'waiting'
        self.keep = []
        self.actions = ['addCBufferToAI', 'addCBufferFieldToAI']
        self.timer = QtCore.QTimer()
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def save(self):
        self.result['phase'] = self.phase
        self.output.write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding='utf-8')

    def tick(self):
        try:
            if time.monotonic() - self.started > 420:
                raise RuntimeError('Attachment probe timed out in ' + self.phase)
            self.probe = sys.modules['__main__']._bread_inline_cb_probe
            if not self.probe.done:
                return
            if not self.probe.result.get('ok'):
                raise RuntimeError('Inline probe failed before attachment check')
            self.window = self.probe.widget.window()
            if self.phase == 'waiting':
                self.phase = 'menu'
                self.save()
            if self.phase == 'menu':
                if not self.actions:
                    self.result['ok'] = True
                    self.phase = 'complete'
                    self.timer.stop()
                    self.save()
                    return
                pyrenderdoc.RaiseDockWindow(self.probe.widget)
                self.phase = 'menu-ready'
                return
            if self.phase == 'menu-ready':
                index = self.probe.row('baseTint')
                self.probe.tree.setCurrentIndex(index)
                self.probe.tree.scrollTo(index)
                self.phase = 'choose'
                QtCore.QTimer.singleShot(30, self.choose)
                self.probe.tree.customContextMenuRequested.emit(self.probe.tree.visualRect(index).center())
            elif self.phase == 'chip':
                chips = self.window.findChildren(QtWidgets.QFrame, 'aiAttachmentChip')
                self.keep.extend(chips)
                if len(chips) != 1:
                    raise RuntimeError('Expected one accepted native attachment, got ' + str(len(chips)))
                self.chip = chips[0]
                buttons = self.chip.findChildren(QtWidgets.QAbstractButton)
                self.keep.extend(buttons)
                previews = [b for b in buttons if b.accessibleName().startswith(
                    QtCore.QCoreApplication.translate('AIAssistant', 'Preview attachment: %1').split('%1')[0])]
                if not previews:
                    raise RuntimeError('No attachment preview action')
                self.phase = 'preview'
                QtCore.QTimer.singleShot(30, self.inspect_preview)
                previews[0].click()
            elif self.phase == 'remove':
                buttons = self.chip.findChildren(QtWidgets.QAbstractButton)
                self.keep.extend(buttons)
                prefix = QtCore.QCoreApplication.translate('AIAssistant', 'Remove attachment: %1').split('%1')[0]
                remove = next(b for b in buttons if b.accessibleName().startswith(prefix))
                remove.click()
                self.actions.pop(0)
                self.phase = 'menu'
        except Exception:
            self.result['error'] = traceback.format_exc()
            self.phase = 'failed'
            self.timer.stop()
            self.save()

    def choose(self):
        menu = QtWidgets.QApplication.activePopupWidget()
        self.keep.append(menu)
        if menu is None:
            self.result['error'] = 'Native context menu did not open'
            self.phase = 'failed'
            self.timer.stop()
            self.save()
            return
        actions = menu.actions()
        self.keep.extend(actions)
        action = next((a for a in actions if a.objectName() == self.actions[0]), None)
        if action is None:
            self.result['error'] = 'Native context action unavailable: ' + self.actions[0]
            self.result['availableActions'] = [a.objectName() for a in actions]
            self.phase = 'failed'
            self.timer.stop()
            menu.close()
            self.save()
            return
        action.trigger()
        menu.close()
        self.phase = 'chip'

    def inspect_preview(self):
        dialog = None
        try:
            dialog = QtWidgets.QApplication.activeModalWidget()
            self.keep.append(dialog)
            edit = dialog.findChild(QtWidgets.QPlainTextEdit)
            self.keep.append(edit)
            body = json.loads(edit.toPlainText())
            assert body['capture']['eventId'] == 61408
            assert isinstance(body['shader']['resourceId'], str)
            assert body['resource']['binding'] == 5
            assert body['resource']['byteOffset'] == '3663872'
            assert body['bindingsAtEvent']
            if self.actions[0] == 'addCBufferFieldToAI':
                assert body['resource']['fieldName'] == 'baseTint'
                assert len(body['resource']['componentByteOffsets']) == 3
            self.result['checks'].append({'action': self.actions[0], 'context': body})
            self.phase = 'remove'
            buttons = dialog.findChild(QtWidgets.QDialogButtonBox)
            self.keep.append(buttons)
            close = buttons.button(QtWidgets.QDialogButtonBox.Close)
            self.keep.append(close)
            close.click()
            self.save()
        except Exception:
            self.result['error'] = traceback.format_exc()
            self.phase = 'failed'
            self.timer.stop()
            if dialog is not None:
                dialog.reject()
            self.save()


def start_attachment_probe():
    sys.modules['__main__']._bread_resource_attachment_probe = AttachmentProbe()


pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(start_attachment_probe)
