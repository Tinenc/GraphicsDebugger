# Target transfer facts

Choose the Unity or Blender profile from the user's request and available
target tools. Record the chosen engine/render pipeline, mesh/material format,
coordinate convention, texture behavior and requested validation. A target
profile changes representation, not the captured source facts. Materially
different rendering choices should be stated; do not silently call an
approximation equivalent.

## Coordinate and texture mapping

Describe source-to-target axis, handedness, units, front-face winding and UV
convention together. Apply one consistent transform to geometry and related
instances, rigs and matrices. Positions use the affine transform; directions
use its linear part; normals use inverse-transpose. A reflection may require
winding and tangent-handedness changes. Apply unit scale once. Matrix layout
and multiplication convention are different questions; keep both explicit.

Use the sampled view and Shader behavior to select sRGB versus data/linear
texture treatment. Preserve relevant swizzles, normal-map convention, alpha,
sampler address/filter/compare and mip behavior, or record target limitations.
A filename or image preview cannot decide color space or sampling semantics.
If a mesh container cannot hold a captured channel, keep a mapped sidecar or
record the loss instead of silently dropping it.

Treat mesh UVs, encoded image-row order and render-target/screen UVs as separate
coordinate domains. Neither the source engine nor its graphics API proves a
global Y flip. Record export/import conversions and apply a correction once,
at the layer supported by evidence. A per-texture transform expressed against
captured UVs must compose with the inverse of a UV transform already baked into
the exported mesh; a transform against exported UVs applies directly. Absent
evidence, preserve the exported UVs and report orientation as unverified.

For Unity render targets, inspect the actual target API and URP path. Use the
applicable Unity screen helpers or `UNITY_UV_STARTS_AT_TOP`, texture texel-size
sign and `_ProjectionParams.x` evidence without repeating their correction in
mesh UVs or image rows. These flags describe runtime rendering conventions,
not an imported image's content. See Unity's [platform rendering differences](https://docs.unity3d.com/6000.0/Documentation/Manual/SL-PlatformDifferences.html).

Choose texture encoding and sampling independently of filenames or the project's
color-space setting. Match the EID's bound view, Shader decode and any image
conversion already performed. Unity's [`sRGBTexture`](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/TextureImporter-sRGBTexture.html)
controls GPU sRGB decoding in a Linear project; setting it does not prove the
source image is sRGB. Preserve normal, mask and other numeric data without gamma
conversion. Do not combine already-linearized pixels with another sRGB decode.
Unity [NormalMap import](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/TextureImporterType.NormalMap.html)
can repack channels, so enable it only with a matching target normal decoder;
retaining captured packing/custom decode normally needs raw data import.

Preserve the sampler used at each Shader sample, including min/mag/mip filtering,
addressing, anisotropy, LOD bias/range, comparison function and border color.
Texture import settings alone do not reproduce an arbitrary bound sampler:
Unity permits [separate/custom samplers](https://docs.unity3d.com/6000.0/Documentation/Manual/SL-SamplerStates.html),
and [`TextureWrapMode`](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/TextureWrapMode.html)
does not expose a captured border color. Record unsupported settings or an
explicit Shader substitute instead of silently treating them as Clamp/Point.
Generated mips differ from captured mips; account for explicit LOD/gradient
sampling and target [mip limits](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/QualitySettings-globalTextureMipmapLimit.html)
when comparing output.

## Engine adaptation

For Unity, choose URP material properties or ShaderLab/HLSL according to the
effect actually requested. Target-native transforms and lighting may replace
ordinary engine plumbing when that preserves the selected draw's distinctive
calculation. Record blend, cull, depth, alpha and pass settings that affect
output. A GBuffer or other pass dependency may require a renderer feature and
multiple graphics/compute passes. Trace and port the producer chain first;
consider a forward approximation or explicit omission only for a concrete
limitation, and keep any unmet requested fidelity visible in the review.

Establish each matrix's input/output spaces before replacing it. For ordinary
URP object/camera transforms, use `TransformObjectToHClip` or
`GetVertexPositionInputs`. Use screen helpers with the input space expected by
the installed URP version: `GetNormalizedScreenSpaceUV` consumes fragment
pixel-position coordinates, not a raw vertex clip-space XY pair. Its
[URP implementation](https://github.com/Unity-Technologies/Graphics/blob/master/Packages/com.unity.render-pipelines.universal/ShaderLibrary/ShaderVariablesFunctions.hlsl)
already normalizes and adjusts screen coordinates. CPU-created Unity camera
projections use [`GL.GetGPUProjectionMatrix`](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/GL.GetGPUProjectionMatrix.html)
with the actual render-target mode; do not feed an already device-adjusted
captured projection through it again. For scene-depth reconstruction, follow
URP's [depth/NDC conversion](https://docs.unity3d.com/6000.0/Documentation/Manual/urp/writing-shaders-urp-reconstruct-world-position.html)
using `UNITY_REVERSED_Z`, `UNITY_NEAR_CLIP_VALUE` and the matching inverse view
projection. Do not blindly apply `1-z` before a helper that already handles
the target convention. Validate against the selected pass with its viewport,
depth and render-target settings, not only a successful Shader compilation.

For Blender, map representable calculations to nodes and record renderer
differences that affect the result. Bind image color-space, UV set,
channel/normal interpretation and transparency from evidence. An assembled
node material is an approximation when the captured Shader operation cannot
be represented there; do not claim it reconstructs an original engine graph.

Deterministic decoding, repacking, hashing and validation belong in tools.
The Agent decides semantic mappings and whether a target-specific substitute
meets the user's requested fidelity.
