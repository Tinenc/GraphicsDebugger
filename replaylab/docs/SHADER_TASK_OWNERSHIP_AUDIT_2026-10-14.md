# Bread AI Shader task audit — 2026-10-14

## Evidence and outcome

The audited task is `dde6eb1652704ce6add0ad6b86781592`, Codex app-server model
`gpt-6-sol`, targeting EID 11402 / fragment / MainPS in the user's `6.rdc`.
The task trace is `%LOCALAPPDATA%/ReplayLab/ai-tasks/<task-id>.jsonl`.
The corresponding console file is
`events.qrenderdoc.32760.50202a9ce6e84b4581f9ec84fd2bc1c7.0000.jsonl`.

This task did **not** finish the requested whole-source organization. It saved
partial edits and compiled candidates, but failed to replace the active shader.
The task trace ends with an in-progress `open_capture` invocation; it contains no
normal final, failed, or cancelled terminal event. The screenshot says interrupted.
There is no evidence here that a token/time budget terminated the task.
The interruption chain is explained by the pre-fix capture lifecycle: `open_capture`
reloads the RDC; `AIAssistant::OnCaptureClosed/OnCaptureLoaded` invokes
`clearCaptureTasks()`, which cancels the capture-bound chat as `interrupted`.
That cancellation uses a zero cooperative wait before closing the child-process
Job and killing the remaining process, so the Agent can be stopped before it
writes a terminal JSONL event.
This matches the last tool invocation and the screenshot's interrupted state.

## Observed execution (local time, UTC+8)

| Time | Actual operation |
| --- | --- |
| 09:58:19.559 | Task queued; provider execution and tool/skill loading follow. |
| Before first write | Reads the complete 318,143-byte source once. A native working copy is created but remains unchanged. |
| 10:04:07.268 | First whole-content proposal write. |
| 10:04:11.395–18.338 | First explicit checkpoint finish, 6.943 seconds. |
| 10:09:38.629 | Second whole-content proposal write. |
| 10:09:42.644–58.862 | Second checkpoint finish, 16.218 seconds. |
| 10:11:46.304 | Third whole-content proposal write. |
| 10:12:32.764 | One source review declares `complete`, while admitting remaining numbered variables and fallback calculations in the entry. |
| 10:12:42.253–10:13:04.114 | Final finish, 21.861 seconds; apply fails on foreign workspace ownership. |
| Until 10:20:40.092 | Repeated capability lookup, apply/occupancy checks and recovery attempts; generic task review records `blocked`. |
| 10:20:48.360 | Host resumes the same provider session for blocker recovery. |
| 10:22:49.618 | Last trace event: `open_capture` is running. Subsequent console entries show project restoration failures. |

There are 78 completed Bread tool calls including failures, of which 28 are
capability discovery calls. There is one full source read, three whole-content
writes, three finish calls, one source review, and one generic task review.
The three finishes produced six compile revisions and six ABI checks (baseline
plus proposal per finish). There were four explicit shared applies plus the
final finish's internal apply. Roughly the last ten minutes were spent on
application/recovery rather than improving the source.

Timestamp analysis refines that attribution: from the failed final apply to the
last `open_capture` start is **585.504 seconds**. The union of the 58 completed
tool intervals in that window is **57.451 seconds**. All 28 capability lookups
occur in this recovery window and total only **0.302 seconds** (median 10 ms,
maximum 21 ms). The remaining **528.053 seconds** occur outside recorded tool
intervals; this trace cannot separate model generation, decisions, transport and
continuation overhead. Thus the ten-minute delay was repeated Agent recovery,
not a slow capability-list endpoint. The main organization tools were already
among the 33 directly exposed core tools. One full-source read took 0.175 seconds
and the three whole-content writes together took 2.025 seconds.

The provider says its native shell was rejected. The handshake proves scoped
workspace write access, but retained logs do not preserve the original rejected
command/result, so its exact cause is unconfirmed. The connector already disables
login shells; its model context now explicitly instructs `exec_command(login=false)`
and a single corrected invocation before choosing a fallback. This is a capability
usage clarification, not permission expansion.

## What changed in the shader, and what remains

The source saved from the proposal is in workspace
`2735c043506873d7e174c989eedca3f0d43a80e8d9c7925732601b79eb36e2bd`,
source SHA-256 `57dab1a270acd9c65bb9f874bc435c408a047fa70512d03b75a4bb96b87358a4`.
It is 322,241 bytes / 4,398 lines, versus 318,143 bytes / 4,386 baseline lines.
Function declarations increased from 35 to 40; numbered local declarations
decreased from 2,193 to 2,002. These numbers describe the edit, not its quality.

Three major variants were extracted and some context/light/occlusion helpers
were introduced. However:

- `CalculateRainTrailNormal` still contains the numbered hash/mask chain around
  lines 666–681.
- `ShadeVariant1` retains separate x/y/z scalar chains around lines 2262–2270.
- `ShadeVariant2` and `ShadeVariant3` remain about 534 and 403 lines respectively.
- `MainPS_inner` remains about 931 lines with fourth/fallback/selection logic.
- Lines 4369–4377 retain a sequence of color copies through
  `selectionBlendColor`, `selectionOrUnlitColor`, `fallbackShadingColor`,
  `nonWetVariantColor`, `selectedVariantColor`, and `linearColor`.

The agent must inspect scope, precision and behavior before merging those
calculations. Compilation/ABI success alone cannot establish readable structure
or whole-file completion. This repair does not silently edit the user's shader.

## Implementation faults and repairs

1. **Inconsistent ownership scope.** Native replacement ownership is keyed by
   current capture session and original ResourceId. Inventory queries previously
   filtered by catalog captureId. Different bounded catalogs of the same capture
   therefore produced “occupied” followed by “no foreign owner”. The inventory now
   supports a session-wide read with each owner's original catalog preserved.
   The Host reuses a canonical live workspace or performs a checked handoff of an
   idle owner. It does not remove the last-good replacement before the candidate
   is validated. Handoff compares the exact old replacement/workspace/session/
   revision/candidate identity. Failure before a checked mutation commits leaves
   the prior target intact. If later validation fails, the Bridge attempts rollback;
   if that rollback also fails after commit, it preserves the still-bound new
   target as an explicit recovery owner and reports failure, without issuing a
   successful validation receipt or freeing that active target. A successful
   handoff gets a new replacement identity. Active writers
   still require serialization; removing all mutual exclusion would introduce
   lost edits and replay races.

2. **Cross-session resource cache relabelling.** A cached target inventory carried
   old ResourceIds 723/755/860 (full IDs have prefix `100000000000000`) but its
   captureSessionId was overwritten with the new session. The pair fast path
   consequently skipped anchor rebinding and tried nonexistent old IDs. Read-only
   live checks in one reopened session found 723@11371 → 5421,
   755@11402 → 5453, 860@11329 → 5558; these are observations, not stable mappings
   for later sessions.
   That routing cache now only hits within its original session. Across sessions,
   the existing bounded target index obtains live IDs. Signed verified-RDC,
   shader-pair and bytecode caches remain available; recompilation is not added.
   Different current resources with identical bytecode remain distinct.

3. **Duplicate workspace admission/presentation.** Workspace discovery now checks
   the live session-wide owner even if local hints use another catalog; it also
   reads the actual `{ui: ...}` response wrapper. Direct preparation of an absent
   deterministic path follows that canonical owner instead of creating another
   copy. Explicit existing drafts keep their own paths and source. A preview of
   the same saved source reuses its document even when live-edit has stopped;
   this does not grant write permission. Historical saved drafts are not deleted.

4. **Premature source completion.** The existing four prose review checks remain,
   with structured scope evidence attached to the same review call. The review
   binds the organization baseline source hash, covers the lexical function-name
   inventories of baseline and current source, lists Agent-declared branch/path
   outcomes and retained-intermediate rationale, and accounts
   for remaining work. A complete verdict cannot contain unmet/blocked scopes or
   remaining work. Legacy prose-only reviews remain readable but cannot authorize
   a new final completion. Changed source bytes invalidate an old review. This
   checks inventory coverage and record contradictions; the agent still owns
   semantic quality. Overloads share a function-name group. The Host does not
   prove that the Agent's declared branch list is exhaustive, that every body was
   understood, or that a submitted plan faithfully retains the original request.
   Those remain source-review responsibilities; this change does not establish
   that the user's partial Shader is now organized or that a future model run
   will necessarily produce a satisfactory rewrite.

5. **Unnecessary intermediate validation.** The skill now explicitly treats
   proposal writes as durable progress. Following the user's clarification,
   intermediate compilation/validation requires an explicit user request;
   a generic technical-risk rationale is no longer an exception. The sequence is complete source
   → one working copy covering all branches → whole-source review → batch repairs
   → compile/ABI/output validation/apply. Runtime continuations preserve the same
   provider session; the guidance asks the Agent to reuse loaded skills and
   evidence. Known exposed tools with known schemas are called directly;
   discovery is limited to a specific missing capability or contract, and its
   result is reused. This guidance does not mechanically prevent every redundant
   model tool call or establish semantic source quality.

6. **Self-interruption by reopening the same capture.** `open_capture` now returns
   the existing session for the same loaded normalized path, before releasing
   replacements or invoking load/close callbacks. Explicit `force_reload=true`
   retains reload behavior and documents that it interrupts capture-bound tasks;
   internal project-preselected loading retains its existing lifecycle. The
   skill excludes capture open/reload and ownership recovery from ordinary source
   editing; ownership coordination belongs to the Host's final apply operation.

7. **An old published version mistaken for an unpublished edit.** Real-app
   verification exposed a further restoration fault: a validated local candidate
   from an older publication overrode the active project head, while the report
   still named the new head. The project service now recognizes an exact older
   publication only when workspace, physical shader, source, revision and compiled
   candidate all agree. Before advancing it, the existing seed path rechecks the
   active head ID/generation under the project lock and source/revision/candidate
   under the workspace lock, and preserves the prior text in a backup. Unknown
   or unpublished edits retain their existing protection. Applied reports now
   distinguish actual source/candidate/revision from the registered project
   source/candidate. The first migration may compile once; subsequent signed
   restore-cache reuse still requires all existing identity and receipt checks.

8. **Backup reporting rejected by the UI client.** The first real migration
   correctly restored `57dab1a2…358a4` / `041887ef…f20fc0` as revision 22 and
   backed up the old `b5326791…499c5` source, but the client rejected the result.
   Its top-level shader schema had omitted the existing `supersededLocalEdits`
   counter, encountered here for the first time. The client now accepts that
   counter only as a bounded integer matching the actual backup items. A fixture
   derived from the real report covers this case and rejects contradictory
   counts. Empty optional project-candidate hashes were not the cause.

## Validation

Targeted automated coverage includes cross-catalog owner lookup, checked handoff
and failure retention, old operation-token invalidation, canonical workspace
admission/source preservation, preview document reuse, review scope omissions and
contradictions, same-session cache hits and changed-session resource rebinding.
The source-review/finish/runtime regression group passed 49 cases. The targeted
capture-open/lifecycle group passed two cases, including same-path no-op,
explicit reload, different-path loading and project-preselected loading. The
Skill validator passed. These tests validate contracts and continuation behavior;
they do not constitute a new AI organization run or semantic approval of the
user's Shader.

The **initial** real capture-reopen acceptance failed. The isolated probe
`C:/Users/YC/AppData/Local/Temp/bread-real-restore-cold-66nhfilw/result.json`
reports three applied items, but its own `ok` is false and its final check raises
`Restored original/candidate bytes differ from the saved project head`. For the
original resource ending in 755, the item names active knowledge
`a27522742ad7b1df4bd90314508b9496` while `localValidatedCandidate=true` selects
workspace `cfe2cfeab01344e4a1902281388f3633`, revision 21, with old source
`b5326791b21356d35d422c149e44a2fe29e3f1eca9137c62c536ce98b98499c5`
and old candidate
`48160f1ff683a3a6894625ab1683ca3bfa825aa3ef8f48d347fb4d9101d20116`.
The intended active head contains source `57dab1a2…358a4` and candidate
`041887ef…f20fc0`. The old historically published version was being protected as
if it were a newer unpublished local edit. That selection fault has a source fix;
the applied count alone does not demonstrate that the requested version, graph
or source navigation was restored. A new successful physical-head check is
required before describing end-to-end restore acceptance as passed. Even that
would establish restoration of the saved partial source, not completion of its
requested organization.

The additional publication/restore/client regression group passed 273 tests and
291 subtests, including backup preservation, rejection of unknown edits and new
revisions, head-generation races, source/candidate CAS, actual applied hashes and
client rejection of malformed hash fields. Source fixes alone do not supersede
the failed real-app observation above; rebuilt-application results follow below.

The subsequent run `bread-real-restore-cold-5abocb97` restored the correct physical
source/candidate and wrote a signed verified-RDC cache, but failed client delivery
on the backup counter described above. After the client repair,
`bread-real-restore-warm-801uaewx` passed actual head/bytecode checks and displayed
exactly three workspace leaves, one per current shader, with no extra historical
nodes. It completed verified-RDC restoration in 39.76 seconds including 18.74
seconds loading the capture; it did not compile again. Its expanded flow test
then exposed an incorrect test assumption: expansion points at the callee while
the source anchor belongs to the caller. That run is not counted as the final
two-open/navigation acceptance run.

### Final installed-application acceptance

Bread runtime **1.13.26** was built and installed in
`D:/Study/GitHub/renderdocUltra/x64/Development`. Fourteen frozen module copies
matched current source bytecode, and deployment verified all 21 first-party
integration files. The native build and translation update/check/build/lint
gates passed. The final bridge counter repair passed 212 restore/client tests
and 222 subtests, including the real migration-report fixture.

The actual-user-profile run
`C:/Users/YC/AppData/Local/Temp/bread-real-restore-warm-unpdnayo/result.json`
finished with `ok=true`, normal process exit, and all acceptance assertions true.
It opened the real RDC, allowed normal automatic restoration, checked the UI,
then closed and reopened the capture in the same process. No model was called.

| Observation | First open | Second open |
| --- | --- | --- |
| Capture load | 18.986 s | 18.963 s |
| Subsequent restore | 20.669 s | 20.183 s |
| Total load + restore | 39.655 s | 39.146 s |
| Restore route | verified-RDC cache | verified-RDC cache |
| Applied / editable shaders | 3 / 3 | 3 / 3 |
| Restore warnings | 0 | 0 |
| Actual workspace tree leaves | 3, no extras | 3, no extras |
| All flow widgets / navigation | pass | pass |
| Restore UI heartbeat P95 / max | 13.243 / 828.518 ms | 15.210 / 805.175 ms |

Physical ResourceIds changed from suffixes `723/755/860` to `3016/3048/3153` on
the second open. Both rounds matched the active project heads and actual candidate
bytes, displayed one workspace per current physical owner, bound graphs to the
actual editor source and document identity, and passed function-call navigation,
expansion and back navigation. Same-file `open_capture` preserved session and
owners. No new compilation/revision was created during these two opens; the
source-origin workspaces and candidate bytes were preserved.

This confirms the tested restore/duplicate/graph/lifecycle paths. It does not
claim an instantaneous restore, absence of every short UI stall, successful
completion of a new long AI conversation, or completion of the user's partial
Shader rewrite. The remaining ~0.8-second gaps are measured limitations, not
hidden behind the passing functional result.

## Follow-up: separate text editing from physical publication (1.13.27)

The workspace source is an ordinary UTF-8 document. Native replacement ownership
protects the currently bound GPU resource, not this document. Draft processing
previously called the broad workspace status validator and checkpoint finish
entered compilation; canonical draft saves could also wake automatic compilation.
Those couplings made a simple source edit dependent on runtime recovery.

- `source_edit_status` checks local source/baseline identity and current text
  without loading candidates, revalidating toolchains, parsing executable ABI,
  repairing state, connecting to replay, or writing the manifest. Its distinct
  source-edit-only schema explicitly grants no ABI/apply authority. Draft reads,
  begin, writes, source operations and review use this path; final physical
  validation retains the strict path.
- Checkpoint finish persists the proposal only. Combining it with validation,
  apply or live-edit flags is rejected before any physical effect. Plain source
  save remains available without declaring completion. A complete review bound
  to current proposal bytes is required before final physical actions.
- An open organization draft defers automatic watcher compilation/adoption.
  Explicit native editor save/compile remains a separate user action. Concurrent
  source saves still use short transactions and compare-and-swap to avoid lost
  edits; there is no conversation-long exclusive file ownership.
- Main-agent and subagent discovery now report whether a tool is directly exposed
  to that actor. Already exposed tools use their typed endpoint; only a missing
  long-tail contract requires directory discovery and the generic dispatcher.
  The 33-tool core was not enlarged. Skills and runtime instructions exclude
  capture reopening from ordinary organization and put all branches and whole-file
  review before final compilation.

Initial verification: the complete workspace-service suite passed 39 tests,
including four new source-only tests. An independent review found no blocker.
A read-only pass over all 39 real project workspaces completed in 0.485 seconds,
with no failures or source/manifest edits. This is a local text-read measurement,
not AI generation time or capture-load latency. The native incremental build
and translation synchronization check passed. Installed-runtime acceptance is
recorded below after deployment.

The organization/read/write/watcher/publication regression group passed 220
tests and 24 subtests, with two pre-existing skips. It includes a real temporary
workspace with a broken previous candidate and stale compiler binding: public
begin, write, checkpoint and plain source-save all succeed while strict status,
compile and replay IPC are instrumented to fail if called. Watcher tests cover
zero automatic compilation/adoption during drafting, explicit editor saves,
resumed draft pointers, and recovery after malformed pointer data. A normal
reviewed final submission compiles and applies once; the optional strict output
comparison route still compiles baseline plus final candidate.

Agent tool-routing tests passed 101 tests with two skips; workflow/review tests
passed 63 tests and both bundled Skill validations passed. These prove tested
contracts, not that a new model conversation will necessarily organize every
calculation correctly. Low-level shared apply/validate endpoints also serve
project restore and therefore do not receive a global draft gate; the intended
Agent workflow uses the reviewed final endpoint.

Runtime 1.13.27 was built, deployed and package validation passed. Thirteen frozen
module copies matched current source and all 21 first-party integration files
were verified at the fixed application path. The existing application was
normally closed after a read-only check confirmed no dirty editor document.

Installed 1.13.27 acceptance:
`C:/Users/YC/AppData/Local/Temp/bread-real-restore-warm-84hjvevq/result.json`
returned `ok=true` and exit code 0 after two opens in the actual user profile.
All three shaders restored and remained editable, all three flow widgets and
navigation worked, and the tree contained one workspace per current physical
owner. Source bytes, candidate bytes and revisions were unchanged, with no new
compile history. Same-path open preserved the session/owners. Load plus restore
was 39.655 seconds then 38.738 seconds; the reported restore heartbeat P95 was
12.899 ms and maximum gap 776.506 ms. This still does not claim zero short UI
stalls or instantaneous capture loading. No model request was made. After this
acceptance process exited normally, the application was restarted with `6.rdc`.
