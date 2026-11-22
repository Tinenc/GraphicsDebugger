"""Preserve shipped skills/tools while omitting only Office and bundled Chromium.

Inputs are an already verified application-owned snapshot, never a user's home.
Dependency capsules are copied verbatim with their hashes and licenses.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile
import os

from prepare_capabilities_bundle import verify

EXCLUDED = ("office/", "browsers/")


def refresh_compatibility_skills(output, value):
    routes = {"renderdoc-analyzer": "renderdoc-explain-event", "replaylab-debugger": "renderdoc-shader-workbench",
              "uex-export-asset-report": "renderdoc-unpacked-asset-intelligence", "uex-export-blender": "renderdoc-unpacked-asset-intelligence"}
    changes = {}
    for name, route in routes.items():
        relative = "custom-skills/" + name + "/SKILL.md"
        path = output / relative
        if not path.is_file(): raise ValueError("Missing retained compatibility skill: " + name)
        header = path.read_text(encoding="utf-8").split("---", 2)[1]
        data = ("---" + header + "---\n\n# Bread compatibility workflow\n\n"
            "Load `replaylab-core` with `load_bread_skill`, then load `" + route + "`. "
            "Follow those current canonical tools and references rather than historical examples. "
            "Keep the task's explicit instance/capture/EID fixed while the user browses. "
            "Reuse current unchanged observations and receipts. Repair missing evidence and continue "
            "feasible work; never certify an unobserved check. Optional configured MCP servers "
            "are searchable without an @ mention.\n").encode("utf-8")
        path.write_bytes(data); changes[relative] = data
    capsule = output / "bundles/custom-skills.zip"
    pending = capsule.with_suffix(".zip.pending")
    with zipfile.ZipFile(capsule) as old, zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_LZMA) as new:
        for member in old.infolist():
            new.writestr(member.filename, changes.get(member.filename, old.read(member)))
    os.replace(pending, capsule)
    for records in (value["files"], value["physicalFiles"]):
        for record in records:
            if record["path"] in changes:
                data = changes[record["path"]]
                record.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
            elif record["path"] == "bundles/custom-skills.zip":
                record.update(size=capsule.stat().st_size, sha256=hashlib.sha256(capsule.read_bytes()).hexdigest())
    for archive in value["componentArchives"]:
        if archive["id"] == "custom-skills":
            archive.update(size=capsule.stat().st_size, sha256=hashlib.sha256(capsule.read_bytes()).hexdigest(),
                unpackedBytes=sum(r["size"] for r in value["files"] if r["path"].startswith("custom-skills/")))


def prepare(source, output):
    if output.exists() and any(output.iterdir()):
        value = verify(output, public=True)
        if value.get("distributionProfile") != "standard":
            raise ValueError("Use a new standard bundle directory")
        return value
    value = verify(source, public=True)
    output.mkdir(parents=True, exist_ok=True)
    selected = [r for r in value["files"] if not r["path"].startswith(EXCLUDED)]
    archives = [r for r in value.get("componentArchives", []) if r["id"] not in {"office", "browsers"}]
    physical = [r for r in value["physicalFiles"] if not r["path"].startswith(EXCLUDED)
                and r["path"] not in {"bundles/office.zip", "bundles/browsers.zip"}]
    for record in physical:
        target = output / record["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / record["path"], target)
    value.update(files=selected, physicalFiles=physical, componentArchives=archives,
                 distributionProfile="standard", features={"office": False, "browser": False,
                     "privateProviderClients": True, "portableTools": True},
                 excludedComponents=["office", "browsers"],
                 sourceManifestSha256=hashlib.sha256((source / "bread-capabilities.json").read_bytes()).hexdigest())
    value["plugins"] = [p for p in value["plugins"] if not (p["path"] + "/").startswith(EXCLUDED)]
    value["skillRoots"] = [p for p in value["skillRoots"] if not (p + "/").startswith(EXCLUDED)]
    refresh_compatibility_skills(output, value)
    (output / "bread-capabilities.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return verify(output, public=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.source.resolve(), args.output.resolve())
    print(json.dumps({"profile": "standard", "plugins": len(result["plugins"]),
                      "physicalBytes": sum(r["size"] for r in result["physicalFiles"]),
                      "excludedComponents": result["excludedComponents"]}))
