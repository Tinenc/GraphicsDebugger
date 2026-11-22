"""Launch an isolated offscreen native UI with a deterministic diagnostic child."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("--app", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--debug", action="store_true")
parser.add_argument("--close-parent", action="store_true")
parser.add_argument("--fail-ai-host", action="store_true", help="Reject all AI profile commands in the child")
parser.add_argument("--ui-script", type=Path)
parser.add_argument("--timeout-seconds", type=int, default=60)
args = parser.parse_args()
if args.ui_script is not None:
    args.ui_script = args.ui_script.resolve(strict=True)
repo = Path(__file__).resolve().parents[2]
run = Path(tempfile.mkdtemp(prefix="bread-connection-ui-"))
for directory in ("config", "local", "roaming", "user"):
    (run / directory).mkdir()
source = run / "fake.cs"
source.write_text(r'''using System; using System.IO; using System.Threading;
class Fake {
 [System.Runtime.InteropServices.DllImport("kernel32.dll")] static extern IntPtr GetConsoleWindow();
 static void Main(string[] args) {
  if(Environment.GetEnvironmentVariable("REPLAYLAB_START_GATED")=="1" && Console.ReadLine()!="start") return;
  string command = String.Join(" ",args);
  if(command.Contains("project-request")) {
   Console.ReadLine();
   string body="{\"consoleWindow\":"+GetConsoleWindow().ToInt64().ToString()+"}";
   Console.WriteLine("{\"status\":200,\"body\":\""+Convert.ToBase64String(System.Text.Encoding.UTF8.GetBytes(body))+"\"}"); return;
  }
  File.AppendAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"commands.txt"), command+"\n");
  if(command.Contains("doctor")) {
   File.WriteAllText(Environment.GetEnvironmentVariable("BREAD_DIAG_FAKE_PID_FILE"), System.Diagnostics.Process.GetCurrentProcess().Id.ToString());
   Console.WriteLine("{\"type\":\"check\",\"index\":0,\"check\":{\"name\":\"first-probe\",\"status\":\"ok\",\"detail\":\"First result\"}}");
   Console.Out.Flush(); Thread.Sleep(1200);
   Console.WriteLine("{\"type\":\"check\",\"index\":1,\"check\":{\"name\":\"second-probe\",\"status\":\"running\",\"detail\":\"Still running\"}}");
   Console.Out.Flush(); Thread.Sleep(60000); return;
  }
  if(command.Contains("profile list") || command.Contains("profile presets")) {
   Console.WriteLine("[{\"id\":\"smoke\",\"preset\":\"openai\",\"kind\":\"model\",\"active\":true,\"title\":\"Isolated test\",\"protocol\":\"openai-compatible\",\"model\":\"test\",\"defaultModel\":\"test\",\"models\":[\"test\"]}]"); return;
  }
  Console.WriteLine("{}");
 }
}''', encoding="utf-8")
if args.fail_ai_host:
    source.write_text(source.read_text(encoding="utf-8").replace(
        'if(command.Contains("doctor"))',
        'if(command.StartsWith("profile ")) { Environment.Exit(2); return; }\n  if(command.Contains("doctor"))'), encoding="utf-8")
compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
fake = run / "fake.exe"
subprocess.run([str(compiler), "/nologo", "/target:exe", "/out:"+str(fake), str(source)], check=True,
               creationflags=subprocess.CREATE_NO_WINDOW)
(run / "config/UI.config").write_text(json.dumps({"rdocConfigData": 1, "UILanguage": "en_US", "Analytics_TotalOptOut": True,
    "CheckUpdate_AllowChecks": False, "Tips_HasSeenFirst": True}))
env = {k: v for k, v in os.environ.items() if not k.startswith(("REPLAYLAB_", "RENDERDOC_", "BREAD_", "PYTHON", "QT_"))}
env.update(RENDERDOC_CONFIG_DIR=str(run / "config"),
    LOCALAPPDATA=str(run / "local"), APPDATA=str(run / "roaming"), USERPROFILE=str(run / "user"),
    REPLAYLAB_AI_CLI=str(fake), REPLAYLAB_CONFIG_DIR=str(run / "config/agent"),
    REPLAYLAB_INTERNAL_RUNTIME_DIR=str(run / "internal"), REPLAYLAB_RUNTIME_DIR=str(run / "mcp"),
    BREAD_DIAG_UI_REPORT=str(run / "report.json"), BREAD_DIAG_UI_IMAGE=str(run / "ui.png"),
    BREAD_DIAG_FAKE_PID_FILE=str(run / "child.pid"))
env["BREAD_DIAG_SOURCE_ROOT"] = str(repo)
env["BREAD_DIAG_PROJECT_HELPER"] = str(fake)
if args.close_parent: env["BREAD_DIAG_CLOSE_PARENT"] = "1"
# The release uses Qt 5.9, while the build machine's offscreen plugin is 5.15.
# Run the real Windows plugin on a private desktop: no windows/focus on the
# user's desktop, and no dependency on a different Qt build's platform plugin.
import ctypes
from ctypes import wintypes as wt
class Startup(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("reserved", wt.LPWSTR), ("desktop", wt.LPWSTR), ("title", wt.LPWSTR),
        ("x", wt.DWORD), ("y", wt.DWORD), ("sx", wt.DWORD), ("sy", wt.DWORD), ("cx", wt.DWORD),
        ("cy", wt.DWORD), ("fill", wt.DWORD), ("flags", wt.DWORD), ("show", wt.WORD),
        ("reserved2size", wt.WORD), ("reserved2", ctypes.c_void_p), ("stdin", wt.HANDLE),
        ("stdout", wt.HANDLE), ("stderr", wt.HANDLE)]
class Process(ctypes.Structure):
    _fields_ = [("process", wt.HANDLE), ("thread", wt.HANDLE), ("pid", wt.DWORD), ("tid", wt.DWORD)]
user, kernel = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
user.CreateDesktopW.restype = wt.HANDLE
user.CreateDesktopW.argtypes = [wt.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p]
user.CloseDesktop.argtypes = [wt.HANDLE]
kernel.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL,
    wt.DWORD, ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(Startup), ctypes.POINTER(Process)]
kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
kernel.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
kernel.CloseHandle.argtypes = [wt.HANDLE]
desktop_name = "BreadDiagnostics" + str(os.getpid())
desktop = user.CreateDesktopW(desktop_name, None, None, 0, 0x01ff, None)
if not desktop: raise ctypes.WinError(ctypes.get_last_error())
startup = Startup(); startup.cb = ctypes.sizeof(Startup); startup.desktop = desktop_name
process = Process()
command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(args.app.resolve()), "--ui-script",
    str(args.ui_script.resolve() if args.ui_script else repo / "replaylab/tests/fixtures/qrenderdoc_connection_check_smoke.py")]))
environment = ctypes.create_unicode_buffer("\0".join(k+"="+v for k,v in sorted(env.items()))+"\0\0")
try:
    if not kernel.CreateProcessW(str(args.app.resolve()), command, None, None, False,
            0x08000400 | (2 if args.debug else 0), environment, str(run), ctypes.byref(startup), ctypes.byref(process)):
        raise ctypes.WinError(ctypes.get_last_error())
    if args.debug:
        import struct, time
        class DebugEvent(ctypes.Structure):
            _fields_ = [("code",wt.DWORD),("pid",wt.DWORD),("tid",wt.DWORD),("data",ctypes.c_ubyte*160)]
        # Win64 DEBUG_EVENT's union is pointer aligned at offset 16.
        class AlignedDebugEvent(ctypes.Structure):
            _fields_ = [("code",wt.DWORD),("pid",wt.DWORD),("tid",wt.DWORD),("alignment",wt.DWORD),("data",ctypes.c_ubyte*160)]
        kernel.WaitForDebugEvent.argtypes = [ctypes.POINTER(AlignedDebugEvent), wt.DWORD]
        kernel.OpenThread.argtypes = [wt.DWORD,wt.BOOL,wt.DWORD]; kernel.OpenThread.restype = wt.HANDLE
        kernel.GetThreadContext.argtypes = [wt.HANDLE,ctypes.c_void_p]
        kernel.ReadProcessMemory.argtypes = [wt.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p]
        dbg = ctypes.WinDLL("dbghelp", use_last_error=True)
        dbg.SymInitialize.argtypes = [wt.HANDLE,wt.LPCSTR,wt.BOOL]
        dbg.SymFromAddr.argtypes = [wt.HANDLE,ctypes.c_ulonglong,ctypes.c_void_p,ctypes.c_void_p]
        dbg.SymInitialize(process.process, str(args.app.resolve().parent).encode(), True)
        deadline = time.monotonic()+55
        while time.monotonic() < deadline:
            event = AlignedDebugEvent()
            if not kernel.WaitForDebugEvent(ctypes.byref(event),1000): continue
            status = 0x10002
            if event.code == 1:
                data = bytes(event.data)
                exception, first = struct.unpack_from("I",data)[0], struct.unpack_from("I",data,152)[0]
                if exception != 0x80000003: status = 0x80010001
                if not first:
                    thread = kernel.OpenThread(0x0048,False,event.tid)
                    context = ctypes.create_string_buffer(1232)
                    struct.pack_into("I",context,48,0x100003)
                    kernel.GetThreadContext(thread,context)
                    rip,rsp = struct.unpack_from("Q",context,248)[0],struct.unpack_from("Q",context,152)[0]
                    stack = ctypes.create_string_buffer(4096)
                    kernel.ReadProcessMemory(process.process,rsp,stack,len(stack),None)
                    resolved = []
                    dbg.SymCleanup.argtypes = [wt.HANDLE]
                    dbg.SymCleanup(process.process)
                    initialized = dbg.SymInitialize(process.process, str(args.app.resolve().parent).encode(), True)
                    dbg.SymRefreshModuleList.argtypes = [wt.HANDLE]
                    refresh = dbg.SymRefreshModuleList(process.process)
                    resolved.append("rip="+hex(rip)+" rsp="+hex(rsp)+" refresh="+str(refresh)+" init="+str(initialized)+" error="+str(ctypes.get_last_error())+" exception="+hex(struct.unpack_from("Q",data,16)[0]))
                    for addr in [rip]+list(struct.unpack("512Q",stack.raw)):
                        symbol = ctypes.create_string_buffer(2048)
                        struct.pack_into("I",symbol,0,88); struct.pack_into("I",symbol,80,1900)
                        if dbg.SymFromAddr(process.process,addr,None,symbol):
                            resolved.append(hex(addr)+" "+symbol.raw[84:].split(b"\0")[0].decode("utf-8","replace"))
                    (run / "crash-stack.txt").write_text(hex(exception)+"\n"+"\n".join(resolved),encoding="utf-8")
                    kernel.CloseHandle(thread)
                    kernel.TerminateProcess(process.process,1)
            kernel.ContinueDebugEvent(event.pid,event.tid,status)
            if event.code == 5: break
    if kernel.WaitForSingleObject(process.process, max(5, min(args.timeout_seconds, 300)) * 1000) == 258:
        kernel.TerminateProcess(process.process, 1)
        kernel.WaitForSingleObject(process.process, 3000)
    exit_code = wt.DWORD()
    kernel.GetExitCodeProcess(process.process, ctypes.byref(exit_code)); code = exit_code.value
finally:
    if process.thread: kernel.CloseHandle(process.thread)
    if process.process: kernel.CloseHandle(process.process)
    user.CloseDesktop(desktop)
report = json.loads((run / "report.json").read_text(encoding="utf-8")) if (run / "report.json").is_file() else {}
pidfile = run / "child.pid"
if pidfile.is_file():
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    child = kernel.OpenProcess(0x1000, False, int(pidfile.read_text()))
    if child:
        state = wt.DWORD()
        kernel.GetExitCodeProcess(child, ctypes.byref(state))
        kernel.CloseHandle(child)
        report["diagnosticChildReaped"] = state.value != 259
    else:
        report["diagnosticChildReaped"] = True
    report["ok"] = report.get("ok", False) and report["diagnosticChildReaped"]
report.update(scratch=str(run), exitCode=code)
if args.fail_ai_host:
    commands = (run / "commands.txt").read_text(encoding="utf-8").splitlines() if (run / "commands.txt").exists() else []
    report["hostCommands"] = commands
    report["configurationNeverStartedHost"] = not any(command.startswith("profile ") for command in commands)
    report["ok"] = report.get("ok", False) and report["configurationNeverStartedHost"]
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
if (run / "ui.png").exists(): shutil.copy2(run / "ui.png", args.output.with_suffix(".png"))
print(json.dumps(report))
raise SystemExit(0 if report.get("ok") and code == 0 else 1)
