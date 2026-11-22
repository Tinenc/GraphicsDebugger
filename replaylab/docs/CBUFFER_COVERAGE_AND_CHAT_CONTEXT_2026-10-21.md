# CBuffer coverage, live-edit latency and chat context — 2026-10-21

Delivered runtime: Bread 1.13.47; Shader Workbench skill 104.
The original measurements below were collected with 1.13.46 / skill 103.

## Follow-up: the application Agent owns naming

The preceding live publication demonstrated the display path, but was performed
by the development assistant. It did not establish that an application Agent
would perform the same work automatically. The user's follow-up specifically
requires fixing that default workflow, without manually renaming the example
Shader or its display metadata again.

Bread 1.13.47 / Shader Workbench skill 104 addresses the execution gaps:

- Begin exposes the resolved target and unpacked-asset configuration. The Agent
  investigates or reuses the associated assets, then uses their vocabulary and
  actual source consumers to choose names and channel groups across all bindings.
- Resource naming review records asset use and every texture disposition. It
  complements CBuffer publication coverage; neither source renames nor a claimed
  asset match substitutes for a successful display write. Unknown meanings remain
  explicit and do not require inventing names or matching every original asset.
- Source validation and overall delivery remain distinct. Missing metadata
  continues the same task after successful compilation/application; it must not
  reopen the closed draft, run finish again or compile unchanged source.
- Bounded tool output retains naming status and concrete next actions even when
  the full publication result spills to a local file. The continuation loop
  recognizes binding-coverage progress without treating timestamps or repeated
  reads as progress.
- Read-only explanations do not acquire a source-editing obligation. Valid
  unchanged evidence can be reused; changes to source or naming target require
  reconsidering the affected naming decisions.

The main Shader Skill is delivered inline in full. Its larger asset guidance is
split between investigation and post-organization publication, with explicit
links. Asset Skill 31 removes repeated explanation without changing its matching
rules. Both references fit the existing bounded delivery; the limits were not
increased to hide truncation.

The Host checks identities, coverage and receipts. Semantic matching and naming
remain the application's Agent's responsibility; these checks cannot prove that
every inferred name is correct. This follow-up does not run another live naming
publication or rewrite the user's Shader.

Follow-up validation: 397 tests passed and 4 environment-dependent tests skipped,
including API/ACP/Codex same-session naming continuation, all-binding accounting,
real texture publication receipt compatibility, stale evidence, Skill delivery
and tool exposure. Translation synchronization passed with no new UI strings.
A read-only inventory of the current EID 61408 source found six CBuffer blocks
and nineteen texture declarations; missing Agent resource review correctly
remained pending with `recompileRequired=false`. This is workflow regression
coverage, not a paid-model rerun or proof of semantic accuracy for every game.

Installation verification compared 60 frozen module signatures, 16 bundled Skill
and reference files, and 20 installed bridge Python files with the current source;
all matched. The full optimized Development build passed with zero errors and
seven optional-dependency packaging warnings. After normal application restart,
the installed MCP reported 1.13.47 and exposed the new review tool and combined
review argument. Its live read-only resource outline retained pending obligations
without requesting recompilation. The current source, CBuffer publication and
asset-resolution file hashes remained identical to the pre-installation baseline.

## Root causes and changes

The former semantic-delivery check counted only submitted fields. A successful CB5
publication could therefore be mistaken for completion of the entire Shader.
Coverage now inventories all current reflected bindings and physical lanes, binds
the review to the source/candidate identities, and records specific unresolved
lanes separately. Source changes invalidate the review; numeric live edits do not.
Partial useful publications remain allowed, but are not whole-Shader completion.

The live publication also exposed two layout details: ten integer display fields
must use the capture's `int/int2` interpretation rather than the source spelling
`uint`; CB2/CB4's original `unknown[N]` storage had a misleading `annotated` shape
label. Verified active typed reflection is now allowed for wholly placeholder
storage after identity/extent/lane validation. Named original typed fields still
cannot be reinterpreted arbitrarily.

The current Shader received 285 semantic fields spanning 474 real lanes across
six bindings (b0–b5). Four unread ABI extent markers remain explicitly unknown.
The committed receipt reports all six bindings accounted for, zero rejected
fields and `partial=false`. Per-binding property counts are 7, 99, 11, 14, 2 and
152. Existing CB5 evidence, including 16 asset-assisted parameter interpretations,
was preserved. Other names are source-consumer inferences, not a newly confirmed
original-material match. This publication changes display metadata only.

Native readback after restart found all 285 names in the original CBuffer windows.
The displayed row counts for b0–b5 are 7, 100, 12, 14, 3 and 153, including the
four unchanged extent markers. Whole-CBuffer and grouped `baseTint` attachments
both passed their native context-menu/preview roundtrip, with correct EID, binding,
64-bit IDs and three physical component offsets. No chat request was sent by the
test. All four project Shaders restored from the verified cache; the final
application session logged zero warning/error events at completion of this audit.

The organization workflow includes unpacked-asset discovery/reuse, texture naming,
all-CBuffer interpretation and semantic publication after source organization.
Names recovered from assets remain distinguishable from meanings inferred from
calculation consumers. Unknowns are retained, not given invented original names.
The final answer should briefly describe work completed, assets matched/used,
inferences and unresolved work. The local CBuffer outline exposes every block and
physical alias candidate without a new replay or repeated asset scan.

The failed code-explanation request never reached a model. A 64-bit Shader ID was
serialized through a JSON number, rounded by Qt, and then rejected by the typed
agent request. Shader IDs now remain strings. Selection attachments carry capture
and EID identity, Shader/stage/entry, workspace/file/source hash, dirty state,
line/column range and neighboring source. Old malformed numeric identity is
discarded without discarding the user's selected text. API, ACP and Codex adapters
retain this context; explanation requests do not imply source modification.

Native CBuffer, individual parameter, texture and pipeline resource menus can add
reference attachments to the composer. Resource IDs and 64-bit offsets are strings;
binding, stage, byte/component offsets, current value and inference provenance are
included where applicable. Adding an attachment does not send a message or run a
replay operation. Grouped semantic rows can also be attached by their proven
component offsets; they remain distinct from reflected extension leaf identities.

## Live-edit measurements

Capture: `D:/Study/抓帧/白银之城/1.rdc`, EID 61408, D3D12, PS CB5.
Six CBuffer tabs were opened; only the selected tab was visible. Each run exercised
scientific numeric input, a 20-event slider drag, float2 editing and a color picker,
followed by exact restoration. All bytes outside the edited field stayed unchanged.
No Shader compile was performed by these edits.

Ordinary CBuffer reads are now bounded to the reflected size: 1,372 bytes rather
than the root-CBV remaining allocation of 530,432 bytes. Source/layout/alias stamps
permit reuse of unchanged semantic metadata. A value-only publication avoids a
redundant event-selection notification. The initial coalescing delay is 16 ms
instead of 80 ms; an in-flight replay retains only the latest pending edit.

The Development executable was built with `/p:ReplayOptimizeDevelopment=true`
(`/m:2`, explicit `SolutionDir`). This optimizes graphics-driver CPU code while
keeping diagnostics and PDBs. No checkpoint-validity checks were relaxed.

Hot samples below exclude the first edit and its reset, which include first-use
allocation. These are small same-machine samples, not a 60-FPS guarantee.

| Measurement | Original refresh + unoptimized driver | Optimized refresh + driver |
| --- | ---: | ---: |
| Latest input through UI publication, median | 692 ms | 527 ms |
| Mutation including replay, median | 596 ms | 451 ms |
| UI publication, median | 64 ms | 45 ms |
| Largest observed 20-ms UI timer interval | 188 ms | 109 ms |

Evidence: `%TEMP%/bread-146-latency-before.json`,
`bread-146-latency-after.json`, `bread-146-latency-driver-final.json`.
The remaining delay is dominated by full replay to this EID (56,423 command chunks
and substantial initial-resource restoration/GPU work), not changing the few
parameter bytes. Shared buffers can affect earlier draws. Reusing an unproven
prefix would produce an incorrect frame. First-use allocation remains slower.

## Validation boundaries

Separate output checks found that unmodified forced replays of this capture can
also produce different full-output hashes. Exact CBuffer reset is proven; universal
bitwise image reproducibility is not. Reset removed all tested patch ranges and
performed a full replay; no stale alias/value cache was found. This observation is
recorded in `bread-146-latency-driver-optimized-repeat.json` and must not be reported
as a successful deterministic pixel comparison, nor attributed to an unknown cause.

Live GPU acceptance covered D3D12. Other backends retain their existing shared
buffer update implementations and typed CPU validation; they were not exercised
with another capture in this task. Direct edits remain session-local.

Frozen installation verification checked 57 module signatures, 15 skill/reference
files and 20 bridge Python files against source. All matched. The translation
catalog contains 5,183 finished active messages with no new unfinished entries.
The semantic/workflow regression run passed 182 tests plus 6 subtests (one skip);
native CBuffer tests passed 180 assertions in six cases. Selection/context tests
also cover the original request's malformed integer ID and all provider adapters.
