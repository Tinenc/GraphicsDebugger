# Interactive frame rendering flow

Use this workflow when the user wants to understand a whole frame as an
illustrated, clickable sequence inside Bread. It is a rendering overview, not
a Shader function-call graph or the retired semantic-pass panel.

## Collect once, explain meaningful stages

`prepare_frame_render_flow(refresh=False)` returns a reusable capture-bound
action outline, not a semantic analysis. Read candidate groups with
`get_frame_render_flow(flow_id, offset, limit=200, section="candidates")` and raw
actions with section="actions". Follow hasMore/nextOffset and inspect
source.complete/omittedActionCount before choosing the overview's ranges.
section="candidates" retains the source grouping after drafts and restores;
section="stages" then returns the authored overview.
Reuse the flow for edits. New analysis uses `prepare_frame_render_flow(reanalyze=True)`
and [reanalysis.md](reanalysis.md) to reassess the requested scope while keeping
valid evidence. An outline refresh alone does not reanalyze algorithms.
Editorial-only changes need no repeated analysis.
Reusing a verified static source with its provenance is valid; merely changing
layout, deleting graphs or republishing old conclusions is not reanalysis.
Use `get_frame_render_flow(section="proposals")` for editable publication data
and section="quality" for current gaps. Do not submit expanded readback objects:
they contain derived stage/image/comparison fields outside the proposal schema.
On schema errors, repair the proposals payload, not an unrelated historical failure.
Recoverable reads should switch to available saved evidence or another read-only
route. Failed reanalysis preserves valid prior bindings and dependency graphs;
do not delete relationships to satisfy a gate. See algorithm-investigation.md
for bounded recovery and the limits of static evidence.

Before drafting cards, read [algorithm-investigation.md](algorithm-investigation.md).
Trace important results backward through their actual producers and consumers;
explain verified modules in execution order, including each effect's composition.
Use [module-discovery.md](module-discovery.md) when old or broad ranges conceal
different local contributions. Reconsider the grouping itself, not only known
validation gaps; equivalent repeated draws may reuse a verified explanation.

Derive modules from this capture's execution order and resource transformations.
Select investigation EIDs for their role in a requested result, a change in
bindings/state or an unresolved resource handoff. Never treat historical EIDs,
a named game's pipeline or a fixed module list as required investigation targets.
Named techniques such as VSM, Lumen or hardware ray tracing are possibilities
only when the evidence points there. Atlases, indirect work, engine names or
missing markers alone cannot prove presence or absence.

Choose consecutive stage ranges with a coherent purpose, and title them by
that purpose rather than "first depth batch", "second depth batch" or "later
computes". Keep separate occurrences across intervening work; explain their
different roles or reference their shared technique in the summary. Preserve
chronology without reducing the overview to an API-operation ledger.

Investigate ambiguous groups during construction with
`inspect_frame_render_stage(flow_id, event_id)`: retain bindings, Shader identity
and relevant geometry/render state. Cross-check RT appearance with mesh shape,
input layout, VS deformation, culling/depth/stencil/blend state and actual Shader
calculations where they distinguish the purpose. Names only guide this inquiry.
Use existing source/disassembly or
`get_current_shader_representation` and targeted `read_evidence_file` reads.
Do not create editable workspaces, reorganize, compile or replace Shaders for
this explanation; readable HLSL compilation is not a prerequisite.

Follow [algorithm-investigation.md](algorithm-investigation.md) through critical
producer/consumer steps inside each range, not just its final EID. Reuse verified
observations and investigate missing dependencies; retain real limits in review.

Keep clear/copy/resolve/compute work when it explains a resource transition or
the final image. API bookkeeping and marker containers can remain in the
underlying outline without separate cards. Review every materially different
contribution in a whole-frame request. Use coverage.uncoveredRanges for omissions
and candidates for changes hidden inside broad ranges. Include unread/truncated
scope in review; a representative subset is not an exhaustive analysis.

## Card content and image evidence

Use a concise vertical spine of chronological functional modules, with each
module's related key images arranged horizontally. Borrow the layout and clarity,
not another game's algorithms or pass order. Determine the number and names of
modules from their actual roles in this frame, not from the reference image's
stages. Use the current application theme.

The default view should answer how the frame is built without an attachment
inventory wall. Select a small set of images at the effect's appearance, key
intermediate transformation and actual downstream combination, as useful for
the question; not a fixed quota or required three-image template. Several important MRT properties
should remain together horizontally when comparing them explains the module.
Keep secondary views and auxiliary evidence available in foldouts. Completeness
of investigation does not require every image to be expanded at once, and
folding a core result does not excuse failing to investigate it.

Use short function titles stating what this range does. The application shows
the EID range below in smaller text. The summary explains in one or two
plain-language sentences the useful operation and why its result is needed.
Titles describe verified roles, not Shader names, IDs, formats or a fixed
vocabulary checklist. Use evidenced technical terms with plain-language explanations,
as described in module-discovery.md.
The node body is a short plain-language summary, not an input/calculation/output
form or a calculation-details panel. Keep stage.algorithm as internal verification
data. Use a horizontal processGraph when results flow between targets or a
transformation/merge needs visual explanation. Multiple draws alone do not
require a subgraph. Short labels and captions explain the actual operations.
Explain what changes and why it matters, beyond a generic category.
Mention particular computations only where evidenced. "Draws a large amount
of geometry" does not identify the useful role; determine it before naming it.
An absent 2D preview is not a stage purpose and needs no extra sentence on the
card. Buffer-only stages should simply explain their useful transformation.

Separate materially different post effects; merge repetitive draws of one
role while preserving chronology. The overview names supported technologies;
cards explain their contributions. Keep inferred purpose distinct from
captured fact; engine names and fixed classifications do not confirm it.

Texture titles name their contents, not an EID/RTV/ResourceId address.
The description briefly explains what is stored and who uses it;
channelMeaning explains the meaningful channel groups, such as RGB storing
surface color and A controlling coverage. Use the capture's actual encoding,
not these example meanings. EID, slot, size and format already have subdued
metadata; do not repeat them in the description.

Investigate deeply and present simply. Keep supporting tests, contradictory
evidence, confidence limits and technical derivations in the investigation;
the card tells the useful result in ordinary language, not the investigation diary.
Keep card text short and useful. Omit phrases such as "physical-attachment",
"preserves physical RGBA", "no 2D texture preview", "not byte-difference
evidence", "exact business meaning not confirmed", and lists of investigation
limitations. Store those observations in receipts/investigation/review instead.
This is an editorial separation, not permission to conceal an unresolved
function: continue the feasible investigation, retain partial status for a real
gap and do not mark the diagram complete by merely deleting unknown wording.

Main arrows express execution order; resource dependencies need binding evidence,
not merely adjacent EIDs. Only stage-title and texture-name links navigate.

The Host navigates to the stage's last effective graphics draw, or last nonzero
dispatch for compute-only work. It uses decoded workload fields, not action
names; unknown indirect dimensions are not proof of zero work. Do not let stale
RT bindings on a trailing dispatch replace actual graphics results. Include an
earlier substage's useful MRT results even if the final draw has one target.

Review bound RTV/DSV outputs, write masks and resource use, then capture relevant
views with `capture_event_texture_evidence`, retaining EID, attachment,
subresource and manifest.path. Explain useful MRT outputs together; inherited
unused bindings need no unrelated thumbnails. An irrelevant, unwritten or
already-explained attachment may use outputOmissions: resourceId, kind, index,
optional eventId/stage/mip/slice/sample, specific reason and evidence. Include
eventId to disambiguate multiple observed events. The Host matches the binding
and keeps the omission in review; never omit an unanalyzed core output.
For compute or ray work, use the actual UAV outputs and SRV inputs returned by
the stage observation; ignore stale graphics RT bindings. Texture evidence
supports `attachment_kind=uav` or `attachment_kind=srv` with the observed
Shader stage and attachment_index from that returned binding list. A register
number is not necessarily its list index. Do not fabricate RTVs for a UAV-only
operation or use a buffer resource as a texture preview.

Show combined output previews in group outputs, always with opaque RGB display;
captured alpha must not make a valid color image invisible. Put meaningful
channel views in group channels for the horizontal channel popup and relevant input textures
in group inputs. Use image.presentation=primary for the few main explanatory
views and image.presentation=detail for secondary textures or supporting views
that belong in the more-textures foldout; omitted presentation defaults to primary
for compatibility. Do not relabel auxiliary pictures as channels to hide them.
Explicit processGraph, processSteps, comparisons and animations determine where their referenced
images appear and take precedence over presentation; do not duplicate those
images as standalone thumbnails. Do not automatically export A as a main preview. A channel
with meaningful mask data may be explained separately as an opaque grayscale
view when it is needed for the user's question; it never acts as UI opacity.
Give each attachment and informative channel a short purpose, backed by Shader
writes and consumers. Split by properties, not automatically by R/G/B/A: a
normal or color in RGB stays a combined view, while an independent mask in A
gets one grayscale view. Packed unrelated scalar properties may need separate
channels. The opaque RGB overview can itself be the color/normal group; do
not export a redundant second RGB image merely to fill the popup.
Attach each channel image
to its combined preview with parentImageIndex (zero-based within that stage's
images). The default association uses the exact EID, ResourceId and
mip/slice/sample; do not group unrelated RTs merely by their slot number.
Declare image.channelGroups as objects with channel and optional label/description.
Keep label a short property name supported by the Shader. Put its explanation
in description, shown below in smaller text; do not use a paragraph as the title
or repeat the same sentence in both fields.
Use rgb plus a for a color/vector and independent mask, or r/g/b/a for unrelated
packed scalar properties. Explain their actual uses; do not infer a packing plan
from the format or an image title. Explicit [] means no detail grouping is useful.
Missing groups are completed with real physical channels; one A record alone is
not a complete RGBA set. A format without A cannot acquire synthetic Alpha.
Publication generates these groups before opening the diagram and saves them
with the capture bundle, so opening a popup does not initiate the normal export.
Check channel preparation failures and retry only the missing work, preserving
valid existing images. A truly single-channel texture needs no channel button.
The popup shows semantic groups horizontally, not an unconditional four-way split.
A physical channel label with an unresolved important meaning
is a cue for targeted Shader investigation, not a finished semantic caption.
Separate channels help explain packed data; they are not a reason to replay
every draw or export uninformative constant channels.

Actually inspect images following [visual-evidence.md](visual-evidence.md), which
covers exposure, atlas visibility, depth/HDR, budgets and alpha. Reuse matching
receipts for editorial changes. Labels match the observed data and mapping;
do not crop away evidence to fill a card.

A before/after slider needs a published comparisons entry and an established
input/output relationship, even between different resources. Use matching
dimensions/channels and one shared explicit range to avoid false exposure
changes. Explain the effect between these EID states; do not imply separate
game frames without temporal evidence. Reuse existing receipts.
Prefer this draggable divider when a composition changes the image only subtly:
side-by-side thumbnails can hide the effect. Do not add a comparison to unrelated
textures, incompatible views or every card by default.

Use animation when a sequence explains more than a pair. Reuse verified,
same-sized previews in chronological order with a stable display mapping.
Add animations as {label, description, imageIndices, frameDurationMs}; indices
refer to 2–16 entries in this stage's images, frameDurationMs is 100–5000 and
there are at most 8 animations per stage. Keep core outputs in group outputs
even when referenced by an animation or comparison: those references already
prevent duplicate standalone thumbnails. Use group=animation-frames only for
extra transitional views, not to remove a required output from coverage.
Publication produces the GIF from those PNGs; do not invent intermediate states
or inject paths. Draw progression is not game motion without temporal evidence.

## Explain an effect through its relevant images

Choose illustrations after understanding the computation. Select the actual
transformations and downstream result that explain this module; do not assume
a familiar effect recipe or show every still-bound texture. A reference image
teaches expression, not required effects, stage counts, EIDs or algorithms.

The main stage sequence shows execution order; a processGraph shows actual data
dependencies across draws/targets. Link only established versioned resource
handoffs: split, merge or sequential transformation. MRT attachments produced together are parallel
results, not steps. Single-draw arithmetic and one snapshot repeated under several
labels are not a chain. Ordinary stages show independent useful results.
Use the fewest nodes that explain a real handoff.

Use stage.processGraph for the module's actual operation and resource paths.
Its nodes (2–16) have a unique id, a short label, optional description/eventId,
and at most one imageIndex, comparisonIndex or animationIndex. In newly authored
graphs, give each displayed node a representative texture image, comparison or
animation. Combine a calculation and its result into one node; do not create a
pure-text graph. Without useful images or relationships, use the concise stage
summary and relevant previews instead of forcing a subgraph.
Its edges (1–32) connect from/to node IDs in an acyclic directed graph;
express an actual transformation, contribution or composition relationship.
Do not replace an understood multi-draw algorithm with unrelated thumbnails,
an arbitrary fixed template, or a required final slider/GIF. Choose branches,
merges and intermediate steps from this capture's evidence. A relationship
must have actual Shader-read, copy or blend evidence, not just a shared binding.
The simpler legacy processSteps sequence remains usable for a truly linear
chain: 2–12 labelled entries, each referencing exactly one image, comparison
or animation. Media indices always refer to this stage's published entries;
UI support alone does not deliver a comparison or animation.

Before naming an image "Final frame", verify presentation. Read the tail of
`get_frame_render_flow(section="actions")`, identify the Present output and
trace its final copy/resolve or render-target writer. Export the exact Present
EID with `capture_event_texture_evidence(attachment_kind="present",
attachment_index=0, channel="rgba")`; the tool resolves the action's
copyDestination, not inherited RTV0. This is the final-image receipt required
for a stage containing a Present target. Use native or explicit 0–1 mapping
for an ordinary display image; preserve the actual supported HDR mapping
where applicable rather than auto-exposing the final frame into a new look.
Inspect the UI pass's
inputs, blend state and target: an isolated transparent UI texture is not the
scene with UI. Follow its consumer through composition to the presented target.
Where helpful, show scene before UI, the UI layer and the composed frame as
distinct steps. If a capture ends before presentation, use the actual last
completed scene result and retain that scope in the review, without claiming
an absent presented frame. Do not assume the last RT0 or currently selected
texture is the final display output.

## Publish an interactive result

Call `validate_frame_render_flow(flow_id, stages, title, summary)` with the
prepared flow ID and the explained stages. Read all returned issue paths.
Repair only the affected fields with the returned `draft_id` and
`stage_updates` keyed by zero-based index. A valid receipt checks the cached
outline and output bindings. Use `publish_frame_render_flow(flow_id,
draft_id=..., publication_scope="frame", show_in_window=True)` to run full media validation and save the
result. Never treat a preflight receipt as publication. If publication
reports a media problem, edit the affected stage and use the new revision.
When a repeated validation error cannot be resolved from available evidence,
report the exact unresolved rule and keep the existing published work.
Each stage supplies exact startEventId/endEventId, title, summary, category,
techniques, investigation, confidence and useful evidence images. Add algorithm
for meaningful calculations/resource chains, with inputs, steps, outputs and integration as specified in
[algorithm-investigation.md](algorithm-investigation.md). Include key earlier
outputs and their computations inside a stage, not just the final draw's RT.
Stage ranges can stay broad, but include the producing/composing events needed
to explain their actual function. Repeated equivalent draws need not become
separate presentation nodes. Navigation anchors and analysis anchors
are separate: the title link goes to the last effective draw, while algorithm
steps and outputs identify the events that actually explain the module.
Output coverage uses those declared key output events and aggregates their
observed attachments. Declare the core results while investigating; do not
pick convenient output events to hide another important calculation or override
the Host's navigation target with an arbitrary early event.
An auxiliary clear/copy stage can give its evidenced purpose and handoff without
four algorithm tables. Declared algorithms still need accurate bindings and
explicit unresolved boundaries.
Category summarizes function. Record technique status/evidence and investigation
status, unresolvedReason and nextAction from work actually performed. Confidence
is captured for directly observed facts, inferred for interpreted purpose, or
unknown for unresolved meaning; marker names do not confirm an entire explanation.
Each image supplies manifestPath from the capture tool's manifest.path, plus
title and description; use group, label, semantic, channelMeaning, evidence
and parentImageIndex to organize the output/channel/input explanation. Set
presentation to primary or detail to keep the default overview concise without
discarding its supporting pictures.
Comparisons reference this
stage's image indices through leftImageIndex/rightImageIndex, label,
description and relationship; the Host checks whether a claimed before/after
pair is comparable. Use processGraph to connect the published images/comparisons/
animations and outputOmissions to record investigated irrelevant attachments.
Use this flow's ranges and EIDs, compatible receipts and generated links;
never supply another capture's images, custom HTML or scripts.

The diagram and media restore automatically with the same RDC. Check
savedWithCapture and persistence, not only documentPath. Reopening needs no AI
run or recapture. Repair persistence failures without discarding the document;
another capture requires matching evidence and bindings.

Publication retains useful drafts. coverage.complete means action-range
coverage; quality.analysisComplete means recorded investigation/output checks
passed. Neither proves the interpretation correct. Repair quality.gaps in a
focused batch, using existing observations, and republish. The main task review
cannot record complete while the latest quality.analysisComplete is false.
Missing explanations and unresolved core purposes are remaining work just as
missing images are; a concrete unavailable observation belongs in the review
instead of an endless retry loop.

Finish with window.published=true, quality.analysisComplete=true and supported
requested content. Repair window/persistence errors without redoing valid work.
Inspect the actual diagram for useful captions, readable images, correct groups
and module relationships. Verify requested media/processGraph are published,
not merely planned. Each independent result needs its actual downstream use;
an attractive texture alone does not explain its contribution.
Conclude briefly with the frame's main functions, window availability and any
remaining scope limits. Reuse the publication receipt for task review.
