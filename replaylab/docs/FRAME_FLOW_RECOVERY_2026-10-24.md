# Frame-flow recovery audit — 2026-10-24

This change repairs Bread AI's analysis/recovery and presentation paths. It does
not manually reinterpret or republish the user's frame. Capture-specific events
in the incident evidence are observations, never required investigation targets.

## Incident and evidence

The inspected AI task was `39c2c5097b1a4cc98d1d46908c0cc64d`, from approximately
20:47:23 to 21:07:18 local time (19m55s). Its retained JSONL and report-state
sidecar are under `%LOCALAPPDATA%/ReplayLab/ai-tasks/`. It requested reanalysis
and returned a 24-stage partial diagram.

The native failure predates the task: at 20:46:33 the second project-restored
shared Shader application hit a GPU timeout. Native replay switched to
`DummyDriver`. Selecting another EID cannot repair that controller. This is
different from a legitimate draw with no pixel Shader, a buffer-only dispatch,
or a utility action without graphics bindings.

Subsequent evidence showed:

- All 24 new stage inspections returned empty Shader/input/output inventories,
  no binding errors and `bindingsComplete=true`. These invalid empty observations
  could replace previously useful bindings.
- No new Shader read succeeded. One representation request reported
  `RenderDoc representation stage contradicts the request`, masking the bridge's
  underlying `No shader at stage 'fragment'` response. Multiple read routes
  depended on the same unavailable native state.
- Publication attempted channel preparation for 76 parent previews; 61 failed,
  with 162 per-channel binding errors. The old PNGs still existed. Treating a
  failed refresh as absent evidence also affected popup visibility.
- The saved diagram's actual dependency subgraphs fell from 15 to 3. Besides
  failed inspection, compatibility/validation of earlier resource-version base
  points was too strict. Removing a valid graph is not a legitimate way to make
  a validation report shorter.
- The task did reuse old images and sources. That was useful evidence reuse,
  not successful fresh replay validation. The 24 stage requests were submitted
  together before their results returned, so they are not evidence of 24 knowing
  retries after the same failure.

The 19m55s duration was not all GPU time: retained Host calls accounted for about
104 seconds, including about 65 seconds in publication. The rest includes Agent
processing and orchestration. No single unmeasured operation is assigned all of
that latency.

## Implementation changes

### Replay and evidence validity

- The bridge checks native fatal status and rejects unavailable replay with a
  distinct diagnostic instead of allowing empty fallback pipeline objects to
  masquerade as observations.
- `frame_stage_evidence.py` distinguishes an impossible all-empty executing
  pipeline from valid utility, depth-only and non-texture workloads.
- `FrameRenderFlowStore` rejects invalid replacement inventories, keeps prior
  observations and their revision, and records the failed read separately.
  Cached results returned after a failed refresh are marked previously captured,
  with `refreshSucceeded=false`; they are not certified as freshly inspected.
- A known fatal replay state stops additional binding/channel replay fan-out.
  Valid saved evidence remains available. `inventoryVersion` survives diagram
  persistence, so restoring a document does not silently lose the contract used
  to interpret its bindings.
- Failed Shader queries now report the original bridge error before checking
  the identity of a successful representation. Genuine successful-response
  stage mismatches still fail validation.
- Resource-graph compatibility preserves valid base-point/version relationships;
  dependency checks still require real resource flow rather than adjacent EIDs,
  similar names or shared bindings alone.

### Saved channel previews

Offline inspection found complete saved channel plans and PNGs for all 61 groups
whose fresh preparation failed. Previously `channelCacheComplete=false` caused
the popup to start with no images; another failed replay then left it blank.
The UI now immediately displays available saved previews, distinguishing them
from a current verified result. Failed refreshes keep the popup and old images;
they do not combine an old complete set with a new partial set and certify the
mixture as fresh. Revision-provider failures/changes retain the visible saved
content. Parent association also checks EID/resource/subresource identity even
when `parentImageIndex` is present, preventing unrelated channel groups from
being attached by index alone.

### Bounded native recovery

`get_replay_health()` reports native controller health without changing the EID.
`recover_replay()` is a conditional repair for a confirmed fatal controller, not
a normal prerequisite or a generic response to an empty result. It reopens the
same local RDC at most once, preserves source drafts, project data and saved
diagrams, and restores selection.

Recovery returns a **new capture session with original capture content**.
Old native Shader replacements and CBuffer edits are invalidated, not blindly
reapplied. Read current context and prepare a new flow after success. Historical
bindings/media must not be reported as current validation; matching the previous
edited picture requires separate validation of the required replacement.

### AI analysis and final reporting

`renderdoc-explain-event` is now v22. Its instructions require:

- Reassess important existing conclusions and actual resource dependencies for
  a reanalysis request; refreshing an outline, changing layout, deleting graphs
  or republishing old conclusions is insufficient.
- Derive functional modules from this capture's execution order and versioned
  resource flow. Trace important actual reads backward to effective producers,
  respecting subresources, partial writes and history boundaries, then confirm
  meaningful downstream use. Cache shared dependencies and investigate only
  unresolved boundaries.
- Actually view relevant RT images. Cross-check useful clues against mesh shape
  and input layout, VS deformation/projection, culling/depth/stencil/blend state,
  Shader mathematics and available unpacked asset associations. Choose evidence
  relevant to the question; no single image, name or triangle count proves a role.
- Reuse verified static bindings, source and PNGs with their provenance. Replay
  only when fresh pixels, dynamic bindings or execution facts are needed. Static
  evidence cannot establish every current branch, contribution or post-edit
  result. A read failure does not erase a previously supported relationship.
- Change to another useful read-only evidence route when recovery is possible.
  Do not repeat equivalent calls against unchanged fatal state, create editable
  Shader workspaces, or compile replacements merely to explain a frame.
- Keep the final answer short: what is ready, what remains, the actual cause and
  the practical next action. Detailed gate/retry history stays in the task log;
  unfinished work remains explicitly unfinished.

The reference rendering diagram was inspected in local sections. It informs
expression only: short functional titles, ordinary-language explanations,
few representative results and clear composition relationships. Its effects,
stage count, EIDs, engine and algorithms are not a checklist for another capture.
The main spine follows execution order; subgraphs show actual cross-draw/target
production, transformation and consumption. Parallel MRTs, single-draw arithmetic
and one snapshot repeated under different labels are not causal chains. Use
representative images where an effect appears, changes or joins a downstream
result, without prescribing an image count or requiring a GIF/slider everywhere.

## Actual Skill routing and packaging

The code path is shared across Bread's native and API connections:

1. `cli.default_skill_root()` uses the source `.agents/skills` in development or
   `sys._MEIPASS/.agents/skills` in the frozen executable.
2. `AgentToolsConfig.skill_root_list()` orders the bundled root before explicitly
   configured user roots. A later user root can intentionally override a same-ID
   Skill; arbitrary Codex global Skill directories are not scanned.
3. `CatalogView` loads that catalog. Native prompts expose its descriptions and
   task MCP routes forward the same roots to `SkillGateway/load_bread_skill`.
   The Agent chooses the relevant domain Skill; UI intent does not select a
   fixed algorithm or phase list.
4. `Build-ReplayLabRuntime.ps1` packages every repository folder containing
   `SKILL.md`, including its references. Its frozen doctor smoke test uses an
   empty tools configuration and checks the full bundled Skill count. It does
   not alone prove a particular Skill version, so the deployed version must
   also be checked explicitly.

The user's inspected `agent-tools.json` had `skillRoots=[]` and
`disabledSkills=[]`. Bread's Codex preset also overrides inherited `CODEX_HOME`
with `%LOCALAPPDATA%/ReplayLab/codex/<profile>`, isolating the desktop Codex global
configuration. Thus the old globally installed `replaylab-debugger` fixed
classification/retired panel instructions were **not** in this Bread catalog.
Those external Skills were audited read-only, not modified.

Before this release was rebuilt, a read-only invocation of the installed
`x64/Development/replaylab/replaylab-ai.exe tools list --json` returned the bundled
`renderdoc-explain-event` **v20**, enabled, from its temporary `_MEIPASS` directory.
This was the pre-deployment state, not the final installed version. The completed
**1.13.60** deployment now reports bundled, enabled **v22**, verified in
`%TEMP%/bread-tools-installed-160.json`. Source tests alone were not used as proof
that the installed binary contained the new guidance.

## Verification status

- Skill/prompt/tool-reference regression suite: **102 passed** before the final
  conditional recovery-tool guidance was added.
- The subsequent combined Skill, gateway, settings, selected native routing and
  Codex profile-isolation suite: **174 passed, 2 skipped**. No paid model session
  or user-frame reanalysis was started by this audit.
- Final frame/server, background, representation and recovery regression suite:
  **803 passed, 6 subtests passed**. Earlier UI/channel checks also passed;
  these totals overlap and must not be added together.
- Skill frontmatter validation, source diff checks, entrypoint budget and
  lossless reference delivery passed. The final recovery paragraph remains
  covered by the complete-reference delivery regression.
- All **254 frozen source files** remained unchanged. The 14 inspected frozen
  modules matched the corresponding source-compiled code structures, and six
  packaged Skill Markdown files matched their source bytes. Evidence:
  `%TEMP%/bread-frame-flow-deploy-160/package-verification.json`.
- Native frame-flow UI fixture: **103 checks passed in 7.84 seconds**. All four
  saved R/G/B/A previews loaded and remained visible when current replay was
  unavailable. Evidence: `%TEMP%/bread-frame-flow-native-160/result.json`.
- Native replay-recovery fixture: **25 checks passed in 47.9 seconds**. Only
  the first health receipt was injected as fatal; no actual GPU fault was
  induced. Real `LoadCapture`, controller recreation, new session, EID restoration,
  dirty native Scintilla text, unsent AI attachment preservation, old-session
  graph history and subsequent healthy Shader/pipeline reads were exercised.
  Shader replacements were not reapplied. RDC and sidecar contents were unchanged.
  Evidence: `%TEMP%/bread-replay-recovery-native-160/result.json`.
- Earlier fixture-only errors involving parallel Qt popup focus and proxy
  context were corrected; final native checks ran serially. They are not
  presented as additional product defects or as GPU-failure reproductions.
- All four localization gates passed. The native optimized build passed with
  `/m:2` and explicit `SolutionDir`; final runtime and integration were deployed.
  Deployment rechecked all 29 bridge files. The main application restarted as
  PID 51768 with no capture or AI task supplied.

These checks verify recovery behavior and evidence/UI preservation. They do not
prove that the original GPU-timeout cause has been eliminated, nor that the
user's diagram has been newly regenerated or fully revalidated. No paid AI
analysis or graph publication was performed for this development verification;
the main application is opened without a capture for the user's own next test.
