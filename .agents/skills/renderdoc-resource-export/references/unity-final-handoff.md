# Resize, stability and honest handoff

Scene and Game execute the same requested functions independently. Use each
rendering camera's target descriptor and active pixel viewport, not global
`Screen.width/height`, Game resolution or a fixed capture aspect. Distinguish
allocation size from RTHandle viewport, dynamic/render scale and viewport origin.
Supply texel sizes, UV scale/bias and that camera's matrices to every relevant
draw/compute dispatch. Recreate resized/format-changed per-view targets and seed
invalid history before reading it. `BreadCameraResources.Acquire` has a descriptor
overload, reset reason and once-per-render `CommitHistory`; call the latter after
real producer submission, never for a skipped/failed pass. A reset is not seeded
history. Use a non-jittered projection for cut detection, per-view previous
matrices, ping-pong/jitter indices, and explicitly invalidate on a real cut or
changed effect settings. Do not reset history every editor repaint or share one
`Time.frameCount` between cameras.

Postprocessing must execute for Scene too. Inspect effective RendererData,
camera filters, Scene FX/lighting switches and the Scene camera's Volume trigger,
mask, resolved stack, exposure and settings. Sharing Feature code/settings is
normal; sharing screen-space/history targets is not. Do not bypass Scene by
displaying Game output or hardcoding Game's projection. A light-space atlas is
shareable only when its actual culling/coverage serves both cameras.

## Stability observations

Use `bread_capture_view_sequence(action="request", camera_ids=[...],
sample_count=8, warmup_renders=2)` then status for a bounded sequence of final
outputs. It repaints existing cameras without moving them or controlling scene
inputs. Backbuffers need the explicit final-target probe. Two warmup renders are
only a default, not proof of convergence: record actual valid history and steady
output; use a longer bounded warmup or annotate convergence separately. No
perpetual capture/update loop belongs in the delivered project.

Collect at least eight steady frames of each view at three distinct held angles
and a return to the first angle. Restore the capture-aligned Game pose/settings
after tests. Repeat Scene and Game Free Aspect holds at wide, tall and original
window extents; verify
geometry/sampling keeps the correct aspect while different FOV framing remains
expected. Do not resize evidence images to disguise a mismatched render. Include
actual original/wide/tall/return final captures for each view under held inputs: changing
640x360 to 800x450 exercises allocation only, since both remain 16:9. Use
`bread_resize_scene_view` or an equivalent supported way to resize the existing
window without focus, restore its saved bounds in finally, and inspect actual
camera pixel extents after settling (docking/DPI can alter requested bounds).
For `review_unity_view_evidence`, the resize experiment supplies
`resizeCaptures={original: capture, wide: capture, tall: capture, return: capture}`
with real sidecar paths, `controlledInputs=true` and an `inputStateKey`. The
review checks each camera's ordered renders, restored extent, held pose,
projection aspect and projection-predicted common image coverage. Flat or
unobserved frames do not establish geometry correctness. Retain numeric
differences, inspect actual final shape, and isolate the first stretched pass;
the correspondence report is a diagnostic, not a whole-task acceptance lock.
Game needs its own `game-resize-wide-tall-return` experiment, with
`freeAspectRatio=true` from actual Game view mode observations. Use
`bread_resize_game_view` list/request/restore without focus and restore the
original bounds and resolution selection in finally. Preview Zoom/Scale alone
is not a resolution change. Update actual per-view RTs, sampling/dispatch
parameters and projection together, reseeding that view's history. Include
paused/unfocused Game renders; Scene-only evidence cannot establish Game
correctness. Fixed-resolution comparison fixtures stay separate from the live
editable Game camera.
Use a controlled geometry/lighting layer to disambiguate legitimate changes in
resolution-dependent effects. Never fix this by stretching output to a window.
Include
an unfocused/paused Game and an independently repainting Scene. Record normal
animation/time/exposure inputs or hold them fixed temporarily, then restore them.
Moving images cannot be classified as unstable by raw pixel differences alone.
Also orbit and translate the Scene camera, change the live light direction and
color/intensity, then move/settle/return. Record actual before/after Scene
sidecars, light control values, expected dynamic response and numerical output
observations; frozen outputs or NaNs during motion do not pass. Retain actual
moving sequences and inspect transient frames, not only endpoints. Distinguish
dynamic direct light/shadows from deliberately static baked world-volume GI.
Restore test controls and verify steady outputs after the motion stops. See
[unity-difference-localization.md](unity-difference-localization.md) for fields.

`review_unity_temporal_stability` reads actual sidecars and float/image files.
Annotate `scenario`, `poseId`, `controlledInputs=true`, `inputStateKey` only from
real test controls; label warmup frames explicitly. Groups with different poses
or extents are not compared pixel by pixel. Adjacent and A/B/A observations,
nonfinite output, local worst tiles and history readiness are diagnostics, not
semantic acceptance. Investigate the first divergent pass/module for stale RTs,
uninitialized clears, shared masks, duplicate Features/callbacks, history resets,
wrong projection/motion, sampler/texel-size errors and exposure changes. Keep both
final original-RDC comparisons and multi-frame evidence in final review. Static
images, successful shader compilation or high SSIM alone do not complete the task.
Review one held pose at a time for high-resolution images, then combine the
same-schema report groups/findings for `review_unity_view_evidence`; do not
recapture unchanged evidence merely to fit it in one tool request. Original-RDC
fidelity comparisons use capture-aligned inputs; other held angles establish
dynamic behavior/stability and need not be pixel-identical to a different RDC pose.

## Convergence and user decisions

Temporal dependence is not itself a blocker. Trace original producer EIDs,
including compute and indirect work, and implement dynamic warmup/reprojection
where practical. A single captured frame may not contain original prior history,
but a live implementation can generate its own future history. Preserve portable
3D/volume/static data where its sampling permits it.

Retain a per-effect attempt summary: hypothesis/strategy, actual changed source
or inputs, measured difference and remaining limit. Reuse unchanged proof. Stop
repeating identical failed attempts. After three distinct diagnosed attempts with
no material improvement, or an agreed time budget, prepare a concrete checkpoint
and reassess. Also keep a finite total repair checkpoint (12 distinct measured
attempts per effect/budget window by default), even when improving, so tiny
improvements cannot create an unlimited loop. This is an advisory checkpoint,
not an ending or user budget gate. At a checkpoint retain the best result and
internally change methods, then execute another feasible route. Improving work
continues autonomously. Try alternative data readers, source/text analysis,
instrumentation, original producer reconstruction and direct Unity repair as
appropriate. Only when these supported routes cannot make further meaningful
progress, or indispensable external input/access is missing, finish independent
work and ask about the concrete limit. Accepting a residual requires the effect
retained, isolated discrepancy and matched remainder; unknown attribution must
be explained honestly. Keep the ledger across internal strategy windows.
An untried feasible same-scope repair remains work; missing an optional tool alone
does not authorize abandoning the effect. `review_unity_restoration_handoff`
reports attempt/effect/baseline gaps without gating execution.

Do not call an unapproved approximation or omitted effect complete. If only the
user choice remains, `record_task_review(verdict="blocked", ..., blocker=...,
decision_request={"question":...,"options":[...],"evidence":[...]})` preserves an
honest incomplete result and ends automatic recovery until the user's reply.
Use 2-3 concrete options and saved, reviewable evidence. Review continuation is
not consent to resume. A user request to continue permits the chosen new work;
it does not retroactively make previous validation pass.

For an effect with a measured residual, explain its exact data/algorithm limit,
what was tried, runtime alternatives and visible consequence. Temporarily
bypassing it is a reversible attribution experiment: compare the equivalent
remainder on both sides AND its isolated output/contribution under controlled
inputs. A closer bypass image alone cannot prove the cause. Distinguish a local
pre-effect boundary from whole-frame remainder proof and label source debug
interventions as controlled RDC references, not untouched original outputs.
Restore the effect and all diagnostic overrides afterwards. Its omission is not
the proposed solution. User decisions concern accepting the quantified retained
effect residual or further reducing it. Keep root-cause work open if the
isolated discrepancy/remainder evidence is missing.

## Asset handoff

The default deliverable is a saved, directly openable Unity scene and its final
assets, required rendering code and requested final renders. Before authoring,
track every task-generated artifact and its purpose. Put builders, one-shot
import/setup scripts, validators, comparison images, probes, logs and scratch
exports in task-owned temporary storage outside the final asset tree wherever
possible. Keep intermediate evidence and source-to-output receipts in Bread task
storage while they are needed; do not duplicate the archive into the user's Unity
delivery. A final requested render is an output, not a disposable validation image.

Use one semantic root such as `Assets/<Effect>/`, with only the needed `Scenes`,
`Models`, `Materials`, `Textures`, `Prefabs` and `Shaders` folders. Add `Runtime`
only for code actually required by the effect, and `Editor` only for a necessary
material inspector or other delivered editing feature. Do not create empty
folders, per-attempt/per-EID directory trees, validation galleries, backup
projects or a second staging copy in the final tree. Reuse existing project
structure when that is simpler. Put requested final renders together in one
`Renders` directory outside Assets unless Unity needs to reference them.

Serialize scene objects, camera/light settings, materials, Volume profiles,
RendererData/Features, references and relevant project configuration to disk.
The user must be able to double-click the final `.unity` and see the correct
Scene/Game result through normal rendering. No menu command, execute-method,
setup script, MCP call, Python process or special scene opener may be needed to
construct or initialize the delivered scene. Normal rendering components may
allocate per-camera/history resources and run the effect automatically; preserve
those genuine runtime dependencies. Do not delete all `.cs`, compute shaders,
data textures or `.asset` files by extension. Replace a bootstrap that creates
objects or assigns essential bindings with saved objects and serialized bindings.

Apply `plan_unity_asset_layout` through `bread_apply_asset_layout`, preserving
GUIDs instead of copying/deleting `.meta` files. Keep flat Materials, Models,
Prefabs, Textures and necessary Shader/Runtime/Editor assets, semantic EID names,
shared-texture identity and source-to-output mappings in Bread storage.
Use `bread_cleanup_empty_asset_folders` first in dry-run mode and then apply only
inside the actual owned restoration root with `managed_folders` from generated
output/manifest facts. It retains referenced/labeled/bundle folders, orphan
metadata, nonempty/unowned directories and the root itself. Do not infer file
deletion permission merely because an asset is not currently visible.

After meaningful asset moves, check scene/prefab/material links, GUID continuity,
normal renderer/model/texture bindings, compile errors and final camera output.
Also repair source-relative Shader includes, Resources/Addressables paths and
assembly-definition scope when code/data moves; GUID continuity alone does not
preserve those dependencies. Keep necessary existing folder boundaries.
Remove temporary diagnostic overrides and per-frame file writes. Report full,
partial-awaiting-user or accepted-subset delivery honestly, with concrete
residual/approximate effects and resume instructions. Cleanup is not image proof.

After successful implementation and comparison, finish this handoff autonomously:

1. Inventory generated files as `final`, `runtime` (with the reason it is needed)
   or `temporary`. Inspect Unity dependencies and references, including string-
   loaded resources, Shader includes and render callbacks. Finalize or move any
   required data out of temporary paths before deleting their producers.
2. Remove this task's temporary builders, setup/validation scripts, reference and
   difference images, readback sidecars, logs and intermediate exports. Delete
   imported assets through Unity's AssetDatabase/project tools so their `.meta`
   files are removed together. Use explicit owned paths, never a broad extension
   search or recursive wipe of the project. Preserve existing user files, shared
   dependencies and Bread's original capture/source/history records. Do not
   remove a failed/unresolved checkpoint needed to continue unfinished work.
3. Save final scenes/assets and let compilation/import settle after removing
   helpers. Preserve unsaved user work; reopen the saved `.unity` through the
   ordinary Editor scene-open operation without executing a setup helper. Check
   missing scripts/references, compile errors and actual final Scene/Game output.
   A domain reload or project reopen is useful to rule out reliance on transient
   state when feasible. Fix any cleanup regression before delivery.
4. Store post-cleanup verification evidence outside the final Unity tree; report
   the exact scene path and retained runtime dependencies. Reuse existing valid
   stability/fidelity evidence for unchanged content. Call
   `review_unity_restoration_handoff` with `projectPath`,
   `artifactInventoryReviewed=true`, `artifactInventory=[{path, role, reason,
   requiredByFinalAssets}]` and `sceneDelivery={scenePath, sceneSha256, saved:true,
   reopenedFromDisk:true, requiresSetupCommand:false, missingScripts:0,
   missingReferences:[], compileErrors:[], finalOutputsReviewed:true,
   evidencePaths:[absolute post-cleanup evidence paths]}`. The review observes
   remaining listed temporary files/metadata, missing final files and the current
   scene hash (record `sceneSha256` from the bytes actually reopened). Reopen,
   ownership and dependency fields describe actual Agent observations; they do
   not create proof or permission. Missing managed receipts do not prevent direct
   repairs or evidence-based assessment.
