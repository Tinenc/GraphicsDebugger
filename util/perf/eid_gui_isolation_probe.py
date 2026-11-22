"""No-capture isolation probe for qrenderdoc --ui-script (embedded Python 3.6).

Launch through run_eid_isolated_gui.py with an empty script configuration, or set
requireInternalHost=false to explicitly limit acceptance to GUI/bridge isolation.
The report records that limitation. This script never opens a capture or repairs
module/extension paths, and exits only its own QApplication.
"""

import ctypes
import hashlib
import json
import os
from pathlib import Path
import site
import sys
import tempfile
import time
import traceback


def _same_path(left, right):
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def _below(root, value):
    root = os.path.normcase(os.path.abspath(str(root)))
    value = os.path.normcase(os.path.abspath(str(value)))
    try:
        return value != root and os.path.commonpath((root, value)) == root
    except ValueError:
        return False


def _sha256(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _windows_modules():
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.K32EnumProcessModules.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE),
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    kernel.K32EnumProcessModules.restype = wintypes.BOOL
    kernel.K32GetModuleFileNameExW.argtypes = (wintypes.HANDLE, wintypes.HMODULE,
                                             wintypes.LPWSTR, wintypes.DWORD)
    kernel.K32GetModuleFileNameExW.restype = wintypes.DWORD
    process = kernel.GetCurrentProcess()
    capacity = 256
    while capacity <= 8192:
        handles = (wintypes.HMODULE * capacity)()
        needed = wintypes.DWORD()
        if not kernel.K32EnumProcessModules(process, handles, ctypes.sizeof(handles),
                                           ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        if needed.value <= ctypes.sizeof(handles):
            break
        capacity *= 2
    else:
        raise RuntimeError("Loaded-module enumeration exceeded its bound")
    paths = []
    for handle in handles[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
        text = ctypes.create_unicode_buffer(32768)
        length = kernel.K32GetModuleFileNameExW(process, handle, text, len(text))
        if not length or length >= len(text):
            raise RuntimeError("Could not obtain a complete loaded-module path")
        paths.append(text.value)
    return paths


def _windows_process_path(pid):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION, read only
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        text = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(text))
        if not kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        return text.value
    finally:
        kernel.CloseHandle(handle)


def _start(context):
    from PySide2 import QtCore, QtWidgets
    import qrenderdoc as qrd

    application = QtWidgets.QApplication.instance()
    config_path = Path(os.environ["RENDERDOC_EID_UI_TEST_CONFIG"])
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    root = Path(config["worktree"])
    run = Path(config["isolationRoot"])
    output = Path(config["output"])
    if not _below(root, run) or not _below(run, config_path) or not _below(run, output):
        raise ValueError("Probe configuration/output escapes the isolated run")
    require_host = config.get("requireInternalHost", True)
    if type(require_host) is not bool:
        raise ValueError("requireInternalHost must be a JSON boolean")
    timeout = float(config.get("probeTimeoutSeconds", 45))
    if not 1 <= timeout <= 120:
        raise ValueError("probeTimeoutSeconds must be between 1 and 120")
    run_bin = root / "x64" / "Development"
    result = {"schema": "eid-gui-isolation-probe/v1", "ok": False, "pid": os.getpid(),
              "captureOpened": False, "internalHostRequired": require_host,
              "internalServiceIsolationVerified": False, "checks": [],
              "startedUtcEpoch": time.time()}
    timer = QtCore.QTimer(application)
    timer.setInterval(100)
    deadline = time.monotonic() + timeout

    def check(name, passed, evidence):
        result["checks"].append({"name": name, "passed": bool(passed), "evidence": evidence})

    def finish(error=None):
        timer.stop()
        if error:
            result["error"] = error
        result["ok"] = not error and all(item["passed"] for item in result["checks"])
        result["completedUtcEpoch"] = time.time()
        temporary = output.with_name(output.name + ".tmp-" + str(os.getpid()))
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(output))
        def close_this_instance():
            # qrenderdoc pumps processEvents while its own main window is visible instead of
            # entering QApplication.exec(). quit alone cannot terminate that loop.
            main_window = context.GetMainWindow()
            main_window.Widget().close()
            QtCore.QCoreApplication.quit()

        # No closeAllWindows(), OS process lookup, or manipulation of another instance.
        QtCore.QTimer.singleShot(0, close_this_instance)

    try:
        check("noCapture", not context.IsCaptureLoaded(), bool(context.IsCaptureLoaded()))
        expected_paths = {"RENDERDOC_CONFIG_DIR": run / "config", "LOCALAPPDATA": run / "local",
                          "APPDATA": run / "roaming", "TEMP": run / "temp", "TMP": run / "temp",
                          "TMPDIR": run / "temp"}
        for name, expected in expected_paths.items():
            actual = os.environ.get(name, "")
            check(name, bool(actual) and _same_path(actual, expected), actual)
        check("resolvedTempDirectories", _same_path(tempfile.gettempdir(), run / "temp")
              and _same_path(QtCore.QDir.tempPath(), run / "temp"),
              {"python": tempfile.gettempdir(), "qt": QtCore.QDir.tempPath()})
        config_files = {name: str(qrd.ConfigFilePath(name))
                        for name in ("", "UI.config", "DefaultLayout.config", "Layout0.config",
                                     "ReplayLab/conversations", "extensions")}
        check("nativeConfigFilePath", all(_same_path(path, run / "config" / name)
                                          for name, path in config_files.items()), config_files)
        check("userSiteDisabled", site.ENABLE_USER_SITE is False and sys.flags.no_user_site == 1,
              {"enableUserSite": site.ENABLE_USER_SITE, "noUserSiteFlag": sys.flags.no_user_site})
        check("python36", sys.version_info[:2] == (3, 6), sys.version)
        search_paths = [str(path) for path in sys.path]
        # Empty sys.path entries resolve to the runner's private application working directory.
        check("pythonSearchRoots", all(_below(root, path or os.getcwd()) for path in search_paths),
              {"sysPath": search_paths, "cwd": os.getcwd(), "prefix": sys.prefix,
               "dontWriteBytecode": sys.dont_write_bytecode})
        check("application", _same_path(application.applicationFilePath(),
                                         run_bin / "q_render_soco_doc.exe"),
              {"executable": application.applicationFilePath(),
               "qtLibraryPaths": list(application.libraryPaths()),
               "applicationName": application.applicationName(),
               "organizationName": application.organizationName()})
        modules = []
        for name in ("renderdoc", "qrenderdoc", "PySide2", "PySide2.QtCore", "PySide2.QtWidgets"):
            module = sys.modules.get(name)
            path = getattr(module, "__file__", None)
            origin = getattr(getattr(module, "__spec__", None), "origin", None)
            builtin = name in ("renderdoc", "qrenderdoc") and origin == "built-in"
            modules.append({"name": name, "path": path, "origin": origin})
            check("module:" + name, module is not None and (builtin or path and _below(run_bin, path)),
                  modules[-1])
        extensions = [{"package": str(item.package), "path": str(item.filePath),
                       "loaded": bool(context.Extensions().IsExtensionLoaded(item.package))}
                      for item in context.Extensions().GetInstalledExtensions()]
        extension_roots = (run_bin / "extensions", run / "config" / "extensions")
        check("extensions", all(any(_below(base, item["path"]) or _same_path(base, item["path"])
                                     for base in extension_roots) and not item["loaded"]
                                 for item in extensions), extensions)
        bridge = sys.modules.get("renderdoc_mcp_bridge")
        bridge_path = getattr(bridge, "__file__", "")
        expected_bridge = run_bin / "integrations" / "replaylab" / "python" / "renderdoc_mcp_bridge"
        check("fixedBridge", bridge is not None and _same_path(bridge_path, expected_bridge / "__init__.py")
              and getattr(bridge, "_ctx", None) is not None,
              {"path": bridge_path, "registered": getattr(bridge, "_ctx", None) is not None})
        ipc = str(getattr(bridge, "IPC_DIR", ""))
        check("bridgeIPC", bool(ipc) and _below(run / "local" / "ReplayLab" / "ipc-v2" / "instances", ipc)
              and Path(ipc).name.startswith(str(os.getpid()) + "-"), ipc)
        check("externalHostDisabled", os.environ.get("REPLAYLAB_EXTERNAL_MCP_AUTOSTART") == "0"
              and not getattr(bridge, "_replaylab_host_owner_active", True),
              bool(getattr(bridge, "_replaylab_host_owner_active", True)))
        if os.name != "nt":
            raise RuntimeError("This probe's native module/process evidence requires Windows")
        loaded = _windows_modules()
        relevant = [path for path in loaded if Path(path).name.lower().startswith(("qt5", "shiboken", "pyside"))
                    or Path(path).name.lower() in ("renderdoc.dll", "python36.dll", "qwindows.dll")]
        names = {Path(path).name.lower() for path in relevant}
        check("nativeModuleSources", {"renderdoc.dll", "python36.dll", "qt5core.dll", "qt5gui.dll",
                                      "qt5widgets.dll", "qwindows.dll"}.issubset(names)
              and all(_below(run_bin, path) for path in relevant),
              [{"path": path, "sha256": _sha256(path)} for path in relevant])
        if not all(item["passed"] for item in result["checks"]):
            finish()
            return
    except Exception:
        finish(traceback.format_exc())
        return

    def poll():
        try:
            if not require_host:
                finish()
                return
            state_file = run / "local" / "ReplayLab" / "internal-runtime" / "internal-host.json"
            owner_file = state_file.parent / "owners" / (str(os.getpid()) + ".json")
            if not state_file.is_file() or not owner_file.is_file():
                if time.monotonic() >= deadline:
                    raise RuntimeError("Private internal-host state/owner did not become ready")
                return
            state = json.loads(state_file.read_text(encoding="utf-8-sig"))
            owner = json.loads(owner_file.read_text(encoding="utf-8-sig"))
            executable = _windows_process_path(int(state.get("pid", 0)))
            port = state.get("port", 0)
            age = time.time() * 1000 - int(state.get("heartbeatUnixMs", 0))
            accepted = (type(port) is int and 1024 <= port <= 65535 and -5000 <= age <= 15000
                        and state.get("host") == "127.0.0.1" and owner.get("pid") == os.getpid()
                        and _same_path(executable, run_bin / "replaylab" / "ReplayLabInternal.exe")
                        and state.get("endpoint") == "http://127.0.0.1:%d/replaylab/internal" % port)
            check("privateInternalHost", accepted,
                  {"statePath": str(state_file), "ownerPath": str(owner_file), "pid": state.get("pid"),
                   "executable": executable, "port": port, "heartbeatAgeMs": age,
                   "hostInstanceId": state.get("hostInstanceId")})
            result["internalServiceIsolationVerified"] = bool(accepted)
            finish()
        except Exception:
            finish(traceback.format_exc())

    timer.timeout.connect(poll)
    timer.start()
    # Keep Python callbacks and their native QObject wrappers alive until the app exits.
    application._eid_isolation_probe = (timer, poll, finish)


if "pyrenderdoc" in globals():
    pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(lambda: _start(pyrenderdoc))
