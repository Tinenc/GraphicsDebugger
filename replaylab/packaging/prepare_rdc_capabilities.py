"""Retain RDC/UEX compatibility skills without general-purpose plugin runtimes.

Built-in Host/Shader/Unity/unpacker runtimes are release allowlist inputs,
independent of this optional skills snapshot. No user home is an input.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from prepare_capabilities_bundle import verify

KEEP = ("renderdoc-analyzer", "replaylab-debugger", "uex-export-asset-report", "uex-export-blender")


def prepare(source, output):
    if output.exists() and any(output.iterdir()):
        value = verify(output)
        if value.get("distributionProfile") != "rdc":
            raise ValueError("Use a new RDC bundle directory")
        return value
    previous = verify(source)
    prefixes = tuple("custom-skills/" + name + "/" for name in KEEP)
    records = [row for row in previous["files"] if row["path"].startswith(prefixes)]
    for name in KEEP:
        if not any(row["path"] == "custom-skills/" + name + "/SKILL.md" for row in records):
            raise ValueError("Missing RDC compatibility skill: " + name)
    for row in records:
        target = output / row["path"]; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / row["path"], target)
    # Compatibility names dispatch to the current canonical workflows rather
    # than reintroducing historical validation gates or developer-machine CLIs.
    routes = {"renderdoc-analyzer": "renderdoc-explain-event", "replaylab-debugger": "renderdoc-shader-workbench",
              "uex-export-asset-report": "renderdoc-unpacked-asset-intelligence",
              "uex-export-blender": "renderdoc-unpacked-asset-intelligence"}
    for name, route in routes.items():
        path = output / "custom-skills" / name / "SKILL.md"
        old = path.read_text(encoding="utf-8")
        header = old.split("---", 2)[1]
        path.write_text("---" + header + "---\n\n# Bread compatibility workflow\n\n"
            "Load `replaylab-core` through `load_bread_skill` first, then load `" + route +
            "` with `skill_id`. Use the current workflow's bundled tools and references.\n"
            "For general live inspection use `renderdoc-explain-event`; Shader work uses "
            "`renderdoc-shader-workbench`, capture-linked asset/Unity export uses "
            "`renderdoc-resource-export`. UEX reports and Blender handoffs use "
            "`renderdoc-unpacked-asset-intelligence`. Find configured optional MCP tools "
            "by capability when relevant; an @ mention is optional.\n\n"
            "Keep the task's explicit instance/capture/EID fixed while the user browses. "
            "Reuse unchanged observations and successful receipts; describe unavailable "
            "evidence and continue feasible work. Never mark an unobserved check passed. "
            "The current canonical workflow takes precedence over historical examples "
            "in this compatibility folder. No external plugin/runtime is required.\n", encoding="utf-8")
    records = [{**row, "size": (output/row["path"]).stat().st_size,
                "sha256": hashlib.sha256((output/row["path"]).read_bytes()).hexdigest()} for row in records]
    value = {"schema": "bread-portable-capabilities/v1", "distributionProfile": "rdc",
        "plugins": [], "skillRoots": ["custom-skills"], "files": records,
        "componentArchives": [], "credentialsIncluded": False,
        "features": {"office": False, "python": False, "node": False, "browser": False,
                     "portableTools": False, "privateProviderClients": False},
        "retainedCompatibilitySkills": list(KEEP),
        "sourceManifestSha256": hashlib.sha256((source / "bread-capabilities.json").read_bytes()).hexdigest()}
    (output / "bread-capabilities.json").write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return verify(output)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    args=parser.parse_args(); result=prepare(args.source.resolve(), args.output.resolve())
    print(json.dumps({"profile": "rdc", "skills": list(KEEP), "bytes": sum(r["size"] for r in result["files"])}))
