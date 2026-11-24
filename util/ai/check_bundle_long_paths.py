"""Read the previously failing MathJax file using the bundled Node runtime."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
root = args.root.resolve()
metadata = json.loads((root / "bread-capabilities.json").read_text(encoding="utf-8"))
matches = [root / r["path"] for r in metadata["files"]
           if r["path"].lower().endswith("/autoloadconfiguration.js")]
if not matches:
    raise RuntimeError("The reported MathJax file is absent from the complete bundle")
target = max(matches, key=lambda p: len(str(p)))
script = "const fs=require('node:fs'); const p=process.argv[1]; const b=fs.readFileSync(p); console.log(JSON.stringify({ok:fs.statSync(p).isFile()&&b.length>0,pathLength:p.length,bytes:b.length}));"
with tempfile.TemporaryDirectory(prefix="bread-node-long-path-") as temporary:
    lengthy = Path(temporary).joinpath(*(["nested-runtime-directory"]*10), target.name)
    native = Path("\\\\?\\" + str(lengthy)) if os.name == "nt" else lengthy
    native.parent.mkdir(parents=True)
    native.write_bytes(target.read_bytes())
    result = subprocess.run([str(root / "node/bin/node.exe"), "-e", script, str(lengthy)],
        capture_output=True, text=True, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
report = json.loads(result.stdout)
args.output.write_text(json.dumps(report), encoding="utf-8")
print(json.dumps(report))
