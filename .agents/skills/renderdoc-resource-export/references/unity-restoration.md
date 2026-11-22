# Functional Unity restoration

Use this route when the user wants a draw/effect to work in Unity, rather than
only exported assets. Keep captured Shader reference data and the Unity
adaptation separate. Reuse saved Shader/source and export receipts;
repair changed parts without repeating unchanged mesh/texture extraction.

The default delivery is an editable, normally rendered project: FBX meshes,
TGA/EXR textures selected by sampling precision, materials, prefabs, engine camera
matrices, adjustable lights and shadows, and Volume-controlled post processing.
Captured buffers and frozen images are reference inputs, not the final default.
The saved `.unity` must open normally without a setup script or special command.
Remove generated construction/validation helpers, comparison images and scratch
exports after successful restoration, keeping required runtime code, final assets
and requested final renders. Follow
[unity-final-handoff.md](unity-final-handoff.md) for simple folders, dependency-aware
cleanup and post-cleanup reopening; retain evidence in Bread task storage.
Expose analysed artistic controls; assign technical constants through scripts.
Design the material inspector while writing the adaptation; follow
[material-inspector.md](material-inspector.md) for texture-first controls,
semantic labels and value-preserving color/range editing.
Keep exact capture-pose comparison separate from free-camera/light verification.
Use `bread_inspect_editable_delivery` to report concrete asset/runtime observations.
`bread_prepare_fbx_exporter` and `bread_export_portable_assets` assist conversion
after actual mesh/layout reconstruction; procedural buffers require that decoding
first. They do not infer a vertex layout or certify shading equivalence.

For camera color, render and read the actual camera target. A good intermediate
effect RT alone is insufficient evidence of Game output. For URP 14-16,
`bread_prepare_urp_presentation` can create an allocated-color-handle adapter;
other versions/pipelines need their version-specific implementation. Inspect
active renderer, viewport, depth/color attachment, final blit, color encoding,
camera stack and Volume overrides. Avoid duplicate execution/tonemapping.
Use `compare_unity_effect_layers` for ported-core, native-engine, post-process
and full-frame observations. Core algorithms use a small explicit error budget;
engine substitutes retain the user's fidelity target and require a justified
portability limitation and acceptable local error. Compare each post pass
against the same fixed input RT fixture to separate its own error from upstream
differences. Metrics and missing receipts are diagnostics; the Agent decides the
next operation. Never declare completion from a cached image after code changes.

## Durable restoration and repair

The Agent owns ordering, recovery and acceptance. Managed restoration checks
are diagnostics and evidence aids, not a prerequisite for direct project work.
Use `get_resource_export_payload` to read a captured EID's original files before
finalization; a broken glTF/validation/binding route must not lock those files.
Optional `plan_unity_restoration_sequence`, `get_unity_restoration_step` and
`annotate_unity_restoration_step` retain ranges, per-EID state and Agent evidence
notes. No previous step must pass a managed gate before selecting the next EID.
Prefer incremental draw order in a shared scene; adjust for actual dependencies.

Optionally begin a durable record with `begin_unity_restoration`, identifying the
selected EID's actual output and target project. The ordinary objective is visual
fidelity: preserve the core algorithm, make the intended effect visible, and fix
large structural or local differences. Use strict pixel comparison when the user
actually requests it. Keep the chosen objective and thresholds fixed during a run.
An explicit perfect/1:1 request keeps that strict target; visible shadow, indirect,
coverage or post-process differences remain open findings until repaired or the
user changes scope. A successful dynamic-response test alone is insufficient.
The default target output is camera color; an intermediate output needs an
explicit `target_output` such as a named global texture produced by the feature.
Both renderers use the same supported `display_mapping`. Never compare unrelated
EIDs or silently substitute a camera PNG for a depth/integer output.

Prefer source preparation in Bread: reuse matching reviewed, compiled and
validated sources, or decompile/organize/review and run supported compile, ABI
and output checks before porting. Permanent replay apply is not required.
`get_unity_shader_transfer_sources` reuses existing proof and reports missing
checks without executing the preparation itself. If application/tool limitations
prevent completion, record the concrete failure and transfer the best retained
source, passed checks and missing/failed-check diagnostics to Unity. Explicit
`organization_ids` selects retained proposals even after canonical source was
rolled back; it neither promotes nor applies them. A default read prefers valid
proof rather than marking every source reference-only. Strict validation can be
requested as a hard requirement, but ordinary migration retains the fallback.
If a retained file was edited after its session receipt, read its current bytes
and pin that observed content with `draft_sha256`; never rewrite an old receipt
or silently select an unobserved edit. Reads use saved-artifact integrity and do
not require constructing a compiler registry or reconnecting the application.
Source assessment, full-read and semantic-directory annotations do not gate
physical compilation or transfer. `finish_shader_workspace_organization` reports
them as advisory observations by default while retaining actual source identity,
compile/ABI/output checks. Use `source_review_policy="strict"` only for an explicit
hard-review request. Do not recreate an organization to satisfy a new task epoch.
Display-name publication or panel refresh gaps do not invalidate matching source
compile/ABI/equivalence proof. Retain that proof and list display gaps separately;
do not recompile or downgrade the source merely to finish presentation metadata.
Native Shader/reflection data can be used when readable source is unavailable;
no workspace source list is required to bind in that fallback.
Its source/dependency hashes, entry points and physical CBuffer identity link
`shader.organizedSources` to the export. The paged export specification exposes
`constantBufferFields`; use the current renamed and merged semantic fields.
The Host resolves actual captured values. Missing lanes remain concrete inputs
to investigate. A justified inferred layout/value or replacement Unity input can
be implemented and verified through the reconstruction route below, with its
provenance retained separately from captured facts.

`unity.assets` carries derived Shader, include, compute and script files with
explicit paths and hashes. `unity.pipelinePolicy` selects the actual existing
pipeline; creating URP is an explicit `allowCreateUrp` choice for an appropriate
new destination, never an implicit change to Built-in/HDRP. `unity.scene`,
`runtimeBindings`, `renderState` and `renderPlan` carry the corresponding settings.
After finalization, `bind_unity_restoration` verifies selected Shader references
and starts the managed import. Inspect its job; preserve successful artifacts.

Use `check_unity_restoration` after a substantive import or edit. Its
`repair_configuration=true` reapplies authoritative recorded settings and checks
again. Fix source/feature errors through project tools, then inspect and render
with `render_unity_restoration`. Review actual modules and adaptation choices
with `review_unity_restoration`. Its automated result stays factual; the Agent
can assess independently collected compilation/execution/images with
`record_task_review`, retaining missing checks and real defects. A change invalidates only the affected
acceptance evidence. Repeated identical images are no progress: diagnose another
cause rather than poll/reinstall or loosen thresholds. Raw asset-only imports
explicitly use `asset_only=true` and do not claim functional restoration.

## Autonomous capability and input recovery

Call `plan_unity_restoration_recovery` after a capability, platform, compilation,
missing-history or visible-output failure. Its routes use the actual target
pipeline/API/capabilities and prior artifact-backed attempts. A route catalogue
is not a ceiling on what the Agent may implement: inspect installed source and
version-matched primary documentation, derive a more suitable implementation and
run it. Missing fixed tools do not by themselves justify ending the restoration.

Apply the same recovery cycle on Windows, macOS, Linux and target-device APIs.
Keep the Editor GPU capabilities separate from the configured player build target
and its APIs: a working Windows Editor compute pass does not verify an Android,
iOS or macOS player. Inspect the installed build modules and package versions,
compile the target implementation and run on the intended device when available.
Use the actual capability receipts to choose compute, raster or CPU producers;
preserve the existing Built-in, URP, HDRP or custom pipeline. Repair missing
project tooling and version-specific integration with the available authorized
file, command-line and Editor interfaces. Report which target was tested rather
than claiming every device is verified from an Editor preview.

`apply_unity_recovery_assets` writes actual derived `.shader`, `.compute`, `.cs`,
include and input-data files under the task's `Assets/BreadRecovery` directory,
even when an existing MCP provider lacks a file-authoring tool. Use its previous
manifest revision and, for externally edited files, exact observed hashes before
replacement. It preserves unrelated project files and original capture evidence.
Refresh the Editor, inspect compiler errors, attach the actual implementation,
and collect fresh execution/readback and image evidence. File creation alone is
not an executed repair.

For unsupported compute operations, consider a version-correct ComputeShader,
equivalent fullscreen/geometry passes, or a C# producer that uploads the needed
inputs. Keep the core sampling/arithmetic and dependency relationships explicit.
For missing temporal buffers, infer their role from producer/consumer operations,
dimensions, formats and ranges; implement a documented seed or warm-up sequence,
then verify that the effect actually reads it. Counters and unknown layouts need
the same explicit reasoning and real binding checks. Prefer source evidence;
use documentation/search to fill knowledge gaps, not to claim unavailable frame
data was observed. Native Unity lighting, shadows and indirect light are valid
adaptation choices when they retain the requested effect.

Record each approximation using `record_unity_reconstruction`: select the exact
`sourceGapIds` from current task status, supply the inference classification,
reason/evidence, real `implementationFiles`, and the pass, runtime binding,
component or Unity interface that verifies its use. If a platform adapter changes
the graph, use `adopt_unity_restoration_render_plan` to bind the actual observed
version-2 plan. Review decisions reference each `reconstructionId` and explain
the `approximation` or `unity-native` substitution. Wrong project/capture identity,
corrupted evidence, compilation failure and absent output are corrected directly;
they cannot be cleared merely by declaring an approximation.

If later work truly resolves an original missing-input gap, retire its exception
with `record_unity_reconstruction` using a record containing `id`, `retire: true`
and a concrete `reason`. First obtain passing current checks, image comparison
and semantic review; the task must remain complete when that exception is removed.
Retirement preserves its history rather than erasing earlier inference evidence.

Finish with all eight review areas: geometry/coordinates, materials/constants,
lighting/indirect illumination, states/passes, scene/dependencies, readable source,
`core_algorithm`, and `visible_effect`. Normal target-renderer differences need
not fail a visual-fidelity task. Large missing effects, broken shapes or a blank
output still need repair. If one implementation route fails unchanged, execute
an untried alternative and retain successful assets and connection state.

If a finalized transfer specification itself needs correction, call
`fork_resource_export` with its verified bundle hash and a stable `revision_key`.
It preserves the old package and copies verified capture evidence into a new
operation without replay or resource extraction. Finalize the corrected mapping
using the returned new bundle hash, then bind the same restoration task again.
For ordinary derived Shader/scene edits, retain the current import and use the
project tools and check/render route instead.

## Inspect the destination

Use the supplied project or an already established target. The source game's
engine does not select the destination pipeline. `inspect_unity_project` reads
Editor/package requirements; package presence is not proof of the active pipeline.
After the managed connection is ready, call `call_unity_project_tool` with
`tool_name="bread_inspect_rendering"` to obtain actual pipeline, package versions,
graphics API, color space, depth/UV conventions and current cameras. Inspect the
active renderer/camera configuration for settings absent from that receipt.
An available RenderGraph API does not prove that Compatibility Mode is disabled.

The built-in importer can create a single-draw prefab/material and a preview
scene. Its default camera/light are framing aids, not restored capture state.
For HDRP, Built-in or custom pipelines, use project tools to create the matching
integration without replacing the user's render pipeline. The connected MCP can
edit scripts/assets, read compiler diagnostics and render cameras; a package or
connection error should lead to its concrete repair/retry route, not a fresh
export. Do not repeatedly launch Editors or install an unchanged provider.
After correcting a URP package installation failure, its returned retry contract
allows `tool_name="bread_prepare_urp"` with `arguments={"retry": true}` in the same
Editor. Ordinary status polling does not retry an unchanged failed installation.

## Preserve the selected computation

First prepare and validate the current EID's readable captured Shader in Bread
where practical, then transfer its actual calculations and CBuffer meanings.
On a concrete application/tool limitation, continue from retained source/data
in Unity with missing checks explicit. Keep reusable material functions, and
replace matrices, camera/light/shadow helpers with Unity interfaces only after
matching their spaces and conventions. Bread proof applies to the exact source,
inputs and checks observed; changing engine interfaces still requires Unity
compilation, execution and visual comparison. Derive helpers rather than replacing
the algorithm with an unrelated stock material. Match geometry/pose, projection,
viewport, cull/depth/stencil/blend, texture views and samplers. Replace matrices,
light and shadow access only after establishing each quantity's space and role.
Choose target-native lighting versus frozen captured dependencies explicitly;
dynamic illumination may differ even when the material algorithm is preserved.

Copy material values from this draw's CBuffer data, preserving vector components,
types and matrix order. Recovered/inferred names identify properties; physical
binding and byte offsets remain the mapping authority. Do not substitute unpacked
material defaults for animated/instance values. Keep camera/system constants in
their Unity interfaces and expose actual artist controls through material or
Volume parameters. Reconcile the values after compile fixes.

Every `system` mapping remains in `binding.systemInterfaces`. Review each exact
`fieldId` and `target` with a `unity-native` decision supported by the derived
source, coordinate space and Unity API. These values are not silently uploaded
as frozen material parameters. Material and runtime-buffer mappings instead need
actual value readback matching the authoritative capture bytes.

For a ComputeShader scalar/vector/matrix input, a `compute-upload` receipt records
the successful typed Unity Set operation and exact uploaded value. Unity does not
provide the corresponding general parameter getter; this receipt is not a GPU
readback. Acceptance also requires the current effect/pass to execute and produce
the compared output. Captured GPU buffers retain complete byte/hash readback checks.

## Effects spanning multiple passes

For geometry-free ComputeShader/fullscreen/custom execution, read the
[executable effect contract](unity-effect-contract.md) before authoring
`unity.effect`. It gives the actual resource, initialization, history, pass,
buffer-counter and version-2 plan fields with a validated temporal example.

Trace the requested output backward through resource versions and then order the
relevant producers forward. Include only dependencies needed by the selected
effect. An RT allocation can be written many times; distinguish its versions,
mips/slices and previous-frame history. Preserve per-pass size, format, filtering,
clear/load behavior, blending and history lifetime. Select insertion relative to
the actual target pipeline's available color/depth/normals and tone mapping.

`validate_unity_render_plan` checks a proposed resource graph before implementation:
`target` contains `pipeline`, `editorVersion`, `insertionPoint`; `externalInputs`
names source/history versions; each `passes` entry has `id`, `reads`, `writes`;
`outputs` names the effect's produced versions. Functional restoration uses
`schemaVersion: 2`, with `resources` describing dimensions, formats, samples and
lifetime, and `implementationAssets` naming the manifested implementation files.
Each pass also identifies its `implementation`, insertion point and attachment
load/store/clear policy. The validator finds missing producers, duplicate writers
and cycles. A valid graph does not prove the Shader algorithm. The actual pass
callback calls `BreadCapturePassEvidence.Record` with its camera, implementation
asset, resource versions and output RenderTexture. Final acceptance checks the
current render's matching execution and dependency order. A global texture must
be the exact output produced by a planned pass during this camera render.

Use the installed pipeline's supported extension route. For URP, inspect its
version and RenderGraph/Compatibility setting before choosing Renderer Feature,
render passes and texture handles. Volume components hold effect controls; they
do not schedule a pass by themselves. Wire them to the renderer and camera, keep
ping-pong outputs separate from sampled inputs, and preserve compute dispatches
when the operation requires them. HDRP and Built-in have different extension
points. Do not force every effect into a single fullscreen Shader or add a Volume
to unrelated geometry rendering. Unity's
[custom postprocessing with Volume support](https://docs.unity.com/en-us/engine/6000.0/manual/post-processing-and-full-screen-effects/urp/custom-post-processing/with-volume)
describes the two URP execution paths; use the APIs present in the target project.

## Render, compare and repair

Capture a RenderDoc reference at the requested output/pass, with the matching
viewport, image orientation and display mapping. Match Unity camera, resolution,
color/exposure, material values, animation/time and temporal history. A single
RDC may lack earlier history; infer and initialize the required inputs or warm up
their producer where justified, retaining the inference separately from captured
facts and validating its visible effect.
Use `render_unity_restoration` for the acceptance render. It captures the exact
reference and invokes `bread_render_capture` in the imported scene with the same
dimensions, target output and display mapping. This excludes screen overlay UI.
Provider camera screenshots can help diagnosis but cannot replace the task-bound
render receipt. Unsupported output formats need a real readback adapter before
they can be compared; never substitute a different output.

View both images returned by the restoration tool. Its comparison uses verified
SHA-256 values and all display pixels without
resize, flip, exposure or gamma correction and returns per-channel error and the
changed region. RGB is appropriate when alpha holds auxiliary capture data;
RGBA requires meaningful alpha in both. Use a shared explicit display transform
for HDR previews and inspect linear/HDR values separately when fidelity needs it.
Do not normalize images independently, hide differences by downsampling or widen
tolerances just to claim success. File comparison cannot certify matching pass,
dependencies, future frames, or GPU numerical equivalence.

Use discrepancy location and intermediate outputs to isolate geometry/coordinate,
sampling, material, lighting or composition errors. Fix a coherent cause, recompile
the affected code, render again and compare; use per-pass images to narrow a
multi-pass failure. Preserve successful imports and valid connection jobs during
repair. Stop when the requested fidelity is supported, or report the specific
unavailable dependency after viable alternatives. A successful compile/import is
not a visual match. Final text should state what runs, what was compared and any
remaining visible difference in a few plain sentences.
