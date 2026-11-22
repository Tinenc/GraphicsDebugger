# Event visual evidence

Read this reference only when an event question needs a texture view, channel,
depth display, GBuffer interpretation or a static evidence diagram. The Agent
chooses which evidence is useful. A visual is derived evidence, not proof of
the captured resource's original bytes.

## Look at the image, then verify its meaning

For important image-producing modules, actually inspect the relevant RT image
content before finalizing the interpretation and illustrations. A saved path,
JSON receipt, image dimensions, histogram or hash is not a viewed image.
`capture_event_texture_evidence` normally delivers its receipt plus an actual
image with inline=True. If the image is already present in the model's input,
look at it and reuse that observation; do not request it again just to satisfy
a tool-count ritual. inline=False deliberately requests only the receipt.
Prefer an applicable saved PNG to a new capture. New pixels or changed resource
contents require replay; a stale image cannot prove their current appearance.
Do not recapture channels against a known unavailable pipeline until the failing
condition changes; retain valid existing media and mark only missing views pending.

The capture receipt's modelImage.included reports that an MCP image block was
sent, not that the model received or inspected it. modelImage.sourceSha256
identifies the exported source; modelImage.sha256 identifies the delivered
preview, which may be resized. Check the actual image content and any provider
"not displayed" notice. A failed preview includes a reason instead of an image;
do not interpret that receipt as visual evidence. The image reader may return
an image block without modelImage metadata, so inspect the block itself.

Check whether the current connection really delivered an image, not only
whether the Host reported successful export. If a supported image reader is
needed, `read_evidence_file(path, inline=True)` can return the exact exported
PNG identified by artifact.output.path as image content. A native Agent may
instead open that same path using its available image viewer. Reading JSON,
printing base64 or reading the PNG as text does not show the picture. If media
delivery is unsupported or explicitly omitted, keep visual confirmation
incomplete, continue useful binding/Shader work and state the remaining visual
limit once. Do not repeatedly request the same unsupported media or describe
its appearance from a filename.

Record concise visual observations tied to the EID, texture, view and display
range: visible geometry/silhouettes, sparse atlas regions, smooth illumination,
packed-looking channels, blur spread, motion or a change after composition.
These are clues, not final algorithm labels. Test the hypothesis against actual
Shader sample/decode/arithmetic/output and producer/consumer resources. Similar
colors can encode unrelated properties; a scene-shaped texture can be lighting,
material data or a mask. A dark screenshot can be poor exposure, not empty data.
When geometry matters, use mesh shape/layout, VS transforms and raster/depth/
stencil/blend state to test that reading; do not identify a function from its
image or an unpacked asset name alone. Use only the evidence relevant to the doubt.
If code and appearance disagree, check binding, resource version/subresource
and display mapping before changing the explanation. Use image.evidence for a
short observation plus the evidence that supports its meaning; do not invent a
visual observation merely to fill that field.

For geometry clues, `capture_mesh_image(event_id=..., inline=True)` delivers a
bounded post-transform wireframe and its exact positions/indices evidence.
Inspect the returned picture and limitations, not just counts or an input layout.
It does not apply depth/stencil, culling or fragment shading. Select the relevant
instance/view; partial geometry cannot establish the entire draw's shape or
coverage. Follow [module-discovery.md](module-discovery.md) for targeted geometry,
visibility, temporal and unpacked-asset investigations.

Choose representative views where a contribution appears, changes meaningfully
or joins the downstream result. These are selection criteria, not prescribed
effect names, stage counts or an image quota. Observe key inputs and the actual
combined result when explaining a separate effect. Preserve the full texture view for frame-level relationships; a crop
may help inspect a local feature but cannot replace the whole-picture preview.
For a GIF or slider, view the constituent images with the same mapping instead
of assuming that a valid list of indices demonstrates the intended effect.

## Texture views

Check orientation per resource view, including intermediate targets from a new
game. Compare recognizable silhouettes or scene landmarks with a known screen
reference, then trace the producer/consumer Shader UV transforms, projection
and viewport convention where needed. A negative viewport height or `1-v`
alone is not proof that the exported image still needs flipping: another stage
may cancel it. Never flip all textures because of the graphics API, engine or
an asset name. When the evidence establishes an inverted preview, export with
`flip_y=True` and retain that choice in image.evidence; native is the default.
This changes presentation only. The returned image, persisted PNG and its hash
already include the transform. Do not flip it again. Channels and volume slices
inherit the same orientation. Recheck final alignment when composing a comparison.

For Texture3D, inspect textureDimension, resourceDepth, mipDepth and the effective
viewSliceFirst/viewSliceCount from the binding inventory. `slice` means a depth
plane at the selected mip, not an array layer. A native numSlices=0 on a true
3D view is normalized by the Host from resource depth; do not abandon it as an
empty texture or pretend an unrelated 2D screenshot is its output. Inspect a
meaningful depth plane, or a few distributed planes if content varies through
the volume, to identify color, density or other stored quantities using Shader
evidence. A slice is not a ray-marched volume rendering. Publication precaches a
bounded set of slices and available channels for the 3D slice popup; scrubbing
uses these saved images without replay. Show the actual sampled Z indices,
preserve a shared display range and orientation, and keep the cache bound to
the exact capture, EID, mip and replay revision. Do not infer world-space Z or
linear depth from a froxel grid without the Shader's mapping. If a genuine
volume view is useful, it requires a justified sampling/transfer function; do
not mislabel a stack of slices as a density render.

Keep the default 0–1 display whenever the image is readable. Do not normalize
every RT just because it is HDR, depth, sparse, or has a few outliers. Adjust
the range only when the useful content is substantially invisible or washed
out, using the full selected subresource's distribution and checking the actual
result. Preserve deliberately black/white masks and constant images. The
visible-distribution policy keeps 0–1 for readable content and records why it
changed an unreadable view. Comparison images must share one justified mapping.

`capture_event_texture_evidence` accepts an exact capture/EID, attachment,
subresource and view. For color, use `attachment_kind=rtv` with
`channel=rgba/r/g/b/a`; for depth, use `attachment_kind=dsv`,
`attachment_index=0`, `channel=depth`. Supported range modes are `native`,
`explicit`, `visible-distribution` and `range_mode=subresource-minmax`; only `explicit` takes
black/white points. Choose the actual bound slot and mip/slice/sample from
capture evidence. The returned receipt, including skips and limits, determines
what was saved. A requested channel is not satisfied by a different channel.
Reuse a matching successful receipt instead of exporting another copy;
`reused=true` is a valid diagram result.

When the captured Shader's `asfloat` use or corroborated asset encoding proves
that an R32_UINT resource stores float bit patterns, request
`capture_event_texture_evidence(value_decode='float32-bits',
range_mode='visible-distribution')`. This explicitly decodes the complete
selected subresource for a visible preview; retain its encoding, full-read
counts and source hash receipt. It does not modify GPU bytes. Integer format
alone does not establish this encoding, depth meaning or linear depth. Do not
reconstruct a whole image from a truncated `get_texture_data` byte prefix.

For the presented frame, use the exact Present EID with
`attachment_kind=present`, `attachment_index=0`, `channel=rgba`. This resolves
the action's copyDestination rather than a stale graphics RT binding. Use
native or explicit 0–1 display for normal display images; HDR needs its actual
supported mapping. A stage containing that Present target needs this receipt,
not a standalone UI texture or a guessed last RTV0.

The combined color preview displays RGB without alpha compositing. Alpha is
often a packed property or zero; it must not make the diagram transparent or
hide the scene. Useful channel previews are opaque grayscale images attached
to the parent texture's channel popup, with semantic groups laid out horizontally. Do not default to displaying
A; show it separately only when its data helps answer the question, and explain
what the mask/property controls. Group channels by meaning: RGB color/normal
plus an independent A mask usually needs two views, not four scalar pictures.
The exact source channels remain in the receipt even when the UI presents a
color image without alpha blending.

When updating an older diagram, inspect suspiciously dark or empty RGB previews.
An old PNG scaled while alpha was active may already have lost RGB where alpha
was zero; discarding alpha afterward cannot recover those pixels. Recapture
only the affected views with the same verified EID/binding and reuse their
established semantic investigation instead of analyzing the frame again.
New receipts with output.reduction.opaqueRgbProcessed=true confirm that RGB
was preserved by dropping alpha before scaling. A missing marker alone does
not prove an old preview is damaged; old unscaled or opaque images may be valid.

For a frame diagram, `inspect_frame_render_stage(flow_id, event_id)` collects
one EID's RTV/DSV/UAV/SRV texture bindings and preserves the observation. For
`attachment_kind=uav` or `attachment_kind=srv`, use its observed Shader stage
and attachment_index from the returned binding list, not a guessed register
number. Compute outputs are UAVs; inherited graphics RT state is not a compute
output. Buffer bindings are not texture images.

A depth PNG displays raw device depth. Linear or world-space depth needs a
separately justified reconstruction. Keep range evidence tied to the same
subresource; `subresource-range-flat` means the Host could not create
contrast from constant depth. Do not assign GBuffer meanings such as normal,
roughness or albedo from slot, filename, format or color alone. Use Shader
writes, view format and downstream reads when those meanings matter.
A disabled write channel cannot be claimed as changed by that draw.
When producer writes only reveal an encoding, use `inspect_frame_resource_usage`
after that producer EID and targeted consumer Shader reads to determine the
property. Follow through
copy/filter/repack operations if needed, caching resource users and visited
Shader calculations. Keep supporting EIDs and derivations in image.evidence;
the visible description should explain the property and use in ordinary words.
Do not repeat format, size, physical-attachment labels or inspection limitations
in that caption. Retain unresolved findings in investigation/review and continue
available work rather than hiding a gap behind a cleaner caption.

`range_mode=visible-distribution` is the default illustration exposure. Evaluate
the populated area of the whole texture, not a picked pixel or a rare extreme
sample. Check the exported view for lost dark structure or clipped detail,
especially for sparse atlases, depth and HDR illumination. Use the reported
distribution to refine explicit black/white points when needed. Global extrema
from subresource-minmax are diagnostic data, not necessarily a readable mapping.
Do not attach an almost black image as a completed explanation when contrast
can reveal its meaningful data. A truly constant/empty image is a finding to
retain, not a reason to invent contrast or produce a warning paragraph on each card.

For a large atlas, request a bounded thumbnail with max_dimension (at most
2048 for the large-atlas path), or choose a supported smaller mip and label it
accordingly. The source-byte budget still applies. Consider these supported
paths before reporting a concrete remaining limit; do not abandon a core
output merely because a full-resolution preview is too large or repeatedly
raise resource limits. Layout or caption changes need no new texture export.

For before/after claims about one resource's bytes, compare the same resource
and subresource at supported timeline boundaries. Post-processing input/output
textures may be different resources and still form a useful visual comparison
when their relationship and dimensions are established; label that difference.
Use matching channels, display ranges and transforms for direct visual comparison.
Choose one explicit range for both comparison images and every animation frame;
do not independently auto-expose them and manufacture an apparent change.
If display mappings differ, explain them and do not infer numerical deltas from
the slider. Equal hashes of complete raw resource bytes
can prove unchanged bytes. Equal display PNGs only establish an unchanged display
under the same mapping. A bound output or declared write is a candidate, not
proof of actual pixel contribution. Preserve the receipt's capture identity,
EID, binding, subresource, channel, display range, hash and restoration state.

A before/after divider needs an explicit comparisons entry referring to the
two stage-image indices, a short label for each state and an explanation of
the operation between them. A scene input and its post-processed output can
be different resources. These are two states of one frame unless separate
frame evidence establishes temporal provenance; do not imply the preceding
game frame can be recovered from a single capture. For animations, publish
the verified PNG image indices through stage.animations as described in
[frame-rendering-flow.md](frame-rendering-flow.md). Keep a consistent mapping
and ordering, and describe whether it shows draw accumulation or actual
temporal samples. The generated GIF is a display aid, not timing evidence.
Use stage.processGraph only for established cross-draw/target resource handoffs,
not single-draw arithmetic or one snapshot relabelled as successive results.
Pair a calculation with its
representative texture; each displayed subgraph node needs actual image content.
Omit unnecessary or pure-text subgraphs. Comparisons and animations are optional
aids, not required endpoints. For subtle composition changes, prefer a draggable
divider with matching views and one shared range.
Connect RTs by their verified use, not slot number, similar appearance or an old
capture's event IDs. Show branches and combinations when resources split or
join; several simultaneously produced MRT views can share one operation node.
Choose those views from the verified algorithm in
[algorithm-investigation.md](algorithm-investigation.md). A colorful isolated
light/effect texture is not a substitute for showing or explaining the draw
that combines it with the scene. When composition belongs to a later stage,
keep that stage's exact EIDs and reference its result instead of moving an image
to the wrong range or inventing an extra composition inside the producer.
For UI, trace its layer to the scene-composition target and eventual Present
resource before labeling that picture as the final frame.

## Static diagrams and pass classification

For the interactive, illustrated frame overview in Bread, use
[frame-rendering-flow.md](frame-rendering-flow.md). The static diagrams below
remain useful for focused physical resource or API-stage explanations; an SVG
artifact receipt alone does not mean an application window was opened.

`render_current_frame_flow_diagram` supports `resource-flow`,
`pipeline-stage` and `pass-overview`. Use `event_id=0` for bounded frame
flow or one positive EID for a pipeline-stage view. Edges must reflect
supported resource handoffs, not merely adjacent EIDs. The Host supplies
escaped capture text, a fixed theme and artifact/hash receipts; truncation
does not imply the omitted actions do not exist.
`analyze_current_frame_flow(max_actions=8000)` is the Host's broad frame-read
limit; the diagram's 40-node/64-edge limit is only a display limit.

`classify_current_frame_passes` returns a fixed classification;
`apply_current_frame_pass_ai_proposals` can attach AIProvisional labels to
its classification. The separate semantic-pass panel is retired, so leave
`publish_to_ui=false` and explain results in chat. Persisted
project knowledge is a separate effect and needs corresponding authorization
and receipt. Neither operation rewrites the captured RenderDoc Event Browser
or original markers.

If a requested artifact is unavailable, provide the supported event facts
and name the missing visual or effect rather than claiming completion.
