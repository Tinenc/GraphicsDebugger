"""Keep complete tool runtimes compressed until first use; no external downloads."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import zipfile

COMPONENTS = {"office", "browsers", "python", "native", "node", "provider-runtime", "custom-skills"}


def _path(value):
    p = PurePosixPath(value)
    if p.is_absolute() or "\\" in value or ":" in value or any(x in ("", ".", "..") for x in value.split("/")):
        raise ValueError("Invalid bundled file path")
    return p


def _write(source, target, record):
    digest = hashlib.sha256(); count = 0
    while data := source.read(1024 * 1024):
        count += len(data); digest.update(data); target.write(data)
    if count != record["size"] or digest.hexdigest() != record["sha256"]:
        raise ValueError("Capability source checksum differs: " + record["path"])


def pack(metadata, opener, output):
    if output.exists() and any(output.iterdir()):
        raise ValueError("Compressed tool output must be a new empty directory")
    output.mkdir(parents=True, exist_ok=True)
    # Version/vendor nesting is metadata, not a runtime directory requirement.
    # Short plugin roots keep even their readable references Explorer-safe.
    original_opener = opener
    remaps = {p["path"].rstrip("/") + "/": "p/" + p["id"].split("@")[0] + "/"
              for p in metadata.get("plugins", [])}
    def remap(path):
        return next((new + path[len(old):] for old, new in remaps.items() if path.startswith(old)), path)
    source_paths = {remap(r["path"]): r["path"] for r in metadata["files"]}
    metadata = {**metadata, "files": [dict(r, path=remap(r["path"])) for r in metadata["files"]],
        "plugins": [dict(p, path=remap(p["path"].rstrip("/") + "/").rstrip("/")) for p in metadata.get("plugins", [])],
        "skillRoots": [remap(path.rstrip("/") + "/").rstrip("/") for path in metadata.get("skillRoots", [])]}
    opener = lambda relative: original_opener(source_paths[relative])
    physical, archives = [], []
    for name in sorted(COMPONENTS):
        records = [r for r in metadata["files"] if r["path"].startswith(name + "/")]
        if not records:
            continue
        relative = "bundles/" + name + ".zip"
        destination = output / relative
        destination.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_LZMA, allowZip64=True) as archive:
            for record in records:
                _path(record["path"])
                with opener(record["path"]) as source, archive.open(record["path"], "w", force_zip64=True) as target:
                    _write(source, target, record)
        with destination.open("rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        physical.append({"path": relative, "size": destination.stat().st_size, "sha256": sha})
        archives.append({"id": name, **physical[-1], "fileCount": len(records),
                         "unpackedBytes": sum(r["size"] for r in records)})
    for record in metadata["files"]:
        parts = _path(record["path"]).parts
        # Keep skill instructions, scripts and small assets readable at startup;
        # nested npm runtimes remain in the verified custom-skills capsule.
        bootstrap = parts[0] == "custom-skills" and "node_modules" not in parts
        if parts[0] in COMPONENTS and not bootstrap:
            continue
        destination = output.joinpath(*parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with opener(record["path"]) as source, destination.open("xb") as target:
            _write(source, target, record)
        physical.append(record)
    result = {**metadata, "componentArchiveFormat": "zip-lzma-v1", "componentArchives": archives,
              "physicalFiles": sorted(physical, key=lambda r: r["path"])}
    (output / "bread-capabilities.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return result


def from_release(base, output):
    from finalize_capabilities_archive import digest
    if digest(base) != base.with_suffix(".zip.sha256").read_text().split()[0]:
        raise ValueError("Base release checksum differs")
    with zipfile.ZipFile(base) as archive:
        prefix = "RenderDocBread/replaylab/tools/capabilities/"
        metadata = json.loads(archive.read(prefix + "bread-capabilities.json"))
        if metadata.get("componentArchives"):
            raise ValueError("The supplied release is already compressed")
        return pack(metadata, lambda relative: archive.open(prefix + relative), output)


def compact_staging(root):
    """Only called for the full packager's private staging directory."""
    import tempfile
    metadata = json.loads((root / "bread-capabilities.json").read_text(encoding="utf-8"))
    if metadata.get("componentArchives"):
        return
    with tempfile.TemporaryDirectory(prefix="bread-tool-capsules-") as temporary:
        compressed = Path(temporary) / "caps"
        pack(metadata, lambda relative: (root / relative).open("rb"), compressed)
        # Validate the exact absolute destination before recursive removal.
        target, parent = root.resolve(), root.parent.resolve()
        if target == parent or not target.is_relative_to(parent) or root.is_symlink():
            raise ValueError("Capability staging target escaped its package directory")
        shutil.rmtree(target)
        shutil.copytree(compressed, target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = from_release(args.base.resolve(), args.output.resolve())
    print(json.dumps({"components": len(result["componentArchives"]),
        "logicalBytes": sum(r["size"] for r in result["files"]),
        "physicalBytes": sum(r["size"] for r in result["physicalFiles"]),
        "physicalFiles": len(result["physicalFiles"]), "maxPathLength": max(len(r["path"]) for r in result["physicalFiles"])}))
