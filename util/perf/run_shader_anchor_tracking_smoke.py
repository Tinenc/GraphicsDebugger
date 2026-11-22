"""Launch the editor-only fixture with a fresh offscreen profile."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("--app", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--library-path", default="")
parser.add_argument("--fixture", default="")
parser.add_argument("--gdb", action="store_true")
args = parser.parse_args()
run = Path(tempfile.mkdtemp(prefix="bread-editor-anchors-"))
for directory in ("config", "local", "roaming", "temp"):
    (run / directory).mkdir()
(run / "config" / "UI.config").write_text(json.dumps({"rdocConfigData": 1,
    "Analytics_TotalOptOut": True, "CheckUpdate_AllowChecks": False,
    "Tips_HasSeenFirst": True, "AlwaysLoad_Extensions": []}))
env = {key: value for key, value in os.environ.items()
       if not key.startswith(("REPLAYLAB_", "RENDERDOC_", "PYTHON", "QT_", "QML"))}
env.update(RENDERDOC_CONFIG_DIR=str(run / "config"), XDG_CONFIG_HOME=str(run / "config"),
    XDG_CACHE_HOME=str(run / "cache"), LOCALAPPDATA=str(run / "local"),
    APPDATA=str(run / "roaming"), TEMP=str(run / "temp"), TMP=str(run / "temp"), PYTHONNOUSERSITE="1",
    PYTHONDONTWRITEBYTECODE="1", BREAD_SHADER_ANCHOR_SMOKE_OUTPUT=str(Path(args.output).resolve()))
if os.name != "nt":
    env["QT_QPA_PLATFORM"] = "offscreen"
if args.library_path:
    env["LD_LIBRARY_PATH"] = args.library_path
fixture = (Path(args.fixture).resolve() if args.fixture else
           Path(__file__).resolve().with_name("shader_anchor_tracking_smoke.py"))
env["BREAD_SHADER_ANCHOR_SOURCE_ROOT"] = str(Path(__file__).resolve().parents[2])
with (run / "stdout.log").open("w") as out, (run / "stderr.log").open("w") as err:
    startup = None
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
    command = [str(Path(args.app).resolve()), "--ui-script", str(fixture)]
    if args.gdb:
        command = ["gdb", "--batch", "-ex", "run", "-ex", "thread apply all bt", "--args"] + command
    process = subprocess.Popen(command,
        cwd=str(run), env=env, stdout=out, stderr=err,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        startupinfo=startup)
    print(json.dumps({"run": str(run), "pid": process.pid}), flush=True)
    try:
        code = process.wait(timeout=90)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
print(json.dumps({"run": str(run), "pid": process.pid, "exitCode": code}))
output = Path(args.output)
report = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
print(json.dumps({"ok": report.get("ok"), "checks": len(report.get("checks", [])), "error": report.get("error")}))
raise SystemExit(0 if code == 0 and report.get("ok") else 1)
