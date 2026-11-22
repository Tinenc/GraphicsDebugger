# Resource export toolchain

Bread resource export uses RenderDoc as the only capture-reading layer and
publishes a versioned evidence package. The current fixed portable scene is a
glTF 2.0 JSON document plus its binary buffer. GLB and FBX are optional future
provider derivatives because neither container can represent every captured
vertex input, descriptor view, sampler, CBuffer, Shader, or pass dependency.

## On-demand Unreal restoration

The Unreal destination reuses the same finalized capture evidence and glTF.
See [Unreal restoration](../mcp_host/docs/UNREAL_RESTORATION.md) for the
editable material/function/instance recipe, installed MCP or persistent stock
Editor connection, compile preflight, regional HDR/tail statistics and continuous
frame evidence review. These interfaces do not independently certify a restored effect.

## On-demand Unity restoration

The preferred workflow prepares the effect in Bread first: decompile, organize
and review source, compile, check ABI and compare captured outputs where supported.
Reuse unchanged validated source, functions and CBuffer meanings. Permanent replay
apply/live edit is not required. `get_unity_shader_transfer_sources` prefers
current proof and reports exact missing checks; it does not execute preparation.
Its `require_replay_validation=false` default permits fallback on concrete
application/tool limitations rather than instructing the Agent to skip preparation.
Explicit `organization_ids` can select retained proposals without promotion.
An edited proposal can be selected with its observed `draft_sha256`, without
rewriting historical session metadata. Source reads use the artifact reader,
independent of compiler executable availability or current application access.
Carry passed checks and unresolved diagnostics, then finish source preparation
and validation in Unity. Reference records retain `replayValidated=false`; known
failed checks are not passed. Native program/reflection evidence can be used
when readable source is unavailable. Source/hash/stage/API/entry/include integrity
and Unity execution/visual checks remain required. Bread proof does not certify
later engine-interface substitutions, coordinate conversions or scene settings.

Export stages follow the selected action: raster, compute, mesh or ray dispatch.
Idle bindings from another pipeline are not executed stages of that EID. A
resource produced by an earlier dispatch remains a dependency to trace through
its actual producer. Before a package is finalized and bound, recovery resolves
data/specification gaps rather than diagnosing nonexistent render images or
selecting an unrelated compute rewrite.

Source engines and output engines are independent. Preserve the destination's
active Built-in, URP, HDRP or custom pipeline; Unity URP is the default only for
an unspecified new target. Asset readers enrich the evidence when available; Shader organization and
capture export remain usable without a reader. `get_draw_workflow_plan` is an
optional advisory plan, not an execution gate. Its five actions are
`organize_shader`, `resolve_names`, `export_unity`, `validate_unity` and
`update_unity_shader`. Reuse requires verified artifact identity; selecting a
plan does not run a scan, export, install or compile.

Functional `export_unity` planning now includes a reusable captured-Shader
organization result before translation. Raw asset export remains available
without this functional workflow. The target pipeline participates in translation
and import reuse keys, so a URP result is not reused for HDRP by mistake.
`inspect_unity_project` distinguishes installed packages from the active pipeline;
the connected `bread_inspect_rendering` Editor command reads the actual pipeline,
graphics API, color space, UV/depth conventions and cameras. An active custom or
HDRP pipeline is preserved by URP setup instead of silently replaced.

Two read-only tools support Agent-driven restoration and calibration:

- `validate_unity_render_plan` checks versioned texture dependencies and orders a
  proposed effect graph. It reports missing producers, duplicate writers and
  cycles without prescribing effects, EIDs or a pass count. The Agent implements
  the checked graph with the installed pipeline's actual APIs through project
  tools. The importer supports geometry draws and explicitly geometry-free
  effect requests whose compute, fullscreen or custom C# passes actually execute.
- `compare_unity_render_images` compares SHA-256-bound PNG display exports at full
  resolution, reporting per-channel errors and the changed region. It never
  resizes, flips, changes exposure or interprets HDR as RGB8. Matching images are
  evidence for the chosen comparison, not proof of Shader numerical equivalence
  or unobserved frames. Functional tasks use `render_unity_restoration` to bind
  both reference and Unity output to the same current task. The default visual
  objective checks large errors, local structure, missing edges and visible
  effect coverage; explicit strict pixel comparison remains available.

Functional tasks use `begin_unity_restoration`, `bind_unity_restoration`,
`check_unity_restoration`, `render_unity_restoration` and
`review_unity_restoration` for a durable repair cycle. Source and scene changes
invalidate prior acceptance. CBuffer values are decoded from capture bytes,
including existing renamed and merged fields, then reconciled with actual Unity
bindings. Review covers source readability and the core algorithm as well as
geometry, material, light, render state, scene dependencies and visible output.

`plan_unity_restoration_recovery` turns missing platform support, history or
provider tools into executable alternatives. `apply_unity_recovery_assets`
writes versioned Shader/ComputeShader/C#/input files under
`Assets/BreadRecovery/<restorationId>` and refreshes the Editor. The AI attaches
and executes those files, then checks and renders again. It can use a supported
compute path, raster equivalent, CPU producer, Unity interface or version-correct
project adapter. `record_unity_reconstruction` records the exact missing-input
gap, inference rationale and real implementation; an accepted reconstruction
requires fresh runtime and image evidence plus an explicit semantic review.
Known parameter corruption, wrong identities and failed compilation are repaired,
not waived by inference. `adopt_unity_restoration_render_plan` binds an observed
replacement pass graph without relabeling it as captured data.
An obsolete reconstruction can be retired through
`record_unity_reconstruction` with `{id, retire: true, reason}` only when current
checks, comparison and review pass and removing that exception leaves the task
complete. Its evidence remains in the history. For compute scalar/matrix inputs,
`compute-upload` proves the successful typed Set call and exact upload values;
it does not claim a general GPU parameter readback. Completion additionally checks
the actual current pass execution and visible output; GPU buffers retain byte/hash
readback verification.

This recovery model applies across host operating systems and target graphics
APIs. Capability inspection distinguishes the active Editor GPU from configured
player APIs and build modules. Editor validation is not a claim that macOS,
Linux, mobile devices or every graphics driver has been exercised. A target API
or package change selects a new recovery context; a revision counter or changed
diagnostic wording alone does not justify endlessly repeating a failed route.

For a supplied Unity project directory:

1. `inspect_unity_project` reports the exact Editor requirement and connection.
2. `start_unity_project_connection` starts a background installation/connection
   job. `get_unity_integration_job` reads it; cancellation never closes an Editor.
3. For functional restoration, bind the completed package with
   `bind_unity_restoration`; it starts the managed connection/import. For an
   explicitly asset-only request use
   `import_resource_export_to_unity(operation_id, project_path, asset_only=true)`.
   Do not run a second setup job first or repeat unchanged capture extraction.
   Follow the existing job through preparation, import and the Editor receipt.
4. `call_unity_project_tool` reuses the same project-scoped Coplay MCP session for
   focused changes and render validation. A Shader-only revision does not require
   another mesh export or another asset scan.

The managed provider is [CoplayDev/unity-mcp](https://github.com/CoplayDev/unity-mcp),
pinned to v10.3.0 / `aa5fc638d623f56178d50329d9ad8c541a57fe66`. The source archive
is SHA-256 checked; its locked Python server runs in an isolated runtime. Bread
packages the connection/identity helper and fixed capture importer as Editor UPM
packages. Installation uses the user's project directory and a matching installed
Editor, not desktop automation. The connection verifies the project path, version
and nonce before dispatch. Downloads and compilation are background jobs. No step
closes or restarts RenderDoc. Editor licensing, unavailable exact Editor versions,
network failures and an incompatible existing render pipeline remain actionable
results; they do not disable unrelated Shader work.

Requests and receipts live under `Library/Bread/Requests`. Imported assets use
`Assets/BreadImports/<request-hash>` and include a mesh, mapped textures/material,
supplied Unity Shader, prefab and separate preview scene. Source artifact hashes
are checked again by the Editor. Repeated requests reuse checked outputs. The
preview scene does not replace the user's active scene. If Unity has an unsaved
Untitled scene, the importer preserves it and delivers the Prefab with a
`preview_scene_deferred` gap; Unity refuses saving an additive scene in that
situation. The receipt is `partial`; the functional task retains the scene gap
for a project-local recovery and does not repeat unrelated export work.
Existing imported files that have changed are preserved and reported.

`transfer-spec.json` supplies semantic decisions. Texture entries use
`targetProperty`, `colorSpace`, an explicit `sampler` (`filterMode`, `wrapU`,
`wrapV`, `anisoLevel`) and optional `unityNormalMap`. A normal map is not inferred
from a filename. Material CBuffer mappings use the physical capture field identity,
`classification: material`, and a target property; the Host resolves their actual
values and types from captured bytes. System entries remain Unity-managed.
`unity.vertexChannels` can map preserved custom attributes to Unity UV channels;
unknown channels are reported. Images preserve explicit color-space decisions,
use uncompressed import, and do not pretend that a regenerated mip chain is the
captured one. Unsupported views, arrays, cubemaps and missing mappings retain
their source evidence and produce receipt gaps.

The importer uses a supplied `shader.unityShaderSource`. URP/Lit is selected only
for an explicit `target-native-pbr` strategy. Translating the captured algorithm,
object/world/view spaces, lighting/shadow inputs and optional captured indirect
light remains Shader work, guided by the source evidence. Import checks cover
mesh, texture, material, Shader compilation, scene creation and the preserved
active pipeline. A geometry-free effect explicitly marks mesh/material checks
not applicable while still checking its real compute/fullscreen/custom code,
bound inputs, execution and output.
Default preview lighting/camera are explicitly approximate.
`visualEquivalenceVerified` stays false until a separate captured-pass comparison
establishes it. Built-in import receipts are separate from the optional strict
external-validator receipts below.

The integration smoke test used a disposable Unity `6000.0.55f1` project with
URP `17.0.4`, reached through the actual pinned Coplay MCP server. A synthetic
capture went through the production export/finalization path, imported a triangle,
an sRGB PNG, a numeric material color and a custom URP Shader using
`TransformObjectToHClip`, then saved and rendered its preview scene. All six import
checks passed, repeat import reused the receipt, and an edited output was preserved.
This validates the plumbing; it is not evidence of equivalence for a real game draw.

### Platform and sampling adaptation

Mesh UVs, image-row orientation and render-target/screen UVs are separate.
Source engine/API names never trigger an automatic Y flip. A texture mapping
can provide `uvTransform: {scale: [u, v], offset: [u, v], space: "captured-uv"}`;
`space` defaults to `captured-uv`, while `exported-uv` uses the already exported
coordinates. Preparation composes captured-space mappings with the inverse of
the mesh's baked `uvPolicy` and produces `unityUvTransform`. Thus a requested
`flip-v` already baked into the mesh needs no second flip. With no mapping,
Unity keeps the exported UVs. This route does not rewrite image rows. Material
scale/offset must also be consumed by the supplied Shader; setting `_ST` alone
does not verify the rendered orientation.

The request's `platformContract` and each texture's `colorSpaceContract` retain
source evidence and explicit mapping decisions. Bound-view sampling and Shader
use decide color/data treatment; filenames and Unity project color space do not
identify source encoding. Avoid decoding already-linearized image bytes again.
Normal/data channels stay free of gamma conversion; `unityNormalMap` additionally
requires adapting the Shader to Unity's normal packing/decoder. Unity's
[`sRGBTexture` behavior](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/TextureImporter-sRGBTexture.html)
depends on Linear project rendering. An incompatible Gamma project is reported
as a gap, without silently changing its global color-space setting.

Receipts record `unityPlatform` (`graphicsDeviceType`, `activeColorSpace`,
`graphicsUVStartsAtTop`, `usesReversedZBuffer`, `unityVersion`) and
`textureEvidence` with actual imported formats, sampler settings, mip counts and
UV transform evidence. The current importer uses base-level images with mip
generation disabled. It maps `filterMode`, `wrapU`, `wrapV` and `anisoLevel`;
captured mip chains, LOD bias/ranges, border colors, comparison samplers and
Shader-selected sampler differences need explicit preservation or a reported
gap. A regenerated mip chain or a default sampler is not captured equivalence.

URP conversion uses target-native object/camera transforms and screen/depth
helpers with their expected input spaces. Follow Unity's
[platform conventions](https://docs.unity3d.com/6000.0/Documentation/Manual/SL-PlatformDifferences.html)
and [URP depth reconstruction](https://docs.unity3d.com/6000.0/Documentation/Manual/urp/writing-shaders-urp-reconstruct-world-position.html):
`UNITY_UV_STARTS_AT_TOP`, `_ProjectionParams.x`, `UNITY_REVERSED_Z` and
`UNITY_NEAR_CLIP_VALUE` concern the current target rendering path. Avoid
repeating a helper's UV/depth correction or converting an already device-space
projection again. These diagnostics and a compiled Shader leave
`orientationVerified` and `visualEquivalenceVerified` false until the relevant
rendered evidence is compared. Missing fidelity data does not block unrelated
Shader organization or erase successfully imported resources.

## Responsibility split

Fixed Host and bridge code owns capture identity, buffer reads and decoding,
raw fallbacks, DDS extraction, hashing, path containment, coordinate math,
container packing, internal validation, and truthful external-provider status. The Event
analysis child loads `renderdoc-explain-event` to identify producer/consumer EIDs
and pass dependencies. The Shader analysis child loads
`renderdoc-shader-workbench` to reconstruct algorithms and ABI-bound source. The
export child loads `renderdoc-resource-export` to select semantic mappings and
target compromises from that admitted evidence. These are ordinary bounded child
Agents with task-relevant Skills, not persistent domain roles.

No graphics API implies an application's object-space basis. A D3D capture is
not sufficient evidence to flip X. Every conversion therefore carries an
explicit source-to-target matrix, unit scale, UV policy, winding action, normal
inverse-transpose rule, and tangent-handedness rule.

## Provider registry

Legacy validation providers are process-owner configuration, never model-selected
commands. A missing validator is reported as unavailable. The managed Unity
integration above can install its pinned provider on an explicit Unity action.
The Host-managed operation
root may be fixed with `RENDERDOC_ULTRA_RESOURCE_EXPORT_ROOT`; the model can
receive its resolved value for artifact handoff, but cannot choose or override
it. Provider configuration is not inherited by vendor Agent processes.

| Purpose | Preferred provider | Role and boundary |
| --- | --- | --- |
| Capture evidence | [RenderDoc Python API](https://github.com/baldurk/renderdoc) | Mesh/pipeline/resource/Shader reflection and replay data. The official mesh decoder is a reference, not complete support for packed formats. |
| Portable scene | [glTF 2.0](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html) | Canonical exchange scene; `_CUSTOM` attributes and `extras` are used only where valid. Exact capture-only facts stay in the sidecar. |
| glTF validation | [Khronos glTF Validator](https://github.com/KhronosGroup/glTF-Validator) | `RENDERDOC_ULTRA_GLTF_VALIDATOR` names the process-owner configured absolute executable. The Host runs the official CLI with resource validation and admits only a non-truncated JSON report with zero errors. |
| SPIR-V validation/conversion | [SPIRV-Tools](https://github.com/KhronosGroup/SPIRV-Tools) and [SPIRV-Cross](https://github.com/KhronosGroup/SPIRV-Cross) | Validate SPIR-V first, then cross-compile/reflection where the observed encoding supports it. |
| HLSL compilation | [DirectX Shader Compiler](https://github.com/microsoft/DirectXShaderCompiler) | Compiles reconstructed HLSL to DXIL/SPIR-V; it is not a general DXBC/DXIL decompiler. |
| Multi-target source | [Slang](https://github.com/shader-slang/slang) | Optional compiler for an already reconstructed, semantically reviewed source lane. |
| DDS processing | [DirectXTex](https://github.com/microsoft/DirectXTex) | Optional registered derivative conversion. Original DDS and resource/view metadata remain authoritative. |
| Unity | [Unity URP custom Shader guidance](https://docs.unity3d.com/6000.0/Documentation/Manual/urp/urp-shaders/birp-urp-custom-shader-upgrade-guide.html) | `RENDERDOC_ULTRA_UNITY_VALIDATOR` optionally names a process-owner configured adapter. It must import the package, compile the Shader, validate the scene, and write the strict Host-bound receipt described below. |
| Blender | [Blender glTF importer](https://docs.blender.org/manual/en/latest/addons/import_export/scene_gltf2.html) | `RENDERDOC_ULTRA_BLENDER` names Blender. The Host runs it in background/factory-startup mode with auto-execution disabled and a Host-generated importer/material/scene validator. A `.blend` in the validation area is evidence only after every required check passes. |
| FBX derivative | Autodesk FBX SDK, Blender CLI, or `ufbx-write` | Optional only. The receipt must name the configured provider and preserve anything it cannot encode in the sidecar. |

SPIRV-Tools, SPIRV-Cross, Slang, and the Khronos validator use permissive
licenses; DXC is LLVM-derived; DirectXTex and RenderDoc are MIT. Blender is run
as a separate GPL program, not linked into ReplayLab. Autodesk FBX SDK remains
an optional system dependency subject to its own EULA. Provider packaging must
review the exact upstream release and license rather than relying on this
summary.

## Package shape

The managed package contains the portable target plus immutable evidence:

```text
<managed-root>/operations/<operation-id>/
  operation.json
  ticket.json
  capture/
    manifest.json
    mesh/*
    textures/*.dds
    shaders/*
    cbuffers/*
  target/
    asset.gltf
    asset.bin
    transfer-spec.json
    package-manifest.json
    source/*                   # immutable copy of capture evidence
    UnityURP/*                 # when requested
    Blender/*                  # when requested
  validation/<validation-request-sha256>/
    request.json
    receipt.json
    gltf/*                     # Khronos report and bounded logs
    blender/*                  # Host script, receipt, logs, artifacts
    unity/*                    # strict request, receipt, logs, artifacts
```

Every captured or packaged payload is recorded relative to its managed capture
or target root with a byte count and SHA-256. The target manifest records
preserved, substituted, unsupported, and omitted data separately. A validator
that did not run is `unavailable`, never a pass. Validation output is outside
`target/`, so providers can create reports and derivatives without modifying the
immutable package they inspect.

## Target-validation lifecycle

After `finalize_resource_export`, call the effectful
`run_resource_export_target_validation(operation_id)` tool. The caller supplies
only the opaque operation ID. Executable paths, package paths, scripts,
arguments, timeouts, log locations, and output locations all come from fixed
Host configuration. The Host launches providers without a shell, caps runtime
and log size, and re-hashes the complete target package after the providers
return.

The validation request binds the operation-request SHA-256, immutable package-
manifest SHA-256, finalize SHA-256, exact requested targets, and every provider
executable SHA-256. Its own SHA-256 is the validation cache key. A repeat call
revalidates and reuses the matching receipt; changing a provider or package
produces a different request or fails integrity validation.

`validate_resource_export(operation_id)` remains read-only. It never launches a
program and never upgrades provider detection to success. It recomputes package
hashes, loads only the exact cached receipt, verifies every recorded artifact,
and sets `targetValidationComplete=true` only when every requested layer has
genuinely passed. A missing or not-yet-run provider leaves completion false. A
provider failure or malformed/tampered receipt makes validation invalid.

### Provider receipt protocol

Blender receipts use
`replaylab-resource-export-blender-provider-receipt/v1`; Unity receipts use
`replaylab-resource-export-unity-provider-receipt/v1`. Both echo the exact
operation ID, validation-request SHA-256, operation-request SHA-256, package-
manifest SHA-256, and provider-executable SHA-256. They also include a real
provider version, boolean required checks, and a complete relative-path artifact
manifest with byte counts and SHA-256 values. The Host rejects redirected,
missing, extra, duplicate, out-of-root, oversized, or hash-mismatched artifacts.

The fixed Blender route requires glTF import, mesh integrity, Principled
material-node construction, image color-space setup, and scene-link integrity.
It currently fails closed when capture material CBuffer mapping, a custom Shader
strategy, or texture-node reconstruction cannot be proven. The scaffold is not
promoted to a validated `.blend` merely because Blender saved a file. The
optional Unity provider must independently pass mesh import, material import,
texture import, Shader compilation, and scene integrity. Its request contains
only Host-derived managed paths and relative output names.

## Current fixed scope

The P1 bridge exports conventional raster draw input (`VSIn`) for `view=0`.
Indexed state comes from RenderDoc action flags, not a possibly stale index
buffer; base vertex, first vertex, instance offset, and instance divisor are
handled explicitly. DDS export preserves all known MSAA samples as array slices.

Packed/special vertex formats retain exact raw bytes and remain canonical-
unsupported. Primitive restart currently retains source indices but blocks glTF
finalization until deterministic primitive splitting is implemented. `view>0`,
post-transform/mesh-shader geometry, authored skeletons/animation, and raw
push/root/inline CBuffer bytes are explicit unsupported or unavailable states.
Buffer-backed CBuffer bytes remain authoritative; the legacy live-value tree is
convenience evidence and may truncate wide vectors or omit trailing zero lanes.

`validate_resource_export` separates internal capture/package checks from
external glTF, Unity, and Blender validation. Unity and Blender package outputs
remain scaffolds until the corresponding target program produces an admitted
receipt; a generated bundle alone is not a `.blend` or Unity import-success
claim.

## Fidelity profiles

- `exact-capture/pass-bundle`: preserve multiple EIDs, MRT/GBuffer resources,
  and separate Shader stages/passes.
- `portable-forward`: retain the draw-local material/surface calculation and
  replace ordinary camera, light, shadow, fog, and environment services with
  target-native equivalents.
- `bake-fallback`: bake a bounded dependency only when the user accepts losing
  its dynamic behavior.

Single-frame input-assembler data does not prove skeleton hierarchy or
animation. Post-VS/GS/mesh output is a posed or baked fallback and must not be
reported as the original authored rig.
