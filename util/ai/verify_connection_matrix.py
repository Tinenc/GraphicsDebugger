"""Run requested no-effect probes for configured connections; never opens a login browser."""
import asyncio
import json
import os
from pathlib import Path
import sys

repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo / "replaylab/src"))
from replaylab_agent.profiles import ProfileStore

async def main():
    store = ProfileStore().load()
    results = []
    for profile in store.profiles.values():
        env = dict(os.environ)
        env.pop("REPLAYLAB_START_GATED", None)
        env.update(PYTHONPATH=str(repo / "replaylab/src"), PYTHONUTF8="1",
                   REPLAYLAB_SHADER_RUNTIME_DIR=str(repo / "x64/Development/replaylab"),
                   BREAD_CAPABILITIES_ROOT=str(repo / "x64/Development/replaylab/tools/capabilities"))
        args = [sys.executable, "-X", "utf8", "-m", "replaylab_agent.cli", "--config", str(store.path),
                "doctor", "--profile", profile.id, "--jsonl", "--execution-test", "--timeout", "90"]
        process = await asyncio.create_subprocess_exec(*args, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0))
        try:
            output, error = await asyncio.wait_for(process.communicate(), 210)
        except TimeoutError:
            process.kill(); await process.wait()
            results.append({"profile": profile.id, "preset": profile.preset, "status": "timeout"})
            continue
        checks = []
        for line in output.decode("utf-8", "replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") == "complete":
                checks = record.get("checks", [])
        result = {"profile": profile.id, "preset": profile.preset, "protocol": profile.provider.protocol,
                  "exitCode": process.returncode, "checks": checks}
        results.append(result)
        print(json.dumps({"preset": profile.preset, "exitCode": process.returncode,
            "failed": [c["name"] for c in checks if c["status"] == "error"], "checks": len(checks)}), flush=True)
        target = repo / "output/bread-release-fixes-20261102/connection-matrix.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    asyncio.run(main())
