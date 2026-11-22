# Executable Unity effect contract

Use `unity.effect` for a geometry-free dispatch or fullscreen effect. Keep the
organized captured algorithm and its authoritative CBuffer mappings; this object
describes the derived Unity execution. `unity.assets` manifests the implementation
files, and `unity.renderPlan` describes the corresponding version-2 dependency
graph. The import request then has `executionKind: effect`, no fabricated mesh,
and captured material constants carried as runtime bindings.

## Resources and initialization

`effect.resources` contains 1–128 entries with a unique `name`, integer `width`
and `height` (1–16384), Unity GraphicsFormat `format`, and `initialization`.
Resources are currently 2D RenderTextures with one sample; the total allocation
is bounded to 64 million pixels. Set `randomWrite: true` for UAV writes; compute
pass outputs enable it automatically. Check actual format Render/LoadStore support
and use a supported implementation or explicit format adaptation when needed.

Every initialization declares `provenance: {kind, reason}`, where `kind` is
`captured`, `inferred` or `unity-native` and `reason` describes the evidence.
`kind: clear` initialization also requires four finite components in `value`.
`kind: texture` selects exactly one of `textureProperty` (an imported texture's
mapped property) or `textureAsset` (a manifested data asset). During export,
`textureAsset` names a `unity.assets` entry with `kind: data`. During an in-project
repair, the configuration sidecar can instead select a canonical project path
such as `Assets/BreadRecovery/<restorationId>/Seed.png`; author it, refresh Unity,
then configure and verify the actual imported texture.

Optional `historyFrom` names another allocated resource. Between warm-up
iterations its produced contents are copied into this resource. Source and
history must have compatible dimensions/formats and distinct names. Resources
are reinitialized for each measured execution; this runner does not imply
cross-camera-frame persistent history. `warmupFrames` accepts 0–64 and produces
that many preparatory iterations followed by one measured final iteration.
Use an actual custom persistent component when the algorithm needs continuing
history beyond this bounded reconstruction.

## Executable passes

`effect.passes` contains 1–128 entries. Each needs unique `id`, `kind`, actual
`entryPoint`, actual `insertionPoint`, `reads` and `writes` arrays of graph
resource-version names, `output` naming an allocated resource, and `outputVersion`
identifying one of its written versions. `effect.output` selects the final
allocated resource returned to the camera. Optional `globalTextureName` also
publishes a pass output for an explicitly selected global-texture target.

- `compute`: `asset` selects a manifested compute file, `kernel` is its actual
  kernel, and `textures` maps kernel property names to allocated resource names.
  Optional `groups: [x,y,z]` contains 1–65535 dispatch groups per dimension;
  otherwise group counts cover the output width/height using the kernel's real
  thread-group sizes, with one group in Z. Guard edge threads in the kernel.
- `fullscreen`: `asset` selects a manifested Shader, optional `source` selects
  its blit source, `textures` binds named resource inputs, and `materialPass`
  selects the actual Shader pass (default 0). Source and output must differ.
- `copy`: `source` and `output` select distinct compatible allocations; the
  runner calls `Graphics.CopyTexture`. Its bundled implementation asset is
  `Packages/com.bread.renderdoc/Runtime/BreadCaptureEffect.cs`; include that exact
  path in the plan's `implementationAssets` and corresponding pass implementation.
  The actual package dependency is hashed and verified with the observation;
  do not substitute a logical method name for a source asset path.
- `custom`: `asset` selects the manifested script, `componentType` is its full
  MonoBehaviour type implementing `IBreadCaptureEffectPass`, and optional
  `settings` supplies its serialized fields. Implement
  `Execute(BreadCaptureEffect effect, Camera camera, BreadEffectPass pass)`;
  obtain inputs and destination through `effect.Resource(name)` and write the
  actual `effect.Resource(pass.output)` contents. The wrapper records execution
  after the callback; an empty callback is not an algorithm implementation.

The built-in effect runner executes at the camera's end (`AfterRendering`).
An `insertionPoint` string does not schedule a different point. Effects requiring
depth, transparency or another earlier stage need an actual pipeline extension
(Renderer Feature, Custom Pass or command buffer) at that point, with matching
execution evidence. Keep configuration and recorded scheduling truthful.

`renderPlan.passes[].implementation.kind` uses `compute` for compute,
`draw` for fullscreen, and `custom` for custom/copy execution. Pass IDs,
implementation asset paths, entry points, insertion points, reads/writes and
output descriptors must match the actual execution receipt. Allocation names
(`history`, `output` below) and graph versions (`history`, `resolved`) are separate
identities; `outputVersion` links them. Warm-up records do not masquerade as extra
passes: only the final iteration records the plan's ordered pass executions.

## Minimal temporal compute example

This is the synthetic 64×64 convergence example used in the effect tests, not a
substitute for the selected EID's algorithm. It assumes the selected captured
CBuffer field has already been mapped to `_BaseColor`; its actual value is
uploaded through the normal capture mapping. Replace the algorithm, dimensions,
pipeline/version, inputs and seed reasoning with the selected effect's evidence.
The following is the `unity` object of the transfer specification:

```json
{
  "pipelinePolicy": {"targetPipeline": "built-in"},
  "assets": [{
    "path": "Effects/Temporal.compute",
    "kind": "compute",
    "content": "#pragma kernel CSMain\nRWTexture2D<float4> Result;\nTexture2D<float4> History;\nfloat4 _BaseColor;\n[numthreads(8,8,1)] void CSMain(uint3 id:SV_DispatchThreadID) { uint w,h; Result.GetDimensions(w,h); if(id.x>=w || id.y>=h) return; Result[id.xy]=lerp(History[id.xy],_BaseColor,0.5); }\n"
  }],
  "effect": {
    "resources": [
      {"name": "history", "width": 64, "height": 64,
       "format": "R32G32B32A32_SFloat", "historyFrom": "output",
       "initialization": {"kind": "clear", "value": [0,0,0,1],
         "provenance": {"kind": "inferred", "reason": "Synthetic zero-history seed with three convergence iterations."}}},
      {"name": "output", "width": 64, "height": 64,
       "format": "R32G32B32A32_SFloat", "randomWrite": true,
       "initialization": {"kind": "clear", "value": [0,0,0,1],
         "provenance": {"kind": "inferred", "reason": "Explicit scratch initialization; the kernel writes every pixel."}}}
    ],
    "warmupFrames": 3,
    "output": "output",
    "passes": [{"id": "temporal", "kind": "compute",
      "asset": "Effects/Temporal.compute", "kernel": "CSMain",
      "entryPoint": "CSMain", "insertionPoint": "AfterRendering",
      "reads": ["history"], "writes": ["resolved"],
      "output": "output", "outputVersion": "resolved",
      "textures": {"Result": "output", "History": "history"}, "groups": [8,8,1]}]
  },
  "renderPlan": {
    "schemaVersion": 2,
    "target": {"pipeline": "built-in", "editorVersion": "6000.0.55f1", "insertionPoint": "AfterRendering"},
    "externalInputs": ["history"], "outputs": ["resolved"],
    "implementationAssets": ["Effects/Temporal.compute"],
    "resources": [
      {"version": "history", "width": 64, "height": 64,
       "format": "R32G32B32A32_SFloat", "samples": 1, "lifetime": "history",
       "historyInitialization": "Inferred zero seed with three temporal warm-up iterations."},
      {"version": "resolved", "width": 64, "height": 64,
       "format": "R32G32B32A32_SFloat", "samples": 1, "lifetime": "frame"}
    ],
    "passes": [{"id": "temporal", "reads": ["history"], "writes": ["resolved"],
      "insertionPoint": "AfterRendering",
      "implementation": {"kind": "compute", "asset": "Effects/Temporal.compute", "entryPoint": "CSMain"},
      "attachments": [{"version": "resolved", "load": "clear", "store": "store", "clearValue": [0,0,0,1]}]}]
  }
}
```

Call `validate_unity_render_plan` with the nested `renderPlan` before finalization.
For repair files authored outside the immutable import, configure the same exact
canonical `Assets/BreadRecovery/...` asset path in both the effect and replacement
plan. Inspect, adopt the observed plan, render, compare and review the result.

## Buffer inputs and hidden counters

The actual field is `unity.runtimeBindings`, not `runtimeBuffers`. Buffer entries
use `name`, `type: buffer`, compatible `stride` and `bufferType` (`structured`,
`raw`, `append` or `counter`). Prefer `bufferSource` with the captured `resourceId`,
`phase: before-action` and a sufficiently specific `sourceUse`; the Host resolves
the captured window and preserves its identity and bytes. After-action snapshots
are reference outputs, not initial input data. Derived data can use explicit
`bufferBase64` (or `dataBase64`) with separate reconstruction evidence.

Append/counter entries additionally require integer `initialCounter` from zero
through the buffer capacity, plus
`counterProvenance: {kind: captured|inferred|unity-native, reason: ...}`. This
initializes the actual Unity counter; a hidden counter must not silently default
to zero. For example, the following **derived** four-element buffer has an inferred
two-element counter and is not claimed to be recovered capture data:

```json
{
  "name": "Items", "type": "buffer", "bufferType": "append", "stride": 4,
  "bufferBase64": "AAAAAAAAAAAAAAAAAAAAAA==", "initialCounter": 2,
  "counterProvenance": {"kind": "inferred", "reason": "Two active entries inferred from the inspected consumer bounds; payload is a generated seed."}
}
```

Record the exact missing-input/counter gap and implementation through
`record_unity_reconstruction`, then validate actual execution and visible output.
Inference must not replace available authoritative CBuffer fields or captured
input-buffer bytes. Known data remains subject to exact binding/value checks.
