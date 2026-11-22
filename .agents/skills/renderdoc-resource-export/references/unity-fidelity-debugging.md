# Source-faithful Unity effect debugging

Keep the user's accuracy target. Editable assets and a responding camera/light
are separate observations from effect fidelity. Do not downgrade a requested
1:1 result to an accepted approximation because the scene runs or a full-frame
SSIM is high. The Agent judges completion from actual remaining differences;
these steps are methods, not a tool-enforced sequence.

## Locate the first divergence

Match capture pose/projection/FOV, viewport/render scale, color space, exposure,
depth range/reversed Z, handedness, UV orientation, jitter, culling, blend,
stencil and material/vertex bindings before artistic tuning. Align passes by
their functions and resource versions, not matching EID and Frame Debugger index.

Use `bread_frame_debugger` to begin/inspect/select/capture events and `end` to
restore its previous state. A pending render is not a captured event. Its image
is the debugger inspection texture: preserve display mapping and inspect its
actual format. For algorithm precision use `bread_capture_texture` on the real
persistent RT or `bread_capture_pass_outputs` with `BreadPassCapture.Record`
after the actual stage's commands on that same command buffer. This records a
linear float readback before subsequent overwrites, including compute outputs.

Use `export_unity_texture_data` for exact RDC mip/slice/sample reference bytes;
common uncompressed float formats include comparison sidecars. Decode packed or
compressed values explicitly and align MSAA sample versus resolved output.
Compare a pass with the same input RT fixture. On its first difference, expose
module outputs for base color/normal, positions/view, direct response, shadow
visibility, indirect coefficients/radiance, environment/specular/rim,
coverage/alpha and composition. Use matching units and floating RT formats;
signed vectors and HDR must not be clamped into an 8-bit preview. Debug both
source and port with the same fixed inputs. Record the module's actual producers.

`compare_unity_effect_layers` accepts float32/float16 files and JSON sidecars,
per-layer absolute/relative tolerances, ROI and shared module labels. Inspect
channel bias, worst tiles, non-finite data and the first divergent layer.
Separate subject and background. Metrics do not establish algorithm identity;
a native-engine category does not waive the user's visual target. Common errors
across EIDs should lead to one shared input/include/algorithm repair, followed
by checks of affected variants and independent per-EID constants.

## Preserve resource algorithms and live dependencies

`trace_unity_effect_dependencies` follows prior writer candidates and possible
history-update passes without requiring a frame-diagram publication. Resource
usage is coarse: inspect subresources, partial atlas writes, shaders, dispatch
sizes, coordinates and actual render state before declaring a producer.

Use captured indirect-light functions and data first. Inspect 3D/cube/array
resources for world/view/clip coordinates, updates and history before deciding
they are immutable. A world-space baked volume may remain static while the
camera moves; that does not mean it relights itself when a dynamic light moves.
Preserve world-to-volume transforms, coefficient encodings, swizzles, precision,
original mips, explicit LOD and sampler U/V/W/filter/aniso/bias. The volume export
and import tools retain raw mip bytes; unsupported compressed formats need an
explicit decoder/format adapter, not an unrelated lighting replacement.

For screen-space or history resources, inspect source EIDs outside the selected
range and port their complete producer chain, including compute. Implement
current depth/normals/motion, reprojection, history allocation/ping-pong, validity
tests, warm-up, camera cuts and resolution/FOV changes as relevant. Frozen RTs
are comparison fixtures. SH/probes/SSAO, native TAA or Gaussian DoF are fallbacks
only after a specific portability limitation was investigated and their errors
were assessed; their availability alone is not a reason to replace the source.

## Shadow fidelity

Investigate source shadow caster and receiver functions, skin/alpha clipping,
light projection, atlas tiles, depth encoding, bias and filter taps. Inspect
actual texel utilization rather than only increasing resolution. The diagnostics
tool measures caster AABBs and proposes a padded, texel-snapped light-space fit.
Include receivers, offscreen casters and filter margins; use cascades where
necessary. Adjust the native implementation only when it can preserve the
target, otherwise port the captured draw/filter calculations as a custom pass.

## Delivery and documentation

Keep Materials, Models, Prefabs and Textures together. Name unique resources
`EID1267_BaseColor`, and omit the EID prefix for genuinely shared resources.
Deduplicate textures using both content and sampling/import identity. Preserve
GUIDs and originals through the explicit layout plan/Editor method; semantic
collisions need meaningful names, not hash suffixes or overwrites.

Write Unicode Markdown/HTML through the document tool and read it back. Display
encoding cannot repair ASCII question marks already replacing Chinese characters.
Bread's in-app reader supports styled text/tables, source text and interactive
native flowchart nodes/arrows. Unsupported Mermaid syntax stays in source;
use the supported flowchart subset rather than claiming full browser scripting.
Document the exact match conditions, live behavior, source/native boundary,
per-pass/module comparison, changed common inputs and remaining discrepancies.
