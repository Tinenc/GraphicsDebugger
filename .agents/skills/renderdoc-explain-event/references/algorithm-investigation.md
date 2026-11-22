# Explain the algorithm and its place in the frame

Read for a frame overview or multi-draw pass: explain an evidenced input -> calculation
-> output -> integration chain, not a binding/code dump or fixed technique checklist.

## Investigate backward, explain forward

Use the existing frame outline and observations. Choose a useful entry point
from the requested result, range or event, or the actual presented scene for a
whole-frame question. Trace important inputs backward to their producers and
separate outputs forward to their actual consumers as needed. Tell the story in
rendering order. This is a way to find missing dependencies, not a compulsory
tool sequence or permission to reorder the captured frame. EID values identify
observations in this capture; historical EIDs are not reusable algorithm targets.

A navigation anchor or the final image cannot explain all earlier work by
itself. Investigate the operations and resource handoffs needed for the user's
question, including earlier important results inside a broad stage. Do not
copy one draw's function onto another merely because both write the same target.

Actually view the relevant input/intermediate/result images as described in
[visual-evidence.md](visual-evidence.md). Cross-check visible clues with the
evidence that can distinguish plausible functions: mesh shape and input layout,
VS deformation or projection, culling, depth/stencil tests, blending/write masks,
Shader mathematics and versioned resource handoffs. Inspect only relevant
dimensions; a compute filter does not require an irrelevant mesh inspection.
Compare pre/post-VS geometry when deformation or coverage matters. Similar
silhouettes, triangle counts, texture colors and asset names cannot establish
the algorithm alone. Available unpacked material/texture names can narrow the
search, but captured calculations and actual uses decide this permutation's role.
An independent layer raises questions about what it contains and how it returns
to the scene, not a preset object/effect label. Keep observations separate from
their interpretation, and resolve contradictions while constructing the module.

Start from a key output version, inspect the producing Shader's actual reads,
then resolve each important input at its consumer EID. A resource version means
its contents at that execution point, not just its ResourceId: retain the view,
mip, layer/slice, sample and relevant channel or region where available. Prefer
`inspect_frame_resource_usage(flow_id, resource_id, anchor_event_id, direction="before", order="nearest")`
to examine possible producers nearest to the consumer first, paging backward
in execution order, not by numeric EID distance. The nearest candidate is not
necessarily the correct producer; verify its actual writes and retained coverage.
Use direction="after" at a producing EID for possible consumers, nearest first
in forward execution order. The anchor is excluded; order="nearest" is not
supported with direction="all". Use offset/limit (at most 200) and follow hasMore/nextOffset
when more relevant candidates remain. complete separately reports whether the
source and outline were complete: the final page does not prove complete
source coverage. The same capture/session/flow/resource query is cached across
anchors and directions; refresh=True is only for stale observations.
`find_draws_by_resource` remains the lower-level source of these coarse usage
candidates. Combine the usage records with the frame's execution order and
`inspect_frame_render_stage` observations to identify useful writers and readers.
Resource usage is not channel-level or causal proof. Binding is not sampling:
verify the Shader sample/load and how that value reaches the relevant result.
Check branch conditions and dynamic indexing where they matter; an available
code path alone does not prove it executed for every pixel. Verify output stores,
depth/stencil rejection, blending, channel write masks and spatial coverage.
Inspect copies/resolves and their source/destination rather than assuming a
resource keeps the same ID through the whole calculation.
`get_event_resource_graph(event_id, depth=1)` helps when the resource has not
yet been identified; its ordered before/after candidates are not causal edges.
If a consumer only filters or repacks a channel, follow the transformed result
to its meaningful use instead of stopping at the intermediate encoding.

A reused ResourceId can contain different results at different EIDs, mips or
slices. The nearest prior writer is not automatically the complete source:
it may affect another subresource or region, perform no work, or preserve earlier
data through additive blending, partial writes or atlas updates. Identify the
effective writes before this consumer and whether their coverage replaces or
retains the needed contents. Follow copy/resolve sources and load/clear/discard
boundaries where relevant; do not erase a dependency merely because the same
resource was bound for writing later. If coverage is unavailable, record that
limit rather than claiming a full replacement or exact pixel contribution.

Then follow each meaningful result forward to a verified Shader read, copy,
blend or other recorded use before its relevant contents are overwritten.
Confirm that the consumed version contributes to that downstream result;
resource reuse, chronological adjacency and matching labels are not dependency
proof. A downstream blend may consume destination contents without an SRV read.
Do not treat an unrelated later overwrite as a consumer of the earlier result.
For an imported or temporal-history input with no producer in this captured frame,
identify that boundary from evidence. Explain its observed use without inventing
the preceding frame's generating passes. A useful terminal result can end the
chain at its verified role; missing current-frame consumers do not justify an
invented composition or an unsupported claim about next-frame use.

## Complete each module while building it

Use the following questions where they clarify a module's role and resource
handoffs. They guide the internal evidence record, not a fixed investigation
sequence or a requirement for four separate draws/RTs. They are not node labels
or a UI form:

1. **Inputs:** What data does it use, and where does that data come from?
   Distinguish material properties, current scene/depth, light data, history
   and independently computed illumination where the Shader does. A named
   texture or one bound SRV alone does not establish the origin or purpose.
2. **Calculation:** What are the key steps that transform those inputs?
   Read the relevant Shader code, disassembly or existing readable source.
   Record actual sample/decode, coordinate reconstruction, visibility tests,
   lighting/filter equations and output stores needed to explain the result.
   A Shader entry-point name or resource list is not algorithm evidence.
3. **Output:** What useful information is written, in which resource and
   meaningful channel groups? Separate a contribution buffer or mask from
   scene color. Check write masks and subresource; merely binding an output
   does not prove pixel contribution.
4. **Integration:** Where and how is this output used next? Establish the
   consuming EID, its actual input binding, its Shader operation or fixed
   blend/resolve behavior, and the destination. Explain how an independent
   contribution returns to the scene. For an in-place scene update, the
   current draw may already be the integration step; do not invent a second
   composition pass. Buffer preparation can end at its verified geometric,
   lighting or dispatch consumer rather than a fictitious color composite.

Record these answers and their exact evidence as the module is investigated.
Only then select its explanatory images and write the concise card. Keep a
small unresolved-boundary list during construction: which input producer,
calculation or consumer is still missing, and the next discriminating
observation. Resolve feasible entries before starting the final review;
review is a consistency check, not the first attempt to understand the code.

Read only the relevant Shader regions, using `inspect_shader` and an existing
representation first. If the preview is truncated, use
`get_current_shader_representation(text_mode="preferred")` for its inline source
or portable IR and continuation. Use `read_evidence_file(text_query="...")`
to locate relevant sample/load/store instructions or names, then read those
regions using the returned offsets; follow search continuation for more matches.
This also searches retained proxy JSON without repeatedly paging whole files.
Preserve an EID, stage, original Shader identity and source location
or instruction range with each decisive observation. No source reorganization,
editable Shader workspace, compilation or replay replacement belongs in this
frame-diagram investigation. Readable HLSL that compiles is not required: source
or disassembly plus binding/state evidence can establish the computation.
A failed explanation read is not a reason to start the Shader workbench workflow;
retain the exact read failure and use other available read-only evidence.

Use configured unpacked assets when they help resolve material semantics or
an association. Reuse the active asset index and verified matches through
`renderdoc-unpacked-asset-intelligence`. Asset names narrow the investigation;
captured calculations establish what this permutation actually computes.

Adapt questions to the observed operation: distinguish evaluating a contribution
from accumulating it, and image composition from geometry/visibility/index use.
Explain reconstruction, filtering or temporal reuse only where observed.
Do not assume Lumen, VSM, ray tracing, reprojection or denoising exists because
the engine commonly uses it. A recognized technique needs actual evidence;
retain inferred/partial status for supported but incomplete interpretations.
Do not silently replace a missing calculation with a plausible generic recipe.

## Present the useful chain, keep the evidence compact

The stage title states the function. Its short plain-language summary explains
the purpose and effect, without Inputs/Calculation/Outputs labels or a
calculation-details section. stage.algorithm remains internal verification data.
The main stage sequence describes execution order; a module's processGraph
describes data dependencies, not merely the order of calls. Use one when actual
cross-draw or cross-target handoffs need visual explanation. Repeated same-target
draws alone do not require one. Derive its branches and
merges from the actual producer/consumer operations; do not map one draw, one
attachment or one internal evidence row mechanically to one graph node. Introduce a technical
name with its role, for example that reprojection looks up a previous result
using current motion/coordinates, only if those operations were observed.
Keep exact Shader anchors, resource IDs and limitations in evidence/review.
Useful detail explains a choice or data transformation; repeated IDs, formats,
unknown-wording or long source listings do not improve the overview.

For an image-producing effect, show the relevant portion of its input ->
intermediate result(s) -> downstream use, with `processGraph` nodes for actual operations and edges
for their established relationships. Each displayed node references a representative
image, comparison or animation; combine calculation and result instead of adding
text-only nodes. Omit a subgraph that adds no useful visual explanation. Neither a final
slider nor a GIF is required. A subtle composition change benefits from a matched
before/after divider. Use the consumer's final result, not another uncomposited effect
texture with a nicer appearance. A useful terminal result does not need an
invented scene blend; its verified role determines where the explanation ends.
Multiple internal computations may share one illustration, not a chain of
identical snapshots relabelled as successive results. Arithmetic inside one
draw does not create intermediate render targets; simultaneous MRTs are parallel
outputs. Ordinary stages show independent useful results. Do not fabricate a
texture or graph edge for a register-only calculation.
Where integration lies in a later stage, retain that stage's EID and result
and connect the explanation across stages; do not move the consumer image
outside its true range to satisfy a visual sequence.

A before/after divider can demonstrate the composition when the relationship,
dimensions and shared display mapping are verified. An animation can show
actual blur levels or accumulation, not invented intermediate results. A
buffer-only algorithm needs a clear transformation and downstream use, not an
irrelevant texture or a warning that it has no 2D image. Final-image claims
still require the exact Present target and its captured receipt.

## Publish the recorded algorithm

Populate stage.algorithm for meaningful calculations/resource chains while investigating,
then submit it through
`publish_frame_render_flow`. It contains four lists, each with at most 32 key
entries. Select the important computation and resources, not every temporary
variable, bound texture or redundant draw.

| List | Fields | Meaning |
| --- | --- | --- |
| inputs | eventId, resourceId, role, optional producerEventId and evidence | The resource read by this local event and its established origin. |
| steps | eventId, title, calculation, evidence | A key operation, described in plain language with the relevant Shader source/instruction anchor or actual copy/blend state. |
| outputs | eventId, resourceId, role | A key result produced by a local step, including an earlier important draw inside the stage. |
| integration | sourceEventId, resourceId, mode, eventId, explanation, evidence, optional targetResourceId | Where that declared output is used and how it enters the next result. |

Input, step and output eventId values belong to this stage. Steps follow actual
execution order; several steps may describe one EID. producerEventId, when
supplied, is an earlier effective action that writes the input. It can belong
to another stage. Otherwise evidence must explain the established external,
history or other source boundary; do not invent a current-frame producer.
Navigation anchors and analysis anchors are separate. A broad stage can keep
one final-EID title link while recording each important generating and composing
EID in steps/outputs. Do not inspect only the final draw and let a major earlier
lighting or effect computation disappear inside that range. The Host retains
the referenced input/output/Shader observations across restarts so these
cross-stage dependencies remain verifiable.

Undecoded indirect arguments leave workload unknown; they are not proof of zero
work or of pixel contribution. Keep that EID and its observed bindings in the
investigation, and qualify execution/contribution separately from the understood
Shader calculation. Do not invent dispatch dimensions, delete a possible writer,
rename it as a different operation or create a Shader workspace to pass validation.
Use available read-only argument/state evidence to resolve a material uncertainty;
call `inspect_event_api_parameters(event_id)` for the exact child and its recorded
parent command. This returns typed API chunks, argument/count-buffer IDs and byte
offsets, command-signature metadata, and native decoded indirect arguments when
available. Refresh `inspect_frame_render_stage` to retain its resolved workload in
the flow evidence. A decoded zero-instance draw does not write a resource even
when output bindings are retained. Exclude proven zero work from contribution
steps; do not exclude an event whose arguments remain unresolved. If this route
is unavailable, preserve the precise boundary without repeating failed routes.
An unavailable count alone does not make a supported algorithm explanation false.

Each key output needs an integration entry matching its sourceEventId and
resourceId. The integration eventId can be in a later stage:

- mode=downstream identifies the actual consumer and needs targetResourceId
  for a completed explanation. Inspect that EID's matching input and output,
  then explain its sampling, filtering, weighting or composition. The target
  field is optional syntactically so a partial draft can survive, not so the
  Agent can skip finding the result.
- mode=in-place uses the producing EID itself for a result already written
  into the scene or other active destination. Evidence must establish that
  role; do not label an isolated effect RT in-place to avoid finding its consumer.
- mode=present requires the exact Present EID and its actual presented
  resource. If an earlier copy changes ResourceId, describe that handoff
  first; a different source RT cannot be passed off as the Present target.
- mode=non-image explains useful buffer output and its verified use. Supply
  the consumer EID when applicable; a same-event endpoint needs evidence for
  that actual terminal role rather than a made-up image composite.

Inspect the referenced producer/consumer EIDs with `inspect_frame_render_stage`
as needed so the Host can check the bindings. Steps need real calculation
evidence, not just "Shader inspected". The Host checks event order, resource
availability and missing chain segments; those structural checks do not prove
the Agent's mathematical interpretation or actual pixel contribution.
Maintain the corresponding confidence and investigation status honestly.
An older diagram can reuse its valid media and observations while the missing
algorithm entries are investigated and added; no full re-export is required.

## Reuse evidence and close gaps deliberately

Use verified saved bindings, source/disassembly and PNG receipts before replay.
Check their capture, Shader identity, resource version and relevant mutation
scope. Replay for needed missing pixels, dynamic bindings or execution facts.
Static evidence cannot establish every current value,
executed branch, pixel contribution or post-edit result. Record reused evidence
as reused, not as validation performed in this run.

An empty or failed pipeline read is not a new valid binding inventory. In
particular, a nonzero draw/dispatch with no Shader, no bindings and a zero
viewport can indicate unavailable state rather than an empty rendering stage.
Keep established observations separate; do not overwrite them with empty lists
or infer that their resources disappeared. Preserve evidence-backed cross-EID
relationships when fresh inspection fails. Remove a relationship only when
evidence shows it is false or obsolete, not to pass a gate or hide a failed
reanalysis. Keep any required fresh verification explicitly unfinished.

Recover autonomously within the task: use an available saved source, alternate
read-only representation, recorded resource consumer or API observation that
answers the missing question. Inspect the concrete diagnostic first. If several
routes share the same unavailable pipeline, do not repeat them or fan out more
reads and channel captures against that unchanged state. A retry needs a changed
condition or a distinct evidence route. Use `get_replay_health` when diagnosing
that state, not as a routine prerequisite. Only confirmed native fatal failure
permits `recover_replay`: one same-file reopen, retaining drafts and saved diagrams.
It returns a new session with capture-original content, not prior Shader replacements
or CBuffer edits. Read current context and prepare a new flow; old bindings/media
are historical, not current validation. Recheck any replacement needed to match
the previous picture rather than claiming the recovered picture is identical.
Continue independent static work; state any remaining dynamic gap and next action.

Cache resource usage by capture/session and ResourceId; cache resolved dependencies
by consumer EID, subresource/view and the established writer interval or history
boundary. Reuse shared dependencies once across modules. Invalidate affected
conclusions when capture, replay mutations, source or relevant bindings change.
Cache Shader calculations by original Shader hash,
relevant bindings/permutation and the question answered. Reuse the same
algorithm for repeated draws only after checking their important bindings
and state; retain the exact EIDs for each occurrence.

Track visited resource/EID/subresource/question tuples and an open frontier.
For a missing light source, inspect the useful producer; for a missing
composition, inspect the consuming draw; for a packed channel, inspect its
decode/use. Expand only that unresolved boundary and stop that branch once
the requested role and integration are supported. Do not follow every resource
user, rescan the whole frame for each card or retry identical unavailable
evidence indefinitely. A concrete capture boundary, unsupported observation
or genuinely ambiguous calculation belongs in the partial review with the
specific missing link and next useful action.

An evidenced stage containing only utility operations may use a concise
functional explanation instead of invented Shader algorithm tables. Determine
this from actual captured actions, never a category label. Any algorithm
relationships supplied are still checked against their recorded bindings.

Completeness requires both range coverage and an explained algorithm chain
for the requested core modules. Do not call the work complete merely because
every card has a title, a texture and investigation.status=investigated. Check
that important input sources, actual calculations and integration destinations
are present in the published content and backed by the recorded observations.
A broad effect name, isolated output or generic "computes lighting" sentence
is still missing the explanation, even when no unknown wording remains.
