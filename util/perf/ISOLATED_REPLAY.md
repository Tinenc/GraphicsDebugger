# Isolated offline replay builds

The EID performance worktree uses its own core, Python binding, CLI, objects,
generated SWIG files, and output directory. Build from that worktree:

```powershell
powershell -NoProfile -File util/perf/build_eid_isolated.ps1 -Label baseline
```

Use a fresh label for each build receipt. The script requires `WORKTREE_ISOLATION.md`,
refuses another active native build or replay process, verifies output paths and their reparse-point
ancestors, and invokes MSBuild sequentially with `/m:2`, `/nr:false`,
`ReplayOptimizeDevelopment=true`, and the local explicit `SolutionDir`.
The build contains `renderdoc.dll`, `system_load.dll`, the standalone matching
`renderdoc.pyd`, CLI, and a small `eid_replay_python.exe` host. It does not build or
start qrenderdoc or deploy a Shader workspace/runtime.

The Python host matters for profiling: the native timing log is written beside
the host executable. Embedding Python in the installed PowerShell executable
would put that log outside the worktree. The isolated host puts it in the local
`x64/Development/eid_timing_driver.log`.

```powershell
powershell -NoProfile -File util/perf/run_eid_isolated_python.ps1 `
  -Script util/perf/verify_eid_isolated_runtime.py -RunLabel import-check
```

This verification imports the local runtime, identifies all loaded modules,
checks native configuration isolation, and opens no capture or GPU replay.
Other scripts run through the same wrapper using `-ScriptArguments`; capture
paths can be shared read-only, while their report/output paths must be under
this worktree. For capture/GPU scripts, require the replay process guard:

```powershell
& ./util/perf/run_eid_isolated_python.ps1 `
  -Script util/perf/eid_checkpoint_profile.py `
  -ScriptArguments @('--capture', 'D:\captures\frame.rdc', '--events', '100,200',
                     '--output', 'output/eid-perf-20260908/checkpoint-run.json') `
  -RunLabel checkpoint-run -RequireIdleReplay
```

The guard checks both GUI names (`q_render_soco_doc.exe`, `qrenderdoc.exe`), both
CLI names (`render_soco_doccmd.exe`, `renderdoccmd.exe`), and `eid_replay_python.exe`
across all worktrees. It runs immediately before starting the native host and
writes a process-identity receipt, then refuses if another replay process exists.
The parent PowerShell process is not a replay process. No other process is killed.
CPU-only import checks omit `-RequireIdleReplay` by default. For concurrent branch
development, pass `-AllowForeignGui` to the builder or replay runner. This permits
only a GUI outside this worktree after inspecting its loaded modules and confirming
none belongs to this worktree. Other replay hosts, unreadable module ownership,
and active competing builds still block; no foreign process is closed or changed.
The replay receipt records the foreign GUI identities, script arguments and
`gpuExclusiveAtStart=false`. Correctness and native copy-count acceptance can run
under these conditions; elapsed times must be labelled as shared-GPU observations,
not evidence of exclusive-GPU latency. This does not waive configuration, path,
binary hash or independent runtime checks.

This is a start-time observation, not a GPU reservation. Coordinate the GPU
window for the entire run and reject performance comparisons if another replay
process starts later. On 2026-09-08, `warmup-6-first-v3` had zero checkpoint hits
with an explicit insufficient-GPU-memory-headroom diagnostic while the original
checkout's GUI was running. Its `failedOrInconclusive` report is retained; it
cannot establish a warmup-order regression.

`RENDERDOC_CONFIG_DIR` is an explicit absolute configuration directory for all
native platforms. With no override, the normal platform path is unchanged.
An invalid explicit directory fails without falling back to the normal user
configuration. The runner supplies a worktree directory and also isolates
`TEMP`/`TMP`, suppresses Python bytecode/user-site use, and rejects scripts,
binaries, or output ancestors that escape through reparse points.

For the independent GUI build, pass `-IncludeGui -AllowForeignGui`. This compiles
the GUI with the same explicit worktree `SolutionDir`, deploys the fixed first-party
bridge, and records GUI/Qt/PySide binary hashes. It does not freeze or deploy the
out-of-process Shader/Agent service executables. Run the normal translation gates
first; the GUI embeds its generated QM catalog.

`run_eid_isolated_gui.py` launches that exact recorded executable with a fresh
directory under `output/gui-isolation/<run-label>`. It supplies private
`RENDERDOC_CONFIG_DIR`, `APPDATA`, `LOCALAPPDATA`, `TEMP` and `TMP`, removes inherited
ReplayLab project/service overrides and Python/Qt overrides, disables external MCP
autostart, and uses only local fixed host candidates. The GUI now routes UI settings,
layouts, conversations and optional extension discovery through the explicit root;
isolated Python initialization also disables user-site loading. No system environment
or normal user configuration is copied or modified. Normal configuration hashes are
observed before and after; concurrent changes by the other GUI cannot be attributed
to this test from hashes alone.

The script uses `--ui-script`, gives it `RENDERDOC_EID_UI_TEST_CONFIG`, and only
terminates its own retained process handle on timeout. Use a fresh run label every
time. The script's UI result and runner receipt must both pass before claiming GUI
acceptance. Native correctness tests do not by themselves establish that result.

```powershell
python util/perf/run_eid_isolated_gui.py `
  --ui-script util/perf/eid_gui_isolation_probe.py `
  --script-config output/gui-probe-config.json --run-label probe-001
```

Keep the probe configuration inside this worktree. Use `{"requireInternalHost":false}`
for GUI/bridge-only acceptance when sidecars have not been packaged; the default
probe requires a healthy private internal service too. Sidecar service availability and project/Shader routes require
their own locally packaged runtime and separate acceptance; the native EID/history
GUI tests do not certify those routes.
