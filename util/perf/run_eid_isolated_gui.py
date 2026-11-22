"""Launch only this worktree's GUI with a fresh private user/runtime environment.

Uses a retained Popen handle for timeout cleanup; never searches for or stops another GUI.
The UI script owns capture opening and result generation. No global environment is changed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time


def contained_path(root, value):
    root = Path(os.path.abspath(str(root)))
    path = Path(os.path.abspath(str(value)))
    if path == root or root not in path.parents:
        raise ValueError("Path must be below the isolated worktree: " + str(path))
    for part in (path,) + tuple(path.parents):
        if part.exists() or part.is_symlink():
            attrs = getattr(part.lstat(), "st_file_attributes", 0)
            if part.is_symlink() or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
                raise ValueError("Runtime path contains a reparse point: " + str(part))
    return path


def file_hash(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def private_environment(inherited, root, run_dir, config_path):
    # Strip inherited project locations, service endpoints, profiler switches,
    # Python overrides and Qt plugin overrides. Retain installed system/driver paths.
    prefixes = ("REPLAYLAB_", "RENDERDOC_", "PYTHON", "QT_", "QML")
    result = {key: value for key, value in inherited.items()
              if not key.upper().startswith(prefixes)}
    run_bin = root / "x64" / "Development"
    result.update({
        "RENDERDOC_CONFIG_DIR": str(run_dir / "config"),
        "LOCALAPPDATA": str(run_dir / "local"),
        "APPDATA": str(run_dir / "roaming"),
        "TEMP": str(run_dir / "temp"), "TMP": str(run_dir / "temp"),
        "TMPDIR": str(run_dir / "temp"),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "REPLAYLAB_EXTERNAL_MCP_AUTOSTART": "0",
        "REPLAYLAB_MCP_HOST": str(run_bin / "replaylab" / "ReplayLabMCP.exe"),
        "REPLAYLAB_INTERNAL_HOST": str(run_bin / "replaylab" / "ReplayLabInternal.exe"),
        "RENDERDOC_PATH": str(run_bin), "RENDERDOC_MODULE_PATH": str(run_bin / "pymodules"),
        "RENDERDOC_EID_UI_TEST_CONFIG": str(config_path),
        "RENDERDOC_EID_TIMING": "1",
        "QT_PLUGIN_PATH": str(run_bin / "qtplugins"),
        "PATH": str(run_bin) + os.pathsep + result.get("PATH", ""),
    })
    return result


def normal_config_witness(environment):
    """Read only hashes of known normal configuration files, never their contents."""
    appdata = Path(environment["APPDATA"])
    paths = [appdata / app / "UI.config" for app in ("q_render_soco_doc", "qrenderdoc")]
    paths.append(appdata / "renderdoc" / "renderdoc.conf")
    return [{"path": str(path), "sha256": file_hash(path) if path.is_file() else None}
            for path in paths]


def checked_runtime(root):
    run_bin = root / "x64" / "Development"
    identity_path = contained_path(root, run_bin / "eid-isolated-runtime.json")
    identity = json.loads(identity_path.read_text(encoding="utf-8-sig"))
    if (Path(identity.get("worktree", "")) != root or not identity.get("configurationOverride")
            or not identity.get("guiBuilt")):
        raise ValueError("Build this GUI with build_eid_isolated.ps1 -IncludeGui first")
    build_path = contained_path(root, identity["buildReceipt"])
    build = json.loads(build_path.read_text(encoding="utf-8-sig"))
    if not build.get("success") or not build.get("sourceStable") or not build.get("guiBuilt"):
        raise ValueError("GUI build has no successful stable-source receipt")
    expected_exe = run_bin / "q_render_soco_doc.exe"
    found = False
    for entry in identity["binaries"]:
        path = contained_path(root, entry["path"])
        if file_hash(path).lower() != entry["sha256"].lower():
            raise ValueError("Binary differs from build receipt: " + str(path))
        found = found or path == expected_exe
    if not found:
        raise ValueError("GUI executable missing from build receipt")
    # Check the actual fixed first-party package, never user extensions.
    source = root / "replaylab" / "renderdoc_mcp_bridge"
    deployed = run_bin / "integrations" / "replaylab" / "python" / "renderdoc_mcp_bridge"
    integration = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix != ".py" and path.name != "integration.json":
            continue
        target = contained_path(root, deployed / path.relative_to(source))
        digest = file_hash(path)
        if not target.is_file() or file_hash(target) != digest:
            raise ValueError("Fixed integration differs from this worktree: " + str(target))
        integration.append({"path": str(target), "sha256": digest})
    if not integration:
        raise ValueError("No fixed first-party integration files")
    return expected_exe, identity, integration


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui-script", required=True)
    parser.add_argument("--script-config", required=True)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_label):
        raise ValueError("Run label may contain only letters, digits, hyphens and underscores")
    if not 10 <= args.timeout_seconds <= 3600:
        raise ValueError("Timeout must be between 10 and 3600 seconds")
    root = Path(__file__).resolve().parents[2]
    if not (root / "WORKTREE_ISOLATION.md").is_file():
        raise ValueError("Not an isolated worktree")
    script = contained_path(root, args.ui_script)
    config_source = contained_path(root, args.script_config)
    config = json.loads(config_source.read_text(encoding="utf-8-sig"))
    exe, identity, integration = checked_runtime(root)
    run_dir = contained_path(root, root / "output" / "gui-isolation" / args.run_label)
    if run_dir.exists():
        raise ValueError("Use a fresh run label; refusing to reuse GUI state")
    for relative in ("config", "local", "roaming", "temp"):
        (run_dir / relative).mkdir(parents=True)
    # --ui-script runs after the analytics first-run prompt. Opt this fresh test
    # profile out before launch; never inherit the user's preferences or projects.
    (run_dir / "config" / "UI.config").write_text(json.dumps({
        "rdocConfigData": 1, "Analytics_TotalOptOut": True,
        "CheckUpdate_AllowChecks": False, "Tips_HasSeenFirst": True, "AlwaysLoad_Extensions": [],
    }, indent=2), encoding="utf-8")
    # The output is always overridden to this fresh run; captures are read-only input.
    config["output"] = str(run_dir / "ui-result.json")
    config["isolationRoot"] = str(run_dir)
    config["worktree"] = str(root)
    config_path = run_dir / "script-config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    child_env = private_environment(os.environ, root, run_dir, config_path)
    receipt = {"schema": "eid-isolated-gui-run/v1", "worktree": str(root),
               "executable": str(exe), "buildIdentity": identity, "integration": integration,
               "script": str(script), "scriptSha256": file_hash(script), "runDirectory": str(run_dir),
               "otherProcessesTerminated": False, "exclusivePerformanceRunCertified": False,
               "scriptConfig": config, "startedUtcEpoch": time.time(), "success": False,
               "normalConfigBefore": normal_config_witness(os.environ)}
    receipt_path = run_dir / "runner-receipt.json"

    def save():
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    startup = None
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # background verification; never steal the user's main GUI
    with open(str(run_dir / "stdout.log"), "wb") as out, open(str(run_dir / "stderr.log"), "wb") as err:
        process = subprocess.Popen([str(exe), "--ui-script", str(script)], cwd=str(exe.parent),
                                   env=child_env, stdout=out, stderr=err, startupinfo=startup)
        receipt["pid"] = process.pid
        save()
        print("Isolated GUI PID {}: {}".format(process.pid, run_dir), flush=True)
        try:
            process.wait(timeout=args.timeout_seconds)
        except subprocess.TimeoutExpired:
            receipt["timedOut"] = True
            # Popen retains the process handle on Windows, avoiding PID-reuse/name cleanup.
            process.terminate()
            process.wait(timeout=30)
        finally:
            receipt["exitCode"] = process.returncode
            receipt["completedUtcEpoch"] = time.time()
            receipt["normalConfigAfter"] = normal_config_witness(os.environ)
            receipt["normalConfigUnchanged"] = receipt["normalConfigBefore"] == receipt["normalConfigAfter"]
            result_path = run_dir / "ui-result.json"
            if result_path.is_file():
                result = json.loads(result_path.read_text(encoding="utf-8-sig"))
                receipt["uiResult"] = str(result_path)
                receipt["uiResultSha256"] = file_hash(result_path)
                receipt["success"] = (process.returncode == 0 and result.get("ok") is True
                                      and receipt["normalConfigUnchanged"] and not receipt.get("timedOut"))
            save()
    print("GUI receipt: " + str(receipt_path), flush=True)
    return 0 if receipt["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
