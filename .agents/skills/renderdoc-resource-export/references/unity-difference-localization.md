# Efficient effect attribution and live-view response

Preserve the requested fidelity. The goal is to identify and fix or reduce the
actual discrepancy while keeping the effect. A temporary bypass is an experiment
to rule out a mistaken attribution, not an offered delivery without that effect.
These are Agent-chosen methods and acceptance observations, never import locks.

## Narrow the first differing boundary

1. Align native extent/viewport, pose/projection/jitter, temporal phase, encoding,
   exposure, texture view/mip/sample and render state. Save a linear float final
   image and inspect its difference region/channels, separating subject from
   unchanged background. Do not resize, recolor or widen tolerances to pass.
2. Inspect a few semantic checkpoints in the actual producer graph: geometry/
   material data, lighting/shadow/GI, pre-post color, post chain and presentation.
   Map source EIDs to Unity function/resource versions, not identical indices.
   Follow the first differing input/output boundary and its actual ancestors;
   do not dump and reprocess every EID before choosing a suspect.
3. Reuse `compare_unity_effect_layers` reports with
   `localize_unity_effect_difference(comparison=..., pass_graph=...,
   inputs=..., isolation=...)`. The latter does not reread image bytes. Each
   graph node has `name` and actual `dependsOn` observation names. It separates
   differing outputs with differing upstream inputs from candidate boundaries
   whose supplied inputs matched, and groups shared module repair candidates.
   Root-cause and whole-task acceptance remain Agent judgments.
4. Compare the suspect's individual output: shadow visibility, indirect radiance,
   normal/position, history rejection, CoC or another meaningful module. Optional
   `referenceBefore` and `renderedBefore` file records compare after-minus-before
   contributions. For nonlinear blending/tone mapping/history this describes an
   observed contribution; it does not make the effect additive or eliminate the
   need for equal inputs/state. Preserve float channels and physical units.
5. Screen actual inputs in groups: matrices/space/order, vertex/varying decoding,
   CBuffer fields, texture view/mip/slice/format/color space, sampler/LOD, depth/
   mask/stencil/blending and temporal data. Then narrow the failing group/field.
   `inputs` records contain `name`, `kind`, `reference`, `rendered`, `consumers`
   and optional finite `absoluteTolerance`/`relativeTolerance`. Supply actual
   decoded values/metadata; the tool returns differing nested field paths. Equal
   content bytes alone do not prove equal sampling or bindings.
6. Change one diagnosed condition, retain the best measured candidate and reuse
   unchanged checks. Fix a shared include/input once, then rerender affected
   descendants and final views. Regressions in matched core modules are not an
   acceptable trade for a higher global image score.

Bisection is useful only when a chain is linear and a persistent divergence has
been established. Branches, masked errors, cancellation and temporal feedback
require boundary checks; a later matching image does not prove earlier passes
matched. Missing graph/input evidence is a diagnosis item, not permission to
guess a different algorithm.

## Reversible attribution, not omission

Retain three numerical observations under recorded controls:

- Full output with the effect present differs from the source.
- Equivalent remainder matches with that one effect bypassed on both sides, or
  controlled pre-effect inputs match at a clearly labeled local boundary.
- The effect's isolated output/contribution differs under matched inputs.

In `isolation`, name `effect`, `fullOutput`, `bypassOutput`, `effectOutput` from
the comparison report and record actual `sameInputs`, `sameEnabledRemainder`,
`singleEffectIntervention`, `restored`. Comparison `referenceOrigin` must be
`rdc-original` or honestly labeled `rdc-controlled-isolation`. A retained Unity
kernel is regression evidence, not a source reference. Whole-frame remainder
proof needs a full-frame baseline; a matching local boundary is narrower proof.

If the remainder still differs, or input/bypass evidence is missing, attribution
is unproven. Do not conclude "history cannot be recovered" merely because a
bypass looks better. Matching remainder plus isolated discrepancy narrows the
suspect; inspect the concrete binding/algorithm/history cause before declaring
a portability limit. Restore all effect switches and debug overrides, then
verify actual final output. Do not leave a diagnostic bypass in the delivery.

## History, diminishing returns and user checkpoints

Trace prior writers and later history-update EIDs, including compute and
multi-pass dependencies. A missing previous captured image is not proof that
runtime history cannot be generated. Test warmup, reprojection, rejection,
disocclusion, cut/resize resets, previous matrices and sampling conventions.

Keep hypotheses, actual changed inputs/source, measured regional/module errors
and the best candidate. After three distinct no-improvement attempts or an
agreed budget, reassess. Also use a finite total checkpoint (default 12 distinct
measured attempts per effect/window) even if every iteration improves slightly.
`review_unity_restoration_handoff` reports both checkpoints and can reuse the
localization report as `diagnosis`. Counts alone never trigger a user question
or stop work. Internally change strategy and pursue feasible alternative reads,
text/source analysis, instrumentation, data reconstruction and direct Unity
repairs; improving work continues without a user budget extension.

At a checkpoint retain the best result, what is isolated, remaining spatial/
temporal discrepancy and the next concrete hypothesis; continue autonomously.
Only when supported recovery cannot make further meaningful progress or actual
external input/access is indispensable, explain the tested alternatives and
ask about that real limit. If attribution is established, the user may accept
the quantified residual with the effect retained or choose further work. If
attribution is not established, explain the unknowns, not a guessed cause.
Do not offer dropping the effect as the normal resolution. Preserve prior work
and the ledger; internal strategy windows may use `budgetWindowId` without
deleting history. Record `resolutionAssessment` with `status`,
`remainingFeasibleRoutes`, `attemptedAlternatives`, `reason` and actual
`evidencePaths`. An untried feasible route takes precedence over an exhaustion
claim. A real choice uses saved results and
`record_task_review(..., decision_request=...)` and ends automatic continuation.

## Scene camera and light response

Both Scene and Game execute the required functions with independent screen and
history resources. A fixed captured image must not substitute for a live effect.
Orbit AND translate Scene's camera; change live light direction and color/
intensity; perform move/settle/return; and check wide/tall/return window extents.
Repaint Scene while Game is unfocused/paused. Confirm geometry, parallax,
specular response, shadow motion/coverage and postprocessing behave as expected.
Hold poses afterwards and inspect steady multi-frame output rather than asking
moving images to remain pixel-identical. Dynamic direct lighting does not imply
captured baked world-volume indirect light should relight dynamically; record
and test that boundary explicitly.

`review_unity_view_evidence` accepts experiment scenarios `scene-camera-orbit`,
`scene-camera-translation`, `scene-light-direction`, `scene-light-color-intensity`
and `motion-settle-return` in addition to existing isolation/resize/hold tests.
Each dynamic record supplies:

- `performed`, actual `beforeCapture` / `afterCapture` sidecar records and the
  `outputComparison` layer from comparing those actual final Scene outputs.
- `expectedOutputChange` and `expectationReason` based on the tested module/
  scene: changed outputs during motion are expected, nonfinite outputs are not.
- For lights, `lightingBefore` / `lightingAfter` with actual `direction`, `color`
  and `intensity` from inspected project values; hold the camera for that test.
- For return, `middleCapture` observes a different pose between the matching
  initial/returned poses. Final stable holds provide the post-motion proof.
- `motionSamples` retains 3..16 ordered actual Scene sidecars including both
  endpoints; `motionComparisons` contains each adjacent pair's numerical layer.
  Check nonfinite frames and inspect visible popping/flicker while moving. Two
  good endpoints cannot establish a stable trajectory; expected frame changes
  during motion are not stationary flicker tests.

The review checks camera/render provenance, observed state changes, file hashes,
nonfinite counts and response against annotations. It cannot judge physically
correct moving shadows from a scalar image metric; the Agent must inspect the
actual images/modules and record the result. Restore user test controls and
compare both integrated final views to aligned RDC evidence before completion.
