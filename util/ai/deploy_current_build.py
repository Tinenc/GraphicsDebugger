"""Deploy verified Bread code to a closed installation; preserve user data/tools."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO / "replaylab/packaging"))
from runtime_inputs import fingerprint
from package_portable import copy_tree, release_files


def digest(path):
    with path.open("rb") as stream: return hashlib.file_digest(stream,"sha256").hexdigest()


def child(root,relative):
    path=(root/relative).resolve()
    if path==root or not path.is_relative_to(root): raise ValueError("Deployment escaped its root")
    cursor=path
    while cursor!=root:
        if cursor.is_symlink() or cursor.is_junction(): raise ValueError("Redirected deployment path")
        cursor=cursor.parent
    return path


def capabilities_target(app, source, version):
    """Reuse a byte-verified immutable runtime instead of duplicating each patch."""
    from prepare_capabilities_bundle import verify
    keys = ("files", "physicalFiles", "componentArchives", "features", "distributionProfile")
    expected = verify(source)
    distribution = app / "replaylab/bread-distribution.json"
    if distribution.is_file():
        try:
            current = json.loads(distribution.read_text(encoding="utf-8"))["capabilitiesDirectory"]
            candidate = child(app / "replaylab", current)
            value = verify(candidate)
            if all(value.get(key) == expected.get(key) for key in keys):
                return current, candidate, True
        except (KeyError, OSError, ValueError):
            pass  # Preserve the old tree; stage a separately verified replacement.
    relative = "tools/capabilities-" + version
    destination = child(app / "replaylab", relative)
    if destination.exists():
        value = verify(destination)
        if any(value.get(key) != expected.get(key) for key in keys):
            raise ValueError("Existing versioned capabilities differ; preserve them and use a new version")
        return relative, destination, True
    return relative, destination, False


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("application","runtime","gui","receipt"):
        parser.add_argument("--"+name,type=Path,required=True)
    parser.add_argument("--capabilities", type=Path)
    parser.add_argument("--backup-root", type=Path, default=REPO / "output/bread-update-backups")
    parser.add_argument("--replay-bindings", type=Path)
    parser.add_argument("--replay-core", type=Path)
    parser.add_argument("--native-directory", type=Path,
        help="Matching rebuilt core, injection helpers and both Python bindings")
    parser.add_argument("--minimal", action="store_true", help="Explicitly deploy only five core skills")
    args=parser.parse_args()
    native_files = {}
    if args.native_directory:
        if args.replay_core or args.replay_bindings:
            raise ValueError("Use a complete native directory or individual legacy overrides")
        native = args.native_directory.resolve(strict=True)
        for relative in ("renderdoc.dll", "system_load.dll", "render_soco_doccmd.exe",
                         "render_soco_docshim64.dll", "pymodules/renderdoc.pyd", "pymodules/qrenderdoc.pyd"):
            source = child(native, relative)
            if not source.is_file():
                raise ValueError("Incomplete matching native build: " + relative)
            native_files[relative] = source
    if args.replay_core and not args.replay_bindings:
        raise ValueError("A changed replay core requires matching Python bindings")
    if not args.capabilities and not args.minimal:
        package_config = json.loads((REPO / "replaylab/packaging/RenderDocBread.package.json").read_text())
        from restore_dependencies import standard_capabilities
        args.capabilities = standard_capabilities(package_config)
    app,runtime,gui=args.application.resolve(),args.runtime.resolve(),args.gui.resolve()
    if app.is_symlink() or app.is_junction() or not (app/"RenderDocBread.exe").is_file():
        raise ValueError("Use an existing regular Bread installation")
    # Existing shortcuts may still target the fork's former executable name.
    # Keep those entry points current instead of leaving a second, obsolete UI.
    legacy_launchers = [name for name in ("q_render_soco_doc.exe", "qrenderdoc.exe")
                        if child(app, name).is_file()]
    # Process names/PIDs only: vendor command lines may contain credentials.
    command="Get-Process | Where-Object { @('RenderDocBread','qrenderdoc','q_render_soco_doc','replaylab-ai','BreadMCP','ReplayLabInternal') -contains $_.ProcessName } | Select-Object Id,Path | ConvertTo-Json -Compress"
    running=subprocess.run(["powershell.exe","-NoProfile","-Command",command],capture_output=True,text=True,
        creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),check=True)
    rows=json.loads(running.stdout or "[]");rows=rows if isinstance(rows,list) else [rows]
    if any(row.get("Path") and Path(row["Path"]).resolve().is_relative_to(app) for row in rows):
        raise ValueError("Close this Bread installation and its tasks before deployment")
    manifest=json.loads((runtime/"replaylab-runtime-manifest.json").read_text(encoding="utf-8-sig"))
    if manifest["sourceInputsSha256"]!=fingerprint(): raise ValueError("Build current sources before deployment")
    for record in manifest["files"]:
        source=child(runtime,record["path"])
        if source.stat().st_size!=record["size"] or digest(source)!=record["sha256"]:
            raise ValueError("Frozen runtime payload differs")
    backup_root = args.backup_root.resolve()
    if backup_root.is_relative_to(app):
        raise ValueError("Deployment backups must stay outside the application")
    backup=backup_root/(manifest["version"]+"-"+datetime.now().strftime("%Y%m%d-%H%M%S"))
    backup.mkdir(parents=True,exist_ok=False)
    scopes={"replaylab/renderdoc_mcp_bridge":"integrations/replaylab/python/renderdoc_mcp_bridge",
        ".agents/skills":"replaylab/skills",
        "replaylab/unity":"replaylab/unity"}
    selected_profile = "core"
    if args.capabilities:
        selected_profile = json.loads((args.capabilities / "bread-capabilities.json").read_text())["distributionProfile"]
    if args.capabilities and selected_profile != "rdc":
        scopes["replaylab/portable_skills"] = "replaylab/portable-skills"
    preserve=["RenderDocBread.exe","RenderDocBread.portable","replaylab/bread-distribution.json","replaylab/.replaylab-runtime-deployment.json",
              "replaylab/replaylab-runtime-manifest.json"]+["replaylab/"+r["path"] for r in manifest["files"]]
    preserve.extend(legacy_launchers)
    preserve.extend(native_files)
    if args.replay_bindings:
        preserve.append("pymodules/renderdoc.pyd")
    if args.replay_core:
        preserve.append("renderdoc.dll")
    for prefix in scopes.values():
        target=child(app,prefix)
        if target.is_dir(): preserve.extend(p.relative_to(app).as_posix() for p in target.rglob("*") if p.is_file())
    for relative in sorted(set(preserve)):
        target=child(app,relative)
        if target.is_file():
            destination=child(backup,relative);destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,destination)
    receipt={"version":manifest["version"],"application":str(app),"backup":str(backup),"state":"prepared",
        "preservedUserData":True,"retainedInstalledTools":True,"writtenFiles":[]}
    if args.receipt.is_file():
        previous=json.loads(args.receipt.read_text(encoding="utf-8"))
        if previous.get("application")==str(app) and previous.get("version")==manifest["version"]:
            receipt["initialBackup"]=previous.get("initialBackup",previous.get("backup"))
    def save(): args.receipt.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding="utf-8")
    save()
    capabilities_directory = None
    if args.capabilities:
        from prepare_capabilities_bundle import stage as stage_capabilities
        capabilities_directory, destination, reused = capabilities_target(app, args.capabilities.resolve(), manifest["version"])
        receipt["reusedCapabilities"] = reused
        if not reused:
            stage_capabilities(args.capabilities.resolve(), destination)
    def powershell(script,*parameters):
        result=subprocess.run(["powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-File",str(REPO/script),*map(str,parameters)],
            capture_output=True,text=True,encoding="utf-8",errors="replace",creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if result.returncode: raise RuntimeError(result.stdout[-1600:]+result.stderr[-1600:])
    powershell(Path("replaylab/packaging/Deploy-ReplayLabRuntime.ps1"),"-ApplicationDirectory",app,"-RuntimeOutputDirectory",runtime)
    powershell(Path("util/i18n/deploy_replaylab_integration.ps1"),"-SourceRoot",REPO/"replaylab/renderdoc_mcp_bridge",
               "-TargetRoot",app/"integrations/replaylab/python/renderdoc_mcp_bridge")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="bread-deploy-code-") as temporary:
        stage=Path(temporary)
        for source,target in scopes.items():
            if source!="replaylab/renderdoc_mcp_bridge": copy_tree(REPO/source,stage/target)
        shutil.copy2(gui,stage/"RenderDocBread.exe")
        for relative, source in native_files.items():
            target = child(stage, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        if args.replay_core:
            shutil.copy2(args.replay_core.resolve(strict=True), stage / "renderdoc.dll")
        if args.replay_bindings:
            (stage / "pymodules").mkdir(parents=True, exist_ok=True)
            shutil.copy2(args.replay_bindings.resolve(strict=True), stage / "pymodules/renderdoc.pyd")
        for name in legacy_launchers:
            shutil.copy2(gui, stage/name)
        (stage/"RenderDocBread.portable").write_text("renderdocbread-portable/v1\n",encoding="ascii")
        distribution = {"schema":"bread-distribution/v1","profile":selected_profile}
        if capabilities_directory:
            distribution["capabilitiesDirectory"] = capabilities_directory
        (stage/"replaylab/bread-distribution.json").write_text(json.dumps(distribution,indent=2)+"\n",encoding="utf-8")
        for source in release_files(stage):
            relative=source.relative_to(stage).as_posix();target=child(app,relative)
            expected=digest(source)
            if not target.is_file() or digest(target)!=expected:
                target.parent.mkdir(parents=True,exist_ok=True)
                pending=target.with_name(target.name+".update-"+uuid.uuid4().hex)
                shutil.copy2(source,pending);os.replace(pending,target)
            if digest(target)!=expected: raise ValueError("Deployed code verification failed: "+relative)
            receipt["writtenFiles"].append({"path":relative,"sha256":expected})
    receipt["state"]="complete";save()
    print(json.dumps({k:v for k,v in receipt.items() if k!="writtenFiles"}))


if __name__=="__main__": main()
