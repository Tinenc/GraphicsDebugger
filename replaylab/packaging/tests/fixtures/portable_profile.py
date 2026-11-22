"""Run inside an isolated portable GUI with --ui-script (Python 3.6 compatible)."""
import json
import os
from pathlib import Path
import sys
import traceback

from PySide2 import QtCore
import renderdoc as rd


def verify():
    output = Path(os.environ['BREAD_PROFILE_SMOKE_OUTPUT'])
    result = {'ok': False}
    try:
        expected = Path(os.environ['BREAD_PROFILE_SMOKE_EXPECTED'])
        actual = QtCore.QProcessEnvironment.systemEnvironment().value('RENDERDOC_CONFIG_DIR')
        if os.path.normcase(os.path.normpath(actual)) != os.path.normcase(str(expected)):
            raise AssertionError('Native Unicode configuration directory differs: ' + actual)
        window = pyrenderdoc.GetMainWindow().Widget()
        if not window.isVisible():
            raise AssertionError('Main window is not visible')
        # Exercise the real core's environment reader and UTF-8 filesystem writer.
        rd.SaveConfigSettings()
        if not (expected / 'renderdoc.conf').is_file():
            raise AssertionError('Core configuration was not saved in the expected directory')
        result.update(ok=True, mainWindowVisible=True, coreConfigSaved=True,
                      nativeConfigDirectory=actual)
    except Exception:
        result['error'] = traceback.format_exc()
    finally:
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        QtCore.QTimer.singleShot(50, pyrenderdoc.GetMainWindow().Widget().close)


sys.modules['__main__']._bread_profile_smoke = verify
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(verify)
