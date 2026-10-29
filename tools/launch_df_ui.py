import os


def launch_wegame():
    exe = r"F:\WeGame\wegame.exe"
    if not os.path.isfile(exe):
        print("WeGame executable not found: " + exe)
        return
    pyrenderdoc.ShowCaptureDialog()
    dialog = pyrenderdoc.GetCaptureDialog()
    settings = dialog.Settings()
    settings.inject = False
    settings.autoStart = False
    settings.executable = exe
    settings.workingDir = r"F:\WeGame"
    settings.commandLine = ""
    settings.options.hookIntoChildren = True
    dialog.SetSettings(settings)
    print("Launching WeGame from TinecmaTool with child-process capture enabled.")
    dialog.TriggerCapture()


pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(launch_wegame)
