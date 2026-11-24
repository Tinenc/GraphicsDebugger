# Independent Scene and Game rendering

An editable restoration renders both views through the same required algorithms
and settings. It does not copy Game color, depth or screen masks onto Scene.
Different camera poses produce different pictures; aligned pose, extent,
exposure/color, jitter and history permit a meaningful image comparison.

## Check the architecture before detailed calibration

Build resize-safe per-view rendering from the first implementation. Each
screen-space producer and consumer uses the rendering camera's target
descriptor, active pixel viewport and UV scale/bias together. `Screen.width`,
Game dimensions, a captured fixed aspect and an RTHandle's maximum allocation
are not Scene's active extent. Do not overwrite Scene's aspect/projection to fit
Game. Reallocate affected targets on descriptor changes and invalidate that
view's history on active viewport, resolution scale or projection changes.
Use `BreadViewLayout` for texel size and normalized viewport mapping, and the
`BreadScreenEffectUrpFeature.cs.txt` template for a per-view URP 14+ starting
point. Do not defer these decisions until the final resize acceptance check.
The same rule applies to Game in **Free Aspect**, including edits while paused
or unfocused. Do not pin `Camera.aspect`, projection or a final presentation RT
to captured dimensions in an editable camera. Unity's current camera/target
descriptor supplies the render extent; Game window UI points and the preview
Zoom/Scale slider are not render resolution. Preserve FOV/lens shift while
adapting aspect; the visible framing may change without distorting geometry.
Update dependent color/depth/motion/shadow-mask/history targets, inverse-VP,
texel size, UV scale/bias and dispatch group counts in the same camera render.
Distinguish render-scale pixels from the final display viewport and include DPI,
window docking and resize/restore in the design. Keep fixed capture-resolution
fixtures explicitly separate from the editable Game camera.
Inventory the actual GPU bindings for every translated capture screen extent,
texel size, screen-to-world/inverse-VP and camera-dependent CBuffer field.
Updating only the RT allocation leaves stale captured constants in the Shader.
Rebind those fields before each camera's draw/dispatch using the original
field's convention (Unity _ScreenParams and texture _TexelSize differ).
Do not save/restore a previous repaint's Scene projection over the editor's
newly calculated projection. Fixed capture projection is an explicit reference
fixture; editable views use their current viewport aspect. The SDK's projection
override adapts its horizontal focal scale by default and leaves Scene-owned
projection alone.

Keep the Editor in the background throughout restoration. Repaint/read back
existing views without `Focus()`, OS activation or repeated window-opening
menus. Use `bread_capture_view_outputs`/pass readbacks for evidence rather than
bringing Unity to the foreground. Explicit user requests to show a window may
open it; ordinary compilation, import and calibration do not request focus.

Use `bread_inspect_camera_rendering` or equivalent current observations. In URP,
Scene uses the pipeline's default RendererData, while a Game camera can select a
different renderer. Preserve unrelated project features when reconciling them.
Review Game-only filters, missing Scene jitter adapters, Volume selection,
render scale, HDR, depth/color attachments and Scene lighting/FX/draw mode.
Definition/source hints are not proof that a Feature actually executed.

Each camera/XR view needs its own screen depth, decoded normals, shadow mask,
motion, reprojection, history ping-pong, validity and sample sequence. Use the
SDK `BreadCameraResources<T>` or equivalent ownership. Include format/MSAA and
relevant settings in its descriptor key, clear/reseed on invalid history, and
mark validity only after real producer submission. Invalidate on cuts,
projection/FOV/extent changes, disabled views and changed algorithm settings.
Use actual per-view render sequences rather than one shared game-frame counter;
Scene can repaint while Game is paused or unfocused. Do not increment a sample
twice merely because the two views rendered in the same engine frame.

`BreadCameraEvidence.RecordFeature/RecordResource` records actual producer
observations. `PublishScreen` binds the current camera's texture and registers
its ready global for reset at camera boundaries. Otherwise perform equivalent
ownership/readiness resets even when an effect is skipped. Publish matrices and
light adapters for each rendering camera. One global texture binding may be
used sequentially, but must refer to the current view's separately owned data.

Static material/volume resources may be shared. Light-space shadow atlases can
be shared only when coverage and caster selection apply to both views; camera
frustum-dependent cascades or culling need their own calculations. Source-pose
projections/hardcoded caster lists need an explicit dynamic-coverage strategy.

## Obtain final outputs independently

`bread_capture_pass_outputs(camera_ids=[...])` records the requested pass once
for each selected camera. Supply the actual camera to `BreadPassCapture.Record`
or run under its observed SRP camera scope. Float sidecars retain camera ID,
type, render sequence, matrices and source sample count. Readback uses copies
and explicit MSAA resolve, not a Blit that silently changes later draw state.
Unsupported copies/formats return diagnostics; capture a persistent decoded RT
through a supported adapter instead of pretending a zero image is valid.

`bread_capture_view_outputs` requests each existing view's actual final target.
It only repaints existing views and never copies camera transforms or moves
them. A final backbuffer may need the opt-in
`Templates/BreadViewEvidenceUrpFeature.cs.txt` adapter in each effective renderer.
That URP 14+ probe runs after final blit and copies the current camera target;
inspect final ordering/display encoding on another pipeline/version. A pending,
timed-out or unsupported target is missing evidence, not a fabricated success.
Source scope excludes Scene editor handles/gizmos; compare rendered scene color.

Separately check editor overlays: with camera/light Transforms held fixed,
observe light gizmo/handle screen positions over at least 16 Scene renders and
use `bread_observe_scene_overlays` request/status/cancel to collect actual
HandleUtility projections and camera/culling matrices without focusing Unity.
Repeat while resizing wide/tall/wide. Temporal jitter must affect raster commands only,
not the SceneView Camera projection/culling matrices or editor handles. Prefer
`BreadRenderProjection.WithPixelJitter` with the current physical viewport;
restore every temporary GPU matrix/global before later passes and gizmos.
Do not lock the Scene camera aspect or reuse a captured Game projection to stop
the flicker. Inspect pre-render and post-render camera/culling matrices plus
non-jittered projected marker positions to distinguish leaked jitter from
changing lights, unstable history or depth fighting. A stable Transform alone
does not demonstrate stable editor projection. Report this overlay check
separately from final-color parity, and never declare dual-view acceptance from
source inspection or one preview image.

Compare both final outputs to original RDC at matched conditions. Keep a
retained Unity kernel/reference labeled as regression evidence, and identify
the original-capture proof it inherits. A good isolated pass or finite image
does not prove the final composition. Check actual final blit/tonemapping and
all enabled effects in addition to module debugging.

## Isolation and acceptance observations

Record Scene-only motion with Game fixed, Game-only motion with Scene fixed,
different view extents/aspects, camera/history resets and an unfocused/paused
Game with an independently moving Scene. Freeze animation/time and stabilize
temporal sampling, or measure its normal variation, before interpreting a
changed supposedly fixed image as cross-contamination. Exercise a second Scene
view or Game camera where applicable. Verify allocations/generations and output
provenance rather than relying on similar-looking pictures.

`review_unity_view_evidence` consolidates observations, captures, full-frame
comparisons and experiments into diagnostic findings. It neither accepts the
whole task nor locks import/repair. Missing instrumentation is unknown; obtain
equivalent supported evidence and let the Agent judge actual completion. Reuse
unchanged proof, keep concrete gaps open, and remove temporary diagnostic
clears/material substitutions and per-frame file I/O before final comparison.

Collect a separate Game `game-resize-wide-tall-return` experiment. Set
`freeAspectRatio=true` only from actual Game view mode observations. Use
`bread_resize_game_view` list/request/restore for an existing window, keeping its
original bounds and resolution selection, and restore in finally. Its guarded
reflection adapter reports unsupported editor versions rather than inventing
Free Aspect readiness. Observe actual camera extents after repaint/settling;
requested window sizes alone are not evidence. Supply the same four real
original/wide/tall/return final captures as the Scene resize experiment, but
with the Game camera's own ID, matrices and render sequences. Each observed
Game/Scene camera needs its own resize evidence. Scene-only proof does not
establish Game Free Aspect correctness, and different aspect ratios must not be
hidden by stretching the reference/output images.
