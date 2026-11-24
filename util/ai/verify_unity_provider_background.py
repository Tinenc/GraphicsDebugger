"""Compile Bread's staged provider adaptations without starting or editing Unity."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo / "replaylab/mcp_host"))
from src.unity_integration import UnityIntegrationService, PACKAGE

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--project", type=Path, default=Path("D:/Study/Project/UnityTest"))
parser.add_argument("--editor-data", type=Path, default=Path("D:/Program Files/Unity/2022.3.21f1/Editor/Data"))
parser.add_argument("--provider", type=Path, default=repo / "x64/Development/replaylab/tools/unity-mcp/MCPForUnity")
parser.add_argument("--output", type=Path, default=repo / "output/bread-restore-background-20261102/provider-compile.json")
args = parser.parse_args()
reference = next((args.project / "Library/Bee").rglob("MCPForUnity.Editor.rsp"))
lines = reference.read_text(encoding="utf-8-sig").splitlines()
options = [line for line in lines if line.startswith(("-r:", "/r:", "-reference:", "/reference:",
    "-define:", "-unsafe", "-nostdlib", "-langversion:"))]
with tempfile.TemporaryDirectory(prefix="bread-background-provider-") as directory:
    temp = Path(directory)
    (temp / "Packages").mkdir()
    (temp / "Packages/manifest.json").write_text('{"dependencies":{}}', encoding="utf-8")
    UnityIntegrationService._install_package(temp, args.provider.resolve())
    package = temp / "Packages" / PACKAGE
    sources = []
    for line in lines:
        value = line.strip('"')
        if not value.endswith(".cs"):
            continue
        value = value.replace("\\", "/")
        if value.startswith("Packages/" + PACKAGE + "/"):
            sources.append(temp / value)
        else:
            sources.append(args.project / value)
    if not sources:
        raise RuntimeError("No provider compiler inputs were found")
    response = temp / "provider.rsp"
    response.write_text("\n".join(["-nologo", "-target:library", '-out:"' + str(temp / "MCPForUnity.Editor.dll") + '"',
        *options, *['"' + str(path) + '"' for path in sources]]), encoding="utf-8")
    completed = subprocess.run([str(args.editor_data / "NetCoreRuntime/dotnet.exe"),
        str(args.editor_data / "DotNetSdkRoslyn/csc.dll"), "@" + str(response)],
        cwd=args.project, capture_output=True, text=True, encoding="utf-8", errors="replace")
    result = {"projectReadOnly": True, "editorStarted": False, "sources": len(sources),
        "exitCode": completed.returncode, "diagnostics": (completed.stdout + completed.stderr).strip(),
        "sceneCaptureRequestsFocus": "sceneView.Focus();" in (package / "Editor/Helpers/EditorWindowScreenshotUtility.cs").read_text(),
        "gameCaptureOpensWindow": 'ExecuteMenuItem("Window/General/Game' in (package / "Editor/Tools/ManageScene.cs").read_text()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(completed.returncode or int(result["sceneCaptureRequestsFocus"] or result["gameCaptureOpensWindow"]))
