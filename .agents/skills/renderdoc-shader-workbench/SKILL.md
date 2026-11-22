---
name: renderdoc-shader-workbench
description: Investigate a captured Shader or reorganize its source into readable calculations and interfaces, with Bread Shader Workspace delivery and validated replay replacement.
metadata:
  replaylab-category: shader
  replaylab-version: "122"
---

# Shader workbench

## Goal and source route

Use the user's target and requested result. Explain-only work needs evidence;
ordinary captured-Shader organization/edit work delivers readable saved source,
a usable Shader Workspace entry and validated replacement, unless the user
requests source-only, file-only or no-apply. Keep that scope in follow-ups.
Even "organize this Shader" delegates the complete workflow to the application
Agent: asset context, source organization, all-binding naming, validation,
publication and review. The Host tracks facts and missing effects; the Agent
chooses names from evidence, not generated naming templates.
For a Unity restoration, source preparation is the preferred upstream work:
organize/review and compile/validate in Bread, reusing current proof. Permanent
replay apply/live edit is not required. A concrete application/tool limitation
permits retaining an exact proposal and transferring it with passed checks and
unresolved diagnostics, then finishing the remaining work in Unity. This does
not mark the original Shader organization or failed validation complete. The
resource-export Skill owns that transfer/fallback scope; do not restart its
source preparation solely to satisfy a standalone replay-publication obligation.
When asked what a previous edit did or why it is incomplete, inspect saved source
and prior receipts with read tools. Do not start a new organization draft merely
to explain those facts, or silently turn that explanation into another rewrite.
For explanations, use language-tagged code blocks and links to the actual saved
source as described in [source-operations.md](references/source-operations.md).
Choose visual evidence and interactive algorithm examples using the event Skill's
visual-explanations.md reference. A preview example does not validate this Shader
or replace a requested debugger/Unity delivery.

Asset matching is optional when no usable unpacked assets are configured;
unknown names do not block organization. Shader organization is independent of the source engine. Use
[capture-context-routing.md](references/capture-context-routing.md) for detailed
context selection and optional asset-naming routes. Before the broad edit,
establish whether the project has an unpacked asset root from begin's
`assetNamingPreparation` or another current receipt; otherwise call
`get_active_unpacked_asset_index_status` once. A `not-checked` mapping or write
status does not prove names were applied. Follow
[asset-naming.md](references/asset-naming.md) when configured: reuse current
binding-specific mappings and already adapted names, otherwise use the declared
reader to match distinctive bound textures/mesh and follow their asset relations
to materials and Shader parameters. A few unique anchors can suffice; neither
all-texture matching nor a whole-game scan is required. Decode narrow candidate
textures when missing pixels prevent comparison. Directory listings, numeric
ranks and an empty thumbnail index do not establish a match or its absence.

Retain associated material parameters with unresolved offsets in
`materialSemanticContext`. Use their vocabulary and actual Shader consumers to
choose function responsibilities, names and comments in the first edit. Respect
current-draw versus reusable Shader-context provenance. Partial mapping or an
unavailable reader does not block organization: use observed calculation roles,
mark inference and keep physical binding/offset identity. Choose the actual
engine reader and capture-platform layout adapter; do not run every reader or
start Unity just to organize a captured Shader. A requested Unity transfer is a
separate derived source. `get_draw_workflow_plan` is optional routing advice,
not execution evidence or a required gate.

In one working copy, use asset-supported names, organize calculations, then
reconcile CBuffer roles against the finished consumers before final review and
compile/ABI/apply. After validation, publish panel names/channel groups under the
[post-organization contract](references/asset-naming.md#reconcile-names-after-organization).
Source names, a material dictionary and panel fields are separate effects.
Captured-Shader organization includes this delivery even in short follow-ups;
file-only work need not. Publication for different source/replacement bytes is stale.
Known physical lanes can receive inferred roles without original names. Preserve
layout and distinguish recovered, inferred and unknown meanings. Review every
used CBuffer binding and texture name, including view, scene, lighting and per-draw
buffers. Use source alias candidates and per-binding coverage from the contract;
record decisions and repair metadata gaps without recompiling or rediscovering assets.
Choose short, distinguishable texture/CBuffer display names as described in
[resource-naming-delivery.md](references/resource-naming-delivery.md#names-that-can-be-distinguished-in-the-panel);
preserve physical identity and user names, and review meaning and usability rather
than accepting repeated prefixes or published field counts as proof of quality.

Confirm the captured binding and inspect current source. Prefer improving useful
source; preserve captured bytes and user edits. When the source is opaque or
apparently unsuitable, inspect available baseline DXIL/IR and use
`decompile_shader_workspace(reference_only=true)` for an independent reference
when useful. One unchanged same-route result is enough; continue structuring
known calculations instead of repeatedly decompiling. Missing debug source,
original names or inferred material meaning alone does not block organization.
See [reconstruction.md](references/reconstruction.md) for route and recovery facts.
Prefer HLSL on verified Direct3D/Vulkan routes. Convert existing saved GLSL with
`convert_shader_workspace_to_hlsl` and its source hash; retain original/unsaved
source and compare output before apply. Follow reconstruction.md for route
limits: unsupported OpenGL/stages retain the working native source, not token
substitution or an unverified HLSL claim.

Understand what this draw renders from available mesh/texture images, draw state
and related assets; reuse current evidence or inspect missing observations. Confirm
the inferred roles in the code before choosing modules and overview titles.

## Readable implementation

Apply readable mathematics, responsibilities and functional call hierarchy in
the first full-scope edit. Inspect every requested branch, simplify actual
calculations and shared results, preserve numerical requirements, and explain
business functions beside their code. Follow
[implementation-hierarchy.md](references/implementation-hierarchy.md) for the
full authoring criteria and
[whole-program-acceptance.md](references/whole-program-acceptance.md) for coverage,
repair and stopping decisions. These references preserve the detailed criteria;
local validation does not reduce the requested scope.

## Whole-program working copy

For each new broad organization or reorganization request, read the complete
current source again: declarations, helper bodies and alternate branches.
Prior reviews, unchanged hashes and old scope maps cannot replace this assessment.
Reconsider calculation responsibilities and call boundaries; justify retaining
good code without forcing edits or a stage list. Within the same pass, reuse
inspected unchanged source and evidence.
Each new Agent organization binds a task-scoped source-read gate. After begin,
read the returned current proposal before the first source edit. Bread source
reads stay inline and may paginate even for `mode="complete"`: follow the returned
character `nextOffset` until null, rather than treating the first page or a file
copy as a complete read. Runtime-observed owned-file reads can supply equivalent
coverage; ordinary shell/file tools without that observation cannot. Use Bread's
paged read in that case. Same-task Host edits inherit the inspected baseline;
untracked external changes require reading the new bytes. Finalization also
requires a new source assessment recorded in this task. Byte coverage proves
delivery of the program, not understanding; assess its bodies and branches below.
When owned native/API file writes are available, begin with an `agent_file_name`
using the source language's extension, edit that working copy and import with
`source_file`. Otherwise use exact `edits` against the current proposal hash.
Reserve full `content` for complete replacement, not retransmitting unchanged code.

Read coherent computation blocks, including long helper bodies; an outline
cannot establish their contents. Keep a short scope/remaining-work map connecting
each block to its responsibility, inputs/results and shared calculation owner.
Use it to design boundaries, simplify bodies and explain flow in the first edit.
Preserve established asset names and captured bindings/layout. Handle repeated
algorithms and component patterns across all applicable branches and helpers;
a few improved examples do not finish a family. Edit definitions and callers
together, review the whole result, and resume from the map without rediscovering
unchanged paths or publishing each helper separately.
Treat this phase like editing an IDE buffer: write/import persists the draft;
do not compile, run ABI checks, apply or start live edit while arranging its parts.
Use already exposed tools and known schemas directly. Discover only a specific
missing capability or contract, then reuse that result for the rest of the task.
Ordinary organization does not open, reload or restore the capture. Replacement
ownership is coordinated by the Host during final apply, not a prerequisite to editing.

## Agent self-review

Assess the whole requested scope and edit where needed before treating it as
complete. When you believe the draft is ready, review the actual source under
these four criteria:

- `readability`: trace all requested paths; names and plain-language comments
  explain current roles, conventions and non-obvious constraints, separating fact
  from inference. Keep detailed notes beside their calculations.
- `unity_style_structure`: use meaningful internal structs, vectors and math idioms,
  preserving captured language/interface without Unity dependencies or invented stages.
- `function_calls`: assess actual helper bodies and callers against responsibilities;
  share equivalent algorithms while preserving meaningful variants. Wrappers or
  renamed scalar storage do not resolve an opaque implementation.
- `redundant_temporaries`: remove aliases, copies and repetitive component chains.
  Check reuse of computed results as well as helper definitions; preserve values
  needed for evaluation order, precision, derivatives, side effects or clear meaning.

Review every requested computation, helper and branch, including unchanged code.
Repair gaps in the same copy, reassess changed definitions/callers and reuse
unchanged reviews until quality is met or a concrete blocker remains. No fixed
review count applies. For each scope, use
[self-review.md](references/self-review.md#completion-decisions-per-computation-scope):
body simplification, responsibility/call design, shared algorithms and results.
Record outcomes or specific retention reasons. A feasible gap keeps its scope
unmet; success elsewhere cannot compensate.
Challenge the conclusion with begin/write's `sourceReviewObservations`: remaining
channel chains, repeated or unchanged helpers, dense expressions and missing
function comments. These lexical leads are not a semantic score; zero matches
do not cover patterns a detector cannot recognize. Assess the complete linked
catalog, not only its inline preview. In the same review, account for every current
`candidateId` in `observation_dispositions` with its scope and a concrete retention
reason, as specified in [self-review.md](references/self-review.md). Repair feasible
gaps; improving a similar expression elsewhere does not resolve a current example.

Import the reviewed working copy, then call `record_shader_organization_review`
with its exact hash, session, four `checks`, structured `scope_review`, findings,
next actions and verdict under [self-review.md](references/self-review.md).
`complete` means source quality is ready before compilation; `needs_work` means
repair in this task, and `blocked` requires the actual blocker. The Host checks
identity and record structure; the Agent evaluates quality. Reassess meaningful
changes, not each edit. If existing source already meets the whole scope, record
`no_change_reason` with the complete review and report no rewrite; do not create
meaningless edits or skip review to claim completion.

## Physical validation

Completed decompile/organization delivers a usable editable program: resolve
includes, entry interface, resources and compiler route, then validate the saved
bytes for the captured target. Keep broken drafts editable and recoverable;
visibility alone is not completion.

After current complete source review, use one final finish/compile/ABI/apply
cycle. `source-review-pending` means continue the same draft, not end the task.
`publication_mode="checkpoint"` only saves an editable proposal: no compile,
validation, apply or live edit. Ordinary writes/imports already save progress
without requiring review.
Repair concrete compiler diagnostics in the same copy, import, reassess affected
source and retry. For mismatched outputs, follow
[numeric-recovery.md](references/numeric-recovery.md): check mismatch coordinates,
values and baseline stability, isolate dependency-coherent changes, then repair.
Reuse unchanged proof; compile/ABI success proves neither behavior nor readability.
Read each delivery stage's result. A save cannot override failed validation/apply;
failure calls for diagnosis and supported recovery. Preserve work and name real
blockers. Even source-only delivery requires the reviewed proposal saved into
the workspace, not merely reviewed in an unsaved working copy.

## Deliver the actual result

- Confirm the saved source and requested diagram describe the final calculations
  and function calls, including the paths reviewed above.
  Directory and flow cards summarize the major effects, grouping supporting
  helpers. Review duplicate phases, overly fine splits, missing effects and
  source anchors together. Flow cards show a short purpose above the larger
  function name, without input/output lists. Drill-down follows actual modules;
  return goes to their parent. See algorithm-design.md for the overview contract.
- Update any existing analysis/diagram notes to the final source and delivery
  receipts. Mark old checkpoint conclusions as historical; no extra artifact
  or validation round is required.
- Show the matching workspace document and check its returned identity and
  `readOnly` state. Display may reuse a clean canonical editor or open an editable
  source draft; source-only saves do not create live-edit authority. Requested writable editing
  needs a writable matching document, not just an eligible record.
- Final finish applies and records the project association together. A successful
  `replacementRegistration` needs no second registration or validation. Repair
  metadata-only failure using its exact recovery parameters; never recompile or
  reorganize unchanged source. See source-operations.md. Read back only missing,
  stale or ambiguous receipts; registration alone does not prove replay changed.

Use respective evidence for readability, saved source, editability and replay
effects. In the main Agent's final `record_task_review`, cite current source and
delivery receipts; repair requested gaps without repeating unchanged checks.
Explain briefly what changed, whether it is applied, which unpacked assets
matched and informed names/interpretation, and remaining uncertainty. Distinguish
matched names from inferred roles; do not hide unresolved bindings.
