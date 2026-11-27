import os
import traceback


def _launch_wegame_impl():
    try:
        exe = os.environ.get("ABI_WEGAME_EXE", r"F:\WeGame\wegame.exe")
        if not os.path.isfile(exe):
            print("[ABI] WeGame executable not found: " + exe)
            return
        workdir = os.path.dirname(exe)
        pyrenderdoc.ShowCaptureDialog()
        dialog = pyrenderdoc.GetCaptureDialog()
        settings = dialog.Settings()
        settings.inject = False
        settings.autoStart = False
        settings.executable = exe
        settings.workingDir = workdir
        settings.commandLine = ""
        settings.options.hookIntoChildren = True
        dialog.SetSettings(settings)
        print("[ABI] Launching WeGame with child-process capture enabled: " + exe)
        dialog.TriggerCapture()
    except Exception:
        print("[ABI] Failed to launch WeGame:")
        traceback.print_exc()


def _schedule_launch():
    try:
        from PySide2 import QtCore
        QtCore.QTimer.singleShot(1500, _launch_wegame_impl)
    except Exception:
        _launch_wegame_impl()


pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(_schedule_launch)
