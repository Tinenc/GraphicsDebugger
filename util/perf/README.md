# EID replay timing

Start qrenderdoc with `RENDERDOC_EID_TIMING=1` to enable driver timing. The variable is sampled once
per process. The driver writes `eid_timing_driver.log` beside the executable; the UI independently
writes `eid_timing.log`. Timing remains disabled during ordinary use. Copy the baseline log before
an A/B run, since the logs append across runs.

```powershell
python util/perf/analyze_eid_timing.py x64/Development/eid_timing_driver.log --last 20
python util/perf/analyze_eid_timing.py current.log --baseline baseline.log --eids 3571,5489,12556 --json comparison.json
python util/perf/analyze_eid_timing.py current.log --api ScrubReplay --type ""
python -m unittest discover -s util/perf -p 'test_*.py'
```

The old `total_ms`, `applyInitial_ms`, `contextReplay_ms`, `explicitWait_ms`, and `explicitSubmit_ms`
fields remain available. Version 2 adds bounded, aggregate fields to the same replay record:

- `section.NAME_ms` is inclusive CPU wall time; nested sections overlap. GPU copy sections measure
  CPU setup and command recording, including any nested waits, **not GPU execution duration**.
- `section.NAME_self_ms` excludes nested sections. All self times plus `unscoped_ms` decompose the
  replay total. Re-entering the same section contributes its inclusive time once.
- `section.NAME_calls` counts non-overlapping outermost entries of that section.
- `wait.NAME_ms` and `wait.NAME_calls` attribute actual CPU wait intervals to the context active
  when the outermost wait began. Their totals equal `explicitWait_ms`; do not add them again.
- `count.NAME` includes initial-state resources visited (including skipped ones), descriptor
  count, CPU copied bytes, GPU buffer bytes, padded texture upload footprint bytes, and number of
  whole-resource GPU copies. The last three are separate quantities, not a measured GPU bandwidth.
  A restore resource counter records entry into that branch; an early error may prevent its copy.

For descriptor restoration, `initialDescriptors` counts descriptors actually copied and
`initialDescriptorsSkipped` counts unchanged descriptors left in place. Dirty writes are grouped
in 64-descriptor blocks and adjacent blocks are copied as one `initialDescriptorRanges` entry.
The first restore, binding invalidation, validation modes, small heaps, and highly fragmented
writes use full restoration (`initialDescriptorFullRestores`). Set
`RENDERDOC_D3D12_CONSERVATIVE_REPLAY_SYNC=1` before starting qrenderdoc to retain full descriptor
restoration and conservative replay synchronization for an A/B comparison.

Only nonzero detail fields are emitted. The disabled path does not read the clock, allocate detail
storage or write logs. Enabled per-resource instrumentation accumulates in thread-local replay
state; formatting and file I/O happen once at the end of a replay, not once per resource.

The analyzer uses nearest-rank p50/p95, preserves unmeasured fields in old records, and checks the
self-time/wait decompositions within rounding tolerance. Baseline comparisons use median latency
for matching destination EIDs; keep the capture, build configuration, validation flags and cache
warmth consistent. The driver total excludes downstream presentation and UI queuing. Inspect
`ScrubReplay`/`ScrubSnapshotHit` and UI timing separately before claiming interactive frame rates.

For a Development build with optimized D3D11, D3D12, OpenGL and Vulkan replay drivers and PDB symbols retained, use
`./build-dev.ps1 -SkipRuntime -OptimizeReplay`, or pass `/p:ReplayOptimizeDevelopment=true` to
MSBuild. The ordinary Development build remains unoptimized for source debugging. Record this
flag in comparisons: optimized code and the default `/Od` build are different measurements.
The option applies to every translation unit and precompiled header in those four driver
projects. It does not change the shared core's compilation flags.

`RENDERDOC_D3D12_CONSERVATIVE_REPLAY_SYNC=1` retains full descriptor restoration, original
initial-state upload sources, and conservative CPU synchronization for correctness comparisons.
Normal mode restores dirty descriptor ranges and prepares immutable initial-state GPU sources
once during capture loading, subject to DXGI memory budget and a cache cap. Shader/CBuffer edits
still invalidate event images and replay the edited event; the immutable capture initial bytes
remain reusable. Allocation rejection retains the original upload source.

The experimental prepared command-list replay cache is disabled by default. It is not part of
the measured, supported speedup. `RENDERDOC_D3D12_ENABLE_PREPARED_REPLAY=1` opts into diagnostic
experiments only; `RENDERDOC_D3D12_DISABLE_PREPARED_REPLAY=1` takes precedence. Its query-enabled
path exposed a captured-list identity bug and still needs pipeline-boundary validation.

Earlier measurements used GPU image snapshots during held scrubbing. Those measurements remain
historical evidence; snapshot hits are not the current real-replay acceptance criterion. The
current interaction must replay each accepted EID and publish matching pipeline/resource state
during motion. Explicit render-target History is a separate feature.

## Cross-API real replay and demo capture

D3D11, D3D12, OpenGL and Vulkan share the controller's event-selection path. A `WithoutDraw`
followed by `OnlyDraw` restores initial contents once: the latter is a partial replay of the target
action. Do not describe it as two complete frame replays. Event-scoped shader/CBuffer edits are
applied between those phases. In Vulkan, first-use shader feedback can execute the target action
and replace `OnlyDraw`; a blind `Full` followed by pipeline fetching can instead execute it twice.
Keep the canonical ordering when publishing complete state. Overlay/post-VS analysis can replay
independently; simple display settings and descriptor queries need separate timing from those
analysis operations.

D3D12 queue, descriptor and immutable upload-source optimizations are API-specific. The common
profiling, event coalescing and same-event state reuse can apply across APIs without removing
their required synchronization. Pipeline-state or descriptor metadata reuse does not authorize
skipping the real resource state that the UI is inspecting.

The repository includes `util/test/demos.sln` and D3D11/GL/Vulkan Simple Triangle demos. Build the
single executable once, using installed MSVC/SDK versions (the checked-in demo defaults are old):

```powershell
$repoRoot = (Resolve-Path .).Path
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
$msbuild = & $vswhere -latest -requires Microsoft.Component.MSBuild -find 'MSBuild\**\Bin\MSBuild.exe' | Select-Object -First 1
$demoSolution = (Join-Path $repoRoot 'util/test').TrimEnd('\') + '\'
$savedCL = $env:CL
$env:CL = '/utf-8 /wd4828 ' + $env:CL # Bundled test sources contain UTF-8 literals and legacy comments.
& $msbuild util/test/demos/demos.vcxproj /m:2 /t:Build /p:Configuration=Release /p:Platform=x64 /p:PlatformToolset=v143 /p:WindowsTargetPlatformVersion=10.0 "/p:SolutionDir=$demoSolution"
$env:CL = $savedCL
powershell -NoProfile -File util/perf/run_bundled_replay_python.ps1 -Script util/perf/capture_api_smoke.py
```

The second command embeds the build's bundled Python 3.6 DLL, avoiding the need for a standalone
Python 3.6 executable or qrenderdoc. Run this helper in a separate PowerShell process. It injects
`demos_x64.exe`, queues frame 5 through target control and validates the received RDC's driver
name. It creates one timestamped output directory and preserves captures, per-demo logs and
`capture-results.json`. It does not replay or change an open GUI capture; the demos create their
own graphics windows. Only each newly created test process is terminated after capture/timeout.
Set `RENDERDOC_API_SMOKE_DEMOS`, `RENDERDOC_API_SMOKE_OUTPUT` or `RENDERDOC_API_SMOKE_APIS`
(`D3D11,OpenGL,Vulkan`) to override defaults. The Python script also works under matching
standalone Python or qrenderdoc `--python` with those environment variables.
The standalone `pymodules/renderdoc.pyd` and `renderdoc.dll` must use the same public API build;
updating qrenderdoc's embedded SWIG bindings does not update the standalone module. The runner
rejects a module older than the newest public API header before loading native code and warns
when the core DLL is newer. Matching timestamps alone do not prove ABI compatibility. Rebuild
`qrenderdoc/Code/pyrenderdoc/pyrenderdoc_module.vcxproj` with the same configuration, `/m:2`,
`ReplayOptimizeDevelopment` value and explicit `SolutionDir` as the core after API changes.
`--compiler-dir` / `RENDERDOC_API_SMOKE_COMPILER_DIR` prepends a compiler directory only to the
newly injected demo's PATH through the capture API; it does not edit system or user PATH.

For Vulkan, the helper generates one temporary implicit-layer manifest under the run output.
It uses `renderdoc.json`'s compiled `VK_LAYER_RENDERDOC_Capture` identity but points its library at
the fork's default injected `system_load.dll`. Child-only layer paths isolate this layer from
other installed RenderDoc copies; the helper does not modify registry keys or machine settings.
Run without elevation because the Vulkan loader ignores those path overrides for elevated apps.
`RENDERDOC_API_SMOKE_VULKAN_LAYER_MANIFEST` and `_VULKAN_LAYER_LIBRARY` override the two input
paths when testing another build; the selected DLL must match the injected capturer.
The JSON records the generated manifest and actual captured driver even on a driver mismatch.
See the [official loader discovery rules](https://github.com/KhronosGroup/Vulkan-Loader/blob/main/docs/LoaderLayerInterface.md#windows-layer-discovery).

Vulkan demos need either the optional linked shaderc library or `glslc`/`glslangValidator` on PATH;
the Vulkan loader alone is insufficient. Capture success proves capture generation and readable
metadata, not correct replay. Follow it with real EID/pipeline/pixel validation. The full upstream
test harness is `util/test/run_tests.py -t '^(D3D11|GL|VK)_Simple_Triangle$'` with explicit
`--demos-binary`, `--renderdoc`, `--pyrenderdoc`, `--temp` and `--artifacts`; its last two output
directories are cleared by the harness, so choose dedicated paths.

## Bounded large-capture History profiling

`eid_history_profile.py` runs in the standalone bundled Python host. It opens one explicitly named
capture and uses the GPU, so run it alone after other replay instances release their resources.
It writes phase progress before every native call, preserves diagnostic log offsets and individual
timing slices, and never overwrites an existing report. The default output folder is
`output/eid-history-20260905`; parameters do not imply that a screenshot's IDs exist in the capture.

```powershell
$env:RENDERDOC_EID_TIMING = '1'
$env:RENDERDOC_EID_HISTORY_CAPTURE = $capturePath
$env:RENDERDOC_EID_HISTORY_RESOURCE = $textureResourceId
$env:RENDERDOC_EID_HISTORY_ANCHOR = $anchorEventId
$env:RENDERDOC_EID_HISTORY_EVENTS = $eventIds -join ','
$env:RENDERDOC_EID_HISTORY_OUTPUT = "$PWD/output/eid-history-20260905/profile-new.json"
$env:RENDERDOC_EID_HISTORY_BUILD_LABEL = 'Development /O2, describe structural changes'
powershell -NoProfile -File util/perf/run_bundled_replay_python.ps1 -Script util/perf/eid_history_profile.py
```

Defaults are one repeat, at most six explicitly requested replay EIDs, and four History samples:
three neighbouring non-Discard revisions around the anchor and one distant endpoint. Candidate
discovery mirrors `RenderTargetHistoryView`: resource-wide write usage, per-EID priority deduplication,
and exclusion of `GenMips` from mip zero. It does not replay all candidates. `GetUsage` and Python
candidate filtering are timed separately; the latter is equivalent selection logic, not Qt layout
or GUI construction time. Thumbnail dimensions use the real candidate count and History's 48 MiB
decoded-RGB budget. `--history-events` can select a bounded explicit list instead.

For replay-generated resources such as OpenGL's default backbuffer, an ID from a previous process
is not a stable selector. Use `--resource-output-event <draw> --resource-output-slot 0` instead of
`--resource`; the environment equivalents end in `_RESOURCE_OUTPUT_EVENT` and `_RESOURCE_OUTPUT_SLOT`,
with `_RESOURCE=0`. The script resolves the actual color binding in that process, rejects unbound
or unknown targets, and records its required canonical replay as a separate metadata warmup.

The prepared `output/eid-history-20260905/history-api-matrix.json` selects one observed Draw event
for each D3D11/OpenGL/Vulkan demo. `run_history_api_matrix.ps1 -BuildLabel '<tested build>'` runs those
entries in separate hidden processes, sequentially, and writes a new output directory. It requires
stable matching canonical thumbnail hashes, restored anchors, and exactly one headless
`HistoryPreview` with `snapshotroute=0`. A failure or timeout stops before another API starts;
a timed-out process is left identified for inspection, not silently killed. This runner uses the
GPU when invoked; preparing the JSON or parsing its PowerShell AST does not run the matrix.

The supplemental `history-api-current-state-matrix.json` uses each same Draw as both anchor and
History event and selects `--history-path both`. Pass it through the runner's `-Matrix` option.
Its optional acceptance flags additionally require `currentstate=1`, zero replay/restore intervals
and backend calls for the direct preview, no backend replay in any snapshot phase, a distinct
snapshot resource reused on the next resolve, and identical RGB from the two display paths.
Canonical controls remain mandatory. These requirements are isolated from the original matrix.
Native GPU versus portable proxy snapshot identity remains explicitly unknown without backend
evidence; setting `requireNativeSnapshot` fails closed until such proof is supplied.
`test_history_matrix_acceptance.ps1` exercises these acceptance checks without loading RenderDoc.

`RENDERDOC_EID_HISTORY_PATH` / `--history-path` selects `transient` (default), `snapshot`, or `both`.
The default measures `DisplayTextureAtEvent` on a headless output, then times RGB readback separately.
Older builds may internally take a snapshot even for this headless call; record the actual
`HistoryPreview.snapshotroute` when available. The optional persistent-snapshot control measures its
first resolve, display/readback, then a repeat resolve, with a conservative total source-byte budget.
The public API does not identify native GPU versus portable CPU-upload snapshots. This is explicit
History analysis and does not authorize replacing interactive EID replay with stored images.

Version 2 additionally compares one representative historical thumbnail with two independent forced
canonical replays, using the same resource, subresource, display settings and output dimensions.
A stable canonical mismatch fails; a changing canonical image is reported as inconclusive. The
anchor is restored even when the control fails. `RENDERDOC_EID_HISTORY_VALIDATE=0` explicitly skips
this control and records `notRequested`; it must not be presented as pixel correctness passing.
The report distinguishes `measurementComplete` from `thumbnailValidation.status`.

`RENDERDOC_EID_HISTORY_REPEATS`, `_MAX_EVENTS`, and `_MAX_SECONDS` control repetitions, History sample
count and a soft total budget (300 s by default). The native `OpenCapture` call has no safe Python
cancellation; supervise `capture.openReplay` externally at 120 s if a hard deadline is needed.
Budget checks occur between native calls and cleanup still runs. Each report records process ID,
UTC phase start, and pending phase so an external supervisor can identify its own test process.

The backend timing fields are CPU wall intervals. `ApplyInitial` includes initial-content recording;
its descriptor, CPU/GPU-copy, barrier, residency and allocator detail explain host work. `ContextReplay`
includes command traversal/deserialization, API recording and any enclosed waits/submits.
`ExplicitWait` and `ExplicitSubmit` are overlapping subsets, not additional costs to add to those
phases; only section self times plus `unscoped_ms` form a disjoint total. Submit duration does not
measure GPU execution. `HistoryPreview` splits API replay/render/restore, while RGB readback remains
a separate script phase. A snapshot resolve includes target replay and anchor restoration; its
non-replay residual includes allocation/copy recording/bookkeeping and is not a GPU timestamp.
Waits outside an active backend replay scope cannot be recovered from the driver detail alone.

CPU-only tooling tests: `python -m unittest discover -s util/perf -p test_eid_history_profile.py -v`.

## Interactive scrub benchmark

`eid_scrub_benchmark.py` runs inside qrenderdoc's bundled Python, using `--python`. Start a fresh,
otherwise idle qrenderdoc instance, keep its Texture Viewer visible in ordinary RGBA mode, and
leave render-target History closed. Do not run another instance against the same timing logs or
interact with the UI during the measurement. Set `RENDERDOC_EID_TIMING=1` before process startup.

Set `RENDERDOC_EID_BENCHMARK_EVENTS` to a comma-separated list of at least two distinct leaf action
EIDs from the capture; no capture-specific event IDs are built into the script. Then run:

```powershell
# Set these two variables to the capture and action EIDs being compared.
$env:RENDERDOC_EID_BENCHMARK_CAPTURE = $capturePath
$env:RENDERDOC_EID_BENCHMARK_EVENTS = $eventIds -join ','
$env:RENDERDOC_EID_TIMING = '1'
$env:RENDERDOC_EID_BENCHMARK_OUTPUT = "$PWD/output/eid-benchmark.json"
$env:RENDERDOC_EID_BENCHMARK_BUILD_LABEL = 'Development /Od' # Or the actual optimized configuration.
$env:RENDERDOC_EID_BENCHMARK_QUIT = '1'
& ./x64/Development/q_render_soco_doc.exe --python "$PWD/util/perf/eid_scrub_benchmark.py"
```

The `renderdoc-eid-scrub-benchmark/v3` JSON retains `sequential`, `burst`, and `presentation`, and
records `held.forward`, `held.reverse`, `held.continuous`, and `held.release`. Held motion runs
first. Controlled forward/reverse steps wait for a new preview and scrub completion with the same
EID/generation and a real driver `ScrubReplay`; any `ScrubSnapshotHit` fails the fixture. Each step
then drains the replay queue and compares the controller EID and public pipeline state with the
GUI's current/selected EID and pipeline. Displayed EID/resource properties written after C++
`Display()` returns must match the viewer binding. The public pipeline comparison covers shader
identities and output targets; it does not claim to validate every panel's rendered pixels.

The continuous phase keeps sending different EIDs for
`RENDERDOC_EID_BENCHMARK_CONTINUOUS_MS` (default 600; must be positive). At least two matching
published views must be observed while input is still active; a single frame after requests stop
does not pass. `held.continuous` records duration, call count, `publishedWhileInputActive` observations
and the final completed target. Release ends the gesture and drains/checks current state; it does
not require a new commit record or defer panel refresh until mouse-up.

`OnScrubEvent` alone is not a completion signal: its first callback precedes replay. The matched
logs plus displayed-state checks establish completion of the application's display path. They do
not measure monitor scanout or prove pixel differences between two EIDs. The script checks that
the Texture Viewer stays visible; a legally unbound followed output is recorded as unbound, with
no placeholder resource. Use independent image/hash validation for content correctness.
`held.forward` means the script's first traversal; capture initialization may already have prepared
resources or analysis metadata, so that label alone does not prove a cold device/cache state.

`completionObservedMs` includes GUI dispatch and log polling; `settledMs` additionally includes a
replay-queue drain and state checks. Controlled held steps query public live state without texture
readback; the continuous phase checks published views while requests keep arriving.
The default poll interval is 5 ms and can be changed with `RENDERDOC_EID_BENCHMARK_POLL_MS`; use the
logged replay timings for finer resolution. The first sequential target may already be committed
after held motion and is explicitly labelled `noOpAlreadyCommitted` rather than counted as replay.
Set `RENDERDOC_EID_BENCHMARK_HELD_ONLY=1` to finish after forward/reverse/continuous and release.
Optionally set `RENDERDOC_EID_BENCHMARK_TEXTURE` to a decimal captured texture ResourceId to open a
locked view of that actual texture before the sweep. The script resolves the real ResourceId from
capture metadata and records its identity and dimensions in the JSON. This measures one fixed
texture across events, which differs from automatically following each event's output; the default
does not change the binding. A locked tab remains open if the instance is kept open after the test.

Optional environment variables are `RENDERDOC_EID_BENCHMARK_TIMEOUT` (per-step seconds, default 30),
`RENDERDOC_EID_BENCHMARK_UI_LOG` and `RENDERDOC_EID_BENCHMARK_DRIVER_LOG` (defaults beside the executable).
The script reads only log bytes appended after each request, rejects log truncation, reports errors
and partial results as JSON, and restores the original event and display settings. The output's
parent directory must already exist. Without `RENDERDOC_EID_BENCHMARK_QUIT=1`, the instance stays open.
Failure reports preserve published state, completion/generation checks and the last 20 new UI
and driver log lines; asynchronous fatal diagnostics have a 1.5 s collection limit. Continuous
`stateSamples` record publication serial, eight shader stages and render targets, capped at 4096
samples. State/EID must remain stable within one GUI publication serial; a new serial may
legitimately replace state at the same EID. `pipelineInstability` includes before/after witnesses.
The integration fixture `replaylab/tests/fixtures/qrenderdoc_eid_scrub_smoke.py` also uses real
held-arrow replay and matching display/controller/pipeline checks with `RENDERDOC_EID_TIMING=1`;
captures lacking five consecutive leaf actions report `heldArrowUnavailable` explicitly.

## CBuffer mutation correctness

`eid_cbuffer_validate.py` is a separate correctness fixture with GPU readbacks. Use a fresh local
D3D11/D3D12 qrenderdoc process without existing edits, select an actual graphics draw EID, and first
run `RENDERDOC_EID_CBUFFER_MODE=probe` (the default). The probe enumerates that draw's actual shader
reflection and CBV bindings, reports supported finite float candidates, and checks that hashes of
its bound color render targets are identical across two forced replays. It does not modify values.

```powershell
$env:RENDERDOC_EID_TIMING = '1'
$env:RENDERDOC_EID_CBUFFER_CAPTURE = $capturePath
$env:RENDERDOC_EID_CBUFFER_EID = $drawEventId
$env:RENDERDOC_EID_CBUFFER_AWAY_EID = $otherEventId
$env:RENDERDOC_EID_CBUFFER_MODE = 'probe' # Change to 'validate' in a new process for the round trip.
$env:RENDERDOC_EID_CBUFFER_OUTPUT = "$PWD/output/eid-cbuffer.json"
$env:RENDERDOC_EID_CBUFFER_QUIT = '1'
& ./x64/Development/q_render_soco_doc.exe --python "$PWD/util/perf/eid_cbuffer_validate.py"
```

Validation opens a locked view of a real bound color target and performs real A-to-B-to-A
scrubbing. Every leg must emit a new `ScrubReplay`; snapshot and virtual commit records fail the
fixture. On return to A it verifies the live shader/CBV binding and all baseline target hashes,
then calls `SetEventCBufferOverride` directly against that real state without another event switch.
Each candidate changes only reflected float-vector components or the first three diagonal entries
of a square float matrix, using a full copy of the original CBV window. The native setter validates
the exact shader, b-register, register space, buffer, offset, and window. Root CBVs use the rounded
reflected block size; descriptor CBVs use the full descriptor window. Unsupported bindings are
reported, including indirect root CBVs, rather than substituting fabricated resources or offsets.

A passing validation requires fresh real replays before/after editing and after clearing,
advancing output generations on set/clear, at least one actual color-target hash change,
unchanged captured CBuffer bytes, and restoration of every bound color-target hash after
clearing. Before each edited candidate, an identity control uploads the complete unchanged CBV
window through the same override API; all target hashes must equal the baseline both with that
shadow applied and after clearing it. This separates an actual value change from a shadow-binding
or replay-path side effect. Every attempted override is cleared in `finally`, including candidates with no visual
effect. Float meanings are not inferred from names; the JSON includes exact reflected paths,
offsets, before/after values, binding identities, hashes, and replay evidence. These hashes validate
actual capture resources. The result schema is `renderdoc-eid-cbuffer-validation/v2`.

`RENDERDOC_EID_CBUFFER_MAX_CANDIDATES` bounds attempts (default 24), and
`RENDERDOC_EID_CBUFFER_CANDIDATE` can select one `candidateIndex` from the probe. The probe reports
the first 64 candidates. `RENDERDOC_EID_CBUFFER_MAX_READ_MIB` bounds estimated color-target readback
storage (default 256 MiB); the test fails instead of silently omitting a bound RT when the budget
is insufficient. Selected mip/slice/sample 0 are reported. If `AWAY_EID` is omitted, the
last distinct draw is used. `RENDERDOC_EID_CBUFFER_BUILD_LABEL` records the tested build. An unchanged
hash is an inconclusive candidate. Use a separate process
from latency measurements and keep the texture output visible; do not interact during validation.
For stripped shader reflection, the probe reports every block's rejection and a bounded type
summary instead of interpreting `unknown` integer arrays as float fields. Set
`RENDERDOC_EID_CBUFFER_DUMP_SHADERS=1` to save the bound stages' original disassembly beside the
OUTPUT JSON for further read-only inspection. No guessed offset/type mutation fallback is enabled.

For a separately reviewed stripped-shader case, `RENDERDOC_EID_CBUFFER_NATIVE_F32_PROOF` may point
to a `renderdoc-eid-native-f32-proof/v1` JSON fixture. This explicit single-float entry requires the
actual shader/resource/CBV identity and window, matching native shader hash, and unchanged exact
disassembly lines proving a fixed F32 load, component extraction, and output use. It remains
separate from typed reflection candidates and changes exactly four bytes. The current fixture's
value rule is specific to its proven `sat(value * 0.125)` output: halve an interior value, otherwise
write 1. The default does not enable this path, and the evidence must be reviewed for each shader.

### Command replay decomposition and real checkpoint validation

With `RENDERDOC_EID_TIMING=1`, the backend retains the timing version 2 fields and adds
`chunk_timing_version=1`. `replaySetup` measures stream/partial-state setup, `chunkFraming` measures
ReadChunk/EndChunk, and `replayBookkeeping` measures loop/cursor and tail work. Around ProcessChunk,
the first successful `SERIALISE_CHECK_READ_ERRORS` separates `chunkDecode` (parameters plus resource-ID
resolution) from `chunkExecute` (replay wrapper state maintenance and native API recording/execution).
Routes without a checked boundary are `chunkUnclassified`. All section self times remain disjoint.
Explicit wait/submit are nested subsets; implicit driver waits inside native calls remain included.

Each ReplayLog emits at most 12 `api=ReplayChunk` type summaries plus `chunk=other`, with inclusive
and self time, boundary counts, and nested wait/submit. No per-command log I/O is performed.
`python util/perf/analyze_eid_chunks.py <driver-log> --driver D3D12 --type WithoutDraw --eids 29385,62176`
aggregates these records; totals cover the selected calls, not a frame rate. Keep profiling settings
consistent in A/B runs. When disabled, scopes only check thread-local state and the parameter callback
is null. ActiveReplay no longer issues loading-progress callbacks; required uploads, descriptors,
barriers, queries and draw/dispatch execution remain unchanged.

`eid_checkpoint_profile.py` validates resource-state checkpoints through the bundled standalone
Python runner. Supply `--capture`, two to eight `--events`, and a new `--output` through
`run_bundled_replay_python.ps1 -ScriptArguments`. `--budget-mib` and `--max-checkpoints` default to
zero (automatic policy). `--max-read-mib` defaults to 512 and fails instead of omitting bound outputs.
`--min-hits` defaults to 1; zero permits canonical-only results explicitly labelled without reuse.

The sequence disables/releases, enables, visits sorted events forward/reverse/forward again with
`force=False`, then performs two independent `force=True` references per event. Force must leave
checkpoint builds/hits/retained versions unchanged. Every visit records status JSON, replay/readback
timings, output bindings/shader IDs/topology, and native full-subresource hashes for all bound
color/depth views, including array slices and samples. A 3D readback covers the whole mip volume.
It checks budget/count limits, disable release, re-enable, and output equivalence. A final one-byte
budget must retain no checkpoint storage and still reproduce the forced references through canonical
fallback. Unstable canonical
references are inconclusive; nonempty wrong pixels or mismatched bindings fail. This does not check
all UAV/buffer contents or pretend event-specific CBuffer overrides are shared persistent edits.
Running it uses the GPU and must be scheduled serially. CPU-only parameter, evidence and verdict tests are in
`test_eid_checkpoint_profile.py`; native execution/C++ timing tests require the matching build.
The JSON retains the last partial output witness if validation fails, including actual view ranges
and texture metadata. Before native shutdown, it preserves the diagnostic log as `.native.log`
(`nativeLog` / `nativeLogBytes` in the report); timing slices remain separate.

`--validate-mutations` compiles a temporary pixel shader after checking the backend's advertised
target encodings: HLSL for D3D11/D3D12, GLSL for OpenGL/Vulkan. The latter path accepts only evidence
for a `main` fragment entry point with one unpacked float4 color output at location 0 and exactly
one float-compatible bound RT0. Missing reflection, integer or multiple outputs, and unknown ABI
are rejected with the observed `fixtureEvidence`; the script does not guess bindings or rewrite
capture shaders. It uses desktop GLSL 330 for OpenGL and Vulkan GLSL 450, and reports compilation
failures explicitly. This path covers the corresponding Simple_Triangle captures, not arbitrary
shader interfaces or GLES shaders.

The script replaces the captured shader globally, checks visible change against two independent
forced references, removes the replacement, and checks recovery against the stable original
references. Each mutation state makes two away/back visits and must observe actual checkpoint
reuse; a canonical-only result does not pass mutation coverage, even with `--min-hits 0`.
Replacement/removal must increment invalidation counters and discard dependent old checkpoints.
On the D3D11 simple-triangle fixture it
also sets and clears an event-local CBuffer at an explicitly unbound b13. A failed expected binding
is rejected; no existing buffer is overwritten. This is a checkpoint invalidation test, not a
shared CBuffer editor. A `finally` block attempts every required override/removal/shader cleanup
independently and records failures.

Checkpoint policy is configured through `IReplayController.ConfigureReplayCheckpoints(enabled,
byteBudget=0, maxCheckpoints=0)`. Zero selects a capture/resource-size budget of 64 MiB to 2 GiB
and two or three retained points; drivers may further cap it against device memory headroom.
With `RENDERDOC_REPLAY_CHECKPOINTS` unset or set to `auto`, capture loading starts with ordinary
replay and observes naturally requested visits. Repeated sufficiently stable visits permit a bounded
checkpoint trial; measured restoration/continuation/completion savings must repay its construction
cost before more points are admitted. Ordinary replay below 16 ms does not start a trial. Fixed-EID
shared-buffer edits measure their own first-consumer prefix, without that ordinary-navigation
threshold. The automatic budget is at most 512 MiB (64 MiB for the initial buffer-edit trial).
`0` disables checkpoints; `1` explicitly enables the capture-size policy. Calling
`ConfigureReplayCheckpoints` also selects explicit control for that controller. The profiling tool
below uses this explicit API; `eid_auto_checkpoint_profile.py` tests the default policy without ever
calling Configure. A correct restore alone is not evidence of lower latency. All entry points
still support canonical replay; `SetFrameEvent(event, true)` deliberately bypasses both checkpoint
creation and restoration, retaining existing points for independent A/B validation. Status JSON
comes from `GetReplayCheckpointStatus()` and reports planned candidates separately from actual
saved events, allocation, sharing, hits, misses and rejected boundaries. A candidate does not imply
that the backend can restore it. Shared bytes describe retained/shared resource contents, not
cached Texture Viewer images, and allocation is checkpoint storage rather than total process VRAM.
Repeated identical configuration preserves live points and counters. Add `--verify-reconfigure`
to check this, and `--steady-rounds 8` to compare additional alternating ordinary visits with points
enabled and disabled. The D3D12 dirty-resource optimization is independent: use
`RENDERDOC_D3D12_DISABLE_DIRTY_RESTORE=1` for its baseline or
`RENDERDOC_D3D12_DIRTY_RESTORE_SHADOW=1` to count prospective skipped bytes while still restoring
all original content. Never mix either measurement with a forced replay, which deliberately resets
the dirty proof. Automatic natural-reference visits disable only checkpoint reuse and retain this
ordinary dirty-resource optimization. The private proxy revision is 5 and must match at both ends.
`RENDERDOC_D3D12_DISABLE_DIRTY_WRITE_CUTOFF=1` restores whole-command-list potential-write
tracking; the default uses the actual submitted occurrence and a conservative executed prefix.
`RENDERDOC_D3D12_DISABLE_SPARSE_DIRTY_CLOSURE=1` restores the old global sparse-write fallback.
The default sparse path is allowed only with a complete initial/full-frame physical-heap closure;
its plan includes the alias writes caused by restoring sparse initial contents. It does not relax
checkpoint alias/sparse eligibility. Measure each change against its own switch in the same build.

Global resource/shader replacement invalidates all checkpoint versions. Existing event-local
shader/CBuffer overrides invalidate the corresponding suffix, and `OnlyDraw` shadow bindings are
never saved into a reusable prefix checkpoint. The original capture initial-state source is kept.
The shared parameter identity is `(canonical buffer ID, absolute byte offset, byte length)`,
where the absolute offset includes the binding window and reflected member/array offsets. A
persistent edit must be reapplied after captured uploads to that same range; changing only initial
bytes would be overwritten by later Map/Update/copy commands. Until an edited write-dependency
index is trustworthy, modified shaders require conservative writable-resource snapshots rather
than treating the original capture usage table as proof that data stayed unchanged.

### Texture Viewer tab and history regression

`replaylab/tests/fixtures/qrenderdoc_history_smoke.py` runs in a fresh owned qrenderdoc process
with `--ui-python`. Set `RENDERDOC_HISTORY_SMOKE_CAPTURE` to an explicitly selected capture,
`RENDERDOC_HISTORY_SMOKE_EID` to a bound draw, `RENDERDOC_HISTORY_SMOKE_OUTPUT` to a new JSON
path, `RENDERDOC_HISTORY_SMOKE_TAB_SWITCH=1`, and `RENDERDOC_HISTORY_SMOKE_QUIT=1`.
`RENDERDOC_HISTORY_SMOKE_TEXTURE` can select a particular actually bound color target.
The fixture checks nearby-first bounded history loading, actual Qt mouse events for single preview
and double-click navigation, scrolling, ordinary and rapid tab changes, and a deliberately delayed
old GUI completion. Custom PySide wrappers may require the real tab bar's key handler; the report
records which input route was used. No private history callback or direct tab selection is used.

The default witness reads complete native desktop pixels and requires an unlocked, unobscured
desktop. Lock-screen pixels are not application evidence. For background regression, set
`RENDERDOC_HISTORY_SMOKE_WITNESS=output` and `RENDERDOC_EID_TIMING=1`: the opt-in
`CapturePresentationWitness` Qt slot reads the existing Texture Viewer output via
`IReplayOutput::ReadbackOutputTexture`, without `Display`, a new output, or resource rebinding.
The one requested image is cleared after the fixture reads it. This validates application output
and stale-request publication, not OS composition/scanout; its diagnostic readback costs also
make the tab-case elapsed times unsuitable as production latency measurements.
The fixture uses `perf_counter` and records slow Python timer intervals with the previous callback's
duration. With timing enabled, native history timers and bounded GUI-operation spans are also
reported. These distinguish queue/display evidence from validation overhead; a delayed native timer
alone does not identify a GPU stall or exclude a GUI Python slot waiting for the GIL.

### Persistent shared Buffer editing

The native inline CBuffer editor exposes a diagnostic `lastTiming` map on the
`cbufferLiveEditor` widget. It separates input coalescing, replay queue wait, initial lane readback,
binding validation, native mutation/replay/display, and final viewer publication. `inputSamples`
counts coalesced UI inputs and `nativeChanges` counts actual native generation advances. The
`qrenderdoc_cbuffer_live_edit_smoke.py` fixture records these maps without counting its verification
readbacks as edit latency. With `RENDERDOC_EID_TIMING=1`, `cbuffer_publication` UI records split the
pipeline snapshot and viewer notification costs; driver `shared_buffer_patch` records already split
backend commit, replay and display. Neither the fixture's complete capture-loading time nor the
native setter alone is input-to-preview latency.

For a same-build comparison, start a separate owned process with
`RENDERDOC_CBUFFER_EDIT_LEGACY_REFRESH=1` to retain the previous 80 ms coalescing interval, unbounded
Root-CBV remainder reads, repeated alias parsing and selected-EID notifications. The default uses
16 ms initial coalescing and keeps one replay in flight. Ordinary reflected CBuffer reads are
bounded by both reflection size and the real binding; custom formats and binding identities remain
unchanged. Alias parsing is reused only for a value publication with identical project, shader,
layout, binding, replacement status and unchanged alias file. Capture/event/source changes and file
watcher refreshes invalidate that reuse. This does not cache rendered output or skip GPU replay.
Run A/B processes serially with the same capture, selected EID, visible viewers and build flags.

`SetSharedBufferPatch(buffer, absoluteOffset, bytes)`, `ClearSharedBufferPatch(buffer,
absoluteOffset, byteLength)` and `ClearSharedBufferPatches()` operate on captured storage across
all consuming events. They return an empty diagnostic string on success. Each actual byte change
advances `GetSharedBufferPatchGeneration()`; an identical byte update does not replay. Overlapping
edits are ordered layers: clearing an exact layer reveals the preceding edit or original contents.
Captured Map/Update/copy commands do not silently clear accepted edits. Unsupported buffer types
or write modes are rejected before replacing the active configuration.

`eid_shared_buffer_profile.py` validates native bytes, neighboring bytes, every bound color/depth
output, away/back navigation, explicit cleanup, and two checkpoint-bypassing reference replays.
`--fixed-values-hex` accepts four to sixteen distinct same-length values and inspects each setter's
already-rendered output before any reference replay. `--require-fast` additionally requires two
steady setter checkpoint hits strictly before the proven earliest consumer. Event zero is eligible
only with native `hasInitialCheckpoint=true`; a default zero lastResumeEvent is not a hit.
`--remote [host]` uses a separately owned compatible server; `--remote-server-pid` can verify its
loaded native module identity for a same-build local comparison. CPU tests are in
`test_eid_shared_buffer_profile.py`.

Remote control protocol revision 5 carries initial-checkpoint identity and the earliest affected
event along with bounded patch payloads, configuration, status and scoped replay intent. In addition
to force bypass and automatic natural-reference requests, foreground shared-parameter setters request
`PrefixOnly`: they may establish a missing pre-consumer anchor but do not synchronously rebuild
downstream checkpoints. This restriction does not apply to ordinary EID navigation or idle work.
Do not combine a new client with an old `renderdoc.dll` or `system_load.dll` server build.

### Idle checkpoint maintenance

`RunIdleCheckpointMaintenance(expectedEvent, expectedMutationGeneration, expectedBufferGeneration)`
performs at most one replay work unit and returns a JSON receipt. Stale tokens, a duplicate request,
analysis, disabled checkpoints or missing materialized state perform no native replay. Automatic
mode rebuilds only a previously measured useful point; explicit mode uses its authorized policy.
The Qt scheduler waits 750 ms and puts maintenance behind pending foreground work. A native GPU
submission already in progress is not preemptible; one work unit is not a fixed millisecond budget.

Run `eid_idle_checkpoint_profile.py --discovery <fixture.json> --fixed-values-hex <distinct-values>
--output <new.json> --require-idle-build --require-neighbors-unchanged` for strict native acceptance.
It checks stale/no-op intervals, actual builds, unchanged current EID/bytes/neighbors/bound color and
depth outputs, forced references and clear-to-original. A raw planned candidate can map to an
already saved native boundary, so only an actual new native build satisfies the strict flag.

### Persistent Shader value debugging

Bread's workspace preview replaces the original Shader resource until canceled. EID navigation
does not compile, migrate or reinstall that target. All draws using the Shader participate in
normal replay, so later shaders can consume its actual RT output. The captured blend/write-mask
state still applies. Independent RGBA32F event previews cannot propagate to later draws and are
not available in this scope. Cancellation restores the exact prior native replacement, including
an existing edited Shader; transient debug targets are not saved into the project or capture.

`shader_visual_debug_persistent_smoke.py` runs with `--python` in a private application profile.
Set `BREAD_SHADER_VISUAL_PERSISTENT_CONFIG` to a JSON file containing `repository`, `output`,
and `captures` (each with `name`, `capture`, `sourceFile`, `language`, and `entryPoint`). The
Shared_Buffer fixtures must contain four draws sharing a Shader and RT. Acceptance starts at
a non-draw EID, reads real pixels across earlier/later events, checks that compilation happens
once, compares/restores a borrowed prior target, and verifies a cached restart. Optional
`serverURL` uses an already running compatible ReplayProxy server with no local fallback.
The launcher owns its server/process lifetime; remote tests do not establish Android or Linux
GPU coverage unless actually run on those devices.

`shader_visual_debug_persistent_workspace_smoke.py` runs with `--ui-script` and
`BREAD_SHADER_VISUAL_DEBUG_WORKSPACE_CONFIG` (`capture`, `sourceFile`, `output`). Use private
fixture copies whose source entry point matches the captured reflection. It exercises the real
workspace toolbar, non-draw startup, earlier/later navigation, native ownership and cancellation.
Delegating observers count compiler calls and editor marks without replacing their implementation.
Screenshots record the highlighted source; source/capture bytes must remain unchanged.

Native tests tagged `[shared-shader]` cover the replacement getter, cross-EID ownership,
checked remote request validation and old-peer capability rejection. Build both client and server
against the updated API. Existing protocol payloads remain compatible; an old peer without the
new checked-replacement capability explicitly rejects this operation.

## Bounded replay caches

The ordinary replay path is authoritative. Optional reuse requires matching resource,
mutation, event and command identities. Unsupported cases retain ordinary replay;
these changes do not guarantee identical speedups for every API, driver or capture.

- D3D12 forward continuation requires the previous successful ordinary final draw.
  Shader replacement, resource edits, debugging and analysis invalidate or bypass it.
- Backward replay and CBuffer edits may use proven upstream checkpoints. Automatic
  policy starts with metadata, builds during idle, measures benefit, and evicts
  unprofitable points. Default checkpoint storage is capped at 512 MiB.
- CBuffer edits invalidate affected downstream results. Read-only ranges and
  ExecuteIndirect arguments are bounded and typed; missing or conflicting evidence
  selects full restoration. Repeated values may retain only unaffected upstream data.
- Vulkan initial-image copies use immutable captured sources with exact subresource
  coverage. Storage defaults to 768 MiB with a 256 MiB memory-budget headroom.
  Missing budget support, allocation failure, sparse/external/multisampled images
  and unsuitable formats retain the original upload path.
- Vulkan decoded descriptor writes cache parameter decoding, not mutable descriptor
  contents. Native writes/tracking still execute. The cache is capped at 4096
  entries and 64 MiB and is invalidated at replay resource/edit boundaries.
- GPU host-write sources are capped at 64 MiB/4096 entries. Aliased image hazards,
  unsupported memory/queue topology, validation and debug callbacks retain the
  original path. Multiple queues retain conservative synchronization.
- Prepared D3D12 native command-list reuse remains explicitly opt-in; ordinary
  command decoding and state continuation provide benefits independently.
- Shader visual-debug caches own constant-block values and raw bytes, not borrowed
  native ShaderReflection wrappers. Mutation/source/capture identities invalidate
  cached plans and targets, and target count/bytes are bounded.
- Visible thumbnails draw through native GPU outputs. Optional UI details are
  deferred/coalesced. Diagnostic pixel witnesses deliberately read back pixels and
  must be excluded from interaction timing.

### Regression tools

All audit inputs are explicit local developer captures, not distributed game data.
Run only against a disposable runtime under this checkout's `output` directory.
The harness owns its child process and isolated configuration/profile; it does not
attach to an existing user application or overwrite an input RDC. Do not use
repository-contained pytest temporary directories.

`replay_audit_native_reuse.py --label LABEL --runtime RUNTIME --capture CAPTURE`
exercises deterministic CBuffer edits/reset, compatible Shader replacements,
debugging, historical output and the native editor save/error route. Add
`--checkpoints` to require a real checkpoint hit and `--canonical-shader-replay`
to compare a replacement with conservative replay.

`replay_audit_ai_memory.py --label LABEL --runtime RUNTIME --capture CAPTURE`
repeats compile/apply/debug/rollback and real IPC reflection reads. A small fixture
may use `--skip-descriptor-scan --no-timing`; this excludes million-entry descriptor
coverage and reports that limit. Check post-warmup private bytes/handles as well
as `--unique-shader-variants` when measuring distinct compiled programs, as well
as exact restoration on deterministic fixtures. A stable bounded run does not
prove every capture and every future operation leak free.

`silver_gpu_reuse_fixture.cpp` creates owned D3D12 test captures, including
variable-count/zero-count indirect draws. It is a synthetic correctness fixture,
not evidence for universal game speedups. `check_interactive_portable.sh` checks
changed GUI translation units using a separately configured Linux build tree.

Record complete input-to-presentation time with detailed tracing disabled.
Compare controls on the same machine/binary/settings and report GPU contention.
A per-stage timer moving earlier does not establish an end-to-end improvement.
Game output can itself be nondeterministic; report repeated reference stability
separately from cache-vs-reference differences.

### D3D11 and OpenGL/GLES acceptance

`replay_audit_all_api.py --label LABEL --runtime RUNTIME --capture CAPTURE`
compares actual EID navigation with stable forced references and native output
presentation in a disposable profile. `--mutations` exercises the native CBuffer
editor, 120 timed inputs, and byte/pixel restoration. Intermediate input values
may coalesce; report the final input-to-publication latency separately from the
duration of the drag and the observer's polling delay.

OpenGL/GLES restores pending attribute/output bindings and uniforms even when
unchanged linked locations let it skip relinking. Use `--force-program-relink`
for a same-binary control. Buffer-only write evidence is independent of texture
write evidence; unknown writes and aliases still prevent static sharing.

D3D11 and OpenGL/GLES may preserve a GPU initial source before editing a proven
frame-read-only constant buffer whose capture omitted initial contents. These
sources are capture-owned, limited to 256 objects and 64 MiB charged in 64 KiB
units, with a 16 MiB per-buffer limit. Subsequent values reuse the source; edited
results and dependent checkpoints retain their existing invalidation rules.

`d3d11_initial_restore_fixture.cpp` supplies an owned native fixture with many
constant-buffer consumers. The audit's `--initial-command-cache` flag is only
for a separately supplied experimental runtime and requires real native hits;
the proposed D3D11 initial-command-list experiment is not enabled or included in
the production driver because complete UI timing showed no reliable gain.
