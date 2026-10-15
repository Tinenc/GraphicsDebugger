"""Validate this source snapshot; --live also performs read-only IPC queries."""
import argparse
import ast
import asyncio
import datetime
import importlib.metadata
import json
import pathlib
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    root = pathlib.Path(__file__).resolve().parent
    sources = sorted((root / "server" / "src").glob("*.py"))
    bridge = root / "bridge" / "renderdoc_mcp" / "__init__.py"
    for path in sources + [bridge]:
        ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    ast.parse(bridge.read_text(encoding="utf-8-sig"), feature_version=(3, 6))
    sys.path.insert(0, str(root / "server"))
    from src import server

    tools = asyncio.run(server.mcp.list_tools())
    report = {
        "verifiedAt": datetime.datetime.now().astimezone().isoformat(),
        "python": sys.version.split()[0],
        "mcpSdkVersion": importlib.metadata.version("mcp"),
        "source": pathlib.Path(server.__file__).relative_to(root).as_posix(),
        "syntaxFilesChecked": len(sources) + 1,
        "bridgePython36Syntax": "passed (syntax only)",
        "registeredToolCount": len(tools),
        "registeredTools": sorted(tool.name for tool in tools),
    }
    if args.live:
        report["ping"] = server.ipc.call("ping", {})
        if "ipc_dir" in report["ping"]:
            report["ping"]["ipc_dir"] = "<current-user TEMP>/renderdoc_mcp_ipc"
        status = server.ipc.call("get_capture_status", {})
        report["captureStatus"] = {
            key: status[key] for key in ("loaded", "api", "renderer", "frameNumber", "stats_note")
            if key in status
        }
        if status.get("loaded"):
            data = server.ipc.call("get_draw_calls", {"include_children": True, "only_actions": True})
            actions = data["actions"]
            report["currentCaptureActions"] = {
                "returnedEntries": len(actions),
                "draws": sum(bool(action["flags"] & 0x0002) for action in actions),
                "dispatches": sum(bool(action["flags"] & 0x0004) for action in actions),
                "presents": [action for action in actions if action["flags"] & 0x0100],
            }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
