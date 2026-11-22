"""Rewrite a sealed release with current hosts and complete compressed tools.

Unlike append-based runtime finalization this drops superseded local ZIP payloads.
It never deploys into, modifies, or stops the running application.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import zipfile

from package_portable import copy_tree, release_files, zip_compress_type
from prepare_capabilities_bundle import verify
from runtime_inputs import fingerprint, REPO
from host_runtime import SUPPORT, runtime_paths
from version_policy import synchronize as check_versions, validate_runtime_version, validate_gui_version

PREFIX = "RenderDocBread/"
CAPS = "replaylab/tools/capabilities/"
SCOPES = {"replaylab/renderdoc_mcp_bridge": "integrations/replaylab/python/renderdoc_mcp_bridge",
          ".agents/skills": "replaylab/skills", "replaylab/portable_skills": "replaylab/portable-skills",
          "replaylab/unity": "replaylab/unity"}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def compose(base, capabilities, runtime, gui, output, *, replay_bindings=None, replay_core=None):
    if output.exists() or output.with_suffix(".zip.partial").exists():
        raise ValueError("A new release destination is required")
    if digest(base) != base.with_suffix(".zip.sha256").read_text().split()[0]:
        raise ValueError("Base archive checksum differs")
    tools = verify(capabilities, public=True) if capabilities else {"files": [], "physicalFiles": []}
    profile = tools.get("distributionProfile", "extended") if capabilities else "core"
    if capabilities and profile != "rdc" and not tools.get("componentArchives"):
        raise ValueError("Compressed capabilities are required")
    hosts = json.loads((runtime / "replaylab-runtime-manifest.json").read_text(encoding="utf-8-sig"))
    if hosts.get("sourceInputsSha256") != fingerprint():
        raise ValueError("Frozen runtime does not match current source inputs")
    identity = check_versions(REPO)
    validate_runtime_version(hosts.get("version"), repo=REPO)
    validate_gui_version(gui, identity["version"])
    additions = {"RenderDocBread.exe": gui}
    if replay_core is not None:
        if replay_bindings is None:
            raise ValueError("A changed replay core requires matching Python bindings")
        additions["renderdoc.dll"] = replay_core.resolve(strict=True)
    if replay_bindings is not None:
        additions["pymodules/renderdoc.pyd"] = replay_bindings.resolve(strict=True)
    for name, path in runtime_paths(runtime, hosts).items():
        additions["replaylab/" + name] = path
    hosts.get("shaderTools", {}).pop("path", None)
    additions["replaylab/replaylab-runtime-manifest.json"] = encoded(hosts)
    if capabilities:
        for record in tools.get("physicalFiles", tools["files"]):
            additions[CAPS + record["path"]] = capabilities / record["path"]
        additions[CAPS + "bread-capabilities.json"] = capabilities / "bread-capabilities.json"
    additions["replaylab/bread-distribution.json"] = encoded({
        "schema": "bread-distribution/v1", "profile": profile})
    additions["README.txt"] = Path(__file__).with_name("PORTABLE_README.txt")
    pending = output.with_suffix(".zip.partial")
    with tempfile.TemporaryDirectory(prefix="bread-compact-release-") as temporary, zipfile.ZipFile(base) as old:
        stage = Path(temporary)
        for source, target in SCOPES.items():
            if source == "replaylab/portable_skills" and (not capabilities or profile == "rdc"):
                continue
            copy_tree(REPO / source, stage / target)
        additions.update({p.relative_to(stage).as_posix(): p for p in release_files(stage)})
        manifest = json.loads(old.read(PREFIX + "RenderDocBread.manifest.json"))
        previous = json.loads(old.read(PREFIX + CAPS + "bread-capabilities.json")) if capabilities and PREFIX + CAPS + "bread-capabilities.json" in old.namelist() else {}
        # Rebased plugin directory names must not silently drop or change data.
        inventory = lambda value: Counter((r["size"], r["sha256"]) for r in value["files"])
        if capabilities and profile not in {"standard", "rdc"} and inventory(previous) != inventory(tools):
            raise ValueError("Compressed dependencies differ from the sealed base release")
        records = {}
        before = {r["path"]: r for r in manifest["files"]}
        owned = tuple(value + "/" for value in SCOPES.values())
        core_config = json.loads(Path(__file__).with_name("RenderDocBread.package.json").read_text(encoding="utf-8"))
        core_tools = set(core_config["toolFiles"]) | set(core_config["toolDirectories"])
        def retained_tool(path):
            if capabilities or not path.startswith("replaylab/tools/"):
                return True
            return path.removeprefix("replaylab/tools/").split("/", 1)[0] in core_tools
        retained = {path: record for path, record in before.items()
                    if not path.startswith((CAPS, "replaylab/" + SUPPORT + "/", *owned)) and path not in additions
                    and retained_tool(path)
                    and "__pycache__" not in Path(path).parts and Path(path).suffix not in {".pyc", ".pyo"}}
        if len(set(old.namelist())) != len(old.namelist()):
            raise ValueError("Base release has duplicate archive members")
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(pending, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as new:
            for path in sorted(set(retained) | set(additions)):
                # A short physical inventory is also checked below. Deep npm and
                # Office layouts only occur inside the owned runtime capsules.
                if len(PREFIX + path) > 190:
                    raise ValueError("Release path is too long: " + path)
                source = additions.get(path)
                if isinstance(source, bytes):
                    stream = io.BytesIO(source)
                elif source is not None:
                    stream = source.open("rb")
                else:
                    stream = old.open(PREFIX + path)
                member = zipfile.ZipInfo(PREFIX + path, datetime.now().timetuple()[:6])
                input_size = len(source) if isinstance(source, bytes) else source.stat().st_size if source is not None else before[path]["size"]
                member.compress_type = zip_compress_type(path, input_size)
                sha = hashlib.sha256(); size = 0
                with stream, new.open(member, "w", force_zip64=True) as dest:
                    while data := stream.read(1024 * 1024):
                        sha.update(data); size += len(data); dest.write(data)
                record = {"path": path, "size": size, "sha256": sha.hexdigest()}
                if path in retained and record != retained[path]:
                    raise ValueError("Base member differs from its receipt: " + path)
                records[path] = record
            manifest.update(createdUtc=datetime.now(timezone.utc).isoformat(),
                            breadVersion=hosts.get("version"), profile=profile,
                            versioning=identity,
                            files=sorted(records.values(), key=lambda r: r["path"]))
            new.writestr(PREFIX + "RenderDocBread.manifest.json", encoded(manifest))
    # Verify every released byte, including retained native dependencies.
    with zipfile.ZipFile(pending) as archive:
        expected = {PREFIX + p for p in records} | {PREFIX + "RenderDocBread.manifest.json"}
        if set(archive.namelist()) != expected or len(archive.namelist()) != len(expected):
            raise ValueError("Release inventory differs")
        for path, record in records.items():
            with archive.open(PREFIX + path) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != record["sha256"]:
                    raise ValueError("Release checksum differs: " + path)
    os.replace(pending, output)
    sha = digest(output)
    output.with_suffix(".zip.sha256").write_text(sha + "  " + output.name + "\n", encoding="ascii")
    return {"archive": str(output), "sha256": sha, "files": len(records),
            "archiveBytes": output.stat().st_size, "extractedBytes": sum(r["size"] for r in records.values()),
            "maxRelativePath": max(len(PREFIX + p) for p in records),
            "logicalToolFiles": len(tools["files"]), "dependenciesPreserved": bool(capabilities),
            "profile": profile}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base", "runtime", "gui", "output", "receipt"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--capabilities", type=Path)
    parser.add_argument("--replay-bindings", type=Path)
    parser.add_argument("--replay-core", type=Path)
    parser.add_argument("--minimal", action="store_true", help="Explicitly omit standard workflow skills/tools")
    args = parser.parse_args()
    if not args.capabilities and not args.minimal:
        config = json.loads(Path(__file__).with_name("RenderDocBread.package.json").read_text())
        from restore_dependencies import standard_capabilities
        args.capabilities = standard_capabilities(config)
    result = compose(args.base.resolve(), args.capabilities.resolve() if args.capabilities else None, args.runtime.resolve(), args.gui.resolve(), args.output.resolve(), replay_bindings=args.replay_bindings, replay_core=args.replay_core)
    args.receipt.write_bytes(encoded(result))
    print(json.dumps(result))
