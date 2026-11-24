# Bread Shader recovery audit — 2026-10-20

## Observed task and source identities

- Task: `8bb831a4d49b4066bbc97f842533555c`.
- Local task-log interval: 16:44:04–17:03:55, approximately 19 minutes 51 seconds.
- Target: `1.rdc`, EID 61408, fragment / MainPS; captured Shader SHA-256
  `9cebc3b0057b57da268b1fe76b6ae75e04b51e75077c836400b5560da272ed90`.
- Workspace: `0c06d2a00fb3bb1b2007aa19d8719946ff04c8b553c90bd57bc04081c429130d`.
- Resumed organization: `367de491677a4dc7b688e5dff9c82338`.
- Starting proposal: `ad1f3ffe4ad72457a7ae6c8587098fa195bc4bf2983a6fcee2d55ee03714cb3a`.
- Saved full result: `4e89716dfefb1670c64749a936564748a3727d4ba6e21dfe9416ab9844ed2f9f`.

The task log and report state under `%LOCALAPPDATA%/ReplayLab/ai-tasks` record
`terminalStatus=partial` and an Agent `blocked` review. The final message did not
claim successful replacement. A save/display receipt established editable source;
the output comparison rejected replacement.

## What this turn changed

The Agent resumed an existing organized draft rather than writing the entire
Shader from scratch. The retained starting and final files have 2,786 and 2,806
lines; a textual diff contains 96 added and 76 removed lines. The final review's
81 functions are the scope of the whole program, not 81 new functions this turn.

Changes include:

- Splitting diffuse preparation into `PrepareDiffuseAmbient`,
  `PrepareDiffuseFallback` and `PrepareComplementDiffuse`, called by
  `EvaluateDiffuseInputs`.
- Combining wet UV, projected direction and reflection-coordinate components.
- Reusing an existing view/light dot product.
- Simplifying 32 expanded `float4(float3.x, float3.y, float3.z, 0)` constructions.
- Updating comments and the saved analysis.

The task used 113 distinct MCP calls: 43 workspace reads, 14 evidence reads,
16 capability queries, 8 Skill loads, 8 writes, 5 finish calls, 4 source reviews,
3 task reviews and smaller context/display/diagnostic calls. Only two finish
calls attempted final physical validation; the other three saved source without
validation. The recorded Host tool execution totals about 83.6 seconds, excluding
model generation, transport, Skill/capability/evidence-reader overhead. Thus the
19-minute elapsed time must not be attributed entirely to compilation.

## Why delivery stopped

1. The full organized candidate compiled and passed ABI checks, but comparison
   found 27 color components beyond tolerance out of 14,745,600, with maximum
   absolute error 0.00244140625. Depth matched byte for byte. Repeated original
   output also differed in 3 components, so the comparison baseline needed
   investigation; this did not prove that candidate differences were harmless.
2. A targeted arithmetic-order restoration did not improve the comparison:
   53 candidate components and 13 baseline self-comparison components differed.
   The Agent reverted the trial.
3. The Agent made a dependency-coherent diagnostic variant
   `644c6b5c5e0c9f0a934bf1e9da85ab25b3ae214dc4f57963ab359984aaf1b2c0`,
   restoring 10 functions. The public validation entry rejected it before
   comparison because an organization draft was open and lacked a complete
   source-quality review. The variant deliberately retained old structure for
   diagnosis, so marking it fully organized would have been a false review.
4. An independent reference decompile failed with
   `Unexpected dynamic cbuffer offset`; the previously available SPIRV-Cross
   source was retained. The decompile job actually returned exit code 1.
5. Pixel debugging at (843, 514) reported 3,483 executed steps but returned only
   steps 0–49, ending during initialization. The exposed tool contract offered no
   continuation cursor or offset.
6. Two same-session blocker reassessments occurred. They found no newly available
   diagnostic route; the task ended partial and preserved the full draft.

The principal gap was a Host diagnostic interface: the internal
`compare_shader_organization_candidates` operation was not an Agent-callable
tool for isolated unfinished trials. Full-publication gates also guarded the
diagnostic path. Additional instructions to “keep trying” alone would produce
more capability queries or encourage a false complete review.

The Agent did not reload the capture, change tolerances or apply a rejected
candidate in this task. Its blocker report was supported by actual errors and
the available contracts. The fix must make a useful diagnostic operation
available, retain source-quality requirements for final publication, and preserve
the user's full proposal while testing smaller variants.

## Implementation guidance

Keep physical diagnostic trials separate from publication. A trial may compile,
check its interface and compare replay outputs without being a fully organized
program. It must not acquire final-review approval, replace the saved proposal,
start monitoring or persist a live replacement. Identify each trial by its exact
source hash and reuse its result only for the same source and observation context.

A failing baseline self-comparison is an explicit diagnostic condition. Stabilize
the relevant observation before attributing a small color difference to a source
edit. Do not widen tolerance or infer equivalence from the count being small.

On failed final validation, provide the Agent with the bounded mismatch location,
values, baseline-stability result and the named diagnostic capability inline,
instead of burying the route behind a large result and a new discovery loop.

## Recovery performed in this investigation

The independent full-frame replay comparison was not a stable reference in this
capture. Testing the captured original against itself also produced differing
color components. This establishes an observation problem; it does not identify
the upstream cause or prove a specific GPU race.

The new D3D12 paired-draw operation replays the upstream frame once and executes
baseline A, candidate B and baseline A again with identical input bindings and
restored pre-draw color/depth attachments. The repeated baseline must be stable.
The initial implementation supports bounded direct, non-MSAA VS/PS draws and
rejects unsupported side effects and resource aliasing. Other cases retain the
existing comparison with an explicit fallback reason. Failure after a supported
paired operation starts cannot silently fall back to another replay.

Actual EID 61408 results:

| Comparison | Color components beyond tolerance | Maximum absolute color error | Depth | Baseline repeat |
| --- | ---: | ---: | --- | --- |
| Captured original versus itself, paired draw | 0 | 0 | Byte exact | Byte exact |
| Captured original versus complete organized source | 0 | 0.00048828125 | Byte exact | Byte exact |
| Saved organization baseline versus final source | 0 | 0 | Byte exact | Byte exact |

No tolerance was increased and no Shader arithmetic was changed to hide the
unstable comparison. The source remains
`4e89716dfefb1670c64749a936564748a3727d4ba6e21dfe9416ab9844ed2f9f`.
The diagnostic and final compiler outputs both have SHA-256
`283ec003242ad2d01fe8b93567f759e4f1f0cc403a8de04e8cae0581d431e755`.
The formal finish completed compilation, ABI checking, output comparison and
replay application at revision 10. Its comparison took 10.969 seconds. This is
validation of the captured event, not a proof for every possible Shader input.

The captured-original diagnostic receipt is retained under the workspace at
`jobs/diagnostic-r0009-9d2aaeedd83e4e5497fc0cb34e53e93d/diagnostic.json`.
Final organization receipts and revision history remain in the same workspace.

## Runtime and editor changes

- Added the public `diagnose_shader_organization_candidate` tool for isolated,
  incomplete diagnostic variants. It preserves the canonical source and proposal,
  does not publish a project version, and restores replay after comparison.
  Final publication still requires complete source review.
- Failed validation returns the diagnostic tool name, exact current source hash,
  mismatch evidence and baseline stability directly. A comparison exception after
  compilation is recorded as a comparison failure, not a successful receipt.
- Pixel debugging persists a bounded trace with pagination instead of exposing
  only its first 50 initialization steps. The actual original trace contained
  3,483 executed steps.
- Shader-language semantic highlighting uses the language server's token legend,
  document versions and UTF-16 positions. Function, type, parameter and variable
  colors update asynchronously; stale responses cannot recolor a newer document.
- Bread AI fenced code has syntax highlighting. Exact workspace links and
  absolute source links with line/column open the relevant document and position;
  ambiguous same-name files are not used as a fallback.
- Expensive replay calls release the embedded Python GIL while native replay
  performs the work, allowing unrelated Python/UI activity to proceed.

## Final project publication defect

The successful finish originally displayed its preview before application and
live-edit startup, and did not update the project's active Shader head. A later
workspace refresh could therefore select the old project source and retire the
new preview even though the new binary was applied.

A manual exact-source registration established project head generation 2 with
knowledge ID `e48ba545a1202782caedefda91b36291`, storing both the new source and its
compiled candidate. An additional display defect was then observed: showing the
saved canonical source without its event/project binding introduced an unbound
duplicate. Final publication must register the actual source/binary, display it
with the current target identity after other side effects, and read back the
visible document. Saving a checkpoint must not publish a new project head.

This sequence is now implemented. Registration pins the validated source hash,
candidate hash and revision. Display carries the current project head and exact
EID; a newer clean document retires the old display while unsaved local buffers
are retained. Older in-flight snapshots cannot restore the retired version.
The final UI readback requires the loaded source hash itself to match, not merely
the on-disk metadata. Registration/display failures preserve the actual
application result but mark delivery incomplete and give a recovery action that
does not require recompiling unchanged source.

## Verification

- Final publication, diagnostics, source display and project-head tests:
  139 passed, 2 skipped. Existing registration/display compatibility selection:
  42 passed and 4 subtests.
- Native paired result contract: 91 assertions in 1 test case through
  `render_soco_doccmd.exe test unit "[shader-pair]"`.
- Native code editor: 43 assertions in 7 test cases.
- Installed Slang service probe: 16 checks covering semantic tokens and source
  navigation.
- Native chat rendering, workspace panel and source-link navigation regressions
  passed; cold-open include line/column preservation has dedicated coverage.
- Numeric comparison and replay restoration tests: 83 passed.
- Modified embedded bridge modules parse with Python 3.6 syntax.
- Translation synchronization: 5,155 active translations, all finished.
- Development native build with `/m:2`: zero errors and zero warnings.

## Installed restart acceptance

Bread runtime 1.13.42 was installed and the application reopened `1.rdc`.
Frozen-module verification matched 41 modules, 15 Skill files and all 20 bridge
Python files against the final checkout.

The new capture session `4c5e832639344663b0cfb25b20bb145d` restored and applied all
four project Shaders, enabled all four editors, and reported no warnings or
errors. The workspace contained exactly the four EIDs 61408, 61523, 61673 and
61990. EID 61408 opened canonical workspace
`469daf01545041cc9a67a9e3e56a100a`, retained revision 10, and its actual loaded,
editable document had the final source hash `4e89716d…ed2f9f`.
The live replacement inventory independently confirmed candidate
`283ec003…31e755` at that EID. No duplicate or unbound Shader remained.

After the final native bounds changes, the installed binary was checked against
the captured original again without recompiling. The paired path remained
available: zero color components beyond tolerance, maximum absolute error
0.00048828125, byte-exact depth and repeated baseline, and the previous replay
mapping was restored. This observation took 11.469 seconds.
