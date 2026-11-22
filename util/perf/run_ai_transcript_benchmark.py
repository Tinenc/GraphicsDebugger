from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
qt = Path("C:/Qt/5.15.2/msvc2019_64")
locator = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft Visual Studio/Installer/vswhere.exe"
installation = Path(subprocess.check_output([str(locator), "-latest", "-products", "*", "-requires", "Microsoft.Component.MSBuild", "-property", "installationPath"], text=True).strip())
vc = installation / "VC/Tools/MSVC"
vc = max(vc.iterdir(), key=lambda p: p.name)
sdk = Path(os.environ["ProgramFiles(x86)"]) / "Windows Kits/10"
version = max((sdk / "Include").iterdir(), key=lambda p: tuple(int(x) for x in p.name.split('.')))
env = os.environ.copy()
env["INCLUDE"] = ';'.join(map(str,[vc/"include",*[version/name for name in ("ucrt","shared","um","winrt")],qt/"include",qt/"include/QtCore",qt/"include/QtGui",qt/"include/QtWidgets",root/"qrenderdoc"]))
env["LIB"] = ';'.join(map(str,[vc/"lib/x64",sdk/"Lib"/version.name/"ucrt/x64",sdk/"Lib"/version.name/"um/x64",qt/"lib"]))
env["PATH"] = str(qt/"bin")+';'+str(root/"x64/Development")+';'+env["PATH"]
env["QT_QPA_PLATFORM"] = "offscreen"
env["QT_PLUGIN_PATH"] = str(qt/"plugins")
with tempfile.TemporaryDirectory(prefix="bread-ai-view-") as scratch:
    scratch=Path(scratch)
    if "--expect-paging" not in sys.argv:
        header=scratch/"Windows/AI/AIConversationView.h";header.parent.mkdir(parents=True)
        header.write_text((root/"qrenderdoc/Windows/AI/AIConversationView.h").read_text().replace("  QHash<QString, int> m_ActivityHistoryLimits;\n", ""),encoding="utf-8")
        env["INCLUDE"]=str(scratch)+';'+env["INCLUDE"]
    args=[str(vc/"bin/Hostx64/x64/cl.exe"),"/nologo","/std:c++17","/MD","/EHsc","/Zc:__cplusplus","/DQT_NO_DEBUG","/DWIN32","/DNOMINMAX",
        str(root/"util/perf/ai_transcript_benchmark.cpp"),*[str(root/"x64/Development/obj/qrenderdoc"/(name+".obj")) for name in ("AIConversationView","AIFileThumbnail","Resources","precompiled")],
        "/Fo"+str(scratch/"benchmark.obj"),"/Fe"+str(scratch/"benchmark.exe"),"/link","Qt5Core.lib","Qt5Gui.lib","Qt5Widgets.lib", "gdi32.lib","user32.lib","ole32.lib","propsys.lib","shell32.lib","windowscodecs.lib","uuid.lib",str(root/"x64/Development/renderdoc.lib")]
    result=subprocess.run(args,env=env,cwd=scratch,capture_output=True,text=True,errors="replace")
    if result.returncode:
        print(result.stdout+result.stderr);raise SystemExit(result.returncode)
    result=subprocess.run([str(scratch/"benchmark.exe"),*sys.argv[1:]],env=env,cwd=scratch,capture_output=True,text=True,errors="replace",timeout=120)
    print(result.stdout.strip())
    if result.returncode:
        print(json.dumps({"exitCode":result.returncode,"stderr":result.stderr}));raise SystemExit(result.returncode)
    label="after" if "--expect-paging" in sys.argv else "before"
    output=root/("output/bread-workflow-repair-20261031/ui-performance-"+label+".json")
    output.write_text(result.stdout.strip(),encoding="utf-8")
