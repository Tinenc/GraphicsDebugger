---
name: renderdoc-resource-export
description: Export RenderDoc draws or resources and restore captured effects in Unity or stock Unreal Engine, or deliver assets to Blender.
metadata:
  replaylab-category: resource-export
  replaylab-version: "37"
---

# Export captured resources

The Agent chooses how to inspect and export the user's selected draw or
resource set. Keep the capture, EID, binding, source bytes and requested target
linked in the result; do not silently expand the resource or output scope.

The source engine and destination engine are independent. Respect an existing
target pipeline; Unity URP is the default for an unspecified new target. Use the configured
source-engine reader for matching assets when available, and capture resources
otherwise. Missing readers or original names do not block capture-based export.

## Editable Unreal delivery

For functional restoration or reassessment, read
[unreal-restoration.md](references/unreal-restoration.md) before changing the
project. It governs native lighting, editable graphs, live RT dependencies,
regional comparisons and multi-view/frame delivery. Use the current standard
when continuing existing work. An asset-only request retains its narrower scope.

Use the target project's associated Editor. Call `start_unreal_project_connection`
and poll `get_unreal_integration_job`. For an editable stock UE5 session, select
`interactive=true` so the project-owned Python channel remains available while
the Editor window is open. Subsequent tasks and Bread restarts can reconnect
through the same tools without desktop input. Batch mode remains available for
closed-project jobs. Preserve unsaved edits in any pre-existing Editor before
switching its connection. This automatically sets up and verifies
the available project provider. Discover its tools with
`call_unreal_project_tool(tool_name="list_toolsets")` and
`call_unreal_project_tool(tool_name="describe_toolset")`. Epic MCP uses the
installed native plugin. On older stock UE5, Bread starts the installed Editor
with its Python plugins as needed for each job. Use `BreadEditorTools.run_python`
when advertised for project-local material, Blueprint, asset and
level edits, including bespoke dependencies that the standard recipe cannot
express. Set `result` to JSON-compatible values and explicitly save and verify
assets. Poll queued edits. A successful batch connection leaves no running
Editor to close. Only investigate an open Editor when a job reports an actual
conflict, and preserve its unsaved edits.

For each needed Draw, inspect its actual vertex inputs, capture and finalize a
`gltf` package. Reuse existing export operations and their verified evidence.
For a shared level, pass the first operation to
`import_resource_export_to_unreal` and use `recipe.additionalMeshes` for the
other validated Draws. Specify each material and transform. Use
`get_unreal_restoration_recipe_schema` for the contract and record remaining
source gaps. Use `render_unreal_restoration` and `review_unreal_restoration_layers`
for managed observations where their recipe covers the effect. Use project-local
observations where a recipe cannot express the required layer or regional test.

When stock templates do not cover a captured dependency, inspect its raw
evidence and try a project-local adapter using an available authorized
execution route. If native shell launch fails, look up the connected
`bread-portable` server's `bread-portable__bread_execute_code` tool, which is available when
local execution is enabled. Inspect its current contract, then use it for
relevant owned files and project-local Python/JS work. If an operation truly
needs the desktop, discover `bread-portable__bread_windows`; match the snapshot's
window ID and PID before sending input. Preserve unsaved work in other Editors. Keep unsupported shader inputs and pass differences explicit
until recovered and measured.

## Editable Unity delivery

For a request to implement a named rendering function, calculation, feature or
combination in an engine, resolve its destination project and rendering pipeline.
Discover the enabled Editor MCP for that engine through the attached tool directory.
For Unity, use the supported project-scoped setup/recovery below when needed. For
Unreal, use the configured Editor's material graph, Blueprint and shader authoring
tools; an asset extractor is evidence, not an Editor connection. Inspect existing
interfaces and author only the requested asset or integration. Compile/save and
verify the actual requested behavior in the destination. A small reusable function
does not require restoring a whole scene. Distinguish authoring from explanation.

Use chat RT comparisons, source links and mesh illustrations when they help explain
the transfer. They do not replace the requested editable Unity project or its actual
final-view validation. Follow the event Skill's visual-explanations.md for presentation.

Before authoring or calibrating Unity restoration, read
[unity-implementation-guidance.md](references/unity-implementation-guidance.md).
It retains the implementation and final-view acceptance requirements, including
independent Scene/Game resources, Free Aspect resizing and stable history.
Before final delivery, follow
[unity-final-handoff.md](references/unity-final-handoff.md): save a normally
openable `.unity`, remove generated intermediate files/builders/validation images,
retain only final assets and necessary runtime/inspector code, keep the directory
tree simple, and recheck actual output after reopening without a setup command.
It also covers autonomous Unity MCP setup and recovery: prepare/reconnect the
selected project before asking for manual UI actions, and inspect uncertain
write outcomes before repeating them.

Default to an editable real-time scene unless the user specifically requests a
fixed capture replay. Deliver FBX model assets, TGA (LDR/data) or EXR (HDR/needed
precision) textures, meaningful material controls, Shader sources and prefabs
with normal MeshRenderer/SkinnedMeshRenderer rendering. Keep captured binary
data in Bread storage as provenance/fallback inputs; retain only data required
for normal rendering in the Unity tree. `bread_prepare_fbx_exporter` can install the
official exporter on request; `bread_export_portable_assets` exports an actual
regular-renderer hierarchy without judging fidelity. A procedural-only draw needs
an explicit vertex/skin layout and mesh conversion, not a renamed data blob.
Preserve rig information when available; a baked current pose is acceptable when
rig information is absent, but retain free camera movement and live lighting.

Initialize camera pose/FOV, light direction/color and shadow settings from capture
evidence. Drive live camera transforms and light direction/color from Unity.
Preserve captured lighting, indirect-light resources/functions and shadow
calculations where portable; engine interfaces are not automatic replacements
for those algorithms. Trace screen/history producer EIDs, including compute,
and implement their live dependencies. Only after investigating a concrete
portability limit consider a measured native or spatial approximation, retaining
the user's fidelity target. Expose analyzed effect controls on materials;
scripts supply system parameters. Use Volume settings with the target pipeline's
real pass extension points for postprocessing. `bread_inspect_editable_delivery`
reports ordinary assets, cameras/lights/Volumes and capture-runner indicators.

Render the actual final camera output. A direct effect RT is a separate pass
observation. `bread_prepare_urp_presentation` provides an optional URP 14+ color
handle adapter; avoid duplicate camera runners and inspect final encoding,
orientation and active Volume settings. Use the interactive Unity Editor;
reuse its connection, and do not substitute a batch job for the editing window.

Editable delivery includes independently rendered Scene and Game views with the
same requested effects. Check their effective RendererData, execution filters
and Scene lighting/FX switches early. Keep view-dependent RTs, motion, jitter
and history separate; never show Game's frame or screen mask through Scene.
Use the methods and actual-output acceptance in
[unity-dual-view.md](references/unity-dual-view.md). Module-only or old-Unity
regression checks do not discharge final original-RDC comparisons for both views.
Scene resizing and Game Free Aspect resizing, multi-frame/angle stability, bounded unresolved-effect work,
user decisions and final resource cleanup follow
[unity-final-handoff.md](references/unity-final-handoff.md). These are acceptance
observations and Agent decisions, never managed import locks.
Efficient spatial/pass/input localization, reversible attribution experiments,
retained-effect residual decisions and live Scene camera/light tests follow
[unity-difference-localization.md](references/unity-difference-localization.md).
Bypassing an effect is only a diagnostic intervention; do not offer dropping it
as the restoration outcome or attribute a difference from that experiment alone.

Use `compare_unity_effect_layers` for ported-core, native-engine, post-process and
full-frame comparisons. Isolate unchanged algorithm inputs for strict small-error
checks. Assess any justified engine shadows/GI approximations separately, adjusting actual
lights/ambient/Volume controls rather than evidence pixels. Use a shared fixed
RT/input fixture per post pass to isolate its error from upstream error. Review
the integrated final camera image too. Tools report facts; the Agent chooses
judgment and next steps, and reuses successful unchanged checks.
Prefer matched original Mesh/Texture assets only after checking the draw's LOD,
submesh, pose, texture view and color-space use; current EID parameters override
material defaults. Preserve unresolved dynamic resources and pass dependencies
as explicit recovery inputs, then implement justified reconstruction or target
interfaces without silently substituting unrelated assets.
For functional restoration, prepare the selected effect in Bread first: reuse
matching organized sources and semantic names, or decompile, organize and review
the calculations, then compile and check ABI and captured outputs where supported.
This catches decompiler/refactoring errors before porting reusable functions and
CBuffer meanings. Reuse current proof for unchanged bytes/includes; do not redo
successful work. Permanent replay apply/live edit is not a port prerequisite.
When an application/tool limitation prevents this preferred preparation, retain
the best source/draft, passed checks and exact missing/failed-check diagnostics,
then continue organization, compilation, debugging and restoration in Unity.
Use `get_unity_shader_transfer_sources(organization_ids=...)` for an explicitly
selected retained proposal without promoting or applying it. Do not silently
downgrade already validated sources to unvalidated references or skip feasible
Bread preparation. Known compiler/numeric defects remain defects to repair in
the practical environment, not successful validation. AI text review can explain
mappings but cannot prove numerical equivalence. Compile and inspect the derived
implementation in Unity, then compare actual images. Reuse unchanged
geometry/textures. Only stages executed by the selected EID belong to its port;
an idle compute/graphics pipeline binding is not an effect dependency.

`inspect_resource_export` shows capture evidence; `prepare_resource_export`
returns a candidate and spec (`get_resource_export_spec_template` also reads it).
`analyze_vertex_input_roles` suggests channel roles from Shader use; confirm the
reflected semantic/location against the actual layout before choosing
`attributeIndex`. Follow [vertex channel transfer](references/vertex-channels.md)
for packed/custom Unity inputs. `export_draw_resources` exports a known mapping.
`finalize_resource_export`, `get_resource_export_status` and
`validate_resource_export` provide publication/validation receipts. Choose the
useful route and retain successful exports when another part fails.
`get_resource_export_payload` exposes the exact captured files and manifest
before finalization. Use them directly in Unity if glTF conversion, organized
source checks or managed binding cannot be completed; none locks raw data access.

For an explicitly asset-only import to a supplied Unity project, call
`import_resource_export_to_unity(asset_only=true)` with the completed compatible
package. It prepares/connects Unity and imports through one background job.
Functional restoration uses the task and binding route below. Follow
`get_unity_integration_job`; avoid duplicate connection/install jobs.
Use `start_unity_project_connection` when the requested work needs an Editor
connection without an import, then `call_unity_project_tool` for needed Editor
operations. Reuse a matching live session and avoid competing Editor instances
or parallel writes to the same project. Discover exact contracts only if absent.
Resolve uncertain timeouts from the existing job ID instead of reinstalling.
Keep RenderDoc open; use supported recovery before reporting a setup blocker.

For functional restoration, follow [unity-restoration.md](references/unity-restoration.md):
inspect the active pipeline, preserve values, implement draw/pass dependencies
and calibrate rendered images. The managed importer preserves an existing target
pipeline and accepts derived implementation assets; multi-pass effects also use
the connected project tools to configure the pipeline's actual extension points.
For fidelity differences, source lighting/volume preservation, live screen/history
dependencies, shadow quality and module debugging, use
[unity-fidelity-debugging.md](references/unity-fidelity-debugging.md).

The Agent chooses the workflow and judges the user's acceptance goal. Tools
provide data, execution methods, diagnostics and evidence annotations. Do not
treat restoration ledgers, session/version IDs, Shader UI publication, recovery
suggestions or missing optional validation as an execution lock. Read the
selected EID's actual bindings. Reuse successful exports and validation for
unchanged inputs; rerun only a changed-input check or a diagnosed failure after
a relevant repair. Reopening the same RDC is not a change to its source bytes.

For ranges, optionally call `plan_unity_restoration_sequence` with inclusive
`event_ranges`, then `get_unity_restoration_step` for each actual draw/dispatch.
Inspect/import/configure/render/adjust incrementally in the shared scene before
moving on. Order is advisory: follow real resource/depth/blending dependencies.
Use `annotate_unity_restoration_step` to retain progress, output paths and missing
checks; `ready-for-next` and `deferred` are Agent notes, not numerical proof.
For postprocessing, inspect resource uses and actual prior writes (including
outside the range), subresources, formats, sizes and load/store behavior. Build
the corresponding ordered URP RenderFeature passes; adjacency is not causation.

Functional EID-to-Unity work can use the durable `begin_unity_restoration` task,
with an explicit captured output and a fixed visual-fidelity objective (or strict
pixel matching when requested). Reuse saved reference
workspace sources through `get_unity_shader_transfer_sources`; include those
identities in `shader.organizedSources` before finalizing the export. Use the
decoded, paged `constantBufferFields` and existing merged semantic names from
`get_resource_export_spec_template`, not guessed parameter values. Derived
Shader/includes/compute/scripts belong in `unity.assets`; keep the destination's
actual pipeline. `bind_unity_restoration` verifies selected reference identities and starts
the import. Follow its job, then `check_unity_restoration`, repair concrete gaps,
`render_unity_restoration`, and `review_unity_restoration`. Source, scene or
parameter changes require current checks and a new render; unchanged exports
remain reusable. Managed receipts are advisory and do not override an evidence-based Agent
assessment using direct project operations. Raw asset-only export does not require this functional restoration task.
An empty workspace source list can use the package's native Shader/reflection
evidence directly as a fallback when Bread preparation is unavailable. Retain
per-stage preparation outcomes and unsupported replay checks as explicit limits,
not a reason to block the port. Repair concrete data/specification defects;
direct Unity preparation and rendering remain available before managed binding.
Do not rewrite the effect as compute merely to satisfy a recovery suggestion.

Tool or platform limitations are recovery inputs. Use
`plan_unity_restoration_recovery` to choose another executable route and
`apply_unity_recovery_assets` to author real ComputeShader, raster Shader, C# or
generated input assets when fixed tools are insufficient. Derive missing history
or layouts from available evidence, build and bind those inputs, and record the
explicit inference and runtime proof with `record_unity_reconstruction`. Adapt an
actual changed pass graph with `adopt_unity_restoration_render_plan`. Preserve the
core algorithm and make its effect visible. Keep the user's fidelity target;
unresolved requested differences remain findings. An inference is not a recovered capture fact.
Read the recovery details in the reference before using these routes.

Keep the target-pipeline Unity Shader as a derived asset: establish coordinate spaces before
replacing matrices, preserve original material algorithms, and choose Unity
light/shadow inputs or captured indirect-light data explicitly. A successful
import or Shader compile does not prove equivalent pixels. Validate target
rendering against the appropriate captured pass/reference and report missing
dependencies or approximations. Validation-only requests reuse the import;
they do not automatically restart asset reading and export.

For platform adaptation, separate mesh UVs, image rows and render-target screen
UVs. Do not infer a global Y flip from the source engine/API or repeat a baked
conversion. Use bound-view sampling evidence for color space and preserve raw
normal/data channels. Apply the relevant [target mapping rules](references/target-profiles.md)
when converting to Unity, without changing the capture-compatible Shader.

Preserve source-to-output mappings, texture view and color-space behavior,
sampler settings, CBuffer values and dependencies relevant to the chosen
target. A normal transform uses inverse-transpose. glTF/GLB, a material
preview or translated Shader is a derived representation, not the captured
GPU state. Record approximations and unsupported source features.

In the main Agent's `record_task_review`, assess requested resource coverage,
destination and target compatibility from actual artifacts, hashes and validation
receipts. File creation alone proves no compatibility. Repair missing outputs;
review changed outputs and dependencies while retaining successful exports.
Try feasible project-local adapters and input reconstruction before treating a
required feature as unavailable. Report a concrete remaining external dependency
only after those authorized alternatives have been examined and attempted; a
missing built-in tool alone is not such a dependency.
Read the relevant reference only when needed:
`references/source-evidence.md` for capture fields,
`references/target-profiles.md` for coordinate and target mapping,
`references/package-and-validation.md` for managed artifacts.
