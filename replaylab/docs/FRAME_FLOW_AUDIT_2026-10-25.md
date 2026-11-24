# Frame-flow follow-up audit — 2026-10-25

This audit inspects the existing Bread AI records and published evidence without
running a model, regenerating a diagram, modifying a capture, or changing its
sidecar. Event and resource IDs below identify this incident only; they are not
analysis targets or product requirements for another frame.

## What actually ran

The main task was `156d954de45a4ef39c45e2d06fbb93de`, from 11:29:25 to
12:01:39 Asia/Shanghai (32 min 13 sec). Its JSONL and report state are under
`%LOCALAPPDATA%/ReplayLab/ai-tasks/`.

The Host report records:

| Operation | Calls | Errors | Total Host time |
| --- | ---: | ---: | ---: |
| Prepare frame outline | 1 | 0 | 2.4 s |
| Read frame-flow sections | 9 | 0 | 3.7 s |
| Inspect exact stage bindings/state | 71 | 0 | 102.0 s |
| Read managed evidence files | 44 | 0 | 31.2 s |
| Capture exact event texture | 2 | 0 | 6.6 s |
| Publish diagram | 9 | 4 | 592.4 s |
| Inspect resource usage | 4 | 0 | 2.6 s |
| Read current context | 1 | 0 | 0.3 s |
| Inspect native API parameters | 3 | 0 | 2.0 s |
| Read raw texture bytes | 1 | 0 | 0.4 s |

These times are tool totals, not a breakdown of GPU time. Publication includes
validation, caching and persistence. The remaining wall time cannot be assigned
to rendering from these records.

The 44 Host evidence reads include 23 image manifests and 21 actual PNG image
deliveries. Two texture captures also returned image content. Thus this task did
receive images, not only image paths. Image transport alone does not establish
that every conclusion was visually verified.

The UI records 214 `read_evidence_file` calls (213 completed, one failed). This is
not 214 images: the task reader also pages retained large tool responses locally,
without a Host evidence-file invocation. Tool names alone cannot establish a new
Shader source read. The Host tool list contains no new Shader source/disassembly
request, mesh geometry read/export, vertex debugging, or asset search. Stage
inspection supplies input layouts, workload, bindings and drawing state; it does
not supply the actual mesh silhouette or pre/post-VS positions.

The task did refresh working pipeline observations and inspect representative
images. Most published calculation anchors and module grouping were reused. For
example, the recorded FFT module retained only its final reconstruction step;
its `processGraph` was absent. The scene-layer stage described the copy back into
scene color and refraction copy without investigating the earlier independent
layer's geometry in this run. This is evidence of incomplete semantic reanalysis,
not a reason to prescribe FFT, characters, outlines or any other technology in
every frame. A new analysis must follow the actual important inputs and versions,
revisiting broad grouping where distinct calculations or geometry justify it.

The final saved result contains 24 stages, 266 image entries (including channel
entries and repeated references), and two explicit process graphs. The Host
reports 198 persisted media files. Most absent subgraphs were absent from the
proposal itself, not merely hidden by the viewer.

Four publish attempts failed validation: twice because an image belonged to a
different capture/stage or unavailable attachment, and twice because a graph
node's event did not match its image. These checks should remain strict. The
agent corrected/retried and retained five successful publications.

## Why the final result remained incomplete

The final publication is
`6cda728b291117f7874346ba8a096e4146742cec43ca682a823d5bafbf88675e` in the
capture session `a13ecafc5637412e855acfd0d1512c3c` artifact directory. Its eleven
quality gaps comprise four resource-version gaps, four associated missing
integration gaps, and three agent-authored partial-investigation entries.

This was not the earlier empty-pipeline/fatal replay incident. All 71 stage reads
succeeded with valid bindings. Reopening the capture would not repair this
version predicate.

`frame_flow_algorithm.write_index` combined `action.outputs`, `depthOutput`,
`copyDestination` and observed outputs for every operation. Native utility actions
retain the preceding draw's bound targets, even when a clear or discard operates
on another resource. The index therefore invented intervening mutations.

Examples from the preserved native outline:

| Utility EID | Native actual destination | Inherited target wrongly treated as changed |
| ---: | ---: | --- |
| 62916 | 18993390 | 18993460 and depth 18993461 |
| 64932 | 18993391 | 19112000 and depth 18997289 |
| 65366 | 18993412 | 18993409 |

The agent obtained exact API evidence confirming these destinations. There was
no supported agent parameter to change the Host predicate, and an agent-authored
override would not be an appropriate fix. The native outline already contained
the required facts. The native D3D12 serializers assign the actual destination
for clear and discard in `d3d12_command_list_wrap.cpp`.

The two zero-instance indirect draws investigated in this task were already
correctly decoded and preserved: their native workload receipts record
`instanceCount=0`, and the outline records `hasWork=false`. They are excluded by
the existing writer predicate. They were not an additional cause of these final
version gaps.

Two integer shadow-atlas previews also remained near black. The raw texture
tool returned only 7,500 of 134,217,728 bytes, as its contract specifies. That
response had no offset/page interface and did not bind a complete conversion to
the producer EID. The event-image capture contract lacked an explicit bit-pattern
decode mode. Treating a uint containing float bits as an ordinary normalized
integer is not a valid depth display. Conversely, not every R32_UINT texture
contains float depth: interpretation must follow actual Shader stores/loads.
The transport limitation was real; nonzero prefix bytes cannot prove a complete
decoded image. Display interpretation and event-bound preview work are handled
separately from the writer-index correction.

## Corrections in this change

`frame_flow_algorithm.py` now uses the native exact destination for clear,
discard, copy, resolve and mip generation when one exists. It excludes inherited
targets and non-writing marker/Present events. Unknown destinations remain
conservative candidates; actual writes to the tracked resource still break the
version chain. No AI assertion, fixed event ID or technology name participates
in this decision.

The same error was present in `_draw_call_details_on_replay` and
`pass_classification._utility_destination_outputs`. Both now filter inherited
RTV/DSV aliases against the exact native destination. An identical alias retains
its original roles and subresource information. Missing exact destinations keep
the earlier conservative behavior.

An offline, read-only evaluation of the real saved outline and observations
reduces its eight algorithm gaps to zero with this predicate change alone. The
three agent-authored partial notes remain; the application AI must revisit and
update those conclusions. This audit does not change them or mark the user's
diagram complete. It also does not establish that all broad module summaries
are sufficiently investigated.

Tests cover unrelated clear/discard destinations, actual clear/copy writes to
the tracked result, missing destinations, graphics/compute writes, marker and
Present handling, zero-instance proofs, sidecar save/reload, and both bridge and
classification output inventories. Build/deployment and integrated runtime
verification are recorded by the coordinating task after source freeze.

The focused combined suite passed **231 tests and 12 subtests** on October 25:
`test_frame_flow_algorithm`, `test_frame_render_flow`,
`test_frame_render_flow_media`, `test_frame_flow_persistence`,
`test_bridge_composite_readonly`, and `test_pass_classification`.

## Earlier follow-up and remaining evidence boundaries

Task `4daafe1355a74260b13a51feb0161b3f` has 99 logged events, from 23:10:08 to
23:10:58 on October 24, and no terminal event in that file. Only prepare and
get-flow have completed Host receipts. The final request was `recover_replay`,
confirmed by the qrenderdoc console receipt at 23:10:58.769. Its owner native
process was **51768**, running the previously deployed **1.13.60** integration.

The native log `RenderDoc_2026.10.24_22.33.24.log` establishes a different failure
from the healthy October 25 task audited above:

1. At 22:39:02 the old D3D12 controller failed its GPU completion check. The
   recorded wait timed out (`258`), completed fence 6658 was below required 6675,
   and device status was `S_OK`. Subsequent live reads correctly rejected it.
2. At 23:10:59 `recover_replay` shut down the controller and reopened the same
   local RDC. D3D12 device creation failed with `E_INVALIDARG` at 23:11:01.
3. Windows Application Error **1000**, at 23:11:01, records an access violation
   in **renderdoc.dll** for PID `0xca38` (51768), exception `0xc0000005`, RVA
   **0xa0d597**. WER report ID is
   `12f5e835-9c26-4420-835f-4bb40e484aa5`. Thus the missing terminal is explained
   by the native application crashing during recovery, not by a recorded user
   close, a new question replacing the task, or a purely stale transcript.

The installed `renderdoc.dll` PE timestamp **0x6adb0f40** exactly matched the WER
module timestamp. Windows DbgHelp resolved that RVA with the matching PDB to
`ReplayController::Shutdown + 0x4e7`, original `replay_controller.cpp:3145`:
`m_pDevice->ClearEventCBufferOverrides()`. `CreateDevice` had returned failure
without a driver; newly added override cleanup dereferenced its null driver
before reaching the existing `if(m_pDevice)` shutdown guard. The fix guards all
driver-owned shader/override/target cleanup, while still clearing registries
and deleting the controller. A native Catch regression exercises shutdown of
a controller with no created device; the existing target-lifetime regression
continues to exercise cleanup with a live driver. This fixes the proven crash,
not the underlying GPU timeout or the native device-creation failure.

A second failure-path defect was found by reading the same recovery path:
`LoadCaptureThreaded` always showed a modal error dialog before clearing its
loading flag. After fixing the crash, an automatic recovery failure could
therefore wait for human dismissal. New explicit native
`LoadCaptureForReplayRecovery` returns `ResultDetails` without that error dialog;
the original five-argument manual `LoadCapture` retains its behavior. The bridge
records the native code/diagnostic as `reopenStatus`, rejects failure, preserves
drafts and saved images, and clears recovery guards in `finally`. It does not
restore the event or claim a new usable capture after a failed reopen.

Additional focused recovery tests passed **76 tests in 4.15 seconds**:
`test_replay_recovery`, `test_bridge_replay_recovery`, and
`test_server_frame_flow_recovery`. The integrated bridge regression returns a
native failure result, checks that the old modal API is never used, and verifies
diagnostic preservation and cleanup. The new isolated native fixture
`qrenderdoc_replay_open_failure_smoke.py` opens only its own invalid temporary
RDC and detects any unexpected modal. On the rebuilt native application it
passed **6 checks in 0.093 seconds**, returned `ResultCode.FileUnrecognised`
with the native diagnostic, and showed **zero error dialogs**. Receipt:
`%TEMP%/bread-replay-open-failure-native-161/result.json`. Its initial test-only
exit used `QApplication.quit`, which does not exit qrenderdoc's custom
`ctx.isRunning()` loop; a normal main-window close ended the process. The fixture
now closes that window itself. No GPU timeout was induced and no user capture
was changed. The coordinating task also ran the rebuilt native shutdown and
target-lifetime suite: **78 assertions in 4 test cases passed**.

An additional successful-recovery fixture launch (PID 12340, isolated profile
`%TEMP%/bread-replay-recovery-native-161`) produced no script receipt and never
opened its requested RDC. The parallel fixture investigation identified the
fresh isolated profile's first-run `AnalyticsPromptDialog`, before `ctx.Begin`
and `--ui-python`, as the startup blocker. This attempt is **not a passing
recovery test**. A main-window close did not end it within the bounded wait, so
only that verified, owned test process was stopped. No other host or application
process was stopped.

After copying only the completed isolated fixture's `UI.config` with
`Analytics_TotalOptOut=true` into another fresh test profile, the final packaged
**1.13.61** application passed **25 successful-recovery checks in 31.39 seconds**.
Receipt: `%TEMP%/bread-replay-recovery-native-161-optout/result.json`. PID 39812
reopened the same read-only RDC through the new native API, received native
`ResultCode.Succeeded`, established a distinct session, restored the selected
event, preserved the dirty native editor, saved historical diagram and unsent
AI attachment, and verified healthy pipeline reads. No replacements were
reapplied. The capture and all existing sidecar files retained their original
receipts. The test then closed its capture and native main window normally;
the process exited, with bridge unload recorded and no shutdown error. Only
the first health receipt was synthetic; no GPU fault or paid AI task was induced.

Static captured action metadata, existing Shader text and verified images can
resolve many questions. Fresh pixels, rejected-fragment results and dynamic
geometry may require replay. A history texture can show previous-frame data,
but a one-frame capture does not necessarily include the earlier frame's draws.
Neither image appearance, bound resources, asset names nor shader code in an
unexecuted branch by itself proves a rendered contribution. These boundaries
should produce targeted investigation and precise limitations, not a fixed
checklist, invented certainty, or repeated publication of unchanged evidence.

## Final build and deployment verification

The Development application and canonical integration were built and deployed
as **1.13.61**, with explain-event skill **v23**. The native build used `/m:2`,
an explicit `SolutionDir`, and `/p:ReplayOptimizeDevelopment=true`. Translation
synchronization, completeness, catalog compilation and source-literal gates
passed (5,305 complete entries, no unfinished entries). Runtime verification
matched 339 frozen source files, 20 packaged Python modules and seven bundled
skill/reference files before deployment. The deployment marker reports
`state=complete`, version `1.13.61`.

The broad frame-flow regression passed 810 tests. The final focused texture,
decode, channel, visual-review and mesh suite passed 142 tests and eight
subtests; these suites overlap and should not be added into a unique total.
The follow-up transcript suite passed 25 tests, including the native Qt/MSVC
fixture: 120 streaming structural updates had a maximum GUI gap of 66 ms.
The accepted-send path now clears stale transcript selection and restores
tail-follow, while unsolicited updates retain the user's reading position.

An isolated read-only test of the user's actual RDC passed 14 checks in 27.3
seconds. It read 132 post-transform positions and indices (44 triangles),
generated a wireframe image, and restored the original visible event. It also
read all 134,217,728 bytes of a 33,554,432-pixel R32_UINT atlas and decoded its
explicit float32 bit representation into a visible preview. The receipt records
the full source hash, pixel statistics and display range; the capture and its
existing sidecars were unchanged. The fixture's event selection is test data,
not a production discovery rule. Receipt:
`%TEMP%/bread-mesh-native-e9a7116a822048e89436c7c94863b855/result.json`.

No paid AI generation was run and no user frame-flow document was regenerated
or marked complete. These checks verify the installed tools, recovery and UI
paths; the user will exercise the revised evidence-driven analysis in Bread AI.

## Follow-up: explicit redraw is a new analysis

The user's correction exposed another independent weakness: the reanalysis
progress check accepted a cached binding read or caption change as a refreshed
stage. Shared instructions emphasized evidence reuse without clearly separating
that reuse from accepting old grouping and old blockers. Version 1.13.62 makes
the distinction explicit for native, API, main-agent and child-agent prompts,
and explain-event skill v24 describes targeted investigation of disputed edges.

The Host now tracks changes to the reasoning evidence rather than captions,
layout or binding-read counts. Renumbering, splitting, reordering or deleting
old evidence cannot by itself complete the new assessment. Existing
`investigation.evidence` can explain why the renewed analysis retains a correct
old conclusion; no extra tool, special wording or fresh GPU read is required.
Reading a cache no longer increments its observation revision. Immediately
after reanalysis starts, paged quality reflects its pending assessment instead
of presenting the prior publication's acceptance as current; the saved document
remains intact. These are progress checks, not semantic correctness proofs.

The assessment scope is the user's whole requested frame/range, including old
accepted stages. Raw source, images and observations may be reused where their
identities and applicability hold. Disputed dependency candidates call for
actual version/view/write investigation and forward/backward tracing, with
alternative static, live or unpacked-asset evidence chosen for the question.
The workflow does not prescribe techniques, EIDs, stage counts or a fixed tool
sequence, and an old blocker is reconsidered against current capabilities.

For this update, 73 reanalysis/provenance/recovery/persistence/version tests,
84 shared-agent guidance/autonomy tests and 41 skill/delivery tests passed, as
did the skill validator and independent scenario review. The dependency suite
is recorded below. Translation synchronization remained current with no new UI
strings. The 1.13.62 runtime was built; 257 frozen source files, 20 packaged
Python modules and eight bundled skill/reference files were checked against
source, then the runtime and 31 canonical bridge files were deployed and their
installed versions/hashes verified. No AI generation or user diagram edit was
performed. Build-time optional import/tool-discovery warnings did not prevent
the packaged Host startup and tool-catalog probes from passing.

### Actionable dependency disputes and exact view extents

`frame_flow_algorithm.py` now returns structured, bounded investigation facts
with dependency gaps: the declared producer and consumer, execution indices,
exact observed views, candidate mutations, native utility operands and indirect
workload receipts. Candidates are ordered by execution, with explicit counts
and truncation flags. Suggested calls use the actual `inspect_frame_render_stage`,
`inspect_event_api_parameters` and `inspect_frame_resource_usage` schemas;
backward searches start nearest the consumer and forward searches start at the
producer. Usage results remain candidates, pagination is explicit, and an
incomplete usage result cannot prove an absent writer. The hints reuse valid
evidence and do not force another live replay or a particular analysis sequence.

The resource-version check now resolves a specific false-dispute class: a
candidate writing another mip, array slice or byte range is excluded only when
its captured write views are disjoint from **every** captured consumer read
view. Missing extents, failed inventories, overlapping aliases and unknown
ranges remain unresolved. A legacy `mip`/`slice` field is a view's base point,
not proof of a one-subresource extent. Buffer placeholders cannot be used as
texture ranges; texture descriptor bytes cannot create buffer-range exclusions.
The Host observation store now preserves explicit view and byte-range fields
instead of dropping them. The current native texture inventory supplies mip and
slice extents; unavailable buffer extents are not invented.

Generic native Copy actions cannot alone distinguish a whole-resource copy
from a subresource copy: both may carry the default `(0,0,0)` Subresource.
Consequently this change reports those operands for exact API investigation,
but does not prune transfer/clear candidates from default subresource values.
Known bindings remain usable even when an unrelated descriptor read fails;
inventory completeness is checked separately before excluding write scopes.
Present remains an observable final output, while remaining excluded from the
write index because presentation does not mutate the resource.

An input explanation cannot dismiss an uninspected intervening writer by adding
prose alone. Inspected partial writes or accumulation can still be explained
using the existing evidence field. For a changed result crossing a stage
boundary, the recovery route asks for its actual updated producer and downstream
use, instead of requiring an external-stage event to become a local step.
Disjoint-write observations are retained with the document so a reload does not
reintroduce the same false dispute. Quality paging preserves the investigation
metadata, the portable sidecar retains it, and rebuilding for another capture
session regenerates tool arguments with the new flow identity.

Validation: **323 tests passed** across the algorithm, dependency recovery,
graph dependencies, resource subgraphs, frame-flow store, resource-usage and
server frame-flow suites. This includes **28 new targeted regressions** for
view overlap, missing range information, partial read failures, utility defaults,
input-evidence bypass, bounded recovery calls, paging, persistence and new-session
rebuilds. During verification, a Present-output regression was found and fixed;
the final complete run includes that case. Final algorithm source SHA-256:
`BA3F5B5308E44ED0B385501CD65078E200D2AB041E3EF2A53B8634F6641D0F9B`.
No user's frame diagram or AI task was run or altered for these tests.
