# Editable Unreal restoration and acceptance

Use this standard for a working stock UE5 project. The installed editor, project
renderer, RHI and enabled plugins determine which techniques are available.
Do not require a particular UE5 minor version or engine source changes.
Probe the project and reuse its authorized editor connection. Resume saved work
from current evidence, reassessing it against this standard even when a prior
turn accepted a different implementation. Reuse validated capture inputs and
preserve the previous accepted state until the replacement has been verified.

## Fidelity objective

The target is a very close restoration of the captured lighting calculations,
appearance, atmosphere and rendering style. Native UE lighting/GI/shadows are
adaptation inputs toward that target. A generic native-lit rendering with
visible face, hair, highlight or backlit differences is an unfinished candidate.
Keep the source-specific portable algorithms, including their material masks
and artistic shaping, while adapting the engine-dependent inputs. Preserve
each user requirement in the task review. Reassess earlier complete claims
when current regional images or runtime behavior contradict them.

## Establish the baseline before adapting indirect light

Proceed through source-algorithm restoration, indirect-light adaptation and
post-processing in that order. Resume from validated assets and identify the
first incomplete stage. Keep the later-stage work already created, but judge
each stage from its own outputs. Partial function checks or improvements to a
shadow receiver cannot establish a complete direct-light baseline for the
character.

1. Restore the source's portable surface and direct-light effects in editable
   material functions. Separate diffuse shaping, highlights, rim and other
   observed effects, and compare their scene-linear HDR outputs before
   post-processing and without indirect diffuse. First verify the calculations
   with matched inputs, then verify the actual scene using UE light actors,
   simultaneous lights and live shadow inputs. Distinguish a portable shadow
   sampling calculation from the native shadow producer that supplies it. If
   indirect light participates inside a source Ramp, tint or compensation
   function, verify that function with a matched fixed indirect input as well
   as the explicitly isolated direct output. Preserve the source's combination
   semantics when moving to the next stage.
2. Hold the verified direct-effect baseline stable and compare indirect
   diffuse separately. Use UE's suitable GI and tune its strength, color,
   spatial distribution, occlusion and dark-side lift toward the corresponding
   source contribution. Retain separate direct, indirect and combined outputs
   so a GI improvement cannot conceal a direct-effect regression. Identify a
   source intermediate, a capture-derived recomputation or a controlled
   diagnostic accurately. A difference obtained by disabling a light can also
   change its GI and does not by itself isolate direct light. Account for
   temporal settling and exposure when isolating native contributions.
3. Once the scene-linear lighting layers are established, add and compare
   post-process stages in their source order. Reuse validated post-process
   work. Inspect the final combined image and repeat the relevant regional,
   multi-view, multi-frame, backlit and stability checks on the saved result.

Establish a working material-output and native-GI interface on a representative
material before expanding that route. Base Color enters the selected shading
model's lighting calculation. Feeding an already-lit color into it can apply
direct lighting again, and Roughness=1 with Metallic=0 does not remove diffuse
lighting. Verify how direct light is suppressed or replaced while requested
native indirect light still reaches the result. An Emissive route needs checks
for its contribution to Lumen lighting as well as the received indirect light.
View-dependent source shading also needs an appropriate, verified Lumen
material representation where applicable. Select the route from the installed
version's actual capabilities and measured outputs, without requiring one
output pin or a particular shading model in advance.

Record which stage is established, the remaining regional differences and the
next unverified dependency. Reuse passing evidence and rerun affected checks
after a change. Do not use final-image GI or color adjustments to compensate
for an unresolved portable direct-light calculation.

## Plan by observed visual contribution

For each relevant Draw or pass, establish what it contributes, its producer
EIDs, observed inputs and a way to check it. Separate surface/texture decoding,
diffuse, specular, rim, outlines, shadows, environment/GI, transparency or
special effects, and each meaningful post-process stage as appropriate. Use
shared functions for genuinely shared effects. Record which calculations can
be ported and tested with the same inputs, which are represented by native UE
features, and which need a project-local real-time pass. Inspect actual shader,
mesh attributes, bindings, constants and resource versions before choosing a
route. Verify strict same-input numeric checks where portable and assess
measured appearance and dynamic behavior for native substitutions. A candidate
that compiles or matches one static image still needs runtime validation.

## Native lighting and generated resources

Use UE's light actors and the selected project's suitable direct-light,
shadow, indirect-light and environment systems. Calibrate their contribution
toward the captured result rather than accepting a general-purpose BRDF's
appearance as the unavoidable final style. For example, compare the source
indirect-light effect's color, distribution, occlusion and intensity with the
UE result and adjust relevant engine and receiving-material controls. Separate
direct, GI and reflection responses in actual outputs so a change in one is
not mistaken for the others. Check simultaneous lights and the source's
per-material shadow policies.

The material/shading route remains an Agent decision based on the installed
version and evidence. Supported native toon/shading features and hybrid
materials or passes are eligible. Source diffuse shaping, highlight/rim
algorithms and artistic masks can be computed in editable node functions and
connected to Base Color or other appropriate outputs, with measured engine
lighting and GI adapters. Test Roughness/Metallic/Specular controls when an
authored highlight needs the native highlight suppressed. Roughness, Metallic
and Specular control different terms; record which direct/indirect/specular
terms are actually retained, replaced or suppressed, and test the result.
Roughness=1 and Metallic=0 alone do not prove full direct-light suppression
or preservation of the desired GI response. An Emissive or Unlit path requires
an explicit measured way for any requested native GI/shadow contribution to
reach the final image. Avoid doubled direct/specular/indirect terms.

Preserve close same-input agreement for portable source calculations. Where
native light, shadow or GI behavior differs, investigate available shading
models, surface controls, light/channel/receiver settings and supported
material/pass interfaces, then measure the adapted result. For shadows,
consider the installed renderer's supported VSM, shadow map, ray-traced or
other suitable native paths. If a stylized calculation requires sampling,
establish a supported live resource/pass and its sample convention; material
access to native shadow internals is version- and domain-dependent. Do not
assume that a Custom expression exposes an engine shadow atlas. Use a
project-local live producer or another measured UE substitute when necessary.
Record why the selected route preserves the intended visible effect.

Track every previous-pass input from the producer to the consumer. RDC shadow
depth, scene color/depth/normal, stencil/masks, temporal history, lighting
atlases and other frame-produced RTs may be kept as comparison evidence
outside the final dependency graph. Never ship these as runtime textures,
including as a capture-pose shortcut, first-frame seed or fallback. Static
material textures, authored LUTs and true static environment inputs are
eligible after their role and sampling are established from bindings. Prefer
native shadow/GI resources. For distinct effects requiring a buffer not
provided by the renderer, generate it in the selected UE project every time
its valid inputs change. Choose the least invasive supported interface, such
as SceneTexture/Custom Depth and Stencil, User Scene Textures for a chain of
post-process materials, SceneCapture/render targets, or a scoped project
plugin with a supported rendering extension point when required. Check actual
material-domain/stage support, buffer format/precision, frame/view ownership,
producer order, sample space and lifetime. Distinguish viewport UV from
allocated-buffer UV, and invalidate or initialize history on cuts and resizes.
Capture-based fixtures may verify an isolated function without becoming part
of a delivered material, Blueprint, map, or plugin dependency.

Match scene-linear layers before tuning post-processing. Prefer engine
post-process and temporal controls where the measured result is suitable,
and implement only source-specific behavior that needs its own graph/pass.
Verify actual output with the supported editor and gameplay views. One view's
image or screen-space/history RT must not stand in for the other.

## Editable material and Blueprint graphs

The material graph is the implementation. Expose the effect algorithms as
native expression networks grouped in named Material Functions with typed,
named inputs and outputs and meaningful defaults. Prefer engine expressions
and existing applicable functions. Keep the parent material's connections
from textures/parameters through functional stages to actual material outputs
readable. Separate reusable math, material-specific decoding, style controls
and engine interface adapters where that improves inspection and reuse.
Retain reference shader code as evidence. Full captured shader bodies, copied
shader families, generated include wrappers and one opaque Custom expression
per feature are not deliverable implementations. If a specific operation has
no viable node equivalent on the installed version, limit Custom HLSL to
that small operation in a named function. Explain its reason, inputs, output,
space and precision, then verify it. Record every exception. Keep ordinary
math, composition, masks and parameter wiring in the graph.

Use a compact, consistent left-to-right signal flow, aligned columns and
comment boxes that follow functional boundaries. Size spacing to the actual
node and pin geometry. Keep related operations close enough to read together
at a useful editor zoom; large fixed gaps, excessively long rows and repeated
long wires need rearrangement or functional grouping. Use alignment,
distribution, reroutes/named reroutes and material functions where they improve
readability. Review both the overview and zoomed functional groups in the
saved graph. A non-overlap statistic alone cannot establish compactness or
readability. Keep boxes and nodes
clear of unrelated wires and avoid stacks of nodes at one position. Use
reroutes or clearly named reroutes where they materially improve wiring.
Name and explain function I/O, non-obvious equations and coordinate/units
conversions. Comments should tell an artist which feature a group implements,
what drives it and what the output feeds. Use effect-specific explanations
for the actual group; repeated generic labels do not explain a normal decoder,
highlight calculation or shadow adapter. Keep comment box bounds fitted to
their contained nodes and validate the saved editor layout. Apply the same layout, naming and
comment principles to authored Blueprint logic. Save the organized asset
positions and comments in the editor asset. Inspect the reopened graph at an
overview and readable zoom for each distinct parent material, representative
functions, and any authored effect Blueprint; keep editor screenshots or an
equivalent graph view that shows actual placement and wires. A node list,
a source file, or valid connections without a readable layout cannot prove
graph quality.

Use named Material Instances with useful scalar/vector/texture or switch
controls grouped by effect. Give controls understandable names, defaults
and valid ranges where supported. Verify representative instance edits change
the intended result without editing graph code, and restore test settings.
Organize the output under the project's own restoration root. Mirror the
Unity delivery's asset categories using UE conventions, such as Meshes,
Textures, Materials with parents/Functions/Instances, Blueprints, and Maps
as applicable. Reuse existing assets where their meaning and bindings match.
Keep reference captures, scripts used only for validation, failed experiments
and duplicate packages outside delivered Content. Record the final map path,
material parents, runtime dependencies and where to adjust each major effect.

## Regional and runtime acceptance

- Align the RDC baseline and UE view for camera/projection, render dimensions,
  pass meaning, buffer encoding and temporal state. Distinguish actual RDC
  buffers, capture-derived same-input recomputation and runtime diagnostics.
  Use true intermediate outputs or controlled effect differences. A Base Color
  or World Normal GBuffer is an input/geometry diagnostic rather than a
  diffuse/specular/indirect output. An on/off image difference demonstrates
  contribution under those controls without proving source equivalence.
- Define the relevant visible object/material regions before scoring, using
  a verified object/EID mask or recorded crop coordinates and exclusions.
  Break the character into parts such as face and eyes, hair, clothing,
  silhouette, and shadow receiver as appropriate. Keep backgrounds separate
  and split large regions or rank smaller tiles within them. Report each
  region's pixel coverage, mean, tail error (P95/P99 as appropriate), maximum,
  fraction over the chosen threshold, and coordinates or bounds of worst
  pixels/tiles. Review corresponding reference, target and difference crops.
  For silhouettes or masks, account for missing/extra coverage independently.
  Attribute visible outliers to an input, material or pass where evidence
  supports it, repair and recheck, and retain the remaining mismatch as an
  unmet fidelity item. Native BRDF/GI differences identify the adaptation
  work; citing that difference alone does not discharge the requested close
  appearance. Distinguish implementation/probe completion from effect
  acceptance and continue feasible calibration or alternative supported
  routes when large visible regional differences remain. Use
  layer-appropriate units and preserve float data for HDR/depth. Full-frame
  images provide context and their average never substitutes for a regional
  pass. No automatically widened tolerance or image rescale to hide defects.
- Validate the actual final camera and an independent editor view or live
  gameplay camera. Save a capture-aligned baseline, at least three distinct
  held angles, near and far framing, and return to the baseline. Sample at
  least eight consecutive steady frames after measured warmup in held tests.
  Retain frames during motion, not just endpoints. Include front-lit, side-lit
  and backlit conditions, with explicit inspection of the shadowed/back-facing
  surfaces, face, hair, clothing and shadow receivers. Compare the source's
  corresponding lighting contribution or same-input function where available;
  label a new view/light configuration as a dynamic-response observation when
  no aligned source reference exists. Check dark-side GI color and lift,
  shadow transition and outline behavior. Include light movement,
  rotation or intensity/color changes as applicable, simultaneous lights,
  and settle/return. Check changed viewport dimensions and aspect when the
  effect samples screen buffers. Observe invalid values, stale/frozen images,
  temporal flicker, ghosting, incorrect depth, shifted shadow or silhouette,
  including small bright/dark outliers and flicker localized to a few pixels,
  and restore camera/lights/project overrides. Alternate views without a
  matching capture establish expected behavior and temporal stability rather
  than a source-pixel claim. Record poses, light values, view, projection,
  dimensions, frame order and enough pass/region evidence to reproduce results.
- Check the saved project, not only diagnostic copies. Reopen the final map
  in the selected interactive UE editor, verify no missing dependencies or
  shader fallback, and inspect the native lighting, material graphs, instance
  controls and directory structure. Trace material/Blueprint/plugin runtime
  dependencies to rule out captured screen/history/shadow RTs and abandoned
  code. Preserve user edits and isolate other test projects or app instances.
  Keep diagnostics outside the accepted runtime assets. Record each requirement,
  actual evidence, exceptions and unresolved differences in the task review.

The Agent chooses the implementation, tools and test route. Managed import,
render and layer-review receipts are useful where they match the requested
scope. Use verified project-local tools and file evidence for graphs, sequences,
per-object comparisons and custom pass RTs that a managed recipe cannot
represent. A tool's successful run does not substitute for the above review.

Official Epic references. Consult the version selector or local installed
editor when checking feature support.

- [Organizing a Material Graph](https://dev.epicgames.com/documentation/unreal-engine/organizing-a-material-graph-in-unreal-engine?lang=zh-CN)
- [Creating and Using Material Functions](https://dev.epicgames.com/documentation/unreal-engine/creating-and-using-material-functions-in-unreal-engine)
- [Movable Light Mobility](https://dev.epicgames.com/documentation/unreal-engine/movable-light-mobility-in-unreal-engine)
- [Lumen Global Illumination and Reflections](https://dev.epicgames.com/documentation/unreal-engine/lumen-global-illumination-and-reflections-in-unreal-engine)
- [Post Process Materials](https://dev.epicgames.com/documentation/unreal-engine/post-process-materials-in-unreal-engine)
- [Post Process Material User Scene Textures](https://dev.epicgames.com/documentation/unreal-engine/post-process-material-user-scene-textures-in-unreal-engine)

## Apply the current user requirements

Choose methods autonomously from this standard and observed capabilities.
Read the current standard when resuming, compare it with the user's entire
requested scope and retain evidence for each item. Keep reusable tool or
connection repairs separate from restoration authoring. A change to a tool
does not provide new evidence that the rendered effect passed. Report
unsupported access or measured residuals precisely and preserve progress.
Do not invent user-approved tolerances, remove required effects to improve
a score, or replace the close-style objective with a general native-lit demo.
