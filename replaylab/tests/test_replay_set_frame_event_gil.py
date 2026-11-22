"""Exercise the actual narrow SWIG action with a native blocking replay stand-in."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import threading

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def replay_gil_probe(tmp_path_factory):
    bundled_swig = ROOT / "qrenderdoc/3rdparty/swig/swig.exe"
    swig = str(bundled_swig) if sys.platform == "win32" else shutil.which("swig")
    if not swig or not Path(swig).is_file():
        pytest.skip("A SWIG executable is required for the native binding regression")
    pytest.importorskip("setuptools")
    from setuptools._distutils import ccompiler
    from setuptools._distutils.sysconfig import customize_compiler

    compiler = ccompiler.new_compiler()
    if sys.platform == "win32":
        try:
            compiler.initialize()
        except Exception as exc:
            pytest.skip(f"A C++ compiler is required for the native binding regression: {exc}")
    else:
        customize_compiler(compiler)

    source = (ROOT / "qrenderdoc/Code/pyrenderdoc/renderdoc.i").read_text(encoding="utf-8")
    begin = source.index("%define REPLAY_NATIVE_GIL_RELEASE(Method)")
    end = source.index("%include <stdint.i>", begin)
    action = source[begin:end]
    callback_source = (ROOT / "qrenderdoc/Code/pyrenderdoc/function_conversion.h").read_text(encoding="utf-8")
    callback_begin = callback_source.index("struct ScopedFuncCall\n")
    callback_end = callback_source.index("\n};", callback_begin) + len("\n};")
    callback_scope = callback_source[callback_begin:callback_end]
    release_body = action[action.index("%exception IReplayController::Method {") + len("%exception IReplayController::Method {"):action.index("\n}\n%enddef")]
    unwind_probe = "bool TestExceptionRestore() { try {\n" + release_body.replace("$action", 'throw std::runtime_error("native failure");') + "\n} catch(const std::exception &) { return PyGILState_Check() != 0; } return false; }\n"
    scratch = tmp_path_factory.mktemp("replay-gil-binding")
    interface = scratch / "replay_gil_probe.i"
    native = scratch / "probe.h"
    native.write_text(
        r"""
#include <Python.h>
#include <atomic>
#include <chrono>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

static std::atomic<bool> nativeActive{false};
extern "C" bool ProbeNativeActive() { return nativeActive.load(); }

static int callbackRefGIL = -1;
static PyObject *ProbeNewRef(PyObject *obj)
{
  callbackRefGIL = PyGILState_Check();
  if(!callbackRefGIL) throw std::runtime_error("callback INCREF without GIL");
  Py_XINCREF(obj);
  return obj;
}
#undef Py_XNewRef
#define Py_XNewRef ProbeNewRef
""" + callback_scope + "\n" + unwind_probe + r"""

struct ShaderReflection { int marker = 42; };
struct ShaderEntryPoint { std::string name = "MainPS"; unsigned stage = 4; };
struct ShaderBuildResult { unsigned resource = 99; std::string messages = "compiled"; };
struct ShaderDebugTrace { unsigned marker = 91; };
struct ShaderDebugger { unsigned marker = 92; };

class IReplayController
{
public:
  IReplayController() : owner(std::this_thread::get_id()) {}
  void SetFrameEvent(unsigned int delayMs, bool force)
  { Block(delayMs); }
  void ReplayFrameEventForAnalysis(unsigned int delayMs) { Block(delayMs); }
  void RestoreFrameEventAfterAnalysis() { Block(100); }
  ShaderBuildResult ComparePixelShaderPair(unsigned delayMs, unsigned original,
      unsigned baseline, unsigned candidate)
  { Block(delayMs); return ShaderBuildResult(); }
  std::vector<ShaderEntryPoint> GetShaderEntryPoints(unsigned int delayMs)
  { Block(delayMs); return {ShaderEntryPoint()}; }
  // Negative control for the GUI/GIL regression: same native work without the targeted wrapper.
  std::vector<ShaderEntryPoint> GetShaderEntryPointsUnreleased(unsigned int delayMs)
  { Block(delayMs); return {ShaderEntryPoint()}; }
  const ShaderReflection *GetShader(unsigned int delayMs, unsigned int resource, const std::string &entry)
  { Block(delayMs); return &reflection; }
  std::string DisassembleShader(unsigned int delayMs, const ShaderReflection *shader, const std::string &target)
  { Block(delayMs); return "assembly"; }
  ShaderBuildResult BuildTargetShader(const std::string &entry, unsigned int delayMs, const std::string &data, unsigned flags, unsigned stage)
  { Block(delayMs); return ShaderBuildResult(); }
  std::string GetTextureData(unsigned int delayMs, const unsigned int &subresource)
  { Block(delayMs); return std::string("bytes\0with-nul", 14); }
  std::string GetBufferData(unsigned delayMs, unsigned offset, unsigned length)
  { Block(delayMs); return std::string("bytes\0with-nul", 14); }
  ShaderBuildResult SaveTexture(const unsigned &delayMs, const std::string &path)
  { Block(delayMs); return ShaderBuildResult(); }
  std::vector<ShaderEntryPoint> GetCBufferVariableContents(unsigned delayMs, unsigned shader,
      unsigned stage, const std::string &entry, unsigned slot, unsigned buffer, unsigned offset,
      unsigned length)
  { Block(delayMs); return {ShaderEntryPoint()}; }
  ShaderBuildResult GetPostVSData(unsigned delayMs, unsigned view, unsigned stage)
  { Block(delayMs); return ShaderBuildResult(); }
  std::vector<ShaderEntryPoint> PixelHistory(unsigned delayMs, unsigned x, unsigned y,
      const unsigned &subresource, unsigned type)
  { Block(delayMs); return {ShaderEntryPoint()}; }
  ShaderBuildResult GetMinMax(unsigned delayMs, const unsigned &subresource, unsigned type)
  { Block(delayMs); return ShaderBuildResult(); }
  std::vector<ShaderEntryPoint> FetchCounters(const std::vector<unsigned> &counters)
  { Block(100); return {ShaderEntryPoint()}; }
  ShaderDebugTrace *DebugVertex(unsigned delayMs, unsigned instance, unsigned index, unsigned view)
  { Block(delayMs); return new ShaderDebugTrace(); }
  ShaderDebugTrace *DebugPixel(unsigned delayMs, unsigned y, const unsigned &inputs)
  { Block(delayMs); return new ShaderDebugTrace(); }
  std::vector<ShaderEntryPoint> ContinueDebug(ShaderDebugger *debugger)
  { Block(100); return {ShaderEntryPoint()}; }
  void FreeTrace(ShaderDebugTrace *trace) { Block(100); delete trace; }
  void ReplaceResource(unsigned int delayMs, unsigned int target) { Block(delayMs); }
  std::string ReplaceTargetShaderChecked(unsigned int delayMs, unsigned int target) { Block(delayMs); return ""; }
  void RemoveReplacement(unsigned int delayMs) { Block(delayMs); }
  std::string RemoveTargetShaderReplacementChecked(unsigned int delayMs) { Block(delayMs); return ""; }
  std::string SetEventShaderOverride(unsigned delayMs, unsigned stage, unsigned original, unsigned replacement)
  { Block(delayMs); return ""; }
  void ClearEventShaderOverride(unsigned delayMs, unsigned stage) { Block(delayMs); }
  void ClearEventShaderOverrides() { Block(100); }
  std::string SetEventCBufferOverride(unsigned delayMs, unsigned stage, unsigned slot,
      unsigned shader, unsigned buffer, unsigned offset, unsigned size, std::string data,
      unsigned space)
  { Block(delayMs); return ""; }
  void ClearEventCBufferOverride(unsigned delayMs, unsigned stage, unsigned slot, unsigned space)
  { Block(delayMs); }
  void ClearEventCBufferOverrides() { Block(100); }
  std::string SetSharedBufferPatch(unsigned delayMs, unsigned offset, std::string data)
  { Block(delayMs); return ""; }
  std::string ClearSharedBufferPatch(unsigned delayMs, unsigned offset, unsigned length)
  { Block(delayMs); return ""; }
  std::string ResetSharedBufferRange(unsigned delayMs, unsigned offset, unsigned length)
  { Block(delayMs); return ""; }
  std::string ClearSharedBufferPatches() { Block(100); return ""; }
  void ClearReplayCache() { Block(100); }
  void FreeTargetResource(unsigned int delayMs) { Block(delayMs); }
  int GetCallbackRefGIL() { return callbackRefGIL; }
  bool GetCallbackRestoredGIL() { return callbackRestoredGIL; }
  void NotePythonProgress() { if(active.load()) ++progress; }
  int GetProgress() { return progress.load(); }
  int GetCalls() { return calls; }
  int GetNativeGIL() { return nativeGIL; }
  bool GetSameThread() { return sameThread; }
  int GetReadGIL() { return PyGILState_Check(); }
private:
  void Block(unsigned int delayMs)
  {
    ++calls;
    nativeGIL = PyGILState_Check();
    sameThread = owner == std::this_thread::get_id();
    active.store(true);
    nativeActive.store(true);
    std::this_thread::sleep_for(std::chrono::milliseconds(delayMs));
    {
      ScopedFuncCall callback(Py_None);
      if(PyGILState_Check() == 0) throw std::runtime_error("callback has no GIL");
    }
    callbackRestoredGIL = PyGILState_Check() == nativeGIL;
    active.store(false);
    nativeActive.store(false);
  }
  ShaderReflection reflection;
  bool callbackRestoredGIL = false;
  const std::thread::id owner;
  std::atomic<bool> active{false};
  std::atomic<int> progress{0};
  int calls = 0;
  int nativeGIL = -1;
  bool sameThread = false;
};
""",
        encoding="utf-8",
    )
    interface.write_text(
        '%module replay_gil_probe\n'
        '%{\n#include "probe.h"\n%}\n'
        '%include <std_string.i>\n'
        '%include <std_vector.i>\n'
        '%template(Counters) std::vector<unsigned>;\n'
        '%ignore ProbeNewRef;\n%ignore ScopedFuncCall;\n%ignore ProbeNativeActive;\n%ignore nativeActive;\n'
        '%rename("ReplayController") IReplayController;\n'
        + action
        + '\n%include "probe.h"\n%template(ShaderEntryPoints) std::vector<ShaderEntryPoint>;\n',
        encoding="utf-8",
    )
    generated = scratch / "probe_wrap.cxx"
    subprocess.run(
        [swig, "-c++", "-python", "-builtin", "-o", str(generated), str(interface)],
        cwd=scratch,
        check=True,
        capture_output=True,
        text=True,
    )
    compile_args = ["/EHsc"] if sys.platform == "win32" else ["-std=c++11"]
    objects = compiler.compile(
        [str(generated)],
        output_dir=str(scratch),
        include_dirs=[sysconfig.get_path("include"), str(scratch)],
        extra_postargs=compile_args,
    )
    extension = scratch / ("_replay_gil_probe" + sysconfig.get_config_var("EXT_SUFFIX"))
    kwargs = {}
    if sys.platform == "win32":
        kwargs["library_dirs"] = [str(Path(sys.base_prefix) / "libs")]
    compiler.link_shared_object(objects, str(extension), target_lang="c++", **kwargs)
    spec = importlib.util.spec_from_file_location("_replay_gil_probe", extension)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._probe_compiler = compiler
    module._probe_objects = objects
    module._probe_scratch = scratch
    return module


def test_native_replay_releases_gil_without_changing_calling_thread(replay_gil_probe):
    controller = replay_gil_probe.ReplayController()
    stop = threading.Event()
    ready = threading.Event()

    def python_worker():
        ready.set()
        while not stop.wait(0.001):
            controller.NotePythonProgress()

    worker = threading.Thread(target=python_worker)
    worker.start()
    try:
        assert ready.wait(2)
        assert controller.SetFrameEvent(200, True) is None
    finally:
        stop.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert controller.GetProgress() > 0
    assert controller.GetNativeGIL() == 0
    assert controller.GetSameThread() is True
    # The targeted action must not release the GIL around unrelated API methods or after return.
    assert controller.GetReadGIL() == 1
    assert controller.GetCalls() == 1


def test_invalid_arguments_do_not_enter_native_replay(replay_gil_probe):
    controller = replay_gil_probe.ReplayController()
    with pytest.raises((TypeError, OverflowError)):
        controller.SetFrameEvent("not an event ID", False)
    assert controller.GetCalls() == 0
    assert controller.GetReadGIL() == 1


@pytest.mark.parametrize("method,arguments", [
    ("ReplayFrameEventForAnalysis", (100,)),
    ("RestoreFrameEventAfterAnalysis", ()),
    ("ComparePixelShaderPair", (100, 1, 1, 2)),
    ("GetShaderEntryPoints", (100,)),
    ("GetShader", (100, 1, "MainPS")),
    ("DisassembleShader", (100, None, "DXIL")),
    ("BuildTargetShader", ("MainPS", 100, "source", 0, 0)),
    ("GetTextureData", (100, 0)),
    ("GetBufferData", (100, 0, 14)),
    ("SaveTexture", (100, "texture.png")),
    ("GetCBufferVariableContents", (100, 1, 4, "MainPS", 0, 2, 0, 16)),
    ("GetPostVSData", (100, 0, 0)),
    ("PixelHistory", (100, 0, 0, 0, 0)),
    ("GetMinMax", (100, 0, 0)),
    ("FetchCounters", ((1, 2),)),
    ("DebugVertex", (100, 0, 0, 0)),
    ("DebugPixel", (100, 0, 0)),
    ("ContinueDebug", (None,)),
    ("FreeTrace", (None,)),
    ("ReplaceResource", (100, 2)),
    ("ReplaceTargetShaderChecked", (100, 2)),
    ("RemoveReplacement", (100,)),
    ("RemoveTargetShaderReplacementChecked", (100,)),
    ("SetEventShaderOverride", (100, 4, 1, 2)),
    ("ClearEventShaderOverride", (100, 4)),
    ("ClearEventShaderOverrides", ()),
    ("SetEventCBufferOverride", (100, 4, 0, 1, 2, 0, 16, "data", 0)),
    ("ClearEventCBufferOverride", (100, 4, 0, 0)),
    ("ClearEventCBufferOverrides", ()),
    ("SetSharedBufferPatch", (100, 0, "data")),
    ("ClearSharedBufferPatch", (100, 0, 4)),
    ("ResetSharedBufferRange", (100, 0, 4)),
    ("ClearSharedBufferPatches", ()),
    ("ClearReplayCache", ()),
    ("FreeTargetResource", (100,)),
])
def test_slow_shader_bindings_allow_python_heartbeat_and_restore_callback_gil(replay_gil_probe, method, arguments):
    controller = replay_gil_probe.ReplayController()
    stop = threading.Event()
    ready = threading.Event()
    def heartbeat():
        ready.set()
        while not stop.wait(0.001):
            controller.NotePythonProgress()
    worker = threading.Thread(target=heartbeat)
    worker.start()
    try:
        assert ready.wait(2)
        result = getattr(controller, method)(*arguments)
    finally:
        stop.set()
        worker.join(timeout=2)
    assert controller.GetProgress() > 0
    assert controller.GetNativeGIL() == 0 and controller.GetSameThread() is True
    assert controller.GetCallbackRefGIL() == 1 and controller.GetCallbackRestoredGIL() is True
    assert controller.GetReadGIL() == 1 and controller.GetCalls() == 1
    if method in ("GetShaderEntryPoints", "GetCBufferVariableContents", "PixelHistory",
                  "FetchCounters", "ContinueDebug"):
        assert len(result) == 1
        assert result[0].name == "MainPS" and result[0].stage == 4
    elif method == "GetShader":
        assert result.marker == 42
    elif method in ("BuildTargetShader", "SaveTexture", "GetPostVSData", "GetMinMax", "ComparePixelShaderPair"):
        assert result.resource == 99 and result.messages == "compiled"
    elif method == "DisassembleShader":
        assert result == "assembly"
    elif method in ("GetTextureData", "GetBufferData"):
        assert result == "bytes\0with-nul"
    elif method in ("DebugVertex", "DebugPixel"):
        assert result.marker == 91
        controller.FreeTrace(result)


def test_native_exception_scope_restores_gil_before_outer_handler(replay_gil_probe):
    assert replay_gil_probe.TestExceptionRestore() is True


@pytest.fixture(scope="module")
def qt_replay_heartbeat_probe(replay_gil_probe):
    """Use a real Qt GUI event loop, embedded Python and the generated production action."""
    if sys.platform != "win32":
        pytest.skip("This packaged Qt/Python integration regression currently requires Windows")
    qt = Path(os.environ.get("BREAD_TEST_QT_ROOT", r"C:\Qt\5.15.2\msvc2019_64"))
    if not (qt / "lib/Qt5Widgets.lib").is_file():
        pytest.skip("Qt 5 development libraries are required for the GUI heartbeat regression")
    scratch = replay_gil_probe._probe_scratch
    source = scratch / "qt_heartbeat.cpp"
    source.write_text(r"""
#include <Python.h>
#include <QApplication>
#include <QElapsedTimer>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QTimer>
#include "Windows/AI/AIGuiHeartbeat.h"
#include <atomic>
#include <iostream>
#include <thread>

extern "C" PyObject *PyInit__replay_gil_probe();
extern "C" bool ProbeNativeActive();

int main(int argc, char **argv)
{
  QApplication app(argc, argv);
  AIGuiHeartbeat gapPolicy;
  const bool gapPolicyOK = gapPolicy.sample(250, false) == 0 &&
      gapPolicy.sample(500, true) == 0 && gapPolicy.sample(2250, true) == 1750 &&
      gapPolicy.sample(4250, true) == 0 && gapPolicy.sample(14750, true) == 10500 &&
      gapPolicy.sample(16750, false) == 0 && gapPolicy.sample(26750, false) == 0 &&
      gapPolicy.sample(27000, true) == 0 && gapPolicy.sample(29000, false) == 2000;
  PyImport_AppendInittab("_replay_gil_probe", &PyInit__replay_gil_probe);
  Py_Initialize();
  if(PyRun_SimpleString("import _replay_gil_probe as probe") != 0) return 2;
  PyThreadState *mainState = PyEval_SaveThread();
  const bool released = argc > 1 && std::string(argv[1]) == "released";
  std::atomic<bool> done{false};
  std::atomic<int> pythonError{0};
  QElapsedTimer clock;
  clock.start();
  qint64 last = 0, maximumGap = 0;
  int heartbeats = 0, pythonCallbacksDuringNative = 0, elapsedUpdatesDuringNative = 0;
  QLabel elapsed;
  QTimer heartbeat, pythonBridgeTimer, elapsedTimer;
  heartbeat.setTimerType(Qt::PreciseTimer);
  heartbeat.setInterval(10);
  QObject::connect(&heartbeat, &QTimer::timeout, &app, [&]() {
    const qint64 now = clock.elapsed();
    maximumGap = qMax(maximumGap, now - last);
    last = now;
    ++heartbeats;
    if(done.load()) app.quit();
  });
  // The bridge's Python Qt timer must acquire the GIL before it can run its body. A native
  // replay method which retains it stalls this GUI callback and therefore every native timer.
  pythonBridgeTimer.setInterval(20);
  QObject::connect(&pythonBridgeTimer, &QTimer::timeout, &app, [&]() {
    if(!ProbeNativeActive()) return;
    PyGILState_STATE gil = PyGILState_Ensure();
    if(ProbeNativeActive()) ++pythonCallbacksDuringNative;
    PyGILState_Release(gil);
  });
  elapsedTimer.setInterval(100);
  QObject::connect(&elapsedTimer, &QTimer::timeout, &app, [&]() {
    elapsed.setText(QString::number(clock.elapsed()));
    if(ProbeNativeActive()) ++elapsedUpdatesDuringNative;
  });
  heartbeat.start();
  pythonBridgeTimer.start();
  elapsedTimer.start();
  std::thread worker([&]() {
    PyGILState_STATE gil = PyGILState_Ensure();
    const char *script = released
        ? "controller = probe.ReplayController(); entries = controller.GetShaderEntryPoints(1200); assert entries[0].name == 'MainPS'"
        : "controller = probe.ReplayController(); entries = controller.GetShaderEntryPointsUnreleased(1200); assert entries[0].name == 'MainPS'";
    pythonError.store(PyRun_SimpleString(script));
    PyGILState_Release(gil);
    done.store(true);
  });
  QTimer::singleShot(5000, &app, &QApplication::quit);
  app.exec();
  worker.join();
  PyEval_RestoreThread(mainState);
  Py_Finalize();
  QJsonObject result;
  result["maxGapMs"] = double(maximumGap);
  result["heartbeats"] = heartbeats;
  result["pythonCallbacksDuringNative"] = pythonCallbacksDuringNative;
  result["elapsedUpdatesDuringNative"] = elapsedUpdatesDuringNative;
  result["pythonError"] = pythonError.load();
  result["gapPolicyOK"] = gapPolicyOK;
  std::cout << QJsonDocument(result).toJson(QJsonDocument::Compact).constData() << std::endl;
  return 0;
}
""", encoding="utf-8")
    compiler = replay_gil_probe._probe_compiler
    objects = compiler.compile(
        [str(source)], output_dir=str(scratch),
        include_dirs=[sysconfig.get_path("include"), str(qt / "include"),
                      str(qt / "include/QtCore"), str(qt / "include/QtGui"),
                      str(qt / "include/QtWidgets"), str(ROOT / "qrenderdoc")],
        extra_postargs=["/EHsc", "/std:c++14"],
    )
    executable = scratch / "qt_replay_heartbeat.exe"
    compiler.link_executable(
        objects + replay_gil_probe._probe_objects, str(executable),
        library_dirs=[str(Path(sys.base_prefix) / "libs"), str(qt / "lib")],
        libraries=["Qt5Widgets", "Qt5Gui", "Qt5Core"],
        extra_postargs=["/SUBSYSTEM:CONSOLE"], target_lang="c++",
    )
    environment = dict(os.environ)
    environment.update({
        "PATH": str(qt / "bin") + os.pathsep + sys.base_prefix + os.pathsep + environment.get("PATH", ""),
        "QT_PLUGIN_PATH": str(qt / "plugins"), "QT_QPA_PLATFORM": "offscreen",
        "PYTHONHOME": sys.base_prefix, "PYTHONNOUSERSITE": "1",
    })
    return executable, environment


def test_context_entry_point_binding_keeps_qt_heartbeat_and_elapsed_running(qt_replay_heartbeat_probe):
    executable, environment = qt_replay_heartbeat_probe
    evidence = {}
    for mode in ("unreleased", "released"):
        process = subprocess.run([str(executable), mode], env=environment, capture_output=True,
                                 text=True, timeout=12, check=True)
        evidence[mode] = json.loads(process.stdout.strip().splitlines()[-1])
        assert evidence[mode]["pythonError"] == 0
        assert evidence[mode]["gapPolicyOK"] is True
    # A negative control makes the regression prove GUI starvation, rather than merely proving
    # that a separate Python heartbeat thread eventually runs after native work has completed.
    assert evidence["unreleased"]["maxGapMs"] >= 900, evidence
    assert evidence["unreleased"]["pythonCallbacksDuringNative"] == 0, evidence
    assert evidence["released"]["maxGapMs"] < 300, evidence
    assert evidence["released"]["pythonCallbacksDuringNative"] >= 5, evidence
    assert evidence["released"]["elapsedUpdatesDuringNative"] >= 5, evidence
    print("Qt replay heartbeat:", json.dumps(evidence, sort_keys=True))
