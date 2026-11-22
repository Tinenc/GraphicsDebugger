# Frame diagram retry audit — 2026-10-24

Internal development evidence. Task `038d1dd507a745919757c9387dfc083c` was a retry
of the earlier frame explanation, not a new Shader editing request.

## Retained evidence and counts

- `%LOCALAPPDATA%/ReplayLab/ai-tasks/038d1dd507a745919757c9387dfc083c.jsonl`
- The adjacent `038d1dd507a745919757c9387dfc083c.report-state.json`
- Published document:
  `%LOCALAPPDATA%/ReplayLab/event-artifacts/frame-render-flow/66f332b186dc411e993f40eb10b47613/d52ceae873d07d2f016c246ecf32cd03ce9deb046f4880a780b1acb9624cbce9.json`
- Capture sidecar: `D:/Study/抓帧/白银之城/1.rdc.bread/frame-render-flow/current.json`.

The JSONL has **868 events**. The first queued and final completed timestamps are
1792817819383 and 1792818259632 ms: **440.249 seconds (7 min 20.249 s)**.
Deduplicating terminal MCP activities by activity ID gives **68 calls**:

| MCP operation | Calls |
| --- | ---: |
| `read_evidence_file` | 40 |
| `list_bread_capabilities` | 9 |
| `load_bread_skill` | 6 |
| `publish_frame_render_flow` | 4 |
| `record_task_review` | 3 |
| `get_frame_render_flow` | 2 |
| `inspect_event` | 2 |
| `prepare_frame_render_flow` | 1 |
| `inspect_frame_render_stage` | 1 |

Host statistics record **23 calls, 4 errors**, with **22.765 seconds** of summed
tool execution. Proxy-owned file reads, skill and capability operations explain
why Host and MCP counts differ; the remainder of wall time cannot be assigned
to individual model/orchestration activities from these statistics.

| Host operation | Calls | Errors | Recorded duration |
| --- | ---: | ---: | ---: |
| `prepare_frame_render_flow` | 1 | 0 | 2.578 s |
| `get_frame_render_flow` | 2 | 1 | 0.515 s |
| `inspect_frame_render_stage` | 1 | 0 | 1.719 s |
| `read_evidence_file` | 13 | 0 | 9.579 s |
| `publish_frame_render_flow` | 4 | 3 | 5.952 s |
| `inspect_event` | 2 | 0 | 2.422 s |

There were **no** `get_current_shader_representation`, `inspect_shader`, workspace
preparation, external decompilation, compilation or apply calls in this retry's
recorded inventory. Earlier decompiler failures must not be attributed to it.

## What actually failed

The read error occurred once: `section must be stages or actions`. Subsequent
stage retrieval succeeded. The three publication errors were schema round-trip
problems, in this order:

1. Read-only stage output fields (`actionCount`, navigation metadata,
   `outputCoverage`, `qualityGaps`) were resubmitted as editable input.
2. Saved image objects were resubmitted instead of managed manifest references.
3. Saved comparison objects were resubmitted instead of image-index references.

The fourth publication succeeded. It saved **23 stages, 113 images and 8 channel
groups**, covering **8,250/8,250 effective event records**. Coverage is separate
from explanation completeness: `quality.analysisComplete=false`, with five gaps
on `redraw-19` (one partial investigation, two unverified bindings, two missing
integrations). This reused most of the existing publication rather than doing a
fresh Shader/algorithm analysis of every stage.

There is **no `effective frame action` rejection in this retry**. The prior
unknown-indirect rejection had already been changed to allow `hasWork=null` in
algorithm inputs, steps and integration. The retained stage explanation and final
blocker still reused the old inability-to-record claim. A stale conclusion was
not re-tested against the corrected contract.

## The remaining real evidence-tool gap

The Agent inspected parent EIDs **62777 and 62782**. `inspect_event` returned
pipeline state: no Shader/RTs for those containers, plus vertex buffers
17357794 and 19036400. Neither buffer is established as the ExecuteIndirect
argument buffer by that response. The Agent correctly did not invent an argument
buffer ID or use vertex-buffer offsets as argument offsets.

However, this was the wrong evidence surface. The bridge's `inspect_event`
collected only draw/pipeline state and omitted the native structured API chunk.
The API already exposes exactly the data needed for this recovery:

- `APIEvent.chunkIndex` indexes `CaptureContext.GetStructuredFile().chunks`.
- `Serialise_ExecuteIndirect` records `pCommandSignature`, `MaxCommandCount`,
  `pArgumentBuffer`, `ArgumentBufferOffset`, `pCountBuffer`, `CountBufferOffset`
  in `renderdoc/driver/d3d12/d3d12_command_list_wrap.cpp`.
- `FinaliseExecuteIndirectEvents` reads the captured indirect argument data and
  creates native synthetic child chunks containing `CommandIndex`,
  `ArgumentIndex`, `ArgumentSignature`, and typed `ArgumentData`. It also sets
  child draw/dispatch counts. Those exact child chunks can distinguish decoded
  zero work from a default-initialized zero without reading a buffer after it
  may have been overwritten.
- `ResourceDescription.initialisationChunks` links the command signature to
  `Serialise_CreateCommandSignature`, including its byte stride and argument
  descriptors.

The missing bridge/Host API surface, not absence of capture evidence, caused this
branch of recovery to stop. Native source proves the available representation;
the actual values for this capture require the new tool's live acceptance check.

## Correction and verification

- Added bounded, typed, read-only `inspect_event_api_parameters`: exact event
  chunks, ExecuteIndirect parent, buffer IDs/offsets, signature initialization,
  and native decoded child arguments. It does not navigate, replay, decompile,
  prepare a Shader workspace or invoke another AI.
- Frame outline and refreshed stage inspection now consume exact native
  `ArgumentData`. Only proven decoded values produce `hasWork=true/false`;
  absent/unsupported representations remain unknown. D3D12 decoding is explicit;
  generic API chunk inspection remains available on other graphics APIs.
- Unknown indirect events remain publishable as unresolved investigation
  boundaries. Bindings and prose alone do not make them verified contributing
  calculations. The quality check requests the concrete API-parameter tool;
  decoded zero events must be removed from the contributing path.
- The candidate-writer index excludes decoded zero-work events. Retained RT
  bindings on a zero-instance draw do not create an intervening resource version
  that the Agent must explain as an executed calculation.
- The accompanying parent change provides editable publication proposals rather
  than requiring the Agent to strip derived fields by trial and error.

Verification: **155 tests and 2 subtests passed** across
`test_frame_stage_evidence.py`, `test_frame_render_flow_outline.py`,
`test_frame_flow_algorithm.py`, `test_bridge_composite_readonly.py`, and
`test_frame_flow_persistence.py`. Cases cover exact EID provenance, missing or
nearby chunks, zero/non-zero draws and dispatches, 64-bit resource IDs, signature
offsets, bounded trees, session mismatch, no replay/navigation, and partial
quality without rejection of the entire diagram. No paid Agent was run.

## Native acceptance on the existing capture

The application remained open on EID **65445**, texture **18993416**. Its existing
native interactive Python shell was invoked through the typed application-control
interface, without displaying the shell, reloading the capture or replacing any
live bridge handler. An independently named module loaded the new helper from
source and ran only `GetAction`, `GetStructuredFile`, `GetResource` and metadata
reads on the UI thread. The shell input returned to its original empty value.

Retained fixture and actual result:
`%TEMP%/bread-indirect-api-audit-20261024/{verify.py,result.json}`.
Native process **18796** returned success, no errors, identical before/after EID
and texture. Both children were **proven zero-instance draws**, not unresolved
dispatches:

| Parent / child EID | Argument buffer | Byte offset | Native child chunk | Index count | Instance count |
| --- | ---: | ---: | ---: | ---: | ---: |
| 62777 / 62778 | 17367029 | 3020 | 76576 | 36 | 0 |
| 62782 / 62783 | 17367029 | 3040 | 76577 | 36 | 0 |

Both use command signature **328**, `MaxCommandCount=1`, no count buffer.
Signature initialization chunk **49** records `ByteStride=20`, one
`D3D12_INDIRECT_ARGUMENT_TYPE_DRAW_INDEXED` argument. The children expose typed
`D3D12_DRAW_INDEXED_ARGUMENTS`; their `InstanceCount=0` conclusively excludes
them from contributing writes. This resolves the specific missing-workload
boundary without inventing buffer IDs, parsing display names, or relaxing the
acceptance criteria. It does not by itself replace analysis of the surrounding
62768 and 62931 Shader calculations.

## Persistence regression found during final review

The document previously retained binding observations but omitted their native
`indirectWorkload` receipt. A freshly verified indirect calculation therefore
became unresolved after saving and rebuilding the diagram when the original
outline still had `hasWork=null`. Zero-work observations had a second loss path:
they were deliberately absent from the writer index and could be omitted from
the saved observation set entirely, inventing a possible intervening write on
reload.

The document now retains the small native receipt from either the top-level or
stage-inspection `details` field, including exact EID, chunk/argument identity
and workload counts. Zero-work receipts survive even without a binding inventory
or a direct algorithm/navigation reference. This does not turn unknown work into
positive work or retain the entire inspection payload.

Eight actual sidecar save/load/rebuild regressions cover both receipt locations,
positive producer/calculation/consumer events, and a zero-work intervening event
outside the stage. The combined regression run passed **250 tests and 2 subtests**
in 9.50 seconds (the five suites above plus `test_frame_render_flow.py`).
Existing saved algorithms that explicitly claim a newly proven zero-work event
still need their calculation path corrected; this persistence fix does not relax
the effective-action validation for those stale claims.

## Recovery, presentation and native channel acceptance

The built-in workflow now requests a fresh review with `reanalyze=True`, keeps
valid evidence, and exposes editable `proposals` separately from derived readback
fields. Unreviewed modules remain visible in `quality`; redisplaying a saved
publication does not establish a new investigation. The skill explicitly routes
missing indirect arguments to `inspect_event_api_parameters` and a refreshed stage
inspection, rather than declaring a missing Host capability from an old error.

Module illustrations accept a general acyclic `processGraph`: transformation
nodes, branches and composition edges, with optional relevant images, comparisons
or animations. A transformation needs no redundant texture. There is no mandatory
final-scene divider and no fixed Bloom recipe. Shader understanding and verified
bindings still determine the explanation.

Publication generates semantic channel groups before opening their popup and
stores verified PNG/manifest receipts with the flow. RGB+A and independent packed
components are different plans; actual single-component formats and explicit
no-split plans require no extra capture. A transient channel read is retried once
for missing groups only, with capture, revision and selected-event guards. Popup
scene replacement is atomic; incomplete results are not complete-cache hits.

The isolated native UI acceptance passed **89 checks**, including branched graph
rendering, channel popup ordering, opaque previews, full-height PNG export and
split-image coverage. Evidence: `%TEMP%/bread-frame-flow-native-156/result.json`.
The deployed bridge then passed **32 checks** against the real capture at EID
62027 / texture 18993397. Both RGB+A and RGBA plans produced verified native PNGs;
EID 66327 was restored and the source RDC was unchanged. Evidence:
`%TEMP%/bread-frame-flow-precache-156-retry/result.json`. The new typed API also
returned `nonZeroWorkload=false` for children 62778 and 62783 in that instance.

These checks validate the revised application workflow and its evidence routes.
They do not claim that a new paid AI run has independently regenerated and fully
explained every algorithm in the existing frame diagram.

Delivery: Bread runtime **1.13.56**, explanation skill **v17**. The final runtime
was rebuilt after the cache/observation corrections and deployed to
`x64/Development/replaylab`; the canonical Python bridge was hash-verified at
`x64/Development/integrations/replaylab/python/renderdoc_mcp_bridge`. Qt catalog
sync/check, complete QM compilation, source-literal lint and native relink passed.
The broad flow suite passed 387 tests plus 6 subtests; the final targeted
algorithm/persistence/panel run passed 152 tests after the last corrections.
Skill gateway/tool-reference checks passed 49 tests with 2 optional tests skipped.
The deployed UI fixture repeated its 89 native checks successfully under
`%TEMP%/bread-frame-flow-native-156-final`.

The normal user instance was restarted on 1.13.56 and restored its project,
23-stage saved flow, selected EID **65445** and texture **18993416** successfully.
A read-only Host acceptance over that actual flow returned 23 canonical editable
proposals and a fresh review with all 23 stages pending, no new inspected events
and `reviewComplete=false`. It did not republish the old diagram. Results:
`%TEMP%/bread-frame-flow-deploy-156/{restart-result.json,reanalysis.json}`.
