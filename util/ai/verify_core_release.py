"""Verify a relocated core ZIP without changing installed apps or user data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "replaylab/packaging"))
from package_portable import audit, smoke, verify_compilers


def clean_owned_temporary(folder, prefix):
    folder = folder.resolve()
    if folder.parent != Path(tempfile.gettempdir()).resolve() or not folder.name.startswith(prefix):
        raise ValueError("Temporary cleanup escaped its owned directory")
    # Frozen child bootloaders can release their executable shortly after the
    # native GUI exits. Never terminate a live installed application to clean up.
    for attempt in range(60):
        try:
            shutil.rmtree(folder)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == 59:
                raise
            time.sleep(.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    with args.archive.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    if sha != args.archive.with_suffix(".zip.sha256").read_text().split()[0]:
        raise ValueError("Release checksum differs")
    report = {"archive": str(args.archive.resolve()), "sha256": sha, "checks": [], "status": "running"}
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    def passed(name):
        report["checks"].append(name)
        save()
        print("Passed: " + name, flush=True)
    stage = Path(tempfile.mkdtemp(prefix="bread-core-relocated-"))
    try:
        with zipfile.ZipFile(args.archive) as archive:
            for name in archive.namelist():
                target = (stage / name).resolve()
                if not target.is_relative_to(stage) or not name.startswith("RenderDocBread/"):
                    raise ValueError("Invalid archive path")
            archive.extractall(stage)
        root = stage / "RenderDocBread"
        config = json.loads((REPO / "replaylab/packaging/RenderDocBread.package.json").read_text())
        manifest = json.loads((root / "RenderDocBread.manifest.json").read_text(encoding="utf-8"))
        for record in manifest["files"]:
            path = root / record["path"]
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != record["sha256"] or path.stat().st_size != record["size"]:
                raise ValueError("Release member differs: " + record["path"])
        passed("all-released-file-hashes")
        assert manifest["profile"] in {"core", "standard", "rdc"}
        if manifest["profile"] == "core":
            assert not (root / "replaylab/tools/capabilities").exists()
            assert not (root / "replaylab/portable-skills").exists()
        else:
            capabilities = json.loads((root / "replaylab/tools/capabilities/bread-capabilities.json").read_text(encoding="utf-8"))
            assert capabilities["distributionProfile"] == manifest["profile"]
            assert not any(r["path"].startswith(("office/", "browsers/")) for r in capabilities["files"])
            if manifest["profile"] == "standard":
                assert {"office", "browsers"} == set(capabilities["excludedComponents"])
                passed("standard-workflows-preserved-without-office")
            else:
                assert not (root / "replaylab/portable-skills").exists()
                assert not capabilities["componentArchives"]
                assert not any(r["path"].startswith(("python/", "node/", "browsers/", "p/")) for r in capabilities["files"])
                passed("rdc-unity-unpacker-workflows-without-general-plugin-runtimes")
        audit(root, config); passed("core-inventory-privacy-and-relative-paths")
        verify_compilers(root); passed("pinned-shader-compilers-and-navigation")
        smoke(root); passed("frozen-host-unity-unpacker-restricted-path-smoke")
        catalog_env = {k: v for k, v in os.environ.items()
                       if not k.startswith(("REPLAYLAB_", "BREAD_", "QT_", "PYTHON", "CODEX_", "CUA_"))
                       and k not in {"VIRTUAL_ENV", "NODE_PATH", "NODE_OPTIONS"}}
        for key, relative in {"LOCALAPPDATA":"local", "APPDATA":"roaming", "USERPROFILE":"user",
                              "REPLAYLAB_CONFIG_DIR":"agent-config"}.items():
            folder = stage / "catalog-profile" / relative
            folder.mkdir(parents=True, exist_ok=True)
            catalog_env[key] = str(folder)
        catalog_env.update(HOME=catalog_env["USERPROFILE"],
            PATH=str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"),
            PYINSTALLER_RESET_ENVIRONMENT="1")
        catalog_process = subprocess.run([str(root / "replaylab/replaylab-ai.exe"), "tools", "list", "--json"],
            env=catalog_env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if catalog_process.returncode:
            raise RuntimeError("Frozen catalog failed: " + catalog_process.stderr[-1200:])
        skills = json.loads(catalog_process.stdout)["skills"]
        expected_count = {"standard": 23, "rdc": 9, "core": 5}[manifest["profile"]]
        if len(skills) != expected_count or any(s["source"] != "bundled" for s in skills):
            raise RuntimeError("Bundled skill inventory/origin differs")
        if any(not Path(s["path"]).resolve().is_relative_to(root.resolve()) for s in skills):
            raise RuntimeError("Built-in skills depend on a temporary or external directory")
        report["skills"] = [{"id": s["id"], "source": s["source"]} for s in skills]
        passed("fresh-profile-skills-all-built-in-with-persistent-bundled-paths")
        if manifest["profile"] == "standard":
            execution = subprocess.run([str(root / "replaylab/replaylab-ai.exe"), "-I", "-m",
                "replaylab_agent.portable_tools", "--verify-execution"], env=catalog_env,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if execution.returncode:
                raise RuntimeError("Relocated document/Node tools failed: " + execution.stderr[-2400:])
            report["portableExecution"] = json.loads(execution.stdout)
            passed("relocated-document-sdk-and-node-execution-without-office-or-chromium-payload")
        # Native window lifecycle regression on a private Windows desktop.
        # The deterministic child avoids accounts, model requests and external services.
        if os.name == "nt":
            # The portable marker intentionally selects the packaged CLI before
            # REPLAYLAB_AI_CLI. Temporarily omit ONLY this disposable copy's
            # marker to inject the deterministic streaming child, then restore
            # its exact bytes before the post-smoke audit.
            marker = root / "RenderDocBread.portable"
            marker_bytes = marker.read_bytes()
            marker.unlink()
            try:
                for parent in (False, True):
                    receipt = stage / ("close-parent.json" if parent else "close-check.json")
                    command = [sys.executable, str(REPO / "util/ai/run_connection_ui_smoke.py"),
                               "--app", str(root / "RenderDocBread.exe"), "--output", str(receipt)]
                    if parent:
                        command.append("--close-parent")
                    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=65, creationflags=subprocess.CREATE_NO_WINDOW)
                    if result.returncode:
                        raise RuntimeError("Native connection UI regression: " + result.stdout[-1600:] + result.stderr[-1600:])
                    details = json.loads(receipt.read_text(encoding="utf-8"))
                    scratch = Path(details.pop("scratch"))
                    diagnostics = list((scratch / "local/ReplayLab/logs").glob("bridge-*.jsonl"))
                    if not diagnostics:
                        raise RuntimeError("Packaged bridge diagnostic module did not start")
                    rows = [json.loads(line) for line in diagnostics[0].read_text(encoding="utf-8").splitlines()]
                    if not any(r["kind"] == "started" and r["outcome"] == manifest["breadVersion"] for r in rows):
                        raise RuntimeError("Packaged bridge version differs")
                    details["bridgeDiagnostics"] = "started-with-matching-version"
                    report["closeParent" if parent else "closeCheck"] = details
                    clean_owned_temporary(scratch, "bread-connection-ui-")
                    passed("native-parent-close" if parent else "native-diagnostic-close")
            finally:
                marker.write_bytes(marker_bytes)
        # Embedded Python ignores Python environment variables and legitimately
        # creates .pyc files on first UI startup. They were absent from the
        # sealed ZIP; remove only these caches in our disposable extraction.
        for cache in sorted(root.rglob("__pycache__"), key=lambda p: len(p.parts), reverse=True):
            if not cache.resolve().is_relative_to(stage) or cache.is_symlink() or cache.is_junction():
                raise ValueError("Runtime cache escaped the temporary extraction")
            shutil.rmtree(cache)
        audit(root, config); passed("post-startup-core-audit")
        report.update(version=manifest["breadVersion"], profile=manifest["profile"],
                      archiveBytes=args.archive.stat().st_size,
                      extractedBytes=sum(r["size"] for r in manifest["files"]), files=len(manifest["files"]))
    finally:
        clean_owned_temporary(stage, "bread-core-relocated-")
    report["status"] = "passed"
    save()


if __name__ == "__main__":
    main()
