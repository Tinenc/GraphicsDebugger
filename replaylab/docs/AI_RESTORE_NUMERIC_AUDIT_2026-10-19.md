# Bread Shader restoration and numeric validation audit — 2026-10-19

## Observed restoration failure

The persisted restore result is:

`D:/Study/抓帧/ReplayLab Project/.replaylab/replay-project/derived/restore-results/b06d64f492175827227e394760cd94351da0390d8e617e64334ceab06be881f6.json`

It records three attempted matches, one applied, two failed. It did not lose the
other Shader associations or fail to match them. Resource `1000000000000003236`
failed while resolving its target workspace because a user replay operation
preempted the background request. Resource `1000000000000003245` failed at the
same phase with `Bread background tool was cancelled`. The middle resource,
`1000000000000003239`, restored successfully. The result's `completed=true`
means orchestration returned; the per-item failures correctly prevent reporting
full restoration.

The IPC poller set the same cancellation event for a user EID selection and a
capture shutdown. A running request could therefore return either the explicit
foreground-yield diagnostic or an undifferentiated cancellation diagnostic.
Restore treated those transient interruptions as permanent per-Shader errors.

The fix records selection preemption separately. On the same pinned capture,
restore resumes only the exact, explicitly yielded IPC request with a fresh
visible-EID snapshot. It uses the original request deadline and bounded backoff,
and checks project/cancellation/deadline state before resuming. Shutdown, capture
changes, missing replies, arbitrary failures, and explicit cancellation are not
replayed. It does not restart already committed Shader applications, reload the
capture, or hide real failures.

## Organization task shown in the screenshot

Task: `f28b8649dd82473b8c99e24e3e919558`

Evidence: `%LOCALAPPDATA%/ReplayLab/ai-tasks/<task>.jsonl` and
`<task>.report-state.json`. Local time: 15:06:44–15:56:06, about 49 minutes.
The final task event is `completed` with `terminalStatus=partial`. A screenshot
taken at 15:49 is before this terminal event and cannot establish that the
completed task remained running afterwards.

Target: EID 61408, fragment `MainPS`, workspace:

`D:/Study/抓帧/ReplayLab Project/workspaces/shaders/v2/2e8aef0efc2edfdc5c02bbebcd501eade946858aff7c083166d3d46460db5f0b-recovery`

The final organization session is `e4e2f95906e343b6ac25a8cb10b9ae43`.
Its baseline has 157,937 stored bytes, 2,303 lines and 15 parsed functions;
the saved proposal has 151,210 bytes, 2,722 lines and 72 parsed functions.
This establishes that substantial extraction occurred. Function/line counts
do not establish readability or semantic correctness.

The Host recorded 154 calls, including 116 source-file reads, 11 proposal writes,
four source reviews and five finish calls. The provider trace also records
19 capability-query calls. All 11 proposal writes used complete `content`, not
the supported atomic `edits` or owned-file import. Source reads used only 0.77 s
of recorded Host execution; all recorded Host durations sum to 131.5 s (nested
tool timings can overlap). The 49-minute task duration therefore cannot be
attributed to those source-file reads blocking for 49 minutes. Repeated model
round trips, generation of complete replacements and analysis were material
contributors. No arbitrary tool-count limit should replace using the whole
working file and narrow edit batches.

### The five finish results

1. Baseline ABI failure: captured `t11 space0` is `Texture<float>`, candidate
   was `Texture<float4>`. The Agent used `repair_shader_workspace_source_abi`.
2. Compile failure (`2147500037`), with a concrete compile-job artifact.
3. Compile/ABI passed; numeric comparison rejected 13 of 14,745,600 color
   components. Maximum absolute error: `0.001953125`. Depth storage was exact.
4. After an order-related source repair, numeric comparison rejected three
   color components. Maximum absolute error: `0.00146484375`. Depth remained
   exact. The baseline output hashes differed across these two observations,
   while the candidate output hash was identical. This does not establish the
   cause; it warrants an actual same-baseline repeat.
5. Save-only finish succeeded, with `applied=false`, `wholeSourceValidated=false`
   and `safeToStartLiveEdit=false`. The saved-source preview explicitly reported
   `readOnly=true`. It was source delivery, not successful replay replacement.

The X marks beside failed finish calls correspond to actual failed operations;
they are not decorative marks and do not imply all earlier source edits failed.

### Asset naming

This task did not skip asset investigation. Before its first proposal write at
15:33, it called `retrieve_shader_asset_candidates`, the configured asset-reader
operations, `uex_material_shader_maps` and `decode_material_shader_outputs`.
The task's assessment says the UEX material parent/platform variants did not
produce a value-consistent match for the target. Decoding included
`M_CharacterPBR`, 114 decoded outputs and 10 unknown opcode programs. The final
record explicitly retained inferred names and physical offsets instead of
claiming recovered names. These records do not prove successful texture/CBuffer
alias publication. Forcing unrelated asset names would introduce incorrect
meaning; future reports should distinguish root/reader lookup, candidate match,
source names and live alias publication separately.

## Later numerical-recovery follow-up

A newer task, `bc7a0f53b6524860b1bb6a379f318b01`, ran from 16:28:08 to
17:13:06 (44 minutes 58 seconds) on the same EID 61408. It was a targeted
diagnostic follow-up, not another complete reorganization. Its final event is
also `completed` with `terminalStatus=partial`. It genuinely tried staged
bisection before stopping; the earlier screenshot does not show this later run.

The Host recorded 138 calls, six errors and 133.3 seconds of summed execution.
The provider trace separately shows 27 capability queries (two failed), so
repeated capability discovery remained excessive. There were 60 source reads,
nine compile calls (one rejected because the organization draft had not been
reviewed), nine successful save-only finishes and nine applications. These were
mainly separate diagnostic revisions, not nine successful production acceptances.

The Agent created a sibling `-bisect` workspace and ran the reconstructed
baseline and reorganized calculation inside the same fragment invocation. It
used eight stage checkpoints, then four final-color contributions. A constant
1000 output served as a positive control. Revisions r4-r6 detected a difference
at the selected final-color checkpoint below or equal to `1e-6`; after adding
more checkpoints, r7-r8 no longer detected it. Instrumentation changed the
optimization context, so this did not identify the original three failing
production components. These checks also did not cover every intermediate
value or exclude NaNs. The saved diagnostic report accurately limits the claim.

The remaining blockers were concrete tool limitations: `debug_pixel` threw
`ShaderDebugState has no attribute sourceVars`, while `get_texture_data`
returned only 7,500 of 29,491,200 bytes and exposed no offset/crop argument.
Fingerprints and display PNGs could not provide the failing production values.
The Agent rolled back the diagnostic replacement (`cleared=true`,
`targetFreed=true`, `originalShaderPreserved=true`), kept the production source
hash `7f7717a4...` unchanged, and finished partial after reviewing remaining
paths without repeating the experiments.

The new comparator coordinates/values remove the need to download a full
texture merely to locate failed components. The confirmed debug serializer bug
is also fixed: current RenderDoc stores mutable values in `ShaderDebugState.changes`,
whereas `sourceVars` belongs to trace/instruction mapping metadata. Pixel and
vertex debug now return actual typed before/after values, the next instruction
and call stack. They preserve zero components, encode non-finite values safely,
bound nested values and retained steps, explicitly report truncation, and free
the debug trace even on failure. This does not claim a complete instruction
trace: the existing first-50-step response bound remains visible to callers.

## Numeric diagnosis changes

Previously the comparator returned error totals and output hashes but no failed
pixel coordinates or reference/candidate values. The Agent explicitly reported
that absence as its blocker and queried capabilities repeatedly.

The comparator now returns, in the existing comparison pass:

- Up to 32 failed samples per output, with x/y, stored component index, both
  values, absolute error and the actual allowed error.
- A failure bounding box and the worst failed component, including a worst
  component outside the bounded first samples.
- Explicit sample truncation; full failed/compared counts remain exact.
- Strict JSON representations for NaN/infinity diagnostics.

Only a failed comparison attempts one additional baseline observation, producing
an A/B/A stability result. A passing comparison has no additional replay. The
optional repeat does not start after 45 seconds or cancellation/preemption, and
releases candidate readback bytes before rereading the baseline. Instability
never converts a failed candidate into success or widens tolerances. The original
mapping and visible event are restored before returning.

These are diagnostic capabilities for the Agent's targeted repair and
dependency-coherent change isolation. They do not automatically rewrite user
source or claim that every numeric failure is caused by floating-point noise.
The latest user's Shader has not been edited or retroactively marked accepted by
this audit.

## Verification

Focused restoration, cancellation, comparator and validation-policy regression:
292 tests and 153 subtests passed. This includes a real restore orchestration
with the target-workspace request yielding once and then continuing through
application/editor restoration, unchanged operation deadlines, rejection of
unrelated failures, sparse channel coordinates, bounded diagnostics, stable and
unstable A/B/A results, and cancellation/budget suppression of optional replay.
The separate bridge-dispatch/retry run passed 44 tests.

Independent review additionally covered optional-repeat failures and replay
cleanup. A failed optional third read now preserves the original A/B mismatch
coordinates and counts; its own error is attached to `baselineStability`.
Cleanup failures remain fatal and supersede resumable preemption, preventing a
request whose visible-EID restoration failed from being retried as a clean
yield. Optional repeat also checks native pending-foreground work and generation
changes, including interaction that does not produce an EID-selection event.
The focused regression for these changes passed 90 tests.

The later pixel/vertex debug API regression passed 14 tests and two subtests,
including current states without `sourceVars`, actual typed values, zero and
non-finite components, bounded traces and cleanup after continuation failure.

## Editor, graph and chat changes

Definition/reference lookup is restricted to the current document and its
explicit include dependencies. Other opened Shader workspaces and suspended
captures no longer supply fallback definitions just because they contain the
same variable name. Member resolution also handles indexed struct values and
vector swizzles. Workspace navigation resolves the exact document identity and
absolute path instead of selecting the first `source/shader.hlsl` suffix match.

The saved working source is editable even when replay publication has not
passed. In that state Ctrl+S saves the managed source without starting compilation
or live replacement. The validated snapshot remains immutable and separate.
Saves check the exact project and expected source bytes before mutating an
organization session; conflicts preserve the user's buffer. Retrying an already
committed save does not reopen the draft or invalidate a later accepted version.

A function's overview is now an execution projection: separate call statements
connect in source order, with source-bound branch/loop/exit blocks. Repeated
calls in different statements are separate steps; multiple calls inside one
expression stay grouped rather than inventing an evaluation order. Plain
assignments are omitted from this compact view. Nodes still navigate to source
and drill into the callee's own overview. A function-call hierarchy is a source
design requirement, distinct from this within-function execution diagram.

The EID context menu uses Add/Remove Bookmark according to the clicked event.
Chat separately labels provider-exposed reasoning summaries and raw reasoning,
when actually supplied, and retains messages. This task supplied summaries;
there is no basis to claim that an unavailable raw reasoning stream was shown.
A failed tool retains its X and diagnostic, but no longer marks the whole Agent
failed while the Agent is still repairing. Actual partial/blocked terminal events
end the visible running state before process cleanup finishes.

The native editor probe passed all 46 checks, including unrelated-file isolation,
include navigation, local/member references, navigation history, flow navigation,
editing and diagnostic jumps. It did not modify capture data or compile user
source. Native chat/trace tests and the focused flow/editable-source suite also
passed. The production shader's numerical discrepancy is still unresolved; these
results validate the product changes, not acceptance of that shader.

## Installation acceptance

Runtime 1.13.37 and Shader workbench skill 96 include these fixes. Live acceptance
also exposed a separate publication-state defect: a save-only draft from the
failed numeric task was registered without a persistent restriction, so project
restoration compiled and applied it after reopening. That exact replacement and
its live-edit session were stopped and rolled back; its source was preserved.
The other three verified replacements were retained.

Source-only publications now carry a persistent content-identity marker. For old
records, the exact saved session/source/program/stage/entry identity migrates to
a project-side negative admission record, independent of the old workspace's
future existence. Both restoration caches and the inventory projection honor
the restriction; an ABI cache cannot override it. Mixed projects can still cache
their eligible replacements while reopening pending drafts without compilation,
replacement or a watcher. The actual four active project records were checked:
only knowledge `12eae68d5f579495a8c378dfddb598a5` / source `7f7717a4...` is
restricted. The new focused publication/projects/restore suite passed 329 tests
and 264 subtests, with one skip. Independent review exercised the inventory and
cache paths, including removal of the legacy workspace and stale binary data.
The first deployed mixed restore correctly applied three replacements and opened
the draft editable, but exposed an older client assumption that any skipped item
made a verified-cache result invalid. The Bridge validator now accepts only
explicit source-only reasons with matching knowledge/source identities and no
overlap with applied resources. Applied/receipt/editor counts remain three. The
actual business payload is a regression fixture; cold and warm variants pass,
while 22 invalid identity/count/cache mutations are rejected. The client suite
passed 55 tests and 116 subtests; mixed host/client tests also passed.

The actual saved `EvaluateStationaryDroplets` source was projected without
modification: its graph contains sequence edges from `MixCellHash` to
`AccumulateDroplet`, repeated call steps and separate branch paths. The final
native build reported zero warnings/errors, all 21 canonical Bridge files were
deployed and verified, and 30 frozen production module code objects plus every
Shader workbench Skill file matched the source checkout.

Final live acceptance (PID 6280, 18:36:28–18:36:56 local) passed. The verified RDC
cache was used, three replacements restored, zero Shader failures occurred,
and the saved numeric-failure source stayed editable and unapplied. No Shader
compile request ran on that cached reopen. The UI completion event was `info`
with phase `complete`, explicitly reporting three applied Shaders and one saved
source draft still unapplied. Restoration took about 28 seconds after capture
load; this measurement includes replay replacement/loading and is not a claim
of zero startup cost. The final UI-classification regression passed 78 tests and
140 subtests; translation checks finished all 5,138 entries. The application was
left running on `1.rdc` with the updated installation.
