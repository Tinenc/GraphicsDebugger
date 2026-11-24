"""Finalize a validated release without recompressing its unchanged dependencies.

The base archive is immutable. Zip append retains compressed payload bytes and
rewrites the central index in a new copy. Declared runtime, first-party and
snapshot updates share the normal packaging filters; scratch caches are omitted.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

from prepare_capabilities_bundle import verify, verify_public_skill_inventory


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def finalize(base: Path, capabilities: Path | None, output: Path, runtime: Path | None = None,
             first_party_source: Path | None = None):
    if output.exists() or output == base:
        raise ValueError("Finalization requires a new archive path")
    if digest(base) != base.with_suffix(".zip.sha256").read_text().split()[0]:
        raise ValueError("Base archive checksum differs")
    if capabilities is None:
        # A release whose complete archive checksum was just verified already
        # owns these exact dependencies. Runtime-only updates need no external
        # capability tree, copy, or repeated per-file filesystem scan.
        with zipfile.ZipFile(base) as previous_archive:
            source = json.loads(previous_archive.read("RenderDocBread/replaylab/tools/capabilities/bread-capabilities.json"))
        verify_public_skill_inventory(source)
    else:
        source = verify(capabilities, public=True)
    source = {**source, "files": [record for record in source["files"]
                                  if not record["path"].startswith(("python/Scripts/", "browsers/.links/"))],
              "excludedBuildOnly": ["python/Scripts/", "browsers/.links/"]}
    prefix = "RenderDocBread/"
    capsule = "replaylab/tools/capabilities/"
    additions = {}
    owned_prefixes = []
    if first_party_source is not None:
        if runtime is None:
            raise ValueError("First-party updates require a matching frozen runtime")
        from package_portable import copy_tree, release_files
        scopes = {"replaylab/renderdoc_mcp_bridge": "integrations/replaylab/python/renderdoc_mcp_bridge",
                  ".agents/skills": "replaylab/skills", "replaylab/portable_skills": "replaylab/portable-skills",
                  "replaylab/unity": "replaylab/unity"}
        with tempfile.TemporaryDirectory(prefix="bread-release-first-party-") as temporary:
            staging = Path(temporary)
            for relative, destination in scopes.items():
                source_root = first_party_source / relative
                if not source_root.is_dir():
                    raise ValueError("Missing first-party release input: " + relative)
                copy_tree(source_root, staging / destination)
                owned_prefixes.append(destination + "/")
            additions.update({path.relative_to(staging).as_posix(): path.read_bytes() for path in release_files(staging)})
    with zipfile.ZipFile(base) as old:
        manifest = json.loads(old.read(prefix + "RenderDocBread.manifest.json"))
        previous = json.loads(old.read(prefix + capsule + "bread-capabilities.json"))
        verify_public_skill_inventory(previous)
        records = {record["path"]: record for record in manifest["files"]}
        next_records = {record["path"]: record for record in source["files"]}
        for record in previous["files"]:
            if next_records.get(record["path"]) != record:
                raise ValueError("Finalization cannot change a validated runtime dependency")
        for path, record in next_records.items():
            if capsule + path not in records:
                if not path.startswith("bread-adapters/"):
                    raise ValueError("Only declared Bread adapters may be added")
                if capabilities is None:
                    raise ValueError("New capabilities require a verified source snapshot")
                additions[capsule + path] = (capabilities / path).read_bytes()
    additions[capsule + "bread-capabilities.json"] = encoded(source)
    if runtime is not None:
        from runtime_inputs import fingerprint
        runtime_manifest = json.loads((runtime / "replaylab-runtime-manifest.json").read_text(encoding="utf-8-sig"))
        if runtime_manifest.get("sourceInputsSha256") != fingerprint():
            raise ValueError("Frozen runtime does not match the final source inputs")
        from host_runtime import SUPPORT, runtime_paths
        if runtime_manifest.get("layout") == "onedir" or any(
                path.startswith("replaylab/" + SUPPORT + "/") for path in records):
            raise ValueError("Rebuild the ZIP with build_compact_release.py when replacing shared Host files")
        for name, path in runtime_paths(runtime, runtime_manifest).items():
            additions["replaylab/" + name] = path.read_bytes()
        runtime_manifest.get("shaderTools", {}).pop("path", None)
        additions["replaylab/replaylab-runtime-manifest.json"] = encoded(runtime_manifest)
    omitted = {path for path in records if "__pycache__" in Path(path).parts or Path(path).suffix.lower() in {".pyc", ".pyo"}}
    runtime_cache_count = len(omitted)
    omitted.update(path for path in records if any(path.startswith(prefix) for prefix in owned_prefixes) and path not in additions)
    for path in omitted:
        del records[path]
    for path, payload in additions.items():
        records[path] = {"path": path, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    manifest["files"] = sorted(records.values(), key=lambda record: record["path"])
    manifest["createdUtc"] = datetime.now(timezone.utc).isoformat()
    additions["RenderDocBread.manifest.json"] = encoded(manifest)
    shutil.copyfile(base, output)
    if digest(output) != digest(base):
        raise ValueError("Base archive copy differs")
    replaced = {prefix + path for path in (*omitted, *additions)}
    with zipfile.ZipFile(output, "a", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as result:
        # ZipFile's index is rewritten at close; old local payloads remain
        # unreachable. No duplicate central entries or base-file mutation.
        result.filelist[:] = [info for info in result.filelist if info.filename not in replaced]
        result.NameToInfo = {info.filename: info for info in result.filelist}
        for path, payload in additions.items():
            result.writestr(prefix + path, payload)
    with zipfile.ZipFile(output) as result:
        expected = {prefix + path for path in records} | {prefix + "RenderDocBread.manifest.json"}
        if set(result.namelist()) != expected or len(result.namelist()) != len(expected):
            raise ValueError("Final archive inventory differs")
        for path, payload in additions.items():
            if result.read(prefix + path) != payload:
                raise ValueError("Final archive changed payload differs")
    output.with_suffix(".zip.sha256").write_text(digest(output) + "  " + output.name + "\n", encoding="ascii")
    return {"archive": str(output), "files": len(records), "omittedRuntimeCaches": runtime_cache_count,
            "removedFirstPartyFiles": len(omitted) - runtime_cache_count,
            "updatedPaths": list(additions), "unchangedCompressedPayloadPreserved": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--capabilities", type=Path, help="Optional verified snapshot; omit to retain the base release's exact dependencies")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--first-party-source", type=Path)
    args = parser.parse_args()
    result = finalize(args.base.resolve(), args.capabilities.resolve() if args.capabilities else None, args.output.resolve(),
                      args.runtime.resolve() if args.runtime else None,
                      args.first_party_source.resolve() if args.first_party_source else None)
    args.receipt.write_bytes(encoded(result))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
