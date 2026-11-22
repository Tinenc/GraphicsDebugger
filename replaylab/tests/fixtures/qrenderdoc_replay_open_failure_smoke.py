"""Isolated native nonmodal load-error acceptance; run with --ui-python.

Set BREAD_REPLAY_OPEN_FAILURE_SMOKE_ROOT to a system-temp directory and isolate
LOCALAPPDATA and RENDERDOC_CONFIG_DIR below it. No existing capture, project,
GPU fault, or model session is used. The fixture opens its own invalid RDC,
checks the native error result and absence of error dialogs, and closes itself.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

from PySide2 import QtCore, QtWidgets
import renderdoc as rd


def contained(child, parent):
    child = os.path.normcase(str(Path(child).resolve()))
    parent = os.path.normcase(str(Path(parent).resolve()))
    return child != parent and os.path.commonpath([child, parent]) == parent


class OpenFailureSmoke(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.root = Path(os.environ['BREAD_REPLAY_OPEN_FAILURE_SMOKE_ROOT']).resolve()
        if not contained(self.root, tempfile.gettempdir()):
            raise RuntimeError('Native failure fixture requires system-temp isolation')
        for key in ('LOCALAPPDATA', 'RENDERDOC_CONFIG_DIR'):
            if not contained(os.environ[key], self.root):
                raise RuntimeError('Native failure fixture must isolate ' + key)
        self.root.mkdir(parents=True, exist_ok=True)
        self.result = {'schema': 'bread-replay-open-failure-native-smoke/v1', 'ok': False,
                       'pid': os.getpid(), 'checks': [], 'errorDialogs': []}
        self.started = time.monotonic()

    def check(self, name, condition):
        self.result['checks'].append({'name': name, 'ok': bool(condition)})
        if not condition:
            raise AssertionError(name)

    def dismiss_unexpected_errors(self):
        # A regression must fail deterministically instead of hanging CI behind
        # a native modal. This only touches dialogs owned by the isolated test.
        for widget in QtWidgets.QApplication.topLevelWidgets():
            if isinstance(widget, QtWidgets.QMessageBox) and widget.isVisible():
                self.result['errorDialogs'].append(widget.windowTitle())
                widget.reject()

    def start(self):
        self.guard = QtCore.QTimer()
        self.guard.setInterval(25)
        self.guard.timeout.connect(self.dismiss_unexpected_errors)
        self.guard.start()
        try:
            self.check('no user capture is open', not self.ctx.IsCaptureLoaded())
            invalid = self.root / 'invalid-fixture.rdc'
            invalid.write_bytes(b'Bread nonmodal recovery error fixture, not an RDC')
            result = self.ctx.LoadCaptureForReplayRecovery(
                str(invalid), self.ctx.Config().DefaultReplayOptions, str(invalid))
            self.result['nativeResult'] = {'code': str(result.code), 'diagnostic': str(result)}
            self.check('native failure is returned', result.code != rd.ResultCode.Succeeded)
            self.check('failure diagnostic is retained', bool(str(result)))
            self.check('no capture is falsely loaded', not self.ctx.IsCaptureLoaded())
            self.check('load operation has ended', not self.ctx.IsCaptureLoading())
            self.check('no error dialog was displayed', not self.result['errorDialogs'])
            self.result['ok'] = True
        except Exception:
            self.result['error'] = traceback.format_exc()
        finally:
            self.guard.stop()
            self.result['elapsedSeconds'] = time.monotonic() - self.started
            (self.root / 'result.json').write_text(
                json.dumps(self.result, ensure_ascii=False, indent=2), encoding='utf-8')
            # qrenderdoc owns a custom ctx.isRunning() event loop. Closing its
            # main window performs lifecycle cleanup; QApplication.quit alone
            # does not terminate that loop.
            QtCore.QTimer.singleShot(50, self.ctx.GetMainWindow().Widget().close)


runner = OpenFailureSmoke(pyrenderdoc)
sys.modules['__main__']._bread_open_failure_smoke = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
