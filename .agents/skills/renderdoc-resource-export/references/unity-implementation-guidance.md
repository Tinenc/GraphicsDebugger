# Unity implementation and acceptance details

## Keep Bread source delivery on the initiating instance

The task's supplied Bread MCP route owns its RenderDoc window, project and RDC.
Use that route for captured-Shader preparation and publication even when other
applications are open. Inspect the current project/capture association once and
retain it; another window's active project is not a recovery shortcut. Repair a
concrete mismatch in this instance using the matching registered project rather
than moving the task to another window or importing the current RDC into an
unrelated project's dictionary.

When captured Shader source has been organized, deliver its source, calculation
outline and flow graph to Bread's Shader Workspace as well as deriving the Unity
asset. Use finish_shader_workspace_organization(show_in_workspace=True) for the
saved captured source, and get_shader_workspace_ui_state to confirm the correct
capture/stage/source is visible in the initiating window. Reuse prior registration
and display receipts; if missing, publish/show the existing saved source without
recompiling or reorganizing it. A Unity .shader/.compute file alone is not a Bread
Workspace delivery. Retained unvalidated source may be shown as an explicit draft;
display does not require replay apply and does not prove compilation or fidelity.

## Prepare and recover the Unity connection

Setting up the project-scoped Unity MCP is part of authorized Unity restoration.
Inspect the selected project and use start_unity_project_connection to install
the bundled/pinned provider and Bread Editor packages, reuse or open its normal
Editor, and establish a verified connection. Poll get_unity_integration_job while
preparation runs. Do not make the user press the MCP window's Start button when
these tools can do the setup. Preserve an existing working project-scoped MCP.

call_unity_project_tool can start missing-connection recovery automatically.
Follow its jobId and nextAction, then continue from the existing assets and task
progress. A reload, changed port or disconnected transport is not a reason to
restart the export, discard a completed pass or redo unchanged validation.
When operationOutcome is unknown, inspect the actual scene/assets or the existing
import request's status before repeating a write. Connection recovery alone does
not prove that a lost write succeeded or failed.

If automatic recovery fails, inspect its concrete Editor/package/runtime logs
and repair feasible causes. Use local commands/scripts and the installed Editor's
CLI or available application-control tools as appropriate; prefer background
operations and keep the exact project fixed. Use reconnect=True on
start_unity_project_connection after a repair. Avoid opening a second Editor on
the same project, changing global MCP settings, or focusing windows repeatedly.
Try a genuinely different supported route when useful. Ask the user only for an
unresolved requirement such as licensing/login, a missing chosen Editor, or a
remaining failure after feasible repairs. Do not loop unchanged failed setup jobs.

## Implementation and visual acceptance

Plan a clean final delivery while authoring. Read
[unity-final-handoff.md](unity-final-handoff.md) for generated-artifact ownership,
simple folders and post-cleanup normal scene reopening. Save objects and bindings
in the `.unity`/assets instead of leaving a construction or setup-script dependency.
Remove task-generated intermediate scripts and verification images after success;
retain required effect/runtime code and keep evidence in Bread task storage.

For Unity effect restoration, prefer preparing the captured source in Bread first:
reuse exact current reviewed/compiled/validated source, or decompile, organize,
review and run supported compile/ABI/output checks. Permanent replay apply or
live edit is not required. Reuse portable functions and CBuffer meanings; adapt
matrices, camera, lighting and other engine interfaces only after establishing
their spaces and conventions. A concrete application/tool limitation permits
transferring the best exact retained source/proposal, passed checks and unresolved
diagnostics, then completing organization, compilation, debugging and validation
in Unity. Use get_unity_shader_transfer_sources with explicit organization_ids
for retained drafts without promoting/applying them. Do not skip feasible Bread
preparation, discard successful proof, restart unchanged failed operations, or
call failed checks successful. A known source defect remains a repair item in
the practical environment. The target still requires actual compilation,
bindings, execution and visual comparison; captured-source proof alone does not
certify changed engine interfaces, coordinates or scene inputs.
The Agent owns the workflow and acceptance judgment. Tools provide data,
operations, observations and optional annotations; managed restoration gaps,
session IDs, UI publication and recovery suggestions are advisory, never a
required sequence. Use direct capture/project APIs when a convenience flow is
unavailable. Inspect the selected EID's current bindings rather than repeatedly
reconciling historical ledgers. For ranges, work incrementally in capture order,
or adjust order for actual dependencies: import, configure render states, render
and adjust one draw/pass in the shared scene, then continue. Optional
plan_unity_restoration_sequence/get_unity_restoration_step and
annotate_unity_restoration_step retain scope and progress without locking it.
Trace postprocessing input/output versions and upstream producers, including
outside the requested range, before designing ordered URP RenderFeature passes.
Reuse successful work for unchanged inputs. Rerun only checks whose actual
source/include/resource/configuration inputs changed, or a diagnosed failed
check after a relevant repair. A status read, task revision, reopened RDC or
missing display receipt is not a reason to recompile/re-export/revalidate.
Automatic Shader source choice selects the newest saved/publication version,
prefers HLSL when ages tie, and uses a stable owner if still tied. Multiple
historical sources do not require a user choice. Retain older files and unsaved
buffers; use the selected source and its concrete diagnostics to continue.
Source-quality annotations are advisory by default in finish_shader_workspace_organization;
keep the same source draft, perform the actual supported compile/ABI/output checks,
and use source_review_policy="strict" only for an explicitly requested hard review.
Missing task-epoch reads, scope tables or semantic-overview metadata do not invalidate
unchanged compiled evidence. If an annotation schema fails, use the exact contract
to repair its fields once; keep successful checks and continue the transfer with
the current source plus concrete missing/failed diagnostics. Do not re-begin or
reorganize unchanged shaders merely to obtain another task-local receipt.
Judge completion from the user's goal and real artifacts/compilation/execution/
images; document missing checks and approximations. Never invent passing proof.
Preserve a requested 1:1/high-fidelity objective. A live camera/light response,
high full-frame SSIM or a note saying "native approximation" does not satisfy
that goal while visible differences remain.
Treat editable Unity delivery as independent Scene and Game rendering with the
same required effects. Inspect effective RendererData (Scene uses the pipeline
default, Game may select another), Feature filters and actual Scene lighting/FX
switches early. Each camera owns its depth/normals/screen shadows/motion, jitter
and temporal history. Shared dictionaries or shaders do not prove that both
views execute. Never present Game color or reuse its screen/history RTs in Scene;
bind readiness/ownership per camera, including when an effect is skipped.
Use bread_inspect_camera_rendering, camera-scoped bread_capture_pass_outputs,
bread_capture_view_outputs and review_unity_view_evidence where useful. The SDK
supplies BreadCameraResources and a final-target URP probe template; integrate
these into the requested project only as part of the authorized restoration.
During implementation, trace every imported screen-size/texel-size and
screen-to-world/view-projection field to the CURRENT camera's actual GPU
binding. Reallocating an RT alone does not update these captured constants.
Never freeze Scene's aspect or restore a stale projection across a resize.
Use BreadScreenEffectUrpFeature as a starting point, mapping the original
CBuffer's conventions to its current view dimensions, UV scale and matrices.
Test actual wide AND tall Scene window aspects, then return to the initial
extent while pose, lighting and time are held. Merely scaling 640x360 to 800x450
does not test aspect. bread_resize_scene_view is an optional no-focus helper;
restore bounds afterwards and capture actual final outputs, not fixed-size
test RTs. review_unity_view_evidence consumes resizeCaptures and evaluates
projection/viewport agreement plus image correspondence. Inspect visible shape
and the first differing pass too. A source review, successful compile or
performed=true note cannot establish resize correctness. Keep unavailable or
failed resize proof as a concrete open gap while continuing independent repairs.
Test Scene-only motion, Game-only motion, different extents, unfocused/paused
views and history resets. Per-camera render sequences handle editor repaints;
one shared Time.frameCount must not advance both histories. Record actual final
outputs for both views and compare against original RDC under aligned pose,
extent, color/exposure and temporal conditions. Different poses should differ;
the required algorithms/settings remain consistent. Distinguish retained Unity
regression baselines from original-RDC comparisons. Local/module success and
finite output alone do not establish dual-view or whole-scene completion. Keep
remaining final-view differences open. Missing optional tooling is a reason to
choose another supported observation, not an import/workflow lock. Avoid per-frame
file writes and diagnostic Blits that alter later rendering state.
Keep Unity restoration in the background. Reuse the connected Editor; do not
activate, focus, maximize or repeatedly reopen its windows for screenshots,
imports, compilation, scene opening or inspection. Use MCP repaint/readback
instead of OS activation or a menu that opens/focuses a window. A user request
to show a window is separate from background restoration. Never work around an
unfocused Editor by stealing focus; queue its player loop/repaint and poll.
Implement Scene resizing correctly when FIRST authoring every effect, before
calibration or acceptance. Use the SDK per-view resource/layout helpers and the
URP screen-effect template where applicable; review allocation, viewport,
projection and UV mapping together for every screen-space producer/consumer.
For Scene resizing, allocate from that rendering camera's actual target
descriptor and active pixel viewport, including render scale, format, MSAA and
XR view. Distinguish allocation size from RTHandle active viewport and apply
correct texel size/UV scale-bias. Never use Screen.width/height or Game's extent
as Scene dimensions, force Game's aspect/projection onto Scene, or stretch a
fixed captured RT to fill a resized window. Run postprocessing and Volume
selection for Scene independently, preserving equivalent requested effects.
Recreate and clear/reseed affected resources; invalidate only that view's history
on extent/projection/settings/cut changes. Advance ping-pong and jitter once per
actual camera render, not per pass/callback or shared frame counter.
Investigate flicker in actual pass output: undefined RT contents, stale ready
globals, cross-camera resources, duplicate Features, repeated history resets,
wrong previous matrices and exposure/time/animation changes. Use
bread_capture_view_sequence and review_unity_temporal_stability for bounded
samples. Record at least eight steady final frames after measured warmup, in
BOTH Scene and Game, at three held camera angles and after returning to the
initial pose. Also test wide/tall/return Scene extents and unfocused/paused Game.
Capture native view extents and compare stationary groups only with frozen or
recorded time/animation/light/exposure/input conditions. Moving poses or normal
temporal convergence are not automatically flicker. One stable frame, similar
angles or module-only checks cannot establish whole-scene completion.
Explicitly orbit AND translate Scene's camera, adjust the live light direction
and color/intensity, and perform a move/settle/return test. Save actual Scene
sidecars and before/after light values. Use numerical output comparisons to
observe expected response and nonfinite pixels, then stable post-motion holds;
retain an actual moving sequence and its adjacent comparisons too, since two
good endpoints can hide flicker/invalid frames during motion. Visually inspect
shadow/specular/postprocess response across that sequence, not a scalar score.
do not demand identical pixels during motion. Baked/world-volume indirect light
may intentionally remain fixed while dynamic direct light/shadows respond.
State that boundary and check both final views after restoring test controls.
History dependence does not mean impossible: trace producers including compute,
implement runtime warmup/reprojection and preserve portable volume/static inputs.
If original external history cannot be recovered, quantify the remaining effect
and alternatives. After three distinct diagnosed attempts with no material
improvement, or an agreed no-progress checkpoint, save results and internally
reassess the hypothesis, inputs and methods, then execute another feasible route.
Do not ask the user merely because a counter/checkpoint was reached. Pursue
practical source/data reads, text/algorithm analysis, Shader instrumentation,
alternate tools, producer reconstruction and direct Unity repairs within scope.
Only when supported recovery cannot make further meaningful progress, or actual
external input/access/user-owned scope choices are indispensable, ask whether to
accept an isolated quantified residual with the effect retained or how to resolve
the concrete limitation. Record attempted alternatives and actual evidence. Never
offer dropping the effect as the solution. If attribution is still unproven,
continue investigating while a feasible route remains. If none remains, explain
what is unknown rather than accepting a guessed history limitation. Never repeat an unchanged failed attempt or
chase an unlimited image-match loop. Continue independent feasible repairs; do
not silently lower fidelity, disable a requested effect or claim complete while
awaiting a choice. review_unity_restoration_handoff is an optional diagnostic aid.
Also use periodic internal repair checkpoints (default 12 distinct measured
attempts per effect/window). Retain the best result and ledger, reassess residual
regions/metrics and choose the next concrete method autonomously. Improving work
continues without a user budget extension; a checkpoint is not an ending rule.
Stop repeating unchanged attempts. When all feasible supported routes are exhausted
without meaningful progress, save the measured limit and ask the user instead of
looping indefinitely or silently lowering fidelity. resolutionAssessment in the
handoff diagnostic records remaining routes or saved evidence of that real limit.
Temporary bypass is only a reversible attribution test. Under matched controls,
compare the SAME remainder on the source and Unity sides, separately compare
the effect output/contribution, then restore all overrides and the effect.
Bypass making an image look closer does not identify the cause; inputs, masks,
blending and downstream passes may compensate. Require matching remainder plus
isolated effect discrepancy before narrowing attribution, and keep root-cause
work open until the concrete defect/data limit is established.
Before handoff organize the owned asset root using plan_unity_asset_layout and
bread_apply_asset_layout (GUID-preserving moves). Keep Materials/Models/Prefabs/
Textures, necessary Shader/Runtime/Editor files, semantic EID names, mappings and
shared assets. Use bread_cleanup_empty_asset_folders only for actually generated,
empty, unreferenced folders; preserve nonempty/user/unowned/annotated folders and
immutable evidence. Recheck scene/prefab/material references and final output
after substantive moves; remove temporary diagnostic overrides and per-frame
writes. Report unresolved effects, disabled/approximate choices and resume steps.
Audit actual pass outputs first,
then debug the first divergent Shader module using identical inputs: geometry
and coordinates, material/normal decoding, direct light, shadow visibility,
indirect coefficients, specular/rim, coverage/alpha and composition. Use
bread_frame_debugger, bread_capture_texture and bread_capture_pass_outputs;
BreadPassCapture.Record must follow the actual commands on that same buffer.
Preserve linear float HDR readbacks/JSON sidecars and group repeated failures by
shared Shader/module/input. Fix common transforms, bindings or includes once,
then recheck affected variants rather than tuning every material separately.
Locate differences efficiently: compare native-size float outputs for spatial
bounds/channel errors, inspect a few semantic checkpoints, then follow actual
resource-version dependencies to the earliest differing boundary. Reuse
compare_unity_effect_layers reports with localize_unity_effect_difference,
including graph dependsOn, actual input values and controlled isolation records.
referenceBefore/renderedBefore measure after-minus-before contributions;
nonlinear/alpha/history effects still require matching inputs/state. Inspect
cbuffers, matrix order/space, geometry and varyings, texture mip/slice/format/
sampler, masks/depth/blend and temporal inputs in groups, then narrow only the
failing group/field. Test one changed condition per repair. Bisection is useful
only on a proven linear persistent divergence; do not assume errors cannot
cancel or branches share a cause. Rerender affected descendants and final views,
reuse unchanged proofs, and retain the best measured candidate without regressing
already matched core modules. Do not change tolerances/exposure just to pass.
Trace screen-space, history, shadow and compute inputs through
trace_unity_effect_dependencies, including producer EIDs outside the selected
range. Port the captured functions/resources and live producer chain first.
Use real Unity camera matrices and light direction/color through explicit
adapters. Do not replace captured indirect light with SH/reflection probes or
SSAO just because those APIs exist. Camera-independent 3D/cube/array textures
can retain the captured sampling/decoding; inspect their writers and coordinates
before deciding staticness. export_unity_volume_texture and
bread_import_volume_texture preserve source volume mip data; supply original
format, color space, U/V/W wrap, filters, LOD and world-to-volume transforms.
For shadows inspect original caster and receiver calculations, atlas occupancy,
projection/depth conventions, bias, PCF taps and alpha/skin coverage. Use
bread_shadow_diagnostics for measured bounds/texel footprint; adjust atlas
allocation/resolution or port the original shadow drawing/filtering with a
padded texel-snapped light-space AABB and appropriate cascades. High resolution
alone does not repair a wrong sampling matrix or missing caster.
Screen/history inputs require live equivalent passes, compute and persistent
history with camera-cut/resolution reset and correct reprojection. A frozen RT
is a reference fixture, not the final live input. Choose an approximation only
after investigating a concrete portability limit and assessing its local error;
native TAA/Gaussian DoF/lighting are not automatic substitutes for the originals.
Use a fixed RT fixture per post pass, ROI and per-channel/numerical errors so a
large unchanged background cannot hide subject differences. Explain remaining
differences; do not mark a strict match complete from global SSIM alone.
Keep resources together under Materials/Models/Prefabs/Textures. Use semantic
EID123_BaseColor-style names with no hash suffix; shared textures omit the EID
prefix and preserve exact sampling identity. plan_unity_asset_layout and
bread_apply_asset_layout preserve GUIDs and archive originals after rebinding.
Write readable Markdown/HTML through write_restoration_document using actual
Unicode content, inspect the saved bytes and link it for Bread's built-in viewer.
Do not send Chinese through a lossy shell pipe. Question marks already written
into a file need regeneration, not another display encoding. Include pass and
module comparison tables, dependency flowcharts and concrete remaining findings.
Unless the user explicitly requests a fixed-frame replay, deliver an editable
scene with conventional FBX models, TGA for LDR/data textures or EXR for HDR/
precision-sensitive textures, actual Shader/material/prefab assets and regular
MeshRenderer/SkinnedMeshRenderer components. Captured bytes are evidence and a
fallback input, not the final model/texture delivery. When original rig data is
unavailable, a documented current-pose mesh still supports a free camera and live
lighting; do not invent a recovered skeleton. Use bread_prepare_fbx_exporter and
bread_export_portable_assets when applicable. Expose analyzed artistic controls
on materials; supply system constants through scripts/live engine interfaces.
Use Unity camera matrices and light direction/color through explicit adapters;
preserve source lighting/shadow/indirect calculations and resources where feasible.
Trace and port history/screen-space producers before choosing a justified live
engine or spatial approximation for a concrete limitation; captured camera/light/
history buffers must not silently become the runtime implementation. Use the
capture's camera pose/FOV and lighting as the initial scene setup, then verify
camera rotation and light adjustment change the expected output. Preserve the
actual destination pipeline. Represent postprocessing through Volume controls
and the pipeline's real renderer/custom-pass integration, not a disconnected RT.
Inspect bread_inspect_editable_delivery and actual Game/camera output. A good
effect RT or screenshot does not establish correct final camera presentation.
For URP, use an allocated camera-color handle and an appropriate insertion point;
bread_prepare_urp_presentation supplies an optional adapter for effect runners.
Avoid duplicate runInCamera execution and check encoding/orientation/Volume state.
Open/reuse the interactive Unity Editor; batchmode/quit jobs or AssetImportWorker
processes are not the user's editing window. Managed launch already opens normally.
Separate comparisons with compare_unity_effect_layers: isolate the ported core
against identical inputs with a small numerical budget. Assess native shadows/GI
against the requested fidelity target; approximations need a concrete reason and
acceptable local error, not a blanket exemption. Use a common RT/input fixture per
post pass so upstream errors do not accumulate into its isolated test. Retain a
separate integrated comparison of the actual final camera output. For calibration
change real light, ambient and Volume parameters in a small diagnosed search;
do not recolor evidence images, repeat unchanged proofs or lower the goal to a
fixed screenshot. These observations guide your judgment; no tool sequence is
a workflow lock.
