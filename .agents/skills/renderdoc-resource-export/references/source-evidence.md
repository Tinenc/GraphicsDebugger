# Capture source evidence

Use the fields relevant to the user's requested export. The identity behind
each artifact is the capture/session, EID, API and pipeline, bound resource/view
and subresource or byte range, plus Shader stage and source hash when available.
A later UI selection does not retarget an active export.

## Geometry

For an input mesh, retain topology, indexed/indirect/instanced draw arguments,
index range and base vertex, vertex format/stride/offset, per-instance rate and
the actual Shader input mapping. Preserve custom channels or record an
explicitly unsupported mapping; a familiar semantic or filename alone does
not establish position, normal, tangent or UV role. Geometry generated after
the vertex inputs may be exportable, but label it as derived/post-transform
rather than the original authored mesh or rig.

## Textures and constants

For a relevant texture, retain resource and bound view identity, dimensions,
format, mip/slice/sample, channel mapping, sampling color space and sampler
behavior. A typeless resource may have several interpretations; use the view
bound at the selected EID. Do not infer sRGB, normal-map convention, alpha
meaning or material role from names alone.

Keep image-row orientation and any export-time row/color conversion separate
from mesh UV transforms and screen/render-target sampling coordinates. Record
which mip bytes were preserved; a decoded base-level image does not preserve
the captured mip chain. Keep unresolved sampler associations as candidates.

For a relevant CBuffer or push/root constant, retain binding, byte range,
layout/packing, type and observed value. Distinguish reusable material
parameters from camera, transform, frame and other system state by Shader use.
An unknown parameter can remain unknown; do not bake one captured system
value into a purported reusable material.

## Shader and pass dependencies

Retain the exact stage, source identity, input/output interface, resource
reads and output writes needed to explain the transferred effect. If the
selected draw depends on prior or later passes, blending, history, UAVs or
scene textures, identify the resource handoff and state whether the target
package includes, replaces, approximates or omits it. Preserve observed facts
separately from inferred pass purpose.

A mapping can be `observed`, deterministically `derived`, semantically
`inferred`, `unavailable` or `conflicting`. The requested result needs an
evidence-backed source or an explicit approximation/unknown for each material
output; conventional defaults must not silently fill visible gaps.
