# Discover the frame's functional modules

Use this reference when a broad range may hide different rendering purposes,
or when a new analysis has only refreshed old bindings/captions. It is not a
technique checklist or a required tool sequence. Choose the next observation
that distinguishes the plausible explanations for this capture.
After switching/reopening an RDC, prepare its returned current flow identity;
do not carry another capture's flow ID, evidence or saved-state receipt forward.

## Form a provisional explanation before grouping

First work out how this frame represents surfaces, where material and lighting
are evaluated, which subjects take different paths, and how their contributions
reach the presented image. Treat this as a working explanation, not a fixed plan:
test it against actual Shader calculations, geometry, state and resource changes,
and revise it when observations disagree. Different subjects can use forward,
deferred or other evidenced arrangements within one frame. Do not force a binary
classification or inherit a pipeline from the game's engine name.

Use rendering knowledge to ask useful questions, not to populate a technology
checklist. A visibility or surface-data representation suggests tracing its
resolve; a separately lit subject suggests finding its geometry and composition;
an unexplained lighting input suggests finding its producer. Follow comparable
clues for specialized geometry, effects and image transformations when present.
Importance follows the contribution to understanding this frame, not the number
of draws, screen area or a familiar technique name. A small local branch can
deserve an explanation while many repeated draws share one.

## Reconsider boundaries, not just old failures

Treat a saved diagram as a set of hypotheses with reusable evidence. Review
important conclusions throughout the requested frame, including previously
accepted ranges, not only the last publication's gap list. Compare candidate
draw groups inside each broad range: Shader identity and operation, geometry,
render-target transitions, depth/stencil/cull/blend changes, and the visible
contribution. One range's final EID cannot stand in for all earlier draws.
Read the source candidates across the requested scope before settling on the
overview. After a draft is published, section="candidates" still returns them;
section="stages" instead returns the draft's authored ranges. Use actual returned
EIDs for evidence calls, not interpolated numbers inside an EID range.

Select representative draws at meaningful changes and investigate their
differences. Draws sharing one purpose and relevant state can share analysis;
cache that result and check only the differing inputs or state. Expand a group
when contradictory observations reveal another role. Do not inspect every
draw with every tool, or split every Shader permutation into a presentation node.

Separate a localized contribution from the surrounding scene work when its
geometry, computation or integration is materially different. The fact that
both eventually write scene color is not a reason to merge their meanings.
Conversely, multiple targets or draws can belong to one coherent operation.
Keep those resource transformations together and explain their actual handoffs.
Do not split or merge to reach a stage count, imitate a reference diagram, or
force the user's example effects to appear.

Record a compact conclusion per investigated group: what changed, the evidence
that distinguishes its purpose, which draws share the result, and its important
resource dependencies. Readable source/IR, image content and state observations
can be reused with their identities; a newly refreshed binding inventory alone
is not a newly understood algorithm.
An image or a marker can suggest a role. Follow a materially different Shader,
geometry or state clue until the grouping can explain that contribution;
do not let one photographed draw stand for an entire heterogeneous range.
Use standard graphics terminology once supported, then explain it briefly.
Appearance labels such as "bright area" or "softened picture" describe clues,
not rendering functions. A Shader entry name likewise suggests what to check;
verify the relevant calculation, state and consumers before naming the technique.
For a packed channel, name the quantity consumed by the calculation, rather
than assuming its role from the channel letter or image appearance.

## Select complementary evidence for the actual doubt

- **Appearance and contribution:** view the actual input, intermediate or result
  image when it answers the question. For an accumulated target, compare a useful
  boundary before and after the candidate draws using matched views and display
  mapping. A small change can still be the important localized effect; inspect
  it with a crop or divider while retaining whole-frame context. An almost
  identical final thumbnail does not justify merging different computations.
- **Geometry and deformation:** `get_post_vs_data(event_id=...)` returns actual
  bounded positions/indices, while `capture_mesh_image(event_id=..., inline=True)`
  delivers their wireframe PNG. Inspect eventId, stage, instance/view, complete
  and limitations. The image is post-transform geometry, not a material render
  or proof that its fragments passed depth/stencil. Mesh counts, strides and
  an input-layout list are not a viewed mesh shape. Use relevant VS/geometry
  source or an applicable unpacked mesh when comparing deformation or object
  identity; the post-transform picture alone does not establish either.
  Select gs-out when final geometry/mesh output is needed and supported. Do
  not silently generalize one instance, one view or a truncated prefix to the
  entire draw. An unavailable position format leaves that observation open;
  it is not evidence of absent geometry.
- **Visibility and blending:** inspect the actual depth/stencil comparison,
  write masks, culling and blend equations where they distinguish the role.
  If whether a local pixel survived remains consequential, `pixel_history`
  at a selected coordinate and explicit cutoff EID can supply rejection and
  modification evidence where supported. This is a targeted replay query, not
  a whole-image requirement. One passing or rejected fragment does not prove
  the entire draw's visible contribution. A configured depth test is not its
  observed result; neither is a wireframe.
- **Calculation and data flow:** follow actual Shader reads, transforms and
  stores with the versioned producer/consumer route in algorithm-investigation.md.
  Intermediate target changes, encodings and independent layers deserve a
  visual chain when later work actually consumes them. Repeated arithmetic
  inside one draw and unrelated outputs do not create cross-target edges.
- **Temporal behavior:** a historical texture bound in this frame can show
  retained data and its current use, but it does not reveal every earlier
  frame's execution. Compare previous/current frames only when matching capture
  or history evidence exists, with frame identity and display mapping retained.
  Two EIDs from one capture are before/after states of that frame. Do not open
  arbitrary RDCs or require unavailable previous frames merely to complete an
  otherwise supported module explanation.

These evidence routes are alternatives and complements, not a per-node checklist.
Follow a contradiction to the smallest observation that can resolve it. Give
important local differences a clear purpose in the overview; keep repeated
instances and supporting technical detail out of the main cards.

## Use unpacked assets to resolve semantics

When a project has a configured unpacked-asset root and captured bindings leave
the material/effect ambiguous, use renderdoc-unpacked-asset-intelligence. Reuse
its current index and target-specific matches. Start from a few distinctive
bound textures or a mesh, then follow dependency/referrer links, material
instance parents, parameter overrides and Shader/function paths. Inspect the
associated material or Shader content that distinguishes the candidate uses.
Matching every texture is not required; shared default resources alone are
weak anchors. Select the reader for the actual engine and platform, not a
fixed extractor or an assumed destination engine.

Combine linked names, properties and functions with this captured permutation's
actual calculations and state. Material parameter names can clarify a role
even when individual CBuffer offsets are not matched. Keep original association,
inferred role and unresolved identity separate. Reuse materialSemanticContext
where valid and retain which asset evidence actually informed the description.
Do not start Shader organization, edit source, rename resources/CBuffer fields
or export a model merely to explain the frame. Unknown original names need not
block a computation understood from captured evidence.

## Review the claims that matter

Use [render-path-review.md](render-path-review.md) to review the actual shading
paths and visible explanation, then repair and re-review meaningful omissions.

Distinguish semantic investigation, structural validation and presentation
delivery. A complete action range or a passed Host check does not establish
that the stage's function, local exceptions or images were understood. Conversely,
an optional diagnostic not needed by the explanation is not a new requirement
to prove every GPU branch or pixel. Validate the important claims actually
made and the user-requested coverage.

If a Host check conflicts with exact API or resource evidence, retain both and
use the supported corrected evidence path; do not erase a real relationship,
invent a handoff or change its meaning solely to get a green status. A missing
core producer, calculation or integration still needs investigation. Continue
independent modules while a concrete boundary is unavailable; isolate that
limit rather than replacing the whole frame with vague ranges. A failed save or
display receipt concerns delivery; continue feasible source, dependency and
module analysis independently, preserving the evolving draft while resolving
that failure. Do not declare the entire task blocked while unrelated requested
analysis is still actionable. Keep the final
message short: what is available, what meaningfully changed and, if necessary,
the precise remaining limitation and practical next step. Detailed retries,
validation fields and evidence ledgers belong in the task log.
