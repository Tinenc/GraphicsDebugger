---
name: renderdoc-explain-event
description: Explain captured rendering effects, objects and calculations with evidence and appropriate visual explanations; investigate missing output or organize a requested EID, pass or frame flow in Bread.
metadata:
  replaylab-category: event-pipeline
  replaylab-version: "55"
---

# Explain captured events

The Agent chooses the scope, tools and presentation for the current capture;
never inherit another capture's EIDs or conclusions. Technical names and examples
are not a taxonomy that the answer must fit. Derive the explanation, scope and
relative importance from the inspected data and falsifiable tests.
No required phase prefix.

For annotated/chat questions, read [visual-explanations.md](references/visual-explanations.md)
to choose images, code, local graphs and interactive meshes according to the user's
actual purpose, including vertex/compute debugging and Unity restoration.

For a local object/color/algorithm/effect question, choose the smallest useful
answer. A simple fact or count needs a direct answer and only the lookup needed
to establish it. When visuals help, use `publish_visual_reply` for relevant
images, comparisons or a compact graph of the actual dependencies. Its expanded viewer is independent. Never
replace the global Frame Rendering Flow or its capture sidecar for a local explanation.
For a Shader calculation diagram with controlled comparisons, organized code
for every tested Shader is part of the visual delivery. Treat shaderCodeGaps in
the publication receipt as remaining work, even when the question is phrased as
an explanation. Use the Shader workbench workflow on the actual EID and stage.
For a marked effect, identify the visible pieces before naming an algorithm.
Check neighbouring draws, even when a sibling uses a different Shader or a
texture in place of a procedural shape. Trace inputs and show how the pieces
combine. For a layered effect, give each confirmed contributor a separate
controlled final comparison, a computed result on the local graph and its
relevant source and texture links. Resolve markedEffect gaps before completing.
Use the all-layers-enabled final image as the common A side. A different
Shader or binding cannot exclude a candidate. Disable its contribution in a
restored experiment and check the same final output and marked region before
excluding it. Start a new chat's analysis from its own evidence and question.
Write the full standalone explanation in reviewed_answer even when a review
continuation only asks you to finish verification.
Check related disabled branches in the captured parameter state. Mention them
after the active explanation and illustrate with a labelled captured diagnostic
or controlled activation when useful and supported. Never conflate an activated
experiment with a contribution present in this frame.
Reuse a current finalized source when it
already explains the calculation. Otherwise organize the whole affected Shader,
name its CBuffers, fields and textures by observed roles, reconcile applicable
display aliases and validate the finalized source. For explanation, finish with
validation and `source_review_policy="strict"`, without permanent replay apply.
Publish excerpts and exact Shader
Workspace line links from the finalized source. A raw decompile is investigation
material. If organization is blocked, continue with verified pictures and plain
language and state the specific missing code evidence. Use `get_cbuffer_parameter_link`
for reflected parameter/component links. Keep annotation coordinates tied to their output EID/RT and
display orientation. A flipped earlier HDR target is not the same image basis as the
final presentation. Verify the full and ROI pictures actually contain the marked target;
never interpret a background ROI as evidence that an effect is inactive. Prefer observing
the annotated final output; map coordinates explicitly before using a different RT.
Establish a cause with `run_shader_effect_experiment`
where supported: same output event/region, unchanged-source baseline control, stable
A/B/A, physical ABI checks and confirmed restoration. Publish its exact image pair
and experimentReceipt for a verified slider. Different EIDs only compare stages.
An explanation is an inspection task. Use scoped experiments that restore the
prior replay state even after an error or cancellation. Keep existing user
replacements and parameter overrides intact. Never leave an experiment applied,
start a persistent live editor, or promote an experiment as a project restore
source. Verify rollback before completion and retain the comparison images.
If replay already uses a project replacement that affects the question, distinguish
it from the captured original using its recorded identity and current evidence.
For final appearance, call run_shader_effect_experiment with
observation_scope="appearance". It resolves a single downstream captured Present,
corrects an early draw hint and verifies the actual EID and destination on replay.
If several candidates remain, trace the effect's resources through grading and
composition, and choose the exact Present for the intended surface. Reuse it and
the display mapping for all layers. If no final output can be verified, explain
the limitation and keep intermediate RT results explicitly scoped as stages.
Rank the main observed causes; briefly mention a relevant disabled/low-impact
mechanism with evidence, without presenting it as this frame's active contribution.
Material/specular color, lighting and post effects can overlap; revise a hypothesis
when depth, meshes, resource usage, mathematics or controlled results contradict it.

Only for an explicitly requested whole-frame diagram, use `prepare_frame_render_flow` and `get_frame_render_flow` for the bound outline,
then `publish_frame_render_flow(publication_scope='frame')` for Bread's Frame Rendering Flow window. Read
[references/frame-rendering-flow.md](references/frame-rendering-flow.md) for
grouping, images and navigation, and
[references/algorithm-investigation.md](references/algorithm-investigation.md)
for tracing key results and composition. Read
[module discovery](references/module-discovery.md) to understand the rendering path before grouping,
and [render-path review](references/render-path-review.md) to test that explanation.
For new analysis or an explicit redraw, use `prepare_frame_render_flow(reanalyze=True)` and
build a new draft from raw evidence; old stages are only a later comparison.
Recheck important conclusions and dependencies, not just flagged gaps or layout;
re-publishing the baseline is not reanalysis.
Follow [reanalysis](references/reanalysis.md) for a fresh assessment and dependency disputes.
Use section="proposals" and section="quality" for the new draft.
Reuse verified static bindings, source and PNGs first; static evidence cannot answer everything.
For Vulkan indirect arguments, `inspect_event_api_parameters` can read the exact
pre-action argument buffer when a single command lacks structured decoding; the
prior replay event is restored. A generic current-state buffer read is different
evidence. A resolved zero workload is an observed no-op: explain its place in the
sequence without claiming an executed contribution, and continue analysis of the
other stages. Retain unknown multi-command child selection as a precise limit,
not a reason to repeat an unchanged full-frame investigation.
An empty pipeline read is not new evidence: retain valid bindings and change the
failed route; never call reused evidence a new validation.
Choose modules, EIDs and relevant RTs from this capture's execution order and
resource flow. Historical EIDs and example techniques are not required targets
or a module checklist. Trace key output versions backward through actual Shader
reads and effective writes, then forward to their downstream use. Respect
subresources, partial writes and history boundaries; cache shared dependencies.
Record sources, calculations and integration for verification, not UI detail panels.
Nodes contain a short plain-language summary, without Inputs/Calculation/Outputs
labels or calculation-details sections. The main sequence follows execution;
a horizontal processGraph shows verified cross-RT handoffs, branches and merges, with a texture
per node. Single-draw arithmetic, parallel MRTs and repeated snapshots are not chains.
Utility-only stages need no invented Shader algorithm tables.
Keep whole-frame analysis read-only; do not switch to Shader organization to satisfy a gate.
The outline is not semantic analysis. Explain techniques and contributions,
not draw batches. Resolve core purposes during construction; range coverage
does not establish understanding. `get_frame_render_flow(section="candidates")`
keeps the source grouping available after drafts. Continue independent analysis
while recovering a save/display failure. `quality.analysisComplete=false` remains
unfinished; preserve real evidence limits.

Actually view the important RT image content, not just its path or JSON receipt.
Cross-check visual clues with relevant mesh shape/layout, VS deformation,
culling/depth/stencil/blend state, Shader mathematics and resource versions.
Asset names are clues, not proof. Choose the evidence needed for the question.
Read [visual evidence](references/visual-evidence.md) for images, exposure and comparisons.
Keep important MRTs together, fold secondary textures and show channels in a popup.

Prefer existing source/disassembly and matching observations. When an authorized
investigation needs temporary compilation, a discriminating experiment or readable
source navigation, prepare only the necessary managed source using the Shader
workbench guidance. Preserve existing source and restore temporary changes. Do
not create or organize workspaces merely to satisfy an abstract tool checklist.

For subtle composition changes, prefer a draggable before/after comparison with
matched display mapping. Other comparisons and animations are optional.
Declare channelGroups by purpose with short names and explanations below;
publication pre-generates and persists them.
Name stages by function and textures by their meaning, never by
EID/ResourceId. Use evidenced technical terms with plain-language explanations.
Captions explain the texture's use and meaningful channel groups
(often RGB plus A), without repeating format/size or evidence disclaimers.
Resolve unclear channels through actual consumers; reuse their investigation.
RGB previews stay opaque, normally at 0–1; remap only unreadable views. Verify the final scene
and UI composition rather than showing an isolated UI layer as the final frame.

Use `inspect_event` for one EID. Check persistence separately from window display.
Pass labels remain AIProvisional with publish_to_ui=false; the semantic pass panel is retired.

Separate facts, inferred purpose and unresolved meaning. Bound outputs are
candidates, not an `observed-contributor`. Raw device depth is not linear depth;
equal PNGs do not prove equal resource bytes. Slots, colors or names alone
do not establish GBuffer channel meaning.

Review the displayed rendering explanation, not only coverage and delivery.
Use render-path-review.md to investigate omissions, revise and review again;
reuse evidence in `record_task_review`. Keep limits in the review, not every card.
A useful partial result states material gaps once; never claim unfinished work complete.
