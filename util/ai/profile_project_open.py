"""Read saved restore telemetry and profile file-only workspace inspection.

Never opens a capture, selects an event, applies a replacement or edits a project.
"""
import argparse
import collections
import cProfile
import io
import json
import os
from pathlib import Path
import pstats
import sys
import time


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--console", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile-workspaces", type=int, default=0)
    parser.add_argument("--profile-project", action="store_true")
    parser.add_argument("--profile-selection", action="store_true")
    parser.add_argument("--benchmark-project-read", action="store_true")
    args = parser.parse_args()
    result = json.loads(args.result.read_text(encoding="utf-8"))
    events = [json.loads(line) for line in args.console.read_text(encoding="utf-8").splitlines()]
    loaded = [e for e in events if e.get("code") == "captureLoaded"][-1:]
    if loaded:
        events = [e for e in events if e.get("unixMs", 0) >= loaded[0]["unixMs"]]
    starts, durations = {}, collections.defaultdict(list)
    for event in events:
        operation = event.get("context", {}).get("operation")
        if event.get("code") == "requestStarted":
            starts[operation] = event["unixMs"]
        elif event.get("code") in ("requestSucceeded", "requestFailed") and operation in starts:
            durations[operation].append(event["unixMs"] - starts.pop(operation))
    summary = {
        "sourceResult": str(args.result),
        "observedStatus": result.get("status"),
        "startedUnixMs": result.get("startedUnixMs"),
        "captureLoadedUnixMs": loaded[0]["unixMs"] if loaded else None,
        "latestProgress": result.get("progress", [])[-1:],
        "result": {k: v for k, v in result.get("result", result).items()
                   if k in ("restoreMode", "cache", "verifiedRdcCache", "inventoryCache", "performance", "error", "warnings")},
        "bridgeRequests": {key: {"count": len(values), "sumMs": sum(values),
                                 "minMs": min(values), "maxMs": max(values)}
                           for key, values in durations.items()},
    }
    derived = args.result.parent.parent
    if args.benchmark_project_read:
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root / "replaylab/mcp_host"))
        from src.projects import ReplayProjectService
        observed = {}
        for cached in (False, True):
            service = ReplayProjectService(derived.parent, cache_validated_reads=cached)
            service._load(persist_migrations=False)
            started = time.perf_counter()
            for unused in range(10):
                service._load(persist_migrations=False)
            observed["cachedMs" if cached else "ordinaryMs"] = round((time.perf_counter()-started)*1000,3)
        summary["projectReadsBenchmark"] = dict(observed, count=10)
    if args.profile_selection:
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root / "replaylab/mcp_host"))
        from src.server import _active_project_service
        service = _active_project_service()
        active = service.status()
        started = time.perf_counter()
        for unused in range(3):
            service.status()
        full = (time.perf_counter()-started)*1000
        started = time.perf_counter()
        for unused in range(3):
            with service.snapshot_active_project(active["workspaceId"], active["projectRoot"]):
                pass
        summary["selectionReads"] = {"count": 3, "fullStatusMs": round(full,3),
                                    "selectionSnapshotMs": round((time.perf_counter()-started)*1000,3)}
    if args.profile_project:
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root / "replaylab/mcp_host"))
        from src.projects import ReplayProjectService
        service = ReplayProjectService(derived.parent, cache_validated_reads=True)
        profiler = cProfile.Profile()
        with profiler:
            for unused in range(3):
                service._load(persist_migrations=False)
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(25)
        summary["projectValidationProfile"] = stream.getvalue()
    pair_groups = collections.defaultdict(list)
    for path in (derived / "validated-shader-pairs").rglob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("capturePath") != result.get("capturePath"):
            continue
        replacement = value.get("replacement", {})
        pair_groups[replacement.get("abiValidationMode", "unknown")].append(replacement.get("timingsMs", {}))
    summary["savedPairTimings"] = {
        mode: {"count": len(rows), "sumsMs": {k: round(sum(row.get(k, 0) for row in rows), 3)
                                             for k in {k for row in rows for k in row}}}
        for mode, rows in pair_groups.items()}
    if args.profile_workspaces:
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root / "replaylab/mcp_host"))
        from src.shader_workspace.artifact_reader import ShaderWorkspaceArtifactReader
        snapshots = []
        for path in (derived / "validated-rdc-restores").glob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("capturePath") == result.get("capturePath"):
                snapshots.append(value)
        paths = [Path(row["workspacePath"]) for snapshot in snapshots for row in snapshot.get("shaders", [])
                 if row.get("workspacePath")]
        if not paths and snapshots:
            project = derived.parents[2]
            paths = [project / row["managedWorkspace"]["path"] for row in snapshots[0]["shaders"]]
        profiler = cProfile.Profile()
        observed = []
        with profiler:
            for path in paths[:args.profile_workspaces]:
                started = time.perf_counter()
                status = ShaderWorkspaceArtifactReader().status(path)
                observed.append({"workspacePath": str(path), "elapsedMs": round((time.perf_counter()-started)*1000, 3),
                                 "revision": status.get("revision")})
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(35)
        summary["artifactInspection"] = observed
        summary["artifactInspectionProfile"] = stream.getvalue()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
