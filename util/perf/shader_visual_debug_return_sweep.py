"""Read-only source snapshot sweep of every non-void return expression.

Reports parser coverage, not GPU or compiler correctness. A GLSL source with
conditional macros requires --glslang for actual preprocessing first. Outputs
only the requested receipt; compiler scratch uses the OS temporary directory.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--language", choices=("hlsl", "glsl"), required=True)
    parser.add_argument("--entry", default="main")
    parser.add_argument("--glslang")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    module_path = root / "replaylab/renderdoc_mcp_bridge/shader_visual_instrument.py"
    spec = importlib.util.spec_from_file_location("bread_return_sweep", str(module_path))
    visual = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(visual)
    source_path = Path(args.source).resolve()
    original = source_path.read_bytes()
    source = original.decode("utf-8-sig")
    report = {"schema": "bread-visual-return-sweep/v1", "source": str(source_path),
              "sourceSha256": hashlib.sha256(original).hexdigest(),
              "instrumenterSha256": hashlib.sha256(module_path.read_bytes()).hexdigest(),
              "language": args.language, "scope": "inspect-only", "returns": []}
    if visual.needs_preprocess(source):
        if args.language != "glsl" or not args.glslang:
            raise RuntimeError("Supply actual compiler preprocessing for this source")
        with tempfile.TemporaryDirectory(prefix="bread-return-sweep-") as temp:
            filename = Path(temp) / "source.frag"
            filename.write_text(source, encoding="utf-8")
            processed = subprocess.run([args.glslang, "-E", "-S", "frag", str(filename)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
            if processed.returncode:
                raise RuntimeError(processed.stderr)
            source = processed.stdout
        report["preprocessedSha256"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
    tokens, _pairs = visual._tokens(source)
    report["voidReturnsSkipped"] = 0
    for index, token in enumerate(tokens):
        if token["v"] != "return":
            continue
        if index + 1 < len(tokens) and tokens[index + 1]["v"] == ";":
            report["voidReturnsSkipped"] += 1
            continue
        offset = len(source[:token["s"]].encode("utf-8"))
        row = {"line": source.count("\n", 0, token["s"]) + 1, "byteStart": offset}
        try:
            plan = visual.inspect(source, offset, language=args.language, entry_point=args.entry)
            row.update(ok=True, selection=plan["selection"], valueType=plan["valueType"], choices=plan["choices"])
        except visual.VisualDebugError as error:
            row.update(ok=False, code=error.code, detail=error.detail)
        report["returns"].append(row)
    report["passed"] = sum(row["ok"] for row in report["returns"])
    report["total"] = len(report["returns"])
    report["ok"] = report["passed"] == report["total"]
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("ok", "passed", "total", "voidReturnsSkipped")}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
